"""Keep workspace work in bwrap; provide host commands only to the owner on Discord."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import socket
import threading
import time
import unicodedata
from collections import OrderedDict
from urllib.parse import unquote, urljoin, urlsplit

OWNER = '543004181842362379'
WORKSPACE = Path('/var/lib/evo-hermes/workspace')
SOCKET = '/run/evo-host/broker.sock'
CLIENT = LOOP = None
HOST_LOCK = threading.Lock()
DOWNLOAD_LOCK = threading.Lock()
ROOT_REFUSALS = OrderedDict()
ROOT_EXECUTIONS = OrderedDict()


def root_turn_refused(session_key, turn_id):
    return (session_key, turn_id) in ROOT_REFUSALS


def remember_root_refusal(session_key, turn_id):
    # No output or credential is retained; only bounded per-turn identifiers.
    ROOT_REFUSALS[(session_key, turn_id)] = True
    while len(ROOT_REFUSALS) > 256:
        ROOT_REFUSALS.popitem(last=False)


def root_command_key(session_key, turn_id, args):
    body = json.dumps([args.get('command', ''), args.get('cwd', '')], ensure_ascii=False)
    return session_key, turn_id, hashlib.sha256(body.encode()).hexdigest()


def remember_root_execution(key):
    ROOT_EXECUTIONS[key] = True
    while len(ROOT_EXECUTIONS) > 256:
        ROOT_EXECUTIONS.popitem(last=False)


def owner_present():
    from ..context import owner_message
    return owner_message() is not None


def result(value):
    return json.dumps(value, ensure_ascii=False)


def rpc(payload, interrupted=None):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
        peer.settimeout(3)
        peer.connect(SOCKET)
        peer.sendall((json.dumps(payload, ensure_ascii=False) + '\n').encode('utf-8'))
        peer.settimeout(0.5)
        body = bytearray()
        deadline = time.monotonic() + 135
        while b'\n' not in body:
            if interrupted is not None and interrupted():
                raise InterruptedError('Host command cancelled')
            if time.monotonic() >= deadline:
                raise TimeoutError('Host broker deadline exceeded')
            try:
                chunk = peer.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                raise ConnectionError('Host broker connection closed')
            body.extend(chunk)
            if len(body) > 256 * 1024:
                raise ValueError('Host broker response exceeded its limit')
        return json.loads(body)


def discord_factory(native, adapter):
    global CLIENT, LOOP
    CLIENT, LOOP = native, asyncio.get_running_loop()


def discord_connected():
    return (CLIENT is not None and LOOP is not None and LOOP.is_running()
            and not LOOP.is_closed() and CLIENT.is_ready())


async def post_root_approval(data, session_key, chat_id):
    """Owner-only buttons resolve this exact request, never another queued tool."""
    import discord
    from .. import context as approval
    from ..context import redact_sensitive_text as _redact_approval_command

    class RootApprovalView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=300)
            self.message = None
            self.finished = False
            self.label = None

        async def interaction_check(self, interaction):
            if str(interaction.user.id) != OWNER:
                await interaction.response.send_message('Only the owner can approve root access.',
                                                        ephemeral=True)
                return False
            return True

        async def finish(self, label):
            if self.finished:
                return
            self.finished = True
            self.label = label
            for child in self.children:
                child.disabled = True
            self.stop()
            if self.message is not None:
                await self.message.edit(content=preview + '\n\n' + label, view=self,
                                        allowed_mentions=discord.AllowedMentions.none())

        async def resolve(self, interaction, choice):
            await interaction.response.defer()
            count = approval.resolve_approval(session_key, choice,
                                                       request_id=data['request_id'])
            label = ('Approved once.' if choice == 'once' else 'Denied.') if count \
                    else 'Expired or cancelled; command was not run.'
            await self.finish(label)

        @discord.ui.button(label='Allow Once', style=discord.ButtonStyle.green)
        async def allow_once(self, interaction, button):
            await self.resolve(interaction, 'once')

        @discord.ui.button(label='Deny', style=discord.ButtonStyle.red)
        async def deny(self, interaction, button):
            await self.resolve(interaction, 'deny')

        async def on_timeout(self):
            approval.withdraw_approval(session_key, data['request_id'])
            await self.finish('Expired; command was not run.')

    command = _redact_approval_command(data['command']).replace('```', "'''")
    reason = _redact_approval_command(data['description']).replace('```', "'''")
    preview = '**Root command — approve this execution only**\n```bash\n' \
              + command + '\n```\n' + reason[:220]
    if len(preview) > 1850:
        raise ValueError('Root command is too long to display in full for approval')
    channel = CLIENT.get_channel(int(chat_id)) or await CLIENT.fetch_channel(int(chat_id))
    view = RootApprovalView()
    view.message = await channel.send(content=preview, view=view,
                                      allowed_mentions=discord.AllowedMentions.none())
    # A very fast click can resolve while the message POST still awaits its ack.
    if view.finished:
        await view.message.edit(content=preview + '\n\n' + view.label, view=view,
                                allowed_mentions=discord.AllowedMentions.none())
    return view


def host_command(args, **kwargs):
    if not owner_present():
        return result(dict(success=False, error='Host commands require the owner Discord session'))
    if not discord_connected():
        return result(dict(success=False, error='Discord is not connected'))
    if not HOST_LOCK.acquire(blocking=False):
        return result(dict(success=False, error='Another host command is running'))
    ticket = None
    try:
        from .. import context as approval
        from ..context import live_owner, is_background
        from ..context import is_interrupted
        from ..context import redact_sensitive_text
        if is_background() or is_interrupted():
            return result(dict(success=False, error='No unattended or interrupted host execution'))
        turn = live_owner()
        if turn is None:
            return result(dict(success=False, error='No live Discord owner session'))
        session_key, turn_id = turn.key, turn.id
        if args.get('run_as', 'evo-hermes') == 'root':
            if not turn_id:
                return result(dict(success=False, retryable=False,
                    error='Root execution requires a live user turn identifier'))
            if root_turn_refused(session_key, turn_id):
                return result(dict(success=False, retryable=False, approval_status='refused_this_turn',
                    error='Root approval was denied or expired this turn. No command ran. '
                          'Do not request root again until a new owner message. Sandbox work is still available.'))
        payload = {key: args[key] for key in ('command', 'reason', 'cwd', 'run_as', 'timeout')
                   if key in args}
        ticket = rpc(dict(action='stage', payload=payload))
        if not ticket.get('success'):
            return result(ticket)
        # Root always asks the live owner. Do not consult YOLO, session grants,
        # saved patterns, or an automatic reviewer for this privileged path.
        if ticket['payload']['run_as'] == 'root':
            execution_key = root_command_key(session_key, turn_id, ticket['payload'])
            if execution_key in ROOT_EXECUTIONS:
                return result(dict(success=False, retryable=False, approval_status='duplicate_execution',
                    error='This root command already executed this turn. No new approval or execution occurred. '
                          'Use its existing result; a fresh execution needs a new owner turn.'))
            from ..context import wait_for_approval
            chat_id = turn.channel
            if not chat_id or not str(chat_id).isdigit():
                return result(dict(success=False, error='No live Discord approval destination'))

            root_view = None

            def notify_root(data):
                nonlocal root_view
                future = asyncio.run_coroutine_threadsafe(
                    post_root_approval(data, session_key, chat_id), LOOP)
                try:
                    root_view = future.result(timeout=15)
                except Exception:
                    future.cancel()
                    raise

            decision = wait_for_approval(session_key, notify_root, {
                'command': '# EVO root; cwd=' + ticket['payload']['cwd'] + '\n'
                           + ticket['payload']['command'],
                'description': 'Root access: ' + ticket['payload']['reason']
                               + ' (this command only)',
            })
            approved = decision.get('resolved') and decision.get('choice') == 'once' \
                       and not decision.get('cancelled')
            if root_view is not None:
                # /approve resolves the same native queue without clicking this
                # card; close it too so it cannot later claim a false timeout.
                label = 'Approved once.' if approved else 'Not approved; command was not run.'
                update = asyncio.run_coroutine_threadsafe(root_view.finish(label), LOOP)
                try:
                    update.result(timeout=5)
                except Exception:
                    update.cancel()
                    raise
            if not approved:
                remember_root_refusal(session_key, turn_id)
                return result(dict(success=False,
                    retryable=False, approval_status='denied_or_expired',
                    error='Root command was not approved once by the Discord owner; not executed. '
                          'Do not request root again until a new owner message. Sandbox work is still available.'))
        def cancelled():
            return is_interrupted() or not discord_connected()
        if cancelled():
            return result(dict(success=False, error='Host command cancelled before execution'))
        if ticket['payload']['run_as'] == 'root':
            remember_root_execution(execution_key)
        answer = rpc(dict(action='execute', request_id=ticket['request_id'],
                          token=ticket['token'], digest=ticket['digest']), cancelled)
        ticket = None  # Consumed atomically before execution, including failures.
        answer['output'] = redact_sensitive_text(answer.get('output', ''))[:12000]
        return result(answer)
    except Exception as error:
        return result(dict(success=False, error=str(error)[:250]))
    finally:
        if ticket is not None and ticket.get('success'):
            try:
                rpc(dict(action='cancel', request_id=ticket['request_id'],
                         token=ticket['token'], digest=ticket['digest']))
            except (OSError, ValueError, ConnectionError):
                pass  # Already consumed or broker gone: the ticket cannot execute here.
        HOST_LOCK.release()


def register(ctx):
    ctx.register_platform_handler('discord', discord_factory)
    ctx.register_tool('host_command', 'evo_host', {
        'name': 'host_command',
        'description': 'Run a command on the EVO host outside bwrap, only in the authenticated owner Discord '
                       'session. Unprivileged commands execute directly; run_as=root ALWAYS requires '
                       'a new Allow Once approval in Discord, including apt update. Session/permanent '
                       'approval never applies to root. Use carefully: host changes persist. '
                       'Use bash for ordinary /workspace work. Default unprivileged; select run_as=root '
                       'instead of sudo when administrator access is needed. Approved root commands use '
                       'the real host filesystem and normal root capabilities without bwrap or path masks. '
                       'APT may use its normal _apt download sandbox. Ordinary host jobs remain restricted '
                       'and see masked placeholders for protected files such as /home/msh/mshsrv. '
                       'Use credentials internally; never print them. Read execution_context before '
                       'interpreting file metadata. Each job has a 512 MiB/no-swap resource limit and '
                       'a maximum 120-second timeout. Background children end with the command.',
        'parameters': {'type': 'object', 'properties': {
            'command': {'type': 'string', 'maxLength': 1000},
            'reason': {'type': 'string', 'maxLength': 160},
            'cwd': {'type': 'string', 'default': '/workspace'},
            'run_as': {'type': 'string', 'enum': ['evo-hermes', 'root'], 'default': 'evo-hermes'},
            'timeout': {'type': 'integer', 'minimum': 1, 'maximum': 120, 'default': 60}},
            'required': ['command', 'reason']}}, host_command)
