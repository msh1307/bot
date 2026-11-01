"""OMP-style file operations over the existing private SSH transport."""
import base64
from collections import OrderedDict
import json
import os
from pathlib import Path
import selectors
import shlex
import signal
import subprocess
import threading
import time

from .. import context
from .. import remote_file_worker as worker
from . import ssh

WORKSPACE = Path('/var/lib/evo-hermes/workspace')
OBSERVED = OrderedDict()
LOCK = threading.Lock()
ACTIONS = ('list', 'read', 'glob', 'grep', 'write', 'edit', 'upload', 'download')
FIELDS = {
    'list': {'offset', 'limit'}, 'read': {'start_line', 'limit'},
    'glob': {'pattern', 'offset', 'limit'}, 'grep': {'pattern', 'glob', 'offset', 'limit'},
    'write': {'content'}, 'edit': {'old_string', 'new_string'},
    'upload': {'local_path'}, 'download': {'local_path'},
}


def validate(args):
    if not isinstance(args, dict):
        raise ValueError('Use instance, action and path')
    action = args.get('action')
    if action not in ACTIONS or set(args) - ({'instance', 'action', 'path'} | FIELDS[action]):
        raise ValueError('Use an exact file action and its parameters')
    if ssh.instance_id(args) is None:
        raise ValueError('Choose instance from remote_control list (ssh)')
    if not isinstance(args.get('path'), str) or not 1 <= len(args['path']) <= 4096 or '\0' in args['path']:
        raise ValueError('Use an absolute remote path or ~/...')
    for key, default, low, high in [('offset', 0, 0, 1000000), ('start_line', 1, 1, 1000000),
                                     ('limit', 100, 1, 200 if action == 'read' else 100)]:
        value = args.get(key, default)
        if type(value) is not int or not low <= value <= high:
            raise ValueError(key + ' is outside its allowed range')
    for key in FIELDS[action] & {'pattern', 'glob', 'content', 'old_string', 'new_string', 'local_path'}:
        if key == 'glob' and key not in args:
            continue
        if not isinstance(args.get(key), str) or len(args[key]) > worker.MAX_TEXT or '\0' in args[key]:
            raise ValueError('Missing or invalid ' + key)
        if key not in ('content', 'new_string') and not args[key]:
            raise ValueError(key + ' cannot be empty')
    return action


def local_path(value):
    """Transfer endpoints stay in the same shared workspace visible to native tools."""
    if value.startswith('/workspace/'):
        value = str(WORKSPACE / value[len('/workspace/'):])
    path = Path(value)
    if not path.is_absolute():
        path = WORKSPACE / path
    path = path.resolve()
    root = WORKSPACE.resolve()
    if root not in path.parents:
        raise ValueError('local_path must be a file inside /workspace')
    return path


def exchange(entry, req, turn):
    """One cancellable call; no agent, package install, saved helper or replay."""
    source = Path(worker.__file__).read_text()
    argv, env = ssh.transport(entry, 'python3 -c ' + shlex.quote(source))
    payload = json.dumps(req, ensure_ascii=False, separators=(',', ':')).encode()
    answer = bytearray()
    deadline = time.monotonic() + 60
    with subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          start_new_session=True, env=env) as proc:
        def write():
            try:
                proc.stdin.write(payload); proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass
        sender = threading.Thread(target=write, daemon=True); sender.start()
        errors = bytearray()
        try:
            with selectors.DefaultSelector() as poll:
                poll.register(proc.stdout, selectors.EVENT_READ, answer)
                poll.register(proc.stderr, selectors.EVENT_READ, errors)
                while poll.get_map():
                    if turn.cancelled.is_set():
                        raise InterruptedError('Remote file request interrupted. Inspect before retrying; no replay.')
                    if time.monotonic() >= deadline:
                        raise TimeoutError('Remote file request timed out. Inspect before retrying; no replay.')
                    for key, _ in poll.select(.1):
                        data = os.read(key.fileobj.fileno(), 65536)
                        if not data:
                            poll.unregister(key.fileobj); continue
                        if len(key.data) + len(data) > (24 * 2**20 if key.data is answer else 16384):
                            raise ValueError('Remote file response exceeds its limit')
                        key.data.extend(data)
                proc.wait(timeout=max(.01, deadline - time.monotonic()))
            if proc.returncode:
                # Authentication errors can contain private file paths; do not reflect stderr.
                raise RuntimeError('SSH file operation failed (exit ' + str(proc.returncode)
                                   + '); check connection and remote python3 availability')
            return json.loads(answer)
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(); sender.join(timeout=1)


def remote_files(args):
    turn = context.owner_message()
    if turn is None:
        raise PermissionError('Remote files require an owner Discord message')
    action = validate(args)
    name = args['instance']
    servers = ssh.configuration()['servers']
    if name not in servers:
        raise ValueError('Unknown SSH instance; use remote_control action=list')
    req = {k: v for k, v in args.items() if k not in ('instance', 'local_path')}
    key = (turn.key, name, args['path'])
    with LOCK:
        req['_expected'] = OBSERVED.get(key)
    dest = None
    if action == 'upload':
        src = local_path(args['local_path'])
        raw = worker.read_bytes(src)
        req['data'] = base64.b64encode(raw).decode('ascii')
    elif action == 'download':
        dest = local_path(args['local_path'])
        if dest.exists() or dest.is_symlink():
            raise ValueError('Download destination exists; choose a new local_path')
    if turn.cancelled.is_set():
        raise InterruptedError('Remote file request interrupted before connection')
    result = exchange(servers[name], req, turn)
    version = result.pop('_version', None)
    if version is not None:
        with LOCK:
            OBSERVED[key] = version; OBSERVED.move_to_end(key)
            while len(OBSERVED) > 256:
                OBSERVED.popitem(last=False)
    if action == 'download' and 'error' not in result:
        if turn.cancelled.is_set():
            raise InterruptedError('Download interrupted before saving')
        raw = base64.b64decode(result.pop('data'), validate=True)
        if len(raw) > worker.MAX_FILE:
            raise ValueError('Download exceeds 16MiB')
        # Exclusive creation: a model-selected destination never overwrites workspace work.
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(dest), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as out:
            out.write(raw)
        result['local_path'] = '/workspace/' + str(dest.relative_to(WORKSPACE.resolve()))
    return result


def register(ctx):
    ctx.register_tool('file_transfer', 'evo_ssh_files', {
        'name': 'file_transfer',
        'description': 'Upload/download a file between /workspace (local_path) and an SSH instance (path). '
            '16MiB maximum; bytes stay out of chat. Download requires a new local_path. '
            'Upload requires a new remote path. Use read/write/edit for text files.',
        'parameters': {'type': 'object', 'properties': {
            'instance': {'type': 'string', 'maxLength': 48},
            'action': {'type': 'string', 'enum': ['upload', 'download']},
            'path': {'type': 'string', 'maxLength': 4096},
            'local_path': {'type': 'string'},
            }, 'required': ['instance', 'action', 'path', 'local_path'], 'additionalProperties': False}}, transfer)


def transfer(args):
    if not isinstance(args, dict) or args.get('action') not in ('upload', 'download'):
        raise ValueError('Use upload or download; text files use read/write/edit')
    return remote_files(args)
