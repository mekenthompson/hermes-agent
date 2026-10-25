"""Observed shell cwd crosses the private process boundary without changing frozen mounts."""
import asyncio
from http.server import BaseHTTPRequestHandler
import json

import pytest

from tests.gateway.fixtures.local_recovery_probe import daemon, websocket
from tests.gateway.test_managed_process_lifecycle import ROOT, _create, _fixture, _submit
from tests.waiting import async_wait_until


class CwdModel(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        messages = body.get('messages') or []
        count = sum(message['role'] == 'tool' for message in messages)
        arguments = self.server.calls[count] if count < len(self.server.calls) else None
        message = ({'role': 'assistant', 'content': None, 'tool_calls': [{
            'id': 'cwd-' + str(count), 'type': 'function',
            'function': {'name': 'terminal', 'arguments': json.dumps(arguments)}}]}
            if arguments is not None else {'role': 'assistant', 'content': 'CWD_DONE'})
        choice = {'index': 0, 'message': message, 'finish_reason': 'tool_calls' if arguments is not None else 'stop'}
        frame = {'id': 'cwd', 'model': 'fixture-model', 'choices': [choice],
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
@pytest.mark.parametrize('direction', ['fg-to-bg', 'bg-to-fg', 'explicit-workdir'])
def test_managed_observed_cwd_preserves_shell_and_background_isolation(tmp_path, direction):
    with _fixture(tmp_path) as (home, peer, url, env, launcher):
        sub = home / 'subdir'
        sub.mkdir()
        peer.RequestHandlerClass = CwdModel
        peer.calls = {
            'fg-to-bg': [{'command': 'cd subdir'}, {'command': 'pwd > bg-cwd', 'background': True}],
            'bg-to-fg': [{'command': 'cd subdir && pwd > bg-cwd', 'background': True}, {'command': 'pwd'}],
            'explicit-workdir': [{'command': 'cd subdir'}, {'command': 'pwd > bg-cwd', 'background': True,
                'workdir': str(home)}, {'command': 'pwd'}],
        }[direction]

        async def exercise(desc):
            async with websocket(home, desc) as ws:
                sid = await _create(ws, home, url, 'cwd')
                snapshot = await _submit(ws, home, sid, 'Preserve the current shell directory', 'cwd-start')
                tools = [json.loads(message['content']) for message in snapshot['messages'] if message['role'] == 'tool']
                assert len(tools) == len(peer.calls) and all(not item.get('error') for item in tools), tools
                expected = home if direction == 'explicit-workdir' else sub
                await async_wait_until((expected / 'bg-cwd').exists, timeout=10, what='actual relative background path')
                assert (expected / 'bg-cwd').read_text().strip() == str(expected), tools
                if direction == 'fg-to-bg':
                    assert tools[0]['cwd'] == str(sub) and not (home / 'bg-cwd').exists(), tools
                elif direction == 'bg-to-fg':
                    assert tools[-1]['output'].strip() == str(home), tools
                else:
                    assert tools[0]['cwd'] == str(sub) and tools[-1]['output'].strip() == str(sub), tools
                    assert not (sub / 'bg-cwd').exists()
        with daemon(ROOT, home, env, barrier=True, fixture=str(launcher)) as (_owner, desc):
            asyncio.run(exercise(desc))
