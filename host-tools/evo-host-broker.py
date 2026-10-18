"""EVO local command broker. Tickets and output stay in RAM; no TCP listener."""
import collections
import hashlib
import hmac
import json
import os
from pathlib import Path
import pwd
import secrets
import selectors
import socket
import socketserver
import struct
import subprocess
import threading
import time
import unicodedata

POLICY = {}
TICKETS = {}
LOCK = threading.Lock()
EXEC_LOCK = threading.Lock()
AUDIT = collections.deque(maxlen=128)
CLIENT_SLOTS = threading.BoundedSemaphore(8)
MAX_REQUEST = 16384
MAX_OUTPUT = 32768


def validate(payload):
    if not isinstance(payload, dict):
        raise ValueError('Expected a request object')
    command = payload.get('command')
    if (not isinstance(command, str) or not command.strip()
            or len(command.encode('utf-16-le')) // 2 > 1000):
        raise ValueError('Command must contain 1 to 1000 characters')
    if '```' in command or any(unicodedata.category(c) in ('Cc', 'Cf', 'Cs')
                              and c not in '\n\t' for c in command):
        raise ValueError('Hidden/control characters and code fences are not allowed')
    reason = payload.get('reason', '')
    if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 160:
        raise ValueError('Explain the command in 1 to 160 characters')
    if any(unicodedata.category(c) in ('Cc', 'Cf', 'Cs') for c in reason):
        raise ValueError('Reason must be plain text')
    run_as = payload.get('run_as', 'evo-hermes')
    if run_as not in POLICY['users']:
        raise ValueError('Only evo-hermes or explicitly approved root is available')
    timeout = payload.get('timeout', 60)
    if type(timeout) is not int or not 1 <= timeout <= POLICY['max_timeout']:
        raise ValueError('Timeout must be an integer from 1 to 120 seconds')
    cwd = payload.get('cwd', '/workspace')
    if not isinstance(cwd, str) or not cwd.startswith('/') or len(cwd) > 240:
        raise ValueError('Provide an absolute EVO directory')
    if '`' in cwd or any(unicodedata.category(c) in ('Cc', 'Cf', 'Cs') for c in cwd):
        raise ValueError('Directory must be plain text')
    if cwd == '/workspace' or cwd.startswith('/workspace/'):
        cwd = str(Path(POLICY['workspace']) / cwd.removeprefix('/workspace').lstrip('/'))
    directory = Path(cwd).resolve(strict=True)
    if not directory.is_dir():
        raise ValueError('Working directory does not exist')
    forbidden = [Path(p) for p in hidden_paths(run_as)]
    if any(directory == p or p in directory.parents for p in forbidden):
        raise ValueError('Credentials and control-service directories are unavailable')
    identity = directory.stat()
    return dict(command=command, reason=reason.strip(), run_as=run_as,
                cwd=str(directory), cwd_dev=identity.st_dev, cwd_ino=identity.st_ino,
                timeout=timeout)


def digest(payload):
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                     separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def audit(request_id, record, outcome, **extra):
    entry = dict(time=int(time.time()), request_id=request_id,
                 digest=record.get('digest'), run_as=record.get('payload', {}).get('run_as'),
                 outcome=outcome, **extra)
    with LOCK:
        AUDIT.append(entry)
        path = Path(POLICY['runtime']) / 'audit.jsonl'
        temp = path.with_suffix('.tmp')
        temp.write_text(''.join(json.dumps(x) + '\n' for x in AUDIT), encoding='utf-8')
        temp.chmod(0o600)
        temp.replace(path)


def expire_tickets():
    expired = []
    with LOCK:
        for request_id, record in list(TICKETS.items()):
            if record['expires'] <= time.monotonic():
                expired.append((request_id, TICKETS.pop(request_id)))
    for request_id, record in expired:
        audit(request_id, record, 'expired')


def stage(payload):
    payload = validate(payload)
    expire_tickets()
    request_id = secrets.token_hex(16)
    record = dict(payload=payload, token=secrets.token_urlsafe(32), digest=digest(payload),
                  expires=time.monotonic() + POLICY['approval_ttl'])
    with LOCK:
        if len(TICKETS) >= 4:
            raise ValueError('Too many pending approvals')
        TICKETS[request_id] = record
    audit(request_id, record, 'staged')
    return dict(success=True, request_id=request_id, token=record['token'],
                digest=record['digest'], payload=payload)


def consume(request):
    request_id = request.get('request_id', '')
    token = request.get('token', '')
    if not isinstance(request_id, str) or not isinstance(token, str):
        raise ValueError('Invalid ticket')
    with LOCK:
        record = TICKETS.get(request_id)
        if record is None or not hmac.compare_digest(record['token'], token):
            raise ValueError('Unknown or already consumed ticket')
        if request.get('digest') != record['digest']:
            raise ValueError('Ticket does not match the approved command')
        TICKETS.pop(request_id)
    if record['expires'] <= time.monotonic():
        audit(request_id, record, 'expired')
        raise ValueError('Approval expired')
    return request_id, record


def stop_unit(unit):
    for command in ([ '/usr/bin/systemctl', 'kill', '--signal=SIGKILL', unit ],
                    [ '/usr/bin/systemctl', 'stop', unit ]):
        try:
            subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=5, check=False)
        except subprocess.TimeoutExpired:
            continue


def hidden_paths(run_as):
    # A separately approved root command is real host administration. Masks
    # belong only to the ordinary identity, not to an approved root job.
    return [] if run_as == 'root' else list(POLICY['private_paths'])


def unit_argv(unit, payload):
    properties = {
        'User': payload['run_as'], 'Group': payload['run_as'],
        'WorkingDirectory': '/', 'MemoryMax': '512M', 'MemorySwapMax': '0',
        'CPUQuota': '100%', 'TasksMax': '64', 'UMask': '0077',
        'LimitCORE': '0',
        'KillMode': 'control-group',
        'RuntimeMaxSec': str(payload['timeout'] + 2), 'TimeoutStopSec': '2',
        'BindsTo': 'evo-host.service', 'After': 'evo-host.service',
        'OOMPolicy': 'kill',
    }
    if payload['run_as'] == 'root':
        # PID 1 starts this service outside the broker's own mount/capability
        # restrictions. Keep only accounting, deadlines and process cleanup;
        # APT's _apt sandbox and other host maintenance use normal root rights.
        properties.update({
            'NoNewPrivileges': 'no', 'CapabilityBoundingSet': '~',
            'PrivateTmp': 'no', 'PrivateMounts': 'no',
            'ProtectSystem': 'no', 'ProtectHome': 'no',
        })
    else:
        properties.update({
            'NoNewPrivileges': 'yes', 'ProtectKernelModules': 'yes',
            'ProtectControlGroups': 'yes', 'RestrictSUIDSGID': 'yes',
            'ReadOnlyPaths': '/usr/local/lib/evo-host',
            'RestrictNamespaces': 'yes',
            'CapabilityBoundingSet': 'CAP_CHOWN CAP_DAC_OVERRIDE CAP_DAC_READ_SEARCH CAP_FOWNER CAP_KILL',
            'TemporaryFileSystem': '/tmp:rw,nosuid,nodev,size=64M',
            'InaccessiblePaths': ' '.join('-' + p for p in hidden_paths(payload['run_as'])),
        })
    argv = ['/usr/bin/systemd-run', '--quiet', '--pipe', '--wait', '--collect',
            '--service-type=exec', '--unit=' + unit]
    argv.extend('--property=' + k + '=' + v for k, v in properties.items())
    argv.extend(['/usr/bin/python3', '-I', '/usr/local/lib/evo-host/command-runner.py'])
    return argv


def run_command(request_id, record, peer):
    payload = record['payload']
    unit = 'evo-host-job-' + request_id + '.service'
    output = bytearray()
    total = 0
    outcome = 'finished'
    started = time.monotonic()
    proc = None
    try:
        proc = subprocess.Popen(unit_argv(unit, payload), stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        proc.stdin.write((json.dumps(payload, ensure_ascii=False) + '\n').encode('utf-8'))
        proc.stdin.close()
        os.set_blocking(proc.stdout.fileno(), False)
        peer.setblocking(False)
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ, 'output')
            selector.register(peer, selectors.EVENT_READ, 'peer')
            output_open = True
            while output_open or proc.poll() is None:
                if time.monotonic() - started > payload['timeout']:
                    outcome = 'timeout'
                    break
                for key, _ in selector.select(0.25):
                    if key.data == 'peer':
                        # There is no second client message after execute; closure means /stop.
                        outcome = 'cancelled'
                        break
                    chunk = os.read(proc.stdout.fileno(), 65536)
                    if not chunk:
                        selector.unregister(proc.stdout)
                        output_open = False
                        continue
                    total += len(chunk)
                    output.extend(chunk[:max(0, MAX_OUTPUT - len(output))])
                if outcome == 'cancelled':
                    break
    finally:
        stop_unit(unit)
        if proc is not None:
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
            if proc.stdout:
                proc.stdout.close()
    elapsed = round(time.monotonic() - started, 3)
    code = proc.returncode if proc is not None else None
    audit(request_id, record, outcome, exit_code=code, elapsed_seconds=elapsed,
          output_bytes=total)
    return dict(success=outcome == 'finished' and code == 0, request_id=request_id,
                run_as=payload['run_as'], cwd=payload['cwd'], exit_code=code,
                outcome=outcome, elapsed_seconds=elapsed,
                output=output.decode('utf-8', errors='replace'),
                output_truncated=total > len(output), unit=unit)


def dispatch(request, peer):
    if not isinstance(request, dict):
        raise ValueError('Expected a request object')
    action = request.get('action')
    if action == 'stage':
        return stage(request.get('payload'))
    if action == 'status':
        expire_tickets()
        with LOCK:
            return dict(success=True, pending=len(TICKETS), running=EXEC_LOCK.locked())
    if action not in ('execute', 'cancel'):
        raise ValueError('Unknown action')
    request_id, record = consume(request)
    if action == 'cancel':
        audit(request_id, record, 'cancelled')
        return dict(success=True)
    if not EXEC_LOCK.acquire(blocking=False):
        audit(request_id, record, 'busy')
        return dict(success=False, error='Another host command is running')
    try:
        return run_command(request_id, record, peer)
    finally:
        EXEC_LOCK.release()


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        if not CLIENT_SLOTS.acquire(blocking=False):
            return
        try:
            _, uid, _ = struct.unpack('3i', self.request.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i')))
            if uid not in (0, POLICY['caller_uid']):
                return
            self.request.settimeout(5)
            raw = self.rfile.readline(MAX_REQUEST + 1)
            if len(raw) > MAX_REQUEST or not raw.endswith(b'\n'):
                raise ValueError('Malformed or oversized request')
            result = dispatch(json.loads(raw), self.request)
        except (ValueError, OSError, TypeError, KeyError, subprocess.SubprocessError) as error:
            result = dict(success=False, error=str(error)[:250])
        finally:
            CLIENT_SLOTS.release()
        run_as = result.get('run_as', result.get('payload', {}).get('run_as', 'evo-hermes'))
        root = run_as == 'root'
        result['execution_context'] = {
            'host': 'EVO local host', 'hostname': socket.gethostname(),
            'run_as': run_as,
            'execution_mode': 'host_root' if root else 'restricted_host_user',
            'protected_paths': hidden_paths(run_as),
            'root_requires_fresh_discord_approval': True,
            'note': ('Approved root jobs use the real host filesystem and normal root capabilities, '
                    'including APT privilege dropping to _apt. No bwrap, path masks, read-only '
                    'overlays or private /tmp are applied. Resource limits, timeout and child cleanup '
                    'still apply. Host changes persist. Use credentials internally; never print them.'
                    if root else
                    'Ordinary host jobs are restricted with systemd, outside bwrap. '
                    'Protected paths are hidden with InaccessiblePaths. '
                    'stat may show a root-owned, mode 000, empty placeholder instead of the real file. '
                    'This does not indicate a different machine or an empty credential file. '
                    'Request run_as=root with fresh Discord approval for real host administration; '
                    'sudo cannot elevate an ordinary job.')}
        try:
            self.request.setblocking(True)
            self.request.settimeout(3)
            self.wfile.write((json.dumps(result, ensure_ascii=False) + '\n').encode('utf-8'))
        except OSError:
            return


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    request_queue_size = 8


def main():
    global POLICY
    POLICY = json.loads(Path('/etc/evo-host/policy.json').read_text())
    account = pwd.getpwnam('evo-hermes')
    POLICY['caller_uid'] = account.pw_uid
    runtime = Path(POLICY['runtime'])
    runtime.mkdir(mode=0o750, exist_ok=True)
    address = runtime / 'broker.sock'
    address.unlink(missing_ok=True)
    os.umask(0o077)
    with Server(str(address), Handler) as server:
        address.chmod(0o660)
        os.chown(address, 0, account.pw_gid)
        server.serve_forever(poll_interval=1)


if __name__ == '__main__':
    main()
