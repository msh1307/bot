"""Real Pi/bwrap and read-only SSH/Codex checks; local scripted provider, no Discord posts.

EVO bot user: verify_remote_rpc.py BASELINE_DEFINITIONS REPORT_DIR
The report retains counts/status only, never chat contents or credentials.
"""
import asyncio
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading
from types import SimpleNamespace

from evo_bot import context, rpc
from evo_bot.integrations import codex, remote
from evo_bot.tools import Registry
from verify_context_payload import Handler, ThreadingHTTPServer, REQUESTS, STEPS


async def main():
    report = Path(sys.argv[2]); report.mkdir(parents=True, exist_ok=True)
    baseline = json.loads(Path(sys.argv[1]).read_text())
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    cfg = Path(tempfile.mkdtemp(prefix='remote-audit-', dir=rpc.RUNTIME))
    (cfg / 'models.yml').write_text((rpc.ROOT / 'models.yml').read_text().replace(
        ':8080/v1', f':{server.server_port}/v1'))
    shutil.copyfile(rpc.ROOT / 'config.yml', cfg / 'config.yml')
    registry = Registry(); await registry.initialize_web()
    captured, results = {}, []
    async def host(name, args, turn):
        result = await registry.call(name, args, turn)
        results.append((args, result))
        return result
    default, settings = codex.server_config()
    codex.CLIENT = context.client = SimpleNamespace(is_ready=lambda: True)
    codex.LOOP = asyncio.get_running_loop()
    codex.SERVERS = {name: codex.Bridge(value, name, name == default) for name, value in settings.items()}
    codex.BRIDGE = codex.SERVERS[default]
    for bridge in codex.SERVERS.values(): bridge.jobs = {}  # No notifications for previous tasks.
    try:
        for variant, definitions in (('before', baseline), ('after', registry.definitions())):
            REQUESTS.clear(); STEPS.clear()
            session = rpc.PiSession('remote-audit', 'sc117-flash-next-iq3_xxs', definitions, host, cfg)
            try:
                await session.start()
                if variant == 'after':
                    STEPS.extend([('remote_control', {'action': 'help', 'protocol': 'codex'}),
                                  ('remote_control', {'action': 'list'}),
                                  ('shell_exec', {'instance': 'n100', 'cmd': 'id -un'}),
                                  ('remote_control', {'protocol': 'codex', 'action': 'list', 'args': {'limit': 1}})])
                answer = await asyncio.wait_for(session.prompt('Read-only remote integration check.',
                    context.Turn('990826359704154225')), 120)
                assert answer == 'VERIFIED_CONTEXT', answer
                tools = REQUESTS[0]['tools']
                captured[variant] = {'tools': len(tools), 'json_bytes': len(json.dumps(tools,
                    ensure_ascii=False, separators=(',', ':')).encode())}
                if variant == 'after':
                    names = {t['function']['name'] for t in tools}
                    assert 'remote_control' in names and not names & {
                        'remote_instance', 'remote_ssh', 'remote_desktop', 'codex_task', 'windows_files', 'windows_exec'}
                    calls = [c['id'] for m in REQUESTS[-1]['messages'] for c in m.get('tool_calls', [])]
                    outputs = [m for m in REQUESTS[-1]['messages'] if m['role'] == 'tool']
                    assert calls == [m['tool_call_id'] for m in outputs] and len(outputs) == 4
            finally: await session.close()
        shell = json.loads(results[2][1]) if isinstance(results[2][1], str) else results[2][1]
        chat = json.loads(results[3][1]) if isinstance(results[3][1], str) else results[3][1]
        assert shell['exit_code'] == 0 and shell['output'].strip() == 'msh', shell
        assert chat.get('success') is True and 'threads' in chat, chat.get('error', 'No thread list')
        result = {'passed': True, 'actual_omp_bwrap': True, 'model_inference': False, 'discord_posts': 0,
                  'rdp_connections': 0, 'tool_results_paired': 4, 'n100_ssh': shell['output'].strip(),
                  'codex_threads_returned': len(chat['threads']),
                  'codex_desktop_error': chat.get('desktop_error'), 'request_tool_schemas': captured}
        (report / 'remote-rpc.json').write_text(json.dumps(result, indent=2))
        print(json.dumps(result), flush=True)
    finally:
        for bridge in codex.SERVERS.values():
            if bridge.ws: await bridge.ws.close()
            if bridge.reader:
                bridge.reader.cancel(); await asyncio.gather(bridge.reader, return_exceptions=True)
        server.shutdown(); shutil.rmtree(cfg)


if __name__ == '__main__': asyncio.run(main())
