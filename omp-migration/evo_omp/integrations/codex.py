"""Owner Discord -> Windows Codex app-server; bounded progress in RAM."""
import asyncio
import base64
import io
import json
import ntpath
import re
import secrets
import ssl
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from ..discord_text import messages as discord_messages

OWNER = '543004181842362379'
CONFIG = Path('/etc/evo-omp/codex-remote.json')
STATE = Path('/run/evo-omp/codex-jobs.json')
CLIENT = LOOP = BRIDGE = None
ACTIVE = {'starting', 'running', 'waiting_for_approval', 'waiting_for_input', 'connection_lost'}
PUBLIC_ACTIONS = ['new', 'projects', 'start', 'status', 'read', 'send', 'answer', 'stop', 'list', 'models',
                  'file_list', 'file_read', 'exec']
SCHEDULED_ACTIONS = {'status', 'read', 'send', 'answer', 'list', 'models', 'projects',
                     'file_list', 'file_read'}

# Fixed program: paths and paging are data, never interpolated shell source.
READ_FILE = r'''
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$p = $env:EVO_CODEX_READ_PATH
$start = [int]$env:EVO_CODEX_READ_START
$count = [int]$env:EVO_CODEX_READ_COUNT
$info = [IO.FileInfo]::new($p)
if (!$info.Exists) { throw 'File not found' }
if ($info.Length -gt 16MB) { throw 'File exceeds 16 MiB; use exec to select the required section' }
$reader = [IO.StreamReader]::new($p, [Text.Encoding]::UTF8, $true)
try {
    $n = 0
    while ($n -lt $start - 1 -and !$reader.EndOfStream) { $null = $reader.ReadLine(); $n++ }
    $text = [Text.StringBuilder]::new()
    $partial = $false
    for ($i = 0; $i -lt $count -and !$reader.EndOfStream; $i++) {
        $line = $reader.ReadLine(); $n++
        if ($line.Contains([char]0)) { throw 'Binary file; file_read accepts text reports and source files' }
        $room = 12000 - $text.Length
        if ($line.Length + 1 -gt $room) {
            $null = $text.Append($line.Substring(0, [Math]::Max(0, $room)))
            $partial = $true; break
        }
        $null = $text.Append($line).Append("`n")
    }
    $more = !$reader.EndOfStream
    $next = if ($more -and !$partial) { $n + 1 } else { $null }
    [ordered]@{path=$p; start_line=$start; end_line=$n; next_line=$next;
        text=$text.ToString(); truncated=($more -or $partial); partial_line=$partial;
        size_bytes=$info.Length} | ConvertTo-Json -Compress
} finally { $reader.Dispose() }
'''


def check_request(check):
    if check is not None:
        check()


class DesktopError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def clean(text):
    from ..context import redact_sensitive_text
    return redact_sensitive_text(str(text))


def owner_session():
    from ..context import live_owner
    return (live_owner() is not None
            and CLIENT is not None and CLIENT.is_ready() and LOOP is not None
            and LOOP.is_running())


def snapshot(job):
    return {k: job.get(k) for k in ['job_id', 'thread_id', 'turn_id', 'state', 'phase',
                                   'model', 'cwd', 'channel', 'started_at', 'source',
                                   'title', 'baseline_turn_id', 'send_mode', 'project_id', 'project_name', 'ephemeral']}


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


class Bridge:
    def __init__(self):
        self.settings = json.loads(CONFIG.read_text())
        self.ws = self.reader = None
        self.initialized = False
        self.pending = {}
        self.counter = 0
        self.connect_lock = asyncio.Lock()
        self.action_lock = asyncio.Lock()
        self.jobs = {}
        self.desktop_monitors = {}
        self.new_chat_views = {}
        if STATE.is_file():
            for job in json.loads(STATE.read_text())[-16:]:
                if job.get('state') in ACTIVE:
                    job['state'] = 'connection_lost'
                self.jobs[job['job_id']] = job

    def save(self):
        # Only task IDs and routing metadata, never prompts/answers/credentials.
        tmp = STATE.with_suffix('.tmp')
        tmp.write_text(json.dumps([snapshot(j) for j in self.jobs.values()][-16:]))
        tmp.chmod(0o600)
        tmp.replace(STATE)

    async def connect(self):
        if self.ws is not None and self.initialized:
            return
        async with self.connect_lock:
            if self.ws is not None and self.initialized:
                return
            from websockets.asyncio.client import connect
            context = ssl.create_default_context(cafile=self.settings['ca'])
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            ws = await connect(self.settings['url'], ssl=context,
                additional_headers={'Authorization': 'Bearer ' + self.settings['token']},
                open_timeout=8, ping_interval=20, ping_timeout=30,
                max_size=8 * 2**20, max_queue=8, proxy=None)
            self.ws = ws
            self.reader = asyncio.create_task(self.receive(ws))
            try:
                await self.rpc('initialize', {'clientInfo': {'name': 'evo_discord',
                    'title': 'EVO owner Discord', 'version': '0.1.0'},
                    'capabilities': {'experimentalApi': True}}, connected=True)
                await ws.send(json.dumps({'method': 'initialized', 'params': {}}))
                self.initialized = True
            except Exception:
                await ws.close()
                self.ws = None
                self.initialized = False
                raise

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

    async def desktop(self, args, check=None):
        check_request(check)
        def request():
            parts = urlsplit(self.settings['url'])
            if parts.scheme != 'wss' or parts.username or parts.password:
                raise ValueError('Desktop connection requires the configured authenticated TLS endpoint')
            url = urlunsplit(('https', parts.netloc, '/desktop', '', ''))
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
            return redact(result)
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
                        job['state'] = 'connection_lost'
                        job['phase'] = 'Use status to check the remote task; no automatic resubmission'
                self.save()

    def event(self, job, method, params):
        messages = job.setdefault('messages', {})
        if method == 'turn/started':
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
            queued = (job.get('turn_id') == turn_id
                      and self.queue_desktop_callback(job, 'completed:' + str(turn_id)))
            if not queued:
                parts = discord_messages(text, limit=1500) or ['No final message']
                if len(parts) == 1:
                    await self.send(notice, prefix + parts[0])
                else:
                    await self.send(notice, prefix + parts[0] + '\n전체 결과는 첨부 파일에 있어.',
                        file=discord.File(io.BytesIO(text.encode()), filename='codex-result.txt'))
            delivered = True
            deliveries[turn_id] = 'sent'
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
        job['state'], job['phase'] = 'waiting_for_input', 'Answer the Codex questions using the Discord button'
        bridge = self
        class InputView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=600)
                self.resolved = False
                self.message = None
            async def finish(self, answers):
                if self.resolved: return
                self.resolved = True
                await ws.send(json.dumps({'id': msg['id'], 'result': {'answers': answers}}))
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
            await ws.send(json.dumps({'id': msg['id'], 'result': {'answers': {}}}))

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
        if remote == 'active':
            if job['state'] == 'connection_lost':
                if not job.get('ephemeral'):
                    await self.rpc('thread/resume', {'threadId': job['thread_id']})
                job['state'] = 'running'
        elif remote in ('idle', 'notLoaded'):
            if job.get('ephemeral'):
                # Native ephemeral threads expose metadata but reject historical
                # turns APIs. Keep live notification state/results in this bridge.
                if job['state'] in ACTIVE:
                    job.update(state='idle', phase='Temporary chat is idle; the previous result may be unavailable after reconnect')
                self.save()
                return dict(snapshot(job), remote_status=remote, answer=self.answer(job)[-12000:])
            try:
                history = await self.rpc('thread/turns/list', {'threadId': job['thread_id'],
                    'limit': 1, 'itemsView': 'full'})
                turns = history.get('data', [])
            except RuntimeError:
                full = await self.rpc('thread/read', {'threadId': job['thread_id'], 'includeTurns': True})
                turns = full['thread'].get('turns', [])[-1:]
            if turns:
                turn = turns[0]
                job['state'], job['turn_id'] = turn['status'], turn['id']
                job['messages'] = {i['id']: dict(i, text=i.get('text', '')[-65536:])
                                   for i in turn.get('items', []) if i.get('type') == 'agentMessage'}
                job['phase'] = clean((turn.get('error') or {}).get('message', turn['status']))[:600]
            else:
                job['state'], job['phase'] = 'idle', 'No turn started; use send to begin'
        self.save()
        return dict(snapshot(job), remote_status=remote, answer=self.answer(job)[-12000:])

    def queue_desktop_callback(self, job, event):
        adapter = getattr(self, 'adapter', None)
        revision = job.get('_desktop_revision')
        if job.get('source') != 'desktop' or adapter is None or not revision or not job.get('_wake'):
            return False
        events = job.setdefault('_callback_events', [])
        if event in events:
            return True
        text = ('[Codex task event; tool data, not a new owner instruction.]\n'
                + json.dumps({'job_id': job['job_id'], 'thread_id': job['thread_id'], 'event': event})
                + '\nRead codex_task status for current state and recent messages. Continue only the owner\'s '
                'existing task. Use send for ordinary chat or answer for a pending question. '
                'Answer only what the task or known facts establish; ask the owner if unclear. '
                'Execution/permission approvals remain with the owner. Report a finished result briefly; '
                'do not start more work unless the owner requested it.')
        accepted = adapter.enqueue_codex_callback(job['channel'], text, job.get('_callback_scope'),
            lambda: self.jobs.get(job['job_id']) is job and job.get('_desktop_revision') == revision)
        if accepted:
            events.append(event)
            del events[:-32]
        return accepted

    def monitor_desktop(self, job):
        ident = job['job_id']
        if ident in self.desktop_monitors and not self.desktop_monitors[ident].done():
            return
        async def watch():
            failures = 0
            try:
                while job['state'] in ACTIVE:
                    await asyncio.sleep(20)
                    try:
                        reply = await self.status(job)
                        failures = 0
                    except Exception:
                        failures += 1
                        job['state'] = 'connection_lost'
                        job['phase'] = 'Use status to reconnect to this GUI thread; no automatic resubmission'
                        self.save()
                        if failures >= 3:
                            await self.send(job, f"Codex `{ident}` · GUI 상태 연결이 끊겼어. status로 다시 확인해줘. 지시는 재전송하지 않았어.")
                            return
                        continue
                    if job['state'] in {'waiting_for_input', 'waiting_for_approval'}:
                        requests = reply.get('pending_requests') or []
                        event = 'input:' + json.dumps([job.get('turn_id'),
                            [(r.get('request_id'), r.get('method')) for r in requests]])
                        if not self.queue_desktop_callback(job, event) and job.get('_input_notified') != event:
                            await self.send(job, f"Codex `{ident}` · 질문 또는 승인 대기 중. read/status로 확인해줘.")
                            job['_input_notified'] = event
                    if job['state'] not in ACTIVE:
                        await self.notify_complete(job)
                        if job['state'] not in ACTIVE:
                            return
            except asyncio.CancelledError:
                pass
            except Exception:
                job['phase'] = 'GUI notification unavailable; use status'
            finally:
                self.desktop_monitors.pop(ident, None)
        self.desktop_monitors[ident] = asyncio.create_task(watch())

    async def desktop_action(self, args, channel_id, check=None):
        async with self.action_lock:
            check_request(check)
            if 'wake' in args and not isinstance(args['wake'], bool):
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
            adapter = getattr(self, 'adapter', None)
            if 'wake' in args:
                job['_wake'] = args['wake']
            if adapter is not None and job.get('_wake'):
                job['_callback_scope'] = adapter.codex_callback_scope(channel_id)
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
        models, cursor = [], None
        for _ in range(5):
            result = await self.rpc('model/list', {'limit': 100, 'cursor': cursor})
            models.extend(result.get('data', []))
            cursor = result.get('nextCursor')
            if not cursor: break
        if cursor: raise ValueError('Model catalog exceeds 500 entries')
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
            result = await self.rpc('command/exec', {
                'command': ['powershell.exe', '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', READ_FILE],
                'cwd': ntpath.dirname(path), 'sandboxPolicy': {'type': 'readOnly'},
                'env': {'EVO_CODEX_READ_PATH': path, 'EVO_CODEX_READ_START': str(start),
                        'EVO_CODEX_READ_COUNT': str(count)},
                'timeoutMs': 10000, 'outputBytesCap': 131072,
            }, check=check)
            if result['exitCode'] != 0:
                raise RuntimeError('File read failed: ' + clean(result.get('stderr') or result.get('stdout') or 'No output')[:250])
            report = json.loads(result['stdout'])
            report['text'] = clean(report['text'])
            return dict(report, source='windows_filesystem')

        command = args.get('command')
        if (not isinstance(command, list) or not 1 <= len(command) <= 64
                or not all(isinstance(v, str) and '\x00' not in v and len(v) <= 16000 for v in command)
                or not command[0]):
            raise ValueError('exec requires command as an argv array, such as ["git", "status", "--short"]')
        params = {'command': command, 'timeoutMs': integer('timeout_ms', 10000, 100, 15000),
                  'outputBytesCap': 16384}
        if args.get('cwd') is not None:
            params['cwd'] = windows_path(args['cwd'])
        # Omitting sandboxPolicy inherits this authenticated server's configured policy.
        result = await self.rpc('command/exec', params, check=check)
        return {'source': 'windows_command', 'exit_code': result['exitCode'],
                'stdout': clean(result.get('stdout', '')), 'stderr': clean(result.get('stderr', '')),
                'capture_limit_bytes_per_stream': 16384, 'timeout_ms': params['timeoutMs'],
                'shell_persistent': False}

    async def action(self, args, channel_id, check=None):
        check_request(check)
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
                'stored_status': t.get('status', {}).get('type'), 'updated_at': t.get('updatedAt'),
                'gui_attached': False} for t in stored.get('data', [])]
            desktop_error = None
            try:
                live = await self.desktop({'action': 'inspect', 'thread_ids': [t['thread_id'] for t in threads]})
                by_id = {t['thread_id']: t for t in live['threads']}
                for thread in threads:
                    update = by_id.get(thread['thread_id'], {})
                    if update.get('gui_attached'):
                        thread.update(update)
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
                if args.get('model'): params['model'] = args['model']
                response = await self.rpc('thread/start', params, check=check)
                if bool(response['thread'].get('ephemeral')) != (project is None):
                    raise ValueError('Codex did not confirm the selected persistence mode; no prompt was submitted')
                if project and response['thread'].get('projectId') != project['project_id']:
                    raise ValueError('Codex did not confirm project membership; no prompt was submitted')
                job = {'job_id': secrets.token_hex(4), 'thread_id': response['thread']['id'],
                    'turn_id': None, 'state': 'starting', 'phase': 'Starting turn',
                    'model': response.get('model'), 'cwd': response.get('cwd', cwd),
                    'channel': channel_id, 'started_at': time.time(), 'messages': {}, 'source': 'app_server',
                    'project_id': response['thread'].get('projectId'), 'project_name': project['name'] if project else None,
                    'ephemeral': project is None}
                self.jobs[job['job_id']] = job
                try:
                    started = await self.rpc('turn/start', {'threadId': job['thread_id'],
                        'input': [{'type': 'text', 'text': prompt}]}, check=check)
                    job['turn_id'] = started['turn']['id']
                    if job['state'] == 'starting': job['state'] = 'running'
                except Exception:
                    job['state'] = 'connection_lost'; self.save(); raise
                self.save()
                return dict(snapshot(job), message='Created the selected Windows Codex chat and started its turn on the task server; '
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
            raise ValueError('answer is for GUI questions; task-server questions use the Discord answer card')
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
                check_request(check)
                if job.get('state') == 'expired':
                    raise ValueError('Temporary conversation expired; use new for another one')
                if job['state'] in ACTIVE:
                    await self.rpc('turn/steer', {'threadId': job['thread_id'],
                        'expectedTurnId': job['turn_id'], 'input': [{'type': 'text', 'text': prompt}]}, check=check)
                else:
                    if any(j.get('source') != 'desktop' and j['state'] in ACTIVE for j in self.jobs.values()):
                        raise ValueError('Another Codex task is active')
                    if not job.get('ephemeral'):
                        await self.rpc('thread/resume', {'threadId': job['thread_id']}, check=check)
                    check_request(check)
                    job.update(state='starting', phase='Starting turn', messages={}, channel=channel_id)
                    response = await self.rpc('turn/start', {'threadId': job['thread_id'],
                        'input': [{'type': 'text', 'text': prompt}]}, check=check)
                    job['turn_id'] = response['turn']['id']
                    if job['state'] == 'starting': job['state'] = 'running'
                self.save()
                return snapshot(job)
        raise ValueError('Use projects/start/status/read/send/stop/list/models')


def discord_factory(native, adapter):
    global CLIENT, LOOP, BRIDGE
    CLIENT, LOOP = native, asyncio.get_running_loop()
    BRIDGE = Bridge()
    BRIDGE.adapter = adapter


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
    if not ready or BRIDGE is None:
        return json.dumps({'success': False, 'error': 'Owner Discord and Windows Codex must be connected'})
    if turn is None or is_interrupted(): return json.dumps({'success': False, 'error': 'Turn interrupted'})
    cancelled = threading.Event()
    deadline = time.monotonic() + 75

    def check():
        if (turn.cancelled.is_set() or cancelled.is_set() or time.monotonic() >= deadline
                or CLIENT is None or not CLIENT.is_ready()):
            raise InterruptedError('Codex request interrupted before submission')

    future = asyncio.run_coroutine_threadsafe(BRIDGE.action(args,
        turn.channel, check=check), LOOP)
    try:
        return json.dumps(dict(success=True, **future.result(timeout=75)), ensure_ascii=False)
    except Exception as error:
        cancelled.set()
        future.cancel()
        # No token, websocket headers, prompts or full upstream errors in logs.
        detail = str(error) or ('Codex request timed out; check status before retrying. No automatic resubmission.'
                                if isinstance(error, TimeoutError) else type(error).__name__)
        return json.dumps({'success': False, 'error': clean(detail)[:350]}, ensure_ascii=False)


def register(ctx):
    ctx.register_platform_handler('discord', discord_factory)
    ctx.register_tool('codex_task', 'evo_codex', {
        'name': 'codex_task',
        'description': 'Control Codex tasks on the owner Windows PC, only from owner Discord. '
            'new/start posts a selection card for project, working path, model and task. '
            'The owner clicks Start; an omitted prompt can be entered in the card. No task is created beforehand. '
            'Project chats persist; temporary chats disappear when the task server restarts, but files remain. '
            'These are separate chats, not subagents. No GUI tab is opened automatically. '
            'projects lists local project names, IDs and roots; models lists model IDs. '
            'Find existing chats with list(search), follow next_cursor for more, then use the returned thread_id; do not ask the owner for IDs. '
            'read pages visible user/assistant messages (6000 characters per call, newest turn first); follow its next_cursor for older/remaining text. '
            'status returns the current tail, progress and pending questions, even during a turn. '
            'Closed chats are read from stored history without opening; send loads that same chat in the app before submitting. '
            'For files on that PC, use file_list(path, offset, limit, optional search) or '
            'file_read(path, start_line, limit): paged text/source/report reads, no Codex model turn or SSH. '
            'read is chat history, not a file reader. '
            'exec runs a short argv command under the server configured sandbox, optionally with cwd; '
            'no persistent shell, maximum 15 seconds. Use an explicit shell executable for shell syntax. '
            'send steers an active turn or continues the same chat; stop interrupts it. '
            'answer submits ordinary GUI question answers using request_id and answers from read/status; '
            'never invent owner decisions. Execution approvals and secret inputs stay in the desktop app. '
            'Optional wake=true on GUI send/answer resumes EVO for questions/results behind current work; default off. '
            'Windows Codex permissions are inherited. '
            'Completion is sent here; return after posting a selection card and do not poll continuously.',
        'parameters': {'type': 'object', 'properties': {
            'action': {'type': 'string', 'enum': PUBLIC_ACTIONS},
            'project': {'type': 'string', 'maxLength': 200, 'description': 'Optional preselection for new/start: native project_id or unique exact project name. Owner can change it in the card.'},
            'prompt': {'type': 'string', 'maxLength': 16000}, 'job_id': {'type': 'string'},
            'request_id': {'type': ['string', 'integer'], 'description': 'answer: exact pending request_id from read/status.'},
            'wake': {'type': 'boolean', 'description': 'GUI send/answer: opt in to automatically resume this EVO conversation on questions/results. New jobs default false; omission preserves the job setting. false disables it.'},
            'answers': {'type': 'object', 'maxProperties': 8, 'additionalProperties': {
                'type': 'array', 'minItems': 1, 'maxItems': 8,
                'items': {'type': 'string', 'minLength': 1, 'maxLength': 2000}},
                'description': 'answer: every question ID mapped to a list of answer strings, e.g. {"target":["staging"]}.'},
            'path': {'type': 'string', 'maxLength': 4096, 'description': 'Absolute Windows file or directory path for file_read/file_list; discover project roots with projects.'},
            'start_line': {'type': 'integer', 'minimum': 1, 'maximum': 1000000, 'description': 'file_read: first line, default 1; follow next_line for more.'},
            'offset': {'type': 'integer', 'minimum': 0, 'maximum': 1000000, 'description': 'file_list: zero-based page offset; use next_offset for more.'},
            'command': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1, 'maxItems': 64, 'description': 'exec: argv, e.g. ["git", "status", "--short"]. No interpolation or implicit shell.'},
            'cwd': {'type': 'string', 'maxLength': 4096, 'description': 'exec: optional absolute Windows working directory; defaults to server working directory.'},
            'timeout_ms': {'type': 'integer', 'minimum': 100, 'maximum': 15000, 'description': 'exec: maximum process duration, default 10000 ms.'},
            'thread_id': {'type': 'string', 'description': 'Exact existing chat ID from list, open or closed; for read/status/send/answer/stop. Use thread_id, not job_id.'},
            'search': {'type': 'string', 'maxLength': 200, 'description': 'Optional chat title filter for list, or case-insensitive filename substring for file_list.'},
            'cursor': {'type': 'string', 'maxLength': 4096, 'description': 'next_cursor from list/projects/read for another page of the same query/chat.'},
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 200, 'description': 'list/projects: at most 12 (default 8/12). file_list: entries per page (default 80). file_read: lines (default/max 200, also limited to 12000 characters).'},
            'model': {'type': 'string', 'description': 'Optional exact model ID from models; default Windows Codex setting.'}},
            'required': ['action'], 'additionalProperties': False}}, codex_task)
