"""Ephemeral, standard-library file operations on the selected SSH host."""
import base64
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile

MAX_FILE = 16 * 2**20


def fingerprint(raw):
    return hashlib.sha256(raw).hexdigest()


def read_bytes(path, maximum=MAX_FILE):
    fd = os.open(str(path), os.O_RDONLY | os.O_NONBLOCK | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as src:
        info = os.fstat(src.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise ValueError('Requires a regular file within the size limit')
        raw = src.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError('File exceeds the size limit')
    return raw


def commit(path, raw, expected):
    """Stage in the destination directory; reject stale/unknown existing content."""
    current = read_bytes(path) if path.exists() else None
    if current is not None and (not expected or fingerprint(current) != expected):
        raise ValueError('File was not read, or changed since read. Read it again before overwriting.')
    if current is None and expected:
        raise ValueError('File disappeared since read. Inspect before retrying.')
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.evo-file-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(raw)
            os.fchmod(out.fileno(), stat.S_IMODE(path.stat().st_mode) if current is not None else 0o644)
        latest = read_bytes(path) if path.exists() else None
        if latest != current:
            raise ValueError('File changed during preparation. Read it again.')
        if current is None:
            os.link(name, str(path))  # Atomic no-clobber creation, even if another writer raced us.
        else:
            os.replace(name, str(path))
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return {'bytes': len(raw), '_version': fingerprint(raw)}


def operate(req):
    path = Path(req['path']).expanduser()
    if not path.is_absolute() or '\0' in str(path):
        raise ValueError('Use an absolute remote path (or ~/...)')
    path = path.resolve()
    action = req['action']
    if action == 'pi_read':
        raw = read_bytes(path)
        mime = ('image/png' if raw.startswith(b'\x89PNG\r\n\x1a\n') else
                'image/jpeg' if raw.startswith(b'\xff\xd8\xff') else
                'image/gif' if raw[:6] in (b'GIF87a', b'GIF89a') else
                'image/webp' if raw[:4] == b'RIFF' and raw[8:12] == b'WEBP' else
                'image/bmp' if raw.startswith(b'BM') else None)
        return {'data': base64.b64encode(raw).decode('ascii'), 'mime': mime}
    if action == 'pi_write':
        raw = req['content'].encode('utf-8')
        if len(raw) > MAX_FILE:
            raise ValueError('File exceeds 16MiB')
        expected = req.get('_expected')
        if '_expected' not in req and path.exists():
            expected = fingerprint(read_bytes(path))
        commit(path, raw, expected)
        return {}
    if action == 'download':
        raw = read_bytes(path)
        return {'data': base64.b64encode(raw).decode('ascii'), 'bytes': len(raw)}
    if action == 'upload':
        raw = base64.b64decode(req['data'], validate=True)
        if len(raw) > MAX_FILE:
            raise ValueError('File exceeds the size limit')
        return commit(path, raw, None)
    raise ValueError('Unknown file operation; Pi handles read/write/edit, Bash handles search')



def main():
    try:
        raw = sys.stdin.buffer.read(24 * 2**20 + 1)
        if len(raw) > 24 * 2**20:
            raise ValueError('Transfer exceeds 16MiB')
        answer = operate(json.loads(raw))
    except (OSError, ValueError, UnicodeError, KeyError) as error:
        answer = {'error': str(error)[:300]}
    print(json.dumps(answer, ensure_ascii=False, separators=(',', ':')))


if __name__ == '__main__':
    main()
