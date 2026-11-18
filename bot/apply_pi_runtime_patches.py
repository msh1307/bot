"""Apply the owner-selected summary prompt to an installed, versioned Pi runtime."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess


def apply(runtime, check=False):
    runtime = Path(runtime)
    if not runtime.is_absolute() or runtime.is_symlink():
        raise ValueError('Use an absolute versioned runtime directory, not a symlink')
    package = runtime / 'node_modules/@earendil-works/pi-coding-agent'
    version = json.loads((package / 'package.json').read_text())['version']
    target = package / 'dist/core/compaction/compaction.js'
    if target.is_symlink() or not target.resolve().is_relative_to(runtime.resolve()):
        raise ValueError('Patch target must stay inside the selected runtime')
    patch = Path(__file__).with_name('patches') / 'pi-compaction-summary.patch'
    digest = lambda data: hashlib.sha256(data).hexdigest()
    before, patch_hash = digest(target.read_bytes()), digest(patch.read_bytes())
    manifest = runtime / 'evo-summary-patch.json'
    if manifest.exists():
        saved = json.loads(manifest.read_text())
        if saved['after_sha256'] != before or saved['patch_sha256'] != patch_hash:
            raise RuntimeError('Patched runtime changed; inspect before applying another patch')
        return dict(saved, changed=False, check_only=check)
    argv = ['patch', '--batch', '--forward', '--fuzz=0', str(target)]
    def run(args):
        return subprocess.run(args, input=patch.read_bytes(), capture_output=True, check=True)
    run([*argv, '--dry-run'])
    report = dict(version=version, target=str(target), before_sha256=before,
                  patch_sha256=patch_hash, changed=True, check_only=check)
    if check:
        return report
    original = target.read_bytes()
    backup = target.with_name(target.name + '.evo-original')
    with backup.open('xb') as stream:
        stream.write(original)
    try:
        run(argv)
        subprocess.run(['node', '--check', str(target)], check=True, capture_output=True)
    except BaseException:
        target.write_bytes(original)
        raise
    report.update(after_sha256=digest(target.read_bytes()), backup=str(backup),
                  applied_utc=datetime.now(timezone.utc).isoformat())
    manifest.write_text(json.dumps(report, indent=2) + '\n')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('runtime')
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    print(json.dumps(apply(args.runtime, args.check), indent=2))
