"""One lazy RDP session for the Discord owner; no desktop frames on disk."""
import contextlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import threading
import time

from .. import context
from .instances import instance_id
from . import remote_config

CONFIG = remote_config.CONFIG
PYTHON = Path('/opt/evo-bot/rdp-runtime/bin/python')
PACKAGE = Path(__file__).resolve().parents[2]
ACTIONS = ['servers', 'connect', 'status', 'screenshot', 'click', 'type', 'key',
           'scroll', 'disconnect']


def configuration(path=None):
    path = CONFIG if path is None else Path(path)
    value = remote_config.protocol('rdp', path)
    if value is None:
        return {'enabled': False, 'servers': {}}
    if value.get('enabled') is False and 'servers' not in value:
        return {'enabled': False, 'servers': {}}
    if not isinstance(value.get('servers'), dict):
        raise ValueError('Invalid private RDP configuration')
    from .credential_input import resolve_connection
    for name, entry in value['servers'].items():
        if not isinstance(entry, dict): raise ValueError('Invalid RDP instance')
        value['servers'][name] = resolve_connection('rdp', name, entry)
    return value


def validate(args):
    if not isinstance(args, dict) or args.get('action') not in ACTIONS:
        raise ValueError('Unknown RDP action')
    action = args['action']
    allowed = {'action', 'server'} | {
        'click': {'x', 'y', 'frame', 'button'}, 'type': {'text', 'frame'},
        'key': {'key', 'frame'}, 'scroll': {'direction', 'steps', 'frame'},
    }.get(action, set())
    if set(args) - allowed:
        raise ValueError('Unexpected RDP argument')
    if 'server' in args and (not isinstance(args['server'], str) or not
                            re.fullmatch(r'[A-Za-z0-9_-]{1,48}', args['server'])):
        raise ValueError('Use a configured RDP instance ID')
    if action in ('click', 'type', 'key', 'scroll'):
        if not isinstance(args.get('frame'), str) or not args['frame']:
            raise ValueError('Observe screenshot first and use its frame ID')
    if action == 'click':
        for key in ('x', 'y'):
            if type(args.get(key)) is not int or not 0 <= args[key] <= 1000:
                raise ValueError('Click coordinates use 0..1000, not pixels')
        if args.get('button', 'left') not in ('left', 'right', 'double'):
            raise ValueError('Use left, right or double click')
    if action == 'type' and (not isinstance(args.get('text'), str) or
                             not 1 <= len(args['text']) <= 2000):
        raise ValueError('Type requires 1..2000 characters')
    if action == 'key' and (not isinstance(args.get('key'), str) or
                            not 1 <= len(args['key']) <= 80):
        raise ValueError('Key requires a supported key or chord')
    if action == 'scroll' and (args.get('direction') not in ('up', 'down') or
                              type(args.get('steps', 3)) is not int or
                              not 1 <= args.get('steps', 3) <= 10):
        raise ValueError('Scroll requires up/down and 1..10 steps')


class RemoteDesktop:
    def __init__(self):
        self.lock = threading.RLock()
        self.process = self.selector = self.timer = None
        self.buffer = bytearray()
        self.server = None
        self.last_used = self.serial = 0

    def running(self):
        return self.process is not None and self.process.poll() is None

    def close(self):
        with self.lock:
            if self.timer:
                self.timer.cancel()
                self.timer = None
            process, self.process = self.process, None
            try:
                if process is not None:
                    with contextlib.suppress(OSError):
                        process.stdin.close()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        with contextlib.suppress(ProcessLookupError):
                            os.killpg(process.pid, signal.SIGTERM)
                        try:
                            process.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            with contextlib.suppress(ProcessLookupError):
                                os.killpg(process.pid, signal.SIGKILL)
                            process.wait(timeout=2)
                    process.stdout.close()
            finally:
                if self.selector:
                    self.selector.close()
                    self.selector = None
                self.buffer.clear()
                self.server = None

    def reap(self):
        with self.lock:
            if time.monotonic() - self.last_used >= 900:
                self.close()

    def request(self, args, turn):
        self.serial += 1
        raw = json.dumps({'id': self.serial, 'params': args}).encode() + b'\n'
        self.process.stdin.write(raw)
        self.process.stdin.flush()
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            if turn.cancelled.is_set():
                raise InterruptedError('RDP operation interrupted; no retry')
            if b'\n' in self.buffer:
                line, _, rest = self.buffer.partition(b'\n')
                self.buffer = bytearray(rest)
                response = json.loads(line)
                if response.get('id') != self.serial:
                    raise ConnectionError('Unexpected RDP response')
                if 'error' in response:
                    raise ValueError(response['error'])
                return response['result']
            if not self.selector.select(.1):
                continue
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise ConnectionError('RDP worker disconnected; no automatic reconnect')
            self.buffer.extend(chunk)
            if len(self.buffer) > 1024 * 1024:
                raise ConnectionError('RDP response exceeded its limit')
        raise TimeoutError('RDP timed out; observe status before retrying')

    def call(self, args, turn):
        validate(args)
        with self.lock:
            action = args['action']
            if action == 'disconnect':
                if self.server is not None and args.get('server', self.server) != self.server:
                    raise ValueError('Disconnect must name the connected instance')
                self.close()
                return {'disconnected': True}
            cfg = configuration()
            if action == 'servers':
                return {'enabled': cfg.get('enabled') is True,
                        'servers': sorted(cfg['servers'])}
            if action == 'status':
                requested = args.get('server')
                if requested is not None and requested not in cfg['servers']:
                    raise ValueError('Unknown instance; use remote_control action=list')
                if cfg.get('enabled') is not True:
                    self.close()
                if requested is not None and requested != self.server:
                    return {'enabled': cfg.get('enabled') is True, 'connected': False, 'server': requested}
                if self.running():
                    try:
                        return dict(enabled=True, **self.request({'action': 'status', 'server': self.server}, turn))
                    except BaseException:
                        self.close()
                        raise
                return {'enabled': cfg.get('enabled') is True, 'connected': False, 'server': None}
            if cfg.get('enabled') is not True:
                raise PermissionError('RDP is staged but not enabled for live testing yet')
            server = args.get('server') or self.server or cfg.get('default')
            if server not in cfg['servers']:
                raise ValueError('Choose an instance from remote_control action=list')
            if turn.cancelled.is_set():
                raise InterruptedError('RDP operation interrupted')
            if self.running() and server != self.server:
                raise ValueError('Disconnect before changing RDP instances')
            if not self.running():
                if action != 'connect':
                    raise ValueError('Connect to RDP first')
                self.close()
                self.process = subprocess.Popen([str(PYTHON), '-m', 'evo_bot.rdp_worker', str(CONFIG)],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    start_new_session=True, bufsize=0, cwd=str(PACKAGE),
                    env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8',
                         'PYTHONPATH': str(PACKAGE), 'PYTHONDONTWRITEBYTECODE': '1'})
                self.server = server
                self.selector = selectors.DefaultSelector()
                self.selector.register(self.process.stdout, selectors.EVENT_READ)
            try:
                value = self.request(dict(args, server=server), turn)
            except BaseException:
                self.close()
                raise
            self.last_used = time.monotonic()
            if self.timer:
                self.timer.cancel()
            self.timer = threading.Timer(900, self.reap)
            self.timer.daemon = True
            self.timer.start()
            return value


CLIENT = RemoteDesktop()


def remote_desktop(args):
    turn = context.owner_message()
    if turn is None:
        raise PermissionError('RDP requires an owner Discord message')
    ident = instance_id(args)
    request = {k: v for k, v in args.items() if k != 'instance'}
    if ident is not None:
        request['server'] = ident
    result = CLIENT.call(request, turn)
    if isinstance(result, dict) and 'server' in result:
        result = dict(result)
        result['instance'] = result.pop('server')
    return result


def close():
    CLIENT.close()
