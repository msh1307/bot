"""Root-only account registration. Secret file references only; never plaintext arguments."""
import argparse
import grp
import json
import os
from pathlib import Path
import secrets

from evo_bot.integrations import credential_input as credentials


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('id')
    parser.add_argument('--username', required=True)
    for field in ('password', 'sudo-password', 'identity', 'totp'):
        parser.add_argument('--' + field + '-file')
    for field in ('ssh-instance', 'rdp-instance', 'origin'):
        parser.add_argument('--' + field, action='append', default=[])
    parser.add_argument('--replace', action='store_true')
    args, unknown = parser.parse_known_args(argv)
    if unknown:
        parser.error('Unsupported option; provide private file references only')
    if os.geteuid() != 0:
        parser.error('Registration requires host root; never give the model plaintext passwords')
    document = credentials.account_document()
    data = document.setdefault('accounts', {})
    if args.id in data and not args.replace:
        parser.error('Account ID already exists; use --replace to update it')
    entry = {'username': args.username}
    paths = {key: getattr(args, key) for key in ('password_file', 'sudo_password_file', 'identity_file', 'totp_file')
             if getattr(args, key)}
    if not paths:
        parser.error('Provide at least one private credential file reference')
    entry.update(paths)
    for key, value in (('ssh_instances', args.ssh_instance), ('rdp_instances', args.rdp_instance), ('origins', args.origin)):
        if value: entry[key] = value
    data[args.id] = entry
    temp = None
    try:
        credentials.validate_accounts({'accounts': data})
        for path in paths.values(): credentials.private_text(path)  # Validate without printing values.
        document['accounts'] = data
        credentials.remote_config.validate(document)
        raw = (json.dumps(document, ensure_ascii=False, indent=2) + '\n').encode()
        if len(raw) > 16384: raise ValueError('Registry size limit')
        target = Path(credentials.CONFIG)
        group = grp.getgrnam('evo-bot').gr_gid
        new_parent = not target.parent.exists()
        target.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        if new_parent:
            os.chown(target.parent, 0, group); target.parent.chmod(0o750)
        temp = target.with_name('.accounts-' + secrets.token_hex(8))
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
        with os.fdopen(fd, 'wb') as out:
            os.fchown(out.fileno(), 0, group); os.fchmod(out.fileno(), 0o640)
            out.write(raw); out.flush(); os.fsync(out.fileno())
        temp.replace(target)
    except Exception:
        if temp is not None: temp.unlink(missing_ok=True)
        parser.error('Registration failed; check account fields and private file permissions. No secret values were printed.')
    print(json.dumps({'registered': args.id, 'username': args.username}))


if __name__ == '__main__': main()
