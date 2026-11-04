"""Disposable native chats only; no user chat edits, Discord posts or server restarts."""
import asyncio
import json
from pathlib import Path
import tempfile
import time
from unittest.mock import AsyncMock

from evo_bot import context
from evo_bot.integrations import codex


async def wait(b, job):
    deadline=time.monotonic()+180
    while time.monotonic()<deadline:
        await b.status(job)
        if job['state'] not in codex.ACTIVE:
            if job['state']!='completed': raise AssertionError((job['state'],job.get('phase')))
            return b.answer(job)
        await asyncio.sleep(1)
    raise TimeoutError('Test turn did not complete; not resubmitted')


async def reconnect(b):
    ws,reader=b.ws,b.reader
    if ws:await ws.close()
    if reader:await asyncio.gather(reader,return_exceptions=True)


async def main():
    report={'instances':{},'discord_posts':0}
    context.current.set(context.Turn('77'))
    with tempfile.TemporaryDirectory(prefix='codex-parity-live-') as temporary:
        codex.STATE=Path(temporary)/'jobs.json'
        _,settings=codex.server_config()
        for ident in ('desktop','oracle'):
            s=settings[ident]
            b=codex.AppServer(s,ident) if s.get('transport')=='ssh' else codex.Bridge(s,ident)
            b.monitor_desktop=lambda _:None
            b.notify_complete=AsyncMock(); b.send=AsyncMock()
            out=report['instances'][ident]={}
            tids=[]
            try:
                args={'action':'start' if ident=='oracle' else '_start',
                      'prompt':'Reply exactly NATIVE-FIRST-OK. Do not use tools.',
                      'model':'gpt-6-luna','mode':'off','wake':False}
                if ident=='desktop':
                    projects=await b.project_catalog()
                    if not projects: raise AssertionError('No persistent Windows project available for the test')
                    args['project']=projects[0]['project_id']
                first=await b.action(args,'77')
                job=b.jobs[first['job_id']];tid=job['thread_id'];tids.append(tid)
                out['thread_id']=tid;out['first']=await wait(b,job)
                assert out['first']=='NATIVE-FIRST-OK',out
                await b.action({'action':'send','thread_id':tid,'prompt':'Reply exactly NATIVE-SECOND-OK. Do not use tools.'},'77')
                out['second']=await wait(b,job);assert out['second']=='NATIVE-SECOND-OK'
                state=await b.native_settings({'action':'settings','thread_id':tid})
                out['continued_settings']=state
                assert state['model']=='gpt-6-luna' and state['daybreak_enabled'] is False,state
                await reconnect(b)
                out['reconnected_settings']=await b.native_settings({'action':'settings','thread_id':tid})
                assert out['reconnected_settings']['model']=='gpt-6-luna'
                assert out['reconnected_settings']['daybreak_enabled'] is False
                out['goal_before']=await b.action({'action':'send','thread_id':tid,'prompt':'/goal show'},'77')
                # A paused goal tests native persistence without starting autonomous work.
                await b.rpc('thread/goal/set',{'threadId':tid,'objective':'Disposable bridge parity test',
                                             'status':'paused','origin':'user'})
                await b.rpc('thread/unsubscribe',{'threadId':tid})
                out['goal_created']=await b.action({'action':'send','thread_id':tid,'prompt':'/goal show'},'77')
                assert out['goal_created']['goal']['status']=='paused'
                out['cold_goal_loaded']=True
                out['goal_cleared']=await b.action({'action':'send','thread_id':tid,'prompt':'/goal clear'},'77')
                assert out['goal_cleared']['goal'] is None
                out['wake_off']=await b.action({'action':'send','thread_id':tid,'prompt':'/wake off'},'77')
                assert out['wake_off']['wake'] is False
                # Explicit same model and Daybreak on must reach turn 1 on both platforms.
                args.pop('model',None)
                args.update(mode='on',prompt='Reply exactly DAYBREAK-FIRST-OK. Do not use tools.')
                first=await b.action(args,'77');job=b.jobs[first['job_id']];tid=job['thread_id'];tids.append(tid)
                out['daybreak_thread_id']=tid
                out['daybreak_first']=await wait(b,job)
                assert out['daybreak_first']=='DAYBREAK-FIRST-OK'
                await b.action({'action':'send','thread_id':tid,'prompt':'Reply exactly DAYBREAK-SECOND-OK. Do not use tools.'},'77')
                out['daybreak_second']=await wait(b,job)
                assert out['daybreak_second']=='DAYBREAK-SECOND-OK'
                await reconnect(b)
                out['daybreak_reconnected']=await b.native_settings({'action':'settings','thread_id':tid})
                assert out['daybreak_reconnected']['daybreak_enabled'] is True
                assert out['daybreak_reconnected']['model']=='gpt-daybreak-blue-latest'
                out['passed']=True
            except Exception as error:
                out['error']=type(error).__name__+': '+str(error)[:600]
            finally:
                for tid in tids:
                    try:
                        state=await b.rpc('thread/read',{'threadId':tid,'includeTurns':False})
                        if state['thread']['status']['type']=='active':
                            # Only the test's own tracked turn may be interrupted.
                            job=next(j for j in b.jobs.values() if j['thread_id']==tid)
                            await b.rpc('turn/interrupt',{'threadId':tid,'turnId':job['turn_id']})
                        await b.rpc('thread/archive',{'threadId':tid})
                    except Exception as error:out.setdefault('cleanup_errors',[]).append(str(error)[:200])
                await b.close()
        report['passed']=all(x.get('passed') for x in report['instances'].values())
        Path('/tmp/evo-codex-parity-live.json').write_text(json.dumps(report,indent=2))
        print(json.dumps(report,indent=2),flush=True)
        if not report['passed']:raise SystemExit(1)


if __name__=='__main__':asyncio.run(main())
