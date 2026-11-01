"""Private I/O adapters for Pi's unmodified coding tools, not model-facing tools."""
import asyncio
import base64
import concurrent.futures
import json
import re
import shlex
import tempfile
import time
from pathlib import Path

from .. import context, remote_file_worker as worker
from . import codex, host, remote_files, ssh


def request(args, fields):
    turn = context.owner_message()
    if turn is None:
        raise PermissionError('Remote coding requires an owner Discord message')
    if not isinstance(args, dict) or set(args) - ({'instance'} | fields):
        raise ValueError('Invalid coding request')
    ident = args.get('instance')
    if not isinstance(ident, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,48}', ident):
        raise ValueError('Choose an exact instance from remote_control list')
    if turn.cancelled.is_set():
        raise InterruptedError('Coding request interrupted; no automatic replay')
    return turn, ident


def checked(value):
    if isinstance(value, str):
        value = json.loads(value)
    if value.get('error') or value.get('success') is False and 'exit_code' not in value:
        raise RuntimeError(value.get('error', 'Remote request failed'))
    return value


def windows_rpc(ident, method, params, turn):
    if not codex.owner_session():
        raise PermissionError('Windows coding requires a live owner Discord connection')
    bridge = codex.select_server(ident)
    def check():
        if turn.cancelled.is_set() or not codex.owner_session():
            raise InterruptedError('Windows operation interrupted; inspect before retrying')
    future = asyncio.run_coroutine_threadsafe(bridge.rpc(method, params, check=check), codex.LOOP)
    deadline = time.monotonic() + 75
    try:
        while True:
            check()
            if time.monotonic() >= deadline:
                raise TimeoutError('Windows request timed out; inspect before retrying. No replay.')
            try:
                return future.result(timeout=.2)
            except concurrent.futures.TimeoutError:
                if future.done():
                    return future.result()
    finally:
        if not future.done():
            future.cancel()


def host_file(req):
    # Reuse the broker's ordinary-user filesystem masks. Requests/data stay in RAM.
    with tempfile.TemporaryDirectory(prefix='evo-pi-io-', dir='/dev/shm') as directory:
        root = Path(directory)
        (root/'worker.py').write_text(Path(worker.__file__).read_text())
        (root/'request.json').write_text(json.dumps(req, ensure_ascii=False))
        command = ('python3 ' + shlex.quote(str(root/'worker.py')) + ' < '
                   + shlex.quote(str(root/'request.json')) + ' > ' + shlex.quote(str(root/'result.json')))
        result = checked(host.host_command({'command': command}))
        if result.get('exit_code') != 0:
            raise RuntimeError('Host file operation failed: ' + result.get('output', '')[:200])
        return json.loads((root/'result.json').read_text())


def file_io(args):
    turn, ident = request(args, {'action', 'path', 'content', 'expected'})
    action, path = args.get('action'), args.get('path')
    if action not in ('read', 'write') or not isinstance(path, str) or not 1 <= len(path) <= 4096 or '\0' in path:
        raise ValueError('Use read/write with an absolute remote path or ~/...')
    if action == 'write' and (not isinstance(args.get('content'), str)
            or len(args['content'].encode('utf-8')) > worker.MAX_FILE):
        raise ValueError('Write content must be text within 16MiB')
    if 'expected' in args and not re.fullmatch(r'[0-9a-f]{64}', str(args['expected'])):
        raise ValueError('Invalid file version')
    if ident in codex.SERVERS:
        import ntpath
        if not re.match(r'^[A-Za-z]:[\\/]', path):
            raise ValueError('Windows files require an absolute drive path')
        if action == 'read':
            result = windows_rpc(ident, 'fs/readFile', {'path': path}, turn)
            raw = base64.b64decode(result['dataBase64'], validate=True)
            if len(raw) > worker.MAX_FILE:
                raise ValueError('File exceeds 16MiB')
            # Pi detects text/images from the same byte stream on every transport.
            return {'data': result['dataBase64'], 'mime': image_mime(raw)}
        if 'expected' in args:
            current = windows_rpc(ident, 'fs/readFile', {'path': path}, turn)
            if worker.fingerprint(base64.b64decode(current['dataBase64'], validate=True)) != args['expected']:
                raise ValueError('File changed during edit. Read it again.')
        windows_rpc(ident, 'fs/createDirectory', {'path': ntpath.dirname(path), 'recursive': True}, turn)
        windows_rpc(ident, 'fs/writeFile', {'path': path,
            'dataBase64': base64.b64encode(args['content'].encode('utf-8')).decode('ascii')}, turn)
        return {}
    if not (path.startswith('/') or path.startswith('~/')):
        raise ValueError('SSH/host files require an absolute path or ~/...')
    req = {'action': 'pi_' + action, 'path': path}
    if action == 'write':
        req['content'] = args['content']
    if 'expected' in args:
        req['_expected'] = args['expected']
    if ident == 'evo-host':
        return checked(host_file(req))
    servers = ssh.configuration()['servers']
    if ident not in servers:
        raise ValueError('Unknown instance; use remote_control list. No fallback was used.')
    return checked(remote_files.exchange(servers[ident], req, turn))


def image_mime(raw):
    if raw.startswith(b'\x89PNG\r\n\x1a\n'): return 'image/png'
    if raw.startswith(b'\xff\xd8\xff'): return 'image/jpeg'
    if raw[:6] in (b'GIF87a', b'GIF89a'): return 'image/gif'
    if raw[:4] == b'RIFF' and raw[8:12] == b'WEBP': return 'image/webp'
    if raw.startswith(b'BM'): return 'image/bmp'
    return None


def execute(args):
    turn, ident = request(args, {'command', 'root', 'reason', 'timeout'})
    command, root = args.get('command'), args.get('root', False)
    timeout = args.get('timeout', 15 if ident in codex.SERVERS else 60)
    if not isinstance(command, str) or not command.strip() or '\0' in command or type(root) is not bool:
        raise ValueError('Use command text and optional root=true')
    if root and (not isinstance(args.get('reason'), str) or not args['reason'].strip()):
        raise ValueError('root=true requires a reason for fresh Discord approval')
    if ident == 'evo-host':
        return checked(host.host_command({'command': command, 'timeout': timeout,
            **({'run_as': 'root', 'reason': args['reason']} if root else {})}))
    if ident in codex.SERVERS:
        if root:
            raise ValueError('Windows elevation is not supported; root=true never bypasses approval')
        if type(timeout) not in (int, float) or not 0 < timeout <= 15:
            raise ValueError('Windows command timeout must be at most 15 seconds')
        result = checked(codex.windows_exec({'instance': ident, 'command': [
            'powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand',
            base64.b64encode(command.encode('utf-16-le')).decode('ascii')], 'timeout_ms': int(timeout * 1000)}))
        return {'exit_code': result['exit_code'], 'output': result.get('stdout', '') + result.get('stderr', ''),
                'output_truncated': result.get('truncated', False)}
    request_args = {'instance': ident, 'command': command, 'timeout': timeout}
    if root:
        request_args.update(run_as='root', reason=args['reason'])
    return checked(ssh.remote_ssh(request_args))
