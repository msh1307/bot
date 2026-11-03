"""Apply only EVO's RPC helper to an existing authenticated Codex installation."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import py_compile
import shutil
import tempfile
from datetime import datetime, timezone


def apply(target, check=False):
    source = Path(__file__).with_name('codex-rpc.py')
    with tempfile.TemporaryDirectory() as temporary:
        py_compile.compile(str(source), cfile=str(Path(temporary) / 'helper.pyc'), doraise=True)
    target = Path(target).expanduser()
    if not target.is_absolute() or target.is_symlink():
        raise ValueError('Target must be an absolute regular-file path, not a symlink')
    digest = lambda data: hashlib.sha256(data).hexdigest()
    data = source.read_bytes()
    before = digest(target.read_bytes()) if target.exists() else None
    result = {'target': str(target), 'before_sha256': before, 'after_sha256': digest(data),
              'changed': before != digest(data), 'check_only': check}
    if check or not result['changed']:
        return result
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        backup = target.with_name(target.name + '.bak-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
        shutil.copy2(target, backup)
        result['backup'] = str(backup)
    descriptor, name = tempfile.mkstemp(dir=target.parent, prefix='.evo-rpc-')
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, 0o755)
        os.replace(name, target)
    finally:
        Path(name).unlink(missing_ok=True)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', default=str(Path.home() / 'bin/codex-rpc'))
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    print(json.dumps(apply(args.target, args.check)))
