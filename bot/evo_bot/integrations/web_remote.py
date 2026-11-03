"""Keep the existing web contract; run its public, anonymous worker over SSH."""
import json
from collections import OrderedDict
from . import browser


class WebClient(browser.Browser):
    def call(self,name,args,turn):
        with self.lock:
            launch = browser.remote_launch(web=True)
            if not launch: return None
            if not self.running():
                self.close()
                argv,env,self.instance = launch
                self.launch(argv,env,turn)
            return self.request(name,args,turn,95)


CLIENT = WebClient()
REFS = OrderedDict()


def call(name,args,turn):
    # References stay on the server which issued them, including local fallback.
    ref = args.get('ref')
    source = REFS.get(ref)
    if source == 'localhost': return None
    try:
        result = CLIENT.call(name,args,turn)
        if not ref and result is not None:
            try: value = json.loads(result) if isinstance(result,str) else result
            except ValueError: value = None
            if isinstance(value,dict) and value.get('success') is False: return None
        return result
    except InterruptedError:
        CLIENT.close(); raise
    except (OSError,TimeoutError,json.JSONDecodeError):
        CLIENT.close()
        if source:
            raise ConnectionError('Remote page reference unavailable; reopen its original URL') from None
        return None


def remember(result,instance):
    try: value = json.loads(result) if isinstance(result,str) else result
    except (ValueError,TypeError): return result
    def visit(node):
        if isinstance(node,dict):
            if isinstance(node.get('ref'),str): REFS[node['ref']] = instance
            for child in node.values(): visit(child)
        elif isinstance(node,list):
            for child in node: visit(child)
    visit(value)
    while len(REFS)>1024: REFS.popitem(last=False)
    if isinstance(value,dict): value['execution_instance'] = instance
    return json.dumps(value,ensure_ascii=False,separators=(',',':')) if isinstance(result,str) else value
