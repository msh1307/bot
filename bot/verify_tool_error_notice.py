"""Tool failure reporting must not change recovery, repeat actions or leak arguments."""
import unittest
from unittest.mock import AsyncMock
from types import SimpleNamespace
from evo_bot import context,gateway,rpc


class Notices(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.error=ConnectionError('private-password-must-not-appear')
        async def handler(*_): raise self.error
        self.session=rpc.PiSession('notice-test','model',[],handler)
        self.session.send=AsyncMock()
        self.messages=[]
        self.session.on_progress=self.messages.append
        self.turn=context.Turn('123')

    async def call(self,name='browser'):
        await self.session.host_call({'id':'fixture','toolName':name,'arguments':{}},self.turn)

    async def test_notice_is_safe_and_original_error_still_reaches_model(self):
        await self.call()
        self.assertEqual(self.messages,['⚠️ browser · 연결 실패'])
        reply=self.session.send.call_args.args[0]
        self.assertTrue(reply['isError'])
        self.assertIn('private-password',reply['result']['content'][0]['text'])
        self.assertNotIn('private-password',''.join(self.messages))

    async def test_repeated_failure_is_reported_once_per_tool(self):
        await self.call();await self.call();await self.call('web_read')
        self.assertEqual(len(self.messages),2)
        self.assertEqual(self.session.send.await_count,3)

    async def test_timeout_label(self):
        self.error=TimeoutError('private endpoint')
        await self.call()
        self.assertEqual(self.messages,['⚠️ browser · 응답 시간 초과 · 실행 상태 확인'])

    async def test_permission_and_invalid_request_have_distinct_labels(self):
        self.error=PermissionError('synthetic approval refused');await self.call()
        self.error=ValueError('synthetic invalid path');await self.call('file_transfer')
        self.assertEqual(self.messages,['⚠️ browser · 승인·권한 확인 필요','⚠️ file_transfer · 입력값 확인 필요'])

    async def test_journal_records_only_safe_failure_metadata(self):
        with self.assertLogs('evo-bot.rpc',level='WARNING') as log:
            await self.session.host_call({'id':'fixture','toolName':'browser','arguments':{'action':'fill','value':'private-password'}},self.turn)
        self.assertIn('tool_failed tool=browser action=fill error_type=ConnectionError',log.output[0])
        self.assertNotIn('private-password',''.join(log.output))

    async def test_root_refusal_remains_an_explicit_nonexecuted_permission_result(self):
        from evo_bot.integrations import coding
        async def refused(*_):
            return coding.checked({'success':False,'approval_status':'denied_or_expired','error':'Not approved; no action ran.'})
        self.session.handler=refused;await self.call('_pi_exec')
        self.assertEqual(self.messages,['⚠️ exec · 승인·권한 확인 필요'])
        reply=self.session.send.call_args.args[0]
        self.assertTrue(reply['isError']);self.assertIn('no action ran',reply['result']['content'][0]['text'])

    async def test_callback_failure_cannot_suppress_tool_error_reply(self):
        def broken(_): raise RuntimeError('UI unavailable')
        self.session.on_progress=broken
        await self.call()
        self.assertTrue(self.session.send.call_args.args[0]['isError'])

    async def test_cancellation_is_not_reported_as_a_tool_failure(self):
        self.turn.cancelled.set()
        await self.call()
        self.assertFalse(self.messages)

    async def test_next_turn_can_report_the_same_tool_again(self):
        await self.call()
        async def reject(data):
            self.session.pending[data['id']].set_result({'success':False,'error':'synthetic rejection'})
        self.session.send=reject
        with self.assertRaisesRegex(RuntimeError,'synthetic rejection'):
            await self.session.prompt('new turn',self.turn)
        self.session.send=AsyncMock()
        await self.call()
        self.assertEqual(len(self.messages),2)

    async def test_no_progress_subscriber_preserves_existing_error_behavior(self):
        self.session.on_progress=None
        await self.call()
        self.assertTrue(self.session.send.call_args.args[0]['isError'])

    async def test_gateway_forwards_notice_and_fallback_can_complete(self):
        bot=gateway.Bot();bot.deliver=AsyncMock(return_value=True)
        channel=SimpleNamespace(id=123)
        async def fallback(*_):return {'success':True,'text':'fixture page content'}
        try:
            async with bot.progress(channel,self.session):
                await self.call()
                self.session.handler=fallback
                await self.call('web_read')
            self.assertEqual(bot.deliver.await_count,1)
            self.assertEqual(bot.deliver.call_args.args[1],'⚠️ browser · 연결 실패')
            reply=self.session.send.call_args.args[0]
            self.assertNotIn('isError',reply)
            self.assertIn('fixture page content',reply['result']['content'][0]['text'])
        finally:await bot.close()


if __name__=='__main__':unittest.main()
