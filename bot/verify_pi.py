"""Real Pi subprocess and bwrap checks; synthetic endpoint, no Discord posts.

As bot user: EVO_HARNESS=pi python verify_pi.py CANDIDATE_ROOT REPORT_DIR [--model]
"""
import asyncio
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import urllib.request

from evo_bot import context, rpc
from evo_bot.tools import Registry
from verify_context_payload import Handler, ThreadingHTTPServer, REQUESTS, STEPS


class FaultHandler(Handler):
    failures = 0
    length_stop = False

    def do_POST(self):
        if type(self).failures:
            type(self).failures -= 1
            REQUESTS.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            body = b'{"error":{"message":"verification rate limit","type":"rate_limit_error"}}'
            self.send_response(429); self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(body))); self.send_header('Retry-After','1')
            self.end_headers(); self.wfile.write(body)
            return
        if type(self).length_stop:
            output = self.wfile
            class LengthOutput:
                def write(self, data): return output.write(data.replace(b'"finish_reason": "stop"', b'"finish_reason": "length"'))
                def flush(self): return output.flush()
            self.wfile = LengthOutput()
            try: return super().do_POST()
            finally: self.wfile = output
        return super().do_POST()


async def main():
    rpc.ROOT = Path(sys.argv[1])
    report = Path(sys.argv[2]); report.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(('127.0.0.1', 0), FaultHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    cfg = Path(tempfile.mkdtemp(prefix='pi-check-', dir=rpc.RUNTIME))
    (cfg/'models.yml').write_text((rpc.ROOT/'models.yml').read_text().replace(
        ':8080/v1', f':{server.server_port}/v1'))
    shutil.copyfile(rpc.ROOT/'config.yml', cfg/'config.yml')
    registry = Registry(); await registry.initialize_web()
    checks, calls, captures = {}, [], {}
    probe = {'name': 'verify_probe', 'description': 'Synthetic check only.',
             'parameters': {'type': 'object', 'properties': {'index': {'type': 'integer'}},
                            'required': ['index']}, 'loadMode': 'essential'}

    async def host(name, args, turn):
        calls.append((name, args))
        if name == 'gmail':
            assert args == {'action': 'status'}
            return {'inbox': 3141, 'unread': 27}
        assert name == 'verify_probe'
        return {'index': args['index']}

    s = None
    try:
        for engine in ('pi',):
            rpc.ENGINE = engine
            REQUESTS.clear(); STEPS.clear()
            s = rpc.PiSession('pi-payload-'+engine, 'sc117-flash-next-iq3_xxs',
                               registry.definitions(), host, cfg)
            try:
                await s.start()
                assert await asyncio.wait_for(s.prompt('Synthetic payload check.', context.Turn('77')), 45) == 'VERIFIED_CONTEXT'
                body = REQUESTS[0]
                captures[engine] = {'tools': [t['function']['name'] for t in body['tools']],
                    'schema_bytes': len(json.dumps(body['tools'], ensure_ascii=False).encode()),
                    'system_bytes': sum(len(str(m['content']).encode()) for m in body['messages'] if m['role'] == 'system')}
                if engine == 'pi':
                    assert body['messages'][0]['role'] == 'system'
                    assert 'answer in concise, plain Korean' in str(body['messages'][0])
                    checks['existing_instructions_and_provider_compat'] = True
            finally:
                await s.close(); s = None
        checks['payloads'] = captures
        checks['pi_native_tools'] = {'read','bash','edit','write','tool_search'} <= set(captures['pi']['tools'])
        checks['deferred_not_in_initial_payload'] = 'gmail' not in captures['pi']['tools']
        assert checks['pi_native_tools'] and checks['deferred_not_in_initial_payload']

        rpc.ENGINE = 'pi'; REQUESTS.clear(); STEPS.clear()
        s = rpc.PiSession('pi-tools', 'sc117-flash-next-iq3_xxs', registry.definitions()+[probe], host, cfg)
        await s.start()
        fixture = '/home/agent/sessions/pi-fixture.txt'
        STEPS.extend([('write', {'path': fixture, 'content': 'value = 41\n'}),
                      ('read', {'path': fixture}),
                      ('edit', {'path': fixture, 'oldText': '41', 'newText': '42'}),
                      ('bash', {'command': 'cat '+fixture}),
                      ('read', {'path': '/home/agent/config/index.json'}),
                      ('read', {'path': '/home/agent/deploy/codex-server/manifest.json'}),
                      ('bash', {'command': 'if touch /home/agent/config/.write-probe 2>/dev/null; then echo CONFIG_WRITABLE; else echo CONFIG_READ_ONLY; fi'}),
                      ('tool_search', {'query': 'gmail', 'limit': 1}),
                      ('gmail', {'action': 'status'})])
        assert await asyncio.wait_for(s.prompt('Synthetic tools check.', context.Turn('77')), 60) == 'VERIFIED_CONTEXT'
        results = [m for m in REQUESTS[-1]['messages'] if m['role'] == 'tool']
        assert 'value = 42' in str(results) and '3141' in str(results)
        assert 'remote-control.json' in str(results) and 'CONFIG_READ_ONLY' in str(results)
        assert 'codex-rpc.py' in str(results) and 'Update-DesktopBridge.ps1' in str(results)
        assert 'CONFIG_WRITABLE' not in str(results)
        assert 'SYNTHETIC_REASONING_MARKER' in str(REQUESTS[-1]['messages'])
        assert calls == [('gmail', {'action': 'status'})], calls
        checks['native_read_write_edit_bash'] = True
        checks['config_index_native_read_and_read_only_mount'] = True
        checks['support_deploy_bundle_readable'] = True
        checks['native_deferred_discovery_and_host_dispatch'] = True
        assert await s.reusable_after_error()
        checks['settled_state'] = True

        calls.clear(); STEPS.extend(('verify_probe', {'index': i}) for i in range(45))
        assert await asyncio.wait_for(s.prompt('Synthetic long tool loop.', context.Turn('77')), 90) == 'VERIFIED_CONTEXT'
        assert len(calls) == 45
        checks['more_than_40_tool_rounds'] = len(calls)
        await s.prompt('Synthetic reasoning retention check.',context.Turn('77'))
        body=REQUESTS[-1]; last_user=max(i for i,m in enumerate(body['messages']) if m['role']=='user')
        assert 'SYNTHETIC_REASONING_MARKER' not in str(body['messages'][:last_user])
        checks['completed_reasoning_omitted_from_next_turn']=True

        calls.clear(); STEPS.append(('not_a_declared_tool', {}))
        assert await asyncio.wait_for(s.prompt('Reject an unknown tool.', context.Turn('77')), 45) == 'VERIFIED_CONTEXT'
        assert not calls
        checks['unknown_tool_not_executed'] = True

        count=len(REQUESTS); FaultHandler.failures=1
        assert await asyncio.wait_for(s.prompt('Retry one transient failure.',context.Turn('77')),45) == 'VERIFIED_CONTEXT'
        assert len(REQUESTS)-count == 2
        checks['transient_429_retry']=True
        count=len(REQUESTS); FaultHandler.failures=9
        try: await asyncio.wait_for(s.prompt('Bound exhausted retries.',context.Turn('77')),45)
        except RuntimeError: pass
        else: raise AssertionError('Persistent provider failure was ignored')
        assert 1 <= len(REQUESTS)-count <= 3, len(REQUESTS)-count
        FaultHandler.failures=0
        assert await s.reusable_after_error()
        checks['retry_bound_and_failed_turn_reuse']=len(REQUESTS)-count

        STEPS.append(('bash', {'command': 'touch /home/agent/sessions/started; sleep 30; touch /home/agent/sessions/late'}))
        task = asyncio.create_task(s.prompt('Synthetic cancellation.', context.Turn('77')))
        for _ in range(100):
            if (s.profile/'sessions/started').exists(): break
            await asyncio.sleep(.05)
        assert (s.profile/'sessions/started').exists()
        await s.abort()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 10)
        assert not (s.profile/'sessions/late').exists()
        assert await s.reusable_after_error()
        checks['native_bash_abort_and_session_reuse'] = True

        assert await s.prompt('Remember PI_HANDOFF_742 and finish.', context.Turn('77')) == 'VERIFIED_CONTEXT'
        saved = cfg/'saved'; shutil.copytree(s.profile/'sessions', saved)
        before = (await s.request('get_messages'))['messages']
        await s.close(); s = None
        s = rpc.PiSession('pi-resume', 'sc117-flash-next-iq3_xxs', registry.definitions()+[probe], host, cfg)
        await s.start(resume_dir=saved)
        after = (await s.request('get_messages'))['messages']
        assert before == after
        checks['pi_restart_preserves_messages_exactly'] = len(after)
        result = await asyncio.wait_for(s.compact(), 45)
        assert result.get('summary','').startswith('VERIFIED_CONTEXT'), result
        assert REQUESTS[-1]['max_tokens'] == 0 and REQUESTS[-1]['final_output_max_tokens'] == 8192
        checks['native_compaction_with_separate_reasoning_budget'] = True
        before = (await s.request('get_messages'))['messages']; FaultHandler.length_stop=True
        try: await asyncio.wait_for(s.compact(),45)
        except RuntimeError: pass
        else: raise AssertionError('Incomplete summary was accepted')
        finally: FaultHandler.length_stop=False
        assert (await s.request('get_messages'))['messages'] == before
        checks['incomplete_summary_preserves_context']=True
        await s.close(); s = None

        if '--model' in sys.argv:
            with urllib.request.urlopen('http://127.0.0.1:8080/evo/status', timeout=5) as r: state=json.load(r)
            status=json.loads((rpc.RUNTIME/'status.json').read_text())
            if state.get('busy') or state.get('loading') or status.get('active') or status.get('queued'):
                raise RuntimeError('Real model check requires idle bot/server')
            s=rpc.PiSession('pi-model', state['active'], registry.definitions(), host)
            await s.start()
            (s.profile/'sessions/pi-number.txt').write_text('74261\n')
            answer=await asyncio.wait_for(s.prompt(
                '/home/agent/sessions/pi-number.txt 파일을 read 도구로 읽고 숫자만 답해.',context.Turn('77')),240)
            assert '74261' in answer and s.tools >= 1, (answer,s.tools)
            checks['real_model']={'model':state['active'],'answer':answer,'tool_calls':s.tools}
        checks['passed']=True
        (report/'checks.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps(checks,ensure_ascii=False),flush=True)
    finally:
        if s: await s.close()
        server.shutdown();server.server_close();shutil.rmtree(cfg)
        await registry.close()


if __name__ == '__main__': asyncio.run(main())
