"""One-way switch with automatic rollback if Discord cannot become ready."""
import datetime
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import time

MANIFEST = Path('/etc/evo-omp/migration.json')

def command(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout

def restore(manifest):
    subprocess.run(['systemctl', 'disable', '--now', 'evo-omp.service'], capture_output=True)
    subprocess.run(['systemctl', 'disable', '--now', 'evo-web-python.service'], capture_output=True)
    for original, archived in reversed(manifest['paths']):
        source, target = Path(original), Path(archived)
        if source.is_symlink() and source.resolve() == target.resolve():
            source.unlink()
            shutil.move(str(target), str(source))
        elif not source.exists() and target.exists():
            shutil.move(str(target), str(source))
        elif source.exists() and target.exists():
            raise RuntimeError('Restore path collision; original retained: ' + str(source))
    command('systemctl', 'daemon-reload')
    command('systemctl', 'enable', '--now', 'evo-hermes.service')
    print('Hermes restored; OMP stopped. Shared workspace was not modified.')

if __name__ == '__main__':
    import sys
    if os.geteuid() != 0:
        raise SystemExit('Run as root')
    if '--rollback' in sys.argv:
        restore(json.loads(MANIFEST.read_text()))
        raise SystemExit(0)
    if MANIFEST.exists():
        raise SystemExit('Migration manifest already exists; inspect before re-running')
    old_status = Path('/var/lib/evo-hermes/hermes/gateway_state.json')
    if old_status.exists() and int(json.loads(old_status.read_text()).get('active_agents', 0)):
        raise SystemExit('Hermes has an active task; wait for it to finish before switching')
    archive = Path('/opt/evo-retired') / ('hermes-' + datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d-%H%M%S'))
    archive.mkdir(parents=True, mode=0o750)
    shutil.chown(archive, user='root', group='evo-hermes')
    paths = [('/opt/evo-hermes', str(archive / 'runtime')),
             ('/var/lib/evo-hermes/hermes', str(archive / 'home')),
             ('/etc/evo-hermes', str(archive / 'credentials'))]
    for original, archived in paths:
        if Path(original).is_symlink() or not Path(original).is_dir():
            raise SystemExit('Unexpected original directory: ' + original)
    shutil.copytree('/etc/systemd/system/evo-hermes.service.d', archive / 'service.d')
    shutil.copyfile('/etc/systemd/system/evo-hermes.service', archive / 'evo-hermes.service')
    status = Path('/run/evo-omp/status.json')
    status.unlink(missing_ok=True)
    command('systemctl', 'stop', 'evo-hermes.service')
    manifest = {'archive': str(archive), 'paths': paths, 'engine': 'omp-18.11.3',
                'workspace': '/var/lib/evo-hermes/workspace'}
    MANIFEST.write_text(json.dumps(manifest, indent=2) + '\n')
    MANIFEST.chmod(0o600)
    try:
        for original, archived in paths:
            shutil.move(original, archived)
            Path(original).symlink_to(archived, target_is_directory=True)
        command('systemctl', 'disable', 'evo-hermes.service')
        python_socket = Path('/var/lib/evo-hermes/python.sock')
        if python_socket.is_symlink() or python_socket.exists():
            if not python_socket.is_symlink() and not stat.S_ISSOCK(python_socket.lstat().st_mode):
                raise RuntimeError('Unexpected Python socket path; refusing replacement')
            python_socket.unlink()
        python_socket.symlink_to('/run/evo-web-python/python.sock')
        command('systemctl', 'daemon-reload')
        command('systemctl', 'enable', '--now', 'evo-web-python.service')
        command('systemctl', 'enable', '--now', 'evo-omp.service')
        for _ in range(60):
            if status.exists():
                state = json.loads(status.read_text())
                if state.get('state') == 'connected':
                    print('OMP Discord connected; Hermes archived at ' + str(archive))
                    break
            time.sleep(1)
        else:
            raise RuntimeError('OMP Discord did not become ready in 60 seconds')
    except Exception:
        restore(manifest)
        raise
