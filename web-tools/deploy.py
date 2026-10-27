"""Deploy the reviewed web module on EVO, preserving an idle OMP conversation in RAM.

Run as root with candidate path and expected current SHA256. Refuses active work.
Does not touch model processes, credentials, sandbox settings or retired services.
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

TARGET = Path('/var/lib/evo-hermes/hermes/plugins/evo-web/__init__.py')
RUNTIME = Path('/run/evo-omp')


def command(*argv):
    return subprocess.run(argv, check=True, capture_output=True, text=True).stdout


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def idle():
    status = json.loads((RUNTIME / 'status.json').read_text())
    if status.get('active') or status.get('queued') or status.get('pending_inputs'):
        raise RuntimeError('OMP work is active or pending; defer deployment')
    if status.get('sessions') != 1:
        raise RuntimeError('This handoff requires exactly one idle OMP session')
    with urllib.request.urlopen('http://127.0.0.1:8080/evo/status', timeout=5) as r:
        state = json.load(r)
    if state.get('busy') or state.get('loading') or state.get('maintenance'):
        raise RuntimeError('Inference server is in use; defer deployment')


def main():
    candidate, expected = Path(sys.argv[1]), sys.argv[2]
    assert os.geteuid() == 0
    if digest(TARGET) != expected:
        raise RuntimeError('Live source changed since review')
    compile(candidate.read_text(), str(candidate), 'exec')
    idle()
    profiles = list(RUNTIME.glob('profiles/*/sessions/*.jsonl'))
    if len(profiles) != 1: raise RuntimeError('Ambiguous session transcript')
    transcript = profiles[0]
    model = None
    for line in transcript.read_text().splitlines():
        event = json.loads(line)
        if event.get('type') == 'model_change': model = event.get('model', '').removeprefix('evo/')
    journal = command('journalctl', '-u', 'evo-omp', '-n', '300', '--no-pager', '-o', 'cat')
    restored = re.findall(r'maintenance_session_restored channel=(\d+) model=([^ ]+)', journal)
    if not restored or restored[-1][1] != model:
        raise RuntimeError('Cannot confirm channel/model recovery mapping')
    channel = restored[-1][0]
    uid, gid = pwd.getpwnam('evo-hermes').pw_uid, pwd.getpwnam('evo-hermes').pw_gid
    backup = Path('/run') / ('evo-web-maintenance-' + str(time.time_ns()))
    backup.mkdir(mode=0o700)
    shutil.copytree(transcript.parent, backup / 'sessions')
    before = digest(transcript)
    if digest(backup / 'sessions' / transcript.name) != before:
        raise RuntimeError('Session backup mismatch')
    shutil.copy2(TARGET, backup / 'web.py')
    persistent_backup = Path('/opt/evo-omp/backups') / backup.name
    persistent_backup.mkdir(mode=0o700)
    shutil.copy2(TARGET, persistent_backup / 'web.py')  # code only; no conversation on SSD
    idle()
    if digest(transcript) != before: raise RuntimeError('Conversation changed; defer deployment')
    stopped = False
    installed = False
    try:
        command('systemctl', 'stop', 'evo-omp')
        stopped = True
        command('systemctl', 'stop', 'evo-web-tools')
        temporary = TARGET.with_name('__init__.py.new')
        shutil.copyfile(candidate, temporary)
        stat = TARGET.stat()
        os.chown(temporary, stat.st_uid, stat.st_gid)
        temporary.chmod(stat.st_mode & 0o777)
        temporary.replace(TARGET)
        installed = True
        # RuntimeDirectoryPreserve=no removes the old directory during stop.
        RUNTIME.mkdir(mode=0o700, exist_ok=True)
        shutil.copytree(backup / 'sessions', RUNTIME / 'recovery')
        manifest = RUNTIME / 'recovery.json'
        manifest.write_text(json.dumps({'channel': channel, 'model': model}))
        for path in [RUNTIME, *RUNTIME.rglob('*')]:
            os.chown(path, uid, gid)
            path.chmod(0o700 if path.is_dir() else 0o600)
        command('systemctl', 'start', 'evo-omp')
        stopped = False
        deadline = time.monotonic() + 60
        while manifest.exists() and time.monotonic() < deadline:
            time.sleep(1)
        if manifest.exists(): raise RuntimeError('OMP recovery did not complete; RAM backup retained')
        active = command('systemctl', 'is-active', 'evo-omp').strip()
        copied = list(RUNTIME.glob('profiles/*/sessions/' + transcript.name))
        if active != 'active' or len(copied) != 1:
            raise RuntimeError('Missing restored OMP session; RAM backup retained')
        # OMP may append new configuration events; original history must be an exact prefix.
        if not copied[0].read_bytes().startswith((backup / 'sessions' / transcript.name).read_bytes()):
            raise RuntimeError('Restored history mismatch; RAM backup retained')
        result = {'deployed_sha256': digest(TARGET), 'previous_sha256': expected,
                  'session_restored': True, 'history_prefix_preserved': True,
                  'session_backup_storage': 'tmpfs', 'model_restarted': False,
                  'code_backup': str(persistent_backup)}
        print(json.dumps(result), flush=True)
        # Only our verified private backup, once recovery has succeeded.
        shutil.rmtree(backup)
    except BaseException:
        if installed:
            shutil.copy2(backup / 'web.py', TARGET)
            command('systemctl', 'stop', 'evo-web-tools')
        if stopped: command('systemctl', 'start', 'evo-omp')
        print('Deployment incomplete. Private RAM recovery backup: ' + str(backup), file=sys.stderr)
        raise


if __name__ == '__main__': main()
