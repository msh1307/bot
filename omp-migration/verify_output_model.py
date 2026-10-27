"""Private real-model check: recover an omitted middle section of a tool result.

The report is synthetic. No Windows commands, Discord messages or model switch.
"""
import asyncio
import contextlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
import time
import urllib.request

sys.path.insert(0, '/opt/evo-omp')
from evo_omp import context


async def main():
    candidate, report = map(Path, sys.argv[1:3])
    with urllib.request.urlopen('http://127.0.0.1:8080/evo/status', timeout=5) as r:
        state = json.load(r)
    if state.get('busy') or state.get('loading') or state.get('active') != 'sc117-flash-next-iq3_xxs':
        raise RuntimeError('Requires idle, already loaded Flash')
    for name, path in [('evo_omp.tool_output', candidate / 'tool_output.py'),
                       ('evo_omp.rpc_candidate', candidate / 'rpc.py')]:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec); sys.modules[name] = module
        spec.loader.exec_module(module)
    Session = sys.modules['evo_omp.rpc_candidate'].OmpSession
    cfg = Path(tempfile.mkdtemp(prefix='output-model-', dir='/run/evo-omp'))
    shutil.copyfile('/opt/evo-omp/models.yml', cfg / 'models.yml')
    shutil.copyfile(candidate / 'config.yml', cfg / 'config.yml')
    text = ''.join(f'SECTION_{i:04d}: revision=rev-{i:04d}; result=pass\n' for i in range(600))
    text = text.replace('SECTION_0317: revision=rev-0317; result=pass',
                        'SECTION_0317: revision=rev-delta-73; result=failed; error=missing build header')
    calls = []
    async def handler(name, args, turn):
        if name != 'codex_task' or args.get('action') != 'status' or len(calls) >= 3:
            raise ValueError('Read-only fixture status only')
        calls.append({'tool': name, 'args': args})
        return {'state': 'completed', 'thread_id': 'fixture', 'answer': text}
    definition = {'name': 'codex_task', 'label': 'codex_task', 'loadMode': 'essential',
        'description': 'Read the completed fixture task report.',
        'parameters': {'type': 'object', 'properties': {'action': {'type': 'string', 'enum': ['status']}},
                       'required': ['action']}}
    session = Session('output-model-check', state['active'], [definition], handler, cfg)
    result = {'synthetic_report': True, 'real_model': state['active'], 'discord_posts': 0}
    started = time.monotonic()
    try:
        await session.start()
        answer = await asyncio.wait_for(session.prompt(
            '완료된 작업 보고서에서 SECTION_0317의 revision, result, error를 확인해서 짧게 알려줘.',
            context.Turn('990826359704154225')), 240)
        result.update(answer=answer, calls=calls, tools=session.tools, rounds=session.rounds,
                      elapsed_seconds=round(time.monotonic() - started, 3))
        result['passed'] = (bool(calls) and session.tools > len(calls)
                            and all(s in answer for s in ('rev-delta-73', 'failed', 'missing build header')))
        assert result['passed'], result
    finally:
        with contextlib.suppress(Exception): await session.abort(reason='Validation completed')
        await session.close(); shutil.rmtree(cfg)
        (report / 'output-model.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__': asyncio.run(main())
