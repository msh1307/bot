"""Real worker/process, paging, mutation and private-transport regression checks."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from evo_omp import context, remote_file_worker as worker
from evo_omp.integrations import remote_files as remote


class RemoteFileChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.file = self.root / 'code.py'
        self.file.write_text('def add(a, b):\n    return a - b\n', encoding='utf-8')
        self.turn = context.Turn('123')
        self.token = context.current.set(self.turn)
        self.pin = patch.object(remote, 'WORKSPACE', self.root); self.pin.start()
        self.cfg = patch.object(remote.ssh, 'configuration', return_value={'servers': {'fixture': {}}}); self.cfg.start()
        remote.OBSERVED.clear()

    def tearDown(self):
        context.current.reset(self.token)
        self.pin.stop(); self.cfg.stop(); self.tmp.cleanup()

    def call(self, action, path=None, **args):
        with patch.object(remote, 'exchange', side_effect=lambda entry, req, turn: worker.operate(req)):
            return remote.remote_files(dict(instance='fixture', action=action, path=str(path or self.file), **args))

    def test_read_edit_execute_roundtrip(self):
        result = self.call('read')
        self.assertIn('2:     return a - b', result['content'])
        self.assertNotIn('_version', result)
        self.call('edit', old_string='return a - b', new_string='return a + b')
        result = subprocess.run([sys.executable, '-c', self.file.read_text() + '\nassert add(2,3)==5; print("PASS")'],
                                check=True, capture_output=True, text=True)
        self.assertEqual(result.stdout.strip(), 'PASS')

    def test_stale_edit_does_not_overwrite(self):
        self.call('read'); self.file.write_text('external change')
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.call('edit', old_string='external change', new_string='wrong')
        self.assertEqual(self.file.read_text(), 'external change')

    def test_read_before_existing_write_and_upload(self):
        src = self.root / 'source'; src.write_bytes(b'upload')
        for action, args in [('write', {'content': 'wrong'}), ('edit', {'old_string': 'return', 'new_string': 'wrong'}),
                             ('upload', {'local_path': 'source'})]:
            with self.assertRaisesRegex(ValueError, 'read|Read'):
                self.call(action, **args)
        self.assertIn('return a - b', self.file.read_text())

    def test_unique_literal_and_empty_replacement(self):
        self.file.write_text('same\nsame\n'); self.call('read')
        with self.assertRaisesRegex(ValueError, 'exactly once'):
            self.call('edit', old_string='same', new_string='new')
        self.call('edit', old_string='same\nsame\n', new_string='')
        self.assertEqual(self.file.read_text(), '')

    def test_create_then_version_tracking(self):
        target = self.root / 'new dir' / 'file.txt'
        self.call('write', target, content='first')
        self.call('edit', target, old_string='first', new_string='second')
        self.assertEqual(target.read_text(), 'second')
        self.assertFalse(list(target.parent.glob('.evo-file-*')))

    def test_binary_transfer_has_no_model_payload(self):
        raw = bytes(range(256)) * 100
        (self.root / 'binary').write_bytes(raw)
        dest = self.root / 'remote'
        result = self.call('upload', dest, local_path='/workspace/binary')
        self.assertEqual(dest.read_bytes(), raw)
        result = self.call('download', dest, local_path='/workspace/back')
        self.assertEqual((self.root / 'back').read_bytes(), raw)
        self.assertEqual(result, {'bytes': len(raw), 'local_path': '/workspace/back'})
        self.assertNotIn(base64.b64encode(raw).decode(), str(result))

    def test_local_paths_cannot_escape_workspace(self):
        (self.root / 'link').symlink_to('/tmp')
        for path in ['/etc/passwd', '../outside', '/workspace/../outside', '/workspace/link/outside']:
            with self.assertRaises(ValueError): remote.local_path(path)

    def test_download_never_overwrites_local(self):
        (self.root / 'existing').write_text('keep')
        with patch.object(remote, 'exchange') as exchange:
            with self.assertRaisesRegex(ValueError, 'exists'):
                remote.remote_files(dict(instance='fixture', action='download', path=str(self.file), local_path='existing'))
            exchange.assert_not_called()
        self.assertEqual((self.root / 'existing').read_text(), 'keep')

    def test_listing_and_read_pagination(self):
        self.file.write_text(''.join(f'line {i}\n' for i in range(300)))
        page = self.call('read', start_line=51, limit=3)
        self.assertEqual(page['next_line'], 54)
        self.assertTrue(page['content'].startswith('51: line 50'))
        for i in range(5): (self.root / ('item'+str(i))).touch()
        first = self.call('list', self.root, limit=2)
        second = self.call('list', self.root, limit=2, offset=first['next_offset'])
        self.assertFalse(set(first['entries']) & set(second['entries']))

    def test_grep_and_glob_with_literal_shell_characters(self):
        target = self.root / "a ';$(echo WRONG).py"
        target.write_text('needle\nneedle\n')
        found = self.call('grep', self.root, pattern='need.e', glob='*.py', limit=1)
        self.assertEqual(found['next_offset'], 1)
        self.assertIn(target.name + ':1: needle', found['matches'][0])
        more = self.call('grep', self.root, pattern='needle', offset=1)
        self.assertEqual(len(more['matches']), 1)
        self.assertIn(target.name, self.call('glob', self.root, pattern='**/*.py')['matches'])

    def test_binary_special_and_large_files(self):
        self.file.write_bytes(b'\0binary')
        with self.assertRaises(ValueError): self.call('read')
        fifo = self.root / 'fifo'; os.mkfifo(fifo)
        with self.assertRaises(ValueError): worker.read_bytes(fifo)
        with self.file.open('wb') as out: out.truncate(worker.MAX_FILE+1)
        with self.assertRaises(ValueError): self.call('upload', self.root / 'other', local_path='code.py')

    def test_wrong_owner_schedule_and_cancel(self):
        for change in [{'user':'other'}, {'platform':'web'}, {'cron':True}]:
            token = context.current.set(context.Turn('123', **change))
            try:
                with self.assertRaises(PermissionError): self.call('read')
            finally: context.current.reset(token)
        self.turn.cancelled.set()
        with self.assertRaises((InterruptedError, PermissionError)): self.call('read')

    def test_schema_unknown_and_limits(self):
        for args in [dict(instance='fixture',action='remove',path='/tmp/x'),
                     dict(instance='fixture',action='read',path='/tmp/x',limit=True),
                     dict(instance='fixture',action='read',path='/tmp/x',root=True),
                     dict(instance='fixture',action='edit',path='/tmp/x',old_string='',new_string='')]:
            with self.assertRaises(ValueError): remote.validate(args)

    def test_unknown_instance_never_connects(self):
        with patch.object(remote, 'exchange') as exchange:
            with self.assertRaises(ValueError):
                remote.remote_files(dict(instance='wrong',action='read',path=str(self.file)))
            exchange.assert_not_called()

    def test_actual_worker_process_and_quoted_path(self):
        def transport(entry, command):
            import shlex
            argv = shlex.split(command)
            self.assertEqual(argv[0], 'python3')
            return [sys.executable, *argv[1:]], {'PATH':'/usr/bin:/bin'}
        with patch.object(remote.ssh, 'transport', transport):
            result = remote.remote_files(dict(instance='fixture',action='read',path=str(self.file)))
        self.assertIn('return a - b',result['content'])

    def test_transport_cancellation_has_no_replay(self):
        def transport(entry, command):
            return [sys.executable, '-c', 'import time; time.sleep(10)'], {'PATH':'/usr/bin:/bin'}
        timer = threading.Timer(.2,self.turn.cancelled.set); timer.start()
        try:
            with patch.object(remote.ssh, 'transport', side_effect=transport) as run:
                with self.assertRaises(InterruptedError): remote.exchange({}, {'action':'read'}, self.turn)
                self.assertEqual(run.call_count,1)
        finally: timer.join()


if __name__ == '__main__': unittest.main()
