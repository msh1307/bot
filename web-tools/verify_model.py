"""Private OMP session using the real model and deployed web-tool path.

No Discord messages, host actions, model/config changes or user-session reset.
The input is a public documentation question, not an instruction hidden in tools.
"""
import asyncio
import contextlib
import json
from pathlib import Path
import sys
import time
import urllib.request

sys.path.insert(0, '/opt/evo-omp')
from evo_omp import context
from evo_omp.rpc import OmpSession
from evo_omp.tools import Registry


async def main():
    output = Path(sys.argv[1]); output.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen('http://127.0.0.1:8080/evo/status', timeout=5) as r:
        state = json.load(r)
    if state.get('busy') or state.get('loading') or state.get('active') != 'sc117-flash-next-iq3_xxs':
        raise RuntimeError('Requires idle Flash already loaded; no model switch')
    registry = Registry(); await registry.initialize_web()
    calls, session = [], None
    async def handler(name, args, turn):
        if name not in ('internet_search', 'web_read') or len(calls) >= 10:
            raise RuntimeError('Documentation check only, web-call budget exhausted')
        started = time.monotonic()
        result = await registry.call(name, args, turn)
        calls.append({'tool': name, 'args': args, 'chars': len(result),
                      'bytes': len(result.encode()), 'seconds': round(time.monotonic() - started, 3),
                      'success': json.loads(result).get('success')})
        return result
    started = time.monotonic()
    result = {'calls': calls, 'discord_message_sent': False,
              'path': 'OmpSession -> real Flash -> Registry -> router -> web worker'}
    try:
        session = OmpSession('search-budget-check', 'sc117-flash-next-iq3_xxs',
                             [registry.tools[n][0] for n in ('internet_search', 'web_read')], handler)
        await session.start()
        turn = context.Turn('990826359704154225')
        question = ('https://docs.python.org/3/library/asyncio-task.html 에서 TaskGroup 내부 태스크가 '
                    'KeyboardInterrupt로 실패하면 나머지 태스크는 어떻게 되고 어떤 예외가 다시 발생하는지 '
                    '문서 근거를 찾아 짧게 알려줘.')
        answer = await asyncio.wait_for(session.prompt(question, turn), 240)
        result.update(answer=answer, rounds=session.rounds, tools=session.tools,
                      context=(await session.request('get_state')).get('contextUsage'),
                      elapsed_seconds=round(time.monotonic() - started, 3))
        result['checks'] = {'read_actual_source': any(c['tool'] == 'web_read' for c in calls),
                            'web_replies_bounded': all(c['bytes'] <= 10000 for c in calls),
                            'all_calls_successful': all(c['success'] for c in calls),
                            'names_original_exception': 'KeyboardInterrupt' in answer}
        result['passed'] = all(result['checks'].values())
    except Exception as e:
        result['error'] = type(e).__name__ + ': ' + str(e)[:300]
        if session: await session.abort(reason='Search validation ended')
    finally:
        if session:
            with contextlib.suppress(Exception): await session.close()
        (output / 'model-verification.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__': asyncio.run(main())
