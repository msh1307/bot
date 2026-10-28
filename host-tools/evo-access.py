#!/usr/bin/python3
"""Temporary access to the existing chat router; no active grant means no TCP listener."""
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import logging.handlers
import os
from pathlib import Path
import secrets
import socket
import socketserver
import ssl
import struct
import threading
import time
from urllib.parse import urlsplit
CONFIG = json.loads(Path('/etc/evo-access/config.json').read_text())
KEY_FILE = Path('/var/lib/evo-access/key.json')
LOCK = threading.RLock()
ADMIN_LOCK = threading.Lock()
GRANT = None
PUBLIC = None
CONNECTIONS = {}
PROXY_GATE = threading.Lock()
AUDIT = logging.getLogger('evo.access.audit')
AUDIT_LOCK = threading.RLock()
AUDIT_FILE = Path('/run/evo-access/access.jsonl')
LAST_DENIAL_LOG = 0.0
AUDIT_PATHS = {'/access/login','/access/logout','/models/load','/models/unload',
               '/v1/chat/completions','/v1/completions','/completion'}

def init_audit():
    handler=logging.handlers.RotatingFileHandler(AUDIT_FILE,maxBytes=128*1024,backupCount=1,delay=True)
    handler.setFormatter(logging.Formatter('%(message)s'))
    AUDIT.addHandler(handler);AUDIT.setLevel(logging.INFO);AUDIT.propagate=False

def router_history(enabled):
    backend=http.client.HTTPConnection('127.0.0.1',8080,timeout=1)
    try:
        backend.request('POST','/evo/clear-history',body=json.dumps({'enabled':enabled}).encode(),headers={'Content-Type':'application/json'})
        backend.getresponse().read(4096)
    except (OSError,http.client.HTTPException):pass
    finally:backend.close()

def clear_access_history():
    with AUDIT_LOCK:
        for handler in AUDIT.handlers:handler.close()
        for path in (AUDIT_FILE,Path(str(AUDIT_FILE)+'.1')):path.unlink(missing_ok=True)
    router_history(False)

def access_history():
    records=[]
    with AUDIT_LOCK:
        for path in (Path(str(AUDIT_FILE)+'.1'),AUDIT_FILE):
            if not path.exists():continue
            for line in path.read_text().splitlines():
                try:records.append(json.loads(line))
                except ValueError:pass
    return {'success':True,'records':records[-32:],'temporary':True}

def resume_maintenance_state():
    # A deployment may restore the existing timer once; ordinary restarts remain closed.
    staged=KEY_FILE.parent/'maintenance-resume.json'
    if not staged.exists():return
    try:
        data=json.loads(staged.read_text())
        if GRANT and data.get('id')==GRANT['id'] and type(data.get('expires_at')) is int and data['expires_at']>time.time():
            GRANT['expires_at']=data['expires_at']
            GRANT['last_action']='maintenance_resume';GRANT['changed_at']=int(time.time())
            open_public()
    finally:staged.unlink(missing_ok=True)

def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()

def load_key():
    if not KEY_FILE.exists(): return None
    saved = json.loads(KEY_FILE.read_text())
    if (not isinstance(saved, dict) or
            not isinstance(saved.get('id'), str) or len(saved['id']) != 16 or
            not isinstance(saved.get('key_hash'), str) or len(saved['key_hash']) != 64 or
            any(c not in '0123456789abcdef' for c in saved['id'] + saved['key_hash']) or
            type(saved.get('created_at')) is not int or saved['created_at'] <= 0):
        raise ValueError('Invalid stored access credential')
    # Restart closed; the owner can refresh the same credential to reopen.
    mode = saved.get('mode', 'web')
    if mode not in ('api', 'web'): raise ValueError('Invalid stored access mode')
    key = saved.get('api_key')
    if key is not None and (not isinstance(key,str) or len(key)>256 or digest(key)!=saved['key_hash']):
        raise ValueError('Stored key does not match its authentication hash')
    return {k:saved[k] for k in ('id', 'key_hash', 'created_at')} | {
        'mode':mode, 'api_key':key, 'cookie_hashes':[], 'expires_at':0,
        'last_action':'restart', 'changed_at':int(time.time())}

def save_key(grant):
    saved = {k:grant[k] for k in ('id', 'key_hash', 'created_at', 'mode', 'api_key')}
    staged = KEY_FILE.with_suffix('.new')
    fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as output:
        os.fchmod(output.fileno(), 0o600)
        output.write(json.dumps(saved)+'\n')
        output.flush()
        os.fsync(output.fileno())
    staged.replace(KEY_FILE)
    directory_fd = os.open(KEY_FILE.parent, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(directory_fd)
    finally: os.close(directory_fd)

def alive(grant_id):
    with LOCK:
        return bool(GRANT and GRANT['id'] == grant_id and GRANT['expires_at'] > time.time())

def public_status():
    with LOCK:
        # The external address can be configured without interrupting an active listener.
        config = json.loads(Path('/etc/evo-access/config.json').read_text())
        base = config.get('public_url','').rstrip('/')
        active = GRANT if GRANT and GRANT['expires_at'] > time.time() else None
        return {'listening': PUBLIC is not None, 'port': 13077, 'mode':GRANT['mode'] if GRANT else None,
                'public_url':base, 'api_base_url':base+'/v1' if base else None,
                'web_login_url':base+'/access/login' if base else None,
                'last_action':GRANT.get('last_action') if GRANT else None,
                'changed_at':GRANT.get('changed_at') if GRANT else None,
                'credential': {k:GRANT[k] for k in ('id', 'created_at')} if GRANT else None,
                'session': {k: active[k] for k in ('id', 'created_at', 'expires_at')} if active else None}

def close_connections(grant_id=None):
    with LOCK:
        sockets = [s for s, owner in CONNECTIONS.items() if grant_id is None or owner == grant_id]
    for connection in sockets:
        try: connection.shutdown(socket.SHUT_RDWR)
        except OSError: pass
        try: connection.close()
        except OSError: pass

def close_public():
    global PUBLIC
    with LOCK:
        server, PUBLIC = PUBLIC, None
    close_connections()
    if server:
        server.shutdown()
        server.server_close()
        print('access_port_closed', flush=True)
    clear_access_history()

def open_public():
    global PUBLIC
    if PUBLIC: return
    server = LimitedServer(('0.0.0.0', 13077), PublicHandler)
    if CONFIG.get('tls_cert') and CONFIG.get('tls_key'):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(CONFIG['tls_cert'], CONFIG['tls_key'])
        server.socket = context.wrap_socket(server.socket, server_side=True)
    PUBLIC = server
    router_history(True)
    threading.Thread(target=server.serve_forever, kwargs={'poll_interval':0.1}, daemon=True).start()
    print('access_port_opened', flush=True)

def admin_action(data):
    with ADMIN_LOCK:
        return _admin_action(data)

def _admin_action(data):
    global GRANT
    action = data.get('action', 'status')
    if action == 'status': return {'success':True, **public_status()}
    if action == 'logs': return access_history()
    if action == 'credentials':
        with LOCK:
            if not GRANT or not GRANT.get('api_key'): raise ValueError('No recoverable key exists')
            state = public_status()
            key = GRANT['api_key']
            base = state['public_url']
            return {'success':True, **state, 'api_key':key, 'web_path':'/access/login#'+key,
                    'web_url':base+'/access/login#'+key if base else None}
    minutes = data.get('minutes', 180)
    if isinstance(minutes, bool) or not isinstance(minutes, int) or not 1 <= minutes <= 180:
        raise ValueError('Duration must be 1–180 minutes')
    mode = data.get('mode')
    if mode is not None and mode not in ('api', 'web'): raise ValueError('Mode must be api or web')
    with LOCK:
        if action in ('issue', 'refresh', 'select') and not GRANT:
            key = 'evo_' + secrets.token_urlsafe(32)
            now = time.time()
            GRANT = {'id':secrets.token_hex(8), 'key_hash':digest(key), 'api_key':key, 'cookie_hashes':[], 'mode':mode or 'web',
                     'created_at':int(now), 'expires_at':int(now+minutes*60),
                     'last_action':action,'changed_at':int(now)}
            try:
                save_key(GRANT)
                open_public()
            except BaseException:
                GRANT = None
                KEY_FILE.unlink(missing_ok=True)
                raise
            state = public_status()
            base = state['public_url']
            return {'success':True, **state, 'api_key':key,
                    'api_base_url':base+'/v1' if base else 'http(s)://YOUR-HOST:13077/v1',
                    'web_path':'/access/login#'+key,
                    'web_url':base+'/access/login#'+key if base else None}
        if action not in ('issue', 'refresh', 'select', 'extend', 'adjust', 'expire', 'discard_undelivered'): raise ValueError('Unknown action')
        if not GRANT: raise ValueError('No credential exists; use Refresh first')
        requested = data.get('session_id')
        if action not in ('issue', 'refresh', 'select') and (not requested or requested != GRANT['id']):
            raise ValueError('Session ID does not match the active grant')
        if requested and requested != GRANT['id']:
            raise ValueError('Session ID does not match the stored credential')
        grant_id = GRANT['id']
        if action in ('issue', 'refresh', 'select', 'extend', 'adjust'):
            old_expiry = GRANT['expires_at']
            old_mode = GRANT['mode']
            now = time.time()
            new_expiry = int(now+minutes*60)
            if action == 'select' and old_expiry > now: new_expiry = old_expiry
            if action == 'adjust':
                delta = data.get('delta_minutes')
                if type(delta) is not int or delta not in (-180, 180):
                    raise ValueError('Timer adjustment must be -180 or +180 minutes')
                if old_expiry <= now and delta < 0: raise ValueError('The timer is already stopped')
                new_expiry = int(max(now, old_expiry)+delta*60)
            if mode is not None and mode != old_mode:
                GRANT['mode'] = mode
                try: save_key(GRANT)
                except BaseException:
                    GRANT['mode'] = old_mode
                    raise
            if new_expiry > now:
                GRANT['expires_at'] = new_expiry
                try: open_public()
                except BaseException:
                    GRANT['expires_at'] = old_expiry
                    raise
                GRANT['last_action'] = action
                GRANT['changed_at'] = int(now)
                print('access_action='+action+' mode='+GRANT['mode']+' expires_at='+str(new_expiry),flush=True)
                return {'success':True, **public_status()}
        GRANT['expires_at'] = 0
        GRANT['cookie_hashes'] = []
        GRANT['last_action'] = action
        GRANT['changed_at'] = int(time.time())
        print('access_action='+action+' closed',flush=True)
        # Used only if the first private delivery failed: no owner has received this key.
        if action == 'discard_undelivered':
            KEY_FILE.unlink(missing_ok=True)
            GRANT = None
    close_connections(grant_id)
    close_public()
    return {'success':True, **public_status()}

class LimitedServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    def __init__(self, *args, **kwargs):
        self.capacity = threading.BoundedSemaphore(16)
        super().__init__(*args, **kwargs)
    def process_request(self, request, address):
        if not self.capacity.acquire(False):
            request.close()
            return
        try: super().process_request(request, address)
        except BaseException:
            self.capacity.release()
            raise
    def process_request_thread(self, request, address):
        try: super().process_request_thread(request, address)
        finally: self.capacity.release()

LOGIN = b'''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>EVO session</title><style>body{background:#101824;color:#e8eef8;font:16px system-ui;max-width:440px;margin:12vh auto;padding:24px}input,button{box-sizing:border-box;width:100%;padding:12px;margin-top:12px;border-radius:8px}p{line-height:1.6}</style><h2>EVO temporary session</h2><p>Enter the temporary key issued in Discord.</p><form><input id="key" type="password" autocomplete="off" placeholder="Temporary key" required><button>Open chat</button></form><p id="status"></p><script>const k=document.querySelector('#key'),s=document.querySelector('#status');const t=location.hash.slice(1);history.replaceState(null,'','/access/login');async function login(){s.textContent='Connecting...';const r=await fetch('/access/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:k.value})});k.value='';if(r.ok)location.replace('/');else s.textContent='Expired or invalid key.'}document.querySelector('form').onsubmit=e=>{e.preventDefault();login()};if(t){k.value=t;login()}</script></html>'''

class PublicHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def log_message(self, *args): pass
    def send_response(self,code,message=None):
        self.audit_status=code
        return super().send_response(code,message)
    def setup(self):
        super().setup()
        self.connection.settimeout(10)
        with LOCK: CONNECTIONS[self.connection] = None
    def finish(self):
        try: super().finish()
        except OSError: pass
        finally:
            with LOCK: CONNECTIONS.pop(self.connection, None)
    def respond(self, code, body, mime='application/json', cookie=None):
        if not isinstance(body, bytes): body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Connection', 'close')
        if cookie: self.send_header('Set-Cookie', cookie)
        self.end_headers()
        self.close_connection = True
        self.wfile.write(body)
    def origin_ok(self):
        origin = self.headers.get('Origin')
        return not origin or urlsplit(origin).netloc == self.headers.get('Host')
    def authenticate(self):
        authorization = self.headers.get('Authorization', '')
        key = authorization[7:] if authorization.startswith('Bearer ') else None
        cookie = None
        for part in self.headers.get('Cookie', '').split(';'):
            if part.strip().startswith('evo_session='): cookie = part.strip().split('=',1)[1]
        with LOCK:
            if not GRANT or GRANT['expires_at'] <= time.time(): return None
            key_ok = bool(key and secrets.compare_digest(digest(key), GRANT['key_hash']))
            cookie_ok = bool(cookie and any(secrets.compare_digest(digest(cookie), saved)
                             for saved in GRANT.get('cookie_hashes',[])))
            if not key_ok and not cookie_ok: return None
            CONNECTIONS[self.connection] = GRANT['id']
            return GRANT['id']
    def body(self):
        if self.headers.get('Transfer-Encoding'): raise ValueError('Chunked requests are not supported')
        size = int(self.headers.get('Content-Length','0'))
        if not 0 < size <= 8*1024**2: raise ValueError('Request size must be 1 byte–8 MiB')
        data = self.rfile.read(size)
        if len(data) != size: raise ValueError('Incomplete request')
        return data
    def do_GET(self): self.handle_audited('GET')
    def do_POST(self): self.handle_audited('POST')
    def handle_audited(self,method):
        global LAST_DENIAL_LOG
        supplied=self.headers.get('X-Evo-Request-ID','')
        self.request_id=supplied if len(supplied)==32 and all(c in '0123456789abcdef' for c in supplied) else secrets.token_hex(16)
        self.audit_status=None;self.audit_model=None
        started=time.monotonic();path=urlsplit(self.path).path
        try:self.handle_public(method)
        finally:
            # Static files and periodic status polling generate no disk access records.
            if method=='POST' and path in AUDIT_PATHS or self.audit_status in (401,403):
                record={'time':time.time(),'peer_ip':self.client_address[0],'request_id':self.request_id,
                        'method':method,'path':path if path in AUDIT_PATHS else 'other',
                        'status':self.audit_status,'duration_seconds':round(time.monotonic()-started,3)}
                if self.audit_model:record['model']=self.audit_model
                with AUDIT_LOCK:
                    with LOCK:enabled=bool(PUBLIC and GRANT and GRANT['expires_at']>time.time())
                    denied=self.audit_status in (401,403)
                    if enabled and (not denied or time.monotonic()-LAST_DENIAL_LOG>=5):
                        if denied:LAST_DENIAL_LOG=time.monotonic()
                        AUDIT.info(json.dumps(record,separators=(',',':')))
    def handle_public(self, method):
        path = urlsplit(self.path).path
        with LOCK:
            mode = GRANT['mode'] if GRANT else None
        if mode == 'api' and (path.startswith('/access/') or method == 'GET' and path == '/'):
            return self.respond(404, {'error':'API mode; select Web UI in Discord to open the browser interface'})
        if path == '/access/login' and method == 'GET':
            return self.respond(200, LOGIN, 'text/html; charset=utf-8')
        if method == 'POST' and not self.origin_ok():
            return self.respond(403, {'error':'Cross-origin request denied'})
        if path == '/access/login' and method == 'POST':
            try:
                key = json.loads(self.body()).get('token','')
                if not isinstance(key,str) or len(key)>256: raise ValueError('Invalid token')
                with LOCK:
                    if not GRANT or GRANT['expires_at'] <= time.time() or not secrets.compare_digest(digest(key),GRANT['key_hash']):
                        return self.respond(401, {'error':'Expired or invalid token'})
                    cookie = secrets.token_urlsafe(32)
                    GRANT['cookie_hashes'] = (GRANT.get('cookie_hashes',[])+[digest(cookie)])[-16:]
                secure = '; Secure' if CONFIG.get('tls_cert') or CONFIG.get('secure_cookie') else ''
                return self.respond(200, {'success':True}, cookie='evo_session='+cookie+'; Path=/; HttpOnly; SameSite=Strict'+secure)
            except (ValueError, TypeError, OSError): return self.respond(400, {'error':'Invalid login request'})
        grant_id = self.authenticate()
        if not grant_id:
            if method == 'GET' and path == '/':
                return self.respond(200, LOGIN, 'text/html; charset=utf-8')
            return self.respond(401, {'error':{'message':'Expired or invalid session','type':'authentication_error'}})
        if path == '/access/status': return self.respond(200, public_status())
        if path == '/access/logout' and method == 'POST':
            with LOCK:
                if GRANT and GRANT['id'] == grant_id:
                    cookies=[part.strip().split('=',1)[1] for part in self.headers.get('Cookie','').split(';')
                             if part.strip().startswith('evo_session=')]
                    hashes={digest(value) for value in cookies}
                    GRANT['cookie_hashes']=[value for value in GRANT.get('cookie_hashes',[]) if value not in hashes]
            return self.respond(200, {'success':True},cookie='evo_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0')
        get_routes = {'/v1/models','/models','/models/sse','/props','/health','/evo/status','/slots','/evo-monitor/metrics','/tools'}
        post_routes = {'/v1/chat/completions','/v1/completions','/completion','/tokenize','/detokenize',
                       '/apply-template','/v1/messages/count_tokens','/models/load','/models/unload','/tools','/evo/tools/cleanup'}
        static = (path=='/' or path.startswith(('/_app/','/chat/','/apple-splash-')) or
                  path in ('/evo-monitor.js','/evo-status.js','/favicon.ico','/favicon.svg','/manifest.webmanifest','/apple-touch-icon.png','/apple-touch-icon-180x180.png'))
        if mode == 'api':
            allowed = (method == 'GET' and path in {'/v1/models', '/health'} or
                       method == 'POST' and path in {'/v1/chat/completions', '/v1/completions'})
        else:
            allowed = (method=='GET' and (path in get_routes or static) or method=='POST' and path in post_routes)
        if not allowed:
            return self.respond(404, {'error':'Route is unavailable on temporary access'})
        body = None
        if method=='POST':
            try: body = self.body()
            except (ValueError, OSError): return self.respond(413, {'error':'Invalid request size'})
            if path in AUDIT_PATHS:
                try:
                    model=json.loads(body).get('model')
                    if model in ('qwen3.8-27b-uncensored','sc117-flash-next-iq3_xxs'):self.audit_model=model
                except (ValueError,AttributeError,TypeError):pass
        exclusive = method=='POST' and path in {'/v1/chat/completions','/v1/completions','/completion','/models/load','/models/unload'}
        if exclusive and not PROXY_GATE.acquire(False):
            return self.respond(429, {'error':{'message':'다른 요청 처리 중입니다. 완료 후 다시 시도하세요.','type':'server_busy'}})
        backend = http.client.HTTPConnection('127.0.0.1',8081 if path=='/evo-monitor/metrics' else 8080,timeout=1800)
        done = threading.Event()
        def watch():
            while not done.wait(0.2):
                if not alive(grant_id):
                    try:
                        if backend.sock: backend.sock.shutdown(socket.SHUT_RDWR)
                    except OSError: pass
                    try: self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError: pass
                    return
        try:
            target = '/api/metrics' if path=='/evo-monitor/metrics' else self.path
            backend.request(method,target,body=body,headers={'Content-Type':self.headers.get('Content-Type','application/json'),'Accept-Encoding':'identity','X-Evo-Request-ID':self.request_id})
            with LOCK:
                if backend.sock: CONNECTIONS[backend.sock] = grant_id
            threading.Thread(target=watch,daemon=True).start()
            response = backend.getresponse()
            if path=='/evo-monitor.js':
                script=response.read(256*1024).replace(b"api.port = '8081'; api.pathname = '/api/metrics';",b"api.pathname = '/evo-monitor/metrics';")
                return self.respond(response.status,script,'text/javascript; charset=utf-8')
            self.send_response(response.status)
            for key,value in response.getheaders():
                if key.lower() not in {'connection','transfer-encoding','server','date','set-cookie','access-control-allow-origin'}:
                    self.send_header(key,value)
            self.send_header('Connection','close')
            self.send_header('Referrer-Policy','no-referrer')
            self.end_headers()
            self.close_connection=True
            while alive(grant_id):
                chunk=response.read1(16384)
                if not chunk: break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (OSError, http.client.HTTPException):
            self.close_connection=True
        finally:
            done.set()
            with LOCK:
                if backend.sock: CONNECTIONS.pop(backend.sock,None)
            backend.close()
            if exclusive: PROXY_GATE.release()

class AdminHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(5)
        _,uid,_ = struct.unpack('3i',self.connection.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
        if uid not in CONFIG['admin_uids']: return
        try:
            raw=self.rfile.readline(4097)
            if len(raw)>4096: raise ValueError('Request too large')
            data=json.loads(raw)
            if not isinstance(data,dict): raise ValueError('Send an object')
            result=admin_action(data)
        except Exception as error:
            result={'success':False,'error':str(error) if isinstance(error,ValueError) else type(error).__name__}
        self.wfile.write((json.dumps(result)+'\n').encode())

class AdminServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads=True

def expire_loop():
    global GRANT
    while True:
        time.sleep(0.2)
        with ADMIN_LOCK:
            with LOCK:
                expired=bool(GRANT and 0 < GRANT['expires_at']<=time.time())
                if expired:
                    GRANT['expires_at']=0
                    GRANT['cookie_hashes']=[]
                    GRANT['last_action']='timer_expired'
                    GRANT['changed_at']=int(time.time())
            if expired: close_public()

if __name__=='__main__':
    os.umask(0o077)
    init_audit()
    GRANT=load_key()
    path=Path('/run/evo-access/admin.sock')
    path.unlink(missing_ok=True)
    server=AdminServer(str(path),AdminHandler)
    path.chmod(0o660)
    threading.Thread(target=expire_loop,daemon=True).start()
    print('access_admin_ready; public port closed',flush=True)
    resume_maintenance_state()
    server.serve_forever(poll_interval=0.2)
