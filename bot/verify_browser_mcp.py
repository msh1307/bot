"""Exercise the official MCP client against a disposable shared worker, without real logins."""
import asyncio
import base64
import concurrent.futures
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent
PNG = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9ZlS8AAAAASUVORK5CYII='
WORKER = '''import sys,json,os,time
from pathlib import Path
state='empty'
for line in sys.stdin:
 r=json.loads(line); p=r.get('params',{}); a=p.get('action')
 with Path(__file__).with_name('events').open('a') as f: f.write(str(a)+'\\n')
 if r['method']=='ready': result={'ready':True,'version':str(os.getpid())}
 elif a=='evaluate' and p.get('script')=='die': sys.exit(0)
 elif a=='evaluate' and p.get('script')=='sleep': time.sleep(.3); result={}
 elif a=='open': state=p['url']; result={'success':True,'url':state,'text':state}
 elif a=='screenshot': result={'content':[{'type':'image','mimeType':'image/png','data':PNG}]}
 else: result={'success':True,'text':state}
 print(json.dumps({'id':r['id'],'result':result}),flush=True)
'''.replace('PNG', repr(PNG))


class Checks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        for name in ('browser_broker.py', 'browser_mcp.py', 'browser-tool.json'):
            shutil.copyfile(ROOT/name, self.root/name)
        (self.root/'venv/bin').mkdir(parents=True)
        (self.root/'venv/bin/python').symlink_to(sys.executable)
        (self.root/'browser_remote.py').write_text(WORKER)
        self.log = (self.root/'stderr').open('wb')
        self.process = subprocess.Popen([sys.executable, str(self.root/'browser_broker.py')], stderr=self.log)
        end = time.monotonic()+5
        while not (self.root/'.browser.sock').exists():
            if self.process.poll() is not None or time.monotonic()>end: raise RuntimeError('Fixture broker failed')
            time.sleep(.01)
        self.clients = []

    def tearDown(self):
        for sock, stream in self.clients: stream.close(); sock.close()
        self.process.terminate(); self.process.wait(timeout=5)
        self.log.close(); self.temp.cleanup()

    def client(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); sock.settimeout(5)
        sock.connect(str(self.root/'.browser.sock')); stream = sock.makefile('rwb')
        self.clients.append((sock, stream)); return sock, stream

    def call(self, stream, method='call', **params):
        stream.write((json.dumps({'id':1,'method':method,'params':params})+'\n').encode()); stream.flush()
        return json.loads(stream.readline())

    def test_legacy_clients_share_worker_and_queued_disconnect_is_not_executed(self):
        _, a = self.client(); _, b = self.client()
        pid = self.call(a, 'ready')['result']['version']
        self.assertEqual(pid, self.call(b, 'ready')['result']['version'])
        self.call(a, action='open', url='https://fixture.invalid/shared')
        self.assertEqual(self.call(b, action='read')['result']['text'], 'https://fixture.invalid/shared')
        with concurrent.futures.ThreadPoolExecutor() as pool:
            sleeping = pool.submit(self.call, a, action='evaluate', script='sleep')
            end = time.monotonic()+2
            while 'evaluate' not in (self.root/'events').read_text() and time.monotonic()<end: time.sleep(.01)
            cancelled, wire = self.client()
            wire.write(b'{"id":2,"method":"call","params":{"action":"fill","value":"NOT_SENT"}}\n'); wire.flush()
            wire.close(); cancelled.close(); self.clients.pop()
            sleeping.result(timeout=2)
        time.sleep(.05)
        self.assertNotIn('fill', (self.root/'events').read_text().splitlines())
        self.assertEqual(self.call(b, action='status')['result']['running'], True)

    def test_official_mcp_one_tool_text_image_validation_and_worker_failure(self):
        async def verify():
            params = StdioServerParameters(command=sys.executable, args=[str(self.root/'browser_mcp.py')])
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = (await session.list_tools()).tools
                    self.assertEqual([t.name for t in tools], ['browser'])
                    self.assertEqual(tools[0].inputSchema, json.loads((ROOT/'browser-tool.json').read_text())['inputSchema'])
                    result = await session.call_tool('browser', {'action':'open','url':'https://fixture.invalid/mcp'})
                    self.assertFalse(result.isError)
                    _, legacy = self.client()
                    self.assertEqual(self.call(legacy, action='read')['result']['text'], 'https://fixture.invalid/mcp')
                    image = await session.call_tool('browser', {'action':'screenshot'})
                    self.assertEqual(image.content[0].type, 'image')
                    self.assertEqual(base64.b64decode(image.content[0].data), base64.b64decode(PNG))
                    await session.call_tool('browser', {'action':'close'})
                    self.assertTrue(self.call(legacy, action='status')['result']['running'])
                    bad = await session.call_tool('browser', {'action':'not-an-action'})
                    self.assertTrue(bad.isError)
                    failed = await session.call_tool('browser', {'action':'evaluate','script':'die'})
                    self.assertTrue(failed.isError)
                    self.assertEqual((self.root/'events').read_text().splitlines().count('evaluate'), 1)
        asyncio.run(verify())


if __name__ == '__main__': unittest.main(verbosity=2)
