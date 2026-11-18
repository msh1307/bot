"""Existing owner integrations plus small, explicit memory and schedule tools."""
import asyncio
from datetime import datetime, timedelta
import json
from pathlib import Path
import secrets
import threading
import time
import urllib.request
from zoneinfo import ZoneInfo

from croniter import croniter
from . import context
from .screen_privacy import filter_result as filter_screen
from .integrations import host, gmail, access, codex, browser, rdp, credential_input, remote, remote_files, coding, web_remote, terminal

STATE = Path('/var/lib/evo-bot')
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
        await asyncio.gather(*(b.close() for b in codex.SERVERS.values()), return_exceptions=True)
        await asyncio.to_thread(browser.close)
        await asyncio.to_thread(web_remote.CLIENT.close)
        await asyncio.to_thread(rdp.close)
        await asyncio.to_thread(terminal.close)

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
            result = await asyncio.to_thread(web_remote.call,name,args,turn)
            instance = web_remote.CLIENT.instance
            if result is None:
                result = await asyncio.to_thread(http_json, '/tools', {'tool': name, 'params': args})
                if 'error' in result: raise RuntimeError(result['error'])
                result = result.get('plain_text', result)
                instance = 'localhost'
            result = web_remote.remember(result,instance)
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
    if turn.cancelled.wait(seconds):
        raise InterruptedError('Wait interrupted')
    return {'waited_seconds': seconds}


def cancel_deferred(channel=None):
    """Explicit stop/reset commands invalidate pending task continuations only."""
    with JOB_LOCK:
        jobs, changed = load_jobs(), False
        for job in jobs:
            if ((job.get('continuation') or job.get('mode') == 'task' or 'task_state' in job) and (channel is None or job['channel'] == channel)
                    and (job.get('enabled') or job.get('running') or job.get('task_state') in ('continue', 'waiting'))):
                job.update(enabled=False, running=False, last_status='cancelled')
                if 'task_state' in job:
                    job['task_state'] = 'blocked'
                changed = True
        if changed:
            save_jobs(jobs)


def running_task(jobs, turn):
    job = next((j for j in jobs if j['id'] == turn.job_id and j['channel'] == turn.channel), None)
    if not (turn.task_run and job and job.get('running') and job.get('last_run_at') == turn.task_run):
        raise InterruptedError('Task was stopped or changed; this run can no longer update it')
    return job


def wake_task(ident, channel, text):
    """Coalesce events into the existing task; never replay a terminal task."""
    with JOB_LOCK:
        jobs = load_jobs()
        job = next((j for j in jobs if j['id'] == ident and j['channel'] == channel), None)
        if not job or 'task_state' not in job:
            return False
        if (job['task_state'] in ('done', 'blocked') or job.get('last_status') == 'paused'
                or time.time() >= job['task_deadline']):
            return True
        job['wake_note'] = (job.get('wake_note', '') + '\n' + text)[-8000:]
        if not job.get('running'):
            job.update(enabled=True, next_run_at=datetime.now(TZ).isoformat())
        save_jobs(jobs)
        return True


def schedule(args):
    t = context.owner_identity()
    if t is None:
        raise PermissionError('Schedules require an owner Discord message')
    action = args.get('action', 'list')
    if 'handoff' in args:
        if type(args['handoff']) is not bool or action != 'enqueue' or t.job_id:
            raise ValueError('handoff is a boolean for enqueue from ordinary chat only')
    if 'state' in args and action != 'note':
        raise ValueError('state is only for note on the current task')
    if t.cron and action not in ('read', 'note') and not (action == 'defer' and t.task_run):
        raise PermissionError('A read-only check may read/note its own job; a one-shot check may defer itself')
    with JOB_LOCK:
        jobs = load_jobs()
        if action == 'note':
            if not t.job_id:
                raise PermissionError('note is only for the current scheduled task; in ordinary chat use defer(after_seconds,prompt)')
            note = args.get('text')
            if not isinstance(note, str) or len(note) > 4000:
                raise ValueError('Progress note must be at most 4000 characters')
            if 'compact' in args and type(args['compact']) is not bool:
                raise ValueError('compact needs a boolean')
            own = next((j for j in jobs if j['id'] == t.job_id and j['channel'] == t.channel), None)
            if own is None:
                raise ValueError('Scheduled job is no longer available')
            if 'state' in args or 'after_seconds' in args:
                if t.cron and 'task_state' not in own:
                    raise ValueError('Recurring checks use note(text); their next run is already scheduled')
                own = running_task(jobs, t)
                state = args.get('state')
                if state not in ('continue', 'waiting', 'done', 'blocked') or not note.strip():
                    raise ValueError('state needs continue|waiting|done|blocked and a nonempty progress/evidence note')
                when = None
                if 'after_seconds' in args:
                    seconds = args['after_seconds']
                    if state != 'waiting' or type(seconds) is not int or not 1 <= seconds <= 600:
                        raise ValueError('A timed recheck needs note(state=waiting, text=next check, after_seconds=1..600)')
                    when = (datetime.now(TZ) + timedelta(seconds=seconds)).isoformat()
                    if datetime.fromisoformat(when).timestamp() >= own['task_deadline']:
                        raise ValueError('Task lifetime ends before this recheck')
                own.update(task_state=state, task_reported=True)
                if state == 'waiting':
                    own['compact'] = args.get('compact', False)
                own.pop('resume_at', None)
                if when:
                    own['resume_at'] = when
                    own['next_run_at'] = when
            own['note'] = note
            if 'compact' in args:
                own['compact'] = args['compact']
            if t.cancelled.is_set():
                raise InterruptedError('Task interrupted; progress was not saved')
            save_jobs(jobs)
            return dict(saved=True, job_id=t.job_id, **({'state': own['task_state']} if 'state' in args else {}),
                        **({'next_run_at': own['resume_at']} if own.get('resume_at') else {}))
        if action == 'list':
            query = args.get('query', '')
            if not isinstance(query, str) or len(query) > 200:
                raise ValueError('query must be at most 200 characters')
            terms = query.casefold().split()
            matches = [j for j in jobs if all(term in (j['name'] + '\n' + j['prompt']).casefold()
                                             for term in terms)]
            matches.sort(key=lambda j: (not j.get('running', False), not j.get('enabled', False)))
            return {'jobs': [dict({k: j[k] for k in ('id', 'name', 'enabled', 'running', 'continuation',
                                    'schedule', 'mode', 'task_state', 'task_deadline', 'until', 'quiet', 'next_run_at', 'last_status', 'queue_timeout_seconds') if k in j},
                                 summary=' '.join(j['prompt'].split())[:140]) for j in matches]}
        ident = args.get('id', t.job_id or None)
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
            if not changes or changes - {'name', 'prompt', 'at', 'cron', 'until', 'quiet', 'queue_timeout_seconds', 'compact'}:
                raise ValueError('Update accepts name, prompt, at or cron, until, quiet, queue_timeout_seconds, compact')
            updated = dict(job)
            if 'compact' in args:
                if type(args['compact']) is not bool: raise ValueError('compact needs a boolean')
                updated['compact'] = args['compact']
            if 'queue_timeout_seconds' in args:
                updated['queue_timeout_seconds'] = queue_timeout(args)
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
                if (updated.get('continuation') and (datetime.fromisoformat(updated['next_run_at'])
                        - datetime.now(TZ)).total_seconds() > 600):
                    raise ValueError('Task callbacks may be delayed by at most 600 seconds')
            if (updated.get('task_deadline') and datetime.fromisoformat(updated['next_run_at']).timestamp()
                    >= updated['task_deadline']):
                raise ValueError('Task lifetime ends before this run; create a new owner task')
            if (updated.get('until') and datetime.fromisoformat(updated['next_run_at'])
                    > datetime.fromisoformat(updated['until'])):
                raise ValueError('No scheduled check occurs before the end time')
            if updated['prompt'] != job['prompt']:
                updated.pop('note', None)
            # Only new instructions/timing replace a run; metadata edits must not replay it.
            if not job.get('running') or changes & {'prompt', 'at', 'cron'}:
                updated['running'] = False
                awaiting_event = (updated.get('task_state') == 'waiting' and job.get('last_status') != 'paused')
                updated['last_status'] = 'pending' if updated['enabled'] or awaiting_event else 'paused'
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
                    if 'task_state' in job:
                        job.update(task_state='continue')
                    job['next_run_at'] = (datetime.now(TZ).isoformat() if 'task_state' in job
                                          else next_run(job).isoformat())
                    if 'task_state' not in job and datetime.fromisoformat(job['next_run_at']) <= datetime.now(TZ):
                        raise ValueError('Expired one-shot tasks need a new future time')
                    if job.get('until') and datetime.fromisoformat(job['next_run_at']) > datetime.fromisoformat(job['until']):
                        raise ValueError('This recurring task has passed its end time')
                    if job.get('task_deadline') and datetime.fromisoformat(job['next_run_at']).timestamp() >= job['task_deadline']:
                        raise ValueError('This task has passed its 12-hour lifetime')
                else:
                    job['running'] = False
                job['last_status'] = 'pending' if job['enabled'] else 'paused'
            save_jobs(jobs)
            return {'success': True, 'id': ident, 'action': action}
        if action not in ('create', 'defer', 'enqueue'):
            raise ValueError('Use list, read, update, create, defer, enqueue, pause, resume or delete')
        # Completed callbacks are not permanent reminders and must not fill the job limit.
        terminal = {'ok', 'error', 'interrupted', 'delivery_failed', 'cancelled', 'interrupted_by_restart', 'queue_expired'}
        jobs = [j for j in jobs if not (j.get('continuation') and j.get('task_state') not in ('waiting', 'blocked') and not j.get('enabled')
                                      and not j.get('running') and j.get('last_status') in terminal)]
        if sum(j.get('last_status') != 'paused'
               and (not j.get('task_deadline') or time.time() < j['task_deadline'])
               and bool(j.get('enabled') or j.get('running') or j.get('task_state') in ('continue','waiting'))
               for j in jobs) >= 64 and not (t.task_run and action in ('defer', 'enqueue')):
            raise ValueError('64 active schedules; pause or finish one before adding another')
        prompt, name = args.get('prompt', ''), args.get('name', 'Follow-up' if action in ('defer', 'enqueue') else '')
        if (not isinstance(prompt, str) or not isinstance(name, str)
                or not 1 <= len(prompt) <= 8000 or not 1 <= len(name) <= 100):
            raise ValueError('Provide a name and prompt')
        timeout = queue_timeout(args)
        mode = args.get('mode', 'check' if action == 'create' and args.get('cron') else 'task')
        if mode not in ('task', 'check') or action != 'create' and 'mode' in args:
            raise ValueError('mode=task|check is for create; callbacks continue the current task')
        if action in ('defer', 'enqueue'):
            seconds = 0 if action == 'enqueue' else args.get('after_seconds')
            if (type(seconds) is not int or action == 'defer' and not 1 <= seconds <= 600
                    or args.get('at') or args.get('cron') or action == 'enqueue' and 'after_seconds' in args):
                raise ValueError('defer needs after_seconds (1..600); enqueue is immediate; neither takes at/cron')
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
        compact = args.get('compact')
        if compact is not None and not isinstance(compact, bool):
            raise ValueError('compact needs a boolean')
        if t.task_run and action in ('defer', 'enqueue'):
            own = running_task(jobs, t)
            if t.cron and (action != 'defer' or own.get('mode', 'check') != 'check'
                           or own['schedule']['kind'] != 'at'):
                raise PermissionError('A read-only check may only defer its own one-shot run')
            if len(prompt) > 4000:
                raise ValueError('Task continuation checkpoint must be at most 4000 characters')
            when = spec['at']
            if datetime.fromisoformat(when).timestamp() >= own['task_deadline']:
                raise ValueError('Task lifetime ends before this callback')
            own.update(task_state='waiting', task_reported=True, resume_at=when,
                       next_run_at=when, note=prompt, compact=False if compact is None else compact)
            save_jobs(jobs)
            return {'success': True, 'id': own['id'], 'state': 'waiting', 'next_run_at': when}
        job = {'id': secrets.token_hex(6), 'name': name, 'prompt': prompt,
               'schedule': spec, 'enabled': True, 'channel': t.channel,
               'last_status': None, 'last_run_at': None, 'queue_timeout_seconds': timeout}
        job['mode'] = mode
        if mode == 'task': job['model'] = t.model
        if until is not None:
            job['until'] = until
        if quiet:
            job['quiet'] = True
        if action in ('defer', 'enqueue'):
            job['continuation'] = True
            job['model'] = t.model
            job['compact'] = True if compact is None else compact
        if spec['kind'] == 'at':
            job['task_state'] = 'continue'
            job['compact'] = True if compact is None else compact
        job['next_run_at'] = next_run(job).isoformat()
        if mode == 'task' or spec['kind'] == 'at' or t.task_deadline:
            # A continuation inherits the original budget; rescheduling cannot renew it.
            base = (max(time.time(), datetime.fromisoformat(job['next_run_at']).timestamp())
                    if action == 'create' else time.time())
            job['task_deadline'] = t.task_deadline or base + 12 * 3600
            if datetime.fromisoformat(job['next_run_at']).timestamp() >= job['task_deadline']:
                raise ValueError('Task lifetime ends before this callback; ask the owner for a new task')
            parent = next((j for j in jobs if j['id'] == t.job_id), None)
            if parent and parent.get('note'):
                job['note'] = parent['note']
        if action != 'enqueue' and datetime.fromisoformat(job['next_run_at']) <= datetime.now(TZ):
            raise ValueError('Reminder time must be in the future')
        if until is not None and datetime.fromisoformat(job['next_run_at']) > datetime.fromisoformat(until):
            raise ValueError('No scheduled check occurs before the end time')
        jobs.append(job)
        if t.cancelled.is_set():
            raise InterruptedError('Task interrupted; callback was not saved')
        save_jobs(jobs)
        return {'success': True, 'id': job['id'], 'next_run_at': job['next_run_at'],
                'schedule': job['schedule'], 'until': until, 'quiet': quiet,
                'continuation': action in ('defer', 'enqueue'), 'mode': mode,
                **({'handoff': True} if args.get('handoff') else {}),
                'task_deadline': job.get('task_deadline'), 'compact': job.get('compact'), 'queue_timeout_seconds': timeout}


def queue_timeout(args):
    seconds = args.get('queue_timeout_seconds', 43200)
    if type(seconds) is not int or not 1 <= seconds <= 43200:
        raise ValueError('queue_timeout_seconds must be 1..43200; task lifetime remains bounded separately')
    return seconds


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
    'description': 'Tasks/reminders, Asia/Seoul. create(name,prompt,at OR cron); at defaults to task, cron to read-only check. '
                   'list(query) finds jobs; read(id) shows instructions/progress. id accepts an ID or exact unique name. '
                   'update changes supplied fields only; pause/resume/delete manage existing jobs. '
                   'note is scheduled-run only: ONE note(text,state) reports the result and next step; omit id. '
                   'done=verified complete; blocked=cannot proceed; waiting=await input/event; continue=more work now. '
                   'For a timed recheck, use note(text=next check,state=waiting,after_seconds=1..600); this also saves progress and schedules it. '
                   'Codex wake=true handles completion events. In ordinary chat, defer(after_seconds,prompt) schedules a callback; enqueue(prompt) queues immediate work. '
                   'enqueue(prompt,handoff=true) transfers the current work into that task before launching workers. '
                   'Callbacks reuse the current task and its fixed 12-hour deadline. Check mode stays read-only; recurring checks only note(text). '
                   'New tasks compact first by default; waiting resumes do not unless compact=true is supplied. '
                   'quiet+until reports only the final recurring check; until=null removes the end. '
                   'enabled controls future scheduling; last_status describes the last run, not whether the schedule is paused. '
                   'Queue wait defaults to 12 hours. Paused/expired jobs do not run; /stop cancels current work.',
    'parameters': {'type': 'object', 'properties': {
        'action': {'type': 'string', 'enum': ['list', 'read', 'update', 'create', 'defer', 'enqueue', 'pause', 'resume', 'delete', 'note']},
        'id': {'type': 'string', 'description': 'Other job ID or exact name; omit for the current task.'},
        'query': {'type': 'string', 'maxLength': 200, 'description': 'list only: words matched in the name or instructions.'},
        'state': {'type': 'string', 'enum': ['continue', 'waiting', 'done', 'blocked'], 'description': 'Scheduled-run note only: continue now, wait for input/event, verified done, or blocked. Add after_seconds to waiting for a timed recheck.'},
        'name': {'type': 'string'}, 'prompt': {'type': 'string'},
        'at': {'type': 'string'}, 'cron': {'type': 'string'}, 'until': {'type': ['string', 'null']},
        'mode': {'type': 'string', 'enum': ['task', 'check'], 'description': 'create only; task acts on owner instructions, check only observes. Defaults: at=task, cron=check.'},
        'quiet': {'type': 'boolean'}, 'compact': {'type': 'boolean', 'description': 'Force compaction before the next run: new tasks default true, waiting resumes false. Normal context-limit compaction stays enabled.'}, 'text': {'type': 'string', 'maxLength': 4000},
        'handoff': {'type': 'boolean', 'description': 'Ordinary-chat enqueue only: true saves the task and ends this turn so that task starts next. Default false just adds work.'},
        'queue_timeout_seconds': {'type': 'integer', 'minimum': 1, 'maximum': 43200},
        'after_seconds': {'type': 'integer', 'minimum': 1, 'maximum': 600, 'description': 'Delay for note(state=waiting), or defer outside the current task.'}}, 'required': ['action']}}
WAIT_SCHEMA = {
    'description': 'Wait 1..300 seconds per call without model requests. Calls have independent limits. '
                   '/stop interrupts the wait; owner addenda apply after it ends. Check state with the relevant tool after waiting.',
    'parameters': {'type': 'object', 'properties': {
        'seconds': {'type': 'integer', 'minimum': 1, 'maximum': 300}}, 'required': ['seconds']}}
USER_INPUT_SCHEMA = {
    'description': 'Get missing owner input: clarification, confirmation or a checkpoint. '
                   'Do not re-confirm already-authorized work. '
                   'Required owner confirmation must be explicit; no reply means stop that step. '
                   'On confirmation, resume the pending step and verify the result. '
                   'Send the prompt here once; do not repeat it in chat or use polling/sleep to wait for the owner. '
                   'Waits without model calls. Multiple messages/images become one reply; typing extends the wait. '
                   'Timeout: 5 minutes idle. After replies: 15 seconds quiet, or immediate Flush. Skip ends the wait. '
                   'Answers enter the conversation as owner messages. Skip/timeout returns permission_granted=false. '
                   'Skip/timeout grants no permission: stop that action and continue independent authorized work; do not guess, retry approval or work around it. '
                   'Separate from root approval cards.',
    'parameters': {'type': 'object', 'properties': {
        'question': {'type': 'string', 'minLength': 1, 'maxLength': 1000,
                     'description': 'Brief question, confirmation request or checkpoint shown to the owner.'}},
        'required': ['question']}}
