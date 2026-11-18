"""Offline task/timer admission regressions; no model calls or Discord sends."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
import tempfile
import time
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from evo_bot import context, gateway, tools
from evo_bot.integrations import terminal


class QueueTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = patch.object(tools, 'STATE', self.root); self.state.start()
        self.ignore = patch.object(gateway, 'IGNORE_PATH', self.root/'ignore.json'); self.ignore.start()
        self.bot = gateway.Bot()
        self.bot.sessions['77'] = object()
        self.bot.deliver = AsyncMock(return_value=True)
        self.bot.get_channel = lambda ident: object()
        self.bot.write_status = lambda state: None
        self.turn = context.Turn('77', model='sc117-flash-next-iq3_xxs')
        self.token = context.current.set(self.turn)

    async def asyncTearDown(self):
        self.bot.sessions.clear()
        await self.bot.close()
        context.current.reset(self.token)
        self.ignore.stop(); self.state.stop(); self.tmp.cleanup()

    def saved(self):
        return tools.load_jobs()

    async def test_late_bound_owner_reply_wakes_goal_without_unrelated_message_capture(self):
        ident = tools.schedule({'action': 'enqueue', 'prompt': 'Proceed after owner confirms scope'})['id']
        claim = self.bot.claim_due()[0]
        self.turn.job_id, self.turn.task_run = ident, claim['last_run_at']
        self.bot.bind_task_reply(self.turn, 123)
        tools.schedule({'action': 'note', 'state': 'waiting', 'text': 'Await scope clarification'})
        self.bot.finish_job(claim, 'ok')
        message = SimpleNamespace(author=SimpleNamespace(bot=False, id=int(context.OWNER)),
            content='Use the original scope only', channel=SimpleNamespace(id=77), attachments=[],
            reference=SimpleNamespace(message_id=123))
        await self.bot.on_message(message)
        self.assertEqual(self.bot.queue.qsize(), 0)
        again = self.bot.claim_due()[0]
        self.assertEqual(again['id'], ident)
        self.assertIn('Use the original scope only', again['wake_note'])

    async def test_unrelated_chat_does_not_resume_waiting_goal(self):
        ident = tools.schedule({'action': 'enqueue', 'prompt': 'Wait for a specific answer'})['id']
        claim = self.bot.claim_due()[0]
        self.turn.job_id, self.turn.task_run = ident, claim['last_run_at']
        tools.schedule({'action': 'note', 'state': 'waiting', 'text': 'Await input'})
        self.bot.finish_job(claim, 'ok')
        message = SimpleNamespace(author=SimpleNamespace(bot=False, id=int(context.OWNER)),
            content='Different question', channel=SimpleNamespace(id=77), attachments=[], reference=None)
        self.bot.interrupt_compaction = AsyncMock()
        await self.bot.on_message(message)
        self.assertEqual(self.bot.queue.get_nowait()['text'], 'Different question')
        self.bot.queue.task_done()
        self.assertFalse(self.saved()[0]['enabled'])

    async def test_worker_and_scheduler_continue_goal_then_stop_on_done(self):
        calls, done = [], asyncio.Event()
        async def prompt(text, turn, images):
            calls.append((turn.job_id, text))
            tools.schedule({'action': 'note', 'state': 'continue' if len(calls) == 1 else 'done',
                            'text': 'A verified; next B' if len(calls) == 1 else 'A and B verified'})
            if len(calls) == 2: done.set()
            return 'Step verified'
        session = self.worker_environment(prompt)
        ident = tools.schedule({'action': 'enqueue', 'prompt': 'Verify A then B'})['id']
        with patch.object(gateway, 'context_footer', AsyncMock(return_value='')):
            workers = [asyncio.create_task(self.bot.work_loop()), asyncio.create_task(self.bot.schedule_loop())]
            try:
                await asyncio.wait_for(done.wait(), 8)
                await asyncio.wait_for(self.bot.queue.join(), 1)
                self.assertEqual([c[0] for c in calls], [ident, ident])
                self.assertTrue(all('Verify A then B' in text for _, text in calls))
                self.assertIn('A verified; next B', calls[1][1])
                self.assertEqual(self.saved()[0]['task_state'], 'done')
                self.assertFalse(self.saved()[0]['enabled'])
                self.assertEqual(session.compact.await_count, 2)
            finally:
                for worker in workers: worker.cancel()
                await asyncio.gather(*workers, return_exceptions=True)

    async def test_enqueue_is_immediate_single_continuation(self):
        out = tools.schedule({'action':'enqueue','prompt':'Check existing Codex task'})
        self.assertTrue(out['continuation'])
        self.assertEqual(out['queue_timeout_seconds'],43200)
        due = self.bot.claim_due()
        self.assertEqual(len(due),1)
        self.assertEqual(due[0]['id'],out['id'])
        self.assertEqual(due[0]['model'],self.turn.model)
        self.assertEqual(self.bot.claim_due(),[])

    async def test_defer_delay_bounds_and_long_alarm_unchanged(self):
        out = tools.schedule({'action':'defer','after_seconds':600,'prompt':'Resume later'})
        delay=(datetime.fromisoformat(out['next_run_at'])-datetime.now(tools.TZ)).total_seconds()
        self.assertTrue(599 <= delay <= 600)
        for seconds in (0,601,True):
            with self.assertRaises(ValueError):
                tools.schedule({'action':'defer','after_seconds':seconds,'prompt':'no'})
        tomorrow=(datetime.now(tools.TZ)+timedelta(days=1)).isoformat()
        alarm=tools.schedule({'action':'create','at':tomorrow,'name':'Tomorrow','prompt':'Normal reminder'})
        self.assertFalse(alarm['continuation'])

    async def test_callbacks_do_not_have_a_separate_eight_job_limit(self):
        for _ in range(12):
            tools.schedule({'action':'defer','after_seconds':600,'prompt':'Independent authorized task'})
        self.assertEqual(len(self.saved()),12)

    async def test_finished_history_does_not_consume_active_schedule_capacity(self):
        out=tools.schedule({'action':'create','at':(datetime.now(tools.TZ)+timedelta(minutes=1)).isoformat(),
                            'name':'History','prompt':'Completed check'})
        old=self.saved()[0]
        old.update(enabled=False,running=False,task_state='done',last_status='ok')
        tools.save_jobs([dict(old,id=str(i)) for i in range(70)])
        created=tools.schedule({'action':'defer','after_seconds':600,'prompt':'New work'})
        self.assertTrue(any(j['id']==created['id'] for j in self.saved()))
        self.assertEqual(len(self.saved()),71)
        jobs=self.saved()
        for job in jobs:job.update(enabled=False,running=False,task_state='waiting',last_status='paused')
        tools.save_jobs(jobs)
        tools.schedule({'action':'defer','after_seconds':600,'prompt':'Paused history does not block new work'})
        self.assertEqual(len(self.saved()),72)

    async def test_active_schedule_capacity_still_bounds_resource_use(self):
        for _ in range(64):
            tools.schedule({'action':'defer','after_seconds':600,'prompt':'Waiting task'})
        with self.assertRaisesRegex(ValueError,'64 active'):
            tools.schedule({'action':'defer','after_seconds':600,'prompt':'Overflow'})

    async def test_agent_configurable_wait_is_bounded(self):
        for seconds in (0,43201,True):
            with self.assertRaises(ValueError):
                tools.schedule({'action':'enqueue','prompt':'no','queue_timeout_seconds':seconds})
        out=tools.schedule({'action':'enqueue','prompt':'ok','queue_timeout_seconds':20})
        self.assertEqual(out['queue_timeout_seconds'],20)
        tools.schedule({'action':'update','id':out['id'],'queue_timeout_seconds':30})
        self.assertEqual(self.saved()[0]['queue_timeout_seconds'],30)

    async def test_terminal_review_uses_existing_callback_queue_and_cancels_on_reset(self):
        client = terminal.Terminal('oracle')
        client.sessions['abc-1'] = {'created': time.monotonic()-7201, 'channel': '77', 'notice': 0}
        with patch.dict(terminal.CLIENTS, {'oracle': client}, clear=True), \
                patch.object(client, 'running', return_value=True):
            terminal.notify_due(self.bot)
            self.assertEqual(self.bot.queue.qsize(), 1)
            item = self.bot.queue.get_nowait()
            self.bot.queue.task_done()
            self.assertTrue(item['callback_valid']())
            self.assertTrue(item['compact'])
            self.assertEqual(item['channel'], '77')
            self.assertIsNone(item['job'])
            self.assertIn('Do not restart/replay', item['text'])
            self.bot.epochs['77'] = 1
            self.assertFalse(item['callback_valid']())

    async def test_callback_cannot_be_moved_past_ten_minutes(self):
        out=tools.schedule({'action':'defer','after_seconds':1,'prompt':'callback'})
        with self.assertRaises(ValueError):
            tools.schedule({'action':'update','id':out['id'],
                'at':(datetime.now(tools.TZ)+timedelta(hours=1)).isoformat()})

    async def test_same_fifo_for_owner_timer_and_codex(self):
        self.bot.enqueue('77','Owner',[])
        task=tools.schedule({'action':'enqueue','prompt':'Timer'})
        claim=self.bot.claim_due()[0]
        self.bot.enqueue('77',claim['prompt'],[],claim)
        scope=self.bot.codex_callback_scope('77')
        self.assertTrue(self.bot.enqueue_codex_callback('77','Codex',scope,lambda:True))
        self.assertEqual([self.bot.queue.get_nowait()['text'] for _ in range(3)],['Owner','Timer','Codex'])

    async def test_codex_retry_uses_remaining_event_deadline(self):
        scope=self.bot.codex_callback_scope('77')
        self.assertTrue(self.bot.enqueue_codex_callback('77','Codex',scope,lambda:True,timeout_seconds=.25))
        item=self.bot.queue.get_nowait()
        self.assertTrue(0 < item['queue_deadline']-time.monotonic() <= .25)
        with patch.object(gateway.time,'monotonic',return_value=item['queue_deadline']+.01):
            with self.assertRaises(gateway.QueueExpired):
                async with self.bot.queued_admission(item):
                    self.fail('Expired retried callback entered execution')

    async def test_internal_timeout_validation_and_existing_callback_contract(self):
        scope=self.bot.codex_callback_scope('77')
        for seconds in (0,-1,43201,True,float('nan'),float('inf')):
            with self.assertRaises(ValueError):
                self.bot.enqueue_codex_callback('77','bad',scope,lambda:True,timeout_seconds=seconds)
        self.assertTrue(self.bot.queue.empty())
        self.assertFalse(self.bot.enqueue_codex_callback('77','stale',None,lambda:True,timeout_seconds=.1))
        self.assertTrue(self.bot.enqueue_codex_callback('77','normal',scope,lambda:True))
        item=self.bot.queue.get_nowait()
        self.assertTrue(43199 < item['queue_deadline']-time.monotonic() <= 43200)

    async def test_sweep_expires_without_waiting_for_current_work(self):
        self.bot.enqueue('77','Keep first',[])
        task=tools.schedule({'action':'enqueue','prompt':'Expired','queue_timeout_seconds':1})
        claim=self.bot.claim_due()[0]
        self.bot.enqueue('77',claim['prompt'],[],claim)
        self.bot.enqueue('77','Keep last',[])
        # Advance admission time rather than sleeping or running an inference.
        with patch.object(gateway.time,'monotonic',return_value=time.monotonic()+2):
            await self.bot.expire_queued()
        self.assertEqual([self.bot.queue.get_nowait()['text'] for _ in range(2)],['Keep first','Keep last'])
        self.assertEqual(self.saved()[0]['last_status'],'queue_expired')
        self.assertFalse(self.saved()[0]['running'])
        self.bot.deliver.assert_awaited_once()

    async def test_full_queue_due_job_expires_without_being_claimed(self):
        out=tools.schedule({'action':'enqueue','prompt':'stale'})
        jobs=self.saved()
        jobs[0]['next_run_at']=(datetime.now(tools.TZ)-timedelta(seconds=43201)).isoformat()
        tools.save_jobs(jobs)
        expired=self.bot.claim_due(0)
        self.assertEqual(len(expired),1)
        self.assertTrue(expired[0]['expired_before_queue'])
        self.assertFalse(self.saved()[0]['enabled'])
        self.assertEqual(self.saved()[0]['last_status'],'queue_expired')
        self.assertEqual(self.bot.claim_due(0),[])

    async def test_recurring_missed_run_expires_and_next_run_survives(self):
        out=tools.schedule({'action':'create','name':'Recurring','prompt':'check','cron':'*/15 * * * *'})
        jobs=self.saved()
        jobs[0]['next_run_at']=(datetime.now(tools.TZ)-timedelta(seconds=43201)).isoformat()
        tools.save_jobs(jobs)
        expired=self.bot.claim_due(0)
        self.assertTrue(expired[0]['expired_before_queue'])
        saved=self.saved()[0]
        self.assertTrue(saved['enabled'])
        self.assertGreater(datetime.fromisoformat(saved['next_run_at']),datetime.now(tools.TZ))

    async def test_due_age_is_not_reset_by_claim_or_enqueue(self):
        due=(datetime.now(tools.TZ)-timedelta(seconds=590)).isoformat()
        self.bot.enqueue('77','aging',[],{'due_at':due,'queue_timeout_seconds':600})
        item=self.bot.queue.get_nowait()
        self.assertTrue(8 <= item['queue_deadline']-time.monotonic() <= 10)

    async def test_admission_deadline_while_lock_is_busy(self):
        await self.bot.admission_lock.acquire()
        item={'queue_deadline':time.monotonic()+.01}
        with self.assertRaises(gateway.QueueExpired):
            async with self.bot.queued_admission(item):
                self.fail('Expired task entered execution')
        self.assertTrue(self.bot.admission_lock.locked())
        self.bot.admission_lock.release()
        async with self.bot.queued_admission({'queue_deadline':time.monotonic()+1}):
            self.assertTrue(self.bot.admission_lock.locked())
        self.assertFalse(self.bot.admission_lock.locked())

    async def test_reset_makes_expired_callback_silent(self):
        scope=self.bot.codex_callback_scope('77')
        self.bot.enqueue_codex_callback('77','old',scope,lambda:True)
        item=self.bot.queue.get_nowait()
        self.bot.reset_epoch+=1
        await self.bot.notify_queue_expired(item)
        self.bot.deliver.assert_not_called()

    async def test_paused_claim_expiration_is_silent(self):
        out=tools.schedule({'action':'enqueue','prompt':'will pause'})
        claim=self.bot.claim_due()[0]
        tools.schedule({'action':'pause','id':out['id']})
        await self.bot.notify_queue_expired({'channel':'77','job':claim})
        self.bot.deliver.assert_not_called()

    async def test_cron_run_cannot_push_new_tasks(self):
        self.turn.cron=True
        for action in ('enqueue','defer'):
            with self.assertRaises(PermissionError):
                tools.schedule({'action':action,'after_seconds':1,'prompt':'no'})

    async def test_task_note_and_lifetime_survive_multiple_callbacks(self):
        result = tools.schedule({'action':'enqueue','prompt':'Watch then report and shut down'})
        original = self.saved()[0]
        self.assertEqual(result['mode'],'task')
        self.assertTrue(43199 <= original['task_deadline']-time.time() <= 43201)
        self.turn.job_id = result['id']; self.turn.task_deadline = original['task_deadline']
        tools.schedule({'action':'note','text':'Thread T running; report before shutdown'})
        self.assertEqual(tools.schedule({'action':'read'})['job']['id'],result['id'])
        child=tools.schedule({'action':'defer','after_seconds':600,'prompt':'Recheck T'})
        follow=next(j for j in self.saved() if j['id']==child['id'])
        self.assertEqual(follow['task_deadline'],original['task_deadline'])
        self.assertEqual(follow['note'],'Thread T running; report before shutdown')
        self.turn.task_deadline=time.time()+10
        with self.assertRaises(ValueError):
            tools.schedule({'action':'defer','after_seconds':600,'prompt':'No extension'})

    async def test_stop_cancels_one_shot_tasks_but_keeps_recurring_read_only_alarm(self):
        at=(datetime.now(tools.TZ)+timedelta(minutes=2)).isoformat()
        task=tools.schedule({'action':'create','at':at,'name':'Shutdown','prompt':'Verify first'})
        alarm=tools.schedule({'action':'create','cron':'0 9 * * *','mode':'check','name':'Briefing','prompt':'Read only'})
        tools.cancel_deferred('77')
        jobs={j['id']:j for j in self.saved()}
        self.assertFalse(jobs[task['id']]['enabled'])
        self.assertTrue(jobs[alarm['id']]['enabled'])

    async def test_read_only_one_shot_can_recheck_without_gaining_execution_rights(self):
        from evo_bot.integrations import coding
        at=(datetime.now(tools.TZ)+timedelta(minutes=1)).isoformat()
        result=tools.schedule({'action':'create','at':at,'mode':'check','name':'Monitor','prompt':'Read status until complete; no execution'})
        jobs=self.saved();jobs[0]['next_run_at']=datetime.now(tools.TZ).isoformat();tools.save_jobs(jobs)
        calls=[]
        async def prompt(text,turn,images):
            self.assertTrue(turn.cron);self.assertTrue(turn.task_run)
            self.assertIsNone(context.owner_message());self.assertIn('Read-only check',text)
            with patch.object(coding,'execute') as execute,self.assertRaises(PermissionError):
                await self.bot.registry.call('_pi_exec',{'instance':'desktop','command':'SYNTHETIC'},turn)
            execute.assert_not_called()
            self.assertFalse(self.saved()[0]['enabled']);self.assertTrue(self.saved()[0]['running'])
            follow=tools.schedule({'action':'note','state':'waiting','after_seconds':600,'text':'Read same status again'})
            self.assertEqual(follow['job_id'],result['id']);calls.append(turn.task_deadline)
            return 'Still running'
        session=self.worker_environment(prompt);claim=self.bot.claim_due()[0]
        worker=asyncio.create_task(self.bot.work_loop())
        try:
            self.bot.enqueue('77',claim['prompt'],[],claim);await asyncio.wait_for(self.bot.queue.join(),2)
            self.assertEqual(calls,[claim['task_deadline']])
            saved=self.saved()[0];self.assertEqual(saved['mode'],'check');self.assertEqual(saved['task_state'],'waiting')
            self.assertTrue(saved['enabled']);self.assertEqual(saved['task_deadline'],claim['task_deadline'])
            session.compact.assert_not_awaited();session.close.assert_awaited_once()
        finally:worker.cancel();await asyncio.gather(worker,return_exceptions=True)

    async def test_legacy_pending_check_gets_run_lease_but_terminal_checks_do_not_revive(self):
        at=(datetime.now(tools.TZ)+timedelta(minutes=1)).isoformat()
        tools.schedule({'action':'create','at':at,'mode':'check','name':'Legacy','prompt':'Observe'})
        jobs=self.saved();job=jobs[0]
        for key in ('task_state','task_deadline'):job.pop(key)
        job['next_run_at']=datetime.now(tools.TZ).isoformat();tools.save_jobs(jobs)
        claim=self.bot.claim_due()[0];self.assertIn('task_state',claim);self.assertIn('task_deadline',claim)
        self.turn.cron=True;self.turn.job_id=claim['id'];self.turn.task_run=claim['last_run_at']
        tools.schedule({'action':'note','state':'done','text':'Status verified'})
        self.bot.finish_job(claim,'ok');self.assertFalse(self.bot.claim_due())
        self.assertEqual(self.saved()[0]['task_state'],'done')
        with self.assertRaises(InterruptedError):tools.schedule({'action':'defer','after_seconds':1,'prompt':'Do not revive'})

    async def test_stop_revokes_one_shot_read_only_continuation(self):
        at=(datetime.now(tools.TZ)+timedelta(minutes=1)).isoformat()
        tools.schedule({'action':'create','at':at,'mode':'check','name':'Check','prompt':'Observe'})
        tools.cancel_deferred('77');self.assertFalse(self.saved()[0]['enabled']);self.assertEqual(self.saved()[0]['task_state'],'blocked')

    async def test_expired_owner_task_has_no_tool_authority(self):
        self.turn.task_deadline=time.time()-1
        self.assertIsNone(context.owner_identity())
        with self.assertRaises(PermissionError):
            tools.schedule({'action':'enqueue','prompt':'Cannot renew'})

    async def test_expired_due_task_never_enters_queue(self):
        out=tools.schedule({'action':'enqueue','prompt':'expired task'})
        jobs=self.saved(); jobs[0]['task_deadline']=time.time()-1; tools.save_jobs(jobs)
        claims=self.bot.claim_due()
        self.assertEqual(len(claims),1)
        self.assertTrue(claims[0]['expired_before_queue'])
        self.assertEqual(self.saved()[0]['last_status'],'task_expired')

    async def test_scheduled_task_has_tools_and_can_execute_requested_followup(self):
        from evo_bot.integrations import remote, codex, coding
        actions=[]
        async def prompt(text,turn,images):
            self.assertFalse(turn.cron)
            self.assertTrue(turn.deferred)
            self.assertIs(context.owner_message(),turn)
            self.assertTrue(turn.task_deadline > time.time())
            self.assertIn('remote_control',{d['name'] for d in self.bot.registry.definitions(turn.cron)})
            await self.bot.registry.call('_pi_exec',{'instance':'desktop',
                'command':'SYNTHETIC-NO-EXECUTION'},turn)
            actions.append('followup')
            tools.schedule({'action': 'note', 'state': 'done', 'text': 'Requested followup verified'})
            return 'completed'
        session=self.worker_environment(prompt)
        session.key='task:fixture'
        out=tools.schedule({'action':'enqueue','prompt':'Follow up'})
        jobs=self.saved(); jobs[0].pop('continuation'); tools.save_jobs(jobs)
        claim=self.bot.claim_due()[0]
        with patch.object(remote,'catalog',return_value={'defaults':{'codex':'desktop'},
                'instances':[{'id':'desktop','protocols':['codex']}]}), \
             patch.object(coding,'windows_backend',return_value=True), \
             patch.object(codex,'windows_exec',return_value={'exit_code':0}) as execute, \
             patch.object(gateway,'context_footer',AsyncMock(return_value='')):
            worker=asyncio.create_task(self.bot.work_loop())
            try:
                self.bot.enqueue('77',claim['prompt'],[],claim)
                await asyncio.wait_for(self.bot.queue.join(),2)
                self.assertEqual(actions,['followup'])
                execute.assert_called_once()
                self.assertEqual(self.saved()[0]['last_status'],'ok')
                session.close.assert_not_awaited()  # Same channel session; compact controls continuity.
            finally:
                worker.cancel(); await asyncio.gather(worker,return_exceptions=True)

    async def test_active_autonomous_task_is_aborted_at_lifetime_limit(self):
        entered=asyncio.Event()
        async def prompt(text,turn,images):
            entered.set(); await asyncio.Event().wait()
        session=self.worker_environment(prompt); session.abort=AsyncMock()
        out=tools.schedule({'action':'enqueue','prompt':'bounded'})
        jobs=self.saved(); jobs[0]['task_deadline']=time.time()+.15; tools.save_jobs(jobs)
        claim=self.bot.claim_due()[0]
        worker=asyncio.create_task(self.bot.work_loop())
        try:
            self.bot.enqueue('77',claim['prompt'],[],claim)
            await asyncio.wait_for(entered.wait(),1)
            await asyncio.wait_for(self.bot.queue.join(),2)
            session.abort.assert_awaited_once()
            self.assertEqual(self.saved()[0]['last_status'],'task_expired')
            self.assertIn('12시간',self.bot.deliver.call_args.args[1])
        finally:
            worker.cancel(); await asyncio.gather(worker,return_exceptions=True)

    def worker_environment(self, prompt):
        @asynccontextmanager
        async def quiet(*args, **kwargs):
            yield
        channel=SimpleNamespace(typing=quiet)
        session=SimpleNamespace(model=self.turn.model, error=None, prompt=prompt,
            compact=AsyncMock(return_value={}), close=AsyncMock(), reusable_after_error=AsyncMock(return_value=True))
        self.worker_session=session
        self.bot.sessions['77']=session
        self.bot.get_channel=lambda ident:channel
        self.bot.session=AsyncMock(return_value=session)
        self.bot.progress=quiet
        self.bot.wait_until_ready=AsyncMock()
        return session

    async def test_real_worker_admits_idle_task_immediately(self):
        entered=asyncio.Event()
        async def prompt(text, turn, images):
            self.assertEqual(text,'idle task')
            entered.set()
            return 'done'
        self.worker_environment(prompt)
        with patch.object(gateway,'context_footer',AsyncMock(return_value='')):
            worker=asyncio.create_task(self.bot.work_loop())
            try:
                started=time.monotonic()
                self.bot.enqueue('77','idle task',[])
                await asyncio.wait_for(entered.wait(),1)
                await asyncio.wait_for(self.bot.queue.join(),1)
                self.assertLess(time.monotonic()-started,1)
                self.bot.deliver.assert_awaited_once()
            finally:
                worker.cancel(); await asyncio.gather(worker,return_exceptions=True)

    async def test_real_worker_waits_for_active_then_runs_callback_and_timer_fifo(self):
        entered=asyncio.Event(); release=asyncio.Event(); calls=[]
        async def prompt(text, turn, images):
            if text=='active task':
                calls.append('active')
                entered.set()
                await release.wait()
            elif text.endswith('Codex callback'):
                calls.append('codex')
                self.assertTrue(turn.deferred)
            elif 'Timer task' in text:
                calls.append('timer')
                self.assertTrue(turn.deferred)
                tools.schedule({'action': 'note', 'state': 'done', 'text': 'Timer task verified'})
            else:
                self.fail('Unexpected worker prompt: '+text)
            return 'done'
        self.worker_environment(prompt)
        with patch.object(gateway,'context_footer',AsyncMock(return_value='')):
            worker=asyncio.create_task(self.bot.work_loop())
            try:
                self.bot.enqueue('77','active task',[])
                await asyncio.wait_for(entered.wait(),1)
                scope=self.bot.codex_callback_scope('77')
                self.assertTrue(self.bot.enqueue_codex_callback('77','Codex callback',scope,lambda:True))
                tools.schedule({'action':'enqueue','prompt':'Timer task'})
                claim=self.bot.claim_due()[0]
                self.bot.enqueue('77',claim['prompt'],[],claim)
                await asyncio.sleep(.02)
                self.assertEqual(calls,['active'])
                self.assertEqual(self.bot.queue.qsize(),2)
                release.set()
                await asyncio.wait_for(self.bot.queue.join(),1)
                self.assertEqual(calls,['active','codex','timer'])
                self.assertEqual(self.worker_session.compact.await_count,2)
                self.assertEqual(self.saved()[0]['last_status'],'ok')
                self.assertEqual(self.bot.deliver.await_count,3)
            finally:
                release.set()
                worker.cancel(); await asyncio.gather(worker,return_exceptions=True)

    async def test_callback_compact_false_keeps_continuous_context(self):
        async def prompt(text,turn,images):
            self.assertEqual(text,'continuous callback')
            return 'done'
        session=self.worker_environment(prompt)
        with patch.object(gateway,'context_footer',AsyncMock(return_value='')):
            worker=asyncio.create_task(self.bot.work_loop())
            try:
                self.bot.enqueue_codex_callback('77','continuous callback',self.bot.codex_callback_scope('77'),lambda:True,compact=False)
                await asyncio.wait_for(self.bot.queue.join(),1)
                session.compact.assert_not_awaited()
            finally:
                worker.cancel();await asyncio.gather(worker,return_exceptions=True)


if __name__=='__main__': unittest.main(verbosity=2)
