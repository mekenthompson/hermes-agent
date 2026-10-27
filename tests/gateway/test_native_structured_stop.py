"""Typed durable Stop uses its original native target, never a later generation.

The model and transport are finite fixtures. The native codec, owner, SQLite
admission/control fence and interrupt body are real; no gateway bootstrap runs.
"""
import asyncio
from unittest.mock import patch
import unittest

from tests.gateway import test_native_inbound_retry as fixtures
from tests.gateway.test_native_inbound_retry import FiniteAgent

no_bootstrap = fixtures.no_bootstrap


class NativeStructuredStopTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.NativeInboundRetryTests.asyncSetUp
    event = fixtures.NativeInboundRetryTests.event
    start = fixtures.NativeInboundRetryTests.start
    settle = fixtures.NativeInboundRetryTests.settle
    receive = fixtures.NativeInboundRetryTests.receive
    row = fixtures.NativeInboundRetryTests.row
    accepted = fixtures.NativeInboundRetryTests.accepted
    cancel_observer = fixtures.NativeInboundRetryTests.cancel_observer
    base_adapter = fixtures.NativeInboundRetryTests.base_adapter

    def stop(self, message_id='structured-stop'):
        from gateway.platforms.event import StructuredControl
        event = self.event(message_id, 'original platform Stop')
        event.structured_control = StructuredControl.STOP
        return event

    async def test_old_stop_after_actual_session_reset_does_not_stop_new_owner(self):
        from gateway.session_ancillary import retry_process_stop
        await self.start()
        await self.receive(self.stop())
        old = self.row('structured-stop')
        old_ref = self.ref
        self.settle('interrupted')
        self.authority.executing.pop(old_ref.session_id, None)
        self.assertTrue(await retry_process_stop(self.authority, old_ref, self.agent))
        reset = self.store.reset_session(self.route)
        self.assertNotEqual(reset.session_id, old_ref.session_id)
        await self.start('after-reset')
        await self.receive(self.stop())
        self.assertEqual(self.agent.effects, [])
        self.assertEqual(self.receipts[-1][1].admission_id, old['admission_id'])
        self.assertEqual(self.receipts[-1][1].ref, old_ref)
        self.assertEqual(self.store.lookup_by_session_key(self.route).session_id, self.ref.session_id)
        self.assertNotIn('structured-stop', [row['request_id'] for row in self.all_rows()])

    def all_rows(self):
        from hermes_state_runtime import list_session_admissions
        return list_session_admissions(self.authority.db, session_id=self.ref.session_id, pending_only=False)

    async def test_retired_stop_receipt_keeps_descriptor_without_body_and_never_recaptures(self):
        from gateway.session_ancillary import retry_process_stop
        from hermes_state_runtime import get_session_admission, RuntimeStoreError
        from hermes_state_mutation_retirement import delete_in_transaction
        await self.start()
        await self.receive(self.stop())
        old = self.row('structured-stop')
        old_ref = self.ref
        self.settle('interrupted')
        self.authority.executing.pop(old_ref.session_id, None)
        await retry_process_stop(self.authority, old_ref, self.agent)
        self.store.reset_session(self.route)
        self.authority.db._execute_write(lambda conn: delete_in_transaction(self.authority.db, conn,
            old_ref.session_id, {}))
        self.authority.sessions.pop(old_ref.session_id)
        tombstone = get_session_admission(self.authority.db, admission_id=old['admission_id'])
        self.assertEqual(tombstone['payload'], {'native_stop_v1': old['payload']['native_stop_v1']})
        await self.start('after-retirement')
        await self.receive(self.stop())
        self.assertEqual(self.agent.effects, [])
        self.assertEqual(self.receipts[-1][1].admission_id, old['admission_id'])
        self.assertTrue(self.receipts[-1][1].control.request_consumed)
        changed = self.stop()
        changed.text = 'changed original Stop body'
        with self.assertRaises(RuntimeStoreError) as conflict:
            await self.receive(changed)
        self.assertEqual(conflict.exception.reason, 'admission_conflict')
        self.assertEqual(self.agent.effects, [])

    async def test_full_fifo_stop_is_consumed_and_old_duplicate_cannot_stop_later_work(self):
        from gateway.session_ancillary import retry_process_stop
        from hermes_state_runtime import ADMISSION_QUEUE_MAX
        await self.start()
        for number in range(ADMISSION_QUEUE_MAX):
            await self.authority.admit_native(self.event('queued-' + str(number)))
        old_generation = self.running['generation']
        self.assertEqual(await self.receive(self.stop()), (True, None))
        stopped = self.row('structured-stop')
        self.assertEqual(stopped['payload']['native_stop_v1']['generation'], old_generation)
        self.assertEqual(stopped['payload']['native_stop_v1']['cutoff'], ADMISSION_QUEUE_MAX + 1)
        self.assertEqual((stopped['status'], stopped['outcome']), ('terminal', 'cancelled'))
        self.assertEqual(self.agent.effects, [('interrupt', None)])
        receipt = next(receipt for mid, receipt in self.receipts if mid == 'structured-stop')
        self.assertTrue(receipt.control.request_consumed)
        self.assertTrue(receipt.control.cleanup_pending)  # request ACK is not physical proof
        from dataclasses import asdict
        self.assertNotIn('control', asdict(receipt))  # established RPC shape is unchanged
        self.assertNotIn('_control', asdict(receipt))
        for number in range(ADMISSION_QUEUE_MAX):
            self.assertEqual(self.row('queued-' + str(number))['outcome'], 'cancelled')
        self.settle('interrupted')
        self.authority.executing.pop(self.ref.session_id, None)
        self.assertTrue(await retry_process_stop(self.authority, self.ref, self.agent))
        await self.start('later-generation')
        frozen = dict(stopped['payload']['native_stop_v1'])
        await self.receive(self.stop())
        self.assertEqual(self.agent.effects, [])
        self.assertEqual(self.row('structured-stop')['payload']['native_stop_v1'], frozen)

    async def test_concurrent_identical_stops_apply_once(self):
        await self.start()
        await asyncio.gather(self.receive(self.stop()), self.receive(self.stop()))
        self.assertEqual(self.agent.effects, [('interrupt', None)])
        self.assertEqual(sum(mid == 'structured-stop' for mid, _ in self.receipts), 2)

    async def test_committed_stop_before_effect_survives_restart_and_blocks_model_claim(self):
        from gateway.session_authority import initialize_session_authority
        from gateway.session_ancillary import process_stop_pending
        from hermes_state_runtime import claim_session_input, RuntimeStoreError
        import gateway.session_native_control as controls
        await self.start()
        async def crash(*args):
            raise RuntimeError('finite interruption after control commit')
        with patch.object(controls, 'reconcile', crash):
            with self.assertRaisesRegex(RuntimeError, 'after control commit'):
                await self.receive(self.stop())
        original = self.row('structured-stop')['payload']['native_stop_v1']
        self.assertEqual(self.row('structured-stop')['status'], 'queued')
        self.assertIsNotNone(process_stop_pending(self.authority.db, self.ref.session_id))
        with self.assertRaises(RuntimeStoreError) as pending:
            claim_session_input(self.authority.db, epoch=self.authority.epoch, session_id=self.ref.session_id)
        self.assertEqual(pending.exception.reason, 'control_pending')
        self.authority = await initialize_session_authority(self.runner, profile_id=str(self.home), instance_id='after-crash')
        self.authority._schedule = lambda ref: self.scheduled.append(ref)
        await self.receive(self.stop())
        self.assertEqual(self.row('structured-stop')['payload']['native_stop_v1'], original)
        self.assertEqual(self.row('structured-stop')['outcome'], 'cancelled')
        self.assertEqual(self.row('opening')['status'], 'unknown')
        receipt = next(receipt for mid, receipt in self.receipts if mid == 'structured-stop')
        self.assertTrue(receipt.control.cleanup_pending)

    async def test_control_cancels_capture_that_entered_before_it_but_preserves_later_input(self):
        import threading
        import gateway.session_envelope as envelope
        from tests.waiting import async_wait_until
        await self.start()
        entered, release = threading.Event(), threading.Event()
        capture = envelope.capture_native_media
        def held(paths):
            entered.set()
            if not release.wait(10):
                raise RuntimeError('fixture capture gate was not released')
            return capture(paths)
        clip = self.home / 'held.bin'
        clip.write_bytes(b'finite capture')
        event = self.event('before-stop-capture')
        event.media_urls, event.media_types = [str(clip)], ['application/octet-stream']
        with patch.object(envelope, 'capture_native_media', held):
            first = asyncio.create_task(self.receive(event))
            try:
                await asyncio.to_thread(entered.wait, 5)
                self.assertTrue(entered.is_set())
                await asyncio.wait_for(self.receive(self.stop()), 5)
            finally:
                release.set()
            self.assertEqual(await asyncio.wait_for(first, 5), (True, None))
        from hermes_state_runtime import list_session_admissions
        self.assertNotIn('before-stop-capture', [row['request_id'] for row in
            list_session_admissions(self.authority.db, session_id=self.ref.session_id, pending_only=False)])
        later = asyncio.create_task(self.receive(self.event('later-status', 'What is the status?')))
        await self.accepted('later-status')
        self.assertEqual(self.row('later-status')['status'], 'queued')
        retry = asyncio.create_task(self.receive(self.stop()))
        await async_wait_until(lambda: sum(mid == 'structured-stop' for mid, _ in self.receipts) == 2)
        await retry
        self.assertEqual(self.row('later-status')['status'], 'queued')
        await self.cancel_observer(later)

    async def test_current_revocation_and_unsupported_adapter_refuse_before_effects(self):
        from hermes_state_runtime import RuntimeStoreError
        await self.start()
        self.adapter.requires_durable_input_ack = False
        with self.assertRaises(RuntimeStoreError):
            await self.receive(self.stop('unsupported'))
        self.assertEqual(self.agent.effects, [])
        self.adapter.requires_durable_input_ack = True
        write = self.authority._admit_native_write
        def revoke(**kwargs):
            result = write(**kwargs)
            self.allowed.remove('fixture-author')
            return result
        with patch.object(self.authority, '_admit_native_write', revoke):
            with self.assertRaises(RuntimeStoreError):
                await self.receive(self.stop())
        self.assertEqual(self.agent.effects, [])
        self.assertEqual(self.row('structured-stop')['status'], 'queued')
        self.allowed.add('fixture-author')
        await self.receive(self.stop())
        self.assertEqual(self.agent.effects, [('interrupt', None)])

    async def test_epoch_changes_after_commit_refuse_old_owner_before_effect(self):
        from hermes_state_runtime import begin_runtime_epoch, RuntimeStoreError
        import gateway.session_native_control as controls
        await self.start()
        reconcile = controls.reconcile
        async def replaced(authority, ref):
            begin_runtime_epoch(authority.db, instance_id='replacement-owner')
            return await reconcile(authority, ref)
        with patch.object(controls, 'reconcile', replaced):
            with self.assertRaises(RuntimeStoreError) as stale:
                await self.receive(self.stop())
        self.assertEqual(stale.exception.reason, 'stale_epoch')
        self.assertEqual(self.agent.effects, [])
        self.assertEqual(self.row('structured-stop')['status'], 'queued')

    async def test_cleanup_unknown_is_separate_from_consumed_request(self):
        from gateway.session_ancillary import process_stop_pending
        import gateway.session_ancillary as ancillary
        await self.start()
        async def unknown(*args):
            return False
        with patch.object(ancillary, 'interrupt_processes', unknown):
            await self.receive(self.stop())
        receipt = next(receipt for mid, receipt in self.receipts if mid == 'structured-stop')
        self.assertTrue(receipt.control.request_consumed)
        self.assertTrue(receipt.control.cleanup_pending)
        self.assertEqual(self.row('structured-stop')['outcome'], 'cancelled')
        self.assertIsNotNone(process_stop_pending(self.authority.db, self.ref.session_id))

    async def test_plain_payload_cannot_claim_control_capacity_or_descriptor(self):
        from hermes_state_runtime import admit_session_input, RuntimeStoreError
        await self.start()
        with self.assertRaises(RuntimeStoreError) as invalid:
            admit_session_input(self.authority.db, epoch=self.authority.epoch, principal_id='ordinary-input',
                session_id=self.ref.session_id, request_id='forged',
                payload={'text': 'ordinary API data', 'native_stop_v1': {'kind': 'stop'}})
        self.assertEqual(invalid.exception.reason, 'invalid_params')
        self.assertEqual(self.agent.effects, [])

    async def test_changed_control_or_work_identity_conflicts_before_target_capture(self):
        from hermes_state_runtime import RuntimeStoreError
        await self.start()
        with self.assertRaises(RuntimeStoreError) as conflict:
            await self.receive(self.stop('opening'))
        self.assertEqual(conflict.exception.reason, 'admission_conflict')
        self.assertEqual(self.agent.effects, [])
        await self.receive(self.stop())
        changed = self.stop()
        changed.text = 'changed original source body'
        with self.assertRaises(RuntimeStoreError) as changed_conflict:
            await self.receive(changed)
        self.assertEqual(changed_conflict.exception.reason, 'admission_conflict')
        self.assertEqual(self.agent.effects, [('interrupt', None)])

    async def test_revocation_before_atomic_write_creates_no_control_or_effect(self):
        from hermes_state_runtime import RuntimeStoreError, list_session_admissions
        from gateway.session_ancillary import process_stop_pending
        await self.start()
        write = self.authority._admit_native_write
        def revoked(**kwargs):
            self.allowed.remove('fixture-author')
            return write(**kwargs)
        with patch.object(self.authority, '_admit_native_write', revoked):
            with self.assertRaises(RuntimeStoreError) as denied:
                await self.receive(self.stop())
        self.assertEqual(denied.exception.reason, 'permission_denied')
        self.assertEqual(self.agent.effects, [])
        self.assertIsNone(process_stop_pending(self.authority.db, self.ref.session_id))
        self.assertEqual(len(list_session_admissions(self.authority.db,
            session_id=self.ref.session_id, pending_only=False)), 1)

    async def test_foreign_profile_and_untyped_control_refuse_without_side_effects(self):
        from gateway.session_authorities import SessionAuthorities
        from hermes_constants import set_hermes_home_override, reset_hermes_home_override
        from hermes_state_runtime import RuntimeStoreError
        await self.start()
        untyped = self.stop('untyped')
        untyped.structured_control = 'stop'
        handled, refusal = await self.receive(untyped)
        self.assertTrue(handled)
        self.assertIn('refused', refusal)
        registry = SessionAuthorities(self.home, multiplexed=True)
        registry.add(self.home, self.authority)
        self.runner.session_authorities = registry
        foreign = self.home / 'foreign'
        foreign.mkdir()
        token = set_hermes_home_override(foreign)
        try:
            with self.assertRaises(RuntimeStoreError) as mismatch:
                await self.receive(self.stop('foreign'))
        finally:
            reset_hermes_home_override(token)
        self.assertEqual(mismatch.exception.reason, 'profile_mismatch')
        self.assertEqual(self.agent.effects, [])

    async def test_pending_control_cannot_be_cancelled_as_model_work(self):
        import gateway.session_native_control as controls
        from hermes_state_runtime import cancel_session_input, RuntimeStoreError
        await self.start()
        async def crash(*args):
            raise RuntimeError('finite interruption after commit')
        with patch.object(controls, 'reconcile', crash):
            with self.assertRaises(RuntimeError):
                await self.receive(self.stop())
        with self.assertRaises(RuntimeStoreError) as unsupported:
            cancel_session_input(self.authority.db, epoch=self.authority.epoch,
                admission_id=self.row('structured-stop')['admission_id'])
        self.assertEqual(unsupported.exception.reason, 'unsupported_control')
        self.assertEqual(self.row('structured-stop')['status'], 'queued')

    async def test_actual_active_base_dispatches_typed_stop_without_text_command_or_model_turn(self):
        from hermes_state_runtime import claim_session_input
        from tests.waiting import async_wait_until
        adapter, _phases, completed = self.base_adapter()
        adapter._busy_text_mode = 'queue'
        await adapter.handle_message(self.event('opening-base'))
        await self.accepted('opening-base')
        self.ref = self.receipts[0][1].ref
        self.running = claim_session_input(self.authority.db, epoch=self.authority.epoch, session_id=self.ref.session_id)
        self.agent = FiniteAgent()
        self.authority.executing[self.ref.session_id] = self.running['generation']
        self.authority.adopt_agent(self.ref.session_id, self.running['generation'], self.agent)
        await asyncio.wait_for(adapter.handle_message(self.stop()), 5)
        await self.accepted('structured-stop')
        self.assertFalse(self.stop().is_command())
        self.assertEqual(self.row('structured-stop')['payload']['text'], 'original platform Stop')
        self.assertEqual(self.row('structured-stop')['outcome'], 'cancelled')
        self.assertEqual(self.agent.effects, [('interrupt', None)])
        self.settle('interrupted')
        await async_wait_until(lambda: self.running['admission_id'] in self.authority.waiters)
        self.authority.waiters[self.running['admission_id']].set_result(None)
        await asyncio.wait_for(completed.wait(), 5)
        await async_wait_until(lambda: not adapter._background_tasks)
