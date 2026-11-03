#!/usr/bin/env python3
import base64, selectors, fcntl, hashlib, json, os, socket, struct, subprocess, sys, time
SOCK = os.path.expanduser('~/.codex/app-server/evo.sock')
BIN = os.path.expanduser('~/.local/bin/codex-app-server')
LOG = os.path.expanduser('~/.codex/app-server/evo.log')

def spawn():
    with open(LOG, 'a') as log:
        return subprocess.Popen([BIN, '--listen', 'unix://' + SOCK], start_new_session=True,
                                stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)

def remaining(deadline):
    left = deadline - time.monotonic()
    if left <= 0: raise TimeoutError('Oracle Codex deadline exceeded; inspect state before retrying')
    return left

def open_socket(deadline):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(remaining(deadline)); s.connect(SOCK); return s
    except BaseException:
        s.close(); raise

def connect(deadline):
    try: return open_socket(deadline)
    except (FileNotFoundError, ConnectionRefusedError): pass
    os.makedirs(os.path.dirname(SOCK), mode=0o700, exist_ok=True)
    # Only one cold-start launcher; never stop or replace an existing daemon.
    with open(SOCK + '.start.lock', 'a') as lock:
        while True:
            remaining(deadline)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB); break
            except BlockingIOError: time.sleep(min(.1, remaining(deadline)))
        try: return open_socket(deadline)
        except (FileNotFoundError, ConnectionRefusedError): pass
        process = spawn()
        while True:
            try: return open_socket(deadline)
            except (FileNotFoundError, ConnectionRefusedError):
                if process.poll() is not None: raise RuntimeError('Oracle Codex daemon failed to start')
                time.sleep(min(.1, remaining(deadline)))

def readn(s, n, deadline):
    buf = b''
    while len(buf) < n:
        s.settimeout(remaining(deadline))
        chunk = s.recv(n - len(buf))
        if not chunk: raise SystemExit('socket closed')
        buf += chunk
    return buf

def frame_out(s, payload, opcode=1):
    head = bytearray([0x80 | opcode]); n = len(payload)
    mask = os.urandom(4)
    if n < 126: head.append(0x80 | n)
    elif n < 65536: head += bytearray([0x80 | 126]) + struct.pack('>H', n)
    else: head += bytearray([0x80 | 127]) + struct.pack('>Q', n)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    s.sendall(bytes(head) + mask + masked)

class WS:
    def __init__(self, s, deadline):
        self.s = s; self.buf = b''; self.deadline = deadline
        s.settimeout(remaining(deadline))
        key = base64.b64encode(os.urandom(16)).decode()
        s.sendall(('GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n'
                   'Connection: Upgrade\r\nSec-WebSocket-Key: ' + key +
                   '\r\nSec-WebSocket-Version: 13\r\n\r\n').encode())
        while b'\r\n\r\n' not in self.buf:
            if len(self.buf) > 16384: raise RuntimeError('WebSocket handshake exceeded size limit')
            self.buf += readn(s, 1, deadline)
        head, self.buf = self.buf.split(b'\r\n\r\n', 1)
        if b' 101 ' not in head.split(b'\r\n', 1)[0]:
            raise SystemExit('ws handshake failed: ' + head.split(b'\r\n', 1)[0].decode(errors='replace')[:120])
        headers = dict(line.split(b':', 1) for line in head.split(b'\r\n')[1:] if b':' in line)
        accept = {k.lower(): v.strip() for k,v in headers.items()}.get(b'sec-websocket-accept')
        expected = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest())
        if accept != expected: raise RuntimeError('Invalid WebSocket handshake accept')
    def send(self, obj):
        self.s.settimeout(remaining(self.deadline))
        frame_out(self.s, json.dumps(obj, separators=(',', ':')).encode())
    def frame(self, deadline):
        message = bytearray(); fragmented = False
        while True:
            remaining(deadline)
            while len(self.buf) < 2: self.buf += readn(self.s, 1, deadline)
            b0, b1 = self.buf[0], self.buf[1]
            op, ln = b0 & 0x0f, b1 & 0x7f
            need = 2 + (2 if ln == 126 else 8 if ln == 127 else 0)
            while len(self.buf) < need: self.buf += readn(self.s, 1, deadline)
            if ln == 126: ln = struct.unpack('>H', self.buf[2:4])[0]
            elif ln == 127: ln = struct.unpack('>Q', self.buf[2:10])[0]
            final = bool(b0 & 0x80)
            if b0 & 0x70 or b1 & 0x80 or op not in (0, 1, 8, 9, 10):
                raise RuntimeError('Unsupported WebSocket frame')
            if op >= 8 and (not final or ln > 125) or ln + len(message) > 8 * 2**20:
                raise RuntimeError('WebSocket frame exceeded protocol limits')
            while len(self.buf) < need + ln: self.buf += readn(self.s, min(65536, need + ln - len(self.buf)), deadline)
            payload, self.buf = self.buf[need:need + ln], self.buf[need + ln:]
            if op == 8: raise ConnectionError('Oracle Codex closed WebSocket')
            if op == 9:
                self.s.settimeout(remaining(deadline)); frame_out(self.s, payload, opcode=10); continue
            if op == 10: continue
            if op == 0 and not fragmented or op == 1 and fragmented:
                raise RuntimeError('Invalid WebSocket continuation')
            message.extend(payload)
            if final: return bytes(message)
            fragmented = True
    def text(self, deadline):
        while True:
            p = self.frame(deadline)
            try: return json.loads(p)
            except ValueError: raise RuntimeError('Oracle Codex sent invalid JSON')

def stdio():
    # Raw tunnel only: the EVO client owns the native WebSocket lifecycle and requests.
    s = connect(time.monotonic() + 20)
    s.setblocking(False); os.set_blocking(0, False); os.set_blocking(1, False)
    output = {s.fileno(): bytearray(), 1: bytearray()}
    inputs = {0: s.fileno(), s.fileno(): 1}
    with s, selectors.DefaultSelector() as poll:
        while True:
            for key in list(poll.get_map().values()): poll.unregister(key.fd)
            for fd in (0, 1, s.fileno()):
                flags = (selectors.EVENT_READ if fd in inputs and len(output[inputs[fd]]) < 8 * 2**20 else 0)
                if output.get(fd): flags |= selectors.EVENT_WRITE
                if flags: poll.register(fd, flags)
            for key, flags in poll.select():
                fd = key.fd
                if flags & selectors.EVENT_READ:
                    data = os.read(fd, 65536)
                    if not data: return
                    output[inputs[fd]].extend(data)
                if flags & selectors.EVENT_WRITE:
                    try: count = os.write(fd, output[fd])
                    except BlockingIOError: continue
                    del output[fd][:count]


def main():
    spec = sys.argv[1]
    if spec == '@stdio': return stdio()
    raw = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] != '-' else '{}'
    if raw.startswith('b64:'):
        raw = base64.b64decode(raw[4:]).decode()
    timeout = int(sys.argv[3]) if len(sys.argv) > 3 else 60
    if not 1 <= timeout <= 120: raise ValueError('timeout must be between 1 and 120 seconds')
    deadline = time.monotonic() + timeout
    ws = WS(connect(deadline), deadline)
    def expect(want_id):
        while True:
            msg = ws.text(deadline)
            if msg.get('id') == want_id: return msg
            if 'method' in msg and 'id' in msg and 'result' not in msg:
                ws.send({'jsonrpc': '2.0', 'id': msg['id'], 'error':
                         {'code': -32601, 'message': 'No approval/input client on this RPC connection'}})
    ws.send({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
             'params': {'clientInfo': {'name': 'evo-bot', 'title': 'EVO agent bridge', 'version': '1.0'},
                        'capabilities': {'experimentalApi': True}}})
    r = expect(1)
    if 'error' in r: print(json.dumps(r['error'], ensure_ascii=False)); raise SystemExit(2)
    ws.send({'jsonrpc': '2.0', 'method': 'initialized'})
    if spec == '@multi':
        calls = json.loads(raw)['calls']
        out = []
        done = []
        for n, call in enumerate(calls, start=2):
            def deref(v):
                if isinstance(v, dict) and '__ref' in v:
                    target = done[v['__ref'][0]]
                    for part in v['__ref'][1:]:
                        target = (target or {}).get(part) if isinstance(target, dict) else None
                    return target
                if isinstance(v, dict): return {k: deref(x) for k, x in v.items()}
                if isinstance(v, list): return [deref(x) for x in v]
                return v
            call = deref(call)
            ws.send({'jsonrpc': '2.0', 'id': n, 'method': call['method'], 'params': call.get('params', {})})
            r = expect(n)
            out.append({'error': r['error']} if 'error' in r else r.get('result'))
            done.append(out[-1])
            if 'error' in r: break  # Never execute later writes after a failed prerequisite.
        print(json.dumps(out, ensure_ascii=False))
        return
    params = json.loads(raw)
    ws.send({'jsonrpc': '2.0', 'id': 2, 'method': spec, 'params': params})
    r = expect(2)
    if 'error' in r: print(json.dumps(r['error'], ensure_ascii=False)); raise SystemExit(2)
    print(json.dumps(r.get('result'), ensure_ascii=False))

if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, ValueError) as error:
        print(json.dumps({'message': str(error)}, ensure_ascii=False))
        raise SystemExit(2)
