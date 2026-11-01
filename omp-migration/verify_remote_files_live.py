"""Actual OMP deferred-tool -> SSH file workflow; temporary fixture, no Discord posts.

Run as bot user: verify_remote_files_live.py REPORT_DIR [SSH_INSTANCE]
Uses the configured private login; no model inference or credential output.
"""
import asyncio
import hashlib
import json
from pathlib import Path
import shlex
import shutil
import sys
import tempfile
import threading
import time

from evo_omp import context, rpc
from evo_omp.integrations import remote_files
from evo_omp.tools import Registry
from verify_context_payload import Handler, ThreadingHTTPServer, REQUESTS, STEPS


async def main():
    report = Path(sys.argv[1]); report.mkdir(parents=True, exist_ok=True)
    instance = sys.argv[2] if len(sys.argv) > 2 else 'n100'
    registry = Registry(); await registry.initialize_web()
    turn = context.Turn('990826359704154225')
    token = context.current.set(turn)
    calls = []
    async def dispatch(name, args, current=turn):
        value = await registry.call(name, args, current)
        value = json.loads(value) if isinstance(value, str) else value
        if isinstance(value, dict) and value.get('error'):
            raise AssertionError(value['error'])
        calls.append((name, args, value))
        return value
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    config = Path(tempfile.mkdtemp(prefix='remote-files-rpc-', dir=rpc.RUNTIME))
    local = Path(tempfile.mkdtemp(prefix='.remote-files-check-', dir=remote_files.WORKSPACE))
    session = None; remote = None
    started = time.monotonic()
    try:
        created = await dispatch('shell_exec', {'instance': instance,
            'cmd': 'mktemp -d /tmp/evo-files-XXXXXXXXXX'})
        assert created['exit_code'] == 0, created
        remote = created['output'].strip()
        assert remote.startswith('/tmp/evo-files-') and len(remote.split('/')) == 3, remote
        source = b'def add(a, b):\n    return a - b\n\nassert add(2, 3) == 5\nprint("REMOTE_FILES_OK 5")\n'
        (local/'source.py').write_bytes(source)
        path = remote + '/code.py'
        def file_args(action, **kw):
            return dict(instance=instance, action=action, path=path, **kw)
        def tool(name, args):
            return ('write', {'path': 'xd://' + name, 'content': json.dumps(args), 'i': 'Private SSH file fixture'})
        STEPS.extend([
            ('read', {'path': 'xd://remote_files', 'i': 'Read remote file schema'}),
            tool('remote_files', file_args('upload', local_path=str(local/'source.py'))),
            tool('remote_files', file_args('read')),
            tool('remote_files', file_args('edit', old_string='return a - b', new_string='return a + b')),
            tool('remote_files', dict(instance=instance, action='grep', path=remote, pattern=r'return a \+ b')),
            tool('remote_files', dict(instance=instance, action='glob', path=remote, pattern='*.py')),
            ('read', {'path': 'xd://shell_exec', 'i': 'Read execution schema'}),
            tool('shell_exec', {'instance': instance, 'cmd': 'python3 ' + shlex.quote(path)}),
            tool('remote_files', file_args('download', local_path=str(local/'back.py'))),
        ])
        (config/'models.yml').write_text((rpc.ROOT/'models.yml').read_text().replace(
            ':8080/v1', f':{server.server_port}/v1'))
        shutil.copyfile(rpc.ROOT/'config.yml', config/'config.yml')
        session = rpc.OmpSession('remote-files-fixture', 'sc117-flash-next-iq3_xxs',
                                 registry.definitions(), dispatch, config)
        await session.start()
        answer = await asyncio.wait_for(session.prompt('Private remote file fixture only.', turn), 180)
        assert answer == 'VERIFIED_CONTEXT', answer
        names = {t['function']['name'] for t in REQUESTS[0]['tools']}
        assert len(names) == 12 and 'remote_files' not in names, names
        results = [m for m in REQUESTS[-1]['messages'] if m['role'] == 'tool']
        assert len(results) == 9 and not any(m.get('isError') for m in results), results
        assert 'remote_files' in str(results[0]['content'])
        assert 'REMOTE_FILES_OK 5' in str(results[7]['content'])
        assert len(calls) == 8, len(calls)
        fixed = source.replace(b'return a - b', b'return a + b')
        assert (local/'back.py').read_bytes() == fixed
        assert 'code.py' in calls[5][2]['matches']
        file_results = [r for n, _, r in calls if n == 'remote_files']
        assert not any('_version' in r or 'data' in r for r in file_results)
        assert calls[-1][2] == {'bytes': len(fixed), 'local_path':
            '/workspace/' + str((local/'back.py').relative_to(remote_files.WORKSPACE))}
        # Changing a previously read file through another path must reject a stale edit.
        await dispatch('shell_exec', {'instance': instance, 'cmd': 'printf "external change\\n" > ' + shlex.quote(path)})
        stale = await registry.call('remote_files', file_args('edit', old_string='return a + b', new_string='wrong'), turn)
        assert 'Read' in stale.get('error', ''), stale
        assert not any(d['name'] == 'remote_files' for d in registry.definitions(cron=True))
        summary = {'passed': True, 'instance': instance, 'real_omp_bwrap': True,
            'real_ssh': True, 'model_inference': False, 'discord_posts': 0,
            'direct_tools': len(names), 'deferred_schema_and_dispatch': True,
            'actions': ['upload', 'read', 'edit', 'grep', 'glob', 'download'],
            'build_test_output': 'REMOTE_FILES_OK 5', 'stale_edit_rejected': True,
            'transfer_sha256': hashlib.sha256(fixed).hexdigest(), 'transfer_bytes': len(fixed),
            'raw_bytes_or_version_in_model_results': False, 'seconds': round(time.monotonic()-started, 2)}
        (report/'remote-files-live.json').write_text(json.dumps(summary, indent=2)+'\n')
        print(json.dumps(summary), flush=True)
    finally:
        if session: await session.close()
        if remote:
            # Exact temp directory created above, never a model-supplied deletion target.
            cleanup = 'import pathlib,shutil; p=pathlib.Path(' + repr(remote) + '); '
            cleanup += 'assert p.parent==pathlib.Path("/tmp") and p.name.startswith("evo-files-"); shutil.rmtree(p)'
            await dispatch('shell_exec', {'instance': instance, 'cmd': 'python3 -c ' + shlex.quote(cleanup)})
        context.current.reset(token)
        server.shutdown(); server.server_close()
        shutil.rmtree(config); shutil.rmtree(local)


if __name__ == '__main__': asyncio.run(main())
