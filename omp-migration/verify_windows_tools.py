"""Windows tool routing, pagination, scope and exact large-output recovery; no network."""
import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from evo_omp import context, tool_output
from evo_omp.integrations import codex, rdp, instances, remote
from evo_omp.tools import Registry


class Checks(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bridge = codex.Bridge.__new__(codex.Bridge)
        self.bridge.server_id = 'test'
        self.calls = []

        async def rpc(method, params, check=None):
            if check:
                check()
            self.calls.append((method, params))
            if method == 'fs/readDirectory':
                return {'entries': [{'fileName': f'{i:02d}', 'isDirectory': i == 0} for i in range(25)]}
            assert method == 'command/exec'
            return {'exitCode': 7, 'stdout': 'out', 'stderr': 'err'}

        async def desktop(args, check=None, endpoint=None):
            if check:
                check()
            self.calls.append((endpoint, args))
            return {'path': args['path'], 'text': 'source\n', 'start_line': args['start_line'],
                    'next_line': args['start_line'] + args['limit']}

        self.bridge.rpc, self.bridge.desktop = rpc, desktop
        self.previous = codex.BRIDGE, codex.CLIENT, codex.LOOP, codex.SERVERS, context.client
        codex.BRIDGE, codex.SERVERS = self.bridge, {'test': self.bridge}
        codex.CLIENT = context.client = SimpleNamespace(is_ready=lambda: True)
        codex.LOOP = asyncio.get_running_loop()

    async def asyncTearDown(self):
        codex.BRIDGE, codex.CLIENT, codex.LOOP, codex.SERVERS, context.client = self.previous

    async def call(self, fn, args, turn=None):
        token = context.current.set(turn or context.Turn('77'))
        try:
            return json.loads(await asyncio.to_thread(fn, args))
        finally:
            context.current.reset(token)

    async def test_pages(self):
        first = await self.call(codex.windows_files, {'action': 'list', 'path': 'C:\\fixture', 'instance': 'test'})
        self.assertEqual(first, {'entries': ['00/'] + [f'{i:02d}' for i in range(1, 20)], 'next_offset': 20})
        last = await self.call(codex.windows_files, {'action': 'list', 'path': 'C:\\fixture', 'offset': 20})
        self.assertEqual(last, {'entries': [f'{i:02d}' for i in range(20, 25)]})
        filtered = await self.call(codex.windows_files, {'action': 'list', 'path': 'C:\\fixture', 'search': '24'})
        self.assertEqual(filtered, {'entries': ['24']})
        page = await self.call(codex.windows_files, {'action': 'read', 'path': 'C:\\fixture.txt', 'start_line': 81})
        self.assertEqual(page, {'text': 'source\n', 'start_line': 81, 'next_line': 161})
        self.assertEqual(self.calls[-1], ('/files', {'path': 'C:\\fixture.txt', 'start_line': 81, 'limit': 80}))

    async def test_command(self):
        args = {'command': ['cmd.exe', '/c', 'echo', 'out'], 'cwd': 'C:\\fixture', 'timeout_ms': 1200}
        result = await self.call(codex.windows_exec, args)
        self.assertEqual(result, {'exit_code': 7, 'stdout': 'out', 'stderr': 'err'})
        self.assertEqual(self.calls, [('command/exec', {'command': args['command'], 'cwd': args['cwd'], 'timeoutMs': 1200})])

    async def test_chat_list_seconds_and_unchanged_db_paging(self):
        self.bridge.jobs = {}
        pages = [
            {'data': [{'id': 'db-new', 'updatedAt': 1700000010},
                      {'id': 'gui', 'updatedAt': 1700000005}], 'nextCursor': 'opaque-db-cursor'},
            {'data': [{'id': 'db-old', 'updatedAt': 1700000000}], 'nextCursor': None}]
        async def rpc(method, params, **kwargs):
            self.calls.append((method, params))
            self.assertEqual(method, 'thread/list')
            return pages[len(self.calls) - 1]
        async def desktop(args, **kwargs):
            return {'threads': [{'thread_id': 'gui', 'gui_attached': True,
                                 'updated_at': 1700000020123}]}
        self.bridge.rpc, self.bridge.desktop = rpc, desktop
        first = await self.bridge.action({'action': 'list', 'limit': 2, 'search': 'fixture'}, '77')
        self.assertEqual([t['thread_id'] for t in first['threads']], ['db-new', 'gui'])
        self.assertEqual([t['updated_at'] for t in first['threads']], [1700000010, 1700000005])
        self.assertEqual(first['next_cursor'], 'opaque-db-cursor')
        second = await self.bridge.action({'action': 'list', 'limit': 2, 'search': 'fixture',
                                          'cursor': first['next_cursor']}, '77')
        self.assertEqual(second['threads'][0]['updated_at'], 1700000000)
        self.assertIsNone(second['next_cursor'])
        params = {'limit': 2, 'sortKey': 'updated_at', 'useStateDbOnly': True, 'searchTerm': 'fixture'}
        self.assertEqual(self.calls, [('thread/list', params),
                                     ('thread/list', dict(params, cursor='opaque-db-cursor'))])
        self.assertEqual(pages[0]['data'][0]['updatedAt'], 1700000010)

    async def test_chat_list_gui_failure_still_returns_db_seconds(self):
        self.bridge.jobs = {}
        async def rpc(*args, **kwargs):
            return {'data': [{'id': 'db', 'updatedAt': 1700000000}], 'nextCursor': 'same-cursor'}
        async def desktop(*args, **kwargs):
            raise RuntimeError('synthetic GUI unavailable')
        self.bridge.rpc, self.bridge.desktop = rpc, desktop
        reply = await self.bridge.action({'action': 'list'}, '77')
        self.assertEqual(reply['threads'][0]['updated_at'], 1700000000)
        self.assertEqual(reply['next_cursor'], 'same-cursor')
        self.assertIn('desktop_error', reply)

    async def test_stored_timestamp_unknown_stays_unknown(self):
        self.assertEqual(codex.epoch_seconds(0), 0)
        self.assertEqual(codex.epoch_seconds(1700000000.499), 1700000000)
        self.assertEqual(codex.epoch_seconds(1700000000.5), 1700000001)
        self.assertEqual(codex.epoch_seconds(1700000000499, milliseconds=True), 1700000000)
        self.assertEqual(codex.epoch_seconds(1700000000500, milliseconds=True), 1700000001)
        for value in (None, True, '1700000000', float('nan'), float('inf')):
            self.assertIsNone(codex.epoch_seconds(value))

    async def test_actual_desktop_boundary_rounds_status_and_inspect_only(self):
        import io
        payload = {'success': True, 'updated_at': 1700000000500,
                   'turn_started_at': 1700000001499, 'elapsed_seconds': 27,
                   'threads': [{'updated_at': 1700000001499, 'turn_started_at': None}]}
        self.bridge.settings = {'url': 'wss://fixture.invalid:1', 'ca': 'unused', 'token': 'fixture'}
        def response(*args, **kwargs):
            return io.BytesIO(json.dumps(payload).encode())
        with patch.object(codex.ssl, 'create_default_context', return_value=SimpleNamespace()), \
                patch.object(codex, 'urlopen', side_effect=response):
            reply = await codex.Bridge.desktop(self.bridge, {'action': 'status', 'thread_id': 'fixture'})
            self.assertEqual(reply['updated_at'], 1700000001)
            self.assertEqual(reply['turn_started_at'], 1700000001)
            self.assertEqual(reply['elapsed_seconds'], 27)
            self.assertEqual(reply['threads'][0], {'updated_at': 1700000001, 'turn_started_at': None})
            files = await codex.Bridge.desktop(self.bridge, {'path': 'C:\\fixture'}, endpoint='/files')
            self.assertEqual(files['updated_at'], 1700000000500)
        self.assertEqual(payload['updated_at'], 1700000000500)

    async def test_scope_and_invalid_inputs(self):
        for turn in (context.Turn('77', platform='web'), context.Turn('77', user='other'), context.Turn('77', cron=True)):
            result = await self.call(codex.windows_exec, {'command': ['whoami']}, turn)
            self.assertIn('error', result)
        turn = context.Turn('77'); turn.cancelled.set()
        self.assertIn('error', await self.call(codex.windows_exec, {'command': ['whoami']}, turn))
        for args in ({'action': 'read', 'path': '/etc/passwd'},
                     {'action': 'read', 'path': 'C:\\x', 'command': ['whoami']},
                     {'action': 'exec', 'path': 'C:\\x'},
                     {'action': 'read', 'path': 'C:\\x', 'limit': True}):
            self.assertIn('error', await self.call(codex.windows_files, args))
        for args in ({'command': 'whoami'}, {'command': ['whoami'], 'timeout_ms': 15001},
                     {'command': ['whoami'], 'thread_id': 'wrong-tool'}):
            self.assertIn('error', await self.call(codex.windows_exec, args))
        self.assertFalse(self.calls)
        result = await self.call(codex.windows_files, {'action': 'list', 'path': 'C:\\fixture'}, context.Turn('77', cron=True))
        self.assertEqual(len(result['entries']), 20)

    async def test_internal_transport_preserves_file_metadata(self):
        result = await self.call(codex.codex_task, {'action': 'file_list', 'path': 'C:\\fixture'})
        self.assertTrue(result['success'])
        self.assertEqual(len(result['entries']), 25)
        self.assertEqual(result['entries'][0]['fileName'], '00')
        self.assertEqual(result['source'], 'windows_filesystem')

    async def test_catalog_has_one_remote_entry(self):
        registry = Registry()
        with patch.object(rdp, 'configuration', return_value={'enabled': False}):
            definitions = {t['name']: t for t in registry.definitions()}
        self.assertNotIn('remote_desktop', definitions)
        self.assertEqual(set(definitions['remote_control']['parameters']['properties']),
                         {'instance', 'protocol', 'action', 'args'})
        self.assertFalse(set(definitions) & {'codex_task', 'windows_files', 'windows_exec', 'remote_instance', 'remote_ssh'})
        scheduled = {t['name'] for t in registry.definitions(cron=True)}
        self.assertIn('remote_control', scheduled)
        self.assertNotIn('windows_exec', scheduled)
        with patch.object(rdp, 'configuration', return_value={'enabled': True}):
            self.assertNotIn('remote_desktop', {t['name'] for t in registry.definitions()})

    async def test_instance_selection_is_per_call_and_rejects_conflicts(self):
        async def second_action(args, channel, check=None):
            check()
            return {'exit_code': 0, 'stdout': 'second'}
        codex.SERVERS['second'] = SimpleNamespace(action=second_action, server_id='second')
        first, second = await asyncio.gather(
            self.call(codex.windows_exec, {'instance': 'test', 'command': ['whoami']}),
            self.call(codex.windows_exec, {'instance': 'second', 'command': ['whoami']}))
        self.assertEqual(first['stdout'], 'out')
        self.assertEqual(second, {'exit_code': 0, 'stdout': 'second'})
        self.assertIs(codex.BRIDGE, self.bridge)
        count = len(self.calls)
        for extra in ({'instance': 'missing'}, {'instance': 1}, {'instance': 'test', 'server': 'second'}):
            self.assertIn('error', await self.call(codex.windows_exec, {'command': ['whoami'], **extra}))
        self.assertEqual(len(self.calls), count)

    async def test_discovery_lists_capabilities_without_secrets_or_connections(self):
        settings = {'name': 'Desktop', 'url': 'wss://192.0.2.10:4500', 'token': 'PRIVATE_TOKEN', 'ca': '/private/cert'}
        desktop = {'default': 'test', 'enabled': False, 'servers': {
            'test': {'host': '192.0.2.10', 'password_file': '/private/password'},
            'screen-only': {'host': '192.0.2.11'}}}
        with patch.object(remote, 'CONFIG', Path('/nonexistent-evo-fixture')), \
                patch.object(codex, 'server_config', return_value=('test', {'test': settings})), \
                patch.object(rdp, 'configuration', return_value=desktop), \
                patch.object(rdp.subprocess, 'Popen', side_effect=AssertionError('Discovery cannot connect')):
            discover = lambda args: json.dumps(remote.remote_control(args))
            result = await self.call(discover, {'action': 'list'})
            self.assertEqual(result['defaults']['codex'], 'test')
            rows = {row['id']: row for row in result['instances']}
            self.assertEqual(rows['test']['protocols'], ['codex'])
            self.assertEqual(rows['test']['disabled'], ['rdp'])
            self.assertFalse(rows['screen-only']['protocols'])
            self.assertNotIn('PRIVATE_TOKEN', json.dumps(result))
            self.assertNotIn('/private', json.dumps(result))
            desktop['enabled'] = True
            scheduled = await self.call(discover, {'action': 'list'}, context.Turn('77', cron=True))
            self.assertIn('rdp', scheduled['instances'][0]['protocols'])
            with self.assertRaises(PermissionError):
                await self.call(discover, {'action': 'list'}, context.Turn('77', platform='web'))
        self.assertFalse(self.calls)

    async def test_large_outputs_keep_complete_text_in_ram_store(self):
        with tempfile.TemporaryDirectory() as folder:
            profile = Path(folder); (profile / 'sessions').mkdir()
            text = 'full output 한글\n' * 6000
            for name in ('remote_control',):
                result = tool_output.compact_result(name, {'content': [{'type': 'text', 'text': text}]}, profile)
                self.assertLessEqual(len(result['content'][0]['text'].encode()), tool_output.INLINE_BYTES)
                self.assertTrue(any(p.read_text() == text for p in (profile / 'sessions/host-output').iterdir()))


if __name__ == '__main__':
    unittest.main(verbosity=2)
