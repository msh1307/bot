"""Install the shared browser connector into an existing Oracle browser runtime."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tomllib

FILES = ('browser_remote.py', 'browser_broker.py', 'browser_mcp.py', 'browser-tool.json',
         'browser-mcp-requirements.txt', 'verify_browser_mcp.py')


def install(root, source, expected):
    launcher = root/'browser_remote.py'
    assert hashlib.sha256(launcher.read_bytes()).hexdigest() == expected, 'Browser launcher changed; review before applying'
    config = Path.home()/'.codex/config.toml'
    original = config.read_text()
    before = tomllib.loads(original)
    assert 'evo_browser' not in before.get('mcp_servers', {}), 'MCP entry already exists; do not overwrite it'
    unit = Path.home()/'.config/systemd/user/evo-browser-broker.service'
    assert not unit.exists(), 'Service already exists; review before applying'
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = root/'backups'/('mcp-'+stamp)
    backup.mkdir(parents=True, mode=0o700)
    shutil.copy2(config, backup/'config.toml'); (backup/'config.toml').chmod(0o600)
    existing = {}
    for name in FILES:
        dest = root/name
        if dest.exists():
            shutil.copy2(dest, backup/name)
            existing[name] = hashlib.sha256(dest.read_bytes()).hexdigest()
    env = {**os.environ, 'XDG_RUNTIME_DIR': '/run/user/'+str(os.getuid())}
    entry = {'command': str(root/'mcp-venv/bin/python'), 'args': [str(root/'browser_mcp.py')],
             'startup_timeout_sec': 20, 'tool_timeout_sec': 120}
    try:
        for name in FILES:
            raw = (source/name).read_bytes()
            if name.endswith('.py'): compile(raw, name, 'exec')
            temp = (root/name).with_suffix('.mcp-tmp')
            temp.write_bytes(raw); temp.chmod(0o600); os.replace(temp, root/name)
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text('[Unit]\nDescription=Shared EVO browser broker\n\n[Service]\nType=simple\n'
            + 'ExecStart='+str(root/'venv/bin/python')+' '+str(root/'browser_broker.py')+'\n'
            + 'WorkingDirectory='+str(root)+'\nRestart=on-failure\nRestartSec=2\nUMask=0077\n'
            + 'KillMode=mixed\nTimeoutStopSec=20\n\n[Install]\nWantedBy=default.target\n')
        subprocess.run(['systemctl', '--user', 'daemon-reload'], env=env, check=True)
        subprocess.run(['systemctl', '--user', 'enable', '--now', unit.name], env=env, check=True)
        subprocess.run(['systemctl', '--user', 'is-active', unit.name], env=env, check=True)
        addition = '\n[mcp_servers.evo_browser]\n' + ''.join(k+' = '+json.dumps(v)+'\n' for k,v in entry.items())
        config.write_text(original.rstrip()+'\n'+addition); config.chmod(0o600)
        after = tomllib.loads(config.read_text())
        assert after['mcp_servers'].pop('evo_browser') == entry
        if 'mcp_servers' not in before: after.pop('mcp_servers')
        assert after == before, 'Unrelated Codex settings changed'
    except BaseException:
        subprocess.run(['systemctl', '--user', 'disable', '--now', unit.name], env=env, capture_output=True)
        unit.unlink(missing_ok=True)
        subprocess.run(['systemctl', '--user', 'daemon-reload'], env=env, capture_output=True)
        shutil.copy2(backup/'config.toml', config)
        for name in FILES:
            if name in existing: shutil.copy2(backup/name, root/name)
            else: (root/name).unlink(missing_ok=True)
        raise
    report = {'backup': str(backup), 'before_sha256': existing,
        'after_sha256': {n: hashlib.sha256((root/n).read_bytes()).hexdigest() for n in FILES},
        'config_before_sha256': hashlib.sha256(original.encode()).hexdigest(),
        'config_after_sha256': hashlib.sha256(config.read_bytes()).hexdigest(),
        'unrelated_codex_config_preserved': True, 'mcp_server': entry,
        'profile': str(root/'profile'), 'external_port_opened': False,
        'service': unit.name, 'unit': str(unit), 'installed_at': stamp}
    (root/'mcp-install.json').write_text(json.dumps(report, indent=2))
    print(json.dumps({'backup': str(backup), 'registered': 'evo_browser', 'shared_profile': True}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--expected-launcher', required=True)
    args = parser.parse_args()
    install(args.root.resolve(), args.source.resolve(), args.expected_launcher)
