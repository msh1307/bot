"""Independent Web UI Python bridge; no Discord, credentials or host tools."""
from pathlib import Path
import signal
import sys

from . import workspace, python_bridge as bridge

WORKSPACE = Path('/var/lib/evo-bot/workspace')


def sandbox_argv(command):
    if WORKSPACE.is_symlink() or WORKSPACE.resolve() != WORKSPACE:
        raise RuntimeError('Unexpected workspace path')
    return ['/usr/bin/bwrap', '--die-with-parent', '--unshare-all', '--share-net',
            '--new-session', '--cap-drop', 'ALL', '--clearenv',
            '--ro-bind', '/usr', '/usr', '--ro-bind', '/lib', '/lib',
            '--ro-bind', '/lib64', '/lib64', '--symlink', 'usr/bin', '/bin',
            '--symlink', 'usr/sbin', '/sbin', '--proc', '/proc', '--dev', '/dev',
            '--ro-bind', '/opt/evo-python', '/opt/evo-python',
            '--ro-bind', '/opt/evo-web-deps/python', '/opt/evo-web-deps/python',
            '--ro-bind', '/opt/evo-bot/sandbox-etc', '/etc',
            '--ro-bind', '/etc/resolv.conf', '/etc/resolv.conf',
            '--ro-bind', '/etc/hosts', '/etc/hosts',
            '--ro-bind', '/etc/nsswitch.conf', '/etc/nsswitch.conf',
            '--ro-bind', '/etc/ssl/certs', '/etc/ssl/certs',
            '--bind', str(WORKSPACE), '/workspace',
            '--bind', str(WORKSPACE), '/tmp', '--chdir', '/workspace',
            '--setenv', 'HOME', '/workspace/.home',
            '--setenv', 'USER', 'sandbox', '--setenv', 'LANG', 'C.UTF-8',
            '--setenv', 'PATH', '/opt/evo-python/venv/bin:/usr/bin:/bin',
            '--setenv', 'TMPDIR', '/workspace/.evo-tmp',
            '--setenv', 'OPENBLAS_NUM_THREADS', '2',
            '--setenv', 'OMP_NUM_THREADS', '2',
            '--setenv', 'GIT_TERMINAL_PROMPT', '0',
            '/bin/bash', '--noprofile', '--norc', '-o', 'pipefail', '-c', command]


def main():
    # The existing bridge calls this helper only after stopping its kernel.
    sys.modules['evo_workspace_reset'] = workspace
    bridge.SOCKET = Path('/run/evo-web-python/python.sock')
    bridge.start_bridge(sandbox_argv)

    def stop(signum, frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while True:
            signal.pause()
    finally:
        bridge.stop_kernel()
        bridge.SERVER.server_close()
        bridge.SOCKET.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
