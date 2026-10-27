"""Follow-up yielding preserves the original native job across Stop, reload and FIFO."""
import asyncio
import json
from pathlib import Path
import shlex
import sys
import threading

import psutil
import pytest

from hermes_state_runtime import RuntimeStoreError, list_session_admissions
from tests.gateway.test_native_job_scope_stop import live, spawn
from tests.gateway.test_session_authority_cancel_settlement import ACTOR, REF, _authority, _submit
from tests.tools.test_foreground_job_scope import capture_foreground_output
from tests.waiting import async_wait_until, wait_until

pytestmark = pytest.mark.platforms('linux')


@pytest.mark.asyncio
@pytest.mark.parametrize('reload_registry', [False, True], ids=['live', 'cold'])
@pytest.mark.parametrize('lose_receipt', [False, True], ids=['physical-stop', 'unknown-retry'])
async def test_foreground_followup_then_stop_quiesces_before_human_fifo(tmp_path, monkeypatch,
                                                                      reload_registry, lose_receipt, record_property):
    from gateway.session_ancillary import process_stop_pending
    from gateway.session_context import clear_session_vars, set_session_vars
    from tools import process_registry as module
    from tools.environments.local import LocalEnvironment
    from tools.interrupt import request_yield, consume_yield
    from tools.process_registry_job import job_quiescent, job_receipt_path, read_job_receipt, request_job_stop
    from tools.terminal_tool_background import yield_to_background_handler
    from utils import atomic_json_write
    db, authority = _authority(tmp_path, monkeypatch)
    original = module.ProcessRegistry()
    monkeypatch.setattr(module, 'process_registry', original)
    ready, release, effect = (tmp_path / name for name in ('child', 'release', 'old-effect'))
    child_code = f'''from pathlib import Path
import os,signal,time
os.setsid(); signal.signal(signal.SIGTERM,signal.SIG_IGN)
Path({str(ready)!r}).write_text(str(os.getpid()))
p=Path({str(release)!r}); end=time.monotonic()+30
while not p.exists() and time.monotonic()<end: time.sleep(.01)
if p.exists(): Path({str(effect)!r}).touch()
'''
    command = shlex.join([sys.executable, '-c',
        f"import subprocess,sys,time; print('original foreground output',flush=True); subprocess.Popen([sys.executable,'-c',{child_code!r}]); time.sleep(30)"])
    env = LocalEnvironment(cwd=str(tmp_path))
    collectors = capture_foreground_output(monkeypatch)
    result, executed = {}, []
    def foreground():
        tokens = set_session_vars(session_id='s', session_key='route')
        try:
            handler = yield_to_background_handler(command=command, env_type='local', cwd=str(tmp_path),
                effective_task_id='worker-uuid', task_id='worker-uuid', session_key='route')
            result.update(env.execute(command, timeout=40, yield_handler=handler))
        finally:
            clear_session_vars(tokens)
    thread = threading.Thread(target=foreground)
    sleeper = shlex.join([sys.executable, '-c', 'import time; time.sleep(30)'])
    neighbors = [await spawn(original, tmp_path, sleeper, parent='foreign-parent'),
                 await spawn(original, tmp_path, sleeper, route='foreign-route')]
    own, child, receipt, saved = None, None, None, None
    async def execute(owner, ref, row):
        assert job_quiescent(own) and not live(child)
        assert all(not neighbor.exited for neighbor in neighbors)
        executed.append(row['request_id'])
        release.touch()
        return 'human continued'
    monkeypatch.setattr('gateway.session_finite.execute_finite_admission', execute)
    thread.start()
    try:
        await asyncio.to_thread(wait_until, ready.exists, timeout=15)
        await asyncio.to_thread(wait_until,
            lambda: any('original foreground output' in output.render() for output in collectors),
            what='foreground partial output actually captured')
        request_yield(thread.ident)
        await asyncio.to_thread(thread.join, 8)
        assert not thread.is_alive(), result
        own = original.get(result['yielded_session_id'])
        child = psutil.Process(int(ready.read_text()))
        assert own.native_job_scope and own.parent_session_id == 's'
        assert own.owner_task_id == 'worker-uuid' and own.session_key == 'route'
        assert 'original foreground output' in result['output']
        current = original
        if reload_registry:
            current = module.ProcessRegistry()
            monkeypatch.setattr(module, 'process_registry', current)
            assert current.recover_from_checkpoint() == 3
            assert current.get(own.id).native_job_scope
        receipt = job_receipt_path(own.id)
        if lose_receipt:
            assert read_job_receipt(own) is not None, 'Original producer must have captured authentic native identity'
            saved = receipt.read_bytes()
            receipt.unlink()
            with pytest.raises(RuntimeStoreError, match='process_stop_unknown'):
                await authority.interrupt(ACTOR, REF, authority._handle(REF).execution_generation)
            assert process_stop_pending(db, 's') and live(child)
            await _submit(authority, 'human continuation')
            await asyncio.wait_for(authority.sessions['s'].task, 10)
            assert executed == []
            assert any(row['status'] == 'queued' for row in list_session_admissions(db, session_id='s'))
            atomic_json_write(receipt, json.loads(saved), mode=0o600)
            assert read_job_receipt(own) == json.loads(saved), 'Authentic native evidence must be fully published'
            # A new ordinary human message drives native recovery; the first
            # queued human request remains ahead of it, with no manual Stop retry.
            await _submit(authority, 'follow-up human continuation')
            expected = ['human continuation', 'follow-up human continuation']
        else:
            try:
                await authority.interrupt(ACTOR, REF, authority._handle(REF).execution_generation)
                first_stop = 'quiescent'
            except RuntimeStoreError as error:
                assert error.reason == 'process_stop_unknown'
                first_stop = 'unknown_pending'
                assert process_stop_pending(db, 's') and executed == []
            record_property('first_stop_outcome', first_stop)
            await _submit(authority, 'human continuation')
            expected = ['human continuation']
        # An uncertain drain may finish paused before the real resource does.
        # Its native completion wakes a later drain, without another message.
        await async_wait_until(lambda: executed == expected and authority.sessions['s'].task.done(), timeout=15)
        assert job_quiescent(own) and not live(child), {
            'receipt': read_job_receipt(own), 'pending': process_stop_pending(db, 's'),
            'native': own.native_job_scope, 'pid': own.pid, 'birth': own.host_start_time,
            'exited': own.exited, 'completion': own._completion_event.is_set(),
            'executed': executed, 'checkpoint': module._checkpoint_path().read_text()}
        assert process_stop_pending(db, 's') is None
        assert executed == expected and not effect.exists()
        assert own.output_buffer.count('original foreground output') == 1
    finally:
        consume_yield(thread.ident)
        if receipt is not None and saved is not None and not receipt.exists():
            atomic_json_write(receipt, json.loads(saved), mode=0o600)
        for session in [own, *neighbors]:
            if session is not None:
                await asyncio.to_thread(request_job_stop, original, session)
                await asyncio.to_thread(session._completion_event.wait, 10)
        await asyncio.to_thread(thread.join, 10)
        env.cleanup()
        db.close()
