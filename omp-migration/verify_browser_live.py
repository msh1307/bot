"""Exercise the registered Discord browser handler against real pages, not a browser mock.

Run as the service UID in a constrained transient unit; OUT must be writable RAM.
Optional --owner-cookies imports existing credentials without printing their values.
"""
import asyncio
import contextlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from evo_omp import context
from evo_omp.tools import Registry
from evo_omp.integrations import browser

OUT = Path(os.environ['OUT'])
OUT.mkdir(mode=0o700, parents=True, exist_ok=True)
browser.PROFILE = OUT / 'profile'
browser.PROFILE.mkdir(mode=0o700, exist_ok=True)
AUTH = '--owner-cookies' in sys.argv
if AUTH:
    original = Path('/var/lib/evo-omp/browser')
    source = original / 'session-cookies.json'
    if not source.exists(): source = original / 'cookies.json'
    shutil.copyfile(source, browser.PROFILE / ('session-cookies.json' if hasattr(browser, 'ENGINE') else 'cookies.json'))
    (browser.PROFILE / ('session-cookies.json' if hasattr(browser, 'ENGINE') else 'cookies.json')).chmod(0o600)

RESULT = {'started': time.time(), 'owner_cookies_used': AUTH,
          'path': 'Registry.call -> registered browser handler -> bwrap -> real browser',
          'discord_transport': 'not exercised by this direct registered-tool check',
          'calls': [], 'samples': []}
STAGE = 'controller'
STOP = threading.Event()


def sample():
    while not STOP.is_set():
        try:
            group = next(x.split(':', 2)[2] for x in Path('/proc/self/cgroup').read_text().splitlines() if x.startswith('0:'))
            cg = Path('/sys/fs/cgroup') / group.lstrip('/')
            pss = 0
            for pid in (cg / 'cgroup.procs').read_text().split():
                with contextlib.suppress(OSError, ProcessLookupError):
                    text = Path(f'/proc/{pid}/smaps_rollup').read_text()
                    pss += int(re.search(r'^Pss:\s+(\d+)', text, re.M)[1]) * 1024
            mem = {k: int(v.strip().split()[0]) for k, v in (x.split(':', 1) for x in Path('/proc/meminfo').read_text().splitlines())}
            vm = dict(x.split() for x in Path('/proc/vmstat').read_text().splitlines())
            RESULT['samples'].append({'time': time.time(), 'stage': STAGE, 'pss_bytes': pss,
                'cgroup_bytes': int((cg / 'memory.current').read_text()),
                'swap_bytes': int((cg / 'memory.swap.current').read_text()),
                'host_available_bytes': mem['MemAvailable'] * 1024,
                'pswpout': int(vm['pswpout'])})
        except OSError:
            pass
        STOP.wait(.5)


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        logged = 'evo_browser_audit=authenticated' in self.headers.get('Cookie', '')
        if self.path == '/dashboard':
            text = '<h1>' + ('DASHBOARD_AUTH_OK' if logged else 'DASHBOARD_LOGIN_REQUIRED') + '</h1>'
        else:
            text = '''<!doctype html><title>EVO login fixture</title><h1>Login fixture</h1>
            <form method="post" action="/login"><input id="name" name="name">
            <input id="password" name="password" type="password"><button id="login">Log in</button></form>
            <script>localStorage.setItem('evoAudit','persisted')</script>'''
        self.send_response(200); self.send_header('Content-Type', 'text/html'); self.end_headers()
        self.wfile.write(text.encode())
    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length', 0)))
        RESULT['fixture_posts'] = RESULT.get('fixture_posts',0)+1
        self.send_response(303)
        self.send_header('Set-Cookie', 'evo_browser_audit=authenticated; HttpOnly; Path=/; Max-Age=3600; SameSite=Lax')
        self.send_header('Location', '/dashboard'); self.end_headers()


async def main():
    global STAGE
    sampler = threading.Thread(target=sample, daemon=True); sampler.start()
    server = ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    reg = Registry()
    turn = context.Turn('990826359704154225')
    token = context.current.set(turn)

    async def call(action, **kwargs):
        start = time.monotonic()
        try:
            result = await reg.call('browser', dict(action=action, **kwargs), turn)
            RESULT['calls'].append({'action': action, 'success': True, 'elapsed': round(time.monotonic()-start, 3)})
            return result
        except Exception as exc:
            RESULT['calls'].append({'action': action, 'success': False,
                'error_class': type(exc).__name__, 'error': context.redact_sensitive_text(str(exc))[:220]})
            raise

    try:
        RESULT['definition'] = reg.tools['browser'][0]
        RESULT['initial_status'] = await call('status')
        STAGE = 'fixture'
        base = f'http://127.0.0.1:{server.server_port}'
        await call('open', url=base)
        await call('fill', selector='#name', value='audit-user')
        await call('fill', selector='#password', value='dummy-fixture-password')
        await call('click', selector='#login')
        page = await call('read')
        for _ in range(10):
            if 'DASHBOARD_AUTH_OK' in str(page): break
            await asyncio.sleep(.3)
            page = await call('read')
        RESULT['fixture_login'] = 'DASHBOARD_AUTH_OK' in str(page)
        await call('close')
        await call('open', url=base + '/dashboard')
        RESULT['fixture_login_restored'] = 'DASHBOARD_AUTH_OK' in str(await call('read'))
        storage = await call('evaluate', script="localStorage.getItem('evoAudit')")
        RESULT['local_storage_restored'] = 'persisted' in str(storage)
        STAGE = 'gemini'
        try:
            await call('open', url='https://gemini.google.com/app', timeout_ms=20000)
            await call('fill', selector='[contenteditable="true"]', value='What is 271828 + 314159? Reply only with the integer.')
            await call('press', key='Enter')
            deadline = time.monotonic() + 75
            while time.monotonic() < deadline:
                text = str(await call('read'))
                if '585987' in text:
                    RESULT['gemini_answer'] = '585987'
                    break
                await asyncio.sleep(2)
            if not RESULT.get('gemini_answer'): RESULT['gemini_failure'] = 'Expected answer did not appear within 75s'
        except Exception as exc:
            RESULT['gemini_failure'] = type(exc).__name__ + ': ' + str(exc)[:160]
        fp = await call('evaluate', script="JSON.stringify({webdriver:navigator.webdriver,ua:navigator.userAgent,hasAccountControl:!!document.querySelector('[aria-label*=\"Google Account\"],[aria-label*=\"Google 계정\"]'),signIn:!![...document.querySelectorAll('a,button')].find(e=>/^(Sign in|로그인)$/.test(e.innerText.trim()))})")
        RESULT['fingerprint_and_auth_before_close'] = fp
        STAGE = 'idle_30s'
        await asyncio.sleep(30)
        await call('close')
        await call('open', url='https://gemini.google.com/app', timeout_ms=20000)
        await asyncio.sleep(3)
        RESULT['auth_after_restart'] = await call('evaluate', script="JSON.stringify({hasAccountControl:!!document.querySelector('[aria-label*=\"Google Account\"],[aria-label*=\"Google 계정\"]'),signIn:!![...document.querySelectorAll('a,button')].find(e=>/^(Sign in|로그인)$/.test(e.innerText.trim()))})")
        if '--anti-bot' in sys.argv:
            STAGE = 'anti_bot'
            try:
                opened = await call('open', url='https://www.g2.com/', timeout_ms=20000)
                text = str(await call('read'))
                RESULT['anti_bot'] = {'url': 'https://www.g2.com/',
                    'challenge_detected': bool(re.search(r'Just a moment|verify you are human|Checking your browser|Access denied|Attention Required|unusual traffic', text, re.I)),
                    'text_excerpt': re.sub(r'[\w.+-]+@[\w.-]+', '[email]', text)[:1200],
                    'open_status': opened.get('http_status')}
            except Exception as exc:
                RESULT['anti_bot'] = {'error': type(exc).__name__ + ': ' + str(exc)[:160]}
        RESULT['final_status'] = await call('status')
    except Exception as exc:
        RESULT['error'] = type(exc).__name__ + ': ' + str(exc)[:300]
    finally:
        await reg.close()
        context.current.reset(token)
        server.shutdown()
        STAGE = 'closed'; await asyncio.sleep(1)
        STOP.set(); sampler.join(3)
        RESULT['ended'] = time.time()
        if RESULT['samples']:
            samples = RESULT['samples']
            RESULT['peak_pss_mib'] = round(max(s['pss_bytes'] for s in samples)/2**20, 2)
            RESULT['swapout_delta_pages'] = max(s['pswpout'] for s in samples)-min(s['pswpout'] for s in samples)
            RESULT['max_cgroup_swap_bytes'] = max(s['swap_bytes'] for s in samples)
            RESULT['minimum_host_available_gib'] = round(min(s['host_available_bytes'] for s in samples)/2**30, 3)
        (OUT / 'result.json').write_text(json.dumps(RESULT, ensure_ascii=False, indent=2))
        print(json.dumps({k:v for k,v in RESULT.items() if k not in ('samples','calls','definition')}, ensure_ascii=False), flush=True)


if __name__ == '__main__': asyncio.run(main())
