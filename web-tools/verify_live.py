"""Exercise deployed WebUI HTTP and the OMP Registry without sending Discord messages."""
import asyncio
import json
from pathlib import Path
import sys
import time
import urllib.request

sys.path.insert(0, '/opt/evo-omp')
from evo_omp import context
from evo_omp.tools import Registry, http_json


def memory():
    mem = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
    vm = dict(line.split() for line in Path('/proc/vmstat').read_text().splitlines())
    return {'available_kib': int(mem['MemAvailable'].split()[0]), 'pswpout': int(vm['pswpout'])}


async def main():
    output = Path(sys.argv[1]); output.mkdir(parents=True, exist_ok=True)
    registry = Registry(); await registry.initialize_web()
    turn = context.Turn('990826359704154225')
    rows, before = [], memory()
    async def call(name, args):
        started = time.monotonic()
        raw = await registry.call(name, args, turn)
        result = json.loads(raw)
        assert result['success'], result
        rows.append({'tool': name, 'action': args.get('action', 'read'),
                     'chars': len(raw), 'bytes': len(raw.encode()),
                     'seconds': round(time.monotonic() - started, 4),
                     'next_offset': result.get('next_offset'),
                     'next': result.get('next'), 'matches': len(result.get('matches', []))})
        return result
    search = await call('internet_search', {'query': 'site:docs.python.org TaskGroup', 'engine': 'local'})
    assert search['returned'] <= 5
    more = await call('internet_search', {'query': 'site:docs.python.org TaskGroup', 'engine': 'local', **search['next']})
    assert search['data']['web'][0]['url'] != more['data']['web'][0]['url']
    url = 'https://docs.python.org/3/library/asyncio-task.html'
    page = await call('web_read', {'url': url, 'backend': 'local'})
    follow = {'ref': page['ref'], 'backend': 'local', 'version': page['version']}
    later = await call('web_read', dict(follow, offset=page['next_offset']))
    assert later['offset'] == page['next_offset']
    found = await call('web_read', dict(follow, action='find', query='KeyboardInterrupt'))
    assert found['matches'] and 'KeyboardInterrupt' in str(found['matches'])
    links = await call('web_read', dict(follow, action='links'))
    assert links['links'] and len(links['links']) <= 6
    oversized = await call('web_read', {'url': url, 'backend': 'local', 'char_limit': 32000})
    assert len(oversized['content']) <= 6000 and oversized.get('next_offset')
    bad = json.loads(await registry.call('web_read', dict(follow, version='000000000000'), turn))
    assert not bad['success'] and 'changed' in bad['error']
    # This is the HTTP route used by WebUI; same worker and output budgets.
    direct = http_json('/tools', {'tool': 'web_read', 'params': {'url': url, 'backend': 'local'}})
    assert json.loads(direct['plain_text'])['version'] == page['version']
    schema = registry.tools['web_read'][0]['parameters']['properties']
    assert schema['char_limit']['default'] == 2400 and 'version' in schema
    after = memory()
    result = {'passed': True, 'path': 'OMP Registry -> router /tools -> web worker; WebUI /tools',
              'discord_message_sent': False, 'schema_loaded': True, 'calls': rows,
              'changed_version_rejected': True, 'webui_matches_omp': True,
              'before': before, 'after': after, 'swapout_delta_pages': after['pswpout'] - before['pswpout']}
    (output / 'live-verification.json').write_text(json.dumps(result, indent=2))
    print(json.dumps(result), flush=True)


if __name__ == '__main__': asyncio.run(main())
