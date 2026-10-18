"""Apply the reviewed five-file OMP update; restore on readiness failure."""
import ast
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import urllib.request

RUN=Path(__file__).resolve().parent
ROOT=Path('/opt/evo-omp')
LEASE=Path('/run/evo-router/maintenance.json')
UNIT=Path('/etc/systemd/system/evo-omp.service')
FILES=('evo_omp/rpc.py','evo_omp/gateway.py','config.yml','models.yml')


def command(*args):
    return subprocess.run(args,check=True,capture_output=True,text=True,timeout=90)


def router_status():
    with urllib.request.urlopen('http://127.0.0.1:8080/evo/status',timeout=10) as r:
        data=json.load(r)
    return {k:data.get(k) for k in ('active','busy','loading','maintenance','error')}


def ready():
    deadline=time.monotonic()+90
    while time.monotonic()<deadline:
        with __import__('contextlib').suppress(OSError,ValueError):
            state=json.loads(Path('/run/evo-omp/status.json').read_text())
            if state.get('state')=='connected':
                return state
        time.sleep(1)
    raise RuntimeError('Discord readiness timed out')


def atomic_copy(source,target):
    data=source.read_bytes()
    old=target.stat()
    temporary=target.with_name(target.name+'.tuning-new')
    temporary.write_bytes(data)
    os.chown(temporary,old.st_uid,old.st_gid)
    temporary.chmod(old.st_mode & 0o777)
    os.replace(temporary,target)


def main():
    if os.geteuid()!=0:
        raise SystemExit('Root maintenance access required')
    bot=json.loads(Path('/run/evo-omp/status.json').read_text())
    router=router_status()
    if bot.get('active') or bot.get('queued') or bot.get('sessions') or any(router.get(k) for k in ('busy','loading','maintenance')):
        raise RuntimeError('Production has work or a live conversation; update deferred')
    for name in FILES:
        if name.endswith('.py'):
            ast.parse((RUN/name).read_text())
    assert (RUN/'models.yml').read_text().count('contextWindow: 131072')==2
    unit=UNIT.read_text()
    assert unit.count('RuntimeDirectoryPreserve=restart')==1
    # The only unit change cleans abandoned ephemeral data on process restart.
    next_unit=RUN/'reviewed-unit.service'
    next_unit.write_text(unit.replace('RuntimeDirectoryPreserve=restart','RuntimeDirectoryPreserve=no'))
    command('systemd-analyze','verify',str(next_unit))
    backup=ROOT/'backups'/time.strftime('tuning-%Y%m%d-%H%M%S',time.gmtime())
    backup.mkdir(parents=True,mode=0o700)
    targets=[(RUN/name,ROOT/name) for name in FILES]+[(next_unit,UNIT)]
    entries=[]
    for source,target in targets:
        archived=backup/(str(target).lstrip('/'))
        archived.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(target,archived)
        entries.append({'target':str(target),'backup':str(archived),
            'before_sha256':hashlib.sha256(target.read_bytes()).hexdigest(),
            'after_sha256':hashlib.sha256(source.read_bytes()).hexdigest()})
    manifest={'backup':str(backup),'files':entries,'before':router}
    (backup/'manifest.json').write_text(json.dumps(manifest,indent=2))
    with LEASE.open('x') as lease:
        json.dump({'pid':os.getpid(),'expires':time.time()+300,'reason':'Applying reviewed OMP recovery and context update'},lease)
    try:
        current=json.loads(Path('/run/evo-omp/status.json').read_text())
        if current.get('active') or current.get('queued') or router_status().get('busy'):
            raise RuntimeError('Request raced update admission; no files changed')
        command('systemctl','stop','evo-omp')
        try:
            for source,target in targets:
                atomic_copy(source,target)
            command('systemctl','daemon-reload')
            command('systemctl','start','evo-omp')
            manifest['bot']=ready()
            for entry in entries:
                assert hashlib.sha256(Path(entry['target']).read_bytes()).hexdigest()==entry['after_sha256']
            manifest.update(deployed=True,router=router_status())
            (RUN/'deployment-results.json').write_text(json.dumps(manifest,indent=2))
            print('DEPLOYED',str(backup),flush=True)
        except BaseException:
            command('systemctl','stop','evo-omp')
            for entry in entries:
                atomic_copy(Path(entry['backup']),Path(entry['target']))
            command('systemctl','daemon-reload')
            command('systemctl','start','evo-omp')
            ready()
            print('ROLLED_BACK',str(backup),flush=True)
            raise
    finally:
        if LEASE.exists() and json.loads(LEASE.read_text()).get('pid')==os.getpid():
            LEASE.unlink()


if __name__=='__main__':
    main()
