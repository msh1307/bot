"""Small on-demand Windows/Codex UI; no model calls for browsing or file access."""
import asyncio
from pathlib import Path
import secrets
import time

from aiohttp import web

KEY_PATH = Path('/etc/evo-access/manage-token')
ACTIONS = {'list', 'read', 'status', 'send', 'stop', 'projects', 'models', 'file_list', 'file_read', 'exec'}


class ManagementServer:
    def __init__(self):
        self.runner = None
        self.key = KEY_PATH.read_text().strip()
        if len(self.key) < 40: raise ValueError('Invalid management backend key')
        self.gate = asyncio.Semaphore(2)

    async def handle(self, request):
        if not secrets.compare_digest(request.headers.get('X-Evo-Manage-Key', '').encode(), self.key.encode()):
            raise web.HTTPUnauthorized()
        from .integrations import codex
        if request.path in ('/manage', '/manage/'):
            return web.Response(body=Path(__file__).with_name('manage.html').read_bytes(), content_type='text/html')
        if request.path == '/manage/api/servers':
            return web.json_response({'servers': codex.server_catalog()})
        try:
            args = await request.json()
            if not isinstance(args, dict) or args.get('action') not in ACTIONS:
                raise ValueError('Unsupported management action')
            bridge = codex.select_server(args.get('server'))
            args['_silent'] = True
            args['_stored_only'] = True
            deadline = time.monotonic() + 70
            def check():
                if time.monotonic() >= deadline:
                    raise InterruptedError('Request timed out before submission; check status before retrying')
            async with self.gate:
                check()
                if args['action'] == 'read':
                    result = await asyncio.wait_for(bridge.read_history(args['thread_id'], args.get('cursor'),
                                                     check=check, include_live=False), 70)
                else:
                    # Direct owner UI: existing actions and the Windows server's permissions.
                    result = await asyncio.wait_for(bridge.action(args, '0', check=check), 70)
            return web.json_response({'success': True, 'server': bridge.server_id, **result})
        except (Exception, asyncio.TimeoutError) as error:
            return web.json_response({'success': False, 'error': codex.clean(str(error) or
                'Timed out; check status before retrying. Commands are not automatically replayed.')[:350]}, status=400)

    async def start(self):
        app = web.Application(client_max_size=65536)
        app.router.add_get('/manage', self.handle)
        app.router.add_get('/manage/', self.handle)
        app.router.add_get('/manage/api/servers', self.handle)
        app.router.add_post('/manage/api/call', self.handle)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, '127.0.0.1', 8767).start()

    async def close(self):
        if self.runner is not None: await self.runner.cleanup()
