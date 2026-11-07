"""Failed delivery/revalidation must settle the real unlimited private worker waiter."""
import asyncio
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest


_CHILD = '''
import sys
from agent.managed_worker import WorkerChannel, WorkerControls, _INTERRUPTED
channel = WorkerChannel(sys.stdout.buffer)
controls = WorkerControls(channel, 'private-worker-route')
result = controls._ask('Choose a color', ['Blue', 'Green'], False)
channel.send('private_waiter_result', cancelled=result is _INTERRUPTED, answered=isinstance(result, str), stopped=controls.stopped.is_set())
'''


async def _check_withdrawal(tmp_path, monkeypatch, failure, fixture):
    from gateway.platforms.base import SendResult
    from gateway.session_contract import SessionActor, Submission
    from gateway.session_kanban import build_kanban_policy
    from gateway.session_kanban_prompts import bind_consumer, deliver, retire_worker
    from gateway.session_local import create_local_session
    from gateway.session_managed_worker import ManagedWorker
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing
    from hermes_state_runtime import claim_session_input
    from tools import clarify_gateway as clarify

    process = worker = None
    try:
        home = fixture.home
        monkeypatch.setenv('HERMES_HOME', str(home))
        monkeypatch.setenv('HERMES_KANBAN_HOME', str(home))
        monkeypatch.setattr('hermes_cli.profiles.resolve_profile_env', lambda name: str(home))
        (home / 'config.yaml').write_text('agent:\n  clarify_timeout: 0\n')
        if not hasattr(fixture, 'running'):
            await fixture.start()
            kb.create_board('owned')
        with connect_closing(board='owned') as conn:
            tid = kb.create_task(conn, title='Private question', assignee='default',
                workspace_kind='dir', workspace_path=str(home))
            task = kb.claim_task(conn, tid)
        fixture.adapter.kanban_prompt_consumer = lambda card: fixture.running['admission_id']
        actor = SessionActor('owner', str(home), frozenset({'session:create', 'session:submit'}), 'test')
        connection = SimpleNamespace(native_owner=True, actor=actor, authority=fixture.authority)
        params = dict(board='owned', task_id=tid, run_id=task.current_run_id, claim_lock=task.claim_lock)
        policy, secrets = build_kanban_policy(connection, params, {'agent': {'clarify_timeout': 0},
            'model': {'provider': 'custom', 'default': 'test'}, 'platform_toolsets': {'cli': ['terminal']}})
        policy = await bind_consumer(fixture.authority, policy)
        request_id = 'private-waiter-' + failure
        ref = create_local_session(fixture.authority, actor, {'request_id': request_id},
                                   trusted_policy=policy, trusted_secrets=secrets)
        await fixture.authority.submit(actor, Submission(request_id, ref,
            {'text': 'Ask the bounded question', 'finite': True}, 'queue'))
        row = claim_session_input(fixture.authority.db, epoch=fixture.authority.epoch, session_id=ref.session_id)
        fixture.authority.executing[ref.session_id] = row['generation']

        source = Path(__file__).resolve().parents[2]
        environment = {k: os.environ[k] for k in ('PATH', 'LANG', 'TZ') if k in os.environ}
        environment.update(HERMES_HOME=str(home), HOME=str(home), PYTHONPATH=str(source))
        process = await asyncio.to_thread(subprocess.Popen, [sys.executable, '-u', '-c', _CHILD], env=environment,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        worker = ManagedWorker(process)
        worker.writer.start()
        frame = await worker.next_frame(5, ack=2)
        assert frame['type'] == 'clarify'
        async def send(*args, **kwargs):
            if failure == 'adapter-exception':
                raise RuntimeError('fixture receiver unavailable')
            if failure == 'revoked-after-answer':
                assert clarify.resolve_gateway_clarify(frame['prompt_id'], 'Blue')
                fixture.allowed.remove(fixture.source.user_id)
                return SendResult(success=True)
            return SendResult(success=False, retryable=False, error='fixture terminal refusal')
        fixture.adapter.send_clarify = send
        if failure == 'adapter-exception':
            with pytest.raises(RuntimeError, match='receiver unavailable'):
                await deliver(fixture.authority, ref, row, policy, worker, frame)
        else:
            await deliver(fixture.authority, ref, row, policy, worker, frame)
        async with asyncio.timeout(5):
            while (settled := await worker.next_frame(5, ack=2))['type'] != 'private_waiter_result':
                assert settled['type'] == 'prompt_settled'
        assert settled == {'type': 'private_waiter_result', 'cancelled': True, 'answered': False, 'stopped': False}
        assert clarify.get_pending(frame['prompt_id']) is None
        await asyncio.to_thread(process.wait, timeout=5)
    finally:
        if worker is not None:
            await retire_worker(worker)
            await asyncio.to_thread(worker.close)
        if process is not None and process.poll() is None:
            process.kill()
            await asyncio.to_thread(process.wait, timeout=5)


@pytest.mark.asyncio
async def test_owner_withdrawal_cancels_unlimited_private_waiter(tmp_path, monkeypatch):
    from tests.gateway.test_native_inbound_retry import NativeInboundRetryTests
    fixture = NativeInboundRetryTests()
    await fixture.asyncSetUp()
    try:
        for failure in ('terminal-send', 'adapter-exception', 'revoked-after-answer'):
            await _check_withdrawal(tmp_path, monkeypatch, failure, fixture)
    finally:
        fixture.doCleanups()
