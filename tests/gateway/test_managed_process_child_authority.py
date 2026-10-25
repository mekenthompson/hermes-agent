"""A native managed delegation retains its own authority for background effects."""
import asyncio
from contextlib import closing, contextmanager
from http.server import BaseHTTPRequestHandler
import json
import shlex
import sqlite3
import subprocess
import sys

import pytest

from tests.gateway.fixtures.local_recovery_probe import daemon, rpc, websocket
from tests.gateway.test_managed_process_lifecycle import ROOT, _alive, _body, _create, _fixture, _settled
from tests.waiting import async_wait_until


class ChildProcessModel(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        messages = body.get('messages') or []
        users = [message.get('content') for message in messages if message['role'] == 'user']
        tools = [message for message in messages if message['role'] == 'tool']
        name, arguments = None, None
        parent = users and users[0] == 'DELEGATE_PROCESS'
        schema_repair = getattr(self.server, 'schema_repair', False)
        repairing = schema_repair and any(str(user).startswith('Your previous final response was rejected') for user in users)
        if repairing:
            repair_index = max(index for index, message in enumerate(messages) if message['role'] == 'user'
                and str(message.get('content')).startswith('Your previous final response was rejected'))
            tools = [message for message in messages[repair_index + 1:] if message['role'] == 'tool']
        child = 'CHILD_PROCESS_GOAL' in json.dumps(users) or repairing
        if users and users[0] in self.server.commands and not tools:
            name, arguments = 'terminal', self.server.commands[users[0]]
        elif parent and not tools and getattr(self.server, 'parent_command', None):
            name, arguments = 'terminal', {'command': self.server.parent_command, 'background': True}
        elif parent and len(tools) == int(bool(getattr(self.server, 'parent_command', None))):
            task = {'goal': 'CHILD_PROCESS_GOAL', 'toolsets': ['terminal']}
            if schema_repair:
                task['output_schema'] = {'type': 'object', 'properties': {'value': {'type': 'integer'}},
                                         'required': ['value'], 'additionalProperties': False}
            name, arguments = 'delegate_task', {'tasks': [task]} if schema_repair else task
            if getattr(self.server, 'sibling_command', None):
                arguments = {'tasks': [task, {'goal': 'CHILD_SIBLING_GOAL', 'toolsets': ['terminal']}]}
        elif 'CHILD_SIBLING_GOAL' in json.dumps(users) and not tools:
            name, arguments = 'terminal', {'command': self.server.sibling_command, 'background': True,
                                          'persist_on_release': True}
        elif child and schema_repair and not repairing:
            if not tools and getattr(self.server, 'schema_initial_command', None):
                name, arguments = 'terminal', {'command': self.server.schema_initial_command}
        elif child and not tools:
            name, arguments = 'terminal', {'command': self.server.child_command, 'background': True,
                **({'persist_on_release': True} if getattr(self.server, 'child_persist', False) else {})}
        elif child and len(tools) == 1 and getattr(self.server, 'parent_command', None):
            name, arguments = 'tool_call', {'calls': [{'name': 'process_manage', 'arguments': {'action': 'list'}}]}
        elif child and len(tools) == (
                2 if getattr(self.server, 'parent_command', None) else 1):
            result = json.loads(tools[0]['content'])
            if result.get('session_id') and getattr(self.server, 'child_mode', 'wait') != 'cleanup':
                action = 'handoff' if self.server.child_mode == 'handoff' else 'wait'
                name, arguments = 'tool_call', {'calls': [{'name': 'process_manage', 'arguments': {
                    'action': action, 'session_id': result['session_id'],
                    **({'data': 'Keep the child process alive for the parent to collect.'} if action == 'handoff'
                       else {'timeout': 60 if self.server.child_mode == 'timeout' else 30})}}]}
        message = ({'role': 'assistant', 'content': None, 'tool_calls': [{
            'id': 'native-child-process', 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps(arguments)}}]}
            if name else {'role': 'assistant', 'content': '{"value": 42}'
                          if repairing and len(tools) >= 2 else 'CHILD_PROCESS_DONE'})
        self.server.child_requests.append(body)
        choice = {'index': 0, 'message': message, 'finish_reason': 'tool_calls' if name else 'stop'}
        frame = {'id': 'native-child', 'model': 'fixture-model', 'choices': [choice],
                 'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}}
        if body.get('stream'):
            choice['delta'] = choice.pop('message')
            for index, call in enumerate(choice['delta'].get('tool_calls', [])):
                call['index'] = index
            payload = ('data: ' + json.dumps(frame) + '\n\ndata: [DONE]\n\n').encode()
            kind = 'text/event-stream'
        else:
            payload, kind = json.dumps(frame).encode(), 'application/json'
        self.send_response(200)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _rows(home, sql, *params):
    with closing(sqlite3.connect(f'file:{home / "state.db"}?mode=ro', uri=True)) as db:
        return db.execute(sql, params).fetchall()


@contextmanager
def _native_child(tmp_path, command, *, mode='wait', timeout=None, parent_command=None, repo_root=None,
                  transport_probe=None, sibling_command=None):
    with _fixture(tmp_path) as (home, peer, url, env, launcher):
        peer.RequestHandlerClass = ChildProcessModel
        peer.child_requests, peer.child_command, peer.child_mode = [], command, mode
        peer.parent_command, peer.repo_root = parent_command, repo_root
        peer.sibling_command = sibling_command
        config = json.loads((home / 'config.yaml').read_text())
        config['platform_toolsets']['cli'] = ['delegation', 'terminal']
        config['delegation'] = {'worktree_isolation': bool(repo_root),
                               **({'child_timeout_seconds': timeout} if timeout else {})}
        if transport_probe == 'callback-policy':
            config['delegation']['subagent_auto_approve'] = 'false'
        (home / 'config.yaml').write_text(json.dumps(config))
        if transport_probe:
            _authority_launcher(launcher, home, transport_probe)
        yield home, peer, url, env, launcher


def _authority_launcher(path, home, mode):
    path.write_text('''import asyncio
import copy
import json
from pathlib import Path
import gateway.session_worker as worker
import gateway.session_worker_child as child
from hermes_state_runtime import RuntimeStoreError
HOME = Path(__HOME__)
MODE = __MODE__
if MODE == 'native-stop':
    import subprocess
    import tools.process_registry as processes
    worker_script = HOME / 'native_child_stop_worker.py'
    worker_script.write_text(__WORKER_STOP_SOURCE__)
    real_popen = subprocess.Popen
    class NativeStopPopen(real_popen):
        def __init__(self, arguments, *args, **kwargs):
            if arguments[1:3] == ['-m', 'agent.managed_worker']:
                arguments = [arguments[0], str(worker_script)]
            super().__init__(arguments, *args, **kwargs)
    subprocess.Popen = NativeStopPopen
    wait, redact = processes._SESSION_ACTIONS['wait']
    def observe_wait(session_id, arguments):
        (HOME / 'wait.entered').touch()
        return wait(session_id, arguments)
    processes._SESSION_ACTIONS['wait'] = observe_wait, redact
original_child = child.worker_child_request
original_worker = worker.worker_request
def record(value):
    with (HOME / 'authority-probes.jsonl').open('a') as stream:
        stream.write(json.dumps(value) + '\\n')
if MODE == 'callback-policy':
    from tools import terminal_tool
    from tools.approval_context import current_approval_task_context
    original_terminal = terminal_tool._handle_terminal
    def terminal(arguments, **kwargs):
        callback = terminal_tool._get_approval_callback()
        origin = current_approval_task_context()
        record({'callback': callback.__name__ if callback else None, 'task': kwargs.get('task_id'),
                'ancestry': list(origin.ancestry) if origin else None})
        return original_terminal(arguments, **kwargs)
    terminal_tool._handle_terminal = terminal
async def child_request(connection, ref, params):
    if params['operation'] == 'child.bind':
        if MODE == 'before-bind-stop':
            cancel = dict(params, operation='child.cancel', payload={})
            record({'cancel': await original_child(connection, ref, cancel)})
        if MODE == 'invalid-scope':
            for field, value in [('cwd', str(HOME.parent / 'unadmitted-workspace')), ('allowed_tools', ['send_message'])]:
                bad = copy.deepcopy(params)
                bad['payload'][field] = value
                try:
                    await original_child(connection, ref, bad)
                except RuntimeStoreError as exc:
                    record({'field': field, 'refused': exc.reason})
                else:
                    raise AssertionError('native child scope was expanded: ' + field)
    return await original_child(connection, ref, params)
async def worker_request(connection, ref, params, *, operation):
    result = await original_worker(connection, ref, params, operation=operation)
    if operation == 'persist' and params.get('operation') == 'execution.finish' and '/child:' in params['execution_id']:
        late = dict(params, sequence=params['sequence'] + 1, operation='process.reserve',
            payload={'tool': 'terminal', 'digest': '0' * 64})
        try:
            await original_worker(connection, ref, late, operation='persist')
        except RuntimeStoreError as exc:
            record({'late_process': exc.reason})
        else:
            raise AssertionError('released native child resumed process authority')
    return result
child.worker_child_request = child_request
worker.worker_request = worker_request
from gateway.run import main
main()
'''.replace('__HOME__', repr(str(home))).replace('__MODE__', repr(mode)).replace('__WORKER_STOP_SOURCE__', repr('''
from pathlib import Path
import agent.managed_worker as worker
HOME = Path(__HOME__)
construct = worker._construct_agent
def construct_with_native_stop(*args, **kwargs):
    agent = construct(*args, **kwargs)
    from tools.delegate_tool_child_run import _ChildRun
    from tests.waiting import wait_until
    original_wait = _ChildRun.wait_liveness_aware
    def stop_real_child(self, settled, future, timeout):
        if self.goal == 'CHILD_SIBLING_GOAL':
            return original_wait(self, settled, future, timeout)
        wait_until((HOME / 'wait.entered').exists, timeout=30, what='actual owner process.wait entry')
        self.child.hard_interrupt()
        (HOME / 'child-stop-acked').touch()
        return original_wait(self, settled, future, None)
    _ChildRun.wait_liveness_aware = stop_real_child
    return agent
worker._construct_agent = construct_with_native_stop
worker.main()
'''.replace('__HOME__', repr(str(home))))))


async def _delegate(home, desc, url, cwd=None):
    async with websocket(home, desc) as ws:
        created = await rpc(ws, 'session.create', request_id='parent', source='cli', cwd=cwd or str(home),
            model='fixture-model', provider='custom', base_url=url, api_key='loopback-only',
            toolsets=['delegation', 'terminal'], ignore_rules=True)
        assert 'result' in created, created
        sid = created['result']['session_id']
        submitted = await rpc(ws, 'prompt.submit', session_id=sid, input_id='child-process', text='DELEGATE_PROCESS')
        assert 'result' in submitted, submitted
        await async_wait_until(lambda: _settled(home, 'child-process'), timeout=90,
                               what='native delegated process settlement')
        return sid


@pytest.mark.platforms('linux')
def test_native_delegated_child_runs_and_waits_for_its_own_background_process(tmp_path):
    """Real producer, owner reservation, WorkerRPC admission and terminal consumer."""
    marker = tmp_path / 'child-effect'
    script = tmp_path / 'child_body.py'
    script.write_text('from pathlib import Path\nPath(' + repr(str(marker))
                      + ').write_text("owned by the child")\nprint("NATIVE_CHILD_OUTPUT", flush=True)\n')
    with _fixture(tmp_path) as (home, peer, url, env, launcher):
        peer.RequestHandlerClass = ChildProcessModel
        peer.child_requests = []
        peer.child_mode = 'wait'
        peer.child_command = shlex.quote(sys.executable) + ' ' + shlex.quote(str(script))
        config = json.loads((home / 'config.yaml').read_text())
        config['platform_toolsets']['cli'] = ['delegation', 'terminal']
        config['delegation'] = {'worktree_isolation': False}
        (home / 'config.yaml').write_text(json.dumps(config))

        async def exercise(desc):
            async with websocket(home, desc) as ws:
                created = await rpc(ws, 'session.create', request_id='parent', source='cli', cwd=str(home),
                    model='fixture-model', provider='custom', base_url=url, api_key='loopback-only',
                    toolsets=['delegation', 'terminal'], ignore_rules=True)
                assert 'result' in created, created
                sid = created['result']['session_id']
                submitted = await rpc(ws, 'prompt.submit', session_id=sid, input_id='child-process',
                                      text='DELEGATE_PROCESS')
                assert 'result' in submitted, submitted
                await async_wait_until(lambda: _settled(home, 'child-process'), timeout=90,
                                       what='native delegated process settlement')
                return sid

        with daemon(ROOT, home, env, barrier=True, fixture=str(launcher)) as (_owner, desc):
            sid = asyncio.run(exercise(desc))
            children = _rows(home, "SELECT w.session_id,w.execution_id,w.status,s.parent_session_id "
                "FROM worker_executions w JOIN sessions s ON s.id=w.session_id WHERE w.kind='child'")
            assert len(children) == 1 and children[0][2:] == ('terminal', sid), children
            child_sid, child_execution = children[0][:2]
            assert '/child:' in child_execution
            tools = [json.loads(content) for role, content in _rows(home,
                'SELECT role,content FROM messages WHERE session_id=? ORDER BY id', child_sid) if role == 'tool']
            assert tools and tools[0].get('session_id'), (tools, (home / 'first.log').read_text())
            assert marker.read_text() == 'owned by the child'
            assert len(tools) == 2 and tools[-1]['status'] == 'exited', (tools,
                [request.get('messages', [])[-3:] for request in peer.child_requests], (home / 'first.log').read_text())
            assert tools[-1]['output'] == 'NATIVE_CHILD_OUTPUT\n', tools
            receipts = _rows(home, "SELECT json_extract(result_json,'$.tool') FROM worker_receipts "
                "WHERE execution_id=? AND json_extract(result_json,'$.resource_id') IS NOT NULL", child_execution)
            assert len(receipts) == 2, receipts


@pytest.mark.platforms('linux')
@pytest.mark.parametrize('mode', ['cleanup', 'timeout', 'handoff'])
def test_native_child_lifecycle_preserves_parent_and_fences_its_own_body(tmp_path, mode):
    child, command = _body(tmp_path, 'child')
    parent, parent_command = _body(tmp_path, 'parent')
    sibling, sibling_command = _body(tmp_path, 'sibling') if mode == 'timeout' else (None, None)
    with _native_child(tmp_path, command, mode=mode,
            transport_probe='native-stop' if mode == 'timeout' else None,
            parent_command=parent_command, sibling_command=sibling_command) as (home, _peer, url, env, launcher):
        try:
            with daemon(ROOT, home, env, barrier=True, fixture=str(launcher)) as (_owner, desc):
                sid = asyncio.run(_delegate(home, desc, url))
                assert (child / 'ready').exists(), (home / 'first.log').read_text()
                child_pid, parent_pid = int((child / 'pid').read_text()), int((parent / 'pid').read_text())
                assert _alive(parent_pid), 'Child cleanup or Stop must preserve its parent-owned process.'
                child_sid = _rows(home, "SELECT w.session_id FROM worker_executions w JOIN messages m "
                    "ON m.session_id=w.session_id WHERE w.kind='child' AND m.role='user' "
                    "AND m.content='CHILD_PROCESS_GOAL'")[0][0]
                child_tools = [json.loads(content) for role, content in _rows(home,
                    'SELECT role,content FROM messages WHERE session_id=? ORDER BY id', child_sid) if role == 'tool']
                assert len(child_tools) >= 2 and 'processes' in child_tools[1], child_tools
                assert parent_command not in json.dumps(child_tools[1]), 'Child list leaked its parent process.'
                if mode == 'handoff':
                    assert _alive(child_pid), 'A native accepted handoff must survive child teardown.'
                    assert 'handed_off' in str(_rows(home, 'SELECT content FROM messages WHERE session_id=?', child_sid))
                    parent_result = [json.loads(content) for role, content in _rows(home,
                        'SELECT role,content FROM messages WHERE session_id=? ORDER BY id', sid) if role == 'tool'][-1]
                    handoffs = parent_result['results'][0]['handed_off_processes']
                    assert handoffs[0]['session_id'] == child_tools[-1]['session_id'], (handoffs, child_tools)
                    assert handoffs[0]['note'] == 'Keep the child process alive for the parent to collect.'
                    assert not parent_result['results'][0].get('orphaned_processes'), parent_result
                    (child / 'release').touch()
                    assert (parent / 'effect').exists() is False
                else:
                    if mode == 'timeout':
                        assert (home / 'child-stop-acked').exists(), 'Real native child hard Stop did not return its fenced ACK.'
                        assert (sibling / 'ready').exists() and _alive(int((sibling / 'pid').read_text())), (
                            'Stopping one child revoked its native sibling reservation/resource.')
                    assert not _alive(child_pid), 'The child still owns an effectful body after cleanup/Stop.'
                    (child / 'release').touch()
                    assert not (child / 'effect').exists(), 'Stopped child cannot perform the delayed external effect.'
                (parent / 'release').touch()
        finally:
            (child / 'release').touch()
            (parent / 'release').touch()
            if sibling is not None:
                (sibling / 'release').touch()


@pytest.mark.platforms('linux')
def test_native_registered_external_writer_worktree_keeps_its_existing_admission(tmp_path):
    """Existing native writer evidence admits an external repo; cwd is not a project ACL."""
    repository = tmp_path / 'external-project'
    repository.mkdir()
    for args in (['init', '-b', 'main'], ['config', 'user.email', 'local@example.invalid'],
                 ['config', 'user.name', 'Native admission fixture'],
                 ['commit', '--allow-empty', '-m', 'native fixture'],
                 ['update-ref', 'refs/remotes/origin/main', 'HEAD']):
        subprocess.run(['git', '-C', str(repository), *args], check=True, capture_output=True, text=True)
    command = shlex.quote(sys.executable) + ' -c ' + shlex.quote('import os; print(os.getcwd(), flush=True)')
    with _native_child(tmp_path, command, repo_root=str(repository)) as (home, _peer, url, env, launcher):
        with daemon(ROOT, home, env, barrier=True, fixture=str(launcher)) as (_owner, desc):
            sid = asyncio.run(_delegate(home, desc, url, cwd=str(repository)))
            children = _rows(home, "SELECT session_id FROM worker_executions WHERE kind='child'")
            assert children, _rows(home, 'SELECT role,content FROM messages WHERE session_id=?', sid)
            child_sid = children[0][0]
            messages = _rows(home, 'SELECT role,content FROM messages WHERE session_id=?', child_sid)
            tools = [json.loads(content) for role, content in messages if role == 'tool']
            assert tools and tools[0].get('session_id'), (tools, (home / 'first.log').read_text())
            assert len(tools) == 2 and str(repository / '.worktrees' / 'subagent-sa-') in tools[-1]['output'], tools


@pytest.mark.platforms('linux')
@pytest.mark.parametrize('probe', ['invalid-scope', 'before-bind-stop'])
def test_native_child_transport_cannot_expand_or_reopen_reserved_authority(tmp_path, probe):
    marker = tmp_path / 'effect'
    command = shlex.quote(sys.executable) + ' -c ' + shlex.quote(
        'from pathlib import Path; Path(' + repr(str(marker)) + ').touch(); print("native effect")')
    with _native_child(tmp_path, command, transport_probe=probe) as (home, _peer, url, env, launcher):
        with daemon(ROOT, home, env, barrier=True, fixture=str(launcher)) as (_owner, desc):
            asyncio.run(_delegate(home, desc, url))
            probes = [json.loads(line) for line in (home / 'authority-probes.jsonl').read_text().splitlines()]
            if probe == 'before-bind-stop':
                assert probes == [{'cancel': {'retired': True}}], probes
                assert not marker.exists(), 'A cancelled native child must never be rebound or start an effect.'
            else:
                assert marker.exists(), (probes, (home / 'first.log').read_text())
                assert probes[:2] == [{'field': 'cwd', 'refused': 'permission_denied'},
                                      {'field': 'allowed_tools', 'refused': 'invalid_params'}], probes
                assert probes[-1] == {'late_process': 'stale_generation'}, probes


@pytest.mark.platforms('linux')
def test_parent_idle_stop_reaps_persisted_native_child_after_worker_exit(tmp_path):
    """Persistence skips agent-close cleanup; the parent native Stop still owns the body."""
    child, command = _body(tmp_path, 'persisted-child')
    other, other_command = _body(tmp_path, 'unrelated')
    with _native_child(tmp_path, command, mode='cleanup') as (home, peer, url, env, launcher):
        peer.child_persist = True
        peer.commands['OTHER_BODY'] = {'command': other_command, 'background': True}

        async def exercise(desc):
            sid = await _delegate(home, desc, url)
            child_sid = _rows(home, "SELECT session_id FROM worker_executions WHERE kind='child'")[0][0]
            tools = [json.loads(content) for role, content in _rows(home,
                'SELECT role,content FROM messages WHERE session_id=?', child_sid) if role == 'tool']
            assert tools and tools[0].get('session_id'), tools
            resource = tools[0]['session_id']
            worker = json.loads((home / 'workers.jsonl').read_text().splitlines()[0])
            await async_wait_until(lambda: not _alive(worker['pid']), timeout=10, what='physical parent worker exit')
            child_pid = int((child / 'pid').read_text())
            assert _alive(child_pid), 'Existing persist_on_release semantics must survive normal child close.'
            async with websocket(home, desc) as ws:
                other_sid = await _create(ws, home, url, 'unrelated')
                await rpc(ws, 'prompt.submit', session_id=other_sid, input_id='other-start', text='OTHER_BODY')
                await async_wait_until(lambda: _settled(home, 'other-start'), timeout=45, what='unrelated body start')
                other_pid = int((other / 'pid').read_text())
                listed = await rpc(ws, 'process.list', session_id=sid)
                assert 'result' in listed and resource in str(listed), listed
                snapshot = (await rpc(ws, 'session.resume', session_id=sid))['result']
                stopped = await rpc(ws, 'session.interrupt', session_id=sid,
                                    execution_generation=snapshot['execution_generation'])
                assert 'result' in stopped, stopped
                await async_wait_until(lambda: not _alive(child_pid), timeout=10, what='native parent Stop physical child reap')
                assert _alive(other_pid), 'Parent Stop crossed into an unrelated native conversation.'
                (child / 'release').touch()
                assert not (child / 'effect').exists()
                (other / 'release').touch()
        try:
            with daemon(ROOT, home, env, barrier=True, fixture=str(launcher)) as (_owner, desc):
                asyncio.run(exercise(desc))
        finally:
            (child / 'release').touch()
            (other / 'release').touch()


@pytest.mark.platforms('linux')
def test_native_child_owner_consumer_keeps_original_callback_and_task_ancestry(tmp_path):
    command = shlex.quote(sys.executable) + ' -c ' + shlex.quote('print("native child callback")')
    with _native_child(tmp_path, command, transport_probe='callback-policy') as (home, _peer, url, env, launcher):
        with daemon(ROOT, home, env, barrier=True, fixture=str(launcher)) as (_owner, desc):
            asyncio.run(_delegate(home, desc, url))
            evidence = [json.loads(line) for line in (home / 'authority-probes.jsonl').read_text().splitlines()]
            consumer = evidence[0]
            assert consumer['callback'] == '_subagent_auto_deny', consumer
            assert consumer['task'].startswith('sa-') and consumer['task'] in consumer['ancestry'], consumer
            assert evidence[-1] == {'late_process': 'stale_generation'}, evidence


@pytest.mark.platforms('linux')
def test_native_schema_repair_retains_original_child_scope_without_replaying_goal(tmp_path):
    work = tmp_path / 'repair-work'
    work.mkdir()
    starts = work / 'schema-repair-starts'
    command = shlex.quote(sys.executable) + ' -c ' + shlex.quote(
        'from pathlib import Path; p=Path("schema-repair-starts"); '
        'p.open("a").write("one native repair body\\n"); print("SCHEMA_REPAIR_OUTPUT", flush=True)')
    with _native_child(tmp_path, command, transport_probe='callback-policy') as (home, peer, url, env, launcher):
        peer.schema_repair = True
        peer.schema_initial_command = 'cd ' + shlex.quote(str(work)) + '; pwd'
        with daemon(ROOT, home, env, barrier=True, fixture=str(launcher)) as (_owner, desc):
            sid = asyncio.run(_delegate(home, desc, url))
            children = _rows(home, "SELECT session_id,execution_id,status FROM worker_executions WHERE kind='child'")
            assert len(children) == 1 and children[0][2] == 'terminal', children
            child_sid, execution = children[0][:2]
            messages = _rows(home, 'SELECT role,content FROM messages WHERE session_id=? ORDER BY id', child_sid)
            goals = [content for role, content in messages if role == 'user' and content == 'CHILD_PROCESS_GOAL']
            repairs = [content for role, content in messages if role == 'user'
                       and content.startswith('Your previous final response was rejected')]
            assert len(goals) == len(repairs) == 1, messages
            tools = [json.loads(content) for role, content in messages if role == 'tool']
            assert len(tools) == 3 and tools[0]['output'].strip() == str(work), tools
            assert tools[-1]['output'] == 'SCHEMA_REPAIR_OUTPUT\n', tools
            assert starts.read_text().splitlines() == ['one native repair body']
            assert not (home / 'schema-repair-starts').exists()
            receipts = _rows(home, "SELECT json_extract(result_json,'$.tool') FROM worker_receipts "
                "WHERE execution_id=? AND json_extract(result_json,'$.resource_id') IS NOT NULL", execution)
            assert sorted(receipts) == [('process_manage',), ('terminal',)], receipts
            bindings = _rows(home, "SELECT count(*) FROM worker_receipts WHERE execution_id=? "
                "AND json_extract(result_json,'$.bound')=1", execution)
            assert bindings == [(1,)], bindings
            evidence = [json.loads(line) for line in (home / 'authority-probes.jsonl').read_text().splitlines()]
            consumers = [item for item in evidence if 'callback' in item]
            assert len(consumers) == 1 and consumers[0]['task'] in consumers[0]['ancestry'], evidence
            assert consumers[0]['task'].startswith('sa-') and consumers[0]['task'] != sid, evidence
            parent = [json.loads(content) for role, content in _rows(home,
                'SELECT role,content FROM messages WHERE session_id=? ORDER BY id', sid) if role == 'tool'][-1]
            result = parent['results'][0]
            assert result['schema_valid'] is True and result['schema_retries'] == 1, result
            assert json.loads(result['summary']) == {'value': 42}, result
