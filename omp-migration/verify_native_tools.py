"""Inspect model-visible native read/search/artifact results in the real sandbox."""
import asyncio
import json
from pathlib import Path
import re
import shutil
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import time

from evo_omp import context
from evo_omp.rpc import OmpSession, ROOT, WORKSPACE

REQUESTS = []
STEPS = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        REQUESTS.append(data)
        tool = STEPS.pop(0) if STEPS else None
        if callable(tool):
            tool = tool(data)
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Connection', 'close')
        self.end_headers()
        delta = {'content': 'VERIFIED_NATIVE'}
        if tool:
            delta = {'tool_calls': [{'index': 0, 'id': 'native_' + str(len(REQUESTS)),
                'type': 'function', 'function': {'name': tool[0], 'arguments': json.dumps(tool[1])}}]}
        for d, finish in (({'role': 'assistant'}, None), (delta, None),
                          ({}, 'tool_calls' if tool else 'stop')):
            chunk = {'id': 'native', 'object': 'chat.completion.chunk', 'created': int(time.time()),
                'model': data['model'], 'choices': [{'index': 0, 'delta': d, 'finish_reason': finish}]}
            if finish:
                tokens = max(1, len(json.dumps(data['messages']))//4)
                chunk['usage'] = {'prompt_tokens': tokens, 'completion_tokens': 16, 'total_tokens': tokens+16}
            self.wfile.write(('data: ' + json.dumps(chunk) + '\n\n').encode())
        self.wfile.write(b'data: [DONE]\n\n')
        self.close_connection = True


def text_of(message):
    content = message['content']
    return content if isinstance(content, str) else '\n'.join(b.get('text', '') for b in content)


async def main():
    server = ThreadingHTTPServer(('127.0.0.1', 18089), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    cfg = Path(tempfile.mkdtemp(prefix='native-config-', dir='/run/evo-omp'))
    source = Path(__file__).resolve().parent
    (cfg / 'models.yml').write_text((source / 'models.yml').read_text().replace(':8080/v1', ':18089/v1'))
    (cfg / 'config.yml').write_text((source / 'config.yml').read_text())
    fixture = Path(tempfile.mkdtemp(prefix='.omp-native-', dir=WORKSPACE))
    base = '/workspace/' + fixture.name
    # Long valid functions ensure the native AST summary has bodies to elide.
    c_source = '\n\n'.join('int function_%d(int value) {\n'%f +
        '\n'.join('    value += %d; /* BODY_%d_%d */'%(n, f, n) for n in range(1, 71)) +
        '\n    return value;\n}' for f in range(3)) + '\n'
    (fixture / 'large.c').write_text(c_source)
    sources = {'c': c_source}
    sources['go'] = 'package fixture\n\n' + '\n\n'.join('func function_%d(value int) int {\n'%f +
        '\n'.join('    value += %d // BODY_%d_%d'%(n, f, n) for n in range(1,71)) +
        '\n    return value\n}' for f in range(3))+'\n'
    sources['rs'] = '\n\n'.join('fn function_%d(mut value: i32) -> i32 {\n'%f +
        '\n'.join('    value += %d; // BODY_%d_%d'%(n, f, n) for n in range(1,71)) +
        '\n    value\n}' for f in range(3))+'\n'
    sources['py'] = '\n\n'.join('def function_%d(value):\n'%f +
        '\n'.join('    value += %d # BODY_%d_%d'%(n, f, n) for n in range(1,71)) +
        '\n    return value' for f in range(3))+'\n'
    for ext, source in sources.items():
        (fixture / ('large.'+ext)).write_text(source)
    (fixture / 'long.txt').write_text(''.join('ROW_%04d %s\n'%(i, 'x'*70) for i in range(1, 501)))
    search = fixture / 'search'
    search.mkdir()
    for i in range(25):
        (search / ('file_%02d.txt'%i)).write_text('before\nNEEDLE_%02d\nafter\n'%i)
    session = OmpSession('native-verify', 'sc117-flash-next-iq3_xxs', [], None, cfg)
    summary = {}
    async def run(steps):
        start = len(REQUESTS)
        STEPS.extend(steps)
        assert await session.prompt('Verify native tool contract.', context.Turn('native-verify')) == 'VERIFIED_NATIVE'
        # Final actual provider request contains the model-visible tool bodies.
        tools = [m for m in REQUESTS[-1]['messages'] if m['role'] == 'tool']
        return tools[-len(steps):], len(REQUESTS)-start
    try:
        await session.start()
        summary['structural_read'] = {}
        for ext, source in sources.items():
            tools, _ = await run([('read', {'path': base+'/large.'+ext}),
                                 ('read', {'path': base+'/large.'+ext+':80-105'})])
            structured, scoped = map(text_of, tools)
            assert 'function_0' in structured and 'function_2' in structured
            assert 'BODY_1_20' in scoped, scoped
            elided = 'BODY_1_20' not in structured
            if elided:
                assert re.search(r'(elid|omit|summar|read.*range)', structured, re.I), structured
            summary['structural_read'][ext] = {'source_bytes': len(source.encode()),
                'visible_bytes': len(structured.encode()), 'elided': elided,
                'body_reread': True, 'footer': structured[-1100:]}
        assert all(v['elided'] for v in summary['structural_read'].values()), summary['structural_read']
        tools, _ = await run([('read', {'path': base+'/long.txt'}),
                             ('read', {'path': base+'/long.txt:301-310'})])
        page, tail = map(text_of, tools)
        assert 'ROW_0300' in page and 'ROW_0301' not in page
        assert 'ROW_0301' in tail and 'ROW_0310' in tail
        assert '301' in page and re.search(r'(more|continu|showing)', page, re.I)
        summary['read_paging'] = {'continuation': True, 'footer': page[-400:]}
        tools, _ = await run([('grep', {'pattern': 'NEEDLE_', 'path': base+'/search'}),
                             ('grep', {'pattern': 'NEEDLE_', 'path': base+'/search', 'skip': 20})])
        first, second = map(text_of, tools)
        a, b = set(re.findall(r'NEEDLE_(\d+)', first)), set(re.findall(r'NEEDLE_(\d+)', second))
        assert len(a)==20 and len(b)==5 and not a & b and len(a|b)==25, (a,b,first,second)
        assert 'skip=20' in first, first[-500:]
        summary['grep_paging'] = {'first_files': len(a), 'next_files': len(b), 'footer': first[-450:]}
        def reopen(data):
            body = text_of(next(m for m in reversed(data['messages']) if m['role']=='tool'))
            found = re.search(r'artifact://[^\s)\]`<>]+', body)
            assert found, body[-1200:]
            return ('read', {'path': found.group(0).rstrip('.,')+':1001-1005'})
        tools, _ = await run([('bash', {'command': "python3 -c \"for i in range(2200): print(f'CAPTURE_{i:04d} ' + 'abcdefghij'*8)\""}), reopen])
        capture, recovered = map(text_of, tools)
        assert 'CAPTURE_1000' in recovered and 'CAPTURE_1004' in recovered, recovered
        artifacts = [p for p in session.profile.rglob('*') if p.is_file() and p.stat().st_size>100000]
        assert artifacts, 'No full capture in the runtime profile'
        assert all(str(p).startswith('/run/evo-omp/') for p in artifacts)
        summary['artifact'] = {'reopened': True, 'visible_bytes': len(capture.encode()),
            'paths': [str(p.relative_to(session.profile)) for p in artifacts],
            'full_bytes': sum(p.stat().st_size for p in artifacts), 'backing': '/run tmpfs'}
        Path(__file__).with_name('native-tools-results.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))
        print('PASS actual structural read, ranged reread, read/grep paging, tmpfs artifact recovery', flush=True)
        print(json.dumps({k: {x:y for x,y in v.items() if x!='footer'} for k,v in summary.items()}), flush=True)
    finally:
        profile = session.profile
        await session.close()
        assert not profile.exists(), 'Runtime artifacts survived session close'
        # Only the mkdtemp fixture owned by this verification is removed.
        assert fixture.parent.resolve()==WORKSPACE.resolve() and fixture.name.startswith('.omp-native-')
        shutil.rmtree(fixture)
        shutil.rmtree(cfg)
        server.shutdown()


if __name__ == '__main__':
    asyncio.run(main())
