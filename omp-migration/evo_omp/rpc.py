"""Bounded upstream OMP RPC transport; native coding tools stay inside bwrap."""
import asyncio
import base64
import codecs
import contextlib
import json
import logging
import os
from pathlib import Path
import secrets
import shutil
import signal
import time

from . import context

ROOT = Path('/opt/evo-omp')
WORKSPACE = Path('/var/lib/evo-hermes/workspace')
RUNTIME = Path('/run/evo-omp')
LOG = logging.getLogger('evo-omp.rpc')


def sandbox_argv(profile, model):
    args = ['/usr/bin/bwrap', '--die-with-parent', '--unshare-all', '--share-net',
            '--new-session', '--cap-drop', 'ALL', '--clearenv',
            '--ro-bind', '/usr', '/usr', '--ro-bind', '/lib', '/lib',
            '--ro-bind', '/lib64', '/lib64', '--symlink', 'usr/bin', '/bin',
            '--symlink', 'usr/sbin', '/sbin', '--proc', '/proc', '--dev', '/dev',
            '--ro-bind', str(ROOT / 'bin'), str(ROOT / 'bin'),
            '--ro-bind', str(ROOT / 'provider.ts'), str(ROOT / 'provider.ts'),
            '--ro-bind', str(ROOT / 'sandbox-etc'), '/etc',
            '--ro-bind', '/etc/resolv.conf', '/etc/resolv.conf',
            '--ro-bind', '/etc/hosts', '/etc/hosts',
            '--ro-bind', '/etc/nsswitch.conf', '/etc/nsswitch.conf',
            '--ro-bind', '/etc/ssl/certs', '/etc/ssl/certs',
            '--bind', str(WORKSPACE), '/workspace',
            '--bind', str(WORKSPACE), '/tmp',
            '--bind', str(profile), '/home/agent', '--chdir', '/workspace']
    for path in ('/opt/evo-python', '/opt/evo-hermes/python'):
        if Path(path).exists():
            args += ['--ro-bind', path, path]
    env = {'HOME': '/workspace/.home', 'USER': 'sandbox', 'LANG': 'C.UTF-8',
           'PATH': '/opt/evo-python/venv/bin:/usr/bin:/bin',
           'PI_CODING_AGENT_DIR': '/home/agent/.omp/agent',
           'TMPDIR': '/home/agent/tmp', 'GIT_TERMINAL_PROMPT': '0',
           'OPENBLAS_NUM_THREADS': '2', 'OMP_NUM_THREADS': '2', 'TZ': 'Asia/Seoul',
           'PI_NO_TITLE': '1', 'PI_NO_PTY': '1'}
    for key, val in env.items():
        args += ['--setenv', key, val]
    return args + [str(ROOT / 'bin/omp-v18.11.3'), '--mode', 'rpc', '--no-ui',
                   '--session-dir', '/home/agent/sessions',
                   '--no-extensions', '--no-skills', '--no-rules',
                   '--extension', str(ROOT / 'provider.ts'),
                   '--no-title', '--no-lsp', '--no-pty', '--no-prewalk',
                   '--approval-mode', 'yolo',
                   '--model', 'evo/' + model,
                   '--tools', 'read,grep,glob,edit,write,bash',
                   '--append-system-prompt', '/home/agent/instructions.txt']


class OmpSession:
    def __init__(self, key, model, tool_definitions, handler, config_dir=None):
        self.key, self.model = key, model
        self.definitions, self.handler = tool_definitions, handler
        self.config_dir = Path(config_dir) if config_dir else ROOT
        self.process = None
        self.pending, self.prompts, self.host_jobs = {}, {}, {}
        self.ready = asyncio.Event()
        self.settled = asyncio.Event()
        self.settled.set()
        self.turn = None
        self.last_text, self.error = '', ''
        self.last_progress = ''
        self.pending_text = ''
        self.on_progress = None
        self.transport_error = ''
        self.stderr_tail = ''
        self.closing = False
        self.abort_reason = ''
        self.rounds, self.tools = 0, 0
        self.last_event = 'idle'
        self.compacting = False
        self.compact_cancelled = False
        self.last_used = time.monotonic()
        self.last_activity = self.last_used
        self.profile = RUNTIME / 'profiles' / secrets.token_hex(12)
        self.chunks = None
        self.write_lock = asyncio.Lock()

    @property
    def transport_alive(self):
        reader = getattr(self, 'reader', None)
        return (self.process is not None and self.process.returncode is None
                and reader is not None and not reader.done() and not self.transport_error)

    async def reusable_after_error(self):
        """Inspect quiescence without replaying a prompt or executing a tool."""
        if not self.transport_alive or self.compacting or self.prompts or self.host_jobs:
            return False
        try:
            state = await self.request('get_state', timeout=5)
            if state.get('isSettled') is not True:
                # prompt_result may precede the final session_settled event.
                await asyncio.wait_for(self.settled.wait(), 5)
                state = await self.request('get_state', timeout=5)
            safe = (state.get('isSettled') is True
                    and state.get('isStreaming') is False
                    and state.get('isCompacting') is False
                    and state.get('hasPendingAsyncWork') is False
                    and state.get('queuedMessageCount') == 0)
            return bool(safe and self.transport_alive and not self.pending
                        and not self.prompts and not self.host_jobs)
        except Exception:
            return False

    async def start(self):
        self.profile.mkdir(parents=True, mode=0o700)
        agent = self.profile / '.omp/agent'
        agent.mkdir(parents=True)
        (self.profile / 'tmp').mkdir(mode=0o700)
        (self.profile / 'sessions').mkdir(mode=0o700)
        for name in ('models.yml', 'config.yml'):
            shutil.copyfile(self.config_dir / name, agent / name)
        shutil.copyfile(ROOT / 'instructions.txt', self.profile / 'instructions.txt')
        for name in ('.home',):
            (WORKSPACE / name).mkdir(exist_ok=True, mode=0o700)
        self.process = await asyncio.create_subprocess_exec(
            *sandbox_argv(self.profile, self.model), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=2**20 + 4096, start_new_session=True,
            env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'})
        self.reader = asyncio.create_task(self.read_loop())
        self.stderr_reader = asyncio.create_task(self.drain_stderr())
        ready_wait = asyncio.create_task(self.ready.wait())
        exit_wait = asyncio.create_task(self.process.wait())
        finished, _ = await asyncio.wait([ready_wait, exit_wait], timeout=45,
                                         return_when=asyncio.FIRST_COMPLETED)
        if ready_wait not in finished:
            ready_wait.cancel()
            detail = self.transport_error or context.redact_sensitive_text(self.stderr_tail)[-2000:]
            raise RuntimeError('OMP startup failed: ' + (detail or 'no ready frame'))
        exit_wait.cancel()
        await self.request('negotiate_protocol', protocolVersion=2)
        await self.request('set_event_filter', events=['message_end', 'turn_start',
            'tool_execution_start', 'tool_execution_end', 'agent_start', 'agent_end',
            'auto_compaction_start', 'auto_compaction_end'], messageUpdates='delta')
        await self.request('set_host_tools', tools=self.definitions)
        await self.request('set_auto_retry', enabled=True)
        await self.request('set_cache_warming', mode='off')
        await self.request('set_auto_compaction', enabled=True)
        state = await self.request('get_state')
        if state.get('model', {}).get('api') != 'evo-bounded-completions':
            raise RuntimeError('EVO retry limiter did not activate; refusing an unbounded provider')
        return self

    async def send(self, data):
        raw = (json.dumps(data, ensure_ascii=False, separators=(',', ':')) + '\n').encode()
        if len(raw) > 2**20:
            raise ValueError('OMP request exceeded 1 MiB')
        async with self.write_lock:
            if self.process is None or self.process.returncode is not None:
                raise ConnectionError('OMP process is not running')
            self.process.stdin.write(raw)
            await self.process.stdin.drain()

    async def request(self, kind, *, timeout=45, **args):
        ident = secrets.token_hex(10)
        future = asyncio.get_running_loop().create_future()
        self.pending[ident] = future
        try:
            await self.send(dict(args, type=kind, id=ident))
            answer = await asyncio.wait_for(future, timeout)
            if not answer.get('success'):
                raise RuntimeError(context.redact_sensitive_text(answer.get('error', 'OMP command failed'))[:300])
            return answer.get('data', {})
        finally:
            self.pending.pop(ident, None)

    async def compact(self, focus=''):
        # Model work waits for completion or an explicit interruption.
        self.last_event = 'compacting'
        self.compacting, self.compact_cancelled = True, False
        try:
            options = {'customInstructions': focus} if focus else {}
            result = await self.request('compact', timeout=None, **options)
            return {'cancelled': True} if self.compact_cancelled else result
        except RuntimeError:
            if self.compact_cancelled:
                return {'cancelled': True}
            raise
        finally:
            self.compacting = False
            self.last_used = time.monotonic()
            self.last_event = 'idle'

    async def prompt(self, text, turn, images=None):
        self.turn = turn
        self.last_text, self.error = '', ''
        self.last_progress = ''
        self.pending_text = ''
        self.abort_reason = ''
        self.rounds, self.tools = 0, 0
        self.last_event = 'waiting for model'
        self.last_used = time.monotonic()
        self.last_activity = self.last_used
        self.settled.clear()
        ident = secrets.token_hex(10)
        done = asyncio.get_running_loop().create_future()
        ack = asyncio.get_running_loop().create_future()
        self.prompts[ident], self.pending[ident] = done, ack
        phase = '작업 접수'
        try:
            await self.send(dict(type='prompt', id=ident, message=text, **({'images': images} if images else {})))
            accepted = await asyncio.wait_for(ack, 45)
            if not accepted.get('success'):
                raise RuntimeError(str(accepted.get('error', 'Prompt rejected'))[:300])
            if accepted.get('data', {}).get('agentInvoked') is False:
                return ''
            result = await done
            # Failed turns also settle. Their transcript remains useful if the
            # upstream session confirms there are no outstanding operations.
            phase = '작업 종료 확인'
            await asyncio.wait_for(self.settled.wait(), 20)
            if result.get('status') in ('error', 'failed'):
                raise RuntimeError(context.redact_sensitive_text(result.get('error') or self.error or 'OMP turn failed')[:350])
            if self.error and not self.last_text:
                raise RuntimeError(self.error)
            return self.last_text
        except asyncio.TimeoutError:
            await self.abort(reason='timeout')
            raise TimeoutError(f'OMP {phase} 응답이 없습니다. /status로 상태를 확인하세요.')
        finally:
            self.pending.pop(ident, None)
            self.prompts.pop(ident, None)
            if self.turn is turn:
                self.turn = None
            self.last_used = time.monotonic()

    async def abort(self, reason='interrupted'):
        # An owner interruption supersedes a simultaneous timeout notice.
        if reason != 'timeout' or not self.abort_reason:
            self.abort_reason = reason
        if self.compacting:
            self.compact_cancelled = True
        if self.turn:
            self.turn.cancelled.set()
        if self.process is not None and self.process.returncode is None:
            try:
                await asyncio.wait_for(self.request('abort'), 15)
            except Exception:
                await self.close()

    async def host_call(self, event, turn):
        ident = event['id']
        token = context.current.set(turn)
        try:
            if turn.cancelled.is_set():
                raise InterruptedError('Turn interrupted')
            value = await self.handler(event['toolName'], event.get('arguments', {}), turn)
            if isinstance(value, dict) and isinstance(value.get('content'), list):
                result = value
            else:
                result = {'content': [{'type': 'text', 'text': value if isinstance(value, str)
                                      else json.dumps(value, ensure_ascii=False)}]}
            if not turn.cancelled.is_set():
                await self.send({'type': 'host_tool_result', 'id': ident, 'result': result})
        except asyncio.CancelledError:
            pass
        except Exception as e:
            with contextlib.suppress(Exception):
                await self.send({'type': 'host_tool_result', 'id': ident, 'isError': True,
                    'result': {'content': [{'type': 'text', 'text': context.redact_sensitive_text(str(e))[:400]}]}})
        finally:
            context.current.reset(token)
            self.host_jobs.pop(ident, None)

    def flush_progress(self):
        text, self.pending_text = self.pending_text, ''
        if text and self.on_progress is not None:
            self.on_progress(text)

    async def dispatch(self, event):
        kind = event.get('type')
        self.last_activity = time.monotonic()
        if kind == 'ready':
            self.ready.set()
        elif kind == 'response':
            fut = self.pending.get(event.get('id'))
            if fut is not None and not fut.done():
                fut.set_result(event)
        elif kind == 'prompt_result':
            self.last_event = 'finishing'
            fut = self.prompts.get(event.get('id'))
            if fut is not None and not fut.done():
                fut.set_result(event)
            if event.get('sessionSettled'):
                self.settled.set()
        elif kind == 'session_settled':
            self.last_event = 'idle'
            self.settled.set()
        elif kind == 'message_end':
            msg = event.get('message', {})
            if msg.get('role') == 'assistant':
                blocks = msg.get('content', [])
                text = '\n'.join(b.get('text', '') for b in blocks if b.get('type') == 'text')
                has_tools = any(b.get('type') in ('toolCall', 'tool_call') for b in blocks)
                if text:
                    self.flush_progress()
                    preview = ' '.join(context.redact_sensitive_text(text).split())
                    self.last_progress = preview[:600] + ('…' if len(preview) > 600 else '')
                    self.pending_text = text
                    if has_tools:
                        self.flush_progress()
                    else:
                        self.last_text = text
                if msg.get('errorMessage'):
                    self.error = context.redact_sensitive_text(msg['errorMessage'])[:350]
        elif kind == 'turn_start':
            self.flush_progress()
            self.last_text = ''
            self.last_event = 'waiting for model'
            self.rounds += 1
        elif kind == 'tool_execution_start':
            self.tools += 1
            self.last_event = 'tool: ' + event.get('toolName', '')[:80]
        elif kind in ('tool_execution_end', 'agent_start', 'auto_compaction_end'):
            self.last_event = 'waiting for model'
        elif kind == 'auto_compaction_start':
            self.last_event = 'compacting'
        elif kind == 'message_update' and self.last_event != 'compacting':
            self.last_event = 'receiving model response'
        elif kind == 'host_tool_call':
            if self.turn is None or event.get('toolName') not in {d['name'] for d in self.definitions}:
                await self.send({'type': 'host_tool_result', 'id': event['id'], 'isError': True,
                    'result': {'content': [{'type': 'text', 'text': 'Unknown tool or no active turn'}]}})
            else:
                self.host_jobs[event['id']] = asyncio.create_task(self.host_call(event, self.turn))
        elif kind == 'host_tool_cancel':
            job = self.host_jobs.get(event.get('targetId'))
            if job is not None and not job.done() and self.turn:
                self.turn.cancelled.set()
            if job:
                # Let the owner approval/execution thread observe cancellation and
                # release its broker ticket; do not abandon a live root thread.
                pass
        elif kind == 'extension_ui_request':
            await self.send({'type': 'extension_ui_response', 'id': event['id'], 'cancelled': True})

    async def read_loop(self):
        try:
            while raw := await self.process.stdout.readline():
                event = json.loads(raw)
                if event.get('type') == 'rpc_chunk':
                    ident, index, count = event['chunkId'], event['index'], event['count']
                    if not self.chunks:
                        if index != 0 or not 0 < event['byteLength'] <= 16 * 2**20 or not 0 < count <= 128:
                            raise ValueError('Invalid OMP chunk start')
                        self.chunks = [ident, 0, count, event['byteLength'], bytearray()]
                    c = self.chunks
                    if [ident, index, count, event['byteLength']] != c[:4]:
                        raise ValueError('Invalid OMP chunk sequence')
                    c[4].extend(base64.b64decode(event['data'], validate=True))
                    if len(c[4]) > c[3]:
                        raise ValueError('OMP chunk overflow')
                    c[1] += 1
                    if c[1] != c[2]:
                        continue
                    if len(c[4]) != c[3]:
                        raise ValueError('Incomplete OMP frame')
                    event = json.loads(c[4])
                    self.chunks = None
                elif self.chunks:
                    raise ValueError('Interleaved OMP chunks')
                await self.dispatch(event)
            if self.closing:
                return
            code = self.process.returncode
            if code is None:
                with contextlib.suppress(asyncio.TimeoutError):
                    code = await asyncio.wait_for(self.process.wait(), 3)
            stderr_reader = getattr(self, 'stderr_reader', None)
            if stderr_reader is not None:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(asyncio.shield(stderr_reader), 1)
            if code is None:
                detail = 'OMP RPC stdout closed while process is still running'
            elif code < 0:
                detail = f'OMP process exited (signal {-code})'
            else:
                detail = f'OMP process exited (code {code})'
            raise ConnectionError(detail)
        except Exception as e:
            if self.closing:
                return
            detail = context.redact_sensitive_text(str(e))[:350]
            stderr = ' '.join(context.redact_sensitive_text(self.stderr_tail).split())[-2000:]
            resources = {}
            cgroup = Path('/sys/fs/cgroup/system.slice/evo-omp.service')
            for name in ('pids.current', 'pids.peak', 'pids.max', 'pids.events',
                         'memory.current', 'memory.peak', 'memory.events'):
                with contextlib.suppress(OSError):
                    resources[name] = (cgroup / name).read_text().strip()[:500]
            LOG.error('omp_transport_failed pid=%s code=%s detail=%s stderr=%s resources=%s',
                      self.process.pid, self.process.returncode, detail,
                      json.dumps(stderr, ensure_ascii=False), json.dumps(resources))
            self.error = detail + (f' · {stderr[-200:]}' if stderr else '')
            self.transport_error = self.error
            for f in [*self.pending.values(), *self.prompts.values()]:
                if not f.done():
                    f.set_exception(ConnectionError(self.error))
            self.settled.set()

    async def drain_stderr(self):
        # Drain arbitrary-length lines. Redact the bounded RAM tail before output.
        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        while raw := await self.process.stderr.read(4096):
            self.stderr_tail = (self.stderr_tail + decoder.decode(raw))[-8192:]
        self.stderr_tail = (self.stderr_tail + decoder.decode(b'', final=True))[-8192:]

    async def close(self):
        self.closing = True
        self.transport_error = self.transport_error or 'OMP session closed'
        if self.turn:
            self.turn.cancelled.set()
        if self.process is not None and self.process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.process.pid, signal.SIGKILL)
                await self.process.wait()
        jobs = list(self.host_jobs.values())
        if jobs:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.gather(*jobs, return_exceptions=True), 20)
        if self.profile.is_relative_to(RUNTIME / 'profiles'):
            shutil.rmtree(self.profile, ignore_errors=True)
        readers = []
        for task in (getattr(self, 'reader', None), getattr(self, 'stderr_reader', None)):
            if task and task is not asyncio.current_task():
                task.cancel()
                readers.append(task)
        await asyncio.gather(*readers, return_exceptions=True)
        for future in [*self.pending.values(), *self.prompts.values()]:
            if not future.done():
                future.set_exception(ConnectionError('OMP session closed'))
        self.pending.clear()
        self.prompts.clear()
        self.host_jobs.clear()
        self.chunks = None
