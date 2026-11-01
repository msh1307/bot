#!/opt/evo-omp/venv/bin/python
"""OpenSSH-only password pipe; no password in argv, environment or tool output."""
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from evo_omp.integrations.credential_input import private_text


def main():
    try:
        if len(sys.argv) != 2 or not sys.argv[1].rstrip().lower().endswith('password:'):
            return 1
        password = private_text(os.environ['EVO_SSH_PASSWORD_FILE']).rstrip('\r\n')
        if not 1 <= len(password) <= 2000 or any(ord(c) < 32 or ord(c) == 127 for c in password):
            return 1
        sys.stdout.write(password + '\n')
        return 0
    except Exception:
        return 1  # Never expose secret-file contents or library exception text.


if __name__ == '__main__':
    raise SystemExit(main())
