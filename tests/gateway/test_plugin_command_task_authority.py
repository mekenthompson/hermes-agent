"""Registered plugin commands retain authenticated native authority and Stop fences."""
import asyncio
from dataclasses import asdict
import json
import os
import shlex
import signal
import threading
from types import SimpleNamespace

import pytest

from tests.gateway.test_cli_plugin_command_in_gateway import _plugin_home, _Wire
from tests.gateway.fixtures.local_recovery_probe import daemon
from tests.waiting import async_wait_until, wait_until


_PLUGIN = '''import json
from dataclasses import asdict
from pathlib import Path


def register(ctx):
    def barrier():
        Path(ENTERED_PATH).touch()
        with open(GATE_PATH) as gate:
            gate.read()

    def effect(arg):
        from tools import terminal_tool
        from tools.approval_context import current_approval_task_context, approval_task_revoked
        evidence = current_approval_task_context()
        before = asdict(evidence) if evidence is not None else None
        original = terminal_tool._acquire_env
        def setup(*args, **kwargs):
            result = original(*args, **kwargs)
            barrier()
            return result
        if arg == 'handler':
            barrier()
        if arg == 'setup':
            terminal_tool._acquire_env = setup
        try:
            reply = ctx.dispatch_tool('terminal', {'command': EFFECT_COMMAND, 'background': True})
        finally:
            terminal_tool._acquire_env = original
        return json.dumps({'before': before, 'revoked': approval_task_revoked(), 'reply': json.loads(reply)})

    def wait(sid):
        from tools.process_registry import process_registry
        return json.dumps(process_registry.wait(sid, timeout=10))
    ctx.register_command('authority-effect', effect, description='Run the owned test effect')
    ctx.register_command('authority-wait', wait, description='Read its result')
'''


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("boundary, stopped", [("handler", False), ("handler", True), ("setup", True)])
def test_websocket_plugin_command_background_authority_and_stop(tmp_path, boundary, stopped):
    """The real socket, registered command, registry tool and Popen share one native owner."""
    effect, entered, gate = tmp_path / "effect", tmp_path / "entered", tmp_path / "gate"
    os.mkfifo(gate)
    command = "printf x >> " + shlex.quote(str(effect))
    plugin = (_PLUGIN.replace('ENTERED_PATH', repr(str(entered))).replace('GATE_PATH', repr(str(gate)))
              .replace('EFFECT_COMMAND', repr(command)))
    with _plugin_home(tmp_path, plugin) as (root, home, env):
        with daemon(root, home, env, barrier=False) as (proc, desc):
            caller, controller = _Wire(home, desc), _Wire(home, desc)
            result = {}
            worker = None
            try:
                created = controller.call('session.create', request_id='plugin-authority', source='cli',
                                          toolsets=[], cwd=str(tmp_path))
                sid = created['session_id']
                caller.call('session.resume', session_id=sid)

                def invoke():
                    try:
                        result['value'] = caller.call('slash.exec', session_id=sid,
                                                      command='/authority-effect ' + boundary)
                    except Exception as exc:
                        result['error'] = exc

                worker = threading.Thread(target=invoke, daemon=True)
                worker.start()
                wait_until(entered.exists, timeout=20, what='native command reaching its effect boundary')
                if stopped:
                    controller.call('session.interrupt', session_id=sid,
                                    execution_generation=created['execution_generation'])
                with gate.open('w') as release:
                    release.write('continue')
                worker.join(20)
                assert not worker.is_alive() and 'error' not in result, result
                response = json.loads(result['value']['output'])
                evidence, reply = response['before'], response['reply']
                assert evidence is not None, response
                assert evidence['identity'] and evidence['task_id'].startswith('command:'), response
                assert evidence['profile'] == str(home) and evidence['session'], response
                assert evidence['text'] == '/authority-effect ' + boundary
                if stopped:
                    assert response['revoked'] is True
                    assert 'stopped' in reply.get('error', '').lower(), response
                    assert not effect.exists()
                else:
                    assert response['revoked'] is False and not reply.get('error'), response
                    final = controller.call('slash.exec', session_id=sid,
                                            command='/authority-wait ' + reply['session_id'])
                    assert json.loads(final['output'])['exit_code'] == 0, final
                    assert effect.read_text() == 'x'
            finally:
                caller.close()
                controller.close()
                if worker is not None:
                    worker.join(1)
                proc.send_signal(signal.SIGINT)
                proc.wait(timeout=20)


async def _discord_runtime(tmp_path, monkeypatch):
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.platforms.base import SendResult
    from gateway.platforms.event import MessageEvent
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    from hermes_cli.plugins import PluginContext, PluginManifest, get_plugin_manager
    from hermes_state import AsyncSessionDB, SessionDB
    from model_tools import discover_builtin_tools
    from plugins.platforms.discord.adapter import DiscordAdapter
    from tools import process_registry
    from tools.approval_context import current_approval_task_context

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('TERMINAL_CWD', str(tmp_path))
    monkeypatch.setenv('DISCORD_ALLOWED_USERS', '42')
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / 'sessions'))
    db = SessionDB(tmp_path / 'state.db')
    runner.session_store._db = db
    runner._session_db = AsyncSessionDB(db)
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token='fixture', typing_indicator=False))
    replies, turns = [], []

    async def send(chat_id, content, reply_to=None, metadata=None):
        replies.append(content)
        return SendResult(success=True, message_id='sent')

    async def answer(**kwargs):
        turns.append(current_approval_task_context())
        return {'final_response': 'ready', 'messages': []}

    monkeypatch.setattr(adapter, 'send', send)
    monkeypatch.setattr(runner, '_run_agent', answer)
    adapter.gateway_runner = runner
    runner.adapters = {Platform.DISCORD: adapter}
    authority = await initialize_session_authority(runner, profile_id=str(tmp_path), instance_id='owner', db=db)
    runner._wire_adapter_handlers(adapter)
    source = adapter.build_source(chat_id='42', chat_type='dm', user_id='42')
    await adapter.handle_message(MessageEvent(text='Prepare the workspace', source=source, message_id='warm'))
    key = runner.session_store._generate_session_key(source)
    await async_wait_until(lambda: runner.session_store.lookup_by_session_key(key) is not None
                           and not adapter._background_tasks, timeout=15, what='native human session')
    registry = process_registry.ProcessRegistry()
    monkeypatch.setattr(process_registry, 'process_registry', registry)
    manager = get_plugin_manager()
    manager.discover_and_load()
    discover_builtin_tools()
    context = PluginContext(PluginManifest(name='authority-plugin'), manager)
    return SimpleNamespace(runner=runner, adapter=adapter, authority=authority, source=source, key=key,
                           context=context, registry=registry, replies=replies, turns=turns, db=db)


@pytest.mark.asyncio
@pytest.mark.platforms('posix')
@pytest.mark.parametrize('stopped', [False, True])
async def test_private_discord_callback_plugin_terminal_and_stop(tmp_path, monkeypatch, stopped):
    from gateway.platforms.event import MessageEvent
    from tools import terminal_tool
    from tools.approval_context import current_approval_task_context

    runtime = await _discord_runtime(tmp_path, monkeypatch)
    effect = tmp_path / 'native-effect'
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    observations = []
    original = terminal_tool._acquire_env

    def setup(*args, **kwargs):
        result = original(*args, **kwargs)
        entered.set()
        assert release.wait(15)
        return result

    def handler(arg):
        evidence = current_approval_task_context()
        result = runtime.context.dispatch_tool('terminal', {
            'command': 'printf x >> ' + shlex.quote(str(effect)), 'background': True})
        observations.append((asdict(evidence) if evidence is not None else None, json.loads(result)))
        finished.set()
        return result

    registration = runtime.context.register_command('authority-effect', handler)
    monkeypatch.setattr(terminal_tool, '_acquire_env', setup)
    try:
        await runtime.adapter.handle_message(MessageEvent(text='/authority-effect', source=runtime.source, message_id='effect'))
        assert await asyncio.to_thread(entered.wait, 10)
        if stopped:
            old_epoch = runtime.authority.route_epoch(runtime.key)
            await runtime.adapter.handle_message(MessageEvent(text='/stop', source=runtime.source, message_id='stop'))
            await async_wait_until(lambda: runtime.authority.route_epoch(runtime.key) > old_epoch,
                                   timeout=10, what='native Stop fencing the command')
        release.set()
        assert await asyncio.to_thread(finished.wait, 15)
        evidence, reply = observations[0]
        assert evidence is not None, reply
        assert evidence['identity'].startswith('messaging:') and evidence['task_id'].startswith('command:')
        source = runtime.source
        assert evidence['identity'] == 'messaging:' + json.dumps(
            [source.profile, source.platform.value, source.chat_id, source.thread_id, source.user_id],
            separators=(',', ':'))
        assert evidence['profile'] == str(tmp_path) and evidence['text'] == '/authority-effect'
        assert evidence['session'] == runtime.key
        if stopped:
            assert 'stopped' in reply.get('error', '').lower(), reply
            assert not effect.exists() and not runtime.registry.list_sessions()
        else:
            session = runtime.registry.get(reply['session_id'])
            assert await asyncio.to_thread(session._completion_event.wait, 10)
            assert session.exit_code == 0 and effect.read_text() == 'x'
    finally:
        release.set()
        registration.dispose()
        runtime.registry.kill_all(source='test')
        await async_wait_until(lambda: not runtime.adapter._background_tasks, timeout=10, what='command settlement')
        runtime.db.close()


@pytest.mark.asyncio
async def test_unverified_plugin_command_inputs_do_not_gain_authority(tmp_path, monkeypatch):
    from gateway.config import Platform
    from gateway.platforms.event import MessageEvent
    from gateway.session import SessionSource

    runtime = await _discord_runtime(tmp_path, monkeypatch)
    calls = []
    registration = runtime.context.register_command('authority-effect', lambda arg: calls.append(arg) or 'ran')
    try:
        foreign = runtime.adapter.build_source(chat_id='43', chat_type='dm', user_id='43')
        await runtime.adapter.handle_message(MessageEvent(text='/authority-effect', source=foreign, message_id='foreign'))
        await async_wait_until(lambda: not runtime.adapter._background_tasks, timeout=10, what='foreign callback refusal')
        assert not calls
        event = MessageEvent(text='/authority-effect', source=runtime.source, message_id='notice',
                             internal=True, allow_gateway_control=False)
        with pytest.raises(PermissionError, match='local platform'):
            await runtime.runner.dispatch_internal_plugin_event(event)
        local = SessionSource(platform=Platform.LOCAL, chat_id='plugin-notice', chat_type='dm', profile='default')
        notice = MessageEvent(text='/authority-effect', source=local, message_id='local-notice',
                              internal=True, allow_gateway_control=False)
        await runtime.runner.prepare_internal_plugin_session(notice)
        await runtime.runner.dispatch_internal_plugin_event(notice)
        await async_wait_until(lambda: not runtime.adapter._background_tasks, timeout=10, what='internal notice settlement')
        assert not calls and runtime.turns[-1] is None
    finally:
        registration.dispose()
        runtime.db.close()
