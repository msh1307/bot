"""Single-file remote configuration and private references; no network."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from evo_bot import context
from evo_bot.integrations import remote_config as rc, remote, instances, ssh, rdp, codex, credential_input as ci
import register_credential


class Checks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path, self.old = self.root / 'remote-control.json', self.root / 'legacy.json'
        self.secret = self.root / 'password'; self.secret.write_text('SYNTHETIC_PRIVATE_VALUE\n'); self.secret.chmod(0o600)
        self.token_file = self.root / 'codex.token'; self.token_file.write_text('SYNTHETIC_PRIVATE_TOKEN\n'); self.token_file.chmod(0o600)
        self.data = {'defaults': {'ssh': 'linux', 'rdp': 'pc', 'codex': 'pc'}, 'instances': {
            'linux': {'host': '192.0.2.1', 'ssh': {'account': 'linux-login', 'known_hosts': str(self.root / 'known_hosts')}},
            'cloud': {'host': '198.51.100.1', 'ssh': {'account': 'cloud-login', 'known_hosts': str(self.root / 'known_hosts'), 'sudo_nopasswd': True}},
            'pc': {'host': '192.0.2.2', 'rdp': {'account': 'pc-login', 'certificate_file': '/private/rdp.pem'},
                   'codex': {'port': 4500, 'token_file': str(self.token_file), 'ca': '/private/ca.pem', 'cwd': 'C:\\Work'}}},
            'accounts': {
                'linux-login': {'username': 'operator', 'password_file': str(self.secret), 'sudo_password_file': str(self.secret), 'ssh_instances': ['linux']},
                'cloud-login': {'username': 'opc', 'identity_file': str(self.secret), 'ssh_instances': ['cloud']},
                'pc-login': {'username': 'operator', 'password_file': str(self.secret), 'rdp_instances': ['pc']}}}
        self.save()
        self.stack = contextlib.ExitStack()
        for mod in (rc, remote, ssh, rdp, codex, ci):
            self.stack.enter_context(patch.object(mod, 'CONFIG', self.path))
        self.identity = context.current.set(context.Turn('123'))

    def save(self):
        self.path.write_text(json.dumps(self.data)); self.path.chmod(0o600)

    def tearDown(self):
        context.current.reset(self.identity)
        self.stack.close(); self.tmp.cleanup()

    def test_all_protocols_and_account_refs_use_one_file(self):
        shell = ssh.configuration()['servers']
        self.assertEqual(shell['cloud']['username'], 'opc')
        self.assertEqual(shell['cloud']['identity_file'], str(self.secret))
        self.assertEqual(shell['linux']['password_file'], str(self.secret))
        self.assertEqual(rdp.configuration()['servers']['pc']['username'], 'operator')
        default, configured = codex.server_config()
        self.assertEqual(default, 'pc')
        self.assertEqual(configured['pc']['url'], 'wss://192.0.2.2:4500/')
        self.assertEqual(ci.accounts(), self.data['accounts'])

    def test_catalog_never_reads_key_password_or_token_contents(self):
        original = ci.private_text
        def read(path):
            self.assertEqual(Path(path), self.path)
            return original(path)
        with patch.object(ci, 'private_text', side_effect=read):
            rows = remote.catalog()
        self.assertEqual(rows['defaults'], self.data['defaults'])
        self.assertEqual({r['id']: r['protocols'] for r in rows['instances']},
                         {'localhost': [], 'pc': ['rdp', 'codex'], 'linux': ['ssh'], 'cloud': ['ssh']})
        text = json.dumps(rows)
        self.assertNotIn('SYNTHETIC_PRIVATE', text)
        self.assertNotIn(str(self.secret), text)

    def test_host_is_single_source_for_rdp_and_codex(self):
        self.data['instances']['pc']['host'] = '192.0.2.30'; self.save()
        self.assertEqual(rdp.configuration()['servers']['pc']['host'], '192.0.2.30')
        self.assertEqual(codex.server_config()[1]['pc']['url'], 'wss://192.0.2.30:4500/')

    def test_token_read_is_deferred_until_bridge_creation(self):
        entry = codex.server_config()[1]['pc']
        self.assertNotIn('token', entry)
        with patch.object(context, 'secret_values', []):
            bridge = codex.Bridge(entry, 'pc')
            self.assertEqual(bridge.settings['token'], 'SYNTHETIC_PRIVATE_TOKEN')
            self.assertIn('SYNTHETIC_PRIVATE_TOKEN', context.secret_values)
        self.assertNotIn('token', entry)

    def test_disabled_protocol_is_not_dispatched_or_defaulted(self):
        self.data['instances']['cloud']['ssh']['enabled'] = False; self.save()
        self.assertNotIn('cloud', ssh.configuration()['servers'])
        with patch.object(ssh, 'remote_ssh') as execute:
            with self.assertRaises(ValueError):
                remote.remote_control({'instance': 'cloud', 'protocol': 'ssh', 'action': 'exec', 'args': {'command': 'id'}})
            execute.assert_not_called()
        self.data['defaults']['ssh'] = 'cloud'; self.save()
        with self.assertRaises(ValueError): remote.catalog()

    def test_present_invalid_config_never_falls_back_to_legacy(self):
        self.old.write_text(json.dumps({'servers': {'legacy': {}}})); self.old.chmod(0o600)
        self.path.write_text('{bad')
        with self.assertRaises(ValueError): rc.protocol('ssh', self.path)
        self.path.unlink()
        self.assertIsNone(rc.protocol('ssh', self.path))
        self.path.write_text(self.old.read_text()); self.path.chmod(0o600)
        with self.assertRaises(ValueError): rc.protocol('ssh', self.path)
        self.path.unlink()
        self.path.symlink_to(self.root / 'missing')
        with self.assertRaises(ValueError): rc.protocol('ssh', self.path)

    def test_public_config_and_inline_secret_or_mismatched_host_rejected(self):
        self.path.chmod(0o644)
        with self.assertRaises(ValueError): remote.catalog()
        for field, value in (('token', 'NO_INLINE_SECRET'), ('url', 'wss://other.example/'), ('port', True)):
            original = copy.deepcopy(self.data)
            self.data['instances']['pc']['codex'][field] = value; self.save()
            with self.assertRaises(ValueError): remote.catalog()
            self.data = original

    @unittest.skipUnless(os.geteuid() == 0, 'Registration requires root')
    def test_account_registration_preserves_instances_defaults_and_file_bytes(self):
        before = copy.deepcopy(self.data); secret = self.secret.read_bytes()
        parent = (self.root.stat().st_mode, self.root.stat().st_uid, self.root.stat().st_gid)
        with contextlib.redirect_stdout(io.StringIO()):
            register_credential.main(['new-login', '--username', 'operator', '--password-file', str(self.secret), '--ssh-instance', 'linux'])
        after = json.loads(self.path.read_text())
        self.assertEqual(after['instances'], before['instances'])
        self.assertEqual(after['defaults'], before['defaults'])
        self.assertEqual(self.secret.read_bytes(), secret)
        self.assertEqual(after['accounts']['new-login']['password_file'], str(self.secret))
        self.assertEqual((self.root.stat().st_mode, self.root.stat().st_uid, self.root.stat().st_gid), parent)


if __name__ == '__main__': unittest.main()
