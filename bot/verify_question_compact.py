"""Exercise question resolution and the next compact RPC, without Discord/model calls."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from evo_bot import context
from evo_bot.gateway import Bot
from evo_bot.rpc import PiSession


async def check(name, parts=(), resolution='answered', cancelled=False):
    initial = 'Read the existing cases. Wait for execution approval.'
    turn = context.Turn('77', task_instructions=initial)
    message = SimpleNamespace(id=1, edit=AsyncMock())
    bot = SimpleNamespace(questions={}, active={'turn': turn}, write_status=lambda _: None)

    async def send(*args, **kwargs):
        pending = bot.questions['77']['input']
        tickets = [pending.begin_message() for _ in parts]
        # Attachment completion order must not change owner message order.
        for ticket in reversed(tickets):
            pending.finish_message(ticket, *parts[ticket])
        if resolution == 'timeout':
            pending.last_activity -= 301
        else:
            pending.resolve(resolution)
        if cancelled:
            turn.cancelled.set()
        return message

    bot.get_channel = lambda _: SimpleNamespace(send=send)
    try:
        reply = await Bot.user_input(bot, {'question': 'How should I proceed?'}, turn)
        assert not cancelled, name
    except InterruptedError:
        assert cancelled, name
        reply = None
    assert not bot.questions, name
    message.edit.assert_awaited_once()

    pi = SimpleNamespace(compact_cancelled=False, request=AsyncMock(return_value={}))
    token = context.current.set(turn)
    try:
        await PiSession.compact(pi)
    finally:
        context.current.reset(token)
    # Compaction reads Pi's transcript; the callback task snapshot is not injected again.
    pi.request.assert_awaited_once_with('compact', timeout=None)
    anchor = turn.task_instructions
    if resolution == 'answered' and not cancelled:
        expected = '\n\n'.join(text for text, _ in parts if text) or '첨부 이미지를 확인해줘.'
        assert expected in anchor, name
        assert anchor.count('[Owner addition/correction]') == 1, name
        if name == 'image':
            assert reply['content'][1] == parts[0][1][0]
            assert json.loads(reply['content'][0]['text'])['status'] == 'answered'
    else:
        assert anchor == initial, name
    return {'case': name, 'passed': True}


async def main():
    image = {'type': 'image', 'mimeType': 'image/png', 'data': 'SYNTHETIC_IMAGE'}
    cases = [
        ('correction', [('Do not execute. Only explain the existing cases.', [])]),
        ('ordered_replies', [('Do not run it.', []), ('Only count the existing cases.', [])]),
        ('image', [('', [image])]),
        ('skipped', (), 'skipped'),
        ('timeout', (), 'timeout'),
        ('cancelled', [('This must not be retained.', [])], 'answered', True),
    ]
    rows = [await check(*case) for case in cases]
    print(json.dumps({'passed': len(rows), 'cases': rows,
                      'discord_posts': 0, 'model_calls': 0}, ensure_ascii=False))


if __name__ == '__main__':
    asyncio.run(main())
