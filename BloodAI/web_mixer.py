"""Blood Mixer Web UI — aiohttp server on port 7777."""
import asyncio, json, logging
from aiohttp import web, WSMsgType
log = logging.getLogger("blood.web_mixer")

INDEX_HTML = """<!DOCTYPE html><html><head><meta charset="UTF-8">
<title>Blood Mixer</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}body{background:#0a0a0a;color:#eee;font-family:system-ui,sans-serif;min-height:100vh;display:flex;flex-direction:column;align-items:center;padding:40px 20px}
h1{font-size:2.5rem;color:#ff3333;margin-bottom:8px}.subtitle{color:#888;font-size:.9rem;margin-bottom:40px}
.mixer{display:flex;gap:30px;flex-wrap:wrap;justify-content:center;max-width:900px;width:100%}
.channel{background:#151515;border:1px solid #222;border-radius:16px;padding:24px;flex:1;min-width:260px;max-width:400px}
.channel-label{display:flex;align-items:center;gap:10px;font-weight:600;font-size:1.1rem;margin-bottom:16px}
.dot{width:10px;height:10px;border-radius:50%;background:#333;transition:.3s}
.dot.on{background:#ff3333;box-shadow:0 0 8px #ff3333}.dot.ducked{background:#ffa500;box-shadow:0 0 8px #ffa500}
.info{color:#aaa;font-size:.85rem;min-height:3em;margin-bottom:16px}
.meter-wrap{background:#0a0a0a;border-radius:6px;height:20px;overflow:hidden;position:relative;margin-bottom:12px}
.meter{height:100%;width:0%;background:linear-gradient(90deg,#0f0,#ff0 70%,#f00);border-radius:6px;transition:width .1s}
.controls{display:flex;gap:12px;align-items:center;margin-top:8px}
input[type=range]{flex:1;-webkit-appearance:none;height:6px;background:#333;border-radius:3px}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:16px;height:16px;border-radius:50%;background:#ff3333;cursor:pointer}
.btn{background:#222;border:1px solid #444;color:#eee;padding:8px 18px;border-radius:8px;cursor:pointer;font-size:.85rem}
.btn:hover{background:#333}.btn.on{background:#ffa500;color:#000;border-color:#ffa500}
.duck-indicator{margin-top:30px;padding:16px 30px;border-radius:12px;background:#151515;border:1px solid #222;font-size:1.2rem;font-weight:600;transition:.3s}
.duck-indicator.ducked{border-color:#ffa500;color:#ffa500}
.status{color:#666;font-size:.8rem;margin-top:8px}
</style></head><body>
<h1>Blood Mixer</h1><p class="subtitle">Real-time music + voice ducking dashboard</p>
<div class="mixer">
<div class="channel" id="m-ch"><div class="channel-label"><div class="dot" id="m-dot"></div>Music</div>
<div class="info" id="m-info">No music</div><div class="meter-wrap"><div class="meter" id="m-meter"></div></div>
<div class="controls"><input type="range" id="m-vol" min="0" max="100" value="80"><span id="m-vv">80%</span></div></div>
<div class="channel" id="t-ch"><div class="channel-label"><div class="dot" id="t-dot"></div>Blood (TTS)</div>
<div class="info" id="t-info">Idle</div><div class="meter-wrap"><div class="meter" id="t-meter"></div></div>
<div class="controls"><button class="btn" id="duck-btn">Force Duck</button></div></div>
</div>
<div class="duck-indicator" id="duck">Music at full volume</div>
<p class="status" id="st">Connecting...</p>
<script>
const ws=new WebSocket('ws://'+location.host+'/ws');
ws.onopen=()=>{document.getElementById('st').textContent='Connected';document.getElementById('st').style.color='#0f0'};
ws.onclose=()=>{document.getElementById('st').textContent='Disconnected — refresh';document.getElementById('st').style.color='#f55'};
ws.onmessage=e=>{
const d=JSON.parse(e.data);
if(typeof d.music.volume==='number'){document.getElementById('m-vol').value=Math.round(d.music.volume*100);document.getElementById('m-vv').textContent=Math.round(d.music.volume*100)+'%'}
const set=(id,txt,act,lv)=>{document.getElementById(id+'-info').textContent=txt;const dot=document.getElementById(id+'-dot');act?dot.classList.add('on'):dot.classList.remove('on');document.getElementById(id+'-meter').style.width=(lv*100)+'%'};
set('m',d.music.active?(d.music.title||'Playing'):'No music',d.music.active,d.music.level);
set('t',d.tts.active?('"'+d.tts.text.substring(0,60)+'"'):'Idle',d.tts.active,d.tts.level);
const duck=document.getElementById('duck');
if(d.ducked){duck.textContent='Music DUCKED (Blood speaking)';duck.classList.add('ducked');document.getElementById('m-dot').classList.add('ducked')}
else{duck.textContent='Music at full volume';duck.classList.remove('ducked');document.getElementById('m-dot').classList.remove('ducked')}
document.getElementById('duck-btn').classList.toggle('on',!!d.force_duck);
};
document.getElementById('m-vol').addEventListener('input',e=>{document.getElementById('m-vv').textContent=e.target.value+'%';ws.send(JSON.stringify({action:'vol',v:e.target.value/100}))});
document.getElementById('duck-btn').addEventListener('click',()=>{const b=document.getElementById('duck-btn');const on=b.classList.toggle('on');ws.send(JSON.stringify({action:on?'duck':'unduck'}))});
</script></body></html>"""

# Shared state
_state={"music":{"active":False,"title":"","level":0.0,"volume":0.5},"tts":{"active":False,"text":"","level":0.0},"ducked":False,"force_duck":False}
_clients=set()
_loop=None
_active_guild_id=None

def bind_guild(guild_id):
    global _active_guild_id
    _active_guild_id=guild_id

def update_state(music_active=None,music_title=None,music_level=None,music_volume=None,tts_active=None,tts_text=None,tts_level=None,ducked=None,force_duck=None):
    if music_active is not None:_state["music"]["active"]=music_active
    if music_title is not None:_state["music"]["title"]=music_title
    if music_level is not None:_state["music"]["level"]=music_level
    if music_volume is not None:_state["music"]["volume"]=music_volume
    if tts_active is not None:_state["tts"]["active"]=tts_active
    if tts_text is not None:_state["tts"]["text"]=tts_text
    if tts_level is not None:_state["tts"]["level"]=tts_level
    if ducked is not None:_state["ducked"]=ducked
    if force_duck is not None:_state["force_duck"]=force_duck
    _broadcast()

def _broadcast():
    if not _loop or _loop.is_closed():
        return
    data=json.dumps(_state)
    coro=_broadcast_async(data)
    try:
        running=asyncio.get_running_loop()
    except RuntimeError:
        running=None
    if running is _loop:
        asyncio.create_task(coro)
    else:
        asyncio.run_coroutine_threadsafe(coro, _loop)

async def _broadcast_async(data):
    dead=set()
    for ws in list(_clients):
        try:
            await ws.send_str(data)
        except Exception:
            dead.add(ws)
    for ws in dead:_clients.discard(ws)

def _get_active_mixer():
    if not _active_guild_id:
        return None
    try:
        from voice import get_music_queue
        mq=get_music_queue(_active_guild_id)
        return mq._mixer
    except Exception:
        return None

async def handle_ws(request):
    ws=web.WebSocketResponse()
    await ws.prepare(request)
    _clients.add(ws)
    await ws.send_str(json.dumps(_state))
    async for msg in ws:
        if msg.type==WSMsgType.TEXT:
            try:
                d=json.loads(msg.data)
                action=d.get("action")
                if action=="vol":
                    vol=max(0.0,min(1.0,float(d.get("v",0.5))))
                    if _active_guild_id:
                        from voice import set_music_volume
                        set_music_volume(_active_guild_id,vol)
                    update_state(music_level=vol if _state["music"]["active"] else 0.0,music_volume=vol)
                elif action=="duck":
                    mixer=_get_active_mixer()
                    if mixer:
                        mixer.set_force_duck(True)
                        update_state(ducked=bool(mixer.has_music),force_duck=True)
                elif action=="unduck":
                    mixer=_get_active_mixer()
                    if mixer:
                        mixer.set_force_duck(False)
                        update_state(ducked=bool(mixer.is_ducking and mixer.has_music),force_duck=False)
            except Exception:
                pass
    _clients.discard(ws)
    return ws

app=web.Application()
app.router.add_get("/",lambda r: web.Response(text=INDEX_HTML,content_type="text/html"))
app.router.add_get("/ws",handle_ws)

_server=None

async def start_web_mixer(port=7777):
    global _server,_loop
    if _server:
        return
    _loop=asyncio.get_running_loop()
    runner=web.AppRunner(app)
    await runner.setup()
    site=web.TCPSite(runner,"0.0.0.0",port)
    await site.start()
    _server=runner
    log.info("Mixer web UI running at http://localhost:%d",port)
