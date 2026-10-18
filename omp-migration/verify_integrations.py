"""Read-only live integration checks plus captured Discord root-denial checks."""
import asyncio
import contextlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import discord

from evo_omp import context
from evo_omp.tools import Registry, STATE, load_jobs, http_json
from evo_omp.integrations import host, gmail, access, codex


class CapturedMessage:
    id = 1
    async def edit(self, **kwargs):
        pass


class CapturedChannel:
    async def send(self, content=None, view=None, **kwargs):
        if view:
            # Decline the exact newly registered request, never approve one.
            async def deny():
                for _ in range(30):
                    with context._lock:
                        pending = list(context._pending)
                    if pending:
                        key, ident = pending[0]
                        assert context.resolve_approval(key, 'deny', ident) == 1
                        return
                    await asyncio.sleep(.05)
                raise AssertionError('Missing root approval')
            asyncio.create_task(deny())
        return CapturedMessage()


class CapturedClient:
    def is_ready(self): return True
    def get_channel(self, ident): return CapturedChannel()
    async def fetch_channel(self, ident): return CapturedChannel()


async def main():
    r = Registry()
    await r.initialize_web()
    client = CapturedClient()
    context.client = client
    host.discord_factory(client, client)
    turn = context.Turn('990826359704154225')
    token = context.current.set(turn)
    try:
        # Public web reading uses the existing router/Lightpanda integration.
        result = await r.call('web_read', {'url': 'https://example.com'}, turn)
        page = json.loads(result) if isinstance(result, str) else result
        assert page.get('success') and page.get('content'), str(result)[:250]
        print('PASS existing WebUI reader through router HTTP', flush=True)
        status = await asyncio.to_thread(access.system_status)
        assert status['success'] and status['model']
        assert '13077' not in access.temp_text(status)
        print('PASS sensors/router and access socket (read-only)', flush=True)
        actual = await asyncio.to_thread(gmail.perform, {'action': 'status'})
        assert actual.get('success', True) and 'error' not in actual, list(actual)
        print('PASS Gmail IMAP authentication and read-only INBOX status; no mail contents printed', flush=True)
        unprivileged = json.loads(await asyncio.to_thread(host.host_command, {
            'command': 'id -u', 'cwd': '/var/lib/evo-hermes/workspace',
            'run_as': 'evo-hermes', 'reason': 'Verify migrated owner host integration', 'timeout': 10}))
        assert unprivileged.get('success'), unprivileged
        assert str(os.getuid()) in unprivileged.get('output', '')
        print('PASS actual broker unprivileged execution as service UID', flush=True)
        denied = json.loads(await asyncio.to_thread(host.host_command, {
            'command': 'id -u', 'cwd': '/var/lib/evo-hermes/workspace', 'run_as': 'root',
            'reason': 'Verify root refusal; this command must NOT execute', 'timeout': 10}))
        assert not denied.get('success') and denied.get('approval_status') == 'denied_or_expired', denied
        again = json.loads(await asyncio.to_thread(host.host_command, {
            'command': 'true', 'run_as': 'root', 'reason': 'Verify same-turn refusal', 'timeout': 10}))
        assert again.get('approval_status') == 'refused_this_turn', again
        print('PASS real broker staging plus captured Discord Deny and same-turn retry block; no root executed', flush=True)
        rogue = context.Turn(turn.channel, user='1')
        bad = context.current.set(rogue)
        try:
            rejected = json.loads(await asyncio.to_thread(host.host_command, {'command': 'true'}))
            assert not rejected['success']
        finally:context.current.reset(bad)
        print('PASS non-owner rejection', flush=True)
        bridge = codex.Bridge()
        try:
            models = await bridge.action({'action': 'models'}, turn.channel)
            assert models['models']
            projects = await bridge.action({'action': 'projects'}, turn.channel)
            assert 'projects' in projects
            print('PASS authenticated Windows Codex TLS models/projects (no message or new task)', flush=True)
        finally:
            if bridge.ws: await bridge.ws.close()
            if bridge.reader: bridge.reader.cancel()
        source = json.loads(Path('/var/lib/evo-hermes/hermes/cron/jobs.json').read_text())['jobs']
        jobs = load_jobs()
        assert len(jobs) == len(source)
        for a, b in zip(source, jobs):
            for key in ('id', 'prompt', 'schedule', 'enabled', 'next_run_at'):
                assert a.get(key) == b.get(key), key
        print('PASS existing alarm IDs/prompts/schedules/next-run unchanged', flush=True)
        # Token login verifies credentials without opening a second gateway or sending messages.
        env = Path('/etc/evo-omp/discord.env').read_text().splitlines()
        value = next(line.split('=', 1)[1].strip().strip('\"\'') for line in env
                     if line.startswith('DISCORD_BOT_TOKEN='))
        async with discord.Client(intents=discord.Intents.none()) as c:
            await c.login(value)
            assert c.user
            print('PASS Discord token login; no second gateway connection and no test message', flush=True)
    finally:
        context.current.reset(token)
        context.cancel_all()


if __name__ == '__main__':
    asyncio.run(main())
