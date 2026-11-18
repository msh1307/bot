"""Actual Pi/bwrap/host-tool path with a scripted provider, without Discord posts."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer

from evo_bot import context, gateway, rpc, tools
import verify_context_payload as fixture


async def main():
    real_model = '--model' in sys.argv
    stage, output = map(Path, sys.argv[1:3])
    server = ThreadingHTTPServer(('127.0.0.1', 0), fixture.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    runtime = stage/'profiles'; runtime.mkdir(exist_ok=True)
    rpc.RUNTIME = runtime
    tools.STATE = stage/'goal-state'; tools.STATE.mkdir(exist_ok=True)
    original_argv = rpc.sandbox_argv
    def argv(*args, **kwargs):
        result = original_argv(*args, **kwargs)
        for i in range(len(result)-2):
            if result[i] == '--ro-bind' and result[i+1] in (str(rpc.ROOT/'pi-runner.mjs'), str(rpc.ROOT/'pi-tools.mjs')):
                result[i+1] = str(stage/Path(result[i+1]).name)
        return result
    rpc.sandbox_argv = argv
    registry = tools.Registry()
    registry.tools = {'schedule': (dict(tools.SCHEDULE_SCHEMA, name='schedule'), tools.schedule)}
    registry.tools['fixture_step'] = ({'name': 'fixture_step', 'loadMode': 'essential',
        'description': 'Synthetic new evidence, no action.', 'parameters': {'type': 'object', 'properties': {}}}, None)
    calls, checks, reports = [], [], []
    async def host(name, args, turn):
        calls.append(name)
        if name == 'schedule': reports.append(dict(args))
        if name == 'fixture_step': return 'New synthetic evidence arrived after the previous state report.'
        assert name == 'schedule', 'This probe permits only task bookkeeping'
        return await registry.call(name, args, turn)
    async def scenario(name, steps, expected, *, managed=True, read_only=False,
                       goal='Compute 6*7, verify result=42, then finish. No external actions.'):
        tools.save_jobs([]); fixture.REQUESTS.clear(); fixture.STEPS.clear(); calls.clear(); reports.clear()
        owner = context.Turn('77', model='sc117-flash-next-iq3_xxs')
        token = context.current.set(owner)
        session = None
        try:
            ident = tools.schedule({'action': 'enqueue', 'prompt': goal})['id']
            if read_only:
                jobs=tools.load_jobs();jobs[0]['mode']='check';tools.save_jobs(jobs)
            claim = gateway.Bot.claim_due()[0]
            owner.job_id, owner.task_run = ident, claim['last_run_at'] if managed else ''
            owner.task_deadline = claim['task_deadline']
            owner.cron=read_only
            for step in steps:
                if step and step[0] == 'schedule': step[1].pop('id', None)
                fixture.STEPS.append(step)
            definitions = registry.definitions(read_only)
            for definition in definitions:
                if definition['name'] == 'fixture_step': definition['loadMode'] = 'essential'
            session = rpc.PiSession('goal-probe', owner.model, definitions, host, cfg)
            await session.start()
            error = None
            try:
                text = gateway.task_prompt(claim) if managed else 'Task: '+goal
                await asyncio.wait_for(session.prompt(text, owner), 120 if real_model else 40)
            except RuntimeError as exc:
                error = str(exc)
            gateway.Bot.finish_job(claim, 'error' if error else 'ok')
            saved = tools.load_jobs()[0]
            assert saved['task_state'] == expected, (name, saved, error)
            assert '_pi_exec' not in calls, (name, 'forbidden action reached host')
            checks.append({'scenario': name, 'state': saved['task_state'], 'provider_requests': len(fixture.REQUESTS),
                           'pi_rounds': session.rounds, 'host_tools': list(calls), 'schedule_calls': list(reports),
                           'enabled':saved['enabled'], 'resume_at':saved.get('resume_at'),
                           'error': bool(error), 'passed': True})
            if name == 'missing-report': assert len(fixture.REQUESTS) == 2
            if name == 'repair':
                assert len(fixture.REQUESTS) == 4
                assert any(t['function']['name'] == 'schedule' for t in fixture.REQUESTS[2]['tools'])
            if name == 'ordinary': assert len(fixture.REQUESTS) == 1
            if name in ('read-only-recheck', 'real-read-only-recheck'):
                assert calls == ['schedule'], (name, 'Recheck needs only one schedule call', calls)
                assert reports[0].get('action') == 'note' and reports[0].get('state') == 'waiting', reports
                assert reports[0].get('after_seconds') == 600, reports
                assert saved['enabled'] and saved['next_run_at'] == saved.get('resume_at'), saved
                assert saved['id'] == ident and len(tools.load_jobs()) == 1
                assert saved['task_deadline'] == claim['task_deadline']
        finally:
            if session: await session.close()
            context.current.reset(token)
    try:
        with tempfile.TemporaryDirectory(prefix='goal-config-', dir=stage) as tmp:
            cfg = Path(tmp)
            source = (rpc.ROOT/'models.yml').read_text(); assert ':8080/v1' in source
            (cfg/'models.yml').write_text(source if real_model else source.replace(':8080/v1', f':{server.server_port}/v1'))
            shutil.copyfile(rpc.ROOT/'config.yml', cfg/'config.yml')
            if real_model:
                rpc.WORKSPACE = stage/'probe-workspace'; rpc.WORKSPACE.mkdir(exist_ok=True)
                await scenario('real-flash', [], 'done')
                await scenario('real-read-only-recheck', [], 'waiting', read_only=True,
                    goal='A synthetic job is still running. Check its status again in 600 seconds. No external actions.')
            else:
                await scenario('reported', [('tool_search', {'query': 'schedule'}),
                    ('schedule', {'action': 'note', 'state': 'continue', 'text': 'Verified 42; next check output'})], 'continue')
                await scenario('repair', [None, ('tool_search', {'query': 'schedule'}),
                    ('schedule', {'action': 'note', 'state': 'done', 'text': 'Verified result=42'})], 'done')
                await scenario('forbidden-repair', [None, ('exec', {'command': 'echo forbidden-side-effect'})], 'blocked')
                await scenario('missing-report', [None, None], 'blocked')
                await scenario('stale-report-repaired', [('tool_search', {'query': 'schedule'}),
                    ('schedule', {'action': 'note', 'state': 'done', 'text': 'Earlier result verified'}),
                    ('fixture_step', {}), None,
                    ('schedule', {'action': 'note', 'state': 'waiting', 'text': 'New evidence needs owner input'})], 'waiting')
                await scenario('stale-report-unconfirmed', [('tool_search', {'query': 'schedule'}),
                    ('schedule', {'action': 'note', 'state': 'done', 'text': 'Earlier result verified'}),
                    ('fixture_step', {}), None, None], 'blocked')
                await scenario('ordinary', [None], 'blocked', managed=False)
                await scenario('read-only-recheck', [('tool_search', {'query':'schedule'}),
                    ('schedule', {'action':'note','state':'waiting','after_seconds':600,'text':'Observe the same status again'})], 'waiting', read_only=True)
                await scenario('read-only-finished', [('tool_search', {'query':'schedule'}),
                    ('schedule', {'action':'note','state':'done','text':'Completion verified'})], 'done', read_only=True)
        report = {'passed': len(checks), 'checks': checks, 'actual_pi_bwrap_host_path': True,
                  'evo_model_requests': sum(c['pi_rounds'] for c in checks) if real_model else 0, 'discord_posts': 0}
        output.write_text(json.dumps(report, indent=2)); print(json.dumps(report))
    finally:
        rpc.sandbox_argv = original_argv
        server.shutdown(); server.server_close()


if __name__ == '__main__':
    asyncio.run(main())
