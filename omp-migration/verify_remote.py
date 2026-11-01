"""Unified remote routing, privacy and permissions; mocked transports, no connections."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from evo_omp import context, tool_output
from evo_omp.integrations import remote, codex, instances, rdp, ssh, credential_input
from evo_omp.tools import Registry


CATALOG = {'default': 'pc', 'defaults': {'ssh': 'linux', 'codex': 'pc', 'rdp': 'pc'},
    'instances': [{'id': 'linux', 'name': 'linux', 'host': '192.0.2.1',
                   'capabilities': ['remote_ssh'], 'sudo': 'owner_approval_once'},
                  {'id': 'pc', 'name': 'pc', 'host': '192.0.2.2',
                   'capabilities': ['codex_task', 'windows_files', 'windows_exec', 'remote_desktop']},
                  {'id': 'off', 'name': 'off', 'host': '192.0.2.3',
                   'capabilities': [], 'disabled': ['remote_desktop']}]}
RETIRED = {'remote_instance', 'remote_ssh', 'remote_desktop', 'codex_task', 'windows_files', 'windows_exec'}


class Checks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'remote-control.json'
        self.pin = patch.object(remote, 'CONFIG', self.path); self.pin.start()
        self.catalog = patch.object(instances, 'catalog', return_value=CATALOG); self.catalog.start()
        self.turn = context.Turn('123')
        self.token = context.current.set(self.turn)

    def tearDown(self):
        context.current.reset(self.token)
        self.pin.stop(); self.catalog.stop(); self.tmp.cleanup()

    def call(self, protocol, action, **params):
        return remote.remote_control({'protocol': protocol, 'action': action, 'args': params})

    def test_one_short_advertised_entry_and_browser_credentials_only(self):
        registry = Registry()
        definitions = {t['name']: t for t in registry.definitions()}
        self.assertFalse(set(definitions) & RETIRED)
        self.assertFalse(RETIRED & registry.tools.keys())
        self.assertEqual(set(definitions['remote_control']['parameters']['properties']),
                         {'instance', 'protocol', 'action', 'args'})
        self.assertEqual(definitions['remote_control']['parameters']['properties']['protocol']['enum'], ['codex', 'rdp'])
        self.assertFalse({'instance', 'frame'} & definitions['credential_input']['parameters']['properties'].keys())
        scheduled = {t['name']: t for t in registry.definitions(cron=True)}
        self.assertEqual(scheduled['remote_control']['parameters']['properties']['protocol']['enum'], ['codex'])
        self.assertFalse(any(name in json.dumps(definitions['remote_control']) for name in RETIRED))

    def test_help_uses_exact_actions_and_parameter_names(self):
        self.assertEqual(set(remote.GUIDES), {'rdp', 'codex'})
        self.assertEqual(set(remote.GUIDES['rdp']) - {'notes'}, remote.RDP_ACTIONS)
        self.assertEqual(set(remote.GUIDES['codex']) - {'notes'}, remote.CODEX_ACTIONS)
        for protocol, guide in remote.GUIDES.items():
            for action, spec in guide.items():
                if action == 'notes': continue
                self.assertTrue(action.isidentifier(), (protocol, action))
                for fields in ('required', 'optional'):
                    self.assertIsInstance(spec.get(fields, {}), dict)
                    self.assertTrue(all(key.isidentifier() for key in spec.get(fields, {})))
        self.assertEqual(set(remote.GUIDES['codex']['read']['optional']), {'cursor'})
        self.assertNotIn('optional', remote.GUIDES['codex']['stop'])

    def test_discovery_and_defaults_do_not_connect_or_show_secret_paths(self):
        with patch.object(ssh, 'remote_ssh') as shell, patch.object(rdp, 'remote_desktop') as desktop, \
                patch.object(codex, 'codex_task') as chat:
            result = remote.remote_control({'action': 'list'})
            shell.assert_not_called(); desktop.assert_not_called(); chat.assert_not_called()
        self.assertEqual(result['defaults'], {'ssh': 'linux', 'codex': 'pc', 'rdp': 'pc'})
        rows = {row['id']: row for row in result['instances']}
        self.assertEqual(rows['linux']['protocols'], ['ssh'])
        self.assertEqual(rows['linux']['shell'], 'bash')
        self.assertEqual(rows['pc']['shell'], 'powershell')
        self.assertEqual(rows['localhost']['protocols'], [])
        self.assertNotIn('password', json.dumps(result))
        self.assertEqual(rows['off']['disabled'], ['rdp'])

    def test_private_override_and_unavailable_default_fail_closed(self):
        self.path.write_text(json.dumps({'defaults': {'codex': 'pc'}, 'instances': {
            'pc': {'host': '192.0.2.2', 'codex': {}}}})); self.path.chmod(0o600)
        self.assertEqual(remote.catalog()['defaults']['codex'], 'pc')
        for value in ({'defaults': {'ssh': 'pc'}}, {'defaults': {'ssh': 'missing'}},
                      {'defaults': {'ssh': None}}, {'defaults': {'http': 'pc'}}, {'extra': 1}):
            self.path.write_text(json.dumps(value))
            with self.assertRaises(ValueError): remote.catalog()
        self.path.write_text('{"defaults": {}}'); self.path.chmod(0o644)
        with self.assertRaises(ValueError): remote.catalog()

    def test_retired_ssh_route_redirects_without_dispatch(self):
        with patch.object(ssh, 'remote_ssh') as fn:
            for action in ('help', 'exec'):
                with self.assertRaisesRegex(ValueError, 'bash'):
                    self.call('ssh', action, command='id -u')
            fn.assert_not_called()

    def test_unknown_mismatch_disabled_or_smuggled_route_never_dispatch(self):
        bad = [{'instance': i, 'protocol': p, 'action': a} for i, p, a in
               [('missing', 'codex', 'list'), ('linux', 'rdp', 'connect'), ('off', 'rdp', 'connect')]]
        bad += [{'protocol': 'ssh', 'action': 'exec', 'args': {k: 'pc'}}
                for k in ('instance', 'server', 'protocol', 'action')]
        bad += [{'action': 'list', 'server': 'pc'}, {'protocol': 'shell', 'action': 'exec'},
                {'protocol': 'ssh', 'action': 'connect'}, {'action': 'send'},
                {'protocol': 'codex', 'action': 'send', 'args': {'password': 'DO_NOT_SEND'}},
                {'action': 'list', 'args': []}]
        with patch.object(ssh, 'remote_ssh') as s, patch.object(rdp, 'remote_desktop') as r, \
                patch.object(codex, 'codex_task') as c:
            for args in bad:
                with self.subTest(args=args), self.assertRaises(ValueError): remote.remote_control(args)
            s.assert_not_called(); r.assert_not_called(); c.assert_not_called()

    def test_codex_chat_file_and_exec_dispatch(self):
        with patch.object(codex, 'codex_task', return_value='CHAT') as chat, \
                patch.object(codex, 'windows_files', return_value='FILES') as files, \
                patch.object(codex, 'windows_exec', return_value='EXEC') as command:
            self.assertEqual(self.call('codex', 'send', thread_id='chat1', prompt='Continue', wake=False), 'CHAT')
            chat.assert_called_once_with({'instance': 'pc', 'action': 'send', 'thread_id': 'chat1', 'prompt': 'Continue', 'wake': False})
            self.assertEqual(self.call('codex', 'file_read', path='C:\\test.txt', start_line=81), 'FILES')
            files.assert_called_once_with({'instance': 'pc', 'action': 'read', 'path': 'C:\\test.txt', 'start_line': 81})
            self.assertEqual(self.call('codex', 'exec', command=['whoami']), 'EXEC')
            command.assert_called_once_with({'instance': 'pc', 'command': ['whoami']})

    def test_rdp_frames_and_private_entry_use_existing_handlers(self):
        with patch.object(rdp, 'remote_desktop', return_value={'image': 'synthetic'}) as desktop, \
                patch.object(credential_input, 'credential_input', return_value={'entered': True}) as private:
            self.call('rdp', 'click', frame='frame1', x=500, y=500)
            desktop.assert_called_once_with({'instance': 'pc', 'action': 'click', 'frame': 'frame1', 'x': 500, 'y': 500})
            self.call('rdp', 'credential', frame='frame2', account='test@example.invalid')
            private.assert_called_once_with({'instance': 'pc', 'action': 'enter', 'frame': 'frame2', 'account': 'test@example.invalid'})
            private.reset_mock(); self.call('rdp', 'accounts')
            private.assert_called_once_with({'instance': 'pc', 'action': 'list'})

    def test_owner_cancelled_and_web_scope(self):
        for turn in (None, context.Turn('123', user='other'), context.Turn('123', platform='web'), self.turn):
            self.turn.cancelled.set()
            token = context.current.set(turn)
            try:
                with self.assertRaises(PermissionError): remote.remote_control({'action': 'list'})
            finally: context.current.reset(token)

    def test_cron_enforces_permissions_at_dispatch_and_help(self):
        token = context.current.set(context.Turn('123', cron=True))
        try:
            with patch.object(codex, 'codex_task', return_value='OK') as chat, \
                    patch.object(ssh, 'remote_ssh') as shell, patch.object(rdp, 'remote_desktop') as desktop:
                for action in ('list', 'read', 'send', 'answer', 'status'):
                    self.assertEqual(self.call('codex', action, thread_id='chat1'), 'OK')
                for proto, action in (('codex', 'start'), ('codex', 'stop'), ('codex', 'exec'),
                                      ('ssh', 'exec'), ('rdp', 'connect'), ('rdp', 'credential')):
                    with self.assertRaises((PermissionError, ValueError)): self.call(proto, action)
                self.assertEqual(chat.call_count, 5); shell.assert_not_called(); desktop.assert_not_called()
            help = remote.remote_control({'action': 'help', 'protocol': 'codex'})
            self.assertTrue({'read', 'status', 'send', 'answer'} <= help.keys())
            self.assertFalse({'start', 'stop', 'exec'} & help.keys())
            with self.assertRaises(PermissionError): remote.remote_control({'action': 'help', 'protocol': 'rdp'})
        finally: context.current.reset(token)

    def test_registry_filters_screens_from_unified_tool(self):
        async def check():
            with patch.object(rdp, 'remote_desktop', return_value={'raw': 'synthetic image'}), \
                    patch('evo_omp.tools.filter_screen', return_value={'filtered': True}) as filter:
                result = await Registry().call('remote_control', {'protocol': 'rdp', 'action': 'screenshot'}, self.turn)
                self.assertEqual(result, {'filtered': True})
                filter.assert_called_once_with({'raw': 'synthetic image'})
        asyncio.run(check())

    def test_long_results_keep_exact_text_in_ram(self):
        self.assertIn('remote_control', tool_output.TOOLS)
        profile = Path(self.tmp.name) / 'profile'; (profile / 'sessions').mkdir(parents=True)
        text = 'SYNTHETIC_SOURCE_LINE\n' * 2000
        result = tool_output.compact_result('remote_control', {'content': [
            {'type': 'text', 'text': json.dumps({'stdout': text, 'exit_code': 0})}]}, profile)
        self.assertLess(len(json.dumps(result)), len(text))
        stored = list((profile / 'sessions/host-output').glob('*.txt'))
        self.assertTrue(stored)
        self.assertTrue(any(text == p.read_text() for p in stored))


if __name__ == '__main__': unittest.main()
