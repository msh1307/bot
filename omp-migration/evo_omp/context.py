"""Explicit per-turn identity, cancellation and one-command approvals."""
import contextvars
import re
import secrets
import threading
import time
from dataclasses import dataclass, field

OWNER = '543004181842362379'
current = contextvars.ContextVar('omp_turn', default=None)
_pending = {}
_lock = threading.Lock()
client = None
secret_values = []


@dataclass
class Turn:
    channel: str
    user: str = OWNER
    platform: str = 'discord'
    cron: bool = False
    deferred: bool = False
    job_id: str = ''
    model: str = ''
    id: str = field(default_factory=lambda: secrets.token_hex(12))
    cancelled: threading.Event = field(default_factory=threading.Event)
    waited_seconds: int = 0
    wait_lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def key(self):
        return self.platform + ':' + self.channel


def owner_identity():
    """Identity comes only from the Discord adapter, never environment strings."""
    t = current.get()
    if (t is not None and t.user == OWNER and t.platform == 'discord'
            and not t.cancelled.is_set() and t.channel.isdigit()):
        return t
    return None


def owner_message():
    t = owner_identity()
    return t if t is not None and not t.cron else None


def is_background():
    t = current.get()
    return t is None or t.cron


def is_interrupted():
    t = current.get()
    return t is None or t.cancelled.is_set()


def live_owner():
    t = owner_message()
    return t if t is not None and client is not None and client.is_ready() else None


def redact_sensitive_text(value):
    value = str(value)
    for secret in secret_values:
        if len(secret) >= 8:
            value = value.replace(secret, '[redacted]')
    return re.sub(r'(?i)(?:gh[pousr]_[A-Za-z0-9_]{15,}|github_pat_[A-Za-z0-9_]+|'
                  r'evo_[A-Za-z0-9_-]{20,}|[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{20,})',
                  '[redacted]', value)


def wait_for_approval(session_key, notify, data):
    t = live_owner()
    if t is None or t.key != session_key:
        return {'resolved': False, 'cancelled': True}
    ident = secrets.token_hex(16)
    item = {'event': threading.Event(), 'choice': None, 'turn': t}
    with _lock:
        _pending[(session_key, ident)] = item
    try:
        notify(dict(data, request_id=ident))
        end = time.monotonic() + 300
        while time.monotonic() < end:
            if t.cancelled.is_set() or client is None or not client.is_ready():
                return {'resolved': False, 'cancelled': True}
            if item['event'].wait(0.1):
                return {'resolved': item['choice'] in ('once', 'deny'),
                        'choice': item['choice'], 'cancelled': item['choice'] is None}
        return {'resolved': False, 'cancelled': True}
    finally:
        with _lock:
            _pending.pop((session_key, ident), None)


def resolve_approval(session_key, choice, request_id=None):
    if choice not in ('once', 'deny'):
        return 0
    with _lock:
        matches = [(k, v) for k, v in _pending.items() if k[0] == session_key
                   and (request_id is None or k[1] == request_id)]
        if len(matches) != 1:
            return 0
        _, item = matches[0]
        if item['event'].is_set() or item['turn'].cancelled.is_set():
            return 0
        item['choice'] = choice
        item['event'].set()
        return 1


def withdraw_approval(session_key, request_id):
    with _lock:
        item = _pending.get((session_key, request_id))
        if item:
            item['event'].set()


def cancel_all():
    with _lock:
        for item in _pending.values():
            item['turn'].cancelled.set()
            item['event'].set()
