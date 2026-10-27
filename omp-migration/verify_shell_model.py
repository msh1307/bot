"""Private Flash check of omitted shell-output recovery; no Discord posts."""
import asyncio
import contextlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import time
import urllib.request

sys.path.insert(0, '/opt/evo-omp')
from evo_omp import context, rpc


async def main():
    binary, report = map(Path, sys.argv[1:3])
    with urllib.request.urlopen('http://127.0.0.1:8080/evo/status', timeout=5) as r:
        state = json.load(r)
    status = json.loads((rpc.RUNTIME / 'status.json').read_text())
    if (state.get('busy') or state.get('loading') or status.get('active')
            or status.get('queued') or state.get('active') != 'sc117-flash-next-iq3_xxs'):
        raise RuntimeError('Requires idle bot and already loaded Flash')
    original_argv = rpc.sandbox_argv
    def argv(profile, model):
        args = original_argv(profile, model)
        target = str(rpc.ROOT / 'bin/omp-v18.11.3')
        at = args.index(target)
        return args[:at] + ['--ro-bind', str(binary), target] + args[at:]
    rpc.sandbox_argv = argv
    fixture = Path(tempfile.mkdtemp(prefix='.omp-shell-model-', dir=rpc.WORKSPACE))
    raw = ''.join(f'SECTION_{i:04d}: revision=rev-{i:04d}; result=pass\n' for i in range(1600))
    raw = raw.replace('SECTION_0917: revision=rev-0917; result=pass',
                      'SECTION_0917: revision=rev-cobalt-29; result=failed; error=missing interface header')
    (fixture / 'report.txt').write_text(raw)
    events = []
    class Session(rpc.OmpSession):
        async def dispatch(self, event):
            if event.get('type') in ('tool_execution_start', 'tool_execution_end'):
                events.append(event)
            await super().dispatch(event)
    session = Session('shell-model-check', state['active'], [], None)
    result = {'real_model': state['active'], 'synthetic_report': True, 'discord_posts': 0}
    started = time.monotonic()
    try:
        await session.start()
        answer = await asyncio.wait_for(session.prompt(
            f'First run bash command `cat /workspace/{fixture.name}/report.txt` to inspect this report. '
            'Then find SECTION_0917 and report its exact revision, result, and error. '
            'If the output is abbreviated, recover the missing section from the saved artifact. '
            'Do not modify files. Keep the final answer short.', context.Turn('990826359704154225')), 240)
        calls = [{'tool': e.get('toolName'), 'args': e.get('args')} for e in events
                 if e.get('type') == 'tool_execution_start']
        first = next(e for e in events if e.get('type') == 'tool_execution_end')
        body = '\n'.join(b.get('text', '') for b in first.get('result', {}).get('content', []))
        result.update(answer=answer, calls=calls, elapsed_seconds=round(time.monotonic()-started, 3),
                      first_output_bytes=len(body.encode()), rounds=session.rounds)
        result['passed'] = (calls[0]['tool'] == 'bash' and len(calls) >= 2 and 'artifact://' in body
                            and len(body.encode()) < 3000 and all(s in answer for s in (
                                'rev-cobalt-29', 'failed', 'missing interface header')))
        assert result['passed'], result
    finally:
        with contextlib.suppress(Exception): await session.abort(reason='Validation completed')
        await session.close()
        rpc.sandbox_argv = original_argv
        shutil.rmtree(fixture)
        (report / 'shell-model.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__': asyncio.run(main())
