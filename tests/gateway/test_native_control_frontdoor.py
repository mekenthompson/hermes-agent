"""Finite public adapter/control seams; no gateway or model bootstrap.

Base dispatch, pause gate, native auth/envelope/admission and Stop are real. The
runner's actor allowlist, pause notice and transport endpoint are finite fixtures.
"""
from types import MethodType
import unittest
from unittest.mock import patch

from tests.gateway import test_native_inbound_retry as fixtures
from tests.gateway import test_native_structured_stop as stop_fixtures

no_bootstrap = fixtures.no_bootstrap


class NativeControlFrontdoorTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.NativeInboundRetryTests.asyncSetUp
    event = fixtures.NativeInboundRetryTests.event
    start = fixtures.NativeInboundRetryTests.start
    settle = fixtures.NativeInboundRetryTests.settle
    receive = fixtures.NativeInboundRetryTests.receive
    row = fixtures.NativeInboundRetryTests.row
    base_adapter = fixtures.NativeInboundRetryTests.base_adapter
    stop = stop_fixtures.NativeStructuredStopTests.stop

    async def test_base_rejects_malformed_control_before_any_inline_command_handler(self):
        from hermes_state_runtime import RuntimeStoreError
        from gateway.platforms.event import MessageType
        adapter, _, _ = self.base_adapter()
        called = []
        async def handler(event):
            called.append(event)
        adapter.set_message_handler(handler)
        cases = [dict(text='/stop', allow_gateway_control=True), dict(internal=True),
            dict(structured_control='stop'), dict(structured_control='unknown'),
            dict(message_type=MessageType.PHOTO), dict(media_urls=['unopened.bin']),
            dict(media_text_inlined=[True]), dict(edit_revision='edit'), dict(message_id=None),
            dict(prompt_response={'prompt_id': 'unowned'})]
        for changes in cases:
            with self.subTest(changes=changes):
                event = self.stop()
                for name, value in changes.items():
                    setattr(event, name, value)
                with self.assertRaises(RuntimeStoreError) as refused:
                    await adapter.handle_message(event)
                self.assertEqual(refused.exception.reason, 'invalid_params')
        self.assertEqual(called, [])
        self.assertEqual(self.receipts, [])

    async def test_authority_frontdoor_refuses_malformed_control_instead_of_command_fallback(self):
        from hermes_state_runtime import RuntimeStoreError
        event = self.stop()
        event.text, event.allow_gateway_control = '/stop', True
        with self.assertRaises(RuntimeStoreError) as refused:
            await self.receive(event)
        self.assertEqual(refused.exception.reason, 'invalid_params')
        self.assertEqual(self.old_callbacks, [])
        self.assertEqual(self.receipts, [])

    async def test_valid_stop_reaches_idle_owned_cleanup_under_global_pause(self):
        from gateway.run_inbound import GatewayInboundMixin
        from gateway.session_ancillary import process_stop_pending
        await self.start()
        await self.receive(self.stop('initial-stop'))
        self.settle('interrupted')
        self.authority.executing.pop(self.ref.session_id, None)
        self.assertIsNotNone(process_stop_pending(self.authority.db, self.ref.session_id))
        self.runner._session_key_for_source = lambda source: self.route
        self.runner._is_session_running = lambda route: False
        self.runner._hm_estop_turn_allowed = MethodType(GatewayInboundMixin._hm_estop_turn_allowed, self.runner)
        adapter, _, _ = self.base_adapter()
        gate_results = []
        async def handler(event):
            # Finite allowlist is the runner fixture's normal actor check. The
            # native codec rechecks it before any control commit or effect.
            if not self.runner._is_user_authorized_for_source(event.source):
                return None
            notice = GatewayInboundMixin._hm_estop_gate(self.runner, event, event.source, False)
            gate_results.append(notice)
            if notice is not None:
                return notice
            _, reply = await self.receive(event)
            return reply
        adapter.set_message_handler(handler)
        with patch('agent.estop.paused_reply', return_value='finite global pause notice'):
            ordinary = GatewayInboundMixin._hm_estop_gate(self.runner, self.event('status'), self.source, False)
            self.assertEqual(ordinary, 'finite global pause notice')
            await adapter.handle_message(self.stop('paused-idle-stop'))
        self.assertEqual(gate_results, [None])
        self.assertEqual(self.row('paused-idle-stop')['outcome'], 'cancelled')
        receipt = self.receipts[-1][1]
        self.assertTrue(receipt.control.request_consumed)
        self.assertFalse(receipt.control.cleanup_pending)

    async def test_invalid_or_unsupported_controls_do_not_pass_pause_gate(self):
        from gateway.run_inbound import GatewayInboundMixin
        self.runner._session_key_for_source = lambda source: self.route
        self.runner._is_session_running = lambda route: True
        event = self.stop()
        event.internal = True
        self.assertFalse(GatewayInboundMixin._hm_estop_turn_allowed(self.runner, event, self.source))
        event.internal = False
        self.adapter.requires_durable_input_ack = False
        self.assertFalse(GatewayInboundMixin._hm_estop_turn_allowed(self.runner, event, self.source))
