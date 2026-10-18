"""Coordinate explicit workspace reset with the shared Python interpreter."""
import json
import os
from pathlib import Path
import shutil
import socket
import stat

WORKSPACE = Path('/var/lib/evo-hermes/workspace')
PYTHON_SOCKET = '/run/evo-web-python/python.sock'


def clear_workspace():
    if WORKSPACE.is_symlink() or WORKSPACE.resolve() != WORKSPACE:
        raise ValueError('Unexpected workspace path; reset refused')
    fd = os.open(WORKSPACE, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if os.fstat(fd).st_uid != os.getuid():
            raise PermissionError('Workspace owner does not match the service')
        count = 0
        for name in os.listdir(fd):
            if name == '.home':
                continue
            metadata = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(metadata.st_mode):
                shutil.rmtree(name, dir_fd=fd)
            else:
                os.unlink(name, dir_fd=fd)
            count += 1
        os.mkdir('.hermes-tmp', mode=0o700, dir_fd=fd)
        return {'success': True, 'removed_entries': count, 'workspace': '/workspace'}
    finally:
        os.close(fd)


def reset_workspace():
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
        peer.settimeout(150)
        peer.connect(PYTHON_SOCKET)
        peer.sendall(b'{"action":"reset_workspace"}\n')
        raw = peer.makefile('rb').readline(8193)
    if len(raw) > 8192 or not raw.endswith(b'\n'):
        raise RuntimeError('Invalid workspace reset response')
    result = json.loads(raw)
    if result.get('error') or not result.get('success'):
        raise RuntimeError(result.get('error', 'Workspace reset failed'))
    return result
