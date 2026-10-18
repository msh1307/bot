"""Small owner-only Discord frontend. OMP owns the model/tool execution loop."""
import asyncio
from collections import OrderedDict
import contextlib
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import shutil
import signal
import time

import discord
from discord.ext import commands
from . import context
from .discord_text import messages as discord_messages
from .rpc import OmpSession, WORKSPACE
from .tools import Registry, STATE, TZ, JOB_LOCK, load_jobs, save_jobs, next_run, image_content, cancel_deferred

LOG = logging.getLogger('evo-omp')
DEFAULT_MODEL = 'sc117-flash-next-iq3_xxs'
MODELS = {'flash': DEFAULT_MODEL, '27b': 'qwen3.8-27b-uncensored'}
PROGRESS_INTERVAL = 30


class Bot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix='!', intents=intents,
                         allowed_mentions=discord.AllowedMentions.none(), max_messages=64)
        self.registry = Registry()
        self.sessions = OrderedDict()
        self.models = {}
        self.queue = asyncio.Queue(maxsize=16)
        self.active = None
        self.active_done = asyncio.Event()
        self.active_done.set()
        self.admission_lock = asyncio.Lock()
        self.epochs = {}
        self.reset_epoch = 0
        self.initialized = False
        self.shutting_down = False
        self.worker_failure = None

    async def _check_slash_authorization(self, interaction, command):
        if str(interaction.user.id) == context.OWNER:
            return True
        await interaction.response.send_message('오너만 사용할 수 있습니다.', ephemeral=True)
        return False

    async def setup_hook(self):
        context.client = self
        await self.registry.initialize_web()
        for factory in self.registry.factories:
            factory(self, self)
        for name in ('help', 'status', 'context', 'stop', 'new', 'reset', 'compact',
                     'model', 'memory', 'alarms', 'approve', 'deny', 'whoami'):
            self.tree.add_command(discord.app_commands.Command(name=name,
                description=DESCRIPTIONS[name], callback=self.slash_callback(name)), override=True)
        try:
            await self.tree.sync()
            # Retire duplicate guild command registrations left by the old gateway.
            guild = discord.Object(id=990826359704154222)
            self.tree.clear_commands(guild=guild)
            await self.tree.sync(guild=guild)
        except Exception as e:
            LOG.error('slash_sync_failed %s', type(e).__name__)
        self.worker = asyncio.create_task(self.work_loop())
        self.worker.add_done_callback(self.worker_finished)
        # An interrupted service must not leave a schedule permanently 'running'.
        await asyncio.to_thread(self.recover_schedules)
        self.scheduler = asyncio.create_task(self.schedule_loop())

    @staticmethod
    def recover_schedules():
        with JOB_LOCK:
            jobs = load_jobs()
            changed = False
            for job in jobs:
                if job.pop('running', False):
                    changed = True
                    job['last_status'] = 'interrupted_by_restart'
                    # Never replay a callback whose actions may already have run.
                    if job.get('continuation'):
                        job['enabled'] = False
                    elif job['schedule']['kind'] == 'at':
                        job['enabled'] = True
            if changed:
                save_jobs(jobs)

    def slash_callback(self, name):
        async def callback(interaction: discord.Interaction, args: str = ''):
            if not await self._check_slash_authorization(interaction, name):
                return
            await interaction.response.defer(ephemeral=True, thinking=True)

            async def reply(text):
                if name == 'compact' and interaction.is_expired():
                    await self.deliver(interaction.channel, text)
                else:
                    await interaction.edit_original_response(content=text)

            try:
                answer = await self.command(interaction.channel, name, args)
                await reply(answer or '완료')
            except Exception as e:
                await reply('실패: ' + context.redact_sensitive_text(str(e))[:500])
        return callback

    async def on_ready(self):
        self.initialized = True
        self.write_status('connected')
        LOG.info('discord_connected bot_id=%s engine=omp-18.11.3', self.user.id)

    async def on_disconnect(self):
        context.cancel_all()
        if self.active:
            self.active['turn'].cancelled.set()
            asyncio.create_task(self.active['session'].abort())
        self.write_status('disconnected')

    async def on_message(self, message):
        if message.author.bot or str(message.author.id) != context.OWNER:
            return
        text = message.content.strip()
        if text.startswith(('/', '!')):
            name, _, args = text[1:].partition(' ')
            if name in DESCRIPTIONS or name in ('temp', 'access', 'compress'):
                try:
                    result = await self.command(message.channel, name, args)
                    if result:
                        await self.deliver(message.channel, result)
                except Exception as e:
                    await self.deliver(message.channel, '실패: ' + context.redact_sensitive_text(str(e))[:500])
                return
        if not text and not message.attachments:
            return
        images = []
        try:
            for attachment in message.attachments[:2]:
                if (attachment.content_type or '').startswith('image/'):
                    if attachment.size > 6 * 2**20:
                        raise ValueError('이미지는 6MiB 이하로 올려주세요.')
                    images.append(await asyncio.to_thread(image_content, await attachment.read()))
                else:
                    if attachment.size > 16 * 2**20:
                        raise ValueError('첨부파일은 16MiB 이하로 올려주세요.')
                    dest = WORKSPACE / 'downloads'
                    dest.mkdir(exist_ok=True)
                    if dest.is_symlink() or dest.resolve().parent != WORKSPACE.resolve():
                        raise ValueError('첨부파일 저장 경로를 확인해 주세요.')
                    name = Path(attachment.filename).name
                    if name in ('', '.', '..'):
                        raise ValueError('파일 이름이 잘못되었습니다.')
                    p = dest / (str(message.id) + '-' + name)
                    # Exclusive creation; no model-controlled host path.
                    with p.open('xb') as f:
                        f.write(await attachment.read())
                    text += '\nAttached file: /workspace/downloads/' + p.name
            if images and sum(len(i['data']) for i in images) > 700_000:
                raise ValueError('큰 이미지는 한 장씩 올려주세요.')
            channel = str(message.channel.id)
            await self.interrupt_compaction(channel)
            async with self.admission_lock:
                if self.queue.full():
                    raise ValueError('요청 대기열이 찼습니다. 잠시 뒤 다시 보내주세요.')
                if self.active and (self.active['channel'] == channel
                                    or self.active['turn'].cron or self.active['turn'].deferred):
                    await self.stop_active()
                    await self.deliver(message.channel, '진행 중 작업을 중단하고 새 메시지를 처리합니다.')
                self.enqueue(channel, text or '첨부 이미지를 확인해줘.', images)
        except Exception as e:
            await self.deliver(message.channel, context.redact_sensitive_text(str(e))[:500])

    async def session(self, key, cron=False):
        model = self.models.get(key, DEFAULT_MODEL)
        if cron:
            # A briefing must not evict the owner's interactive conversation.
            session = OmpSession(key, model, self.registry.definitions(True), self.registry.call)
            try:
                return await session.start()
            except Exception:
                await self.release_session(session)
                raise
        session = self.sessions.get(key)
        if session and (not session.transport_alive or session.model != model):
            await self.release_session(session)
            session = None
        if session:
            self.sessions.move_to_end(key)
            return session
        while len(self.sessions) >= 2:
            _, old = self.sessions.popitem(last=False)
            await self.release_session(old)
        session = OmpSession(key, model, self.registry.definitions(cron), self.registry.call)
        try:
            await session.start()
        except Exception:
            await self.release_session(session)
            raise
        self.sessions[key] = session
        return session

    async def release_session(self, session):
        try:
            await session.close()
        except Exception as e:
            LOG.error('session_cleanup_failed class=%s detail=%s', type(e).__name__,
                      context.redact_sensitive_text(str(e))[:300])
            # Do not admit another session while a failed cleanup could leave
            # a live child behind. systemd restarts this cgroup as one unit.
            self.worker_failure = 'session cleanup failed'
            asyncio.create_task(self.close())
            raise
        finally:
            if self.sessions.get(session.key) is session:
                self.sessions.pop(session.key, None)

    def worker_finished(self, task):
        if self.shutting_down or self.is_closed() or task.cancelled():
            return
        error = task.exception()
        self.worker_failure = type(error).__name__ if error else 'worker exited'
        LOG.error('worker_stopped class=%s; restarting service', self.worker_failure)
        asyncio.create_task(self.close())

    async def work_loop(self):
        await self.wait_until_ready()
        while not self.is_closed():
            item = await self.queue.get()
            channel = None
            session = None
            job = item['job']
            deferred = bool(job and job.get('continuation'))
            turn = context.Turn(item['channel'], cron=bool(job and not deferred), deferred=deferred)
            token = context.current.set(turn)
            outcome = 'error'
            try:
                async with self.admission_lock:
                    if (item['epoch'] != self.epochs.get(item['channel'], 0)
                            or item['reset_epoch'] != self.reset_epoch):
                        outcome = 'cancelled'
                        continue
                    if job and not await asyncio.to_thread(self.job_claim_valid, job):
                        outcome = 'cancelled'
                        continue
                    channel = self.get_channel(int(item['channel'])) or await self.fetch_channel(int(item['channel']))
                    key = 'cron:' + job['id'] if turn.cron else item['channel']
                    previous = self.sessions.get(key)
                    if deferred and job.get('model') in MODELS.values():
                        self.models.setdefault(key, job['model'])
                    session = await self.session(key, turn.cron)
                    reused = previous is session
                    turn.model = session.model
                    self.active_done.clear()
                    self.active = {'channel': item['channel'], 'session': session, 'turn': turn, 'start': time.monotonic()}
                    self.write_status('working')
                async with channel.typing(), self.progress(channel, session, enabled=not turn.cron):
                    text = item['text']
                    if deferred:
                        text = ('[Scheduled continuation of the owner task; not a new owner message. '
                                + ('Earlier conversation is available.' if reused else 'Earlier conversation is unavailable; use the task details below.')
                                + ' Check current state before taking action. Root still requires per-command approval.]\n'
                                + text)
                    answer = await session.prompt(text, turn, item.get('images'))
                if turn.cancelled.is_set():
                    outcome = 'interrupted'
                    if session.error:
                        await self.deliver(channel, session.error)
                else:
                    if not answer:
                        raise RuntimeError(session.error or '모델이 최종 답변을 반환하지 않았습니다. 다시 요청할 수 있습니다.')
                    outcome = 'ok' if await self.deliver(channel, answer) else 'delivery_failed'
            except Exception as e:
                LOG.error('turn_failed turn=%s class=%s detail=%s', turn.id, type(e).__name__,
                          context.redact_sensitive_text(str(e))[:400])
                if channel and (not turn.cancelled.is_set()
                                or (session and session.abort_reason == 'timeout')):
                    await self.deliver(channel, '작업 실패: ' + context.redact_sensitive_text(str(e))[:500])
                if session:
                    if not turn.cron and await session.reusable_after_error():
                        LOG.info('turn_failed_session_preserved turn=%s', turn.id)
                    else:
                        await self.release_session(session)
            finally:
                try:
                    if item['job']:
                        try:
                            await asyncio.to_thread(self.finish_job, job, outcome)
                        except Exception as e:
                            LOG.error('schedule_finalize_failed job=%s class=%s',
                                      item['job']['id'], type(e).__name__)
                        finally:
                            if session and turn.cron:
                                await self.release_session(session)
                finally:
                    self.active = None
                    self.active_done.set()
                    context.current.reset(token)
                    self.queue.task_done()
                    self.write_status('connected' if self.is_ready() else 'disconnected')

    @staticmethod
    def job_claim_valid(claim):
        with JOB_LOCK:
            return any(j['id'] == claim['id'] and j.get('running')
                       and j.get('last_run_at') == claim.get('last_run_at') for j in load_jobs())

    @staticmethod
    def finish_job(claim, outcome):
        with JOB_LOCK:
            jobs = load_jobs()
            job = next((j for j in jobs if j['id'] == claim['id']), None)
            if job and job.get('running') and job.get('last_run_at') == claim.get('last_run_at'):
                job['last_status'] = outcome
                job['running'] = False
                if outcome == 'queue_full':
                    job['enabled'] = True
                    job['next_run_at'] = claim['due_at']
                save_jobs(jobs)

    async def schedule_loop(self):
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                capacity = self.queue.maxsize - self.queue.qsize()
                due = await asyncio.to_thread(self.claim_due, capacity)
                for job in due:
                    if self.queue.full():
                        await asyncio.to_thread(self.finish_job, job, 'queue_full')
                    else:
                        self.enqueue(job['channel'], job['prompt'], [], job)
            except Exception as e:
                LOG.error('schedule_failed %s', type(e).__name__)
            await asyncio.sleep(5)

    @staticmethod
    def claim_due(limit=16):
        now = datetime.now(TZ)
        with JOB_LOCK:
            jobs, due = load_jobs(), []
            for job in jobs:
                if len(due) >= limit:
                    break
                when = job.get('next_run_at')
                if not job.get('enabled') or not when or job.get('running'):
                    continue
                if datetime.fromisoformat(when).astimezone(TZ) <= now:
                    job['running'] = True
                    job['last_run_at'] = now.isoformat()
                    job['due_at'] = when
                    if job['schedule']['kind'] == 'at':
                        job['enabled'] = False
                    else:
                        job['next_run_at'] = next_run(job).isoformat()
                    due.append(dict(job))
            if due:
                save_jobs(jobs)
            return due

    @contextlib.asynccontextmanager
    async def progress(self, channel, session, enabled=True):
        """One temporary status for long interactive turns; no model text or commands."""
        if not enabled:
            yield
            return
        notice, started = None, time.monotonic()

        async def update():
            nonlocal notice
            while True:
                await asyncio.sleep(PROGRESS_INTERVAL)
                seconds = int(time.monotonic() - started)
                elapsed = f'{seconds // 60}분 {seconds % 60}초' if seconds >= 60 else f'{seconds}초'
                text = f'⏳ 작업 중 · {elapsed} · 도구 {session.tools}회'
                try:
                    if notice is None:
                        notice = await asyncio.wait_for(channel.send(text,
                            allowed_mentions=discord.AllowedMentions.none()), 5)
                    else:
                        await asyncio.wait_for(notice.edit(content=text,
                            allowed_mentions=discord.AllowedMentions.none()), 5)
                except (discord.HTTPException, asyncio.TimeoutError) as error:
                    LOG.warning('progress_update_failed class=%s', type(error).__name__)
                    return

        task = asyncio.create_task(update())
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if notice is not None:
                with contextlib.suppress(discord.HTTPException, asyncio.TimeoutError):
                    await asyncio.wait_for(notice.delete(), 3)

    async def deliver(self, channel, text):
        text = context.redact_sensitive_text(text)
        for part in discord_messages(text):
            try:
                await channel.send(part, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException as e:
                LOG.error('discord_delivery_failed class=%s status=%s', type(e).__name__, e.status)
                return False
        return True

    def enqueue(self, channel, text, images, job=None):
        self.queue.put_nowait({'channel': channel, 'text': text, 'images': images, 'job': job,
            'epoch': self.epochs.get(channel, 0), 'reset_epoch': self.reset_epoch})

    async def stop_active(self):
        active = self.active
        if not active:
            return
        active['turn'].cancelled.set()
        await active['session'].abort()
        try:
            await asyncio.wait_for(self.active_done.wait(), 25)
        except asyncio.TimeoutError:
            await active['session'].close()
            await asyncio.wait_for(self.active_done.wait(), 10)

    async def interrupt_compaction(self, key=None):
        # Abort before waiting for admission: manual compaction holds that lock.
        for channel, session in list(self.sessions.items()):
            if session.compacting and (key is None or channel == key):
                await session.abort()

    def write_status(self, state):
        a = self.active
        data = {'engine': 'omp', 'version': '18.11.3', 'state': state,
                'queued': self.queue.qsize(), 'sessions': len(self.sessions),
                'active': bool(a), 'rounds': a['session'].rounds if a else 0,
                'tools': a['session'].tools if a else 0, 'updated': int(time.time())}
        p = Path('/run/evo-omp/status.json')
        with contextlib.suppress(OSError):
            p.with_suffix('.tmp').write_text(json.dumps(data))
            p.with_suffix('.tmp').replace(p)

    async def command(self, channel, name, args):
        if name == 'compress':
            name = 'compact'  # Compatibility for previously typed commands only.
        turn = context.Turn(str(channel.id))
        token = context.current.set(turn)
        try:
            key = str(channel.id)
            if name in self.registry.commands:
                return await self.registry.commands[name](args)
            if name == 'help':
                return '/status · /temp · /model flash 또는 27b · /stop · /new · /reset · /compact · /memory · /alarms · /access\n새 메시지를 보내면 같은 채널의 진행 중 작업을 중단하고 이어서 처리합니다.'
            if name == 'whoami':
                return '오너 · 관리자 권한 · OMP 18.11.3 · root는 매번 Allow Once 승인'
            if name in ('status', 'context'):
                a = self.active
                if name == 'context':
                    s = self.sessions.get(key)
                    if s:
                        state = await s.request('get_state')
                        return f"OMP · {s.model}\nContext: " + json.dumps(state.get('contextUsage'), ensure_ascii=False)
                    return '열린 대화가 없습니다.'
                if a:
                    s = a['session']
                    return f"OMP · {s.model}\n{s.rounds}/40회 · 도구 {s.tools}회 · {int(time.monotonic()-a['start'])}초\n{s.last_event} · 대기 {self.queue.qsize()}개"
                if any(s.compacting for s in self.sessions.values()):
                    return 'OMP 대화 압축 중 · /stop으로 이 채널의 압축을 중단할 수 있습니다.'
                return f"OMP 대기 중 · 모델 {self.models.get(key, DEFAULT_MODEL)} · 열린 대화 {len(self.sessions)}개"
            if name in ('approve', 'deny'):
                n = context.resolve_approval('discord:' + key, 'once' if name == 'approve' else 'deny', args.strip() or None)
                return '처리했습니다.' if n else '해당 채널에 유효한 승인 요청이 없습니다.'
            if name == 'stop':
                await self.interrupt_compaction(key)
                async with self.admission_lock:
                    self.epochs[key] = self.epochs.get(key, 0) + 1
                    if self.active and self.active['channel'] == key:
                        await self.stop_active()
                    await asyncio.to_thread(cancel_deferred, key)
                return '이 채널의 작업·대기 요청·후속 타이머를 중단했습니다.'
            if name in ('new', 'reset', 'model'):
                model = MODELS.get(args.strip().lower()) if name == 'model' else None
                if name == 'model' and not model:
                    return '사용법: /model flash 또는 /model 27b'
                await self.interrupt_compaction(None if name == 'reset' else key)
                async with self.admission_lock:
                    if name == 'reset':
                        self.reset_epoch += 1
                    else:
                        self.epochs[key] = self.epochs.get(key, 0) + 1
                    if self.active and (name == 'reset' or self.active['channel'] == key):
                        await self.stop_active()
                    await asyncio.to_thread(cancel_deferred, None if name == 'reset' else key)
                    for k, s in list(self.sessions.items()):
                        if name == 'reset' or k == key:
                            await s.close()
                            self.sessions.pop(k, None)
                    if name == 'model':
                        self.models[key] = model
                        return '이 대화 모델: ' + model
                    if name == 'reset':
                        await asyncio.to_thread(reset_workspace)
                        return '대화·공용 workspace를 초기화했습니다. Git 인증용 홈, 장기기억·알람은 유지합니다.'
                    return '새 대화를 시작합니다. workspace·장기기억·알람은 유지합니다.'
            if name == 'compact':
                if any(s.compacting for s in self.sessions.values()):
                    return '이미 대화를 압축하고 있습니다. /status로 확인할 수 있습니다.'
                if self.active:
                    return '작업이 끝난 뒤 압축할 수 있습니다.'
                async with self.admission_lock:
                    if self.active:
                        return '작업이 끝난 뒤 압축할 수 있습니다.'
                    s = self.sessions.get(key)
                    if not s:
                        return '압축할 대화가 없습니다.'
                    result = await s.compact(args.strip())
                    if result.get('cancelled'):
                        return '대화 압축을 중단했습니다.'
                return '대화를 압축했습니다.'
            if name == 'memory':
                from .tools import memory
                action, _, filename = args.partition(' ')
                result = await asyncio.to_thread(memory, {'action': action or 'list', 'name': filename.strip()})
                return json.dumps(result, ensure_ascii=False)[:1800]
            if name == 'alarms':
                from .tools import schedule
                result = await asyncio.to_thread(schedule, {'action': 'list'})
                return json.dumps(result, ensure_ascii=False)[:1800]
            return '알 수 없는 명령입니다. /help'
        finally:
            context.current.reset(token)

    async def close(self):
        if self.shutting_down:
            return
        self.shutting_down = True
        context.cancel_all()
        if self.active:
            self.active['turn'].cancelled.set()
        for name in ('worker', 'scheduler', 'reaper'):
            task = getattr(self, name, None)
            if task and task is not asyncio.current_task():
                task.cancel()
        # The cron conversation is deliberately absent from self.sessions.
        sessions = list(self.sessions.values())
        if self.active and self.active['session'] not in sessions:
            sessions.append(self.active['session'])
        results = await asyncio.gather(*(s.close() for s in sessions), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                LOG.error('shutdown_cleanup_failed class=%s', type(result).__name__)
        self.sessions.clear()
        await self.registry.close()
        await super().close()


def reset_workspace():
    from .workspace import reset_workspace as coordinated_reset
    return coordinated_reset()


DESCRIPTIONS = {'help': '사용 가능한 명령', 'status': '현재 작업·모델·대기 상태',
    'context': '대화와 도구 실행 상태', 'stop': '현재 작업 중단', 'new': 'workspace를 유지하고 새 대화',
    'reset': '대화와 공용 workspace 초기화 · 기억·알람 유지', 'compact': '현재 대화 압축 · args에 보존할 내용 지정 가능',
    'model': '이 대화 모델 선택 · flash 또는 27b', 'memory': '장기기억 문서 조회·삭제',
    'alarms': '예약 작업 목록', 'approve': '현재 채널의 root 명령 한 번 승인',
    'deny': '현재 채널의 root 명령 거절', 'whoami': '오너 권한 확인'}


async def main():
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s %(message)s')
    logging.getLogger('discord').setLevel(logging.WARNING)
    token = os.environ.get('DISCORD_BOT_TOKEN') or os.environ.get('DISCORD_TOKEN')
    if not token:
        raise SystemExit('Discord token is not configured')
    context.secret_values.append(token)
    for path in ('/etc/evo-omp/gmail.json', '/etc/evo-omp/codex-remote.json'):
        p = Path(path)
        if p.exists():
            data = json.loads(p.read_text())
            context.secret_values += [v for v in data.values() if isinstance(v, str) and len(v) >= 8]
    bot = Bot()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(bot.close()))
    async with bot:
        await bot.start(token)
    if bot.worker_failure:
        raise SystemExit(1)


if __name__ == '__main__':
    asyncio.run(main())
