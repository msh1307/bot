"""Portable support bundle checks; no real server, credentials or network."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

BOT = Path(__file__).resolve().parent
DEPLOY = (BOT / 'deploy' if (BOT / 'deploy').is_dir() else BOT.parent / 'deploy') / 'codex-server'
spec = importlib.util.spec_from_file_location('support_apply', DEPLOY / 'linux/apply.py')
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class BundleChecks(unittest.TestCase):
    def test_catalog_resolves_only_support_files(self):
        catalog = json.loads((DEPLOY / 'manifest.json').read_text())
        for platform in ('windows', 'linux'):
            for name in catalog[platform]['files']:
                self.assertTrue((DEPLOY / platform / name).is_file(), name)
                self.assertNotIn(Path(name).suffix, ('.exe', '.key', '.pem'))

    def test_apply_is_atomic_backed_up_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'bin/codex-rpc'
            target.parent.mkdir()
            target.write_text('old helper')
            before = target.read_bytes()
            planned = helper.apply(target, check=True)
            self.assertTrue(planned['changed'])
            self.assertEqual(before, target.read_bytes())
            applied = helper.apply(target)
            self.assertEqual(Path(applied['backup']).read_bytes(), before)
            self.assertEqual(target.read_bytes(), (DEPLOY / 'linux/codex-rpc.py').read_bytes())
            self.assertFalse(helper.apply(target)['changed'])

    def test_apply_does_not_follow_link(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secret = root / 'private'
            secret.write_text('fixture only')
            link = root / 'helper'
            link.symlink_to(secret)
            with self.assertRaises(ValueError): helper.apply(link)
            self.assertEqual(secret.read_text(), 'fixture only')

    def test_pi_mount_has_discoverable_readonly_bundle(self):
        from evo_bot import rpc
        args = rpc.sandbox_argv(Path('/fixture'), 'fixture')
        index = args.index('/home/agent/deploy')
        self.assertEqual(args[index - 2:index], ['--ro-bind', str(rpc.ROOT / 'deploy')])
        catalog = json.loads((BOT / 'config/index.json').read_text())
        self.assertEqual(catalog['server_deploy']['read'], '/home/agent/deploy/codex-server/manifest.json')


if __name__ == '__main__': unittest.main(verbosity=2)
