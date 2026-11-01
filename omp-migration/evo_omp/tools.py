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
from .screen_privacy import filter_result as filter_screen
from .integrations import host, gmail, access, codex, browser, rdp, credential_input, remote, remote_files, coding

STATE = Path('/var/lib/evo-omp')
TZ = ZoneInfo('Asia/Seoul')
JOB_LOCK = threading.Lock()
DIRECT_TOOLS = {'internet_search', 'web_read', 'browser', 'remote_control', 'user_input', 'wait'}


def http_json(path, payload=None):
    req = urllib.request.Request('http://127.0.0.1:8080' + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=100) as r:
        raw = r.read(10 * 2**20 + 1)
    if len(raw) > 10 * 2**20:
        raise ValueError('Web response exceeded limit')
    return json.loads(raw)


def screen_share_request(name, args, turn):
    params = args.get('args', {}) if name == 'remote_control' else args
    if name not in ('browser', 'remote_control') or not isinstance(params, dict) or 'share' not in params:
        return args, False
    share = params['share']
    if (type(share) is not bool or args.get('action') != 'screenshot'
            or name == 'remote_control' and args.get('protocol') != 'rdp'):
        raise ValueError('share is a boolean for browser/RDP screenshot only')
    if share and context.live_owner() is not turn:
        raise PermissionError('Screenshot sharing requires a live owner Discord message')
    params = dict(params)
    params.pop('share')
    return (dict(args, args=params) if name == 'remote_control' else params), share


async def share_screen(result, turn):
    """Upload only the privacy-filtered image, in memory, to this turn's channel."""
    import base64
    from contextlib import closing
    import io
    import discord
    if context.live_owner() is not turn:
        raise PermissionError('Screenshot sharing requires a live owner Discord message')
    images = [b for b in result.get('content', []) if isinstance(b, dict) and b.get('type') == 'image'] \
        if isinstance(result, dict) else []
    if len(images) != 1:
        raise ValueError('Screenshot sharing requires one filtered image')
    block = images[0]
    mime, data = block.get('mimeType'), block.get('data')
    if mime not in ('image/jpeg', 'image/png') or not isinstance(data, str) or len(data) > 600000:
        raise ValueError('Invalid screenshot attachment')
    try:
        raw = base64.b64decode(data, validate=True)
    except ValueError:
        raise ValueError('Invalid screenshot attachment') from None
    if not raw or len(raw) > 450000:
        raise ValueError('Invalid screenshot attachment')
    try:
        client = context.client
        channel = client.get_channel(int(turn.channel))
        if channel is None:
            channel = await asyncio.wait_for(client.fetch_channel(int(turn.channel)), 15)
        if context.live_owner() is not turn or channel.id != int(turn.channel):
            raise PermissionError('Screenshot destination is no longer active')
        with closing(discord.File(io.BytesIO(raw), filename='screen.' + ('jpg' if mime == 'image/jpeg' else 'png'))) as attachment:
            await asyncio.wait_for(channel.send(file=attachment, allowed_mentions=discord.AllowedMentions.none()), 30)
    except (PermissionError, InterruptedError):
        raise
    except Exception:
        raise RuntimeError('Screenshot upload failed; delivery is uncertain. Do not automatically retry.') from None


class Registry:
    def __init__(self):
        self.tools, self.commands, self.factories = {}, {}, []
        for module in (host, gmail, access, codex, browser, credential_input, remote, remote_files):
            module.register(self)

    def register_tool(self, name, toolset, definition, handler):
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
                                      ('user_input', USER_INPUT_SCHEMA, None),
                                      ('wait', WAIT_SCHEMA, wait)]:
            self.tools[name] = (dict(definition, name=name, label=name, loadMode='essential'), fn)

    def definitions(self, cron=False):
        names = ('internet_search', 'web_read', 'web_image', 'browser',
                 'evo_system_status', 'remote_control', 'schedule') if cron else self.tools
        return [dict(remote.definition(cron) if n == 'remote_control' else self.tools[n][0],
                     label=n, loadMode='essential' if n in DIRECT_TOOLS else 'discoverable')
                for n in names if n in self.tools]

    async def close(self):
        await asyncio.to_thread(browser.close)
        await asyncio.to_thread(rdp.close)

    async def call(self, name, args, turn):
        if turn.cancelled.is_set():
            raise InterruptedError('Turn interrupted')
        if turn.user != context.OWNER:
            raise PermissionError('Owner session required')
        if turn.cron and name not in ('internet_search', 'web_read', 'web_image',
                                    'browser', 'evo_system_status', 'remote_control', 'schedule'):
            raise PermissionError('This tool requires an owner Discord message')
        if name == 'user_input':
            return await context.client.user_input(args, turn)
        if name in ('_pi_io', '_pi_exec'):
            return await asyncio.to_thread(coding.file_io if name == '_pi_io' else coding.execute, args)
        args, share = screen_share_request(name, args, turn)
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
        if name in ('browser', 'remote_control'):
            result = await asyncio.to_thread(filter_screen, result)
        if share:
            await share_screen(result, turn)
            result = dict(result, shared_to_discord=True)
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
    t = context.owner_identity()
    if t is None:
        raise PermissionError('Schedules require an owner Discord message')
    action = args.get('action', 'list')
    if t.cron and action not in ('read', 'note'):
        raise PermissionError('A scheduled run can only read its own task or update its progress note')
    with JOB_LOCK:
        jobs = load_jobs()
        if action == 'note':
            if not t.cron or not t.job_id:
                raise PermissionError('Progress notes belong to a scheduled run')
            note = args.get('text')
            if not isinstance(note, str) or len(note) > 4000:
                raise ValueError('Progress note must be at most 4000 characters')
            own = next((j for j in jobs if j['id'] == t.job_id and j['channel'] == t.channel), None)
            if own is None:
                raise ValueError('Scheduled job is no longer available')
            own['note'] = note
            save_jobs(jobs)
            return {'saved': True, 'job_id': t.job_id}
        if action == 'list':
            query = args.get('query', '')
            if not isinstance(query, str) or len(query) > 200:
                raise ValueError('query must be at most 200 characters')
            terms = query.casefold().split()
            matches = [j for j in jobs if all(term in (j['name'] + '\n' + j['prompt']).casefold()
                                             for term in terms)]
            matches.sort(key=lambda j: (not j.get('running', False), not j.get('enabled', False)))
            return {'jobs': [dict({k: j[k] for k in ('id', 'name', 'enabled', 'running', 'continuation',
                                    'schedule', 'until', 'quiet', 'next_run_at', 'last_status') if k in j},
                                 summary=' '.join(j['prompt'].split())[:140]) for j in matches]}
        ident = args.get('id', t.job_id if t.cron else None)
        job = next((j for j in jobs if j['id'] == ident), None)
        if job is None and not t.cron and isinstance(ident, str):
            # Exact unique names are convenient; partial matches are discovery only.
            names = [j for j in jobs if ' '.join(j['name'].split()).casefold()
                     == ' '.join(ident.split()).casefold()]
            if len(names) > 1:
                raise ValueError('Several schedules have that name. Use list(query=...) and select an ID.')
            job = names[0] if names else None
            if job is not None:
                ident = job['id']
        if action == 'read':
            if t.cron and (ident != t.job_id or job is not None and job['channel'] != t.channel):
                raise PermissionError('A scheduled run can only read its own task')
            if job is None:
                raise ValueError('Unknown schedule. Use list(query=...) then its ID or exact unique name.')
            return {'job': dict(job)}
        if action == 'update':
            if job is None:
                raise ValueError('Unknown schedule. Use list(query=...) then its ID or exact unique name.')
            changes = set(args) - {'action', 'id'}
            if not changes or changes - {'name', 'prompt', 'at', 'cron', 'until', 'quiet'}:
                raise ValueError('Update accepts name, prompt, at or cron, until, quiet')
            updated = dict(job)
            for key in ('name', 'prompt', 'quiet'):
                if key in args:
                    updated[key] = args[key]
            if (not isinstance(updated['name'], str) or not 1 <= len(updated['name']) <= 100
                    or not isinstance(updated['prompt'], str) or not 1 <= len(updated['prompt']) <= 8000):
                raise ValueError('Provide a name (1..100 chars) and prompt (1..8000 chars)')
            timing_changed = bool(changes & {'at', 'cron'})
            if timing_changed:
                if ('at' in args) == ('cron' in args):
                    raise ValueError('Use exactly one of at or cron')
                if 'at' in args:
                    if not isinstance(args['at'], str) or not args['at']:
                        raise ValueError('at must be a future ISO date/time')
                    updated['schedule'] = {'kind': 'at', 'at': args['at']}
                    updated.pop('until', None)
                    updated['quiet'] = False
                else:
                    if (not isinstance(args['cron'], str) or len(args['cron'].split()) != 5
                            or not croniter.is_valid(args['cron'])):
                        raise ValueError('cron must be a valid five-field expression')
                    if updated.get('continuation'):
                        raise ValueError('Callbacks run once; create a separate recurring schedule')
                    updated['schedule'] = {'kind': 'cron', 'expr': args['cron']}
                if 'quiet' in args:
                    updated['quiet'] = args['quiet']
            if 'until' in args:
                updated.pop('until', None)
                if args['until'] is not None:
                    if updated['schedule']['kind'] != 'cron' or not isinstance(args['until'], str):
                        raise ValueError('until is only for recurring cron schedules')
                    end = datetime.fromisoformat(args['until'])
                    if end.tzinfo is None:
                        end = end.replace(tzinfo=TZ)
                    updated['until'] = end.astimezone(TZ).isoformat()
            if (not isinstance(updated.get('quiet', False), bool)
                    or updated.get('quiet') and not updated.get('until')):
                raise ValueError('quiet requires a recurring schedule with an end time')
            if timing_changed:
                updated['next_run_at'] = next_run(updated).isoformat()
                if datetime.fromisoformat(updated['next_run_at']) <= datetime.now(TZ):
                    raise ValueError('Reminder time must be in the future')
            if (updated.get('until') and datetime.fromisoformat(updated['next_run_at'])
                    > datetime.fromisoformat(updated['until'])):
                raise ValueError('No scheduled check occurs before the end time')
            if updated['prompt'] != job['prompt']:
                updated.pop('note', None)
            # Revoke a queued old claim; its stale completion cannot overwrite this edit.
            updated['running'] = False
            updated['last_status'] = 'pending' if updated['enabled'] else 'paused'
            if t.cancelled.is_set():
                raise InterruptedError('Task interrupted; schedule was not updated')
            jobs[jobs.index(job)] = updated
            save_jobs(jobs)
            return dict({k: updated[k] for k in ('id', 'name', 'enabled', 'schedule', 'until', 'quiet', 'next_run_at') if k in updated},
                        changed=sorted(changes), summary=' '.join(updated['prompt'].split())[:140])
        if action in ('delete', 'pause', 'resume'):
            if job is None:
                raise ValueError('Unknown schedule. Use list(query=...) then its ID or exact unique name.')
            if action == 'delete':
                jobs.remove(job)
            else:
                job['enabled'] = action == 'resume'
                if job['enabled']:
                    job['next_run_at'] = next_run(job).isoformat()
                    if datetime.fromisoformat(job['next_run_at']) <= datetime.now(TZ):
                        raise ValueError('Expired one-shot tasks need a new future time')
                    if job.get('until') and datetime.fromisoformat(job['next_run_at']) > datetime.fromisoformat(job['until']):
                        raise ValueError('This recurring task has passed its end time')
                else:
                    job['running'] = False
                job['last_status'] = 'pending' if job['enabled'] else 'paused'
            save_jobs(jobs)
            return {'success': True, 'id': ident, 'action': action}
        if action not in ('create', 'defer'):
            raise ValueError('Use list, read, update, create, defer, pause, resume or delete')
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
        until = args.get('until')
        if until is not None:
            if action != 'create' or spec['kind'] != 'cron' or not isinstance(until, str):
                raise ValueError('until is only for recurring cron schedules')
            end = datetime.fromisoformat(until)
            if end.tzinfo is None:
                end = end.replace(tzinfo=TZ)
            until = end.astimezone(TZ).isoformat()
        quiet = args.get('quiet', False)
        if not isinstance(quiet, bool) or (quiet and until is None):
            raise ValueError('quiet requires a recurring schedule with an end time')
        job = {'id': secrets.token_hex(6), 'name': name, 'prompt': prompt,
               'schedule': spec, 'enabled': True, 'channel': t.channel,
               'last_status': None, 'last_run_at': None}
        if until is not None:
            job['until'] = until
        if quiet:
            job['quiet'] = True
        if action == 'defer':
            job['continuation'] = True
            job['model'] = t.model
        job['next_run_at'] = next_run(job).isoformat()
        if datetime.fromisoformat(job['next_run_at']) <= datetime.now(TZ):
            raise ValueError('Reminder time must be in the future')
        if until is not None and datetime.fromisoformat(job['next_run_at']) > datetime.fromisoformat(until):
            raise ValueError('No scheduled check occurs before the end time')
        jobs.append(job)
        if t.cancelled.is_set():
            raise InterruptedError('Task interrupted; callback was not saved')
        save_jobs(jobs)
        return {'success': True, 'id': job['id'], 'next_run_at': job['next_run_at'],
                'schedule': job['schedule'], 'until': until, 'quiet': quiet,
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
        'name': {'type': 'string', 'maxLength': 80, 'description': '.md filename from list; required for read/write/delete.'},
        'text': {'type': 'string', 'maxLength': 16000}}, 'required': ['action'], 'additionalProperties': False}}
SCHEDULE_SCHEMA = {
    'description': 'Owner schedules, Asia/Seoul. list(query) searches names/instructions; active jobs first, short summaries. '
                   'read(id) shows the full task/note. id also accepts an exact unique name; ambiguous names need an ID. '
                   'Edit existing jobs with update, not create: only supplied name/prompt/at/cron/until/quiet change. '
                   'Paused jobs stay paused until resume. cron repeats; at runs once. quiet+until sends only the final summary; '
                   'until=null removes the end. Verify the returned schedule/changed fields. '
                   'defer(after_seconds,prompt) resumes this interactive task once. pause/delete manage jobs; /stop stops current work. '
                   'Scheduled runs: own read/note only, no secrets. Root still needs approval.',
    'parameters': {'type': 'object', 'properties': {
        'action': {'type': 'string', 'enum': ['list', 'read', 'update', 'create', 'defer', 'pause', 'resume', 'delete', 'note']},
        'id': {'type': 'string', 'description': 'ID or exact unique name from list; omit for a scheduled run reading itself.'},
        'query': {'type': 'string', 'maxLength': 200, 'description': 'list only: words matched in the name or instructions.'},
        'name': {'type': 'string'}, 'prompt': {'type': 'string'},
        'at': {'type': 'string'}, 'cron': {'type': 'string'}, 'until': {'type': ['string', 'null']},
        'quiet': {'type': 'boolean'}, 'text': {'type': 'string', 'maxLength': 4000},
        'after_seconds': {'type': 'integer', 'minimum': 1, 'maximum': 86400}}, 'required': ['action']}}
WAIT_SCHEMA = {
    'description': 'Pause before checking an in-progress task again. No model requests while sleeping. '
                   'Prefer short polling for work expected within a minute; defer longer waits. '
                   'Maximum 300 seconds TOTAL per turn; use schedule action=defer for longer waits. '
                   'New owner messages and /stop interrupt the wait. Check state with the relevant tool after waiting.',
    'parameters': {'type': 'object', 'properties': {
        'seconds': {'type': 'integer', 'minimum': 1, 'maximum': 300}}, 'required': ['seconds']}}
USER_INPUT_SCHEMA = {
    'description': 'Ask the owner one concise clarification or choice and collect their replies in this channel. '
                   'Do not repeat the question in separate text. No model requests while waiting. '
                   'Returns answered with the reply, or skipped/timeout with permission_granted=false. '
                   'Skip or timeout does NOT approve the proposed action: do not execute it or an equivalent workaround. '
                   'If intent or required information is still unclear, stop the affected task and state the blocker. '
                   'Do not guess or repeat the same approval request. '
                   'Timeout after 5 minutes without owner activity; messages and typing extend the wait. '
                   'After replies, resume once typing has expired and the channel has been quiet for 15 seconds. '
                   'Multiple messages become one answer. This human-input wait is separate from the polling budget. '
                   'The owner can Flush collected replies immediately or Skip without an answer. '
                   'This is not root approval; root commands still need their dedicated approval.',
    'parameters': {'type': 'object', 'properties': {
        'question': {'type': 'string', 'minLength': 1, 'maxLength': 1000}},
        'required': ['question']}}
