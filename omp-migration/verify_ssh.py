"""SSH credential/owner boundaries and real local OpenSSH transport; no LLM."""
import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from evo_omp import context, ssh_askpass
from evo_omp.integrations import ssh, instances, credential_input
from evo_omp.integrations import host
from evo_omp.tools import Registry
from evo_omp.tool_output import TOOLS


class SSHChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.password = self.root / 'password'
        self.password.write_text('synthetic-ssh-secret\n')
        self.password.chmod(0o600)
        self.config = self.root / 'ssh.json'
        self.entry = {'host': '127.0.0.1', 'username': 'operator',
                      'known_hosts': str(self.root / 'known_hosts'),
                      'password_file': str(self.password)}
        self.save()
        self.pin = patch.object(ssh, 'CONFIG', self.config); self.pin.start()
        self.accounts_pin = patch.object(credential_input, 'CONFIG', self.config); self.accounts_pin.start()
        self.turn = context.Turn('123')
        self.token = context.current.set(self.turn)

    def save(self):
        auth = {k: v for k, v in self.entry.items() if k in
                ('username', 'password_file', 'identity_file', 'sudo_password_file')}
        auth['ssh_instances'] = ['test']
        settings = {k: v for k, v in self.entry.items() if k not in auth and k != 'host'}
        settings['account'] = 'test-login'
        self.config.write_text(json.dumps({'defaults': {'ssh': 'test'}, 'instances': {
            'test': {'host': self.entry['host'], 'ssh': settings}}, 'accounts': {'test-login': auth}}))
        self.config.chmod(0o600)

    def tearDown(self):
        context.current.reset(self.token)
        self.pin.stop(); self.accounts_pin.stop(); self.tmp.cleanup()

    def test_no_password_in_argv_or_environment(self):
        argv, env = ssh.transport(self.entry, 'id')
        self.assertNotIn('synthetic-ssh-secret', repr((argv, env)))
        self.assertEqual(env['EVO_SSH_PASSWORD_FILE'], str(self.password))
        self.assertIn('StrictHostKeyChecking=yes', argv)
        self.assertIn('NumberOfPasswordPrompts=1', argv)
        self.assertNotIn('SSH_AUTH_SOCK', env)

    def test_key_auth_never_requests_a_password(self):
        entry = dict(self.entry); entry['identity_file'] = entry.pop('password_file')
        argv, env = ssh.transport(entry, 'id')
        self.assertIn('BatchMode=yes', argv)
        self.assertIn('IdentityAgent=none', argv)
        self.assertNotIn('SSH_ASKPASS', env)

    def test_owner_and_interactive_scope(self):
        for changes in ({'user': '456'}, {'platform': 'web'}, {'cron': True}):
            token = context.current.set(context.Turn('123', **changes))
            try:
                with self.assertRaises(PermissionError): ssh.remote_ssh({'command': 'id'})
            finally: context.current.reset(token)

    def test_cancel_before_launch(self):
        self.turn.cancelled.set()
        with patch.object(ssh.subprocess, 'Popen') as popen:
            with self.assertRaises((InterruptedError, PermissionError)): ssh.remote_ssh({'command': 'id'})
            popen.assert_not_called()

    def test_closed_schema_and_limits(self):
        for args in ({'command': 'id', 'host': 'other'}, {'command': 'x\0y'},
                     {'command': 'id', 'timeout': True}, {'command': 'id', 'timeout': 301},
                     {'command': 'id', 'instance': 'bad@host'}, {'command': ''}):
            with self.assertRaises(ValueError): ssh.validate(args)

    def test_reject_unsafe_auth_configuration(self):
        for change in ({'username': 'root'}, {'host': '-oProxyCommand=evil'},
                       {'identity_file': '/key'}, {'port': True}, {'known_hosts': 'relative'}):
            old = self.entry.copy(); self.entry.update(change); self.save()
            with self.assertRaises(ValueError): ssh.configuration()
            self.entry = old
        self.save()
        self.config.chmod(0o644)
        with self.assertRaises(ValueError): ssh.configuration()

    def test_askpass_only_password_prompt_and_no_error_echo(self):
        import io
        for prompt, expected in (("operator@host's password: ", 0), ('Enter passphrase:', 1),
                                 ('Are you sure?', 1), ('Verification code:', 1)):
            out = io.StringIO()
            with patch.dict(os.environ, {'EVO_SSH_PASSWORD_FILE': str(self.password)}), \
                    patch.object(sys, 'argv', ['askpass', prompt]), contextlib.redirect_stdout(out):
                self.assertEqual(ssh_askpass.main(), expected)
            self.assertEqual(out.getvalue(), 'synthetic-ssh-secret\n' if expected == 0 else '')

    def test_askpass_missing_file_does_not_echo_path(self):
        import io
        out = io.StringIO()
        with patch.dict(os.environ, {'EVO_SSH_PASSWORD_FILE': '/missing-private-path'}), \
                patch.object(sys, 'argv', ['askpass', 'Password:']), contextlib.redirect_stdout(out):
            self.assertEqual(ssh_askpass.main(), 1)
        self.assertEqual(out.getvalue(), '')

    def test_unknown_instance_never_connects(self):
        with patch.object(ssh.subprocess, 'Popen') as popen:
            with self.assertRaises(ValueError): ssh.remote_ssh({'instance': 'other', 'command': 'id'})
            popen.assert_not_called()

    def local_process(self, entry, command):
        return [sys.executable, '-c', self.script], {'PATH': '/usr/bin:/bin'}

    def test_bounded_output_and_exit_status(self):
        self.script = 'import sys; print("x"*300000); sys.exit(7)'
        with patch.object(ssh, 'transport', self.local_process):
            result = json.loads(ssh.remote_ssh({'command': 'fixture'}))
        self.assertEqual(result['exit_code'], 7)
        self.assertTrue(result['output_truncated'])
        self.assertEqual(len(result['output']), ssh.MAX_OUTPUT)

    def test_connection_password_is_withheld_if_echoed(self):
        self.script = 'print("synthetic-ssh-secret")'
        with patch.object(ssh, 'transport', self.local_process):
            result = json.loads(ssh.remote_ssh({'command': 'fixture'}))
        self.assertNotIn('synthetic-ssh-secret', result['output'])

    def test_timeout_keeps_partial_result_without_replaying(self):
        self.script = 'import time; print("started",flush=True); time.sleep(10)'
        with patch.object(ssh, 'transport', self.local_process):
            result = json.loads(ssh.remote_ssh({'command': 'fixture', 'timeout': 1}))
        self.assertIsNone(result['exit_code'])
        self.assertTrue(result['timed_out'])
        self.assertEqual(result['output'], 'started\n')

    def test_cancellation_during_command(self):
        self.script = 'import time; time.sleep(10)'
        timer = threading.Timer(.2, self.turn.cancelled.set); timer.start()
        try:
            with patch.object(ssh, 'transport', self.local_process):
                with self.assertRaises(InterruptedError): ssh.remote_ssh({'command': 'fixture'})
        finally: timer.join()

    def test_inventory_and_discovery(self):
        registry = Registry()
        self.assertIn('remote_control', {d['name'] for d in registry.definitions()})
        self.assertNotIn('shell_exec', {d['name'] for d in registry.definitions()})
        self.assertNotIn('host_command', {d['name'] for d in registry.definitions()})
        self.assertIn('file_transfer', {d['name'] for d in registry.definitions()})
        self.assertNotIn('shell_exec', {d['name'] for d in registry.definitions(cron=True)})
        self.assertNotIn('remote_ssh', {d['name'] for d in registry.definitions()})
        self.assertNotIn('remote_ssh', {d['name'] for d in registry.definitions(cron=True)})
        self.assertIn('shell_exec', TOOLS)
        self.assertNotIn('remote_ssh', registry.tools)
        with patch('evo_omp.integrations.codex.server_config', return_value=(None, {})), \
                patch('evo_omp.integrations.rdp.configuration', return_value={'servers': {}}):
            item = instances.catalog()['instances'][0]
        self.assertEqual(item['id'], 'test')
        self.assertEqual(item['capabilities'], ['remote_ssh'])

    def test_short_shell_call_selects_exact_instance_and_command(self):
        with patch.object(ssh, 'remote_ssh', return_value='RESULT') as run:
            self.assertEqual(ssh.shell_exec({'instance': 'test', 'cmd': 'sudo -n id -u'}), 'RESULT')
            run.assert_called_once_with({'instance': 'test', 'command': 'sudo -n id -u'})

    def test_short_root_call_uses_existing_approval_route(self):
        with patch.object(ssh, 'remote_ssh', return_value='RESULT') as run:
            self.assertEqual(ssh.shell_exec({'instance': 'test', 'cmd': 'id -u', 'root': True, 'timeout': 20}), 'RESULT')
            run.assert_called_once_with({'instance': 'test', 'command': 'id -u', 'timeout': 20,
                                        'run_as': 'root', 'reason': 'Requested root command on test'})

    def test_short_shell_call_rejects_ambiguous_or_extra_routing(self):
        with patch.object(ssh, 'remote_ssh') as run:
            for args in ({'cmd': 'id'}, {'instance': 'test'}, {'instance': None, 'cmd': 'id'},
                         {'instance': 'test', 'cmd': 'id', 'root': 1},
                         {'instance': 'test', 'cmd': 'id', 'host': 'other'}):
                with self.assertRaises(ValueError): ssh.shell_exec(args)
            run.assert_not_called()

    def root_args(self):
        self.entry['sudo_password_file'] = str(self.password); self.save()
        return {'command': 'id -u', 'run_as': 'root', 'reason': 'Synthetic approval test'}

    def test_root_requires_registered_secret_and_reason(self):
        with self.assertRaises(ValueError): ssh.validate({'command': 'id', 'run_as': 'root'})
        with patch.object(host, 'approve_root_once') as approve, patch.object(ssh, 'execute') as execute:
            with self.assertRaises(ValueError):
                ssh.remote_ssh({'command': 'id', 'run_as': 'root', 'reason': 'Test'})
            approve.assert_not_called(); execute.assert_not_called()

    def test_root_denial_never_reads_password_or_connects(self):
        args = self.root_args()
        with patch.object(host, 'approve_root_once', return_value={'success': False, 'approval_status': 'denied_or_expired'}), \
             patch.object(ssh, 'stored_password') as read, patch.object(ssh, 'execute') as execute:
            result = json.loads(ssh.remote_ssh(args))
        self.assertFalse(result['success']); read.assert_not_called(); execute.assert_not_called()

    def test_passwordless_sudo_configuration_is_explicit_and_unambiguous(self):
        self.entry['identity_file'] = self.entry.pop('password_file')
        self.entry['sudo_nopasswd'] = True; self.save()
        self.assertTrue(ssh.configuration()['servers']['test']['sudo_nopasswd'])
        for change in ({'sudo_nopasswd': 'true'}, {'sudo_nopasswd': 1},
                       {'sudo_nopasswd': True, 'sudo_password_file': str(self.password)}):
            self.entry.update(change); self.save()
            with self.assertRaises(ValueError): ssh.configuration()

    def test_passwordless_root_denial_never_reads_secrets_or_connects(self):
        self.entry['sudo_nopasswd'] = True; self.save()
        with patch.object(host, 'approve_root_once', return_value={'success': False}) as approve, \
                patch.object(ssh, 'stored_password') as read, patch.object(ssh, 'execute') as execute:
            result = json.loads(ssh.remote_ssh({'command': 'id -u', 'run_as': 'root', 'reason': 'Test'}))
        self.assertFalse(result['success']); approve.assert_called_once()
        read.assert_not_called(); execute.assert_not_called()

    def test_passwordless_root_uses_same_approval_without_password_read(self):
        self.entry['sudo_nopasswd'] = True; self.save()
        with patch.object(host, 'approve_root_once', return_value=None) as approve, \
                patch.object(host, 'discord_connected', return_value=True), \
                patch.object(ssh, 'stored_password') as read, patch.object(ssh, 'execute', return_value='{}') as execute:
            ssh.remote_ssh({'command': 'id -u', 'run_as': 'root', 'reason': 'Test'})
        approve.assert_called_once(); read.assert_not_called()
        self.assertEqual(execute.call_args.kwargs, {'sudo_nopasswd': True})

    def test_passwordless_root_child_gets_eof_and_noninteractive_sudo(self):
        import shlex
        captured = []
        def nopasswd(entry, command):
            captured.append(shlex.split(command))
            return captured[-1][6:], {'PATH': '/usr/bin:/bin'}
        with patch.object(ssh, 'transport', nopasswd), patch.object(host, 'discord_connected', return_value=True):
            result = json.loads(ssh.execute(self.entry, 'test', 'cat; printf EOF_OK', 5,
                                           self.turn, sudo_nopasswd=True))
        self.assertEqual(result['output'], 'EOF_OK')
        self.assertEqual(captured[0][:6], ['sudo', '-k', '-n', '-p', '', '--'])

    def test_passwordless_root_still_stops_on_discord_disconnect(self):
        self.script = 'import time; time.sleep(10)'
        with patch.object(ssh, 'transport', self.local_process), patch.object(host, 'discord_connected', return_value=False):
            with self.assertRaises(InterruptedError):
                ssh.execute(self.entry, 'test', 'fixture', 5, self.turn, sudo_nopasswd=True)

    def test_root_preview_binds_instance_target_command_without_value(self):
        args = self.root_args()
        with patch.object(host, 'approve_root_once', return_value=None) as approve, \
             patch.object(host, 'discord_connected', return_value=True), \
             patch.object(ssh, 'execute', return_value='{}') as execute:
            ssh.remote_ssh(args)
        preview = approve.call_args.args[1]
        self.assertIn('operator@127.0.0.1:22 (test)', preview)
        self.assertIn('id -u', preview)
        self.assertNotIn('synthetic-ssh-secret', repr(approve.call_args))
        self.assertEqual(execute.call_args.kwargs['sudo_password'], 'synthetic-ssh-secret')

    def test_root_password_is_only_stdin_and_child_command_receives_eof(self):
        self.entry['sudo_password_file'] = str(self.password); self.save()
        captured = []
        def fake_sudo_transport(entry, command):
            captured.append(command)
            # Simulate sudo consuming one password line, then executing the quoted shell.
            import shlex
            script = 'import subprocess,sys; p=sys.stdin.readline(); print("AUTH_OK" if p=="synthetic-ssh-secret\\n" else "AUTH_BAD",flush=True); sys.exit(subprocess.call(sys.argv[1:]))'
            return [sys.executable, '-c', script, *shlex.split(command)[6:]], {'PATH': '/usr/bin:/bin'}
        # sudo -k -S -p '' -- /bin/sh -c <script> has six arguments before the shell.
        with patch.object(ssh, 'transport', fake_sudo_transport), patch.object(host, 'discord_connected', return_value=True):
            value = json.loads(ssh.execute(self.entry, 'test', 'cat; printf ROOT_OK', 5, self.turn,
                                          sudo_password='synthetic-ssh-secret'))
        self.assertEqual(value['exit_code'], 0, value)
        self.assertEqual(value['output'], 'AUTH_OK\nROOT_OK')
        self.assertNotIn('synthetic-ssh-secret', captured[0])

    def test_nopasswd_root_command_does_not_receive_password_on_stdin(self):
        import shlex
        def nopasswd(entry, command):
            return shlex.split(command)[6:], {'PATH': '/usr/bin:/bin'}
        with patch.object(ssh, 'transport', nopasswd), patch.object(host, 'discord_connected', return_value=True):
            value = json.loads(ssh.execute(self.entry, 'test', 'cat; printf EOF_OK', 5, self.turn,
                                          sudo_password='synthetic-ssh-secret'))
        self.assertEqual(value['output'], 'EOF_OK')

    def test_truncated_echo_does_not_expose_password_prefix(self):
        self.script = 'print("x"*(' + str(ssh.MAX_OUTPUT) + '-5)+"synthetic-ssh-secret",end="")'
        with patch.object(ssh, 'transport', self.local_process):
            value = json.loads(ssh.remote_ssh({'command': 'fixture'}))
        self.assertTrue(value['output'].endswith('[credential withheld]'))
        self.assertNotIn('synth', value['output'])


if __name__ == '__main__': unittest.main()
