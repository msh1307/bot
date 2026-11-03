"""Private Web UI Python bridge in its own service cgroup."""
import atexit
import contextlib
import json
import os
from pathlib import Path
import queue
import select
import signal
import socket
import socketserver
import struct
import subprocess
import threading
import time

SOCKET = Path('/var/lib/evo-bot/python.sock')
SOURCE_LIMIT = 96 * 1024
COMMAND_LIMIT = 16 * 1024
KERNEL = None
SERVER = None
LOCK = threading.Lock()
DISPATCH_LOCK = threading.Lock()
JOBS = queue.Queue()
BUFFER = bytearray()
SANDBOX = None


def stop_kernel():
    global KERNEL
    proc, KERNEL = KERNEL, None
    if proc is not None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=3)
        for stream in (proc.stdin, proc.stdout):
            if stream:
                stream.close()
    BUFFER.clear()


def read_kernel(timeout):
    deadline = time.monotonic() + timeout
    while True:
        if b'\n' in BUFFER:
            line, _, remaining = BUFFER.partition(b'\n')
            BUFFER[:] = remaining
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError('Invalid kernel response')
            return value
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Python execution timed out')
        if not select.select([KERNEL.stdout], [], [], remaining)[0]:
            raise TimeoutError('Python execution timed out')
        block = os.read(KERNEL.stdout.fileno(), 16384)
        if not block:
            raise RuntimeError('Python kernel exited')
        BUFFER.extend(block)
        if len(BUFFER) > 512 * 1024:
            raise ValueError('Kernel response is too large')


def ensure_kernel():
    global KERNEL
    if KERNEL is not None and KERNEL.poll() is None:
        return False
    stop_kernel()
    KERNEL = subprocess.Popen(SANDBOX('exec /opt/evo-python/venv/bin/python -u /opt/evo-python/kernel.py'),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env={'PATH': '/usr/bin:/bin'}, start_new_session=True,
        cwd='/var/lib/evo-bot/workspace')
    ready = read_kernel(12)
    if not ready.get('ready'):
        raise RuntimeError('Python did not become ready')
    return True


def execute(params):
    if not isinstance(params, dict):
        return {'error': 'Send Python parameters as an object.'}
    shell = params.get('shell')
    code = params.get('code')
    timeout = params.get('timeout_seconds', 60)
    if shell is not None and not isinstance(code, str):
        if not isinstance(shell, str) or not shell.strip() or len(shell.encode()) > COMMAND_LIMIT:
            return {'error': 'Send nonempty shell command, at most 16 KiB.'}
        mode = 'shell'
    elif isinstance(code, str) and code.strip() and len(code.encode()) <= SOURCE_LIMIT:
        mode = 'python'
    else:
        return {'error': 'Send Python code (at most 96 KiB) or a shell command (at most 16 KiB).'}
    if type(timeout) is not int or not 1 <= timeout <= 120:
        return {'error': 'timeout_seconds must be an integer from 1 to 120.'}
    if not LOCK.acquire(blocking=False):
        return {'error': 'The shared Python interpreter is busy. Retry after the current calculation.'}
    try:
        started = ensure_kernel()
        if mode == 'shell':
            request = {'shell': shell, 'timeout': timeout}
            cwd = params.get('cwd')
            if isinstance(cwd, str) and cwd.strip():
                request['cwd'] = cwd
            extra_env = params.get('env')
            if isinstance(extra_env, dict):
                request['env'] = extra_env
        else:
            request = {'code': code}
        KERNEL.stdin.write((json.dumps(request) + '\n').encode())
        KERNEL.stdin.flush()
        result = read_kernel(timeout)
        result['kernel_started'] = started
        text = json.dumps(result, ensure_ascii=False)
        return {'error': text} if result.get('error_type') else {'plain_text_response': text}
    except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
        stop_kernel()
        return {'error': f'{type(exc).__name__}: calculation stopped; Python variables were reset. Workspace files remain.'}
    finally:
        LOCK.release()


def worker():
    # bwrap --die-with-parent follows the spawning Linux thread. Keep that
    # thread alive across HTTP requests so the persistent kernel stays alive.
    while True:
        action, params, result, done = JOBS.get()
        try:
            if action == 'warmup':
                with LOCK:
                    ensure_kernel()
                result['value'] = {'ready': True}
            elif action == 'reset_workspace':
                with LOCK:
                    stop_kernel()
                    from evo_workspace_reset import clear_workspace
                    result['value'] = clear_workspace()
            else:
                result['value'] = execute(params)
        except BaseException as exc:
            stop_kernel()
            result['value'] = {'error': 'Python sandbox unavailable: ' + type(exc).__name__}
        finally:
            done.set()


def dispatch(action, params=None):
    if not DISPATCH_LOCK.acquire(blocking=False):
        return {'error': 'The shared Python interpreter is busy. Retry after the current calculation.'}
    try:
        result, done = {}, threading.Event()
        JOBS.put((action, params, result, done))
        if not done.wait(145):
            return {'error': 'Python worker did not respond in time.'}
        return result['value']
    finally:
        DISPATCH_LOCK.release()


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(5)
        _, uid, _ = struct.unpack('3i', self.connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if uid not in (0, os.getuid()):
            return
        try:
            raw = self.rfile.readline(SOURCE_LIMIT * 8 + 1)
            if len(raw) > SOURCE_LIMIT * 8 or not raw.endswith(b'\n'):
                raise ValueError('Request is too large')
            data = json.loads(raw)
            if data.get('action') == 'status':
                if data.get('warmup') and uid == 0:
                    warmed = dispatch('warmup')
                    if warmed.get('error'):
                        raise RuntimeError('Python sandbox startup failed')
                result = {'ready': True, 'kernel_loaded': KERNEL is not None and KERNEL.poll() is None,
                          'busy': LOCK.locked(), 'shared_workspace': '/workspace'}
            elif data.get('tool') == 'run_python':
                result = dispatch('execute', data.get('params'))
            elif data.get('action') == 'reset_workspace':
                result = dispatch('reset_workspace')
            else:
                result = {'error': 'Only run_python is available.'}
        except (OSError, ValueError, AttributeError, RuntimeError) as exc:
            result = {'error': type(exc).__name__}
        with contextlib.suppress(OSError):
            self.wfile.write((json.dumps(result) + '\n').encode())


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    request_queue_size = 8
    def handle_error(self, request, client_address):
        # Never write source code or calculation results to journal.
        print('web_python_bridge_handler_error', flush=True)


def start_bridge(sandbox_factory):
    global SERVER, SANDBOX
    if SERVER is not None:
        return
    SANDBOX = sandbox_factory
    for name in ('.home', '.evo-tmp'):
        (Path('/var/lib/evo-bot/workspace') / name).mkdir(mode=0o700, exist_ok=True)
    SOCKET.unlink(missing_ok=True)
    SERVER = Server(str(SOCKET), Handler)
    SOCKET.chmod(0o600)
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=SERVER.serve_forever, kwargs={'poll_interval': 0.2}, daemon=True).start()
    atexit.register(stop_kernel)
    print('web_python_bridge_ready; interpreter loads on demand', flush=True)
