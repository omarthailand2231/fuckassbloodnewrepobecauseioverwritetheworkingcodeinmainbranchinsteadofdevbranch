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
const set=(id,txt,act,lv)=>{document.getElementById(id+'-info').textContent=txt;const dot=document.getElementById(id+'-dot');act?dot.classList.add('on'):dot.classList.remove('on');document.getElementById(id+'-meter').style.width=(lv*100)+'%'};
set('m',d.music.active?(d.music.title||'Playing'):'No music',d.music.active,d.music.level);
set('t',d.tts.active?('"'+d.tts.text.substring(0,60)+'"'):'Idle',d.tts.active,d.tts.level);
const duck=document.getElementById('duck');
if(d.ducked){duck.textContent='Music DUCKED (Blood speaking)';duck.classList.add('ducked');document.getElementById('m-dot').classList.add('ducked')}
else{duck.textContent='Music at full volume';duck.classList.remove('ducked');document.getElementById('m-dot').classList.remove('ducked')}
};
document.getElementById('m-vol').addEventListener('input',e=>{document.getElementById('m-vv').textContent=e.target.value+'%';ws.send(JSON.stringify({action:'vol',v:e.target.value/100}))});
document.getElementById('duck-btn').addEventListener('click',()=>{const b=document.getElementById('duck-btn');const on=b.classList.toggle('on');ws.send(JSON.stringify({action:on?'duck':'unduck'}))});
</script></body></html>"""

# Shared state
_state={"music":{"active":False,"title":"","level":0.0},"tts":{"active":False,"text":"","level":0.0},"ducked":False}
_clients=set()

def update_state(music_active=None,music_title=None,music_level=None,tts_active=None,tts_text=None,tts_level=None,ducked=None):
    if music_active is not None:_state["music"]["active"]=music_active
    if music_title is not None:_state["music"]["title"]=music_title
    if music_level is not None:_state["music"]["level"]=music_level
    if tts_active is not None:_state["tts"]["active"]=tts_active
    if tts_text is not None:_state["tts"]["text"]=tts_text
    if tts_level is not None:_state["tts"]["level"]=tts_level
    if ducked is not None:_state["ducked"]=ducked
    _broadcast()

def _broadcast():
    data=json.dumps(_state)
    dead=set()
    for ws in _clients:
        try:
            ws.send_str(data)
        except Exception:
            dead.add(ws)
    for ws in dead:_clients.discard(ws)

async def handle_ws(request):
    ws=web.WebSocketResponse()
    await ws.prepare(request)
    _clients.add(ws)
    ws.send_str(json.dumps(_state))
    async for msg in ws:
        if msg.type==WSMsgType.TEXT:
            try:
                d=json.loads(msg.data)
                action=d.get("action")
                if action=="vol":
                    from mixer import get_mixer
                    mixer=get_mixer()
                    if mixer:mixer.set_volume("music",d.get("v",0.8))
                elif action=="duck":
                    from mixer import get_mixer
                    mixer=get_mixer()
                    if mixer:mixer.start_duck("music")
                elif action=="unduck":
                    from mixer import get_mixer
                    mixer=get_mixer()
                    if mixer:mixer.stop_duck("music")
            except Exception:
                pass
    _clients.discard(ws)
    return ws

app=web.Application()
app.router.add_get("/",lambda r: web.Response(text=INDEX_HTML,content_type="text/html"))
app.router.add_get("/ws",handle_ws)

_server=None

async def start_web_mixer(port=7777):
    global _server
    if _server:
        return
    runner=web.AppRunner(app)
    await runner.setup()
    site=web.TCPSite(runner,"0.0.0.0",port)
    await site.start()
    _server=runner
    log.info("Mixer web UI running at http://localhost:%d",port)
