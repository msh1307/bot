"""Keep workspace work in bwrap; provide host commands only to the owner on Discord."""
import asyncio
import hashlib
import json
import logging
from pathlib import Path
import socket
import threading
import time
from collections import OrderedDict

OWNER = '543004181842362379'
WORKSPACE = Path('/var/lib/evo-bot/workspace')
SOCKET = '/run/evo-host/broker.sock'
CLIENT = LOOP = None
HOST_LOCK = threading.Lock()
ROOT_REFUSALS = OrderedDict()
ROOT_EXECUTIONS = OrderedDict()
LOG = logging.getLogger(__name__)


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


def rpc(payload, interrupted=None, *, timeout=135):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
        peer.settimeout(3)
        peer.connect(SOCKET)
        peer.sendall((json.dumps(payload, ensure_ascii=False) + '\n').encode('utf-8'))
        peer.settimeout(0.5)
        body = bytearray()
        deadline = time.monotonic() + timeout
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
    not_run = 'password was not disclosed.' if data.get('kind') == 'credential_read' else 'command was not run.'

    class RootApprovalView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=300)
            self.message = None
            self.finished = False
            self.label = None

        async def interaction_check(self, interaction):
            if str(interaction.user.id) != OWNER:
                await interaction.response.send_message('Only the owner can approve this action.',
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
                    else 'Expired or cancelled; ' + not_run
            await self.finish(label)

        @discord.ui.button(label='Allow Once', style=discord.ButtonStyle.green)
        async def allow_once(self, interaction, button):
            await self.resolve(interaction, 'once')

        @discord.ui.button(label='Deny', style=discord.ButtonStyle.red)
        async def deny(self, interaction, button):
            await self.resolve(interaction, 'deny')

        async def on_timeout(self):
            approval.withdraw_approval(session_key, data['request_id'])
            await self.finish('Expired; ' + not_run)

    command = _redact_approval_command(data['command']).replace('```', "'''")
    reason = _redact_approval_command(data['description']).replace('```', "'''")
    title = data.get('title', 'Root approval')
    preview = '**' + title + '**\n```bash\n' \
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


def approve_once(turn, command, description, execution_key, kind='root'):
    """Exact owner approval; no session lease and no secret in the preview."""
    from ..context import live_owner, wait_for_approval
    if live_owner() is not turn or not discord_connected() or not turn.id or turn.cancelled.is_set():
        return dict(success=False, error='Approval requires a connected live owner turn')
    refusal_key = turn.key if kind == 'root' else kind + ':' + turn.key
    if root_turn_refused(refusal_key, turn.id):
        return dict(success=False, retryable=False, approval_status='refused_this_turn',
                    error='Approval was denied or expired this turn. Wait for a new owner message.')
    if execution_key in ROOT_EXECUTIONS:
        return dict(success=False, retryable=False, approval_status='duplicate_execution',
                    error='Already executed this turn; use the existing result.')
    root_view = None

    def notify(data):
        nonlocal root_view
        future = asyncio.run_coroutine_threadsafe(post_root_approval(data, turn.key, turn.channel), LOOP)
        try:
            root_view = future.result(timeout=15)
        except Exception:
            future.cancel()
            raise

    data = {'command': command, 'description': description}
    if kind == 'credential':
        data['title'] = 'Secret entry — approve this input only'
    elif kind == 'credential_read':
        data.update(kind=kind, title='비밀번호 공개 — 이번 조회만 승인')
    decision = wait_for_approval(turn.key, notify, data)
    approved = decision.get('resolved') and decision.get('choice') == 'once' and not decision.get('cancelled')
    if root_view is not None:
        label = 'Approved once.' if approved else ('Not approved; password was not disclosed.'
                    if kind == 'credential_read' else 'Not approved; command was not run.')
        update = asyncio.run_coroutine_threadsafe(root_view.finish(label), LOOP)
        try:
            update.result(timeout=5)
        except Exception:
            update.cancel()
            raise
    if not approved:
        remember_root_refusal(refusal_key, turn.id)
        return dict(success=False, retryable=False, approval_status='denied_or_expired',
                    error='Not approved; command not run. Wait for a new owner message.')
    if turn.cancelled.is_set() or not discord_connected():
        return dict(success=False, error='Approved action cancelled before execution')
    return None


def approve_root_once(turn, command, description, execution_key):
    return approve_once(turn, command, description, execution_key)


def root_gate_policy():
    from . import remote_config
    return remote_config.host_root_gate()


def root_summary(scope, status, reason, command):
    reason = ' '.join(str(reason).split())
    command = ' '.join(str(command).split())
    if len(reason) > 80:
        reason = reason[:80] + f'…(+{len(reason) - 80}자)'
    if len(command) > 200:
        command = command[:200] + f'…(+{len(command) - 200}자)'
    return f'[{scope}] {status} | {reason} | {command}'


async def _post_root_notice(turn, message):
    import discord
    from ..context import redact_sensitive_text
    channel = CLIENT.get_channel(int(turn.channel)) or await CLIENT.fetch_channel(int(turn.channel))
    return await channel.send(redact_sensitive_text(message),
                              allowed_mentions=discord.AllowedMentions.none())


def notify_root(turn, message):
    if not discord_connected():
        raise InterruptedError('Discord disconnected before root notice')
    future = asyncio.run_coroutine_threadsafe(_post_root_notice(turn, message), LOOP)
    try:
        delivered = future.result(timeout=15)
        LOG.info('root_notice_sent message_id=%s', getattr(delivered, 'id', None))
        return delivered
    except Exception:
        future.cancel()
        raise


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
        root_gate = root_gate_policy() if args.get('run_as', 'evo-bot') == 'root' else None
        if args.get('run_as', 'evo-bot') == 'root':
            if not turn_id:
                return result(dict(success=False, retryable=False,
                    error='Root execution requires a live user turn identifier'))
            if root_gate == 'approval_card' and root_turn_refused(session_key, turn_id):
                return result(dict(success=False, retryable=False, approval_status='refused_this_turn',
                    error='Root approval denied or expired; command not run. Wait for a new owner message.'))
        payload = {key: args[key] for key in ('command', 'reason', 'cwd', 'run_as', 'timeout')
                   if key in args}
        ticket = rpc(dict(action='stage', payload=payload))
        if not ticket.get('success'):
            return result(ticket)
        if ticket['payload']['run_as'] == 'root':
            execution_key = root_command_key(session_key, turn_id, ticket['payload'])
            if root_gate == 'approval_card':
                refusal = approve_root_once(turn,
                    '# EVO root; cwd=' + ticket['payload']['cwd'] + '\n' + ticket['payload']['command'],
                    'Reason: ' + ticket['payload']['reason'], execution_key)
                if refusal:
                    return result(refusal)
            elif execution_key in ROOT_EXECUTIONS:
                return result(dict(success=False, retryable=False,
                                   error='This root command already executed this turn.'))
        def cancelled():
            return is_interrupted() or not discord_connected()
        if cancelled():
            return result(dict(success=False, error='Host command cancelled before execution'))
        if ticket['payload']['run_as'] == 'root':
            remember_root_execution(execution_key)
        executed_payload = ticket['payload']
        answer = rpc(dict(action='execute', request_id=ticket['request_id'],
                          token=ticket['token'], digest=ticket['digest']), cancelled,
                     timeout=ticket['payload']['timeout'] + 15)
        ticket = None  # Consumed atomically before execution, including failures.
        answer['output'] = redact_sensitive_text(answer.get('output', ''))
        if executed_payload['run_as'] == 'root' and root_gate == 'notify_only':
            try:
                notify_root(turn, root_summary('host', f"exit={answer.get('exit_code')}",
                                               executed_payload['reason'], executed_payload['command']))
            except Exception:
                LOG.exception('host_root_notice_failed turn=%s', turn_id)
                answer['discord_notice'] = 'delivery_failed_after_execution; do_not_retry'
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
