"""Settings parity: preserve omitted values and use the existing GUI owner."""
import unittest
from unittest.mock import AsyncMock, patch

from evo_bot.integrations import codex


class SettingsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bridge = codex.Bridge({'url':'wss://unused','ca':'unused','token':'test'}, 'verification')
        self.bridge.jobs = {}
        self.current = dict(thread_id='chat', source='desktop', gui_attached=True,
                            model='model-a', daybreak_enabled=True, approval_policy='on-request', sandbox='workspaceWrite')

    async def test_gui_read_uses_owner_only(self):
        with patch.object(self.bridge, 'desktop', new=AsyncMock(return_value=self.current)) as desktop, \
             patch.object(self.bridge, 'rpc', new=AsyncMock()) as rpc:
            out = await self.bridge.chat_settings({'action':'settings','thread_id':'chat'})
        self.assertEqual(out['model'], 'model-a')
        self.assertEqual(desktop.call_count, 1)
        rpc.assert_not_called()

    async def test_gui_off_preserves_model_permissions(self):
        after = dict(self.current, daybreak_enabled=False)
        with patch.object(self.bridge, 'desktop', new=AsyncMock(side_effect=[self.current, after])) as desktop, \
             patch.object(self.bridge, 'rpc', new=AsyncMock()) as rpc:
            out = await self.bridge.chat_settings({'action':'daybreak','mode':'off','thread_id':'chat'})
        self.assertTrue(out['applied']); self.assertEqual(out['changed'], ['daybreakEnabled'])
        self.assertEqual(desktop.call_args.args[0], dict(action='update_settings',thread_id='chat',thread_settings={},daybreak_enabled=False))
        rpc.assert_not_called()

    async def test_gui_policy_not_fabricated_from_request(self):
        with patch.object(self.bridge, 'desktop', new=AsyncMock(return_value=self.current)):
            out = await self.bridge.chat_settings({'action':'approval','thread_id':'chat','policy':'never'})
        self.assertFalse(out['applied']); self.assertIsNone(out['changed'])
        self.assertEqual(out['approval_policy'], 'on-request')

    async def test_gui_omitted_policy_not_written(self):
        with patch.object(self.bridge, 'desktop', new=AsyncMock(return_value=self.current)) as desktop:
            await self.bridge.chat_settings({'action':'approval','thread_id':'chat','sandbox':'workspace-write'})
        self.assertEqual(desktop.call_args.args[0]['thread_settings'], {'sandboxPolicy':{'type':'workspaceWrite'}})

    async def test_gui_invalid_model_rejected_before_write(self):
        with patch.object(self.bridge, 'desktop', new=AsyncMock(return_value=self.current)) as desktop, \
             patch.object(self.bridge, 'rpc', new=AsyncMock(return_value={'data':[{'id':'valid'}]})):
            with self.assertRaisesRegex(ValueError, 'offers no model'):
                await self.bridge.chat_settings({'action':'daybreak','mode':'off','thread_id':'chat','model':'missing'})
        self.assertEqual(desktop.call_count, 1)

    async def test_gui_failure_never_resumes_copy(self):
        for error in ('outcome_unknown', 'desktop_unavailable'):
            with self.subTest(error=error), \
                 patch.object(self.bridge, 'desktop', new=AsyncMock(side_effect=codex.DesktopError(error,'unavailable'))) as desktop, \
                 patch.object(self.bridge, 'rpc', new=AsyncMock()) as rpc:
                with self.assertRaises(codex.DesktopError):
                    await self.bridge.chat_settings({'action':'settings','thread_id':'chat'})
                rpc.assert_not_called(); self.assertEqual(desktop.call_count, 1)

    async def test_closed_gui_loads_in_app_only(self):
        with patch.object(self.bridge, 'desktop', new=AsyncMock(side_effect=[codex.DesktopError('no_gui_owner','closed'), {}, self.current])) as desktop, \
             patch.object(self.bridge, 'rpc', new=AsyncMock(return_value={'thread':{'id':'chat'}})) as rpc:
            await self.bridge.chat_settings({'action':'settings','thread_id':'chat'})
        self.assertEqual([c.args[0] for c in rpc.call_args_list], ['thread/read'])
        self.assertEqual([c.args[0]['action'] for c in desktop.call_args_list], ['settings','open','settings'])

    async def test_unknown_id_never_opened_or_changed(self):
        with patch.object(self.bridge, 'desktop', new=AsyncMock(side_effect=codex.DesktopError('no_gui_owner','closed'))) as desktop, \
             patch.object(self.bridge, 'rpc', new=AsyncMock(return_value={'thread':{'id':'other'}})):
            with self.assertRaises(codex.DesktopError):
                await self.bridge.chat_settings({'action':'approval','thread_id':'chat','policy':'never'})
        self.assertEqual(desktop.call_count, 1)

    async def test_tracked_native_task_uses_native_only(self):
        self.bridge.jobs = {'job':{'thread_id':'chat','source':'app_server'}}
        with patch.object(self.bridge, 'desktop', new=AsyncMock(side_effect=codex.DesktopError('no_gui_owner','closed'))), \
             patch.object(self.bridge, 'native_settings', new=AsyncMock(return_value=self.current)) as native:
            await self.bridge.chat_settings({'action':'settings','thread_id':'chat'})
        native.assert_awaited_once()

    async def test_rpc_batch_stops_after_error_without_replay(self):
        with patch.object(self.bridge, 'rpc', new=AsyncMock(side_effect=TimeoutError('unknown'))) as rpc:
            with self.assertRaises(TimeoutError):
                await self.bridge.settings_rpc('@multi', {'calls':[{'method':'thread/settings/update','params':{}},
                                                                 {'method':'thread/metadata/update','params':{}}]}, 1, None)
        self.assertEqual(rpc.call_count, 1)


if __name__ == '__main__':
    unittest.main()
