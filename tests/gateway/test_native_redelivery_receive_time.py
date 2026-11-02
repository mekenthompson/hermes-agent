"""A platform redelivery after a gateway restart is the same message, not a conflicting one.

Socket Mode (Slack) and polling transports redeliver an event whose acknowledgement the stopping
gateway never sent. The new process's in-memory dedupe is empty, so the event reaches native
admission again under its original platform message id. The envelope's ``timestamp`` is the
gateway's receive time, so it differs on every delivery; that alone must not refuse the redelivery
as ``admission_conflict`` (the message would go unanswered). Changed content still conflicts.
"""
from datetime import UTC, datetime
import unittest

import pytest

from tests.gateway import test_native_inbound_retry as base
from tests.gateway.test_native_inbound_retry import no_bootstrap  # noqa: F401 - autouse fixture



class NativeRedeliveryReceiveTimeTests(unittest.IsolatedAsyncioTestCase):
    # The shared finite native fixture; a module-level alias would collect its tests here again.
    asyncSetUp = base.NativeInboundRetryTests.asyncSetUp
    start, settle = base.NativeInboundRetryTests.start, base.NativeInboundRetryTests.settle
    receive, row = base.NativeInboundRetryTests.receive, base.NativeInboundRetryTests.row

    def event(self, message_id, text='bounded instruction', *, received=datetime(2026, 11, 2, tzinfo=UTC)):
        from gateway.platforms.event import MessageEvent
        return MessageEvent(text=text, source=self.source, message_id=message_id, timestamp=received)

    async def restart(self, name):
        from gateway.session_authority import initialize_session_authority
        self.authority = await initialize_session_authority(self.runner, profile_id=str(self.home), instance_id=name)
        self.authority._schedule = lambda ref: self.scheduled.append(ref)

    async def admit(self, message_id, *, received, text='bounded instruction'):
        return await self.authority.admit_native(self.event(message_id, text, received=received))

    async def test_redelivery_with_a_later_receive_time_returns_the_original_receipt(self):
        for durable in (False, True):
            with self.subTest(durable_input_ack=durable):
                self.adapter.requires_durable_input_ack = durable
                message = f'slack-ts-{durable}'
                first = await self.admit(message, received=datetime(2026, 11, 2, 8, 5, 1, tzinfo=UTC))
                await self.restart(f'after-restart-{durable}')
                again = await self.admit(message, received=datetime(2026, 11, 2, 8, 6, 23, tzinfo=UTC))
                self.assertEqual(again.admission_id, first.admission_id)
                rows = [row for row in base_rows(self.authority, first.ref.session_id) if row['request_id'] == message]
                self.assertEqual(len(rows), 1)

    async def test_redelivered_terminal_message_after_restart_is_not_answered_again(self):
        self.adapter.requires_durable_input_ack = False
        self.runner._is_session_running = lambda route: False
        await self.start('answered')
        self.settle()
        await self.restart('second-boot')
        handled, reply = await self.receive(self.event('answered', received=datetime(2026, 11, 2, 9, tzinfo=UTC)))
        self.assertTrue(handled)
        self.assertIsNone(reply)
        self.assertEqual(self.row('answered')['status'], 'terminal')

    async def test_redelivery_with_changed_text_still_conflicts(self):
        from hermes_state_runtime import RuntimeStoreError
        self.adapter.requires_durable_input_ack = False
        await self.admit('edited-in-place', received=datetime(2026, 11, 2, 8, tzinfo=UTC))
        with pytest.raises(RuntimeStoreError) as refused:
            await self.admit('edited-in-place', received=datetime(2026, 11, 2, 8, 1, tzinfo=UTC), text='other words')
        self.assertEqual(refused.value.reason, 'admission_conflict')


def base_rows(authority, session_id):
    from hermes_state_runtime import list_session_admissions
    return list_session_admissions(authority.db, session_id=session_id, pending_only=False)
