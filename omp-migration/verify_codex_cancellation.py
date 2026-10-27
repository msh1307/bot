"""Capture Codex transports to check owner cancellation; no external requests."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

from evo_omp import context
from evo_omp.integrations import codex


def bridge():
    b = codex.Bridge.__new__(codex.Bridge)
    b.action_lock = asyncio.Lock()
    b.jobs, b.desktop_monitors, b.new_chat_views = {}, {}, {}
    b.pending, b.counter = {}, 0
    b.save = lambda: None
    b.monitor_desktop = lambda job: None
    b.settings = {'cwd': 'C:\\verification'}
    return b


async def queued_owner_send():
    b = bridge()
    calls = []
    entered = asyncio.Event()
    action = b.action

    async def capture_action(*args, **kwargs):
        entered.set()
        return await action(*args, **kwargs)

    async def desktop(args, **kwargs):
        calls.append(args)
        return {'message': 'accepted', 'mode': 'steer', 'turn_id': 'same-turn'}

    b.action, b.desktop = capture_action, desktop
    previous = codex.BRIDGE, codex.CLIENT, codex.LOOP, context.client
    codex.BRIDGE, codex.CLIENT = b, SimpleNamespace(is_ready=lambda: True)
    codex.LOOP, context.client = asyncio.get_running_loop(), codex.CLIENT
    turn = context.Turn('77')
    token = context.current.set(turn)
    await b.action_lock.acquire()
    task = asyncio.create_task(asyncio.to_thread(codex.codex_task,
        {'action': 'send', 'thread_id': 'existing', 'prompt': 'correction'}))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.sleep(0)
        turn.cancelled.set()
        b.action_lock.release()
        result = json.loads(await asyncio.wait_for(task, 3))
        assert not calls, 'Interrupted queued instruction was sent to Codex'
        assert result['success'] is False and 'interrupted' in result['error'].lower(), result
        assert not b.jobs
    finally:
        if b.action_lock.locked():
            b.action_lock.release()
        await asyncio.gather(task, return_exceptions=True)
        context.current.reset(token)
        codex.BRIDGE, codex.CLIENT, codex.LOOP, context.client = previous
    print('PASS interrupted owner request waiting for action lock is never sent')


async def main():
    await queued_owner_send()
    await cancelled_during_status()
    await cancelled_during_reconnect()
    await cancelled_in_http_worker()
    await accepted_before_cancel()
    await same_chat_followups()
    await owner_scope()


def guard(turn):
    def check():
        if turn.cancelled.is_set():
            raise InterruptedError('verification interrupted')
    return check


async def cancelled_during_status():
    for state in ('running', 'completed'):
        b, turn, calls = bridge(), context.Turn('77'), []
        job = {'job_id': 'job', 'thread_id': 'native-thread', 'turn_id': 'active-turn',
               'state': state, 'source': 'app_server', 'messages': {}}
        b.jobs['job'] = job

        async def status(job):
            turn.cancelled.set()

        async def rpc(method, args, **kwargs):
            calls.append(method)

        b.status, b.rpc = status, rpc
        try:
            await b.action({'action': 'send', 'job_id': 'job', 'prompt': 'correction'},
                           '77', check=guard(turn))
        except InterruptedError:
            pass
        else:
            raise AssertionError('Cancelled native send was accepted')
        assert not calls and job['state'] == state
    print('PASS cancellation during status blocks native steer/resume/start')


async def cancelled_during_reconnect():
    b, turn, calls = bridge(), context.Turn('77'), []

    async def connect():
        turn.cancelled.set()

    async def send(raw):
        calls.append(raw)

    b.connect, b.ws = connect, SimpleNamespace(send=send)
    try:
        await b.rpc('turn/steer', {}, check=guard(turn))
    except InterruptedError:
        pass
    else:
        raise AssertionError('Cancelled reconnect submitted a command')
    assert not calls and not b.pending
    print('PASS cancellation during reconnect blocks websocket submission')


async def cancelled_in_http_worker():
    b, turn = bridge(), context.Turn('77')
    b.settings.update(url='wss://verification.invalid:1234', ca='unused', token='unused')

    def tls(**kwargs):
        turn.cancelled.set()
        return SimpleNamespace()

    with patch.object(codex.ssl, 'create_default_context', tls), patch.object(codex, 'urlopen') as opened:
        try:
            await b.desktop({'action': 'send', 'thread_id': 'existing', 'prompt': 'cancelled'},
                            check=guard(turn))
        except InterruptedError:
            pass
        else:
            raise AssertionError('Cancelled HTTP worker opened a connection')
        opened.assert_not_called()
    print('PASS HTTP worker rechecks cancellation before opening connection')


async def accepted_before_cancel():
    b, turn, calls = bridge(), context.Turn('77'), []

    async def desktop(args, check=None):
        check()
        calls.append(args)
        turn.cancelled.set()  # Remote acceptance happened first.
        return {'message': 'accepted', 'mode': 'steer', 'turn_id': 'same-turn'}

    b.desktop = desktop
    result = await b.action({'action': 'send', 'thread_id': 'existing', 'prompt': 'accepted'},
                            '77', check=guard(turn))
    assert len(calls) == 1 and result['job_id'] in b.jobs
    assert b.jobs[result['job_id']]['thread_id'] == 'existing'
    assert b.jobs[result['job_id']]['turn_id'] == 'same-turn'
    print('PASS already accepted GUI command retains routing without replay or implicit stop')


async def same_chat_followups():
    b, turn, calls = bridge(), context.Turn('77'), []
    job = {'job_id': 'job', 'thread_id': 'native-thread', 'turn_id': 'active-turn',
           'state': 'running', 'source': 'app_server', 'messages': {}, 'ephemeral': False}
    b.jobs['job'] = job

    async def status(job):
        return {}

    async def rpc(method, args, check=None):
        if check:
            check()
        calls.append((method, args))
        return {'turn': {'id': 'next-turn'}}

    b.status, b.rpc = status, rpc
    await b.action({'action': 'send', 'job_id': 'job', 'prompt': 'correction'}, '77', check=guard(turn))
    assert calls == [('turn/steer', {'threadId': 'native-thread', 'expectedTurnId': 'active-turn',
                                  'input': [{'type': 'text', 'text': 'correction'}]})]
    assert job['turn_id'] == 'active-turn'
    calls.clear()
    job['state'] = 'completed'
    await b.action({'action': 'send', 'job_id': 'job', 'prompt': 'followup'}, '77', check=guard(turn))
    assert [name for name, _ in calls] == ['thread/resume', 'turn/start']
    assert all(args['threadId'] == 'native-thread' for _, args in calls)
    assert job['turn_id'] == 'next-turn'
    calls.clear()
    await b.action({'action': 'stop', 'job_id': 'job'}, '77', check=guard(turn))
    assert calls == [('turn/interrupt', {'threadId': 'native-thread', 'turnId': 'next-turn'})]
    print('PASS native active steer/completed followup/exact-turn stop reuse the same chat')

    async def failure(method, args, **kwargs):
        calls.append((method, args))
        raise ConnectionError('verification transport lost')

    calls.clear()
    b.rpc = failure
    try:
        await b.action({'action': 'send', 'job_id': 'job', 'prompt': 'no replay'}, '77', check=guard(turn))
    except ConnectionError:
        pass
    assert len(calls) == 1 and calls[0][0] == 'turn/steer'
    print('PASS ambiguous transport failure is not automatically resubmitted')


async def owner_scope():
    previous = codex.BRIDGE, codex.CLIENT, codex.LOOP, context.client
    codex.CLIENT = context.client = SimpleNamespace(is_ready=lambda: True)
    codex.LOOP = asyncio.get_running_loop()
    calls = []

    async def action(*args, **kwargs):
        calls.append(args)
        return {}

    codex.BRIDGE = SimpleNamespace(action=action)
    try:
        for turn in (context.Turn('77', user='1'), context.Turn('77', cron=True, user='1'),
                     context.Turn('77', platform='web'), context.Turn('77')):
            if turn.platform == 'discord' and turn.user == context.OWNER and not turn.cron:
                turn.cancelled.set()
            token = context.current.set(turn)
            try:
                result = json.loads(await asyncio.to_thread(codex.codex_task,
                    {'action': 'send', 'thread_id': 'existing', 'prompt': 'rejected'}))
                assert result['success'] is False
            finally:
                context.current.reset(token)
        assert not calls
        token = context.current.set(context.Turn('77', cron=True))
        try:
            result = json.loads(await asyncio.to_thread(codex.codex_task,
                {'action': 'send', 'thread_id': 'existing', 'prompt': 'authorized scheduled check'}))
            assert result['success'] is True and len(calls) == 1
            result = json.loads(await asyncio.to_thread(codex.codex_task, {'action': 'exec', 'command': ['whoami']}))
            assert result['success'] is False and len(calls) == 1
        finally:
            context.current.reset(token)
    finally:
        codex.BRIDGE, codex.CLIENT, codex.LOOP, context.client = previous
    print('PASS owner Discord scope; scheduled sends allowed, scheduled exec denied')


if __name__ == '__main__':
    asyncio.run(main())
