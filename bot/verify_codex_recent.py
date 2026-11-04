"""Unread Codex inbox through the same remote tool and Pi delivery boundary."""
import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from evo_bot import context, tool_output
from evo_bot.integrations import codex, remote
from evo_bot.integrations.codex_recent import Recent
from evo_bot.rpc import PiSession


class RecentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.store = Recent(self.path / 'recent.sqlite3')
        self.patches = [patch.object(codex, 'STATE', self.path / 'jobs.json'),
                        patch.object(codex, 'RECENT', self.store)]
        for p in self.patches: p.start()
        self.bridges = {name: kind({'cwd': '/tmp'}, name)
                        for name, kind in [('desktop', codex.Bridge), ('oracle', codex.AppServer)]}
        for bridge in self.bridges.values():
            bridge.recent = self.store
            bridge.send = AsyncMock()
            bridge.monitor_desktop = lambda job: None
        self.client = SimpleNamespace(is_ready=lambda: True)
        self.patches += [patch.object(codex, 'SERVERS', self.bridges),
                         patch.object(codex, 'BRIDGE', self.bridges['desktop']),
                         patch.object(codex, 'CLIENT', self.client), patch.object(context, 'client', self.client),
                         patch.object(codex, 'LOOP', asyncio.get_event_loop())]
        for p in self.patches[2:]: p.start()
        self.turn = context.Turn('77')
        self.token = context.current.set(self.turn)

    async def asyncTearDown(self):
        context.current.reset(self.token)
        for p in reversed(self.patches): p.stop()
        self.tmp.cleanup()

    def reply(self, turn='turn', **extra):
        return dict(thread_id='chat', turn_id=turn, turn_status='completed', answer='RESULT-' + turn, **extra)

    async def tool(self, action, instance=None, **params):
        args = dict(protocol='codex', action=action, args=params)
        if instance: args['instance'] = instance
        value = await asyncio.to_thread(remote.remote_control, args)
        return args, value

    def deliver(self, args, value):
        blocks = {'content': [{'type': 'text', 'text': value}]}
        codex.confirm_read('remote_control', args, blocks)

    async def test_all_unread_not_ten_and_listing_does_not_consume(self):
        for i in range(17): self.store.observe('desktop', self.reply(str(i)))
        args, value = await self.tool('recent')
        self.assertEqual(len(json.loads(value)['events']), 17)
        self.deliver(args, value)
        self.assertEqual(len(self.store.pending()['events']), 17)

    async def test_same_tool_path_both_platforms_and_list_is_not_read(self):
        for name, bridge in self.bridges.items():
            result = self.reply()
            self.store.observe(name, result)
            bridge.action = AsyncMock(return_value=result)
            args, value = await self.tool('list', name)
            self.deliver(args, value)
            self.assertEqual(len(self.store.pending(name)['events']), 1)
            args, value = await self.tool('status', name, thread_id='chat')
            self.assertEqual(len(self.store.pending(name)['events']), 1, 'Not delivered to Pi yet')
            self.deliver(args, value)
            self.assertEqual(self.store.pending(name)['events'], [])

    async def test_read_removes_only_matching_turn_and_returned_text(self):
        for turn in ('old', 'new'): self.store.observe('desktop', self.reply(turn))
        self.store.read('desktop', dict(thread_id='chat', turn_id='old', recent_messages=[
            {'role': 'assistant', 'phase': 'commentary', 'text': 'Different part of the same chat'}]))
        self.assertEqual(len(self.store.pending()['events']), 2)
        self.store.read('desktop', dict(thread_id='chat', turn_id='old', recent_messages=[
            {'role': 'assistant', 'text': 'RESULT-old'}]))
        self.assertEqual([r['turn_id'] for r in self.store.pending()['events']], ['new'])

    async def test_error_metadata_empty_answer_and_failed_tool_keep_unread(self):
        row = dict(thread_id='chat', turn_id='turn', turn_status='failed', turn_error={'message': 'BUILD FAILED'})
        self.store.observe('oracle', row)
        self.store.read('oracle', dict(row, turn_error=None, answer=''))
        self.assertEqual(len(self.store.pending()['events']), 1)
        self.deliver({'protocol': 'codex', 'instance': 'oracle', 'action': 'status'}, json.dumps(dict(row, success=False)))
        self.assertEqual(len(self.store.pending()['events']), 1)
        self.store.read('oracle', row)
        self.assertEqual(self.store.pending()['events'], [])

    async def test_pending_request_identity_and_turn_are_required(self):
        request = {'request_id': 7, 'questions': [{'question': 'Which project?'}]}
        row = dict(thread_id='chat', turn_id='new', pending_requests=[request])
        self.store.observe('desktop', row)
        for other in (dict(row, turn_id='old'), dict(row, pending_requests=[dict(request, request_id=8)]),
                      dict(row, pending_requests=[{'request_id': 7}])):
            self.store.read('desktop', other)
            self.assertEqual(len(self.store.pending()['events']), 1)
        self.store.read('desktop', row)
        self.assertEqual(self.store.pending()['events'], [])

    async def test_read_survives_restart_and_repeated_polling_does_not_resurrect(self):
        row = self.reply()
        self.store.observe('desktop', row)
        reopened = Recent(self.store.path)
        self.assertEqual(len(reopened.pending()['events']), 1)
        reopened.read('desktop', row)
        for _ in range(3): self.store.observe('desktop', row)
        self.assertEqual(Recent(self.store.path).pending()['events'], [])

    async def test_instance_filter_and_read_do_not_touch_other_server(self):
        for name in self.bridges: self.store.observe(name, self.reply())
        _, value = await self.tool('recent', 'oracle')
        self.assertEqual([r['instance'] for r in json.loads(value)['events']], ['oracle'])
        self.store.read('oracle', self.reply())
        self.assertEqual([r['instance'] for r in self.store.pending()['events']], ['desktop'])

    async def test_wake_false_completion_recorded_without_changing_wake_or_queued_work(self):
        for name, bridge in self.bridges.items():
            job = dict(self.reply(), job_id='j', state='completed', messages={'a': {'text': 'RESULT-turn'}},
                       _wake=False, channel='77', _callback_events=['keep'])
            bridge.jobs['j'] = job
            await bridge.notify_complete(job)
            self.assertEqual(len(self.store.pending(name)['events']), 1)
            self.store.read(name, self.reply())
            self.assertFalse(job['_wake'])
            self.assertEqual(job['_callback_events'], ['keep'])
            self.assertEqual(bridge.jobs['j'], job)

    async def test_preview_omission_never_marks_hidden_turn_read(self):
        self.store.observe('desktop', self.reply('middle'))
        result = dict(success=True, instance='desktop', thread_id='chat', recent_messages=[
            {'turn_id': t, 'role': 'assistant', 'text': 'RESULT-' + t + 'x' * 3500} for t in ('first', 'middle', 'last')])
        profile = self.path / 'profile'; (profile / 'sessions').mkdir(parents=True)
        blocks = tool_output.compact_result('remote_control', {'content': [{'type': 'text', 'text': json.dumps(result)}]}, profile)
        codex.confirm_read('remote_control', {'protocol': 'codex', 'instance': 'desktop', 'action': 'read'}, blocks)
        self.assertEqual(len(self.store.pending()['events']), 1)

    async def test_default_and_slash_status_acknowledge(self):
        self.store.observe('desktop', self.reply())
        self.deliver({'protocol': 'codex', 'action': 'send', 'args': {'prompt': '/status'}},
                     json.dumps(dict(self.reply(), success=True)))
        self.assertEqual(self.store.pending()['events'], [])

    async def test_routing_default_uses_actual_returned_instance(self):
        for name in self.bridges: self.store.observe(name, self.reply())
        self.deliver({'protocol': 'codex', 'action': 'status'}, json.dumps(dict(self.reply(), success=True, instance='oracle')))
        self.assertEqual([r['instance'] for r in self.store.pending()['events']], ['desktop'])

    async def test_read_before_completion_notification_does_not_resurrect(self):
        row = dict(thread_id='chat', turn_id='turn', turn_status='completed', recent_messages=[
            {'role': 'assistant', 'phase': 'final_answer', 'text': 'RESULT-turn'}])
        bridge = self.bridges['desktop']
        bridge.action = AsyncMock(return_value=row)
        args, value = await self.tool('read', 'desktop', thread_id='chat')
        self.deliver(args, value)
        self.store.observe('desktop', self.reply())
        self.assertEqual(self.store.pending()['events'], [])

    async def test_real_pi_delivery_boundary_and_interruption(self):
        args = {'protocol': 'codex', 'instance': 'desktop', 'action': 'status', 'args': {'thread_id': 'chat'}}
        for mode in ('cancelled', 'disconnected', 'ok'):
            row = self.reply(mode)
            self.store.observe('desktop', row)
            session = PiSession.__new__(PiSession)
            session.profile = self.path / 'profile'
            session.host_jobs = {'1': None}
            async def handler(*_):
                if mode == 'cancelled': self.turn.cancelled.set()
                return json.dumps(dict(row, success=True, instance='desktop'))
            session.handler = handler
            session.send = AsyncMock(side_effect=ConnectionError() if mode == 'disconnected' else None)
            await session.host_call({'id': '1', 'toolName': 'remote_control', 'arguments': args}, self.turn)
            turns = [r['turn_id'] for r in self.store.pending()['events']]
            self.assertEqual(mode in turns, mode != 'ok')
            self.turn.cancelled.clear()

    async def test_storage_failure_does_not_break_existing_callback(self):
        bridge = self.bridges['oracle']
        with patch.object(self.store, 'observe', side_effect=OSError('disk unavailable')):
            self.assertFalse(bridge.queue_callback(dict(job_id='j', thread_id='chat'), 'event'))

    async def test_owner_and_arguments_are_still_checked(self):
        self.store.observe('desktop', self.reply())
        for args in ({'protocol': 'codex', 'action': 'recent', 'args': {'limit': 1}},
                     {'protocol': 'codex', 'action': 'recent', 'instance': 'absent'}):
            try:
                value = await asyncio.to_thread(remote.remote_control, args)
                self.assertFalse(json.loads(value)['success'])
            except ValueError:
                pass
        self.turn.cancelled.set()
        with self.assertRaises(PermissionError): await self.tool('recent')
        self.assertEqual(len(self.store.pending()['events']), 1)


if __name__ == '__main__': unittest.main()
