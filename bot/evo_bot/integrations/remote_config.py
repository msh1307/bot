"""One private remote configuration; protocol adapters keep existing transports."""
import copy
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

CONFIG = Path('/etc/evo-bot/remote-control.json')
PROTOCOLS = {'ssh', 'rdp', 'codex'}


def document(path=CONFIG):
    from .credential_input import private_text
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        return None
    data = json.loads(private_text(path))
    if not isinstance(data, dict):
        raise ValueError('Remote configuration must be an object')
    validate(data)
    return data


def validate(data):
    if (set(data) - {'defaults', 'instances', 'accounts', 'browser'}
            or not isinstance(data.get('instances'), dict)
            or not isinstance(data.get('defaults', {}), dict)
            or set(data.get('defaults', {})) - PROTOCOLS):
        raise ValueError('Remote configuration uses defaults, instances and accounts')
    for ident, entry in data['instances'].items():
        if (not isinstance(ident, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,48}', ident)
                or not isinstance(entry, dict) or set(entry) - {'host', 'name', *PROTOCOLS}
                or not any(p in entry for p in PROTOCOLS)):
            raise ValueError('Invalid remote instance')
        for protocol in PROTOCOLS & entry.keys():
            settings = entry[protocol]
            if (not isinstance(settings, dict) or type(settings.get('enabled', True)) is not bool
                    or set(settings) & {'host', 'name', 'password', 'private_key', 'token'}):
                raise ValueError('Protocol settings use file references; host/name belong to the instance')
            if protocol in ('ssh', 'rdp') and (not isinstance(entry.get('host'), str)
                    or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.:-]{0,252}', entry['host'])):
                raise ValueError('SSH/RDP instances require a valid host')
            if protocol == 'codex':
                host, port, path = entry.get('host'), settings.get('port', 4500), settings.get('path', '/')
                if (not isinstance(host, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.:-]{0,252}', host)
                        or type(port) is not int or not 1 <= port <= 65535
                        or not isinstance(path, str) or not path.startswith('/')
                        or any(c in path for c in '\r\n?#')
                        or 'url' in settings and urlsplit(settings['url']).hostname != host):
                    raise ValueError('Codex requires one host, valid port/path and a matching URL if supplied')
    for protocol, ident in data.get('defaults', {}).items():
        if (not isinstance(ident, str) or ident not in data['instances']
                or protocol not in data['instances'][ident]
                or data['instances'][ident][protocol].get('enabled', True) is not True):
            raise ValueError('Remote default must name an enabled instance/protocol')
    browser = data.get('browser')
    if browser is not None:
        if (not isinstance(browser, dict) or set(browser) != {'instance', 'runtime'}
                or browser.get('instance') not in data['instances']
                or 'ssh' not in data['instances'][browser['instance']]
                or data['instances'][browser['instance']]['ssh'].get('enabled', True) is not True
                or not isinstance(browser.get('runtime'), str)
                or not re.fullmatch(r'/[A-Za-z0-9_./-]{1,220}', browser['runtime'])
                or '..' in browser['runtime'].split('/')):
            raise ValueError('browser needs an enabled SSH instance and absolute runtime path')
    from .credential_input import validate_accounts
    validate_accounts({'accounts': data.get('accounts', {})})


def protocol(name, path=CONFIG):
    data = document(path)
    if data is None:
        return None
    servers = {}
    for ident, entry in data['instances'].items():
        if name not in entry or entry[name].get('enabled', True) is not True:
            continue
        settings = copy.deepcopy(entry[name])
        settings.pop('enabled', None)
        settings.update({k: entry[k] for k in ('host', 'name') if k in entry})
        if name == 'codex' and 'url' not in settings:
            host = settings['host']
            host = '[' + host + ']' if ':' in host else host
            settings['url'] = f"wss://{host}:{settings.pop('port', 4500)}{settings.pop('path', '/')}"
        servers[ident] = settings
    value = {'default': data.get('defaults', {}).get(name), 'servers': servers}
    if name == 'rdp':
        value['enabled'] = bool(servers)
    return value
