"""Exercise the real OMP binary and bwrap with a local scripted provider.

No Discord messages and no real root execution. User workspace is not reset.
"""
import asyncio
from collections import Counter
import contextlib
import json
from pathlib import Path
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import time

from evo_omp import context
from evo_omp.rpc import OmpSession, ROOT, WORKSPACE

COUNTS = Counter()
REQUESTS = []
PROBE = {'name': 'verify_probe', 'label': 'verify_probe', 'description': 'Local verification noop.',
         'loadMode': 'essential', 'parameters': {'type': 'object', 'properties': {
             'index': {'type': 'integer'}}, 'required': ['index']}}


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        REQUESTS.append(data)
        messages = data['messages']
        user_index = max(i for i, m in enumerate(messages) if m['role'] == 'user')
        text = messages[user_index]['content']
        if not isinstance(text, str):
            text = ' '.join(x.get('text', '') for x in text)
        mode = next((s for s in ('WRITE', 'HOST', 'BAD', 'LOOP', 'CANCEL', 'CONTINUE', '429', '500')
                     if 'VERIFY_' + s in text), 'HELLO')
        COUNTS[mode] += 1
        count = COUNTS[mode]
        tail = messages[user_index + 1:]
        tool = None
        if mode == 'WRITE':
            if not tail:
                tool = ('write', {'path': '/workspace/.omp-verify/file.txt', 'content': 'OMP_OK\n'})
            elif sum(m['role'] == 'tool' for m in tail) == 1:
                tool = ('bash', {'command': 'cat /workspace/.omp-verify/file.txt'})
            else:
                assert any('OMP_OK' in str(m['content']) for m in tail if m['role'] == 'tool')
        elif mode in ('HOST', 'LOOP'):
            if mode == 'LOOP' or not tail:
                tool = ('verify_probe', {'index': count})
        elif mode == 'BAD' and not tail:
            tool = ('not_a_declared_tool', {})
        elif mode == 'CANCEL' and not tail:
            tool = ('bash', {'command': 'touch /workspace/.omp-verify/sleep-start; sleep 30; touch /workspace/.omp-verify/late'})
        elif mode == '500' or (mode == '429' and count == 1):
            self.send_response(500 if mode == '500' else 429)
            body = json.dumps({'error': {'message': 'verification transient error', 'type': 'rate_limit_error'}}).encode()
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Retry-After', '1')
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Connection', 'close')
        self.end_headers()
        def emit(delta, finish=None):
            event = {'id': 'verify-' + str(len(REQUESTS)), 'object': 'chat.completion.chunk',
                'created': int(time.time()), 'model': data['model'],
                'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}
            self.wfile.write(('data: ' + json.dumps(event) + '\n\n').encode())
            self.wfile.flush()
        try:
            emit({'role': 'assistant'})
            if tool:
                emit({'tool_calls': [{'index': 0, 'id': 'call_' + str(len(REQUESTS)),
                    'type': 'function', 'function': {'name': tool[0], 'arguments': json.dumps(tool[1])}}]})
                emit({}, 'tool_calls')
            else:
                emit({'content': 'VERIFIED_' + mode})
                emit({}, 'stop')
            self.wfile.write(b'data: [DONE]\n\n')
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.close_connection = True


async def main():
    server = ThreadingHTTPServer(('127.0.0.1', 18088), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    temporary = tempfile.TemporaryDirectory(prefix='verify-config-', dir='/run/evo-omp')
    cfg = Path(temporary.name)
    source = Path(__file__).resolve().parent
    (cfg / 'models.yml').write_text((source / 'models.yml').read_text().replace(':8080/v1', ':18088/v1'))
    (cfg / 'config.yml').write_text((source / 'config.yml').read_text())
    verification_dir = WORKSPACE / '.omp-verify'
    verification_dir.mkdir(exist_ok=True)
    assert not verification_dir.is_symlink()
    for name in ('late', 'sleep-start', 'file.txt'):
        (verification_dir / name).unlink(missing_ok=True)
    probes = []
    async def host(name, args, turn):
        probes.append(args['index'])
        return {'index': args['index'], 'ok': True}
    s = OmpSession('verify', 'sc117-flash-next-iq3_xxs', [PROBE], host, cfg)
    results = {}
    try:
        await s.start()
        state = await s.request('get_state')
        assert state['model']['api'] == 'evo-bounded-completions'
        tools = {t['name'] for t in state['dumpTools']}
        assert {'read', 'grep', 'glob', 'write', 'edit', 'bash', 'verify_probe'} <= tools, tools
        assert 'task' not in tools
        print('PASS native startup and declared tool registry', flush=True)
        for mode in ('WRITE', 'HOST', 'BAD', '429'):
            answer = await asyncio.wait_for(s.prompt('VERIFY_' + mode, context.Turn('verify')), 60)
            assert answer == 'VERIFIED_' + mode, (mode, answer, s.error)
            results[mode] = {'requests': COUNTS[mode], 'tools': s.tools}
            print('PASS', mode, results[mode], flush=True)
        before = COUNTS['LOOP']
        with contextlib.suppress(Exception):
            await asyncio.wait_for(s.prompt('VERIFY_LOOP', context.Turn('verify')), 60)
        assert COUNTS['LOOP'] - before == 40, COUNTS['LOOP'] - before
        print('PASS exact 40 provider-dispatch cap', flush=True)
        pending = asyncio.create_task(s.prompt('VERIFY_CANCEL', context.Turn('verify')))
        for _ in range(100):
            if (verification_dir / 'sleep-start').exists():
                break
            await asyncio.sleep(.1)
        assert (verification_dir / 'sleep-start').exists(), 'bash did not start'
        start = time.monotonic()
        await s.abort()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(pending, 25)
        assert time.monotonic() - start < 15, 'cancellation too slow'
        answer = await asyncio.wait_for(s.prompt('VERIFY_CONTINUE', context.Turn('verify')), 20)
        assert answer == 'VERIFIED_CONTINUE', (answer, s.error)
        assert not (verification_dir / 'late').exists(), 'cancelled bash survived/replayed'
        print('PASS cancellation and corrected next prompt, no tool replay', flush=True)
        failed = False
        try:
            await asyncio.wait_for(s.prompt('VERIFY_500', context.Turn('verify')), 60)
        except Exception as e:
            failed = True
            assert str(e), 'empty failure'
        assert failed, 'failed provider was presented as success'
        assert COUNTS['500'] <= 3, ('multiplied retry attempts', COUNTS['500'])
        old_id = (await s.request('get_state'))['sessionId']
        assert await s.reusable_after_error(), 'settled failed provider destroyed session'
        answer = await s.prompt('VERIFY_CONTINUE recovery-after-500', context.Turn('verify'))
        assert answer == 'VERIFIED_CONTINUE', (answer, s.error)
        assert (await s.request('get_state'))['sessionId'] == old_id
        assert COUNTS['500'] <= 3, 'failed turn replayed during recovery'
        print('PASS exhausted provider error and same-conversation continuation without replay', flush=True)
        assert all(any(t['function']['name'] == 'verify_probe' for t in r['tools']) for r in REQUESTS)
        assert all(r['model'] == 'sc117-flash-next-iq3_xxs' for r in REQUESTS)
        for r in REQUESTS:
            edit = next(t['function'] for t in r['tools'] if t['function']['name'] == 'edit')
            assert 'old_string' in edit['parameters']['properties'], 'Model edit variant was not applied'
        # No owner credentials, private account data or historical persona in requests.
        text = json.dumps(REQUESTS)
        assert '/etc/evo-omp/gmail.json' not in text
        results['counts'] = dict(COUNTS)
        (Path('/run/evo-omp') / 'verification.json').write_text(json.dumps(results, indent=2))
        print('PASS actual provider payload registry and model identity', flush=True)
        qwen = OmpSession('verify-qwen', 'qwen3.8-27b-uncensored', [], None, cfg)
        try:
            await qwen.start()
            state = await qwen.request('get_state')
            assert state['model']['contextWindow'] == 131072, state['model']
            await qwen.prompt('VERIFY_HELLO', context.Turn('verify-qwen'))
            edit = next(t['function'] for t in REQUESTS[-1]['tools'] if t['function']['name'] == 'edit')
            assert 'old_string' in edit['parameters']['properties']
            assert REQUESTS[-1]['model'] == 'qwen3.8-27b-uncensored'
            print('PASS both model context/replace contracts in actual provider payloads', flush=True)
        finally:
            await qwen.close()
    finally:
        await s.close()
        server.shutdown()
        temporary.cleanup()
        for name in ('late', 'sleep-start', 'file.txt'):
            (verification_dir / name).unlink(missing_ok=True)
        verification_dir.rmdir()


if __name__ == '__main__':
    asyncio.run(main())
