"""Native task Stop reaches its admitted executor and actual hosted process scope."""
import asyncio
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shlex
import sys
import threading

import pytest

from tests.gateway.fixtures.local_recovery_probe import child_env, daemon, rpc, websocket
from tests.gateway.test_managed_process_lifecycle import ROOT, _alive, _body, _settled
from tests.waiting import wait_until


class TaskModel(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        tools = [message for message in body.get('messages', []) if message['role'] == 'tool']
        self.server.results.extend(tools)
        successor = any(call['id'] == 'successor-task' for message in body.get('messages', [])
                        for call in message.get('tool_calls', []))
        call_id = 'native-task-' + str(len(tools))
        if not tools:
            command = self.server.command
            if self.server.allow_successor.is_set():
                command, call_id = self.server.successor_command, 'successor-task'
        elif len(tools) == 1 and self.server.worker_terminal:
            self.server.waiting.set()
            command = 'worker-terminal'
        elif len(tools) == 1 and not self.server.idle and not successor:
            self.server.waiting.set()
            self.server.release.wait(50)
            command = self.server.followup
        else:
            self.server.waiting.set()
            command = None
        name, arguments = 'terminal', {'command': command,
            'background': True, 'persist_on_release': self.server.persist}
        if command == 'worker-terminal':
            name, arguments = self.server.worker_terminal
        message = ({'role': 'assistant', 'content': None, 'tool_calls': [{
            'id': call_id, 'index': 0, 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps(arguments)}}]}
            if command else {'role': 'assistant', 'content': 'NATIVE_TASK_END'})
        frame = {'id': 'native-task', 'choices': [{'index': 0, 'message': message,
            'finish_reason': 'tool_calls' if command else 'stop'}],
            'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}}
        if body.get('stream'):
            frame['choices'][0]['delta'] = frame['choices'][0].pop('message')
            payload = ('data: ' + json.dumps(frame) + '\n\ndata: [DONE]\n\n').encode()
        else:
            payload = json.dumps(frame).encode()
        try:
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream' if body.get('stream') else 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass


def _owner_launcher(path):
    path.write_text('''from pathlib import Path
from tools.process_registry_custody import LocalCustody
original_stop = LocalCustody.stop
def denied(self):
    if (Path(__file__).parent / 'cleanup.allowed').exists():
        return original_stop(self)
    raise PermissionError('Test OS custody control denied')
LocalCustody.stop = denied
from gateway.run import main
main()
''')


def _native_owner(conn, task_id, run_id):
    row = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND run_id=? AND kind='owner_admitted'",
                       (task_id, run_id)).fetchone()
    assert row is not None
    return json.loads(row[0])


def _ordinary_reap(conn, dispatch, run_id, monkeypatch):
    observed, original = [], dispatch._reap_terminal_worker_row
    def reap(*args):
        observed.append(args[1]['id'])
        return original(*args)
    with monkeypatch.context() as patch:
        patch.setattr(dispatch, 'TERMINAL_WORKER_REAP_GRACE_SECONDS', 0)
        patch.setattr(dispatch, '_reap_terminal_worker_row', reap)
        dispatch.reap_terminal_workers(conn)
    assert run_id in observed, 'The actual original native run must reach the ordinary reaper'


@pytest.mark.platforms('linux')
@pytest.mark.parametrize('mode,persist', [('live', False), ('active', False), ('active', True),
                                       ('idle', True), ('unknown', False), ('reclaim', False),
                                       ('self-block', True), ('reclaim-unknown', True), ('successor', True),
                                       ('self-supervisor', True), ('repeat-stop', True),
                                       ('complete-supervisor', True), ('complete-supervisor', False)])
def test_native_task_block_preserves_real_execution_authority_and_stop_truth(tmp_path, monkeypatch, mode, persist):
    from hermes_cli import kanban_db as kb, kanban_db_dispatch as dispatch
    from hermes_cli.kanban_db_connect import connect

    home, user, workspace = tmp_path / 'state', tmp_path / 'user', tmp_path / 'workspace'
    home.mkdir(mode=0o700)
    user.mkdir()
    workspace.mkdir()
    (user / '.hushlogin').touch()
    for key in ('HERMES_KANBAN_DB', 'HERMES_KANBAN_TASK', 'HERMES_KANBAN_RUN_ID',
                'HERMES_KANBAN_CLAIM_LOCK', 'HERMES_DELEGATED_CHILD'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(home))
    monkeypatch.setenv('HOME', str(user))
    monkeypatch.setenv('USERPROFILE', str(user))
    tree, command = _body(tmp_path, 'body')
    successor_tree, successor_command = _body(tmp_path, 'successor')
    next_effect = tmp_path / 'next-effect'
    peer = ThreadingHTTPServer(('127.0.0.1', 0), TaskModel)
    peer.command, peer.followup = command, 'printf next > ' + shlex.quote(str(next_effect))
    peer.persist, peer.idle = persist, mode == 'idle'
    peer.worker_terminal = {
        'self-supervisor': ('kanban_block', {'reason': 'Awaiting a supervisor decision', 'kind': 'needs_input'}),
        'complete-supervisor': ('kanban_complete', {'summary':
            'Native parent work is complete; the separately managed background service remains available.'}),
    }.get(mode)
    peer.allow_successor, peer.successor_command = threading.Event(), (
        successor_command if mode == 'successor' else None)
    peer.waiting, peer.release = threading.Event(), threading.Event()
    peer.results = []
    threading.Thread(target=peer.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{peer.server_port}/v1'
    monkeypatch.setenv('OPENAI_API_KEY', 'loopback-only')
    monkeypatch.setenv('OPENAI_BASE_URL', url)
    (home / 'config.yaml').write_text(json.dumps({
        'gateway': {'multiplex_profiles': False, 'managed_workers': True},
        'model': {'provider': 'custom', 'default': 'fixture-model', 'base_url': url},
        'auxiliary': {'title_generation': {'enabled': False}}, 'approvals': {'mode': 'off'},
        'platform_toolsets': {'cli': ['terminal']}, 'terminal': {'backend': 'local', 'daemon_term_grace_seconds': .1},
        'agent': {'api_max_retries': 1}, 'kanban': {'dispatch_in_gateway': False}}))
    monkeypatch.setattr(dispatch, '_resolve_hermes_argv', lambda: [sys.executable, '-m', 'hermes_cli.main'])
    kb.create_board('owned')
    with closing(connect(board='owned')) as conn:
        task_id = kb.create_task(conn, title='Native task process scope', body='Start the configured body and follow-up',
            assignee='default', workspace_kind='dir', workspace_path=str(workspace))
    env = child_env()
    env.update(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(home), HERMES_KANBAN_HOME=str(home),
               OPENAI_API_KEY='loopback-only', OPENAI_BASE_URL=url, PYTHONPATH=str(ROOT))
    launcher = tmp_path / 'native_task_owner.py'
    _owner_launcher(launcher)
    try:
        with daemon(ROOT, home, env, barrier=mode in {'unknown', 'repeat-stop'}, fixture=str(launcher)) as (_owner, desc):
            with closing(connect(board='owned')) as conn:
                spawned = dispatch.dispatch_once(conn, board='owned', max_spawn=1)
                assert [item[0] for item in spawned.spawned] == [task_id], spawned
                assert peer.waiting.wait(35), (home / ('first.log' if mode in {'unknown', 'repeat-stop'} else 'restart.log')).read_text()
                try:
                    wait_until((tree / 'ready').exists, timeout=10, what='real native task kernel body')
                except AssertionError as exc:
                    raise AssertionError({'tools': peer.results, 'log': (home / 'restart.log').read_text()}) from exc
                pid = int((tree / 'pid').read_text())
                if mode in {'self-supervisor', 'complete-supervisor'}:
                    terminal_status = 'blocked' if mode == 'self-supervisor' else 'done'
                    wait_until(lambda: kb.get_task(conn, task_id).status == terminal_status, timeout=15,
                               what='actual registered native worker lifecycle tool')
                task = kb.get_task(conn, task_id)
                original_run = task.current_run_id or conn.execute(
                    'SELECT id FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1', (task_id,)).fetchone()[0]
                binding = _native_owner(conn, task_id, original_run)
                original_worker = json.loads(conn.execute(
                    "SELECT payload FROM task_events WHERE task_id=? AND run_id=? AND kind='worker_bound'",
                    (task_id, original_run)).fetchone()[0])['pid']
                assert _alive(pid) and not (tree / 'effect').exists()
                if mode == 'live':
                    (tree / 'release').touch()
                    peer.release.set()
                    wait_until(next_effect.exists, timeout=20, what='healthy original claim continuation')
                    assert (tree / 'effect').read_text() == 'finished'
                    assert next_effect.read_text() == 'next'
                    return
                if mode == 'idle':
                    wait_until(lambda: not kb._pid_alive(task.worker_pid), timeout=20,
                               what='normal native model/submitter return before Stop')
                    assert _alive(pid), 'A persisted body must outlive normal parent return'
                if mode in {'reclaim', 'reclaim-unknown'}:
                    if mode == 'reclaim-unknown':
                        from hermes_cli import kanban_owner_control
                        async def unavailable(*args):
                            raise OSError('Original native transport is unavailable')
                        monkeypatch.setattr(kanban_owner_control, '_interrupt', unavailable)
                    assert kb.reclaim_task(conn, task_id, reason='Reconcile the original native executor') is (
                        mode == 'reclaim')
                elif mode not in {'self-supervisor', 'complete-supervisor'}:
                    assert kb.block_task(conn, task_id, kind='needs_input', reason='Stop this native task now',
                        expected_run_id=original_run if mode in {'self-block', 'self-supervisor'} else None,
                        idempotency_key='original-stop' if mode in {'repeat-stop', 'successor'} else None)
                events = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND run_id=? AND kind='native_owner_stop'",
                                      (task_id, original_run)).fetchall()
                if mode in {'self-supervisor', 'complete-supervisor'}:
                    assert not events and _alive(pid), 'Normal worker return preserves its managed background scope'
                    assert kb.get_task(conn, task_id).status == terminal_status
                    if mode == 'self-supervisor':
                        worker_block = json.loads(conn.execute("SELECT payload FROM task_events WHERE task_id=? "
                            "AND run_id=? AND kind='blocked'", (task_id, original_run)).fetchone()[0])
                        assert worker_block['supervisor_stop'] is False
                    submitter = conn.execute('SELECT worker_pid FROM task_runs WHERE id=?', (original_run,)).fetchone()[0]
                    wait_until(lambda: not _alive(original_worker) and not kb._pid_alive(submitter), timeout=35,
                               what='normal native worker and submitter physical return')
                    _ordinary_reap(conn, dispatch, original_run, monkeypatch)
                    assert _alive(pid), 'Ordinary bookkeeping reaping must preserve the managed background scope'
                    assert not conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND run_id=? "
                                            "AND kind='native_owner_stop'", (task_id, original_run)).fetchall()
                    assert kb.block_task(conn, task_id, kind='needs_input', reason='Explicit supervisor service Stop') is False
                    events = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND run_id=? "
                                          "AND kind='native_owner_stop'", (task_id, original_run)).fetchall()
                if mode == 'repeat-stop':
                    first = [json.loads(row[0]) for row in events]
                    assert [row['status'] for row in first] == ['pending', 'unknown'] and _alive(pid), first
                    checkpoint, = json.loads((home / 'processes.json').read_text())
                    assert checkpoint['reconciliation_required'] and checkpoint['parent_session_id'] == binding['session_id']
                    peer.release.set()
                    wait_until(lambda: not _alive(original_worker), timeout=35,
                               what='original stopped model returns before physical cleanup retry')
                    assert _alive(pid) and not next_effect.exists()
                    (tmp_path / 'cleanup.allowed').touch()
                    assert kb.block_task(conn, task_id, kind='needs_input', reason='Stop this native task now',
                                         idempotency_key='original-stop')
                    events = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND run_id=? "
                                          "AND kind='native_owner_stop'", (task_id, original_run)).fetchall()
                    assert [json.loads(row[0])['status'] for row in events] == ['pending', 'unknown', 'pending', 'confirmed']
                    assert json.loads(events[-1][0])['generation'] == first[-1]['generation']
                    assert json.loads(events[-1][0])['admission_id'] == first[-1]['admission_id']
                    events = events[-2:]
                if mode in {'self-block', 'reclaim-unknown'}:
                    if mode == 'self-block':
                        assert not events, 'A worker self-block must not manufacture a supervisor Stop'
                    else:
                        assert [json.loads(row[0])['status'] for row in events] == ['pending', 'unknown']
                        retained = kb.get_task(conn, task_id)
                        assert retained.status == 'running' and retained.current_run_id == original_run
                    assert _alive(pid), 'A self-block is distinct from explicit service termination'
                    peer.release.set()
                    wait_until(lambda: _settled(home, binding['request_id']), timeout=20,
                               what='old native claim rejects its delayed second tool')
                    assert not next_effect.exists(), 'Ended original claim borrowed session authority'
                    (tree / 'release').touch()
                    wait_until(lambda: not _alive(pid), timeout=15, what='naturally released persisted body')
                    return
                assert [json.loads(row[0])['status'] for row in events] == [
                    'pending', 'unknown' if mode == 'unknown' else 'confirmed'], events
                stopped = json.loads(events[-1][0])
                assert kb.get_task(conn, task_id).status == (
                    'done' if mode == 'complete-supervisor' else 'ready' if mode == 'reclaim' else 'blocked')
                if mode == 'unknown':
                    assert stopped['status'] == 'unknown' and _alive(pid), stopped
                    checkpoint, = json.loads((home / 'processes.json').read_text())
                    assert checkpoint['reconciliation_required'] and checkpoint['parent_session_id'] == binding['session_id']
                else:
                    assert stopped['status'] == 'confirmed' and not _alive(pid), stopped
                if mode == 'successor':
                    assert stopped['confirmation'] == 'effect_scope_cleaned'
                    assert stopped['execution_state'] in {'running', 'waiting', 'idle', 'terminal'}
                    assert kb.unblock_task(conn, task_id)
                    peer.allow_successor.set()
                    replacement = dispatch.dispatch_once(conn, board='owned', max_spawn=1)
                    assert [item[0] for item in replacement.spawned] == [task_id], replacement
                    wait_until((successor_tree / 'ready').exists, timeout=25, what='authorized successor body')
                    successor_pid = int((successor_tree / 'pid').read_text())
                    successor_run = kb.get_task(conn, task_id).current_run_id
                    assert successor_run != original_run and _alive(successor_pid)
                    assert _native_owner(conn, task_id, successor_run)['session_id'] != binding['session_id']
                    peer.release.set()
                    wait_until(lambda: not _alive(original_worker), timeout=35, what='old model physically returns')
                    current = kb.get_task(conn, task_id)
                    assert current.status == 'running' and current.current_run_id == successor_run
                    assert _alive(successor_pid) and not next_effect.exists()
                    assert kb.block_task(conn, task_id, kind='needs_input', reason='Stop this native task now',
                                         idempotency_key='original-stop')
                    current = kb.get_task(conn, task_id)
                    assert current.status == 'running' and current.current_run_id == successor_run
                    assert _alive(successor_pid), 'A delayed original Stop replay controlled the successor'
                    assert not (tree / 'effect').exists() and not (successor_tree / 'effect').exists()
                    assert kb.block_task(conn, task_id, kind='needs_input', reason='Stop the successor itself')
                    assert not _alive(successor_pid)
                    return
                peer.release.set()  # A delayed second tool may never regain the stopped run's authority.
                (tree / 'release').touch()
                wait_until(lambda: not _alive(pid), timeout=15, what='actual stopped or naturally reconciled body')
                if mode != 'unknown':
                    assert not (tree / 'effect').exists()

                async def inspect():
                    async with websocket(home, desc) as ws:
                        snapshot = await rpc(ws, 'session.resume', session_id=binding['session_id'])
                        assert 'result' in snapshot, snapshot
                        listed = await rpc(ws, 'process.list', session_id=binding['session_id'])
                        assert 'result' in listed, listed
                        return snapshot
                asyncio.run(inspect())
                assert not next_effect.exists(), 'The stopped original run admitted a new background effect'
                assert len((tree / 'starts').read_text().splitlines()) == 1
    finally:
        peer.release.set()
        (tree / 'release').touch()
        (successor_tree / 'release').touch()
        peer.shutdown()
        peer.server_close()
