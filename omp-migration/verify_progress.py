"""Captured Discord status lifecycle checks; no Discord/model/host requests."""
import asyncio
from contextlib import suppress
from types import SimpleNamespace
from unittest.mock import patch

import discord
from evo_omp import gateway
from evo_omp.context import OWNER
from verify_gateway import CaptureBot


def http_error():
    return discord.HTTPException(SimpleNamespace(status=503, reason='verification'), 'captured failure')


class Notice:
    def __init__(self, channel):
        self.channel, self.edits, self.deleted = channel, [], False

    async def edit(self, *, content, **kwargs):
        assert not kwargs['allowed_mentions'].everyone
        if self.channel.fail == 'edit':
            raise http_error()
        self.edits.append(content)

    async def delete(self):
        self.deleted = True
        self.channel.events.append('deleted')
        if self.channel.fail == 'delete':
            raise http_error()


class Channel:
    id = 77
    def __init__(self, fail=None):
        self.fail = fail
        self.messages, self.notices, self.events = [], [], []
        self.sent = asyncio.Event()

    async def send(self, text, **kwargs):
        assert not kwargs['allowed_mentions'].everyone
        self.messages.append(text)
        if self.fail == 'send':
            raise http_error()
        self.events.append(text)
        notice = Notice(self)
        self.notices.append(notice)
        self.sent.set()
        return notice

    def typing(self):
        from contextlib import asynccontextmanager
        @asynccontextmanager
        async def typing(): yield
        return typing()


async def main():
    session = SimpleNamespace(tools=2)
    with patch.object(gateway, 'PROGRESS_INTERVAL', .01):
        channel = Channel()
        async with gateway.Bot.progress(None, channel, session):
            await asyncio.sleep(0)
        assert not channel.messages
        async with gateway.Bot.progress(None, channel, session, enabled=False):
            await asyncio.sleep(.03)
        assert not channel.messages
        print('PASS short turns and scheduled turns produce no status message')

        async with gateway.Bot.progress(None, channel, session):
            await asyncio.wait_for(channel.sent.wait(), 1)
            session.tools = 7
            await asyncio.sleep(.04)
        assert len(channel.messages) == 1
        assert len(channel.notices[0].edits) >= 2
        assert '도구 7회' in channel.notices[0].edits[-1]
        assert all('작업 중' in t for t in [channel.messages[0], *channel.notices[0].edits])
        assert channel.notices[0].deleted
        edits = len(channel.notices[0].edits)
        await asyncio.sleep(.02)
        assert len(channel.notices[0].edits) == edits
        print('PASS a long turn edits one temporary status and leaves no updater behind')

        for failure in ('send', 'edit', 'delete'):
            channel = Channel(failure)
            async with gateway.Bot.progress(None, channel, session):
                await asyncio.sleep(.05)
            assert len(channel.messages) == 1
            if channel.notices:
                assert channel.notices[0].deleted
        print('PASS Discord status failures do not fail or repeatedly spam the model turn')

        for fail in (False, True):
            channel = Channel()
            try:
                async with gateway.Bot.progress(None, channel, session):
                    await asyncio.wait_for(channel.sent.wait(), 1)
                    if fail:
                        raise ValueError('captured task failure')
            except ValueError:
                assert fail
            assert channel.notices[0].deleted
        print('PASS normal completion and task failure both remove temporary status')

        channel = Channel()
        async def pending():
            async with gateway.Bot.progress(None, channel, session):
                await asyncio.Future()
        task = asyncio.create_task(pending())
        await asyncio.wait_for(channel.sent.wait(), 1)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        assert channel.notices[0].deleted
        print('PASS cancellation removes temporary status and cancels its updater')

        bot = CaptureBot()
        bot.channel = Channel()
        worker = asyncio.create_task(bot.work_loop())
        def message(text):
            return SimpleNamespace(author=SimpleNamespace(bot=False, id=int(OWNER)),
                content=text, channel=bot.channel, attachments=[], id=1)
        try:
            await bot.on_message(message('long'))
            await asyncio.wait_for(bot.channel.sent.wait(), 1)
            await bot.on_message(message('correction'))
            await asyncio.wait_for(bot.queue.join(), 2)
            assert bot.fake.inputs == ['long', 'correction']
            assert bot.channel.notices[0].deleted
            assert bot.channel.messages[-1] == 'answer:correction'
            assert 'answer:long' not in bot.channel.messages
            assert bot.channel.events.index('deleted') < bot.channel.events.index('answer:correction')
            assert not worker.done()
        finally:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
        print('PASS actual worker interruption removes old status before the corrected answer')


if __name__ == '__main__':
    asyncio.run(main())
