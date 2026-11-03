"""Live native completion/reconnect and public-router checks; never posts to Discord."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
from evo_bot import context
from evo_bot.gateway import Bot
from evo_bot.integrations import codex, remote


async def main():
    results = Path('/tmp/evo-codex-uniform-tests')
    with tempfile.TemporaryDirectory(prefix='codex-uniform-live-') as temporary:
        codex.STATE = Path(temporary) / 'jobs.json'
        client = SimpleNamespace(is_ready=lambda: True)
        context.client = client
        codex.discord_factory(client, None)
        bot = Bot(); bot.write_status = lambda _: None
        bot.sessions['77'] = object()
        token = context.current.set(context.Turn('77'))
        report = {'discord_posts': 0, 'instances': {}}
        try:
            for ident in ('oracle', 'desktop'):
                bridge = codex.SERVERS[ident]
                bridge.adapter = bot
                async def unexpected_notice(*args, **kwargs):
                    raise AssertionError('Unexpected Discord notification')
                bridge.send = unexpected_notice
                models = await bridge.rpc('model/list', {'limit': 100})
                model = next((row['id'] for row in models['data'] if 'luna' in row['id'].lower()), None)
                expected = 'UNIFORM-' + ident.upper() + '-OK'
                params = {'prompt': 'Reply exactly ' + expected + '. Do not use any tools.'}
                if model: params['model'] = model
                if ident == 'oracle':
                    params['cwd'] = '/tmp'
                    output = json.loads(await asyncio.to_thread(remote.remote_control,
                        {'instance': ident, 'protocol': 'codex', 'action': 'start', 'args': params}))
                    assert output.get('success'), output
                else:
                    output = await bridge.action(dict(params, action='_start'), '77')
                job = bridge.jobs[output['job_id']]
                entry = report['instances'][ident] = {'thread_id': job['thread_id'],
                    'model': model, 'ephemeral': job.get('ephemeral'), 'default_wake': output['wake']}
                assert output['wake']
                queued = await asyncio.wait_for(bot.queue.get(), 180)
                bot.queue.task_done()
                assert queued['callback_valid']()
                event = json.loads(queued['text'].splitlines()[1])
                answer = bridge.answer(job)
                assert answer == expected, (ident, answer, job.get('state'))
                assert event['instance'] == ident and event['event'].startswith('completed:'), event
                entry.update(answer=answer, state=job['state'], callback=event)
                if ident == 'oracle':
                    async def routed(action, **args):
                        result = json.loads(await asyncio.to_thread(remote.remote_control,
                            {'instance': ident, 'protocol': 'codex', 'action': action, 'args': args}))
                        assert result.get('success'), result
                        return result
                    status = await routed('status', thread_id=job['thread_id'])
                    history = await routed('read', thread_id=job['thread_id'])
                    assert status['answer'] == expected, status
                    assert any(row['text'] == expected for row in history['messages']), history
                    entry.update(public_status_answer=status['answer'], public_read_rows=len(history['messages']))
                    first = await routed('list', limit=2)
                    second = await routed('list', limit=2, cursor=first['next_cursor']) if first.get('next_cursor') else None
                    pages = [first] + ([second] if second else [])
                    rows = [row for page in pages for row in page['threads']]
                    times = [row['updated_at'] for row in rows]
                    assert all(isinstance(t, int) and 1_000_000_000 < t < 10_000_000_000 for t in times), times
                    assert times == sorted(times, reverse=True), times
                    assert len({row['thread_id'] for row in rows}) == len(rows)
                    entry['list_pages'] = [{'thread_ids': [r['thread_id'] for r in p['threads']],
                        'updated_at': [r['updated_at'] for r in p['threads']], 'next_cursor': p['next_cursor']} for p in pages]
                    socket = bridge.ws; reader = bridge.reader
                    await socket.close(); await asyncio.gather(reader, return_exceptions=True)
                    after = await bridge.status(job)
                    assert job['state'] == 'completed', after
                    entry['reconnected_status'] = job['state']
                    await bridge.rpc('thread/archive', {'threadId': job['thread_id']})
                    entry['test_chat_archived'] = True
                await bridge.close()
            report['passed'] = True
        finally:
            bot.sessions.clear()
            await bot.close()
            context.current.reset(token)
            (results / 'uniform-live.json').write_text(json.dumps(report, indent=2))
        print(json.dumps(report, indent=2), flush=True)


if __name__ == '__main__': asyncio.run(main())
