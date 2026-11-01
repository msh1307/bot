"""Pi I/O adapter regressions; unit mode is offline, --live uses disposable files."""
import asyncio
import base64
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from evo_omp import context, remote_file_worker as worker, rpc
from evo_omp.integrations import coding, codex, host, remote, ssh
from evo_omp.tools import Registry


class CodingChecks(unittest.TestCase):
    def setUp(self):
        self.turn = context.Turn('77')
        self.token = context.current.set(self.turn)
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name)/'fixture.py')
    def tearDown(self):
        context.current.reset(self.token); self.temp.cleanup()
    def file(self, action, **kw):
        with patch.object(ssh, 'configuration', return_value={'servers': {'fixture': {}}}), \
             patch.object(coding.remote_files, 'exchange', side_effect=lambda _, req, __: worker.operate(req)):
            return coding.file_io(dict(instance='fixture', action=action, path=self.path, **kw))
    def test_raw_round_trip_and_stale_edit(self):
        self.file('write', content='a\r\nb\r\n')
        data = base64.b64decode(self.file('read')['data'])
        self.assertEqual(data, b'a\r\nb\r\n')
        self.file('write', content='changed')
        with self.assertRaisesRegex(ValueError, 'changed|read'):
            self.file('write', content='wrong', expected=worker.fingerprint(data))
        self.assertEqual(Path(self.path).read_text(), 'changed')
    def test_write_overwrites_like_native_pi(self):
        self.file('write', content='one'); self.file('write', content='two')
        self.assertEqual(Path(self.path).read_text(), 'two')
    def test_unknown_target_never_falls_back(self):
        with patch.object(ssh, 'configuration', return_value={'servers': {}}), self.assertRaisesRegex(ValueError, 'Unknown instance'):
            coding.file_io(dict(instance='wrong', action='write', path=self.path, content='bad'))
        self.assertFalse(Path(self.path).exists())
    def test_non_owner_and_scheduled_calls_blocked(self):
        for turn in (context.Turn('77', user='other'), context.Turn('77', cron=True)):
            token = context.current.set(turn)
            try:
                with self.assertRaises(PermissionError): self.file('read')
            finally: context.current.reset(token)
    def test_root_missing_reason_rejected_before_dispatch(self):
        with patch.object(ssh, 'remote_ssh') as run:
            with self.assertRaisesRegex(ValueError, 'reason'):
                coding.execute({'instance': 'fixture', 'command': 'id', 'root': True})
            run.assert_not_called()
    def test_non_root_reason_omitted(self):
        with patch.object(ssh, 'remote_ssh', return_value='{"exit_code":0,"output":"ok"}') as run:
            coding.execute({'instance': 'fixture', 'command': 'id'})
            self.assertNotIn('reason', run.call_args.args[0])
    def test_root_uses_existing_gate_exact_command_and_reason(self):
        request = {'instance': 'fixture', 'command': 'id', 'root': True, 'reason': 'Owner test'}
        refusal = {'success': False, 'approval_status': 'denied_or_expired', 'error': 'Not approved'}
        with patch.object(ssh, 'remote_ssh', return_value=json.dumps(refusal)) as run:
            with self.assertRaisesRegex(RuntimeError, 'Not approved'): coding.execute(request)
            self.assertEqual(run.call_args.args[0], {'instance': 'fixture', 'command': 'id',
                'run_as': 'root', 'reason': 'Owner test', 'timeout': 60})
    def test_host_uses_existing_broker(self):
        with patch.object(host, 'host_command', return_value='{"success":true,"exit_code":0,"output":"ok"}') as run:
            coding.execute({'instance': 'localhost', 'command': 'id'})
            self.assertEqual(run.call_args.args[0], {'command': 'id', 'timeout': 60})
    def test_windows_default_and_no_elevation(self):
        with patch.dict(codex.SERVERS, {'fixture': object()}), \
             patch.object(codex, 'windows_exec', return_value='{"exit_code":0,"stdout":"ok"}') as run:
            coding.execute({'instance': 'fixture', 'command': 'Write-Output ok'})
            self.assertEqual(run.call_args.args[0]['timeout_ms'], 15000)
            self.assertEqual(run.call_args.args[0]['command'][0], 'powershell.exe')
            with self.assertRaisesRegex(ValueError, 'elevation'):
                coding.execute({'instance': 'fixture', 'command': 'id', 'root': True, 'reason': 'test'})
            self.assertEqual(run.call_count, 1)
    def test_nonzero_exit_is_preserved(self):
        self.assertEqual(coding.checked({'success': False, 'exit_code': 7})['exit_code'], 7)
    def test_legacy_tools_not_declared(self):
        names = {d['name'] for d in Registry().definitions()}
        self.assertFalse(names & {'remote_files', 'shell_exec', 'host_command', '_pi_io', '_pi_exec'})
        self.assertIn('file_transfer', names)


async def integration(root, report, live=False):
    from verify_context_payload import Handler, ThreadingHTTPServer, REQUESTS, STEPS
    rpc.ROOT = root; rpc.ENGINE = 'pi'
    report.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    cfg = Path(tempfile.mkdtemp(prefix='pi-coding-', dir=rpc.RUNTIME))
    (cfg/'models.yml').write_text((root/'models.yml').read_text().replace(':8080/v1', f':{server.server_port}/v1'))
    shutil.copyfile(root/'config.yml', cfg/'config.yml')
    codex.STATE = cfg/'codex-jobs.json'  # Test clients never touch the live job store.
    registry = Registry(); await registry.initialize_web()
    session = None; bridges = []
    results = {}
    host.CLIENT = context.client = type('Client', (), {'is_ready': lambda _: True})()
    host.LOOP = codex.LOOP = asyncio.get_running_loop(); codex.CLIENT = context.client
    try:
        if live:
            default, servers = codex.server_config()
            for ident, settings in servers.items():
                bridge = codex.Bridge(settings, ident); bridges.append(bridge); codex.SERVERS[ident] = bridge
            codex.BRIDGE = codex.SERVERS[default]
        session = rpc.OmpSession('pi-coding', 'sc117-flash-next-iq3_xxs', registry.definitions(), registry.call, cfg)
        await session.start()
        fixture = '/home/agent/sessions/coding-local.txt'
        STEPS.extend([('write', {'path': fixture, 'content': 'a = 41\n'}),
            ('edit', {'path': fixture, 'edits': [{'oldText': '41', 'newText': '42'}]}),
            ('read', {'path': fixture}),
            ('bash', {'command': 'export PI_PROBE=42'}),
            ('bash', {'command': 'test -z "$PI_PROBE" && echo ONE_SHOT_OK'}),
            ('read', {'instance': 'no-such-instance', 'path': '/tmp/not-local'}),
            ('bash', {'instance': 'n100', 'command': 'id', 'root': True})])
        await session.prompt('Synthetic local routing and rejection checks.', context.Turn('77'))
        text = str([m['content'] for m in REQUESTS[-1]['messages'] if m['role'] == 'tool'])
        assert 'a = 42' in text and 'ONE_SHOT_OK' in text and 'Unknown instance' in text and 'requires' in text, [
            m for m in REQUESTS[-1]['messages'] if m['role'] == 'tool']
        results['local_native_edit_read_and_one_shot'] = True
        results['unknown_target_and_root_without_reason_rejected'] = True
        if live:
            import secrets
            marker = 'pi-wrapper-' + secrets.token_hex(8)
            targets = [('n100', '/tmp/'+marker+'/code.py'),
                       ('desktop', 'C:/Users/msh1307/Documents/ChatGPT/EVO-X2/.pi-migration-work/'+marker+'/code.py'),
                       ('localhost', '/var/lib/evo-hermes/workspace/.'+marker+'/code.py')]
            if '--oracle' in sys.argv:
                targets.insert(1, ('oracle', '/tmp/'+marker+'/code.py'))
            # All tests create a unique directory; cleanup is restricted to that directory.
            for ident, path in targets:
                REQUESTS.clear(); STEPS.clear()
                STEPS.extend([('write', {'instance': ident, 'path': path, 'content': 'value = 41\r\n'}),
                    ('read', {'instance': ident, 'path': path}),
                    ('edit', {'instance': ident, 'path': path, 'edits': [{'oldText': '41', 'newText': '42'}]}),
                    ('read', {'instance': ident, 'path': path})])
                shell = ("[Console]::Write('PI_EXEC_OK'); Get-Content -LiteralPath '"+path+"'" if ident == 'desktop'
                         else 'printf PI_EXEC_OK; cat '+path)
                STEPS.append(('bash', {'instance': ident, 'command': shell}))
                await session.prompt('Synthetic real transport fixture check.', context.Turn('77'))
                text = str([m['content'] for m in REQUESTS[-1]['messages'] if m['role'] == 'tool'])
                assert 'value = 42' in text and 'PI_EXEC_OK' in text, text[-5000:]
                results[ident] = {'read_write_edit_exec': True}
                print('VERIFIED_TARGET', ident, flush=True)
                # A large file exercises host -> Pi chunking without putting bytes into model context.
                if ident == 'n100':
                    payload = 'x' * 1200000 + '\nCHUNK_OK\n'
                    turn = context.Turn('77'); token = context.current.set(turn)
                    try: await asyncio.to_thread(coding.file_io, {'instance': ident, 'action':'write', 'path':path, 'content':payload})
                    finally: context.current.reset(token)
                    REQUESTS.clear(); STEPS.append(('read', {'instance': ident, 'path':path, 'offset':2, 'limit':1}))
                    await session.prompt('Synthetic large file transfer check.', context.Turn('77'))
                    assert any('CHUNK_OK' in str(m['content']) for m in REQUESTS[-1]['messages'] if m['role'] == 'tool')
                    results['large_file_chunk_transport'] = len(payload)
                turn = context.Turn('77'); token = context.current.set(turn)
                try:
                    if ident == 'desktop':
                        directory = path.rsplit('/',1)[0]
                        cleanup = "Remove-Item -LiteralPath '"+directory+"' -Recurse -Force"
                    else:
                        import shlex
                        cleanup = 'rm -rf -- '+shlex.quote(path.rsplit('/',1)[0])
                    assert marker in cleanup and len(marker) > 16
                    await asyncio.to_thread(coding.execute, {'instance':ident, 'command':cleanup})
                finally: context.current.reset(token)
            REQUESTS.clear(); STEPS.append(('read', {'instance':'localhost','path':'/opt/evo-omp/instructions.txt'}))
            await session.prompt('Synthetic host protected-view check.', context.Turn('77'))
            text = str([m['content'] for m in REQUESTS[-1]['messages'] if m['role'] == 'tool'])
            assert 'Could not' in text or 'Permission' in text or 'error' in text.lower(), text[-2000:]
            results['host_guard_preserved'] = True
        results['passed'] = True
        (report/'coding-checks.json').write_text(json.dumps(results, indent=2)+'\n')
        print(json.dumps(results), flush=True)
    finally:
        if session: await session.close()
        for bridge in bridges:
            if bridge.ws: await bridge.ws.close()
            if bridge.reader: bridge.reader.cancel()
        await registry.close(); server.shutdown(); server.server_close(); shutil.rmtree(cfg)


if __name__ == '__main__':
    if len(sys.argv) == 1: unittest.main()
    else: asyncio.run(integration(Path(sys.argv[1]), Path(sys.argv[2]), '--live' in sys.argv))
