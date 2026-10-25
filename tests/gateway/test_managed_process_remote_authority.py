"""Managed remote background effects retain their original native request and uncertainty."""
import asyncio
import json

import pytest

from tests.gateway.fixtures.local_recovery_probe import daemon, rpc, websocket
from tests.gateway.test_managed_process_lifecycle import ROOT, _alive, _body, _create, _fixture, _status, _submit
from tests.waiting import async_wait_until


def _remote_boundary(launcher, home, case):
    (home / 'remote').mkdir()
    boundary = '''
from tests.fakes.loopback_remote_process import LoopbackRemoteEnvironment
from tools import terminal_tool
from tools.process_registry import process_registry
CASE = __CASE__
actual_spawn = terminal_tool.spawn_background_process
actual_write = process_registry._write_checkpoint
process_registry._env_temp_dir = lambda env: str(HOME / 'remote')
held = False

def write(*args, **kwargs):
    global held
    pending = [session for session in process_registry._running.values()
        if session.pid_scope == 'sandbox' and session.pid is None and session.reconciliation_required]
    if pending and CASE == 'failed':
        return False
    result = actual_write(*args, **kwargs)
    if pending and CASE == 'stop' and not held:
        held = True
        (HOME / 'checkpoint.entered').touch()
        from tests.waiting import wait_until
        wait_until((HOME / 'checkpoint.release').exists, timeout=20, what='test remote writer release')
    return result

def spawn(**kwargs):
    remote = LoopbackRemoteEnvironment(kwargs['env'])
    actual_execute = remote.execute
    def execute(command, *args, **options):
        if 'nohup bash -lc' in command:
            record('launches.jsonl', {'command': command})
        result = actual_execute(command, *args, **options)
        if CASE == 'lost' and 'nohup bash -lc' in command:
            raise OSError('Test remote response lost after actual launch')
        return result
    remote.execute = execute
    return actual_spawn(**{**kwargs, 'env': remote, 'env_type': 'fixture-remote'})

terminal_tool.spawn_background_process = spawn
process_registry._write_checkpoint = write
'''.replace('__CASE__', repr(case))
    launcher.write_text(launcher.read_text().replace('from gateway.run import main\nmain()',
        boundary + '\nfrom gateway.run import main\nmain()'))


@pytest.mark.platforms('linux')
@pytest.mark.parametrize('case', ['valid', 'lost', 'failed', 'stop'])
def test_managed_remote_effect_never_becomes_a_false_local_no_start(tmp_path, case):
    directory, command = _body(tmp_path, 'remote-body')
    with _fixture(tmp_path) as (home, peer, url, env, launcher):
        _remote_boundary(launcher, home, case)
        peer.commands['REMOTE'] = {'command': command, 'background': True}

        async def exercise(desc):
            async with websocket(home, desc) as ws:
                sid = await _create(ws, home, url, 'remote')
                if case == 'stop':
                    assert 'result' in await rpc(ws, 'prompt.submit', session_id=sid, input_id='remote-start', text='REMOTE')
                    await async_wait_until((home / 'checkpoint.entered').exists, timeout=20, what='actual remote pending checkpoint')
                    assert not (home / 'launches.jsonl').exists() and not (directory / 'starts').exists()
                    snapshot = (await rpc(ws, 'session.resume', session_id=sid))['result']
                    stopped = await rpc(ws, 'session.interrupt', session_id=sid,
                                        execution_generation=snapshot['execution_generation'])
                    assert 'result' in stopped or stopped.get('error', {}).get('message') == 'unknown_execution', stopped
                    (home / 'checkpoint.release').touch()
                    await async_wait_until(lambda: _status(home, 'remote-start') in {'terminal', 'unknown'}, timeout=35,
                                           what='revoked remote parent settlement')
                    assert not (home / 'launches.jsonl').exists() and not (directory / 'starts').exists()
                    assert not list((home / 'remote').iterdir()) and not (directory / 'effect').exists()
                    return
                snapshot = await _submit(ws, home, sid, 'REMOTE', 'remote-start')
                tools = [json.loads(message['content']) for message in snapshot['messages'] if message['role'] == 'tool']
                assert tools, (snapshot, (home / 'first.log').read_text())
                if case == 'failed':
                    assert tools[-1].get('error') and 'checkpoint' in tools[-1]['error'].lower(), tools
                    assert not (home / 'launches.jsonl').exists() and not (directory / 'starts').exists()
                    assert not list((home / 'remote').iterdir())
                    assert json.loads((home / 'processes.json').read_text()) == []
                    return
                await async_wait_until((directory / 'ready').exists, timeout=10, what='actual remote shell body')
                pid = int((directory / 'pid').read_text())
                assert _alive(pid) and not (directory / 'effect').exists()
                listed = await rpc(ws, 'process.list', session_id=sid)
                processes = listed.get('result', {}).get('processes')
                assert processes and len(processes) == 1, listed
                process, = processes
                assert process['status'] in {'running', 'unknown'} and process.get('exit_code') is None, process
                assert process.get('completion_reason') != 'failed_start', process
                checkpoint, = json.loads((home / 'processes.json').read_text())
                assert checkpoint['pid_scope'] == 'sandbox' and checkpoint['session_id'] == process['session_id']
                if case == 'lost':
                    assert process['status'] == 'unknown' and checkpoint['reconciliation_required'], (process, checkpoint)
                    assert checkpoint['pid'] is None, 'A lost remote receipt cannot fabricate a host or sandbox PID'
                (directory / 'release').touch()
                await async_wait_until((directory / 'effect').exists, timeout=10, what='original remote body completion')
                if case == 'valid':
                    snapshot = await _submit(ws, home, sid, 'WAIT ' + process['session_id'], 'remote-wait')
                    waited = json.loads([message['content'] for message in snapshot['messages'] if message['role'] == 'tool'][-1])
                    assert waited['status'] == 'exited' and waited['exit_code'] == 7, waited
                    assert waited['output'] == 'FIRST_42\nSECOND_42\n', waited
                else:
                    listed = await rpc(ws, 'process.list', session_id=sid)
                    assert listed['result']['processes'][0]['status'] == 'unknown', listed
                assert len((directory / 'starts').read_text().splitlines()) == 1
                assert len((home / 'launches.jsonl').read_text().splitlines()) == 1

        try:
            with daemon(ROOT, home, env, barrier=True, fixture=str(launcher)) as (_owner, desc):
                asyncio.run(exercise(desc))
        finally:
            (directory / 'release').touch()
            (home / 'checkpoint.release').touch()


@pytest.mark.platforms('linux')
def test_cold_managed_remote_resource_remains_unknown_visible_and_never_replayed(tmp_path):
    directory, command = _body(tmp_path, 'remote-body')
    with _fixture(tmp_path) as (home, peer, url, env, launcher):
        _remote_boundary(launcher, home, 'valid')
        peer.commands['REMOTE'] = {'command': command, 'background': True}
        saved = {}

        async def launch(desc):
            async with websocket(home, desc) as ws:
                saved['sid'] = await _create(ws, home, url, 'remote')
                snapshot = await _submit(ws, home, saved['sid'], 'REMOTE', 'remote-start')
                result = json.loads([m['content'] for m in snapshot['messages'] if m['role'] == 'tool'][-1])
                saved['resource'] = result['session_id']
                await async_wait_until((directory / 'ready').exists, timeout=10, what='real remote body before owner loss')

        async def inspect(desc):
            async with websocket(home, desc) as ws:
                snapshot = await rpc(ws, 'session.resume', session_id=saved['sid'])
                assert 'result' in snapshot, snapshot
                listed = await rpc(ws, 'process.list', session_id=saved['sid'])
                rows = listed.get('result', {}).get('processes')
                assert rows and len(rows) == 1, listed
                record, = rows
                assert record['session_id'] == saved['resource'] and record['status'] == 'unknown', record
                stopped = await rpc(ws, 'session.interrupt', session_id=saved['sid'],
                                    execution_generation=snapshot['result']['execution_generation'])
                assert stopped.get('error', {}).get('message') == 'unknown_execution', stopped
                pid = int((directory / 'pid').read_text())
                assert _alive(pid) and not (directory / 'effect').exists()
                checkpoint, = json.loads((home / 'processes.json').read_text())
                assert checkpoint['session_id'] == saved['resource'] and checkpoint['pid_scope'] == 'sandbox'
                assert checkpoint['reconciliation_required'] and checkpoint.get('custody_kind', '') == ''
                assert len((directory / 'starts').read_text().splitlines()) == 1
                assert len((home / 'launches.jsonl').read_text().splitlines()) == 1
                (directory / 'release').touch()
                await async_wait_until(lambda: not _alive(pid), timeout=10, what='natural release of unadopted remote body')
                listed = await rpc(ws, 'process.list', session_id=saved['sid'])
                assert listed['result']['processes'][0]['status'] == 'unknown', listed
        try:
            with daemon(ROOT, home, env, barrier=True, fixture=str(launcher)) as (_owner, desc):
                asyncio.run(launch(desc))
            assert _alive(int((directory / 'pid').read_text()))
            with daemon(ROOT, home, env, barrier=False) as (_owner, desc):
                asyncio.run(inspect(desc))
        finally:
            (directory / 'release').touch()
