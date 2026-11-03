"""Binary transfer and raw Pi I/O regressions; native edits are tested separately."""
import base64
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from evo_bot import context, remote_file_worker as worker
from evo_bot.integrations import remote_files as remote


class RemoteFileChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.file = self.root / 'code.py'; self.file.write_text('value = 42\n')
        self.turn = context.Turn('123'); self.token = context.current.set(self.turn)
        self.pin = patch.object(remote, 'WORKSPACE', self.root); self.pin.start()
        self.cfg = patch.object(remote.ssh, 'configuration', return_value={'servers': {'fixture': {}}}); self.cfg.start()

    def tearDown(self):
        context.current.reset(self.token); self.pin.stop(); self.cfg.stop(); self.tmp.cleanup()

    def call(self, action, path=None, **args):
        with patch.object(remote, 'exchange', side_effect=lambda entry, req, turn: worker.operate(req)):
            return remote.transfer(dict(instance='fixture', action=action, path=str(path or self.file), **args))

    def test_binary_transfer_has_no_model_payload(self):
        raw = bytes(range(256)) * 100; (self.root / 'binary').write_bytes(raw)
        dest = self.root / 'remote'
        self.call('upload', dest, local_path='/workspace/binary')
        result = self.call('download', dest, local_path='/workspace/back')
        self.assertEqual(dest.read_bytes(), raw); self.assertEqual((self.root / 'back').read_bytes(), raw)
        self.assertEqual(result, {'bytes': len(raw), 'local_path': '/workspace/back'})
        self.assertNotIn(base64.b64encode(raw).decode(), str(result))

    def test_upload_never_overwrites_remote(self):
        (self.root / 'source').write_bytes(b'wrong')
        with self.assertRaisesRegex(ValueError, 'read|Read'):
            self.call('upload', local_path='source')
        self.assertEqual(self.file.read_text(), 'value = 42\n')

    def test_local_paths_cannot_escape_workspace(self):
        (self.root / 'link').symlink_to('/tmp')
        for path in ['/etc/passwd', '../outside', '/workspace/../outside', '/workspace/link/outside']:
            with self.assertRaises(ValueError): remote.local_path(path)

    def test_download_never_overwrites_local(self):
        (self.root / 'existing').write_text('keep')
        with patch.object(remote, 'exchange') as exchange:
            with self.assertRaisesRegex(ValueError, 'exists'): self.call('download', local_path='existing')
            exchange.assert_not_called()
        self.assertEqual((self.root / 'existing').read_text(), 'keep')

    def test_special_and_large_files(self):
        fifo = self.root / 'fifo'; os.mkfifo(fifo)
        with self.assertRaises(ValueError): worker.read_bytes(fifo)
        with self.file.open('wb') as out: out.truncate(worker.MAX_FILE+1)
        with self.assertRaises(ValueError): self.call('upload', self.root / 'other', local_path='code.py')

    def test_wrong_owner_schedule_and_cancel(self):
        for change in [{'user':'other'}, {'platform':'web'}, {'cron':True}]:
            token = context.current.set(context.Turn('123', **change))
            try:
                with self.assertRaises(PermissionError): self.call('download', local_path='new')
            finally: context.current.reset(token)
        self.turn.cancelled.set()
        with self.assertRaises((InterruptedError, PermissionError)): self.call('download', local_path='new')

    def test_old_editor_actions_are_not_transfer_actions(self):
        for action in ('list','read','write','edit','glob','grep'):
            with self.assertRaises(ValueError): self.call(action, local_path='new')
            with self.assertRaises(ValueError): worker.operate({'action':action,'path':str(self.file)})

    def test_unknown_instance_never_connects(self):
        with patch.object(remote, 'exchange') as exchange:
            with self.assertRaises(ValueError):
                remote.transfer(dict(instance='wrong',action='download',path=str(self.file),local_path='new'))
            exchange.assert_not_called()

    def test_raw_worker_process_uses_quoted_path(self):
        def transport(entry, command):
            import shlex
            argv = shlex.split(command); self.assertEqual(argv[0], 'python3')
            return [sys.executable, *argv[1:]], {'PATH':'/usr/bin:/bin'}
        with patch.object(remote.ssh, 'transport', transport): result = self.call('download', local_path='new')
        self.assertEqual((self.root / 'new').read_bytes(), self.file.read_bytes())
        self.assertEqual(result['local_path'], '/workspace/new')

    def test_transport_cancellation_has_no_replay(self):
        def transport(entry, command):
            return [sys.executable, '-c', 'import time; time.sleep(10)'], {'PATH':'/usr/bin:/bin'}
        timer = threading.Timer(.2,self.turn.cancelled.set); timer.start()
        try:
            with patch.object(remote.ssh, 'transport', side_effect=transport) as run:
                with self.assertRaises(InterruptedError): remote.exchange({}, {'action':'pi_read'}, self.turn)
                self.assertEqual(run.call_count,1)
        finally: timer.join()


if __name__ == '__main__': unittest.main()
