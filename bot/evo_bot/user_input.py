"""Event-driven owner replies; typing extends the idle deadline without inference."""
import asyncio
import time

import discord
from . import context

IDLE_SECONDS = 300
QUIET_SECONDS = 15
TYPING_SECONDS = 10  # Discord sends typing-start, but no typing-stop event.


class PendingInput:
    def __init__(self):
        self.changed = asyncio.Event()
        self.parts = []
        self.receiving = set()
        self.last_activity = time.monotonic()
        self.typing_until = 0
        self.cancelled = False
        self.resolution = None
        self.done = False

    @property
    def accepting(self):
        return not (self.cancelled or self.resolution or self.done)

    def typing(self):
        self.last_activity = time.monotonic()
        self.typing_until = self.last_activity + TYPING_SECONDS
        self.changed.set()

    def begin_message(self):
        self.last_activity = time.monotonic()
        self.typing_until = 0
        ticket = len(self.parts)
        self.parts.append(None)
        self.receiving.add(ticket)
        self.changed.set()
        return ticket

    def finish_message(self, ticket, text, images):
        parts = [p for p in self.parts if p is not None]
        if len(text) + sum(len(p[0]) for p in parts) > 60000:
            raise ValueError('답변이 너무 깁니다. 60,000자 안으로 나눠주세요.')
        combined = [image for p in parts for image in p[1]] + images
        if len(combined) > 2 or sum(len(i['data']) for i in combined) > 700_000:
            raise ValueError('한 답변에는 이미지 두 장까지, 큰 이미지는 한 장만 보내주세요.')
        self.parts[ticket] = (text, images)
        self.discard_message(ticket)

    def discard_message(self, ticket):
        self.receiving.discard(ticket)
        self.changed.set()

    def cancel(self):
        self.cancelled = True
        self.changed.set()

    def resolve(self, action):
        if not self.accepting:
            raise ValueError('이미 종료된 질문입니다.')
        if action == 'answered' and (not any(p is not None for p in self.parts) or self.receiving):
            raise ValueError('채팅으로 답변을 먼저 보내주세요. 첨부파일이 전송 중이면 잠깐 기다려주세요.')
        self.resolution = action
        self.changed.set()

    async def read(self):
        while True:
            self.changed.clear()
            if self.cancelled:
                raise InterruptedError('Question cancelled')
            if self.resolution == 'skipped':
                self.done = True
                return {'status': 'skipped', 'answered': False, 'permission_granted': False,
                        'next_action': 'stop_affected_task'}
            parts = [p for p in self.parts if p is not None]
            deadline = max(self.last_activity, self.typing_until) + IDLE_SECONDS
            if parts and not self.receiving:
                deadline = max(self.last_activity, self.typing_until) + QUIET_SECONDS
            remaining = deadline - time.monotonic()
            if remaining <= 0 or self.resolution == 'answered':
                self.done = True
                if not parts or self.receiving:
                    return {'status': 'timeout', 'answered': False, 'permission_granted': False,
                            'next_action': 'stop_affected_task', 'idle_seconds': IDLE_SECONDS}
                return {'status': 'answered', 'answered': True,
                        'answer': '\n\n'.join(p[0] for p in parts if p[0]),
                        'images': [image for p in parts for image in p[1]]}
            try:
                await asyncio.wait_for(self.changed.wait(), remaining)
            except asyncio.TimeoutError:
                pass


class QuestionView(discord.ui.View):
    def __init__(self, pending, is_current):
        # PendingInput owns the activity-based deadline; a fixed View timeout would conflict.
        super().__init__(timeout=None)
        self.pending, self.is_current = pending, is_current

    async def interaction_check(self, interaction):
        if str(interaction.user.id) != context.OWNER:
            await interaction.response.send_message('오너만 답변할 수 있습니다.', ephemeral=True)
            return False
        if not self.is_current() or not self.pending.accepting:
            await interaction.response.send_message('이미 종료된 질문입니다.', ephemeral=True)
            return False
        return True

    async def resolve(self, interaction, action):
        try:
            self.pending.resolve(action)
        except ValueError as error:
            self.pending.last_activity = time.monotonic()
            self.pending.changed.set()
            await interaction.response.send_message(str(error), ephemeral=True)
        else:
            await interaction.response.defer()

    @discord.ui.button(label='답변 전달 · Flush', style=discord.ButtonStyle.primary)
    async def flush(self, interaction, button):
        await self.resolve(interaction, 'answered')

    @discord.ui.button(label='건너뛰기', style=discord.ButtonStyle.secondary)
    async def skip(self, interaction, button):
        await self.resolve(interaction, 'skipped')
