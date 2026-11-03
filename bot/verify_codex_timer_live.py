"""Real one-second timer -> real gateway worker -> public Codex tools, no LLM/Discord posts."""
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
from unittest.mock import AsyncMock, patch
from evo_bot import context, gateway, tools
from evo_bot.integrations import codex


async def main():
    report = {'discord_posts': 0, 'model_inference': False, 'callbacks': []}
    with tempfile.TemporaryDirectory(prefix='codex-timer-live-') as temporary:
        root = Path(temporary)
        with patch.object(tools, 'STATE', root), patch.object(codex, 'STATE', root / 'jobs.json'), \
                patch.object(gateway, 'IGNORE_PATH', root / 'ignore.json'), \
                patch.object(gateway, 'context_footer', AsyncMock(return_value='')):
            client = SimpleNamespace(is_ready=lambda: True)
            context.client = client
            codex.discord_factory(client, None)
            bot = gateway.Bot(); bot.write_status = lambda _: None
            bot.deliver = AsyncMock(return_value=True)
            bot.wait_until_ready = AsyncMock()
            registry = tools.Registry()
            await registry.initialize_web()
            @asynccontextmanager
            async def quiet(*args, **kwargs): yield
            async def prompt(text, turn, images):
                ident = text.rsplit('\n', 1)[-1]
                assert ident in ('oracle', 'desktop') and turn.deferred and not turn.cron
                result = await registry.call('remote_control',
                    {'instance': ident, 'protocol': 'codex', 'action': 'models'}, turn)
                result = json.loads(result) if isinstance(result, str) else result
                assert result.get('success') and result.get('models'), result
                report['callbacks'].append({'instance': ident, 'model_count': len(result['models']),
                    'deferred': turn.deferred, 'cron': turn.cron, 'tool_success': True})
                return 'Timer Codex check verified'
            session = SimpleNamespace(model=gateway.DEFAULT_MODEL, error=None, prompt=prompt,
                close=AsyncMock(), reusable_after_error=AsyncMock(return_value=True))
            bot.sessions['77'] = session
            bot.session = AsyncMock(return_value=session)
            bot.get_channel = lambda ident: SimpleNamespace(typing=quiet)
            bot.progress = quiet
            turn = context.Turn('77', model=gateway.DEFAULT_MODEL)
            token = context.current.set(turn)
            worker = None
            try:
                for seconds in (300, 600):
                    saved = await registry.call('schedule', {'action': 'defer', 'after_seconds': seconds,
                        'prompt': 'Boundary verification only'}, turn)
                    saved = json.loads(saved) if isinstance(saved, str) else saved
                    assert saved['success'] and saved['continuation'], saved
                    await registry.call('schedule', {'action': 'delete', 'id': saved['id']}, turn)
                for ident in ('oracle', 'desktop'):
                    await registry.call('schedule', {'action': 'defer', 'after_seconds': 1, 'prompt': ident}, turn)
                assert bot.claim_due() == []
                await asyncio.sleep(1.1)
                claims = bot.claim_due()
                assert len(claims) == 2
                for job in claims: bot.enqueue('77', job['prompt'], [], job)
                worker = asyncio.create_task(bot.work_loop())
                await asyncio.wait_for(bot.queue.join(), 120)
                assert len(report['callbacks']) == 2, report
                assert all(j['last_status'] == 'ok' for j in tools.load_jobs()), tools.load_jobs()
                report.update(passed=True, five_and_ten_minute_registration=True,
                    live_timer_delay_seconds=1, real_worker=True, real_remote_tools=True,
                    scheduled_jobs=len(claims), deliveries=bot.deliver.await_count)
            finally:
                if worker:
                    worker.cancel(); await asyncio.gather(worker, return_exceptions=True)
                bot.sessions.clear(); await bot.close(); await registry.close()
                context.current.reset(token)
                Path('/tmp/evo-codex-uniform-tests/timer-live.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report), flush=True)


if __name__ == '__main__': asyncio.run(main())
