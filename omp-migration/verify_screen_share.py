"""Real registry routes with synthetic images and mocked Discord; no live posts/connections."""
import base64
import copy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from evo_omp import context, tools
from evo_omp.integrations import browser, rdp, remote


def image(raw=b'filtered-image'):
    return {'frame': 'frame1', 'content': [{'type': 'image', 'mimeType': 'image/jpeg',
                                           'data': base64.b64encode(raw).decode()}]}


class Checks(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.turn = context.Turn('123')
        self.token = context.current.set(self.turn)
        self.uploads = []

        async def send(**kwargs):
            attachment = kwargs['file']
            self.uploads.append((attachment.filename, attachment.fp.read(), kwargs))

        self.channel = SimpleNamespace(id=123, send=AsyncMock(side_effect=send))
        self.client = SimpleNamespace(is_ready=Mock(return_value=True),
            get_channel=Mock(return_value=self.channel), fetch_channel=AsyncMock(return_value=self.channel))
        self.client_patch = patch.object(context, 'client', self.client)
        self.client_patch.start()
        self.registry = tools.Registry()
        self.raw, self.filtered = image(b'private-unfiltered'), image()
        self.filter_patch = patch.object(tools, 'filter_screen', return_value=self.filtered)
        self.filter = self.filter_patch.start()
        self.browser_patch = patch.object(browser.CLIENT, 'call', return_value=self.raw)
        self.browser_call = self.browser_patch.start()

    async def asyncTearDown(self):
        self.browser_patch.stop()
        self.filter_patch.stop()
        self.client_patch.stop()
        context.current.reset(self.token)

    async def call(self, **args):
        return await self.registry.call('browser', dict(action='screenshot', **args), self.turn)

    async def test_browser_uploads_filtered_bytes_once_and_retains_model_image(self):
        args = {'action': 'screenshot', 'share': True}
        original = copy.deepcopy(args)
        result = await self.registry.call('browser', args, self.turn)
        self.browser_call.assert_called_once_with({'action': 'screenshot'}, self.turn)
        self.filter.assert_called_once_with(self.raw)
        self.assertEqual(args, original)
        self.assertTrue(result['shared_to_discord'])
        self.assertEqual(result['content'], self.filtered['content'])
        self.assertEqual(self.uploads[0][:2], ('screen.jpg', b'filtered-image'))
        self.assertEqual(len(self.uploads), 1)
        self.assertEqual(set(self.uploads[0][2]), {'file', 'allowed_mentions'})
        self.assertFalse(self.uploads[0][2]['allowed_mentions'].everyone)

    async def test_rdp_router_strips_share_and_preserves_fresh_frame(self):
        catalog = {'defaults': {'rdp': 'pc'}, 'instances': [{'id': 'pc', 'protocols': ['rdp']}]}
        args = {'protocol': 'rdp', 'instance': 'pc', 'action': 'screenshot', 'args': {'share': True}}
        with patch.object(remote, 'catalog', return_value=catalog), \
                patch.object(rdp, 'remote_desktop', return_value=self.raw) as capture:
            result = await self.registry.call('remote_control', args, self.turn)
        capture.assert_called_once_with({'instance': 'pc', 'action': 'screenshot'})
        self.assertEqual(result['frame'], 'frame1')
        self.assertEqual(self.uploads[0][1], b'filtered-image')
        self.assertEqual(args['args'], {'share': True})

    async def test_default_and_false_do_not_upload(self):
        for params in ({}, {'share': False}):
            result = await self.call(**params)
            self.assertNotIn('shared_to_discord', result)
        self.channel.send.assert_not_called()

    async def test_invalid_parameter_or_action_never_captures(self):
        args = [{'action': 'screenshot', 'share': v} for v in ('true', 1, None)]
        args += [{'action': 'click', 'share': True}, {'action': 'read', 'share': False}]
        for params in args:
            with self.subTest(params=params), self.assertRaises(ValueError):
                await self.registry.call('browser', params, self.turn)
        for protocol in ('ssh', 'codex'):
            with self.assertRaises(ValueError):
                await self.registry.call('remote_control',
                    {'protocol': protocol, 'action': 'screenshot', 'args': {'share': True}}, self.turn)
        self.browser_call.assert_not_called()
        self.channel.send.assert_not_called()

    async def test_scope_identity_and_disconnect_fail_before_capture(self):
        for turn in (context.Turn('123', cron=True), context.Turn('123', platform='web'),
                     context.Turn('123', user='other'), context.Turn('456')):
            token = context.current.set(turn)
            try:
                passed_turn = self.turn if turn.channel == '456' else turn
                with self.subTest(turn=turn), self.assertRaises(PermissionError):
                    await self.registry.call('browser', {'action': 'screenshot', 'share': True}, passed_turn)
            finally:
                context.current.reset(token)
        self.client.is_ready.return_value = False
        with self.assertRaises(PermissionError):
            await self.call(share=True)
        self.browser_call.assert_not_called()

    async def test_cancel_before_or_during_capture_never_uploads(self):
        self.turn.cancelled.set()
        with self.assertRaises(InterruptedError):
            await self.call(share=True)
        self.browser_call.assert_not_called()
        self.turn.cancelled.clear()

        def cancel(_):
            self.turn.cancelled.set()
            return self.filtered

        self.filter.side_effect = cancel
        with self.assertRaises(PermissionError):
            await self.call(share=True)
        self.channel.send.assert_not_called()

    async def test_privacy_failure_never_uploads_original(self):
        self.filter.side_effect = ValueError('Screen privacy filter failed; no screenshot returned')
        with self.assertRaisesRegex(ValueError, 'privacy filter failed'):
            await self.call(share=True)
        self.channel.send.assert_not_called()

    async def test_missing_malformed_or_oversized_image_never_uploads(self):
        bad = [{}, image(b''), image(b'x' * 450001), image()]
        bad[-1]['content'][0]['data'] = 'invalid base64!'
        bad += [{'content': image()['content'] * 2}, {'content': [dict(image()['content'][0], mimeType='text/html')]}]
        for value in bad:
            self.filter.return_value = value
            with self.subTest(value_type=type(value)), self.assertRaises(ValueError):
                await self.call(share=True)
        self.channel.send.assert_not_called()

    async def test_channel_cache_miss_fetches_only_current_channel(self):
        self.client.get_channel.return_value = None
        await self.call(share=True)
        self.client.fetch_channel.assert_awaited_once_with(123)

    async def test_mismatched_channel_or_cancel_during_fetch_never_uploads(self):
        self.channel.id = 456
        with self.assertRaises(PermissionError):
            await self.call(share=True)
        self.channel.id = 123
        self.client.get_channel.return_value = None

        async def cancel(_):
            self.turn.cancelled.set()
            return self.channel

        self.client.fetch_channel.side_effect = cancel
        with self.assertRaises(PermissionError):
            await self.call(share=True)
        self.channel.send.assert_not_called()

    async def test_upload_failure_is_sanitized_and_not_replayed(self):
        self.channel.send.side_effect = RuntimeError('private URL/token from transport')
        with self.assertRaisesRegex(RuntimeError, '^Screenshot upload failed; delivery is uncertain') as error:
            await self.call(share=True)
        self.assertNotIn('private URL', str(error.exception))
        self.channel.send.assert_awaited_once()

    async def test_no_new_tool_and_rdp_help_remains_on_demand(self):
        definitions = {d['name']: d for d in self.registry.definitions()}
        self.assertNotIn('share_screen', definitions)
        self.assertIn('share', definitions['browser']['parameters']['properties'])
        self.assertIn('share', remote.GUIDES['rdp']['screenshot']['optional'])
        self.assertEqual(set(definitions['remote_control']['parameters']['properties']),
                         {'instance', 'protocol', 'action', 'args'})


if __name__ == '__main__':
    unittest.main()
