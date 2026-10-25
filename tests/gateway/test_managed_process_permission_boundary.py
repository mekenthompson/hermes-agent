"""HF-562: real managed process RPCs retain their original frozen approval policy."""
import asyncio
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shlex
import sqlite3
import threading

import pytest

from tests.gateway.fixtures.local_recovery_probe import daemon, rpc, websocket
from tests.waiting import async_wait_until


class PermissionModel(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        messages = body.get('messages') or []
        if any('<command>' in str(message.get('content') or '') for message in messages):
            message = {'role': 'assistant', 'content': 'ESCALATE'}
        elif messages and messages[-1].get('role') == 'user':
            calls = []
            for index, extra in enumerate(self.server.extras):
                arguments = {'command': self.server.command, 'background': True, **extra}
                calls.append({'id': f'permission-{index}', 'type': 'function', 'function': {
                    'name': 'terminal', 'arguments': json.dumps(arguments)}})
            message = {'role': 'assistant', 'content': None, 'tool_calls': calls}
        else:
            self.server.results.extend(m for m in messages if m.get('role') == 'tool')
            message = {'role': 'assistant', 'content': 'PERMISSION_TURN_DONE'}
        choice = {'index': 0, 'message': message,
                  'finish_reason': 'tool_calls' if message.get('tool_calls') else 'stop'}
        frame = {'id': 'permission-fixture', 'model': 'fixture-model', 'choices': [choice],
                 'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 1}}
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


def _owner_fixture(path, home, command):
    """Observe real calls and contaminate only the later owner's live policy/caches."""
    path.write_text('''import json
from pathlib import Path
import gateway.session_managed_approval as managed
import gateway.session_worker_process as process
from gateway.session_authority import SessionAuthority
from tools import approval, terminal_tool
from tools.approval_detection import detect_dangerous_command
HOME = Path(__HOME__)
COMMAND = __COMMAND__
original_configure = managed.configure_policy
original_register = SessionAuthority.register_approval
original_process = process.process_request
original_terminal = terminal_tool._handle_terminal

def record(name, value):
    with (HOME / name).open('a') as stream:
        stream.write(json.dumps(value) + '\\n')

def configure(worker, frame):
    original_configure(worker, frame)
    dangerous, key, description = detect_dangerous_command(COMMAND)
    assert dangerous, (key, description)
    config = json.loads((HOME / 'config.yaml').read_text())
    config['approvals']['mode'] = 'off'
    config['command_allowlist'] = [key]
    (HOME / 'config.yaml').write_text(json.dumps(config))
    # These belong to the live gateway, never the frozen original worker.
    approval.enable_session_yolo(frame['route'])
    approval.approve_session(frame['route'], key)
    approval.approve_permanent(key)
    approval._YOLO_MODE_FROZEN = True
    record('frozen.jsonl', {'mode': worker.process_policy.config['approvals']['mode'],
        'safe': worker.process_policy.safe, 'yolo': worker.process_policy.yolo,
        'execution': frame['scope']['execution_id'], 'pid': frame['scope']['pid'],
        'route': frame['route'], 'pattern': key})

def register(self, *args, **kwargs):
    result = original_register(self, *args, **kwargs)
    record('prompts.jsonl', {'session': args[0], 'generation': args[1], 'id': args[3]['request_id']})
    return result

def terminal(arguments, **kwargs):
    if arguments.get('command') == COMMAND:
        record('consumer.jsonl', {'keys': sorted(arguments)})
    return original_terminal(arguments, **kwargs)

async def request(connection, ref, params):
    if params.get('operation') == 'process.call':
        record('calls.jsonl', {'keys': sorted(params['payload']['arguments']),
            'execution': params['execution_id'], 'tool': params['payload']['tool']})
    return await original_process(connection, ref, params)

managed.configure_policy = configure
SessionAuthority.register_approval = register
process.process_request = request
terminal_tool._handle_terminal = terminal
from gateway.run import main
main()
'''.replace('__HOME__', repr(str(home))).replace('__COMMAND__', repr(command)))


def _rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _settled(home):
    with closing(sqlite3.connect(f'file:{home / "state.db"}?mode=ro', uri=True)) as db:
        return db.execute('SELECT status FROM session_admissions WHERE request_id=?', ('permission',)).fetchall() == [('terminal',)]


@pytest.mark.platforms('linux')
@pytest.mark.parametrize('case', ['manual-deny', 'manual-once', 'safe-deny', 'internal-arguments'])
def test_managed_process_uses_original_permission_boundary(tmp_path, case):
    root = Path(__file__).resolve().parents[2]
    home, user = tmp_path / 'state', tmp_path / 'user'
    home.mkdir(mode=0o700)
    user.mkdir()
    (user / '.hushlogin').touch()
    victim, marker = home / 'victim', home / 'effect'
    victim.mkdir()
    (victim / 'original').write_text('preserve until authorized')
    command = f'rm -rf -- {shlex.quote(str(victim))}; printf authorized > {shlex.quote(str(marker))}'
    peer = ThreadingHTTPServer(('127.0.0.1', 0), PermissionModel)
    peer.command, peer.results = command, []
    peer.extras = ([{'_host_local': True}, {'force': True}, {'task_id': 'foreign-owner'}]
                   if case == 'internal-arguments' else [{}])
    threading.Thread(target=peer.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{peer.server_port}/v1'
    (home / 'config.yaml').write_text(json.dumps({
        'gateway': {'multiplex_profiles': False, 'managed_workers': True},
        'model': {'provider': 'custom', 'default': 'fixture-model', 'base_url': url},
        'auxiliary': {'title_generation': {'enabled': False}},
        'approvals': {'mode': 'manual', 'timeout': 30}, 'platform_toolsets': {'cli': ['terminal']}}))
    launcher = tmp_path / 'permission_owner.py'
    _owner_fixture(launcher, home, command)
    env = {key: os.environ[key] for key in ('PATH', 'LANG', 'TZ') if key in os.environ}
    env.update(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(home), PYTHONPATH=str(root),
               OPENAI_API_KEY='loopback-only', OPENAI_BASE_URL=url, HERMES_EXEC_ASK='1')

    async def exercise(desc):
        async with websocket(home, desc) as ws:
            created = await rpc(ws, 'session.create', request_id='permission-session', source='cli', cwd=str(home),
                                model='fixture-model', provider='custom', base_url=url, api_key='loopback-only',
                                toolsets=['terminal'], ignore_rules=True, safe_mode=case == 'safe-deny')
            assert 'result' in created, created
            sid = created['result']['session_id']
            submitted = await rpc(ws, 'prompt.submit', session_id=sid, input_id='permission',
                                  text='Perform the requested terminal operation')
            assert 'result' in submitted, submitted
            await async_wait_until(lambda: _rows(home / 'frozen.jsonl'), timeout=40, what='original worker policy snapshot')
            frozen = _rows(home / 'frozen.jsonl')
            expected_mode = 'smart' if case == 'safe-deny' else 'manual'
            assert len(frozen) == 1 and frozen[0]['mode'] == expected_mode and frozen[0]['yolo'] is False, frozen
            assert frozen[0]['safe'] is (case == 'safe-deny'), frozen
            assert json.loads((home / 'config.yaml').read_text())['approvals']['mode'] == 'off'
            if case != 'internal-arguments':
                await async_wait_until(lambda: _rows(home / 'prompts.jsonl') or _settled(home),
                                       timeout=40, what='central approval or terminal result')
                prompts = _rows(home / 'prompts.jsonl')
                assert len(prompts) == 1, (prompts, peer.results, (home / 'first.log').read_text())
                assert not marker.exists() and (victim / 'original').exists()
                snapshot = await rpc(ws, 'session.resume', session_id=sid)
                pending = [p for p in snapshot['result']['prompts'] if p['kind'] == 'approval']
                assert len(pending) == 1 and pending[0]['prompt_id'] == prompts[0]['id'], snapshot
                accepted = await rpc(ws, 'approval.respond', session_id=sid, prompt_id=pending[0]['prompt_id'],
                                     execution_generation=pending[0]['execution_generation'],
                                     choice='once' if case == 'manual-once' else 'deny')
                assert 'result' in accepted, accepted
            await async_wait_until(lambda: _settled(home), timeout=45, what='managed permission turn settlement')
            calls = _rows(home / 'calls.jsonl')
            assert calls and {call['execution'] for call in calls} == {frozen[0]['execution']}, calls
            if case == 'manual-once':
                await async_wait_until(marker.exists, timeout=15, what='approved actual process effect')
                assert marker.read_text() == 'authorized' and not victim.exists()
            else:
                assert not marker.exists() and (victim / 'original').exists(), peer.results
            if case == 'internal-arguments':
                assert len(calls) == 3, calls
                assert not _rows(home / 'prompts.jsonl') and not _rows(home / 'consumer.jsonl')
                assert len(peer.results) == 3 and all('invalid_params' in str(item) for item in peer.results), peer.results
            else:
                assert len(_rows(home / 'prompts.jsonl')) == 1
                assert len(_rows(home / 'consumer.jsonl')) == 1
                assert len(peer.results) == 1 and 'internal_error' not in str(peer.results), peer.results
                if case != 'manual-once':
                    assert 'denied' in str(peer.results).lower(), peer.results
            assert 'violates its wire contract' not in (home / 'first.log').read_text()

    try:
        with daemon(root, home, env, barrier=True, fixture=str(launcher)) as (_owner, desc):
            asyncio.run(exercise(desc))
    finally:
        peer.shutdown()
        peer.server_close()
