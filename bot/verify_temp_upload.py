"""Loopback-only upload tests; no real credentials, Discord posts or router calls."""
import asyncio
import base64
import hashlib
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import secrets
import socket
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import quote

default_source = Path(__file__).with_name('evo-access.py')
SOURCE = Path(os.environ.get('EVO_ACCESS_SOURCE', default_source if default_source.exists() else '/usr/local/lib/evo-access.py'))
original_read = Path.read_text
def read_config(path, *args, **kwargs):
    if str(path)=='/etc/evo-access/config.json':
        return json.dumps({'public_url':'https://fixture.invalid','admin_uids':[]})
    return original_read(path,*args,**kwargs)
with patch.object(Path,'read_text',read_config):
    spec=importlib.util.spec_from_file_location('upload_under_test',SOURCE)
    service=importlib.util.module_from_spec(spec);spec.loader.exec_module(service)


class Uploads(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        service.UPLOAD_DIR=self.root/'uploads';service.UPLOAD_DIR.mkdir()
        service.KEY_FILE=self.root/'key.json'
        service.GRANT={'id':'0123456789abcdef','api_key':'evo_DUMMY_MASTER',
                       'key_hash':service.digest('evo_DUMMY_MASTER'),'created_at':1,
                       'expires_at':int(time.time())+120,'mode':'upload','cookie_hashes':[]}
        service.UPLOAD_SECRET=secrets.token_bytes(32)
        self.token=service.upload_token(service.GRANT)
        self.server=service.LimitedServer(('127.0.0.1',0),service.PublicHandler)
        service.PUBLIC=None
        service.DOWNLOADS.clear()
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.config=patch.object(Path,'read_text',read_config);self.config.start()

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join()
        service.DOWNLOADS.clear();self.config.stop();self.temp.cleanup()

    def request(self,body=b'test',name='sample.txt',token=None,path='/upload',method='POST',extra=None):
        connection=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=3)
        headers={'Authorization':'Bearer '+(self.token if token is None else token),
                 'X-File-Name':quote(name,safe='')}
        headers.update(extra or {})
        try:
            connection.request(method,path,body=body,headers=headers)
            response=connection.getresponse();return response.status,response.read()
        finally:connection.close()

    def test_binary_unicode_upload_and_group_readability(self):
        data=bytes(range(256))*1000
        status,raw=self.request(data,'한글 파일.bin')
        result=json.loads(raw);self.assertEqual(status,201);self.assertEqual(result['bytes'],len(data))
        path=service.UPLOAD_DIR/Path(result['path']).name
        self.assertTrue(path.name.endswith('-한글 파일.bin'))
        self.assertEqual(hashlib.sha256(path.read_bytes()).digest(),hashlib.sha256(data).digest())
        self.assertEqual(path.stat().st_mode & 0o777,0o660)
        self.assertFalse(list(service.UPLOAD_DIR.glob('*.part')))

    def test_missing_wrong_expired_and_master_keys_are_rejected(self):
        for token in ('','wrong','evo_DUMMY_MASTER'):
            self.assertEqual(self.request(token=token)[0],401)
        service.GRANT['expires_at']=0
        self.assertEqual(self.request()[0],401)
        self.assertEqual(list(service.UPLOAD_DIR.iterdir()),[])

    def test_filename_cannot_escape_the_upload_directory(self):
        for name in ('../outside','/tmp/outside','x\\outside','.','..','\x00bad','x'*181):
            with self.subTest(name=name):self.assertEqual(self.request(name=name)[0],400)
        self.assertEqual(list(service.UPLOAD_DIR.iterdir()),[])

    def test_size_chunking_and_cross_origin_are_rejected(self):
        for headers,status in [({'Content-Length':str(service.MAX_UPLOAD_BYTES+1)},413),
                               ({'Transfer-Encoding':'chunked'},413),
                               ({'Origin':'https://untrusted.invalid'},403)]:
            self.assertEqual(self.request(body=b'',extra=headers)[0],status)
        self.assertEqual(list(service.UPLOAD_DIR.iterdir()),[])

    def test_original_file_is_not_overwritten(self):
        original=service.secrets.token_hex
        with patch.object(service.secrets,'token_hex',lambda n: 'a'*12 if n==6 else original(n)):
            self.assertEqual(self.request(body=b'original')[0],201)
            self.assertEqual(self.request(body=b'replacement')[0],400)
        self.assertEqual((service.UPLOAD_DIR/'aaaaaaaaaaaa-sample.txt').read_bytes(),b'original')
        self.assertEqual(len(list(service.UPLOAD_DIR.iterdir())),1)

    def test_existing_partial_symlink_is_not_followed_or_deleted(self):
        outside=self.root/'outside';outside.write_bytes(b'unchanged')
        link=service.UPLOAD_DIR/('.evo-upload-'+'a'*32+'.part');link.symlink_to(outside)
        original=service.secrets.token_hex
        with patch.object(service.secrets,'token_hex',lambda n: 'a'*(2*n) if n in (6,16) else original(n)):
            self.assertEqual(self.request()[0],400)
        self.assertTrue(link.is_symlink());self.assertEqual(outside.read_bytes(),b'unchanged')

    def test_restart_cleans_only_owned_partial_regular_files(self):
        stale=service.UPLOAD_DIR/('.evo-upload-'+'a'*32+'.part');stale.write_bytes(b'incomplete')
        other=service.UPLOAD_DIR/'user.part';other.write_bytes(b'keep')
        link=service.UPLOAD_DIR/('.evo-upload-'+'b'*32+'.part');link.symlink_to(other)
        service.cleanup_partial_uploads()
        self.assertFalse(stale.exists());self.assertTrue(link.is_symlink());self.assertEqual(other.read_bytes(),b'keep')

    def test_symlinked_destination_directory_is_rejected(self):
        outside=self.root/'elsewhere';outside.mkdir()
        service.UPLOAD_DIR.rmdir();service.UPLOAD_DIR.symlink_to(outside,target_is_directory=True)
        self.assertEqual(self.request()[0],400);self.assertEqual(list(outside.iterdir()),[])

    def test_aborted_upload_removes_partial_file(self):
        sock=socket.create_connection(('127.0.0.1',self.server.server_port),timeout=3)
        request=(f'POST /upload HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {self.token}\r\n'
                 'X-File-Name: aborted.bin\r\nContent-Length: 1000\r\n\r\n').encode()+b'partial'
        sock.sendall(request);sock.shutdown(socket.SHUT_WR)
        while sock.recv(4096):pass
        sock.close();deadline=time.monotonic()+2
        while list(service.UPLOAD_DIR.iterdir()) and time.monotonic()<deadline:time.sleep(.01)
        self.assertEqual(list(service.UPLOAD_DIR.iterdir()),[])

    def test_upload_window_has_no_api_or_file_listing(self):
        for path in ('/v1/models','/v1/chat/completions','/access/login','/uploads','/tools'):
            self.assertEqual(self.request(path=path,method='GET')[0],404)
        service.GRANT['mode']='web'
        self.assertEqual(self.request(path='/v1/models',method='GET')[0],401)
        handler=service.PublicHandler.__new__(service.PublicHandler)
        handler.headers={'Authorization':'Bearer evo_DUMMY_MASTER'};handler.connection=object()
        self.assertEqual(handler.authenticate(),service.GRANT['id'])

    def test_refresh_rotates_upload_key_even_with_the_same_expiry(self):
        old_master=service.GRANT['api_key']
        with patch.object(service,'open_public'),patch.object(service.time,'time',return_value=100000):
            first=service.admin_action({'action':'refresh','mode':'upload','minutes':120})
            key1=service.upload_token(service.GRANT)
            second=service.admin_action({'action':'refresh','mode':'upload','minutes':120})
            key2=service.upload_token(service.GRANT)
        self.assertEqual(first['session']['expires_at'],107200)
        self.assertEqual(first['session']['expires_at'],second['session']['expires_at'])
        self.assertNotEqual(key1,key2);self.assertEqual(service.GRANT['api_key'],old_master)
        self.assertEqual(self.request(token=key1)[0],401)

    def test_expiry_closes_listener_and_keeps_completed_files(self):
        self.assertEqual(self.request()[0],201)
        service.GRANT['expires_at']=time.time()-1
        class StopLoop(Exception):pass
        with patch.object(service.time,'sleep',side_effect=[None,StopLoop]),patch.object(service,'close_public') as close:
            with self.assertRaises(StopLoop):service.expire_loop()
        close.assert_called_once();self.assertEqual(service.GRANT['expires_at'],0)
        self.assertEqual(len(list(service.UPLOAD_DIR.iterdir())),1)

    def test_page_uses_fragment_and_text_nodes_without_external_assets(self):
        status,body=self.request(method='GET',body=None)
        self.assertEqual(status,200);self.assertIn(b'location.hash',body)
        self.assertIn(b'row.textContent',body);self.assertNotIn(b'__MAX_BYTES__',body)
        self.assertNotIn(b'<script src=',body)


class Downloads(unittest.TestCase):
    setUp=Uploads.setUp
    tearDown=Uploads.tearDown
    request=Uploads.request

    def share(self,raw=b'file contents',name='report.md',minutes=120):
        with patch.object(service,'open_public'):
            return service.admin_action({'action':'share','name':name,'data':base64.b64encode(raw).decode(),'minutes':minutes})['files'][0]

    def get(self,file):
        return self.request(path=file['url'].split('fixture.invalid',1)[1],method='GET',body=None)

    def test_real_http_download_matches_bytes_and_preserves_existing_access(self):
        before=dict(service.GRANT);raw=bytes(range(256))*200
        file=self.share(raw,'보고서.md')
        self.assertEqual(self.get(file),(200,raw));self.assertEqual(service.GRANT,before)
        self.assertLessEqual(abs(file['expires_at']-time.time()-7200),2)
        self.assertEqual(list(self.root.iterdir()),[service.UPLOAD_DIR])
        status=service.public_status();self.assertEqual(status['downloads'][0]['bytes'],len(raw))
        self.assertNotIn('/download/',json.dumps(status))

    def test_link_has_no_api_authority_and_cannot_select_arbitrary_paths(self):
        file=self.share();token=file['url'].rsplit('/',1)[1]
        for path in ('/download/wrong','/download/'+token+'/../key.json','/v1/models','/access/login'):
            status,_=self.request(path=path,method='GET',body=None,token=token)
            self.assertEqual(status,404)  # Existing fixture is upload-only.
        self.assertEqual(self.get(file)[0],200)

    def test_expiry_removes_ram_copy_and_does_not_touch_source_files(self):
        file=self.share();key=file['url'].rsplit('/',1)[1]
        service.DOWNLOADS[key]['expires_at']=0
        self.assertEqual(self.get(file)[0],404)
        service.purge_downloads();self.assertFalse(service.DOWNLOADS)

    def test_close_revokes_links_even_without_an_api_credential(self):
        file=self.share();service.GRANT=None
        with patch.object(service,'close_public') as close:
            service.admin_action({'action':'expire'})
        close.assert_called_once();self.assertFalse(service.DOWNLOADS);self.assertEqual(self.get(file)[0],404)

    def test_timer_keeps_files_alive_when_web_window_expires(self):
        file=self.share();service.GRANT['expires_at']=0.1
        class StopLoop(Exception):pass
        with patch.object(service.time,'sleep',side_effect=[None,StopLoop]),patch.object(service,'close_public') as close:
            with self.assertRaises(StopLoop):service.expire_loop()
        close.assert_not_called();self.assertEqual(self.get(file)[0],200)

    def test_last_file_expiry_closes_listener(self):
        file=self.share();service.GRANT=None;service.PUBLIC=object()
        service.DOWNLOADS[file['url'].rsplit('/',1)[1]]['expires_at']=0
        class StopLoop(Exception):pass
        with patch.object(service.time,'sleep',side_effect=[None,StopLoop]),patch.object(service,'close_public') as close:
            with self.assertRaises(StopLoop):service.expire_loop()
        close.assert_called_once();service.PUBLIC=None

    def test_invalid_filename_data_and_duration_are_rejected(self):
        for name in ('../secret','x\\secret','bad\r\nheader','', 'x'*181):
            with self.assertRaises(ValueError):self.share(name=name)
        for minutes in (0,181,True,'120'):
            with self.assertRaises(ValueError):self.share(minutes=minutes)
        with self.assertRaises(ValueError):service.admin_action({'action':'share','name':'x','data':'!not base64'})
        self.assertFalse(service.DOWNLOADS)

    def test_bounded_ram_and_file_count_do_not_evict_existing_links(self):
        with patch.object(service,'MAX_SHARED_BYTES',3):
            file=self.share(raw=b'123')
            with self.assertRaisesRegex(ValueError,'space is full'):self.share(raw=b'4')
        self.assertEqual(self.get(file),(200,b'123'));service.DOWNLOADS.clear()
        for _ in range(8):self.share(raw=b'')
        with self.assertRaisesRegex(ValueError,'space is full'):self.share(raw=b'')

    def test_large_unix_admin_packet_is_read_once_and_downloadable(self):
        sockpath=str(self.root/'admin.sock');server=service.AdminServer(sockpath,service.AdminHandler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        raw=b'large packet\x00'*5000
        try:
            with patch.dict(service.CONFIG,{'admin_uids':[os.getuid()]}),patch.object(service,'open_public'):
                with socket.socket(socket.AF_UNIX) as conn:
                    conn.settimeout(3);conn.connect(sockpath)
                    conn.sendall((json.dumps({'action':'share','name':'large.bin','data':base64.b64encode(raw).decode()})+'\n').encode())
                    answer=b''
                    while b'\n' not in answer:answer+=conn.recv(4096)
            result=json.loads(answer);self.assertTrue(result['success']);self.assertEqual(self.get(result['files'][0]),(200,raw))
        finally:server.shutdown();server.server_close();thread.join()


class Discord(unittest.IsolatedAsyncioTestCase):
    async def test_upload_button_opens_two_hours_and_exposes_only_upload_link(self):
        from evo_bot.integrations import access
        class NoBackground:
            def create_task(self,coro):coro.close()
        native=SimpleNamespace(loop=NoBackground(),add_view=Mock(),tree=SimpleNamespace(add_command=Mock()))
        state={'success':True,'mode':'web','session':None}
        opened={'success':True,'mode':'upload','session':{'expires_at':int(time.time())+7200}}
        credentials={**opened,'api_key':'MASTER_DO_NOT_DISPLAY','upload_url':'https://fixture.invalid/upload#up_DUMMY'}
        def admin(payload):
            if payload['action']=='credentials':return credentials
            if payload['action']=='refresh':return opened
            return state
        with patch.object(access,'CLIENT'),patch.object(access,'LOOP'),patch.object(access,'VIEW_CLASS'),patch.object(access,'admin',side_effect=admin) as calls:
            access.discord_factory(native,SimpleNamespace())
            view=access.VIEW_CLASS()
            interaction=SimpleNamespace(message=SimpleNamespace(content='**EVO 접속 · 닫힘**'),
                user=SimpleNamespace(id=int(access.OWNER)),response=SimpleNamespace(defer=AsyncMock(),send_message=AsyncMock()),
                edit_original_response=AsyncMock(),followup=SimpleNamespace(send=AsyncMock()))
            self.assertTrue(await view.interaction_check(interaction))
            button=next(b for b in view.children if b.custom_id=='evo_access_upload')
            await button.callback(interaction)
            self.assertIn({'action':'refresh','minutes':120,'mode':'upload'},[c.args[0] for c in calls.call_args_list])
            shown=str(interaction.followup.send.call_args)
            self.assertIn('upload#up_DUMMY',shown);self.assertNotIn('MASTER_DO_NOT_DISPLAY',shown)
            interaction.user.id=0
            self.assertFalse(await view.interaction_check(interaction))

    async def test_model_gets_metadata_while_link_goes_privately(self):
        from evo_bot.integrations import access
        opened={'success':True,'mode':'upload','session':{'expires_at':int(time.time())+7200}}
        private={**opened,'api_key':'MASTER','upload_url':'https://fixture.invalid/upload#SECRET'}
        with patch.object(access,'owner_present',return_value=True),patch.object(access,'LOOP',asyncio.get_running_loop()),patch.object(access,'private_web_link',new_callable=AsyncMock) as dm,patch.object(access,'admin',side_effect=[opened,private]) as admin:
            result=json.loads(await asyncio.to_thread(access.manage,{'action':'refresh','mode':'upload'}))
            self.assertEqual(admin.call_args_list[0].args[0],{'action':'refresh','minutes':120,'mode':'upload'})
            dm.assert_awaited_once_with(private)
            self.assertNotIn('SECRET',json.dumps(result));self.assertNotIn('MASTER',json.dumps(result))

    async def test_remote_share_uses_existing_reader_and_model_only_receives_metadata(self):
        from evo_bot.integrations import access,coding
        raw=b'BODY_NOT_FOR_MODEL'
        file={'name':'report.md','bytes':len(raw),'expires_at':int(time.time())+7200,'url':'https://fixture.invalid/download/SECRET'}
        with patch.object(access,'owner_present',return_value=True),patch.object(access,'LOOP',asyncio.get_running_loop()), \
             patch.object(access,'private_web_link',new_callable=AsyncMock) as dm, \
             patch.object(coding,'file_io',return_value={'data':base64.b64encode(raw).decode()}) as read, \
             patch.object(access,'admin',return_value={'success':True,'files':[file]}) as admin:
            result=json.loads(await asyncio.to_thread(access.manage,{'action':'share','instance':'oracle','path':'/tmp/report.md'}))
            read.assert_called_once_with({'instance':'oracle','action':'read','path':'/tmp/report.md'})
            self.assertEqual(admin.call_args.args[0]['minutes'],120)
            self.assertEqual(base64.b64decode(admin.call_args.args[0]['data']),raw)
            dm.assert_awaited_once();self.assertTrue(result['success'])
            self.assertNotIn('SECRET',str(result));self.assertNotIn('BODY_NOT_FOR_MODEL',str(result))
            dm.side_effect=TimeoutError('private transport details')
            result=json.loads(await asyncio.to_thread(access.manage,{'action':'downloads'}))
            self.assertIn('/access',result['error']);self.assertNotIn('private',str(result))

    async def test_file_links_share_the_access_panel_and_no_extra_transfer_action(self):
        from evo_bot.integrations import access
        from evo_bot.tools import Registry
        file={'name':'report.md','bytes':1,'expires_at':int(time.time())+7200,'url':'https://fixture.invalid/download/secret'}
        self.assertIn('파일 공유 중',access.panel_text({'downloads':[file]}))
        self.assertEqual(len(access.web_link_view({'files':[file]}).children),1)
        definitions={d['name']:d for d in Registry().definitions()}
        self.assertEqual(definitions['file_transfer']['parameters']['properties']['action']['enum'],['upload','download'])
        self.assertIn('share',definitions['evo_access_manage']['parameters']['properties']['action']['enum'])

    async def test_stop_during_remote_read_does_not_publish_a_link(self):
        from evo_bot.integrations import access,coding
        with patch.object(access,'owner_present',side_effect=[True,False]), \
             patch.object(coding,'file_io',return_value={'data':'eA=='}),patch.object(access,'admin') as admin:
            result=json.loads(await asyncio.to_thread(access.manage,{'action':'share','instance':'oracle','path':'/tmp/report.md'}))
            self.assertFalse(result['success']);self.assertIn('interrupted',result['error']);admin.assert_not_called()


if __name__=='__main__':unittest.main()
