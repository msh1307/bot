"""Private three-turn real-model check of completed reasoning removal.

Run as bot user: python verify_context_model.py PROVIDER_CANDIDATE REPORT_DIR
Only a synthetic read-only report; no Discord posts, model switch or live bot restart.
"""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import urllib.request

sys.path.insert(0, '/opt/evo-omp')
from evo_omp import context, rpc

REQUESTS = []


def idle():
    state = json.loads((rpc.RUNTIME / 'status.json').read_text())
    if state.get('active') or state.get('queued') or state.get('pending_inputs'):
        raise RuntimeError('Live bot is busy; defer validation')
    with urllib.request.urlopen('http://127.0.0.1:8080/evo/status', timeout=5) as response:
        state = json.load(response)
    if state.get('busy') or state.get('loading') or state.get('active') != 'sc117-flash-next-iq3_xxs':
        raise RuntimeError('Requires idle loaded Flash')


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        row = {'request': body, 'started': time.monotonic()}; REQUESTS.append(row)
        request = urllib.request.Request('http://127.0.0.1:8082/v1/chat/completions',
            data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=240) as response:
            self.send_response(200); self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Connection', 'close'); self.end_headers()
            for line in response:
                self.wfile.write(line); self.wfile.flush()
                if line.startswith(b'data: {'):
                    event = json.loads(line[6:])
                    if event.get('usage'): row['usage'] = event['usage']
                    if event.get('timings'): row['timings'] = event['timings']
        row['seconds'] = time.monotonic() - row['started']; self.close_connection = True


async def main():
    candidate, report = map(Path, sys.argv[1:3]); report.mkdir(parents=True, exist_ok=True)
    idle()
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    cfg = Path(tempfile.mkdtemp(prefix='context-model-', dir=rpc.RUNTIME))
    (cfg / 'models.yml').write_text((rpc.ROOT / 'models.yml').read_text().replace(':8080/v1', f':{server.server_port}/v1'))
    shutil.copyfile(rpc.ROOT / 'config.yml', cfg / 'config.yml')
    original_argv = rpc.sandbox_argv
    def candidate_argv(profile, model):
        args = original_argv(profile, model); args[args.index(str(rpc.ROOT / 'provider.ts'))] = str(candidate)
        return args
    calls = []
    async def host(name, args, turn):
        assert name == 'codex_task' and args == {'action': 'status'}
        calls.append(args)
        return {'state': 'completed', 'thread_id': 'synthetic',
                'answer': 'revision=rev-A17; passed=17; failed=6; owner_scope=READ_ONLY'}
    definition = {'name': 'codex_task', 'label': 'codex_task', 'loadMode': 'essential',
        'description': 'Read the synthetic completed task report.',
        'parameters': {'type': 'object', 'properties': {'action': {'type': 'string', 'enum': ['status']}},
                       'required': ['action']}}
    prompts = [
        '코드나 파일은 수정하지 마. codex_task status로 보고서를 확인하고 revision과 전체 건수를 짧게 알려줘.',
        '정정: revision은 rev-B29야. 보고서 수치는 그대로야. 정정된 revision과 전체 건수만 답해.',
        '현재 revision, 전체 건수, 이 작업에서 파일 수정을 허용했는지만 짧게 답해. 추가 도구는 필요 없어.',
    ]
    rows = []
    before = dict(line.split() for line in Path('/proc/vmstat').read_text().splitlines())
    try:
        for variant in ('baseline', 'candidate'):
            idle(); rpc.sandbox_argv = candidate_argv if variant == 'candidate' else original_argv
            session = rpc.OmpSession('context-model', 'sc117-flash-next-iq3_xxs', [definition], host, cfg)
            try:
                await session.start()
                for n, prompt in enumerate(prompts):
                    idle(); start = len(REQUESTS); began = time.monotonic()
                    answer = await asyncio.wait_for(session.prompt(prompt, context.Turn('990826359704154225')), 240)
                    expected = 'rev-A17' if n == 0 else 'rev-B29'
                    assert expected in answer and '23' in answer, answer
                    if n == 2: assert any(word in answer for word in ('불가', '아니', '금지', '없', '허용하지', '읽기')), answer
                    rows.append({'variant': variant, 'turn': n, 'answer': answer,
                        'seconds': round(time.monotonic()-began, 3), 'calls': len(REQUESTS)-start,
                        'requests': [{k:v for k,v in r.items() if k not in ('request', 'started')} for r in REQUESTS[start:]],
                        'old_reasoning_bytes': [sum(len(m.get('reasoning_content', '').encode())
                            for m in r['request']['messages'][:max(i for i,m in enumerate(r['request']['messages']) if m['role']=='user')]
                            ) for r in REQUESTS[start:]]})
            finally: await session.close()
        after = dict(line.split() for line in Path('/proc/vmstat').read_text().splitlines())
        result = {'passed': True, 'scope': 'Two private synthetic three-turn Flash sessions; not broad quality or speed evidence',
            'discord_posts': 0, 'model_switches': 0, 'rows': rows,
            'swapout_delta_pages': int(after['pswpout'])-int(before['pswpout']),
            'oom_kill_delta': int(after['oom_kill'])-int(before['oom_kill'])}
        (report/'context-model.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps(result, ensure_ascii=False), flush=True)
    finally:
        rpc.sandbox_argv = original_argv; server.shutdown(); shutil.rmtree(cfg)


if __name__ == '__main__': asyncio.run(main())
