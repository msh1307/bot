"""Remote selection, startup fallback, non-replay, and reference routing checks."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
from evo_bot import context
from evo_bot.integrations import browser,remote_config as rc,web_remote


class Checks(unittest.TestCase):
    def test_credentials_can_be_imported_before_browser(self):
        done=subprocess.run([sys.executable,'-c',
            'from evo_bot.integrations import credential_input; from evo_bot.integrations import browser'],
            cwd=Path(__file__).resolve().parent,capture_output=True,text=True)
        self.assertEqual(done.returncode,0,done.stderr)
    def setUp(self):
        self.turn=context.Turn('123')
        self.temp=tempfile.TemporaryDirectory()
        self.profile=patch.object(browser,'PROFILE',Path(self.temp.name)/'profile');self.profile.start()
    def tearDown(self): self.profile.stop();self.temp.cleanup()
    def test_oracle_touch_cancels_old_timer_without_creating_another(self):
        client=browser.Browser();client.instance='oracle';old=client.timer=Mock()
        with patch.object(browser.threading,'Timer') as timer:
            client.touch()
        old.cancel.assert_called_once();timer.assert_not_called();self.assertIsNone(client.timer)
    def test_stale_reaper_cannot_close_oracle(self):
        client=browser.Browser();client.instance='oracle';client.last_used=0
        with patch.object(browser.time,'monotonic',return_value=10000),patch.object(client,'close') as close:
            client.reap()
        close.assert_not_called()
    def test_local_keeps_idle_timer_and_reaper(self):
        client=browser.Browser()
        with patch.object(browser.threading,'Timer') as timer:
            client.touch()
        timer.assert_called_once_with(900,client.reap);timer.return_value.start.assert_called_once()
        client.last_used=0
        with patch.object(browser.time,'monotonic',return_value=10000),patch.object(client,'close') as close:
            client.reap()
        close.assert_called_once()
    def test_idle_status_reports_oracle_persistence_and_local_timeout(self):
        client=browser.Browser();self.assertEqual(client.call({'action':'status'},self.turn)['idle_close_minutes'],15)
        client.instance='oracle';self.assertIsNone(client.call({'action':'status'},self.turn)['idle_close_minutes'])
    def test_remote_is_first_and_local_is_startup_fallback(self):
        client=browser.Browser();calls=[]
        def launch(argv,env,turn):
            calls.append(argv)
            if len(calls)==1: raise ConnectionError('unreachable')
        with patch.object(browser,'remote_launch',return_value=(['ssh'],{},'oracle')), \
             patch.object(browser,'sandbox_argv',return_value=['local']),patch.object(client,'launch',side_effect=launch):
            client.start(self.turn)
        self.assertEqual(calls,[['ssh'],['local']]);self.assertEqual(client.instance,'localhost')
        self.assertEqual(client.fallback,'ConnectionError')
    def test_remote_success_does_not_start_local(self):
        client=browser.Browser()
        with patch.object(browser,'remote_launch',return_value=(['ssh'],{},'oracle')),patch.object(client,'launch') as run:
            client.start(self.turn)
        run.assert_called_once_with(['ssh'],{},self.turn);self.assertEqual(client.instance,'oracle')
    def test_cancelled_start_does_not_fallback(self):
        client=browser.Browser()
        with patch.object(browser,'remote_launch',return_value=(['ssh'],{},'oracle')), \
             patch.object(client,'launch',side_effect=InterruptedError) as run,self.assertRaises(InterruptedError):
            client.start(self.turn)
        self.assertEqual(run.call_count,1)
    def test_disconnected_click_never_replayed(self):
        client=browser.Browser();client.instance='oracle'
        with patch.object(client,'running',return_value=True),patch.object(client,'start') as start, \
             patch.object(client,'request',side_effect=ConnectionError) as run,patch.object(client,'touch'),self.assertRaises(ConnectionError):
            client.call({'action':'click','selector':'#submit'},self.turn)
        self.assertEqual(run.call_count,1);start.assert_called_once()
    def test_missing_session_requires_open(self):
        client=browser.Browser()
        with patch.object(client,'start'),patch.object(client,'request') as run,self.assertRaisesRegex(ValueError,'open'):
            client.call({'action':'press','key':'Enter'},self.turn)
        run.assert_not_called()
    def test_private_urls_select_local(self):
        for url in ('http://localhost/a','http://192.168.219.100','http://[::1]','http://server.local'):
            self.assertTrue(browser.local_url(url))
        self.assertFalse(browser.local_url('https://example.com'))
    def test_config_requires_enabled_ssh_target_and_safe_path(self):
        base={'instances':{'oracle':{'host':'192.0.2.1','ssh':{}}},'browser':{'instance':'oracle','runtime':'/home/user/browser'}}
        rc.validate(base)
        for value in ('/home/user/../secret','/tmp/$(id)','relative'):
            base['browser']['runtime']=value
            with self.assertRaises(ValueError):rc.validate(base)
    def test_local_refs_never_sent_remote(self):
        web_remote.REFS.clear()
        value=web_remote.remember(json.dumps({'ref':'r-test','text':'page'}),'localhost')
        self.assertEqual(json.loads(value)['execution_instance'],'localhost')
        with patch.object(web_remote.CLIENT,'call') as run:
            self.assertIsNone(web_remote.call('web_read',{'ref':'r-test'},self.turn))
        run.assert_not_called()
    def test_remote_refs_do_not_silently_fallback_after_disconnect(self):
        web_remote.REFS['r-test']='oracle'
        with patch.object(web_remote.CLIENT,'call',side_effect=ConnectionError),patch.object(web_remote.CLIENT,'close'), \
             self.assertRaisesRegex(ConnectionError,'original URL'):
            web_remote.call('web_read',{'ref':'r-test'},self.turn)
        web_remote.REFS.clear()
    def test_new_public_search_can_fallback(self):
        with patch.object(web_remote.CLIENT,'call',side_effect=ConnectionError),patch.object(web_remote.CLIENT,'close'):
            self.assertIsNone(web_remote.call('internet_search',{'query':'test'},self.turn))
    def test_provider_failure_falls_back_but_empty_success_does_not(self):
        with patch.object(web_remote.CLIENT,'call',return_value='{"success":false}'):
            self.assertIsNone(web_remote.call('internet_search',{'query':'test'},self.turn))
        with patch.object(web_remote.CLIENT,'call',return_value='{"success":true,"data":{"web":[]}}'):
            self.assertIsNotNone(web_remote.call('internet_search',{'query':'test'},self.turn))


if __name__=='__main__': unittest.main()
