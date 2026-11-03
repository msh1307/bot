"""SSH entry point: one private Camoufox profile, no listening browser port."""
import fcntl
import os
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parent
PROFILE = ROOT/('web-profile' if '--web' in sys.argv else 'profile')


def main():
    os.umask(0o077)
    if PROFILE.is_symlink(): raise ValueError('Unexpected profile symlink')
    PROFILE.mkdir(mode=0o700, exist_ok=True)
    if '--reset' in sys.argv:
        with (PROFILE/'.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            shutil.rmtree(PROFILE)
        print('reset', flush=True)
        return
    for name in ('profile','web-profile'):
        path = ROOT/name
        if path.is_symlink(): raise ValueError('Unexpected profile symlink')
        path.mkdir(mode=0o700,exist_ok=True)
    fonts = ROOT/'cache/camoufox/fontconfig'
    fonts.mkdir(parents=True,exist_ok=True)
    (PROFILE/'fontconfig').mkdir(mode=0o700,exist_ok=True)
    args = ['/usr/bin/bwrap', '--die-with-parent', '--unshare-all', '--share-net',
        '--new-session', '--cap-drop', 'ALL', '--clearenv',
        '--ro-bind', '/usr', '/usr', '--symlink', 'usr/lib', '/lib',
        '--symlink', 'usr/lib64', '/lib64', '--symlink', 'usr/bin', '/bin',
        '--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp', '--dir', '/run',
        '--tmpfs', '/dev/shm', '--dir', '/etc', '--ro-bind', str(ROOT), str(ROOT),
        '--bind', str(PROFILE), '/profile',
        '--bind', str(PROFILE/'fontconfig'), str(fonts), '--tmpfs', str(ROOT/'profile'),
        '--tmpfs', str(ROOT/'web-profile'), '--chdir', '/tmp']
    for name in ('resolv.conf','hosts','nsswitch.conf','fonts','pki','ssl/certs','localtime'):
        path = Path('/etc')/name
        if path.exists(): args += ['--ro-bind', str(path), str(path)]
    env = {'HOME':'/tmp', 'PATH':'/usr/bin:/bin', 'LANG':'C.UTF-8',
        'XDG_CACHE_HOME':str(ROOT/'cache'), 'PYTHONDONTWRITEBYTECODE':'1',
        'EVO_BROWSER_ENGINE':'Camoufox', 'TMPDIR':'/tmp',
        'NODE_OPTIONS':'--max-old-space-size=128'}
    for key,value in env.items(): args += ['--setenv', key, value]
    args += [str(ROOT/'venv/bin/python'), str(ROOT/('web_worker.py' if '--web' in sys.argv else 'browser_worker.py'))]
    os.execv(args[0], args)


if __name__ == '__main__': main()
