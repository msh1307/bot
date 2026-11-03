"""Synthetic secret-entry checks. No real accounts, Discord messages or RDP login.

Run on EVO with the bot venv; --browser also uses the real Headless Shell
against intercepted HTTPS fixture pages (no external requests).
"""
import base64
import json
import io
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch, Mock

from evo_bot import context
from evo_bot.integrations import credential_input as ci
from evo_bot.integrations import host, rdp

REAL_BROWSER = '--browser' in sys.argv
if REAL_BROWSER:
    sys.argv.remove('--browser')


class CredentialChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.password = self.root / 'password'
        self.password.write_text('synthetic-password-NOT-A-REAL-SECRET\n')
        self.password.chmod(0o600)
        self.seed = self.root / 'totp'
        self.seed.write_text(base64.b32encode(b'12345678901234567890').decode())
        self.seed.chmod(0o600)
        self.config = self.root / 'accounts.json'
        self.entry = {'origins': ['https://credential-fixture.test'],
                      'password_file': str(self.password), 'totp_file': str(self.seed)}
        self.save()
        self.config_patch = patch.object(ci, 'CONFIG', self.config)
        self.config_patch.start()
        self.turn = context.Turn('123')
        self.token = context.current.set(self.turn)

    def tearDown(self):
        context.current.reset(self.token)
        self.config_patch.stop()
        self.temp.cleanup()

    def save(self):
        self.config.write_text(json.dumps({'instances': {}, 'accounts': {'fixture@example.test': self.entry}}))
        self.config.chmod(0o600)

    def enter(self, **args):
        return ci.credential_input({'action': 'enter', 'account': 'fixture@example.test',
                                    'field': 'password', 'selector': '#password', **args})

    def test_listing_never_reads_values_or_returns_paths(self):
        with patch.object(ci, 'private_text', wraps=ci.private_text) as read:
            listed = ci.credential_input({'action': 'list'})
        self.assertEqual(read.call_count, 1)  # Config only.
        self.assertEqual(listed, {'accounts': [{'id': 'fixture@example.test', 'username': 'fixture@example.test'}]})
        self.assertNotIn(str(self.root), json.dumps(listed))
        self.assertNotIn('NOT-A-REAL-SECRET', json.dumps(listed))

    def test_empty_unconfigured_store(self):
        self.config.unlink()
        self.assertEqual(ci.credential_input({'action': 'list'}), {'accounts': []})

    def test_named_accounts_require_a_username_and_email_ids_remain_compatible(self):
        for name in ('fixture', 'user@example.test\n', 'first last@example.test', 'x' * 65 + '@example.test'):
            self.config.write_text(json.dumps({'instances': {}, 'accounts': {name: self.entry}}))
            with self.assertRaises(ValueError):
                ci.accounts()
        self.config.write_text(json.dumps({'instances': {}, 'accounts': {'fixture': dict(self.entry, username='operator')}}))
        self.assertEqual(ci.accounts()['fixture']['username'], 'operator')

    def test_browser_receives_value_privately_not_in_result(self):
        with patch.object(ci.browser.CLIENT, 'enter_secret', return_value={'evil_echo': self.password.read_text()}) as fill:
            result = self.enter()
        self.assertEqual(result, {'entered': True})
        self.assertEqual(fill.call_args.args[3], self.password.read_text().rstrip('\n'))
        self.assertNotIn('value', fill.call_args.kwargs)

    def test_totp_matches_all_rfc6238_sha1_vectors(self):
        seed = self.seed.read_text()
        for at, expected in [(59, '94287082'), (1111111109, '07081804'),
                (1111111111, '14050471'), (1234567890, '89005924'),
                (2000000000, '69279037'), (20000000000, '65353130')]:
            self.assertEqual(ci.totp(seed, at, digits=8), expected)
        with patch.object(ci.time, 'time', return_value=59), patch.object(ci.browser.CLIENT, 'enter_secret') as fill:
            self.assertEqual(self.enter(field='otp', selector='#otp'), {'entered': True})
        self.assertEqual(fill.call_args.args[3], '287082')

    def test_click_then_enter_defaults_to_focused_password_field(self):
        with patch.object(ci.browser.CLIENT, 'enter_secret') as fill:
            result = ci.credential_input({'action': 'enter', 'account': 'fixture@example.test'})
        self.assertEqual(result, {'entered': True})
        self.assertEqual(fill.call_args.args[:3], (':focus', self.entry['origins'], 'password'))

    def test_remote_targets_unknown_account_and_raw_value_rejected(self):
        with patch.object(ci.browser.CLIENT, 'enter_secret') as browser:
            for args in ({'instance': 'other', 'frame': 'x'}, {'instance': 'test', 'frame': 'x'},
                         {'account': 'not-registered'}, {'value': 'injected'}, {'field': 'unknown'},
                         {'selector': ''}):
                with self.assertRaises(ValueError):
                    self.enter(**args)
            for args in ({'instance': 'other', 'frame': 'x'}, {'instance': 'test'}):
                with self.assertRaises(ValueError):
                    ci.credential_input({'action': 'enter', 'account': 'fixture@example.test', 'field': 'password', **args})
        browser.assert_not_called()

    def test_owner_only_no_web_no_cron_no_cancelled_input(self):
        for turn in (None, context.Turn('123', user='other'), context.Turn('123', platform='web'),
                     context.Turn('123', cron=True)):
            token = context.current.set(turn)
            try:
                with self.assertRaises(PermissionError):
                    self.enter()
            finally:
                context.current.reset(token)
        self.turn.cancelled.set()
        with self.assertRaises(PermissionError):
            self.enter()

    def test_private_file_checks_and_no_path_or_value_in_error(self):
        for mode in (0o644, 0o660, 0o700):
            self.password.chmod(mode)
            with self.assertRaises(ValueError) as error:
                self.enter()
            self.assertNotIn(str(self.password), str(error.exception))
        self.password.unlink()
        self.password.symlink_to(self.seed)
        with self.assertRaises(ValueError):
            self.enter()

    def test_no_control_keys_or_empty_value(self):
        for value in ('', 'password\nEnter', 'password\t', 'password\x00', 'a' * 2001):
            self.password.write_text(value)
            with patch.object(ci.browser.CLIENT, 'enter_secret') as fill:
                with self.assertRaises(ValueError):
                    self.enter()
                fill.assert_not_called()

    def test_transport_errors_never_echo_secret_or_replay(self):
        for error in (RuntimeError(self.password.read_text()), InterruptedError(self.password.read_text())):
            with patch.object(ci.browser.CLIENT, 'enter_secret', side_effect=error) as fill:
                with self.assertRaises((ValueError, InterruptedError)) as raised:
                    self.enter()
                self.assertNotIn('NOT-A-REAL-SECRET', str(raised.exception))
                self.assertEqual(fill.call_count, 1)

    def test_exact_https_origins_no_wildcards_paths_or_userinfo(self):
        for origin in ['http://credential-fixture.test', 'https://credential-fixture.test/login',
                       'https://user@credential-fixture.test', 'https://credential-fixture.test?x=1']:
            self.entry['origins'] = [origin]; self.save()
            with self.assertRaises(ValueError):
                ci.accounts()

    def test_tool_is_discord_only_and_not_in_scheduled_catalog(self):
        from evo_bot.tools import Registry
        registry = Registry()
        self.assertIn('credential_input', [t['name'] for t in registry.definitions()])
        self.assertNotIn('credential_input', [t['name'] for t in registry.definitions(cron=True)])

    def rdp_args(self):
        self.entry['rdp_instances'] = ['test']; self.save()
        return {'action': 'enter', 'account': 'fixture@example.test', 'instance': 'test', 'frame': 'observed'}

    def test_rdp_list_is_target_scoped_and_contains_no_secret(self):
        self.rdp_args()
        self.assertEqual(ci.credential_input({'action': 'list', 'instance': 'test'}),
                         {'accounts': [{'id': 'fixture@example.test', 'username': 'fixture@example.test'}]})
        self.assertEqual(ci.credential_input({'action': 'list', 'instance': 'other'}), {'accounts': []})
        self.entry['rdp_instances'] = ['bad@host']; self.save()
        with self.assertRaises(ValueError): ci.accounts()

    def test_rdp_input_is_private_and_returns_no_frame_or_echo(self):
        args = self.rdp_args()
        client = Mock(lock=threading.RLock(), server='test')
        client.running.return_value = True
        client.call.return_value = {'text': self.password.read_text(), 'content': [{'type': 'image', 'data': 'raw'}]}
        with patch.object(rdp, 'CLIENT', client), patch.object(rdp, 'configuration', return_value={'enabled': True}), \
             patch.object(host, 'approve_once', return_value=None) as approve, \
             patch.object(host, 'discord_connected', return_value=True):
            result = ci.credential_input(args)
        self.assertEqual(result, {'entered': True})
        sent = client.call.call_args.args[0]
        self.assertEqual(sent['text'], self.password.read_text().strip())
        self.assertEqual(sent['action'], 'type'); self.assertEqual(sent['frame'], 'observed')
        self.assertNotIn('NOT-A-REAL-SECRET', repr(approve.call_args))
        self.assertEqual(approve.call_args.kwargs['kind'], 'credential')

    def test_rdp_denial_never_reads_value_or_types(self):
        args = self.rdp_args()
        client = Mock(lock=threading.RLock(), server='test'); client.running.return_value = True
        with patch.object(rdp, 'CLIENT', client), patch.object(rdp, 'configuration', return_value={'enabled': True}), \
             patch.object(host, 'approve_once', return_value={'success': False}), \
             patch.object(ci, 'private_text', wraps=ci.private_text) as reader:
            self.assertEqual(ci.credential_input(args), {'success': False})
        self.assertEqual([call.args[0] for call in reader.call_args_list], [ci.CONFIG])
        client.call.assert_not_called()

    def test_rdp_requires_frame_allowed_pc_and_existing_connection(self):
        args = self.rdp_args()
        with patch.object(host, 'approve_once') as approve:
            for changes in ({'frame': ''}, {'instance': 'other'}, {'selector': '#pwd'}):
                with self.assertRaises(ValueError): ci.credential_input(args | changes)
            with patch.object(rdp.CLIENT, 'running', return_value=False):
                with self.assertRaises(ValueError): ci.credential_input(args)
        approve.assert_not_called()

    def test_rdp_stale_or_interrupted_input_never_echoes_secret_or_retries(self):
        args = self.rdp_args()
        client = Mock(lock=threading.RLock(), server='test'); client.running.return_value = True
        for failure in (ValueError(self.password.read_text()), InterruptedError(self.password.read_text())):
            client.call.reset_mock(); client.call.side_effect = failure
            with patch.object(rdp, 'CLIENT', client), patch.object(rdp, 'configuration', return_value={'enabled': True}), \
                 patch.object(host, 'approve_once', return_value=None), patch.object(host, 'discord_connected', return_value=True):
                with self.assertRaises((ValueError, InterruptedError)) as raised: ci.credential_input(args)
            self.assertNotIn('NOT-A-REAL-SECRET', str(raised.exception))
            self.assertEqual(client.call.call_count, 1)


class ApprovalChecks(unittest.TestCase):
    def setUp(self):
        self.turn = context.Turn('123')
        self.token = context.current.set(self.turn)

    def tearDown(self): context.current.reset(self.token)

    def gate(self, key='test', **kwargs):
        return host.approve_once(self.turn, 'Synthetic action', 'No real execution',
                                 (self.turn.key, self.turn.id, key), **kwargs)

    def test_no_live_owner_no_approval(self):
        with patch.object(context, 'wait_for_approval') as wait:
            self.assertFalse(self.gate()['success'])
        wait.assert_not_called()

    def test_denial_or_expiry_blocks_new_requests_in_same_turn(self):
        for choice in ({'resolved': True, 'choice': 'deny'}, {'resolved': False, 'cancelled': True}):
            turn = context.Turn('123'); self.turn = turn
            with patch.object(context, 'live_owner', return_value=turn), \
                 patch.object(host, 'discord_connected', return_value=True), \
                 patch.object(context, 'wait_for_approval', return_value=choice) as wait:
                self.assertEqual(self.gate()['approval_status'], 'denied_or_expired')
                self.assertEqual(self.gate(key='different')['approval_status'], 'refused_this_turn')
                self.assertEqual(wait.call_count, 1)

    def test_once_is_exact_and_is_not_a_session_grant(self):
        with patch.object(context, 'live_owner', return_value=self.turn), \
             patch.object(host, 'discord_connected', return_value=True), \
             patch.object(context, 'wait_for_approval', return_value={'resolved': True, 'choice': 'once'}) as wait:
            self.assertIsNone(self.gate())
            host.remember_root_execution((self.turn.key, self.turn.id, 'test'))
            self.assertEqual(self.gate()['approval_status'], 'duplicate_execution')
            self.assertIsNone(self.gate(key='other'))
            self.assertEqual(wait.call_count, 2)

    def test_session_choice_or_disconnect_never_approves(self):
        with patch.object(context, 'live_owner', return_value=self.turn), \
             patch.object(host, 'discord_connected', return_value=True), \
             patch.object(context, 'wait_for_approval', return_value={'resolved': True, 'choice': 'session'}):
            self.assertEqual(self.gate()['approval_status'], 'denied_or_expired')

    def test_credential_denial_does_not_grant_or_deny_root(self):
        with patch.object(context, 'live_owner', return_value=self.turn), \
             patch.object(host, 'discord_connected', return_value=True), \
             patch.object(context, 'wait_for_approval', side_effect=[{'resolved': True, 'choice': 'deny'},
                     {'resolved': True, 'choice': 'once'}]):
            self.assertEqual(self.gate(kind='credential')['approval_status'], 'denied_or_expired')
            self.assertIsNone(self.gate())

    def test_existing_host_root_denial_cancels_ticket_without_execution(self):
        ticket = {'success': True, 'request_id': 'synthetic', 'token': 'test', 'digest': 'test',
                  'payload': {'run_as': 'root', 'command': 'id -u', 'cwd': '/workspace', 'reason': 'Test'}}
        def broker(payload, *args):
            if payload['action'] == 'stage': return ticket
            self.assertEqual(payload['action'], 'cancel')
            return {'success': True}
        with patch.object(context, 'live_owner', return_value=self.turn), \
             patch.object(host, 'discord_connected', return_value=True), \
             patch.object(context, 'wait_for_approval', return_value={'resolved': True, 'choice': 'deny'}), \
             patch.object(host, 'rpc', side_effect=broker) as rpc:
            denied = json.loads(host.host_command({'run_as': 'root', 'command': 'id -u', 'reason': 'Test'}))
        self.assertEqual(denied['approval_status'], 'denied_or_expired')
        self.assertEqual([c.args[0]['action'] for c in rpc.call_args_list], ['stage', 'cancel'])


@unittest.skipUnless(REAL_BROWSER, 'use --browser for actual Headless Shell fixture')
class BrowserChecks(unittest.TestCase):
    def test_actual_renderer_field_and_origin_checks(self):
        from playwright.sync_api import sync_playwright
        from evo_bot.browser_worker import Worker, BINARY
        with tempfile.TemporaryDirectory() as profile, sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(profile, executable_path=BINARY,
                        headless=True, chromium_sandbox=False)
            try:
                page = ctx.pages[0]
                page.route('**/*', lambda route: route.fulfill(content_type='text/html',
                    body='<form><input id="password" type="password"><input id="otp" autocomplete="one-time-code">'
                         '<input id="plain"></form><form action="https://other.test/post">'
                         '<input id="cross" type="password"></form>'))
                page.goto('https://credential-fixture.test/login')
                worker = object.__new__(Worker); worker.page = page; worker.cache = ('stale',)
                args = {'selector': '#password', 'field': 'password', 'value': 'SYNTHETIC_ONLY',
                        'origins': ['https://credential-fixture.test']}
                self.assertEqual(worker.enter_secret(args), {'entered': True})
                self.assertIsNone(worker.cache)
                self.assertEqual(page.locator('#password').input_value(), 'SYNTHETIC_ONLY')
                page.locator('#password').click()
                self.assertEqual(worker.enter_secret(dict(args, selector=':focus')), {'entered': True})
                self.assertEqual(worker.enter_secret(dict(args, selector='#otp', field='otp', value='123456')),
                                 {'entered': True})
                for bad in ({'selector': '#plain'}, {'selector': '#cross'}, {'selector': 'input'},
                            {'origins': ['https://other.test']}, {'field': 'otp', 'selector': '#plain'}):
                    with self.assertRaises(ValueError):
                        worker.enter_secret(dict(args, **bad))
                page.goto('https://credential-fixture.test.evil.test')
                with self.assertRaises(ValueError):
                    worker.enter_secret(args)
            finally:
                ctx.close()

    def test_screenshots_mask_revealed_password_and_otp_without_changing_page(self):
        from PIL import Image
        from playwright.sync_api import sync_playwright
        from evo_bot.browser_worker import Worker, BINARY
        with tempfile.TemporaryDirectory() as profile, sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(profile, executable_path=BINARY,
                        headless=True, chromium_sandbox=False,viewport={'width':960,'height':640})
            try:
                page = ctx.pages[0]
                html = '''<style>input {display:block;width:300px;height:40px;background:#ff00ff;margin:10px}</style>
                    <form><input id="email" value="fixture@example.test">
                    <input id="password" type="password"><input id="otp" autocomplete="one-time-code" value="123456"></form>
                    <input id="unfilled" type="password" value="NOT_ENTERED_WITH_TOOL">
                    <iframe srcdoc='<input type="password" value="FRAME_ONLY" style="width:150px;height:40px;background:#ff00ff">'></iframe>'''
                page.route('**/*',lambda route:route.fulfill(content_type='text/html',body=html))
                page.goto('https://credential-fixture.test/login')
                worker=object.__new__(Worker);worker.page=page;worker.cache=None;worker.http_status=200;worker.images_enabled=False
                worker.enter_secret({'selector':'#password','field':'password','value':'SYNTHETIC_ONLY',
                                     'origins':['https://credential-fixture.test']})
                page.locator('#password').evaluate("e => e.type='text'")
                result=worker.call({'action':'screenshot'})
                block=result['content'][1]
                image=Image.open(io.BytesIO(base64.b64decode(block['data']))).convert('RGB')
                for selector in ('#password','#otp','#unfilled'):
                    box=page.locator(selector).bounding_box()
                    pixel=image.getpixel((round(box['x']+box['width']-20),round(box['y']+box['height']/2)))
                    self.assertTrue(max(pixel)<12, (selector,pixel))
                child=page.frames[1].locator('input').bounding_box()
                self.assertTrue(max(image.getpixel((round(child['x']+child['width']-20),round(child['y']+child['height']/2))))<12)
                box=page.locator('#email').bounding_box()
                pixel=image.getpixel((round(box['x']+box['width']-20),round(box['y']+box['height']/2)))
                self.assertGreater(pixel[0],200);self.assertGreater(pixel[2],200)
                self.assertEqual(page.locator('#password').input_value(),'SYNTHETIC_ONLY')
                self.assertEqual(page.locator('#password').get_attribute('type'),'text')
                self.assertEqual(page.locator('#otp').input_value(),'123456')
                self.assertNotIn('SYNTHETIC_ONLY',json.dumps(result))
                # A screenshot error must not produce an unmasked fallback frame.
                with patch.object(page,'screenshot',side_effect=RuntimeError('fixture mask failure')) as shot:
                    with self.assertRaises(RuntimeError):worker.call({'action':'screenshot'})
                    self.assertEqual(shot.call_count,1)
                    self.assertIn('mask',shot.call_args.kwargs)
            finally:
                ctx.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
