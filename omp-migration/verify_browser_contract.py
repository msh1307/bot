"""Real bwrap/renderer checks for browser lifetime, policy, images and paging."""
import asyncio
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from evo_omp import context
from evo_omp.tools import Registry
from evo_omp.integrations import browser


class Fixture(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def do_GET(self):
        if self.path == '/cookie':
            self.send_response(200);self.end_headers();self.wfile.write(self.headers.get('Cookie', '').encode());return
        if self.path=='/pixel.png':
            import io
            from PIL import Image
            out=io.BytesIO();Image.new('RGB',(4,4),'red').save(out,format='PNG')
            self.send_response(200);self.send_header('Content-Type','image/png');self.end_headers();self.wfile.write(out.getvalue());return
        if self.path == '/form':
            body = '''<title>Dynamic form</title><button id="reveal" onclick="reveal()">New request</button>
                <main id="content"></main><script>
                function reveal() {
                  document.querySelector('#content').innerHTML = `<form id="request">
                    <label>Email<input name="email" type="email" required></label>
                    <label>Plan<select name="plan"><option value="basic">Basic</option><option value="pro">Pro</option></select></label>
                    <label>Accept<input name="accept" type="checkbox" required></label>
                    <button type="submit">Send request</button></form>`;
                  document.querySelector('#request').onsubmit = async e => {
                    e.preventDefault();const f=new FormData(e.target);
                    const r=await fetch('/submit',{method:'POST',body:new URLSearchParams(f)});
                    document.querySelector('#content').textContent=await r.text();
                  };
                }</script>'''
        else:
            body = '<title>Contract fixture</title><h1>CONTRACT_OK</h1><input id="text" aria-label="Audit input"><button id="button" onclick="this.textContent=\'CLICKED_OK\'">Audit button</button><img id="pixel" src="/pixel.png"><pre>'+'ROW\n'*5000+'</pre>'
        self.send_response(403 if self.path=='/blocked' else 200);self.send_header('Content-Type','text/html');self.end_headers();self.wfile.write(body.encode())

    def do_POST(self):
        from urllib.parse import parse_qs
        fields = parse_qs(self.rfile.read(int(self.headers['Content-Length'])).decode())
        valid = self.path == '/submit' and fields == {'email': ['audit@example.test'], 'plan': ['pro'], 'accept': ['on']}
        self.send_response(200 if valid else 400)
        self.send_header('Set-Cookie', 'audit_session=restored; HttpOnly; SameSite=Lax; Path=/')
        self.end_headers();self.wfile.write(b'DYNAMIC_FORM_OK' if valid else b'WRONG_FORM_VALUES')


async def main():
    out = Path(os.environ['OUT']);out.mkdir(mode=0o700,parents=True,exist_ok=True)
    browser.PROFILE = out/'profile'
    server = ThreadingHTTPServer(('127.0.0.1',0),Fixture)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    reg = Registry(); turn = context.Turn('990826359704154225')
    token = context.current.set(turn); result = {}
    async def call(action,**args): return await reg.call('browser',dict(action=action,**args),turn)
    try:
        assert not (await call('status'))['running'];result['lazy_status'] = True
        await call('open',url=f'http://127.0.0.1:{server.server_port}/form')
        await call('click',selector='#reveal')
        fields = json.loads((await call('find',query='Email',role='textbox'))['text'])
        await call('fill',selector=fields[0]['selector'],value='audit@example.test')
        await call('select',selector='select[name="plan"]',value='pro')
        await call('check',selector='input[name="accept"]',checked=True)
        await call('press',selector='input[name="email"]',key='Enter')
        await call('wait',selector='#content:text-is("DYNAMIC_FORM_OK")')
        assert 'DYNAMIC_FORM_OK' in (await call('read'))['text'];result['dynamic_form_async_submit'] = True
        await call('close')
        await call('open',url=f'http://127.0.0.1:{server.server_port}/form')
        cookies = await call('evaluate',script="fetch('/cookie').then(r=>r.text())")
        assert 'audit_session=restored' in json.loads(cookies['text']);result['http_only_session_cookie_restored'] = True
        if '--model' in sys.argv:
            import urllib.request
            from evo_omp.rpc import OmpSession
            with urllib.request.urlopen('http://127.0.0.1:8080/evo/status', timeout=5) as response:
                state = json.load(response)
            status = json.loads(Path('/run/evo-omp/status.json').read_text())
            if state.get('busy') or state.get('loading') or status.get('active') or status.get('queued'):
                raise RuntimeError('Live model/bot busy; defer the optional model check')
            calls = []
            async def handler(name, args, current):
                calls.append(dict(tool=name, **args))
                return await reg.call(name, args, current)
            session = OmpSession('browser-form-audit', state['active'], [reg.tools['browser'][0]], handler)
            try:
                await session.start()
                started = time.monotonic()
                answer = await asyncio.wait_for(session.prompt(
                    f'Use only the browser tool to open http://127.0.0.1:{server.server_port}/form . '
                    'Click New request, inspect the newly displayed form and submit email audit@example.test, '
                    'plan Pro, Accept checked. Read and report the confirmation text. '
                    'Use normal form actions, not JavaScript evaluation or shell commands. '
                    'This is a synthetic local fixture; no login is needed. Keep the answer short.', turn), 240)
                actions = {c.get('action') for c in calls}
                assert 'DYNAMIC_FORM_OK' in answer and {'open','click','fill','select','check'} <= actions, (answer, calls)
                result['model_form'] = {'model': state['active'], 'answer': answer,
                    'calls': calls, 'seconds': round(time.monotonic()-started, 2)}
            finally:
                await session.close()
        await call('open',url=f'http://127.0.0.1:{server.server_port}')
        loaded = await call('evaluate',script="document.querySelector('#pixel').naturalWidth")
        assert loaded['text']=='0';result['images_blocked_by_default'] = True
        await call('open',url=f'http://127.0.0.1:{server.server_port}',images=True)
        loaded = await call('evaluate',script="document.querySelector('#pixel').naturalWidth")
        assert loaded['text']=='4' and (await call('status'))['images_enabled'];result['images_opt_in_loaded'] = True
        box=await call('evaluate',script="() => {const r=document.querySelector('#button').getBoundingClientRect();return {x:Math.round((r.left+r.right)/2),y:Math.round((r.top+r.bottom)/2)}}")
        await call('click',**json.loads(box['text']))
        assert 'CLICKED_OK' in (await call('read'))['text'];result['viewport_coordinate_click'] = True
        for invalid in ({'x':-1,'y':10},{'x':960,'y':10},{'x':True,'y':10},{'x':10},{'selector':'#button','x':10,'y':10}):
            try: await call('click',**invalid)
            except ValueError: pass
            else: raise AssertionError('Invalid coordinate click accepted')
        result['invalid_clicks_rejected'] = True
        for invalid in ({'action':'read','images':True},{'action':'open','url':f'http://127.0.0.1:{server.server_port}','images':'true'}):
            try: await reg.call('browser',invalid,turn)
            except ValueError: pass
            else: raise AssertionError('Invalid image option accepted')
        result['invalid_image_options_rejected'] = True
        await call('open',url=f'http://127.0.0.1:{server.server_port}')
        assert not (await call('status'))['images_enabled'];result['images_opt_in_resets_on_next_open'] = True
        blocked = await call('open',url=f'http://127.0.0.1:{server.server_port}/blocked')
        assert blocked['success'] is False and blocked['http_status']==403
        assert 'HTTP 403' in blocked['error'];result['http_error_not_reported_as_success'] = True
        await call('open',url=f'http://127.0.0.1:{server.server_port}')
        found = await call('find',query='Audit input',role='textbox')
        assert '#text' in found['text'];result['find_selectors'] = True
        inspected = await call('inspect',selector='#text')
        assert 'Audit input' in inspected['text'];result['inspect'] = True
        before_wait = time.monotonic();await call('wait',duration_ms=150)
        assert time.monotonic()-before_wait>=.14;result['explicit_wait_duration'] = True
        first = await call('read',char_limit=1000)
        assert len(first['text'])==1000 and first['next_offset']==1000
        second = await call('read',char_limit=1000,offset=1000)
        assert second['next_offset']==2000;result['snapshot_paging'] = True
        await call('fill',selector='#text',value='contract value')
        try: await call('read',char_limit=1000,offset=1000)
        except ValueError: result['stale_snapshot_rejected'] = True
        else: raise AssertionError('Stale snapshot accepted')
        image = await call('screenshot')
        block = next(c for c in image['content'] if c['type']=='image')
        raw = base64.b64decode(block['data'],validate=True)
        assert raw.startswith(b'\xff\xd8') and len(raw)<450000
        (out/'fixture.jpg').write_bytes(raw);result['screenshot_jpeg_bytes'] = len(raw)
        try: await call('fill',selector='#does-not-exist',value='DUMMY_SECRET_MUST_NOT_ECHO',timeout_ms=1000)
        except ValueError as exc:
            assert 'DUMMY_SECRET' not in str(exc);result['fill_error_redaction'] = True
        else: raise AssertionError('Missing selector unexpectedly filled')
        for who in (context.Turn(turn.channel,user='1'),context.Turn(turn.channel,platform='web')):
            t = context.current.set(who)
            try:
                try: await reg.call('browser',{'action':'status'},who)
                except PermissionError: pass
                else: raise AssertionError('Non-owner/Discord context accepted')
            finally: context.current.reset(t)
        result['owner_discord_only'] = True
        cron = context.Turn(turn.channel,cron=True);t = context.current.set(cron)
        try:
            await reg.call('browser',{'action':'read'},cron)
            try: await reg.call('browser',{'action':'evaluate','script':'1'},cron)
            except PermissionError: result['cron_read_only'] = True
            else: raise AssertionError('Cron evaluated page JS')
        finally: context.current.reset(t)
        args = browser.sandbox_argv()[:-3]+['/usr/bin/python3','-c',
            'from pathlib import Path;import json;print(json.dumps({p:Path(p).exists() for p in ["/home/msh/mshsrv","/etc/evo-omp/discord.env","/var/lib/evo-hermes","/workspace"]}))']
        check = await asyncio.to_thread(subprocess.run,args,capture_output=True,text=True,timeout=10)
        assert check.returncode==0,check.stderr
        assert not any(json.loads(check.stdout).values());result['host_credentials_not_mounted'] = True
        begin = time.monotonic()
        task = asyncio.create_task(call('evaluate',script='() => { while(true) {} }',timeout_ms=60000))
        await asyncio.sleep(.3);turn.cancelled.set()
        try: await asyncio.wait_for(task,12)
        except InterruptedError: result['cancelled_hung_script_seconds'] = round(time.monotonic()-begin,2)
        else: raise AssertionError('Hung JS was not interrupted')
        turn = context.Turn(turn.channel);context.current.set(turn)
        assert not (await call('status'))['running'];result['cancel_closes_worker'] = True
        await call('open',url=f'http://127.0.0.1:{server.server_port}')
        browser.IDLE_SECONDS = .2
        await call('read');await asyncio.sleep(1)
        assert not (await call('status'))['running'];result['idle_close'] = True
        await call('reset');assert not browser.PROFILE.exists();result['reset_removes_profile'] = True
    finally:
        await reg.close();context.current.reset(token);server.shutdown()
        (out/'result.json').write_text(json.dumps(result,indent=2))
    print(json.dumps(result),flush=True)


if __name__=='__main__': asyncio.run(main())
