"""Durable provider input cannot invert the routing and database write locks."""

import asyncio
from contextvars import copy_context
from dataclasses import replace
import threading
import unittest
from unittest.mock import patch

from tests.gateway import test_native_inbound_retry as fixtures
from tests.gateway import test_native_structured_stop as stop_fixtures

no_bootstrap = fixtures.no_bootstrap


class NativeInboxRouteLockTests(unittest.IsolatedAsyncioTestCase):
    event = fixtures.NativeInboundRetryTests.event
    receive = fixtures.NativeInboundRetryTests.receive
    base_adapter = fixtures.NativeInboundRetryTests.base_adapter
    stop = stop_fixtures.NativeStructuredStopTests.stop

    async def asyncSetUp(self):
        with patch('hermes_state_wal.resolve_journal_mode', return_value='delete'):
            await fixtures.NativeInboundRetryTests.asyncSetUp(self)

    async def test_base_input_commits_while_route_metadata_waits_for_database_writer(self):
        from tests.waiting import async_wait_until

        self.store.get_or_create_session(self.source)
        self.assertIs(self.store._routing_db, self.authority.db)
        adapter, _phases, completed = self.base_adapter()
        db = self.authority.db
        writer = threading.local()
        entered, metadata_locked = threading.Event(), threading.Event()
        violations, metadata_errors = [], []
        original_write = db._execute_write
        original_lookup = self.store.lookup_by_session_key
        original_persist = self.store._persist_routing_data

        def metadata():
            try:
                self.assertIs(self.store._routing_db, db)
                self.store.set_session_metadata(self.route, 'concurrent-marker', 'preserved')
            except BaseException as error:
                metadata_errors.append(error)

        context = copy_context()
        concurrent = threading.Thread(target=context.run, args=(metadata,), daemon=True)

        def observed_persist(*args, **kwargs):
            if threading.current_thread() is concurrent:
                # This real routing mutation already holds the store lock and
                # now needs the real database writer held by native input.
                self.assertTrue(self.store._lock.locked())
                metadata_locked.set()
            return original_persist(*args, **kwargs)

        def observed_write(fn, *args, **kwargs):
            def transaction(conn):
                writer.active = True
                try:
                    if not entered.is_set():
                        entered.set()
                        concurrent.start()
                        self.assertTrue(metadata_locked.wait(5))
                    return fn(conn)
                finally:
                    writer.active = False
            return original_write(transaction, *args, **kwargs)

        def observed_lookup(*args, **kwargs):
            if getattr(writer, 'active', False) and metadata_locked.is_set():
                violations.append('native writer waits for store held by metadata waiting for writer')
                # Refuse the observed cycle before the real lock can hang the
                # test. No lock is replaced, released or acquired out of order.
                raise AssertionError(violations[-1])
            return original_lookup(*args, **kwargs)

        try:
            with (patch.object(db, '_execute_write', observed_write),
                  patch.object(self.store, '_persist_routing_data', observed_persist),
                  patch.object(self.store, 'lookup_by_session_key', observed_lookup)):
                await adapter.handle_message(self.event('concurrent-route-input'))
                await async_wait_until(lambda: bool(self.receipts) or completed.is_set(), timeout=10)
                self.assertEqual(violations, [])
                self.assertEqual([mid for mid, _ in self.receipts], ['concurrent-route-input'])
                await asyncio.to_thread(concurrent.join, 5)
                self.assertFalse(concurrent.is_alive())
                self.assertEqual(metadata_errors, [])
            self.assertEqual(self.store.get_session_metadata(self.route, 'concurrent-marker'), 'preserved')
        finally:
            tasks = list(adapter._background_tasks)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if concurrent.ident is not None:
                await asyncio.to_thread(concurrent.join, 5)

    async def test_route_stop_fence_survives_reset_and_excludes_other_routes(self):
        from hermes_state_runtime import native_stop_watermark

        await self.receive(self.stop('before-reset'))
        first = self.receipts[-1][1]
        before = native_stop_watermark(self.authority.db, route=self.route)
        self.assertGreater(before, 0)
        other = self.stop('other-route-stop')
        other.source = replace(self.source, chat_id='independent-chat')
        await self.receive(other)
        self.assertEqual(native_stop_watermark(self.authority.db, route=self.route), before)
        self.store.reset_session(self.route)
        await self.receive(self.stop('after-reset'))
        after = self.receipts[-1][1]
        self.assertNotEqual(after.ref.session_id, first.ref.session_id)
        mark = native_stop_watermark(self.authority.db, route=self.route)
        self.assertGreater(mark, before)
        self.assertEqual(native_stop_watermark(self.authority.db, first.ref.session_id), before)
        await self.receive(self.stop('before-reset'))
        self.assertEqual(native_stop_watermark(self.authority.db, route=self.route), mark)

    async def test_first_capture_is_fenced_by_stop_before_any_route_existed(self):
        import gateway.session_envelope as envelope
        from hermes_state_runtime import list_session_admissions

        self.assertIsNone(self.store.lookup_by_session_key(self.route))
        entered, release = threading.Event(), threading.Event()
        capture = envelope.capture_native_media

        def held(paths):
            entered.set()
            if not release.wait(10):
                raise AssertionError('test must release native capture')
            return capture(paths)

        clip = self.home / 'first-capture.bin'
        clip.write_bytes(b'finite fixture')
        event = self.event('before-first-stop')
        event.media_urls, event.media_types = [str(clip)], ['application/octet-stream']
        with patch.object(envelope, 'capture_native_media', held):
            first = asyncio.create_task(self.receive(event))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                await asyncio.wait_for(self.receive(self.stop('first-stop')), 5)
                ref = self.receipts[-1][1].ref
            finally:
                release.set()
            self.assertEqual(await asyncio.wait_for(first, 5), (True, None))
        rows = list_session_admissions(self.authority.db, session_id=ref.session_id, pending_only=False)
        self.assertNotIn('before-first-stop', [row['request_id'] for row in rows])
        later = asyncio.create_task(self.receive(self.event('after-first-stop', 'Current status?')))
        try:
            await asyncio.wait_for(self.receipt_events.setdefault('after-first-stop', asyncio.Event()).wait(), 5)
            rows = list_session_admissions(self.authority.db, session_id=ref.session_id, pending_only=False)
            self.assertEqual(next(row for row in rows if row['request_id'] == 'after-first-stop')['status'], 'queued')
        finally:
            later.cancel()
            await asyncio.gather(later, return_exceptions=True)
