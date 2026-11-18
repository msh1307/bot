"""Actual Pi/bwrap parking and restore; local scripted provider, no Discord or target commands."""
import asyncio
from collections import OrderedDict
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import shutil
import sys
import tempfile
import threading
from unittest.mock import patch

from evo_bot import context, gateway, rpc
import verify_context_payload as fixture


async def main():
    stage, output = map(Path, sys.argv[1:3])
    rpc.RUNTIME=stage/'cache-runtime';rpc.RUNTIME.mkdir()
    rpc.WORKSPACE=stage/'cache-workspace';rpc.WORKSPACE.mkdir()
    server=fixture.ThreadingHTTPServer(('127.0.0.1',0),fixture.Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    sessions=[];checks=[]
    async def deny(*args): raise AssertionError('Cache verification cannot execute tools')
    bot=SimpleNamespace(models={},sessions=OrderedDict(),registry=SimpleNamespace(definitions=lambda cron:[],call=deny))
    async def release(session): await gateway.Bot.release_session(bot,session)
    bot.release_session=release
    def check(value,label):
        assert value,label
        checks.append(label)
    def transcript(session):
        path,=list((session.profile/'sessions').glob('*.jsonl'))
        return path
    try:
        with tempfile.TemporaryDirectory(prefix='cache-config-',dir=stage) as folder:
            cfg=Path(folder)
            source=(rpc.ROOT/'models.yml').read_text();assert ':8080/v1' in source
            (cfg/'models.yml').write_text(source.replace(':8080/v1',f':{server.server_port}/v1'))
            shutil.copyfile(rpc.ROOT/'config.yml',cfg/'config.yml')
            def factory(key,model,definitions,host):
                session=rpc.PiSession(key,model,definitions,host,cfg);sessions.append(session);return session
            async def get(key):return await gateway.Bot.session(bot,key)
            async def seed(session,text):
                turn=context.Turn(session.key,model=session.model);token=context.current.set(turn)
                try: await session.prompt(text,turn)
                finally:context.current.reset(token)
            with patch.object(gateway,'PiSession',factory):
                fixture.REQUESTS.clear();fixture.STEPS.clear()
                first=await get('77');await seed(first,'CACHE-A-SENTINEL')
                raw=transcript(first).read_bytes();digest=hashlib.sha256(raw).hexdigest()
                second=await get('78');await seed(second,'CACHE-B-SENTINEL')
                third=await get('79')
                check(len(bot.sessions)==3,'Three logical conversations retained')
                check(first.parked and first.process.returncode is not None,'Oldest idle process exited')
                check(hashlib.sha256(transcript(first).read_bytes()).hexdigest()==digest,'Parked transcript byte-identical')
                check(sum(not s.parked for s in bot.sessions.values())==2,'Only two live Pi processes')
                restored=await get('77')
                check(restored is not first and restored.transport_alive,'Return starts a fresh healthy process')
                check(transcript(restored).read_bytes().startswith(raw),'Return preserves all original transcript bytes')
                messages=(await restored.request('get_messages'))['messages']
                check('CACHE-A-SENTINEL' in json.dumps(messages),'Original conversation loaded')
                check('CACHE-B-SENTINEL' not in json.dumps(messages),'Conversations remain isolated')
                check(not first.profile.exists(),'Old profile reclaimed only after successful restore')
                check(second.parked,'Next oldest idle process parked')
                saved=transcript(second).read_bytes()
                with patch.object(rpc.PiSession,'start',side_effect=RuntimeError('Synthetic startup failure')):
                    try:await get('78')
                    except RuntimeError:pass
                    else:raise AssertionError('Failure probe unexpectedly started')
                check(bot.sessions['78'] is second and transcript(second).read_bytes()==saved,
                      'Failed restore retains the original parked history')
                again=await get('78')
                check('CACHE-B-SENTINEL' in json.dumps((await again.request('get_messages'))['messages']),
                      'Retry restores preserved history without replay')
                again.compacting=True
                try:
                    try:await again.close(preserve_history=True)
                    except RuntimeError:pass
                    else:raise AssertionError('Busy session was parked')
                    check(again.transport_alive,'Busy process is never killed to free a slot')
                finally:again.compacting=False
                check(len(fixture.REQUESTS)==2,'Parking/restore issues no extra inference requests')
                check(sum(not s.parked for s in bot.sessions.values())==2,'Process bound still holds after failure/retry')
            for s in list(bot.sessions.values()):await bot.release_session(s)
            check(not bot.sessions and not list((rpc.RUNTIME/'profiles').glob('*')),'Close/reset reclaims live and parked profiles')
        report={'passed':len(checks),'checks':checks,'scripted_provider_requests':len(fixture.REQUESTS),
                'evo_model_requests':0,'discord_posts':0,'target_commands':0,'history_sha256':digest}
        output.write_text(json.dumps(report,indent=2));print(json.dumps(report),flush=True)
    finally:
        for s in sessions:await s.close()
        server.shutdown();server.server_close()


if __name__=='__main__':asyncio.run(main())
