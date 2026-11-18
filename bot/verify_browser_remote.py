"""Oracle browser connection, non-replay, and web-reference routing checks."""
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
from evo_bot import context
from evo_bot.integrations import browser,remote_config as rc,web_remote


class Checks(unittest.TestCase):
    def test_default_action_gets_the_full_single_call_timeout(self):
        client=browser.Browser();client.instance='oracle'
        turn=context.Turn('123')
        with patch.object(client,'running',return_value=True),patch.object(client,'start'),patch.object(client,'touch'), \
             patch.object(client,'request',return_value={'success':True}) as request:
            client.call({'action':'click','selector':'#ready'},turn)
        self.assertEqual(request.call_args.args[1]['timeout_ms'],60000)

    def test_credentials_can_be_imported_before_browser(self):
        done=subprocess.run([sys.executable,'-c',
            'from evo_bot.integrations import credential_input; from evo_bot.integrations import browser'],
            cwd=Path(__file__).resolve().parent,capture_output=True,text=True)
        self.assertEqual(done.returncode,0,done.stderr)
    def setUp(self):
        self.turn=context.Turn('123')
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
        client=browser.Browser();client.instance='localhost'  # Shared transport also serves terminals.
        with patch.object(browser.threading,'Timer') as timer:
            client.touch()
        timer.assert_called_once_with(900,client.reap);timer.return_value.start.assert_called_once()
        client.last_used=0
        with patch.object(browser.time,'monotonic',return_value=10000),patch.object(client,'close') as close:
            client.reap()
        close.assert_called_once()
    def test_idle_status_reports_oracle_persistence_and_local_timeout(self):
        client=browser.Browser();client.instance='localhost'
        self.assertEqual(client.call({'action':'status'},self.turn)['idle_close_minutes'],15)
        client.instance='oracle';self.assertIsNone(client.call({'action':'status'},self.turn)['idle_close_minutes'])
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
        with patch.object(client,'start') as start,patch.object(client,'request') as run,self.assertRaisesRegex(ValueError,'open'):
            client.call({'action':'press','key':'Enter'},self.turn)
        run.assert_not_called();start.assert_not_called()

    def test_transport_timeout_explains_closed_connection_without_replay(self):
        client=browser.Browser()
        with patch.object(client,'running',return_value=True),patch.object(client,'start'), \
             patch.object(client,'close') as close,patch.object(client,'touch'), \
             patch.object(client,'request',side_effect=TimeoutError) as request, \
             self.assertRaisesRegex(TimeoutError,'connection closed.*may have run.*not replayed'):
            client.call({'action':'click','selector':'#confirm'},self.turn)
        close.assert_called_once();request.assert_called_once()

    def test_screenshot_context_is_inside_model_visible_content(self):
        client=browser.Browser();image={'type':'image','data':'synthetic','mimeType':'image/jpeg'}
        with patch.object(client,'running',return_value=True),patch.object(client,'start'),patch.object(client,'touch'), \
             patch.object(client,'request',return_value={'url':'https://fixture.invalid/','content':[image]}):
            value=client.call({'action':'screenshot'},self.turn)
        self.assertIn('oracle',value['content'][0]['text'])
        self.assertIn('https://fixture.invalid/',value['content'][0]['text'])
        self.assertIs(value['content'][1],image)
    def test_frame_ref_forwarded_without_reopening_or_closing(self):
        client=browser.Browser();client.instance='oracle'
        with patch.object(client,'running',return_value=True),patch.object(client,'start'), \
             patch.object(client,'request',return_value={'success':True}) as request, \
             patch.object(client,'touch'),patch.object(client,'close') as close:
            for turn in (self.turn,context.Turn('123')):
                client.call({'action':'click','frame':'f1','selector':'#confirm'},turn)
        self.assertEqual(request.call_count,2)
        self.assertEqual(request.call_args.args[1]['frame'],'f1')
        close.assert_not_called()
    def test_invalid_frame_scope_fails_before_execution(self):
        client=browser.Browser()
        for args in ({'action':'read','frame':''},{'action':'read','frame':1},
                     {'action':'open','url':'https://example.test','frame':'f1'},
                     {'action':'click','frame':'f1','x':1,'y':1},
                     {'action':'press','frame':'f1','key':'Enter'},
                     {'action':'screenshot','frame':'f1'}):
            with patch.object(client,'start') as start,self.assertRaises(ValueError):
                client.call(args,self.turn)
            start.assert_not_called()
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


class OracleBrowserChecks(unittest.TestCase):
    def setUp(self):
        self.turn=context.Turn('123')

    def test_oracle_is_the_current_default(self):
        status=browser.Browser().call({'action':'status'},self.turn)
        self.assertEqual((status['instance'],status['engine']),('oracle','Camoufox'))
        self.assertIsNone(status['idle_close_minutes'])

    def test_unavailable_oracle_does_not_start_evo(self):
        client=browser.Browser()
        with patch.object(browser,'remote_launch',return_value=(['ssh'],{},'oracle')), \
             patch.object(client,'launch',side_effect=ConnectionError('unreachable')) as launch, \
             patch.object(browser,'sandbox_argv') as local, \
             self.assertRaisesRegex(ConnectionError,'unreachable'):
            client.start(self.turn)
        launch.assert_called_once_with(['ssh'],{},self.turn)
        local.assert_not_called()

    def test_missing_configuration_does_not_start_evo(self):
        with patch.object(browser,'remote_launch',return_value=None), \
             patch.object(browser,'sandbox_argv') as local, \
             self.assertRaisesRegex(ConnectionError,'not configured'):
            browser.Browser().start(self.turn)
        local.assert_not_called()

    def test_missing_configuration_does_not_claim_reset(self):
        with patch.object(browser,'remote_launch',return_value=None), \
             self.assertRaisesRegex(ConnectionError,'not configured'):
            browser.Browser().call({'action':'reset'},self.turn)

    def test_all_urls_keep_the_same_oracle_session(self):
        client=browser.Browser()
        urls=('http://localhost/a','http://127.0.0.1:8097','http://[::1]',
              'http://192.168.219.100','http://server.local','https://example.com')
        with patch.object(client,'running',return_value=True),patch.object(client,'start') as start, \
             patch.object(client,'close') as close,patch.object(client,'touch'), \
             patch.object(client,'request',side_effect=lambda method,args,*_: {'url':args['url'],'success':True}) as request:
            for url in urls:
                result=client.call({'action':'open','url':url},self.turn)
                self.assertEqual(result['instance'],'oracle')
                self.assertEqual(start.call_args.args,(self.turn,))
                self.assertEqual(start.call_args.kwargs,{})
        close.assert_not_called();self.assertEqual(request.call_count,len(urls))

    def test_failed_private_navigation_is_not_replayed_elsewhere(self):
        client=browser.Browser()
        with patch.object(client,'running',return_value=True),patch.object(client,'start') as start, \
             patch.object(client,'close') as close,patch.object(client,'touch'), \
             patch.object(client,'request',side_effect=ValueError('ERR_CONNECTION_REFUSED')) as request, \
             self.assertRaisesRegex(ValueError,'ERR_CONNECTION_REFUSED'):
            client.call({'action':'open','url':'http://127.0.0.1:8097'},self.turn)
        start.assert_called_once_with(self.turn)
        request.assert_called_once();close.assert_not_called()

    def test_model_description_matches_oracle_only_routing(self):
        registry=Mock();browser.register(registry)
        description=registry.register_tool.call_args.args[2]['description']
        self.assertIn('including localhost/private IPs',description)
        self.assertIn('Oracle Camoufox browser over SSH',description)
        self.assertNotIn('URLs use EVO',description)


if __name__=='__main__': unittest.main()
