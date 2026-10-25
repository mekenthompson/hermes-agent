"""A managed GUI request approves each distinct background command once."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shlex
import threading

import pytest

from tests.gateway.fixtures.local_recovery_probe import daemon, rpc, websocket
from tests.gateway.test_managed_process_lifecycle import _settled
from tests.gateway.test_managed_process_permission_boundary import _owner_fixture, _rows
from tests.waiting import async_wait_until


class BatchModel(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        messages = body.get('messages') or []
        if messages and messages[-1].get('role') == 'user' and '<command>' in str(messages[-1].get('content')):
            message = {'role': 'assistant', 'content': 'ESCALATE'}
        elif messages and messages[-1].get('role') == 'user' and messages[-1].get('content') == 'Perform both configured commands':
            message = {'role': 'assistant', 'content': None, 'tool_calls': [{
                'id': 'batch-' + str(index), 'type': 'function',
                'function': {'name': 'terminal', 'arguments': json.dumps({'command': command, 'background': True})}}
                for index, command in enumerate(self.server.commands)]}
        else:
            self.server.results.extend(message for message in messages if message.get('role') == 'tool')
            message = {'role': 'assistant', 'content': 'BATCH_DONE'}
        choice = {'index': 0, 'message': message, 'finish_reason': 'tool_calls' if message.get('tool_calls') else 'stop'}
        frame = {'id': 'batch', 'model': 'fixture-model', 'choices': [choice],
                 'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}}
        if body.get('stream'):
            choice['delta'] = choice.pop('message')
            for index, call in enumerate(choice['delta'].get('tool_calls', [])):
                call['index'] = index
            payload = ('data: ' + json.dumps(frame) + '\n\ndata: [DONE]\n\n').encode()
        else:
            payload = json.dumps(frame).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream' if body.get('stream') else 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.mark.platforms('linux')
def test_two_managed_gui_background_commands_each_require_one_original_approval(tmp_path):
    root = Path(__file__).resolve().parents[2]
    home, user = tmp_path / 'state', tmp_path / 'user'
    home.mkdir(mode=0o700)
    user.mkdir()
    (user / '.hushlogin').touch()
    victim, marker = home / 'victim', home / 'effect'
    victim.mkdir()
    (victim / 'original').write_text('await authorized commands')
    command = f'rm -rf -- {shlex.quote(str(victim))}; printf authorized >> {shlex.quote(str(marker))}'
    peer = ThreadingHTTPServer(('127.0.0.1', 0), BatchModel)
    second = f'rm -rf -- {shlex.quote(str(victim))}; printf second >> {shlex.quote(str(marker))}'
    peer.commands, peer.results = [command, second], []
    threading.Thread(target=peer.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{peer.server_port}/v1'
    (home / 'config.yaml').write_text(json.dumps({
        'gateway': {'multiplex_profiles': False, 'managed_workers': True},
        'model': {'provider': 'custom', 'default': 'fixture-model', 'base_url': url},
        'auxiliary': {'title_generation': {'enabled': False}},
        'approvals': {'mode': 'manual', 'timeout': 30}, 'platform_toolsets': {'cli': ['terminal']}}))
    launcher = tmp_path / 'batch_owner.py'
    _owner_fixture(launcher, home, command)
    env = {key: os.environ[key] for key in ('PATH', 'LANG', 'TZ') if key in os.environ}
    env.update(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(home), PYTHONPATH=str(root),
               OPENAI_API_KEY='loopback-only', OPENAI_BASE_URL=url, HERMES_EXEC_ASK='1')

    async def exercise(desc):
        async with websocket(home, desc) as ws:
            created = await rpc(ws, 'session.create', request_id='batch', source='gui', cwd=str(home),
                model='fixture-model', provider='custom', base_url=url, api_key='loopback-only',
                toolsets=['terminal'], ignore_rules=True, ignore_user_config=True)
            assert 'result' in created, created
            sid = created['result']['session_id']
            assert 'result' in await rpc(ws, 'prompt.submit', session_id=sid, input_id='batch', text='Perform both configured commands')
            answered = set()
            async with asyncio.timeout(50):
                while not _settled(home, 'batch'):
                    snapshot = (await rpc(ws, 'session.resume', session_id=sid))['result']
                    pending = [prompt for prompt in snapshot['prompts'] if prompt['kind'] == 'approval'
                               and prompt['prompt_id'] not in answered]
                    for prompt in pending:
                        answered.add(prompt['prompt_id'])
                        result = await rpc(ws, 'approval.respond', session_id=sid, prompt_id=prompt['prompt_id'],
                            execution_generation=prompt['execution_generation'], choice='once')
                        assert 'result' in result, result
                    await async_wait_until(lambda: _settled(home, 'batch') or len(_rows(home / 'prompts.jsonl')) > len(answered),
                                           timeout=35, what='next actual approval or batch settlement')
            assert len(answered) == len(_rows(home / 'prompts.jsonl')) == 2, json.dumps({
                'prompts': _rows(home / 'prompts.jsonl'), 'results': peer.results,
                'snapshot': (await rpc(ws, 'session.resume', session_id=sid))['result']['messages'],
                'log': (home / 'first.log').read_text()}, indent=2)
            assert all('error' not in json.loads(item['content']) or not json.loads(item['content'])['error']
                       for item in peer.results), json.dumps(peer.results, indent=2)
            await async_wait_until(lambda: marker.exists() and marker.read_text() == 'authorizedsecond',
                                   timeout=10, what='two approved actual background effects')
            assert len(_rows(home / 'calls.jsonl')) == 2
            assert len(peer.results) == 2 and all('internal_error' not in str(item) for item in peer.results)
    try:
        with daemon(root, home, env, barrier=True, fixture=str(launcher)) as (_owner, desc):
            asyncio.run(exercise(desc))
    finally:
        peer.shutdown()
        peer.server_close()
