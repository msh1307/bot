"""Actual OMP/bwrap/RPC output A/B with a scripted provider, no Discord posts.

Run on EVO as the bot user: python verify_harness_output.py CANDIDATE_DIR REPORT_DIR [BASELINE_DIR]
Candidate dir contains rpc.py, tool_output.py and config.yml. Production untouched.
After deployment use its code-backup directory for BASELINE_DIR (readable test copy).
"""
import asyncio
import importlib.util
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile

sys.path.insert(0, '/opt/evo-omp')
from evo_omp import context
from evo_omp.rpc import OmpSession as Baseline, ROOT
from verify_native_tools import Handler, REQUESTS, STEPS, text_of
from http.server import ThreadingHTTPServer
import threading


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


async def main():
    candidate, report = map(Path, sys.argv[1:3])
    baseline_dir = Path(sys.argv[3]) if len(sys.argv) > 3 else ROOT
    baseline_class = (load('evo_omp.rpc_baseline', baseline_dir / 'evo_omp/rpc.py').OmpSession
                      if baseline_dir != ROOT else Baseline)
    report.mkdir(parents=True, exist_ok=True)
    output = load('evo_omp.tool_output', candidate / 'tool_output.py')
    Candidate = load('evo_omp.rpc_candidate', candidate / 'rpc.py').OmpSession
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    fixtures = {
        'host_command': {'success': False, 'exit_code': 17, 'output_truncated': False,
            'output': ''.join(f'ROW_{i:04d} ' + '한글 mixed source checking ' * 2 + '\n' for i in range(240))},
        'codex_task': {'state': 'completed', 'thread_id': 'synthetic-thread',
            'answer': ''.join(f'ANSWER_{i:04d} Evidence source line, details available.\n' for i in range(500))},
        'browser': {'title': 'Fixture page', 'url': 'https://example.com/',
            'text': ''.join(f'ELEMENT_{i:04d} heading and page text 내용\n' for i in range(400))},
    }
    definitions = [dict(name=n, label=n, loadMode='essential', description='Return fixture output.',
                       parameters={'type': 'object', 'properties': {}}) for n in fixtures]
    async def handler(name, args, turn):
        return json.dumps(fixtures[name], ensure_ascii=False)
    rows, checks = [], []
    async def variant(label, session_class):
        cfg = Path(tempfile.mkdtemp(prefix='output-check-', dir='/run/evo-omp'))
        (cfg / 'models.yml').write_text((ROOT / 'models.yml').read_text().replace(
            ':8080/v1', f':{server.server_port}/v1'))
        shutil.copyfile((candidate if label == 'after' else baseline_dir) / 'config.yml', cfg / 'config.yml')
        session = session_class('output-check', 'sc117-flash-next-iq3_xxs', definitions, handler, cfg)
        try:
            await session.start()
            async def run(steps):
                STEPS.extend(steps)
                assert await session.prompt('Verify fixture only.', context.Turn('990826359704154225')) == 'VERIFIED_NATIVE'
                tools = [m for m in REQUESTS[-1]['messages'] if m['role'] == 'tool']
                return [text_of(m) for m in tools[-len(steps):]]
            for name in fixtures:
                body = (await run([(name, {})]))[0]
                rows.append({'case': name, 'variant': label, 'text': body, 'bytes': len(body.encode())})
                if label == 'after':
                    refs = re.findall(r'/home/agent/sessions/host-output/[a-f0-9]{64}\.txt', body)
                    assert refs, body[:400]
                    field = {'host_command': 'output', 'codex_task': 'answer', 'browser': 'text'}[name]
                    stored = session.profile / refs[0].removeprefix('/home/agent/')
                    assert stored.read_text() == fixtures[name][field]
                    expected = {'host_command': 'ROW_0120', 'codex_task': 'ANSWER_0120', 'browser': 'ELEMENT_0120'}[name]
                    read = (await run([('read', {'path': refs[0] + ':121-123'})]))[0]
                    assert expected in read and len(read.encode()) < 2048, read[:400]
                    checks.append(name + ': exact full source + native ranged read')
            # Streaming native tool also uses the smaller configured preview.
            cmd = "python3 -c \"for i in range(2200): print(f'CAPTURE_{i:04d} ' + 'abcdefghij'*8)\""
            body = (await run([('bash', {'command': cmd})]))[0]
            rows.append({'case': 'native_bash', 'variant': label, 'text': body, 'bytes': len(body.encode())})
            match = re.search(r'artifact://[^\s)\]`<>]+', body)
            assert match, body[-1200:]
            read = (await run([('read', {'path': match[0].rstrip('.,') + ':1001-1005'})]))[0]
            assert 'CAPTURE_1000' in read and 'CAPTURE_1004' in read
            checks.append(label + ': native artifact middle recovered')
            if label == 'after':
                # Session recovery copies sessions/, including our referenced files.
                saved = cfg / 'saved'
                shutil.copytree(session.profile / 'sessions', saved)
                await session.close()
                session = session_class('output-recovered', 'sc117-flash-next-iq3_xxs', definitions, handler, cfg)
                await session.start(resume_dir=saved)
                assert (session.profile / refs[0].removeprefix('/home/agent/')).read_text() == fixtures['browser']['text']
                read = (await run([('read', {'path': refs[0] + ':121-123'})]))[0]
                assert 'ELEMENT_0120' in read
                checks.append('RAM session recovery retains exact references')
        finally:
            profile = session.profile
            await session.close()
            assert not profile.exists()
            shutil.rmtree(cfg)
    try:
        await variant('before', baseline_class)
        await variant('after', Candidate)
        # Also exercise storage failure after a side-effectful handler: result survives.
        from unittest.mock import patch
        session = Candidate('failure-check', 'sc117-flash-next-iq3_xxs', definitions, handler)
        sent = []
        async def send(frame): sent.append(frame)
        session.send = send
        with patch.object(sys.modules['evo_omp.rpc_candidate'], 'compact_result', side_effect=OSError('full')):
            await session.host_call({'id': 'once', 'toolName': 'host_command'}, context.Turn('990826359704154225'))
        assert len(sent) == 1 and not sent[0].get('isError')
        assert json.loads(sent[0]['result']['content'][0]['text']) == fixtures['host_command']
        checks.append('storage failure returns original result, no command retry')
        (report / 'output-replies.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2))
        data = {'passed': True, 'path': 'real OMP binary + bwrap + host RPC + native read/bash; scripted provider',
                'checks': checks, 'cases': [{k:v for k,v in r.items() if k != 'text'} for r in rows],
                'model_inference': False, 'discord_posts': 0}
        (report / 'harness-output.json').write_text(json.dumps(data, indent=2))
        print(json.dumps(data), flush=True)
    finally:
        server.shutdown()


if __name__ == '__main__': asyncio.run(main())
