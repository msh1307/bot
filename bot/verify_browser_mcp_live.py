"""Local fixture only: real shared Camoufox, MCP images and cookie persistence."""
import asyncio
import base64
import http.server
import json
import os
from pathlib import Path
import socket
import select
import subprocess
import sys
import threading
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent


class Page(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'''<html><title>MCP shared browser test</title><body>
<h1>MCP shared browser test</h1><input id="name"><button id="go" onclick="document.getElementById('out').textContent='clicked:'+document.getElementById('name').value">Apply</button>
<p id="out">initial</p><input id="pw" type="password" value="FIXTURE_PASSWORD">
</body></html>'''
        self.send_response(200); self.send_header('Content-Type', 'text/html'); self.end_headers(); self.wfile.write(body)
    def log_message(self, *_): pass


class Legacy:
    def __init__(self):
        self.process = subprocess.Popen([str(ROOT/'venv/bin/python'), str(ROOT/'browser_remote.py')],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    def call(self, method='call', **params):
        self.process.stdin.write((json.dumps({'id':1, 'method':method, 'params':params})+'\n').encode()); self.process.stdin.flush()
        if not select.select([self.process.stdout], [], [], 40)[0]: raise TimeoutError('Legacy proxy did not reply')
        reply = json.loads(self.process.stdout.readline())
        if 'error' in reply: raise RuntimeError(reply['error'])
        return reply['result']
    def close(self):
        self.process.stdin.close(); self.process.wait(timeout=5)
        self.process.stdout.close(); self.process.stderr.close()


async def verify():
    fixture = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Page)
    threading.Thread(target=fixture.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{fixture.server_port}/'
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT/'browser_mcp.py')])
    records = []
    def check(name, condition):
        if not condition: raise AssertionError(name)
        records.append(name)
    async def call(client, **args):
        reply = await client.call_tool('browser', args)
        if reply.isError: raise RuntimeError(str(reply.content))
        return reply
    def text(reply): return json.loads(reply.content[0].text)
    legacy = Legacy()
    try:
        legacy.call('ready')
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                check('one browser tool', [t.name for t in (await client.list_tools()).tools] == ['browser'])
                opened = text(await call(client, action='open', url=url))
                check('existing Camoufox engine', opened['engine'] == 'Camoufox')
                await call(client, action='fill', selector='#name', value='MCP_SHARED_TEST')
                await call(client, action='click', selector='#go')
                check('DOM fill/click/read', 'clicked:MCP_SHARED_TEST' in text(await call(client, action='read'))['text'])
                check('legacy and MCP share the same live page', 'clicked:MCP_SHARED_TEST' in legacy.call(action='read')['text'])
                await call(client, action='evaluate', script="document.cookie='evo_mcp_fixture=shared; path=/; SameSite=Lax'; localStorage.setItem('evo_mcp_fixture','shared'); window.evoProbe='shared'; true")
                check('same JS page state', json.loads(legacy.call(action='evaluate', script='window.evoProbe')['text']) == 'shared')
                image_reply = await call(client, action='screenshot')
                image = next(b for b in image_reply.content if b.type == 'image')
                raw = base64.b64decode(image.data, validate=True)
                check('screenshot is MCP JPEG image content', image.mimeType == 'image/jpeg' and raw.startswith(b'\xff\xd8') and len(raw)>2000)
                output = ROOT/'mcp-verify.jpg'; output.write_bytes(raw); output.chmod(0o600)
                box = json.loads(text(await call(client, action='evaluate', script="(()=>{let r=document.getElementById('pw').getBoundingClientRect();return [r.x,r.y,r.width,r.height]})()"))['text'])
                inspect = ('from PIL import Image;import json,sys;im=Image.open(sys.argv[1]);'
                    'x,y,w,h=map(int,json.loads(sys.argv[2]));crop=im.crop((x,y,x+w,y+h)).convert("RGB");'
                    'print(json.dumps({"size":im.size,"black_ratio":sum(max(p)<20 for p in crop.getdata())/(crop.width*crop.height)}))')
                pixels = json.loads(subprocess.check_output([str(ROOT/'venv/bin/python'), '-c', inspect, str(output), json.dumps(box)], text=True))
                check('viewport and native password masking', pixels['size'] == [960,640] and pixels['black_ratio']>.95)
                workers = []
                for p in Path('/proc').iterdir():
                    if p.name.isdigit():
                        try: argv = (p/'cmdline').read_bytes().split(b'\0')
                        except OSError: continue
                        if (argv and Path(os.fsdecode(argv[0])).name.startswith('python')
                                and str(ROOT/'browser_worker.py').encode() in argv): workers.append(int(p.name))
                check('one worker across both clients', len(workers)==1)
                await call(client, action='close')
                check('MCP close preserves the other frontend', legacy.call(action='status')['running'])
        legacy.close(); legacy = None
        deadline = time.monotonic()+12
        while True:
            probe = Legacy()
            try: running = probe.call(action='status')['running']
            finally: probe.close()
            if not running: break
            if time.monotonic()>deadline: raise AssertionError('last client did not release browser')
            await asyncio.sleep(.1)
        check('last disconnect releases browser', True)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                await call(client, action='open', url=url)
                state = json.loads(text(await call(client, action='evaluate', script="({cookie:document.cookie,storage:localStorage.getItem('evo_mcp_fixture')})"))['text'])
                check('cookie and localStorage survive reconnect', 'evo_mcp_fixture=shared' in state['cookie'] and state['storage']=='shared')
                await call(client, action='evaluate', script="document.cookie='evo_mcp_fixture=; Max-Age=0; path=/';localStorage.removeItem('evo_mcp_fixture');true")
                await call(client, action='close')
        report = {'passed':len(records), 'checks':records, 'pixels':pixels, 'shared_profile':str(ROOT/'profile'),
                  'image_saved':str(output), 'no_real_login_or_external_site_used':True}
        (ROOT/'mcp-live-verification.json').write_text(json.dumps(report,indent=2))
        print(json.dumps(report),flush=True)
    finally:
        if legacy: legacy.close()
        fixture.shutdown()


if __name__ == '__main__': asyncio.run(verify())
