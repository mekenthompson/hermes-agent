"""Native command custody survives outer retry, deadline and host-exit boundaries."""
import builtins
import json
import os
from pathlib import Path
import shlex
import sys
from types import SimpleNamespace

import pytest

from tests.waiting import wait_until
from tools import process_registry as module
from tools import process_registry_chat_execution as execution
from tools import terminal_tool as terminal
from tools.environments import base
from tools.environments.local import LocalEnvironment
from tools.process_registry_job import job_quiescent, read_job_receipt

pytestmark = pytest.mark.platforms('linux')


@pytest.fixture
def registry(monkeypatch):
    registry = module.ProcessRegistry()
    monkeypatch.setattr(module, 'process_registry', registry)
    yield registry
    for session in list(registry._running.values()):
        registry.kill_process(session.id)


def outer(command, env, root):
    plan = terminal._ExecPlan(config={}, env_type='local', effective_task_id='boundary', image='',
                             cwd=str(root), host_cwd=None, effective_timeout=15)
    return json.loads(terminal._run_foreground(command, env, plan, task_id='boundary', session_id='boundary',
        session_key='route', workdir=str(root), approval_note=None, clear_interrupt=False, metered=False))


@pytest.mark.parametrize('phase', ['record', 'registration', 'descriptor-close', 'handle', 'stdin',
                                   'capture', 'wait', 'result'])
def test_outer_terminal_never_replays_any_native_postlaunch_failure(tmp_path, monkeypatch, registry, phase):
    starts = tmp_path / 'starts'
    payload = (f"from pathlib import Path; import time; print('original output',flush=True); "
               f"Path({str(starts)!r}).open('a').write('ran\\n'); "
               + ('' if phase == 'result' else 'time.sleep(12)'))
    command = shlex.join([sys.executable, '-c', payload])
    env = LocalEnvironment(cwd=str(tmp_path))
    failures = []
    def fail_once(original):
        def wrapped(*args, **kwargs):
            if not failures:
                wait_until(starts.exists, what='original command actually ran')
                failures.append(phase)
                raise RuntimeError('postlaunch ' + phase + ' failure')
            return original(*args, **kwargs)
        return wrapped
    try:
        seams = {'record': (execution, 'record_local_process'), 'registration': (registry, '_track_started'),
                 'handle': (execution, '_ForegroundJob'), 'capture': (base, '_new_output_collector'),
                 'wait': (env, '_wait_for_process'), 'result': (env, '_update_cwd')}
        if phase in seams:
            owner, name = seams[phase]
            monkeypatch.setattr(owner, name, fail_once(getattr(owner, name)))
        if phase == 'descriptor-close':
            opened, original_open, original_close = [], module.open_output_log, os.close
            def open_log(path):
                fd = original_open(path)
                opened.append(fd)
                return fd
            def close(fd):
                original_close(fd)
                if opened and fd == opened[0] and not failures:
                    wait_until(starts.exists)
                    failures.append(phase)
                    raise OSError('postlaunch descriptor close failure')
            monkeypatch.setattr(module, 'open_output_log', open_log)
            monkeypatch.setattr(os, 'close', close)
        if phase == 'stdin':
            from tools.environments import base_output
            monkeypatch.setattr(base_output, '_pipe_stdin', fail_once(base_output._pipe_stdin))
            execute = env.execute
            monkeypatch.setattr(env, 'execute', lambda *args, **kwargs: execute(*args, stdin_data='input', **kwargs))
        result = outer(command, env, tmp_path)
        assert starts.read_text().splitlines() == ['ran'], result
        assert failures == [phase]
        assert result['status'] == 'execution_unknown' and result['retryable'] is False, result
        assert result['exit_code'] is None and 'not retried' in result['error']
        session = registry.get(result['session_id'])
        assert session is not None and session.native_job_scope
        assert session._completion_event.wait(10)
        assert job_quiescent(session)
        assert 'original output' in session.output_buffer
        assert Path(session.output_log).exists()
    finally:
        env.cleanup()


def test_outer_terminal_still_retries_a_verified_prelaunch_failure(tmp_path, monkeypatch, registry):
    starts = tmp_path / 'starts'
    command = shlex.join([sys.executable, '-c',
        f"from pathlib import Path; Path({str(starts)!r}).open('a').write('ran\\n'); print('done')"])
    env = LocalEnvironment(cwd=str(tmp_path))
    original, attempts = execution.start_local_process, []
    def unavailable_once(handle):
        attempts.append(handle)
        if len(attempts) == 1:
            assert not starts.exists()
            raise OSError('temporary prelaunch failure')
        return original(handle)
    monkeypatch.setattr(execution, 'start_local_process', unavailable_once)
    try:
        result = outer(command, env, tmp_path)
        assert result['exit_code'] == 0 and result['output'].strip() == 'done', result
        assert len(attempts) == 2 and starts.read_text().splitlines() == ['ran']
    finally:
        env.cleanup()


def native_handle(registry, root):
    session = registry._new_session('finite boundary command', 'boundary', 'boundary', 'route', str(root))
    handle = execution.spawn_foreground(session,
        [sys.executable, '-c', 'import time; time.sleep(12)'], dict(os.environ), str(root), None)
    wait_until(lambda: read_job_receipt(session), what='real native helper readiness')
    return session, handle


@pytest.mark.parametrize('boundary', ['deadline', 'interrupt', 'force', 'shutdown', 'hard-exit'])
def test_native_refusal_never_falls_back_to_generic_pid_killing(tmp_path, monkeypatch, registry, boundary):
    from agent import deadline
    from tools import process_registry_job
    env = LocalEnvironment(cwd=str(tmp_path))
    session, handle = native_handle(registry, tmp_path)
    calls = []
    try:
        with monkeypatch.context() as patch:
            patch.setattr(process_registry_job, 'request_job_stop', lambda *_: False)
            patch.setattr(deadline, 'kill_process_tree', calls.append)
            if boundary == 'deadline':
                verdict = env._kill_spawned_tree(handle)
                assert verdict['error'] == 'process_stop_unknown'
            elif boundary == 'interrupt':
                assert env._kill_process(handle)['error'] == 'process_stop_unknown'
            elif boundary == 'force':
                assert env._force_kill_process(handle)['error'] == 'process_stop_unknown'
            else:
                patch.setattr(base, '_live_foreground', {id(handle): (env, handle)})
                patch.setattr(base, '_spawns_in_flight', 0)
                patch.setattr(base, '_exit_fenced', False)
                assert base.kill_live_foreground_processes(now=boundary == 'hard-exit') == 1
            assert calls == []
            assert session.process.poll() is None and not job_quiescent(session)
            assert session._scope_stop_pending
            assert session.id in {row['session_id'] for row in json.loads(module._checkpoint_path().read_text())}
        assert registry.kill_process(session.id)['status'] == 'killed'
        assert job_quiescent(session)
    finally:
        registry.kill_process(session.id)
        env.cleanup()


def test_native_cleanup_exception_does_not_authorize_generic_kill(tmp_path, monkeypatch, registry):
    from agent import deadline
    env = LocalEnvironment(cwd=str(tmp_path))
    session, handle = native_handle(registry, tmp_path)
    calls = []
    try:
        with monkeypatch.context() as patch:
            def refused(*args, **kwargs):
                raise OSError('native ownership evidence temporarily unreadable')
            patch.setattr(registry, 'kill_process', refused)
            patch.setattr(deadline, 'kill_process_tree', calls.append)
            assert env._kill_spawned_tree(handle)['error'] == 'process_stop_unknown'
            assert calls == [] and session.process.poll() is None
            assert session.id in {row['session_id'] for row in json.loads(module._checkpoint_path().read_text())}
        assert registry.kill_process(session.id)['status'] == 'killed'
        assert job_quiescent(session)
    finally:
        registry.kill_process(session.id)
        env.cleanup()


def test_old_deadline_cannot_generically_kill_new_parent_after_handoff(tmp_path, monkeypatch, registry):
    from agent import deadline
    env = LocalEnvironment(cwd=str(tmp_path))
    session, handle = native_handle(registry, tmp_path)
    calls = []
    try:
        assert registry.transfer_ownership(session.id, from_owner='boundary', to_owner='parent',
            to_task_id='parent', to_session_key='parent-route', to_parent_session_id='native-parent',
            note='continue', child_stop_event=None) is session
        monkeypatch.setattr(deadline, 'kill_process_tree', calls.append)
        assert env._kill_spawned_tree(handle)['status'] == 'ownership_changed'
        assert calls == [] and session.process.poll() is None
        assert registry.kill_process(session.id)['status'] == 'killed'
        assert job_quiescent(session)
    finally:
        registry.kill_process(session.id)
        env.cleanup()


def test_unmanaged_backend_keeps_ordinary_generic_cleanup_fallback(monkeypatch):
    from agent import deadline
    env = object.__new__(LocalEnvironment)
    proc = SimpleNamespace(pid=424242)
    calls = []
    monkeypatch.setattr(env, '_kill_process', lambda handle: calls.append(('backend', handle)))
    monkeypatch.setattr(deadline, 'kill_process_tree', lambda pid: calls.append(('tree', pid)))
    env._kill_spawned_tree(proc)
    assert calls == [('backend', proc), ('tree', proc.pid)]


def test_deadline_remembers_native_handle_published_after_bound(tmp_path, monkeypatch, registry):
    import threading
    from tools.terminal_tool_background import yield_to_background_handler
    env = LocalEnvironment(cwd=str(tmp_path))
    published, release, waited = threading.Event(), threading.Event(), threading.Event()
    spawn, wait = env._spawn_command, env._wait_for_process
    state = {}
    def held_spawn(*args, **kwargs):
        handle = spawn(*args, **kwargs)
        state['handle'] = handle
        published.set()
        assert release.wait(12)
        return handle
    def observed_wait(handle, **kwargs):
        state['quiescent_before_wait'] = job_quiescent(handle._hermes_job_session)
        waited.set()
        return wait(handle, **kwargs)
    monkeypatch.setattr(env, '_spawn_command', held_spawn)
    monkeypatch.setattr(env, '_wait_for_process', observed_wait)
    monkeypatch.setattr(base, '_EXECUTE_WAIT_BOUND_GRACE_S', .05)
    command = shlex.join([sys.executable, '-c', 'import time; time.sleep(20)'])
    handler = yield_to_background_handler(command=command, env_type='local', cwd=str(tmp_path),
        effective_task_id='boundary', task_id='boundary', session_key='route')
    try:
        result = env.execute(command, timeout=2, yield_handler=handler)
        assert result['hermes_timed_out']
        assert published.wait(10)
        assert not job_quiescent(state['handle']._hermes_job_session)
        release.set()
        assert waited.wait(10)
        assert state['quiescent_before_wait'], 'late published scope escaped the already expired deadline'
        assert state['handle']._hermes_job_session._completion_event.wait(10)
    finally:
        release.set()
        if 'handle' in state:
            registry.kill_process(state['handle']._hermes_job_session.id)
        env.cleanup()


def test_missing_real_native_receipt_keeps_job_and_checkpoint_until_authenticated_retry(tmp_path, monkeypatch, registry):
    from agent import deadline
    from tools.process_registry_job import job_receipt_path
    env = LocalEnvironment(cwd=str(tmp_path))
    session, handle = native_handle(registry, tmp_path)
    receipt = job_receipt_path(session.id)
    authentic = receipt.read_bytes()
    calls = []
    try:
        receipt.unlink()
        monkeypatch.setattr(deadline, 'kill_process_tree', calls.append)
        verdict = env._kill_spawned_tree(handle)
        assert verdict['error'] == 'process_stop_unknown'
        assert not calls and session.process.poll() is None and not job_quiescent(session)
        assert session._scope_stop_pending
        assert session.id in {row['session_id'] for row in json.loads(module._checkpoint_path().read_text())}
        receipt.write_bytes(authentic)
        assert env._kill_spawned_tree(handle)['status'] == 'killed'
        assert job_quiescent(session) and not calls
    finally:
        if not receipt.exists():
            receipt.write_bytes(authentic)
        registry.kill_process(session.id)
        env.cleanup()


def test_postlaunch_failure_cannot_reap_a_process_already_handed_to_parent(tmp_path, monkeypatch, registry):
    env = LocalEnvironment(cwd=str(tmp_path))
    captured = []
    def transferred_handle(owner, session, ownership):
        captured.append(session)
        assert registry.transfer_ownership(session.id, from_owner='boundary', to_owner='parent',
            to_task_id='parent', to_session_key='parent-route', to_parent_session_id='native-parent',
            note='continue', child_stop_event=None) is session
        raise RuntimeError('handle setup failed after accepted handoff')
    monkeypatch.setattr(execution, '_ForegroundJob', transferred_handle)
    command = shlex.join([sys.executable, '-c', 'import time; time.sleep(12)'])
    try:
        result = outer(command, env, tmp_path)
        session = captured[0]
        assert result['status'] == 'execution_unknown' and result['session_id'] == session.id
        assert session.parent_session_id == 'native-parent' and session.ownership_generation == 1
        assert session.process.poll() is None and not job_quiescent(session), 'old launch cleanup killed new owner'
        assert registry.kill_process(session.id)['status'] == 'killed'
        assert job_quiescent(session)
    finally:
        for session in captured:
            registry.kill_process(session.id)
        env.cleanup()


def test_postlaunch_error_retains_scope_until_original_output_reader_finishes(tmp_path, monkeypatch, registry):
    import threading
    from tools.process_registry_results import load_completed_results
    env = LocalEnvironment(cwd=str(tmp_path))
    entered, release = threading.Event(), threading.Event()
    ingest = registry._ingest_output
    def held_ingest(session, text, **kwargs):
        entered.set()
        assert release.wait(12)
        return ingest(session, text, **kwargs)
    monkeypatch.setattr(registry, '_ingest_output', held_ingest)
    def failed_wait(*args, **kwargs):
        assert entered.wait(10)
        raise OSError('foreground wait failed while original log reader drains')
    monkeypatch.setattr(env, '_wait_for_process', failed_wait)
    command = shlex.join([sys.executable, '-c', "import time; print('original retained output',flush=True); time.sleep(15)"])
    try:
        result = outer(command, env, tmp_path)
        assert result['status'] == 'execution_unknown' and result['retryable'] is False
        session = registry.get(result['session_id'])
        assert job_quiescent(session)
        assert not session._completion_event.is_set(), 'cleanup finalized before the original reader finished'
        assert session._scope_stop_pending
        assert session.id in {row['session_id'] for row in json.loads(module._checkpoint_path().read_text())}
        release.set()
        assert session._completion_event.wait(10)
        retained = load_completed_results(owns_entry=lambda row: row['id'] == session.id, strict=True)[session.id]
        assert 'original retained output' in retained.output_buffer
    finally:
        release.set()
        for session in list(registry._running.values()):
            registry.kill_process(session.id)
        env.cleanup()


def test_handoff_before_handle_creation_does_not_grant_old_foreground_new_ownership(tmp_path, monkeypatch, registry):
    track = registry._track_started
    def handed_before_return(session, *args, **kwargs):
        result = track(session, *args, **kwargs)
        assert registry.transfer_ownership(session.id, from_owner='boundary', to_owner='parent',
            to_task_id='parent', to_session_key='parent-route', to_parent_session_id='native-parent',
            note='continue', child_stop_event=None) is session
        return result
    monkeypatch.setattr(registry, '_track_started', handed_before_return)
    session, handle = native_handle(registry, tmp_path)
    try:
        assert handle.kill()['status'] == 'ownership_changed'
        assert session.process.poll() is None and not job_quiescent(session)
        assert registry.kill_process(session.id)['status'] == 'killed'
        assert job_quiescent(session)
    finally:
        registry.kill_process(session.id)


def test_missing_original_receipt_does_not_substitute_helper_wait_status(tmp_path, monkeypatch, registry):
    import signal
    import threading
    from tools.process_registry_job import job_receipt_path
    from tools.process_registry_results import load_completed_results
    entered, release = threading.Event(), threading.Event()
    read = registry._log_reader_loop
    def held_reader(session):
        entered.set()
        assert release.wait(10)
        return read(session)
    monkeypatch.setattr(registry, '_log_reader_loop', held_reader)
    session = registry._new_session('real signalled primary', 'boundary', 'boundary', 'route', str(tmp_path))
    handle = execution.spawn_foreground(session, [sys.executable, '-c',
        'import os,signal; print("original signal payload",flush=True); os.kill(os.getpid(),signal.SIGKILL)'],
        dict(os.environ), str(tmp_path), None)
    receipt = job_receipt_path(session.id)
    original = None
    try:
        assert entered.wait(5)
        assert session.process.wait(5) == 128 + signal.SIGKILL
        original = receipt.read_bytes()
        assert json.loads(original)['exit_code'] == -signal.SIGKILL and session.exit_code is None
        receipt.unlink()
        with pytest.raises(base.CommandExecutionUnknown):
            handle.poll()
        assert registry.kill_process(session.id)['error'] == 'process_stop_unknown'
        assert session.id in {row['session_id'] for row in json.loads(module._checkpoint_path().read_text())}
        receipt.write_bytes(original)
        assert handle.poll() == -signal.SIGKILL
        release.set()
        assert session._completion_event.wait(5)
        retained = load_completed_results(owns_entry=lambda row: row['id'] == session.id, strict=True)[session.id]
        assert retained.exit_code == -signal.SIGKILL and retained.output_buffer == 'original signal payload\n'
        receipt.unlink()
        assert handle.poll() == -signal.SIGKILL  # The original reader already captured this status.
        assert registry.kill_process(session.id)['error'] == 'process_stop_unknown'
        receipt.write_bytes(original)
        assert registry.kill_process(session.id)['status'] == 'already_exited'
    finally:
        if original is not None and not receipt.exists():
            receipt.write_bytes(original)
        release.set()
        registry.kill_process(session.id)


@pytest.mark.parametrize('home_source', ['context', 'environment'])
def test_native_foreground_handle_keeps_producer_home_off_turn(tmp_path, monkeypatch, registry, home_source):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    producer, unrelated = tmp_path / 'producer', tmp_path / 'unrelated'
    producer.mkdir()
    unrelated.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(producer))
    token = set_hermes_home_override(producer if home_source == 'context' else None)
    try:
        session, handle = native_handle(registry, producer)
    finally:
        reset_hermes_home_override(token)
    try:
        monkeypatch.setenv('HERMES_HOME', str(unrelated))
        token = set_hermes_home_override(unrelated)
        try:
            result = handle.kill()
            assert result['status'] == 'killed', result
        finally:
            reset_hermes_home_override(token)
        assert (producer / 'logs' / 'process-results' / (session.id + '.json')).exists()
        assert not (unrelated / 'logs' / 'process-results' / (session.id + '.json')).exists()
        assert not (unrelated / 'processes.json').exists()
    finally:
        token = set_hermes_home_override(producer)
        try:
            registry.kill_process(session.id)
            assert job_quiescent(session)
        finally:
            reset_hermes_home_override(token)


def test_foreground_keeps_tail_published_between_read_and_exit_observation(tmp_path, monkeypatch):
    registry = module.ProcessRegistry()
    monkeypatch.setattr(module, 'process_registry', registry)
    ready, release = tmp_path / 'ready', tmp_path / 'release'
    payload = f'''from pathlib import Path
import time
print('prefix',flush=True)
Path({str(ready)!r}).touch()
end=time.monotonic()+15
while not Path({str(release)!r}).exists() and time.monotonic()<end: time.sleep(.01)
print('tail',flush=True)
raise SystemExit(7)
'''
    command = shlex.join([sys.executable, '-c', payload])
    session = registry._new_session(command, 'owner', 'owner', 'route', str(tmp_path))
    env = LocalEnvironment(cwd=str(tmp_path))
    handle = env._run_bash(command, native_session=session)
    try:
        wait_until(ready.exists, timeout=5)
        class EmptyReadBoundary:
            def __init__(self, stream):
                self.stream = stream
                self.released = False
            def __getattr__(self, name):
                return getattr(self.stream, name)
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return self.stream.__exit__(*args)
            def read(self, count):
                chunk = self.stream.read(count)
                if not chunk and not self.released:
                    self.released = True
                    release.touch()
                    assert session._completion_event.wait(10)
                return chunk
        monkeypatch.setattr(execution, 'open', lambda *args, **kwargs: EmptyReadBoundary(builtins.open(*args, **kwargs)), raising=False)
        inline_output = ''.join(handle.stdout)
        assert handle.poll() == session.exit_code == 7
        assert session.output_buffer == 'prefix\ntail\n'
        assert inline_output == session.output_buffer
    finally:
        release.touch()
        registry.kill_process(session.id)
        handle.stdout.close()
        env.cleanup()
