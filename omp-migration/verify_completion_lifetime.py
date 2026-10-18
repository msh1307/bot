"""Bounded completion delivery and tool lifetime; captured I/O only."""
import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import patch

from evo_omp import context
from evo_omp.integrations import codex
from evo_omp.rpc import OmpSession


def bridge():
    b = codex.Bridge.__new__(codex.Bridge)
    b.action_lock = asyncio.Lock()
    b.jobs, b.desktop_monitors = {}, {}
    b.save = lambda: None
    return b


def job():
    return dict(job_id='job', thread_id='thread', turn_id='first',
                state='running', source='app_server', channel='77',
                messages={'answer': {'text': 'first answer', 'phase': 'final_answer'}})


def completed(b, j, tasks):
    create = asyncio.create_task

    def capture(coroutine):
        task = create(coroutine)
        tasks.append(task)
        return task

    with patch.object(codex.asyncio, 'create_task', capture):
        b.event(j, 'turn/completed', {'turn': {'id': j['turn_id'], 'status': 'completed'}})


async def duplicate_delivery():
    b, j, tasks, sent = bridge(), job(), [], []
    entered, release = asyncio.Event(), asyncio.Event()

    async def send(target, text, **kwargs):
        sent.append((dict(target), text))
        entered.set()
        await release.wait()

    b.send = send
    completed(b, j, tasks)
    completed(b, j, tasks)
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.sleep(0)
        assert len(sent) == 1, 'Duplicate completion was sent while first delivery awaited Discord'
    finally:
        release.set()
        await asyncio.gather(*tasks)
    await b.notify_complete(j)
    assert len(sent) == 1, 'Completed delivery was not deduplicated'
    print('PASS repeated completion events deliver one notification')


async def queued_snapshot():
    b, j, tasks, sent = bridge(), job(), [], []

    async def send(target, text, **kwargs):
        sent.append((dict(target), text))

    b.send = send
    completed(b, j, tasks)
    # The next turn can be accepted before the queued notification even starts.
    j.update(turn_id='second', state='running', channel='88',
             messages={'answer': {'text': 'second answer', 'phase': 'final_answer'}})
    await asyncio.gather(*tasks)
    assert len(sent) == 1 and sent[0][0]['channel'] == '77', sent
    assert 'first answer' in sent[0][1] and 'completed' in sent[0][1], sent
    assert j.get('notified_turn') != 'second', 'Old completion marked the next turn delivered'
    print('PASS queued completion captures its own turn, answer, state and channel')


async def next_turn_during_send():
    b, j, tasks, sent = bridge(), job(), [], []
    entered, release = asyncio.Event(), asyncio.Event()

    async def send(target, text, **kwargs):
        sent.append(text)
        entered.set()
        await release.wait()

    b.send = send
    completed(b, j, tasks)
    await asyncio.wait_for(entered.wait(), 2)
    j.update(turn_id='second', state='running',
             messages={'answer': {'text': 'second answer', 'phase': 'final_answer'}})
    release.set()
    await asyncio.gather(*tasks)
    assert j.get('notified_turn') != 'second', 'In-flight delivery consumed the next completion'
    completed(b, j, tasks)
    await asyncio.gather(*tasks)
    assert len(sent) == 2 and 'second answer' in sent[1], sent
    print('PASS next turn still delivers after an older notification awaits Discord')


async def failed_delivery_retry():
    b, j, tasks, sent = bridge(), job(), [], []

    async def fail(target, text, **kwargs):
        raise OSError('captured delivery failure')

    b.send = fail
    completed(b, j, tasks)
    await asyncio.gather(*tasks)
    assert 'failed' in j.get('delivery', '')

    async def send(target, text, **kwargs):
        sent.append(text)

    b.send = send
    await b.notify_complete(j)
    assert len(sent) == 1 and 'failed' not in j.get('delivery', '')
    print('PASS failed notification releases its claim and explicit retry can deliver')


async def bounded_delivery():
    b, j, sent = bridge(), job(), []

    async def send(target, text, **kwargs):
        assert len(text.encode('utf-16-le')) // 2 <= 2000
        attachment = kwargs.get('file')
        if attachment:
            assert attachment.fp.read().decode() == '😀' * 2000
            attachment.close()
        sent.append(target['turn_id'])

    b.send = send
    for i in range(36):
        j.update(turn_id=str(i), state='completed')
        await b.notify_complete(j)
    assert len(j.get('_completion_delivery', {})) <= 32 and len(sent) == 36
    await b.notify_complete(j)
    assert len(sent) == 36
    j.update(turn_id='unicode', messages={'long': {'text': '😀' * 2000}})
    await b.notify_complete(j)
    assert sent[-1] == 'unicode' and len(j.get('_completion_delivery', {})) <= 32
    assert 'delivery' not in j
    print('PASS delivery bookkeeping stays at 32 turns; long Unicode result fits Discord with attachment')


async def desktop_followup_monitor():
    b, j, sent = bridge(), job(), []
    j['source'] = 'desktop'
    b.jobs['job'] = j
    ticks = asyncio.Queue()
    entered, release = asyncio.Event(), asyncio.Event()
    real_sleep = asyncio.sleep

    async def sleep(delay):
        await ticks.get()

    async def status(target):
        target.update(state='completed', phase='completed')

    async def send(target, text, **kwargs):
        sent.append(text)
        if len(sent) == 1:
            entered.set()
            await release.wait()

    async def desktop(args, **kwargs):
        return dict(turn_id='second', previous_turn_id='first', mode='start_turn', message='accepted')

    b.status, b.send, b.desktop = status, send, desktop
    with patch.object(codex.asyncio, 'sleep', sleep):
        b.monitor_desktop(j)
        watcher = b.desktop_monitors['job']
        try:
            ticks.put_nowait(None)
            await asyncio.wait_for(entered.wait(), 2)
            await b.desktop_action({'action': 'send', 'thread_id': 'thread', 'prompt': 'followup'}, '77')
            j['messages'] = {'answer': {'text': 'second answer', 'phase': 'final_answer'}}
            release.set()
            await real_sleep(0)
            await real_sleep(0)
            assert not watcher.done(), 'New GUI turn lost its watcher during previous result delivery'
            ticks.put_nowait(None)
            await asyncio.wait_for(watcher, 2)
            assert len(sent) == 2 and 'second answer' in sent[1], sent
        finally:
            release.set()
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
    print('PASS GUI followup keeps its completion watcher across old-result delivery')


async def message_bound():
    b, j, tasks = bridge(), job(), []
    j['messages'] = {str(i): {'text': str(i)} for i in range(32)}
    b.event(j, 'item/agentMessage/delta', {'itemId': '31', 'delta': '+'})
    assert len(j['messages']) == 32 and '0' in j['messages']
    b.event(j, 'item/started', {'item': {'type': 'agentMessage', 'id': '32', 'text': 'new'}})
    assert len(j['messages']) == 32 and '0' not in j['messages'] and '1' in j['messages']
    b.event(j, 'item/completed', {'item': {'type': 'agentMessage', 'id': '32', 'text': 'final'}})
    assert len(j['messages']) == 32 and j['messages']['32']['text'] == 'final'
    b.event(j, 'turn/started', {'turn': {'id': 'second'}})
    assert not j['messages'], 'Native new turn retained previous final-answer items'
    b.event(j, 'item/started', {'item': {'type': 'agentMessage', 'id': 'new', 'text': 'answer'}})
    b.event(j, 'turn/started', {'turn': {'id': 'second'}})
    assert 'new' in j['messages'], 'Repeated started event erased live output'
    print('PASS 32-message bound evicts only on insertion; native new turn clears old items')


async def stale_desktop_status():
    b, j = bridge(), job()
    j['source'] = 'desktop'
    b.jobs['job'] = j
    b.monitor_desktop = lambda target: None
    entered, release = asyncio.Event(), asyncio.Event()

    async def desktop(args, **kwargs):
        if args['action'] == 'status':
            entered.set()
            await release.wait()
            return dict(turn_id='first', turn_status='inProgress', answer='old answer', phase='old phase')
        return dict(turn_id='first', previous_turn_id='first', mode='steer', message='accepted')

    b.desktop = desktop
    poll = asyncio.create_task(b.status(j))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await b.desktop_action({'action': 'send', 'thread_id': 'thread', 'prompt': 'followup'}, '77')
        release.set()
        result = await asyncio.wait_for(poll, 2)
        assert j['turn_id'] == 'first' and j['state'] == 'running', j
        assert not j['messages'] and j['phase'] == 'GUI instruction accepted', result
    finally:
        release.set()
        await asyncio.gather(poll, return_exceptions=True)
    print('PASS delayed GUI status cannot overwrite a newly accepted instruction')


async def tool_lifetime():
    for outcome in ('completed', 'failed', 'rejected', 'not_invoked'):
        calls, frames = [], []

        async def handler(name, args, turn):
            calls.append(name)
            return 'captured'

        s = OmpSession('lifetime', 'verification', [{'name': 'external_tool'}], handler)

        async def send(frame):
            frames.append(frame)
            if frame['type'] != 'prompt':
                return
            await s.dispatch(dict(type='response', id=frame['id'],
                success=outcome != 'rejected', data={'agentInvoked': outcome != 'not_invoked'}))
            if outcome in ('rejected', 'not_invoked'):
                return
            await s.dispatch(dict(type='message_end', message={'role': 'assistant',
                'content': [{'type': 'text', 'text': 'answer'}]}))
            await s.dispatch(dict(type='prompt_result', id=frame['id'], status=outcome,
                                 sessionSettled=True, error='captured error' if outcome == 'failed' else ''))

        s.send = send
        try:
            await s.prompt('captured request', context.Turn('77'))
        except RuntimeError:
            assert outcome in ('failed', 'rejected')
        else:
            assert outcome not in ('failed', 'rejected')
        assert s.turn is None, 'Completed/rejected prompt retained a live tool identity'
        await s.dispatch(dict(type='host_tool_call', id='late', toolName='external_tool', arguments={}))
        await asyncio.sleep(0)
        assert not calls and frames[-1].get('isError') is True, frames
        assert not s.host_jobs
        new_turn = context.Turn('77')
        s.turn = new_turn
        await s.dispatch(dict(type='host_tool_cancel', targetId='late-old-call'))
        assert not new_turn.cancelled.is_set(), 'Stale cancellation interrupted an unrelated turn'
        running = asyncio.get_running_loop().create_future()
        s.host_jobs['live-call'] = running
        await s.dispatch(dict(type='host_tool_cancel', targetId='live-call'))
        assert new_turn.cancelled.is_set(), 'Live host call cancellation stopped working'
        running.set_result(None)
    print('PASS late host tools are rejected after success, failure, rejection and uninvoked prompt')
    print('PASS stale host-tool cancellation is ignored; a live call still cancels its turn')


async def main():
    for test in (duplicate_delivery, queued_snapshot, next_turn_during_send,
                 failed_delivery_retry, bounded_delivery, desktop_followup_monitor, message_bound,
                 stale_desktop_status, tool_lifetime):
        try:
            await test()
        except Exception as error:
            print('FAIL', test.__name__, type(error).__name__, str(error))
            failures.append(test.__name__)
    assert not failures, failures


if __name__ == '__main__':
    try:
        import discord
    except ModuleNotFoundError:
        # Short captured notifications need no Discord implementation locally.
        # EVO runs this same check with its installed Discord package.
        sys.modules['discord'] = SimpleNamespace(File=lambda fp, filename:
            SimpleNamespace(fp=fp, close=fp.close))
    failures = []
    asyncio.run(main())
