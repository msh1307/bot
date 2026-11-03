"""Shared authentication references, scopes and private registration; no network or real accounts."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from evo_bot import context
from evo_bot.integrations import credential_input as ci, ssh, rdp
import register_credential


class Checks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.password = self.root / 'password'; self.password.write_text('SYNTHETIC_REGISTRY_SECRET\n'); self.password.chmod(0o600)
        self.config = self.root / 'accounts.json'
        self.entry = {'username': 'operator', 'password_file': str(self.password),
                      'sudo_password_file': str(self.password), 'ssh_instances': ['linux'], 'rdp_instances': ['pc']}
        self.save()
        self.pin = patch.object(ci, 'CONFIG', self.config); self.pin.start()
        self.token = context.current.set(context.Turn('123'))

    def save(self):
        self.config.write_text(json.dumps({'instances': {}, 'accounts': {'main-login': self.entry}})); self.config.chmod(0o600)

    def tearDown(self):
        context.current.reset(self.token); self.pin.stop(); self.tmp.cleanup()

    def test_ssh_rdp_resolve_same_account_without_reading_secret_values(self):
        with patch.object(ci, 'private_text', wraps=ci.private_text) as reader:
            shell = ci.resolve_connection('ssh', 'linux', {'host': '192.0.2.1', 'account': 'main-login'})
            desktop = ci.resolve_connection('rdp', 'pc', {'host': '192.0.2.2', 'account': 'main-login'})
        self.assertEqual([c.args[0] for c in reader.call_args_list], [self.config, self.config])
        self.assertEqual(shell['username'], desktop['username'])
        self.assertEqual(shell['password_file'], desktop['password_file'])
        self.assertEqual(shell['sudo_password_file'], str(self.password))
        self.assertNotIn('sudo_password_file', desktop)

    def test_connections_require_an_account_reference(self):
        for protocol in ('ssh', 'rdp'):
            with self.assertRaises(ValueError):
                ci.resolve_connection(protocol, 'linux', {'username': 'old', 'password_file': str(self.password)})

    def test_scope_and_ambiguous_authentication_fail_closed(self):
        for protocol, target, extra in [('ssh', 'pc', {}), ('rdp', 'linux', {}), ('rdp', 'other', {}),
                                        ('ssh', 'linux', {'username': 'other'}),
                                        ('rdp', 'pc', {'password_file': str(self.password)})]:
            with self.assertRaises(ValueError): ci.resolve_connection(protocol, target, {'account': 'main-login', **extra})
        with self.assertRaises(ValueError): ci.resolve_connection('ssh', 'linux', {'account': 'unknown'})

    def test_metadata_and_browser_listing_never_show_secrets_or_unrelated_accounts(self):
        self.assertEqual(ci.credential_input({'action': 'list'}), {'accounts': []})
        expected = {'accounts': [{'id': 'main-login', 'username': 'operator'}]}
        self.assertEqual(ci.credential_input({'action': 'list', 'instance': 'pc'}), expected)
        self.entry['origins'] = ['https://example.test']; self.save()
        self.assertEqual(ci.credential_input({'action': 'list'}), expected)
        self.assertNotIn(str(self.password), json.dumps(expected))

    def test_configuration_resolvers_share_the_registry(self):
        for module, target in ((ssh, 'linux'), (rdp, 'pc')):
            config = self.root / (target + '.json')
            protocol = 'ssh' if module is ssh else 'rdp'
            config.write_text(json.dumps({'defaults': {protocol: target}, 'instances': {target: {
                'host': '192.0.2.1', protocol: {'known_hosts': str(self.root / 'known_hosts'), 'account': 'main-login'}}}}))
            config.chmod(0o600)
            with patch.object(module, 'CONFIG', config):
                entry = module.configuration()['servers'][target]
                self.assertEqual(entry['username'], 'operator')
                self.assertEqual(entry['password_file'], str(self.password))

    def test_key_auth_and_sudo_are_independent(self):
        self.entry.pop('password_file'); self.entry.pop('sudo_password_file')
        self.entry['identity_file'] = str(self.password); self.save()
        result = ci.resolve_connection('ssh', 'linux', {'account': 'main-login'})
        self.assertEqual(result['identity_file'], str(self.password))
        self.assertNotIn('sudo_password_file', result)
        with self.assertRaises(ValueError): ci.resolve_connection('rdp', 'pc', {'account': 'main-login'})

    @unittest.skipUnless(os.geteuid() == 0, 'Registration is host-root only')
    def test_registration_uses_file_refs_atomically_and_requires_replace(self):
        original = self.password.read_bytes()
        argv = ['second-login', '--username', 'second', '--password-file', str(self.password), '--ssh-instance', 'linux']
        out = io.StringIO()
        with contextlib.redirect_stdout(out): register_credential.main(argv)
        self.assertEqual(json.loads(out.getvalue()), {'registered': 'second-login', 'username': 'second'})
        self.assertEqual(self.password.read_bytes(), original)
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o640)
        self.assertEqual(ci.accounts()['second-login']['password_file'], str(self.password))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit): register_credential.main(argv)
        with contextlib.redirect_stdout(io.StringIO()): register_credential.main(argv + ['--replace'])

    def test_no_plaintext_password_registration_option(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            register_credential.main(['bad', '--username', 'operator', '--password', 'DO_NOT_ECHO_THIS'])
        self.assertNotIn('DO_NOT_ECHO_THIS', err.getvalue())


if __name__ == '__main__': unittest.main()
