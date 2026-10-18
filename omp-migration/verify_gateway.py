"""Capture the Discord boundary to verify steering without sending messages."""
import asyncio
import ast
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from types import SimpleNamespace
from evo_omp import context
from evo_omp.gateway import Bot
from evo_omp.context import OWNER

class Channel:
    id = 77
    def __init__(self): self.messages = []
    async def send(self, text, **kwargs): self.messages.append(text)
    @asynccontextmanager
    async def typing(self): yield

class Session:
    model = 'verification'
    rounds = tools = 0
    last_event = 'waiting'
    error = ''
    key = '77'
    abort_reason = ''
    transport_alive = True
    def __init__(self):
        self.stop = asyncio.Event(); self.started = asyncio.Event(); self.inputs = []
        self.closed = 0
    async def prompt(self, text, turn, images=None):
        self.inputs.append(text)
        self.started.set()
        if text == 'long': await self.stop.wait()
        if text == 'failure': raise RuntimeError('verification failure')
        if text == 'timeout':
            self.abort_reason = 'timeout'
            turn.cancelled.set()
            raise TimeoutError('verification timeout')
        if text == 'broken':
            self.transport_alive = False
            raise ConnectionError('verification transport lost')
        return 'answer:' + text
    async def abort(self): self.stop.set()
    async def reusable_after_error(self): return self.transport_alive
    async def close(self): self.closed += 1; self.stop.set()

class CaptureBot(Bot):
    def __init__(self):
        super().__init__(); self.channel = Channel(); self.fake = Session()
    async def wait_until_ready(self): pass
    def is_closed(self): return False
    def is_ready(self): return True
    def write_status(self, state): pass
    def get_channel(self, ident): return self.channel
    async def session(self, key, cron=False): return self.fake

async def main():
    for p in (Path(__file__).parent / 'evo_omp').rglob('*.py'):
        source = p.read_text()
        assert 'HERMES_SESSION_' not in source, p
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom) and node.level == 0:
                assert (node.module or '').split('.')[0] not in ('agent', 'gateway', 'tools'), p
    print('PASS no legacy agent imports or environment-session compatibility in runtime')
    b = CaptureBot()
    worker = asyncio.create_task(b.work_loop())
    def message(text, user=OWNER):
        return SimpleNamespace(author=SimpleNamespace(bot=False, id=int(user)), content=text,
            channel=b.channel, attachments=[], id=1)
    try:
        await b.on_message(message('long'))
        await asyncio.wait_for(b.fake.started.wait(), 2)
        await asyncio.wait_for(b.on_message(message('correction')), 3)
        await asyncio.wait_for(b.queue.join(), 3)
        assert b.fake.inputs == ['long', 'correction'], b.fake.inputs
        assert 'answer:long' not in b.channel.messages
        assert b.channel.messages[-1] == 'answer:correction', b.channel.messages
        assert len([m for m in b.channel.messages if '중단' in m]) == 1
        print('PASS new owner message interrupts, reports once, and continues with correction')
        before = list(b.fake.inputs)
        await b.on_message(message('unauthorized', '1'))
        assert b.fake.inputs == before and b.queue.empty()
        print('PASS other Discord accounts never enter the agent queue')
        async with b.admission_lock:
            b.enqueue('77', 'stale', [])
            b.epochs['77'] = b.epochs.get('77', 0) + 1
        await asyncio.wait_for(b.queue.join(), 3)
        assert 'stale' not in b.fake.inputs
        print('PASS reset/new invalidation discards previously queued messages')
        await b.on_message(message('failure'))
        await asyncio.wait_for(b.queue.join(), 3)
        await b.on_message(message('recovery'))
        await asyncio.wait_for(b.queue.join(), 3)
        assert b.channel.messages[-1] == 'answer:recovery'
        assert not worker.done()
        assert b.fake.closed == 0
        assert b.fake.inputs.count('failure') == 1
        print('PASS failed task preserves a healthy conversation without replay')
        await b.on_message(message('timeout'))
        await asyncio.wait_for(b.queue.join(), 3)
        assert 'verification timeout' in b.channel.messages[-1]
        assert b.fake.closed == 0
        b.fake.abort_reason = ''
        print('PASS timeout is reported even when cancellation revoked tool authority')
        await b.on_message(message('broken'))
        await asyncio.wait_for(b.queue.join(), 3)
        assert b.fake.closed == 1
        assert b.fake.inputs.count('broken') == 1
        assert not worker.done()
        print('PASS broken transport is released without replaying the failed turn')
        token = context.current.set(context.Turn('77', platform='web'))
        try:
            assert context.owner_message() is None and context.live_owner() is None
        finally: context.current.reset(token)
        token = context.current.set(context.Turn('77', cron=True))
        try:
            assert context.owner_identity() is not None
            assert context.owner_message() is None and context.live_owner() is None
        finally: context.current.reset(token)
        print('PASS scheduled read identity and interactive Discord authority are separate')
        previous_client = context.client
        context.client = SimpleNamespace(is_ready=lambda: True)
        turn = context.Turn('77')
        token = context.current.set(turn)
        received = []
        try:
            pending = asyncio.create_task(asyncio.to_thread(context.wait_for_approval,
                turn.key, received.append, {'command': 'synthetic; never executed'}))
            for _ in range(100):
                if received: break
                await asyncio.sleep(.01)
            assert received
            ident = received[0]['request_id']
            assert context.resolve_approval('discord:78', 'deny', ident) == 0
            assert context.resolve_approval(turn.key, 'deny', 'stale') == 0
            assert context.resolve_approval(turn.key, 'deny', ident) == 1
            assert context.resolve_approval(turn.key, 'deny', ident) == 0
            assert (await asyncio.wait_for(pending, 2))['choice'] == 'deny'
            assert not context._pending
            received.clear()
            pending = asyncio.create_task(asyncio.to_thread(context.wait_for_approval,
                turn.key, received.append, {'command': 'synthetic; never executed'}))
            for _ in range(100):
                if received: break
                await asyncio.sleep(.01)
            assert received
            turn.cancelled.set()
            assert (await asyncio.wait_for(pending, 2))['cancelled']
            assert not context._pending
        finally:
            context.current.reset(token)
            context.client = previous_client
        print('PASS exact approval request binding, duplicate rejection, and interruption withdrawal; no commands executed')
    finally:
        worker.cancel()
        with suppress(asyncio.CancelledError): await worker

if __name__ == '__main__': asyncio.run(main())
