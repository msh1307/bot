"""Server-side preparation. Does not stop or start either Discord gateway."""
import json
import os
from pathlib import Path
import pwd
import shutil

ROOT = Path('/opt/evo-omp')
STATE = Path('/var/lib/evo-omp')
SECRETS = Path('/etc/evo-omp')
OLD = Path('/var/lib/evo-hermes/hermes')
uid = pwd.getpwnam('evo-hermes').pw_uid
gid = pwd.getpwnam('evo-hermes').pw_gid

def private_dir(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chown(path, uid, gid)
    path.chmod(0o700)

for p in (STATE, STATE / 'memory', Path('/run/evo-omp')):
    private_dir(p)
SECRETS.mkdir(mode=0o750, exist_ok=True)
SECRETS.chmod(0o750)
os.chown(SECRETS, 0, gid)
for name in ('discord.env', 'gmail.json', 'codex-remote.json', 'codex-server.pem'):
    src = Path('/etc/evo-hermes') / name
    dst = SECRETS / name
    if src.exists() and not dst.exists():
        shutil.copyfile(src, dst)
        os.chown(dst, 0, gid)
        dst.chmod(0o640)
# Certificates embedded in the bridge config keep working after retirement.
p = SECRETS / 'codex-remote.json'
if p.exists():
    s = p.read_text().replace('/etc/evo-hermes/', '/etc/evo-omp/')
    p.write_text(s)

etc = ROOT / 'sandbox-etc'
etc.mkdir(mode=0o755, exist_ok=True)
(etc / 'passwd').write_text(f'sandbox:x:{uid}:{gid}:Sandbox:/workspace/.home:/bin/bash\n')
(etc / 'group').write_text(f'sandbox:x:{gid}:\n')
for p in etc.iterdir():
    p.chmod(0o755 if p.is_dir() else 0o644)
for name in ('resolv.conf', 'hosts', 'nsswitch.conf'):
    (etc / name).touch()
(etc / 'ssl/certs').mkdir(parents=True, exist_ok=True)
(etc / 'fonts').mkdir(exist_ok=True)

panel = OLD / 'evo-access-panel.json'
if panel.exists() and not (STATE / panel.name).exists():
    shutil.copyfile(panel, STATE / panel.name)

# Retain on-demand documents without importing the old persona or injecting them.
memory_sources = [OLD / 'memories', OLD / 'memory']
for folder in memory_sources:
    if folder.is_dir():
        for src in folder.rglob('*.md'):
            if not src.is_symlink() and src.stat().st_size <= 256 * 1024:
                name = src.relative_to(folder).as_posix().replace('/', '__')
                dst = STATE / 'memory' / name
                if not dst.exists():
                    shutil.copyfile(src, dst)
for name in ('MEMORY.md', 'USER.md'):
    src = OLD / name
    if src.is_file() and src.stat().st_size:
        dst = STATE / 'memory' / ('legacy-' + name)
        if not dst.exists():
            shutil.copyfile(src, dst)

p = STATE / 'jobs.json'
if not p.exists():
    source = json.loads((OLD / 'cron/jobs.json').read_text())
    jobs = source['jobs'] if isinstance(source, dict) else source
    output = []
    for j in jobs:
        # Hermes schema is validated before this script is installed.
        origin = j.get('origin', {})
        output.append({k: j.get(k) for k in ('id', 'name', 'prompt', 'schedule', 'enabled',
            'next_run_at', 'last_status', 'last_run_at')} | {
            'channel': str(origin.get('chat_id', '990826359704154225')), 'running': False})
    p.write_text(json.dumps({'jobs': output}, ensure_ascii=False, indent=2) + '\n')
for p in STATE.rglob('*'):
    if p.is_file() and not p.is_symlink():
        os.chown(p, uid, gid)
        p.chmod(0o600)

for name in ('evo-omp.service', 'evo-web-python.service'):
    target = Path('/etc/systemd/system') / name
    shutil.copyfile(ROOT / name, target)
    os.chown(target, 0, 0)
    target.chmod(0o644)
print('Staged OMP code, private credentials, memory documents and schedules; gateways unchanged.')
