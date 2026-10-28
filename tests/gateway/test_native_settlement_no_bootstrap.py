"""Native result settlement needs no gateway bootstrap for owner-stop copy.

The finite runner/agent settings and actual SQLite admission fixture never run a
model. The existing guard rejects gateway.run/run_agent imports and activation.
"""
from types import SimpleNamespace
import unittest

from tests.gateway import test_native_inbound_retry as fixtures

no_bootstrap = fixtures.no_bootstrap


class NativeSettlementNoBootstrapTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.NativeInboundRetryTests.asyncSetUp
    event = fixtures.NativeInboundRetryTests.event
    start = fixtures.NativeInboundRetryTests.start

    async def test_completed_finish_result_commits_and_reuses_exact_reply_without_bootstrap(self):
        from gateway.session_results import admission_result, finish_result
        await self.start()
        settled, reply = finish_result(self.authority.db, epoch=self.authority.epoch,
            row=self.running, response='finite delivered result', outcome='completed')
        self.assertEqual((settled['status'], settled['outcome']), ('terminal', 'completed'))
        self.assertEqual(reply, 'finite delivered result')
        self.assertEqual(admission_result(self.authority.db, settled['admission_id'])['result']['final_response'], reply)
        _, retried = finish_result(self.authority.db, epoch=self.authority.epoch,
            row=self.running, response='different retry must not replace the result', outcome='completed')
        self.assertEqual(retried, reply)

    async def test_restart_and_shutdown_settle_with_identical_saved_notice_once(self):
        from gateway.session_results import finish_result
        from gateway.session_settlement_recovery import RESTART_INTERRUPT_NOTICE, SHUTDOWN_INTERRUPT_NOTICE
        for cause, expected in [('Gateway restarting', RESTART_INTERRUPT_NOTICE),
                                ('Gateway shutting down', SHUTDOWN_INTERRUPT_NOTICE)]:
            with self.subTest(cause=cause):
                await self.start(cause)
                self.authority.db.append_message(self.ref.session_id, role='user', content=cause)
                result = {'result': {'interrupted': True, 'interrupt_message': cause, 'messages': [], 'final_response': ''}}
                settled, _ = finish_result(self.authority.db, epoch=self.authority.epoch,
                    row=self.running, response='', outcome='interrupted', result=result)
                self.assertEqual(settled['outcome'], 'interrupted')
                messages = self.authority.db.get_messages(self.ref.session_id)
                self.assertEqual(sum(message['content'] == expected for message in messages), 1)
                finish_result(self.authority.db, epoch=self.authority.epoch,
                    row=self.running, response='', outcome='interrupted', result=result)
                messages = self.authority.db.get_messages(self.ref.session_id)
                self.assertEqual(sum(message['content'] == expected for message in messages), 1)

    async def test_owner_reason_and_user_stop_copy_stay_distinct_without_bootstrap(self):
        from gateway.run_shutdown import GatewayShutdownMixin
        from gateway.session_settlement_recovery import owner_stop_notice, RESTART_INTERRUPT_NOTICE
        reason = GatewayShutdownMixin._shutdown_interrupt_reason
        self.assertEqual(reason(SimpleNamespace(_restart_requested=True)), 'Gateway restarting')
        self.assertEqual(reason(SimpleNamespace(_restart_requested=False)), 'Gateway shutting down')
        self.assertEqual(owner_stop_notice({'result': {'interrupt_message': 'Gateway restarting'}},
                                          'interrupted'), RESTART_INTERRUPT_NOTICE)
        self.assertIsNone(owner_stop_notice({'result': {'interrupt_message': 'Gateway restarting'}}, 'completed'))
        self.assertIsNone(owner_stop_notice({'result': {'interrupted': True}}, 'interrupted'))
        self.assertIsNone(owner_stop_notice({'result': {'interrupt_message': 'User stopped'}}, 'interrupted'))
