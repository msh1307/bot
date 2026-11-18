"""Owned loopback fixtures only: custom buttons, cross-origin frames and a human approval wait."""
import argparse
import contextlib
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def verify(path, baseline=False):
    spec = importlib.util.spec_from_file_location('browser_fixture_worker', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    state = {'requests': 0, 'approvals': 0, 'confirmations': 0}

    class Auth(BaseHTTPRequestHandler):
        def do_GET(self):
            state['requests'] += 1
            body = b'''<title>Approval frame</title><label>Name<input id="name"></label>
<input id="password" type="password" value="DUMMY_SECRET_NOT_A_LABEL">
<input id="check" type="button" value="Check">
<a id="confirm" onclick="fetch('/confirm',{method:'POST'}).then(r=>r.text()).then(t=>document.querySelector('#result').textContent=t)">Confirm approval</a>
<div id="result">WAITING</div>'''
            self.send_response(200); self.send_header('Content-Type', 'text/html')
            self.end_headers(); self.wfile.write(body)

        def do_POST(self):
            if self.path == '/approve': state['approvals'] += 1
            elif self.path == '/confirm': state['confirmations'] += 1
            self.send_response(200); self.end_headers()
            self.wfile.write(b'LOGIN_OK' if state['approvals'] else b'WAITING')

        def log_message(self, *args): pass

    auth = ThreadingHTTPServer(('127.0.0.1', 0), Auth)

    class Main(BaseHTTPRequestHandler):
        def do_GET(self):
            body = ('''<title>Interactive fixture</title>
<a id="login" onclick="document.querySelector('#slot').innerHTML=&quot;&lt;iframe name='approval' src='http://127.0.0.1:PORT/'&gt;&lt;/iframe&gt;&quot;">Login</a>
<div id="custom" tabindex="0" onclick="this.textContent='Clicked'">Custom action</div>
<input id="agree" type="checkbox" style="display:none"><label id="agree-label" for="agree">Agree</label>
<div id="slot"></div>'''.replace('PORT', str(auth.server_port))).encode()
            self.send_response(200); self.send_header('Content-Type', 'text/html')
            if self.path=='/csp': self.send_header('Content-Security-Policy',"script-src 'self' 'unsafe-inline'")
            self.send_header('Set-Cookie', 'fixture_session=kept; Path=/; HttpOnly')
            self.end_headers(); self.wfile.write(body)

        def log_message(self, *args): pass

    main = ThreadingHTTPServer(('127.0.0.1', 0), Main)
    for server in (auth, main):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    checks = []
    worker = None
    temporary = tempfile.TemporaryDirectory(prefix='evo-browser-auth-fixture-',dir='/dev/shm')
    try:
        with contextlib.nullcontext(temporary.name) as temp:
            module.PROFILE = Path(temp)
            module.ENGINE = 'Camoufox'
            worker = module.Worker()
            result = worker.call({'action': 'open', 'url': f'http://127.0.0.1:{main.server_port}/', 'state': 'load'})
            controls = json.loads(result['text'])['controls']
            if baseline:
                assert not any(c['selector'] == '#login' for c in controls)
                assert not hasattr(worker, 'frames')
                return {'baseline_reproduced': True, 'missing_link_button': True, 'missing_frame_actions': True}
            assert any(c['selector'] == '#login' for c in controls)
            assert any(c['selector'] == '#custom' for c in controls)
            checks.append('href-less anchors and keyboard/click controls are discoverable')
            agree=next(c for c in controls if c['selector']=='#agree-label')
            assert agree['role']=='checkbox' and agree['checked'] is False
            worker.call({'action':'click','selector':'#agree-label'})
            agree=next(c for c in json.loads(worker.call({'action':'tree'})['text'])['controls'] if c['selector']=='#agree-label')
            assert agree['checked'] is True
            checks.append('hidden native checkboxes expose their visible labels and verified checked state')
            clicked = worker.call({'action': 'click', 'selector': '#login'})
            assert clicked['executed'] is True
            assert json.loads(clicked['text'])['title'] == 'Interactive fixture'
            worker.page.locator('iframe').wait_for(state='visible')
            assert 'WAITING' in worker.call({'action': 'tree'})['text']
            frames = json.loads(worker.call({'action': 'frames'})['text'])
            assert len(frames) == 1 and frames[0]['name'] == 'approval'
            inherited = module.Worker.__new__(module.Worker)
            inherited.page = worker.page
            assert len(inherited.frames()) == 1
            ref = frames[0]['frame']
            fields = json.loads(worker.call({'action': 'tree', 'frame': ref})['text'])['controls']
            assert any(c['selector'] == '#check' and c['role'] == 'button' and c['name'] == 'Check' for c in fields)
            assert 'DUMMY_SECRET_NOT_A_LABEL' not in json.dumps(fields)
            checks.append('cross-origin frame controls and button labels are exposed without input values')
            worker.call({'action': 'fill', 'frame': ref, 'selector': '#name', 'value': 'Fixture owner'})
            page = worker.page
            worker.call({'action': 'wait', 'duration_ms': 150})
            assert state == {'requests': 1, 'approvals': 0, 'confirmations': 0}
            assert worker.page is page and not page.is_closed()
            checks.append('approval wait does not click, resend or close the browser')
            import urllib.request
            with urllib.request.urlopen(urllib.request.Request(
                    f'http://127.0.0.1:{auth.server_port}/approve', data=b'', method='POST')) as reply:
                reply.read()
            worker.call({'action': 'click', 'frame': ref, 'selector': '#confirm'})
            worker.call({'action': 'wait', 'frame': ref, 'selector': '#result:text-is("LOGIN_OK")'})
            assert 'LOGIN_OK' in worker.call({'action': 'read', 'frame': ref})['text']
            assert state == {'requests': 1, 'approvals': 1, 'confirmations': 1}
            assert worker.page is page and any(c['name'] == 'fixture_session' for c in worker.ctx.cookies())
            checks.append('one approval then one click succeeds in the same browser session')
            worker.call({'action': 'evaluate', 'script': 'document.querySelector("iframe").remove()'})
            try:
                worker.call({'action': 'click', 'frame': ref, 'selector': '#confirm'})
            except ValueError as error:
                assert 'detached frame' in str(error)
            else:
                raise AssertionError('Detached frame silently fell back to the main page')
            checks.append('detached frame references are rejected without fallback or replay')
            worker.call({'action': 'click', 'selector': '#login'})
            worker.page.locator('iframe').wait_for(state='visible')
            newer = json.loads(worker.call({'action': 'frames'})['text'])
            assert newer[0]['frame'] != ref
            checks.append('frame IDs are not reused for a new authentication document')
            worker.call({'action':'open','url':f'http://127.0.0.1:{main.server_port}/csp'})
            for script in ('3+4','() => 7','window.__fixtureValue=3; window.__fixtureValue+4'):
                value=worker.call({'action':'evaluate','script':script,'timeout_ms':1000})
                assert json.loads(value['text'])==7
            checks.append('evaluate preserves expressions/functions/statements on a page forbidding unsafe-eval')
            try:
                worker.call({'action':'evaluate','script':'(() => { window.__once=(window.__once||0)+1; throw new SyntaxError("fixture"); })()'})
            except Exception as error: assert 'fixture' in str(error)
            else: raise AssertionError('Expected script exception')
            assert json.loads(worker.call({'action':'evaluate','script':'window.__once'})['text'])==1
            checks.append('runtime SyntaxError never replays the supplied script')
            page=worker.page;started=time.monotonic()
            try:
                worker.call({'action':'evaluate','script':'() => new Promise(r=>setTimeout(()=>r(7),500))','timeout_ms':50})
            except Exception as error: assert 'evaluation timed out' in str(error)
            else: raise AssertionError('evaluate ignored its deadline')
            assert time.monotonic()-started<.5 and worker.page is page and not page.is_closed()
            assert 'Interactive fixture' in worker.call({'action':'tree'})['text']
            assert any(c['name']=='fixture_session' for c in worker.ctx.cookies())
            checks.append('pending async evaluation respects its deadline without closing the page or login context')
            worker.close(); worker = None
    finally:
        try:
            if worker is not None: worker.close()
        finally:
            temporary.cleanup()
            for server in (main, auth): server.shutdown(); server.server_close()
    return {'passed': len(checks), 'checks': checks, 'owner_accounts_used': False,
            'target_traffic': False, 'synthetic_approval': True}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--worker', type=Path, required=True)
    parser.add_argument('--baseline', action='store_true')
    args = parser.parse_args()
    print(json.dumps(verify(args.worker, args.baseline)))
