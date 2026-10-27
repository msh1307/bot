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
from .discord_text import messages as discord_messages, units as discord_units
from .rpc import OmpSession, WORKSPACE
from .tools import Registry, STATE, TZ, JOB_LOCK, load_jobs, save_jobs, next_run, image_content, cancel_deferred
from .user_input import PendingInput, QuestionView

LOG = logging.getLogger('evo-omp')
DEFAULT_MODEL = 'sc117-flash-next-iq3_xxs'
MODELS = {'flash': DEFAULT_MODEL, '27b': 'qwen3.8-27b-uncensored'}
STATUS_INTERVAL = 30
PROGRESS_INTERVAL = 60
IGNORE_PATH = STATE / 'ignored_channels.json'


def load_ignored_channels():
    if not IGNORE_PATH.exists():
        return set()
    channels = json.loads(IGNORE_PATH.read_text())
    if not isinstance(channels, list) or any(not isinstance(value, str) or not value.isdigit()
                                             for value in channels):
        raise ValueError('Invalid ignored channels file')
    return set(channels)


def save_ignored_channels(channels):
    temporary = IGNORE_PATH.with_suffix('.tmp')
    temporary.write_text(json.dumps(sorted(channels)) + '\n')
    temporary.chmod(0o600)
    temporary.replace(IGNORE_PATH)


async def context_footer(session):
    try:
        state = await session.request('get_state', timeout=5)
        percent = (state.get('contextUsage') or {}).get('percent')
        if isinstance(percent, (int, float)) and 0 <= percent <= 100:
            return f'[context {percent:.0f}%]'
    except Exception as error:
        LOG.warning('context_usage_unavailable class=%s', type(error).__name__)
    return ''


def progress_text(session, seconds, include_text=True):
    seconds = max(0, int(seconds))
    elapsed = f'{seconds // 60}분 {seconds % 60}초' if seconds >= 60 else f'{seconds}초'
    phase = {'waiting for model': '모델 응답 대기',
             'receiving model response': '모델 응답 중',
             'reasoning': '추론 중', 'writing answer': '답변 작성 중',
             'preparing tool': '도구 요청 준비 중', 'compacting': '대화 압축 중',
             'finishing': '마무리 중', 'idle': '마무리 중'}.get(session.last_event)
    if phase is None:
        phase = '도구 실행 중: ' + session.last_event.removeprefix('tool: ')
    idle = int(time.monotonic() - session.last_activity)
    if idle >= STATUS_INTERVAL:
        phase += f' · 마지막 이벤트 {idle}초 전'
    text = f'⏳ 작업 중 · {elapsed} · {session.rounds}회 · 도구 {session.tools}회\n{phase}'
    if include_text and session.last_progress:
        text += '\n최근 중간 답변: ' + discord.utils.escape_markdown(session.last_progress)
    return context.redact_sensitive_text(text)


class Bot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.typing = True
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
        self.questions = {}
        self.ignored_channels = load_ignored_channels()

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
        from .management import KEY_PATH, ManagementServer
        self.management = ManagementServer() if KEY_PATH.is_file() else None
        if self.management is not None: await self.management.start()
        for name in ('help', 'status', 'context', 'stop', 'new', 'reset', 'compact',
                     'model', 'memory', 'alarms', 'approve', 'deny', 'whoami', 'ignore'):
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
        await self.restore_recovery()
        self.worker = asyncio.create_task(self.work_loop())
        self.worker.add_done_callback(self.worker_finished)
        # An interrupted service must not leave a schedule permanently 'running'.
        await asyncio.to_thread(self.recover_schedules)
        self.scheduler = asyncio.create_task(self.schedule_loop())

    async def restore_recovery(self):
        # An explicit maintenance handoff on tmpfs; normal restarts stay fresh.
        manifest = Path('/run/evo-omp/recovery.json')
        if not manifest.exists():
            return
        item = json.loads(manifest.read_text())
        channel, model = str(item['channel']), item['model']
        source = Path('/run/evo-omp/recovery')
        if not channel.isdigit() or model not in MODELS.values():
            raise ValueError('Invalid maintenance recovery manifest')
        session = OmpSession(channel, model, self.registry.definitions(False), self.registry.call)
        try:
            await session.start(resume_dir=source)
        except BaseException:
            await session.close()
            raise
        self.sessions[channel], self.models[channel] = session, model
        usage = (await session.request('get_state')).get('contextUsage')
        manifest.unlink()
        shutil.rmtree(source)
        LOG.info('maintenance_session_restored channel=%s model=%s context=%s', channel, model, usage)

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
        self.write_status('working' if self.active else 'connected')
        LOG.info('discord_connected bot_id=%s engine=omp-18.11.3', self.user.id)

    async def on_resumed(self):
        self.write_status('working' if self.active else 'connected')
        LOG.info('discord_resumed')

    async def on_disconnect(self):
        # Withdraw pending approvals, but let accepted work survive a transient
        # gateway reconnect. Discord.py resumes the connection automatically.
        context.cancel_all()
        self.write_status('disconnected')

    async def on_raw_typing(self, payload):
        # Raw events also work when the channel/member is absent from Discord's cache.
        if str(payload.user_id) != context.OWNER:
            return
        if str(payload.channel_id) in self.ignored_channels:
            return
        question = self.questions.get(str(payload.channel_id))
        if question and question['input'].accepting and not question['turn'].cancelled.is_set():
            question['input'].typing()

    async def on_message(self, message):
        if message.author.bot or str(message.author.id) != context.OWNER:
            return
        text = message.content.strip()
        ignored = str(message.channel.id) in self.ignored_channels
        if text.startswith(('/', '!')):
            name, _, args = text[1:].partition(' ')
            if (not ignored or name == 'ignore') and (name in DESCRIPTIONS or name in ('temp', 'access', 'compress')):
                try:
                    result = await self.command(message.channel, name, args)
                    if result:
                        await self.deliver(message.channel, result)
                except Exception as e:
                    await self.deliver(message.channel, '실패: ' + context.redact_sensitive_text(str(e))[:500])
                return
        if ignored:
            return
        if not text and not message.attachments:
            return
        channel = str(message.channel.id)
        question = self.questions.get(channel)
        if question and (not question['input'].accepting or question['turn'].cancelled.is_set()):
            question = None
        ticket = question['input'].begin_message() if question else None
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
            if question:
                if self.questions.get(channel) is question and not question['turn'].cancelled.is_set():
                    question['input'].finish_message(ticket, text, images)
                    ticket = None
                return
            await self.interrupt_compaction(channel)
            interrupted = False
            async with self.admission_lock:
                if self.queue.full():
                    raise ValueError('요청 대기열이 찼습니다. 잠시 뒤 다시 보내주세요.')
                if self.active and (self.active['channel'] == channel
                                    or self.active['turn'].cron or self.active['turn'].deferred):
                    await self.stop_active()
                    interrupted = True
                self.enqueue(channel, text or '첨부 이미지를 확인해줘.', images)
            if interrupted:
                with contextlib.suppress(discord.HTTPException, asyncio.TimeoutError):
                    await asyncio.wait_for(message.add_reaction('↪️'), 3)
        except Exception as e:
            await self.deliver(message.channel, context.redact_sensitive_text(str(e))[:500])
        finally:
            if ticket is not None:
                question['input'].discard_message(ticket)

    async def user_input(self, args, turn):
        question = args.get('question')
        if not isinstance(question, str) or not 1 <= len(question.strip()) <= 1000:
            raise ValueError('question must contain 1..1000 characters')
        if turn.channel in self.questions:
            raise ValueError('A question is already pending in this channel')
        if not self.active or self.active['turn'] is not turn or turn.cancelled.is_set():
            raise InterruptedError('No active owner task')
        item = {'turn': turn, 'input': PendingInput()}
        self.questions[turn.channel] = item
        view = QuestionView(item['input'], lambda: self.questions.get(turn.channel) is item
                            and not turn.cancelled.is_set())
        message, label = None, '질문 취소됨'
        self.write_status('working')
        try:
            channel = self.get_channel(int(turn.channel)) or await self.fetch_channel(int(turn.channel))
            preview = context.redact_sensitive_text(question.strip())
            message = await asyncio.wait_for(channel.send(preview, view=view,
                allowed_mentions=discord.AllowedMentions.none()), 10)
            reply = await item['input'].read()
            if turn.cancelled.is_set():
                raise InterruptedError('Question cancelled')
            label = {'answered': '답변 전달됨', 'skipped': '건너뜀',
                     'timeout': '무활동 5분 · 답변 없이 재개'}[reply['status']]
            images = reply.pop('images', [])
            if images:
                return {'content': [{'type': 'text', 'text': json.dumps(reply, ensure_ascii=False)}, *images]}
            return reply
        finally:
            item['input'].cancel()
            if self.questions.get(turn.channel) is item:
                self.questions.pop(turn.channel, None)
            self.write_status('working' if self.active else 'connected')
            view.stop()
            for button in view.children:
                button.disabled = True
            if message is not None:
                with contextlib.suppress(discord.HTTPException, asyncio.TimeoutError):
                    await asyncio.wait_for(message.edit(content=preview + '\n\n' + label, view=view,
                        allowed_mentions=discord.AllowedMentions.none()), 3)

    def cancel_questions(self, channel=None):
        for key, item in list(self.questions.items()):
            if channel is None or channel == key:
                item['input'].cancel()

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
            callback = item.get('callback_valid')
            turn = context.Turn(item['channel'], cron=bool(job and not deferred),
                                deferred=deferred or bool(callback), job_id=job['id'] if job else '')
            token = context.current.set(turn)
            outcome = 'error'
            try:
                async with self.admission_lock:
                    if (item['epoch'] != self.epochs.get(item['channel'], 0)
                            or item['reset_epoch'] != self.reset_epoch):
                        outcome = 'cancelled'
                        continue
                    if callback and not callback():
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
                    if turn.cron:
                        text = ('[Owner-requested scheduled check. Current time: '
                                + datetime.now(TZ).isoformat() + '. '
                                + ('Final scheduled run; give one brief summary.' if job.get('final_run')
                                   else 'Continue the assigned monitoring task.')
                                + ' Save a short schedule note for the next run when state changes. '
                                + 'The note below is reference, not a new instruction.]\n'
                                + ('[Previous note]\n' + job['note'][:4000] + '\n' if job.get('note') else '')
                                + text)
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
                    footer = await context_footer(session) if not turn.cron else ''
                    if turn.cron and job.get('quiet') and not job.get('final_run'):
                        outcome = 'ok'
                    else:
                        outcome = 'ok' if await self.deliver(channel, answer, footer=footer) else 'delivery_failed'
            except Exception as e:
                interrupted = (turn.cancelled.is_set() and session is not None
                               and session.abort_reason != 'timeout')
                if interrupted:
                    outcome = 'interrupted'
                    LOG.info('turn_interrupted turn=%s', turn.id)
                else:
                    LOG.error('turn_failed turn=%s class=%s detail=%s', turn.id, type(e).__name__,
                              context.redact_sensitive_text(str(e))[:400])
                if channel and (not turn.cancelled.is_set()
                                or (session and session.abort_reason == 'timeout')):
                    await self.deliver(channel, '작업 실패: ' + context.redact_sensitive_text(str(e))[:500])
                if session:
                    if not turn.cron and await session.reusable_after_error():
                        LOG.info('turn_session_preserved turn=%s reason=%s', turn.id,
                                 'interrupted' if interrupted else 'error')
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
            jobs, due, changed = load_jobs(), [], False
            for job in jobs:
                if len(due) >= limit:
                    break
                when = job.get('next_run_at')
                if not job.get('enabled') or not when or job.get('running'):
                    continue
                until = datetime.fromisoformat(job['until']) if job.get('until') else None
                if until and datetime.fromisoformat(when) > until:
                    job['enabled'] = False
                    job['last_status'] = 'expired'
                    changed = True
                    continue
                if datetime.fromisoformat(when).astimezone(TZ) <= now:
                    job['running'] = True
                    job['last_run_at'] = now.isoformat()
                    job['due_at'] = when
                    if job['schedule']['kind'] == 'at':
                        job['enabled'] = False
                    else:
                        job['next_run_at'] = next_run(job).isoformat()
                        if until and datetime.fromisoformat(job['next_run_at']) > until:
                            job['enabled'] = False
                    due.append(dict(job, final_run=not job['enabled']))
                    changed = True
            if changed:
                save_jobs(jobs)
            return due

    @contextlib.asynccontextmanager
    async def progress(self, channel, session, enabled=True):
        """Forward completed public text and keep one quiet status while waiting."""
        if not enabled:
            yield
            return
        notice, started = None, time.monotonic()
        last_update, last_phase = started, None
        updates = asyncio.Queue(maxsize=16)
        delivered = False

        def enqueue(text):
            try:
                updates.put_nowait(text)
            except asyncio.QueueFull:
                LOG.warning('progress_delivery_backlog; intermediate message not delivered')

        session.on_progress = enqueue

        async def forward():
            nonlocal last_update, delivered
            while True:
                text = await updates.get()
                try:
                    if await asyncio.wait_for(self.deliver(channel, text), 15):
                        delivered = True
                        last_update = time.monotonic()
                except Exception as error:
                    LOG.warning('progress_delivery_failed class=%s', type(error).__name__)
                finally:
                    updates.task_done()

        async def update():
            nonlocal notice, last_update, last_phase
            while True:
                await asyncio.sleep(STATUS_INTERVAL)
                self.write_status('working')
                now = time.monotonic()
                if str(channel.id) in self.questions:
                    # The question itself is visible; avoid a second working notice.
                    last_update = now
                    if notice is not None:
                        with contextlib.suppress(discord.HTTPException, asyncio.TimeoutError):
                            await asyncio.wait_for(notice.delete(), 3)
                        notice = None
                    continue
                compacting = session.last_event == 'compacting'
                interval = STATUS_INTERVAL if compacting else PROGRESS_INTERVAL
                if now - last_update < interval and not (compacting and last_phase != 'compacting'):
                    continue
                text = progress_text(session, now - started, include_text=False)
                try:
                    if notice is None:
                        notice = await asyncio.wait_for(channel.send(text,
                            allowed_mentions=discord.AllowedMentions.none()), 5)
                    else:
                        await asyncio.wait_for(notice.edit(content=text,
                            allowed_mentions=discord.AllowedMentions.none()), 5)
                    last_update = now
                    last_phase = session.last_event
                except (discord.HTTPException, asyncio.TimeoutError) as error:
                    LOG.warning('progress_update_failed class=%s', type(error).__name__)
                    return

        task = asyncio.create_task(update())
        sender = asyncio.create_task(forward())
        finished = False
        try:
            yield
            finished = True
        finally:
            if not finished or context.is_interrupted():
                session.flush_progress()
            session.on_progress = None
            session.pending_text = ''
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            try:
                await asyncio.wait_for(updates.join(), 3 if context.is_interrupted() else 15)
            except asyncio.TimeoutError:
                LOG.warning('progress_delivery_incomplete pending=%s', updates.qsize())
            finally:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
            if notice is not None:
                with contextlib.suppress(discord.HTTPException, asyncio.TimeoutError):
                    if session.last_progress and not delivered and (not finished or context.is_interrupted()):
                        text = '⏹ 작업 중단 · 마지막 중간 답변\n' + discord.utils.escape_markdown(session.last_progress)
                        await asyncio.wait_for(notice.edit(content=text,
                            allowed_mentions=discord.AllowedMentions.none()), 3)
                    else:
                        await asyncio.wait_for(notice.delete(), 3)

    async def deliver(self, channel, text, footer=''):
        text = context.redact_sensitive_text(text)
        parts = discord_messages(text)
        if footer:
            if parts and discord_units(parts[-1] + '\n\n' + footer) <= 1900:
                parts[-1] += '\n\n' + footer
            else:
                parts.append(footer)
        for part in parts:
            try:
                await channel.send(part, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException as e:
                LOG.error('discord_delivery_failed class=%s status=%s', type(e).__name__, e.status)
                return False
        return True

    def codex_callback_scope(self, channel):
        session = self.sessions.get(channel)
        return (self.reset_epoch, self.epochs.get(channel, 0), id(session)) if session else None

    def enqueue_codex_callback(self, channel, text, scope, valid):
        # Queue behind existing work; never interrupt the owner or revive a reset
        # conversation. Only identifiers are queued, not another chat's history.
        def current():
            return self.codex_callback_scope(channel) == scope and valid()
        if scope is None or self.shutting_down or self.queue.full() or not current():
            return False
        self.enqueue(channel, text, [], callback_valid=current)
        return True

    def enqueue(self, channel, text, images, job=None, *, callback_valid=None):
        self.queue.put_nowait({'channel': channel, 'text': text, 'images': images, 'job': job,
            'epoch': self.epochs.get(channel, 0), 'reset_epoch': self.reset_epoch,
            'callback_valid': callback_valid})

    async def stop_active(self):
        active = self.active
        if not active:
            return
        self.cancel_questions(active['channel'])
        active['turn'].cancelled.set()
        await active['session'].abort()
        try:
            await asyncio.wait_for(self.active_done.wait(), 25)
        except asyncio.TimeoutError:
            await active['session'].close()
            await asyncio.wait_for(self.active_done.wait(), 10)

    async def interrupt_compaction(self, key=None):
        # Abort before waiting for admission: manual compaction holds that lock.
        # Active turns are aborted once by stop_active after admission.
        active_session = self.active['session'] if self.active else None
        for channel, session in list(self.sessions.items()):
            if (session is not active_session and session.compacting
                    and (key is None or channel == key)):
                await session.abort()

    def write_status(self, state):
        if state == 'working' and not self.is_ready():
            state = 'disconnected'
        a = self.active
        data = {'engine': 'omp', 'version': '18.11.3', 'state': state,
                'queued': self.queue.qsize(), 'sessions': len(self.sessions),
                'pending_inputs': len(self.questions),
                'active': bool(a), 'rounds': a['session'].rounds if a else 0,
                'tools': a['session'].tools if a else 0,
                'elapsed_seconds': int(time.monotonic() - a['start']) if a else 0,
                'idle_seconds': int(time.monotonic() - a['session'].last_activity) if a else 0,
                'updated': int(time.time())}
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
                return '/status · /temp · /model flash 또는 27b · /stop · /new · /reset · /compact · /memory · /alarms · /access · /ignore\n새 메시지를 보내면 같은 채널의 진행 중 작업을 중단하고 이어서 처리합니다.'
            if name == 'ignore':
                mode = args.strip().lower()
                if mode not in ('', 'on', 'off', 'status'):
                    return '사용법: /ignore [on|off|status]'
                enabled = key in self.ignored_channels
                if mode == 'status':
                    return '이 채널 무시 중' if enabled else '이 채널 응답 중'
                enable = not enabled if not mode else mode == 'on'
                if enable:
                    self.ignored_channels.add(key)
                else:
                    self.ignored_channels.discard(key)
                try:
                    save_ignored_channels(self.ignored_channels)
                except OSError:
                    if enable:
                        self.ignored_channels.discard(key)
                    else:
                        self.ignored_channels.add(key)
                    raise
                return ('이 채널의 일반 메시지를 무시합니다. /ignore로 해제할 수 있습니다.'
                        if enable else '이 채널의 일반 메시지에 다시 응답합니다.')
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
                    if a['channel'] in self.questions:
                        return '답변 대기 중 · 무활동 5분 후 재개 · 타이핑하면 대기 연장'
                    s = a['session']
                    return f"OMP · {s.model}\n" + progress_text(s, time.monotonic() - a['start']) + f"\n대기 {self.queue.qsize()}개"
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
                    channel = self.get_channel(int(key)) or await self.fetch_channel(int(key))
                    async with self.progress(channel, s):
                        result = await s.compact(args.strip())
                    if result.get('cancelled'):
                        return '대화 압축을 중단했습니다.'
                footer = await context_footer(s)
                return '대화를 압축했습니다.' + ('\n' + footer if footer else '')
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
        if getattr(self, 'management', None) is not None:
            await self.management.close()
        context.cancel_all()
        self.cancel_questions()
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
    'deny': '현재 채널의 root 명령 거절', 'whoami': '오너 권한 확인',
    'ignore': '이 채널 일반 메시지 무시 또는 해제'}


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
            if p.name == 'codex-remote.json':
                for entry in data.get('servers', {'default': data}).values():
                    context.secret_values.append(entry['token'])
            else:
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
