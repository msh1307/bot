"""Small host-tool previews; complete text stays in the session's tmpfs files."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat

INLINE_BYTES = 8192
FIELD_BYTES = 4096
EDGE_BYTES = 1024
MAX_FILES = 64
MAX_BYTES = 16 * 2**20
TOOLS = {'host_command', 'codex_task', 'browser', 'gmail'}


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def save_text(root, text):
    """Do not announce a reference until its complete content has been saved."""
    raw = text.encode('utf-8')
    if len(raw) > MAX_BYTES:
        raise ValueError('Tool result exceeds temporary storage budget')
    parent = root.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid():
        raise ValueError('Unexpected tool-output parent')
    root.mkdir(mode=0o700, exist_ok=True)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise ValueError('Unexpected tool-output directory')
    name = hashlib.sha256(raw).hexdigest() + '.txt'
    path = root / name
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or path.read_bytes() != raw:
            raise ValueError('Stored tool output was changed')
        path.touch()
        return path
    files = []
    for old in root.iterdir():
        if re.fullmatch(r'[0-9a-f]{64}\.txt', old.name):
            info = old.lstat()
            if stat.S_ISREG(info.st_mode):
                files.append((info.st_mtime_ns, info.st_size, old))
    total = sum(size for _, size, _ in files)
    files.sort()
    while files and (len(files) >= MAX_FILES or total + len(raw) > MAX_BYTES):
        _, size, old = files.pop(0)
        old.unlink()
        total -= size
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as out:
            if out.write(raw) != len(raw):
                raise OSError('Incomplete tool output write')
        if path.stat().st_size != len(raw):
            raise OSError('Incomplete tool output file')
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def preview(text, root):
    raw = text.encode('utf-8')
    path = save_text(root, text)
    # This path remains valid across the existing sessions-directory RAM handoff.
    ref = '/home/agent/sessions/host-output/' + path.name
    head = raw[:EDGE_BYTES].decode('utf-8', errors='ignore')
    tail = raw[-EDGE_BYTES:].decode('utf-8', errors='ignore')
    return (f'[Preview of {len(raw)} returned bytes; complete returned text: {ref}. '
            'Use read with a line range or grep on this file for omitted details. '
            'Temporary session storage; old entries may expire.]\n'
            + head + '\n[... middle omitted ...]\n' + tail)


def compact_text(text, root):
    if len(text.encode('utf-8')) <= INLINE_BYTES:
        return text
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        return preview(text, root)
    # Preserve command status and identifiers while bounding large text fields.
    # Their files contain plain, exact text, so native read/grep work directly.
    def fields(item, depth=0):
        if isinstance(item, str) and len(item.encode('utf-8')) > FIELD_BYTES:
            return preview(item, root)
        if depth < 8 and isinstance(item, dict):
            return {k: fields(v, depth + 1) for k, v in item.items()}
        if depth < 8 and isinstance(item, list):
            return [fields(v, depth + 1) for v in item]
        return item
    # Avoid producing references for an arbitrarily large list of fields.
    def large_fields(item, depth=0):
        if isinstance(item, str):
            return int(len(item.encode('utf-8')) > FIELD_BYTES)
        if depth < 8 and isinstance(item, (dict, list)):
            return sum(large_fields(v, depth + 1) for v in
                       (item.values() if isinstance(item, dict) else item))
        return 0
    if isinstance(value, dict) and len(value) <= 32 and large_fields(value) <= 8:
        candidate = dumps(fields(value))
        if len(candidate.encode('utf-8')) <= INLINE_BYTES:
            return candidate
    return preview(text, root)


def compact_result(name, result, profile):
    if name not in TOOLS:
        return result  # Web pagination, user replies and memory are already explicit.
    root = Path(profile) / 'sessions' / 'host-output'
    blocks = []
    for block in result['content']:
        if block.get('type') == 'text' and isinstance(block.get('text'), str):
            block = dict(block, text=compact_text(block['text'], root))
        blocks.append(block)
    return dict(result, content=blocks)  # Images and result/error metadata are unchanged.
