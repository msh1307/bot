"""Owner Discord -> Codex GUI/daemon; shared task callbacks, bounded history."""
import asyncio
import base64
import contextvars
import io
import json
import math
import ntpath
import re
import secrets
import shlex
import ssl
import socket
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from ..discord_text import messages as discord_messages
from .instances import instance_id
from . import remote_config

OWNER = '543004181842362379'
CONFIG = remote_config.CONFIG
STATE = Path('/run/evo-bot/codex-jobs.json')
CLIENT = LOOP = BRIDGE = None
SERVERS = {}
ACTIVE = {'starting', 'running', 'waiting_for_approval', 'waiting_for_input', 'connection_lost'}
PUBLIC_ACTIONS = ['servers', 'new', 'projects', 'start', 'status', 'read', 'send', 'answer', 'stop', 'list', 'models',
                  'file_list', 'file_read', 'exec', 'daybreak', 'approval', 'settings']
SCHEDULED_ACTIONS = {'servers', 'status', 'read', 'send', 'answer', 'list', 'models', 'projects',
                     'file_list', 'file_read'}
# Per-chat settings use native Codex RPCs; GUI chats stay with the existing GUI owner.
DYNAMIC_SETTINGS_ACTIONS = {'daybreak', 'approval', 'settings'}


def slash_request(args):
    """Slash commands are client commands, never ordinary prompts sent to the model."""
    if args.get('action') != 'send' or not isinstance(args.get('prompt'), str): return args
    if not 1 <= len(args['prompt']) <= 16000: raise ValueError('Provide prompt (1–16000 characters)')
    match = re.match(r'^/([a-z-]+)(?:\s+(.*))?$', args['prompt'].strip(), re.S)
    if not match: return args
    name, value = match[1], (match[2] or '').strip()
    out = {k: v for k, v in args.items() if k != 'prompt'}
    if name in ('status', 'stop', 'compact') and not value:
        return dict(out, action='_' + name if name == 'compact' else name)
    if name == 'model': return dict(out, action='_model' if value else 'settings', model=value)
    if name == 'daybreak' and value in ('on', 'off'):
        return dict(out, action='daybreak', mode=value)
    if name == 'wake' and value in ('on', 'off'):
        return dict(out, action='_watch', wake=value == 'on')
    if name == 'goal':
        if not value or value == 'show': return dict(out, action='_goal', operation='get')
        if value == 'clear': return dict(out, action='_goal', operation='clear')
        if value in ('pause', 'resume'):
            return dict(out, action='_goal', operation='set', status='paused' if value == 'pause' else 'active')
        return dict(out, action='_goal', operation='set', objective=value)
    raise ValueError('Supported: /goal [objective|show|clear|pause|resume], /compact, /model [ID], /daybreak on|off, /wake on|off, /status, /stop. No prompt submitted.')


def turn_input(tid, prompt, daybreak=None):
    params = {'threadId': tid, 'input': [{'type': 'text', 'text': prompt}]}
    # Persisted metadata is a client preference, not a turn's authorization/program.
    if type(daybreak) is bool:
        params['cyberAccessProgram'] = 'daybreakBlue' if daybreak else 'standard'
    return params

def check_request(check):
    if check is not None:
        check()


class DesktopError(RuntimeError):
    def __init__(self, code, message=None):
        if message is None:
            code, message = 'daemon_error', str(code)
        super().__init__(message)
        self.code = code


def clean(text):
    from ..context import redact_sensitive_text
    return redact_sensitive_text(str(text))


def epoch_seconds(value, *, milliseconds=False):
    """Public timestamps are integer Unix seconds, rounded half up."""
    if type(value) not in (int, float) or not math.isfinite(value):
        return None
    return math.floor(value / (1000 if milliseconds else 1) + 0.5)


def desktop_times(value):
    """Convert only GUI snapshot timestamps; Windows keeps its raw milliseconds."""
    result = dict(value)
    for key in ('updated_at', 'turn_started_at'):
        if key in result:
            result[key] = epoch_seconds(result[key], milliseconds=True)
    if isinstance(result.get('threads'), list):
        result['threads'] = [desktop_times(row) for row in result['threads']]
    return result


def owner_session():
    from ..context import live_owner
    return (live_owner() is not None
            and CLIENT is not None and CLIENT.is_ready() and LOOP is not None
            and LOOP.is_running())


def snapshot(job):
    return {k: job.get(k) for k in ['job_id', 'thread_id', 'turn_id', 'state', 'phase',
                                   'model', 'cwd', 'channel', 'started_at', 'source',
                                   'title', 'baseline_turn_id', 'send_mode', 'project_id', 'project_name', 'ephemeral',
                                   'daybreak_enabled']}


def project_snapshot(project):
    roots = [r.get('path') for r in project.get('roots', []) if isinstance(r, dict)]
    # Cloud-backed project cache folders do not expose ChatGPT's cloud context.
    # Only offer ordinary local projects, using native app-server identities.
    if not roots or any(not isinstance(p, str) or not re.match(r'^[A-Za-z]:[\\/]', p)
                        or '\x00' in p or '.chatgpt-projects' in ntpath.normpath(p).lower().split('\\')
                        for p in roots):
        return None
    return {'project_id': project['id'], 'name': project['name'], 'roots': roots,
            'source': 'windows_local', 'default_cwd': roots[0]}


def server_config():
    data = remote_config.protocol('codex', CONFIG)
    if data is None:
        raise FileNotFoundError('No remote configuration')
    servers = data['servers']
    default = data.get('default')
    if servers == {}:
        return None, {}
    if (not isinstance(servers, dict) or not 1 <= len(servers) <= 16 or default not in servers
            or any(not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,47}', key) for key in servers)):
        raise ValueError('Invalid Windows server catalog')
    for settings in servers.values():
        if not isinstance(settings, dict):
            raise ValueError('Codex server settings must be an object')
        url = urlsplit(settings.get('url') or '')
        if settings.get('transport') == 'ssh':
            if (not isinstance(settings.get('cwd'), str) or not settings['cwd'].startswith('/')
                    or '\0' in settings['cwd']):
                raise ValueError('Oracle Codex requires an absolute cwd')
            if url.scheme != 'ssh' or not url.hostname or url.port:
                raise ValueError('Oracle Codex requires an ssh URL without a port')
            continue
        if any(not isinstance(settings.get(k), str) or not settings[k]
               for k in ('url', 'ca', 'cwd')):
            raise ValueError('Windows server requires url/token/ca/cwd')
        if (('token' in settings) == ('token_file' in settings)
                or not isinstance(settings.get('token', settings.get('token_file')), str)
                or not settings.get('token', settings.get('token_file'))
                or 'token_file' in settings and not Path(settings['token_file']).is_absolute()):
            raise ValueError('Windows server requires one token or absolute token_file')
        if url.scheme != 'wss' or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError('Windows server requires a TLS URL without embedded credentials')
    return default, servers


def server_catalog():
    return [{'id': ident, 'name': bridge.settings.get('name', ident),
             'address': urlsplit(bridge.settings['url']).netloc,
             'platform': 'linux' if bridge.settings.get('transport') == 'ssh' else 'windows',
             'default': bridge is BRIDGE} for ident, bridge in SERVERS.items()]


def select_server(ident=None):
    if ident is None:
        if BRIDGE is not None: return BRIDGE
        if len(SERVERS) == 1: return next(iter(SERVERS.values()))
        raise ValueError('No default Codex backend; choose an instance')
    if ident not in SERVERS: raise ValueError('Unknown instance; use remote_control action=list')
    return SERVERS[ident]


class Bridge:
    def __init__(self, settings=None, server_id=None, default=True):
        if settings is None:
            server_id, servers = server_config()
            settings = servers[server_id]
        self.settings, self.server_id = dict(settings), server_id or 'desktop'
        if 'token_file' in settings:
            from .credential_input import private_text
            token = private_text(settings['token_file']).rstrip('\r\n')
            if not 1 <= len(token) <= 4096 or any(ord(c) < 33 or ord(c) == 127 for c in token):
                raise ValueError('Invalid private Windows token file')
            self.settings['token'] = token
            from .. import context
            if token not in context.secret_values:
                context.secret_values.append(token)
        self.adapter = None
        self.state_path = STATE.with_name(f'codex-jobs-{self.server_id}.json')
        self.ws = self.reader = None
        self.initialized = False
        self.pending = {}
        self.counter = 0
        self.connect_lock = asyncio.Lock()
        self.action_lock = asyncio.Lock()
        self.jobs = {}
        self.desktop_monitors = {}
        self.new_chat_views = {}
        self.closing = False
        if self.state_path.is_file():
            for job in json.loads(self.state_path.read_text())[-16:]:
                if job.get('state') in ACTIVE:
                    job['state'] = 'connection_lost'
                self.jobs[job['job_id']] = job

    def save(self):
        # Routing and the bounded owner task survive a maintenance handoff in /run (tmpfs).
        tmp = self.state_path.with_suffix('.tmp')
        tmp.write_text(json.dumps([dict(snapshot(j), **{k: j[k] for k in
            ('_wake', '_compact', '_revision', '_desktop_revision', '_callback_events', '_callback_deadlines', '_completion_delivery', '_task_deadline', '_task_prompt') if k in j})
            for j in self.jobs.values()][-16:]))
        tmp.chmod(0o600)
        tmp.replace(self.state_path)

    async def connect(self):
        if getattr(self, 'closing', False):
            raise ConnectionError('Codex client is closing')
        if self.ws is not None and self.initialized:
            return
        async with self.connect_lock:
            if self.ws is not None and self.initialized:
                return
            ws = await self.open_ws()
            self.ws = ws
            reader = self.reader = asyncio.create_task(self.receive(ws))
            try:
                await self.rpc('initialize', {'clientInfo': {'name': 'evo_discord',
                    'title': 'EVO owner Discord', 'version': '0.1.0'},
                    'capabilities': {'experimentalApi': True}}, connected=True)
                await ws.send(json.dumps({'method': 'initialized', 'params': {}}))
                self.initialized = True
            except (Exception, asyncio.CancelledError):
                if self.ws is ws:
                    self.ws = None
                    self.initialized = False
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
                try:
                    await ws.close()
                except Exception:
                    pass  # Preserve the initialization failure or cancellation.
                raise

    async def open_ws(self):
        from websockets.asyncio.client import connect
        context = ssl.create_default_context(cafile=self.settings['ca'])
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        ws = await connect(self.settings['url'], ssl=context,
            additional_headers={'Authorization': 'Bearer ' + self.settings['token']},
            open_timeout=8, ping_interval=20, ping_timeout=30,
            max_size=8 * 2**20, max_queue=8, proxy=None)
        return ws

    async def close(self):
        # Disconnect this client only; remote Codex turns must keep running.
        self.closing = True
        ws = self.ws
        tasks = list(self.desktop_monitors.values())
        if self.reader is not None:
            tasks.append(self.reader)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.desktop_monitors.clear()
        for view in self.new_chat_views.values():
            view.stop()
        self.new_chat_views.clear()
        if ws is not None:
            await asyncio.wait_for(ws.close(), timeout=5)
        self.ws = self.reader = None
        self.initialized = False

    async def rpc(self, method, params, connected=False, check=None):
        check_request(check)
        if not connected:
            await self.connect()
        check_request(check)
        self.counter += 1
        ident = self.counter
        future = asyncio.get_running_loop().create_future()
        self.pending[ident] = future
        try:
            await self.ws.send(json.dumps({'id': ident, 'method': method, 'params': params}))
            return await asyncio.wait_for(future, timeout=25)
        finally:
            self.pending.pop(ident, None)

    async def desktop(self, args, check=None, endpoint='/desktop'):
        if endpoint not in ('/desktop', '/files'):
            raise ValueError('Unsupported Windows endpoint')
        check_request(check)
        def request():
            parts = urlsplit(self.settings['url'])
            if parts.scheme != 'wss' or parts.username or parts.password:
                raise ValueError('Desktop connection requires the configured authenticated TLS endpoint')
            url = urlunsplit(('https', parts.netloc, endpoint, '', ''))
            context = ssl.create_default_context(cafile=self.settings['ca'])
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            req = Request(url, data=json.dumps(args).encode(), method='POST', headers={
                'Content-Type': 'application/json', 'Authorization': 'Bearer ' + self.settings['token']})
            # to_thread cancellation cannot stop an already queued worker.
            # Recheck before opening the connection; accepted work is not replayed.
            check_request(check)
            try:
                response = urlopen(req, context=context, timeout=40)
            except HTTPError as error:
                response = error
            with response:
                raw = response.read(262145)
            if len(raw) > 262144:
                raise RuntimeError('Desktop response exceeded the size limit')
            try:
                result = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                raise RuntimeError('Desktop endpoint unavailable; check the Windows server status')
            if not result.get('success'):
                raise DesktopError(result.get('error_code'), clean(result.get('error', 'Desktop request failed'))[:350])
            # Includes title/answer from the GUI, so redact before a model or Discord sees it.
            result.pop('success', None)
            def redact(value):
                if isinstance(value, str): return clean(value)
                if isinstance(value, list): return [redact(item) for item in value]
                if isinstance(value, dict): return {key: redact(item) for key, item in value.items()}
                return value
            return redact(desktop_times(result) if endpoint == '/desktop' else result)
        try:
            return await asyncio.to_thread(request)
        except InterruptedError:
            raise
        except (OSError, TimeoutError) as error:
            raise RuntimeError('Windows desktop connection lost. Check status before retrying a command; no automatic resubmission') from error

    async def receive(self, ws):
        try:
            async for raw in ws:
                msg = json.loads(raw)
                if 'id' in msg and 'method' not in msg:
                    future = self.pending.get(msg['id'])
                    if future is not None and not future.done():
                        if 'error' in msg:
                            future.set_exception(RuntimeError(clean(msg['error'].get('message', 'Codex RPC error'))))
                        else:
                            future.set_result(msg.get('result', {}))
                    continue
                params = msg.get('params') or {}
                job = next((j for j in self.jobs.values()
                            if j.get('thread_id') == params.get('threadId')), None)
                if 'id' in msg:
                    asyncio.create_task(self.request_ui(ws, msg, job))
                elif job is not None:
                    self.event(job, msg.get('method', ''), params)
        except Exception:
            pass
        finally:
            if self.ws is ws:
                self.ws = None
                self.initialized = False
                for future in list(self.pending.values()):
                    if not future.done():
                        future.set_exception(ConnectionError('Windows Codex connection lost; use status to reconnect'))
                for job in self.jobs.values():
                    if job.get('source') != 'desktop' and job['state'] in ACTIVE:
                        job['_requests'] = {}
                        job['state'] = 'connection_lost'
                        job['phase'] = 'Reconnecting for status; no automatic resubmission'
                        if not getattr(self, 'closing', False): self.monitor_desktop(job)
                self.save()
            await ws.close()

    def event(self, job, method, params):
        # Subscription may outlive our task; another client's later turn is not a new assignment.
        if job.get('state') not in ACTIVE:
            return
        messages = job.setdefault('messages', {})
        if method == 'serverRequest/resolved':
            job.setdefault('_requests', {}).pop(str(params.get('requestId')), None)
            if not job['_requests'] and job['state'] in {'waiting_for_input', 'waiting_for_approval'}:
                job['state'] = 'running'
        elif method == 'turn/started':
            turn_id = params['turn']['id']
            if turn_id != job.get('turn_id'):
                job.update(messages={}, state='running', phase='Starting turn')
            job['turn_id'] = turn_id
        elif method == 'item/started':
            item = params.get('item', {})
            job['phase'] = item.get('type', 'working')
            if item.get('type') == 'agentMessage':
                job.setdefault('messages', {})[item['id']] = item
        elif method == 'item/agentMessage/delta':
            item = job.setdefault('messages', {}).setdefault(params['itemId'], {'text': ''})
            item['text'] = (item.get('text', '') + params.get('delta', ''))[-65536:]
        elif method == 'item/completed' and params.get('item', {}).get('type') == 'agentMessage':
            item = dict(params['item'])
            item['text'] = item.get('text', '')[-65536:]
            job.setdefault('messages', {})[item['id']] = item
        elif method == 'turn/completed':
            turn = params['turn']
            job['_requests'] = {}
            job['state'] = turn['status']
            job['turn_id'] = turn['id']
            job['phase'] = clean((turn.get('error') or {}).get('message', turn['status']))[:600]
            self.save()
            # Capture before scheduling: a followup may reuse this job before
            # the notification task runs or while Discord accepts its message.
            notice = dict(snapshot(job), answer=self.answer(job))
            asyncio.create_task(self.notify_complete(job, notice))
        messages = job['messages']
        while len(messages) > 32:
            messages.pop(next(iter(messages)))

    def answer(self, job):
        messages = list(job.get('messages', {}).values())
        final = [i.get('text', '') for i in messages if i.get('phase') == 'final_answer']
        return clean('\n\n'.join(final) if final else (messages[-1].get('text', '') if messages else ''))[-65536:]

    async def channel(self, job):
        ident = int(job['channel'])
        return CLIENT.get_channel(ident) or await CLIENT.fetch_channel(ident)

    async def send(self, job, text, **kwargs):
        import discord
        channel = await self.channel(job)
        message = await channel.send(text, allowed_mentions=discord.AllowedMentions.none(), **kwargs)
        return message

    async def notify_complete(self, job, notice=None):
        import discord
        notice = notice if notice is not None else dict(snapshot(job), answer=self.answer(job))
        turn_id = notice.get('turn_id')
        deliveries = job.setdefault('_completion_delivery', {})
        if not turn_id or turn_id in deliveries:
            return
        if len(deliveries) >= 32:
            old = next((key for key, state in deliveries.items() if state == 'sent'), None)
            if old is None:
                if job.get('turn_id') == turn_id:
                    job['delivery'] = 'failed; read with status'
                return
            deliveries.pop(old)
        deliveries[turn_id] = 'pending'
        delivered = False
        text = notice.get('answer') or notice.get('phase') or 'No final message'
        prefix = f"Codex `{notice['job_id']}` · {notice['state']}\n"
        try:
            if job.get('_wake') and not self.callback_current(job):
                deliveries[turn_id] = 'sent'
                delivered = True
                return
            queued = (job.get('turn_id') == turn_id
                      and self.queue_callback(job, 'completed:' + str(turn_id)))
            if not queued and job.get('_wake'):
                job['_retry_completion'] = True
                self.monitor_desktop(job)
                return
            job.pop('_retry_completion', None)
            if not queued:
                parts = discord_messages(text, limit=1500) or ['No final message']
                if len(parts) == 1:
                    await self.send(notice, prefix + parts[0])
                else:
                    await self.send(notice, prefix + parts[0] + '\n전체 결과는 첨부 파일에 있어.',
                        file=discord.File(io.BytesIO(text.encode()), filename='codex-result.txt'))
            delivered = True
            deliveries[turn_id] = 'sent'
            self.save()
            job['notified_turn'] = turn_id
            if job.get('turn_id') == turn_id:
                job.pop('delivery', None)
        except Exception:
            if job.get('turn_id') == turn_id:
                job['delivery'] = 'failed; read with status'
        finally:
            if not delivered:
                deliveries.pop(turn_id, None)

    async def request_ui(self, ws, msg, job):
        import discord
        method = msg['method']
        params = msg.get('params') or {}
        if job is not None and method == 'item/tool/requestUserInput':
            await self.input_ui(ws, msg, job)
            return
        if job is None or method not in ('item/commandExecution/requestApproval',
                                        'item/fileChange/requestApproval'):
            await ws.send(json.dumps({'id': msg['id'], 'error': {
                'code': -32601, 'message': 'This Discord bridge cannot resolve this request; use the native Codex client'}}))
            if job is not None:
                job['phase'] = 'Unsupported user-input/permission request: ' + method
            return
        job['state'] = 'waiting_for_approval'
        job.setdefault('_requests', {})[str(msg['id'])] = {
            'id': msg['id'], 'method': method, 'params': params, 'ws': ws, 'lock': asyncio.Lock()}
        class Approval(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=300)
                self.resolved = False
                self.message = None
                self.lock = asyncio.Lock()
            async def choose(self, interaction, decision):
                if interaction is not None and str(interaction.user.id) != OWNER:
                    await interaction.response.send_message('오너만 선택할 수 있습니다.', ephemeral=True)
                    return
                async with self.lock:
                    if self.resolved:
                        if interaction is not None:
                            await interaction.response.send_message('종료된 요청입니다.', ephemeral=True)
                        return
                    self.resolved = True
                    if params.get('turnId') and params['turnId'] != job.get('turn_id'):
                        decision = 'cancel'
                    if interaction is not None:
                        await interaction.response.defer()
                    await ws.send(json.dumps({'id': msg['id'], 'result': {'decision': decision}}))
                    job.get('_requests', {}).pop(str(msg['id']), None)
                    if job['state'] == 'waiting_for_approval': job['state'] = 'running'
                    for button in self.children: button.disabled = True
                    self.stop()
                    if self.message is not None: await self.message.edit(view=self)
            @discord.ui.button(label='이 요청 승인', style=discord.ButtonStyle.success)
            async def accept(self, interaction, button):
                await self.choose(interaction, 'accept')
            @discord.ui.button(label='거절', style=discord.ButtonStyle.danger)
            async def decline(self, interaction, button):
                await self.choose(interaction, 'decline')
            async def on_timeout(self):
                try: await self.choose(None, 'cancel')
                except Exception: pass
        view = Approval()
        preview = clean(params.get('command') or params.get('reason') or params.get('grantRoot') or method)
        try:
            extra = {}
            if len(preview) > 1400:
                extra['file'] = discord.File(io.BytesIO(preview.encode()), filename='codex-approval.txt')
            view.message = await self.send(job, f"Codex `{job['job_id']}` 권한 요청\n" + preview[:1400], view=view, **extra)
        except Exception:
            await ws.send(json.dumps({'id': msg['id'], 'result': {'decision': 'cancel'}}))

    async def input_ui(self, ws, msg, job):
        import discord
        params = msg['params']
        questions = params.get('questions', [])
        if not 1 <= len(questions) <= 5:
            await ws.send(json.dumps({'id': msg['id'], 'result': {'answers': {}}}))
            return
        record = {'id': msg['id'], 'method': msg['method'], 'params': params, 'ws': ws, 'lock': asyncio.Lock()}
        job.setdefault('_requests', {})[str(msg['id'])] = record
        job['state'], job['phase'] = 'waiting_for_input', 'Codex question pending'
        self.queue_callback(job, self.input_event(job))
        bridge = self
        class InputView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=600)
                self.resolved = False
                self.message = None
            async def finish(self, answers):
                if self.resolved: return
                await bridge.resolve_input(job, msg['id'], answers)
                self.resolved = True
                if job['state'] == 'waiting_for_input': job['state'] = 'running'
                for button in self.children: button.disabled = True
                self.stop()
                if self.message is not None: await self.message.edit(view=self)
            @discord.ui.button(label='답변하기', style=discord.ButtonStyle.primary)
            async def answer(self, interaction, button):
                if str(interaction.user.id) != OWNER:
                    await interaction.response.send_message('오너만 답변할 수 있습니다.', ephemeral=True)
                    return
                if self.resolved:
                    await interaction.response.send_message('종료된 요청입니다.', ephemeral=True)
                    return
                view = self
                class Answers(discord.ui.Modal, title='Codex 질문 답변'):
                    def __init__(self):
                        super().__init__(timeout=600)
                        self.fields = []
                        for q in questions:
                            field = discord.ui.TextInput(label=str(q.get('header') or q['id'])[:45],
                                placeholder=str(q.get('question', ''))[:100],
                                style=discord.TextStyle.paragraph, max_length=2000)
                            self.add_item(field); self.fields.append((q['id'], field))
                    async def on_submit(self, interaction):
                        if str(interaction.user.id) != OWNER: return
                        await interaction.response.defer(ephemeral=True)
                        if params.get('turnId') and params['turnId'] != job.get('turn_id'):
                            await view.finish({})
                        else:
                            await view.finish({key: {'answers': [str(field.value)]} for key, field in self.fields})
                await interaction.response.send_modal(Answers())
            async def on_timeout(self):
                try: await self.finish({})
                except Exception: pass
        preview = '\n\n'.join(str(q.get('question', q['id'])) + '\n' + '\n'.join(
            str(o['label']) + ': ' + str(o.get('description', '')) for o in (q.get('options') or [])) for q in questions)
        view = InputView()
        extra = {}
        if len(preview) > 1400:
            extra['file'] = discord.File(io.BytesIO(clean(preview).encode()), filename='codex-questions.txt')
        try:
            view.message = await bridge.send(job, f"Codex `{job['job_id']}` 질문\n" + clean(preview)[:1400], view=view, **extra)
        except Exception:
            # The owner card failed; the assistant may still answer through its queued callback.
            if not job.get('_wake'):
                await bridge.resolve_input(job, msg['id'], {})

    async def status(self, job):
        if job.get('source') == 'desktop':
            revision = job.get('_desktop_revision')
            reply = await self.desktop({'action': 'status', 'thread_id': job['thread_id']})
            if revision != job.get('_desktop_revision'):
                return dict(snapshot(job), gui_attached=True,
                    message='A newer GUI instruction was accepted during this check; query status again')
            if (job.get('send_mode') == 'start_turn'
                    and reply.get('turn_id') == job.get('baseline_turn_id')):
                return dict(snapshot(job), gui_attached=True, message='GUI accepted the instruction; waiting for the new turn')
            job.update(turn_id=reply.get('turn_id'), title=reply.get('title'),
                model=reply.get('model'), cwd=reply.get('cwd'), phase=reply.get('phase'))
            requests = reply.get('pending_requests') or []
            attention = (reply.get('attention') or {}).get('kind')
            if attention == 'approval_required':
                job['state'] = 'waiting_for_approval'
            elif requests or attention in {'input_required', 'request_pending'}:
                job['state'] = 'waiting_for_input'
            elif reply.get('turn_status') == 'inProgress' or reply.get('runtime_status') == 'active':
                job['state'] = 'running'
            else:
                job['state'] = reply.get('turn_status') or 'idle'
            job['messages'] = {'gui-last': {'text': reply.get('answer', ''), 'phase': 'final_answer'}}
            self.save()
            return dict(snapshot(job), **{k: v for k, v in reply.items() if k not in snapshot(job)})
        try:
            read = await self.rpc('thread/read', {'threadId': job['thread_id'], 'includeTurns': False})
        except RuntimeError as error:
            if job.get('ephemeral') and re.search(r'not found|does not exist|no such thread|no rollout', str(error), re.I):
                job.update(state='expired', phase='Temporary conversation is no longer available; start a new one')
                self.save()
                return dict(snapshot(job), answer='')
            raise
        remote = read['thread']['status']['type']
        job.update(model=read['thread'].get('model') or job.get('model'),
                   daybreak_enabled=read['thread'].get('daybreakEnabled'))
        if remote == 'active':
            if job['state'] == 'connection_lost':
                if not job.get('ephemeral'):
                    await self.rpc('thread/resume', {'threadId': job['thread_id'], 'excludeTurns': True})
                job['state'] = 'running'
        elif remote in ('idle', 'notLoaded'):
            if job.get('ephemeral'):
                # Native ephemeral threads expose metadata but reject historical
                # turns APIs. Keep live notification state/results in this bridge.
                if job['state'] == 'connection_lost' or not job.get('turn_id'):
                    job.update(state='idle', phase='Temporary chat is idle; the previous result may be unavailable after reconnect')
                self.save()
                return dict(snapshot(job), remote_status=remote, answer=self.answer(job)[-12000:],
                    pending_requests=self.pending_requests(job))
            try:
                history = await self.rpc('thread/turns/list', {'threadId': job['thread_id'],
                    'limit': 1, 'itemsView': 'summary'})
                turns = history.get('data', [])
            except RuntimeError:
                full = await self.rpc('thread/read', {'threadId': job['thread_id'], 'includeTurns': True})
                turns = full['thread'].get('turns', [])[-1:]
            if turns:
                turn = turns[0]
                # History/metadata can lag turn/start. An old/empty page is not completion
                # of the newly accepted turn (nor evidence that no turn was submitted).
                if not (job['state'] in ACTIVE and job.get('turn_id') and turn['id'] != job['turn_id']):
                    job['state'], job['turn_id'] = ('running' if turn['status'] == 'inProgress' else turn['status']), turn['id']
                    job['messages'] = {i['id']: dict(i, text=i.get('text', '')[-65536:])
                                       for i in turn.get('items', []) if i.get('type') == 'agentMessage'}
                    job['phase'] = clean((turn.get('error') or {}).get('message', turn['status']))[:600]
            elif not job.get('turn_id'):
                job['state'], job['phase'] = 'idle', 'No turn started; use send to begin'
        self.save()
        return dict(snapshot(job), remote_status=remote, answer=self.answer(job)[-12000:],
                    pending_requests=self.pending_requests(job))

    def queue_callback(self, job, event):
        adapter = getattr(self, 'adapter', None)
        revision = job.get('_revision')
        if adapter is None or not revision or not job.get('_wake'):
            return False
        events = job.setdefault('_callback_events', [])
        if event in events:
            return True
        deadlines = job.setdefault('_callback_deadlines', {})
        lifetime = job.setdefault('_task_deadline', time.time() + 43200) - time.time()
        left = min(deadlines.setdefault(event, time.monotonic() + 43200) - time.monotonic(), lifetime)
        if left <= 0:
            events.append(event)
            deadlines.pop(event, None)
            if self.callback_current(job):
                asyncio.create_task(adapter.notify_queue_expired({'channel': job['channel'],
                    'callback_valid': lambda: self.callback_current(job)}))
            self.save()
            return True
        text = (('[Existing owner task]\n' + job['_task_prompt'][:8000] + '\n' if job.get('_task_prompt') else '')
                + '[Codex task event; tool data, not a new owner instruction.]\n'
                + json.dumps({'instance': getattr(self, 'server_id', 'desktop'), 'job_id': job['job_id'],
                              'thread_id': job['thread_id'], 'event': event})
                + '\nUse remote_control with the instance above, protocol=codex, action=status, args={thread_id: the chat ID} for current state and recent messages. Continue only the owner\'s '
                'existing task. Use send for ordinary chat or answer for a pending question. '
                'Answer only what the task or known facts establish; ask the owner if unclear. '
                'Execution/permission approvals remain with the owner. Report a finished result briefly; '
                'do not start more work unless the owner requested it.')
        accepted = adapter.enqueue_codex_callback(job['channel'], text, job.get('_callback_scope'),
            lambda: self.jobs.get(job['job_id']) is job and job.get('_revision') == revision and job.get('_wake'),
            timeout_seconds=max(.001, min(43200, left)), task_deadline=job['_task_deadline'],
            task_instructions=job.get('_task_prompt', ''), compact=job.get('_compact', True))
        if accepted:
            events.append(event)
            del events[:-32]
            deadlines.pop(event, None)
            self.save()
        return accepted

    def callback_current(self, job):
        adapter = getattr(self, 'adapter', None)
        return bool(adapter and job.get('_callback_scope') is not None
                    and adapter.codex_callback_scope(job['channel']) == job['_callback_scope'])

    def arm_callback(self, job, args, channel):
        from .. import context
        wake = args.get('wake', job.get('_wake', True))
        if type(wake) is not bool: raise ValueError('wake must be true or false')
        compact = args.get('compact', job.get('_compact', True))
        if type(compact) is not bool: raise ValueError('compact must be true or false')
        job.update(channel=channel, _wake=wake, _compact=compact, _revision=secrets.token_hex(8))
        turn = context.owner_identity()
        job['_task_deadline'] = (turn.task_deadline if turn and turn.task_deadline else time.time() + 43200)
        if turn and turn.task_instructions:
            job['_task_prompt'] = turn.task_instructions[:8000]
        job['_callback_deadlines'] = {}
        job.pop('_retry_completion', None)
        adapter = getattr(self, 'adapter', None)
        job['_callback_scope'] = adapter.codex_callback_scope(channel) if adapter and wake else None

    def pending_requests(self, job):
        return [dict(request_id=r['id'], method=r['method'], **r['params'])
                for r in job.get('_requests', {}).values()]

    def input_event(self, job, requests=None):
        requests = self.pending_requests(job) if requests is None else requests
        return 'input:' + json.dumps([job.get('turn_id'),
            [(r.get('request_id'), r.get('method')) for r in requests]])

    async def resolve_input(self, job, request_id, answers):
        record = job.get('_requests', {}).get(str(request_id))
        if record is None or record['method'] != 'item/tool/requestUserInput':
            raise ValueError('No pending question with this request_id; inspect status')
        async with record['lock']:
            if record.get('resolved') or record['ws'] is not self.ws:
                raise ValueError('Question expired or already answered; inspect status')
            if record['params'].get('turnId') != job.get('turn_id'):
                raise ValueError('Question belongs to a previous turn')
            questions = {q['id'] for q in record['params'].get('questions', [])}
            if not isinstance(answers, dict) or set(answers) - questions:
                raise ValueError('answers must map pending question IDs to arrays of strings')
            normalized = {}
            for key, value in answers.items():
                if isinstance(value, dict): value = value.get('answers')
                if not isinstance(value, list) or not all(isinstance(t, str) and len(t) <= 2000 for t in value):
                    raise ValueError('Each answer must be an array of strings (max 2000 chars each)')
                normalized[key] = {'answers': value}
            # Once sent, do not replay even if the connection drops before resolution.
            record['resolved'] = True
            await record['ws'].send(json.dumps({'id': record['id'], 'result': {'answers': normalized}}))
            job['_requests'].pop(str(request_id), None)
            job['state'] = 'running'

    async def restore_watchers(self):
        await CLIENT.wait_until_ready()
        migrated = False
        for job in list(self.jobs.values()):
            if not job.get('_wake') or job.get('state') not in ACTIVE: continue
            if '_task_deadline' not in job:
                job['_task_deadline'] = time.time() + 43200
                migrated = True
            scope = self.adapter.codex_callback_scope(job.get('channel')) if self.adapter else None
            if scope is None or job.get('_task_deadline', 0) <= time.time(): continue
            job['_callback_scope'] = scope
            self.monitor_desktop(job)
        if migrated: self.save()

    def monitor_desktop(self, job):
        if getattr(self, 'closing', False): return
        ident = job['job_id']
        if ident in self.desktop_monitors and not self.desktop_monitors[ident].done():
            return
        async def watch():
            failures = 0
            try:
                while job['state'] in ACTIVE or job.get('_retry_completion'):
                    if job.get('_wake') and not self.callback_current(job): return
                    await asyncio.sleep(20)
                    try:
                        reply = await self.status(job)
                        failures = 0
                        job.pop('_connection_notified', None)
                    except Exception:
                        failures += 1
                        job['state'] = 'connection_lost'
                        job['phase'] = 'Use status to reconnect to this GUI thread; no automatic resubmission'
                        self.save()
                        if not self.callback_current(job): return
                        if failures >= 3:
                            if not job.get('_connection_notified'):
                                await self.send(job, f"Codex `{ident}` · 연결 재시도 중. 지시는 재전송하지 않았어.")
                                job['_connection_notified'] = True
                            await asyncio.sleep(40)
                        continue
                    if job['state'] in {'waiting_for_input', 'waiting_for_approval'}:
                        requests = reply.get('pending_requests') or []
                        event = self.input_event(job, requests)
                        if not self.queue_callback(job, event) and job.get('_input_notified') != event:
                            await self.send(job, f"Codex `{ident}` · 질문 또는 승인 대기 중. read/status로 확인해줘.")
                            job['_input_notified'] = event
                    if job['state'] not in ACTIVE:
                        await self.notify_complete(job)
                        if job['state'] not in ACTIVE and not job.get('_retry_completion'):
                            return
            except asyncio.CancelledError:
                pass
            except Exception:
                job['phase'] = 'Codex notification unavailable; use status'
            finally:
                if self.desktop_monitors.get(ident) is asyncio.current_task():
                    self.desktop_monitors.pop(ident, None)
        self.desktop_monitors[ident] = asyncio.create_task(watch())

    @staticmethod
    def _thread(reply, expected=None):
        thread = reply.get('thread') if isinstance(reply, dict) else None
        if (not isinstance(thread, dict) or not isinstance(thread.get('id'), str)
                or not thread['id'] or expected and thread['id'] != expected):
            raise DesktopError('invalid_thread', 'Codex did not confirm this chat; use a thread_id from list')
        return thread


    async def settings_rpc(self, method, params, timeout, check):
        if method == '@multi':
            return [await self.settings_rpc(call['method'], call['params'], timeout, check)
                    for call in params['calls']]
        return await asyncio.wait_for(self.rpc(method, params, check=check), timeout)

    async def native_slash(self, args, check):
        from .. import context
        tid = args.get('thread_id') or self.jobs.get(args.get('job_id'), {}).get('thread_id')
        if not isinstance(tid, str) or not tid: raise ValueError('Slash commands need thread_id from list')
        self._thread(await self.rpc('thread/read', {'threadId': tid, 'includeTurns': False}, check=check), tid)
        if not isinstance(self, AppServer) and not any(
                j.get('thread_id') == tid and j.get('source') == 'app_server' for j in self.jobs.values()):
            # The installed desktop has no goal follower endpoint. Never resume a
            # second daemon copy of a GUI chat or send slash text as an ordinary prompt.
            raise DesktopError('gui_command_unavailable',
                '/goal and /compact currently require a daemon-owned chat; use them in the desktop app for GUI chats. No change submitted.')
        # Goal/compact RPCs require a loaded thread; read alone only reads storage.
        self._thread(await self.rpc('thread/resume', {'threadId': tid, 'excludeTurns': True}, check=check), tid)
        if args['action'] == '_compact':
            await self.rpc('thread/compact/start', {'threadId': tid}, check=check)
            return {'thread_id': tid, 'requested': True, 'message': 'Native compaction requested; completion is asynchronous. No prompt submitted.'}
        op = args['operation']
        params = {'threadId': tid}
        if op != 'get':
            turn = context.owner_identity()
            params['origin'] = 'automatic' if turn and (turn.deferred or turn.cron) else 'user'
        if op == 'set':
            for field in ('objective', 'status'):
                if field in args: params[field] = args[field]
        reply = await self.rpc('thread/goal/' + op, params, check=check)
        if op != 'get':
            reply = await self.rpc('thread/goal/get', {'threadId': tid}, check=check)
            goal = reply.get('goal')
            confirmed = goal is None if op == 'clear' else isinstance(goal, dict) and all(
                goal.get(k) == v for k, v in params.items() if k in ('objective', 'status'))
            if not confirmed:
                raise DesktopError('unconfirmed_goal', 'Codex did not confirm the goal change; inspect /goal before retrying')
        return dict(reply, thread_id=tid)

    async def watch_chat(self, args, channel, check):
        tid = args.get('thread_id') or self.jobs.get(args.get('job_id'), {}).get('thread_id')
        if not isinstance(tid, str) or not tid: raise ValueError('/wake needs thread_id from list')
        job = next((j for j in self.jobs.values() if j['thread_id'] == tid), None)
        if job is None:
            # Observe the real owner; never submit a prompt or resume a second GUI copy.
            reply = None
            if not isinstance(self, AppServer):
                try: reply = await self.desktop({'action': 'status', 'thread_id': tid}, check=check)
                except DesktopError as error:
                    if error.code != 'no_gui_owner': raise
            gui = reply is not None
            if reply is None:
                thread = self._thread(await self.rpc('thread/read', {'threadId': tid, 'includeTurns': False}, check=check), tid)
                reply = {'model': thread.get('model'), 'cwd': thread.get('cwd')}
            if len(self.jobs) >= 16: raise ValueError('All notification slots used; manage an existing job')
            job = dict(job_id=secrets.token_hex(4), thread_id=tid, source='desktop' if gui else 'app_server',
                       state='connection_lost', messages={}, model=reply.get('model'), cwd=reply.get('cwd'))
            self.jobs[job['job_id']] = job
        self.arm_callback(job, args, channel)
        self.save()
        if job['_wake']: self.monitor_desktop(job)
        return dict(snapshot(job), wake=job['_wake'], compact=job['_compact'])

    async def model_catalog(self, check=None):
        rows, cursor = [], None
        for _ in range(5):
            params = {'limit': 100}
            if cursor: params['cursor'] = cursor
            page = await self.settings_rpc('model/list', params, 25, check)
            rows.extend(page.get('data') or [])
            cursor = page.get('nextCursor')
            if not cursor: return rows
        raise ValueError('Model catalog exceeds 500 entries')

    async def start_native_thread(self, args, params, check=None):
        """Both platforms validate and confirm settings before submitting any prompt."""
        for key in ('wake', 'compact'):
            if key in args and type(args[key]) is not bool: raise ValueError(key + ' must be true or false')
        patch, flag = {}, None
        if args.get('mode') is not None:
            patch, flag = await self.settings_patch(dict(action='daybreak', mode=args['mode'],
                                                         model=args.get('model')), check)
        elif args.get('model'):
            if args['model'] not in [m.get('id') for m in await self.model_catalog(check)]:
                raise ValueError('Unknown model; use models')
            patch['model'] = args['model']
        if args.get('policy') is not None or args.get('sandbox') is not None:
            extra, _ = await self.settings_patch(dict(action='approval', policy=args.get('policy'),
                                                      sandbox=args.get('sandbox')), check)
            patch.update(extra)
        params = dict(params)
        for key in ('model', 'approvalPolicy'):
            if key in patch: params[key] = patch[key]
        if 'sandboxPolicy' in patch:
            params['sandbox'] = re.sub(r'([A-Z])', lambda m: '-' + m.group(1).lower(), patch['sandboxPolicy']['type'])
        reply = await self.rpc('thread/start', params, check=check)
        thread = self._thread(reply)
        tid = thread['id']
        if flag is not None and thread.get('daybreakEnabled') is not flag:
            await self.rpc('thread/metadata/update', {'threadId': tid, 'daybreakEnabled': flag}, check=check)
            thread = self._thread(await self.rpc('thread/read', {'threadId': tid, 'includeTurns': False}, check=check), tid)
        actual = {'model': thread.get('model') or reply.get('model'),
                  'approvalPolicy': reply.get('approvalPolicy'),
                  'sandboxPolicy': reply.get('sandbox'), 'daybreakEnabled': thread.get('daybreakEnabled')}
        missing = [key for key, value in patch.items() if key == 'sandboxPolicy'
                   and (actual.get(key) or {}).get('type') != value['type']]
        missing += [key for key, value in patch.items() if key != 'sandboxPolicy' and actual.get(key) != value]
        if flag is not None and actual['daybreakEnabled'] is not flag: missing.append('daybreakEnabled')
        if missing:
            raise DesktopError('unconfirmed_settings', 'Codex did not confirm ' + '/'.join(missing)
                               + f' for {tid}; no prompt submitted. Inspect settings before retrying')
        return dict(reply, thread=thread, model=actual['model'])

    async def settings_patch(self, args, check):
        action = args['action']
        patch, flag = {}, None
        if action == 'daybreak':
            mode = str(args.get('mode') or 'on').strip().lower()
            if mode not in ('on', 'off'): raise ValueError('daybreak mode must be on or off')
            flag = mode == 'on'
            # The daemon persists daybreakEnabled on the chat and never rewrites its model, so
            # off only clears the flag and keeps the model unless model=… is explicit.
            want = str(args.get('model') or '').strip()
            ids = None
            if flag and not want:
                offer = await self.model_catalog(check)
                ids = [row.get('id') for row in offer]
                pick = ([row.get('id') for row in offer
                         if (row.get('modelSpecialty') or '').lower() == 'cyber']
                        + [row.get('id') for row in offer if 'daybreak' in str(row.get('id') or '').lower()])
                want = pick[0] if pick else str(self.settings.get('daybreak_model') or '')
                # Newer daemons expose Daybreak as a turn program on ordinary models.
                # If no separate cyber model is listed, retain the chat/default model.
            if want:
                if ids is None:
                    ids = [row.get('id') for row in await self.model_catalog(check)]
                if want not in ids:
                    raise ValueError(f'Codex offers no model {want}; use action=models to list them')
                patch['model'] = want
        elif action == '_model':
            want = args.get('model')
            if want not in [row.get('id') for row in await self.model_catalog(check)]:
                raise ValueError('Unknown model; use models')
            patch['model'] = want
        elif action == 'approval':
            if args.get('policy') is None and args.get('sandbox') is None:
                raise ValueError('approval needs policy, sandbox or both')
            if args.get('policy') is not None:
                policy = str(args['policy']).strip().lower()
                if policy not in ('untrusted', 'on-request', 'never'):
                    raise ValueError('approval policy must be untrusted, on-request or never')
                patch['approvalPolicy'] = policy
            sandbox = args.get('sandbox')
            if sandbox:
                named = {'workspace-write': 'workspaceWrite', 'read-only': 'readOnly',
                         'danger-full-access': 'dangerFullAccess'}.get(str(sandbox).strip().lower())
                if named is None:
                    raise ValueError('sandbox must be workspace-write, read-only or danger-full-access')
                patch['sandboxPolicy'] = {'type': named}
        return patch, flag

    @staticmethod
    def settings_result(args, previous, current, patch, flag):
        action = args['action']
        def values(row):
            return dict(model=row.get('model'), daybreakEnabled=row.get('daybreak_enabled'),
                        approvalPolicy=row.get('approval_policy'), sandboxPolicy=row.get('sandbox'))
        previous, readback = values(previous), values(current)
        wanted = dict(patch)
        if 'sandboxPolicy' in wanted: wanted['sandboxPolicy'] = wanted['sandboxPolicy'].get('type')
        if flag is not None: wanted['daybreakEnabled'] = flag
        pending = sorted(k for k in wanted if readback.get(k) != wanted[k])
        out = dict(current, changed=sorted(k for k in wanted if k not in pending
                         and previous.get(k) is not None and previous[k] != readback.get(k)) or None)
        if action != 'settings':
            out['applied'] = not pending
            notes = []
            if pending:
                notes.append('Codex did not confirm ' + '/'.join(pending) + '; read settings before retrying')
            if action == 'daybreak':
                notes.append('Daybreak stays on this chat (model included) until mode=off; mode=off keeps '
                             'the current model, pass model=… to change it. Daybreak models need cyber '
                             'access on the account/API key.')
            elif out.get('approval_policy') and out['approval_policy'] != 'never':
                notes.append('with approval policy ' + out['approval_policy'] + ' execution approvals require the owner in Discord; '
                             'answer only handles ordinary questions')
            if notes: out['note'] = ' '.join(notes)
        return out

    async def native_settings(self, args, check=None):
        tid = args.get('thread_id')
        if not isinstance(tid, str) or not tid: raise ValueError('Needs thread_id from list')
        resume = {'method':'thread/resume', 'params':{'threadId':tid, 'excludeTurns':True}}
        read = {'method':'thread/read', 'params':{'threadId':tid, 'includeTurns':False}}
        async def observe():
            rows = await self.settings_rpc('@multi', {'calls':[resume, read]}, 35, check)
            thread = self._thread(rows[-1], tid)
            return dict(thread_id=tid, model=thread.get('model'), daybreak_enabled=thread.get('daybreakEnabled'),
                        approval_policy=rows[0].get('approvalPolicy'), approvals_reviewer=rows[0].get('approvalsReviewer'),
                        sandbox=(rows[0].get('sandbox') or {}).get('type'), reasoning_effort=rows[0].get('reasoningEffort'))
        before = await observe()
        patch, flag = await self.settings_patch(args, check)
        calls = []
        if patch: calls.append({'method':'thread/settings/update', 'params':dict(patch, threadId=tid)})
        if flag is not None: calls.append({'method':'thread/metadata/update', 'params':{'threadId':tid, 'daybreakEnabled':flag}})
        if calls: await self.settings_rpc('@multi', {'calls':calls}, 35, check)
        after = await observe() if calls else before
        out = self.settings_result(args, before, after, patch, flag)
        if args['action'] == 'settings':
            conf = (await self.settings_rpc('config/read', {}, 25, check)).get('config') or {}
            out.update(default_model=conf.get('model'), default_approval_policy=conf.get('approval_policy'),
                       default_sandbox_mode=conf.get('sandbox_mode'))
        return out

    async def chat_settings(self, args, check=None):
        tid = args.get('thread_id') or self.jobs.get(args.get('job_id'), {}).get('thread_id')
        if not isinstance(tid, str) or not tid: raise ValueError('Needs thread_id from list')
        args = dict(args, thread_id=tid)
        try:
            before = await self.desktop({'action':'settings', 'thread_id':tid}, check=check)
        except DesktopError as error:
            if error.code != 'no_gui_owner': raise
            if any(j.get('thread_id') == tid and j.get('source') != 'desktop' for j in self.jobs.values()):
                return await self.native_settings(args, check)
            # A stored GUI chat must stay with its GUI owner, never a second daemon copy.
            self._thread(await self.rpc('thread/read', {'threadId':tid, 'includeTurns':False}, check=check), tid)
            await self.desktop({'action':'open', 'thread_id':tid}, check=check)
            before = await self.desktop({'action':'settings', 'thread_id':tid}, check=check)
        patch, flag = await self.settings_patch(args, check)
        after = before
        if patch or flag is not None:
            request = {'action':'update_settings', 'thread_id':tid, 'thread_settings':patch}
            if flag is not None: request['daybreak_enabled'] = flag
            after = await self.desktop(request, check=check)
        return self.settings_result(args, before, after, patch, flag)

    async def desktop_action(self, args, channel_id, check=None):
        async with self.action_lock:
            check_request(check)
            if 'wake' in args and type(args['wake']) is not bool:
                raise ValueError('wake must be true or false')
            action, thread_id = args['action'], args['thread_id']
            request = {'action': action, 'thread_id': thread_id,
                **({'prompt': args.get('prompt')} if action == 'send' else {}),
                **({k: args.get(k) for k in ('request_id', 'answers')} if action == 'answer' else {})}
            try:
                reply = await self.desktop(request, check=check)
            except DesktopError as error:
                # no_gui_owner is returned before any instruction is submitted.
                # Never replay after a timeout, disconnected IPC or ambiguous error.
                if error.code != 'no_gui_owner':
                    raise
                if action == 'status':
                    return await self.stored_history(thread_id)
                if action != 'send':
                    raise
                prompt = args.get('prompt')
                if not isinstance(prompt, str) or not 1 <= len(prompt) <= 16000:
                    raise ValueError('Provide prompt (1–16000 characters)')
                # Validate a real stored ID before asking the GUI to load it.
                await self.rpc('thread/read', {'threadId': thread_id, 'includeTurns': False}, check=check)
                await self.desktop({'action': 'open', 'thread_id': thread_id}, check=check)
                reply = await self.desktop(request, check=check)
            if action not in ('send', 'answer'):
                return reply
            job = next((j for j in self.jobs.values()
                if j.get('source') == 'desktop' and j.get('thread_id') == thread_id), None)
            if job is None:
                # A GUI job is routing metadata for this existing thread, not a new Codex task.
                if len(self.jobs) >= 16:
                    old = next((key for key, value in self.jobs.items() if value['state'] not in ACTIVE), None)
                    if old is None:
                        return dict(reply, message=reply['message'] + ' No notification slot available; use thread_id with status.')
                    self.jobs.pop(old)
                job = {'job_id': secrets.token_hex(4), 'thread_id': thread_id, 'source': 'desktop'}
                self.jobs[job['job_id']] = job
            job.update(turn_id=reply.get('turn_id'), state='running', phase='GUI instruction accepted',
                channel=channel_id, started_at=time.time(), messages={},
                _desktop_revision=secrets.token_hex(8),
                baseline_turn_id=reply.get('previous_turn_id'), send_mode=reply.get('mode'))
            self.arm_callback(job, args, channel_id)
            self.save()
            self.monitor_desktop(job)
            return dict(reply, job_id=job['job_id'], wake=bool(job.get('_wake')),
                notification=('Questions/results resume this owner conversation when it is idle; do not poll continuously'
                              if job.get('_wake') else 'Completion/input notices go to Discord; EVO is not automatically resumed'))

    async def stored_history(self, thread_id):
        """Read a small stored tail without acquiring/resuming a GUI conversation."""
        thread = (await self.rpc('thread/read', {'threadId': thread_id, 'includeTurns': False}))['thread']
        page = await self.rpc('thread/turns/list', {'threadId': thread_id, 'limit': 3,
            'sortDirection': 'desc', 'itemsView': 'summary'})
        turns = page.get('data', [])
        messages = []
        for turn in reversed(turns):
            for item in turn.get('items', []):
                kind = item.get('type')
                if kind == 'agentMessage':
                    text = item.get('text', '')
                elif kind == 'userMessage':
                    text = '\n'.join(c.get('text', '') for c in item.get('content', []) if c.get('type') == 'text')
                else:
                    continue
                if not text:
                    continue
                messages.append({'turn_id': turn['id'], 'role': 'user' if kind == 'userMessage' else 'assistant',
                    'phase': item.get('phase'), 'text': clean(text[:2000]), 'truncated': len(text) > 2000})
        latest_user = next((m for m in reversed(messages) if m['role'] == 'user'), None)
        selected = messages[-8:]
        if latest_user is not None and not any(m is latest_user for m in selected):
            selected = [latest_user, *selected[-7:]]
        recent, budget = [], 6000
        reserved = len(latest_user['text']) if latest_user is not None else 0
        for message in reversed(selected):
            room = budget if message is latest_user else max(0, budget - reserved)
            if not room:
                continue
            text = message['text'][:room]
            recent.insert(0, dict(message, text=text, truncated=message['truncated'] or len(text) < len(message['text'])))
            budget -= len(text)
            if message is latest_user:
                reserved = 0
        latest = turns[0] if turns else {}
        error = clean((latest.get('error') or {}).get('message', ''))[:500]
        return {'thread_id': thread_id, 'title': thread.get('name') or thread.get('title'), 'cwd': thread.get('cwd'),
            'source': 'stored_history', 'gui_attached': False, 'runtime_status': thread.get('status', {}).get('type'),
            'turn_id': latest.get('id'), 'turn_status': latest.get('status'), 'phase': latest.get('status', 'idle'),
            'recent_messages': recent, 'answer': next((m['text'] for m in reversed(recent) if m['role'] == 'assistant'), ''),
            'answer_is_partial': latest.get('status') == 'inProgress', 'turn_error': error or None,
            'pending_requests': [], 'conversation_view': {'source': 'stored_history_tail', 'complete_history': False,
                'live_status_available': False, 'description': 'Stored recent messages; send loads this same chat in the app. Pending live requests are unavailable until loaded.'}}

    async def read_history(self, thread_id, cursor=None, check=None):
        """Page visible messages, including long answers, without loading a chat."""
        native_cursor, expected_turn, index, offset = None, None, 0, 0
        if cursor is not None:
            try:
                if not isinstance(cursor, str) or len(cursor) > 4096 or not cursor.startswith('h1:'):
                    raise ValueError()
                data = json.loads(base64.urlsafe_b64decode(cursor[3:] + '=' * (-len(cursor[3:]) % 4)))
                saved_id, native_cursor, expected_turn, index, offset = data
                if (saved_id != thread_id or not isinstance(expected_turn, (str, type(None)))
                        or not isinstance(native_cursor, (str, type(None)))
                        or type(index) is not int or type(offset) is not int or min(index, offset) < 0):
                    raise ValueError()
            except (ValueError, TypeError, UnicodeError):
                raise ValueError('Use next_cursor from read of this same chat') from None
        params = {'threadId': thread_id, 'limit': 1, 'sortDirection': 'desc', 'itemsView': 'summary'}
        if native_cursor is not None:
            params['cursor'] = native_cursor
        metadata, page = await asyncio.gather(
            self.rpc('thread/read', {'threadId': thread_id, 'includeTurns': False}, check=check),
            self.rpc('thread/turns/list', params, check=check))
        thread = metadata['thread']
        turns = page.get('data', [])
        turn = turns[0] if turns else {}
        if expected_turn is not None and expected_turn != turn.get('id'):
            raise ValueError('History changed while paging; read again without cursor')
        messages = []
        for item in turn.get('items', []):
            kind = item.get('type')
            if kind == 'agentMessage':
                text = item.get('text', '')
            elif kind == 'userMessage':
                text = '\n'.join(c.get('text', '') for c in item.get('content', []) if c.get('type') == 'text')
            else:
                continue
            if text:
                # Redact before slicing so paging cannot expose parts of a secret.
                messages.append({'role': 'user' if kind == 'userMessage' else 'assistant',
                                 'phase': item.get('phase'), 'text': clean(text)})
        if index > len(messages) or (index < len(messages) and offset > len(messages[index]['text'])):
            raise ValueError('History changed while paging; read again without cursor')
        recent, room = [], 6000
        while index < len(messages) and room and len(recent) < 8:
            message = messages[index]
            text = message['text'][offset:offset + room]
            end = offset + len(text)
            recent.append(dict(message, text=text, continued=offset > 0, truncated=end < len(message['text'])))
            room -= len(text)
            if end < len(message['text']):
                offset = end
                break
            index, offset = index + 1, 0
        if index < len(messages):
            continuation = [thread_id, native_cursor, turn['id'], index, offset]
        elif page.get('nextCursor'):
            continuation = [thread_id, page['nextCursor'], None, 0, 0]
        else:
            continuation = None
        next_cursor = ('h1:' + base64.urlsafe_b64encode(json.dumps(continuation).encode()).decode().rstrip('=')
                       if continuation is not None else None)
        result = {'thread_id': thread_id, 'title': clean(thread.get('name') or thread.get('title') or ''),
            'cwd': thread.get('cwd'), 'source': 'stored_history', 'turn_id': turn.get('id'),
            'turn_status': turn.get('status'), 'recent_messages': recent, 'next_cursor': next_cursor,
            'history_order': 'newest turn first; messages within each turn are chronological',
            'gui_attached': False, 'live_status_available': False}
        if cursor is None:
            try:
                live = await self.desktop({'action': 'status', 'thread_id': thread_id}, check=check)
                result.update({key: live[key] for key in ('gui_attached', 'runtime_status', 'runtime_flags',
                    'phase', 'pending_requests', 'activity') if key in live})
                result['live_status_available'] = True
            except DesktopError as error:
                if error.code != 'no_gui_owner':
                    result['live_error'] = clean(str(error))[:350]
            except RuntimeError as error:
                result['live_error'] = clean(str(error))[:350]
        return result

    async def project_catalog(self):
        projects, cursor = [], None
        for _ in range(5):
            result = await self.rpc('project/list', {'limit': 100, 'cursor': cursor, 'sortKey': 'position'})
            projects.extend(p for item in result.get('data', []) if (p := project_snapshot(item)) is not None)
            cursor = result.get('nextCursor')
            if not cursor:
                return projects
        raise ValueError('Project catalog exceeds 500 entries; selection is unavailable')

    async def open_new_chat(self, args, channel_id, check=None):
        import discord
        if not channel_id or not CLIENT or not CLIENT.is_ready():
            raise ValueError('A live owner Discord channel is required')
        if len(self.new_chat_views) >= 4:
            raise ValueError('Four selection cards are already open; cancel one first')
        prompt = args.get('prompt') or ''
        if not isinstance(prompt, str) or len(prompt) > 4000:
            raise ValueError('New-chat instructions must fit 4000 characters; send can add more later')
        projects = await self.project_catalog()
        models = await self.model_catalog(check)
        # Validate requested settings before posting a card; preserve them through the picker.
        if args.get('mode') is not None:
            preset, _ = await self.settings_patch(dict(args, action='daybreak'), check)
            args = dict(args, **({'model': preset['model']} if 'model' in preset else {}))
        if args.get('policy') is not None or args.get('sandbox') is not None:
            await self.settings_patch(dict(args, action='approval'), check)
        selected = await self.select_project(args['project']) if args.get('project') else None
        model = args.get('model') or ''
        if model and not any(m['id'] == model for m in models):
            raise ValueError('Unknown model; use models or the selection card')
        check_request(check)
        bridge, ident = self, secrets.token_hex(4)

        class Picker(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=600)
                self.project = selected
                self.cwd = selected['default_cwd'] if selected else bridge.settings['cwd']
                self.model, self.prompt = model, prompt
                self.project_page = self.model_page = self.root_page = 0
                self.message = None
                self.finished = self.launching = False
                self.final_suffix = ''
                self.lock = asyncio.Lock()
                self.target_select = discord.ui.Select(placeholder='프로젝트 또는 임시 대화', row=0)
                self.root_select = discord.ui.Select(placeholder='작업 경로 선택', row=1)
                self.model_select = discord.ui.Select(placeholder='모델 선택', row=2)
                self.target_select.callback = self.choose_target
                self.root_select.callback = self.choose_root
                self.model_select.callback = self.choose_model
                self.add_item(self.target_select); self.add_item(self.root_select); self.add_item(self.model_select)
                self.refresh_options()

            async def interaction_check(self, interaction):
                if str(interaction.user.id) != OWNER:
                    await interaction.response.send_message('오너만 선택할 수 있어.', ephemeral=True)
                    return False
                if self.finished or self.launching:
                    await interaction.response.send_message('이미 처리된 선택이야.', ephemeral=True)
                    return False
                return True

            def refresh_options(self):
                self.target_select.options = [discord.SelectOption(label='임시 대화 · 기록 저장 안 함', value='temporary',
                    description=bridge.settings['cwd'][:100], default=self.project is None)] + [
                    discord.SelectOption(label=p['name'][:100], value=p['project_id'],
                        description=p['default_cwd'][:100], default=bool(self.project and p['project_id'] == self.project['project_id']))
                    for p in projects[self.project_page * 24:(self.project_page + 1) * 24]]
                roots = self.project['roots'] if self.project else [bridge.settings['cwd']]
                self.root_select.options = [discord.SelectOption(label=ntpath.basename(p)[:100] or p[:100],
                    value=str(i), description=p[:100], default=p == self.cwd)
                    for i, p in enumerate(roots) if self.root_page * 25 <= i < (self.root_page + 1) * 25]
                self.model_select.options = [discord.SelectOption(label='Windows Codex 기본 모델', value='default', default=not self.model)] + [
                    discord.SelectOption(label=str(m.get('displayName') or m['id'])[:100], value=m['id'],
                        default=m['id'] == self.model) for m in models[self.model_page * 24:(self.model_page + 1) * 24]]
                self.previous_projects.disabled = self.project_page == 0
                self.next_projects.disabled = (self.project_page + 1) * 24 >= len(projects)
                self.previous_models.disabled = self.model_page == 0
                self.next_models.disabled = (self.model_page + 1) * 24 >= len(models)
                self.more_roots.disabled = len(roots) <= 25
                self.launch.disabled = not self.prompt.strip()
                for button, needed in [(self.previous_projects, len(projects) > 24),
                        (self.next_projects, len(projects) > 24),
                        (self.previous_models, len(models) > 24),
                        (self.next_models, len(models) > 24), (self.more_roots, len(roots) > 25)]:
                    if needed and button not in self.children: self.add_item(button)
                    elif not needed and button in self.children: self.remove_item(button)

            def content(self, suffix=''):
                target = ('프로젝트: ' + self.project['name']) if self.project else '임시 대화: 서버 재시작 시 사라짐'
                target += '\n경로: ' + self.cwd
                return '**Codex 새 대화**\n' + clean(target)[:850] + '\n모델: ' + clean(self.model or 'Windows 기본값') \
                    + '\n작업: ' + (clean(self.prompt)[:650] or '작업 입력 버튼으로 적어줘.') \
                    + '\n' + (suffix or self.final_suffix or '선택 후 시작을 눌러줘. 임시 대화도 작업 파일은 자동 삭제하지 않아.')

            async def choose_target(self, interaction):
                value = self.target_select.values[0]
                self.project = next((p for p in projects if p['project_id'] == value), None)
                self.cwd = self.project['default_cwd'] if self.project else bridge.settings['cwd']
                self.root_page = 0
                self.refresh_options()
                await interaction.response.edit_message(content=self.content(), view=self)

            async def choose_root(self, interaction):
                roots = self.project['roots'] if self.project else [bridge.settings['cwd']]
                self.cwd = roots[int(self.root_select.values[0])]
                self.refresh_options()
                await interaction.response.edit_message(content=self.content(), view=self)

            async def choose_model(self, interaction):
                value = self.model_select.values[0]
                self.model = '' if value == 'default' else value
                self.refresh_options()
                await interaction.response.edit_message(content=self.content(), view=self)

            async def move(self, interaction, kind, delta):
                name = kind + '_page'
                count = len(projects if kind == 'project' else models)
                setattr(self, name, max(0, min((max(1, count) - 1) // 24, getattr(self, name) + delta)))
                self.refresh_options()
                await interaction.response.edit_message(content=self.content(), view=self)

            @discord.ui.button(label='프로젝트 ◀', row=3)
            async def previous_projects(self, interaction, button): await self.move(interaction, 'project', -1)
            @discord.ui.button(label='프로젝트 ▶', row=3)
            async def next_projects(self, interaction, button): await self.move(interaction, 'project', 1)
            @discord.ui.button(label='모델 ◀', row=3)
            async def previous_models(self, interaction, button): await self.move(interaction, 'model', -1)
            @discord.ui.button(label='모델 ▶', row=3)
            async def next_models(self, interaction, button): await self.move(interaction, 'model', 1)

            @discord.ui.button(label='경로 더보기', row=3)
            async def more_roots(self, interaction, button):
                roots = self.project['roots'] if self.project else [bridge.settings['cwd']]
                self.root_page = (self.root_page + 1) % ((len(roots) + 24) // 25)
                self.refresh_options()
                await interaction.response.edit_message(content=self.content(), view=self)

            @discord.ui.button(label='작업 입력', row=4)
            async def edit_prompt(self, interaction, button):
                view = self
                class Prompt(discord.ui.Modal, title='Codex 작업'):
                    text = discord.ui.TextInput(label='지시', style=discord.TextStyle.paragraph, max_length=4000)
                    def __init__(self):
                        super().__init__(timeout=300)
                        self.text.default = view.prompt
                    async def on_submit(self, interaction):
                        if str(interaction.user.id) != OWNER or view.finished or view.launching:
                            await interaction.response.send_message('종료된 선택이야.', ephemeral=True); return
                        view.prompt = str(self.text.value)
                        view.refresh_options()
                        await interaction.response.edit_message(content=view.content(), view=view)
                await interaction.response.send_modal(Prompt())

            async def finish(self, suffix):
                self.finished = True
                self.final_suffix = suffix
                for child in self.children: child.disabled = True
                bridge.new_chat_views.pop(ident, None)
                self.stop()
                if self.message: await self.message.edit(content=self.content(suffix), view=self)

            @discord.ui.button(label='시작', style=discord.ButtonStyle.success, row=4)
            async def launch(self, interaction, button):
                async with self.lock:
                    if self.finished or self.launching:
                        await interaction.response.send_message('이미 처리 중이야.', ephemeral=True); return
                    await interaction.response.defer()
                    self.launching = True
                    launch_args = {'action': '_start', 'prompt': self.prompt, 'cwd': self.cwd}
                    if 'wake' in args: launch_args['wake'] = args['wake']
                    for key in ('mode', 'policy', 'sandbox', 'compact'):
                        if key in args: launch_args[key] = args[key]
                    if self.project: launch_args['project'] = self.project['project_id']
                    if self.model: launch_args['model'] = self.model
                    try:
                        result = await bridge.action(launch_args, channel_id)
                    except Exception as error:
                        await self.finish('시작 실패 또는 결과 미확인: ' + clean(str(error))[:250] + '\nlist/status로 확인해줘. 자동 재전송하지 않았어.')
                    else:
                        # A message-edit failure must not relabel an already accepted task as failed.
                        await self.finish('시작됨 · 작업 `' + result['job_id'] + '` · 대화 `' + result['thread_id'] + '`')

            @discord.ui.button(label='취소', style=discord.ButtonStyle.danger, row=4)
            async def cancel(self, interaction, button):
                async with self.lock:
                    await interaction.response.defer()
                    if not self.launching: await self.finish('취소됨 · 대화는 만들지 않았어.')

            async def on_timeout(self):
                async with self.lock:
                    if not self.launching: await self.finish('선택 만료 · 대화는 만들지 않았어.')

        view = Picker()
        self.new_chat_views[ident] = view
        try:
            check_request(check)
            view.message = await self.send({'channel': channel_id}, view.content(), view=view)
            if view.finished: await view.message.edit(content=view.content(), view=view)
        except Exception:
            self.new_chat_views.pop(ident, None); view.stop(); raise
        return {'selection_id': ident, 'state': 'waiting_for_selection',
                'message': 'Selection card posted. The owner chooses a project/path or temporary chat, model and prompt, '
                           'then clicks Start. No conversation has been created yet. Do not poll or open another card.'}

    async def select_project(self, selector):
        if not isinstance(selector, str) or not selector.strip() or len(selector) > 200:
            raise ValueError('project must be an exact project ID or project name from projects')
        selector = selector.strip()
        if re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', selector):
            result = await self.rpc('project/read', {'projectId': selector})
            project = project_snapshot(result['project'])
            if project is None:
                raise ValueError('This is not a supported local Windows project; use projects')
            return project
        matches, cursor = [], None
        for _ in range(5):
            result = await self.rpc('project/list', {'limit': 100, 'cursor': cursor, 'sortKey': 'position'})
            for item in result.get('data', []):
                project = project_snapshot(item)
                if project and project['name'].casefold() == selector.casefold():
                    matches.append(project)
            cursor = result.get('nextCursor')
            if not cursor:
                break
        if cursor:
            raise ValueError('Project catalog is too large for name lookup; use an exact project ID')
        if len(matches) != 1:
            raise ValueError('Project name is missing or ambiguous; use projects and its exact project_id')
        # Re-read before creating a thread: a project may have moved or been deleted.
        result = await self.rpc('project/read', {'projectId': matches[0]['project_id']})
        project = project_snapshot(result['project'])
        if project is None:
            raise ValueError('Project is no longer a supported local Windows project')
        return project

    async def files_or_command(self, args, check=None):
        if any(args.get(k) is not None for k in ('thread_id', 'job_id', 'project')):
            raise ValueError('File/command actions use path or cwd directly, without a chat or project selector')

        def windows_path(value):
            if (not isinstance(value, str) or len(value) > 4096 or '\x00' in value
                    or not re.match(r'^[A-Za-z]:[\\/]', value)):
                raise ValueError('Use an absolute Windows path, for example C:\\Users\\name\\project\\report.md')
            return ntpath.normpath(value)

        def integer(name, default, low, high):
            value = args.get(name, default)
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f'{name} must be an integer from {low} to {high}')
            return value

        action = args['action']
        if action == 'file_list':
            path = windows_path(args.get('path'))
            offset = integer('offset', 0, 0, 1000000)
            limit = integer('limit', 80, 1, 200)
            result = await self.rpc('fs/readDirectory', {'path': path}, check=check)
            entries = sorted(result['entries'], key=lambda e: (not e['isDirectory'], e['fileName'].casefold()))
            query = str(args.get('search') or '').casefold()
            if query:
                entries = [e for e in entries if query in e['fileName'].casefold()]
            page = entries[offset:offset + limit]
            return {'source': 'windows_filesystem', 'path': path, 'entries': page,
                    'total': len(entries), 'next_offset': offset + len(page) if offset + len(page) < len(entries) else None}

        if action == 'file_read':
            path = windows_path(args.get('path'))
            start = integer('start_line', 1, 1, 1000000)
            count = integer('limit', 200, 1, 200)
            report = await self.desktop({'path': path, 'start_line': start, 'limit': count},
                                        check=check, endpoint='/files')
            return dict(report, source='windows_filesystem')

        command = args.get('command')
        if (not isinstance(command, list) or not 1 <= len(command) <= 64
                or not all(isinstance(v, str) and '\x00' not in v and len(v) <= 16000 for v in command)
                or not command[0]):
            raise ValueError('exec requires command as an argv array, such as ["git", "status", "--short"]')
        # Windows sandbox rejects custom outputBytesCap; keep its native cap and
        # bound the result returned to the UI/model separately.
        params = {'command': command, 'timeoutMs': integer('timeout_ms', 10000, 100, 15000)}
        if args.get('cwd') is not None:
            params['cwd'] = windows_path(args['cwd'])
        # Omitting sandboxPolicy inherits this authenticated server's configured policy.
        result = await self.rpc('command/exec', params, check=check)
        stdout, stderr = clean(result.get('stdout', '')), clean(result.get('stderr', ''))
        return {'source': 'windows_command', 'exit_code': result['exitCode'],
                'stdout': stdout[:16000], 'stderr': stderr[:16000],
                'truncated': len(stdout) > 16000 or len(stderr) > 16000,
                'timeout_ms': params['timeoutMs'],
                'shell_persistent': False}

    async def action(self, args, channel_id, check=None, turn=None):
        check_request(check)
        args = slash_request(args)
        from .. import context
        turn = turn or context.owner_identity()
        if turn and turn.cron and args['action'] not in SCHEDULED_ACTIONS:
            raise PermissionError('Read-only scheduled checks cannot change Codex settings/goals')
        if args['action'] == '_watch':
            async with self.action_lock: return await self.watch_chat(args, channel_id, check)
        if args['action'] in ('_goal', '_compact'):
            async with self.action_lock: return await self.native_slash(args, check)
        if args.get('action') == 'read':
            thread_id = args.get('thread_id')
            if not thread_id and args.get('job_id'):
                job = self.jobs.get(args['job_id'], {})
                if job.get('source') == 'desktop':
                    thread_id = job.get('thread_id')
            if thread_id:
                return await self.read_history(thread_id, args.get('cursor'), check=check)
            args = dict(args, action='status')
        action = args.get('action', 'status')
        if action in DYNAMIC_SETTINGS_ACTIONS or action == '_model':
            async with self.action_lock:
                return await self.chat_settings(args, check)
        if action == 'servers':
            return {'servers': server_catalog()}
        if action in ('file_list', 'file_read', 'exec'):
            return await self.files_or_command(args, check=check)
        if args.get('project') is not None and action not in ('start', 'new', '_start'):
            raise ValueError('project selects a project for start; existing chats use thread_id')
        if action in ('new', 'start'):
            if args.get('thread_id') or args.get('job_id'):
                raise ValueError('New conversations do not accept an existing thread_id/job_id; use send')
            return await self.open_new_chat(args, channel_id, check=check)
        if action == 'projects':
            params = {'limit': min(12, max(1, int(args.get('limit', 12)))), 'sortKey': 'position'}
            if args.get('cursor'): params['cursor'] = args['cursor']
            result = await self.rpc('project/list', params)
            projects = [p for item in result.get('data', []) if (p := project_snapshot(item)) is not None]
            return {'projects': projects, 'next_cursor': result.get('nextCursor'),
                    'message': 'Existing local Windows projects; start with project_id or unique exact name in project. '
                               'ChatGPT cloud projects are excluded. No project or chat was created.'}
        if action == 'models':
            reply = await self.rpc('model/list', {'limit': 100})
            return {'models': [{'id': m['id'], 'name': m.get('displayName'),
                                'default': m.get('isDefault')} for m in reply['data']]}
        if action == 'list':
            limit = min(12, max(1, int(args.get('limit', 8))))
            params = {'limit': limit, 'sortKey': 'updated_at', 'useStateDbOnly': True}
            if args.get('cursor'): params['cursor'] = args['cursor']
            if args.get('search'): params['searchTerm'] = str(args['search'])[:200]
            stored = await self.rpc('thread/list', params)
            threads = [{'thread_id': t['id'], 'title': t.get('title') or t.get('name'), 'cwd': t.get('cwd'),
                'project_id': t.get('projectId'),
                'stored_status': t.get('status', {}).get('type'), 'updated_at': epoch_seconds(t.get('updatedAt')),
                'gui_attached': False} for t in stored.get('data', [])]
            desktop_error = None
            try:
                live = await self.desktop({'action': 'inspect', 'thread_ids': [t['thread_id'] for t in threads]})
                by_id = {t['thread_id']: t for t in live['threads']}
                for thread in threads:
                    update = by_id.get(thread['thread_id'], {})
                    if update.get('gui_attached'):
                        # List timestamps, ordering and cursors share the DB clock.
                        thread.update({key: value for key, value in update.items() if key != 'updated_at'})
                    elif update.get('error_code') == 'no_gui_owner':
                        thread['history_available'] = True
                    else:
                        thread['gui_error'] = update.get('error', 'GUI inspection unavailable')
            except Exception as error:
                desktop_error = clean(str(error))[:350]
            return {'jobs': [snapshot(j) for j in self.jobs.values()][-16:],
                'threads': threads, 'next_cursor': stored.get('nextCursor'),
                **({'desktop_error': desktop_error} if desktop_error else {})}
        if args.get('thread_id'):
            tracked = next((j for j in self.jobs.values() if j.get('source') != 'desktop'
                            and j.get('thread_id') == args['thread_id']), None)
            if tracked is not None:
                return await self.action(dict(args, thread_id=None, job_id=tracked['job_id']),
                                         channel_id, check=check, turn=turn)
            if action not in ('status', 'send', 'answer', 'stop'):
                raise ValueError('thread_id is for status/send/answer/stop of an existing chat')
            return await self.desktop_action(args, channel_id, check=check)
        if action == '_start':
            prompt = args.get('prompt', '')
            if not isinstance(prompt, str) or not 1 <= len(prompt) <= 16000:
                raise ValueError('Provide prompt (1–16000 characters)')
            async with self.action_lock:
                check_request(check)
                for existing in list(self.jobs.values()):
                    if existing.get('ephemeral') and existing['state'] == 'connection_lost':
                        await self.status(existing)
                if any(j.get('source') != 'desktop' and j['state'] in ACTIVE for j in self.jobs.values()):
                    raise ValueError('A Codex task is active; use status/send/stop before starting another')
                project = await self.select_project(args['project']) if args.get('project') is not None else None
                cwd = args.get('cwd') or (project['default_cwd'] if project else self.settings['cwd'])
                if not isinstance(cwd, str) or not re.match(r'^[A-Za-z]:[\\/]', cwd) or '\x00' in cwd:
                    raise ValueError('cwd must be an absolute Windows drive path')
                if project:
                    candidate = ntpath.normcase(ntpath.normpath(cwd))
                    inside = False
                    for root in project['roots']:
                        root = ntpath.normcase(ntpath.normpath(root))
                        try:
                            inside = inside or ntpath.commonpath([root, candidate]) == root
                        except ValueError:
                            pass
                    if not inside:
                        raise ValueError('cwd must be inside a root of the selected project')
                if len(self.jobs) >= 16:
                    old = next((key for key, value in self.jobs.items() if value['state'] not in ACTIVE), None)
                    if old is None: raise ValueError('All 16 notification slots are active; wait or stop a task first')
                    self.jobs.pop(old)
                params = {'cwd': cwd, 'ephemeral': project is None}
                if project:
                    params.update(projectId=project['project_id'], runtimeWorkspaceRoots=project['roots'], ephemeral=False)
                response = await self.start_native_thread(args, params, check)
                if bool(response['thread'].get('ephemeral')) != (project is None):
                    raise ValueError('Codex did not confirm the selected persistence mode; no prompt was submitted')
                if project and response['thread'].get('projectId') != project['project_id']:
                    raise ValueError('Codex did not confirm project membership; no prompt was submitted')
                job = {'job_id': secrets.token_hex(4), 'thread_id': response['thread']['id'],
                    'turn_id': None, 'state': 'starting', 'phase': 'Starting turn',
                    'model': response.get('model'), 'daybreak_enabled': response['thread'].get('daybreakEnabled'),
                    'cwd': response.get('cwd', cwd),
                    'channel': channel_id, 'started_at': time.time(), 'messages': {}, 'source': 'app_server',
                    'project_id': response['thread'].get('projectId'), 'project_name': project['name'] if project else None,
                    'ephemeral': project is None}
                self.jobs[job['job_id']] = job
                self.arm_callback(job, args, channel_id)
                try:
                    started = await self.rpc('turn/start', turn_input(job['thread_id'], prompt, job.get('daybreak_enabled')), check=check)
                    job['turn_id'] = started['turn']['id']
                    if job['state'] == 'starting': job['state'] = 'running'
                except Exception:
                    job['state'] = 'connection_lost'; self.save(); raise
                self.save()
                self.monitor_desktop(job)
                return dict(snapshot(job), wake=job['_wake'], message='Created the selected Windows Codex chat and started its turn on the task server; '
                    'completion will be delivered to this Discord channel. This is a separate conversation, not a subagent.',
                    **({'desktop_url': 'codex://threads/' + job['thread_id']} if project else {}))
        ident = args.get('job_id')
        if ident not in self.jobs: raise ValueError('Unknown job_id; use list')
        job = self.jobs[ident]
        if job.get('source') == 'desktop':
            if action == 'status':
                try:
                    reply = await self.status(job)
                except DesktopError as error:
                    if error.code != 'no_gui_owner':
                        raise
                    return dict(await self.stored_history(job['thread_id']), job_id=ident)
                if job['state'] in ACTIVE: self.monitor_desktop(job)
                return reply
            return await self.desktop_action(dict(args, thread_id=job['thread_id']), channel_id, check=check)
        if action == 'answer':
            self.arm_callback(job, args, channel_id)
            await self.resolve_input(job, args.get('request_id'), args.get('answers'))
            return dict(snapshot(job), wake=job['_wake'], message='Answer sent; use status for confirmation')
        if action == 'status': return await self.status(job)
        if action == 'stop':
            if not job.get('turn_id'): raise ValueError('No turn to interrupt')
            await self.rpc('turn/interrupt', {'threadId': job['thread_id'], 'turnId': job['turn_id']}, check=check)
            return {'job_id': ident, 'message': 'Interrupt requested; check status for confirmation'}
        if action == 'send':
            prompt = args.get('prompt', '')
            if not isinstance(prompt, str) or not 1 <= len(prompt) <= 16000:
                raise ValueError('Provide prompt (1–16000 characters)')
            async with self.action_lock:
                check_request(check)
                await self.status(job)
                self.arm_callback(job, args, channel_id)
                check_request(check)
                if job.get('state') == 'expired':
                    raise ValueError('Temporary conversation expired; use remote_control protocol=codex action=start for another one')
                if job['state'] in ACTIVE:
                    await self.rpc('turn/steer', {'threadId': job['thread_id'],
                        'expectedTurnId': job['turn_id'], 'input': [{'type': 'text', 'text': prompt}]}, check=check)
                else:
                    if any(j.get('source') != 'desktop' and j['state'] in ACTIVE for j in self.jobs.values()):
                        raise ValueError('Another Codex task is active')
                    if not job.get('ephemeral'):
                        self._thread(await self.rpc('thread/resume', {'threadId': job['thread_id'], 'excludeTurns': True}, check=check), job['thread_id'])
                    check_request(check)
                    job.update(state='starting', phase='Starting turn', messages={}, channel=channel_id)
                    response = await self.rpc('turn/start', turn_input(job['thread_id'], prompt, job.get('daybreak_enabled')), check=check)
                    job['turn_id'] = response['turn']['id']
                    if job['state'] == 'starting': job['state'] = 'running'
                self.save()
                self.monitor_desktop(job)
                return dict(snapshot(job), wake=job['_wake'])
        raise ValueError('Use projects/start/status/read/send/stop/list/models')


class SSHConnection:
    """WebSocket through OpenSSH stdio; closing it also reaps the local SSH process."""
    def __init__(self, ws, process): self.ws, self.process = ws, process
    def __aiter__(self): return self.ws.__aiter__()
    async def send(self, text): await self.ws.send(text)
    async def close(self):
        try: await self.ws.close()
        finally:
            if self.process.returncode is None:
                try: self.process.terminate()
                except ProcessLookupError: pass
            try: await asyncio.wait_for(self.process.wait(), 5)
            except asyncio.TimeoutError:
                self.process.kill(); await self.process.wait()


class AppServer(Bridge):
    """Linux app-server over private SSH: persistent events, bounded read/settings RPCs."""

    def __init__(self, settings, server_id, default=False):
        super().__init__(settings, server_id, default)
        self.default = default
        self.sync_lock = threading.Lock()

    async def open_ws(self):
        from . import ssh
        from websockets.asyncio.client import connect
        entry = ssh.configuration()['servers'].get(self.server_id)
        if entry is None: raise ValueError('SSH login is not configured for this Codex instance')
        command = shlex.join([self.settings.get('rpc', '/home/opc/bin/codex-rpc'), '@stdio'])
        argv, env = ssh.transport(entry, command)
        client, child = socket.socketpair()
        process = None
        try:
            process = await asyncio.create_subprocess_exec(*argv, stdin=child, stdout=child,
                stderr=asyncio.subprocess.DEVNULL, env=env, start_new_session=True)
            child.close()
            client.setblocking(False)
            ws = await connect('ws://localhost', sock=client, open_timeout=20,
                ping_interval=20, ping_timeout=30, max_size=8 * 2**20, max_queue=8, proxy=None)
            return SSHConnection(ws, process)
        except BaseException:
            client.close(); child.close()
            if process is not None:
                if process.returncode is None: process.terminate()
                await process.wait()
            raise

    async def native_action(self, args, channel, check):
        check_request(check)
        action = args['action']
        if 'wake' in args and type(args['wake']) is not bool: raise ValueError('wake must be true or false')
        tid = args.get('thread_id')
        if not tid and args.get('job_id'):
            tid = self.jobs.get(args['job_id'], {}).get('thread_id')
        if tid is not None and (not isinstance(tid, str) or not tid.strip() or len(tid) > 200 or '\0' in tid):
            raise ValueError('Use a thread_id from list')
        if action == 'send' and not tid:
            raise ValueError('send needs thread_id or job_id; use start for a new chat')
        if action == 'answer':
            job = next((j for j in self.jobs.values() if j['thread_id'] == tid), None)
            if job is None: raise ValueError('No tracked question for this thread; inspect status')
            self.arm_callback(job, args, channel)
            await self.resolve_input(job, args.get('request_id'), args.get('answers'))
            self.save(); self.monitor_desktop(job)
            return dict(snapshot(job), wake=job['_wake'], message='Answer sent; use status for confirmation')
        prompt = args.get('prompt')
        if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 16000:
            raise ValueError('Provide prompt (1–16000 characters)')
        if action in ('start', 'new') and (tid or args.get('job_id')):
            raise ValueError('start creates a new chat; use send for an existing thread')
        if args.get('project') is not None:
            raise ValueError('Choose an absolute cwd for this daemon; GUI project selection is unavailable')
        if tid and 'ephemeral' in args:
            raise ValueError('ephemeral applies to a new chat only')
        if tid:
            thread = self._thread(await self.rpc('thread/resume',
                {'threadId': tid, 'excludeTurns': True}, check=check), tid)
        else:
            cwd = args.get('cwd') or self.settings.get('cwd')
            if not isinstance(cwd, str) or not cwd.startswith('/') or '\0' in cwd:
                raise ValueError('cwd must be an absolute Linux path')
            params = {'cwd': cwd}
            if 'ephemeral' in args:
                if type(args['ephemeral']) is not bool:
                    raise ValueError('ephemeral must be true or false')
                params['ephemeral'] = args['ephemeral']
            started = await self.start_native_thread(args, params, check)
            thread = self._thread(started)
            tid = thread['id']
            if 'ephemeral' in args and thread.get('ephemeral') is not args['ephemeral']:
                raise DesktopError('unconfirmed_persistence', 'Codex did not confirm persistence; no prompt submitted')
        job = next((j for j in self.jobs.values() if j['thread_id'] == tid), None)
        if job is None:
            if len(self.jobs) >= 16:
                old = next((k for k, v in self.jobs.items() if v['state'] not in ACTIVE), None)
                if old is None: raise ValueError('All 16 notification slots are active')
                self.jobs.pop(old)
            job = {'job_id': secrets.token_hex(4), 'thread_id': tid, 'source': 'app_server', 'messages': {}}
            self.jobs[job['job_id']] = job
        job.update(state='starting', phase='Submitting instruction', channel=channel,
                   started_at=time.time(), cwd=(thread.get('environments') or [{}])[0].get('cwd'),
                   model=thread.get('model'), daybreak_enabled=thread.get('daybreakEnabled'),
                   ephemeral=bool(thread.get('ephemeral')))
        self.arm_callback(job, args, channel)
        if (thread.get('status') or {}).get('type') == 'active':
            page = await self.rpc('thread/turns/list', {'threadId': tid, 'limit': 1}, check=check)
            active = next((t for t in page.get('data', []) if t.get('status') == 'inProgress'), None)
        else: active = None
        try:
            if active:
                turn = active
                await self.rpc('turn/steer', {'threadId': tid, 'expectedTurnId': turn['id'],
                    'input': [{'type': 'text', 'text': prompt}]}, check=check)
            else:
                job['messages'] = {}
                reply = await self.rpc('turn/start', turn_input(tid, prompt, thread.get('daybreakEnabled')), check=check)
                turn = reply.get('turn') or {}
                if not turn.get('id'):
                    raise DesktopError('outcome_unknown', 'No turn ID confirmed; inspect status before retrying')
            job['turn_id'] = turn['id']
            if job['state'] == 'starting': job['state'] = 'running'
        except BaseException:
            job['state'] = 'connection_lost'
            raise
        finally:
            self.save(); self.monitor_desktop(job)
        return dict(snapshot(job), wake=job['_wake'],
                    message='Accepted. Completion/questions resume EVO when idle; no polling needed.'
                    if job['_wake'] else 'Accepted. Completion notices go to Discord.')

    def _rpc(self, spec, params, timeout, check):
        from . import ssh
        from .. import context
        entry = ssh.configuration()['servers'].get(self.server_id)
        if entry is None: raise ValueError('Oracle SSH login is not configured')
        check_request(check)
        turn = context.owner_identity()
        if turn is None: raise PermissionError('Codex requests need an owner turn')
        blob = base64.b64encode(json.dumps(params, ensure_ascii=False).encode()).decode()
        rpc = self.settings.get('rpc', '/home/opc/bin/codex-rpc')
        out = json.loads(ssh.execute(entry, self.server_id,
                                     shlex.join([rpc, spec, 'b64:' + blob, str(timeout)]), timeout + 20, turn))
        check_request(check)
        if out.get('timed_out'):
            raise TimeoutError('Oracle Codex timed out; inspect current state before retrying')
        if out.get('output_truncated'):
            raise DesktopError('response_too_large', 'Oracle Codex response exceeded the transport limit; '
                               'inspect current state before retrying')
        text = (out.get('output') or '').strip()
        if out.get('exit_code') != 0:
            raise DesktopError('rpc_failed', clean(text or 'Oracle Codex RPC failed')[:300])
        try:
            reply = json.loads(text.splitlines()[-1]) if text else None
        except ValueError:
            reply = None
        if spec == '@multi':
            if not isinstance(reply, list) or len(reply) > len(params['calls']):
                raise DesktopError('invalid_response', 'Oracle Codex returned an incomplete batch')
            for call, result in zip(params['calls'], reply):
                self._result(result, call['method'])
            if len(reply) != len(params['calls']):
                raise DesktopError('invalid_response', 'Oracle Codex returned an incomplete batch')
        else:
            self._result(reply, spec)
        return reply

    @staticmethod
    def _result(reply, method):
        if not isinstance(reply, dict):
            raise DesktopError('invalid_response', 'Oracle Codex returned unreadable data for ' + method)
        if 'error' in reply:
            error = reply['error']
            message = error.get('message', 'RPC failed') if isinstance(error, dict) else str(error)
            raise DesktopError('rpc_failed', method + ': ' + clean(message)[:300])
        if method in ('thread/list', 'thread/turns/list', 'thread/items/list', 'model/list'):
            if not isinstance(reply.get('data'), list):
                raise DesktopError('invalid_response', 'Oracle Codex did not confirm a page for ' + method)
        if method in ('thread/start', 'thread/resume', 'thread/read'):
            if not isinstance(reply.get('thread'), dict) or not reply['thread'].get('id'):
                raise DesktopError('invalid_thread', 'Codex did not confirm this chat; use a thread_id from list')


    async def action(self, args, channel, check=None, turn=None):
        # run_in_executor does not carry contextvars into the worker thread, so the owner
        # turn is installed in an explicit copy that the worker runs inside.
        from .. import context
        if turn is None: turn = context.owner_identity()
        if turn is None or turn.cron and args['action'] not in SCHEDULED_ACTIONS:
            raise PermissionError('This Codex action needs an owner message')
        args = slash_request(args)
        if turn.cron and args['action'] not in SCHEDULED_ACTIONS:
            raise PermissionError('Read-only scheduled checks cannot change Codex settings/goals')
        if args['action'] == '_watch':
            async with self.action_lock: return await self.watch_chat(args, channel, check)
        if args['action'] in ('_goal', '_compact'):
            async with self.action_lock: return await self.native_slash(args, check)
        if turn.cron and args['action'] == 'send' and not args.get('thread_id'):
            raise PermissionError('Scheduled send requires an existing thread_id')
        if args['action'] in DYNAMIC_SETTINGS_ACTIONS or args['action'] == '_model':
            async with self.action_lock:
                return await self.native_settings(args, check)
        if args['action'] in ('send', 'start', 'new', 'answer'):
            async with self.action_lock:
                return await self.native_action(args, channel, check)
        if args['action'] in ('status', 'read'):
            args = dict(args)
            if not args.get('thread_id') and args.get('job_id') in self.jobs:
                args['thread_id'] = self.jobs[args['job_id']]['thread_id']
        loop = asyncio.get_running_loop()
        scope = contextvars.copy_context()
        if turn is not None: scope.run(context.current.set, turn)
        cancelled = threading.Event()
        def guard():
            check_request(check)
            if cancelled.is_set() or turn.cancelled.is_set():
                raise InterruptedError('Codex request interrupted before connection')
        def run():
            # Cancelling an asyncio executor future does not stop its worker.
            # Keep serialization until that worker exits, and never replay it.
            while not self.sync_lock.acquire(timeout=.1): guard()
            try:
                guard()
                return scope.run(self._sync, args, guard)
            finally:
                self.sync_lock.release()
        async with self.action_lock:
            guard()
            try:
                result = await loop.run_in_executor(None, run)
                job = next((j for j in self.jobs.values() if j['thread_id'] == args.get('thread_id')), None)
                if job and args['action'] in ('status', 'read'):
                    result['pending_requests'] = self.pending_requests(job)
                    if job['state'] == 'waiting_for_input': result['turn_status'] = 'waiting_for_input'
                return result
            except asyncio.CancelledError:
                cancelled.set()
                raise

    def _sync(self, args, check):
        action = args['action']
        if action == 'list':
            params = {'limit': min(12, max(1, int(args.get('limit', 8)))),
                      'sortKey': 'updated_at', 'useStateDbOnly': True}
            if args.get('search'): params['searchTerm'] = str(args['search'])[:200]
            if args.get('cursor'): params['cursor'] = str(args['cursor'])[:400]
            reply = self._rpc('thread/list', params, 20, check)
            rows = []
            for row in (reply.get('data') or [])[:12]:
                rows.append({'thread_id': row.get('id'),
                             'title': row.get('name') or (row.get('preview') or '')[:80] or None,
                             'cwd': (row.get('environments') or [{}])[0].get('cwd'),
                             'model': row.get('model'),
                             'stored_status': (row.get('status') or {}).get('type'),
                             'updated_at': epoch_seconds(row.get('updatedAt'))})
            return {'threads': rows, 'next_cursor': reply.get('nextCursor')}
        if action in ('status', 'read'):
            tid = args.get('thread_id')
            if not isinstance(tid, str) or not tid: raise ValueError('Needs thread_id from list')
            if action == 'read':
                self._thread(self._rpc('thread/read', {'threadId': tid, 'includeTurns': False}, 20, check), tid)
                params = {'threadId': tid, 'limit': 4, 'itemsView': 'full'}
                if args.get('cursor'): params['cursor'] = str(args['cursor'])[:400]
                reply = self._rpc('thread/turns/list', params, 25, check)
                rows = []
                for turn in reversed(reply.get('data') or []):
                    for item in turn.get('items') or []:
                        role = {'userMessage': 'user', 'agentMessage': 'assistant'}.get(item.get('type'))
                        if role is None: continue
                        text = ((item.get('content') or [{}])[0].get('text')
                                if role == 'user' else item.get('text')) or ''
                        rows.append({'turn_id': turn.get('id'), 'role': role, 'phase': item.get('phase'),
                                     'text': clean(text)[:1200], 'truncated': len(text) > 1200})
                return {'thread_id': tid, 'messages': rows, 'next_cursor': reply.get('nextCursor'),
                        'complete_history': False}
            reply = self._rpc('@multi', {'calls': [
                {'method': 'thread/read', 'params': {'threadId': tid, 'includeTurns': False}},
                {'method': 'thread/turns/list', 'params': {'threadId': tid, 'limit': 2}}]}, 35, check)
            thread = self._thread(reply[0], tid)
            turns = (reply[1] or {}).get('data') or []
            turn = turns[0] if turns else {}
            items = turn.get('items') or []
            messages = []
            for item in items[-6:]:
                role = {'userMessage': 'user', 'agentMessage': 'assistant'}.get(item.get('type'))
                if role is None: continue
                text = ((item.get('content') or [{}])[0].get('text')
                        if role == 'user' else item.get('text')) or ''
                messages.append({'turn_id': turn.get('id'), 'role': role, 'phase': item.get('phase'),
                                 'text': clean(text)[:400], 'truncated': len(text) > 400})
            answer = next((item.get('text') for item in reversed(items)
                           if item.get('type') == 'agentMessage' and item.get('text')), None)
            status = turn.get('status')
            return {'thread_id': tid,
                    'title': thread.get('name') or thread.get('preview'),
                    'cwd': (thread.get('environments') or [{}])[0].get('cwd'),
                    'model': thread.get('model'),
                    'stored_status': (thread.get('status') or {}).get('type'),
                    'updated_at': epoch_seconds(thread.get('updatedAt')),
                    'turn_id': turn.get('id'), 'turn_status': status or 'unknown',
                    'phase': 'agentMessage' if answer else status,
                    'answer': clean(answer)[:12000] if answer else answer,
                    'answer_truncated': bool(answer and len(answer) > 12000),
                    'answer_is_partial': status == 'inProgress' and bool(answer),
                    'recent_messages': messages, 'pending_requests': [],
                    'turn_error': turn.get('error')}
        if action == 'stop':
            tid = args.get('thread_id')
            if not isinstance(tid, str) or not tid: raise ValueError('stop needs thread_id')
            turn_id = args.get('turn_id')
            if not (isinstance(turn_id, str) and turn_id):
                reply = self._rpc('thread/turns/list', {'threadId': tid, 'limit': 3}, 20, check)
                running = next((row for row in (reply.get('data') or [])
                                if row.get('status') == 'inProgress'), None)
                if running is None: raise ValueError('No active turn to stop')
                turn_id = running.get('id')
            self._rpc('turn/interrupt', {'threadId': tid, 'turnId': turn_id}, 20, check)
            return {'thread_id': tid, 'turn_id': turn_id, 'stopped': True}
        if action == 'models':
            reply = self._rpc('model/list', {'limit': 100}, 25, check)
            rows = [{'id': row.get('id'), 'name': row.get('displayName'), 'specialty': row.get('modelSpecialty'),
                     'default': bool(row.get('default')), 'supports_images': bool(row.get('supportsImages'))}
                    for row in (reply.get('data') or [])[:24]]
            return {'models': rows, 'next_cursor': reply.get('nextCursor')}
        raise ValueError('This daemon supports start/list/status/read/send/answer/stop/models/'
                         'daybreak/approval/settings; GUI project/file/exec actions are unavailable')


def discord_factory(native, adapter):
    global CLIENT, LOOP, BRIDGE
    CLIENT, LOOP = native, asyncio.get_running_loop()
    default, configured = server_config()
    SERVERS.clear()
    for ident, settings in configured.items():
        if settings.get('transport') == 'ssh':
            bridge = SERVERS[ident] = AppServer(settings, ident, ident == default)
        else:
            bridge = SERVERS[ident] = Bridge(settings, ident, ident == default)
        bridge.adapter = adapter
        if adapter is not None:
            asyncio.create_task(bridge.restore_watchers())
    BRIDGE = SERVERS.get(default)


def codex_task(args, **kwargs):
    if args.get('action') not in PUBLIC_ACTIONS:
        return json.dumps({'success': False, 'error': 'Unsupported action'})
    from ..context import live_owner, owner_identity
    from ..context import is_interrupted
    turn = owner_identity()
    if turn is not None and turn.cron:
        if args['action'] not in SCHEDULED_ACTIONS:
            return json.dumps({'success': False, 'error': 'This scheduled check cannot start, stop or execute Windows commands'})
        ready = CLIENT is not None and CLIENT.is_ready() and LOOP is not None and LOOP.is_running()
    else:
        ready = owner_session()
        turn = live_owner()
    if not ready or not SERVERS:
        return json.dumps({'success': False, 'error': 'Owner Discord and a Codex backend must be connected'})
    if turn is None or is_interrupted(): return json.dumps({'success': False, 'error': 'Turn interrupted'})
    try:
        bridge = select_server(instance_id(args))
    except ValueError as error:
        return json.dumps({'success': False, 'error': str(error)})
    cancelled = threading.Event()
    deadline = time.monotonic() + 75

    def check():
        if (turn.cancelled.is_set() or cancelled.is_set() or time.monotonic() >= deadline
                or CLIENT is None or not CLIENT.is_ready()):
            raise InterruptedError('Codex request interrupted before submission')

    future = asyncio.run_coroutine_threadsafe(bridge.action(args,
        turn.channel, check=check, turn=turn), LOOP)
    try:
        target_key = 'instance' if 'instance' in args else 'server'
        return json.dumps({'success': True, target_key: getattr(bridge, 'server_id', 'desktop'),
                           **future.result(timeout=75)}, ensure_ascii=False)
    except Exception as error:
        cancelled.set()
        future.cancel()
        # No token, websocket headers, prompts or full upstream errors in logs.
        detail = str(error) or ('Codex request timed out; check status before retrying. No automatic resubmission.'
                                if isinstance(error, TimeoutError) else type(error).__name__)
        return json.dumps({'success': False, 'error': clean(detail)[:350]}, ensure_ascii=False)


def windows_files(args):
    action = args.get('action')
    allowed = {'action', 'instance', 'server', 'path', 'limit'} | {
        'list': {'offset', 'search'}, 'read': {'start_line'},
    }.get(action, set())
    if action not in ('list', 'read') or set(args) - allowed:
        return json.dumps({'error': 'Use list(path, offset, search) or read(path, start_line)'})
    request = dict(args, action='file_' + action)
    request.setdefault('limit', 20 if action == 'list' else 80)
    result = json.loads(codex_task(request))
    if not result.get('success'):
        return json.dumps({'error': result.get('error', 'Windows file request failed')})
    # The requested server/path are already in context. Keep only page contents
    # and continuation fields; the task result remains unchanged.
    for key in ('success', 'server', 'instance', 'source', 'path', 'total'):
        result.pop(key, None)
    if action == 'list':
        result['entries'] = [e['fileName'] + ('/' if e['isDirectory'] else '') for e in result['entries']]
    return json.dumps({k: v for k, v in result.items() if v is not None}, ensure_ascii=False)


def windows_exec(args):
    if set(args) - {'instance', 'server', 'command', 'cwd', 'timeout_ms'}:
        return json.dumps({'error': 'Use command argv, optional instance/cwd/timeout_ms'})
    result = json.loads(codex_task(dict(args, action='exec')))
    if not result.get('success'):
        return json.dumps({'error': result.get('error', 'Windows command failed')})
    return json.dumps({key: result[key] for key in ('exit_code', 'stdout', 'stderr', 'truncated')
                       if key in result and (key == 'exit_code' or result[key])}, ensure_ascii=False)


def register(ctx):
    ctx.register_platform_handler('discord', discord_factory)
