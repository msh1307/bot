"""Actual Pi/bwrap/Python/gateway A-wait/B/A test; localhost scripted provider only.

Usage: python verify_task_resume_live.py CANDIDATE_ROOT REPORT.json
All jobs, profiles and workspace files are disposable; Discord is never connected.
"""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
import hashlib
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from evo_bot import context, gateway, rpc, tools
import verify_context_payload as fixture


@asynccontextmanager
async def quiet(*args, **kwargs):
    yield


class CaptureBot(gateway.Bot):
    def __init__(self):
        super().__init__()
        self.posts = []
        self.channel = SimpleNamespace(id=77, typing=quiet)

    def get_channel(self, ident):
        assert ident == 77
        return self.channel

    def write_status(self, state):
        pass  # Never touch the live /run/evo-bot/status.json.

    async def wait_until_ready(self):
        pass

    async def deliver(self, channel, text, footer=''):
        self.posts.append(text)
        return True

    progress = quiet


async def main():
    stage, output = map(Path, sys.argv[1:3])
    stage = stage.resolve()
    server = ThreadingHTTPServer(('127.0.0.1', 0), fixture.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    checks, sessions, compactions = [], [], []
    def check(ok, label):
        assert ok, label
        checks.append(label)
    try:
        with tempfile.TemporaryDirectory(prefix='evo-task-resume-') as folder:
            root = Path(folder)
            for name in ('runtime', 'workspace', 'state', 'config'):
                (root / name).mkdir()
            cfg = root / 'config'
            models = (stage / 'models.yml').read_text()
            assert ':8080/v1' in models
            (cfg / 'models.yml').write_text(models.replace(':8080/v1', f':{server.server_port}/v1'))
            shutil.copyfile(stage / 'config.yml', cfg / 'config.yml')
            original_session, original_compact = rpc.PiSession, rpc.PiSession.compact
            original_sandbox = rpc.sandbox_argv
            def sandbox(*args, **kwargs):
                argv = original_sandbox(*args, **kwargs)
                for index in range(len(argv) - 2):
                    if argv[index] == '--ro-bind' and argv[index + 1] in {
                            str(rpc.ROOT / name) for name in ('pi-runner.mjs', 'pi-tools.mjs', 'summary-guard.mjs')}:
                        argv[index + 1] = str(stage / Path(argv[index + 1]).name)
                return argv
            def factory(key, model, definitions, host):
                session = original_session(key, model, definitions, host, cfg)
                sessions.append(session)
                return session
            async def compact(session, *args, **kwargs):
                compactions.append(session.key)
                return await original_compact(session, *args, **kwargs)
            with patch.object(rpc, 'sandbox_argv', sandbox), \
                    patch.object(rpc, 'RUNTIME', root / 'runtime'), \
                    patch.object(rpc, 'WORKSPACE', root / 'workspace'), patch.object(tools, 'STATE', root / 'state'), \
                    patch.object(gateway, 'IGNORE_PATH', root / 'state/ignore.json'), \
                    patch.object(gateway, 'PiSession', factory), patch.object(rpc.PiSession, 'compact', compact):
                bot = CaptureBot()
                definition = dict(tools.SCHEDULE_SCHEMA, name='schedule', label='schedule', loadMode='essential')
                bot.registry.tools = {'schedule': (definition, tools.schedule)}
                bot.registry.definitions = lambda cron=False: [definition]
                bot.registry.close = AsyncMock()
                fixture.REQUESTS.clear(); fixture.STEPS.clear()
                fixture.STEPS.extend([
                    ('schedule', {'action': 'note', 'state': 'waiting', 'after_seconds': 600, 'text': 'OLD-CHECKPOINT: worker running'}),
                    None,
                    ('schedule', {'action': 'note', 'state': 'done', 'text': 'Corrected A result verified'}),
                    None])
                owner = context.Turn('77', model=gateway.DEFAULT_MODEL)
                token = context.current.set(owner)
                worker = asyncio.create_task(bot.work_loop())
                try:
                    ident = tools.schedule({'action': 'enqueue', 'prompt': 'A-INITIAL-GOAL: coordinate worker'})['id']
                    claim = bot.claim_due()[0]
                    bot.enqueue('77', claim['prompt'], [], claim)
                    await asyncio.wait_for(bot.queue.join(), 60)
                    session = bot.sessions['77']
                    saved = tools.load_jobs()[0]
                    check(session.disposition == 'waiting' and bot.active is None, 'A yields through real Pi/RPC/gateway to idle')
                    check(saved['task_state'] == 'waiting' and not saved['running'] and saved['task_reported'], 'Checkpoint persisted before waiting release')
                    check(saved['note'].startswith('OLD-CHECKPOINT') and saved['resume_at'], 'Progress and timed fallback retained')
                    check(len(fixture.REQUESTS) == 1 and not bot.posts, 'Waiting causes no extra model request or fake final answer')
                    message = SimpleNamespace(author=SimpleNamespace(bot=False, id=int(context.OWNER)),
                        content='B-QUESTION: unrelated owner request', channel=bot.channel, attachments=[], reference=None)
                    await bot.on_message(message)
                    await asyncio.wait_for(bot.queue.join(), 45)
                    check(bot.sessions['77'] is session and len(fixture.REQUESTS) == 2, 'Owner B runs in the same real conversation')
                    check(len(bot.posts) == 1 and 'VERIFIED_CONTEXT' in bot.posts[0], 'B answer captured without Discord delivery')
                    tools.schedule({'action': 'update', 'id': ident, 'prompt': 'A-CORRECTED-GOAL: report only; do not execute'})
                    jobs = tools.load_jobs()
                    jobs[0]['resume_at'] = jobs[0]['next_run_at'] = datetime.now(tools.TZ).isoformat()
                    tools.save_jobs(jobs)  # Simulate the original timer becoming due, without sleeping.
                    check(bot.enqueue_codex_callback('77', 'Worker A completed', bot.codex_callback_scope('77'),
                          lambda: True, owner_task_id=ident), 'Completion callback accepted while timer is due')
                    due = bot.claim_due()
                    check(len(due) == 1 and not bot.claim_due(), 'Timer and completion event produce exactly one claim')
                    bot.enqueue('77', due[0]['prompt'], [], due[0])
                    await asyncio.wait_for(bot.queue.join(), 45)
                    check(tools.load_jobs()[0]['task_state'] == 'done' and len(fixture.REQUESTS) == 4, 'A resumes once and reaches verified completion')
                    latest = [m for m in fixture.REQUESTS[2]['messages'] if m['role'] == 'user'][-1]['content']
                    check('A-CORRECTED-GOAL' in str(latest) and 'OLD-CHECKPOINT' not in str(latest)
                          and 'A-INITIAL-GOAL' not in str(latest), 'Latest task anchor contains corrected scope without stale checkpoint')
                    history = json.dumps((await session.request('get_messages'))['messages'])
                    check('A-INITIAL-GOAL' in history and 'B-QUESTION' in history and 'A-CORRECTED-GOAL' in history,
                          'Shared history retains A and B while current task uses its latest goal')
                    check(len(compactions) == 1, 'Only initial-task compaction attempted; waiting resume forces none')
                    bot.enqueue_codex_callback('77', 'Late duplicate completion', bot.codex_callback_scope('77'), lambda: True, owner_task_id=ident)
                    check(bot.queue.empty() and not bot.claim_due() and len(fixture.REQUESTS) == 4, 'Completed task never revives on a late callback')
                    check(len(bot.posts) == 2 and bot.active is None, 'Only B and final A answers reported; gateway idle')
                    report = {'passed': len(checks), 'failed': 0, 'checks': checks,
                              'actual_pi_bwrap_gateway': True, 'scripted_provider_requests': len(fixture.REQUESTS),
                              'real_model_requests': 0, 'discord_posts': 0, 'target_commands': 0,
                              'runner_sha256': hashlib.sha256((stage / 'pi-runner.mjs').read_bytes()).hexdigest()}
                    output.write_text(json.dumps(report, indent=2)); print(json.dumps(report), flush=True)
                finally:
                    worker.cancel(); await asyncio.gather(worker, return_exceptions=True)
                    await bot.close()
                    context.current.reset(token)
    finally:
        output.with_suffix('.diagnostic.json').write_text(json.dumps({'payloads': fixture.REQUESTS}, indent=2))
        server.shutdown(); server.server_close()


if __name__ == '__main__':
    asyncio.run(main())
