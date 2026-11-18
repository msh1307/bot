"""Exercise Registry -> terminal-use -> sandbox/SSH without inference or Discord messages."""
import asyncio
import json
from pathlib import Path
import re
import subprocess
import sys

from evo_bot import context
from evo_bot.integrations import terminal
from evo_bot.tools import Registry


async def verify(instances):
    registry = Registry()
    turn = context.Turn('123')
    token = context.current.set(turn)
    records = []
    async def call(instance, action, **args):
        result = await registry.call('remote_control', {'instance': instance, 'protocol': 'terminal',
                                    'action': action, 'args': args}, turn)
        return '\n'.join(b.get('text', '') for b in result.get('content', []))
    def check(name, value):
        if not value:
            raise AssertionError(name)
        records.append(name)
    try:
        for instance in instances:
            client = terminal.client(instance)
            help_args = {'instance': instance, 'protocol': 'terminal',
                         'action': 'help', 'args': {'tool': 'read'}}
            schema = await registry.call('remote_control', help_args, turn)
            check(instance + ': schema-only help releases process',
                  schema['properties']['sessionId']['type'] == 'string'
                  and not client.running() and client.process is None and not client.sessions)
            again = await registry.call('remote_control', help_args, turn)
            check(instance + ': cached help needs no process', again == schema and client.process is None)
            command = ('test -t 0 || exit 3; printf "PTY_OK\\n"; read -r value; '
                       'printf "VALUE:%s\\n" "$value"; sleep 1; '
                       'for n in $(seq 1 120); do printf "LOG-%03d abcdefghijklmnopqrstuvwxyz\\n" "$n"; done; '
                       'printf "DONE\\n"')
            created = await call(instance, 'create', command=command)
            handle = re.search(r'Use sessionId="([a-f0-9-]+)"', created).group(1)
            pid = client.process.pid
            await registry.call('remote_control', help_args, turn)
            check(instance + ': help preserves active session', client.running()
                  and client.process.pid == pid and handle in client.sessions)
            listed = await call(instance, 'list')
            check(instance + ': list includes elapsed time', 'elapsed=' in listed and handle in listed)
            ready = await call(instance, 'wait', sessionId=handle, pattern='PTY_OK', timeoutMs=5000)
            check(instance + ': PTY ready', 'matched after' in ready)
            await call(instance, 'type', sessionId=handle, text='hello\n')
            done = await call(instance, 'wait', sessionId=handle, timeoutMs=5000)
            check(instance + ': input and exit status', 'exit code 0' in done and 'DONE' in done)
            small = await call(instance, 'read', sessionId=handle, rows=8, cursor=False, highlights=False)
            check(instance + ': bounded screen read', 'DONE' in small and 'LOG-001' not in small)
            await call(instance, 'destroy', sessionId=handle)
            created = await call(instance, 'create')
            handle = re.search(r'Use sessionId="([a-f0-9-]+)"', created).group(1)
            await call(instance, 'type', sessionId=handle, text='export EVO_TTY_CHECK=alive\n')
            result = await call(instance, 'batch', sessionId=handle, actions=[
                {'type': 'type', 'text': 'printf "STATE:%s\\n" "$EVO_TTY_CHECK"\n'},
                {'type': 'wait', 'pattern': '^STATE:alive$', 'timeoutMs': 5000}])
            check(instance + ': shell state and batch', 'STATE:alive' in result)
            if instance == 'localhost':
                import time
                started = time.monotonic()
                result = await call(instance, 'batch', sessionId=handle, actions=[
                    {'type': 'wait', 'ms': 16000} for _ in range(3)])
                elapsed = time.monotonic() - started
                check('localhost: batch exceeds old 45s transport limit', 'Ran all 3 actions' in result and elapsed >= 48)
                print(json.dumps({'long_batch_elapsed_seconds': round(elapsed, 2)}), flush=True)
            await call(instance, 'type', sessionId=handle, text='sleep 30\n')
            await call(instance, 'press', sessionId=handle, key='Ctrl+C')
            waited = await call(instance, 'wait', sessionId=handle, timeoutMs=3000)
            check(instance + ': interrupt', 'Timed out' not in waited)
            if instance != 'localhost':
                check(instance + ': SSH quiet never claims command completion', 'Command finished:' not in waited)
            processes = [line.split(None, 3) for line in subprocess.check_output(
                ['ps', '-eo', 'pid=,ppid=,rss=,comm='], text=True).splitlines()]
            tree = {terminal.CLIENTS[instance].process.pid}
            while True:
                children = {int(p[0]) for p in processes if int(p[1]) in tree}
                if children <= tree: break
                tree.update(children)
            print(json.dumps({'instance': instance, 'process_tree_rss_mib': round(sum(
                int(p[2]) for p in processes if int(p[0]) in tree) / 1024, 2),
                'short_read_bytes': len(small.encode()), 'sessionId': handle}), flush=True)
            await call(instance, 'destroy', sessionId=handle)
            try:
                await call(instance, 'read', sessionId=handle)
            except ValueError:
                check(instance + ': stale handle rejected', True)
            else:
                check(instance + ': stale handle rejected', False)
        print(json.dumps({'passed': len(records), 'checks': records}), flush=True)
    finally:
        terminal.close()
        context.current.reset(token)


if __name__ == '__main__':
    asyncio.run(verify(sys.argv[1:] or ['localhost']))
