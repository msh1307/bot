"""Discord admission -> real Pi steering, with a local scripted provider only."""
import asyncio
import base64
from contextlib import asynccontextmanager
import json
import logging
from pathlib import Path
import shutil
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from evo_bot import context, gateway, rpc
from verify_gateway import CaptureBot
from verify_context_payload import Handler, REQUESTS, STEPS


def message(bot, text):
    return SimpleNamespace(author=SimpleNamespace(bot=False, id=int(context.OWNER)),
        content=text, channel=bot.channel, attachments=[], id=1, add_reaction=bot.reaction)


async def main():
    checks = []
    def check(ok, label):
        assert ok, label
        checks.append(label)
    @asynccontextmanager
    async def quiet(*args, **kwargs): yield
    async def react(emoji): reactions.append(emoji)
    bot = CaptureBot(); bot.progress = quiet; bot.reaction = react
    reactions = []
    worker = asyncio.create_task(bot.work_loop())
    try:
        await bot.on_message(message(bot, 'long'))
        await asyncio.wait_for(bot.fake.started.wait(), 2)
        active = bot.active
        for text in ('ADDITION-1', 'ADDITION-2'):
            await bot.on_message(message(bot, text))
        check(bot.active is active and not active['turn'].cancelled.is_set(), 'original turn stays alive')
        check(bot.fake.steered == ['ADDITION-1', 'ADDITION-2'] and bot.queue.empty(), 'addenda steer exactly once')
        check(bot.fake.aborts == 0 and reactions == ['↪️', '↪️'], 'acknowledgement without abort')
        bot.fake.steer_reply = TimeoutError()
        await bot.on_message(message(bot, 'AMBIGUOUS'))
        check(bot.queue.empty() and '지연' in bot.channel.messages[-1], 'ambiguous delivery never replays as new prompt')
        bot.fake.steer_reply = 'not_running'
        await bot.on_message(message(bot, 'JUST-FINISHED'))
        check(bot.fake.aborts == 0 and bot.queue.qsize() == 1, 'settling race queues next task without abort')
        bot.fake.stop.set()
        await asyncio.wait_for(bot.queue.join(), 2)
        check(bot.fake.inputs == ['long', 'JUST-FINISHED'], 'next task runs once after settling')
    finally:
        worker.cancel(); await asyncio.gather(worker, return_exceptions=True)

    # Real bwrap runner + unmodified Pi. Only Discord sends and provider responses are captured.
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    cfg = Path(tempfile.mkdtemp(prefix='steer-check-', dir=rpc.RUNTIME))
    (cfg/'models.yml').write_text((rpc.ROOT/'models.yml').read_text().replace(':8080/v1', f':{server.server_port}/v1'))
    shutil.copyfile(rpc.ROOT/'config.yml', cfg/'config.yml')
    held, release = asyncio.Event(), asyncio.Event()
    async def hold(name, args, turn):
        held.set()
        while not release.is_set() and not turn.cancelled.is_set():
            await asyncio.sleep(.01)
        if turn.cancelled.is_set(): raise InterruptedError('Fixture stopped')
        return 'released'
    definition = {'name': 'fixture_hold', 'description': 'Pause local verification.', 'loadMode': 'essential',
                  'parameters': {'type': 'object', 'properties': {}}}
    session = rpc.PiSession('steer-check', 'sc117-flash-next-iq3_xxs', [definition], hold, cfg)
    bot = CaptureBot(); bot.fake = session; bot.sessions['77'] = session
    bot.progress = quiet; bot.reaction = react
    worker = None
    admission_events = []
    class CaptureAdmission(logging.Handler):
        def emit(self, record):
            if record.getMessage().startswith('owner_input_'):
                admission_events.append(record.getMessage())
    logger = logging.getLogger('evo-bot'); old_level = logger.level
    capture = CaptureAdmission(); logger.addHandler(capture); logger.setLevel(logging.INFO)
    try:
        await session.start()
        REQUESTS.clear(); STEPS[:] = [('fixture_hold', {})]
        with patch.object(gateway, 'context_footer', AsyncMock(return_value='')):
            worker = asyncio.create_task(bot.work_loop())
            await bot.on_message(message(bot, 'ORIGINAL-KAPITA-TASK'))
            await asyncio.wait_for(held.wait(), 15)
            original = bot.active['turn']
            first = message(bot, 'ADDENDUM-ONE')
            png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=')
            async def read_image(): return png
            first.attachments = [SimpleNamespace(content_type='image/png', size=len(png), read=read_image)]
            await bot.on_message(first)
            await bot.on_message(message(bot, 'ADDENDUM-TWO'))
            state = await session.request('get_state')
            check(state['queuedMessageCount'] == 2 and not original.cancelled.is_set(), 'actual Pi has two steering inputs; turn not cancelled')
            release.set()
            await asyncio.wait_for(bot.queue.join(), 20)
            users = json.dumps([m for m in REQUESTS[-1]['messages'] if m['role'] == 'user'])
            check(all(users.count(t) == 1 for t in ('ORIGINAL-KAPITA-TASK','ADDENDUM-ONE','ADDENDUM-TWO')),
                  'actual next provider payload retains original and each addendum once')
            attached = next(m for m in REQUESTS[-1]['messages'] if m['role'] == 'user'
                            and 'ADDENDUM-ONE' in json.dumps(m))
            check(any(b.get('type') == 'image_url' for b in attached['content']),
                  'actual Discord image attachment passes validation and reaches steering payload')
            check(sum(s.startswith('owner_input_steered ') for s in admission_events) == 2,
                  'actual on_message to Pi path emits both owner_input_steered events')
            check(len(REQUESTS) == 2 and bot.channel.messages[-1] == 'VERIFIED_CONTEXT',
                  f'one continued turn, no replay: calls={len(REQUESTS)}, output={bot.channel.messages[-1:]}')
            state = await session.request('get_state')
            check(state['isSettled'] and state['queuedMessageCount'] == 0, 'actual session fully settles')
            held.clear(); release.clear(); STEPS[:] = [('fixture_hold', {})]
            await bot.on_message(message(bot, 'STOP-TEST'))
            await asyncio.wait_for(held.wait(), 15)
            await bot.on_message(message(bot, 'MUST-NOT-LEAK'))
            await bot.command(bot.channel, 'stop', '')
            await asyncio.wait_for(bot.queue.join(), 10)
            if session.transport_alive:
                check((await session.request('get_state'))['queuedMessageCount'] == 0, '/stop clears actual pending steering')
            else:
                check(session.closing, '/stop releases a session whose host call has not settled')
                session = rpc.PiSession('77', 'sc117-flash-next-iq3_xxs', [definition], hold, cfg)
                await session.start(); bot.fake = session; bot.sessions['77'] = session
            release.set()
            await bot.on_message(message(bot, 'NEXT-TASK'))
            await asyncio.wait_for(bot.queue.join(), 15)
            check('MUST-NOT-LEAK' not in json.dumps(REQUESTS[-1]), 'stopped addendum never leaks into next task')
        result = {'passed': len(checks), 'failed': 0, 'checks': checks,
                  'actual_pi_bwrap': True, 'evo_model_requests': 0, 'discord_posts': 0,
                  'captured_admission_events': admission_events}
        Path(sys.argv[1]).write_text(json.dumps(result, indent=2))
        print(json.dumps(result))
    finally:
        logger.removeHandler(capture); logger.setLevel(old_level)
        release.set()
        if worker: worker.cancel(); await asyncio.gather(worker, return_exceptions=True)
        await session.close(); server.shutdown(); shutil.rmtree(cfg)


if __name__ == '__main__': asyncio.run(main())
