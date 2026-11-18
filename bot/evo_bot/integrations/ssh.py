"""Owner Discord commands over OpenSSH; credentials stay outside the workspace."""
import contextlib
import hashlib
import logging
import json
import os
from pathlib import Path
import re
import selectors
import shlex
import signal
import subprocess
import tempfile
import time

from .. import context
from .credential_input import private_text, resolve_connection
from .instances import instance_id
from . import host, remote_config

CONFIG = remote_config.CONFIG
ASKPASS = Path(__file__).resolve().parents[1] / 'ssh_askpass.py'
MAX_OUTPUT = 256 * 1024
LOG = logging.getLogger(__name__)


def configuration():
    data = remote_config.protocol('ssh', CONFIG)
    if data is None:
        return {'servers': {}}
    if not isinstance(data, dict) or not isinstance(data.get('servers'), dict):
        raise ValueError('Invalid private SSH configuration')
    for name, entry in data['servers'].items():
        if isinstance(entry, dict):
            entry = data['servers'][name] = resolve_connection('ssh', name, entry)
        if (not re.fullmatch(r'[A-Za-z0-9_-]{1,48}', name) or not isinstance(entry, dict)
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.:-]{0,252}', entry.get('host', ''))
                or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]{0,63}', entry.get('username', ''))
                or entry['username'] == 'root'
                or type(entry.get('port', 22)) is not int or not 1 <= entry.get('port', 22) <= 65535
                or ('password_file' in entry) == ('identity_file' in entry)):
            raise ValueError('SSH needs a host, non-root user and one authentication file')
        for key in ('known_hosts', 'password_file' if 'password_file' in entry else 'identity_file'):
            if not isinstance(entry.get(key), str) or not Path(entry[key]).is_absolute():
                raise ValueError('SSH authentication and known_hosts paths must be absolute')
        if 'sudo_password_file' in entry and (not isinstance(entry['sudo_password_file'], str)
                or not Path(entry['sudo_password_file']).is_absolute()):
            raise ValueError('Stored sudo password needs an absolute private path')
        if (type(entry.get('sudo_nopasswd', False)) is not bool
                or entry.get('sudo_nopasswd') and 'sudo_password_file' in entry):
            raise ValueError('Choose either sudo_nopasswd=true or a stored sudo password')
    if data.get('default') is not None and data['default'] not in data['servers']:
        raise ValueError('Unknown default SSH instance')
    return data


def command_timeout(value=600):
    if type(value) not in (int, float) or not .1 <= value <= 600:
        raise ValueError('Command timeout must be 0.1..600 seconds; default 600')
    return value


def validate(args):
    if not isinstance(args, dict) or set(args) - {'instance', 'command', 'timeout', 'run_as', 'reason'}:
        raise ValueError('Use instance, command, timeout and optional run_as=root with reason')
    instance_id(args)
    command, timeout = args.get('command'), command_timeout(args.get('timeout', 600))
    if (not isinstance(command, str) or not 1 <= len(command) <= 16000
            or not command.strip() or '\0' in command):
        raise ValueError('command must contain 1..16000 characters without NUL')
    if args.get('run_as', 'user') not in ('user', 'root'):
        raise ValueError('run_as must be user or root')
    if args.get('run_as') == 'root' and (len(command) > 1000 or not isinstance(args.get('reason'), str)
            or not 1 <= len(args['reason']) <= 160):
        raise ValueError('Root needs command <=1000 characters and reason (1..160) for Discord notice')
    return command, timeout


def transport(entry, command):
    # Ignore ambient user/system config, agents, forwarding and automatic trust.
    argv = ['/usr/bin/ssh', '-F', '/dev/null', '-T',
            '-o', 'StrictHostKeyChecking=yes', '-o', 'UpdateHostKeys=no',
            '-o', 'GlobalKnownHostsFile=/dev/null', '-o', 'UserKnownHostsFile=' + entry['known_hosts'],
            '-o', 'ConnectTimeout=10', '-o', 'ConnectionAttempts=1',
            '-o', 'ServerAliveInterval=10', '-o', 'ServerAliveCountMax=2',
            '-o', 'ClearAllForwardings=yes', '-o', 'ForwardAgent=no',
            '-o', 'PermitLocalCommand=no', '-o', 'LogLevel=ERROR',
            '-o', 'NumberOfPasswordPrompts=1', '-o', 'KbdInteractiveAuthentication=no']
    env = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'HOME': '/nonexistent'}
    if 'password_file' in entry:
        argv += ['-o', 'PreferredAuthentications=password', '-o', 'PubkeyAuthentication=no']
        env.update(SSH_ASKPASS=str(ASKPASS), SSH_ASKPASS_REQUIRE='force',
                   DISPLAY='evo-ssh:0', EVO_SSH_PASSWORD_FILE=entry['password_file'])
    else:
        argv += ['-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
                 '-o', 'IdentityAgent=none', '-o', 'PreferredAuthentications=publickey',
                 '-i', entry['identity_file']]
    return argv + ['-p', str(entry.get('port', 22)),
                   entry['username'] + '@' + entry['host'], command], env


def stored_password(path):
    value = private_text(path).rstrip('\r\n')
    if not 1 <= len(value) <= 2000 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('Invalid stored password; owner must update private configuration, never chat')
    return value


def remote_ssh(args):
    turn = context.owner_message()
    if turn is None:
        raise PermissionError('SSH requires an owner Discord message')
    command, timeout = validate(args)
    cfg = configuration()
    name = instance_id(args) or cfg.get('default')
    if name not in cfg['servers']:
        raise ValueError('Unknown SSH instance; use remote_control action=list')
    entry = cfg['servers'][name]
    if args.get('run_as') != 'root':
        return execute(entry, name, command, timeout, turn)
    if 'sudo_password_file' not in entry and not entry.get('sudo_nopasswd'):
        raise ValueError('Stored sudo missing; configure it privately.')
    if not host.HOST_LOCK.acquire(blocking=False):
        raise ValueError('Another host/SSH root execution is pending')
    try:
        gate = entry.get('root_gate', 'notify_only')
        if gate not in ('notify_only', 'approval_card'):
            raise ValueError('ssh.root_gate must be notify_only or approval_card')
        target = f"{entry['username']}@{entry['host']}:{entry.get('port', 22)} ({name})"
        execution_key = host.root_command_key(turn.key, turn.id, {'command': command, 'cwd': 'ssh:' + target})
        if gate == 'approval_card':
            refusal = host.approve_root_once(turn, '# SSH root on ' + target + '\n' + command,
                                            'Reason: ' + args['reason'], execution_key)
            if refusal:
                return json.dumps(refusal, ensure_ascii=False)
        elif execution_key in host.ROOT_EXECUTIONS:
            return json.dumps({'success': False, 'retryable': False,
                               'error': 'This SSH root command already executed this turn.'})
        command_hash = hashlib.sha256(command.encode()).hexdigest()
        LOG.info('remote_ssh_root_start turn=%s instance=%s gate=%s command_sha256=%s reason_sha256=%s',
                 turn.id, name, gate, command_hash, hashlib.sha256(args['reason'].encode()).hexdigest())
        try:
            secret = stored_password(entry['sudo_password_file']) if 'sudo_password_file' in entry else None
            if turn.cancelled.is_set() or not host.discord_connected():
                raise InterruptedError('SSH root interrupted before connection')
            host.remember_root_execution(execution_key)
            result = (execute(entry, name, command, timeout, turn, sudo_nopasswd=True)
                      if entry.get('sudo_nopasswd') else
                      execute(entry, name, command, timeout, turn, sudo_password=secret))
            payload = json.loads(result)
        except Exception as exc:
            LOG.exception('remote_ssh_root_error turn=%s instance=%s command_sha256=%s',
                          turn.id, name, command_hash)
            if gate == 'notify_only':
                try:
                    host.notify_root(turn, host.root_summary('ssh:' + name, 'error=' + type(exc).__name__,
                                                             args['reason'], command))
                except Exception:
                    LOG.exception('remote_ssh_root_result_notice_failed turn=%s instance=%s', turn.id, name)
            raise
        output = str(payload.get('output', ''))
        LOG.info('remote_ssh_root_finish turn=%s instance=%s command_sha256=%s exit_code=%s timed_out=%s output_sha256=%s',
                 turn.id, name, command_hash, payload.get('exit_code'), payload.get('timed_out', False),
                 hashlib.sha256(output.encode()).hexdigest())
        if gate == 'notify_only':
            try:
                status = 'exit=' + str(payload.get('exit_code'))
                if payload.get('timed_out'):
                    status = 'error=timeout'
                host.notify_root(turn, host.root_summary('ssh:' + name, status, args['reason'], command))
            except Exception:
                LOG.exception('remote_ssh_root_result_notice_failed turn=%s instance=%s', turn.id, name)
                payload['discord_notice'] = 'delivery_failed_after_execution; do_not_retry'
                return json.dumps(payload, ensure_ascii=False)
        return result
    finally:
        host.HOST_LOCK.release()


def execute(entry, name, command, timeout, turn, sudo_password=None, sudo_nopasswd=False):
    secret = stored_password(entry['password_file']) if 'password_file' in entry else None
    root = sudo_password is not None or sudo_nopasswd
    script = None if root else command.encode('utf-8')
    shell = '/bin/bash --noprofile --norc -c '
    if root:
        # -k requires fresh auth. Even NOPASSWD commands get EOF, never the password pipe.
        command = 'sudo -k ' + ('-n' if sudo_nopasswd else '-S') + ' -p "" -- ' + shell + shlex.quote('exec </dev/null\n' + command)
    else:
        # Keep command text out of ancestor argv; pkill -f must not match its own SSH shell.
        # FD 3 carries the script while programs receive EOF on stdin.
        command = 'exec /bin/bash --noprofile --norc /dev/fd/3 3<&0 </dev/null'
    argv, env = transport(entry, command)
    if turn.cancelled.is_set():
        raise InterruptedError('SSH interrupted before connection')
    output, received, timed_out = bytearray(), 0, False
    deadline = time.monotonic() + timeout
    with contextlib.ExitStack() as resources:
        source = resources.enter_context(tempfile.TemporaryFile(dir='/dev/shm')) if script is not None else None
        if source is not None: source.write(script); source.seek(0)
        process = resources.enter_context(subprocess.Popen(argv,
            stdin=source if source is not None else subprocess.PIPE if sudo_password is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True, env=env))
        try:
            if sudo_password is not None:
                try:
                    process.stdin.write((sudo_password + '\n').encode())
                except BrokenPipeError:
                    pass  # Authentication/host-key failures are returned below, never retried.
                finally:
                    try:
                        process.stdin.close()
                    except BrokenPipeError:
                        pass
            with selectors.DefaultSelector() as poll:
                poll.register(process.stdout, selectors.EVENT_READ)
                while True:
                    if turn.cancelled.is_set() or root and not host.discord_connected():
                        raise InterruptedError('SSH interrupted; remote work may continue. Do not replay automatically.')
                    if time.monotonic() >= deadline:
                        timed_out = True
                        break
                    if not poll.select(.1):
                        continue
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        break
                    received += len(chunk)
                    output.extend(chunk[:max(0, MAX_OUTPUT - len(output))])
                if not timed_out:
                    process.wait(timeout=max(.01, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
    text = output.decode('utf-8', errors='replace')
    for value in set(filter(None, (secret, sudo_password))):
        text = text.replace(value, '[credential withheld]')
        if received > MAX_OUTPUT:
            # A truncated echo must not expose a prefix of a registered password.
            for size in range(min(len(value) - 1, len(text)), 0, -1):
                if text.endswith(value[:size]):
                    text = text[:-size] + '[credential withheld]'
                    break
    result = {'instance': name, 'exit_code': None if timed_out else process.returncode, 'output': text}
    if received > MAX_OUTPUT:
        result['output_truncated'] = True
    if timed_out:
        result.update(timed_out=True, note='Connection closed; remote work may continue. Inspect before retrying.')
    return json.dumps(result, ensure_ascii=False)
