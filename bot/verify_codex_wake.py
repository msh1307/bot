"""Behavior regressions: automatic callbacks, native requests, ordering and transport cleanup."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from evo_bot import context
from evo_bot.gateway import Bot
from evo_bot.integrations import codex


class WakeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = patch.object(codex, 'STATE', Path(self.tmp.name)/'jobs.json'); self.state.start()
        self.bot = Bot(); self.bot.sessions['77'] = object()
        self.b = codex.AppServer({'cwd': '/tmp'}, 'oracle')
        self.b.adapter = self.bot
        self.b.monitor_desktop = lambda job: None
        self.b.send = AsyncMock()
        self.turn = context.Turn('77'); self.token = context.current.set(self.turn)
        self.b.rpc = AsyncMock()

    async def asyncTearDown(self):
        self.bot.sessions.clear()
        await self.bot.close()
        context.current.reset(self.token); self.state.stop(); self.tmp.cleanup()

    def job(self, source='app_server'):
        job = {'job_id': 'j', 'thread_id': 't', 'turn_id': 'turn', 'state': 'running',
               'source': source, 'messages': {}}
        self.b.jobs['j'] = job
        self.b.arm_callback(job, {}, '77')
        return job

    async def test_default_automatic_both_sources(self):
        for source in ('desktop', 'app_server'):
            job = self.job(source)
            self.assertTrue(job['_wake'])
            self.assertTrue(self.b.queue_callback(job, source))
            item = self.bot.queue.get_nowait(); self.bot.queue.task_done()
            self.assertTrue(item['callback_valid']())
            data = json.loads(item['text'].splitlines()[1])
            self.assertEqual(data['instance'], 'oracle')
            self.assertEqual(data['thread_id'], 't')

    async def test_behind_owner_work_and_duplicate_event(self):
        self.bot.enqueue('77', 'owner task', [])
        job = self.job()
        self.assertTrue(self.b.queue_callback(job, 'complete:1'))
        self.assertTrue(self.b.queue_callback(job, 'complete:1'))
        self.assertEqual(self.bot.queue.qsize(), 2)
        self.assertEqual(self.bot.queue.get_nowait()['text'], 'owner task')
        self.assertTrue(self.bot.queue.get_nowait()['callback_valid']())

    async def test_new_instruction_reset_and_session_reject_stale(self):
        for mutation in ('revision','reset'):
            job = self.job()
            self.b.queue_callback(job, mutation)
            item = self.bot.queue.get_nowait()
            if mutation == 'revision': self.b.arm_callback(job, {}, '77')
            elif mutation == 'reset': self.bot.reset_epoch += 1
            else: self.bot.sessions['77'] = object()
            self.assertFalse(item['callback_valid']())

    async def test_explicit_wake_false(self):
        job = self.job(); self.b.arm_callback(job, {'wake': False}, '77')
        self.assertFalse(self.b.queue_callback(job, 'done'))

    async def test_omitted_wake_preserves_opt_out(self):
        job = self.job(); self.b.arm_callback(job, {'wake': False}, '77')
        self.b.arm_callback(job, {}, '77')
        self.assertFalse(job['_wake'])

    async def test_send_preserves_native_daybreak_model_and_approval(self):
        native = {'id': 't', 'model': 'gpt-cyber-native', 'daybreak': True,
                  'approvalPolicy': 'on-request', 'status': {'type': 'idle'}}
        self.b.rpc.side_effect = [{'thread': native}, {'turn': {'id': 'new'}}]
        result = await self.b.action({'action': 'send', 'thread_id': 't', 'prompt': 'continue'}, '77')
        self.assertEqual(result['model'], 'gpt-cyber-native')
        calls = self.b.rpc.call_args_list
        self.assertEqual(calls[0].args, ('thread/resume', {'threadId': 't', 'excludeTurns': True}))
        self.assertEqual(calls[1].args[1], {'threadId': 't', 'input': [{'type': 'text', 'text': 'continue'}]})

    async def test_ephemeral_readback_required_before_prompt(self):
        self.b.rpc.return_value = {'thread': {'id': 't'}}
        with self.assertRaises(codex.DesktopError):
            await self.b.action({'action': 'start', 'prompt': 'test', 'ephemeral': True}, '77')
        self.assertEqual(self.b.rpc.await_count, 1)

    async def test_queue_full_retries_completion(self):
        job = self.job(); job['state'] = 'completed'
        for i in range(self.bot.queue.maxsize): self.bot.enqueue('77', str(i), [])
        await self.b.notify_complete(job)
        self.assertTrue(job['_retry_completion'])
        self.b.send.assert_not_called()
        self.bot.queue.get_nowait()
        await self.b.notify_complete(job)
        self.assertNotIn('_retry_completion', job)
        self.assertEqual(job['_completion_delivery']['turn'], 'sent')

    async def test_reset_drops_completion_without_stray_discord_notice(self):
        job = self.job(); self.bot.reset_epoch += 1
        await self.b.notify_complete(job)
        self.b.send.assert_not_called(); self.assertTrue(self.bot.queue.empty())

    async def test_queue_full_has_event_deadline_not_infinite_retry(self):
        job=self.job(); self.bot.notify_queue_expired=AsyncMock()
        job['_callback_deadlines']={'completed:turn':codex.time.monotonic()-1}
        self.assertTrue(self.b.queue_callback(job,'completed:turn'))
        await asyncio.sleep(0)
        self.bot.notify_queue_expired.assert_awaited_once()
        self.assertTrue(self.bot.queue.empty())
        self.assertTrue(self.b.queue_callback(job,'completed:turn'))
        self.assertEqual(self.bot.notify_queue_expired.await_count,1)

    async def test_native_send_validation_and_no_replay(self):
        self.b.rpc.side_effect = [{'thread': {'id': 't', 'status': {'type': 'idle'}}}, {'turn': {'id': 'new'}}]
        result = await self.b.action({'action': 'send', 'thread_id': 't', 'prompt': 'hello'}, '77')
        self.assertTrue(result['wake']); self.assertEqual(result['turn_id'], 'new')
        self.assertEqual([c.args[0] for c in self.b.rpc.call_args_list], ['thread/resume','turn/start'])
        self.b.rpc.reset_mock(); self.b.rpc.side_effect = [{'thread': {'id': 't'}}, TimeoutError()]
        with self.assertRaises(TimeoutError):
            await self.b.action({'action': 'send', 'thread_id': 't', 'prompt': 'hello'}, '77')
        self.assertEqual(self.b.rpc.await_count, 2)

    async def test_unknown_thread_never_starts(self):
        for thread in ({}, {'id': 'other'}):
            self.b.rpc.reset_mock(); self.b.rpc.side_effect = None
            self.b.rpc.return_value = {'thread': thread}
            with self.assertRaises(codex.DesktopError):
                await self.b.action({'action': 'send', 'thread_id': 't', 'prompt': 'hello'}, '77')
            self.assertEqual(self.b.rpc.await_count, 1)

    async def test_active_send_steers_same_turn(self):
        self.b.rpc.side_effect = [{'thread': {'id':'t','status':{'type':'active'}}},
            {'data':[{'id':'active','status':'inProgress'}]}, {}]
        result = await self.b.action({'action':'send','thread_id':'t','prompt':'correction'}, '77')
        self.assertEqual(result['turn_id'], 'active')
        self.assertEqual(self.b.rpc.call_args.args[0], 'turn/steer')
        self.assertEqual(self.b.rpc.call_args.args[1]['expectedTurnId'], 'active')

    async def test_new_chat_and_invalid_wake(self):
        self.b.rpc.return_value = {'thread': {'id': 't'}}
        with self.assertRaises(ValueError):
            await self.b.action({'action':'start','prompt':'test','wake':'true'}, '77')
        self.b.rpc.assert_not_called()
        self.b.rpc.side_effect = [{'thread': {'id':'t'}}, {'turn': {'id':'turn'}}]
        result = await self.b.action({'action':'start','prompt':'test'}, '77')
        self.assertTrue(result['wake'])
        self.assertEqual(self.b.rpc.call_args_list[0].args[0], 'thread/start')

    async def test_model_facing_start_forwards_cwd_and_wake(self):
        from evo_bot.integrations import remote
        params = {'prompt': 'test', 'cwd': '/tmp/selected-project', 'wake': False}
        catalog = {'defaults': {'codex': 'oracle'},
                   'instances': [{'id': 'oracle', 'protocols': ['codex']}]}
        with patch.object(remote, 'catalog', return_value=catalog), \
                patch.object(codex, 'codex_task', side_effect=lambda args: args):
            routed = remote.remote_control({'instance': 'oracle', 'protocol': 'codex',
                                            'action': 'start', 'args': params})
        self.assertEqual(routed, dict(params, instance='oracle', action='start'))
        self.b.rpc.side_effect = [{'thread': {'id': 't'}}, {'turn': {'id': 'turn'}}]
        result = await self.b.action(routed, '77')
        self.assertFalse(result['wake'])
        self.assertEqual(self.b.rpc.call_args_list[0].args[1]['cwd'], params['cwd'])

    async def test_question_status_answer_and_duplicate_manual_answer(self):
        job = self.job(); ws = SimpleNamespace(send=AsyncMock()); self.b.ws = ws
        msg = {'id': 9, 'method':'item/tool/requestUserInput', 'params':{
            'threadId':'t','turnId':'turn','questions':[{'id':'q','question':'Which?','header':'Choice'}]}}
        await self.b.input_ui(ws,msg,job)
        self.assertEqual(self.b.pending_requests(job)[0]['request_id'], 9)
        self.assertEqual(self.bot.queue.qsize(),1)
        result = await self.b.action({'action':'answer','thread_id':'t','request_id':9,
                                     'answers': {'q':['test']}}, '77')
        self.assertTrue(result['wake']); self.assertEqual(job['state'],'running')
        self.assertEqual(json.loads(ws.send.call_args.args[0]), {'id':9,'result':{'answers':{'q':{'answers':['test']}}}})
        with self.assertRaises(ValueError):
            await self.b.resolve_input(job,9,{'q':['again']})
        self.assertEqual(ws.send.await_count,1)

    async def test_question_does_not_resolve_approval_or_stale_ws(self):
        job = self.job(); self.b.ws = SimpleNamespace(send=AsyncMock())
        record = {'id':3,'method':'item/commandExecution/requestApproval','params':{'turnId':'turn'},
                  'ws':self.b.ws,'lock':asyncio.Lock()}
        job['_requests']={'3':record}
        with self.assertRaises(ValueError): await self.b.resolve_input(job,3,{})
        record['method']='item/tool/requestUserInput'; self.b.ws=object()
        with self.assertRaises(ValueError): await self.b.resolve_input(job,3,{})

    async def test_completion_event_deduplicated_and_request_resolved(self):
        job=self.job()
        self.b.event(job,'turn/completed',{'turn':{'id':'turn','status':'completed'}})
        await asyncio.sleep(0)
        self.b.event(job,'turn/completed',{'turn':{'id':'turn','status':'completed'}})
        await asyncio.sleep(0)
        self.assertEqual(self.bot.queue.qsize(),1)
        self.b.event(job,'turn/started',{'turn':{'id':'other-client'}})
        self.b.event(job,'turn/completed',{'turn':{'id':'other-client','status':'completed'}})
        await asyncio.sleep(0)
        self.assertEqual(self.bot.queue.qsize(),1)
        self.assertEqual(job['turn_id'],'turn')

    async def test_restore_owner_task_even_if_pi_session_was_evicted(self):
        job=self.job(); self.b.save(); self.b.jobs.clear()
        b=codex.AppServer({'cwd':'/tmp'},'oracle'); b.adapter=self.bot
        b.monitor_desktop=unittest.mock.Mock()
        with patch.object(codex,'CLIENT',SimpleNamespace(wait_until_ready=AsyncMock())):
            await b.restore_watchers()
        b.monitor_desktop.assert_called_once()
        self.assertTrue(b.callback_current(b.jobs['j']))
        self.bot.sessions.clear(); b.monitor_desktop.reset_mock()
        with patch.object(codex,'CLIENT',SimpleNamespace(wait_until_ready=AsyncMock())):
            await b.restore_watchers()
        b.monitor_desktop.assert_called_once()

    async def test_callback_carries_fixed_owner_task_budget_and_instructions(self):
        self.turn.task_deadline=codex.time.time()+1800
        self.turn.task_instructions='Finish T, report, then shut down desktop'
        job=self.job()
        self.assertEqual(job['_task_deadline'],self.turn.task_deadline)
        self.assertTrue(self.b.queue_callback(job,'completed:turn'))
        item=self.bot.queue.get_nowait()
        self.assertEqual(item['task_deadline'],self.turn.task_deadline)
        self.assertIn('then shut down',item['text'])
        self.assertEqual(item['task_instructions'],self.turn.task_instructions)
        self.assertTrue(1799 < item['queue_deadline']-codex.time.monotonic() <= 1800)
        self.bot.sessions.clear()
        self.assertTrue(item['callback_valid']())

    async def test_expired_task_does_not_restore_watcher(self):
        job=self.job(); job['_task_deadline']=codex.time.time()-1; self.b.save()
        b=codex.AppServer({'cwd':'/tmp'},'oracle'); b.adapter=self.bot
        b.monitor_desktop=unittest.mock.Mock()
        with patch.object(codex,'CLIENT',SimpleNamespace(wait_until_ready=AsyncMock())):
            await b.restore_watchers()
        b.monitor_desktop.assert_not_called()


if __name__ == '__main__': unittest.main(verbosity=2)
