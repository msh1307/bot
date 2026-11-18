"""Regression checks for anonymous browser isolation and bounded lifecycle."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('browser', Path(__file__).with_name('web_ui_browser.py'))
b = importlib.util.module_from_spec(spec); spec.loader.exec_module(b)


class Connection:
    def __init__(self): self.closed = False; self.last_used = b.time.monotonic(); self.calls = []
    def request(self, method, params, timeout): self.calls.append(params); return {'text': 'ok'}
    def close(self): self.closed = True


class Tests(unittest.TestCase):
    def setUp(self):
        self.patch = patch.object(b, 'Connection', Connection); self.patch.start()
        self.pool = b.Browsers()
    def tearDown(self): self.pool.close(); self.patch.stop()
    def open(self, session): return self.pool.call({'action': 'open', 'url': 'https://example.com'}, session)

    def test_no_session_no_browser(self):
        for s in (None, '', '../../etc/passwd', 'x' * 81):
            with self.assertRaises(ValueError): self.open(s)
        self.assertFalse(self.pool.sessions)

    def test_cleanup_is_per_conversation(self):
        self.open('chat-one'); self.open('chat-two')
        one, two = self.pool.sessions.values()
        self.pool.cleanup('chat-one')
        self.assertTrue(one.closed); self.assertFalse(two.closed)
        self.assertEqual(list(self.pool.sessions), ['chat-two'])

    def test_no_action_replay_after_disconnect(self):
        self.open('chat-one'); c = self.pool.sessions['chat-one']
        with patch.object(c, 'request', side_effect=TimeoutError) as send:
            with self.assertRaises(TimeoutError): self.pool.call({'action': 'click', 'selector': 'button'}, 'chat-one')
            self.assertEqual(send.call_count, 1)
        self.assertTrue(c.closed)
        with self.assertRaises(ValueError): self.pool.call({'action': 'read'}, 'chat-one')

    def test_capacity_does_not_evict_live_sessions(self):
        for i in range(b.MAX_SESSIONS): self.open('session-' + str(i))
        with self.assertRaises(ValueError): self.open('extra-session')
        self.assertEqual(len(self.pool.sessions), b.MAX_SESSIONS)
        self.assertTrue(all(not c.closed for c in self.pool.sessions.values()))

    def test_stale_context_is_destroyed(self):
        self.open('chat-one'); c = self.pool.sessions['chat-one']
        c.last_used -= b.IDLE_SECONDS + 1
        self.pool.cleanup(); self.assertTrue(c.closed)
        self.assertFalse(self.pool.sessions)

    def test_type_bounds_and_owner_operations_rejected(self):
        for args in ({'action': 'reset'}, {'action': 'credential_fill'},
                     {'action': 'open', 'url': 'x', 'share': True},
                     {'action': 'read', 'char_limit': 1000000},
                     {'action': 'read', 'offset': -1}, {'action': 'wait', 'duration_ms': 999999},
                     {'action': 'click', 'x': 960, 'y': 0}, {'action': 'click'},
                     {'action': 'fill', 'selector': 'input', 'value': 'x' * 8001},
                     {'action': 'read', 'timeout_ms': True}):
            with self.subTest(args=args), self.assertRaises(ValueError): b.validate(args)


if __name__ == '__main__': unittest.main()
