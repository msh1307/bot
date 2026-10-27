"""Private Flash check: correction and scope survive native manual compaction.

Uses the installed provider, a candidate OMP binary and synthetic read-only data.
No Discord messages or live session changes. Only this private test lowers the
retained-tail size so a short conversation exercises real model summarization.
"""
import asyncio
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import yaml

from verify_context_model import Handler, ThreadingHTTPServer, REQUESTS, idle, rpc, context


async def main():
    binary, report = map(Path, sys.argv[1:3]); report.mkdir(parents=True, exist_ok=True)
    idle()
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    cfg = Path(tempfile.mkdtemp(prefix='budget-model-', dir=rpc.RUNTIME))
    (cfg / 'models.yml').write_text((rpc.ROOT / 'models.yml').read_text().replace(
        ':8080/v1', f':{server.server_port}/v1'))
    settings = yaml.safe_load((rpc.ROOT / 'config.yml').read_text())
    settings['compaction']['keepRecentTokens'] = 128
    (cfg / 'config.yml').write_text(yaml.safe_dump(settings))
    original_argv = rpc.sandbox_argv
    def candidate_argv(profile, model):
        args = original_argv(profile, model); target = str(rpc.ROOT / 'bin/omp-v18.11.3')
        at = args.index(target)
        return args[:at] + ['--ro-bind', str(binary), target,
            '--setenv', 'EVO_COMPLETED_REASONING_CONTEXT', '1'] + args[at:]
    rpc.sandbox_argv = candidate_argv
    async def host(name, args, turn):
        assert name == 'codex_task' and args == {'action': 'status'}
        return {'state': 'completed', 'thread_id': 'synthetic',
                'answer': 'revision=rev-A17; passed=17; failed=6; owner_scope=READ_ONLY'}
    definition = {'name': 'codex_task', 'label': 'codex_task', 'loadMode': 'essential',
        'description': 'Read the synthetic completed task report.',
        'parameters': {'type': 'object', 'properties': {'action': {'type': 'string', 'enum': ['status']}},
                       'required': ['action']}}
    session = rpc.OmpSession('budget-model', 'sc117-flash-next-iq3_xxs', [definition], host, cfg)
    prompts = ['코드나 파일은 수정하지 마. codex_task status로 보고서를 확인하고 revision과 전체 건수를 알려줘.',
               '정정: revision은 rev-B29야. 보고서 수치는 그대로야. 정정된 revision과 전체 건수만 답해.']
    before = dict(line.split() for line in Path('/proc/vmstat').read_text().splitlines())
    answers = []
    try:
        await session.start()
        for prompt in prompts:
            idle(); answers.append(await asyncio.wait_for(session.prompt(prompt, context.Turn('990826359704154225')), 180))
        idle(); first = len(REQUESTS); began = time.monotonic()
        compacted = await asyncio.wait_for(session.compact(), 180)
        seconds = time.monotonic() - began; compact_requests = REQUESTS[first:]
        assert compact_requests, 'No model compaction was exercised'
        idle(); answer = await asyncio.wait_for(session.prompt(
            '현재 revision, 전체 건수, 이 작업에서 파일 수정을 허용했는지만 답해. 추가 도구는 필요 없어.',
            context.Turn('990826359704154225')), 180)
        answers.append(answer)
        assert 'rev-B29' in answer and '23' in answer, answer
        assert any(word in answer for word in ('불가', '아니', '금지', '없', '허용하지', '읽기')), answer
        after = dict(line.split() for line in Path('/proc/vmstat').read_text().splitlines())
        result = {'passed': True, 'scope': 'One private synthetic Flash conversation; not broad quality evidence',
                  'answers': answers, 'compaction_seconds': seconds, 'compaction_calls': len(compact_requests),
                  'compaction_thinking': [row['request'].get('chat_template_kwargs') for row in compact_requests],
                  'compaction_final_budget': [row['request'].get('final_output_max_tokens') for row in compact_requests],
                  'cancelled': compacted.get('cancelled', False), 'discord_posts': 0, 'model_switches': 0,
                  'swapout_delta_pages': int(after['pswpout'])-int(before['pswpout']),
                  'oom_kill_delta': int(after['oom_kill'])-int(before['oom_kill'])}
        (report / 'budget-model.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps(result, ensure_ascii=False), flush=True)
    finally:
        await session.close(); rpc.sandbox_argv = original_argv
        server.shutdown(); shutil.rmtree(cfg)


if __name__ == '__main__': asyncio.run(main())
