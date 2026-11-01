"""Ephemeral, standard-library file operations on the selected SSH host."""
import base64
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

MAX_FILE = 16 * 2**20
MAX_TEXT = 2 * 2**20
SKIP = {'.git', 'node_modules', '.venv', '__pycache__'}


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


def text(raw):
    if b'\0' in raw:
        raise ValueError('Binary file: use download')
    return raw.decode('utf-8')


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


def files(path):
    if path.is_file():
        yield path
        return
    if not path.is_dir():
        raise FileNotFoundError(str(path))
    count = 0
    for parent, dirs, names in os.walk(str(path), followlinks=False):
        dirs[:] = sorted(n for n in dirs if n not in SKIP and not Path(parent, n).is_symlink())
        for name in sorted(names):
            item = Path(parent, name)
            if item.is_symlink():
                continue
            count += 1
            if count > 20000:
                raise ValueError('Search scope exceeds 20000 files; choose a narrower path')
            yield item


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
    offset, limit = req.get('offset', 0), req.get('limit', 100)
    if action == 'list' or action == 'read' and path.is_dir():
        items = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name))
        page = [p.name + ('/' if p.is_dir() else '') for p in items[offset:offset + limit]]
        result = {'entries': page}
        if offset + len(page) < len(items):
            result['next_offset'] = offset + len(page)
        return result
    if action in ('read', 'download'):
        raw = read_bytes(path, MAX_TEXT if action == 'read' else MAX_FILE)
        result = {'_version': fingerprint(raw)}
        if action == 'download':
            return dict(result, data=base64.b64encode(raw).decode('ascii'), bytes=len(raw))
        lines = text(raw).splitlines(keepends=True)
        start = req.get('start_line', 1) - 1
        selected, size = [], 0
        for line in lines[start:start + limit]:
            if size + len(line) > 12000:
                if not selected:
                    raise ValueError('Single line exceeds 12000 characters; use shell_exec for a narrower extraction')
                break
            selected.append(line); size += len(line)
        result.update(content=''.join(f'{start+i+1}: {line}' for i, line in enumerate(selected)))
        if start + len(selected) < len(lines):
            result['next_line'] = start + len(selected) + 1
        return result
    if action in ('write', 'edit', 'upload'):
        if action == 'edit':
            old = read_bytes(path, MAX_TEXT)
            if not req.get('_expected') or fingerprint(old) != req['_expected']:
                raise ValueError('Read this file before editing; it may have changed')
            content = text(old)
            if content.count(req['old_string']) != 1:
                raise ValueError('old_string must match exactly once; include more surrounding text')
            raw = content.replace(req['old_string'], req['new_string'], 1).encode('utf-8')
        elif action == 'upload':
            raw = base64.b64decode(req['data'], validate=True)
        else:
            raw = req['content'].encode('utf-8')
        if len(raw) > (MAX_FILE if action == 'upload' else MAX_TEXT):
            raise ValueError('File exceeds the size limit')
        return commit(path, raw, req.get('_expected'))
    if action in ('glob', 'grep'):
        pattern = req['pattern']
        regex = re.compile(pattern) if action == 'grep' else None
        matches, seen, scanned = [], 0, 0
        for item in files(path):
            name = str(item.relative_to(path)) if path.is_dir() else item.name
            if action == 'glob':
                hit = fnmatch.fnmatchcase(name, pattern) or fnmatch.fnmatchcase(item.name, pattern) \
                    or pattern.startswith('**/') and fnmatch.fnmatchcase(name, pattern[3:])
                rows = [name] if hit else []
            else:
                if req.get('glob') and not fnmatch.fnmatchcase(name, req['glob']) \
                        and not fnmatch.fnmatchcase(item.name, req['glob']):
                    continue
                try:
                    raw = read_bytes(item, MAX_TEXT)
                    scanned += len(raw)
                    if scanned > 32 * 2**20:
                        raise ValueError('Search exceeds 32MiB; choose a narrower path')
                    lines = text(raw).splitlines()
                except (OSError, UnicodeError):
                    continue
                except ValueError as error:
                    if scanned > 32 * 2**20:
                        raise
                    continue
                rows = (f'{name}:{i}: {line[:400]}' for i, line in enumerate(lines, 1) if regex.search(line))
            for row in rows:
                if seen >= offset:
                    if len(matches) == limit or sum(map(len, matches)) + len(row) > 12000:
                        return {'matches': matches, 'next_offset': seen}
                    matches.append(row)
                seen += 1
        return {'matches': matches}
    raise ValueError('Unknown file operation')


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
