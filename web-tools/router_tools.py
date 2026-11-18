"""Router bridge for the bounded local web worker; no inference or credential use."""
import json
import socket

NAMES = frozenset(('internet_search', 'web_read', 'web_image', 'browser'))


def rpc(request):
    data = (json.dumps(request, separators=(',', ':')) + '\n').encode()
    if len(data) > 32768:
        raise ValueError('Web request exceeds its size limit')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(90)
        connection.connect('/run/evo-web-tools.sock')
        connection.sendall(data)
        line = connection.makefile('rb').readline(9 * 1024 * 1024 + 1)
    if len(line) > 9 * 1024 * 1024 or not line.endswith(b'\n'):
        raise ValueError('Invalid web worker response')
    result = json.loads(line)
    if not isinstance(result, dict):
        raise ValueError('Invalid web worker response')
    return result


def list_tools():
    result = rpc({'operation': 'list'})
    tools = result.get('tools')
    if not isinstance(tools, list) or {t.get('tool') for t in tools} != NAMES:
        raise ValueError('Invalid web tool registry')
    return tools


def call(request):
    if request.get('tool') not in NAMES:
        raise ValueError('Unknown web tool')
    return rpc({'tool': request['tool'], 'params': request.get('params'), 'session': request.get('session')})


def cleanup(reset=False, session=None):
    return rpc({'operation': 'cleanup', 'reset': bool(reset), 'session': session})
