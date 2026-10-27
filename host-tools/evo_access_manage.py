"""Owner-only management sessions, separate from shared LLM access keys."""
import hashlib
import http.client
import json
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import urlsplit

KEY_PATH = Path('/etc/evo-access/manage-token')
LOGIN = '''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>서버 관리 로그인</title><style>body{background:#17191e;color:#eee;font:16px system-ui;max-width:420px;margin:15vh auto;padding:24px}input,button{box-sizing:border-box;width:100%;padding:12px;margin:8px 0}p{white-space:pre-wrap}</style><h1>서버 관리</h1><p>Discord /access의 서버 관리 버튼으로 접속하세요.</p><form><input id="token" type="password" placeholder="관리 전용 키" required><button>로그인</button></form><p id="status"></p><script>const input=document.querySelector('#token'),status=document.querySelector('#status');input.value=location.hash.slice(1);history.replaceState(null,'','/manage/login');async function login(){status.textContent='연결 중…';try{const r=await fetch('/manage/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:input.value})});input.value='';if(r.ok)location.replace('/manage/');else status.textContent='만료된 링크입니다. Discord에서 다시 열어주세요.'}catch(e){status.textContent='연결하지 못했습니다.'}}document.querySelector('form').onsubmit=e=>{e.preventDefault();login()};if(input.value)login();</script></html>'''.encode()


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class ManagementAccess:
    def __init__(self):
        self.lock = threading.RLock()
        self.links, self.cookies = {}, {}

    def clear(self):
        with self.lock:
            self.links.clear()
            self.cookies.clear()

    def prune(self):
        for entries in (self.links, self.cookies):
            for key, value in list(entries.items()):
                if value['expires_at'] <= time.time(): entries.pop(key, None)
            while len(entries) > 8: entries.pop(next(iter(entries)))

    def issue(self, state):
        if not KEY_PATH.is_file(): raise ValueError('Management backend is not configured')
        session = state.get('session')
        if not session or not state.get('public_url'): raise ValueError('Active access and public_url are required')
        token = secrets.token_urlsafe(32)
        with self.lock:
            self.prune()
            self.links[digest(token)] = {'expires_at': min(time.time() + 300, session['expires_at']),
                                        'session_expiry': session['expires_at'], 'grant_id': session['id']}
            self.prune()
        return {'success': True, 'url': state['public_url'] + '/manage/login#' + token,
                'expires_at': session['expires_at']}

    def handle(self, handler, method, state, connections, connection_lock, secure=False):
        path = urlsplit(handler.path).path
        session = state.get('session')
        if not session: return handler.respond(401, {'error': 'Access is closed'})
        if method == 'POST' and (not handler.origin_ok() or
                not handler.headers.get('Content-Type', '').startswith('application/json')):
            return handler.respond(403, {'error': 'Same-origin JSON request required'})
        if path == '/manage/login' and method == 'GET':
            return handler.respond(200, LOGIN, 'text/html; charset=utf-8')
        if path == '/manage/login' and method == 'POST':
            try:
                if int(handler.headers.get('Content-Length', '0')) > 1024: raise ValueError()
                token = json.loads(handler.body()).get('token')
                if not isinstance(token, str) or len(token) > 256: raise ValueError()
            except (ValueError, TypeError, AttributeError):
                return handler.respond(400, {'error': 'Invalid login'})
            with self.lock:
                self.prune()
                saved = self.links.pop(digest(token), None)
                if not saved or saved['grant_id'] != session['id']:
                    return handler.respond(401, {'error': 'Expired or invalid management link'})
                cookie = secrets.token_urlsafe(32)
                self.cookies[digest(cookie)] = {'expires_at': min(saved['session_expiry'], session['expires_at']),
                                               'grant_id': session['id']}
                self.prune()
            return handler.respond(200, {'success': True}, cookie='evo_owner=' + cookie +
                '; Path=/manage; HttpOnly; SameSite=Strict' + ('; Secure' if secure else ''))
        cookie = next((part.strip().split('=', 1)[1] for part in handler.headers.get('Cookie', '').split(';')
                       if part.strip().startswith('evo_owner=')), '')
        with self.lock:
            self.prune()
            saved = self.cookies.get(digest(cookie))
            authorized = saved and saved['grant_id'] == session['id']
        if not authorized: return handler.respond(401, {'error': 'Management login required; LLM keys do not grant access'})
        if method == 'POST' and path == '/manage/logout':
            with self.lock: self.cookies.pop(digest(cookie), None)
            return handler.respond(200, {'success': True}, cookie='evo_owner=; Path=/manage; HttpOnly; SameSite=Strict; Max-Age=0')
        if (method, path) not in {('GET', '/manage'), ('GET', '/manage/'),
                                 ('GET', '/manage/api/servers'), ('POST', '/manage/api/call')}:
            return handler.respond(404, {'error': 'Unknown management route'})
        if method == 'POST':
            try:
                if not 0 < int(handler.headers.get('Content-Length', '0')) <= 65536: raise ValueError()
                body = handler.body()
            except (ValueError, OSError): return handler.respond(413, {'error': 'Invalid request size'})
        else: body = None
        with connection_lock: connections[handler.connection] = session['id']
        backend = http.client.HTTPConnection('127.0.0.1', 8767, timeout=75)
        try:
            backend.request(method, path, body=body, headers={'Content-Type': 'application/json',
                'X-Evo-Manage-Key': KEY_PATH.read_text().strip()})
            response = backend.getresponse()
            content = response.read(262145)
            if len(content) > 262144: raise ValueError('Management response too large')
            return handler.respond(response.status, content, response.getheader('Content-Type', 'application/json'))
        except (OSError, ValueError, http.client.HTTPException):
            return handler.respond(502, {'error': 'Management connection lost; check status before resending commands'})
        finally: backend.close()
