"""Exercise registry shutdown with real Windows/Oracle backend classes, no network."""
import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from evo_bot import tools
from evo_bot.integrations import codex


async def main():
    with tempfile.TemporaryDirectory(prefix='evo-codex-shutdown-') as directory, \
            patch.object(codex, 'STATE', Path(directory) / 'codex-jobs.json'):
        desktop = codex.Bridge({}, 'desktop')
        oracle = codex.AppServer({}, 'oracle')
        view = Mock()
        desktop.new_chat_views['test'] = view
        started = asyncio.Event()

        async def background():
            started.set()
            await asyncio.Event().wait()

        monitor = asyncio.create_task(background())
        await started.wait()
        async def read():
            try:
                await asyncio.Event().wait()
            finally:
                # The real receive() detaches its socket on cancellation too.
                desktop.ws = None
                desktop.initialized = False

        reader = asyncio.create_task(read())
        desktop.desktop_monitors['test'] = monitor
        desktop.reader = reader
        socket = SimpleNamespace(close=AsyncMock())
        desktop.ws = socket
        desktop.initialized = True
        await asyncio.sleep(0)
        registry = tools.Registry.__new__(tools.Registry)
        with patch.dict(codex.SERVERS, {'desktop': desktop, 'oracle': oracle}, clear=True), \
                patch.object(tools.browser, 'close') as browser_close, \
                patch.object(tools.web_remote.CLIENT, 'close') as web_close, \
                patch.object(tools.rdp, 'close') as rdp_close, \
                patch.object(desktop, 'rpc', AsyncMock()) as rpc, \
                patch.object(oracle, '_rpc') as ssh_rpc:
            await asyncio.wait_for(registry.close(), timeout=2)
            assert monitor.cancelled() and reader.cancelled()
            assert not desktop.desktop_monitors and not desktop.new_chat_views
            assert desktop.ws is None and desktop.reader is None and not desktop.initialized
            socket.close.assert_awaited_once()
            view.stop.assert_called_once()
            browser_close.assert_called_once()
            web_close.assert_called_once()
            rdp_close.assert_called_once()
            rpc.assert_not_called()
            ssh_rpc.assert_not_called()
            await asyncio.wait_for(desktop.close(), timeout=1)
            await asyncio.wait_for(oracle.close(), timeout=1)
            socket.close.assert_awaited_once()
        print('PASS mixed Windows/Oracle registry cleanup, idempotent disconnect, '
              'monitors/readers/views released, remote turns untouched; no network')


if __name__ == '__main__':
    asyncio.run(main())
