"""Terminal routing and transport checks without SSH connections or Discord messages."""
import io
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch, MagicMock

from evo_bot import context, rpc
from evo_bot.integrations import remote, terminal


class Checks(unittest.TestCase):
    def setUp(self):
        self.turn = context.Turn('123')
        self.token = context.current.set(self.turn)

    def tearDown(self):
        context.current.reset(self.token)

    def test_discovery_keeps_one_tool_and_no_connection(self):
        with patch.object(terminal, 'client') as client:
            result = remote.remote_control({'protocol': 'terminal', 'action': 'help'})
        self.assertIn('create', result)
        self.assertEqual(set(result) - {'notes'}, set(terminal.ACTIONS))
        client.assert_not_called()
        self.assertEqual(set(remote.definition()['parameters']['properties']),
                         {'instance', 'protocol', 'action', 'args'})

    def test_routes_same_actions_to_selected_target(self):
        with patch.object(terminal.ssh, 'configuration', return_value={'servers': {'oracle': {}, 'new-server': {}}}), \
                patch.object(terminal, 'client') as client:
            for instance in ('localhost', 'oracle', 'new-server'):
                args = {'sessionId': 'abc-1', 'text': 'hello\n'}
                remote.remote_control({'instance': instance, 'protocol': 'terminal',
                                       'action': 'type', 'args': args})
                client.assert_called_with(instance)
                client.return_value.call.assert_called_with('type', args, self.turn)

    def test_missing_unknown_target_and_wrong_actions_do_not_connect(self):
        with patch.object(terminal.ssh, 'configuration', return_value={'servers': {}}), \
                patch.object(terminal, 'client') as client:
            for args in ({'protocol': 'terminal', 'action': 'create'},
                         {'instance': 'bad', 'protocol': 'terminal', 'action': 'create'},
                         {'instance': 'localhost', 'protocol': 'terminal', 'action': 'sudo'}):
                with self.assertRaises(ValueError): remote.remote_control(args)
        client.assert_not_called()

    def test_web_other_user_cron_and_cancelled_cannot_access(self):
        for kwargs in ({'platform': 'web'}, {'user': 'other'}, {'cron': True}):
            token = context.current.set(context.Turn('123', **kwargs))
            try:
                with self.assertRaises(PermissionError):
                    remote.remote_control({'instance': 'localhost', 'protocol': 'terminal', 'action': 'list'})
            finally: context.current.reset(token)
        self.turn.cancelled.set()
        with self.assertRaises(PermissionError): terminal.control('localhost', 'list', {})

    def test_stale_or_cross_instance_handle_never_starts_or_replays(self):
        client = terminal.Terminal('localhost')
        with patch.object(client, 'start') as start:
            for value in ('old-1', 1, {}, None):
                with self.assertRaises(ValueError): client.call('type', {'sessionId': value}, self.turn)
        start.assert_not_called()

    def test_native_arguments_and_results_are_retained(self):
        client = terminal.Terminal('localhost')
        client.schemas = {'create': {'inputSchema': {'properties': {'command': {}}}},
                          'type': {'inputSchema': {'properties': {'sessionId': {}, 'text': {}}}}}
        created = {'content': [{'type': 'text', 'text': 'Created session 1.'}]}
        with patch.object(client, 'start'), patch.object(client, 'running', return_value=True), \
                patch.object(client, 'request', return_value=created) as request:
            result = client.call('create', {'command': 'python3'}, self.turn)
            handle = next(iter(client.sessions))
            self.assertIn(handle, result['content'][-1]['text'])
            self.assertEqual(request.call_args.args[1]['arguments'], {'command': 'python3'})
            client.call('type', {'sessionId': handle, 'text': 'hello'}, self.turn)
            self.assertEqual(request.call_args.args[1]['arguments']['sessionId'], 1)
            with self.assertRaises(ValueError): client.call('type', {'sessionId': handle, 'root': True}, self.turn)

    def test_ssh_launch_fields_stay_out_of_model_parameters(self):
        client = terminal.Terminal('oracle')
        client.schemas = {'create': {'inputSchema': {'properties': {k: {} for k in ('cwd', 'env', 'command')}}}}
        with patch.object(client, 'start'), patch.object(client, 'request', return_value={
                'content': [{'type': 'text', 'text': 'Created session 1.'}]}) as request:
            client.call('create', {'cwd': '/tmp', 'env': {'PYTHONPATH': '/tmp/app'}, 'command': 'python3'}, self.turn)
        params = request.call_args.args[1]['arguments']
        self.assertEqual(params['shell'], str(terminal.SHELL))
        self.assertNotIn('PYTHONPATH', params['env'])
        self.assertEqual(json.loads(params['env']['EVO_TERMINAL_OPTIONS'])['env']['PYTHONPATH'], '/tmp/app')

    def test_ssh_wait_never_calls_remote_shell_return_completion(self):
        client = terminal.Terminal('oracle')
        client.sessions['abc-1'] = {'id': 1, 'command': False}
        client.schemas = {'wait': {'inputSchema': {'properties': {'sessionId': {}, 'pattern': {}}}}}
        with patch.object(client, 'start'), patch.object(client, 'running', return_value=True), \
                patch.object(client, 'request', return_value={}) as request:
            client.call('wait', {'sessionId': 'abc-1'}, self.turn)
            self.assertEqual(request.call_args.args[1]['arguments']['until'], 'quiet')
            client.call('wait', {'sessionId': 'abc-1', 'pattern': 'READY'}, self.turn)
            self.assertNotIn('until', request.call_args.args[1]['arguments'])

    def test_discovered_schema_matches_public_session_handle(self):
        client = terminal.Terminal('localhost')
        client.schemas = {'read': {'inputSchema': {'properties': {'sessionId': {'type': 'integer'}}}}}
        with patch.object(client, 'start'):
            result = client.call('help', {'tool': 'read'}, self.turn)
        self.assertEqual(result['properties']['sessionId']['type'], 'string')
        self.assertEqual(client.schemas['read']['inputSchema']['properties']['sessionId']['type'], 'integer')

    def test_schema_only_help_closes_empty_process_and_reuses_schema(self):
        client = terminal.Terminal('localhost')
        def start(_turn):
            client.schemas = {'read': {'inputSchema': {'properties': {'sessionId': {'type': 'integer'}}}}}
        with patch.object(client, 'start', side_effect=start) as launch, patch.object(client, 'close') as close:
            first = client.call('help', {'tool': 'read'}, self.turn)
            second = client.call('help', {'tool': 'read'}, self.turn)
            self.assertEqual(first, second)
            self.assertEqual(launch.call_count, 1)
            self.assertEqual(close.call_count, 2)

    def test_help_keeps_existing_sessions_and_closes_on_schema_error_only_if_empty(self):
        client = terminal.Terminal('localhost')
        client.schemas = {'read': {'inputSchema': {'properties': {}}}}
        client.sessions['abc-1'] = {'id': 1, 'command': False}
        with patch.object(client, 'close') as close:
            client.call('help', {'tool': 'read'}, self.turn)
            with self.assertRaises(KeyError): client.call('help', {'tool': 'unknown'}, self.turn)
            close.assert_not_called()
            client.sessions.clear()
            with self.assertRaises(KeyError): client.call('help', {'tool': 'unknown'}, self.turn)
            close.assert_called_once()

    def test_batch_budget_covers_action_waits_and_standalone_wait_is_unchanged(self):
        client = terminal.Terminal('localhost')
        client.sessions['abc-1'] = {'id': 1, 'command': False}
        client.schemas = {action: {'inputSchema': {'properties': {k: {} for k in fields}}}
            for action, fields in {'batch': ('sessionId', 'actions'), 'wait': ('sessionId', 'timeoutMs')}.items()}
        with patch.object(client, 'start'), patch.object(client, 'running', return_value=True), \
                patch.object(client, 'request', return_value={}) as request:
            for actions, minimum in (([{'type': 'wait', 'ms': 16000}] * 3, 48),
                    ([{'type': 'wait', 'pattern': 'DONE', 'timeoutMs': 30000}] * 2, 60),
                    ([{'type': 'wait', 'pattern': 'DONE'}] * 10, 50),
                    ([{'type': 'press', 'key': 'ArrowDown'}] * 50, 100)):
                with self.subTest(actions=actions):
                    client.call('batch', {'sessionId': 'abc-1', 'actions': actions}, self.turn)
                    self.assertGreater(request.call_args.args[3], minimum + 10)
            client.call('wait', {'sessionId': 'abc-1', 'timeoutMs': 600000}, self.turn)
            self.assertEqual(request.call_args.args[3], 615)

    def test_launcher_reuses_pinned_ssh_auth_and_disables_escape(self):
        entry = {'host': '192.0.2.5', 'username': 'user', 'known_hosts': '/private/hosts',
                 'identity_file': '/private/key'}
        with patch.dict(terminal.os.environ, {'EVO_TERMINAL_INSTANCE': 'oracle',
                  'EVO_TERMINAL_OPTIONS': json.dumps({'cwd': '/tmp/a b', 'env': {'X': 'hello'}})}, clear=True), \
                patch.object(terminal.ssh, 'configuration', return_value={'servers': {'oracle': entry}}), \
                patch('sys.argv', ['terminal-shell', '-c', 'printf hello']), \
                patch.object(terminal.os, 'execve') as execute:
            terminal.launch_ssh()
        _, argv, env = execute.call_args.args
        self.assertIn('-tt', argv)
        self.assertNotIn('-T', argv)
        self.assertIn('EscapeChar=none', argv)
        self.assertIn('StrictHostKeyChecking=yes', argv)
        self.assertIn('/private/key', argv)
        self.assertIn("cd -- '/tmp/a b'", argv[-1])
        self.assertNotIn('EVO_TERMINAL_OPTIONS', env)

    def test_list_adds_elapsed_only_to_session_metadata(self):
        client = terminal.Terminal('oracle')
        client.sessions['abc-1'] = {'id': 1, 'command': False,
            'created': terminal.time.monotonic() - 7500, 'channel': '123', 'notice': 0}
        client.schemas = {'list': {'inputSchema': {'properties': {}}}}
        result = {'content': [{'type': 'text', 'text': '1 live session(s):\n[1] 80x20 alive'}]}
        with patch.object(client, 'start'), patch.object(client, 'running', return_value=True), \
                patch.object(client, 'request', return_value=result):
            listed = client.call('list', {}, self.turn)
        self.assertIn('[abc-1] 80x20 alive elapsed=2h05m', listed['content'][0]['text'])

    def test_age_reviews_are_grouped_bounded_and_queue_backpressure_retries(self):
        from types import SimpleNamespace
        client = terminal.Terminal('oracle')
        client.sessions = {h: {'created': terminal.time.monotonic()-7201, 'channel': '123', 'notice': 0}
                           for h in ('abc-1', 'abc-2')}
        bot = SimpleNamespace(codex_callback_scope=lambda channel: (0, 0),
                              enqueue_codex_callback=MagicMock(return_value=False))
        with patch.dict(terminal.CLIENTS, {'oracle': client}, clear=True), \
                patch.object(client, 'running', return_value=True), patch.object(client, 'request') as rpc_call:
            terminal.notify_due(bot)
            self.assertEqual(client.sessions['abc-1']['notice'], 0)
            bot.enqueue_codex_callback.return_value = True
            terminal.notify_due(bot)
            first = bot.enqueue_codex_callback.call_args.args
            self.assertIn('sessionId=abc-1', first[1])
            self.assertIn('sessionId=abc-2', first[1])
            self.assertTrue(first[3]())
            calls = bot.enqueue_codex_callback.call_count
            terminal.notify_due(bot)
            self.assertEqual(bot.enqueue_codex_callback.call_count, calls)
            for meta in client.sessions.values(): meta['created'] -= 3600
            terminal.notify_due(bot)
            self.assertEqual(bot.enqueue_codex_callback.call_count, calls+1)
            self.assertFalse(first[3]())  # The newer review supersedes a queued 2h review.
            latest = bot.enqueue_codex_callback.call_args.args
            self.assertTrue(latest[3]())
            for meta in client.sessions.values(): meta['created'] -= 3600
            terminal.notify_due(bot)
            self.assertEqual(bot.enqueue_codex_callback.call_count, calls+1)
            client.sessions.clear()
            self.assertFalse(latest[3]())
            rpc_call.assert_not_called()

    def test_age_check_skips_busy_or_stopped_clients(self):
        from types import SimpleNamespace
        client = terminal.Terminal('oracle')
        client.lock = threading.Lock()
        client.sessions['abc-1'] = {'created': terminal.time.monotonic()-7201, 'channel': '123', 'notice': 0}
        bot = SimpleNamespace(enqueue_codex_callback=MagicMock())
        with patch.dict(terminal.CLIENTS, {'oracle': client}, clear=True):
            with client.lock:
                terminal.notify_due(bot)
            terminal.notify_due(bot)  # No server process: never launch one to check age.
        bot.enqueue_codex_callback.assert_not_called()

    def test_sandbox_default_is_unchanged_and_accepts_alternate_program(self):
        original = rpc.sandbox_argv(Path('/fixture'), 'model')
        modified = rpc.sandbox_argv(Path('/fixture'), 'model', command=['node', 'terminal-use'])
        self.assertEqual(original[:-3], modified[:-2])
        self.assertEqual(original[-3:], ['/usr/bin/node', str(rpc.ROOT/'pi-runner.mjs'), 'model'])


if __name__ == '__main__': unittest.main(verbosity=2)
