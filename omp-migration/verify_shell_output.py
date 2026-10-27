"""Compare installed and candidate OMP shell output through real bwrap/RPC.

Run as the bot user: python verify_shell_output.py CANDIDATE_BINARY REPORT_DIR
Uses a local scripted provider and disposable files; sends no Discord messages.
"""
import asyncio
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer

sys.path.insert(0, '/opt/evo-omp')
from evo_omp import context, rpc
from verify_native_tools import Handler, REQUESTS, STEPS, text_of


async def main():
    binary, report = map(Path, sys.argv[1:3])
    report.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    original_argv = rpc.sandbox_argv
    def candidate_argv(profile, model):
        args = original_argv(profile, model)
        target = str(rpc.ROOT / 'bin/omp-v18.11.3')
        at = args.index(target)
        return args[:at] + ['--ro-bind', str(binary), target] + args[at:]

    fixtures = {
        'long': ''.join(f'CAPTURE_{i:04d} ' + 'abcdefghij' * 8 + '\n' for i in range(2200)),
        'unicode_failure': ''.join(f'ROW_{i:04d} 한글🙂 exact evidence ' + 'x' * 50 + '\n' for i in range(400)),
        'small': 'small complete result\n',
        'wide': 'wide_' + '한글🙂' * 1500 + '\nlast\n',
    }
    cfg = Path(tempfile.mkdtemp(prefix='shell-config-', dir=rpc.RUNTIME))
    fixture = Path(tempfile.mkdtemp(prefix='.omp-shell-', dir=rpc.WORKSPACE))
    (cfg / 'models.yml').write_text((rpc.ROOT / 'models.yml').read_text().replace(':8080/v1', f':{server.server_port}/v1'))
    shutil.copyfile(rpc.ROOT / 'config.yml', cfg / 'config.yml')
    for name, raw in fixtures.items():
        (fixture / (name + '.txt')).write_text(raw)
    rows, checks = [], []
    installed = binary.resolve() == (rpc.ROOT / 'bin/omp-v18.11.3').resolve()
    try:
        for variant in (('after',) if installed else ('before', 'after')):
            rpc.sandbox_argv = original_argv if variant == 'before' else candidate_argv
            session = rpc.OmpSession('shell-output-check', 'sc117-flash-next-iq3_xxs', [], None, cfg)
            try:
                await session.start()
                async def call(tool, args):
                    STEPS.append((tool, args))
                    assert await session.prompt('Inspect the fixture.', context.Turn('shell-check')) == 'VERIFIED_NATIVE'
                    return text_of(next(m for m in reversed(REQUESTS[-1]['messages']) if m['role'] == 'tool'))
                for name, raw in fixtures.items():
                    command = f'cat /workspace/{fixture.name}/{name}.txt'
                    if name == 'unicode_failure': command += '; exit 7'
                    body = await call('bash', {'command': command})
                    rows.append({'variant': variant, 'case': name, 'bytes': len(body.encode()), 'text': body})
                    if name == 'small':
                        assert raw.strip() in body and 'artifact://' not in body
                    if variant == 'after':
                        assert len(body.encode()) < 3000, (name, len(body.encode()))
                        assert '\ufffd' not in body
                    if name == 'unicode_failure':
                        assert re.search(r'(exit|code).*7', body, re.I), body[-800:]
                    if name in ('long', 'unicode_failure'):
                        match = re.search(r'artifact://\d+', body)
                        assert match, body[-800:]
                        ref = match[0]
                        line = 1001 if name == 'long' else 201
                        reread = await call('read', {'path': f'{ref}:{line}-{line+2}'})
                        marker = 'CAPTURE_1000' if name == 'long' else 'ROW_0200'
                        assert marker in reread
                        stored = [p.read_bytes() for p in session.profile.rglob('*.log')
                                  if p.stat().st_size >= len(raw.encode())]
                        if variant == 'before' and name == 'unicode_failure':
                            # The old final cap saves stdout plus its exit-status notice.
                            assert any(data.startswith(raw.encode()) and re.search(
                                rb'(exit|code).*7', data[len(raw.encode()):], re.I) for data in stored)
                        else:
                            assert raw.encode() in stored, 'Full capture missing or changed'
                if variant == 'after':
                    saved = cfg / 'saved'
                    shutil.copytree(session.profile / 'sessions', saved)
                    await session.close()
                    session = rpc.OmpSession('shell-output-recovered', 'sc117-flash-next-iq3_xxs', [], None, cfg)
                    await session.start(resume_dir=saved)
                    reread = await call('read', {'path': f'{ref}:201-203'})
                    assert 'ROW_0200' in reread
                    checks.append('RAM session recovery preserves artifact references')
            finally:
                await session.close()
        checks += [('installed OMP + bwrap + provider request' if installed else
                    'real OMP + bwrap + provider request A/B'), 'small output remains inline',
                   'exact full capture and native ranged reread', 'failure exit code retained',
                   'Unicode and wide lines remain readable']
        (report / 'replies.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2))
        summary = {'passed': True, 'checks': checks, 'cases': [
            {k:v for k,v in row.items() if k != 'text'} for row in rows],
            'model_inference': False, 'discord_posts': 0}
        (report / 'shell-output.json').write_text(json.dumps(summary, indent=2))
        print(json.dumps(summary), flush=True)
    finally:
        rpc.sandbox_argv = original_argv
        server.shutdown()
        shutil.rmtree(cfg)
        shutil.rmtree(fixture)


if __name__ == '__main__': asyncio.run(main())
