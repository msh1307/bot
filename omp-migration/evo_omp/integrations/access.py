"""Constrained host tools and native Discord controls; no access to shell or credentials."""
import asyncio
import json
from pathlib import Path
import socket
import time
import urllib.request

OWNER = '543004181842362379'
CLIENT = None
LOOP = None
VIEW_CLASS = None
PANEL_FILE = Path('/var/lib/evo-omp/evo-access-panel.json')

def owner_present():
    from ..context import owner_message
    return owner_message() is not None

def admin(payload):
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
        connection.settimeout(5)
        connection.connect('/run/evo-access/admin.sock')
        connection.sendall((json.dumps(payload)+'\n').encode())
        output=bytearray()
        while b'\n' not in output:
            chunk=connection.recv(4096)
            if not chunk: break
            output.extend(chunk)
            if len(output)>16384: raise ValueError('Admin response too large')
        return json.loads(output)

def runtime_status(mem):
    def read_int(path):
        try: return int(path.read_text().strip())
        except (OSError, ValueError): return None

    def read_json(path):
        try:
            value=json.loads(Path(path).read_text())
            return value if isinstance(value,dict) else {}
        except (OSError,ValueError): return {}

    inventory=read_json('/etc/evo-llm/hardware.json')
    physical=inventory.get('physical_memory_bytes')
    if not isinstance(physical,int) or isinstance(physical,bool) or physical<=0: physical=None
    gpu=[]
    for card in sorted(Path('/sys/class/drm').glob('card[0-9]*')):
        if not card.name[4:].isdigit(): continue
        values={name:read_int(card/'device'/('mem_info_'+name))
                for name in ('vram_total','vram_used','gtt_total','gtt_used')}
        if any(value is not None for value in values.values()):
            gpu.append({'card':card.name,**{name+'_gib':round(value/2**30,3)
                        if value is not None else None for name,value in values.items()}})
    policies={}
    for path in sorted(Path('/sys/devices/system/cpu/cpufreq').glob('policy*')):
        try:
            key=((path/'scaling_governor').read_text().strip(),
                 (path/'energy_performance_preference').read_text().strip(),
                 read_int(path/'scaling_max_freq'))
        except OSError: continue
        policies[key]=policies.get(key,0)+1
    power=read_json('/run/evo-power/profile.json')
    return {'memory':{
                'physical_total_gib':round(physical/2**30,2) if physical is not None else None,
                'physical_source':inventory.get('source') if physical is not None else None,
                'physical_observed_at':inventory.get('observed_at') if physical is not None else None,
                'kernel_managed_total_gib':round(mem['MemTotal']/2**30,2),
                'available_gib':round(mem['MemAvailable']/2**30,2),
                'swap_total_gib':round(mem['SwapTotal']/2**30,2),
                'accounting':'MemTotal excludes firmware-reserved RAM. GPU carveout is separate; '
                             'GTT is backed by system RAM. Do not add GTT as extra physical RAM.'},
            'gpu_memory':gpu,
            'power':{'last_applied':{key:power[key] for key in ('profile','stapm_w','fast_w','slow_w')
                                     if key in power},
                     'current_cpu_policies':[{'governor':key[0],'epp':key[1],
                         'max_mhz':key[2]/1000 if key[2] is not None else None,'policy_count':count}
                         for key,count in policies.items()]}}

def system_status():
    mem={line.split(':')[0]:int(line.split()[1])*1024 for line in Path('/proc/meminfo').read_text().splitlines()
         if len(line.split())>2 and line.split()[1].isdigit()}
    sensors={}
    for hw in Path('/sys/class/hwmon').glob('hwmon*'):
        try: name=(hw/'name').read_text().strip()
        except OSError: continue
        ram_sensor = name in ('spd5118','jc42') or any(word in name.lower() for word in ('dimm','lpddr','dram'))
        if name not in ('k10temp','amdgpu','nvme') and not ram_sensor: continue
        values=[]
        for item in sorted(hw.glob('temp*_input')):
            try: values.append(int(item.read_text())/1000)
            except (OSError,ValueError): pass
        if values: sensors.setdefault('ram' if ram_sensor else name,[]).extend(values)
    model=None
    try:
        with urllib.request.urlopen('http://127.0.0.1:8080/evo/status',timeout=3) as response:
            model=json.load(response)
    except OSError: pass
    model_swap={}
    model_swap_max={}
    for unit in ('evo-flash','evo-qwen'):
        group=Path('/sys/fs/cgroup/system.slice')/(unit+'.service')
        file=group/'memory.swap.current'
        if file.exists():
            try: model_swap[unit]=int(file.read_text())
            except (OSError,ValueError): pass
        try: model_swap_max[unit]=(group/'memory.swap.max').read_text().strip()
        except OSError: pass
    try: access=admin({'action':'status'})
    except (OSError,ValueError): access={'listening':None}
    return {'success':True,'sampled_at':int(time.time()),'temperatures_c':sensors,
            'ram_temperature_available':bool(sensors.get('ram')),
            'ram_available_gib':round(mem['MemAvailable']/2**30,2),
            'system_swap_mib':round((mem['SwapTotal']-mem['SwapFree'])/2**20,2),
            'model_swap_bytes':model_swap,'model_swap_max':model_swap_max,
            'model':model,'access':access,**runtime_status(mem)}

def temp_text(result):
    sensors=result['temperatures_c']
    temperatures=' · '.join(f'{label} {max(sensors[name]):.1f}°C'
        for label,name in [('CPU','k10temp'),('GPU','amdgpu'),('RAM','ram'),('SSD','nvme')]
        if sensors.get(name)) or '조회 가능한 온도 센서 없음'
    model=(result.get('model') or {}).get('active') or '없음'
    if model.startswith('sc117'): model='Flash-Next'
    elif model.startswith('qwen'): model='Qwen 27B'
    swaps=', '.join(k.replace('evo-','')+': '+str(v)+'B' for k,v in result['model_swap_bytes'].items())
    return (temperatures+'\n'
            f"가용 RAM {result['ram_available_gib']:.2f}GiB · 시스템 swap {result['system_swap_mib']:.1f}MiB\n"
            f"모델 {model} · 모델 swap {swaps or '없음'}")

def panel_text(state):
    session=state.get('session')
    mode='API' if state.get('mode')=='api' else 'Web UI'
    if not session:
        return f'**EVO 접속 · 🔴 닫힘**\n모드 {mode} · 13077 닫힘 · 키 유지\nAPI / Web UI를 선택하면 3시간 열립니다.'
    expiry=int(session['expires_at'])
    return f"**EVO 접속 · 🟢 열림**\n모드 {mode} · 13077\n만료 <t:{expiry}:t> · <t:{expiry}:R>"

def access_log_text(result):
    records=result.get('records',[])[-6:]
    if not records:return '이번 접속 기간의 기록이 없습니다. 닫기·만료 때 기록을 삭제합니다.'
    rows=['**최근 접속 기록 · 임시**']
    labels={'/access/login':'로그인','/access/logout':'로그아웃','/models/load':'모델 선택',
            '/models/unload':'모델 내림','/v1/chat/completions':'채팅/API','/v1/completions':'API','/completion':'채팅'}
    for record in reversed(records):
        model=record.get('model','')
        model=' · Flash-Next' if model.startswith('sc117') else ' · 27B' if model.startswith('qwen') else ''
        rows.append(f"<t:{int(record['time'])}:T> · `{record['peer_ip']}` · {labels.get(record['path'],'접근 거부')}{model} · {record['status']}")
    rows.append('서버에서 보이는 IP 기준입니다. 닫기·만료 때 삭제하며 메시지·키는 기록하지 않습니다.')
    return '\n'.join(rows)

def credential_text(result):
    session=result.get('session')
    timer=f"만료 <t:{session['expires_at']}:F> · <t:{session['expires_at']}:R>" if session else '접속 닫힘 · Web UI 또는 Refresh를 누르면 열립니다.'
    url=result.get('web_url') or ('외부주소:13077'+result['web_path'])
    return (f"**EVO 웹/API 접속정보 · 고정 키**\n{timer}\n"
            f"웹 접속 (Web UI 선택 후): {url}\nAPI base: `{result['api_base_url']}`\n"
            f"API key (Bearer):\n```\n{result['api_key']}\n```\n"
            '이 키를 보관하세요. Refresh는 같은 키로 지금부터 3시간 열고, 종료/만료는 포트와 진행 중 연결을 닫습니다. 재시작 후에도 같은 키로 Refresh할 수 있습니다.')

async def private_credentials(result):
    if CLIENT is None: raise RuntimeError('Discord is not connected')
    user=CLIENT.get_user(int(OWNER)) or await CLIENT.fetch_user(int(OWNER))
    await user.send(credential_text(result),view=VIEW_CLASS())

def web_link_text(result):
    session=result.get('session')
    timer=f"만료 <t:{session['expires_at']}:F> · <t:{session['expires_at']}:R>" if session else '접속 닫힘 · Web UI 버튼으로 열어주세요.'
    return f"[웹 채팅 열기]({result['web_url']})\n{timer}"

def web_link_view(result):
    import discord
    view=discord.ui.View(timeout=300)
    view.add_item(discord.ui.Button(label='웹 채팅 열기 · 자동 로그인',style=discord.ButtonStyle.link,url=result['web_url']))
    return view

async def private_web_link(result):
    if CLIENT is None: raise RuntimeError('Discord is not connected')
    user=CLIENT.get_user(int(OWNER)) or await CLIENT.fetch_user(int(OWNER))
    await user.send(web_link_text(result),view=web_link_view(result))

def manage(args,**kwargs):
    if not owner_present(): return json.dumps({'success':False,'error':'Only the Discord owner can manage external access'})
    action=args.get('action','status')
    if action not in ('issue','refresh','select','extend','adjust','expire','status'): return json.dumps({'success':False,'error':'Unknown action'})
    if action in ('issue','extend'): action='refresh'
    payload={'action':action,'minutes':args.get('minutes',180)}
    if args.get('mode') is not None: payload['mode']=args['mode']
    if args.get('delta_minutes') is not None: payload['delta_minutes']=args['delta_minutes']
    try:
        if action in ('expire','adjust'):
            state=admin({'action':'status'})
            credential=state.get('credential') or state.get('session')
            if not credential: raise ValueError('No credential exists')
            payload['session_id']=credential['id']
        result=admin(payload)
        first_issue=bool(result.get('api_key'))
        if result.get('api_key'):
            # The key goes directly to the owner, not into tool output or the model's context.
            try:
                asyncio.run_coroutine_threadsafe(private_credentials(result),LOOP).result(timeout=12)
            except Exception:
                admin({'action':'discard_undelivered','session_id':result['session']['id']})
                raise RuntimeError('First credential delivery failed; access was closed and the undelivered key discarded')
            for key in ('api_key','web_path','web_url'): result.pop(key,None)
            result['credentials_delivered']='Discord owner DM'
        if result.get('success') and action=='select' and args.get('mode')=='web' and not first_issue:
            credentials=admin({'action':'credentials'})
            if not credentials.get('success'): raise ValueError(credentials.get('error','Cannot read the connection link'))
            asyncio.run_coroutine_threadsafe(private_web_link(credentials),LOOP).result(timeout=12)
            result['connection_link_delivered']='Discord owner DM'
        return json.dumps(result,ensure_ascii=False)
    except Exception as error: return json.dumps({'success':False,'error':str(error)},ensure_ascii=False)

def status_tool(args,**kwargs):
    from ..context import owner_identity
    if owner_identity() is None: return json.dumps({'success':False,'error':'Owner Discord context required'})
    try: return json.dumps(system_status(),ensure_ascii=False)
    except Exception as error: return json.dumps({'success':False,'error':str(error)},ensure_ascii=False)

async def temp_command(raw_args):
    if not owner_present(): return '오너만 사용할 수 있습니다.'
    return temp_text(await asyncio.to_thread(system_status))

async def access_command(raw_args):
    from ..context import owner_message
    if not owner_present(): return '오너만 사용할 수 있습니다.'
    if CLIENT is None: return 'Discord 연결 준비 중입니다.'
    channel_id=owner_message().channel
    channel=CLIENT.get_channel(int(channel_id)) or await CLIENT.fetch_channel(int(channel_id))
    state=await asyncio.to_thread(admin,{'action':'status'})
    await channel.send(panel_text(state),view=VIEW_CLASS())
    return None

def discord_factory(native,adapter):
    global CLIENT,LOOP,VIEW_CLASS
    import discord
    CLIENT=native
    LOOP=asyncio.get_running_loop()
    class AccessView(discord.ui.View):
        def __init__(self): super().__init__(timeout=None)
        async def interaction_check(self,interaction):
            if str(interaction.user.id)!=OWNER:
                await interaction.response.send_message('오너만 사용할 수 있습니다.',ephemeral=True)
                return False
            return True
        async def perform(self,interaction,action,mode=None,delta_minutes=None):
            is_panel=bool(interaction.message and interaction.message.content.startswith(('**EVO 외부 접속','**EVO 접속')))
            # A component update edits an ephemeral panel through its interaction token.
            # Credential DMs instead receive a new private reply so their key is retained.
            await interaction.response.defer(ephemeral=True,thinking=not is_panel)
            try:
                state=await asyncio.to_thread(admin,{'action':'status'})
                payload={'action':action,'minutes':180}
                if mode is not None: payload['mode']=mode
                if delta_minutes is not None: payload['delta_minutes']=delta_minutes
                if action in ('expire','adjust'):
                    credential=state.get('credential') or state.get('session')
                    if not credential: raise ValueError('아직 발급된 키가 없습니다.')
                    payload['session_id']=credential['id']
                result=await asyncio.to_thread(admin,payload)
                text=panel_text(result)
                reply_view=AccessView() if result.get('success') else None
                if result.get('api_key'):
                    try: await private_credentials(result)
                    except Exception:
                        await asyncio.to_thread(admin,{'action':'discard_undelivered','session_id':result['session']['id']})
                        raise RuntimeError('키 DM 전송 실패: 접속을 닫았습니다. DM을 허용한 뒤 다시 Refresh하세요.')
                    text+='\n최초 키는 오너 DM으로 보냈습니다. 이후 Refresh에서는 키를 바꾸지 않습니다.'
                if result.get('success') and action=='select' and mode=='web':
                    credentials=await asyncio.to_thread(admin,{'action':'credentials'})
                    if not credentials.get('success'): raise ValueError(credentials.get('error','접속 링크를 읽지 못했습니다.'))
                    text=web_link_text(credentials)
                    reply_view=web_link_view(credentials)
                elif result.get('success') and action=='select' and mode=='api':
                    credentials=await asyncio.to_thread(admin,{'action':'credentials'})
                    if not credentials.get('success'): raise ValueError(credentials.get('error','접속 키를 읽지 못했습니다.'))
                    text=f"API base: `{credentials['api_base_url']}`\nBearer key:\n```\n{credentials['api_key']}\n```"
                    reply_view=None
                if not result.get('success'): text='실패: '+result.get('error','알 수 없는 오류')
                if is_panel:
                    panel=panel_text(await asyncio.to_thread(admin,{'action':'status'}))
                    if not result.get('success'): panel+='\n'+text
                    await interaction.edit_original_response(content=panel,view=AccessView())
                    if result.get('success') and action=='select':
                        await interaction.followup.send(content=text,view=(reply_view if reply_view is not None else discord.utils.MISSING),ephemeral=True)
                else:
                    await interaction.edit_original_response(content=text,view=reply_view)
                if result.get('success') and action=='status':
                    logs=await asyncio.to_thread(admin,{'action':'logs'})
                    await interaction.followup.send(content=access_log_text(logs),ephemeral=True)
            except Exception as error:
                await interaction.edit_original_response(content='실패: '+str(error))
        @discord.ui.button(label='API',style=discord.ButtonStyle.primary,custom_id='evo_access_api',row=0)
        async def api(self,interaction,button): await self.perform(interaction,'select','api')
        @discord.ui.button(label='Web UI',style=discord.ButtonStyle.primary,custom_id='evo_access_web',row=0)
        async def web(self,interaction,button): await self.perform(interaction,'select','web')
        @discord.ui.button(label='서버 관리',style=discord.ButtonStyle.secondary,custom_id='evo_access_manage',row=1)
        async def servers(self,interaction,button):
            await interaction.response.defer(ephemeral=True,thinking=True)
            try:
                result=await asyncio.to_thread(admin,{'action':'management'})
                if not result.get('success'): raise ValueError(result.get('error','관리 화면을 열지 못했습니다.'))
                view=discord.ui.View(timeout=300)
                view.add_item(discord.ui.Button(label='서버 관리 열기',style=discord.ButtonStyle.link,url=result['url']))
                await interaction.edit_original_response(content='서버 선택 · Codex 대화 · 파일 · 짧은 명령\n'
                    +f"관리 전용 로그인 · <t:{int(result['expires_at'])}:R> 만료",view=view)
            except Exception as error:
                await interaction.edit_original_response(content='관리 화면 연결 실패: '+str(error)[:200])
        @discord.ui.button(label='Refresh · 3시간',style=discord.ButtonStyle.success,custom_id='evo_access_issue',row=1)
        async def refresh(self,interaction,button): await self.perform(interaction,'refresh')
        @discord.ui.button(label='닫기',style=discord.ButtonStyle.danger,custom_id='evo_access_expire',row=0)
        async def expire(self,interaction,button): await self.perform(interaction,'expire')
        @discord.ui.button(label='상태',style=discord.ButtonStyle.secondary,custom_id='evo_access_status',row=0)
        async def status(self,interaction,button): await self.perform(interaction,'status')
        @discord.ui.button(label='−3시간',style=discord.ButtonStyle.secondary,custom_id='evo_access_minus',row=1)
        async def minus(self,interaction,button): await self.perform(interaction,'adjust',delta_minutes=-180)
        @discord.ui.button(label='+3시간',style=discord.ButtonStyle.secondary,custom_id='evo_access_plus',row=1)
        async def plus(self,interaction,button): await self.perform(interaction,'adjust',delta_minutes=180)
    VIEW_CLASS=AccessView
    native.add_view(AccessView())
    async def native_access(interaction):
        if str(interaction.user.id)!=OWNER:
            await interaction.response.send_message('오너만 사용할 수 있습니다.',ephemeral=True)
            return
        if not await adapter._check_slash_authorization(interaction,'/access'): return
        await interaction.response.defer(ephemeral=True,thinking=True)
        state=await asyncio.to_thread(admin,{'action':'status'})
        await interaction.edit_original_response(content=panel_text(state),view=AccessView())
    native.tree.add_command(discord.app_commands.Command(name='access',description='API / 웹 접속 · 3시간 타이머',callback=native_access),override=True)
    async def native_temp(interaction):
        if str(interaction.user.id)!=OWNER:
            await interaction.response.send_message('오너만 사용할 수 있습니다.',ephemeral=True)
            return
        if not await adapter._check_slash_authorization(interaction,'/temp'): return
        await interaction.response.defer(ephemeral=True,thinking=True)
        try:
            result=await asyncio.to_thread(system_status)
            await interaction.edit_original_response(content=temp_text(result))
        except Exception as error:
            await interaction.edit_original_response(content='온도 조회 실패: '+type(error).__name__)
    native.tree.add_command(discord.app_commands.Command(name='temp',description='CPU·GPU·RAM·SSD 온도와 가용 RAM 조회',callback=native_temp),override=True)
    class LegacyExtendView(discord.ui.View):
        def __init__(self): super().__init__(timeout=None)
        async def interaction_check(self,interaction):
            return await AccessView().interaction_check(interaction)
        @discord.ui.button(label='Refresh · 3시간',custom_id='evo_access_extend')
        async def refresh(self,interaction,button): await AccessView().perform(interaction,'refresh')
    native.add_view(LegacyExtendView())
    async def panel_watch():
        message=None
        last_text=None
        while CLIENT is native and not native.is_closed():
            try:
                state=await asyncio.to_thread(admin,{'action':'status'})
                text=panel_text(state)
                if message is None:
                    channel=native.get_channel(990826359704154225) or await native.fetch_channel(990826359704154225)
                    if PANEL_FILE.exists():
                        try: message=await channel.fetch_message(int(json.loads(PANEL_FILE.read_text())['message_id']))
                        except (ValueError,KeyError,discord.NotFound): pass
                    if message is None:
                        message=await channel.send(text,view=AccessView())
                        PANEL_FILE.write_text(json.dumps({'message_id':str(message.id)})+'\n')
                        PANEL_FILE.chmod(0o600)
                if text!=last_text:
                    await message.edit(content=text,view=AccessView())
                    last_text=text
            except Exception:
                # Do not emit credentials or make a reconnect depend on the panel.
                await asyncio.sleep(5)
            await asyncio.sleep(2)
    native.loop.create_task(panel_watch())

def register(ctx):
    ctx.register_command('temp',temp_command,'CPU/GPU/RAM/SSD 온도, RAM, swap 확인')
    ctx.register_command('access',access_command,'고정 키로 외부 웹/API Refresh·종료 버튼')
    ctx.register_platform_handler('discord',discord_factory)
    ctx.register_tool('evo_access_manage','evo_access',{
        'name':'evo_access_manage',
        'description':'Owner-requested access on port 13077 with one fixed random key across expiry and restarts. refresh resets the timer to minutes from now (default 180); issue and extend are aliases. select changes mode without changing an active timer, or opens for 180 minutes if closed. mode api exposes OpenAI chat/completion APIs only; web exposes the existing 8080 chat interface including its API routes. adjust adds delta_minutes (+180 or -180) to the timer; reducing it past now closes access. expire closes the port and connections but keeps the key. The first opening sends the key privately to the owner. status reads metadata. Never open, extend, or switch access unless explicitly requested by the owner.',
        'parameters':{'type':'object','properties':{'action':{'type':'string','enum':['refresh','select','adjust','issue','extend','expire','status']},
            'minutes':{'type':'integer','minimum':1,'maximum':180,'default':180},
            'mode':{'type':'string','enum':['api','web'],'description':'Omit to retain the current mode.'},
            'delta_minutes':{'type':'integer','enum':[-180,180],'description':'Required for adjust: remove or add three hours.'}},'required':['action']}},manage)
    ctx.register_tool('evo_system_status','evo_access',{
        'name':'evo_system_status','description':'Read EVO temperatures, physical RAM inventory (if available), kernel-managed RAM, GPU carveout/GTT, available RAM, system/model swap and limits, last-applied power profile, current CPU policy and model/access status. Missing measurements are unknown. Infrastructure status only; conversation history is not inspected. Read-only; no root approval or inference.',
        'parameters':{'type':'object','properties':{}}},status_tool)
