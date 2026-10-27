"""Foreground commands keep native lifetime custody across follow-up and worker loss."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import threading

import pytest

from tests.waiting import wait_until
from tools import process_registry as module
from tools.environments.local import LocalEnvironment
from tools.interrupt import request_yield, consume_yield, set_interrupt
from tools.process_registry_job import job_quiescent
from tools.terminal_tool_background import yield_to_background_handler

pytestmark = pytest.mark.platforms('linux')


def handler(command, root, task='foreground', route='route'):
    return yield_to_background_handler(command=command, env_type='local', cwd=str(root),
                                      effective_task_id=task, task_id=task, session_key=route)


@pytest.fixture
def scope_reaper():
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    previous = ctypes.c_int()
    assert libc.prctl(37, ctypes.byref(previous), 0, 0, 0) == 0
    assert libc.prctl(36, 1, 0, 0, 0) == 0
    adopted = []
    try:
        yield adopted
    finally:
        for pid in adopted:
            os.waitpid(pid, 0)
        assert libc.prctl(36, previous.value, 0, 0, 0) == 0


@pytest.fixture
def registry(monkeypatch, scope_reaper):
    registry = module.ProcessRegistry()
    monkeypatch.setattr(module, 'process_registry', registry)
    monkeypatch.setattr('tools.terminal_tool_background._stamp_routing_if_gateway',
                        lambda registry, session, key: setattr(session, 'notify_on_complete', False))
    yield registry
    for session in list(registry._running.values()):
        registry.kill_process(session.id)


@pytest.mark.parametrize('reload_owner', [False, True], ids=['live', 'cold'])
def test_foreground_followup_adopts_existing_native_scope(tmp_path, registry, reload_owner):
    ready, gate, effect = (tmp_path / name for name in ('ready', 'gate', 'old-effect'))
    child = f'''import os, time
from pathlib import Path
os.setsid()
Path({str(ready)!r}).write_text(str(os.getpid()))
end=time.monotonic()+15
while time.monotonic()<end and not Path({str(gate)!r}).exists(): time.sleep(.01)
if Path({str(gate)!r}).exists(): Path({str(effect)!r}).touch()
'''
    parent = f"import subprocess,sys,time; print('partial',flush=True); subprocess.Popen([sys.executable,'-c',{child!r}]); time.sleep(15)"
    command = shlex.join([sys.executable, '-c', parent])
    env = LocalEnvironment(cwd=str(tmp_path))
    result = {}
    thread = threading.Thread(target=lambda: result.update(env.execute(
        command, timeout=20, yield_handler=handler(command, tmp_path))))
    thread.start()
    try:
        wait_until(ready.exists, what='actual detached command child')
        request_yield(thread.ident)
        thread.join(5)
        assert not thread.is_alive(), result
        session = registry.get(result['yielded_session_id'])
        assert 'partial' in result['output']
        assert session.native_job_scope, 'fresh foreground yield lost native scope'
        assert session.output_log and not session.persist_on_release
        pid = int(ready.read_text())
        if reload_owner:
            cold = module.ProcessRegistry()
            assert cold.recover_from_checkpoint() == 1
            session = cold.get(session.id)
            assert session.native_job_scope and session.output_log
            registry = cold
        killed = registry.kill_process(session.id)
        assert killed['status'] == 'killed', killed
        assert job_quiescent(session)
        assert not Path(f'/proc/{pid}').exists()
        gate.touch()
        assert not effect.exists()
        assert session.output_buffer.count('partial') == 1
    finally:
        consume_yield(thread.ident)
        if result.get('yielded_session_id'):
            registry.kill_process(result['yielded_session_id'])
        thread.join(20)
        env.cleanup()


def test_foreground_primary_exit_preserves_detached_service_and_can_stop_it(tmp_path, registry):
    ready = tmp_path / 'service'
    child = f"import os,time; from pathlib import Path; os.setsid(); Path({str(ready)!r}).touch(); time.sleep(15)"
    parent = f"import subprocess,sys; subprocess.Popen([sys.executable,'-c',{child!r}]); print('shell finished'); raise SystemExit(7)"
    command = shlex.join([sys.executable, '-c', parent])
    env = LocalEnvironment(cwd=str(tmp_path))
    try:
        result = env.execute(command, timeout=10, yield_handler=handler(command, tmp_path))
        assert result['returncode'] == 7 and 'shell finished' in result['output']
        wait_until(ready.exists)
        session = next(s for s in registry._running.values() if s.command == command)
        assert session.persist_on_release and not session.notify_on_complete
        assert not job_quiescent(session) and session.process.poll() is None
        assert registry.kill_all(task_id='foreground') == 0, 'ordinary retirement preserves service lifetime'
        assert registry.kill_process(session.id)['status'] == 'killed'
        assert job_quiescent(session)
    finally:
        env.cleanup()


def test_foreground_preserves_stdin_env_cwd_full_output_and_primary_status(tmp_path, registry):
    sub = tmp_path / 'sub'
    sub.mkdir()
    payload = "import os,sys; print(os.getcwd()); print(os.environ['FOREGROUND_TEST']); print(sys.stdin.read()); print('日'*75000)"
    command = 'cd ' + shlex.quote(str(sub)) + '; export FOREGROUND_TEST=kept; ' + shlex.join([sys.executable, '-c', payload])
    env = LocalEnvironment(cwd=str(tmp_path))
    try:
        result = env.execute(command, stdin_data='stdin retained', timeout=15,
                             yield_handler=handler(command, tmp_path))
        assert result['returncode'] == 0
        assert result['output'].count('日') == 75000
        assert 'stdin retained' in result['output'] and 'kept' in result['output']
        assert str(sub) in result['output'] and env.cwd == str(sub)
        session = next(s for s in list(registry._finished.values()) + list(registry._running.values())
                       if s.command == command)
        assert session._completion_event.wait(5)
        assert job_quiescent(session) and session.exit_code == 0
        assert registry.completion_queue.empty()
    finally:
        env.cleanup()


def test_foreground_interruption_reaps_scope_and_preserves_unrelated_job(tmp_path, registry):
    ready = tmp_path / 'ready'
    payload = f"import signal,time; from pathlib import Path; signal.signal(signal.SIGTERM,signal.SIG_IGN); Path({str(ready)!r}).touch(); time.sleep(15)"
    command = shlex.join([sys.executable, '-c', payload])
    neighbor = registry.spawn_local(shlex.join([sys.executable, '-c', 'import time; time.sleep(15)']),
                                    task_id='foreign', session_key='foreign')
    env = LocalEnvironment(cwd=str(tmp_path))
    result = {}
    thread = threading.Thread(target=lambda: result.update(env.execute(
        command, timeout=20, yield_handler=handler(command, tmp_path))))
    thread.start()
    try:
        wait_until(ready.exists)
        set_interrupt(True, thread_id=thread.ident)
        thread.join(10)
        assert not thread.is_alive() and result['hermes_interrupted']
        session = next(s for s in registry._finished.values() if s.command == command)
        assert job_quiescent(session)
        assert neighbor.process.poll() is None
    finally:
        set_interrupt(False, thread_id=thread.ident)
        thread.join(20)
        env.cleanup()


def test_worker_exit_after_foreground_yield_keeps_scope_output_and_cold_cleanup(tmp_path, registry, scope_reaper):
    ready = tmp_path / 'ready'
    code = '''import json,os,shlex,sys,threading,time
from pathlib import Path
from tools import process_registry as m
from tools.environments.local import LocalEnvironment
from tools.terminal_tool_background import yield_to_background_handler
from tools.interrupt import request_yield
root,ready=sys.argv[1:]
command=shlex.join([sys.executable,'-c',"import time; from pathlib import Path; print('before worker exit',flush=True); Path("+repr(ready)+").touch(); time.sleep(15)"])
env=LocalEnvironment(cwd=root)
handler=yield_to_background_handler(command=command,env_type='local',cwd=root,effective_task_id='task',task_id='task',session_key='route')
result={}
t=threading.Thread(target=lambda:result.update(env.execute(command,timeout=20,yield_handler=handler)))
t.start()
end=time.monotonic()+8
while not Path(ready).exists() and time.monotonic()<end: time.sleep(.01)
request_yield(t.ident);t.join(5)
assert not t.is_alive(),result
print(json.dumps(result),flush=True)
os._exit(0)
'''
    worker = subprocess.run([sys.executable, '-c', code, str(tmp_path), str(ready)],
                            cwd=Path(module.__file__).parents[1], env=os.environ.copy(),
                            capture_output=True, text=True, timeout=12, check=False)
    assert worker.returncode == 0, worker.stderr
    receipt = json.loads(worker.stdout.splitlines()[-1])
    assert registry.recover_from_checkpoint() == 1
    session = registry.get(receipt['yielded_session_id'])
    scope_reaper.append(session.pid)
    assert session.native_job_scope and session.output_log
    verdict = registry.kill_process(session.id)
    from tools.process_registry_job import read_job_receipt
    assert verdict['status'] == 'killed', (verdict, read_job_receipt(session), session)
    assert job_quiescent(session)
    assert 'before worker exit' in session.output_buffer


def test_old_foreground_handle_cannot_stop_a_handed_off_job(tmp_path, registry, monkeypatch):
    ready = tmp_path / 'handoff-ready'
    payload = f"import time; from pathlib import Path; Path({str(ready)!r}).touch(); time.sleep(15)"
    command = shlex.join([sys.executable, '-c', payload])
    env = LocalEnvironment(cwd=str(tmp_path))
    launch, captured, result = env._run_bash, [], {}
    def capture(*args, **kwargs):
        proc = launch(*args, **kwargs)
        captured.append(proc)
        return proc
    monkeypatch.setattr(env, '_run_bash', capture)
    thread = threading.Thread(target=lambda: result.update(env.execute(
        command, timeout=20, yield_handler=handler(command, tmp_path))))
    thread.start()
    try:
        wait_until(ready.exists)
        request_yield(thread.ident)
        thread.join(5)
        assert not thread.is_alive(), result
        session = registry.get(result['yielded_session_id'])
        assert registry.transfer_ownership(session.id, from_owner='foreground', to_owner='parent',
            to_task_id='parent', to_session_key='parent-route', to_parent_session_id='native-parent',
            note='continue', child_stop_event=None) is session
        refused = captured[0].kill()
        assert refused['status'] == 'ownership_changed' and session.process.poll() is None
        assert not job_quiescent(session)
        assert registry.kill_process(session.id)['status'] == 'killed'
        assert job_quiescent(session)
    finally:
        consume_yield(thread.ident)
        thread.join(20)
        env.cleanup()


def test_foreground_registration_failure_never_replays_original_command(tmp_path, registry, monkeypatch):
    starts = tmp_path / 'starts'
    payload = f"import os,time; from pathlib import Path; Path({str(starts)!r}).open('a').write(str(os.getpid())+'\\n'); time.sleep(15)"
    command = shlex.join([sys.executable, '-c', payload])
    track, captured = registry._track_started, []
    def fail_once(session, *args, **kwargs):
        captured.append(session)
        if len(captured) == 1:
            wait_until(starts.exists)
            raise RuntimeError('reader failed after native foreground start')
        return track(session, *args, **kwargs)
    monkeypatch.setattr(registry, '_track_started', fail_once)
    env = LocalEnvironment(cwd=str(tmp_path))
    try:
        with pytest.raises(RuntimeError, match='after native foreground start'):
            env.execute(command, timeout=20, yield_handler=handler(command, tmp_path))
        session = captured[0]
        assert session._completion_event.wait(10)
        assert job_quiescent(session)
        assert len(starts.read_text().splitlines()) == 1
        assert all(item is session for item in captured)
    finally:
        env.cleanup()
