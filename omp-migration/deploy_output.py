"""Deploy only reviewed output-budget files; preserve one idle conversation in RAM.

Root: python deploy_output.py CANDIDATE_DIR
Existing rpc/config hashes must match this review. Host edits are one exact replacement.
"""
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import sys
import time
import urllib.request

ROOT = Path('/opt/evo-omp')
RUN = Path('/run/evo-omp')
EXPECTED = {
    'evo_omp/rpc.py': 'ef86eaf104c2051b6a4220b1fd59691172052d629790900f652be1a4ce843e07',
    'config.yml': '44887222fe1aca4631ba60e86c06ee637d6b4e089578bb946a30eb1b7d74364d',
}


def digest(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def command(*args): return subprocess.run(args, check=True, capture_output=True, text=True).stdout


def idle():
    status = json.loads((RUN / 'status.json').read_text())
    if (status.get('active') or status.get('queued') or status.get('pending_inputs')
            or status.get('sessions') != 1):
        raise RuntimeError('Requires exactly one idle OMP conversation')
    with urllib.request.urlopen('http://127.0.0.1:8080/evo/status', timeout=5) as r:
        state = json.load(r)
    if state.get('busy') or state.get('loading') or state.get('maintenance'):
        raise RuntimeError('Inference server is busy')


def main():
    assert os.geteuid() == 0
    candidate = Path(sys.argv[1])
    for name, expected in EXPECTED.items():
        if digest(ROOT / name) != expected: raise RuntimeError('Live source changed: ' + name)
    if (ROOT / 'evo_omp/tool_output.py').exists(): raise RuntimeError('Output module already exists')
    changes = {name: (candidate / Path(name).name).read_bytes()
               for name in ('evo_omp/rpc.py', 'evo_omp/tool_output.py', 'config.yml')}
    host = ROOT / 'evo_omp/integrations/host.py'
    before_host = host.read_bytes()
    old = b"answer['output'] = redact_sensitive_text(answer.get('output', ''))[:12000]"
    new = b"answer['output'] = redact_sensitive_text(answer.get('output', ''))"
    if before_host.count(old) != 1: raise RuntimeError('Host output code differs from reviewed version')
    changes['evo_omp/integrations/host.py'] = before_host.replace(old, new)
    for name, data in changes.items():
        if name.endswith('.py'): compile(data, name, 'exec')
    idle()
    transcripts = list(RUN.glob('profiles/*/sessions/*.jsonl'))
    if len(transcripts) != 1: raise RuntimeError('Ambiguous active transcript')
    transcript = transcripts[0]
    model = None
    for line in transcript.read_text().splitlines():
        event = json.loads(line)
        if event.get('type') == 'model_change': model = event.get('model', '').removeprefix('evo/')
    logs = command('journalctl', '-u', 'evo-omp', '-n', '300', '--no-pager', '-o', 'cat')
    mappings = re.findall(r'maintenance_session_restored channel=(\d+) model=([^ ]+)', logs)
    if not mappings or mappings[-1][1] != model: raise RuntimeError('Unconfirmed conversation mapping')
    channel = mappings[-1][0]
    ident = 'output-maintenance-' + str(time.time_ns())
    backup = Path('/run') / ('evo-' + ident); backup.mkdir(mode=0o700)
    shutil.copytree(transcript.parent, backup / 'sessions')
    before = transcript.read_bytes()
    if (backup / 'sessions' / transcript.name).read_bytes() != before:
        raise RuntimeError('Backup mismatch')
    code_backup = ROOT / 'backups' / ident; code_backup.mkdir(mode=0o700)
    old_files = {}
    for name in changes:
        target = ROOT / name
        old_files[name] = target.read_bytes() if target.exists() else None
        if target.exists():
            dest = code_backup / name; dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, dest)
    user = pwd.getpwnam('evo-hermes')
    def restore():
        RUN.mkdir(mode=0o700, exist_ok=True)
        shutil.copytree(backup / 'sessions', RUN / 'recovery')
        manifest = RUN / 'recovery.json'
        manifest.write_text(json.dumps({'channel': channel, 'model': model}))
        for p in [RUN, *RUN.rglob('*')]:
            os.chown(p, user.pw_uid, user.pw_gid); p.chmod(0o700 if p.is_dir() else 0o600)
        command('systemctl', 'start', 'evo-omp')
        end = time.monotonic() + 60
        while manifest.exists() and time.monotonic() < end: time.sleep(1)
        copied = list(RUN.glob('profiles/*/sessions/' + transcript.name))
        if (manifest.exists() or len(copied) != 1 or not copied[0].read_bytes().startswith(before)
                or command('systemctl', 'is-active', 'evo-omp').strip() != 'active'):
            raise RuntimeError('Conversation recovery failed; RAM backup retained')
    idle()
    if transcript.read_bytes() != before or host.read_bytes() != before_host:
        raise RuntimeError('State changed; defer deployment')
    command('systemctl', 'stop', 'evo-omp')
    try:
        for name, data in changes.items():
            target = ROOT / name
            temp = target.with_suffix(target.suffix + '.output-new')
            temp.write_bytes(data); temp.chmod(0o644); temp.replace(target)
        restore()
    except BaseException:
        command('systemctl', 'stop', 'evo-omp')
        for name, data in old_files.items():
            if data is None: (ROOT / name).unlink(missing_ok=True)
            else: shutil.copy2(code_backup / name, ROOT / name)
        restore()
        raise
    result = {'files': {n: digest(ROOT / n) for n in changes}, 'session_restored': True,
              'history_prefix_preserved': True, 'model_restarted': False,
              'code_backup': str(code_backup), 'conversation_backup_storage': 'tmpfs'}
    (candidate / 'results' / 'deployment.json').write_text(json.dumps(result, indent=2))
    shutil.rmtree(backup)
    print(json.dumps(result), flush=True)


if __name__ == '__main__': main()
