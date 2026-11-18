"""Goal task transitions on the existing schedule store and FIFO."""
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from evo_bot import context, gateway, tools


class GoalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = patch.object(tools, 'STATE', Path(self.tmp.name)); self.state.start()
        self.turn = context.Turn('77', model='sc117-flash-next-iq3_xxs')
        self.token = context.current.set(self.turn)
        self.ident = tools.schedule({'action': 'enqueue', 'prompt': 'Inspect result, verify pass, report it'})['id']

    def tearDown(self):
        context.current.reset(self.token)
        self.state.stop(); self.tmp.cleanup()

    def saved(self):
        return tools.load_jobs()[0]

    def claim(self):
        job = gateway.Bot.claim_due()[0]
        self.turn.job_id, self.turn.task_run = job['id'], job['last_run_at']
        self.turn.task_deadline = job['task_deadline']
        return job

    def note(self, state, text):
        return tools.schedule({'action': 'note', 'state': state, 'text': text})

    def finish(self, claim, state='continue', text='checked A; next B'):
        self.note(state, text)
        return gateway.Bot.finish_job(claim, 'ok')

    def test_continue_same_id_goal_deadline_then_done(self):
        claim = self.claim(); deadline = claim['task_deadline']
        self.finish(claim)
        again = self.claim()
        self.assertEqual((again['id'], again['prompt'], again['task_deadline']),
                         (self.ident, claim['prompt'], deadline))
        self.assertEqual(again['note'], 'checked A; next B')
        self.finish(again, 'done', 'Pass verified: result=42')
        self.assertEqual(self.saved()['task_state'], 'done')
        self.assertEqual(gateway.Bot.claim_due(), [])

    def test_waiting_is_not_permission_or_periodic_poll(self):
        self.finish(self.claim(), 'waiting', 'Owner confirmation required')
        self.assertFalse(self.saved()['enabled'])
        self.assertEqual(gateway.Bot.claim_due(), [])

    def test_defer_reuses_task_goal_and_budget(self):
        claim = self.claim()
        result = tools.schedule({'action': 'defer', 'after_seconds': 300, 'prompt': 'Check result once'})
        self.assertEqual(result['id'], self.ident)
        self.assertEqual(len(tools.load_jobs()), 1)
        gateway.Bot.finish_job(claim, 'ok')
        saved = self.saved()
        self.assertEqual(saved['prompt'], claim['prompt'])
        self.assertEqual(saved['task_deadline'], claim['task_deadline'])
        self.assertEqual(saved['task_state'], 'waiting')
        self.assertTrue(299 <= (datetime.fromisoformat(saved['next_run_at']) - datetime.now(tools.TZ)).total_seconds() <= 300)

    def test_one_report_saves_progress_and_timed_recheck(self):
        claim = self.claim()
        result = tools.schedule({'action':'note','state':'waiting','text':'Still running; check the result','after_seconds':600})
        self.assertEqual(result['job_id'], self.ident)
        self.assertEqual(result['state'], 'waiting')
        self.assertEqual(self.saved()['resume_at'], result['next_run_at'])
        gateway.Bot.finish_job(claim, 'ok')
        saved = self.saved()
        self.assertEqual(len(tools.load_jobs()), 1)
        self.assertEqual((saved['prompt'], saved['task_deadline']), (claim['prompt'], claim['task_deadline']))
        self.assertEqual(saved['next_run_at'], result['next_run_at'])
        self.assertTrue(saved['enabled'])

    def test_bad_recheck_leaves_the_task_unchanged(self):
        self.claim()
        before = self.saved()
        for state, seconds in [('done',600),('waiting',0),('waiting',601),('waiting',True)]:
            with self.assertRaises(ValueError):
                tools.schedule({'action':'note','state':state,'text':'Bad delay','after_seconds':seconds})
            self.assertEqual(self.saved(), before)
        jobs=tools.load_jobs();jobs[0]['task_deadline']=time.time()+30;tools.save_jobs(jobs)
        before=self.saved()
        with self.assertRaises(ValueError):
            tools.schedule({'action':'note','state':'waiting','text':'Too late','after_seconds':600})
        self.assertEqual(self.saved(), before)

    def test_event_wait_clears_an_old_timed_recheck(self):
        claim = self.claim()
        tools.schedule({'action':'note','state':'waiting','text':'Timed check','after_seconds':600})
        self.note('waiting','Await Codex completion event instead')
        gateway.Bot.finish_job(claim,'ok')
        self.assertNotIn('resume_at',self.saved())
        self.assertFalse(self.saved()['enabled'])

    def test_event_during_run_is_not_lost_or_duplicated(self):
        claim = self.claim()
        self.assertTrue(tools.wake_task(self.ident, '77', 'Codex completed A'))
        self.assertTrue(tools.wake_task(self.ident, '77', 'Codex failed B'))
        self.assertEqual(gateway.Bot.claim_due(), [])
        self.finish(claim, 'waiting', 'Read the event')
        again = self.claim()
        self.assertIn('completed A', again['wake_note'])
        self.assertIn('failed B', again['wake_note'])
        self.assertEqual(gateway.Bot.claim_due(), [])
        self.finish(again, 'waiting', 'Events handled; await owner')
        self.assertEqual(gateway.Bot.claim_due(), [])

    def test_late_event_wakes_waiting_but_not_terminal_or_paused(self):
        self.finish(self.claim(), 'waiting', 'Await Codex')
        tools.wake_task(self.ident, '77', 'Ready')
        self.finish(self.claim(), 'done', 'Verified ready')
        tools.wake_task(self.ident, '77', 'Duplicate ready')
        self.assertEqual(gateway.Bot.claim_due(), [])
        tools.schedule({'action': 'resume', 'id': self.ident})
        tools.schedule({'action': 'pause', 'id': self.ident})
        tools.wake_task(self.ident, '77', 'Still ready')
        self.assertEqual(gateway.Bot.claim_due(), [])

    def test_no_report_blocks_without_success_claim(self):
        warning = gateway.Bot.finish_job(self.claim(), 'ok')
        self.assertIn('완료로 처리하지', warning)
        self.assertEqual(self.saved()['task_state'], 'blocked')
        self.assertEqual(gateway.Bot.claim_due(), [])

    def test_repeated_checkpoint_does_not_invent_a_stop_condition(self):
        deadline=self.saved()['task_deadline']
        for _ in range(5):
            self.assertIsNone(self.finish(self.claim(), text='Next authorized step'))
            self.assertEqual(self.saved()['task_state'],'continue')
            self.assertTrue(self.saved()['enabled'])
            self.assertEqual(self.saved()['task_deadline'],deadline)

    def test_error_interrupt_delivery_failure_never_replay(self):
        for outcome in ('error', 'interrupted', 'delivery_failed', 'task_expired'):
            with self.subTest(outcome=outcome):
                jobs = tools.load_jobs(); jobs[0]['enabled'] = True; tools.save_jobs(jobs)
                claim = self.claim()
                self.note('continue', 'Next authorized step')
                gateway.Bot.finish_job(claim, outcome)
                self.assertFalse(self.saved()['enabled'])
                self.assertEqual(self.saved()['task_state'], 'blocked')

    def test_queue_full_does_not_finish_or_lose_task(self):
        tools.wake_task(self.ident, '77', 'OWNER_CORRECTION_KEEP')
        claim = self.claim()
        gateway.Bot.finish_job(claim, 'queue_full')
        retry = self.claim()
        self.assertEqual(retry['id'], self.ident)
        self.assertIn('OWNER_CORRECTION_KEEP', retry['wake_note'])

    def test_queue_full_keeps_events_received_after_claim_in_order(self):
        tools.wake_task(self.ident, '77', 'EARLIER_OWNER_REPLY')
        claim = self.claim()
        tools.wake_task(self.ident, '77', 'LATER_CODEX_EVENT')
        gateway.Bot.finish_job(claim, 'queue_full')
        retry = self.claim()['wake_note']
        self.assertEqual(retry.count('EARLIER_OWNER_REPLY'), 1)
        self.assertEqual(retry.count('LATER_CODEX_EVENT'), 1)
        self.assertLess(retry.index('EARLIER_OWNER_REPLY'), retry.index('LATER_CODEX_EVENT'))

    def test_repeated_queue_returns_do_not_erode_a_full_note(self):
        text = 'BEGIN_' + 'x' * 7989 + '_END!'
        self.assertEqual(len(text), 8000)
        tools.wake_task(self.ident, '77', text)
        for _ in range(4):
            claim = self.claim()
            self.assertEqual(claim['wake_note'], text)
            gateway.Bot.finish_job(claim, 'queue_full')

    def test_cancel_and_stale_run_cannot_update_or_wake(self):
        claim = self.claim()
        tools.cancel_deferred('77')
        with self.assertRaises(InterruptedError):
            self.note('done', 'stale result')
        gateway.Bot.finish_job(claim, 'ok')
        tools.wake_task(self.ident, '77', 'late event')
        self.assertEqual(self.saved()['last_status'], 'cancelled')
        self.assertEqual(gateway.Bot.claim_due(), [])

    def test_stop_also_cancels_disabled_waiting_task(self):
        self.finish(self.claim(), 'waiting', 'Need owner response')
        tools.cancel_deferred('77')
        tools.wake_task(self.ident, '77', 'Late completion')
        self.assertEqual(self.saved()['task_state'], 'blocked')
        self.assertEqual(self.saved()['last_status'], 'cancelled')
        self.assertEqual(gateway.Bot.claim_due(), [])

    def test_fixed_lifetime_even_when_waiting(self):
        self.finish(self.claim(), 'waiting', 'Await result')
        jobs = tools.load_jobs(); jobs[0]['task_deadline'] = time.time() - 1; tools.save_jobs(jobs)
        tools.wake_task(self.ident, '77', 'too late')
        self.assertFalse(self.saved()['enabled'])

    def test_report_validation_and_readonly_check(self):
        self.claim()
        for state, text in [('unknown', 'x'), ('done', '')]:
            with self.assertRaises(ValueError): self.note(state, text)
        with self.assertRaises(ValueError): tools.schedule({'action': 'list', 'state': 'done'})
        self.turn.cron = True
        self.turn.task_run = ''
        with self.assertRaises(InterruptedError): self.note('done', 'cannot grant task powers')

    def test_default_compact_and_explicit_continuous(self):
        self.assertTrue(self.saved()['compact'])
        tools.schedule({'action': 'update', 'id': self.ident, 'compact': False})
        self.assertFalse(self.claim()['compact'])

    def test_reply_binding_is_run_specific_and_bounded(self):
        self.claim()
        for ident in range(8): gateway.Bot.bind_task_reply(self.turn, ident)
        self.assertEqual(self.saved()['reply_ids'], ['4', '5', '6', '7'])
        self.turn.task_run = 'stale'
        gateway.Bot.bind_task_reply(self.turn, 9)
        self.assertEqual(self.saved()['reply_ids'], ['4', '5', '6', '7'])

    def test_wrong_channel_event_cannot_wake(self):
        self.finish(self.claim(), 'waiting', 'Owner needed')
        self.assertFalse(tools.wake_task(self.ident, '88', 'unrelated'))
        self.assertFalse(self.saved()['enabled'])

    def test_waiting_expires_without_wake_and_notifies_once(self):
        self.finish(self.claim(), 'waiting', 'Await result')
        jobs = tools.load_jobs(); jobs[0]['task_deadline'] = time.time()-1; tools.save_jobs(jobs)
        due = gateway.Bot.claim_due()
        self.assertEqual(len(due), 1)
        self.assertTrue(due[0]['expired_before_queue'])
        self.assertEqual(self.saved()['task_state'], 'blocked')
        self.assertEqual(gateway.Bot.claim_due(), [])


if __name__ == '__main__':
    unittest.main()
