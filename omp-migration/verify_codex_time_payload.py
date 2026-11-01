"""Observe timestamp fields in real OMP provider requests; scripted HTTP, no LLM or Discord posts."""
import asyncio
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading

from evo_omp import context, rpc
from evo_omp.tools import Registry
from verify_context_payload import Handler, ThreadingHTTPServer, REQUESTS, STEPS


async def main():
    report = Path(sys.argv[1]); report.mkdir(mode=0o700, parents=True, exist_ok=True)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    config = Path(tempfile.mkdtemp(prefix='timestamp-check-', dir=rpc.RUNTIME))
    (config/'models.yml').write_text((rpc.ROOT/'models.yml').read_text().replace(
        ':8080/v1', f':{server.server_port}/v1'))
    shutil.copyfile(rpc.ROOT/'config.yml', config/'config.yml')
    registry = Registry(); await registry.initialize_web()
    fixture = {'threads': [{'updated_at': 1700000001, 'turn_started_at': 1700000000}],
               'updated_at': 1700000001, 'turn_started_at': 1700000000}
    async def host(name, args, turn):
        assert name == 'remote_control' and args == {'protocol': 'codex', 'action': 'list'}
        return fixture
    session = rpc.OmpSession('timestamp-payload', 'sc117-flash-next-iq3_xxs', registry.definitions(), host, config)
    try:
        REQUESTS.clear(); STEPS.clear()
        STEPS.append(('remote_control', {'protocol': 'codex', 'action': 'list'}))
        await session.start()
        assert await asyncio.wait_for(session.prompt('Inspect the private synthetic timestamp fixture.',
            context.Turn('990826359704154225')), 60) == 'VERIFIED_CONTEXT'
        messages = [m for m in REQUESTS[-1]['messages'] if m['role'] == 'tool']
        assert len(messages) == 1
        content = messages[0]['content']
        text = content if isinstance(content, str) else '\n'.join(
            block.get('text', '') for block in content if isinstance(block, dict))
        # Native host results may be wrapped; inspect the actual provider text.
        assert '"updated_at": 1700000001' in text or '"updated_at":1700000001' in text
        assert '"turn_started_at": 1700000000' in text or '"turn_started_at":1700000000' in text
        assert 'middle omitted' not in text
        result = dict(passed=True, real_omp_bwrap_provider_path=True, model_inference=False,
            discord_posts=0, updated_at_reaches_model=True, turn_started_at_reaches_model=True,
            timestamp_fields_removed_or_summarized=False, provider_tool_result=text,
            payload_fixture_only=True)
        (report/'timestamp-payload.json').write_text(json.dumps(result, indent=2)+'\n')
        print(json.dumps(result), flush=True)
    finally:
        await session.close()
        server.shutdown(); server.server_close(); shutil.rmtree(config)
        await registry.close()


if __name__ == '__main__': asyncio.run(main())
