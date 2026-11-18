"""An executed browser action stays acknowledged even if its state cannot be read."""
import json
import unittest
from unittest.mock import Mock
from evo_bot.browser_worker import Worker
from playwright.sync_api import Error as PlaywrightError


class ActionState(unittest.TestCase):
    def worker(self):
        w=Worker.__new__(Worker)
        w.page=Mock();w.page.url='https://fixture.invalid/'
        w.images_enabled=False;w.http_status=None;w.cache=None
        return w

    def test_failed_state_read_does_not_repeat_or_fail_the_completed_click(self):
        w=self.worker();w.snapshot=Mock(side_effect=RuntimeError('document replaced'))
        result=w.call({'action':'click','selector':'#submit'})
        w.page.locator.return_value.first.click.assert_called_once()
        self.assertTrue(result['success']);self.assertTrue(result['executed'])
        self.assertTrue(json.loads(result['text'])['state_unavailable'])
        self.assertIsNone(result['next_offset'])

    def test_action_response_contains_state_without_another_action(self):
        w=self.worker();w.snapshot=Mock(return_value='{"text":"CHECK_OK"}')
        result=w.call({'action':'click','selector':'#run'})
        w.page.locator.return_value.first.click.assert_called_once()
        self.assertTrue(result['executed']);self.assertIn('CHECK_OK',result['text'])
        w.snapshot.assert_called_once_with('tree',{'action':'click','selector':'#run'},w.page,brief=True)

    def test_read_does_not_claim_to_have_executed_an_action(self):
        w=self.worker();w.snapshot=Mock(return_value='Existing status')
        result=w.call({'action':'read'})
        self.assertNotIn('executed',result)

    def test_screenshot_recovers_one_navigation_race_with_masking(self):
        w=self.worker();w.page.frames=[]
        w.page.screenshot.side_effect=[PlaywrightError('Execution context was destroyed'),b'owned-image']
        result=w.call({'action':'screenshot'})
        self.assertEqual(w.page.screenshot.call_count,2)
        w.page.wait_for_load_state.assert_called_once()
        self.assertTrue(any(b['type']=='image' for b in result['content']))
        for call in w.page.screenshot.call_args_list:self.assertIn('mask',call.kwargs)

    def test_screenshot_mask_failure_has_no_raw_fallback(self):
        w=self.worker();w.page.frames=[];w.page.screenshot.side_effect=PlaywrightError('Mask failed')
        with self.assertRaisesRegex(PlaywrightError,'Mask failed'):w.call({'action':'screenshot'})
        w.page.screenshot.assert_called_once()


if __name__=='__main__':unittest.main()
