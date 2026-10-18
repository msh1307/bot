"""Existing owner integrations plus small, explicit memory and schedule tools."""
import asyncio
from datetime import datetime, timedelta
import json
from pathlib import Path
import secrets
import threading
import urllib.request
from zoneinfo import ZoneInfo

from croniter import croniter
from . import context
from .integrations import host, gmail, access, codex, browser

STATE = Path('/var/lib/evo-omp')
TZ = ZoneInfo('Asia/Seoul')
JOB_LOCK = threading.Lock()


def http_json(path, payload=None):
    req = urllib.request.Request('http://127.0.0.1:8080' + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=100) as r:
        raw = r.read(10 * 2**20 + 1)
    if len(raw) > 10 * 2**20:
        raise ValueError('Web response exceeded limit')
    return json.loads(raw)


class Registry:
    def __init__(self):
        self.tools, self.commands, self.factories = {}, {}, []
        for module in (host, gmail, access, codex, browser):
            module.register(self)

    def register_tool(self, name, toolset, definition, handler):
        if name == 'workspace_download':
            return  # Native sandbox bash downloads files without a host tool.
        self.tools[name] = (dict(definition, label=name, loadMode='essential'), handler)

    def register_command(self, name, handler, description):
        self.commands[name] = handler

    def register_platform_handler(self, name, factory):
        if name == 'discord':
            self.factories.append(factory)

    async def initialize_web(self):
        entries = await asyncio.to_thread(http_json, '/tools')
        for entry in entries:
            if entry['tool'] not in ('internet_search', 'web_read', 'web_image'):
                continue
            d = entry['definition']['function']
            name = d['name']
            self.tools[name] = (dict(d, label=name, loadMode='essential'), None)
        for name, definition, fn in [('memory', MEMORY_SCHEMA, memory),
                                      ('schedule', SCHEDULE_SCHEMA, schedule),
                                      ('wait', WAIT_SCHEMA, wait)]:
            self.tools[name] = (dict(definition, name=name, label=name, loadMode='essential'), fn)

    def definitions(self, cron=False):
        names = ('internet_search', 'web_read', 'web_image', 'browser', 'evo_system_status') if cron else self.tools
        return [self.tools[n][0] for n in names if n in self.tools]

    async def close(self):
        await asyncio.to_thread(browser.close)

    async def call(self, name, args, turn):
        if turn.cancelled.is_set():
            raise InterruptedError('Turn interrupted')
        if turn.user != context.OWNER:
            raise PermissionError('Owner session required')
        if turn.cron and name not in ('internet_search', 'web_read', 'web_image', 'browser', 'evo_system_status'):
            raise PermissionError('This tool requires an owner Discord message')
        definition, handler = self.tools[name]
        if handler is not None:
            result = await asyncio.to_thread(handler, args)
        else:
            result = await asyncio.to_thread(http_json, '/tools', {'tool': name, 'params': args})
            if 'error' in result:
                raise RuntimeError(result['error'])
            result = result.get('plain_text', result)
            if name == 'web_image' and isinstance(result, str) and '\ndata:image/' in result:
                text, data_url = result.rsplit('\n', 1)
                import base64
                image = image_content(base64.b64decode(data_url.split(',', 1)[1], validate=True))
                return {'content': [{'type': 'text', 'text': text}, image]}
        return context.redact_sensitive_text(result) if isinstance(result, str) else result


def image_content(raw):
    import base64
    import io
    from PIL import Image, ImageOps
    if len(raw) > 6 * 2**20:
        raise ValueError('Image download exceeds 6 MiB')
    with Image.open(io.BytesIO(raw)) as source:
        if source.width * source.height > 8_000_000:
            raise ValueError('Image exceeds 8 megapixels')
        source.seek(0)
        image = ImageOps.exif_transpose(source).convert('RGB')
        # RPC input has a 1 MiB frame ceiling. Keep a complete prompt below it.
        image.thumbnail((2048, 2048))
        for quality in (90, 80, 65, 50):
            buf = io.BytesIO()
            image.save(buf, format='JPEG', quality=quality)
            data = base64.b64encode(buf.getvalue()).decode()
            if len(data) <= 650_000:
                return {'type': 'image', 'data': data, 'mimeType': 'image/jpeg'}
        raise ValueError('Image cannot fit the RPC input limit')


def memory(args):
    if context.is_background():
        raise PermissionError('Memory changes require an owner message')
    root = STATE / 'memory'
    root.mkdir(mode=0o700, exist_ok=True)
    action, name = args.get('action', 'list'), args.get('name', '')
    if action == 'list':
        return {'documents': sorted(p.name for p in root.glob('*.md'))}
    if not name or len(name) > 80 or Path(name).name != name or not name.endswith('.md'):
        raise ValueError('Use a plain .md filename from list')
    p = root / name
    if p.is_symlink():
        raise ValueError('Invalid memory document')
    if action == 'read':
        return {'name': name, 'text': p.read_text()[:16000]}
    if action == 'delete':
        p.unlink()
        return {'deleted': name}
    if action == 'write':
        text = args.get('text', '')
        if not isinstance(text, str) or len(text) > 16000:
            raise ValueError('Memory document limit is 16000 characters')
        p.write_text(text)
        p.chmod(0o600)
        return {'saved': name}
    raise ValueError('Use list, read, write or delete')


def load_jobs():
    p = STATE / 'jobs.json'
    return json.loads(p.read_text()).get('jobs', []) if p.exists() else []


def save_jobs(jobs):
    p = STATE / 'jobs.json'
    temp = p.with_suffix('.tmp')
    temp.write_text(json.dumps({'jobs': jobs}, ensure_ascii=False, indent=2) + '\n')
    temp.chmod(0o600)
    temp.replace(p)


def wait(args):
    turn = context.owner_message()
    if turn is None:
        raise PermissionError('Waiting requires an owner Discord task')
    seconds = args.get('seconds')
    if type(seconds) is not int or not 1 <= seconds <= 300:
        raise ValueError('seconds must be an integer from 1 to 300')
    with turn.wait_lock:
        if turn.waited_seconds + seconds > 300:
            raise ValueError('This turn has a 300-second total wait budget; use schedule action=defer instead')
        turn.waited_seconds += seconds
        remaining = 300 - turn.waited_seconds
    if turn.cancelled.wait(seconds):
        raise InterruptedError('Wait interrupted')
    return {'waited_seconds': seconds, 'remaining_wait_seconds': remaining}


def cancel_deferred(channel=None):
    """Explicit stop/reset commands invalidate pending task continuations only."""
    with JOB_LOCK:
        jobs, changed = load_jobs(), False
        for job in jobs:
            if (job.get('continuation') and (channel is None or job['channel'] == channel)
                    and (job.get('enabled') or job.get('running'))):
                job.update(enabled=False, running=False, last_status='cancelled')
                changed = True
        if changed:
            save_jobs(jobs)


def schedule(args):
    t = context.owner_message()
    if t is None:
        raise PermissionError('Schedules require an owner Discord message')
    action = args.get('action', 'list')
    with JOB_LOCK:
        jobs = load_jobs()
        if action == 'list':
            return {'jobs': [{k: j.get(k) for k in ('id', 'name', 'enabled', 'running', 'continuation', 'schedule', 'next_run_at', 'last_status')}
                             for j in jobs]}
        ident = args.get('id')
        job = next((j for j in jobs if j['id'] == ident), None)
        if action in ('delete', 'pause', 'resume'):
            if job is None:
                raise ValueError('Unknown schedule id')
            if action == 'delete':
                jobs.remove(job)
            else:
                job['enabled'] = action == 'resume'
                if job['enabled']:
                    job['next_run_at'] = next_run(job).isoformat()
                    if datetime.fromisoformat(job['next_run_at']) <= datetime.now(TZ):
                        raise ValueError('Expired one-shot tasks need a new future time')
                else:
                    job['running'] = False
                job['last_status'] = 'pending' if job['enabled'] else 'paused'
            save_jobs(jobs)
            return {'success': True, 'id': ident, 'action': action}
        if action not in ('create', 'defer'):
            raise ValueError('Use list, create, defer, pause, resume or delete')
        # Completed callbacks are not permanent reminders and must not fill the job limit.
        terminal = {'ok', 'error', 'interrupted', 'delivery_failed', 'cancelled', 'interrupted_by_restart'}
        jobs = [j for j in jobs if not (j.get('continuation') and not j.get('enabled')
                                      and not j.get('running') and j.get('last_status') in terminal)]
        if len(jobs) >= 64:
            raise ValueError('Schedule limit reached')
        prompt, name = args.get('prompt', ''), args.get('name', 'Follow-up' if action == 'defer' else '')
        if (not isinstance(prompt, str) or not isinstance(name, str)
                or not 1 <= len(prompt) <= 8000 or not 1 <= len(name) <= 100):
            raise ValueError('Provide a name and prompt')
        if action == 'defer':
            seconds = args.get('after_seconds')
            if type(seconds) is not int or not 1 <= seconds <= 86400 or args.get('at') or args.get('cron'):
                raise ValueError('defer needs after_seconds (1..86400), without at or cron')
            if sum(bool(j.get('continuation')) for j in jobs) >= 8:
                raise ValueError('At most 8 pending task callbacks; list or cancel existing ones')
            spec = {'kind': 'at', 'at': (datetime.now(TZ) + timedelta(seconds=seconds)).isoformat()}
        else:
            if bool(args.get('at')) == bool(args.get('cron')) or 'after_seconds' in args:
                raise ValueError('create needs exactly one of at or cron; use defer for a task callback')
            spec = {'kind': 'at', 'at': args['at']} if args.get('at') else {'kind': 'cron', 'expr': args['cron']}
        job = {'id': secrets.token_hex(6), 'name': name, 'prompt': prompt,
               'schedule': spec, 'enabled': True, 'channel': t.channel,
               'last_status': None, 'last_run_at': None}
        if action == 'defer':
            job['continuation'] = True
            job['model'] = t.model
        job['next_run_at'] = next_run(job).isoformat()
        if datetime.fromisoformat(job['next_run_at']) <= datetime.now(TZ):
            raise ValueError('Reminder time must be in the future')
        jobs.append(job)
        if t.cancelled.is_set():
            raise InterruptedError('Task interrupted; callback was not saved')
        save_jobs(jobs)
        return {'success': True, 'id': job['id'], 'next_run_at': job['next_run_at'],
                'continuation': action == 'defer'}


def next_run(job):
    spec = job['schedule']
    if spec['kind'] == 'at':
        d = datetime.fromisoformat(spec.get('at') or spec['run_at'])
        return d.replace(tzinfo=TZ) if d.tzinfo is None else d.astimezone(TZ)
    return croniter(spec['expr'], datetime.now(TZ)).get_next(datetime)


MEMORY_SCHEMA = {
    'description': 'Read on-demand memory documents. They are never injected automatically. '
                   'Write or delete only when the owner asks to remember, update or forget something.',
    'parameters': {'type': 'object', 'properties': {
        'action': {'type': 'string', 'enum': ['list', 'read', 'write', 'delete']},
        'name': {'type': 'string'}, 'text': {'type': 'string'}}, 'required': ['action']}}
SCHEDULE_SCHEMA = {
    'description': 'Manage owner-requested reminders and recurring briefings. Times use Asia/Seoul. '
                   'create uses at (ISO datetime) or cron (five fields): separate read-only web briefings. '
                   'defer uses after_seconds to resume the CURRENT owner task later in this channel; '
                   'then finish this turn instead of polling. Put the task, identifiers and next check in prompt '
                   'so it can resume even after a restart. Reuses the conversation when still in RAM; no extra model. '
                   'Due callbacks wait in the task queue; new messages do not cancel pending callbacks. '
                   '/stop, /new, /reset or /model cancel pending callbacks. '
                   'Callbacks retain ordinary task tools; root still needs a fresh Discord approval for each command. '
                   'Use defer only to continue the owner task, never to invent unrelated work. '
                   'list shows IDs; pause/delete cancel queued work but /stop is needed for already running work.',
    'parameters': {'type': 'object', 'properties': {
        'action': {'type': 'string', 'enum': ['list', 'create', 'defer', 'pause', 'resume', 'delete']},
        'id': {'type': 'string'}, 'name': {'type': 'string'}, 'prompt': {'type': 'string'},
        'at': {'type': 'string'}, 'cron': {'type': 'string'},
        'after_seconds': {'type': 'integer', 'minimum': 1, 'maximum': 86400}}, 'required': ['action']}}
WAIT_SCHEMA = {
    'description': 'Pause before checking an in-progress task again. No model requests while sleeping. '
                   'Prefer short polling for work expected within a minute; defer longer waits. '
                   'Maximum 300 seconds TOTAL per turn; use schedule action=defer for longer waits. '
                   'New owner messages and /stop interrupt the wait. Check state with the relevant tool after waiting.',
    'parameters': {'type': 'object', 'properties': {
        'seconds': {'type': 'integer', 'minimum': 1, 'maximum': 300}}, 'required': ['seconds']}}
