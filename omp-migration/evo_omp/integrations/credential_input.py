"""Enter a registered secret without returning it to the agent or clipboard.

This reduces secret exposure in tool history; it does not confine an agent
that already controls an authenticated browser, desktop or approved root job.
"""
import base64
import hashlib
import hmac
import os
from pathlib import Path
import re
import stat
import time
from urllib.parse import urlsplit

from .. import context
from . import browser
from . import remote_config

CONFIG = remote_config.CONFIG


def private_text(path):
    """No shell/env lookup, symlink, public file or unbounded secret read."""
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'r') as source:
            info = os.fstat(source.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.geteuid())
                    or info.st_mode & 0o137 or info.st_size > 16384):
                raise ValueError
            value = source.read(16385)
            if len(value) > 16384:
                raise ValueError
            return value
    except (OSError, ValueError, UnicodeError):
        raise ValueError('Private credential file unavailable or permissions unsafe') from None


def account_document():
    try:
        data = remote_config.document(CONFIG)
        if data is None:
            return {'instances': {}, 'accounts': {}}
        validate_accounts({'accounts': data.get('accounts', {})})
        return data
    except (ValueError, TypeError, KeyError):
        raise ValueError('Invalid private credential configuration') from None


def accounts():
    return account_document().get('accounts', {})


def validate_accounts(data):
    if not isinstance(data, dict) or set(data) != {'accounts'} or not isinstance(data['accounts'], dict):
        raise ValueError('Expected accounts object')
    for name, entry in data['accounts'].items():
        email = isinstance(name, str) and re.fullmatch(r'[^@\s<>]{1,64}@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}', name)
        if (not isinstance(name, str) or len(name) > 254 or not isinstance(entry, dict)
                or not (email or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', name))):
            raise ValueError('Use an account ID or email')
        if set(entry) - {'username', 'origins', 'password_file', 'totp_file', 'identity_file',
                         'sudo_password_file', 'rdp_instances', 'ssh_instances'}:
            raise ValueError('Unknown credential field')
        user = entry.get('username', name if email else '')
        if not isinstance(user, str) or not 1 <= len(user) <= 254 or any(ord(c) < 32 or ord(c) == 127 for c in user):
            raise ValueError('Named accounts need a username')
        for scope in ('rdp_instances', 'ssh_instances'):
            if (not isinstance(entry.get(scope, []), list) or any(not isinstance(v, str)
                    or not re.fullmatch(r'[A-Za-z0-9_-]{1,48}', v) for v in entry.get(scope, []))):
                raise ValueError('Invalid credential instance scope')
        if not isinstance(entry.get('origins', []), list):
            raise ValueError('Expected HTTPS origins')
        for origin in entry.get('origins', []):
            if not isinstance(origin, str): raise ValueError('Invalid origin')
            parsed = urlsplit(origin)
            if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                    or parsed.path or parsed.query or parsed.fragment or parsed.netloc != parsed.hostname
                    + (':' + str(parsed.port) if parsed.port else '')):
                raise ValueError('Use exact HTTPS origins')
        for key in ('password_file', 'totp_file', 'identity_file', 'sudo_password_file'):
            if key in entry and (not isinstance(entry[key], str) or not Path(entry[key]).is_absolute()):
                raise ValueError('Credential files must have absolute paths')
    return data['accounts']


def resolve_connection(protocol, instance, settings):
    """Resolve account references privately; listing/configuration never reads secret values."""
    if (protocol not in ('ssh', 'rdp') or not isinstance(settings.get('account'), str)
            or set(settings) & {'username', 'password_file', 'identity_file', 'sudo_password_file'}):
        raise ValueError('Use one account reference without inline authentication fields')
    entry = accounts().get(settings['account'])
    if entry is None or instance not in entry.get(protocol + '_instances', []):
        raise ValueError('Account is not registered for this instance/protocol')
    keys = ('password_file', 'identity_file', 'sudo_password_file') if protocol == 'ssh' else ('password_file',)
    result = dict(settings, username=entry.get('username', settings['account']))
    result.update({key: entry[key] for key in keys if key in entry})
    if (protocol == 'ssh' and ('password_file' in result) == ('identity_file' in result)
            or protocol == 'rdp' and 'password_file' not in result):
        raise ValueError('Account lacks one usable authentication method')
    return result


def account_metadata(entries):
    return [{'id': name, 'username': entry.get('username', name)} for name, entry in sorted(entries.items())]


def totp(seed, at=None, digits=6):
    # RFC 6238: standard authenticator SHA-1 / 30s. No OTP/seed in results.
    normalized = ''.join(seed.split()).upper()
    if not re.fullmatch(r'[A-Z2-7]{16,1024}={0,6}', normalized):
        raise ValueError('Invalid TOTP seed')
    try:
        key = base64.b32decode(normalized + '=' * (-len(normalized) % 8))
    except ValueError:
        raise ValueError('Invalid TOTP seed') from None
    counter = int(time.time() if at is None else at) // 30
    digest = hmac.new(key, counter.to_bytes(8, 'big'), hashlib.sha1).digest()
    offset = digest[-1] & 15
    number = int.from_bytes(digest[offset:offset + 4], 'big') & 0x7fffffff
    return str(number % (10 ** digits)).zfill(digits)


def credential_input(args):
    turn = context.owner_message()
    if turn is None:
        raise PermissionError('Credential input requires an owner Discord message')
    if not isinstance(args, dict):
        raise ValueError('Use list or enter')
    action = args.get('action')
    if action not in ('list', 'enter'):
        raise ValueError('Use list or enter')
    if action == 'list':
        if set(args) - {'action', 'instance'}:
            raise ValueError('list takes only optional RDP instance')
        if 'instance' in args:
            from .instances import instance_id
            ident = instance_id(args)
            if ident is None:
                raise ValueError('Choose a registered RDP instance')
            return {'accounts': account_metadata({name: entry for name, entry in accounts().items()
                                                  if ident in entry.get('rdp_instances', [])})}
        return {'accounts': account_metadata({name: entry for name, entry in accounts().items() if entry.get('origins')})}
    field = args.get('field', 'password')
    if (set(args) - {'action', 'account', 'field', 'selector', 'instance', 'frame'}
            or field not in ('password', 'otp')
            or not isinstance(args.get('account'), str)):
        raise ValueError('Use account=ID from list, optional field=password|otp and browser selector')
    entry = accounts().get(args['account'])
    if entry is None:
        raise ValueError('Unknown account; use remote_control protocol=rdp action=accounts' if 'instance' in args
                         else 'Unknown account; use credential_input action=list')
    if 'instance' in args or 'frame' in args:
        return enter_rdp(args, entry, turn, field)
    selector = args.get('selector', ':focus')
    if (not isinstance(selector, str) or not 1 <= len(selector) <= 2000 or not entry.get('origins')):
        raise ValueError('Browser input needs a login field and registered HTTPS origins')
    path = entry.get('password_file' if field == 'password' else 'totp_file')
    if not path:
        raise ValueError('That credential field is not configured')
    if turn.cancelled.is_set():
        raise InterruptedError('Credential input interrupted')
    value = private_text(path).rstrip('\r\n')
    if field == 'otp':
        value = totp(value)
    if not 1 <= len(value) <= 2000 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('Credential must be 1..2000 characters without control keys')
    try:
        browser.CLIENT.enter_secret(selector, entry['origins'], field, value, turn)
    except InterruptedError:
        raise InterruptedError('Credential input interrupted; inspect before retrying') from None
    except Exception:
        # Transport/library failures may contain the input. Never propagate them.
        raise ValueError('Credential input not confirmed; check target before retrying') from None
    finally:
        del value
    return {'entered': True}  # No automatic submit, screenshot, URL or worker echo.


def enter_rdp(args, entry, turn, field):
    from . import host, rdp
    from .instances import instance_id
    ident = instance_id(args)
    frame = args.get('frame')
    if ('selector' in args or not ident or ident not in entry.get('rdp_instances', [])
            or not isinstance(frame, str) or not frame):
        raise ValueError('RDP secret input needs an allowed instance and latest frame, without selector')
    path = entry.get('password_file' if field == 'password' else 'totp_file')
    if not path:
        raise ValueError('That credential field is not configured')
    # Hold the connection stable while the owner reviews the destination, not the value.
    with rdp.CLIENT.lock:
        if (not rdp.CLIENT.running() or rdp.CLIENT.server != ident
                or rdp.configuration().get('enabled') is not True):
            raise ValueError('Connect to the allowed RDP instance and observe the input field first')
        key = host.root_command_key('credential:' + turn.key, turn.id,
                                    {'command': args['account'] + ':' + field, 'cwd': ident + ':' + frame})
        refusal = host.approve_once(turn, f"RDP {ident}\nAccount: {args['account']}\nField: {field}\nFrame: {frame}",
            'Type into the currently focused field. RDP cannot verify its site or whether it hides text. No submit.',
            key, kind='credential')
        if refusal:
            return refusal
        value = private_text(path).rstrip('\r\n')
        if field == 'otp':
            value = totp(value)
        if not 1 <= len(value) <= 2000 or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError('Credential must be 1..2000 characters without control keys')
        try:
            if turn.cancelled.is_set() or not host.discord_connected():
                raise InterruptedError
            host.remember_root_execution(key)
            # Private backend pipe + Unicode key events; no clipboard or model-visible text.
            rdp.CLIENT.call({'action': 'type', 'server': ident, 'frame': frame, 'text': value}, turn)
        except InterruptedError:
            raise InterruptedError('Secret input interrupted; inspect before retrying') from None
        except Exception:
            raise ValueError('Secret input not confirmed; inspect before retrying') from None
        finally:
            del value
    return {'entered': True}


def register(ctx):
    ctx.register_tool('credential_input', 'evo_credentials', {
        'name': 'credential_input',
        'description': 'list shows registered account IDs/usernames; enter(account=ID) fills a password/OTP privately. '
            'Built-in browser: focus login field or give selector; HTTPS origin/field checked. '
            'Default field=password; OTP needs field=otp. '
            'No clipboard, screenshot, submit or retry. Navigate separately; never read secrets via shell/JS. '
            'Owner Discord only; no scheduled input.',
        'parameters': {'type': 'object', 'properties': {
            'action': {'type': 'string', 'enum': ['list', 'enter']},
            'account': {'type': 'string', 'maxLength': 254, 'description': 'Exact account id from list.'},
            'field': {'type': 'string', 'enum': ['password', 'otp']},
            'selector': {'type': 'string', 'maxLength': 2000, 'description': 'Optional; defaults to the focused field.'}},
            'required': ['action'], 'additionalProperties': False}}, credential_input)
