"""Owner Discord commands over OpenSSH; credentials stay outside the workspace."""
import json
import os
from pathlib import Path
import re
import selectors
import shlex
import signal
import subprocess
import time

from .. import context
from .credential_input import private_text, resolve_connection
from .instances import instance_id
from . import host, remote_config

CONFIG = remote_config.CONFIG
ASKPASS = Path(__file__).resolve().parents[1] / 'ssh_askpass.py'
MAX_OUTPUT = 256 * 1024


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


def validate(args):
    if not isinstance(args, dict) or set(args) - {'instance', 'command', 'timeout', 'run_as', 'reason'}:
        raise ValueError('Use instance, command, timeout and optional run_as=root with reason')
    instance_id(args)
    command, timeout = args.get('command'), args.get('timeout', 60)
    if (not isinstance(command, str) or not 1 <= len(command) <= 16000
            or not command.strip() or '\0' in command):
        raise ValueError('command must contain 1..16000 characters without NUL')
    if type(timeout) is not int or not 1 <= timeout <= 300:
        raise ValueError('timeout must be 1..300 seconds')
    if args.get('run_as', 'user') not in ('user', 'root'):
        raise ValueError('run_as must be user or root')
    if args.get('run_as') == 'root' and (len(command) > 1000 or not isinstance(args.get('reason'), str)
            or not 1 <= len(args['reason']) <= 160):
        raise ValueError('Root needs command <=1000 characters and reason (1..160) for Discord approval')
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
        raise ValueError('Stored sudo is not configured for this instance. Owner must configure it privately; never paste passwords in chat.')
    if not host.HOST_LOCK.acquire(blocking=False):
        raise ValueError('Another host/SSH root approval or execution is pending')
    try:
        target = f"{entry['username']}@{entry['host']}:{entry.get('port', 22)} ({name})"
        execution_key = host.root_command_key(turn.key, turn.id, {'command': command, 'cwd': 'ssh:' + target})
        refusal = host.approve_root_once(turn, '# SSH root on ' + target + '\n' + command,
                                        'Root access: ' + args['reason'] + ' (this command only)', execution_key)
        if refusal:
            return json.dumps(refusal, ensure_ascii=False)
        # Only the backend reads this value, after approval. Never copy it to a workspace.
        secret = stored_password(entry['sudo_password_file']) if 'sudo_password_file' in entry else None
        if turn.cancelled.is_set() or not host.discord_connected():
            raise InterruptedError('SSH root interrupted before connection')
        host.remember_root_execution(execution_key)
        if entry.get('sudo_nopasswd'):
            return execute(entry, name, command, timeout, turn, sudo_nopasswd=True)
        return execute(entry, name, command, timeout, turn, sudo_password=secret)
    finally:
        host.HOST_LOCK.release()


def execute(entry, name, command, timeout, turn, sudo_password=None, sudo_nopasswd=False):
    secret = stored_password(entry['password_file']) if 'password_file' in entry else None
    root = sudo_password is not None or sudo_nopasswd
    shell = '/bin/bash --noprofile --norc -c '
    if root:
        # -k requires fresh auth. Even NOPASSWD commands get EOF, never the password pipe.
        command = 'sudo -k ' + ('-n' if sudo_nopasswd else '-S') + ' -p "" -- ' + shell + shlex.quote('exec </dev/null\n' + command)
    else:
        command = shell + shlex.quote(command)
    argv, env = transport(entry, command)
    if turn.cancelled.is_set():
        raise InterruptedError('SSH interrupted before connection')
    output, received, timed_out = bytearray(), 0, False
    deadline = time.monotonic() + timeout
    with subprocess.Popen(argv, stdin=subprocess.PIPE if sudo_password is not None else subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, start_new_session=True, env=env) as process:
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
