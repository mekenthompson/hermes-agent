"""Registered durable inboxes commit under SQLite's supported DELETE fallback.

The production Base/native admission path and nonreentrant DB lock are real.
Finite settings/agent/transport fixtures never bootstrap a gateway or model.
"""
from contextlib import contextmanager
from dataclasses import replace
import threading
import unittest
from unittest.mock import patch

from tests.gateway import test_native_inbound_retry as fixtures

no_bootstrap = fixtures.no_bootstrap


class NativeInboxDeleteModeTests(unittest.IsolatedAsyncioTestCase):
    event = fixtures.NativeInboundRetryTests.event
    accepted = fixtures.NativeInboundRetryTests.accepted
    row = fixtures.NativeInboundRetryTests.row
    receive = fixtures.NativeInboundRetryTests.receive
    settle = fixtures.NativeInboundRetryTests.settle

    async def asyncSetUp(self):
        with patch('hermes_state_wal.resolve_journal_mode', return_value='delete'):
            await fixtures.NativeInboundRetryTests.asyncSetUp(self)
        db = self.authority.db
        self.assertFalse(db._wal_active)
        self.assertEqual(db._conn.execute('PRAGMA journal_mode').fetchone()[0].lower(), 'delete')
        # A diagnostic observer makes the exact production deadlock finite on
        # the old source. It never replaces/releases the real DB lock or runs
        # a refused read; corrected code performs no nested reader at all.
        inside_write = threading.local()
        write, read = db._execute_write, db._read_ctx
        def observed_write(fn, *args, **kwargs):
            def transaction(conn):
                inside_write.active = True
                try:
                    return fn(conn)
                finally:
                    inside_write.active = False
            return write(transaction, *args, **kwargs)
        @contextmanager
        def observed_read(*args, **kwargs):
            if getattr(inside_write, 'active', False):
                raise AssertionError('nested SessionDB reader inside native writer deadlocks DELETE mode')
            with read(*args, **kwargs) as conn:
                yield conn
        self.patchers = [patch.object(db, '_execute_write', observed_write), patch.object(db, '_read_ctx', observed_read)]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def base_adapter(self):
        from gateway.config import Platform, PlatformConfig
        from gateway.platform_registry import platform_registry
        from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
        manager = PluginManager(scope_key=str(self.home))
        context = PluginContext(PluginManifest(name='durable-fixture', source='user', kind='standalone'), manager)
        created = []
        def factory(config):
            self.source = replace(self.source, platform=Platform('durable-fixture'))
            self.route = self.store._generate_session_key(self.source)
            adapter, phases, completed = fixtures.NativeInboundRetryTests.base_adapter(self)
            created.append((adapter, phases, completed))
            return adapter
        context.register_platform('durable-fixture', 'Durable fixture', factory, lambda: True)
        self.addCleanup(platform_registry.unregister, 'durable-fixture', scope=manager.scope_key)
        entry = platform_registry.get('durable-fixture')
        self.assertIsNotNone(entry)
        adapter = entry.adapter_factory(PlatformConfig(enabled=True, typing_indicator=False))
        self.assertIs(adapter, created[0][0])
        self.assertIs(adapter.requires_durable_input_ack, True)
        return created[0]

    async def test_registered_base_commits_each_original_delivery_before_own_ack_in_delete_mode(self):
        await fixtures.NativeInboundRetryTests.test_actual_active_base_keeps_distinct_provider_ids_and_bodies_unmerged(self)

    async def test_transaction_owner_keeps_compression_reset_and_branch_identities(self):
        db = self.authority.db
        db.create_session('root', source='telegram')
        db.end_session('root', 'compression')
        db.create_session('compressed', source='telegram', parent_session_id='root')
        db.create_session('reset', source='telegram', parent_session_id='root', model_config={'_reset_from': 'root'})
        db.create_session('branch', source='telegram', parent_session_id='root', model_config={'_branched_from': 'root'})
        db.create_session('tool', source='tool', parent_session_id='root')
        for sid in ('root', 'compressed', 'reset', 'branch', 'tool', 'missing'):
            with self.subTest(session_id=sid):
                expected = self.authority.logical_owner(sid)
                observed = db._execute_write(lambda conn: self.authority.logical_owner(sid, cursor=conn))
                self.assertEqual(observed, expected)
        self.assertEqual(self.authority.logical_owner('compressed'), 'root')
        self.assertEqual(self.authority.logical_owner('reset'), 'reset')
