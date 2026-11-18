"""Actual installed Pi task-yield regressions; scripted localhost provider only.

Usage: EVO_PI_RUNTIME=/path/to/pi python verify_pi_yield.py RUNNER REPORT.json
No real model, Discord, SSH, credentials or production jobs are used.
"""
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


def check(ok, label):
    assert ok, label
    checks.append(label)


def tool(name, args, index=0):
    return {'index': index, 'id': f'yield-{len(payloads)}-{index}', 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps(args)}}


class Provider(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['content-length'])))
        payloads.append(body)
        n = len(payloads)
        delta, finish = {'role': 'assistant'}, 'stop'
        if n == 1:
            if scenario == 'abort':
                calls = [tool('fixture_hold', {})]
            elif scenario in ('handoff', 'plain-enqueue'):
                args = {'action': 'enqueue', 'prompt': 'Registered worker task'}
                if scenario == 'handoff':
                    args['handoff'] = True
                calls = [tool('schedule', args)]
            else:
                calls = [tool('schedule', {'action': 'note', 'state': 'waiting', 'text': 'Worker running'})]
                if scenario == 'mixed':
                    calls.append(tool('fixture_hold', {}, 1))
            delta['tool_calls'], finish = calls, 'tool_calls'
        elif scenario == 'mixed' and n == 2:
            delta['tool_calls'] = [tool('schedule', {'action': 'note', 'state': 'waiting', 'text': 'Worker running'})]
            finish = 'tool_calls'
        elif scenario in ('failed-save', 'wrong-task', 'rejected-save') and n == 2 or scenario == 'steer' and n == 3:
            delta['tool_calls'] = [tool('schedule', {'action': 'note', 'state': 'done', 'text': 'Current state verified'})]
            finish = 'tool_calls'
        else:
            delta['content'] = 'OWNER-HANDLED' if scenario == 'steer' else 'FINAL-OK'
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Connection', 'close')
        self.end_headers()
        try:
            for change, stop in ((delta, None), ({}, finish)):
                chunk = {'id': 'yield-fixture', 'object': 'chat.completion.chunk', 'model': fixture.MODEL,
                         'choices': [{'index': 0, 'delta': change, 'finish_reason': stop}]}
                if stop:
                    chunk['usage'] = {'prompt_tokens': 1000, 'completion_tokens': 20, 'total_tokens': 1020}
                self.wfile.write(('data: ' + json.dumps(chunk) + '\n\n').encode())
                self.wfile.flush()
            self.wfile.write(b'data: [DONE]\n\n')
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


def main():
    global scenario
    native = Path(os.environ.get('EVO_PI_RUNTIME') or str(rpc.PI_RUNTIME))
    server = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    processes = []
    try:
        with tempfile.TemporaryDirectory(prefix='evo-pi-yield-') as tmp:
            home = Path(tmp)
            profile = home / '.pi/agent'; profile.mkdir(parents=True)
            workspace = home / 'workspace'; workspace.mkdir()
            (profile / 'config.yml').write_text('retry: {enabled: false}\ncompaction: {reserveTokens: 28000, keepRecentTokens: 12000}\n')
            (profile / 'models.yml').write_text(json.dumps({'providers': {'evo': {
                'baseUrl': f'http://127.0.0.1:{server.server_port}/v1', 'apiKey': 'test-only',
                'models': [{'id': fixture.MODEL, 'name': fixture.MODEL, 'reasoning': True,
                            'input': ['text'], 'contextWindow': 131072, 'maxTokens': 8192,
                            'cost': dict(input=0, output=0, cacheRead=0, cacheWrite=0)}]}}}))
            (home / 'instructions.txt').write_text('Local task-yield fixture; no external actions.')
            code = runner.read_text().replace("const home = '/home/agent';", 'const home = ' + json.dumps(tmp) + ';')
            for name in ('pi-tools.mjs', 'summary-guard.mjs'):
                code = code.replace("from './" + name + "'", 'from ' + json.dumps(str(runner.parent / name)))
            code = code.replace("'/workspace'", json.dumps(str(workspace)))
            candidate = home / 'runner.mjs'; candidate.write_text(code)
            observed = set()

            def fresh(kind):
                global scenario
                scenario = kind
                fixture.frames.clear(); payloads.clear(); observed.clear()
                process = subprocess.Popen(['node', str(candidate), fixture.MODEL], stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    env=dict(os.environ, EVO_PI_RUNTIME=str(native)))
                processes.append(process)
                client = fixture.Client(process)
                client.wait(lambda f: f.get('type') == 'ready')
                client.call('set_host_tools', tools=[
                    {'name': 'schedule', 'description': 'Record local task state.', 'loadMode': 'essential',
                     'parameters': {'type': 'object', 'properties': {
                         'action': {'type': 'string'}, 'state': {'type': 'string'},
                         'text': {'type': 'string'}, 'prompt': {'type': 'string'}, 'handoff': {'type': 'boolean'}},
                         'required': ['action']}},
                    {'name': 'fixture_hold', 'description': 'Hold a local tool boundary.', 'loadMode': 'essential',
                     'parameters': {'type': 'object', 'properties': {}}}])
                return client

            def next_host(client):
                match = lambda f: f.get('type') == 'host_tool_call' and f['id'] not in observed
                call = next((f for f in fixture.frames if match(f)), None) or client.wait(match)
                observed.add(call['id'])
                return call

            def reply_host(client, call, report, error=False):
                client.process.stdin.write(json.dumps({'type': 'host_tool_result', 'id': call['id'],
                    'isError': error, 'result': {'content': [{'type': 'text', 'text': json.dumps(report)}]}}) + '\n')
                client.process.stdin.flush()

            def begin(client, task=True):
                ident = client.send('prompt', message='Fixture owner task', **({'task_id': 'TASK-A'} if task else {}))
                check(client.reply(ident)['success'], scenario + ': prompt accepted')
                return ident

            def finished(client, ident):
                match = lambda f: f.get('type') == 'prompt_result' and f.get('id') == ident
                return next((f for f in fixture.frames if match(f)), None) or client.wait(match)

            def close(client):
                client.process.terminate()
                try:
                    client.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    client.process.kill(); client.process.wait()

            def waiting_ack(client, call):
                reply_host(client, call, {'saved': True, 'job_id': 'TASK-A', 'state': 'waiting'})

            client = fresh('waiting'); ident = begin(client)
            call = next_host(client)
            check(call['toolName'] == 'schedule' and call['arguments']['state'] == 'waiting', 'waiting: current task checkpoint requested')
            waiting_ack(client, call)
            result = finished(client, ident)
            check(result['status'] == 'completed' and result.get('disposition') == 'waiting'
                  and result.get('taskId') == 'TASK-A' and result['taskReported'], 'waiting: typed successful yield bound to saved task')
            check(len(payloads) == 1, 'waiting: no extra provider request or forced final')
            check(not any(f.get('type') == 'final_answer_recovery' for f in fixture.frames), 'waiting: empty-answer recovery not invoked')
            state = client.call('get_state')
            check(state['isSettled'] and state['queuedMessageCount'] == 0, 'waiting: runner fully settles')
            close(client)

            for kind in ('handoff', 'plain-enqueue'):
                client = fresh(kind); ident = begin(client, task=False)
                call = next_host(client)
                ack = {'success': True, 'id': 'NEW-TASK', 'continuation': True, 'mode': 'task'}
                if kind == 'handoff': ack['handoff'] = True
                reply_host(client, call, ack)
                result = finished(client, ident)
                check(result['status'] == 'completed', kind + ': prompt completes')
                if kind == 'handoff':
                    check(result.get('disposition') == 'queued' and result.get('taskId') == 'NEW-TASK', 'handoff: registered task returned to host')
                    check(len(payloads) == 1, 'handoff: owner turn ends before worker launch')
                else:
                    check('disposition' not in result and len(payloads) == 2, 'plain-enqueue: existing add-work semantics remain')
                close(client)

            for kind in ('failed-save', 'wrong-task', 'rejected-save'):
                client = fresh(kind); ident = begin(client)
                call = next_host(client)
                ack = {'saved': kind == 'wrong-task', 'job_id': 'OTHER' if kind == 'wrong-task' else 'TASK-A', 'state': 'waiting'}
                reply_host(client, call, ack, error=kind == 'rejected-save')
                repair = next_host(client)
                check(repair['arguments']['state'] == 'done', kind + ': model continues after invalid checkpoint')
                reply_host(client, repair, {'saved': True, 'job_id': 'TASK-A', 'state': 'done'})
                result = finished(client, ident)
                check(result['status'] == 'completed' and 'disposition' not in result, kind + ': invalid waiting acknowledgment never yields')
                check(len(payloads) == 3, kind + ': no replay of waiting operation')
                close(client)

            client = fresh('mixed'); ident = begin(client)
            call = next_host(client)
            check(call['toolName'] == 'fixture_hold', 'mixed: waiting save blocked before reaching host')
            reply_host(client, call, {'released': True})
            repair = next_host(client)
            check(repair['toolName'] == 'schedule', 'mixed: separate waiting save allowed')
            waiting_ack(client, repair)
            result = finished(client, ident)
            check(result.get('disposition') == 'waiting' and len(payloads) == 2, 'mixed: repaired checkpoint yields once')
            check(sum(f.get('type') == 'host_tool_call' and f.get('toolName') == 'schedule'
                      for f in fixture.frames) == 1, 'mixed: no hidden first checkpoint side effect')
            close(client)

            client = fresh('steer'); ident = begin(client)
            call = next_host(client)
            check(client.call('steer', message='OWNER-CORRECTION', onlyIfActive=True)['disposition'] == 'queued', 'steer: owner correction accepted before waiting acknowledgment')
            check(client.call('get_state')['queuedMessageCount'] == 1, 'steer: pending owner input visible')
            waiting_ack(client, call)
            repair = next_host(client)
            check(repair['arguments']['state'] == 'done', 'steer: latest input receives fresh bookkeeping')
            reply_host(client, repair, {'saved': True, 'job_id': 'TASK-A', 'state': 'done'})
            result = finished(client, ident)
            check(result['status'] == 'completed' and 'disposition' not in result, 'steer: old waiting acknowledgment does not end corrected work')
            users = [fixture.user_text(m) for m in payloads[-1]['messages'] if m['role'] == 'user']
            check(users.count('OWNER-CORRECTION') == 1, 'steer: normal completion retains correction exactly once')
            state = client.call('get_state')
            check(state['isSettled'] and state['queuedMessageCount'] == 0, 'steer: owner queue drains without being cleared')
            close(client)

            client = fresh('abort'); ident = begin(client)
            next_host(client)
            check(client.call('follow_up', message='MUST-NOT-LEAK')['disposition'] == 'queued', 'abort: queued follow-up accepted')
            abort = client.send('abort'); client.reply(abort)
            result = finished(client, ident)
            check(result['status'] == 'error' and 'disposition' not in result, 'abort: interruption is not reported as successful yield')
            state = client.call('get_state')
            check(state['isSettled'] and state['queuedMessageCount'] == 0, 'abort: explicit stop clears pending input')
            ident = begin(client, task=False)
            check(finished(client, ident)['status'] == 'completed', 'abort: next owner prompt works')
            check('MUST-NOT-LEAK' not in json.dumps(payloads[-1]), 'abort: cancelled follow-up does not leak into next prompt')
            close(client)

            report = {'passed': len(checks), 'failed': 0, 'checks': checks,
                      'runtime': json.loads((native / 'node_modules/@earendil-works/pi-coding-agent/package.json').read_text())['version'],
                      'runner_sha256': hashlib.sha256(runner.read_bytes()).hexdigest(),
                      'mode': 'actual installed Pi runner with scripted localhost HTTP',
                      'real_model_requests': 0, 'discord_posts': 0}
            output.write_text(json.dumps(report, indent=2))
            print(json.dumps(report), flush=True)
    finally:
        output.with_suffix('.diagnostic.json').write_text(json.dumps({'scenario': scenario, 'payloads': payloads, 'frames': fixture.frames}, indent=2))
        for process in processes:
            if process.poll() is None:
                process.kill(); process.wait()
        server.shutdown(); server.server_close()


if __name__ == '__main__':
    main()
