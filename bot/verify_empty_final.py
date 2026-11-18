"""Actual Pi + scripted provider: empty finals recover once without executing tools."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from evo_bot import rpc

import verify_pi_steering as fixture

runner, output = map(Path, sys.argv[1:3])
checks, payloads = [], []
scenario = ''
held, release = threading.Event(), threading.Event()


class Provider(BaseHTTPRequestHandler):
    def log_message(self, *_): pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['content-length'])))
        payloads.append(body)
        n = len(payloads)
        if scenario == 'provider-error':
            self.send_response(400); self.end_headers()
            self.wfile.write(b'{"error":{"message":"terminal fixture error"}}'); return
        self.send_response(200); self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Connection', 'close'); self.end_headers()
        if scenario == 'abort' or scenario == 'steer' and n == 2:
            held.set(); release.wait(10)
        delta, finish = {'role': 'assistant'}, 'stop'
        if scenario == 'normal' or n > 1 and scenario not in ('still-empty', 'bad-tool'):
            delta['content'] = 'FINAL-OK'
        elif scenario == 'bad-tool' and n == 2:
            delta['tool_calls'] = [{'index': 0, 'id': 'forbidden-replay', 'type': 'function',
                'function': {'name': 'fixture_hold', 'arguments': '{}'}}]
            finish = 'tool_calls'
        else:
            delta['reasoning_content'] = 'Private synthetic reasoning without a final answer.'
        try:
            for change, stop in ((delta, None), ({}, finish)):
                frame = {'id': 'empty-final-fixture', 'object': 'chat.completion.chunk',
                    'model': fixture.MODEL, 'choices': [{'index': 0, 'delta': change, 'finish_reason': stop}]}
                if stop: frame['usage'] = {'prompt_tokens': 1000, 'completion_tokens': 26, 'total_tokens': 1026}
                self.wfile.write(('data: '+json.dumps(frame)+'\n\n').encode()); self.wfile.flush()
            self.wfile.write(b'data: [DONE]\n\n'); self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError): pass


def check(ok, label):
    assert ok, label
    checks.append(label)


def main():
    global scenario
    server = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    native = Path(os.environ.get('EVO_PI_RUNTIME') or str(rpc.PI_RUNTIME))
    processes = []
    try:
        with tempfile.TemporaryDirectory(prefix='evo-empty-final-') as directory:
            home = Path(directory); profile = home/'.pi/agent'; profile.mkdir(parents=True)
            workspace = home/'workspace'; workspace.mkdir()
            (profile/'config.yml').write_text('retry: {enabled: false}\ncompaction: {reserveTokens: 31072, keepRecentTokens: 12000}\n')
            (profile/'models.yml').write_text(json.dumps({'providers': {'evo': {
                'baseUrl': f'http://127.0.0.1:{server.server_port}/v1', 'apiKey': 'synthetic-only',
                'models': [{'id': fixture.MODEL, 'name': fixture.MODEL, 'reasoning': True,
                    'input': ['text', 'image'], 'contextWindow': 131072, 'maxTokens': 8192,
                    'cost': dict(input=0, output=0, cacheRead=0, cacheWrite=0)}]}}}))
            (home/'instructions.txt').write_text('Synthetic fixture. Answer briefly. No external actions.')
            code = runner.read_text().replace("const home = '/home/agent';", 'const home = '+json.dumps(directory)+';')
            code = code.replace("from './pi-tools.mjs'", 'from '+json.dumps(str(runner.parent/'pi-tools.mjs')))
            code = code.replace("'/workspace'", json.dumps(str(workspace)))
            candidate = home/'runner.mjs'; candidate.write_text(code)

            def fresh(kind):
                global scenario
                scenario = kind; payloads.clear(); fixture.frames.clear(); held.clear(); release.clear()
                p = subprocess.Popen(['node', str(candidate), fixture.MODEL], stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    env=dict(os.environ, EVO_PI_RUNTIME=str(native)))
                processes.append(p); client = fixture.Client(p)
                client.wait(lambda f: f.get('type') == 'ready')
                client.call('set_host_tools', tools=[{'name': 'fixture_hold', 'loadMode': 'essential',
                    'description': 'Synthetic action that must never run during recovery.',
                    'parameters': {'type': 'object', 'properties': {}}}])
                return client

            def done(client, ident):
                match = lambda f: f.get('type') == 'prompt_result' and f.get('id') == ident
                return next((f for f in fixture.frames if match(f)), None) or client.wait(match)

            def close(client):
                client.process.terminate(); client.process.wait(timeout=5)

            def texts():
                return [b['text'] for f in fixture.frames if f.get('type') == 'message_end'
                    and f.get('message', {}).get('role') == 'assistant'
                    for b in f['message']['content'] if b.get('type') == 'text']

            if '--baseline' in sys.argv:
                client = fresh('empty'); ident = client.send('prompt', message='Fixture task.')
                client.reply(ident); result = done(client, ident)
                check(result['status'] == 'completed' and not texts() and len(payloads) == 1,
                      'Live baseline reports completed with no final answer')
                close(client)
            else:
                for kind in ('normal', 'empty', 'still-empty', 'bad-tool', 'provider-error'):
                    client = fresh(kind); ident = client.send('prompt', message='Fixture task.')
                    client.reply(ident); result = done(client, ident)
                    expected = 1 if kind in ('normal', 'provider-error') else 2
                    check(len(payloads) == expected, kind+': bounded provider calls')
                    check(not any(f.get('type') == 'host_tool_call' for f in fixture.frames), kind+': no tool execution')
                    if kind in ('normal', 'empty'):
                        check(result['status'] == 'completed' and 'FINAL-OK' in texts(), kind+': final delivered')
                    else: check(result['status'] == 'error', kind+': failure remains explicit')
                    if expected == 2:
                        recovery = payloads[1]
                        check(recovery.get('tools') == [] and recovery.get('tool_choice') == 'none', kind+': tools disabled')
                        check(recovery['chat_template_kwargs']['enable_thinking'] is False, kind+': answer-only recovery')
                        check(sum(f.get('type') == 'final_answer_recovery' for f in fixture.frames) == 1, kind+': exactly one recovery')
                    if kind == 'empty':
                        payloads.clear(); fixture.frames.clear()
                        second = client.send('prompt', message='Another fixture task.')
                        client.reply(second)
                        check(done(client, second)['status'] == 'completed' and len(payloads) == 2,
                              'empty: next prompt gets its own single recovery')
                    close(client)
                client = fresh('steer'); ident = client.send('prompt', message='Fixture task.')
                client.reply(ident); check(held.wait(10), 'steer: recovery request started')
                check(client.call('steer', message='New owner instruction', onlyIfActive=True)['disposition'] == 'queued',
                      'steer: owner input accepted during recovery')
                release.set(); check(done(client, ident)['status'] == 'completed', 'steer: run completed')
                check(len(payloads) == 3 and payloads[2].get('tools'), 'steer: next owner turn restores tools')
                check(client.call('get_state')['isSettled'], 'steer: queue drained and settled')
                close(client)
                client = fresh('abort'); ident = client.send('prompt', message='Fixture task.')
                client.reply(ident); check(held.wait(10), 'abort: provider request started')
                abort = client.send('abort'); client.reply(abort); release.set(); done(client, ident)
                check(len(payloads) == 1 and not any(f.get('type') == 'final_answer_recovery' for f in fixture.frames),
                      'abort: no recovery after owner stop')
                close(client)
    finally:
        release.set()
        for p in processes:
            if p.poll() is None: p.kill(); p.wait()
        server.shutdown(); server.server_close()
        report = {'passed': len(checks), 'checks': checks, 'runner_sha256': hashlib.sha256(runner.read_bytes()).hexdigest(),
                  'actual_installed_pi': True, 'scripted_provider': True, 'evo_model_requests': 0, 'discord_posts': 0}
        output.write_text(json.dumps(report, indent=2)); print(json.dumps(report), flush=True)


if __name__ == '__main__': main()
