"""Route the unmodified terminal-use MCP server through existing owner/SSH access."""
import contextlib
import copy
import json
import os
from pathlib import Path
import re
import secrets
import selectors
import shlex
import subprocess
import tempfile
import threading
import time

from .. import context, rpc
from . import browser, ssh

ROOT = Path('/opt/evo-bot')
SERVER = ROOT / 'bin/terminal-use/node_modules/terminal-use/bin/terminal-use.js'
SHELL = ROOT / 'terminal-shell.py'
ACTIONS = ('create', 'list', 'type', 'press', 'wait', 'read', 'batch', 'destroy',
           'resize', 'reset', 'screenshot', 'click', 'scroll')
GUIDE = {name: ({} if name in ('create', 'list') else
               {'required': {'sessionId': 'handle returned by create'}}) for name in ACTIONS}
GUIDE.update({
    'create': {'optional': {'command': 'program to run; omit for a persistent shell',
                           'cwd': 'working directory on the selected instance'}},
    'type': {'required': {'sessionId': 'handle', 'text': 'literal input; \\n presses Enter'}},
    'press': {'required': {'sessionId': 'handle', 'key': 'Enter, Ctrl+C, ArrowUp, ...'}},
    'read': {'required': {'sessionId': 'handle'}, 'optional': {'rows': 'lines per page', 'page': '0=newest'}},
    'wait': {'required': {'sessionId': 'handle'}, 'optional': {
        'pattern': 'regex for the required state', 'timeoutMs': '0..600000; timeout leaves the program running'}},
    'batch': {'required': {'sessionId': 'handle', 'actions': 'array of type/paste/press/wait actions'}},
    'notes': 'Always select instance: localhost is the /workspace sandbox; SSH IDs use existing credentials. '
             'Use create(command=...) for installs/programs, then wait; omit command for an interactive shell. '
             'SSH shell waits use quiet, which is NOT completion; use a pattern or a command session. '
             'read pages output; batch combines input. Root commands still use exec(root=true,reason=...). '
             'list shows elapsed time. Destroy finished/unneeded sessions; review callbacks arrive at 2h and 3h. '
             'help args={tool:ACTION} returns original parameters. Sessions last across turns, not bot restarts.'})
CLIENTS = {}
CLIENTS_LOCK = threading.Lock()


class Terminal(browser.Browser):
    def __init__(self, instance):
        super().__init__()
        self.instance = instance
        self.profile = None
        self.schemas, self.sessions = {}, {}
        self.generation = secrets.token_hex(3)

    def close(self):
        with self.lock:
            super().close()
            self.sessions.clear()
            if self.profile:
                self.profile.cleanup()
                self.profile = None

    def notify(self, method, params):
        self.process.stdin.write((json.dumps({'jsonrpc': '2.0', 'method': method,
                                             'params': params}) + '\n').encode())
        self.process.stdin.flush()

    def request(self, method, params, turn, timeout=35):
        self.serial += 1
        ident = self.serial
        self.process.stdin.write((json.dumps({'jsonrpc': '2.0', 'id': ident,
            'method': method, 'params': params}, ensure_ascii=False) + '\n').encode())
        self.process.stdin.flush()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if turn.cancelled.is_set():
                with contextlib.suppress(OSError):
                    self.notify('notifications/cancelled', {'requestId': ident, 'reason': 'Owner interrupted'})
                raise InterruptedError('Terminal call interrupted; the session remains open. Inspect before retrying.')
            if b'\n' in self.buffer:
                line, _, rest = self.buffer.partition(b'\n')
                self.buffer = bytearray(rest)
                frame = json.loads(line)
                if frame.get('id') != ident:
                    continue
                if 'error' in frame:
                    raise ValueError(frame['error'].get('message', 'Terminal request failed'))
                result = frame.get('result', {})
                if result.get('isError'):
                    raise ValueError('\n'.join(b.get('text', '') for b in result.get('content', [])))
                return result
            if not self.selector.select(.1):
                continue
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise ConnectionError('Terminal server exited; sessions were lost. No action was replayed.')
            self.buffer.extend(chunk)
            if len(self.buffer) > browser.MAX_FRAME:
                raise ConnectionError('Terminal response exceeded 4 MiB')
        with contextlib.suppress(OSError):
            self.notify('notifications/cancelled', {'requestId': ident, 'reason': 'Transport timeout'})
        raise TimeoutError('Terminal request timed out; inspect the session before retrying. No replay.')

    def start(self, turn):
        if self.running():
            return
        self.close()
        if not SERVER.is_file():
            raise FileNotFoundError('Install terminal-use@0.2.1 under /opt/evo-bot/bin/terminal-use first')
        self.generation = secrets.token_hex(3)
        command = ['/usr/bin/node', str(SERVER), '--cols', '80', '--rows', '20', '--scrollback', '200']
        env = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'HOME': '/var/lib/evo-bot',
               'PYTHONDONTWRITEBYTECODE': '1'}
        if self.instance == 'localhost':
            self.profile = tempfile.TemporaryDirectory(prefix='terminal-', dir=str(rpc.RUNTIME))
            profile = Path(self.profile.name)
            (profile / 'tmp').mkdir()
            command = rpc.sandbox_argv(profile, '', command=['/usr/bin/env', '-u', 'PI_NO_PTY',
                '-u', 'PI_NO_TITLE', '-u', 'PI_CODING_AGENT_DIR', *command])
        try:
            self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, start_new_session=True, bufsize=0, env=env)
            self.selector = selectors.DefaultSelector()
            self.selector.register(self.process.stdout, selectors.EVENT_READ)
            self.request('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {},
                'clientInfo': {'name': 'evo-bot', 'version': '1'}}, turn)
            self.notify('notifications/initialized', {})
            tools = self.request('tools/list', {}, turn)['tools']
            self.schemas = {t['name'].removeprefix('terminal_'): t for t in tools}
        except BaseException:
            self.close()
            raise

    def call(self, action, args, turn):
        with self.lock:
            if action not in ('create', 'list', 'help'):
                handle = args.get('sessionId')
                if not isinstance(handle, str) or not self.running() or handle not in self.sessions:
                    raise ValueError('Unknown/expired sessionId for this instance; list or create a session. No fallback.')
            if action == 'list' and not self.running():
                return {'content': [{'type': 'text', 'text': 'No open terminal sessions.'}]}
            if action != 'help' or not self.schemas:
                self.start(turn)
            if action == 'help':
                try:
                    schema = copy.deepcopy(self.schemas[args['tool']]['inputSchema'])
                    if 'sessionId' in schema.get('properties', {}):
                        schema['properties']['sessionId'] = {'type': 'string',
                            'description': 'Handle returned by create on this instance.'}
                    return schema
                finally:
                    if not self.sessions:
                        self.close()
            if set(args) - set(self.schemas[action]['inputSchema'].get('properties', {})):
                raise ValueError('Unknown terminal arguments; use help with args={tool:ACTION}')
            params = dict(args)
            handle = params.get('sessionId')
            if handle is not None:
                params['sessionId'] = self.sessions[handle]['id']
            if action == 'create' and self.instance != 'localhost':
                options = {k: params.pop(k) for k in ('cwd', 'env', 'shell') if k in params}
                params.update(shell=str(SHELL), cwd=str(rpc.WORKSPACE), env={
                    'EVO_TERMINAL_INSTANCE': self.instance,
                    'EVO_TERMINAL_OPTIONS': json.dumps(options)})
            if (action == 'wait' and self.instance != 'localhost' and not self.sessions[handle]['command']
                    and not params.get('pattern') and 'until' not in params):
                params['until'] = 'quiet'
            timeout = 15 + params.get('timeoutMs', 30000) / 1000
            if action == 'batch':
                # Native batches wait per action, not via a top-level timeoutMs.
                timeout += sum((a.get('timeoutMs', 5000) if 'pattern' in a else a.get('ms', 0)) / 1000
                    if a.get('type') == 'wait' else 2 for a in params.get('actions', []))
            result = self.request('tools/call', {'name': 'terminal_' + action, 'arguments': params}, turn, timeout)
            if action == 'create':
                text = '\n'.join(b.get('text', '') for b in result.get('content', []))
                match = re.search(r'Created session (\d+)\b', text)
                if not match:
                    raise RuntimeError('Terminal did not return a session ID; inspect before retrying')
                handle = self.generation + '-' + match[1]
                self.sessions[handle] = {'id': int(match[1]), 'command': bool(args.get('command')),
                    'created': time.monotonic(), 'channel': turn.channel, 'notice': 0}
                result['content'].append({'type': 'text', 'text': f'Use sessionId="{handle}", instance="{self.instance}".'})
            elif action == 'destroy':
                del self.sessions[handle]
                if not self.sessions:
                    self.close()
            if action in ('create', 'list'):
                handles = {str(v['id']): k for k, v in self.sessions.items()}
                for block in result.get('content', []):
                    if block.get('type') != 'text':
                        continue
                    block['text'] = re.sub(r'(?m)^(Created session |\[)(\d+)(?=[\] (.])',
                        lambda m: m[1] + handles.get(m[2], m[2]), block['text'])
                    if self.instance != 'localhost':
                        block['text'] = re.sub(r'(?m)^(\[[^\n]+)$', lambda m: m[0]
                            .replace(' pid=', ' transport_pid=').replace(' shell=', ' launcher_shell=')
                            .replace(' cwd=', ' launcher_cwd='), block['text'])
                    if action == 'list':
                        for public, meta in self.sessions.items():
                            block['text'] = re.sub(r'(?m)^(\[' + re.escape(public) + r'\][^\n]*)$',
                                lambda m: m[1] + ' elapsed=' + elapsed(meta['created']), block['text'])
            return result


def elapsed(created):
    seconds = max(0, int(time.monotonic() - created))
    return (f'{seconds // 3600}h{seconds % 3600 // 60:02}m' if seconds >= 3600
            else f'{seconds // 60}m{seconds % 60:02}s')


def notify_due(bot):
    """Two bounded reviews via the existing queue; no terminal polling or new timer."""
    groups = {}
    with CLIENTS_LOCK:
        clients = list(CLIENTS.values())
    for item in clients:
        if not item.lock.acquire(blocking=False):
            continue
        try:
            if not item.running():
                continue
            for handle, meta in item.sessions.items():
                age = time.monotonic() - meta['created']
                milestone = 3 if age >= 10800 else 2 if age >= 7200 else 0
                if milestone > meta['notice']:
                    groups.setdefault(meta['channel'], []).append((item, handle, meta, milestone))
        finally:
            item.lock.release()
    for channel, entries in groups.items():
        def valid(entries=entries):
            return any(c.running() and c.sessions.get(h) is m and m['notice'] <= n for c, h, m, n in entries)
        text = ('[Terminal review] List these sessions first; destroy exited/unneeded ones, keep needed running work. '
                'Do not restart/replay tasks. Briefly report elapsed time and state.\n' + '\n'.join(
                    f'instance={c.instance} sessionId={h} elapsed={elapsed(m["created"])}' for c, h, m, _ in entries))
        if bot.enqueue_codex_callback(channel, text, bot.codex_callback_scope(channel), valid):
            for _, _, meta, milestone in entries:
                meta['notice'] = milestone


def client(instance):
    with CLIENTS_LOCK:
        if instance not in CLIENTS:
            CLIENTS[instance] = Terminal(instance)
        return CLIENTS[instance]


def control(instance, action, args):
    turn = context.owner_message()
    if turn is None:
        raise PermissionError('Interactive terminals require an owner Discord message')
    if instance != 'localhost' and instance not in ssh.configuration()['servers']:
        raise ValueError('Choose localhost or a configured SSH instance; no fallback was used')
    if action == 'help':
        if not args:
            return GUIDE
        if set(args) != {'tool'} or args['tool'] not in ACTIONS:
            raise ValueError('Terminal help accepts args={tool:ACTION}')
    elif action not in ACTIONS:
        raise ValueError('One-shot commands use exec(command, instance). Interactive programs use '
                         'remote_control(protocol="terminal", action="create", instance=ID, args={"command":"program"}).')
    return client(instance).call(action, args, turn)


def close():
    with CLIENTS_LOCK:
        clients = list(CLIENTS.values())
        CLIENTS.clear()
    for item in clients:
        item.close()


def launch_ssh():
    """PTY shell launcher: only this backend resolves authentication files."""
    import sys
    instance = os.environ['EVO_TERMINAL_INSTANCE']
    options = json.loads(os.environ.get('EVO_TERMINAL_OPTIONS', '{}'))
    entry = ssh.configuration()['servers'][instance]
    args = sys.argv[1:]
    login = bool(args and args[0] == '-l')
    if login:
        args = args[1:]
    if args and (len(args) != 2 or args[0] != '-c'):
        raise ValueError('Invalid terminal shell arguments')
    shell = options.get('shell', '/bin/bash')
    if not isinstance(shell, str) or not re.fullmatch(r'/[A-Za-z0-9_./-]+', shell):
        raise ValueError('Remote shell must be an absolute executable path')
    environment = options.get('env', {})
    if not isinstance(environment, dict) or any(not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', k)
            or not isinstance(v, str) or '\0' in v for k, v in environment.items()):
        raise ValueError('Invalid remote environment')
    command = shlex.join(['env', *[k + '=' + v for k, v in environment.items()], shell,
                         *(['-l'] if login else []), *args])
    cwd = options.get('cwd')
    if cwd is not None:
        if not isinstance(cwd, str) or not cwd.startswith('/') or '\0' in cwd:
            raise ValueError('Remote cwd must be an absolute path')
        command = 'cd -- ' + shlex.quote(cwd) + ' && exec ' + command
    else:
        command = 'exec ' + command
    argv, env = ssh.transport(entry, command)
    argv[argv.index('-T')] = '-tt'
    argv[1:1] = ['-o', 'EscapeChar=none']
    os.execve(argv[0], argv, env)
