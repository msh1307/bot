"""Credential entry plus owner-agent OTP registration and code retrieval.

Passwords require one-time owner approval to disclose; seeds stay private.
This reduces secret exposure in tool history; it does not confine an agent
that already controls an authenticated browser, desktop or approved root job.
"""
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time
from urllib.parse import parse_qs, urlsplit

from .. import context
from . import browser
from . import remote_config

CONFIG = remote_config.CONFIG
OTP_CONFIG = Path('/var/lib/evo-bot/credentials/otp.json')
OTP_LOCK = threading.RLock()
DISCLOSURE_LOCK = threading.Lock()


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
    entries = account_document().get('accounts', {})
    for name, profile in otp_profiles().items():
        entries.setdefault(name, {'username': name, 'origins': profile['origins']})
    return entries


def otp_profiles():
    if not OTP_CONFIG.exists() and not OTP_CONFIG.is_symlink():
        return {}
    try:
        profiles = json.loads(private_text(OTP_CONFIG))
        if not isinstance(profiles, dict): raise ValueError
        for name, profile in profiles.items():
            if not isinstance(profile, dict) or set(profile) != {'seed', 'origins'}: raise ValueError
            validate_accounts({'accounts': {name: {'username': name, 'origins': profile['origins']}}})
            totp(profile['seed'], at=0)  # Validate without returning a code.
        return profiles
    except (ValueError, TypeError, AttributeError):
        raise ValueError('Invalid registered OTP configuration') from None


def register_otp(account, seed, origins=''):
    """Store a seed through the existing owner credential tool, outside /workspace."""
    if not isinstance(seed, str): raise ValueError('Invalid TOTP seed')
    seed = seed.strip()
    totp(seed, at=0)
    entry = {'username': account, 'origins': origins.split()}
    validate_accounts({'accounts': {account: entry}})
    existing = account_document().get('accounts', {}).get(account)
    if existing is not None:
        if entry['origins'] and set(entry['origins']) != set(existing.get('origins', [])):
            raise ValueError('기존 계정에 등록된 로그인 사이트 주소를 사용하세요.')
        entry['origins'] = existing.get('origins', [])
    with OTP_LOCK:
        profiles = otp_profiles()
        profiles[account] = {'seed': seed, 'origins': entry['origins']}
        raw = (json.dumps(profiles, ensure_ascii=False) + '\n').encode()
        if len(raw) > 16384: raise ValueError('OTP 등록 용량을 초과했습니다.')
        temporary = OTP_CONFIG.with_name('.otp-' + secrets.token_hex(8))
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'wb') as out:
                os.fchmod(out.fileno(), 0o600)
                out.write(raw); out.flush(); os.fsync(out.fileno())
            temporary.replace(OTP_CONFIG)
        finally:
            temporary.unlink(missing_ok=True)
    return {'registered': account}


def validate_accounts(data):
    if not isinstance(data, dict) or set(data) != {'accounts'} or not isinstance(data['accounts'], dict):
        raise ValueError('Expected accounts object')
    for name, entry in data['accounts'].items():
        email = isinstance(name, str) and re.fullmatch(r'[^@\s<>]{1,64}@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}', name)
        if (not isinstance(name, str) or len(name) > 254 or not isinstance(entry, dict)
                or not (email or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', name))):
            raise ValueError('Use an account ID or email')
        if set(entry) - {'username', 'origins', 'password_file', 'totp_file', 'identity_file',
                         'sudo_password_file', 'rdp_instances', 'ssh_instances', 'root_gate'}:
            raise ValueError('Unknown credential field')
        if entry.get('root_gate', 'notify_only') not in ('notify_only', 'approval_card'):
            raise ValueError('account.root_gate must be notify_only or approval_card')
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
    if protocol == 'ssh':
        result.setdefault('root_gate', entry.get('root_gate', 'notify_only'))
    if (protocol == 'ssh' and ('password_file' in result) == ('identity_file' in result)
            or protocol == 'rdp' and 'password_file' not in result):
        raise ValueError('Account lacks one usable authentication method')
    return result


def account_metadata(entries):
    registered = otp_profiles()
    return [dict(id=name, username=entry.get('username', name), **({'otp': True} if name in registered else {}))
            for name, entry in sorted(entries.items())]


def totp(seed, at=None, digits=6):
    # RFC 6238; raw Base32 uses SHA-1 / 30s, otpauth URIs retain their parameters.
    if not isinstance(seed, str) or len(seed) > 2048:
        raise ValueError('Invalid TOTP seed')
    algorithm, period = 'SHA1', 30
    if seed.strip().lower().startswith('otpauth:'):
        try:
            parsed = urlsplit(seed.strip())
            parameters = parse_qs(parsed.query, keep_blank_values=True)
        except ValueError:
            raise ValueError('Invalid TOTP URI') from None
        if (parsed.scheme != 'otpauth' or parsed.netloc != 'totp' or parsed.fragment
                or any(len(parameters.get(k, [])) != 1 for k in ('secret',)
                       + tuple(k for k in ('algorithm', 'digits', 'period') if k in parameters))):
            raise ValueError('Use a TOTP otpauth URI, not HOTP')
        seed = parameters['secret'][0]
        algorithm = parameters.get('algorithm', ['SHA1'])[0].upper()
        try:
            digits = int(parameters.get('digits', [digits])[0])
            period = int(parameters.get('period', [30])[0])
        except (ValueError, TypeError):
            raise ValueError('Invalid TOTP parameters') from None
    hashes = {'SHA1': hashlib.sha1, 'SHA256': hashlib.sha256, 'SHA512': hashlib.sha512}
    if algorithm not in hashes or digits not in (6, 8) or not 1 <= period <= 300:
        raise ValueError('TOTP requires SHA1/SHA256/SHA512, 6/8 digits and a 1..300 second period')
    normalized = ''.join(seed.split()).upper()
    if not re.fullmatch(r'[A-Z2-7]{16,1024}={0,6}', normalized):
        raise ValueError('Invalid TOTP seed')
    try:
        key = base64.b32decode(normalized + '=' * (-len(normalized) % 8))
    except ValueError:
        raise ValueError('Invalid TOTP seed') from None
    counter = int(time.time() if at is None else at) // period
    digest = hmac.new(key, counter.to_bytes(8, 'big'), hashes[algorithm]).digest()
    offset = digest[-1] & 15
    number = int.from_bytes(digest[offset:offset + 4], 'big') & 0x7fffffff
    return str(number % (10 ** digits)).zfill(digits)


def secret_value(account, entry, field):
    if field == 'otp':
        return totp(otp_seed(account, entry))
    path = entry.get('password_file')
    if not path: raise ValueError('That credential field is not configured')
    return private_text(path).rstrip('\r\n')


def otp_seed(account, entry):
    profile = otp_profiles().get(account)
    if profile is not None: return profile['seed']
    path = entry.get('totp_file')
    if not path: raise ValueError('OTP is not configured for that account')
    return private_text(path).rstrip('\r\n')


def otp_sensitive_values():
    values = []
    for profile in otp_profiles().values():
        seed = profile['seed']
        params = parse_qs(urlsplit(seed).query) if seed.lower().startswith('otpauth:') else {}
        values.extend([seed, params.get('secret', [seed])[0]])
        period = int(params.get('period', [30])[0])
        values.extend(totp(seed, time.time() + n * period) for n in (-1, 0, 1))
    return values


def read_password(args, entry, turn):
    from . import host
    reason = args.get('reason')
    if (not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 160
            or any(ord(c) < 32 or ord(c) == 127 or c in '\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069' for c in reason)):
        raise ValueError('Password disclosure requires a short reason (1..160 characters, no control keys)')
    path = entry.get('password_file')
    if not path: raise ValueError('Password is not configured for that account')
    if not DISCLOSURE_LOCK.acquire(blocking=False):
        raise ValueError('Another password disclosure is awaiting approval')
    try:
        preview = f"auth read\nAccount: {args['account']}\nField: password\nReason: {reason.strip()}"
        key = host.root_command_key('auth-read:' + turn.key, turn.id, {'command': preview, 'cwd': path})
        refusal = host.approve_once(turn, preview,
            'Send this password to the local model once. It may remain in conversation history. '
            'Do not quote or forward it to external agents or services.', key, kind='credential_read')
        if refusal: return refusal
        if turn.cancelled.is_set() or not host.discord_connected():
            raise InterruptedError('Password disclosure cancelled')
        if accounts().get(args['account'], {}).get('password_file') != path:
            raise ValueError('Account configuration changed; password was not disclosed')
        value = secret_value(args['account'], entry, 'password')
        if not 1 <= len(value) <= 2000 or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError('Credential must be 1..2000 characters without control keys')
        if turn.cancelled.is_set() or not host.discord_connected():
            raise InterruptedError('Password disclosure cancelled')
        host.remember_root_execution(key)
        return {'password': value}
    finally:
        DISCLOSURE_LOCK.release()


def credential_input(args):
    turn = context.owner_message()
    if turn is None:
        raise PermissionError('Credential input requires an owner Discord message')
    if not isinstance(args, dict):
        raise ValueError('Use list, register, read or enter')
    action = args.get('action')
    if action not in ('list', 'register', 'read', 'enter'):
        raise ValueError('Use list, register, read or enter')
    if action == 'register':
        origins = args.get('origins', [])
        if (set(args) - {'action', 'account', 'field', 'seed', 'origins'}
                or args.get('field', 'otp') != 'otp' or not isinstance(args.get('account'), str)
                or not isinstance(origins, list) or any(not isinstance(v, str) for v in origins)):
            raise ValueError('register takes account, seed, optional HTTPS origins; field=otp only')
        return register_otp(args['account'], args.get('seed'), ' '.join(origins))
    if action == 'read':
        field = args.get('field', 'otp')
        if (set(args) - {'action', 'account', 'field', 'reason'} or field not in ('otp', 'password')
                or field == 'otp' and 'reason' in args
                or not isinstance(args.get('account'), str)):
            raise ValueError('read takes account; password additionally requires field=password and reason')
        entry = accounts().get(args['account'])
        if entry is None: raise ValueError('Unknown account; use list')
        if field == 'password': return read_password(args, entry, turn)
        seed = otp_seed(args['account'], entry).strip()
        now = time.time()
        code = totp(seed, at=now)
        params = parse_qs(urlsplit(seed).query) if seed.lower().startswith('otpauth:') else {}
        period = int(params.get('period', [30])[0])
        return {'otp': code, 'expires_in': period - int(now) % period}
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
        return {'accounts': account_metadata(accounts())}
    field = args.get('field', 'password')
    if (set(args) - {'action', 'account', 'field', 'selector', 'instance', 'frame'}
            or field not in ('password', 'otp')
            or not isinstance(args.get('account'), str)):
        raise ValueError('Use account=ID from list, optional field=password|otp and browser selector')
    entry = accounts().get(args['account'])
    if entry is None:
        raise ValueError('Unknown account; use remote_control protocol=rdp action=accounts' if 'instance' in args
                         else 'Unknown account; use auth action=list')
    if 'instance' in args or 'frame' in args:
        return enter_rdp(args, entry, turn, field)
    selector = args.get('selector', ':focus')
    if (not isinstance(selector, str) or not 1 <= len(selector) <= 2000 or not entry.get('origins')):
        raise ValueError('Browser input needs a login field and registered HTTPS origins')
    if turn.cancelled.is_set():
        raise InterruptedError('Credential input interrupted')
    value = secret_value(args['account'], entry, field)
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
    if not entry.get('password_file' if field == 'password' else 'totp_file') and not (
            field == 'otp' and args['account'] in otp_profiles()):
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
        value = secret_value(args['account'], entry, field)
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
    ctx.register_tool('auth', 'evo_credentials', {
        'name': 'auth',
        'description': 'list accounts; register(account,seed,origins) stores an owner-provided OTP seed; '
            'read(account) returns the current OTP and expires_in seconds, without approval. '
            'Exceptional read(account,field=password,reason) discloses the password only after owner Allow Once; prefer private enter. '
            'enter(account,field=password|otp) fills the focused browser field or selector; default=password, HTTPS origin checked. '
            'Password setup: host /etc/evo-bot/remote-control.json accounts (username,password_file,origins); see /home/agent/config/index.json. '
            'Never forward disclosed passwords to external agents/services. Seeds stay private. No auto-submit. Owner Discord only.',
        'parameters': {'type': 'object', 'properties': {
            'action': {'type': 'string', 'enum': ['list', 'register', 'read', 'enter']},
            'account': {'type': 'string', 'maxLength': 254, 'description': 'ID from list; register may create a new ID.'},
            'field': {'type': 'string', 'enum': ['password', 'otp'],
                      'description': 'enter defaults to password; read defaults to otp; register supports otp only.'},
            'reason': {'type': 'string', 'maxLength': 160, 'description': 'Required only for read field=password: why the model needs the raw password.'},
            'seed': {'type': 'string', 'maxLength': 2048, 'description': 'register only: owner-provided Base32 or otpauth:// TOTP seed.'},
            'origins': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 16,
                        'description': 'register only: optional HTTPS origins for browser entry, not needed for OTP read.'},
            'selector': {'type': 'string', 'maxLength': 2000, 'description': 'Optional; defaults to the focused field.'}},
            'required': ['action'], 'additionalProperties': False}}, credential_input)
