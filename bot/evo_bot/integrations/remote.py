"""Small model-facing router; transports and their permission gates stay separate."""
import json

from .. import context
from . import codex, credential_input, instances, rdp, remote_config, terminal

CONFIG = remote_config.CONFIG
PROTOCOLS = {'rdp', 'codex', 'terminal'}
CODEX_ACTIONS = set(codex.PUBLIC_ACTIONS) - {'servers', 'new', 'exec'}
RDP_ACTIONS = set(rdp.ACTIONS) - {'servers'} | {'accounts', 'credential'}

# Short usage is retrieved once when needed, never injected as six full schemas.
GUIDES = {
    'rdp': {
        'connect': {}, 'status': {},
        'screenshot': {'optional': {'share': 'boolean: also attach to current Discord channel; default false'}},
        'wait': {'optional': {'seconds': '0..600; default 1; returns a fresh screenshot without input'}},
        'disconnect': {},
        'click': {'required': {'frame': 'latest screenshot ID', 'x': 'normalized integer 0..1000', 'y': 'normalized integer 0..1000'},
                  'optional': {'button': 'left (default), right or double'}},
        'type': {'required': {'frame': 'latest screenshot ID', 'text': 'literal string, 1..2000 chars'}},
        'key': {'required': {'frame': 'latest screenshot ID', 'key': 'key/chord string, e.g. Enter or Ctrl+a'}},
        'scroll': {'required': {'frame': 'latest screenshot ID', 'direction': 'up or down'},
                   'optional': {'steps': 'integer 1..10; default 3'}},
        'accounts': {}, 'credential': {'required': {'account': 'id from accounts', 'frame': 'latest screenshot ID'},
                                      'optional': {'field': 'password (default) or otp'}},
        'notes': 'Connect only when requested; may lock local display. Inputs already return a fresh screenshot; use its frame. '
                 'Screenshots are image input; no separate vision tool is needed. '
                 'credential uses private keyboard input + Allow Once; cannot verify focused site. No submit/retry. '
                 'No idle close; disconnect when finished. Stale frame: screenshot again, not reconnect/replay.'},
    'codex': {
        'recent': {'note': 'All unread reply/error/question previews, saved regardless of wake. Optional instance; omit for all. list/recent keep unread; read/status remove only content actually returned.'},
        'list': {'optional': {'search': 'title substring', 'cursor': 'returned next_cursor',
                              'limit': 'integer 1..12; default 8'}},
        'projects': {'optional': {'cursor': 'returned next_cursor', 'limit': 'integer 1..12; default 12'}},
        'models': {},
        'daybreak': {'required': {'thread_id': 'chat ID from list'}, 'optional': {
            'mode': 'on|off; default on. on picks an offered cyber model if available; otherwise keeps the model. off keeps its model',
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
                              'compact': 'boolean: callbacks compact first by default; false keeps continuous context',
                              'job_id': 'alternative to thread_id from start'}},
        'answer': {'required': {'thread_id': 'chat ID from list', 'request_id': 'pending request ID',
                                'answers': 'object: question_id -> array of answer strings'}},
        'start': {'optional': {'prompt': 'instruction string', 'model': 'ID from models',
                               'mode': 'on|off: initial Daybreak choice; omit inherits native default',
                               'policy': 'never|on-request|untrusted; omit inherits native default',
                               'sandbox': 'workspace-write|read-only|danger-full-access; omit inherits native default',
                               'cwd': 'absolute path for a new daemon chat', 'project': 'Windows ID/name from projects',
                               'ephemeral': 'boolean, new daemon chat only; default false',
                               'wake': 'boolean; default true', 'compact': 'boolean; callback default true'},
                  'note': 'New chat: prompt starts immediately. Windows without prompt opens an owner selection card.'},
        'file_list': {'required': {'path': 'absolute Windows path'}, 'optional': {
            'offset': 'integer; default 0, continue with returned next_offset', 'search': 'filename substring',
            'limit': 'integer 1..200; default 20'}},
        'file_read': {'required': {'path': 'absolute Windows path'}, 'optional': {
            'start_line': 'integer; default 1, continue with returned next_line', 'limit': 'integer 1..200; default 80'}},
        'notes': 'Select instance; uses its authenticated Codex connection. Windows-only projects/files. Commands use exec. '
                 'send steers/continues the SAME chat; status shows tail/questions. Page with returned cursors. '
                 'New tasks wake EVO on completion/questions; wake=false disables. send also accepts client commands: '
                 '/goal [objective|show|clear|pause|resume], /compact, /model [ID], /daybreak on|off, /wake on|off, /status, /stop. '
                 '/goal and /compact need a daemon-owned chat; GUI-owned chats must use the app for these commands. '
                 '/goal is native state; clear/pause do not interrupt the current turn (/stop does). '
                 '/wake controls callbacks without sending a prompt; off revokes queued events. Execution approvals require owner. No replay on timeout. '
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
        if 'ssh' in protocols:
            protocols.append('terminal')
    rows.insert(0, {'id': 'localhost', 'shell': 'bash', 'protocols': ['terminal'],
                    'description': 'EVO host for coding; terminal uses sandbox /workspace.'})
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
    params = args.get('args', {})
    if not isinstance(params, dict):
        raise ValueError('args must be an object of action parameters')
    args, params = dict(args), dict(params)
    for key in ('instance', 'protocol', 'action'):
        if key in params:
            value = params.pop(key)
            if args.get(key) is not None and args[key] != value:
                raise ValueError('Conflicting routing fields; choose one instance, protocol and action')
            args[key] = value
    action, protocol = args.get('action'), args.get('protocol')
    if not isinstance(action, str) or not 1 <= len(action) <= 32:
        raise ValueError('Choose an action; use list or help first')
    if protocol is not None and (not isinstance(protocol, str) or protocol not in PROTOCOLS):
        raise ValueError('Use protocol=rdp, codex or terminal. One-shot commands use exec(command, instance).')
    ident = instances.instance_id(args)
    if ('server' in params
            or len(json.dumps(params, ensure_ascii=False).encode()) > 65536):
        raise ValueError('Use bounded action parameters in args; select targets with instance, not server')
    if turn.cron and action not in ('list', 'help') and (protocol != 'codex' or action not in codex.SCHEDULED_ACTIONS):
        raise PermissionError('Scheduled checks allow Codex inspection/continuation/file reads only; no SSH/RDP/start/exec/stop')
    if protocol == 'terminal':
        if turn.cron:
            raise PermissionError('Interactive terminals require an owner Discord message')
        if action != 'help' and ident is None:
            raise ValueError('Terminal calls require instance=localhost or a configured SSH instance')
        return terminal.control(ident or 'localhost', action, params)
    if action == 'help':
        if params and (set(params) != {'tool'} or protocol not in GUIDES or not isinstance(params.get('tool'), str)
                       or params['tool'] not in GUIDES[protocol] or params['tool'] == 'notes'):
            raise ValueError('help accepts optional instance/protocol and args={tool:ACTION} for one action')
        if turn.cron and protocol not in (None, 'codex'):
            raise PermissionError('Scheduled checks have no SSH or RDP control')
        if protocol:
            if params:
                if turn.cron and params['tool'] not in codex.SCHEDULED_ACTIONS:
                    raise PermissionError('Scheduled checks cannot use that action')
                return {params['tool']: GUIDES[protocol][params['tool']]}
            if turn.cron:
                return {action: value for action, value in GUIDES['codex'].items()
                        if action == 'notes' or action in codex.SCHEDULED_ACTIONS}
            return GUIDES[protocol]
        return {'usage': 'list: instances/defaults. protocol+action with action parameters in args. help(protocol): full usage.',
                'protocols': ['codex'] if turn.cron else ['codex', 'rdp', 'terminal'],
                **({} if turn.cron else {'coding': 'read/write/edit/exec take optional instance. Omit it for sandbox /workspace. '
                                        'exec root=true requires reason and Discord approval. file_transfer uploads/downloads.'})}
    if protocol is None and action == 'list':
        if params or ident is not None:
            raise ValueError('Server list takes no instance/args; Codex chats use protocol=codex, action=list')
        return catalog()
    if protocol is None:
        raise ValueError('Choose protocol=rdp|codex|terminal; one-shot commands use exec(command, instance).')
    if protocol == 'codex' and action == 'exec':
        raise ValueError('Commands use the exec tool with command text, instance and timeout in seconds. remote_control manages chats.')
    if protocol == 'rdp' and action not in RDP_ACTIONS \
            or protocol == 'codex' and action not in CODEX_ACTIONS:
        raise ValueError(f'Unsupported action; use remote_control action=help protocol={protocol}, then its exact action name')
    if protocol == 'codex' and action == 'recent':
        if params:
            raise ValueError('recent takes no args; optional instance selects one server')
        return codex.codex_task(dict(action='recent', **({'instance': ident} if ident else {})))
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
    # Check the action schema as well as routing before entering the existing bridge.
    guide = GUIDES['codex'][action]
    allowed = {'project', 'prompt', 'request_id', 'wake', 'compact', 'answers', 'thread_id', 'job_id',
               'search', 'cursor', 'limit', 'model', 'mode', 'policy', 'sandbox', 'ephemeral', 'cwd'}
    if set(params) - allowed:
        expected = set(guide.get('required', {})) | set(guide.get('optional', {}))
        raise ValueError('Unexpected args key; ' + action + ' accepts: ' + (', '.join(sorted(expected)) or '(no args)')
                         + '. Use help with protocol=codex, args={tool:"' + action + '"} for details')
    return codex.codex_task(dict(params, instance=ident, action=action))


def definition(cron=False):
    return {'name': 'remote_control',
        'description': 'Control a configured remote instance. list returns IDs/defaults; help(protocol, args={tool:ACTION}) gives one action; omit tool for all. '
            'codex recent lists unread updates; read/status clears viewed content. '
            'Example: {"protocol":"codex","action":"send","args":{"thread_id":"ID","prompt":"Continue"}}. '
            + ('Scheduled: Codex inspection, continuation and file reads only; no start/stop/exec.' if cron else
               'Owner Discord only. Files use read/write/edit; commands use exec with instance. '
               'terminal=interactive PTY; choose instance, then help/create/read/type/wait. '
               'RDP secret entry needs Allow Once. No replay on timeout.'),
        'parameters': {'type': 'object', 'properties': {
            'instance': {'type': 'string', 'maxLength': 48, 'description':
                'ID from list; omit for protocol default.' if cron else
                'ID from list. Terminal: required except help. Codex/RDP: optional.'},
            'protocol': {'type': 'string', 'enum': ['codex'] if cron else ['codex', 'rdp', 'terminal'],
                         'description': 'codex=chats on the selected instance' if cron else
                                        'rdp=screen; codex=chats; terminal=interactive local sandbox or SSH.'},
            'action': {'type': 'string', 'maxLength': 32, 'description': 'Exact action from help. list/help may omit protocol.'},
            'args': {'type': 'object', 'description': 'Put prompt/thread_id/wake and other action parameters HERE; exact keys from help.'}},
            'required': ['action'], 'additionalProperties': False}}


def register(ctx):
    ctx.register_tool('remote_control', 'evo_remote', definition(), remote_control)
