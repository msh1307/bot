"""Cross-layer contracts: no real commands, browsers, secrets or Discord messages."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from evo_bot import context, rpc, tools
from evo_bot.integrations import browser, coding, codex, host, remote, ssh
from verify_host_reason import broker


class Contracts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.turn = context.Turn('77')
        self.token = context.current.set(self.turn)
        self.policy = patch.object(broker, 'POLICY', {'users':['evo-bot','root'], 'max_timeout':600,
            'workspace':self.tmp.name, 'private_paths':[], 'runtime':self.tmp.name, 'approval_ttl':300})
        self.policy.start()

    def tearDown(self):
        self.policy.stop(); context.current.reset(self.token); self.tmp.cleanup()

    def test_omitted_execution_timeout_is_ten_minutes_on_every_target(self):
        self.assertEqual(broker.validate({'command':'true'})['timeout'],600)
        self.assertEqual(ssh.validate({'command':'true'}),('true',600))
        self.assertEqual(codex.exec_timeout_ms({}),600000)
        for seconds in (None,.5):
            request={'command':'true',**({} if seconds is None else {'timeout':seconds})}
            with patch.object(host,'host_command',return_value={'success':True,'exit_code':0}) as host_run, \
                 patch.object(ssh,'remote_ssh',return_value={'exit_code':0}) as ssh_run, \
                 patch.dict(codex.SERVERS,{'pc':object()},clear=True), \
                 patch.object(codex,'windows_exec',return_value={'exit_code':0}) as windows_run:
                for ident in ('localhost','n100','pc'): coding.execute({'instance':ident,**request})
                expected=600 if seconds is None else seconds
                self.assertEqual(host_run.call_args.args[0]['timeout'],expected)
                self.assertEqual(ssh_run.call_args.args[0]['timeout'],expected)
                self.assertEqual(windows_run.call_args.args[0]['timeout_ms'],int(expected*1000))

    def test_all_execution_layers_accept_same_timeout_values(self):
        for seconds in (.1, .5, 180, 300, 600):
            self.assertEqual(broker.validate({'command':'true','timeout':seconds})['timeout'], seconds)
            self.assertEqual(ssh.validate({'command':'true','timeout':seconds}), ('true',seconds))
            with patch.object(host,'host_command',return_value={'success':True,'exit_code':0}) as run:
                coding.execute({'instance':'localhost','command':'true','timeout':seconds})
                self.assertEqual(run.call_args.args[0]['timeout'],seconds)

    def test_invalid_timeout_never_dispatches(self):
        for value in (0, -.1, 601, True, '180', float('nan'), float('inf')):
            with self.subTest(value=value):
                for validate in (broker.validate, ssh.validate):
                    with self.assertRaises(ValueError): validate({'command':'true','timeout':value})
                with patch.object(host,'host_command') as run:
                    with self.assertRaises(ValueError): coding.execute({'instance':'localhost','command':'true','timeout':value})
                    run.assert_not_called()

    def test_broker_limit_error_uses_actual_policy(self):
        broker.POLICY['max_timeout'] = 120
        with self.assertRaisesRegex(ValueError, '0.1..120'):
            broker.validate({'command':'true','timeout':180})

    def test_unprivileged_command_limit_matches_ssh_root_limit_stays_reviewable(self):
        command = 'echo ' + 'a'*2000
        self.assertEqual(broker.validate({'command':command})['command'], command)
        self.assertEqual(ssh.validate({'command':command})[0], command)
        with self.assertRaisesRegex(ValueError, '1000'):
            broker.validate({'command':command,'run_as':'root','reason':'Owner test'})
        with self.assertRaisesRegex(ValueError, '16000'):
            broker.validate({'command':'a'*16001})

    def test_host_transport_waits_for_advertised_command_budget(self):
        peer = Mock(); peer.recv.return_value = b'{"success":true}\n'
        sock = Mock(); sock.__enter__ = Mock(return_value=peer); sock.__exit__ = Mock(return_value=False)
        with patch.object(host.socket,'socket',return_value=sock), patch.object(host.time,'monotonic',side_effect=[0,200]):
            self.assertEqual(host.rpc({'action':'execute'}, timeout=615), {'success':True})

    def test_default_shell_uses_selected_windows_instance_exactly_once(self):
        with patch.dict(codex.SERVERS, {'pc':object()}, clear=True), \
             patch.object(codex,'windows_exec',return_value={'exit_code':0,'stdout':'ok'}) as run, \
             patch.object(ssh,'remote_ssh') as linux:
            coding.execute({'instance':'pc','command':'Write-Output ok'})
            self.assertEqual(run.call_args.args[0]['instance'],'pc')
            self.assertEqual(run.call_args.args[0]['command'][0],'powershell.exe')
            run.assert_called_once(); linux.assert_not_called()
            with self.assertRaisesRegex(ValueError,'elevation'):
                coding.execute({'instance':'pc','command':'Write-Output ok','root':True,'reason':'test'})
            self.assertEqual(run.call_count,1)

    def test_file_target_checked_before_path_and_never_falls_back(self):
        with patch.object(ssh,'configuration',return_value={'servers':{}}), patch.dict(codex.SERVERS,{},clear=True), \
             patch.object(coding.remote_files,'exchange') as run:
            with self.assertRaisesRegex(ValueError, 'Unknown instance'):
                coding.file_io({'instance':'instance_placeholder','action':'write','path':'relative','content':'x'})
            run.assert_not_called()

    def test_help_accepts_selected_instance_and_equivalent_nested_routes(self):
        expected = remote.GUIDES['codex']
        self.assertEqual(remote.remote_control({'action':'help','instance':'pc','protocol':'codex'}), expected)
        self.assertEqual(remote.remote_control({'action':'help','args':{'protocol':'codex','instance':'pc'}}), expected)
        self.assertEqual(remote.remote_control({'action':'help','protocol':'codex','args':{'action':'help','protocol':'codex'}}),expected)

    def test_conflicting_route_is_rejected_before_dispatch(self):
        for key, outer, inner in [('instance','pc','other'), ('protocol','codex','rdp'), ('action','help','stop')]:
            with patch.object(codex,'codex_task') as run, self.assertRaisesRegex(ValueError,'Conflicting'):
                remote.remote_control({'action':'help',key:outer,'args':{key:inner}})
            run.assert_not_called()

    def test_compact_help_and_invalid_args_give_exact_action_contract(self):
        result=remote.remote_control({'action':'help','protocol':'codex','instance':'pc','args':{'tool':'read'}})
        self.assertEqual(result,{'read':remote.GUIDES['codex']['read']})
        with patch.object(remote,'catalog',return_value={'instances':[{'id':'pc','protocols':['codex']}],'defaults':{}}), \
             patch.object(codex,'codex_task') as run:
            with self.assertRaisesRegex(ValueError, 'read accepts: cursor, thread_id'):
                remote.remote_control({'action':'read','protocol':'codex','instance':'pc','args':{'thread_id':'t','tail_lines':5}})
            run.assert_not_called()

    def test_each_wait_has_an_independent_limit_and_no_forced_handoff(self):
        self.turn.cancelled = Mock(); self.turn.cancelled.is_set.return_value=False; self.turn.cancelled.wait.return_value=False
        for _ in range(2):
            self.assertEqual(tools.wait({'seconds':300}), {'waited_seconds':300})
        self.assertEqual([c.args for c in self.turn.cancelled.wait.call_args_list], [(300,),(300,)])

    def test_stale_frame_returns_live_choices_without_replaying_action(self):
        client=browser.Browser(); client.instance='oracle'
        error=ValueError('Unknown or detached frame; list frames again. No main-page fallback.')
        with patch.object(client,'running',return_value=True), patch.object(client,'start'), \
             patch.object(client,'close') as close, patch.object(client,'touch'), \
             patch.object(client,'request',side_effect=[error,{'text':json.dumps([{'frame':'f2','name':'approval'}])}]) as request:
            with self.assertRaisesRegex(ValueError,'f2'):
                client.call({'action':'click','selector':'#next','frame':'f1'},self.turn)
            self.assertEqual([c.args[1]['action'] for c in request.call_args_list],['click','frames'])
            close.assert_not_called()

    def test_notice_uses_public_tool_and_never_raw_secret(self):
        event={'toolName':'_pi_io','displayName':'edit','arguments':{'action':'write','content':'private'}}
        self.assertEqual(rpc.tool_failure_notice(event,ValueError('SSH/host files require an absolute path or ~/...')),
                         ('edit','path','서버·절대경로 확인 필요'))
        for error in (ValueError('private-password'),RuntimeError('private-password'),PermissionError('private-password')):
            self.assertNotIn('private',str(rpc.tool_failure_notice(event,error)))
        event['displayName']='private-password'
        self.assertEqual(rpc.tool_failure_notice(event,ValueError('bad'))[0],'write')


if __name__=='__main__': unittest.main()
