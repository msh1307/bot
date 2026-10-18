"""Timing contracts and optional real OMP callbacks; never sends Discord messages."""
import argparse
import asyncio
import contextlib
from datetime import datetime, timedelta
import json
from pathlib import Path
import time
from types import SimpleNamespace

from evo_omp import context, tools
from evo_omp.gateway import Bot
from verify_gateway import CaptureBot


def check(condition, label):
    assert condition, label
    print('PASS', label, flush=True)


def rejects(fn, args, error=ValueError):
    try:
        fn(args)
    except error:
        return
    raise AssertionError(('accepted invalid input', args))


def defer(seconds=600, channel='77'):
    turn = context.Turn(channel, model='sc117-flash-next-iq3_xxs')
    token = context.current.set(turn)
    try:
        return tools.schedule({'action': 'defer', 'after_seconds': seconds,
                               'prompt': 'Check task example-id; report its current result.'})
    finally:
        context.current.reset(token)


def make_due(ident):
    jobs = tools.load_jobs()
    for job in jobs:
        if job['id'] == ident:
            job['next_run_at'] = (datetime.now(tools.TZ) - timedelta(seconds=1)).isoformat()
    tools.save_jobs(jobs)


async def contracts():
    turn = context.Turn('77')
    token = context.current.set(turn)
    try:
        for value in (0, -1, 301, True, 1.5, '1', None):
            rejects(tools.wait, {'seconds': value})
        started = time.monotonic()
        wait = asyncio.create_task(asyncio.to_thread(tools.wait, {'seconds': 1}))
        await asyncio.sleep(.05)
        check(not wait.done(), 'wait releases event loop while sleeping')
        result = await wait
        check(time.monotonic()-started >= .95 and result['remaining_wait_seconds'] == 299,
              'one-second wait completes and accounts for budget')
        turn.waited_seconds = 299
        rejects(tools.wait, {'seconds': 2})
        turn.waited_seconds = 0
        wait = asyncio.create_task(asyncio.to_thread(tools.wait, {'seconds': 300}))
        await asyncio.sleep(.05)
        started = time.monotonic()
        turn.cancelled.set()
        try:
            await asyncio.wait_for(wait, 1)
            raise AssertionError('Wait did not cancel')
        except InterruptedError:
            pass
        check(time.monotonic()-started < 1, '300-second wait promptly cancels; cumulative limit enforced')
    finally:
        context.current.reset(token)

    tools.save_jobs([])
    ident = defer()['id']
    job = tools.load_jobs()[0]
    delay = (datetime.fromisoformat(job['next_run_at']) - datetime.now(tools.TZ)).total_seconds()
    check(598 < delay <= 600 and job['continuation'], 'relative ten-minute callback saved with model and task details')
    token = context.current.set(context.Turn('77'))
    try:
        for value in (0, 86401, True, '600'):
            rejects(tools.schedule, {'action': 'defer', 'after_seconds': value, 'prompt': 'check'})
        tools.schedule({'action': 'pause', 'id': ident})
        defer()
        check(len(tools.load_jobs()) == 2, 'paused callback survives creation of another timer')
        tools.schedule({'action': 'resume', 'id': ident})
    finally:
        context.current.reset(token)
    # One-shot claims must be serialized, cancellable, and recoverable from queue pressure.
    make_due(ident)
    check(Bot.claim_due(0) == [], 'full queue leaves timers pending')
    claim = Bot.claim_due(1)[0]
    check(Bot.claim_due(1) == [] and Bot.job_claim_valid(claim), 'due callback claimed only once')
    Bot.finish_job(claim, 'queue_full')
    claim = Bot.claim_due(1)[0]
    check(Bot.job_claim_valid(claim), 'queue admission race returns callback for later retry')
    token = context.current.set(context.Turn('77'))
    try:
        tools.schedule({'action': 'pause', 'id': ident})
    finally:
        context.current.reset(token)
    Bot.finish_job(claim, 'ok')
    check(not Bot.job_claim_valid(claim) and tools.load_jobs()[0]['last_status'] == 'paused',
          'pause revokes queued claim and stale completion cannot overwrite it')
    tools.save_jobs([])
    ident = defer()['id']; make_due(ident)
    claim = Bot.claim_due(1)[0]
    Bot.recover_schedules()
    check(not tools.load_jobs()[0]['enabled'] and not Bot.job_claim_valid(claim),
          'restart does not replay a callback whose actions may already have executed')
    pending = defer()['id']
    Bot.recover_schedules()
    check(any(j['id'] == pending and j['enabled'] for j in tools.load_jobs()),
          'unclaimed callback survives restart')

    tools.save_jobs([])
    token = context.current.set(context.Turn('77'))
    try:
        cron = tools.schedule({'action': 'create', 'name': 'retained briefing',
                               'cron': '0 8 * * *', 'prompt': 'Public sources only'})['id']
    finally:
        context.current.reset(token)
    first = defer()['id']; second = defer(channel='78')['id']
    tools.cancel_deferred('77')
    jobs = {j['id']: j for j in tools.load_jobs()}
    check(not jobs[first]['enabled'] and jobs[second]['enabled'] and jobs[cron]['enabled'],
          'channel cancellation retains other channels and normal alarms')

    tools.save_jobs([])
    bot = CaptureBot()
    bot.fake.compacting = False
    bot.sessions['77'] = bot.fake
    worker = asyncio.create_task(bot.work_loop())
    try:
        ident = defer()['id']; make_due(ident)
        claim = Bot.claim_due(1)[0]
        bot.enqueue('77', claim['prompt'], [], claim)
        await asyncio.wait_for(bot.queue.join(), 3)
        check(len(bot.fake.inputs) == 1 and 'Earlier conversation is available' in bot.fake.inputs[0]
              and bot.fake.closed == 0, 'callback uses existing conversation and keeps it open afterwards')
        ident = defer()['id']; make_due(ident)
        claim = Bot.claim_due(1)[0]
        tools.cancel_deferred('77')
        bot.enqueue('77', claim['prompt'], [], claim)
        await asyncio.wait_for(bot.queue.join(), 3)
        check(len(bot.fake.inputs) == 1, 'cancelled queued callback never reaches model')
        ident = defer()['id']
        await bot.command(bot.channel, 'stop', '')
        check(not next(j for j in tools.load_jobs() if j['id'] == ident)['enabled'], '/stop cancels future callbacks')
        ident = defer()['id']
        message = SimpleNamespace(author=SimpleNamespace(bot=False,id=int(context.OWNER)),
                                  channel=bot.channel,content='correction',attachments=[])
        await bot.on_message(message)
        await asyncio.wait_for(bot.queue.join(), 3)
        check(next(j for j in tools.load_jobs() if j['id'] == ident)['enabled']
              and bot.fake.inputs[-1] == 'correction', 'new owner message proceeds without cancelling pending timer')
    finally:
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker

    token = context.current.set(context.Turn('77', cron=True))
    try:
        rejects(tools.wait, {'seconds':1}, PermissionError)
        rejects(tools.schedule, {'action':'defer','after_seconds':1,'prompt':'x'}, PermissionError)
    finally:
        context.current.reset(token)
    check(True, 'read-only cron tasks cannot self-promote into ordinary owner callbacks')


async def live():
    class Channel:
        id = 77
        def __init__(self): self.messages = []
        @contextlib.asynccontextmanager
        async def typing(self): yield
        async def send(self, content, **kwargs): self.messages.append(content)

    class LiveBot(Bot):
        def __init__(self): super().__init__(); self.channel = Channel()
        async def wait_until_ready(self): pass
        def is_closed(self): return False
        def is_ready(self): return True
        def get_channel(self, ident): return self.channel
        def write_status(self, state): pass

    bot = LiveBot()
    # Use production worker/scheduler/session, capture only the Discord transport.
    bot.progress = lambda *args, **kwargs: quiet_progress()
    await bot.registry.initialize_web()
    check({'wait','schedule'} <= {d['name'] for d in bot.registry.definitions()}, 'production registry exposes wait and schedule')
    original = bot.registry.call
    calls = []
    async def safe_call(name, args, turn):
        if name not in ('wait', 'schedule'): raise PermissionError('Verification only: timing tools allowed')
        calls.append({'tool':name,'args':args,'deferred':turn.deferred})
        return await original(name, args, turn)
    bot.registry.call = safe_call
    bot.registry.tools = {n:v for n,v in bot.registry.tools.items() if n in ('wait','schedule')}
    context.client = bot
    tools.save_jobs([])
    worker = asyncio.create_task(bot.work_loop())
    scheduler = asyncio.create_task(bot.schedule_loop())
    started = time.monotonic()
    try:
        bot.enqueue('77', 'This is a timing-tool verification. Use wait for 2 seconds, then reply exactly WAIT_OK.', [])
        await asyncio.wait_for(bot.queue.join(), 240)
        check(any(c['tool']=='wait' and c['args'].get('seconds')==2 for c in calls)
              and 'WAIT_OK' in bot.channel.messages[-1], 'real OMP model calls wait and receives its result')
        session = bot.sessions['77']
        bot.enqueue('77', 'Use schedule action=defer, after_seconds=2, name=Timing verification, '
                    'prompt="Reply exactly TIMER_OK when this continuation runs." Then finish this turn with TIMER_SET.', [])
        await asyncio.wait_for(bot.queue.join(), 240)
        for _ in range(600):
            if any(j.get('continuation') and j.get('last_status')=='ok' for j in tools.load_jobs()): break
            await asyncio.sleep(.25)
        else: raise AssertionError('Real timer did not finish')
        check(bot.sessions.get('77') is session and 'TIMER_OK' in bot.channel.messages[-1],
              'real scheduler resumes the same OMP conversation and captures callback response')
        result = {'elapsed_seconds':round(time.monotonic()-started,3), 'calls':calls,
                  'messages':bot.channel.messages,'same_session':True,'jobs':tools.load_jobs()}
        (tools.STATE.parent/'live-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    finally:
        for task in (worker,scheduler): task.cancel()
        await asyncio.gather(worker,scheduler,return_exceptions=True)
        for session in list(bot.sessions.values()): await session.close()
        await bot.registry.close()


@contextlib.asynccontextmanager
async def quiet_progress(): yield


async def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--live', action='store_true')
    parser.add_argument('--state', required=True); args = parser.parse_args()
    state = Path(args.state).resolve()
    if not str(state).startswith('/run/evo-timing-'): raise SystemExit('Use isolated /run/evo-timing-* state')
    state.mkdir(parents=True, exist_ok=True)
    tools.STATE = state
    await contracts()
    if args.live: await live()


if __name__ == '__main__': asyncio.run(main())
