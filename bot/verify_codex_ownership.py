"""GUI/daemon ownership regressions; synthetic RPCs, no prompts or real chats."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from evo_bot.integrations import codex


class OwnershipTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = patch.object(codex, 'STATE', Path(self.tmp.name) / 'jobs.json'); self.patch.start()
        self.b = codex.Bridge({'url':'wss://fixture', 'token':'test', 'ca':'fixture', 'cwd':'C:\\fixture'}, 'fixture')
        self.job = dict(job_id='j', thread_id='thread', turn_id='turn', source='app_server',
                        state='completed', messages={}, _wake=False)
        self.b.jobs['j'] = self.job
        self.b.monitor_desktop = lambda job: None

    def tearDown(self):
        self.patch.stop(); self.tmp.cleanup()

    def rpc(self, *, goal=None, status='idle', loaded=True):
        async def call(method, params, **kwargs):
            if method == 'thread/loaded/list': return {'data':['thread'] if loaded else []}
            self.assertEqual(params['threadId'], 'thread')
            if method == 'thread/read': return {'thread':{'id':'thread', 'status':{'type':status}}}
            if method == 'thread/goal/get': return {'goal':goal}
            self.assertEqual(method, 'thread/unsubscribe'); return {'status':'unsubscribed'}
        self.b.rpc = AsyncMock(side_effect=call)

    async def test_finished_task_releases_only_its_own_thread(self):
        self.rpc(); await self.b.release_idle(self.job)
        self.assertEqual([c.args[0] for c in self.b.rpc.call_args_list],
                         ['thread/loaded/list','thread/read','thread/goal/get','thread/unsubscribe'])

    async def test_active_goal_and_active_turn_keep_their_owner(self):
        for goal, status in (({'status':'active'},'idle'), (None,'active')):
            self.rpc(goal=goal,status=status); await self.b.release_idle(self.job)
            self.assertNotIn('thread/unsubscribe',[c.args[0] for c in self.b.rpc.call_args_list])
        self.job['state']='running'; self.rpc(); await self.b.release_idle(self.job)
        self.b.rpc.assert_not_called()

    async def test_paused_goal_releases_and_unloaded_task_never_resumes(self):
        self.rpc(goal={'status':'paused'}); await self.b.release_idle(self.job)
        self.assertEqual(self.b.rpc.call_args_list[-1].args[0], 'thread/unsubscribe')
        self.rpc(loaded=False); await self.b.release_idle(self.job)
        self.assertEqual([c.args[0] for c in self.b.rpc.call_args_list], ['thread/loaded/list'])

    async def test_gui_takeover_reuses_chat_and_never_sends_to_daemon(self):
        self.b.rpc=AsyncMock(side_effect=AssertionError('Native command must not run'))
        async def desktop(args, **kwargs):
            if args['action']=='inspect':return {'threads':[{'thread_id':'thread','gui_attached':True}]}
            self.assertEqual(args['thread_id'],'thread'); self.assertEqual(args['action'],'send')
            return {'turn_id':'gui-turn','mode':'start_turn','message':'accepted'}
        self.b.desktop=AsyncMock(side_effect=desktop)
        self.b.arm_callback=lambda *args:None
        result=await self.b.action({'action':'send','job_id':'j','prompt':'authorized followup'},'77')
        self.assertEqual(result['job_id'],'j'); self.assertEqual(self.job['source'],'desktop')
        self.assertEqual(len(self.b.jobs),1); self.b.rpc.assert_not_called()

    async def test_no_owner_keeps_native_route_but_ambiguous_owner_blocks(self):
        self.b.desktop=AsyncMock(return_value={'threads':[{'thread_id':'thread','error_code':'no_gui_owner'}]})
        self.assertFalse(await self.b.prefer_gui(self.job))
        for row in ({'thread_id':'thread','error_code':'snapshot_unavailable'},
                    {'thread_id':'different','gui_attached':True}):
            self.b.desktop=AsyncMock(return_value={'threads':[row]})
            with self.assertRaises(codex.DesktopError):await self.b.prefer_gui(self.job)
        self.assertEqual(self.job['source'],'app_server')

    async def test_gui_goal_command_never_resumes_a_second_daemon_copy(self):
        self.b.desktop=AsyncMock(return_value={'threads':[{'thread_id':'thread','gui_attached':True}]})
        self.b.rpc=AsyncMock(return_value={'thread':{'id':'thread'}})
        with self.assertRaisesRegex(codex.DesktopError,'desktop app'):
            await self.b.native_slash({'action':'_goal','thread_id':'thread','operation':'get'},None)
        self.assertEqual([c.args[0] for c in self.b.rpc.call_args_list],['thread/read'])


if __name__ == '__main__': unittest.main(verbosity=2)
