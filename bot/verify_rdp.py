"""Offline RDP tool checks. Fake transport only; never log in to a Windows PC."""
import asyncio
import base64
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

from PIL import Image
from evo_bot import context
from evo_bot.integrations import rdp
from evo_bot.integrations import credential_input as ci
from evo_bot.rdp_worker import Desktop, DesktopError, chord, TrustedTLS, connect_verified
from evo_bot import rdp_worker
from asysocks.unicomm.common.connection import UniConnection
from aardwolf.connection import RDPConnection
from aardwolf.protocol.T125.extendedinfopacket import PERF


class Connection:
    def __init__(self):
        self.disconnected_evt = asyncio.Event()
        self.calls = []
        self.fail_press = self.fail_release = False
        self.fail_focus = False

    async def send_focus_in(self):
        self.calls.append(('focus',))
        return (None, RuntimeError('synthetic focus failure')) if self.fail_focus else (True, None)

    def get_desktop_buffer(self):
        return Image.new('RGB', (1280, 720), 'green')

    async def send_mouse(self, button, x, y, pressed, steps=0):
        self.calls.append(('mouse', button.name, x, y, pressed, steps))
        return True, None

    async def send_key_scancode(self, code, pressed, extended):
        self.calls.append(('key', code, pressed, extended))
        if pressed and code == 30 and self.fail_press or not pressed and code == 30 and self.fail_release:
            return None, RuntimeError('synthetic failure')
        return True, None

    async def send_key_char(self, char, pressed):
        self.calls.append(('text', char, pressed))
        return True, None


class WorkerChecks(unittest.IsolatedAsyncioTestCase):
    async def test_actual_tls_upgrade_receives_pin_and_restores_on_failure(self):
        transport = type('Transport', (), {'packetizer': object(), 'reader': object(), 'writer': object()})()
        tls = type('TLS', (), {'context': object()})()
        class Connecting:
            async def connect(self):
                await UniConnection.wrap_ssl(transport)
                raise RuntimeError('synthetic connection failure')
        original = UniConnection.wrap_ssl
        with patch('asysocks.unicomm.common.connection.PacketizerSSL') as packetizer:
            packetizer.return_value.do_handshake = AsyncMock()
            with self.assertRaisesRegex(RuntimeError, 'synthetic'):
                await connect_verified(Connecting(), tls)
            self.assertIs(packetizer.call_args.args[0], tls.context)
        self.assertIs(UniConnection.wrap_ssl, original)

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Path(self.tmp.name) / 'rdp.json'
        self.data = {'instances': {'test': {'host': '192.0.2.1', 'rdp': {'account': 'pc-login'}}},
                     'accounts': {'pc-login': {'username': 'operator', 'password_file': '/private/password',
                                               'rdp_instances': ['test']}}}
        self.config.write_text(json.dumps(self.data))
        self.config.chmod(0o600)
        self.accounts_pin = patch.object(ci, 'CONFIG', self.config); self.accounts_pin.start()
        self.desktop = Desktop(self.config)
        self.conn = Connection()
        self.desktop.conn, self.desktop.server = self.conn, 'test'
        self.desktop.screenshot()

    async def asyncTearDown(self):
        self.accounts_pin.stop()
        self.tmp.cleanup()

    async def test_worker_resolves_registered_account_before_connect(self):
        with patch.object(self.desktop, 'connect', new_callable=AsyncMock) as connect:
            await self.desktop.call({'action': 'connect', 'server': 'test'})
        settings = connect.call_args.args[1]['servers']['test']
        self.assertEqual(settings['username'], 'operator')
        self.assertEqual(settings['password_file'], '/private/password')

    async def test_connection_preserves_font_smoothing_and_themes(self):
        password = Path(self.tmp.name) / 'password'
        password.write_text('synthetic-test-password')
        password.chmod(0o600)
        self.conn.target = type('Target', (), {})()
        self.conn.ext_out_queue = asyncio.Queue()
        self.conn.desktop_buffer_has_data = True
        self.desktop.conn = None
        cfg = {'servers': {'test': {'username': 'test', 'host': '127.0.0.1',
            'password_file': str(password), 'certificate_file': 'unused'}}}
        with patch('evo_bot.rdp_worker.TrustedTLS', return_value=object()), \
             patch('evo_bot.rdp_worker.connect_verified', new=AsyncMock(return_value=(True, None))), \
             patch('evo_bot.rdp_worker.RDPConnectionFactory.from_url') as factory:
            factory.return_value.get_connection.return_value = self.conn
            await self.desktop.connect('test', cfg)
            settings = factory.call_args.args[1]
            self.assertTrue(settings.performance_flags & PERF.ENABLE_FONT_SMOOTHING)
            self.assertFalse(settings.performance_flags & PERF.DISABLE_THEMING)
            self.assertEqual(settings.performance_flags.to_bytes(4, 'little'), b'\x80\0\0\0')
        await self.desktop.close()

    async def action(self, name, **args):
        return await self.desktop.call({'action': name, 'server': 'test',
                                        'frame': self.desktop.frame, **args})

    async def test_image_is_real_jpeg_in_memory(self):
        result = self.desktop.screenshot()
        block = result['content'][1]
        with Image.open(io.BytesIO(base64.b64decode(block['data']))) as image:
            self.assertEqual(image.size, (1280, 720))
            self.assertEqual(image.format, 'JPEG')
        self.assertEqual(sorted(p.name for p in Path(self.tmp.name).iterdir()), ['rdp.json'])

    async def test_normalized_coordinates_and_new_frame(self):
        old = self.desktop.frame
        result = await self.action('click', x=1000, y=1000)
        self.assertEqual(self.conn.calls[0][2:4], (1279, 719))
        self.assertFalse(self.conn.calls[1][4])
        self.assertNotEqual(self.desktop.frame, old)
        self.assertEqual(result['content'][1]['type'], 'image')
        with self.assertRaisesRegex(DesktopError, 'Stale'):
            await self.action('click', frame=old, x=0, y=0)
        self.assertEqual(len(self.conn.calls), 2)

    async def test_keys_release_when_press_fails(self):
        self.conn.fail_press = True
        with self.assertRaises(ConnectionError):
            await self.action('key', key='Ctrl+a')
        self.assertEqual(self.conn.calls[-2:], [('key', 30, False, False), ('key', 29, False, False)])

    async def test_other_keys_release_when_one_release_fails(self):
        self.conn.fail_release = True
        with self.assertRaises(ConnectionError):
            await self.action('key', key='Ctrl+a')
        self.assertEqual(self.conn.calls[-1], ('key', 29, False, False))

    async def test_korean_text_and_unsupported_emoji_before_any_input(self):
        await self.action('type', text='한글')
        self.assertEqual(self.conn.calls, [('focus',), ('text', '한', True), ('text', '한', False),
                                         ('text', '글', True), ('text', '글', False)])
        self.conn.calls.clear()
        with self.assertRaises(DesktopError):
            await self.action('type', text='abc\U0001f600')
        self.assertEqual(self.conn.calls, [])

    async def test_focus_failure_never_types_or_presses_key(self):
        self.conn.fail_focus = True
        for action, args in [('key', {'key': 'Enter'}), ('type', {'text': 'test'})]:
            self.desktop.screenshot()
            with self.assertRaises(ConnectionError):
                await self.action(action, **args)
        self.assertEqual(self.conn.calls, [('focus',), ('focus',)])

    async def test_real_focus_packet_resets_keys_before_chord(self):
        # Use the installed serializer, not a mock that merely accepts keys.
        connection = object.__new__(RDPConnection)
        connection.cryptolayer = None
        connection._RDPConnection__joined_channels = {'MCS': type('Channel', (), {'channel_id': 1003})()}
        packets = []
        async def send(pdu, *args):
            packets.append(pdu.to_bytes())
            return True, None
        connection.handle_out_data = send
        self.desktop.conn = connection
        # Preserve the real transport methods while supplying a test framebuffer.
        connection.disconnected_evt = asyncio.Event()
        connection.get_desktop_buffer = self.conn.get_desktop_buffer
        await self.action('key', key='Ctrl+a')
        # 4-byte PDU header + 4-byte eventTime + 2-byte messageType.
        self.assertEqual([int.from_bytes(p[8:10], 'little') for p in packets],
                         [4, 0, 4, 4, 4, 4, 4])
        self.assertEqual(packets[1][10:], bytes(6))  # TS_SYNC_EVENT: all keys up.
        self.assertEqual([int.from_bytes(p[12:14], 'little') for p in packets[3:]], [29, 30, 30, 29])
        self.assertEqual([int.from_bytes(p[10:12], 'little') for p in packets[3:]], [0, 0, 0x8000, 0x8000])

    async def test_scroll_has_correct_signed_wire_delta(self):
        await self.action('scroll', direction='down', steps=1)
        call = self.conn.calls[-1]
        self.assertEqual(call[1], 'MOUSEBUTTON_WHEEL_UP')
        self.assertEqual(call[-1], -120)
        self.assertEqual((512 | (511 & call[-1])), 0x388)
        self.assertEqual(call[2:4], (640, 360))

    async def test_scroll_targets_last_clicked_pane(self):
        await self.action('click', x=750, y=650)
        await self.action('scroll', direction='up', steps=1)
        self.assertEqual(self.conn.calls[-1][2:4], (959, 467))

    async def test_disabled_and_wrong_server_never_send_input(self):
        self.data['instances']['test']['rdp']['enabled'] = False
        self.config.write_text(json.dumps(self.data))
        with self.assertRaises(PermissionError):
            await self.action('click', x=1, y=1)
        self.assertFalse(self.conn.calls)
        self.data['instances']['test']['rdp']['enabled'] = True
        self.config.write_text(json.dumps(self.data))
        with self.assertRaisesRegex(DesktopError, 'Wrong RDP server'):
            await self.action('click', server='other', x=1, y=1)
        self.assertFalse(self.conn.calls)

    async def test_wait_needs_no_frame_and_returns_fresh_image_without_input(self):
        self.desktop.frame = None
        result = await self.desktop.call({'action': 'wait', 'server': 'test', 'seconds': 0})
        self.assertTrue(self.desktop.frame)
        self.assertEqual(result['content'][1]['type'], 'image')
        self.assertFalse(self.conn.calls)

    async def test_wait_detects_disconnect_without_sending_input(self):
        self.conn.disconnected_evt.set()
        with self.assertRaisesRegex(ConnectionError, 'disconnected'):
            await self.desktop.call({'action': 'wait', 'server': 'test', 'seconds': 600})
        self.assertFalse(self.conn.calls)

    async def test_unsupported_key_does_not_consume_frame_or_send_focus(self):
        frame = self.desktop.frame
        with self.assertRaisesRegex(DesktopError, 'Unsupported key'):
            await self.action('key', key='not-a-key')
        self.assertEqual(self.desktop.frame, frame)
        self.assertFalse(self.conn.calls)

    async def test_worker_stale_frame_rejection_keeps_session_for_next_screenshot(self):
        requests = [dict(id=1, params={'action': 'click', 'server': 'test', 'frame': 'expired', 'x': 0, 'y': 0}),
                    dict(id=2, params={'action': 'screenshot', 'server': 'test'})]
        source = io.BytesIO(('\n'.join(json.dumps(r) for r in requests) + '\n').encode())
        output = io.StringIO()
        with patch.object(rdp_worker, 'Desktop', return_value=self.desktop), \
                patch.object(rdp_worker.sys, 'argv', ['worker', str(self.config)]), \
                patch.object(rdp_worker.sys, 'stdin', SimpleNamespace(buffer=source)), \
                patch.object(rdp_worker.sys, 'stdout', output), patch.object(rdp_worker.logging, 'disable'):
            await rdp_worker.main()
        first, second = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertTrue(first['recoverable'])
        self.assertIn('Stale', first['error'])
        self.assertEqual(second['result']['content'][1]['type'], 'image')
        self.assertFalse(self.conn.calls)


class AdapterChecks(unittest.TestCase):
    def test_owner_discord_only(self):
        for turn in (None, context.Turn('123', user='other'), context.Turn('123', platform='web'),
                     context.Turn('123', cron=True)):
            token = context.current.set(turn)
            try:
                with self.assertRaises(PermissionError):
                    rdp.remote_desktop({'action': 'servers'})
            finally:
                context.current.reset(token)

    def test_invalid_arguments(self):
        for args in ({'action': 'click', 'frame': 'x', 'x': True, 'y': 1},
                     {'action': 'click', 'frame': 'x', 'x': 1001, 'y': 1},
                     {'action': 'click', 'x': 1, 'y': 1},
                     {'action': 'connect', 'host': 'unconfigured'},
                     {'action': 'key', 'frame': 'x', 'key': ''},
                     {'action': 'secret_type', 'frame': 'x', 'text': 'raw'}):
            with self.assertRaises(ValueError):
                rdp.validate(args)

    def test_wait_bounds(self):
        for seconds in (0, .01, 600):
            rdp.validate({'action': 'wait', 'seconds': seconds})
        for seconds in (-1, 601, True, None, float('nan'), float('inf')):
            with self.assertRaises(ValueError):
                rdp.validate({'action': 'wait', 'seconds': seconds})

    def test_recoverable_worker_error_does_not_close_or_replay(self):
        client = rdp.RemoteDesktop(); client.server = 'test'
        with patch.object(rdp, 'configuration', return_value={'enabled': True, 'servers': {'test': {}}}), \
                patch.object(client, 'running', return_value=True), patch.object(client, 'close') as close, \
                patch.object(client, 'request', side_effect=rdp.RequestError('Stale RDP frame')) as request:
            with self.assertRaises(rdp.RequestError):
                client.call({'action': 'screenshot'}, context.Turn('123'))
        close.assert_not_called(); request.assert_called_once()

    def test_connection_fault_closes_without_replay(self):
        client = rdp.RemoteDesktop(); client.server = 'test'
        with patch.object(rdp, 'configuration', return_value={'enabled': True, 'servers': {'test': {}}}), \
                patch.object(client, 'running', return_value=True), patch.object(client, 'close') as close, \
                patch.object(client, 'request', side_effect=ConnectionError('Transport lost')) as request:
            with self.assertRaises(ConnectionError):
                client.call({'action': 'screenshot'}, context.Turn('123'))
        close.assert_called_once(); request.assert_called_once()

    def test_rdp_does_not_schedule_idle_disconnect(self):
        client = rdp.RemoteDesktop(); client.server = 'test'
        with patch.object(rdp, 'configuration', return_value={'enabled': True, 'servers': {'test': {}}}), \
                patch.object(client, 'running', return_value=True), patch.object(client, 'close') as close, \
                patch.object(client, 'request', return_value={'content': []}), \
                patch.object(rdp.threading, 'Timer') as timer:
            client.call({'action': 'screenshot'}, context.Turn('123'))
        close.assert_not_called(); timer.assert_not_called()

    def test_lazy_status_and_disabled_connect_do_not_spawn(self):
        client = rdp.RemoteDesktop()
        with patch.object(rdp, 'configuration', return_value={'enabled': False, 'servers': {'test': {}}}), \
                patch.object(rdp.subprocess, 'Popen', side_effect=AssertionError('Must not spawn')):
            self.assertEqual(client.call({'action': 'servers'}, context.Turn('123'))['servers'], ['test'])
            self.assertFalse(client.call({'action': 'status'}, context.Turn('123'))['connected'])
            with self.assertRaises(PermissionError):
                client.call({'action': 'connect', 'server': 'test'}, context.Turn('123'))
        client.close()

    def test_cancelled_request_does_not_spawn(self):
        turn = context.Turn('123')
        turn.cancelled.set()
        with patch.object(rdp, 'configuration', return_value={'enabled': True, 'servers': {'test': {}}}), \
                patch.object(rdp.subprocess, 'Popen', side_effect=AssertionError('Must not spawn')):
            with self.assertRaises(InterruptedError):
                rdp.RemoteDesktop().call({'action': 'connect', 'server': 'test'}, turn)

    def test_registered_only_for_owner_tools_not_cron(self):
        from evo_bot.tools import Registry
        registry = Registry()
        self.assertNotIn('remote_desktop', registry.tools)
        self.assertIn('remote_control', registry.tools)
        self.assertNotIn('remote_desktop', [t['name'] for t in registry.definitions(cron=True)])

    def test_key_contract(self):
        self.assertEqual(chord('Ctrl+a'), [(29, False), (30, False)])
        for key in ('Meta+r', 'Ctrl+not-a-key', 'Ctrl+Ctrl'):
            with self.assertRaises(DesktopError):
                chord(key)

    def test_instance_selector_and_no_cross_instance_disconnect(self):
        token = context.current.set(context.Turn('123'))
        try:
            with patch.object(rdp.CLIENT, 'call', return_value={'server': 'test', 'connected': False}) as call:
                value = rdp.remote_desktop({'action': 'status', 'instance': 'test'})
                self.assertEqual(value, {'instance': 'test', 'connected': False})
                self.assertEqual(call.call_args.args[0], {'action': 'status', 'server': 'test'})
                with self.assertRaises(ValueError):
                    rdp.remote_desktop({'action': 'status', 'instance': 'test', 'server': 'other'})
            client = rdp.RemoteDesktop(); client.server = 'first'
            with patch.object(client, 'close') as close:
                with self.assertRaises(ValueError):
                    client.call({'action': 'disconnect', 'server': 'second'}, context.Turn('123'))
                close.assert_not_called()
                with patch.object(rdp, 'configuration', return_value={'enabled': True, 'servers': {'first': {}, 'second': {}}}):
                    self.assertEqual(client.call({'action': 'status', 'server': 'second'}, context.Turn('123')),
                                     {'enabled': True, 'connected': False, 'server': 'second'})
        finally:
            context.current.reset(token)

    def test_tls_does_not_disable_verification(self):
        with patch('ssl.create_default_context') as factory:
            tls = TrustedTLS('/private/pc.pem')
            factory.assert_called_once_with(cafile='/private/pc.pem')
            self.assertFalse(tls.context.check_hostname)
            self.assertNotIn('verify_mode', vars(tls.context))


if __name__ == '__main__':
    unittest.main(verbosity=2)
