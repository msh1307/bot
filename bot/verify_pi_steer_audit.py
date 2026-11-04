"""Installed Pi contract/race checks; scripted API and test-only public extension hooks."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

import verify_pi_steering as fixture

MODEL = fixture.MODEL
checks, payloads = [], []
stream_started, stream_release = threading.Event(), threading.Event()


class Provider(BaseHTTPRequestHandler):
    def log_message(self, *_): pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['content-length'])))
        payloads.append(body)
        last = body['messages'][-1]
        hold = last['role'] == 'user' and fixture.user_text(last).endswith(':HOLD')
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Connection', 'close')
        self.end_headers()
        if last['role'] == 'user' and fixture.user_text(last).endswith(':STREAM'):
            chunk = {'id': 'audit', 'object': 'chat.completion.chunk', 'model': MODEL,
                'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': 'STREAM_HEAD '}, 'finish_reason': None}]}
            self.wfile.write(('data: ' + json.dumps(chunk) + '\n\n').encode()); self.wfile.flush()
            stream_started.set(); stream_release.wait(10)
        delta = {'role': 'assistant'}
        if hold:
            delta['tool_calls'] = [{'index': 0, 'id': 'hold-' + str(len(payloads)), 'type': 'function',
                'function': {'name': 'fixture_hold', 'arguments': '{}'}}]
        else:
            delta['content'] = 'AUDIT_OK'
        for data, finish in ((delta, None), ({}, 'tool_calls' if hold else 'stop')):
            chunk = {'id': 'audit', 'object': 'chat.completion.chunk', 'model': MODEL,
                'choices': [{'index': 0, 'delta': data, 'finish_reason': finish}]}
            if finish: chunk['usage'] = {'prompt_tokens': 1000, 'completion_tokens': 10, 'total_tokens': 1010}
            self.wfile.write(('data: ' + json.dumps(chunk) + '\n\n').encode())
        self.wfile.write(b'data: [DONE]\n\n'); self.wfile.flush()


def check(ok, label, **evidence):
    checks.append({'passed': bool(ok), 'check': label, **evidence})


def users(body):
    return [fixture.user_text(m) for m in body['messages'] if m['role'] == 'user']


def main():
    runner, output = map(Path, sys.argv[1:3])
    server = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    native = runner.parent / 'bin/pi-1.4.0'
    if not native.exists(): native = Path('/opt/evo-bot/bin/pi-1.4.0')
    processes = []
    try:
        with tempfile.TemporaryDirectory(prefix='evo-steer-audit-') as tmp:
            home = Path(tmp)
            profile = home / '.pi/agent'; profile.mkdir(parents=True)
            workspace = home / 'workspace'; workspace.mkdir()
            (profile/'config.yml').write_text('retry: {enabled: false}\ncompaction: {reserveTokens: 28000, keepRecentTokens: 12000}\n')
            (profile/'models.yml').write_text(json.dumps({'providers': {'evo': {
                'baseUrl': f'http://127.0.0.1:{server.server_port}/v1', 'apiKey': 'test-only',
                'models': [{'id': MODEL, 'name': MODEL, 'reasoning': True, 'input': ['text', 'image'],
                    'contextWindow': 131072, 'maxTokens': 8192,
                    'cost': dict(input=0, output=0, cacheRead=0, cacheWrite=0)}]}}}))
            (home/'instructions.txt').write_text('Local transport fixture; no external actions.')
            code = runner.read_text().replace("const home = '/home/agent';", 'const home = ' + json.dumps(tmp) + ';')
            code = code.replace("from './pi-tools.mjs'", 'from ' + json.dumps(str(runner.parent/'pi-tools.mjs')))
            code = code.replace("'/workspace'", json.dumps(str(workspace)))
            # Only the fixture installs these hooks; production has no input handlers/commands.
            code = code.replace('extensionFactories: [createToolSearchExtension(),',
                                'extensionFactories: [createToolSearchExtension(), auditExtension,')
            if 'auditExtension,' not in code:
                code = code.replace('extensionFactories: [createToolSearchExtension()]',
                                    'extensionFactories: [createToolSearchExtension(), auditExtension]')
            code = code.replace("let definitions = [];", """
let releaseAuditInput;
function auditExtension(pi) {
  pi.on('input', async event => {
    if (event.text.startsWith('PAUSED-')) {
      emit({ type: 'fixture_input_wait' });
      await new Promise(resolve => { releaseAuditInput = resolve; });
    }
    if (event.text === 'HANDLED') return { action: 'handled' };
    return { action: 'continue' };
  });
  pi.registerCommand('fixture_command', { handler: async () => {} });
}
let definitions = [];""")
            code = code.replace("case 'negotiate_protocol':", "case 'fixture_release': releaseAuditInput?.(); return respond(c);\n    case 'negotiate_protocol':")
            patched = home/'runner.mjs'; patched.write_text(code)

            def fresh():
                fixture.frames.clear(); payloads.clear()
                process = subprocess.Popen(['node', str(patched), MODEL], stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    env=dict(os.environ, EVO_PI_RUNTIME=str(native)))
                processes.append(process)
                client = fixture.Client(process)
                client.wait(lambda f: f.get('type') == 'ready')
                client.call('set_host_tools', tools=[{'name': 'fixture_hold', 'loadMode': 'essential',
                    'description': 'Pause local fixture.', 'parameters': {'type': 'object', 'properties': {}}}])
                return client

            def hold(client, label):
                ident = client.send('prompt', message=label + ':HOLD'); client.reply(ident)
                call = client.wait(lambda f: f.get('type') == 'host_tool_call')
                return ident, call

            def release_tool(client, call):
                client.process.stdin.write(json.dumps({'type': 'host_tool_result', 'id': call['id'],
                    'result': {'content': [{'type': 'text', 'text': 'released'}]}}) + '\n')
                client.process.stdin.flush()

            def finished(client, ident):
                match = lambda f: f.get('type') == 'prompt_result' and f.get('id') == ident
                return next((f for f in fixture.frames if match(f)), None) or client.wait(match)

            def close(client):
                client.process.terminate(); client.process.wait(timeout=5)
                # Keep the reader from appending frames into the next fixture.
                time.sleep(.02)

            client = fresh(); prompt, call = hold(client, 'LIMIT')
            ids = [client.send('steer', message='INPUT-' + str(i), onlyIfActive=True) for i in range(20)]
            replies = [client.reply(i) for i in ids]
            count = client.call('get_state')['queuedMessageCount']
            check(sum(r['success'] for r in replies) == 16 and count == 16,
                  'concurrent onlyIfActive admissions obey 16 limit', accepted=sum(r['success'] for r in replies), queued=count)
            rejected = client.reply(client.send('follow_up', message='RAW-OVER-LIMIT'))
            check(not rejected['success'] and 'queue is full' in rejected.get('error', ''), 'raw follow_up cannot bypass queue cap')
            client.call('abort'); close(client)

            client = fresh(); prompt, call = hold(client, 'ABORT')
            steer = client.send('steer', message='PAUSED-ABORT', onlyIfActive=True)
            client.wait(lambda f: f.get('type') == 'fixture_input_wait')
            since = len(fixture.frames)
            aborts = [client.send('abort'), client.send('abort')]
            client.wait(lambda f: f.get('type') == 'agent_end')
            state = client.call('get_state')
            check(not state['isSettled'], 'abort/preflight is not reported settled', state={k: state[k] for k in
                  ('isSettled', 'queuedMessageCount', 'hasPendingAsyncWork')})
            check(state['hasPendingAsyncWork'], 'in-flight preflight remains visible as pending work')
            check(not any(f.get('type') == 'session_settled' or f.get('sessionSettled') is True
                          for f in fixture.frames[since:]), 'no early settled event while input preflight is pending')
            during = client.reply(client.send('steer', message='DURING-ABORT', onlyIfActive=True))
            check(not during['success'], 'concurrent abort keeps stopping gate closed')
            client.call('fixture_release')
            reply = client.reply(steer)
            for ident in aborts: client.reply(ident)
            state = client.call('get_state')
            check(not reply['success'] and state['queuedMessageCount'] == 0 and state['isSettled'],
                  'late steering cannot survive abort', disposition=reply.get('data'), queued=state['queuedMessageCount'])
            ident = client.send('prompt', message='AFTER-ABORT'); client.reply(ident)
            finished(client, ident)
            check('PAUSED-ABORT' not in json.dumps(payloads[-1]), 'aborted preflight never leaks into next prompt')
            close(client)

            client = fresh(); prompt, call = hold(client, 'SETTLE')
            steer = client.send('steer', message='PAUSED-SETTLE', onlyIfActive=True)
            client.wait(lambda f: f.get('type') == 'fixture_input_wait')
            release_tool(client, call)
            client.wait(lambda f: f.get('type') == 'message_end' and f.get('message', {}).get('role') == 'assistant'
                        and any(b.get('text') == 'AUDIT_OK' for b in f['message'].get('content', [])))
            state = client.call('get_state')
            check(not state['isSettled'], 'turn waits for already-admitted input preflight at settle boundary')
            client.call('fixture_release'); client.reply(steer)
            finished(client, prompt)
            state = client.call('get_state')
            check(state['queuedMessageCount'] == 0 and state['isSettled'] and users(payloads[-1]).count('PAUSED-SETTLE') == 1,
                  'end-of-turn preflight is consumed exactly once; no idle orphan', queued=state['queuedMessageCount'])
            idle = client.call('steer', message='IDLE', onlyIfActive=True)
            check(idle['disposition'] == 'not_running' and client.call('get_state')['queuedMessageCount'] == 0,
                  'idle guard returns not_running without queue mutation')
            close(client)

            client = fresh(); stream_started.clear(); stream_release.clear()
            ident = client.send('prompt', message='BOUNDARY:STREAM'); client.reply(ident)
            check(stream_started.wait(5), 'provider is streaming before steering admission')
            client.call('steer', message='STREAM-CORRECTION', onlyIfActive=True)
            state = client.call('get_state')
            check(len(payloads) == 1 and state['queuedMessageCount'] == 1 and state['isStreaming'],
                  'mid-stream input waits; it does not restart or rewrite current provider request')
            stream_release.set(); finished(client, ident)
            check(len(payloads) == 2 and users(payloads[-1]).count('STREAM-CORRECTION') == 1,
                  'stream-time correction enters exactly the next model call')
            close(client)

            client = fresh(); prompt, call = hold(client, 'VALIDATION')
            check(client.call('steer', message='HANDLED', onlyIfActive=True)['disposition'] == 'handled',
                  'native input interception returns handled')
            check(client.call('get_state')['queuedMessageCount'] == 0, 'handled does not enter model queue')
            invalid = client.reply(client.send('steer', message='/fixture_command', onlyIfActive=True))
            check(not invalid['success'] and 'cannot be queued' in invalid.get('error', ''),
                  'extension command error is explicit and does not enqueue')
            long_text = '가' * 40000
            valid = client.call('steer', message=long_text)
            check(valid['disposition'] == 'queued' and client.call('get_state')['queuedMessageCount'] == 1,
                  'long valid UTF-8 message is accepted without truncation')
            cleared = client.call('clear_queue')
            check(cleared['steering'] == [long_text], 'clear_queue returns the entire long message intact')
            for value in ('', '   ', None):
                reply = client.reply(client.send('steer', message=value))
                check(not reply['success'], 'invalid text rejected: ' + repr(value))
            bad = client.reply(client.send('steer', message='BAD-IMAGE', images=[{}]))
            check(not bad['success'], 'malformed image metadata is rejected')
            large = client.reply(client.send('steer', message='가' * 360000))
            check(not large['success'], 'UTF-8 payload beyond 1 MiB is rejected')
            check(client.call('get_state')['queuedMessageCount'] == 0, 'rejected inputs leave queue unchanged')
            close(client)
            client = fresh(); prompt, call = hold(client, 'MODES')
            image = {'type': 'image', 'mimeType': 'image/png', 'data':
                'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII='}
            for name, args in [('steer', {'message': 'S1', 'images': [image]}), ('steer', {'message': 'S2'}),
                               ('follow_up', {'message': 'F1'}), ('follow_up', {'message': 'F2'})]:
                client.call(name, **args)
            state = client.call('get_state')
            check(state['queuedMessageCount'] == 4 and not state['isSettled'], 'state includes both native queues')
            release_tool(client, call)
            finished(client, prompt)
            batches = [users(p) for p in payloads]
            check(all(s in batches[1] for s in ('S1', 'S2')) and 'F1' not in batches[1], 'steering all precedes follow-ups')
            check('F1' in batches[2] and 'F2' not in batches[2] and 'F2' in batches[3], 'follow-ups drain one-at-a-time')
            s1 = next(m for m in payloads[1]['messages'] if m['role'] == 'user' and fixture.user_text(m) == 'S1')
            check(any(b.get('type') == 'image_url' for b in s1['content']), 'steering image reaches next actual provider payload')
            check(all(batches[-1].count(s) == 1 for s in ('S1', 'S2', 'F1', 'F2')), 'queued messages appear once each')
            close(client)

            client = fresh(); prompt, call = hold(client, 'DISABLED-TEMPLATES')
            for text in ('/skill:not-installed args', '/not-installed-template args'):
                check(client.call('steer', message=text)['disposition'] == 'queued', 'disabled template stays literal: ' + text)
            release_tool(client, call)
            finished(client, prompt)
            check(all(s in users(payloads[-1]) for s in ('/skill:not-installed args', '/not-installed-template args')),
                  'no implicit skill or prompt-template expansion in production loader')
            close(client)
    finally:
        stream_release.set()
        for process in processes:
            if process.poll() is None:
                process.kill(); process.wait()
        server.shutdown(); server.server_close()
        report = {'runtime': 'actual installed Pi 1.4.0', 'runner_sha256': hashlib.sha256(runner.read_bytes()).hexdigest(),
            'checks': checks, 'passed': sum(c['passed'] for c in checks), 'failed': sum(not c['passed'] for c in checks),
            'test_only_extension_hooks': True, 'scripted_provider': True, 'discord_posts': 0, 'evo_model_requests': 0}
        output.write_text(json.dumps(report, indent=2))
        print(json.dumps(report), flush=True)
    if any(not c['passed'] for c in checks) and '--baseline' not in sys.argv: raise SystemExit(1)


if __name__ == '__main__': main()
