"""Per-chat anonymous browser over a forced-command SSH key, outside EVO RAM."""
import json
import contextlib
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import time

CONFIG = Path('/etc/evo-web-browser.json')
IDLE_SECONDS = 600
MAX_SESSIONS = 4
ACTIONS = ['open', 'read', 'tree', 'find', 'inspect', 'links', 'click', 'fill',
           'press', 'select', 'check', 'scroll', 'wait', 'evaluate', 'screenshot', 'status', 'close']
PROPERTIES = {
    'action': {'type': 'string', 'enum': ACTIONS}, 'url': {'type': 'string', 'maxLength': 4096},
    'selector': {'type': 'string', 'maxLength': 2000}, 'query': {'type': 'string', 'maxLength': 300},
    'role': {'type': 'string', 'maxLength': 80}, 'value': {'type': 'string', 'maxLength': 8000},
    'key': {'type': 'string', 'maxLength': 80}, 'script': {'type': 'string', 'maxLength': 12000},
    'images': {'type': 'boolean'}, 'checked': {'type': 'boolean'},
    'x': {'type': 'integer'}, 'y': {'type': 'integer'},
    'duration_ms': {'type': 'integer', 'minimum': 0, 'maximum': 5000},
    'timeout_ms': {'type': 'integer', 'minimum': 1000, 'maximum': 60000},
    'char_limit': {'type': 'integer', 'minimum': 1000, 'maximum': 12000},
    'offset': {'type': 'integer', 'minimum': 0, 'maximum': 200000},
    'state': {'type': 'string', 'enum': ['load', 'domcontentloaded', 'networkidle']}}
DEFINITION = {'name': 'browser',
    'description': 'Browse public websites in an anonymous session. open/tree show CSS selectors; '
        'read shows text; click/fill/press interact; screenshot returns an image (960x640). '
        'Use x,y for viewport clicks or scroll deltas. Images are off unless open images=true. '
        'offset continues long text. Cookies/login/history are discarded at turn end or close. '
        'No personal browser, saved accounts, downloads or private-network access.',
    'parameters': {'type': 'object', 'properties': PROPERTIES,
                   'required': ['action'], 'additionalProperties': False}}


def validate(args):
    if not isinstance(args, dict) or set(args) - PROPERTIES.keys():
        raise ValueError('Unknown browser parameters')
    for key, value in args.items():
        rule = PROPERTIES[key]
        expected = {'string': str, 'integer': int, 'boolean': bool}[rule['type']]
        if type(value) is not expected or ('enum' in rule and value not in rule['enum']):
            raise ValueError('Invalid browser ' + key)
        if isinstance(value, str) and len(value) > rule.get('maxLength', 20000):
            raise ValueError('Browser parameter too long: ' + key)
        if type(value) is int and not rule.get('minimum', -1000000) <= value <= rule.get('maximum', 1000000):
            raise ValueError('Browser parameter out of bounds: ' + key)
    action = args.get('action')
    if action not in ACTIONS: raise ValueError('Unknown browser action')
    required = {'open': ['url'], 'find': ['query'], 'inspect': ['selector'],
                'fill': ['selector', 'value'], 'press': ['key'], 'select': ['selector', 'value'],
                'check': ['selector', 'checked'], 'evaluate': ['script']}.get(action, [])
    if any(key not in args for key in required): raise ValueError('Missing browser action parameter')
    if action == 'click' and not ('selector' in args or all(k in args for k in ('x', 'y'))):
        raise ValueError('Click requires selector or x,y')
    if action == 'click' and 'selector' not in args and not (0 <= args['x'] < 960 and 0 <= args['y'] < 640):
        raise ValueError('Click is outside the viewport')


class Connection:
    def __init__(self):
        cfg = json.loads(CONFIG.read_text())
        self.process = subprocess.Popen(['/usr/bin/ssh', '-F', '/dev/null', '-T',
            '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes', '-o', 'IdentityAgent=none',
            '-o', 'StrictHostKeyChecking=yes', '-o', 'UpdateHostKeys=no',
            '-o', 'GlobalKnownHostsFile=/dev/null', '-o', 'UserKnownHostsFile=' + cfg['known_hosts'],
            '-o', 'ClearAllForwardings=yes', '-o', 'ForwardAgent=no', '-o', 'PermitLocalCommand=no',
            '-o', 'ConnectTimeout=10', '-o', 'ConnectionAttempts=1',
            '-o', 'ServerAliveInterval=10', '-o', 'ServerAliveCountMax=2',
            '-i', cfg['identity_file'], '-p', str(cfg.get('port', 22)),
            cfg['username'] + '@' + cfg['host']], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, start_new_session=True, bufsize=0,
            env={'PATH': '/usr/bin:/bin', 'HOME': '/nonexistent', 'LANG': 'C.UTF-8'})
        self.reader = selectors.DefaultSelector()
        self.reader.register(self.process.stdout, selectors.EVENT_READ)
        self.buffer = bytearray()
        self.serial = 0
        self.last_used = time.monotonic()
        try: assert self.request('ready', {}, 20).get('anonymous') is True
        except BaseException: self.close(); raise

    def request(self, method, params, timeout):
        self.serial += 1
        raw = (json.dumps({'id': self.serial, 'method': method, 'params': params}) + '\n').encode()
        self.process.stdin.write(raw); self.process.stdin.flush()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if b'\n' in self.buffer:
                line, _, self.buffer = self.buffer.partition(b'\n')
                data = json.loads(line)
                if data.get('id') != self.serial: raise ConnectionError('Browser reply mismatch')
                if 'error' in data: raise ValueError('Anonymous browser: ' + str(data['error']))
                self.last_used = time.monotonic()
                return data['result']
            if not self.reader.select(.1): continue
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk: raise ConnectionError('Anonymous browser disconnected; open the page again')
            self.buffer.extend(chunk)
            if len(self.buffer) > 4 * 2**20: raise ConnectionError('Browser response too large')
        raise TimeoutError('Browser timed out; action was not retried')

    def close(self):
        try:
            with contextlib.suppress(OSError): self.process.stdin.close()
            try: self.process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGTERM)
                try: self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    os.killpg(self.process.pid, signal.SIGKILL); self.process.wait(timeout=2)
        finally:
            self.reader.close(); self.process.stdout.close()


class Browsers:
    def __init__(self): self.sessions = {}

    def cleanup(self, session=None):
        for key, connection in list(self.sessions.items()):
            if key == session or time.monotonic() - connection.last_used >= IDLE_SECONDS:
                self.sessions.pop(key).close()

    def call(self, args, session):
        if not isinstance(session, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,80}', session):
            raise ValueError('Browser requires a Web UI conversation session; refresh the page')
        validate(args)
        self.cleanup()
        action = args['action']
        if action == 'close':
            self.cleanup(session)
            return {'closed': True, 'anonymous': True}
        if action == 'status': return {'running': session in self.sessions, 'anonymous': True}
        if session not in self.sessions:
            if action != 'open': raise ValueError('Anonymous session ended; use open first')
            if len(self.sessions) >= MAX_SESSIONS: raise ValueError('Anonymous browsers are busy; close an idle session and retry')
            self.sessions[session] = Connection()
        try:
            return self.sessions[session].request('call', args, args.get('timeout_ms', 15000) / 1000 + 15)
        except (OSError, TimeoutError, json.JSONDecodeError):
            self.cleanup(session)
            raise

    def close(self):
        for session in list(self.sessions): self.cleanup(session)
