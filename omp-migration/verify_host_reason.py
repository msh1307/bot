"""Host reason/approval/hash regressions; no real commands or Discord messages."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from evo_omp import context
from evo_omp.integrations import host

source = os.environ.get('EVO_HOST_BROKER_SOURCE', '/usr/local/lib/evo-host/evo-host-broker.py')
spec = importlib.util.spec_from_file_location('reason_test_broker', source)
broker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(broker)


class Checks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        broker.POLICY = {'users': ['evo-hermes', 'root'], 'max_timeout': 120,
                         'workspace': str(self.root), 'private_paths': [],
                         'runtime': str(self.root), 'approval_ttl': 300}
        broker.TICKETS.clear(); broker.AUDIT.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def test_unprivileged_reason_optional(self):
        for extra in ({}, {'run_as': 'evo-hermes'}, {'reason': ''}, {'reason': '   '}):
            self.assertEqual(broker.validate(dict(command='true', **extra))['reason'], '')

    def test_root_reason_required(self):
        for extra in ({}, {'reason': ''}, {'reason': '   '}):
            with self.assertRaisesRegex(ValueError, 'Root commands require a reason'):
                broker.validate(dict(command='true', run_as='root', **extra))
        self.assertEqual(broker.validate({'command': 'true', 'run_as': 'root',
                                         'reason': ' root purpose '})['reason'], 'root purpose')

    def test_invalid_reason_rejected_for_both_users(self):
        for who in ('root', 'evo-hermes'):
            for reason in (None, 1, 'x' * 161, 'line\nbreak', 'hidden\u202e'):
                with self.assertRaises(ValueError):
                    broker.validate({'command': 'true', 'run_as': who, 'reason': reason})

    def test_hash_and_ticket_binding(self):
        ticket = broker.stage({'command': 'true', 'run_as': 'root', 'reason': 'purpose'})
        expected = hashlib.sha256(json.dumps(ticket['payload'], sort_keys=True,
            ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
        self.assertEqual(ticket['digest'], expected)
        changed = dict(ticket['payload'], reason='different purpose')
        with self.assertRaisesRegex(ValueError, 'does not match'):
            broker.consume(dict(ticket, digest=broker.digest(changed)))
        self.assertIn(ticket['request_id'], broker.TICKETS)
        _, record = broker.consume(ticket)
        self.assertEqual(record['payload']['reason'], 'purpose')
        with self.assertRaisesRegex(ValueError, 'already consumed'):
            broker.consume(ticket)

    def test_audit_fields_and_no_plaintext(self):
        broker.stage({'command': 'printf PRIVATE_COMMAND_MARKER', 'reason': 'PRIVATE_REASON_MARKER'})
        text = (self.root/'audit.jsonl').read_text()
        self.assertEqual(set(json.loads(text)), {'time','request_id','digest','run_as','outcome'})
        self.assertNotIn('PRIVATE_COMMAND_MARKER', text)
        self.assertNotIn('PRIVATE_REASON_MARKER', text)

    def test_host_is_an_internal_approval_backend(self):
        definitions = {}
        registry = SimpleNamespace(register_platform_handler=lambda *_: None,
            register_tool=lambda name, group, definition, handler: definitions.update({name: definition}))
        host.register(registry)
        self.assertNotIn('host_command', definitions)

    def test_root_card_and_execution_key_preserved(self):
        turn = context.Turn('77')
        calls = []
        def rpc(request, *args):
            calls.append(request['action'])
            if request['action'] == 'stage':
                return dict(success=True, request_id='fixture', token='fixture', digest='fixture',
                            payload=broker.validate(request['payload']))
            return dict(success=True, output='synthetic root result')
        token = context.current.set(turn)
        try:
            with patch.object(context, 'client', SimpleNamespace(is_ready=lambda: True)), \
                    patch.object(host, 'discord_connected', return_value=True), \
                    patch.object(host, 'rpc', side_effect=rpc), \
                    patch.object(host, 'approve_root_once', return_value=None) as approval, \
                    patch.object(host, 'remember_root_execution'):
                reply = json.loads(host.host_command({'command': 'true', 'run_as': 'root', 'reason': 'purpose'}))
                self.assertTrue(reply['success'], reply)
                args = approval.call_args.args
                self.assertIs(args[0], turn)
                self.assertIn('# EVO root; cwd=', args[1])
                self.assertEqual(args[2], 'Root access: purpose (this command only)')
                payload = broker.validate({'command': 'true', 'run_as': 'root', 'reason': 'purpose'})
                self.assertEqual(args[3], host.root_command_key(turn.key, turn.id, payload))
                self.assertEqual(calls, ['stage', 'execute'])
        finally:
            context.current.reset(token)


if __name__ == '__main__': unittest.main()
