"""Installed Pi + bwrap contracts; synthetic API, isolated files/store, no Discord posts."""
import asyncio
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading
from unittest.mock import patch

from evo_bot import context, rpc, tools, remote_file_worker
from evo_bot.integrations import coding, codex, host, ssh
import verify_context_payload as fixture


async def main():
    stage, output = map(Path, sys.argv[1:3])
    rpc.RUNTIME = stage/'contract-profiles'; rpc.RUNTIME.mkdir(exist_ok=True)
    rpc.WORKSPACE = stage/'workspace'; rpc.WORKSPACE.mkdir(exist_ok=True)
    tools.STATE = stage/'contract-jobs'; tools.STATE.mkdir(exist_ok=True)
    tools.save_jobs([])
    server = fixture.ThreadingHTTPServer(('127.0.0.1',0),fixture.Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    registry=tools.Registry(); await registry.initialize_web()
    original=rpc.sandbox_argv
    def argv(*args,**kwargs):
        result=original(*args,**kwargs)
        for i in range(len(result)-2):
            if result[i]=='--ro-bind' and result[i+1] in (str(rpc.ROOT/'pi-runner.mjs'),str(rpc.ROOT/'pi-tools.mjs')):
                result[i+1]=str(stage/Path(result[i+1]).name)
        return result
    rpc.sandbox_argv=argv
    notices, events, checks = [], [], []
    session=None
    with tempfile.TemporaryDirectory(prefix='contract-config-',dir=stage) as directory:
        cfg=Path(directory); source=(rpc.ROOT/'models.yml').read_text(); assert ':8080/v1' in source
        (cfg/'models.yml').write_text(source.replace(':8080/v1',f':{server.server_port}/v1'))
        shutil.copyfile(rpc.ROOT/'config.yml',cfg/'config.yml')
        turn=context.Turn('77')
        path=str(rpc.WORKSPACE/'contract-file.txt')
        with patch.object(ssh,'configuration',return_value={'servers':{'contract':{}}}), \
             patch.dict(codex.SERVERS,{'desktop':object()},clear=True), \
             patch.object(coding.remote_files,'exchange',side_effect=lambda _,req,__:remote_file_worker.operate(req)), \
             patch.object(codex,'windows_exec',return_value={'exit_code':0,'stdout':'WINDOWS_OK'}) as windows, \
             patch.object(host,'host_command',side_effect=[
                 {'success':True,'exit_code':0,'output':'HOST_OK'},
                 {'success':False,'exit_code':7,'output':'HOST_EXIT_OUTPUT'},
                 {'success':False,'exit_code':-15,'outcome':'timeout','output':'HOST_TIMEOUT_OUTPUT'},
             ]) as localhost:
            try:
                session=rpc.PiSession('contracts', 'sc117-flash-next-iq3_xxs', registry.definitions(), registry.call,cfg)
                original_host_call=session.host_call
                async def capture(event, owner):
                    events.append({'adapter':event['toolName'],'displayName':event.get('displayName')})
                    await original_host_call(event,owner)
                session.host_call=capture
                await session.start(); session.on_progress=notices.append
                fixture.REQUESTS.clear(); fixture.STEPS.clear()
                fixture.STEPS.extend([
                    ('exec',{'command':'printf SANDBOX_OK'}),
                    ('remote_control',{'action':'help','protocol':'codex','instance':'desktop'}),
                    ('remote_control',{'action':'help','args':{'protocol':'codex','instance':'desktop'}}),
                    ('wait',{'seconds':1}), ('tool_search',{'query':'schedule'}),
                    ('schedule',{'action':'defer','after_seconds':600,'prompt':'Synthetic next check only'}),
                    ('write',{'instance':'contract','path':path,'content':'value = 41\n'}),
                    ('edit',{'instance':'contract','path':path,'edits':[{'oldText':'41','newText':'42'}]}),
                    ('read',{'instance':'contract','path':path}),
                    ('read',{'path':'C:/not-an-EVO-file'}),
                    ('write',{'instance':'instance_placeholder','path':'relative','content':'no write'}),
                    ('exec',{'instance':'desktop','command':'Write-Output WINDOWS_OK','timeout':180}),
                    ('exec',{'instance':'desktop','command':'Write-Output WINDOWS_OK','timeout':600}),
                    ('exec',{'instance':'localhost','command':'printf HOST_OK'}),
                    ('exec',{'instance':'localhost','command':'exit 7'}),
                    ('exec',{'instance':'localhost','command':'sleep 1','timeout':.1}),
                    ('exec',{'command':'sleep 1','timeout':.1}),
                ])
                assert await asyncio.wait_for(session.prompt('Synthetic contract probe; no external actions.',turn),60)=='VERIFIED_CONTEXT'
                text=str([m.get('content') for m in fixture.REQUESTS[-1]['messages'] if m.get('role')=='tool'])
                for marker in ('SANDBOX_OK','value = 42','WINDOWS_OK','HOST_OK','HOST_EXIT_OUTPUT','code 7','HOST_TIMEOUT_OUTPUT',
                               'Remote command timed out','waited_seconds','Windows paths require instance','Unknown instance'):
                    assert marker in text, marker
                    checks.append(marker)
                assert 'remaining_wait_seconds' not in text and 'Use tool_search' not in text
                assert windows.call_count==2 and all(c.args[0]['command'][0]=='powershell.exe' for c in windows.call_args_list)
                assert [c.args[0]['timeout_ms'] for c in windows.call_args_list]==[180000,600000]
                assert [c.args[0]['timeout'] for c in localhost.call_args_list]==[600,600,.1]
                assert len(tools.load_jobs())==1 and tools.load_jobs()[0]['continuation']
                assert {'adapter':'_pi_io','displayName':'edit'} in events
                assert {'adapter':'_pi_exec','displayName':'exec'} in events
                assert any(n.startswith('⚠️ write · 서버 ID') for n in notices),notices
                assert not any('_pi_' in n for n in notices),notices
                schemas={t['function']['name']:t['function']['parameters'] for t in fixture.REQUESTS[0]['tools']}
                assert schemas['exec']['properties']['timeout']['maximum']==600
                assert schemas['exec']['properties']['timeout']['default']==600
                assert set(schemas['exec']['properties'])=={'command','instance','timeout','root','reason'}
                assert 'exec' in schemas and not schemas.keys() & {'bash','powershell'}
                state=await session.request('get_state')
                all_names={t['name'] for t in state['dumpTools']}
                assert 'exec' in all_names and not all_names & {'bash','powershell'},all_names
                assert 'Remote operation failed' not in text
                checks.extend(['single exec; no old tools in discovery','native shell selected once','timeouts survive native Pi','default timeout 600; explicit short timeout retained',
                               'host nonzero output preserved; timeout distinct; no replay','sandbox short timeout cancels once','wait and optional callback discovery',
                               'edit keeps its public name','public notices hide private adapters','single exec schema matches execution'])
                report={'passed':len(checks),'checks':checks,'actual_pi_bwrap_host_path':True,'provider_requests':len(fixture.REQUESTS),
                        'evo_model_requests':0,'target_commands_executed':0,'discord_posts':0,'notices':notices}
                output.write_text(json.dumps(report,ensure_ascii=False,indent=2)); print(json.dumps(report,ensure_ascii=False))
            finally:
                if session: await session.close()
                rpc.sandbox_argv=original
                server.shutdown(); server.server_close()


if __name__=='__main__':asyncio.run(main())
