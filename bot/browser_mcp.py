"""One MCP tool, backed by the same private browser as the EVO Discord bot."""
import asyncio
import json
from pathlib import Path

from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp import types

ROOT = Path(__file__).resolve().parent
SERVER = Server('evo-browser', version='1')
TOOL = types.Tool.model_validate(json.loads((ROOT/'browser-tool.json').read_text()))


class Connection:
    def __init__(self):
        self.reader = self.writer = None
        self.serial = 0
        self.lock = asyncio.Lock()

    def close(self):
        if self.writer: self.writer.close()
        self.reader = self.writer = None

    async def request(self, method, params):
        if self.writer is None:
            self.reader, self.writer = await asyncio.open_unix_connection(str(ROOT/'.browser.sock'), limit=4*2**20)
        self.serial += 1
        raw = (json.dumps({'id': self.serial, 'method': method, 'params': params}, ensure_ascii=False)+'\n').encode()
        if len(raw) > 60000: raise ValueError('Browser request is too large')
        self.writer.write(raw); await self.writer.drain()
        response = json.loads(await asyncio.wait_for(self.reader.readline(), 95))
        if response.get('id') != self.serial or response.get('error'):
            raise RuntimeError('Shared browser operation not confirmed. Inspect status; no automatic retry.')
        return response['result']


CONNECTION = Connection()


@SERVER.list_tools()
async def list_tools():
    return [TOOL]


@SERVER.call_tool()
async def call_tool(name, arguments):
    if name != 'browser': raise ValueError('Unknown tool')
    async with CONNECTION.lock:
        try:
            args = arguments or {}
            if args.get('share'): raise ValueError('share is a Discord-only option')
            if args['action'] == 'close':
                CONNECTION.close()
                return types.CallToolResult(content=[types.TextContent(type='text',
                    text='{"success":true,"closed":true,"profile_kept":true}')])
            if CONNECTION.writer is None and args['action'] not in ('status', 'close', 'reset'):
                await CONNECTION.request('ready', {})
            result = await CONNECTION.request('call', args)
            if 'content' in result: return types.CallToolResult.model_validate(result)
            return types.CallToolResult(content=[types.TextContent(type='text', text=json.dumps(result, ensure_ascii=False))],
                                        isError=result.get('success') is False)
        except BaseException:
            CONNECTION.close()
            raise


async def main():
    try:
        async with stdio_server() as (read, write):
            await SERVER.run(read, write, SERVER.create_initialization_options())
    finally: CONNECTION.close()


if __name__ == '__main__': asyncio.run(main())
