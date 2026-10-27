"""Check tool inventory and native search on the real private OMP/bwrap path.

Usage on EVO as bot user: verify_tool_inventory.py REPORT_DIR [CANDIDATE_BINARY]
Requires verify_context_payload.py alongside. Scripted local provider; no LLM,
Discord posts, external requests or changes to the shared workspace.
"""
import asyncio
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading

sys.path.insert(0, '/opt/evo-omp')
from evo_omp import context, rpc
from evo_omp.tools import Registry
from verify_context_payload import Handler, ThreadingHTTPServer, REQUESTS, STEPS


async def main():
    report = Path(sys.argv[1]); report.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    cfg = Path(tempfile.mkdtemp(prefix='inventory-audit-', dir=rpc.RUNTIME))
    (cfg / 'models.yml').write_text((rpc.ROOT / 'models.yml').read_text().replace(
        ':8080/v1', f':{server.server_port}/v1'))
    shutil.copyfile(rpc.ROOT / 'config.yml', cfg / 'config.yml')
    registry = Registry(); await registry.initialize_web()
    original_argv = rpc.sandbox_argv
    if len(sys.argv) > 2:
        def candidate_argv(profile, model):
            args = original_argv(profile, model)
            binary = str(rpc.ROOT / 'bin/omp-v18.11.3')
            at = args.index(binary)
            return args[:at] + ['--ro-bind', sys.argv[2], binary] + args[at:]
        rpc.sandbox_argv = candidate_argv
    async def host(*_):
        raise AssertionError('Only native fixture tools may run')
    session = rpc.OmpSession('inventory-audit', 'sc117-flash-next-iq3_xxs',
                             registry.definitions(), host, cfg)
    try:
        await session.start()
        fixture = session.profile / 'sessions/fixture.txt'
        lines = [f'RECORD_{i:04d}: ordinary synthetic line' for i in range(1000)]
        lines[720] = 'MIDDLE_EVIDENCE: revision=rev-B29; passed=17; failed=6'
        fixture.write_text('\n'.join(lines))
        path = '/home/agent/sessions/fixture.txt'
        STEPS.extend([('grep', {'pattern': 'MIDDLE_EVIDENCE', 'path': path, 'i': 'Find fixture evidence'}),
                      ('read', {'path': path + ':721-722', 'i': 'Read fixture evidence'})])
        answer = await asyncio.wait_for(session.prompt('Search the private synthetic fixture only.',
                            context.Turn('990826359704154225')), 90)
        assert answer == 'VERIFIED_CONTEXT', answer
        tools = {t['function']['name']: t['function'] for t in REQUESTS[0]['tools']}
        assert len(tools) == 19 and 'task' not in tools, sorted(tools)
        assert 'Multi-round search MUST use Task' not in tools['grep']['description']
        results = [m for m in REQUESTS[-1]['messages'] if m['role'] == 'tool']
        assert len(results) == 2 and all('rev-B29' in str(m['content']) for m in results)
        calls = [c['id'] for m in REQUESTS[-1]['messages'] for c in m.get('tool_calls', [])]
        assert calls == [m['tool_call_id'] for m in results]
        result = {'passed': True, 'real_omp_bwrap': True, 'model_inference': False,
                  'tool_count': len(tools), 'missing_task_guidance_removed': True,
                  'native_grep_and_ranged_read_passed': True, 'tool_pairs_intact': True,
                  'discord_posts': 0, 'live_bot_restarted': False}
        (report / 'inventory.json').write_text(json.dumps(result, indent=2))
        (report / 'synthetic-payloads.json').write_text(json.dumps({'audit': REQUESTS}, ensure_ascii=False))
        print(json.dumps(result), flush=True)
    finally:
        await session.close()
        rpc.sandbox_argv = original_argv
        server.shutdown(); shutil.rmtree(cfg)


if __name__ == '__main__': asyncio.run(main())
