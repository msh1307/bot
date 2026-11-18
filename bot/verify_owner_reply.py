"""Actual Pi provider-payload checks; synthetic owner replies, no Discord or model calls."""
import asyncio
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import yaml

# Import the selected implementation before the legacy fixture adds its live path.
from evo_bot import context, rpc
from evo_bot.tools import Registry
import verify_context_payload as fixture


async def main():
    baseline='--baseline' in sys.argv
    folder=Path(tempfile.mkdtemp(prefix='owner-reply-verification-',dir='/run/evo-bot'))
    rpc.RUNTIME=folder/'runtime';rpc.RUNTIME.mkdir()
    rpc.WORKSPACE=folder/'workspace';rpc.WORKSPACE.mkdir()
    cfg=folder/'config';cfg.mkdir()
    fixture.THOUGHT='SYNTHETIC_REASONING'
    server=fixture.ThreadingHTTPServer(('127.0.0.1',0),fixture.Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    models=(rpc.ROOT/'models.yml').read_text()
    fake=models.replace(':8080/v1',f':{server.server_port}/v1')
    assert fake!=models
    (cfg/'models.yml').write_text(fake)
    settings=yaml.safe_load((rpc.ROOT/'config.yml').read_text())
    settings['compaction']['keepRecentTokens']=100
    (cfg/'config.yml').write_text(yaml.safe_dump(settings))
    registry=Registry();await registry.initialize_web()
    candidate=os.environ.get('EVO_OWNER_REPLY_RUNNER')
    original=rpc.sandbox_argv
    def sandbox(*args,**kwargs):
        argv=original(*args,**kwargs)
        if candidate:argv[argv.index(str(rpc.ROOT/'pi-runner.mjs'))]=candidate
        return argv
    rpc.sandbox_argv=sandbox
    from PIL import Image
    picture=io.BytesIO();Image.new('RGB',(2,2),'blue').save(picture,format='PNG')
    image={'type':'image','mimeType':'image/png','data':base64.b64encode(picture.getvalue()).decode()}
    cases=[('stop',{'status':'answered','answered':True,'answer':'지금은 아무 작업도 하지 말고 질문에만 답해.'})]
    if not baseline:
        cases += [('answer',{'status':'answered','answered':True,'answer':'승인을 완료했어. 원래 작업을 계속해.'}),
                  ('long',{'status':'answered','answered':True,'answer':'OWNER_BEGIN_'+('가나다'*7000)+'_OWNER_END'}),
                  ('image',{'content':[{'type':'text','text':json.dumps({'status':'answered','answered':True,'answer':'첨부 화면을 확인해줘.'},ensure_ascii=False)},image]}),
                  ('timeout',{'status':'timeout','answered':False,'permission_granted':False,'next_action':'stop_affected_task'}),
                  ('skip',{'status':'skipped','answered':False,'permission_granted':False,'next_action':'stop_affected_task'})]
    rows=[]
    try:
        for name,value in cases:
            fixture.REQUESTS.clear();fixture.STEPS.clear()
            calls=[];events=[]
            async def host(tool,args,turn):
                assert tool=='user_input';calls.append(tool);return value
            session=rpc.PiSession('owner-reply-'+name,'sc117-flash-next-iq3_xxs',registry.definitions(),host,cfg)
            dispatch=session.dispatch
            async def capture(event):
                if event.get('type') in ('owner_reply_forwarded','owner_reply_fallback'):
                    events.append({k:event.get(k) for k in ('type','disposition')})
                await dispatch(event)
            session.dispatch=capture
            try:
                await session.start()
                await asyncio.wait_for(session.prompt('SYNTHETIC_HISTORY\n'+'fixture_read_only '*300,
                    context.Turn('990826359704154225')),30)
                fixture.REQUESTS.clear();fixture.STEPS[:]=[('user_input',{'question':'SYNTHETIC_CHECKPOINT'})]
                turn=context.Turn('990826359704154225',task_instructions='STALE_TASK_SNAPSHOT_DO_NOT_INJECT')
                answer=await asyncio.wait_for(session.prompt('SYNTHETIC_INITIAL_REQUEST\n'+'fixture_read_only '*300,turn),30)
                assert answer=='VERIFIED_CONTEXT' and calls==['user_input']
                assert len(fixture.REQUESTS)==2
                messages=fixture.REQUESTS[-1]['messages']
                users=[m for m in messages if m['role']=='user' and 'SYNTHETIC_HISTORY' not in str(m['content'])]
                tools=[m for m in messages if m['role']=='tool']
                assert len(tools)==1
                reply=json.loads(value['content'][0]['text']) if 'content' in value else value
                expected=reply.get('answer','')
                if baseline:
                    assert len(users)==1 and expected in tools[0]['content']
                elif reply['status']=='answered':
                    assert len(users)==2 and expected in str(users[-1]['content']), name
                    assert expected not in tools[0]['content']
                    assert any(e['type']=='owner_reply_forwarded' for e in events)
                    if name=='long':assert 'OWNER_END' in str(users[-1]['content'])
                    if name=='image':
                        assert json.dumps(messages).count('data:image/png;base64,')==1
                        assert json.dumps(users[-1]).count('data:image/png;base64,')==1
                else:
                    assert len(users)==1 and not events
                    assert json.loads(tools[0]['content'])['permission_granted'] is False
                if not baseline:
                    before_compact=len(fixture.REQUESTS)
                    token=context.current.set(turn)
                    try: await asyncio.wait_for(session.compact(),30)
                    finally: context.current.reset(token)
                    assert len(fixture.REQUESTS)>before_compact
                    summary=fixture.REQUESTS[-1]
                    summary_text='\n'.join(m['content'] if isinstance(m['content'],str) else
                        '\n'.join(b.get('text','') for b in m['content']) for m in summary['messages'])
                    retained=await session.request('get_messages')
                    assert 'SYNTHETIC_INITIAL_REQUEST' in summary_text or 'SYNTHETIC_INITIAL_REQUEST' in json.dumps(retained)
                    assert 'STALE_TASK_SNAPSHOT_DO_NOT_INJECT' not in summary_text+json.dumps(retained)
                    if reply['status']=='answered':
                        assert expected in summary_text or json.dumps(expected,ensure_ascii=False)[1:-1] in json.dumps(retained,ensure_ascii=False), name
                rows.append({'case':name,'passed':True,'user_messages':len(users),
                             'tool_calls':len(calls),'owner_text_chars':len(expected),
                             'events':events,'owner_reply_is_user_message':len(users)==2,
                             'compaction_uses_transcript_only':not baseline})
            finally:await session.close()
        report={'passed':True,'baseline':baseline,'cases':rows,'model_inference':False,'discord_posts':0,
                'rpc_source':rpc.__file__,'rpc_sha256':hashlib.sha256(Path(rpc.__file__).read_bytes()).hexdigest(),
                'runner_sha256':hashlib.sha256(Path(candidate or rpc.ROOT/'pi-runner.mjs').read_bytes()).hexdigest()}
        Path(os.environ.get('EVO_OWNER_REPLY_REPORT','/tmp/evo-owner-reply-verification.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print(json.dumps(report,ensure_ascii=False),flush=True)
    finally:
        rpc.sandbox_argv=original;server.shutdown();server.server_close();shutil.rmtree(folder)


if __name__=='__main__':asyncio.run(main())
