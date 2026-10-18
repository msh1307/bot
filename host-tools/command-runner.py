"""Started only by the broker's bounded transient systemd service."""
import json
import os
import pwd
import sys

raw = sys.stdin.buffer.readline(16385)
if len(raw) > 16384 or not raw.endswith(b'\n'):
    raise SystemExit('Invalid command input')
payload = json.loads(raw)
account = pwd.getpwuid(os.getuid())
if account.pw_name != payload['run_as']:
    raise SystemExit('Execution identity mismatch')
# Pin the approved directory inode, then chdir by fd; a replaced workspace symlink
# must not redirect an already approved relative command into a different host path.
directory = os.open(payload['cwd'], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
identity = os.fstat(directory)
if (identity.st_dev, identity.st_ino) != (payload['cwd_dev'], payload['cwd_ino']):
    raise SystemExit('Approved directory changed; request fresh approval')
os.fchdir(directory)
os.close(directory)
# Do not forward Discord keys, API keys, shell startup files or the broker environment.
environment = dict(PATH='/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
                   HOME=account.pw_dir if account.pw_uid == 0 else '/tmp',
                   USER=account.pw_name, LOGNAME=account.pw_name,
                   LANG='C.UTF-8', TMPDIR='/tmp', TZ='Asia/Seoul',
                   OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1')
os.execve('/bin/bash', ['bash', '--noprofile', '--norc', '-c', payload['command']], environment)
