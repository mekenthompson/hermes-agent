"""Busy effects and replies use the native commit; admission keeps its transport event."""
import asyncio
from dataclasses import replace
import threading
import unittest
from unittest.mock import patch

from tests.gateway import test_native_inbound_retry as fixtures

no_bootstrap = fixtures.no_bootstrap


class NativeBusyImmutableTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.NativeInboundRetryTests.asyncSetUp
    event = fixtures.NativeInboundRetryTests.event
    start = fixtures.NativeInboundRetryTests.start
    receive = fixtures.NativeInboundRetryTests.receive
    row = fixtures.NativeInboundRetryTests.row
    accepted = fixtures.NativeInboundRetryTests.accepted
    cancel_observer = fixtures.NativeInboundRetryTests.cancel_observer

    async def mutated_commit(self, event, mutate, *, queued=False, cancelled=False):
        committed, release = threading.Event(), threading.Event()
        original = self.authority._admit_native_write

        def held_write(**kwargs):
            result = original(**kwargs)
            committed.set()
            if not release.wait(5):
                raise AssertionError('native commit synchronization was not released')
            return result

        with patch.object(self.authority, '_admit_native_write', held_write):
            observer = asyncio.create_task(self.receive(event))
            try:
                self.assertTrue(await asyncio.to_thread(committed.wait, 5))
                self.assertEqual(self.agent.effects, [])
                mutate()
                if cancelled:
                    await self.cancel_observer(observer)
            finally:
                release.set()
            if cancelled:
                await asyncio.gather(*tuple(self.authority._mutation_tasks))
            elif queued:
                await self.accepted(event.message_id)
                await self.cancel_observer(observer)
            else:
                await asyncio.wait_for(observer, 5)

    async def test_text_source_media_and_mode_mutation_cannot_replace_busy_effects(self):
        from gateway.platforms.event import MessageType
        await self.start()
        from gateway.run_busy import GatewayBusySessionMixin
        origins, folded, acknowledgements, deliveries, transport_acks = [], [], [], [], []
        self.runner._steer_text_with_origin = lambda text, event: origins.append(event) or text
        self.runner._fold_into_running_turn = lambda agent, route, event: folded.append((route, event))
        self.runner._effective_busy_input_mode = lambda source: 'steer' if source.chat_id == 'fixture-chat' else 'queue'

        async def send(**kwargs):
            deliveries.append(kwargs)
        self.adapter._send_with_retry = send
        self.runner._reply_anchor_for_event = lambda event: event.reply_anchor_override or event.message_id
        self.runner._thread_metadata_for_source = lambda source, anchor: {'thread_id': source.thread_id}
        self.runner._busy_reply_to = GatewayBusySessionMixin._busy_reply_to
        original_hook = self.adapter.on_message_admitted

        async def transport_ack(event, receipt):
            transport_acks.append(event)
            await original_hook(event, receipt)
        self.adapter.on_message_admitted = transport_ack

        async def ack(event, route, adapter, *args, **kwargs):
            acknowledgements.append(event)
            await GatewayBusySessionMixin._send_busy_reply(self.runner, event, adapter, event.text)
        self.runner._busy_interrupt_and_ack = ack
        event = self.event('immutable-text', 'committed text', source=replace(self.source))

        def mutate():
            event.text = 'unadmitted replacement'
            event.source.chat_id = 'unadmitted route'
            event.source.user_id = 'unauthorized user'
            event.source.thread_id = 'unadmitted thread'
            event.message_id = 'unadmitted anchor'
            event.media_urls, event.media_types = ['unadmitted media'], ['image/png']
            event.message_type = MessageType.PHOTO

        await self.mutated_commit(event, mutate)
        self.assertEqual(self.agent.effects[0][1], 'committed text')
        self.assertEqual(self.row('immutable-text')['payload']['text'], 'committed text')
        self.assertEqual(origins[0].source.user_id, 'fixture-author')
        self.assertEqual(origins[0].source.chat_id, 'fixture-chat')
        self.assertEqual(folded, [(self.route, origins[0])])
        self.assertEqual(acknowledgements[0].text, 'committed text')
        self.assertEqual(deliveries, [{'chat_id': 'fixture-chat', 'content': 'committed text',
            'reply_to': 'immutable-text', 'metadata': {'thread_id': None}}])
        self.assertIs(transport_acks[0], event)
        self.assertTrue(event._gateway_accepted)

    async def test_redirect_and_interrupt_receive_only_committed_instruction(self):
        await self.start()
        self.mode = 'interrupt'
        anchored = []
        self.runner._reanchor_redirected_turn = lambda agent, route, event: anchored.append(event.text)
        event = self.event('immutable-redirect', 'committed redirect')
        await self.mutated_commit(event, lambda: setattr(event, 'text', 'unadmitted redirect'))
        self.assertEqual(self.agent.effects, [('redirect', 'committed redirect')])
        self.assertEqual(anchored, ['committed redirect'])
        self.agent.effects.clear()
        self.agent._supports_active_turn_redirect = False
        event = self.event('immutable-interrupt', 'committed interrupt')
        await self.mutated_commit(event, lambda: setattr(event, 'text', 'unadmitted interrupt'), queued=True)
        self.assertEqual(self.agent.effects, [('interrupt', 'committed interrupt')])

    async def test_removing_committed_media_cannot_turn_it_into_a_steer(self):
        await self.start()
        clip = self.home / 'committed.bin'
        clip.write_bytes(b'finite committed attachment')
        event = self.event('immutable-media')
        event.media_urls, event.media_types = [str(clip)], ['application/octet-stream']

        def mutate():
            event.media_urls.clear()
            event.media_types.clear()
        await self.mutated_commit(event, mutate, queued=True)
        self.assertEqual(self.agent.effects, [])
        self.assertEqual(self.row('immutable-media')['status'], 'queued')
        self.assertEqual(len(self.row('immutable-media')['payload']['native_text_v1']['media']), 1)

    async def test_changed_caller_cannot_replace_a_revoked_committed_requester(self):
        await self.start()
        event = self.event('immutable-requester', source=replace(self.source))

        def mutate():
            self.allowed.remove('fixture-author')
            event.source.user_id = 'other-author'
        await self.mutated_commit(event, mutate)
        self.assertEqual(self.agent.effects, [])
        self.assertEqual(self.row('immutable-requester')['outcome'], 'cancelled')

    def pending_approval(self):
        from tools import approval
        from tools.approval_gateway_wait import _ApprovalEntry
        pending = _ApprovalEntry({'request_id': 'committed-target', 'command': 'finite fixture'})
        with approval._lock:
            approval._gateway_queues[self.route] = [pending]
        self.addCleanup(approval._gateway_queues.pop, self.route, None)
        return pending

    async def test_committed_approval_keeps_its_answer_after_caller_mutation(self):
        from types import MethodType
        from gateway.run_busy import GatewayBusySessionMixin
        await self.start()
        pending = self.pending_approval()
        event = self.event('immutable-approval', 'yes', control=True, source=replace(self.source))
        confirmations, deliveries = [], []

        async def confirmation(event, text, verb):
            confirmations.append((event.text, event.source.chat_id, event.message_id, verb))
            return 'committed confirmation'

        async def send(**kwargs):
            deliveries.append(kwargs)
        self.runner._deliver_approval_confirmation = confirmation
        self.adapter._send_with_retry = send
        self.adapter._unwrap_ephemeral = lambda text: (text, None)
        self.runner._reply_anchor_for_event = lambda event: event.message_id
        self.runner._thread_metadata_for_source = lambda source, anchor: {'thread_id': source.thread_id}
        self.runner._send_busy_reply = MethodType(GatewayBusySessionMixin._send_busy_reply, self.runner)

        def mutate():
            event.text, event.allow_gateway_control = 'no', False
            event.source.chat_id, event.source.thread_id = 'unadmitted route', 'unadmitted thread'
            event.message_id = 'unadmitted anchor'
        await self.mutated_commit(event, mutate)
        self.assertEqual(pending.result, 'once')
        self.assertEqual(self.row('immutable-approval')['payload']['text'], 'yes')
        self.assertEqual(self.agent.effects, [])
        self.assertEqual(confirmations, [('yes', 'fixture-chat', 'immutable-approval', 'approve')])
        self.assertEqual(deliveries, [{'chat_id': 'fixture-chat', 'content': 'committed confirmation',
            'reply_to': 'immutable-approval', 'metadata': {'thread_id': None}}])

    async def test_changed_body_or_control_before_snapshot_cannot_borrow_approval_targets(self):
        import gateway.session_envelope as envelope
        await self.start()
        pending = self.pending_approval()
        original = envelope.prepare_native
        for field, value in [('text', 'no'), ('allow_gateway_control', False)]:
            with self.subTest(field=field):
                event = self.event('changed-before-snapshot-' + field, 'yes', control=True)

                async def changed(runner, incoming):
                    setattr(incoming, field, value)
                    return await original(runner, incoming)

                with patch.object(envelope, 'prepare_native', changed):
                    observer = asyncio.create_task(self.receive(event))
                    await self.accepted(event.message_id)
                    self.assertIsNone(pending.result)
                    self.assertEqual(self.agent.effects, [])
                    self.assertEqual(self.row(event.message_id)['status'], 'queued')
                    await self.cancel_observer(observer)

    async def test_cancelled_observer_continues_with_committed_body(self):
        await self.start()
        event = self.event('immutable-cancelled', 'committed after observer cancellation')
        await self.mutated_commit(event, lambda: setattr(event, 'text', 'unadmitted text'), cancelled=True)
        self.assertEqual(self.agent.effects[0][1], 'committed after observer cancellation')
        self.assertEqual(self.row('immutable-cancelled')['outcome'], 'steering')
