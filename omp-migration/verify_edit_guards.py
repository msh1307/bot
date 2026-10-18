"""Exercise native replacement guards without a live model."""
import asyncio
import json
from pathlib import Path
import shutil
import tempfile
import threading

from evo_omp import context
from evo_omp.rpc import OmpSession, WORKSPACE
from verify_native_tools import Handler, REQUESTS, STEPS, ThreadingHTTPServer, text_of


async def main():
    run=Path(__file__).resolve().parent
    cfg=Path(tempfile.mkdtemp(prefix='edit-guard-config-',dir='/run/evo-omp'))
    (cfg/'models.yml').write_text((run/'models.yml').read_text().replace(':8080/v1',':18089/v1'))
    (cfg/'config.yml').write_text((run/'config.yml').read_text())
    fixture=Path(tempfile.mkdtemp(prefix='.omp-edit-guards-',dir=WORKSPACE))
    target=fixture/'guard.py'
    path='/workspace/'+fixture.name+'/guard.py'
    target.write_text('value = 1\n')
    server=ThreadingHTTPServer(('127.0.0.1',18089),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    session=OmpSession('edit-guards','sc117-flash-next-iq3_xxs',[],None,cfg)
    async def one(steps):
        STEPS.extend(steps)
        await session.prompt('Verify native edit guards.',context.Turn('edit-guards'))
        return text_of(next(m for m in reversed(REQUESTS[-1]['messages']) if m['role']=='tool'))
    try:
        await session.start()
        def stale(_data):
            target.write_text('value = 9\n')
            return ('edit',{'path':path,'old_string':'value = 1','new_string':'value = 2'})
        result=await one([('read',{'path':path}),stale])
        assert target.read_text()=='value = 9\n',result
        assert any(s in result.lower() for s in ('not find','not found','no match')),result
        target.write_text('left = 1\nright = 1\n')
        result=await one([('edit',{'path':path,'old_string':'= 1','new_string':'= 2'})])
        assert target.read_text()=='left = 1\nright = 1\n',result
        assert any(s in result.lower() for s in ('multiple','ambiguous','occurrence')),result
        result=await one([('read',{'path':path}),
            ('edit',{'path':path,'old_string':'right = 1','new_string':'right = 2'})])
        assert target.read_text()=='left = 1\nright = 2\n',result
        print('PASS stale target and ambiguous matches rejected; exact scoped edit succeeds',flush=True)
    finally:
        await session.close()
        assert fixture.parent.resolve()==WORKSPACE.resolve() and fixture.name.startswith('.omp-edit-guards-')
        shutil.rmtree(fixture)
        shutil.rmtree(cfg)
        server.shutdown()


if __name__=='__main__':
    asyncio.run(main())
