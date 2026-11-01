"""Schedule edits against an isolated store; no Discord posts or model calls."""
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest

from evo_omp import context, tools
from evo_omp.gateway import Bot


class ScheduleChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='evo-schedule-')
        self.previous_state = tools.STATE
        tools.STATE = Path(self.temp.name)
        self.turn = context.Turn('77')
        self.token = context.current.set(self.turn)

    def tearDown(self):
        context.current.reset(self.token)
        tools.STATE = self.previous_state
        self.temp.cleanup()

    def call(self, action, **args):
        return tools.schedule(dict(action=action, **args))

    def create(self, **args):
        return self.call('create', name='Briefing', prompt='Read public updates.',
                         **({'cron': '0 9 * * *'} | args))['id']

    def read(self, ident):
        return self.call('read', id=ident)['job']

    def test_read_exposes_exact_task_and_note(self):
        ident = self.create()
        jobs = tools.load_jobs(); jobs[0]['note'] = 'Last seen post: example'
        tools.save_jobs(jobs)
        self.assertEqual(self.read(ident)['prompt'], 'Read public updates.')
        self.assertEqual(self.read(ident)['note'], jobs[0]['note'])
        self.assertNotIn('prompt', self.call('list')['jobs'][0])

    def test_edit_keeps_id_recurrence_and_due_time(self):
        ident = self.create(); before = self.read(ident)
        self.call('update', id=ident, prompt='Read only new posts.', name='New briefing')
        after = self.read(ident)
        self.assertEqual(len(tools.load_jobs()), 1)
        for key in ('id', 'channel', 'schedule', 'next_run_at', 'enabled'):
            self.assertEqual(after[key], before[key])
        self.assertEqual(after['prompt'], 'Read only new posts.')
        self.assertEqual(after['name'], 'New briefing')

    def test_prompt_edit_clears_stale_note(self):
        ident = self.create()
        jobs = tools.load_jobs(); jobs[0]['note'] = 'Old task progress'; tools.save_jobs(jobs)
        self.call('update', id=ident, name='Rename only')
        self.assertIn('note', self.read(ident))
        self.call('update', id=ident, prompt='Different task.')
        self.assertNotIn('note', self.read(ident))

    def test_edit_recurrence_and_end_time(self):
        ident = self.create()
        until = (datetime.now(tools.TZ) + timedelta(days=2)).replace(tzinfo=None).isoformat()
        result = self.call('update', id=ident, cron='*/15 * * * *', until=until, quiet=True)
        job = self.read(ident)
        self.assertEqual(result['schedule']['expr'], '*/15 * * * *')
        self.assertTrue(job['quiet'])
        self.assertTrue(job['until'].endswith('+09:00'))
        self.assertGreater(datetime.fromisoformat(job['next_run_at']), datetime.now(tools.TZ))

    def test_clear_end_time(self):
        ident = self.create(until=(datetime.now(tools.TZ) + timedelta(days=2)).isoformat(), quiet=True)
        self.call('update', id=ident, until=None, quiet=False)
        self.assertNotIn('until', self.read(ident))
        self.assertFalse(self.read(ident)['quiet'])

    def test_switch_to_one_shot_clears_recurring_options(self):
        ident = self.create(until=(datetime.now(tools.TZ) + timedelta(days=2)).isoformat(), quiet=True)
        at = (datetime.now(tools.TZ) + timedelta(hours=1)).replace(tzinfo=None).isoformat()
        self.call('update', id=ident, at=at)
        job = self.read(ident)
        self.assertEqual(job['schedule'], {'kind': 'at', 'at': at})
        self.assertFalse(job['quiet']); self.assertNotIn('until', job)
        self.assertTrue(job['next_run_at'].endswith('+09:00'))

    def test_paused_job_stays_paused_until_resume(self):
        ident = self.create(); self.call('pause', id=ident)
        self.call('update', id=ident, cron='0 10 * * *')
        self.assertFalse(self.read(ident)['enabled'])
        self.call('resume', id=ident)
        self.assertTrue(self.read(ident)['enabled'])

    def test_invalid_edits_leave_store_unchanged(self):
        ident = self.create()
        before = (tools.STATE/'jobs.json').read_bytes()
        invalid = [dict(), dict(prompt=''), dict(name='x'*101), dict(quiet='true'),
                   dict(quiet=True), dict(cron='bad'), dict(cron='* * * * * *'),
                   dict(at='not-a-date'), dict(at='2000-01-01'), dict(cron='0 9 * * *', at='2030-01-01'),
                   dict(until='2000-01-01'), dict(until=False), dict(enabled=True),
                   dict(at='2030-01-01', until='2030-01-02')]
        for args in invalid:
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.call('update', id=ident, **args)
            self.assertEqual((tools.STATE/'jobs.json').read_bytes(), before)

    def test_unknown_id_does_not_create_a_job(self):
        self.create()
        for action in ('read', 'update'):
            with self.assertRaises(ValueError): self.call(action, id='absent', name='x')
        self.assertEqual(len(tools.load_jobs()), 1)

    def test_cancelled_turn_cannot_save_an_edit(self):
        ident = self.create(); self.turn.cancelled.set()
        with self.assertRaises(PermissionError): self.call('update', id=ident, name='x')
        self.turn.cancelled.clear()
        self.assertEqual(self.read(ident)['name'], 'Briefing')

    def test_scheduled_run_can_read_only_its_own_task(self):
        ident = self.create(); other = self.create()
        token = context.current.set(context.Turn('77', cron=True, job_id=ident))
        try:
            self.assertEqual(self.call('read')['job']['id'], ident)
            for action, args in [('read', {'id':other}), ('list', {}),
                                 ('update', {'id':ident, 'prompt':'Changed'}),
                                 ('create', {'name':'x', 'prompt':'x', 'cron':'* * * * *'})]:
                with self.assertRaises(PermissionError): self.call(action, **args)
            self.call('note', text='Progress retained')
        finally:
            context.current.reset(token)
        self.assertEqual(self.read(ident)['note'], 'Progress retained')

    def test_non_owner_cannot_read_or_edit(self):
        ident = self.create()
        for who in (context.Turn('77', user='1'), context.Turn('77', platform='web')):
            token = context.current.set(who)
            try:
                for action in ('read', 'update'):
                    with self.assertRaises(PermissionError): self.call(action, id=ident, name='x')
            finally:
                context.current.reset(token)

    def test_callbacks_cannot_become_privileged_recurring_jobs(self):
        ident = self.call('defer', after_seconds=600, prompt='Continue task')['id']
        with self.assertRaises(ValueError): self.call('update', id=ident, cron='* * * * *')
        at = (datetime.now(tools.TZ) + timedelta(hours=1)).isoformat()
        self.call('update', id=ident, at=at)
        self.assertTrue(self.read(ident)['continuation'])

    def test_old_queued_claim_and_completion_are_revoked(self):
        ident = self.create()
        jobs = tools.load_jobs()
        jobs[0]['next_run_at'] = (datetime.now(tools.TZ) - timedelta(seconds=1)).isoformat()
        tools.save_jobs(jobs)
        claim = Bot.claim_due(1)[0]
        self.assertTrue(Bot.job_claim_valid(claim))
        self.call('update', id=ident, prompt='Use the new instructions.')
        self.assertFalse(Bot.job_claim_valid(claim))
        Bot.finish_job(claim, 'error')
        self.assertEqual(self.read(ident)['last_status'], 'pending')
        jobs = tools.load_jobs()
        jobs[0]['next_run_at'] = (datetime.now(tools.TZ) - timedelta(seconds=1)).isoformat()
        tools.save_jobs(jobs)
        new_claim = Bot.claim_due(1)[0]
        Bot.finish_job(claim, 'error')
        self.assertTrue(Bot.job_claim_valid(new_claim))
        self.assertEqual(new_claim['prompt'], 'Use the new instructions.')
        Bot.finish_job(new_claim, 'ok')
        self.assertEqual(self.read(ident)['last_status'], 'ok')

    def test_schema_advertises_read_update_and_nullable_end(self):
        props = tools.SCHEDULE_SCHEMA['parameters']['properties']
        self.assertTrue({'read', 'update'} <= set(props['action']['enum']))
        self.assertIn('null', props['until']['type'])
        json.dumps(tools.SCHEDULE_SCHEMA)

    def test_find_by_instruction_words_and_show_brief_summary(self):
        first = self.create()
        other = self.create()
        self.call('update', id=other, name='Other task', prompt='Check server uptime and storage. ' * 20)
        matches = self.call('list', query='SERVER storage')['jobs']
        self.assertEqual([j['id'] for j in matches], [other])
        self.assertLessEqual(len(matches[0]['summary']), 140)
        self.assertNotIn('prompt', matches[0])
        self.assertNotIn('until', matches[0])
        self.assertEqual(self.call('list', query='no-match')['jobs'], [])
        self.assertEqual(len(tools.load_jobs()), 2)

    def test_active_jobs_precede_disabled_history(self):
        disabled = self.create(); active = self.create()
        self.call('pause', id=disabled)
        self.assertEqual([j['id'] for j in self.call('list')['jobs']], [active, disabled])

    def test_unique_name_reads_and_edits_same_job_without_duplicate(self):
        ident = self.create(); before = self.read(ident)
        self.assertEqual(self.read('  BRIEFING  ')['id'], ident)
        result = self.call('update', id='Briefing', prompt='Only changed public updates.')
        self.assertEqual(result['id'], ident)
        self.assertEqual(result['changed'], ['prompt'])
        self.assertEqual(result['summary'], 'Only changed public updates.')
        self.assertEqual(self.read(ident)['next_run_at'], before['next_run_at'])
        self.assertEqual(len(tools.load_jobs()), 1)

    def test_duplicate_names_and_partial_names_never_mutate_a_job(self):
        self.create(); self.create()
        before = (tools.STATE/'jobs.json').read_bytes()
        for ident in ('Briefing', 'Brief'):
            for action in ('read', 'update', 'pause', 'resume', 'delete'):
                args = {'prompt': 'Wrong task'} if action == 'update' else {}
                with self.assertRaises(ValueError): self.call(action, id=ident, **args)
                self.assertEqual((tools.STATE/'jobs.json').read_bytes(), before)

    def test_scheduled_run_still_cannot_select_by_name_or_edit_itself(self):
        ident = self.create()
        token = context.current.set(context.Turn('77', cron=True, job_id=ident))
        try:
            self.assertEqual(self.call('read')['job']['id'], ident)
            with self.assertRaises(PermissionError): self.call('read', id='Briefing')
            with self.assertRaises(PermissionError): self.call('update', id=ident, prompt='Changed')
            with self.assertRaises(PermissionError): self.call('list', query='Briefing')
        finally:context.current.reset(token)


if __name__ == '__main__': unittest.main(verbosity=2)
