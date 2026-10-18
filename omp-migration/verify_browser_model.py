"""Real OMP/model -> registered browser -> Gemini, with isolated RAM profile."""
import asyncio
import contextlib
import json
import re
import threading
import time
from evo_omp import context
from evo_omp.rpc import OmpSession
from evo_omp.tools import Registry
import verify_browser_live as live


async def main():
    result = live.RESULT
    result['path'] = 'OmpSession.prompt -> real EVO model -> host_tool_call -> Registry.call -> browser -> Gemini'
    result['discord_transport'] = 'Not exercised: same OMP/model/tool path; no fabricated Discord inbound message.'
    sampler = threading.Thread(target=live.sample,daemon=True);sampler.start()
    reg = Registry(); session = None
    async def handler(name,args,turn):
        start = time.monotonic()
        entry = {'tool':name,'action':args.get('action')}
        entry['arguments'] = {k:v for k,v in args.items() if k in
            ('action','url','selector','key','query','role','state','timeout_ms')}
        # Retain page-JS provenance, never input values or page/account contents.
        if args.get('action')=='evaluate': entry['script'] = args.get('script','')[:1500]
        try:
            value = await reg.call(name,args,turn)
            entry.update(success=True,has_expected_answer=bool(re.search(r'585[,\s]*987',str(value))))
            return value
        except Exception as exc:
            entry.update(success=False,error=type(exc).__name__+': '+context.redact_sensitive_text(str(exc))[:200])
            raise
        finally:
            entry['seconds'] = round(time.monotonic()-start,3)
            result['calls'].append(entry)
    try:
        session = OmpSession('browser-migration-check','sc117-flash-next-iq3_xxs',
            [reg.tools['browser'][0]],handler)
        await session.start()
        prompt = ('Browser integration check. Use only the browser tool, not bash or HTTP tools. '
            'Open https://gemini.google.com/app. Find its message input from the current page controls. '
            'Ask Gemini: What is 271828 + 314159? Reply only with the integer. '
            'Submit, then read the page until Gemini replies. Your final answer must quote the integer '
            'actually returned by Gemini. Do not calculate it yourself. Stop after obtaining that answer. '
            'If the browser is blocked, report the actual error; do not retry indefinitely.')
        turn = context.Turn('990826359704154225')
        answer = await asyncio.wait_for(session.prompt(prompt,turn),240)
        result.update(answer=context.redact_sensitive_text(answer)[:1500],rounds=session.rounds,
            model_tool_count=session.tools,session_error=session.error)
        token = context.current.set(turn)
        try:
            for _ in range(11):
                visible = await reg.call('browser',{'action':'evaluate','script':
                    "() => {const texts=[...document.querySelectorAll('model-response')].map(e=>e.innerText);return {answerVisible:texts.some(t=>/585[,\\s]*987/.test(t)),url:location.origin,responseCount:texts.length,responseExcerpt:texts.find(t=>/585[,\\s]*987/.test(t))?.slice(-600)||''}}"},turn)
                if '"answerVisible": true' in str(visible): break
                await asyncio.sleep(2)
            result['independent_page_check'] = visible
        finally: context.current.reset(token)
        result['passed'] = bool('585987' in answer and '"answerVisible": true' in str(visible)
            and any(c.get('has_expected_answer') for c in result['calls']))
    except Exception as exc:
        result['error'] = type(exc).__name__+': '+context.redact_sensitive_text(str(exc))[:300]
        if session: await session.abort(reason='browser test ended')
    finally:
        if session:
            with contextlib.suppress(Exception): await session.close()
        await reg.close()
        await asyncio.sleep(1);live.STOP.set();sampler.join(3)
        result['ended'] = time.time()
        if result['samples']:
            ss = result['samples']
            result['peak_pss_mib'] = round(max(s['pss_bytes'] for s in ss)/2**20,2)
            result['swapout_delta_pages'] = max(s['pswpout'] for s in ss)-min(s['pswpout'] for s in ss)
            result['max_cgroup_swap_bytes'] = max(s['swap_bytes'] for s in ss)
        (live.OUT/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
        print(json.dumps({k:v for k,v in result.items() if k!='samples'},ensure_ascii=False),flush=True)


if __name__=='__main__': asyncio.run(main())
