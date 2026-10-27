"""Capture real private OMP requests; no model inference or Discord messages.

EVO bot user: verify_tool_schema.py REPORT_DIR FULL_PROVIDER CANDIDATE_PROVIDER LIVE_PROVIDER
verify_context_payload.py must be alongside this script.
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


def structure(value):
    if isinstance(value, dict):
        return {k: structure(v) for k, v in value.items() if k != 'description'}
    if isinstance(value, list):
        return [structure(v) for v in value]
    return value


async def main():
    report = Path(sys.argv[1]); report.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    cfg = Path(tempfile.mkdtemp(prefix='schema-audit-', dir=rpc.RUNTIME))
    (cfg / 'models.yml').write_text((rpc.ROOT / 'models.yml').read_text().replace(
        ':8080/v1', f':{server.server_port}/v1'))
    shutil.copyfile(rpc.ROOT / 'config.yml', cfg / 'config.yml')
    registry = Registry(); await registry.initialize_web()
    original = rpc.sandbox_argv
    captured, checks = {}, {}
    async def host(*_):
        raise AssertionError('No host tool may run in this check')
    try:
        for name, provider in zip(('full', 'candidate', 'previous'), sys.argv[2:]):
            def argv(profile, model):
                args = original(profile, model)
                args[args.index(str(rpc.ROOT / 'provider.ts'))] = provider
                return args
            rpc.sandbox_argv = argv
            REQUESTS.clear(); STEPS.clear()
            session = rpc.OmpSession('schema-audit', 'sc117-flash-next-iq3_xxs',
                                     registry.definitions(), host, cfg)
            try:
                await session.start()
                if name == 'candidate':
                    STEPS.append(('read', {'path': '/home/agent/tool-help/codex_task.json:raw',
                                          'i': 'Read the current full Codex tool specification'}))
                answer = await asyncio.wait_for(session.prompt('Private tool-schema check.',
                                context.Turn('990826359704154225')), 45)
                assert answer == 'VERIFIED_CONTEXT'
                captured[name] = REQUESTS[0]['tools']
                if name == 'candidate':
                    full = {t['function']['name']: t['function'] for t in captured['full']}
                    for tool in full:
                        saved = json.loads((session.profile / 'tool-help' / (tool + '.json')).read_text())
                        assert saved == full[tool], tool
                    result = next(m for m in REQUESTS[-1]['messages'] if m['role'] == 'tool')
                    assert 'file_list' in str(result['content']) and 'request_id' in str(result['content'])
                    checks['full_help_matches_current_request'] = True
                    checks['native_read_help_passed'] = True
            finally:
                await session.close()
        if 'candidate' in captured:
            assert structure(captured['full']) == structure(captured['candidate'])
            checks['all_schema_constraints_preserved'] = True
            checks['tool_count'] = len(captured['candidate'])
            assert checks['tool_count'] == 19
            checks['user_input_present'] = any(t['function']['name'] == 'user_input' for t in captured['candidate'])
        (report / 'schemas.json').write_text(json.dumps(captured, ensure_ascii=False, indent=2))
        result = {'passed': True, 'model_inference': False, 'discord_posts': 0,
                  'real_omp_bwrap': True, **checks}
        (report / 'checks.json').write_text(json.dumps(result, indent=2))
        print(json.dumps(result), flush=True)
    finally:
        rpc.sandbox_argv = original
        server.shutdown(); shutil.rmtree(cfg)


if __name__ == '__main__': asyncio.run(main())
