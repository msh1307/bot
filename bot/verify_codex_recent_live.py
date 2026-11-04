"""Disposable chats on both live daemons, through the bot's tool delivery path.

No Discord messages, owner chat edits or daemon restarts. Run as evo-bot.
"""
import asyncio
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from evo_bot import context
from evo_bot.integrations import codex
from evo_bot.integrations.codex_recent import Recent
from evo_bot.rpc import PiSession
from evo_bot.tools import Registry


async def main():
    report = {'instances': {}, 'discord_posts': 0, 'daemon_restarts': 0}
    with tempfile.TemporaryDirectory(prefix='codex-recent-live-') as tmp:
        codex.STATE = Path(tmp) / 'jobs.json'
        codex.RECENT = store = Recent(Path(tmp) / 'recent.sqlite3')
        context.current.set(context.Turn('77'))
        codex.CLIENT = context.client = SimpleNamespace(is_ready=lambda: True)
        codex.LOOP = asyncio.get_running_loop()
        default, settings = codex.server_config()
        registry = Registry()
        session = PiSession.__new__(PiSession)
        session.profile = Path(tmp) / 'profile'
        (session.profile / 'sessions').mkdir(parents=True)
        session.host_jobs = {}
        session.handler = registry.call
        delivered = []
        async def send(value): delivered.append(value)
        session.send = send

        async def tool(instance, action, **args):
            delivered.clear()
            await session.host_call({'id': 'test', 'toolName': 'remote_control',
                'arguments': {'instance': instance, 'protocol': 'codex', 'action': action, 'args': args}}, context.current.get())
            assert len(delivered) == 1 and not delivered[0].get('isError'), delivered
            value = json.loads(delivered[0]['result']['content'][0]['text'])
            assert value.get('success', True), value
            return value

        for ident in ('desktop', 'oracle'):
            s = settings[ident]
            b = codex.AppServer(s, ident) if s.get('transport') == 'ssh' else codex.Bridge(s, ident)
            b.recent, b.send = store, AsyncMock()
            b.monitor_desktop = lambda _: None
            codex.SERVERS[ident] = b
            codex.BRIDGE = b
            row = report['instances'][ident] = {}
            tid = None
            try:
                args = {'action': 'start' if ident == 'oracle' else '_start', 'model': 'gpt-6-luna',
                        'mode': 'off', 'wake': False, 'prompt': 'Reply exactly CODEX-RECENT-OK. Do not use tools.'}
                if ident == 'desktop': args['project'] = (await b.project_catalog())[0]['project_id']
                started = await b.action(args, '77')
                job = b.jobs[started['job_id']]; tid = job['thread_id']
                deadline = time.monotonic() + 180
                while time.monotonic() < deadline:
                    status = await b.status(job)
                    if job['state'] not in codex.ACTIVE: break
                    await asyncio.sleep(2)
                else: raise TimeoutError('Test result not ready; no resubmission')
                assert job['state'] == 'completed' and b.answer(job) == 'CODEX-RECENT-OK', job['state']
                row['answer'] = b.answer(job)
                await asyncio.sleep(.1)
                row['recorded_by_live_completion_event'] = bool(store.pending(ident)['events'])
                # Same observation as the normal background monitor, if history beat its event.
                b.observe_recent(status)
                row['unread_before'] = len((await tool(ident, 'recent'))['events'])
                assert row['unread_before'] == 1
                assert len(Recent(store.path).pending(ident)['events']) == 1
                await tool(ident, 'list', limit=2)
                row['unread_after_list'] = len(store.pending(ident)['events'])
                assert row['unread_after_list'] == 1
                read = await tool(ident, 'read', thread_id=tid)
                row['read_has_answer'] = 'CODEX-RECENT-OK' in json.dumps(read)
                row['unread_after_read'] = len(store.pending(ident)['events'])
                assert row['read_has_answer'] and row['unread_after_read'] == 0
                b.observe_recent(status)
                assert Recent(store.path).pending(ident)['events'] == []
                row['survives_reopen_without_resurrection'] = True
                row['passed'] = True
            except Exception as error:
                row['error'] = type(error).__name__ + ': ' + str(error)[:300]
            finally:
                if tid:
                    try:
                        if job['state'] in codex.ACTIVE:
                            await b.rpc('turn/interrupt', {'threadId': tid, 'turnId': job['turn_id']})
                        await b.rpc('thread/archive', {'threadId': tid})
                    except Exception as error:
                        row['cleanup_error'] = type(error).__name__
                await b.close()
    report['passed'] = all(row.get('passed') and not row.get('cleanup_error') for row in report['instances'].values())
    Path('/tmp/evo-codex-recent-live.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2), flush=True)
    if not report['passed']: raise SystemExit(1)


if __name__ == '__main__': asyncio.run(main())
