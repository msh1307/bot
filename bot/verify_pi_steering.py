"""Actual installed Pi + wrapper against a local fake API; no EVO inference or Discord."""
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = 'sc117-flash-next-iq3_xxs'
runner, output = map(Path, sys.argv[1:3])
baseline = '--expect-missing' in sys.argv[3:]
payloads, frames = [], []
checks = []

def check(condition, label):
    assert condition, label
    checks.append(label)

def user_text(message):
    blocks = message.get('content', '')
    return (blocks if isinstance(blocks, str) else '\n'.join(b.get('text', '') for b in blocks)).strip()

class Provider(BaseHTTPRequestHandler):
    def log_message(self, *_): pass
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['content-length'])))
        payloads.append(body)
        users = [user_text(m) for m in body['messages'] if m['role'] == 'user']
        last = users[-1]
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Connection', 'close')
        self.end_headers()
        hold = last in ('BASE', 'CANCEL-ME')
        delta = {'role': 'assistant'}
        if hold:
            delta['tool_calls'] = [{'index': 0, 'id': 'hold-' + str(len(payloads)), 'type': 'function',
                                    'function': {'name': 'fixture_hold', 'arguments': '{}'}}]
        else:
            delta['content'] = {'CORRECTION': 'CORRECTED', 'FOLLOWUP': 'FOLLOWUP-OK',
                                'AFTER-STOP': 'AFTER-STOP-OK'}.get(last, 'DONE')
        for choice, usage in [({'index': 0, 'delta': delta, 'finish_reason': None}, None),
                              ({'index': 0, 'delta': {}, 'finish_reason': 'tool_calls' if hold else 'stop'},
                               {'prompt_tokens': 1000, 'completion_tokens': 20, 'total_tokens': 1020})]:
            chunk = {'id': 'fixture', 'object': 'chat.completion.chunk', 'model': MODEL, 'choices': [choice]}
            if usage: chunk['usage'] = usage
            self.wfile.write(('data: ' + json.dumps(chunk) + '\n\n').encode())
        self.wfile.write(b'data: [DONE]\n\n')
        self.wfile.flush()

class Client:
    def __init__(self, process):
        self.process, self.frames, self.sequence = process, queue.Queue(), 0
        def read():
            for line in process.stdout:
                frame = json.loads(line)
                frames.append(frame)
                self.frames.put(frame)
        threading.Thread(target=read, daemon=True).start()
    def send(self, kind, **data):
        self.sequence += 1
        ident = str(self.sequence)
        self.process.stdin.write(json.dumps(dict(data, type=kind, id=ident)) + '\n')
        self.process.stdin.flush()
        return ident
    def wait(self, predicate, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try: frame = self.frames.get(timeout=.2)
            except queue.Empty:
                if self.process.poll() is not None:
                    raise AssertionError('Runner exited: ' + self.process.stderr.read()[-3000:])
                continue
            if predicate(frame): return frame
        raise AssertionError('Timed out; frames=' + json.dumps(frames[-8:]))
    def reply(self, ident):
        existing = next((f for f in frames if f.get('type') == 'response' and f.get('id') == ident), None)
        if existing is not None: return existing
        return self.wait(lambda f: f.get('type') == 'response' and f.get('id') == ident)
    def call(self, kind, **data):
        response = self.reply(self.send(kind, **data))
        assert response['success'], response
        return response.get('data', {})

def main():
    server = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    process = None
    try:
        with tempfile.TemporaryDirectory(prefix='evo-pi-steering-') as tmp:
            home = Path(tmp)
            profile = home / '.pi/agent'; profile.mkdir(parents=True)
            workspace = home / 'workspace'; workspace.mkdir()
            (profile / 'config.yml').write_text('retry: {enabled: false}\ncompaction: {reserveTokens: 28000, keepRecentTokens: 12000}\n')
            (profile / 'models.yml').write_text(json.dumps({'providers': {'evo': {
                'baseUrl': f'http://127.0.0.1:{server.server_port}/v1', 'api': 'openai-completions', 'apiKey': 'test-only',
                'models': [{'id': MODEL, 'name': MODEL, 'reasoning': True, 'input': ['text', 'image'],
                            'contextWindow': 131072, 'maxTokens': 8192,
                            'cost': dict(input=0, output=0, cacheRead=0, cacheWrite=0)}]}}}))
            (home / 'instructions.txt').write_text('Follow the current user task. This is a local transport fixture.')
            code = runner.read_text().replace("const home = '/home/agent';", 'const home = ' + json.dumps(tmp) + ';')
            code = code.replace("from './pi-tools.mjs'", 'from ' + json.dumps(str(runner.parent / 'pi-tools.mjs')))
            code = code.replace("'/workspace'", json.dumps(str(workspace)))
            patched = home / 'runner.mjs'; patched.write_text(code)
            process = subprocess.Popen(['node', str(patched), MODEL], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                env=dict(os.environ, EVO_PI_RUNTIME=str(runner.parent / 'bin/pi-1.4.0')))
            client = Client(process)
            client.wait(lambda f: f.get('type') == 'ready')
            tool = {'name': 'fixture_hold', 'description': 'Pause the local fixture.', 'loadMode': 'essential',
                    'parameters': {'type': 'object', 'properties': {}}}
            client.call('set_host_tools', tools=[tool])
            if baseline:
                failures = {}
                for kind in ('steer', 'follow_up'):
                    reply = client.reply(client.send(kind, message='probe'))
                    check(not reply['success'] and 'Unsupported host command' in reply['error'], kind + ' missing')
                    failures[kind] = reply['error']
                report = {'baseline_reproduced': failures, 'model_requests': len(payloads), 'discord_posts': 0}
            else:
                check(client.call('get_state')['queuedMessageCount'] == 0, 'initial queue empty')
                check(client.call('steer', message='IDLE-GUARD', onlyIfActive=True)['disposition'] == 'not_running',
                      'Discord end-of-turn race returns not_running')
                check(client.call('get_state')['queuedMessageCount'] == 0, 'idle guard never strands a queued input')
                prompt = client.send('prompt', message='BASE')
                check(client.reply(prompt)['success'], 'main prompt accepted')
                hold = client.wait(lambda f: f.get('type') == 'host_tool_call')
                image = {'type': 'image', 'mimeType': 'image/png', 'data':
                         'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII='}
                check(client.call('steer', message='CORRECTION', images=[image])['disposition'] == 'queued', 'steer accepted')
                check(client.call('follow_up', message='FOLLOWUP')['disposition'] == 'queued', 'follow-up accepted')
                state = client.call('get_state')
                check(state['queuedMessageCount'] == 2, 'both pending inputs reported')
                check(not state['isSettled'] and state['hasPendingAsyncWork'], 'active state not falsely settled')
                busy = client.reply(client.send('prompt', message='duplicate'))
                check(not busy['success'], 'concurrent second prompt rejected')
                process.stdin.write(json.dumps({'type': 'host_tool_result', 'id': hold['id'],
                    'result': {'content': [{'type': 'text', 'text': 'released'}]}}) + '\n'); process.stdin.flush()
                done = client.wait(lambda f: f.get('type') == 'prompt_result' and f.get('id') == prompt)
                check(done['status'] == 'completed', 'one prompt completes after all continuations')
                check(len(payloads) == 3, 'exactly three model calls, no duplicate replay')
                user_turns = [[user_text(m) for m in p['messages'] if m['role'] == 'user'] for p in payloads]
                check(user_turns[1][-1] == 'CORRECTION' and 'FOLLOWUP' not in user_turns[1], 'steer precedes follow-up')
                check(user_turns[2][-1] == 'FOLLOWUP', 'follow-up runs after corrected answer')
                correction = next(m for m in payloads[1]['messages'] if m['role'] == 'user' and user_text(m) == 'CORRECTION')
                check(any(b.get('type') == 'image_url' for b in correction['content']), 'steering image forwarded')
                check(all(user_turns[2].count(t) == 1 for t in ('BASE', 'CORRECTION', 'FOLLOWUP')), 'all owner inputs retained once')
                state = client.call('get_state')
                check(state['queuedMessageCount'] == 0 and state['isSettled'], 'settled state accurate')
                check(sum(f.get('type') == 'prompt_result' and f.get('id') == prompt for f in frames) == 1, 'single completion frame')
                client.call('steer', message='IDLE-QUEUED')
                state = client.call('get_state')
                check(state['queuedMessageCount'] == 1 and not state['isSettled'], 'idle queued input not hidden')
                cleared = client.call('clear_queue')
                check(cleared['steering'] == ['IDLE-QUEUED'] and not cleared['followUp'], 'explicit queue clearing')
                prompt = client.send('prompt', message='CANCEL-ME'); client.reply(prompt)
                client.wait(lambda f: f.get('type') == 'host_tool_call')
                client.call('follow_up', message='MUST-NOT-LEAK')
                abort = client.send('abort')
                done = client.wait(lambda f: f.get('type') == 'prompt_result' and f.get('id') == prompt)
                check(client.reply(abort)['success'], 'abort acknowledged')
                state = client.call('get_state')
                check(state['queuedMessageCount'] == 0 and state['isSettled'], 'stop clears pending continuation')
                prompt = client.send('prompt', message='AFTER-STOP'); client.reply(prompt)
                done = client.wait(lambda f: f.get('type') == 'prompt_result' and f.get('id') == prompt)
                check(done['status'] == 'completed', 'new owner task works after stop')
                check('MUST-NOT-LEAK' not in json.dumps(payloads[-1]), 'cancelled input never enters next task')
                for kind in ('steer', 'follow_up'):
                    invalid = client.reply(client.send(kind, message=''))
                    check(not invalid['success'] and 'nonempty string' in invalid['error'], kind + ' rejects invalid input')
                report = {'passed': len(checks), 'failed': 0, 'checks': checks, 'runtime': 'Pi 1.4.0',
                          'mode': 'actual installed Pi and wrapper, local fake API',
                          'provider_requests': len(payloads), 'evo_model_requests': 0, 'discord_posts': 0}
            report['runner_sha256'] = hashlib.sha256(runner.read_bytes()).hexdigest()
            output.write_text(json.dumps(report, indent=2))
            print(json.dumps(report), flush=True)
    finally:
        output.with_suffix('.diagnostic.json').write_text(json.dumps({'payloads': payloads, 'frames': frames}, indent=2))
        if process:
            process.terminate()
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired: process.kill(); process.wait()
        server.shutdown(); server.server_close()

if __name__ == '__main__': main()
