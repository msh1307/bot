"""Small paired real-model edit pilot. No Discord or privileged agent tools.

The root controller only switches the existing inference services. The actual
client runs as evo-hermes in the usual bwrap sandbox with read/edit only.
ExecStopPost --restore is required when launching the controller.
"""
import asyncio
import contextlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

RUN = Path(__file__).resolve().parent
LEASE = Path('/run/evo-router/maintenance.json')
CONTROLLER = 'evo-omp-edit-review'
CLIENT = 'evo-omp-edit-client'
FLASH = 'sc117-flash-next-iq3_xxs'
QWEN = 'qwen3.8-27b-uncensored'


def command(*args, check=True, timeout=240):
    return subprocess.run(args, capture_output=True, text=True, check=check, timeout=timeout)


def api(path, port=8080):
    with urllib.request.urlopen(f'http://127.0.0.1:{port}{path}', timeout=5) as r:
        return json.load(r)


def write(path, data):
    tmp = path.with_suffix('.new')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    os.replace(tmp, path)


def sample():
    mem = {s.split(':')[0]: int(s.split()[1])*1024 for s in Path('/proc/meminfo').read_text().splitlines()
           if len(s.split())>2 and s.split()[1].isdigit()}
    vm = dict(s.split() for s in Path('/proc/vmstat').read_text().splitlines())
    row = {'time': time.time(), 'available': mem['MemAvailable'], 'pswpout': int(vm['pswpout']),
           'pswpin': int(vm['pswpin']), 'oom': int(vm['oom_kill']), 'temps': {}}
    for h in Path('/sys/class/hwmon').glob('hwmon*'):
        name = (h/'name').read_text().strip()
        if name in ('k10temp', 'amdgpu', 'nvme'):
            row['temps'][name] = max(int(p.read_text()) for p in h.glob('temp*_input'))/1000
    return row


def ready(port):
    end = time.monotonic()+240
    while time.monotonic()<end:
        try:
            if api('/health',port).get('status')=='ok':
                return
        except Exception:
            pass
        time.sleep(1)
    raise TimeoutError(f'Backend {port} did not recover')


def restore():
    path = RUN/'edit-original.json'
    if not path.exists():
        return
    state = json.loads(path.read_text())
    write(LEASE, {'pid': os.getpid(), 'expires': time.time()+360, 'reason': 'Restoring inference after edit pilot'})
    command('systemctl','stop',CLIENT,check=False)
    command('systemctl','stop','evo-qwen',check=False)
    if state['flash_active']:
        command('systemctl','start','evo-flash')
        ready(8082)
    if state['bot_active']:
        command('systemctl','start','evo-omp')
    write(RUN/'edit-restored.json', {'restored':True,'sample':sample()})
    if LEASE.exists() and json.loads(LEASE.read_text()).get('pid')==os.getpid():
        LEASE.unlink()


def controller():
    router=api('/evo/status')
    bot=json.loads(Path('/run/evo-omp/status.json').read_text())
    if router['active']!=FLASH or any(router.get(k) for k in ('busy','loading','maintenance')) or bot['active'] or bot['queued']:
        raise RuntimeError('Production busy; edit review not admitted')
    with LEASE.open('x') as f:
        json.dump({'pid':os.getpid(),'expires':time.time()+1500,'reason':'Paired OMP edit contract review'}, f)
    if api('/evo/status').get('busy'):
        LEASE.unlink()
        raise RuntimeError('Request raced maintenance admission')
    write(RUN/'edit-original.json', {'flash_active':True,
        'bot_active': command('systemctl','is-active','evo-omp',check=False).stdout.strip()=='active'})
    baseline=sample()
    rows=[]
    stop=threading.Event()
    unsafe=[]
    def monitor():
        while not stop.wait(1):
            row=sample()
            rows.append(row)
            if (row['available']<6.25*2**30 or row['oom']!=baseline['oom'] or
                row['pswpout']-baseline['pswpout']>2048 or
                any(t>=(65 if name=='nvme' else 90) for name,t in row['temps'].items())):
                unsafe.append(row)
                command('systemctl','stop',CLIENT,check=False)
                return
    monitor_thread=threading.Thread(target=monitor,daemon=True)
    monitor_thread.start()
    try:
        command('systemctl','stop','evo-omp')
        runtime=Path('/run/evo-omp')
        runtime.mkdir(exist_ok=True,mode=0o750)
        user=pwd.getpwnam('evo-hermes')
        os.chown(runtime,user.pw_uid,user.pw_gid)
        for model,port in ((FLASH,8082),(QWEN,8083)):
            if model==QWEN:
                command('systemctl','stop','evo-flash')
                command('systemctl','start','evo-qwen')
                ready(8083)
            command('systemd-run','--unit='+CLIENT,'--collect','--wait',
                '--property=User=evo-hermes','--property=Group=evo-hermes',
                '--property=MemoryMax=1G','--property=MemorySwapMax=0','--property=CPUQuota=200%',
                '--property=RuntimeMaxSec=650','--setenv=PYTHONPATH='+str(RUN),
                '/opt/evo-omp/venv/bin/python','-B',str(Path(__file__)),'--client',model,str(port),timeout=700)
            if unsafe:
                raise RuntimeError('Resource guard tripped')
        write(RUN/'edit-controller-results.json',{'baseline':baseline,'samples':rows,'end':sample()})
        print('EDIT_PILOT_DONE',flush=True)
    finally:
        stop.set()
        monitor_thread.join(5)
        write(RUN/'edit-controller-resources.json',{'baseline':baseline,'samples':rows,'unsafe':unsafe,'end':sample()})
        restore()


async def client(model,port):
    from evo_omp import context,rpc
    requests=[]
    class Tap(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass
        def do_POST(self):
            body=self.rfile.read(int(self.headers['Content-Length']))
            data=json.loads(body)
            capture={'request':data,'start':time.monotonic(),'response_bytes':0,'usage':None}
            requests.append(capture)
            conn=http.client.HTTPConnection('127.0.0.1',port,timeout=240)
            try:
                conn.request('POST',self.path,body,{'Content-Type':'application/json'})
                response=conn.getresponse()
                capture['status']=response.status
                self.send_response(response.status)
                self.send_header('Content-Type',response.getheader('Content-Type','text/event-stream'))
                self.send_header('Connection','close')
                self.end_headers()
                while line:=response.readline():
                    capture['response_bytes']+=len(line)
                    if line.startswith(b'data: {'):
                        with contextlib.suppress(Exception):
                            event=json.loads(line[6:])
                            if event.get('usage'):
                                capture['usage']=event['usage']
                    self.wfile.write(line)
                    self.wfile.flush()
            except (BrokenPipeError,ConnectionResetError):
                pass
            finally:
                capture['elapsed']=time.monotonic()-capture['start']
                conn.close()
                self.close_connection=True
    server=ThreadingHTTPServer(('127.0.0.1',18090),Tap)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    original_argv=rpc.sandbox_argv
    def coding_only(profile,selected):
        args=original_argv(profile,selected)
        args[args.index('--tools')+1]='read,edit'
        return args
    rpc.sandbox_argv=coding_only
    tasks=[
        ({'bounds.c': '#include <stddef.h>\nint append_byte(char *out, size_t cap, size_t *len, char value) {\n    if (*len > cap) return -1;\n    out[*len] = value;\n    *len += 1;\n    return 0;\n}\n',
          'bounds.go': 'package bounds\n\nfunc At(values []int, index int) (int, bool) {\n    if index < 0 || index > len(values) {\n        return 0, false\n    }\n    return values[index], true\n}\n',
          'bounds.rs': 'pub fn at(values: &[i32], index: usize) -> Option<i32> {\n    if index > values.len() {\n        None\n    } else {\n        Some(values[index])\n    }\n}\n'},
         'Fix the out-of-bounds boundary check in append_byte, At, and at. Empty/full buffers and index equal to length must be rejected. Preserve the APIs and other behavior.'),
        ({'helpers.c': '#include <stddef.h>\nint positive(int value) {\n    return value > 0;\n}\nint nonnegative(int value) {\n    return value > 0;\n}\nsize_t window_size(size_t begin, size_t end) {\n    return end - begin + 1;\n}\n',
          'helpers.go': 'package helpers\n\nfunc Positive(value int) bool {\n    return value > 0\n}\nfunc Nonnegative(value int) bool {\n    return value > 0\n}\nfunc WindowSize(begin, end int) int {\n    return end - begin + 1\n}\n',
          'helpers.rs': 'pub fn positive(value: i32) -> bool {\n    value > 0\n}\npub fn nonnegative(value: i32) -> bool {\n    value > 0\n}\npub fn window_size(begin: usize, end: usize) -> usize {\n    end - begin + 1\n}\n'},
         'In all three files, make nonnegative/Nonnegative include zero, and window_size/WindowSize return the size of a half-open [begin,end) window. Inputs have begin<=end. Keep positive/Positive unchanged. Preserve the existing style.')]
    results=[]
    try:
        for index,(files,goal) in enumerate(tasks):
            modes=('hashline','replace') if index==0 else ('replace','hashline')
            for mode in modes:
                fixture=Path(tempfile.mkdtemp(prefix='.omp-edit-',dir=rpc.WORKSPACE))
                cfg=Path(tempfile.mkdtemp(prefix='edit-config-',dir=rpc.RUNTIME))
                for name,content in files.items():
                    (fixture/name).write_text(content)
                (cfg/'models.yml').write_text((RUN/'models.yml').read_text().replace(':8080/v1',':18090/v1'))
                (cfg/'config.yml').write_text((RUN/'config.yml').read_text()+f'\nedit:\n  mode: {mode}\n')
                session=rpc.OmpSession('edit-review',model,[],None,cfg)
                start_index=len(requests)
                started=time.monotonic()
                answer,error='',''
                try:
                    await session.start()
                    prompt='Edit only these files: '+', '.join('/workspace/'+fixture.name+'/'+n for n in files)+'.\n'+goal+'\nUse the read and edit tools. Do not add files. Reply briefly after editing.'
                    answer=await asyncio.wait_for(session.prompt(prompt,context.Turn('edit-review')),210)
                except Exception as e:
                    error=str(e) or type(e).__name__
                finally:
                    await session.close()
                actual={n:(fixture/n).read_text() for n in files}
                expected={}
                for name,source in files.items():
                    if index==0:
                        expected[name]=source.replace('*len > cap','*len >= cap').replace('index > len(values)','index >= len(values)').replace('index > values.len()','index >= values.len()')
                    else:
                        marker='nonnegative' if name.endswith(('.c','.rs')) else 'Nonnegative'
                        before,after=source.split(marker,1)
                        expected[name]=before+marker+after.replace('value > 0','value >= 0',1).replace('end - begin + 1','end - begin')
                norm=lambda s: re.sub(r'\s+','',s)
                correctness={n:norm(actual[n])==norm(expected[n]) for n in files}
                reqs=requests[start_index:]
                last=reqs[-1]['request'] if reqs else {'messages':[]}
                tool_results=[m for m in last['messages'] if m['role']=='tool']
                row={'model':model,'mode':mode,'workload':index,'elapsed':round(time.monotonic()-started,3),
                    'correct_files':correctness,'correct':all(correctness.values()),'error':error,
                    'provider_calls':len(reqs),'native_tools':session.tools,
                    'tool_result_bytes':sum(len(json.dumps(m['content']).encode()) for m in tool_results),
                    'usages':[r['usage'] for r in reqs], 'answer':answer[:500],
                    'tool_results':tool_results,'actual':actual,
                    'edit_schema':next((t for t in last.get('tools',[]) if t.get('function',{}).get('name')=='edit'),None)}
                results.append(row)
                write(RUN/'edit-results'/('edit-'+model+'.json'),results)
                print(json.dumps({k:v for k,v in row.items() if k not in ('actual','tool_results','usages','edit_schema')}),flush=True)
                assert fixture.parent.resolve()==rpc.WORKSPACE.resolve() and fixture.name.startswith('.omp-edit-')
                shutil.rmtree(fixture)
                shutil.rmtree(cfg)
    finally:
        server.shutdown()
        rpc.sandbox_argv=original_argv


if __name__=='__main__':
    if sys.argv[1]=='--controller':
        controller()
    elif sys.argv[1]=='--restore':
        restore()
    else:
        asyncio.run(client(sys.argv[2],int(sys.argv[3])))
