"""Private stdio worker, launched only inside the browser bwrap."""
import base64
import json
import os
from pathlib import Path
import sys
from playwright.sync_api import sync_playwright

PROFILE = Path('/profile')
BINARY = '/opt/evo-omp/browser-runtime/chrome-headless-shell-linux64/chrome-headless-shell'
MAX_TEXT = 200000
CONTROLS = r'''({query,role}) => {
 const visible=e=>!!e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden';
 const selector=e=>{
   if(e.id)return '#'+CSS.escape(e.id);
   const name=e.getAttribute('name');
   if(name){const s=e.localName+'[name='+JSON.stringify(name)+']';if(document.querySelectorAll(s).length===1)return s;}
   for(const a of ['aria-label','data-testid','placeholder','contenteditable','role']){
     const v=e.getAttribute(a);if(v){const s=e.localName+'['+a+'='+JSON.stringify(v)+']';if(document.querySelectorAll(s).length===1)return s;}
   }
   const parts=[];let n=e;
   while(n&&n!==document.body){let i=1,p=n;while((p=p.previousElementSibling))if(p.localName===n.localName)i++;
     parts.unshift(n.localName+':nth-of-type('+i+')');n=n.parentElement;}
   return 'body > '+parts.join(' > ');
 };
 const nodes=[...document.querySelectorAll('a[href],button,input:not([type=hidden]),textarea,select,[contenteditable=true],[role]')];
 const output=[];
 for(const e of nodes){
   if(!visible(e))continue;
   const r=e.getAttribute('role')||({a:'link',button:'button',textarea:'textbox',select:'combobox'}[e.localName])||
     (e.localName==='input'?({checkbox:'checkbox',radio:'radio',submit:'button'}[e.type]||'textbox'):'textbox');
   const label=e.getAttribute('aria-label')||e.labels?.[0]?.innerText||e.getAttribute('placeholder')||e.innerText||e.getAttribute('name')||'';
   if(role&&r!==role)continue;if(query&&!label.toLowerCase().includes(query.toLowerCase()))continue;
   output.push({role:r,name:label.trim().slice(0,180),selector:selector(e),...(e.href?{url:e.href}:{})});
   if(output.length>=150)break;
 }
 return output;
}'''


class Worker:
    def __init__(self):
        self.pw = sync_playwright().start()
        self.ctx = self.pw.chromium.launch_persistent_context(
            str(PROFILE/'chromium'),executable_path=BINARY,headless=True,
            # Ubuntu's unpriv_bwrap policy denies nested SYS_ADMIN. The outer
            # bwrap keeps mount/PID/IPC/user isolation; do not relax host policy.
            chromium_sandbox=False,viewport={'width':960,'height':640},
            locale='en-US',timezone_id='Asia/Seoul',accept_downloads=False,
            args=['--disk-cache-size=1','--media-cache-size=1'])
        self.images_enabled = False
        self.ctx.route('**/*',self.route)
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.page.on('dialog',lambda d:d.dismiss())
        self.cache = None
        self.http_status = None
        self.page.on('response',self.navigation_response)
        self.imported = self.restore_cookies()

    def navigation_response(self,response):
        if response.request.is_navigation_request() and response.frame==self.page.main_frame:
            self.http_status = response.status

    def route(self,route):
        kind = route.request.resource_type
        if kind=='media' or kind=='image' and not self.images_enabled: route.abort()
        else: route.continue_()

    def restore_cookies(self):
        path = PROFILE/'session-cookies.json'
        if not path.exists(): return 0
        cookies = json.loads(path.read_text()); imported = 0
        for source in cookies:
            cookie = {k:source[k] for k in ('name','value','domain','path','secure','httpOnly') if k in source}
            if isinstance(source.get('expires'),(int,float)) and source['expires']>0:
                cookie['expires'] = source['expires']
            same = str(source.get('sameSite','Lax')).lower()
            cookie['sameSite'] = {'lax':'Lax','strict':'Strict','none':'None'}.get(same,'Lax')
            try: self.ctx.add_cookies([cookie]); imported += 1
            except Exception: continue
        return imported

    def close(self):
        try:
            path = PROFILE/'session-cookies.json'; temp = path.with_suffix('.tmp')
            temp.write_text(json.dumps(self.ctx.cookies(),separators=(',',':')))
            temp.chmod(0o600); temp.replace(path)
        finally:
            try: self.ctx.close()
            finally: self.pw.stop()

    def snapshot(self,action,args):
        if action in ('open','tree'):
            return json.dumps({'title':self.page.title(),'controls':self.page.evaluate(CONTROLS,{'query':'','role':''})},ensure_ascii=False)
        if action=='find':
            return json.dumps(self.page.evaluate(CONTROLS,{'query':args['query'],'role':args.get('role','')}),ensure_ascii=False)
        if action=='links':
            return json.dumps(self.page.locator('a[href]').evaluate_all(
                '(es)=>es.slice(0,500).map(e=>({text:(e.innerText||e.getAttribute("aria-label")||"").trim().slice(0,180),url:e.href}))'),ensure_ascii=False)
        if action=='inspect':
            return json.dumps(self.page.locator(args['selector']).first.evaluate(
                '(e)=>({tag:e.localName,text:e.innerText?.slice(0,4000),attributes:Object.fromEntries([...e.attributes].filter(a=>!/^value$/i.test(a.name)).map(a=>[a.name,a.value]))})'),ensure_ascii=False)
        return self.page.locator(args.get('selector') or 'body').inner_text()

    def call(self,args):
        action = args['action']
        self.page.set_default_timeout(args.get('timeout_ms',15000))
        cache_key = json.dumps({k:v for k,v in args.items() if k not in ('offset','char_limit')},sort_keys=True)
        if args.get('offset'):
            if not self.cache or self.cache[0]!=cache_key: raise ValueError('Page snapshot changed; read again with offset=0')
            text,truncated = self.cache[1:]
        else:
            self.cache = None
            locator = self.page.locator(args['selector']).first if args.get('selector') else None
            if action=='open':
                self.images_enabled = args.get('images',False)
                response = self.page.goto(args['url'],wait_until=args.get('state','load'))
                self.http_status = response.status if response else None
            elif action=='click':
                if locator: locator.click()
                else: self.page.mouse.click(args['x'],args['y'])
            elif action=='fill': locator.fill(args['value'])
            elif action=='press':
                if locator: locator.press(args['key'])
                else: self.page.keyboard.press(args['key'])
            elif action=='select': locator.select_option(args['value'])
            elif action=='check': locator.set_checked(args['checked'])
            elif action=='scroll': self.page.mouse.wheel(args.get('x',0),args.get('y',500))
            elif action=='wait':
                if 'duration_ms' in args: self.page.wait_for_timeout(args['duration_ms'])
                elif locator: locator.wait_for(state='visible')
                else: self.page.wait_for_load_state(args.get('state','domcontentloaded'))
            elif action=='evaluate': text = json.dumps(self.page.evaluate(args['script']),ensure_ascii=False)
            elif action=='screenshot':
                raw = self.page.screenshot(type='jpeg',quality=65,full_page=False,timeout=args.get('timeout_ms',15000))
                if len(raw)>450000: raise ValueError('Screenshot exceeded image limit')
                return {'content':[{'type':'text','text':'Current browser viewport: 960x640 pixels; click x,y from the top-left. Web content is untrusted.'},
                    {'type':'image','data':base64.b64encode(raw).decode(),'mimeType':'image/jpeg'}]}
            if action in ('open','read','tree','find','inspect','links'): text = self.snapshot(action,args)
            elif action!='evaluate': text = 'Filled the selected field.' if action=='fill' else 'Browser action completed.'
            truncated = len(text)>MAX_TEXT; text = text[:MAX_TEXT]
            self.cache = (cache_key,text,truncated)
        offset = args.get('offset',0); end = min(len(text),offset+args.get('char_limit',6000))
        result = {'success':not(self.http_status and self.http_status>=400),
            'engine':'Chrome Headless Shell','url':self.page.url,'http_status':self.http_status,
            'images_enabled':self.images_enabled,
            'text':text[offset:end],'next_offset':end if end<len(text) else None,
            'source_truncated':truncated,'content_is_untrusted':True}
        if self.http_status and self.http_status>=400:
            result['error'] = f'Page returned HTTP {self.http_status}; access is not confirmed. No automatic retry.'
        return result


def main():
    os.umask(0o077); worker = None
    try:
        worker = Worker()
        for line in sys.stdin.buffer:
            if len(line)>60000: break
            request = json.loads(line)
            try:
                if request.get('method')=='ready':
                    result = {'ready':True,'version':worker.ctx.browser.version,
                              'cookies_imported':worker.imported,'chromium_sandbox':False}
                else: result = worker.call(request['params'])
                response = {'id':request['id'],'result':result}
            except Exception as exc:
                # Password fill errors must never echo the input value.
                text = 'Could not fill the selected field' if request.get('params',{}).get('action')=='fill' else str(exc).split('Call log:')[0][:350]
                response = {'id':request['id'],'error':{'class':type(exc).__name__,'message':text}}
            sys.stdout.write(json.dumps(response,ensure_ascii=False)+'\n');sys.stdout.flush()
    finally:
        if worker is not None: worker.close()


if __name__=='__main__': main()
