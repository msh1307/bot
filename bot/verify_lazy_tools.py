"""Actual Pi deferred-tool routing; synthetic endpoint/mail, no Discord posts.

Run as the bot user: verify_lazy_tools.py REPORT_DIR CANDIDATE_ROOT [--model]
Candidate root contains evo_bot, provider.ts and config.yml. Uses the current
live provider for the baseline; no live files or schedules are edited.
"""
import asyncio
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import urllib.request

from evo_bot import context, rpc
from evo_bot.tools import Registry
from verify_context_payload import Handler, ThreadingHTTPServer, REQUESTS, STEPS


async def main():
    report, candidate = map(Path, sys.argv[1:3])
    report.mkdir(mode=0o700, parents=True, exist_ok=True)
    registry = Registry(); await registry.initialize_web()
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    config = Path(tempfile.mkdtemp(prefix='lazy-check-', dir=rpc.RUNTIME))
    (config/'models.yml').write_text((rpc.ROOT/'models.yml').read_text().replace(
        ':8080/v1', f':{server.server_port}/v1'))
    shutil.copyfile(candidate/'config.yml', config/'config.yml')
    original = rpc.sandbox_argv
    captures, checks, calls = {}, {}, []
    async def host(name, args, turn):
        assert name == 'gmail' and args == {'action': 'status'}, (name, args)
        assert not turn.cron
        calls.append(name)
        return {'inbox': 3141, 'unread': 27}
    try:
        for variant in ('before', 'after', 'cron'):
            def argv(profile, model):
                args = original(profile, model)
                args[args.index(str(rpc.ROOT/'provider.ts'))] = str(
                    (rpc.ROOT if variant == 'before' else candidate)/'provider.ts')
                return args
            rpc.sandbox_argv = argv
            definitions = registry.definitions(variant == 'cron')
            if variant == 'before':
                definitions = [dict(d, loadMode='essential') for d in definitions]
            REQUESTS.clear(); STEPS.clear(); calls.clear()
            if variant == 'after':
                STEPS.extend([('read', {'path': 'xd://gmail', 'i': 'Read mail tool schema'}),
                    ('write', {'path': 'xd://gmail', 'content': '{"action":"status"}', 'i': 'Read synthetic mailbox counts'}),
                    ('read', {'path': 'xd://host_command', 'i': 'Check optional host-command guidance only'})])
            elif variant == 'cron':
                STEPS.append(('write', {'path': 'xd://shell_exec', 'content': '{"instance":"test","cmd":"id"}',
                                        'i': 'Verify forbidden scheduled tool cannot dispatch'}))
            session = rpc.PiSession('lazy-'+variant, 'sc117-flash-next-iq3_xxs', definitions, host, config)
            try:
                await session.start()
                answer = await asyncio.wait_for(session.prompt('Private optional-tool check.',
                    context.Turn('990826359704154225', cron=variant == 'cron')), 60)
                assert answer == 'VERIFIED_CONTEXT'
                captures[variant] = list(REQUESTS)
                names = {t['function']['name'] for t in REQUESTS[0]['tools']}
                native = {'read','grep','glob','edit','write','bash'}
                expected = {d['name'] for d in definitions if d['loadMode'] == 'essential'}
                assert names == native | expected, (variant, names, expected)
                checks[variant+'_direct_tools'] = len(names)
                if variant == 'after':
                    assert calls == ['gmail']
                    messages = [m for m in REQUESTS[-1]['messages'] if m['role'] == 'tool']
                    assert 'message_id' in str(messages[0]['content'])
                    assert '3141' in str(messages[1]['content']) and '27' in str(messages[1]['content'])
                    checks['deferred_schema_and_dispatch'] = True
                    assert 'Omit for non-root. Required only for run_as=root.' in str(messages[2]['content'])
                    checks['deferred_host_reason_omission_guidance'] = True
                elif variant == 'cron':
                    assert not calls
                    result = [m for m in REQUESTS[-1]['messages'] if m['role'] == 'tool'][-1]
                    assert 'No such tool: xd://shell_exec' in str(result['content']), result
                    checks['scheduled_forbidden_tool_not_dispatched'] = True
            finally:
                await session.close()
        if '--model' in sys.argv:
            with urllib.request.urlopen('http://127.0.0.1:8080/evo/status', timeout=5) as r:
                state = json.load(r)
            status = json.loads((rpc.RUNTIME/'status.json').read_text())
            if state.get('busy') or state.get('loading') or status.get('active') or status.get('queued'):
                raise RuntimeError('Real-model check requires an idle bot/model')
            variant = 'after'; calls.clear()
            session = rpc.PiSession('lazy-model', state['active'], registry.definitions(), host, candidate)
            started = time.monotonic()
            try:
                await session.start()
                answer = await asyncio.wait_for(session.prompt(
                    'Gmail 상태만 조회해서 전체 메일 수와 안 읽은 수를 짧게 알려줘. 개별 메일은 읽지 마.',
                    context.Turn('990826359704154225')), 180)
                assert calls == ['gmail'] and '27' in answer and ('3141' in answer or '3,141' in answer), answer
                checks['real_model'] = {'name': state['active'], 'answer': answer,
                                        'seconds': round(time.monotonic()-started, 2)}
            finally:
                await session.close()
        (report/'synthetic-payloads.json').write_text(json.dumps(captures, ensure_ascii=False))
        (report/'checks.json').write_text(json.dumps(checks, ensure_ascii=False, indent=2)+'\n')
        print(json.dumps(checks, ensure_ascii=False), flush=True)
    finally:
        rpc.sandbox_argv = original
        server.shutdown(); server.server_close(); shutil.rmtree(config)
        await registry.close()


if __name__ == '__main__': asyncio.run(main())
