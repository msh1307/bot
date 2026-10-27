"""Inspect actual OMP provider payloads with synthetic reasoning and tool results.

Run on EVO as the bot user; uses a private scripted endpoint, never Discord/LLM.
Only synthetic requests are retained in the report directory (use tmpfs).
"""
import asyncio
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time

sys.path.insert(0, '/opt/evo-omp')
from evo_omp import context, rpc
from evo_omp.tools import Registry

REQUESTS = []
STEPS = []
THOUGHT = 'SYNTHETIC_REASONING_MARKER\n' + '\n'.join(
    f'Fixture reasoning record {i}: {hashlib.sha256(str(i).encode()).hexdigest()}' for i in range(64))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        REQUESTS.append(body)
        step = STEPS.pop(0) if STEPS else None
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Connection', 'close')
        self.end_headers()
        result = {'content': 'VERIFIED_CONTEXT'}
        if step:
            result = {'tool_calls': [{'index': 0, 'id': f'fixture_{len(REQUESTS)}',
                'type': 'function', 'function': {'name': step[0], 'arguments': json.dumps(step[1])}}]}
        for delta, finish in [({'role': 'assistant'}, None),
                              ({'reasoning_content': THOUGHT}, None), (result, None),
                              ({}, 'tool_calls' if step else 'stop')]:
            event = {'id': 'fixture', 'object': 'chat.completion.chunk', 'created': int(time.time()),
                'model': body['model'], 'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}
            if finish:
                # Below the normal maintenance threshold; no compaction model call.
                event['usage'] = {'prompt_tokens': 8000, 'completion_tokens': 1200,
                                  'total_tokens': 9200}
            self.wfile.write(('data: ' + json.dumps(event) + '\n\n').encode())
        self.wfile.write(b'data: [DONE]\n\n')
        self.close_connection = True


async def main():
    report = Path(sys.argv[1]); report.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    cfg = Path(tempfile.mkdtemp(prefix='context-audit-', dir=rpc.RUNTIME))
    (cfg / 'models.yml').write_text((rpc.ROOT / 'models.yml').read_text().replace(
        ':8080/v1', f':{server.server_port}/v1'))
    shutil.copyfile(rpc.ROOT / 'config.yml', cfg / 'config.yml')
    registry = Registry(); await registry.initialize_web()
    original = rpc.compact_result
    summaries, payloads = [], {}
    async def host(name, args, turn):
        return {'success': True, 'exit_code': 0, 'text': ''.join(
            f'{name} ROW_{i:04d}: source evidence and exact revision record.\n' for i in range(240))}
    try:
        variants = ('without_host_previews', 'deployed', 'completed_reasoning_trim') if len(sys.argv) > 2 else ('without_host_previews', 'deployed')
        original_argv = rpc.sandbox_argv
        for variant in variants:
            def candidate_argv(profile, model):
                args = original_argv(profile, model)
                at = args.index(str(rpc.ROOT / 'provider.ts'))
                args[at] = sys.argv[2]
                return args
            rpc.sandbox_argv = candidate_argv if variant == 'completed_reasoning_trim' else original_argv
            rpc.compact_result = (lambda _name, result, _profile: result) if variant == 'without_host_previews' else original
            REQUESTS.clear()
            session = rpc.OmpSession('context-audit', 'sc117-flash-next-iq3_xxs',
                                     registry.definitions(), host, cfg)
            events = []
            try:
                await session.start()
                for i, (name, args) in enumerate((
                        ('host_command', {'command': 'echo synthetic', 'reason': 'Synthetic audit only'}),
                        ('browser', {'action': 'status'}), ('codex_task', {'action': 'list'}))):
                    STEPS.append((name, args))
                    assert await session.prompt(f'FIXTURE_REQUEST_{i}: Inspect evidence only.',
                                                context.Turn('990826359704154225')) == 'VERIFIED_CONTEXT'
                    events.append(len(REQUESTS) - 1)
                payloads[variant] = list(REQUESTS)
                last = REQUESTS[-1]
                calls = [c['id'] for m in last['messages'] for c in m.get('tool_calls', [])]
                results = [m['tool_call_id'] for m in last['messages'] if m['role'] == 'tool']
                assert calls == results
                users = [m for m in last['messages'] if m['role'] == 'user']
                assert all(any(f'FIXTURE_REQUEST_{i}' in str(m) for m in users) for i in range(3))
                rows = []
                for n, req in enumerate(REQUESTS):
                    messages = req['messages']
                    last_user = max(i for i, m in enumerate(messages) if m['role'] == 'user')
                    old = [m for m in messages[:last_user] if m['role'] == 'assistant']
                    current = [m for m in messages[last_user:] if m['role'] == 'assistant']
                    rows.append({'request': n, 'messages': len(messages),
                        'old_reasoning_bytes': sum(len(m.get('reasoning_content', '').encode()) for m in old),
                        'current_reasoning_bytes': sum(len(m.get('reasoning_content', '').encode()) for m in current),
                        'tool_result_bytes': sum(len(json.dumps(m.get('content'), ensure_ascii=False).encode())
                                                 for m in messages if m['role'] == 'tool'),
                        'template_kwargs': req.get('chat_template_kwargs', {})})
                summaries.append({'variant': variant, 'requests': rows, 'tool_count': len(last['tools']),
                    'tool_schema_bytes': len(json.dumps(last['tools'], ensure_ascii=False).encode()),
                    'tool_pairs_intact': True, 'all_user_requests_retained': True})
            finally:
                await session.close()
        (report / 'synthetic-payloads.json').write_text(json.dumps(payloads, ensure_ascii=False))
        result = {'passed': True, 'real_omp_bwrap_provider_path': True, 'model_inference': False,
                  'discord_posts': 0, 'scope': 'Synthetic three-turn payload audit, not quality/latency benchmark',
                  'variants': summaries}
        (report / 'context-payload.json').write_text(json.dumps(result, indent=2))
        print(json.dumps(result), flush=True)
    finally:
        rpc.compact_result = original
        rpc.sandbox_argv = original_argv
        server.shutdown()
        shutil.rmtree(cfg)


if __name__ == '__main__':
    asyncio.run(main())
