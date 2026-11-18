"""Anonymous public browser; reuse Discord's DOM/actions, never its profile."""
import contextlib
import json
from pathlib import Path
import sys

from browser_worker import Worker
sys.path.insert(0, str(Path(__file__).resolve().parent/'web-code'))
import evo_web as web


class AnonymousWorker(Worker):
    def __init__(self):
        from playwright.sync_api import sync_playwright
        from camoufox.sync_api import NewBrowser
        from camoufox.addons import DefaultAddons
        self.pw = sync_playwright().start()
        with contextlib.redirect_stdout(sys.stderr):
            self.browser = NewBrowser(self.pw, headless=True, os='linux', locale='en-US',
                window=(960, 640), exclude_addons=list(DefaultAddons),
                firefox_user_prefs={'browser.cache.disk.enable': False,
                                    'places.history.enabled': False})
        self.ctx = self.browser.new_context(viewport={'width': 960, 'height': 640},
            service_workers='block', accept_downloads=False)
        self.images_enabled = False
        self.ctx.route('**/*', self.route)
        self.ctx.route_web_socket('**/*', lambda socket: socket.close())
        self.page = self.ctx.new_page()
        self.page.on('dialog', lambda dialog: dialog.dismiss())
        self.page.on('response', self.navigation_response)
        self.cache = self.http_status = None

    def route(self, route):
        try:
            web.validate_url(route.request.url)
        except ValueError:
            return route.abort()
        return super().route(route)

    def close(self):
        try: self.browser.close()
        finally: self.pw.stop()


def main():
    worker = None
    try:
        for line in sys.stdin.buffer:
            if len(line) > 60000: break
            request = json.loads(line)
            try:
                if request['method'] == 'ready':
                    result = {'ready': True, 'anonymous': True, 'engine': 'Camoufox'}
                elif request['method'] == 'call':
                    args = request['params']
                    if args.get('action') == 'open': web.validate_url(args['url'])
                    if worker is None: worker = AnonymousWorker()
                    result = worker.call(args)
                else:
                    raise ValueError('Unknown anonymous browser method')
                response = {'id': request['id'], 'result': result}
            except Exception as error:
                # Never echo typed values, URLs or page contents in error messages.
                response = {'id': request['id'], 'error': type(error).__name__}
            print(json.dumps(response, ensure_ascii=False), flush=True)
    finally:
        if worker: worker.close()


if __name__ == '__main__': main()
