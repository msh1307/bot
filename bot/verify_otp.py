"""Synthetic OTP registration and existing private-input paths; no real seeds or logins."""
import base64
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from evo_bot import context
from evo_bot.integrations import credential_input as ci, host, rdp

SEED = base64.b32encode(b'12345678901234567890').decode()


class OTPChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / 'remote.json'
        self.config.write_text(json.dumps({'instances': {}, 'accounts': {}}))
        self.config.chmod(0o600)
        self.store = self.root / 'otp.json'
        self.patches = [patch.object(ci, 'CONFIG', self.config), patch.object(ci, 'OTP_CONFIG', self.store)]
        for item in self.patches: item.start()
        self.turn = context.Turn('123')
        self.token = context.current.set(self.turn)

    def tearDown(self):
        context.current.reset(self.token)
        for item in self.patches: item.stop()
        self.temp.cleanup()

    def register(self, account='test@example.test', seed=SEED, origins='https://otp-fixture.test'):
        return ci.register_otp(account, seed, origins)

    def test_register_persists_privately_and_listing_has_no_seed_or_path(self):
        self.assertEqual(self.register(), {'registered': 'test@example.test'})
        self.assertEqual(self.store.stat().st_mode & 0o777, 0o600)
        listing = ci.credential_input({'action': 'list'})
        self.assertEqual(listing, {'accounts': [{'id': 'test@example.test', 'username': 'test@example.test', 'otp': True}]})
        self.assertNotIn(SEED, json.dumps(listing)); self.assertNotIn(str(self.store), json.dumps(listing))
        with patch.object(ci.time, 'time', return_value=59), patch.object(ci.browser.CLIENT, 'enter_secret') as fill:
            result = ci.credential_input({'action': 'enter', 'account': 'test@example.test', 'field': 'otp'})
        self.assertEqual(result, {'entered': True})
        self.assertEqual(fill.call_args.args[:4], (':focus', ['https://otp-fixture.test'], 'otp', '287082'))
        self.assertNotIn('287082', json.dumps(result))

    def test_invalid_registration_leaves_store_unchanged_and_errors_do_not_echo(self):
        self.register(); original = self.store.read_bytes()
        for kwargs in ({'seed': 'NOT-A-VALID-SEED!'}, {'account': '../../bad'}, {'origins': 'http://bad.test'},
                       {'seed': 'otpauth://hotp/test?secret=' + SEED},
                       {'seed': 'otpauth://totp/test?secret=' + SEED + '&digits=9'},
                       {'seed': 'otpauth://totp/test?secret=' + SEED + '&secret=' + SEED}):
            with self.assertRaises(ValueError) as raised: self.register(**kwargs)
            self.assertNotIn(SEED, str(raised.exception))
            self.assertEqual(self.store.read_bytes(), original)

    def test_replace_and_atomic_failure_keep_previous_seed(self):
        self.register(); original = self.store.read_bytes()
        second = base64.b32encode(b'other-synthetic-key!!').decode()
        with patch.object(os, 'replace', side_effect=OSError('synthetic disk failure')):
            with self.assertRaises(OSError): self.register(seed=second)
        self.assertEqual(self.store.read_bytes(), original)
        self.assertEqual(list(self.root.glob('.otp-*')), [])
        self.register(seed=second)
        self.assertEqual(ci.otp_profiles()['test@example.test']['seed'], second)

    def test_existing_password_and_scopes_are_preserved(self):
        entry = {'username': 'operator', 'origins': ['https://otp-fixture.test'], 'password_file': '/private/password'}
        self.config.write_text(json.dumps({'instances': {}, 'accounts': {'test@example.test': entry}}))
        original = self.config.read_bytes(); self.register(origins='')
        self.assertEqual(ci.accounts()['test@example.test'], entry)
        self.assertEqual(self.config.read_bytes(), original)
        with self.assertRaises(ValueError): self.register(origins='https://other.test')
        with patch.object(ci, 'private_text', return_value='existing-password'):
            self.assertEqual(ci.secret_value('test@example.test', entry, 'password'), 'existing-password')

    def test_uri_parameters_match_rfc6238_and_period(self):
        for algorithm, raw, expected in [('SHA1', b'12345678901234567890', '94287082'),
                ('SHA256', b'12345678901234567890123456789012', '46119246'),
                ('SHA512', b'1234567890123456789012345678901234567890123456789012345678901234', '90693936')]:
            seed = base64.b32encode(raw).decode()
            uri = f'otpauth://totp/Fixture?secret={seed}&algorithm={algorithm}&digits=8&period=30'
            self.assertEqual(ci.totp(uri, at=59), expected)
        self.assertEqual(ci.totp(f'otpauth://totp/Fixture?secret={SEED}&period=60', at=119), '287082')

    def test_new_registration_survives_concurrent_writes(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda n: self.register(account=f'test{n}@example.test'), range(8)))
        self.assertEqual(len(ci.otp_profiles()), 8)

    def test_screenshot_values_include_seed_and_neighbor_codes(self):
        self.register()
        with patch.object(ci.time, 'time', return_value=59):
            values = ci.otp_sensitive_values()
        self.assertIn(SEED, values)
        self.assertIn('287082', values)
        for n in (-1, 0, 1): self.assertIn(ci.totp(SEED, at=59 + n * 30), values)

    def test_rdp_uses_same_registered_otp_after_existing_approval(self):
        self.config.write_text(json.dumps({'instances': {}, 'accounts': {'desktop': {
            'username': 'operator', 'password_file': '/private/password', 'rdp_instances': ['test']}}}))
        self.register(account='desktop', origins='')
        client = Mock(lock=threading.RLock(), server='test'); client.running.return_value = True
        with patch.object(rdp, 'CLIENT', client), patch.object(rdp, 'configuration', return_value={'enabled': True}), \
             patch.object(host, 'approve_once', return_value=None), patch.object(host, 'discord_connected', return_value=True), \
             patch.object(ci.time, 'time', return_value=59):
            result = ci.credential_input({'action': 'enter', 'account': 'desktop', 'field': 'otp',
                                         'instance': 'test', 'frame': 'observed'})
        self.assertEqual(result, {'entered': True})
        self.assertEqual(client.call.call_args.args[0]['text'], '287082')

    def test_existing_tool_registers_and_reads_fresh_otp_without_approval(self):
        registered = ci.credential_input({'action': 'register', 'account': 'test@example.test',
            'seed': SEED, 'origins': ['https://otp-fixture.test']})
        self.assertEqual(registered, {'registered': 'test@example.test'})
        with patch.object(ci.time, 'time', return_value=59), patch.object(host, 'approve_once') as approve:
            first = ci.credential_input({'action': 'read', 'account': 'test@example.test'})
        self.assertEqual(first, {'otp': '287082', 'expires_in': 1}); approve.assert_not_called()
        with patch.object(ci.time, 'time', return_value=60):
            second = ci.credential_input({'action': 'read', 'account': 'test@example.test'})
        self.assertNotEqual(first['otp'], second['otp']); self.assertEqual(second['expires_in'], 30)
        self.assertNotIn(SEED, json.dumps([registered, first, second]))

    def test_registration_adds_no_tool_or_discord_command(self):
        registry = Mock(); ci.register(registry)
        self.assertEqual(registry.register_tool.call_count, 1)
        self.assertEqual(registry.register_tool.call_args.args[0], 'auth')
        registry.register_platform_handler.assert_not_called()
        self.assertFalse(hasattr(ci, 'discord_factory'))

    def test_password_read_requires_reason_and_all_reads_require_owner(self):
        self.register()
        with self.assertRaises(ValueError):
            ci.credential_input({'action': 'read', 'account': 'test@example.test', 'field': 'password'})
        for turn in (None, context.Turn('123', user='other'), context.Turn('123', platform='web'),
                     context.Turn('123', cron=True)):
            token = context.current.set(turn)
            try:
                for action in ('register', 'read'):
                    with self.assertRaises(PermissionError):
                        ci.credential_input({'action': action, 'account': 'test@example.test', 'seed': SEED})
            finally:
                context.current.reset(token)

    def test_read_keeps_legacy_seed_files_and_custom_periods(self):
        seed_file = self.root / 'seed'
        seed_file.write_text(f' otpauth://totp/Fixture?secret={SEED}&period=60\n')
        seed_file.chmod(0o600)
        self.config.write_text(json.dumps({'instances': {}, 'accounts': {'legacy': {
            'username': 'operator', 'totp_file': str(seed_file)}}}))
        with patch.object(ci.time, 'time', return_value=89):
            self.assertEqual(ci.credential_input({'action': 'read', 'account': 'legacy'}),
                             {'otp': '287082', 'expires_in': 31})

    def test_otp_only_registration_needs_no_browser_url(self):
        ci.credential_input({'action': 'register', 'account': 'totp-only', 'seed': SEED})
        self.assertIn('totp-only', [a['id'] for a in ci.credential_input({'action': 'list'})['accounts']])
        with patch.object(ci.time, 'time', return_value=59):
            self.assertEqual(ci.credential_input({'action': 'read', 'account': 'totp-only'})['otp'], '287082')
        with self.assertRaises(ValueError), patch.object(ci.browser.CLIENT, 'enter_secret') as fill:
            ci.credential_input({'action': 'enter', 'account': 'totp-only', 'field': 'otp'})
        fill.assert_not_called()


if __name__ == '__main__': unittest.main(verbosity=2)
