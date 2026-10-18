"""Exercise threshold-driven native shake with recoverable tmpfs artifacts."""
import asyncio
import json
from pathlib import Path
import re
import shutil
import tempfile
import threading

from evo_omp import context
from evo_omp.rpc import OmpSession, WORKSPACE
from verify_native_tools import Handler, REQUESTS, STEPS, ThreadingHTTPServer, text_of

TOOL={'name':'fixture_output','label':'fixture_output','description':'Return a local verification fixture.',
      'loadMode':'essential','parameters':{'type':'object','properties':{'index':{'type':'integer'}},'required':['index']}}


async def main():
    run=Path(__file__).resolve().parent
    cfg=Path(tempfile.mkdtemp(prefix='compact-config-',dir='/run/evo-omp'))
    (cfg/'models.yml').write_text((run/'models.yml').read_text().replace(':8080/v1',':18089/v1'))
    config = (run/'config.yml').read_text().replace('methodOrder: [soft]', 'methodOrder: [shake, soft]')
    (cfg/'config.yml').write_text(config.replace('methodOrder: [shake, soft]',
        'methodOrder: [shake, soft]\n  thresholdTokens: 42000'))
    server=ThreadingHTTPServer(('127.0.0.1',18089),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    async def host(name,args,turn):
        index=args['index']
        # Heavy recoverable old tool output, not an active user instruction.
        return '\n'.join('FRAME_%d_ROW_%04d %s'%(index,i,'payload_%04d '%i*5) for i in range(1000))
    events=[]
    class Observed(OmpSession):
        async def dispatch(self,event):
            if event.get('type') in ('auto_compaction_start','auto_compaction_end'):
                events.append(event)
            await super().dispatch(event)
    session=Observed('compact-verify','sc117-flash-next-iq3_xxs',[TOOL],host,cfg)
    try:
        await session.start()
        STEPS.extend(('fixture_output',{'index':i}) for i in range(20))
        answer=await session.prompt('ACTIVE_GOAL_KEEP: inspect fixture outputs; preserve this request and its correction.',context.Turn('compact-verify'))
        assert answer=='VERIFIED_NATIVE', (answer,session.error)
        shakes=[e for e in events if e.get('action')=='shake' and e.get('type')=='auto_compaction_end']
        assert shakes, ('No threshold shake',events,len(REQUESTS))
        # A soft summary would introduce another unscripted model dispatch and
        # destroy the matching fixture sequence; inspect the actual calls too.
        assert not any(e.get('action') in ('context-full','soft') for e in events),events
        messages=REQUESTS[-1]['messages']
        user_text='\n'.join(text_of(m) for m in messages if m['role']=='user')
        assert 'ACTIVE_GOAL_KEEP' in user_text,user_text[:500]
        tool_text='\n'.join(text_of(m) for m in messages if m['role']=='tool')
        refs=re.findall(r'artifact://[^\s)\]`<>]+',tool_text)
        assert refs,tool_text[:1000]
        assert 'FRAME_19_ROW_0999' in tool_text,'Recent tool tail was removed'
        calls={t['id'] for m in messages for t in m.get('tool_calls',[])}
        results={m['tool_call_id'] for m in messages if m['role']=='tool'}
        assert calls==results,(calls,results)
        ref=refs[0].rstrip('.,')
        STEPS.append(('read',{'path':ref+':1-20'}))
        await session.prompt('Reopen the archived fixture header.',context.Turn('compact-verify'))
        recovered=text_of(next(m for m in reversed(REQUESTS[-1]['messages']) if m['role']=='tool'))
        assert 'FRAME_' in recovered,recovered
        captures=[p for p in session.profile.rglob('*') if p.is_file() and 'shake' in p.name]
        assert captures,'No recovery artifact on tmpfs'
        result={'shake_events':shakes,'provider_calls':len(REQUESTS),
            'request_bytes':[len(json.dumps(r['messages'])) for r in REQUESTS], 'active_request_preserved':True,
            'tool_pairs_preserved':True,'recent_tail_preserved':True,'recovery_read':True,
            'artifacts':[str(p.relative_to(session.profile)) for p in captures]}
        (run/'compaction-results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
        print('PASS automatic threshold shake, active/recent preservation, paired calls, tmpfs recovery',flush=True)
    finally:
        profile=session.profile
        await session.close()
        assert not profile.exists()
        shutil.rmtree(cfg)
        server.shutdown()


if __name__=='__main__':
    asyncio.run(main())
