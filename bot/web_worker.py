"""Private public-web stdio worker. No owner profile or cookies are loaded."""
import json
import contextlib
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parent/'web-code'))
import evo_web as web


class Renderer:
    def __init__(self): self.pw = self.browser = None
    def render(self,url):
        web.validate_url(url)
        if self.pw is None:
            from playwright.sync_api import sync_playwright
            from camoufox.sync_api import NewBrowser
            from camoufox.addons import DefaultAddons
            self.pw = sync_playwright().start()
            with contextlib.redirect_stdout(sys.stderr):
                self.browser = NewBrowser(self.pw,headless=True,os='linux',
                    locale='en-US',exclude_addons=list(DefaultAddons),
                    firefox_user_prefs={'browser.cache.disk.enable':False})
        context = self.browser.new_context(service_workers='block',accept_downloads=False)
        def route(r):
            try:
                if r.request.resource_type in ('image','media','font'): return r.abort()
                web.validate_url(r.request.url)
            except ValueError: return r.abort()
            r.continue_()
        try:
            context.route('**/*',route)
            page = context.new_page()
            response = page.goto(url,wait_until='domcontentloaded',timeout=20000)
            page.wait_for_timeout(1000)
            web.validate_url(page.url)
            if response and response.status>=400: raise ValueError('Renderer HTTP '+str(response.status))
            text = page.content()
            if len(text.encode())>web.MAX_BYTES: raise ValueError('Rendered response exceeded 2 MiB')
            return page.url,text
        finally: context.close()
    def close(self):
        if self.browser: self.browser.close()
        if self.pw: self.pw.stop()


def main():
    render = Renderer(); web.render_public = render.render
    methods = {'internet_search':web.search,'web_read':web.read_page,'web_image':web.view_image}
    try:
        for line in sys.stdin.buffer:
            if len(line)>60000: break
            request = json.loads(line)
            try:
                result = {'ready':True,'version':'public-web','engine':'Camoufox'} if request['method']=='ready' \
                    else methods[request['method']](request['params'])
                reply = {'id':request['id'],'result':result}
            except Exception as exc:
                reply = {'id':request['id'],'error':{'message':type(exc).__name__}}
            print(json.dumps(reply,ensure_ascii=False),flush=True)
    finally: render.close()


if __name__ == '__main__': main()
