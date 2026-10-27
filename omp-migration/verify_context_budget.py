"""Probe native compaction accounting after provider-side reasoning filtering.

Run as the bot user with a tmpfs report directory. Uses a scripted local endpoint,
synthetic history and real installed OMP/bwrap; no inference or Discord messages.
"""
import asyncio
import hashlib
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
    cfg = Path(tempfile.mkdtemp(prefix='budget-audit-', dir=rpc.RUNTIME))
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
            return args[:at] + ['--ro-bind', sys.argv[2], binary,
                '--setenv', 'EVO_COMPLETED_REASONING_CONTEXT', '1'] + args[at:]
        rpc.sandbox_argv = candidate_argv
    events, turns = [], []
    async def host(_name, _args, _turn):
        return {'success': True, 'answer': 'SYNTHETIC_EVIDENCE: 17 passed, 6 failed; read-only.'}
    class Observed(rpc.OmpSession):
        async def dispatch(self, event):
            if event.get('type') in ('auto_compaction_start', 'auto_compaction_end'):
                events.append(event)
            await super().dispatch(event)
    session = Observed('budget-audit', 'sc117-flash-next-iq3_xxs', registry.definitions(), host, cfg)
    try:
        await session.start()
        for i in range(10):
            STEPS.append(('codex_task', {'action': 'list'}))
            begin = len(REQUESTS)
            answer = await asyncio.wait_for(session.prompt(
                f'FIXTURE_REQUEST_{i}: Inspect evidence only. Do not modify files.',
                context.Turn('990826359704154225')), 90)
            assert answer == 'VERIFIED_CONTEXT', (i, answer)
            turns.append({'turn': i, 'first_request': begin, 'requests': len(REQUESTS)-begin,
                          'compaction_events': len(events)})
            if events:
                break
        normal_events = list(events)
        normal_requests = len(REQUESTS)
        (report / 'normal-audit.json').write_text(json.dumps({'turns': turns, 'events': normal_events}))
        if len(sys.argv) > 2:
            assert not normal_events, 'Candidate still compacts completed reasoning'
        # A real large user input must still trigger maintenance: do not simply
        # ignore the stored-context floor or disable overflow protection.
        evidence = '\n'.join(hashlib.sha256(f'evidence-{i}'.encode()).hexdigest() for i in range(2200))
        answer = await asyncio.wait_for(session.prompt(
            'CURRENT_INTENT_READ_ONLY: inspect this synthetic evidence only.\n' + evidence,
            context.Turn('990826359704154225')), 90)
        assert answer == 'VERIFIED_CONTEXT', answer
        assert len(events) > len(normal_events), 'Large current input did not trigger maintenance'
        normal = [request for request in REQUESTS if request.get('tools')]
        assert any('CURRENT_INTENT_READ_ONLY' in str(m.get('content'))
                   for m in normal[-1]['messages'] if m['role'] == 'user')
        (report / 'synthetic-payloads.json').write_text(json.dumps({'audit': REQUESTS}, ensure_ascii=False))
        result = {'model_inference': False, 'discord_posts': 0, 'turns': turns, 'events': events,
                  'normal_events': normal_events, 'normal_requests': normal_requests,
                  'large_current_input_compacted': True, 'current_intent_preserved': True,
                  'threshold_changed': False, 'live_bot_restarted': False}
        (report / 'budget-audit.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps(result, ensure_ascii=False), flush=True)
    finally:
        (report / 'synthetic-payloads.json').write_text(json.dumps({'audit': REQUESTS}, ensure_ascii=False))
        await session.close(); server.shutdown(); shutil.rmtree(cfg)
        rpc.sandbox_argv = original_argv


if __name__ == '__main__': asyncio.run(main())
