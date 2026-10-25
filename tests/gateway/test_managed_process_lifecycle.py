"""Native managed workers hand process custody to the existing gateway owner."""
import asyncio
from contextlib import closing, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shlex
import sqlite3
import sys
import threading

import psutil
import pytest

from tests.gateway.fixtures.local_recovery_probe import daemon, rpc, websocket
from tests.waiting import async_wait_until, wait_until

ROOT = Path(__file__).resolve().parents[2]


def _alive(pid):
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _status(home, request):
    with closing(sqlite3.connect(f'file:{home / "state.db"}?mode=ro', uri=True)) as db:
        row = db.execute('SELECT status FROM session_admissions WHERE request_id=?', (request,)).fetchone()
    return row[0] if row else None


def _settled(home, request):
    return _status(home, request) == 'terminal'


class LifecycleModel(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        messages = body.get('messages') or []
        last = messages[-1] if messages else {}
        if last.get('role') == 'user' and last.get('content') in self.server.commands:
            name, arguments = 'terminal', self.server.commands[last['content']]
        elif last.get('role') == 'user' and str(last.get('content', '')).startswith('WAIT '):
            name, arguments = 'tool_call', {'calls': [{'name': 'process_manage', 'arguments': {
                'action': 'wait', 'session_id': last['content'].split()[1], 'timeout': 30}}]}
        else:
            name, arguments = None, None
            self.server.results.extend(message for message in messages if message.get('role') == 'tool')
        message = ({'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'lifecycle', 'type': 'function',
                    'function': {'name': name, 'arguments': json.dumps(arguments)}}]}
                   if name else {'role': 'assistant', 'content': 'LIFECYCLE_DONE'})
        choice = {'index': 0, 'message': message, 'finish_reason': 'tool_calls' if name else 'stop'}
        frame = {'id': 'lifecycle', 'model': 'fixture-model', 'choices': [choice],
                 'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}}
        if body.get('stream'):
            choice['delta'] = choice.pop('message')
            for index, call in enumerate(choice['delta'].get('tool_calls', [])):
                call['index'] = index
            payload, kind = ('data: ' + json.dumps(frame) + '\n\ndata: [DONE]\n\n').encode(), 'text/event-stream'
        else:
            payload, kind = json.dumps(frame).encode(), 'application/json'
        self.send_response(200)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _launcher(path, home, mode):
    path.write_text('''import json
import asyncio
from pathlib import Path
import gateway.session_managed_approval as managed
import gateway.session_worker_process as process
from gateway.session_authority import SessionAuthority
from gateway.session_runtime_workers import command_effects, command_unknowns, join_authority_work, stop_authority_work
from tools.process_registry import process_registry
HOME = Path(__HOME__)
MODE = __MODE__
original_configure = managed.configure_policy
original_request = process.process_request
original_attach = process.attach_lifetime
original_view = SessionAuthority.attach

def record(name, value):
    with (HOME / name).open('a') as stream:
        stream.write(json.dumps(value) + '\\n')

def configure(worker, frame):
    original_configure(worker, frame)
    record('workers.jsonl', {'pid': frame['scope']['pid'], 'route': frame['route'],
        'physical': frame['scope']['session_id'], 'execution': frame['scope']['execution_id']})
    if MODE == 'released':
        worker.original_reservation.release()

async def request(connection, ref, params):
    result = await original_request(connection, ref, params)
    if params.get('operation') == 'process.call':
        resource = process_registry.get(params['payload']['resource_id'])
        record('calls.jsonl', {'resource': params['payload']['resource_id'], 'tool': params['payload']['tool'],
            'sequence': params['sequence'], 'exited': resource.exited if resource else None,
            'lifetime_live': bool(resource and hasattr(resource, '_managed_lifetime_task')
                and not resource._managed_lifetime_task.done())})
        if MODE == 'duplicate' and params['payload']['tool'] == 'terminal':
            duplicate = await original_request(connection, ref, params)
            record('duplicates.jsonl', {'first': result, 'duplicate': duplicate,
                'resource': params['payload']['resource_id'], 'sequence': params['sequence']})
        if MODE == 'lost-ack' and params['payload']['tool'] == 'terminal':
            await connection.transport._ws.close(code=1011)
    return result

async def attach(registry, session, worker):
    if MODE == 'before-attach':
        record('reserved.jsonl', {'resource': session.id})
        while not (HOME / 'attach.release').exists():
            await asyncio.sleep(.01)
    await original_attach(registry, session, worker)
    record('attached.jsonl', {'resource': session.id, 'exited': session.exited})

async def view(self, *args, **kwargs):
    snapshot = await original_view(self, *args, **kwargs)
    if MODE == 'profile' and (HOME / 'retire.request').exists() and not (HOME / 'retired.jsonl').exists():
        before = len(command_effects(self))
        stop_authority_work(self)
        try:
            await join_authority_work(self, timeout=10)
            outcome = 'settled'
        except Exception as exc:
            outcome = str(exc)
        record('retired.jsonl', {'before': before, 'after': len(command_effects(self)),
            'unknown': len(command_unknowns(self)), 'outcome': outcome})
    record('views.jsonl', {'effects': len(command_effects(self)), 'unknown': len(command_unknowns(self))})
    return snapshot

managed.configure_policy = configure
process.process_request = request
process.attach_lifetime = attach
SessionAuthority.attach = view
from gateway.run import main
main()
'''.replace('__HOME__', repr(str(home))).replace('__MODE__', repr(mode)))


@contextmanager
def _fixture(tmp_path, mode='normal'):
    home, user = tmp_path / 'state', tmp_path / 'user'
    home.mkdir(mode=0o700)
    user.mkdir()
    (user / '.hushlogin').touch()
    peer = ThreadingHTTPServer(('127.0.0.1', 0), LifecycleModel)
    peer.commands, peer.results = {}, []
    threading.Thread(target=peer.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{peer.server_port}/v1'
    (home / 'config.yaml').write_text(json.dumps({
        'gateway': {'multiplex_profiles': False, 'managed_workers': True},
        'model': {'provider': 'custom', 'default': 'fixture-model', 'base_url': url},
        'auxiliary': {'title_generation': {'enabled': False}}, 'approvals': {'mode': 'off'},
        'terminal': {'daemon_term_grace_seconds': .1}, 'platform_toolsets': {'cli': ['terminal']}}))
    env = {key: os.environ[key] for key in ('PATH', 'LANG', 'TZ') if key in os.environ}
    env.update(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(home), PYTHONPATH=str(ROOT),
               OPENAI_API_KEY='loopback-only', OPENAI_BASE_URL=url)
    launcher = tmp_path / 'lifecycle_owner.py'
    _launcher(launcher, home, mode)
    try:
        yield home, peer, url, env, launcher
    finally:
        for release in tmp_path.glob('*/release'):
            release.touch()
        peer.shutdown()
        peer.server_close()


def _body(tmp_path, name, *, escape=False):
    directory = tmp_path / name
    directory.mkdir()
    script = directory / 'body.py'
    script.write_text('''import os, pathlib, time
root = pathlib.Path(__file__).parent
with (root / 'starts').open('a') as stream:
    stream.write(str(os.getpid()) + '\\n')
if __ESCAPE__:
    child = os.fork()
    if child:
        print('PARENT_RETURNED', flush=True)
        raise SystemExit(0)
    os.setsid()
    null = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(null, fd)
(root / 'pid').write_text(str(os.getpid()))
(root / 'ready').touch()
while not (root / 'release').exists():
    time.sleep(.02)
(root / 'effect').write_text('finished')
print('FIRST_42', flush=True)
print('SECOND_42', flush=True)
raise SystemExit(7)
'''.replace('__ESCAPE__', repr(escape)))
    return directory, shlex.quote(sys.executable) + ' ' + shlex.quote(str(script))


async def _create(ws, home, url, name):
    created = await rpc(ws, 'session.create', request_id=name, source='cli', cwd=str(home),
                        model='fixture-model', provider='custom', base_url=url, api_key='loopback-only',
                        toolsets=['terminal'], ignore_rules=True)
    assert 'result' in created, created
    return created['result']['session_id']


async def _submit(ws, home, sid, text, request):
    submitted = await rpc(ws, 'prompt.submit', session_id=sid, input_id=request, text=text)
    assert 'result' in submitted, submitted
    await async_wait_until(lambda: _settled(home, request), timeout=45, what='managed native turn settlement')
    return (await rpc(ws, 'session.resume', session_id=sid))['result']


async def _start(ws, home, peer, url, directory, command, *, name='own', persist=False):
    peer.commands[name] = {'command': command, 'background': True, 'persist_on_release': persist}
    sid = await _create(ws, home, url, name)
    snapshot = await _submit(ws, home, sid, name, name + '-start')
    result = json.loads([message['content'] for message in snapshot['messages'] if message['role'] == 'tool'][-1])
    assert not result.get('error') and result.get('session_id'), (result, (home / 'first.log').read_text())
    await async_wait_until((directory / 'ready').exists, timeout=10, what='actual child readiness')
    worker = _rows(home / 'workers.jsonl')[-1]
    await async_wait_until(lambda: not _alive(worker['pid']), timeout=10, what='physical managed interpreter exit')
    assert _alive(int((directory / 'pid').read_text())), 'The same body must remain alive after worker exit'
    return sid, result['session_id'], worker


@pytest.mark.platforms('linux')
@pytest.mark.parametrize('mode', ['normal', 'duplicate'])
def test_physical_worker_exit_preserves_one_body_and_next_turn_output(tmp_path, mode):
    directory, command = _body(tmp_path, 'body')
    with _fixture(tmp_path, mode) as (home, peer, url, env, launcher):
        async def exercise(desc):
            async with websocket(home, desc) as ws:
                sid, resource, first_worker = await _start(ws, home, peer, url, directory, command)
                assert _rows(home / 'calls.jsonl')[0]['lifetime_live'], 'A native resource effect must outlive its model worker'
                (directory / 'release').touch()
                snapshot = await _submit(ws, home, sid, 'WAIT ' + resource, 'wait')
                waited = json.loads([m['content'] for m in snapshot['messages'] if m['role'] == 'tool'][-1])
                assert waited['status'] == 'exited' and waited['exit_code'] == 7, waited
                assert waited['output'] == 'FIRST_42\nSECOND_42\n', waited
                workers = _rows(home / 'workers.jsonl')
                assert len(workers) == 2 and workers[1]['pid'] != first_worker['pid'], workers
                assert workers[1]['physical'] == first_worker['physical']
                assert len((directory / 'starts').read_text().splitlines()) == 1
                if mode == 'duplicate':
                    duplicates = _rows(home / 'duplicates.jsonl')
                    assert len(duplicates) == 1 and duplicates[0]['resource'] == resource, duplicates
                    assert duplicates[0]['duplicate']['session_id'] == resource
        with daemon(ROOT, home, env, barrier=True, fixture=str(launcher)) as (_owner, desc):
            asyncio.run(exercise(desc))


@pytest.mark.platforms('linux')
@pytest.mark.parametrize('persist', [False, True])
def test_stop_after_worker_exit_reaps_escaped_body_and_preserves_other_conversation(tmp_path, persist):
    own, own_command = _body(tmp_path, 'own', escape=True)
    other, other_command = _body(tmp_path, 'other')
    with _fixture(tmp_path) as (home, peer, url, env, launcher):
        async def exercise(desc):
            async with websocket(home, desc) as ws:
                sid, resource, _worker = await _start(ws, home, peer, url, own, own_command, persist=persist)
                _other_sid, _other_resource, _other_worker = await _start(ws, home, peer, url, other, other_command, name='other')
                own_pid, other_pid = int((own / 'pid').read_text()), int((other / 'pid').read_text())
                listed = await rpc(ws, 'process.list', session_id=sid)
                assert resource in str(listed) and _alive(own_pid), listed
                snapshot = (await rpc(ws, 'session.resume', session_id=sid))['result']
                stopped = await rpc(ws, 'session.interrupt', session_id=sid, execution_generation=snapshot['execution_generation'])
                assert 'result' in stopped, (stopped, (home / 'first.log').read_text())
                assert not _alive(own_pid) and _alive(other_pid), stopped
                assert not (own / 'effect').exists()
                (other / 'release').touch()
                await async_wait_until((other / 'effect').exists, timeout=10, what='unrelated process actual effect')
                assert len((own / 'starts').read_text().splitlines()) == 1
        with daemon(ROOT, home, env, barrier=True, fixture=str(launcher)) as (_owner, desc):
            asyncio.run(exercise(desc))


@pytest.mark.platforms('linux')
def test_released_original_native_scope_refuses_before_background_effect(tmp_path):
    directory, command = _body(tmp_path, 'body')
    with _fixture(tmp_path, 'released') as (home, peer, url, env, launcher):
        async def exercise(desc):
            async with websocket(home, desc) as ws:
                peer.commands['released'] = {'command': command, 'background': True}
                sid = await _create(ws, home, url, 'released')
                assert 'result' in await rpc(ws, 'prompt.submit', session_id=sid, input_id='released-start', text='released')
                await async_wait_until(lambda: _status(home, 'released-start') == 'unknown', timeout=30,
                                       what='missing native authority fenced as unknown')
                snapshot = (await rpc(ws, 'session.resume', session_id=sid))['result']
                assert any(row['status'] == 'unknown' for row in snapshot['pending']), snapshot
                assert 'permission_denied' in (home / 'first.log').read_text()
                assert not (directory / 'starts').exists() and not (directory / 'effect').exists()
                assert not _rows(home / 'calls.jsonl')
        with daemon(ROOT, home, env, barrier=True, fixture=str(launcher)) as (_owner, desc):
            asyncio.run(exercise(desc))


@pytest.mark.platforms('linux')
def test_cold_owner_retains_persisted_custody_as_unknown_without_replay(tmp_path):
    directory, command = _body(tmp_path, 'body', escape=True)
    with _fixture(tmp_path) as (home, peer, url, env, launcher):
        saved = {}

        async def start(desc):
            async with websocket(home, desc) as ws:
                saved['sid'], saved['resource'], _worker = await _start(ws, home, peer, url, directory, command, persist=True)

        with daemon(ROOT, home, env, barrier=True, fixture=str(launcher)) as (_owner, desc):
            asyncio.run(start(desc))
            original_checkpoint = json.loads((home / 'processes.json').read_text())
            original = next(row for row in original_checkpoint if row['session_id'] == saved['resource'])
            assert original['custody_kind'] == 'linux_subreaper' and original['custody_pid']
        pid = int((directory / 'pid').read_text())
        assert _alive(pid) and not (directory / 'effect').exists()

        async def inspect(desc):
            async with websocket(home, desc) as ws:
                snapshot = await rpc(ws, 'session.resume', session_id=saved['sid'])
                assert 'result' in snapshot, snapshot
                listed = await rpc(ws, 'process.list', session_id=saved['sid'])
                assert saved['resource'] in str(listed) and 'reconciliation' in str(listed).lower(), listed
                stopped = await rpc(ws, 'session.interrupt', session_id=saved['sid'],
                                    execution_generation=snapshot['result']['execution_generation'])
                assert stopped.get('error', {}).get('message') == 'unknown_execution', stopped
                assert _alive(pid), 'A cold owner cannot claim or kill an unverified controller'
                checkpoint = json.loads((home / 'processes.json').read_text())
                record = next(row for row in checkpoint if row['session_id'] == saved['resource'])
                assert record['reconciliation_required'] and record['custody_pid'] == original['custody_pid'], record
                assert len((directory / 'starts').read_text().splitlines()) == 1
                (directory / 'release').touch()
                await async_wait_until(lambda: not _alive(pid), timeout=10, what='naturally released original child')
        with daemon(ROOT, home, env, barrier=False) as (_owner, desc):
            asyncio.run(inspect(desc))


@pytest.mark.platforms('linux')
def test_known_no_start_before_lifetime_attachment_does_not_fork_or_keep_an_effect(tmp_path):
    directory, command = _body(tmp_path, 'body')
    with _fixture(tmp_path, 'before-attach') as (home, peer, url, env, launcher):
        async def exercise(desc):
            async with websocket(home, desc) as ws:
                peer.commands['held'] = {'command': command, 'background': True}
                sid = await _create(ws, home, url, 'held')
                assert 'result' in await rpc(ws, 'prompt.submit', session_id=sid, input_id='held-start', text='held')
                await async_wait_until(lambda: _rows(home / 'reserved.jsonl'), timeout=20, what='real reserved descriptor before attachment')
                listed = await rpc(ws, 'process.list', session_id=sid)
                resource = _rows(home / 'reserved.jsonl')[0]['resource']
                assert resource in str(listed), listed
                stopped = await rpc(ws, 'process.stop', session_id=sid)
                assert 'result' in stopped and not (directory / 'starts').exists(), stopped
                (home / 'attach.release').touch()
                await async_wait_until(lambda: _settled(home, 'held-start'), timeout=30, what='NoStart parent settlement')
                assert not (directory / 'starts').exists() and not (directory / 'effect').exists()
                assert _rows(home / 'attached.jsonl') == [{'resource': resource, 'exited': True}]
                checkpoint = json.loads((home / 'processes.json').read_text())
                assert not any(row['session_id'] == resource for row in checkpoint), checkpoint
        with daemon(ROOT, home, env, barrier=True, fixture=str(launcher)) as (_owner, desc):
            asyncio.run(exercise(desc))


@pytest.mark.platforms('linux')
def test_lost_actual_effect_reply_fences_the_turn_and_keeps_one_owned_resource(tmp_path):
    directory, command = _body(tmp_path, 'body', escape=True)
    with _fixture(tmp_path, 'lost-ack') as (home, peer, url, env, launcher):
        async def exercise(desc):
            async with websocket(home, desc) as ws:
                peer.commands['lost'] = {'command': command, 'background': True}
                sid = await _create(ws, home, url, 'lost')
                assert 'result' in await rpc(ws, 'prompt.submit', session_id=sid, input_id='lost-start', text='lost')
                await async_wait_until(lambda: _status(home, 'lost-start') == 'unknown', timeout=35,
                                       what='actual sent effect reply loss fences managed turn')
                await async_wait_until((directory / 'ready').exists, timeout=10, what='actual escaped resource after lost receipt')
                calls = _rows(home / 'calls.jsonl')
                assert len(calls) == 1 and len((directory / 'starts').read_text().splitlines()) == 1, calls
                pid = int((directory / 'pid').read_text())
                assert _alive(pid) and not (directory / 'effect').exists()
                listed = await rpc(ws, 'process.list', session_id=sid)
                assert calls[0]['resource'] in str(listed), listed
                checkpoint = json.loads((home / 'processes.json').read_text())
                assert sum(row['session_id'] == calls[0]['resource'] for row in checkpoint) == 1
                snapshot = (await rpc(ws, 'session.resume', session_id=sid))['result']
                stopped = await rpc(ws, 'session.interrupt', session_id=sid, execution_generation=snapshot['execution_generation'])
                assert 'result' in stopped and not _alive(pid), (stopped, (home / 'first.log').read_text())
                assert not (directory / 'effect').exists()
        with daemon(ROOT, home, env, barrier=True, fixture=str(launcher)) as (_owner, desc):
            asyncio.run(exercise(desc))


@pytest.mark.platforms('linux')
@pytest.mark.parametrize('persist', [False, True])
def test_profile_retirement_keeps_native_resource_ownership_and_persistence_truthful(tmp_path, persist):
    directory, command = _body(tmp_path, 'body', escape=True)
    with _fixture(tmp_path, 'profile') as (home, peer, url, env, launcher):
        async def exercise(desc):
            async with websocket(home, desc) as ws:
                sid, resource, _worker = await _start(ws, home, peer, url, directory, command, persist=persist)
                pid = int((directory / 'pid').read_text())
                (home / 'retire.request').touch()
                snapshot = await rpc(ws, 'session.resume', session_id=sid)
                assert 'result' in snapshot, (snapshot, (home / 'first.log').read_text())
                retirement, = _rows(home / 'retired.jsonl')
                assert retirement['before'] > 0 and retirement['after'] == 0, retirement
                if persist:
                    assert 'unknown_execution' in retirement['outcome'] and retirement['unknown'] == 1, retirement
                    assert _alive(pid) and not (directory / 'effect').exists()
                    checkpoint = json.loads((home / 'processes.json').read_text())
                    record, = [row for row in checkpoint if row['session_id'] == resource]
                    assert record['reconciliation_required'] and record['persist_on_release'], record
                    stopped = await rpc(ws, 'session.interrupt', session_id=sid,
                                        execution_generation=snapshot['result']['execution_generation'])
                    assert 'result' in stopped and not _alive(pid), (stopped, (home / 'first.log').read_text())
                else:
                    assert retirement['outcome'] == 'settled' and retirement['unknown'] == 0, retirement
                    assert not _alive(pid)
                assert not (directory / 'effect').exists()
        with daemon(ROOT, home, env, barrier=True, fixture=str(launcher)) as (_owner, desc):
            asyncio.run(exercise(desc))


@pytest.mark.platforms('linux')
@pytest.mark.parametrize('invalid', [{'force': True}, {'timeout': 'invalid'}])
def test_rejected_public_arguments_leave_known_no_start_and_no_resource_lease(tmp_path, invalid):
    directory, command = _body(tmp_path, 'body')
    with _fixture(tmp_path) as (home, peer, url, env, launcher):
        async def exercise(desc):
            async with websocket(home, desc) as ws:
                peer.commands['invalid'] = {'command': command, 'background': True, **invalid}
                sid = await _create(ws, home, url, 'invalid')
                snapshot = await _submit(ws, home, sid, 'invalid', 'invalid-start')
                tools = [message for message in snapshot['messages'] if message['role'] == 'tool']
                assert tools and 'invalid_params' in str(tools), (tools, (home / 'first.log').read_text())
                assert not (directory / 'starts').exists() and not (directory / 'effect').exists()
                assert _rows(home / 'views.jsonl')[-1] == {'effects': 0, 'unknown': 0}
                assert json.loads((home / 'processes.json').read_text()) == []
        with daemon(ROOT, home, env, barrier=True, fixture=str(launcher)) as (_owner, desc):
            asyncio.run(exercise(desc))
