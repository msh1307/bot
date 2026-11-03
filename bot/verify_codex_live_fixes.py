"""Offline regressions for the deployed Pi Codex bridge; no real sends or settings writes."""
import asyncio
import importlib.machinery
import importlib.util
import json
import pathlib
import socket
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock

from evo_bot import context
from evo_bot.integrations import codex, ssh


class OracleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.b = codex.AppServer({'rpc': '/home/opc/bin/codex-rpc'}, 'oracle')
        self.b.jobs = {}; self.b.save = lambda: None
        self.b.monitor_desktop = lambda job: None
        self.turn = context.Turn('77')
        self.token = context.current.set(self.turn)
        self.cfg = patch.object(ssh, 'configuration', return_value={'servers': {'oracle': {}}})
        self.cfg.start()

    def tearDown(self):
        self.cfg.stop()
        context.current.reset(self.token)

    def output(self, result, **kw):
        return json.dumps(dict(exit_code=0, output=json.dumps(result), **kw))

    def rpc(self, result, spec='thread/list', params=None, **kw):
        with patch.object(ssh, 'execute', return_value=self.output(result, **kw)) as transport:
            result = self.b._rpc(spec, params or {}, 10, None)
        self.assertEqual(transport.call_count, 1)
        return result

    def test_nested_error_is_not_success(self):
        with self.assertRaisesRegex(codex.DesktopError, 'thread/resume: thread not loaded'):
            self.rpc([{'error': {'message': 'thread not loaded'}}], '@multi',
                     {'calls': [{'method': 'thread/resume'}, {'method': 'turn/start'}]})

    def test_incomplete_batch(self):
        with self.assertRaisesRegex(codex.DesktopError, 'incomplete batch'):
            self.rpc([{}], '@multi', {'calls': [{'method': 'a'}, {'method': 'b'}]})

    def test_single_error(self):
        with self.assertRaisesRegex(codex.DesktopError, 'denied'):
            self.rpc({'error': {'message': 'denied'}})

    def test_unavailable_query_not_empty_success(self):
        for method in ('thread/list','model/list','thread/read','thread/resume','thread/start','thread/items/list'):
            with self.subTest(method=method), self.assertRaises(codex.DesktopError): self.rpc({}, method)

    def test_exit_error_preserved(self):
        with patch.object(ssh, 'execute', return_value=json.dumps({'exit_code': 2, 'output': 'unavailable'})):
            with self.assertRaisesRegex(codex.DesktopError, 'unavailable') as error:
                self.b._rpc('thread/list', {}, 10, None)
            self.assertEqual(error.exception.code, 'rpc_failed')

    def test_empty_malformed_and_nonobject(self):
        for raw in ('', 'not-json', 'null', '[]'):
            with self.subTest(raw=raw), patch.object(ssh, 'execute', return_value=json.dumps({'exit_code': 0, 'output': raw})):
                with self.assertRaises(codex.DesktopError):
                    self.b._rpc('thread/list', {}, 10, None)

    def test_truncation_not_json_or_success(self):
        with self.assertRaisesRegex(codex.DesktopError, 'transport limit') as error:
            self.rpc({'data': []}, output_truncated=True)
        self.assertEqual(error.exception.code, 'response_too_large')

    def test_timeout_not_replayed(self):
        with patch.object(ssh, 'execute', return_value=self.output({}, timed_out=True)) as send:
            with self.assertRaises(TimeoutError): self.b._rpc('turn/start', {}, 10, None)
        self.assertEqual(send.call_count, 1)

    def test_guard_before_transport(self):
        with patch.object(ssh, 'execute') as send:
            with self.assertRaises(InterruptedError):
                self.b._rpc('thread/list', {}, 10, lambda: (_ for _ in ()).throw(InterruptedError()))
            send.assert_not_called()

    def test_status_metadata_only_and_seconds(self):
        result = [{'thread': {'id': 'chat', 'updatedAt': 1793634656}},
                  {'data': [{'id': 'turn', 'status': 'completed', 'items':
                              [{'type': 'agentMessage', 'text': 'a' * 13000}]}]}]
        with patch.object(self.b, '_rpc', return_value=result) as rpc:
            out = self.b._sync({'action': 'status', 'thread_id': 'chat'}, None)
        self.assertFalse(rpc.call_args.args[1]['calls'][0]['params']['includeTurns'])
        self.assertEqual(out['updated_at'], 1793634656)
        self.assertEqual(len(out['answer']), 12000)
        self.assertTrue(out['answer_truncated'])

    def test_empty_status_and_read_rejected(self):
        for action in ('status', 'read'):
            with self.subTest(action=action), patch.object(self.b, '_rpc', return_value=[{}, {'data': []}]):
                with self.assertRaises(codex.DesktopError):
                    self.b._sync({'action': action, 'thread_id': 'missing'}, None)

    def test_list_seconds_cursor_and_order_unchanged(self):
        rows = [{'id': 'b', 'updatedAt': 1793634656}, {'id': 'a', 'updatedAt': 1793634656}]
        with patch.object(self.b, '_rpc', return_value={'data': rows, 'nextCursor': 'opaque'}) as rpc:
            out = self.b._sync({'action': 'list', 'limit': 12, 'cursor': 'before'}, None)
        self.assertEqual([i['thread_id'] for i in out['threads']], ['b', 'a'])
        self.assertEqual(out['threads'][0]['updated_at'], 1793634656)
        self.assertEqual(out['next_cursor'], 'opaque')
        self.assertEqual(rpc.call_args.args[1]['cursor'], 'before')
        self.assertEqual(rpc.call_args.args[1]['sortKey'], 'updated_at')
        self.assertEqual(codex.desktop_times({'updated_at': 1793634656500})['updated_at'], 1793634657)

    async def test_changed_means_observed_change_policy_omitted(self):
        for initial, expected in (('model-a', None), ('model-b', ['model'])):
            base = [{'approvalPolicy': 'never'}, {'thread': {'id': 'chat', 'model': initial, 'daybreakEnabled': True}}]
            after = [{'approvalPolicy': 'never'}, {'thread': {'id': 'chat', 'model': 'model-a', 'daybreakEnabled': True}}]
            with patch.object(self.b, 'settings_rpc', new_callable=AsyncMock, side_effect=[base, {'data': [{'id': 'model-a', 'modelSpecialty': 'cyber'}]}, [{}, {}], after]) as rpc:
                out = await self.b.native_settings({'action': 'daybreak', 'thread_id': 'chat'}, None)
            self.assertEqual(out['changed'], expected)
            self.assertTrue(out['applied'])
            self.assertNotIn('approvalPolicy', rpc.call_args_list[-2].args[1]['calls'][0]['params'])

    async def test_unapplied_settings_not_changed(self):
        base = [{'approvalPolicy': 'never'}, {'thread': {'id': 'chat', 'model': 'a'}}]
        with patch.object(self.b, 'settings_rpc', new_callable=AsyncMock, side_effect=[base, [{}], base]):
            out = await self.b.native_settings({'action': 'approval', 'thread_id': 'chat', 'policy': 'untrusted'}, None)
        self.assertFalse(out['applied']); self.assertIsNone(out['changed'])
        self.assertNotIn('no client', out.get('note', ''))  # Actual policy remains never.

    async def test_invalid_chat_settings_never_write(self):
        with patch.object(self.b,'settings_rpc',new_callable=AsyncMock,return_value=[{},{}]) as rpc:
            with self.assertRaises(codex.DesktopError):
                await self.b.native_settings({'action':'approval','thread_id':'missing','policy':'never'},None)
        self.assertEqual(rpc.call_count,1)

    async def test_daybreak_off_keeps_native_model(self):
        before=[{'approvalPolicy':'never'},{'thread':{'id':'chat','model':'cyber'}}]
        after=before
        with patch.object(self.b,'settings_rpc',new_callable=AsyncMock,side_effect=[before,[{}],after]) as rpc:
            out=await self.b.native_settings({'action':'daybreak','mode':'off','thread_id':'chat'},None)
        self.assertEqual(out['model'],'cyber')
        methods=[c['method'] for c in rpc.call_args_list[-2].args[1]['calls']]
        self.assertNotIn('thread/settings/update',methods)
        self.assertNotIn('model/list',[call.args[0] for call in rpc.call_args_list])

    async def test_daybreak_off_explicit_model_still_validated(self):
        before=[{'approvalPolicy':'never'},{'thread':{'id':'chat','model':'cyber'}}]
        with patch.object(self.b,'settings_rpc',new_callable=AsyncMock,side_effect=[before,{'data':[{'id':'offered'}]}]) as rpc:
            with self.assertRaisesRegex(ValueError,'offers no model'):
                await self.b.native_settings({'action':'daybreak','mode':'off','thread_id':'chat','model':'missing'},None)
        self.assertEqual(rpc.call_count,2)

    async def test_sandbox_only_does_not_set_approval_policy(self):
        resumed={'approvalPolicy':'untrusted','sandbox':{'type':'readOnly'}}
        before=[resumed,{'thread':{'id':'chat','model':'a'}}]
        with patch.object(self.b,'settings_rpc',new_callable=AsyncMock,side_effect=[before,[{}],before]) as rpc:
            out=await self.b.native_settings({'action':'approval','thread_id':'chat','sandbox':'read-only'},None)
        self.assertNotIn('approvalPolicy',rpc.call_args_list[-2].args[1]['calls'][0]['params'])
        self.assertTrue(out['applied']);self.assertIsNone(out['changed'])
        self.assertIn('untrusted',out['note'])

    async def test_scheduled_existing_send_and_read(self):
        self.turn.cron = True
        responses = [{'thread': {'id': 'chat'}}, {'turn': {'id': 'turn'}}]
        with patch.object(self.b, 'rpc', new=AsyncMock(side_effect=responses)) as send:
            out = await self.b.action({'action': 'send', 'thread_id': 'chat', 'prompt': 'check'}, '77')
        self.assertEqual(out['turn_id'], 'turn'); self.assertEqual(send.call_count, 2)
        with patch.object(ssh, 'execute', return_value=self.output({'data': []})):
            self.assertEqual((await self.b.action({'action': 'list'}, '77'))['threads'], [])

    async def test_scheduled_creation_privilege_changes_and_nonowner_denied(self):
        self.turn.cron = True
        with patch.object(ssh, 'execute') as send:
            for args in ({'action': 'send', 'prompt': 'new'}, {'action': 'stop'}, {'action': 'approval'}, {'action': 'exec'}, {'action': 'start'}):
                with self.assertRaises(PermissionError): await self.b.action(args, '77')
            self.turn.user = 'not-owner'
            with self.assertRaises(PermissionError): await self.b.action({'action': 'list'}, '77')
            send.assert_not_called()

    async def test_cancelled_worker_does_not_overlap_or_replay(self):
        started, release = threading.Event(), threading.Event()
        calls = []
        def run(args, check):
            calls.append(args['n']); started.set()
            if args['n'] == 1:
                release.wait(2); check()
            return {}
        with patch.object(self.b, '_sync', side_effect=run):
            first = asyncio.create_task(self.b.action({'action': 'list', 'n': 1}, '77'))
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            first.cancel()
            with self.assertRaises(asyncio.CancelledError): await first
            second = asyncio.create_task(self.b.action({'action': 'list', 'n': 2}, '77'))
            await asyncio.sleep(.15); self.assertEqual(calls, [1])
            release.set(); await second
            self.assertEqual(calls, [1, 2])


class DesktopTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_initialization_closes_socket_and_reader(self):
        b = codex.Bridge.__new__(codex.Bridge)
        b.ws = None; b.initialized = False; b.connect_lock = asyncio.Lock()
        b.settings = {'ca': 'unused', 'url': 'wss://unused', 'token': 'test'}
        entered = asyncio.Event(); closed = []
        async def receive(ws):
            await asyncio.Future()
        async def initialize(*args, **kw):
            entered.set(); await asyncio.Future()
        async def close(): closed.append(True)
        ws = SimpleNamespace(close=close)
        async def connect(*args, **kw): return ws
        b.receive, b.rpc = receive, initialize
        with patch('ssl.create_default_context', return_value=SimpleNamespace()), \
             patch('websockets.asyncio.client.connect', connect):
            task = asyncio.create_task(b.connect()); await entered.wait(); task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
        self.assertTrue(closed); self.assertIsNone(b.ws)
        self.assertFalse(b.initialized); self.assertTrue(b.reader.done())


class WireTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = pathlib.Path(__file__).resolve().parent
        deploy = root / 'deploy' if (root / 'deploy').is_dir() else root.parent / 'deploy'
        path = str(deploy / 'codex-server/linux/codex-rpc.py')
        loader = importlib.machinery.SourceFileLoader('oracle_rpc', path)
        spec = importlib.util.spec_from_loader(loader.name, loader)
        cls.rpc = importlib.util.module_from_spec(spec); loader.exec_module(cls.rpc)

    def wire(self, data):
        client, server = socket.socketpair(); self.addCleanup(client.close); self.addCleanup(server.close)
        w = self.rpc.WS.__new__(self.rpc.WS); w.s=client; w.buf=data
        w.deadline = time.monotonic()+1
        return w, server

    def test_ping_pong_payload_and_pong_ignored(self):
        w, server = self.wire(b'\x89\x02ok\x8a\x00\x81\x02{}')
        self.assertEqual(w.text(w.deadline), {})
        raw = server.recv(128)
        self.assertEqual(raw[0] & 15, 10); self.assertTrue(raw[1] & 128)
        self.assertEqual(bytes(v ^ raw[2+i%4] for i,v in enumerate(raw[6:])), b'ok')

    def test_fragmented_text_with_control_frame(self):
        w, _ = self.wire(b'\x01\x01{\x8a\x00\x80\x01}')
        self.assertEqual(w.text(w.deadline), {})

    def test_close_and_oversize_rejected(self):
        for data, error in ((b'\x88\x00', ConnectionError), (b'\x81\x7f' + (9*2**20).to_bytes(8,'big'), RuntimeError)):
            with self.subTest(data=data):
                w, _ = self.wire(data)
                with self.assertRaises(error): w.frame(w.deadline)

    def test_expired_read_does_not_consume_bytes(self):
        w, server = self.wire(b''); server.sendall(b'a')
        with self.assertRaises(TimeoutError): self.rpc.readn(w.s, 1, time.monotonic()-1)
        self.assertEqual(w.s.recv(1), b'a')

    def test_handshake_eof_is_bounded(self):
        w, server = self.wire(b''); server.shutdown(socket.SHUT_WR)
        with self.assertRaises(SystemExit): self.rpc.WS(w.s, time.monotonic()+.1)

    def test_spawn_detached_without_wait(self):
        with patch('builtins.open', unittest.mock.mock_open()), patch.object(self.rpc.subprocess, 'Popen') as start:
            self.rpc.spawn()
        self.assertTrue(start.call_args.kwargs['start_new_session'])
        self.assertEqual(start.call_count, 1)

    def test_concurrent_cold_start_launches_once(self):
        from concurrent.futures import ThreadPoolExecutor
        with tempfile.TemporaryDirectory() as directory:
            path = str(pathlib.Path(directory)/'rpc.sock')
            listener=socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            launches=[]
            def spawn():
                launches.append(1)
                def ready():
                    time.sleep(.05); listener.bind(path); listener.listen(4)
                threading.Thread(target=ready).start()
                return SimpleNamespace(poll=lambda:None)
            clients=[]
            try:
                with patch.object(self.rpc,'SOCK',path), patch.object(self.rpc,'spawn',spawn), ThreadPoolExecutor(2) as pool:
                    futures=[pool.submit(self.rpc.connect,time.monotonic()+2) for _ in range(2)]
                    clients=[f.result(3) for f in futures]
                self.assertEqual(launches,[1]); self.assertEqual(len(clients),2)
            finally:
                for s in clients:s.close()
                listener.close()

    def test_silent_handshake_obeys_deadline(self):
        w, _ = self.wire(b'')
        with self.assertRaises(TimeoutError): self.rpc.WS(w.s,time.monotonic()+.02)

    def test_batch_error_stops_later_mutation(self):
        sent=[]
        class FakeWS:
            def __init__(self, *a): pass
            def send(self, obj): sent.append(obj)
            def text(self, deadline):
                ident=sent[-1].get('id')
                return {'id':ident, **({'result':{}} if ident==1 else {'error':{'message':'missing'}})}
        calls={'calls':[{'method':'thread/resume'},{'method':'turn/start'}]}
        with patch.object(self.rpc,'WS',FakeWS), patch.object(self.rpc,'connect'), \
             patch('sys.argv',['codex-rpc','@multi',json.dumps(calls),'5']), patch('builtins.print'):
            self.rpc.main()
        self.assertEqual([s['method'] for s in sent], ['initialize','initialized','thread/resume'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
