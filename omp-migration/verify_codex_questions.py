"""Question forwarding and opt-in callbacks; no network or Discord messages."""
import asyncio
from types import SimpleNamespace

from evo_omp.gateway import Bot
from evo_omp.integrations import codex


async def main():
    b = codex.Bridge.__new__(codex.Bridge)
    b.action_lock = asyncio.Lock()
    b.jobs, b.desktop_monitors = {}, {}
    b.save = lambda: None
    b.monitor_desktop = lambda job: None
    calls, events = [], []
    scope = ('reset', 'channel', 'session')
    b.adapter = SimpleNamespace(codex_callback_scope=lambda channel: scope,
        enqueue_codex_callback=lambda *args: events.append(args) or True)

    async def desktop(args, **kwargs):
        calls.append(args)
        return {'turn_id': 'turn', 'mode': args['action'], 'message': 'accepted'}

    b.desktop = desktop
    result = await b.action({'action': 'send', 'thread_id': 'thread', 'prompt': 'question'}, '77')
    job = b.jobs[result['job_id']]
    assert result['wake'] is False
    assert not b.queue_desktop_callback(job, 'input:1') and not events
    result = await b.action({'action': 'answer', 'thread_id': 'thread', 'request_id': 7,
                            'answers': {'target': ['staging']}, 'wake': True}, '77')
    assert calls[-1] == {'action': 'answer', 'thread_id': 'thread', 'request_id': 7,
                         'answers': {'target': ['staging']}}
    assert result['wake'] is True
    assert b.queue_desktop_callback(job, 'input:2')
    assert b.queue_desktop_callback(job, 'input:2') and len(events) == 1
    channel, text, captured_scope, valid = events[0]
    assert channel == '77' and captured_scope == scope and valid()
    assert 'not a new owner instruction' in text and 'question' not in text.split('\n')[1]
    job['_desktop_revision'] = 'newer'
    assert not valid()
    result = await b.action({'action': 'send', 'job_id': job['job_id'],
                            'prompt': 'followup', 'wake': False}, '77')
    assert not result['wake'] and not b.queue_desktop_callback(job, 'completed:turn')

    bot = Bot()
    bot.sessions['77'] = object()
    current_scope = bot.codex_callback_scope('77')
    assert not bot.enqueue_codex_callback('77', 'stale', None, lambda: True)
    assert bot.enqueue_codex_callback('77', 'event', current_scope, lambda: True)
    item = bot.queue.get_nowait()
    assert item['callback_valid']() and item['job'] is None
    bot.epochs['77'] = 1
    assert not item['callback_valid']()
    assert not bot.enqueue_codex_callback('77', 'stale', current_scope, lambda: True)
    current_scope = bot.codex_callback_scope('77')
    assert bot.enqueue_codex_callback('77', 'event', current_scope, lambda: True)
    item = bot.queue.get_nowait()
    bot.sessions['77'] = object()
    assert not item['callback_valid']()
    bot.sessions.clear()
    await bot.close()
    print('PASS GUI answer transport, wake default off/on/off, dedupe, stale revision/reset/session rejection')


if __name__ == '__main__':
    asyncio.run(main())
