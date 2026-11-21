"""A live native completion retains its original grant until delivery settles."""
import json
import shlex
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.waiting import wait_until

pytestmark = pytest.mark.platforms('linux')


@pytest.mark.parametrize('pressure', ['age', 'cap'])
@pytest.mark.parametrize('disposition', ['pending', 'revoked', 'delivered'])
def test_long_running_native_completion_keeps_live_origin_through_foreground_spawn(
        tmp_path, monkeypatch, pressure, disposition):
    from gateway.session_context import clear_session_vars, set_session_vars
    from gateway.session_approval_completion import completion_reservation_for_event, release_completion_event_reservation
    from hermes_constants import get_hermes_home
    from tools import process_registry as module
    from tools.approval_context import approval_task_context, release_background_approval_task
    from tools.environments.local import LocalEnvironment
    from tools.terminal_tool_background import spawn_background_process, yield_to_background_handler
    registry = module.ProcessRegistry()
    monkeypatch.setattr(module, 'process_registry', registry)
    ready, release = tmp_path / 'ready', tmp_path / 'release'
    payload = f'''from pathlib import Path
import time
Path({str(ready)!r}).touch()
end=time.monotonic()+20
while not Path({str(release)!r}).exists() and time.monotonic()<end: time.sleep(.01)
print('original completed output',flush=True)
'''
    command = shlex.join([sys.executable, '-c', payload])
    profile, route = str(get_hermes_home()), 'native-route'
    tokens = set_session_vars(platform='tui', source='tui', session_id=route, session_key=route)
    reservation, event, env = None, None, None
    try:
        def current_owner():
            assert registry._lock.acquire(timeout=5), 'owner callback ran under registry lock'
            registry._lock.release()
            return True
        with approval_task_context('Inspect only the original build', identity='fixture:user', profile=profile,
                session=route, task_id='original-human-request', is_current=current_owner):
            started = json.loads(spawn_background_process(command=command, env=SimpleNamespace(env={}),
                env_type='local', effective_task_id='original', task_id='original', session_key=route,
                workdir=None, cwd=str(tmp_path), effective_pty=False, notify_on_complete=True,
                watch_patterns=None, approval_note=None, pty_disabled_reason=None))
        process = registry.get(started['session_id'])
        reservation = process.approval_task_reservation
        assert process.native_job_scope and reservation.active()
        wait_until(ready.exists)
        # Model the elapsed duration of a real long-running producer, without
        # changing the captured kernel identity or fabricating a scope verdict.
        if pressure == 'age':
            with registry._lock, process._lock:
                process.started_at = time.time() - module.FINISHED_TTL_SECONDS - 1
        else:
            monkeypatch.setattr(module, 'MAX_PROCESSES', 1)
        assert registry._write_checkpoint()
        release.touch()
        assert process._completion_event.wait(10)
        event = registry.completion_queue.get(timeout=5)
        assert completion_reservation_for_event(event, route, profile) is reservation
        if pressure == 'cap':
            from dataclasses import replace
            from tools import process_registry_results as results
            for index in range(results.MAX_RETAINED_RESULTS + 1):
                history = replace(process, id=f'proc_output_history_{index}', pid=None, host_start_time=None,
                    native_job_scope=False, output_log='',
                    parent_session_id='history', task_id='history', owner_task_id='history',
                    session_key='history', notify_on_complete=False)
                # History pressure persists without claiming original-publisher finality.
                assert not results.save_completed_result(history)
            assert len(results._result_paths()) == results.MAX_RETAINED_RESULTS == 64
            assert not (Path(profile) / 'logs' / 'process-results' / f'{process.id}.json').exists()
        if disposition == 'revoked':
            reservation.lease.revoke()
        if disposition == 'delivered':
            release_completion_event_reservation(event)
        env = LocalEnvironment(cwd=str(tmp_path))
        followup = 'printf next'
        handler = yield_to_background_handler(command=followup, env_type='local', cwd=str(tmp_path),
            effective_task_id='followup', task_id='followup', session_key=route)
        assert env.execute(followup, timeout=10, yield_handler=handler)['returncode'] == 0
        if disposition == 'pending':
            assert completion_reservation_for_event(event, route, profile) is reservation
            assert reservation.active() and reservation.evidence.text == 'Inspect only the original build'
            release_completion_event_reservation(event)
        else:
            assert completion_reservation_for_event(event, route, profile) is None
        assert process.approval_task_reservation is None and not reservation.active()
        with registry._lock:
            registry._prune_if_needed()
        assert process.id not in registry._finished
    finally:
        release.touch()
        if event is not None:
            release_completion_event_reservation(event)
        release_background_approval_task(reservation)
        for own in list(registry._running.values()):
            registry.kill_process(own.id)
        if env is not None:
            env.cleanup()
        clear_session_vars(tokens)
