"""Existing closed chats: bounded reads and safe GUI loading, without live messages."""
import asyncio
from evo_omp.integrations import codex


async def main():
    bridge = codex.Bridge.__new__(codex.Bridge)
    bridge.action_lock = asyncio.Lock()
    bridge.jobs = {}
    bridge.save = lambda: None
    bridge.monitor_desktop = lambda job: None
    calls = []
    opened = False

    async def desktop(args, **kwargs):
        nonlocal opened
        calls.append(('desktop', args))
        if args['action'] == 'open':
            opened = True
            return {'gui_attached': True}
        if not opened:
            raise codex.DesktopError('no_gui_owner', 'closed')
        return {'turn_id': 'new-turn', 'mode': 'start_turn', 'message': 'accepted'}

    async def rpc(method, params, **kwargs):
        calls.append((method, params))
        if method == 'thread/read':
            assert params['includeTurns'] is False
            return {'thread': {'name': 'existing', 'status': {'type': 'notLoaded'}}}
        assert method == 'thread/turns/list'
        assert params == {'threadId': 'existing-id', 'limit': 3, 'sortDirection': 'desc', 'itemsView': 'summary'}
        return {'data': [{'id': 'old-turn', 'status': 'completed', 'items': [
            {'type': 'userMessage', 'content': [{'type': 'text', 'text': 'latest user'}]},
            {'type': 'reasoning', 'text': 'private reasoning'},
            {'type': 'commandExecution', 'aggregatedOutput': 'large tool output'},
            *[{'type': 'agentMessage', 'text': str(i) * 9000} for i in range(12)]]}]}

    bridge.desktop, bridge.rpc = desktop, rpc
    reply = await bridge.action({'action': 'status', 'thread_id': 'existing-id'}, '77')
    assert reply['source'] == 'stored_history' and not reply['gui_attached']
    assert len(reply['recent_messages']) <= 8
    assert sum(len(m['text']) for m in reply['recent_messages']) <= 6000
    assert any(m['text'] == 'latest user' for m in reply['recent_messages'])
    assert 'private reasoning' not in str(reply) and 'large tool output' not in str(reply)
    assert not opened and not bridge.jobs
    assert not any(method == 'thread/resume' for method, _ in calls)

    calls.clear()
    reply = await bridge.action({'action': 'send', 'thread_id': 'existing-id', 'prompt': 'follow up'}, '77')
    assert reply['wake'] is False and len(bridge.jobs) == 1
    assert [method for method, _ in calls] == ['desktop', 'thread/read', 'desktop', 'desktop']
    assert [args['action'] for method, args in calls if method == 'desktop'] == ['send', 'open', 'send']
    assert not any(method in ('thread/start', 'thread/resume', 'turn/start') for method, _ in calls)

    for code in ('outcome_unknown', 'desktop_unavailable', 'waiting_for_input', 'owner_changed'):
        attempts = []
        async def failure(args, **kwargs):
            attempts.append(args)
            raise codex.DesktopError(code, 'failure')
        bridge.desktop = failure
        try:
            await bridge.action({'action': 'send', 'thread_id': 'existing-id', 'prompt': 'follow up'}, '77')
            raise AssertionError('failure must propagate')
        except codex.DesktopError as error:
            assert error.code == code
        assert len(attempts) == 1
    await history_paging()
    print('PASS closed-history tail, user retention, no reasoning/tools, no resume, same-chat GUI load, ambiguous errors never replayed')


async def history_paging():
    b = codex.Bridge.__new__(codex.Bridge)
    b.jobs = {}
    visible = ['latest question', ('긴 답변\n' * 3000), 'last answer', 'older question', 'older answer']
    hidden = {'type': 'reasoning', 'text': 'hidden reasoning'}
    turns = [
        {'id': 'latest', 'status': 'completed', 'items': [
            {'type': 'userMessage', 'content': [{'type': 'text', 'text': visible[0]}]}, hidden,
            *[{'type': 'agentMessage', 'text': text} for text in visible[1:3]]]},
        {'id': 'older', 'status': 'completed', 'items': [
            {'type': 'userMessage', 'content': [{'type': 'text', 'text': visible[3]}]},
            {'type': 'agentMessage', 'text': visible[4]}]}]
    calls = []

    async def rpc(method, params, **kwargs):
        calls.append((method, params))
        if method == 'thread/read':
            assert params['includeTurns'] is False
            return {'thread': {'name': 'history'}}
        assert method == 'thread/turns/list' and params['limit'] == 1 and params['itemsView'] == 'summary'
        index = 1 if params.get('cursor') == 'older-page' else 0
        return {'data': [turns[index]], 'nextCursor': 'older-page' if index == 0 else None}

    async def desktop(args, **kwargs):
        raise codex.DesktopError('no_gui_owner', 'closed')

    b.rpc, b.desktop = rpc, desktop
    cursor, chunks, pages = None, [], 0
    while True:
        page = await b.action({'action': 'read', 'thread_id': 'history',
                               **({'cursor': cursor} if cursor else {})}, '77')
        pages += 1
        assert pages < 10 and len(page['recent_messages']) <= 8
        assert sum(len(m['text']) for m in page['recent_messages']) <= 6000
        assert 'hidden reasoning' not in str(page) and not page['gui_attached']
        chunks.extend(m['text'] for m in page['recent_messages'])
        cursor = page['next_cursor']
        if not cursor:
            break
    assert ''.join(chunks) == ''.join(visible), 'Long answers or older turns were skipped/duplicated'
    assert pages == 4
    page = await b.action({'action': 'read', 'thread_id': 'history'}, '77')
    for invalid in ('bad cursor', page['next_cursor'].replace('h1:', 'wrong:'), 'h1:W10'):
        before = len(calls)
        try:
            await b.action({'action': 'read', 'thread_id': 'history', 'cursor': invalid}, '77')
            raise AssertionError('Invalid cursor accepted')
        except ValueError:
            pass
        assert len(calls) == before
    try:
        await b.action({'action': 'read', 'thread_id': 'other', 'cursor': page['next_cursor']}, '77')
        raise AssertionError('Cursor reused for a different chat')
    except ValueError:
        pass
    turns[0]['id'] = 'changed'
    try:
        await b.action({'action': 'read', 'thread_id': 'history', 'cursor': page['next_cursor']}, '77')
        raise AssertionError('Changed page silently accepted')
    except ValueError:
        pass
    print('PASS paged long answers and older turns, bounded output, thread-bound cursors, stale-page rejection')


if __name__ == '__main__':
    asyncio.run(main())
