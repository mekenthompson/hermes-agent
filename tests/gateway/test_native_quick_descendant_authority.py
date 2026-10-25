"""Returned native quick shells retain evidence of children with redirected streams."""

import asyncio
import json
import shlex
import subprocess
import sys
from types import SimpleNamespace

import psutil
import pytest

from tests.gateway.fixtures.local_recovery_probe import daemon
from tests.gateway.test_cli_plugin_command_in_gateway import _plugin_home, _Wire
from tests.waiting import async_wait_until, wait_until


def _alive(pid):
    try:
        return bool(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _pid(path):
    text = path.read_text().strip() if path.exists() else ''
    return int(text) if text else None


def _child(tmp_path, name):
    root = tmp_path / name
    root.mkdir()
    script, pid, release, effect = (root / item for item in ('child.py', 'pid', 'release', 'effect'))
    script.write_text('''import os, sys, time
from pathlib import Path
pid, release, effect = map(Path, sys.argv[1:])
pid.write_text(str(os.getpid()))
while not release.exists():
    time.sleep(.01)
effect.write_text('child completed')
''')
    command = shlex.join([sys.executable, str(script), str(pid), str(release), str(effect)])
    command += ' </dev/null >/dev/null 2>&1 & printf launched'
    return SimpleNamespace(pid=pid, release=release, effect=effect, command=command)


def _configure(home, commands):
    path = home / 'config.yaml'
    config = json.loads(path.read_text())
    config['quick_commands'] = {key: {'type': 'exec', 'command': command} for key, command in commands.items()}
    path.write_text(json.dumps(config))


def _launch(wire, tree, name):
    created = wire.call('session.create', request_id=name, source='cli', toolsets=[], cwd=str(tree.pid.parent))
    sid = created['session_id']
    receipt = wire.call('slash.exec', session_id=sid, command='/' + name)
    assert receipt['output'].splitlines()[0] == 'launched', receipt
    diagnostic = receipt['output'].lower()
    assert 'unconfirmed' in diagnostic and 'reconcile' in diagnostic and 'proc_' in diagnostic, receipt
    pid = wait_until(lambda: _pid(tree.pid), timeout=10, what='actual redirected quick child')
    assert _alive(pid) and not tree.effect.exists()
    return sid, pid


def _stop(wire, sid, home):
    snapshot = wire.call('session.resume', session_id=sid)
    try:
        return wire.call('session.interrupt', session_id=sid,
                         execution_generation=snapshot['execution_generation']), False
    except Exception as exc:
        assert 'unknown_execution' in str(exc), (str(exc), (home / 'restart.log').read_text())
        records = json.loads((home / 'processes.json').read_text())
        assert any(row.get('reconciliation_required') for row in records), records
        return None, True


def _cold_registry(root, home, env):
    """An independent interpreter rewrites and reads actual retained native metadata."""
    program = '''import json
from tools.process_registry import ProcessRegistry
foreign = ProcessRegistry()
foreign._write_checkpoint()
cold = ProcessRegistry()
count = cold.recover_from_checkpoint()
records = []
for sid, session in list(cold._running.items()):
    records.append(dict(id=sid, pid=session.pid, birth=session.host_start_time,
        note=session.reconciliation_required, poll=cold.poll(sid)['status'], kill=cold.kill_process(sid)['status']))
cold._write_checkpoint()
print(json.dumps(dict(count=count, records=records)))
'''
    result = subprocess.run([sys.executable, '-c', program], cwd=root, env=env,
                            capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def _release(tree):
    tree.release.touch()
    pid = _pid(tree.pid)
    if pid is not None:
        wait_until(lambda: not _alive(pid), timeout=10, what='naturally released test child exit')


@pytest.mark.platforms('linux')
@pytest.mark.parametrize('stopped', [False, True])
def test_native_wire_quick_parent_return_keeps_child_resource_until_reconciled(tmp_path, stopped):
    tree = _child(tmp_path, 'child')
    with _plugin_home(tmp_path, 'def register(ctx):\n    pass\n') as (root, home, env):
        _configure(home, {'launch-child': tree.command})
        with daemon(root, home, env, barrier=False) as (_proc, desc):
            wire = _Wire(home, desc)
            try:
                sid, pid = _launch(wire, tree, 'launch-child')
                records = json.loads((home / 'processes.json').read_text())
                assert records and any(row.get('reconciliation_required') for row in records), records
                assert all(row.get('owner_task_id') and row.get('session_key') for row in records)
                cold = _cold_registry(root, home, env)
                assert cold['count'] and all(row['note'] and row['poll'] == 'unknown'
                                             and row['kill'] in {'unknown', 'error'} for row in cold['records']), cold
                assert _alive(pid), 'cold inspection cannot grant ownership of an unverified orphan'
                unknown = False
                if stopped:
                    _, unknown = _stop(wire, sid, home)
                    assert unknown or not _alive(pid), 'Stop cannot acknowledge an ongoing descendant as settled'
                tree.release.touch()
                if _alive(pid):
                    wait_until(tree.effect.exists, timeout=10, what='actual redirected descendant completion')
                if not stopped:
                    assert tree.effect.read_text() == 'child completed'
                elif not unknown:
                    assert not tree.effect.exists()
            finally:
                _release(tree)
                wire.close()


@pytest.mark.platforms('linux')
def test_unknown_quick_descendant_does_not_stop_other_conversation_or_its_new_command(tmp_path):
    own, other = _child(tmp_path, 'own'), _child(tmp_path, 'other')
    fresh = tmp_path / 'fresh-command-effect'
    with _plugin_home(tmp_path, 'def register(ctx):\n    pass\n') as (root, home, env):
        _configure(home, {'launch-own': own.command, 'launch-other': other.command,
                          'fresh-work': 'printf fresh > ' + shlex.quote(str(fresh))})
        with daemon(root, home, env, barrier=False) as (_proc, desc):
            wire, neighbor = _Wire(home, desc), _Wire(home, desc)
            try:
                sid, pid = _launch(wire, own, 'launch-own')
                other_sid, other_pid = _launch(neighbor, other, 'launch-other')
                _, unknown = _stop(wire, sid, home)
                assert unknown or not _alive(pid)
                assert _alive(other_pid) and not other.effect.exists()
                receipt = neighbor.call('slash.exec', session_id=other_sid, command='/fresh-work')
                assert receipt['type'] == 'exec' and fresh.read_text() == 'fresh', receipt
                assert _alive(other_pid), 'a distinct authorized command must preserve the existing child'
                other.release.touch()
                wait_until(other.effect.exists, timeout=10, what='unrelated child completion')
                assert other.effect.read_text() == 'child completed'
            finally:
                _release(own)
                _release(other)
                wire.close()
                neighbor.close()


@pytest.mark.platforms('linux')
@pytest.mark.parametrize('birth', ['missing', 'stale'])
def test_cold_quick_descendant_unknown_birth_never_becomes_a_pid_kill_grant(tmp_path, birth):
    tree = _child(tmp_path, 'identity')
    with _plugin_home(tmp_path, 'def register(ctx):\n    pass\n') as (root, home, env):
        _configure(home, {'launch-identity': tree.command})
        with daemon(root, home, env, barrier=False) as (_proc, desc):
            wire = _Wire(home, desc)
            try:
                sid, pid = _launch(wire, tree, 'launch-identity')
                _, unknown = _stop(wire, sid, home)
                assert unknown and _alive(pid)
                path = home / 'processes.json'
                records = json.loads(path.read_text())
                assert records and all(row.get('reconciliation_required') for row in records)
                # Only corrupt the birth baseline in a negative checkpoint fixture;
                # producer-derived ownership/session/command fields stay untouched.
                for row in records:
                    row['host_start_time'] = None if birth == 'missing' else (row.get('host_start_time') or 0) + 1
                expected = {row['session_id']: row['host_start_time'] for row in records}
                path.write_text(json.dumps(records))
                recovered = _cold_registry(root, home, env)
                assert recovered['count'] == len(records), recovered
                assert all(row['birth'] == expected[row['id']] and row['note']
                           and row['poll'] == 'unknown' and row['kill'] in {'unknown', 'error'}
                           for row in recovered['records']), recovered
                assert _alive(pid) and not tree.effect.exists()
                rewritten = json.loads(path.read_text())
                assert {row['session_id']: row['host_start_time'] for row in rewritten} == expected
            finally:
                _release(tree)
                wire.close()


@pytest.mark.asyncio
@pytest.mark.platforms('linux')
async def test_native_stop_escalates_verified_term_ignoring_parent_and_child(tmp_path, monkeypatch):
    from tests.gateway.test_native_terminal_stop_authority import _cleanup, _runtime, _start, _tree

    tree = _tree(tmp_path, 'term-ignoring')
    parent_script, child_script = tmp_path / 'parent.py', tmp_path / 'child.py'
    child_script.write_text('''import os, signal, sys, time
from pathlib import Path
pid, release, effect = map(Path, sys.argv[1:])
signal.signal(signal.SIGTERM, signal.SIG_IGN)
pid.write_text(str(os.getpid()))
while not release.exists():
    time.sleep(.01)
effect.write_text('child survived Stop')
''')
    parent_script.write_text('''import os, signal, subprocess, sys, time
from pathlib import Path
child_script, parent_pid, child_pid, ready, release, effect = sys.argv[1:]
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path(parent_pid).write_text(str(os.getpid()))
child = subprocess.Popen([sys.executable, child_script, child_pid, release, effect])
while not Path(child_pid).exists():
    time.sleep(.01)
Path(ready).touch()
while not Path(release).exists():
    time.sleep(.01)
child.wait()
''')
    tree.command = 'trap "" TERM; exec ' + shlex.join([sys.executable, str(parent_script), str(child_script),
        str(tree.shell), str(tree.child), str(tree.ready), str(tree.release), str(tree.effect)])
    runtime = await _runtime(tmp_path, monkeypatch)
    try:
        ref, process = await _start(runtime, tree, notify=True)
        parent, child = int(tree.shell.read_text()), int(tree.child.read_text())
        assert process.process.pid == parent and _alive(parent) and _alive(child)
        assert process.host_start_time is not None
        await runtime.authority.interrupt(runtime.actor, ref, runtime.authority._handle(ref).execution_generation)
        assert process.exited and process.process.poll() is not None
        assert not _alive(parent) and not _alive(child), 'escalation must settle both verified actual processes'
        tree.release.touch()
        assert not tree.effect.exists()
    finally:
        await _cleanup(runtime, (tree,))
        if tree.child.exists():
            await async_wait_until(lambda: not _alive(_pid(tree.child)), timeout=10, what='test child physical settlement')


@pytest.mark.platforms('linux')
def test_postfork_quick_checkpoint_failure_reports_actual_memory_resource_and_unknown_stop(tmp_path):
    tree = _child(tmp_path, 'checkpoint-denied')
    marker = 'quick-descendant-checkpoint-denied'
    checkpoint = tmp_path / 'state' / 'processes.json'
    plugin = f'''import json

def register(ctx):
    import utils
    from tools import process_registry
    actual_write = utils.atomic_json_write
    def deny_process_checkpoint(path, *args, **kwargs):
        if str(path) == {str(checkpoint)!r}:
            raise PermissionError('test descendant checkpoint denied')
        return actual_write(path, *args, **kwargs)
    utils.atomic_json_write = deny_process_checkpoint
    def inspect(arg):
        return json.dumps([dict(id=p.id, pid=p.pid, birth=p.host_start_time,
            owner=p.owner_task_id, route=p.session_key, note=p.reconciliation_required,
            exited=p.exited) for p in process_registry.process_registry._running.values()
            if {marker!r} in p.command])
    ctx.register_command('inspect-quick-resource', inspect)
'''
    with _plugin_home(tmp_path, plugin) as (root, home, env):
        _configure(home, {'launch-denied': tree.command + ' # ' + marker})
        with daemon(root, home, env, barrier=False) as (_proc, desc):
            wire = _Wire(home, desc)
            try:
                sid = wire.call('session.create', request_id='launch-denied', source='cli',
                                toolsets=[], cwd=str(tree.pid.parent))['session_id']
                receipt = wire.call('slash.exec', session_id=sid, command='/launch-denied')
                output = receipt['output'].lower()
                assert 'launched' in output and 'checkpoint' in output and 'failed' in output, receipt
                assert 'in-memory' in output and 'original pid' in output and 'birth' in output, receipt
                assert 'reconcile' in output and 'retry' in output, receipt
                pid = wait_until(lambda: _pid(tree.pid), timeout=10, what='actual child after checkpoint denial')
                assert _alive(pid) and not tree.effect.exists() and not checkpoint.exists()
                inspected = wire.call('slash.exec', session_id=sid, command='/inspect-quick-resource')
                resource, = json.loads(inspected['output'])
                assert resource['id'] in receipt['output'] and resource['pid']
                assert resource['owner'] and resource['route'] and resource['note'] and not resource['exited']
                snapshot = wire.call('session.resume', session_id=sid)
                with pytest.raises(Exception, match='unknown_execution'):
                    wire.call('session.interrupt', session_id=sid,
                              execution_generation=snapshot['execution_generation'])
                assert _alive(pid) and not tree.effect.exists() and not checkpoint.exists()
            finally:
                _release(tree)
                wire.close()
