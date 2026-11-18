"""Model/settings/slash parity; no real chats or credentials."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from evo_bot import context, tools
from evo_bot.integrations import codex


class ParityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = patch.object(codex, 'STATE', Path(self.tmp.name)/'jobs.json'); self.state.start()
        self.turn = context.Turn('77')
        self.token = context.current.set(self.turn)

    async def asyncTearDown(self):
        context.current.reset(self.token); self.state.stop(); self.tmp.cleanup()

    def bridge(self, kind=codex.Bridge):
        b=kind({'url':'wss://unused','ca':'unused','token':'test','cwd':'/tmp'},'test')
        b.monitor_desktop=lambda job:None
        b.prefer_gui=AsyncMock(return_value=False)
        return b

    async def test_first_turn_settings_shared_by_both_platforms(self):
        for kind in (codex.Bridge,codex.AppServer):
            b=self.bridge(kind)
            before={'thread':{'id':'t','model':'cyber','daybreakEnabled':False},
                    'model':'cyber','approvalPolicy':'on-request','sandbox':{'type':'readOnly'}}
            b.rpc=AsyncMock(side_effect=[before,{}, {'thread':dict(before['thread'],daybreakEnabled=True)}])
            with patch.object(b,'model_catalog',new=AsyncMock(return_value=[{'id':'cyber'}])):
                result=await b.start_native_thread({'model':'cyber','mode':'on','policy':'on-request','sandbox':'read-only'}, {'cwd':'path'})
            self.assertEqual(b.rpc.call_args_list[0].args[1],{'cwd':'path','model':'cyber','approvalPolicy':'on-request','sandbox':'read-only'})
            self.assertTrue(result['thread']['daybreakEnabled'])
            self.assertEqual([x.args[0] for x in b.rpc.call_args_list],['thread/start','thread/metadata/update','thread/read'])

    async def test_mode_off_is_written_even_when_native_default_is_on(self):
        b=self.bridge(); b.rpc=AsyncMock(side_effect=[{'thread':{'id':'t','daybreakEnabled':True}}, {},
            {'thread':{'id':'t','daybreakEnabled':False}}])
        await b.start_native_thread({'mode':'off'},{})
        self.assertIs(b.rpc.call_args_list[1].args[1]['daybreakEnabled'],False)

    async def test_unconfirmed_setting_never_reaches_prompt(self):
        for wrong in ('model','policy','daybreak'):
            b=self.bridge(); b.rpc=AsyncMock(side_effect=[{'thread':{'id':'t','model':'wrong' if wrong=='model' else 'chosen',
                'daybreakEnabled':True},'approvalPolicy':'never' if wrong=='policy' else 'on-request'}, {},
                {'thread':{'id':'t','model':'chosen','daybreakEnabled':True}}])
            with patch.object(b,'model_catalog',new=AsyncMock(return_value=[{'id':'chosen'}])):
                with self.assertRaises(codex.DesktopError):
                    await b.start_native_thread({'model':'chosen','policy':'on-request','mode':'off' if wrong=='daybreak' else 'on'},{})
            self.assertNotIn('turn/start',[c.args[0] for c in b.rpc.call_args_list])

    async def test_invalid_model_never_creates_thread(self):
        b=self.bridge(); b.rpc=AsyncMock()
        with patch.object(b,'model_catalog',new=AsyncMock(return_value=[{'id':'offered'}])):
            with self.assertRaises(ValueError): await b.start_native_thread({'model':'missing'},{})
        b.rpc.assert_not_called()

    async def test_omitted_settings_keep_server_defaults(self):
        b=self.bridge(); b.rpc=AsyncMock(return_value={'thread':{'id':'t','model':'server-default'}})
        out=await b.start_native_thread({}, {'cwd':'x'})
        self.assertEqual(b.rpc.call_args.args[1],{'cwd':'x'});self.assertEqual(out['model'],'server-default')

    async def test_windows_start_prompt_skips_picker_and_preserves_defaults(self):
        b=self.bridge();b.settings['cwd']=r'C:\workspace';b.open_new_chat=AsyncMock()
        b.rpc=AsyncMock(side_effect=[{'thread':{'id':'t','ephemeral':True,
            'model':'native-default','daybreakEnabled':True}}, {'turn':{'id':'turn'}}])
        out=await b.action({'action':'start','prompt':'Inspect the code'},'77')
        b.open_new_chat.assert_not_called()
        self.assertEqual([c.args[0] for c in b.rpc.call_args_list],['thread/start','turn/start'])
        self.assertEqual(b.rpc.call_args_list[0].args[1],{'cwd':r'C:\workspace','ephemeral':True})
        self.assertEqual(b.rpc.call_args_list[1].args[1],codex.turn_input('t','Inspect the code',True))
        self.assertEqual(out['state'],'running');self.assertEqual(out['model'],'native-default')
        self.assertTrue(out['wake']);self.assertTrue(b.jobs[out['job_id']]['_compact'])

    async def test_windows_picker_remains_available_without_prompt_or_with_new(self):
        for args in ({'action':'start'}, {'action':'new','prompt':'Inspect the code'}):
            b=self.bridge();b.rpc=AsyncMock();b.open_new_chat=AsyncMock(return_value={'picker':True})
            self.assertEqual(await b.action(args,'77'),{'picker':True})
            b.open_new_chat.assert_awaited_once();b.rpc.assert_not_called()

    async def test_windows_direct_start_validates_prompt_and_existing_ids(self):
        for extra in ({'prompt':''},{'prompt':'  '},{'prompt':123},{'prompt':'x'*16001},
                      {'prompt':'hi','thread_id':'existing'},{'prompt':'hi','job_id':'existing'}):
            b=self.bridge();b.rpc=AsyncMock();b.open_new_chat=AsyncMock()
            with self.assertRaises(ValueError):await b.action(dict(action='start',**extra),'77')
            b.rpc.assert_not_called();b.open_new_chat.assert_not_called()

    async def test_windows_direct_start_does_not_replay_or_open_picker_on_timeout(self):
        for fail_at in ('thread/start','turn/start'):
            b=self.bridge();b.settings['cwd']=r'C:\workspace';b.open_new_chat=AsyncMock()
            replies=[TimeoutError('uncertain result')] if fail_at=='thread/start' else [
                {'thread':{'id':'t','ephemeral':True}},TimeoutError('uncertain result')]
            b.rpc=AsyncMock(side_effect=replies)
            with self.assertRaises(TimeoutError):await b.action({'action':'start','prompt':'hi'},'77')
            self.assertEqual([c.args[0] for c in b.rpc.call_args_list],
                ['thread/start'] if fail_at=='thread/start' else ['thread/start','turn/start'])
            b.open_new_chat.assert_not_called()
            if fail_at=='turn/start':self.assertEqual(next(iter(b.jobs.values()))['state'],'connection_lost')

    async def test_windows_direct_start_keeps_project_and_explicit_settings(self):
        b=self.bridge();b.open_new_chat=AsyncMock()
        b.select_project=AsyncMock(return_value={'project_id':'p','name':'Code',
            'default_cwd':r'C:\code','roots':[r'C:\code']})
        b.model_catalog=AsyncMock(return_value=[{'id':'chosen'}])
        b.rpc=AsyncMock(side_effect=[{'thread':{'id':'t','ephemeral':False,'projectId':'p',
            'model':'chosen','daybreakEnabled':False},'approvalPolicy':'on-request',
            'sandbox':{'type':'readOnly'}}, {'turn':{'id':'turn'}}])
        out=await b.action(dict(action='start',prompt='hi',project='p',model='chosen',mode='off',
            policy='on-request',sandbox='read-only',wake=False,compact=False),'77')
        self.assertEqual(b.rpc.call_args_list[0].args[1],{'cwd':r'C:\code','ephemeral':False,
            'projectId':'p','runtimeWorkspaceRoots':[r'C:\code'],'model':'chosen',
            'approvalPolicy':'on-request','sandbox':'read-only'})
        self.assertEqual(b.rpc.call_args_list[1].args[1],codex.turn_input('t','hi',False))
        self.assertFalse(out['wake']);self.assertFalse(b.jobs[out['job_id']]['_compact'])
        b.open_new_chat.assert_not_called()

    async def test_scheduled_start_remains_denied_on_both_platforms(self):
        self.turn.cron=True
        for kind in (codex.Bridge,codex.AppServer):
            b=self.bridge(kind);b.rpc=AsyncMock();b.open_new_chat=AsyncMock()
            with self.assertRaises(PermissionError):await b.action({'action':'start','prompt':'hi'},'77')
            b.rpc.assert_not_called();b.open_new_chat.assert_not_called()

    async def test_model_lookup_pages_and_second_page_selection(self):
        b=self.bridge(); b.settings_rpc=AsyncMock(side_effect=[{'data':[{'id':'a'}],'nextCursor':'page2'},
                                                               {'data':[{'id':'b'}]}])
        rows=await b.model_catalog();self.assertEqual([r['id'] for r in rows],['a','b'])
        self.assertEqual(b.settings_rpc.call_args.args[1]['cursor'],'page2')

    async def test_goal_slash_both_platforms_reads_native_readback_only(self):
        for kind in (codex.Bridge,codex.AppServer):
            b=self.bridge(kind);b.jobs['j']={'thread_id':'t','source':'app_server'}
            b.rpc=AsyncMock(side_effect=[{'thread':{'id':'t'}},{'thread':{'id':'t'}},{}, {'goal':None}])
            out=await b.action({'action':'send','thread_id':'t','prompt':'/goal clear'},'77')
            self.assertIsNone(out['goal'])
            self.assertEqual([x.args[0] for x in b.rpc.call_args_list],['thread/read','thread/resume','thread/goal/clear','thread/goal/get'])

    async def test_gui_slash_never_resumes_a_second_daemon_copy(self):
        b=self.bridge();b.rpc=AsyncMock(return_value={'thread':{'id':'t'}})
        for prompt in ('/goal show','/goal clear','/compact'):
            with self.assertRaisesRegex(codex.DesktopError,'daemon-owned'):
                await b.action({'action':'send','thread_id':'t','prompt':prompt},'77')
        self.assertTrue(all(c.args[0]=='thread/read' for c in b.rpc.call_args_list))

    async def test_goal_failure_not_reported_as_cleared(self):
        b=self.bridge();b.jobs['j']={'thread_id':'t','source':'app_server'}
        b.rpc=AsyncMock(side_effect=[{'thread':{'id':'t'}},{'thread':{'id':'t'}},{},{'goal':{'objective':'still here'}}])
        with self.assertRaises(codex.DesktopError):
            await b.action({'action':'send','thread_id':'t','prompt':'/goal clear'},'77')

    async def test_unsupported_slash_does_not_send_as_chat(self):
        for kind in (codex.Bridge,codex.AppServer):
            b=self.bridge(kind);b.rpc=AsyncMock()
            with self.assertRaisesRegex(ValueError,'Supported:'):
                await b.action({'action':'send','thread_id':'t','prompt':'/not-a-command'},'77')
            b.rpc.assert_not_called()

    async def test_readonly_schedule_cannot_use_slash_to_mutate(self):
        self.turn.cron=True
        for kind in (codex.Bridge,codex.AppServer):
            b=self.bridge(kind);b.rpc=AsyncMock()
            with self.assertRaises(PermissionError):
                await b.action({'action':'send','thread_id':'t','prompt':'/goal clear'},'77')
            b.rpc.assert_not_called()

    async def test_wake_off_revokes_queued_callback_without_sending(self):
        b=self.bridge();b.rpc=AsyncMock();b.adapter=type('Adapter',(),{'codex_callback_scope':lambda _,channel:(0,0)})()
        job={'job_id':'j','thread_id':'t','state':'running','source':'app_server'};b.jobs['j']=job
        b.arm_callback(job,{},'77');revision=job['_revision']
        out=await b.action({'action':'send','thread_id':'t','prompt':'/wake off'},'77')
        self.assertFalse(out['wake']);self.assertNotEqual(revision,job['_revision']);b.rpc.assert_not_called()

    async def test_callback_compact_default_and_explicit_optout_preserved(self):
        b=self.bridge();job={}
        b.arm_callback(job,{},'77');self.assertTrue(job['_compact'])
        b.arm_callback(job,{'compact':False},'77');b.arm_callback(job,{},'77')
        self.assertFalse(job['_compact'])

    async def test_status_inprogress_stays_active_not_completed(self):
        b=self.bridge();job=dict(job_id='j',thread_id='t',source='app_server',state='running',messages={})
        b.jobs['j']=job
        b.rpc=AsyncMock(side_effect=[{'thread':{'id':'t','status':{'type':'idle'}}},
            {'data':[{'id':'turn','status':'inProgress','items':[]}]}])
        await b.status(job)
        self.assertEqual(job['state'],'running');self.assertIn(job['state'],codex.ACTIVE)

    async def test_status_empty_or_previous_turn_does_not_end_newly_accepted_turn(self):
        for rows in ([],[{'id':'old','status':'completed','items':[]}],[{'id':'old','status':'interrupted','items':[]}]):
            b=self.bridge();job=dict(job_id='j',thread_id='t',turn_id='new',source='app_server',state='running',messages={})
            b.jobs['j']=job;b.rpc=AsyncMock(side_effect=[{'thread':{'id':'t','status':{'type':'idle'}}},{'data':rows}])
            await b.status(job)
            self.assertEqual(job['state'],'running');self.assertEqual(job['turn_id'],'new')

    def test_daybreak_is_a_per_turn_program_not_only_metadata(self):
        self.assertEqual(codex.turn_input('t','hi',True)['cyberAccessProgram'],'daybreakBlue')
        self.assertEqual(codex.turn_input('t','hi',False)['cyberAccessProgram'],'standard')
        self.assertNotIn('cyberAccessProgram',codex.turn_input('t','hi'))

    def test_schedule_compact_default_update_and_optout(self):
        with patch.object(tools,'STATE',Path(self.tmp.name)):
            for opt in ({},{'compact':False}):
                saved=tools.schedule(dict(action='enqueue',prompt='check once',**opt))
                self.assertIs(saved['compact'],opt.get('compact',True))
                tools.schedule({'action':'update','id':saved['id'],'compact':False})
                self.assertFalse(tools.schedule({'action':'read','id':saved['id']})['job']['compact'])


if __name__=='__main__':unittest.main()
