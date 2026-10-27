"""Synthetic authorization/routing checks. No live prompts, files or commands."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).parent))
import evo_access_manage as access
from evo_omp import management
from evo_omp.integrations import codex


class Handler:
    def __init__(self, path, data=None, cookie='', origin=True):
        self.path, self.data, self.connection = path, data, object()
        self.headers = {'Content-Type': 'application/json', 'Content-Length': str(len(json.dumps(data or {}).encode())),
                        'Cookie': cookie, 'Authorization': 'Bearer shared-llm-key'}
        self.origin = origin
    def origin_ok(self): return self.origin
    def body(self): return json.dumps(self.data or {}).encode()
    def respond(self, status, body, mime=None, cookie=None): return status, body, cookie


def access_checks(key):
    broker = access.ManagementAccess()
    state = {'public_url': 'https://owner.example', 'session': {'id': 'grant', 'expires_at': int(time.time()) + 3600}}
    h = Handler('/manage/api/servers')
    with patch.object(access, 'KEY_PATH', key):
        assert broker.handle(h, 'GET', state, {}, broker.lock)[0] == 401
        link = broker.issue(state)
        token = link['url'].split('#')[1]
        h = Handler('/manage/login', {'token': token}, origin=False)
        assert broker.handle(h, 'POST', state, {}, broker.lock)[0] == 403
        h.origin = True
        result = broker.handle(h, 'POST', state, {}, broker.lock, secure=True)
        assert result[0] == 200 and 'HttpOnly' in result[2] and 'SameSite=Strict' in result[2] and 'Secure' in result[2]
        cookie = result[2].split(';')[0]
        assert broker.handle(h, 'POST', state, {}, broker.lock)[0] == 401, 'One-use link replayed'
        h = Handler('/manage/logout', {}, cookie=cookie)
        assert broker.handle(h, 'POST', state, {}, broker.lock)[0] == 200
        assert broker.handle(Handler('/manage/api/servers', cookie=cookie), 'GET', state, {}, broker.lock)[0] == 401
        link = broker.issue(state)
        h = Handler('/manage/login', {'token': link['url'].split('#')[1]})
        cookie = broker.handle(h, 'POST', state, {}, broker.lock)[2].split(';')[0]
        broker.clear()
        assert broker.handle(Handler('/manage/api/servers', cookie=cookie), 'GET', state, {}, broker.lock)[0] == 401
        for _ in range(15): broker.issue(state)
        assert len(broker.links) <= 8
    print('PASS separate owner login, shared-key rejection, origin check, one-use links, cookie flags, logout/close revocation, bounded sessions')


async def backend_checks(key):
    calls = []
    class Fake:
        def __init__(self, ident):
            self.server_id = ident
            self.settings = {'name': ident, 'url': f'wss://{ident}.example:4500'}
        async def action(self, args, channel, check=None):
            check();calls.append((self.server_id, args))
            return {'echo': args['action']}
        async def read_history(self, thread_id, cursor=None, check=None, include_live=True):
            check();assert not include_live;calls.append((self.server_id, {'read': thread_id}))
            return {'recent_messages': [{'role': 'assistant', 'text': 'synthetic'}]}
    previous = codex.BRIDGE, dict(codex.SERVERS)
    codex.SERVERS.clear();codex.SERVERS.update({name: Fake(name) for name in ('desktop-1', 'desktop-2')})
    codex.BRIDGE = codex.SERVERS['desktop-1']
    try:
        with patch.object(management, 'KEY_PATH', key): backend = management.ManagementServer()
        app = web.Application(client_max_size=65536)
        app.router.add_get('/manage/api/servers', backend.handle)
        app.router.add_post('/manage/api/call', backend.handle)
        async with TestClient(TestServer(app)) as client:
            assert (await client.get('/manage/api/servers')).status == 401
            headers = {'X-Evo-Manage-Key': key.read_text().strip()}
            r = await client.get('/manage/api/servers', headers=headers)
            body = await r.json();assert len(body['servers']) == 2 and 'token' not in str(body)
            r = await client.post('/manage/api/call', json={'action': 'list', 'server': 'desktop-2'}, headers=headers)
            assert r.status == 200 and calls[-1][0] == 'desktop-2'
            r = await client.post('/manage/api/call', json={'action': 'read', 'thread_id': 'existing'}, headers=headers)
            assert r.status == 200 and calls[-1][0] == 'desktop-1'
            r = await client.post('/manage/api/call', json={'action': 'send', 'server': 'desktop-2', 'thread_id': 'existing', 'prompt': 'synthetic'}, headers=headers)
            assert r.status == 200 and calls[-1][1]['_silent'] is True
            before = len(calls)
            for args in ({'action': 'list', 'server': 'missing'}, {'action': 'new'}, {'action': 'root'}):
                assert (await client.post('/manage/api/call', json=args, headers=headers)).status == 400
            assert len(calls) == before
        print('PASS internal authentication, per-server routing, stored-only reads, silent UI sends, unknown-server/action rejection')
    finally:
        codex.BRIDGE = previous[0];codex.SERVERS.clear();codex.SERVERS.update(previous[1])


def config_checks(root):
    config = root / 'servers.json'
    settings = {'url': 'wss://desktop.example:4500', 'token': 'synthetic' * 6, 'ca': '/private/ca.pem', 'cwd': 'C:\\Work'}
    with patch.object(codex, 'CONFIG', config), patch.object(codex, 'STATE', root / 'jobs.json'):
        config.write_text(json.dumps(settings))
        assert codex.Bridge().server_id == 'msh-desktop'
        config.write_text(json.dumps({'default': 'desk-2', 'servers': {'desk-1': settings, 'desk-2': settings}}))
        assert codex.Bridge().server_id == 'desk-2'
        assert codex.Bridge().state_path.name == 'codex-jobs-desk-2.json'
        assert codex.Bridge(settings, 'desk-1', False).state_path.name == 'codex-jobs-desk-1.json'
        config.write_text(json.dumps({'default': 'missing', 'servers': {'desk-1': settings}}))
        try: codex.server_config();raise AssertionError('Missing default accepted')
        except ValueError: pass
    print('PASS old/new private config and isolated server job state')


if __name__ == '__main__':
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory);key = root / 'key';key.write_text('synthetic-internal-key-' + 'x' * 40)
        access_checks(key);config_checks(root);asyncio.run(backend_checks(key))
