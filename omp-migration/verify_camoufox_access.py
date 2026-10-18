"""One bounded same-IP access comparison. Never solves or retries a challenge."""
import json
import os
from pathlib import Path
import re
import time
from camoufox.addons import DefaultAddons
from camoufox.sync_api import Camoufox

out = Path(os.environ['OUT']);out.mkdir(mode=0o700,parents=True,exist_ok=True)
result = {'url':'https://www.g2.com/','started':time.time(),'account_cookies_used':False}
try:
    with Camoufox(headless=True,os='linux',locale='en-US',
        executable_path='/opt/evo-camoufox-trial/cache/camoufox/browsers/official/156.0.1-beta.36-72637885/camoufox-bin',
        exclude_addons=list(DefaultAddons),
        firefox_user_prefs={'browser.cache.disk.enable':False,'dom.ipc.processCount':2}) as b:
        page = b.new_page(viewport={'width':960,'height':640})
        page.route('**/*',lambda r:r.abort() if r.request.resource_type in ('image','media') else r.continue_())
        response = page.goto(result['url'],wait_until='domcontentloaded',timeout=30000)
        page.wait_for_timeout(5000)
        text = page.locator('body').inner_text(timeout=5000)
        result.update(http_status=response.status if response else None,title=page.title(),
            excerpt=text[:1200],webdriver=page.evaluate('navigator.webdriver'),
            challenge_detected=bool(re.search('Just a moment|verify you are human|Access denied|Attention Required|unusual traffic',text,re.I)))
except Exception as exc: result['error'] = type(exc).__name__+': '+str(exc)[:300]
result['ended'] = time.time()
(out/'result.json').write_text(json.dumps(result,indent=2))
print(json.dumps(result),flush=True)
