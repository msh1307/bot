"""Small model-facing router; transports and their permission gates stay separate."""
import json

from .. import context
from . import codex, credential_input, instances, rdp, remote_config

CONFIG = remote_config.CONFIG
PROTOCOLS = {'rdp', 'codex'}
CODEX_ACTIONS = set(codex.PUBLIC_ACTIONS) - {'servers', 'new'}
RDP_ACTIONS = set(rdp.ACTIONS) - {'servers'} | {'accounts', 'credential'}

# Short usage is retrieved once when needed, never injected as six full schemas.
GUIDES = {
    'rdp': {
        'connect': {}, 'status': {},
        'screenshot': {'optional': {'share': 'boolean: also attach to current Discord channel; default false'}},
        'disconnect': {},
        'click': {'required': {'frame': 'latest screenshot ID', 'x': 'normalized integer 0..1000', 'y': 'normalized integer 0..1000'},
                  'optional': {'button': 'left (default), right or double'}},
        'type': {'required': {'frame': 'latest screenshot ID', 'text': 'literal string, 1..2000 chars'}},
        'key': {'required': {'frame': 'latest screenshot ID', 'key': 'key/chord string, e.g. Enter or Ctrl+a'}},
        'scroll': {'required': {'frame': 'latest screenshot ID', 'direction': 'up or down'},
                   'optional': {'steps': 'integer 1..10; default 3'}},
        'accounts': {}, 'credential': {'required': {'account': 'id from accounts', 'frame': 'latest screenshot ID'},
                                      'optional': {'field': 'password (default) or otp'}},
        'notes': 'Connect only when requested; may lock local display. Observe screenshot after each input; use its fresh frame. '
                 'Screenshots are image input; no separate vision tool is needed. '
                 'credential uses private keyboard input + Allow Once; cannot verify focused site. No submit/retry. Idle close 15min.'},
    'codex': {
        'list': {'optional': {'search': 'title substring', 'cursor': 'returned next_cursor',
                              'limit': 'integer 1..12; default 8'}},
        'projects': {'optional': {'cursor': 'returned next_cursor', 'limit': 'integer 1..12; default 12'}},
        'models': {},
        'daybreak': {'required': {'thread_id': 'chat ID from list'}, 'optional': {
            'mode': 'on|off; default on. on switches this chat to the cyber-capable model; off only clears '
                    'the flag and the chat keeps its model',
            'model': 'ID from models; applies to this chat only, omit to keep it (off) or auto-pick (on)'}},
        'approval': {'required': {'thread_id': 'chat ID from list'}, 'optional': {
            'policy': 'never|on-request|untrusted; omit keeps the current policy',
            'sandbox': 'workspace-write|read-only|danger-full-access; omit keeps the current sandbox'},
            'note': 'give policy, sandbox or both'},
        'settings': {'required': {'thread_id': 'chat ID from list'}},
        'read': {'required': {'thread_id': 'chat ID from list'}, 'optional': {'cursor': 'returned next_cursor'}},
        'status': {'required': {'thread_id': 'chat ID from list'}},
        'stop': {'required': {'thread_id': 'chat ID from list'}},
        'send': {'required': {'prompt': 'instruction string, 1..16000 chars'},
                 'optional': {'thread_id': 'chat ID from list; or use job_id from start',
                              'wake': 'boolean: callback to EVO; omit keeps setting; new tasks default true',
                              'job_id': 'alternative to thread_id from start'}},
        'answer': {'required': {'thread_id': 'chat ID from list', 'request_id': 'pending request ID',
                                'answers': 'object: question_id -> array of answer strings'}},
        'start': {'optional': {'prompt': 'instruction string', 'model': 'ID from models',
                               'cwd': 'absolute path for a new daemon chat', 'project': 'Windows ID/name from projects',
                               'ephemeral': 'boolean, new daemon chat only; default false',
                               'wake': 'boolean; default true'},
                  'note': 'New chat. Windows shows an owner selection card; Linux starts in cwd.'},
        'file_list': {'required': {'path': 'absolute Windows path'}, 'optional': {
            'offset': 'integer; default 0, continue with returned next_offset', 'search': 'filename substring',
            'limit': 'integer 1..200; default 20'}},
        'file_read': {'required': {'path': 'absolute Windows path'}, 'optional': {
            'start_line': 'integer; default 1, continue with returned next_line', 'limit': 'integer 1..200; default 80'}},
        'exec': {'required': {'command': 'argv array of strings'}, 'optional': {
            'cwd': 'absolute Windows path', 'timeout_ms': 'integer 100..15000; default 10000'}},
        'notes': 'Select instance; uses its authenticated Codex connection. Windows-only projects/files/exec. '
                 'send steers/continues the SAME chat; status shows tail/questions. Page with returned cursors. '
                 'New tasks wake EVO on completion/questions; wake=false disables. Execution approvals require owner. No replay on timeout. '
                 'For a delayed check use schedule action=defer, after_seconds=1..600, prompt=task; not a Codex action. '
                 'daybreak/approval/settings affect this chat only; GUI chats stay with the app owner. Omitted settings are preserved.'}}


def defaults():
    data = remote_config.document(CONFIG)
    return data.get('defaults', {}) if data is not None else {}


def catalog():
    old = instances.catalog()
    fallback = old['defaults']
    configured = defaults()
    selected = {**fallback, **configured}
    rows = []
    for entry in old['instances']:
        capabilities = set(entry['capabilities'])
        protocols = [p for p, name in (('ssh', 'remote_ssh'), ('rdp', 'remote_desktop'), ('codex', 'codex_task'))
                     if name in capabilities]
        row = {'id': entry['id'], 'host': entry['host'], 'protocols': protocols}
        if entry.get('name') != entry['id']:
            row['name'] = entry['name']
        if entry.get('sudo'):
            row['sudo'] = entry['sudo']
        if entry.get('disabled'):
            row['disabled'] = ['rdp']
        rows.append(row)
        row['shell'] = 'bash' if 'ssh' in protocols else 'powershell' if 'codex' in protocols else None
    rows.insert(0, {'id': 'localhost', 'shell': 'bash', 'protocols': [],
                    'description': 'EVO host. Omit instance for sandbox /workspace.'})
    for protocol, ident in selected.items():
        if ident is not None and not any(r['id'] == ident and protocol in r['protocols'] for r in rows):
            if protocol in configured:
                raise ValueError(f'Configured {protocol} default is unavailable; fix private configuration')
            selected[protocol] = None
    return {'defaults': {k: v for k, v in selected.items() if v is not None}, 'instances': rows}


def remote_control(args):
    turn = context.owner_identity()
    if turn is None:
        raise PermissionError('Remote control requires owner Discord')
    if not isinstance(args, dict) or set(args) - {'instance', 'protocol', 'action', 'args'}:
        raise ValueError('Use instance, protocol, action, args={action parameters}; list/help discovers usage')
    action, protocol = args.get('action'), args.get('protocol')
    if not isinstance(action, str) or not 1 <= len(action) <= 32:
        raise ValueError('Choose an action; use list or help first')
    if protocol is not None and (not isinstance(protocol, str) or protocol not in PROTOCOLS):
        raise ValueError('Use protocol=rdp or codex. Commands use bash(command, instance).')
    ident = instances.instance_id(args)
    params = args.get('args', {})
    if (not isinstance(params, dict) or any(k in params for k in ('instance', 'server', 'protocol', 'action'))
            or len(json.dumps(params, ensure_ascii=False).encode()) > 65536):
        raise ValueError('args contains only action parameters, not routing fields or secrets')
    if turn.cron and action not in ('list', 'help') and (protocol != 'codex' or action not in codex.SCHEDULED_ACTIONS):
        raise PermissionError('Scheduled checks allow Codex inspection/continuation/file reads only; no SSH/RDP/start/exec/stop')
    if action == 'help':
        if params or ident is not None:
            raise ValueError('help takes only optional protocol')
        if turn.cron and protocol not in (None, 'codex'):
            raise PermissionError('Scheduled checks have no SSH or RDP control')
        if protocol:
            if turn.cron:
                return {action: value for action, value in GUIDES['codex'].items()
                        if action == 'notes' or action in codex.SCHEDULED_ACTIONS}
            return GUIDES[protocol]
        return {'usage': 'list: instances/defaults. protocol+action with action parameters in args. help(protocol): full usage.',
                'protocols': ['codex'] if turn.cron else ['codex', 'rdp'],
                **({} if turn.cron else {'coding': 'read/write/edit/bash take optional instance. Omit it for sandbox /workspace. '
                                        'bash root=true requires reason and Discord approval. file_transfer uploads/downloads.'})}
    if protocol is None and action == 'list':
        if params or ident is not None:
            raise ValueError('Server list takes no instance/args; Codex chats use protocol=codex, action=list')
        return catalog()
    if protocol is None:
        raise ValueError('Choose protocol=rdp|codex; commands use bash(command, instance).')
    if protocol == 'rdp' and action not in RDP_ACTIONS \
            or protocol == 'codex' and action not in CODEX_ACTIONS:
        raise ValueError(f'Unsupported action; use remote_control action=help protocol={protocol}, then its exact action name')
    available = catalog()
    ident = ident or available['defaults'].get(protocol)
    if not ident or not any(r['id'] == ident and protocol in r['protocols'] for r in available['instances']):
        raise ValueError('Instance/protocol unavailable; use remote_control action=list. No fallback to another machine.')
    if protocol == 'rdp':
        if action in ('accounts', 'credential'):
            return credential_input.credential_input(dict(params, instance=ident,
                action='list' if action == 'accounts' else 'enter'))
        return rdp.remote_desktop(dict(params, instance=ident, action=action))
    if action in ('file_list', 'file_read'):
        return codex.windows_files(dict(params, instance=ident, action=action.removeprefix('file_')))
    if action == 'exec':
        return codex.windows_exec(dict(params, instance=ident))
    # Check the action schema as well as routing before entering the existing bridge.
    allowed = {'project', 'prompt', 'request_id', 'wake', 'answers', 'thread_id', 'job_id',
               'search', 'cursor', 'limit', 'model', 'mode', 'policy', 'sandbox', 'ephemeral', 'cwd'}
    if set(params) - allowed:
        raise ValueError('Unexpected args key; use remote_control action=help protocol=codex, then its exact args keys')
    return codex.codex_task(dict(params, instance=ident, action=action))


def definition(cron=False):
    return {'name': 'remote_control',
        'description': 'Control a configured remote instance. list returns IDs/defaults; help with protocol returns exact actions/args. '
            'Example: {"protocol":"codex","action":"send","args":{"thread_id":"ID","prompt":"Continue"}}. '
            + ('Scheduled: Codex inspection, continuation and file reads only; no start/stop/exec.' if cron else
               'Owner Discord only. Files/commands use read/write/edit/bash with instance. '
               'RDP secret entry needs Allow Once. No replay on timeout.'),
        'parameters': {'type': 'object', 'properties': {
            'instance': {'type': 'string', 'maxLength': 48, 'description': 'ID from list; omit for protocol default.'},
            'protocol': {'type': 'string', 'enum': ['codex'] if cron else ['codex', 'rdp'],
                         'description': 'codex=chats on the selected instance' if cron else
                                        'rdp=screen/keyboard/mouse; codex=chats on the selected instance.'},
            'action': {'type': 'string', 'maxLength': 32, 'description': 'Exact action from help. list/help may omit protocol.'},
            'args': {'type': 'object', 'description': 'Action parameters only; exact keys from help.'}},
            'required': ['action'], 'additionalProperties': False}}


def register(ctx):
    ctx.register_tool('remote_control', 'evo_remote', definition(), remote_control)
