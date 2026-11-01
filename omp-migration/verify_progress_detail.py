"""Bounded local-stream observation and owner-only UI; no live Discord/LLM requests."""
import asyncio
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, patch

import discord

from evo_omp import context, gateway, rpc
from evo_omp.discord_text import units

RPC = '--rpc' in sys.argv
if RPC:
    sys.argv.remove('--rpc')


class Checks(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = rpc.OmpSession('77', 'fixture', [], None)
        self.turn = context.Turn('77')
        self.token = context.current.set(self.turn)
        self.active = dict(channel='77', session=self.session, turn=self.turn, start=time.monotonic())
        self.bot = SimpleNamespace(active=self.active, registry=SimpleNamespace(commands={}))

    async def asyncTearDown(self):
        context.current.reset(self.token)

    async def delta(self, subtype, text):
        await self.session.dispatch({'type': 'message_update',
            'assistantMessageEvent': {'type': subtype, 'delta': text}})

    def interaction(self, user=None, channel=77, message=None):
        message = message or SimpleNamespace(edit=AsyncMock())
        return SimpleNamespace(user=SimpleNamespace(id=user or int(context.OWNER)), channel_id=channel,
            response=SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock(), defer=AsyncMock()),
            original_response=AsyncMock(return_value=message),
            followup=SimpleNamespace(send=AsyncMock(return_value=message)))

    async def test_real_delta_fields_are_bounded_and_never_forward_reasoning(self):
        delivered = []
        self.session.on_progress = delivered.append
        await self.delta('thinking_delta', 'a' * 100000)
        await self.delta('thinking_delta', 'last thought')
        self.assertEqual(len(self.session.reasoning_tail), 1400)
        self.assertTrue(self.session.reasoning_tail.endswith('last thought'))
        self.assertEqual(self.session.reasoning_chars, 100012)
        self.assertEqual(self.session.last_event, 'reasoning')
        self.assertEqual(delivered, [])
        await self.delta('text_delta', 'b' * 10000 + 'public tail')
        self.assertEqual(len(self.session.live_text), 600)
        self.assertTrue(self.session.live_text.endswith('public tail'))
        self.assertEqual(delivered, [])
        await self.session.dispatch({'type': 'message_end', 'message': {'role': 'assistant', 'content': [
            {'type': 'thinking', 'thinking': 'private thought'}, {'type': 'text', 'text': 'public note'},
            {'type': 'toolCall', 'name': 'read'}]}})
        self.assertEqual(delivered, ['public note'])

    async def test_tool_arguments_and_invalid_deltas_are_not_previews(self):
        for subtype, delta in [('toolcall_delta', 'not a preview'), ('thinking_delta', {}),
                               ('text_delta', []), ('unknown', 'other')]:
            await self.delta(subtype, delta)
        self.assertEqual(self.session.reasoning_tail, '')
        self.assertEqual(self.session.live_text, '')
        self.assertEqual(self.session.reasoning_chars, 0)

    async def test_loop_start_and_close_clear_old_observer_state(self):
        await self.delta('thinking_delta', 'previous thought')
        await self.delta('text_delta', 'previous text')
        await self.session.dispatch({'type': 'turn_start'})
        self.assertEqual((self.session.reasoning_tail, self.session.live_text, self.session.reasoning_chars), ('', '', 0))
        await self.delta('thinking_delta', 'temporary thought')
        await self.session.close()
        self.assertEqual(self.session.reasoning_tail, '')

    async def test_prompt_finally_clears_tail_on_success_and_failure(self):
        async def settled():
            pass

        async def send(event):
            ident = event['id']
            self.session.pending[ident].set_result({'success': True})
            await self.delta('thinking_delta', 'turn-only thought')
            await self.delta('text_delta', 'turn-only text')
            self.session.last_text = 'answer'
            self.session.prompts[ident].set_result({'status': 'ok'})

        self.session.send, self.session.wait_until_settled = send, settled
        self.assertEqual(await self.session.prompt('fixture', self.turn), 'answer')
        self.assertEqual((self.session.reasoning_tail, self.session.live_text), ('', ''))

        async def fail(_):
            await self.delta('thinking_delta', 'failed turn')
            raise RuntimeError('fixture failure')

        self.session.send = fail
        with self.assertRaisesRegex(RuntimeError, 'fixture failure'):
            await self.session.prompt('fixture', self.turn)
        self.assertEqual(self.session.reasoning_tail, '')

    async def test_basic_status_is_quiet_and_detail_is_bounded_for_unicode(self):
        await self.delta('thinking_delta', 'THOUGHT_MARKER' + '😀_*`' * 500)
        await self.delta('text_delta', '😀_*`' * 500)
        self.assertNotIn('THOUGHT_MARKER', gateway.progress_text(self.session, 60, include_text=False))
        detail = gateway.progress_detail(self.active)
        self.assertLessEqual(units(detail), 1900)
        self.assertIn('로컬 추론 일부', detail)
        self.assertIn('확정 결과 아님', detail)

    async def test_one_private_card_defaults_to_public_progress_without_rpc_or_inference(self):
        await self.delta('thinking_delta', 'CURRENT_THOUGHT')
        self.session.request = AsyncMock(side_effect=AssertionError('UI must not call RPC'))
        view = gateway.ProgressView(self.bot, '77', self.session)
        interaction = self.interaction()
        try:
            for _ in range(2):
                await view.inspect_progress.callback(interaction)
            args = interaction.followup.send.call_args
            self.assertEqual(interaction.followup.send.await_count, 1)
            self.assertEqual(interaction.response.defer.await_count, 2)
            self.assertEqual(view.detail.message.edit.await_count, 1)
            self.assertNotIn('CURRENT', str(args.kwargs['embed'].to_dict()))
            self.assertTrue(args.kwargs['ephemeral'])
            self.assertFalse(args.kwargs['allowed_mentions'].everyone)
            self.assertEqual([b.label for b in view.detail.children], ['새로고침', '추론 보기', '중단'])
            self.session.request.assert_not_called()
            self.assertEqual(self.session.reasoning_tail, 'CURRENT_THOUGHT')
        finally:
            await view.close()

    async def test_reasoning_toggle_refreshes_same_card_and_stays_bounded(self):
        await self.delta('thinking_delta', '😀_*`' * 500 + 'CURRENT_THOUGHT')
        view = gateway.ProgressView(self.bot, '77', self.session)
        interaction = self.interaction()
        try:
            await view.inspect_progress.callback(interaction)
            detail = view.detail
            await detail.reasoning.callback(interaction)
            embed = view.detail.message.edit.call_args.kwargs['embed']
            self.assertIn('CURRENT\\_THOUGHT', embed.fields[-1].value)
            self.assertLessEqual(units(embed.fields[-1].value), 1024)
            self.assertTrue(detail.show_reasoning)
            self.assertEqual(detail.reasoning.label, '추론 숨기기')
            await self.delta('thinking_delta', 'NEW_THOUGHT')
            await detail.refresh.callback(interaction)
            embed = view.detail.message.edit.call_args.kwargs['embed']
            self.assertIn('NEW\\_THOUGHT', embed.fields[-1].value)
            await detail.reasoning.callback(interaction)
            self.assertNotIn('THOUGHT', str(view.detail.message.edit.call_args.kwargs['embed'].to_dict()))
            self.assertEqual(interaction.followup.send.await_count, 1)
        finally:
            await view.close()

    async def test_auto_refresh_uses_received_state_and_stops_after_close(self):
        self.session.request = AsyncMock(side_effect=AssertionError('No RPC from auto-refresh'))
        view = gateway.ProgressView(self.bot, '77', self.session)
        interaction = self.interaction()
        with patch.object(gateway, 'DETAIL_INTERVAL', .005):
            await view.inspect_progress.callback(interaction)
            detail = view.detail
            await self.delta('text_delta', 'LATEST_PUBLIC')
            for _ in range(40):
                if detail.message.edit.await_count >= 2:
                    break
                await asyncio.sleep(.005)
            self.assertGreaterEqual(detail.message.edit.await_count, 2)
            embed = detail.message.edit.call_args.kwargs['embed']
            self.assertIn('LATEST\\_PUBLIC', embed.fields[0].value)
            self.session.request.assert_not_called()
            await view.close()
            self.assertTrue(detail.is_finished())
            edits = detail.message.edit.await_count
            await asyncio.sleep(.02)
            self.assertEqual(detail.message.edit.await_count, edits)
            self.assertTrue(detail.updater.done())

    async def test_stop_button_stops_only_current_task(self):
        self.bot.stop_active = AsyncMock()
        view = gateway.ProgressView(self.bot, '77', self.session)
        interaction = self.interaction()
        try:
            await view.inspect_progress.callback(interaction)
            await view.detail.cancel.callback(interaction)
            self.bot.stop_active.assert_awaited_once()
            self.assertTrue(view.detail.is_finished())
            self.assertIn('중단했습니다', view.detail.message.edit.call_args.kwargs['content'])
        finally:
            await view.close()

    async def test_stop_rechecks_after_discord_ack_so_it_cannot_stop_replacement_turn(self):
        self.bot.stop_active = AsyncMock()
        view = gateway.ProgressView(self.bot, '77', self.session)
        interaction = self.interaction()
        try:
            await view.inspect_progress.callback(interaction)
            async def replace():
                self.bot.active = dict(self.active, turn=context.Turn('77'))
            interaction.response.defer.side_effect = replace
            await view.detail.cancel.callback(interaction)
            self.bot.stop_active.assert_not_called()
        finally:
            await view.close()

    async def test_stop_button_releases_actual_worker_and_its_preview_without_deadlock(self):
        from contextlib import suppress
        from verify_gateway import CaptureBot
        from verify_progress import Channel
        bot = CaptureBot()
        bot.channel = Channel()
        worker = asyncio.create_task(bot.work_loop())
        with patch.object(gateway, 'STATUS_INTERVAL', .005), \
                patch.object(gateway, 'PROGRESS_INTERVAL', .01), \
                patch.object(gateway, 'DETAIL_INTERVAL', .005):
            try:
                bot.enqueue('77', 'long', [])
                await asyncio.wait_for(bot.channel.sent.wait(), 1)
                view = bot.channel.notices[0].view
                interaction = self.interaction()
                await view.inspect_progress.callback(interaction)
                detail = view.detail
                await asyncio.wait_for(detail.cancel.callback(interaction), 2)
                await asyncio.wait_for(bot.queue.join(), 1)
                await asyncio.sleep(0)
                self.assertEqual(bot.fake.aborts, 1)
                self.assertEqual(bot.fake.closed, 0)
                self.assertIsNone(bot.active)
                self.assertFalse(worker.done())
                self.assertTrue(view.is_finished() and detail.is_finished())
                self.assertTrue(detail.updater.done())
                self.assertTrue(bot.channel.notices[0].deleted)
                self.assertNotIn('answer:long', bot.channel.messages)
            finally:
                worker.cancel()
                with suppress(asyncio.CancelledError):
                    await worker

    async def test_auto_refresh_clears_card_when_turn_is_replaced(self):
        view = gateway.ProgressView(self.bot, '77', self.session)
        with patch.object(gateway, 'DETAIL_INTERVAL', .005):
            await view.inspect_progress.callback(self.interaction())
            detail = view.detail
            self.bot.active = dict(self.active, turn=context.Turn('77'))
            await asyncio.wait_for(detail.updater, 1)
            self.assertTrue(detail.is_finished())
            self.assertIsNone(detail.message.edit.call_args.kwargs['embed'])
            self.assertIsNone(detail.message.edit.call_args.kwargs['view'])
        await view.close()

    async def test_private_controls_reject_non_owner_and_stale_turn(self):
        self.bot.stop_active = AsyncMock()
        view = gateway.ProgressView(self.bot, '77', self.session)
        try:
            await view.inspect_progress.callback(self.interaction())
            for user, channel, active in [('other', 77, self.active), (context.OWNER, 88, self.active),
                                         (context.OWNER, 77, None)]:
                self.bot.active = active
                for button in view.detail.children:
                    interaction = self.interaction(user=user, channel=channel)
                    await button.callback(interaction)
                    interaction.response.send_message.assert_awaited_once()
                    interaction.response.edit_message.assert_not_called()
                    self.bot.stop_active.assert_not_called()
        finally:
            await view.close()

    async def test_expired_webhook_reopens_only_when_owner_requests_it(self):
        view = gateway.ProgressView(self.bot, '77', self.session)
        interaction = self.interaction()
        try:
            await view.inspect_progress.callback(interaction)
            previous = view.detail
            previous.message.edit.side_effect = discord.NotFound(
                SimpleNamespace(status=404, reason='Expired'), 'unknown webhook')
            next_click = self.interaction()
            await view.inspect_progress.callback(next_click)
            self.assertTrue(previous.is_finished())
            self.assertIsNot(view.detail, previous)
            next_click.followup.send.assert_awaited_once()
            self.assertTrue(next_click.followup.send.call_args.kwargs['ephemeral'])
        finally:
            await view.close()

    async def test_preview_timeout_and_discord_error_stop_updater(self):
        view = gateway.ProgressView(self.bot, '77', self.session)
        with patch.object(gateway, 'DETAIL_INTERVAL', .005):
            await view.inspect_progress.callback(self.interaction())
            detail = view.detail
            detail.message.edit.side_effect = discord.HTTPException(
                SimpleNamespace(status=503, reason='Captured'), 'failure')
            await asyncio.wait_for(detail.updater, 1)
            self.assertTrue(detail.is_finished())
            self.assertEqual(detail.message.edit.await_count, 2)  # One failed refresh, one bounded cleanup.
            await view.close()
        view = gateway.ProgressView(self.bot, '77', self.session)
        await view.inspect_progress.callback(self.interaction())
        await view.detail.on_timeout()
        self.assertTrue(view.detail.is_finished())
        self.assertIn('만료', view.detail.message.edit.call_args.kwargs['content'])
        await view.close()

    async def test_button_rejects_non_owner_wrong_channel_stale_or_cancelled_turn(self):
        await self.delta('thinking_delta', 'DO_NOT_DISCLOSE')
        view = gateway.ProgressView(self.bot, '77', self.session)
        try:
            for user, channel, active in [('other', 77, self.active), (context.OWNER, 88, self.active),
                                           (context.OWNER, 77, None),
                                           (context.OWNER, 77, dict(self.active, turn=context.Turn('77'))),
                                           (context.OWNER, 77, dict(self.active, session=object()))]:
                self.bot.active = active
                interaction = self.interaction(user=user, channel=channel)
                await view.inspect_progress.callback(interaction)
                self.assertNotIn('DO_NOT_DISCLOSE', interaction.followup.send.call_args.args[0])
            self.bot.active = self.active
            self.turn.cancelled.set()
            await view.inspect_progress.callback(interaction)
            self.assertNotIn('DO_NOT_DISCLOSE', interaction.followup.send.call_args.args[0])
        finally:
            view.stop()

    async def test_existing_status_detail_is_read_only_and_channel_scoped(self):
        await self.delta('thinking_delta', 'LATEST_THOUGHT')
        channel = SimpleNamespace(id=77)
        for alias in ('detail', 'thinking', 'reasoning'):
            text = await gateway.Bot.command(self.bot, channel, 'status', alias)
            self.assertIn('LATEST\\_THOUGHT', text)
        text = await gateway.Bot.command(self.bot, SimpleNamespace(id=88), 'status', 'detail')
        self.assertNotIn('LATEST\\_THOUGHT', text)
        self.assertFalse(self.turn.cancelled.is_set())

    @unittest.skipUnless(RPC, 'Use --rpc for the compiled OMP / scripted SSE path')
    async def test_compiled_omp_real_thinking_delta_and_turn_cleanup(self):
        from verify_context_payload import Handler, ThreadingHTTPServer, REQUESTS, STEPS, THOUGHT
        REQUESTS.clear()
        STEPS.clear()
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        cfg = Path(tempfile.mkdtemp(prefix='progress-check-', dir=rpc.RUNTIME))
        (cfg / 'models.yml').write_text((rpc.ROOT / 'models.yml').read_text().replace(
            ':8080/v1', f':{server.server_port}/v1'))
        shutil.copyfile(rpc.ROOT / 'config.yml', cfg / 'config.yml')
        session = rpc.OmpSession('progress-fixture', 'sc117-flash-next-iq3_xxs', [], None, cfg)
        original, seen, frames = session.dispatch, [], []

        async def observe(event):
            await original(event)
            update = event.get('assistantMessageEvent') or {}
            if event.get('type') == 'message_update':
                frames.append((event.get('message'), set(update)))
            if update.get('type') == 'thinking_delta':
                seen.append((session.reasoning_tail, session.reasoning_chars))

        session.dispatch = observe
        try:
            await session.start()
            self.assertEqual(await session.prompt('Synthetic progress check.', self.turn), 'VERIFIED_CONTEXT')
            self.assertTrue(seen, 'Compiled RPC did not send thinking_delta')
            self.assertEqual(seen[-1], (THOUGHT[-1400:], len(THOUGHT)))
            self.assertTrue(frames)
            self.assertTrue(all(message == {'role': 'assistant'} and 'partial' not in update for message, update in frames),
                            'RPC observer must use deltas, not growing full-message snapshots')
            self.assertEqual((session.reasoning_tail, session.reasoning_chars), ('', 0))
            self.assertEqual(len(REQUESTS), 1)
            print(json.dumps({'compiled_omp_stream_verified': True, 'model_inference': False,
                              'discord_posts': 0, 'extra_provider_requests': 0}))
        finally:
            await session.close()
            server.shutdown()
            server.server_close()
            shutil.rmtree(cfg)


if __name__ == '__main__':
    unittest.main()
