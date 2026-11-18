"""Public Web UI tools; no Discord owner profile or account access."""
import atexit
import importlib.util
import json
import os
from pathlib import Path
import select
import socket
import sys
import time
from evo_web_browser import Browsers, DEFINITION

MAX_REQUEST = 32768
MAX_RESPONSE = 9 * 1024 * 1024
IDLE_SECONDS = 600
sys.path.insert(0, '/opt/evo-web-deps/src')
spec = importlib.util.spec_from_file_location('evo_public_web',
    '/opt/evo-web-tools/evo-web.py')
web = importlib.util.module_from_spec(spec)
spec.loader.exec_module(web)


class Registry:
    def __init__(self):
        self.tools = []
        self.handlers = {}

    def register_tool(self, name, toolset, definition, handler):
        if name not in ('internet_search', 'web_read', 'web_image', 'browser'):
            raise ValueError('Unexpected web tool')
        self.tools.append({'tool': name, 'uses_cwd': False,
                           'definition': {'type': 'function', 'function': definition}})
        self.handlers[name] = handler


registry = Registry()
web.register(registry)
browsers = Browsers()
atexit.register(browsers.close)
registry.register_tool('browser', 'evo_browser', DEFINITION, None)


def dispatch(request):
    if not isinstance(request, dict):
        raise ValueError('Send a JSON object')
    if request.get('operation') == 'list':
        return {'tools': registry.tools}
    if request.get('operation') == 'cleanup':
        web.clear_cache(clear_refs=request.get('reset') is True)
        browsers.cleanup(request.get('session'))
        return {'success': True}
    name, params = request.get('tool'), request.get('params')
    if name not in registry.handlers or not isinstance(params, dict):
        raise ValueError('Unknown tool or invalid parameters')
    # Preserve the existing plugin's bounded result and source references verbatim.
    result = browsers.call(params, request.get('session')) if name == 'browser' else registry.handlers[name](params)
    if isinstance(result, dict) and (result.get('_multimodal') is True or 'content' in result):
        # The native UI extracts standalone data URLs into image attachments.
        parts = result['content']
        return {'plain_text': '\n'.join(part['text'] if part['type'] == 'text' else
            'data:' + part['mimeType'] + ';base64,' + part['data'] if part['type'] == 'image'
            else part['image_url']['url'] for part in parts)}
    return {'plain_text': result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, separators=(',', ':'))}


def serve():
    listener = socket.socket(fileno=os.dup(0))
    last_request = time.monotonic()
    while time.monotonic() - last_request < IDLE_SECONDS:
        browsers.cleanup()
        readable, _, _ = select.select([listener], [], [], 15)
        if not readable:
            continue
        connection, _ = listener.accept()
        last_request = time.monotonic()
        with connection:
            connection.settimeout(10)
            request = {}
            try:
                line = connection.makefile('rb').readline(MAX_REQUEST + 1)
                if len(line) > MAX_REQUEST or not line.endswith(b'\n'):
                    raise ValueError('Web request exceeds its size limit')
                request = json.loads(line)
                result = dispatch(request)
            except Exception as error:
                result = {'error': str(error) if isinstance(request, dict) and request.get('tool') == 'browser' and isinstance(error, (ValueError, TimeoutError, ConnectionError))
                          else 'Web tool failed: ' + type(error).__name__}
            encoded = (json.dumps(result, ensure_ascii=False, separators=(',', ':')) + '\n').encode()
            if len(encoded) > MAX_RESPONSE:
                encoded = b'{"error":"Web result exceeds its size limit"}\n'
            try:
                connection.sendall(encoded)
            except OSError:
                pass
            last_request = time.monotonic()
    listener.close()


if __name__ == '__main__':
    serve()
