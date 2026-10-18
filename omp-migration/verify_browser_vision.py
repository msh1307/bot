"""Measure real OMP browser screenshots through Flash vision; no account pages."""
import asyncio
import base64
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import sys
import threading
import time
import urllib.request

from evo_omp import context
from evo_omp.rpc import OmpSession
from evo_omp.tools import Registry
from evo_omp.integrations import browser

OUT = Path(os.environ['OUT'])
OUT.mkdir(mode=0o700, parents=True, exist_ok=True)
browser.PROFILE = OUT / 'profile'
browser.PROFILE.mkdir(mode=0o700, exist_ok=True)
SELF_CG = Path('/sys/fs/cgroup') / next(x.split('::')[1].strip().lstrip('/')
    for x in Path('/proc/self/cgroup').read_text().splitlines() if x.startswith('0::'))
STOP = threading.Event()
ABORT = threading.Event()
PHASE = 'baseline'
RESULT = {'scope': 'Installed OMP -> browser screenshot -> real Flash vision',
    'discord_transport': 'Not exercised; installed RPC and registered browser tool path.',
    'full_128k_context': False, 'samples': [], 'turns': [], 'calls': []}
CASES = [{'code': str(secrets.randbelow(900000) + 100000), 'circles': n,
          'square': side} for n, side in ((2, 'LEFT'), (3, 'RIGHT'), (1, 'LEFT'))]
HEAVY = '--heavy-page' in sys.argv
CLICK_THROUGH = '--click-through' in sys.argv
if HEAVY:
    CASES = CASES[:1]
    RESULT['scope'] += '; real Gemini app underneath a controlled canvas overlay'
if CLICK_THROUGH:
    CASES = CASES[:1]
    CASES[0]['square'] = secrets.choice(['LEFT','RIGHT'])
    RESULT['scope'] += '; model selects viewport coordinates and clicks the blue square'


def number(path):
    try: return int(Path(path).read_text().strip())
    except (OSError, ValueError): return None


def counters(path):
    try: return {k:int(v) for k,v in (s.split() for s in Path(path).read_text().splitlines())}
    except (OSError, ValueError): return {}


def snapshot():
    mem = {s.split(':')[0]: int(s.split()[1])*1024 for s in
           Path('/proc/meminfo').read_text().splitlines() if len(s.split()) == 3}
    vm = counters('/proc/vmstat')
    row = {'time': time.time(), 'phase': PHASE, 'available': mem['MemAvailable'],
        'swap_used': mem['SwapTotal']-mem['SwapFree'],
        'pswpout': vm['pswpout'], 'pswpin': vm['pswpin'], 'oom_kill': vm['oom_kill']}
    for name, cg in [('test', SELF_CG), ('flash', Path('/sys/fs/cgroup/system.slice/evo-flash.service')),
                     ('bot', Path('/sys/fs/cgroup/system.slice/evo-omp.service'))]:
        row[name+'_memory'] = number(cg/'memory.current')
        row[name+'_swap'] = number(cg/'memory.swap.current')
        row[name+'_events'] = counters(cg/'memory.events')
    pss = 0
    for pid in (SELF_CG/'cgroup.procs').read_text().split():
        with contextlib.suppress(OSError, ProcessLookupError):
            text = Path(f'/proc/{pid}/smaps_rollup').read_text()
            pss += int(re.search(r'^Pss:\s+(\d+)', text, re.M)[1])*1024
    row['test_pss'] = pss
    row['vision_active'] = Path('/run/evo-flash-vision/encoding.json').exists()
    row['encoder_count'] = 0
    for p in Path('/proc').glob('[0-9]*/comm'):
        with contextlib.suppress(OSError):
            if p.read_text().strip() == 'strata-vision': row['encoder_count'] += 1
    return row


def sample():
    while not STOP.is_set():
        try:
            row = snapshot(); RESULT['samples'].append(row)
            if row['available'] < 6.25*2**30 or row['oom_kill'] > RESULT['baseline']['oom_kill']:
                RESULT['guard'] = row; ABORT.set()
        except (OSError, KeyError): pass
        STOP.wait(.25)


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        try: case = CASES[int(self.path.strip('/'))]
        except (ValueError, IndexError): self.send_error(404); return
        # Canvas pixels, not page text or accessible controls. No DOM text answer.
        text = '''<!doctype html><title>Visual fixture</title>
<style>body{margin:0;background:white}</style><canvas width="960" height="640"></canvas>
<script>const c=document.querySelector('canvas').getContext('2d');
c.fillStyle='white';c.fillRect(0,0,960,640);c.fillStyle='black';
c.font='bold 40px sans-serif';c.fillText('VISUAL CHECK',50,65);
c.font='bold 62px monospace';c.fillText('CODE: '+%s,50,160);
c.fillStyle='#d52020';for(let i=0;i<%d;i++){c.beginPath();c.arc(140+i*180,290,48,0,Math.PI*2);c.fill();}
c.fillStyle='#1559dd';c.fillRect(%d,430,110,110);
c.canvas.addEventListener('click',e=>{const r=c.canvas.getBoundingClientRect();
const x=e.clientX-r.left,y=e.clientY-r.top;
if(x>=%d&&x<=%d&&y>=430&&y<=540){document.body.dataset.clicked='yes';
document.title='VISUAL_CLICK_OK';}});
</script>''' % (json.dumps(case['code']), case['circles'],
                70 if case['square']=='LEFT' else 760,
                70 if case['square']=='LEFT' else 760,
                180 if case['square']=='LEFT' else 870)
        raw = text.encode()
        self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)


def api(path):
    with urllib.request.urlopen('http://127.0.0.1:8080'+path, timeout=10) as r: return json.load(r)


class ObservedSession(OmpSession):
    async def dispatch(self, event):
        if event.get('type')=='tool_execution_start':
            RESULT.setdefault('all_tool_names', []).append(event.get('toolName'))
        return await super().dispatch(event)


async def main():
    global PHASE
    reg = Registry(); session = None; monitor = None; server = None
    start = time.monotonic()
    async def handler(name, args, turn):
        allowed=('open','screenshot','click') if CLICK_THROUGH else ('open','screenshot')
        if name!='browser' or args.get('action') not in allowed:
            raise ValueError('This isolated visual test allows browser '+', '.join(allowed))
        if CLICK_THROUGH and args.get('action')=='click' and 'selector' in args:
            raise ValueError('This visual test requires viewport coordinates')
        began=time.monotonic(); value=await reg.call(name,args,turn)
        entry={'action':args.get('action'), 'seconds':round(time.monotonic()-began,3)}
        if args.get('action')=='click':entry.update({k:args.get(k) for k in ('x','y')})
        for block in value.get('content',[]):
            if block.get('type')=='image':
                raw=base64.b64decode(block['data'],validate=True)
                entry['image_bytes']=len(raw)
                (OUT/f'screenshot-{len(RESULT["calls"])}.jpg').write_bytes(raw)
        RESULT['calls'].append(entry)
        return value
    try:
        state=api('/evo/status'); bot=json.loads(Path('/run/evo-omp/status.json').read_text())
        if (state['active']!='sc117-flash-next-iq3_xxs' or state.get('busy') or
                state.get('loading') or state.get('maintenance') or bot['active'] or bot['queued']):
            raise RuntimeError('Production is busy or Flash is not active; test not started')
        RESULT['baseline']=snapshot()
        monitor=threading.Thread(target=sample,daemon=True); monitor.start()
        server=ThreadingHTTPServer(('127.0.0.1',0),Fixture)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        session=ObservedSession('browser-vision-check','sc117-flash-next-iq3_xxs',
            [reg.tools['browser'][0]],handler)
        PHASE='omp_start'; await session.start()
        turn=context.Turn(context.OWNER)
        for i, case in enumerate(CASES):
            PHASE=f'vision_turn_{i+1}'; began=time.monotonic()
            if HEAVY:
                token=context.current.set(turn)
                try:
                    await reg.call('browser',{'action':'open','url':'https://gemini.google.com/app','timeout_ms':30000},turn)
                    await reg.call('browser',{'action':'wait','duration_ms':5000},turn)
                    # Keep the real app alive, but cover it with a non-private known visual.
                    setup=await reg.call('browser',{'action':'evaluate','script':
                        "() => {const v=document.createElement('canvas');v.width=960;v.height=640;"
                        "v.style='position:fixed;inset:0;width:960px;height:640px;z-index:2147483647';"
                        "document.body.appendChild(v);const c=v.getContext('2d');"
                        "c.fillStyle='white';c.fillRect(0,0,960,640);c.fillStyle='black';"
                        "c.font='bold 40px sans-serif';c.fillText('VISUAL CHECK',50,65);"
                        "c.font='bold 62px monospace';c.fillText('CODE: '+"+json.dumps(case['code'])+",50,160);"
                        "c.fillStyle='#d52020';for(let i=0;i<"+str(case['circles'])+";i++){c.beginPath();"
                        "c.arc(140+i*180,290,48,0,Math.PI*2);c.fill();}"
                        "c.fillStyle='#1559dd';c.fillRect(70,430,110,110);"
                        "return {title:document.title,real_app:location.origin,canvas_ready:true}}"},turn)
                    RESULT['heavy_page_setup']=setup
                finally: context.current.reset(token)
                instruction='The browser already has a prepared visual test screen. Take a browser screenshot. '
            else:
                instruction=f'Open http://127.0.0.1:{server.server_port}/{i} then take a browser screenshot. '
            prompt=('Visual integration test. Use only the browser tool. '+instruction+
                'Read the CODE digits from the screenshot pixels, count red circles, '
                'and identify whether the blue square is on the LEFT or RIGHT. Do not use read, evaluate, bash, '
                'page source, or other tools. '+
                ('After seeing the screenshot, click the center of the blue square using viewport x,y coordinates. '
                 if CLICK_THROUGH else '')+
                'Answer only: CODE=<digits>; RED=<count>; BLUE=<LEFT or RIGHT>. '
                'If the image is unavailable, say so. Do not guess.')
            job=asyncio.create_task(session.prompt(prompt,turn))
            while not job.done():
                if ABORT.is_set():
                    await session.abort(reason='visual memory test guard')
                    raise RuntimeError('Available RAM guard stopped the test')
                await asyncio.sleep(.25)
                if time.monotonic()-began > 180:
                    await session.abort(reason='visual test timeout')
                    raise TimeoutError('Visual turn exceeded 180 seconds')
            answer=await job
            if CLICK_THROUGH:
                token=context.current.set(turn)
                try:
                    check=await reg.call('browser',{'action':'evaluate','script':
                        "() => ({clicked:document.body.dataset.clicked==='yes',title:document.title})"},turn)
                    RESULT['independent_click_check']=check
                finally:context.current.reset(token)
            expected=f'CODE={case["code"]}; RED={case["circles"]}; BLUE={case["square"]}'
            ok=(re.search(r'CODE\s*=\s*'+case['code'],answer) is not None and
                re.search(r'RED\s*=\s*'+str(case['circles']),answer) is not None and
                re.search(r'BLUE\s*=\s*'+case['square'],answer,re.I) is not None)
            if CLICK_THROUGH:ok=ok and json.loads(check['text']).get('clicked') is True
            row={'case':i+1, 'answer':answer[:1500], 'expected':expected, 'passed':ok,
                'seconds':round(time.monotonic()-began,3), 'rounds':session.rounds, 'tools':session.tools}
            RESULT['turns'].append(row); print(json.dumps(row),flush=True)
            PHASE=f'after_turn_{i+1}'; await asyncio.sleep(4)
        PHASE='browser_closed'; await reg.close(); await asyncio.sleep(4)
        RESULT['passed']=all(t['passed'] for t in RESULT['turns']) and len(RESULT['turns'])==len(CASES)
    except BaseException as exc:
        RESULT['error']=type(exc).__name__+': '+context.redact_sensitive_text(str(exc))[:400]
        if session:
            with contextlib.suppress(Exception): await session.abort(reason='visual test stopped')
    finally:
        if session:
            with contextlib.suppress(Exception): await session.close()
        await reg.close()
        if server: server.shutdown()
        PHASE='closed'; await asyncio.sleep(2)
        STOP.set()
        if monitor: monitor.join(3)
        RESULT['after']=snapshot(); RESULT['seconds']=round(time.monotonic()-start,3)
        ss=RESULT['samples']
        if ss:
            RESULT['minimum_available_gib']=round(min(s['available'] for s in ss)/2**30,3)
            RESULT['peak_test_pss_mib']=round(max(s['test_pss'] for s in ss)/2**20,2)
            RESULT['peak_test_cgroup_mib']=round(max(s['test_memory'] for s in ss)/2**20,2)
            RESULT['peak_flash_cgroup_gib']=round(max(s['flash_memory'] for s in ss)/2**30,3)
            RESULT['flash_cgroup_peak_delta_mib']=round((max(s['flash_memory'] for s in ss)-RESULT['baseline']['flash_memory'])/2**20,2)
            RESULT['swapout_delta_bytes']=(RESULT['after']['pswpout']-RESULT['baseline']['pswpout'])*os.sysconf('SC_PAGE_SIZE')
            RESULT['oom_delta']=RESULT['after']['oom_kill']-RESULT['baseline']['oom_kill']
            RESULT['max_encoder_count']=max(s['encoder_count'] for s in ss)
            RESULT['max_test_swap_bytes']=max(s['test_swap'] for s in ss)
            RESULT['max_flash_swap_bytes']=max(s['flash_swap'] for s in ss)
            RESULT['phases']={}
            for p in dict.fromkeys(s['phase'] for s in ss):
                rows=[s for s in ss if s['phase']==p]
                RESULT['phases'][p]={'minimum_available_gib':round(min(s['available'] for s in rows)/2**30,3),
                    'peak_test_cgroup_mib':round(max(s['test_memory'] for s in rows)/2**20,2),
                    'peak_flash_cgroup_gib':round(max(s['flash_memory'] for s in rows)/2**30,3),
                    'encoder_seen':any(s['encoder_count'] for s in rows)}
        (OUT/'result.json').write_text(json.dumps(RESULT,ensure_ascii=False,indent=2))
        print(json.dumps({k:v for k,v in RESULT.items() if k!='samples'},ensure_ascii=False),flush=True)


if __name__=='__main__': asyncio.run(main())
