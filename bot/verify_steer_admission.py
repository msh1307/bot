"""Discord disposition, stop/fallback and RPC timeout regressions; no Discord sends."""
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
from types import SimpleNamespace
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

from evo_bot import context, gateway, rpc
from evo_bot.integrations.codex import Bridge
from verify_gateway import CaptureBot


class Admission(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = CaptureBot()
        @asynccontextmanager
        async def quiet(*a, **k): yield
        self.bot.progress = quiet
        self.reactions = []
        async def react(emoji): self.reactions.append(emoji)
        self.react = react
        self.worker = asyncio.create_task(self.bot.work_loop())
        self.footer = patch.object(gateway, 'context_footer', AsyncMock(return_value=''))
        self.footer.start()
        await self.bot.on_message(self.message('long'))
        await asyncio.wait_for(self.bot.fake.started.wait(), 2)

    async def asyncTearDown(self):
        self.bot.fake.stop.set()
        self.worker.cancel(); await asyncio.gather(self.worker, return_exceptions=True)
        self.footer.stop()

    def message(self, text, attachments=()):
        return SimpleNamespace(author=SimpleNamespace(bot=False, id=int(context.OWNER)),
            content=text, channel=self.bot.channel, attachments=attachments, id=123, add_reaction=self.react)

    async def test_handled_is_accepted_without_abort_or_replay(self):
        self.bot.fake.steer_reply = 'handled'
        with self.assertLogs('evo-bot', 'INFO') as logs:
            await self.bot.on_message(self.message('HANDLED'))
        self.assertTrue(any('owner_input_handled' in s for s in logs.output))
        self.assertEqual(self.reactions, ['↪️'])
        self.assertEqual(self.bot.fake.aborts, 0)
        self.assertTrue(self.bot.queue.empty())
        self.assertEqual(self.bot.channel.messages, [])

    async def test_queued_has_actual_admission_event(self):
        with self.assertLogs('evo-bot', 'INFO') as logs:
            await self.bot.on_message(self.message('ADD'))
        self.assertTrue(any('owner_input_steered' in s and 'message=123' in s for s in logs.output))
        self.assertEqual(self.bot.fake.steered, ['ADD'])
        self.assertIn('long', self.bot.active['turn'].task_instructions)
        self.assertIn('ADD', self.bot.active['turn'].task_instructions)
        bridge = Bridge.__new__(Bridge); bridge.adapter = self.bot
        job = {}
        token = context.current.set(self.bot.active['turn'])
        try:
            bridge.arm_callback(job, {}, '77')
        finally:
            context.current.reset(token)
        self.assertIn('long', job['_task_prompt'])
        self.assertIn('ADD', job['_task_prompt'])

    async def test_queue_full_is_explicit_without_stop_or_replay(self):
        self.bot.fake.steer_reply = RuntimeError('Additional input queue is full (16)')
        await self.bot.on_message(self.message('FULL'))
        self.assertIn('최대 16개', self.bot.channel.messages[-1])
        self.assertEqual(self.bot.fake.aborts, 0)
        self.assertTrue(self.bot.queue.empty())

    async def test_timeout_is_ambiguous_and_never_replayed(self):
        self.bot.fake.steer_reply = TimeoutError()
        with self.assertLogs('evo-bot', 'WARNING') as logs:
            await self.bot.on_message(self.message('TIMEOUT'))
        self.assertTrue(any('owner_input_unconfirmed' in s for s in logs.output))
        self.assertIn('지연', self.bot.channel.messages[-1])
        self.assertEqual(self.bot.fake.aborts, 0)
        self.assertTrue(self.bot.queue.empty())

    async def test_unknown_disposition_never_replayed(self):
        self.bot.fake.steer_reply = 'unexpected'
        await self.bot.on_message(self.message('UNKNOWN'))
        self.assertIn('접수되지', self.bot.channel.messages[-1])
        self.assertEqual(self.bot.fake.aborts, 0)
        self.assertTrue(self.bot.queue.empty())

    async def test_stop_invalidates_not_running_fallback_exactly_once(self):
        self.bot.fake.steer_reply = 'not_running'
        await self.bot.on_message(self.message('FINISHING-FALLBACK'))
        self.assertEqual(self.bot.queue.qsize(), 1)
        with patch.object(gateway, 'cancel_deferred', return_value=0):
            await self.bot.command(self.bot.channel, 'stop', '')
        await asyncio.wait_for(self.bot.queue.join(), 2)
        self.assertEqual(self.bot.fake.aborts, 1)
        self.assertNotIn('FINISHING-FALLBACK', self.bot.fake.inputs)

    async def test_compaction_fallback_stops_and_enqueues_once(self):
        self.bot.fake.compacting = True
        await self.bot.on_message(self.message('COMPACTION-CORRECTION'))
        await asyncio.wait_for(self.bot.queue.join(), 2)
        self.assertEqual(self.bot.fake.aborts, 1)
        self.assertEqual(self.bot.fake.inputs, ['long', 'COMPACTION-CORRECTION'])

    async def test_excess_attachments_are_rejected_before_read(self):
        await self.bot.on_message(self.message('FILES', [object(), object(), object()]))
        self.assertIn('2개', self.bot.channel.messages[-1])
        self.assertEqual(self.bot.fake.steered, [])
        self.assertEqual(self.bot.fake.aborts, 0)

    async def test_stop_invalidates_input_still_downloading_an_attachment(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def read():
            started.set(); await release.wait(); return b'fixture'
        attachment = SimpleNamespace(content_type='image/png', size=7, read=read)
        with patch.object(gateway, 'image_content', return_value={'type': 'image', 'data': 'fixture', 'mimeType': 'image/png'}), \
                patch.object(gateway, 'cancel_deferred', return_value=0):
            incoming = asyncio.create_task(self.bot.on_message(self.message('PRE-STOP-IMAGE', [attachment])))
            await asyncio.wait_for(started.wait(), 2)
            await self.bot.command(self.bot.channel, 'stop', '')
            release.set()
            await asyncio.wait_for(incoming, 2)
            await asyncio.wait_for(self.bot.queue.join(), 2)
        self.assertNotIn('PRE-STOP-IMAGE', self.bot.fake.inputs)
        self.assertEqual(self.bot.fake.steered, [])

    async def test_help_describes_boundary_and_explicit_stop(self):
        help_text = await self.bot.command(self.bot.channel, 'help', '')
        self.assertIn('다음 모델 요청', help_text)
        self.assertIn('/stop', help_text)
        self.assertNotIn('진행 중 작업을 중단하고 이어서', help_text)

    async def test_wait_description_matches_non_interrupting_steering(self):
        from evo_bot.tools import WAIT_SCHEMA
        self.assertIn('/stop interrupts the wait', WAIT_SCHEMA['description'])
        self.assertIn('owner addenda apply after it ends', WAIT_SCHEMA['description'])
        self.assertNotIn('New owner messages and /stop interrupt', WAIT_SCHEMA['description'])

    async def test_explicit_new_updates_status_and_logs_reset(self):
        self.bot.sessions['77'] = self.bot.fake
        self.bot.write_status = Mock()
        with patch.object(gateway, 'cancel_deferred', return_value=0), self.assertLogs('evo-bot', 'INFO') as logs:
            await self.bot.command(self.bot.channel, 'new', '')
        self.assertEqual(len(self.bot.sessions), 0)
        self.bot.write_status.assert_called_with('connected')
        self.assertTrue(any('owner_session_reset channel=77 command=new' in s for s in logs.output))


class Timeout(unittest.IsolatedAsyncioTestCase):
    async def test_handoff_retains_original_and_latest_correction_within_budget(self):
        turn = context.Turn('77', task_instructions='ORIGINAL-TASK')
        for _ in range(10): turn.add_instruction('filler ' * 1000)
        turn.add_instruction('LATEST-CORRECTION')
        self.assertEqual(len(turn.task_instructions), 8000)
        self.assertIn('ORIGINAL-TASK', turn.task_instructions)
        self.assertTrue(turn.task_instructions.endswith('LATEST-CORRECTION'))

    async def test_timeout_includes_blocked_send_and_cleans_pending(self):
        session = rpc.PiSession('fixture', 'fixture', [], None)
        async def block(_): await asyncio.Event().wait()
        session.send = block
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(session.request('steer', timeout=.02, message='x'), .3)
        # Under the old implementation only outer wait_for expires (around .3s).
        self.assertLess(time.monotonic() - started, .2)
        self.assertEqual(session.pending, {})


if __name__ == '__main__': unittest.main()
