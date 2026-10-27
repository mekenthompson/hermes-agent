"""Native messaging commits identity before busy effects; no gateway/model bootstrap.

The runner settings, agent and transport below are finite fixtures. Admission,
epoch recovery, the production inbound method and transfer settlement use Core.
"""
import asyncio
from datetime import UTC, datetime
from types import MethodType, SimpleNamespace
import sys
import unittest

import pytest


@pytest.fixture(autouse=True)
def no_bootstrap(monkeypatch):
    class DenyBootstrap:
        def find_spec(self, fullname, path=None, target=None):
            if fullname in {'gateway.run', 'run_agent'}:
                raise AssertionError('native seam must not bootstrap ' + fullname)
    monkeypatch.setattr(sys, 'meta_path', [DenyBootstrap(), *sys.meta_path])
    violations = []
    def calls(frame, event, arg):
        if event != 'call':
            return
        module, name = frame.f_globals.get('__name__', ''), frame.f_code.co_name
        if (module == 'hermes_cli.auth' and name in {'_load_auth_store', '_load_global_auth_store',
                '_save_auth_store', '_auth_store_lock'}
                or module == 'agent.credential_pool' and name in {'load_pool', 'select', '_refresh_entry', '_persist'}
                or module == 'gateway.session_local_inventory' and name in {'snapshot_info', 'session_inventory'}
                or module.startswith(('openai.resources.', 'anthropic.resources.')) and name in {'create', 'stream'}):
            violations.append(module + '.' + name)
            raise AssertionError('forbidden native seam activation: ' + violations[-1])
    previous = sys.getprofile()
    sys.setprofile(calls)
    yield
    sys.setprofile(previous)
    assert not violations


class FiniteAgent:
    """Only an in-memory model endpoint; never executes tools or contacts a provider."""
    _supports_active_turn_redirect = True
    def __init__(self):
        self.effects, self.pending = [], set()
        self.failure = None
    def steer(self, text, *, ticket=None):
        if self.failure == 'refuse':
            return False
        self.effects.append(('steer', text, ticket))
        self.pending.add(ticket)
        if self.failure == 'unknown':
            raise RuntimeError('finite endpoint lost its acknowledgement after enqueue')
        return True
    def withdraw_steer(self, ticket):
        if ticket not in self.pending:
            return False
        self.pending.remove(ticket)
        return True
    def redirect(self, text):
        self.effects.append(('redirect', text))
        return True
    def interrupt(self, text):
        self.effects.append(('interrupt', text))


class NativeInboundRetryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.assertNotIn('gateway.run', sys.modules)
        self.assertNotIn('run_agent', sys.modules)
        import tempfile
        from pathlib import Path
        from gateway.config import GatewayConfig, Platform
        from gateway.session import SessionStore, SessionSource
        from gateway.session_authority import initialize_session_authority
        from gateway.run_busy import GatewayBusySessionMixin
        from hermes_constants import set_hermes_home_override, reset_hermes_home_override
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.addCleanup(reset_hermes_home_override, set_hermes_home_override(self.home))
        self.store = SessionStore(self.home / 'sessions', GatewayConfig())
        self.addCleanup(self.store._db.close)
        self.runner = SimpleNamespace(session_store=self.store, _session_db=self.store._db, adapters={},
            _draining=False, config=GatewayConfig(), _cached_agent_for=lambda key: None,
            _peek_session_state=lambda key: None, _primary_profile_name='default')
        self.allowed = {'fixture-author', 'other-author'}
        self.runner._adapter_for_source = lambda source: self.runner.adapters.get(source.platform)
        self.runner._adapter_flag = lambda *args: False
        self.runner._adapter_profile_for_source = lambda source: None
        self.runner._is_user_authorized_for_source = lambda source, **kwargs: source.user_id in self.allowed
        self.authority = await initialize_session_authority(self.runner, profile_id=str(self.home), instance_id='first')
        self.scheduled = []
        self.authority._schedule = lambda ref: self.scheduled.append(ref)
        self.source = SessionSource(platform=Platform.TELEGRAM, chat_id='fixture-chat', user_id='fixture-author')
        self.route = self.store._generate_session_key(self.source)
        self.receipts, self.receipt_events = [], {}
        async def admitted(event, receipt):
            self.receipts.append((event.message_id, receipt))
            self.receipt_events.setdefault(event.message_id, asyncio.Event()).set()
        self.adapter = SimpleNamespace(on_message_admitted=admitted, requires_durable_input_ack=True)
        self.runner.adapters[self.source.platform] = self.adapter
        self.runner._intake_adapter_for = lambda source: self.runner.adapters.get(source.platform)
        self.runner._delivery_adapter_for = lambda source: self.adapter
        self.mode = 'steer'
        self.runner._effective_busy_input_mode = lambda source: self.mode
        self.runner._effective_busy_text_mode = lambda source: 'interrupt'
        self.runner._agent_has_active_subagents = lambda agent: False
        async def no_compression(route):
            return False
        self.runner._session_has_compression_in_flight = no_compression
        self.runner._steer_text_with_origin = lambda text, event: text
        self.runner._steer_active_subagents = GatewayBusySessionMixin._steer_active_subagents
        self.runner._steer_running_agent = MethodType(GatewayBusySessionMixin._steer_running_agent, self.runner)
        self.runner._fold_into_running_turn = lambda *args: None
        self.runner._reanchor_redirected_turn = lambda *args: None
        self.acks = []
        async def ack(*args, **kwargs):
            self.acks.append(args[0].message_id)
        self.runner._busy_interrupt_and_ack = ack
        async def no_consume(*args):
            return False, None
        self.runner._hm_post_admission_consume = no_consume
        self.runner._is_session_running = lambda route: True
        self.old_callbacks = []
        async def legacy_callback(event, *args):
            self.old_callbacks.append(event.message_id)
            return True
        self.runner._route_plaintext_approval_while_busy = legacy_callback
        self.runner._hm_busy_preempt = legacy_callback
        self.runner._plaintext_approval_words = lambda: {'yes': ('approve', '')}
        async def approval_confirmation(*args):
            return None  # finite transport presentation only
        self.runner._deliver_approval_confirmation = approval_confirmation

    def event(self, message_id, text='bounded instruction', *, source=None, control=False):
        from gateway.platforms.event import MessageEvent
        return MessageEvent(text=text, source=source or self.source, message_id=message_id,
            timestamp=datetime(2026, 1, 1, tzinfo=UTC), allow_gateway_control=control)

    async def start(self, message_id='opening'):
        from hermes_state_runtime import claim_session_input
        receipt = await self.authority.admit_native(self.event(message_id))
        row = claim_session_input(self.authority.db, epoch=self.authority.epoch, session_id=receipt.ref.session_id)
        self.ref, self.running = receipt.ref, row
        self.agent = FiniteAgent()
        self.authority.executing[self.ref.session_id] = row['generation']
        self.authority.adopt_agent(self.ref.session_id, row['generation'], self.agent)
        return row

    def settle(self, outcome='completed'):
        from gateway.session_queued_controls import steer_transfers
        from hermes_state_runtime import settle_session_input
        with self.authority.sessions[self.ref.session_id].event_stream.lock:
            transfers = steer_transfers(self.authority, self.ref.session_id, self.running['generation'])
        return settle_session_input(self.authority.db, epoch=self.authority.epoch,
            admission_id=self.running['admission_id'], generation=self.running['generation'],
            outcome=outcome, _transfers=transfers)

    async def receive(self, event):
        from gateway.run_inbound import GatewayInboundMixin
        return await GatewayInboundMixin._hm_authority_admission(
            self.runner, event, event.source, self.store._generate_session_key(event.source), False)

    def row(self, message_id):
        from hermes_state_runtime import list_session_admissions
        return next(row for row in list_session_admissions(self.authority.db,
            session_id=self.ref.session_id, pending_only=False) if row['request_id'] == message_id)

    async def accepted(self, message_id):
        await asyncio.wait_for(self.receipt_events.setdefault(message_id, asyncio.Event()).wait(), 5)

    async def cancel_observer(self, task):
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_terminal_redelivery_after_restart_does_not_preempt_later_turn(self):
        from gateway.session_authority import initialize_session_authority
        for outcome in ('completed', 'interrupted'):
            with self.subTest(outcome=outcome):
                await self.start('original-' + outcome)
                self.settle(outcome)
                self.authority = await initialize_session_authority(self.runner, profile_id=str(self.home), instance_id=outcome)
                self.authority._schedule = lambda ref: self.scheduled.append(ref)
                await self.start('later-' + outcome)
                self.old_callbacks.clear()
                try:
                    handled, reply = await self.receive(self.event('original-' + outcome))
                    self.assertTrue(handled)
                    self.assertIsNone(reply)
                    self.assertEqual(self.agent.effects, [])
                    self.assertEqual(self.old_callbacks, [])
                finally:
                    self.settle()

    async def test_concurrent_first_delivery_has_one_transfer_and_consumption_receipt(self):
        await self.start()
        results = await asyncio.gather(*(self.receive(self.event('followup')) for _ in range(2)))
        self.assertTrue(all(result == (True, None) for result in results))
        self.assertEqual(len(self.agent.effects), 1)
        transferred = self.row('followup')
        self.assertEqual(transferred['outcome'], 'steering')
        self.assertTrue(any(mid == 'followup' for mid, _receipt in self.receipts))
        self.agent.pending.clear()  # finite model endpoint consumed the ticket
        self.settle()
        self.assertEqual(self.row('followup')['outcome'], 'steered')

    async def test_unconsumed_ticket_returns_to_fifo_and_interrupted_consumption_never_replays(self):
        await self.start()
        await self.receive(self.event('unconsumed'))
        self.settle()
        self.assertEqual(self.row('unconsumed')['status'], 'queued')

    async def test_child_consumption_prevents_replaying_parent_fanout(self):
        await self.start()
        child = FiniteAgent()
        self.agent._active_children = [child]
        await self.receive(self.event('fanout'))
        ticket = self.row('fanout')['admission_id']
        self.assertEqual(self.agent.effects[0][2], ticket)
        self.assertEqual(child.effects[0][2], ticket)
        child.pending.clear()  # child consumed; parent has not consumed yet
        self.settle()
        self.assertEqual(self.row('fanout')['outcome'], 'steered')
        self.assertNotIn(ticket, self.agent.pending)
        await self.receive(self.event('fanout'))
        self.assertEqual(len(child.effects), 1)

    async def test_all_fanout_tickets_unconsumed_return_one_input_to_fifo(self):
        await self.start()
        child = FiniteAgent()
        self.agent._active_children = [child]
        await self.receive(self.event('unused-fanout'))
        ticket = self.row('unused-fanout')['admission_id']
        self.settle()
        self.assertEqual(self.row('unused-fanout')['status'], 'queued')
        self.assertNotIn(ticket, self.agent.pending)
        self.assertNotIn(ticket, child.pending)

    async def test_attempted_apply_failure_is_interrupted_not_queued(self):
        await self.start()
        self.agent.failure = 'unknown'
        with self.assertRaisesRegex(RuntimeError, 'lost its acknowledgement'):
            await self.receive(self.event('uncertain'))
        self.assertEqual(self.row('uncertain')['outcome'], 'interrupted')
        await self.receive(self.event('uncertain'))
        self.assertEqual(len(self.agent.effects), 1)
        self.settle()
        self.assertEqual(self.row('uncertain')['outcome'], 'interrupted')

    async def test_native_transfer_consumed_then_old_turn_interrupted_stays_history(self):
        await self.start()
        await self.receive(self.event('consumed-before-stop'))
        self.agent.pending.clear()
        self.settle('interrupted')
        self.assertEqual(self.row('consumed-before-stop')['outcome'], 'interrupted')
        await self.receive(self.event('consumed-before-stop'))
        self.assertEqual(len(self.agent.effects), 1)

    async def test_queued_duplicate_does_not_preempt_and_receipt_is_not_completion(self):
        await self.start()
        await self.authority.admit_native(self.event('queued'))
        observer = asyncio.create_task(self.receive(self.event('queued')))
        await self.accepted('queued')
        # The first direct admission also called the hook; wait until retry's
        # hook fires, without using sleep or inventing a native completion.
        from tests.waiting import async_wait_until
        await async_wait_until(lambda: sum(mid == 'queued' for mid, _ in self.receipts) == 2)
        self.assertEqual(self.row('queued')['status'], 'queued')
        self.assertFalse(observer.done())
        self.assertEqual(self.agent.effects, [])
        await self.cancel_observer(observer)

    async def test_unknown_started_input_after_restart_is_not_replayed(self):
        from gateway.session_authority import initialize_session_authority
        await self.start('lost-started')
        self.authority = await initialize_session_authority(self.runner, profile_id=str(self.home), instance_id='recovered')
        self.authority._schedule = lambda ref: self.scheduled.append(ref)
        handled, notice = await self.receive(self.event('lost-started'))
        self.assertTrue(handled)
        self.assertIn('paused', notice)
        self.assertEqual(self.row('lost-started')['status'], 'unknown')
        self.assertEqual(self.agent.effects, [])
        later = asyncio.create_task(self.receive(self.event('after-unknown')))
        await self.accepted('after-unknown')
        self.assertEqual(self.row('after-unknown')['status'], 'queued')
        self.assertEqual(self.agent.effects, [])
        await self.cancel_observer(later)

    async def test_redirect_and_interrupt_are_applied_only_for_first_committed_input(self):
        await self.start()
        self.mode = 'interrupt'
        await self.receive(self.event('redirected'))
        await self.receive(self.event('redirected'))
        self.assertEqual(self.agent.effects, [('redirect', 'bounded instruction')])
        self.agent._supports_active_turn_redirect = False
        observer = asyncio.create_task(self.receive(self.event('interrupted')))
        await self.accepted('interrupted')
        self.assertEqual(self.agent.effects[-1], ('interrupt', 'bounded instruction'))
        self.assertEqual(self.row('interrupted')['status'], 'queued')
        await self.cancel_observer(observer)
        retry = asyncio.create_task(self.receive(self.event('interrupted')))
        from tests.waiting import async_wait_until
        await async_wait_until(lambda: sum(mid == 'interrupted' for mid, _ in self.receipts) == 2)
        self.assertEqual(len(self.agent.effects), 2)
        await self.cancel_observer(retry)

    async def test_conflict_and_unauthorized_actor_never_enter_busy_effects(self):
        from dataclasses import replace
        from hermes_state_runtime import RuntimeStoreError
        await self.start()
        await self.receive(self.event('same-id', 'original text'))
        with self.assertRaises(RuntimeStoreError) as conflict:
            await self.receive(self.event('same-id', 'changed text'))
        self.assertEqual(conflict.exception.reason, 'admission_conflict')
        self.allowed.remove('fixture-author')
        with self.assertRaises(RuntimeStoreError) as denied:
            await self.receive(self.event('same-id', 'original text'))
        self.assertEqual(denied.exception.reason, 'permission_denied')
        self.allowed.add('fixture-author')
        await self.receive(self.event('same-id', 'original text'))
        self.assertEqual(len(self.agent.effects), 1)
        other = replace(self.source, user_id='other-author')
        await self.receive(self.event('same-id', 'separate actor input', source=other))
        self.assertEqual(len(self.agent.effects), 2)
        self.allowed.remove('fixture-author')
        with self.assertRaises(RuntimeStoreError):
            await self.receive(self.event('first-refused-id'))
        self.allowed.add('fixture-author')
        await self.receive(self.event('first-refused-id'))
        self.assertEqual(len(self.agent.effects), 3)

    async def test_newer_stop_after_commit_cancels_without_transfer(self):
        await self.start()
        write = self.authority._admit_native_write
        def stop_after_write(**kwargs):
            result = write(**kwargs)
            self.authority.fence_route(self.route)
            return result
        from unittest.mock import patch
        with patch.object(self.authority, '_admit_native_write', stop_after_write):
            await self.receive(self.event('held-by-stop'))
        self.assertEqual(self.agent.effects, [])
        self.assertEqual(self.row('held-by-stop')['outcome'], 'cancelled')

    async def test_newer_stop_after_transfer_reservation_does_not_requeue(self):
        from unittest.mock import patch
        import gateway.session_queued_controls as queued
        await self.start()
        transfer = queued.steer_session_input
        def stopped(db, **kwargs):
            result = transfer(db, **kwargs)
            self.authority.fence_route(self.route)
            return result
        with patch.object(queued, 'steer_session_input', stopped):
            # Keep the real SQLite transition; only inject the control boundary.
            await self.receive(self.event('stopped-transfer'))
        self.assertEqual(self.agent.effects, [])
        self.assertEqual(self.row('stopped-transfer')['outcome'], 'interrupted')
        self.settle()
        self.assertEqual(self.row('stopped-transfer')['outcome'], 'interrupted')

    async def test_epoch_and_agent_change_between_prepare_and_apply_do_not_deliver(self):
        from unittest.mock import patch
        from hermes_state_runtime import begin_runtime_epoch, RuntimeStoreError
        await self.start()
        write = self.authority._admit_native_write
        def new_epoch(**kwargs):
            result = write(**kwargs)
            begin_runtime_epoch(self.authority.db, instance_id='replacement-owner')
            return result
        with patch.object(self.authority, '_admit_native_write', new_epoch):
            with self.assertRaises(RuntimeStoreError) as stale:
                await self.receive(self.event('epoch-change'))
        self.assertEqual(stale.exception.reason, 'stale_epoch')
        self.assertEqual(self.agent.effects, [])

    async def test_changed_agent_keeps_input_queued_and_later_input_can_steer_new_agent(self):
        from unittest.mock import patch
        await self.start()
        write, old = self.authority._admit_native_write, self.agent
        replacement = FiniteAgent()
        def replace_agent(**kwargs):
            result = write(**kwargs)
            self.authority.adopt_agent(self.ref.session_id, self.running['generation'], replacement)
            return result
        with patch.object(self.authority, '_admit_native_write', replace_agent):
            observer = asyncio.create_task(self.receive(self.event('changed-target')))
            await self.accepted('changed-target')
            self.assertEqual(self.row('changed-target')['status'], 'queued')
            await self.cancel_observer(observer)
        self.assertEqual(old.effects, [])
        self.assertEqual(replacement.effects, [])
        await self.receive(self.event('current-target'))
        self.assertEqual(len(replacement.effects), 1)

    async def test_approval_is_committed_and_bound_to_captured_request_id(self):
        from tools import approval
        from tools.approval_gateway_wait import _ApprovalEntry
        from unittest.mock import patch
        await self.start()
        before = _ApprovalEntry({'request_id': 'before', 'command': 'finite fixture'})
        later = _ApprovalEntry({'request_id': 'later', 'command': 'finite fixture'})
        with approval._lock:
            approval._gateway_queues[self.route] = [before]
        self.addCleanup(approval._gateway_queues.pop, self.route, None)
        await self.receive(self.event('approve-before', 'yes', control=True))
        self.assertEqual(before.result, 'once')
        self.assertEqual(self.row('approve-before')['outcome'], 'steering')
        with approval._lock:
            approval._gateway_queues[self.route] = [later]
        await self.receive(self.event('approve-before', 'yes', control=True))
        self.assertIsNone(later.result)
        write = self.authority._admit_native_write
        def replace_prompt(**kwargs):
            result = write(**kwargs)
            with approval._lock:
                approval._gateway_queues[self.route] = [_ApprovalEntry({'request_id': 'newest'})]
            return result
        with patch.object(self.authority, '_admit_native_write', replace_prompt):
            observer = asyncio.create_task(self.receive(self.event('stale-approval-target', 'yes', control=True)))
            await self.accepted('stale-approval-target')
            self.assertIsNone(later.result)
            self.assertIsNone(approval._gateway_queues[self.route][0].result)
            await self.cancel_observer(observer)

    async def test_slow_or_failing_admission_hook_does_not_hold_ordering_or_lose_schedule(self):
        from unittest.mock import patch
        await self.start()
        entered, release = asyncio.Event(), asyncio.Event()
        original_hook = self.adapter.on_message_admitted
        async def held(event, receipt):
            await original_hook(event, receipt)
            if event.message_id == 'held-ack':
                entered.set()
                await release.wait()
                raise RuntimeError('finite transport ACK failed')
        with patch.object(self.adapter, 'on_message_admitted', held):
            first = asyncio.create_task(self.receive(self.event('held-ack')))
            await asyncio.wait_for(entered.wait(), 5)
            await asyncio.wait_for(self.receive(self.event('later-input')), 5)
            self.authority.fence_route(self.route)
            release.set()
            with self.assertRaisesRegex(RuntimeError, 'ACK failed'):
                await first
        self.assertEqual(len(self.agent.effects), 2)
        self.assertGreaterEqual(len(self.scheduled), 3)
        await self.receive(self.event('held-ack'))
        self.assertEqual(len(self.agent.effects), 2)

    async def test_media_stays_a_complete_native_turn_and_is_usable_after_running_turn(self):
        from hermes_state_runtime import claim_session_input, settle_session_input
        await self.start()
        clip = self.home / 'fixture.bin'
        clip.write_bytes(b'bounded fixture attachment')
        event = self.event('media-turn')
        event.media_urls, event.media_types = [str(clip)], ['application/octet-stream']
        observer = asyncio.create_task(self.receive(event))
        await self.accepted('media-turn')
        row = self.row('media-turn')
        self.assertEqual(row['status'], 'queued')
        self.assertEqual(len(row['payload']['native_text_v1']['media']), 1)
        self.assertEqual(self.agent.effects, [])
        self.settle()
        next_row = claim_session_input(self.authority.db, epoch=self.authority.epoch, session_id=self.ref.session_id)
        self.assertEqual(next_row['admission_id'], row['admission_id'])
        settle_session_input(self.authority.db, epoch=self.authority.epoch, admission_id=next_row['admission_id'],
            generation=next_row['generation'], outcome='completed')
        self.authority.waiters[row['admission_id']].set_result(None)  # finite no-model execution endpoint
        self.assertEqual(await asyncio.wait_for(observer, 5), (True, None))

    async def test_foreign_active_profile_cannot_reconcile_or_preempt_this_owner(self):
        from gateway.session_authorities import SessionAuthorities
        from hermes_constants import set_hermes_home_override, reset_hermes_home_override
        from hermes_state_runtime import RuntimeStoreError
        await self.start()
        registry = SessionAuthorities(self.home, multiplexed=True)
        registry.add(self.home, self.authority)
        self.runner.session_authorities = registry
        other = self.home / 'unserved-profile'
        other.mkdir()
        token = set_hermes_home_override(other)
        try:
            with self.assertRaises(RuntimeStoreError) as mismatch:
                await self.receive(self.event('foreign-profile'))
        finally:
            reset_hermes_home_override(token)
        self.assertEqual(mismatch.exception.reason, 'profile_mismatch')
        self.assertEqual(self.agent.effects, [])

    async def test_full_fifo_reconciles_duplicates_and_keeps_specific_approval_control_usable(self):
        from tools import approval
        from tools.approval_gateway_wait import _ApprovalEntry
        from hermes_state_runtime import ADMISSION_QUEUE_MAX
        from gateway.session_ingress import QUEUE_FULL_REPLY
        await self.start()
        await self.receive(self.event('already-steered'))
        for number in range(ADMISSION_QUEUE_MAX):
            await self.authority.admit_native(self.event('queued-' + str(number)))
        await self.receive(self.event('already-steered'))
        self.assertEqual(len(self.agent.effects), 1)
        pending = _ApprovalEntry({'request_id': 'specific-control', 'command': 'finite fixture'})
        with approval._lock:
            approval._gateway_queues[self.route] = [pending]
        self.addCleanup(approval._gateway_queues.pop, self.route, None)
        self.assertEqual(await self.receive(self.event('full-fifo-yes', 'yes', control=True)), (True, QUEUE_FULL_REPLY))
        self.assertIsNone(pending.result)
        self.assertEqual(len(self.agent.effects), 1)
        # The existing targeted approval control is independent of FIFO capacity.
        self.assertEqual(approval.resolve_gateway_approval(self.route, 'once', request_id='specific-control'), 1)
        self.assertEqual(pending.result, 'once')
        self.authority.fence_route(self.route)  # structured Stop's existing native fence remains available

    def base_adapter(self):
        from gateway.config import PlatformConfig
        from gateway.platforms.base import BasePlatformAdapter, SendResult
        phases, completed = [], asyncio.Event()
        fixture = self
        class Adapter(BasePlatformAdapter):
            requires_durable_input_ack = True
            async def connect(self, *, is_reconnect=False):
                return True
            async def disconnect(self):
                return None
            async def send(self, *args, **kwargs):
                return SendResult(success=True, message_id='finite-send')
            async def get_chat_info(self, chat_id):
                return {'name': 'finite fixture', 'type': 'dm'}
            async def on_message_admitted(self, event, receipt):
                phases.append(('accepted', receipt.status))
                await fixture.adapter.on_message_admitted(event, receipt)
            async def on_processing_complete(self, event, outcome):
                phases.append(('processed', outcome.value))
                completed.set()
        adapter = Adapter(PlatformConfig(enabled=True, typing_indicator=False), self.source.platform)
        self.runner.adapters[self.source.platform] = adapter
        async def handle(runner, event):
            _handled, reply = await fixture.receive(event)
            return reply
        adapter.set_message_handler(MethodType(handle, self.runner))
        adapter.set_session_store(self.store)
        return adapter, phases, completed

    async def test_actual_base_lifecycle_acks_durable_input_before_finite_turn_completion(self):
        from hermes_state_runtime import list_session_admissions, claim_session_input, settle_session_input
        from tests.waiting import async_wait_until
        adapter, phases, completed = self.base_adapter()
        await adapter.handle_message(self.event('base-lifecycle'))
        await self.accepted('base-lifecycle')
        self.assertEqual(phases, [('accepted', 'queued')])
        self.assertFalse(completed.is_set())
        session_id = self.store.lookup_by_session_key(self.route).session_id
        rows = list_session_admissions(self.authority.db, session_id=session_id, pending_only=False)
        row = claim_session_input(self.authority.db, epoch=self.authority.epoch, session_id=session_id)
        self.assertEqual(row['admission_id'], rows[0]['admission_id'])
        await async_wait_until(lambda: row['admission_id'] in self.authority.waiters)
        settle_session_input(self.authority.db, epoch=self.authority.epoch, admission_id=row['admission_id'],
            generation=row['generation'], outcome='completed')
        self.authority.waiters[row['admission_id']].set_result(None)
        await asyncio.wait_for(completed.wait(), 5)
        await async_wait_until(lambda: not adapter._background_tasks)
        self.assertEqual(phases[-1], ('processed', 'success'))

    async def test_actual_active_base_keeps_distinct_provider_ids_and_bodies_unmerged(self):
        from hermes_state_runtime import claim_session_input
        from tests.waiting import async_wait_until
        adapter, _phases, completed = self.base_adapter()
        adapter._busy_text_mode = 'queue'  # an ordinary adapter would debounce/merge this burst
        await adapter.handle_message(self.event('opening-base'))
        await self.accepted('opening-base')
        self.ref = self.receipts[0][1].ref
        self.running = claim_session_input(self.authority.db, epoch=self.authority.epoch, session_id=self.ref.session_id)
        self.agent = FiniteAgent()
        self.authority.executing[self.ref.session_id] = self.running['generation']
        self.authority.adopt_agent(self.ref.session_id, self.running['generation'], self.agent)
        await asyncio.gather(adapter.handle_message(self.event('activity-a', 'first immutable body')),
                             adapter.handle_message(self.event('activity-b', 'second immutable body')))
        for message_id, body in [('activity-a', 'first immutable body'), ('activity-b', 'second immutable body')]:
            await self.accepted(message_id)
            self.assertEqual(self.row(message_id)['payload']['text'], body)
            self.assertEqual(sum(mid == message_id for mid, _ in self.receipts), 1)
        self.assertEqual(len(self.agent.effects), 2)
        self.agent.pending.clear()
        self.settle()
        await async_wait_until(lambda: self.running['admission_id'] in self.authority.waiters)
        self.authority.waiters[self.running['admission_id']].set_result(None)
        await asyncio.wait_for(completed.wait(), 5)
        await async_wait_until(lambda: not adapter._background_tasks)

    async def test_default_adapter_retains_legacy_busy_callback_without_native_claim(self):
        self.adapter.requires_durable_input_ack = False
        self.assertEqual(await self.receive(self.event('legacy-busy')), (True, None))
        self.assertEqual(self.old_callbacks, ['legacy-busy'])
        self.assertEqual(self.receipts, [])
