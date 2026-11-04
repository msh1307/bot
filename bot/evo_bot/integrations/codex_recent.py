"""Small persistent inbox for Codex replies, errors and pending questions."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time


def text(value):
    if isinstance(value, dict):
        value = value.get('message', '')
    return value.strip() if isinstance(value, str) else ''


def questions(result):
    for request in result.get('pending_requests') or []:
        ident = request.get('request_id', request.get('id'))
        parts = [text(q.get('question')) for q in request.get('questions') or []]
        parts += [text(request.get('reason')), text(request.get('command'))]
        content = '\n'.join(p for p in parts if p)
        if ident is not None and content:
            yield str(ident), content


def error_text(result):
    error = text(result.get('turn_error'))
    if not error and result.get('state', result.get('turn_status')) in ('failed', 'connection_lost'):
        error = text(result.get('phase'))
    return error if error not in ('failed', 'connection_lost') else ''


class Recent:
    def __init__(self, path):
        self.path, self.lock = Path(path), threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(mode=0o600, exist_ok=True)
        with closing(self.connect()) as db:
            db.execute('CREATE TABLE IF NOT EXISTS events ('
                       'id TEXT PRIMARY KEY, instance TEXT, thread TEXT, turn TEXT, kind TEXT, '
                       'request TEXT, at INTEGER, preview TEXT, tail TEXT)')

    def connect(self):
        return sqlite3.connect(self.path, timeout=5)

    def observe(self, instance, result):
        thread, turn = result.get('thread_id'), result.get('turn_id')
        if not thread:
            return
        events = [('question', ident, content) for ident, content in questions(result)]
        error = error_text(result)
        if error:
            events.append(('error', hashlib.sha256(error.encode()).hexdigest(), error))
        elif result.get('state', result.get('turn_status')) in ('completed', 'interrupted') and turn:
            answer = text(result.get('answer'))
            if not answer:
                answer = next((text(m.get('text')) for m in reversed(result.get('recent_messages') or [])
                               if m.get('role') == 'assistant' and m.get('phase') in (None, 'final_answer')
                               and m.get('turn_id', turn) == turn), '')
            if answer:
                events.append(('reply', '', answer))
        with self.lock, closing(self.connect()) as db, db:
            for kind, request, content in events:
                key = hashlib.sha256(json.dumps([instance, thread, turn, kind, request]).encode()).hexdigest()
                # Keep read IDs as tiny tombstones: polling must not resurrect them.
                db.execute('INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?,?,?,?)',
                           (key, instance, thread, turn, kind, request, int(time.time()), content[:400], content[-200:]))

    def pending(self, instance=None):
        """Listing a preview is not reading the original reply/question."""
        where = 'preview IS NOT NULL' + (' AND instance=?' if instance else '')
        with self.lock, closing(self.connect()) as db, db:
            rows = db.execute('SELECT id,instance,thread,turn,kind,at,preview FROM events WHERE '
                              + where + ' ORDER BY at,rowid', (instance,) if instance else ()).fetchall()
        return {'events': [dict(instance=r[1], thread_id=r[2], turn_id=r[3], type=r[4], at=r[5], text=r[6])
                           for r in rows]}

    def read(self, instance, result):
        """Only acknowledge content actually returned, for its own turn/request."""
        thread, turn = result.get('thread_id'), result.get('turn_id')
        if not thread:
            return
        replies = [(turn, text(result.get('answer')))]
        for message in result.get('recent_messages', result.get('messages', [])) or []:
            if message.get('role') == 'assistant':
                replies.append((message.get('turn_id', turn), text(message.get('text'))))
        pending, error = dict(questions(result)), error_text(result)

        def overlaps(content, preview, tail):
            return bool(content and (preview in content or tail in content or content in preview or content in tail))

        with self.lock, closing(self.connect()) as db, db:
            rows = db.execute('SELECT id,turn,kind,request,preview,tail FROM events '
                              'WHERE instance=? AND thread=? AND preview IS NOT NULL', (instance, thread)).fetchall()
            for ident, saved_turn, kind, request, preview, tail in rows:
                covered = (kind == 'question' and turn == saved_turn and overlaps(pending.get(request, ''), preview, tail)
                           or kind == 'error' and turn == saved_turn and overlaps(error, preview, tail)
                           or kind == 'reply' and any(t == saved_turn and overlaps(content, preview, tail)
                                                     for t, content in replies))
                if covered:
                    db.execute('UPDATE events SET preview=NULL,tail=NULL WHERE id=?', (ident,))
