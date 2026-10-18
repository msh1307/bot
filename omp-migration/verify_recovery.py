"""Conservative session admission checks. No external messages or commands."""
import asyncio
from types import SimpleNamespace

from evo_omp.rpc import OmpSession


async def main():
    s = OmpSession('recovery-check', 'verification', [], None)
    s.process = SimpleNamespace(returncode=None)
    s.reader = asyncio.get_running_loop().create_future()
    s.settled.set()
    safe = dict(isSettled=True, isStreaming=False, isCompacting=False,
                hasPendingAsyncWork=False, queuedMessageCount=0)
    state = dict(safe)
    calls = []

    async def request(kind, **kwargs):
        calls.append(kind)
        return state

    s.request = request
    assert await s.reusable_after_error()
    for key, value in [('isSettled', False), ('isStreaming', True),
                       ('isCompacting', True), ('hasPendingAsyncWork', True),
                       ('queuedMessageCount', 1), ('isSettled', None)]:
        state = dict(safe, **{key: value})
        assert not await s.reusable_after_error(), key
    state = dict(safe)
    for attr, value in [('host_jobs', {'unresolved': object()}),
                        ('prompts', {'unresolved': object()}),
                        ('pending', {'unresolved': object()}),
                        ('transport_error', 'reader failure'), ('compacting', True)]:
        original = getattr(s, attr)
        setattr(s, attr, value)
        assert not await s.reusable_after_error(), attr
        setattr(s, attr, original)
    s.reader.set_result(None)
    assert not s.transport_alive
    assert not await s.reusable_after_error()
    assert set(calls) == {'get_state'}, calls
    print('PASS settled/streaming/compaction/async/queue/RPC/host-job admission; no replay')


if __name__ == '__main__':
    asyncio.run(main())
