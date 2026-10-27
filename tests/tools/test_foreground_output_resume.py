"""Foreground follow-up refusal preserves the original stream, including split UTF-8."""
import shlex
import sys
import threading

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tests.waiting import wait_until
from tools import process_registry as module
from tools.environments import base, base_output
from tools.environments.local import LocalEnvironment
from tools.interrupt import consume_yield, request_yield
from tools.process_registry_job import job_quiescent
from tools.terminal_tool_background import yield_to_background_handler

pytestmark = pytest.mark.platforms('linux')


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    token = set_hermes_home_override(tmp_path)
    registry = module.ProcessRegistry()
    monkeypatch.setattr(module, 'process_registry', registry)
    try:
        yield registry
    finally:
        for session in list(registry._running.values()):
            registry.kill_process(session.id, source='session.interrupt')
        reset_hermes_home_override(token)


def handler(command, root, route='route'):
    return yield_to_background_handler(command=command, env_type='local', cwd=str(root),
                                      effective_task_id='task', task_id='task', session_key=route)


@pytest.mark.parametrize('completed', [True, False], ids=['completed-job', 'foreign-route-refused'])
def test_refused_yield_preserves_cursor_and_split_character(tmp_path, monkeypatch, registry, completed):
    gate = tmp_path / 'gate'
    payload = f'''import os,time
from pathlib import Path
os.write(1,b'unique_prefix\\n\\xe6')
end=time.monotonic()+12
while not Path({str(gate)!r}).exists() and time.monotonic()<end: time.sleep(.01)
os.write(1,b'\\x97\\xa5-unique_tail\\n')
raise SystemExit(7)
'''
    command = shlex.join([sys.executable, '-c', payload])
    actual = handler(command, tmp_path)
    refused = actual if completed else handler(command, tmp_path, route='foreign')
    entered, release = threading.Event(), threading.Event()
    def held(proc, output):
        entered.set()
        assert release.wait(5)
        return refused(proc, output)
    held.prepare_local = actual.prepare_local
    collectors, create = [], base._new_output_collector
    def capture(*args, **kwargs):
        output = create(*args, **kwargs)
        collectors.append(output)
        return output
    monkeypatch.setattr(base, '_new_output_collector', capture)
    env, result = LocalEnvironment(cwd=str(tmp_path)), {}
    worker = threading.Thread(target=lambda: result.update(env.execute(command, timeout=15, yield_handler=held)))
    worker.start()
    try:
        wait_until(lambda: any('unique_prefix' in c.render() for c in collectors), timeout=5)
        request_yield(worker.ident)
        assert entered.wait(5)
        session = next(s for s in registry._running.values() if s.command == command)
        if completed:
            gate.touch()
            assert session._completion_event.wait(5)
        release.set()
        if not completed:
            gate.touch()
        worker.join(10)
        assert not worker.is_alive()
        assert result['output'] == 'unique_prefix\n日-unique_tail\n' and result['returncode'] == 7
        assert 'yielded_session_id' not in result
        assert session._completion_event.wait(5)
        assert (session.owner_task_id, session.session_key) == ('task', 'route')
        assert session.persist_on_release and not session.notify_on_complete
        # The native result also retains the shell's cwd observation trailer;
        # LocalEnvironment removes that trailer only from its inline response.
        assert session.output_buffer.startswith(result['output']) and session.exit_code == 7
        assert session.output_buffer.count('unique_prefix') == session.output_buffer.count('日-unique_tail') == 1
        assert job_quiescent(session) and session._result_complete
        assert registry.poll(session.id)['exit_code'] == 7
        assert registry.completion_queue.empty()
    finally:
        release.set()
        gate.touch()
        consume_yield(worker.ident)
        worker.join(15)
        env.cleanup()


def test_stop_during_iterator_read_keeps_the_fetched_piece(tmp_path, monkeypatch, registry):
    command = shlex.join([sys.executable, '-c', "print('read-boundary-日')"])
    env = LocalEnvironment(cwd=str(tmp_path))
    proc = env._run_bash(command, native_session=handler(command, tmp_path).prepare_local())
    session, stop = proc._hermes_job_session, threading.Event()
    output = base_output._new_output_collector(proc, False)
    iterate = type(proc.stdout).__iter__
    def stop_after_fetch(stream):
        for piece in iterate(stream):
            if piece:
                stop.set()
            yield piece
    monkeypatch.setattr(type(proc.stdout), '__iter__', stop_after_fetch)
    try:
        assert session._completion_event.wait(5)
        base_output._drain_stdout(proc, output, stop)
        assert output.render() == 'read-boundary-日\n'
        stop.clear()
        base_output._drain_stdout(proc, output, stop)
        assert output.render() == session.output_buffer == 'read-boundary-日\n'
        assert proc.poll() == session.exit_code == 0 and job_quiescent(session)
    finally:
        proc.stdout.close()
        env.cleanup()
