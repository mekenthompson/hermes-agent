"""Independent review: cancelling a provider observer cannot strand a committed input."""
import asyncio
import threading
import unittest

from tests.gateway import test_native_inbound_retry as fixtures

no_bootstrap = fixtures.no_bootstrap


class CancelledNativeCommitTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.NativeInboundRetryTests.asyncSetUp
    event = fixtures.NativeInboundRetryTests.event
    start = fixtures.NativeInboundRetryTests.start

    async def test_cancelled_observer_still_schedules_its_committed_native_input(self):
        entered, release = threading.Event(), threading.Event()
        original = self.authority._admit_native_write

        def held_write(**kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError('review synchronization was not released')
            return original(**kwargs)

        self.authority._admit_native_write = held_write
        event = self.event('review-cancelled-at-commit')
        observer = asyncio.create_task(self.authority.admit_native(event))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))
            observer.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await observer
        finally:
            release.set()
        await asyncio.gather(*tuple(self.authority._mutation_tasks))
        from hermes_state_runtime import list_session_admissions
        rows = list_session_admissions(self.authority.db,
            session_id=self.store.lookup_by_session_key(self.route).session_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['status'], 'queued')
        self.assertTrue(event._gateway_accepted)
        self.assertTrue(self.scheduled, 'durable input committed after cancellation must wake its native drain')

    async def test_cancelled_busy_observer_keeps_handling_before_drain(self):
        from hermes_state_runtime import get_session_admission
        entered, release = asyncio.Event(), asyncio.Event()
        effects = []

        async def before_schedule(receipt, fence):
            row = get_session_admission(self.authority.db, admission_id=receipt.admission_id)
            self.assertEqual(row['status'], 'queued')
            self.assertFalse(self.scheduled)
            entered.set()
            await release.wait()
            effects.append('busy handling completed')

        observer = asyncio.create_task(self.authority.admit_native(
            self.event('cancelled-busy-continuation'), before_schedule=before_schedule))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            observer.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await observer
            self.assertFalse(effects)
            self.assertFalse(self.scheduled)
        finally:
            release.set()
            await asyncio.gather(*tuple(self.authority._mutation_tasks))
        self.assertEqual(effects, ['busy handling completed'])
        self.assertEqual(len(self.scheduled), 1)

    async def test_cancelled_stop_observer_keeps_original_control_reconciliation(self):
        from gateway.platforms.event import StructuredControl
        from gateway.session_ancillary import process_stop_pending
        from hermes_state_runtime import list_session_admissions
        await self.start()
        self.scheduled.clear()
        entered, release = threading.Event(), threading.Event()
        original = self.authority._admit_native_write

        def held_write(**kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError('Stop commit synchronization was not released')
            return original(**kwargs)

        self.authority._admit_native_write = held_write
        event = self.event('cancelled-stop-continuation', 'original platform Stop')
        event.structured_control = StructuredControl.STOP
        observer = asyncio.create_task(self.authority.admit_native(event))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))
            observer.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await observer
        finally:
            release.set()
            await asyncio.gather(*tuple(self.authority._mutation_tasks))
        rows = list_session_admissions(self.authority.db, session_id=self.ref.session_id, pending_only=False)
        stopped = [row for row in rows if row['request_id'] == event.message_id]
        self.assertEqual(len(stopped), 1)
        self.assertEqual((stopped[0]['status'], stopped[0]['outcome']), ('terminal', 'cancelled'))
        self.assertTrue(event._gateway_accepted)
        self.assertEqual(self.agent.effects, [('interrupt', None)])
        self.assertIsNotNone(process_stop_pending(self.authority.db, self.ref.session_id))
        self.assertEqual(len(self.scheduled), 1)
