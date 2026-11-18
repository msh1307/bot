"""Private Unix-socket multiplexer for the existing browser worker protocol."""
import contextlib
import fcntl
import json
import os
from pathlib import Path
import select
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parent
SOCKET = ROOT / '.browser.sock'
MAX_FRAME = 4 * 2**20


class Backend:
    def __init__(self):
        self.lock = threading.RLock()
        self.process = None
        self.buffer = bytearray()
        self.clients = 0
        self.connections = set()
        self.info = {}

    def running(self):
        return self.process is not None and self.process.poll() is None

    def close(self):
        process, self.process = self.process, None
        if process:
            with contextlib.suppress(OSError): process.stdin.close()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError): os.killpg(process.pid, signal.SIGTERM)
                try: process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(ProcessLookupError): os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=3)
            process.stdout.close()
        self.buffer.clear()
        self.info.clear()

    def request(self, request, connection):
        method, params = request.get('method'), request.get('params', {})
        if method not in ('ready', 'call', 'credential_fill', 'reset') or not isinstance(params, dict):
            raise ValueError('Unsupported browser request')
        deadline = time.monotonic() + (30 if method in ('ready', 'reset') else params.get('timeout_ms', 15000)/1000 + 5)
        if not self.lock.acquire(timeout=max(0, deadline-time.monotonic())):
            raise TimeoutError('Browser busy; request was not executed. No replay.')
        try:
            if time.monotonic() >= deadline or (select.select([connection], [], [], 0)[0]
                    and connection.recv(1, socket.MSG_PEEK) == b''):
                raise TimeoutError('Browser request cancelled before execution. No replay.')
            action = params.get('action') if method == 'call' else None
            if action == 'status':
                return {'id': request['id'], 'result': {'running': self.running(), **self.info,
                    'engine': 'Camoufox', 'instance': 'oracle', 'persistent_profile': True,
                    'shared_owner_session': True, 'idle_close_minutes': None}}
            if method == 'reset' or action in ('close', 'reset'):
                self.close()
                if method == 'reset' or action == 'reset':
                    for other in self.connections - {connection}:
                        with contextlib.suppress(OSError): other.shutdown(socket.SHUT_RDWR)
                    done = subprocess.run([str(ROOT/'venv/bin/python'), str(ROOT/'browser_remote.py'),
                        '--direct', '--reset'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
                    if done.returncode: raise RuntimeError('Browser profile reset failed')
                return {'id': request['id'], 'result': {'success': True, 'closed': True,
                    'profile_kept': method != 'reset' and action != 'reset'}}
            if not self.running():
                if method not in ('ready',) and action != 'open':
                    raise ValueError('Shared browser closed; open the page before continuing')
                self.close()
                self.process = subprocess.Popen([str(ROOT/'venv/bin/python'), str(ROOT/'browser_remote.py'), '--direct'],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    start_new_session=True, bufsize=0)
            raw = (json.dumps(request, ensure_ascii=False)+'\n').encode()
            if len(raw) > 60000: raise ValueError('Browser request is too large')
            try:
                self.process.stdin.write(raw)
                self.process.stdin.flush()
                while time.monotonic() < deadline:
                    if b'\n' in self.buffer:
                        line, _, rest = self.buffer.partition(b'\n'); self.buffer = bytearray(rest)
                        response = json.loads(line)
                        if response.get('id') != request['id']: raise ConnectionError('Browser response ID mismatch')
                        result = response.get('result', {})
                        for key in ('url', 'version', 'images_enabled'):
                            if key in result: self.info[key] = result[key]
                        return response
                    if not select.select([self.process.stdout], [], [], min(.1, max(0, deadline-time.monotonic())))[0]:
                        continue
                    chunk = os.read(self.process.stdout.fileno(), 65536)
                    if not chunk: raise ConnectionError('Browser worker exited; reopen before continuing. No replay.')
                    self.buffer.extend(chunk)
                    if len(self.buffer) > MAX_FRAME: raise ValueError('Browser response exceeds 4MiB')
                raise TimeoutError('Browser request timed out; it may have applied. No replay.')
            except BaseException:
                self.close()
                raise
        finally:
            self.lock.release()


def serve():
    os.umask(0o077)
    lease = (ROOT/'.broker.lock').open('a')
    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
    backend = Backend()
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            with backend.lock:
                backend.clients += 1
                backend.connections.add(self.connection)
            try:
                while True:
                    line = self.rfile.readline(60001)
                    if not line or len(line) > 60000: break
                    request = None
                    try:
                        request = json.loads(line)
                        if not isinstance(request, dict) or 'id' not in request: raise ValueError('Invalid frame')
                        response = backend.request(request, self.connection)
                    except Exception:
                        # Browser/library errors may include input values; never log/echo them.
                        response = {'id': request.get('id') if isinstance(request, dict) else None, 'error': {'message':
                            'Shared browser operation failed; inspect status before retrying. No replay.'}}
                    try:
                        self.wfile.write((json.dumps(response, ensure_ascii=False)+'\n').encode())
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError): break
            finally:
                with backend.lock:
                    backend.clients -= 1
                    backend.connections.discard(self.connection)
                    if backend.clients == 0: backend.close()
    class Server(socketserver.ThreadingUnixStreamServer):
        daemon_threads = True
    if SOCKET.exists(): SOCKET.unlink()
    with Server(str(SOCKET), Handler) as server:
        signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
        try: server.serve_forever(poll_interval=.5)
        finally:
            with backend.lock: backend.close()
            SOCKET.unlink(missing_ok=True)


def proxy(reset=False):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(100)
        sock.connect(str(SOCKET))
        stream = sock.makefile('rwb')
        while True:
            if reset:
                line = b'{"id":1,"method":"reset","params":{}}\n'
            else:
                ready, _, _ = select.select([sys.stdin.buffer, sock], [], [])
                if sock in ready: return  # Broker/reset disconnected this client; never keep a stale frontend alive.
                line = sys.stdin.buffer.readline()
                if not line: return
            if len(line) > 60000: raise ValueError('Browser request is too large')
            stream.write(line); stream.flush()
            result = stream.readline(MAX_FRAME+1)
            if not result or len(result) > MAX_FRAME: raise ConnectionError('Shared browser disconnected. No replay.')
            if reset:
                if json.loads(result).get('error'): raise RuntimeError('Browser reset was not confirmed')
                return
            else:
                sys.stdout.buffer.write(result); sys.stdout.buffer.flush()


if __name__ == '__main__': serve()
