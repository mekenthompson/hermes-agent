"""Durable provider IDs retain their native receipts across real route transitions."""
import unittest

from tests.gateway import test_native_inbound_retry as fixtures

no_bootstrap = fixtures.no_bootstrap


class NativeDeliveryResetTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.NativeInboundRetryTests.asyncSetUp
    event = fixtures.NativeInboundRetryTests.event
    start = fixtures.NativeInboundRetryTests.start
    settle = fixtures.NativeInboundRetryTests.settle
    receive = fixtures.NativeInboundRetryTests.receive

    async def test_opt_in_frontdoor_does_not_offer_delivery_to_uncommitted_consumer(self):
        calls = []
        async def consume(event, *args):
            calls.append(event.message_id)
            return True, 'unrecorded reply'
        self.runner._hm_post_admission_consume = consume
        await self.start()
        await self.receive(self.event('provider-activity'))
        self.assertEqual(calls, [])
        self.assertEqual(self.receipts[-1][0], 'provider-activity')
        self.assertEqual(self.receipts[-1][1].status, 'started')
        self.adapter.requires_durable_input_ack = False
        self.runner._is_session_running = lambda route: False
        self.assertEqual(await self.receive(self.event('legacy-activity')), (True, 'unrecorded reply'))
        self.assertEqual(calls, ['legacy-activity'])

    async def test_terminal_delivery_after_reset_reuses_original_receipt_and_preserves_new_turn(self):
        from hermes_state_runtime import list_session_admissions, RuntimeStoreError
        await self.start('provider-activity')
        old, old_ref = self.running, self.ref
        self.settle()
        self.store.reset_session(self.route)
        await self.start('new-provider-activity')
        await self.receive(self.event('provider-activity'))
        self.assertEqual(self.agent.effects, [])
        self.assertEqual(self.receipts[-1][1].admission_id, old['admission_id'])
        self.assertEqual(self.receipts[-1][1].ref, old_ref)
        self.assertEqual(len(list_session_admissions(self.authority.db,
            session_id=self.ref.session_id, pending_only=False)), 1)
        with self.assertRaises(RuntimeStoreError) as changed:
            await self.receive(self.event('provider-activity', 'changed content'))
        self.assertEqual(changed.exception.reason, 'admission_conflict')
        self.allowed.clear()
        with self.assertRaises(RuntimeStoreError) as revoked:
            await self.receive(self.event('provider-activity'))
        self.assertEqual(revoked.exception.reason, 'permission_denied')
        self.assertEqual(self.agent.effects, [])

    async def test_deleted_original_target_returns_only_its_terminal_receipt(self):
        from hermes_state_runtime import get_session_admission
        await self.start('retired-provider-activity')
        old, old_ref = self.running, self.ref
        self.settle()
        self.store.reset_session(self.route)
        self.assertTrue(self.authority.db.delete_session(old_ref.session_id))
        self.authority.sessions.pop(old_ref.session_id)
        await self.start('new-provider-activity')
        await self.receive(self.event('retired-provider-activity'))
        self.assertEqual(self.receipts[-1][1].admission_id, old['admission_id'])
        self.assertEqual(get_session_admission(self.authority.db,
            admission_id=old['admission_id'])['payload'], {})
        self.assertNotIn(old_ref.session_id, self.authority.sessions)
        self.assertEqual(self.agent.effects, [])

    async def test_pending_original_target_cannot_retarget_after_reset(self):
        from hermes_state_runtime import RuntimeStoreError
        await self.start('pending-provider-activity')
        old_ref = self.ref
        self.store.reset_session(self.route)
        await self.start('new-provider-activity')
        with self.assertRaises(RuntimeStoreError) as held:
            await self.receive(self.event('pending-provider-activity'))
        self.assertEqual(held.exception.reason, 'session_superseded')
        self.assertNotEqual(old_ref, self.ref)
        self.assertEqual(self.agent.effects, [])

    async def test_compression_keeps_delivery_identity_on_original_logical_owner(self):
        await self.start('compressed-provider-activity')
        old, old_ref = self.running, self.ref
        self.settle()
        self.authority.db.end_session(old_ref.session_id, 'compression')
        tip = 'finite-compression-continuation'
        self.authority.db.create_session(tip, source=self.source.platform.value,
            session_key=self.route, parent_session_id=old_ref.session_id)
        self.assertIsNotNone(self.store.switch_session(self.route, tip))
        await self.receive(self.event('compressed-provider-activity'))
        self.assertEqual(self.receipts[-1][1].admission_id, old['admission_id'])
        self.assertEqual(self.receipts[-1][1].ref, old_ref)
        self.assertEqual(self.store.lookup_by_session_key(self.route).session_id, tip)

    async def test_ambiguous_legacy_delivery_is_refused_before_new_route_reservation(self):
        from gateway.session_envelope import snapshot_native
        from gateway.session_native_delivery import identity
        from hermes_state_runtime import admit_session_input, RuntimeStoreError
        event = self.event('ambiguous-provider-activity')
        await self.start(event.message_id)
        self.settle()
        self.store.reset_session(self.route)
        next_ref = self.authority.register(self.source)
        payload = snapshot_native(self.runner, event)
        principal, request = identity(self.source, payload)
        admit_session_input(self.authority.db, epoch=self.authority.epoch,
            principal_id=principal, session_id=next_ref.session_id, request_id=request, payload=payload)
        with self.assertRaises(RuntimeStoreError) as held:
            await self.receive(event)
        self.assertEqual(held.exception.reason, 'admission_conflict')
        self.assertEqual(self.agent.effects, [])
