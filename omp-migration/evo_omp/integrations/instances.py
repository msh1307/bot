"""Shared instance IDs; discovery never connects or changes a selected target."""
import re
from urllib.parse import urlsplit


def instance_id(args):
    if 'server' in args:
        raise ValueError('Use instance, not server')
    ident = args.get('instance')
    if ident is not None and (not isinstance(ident, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,48}', ident)):
        raise ValueError('Use an instance ID from remote_control action=list')
    return ident


def catalog():
    from . import codex, rdp, ssh
    try:
        default, configured = codex.server_config()
    except FileNotFoundError:
        default, configured = None, {}
    entries = {ident: {'id': ident, 'name': settings.get('name', ident),
                      'host': urlsplit(settings['url']).hostname,
                      'capabilities': ['codex_task', 'windows_files', 'windows_exec']}
               for ident, settings in configured.items()}
    desktop = rdp.configuration()
    for ident, settings in desktop.get('servers', {}).items():
        item = entries.setdefault(ident, {'id': ident, 'name': settings.get('name', ident),
                                         'host': settings['host'], 'capabilities': []})
        if desktop.get('enabled') is True:
            item['capabilities'].append('remote_desktop')
        else:
            item['disabled'] = ['remote_desktop']
    shell = ssh.configuration()
    for ident, settings in shell.get('servers', {}).items():
        item = entries.setdefault(ident, {'id': ident, 'name': settings.get('name', ident),
                                         'host': settings['host'], 'capabilities': []})
        item['capabilities'].append('remote_ssh')
        if settings.get('sudo_password_file') or settings.get('sudo_nopasswd'):
            item['sudo'] = 'owner_approval_once'
    return {'default': default or desktop.get('default') or shell.get('default'),
            'defaults': {'codex': default, 'rdp': desktop.get('default'), 'ssh': shell.get('default')},
            'instances': list(entries.values())}
