"""Blood Mixer Web UI — aiohttp server on port 7777.

Supports multiple guilds.  Each guild gets its own state dict and
appears as a tab in the web UI.  WS messages use the envelope:
  { "guilds": { "<guild_id>": { "name": "…", …state… }, … } }
Actions from the client carry  "guild": "<guild_id>"  so the server
knows which guild to target.
"""
import asyncio, json, logging
from aiohttp import web, WSMsgType
log = logging.getLogger("blood.web_mixer")

# ═══════════════════════════════════════════════════════════════════════
# HTML / CSS / JS  (multi-guild tab UI)
# ═══════════════════════════════════════════════════════════════════════

INDEX_HTML = """<!DOCTYPE html><html><head><meta charset="UTF-8">
<title>Blood Mixer</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0a0a0a;color:#eee;font-family:system-ui,sans-serif;min-height:100vh;display:flex;flex-direction:column;align-items:center;padding:30px 20px}
h1{font-size:2.5rem;color:#ff3333;margin-bottom:4px}
.subtitle{color:#888;font-size:.9rem;margin-bottom:20px}

/* Tabs */
.tab-bar{display:flex;gap:6px;flex-wrap:wrap;justify-content:center;margin-bottom:24px;max-width:960px;width:100%}
.tab{background:#151515;border:1px solid #222;color:#888;padding:8px 20px;border-radius:10px 10px 0 0;cursor:pointer;font-size:.85rem;transition:.2s;display:flex;align-items:center;gap:8px}
.tab:hover{background:#1a1a1a;color:#ccc}
.tab.active{background:#1c1c1c;border-bottom-color:#1c1c1c;color:#ff3333;font-weight:600}
.tab .t-dot{width:8px;height:8px;border-radius:50%;background:#333;flex-shrink:0}
.tab .t-dot.music{background:#ff3333;box-shadow:0 0 6px #ff3333}
.tab .t-dot.radio{background:#0f0;box-shadow:0 0 6px #0f0}
.tab .t-dot.idle{background:#333}
.no-servers{color:#555;font-style:italic;font-size:.9rem;padding:20px 0}

/* Layout */
.main-grid{display:grid;grid-template-columns:1fr 1fr;gap:20px;max-width:960px;width:100%}
.full-span{grid-column:1/-1}

/* Cards */
.card{background:#151515;border:1px solid #222;border-radius:14px;padding:20px}
.card-title{display:flex;align-items:center;gap:10px;font-weight:600;font-size:1.05rem;margin-bottom:14px}
.dot{width:10px;height:10px;border-radius:50%;background:#333;transition:.3s}
.dot.on{background:#ff3333;box-shadow:0 0 8px #ff3333}
.dot.ducked{background:#ffa500;box-shadow:0 0 8px #ffa500}
.dot.radio{background:#0f0;box-shadow:0 0 8px #0f0}
.info{color:#aaa;font-size:.85rem;min-height:1.5em;margin-bottom:12px}

/* Meters */
.meter-wrap{background:#0a0a0a;border-radius:6px;height:16px;overflow:hidden;margin-bottom:10px}
.meter{height:100%;width:0%;background:linear-gradient(90deg,#0f0,#ff0 70%,#f00);border-radius:6px;transition:width .1s}

/* Sliders */
.slider-row{display:flex;gap:10px;align-items:center;margin-top:6px}
.slider-row label{font-size:.75rem;color:#888;min-width:70px}
.slider-row span{font-size:.8rem;color:#ccc;min-width:40px;text-align:right}
input[type=range]{flex:1;-webkit-appearance:none;height:5px;background:#333;border-radius:3px;outline:none}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:14px;height:14px;border-radius:50%;background:#ff3333;cursor:pointer}

/* Buttons */
.btn{background:#222;border:1px solid #444;color:#eee;padding:7px 16px;border-radius:8px;cursor:pointer;font-size:.8rem;transition:.2s}
.btn:hover{background:#333}
.btn.on{background:#ffa500;color:#000;border-color:#ffa500}
.btn.danger{border-color:#f44;color:#f44}
.btn.danger:hover{background:#f44;color:#000}
.btn.green{border-color:#0c0;color:#0c0}
.btn.green:hover{background:#0c0;color:#000}
.btn.green.on{background:#0c0;color:#000}
.btn-row{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}

/* Select */
select{background:#222;border:1px solid #444;color:#eee;padding:6px 10px;border-radius:6px;font-size:.8rem;cursor:pointer}
select:focus{outline:1px solid #ff3333}

/* Duck indicator */
.duck-indicator{padding:14px 24px;border-radius:12px;background:#151515;border:1px solid #222;font-size:1.1rem;font-weight:600;text-align:center;transition:.3s}
.duck-indicator.ducked{border-color:#ffa500;color:#ffa500}

/* Script box */
.script-box{padding:16px 24px;border-radius:12px;background:#151515;border:1px solid #222;text-align:center;transition:.3s}
.script-box.has-script{border-color:#8b5cf6;background:#1a1025}
.script-label{font-size:.7rem;color:#666;text-transform:uppercase;letter-spacing:.1em;margin-bottom:6px}
.script-text{font-size:.95rem;color:#c4b5fd;font-style:italic;line-height:1.4;min-height:1.4em}

/* Queue */
.queue-list{list-style:none;max-height:180px;overflow-y:auto;scrollbar-width:thin;scrollbar-color:#333 #0a0a0a}
.queue-list li{padding:6px 10px;border-radius:6px;font-size:.82rem;color:#aaa;border-bottom:1px solid #1a1a1a}
.queue-list li:nth-child(odd){background:#111}
.queue-list li .num{color:#555;margin-right:8px;font-size:.75rem}
.queue-empty{color:#555;font-size:.85rem;font-style:italic;padding:10px 0}

/* Status */
.status{color:#666;font-size:.78rem;margin-top:12px}

#panel-wrap{max-width:960px;width:100%}
@media(max-width:700px){.main-grid{grid-template-columns:1fr}}
</style></head><body>
<h1>Blood Mixer</h1>
<p class="subtitle">Real-time music + voice ducking dashboard</p>

<!-- Server Tabs -->
<div class="tab-bar" id="tab-bar"><div class="no-servers" id="no-srv">No active servers</div></div>

<!-- Panel (populated by JS for active guild) -->
<div id="panel-wrap"></div>

<p class="status" id="st">Connecting...</p>

<script>
const ws=new WebSocket('ws://'+location.host+'/ws');
const $=id=>document.getElementById(id);
let G={};           // guild_id -> state
let activeG=null;   // selected guild_id
let renderedG=null; // guild_id whose panel DOM is currently in place
const dragging={};  // slider id -> true while user is dragging

ws.onopen=()=>{$('st').textContent='Connected';$('st').style.color='#0f0'};
ws.onclose=()=>{$('st').textContent='Disconnected — refresh';$('st').style.color='#f55'};

function send(action,extra){
  if(!activeG){console.warn('No active guild');return}
  if(ws.readyState!==1){console.warn('WS not open');return}
  const msg=Object.assign({action,guild:activeG},extra||{});
  console.log('TX',msg);
  ws.send(JSON.stringify(msg));
}

ws.onmessage=e=>{
  const msg=JSON.parse(e.data);
  console.log('RX guilds:',Object.keys(msg.guilds||{}));
  if(!msg.guilds)return;
  G=msg.guilds;
  const ids=Object.keys(G);
  if(!activeG||!G[activeG]){activeG=ids[0]||null}
  renderTabs();
  // only rebuild DOM when guild changed; otherwise just patch values
  if(activeG!==renderedG) buildPanel();
  else if(activeG&&G[activeG]) updatePanel(G[activeG]);
};

function renderTabs(){
  const bar=$('tab-bar');
  const ids=Object.keys(G);
  if(!ids.length){bar.innerHTML='<div class="no-servers">No active servers</div>';return}
  let h='';
  ids.forEach(gid=>{
    const g=G[gid];
    const cls=gid===activeG?'tab active':'tab';
    let dot='idle';
    if(g.radio_active)dot='radio';else if(g.music&&g.music.active)dot='music';
    h+='<button class="'+cls+'" data-g="'+gid+'"><span class="t-dot '+dot+'"></span>'+esc(g.name||gid)+'</button>';
  });
  bar.innerHTML=h;
  bar.querySelectorAll('.tab').forEach(b=>b.onclick=()=>{activeG=b.dataset.g;renderTabs();buildPanel()});
}

function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML}

function buildPanel(){
  renderedG=activeG;
  const wrap=$('panel-wrap');
  if(!activeG||!G[activeG]){wrap.innerHTML='';return}

  wrap.innerHTML=`
<div class="main-grid">
<div class="card" id="m-ch">
  <div class="card-title"><div class="dot" id="m-dot"></div>Music</div>
  <div class="info" id="m-info"></div>
  <div class="meter-wrap"><div class="meter" id="m-meter"></div></div>
  <div class="slider-row"><label>Volume</label><input type="range" id="m-vol" min="0" max="100" value="80"><span id="m-vv">80%</span></div>
  <div class="slider-row"><label>Duck Level</label><input type="range" id="duck-vol" min="0" max="50" value="15"><span id="duck-vv">15%</span></div>
  <div class="btn-row">
    <button class="btn" id="pause-btn">Pause</button>
    <button class="btn" id="skip-btn">Skip</button>
    <button class="btn" id="loop-btn">Loop</button>
    <button class="btn danger" id="stop-btn">Stop</button>
  </div>
</div>
<div class="card" id="t-ch">
  <div class="card-title"><div class="dot" id="t-dot"></div>Blood (TTS)</div>
  <div class="info" id="t-info">Idle</div>
  <div class="meter-wrap"><div class="meter" id="t-meter"></div></div>
  <div class="btn-row"><button class="btn" id="duck-btn">Force Duck</button></div>
  <div class="script-box" id="script-box" style="margin-top:14px">
    <div class="script-label">Upcoming DJ Script</div>
    <div class="script-text" id="script-text">No script queued</div>
  </div>
</div>
<div class="card">
  <div class="card-title">Effects</div>
  <div class="slider-row"><label>Speed</label><input type="range" id="speed-sl" min="50" max="200" value="100" step="5"><span id="speed-vv">1.00x</span></div>
  <div class="slider-row"><label>Bass Boost</label><input type="range" id="bass-sl" min="0" max="20" value="0"><span id="bass-vv">0 dB</span></div>
  <div class="slider-row" style="margin-top:10px">
    <label>Preset</label>
    <select id="fx-sel">
      <option value="none">None</option><option value="bass">Bass</option>
      <option value="nightcore">Nightcore</option><option value="slowed">Slowed + Reverb</option>
      <option value="8d">8D</option><option value="vapor">Vaporwave</option>
      <option value="treble">Treble</option><option value="deep">Deep</option>
    </select>
    <button class="btn" id="fx-clear" style="margin-left:auto">Clear All</button>
  </div>
  <div class="info" style="margin-top:8px;font-size:.75rem" id="fx-info">No effects active</div>
</div>
<div class="card">
  <div class="card-title"><div class="dot radio" id="radio-dot" style="display:none"></div>Queue <span id="q-count" style="color:#555;font-size:.8rem;margin-left:auto">0 tracks</span></div>
  <div id="radio-badge" style="display:none;margin-bottom:10px">
    <span style="background:#1a2e1a;color:#0f0;padding:3px 10px;border-radius:6px;font-size:.75rem;border:1px solid #0c0">RADIO ON AIR</span>
  </div>
  <ul class="queue-list" id="q-list"></ul>
  <div class="queue-empty" id="q-empty">Queue is empty</div>
</div>
<div class="full-span"><div class="duck-indicator" id="duck">Music at full volume</div></div>
</div>`;

  updatePanel(G[activeG]);
  bindEvents();
}

// track which sliders are being dragged so we don't overwrite them
function trackSlider(id){
  const el=$(id);if(!el)return;
  el.addEventListener('mousedown',()=>{dragging[id]=true});
  el.addEventListener('touchstart',()=>{dragging[id]=true},{passive:true});
  const up=()=>{delete dragging[id]};
  window.addEventListener('mouseup',up);window.addEventListener('touchend',up);
}

function updatePanel(d){
  if(!d||!$('m-dot'))return;
  // Music
  if(!dragging['m-vol']){const mV=Math.round((d.music?.volume??0.5)*100);$('m-vol').value=mV;$('m-vv').textContent=mV+'%'}
  const mAct=!!d.music?.active;
  $('m-info').textContent=mAct?(d.music.title||'Playing'):'No music';
  const mDot=$('m-dot');mAct?mDot.classList.add('on'):mDot.classList.remove('on');
  $('m-meter').style.width=((d.music?.level??0)*100)+'%';
  // TTS
  const tAct=!!d.tts?.active;
  $('t-info').textContent=tAct?('"'+(d.tts.text||'').substring(0,80)+'"'):'Idle';
  const tDot=$('t-dot');tAct?tDot.classList.add('on'):tDot.classList.remove('on');
  $('t-meter').style.width=((d.tts?.level??0)*100)+'%';
  // Ducking
  const duck=$('duck');
  if(d.ducked){duck.textContent='Music DUCKED (Blood speaking)';duck.classList.add('ducked');mDot.classList.add('ducked')}
  else{duck.textContent='Music at full volume';duck.classList.remove('ducked');mDot.classList.remove('ducked')}
  $('duck-btn').classList.toggle('on',!!d.force_duck);
  // Duck vol
  if(!dragging['duck-vol']){const dv=Math.round((d.duck_vol??0.15)*100);$('duck-vol').value=dv;$('duck-vv').textContent=dv+'%'}
  // Script
  const sb=$('script-box'),st=$('script-text');
  if(d.upcoming_script){st.textContent='"'+d.upcoming_script+'"';sb.classList.add('has-script')}
  else{st.textContent='No script queued';sb.classList.remove('has-script')}
  // Paused / Loop
  $('pause-btn').textContent=d.paused?'Resume':'Pause';
  $('pause-btn').classList.toggle('on',!!d.paused);
  $('loop-btn').classList.toggle('on',!!d.loop);
  // Effects
  const fx=d.effects||{};
  if(!dragging['speed-sl']){$('speed-sl').value=Math.round((fx.speed||1)*100);$('speed-vv').textContent=(fx.speed||1).toFixed(2)+'x'}
  if(!dragging['bass-sl']){$('bass-sl').value=fx.bass_db||0;$('bass-vv').textContent=(fx.bass_db||0)+' dB'}
  if(fx.effect)$('fx-sel').value=fx.effect;
  const parts=[];
  if(fx.effect&&fx.effect!=='none')parts.push(fx.effect);
  if(fx.speed&&fx.speed!==1.0)parts.push(fx.speed.toFixed(2)+'x');
  if(fx.bass_db>0)parts.push('+'+fx.bass_db+' dB bass');
  $('fx-info').textContent=parts.length?parts.join(' | '):'No effects active';
  // Queue
  const q=d.queue||[];
  $('q-count').textContent=q.length+' track'+(q.length!==1?'s':'');
  const ql=$('q-list'),qe=$('q-empty');
  if(q.length){qe.style.display='none';ql.style.display='';ql.innerHTML=q.map((t,i)=>'<li><span class="num">'+(i+1)+'.</span>'+esc(t)+'</li>').join('')}
  else{qe.style.display='';ql.style.display='none';ql.innerHTML=''}
  // Radio
  $('radio-dot').style.display=d.radio_active?'':'none';
  $('radio-badge').style.display=d.radio_active?'':'none';
}

function bindEvents(){
  // sliders — track drag state + send on input
  ['m-vol','duck-vol','speed-sl','bass-sl'].forEach(trackSlider);
  $('m-vol').oninput=e=>{$('m-vv').textContent=e.target.value+'%';send('vol',{v:e.target.value/100})};
  $('duck-vol').oninput=e=>{$('duck-vv').textContent=e.target.value+'%';send('duck_vol',{v:e.target.value/100})};
  // buttons — give instant local feedback + send
  $('duck-btn').onclick=()=>{
    const on=$('duck-btn').classList.toggle('on');
    send(on?'duck':'unduck');
  };
  $('pause-btn').onclick=()=>{
    const isPause=$('pause-btn').textContent==='Pause';
    $('pause-btn').textContent=isPause?'Resume':'Pause';
    $('pause-btn').classList.toggle('on',isPause);
    send(isPause?'pause':'resume');
  };
  $('skip-btn').onclick=()=>{
    $('skip-btn').textContent='Skipping...';
    send('skip');
    setTimeout(()=>{if($('skip-btn'))$('skip-btn').textContent='Skip'},2000);
  };
  $('stop-btn').onclick=()=>{
    if(confirm('Stop music and clear queue?'))send('stop');
  };
  $('loop-btn').onclick=()=>{
    const on=!$('loop-btn').classList.contains('on');
    $('loop-btn').classList.toggle('on',on);
    send('loop',{v:on});
  };
  // effects
  $('speed-sl').oninput=e=>{const v=e.target.value/100;$('speed-vv').textContent=v.toFixed(2)+'x';send('speed',{v})};
  $('bass-sl').oninput=e=>{$('bass-vv').textContent=e.target.value+' dB';send('bass',{v:parseInt(e.target.value)})};
  $('fx-sel').onchange=e=>send('effect',{v:e.target.value});
  $('fx-clear').onclick=()=>send('clear_fx');
}
</script></body></html>"""

# ═══════════════════════════════════════════════════════════════════════
# Per-guild state
# ═══════════════════════════════════════════════════════════════════════

_states = {}          # guild_id -> state dict
_guild_names = {}     # guild_id -> server display name
_clients = set()
_loop = None


def _default_state():
    return {
        "music": {"active": False, "title": "", "level": 0.0, "volume": 0.5},
        "tts": {"active": False, "text": "", "level": 0.0},
        "ducked": False, "force_duck": False, "duck_vol": 0.15,
        "upcoming_script": "",
        "paused": False, "loop": False, "radio_active": False,
        "effects": {"speed": 1.0, "bass_db": 0, "treble_db": 0, "effect": "none"},
        "queue": [],
    }


def _ensure_guild(guild_id):
    """Make sure a state dict exists for this guild, return it."""
    if guild_id and guild_id not in _states:
        _states[guild_id] = _default_state()
    return _states.get(guild_id)


def bind_guild(guild_id, name=None):
    """Register or update a guild.  Called when Blood joins a VC."""
    _ensure_guild(guild_id)
    if name:
        _guild_names[guild_id] = name
    _broadcast()


def unbind_guild(guild_id):
    """Remove a guild.  Called when Blood leaves a VC."""
    _states.pop(guild_id, None)
    _guild_names.pop(guild_id, None)
    _broadcast()


def update_state(guild_id=None, music_active=None, music_title=None,
                 music_level=None, music_volume=None, tts_active=None,
                 tts_text=None, tts_level=None, ducked=None,
                 force_duck=None, upcoming_script=None, duck_vol=None,
                 paused=None, loop=None, radio_active=None, effects=None,
                 queue=None):
    """Update a single guild's state then broadcast all guilds."""
    # resolve guild_id: explicit > only-one-guild fallback
    if guild_id is None:
        if len(_states) == 1:
            guild_id = next(iter(_states))
        else:
            return
    st = _ensure_guild(guild_id)
    if st is None:
        return
    if music_active is not None: st["music"]["active"] = music_active
    if music_title is not None: st["music"]["title"] = music_title
    if music_level is not None: st["music"]["level"] = music_level
    if music_volume is not None: st["music"]["volume"] = music_volume
    if tts_active is not None: st["tts"]["active"] = tts_active
    if tts_text is not None: st["tts"]["text"] = tts_text
    if tts_level is not None: st["tts"]["level"] = tts_level
    if ducked is not None: st["ducked"] = ducked
    if force_duck is not None: st["force_duck"] = force_duck
    if duck_vol is not None: st["duck_vol"] = duck_vol
    if upcoming_script is not None: st["upcoming_script"] = upcoming_script
    if paused is not None: st["paused"] = paused
    if loop is not None: st["loop"] = loop
    if radio_active is not None: st["radio_active"] = radio_active
    if effects is not None: st["effects"] = effects
    if queue is not None: st["queue"] = queue
    _broadcast()


# ═══════════════════════════════════════════════════════════════════════
# Broadcast
# ═══════════════════════════════════════════════════════════════════════

def _build_payload():
    """Build the JSON payload: { guilds: { id: {name, ...state}, … } }"""
    guilds = {}
    for gid, st in _states.items():
        entry = dict(st)
        entry["name"] = _guild_names.get(gid, gid)
        guilds[gid] = entry
    return json.dumps({"guilds": guilds})


def _broadcast():
    if not _loop or _loop.is_closed():
        return
    data = _build_payload()
    coro = _broadcast_async(data)
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is _loop:
        asyncio.create_task(coro)
    else:
        asyncio.run_coroutine_threadsafe(coro, _loop)


async def _broadcast_async(data):
    dead = set()
    for ws in list(_clients):
        try:
            await ws.send_str(data)
        except Exception:
            dead.add(ws)
    for ws in dead:
        _clients.discard(ws)


# ═══════════════════════════════════════════════════════════════════════
# Helpers — resolve guild objects / mixer for WS actions
# ═══════════════════════════════════════════════════════════════════════

def _get_mixer(guild_id):
    if not guild_id:
        return None
    try:
        from voice import get_music_queue
        mq = get_music_queue(guild_id)
        mixer = mq._mixer
        log.debug("_get_mixer(%s): mixer=%s has_music=%s", guild_id, mixer is not None,
                  mixer.has_music if mixer else 'N/A')
        return mixer
    except Exception as e:
        log.warning("_get_mixer(%s) failed: %s", guild_id, e)
        return None


def _get_bot():
    """Get the actual running bot instance (handles __main__ vs module import)."""
    import sys
    main = sys.modules.get("__main__")
    if main and hasattr(main, "bot") and main.bot is not None:
        return main.bot
    try:
        from bot import bot
        return bot
    except Exception:
        return None


def _get_guild(guild_id):
    if not guild_id:
        return None
    try:
        bot = _get_bot()
        if not bot:
            log.warning("_get_guild(%s): could not get bot instance", guild_id)
            return None
        g = bot.get_guild(int(guild_id))
        if g:
            vc = g.voice_client
            log.debug("_get_guild(%s): guild=%s vc=%s playing=%s paused=%s",
                      guild_id, g.name, vc is not None,
                      vc.is_playing() if vc else 'N/A',
                      vc.is_paused() if vc else 'N/A')
        else:
            log.warning("_get_guild(%s): bot.get_guild returned None (bot has %d guilds)",
                        guild_id, len(bot.guilds))
        return g
    except Exception as e:
        log.warning("_get_guild(%s) failed: %s", guild_id, e)
        return None


def _sync_queue_for(guild_id):
    st = _states.get(guild_id)
    if not st:
        return
    try:
        from voice import get_music_queue
        mq = get_music_queue(guild_id)
        st["queue"] = [t.title for t in mq.queue[:20]]
    except Exception:
        pass


def _sync_effects_for(guild_id):
    st = _states.get(guild_id)
    mixer = _get_mixer(guild_id)
    if st and mixer:
        st["effects"] = {
            "speed": mixer._speed, "bass_db": mixer._bass_db,
            "treble_db": mixer._treble_db, "effect": mixer._effect,
        }


# ═══════════════════════════════════════════════════════════════════════
# WebSocket handler
# ═══════════════════════════════════════════════════════════════════════

async def handle_ws(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    _clients.add(ws)
    # sync all guilds before initial send
    for gid in list(_states):
        _sync_queue_for(gid)
        _sync_effects_for(gid)
    await ws.send_str(_build_payload())

    async for msg in ws:
        if msg.type == WSMsgType.TEXT:
            try:
                d = json.loads(msg.data)
                action = d.get("action")
                gid = d.get("guild")  # which server the client targets
                log.info("WS action=%s guild=%s data=%s", action, gid, {k:v for k,v in d.items() if k not in ('action','guild')})
                if not gid:
                    continue

                if action == "vol":
                    vol = max(0.0, min(1.0, float(d.get("v", 0.5))))
                    try:
                        from voice import set_music_volume
                        set_music_volume(gid, vol)
                    except Exception:
                        pass
                    st = _states.get(gid)
                    update_state(gid, music_level=vol if (st and st["music"]["active"]) else 0.0, music_volume=vol)

                elif action == "duck_vol":
                    dv = max(0.0, min(0.5, float(d.get("v", 0.15))))
                    mixer = _get_mixer(gid)
                    if mixer:
                        mixer._duck_vol = dv
                    update_state(gid, duck_vol=dv)

                elif action == "duck":
                    mixer = _get_mixer(gid)
                    if mixer:
                        mixer.set_force_duck(True)
                        update_state(gid, ducked=bool(mixer.has_music), force_duck=True)

                elif action == "unduck":
                    mixer = _get_mixer(gid)
                    if mixer:
                        mixer.set_force_duck(False)
                        mixer._tts_active = False
                        mixer._tts_buf.clear()
                        mixer._duck_since = 0.0
                    update_state(gid, ducked=False, force_duck=False,
                                 tts_active=False, tts_text="", tts_level=0.0)

                elif action == "skip":
                    guild = _get_guild(gid)
                    if guild:
                        from voice import skip_music
                        asyncio.create_task(skip_music(guild))

                elif action == "pause":
                    guild = _get_guild(gid)
                    if guild and guild.voice_client:
                        vc = guild.voice_client
                        log.info("PAUSE: vc.is_playing=%s vc.is_paused=%s", vc.is_playing(), vc.is_paused())
                        if vc.is_playing():
                            vc.pause()
                            log.info("PAUSE: paused OK")
                        else:
                            log.warning("PAUSE: vc not playing")
                    else:
                        log.warning("PAUSE: guild=%s vc=%s", guild is not None,
                                    guild.voice_client is not None if guild else 'no guild')
                    update_state(gid, paused=True)

                elif action == "resume":
                    guild = _get_guild(gid)
                    if guild and guild.voice_client:
                        vc = guild.voice_client
                        log.info("RESUME: vc.is_playing=%s vc.is_paused=%s", vc.is_playing(), vc.is_paused())
                        if vc.is_paused():
                            vc.resume()
                            log.info("RESUME: resumed OK")
                        else:
                            log.warning("RESUME: vc not paused")
                    else:
                        log.warning("RESUME: guild=%s vc=%s", guild is not None,
                                    guild.voice_client is not None if guild else 'no guild')
                    update_state(gid, paused=False)

                elif action == "stop":
                    guild = _get_guild(gid)
                    if guild:
                        from voice import stop_music, stop_radio
                        log.info("STOP: stopping radio + music for guild %s", gid)
                        await stop_radio(gid)
                        await stop_music(guild)
                        update_state(gid, paused=False, radio_active=False, queue=[])
                        log.info("STOP: done")
                    else:
                        log.warning("STOP: _get_guild returned None for %s", gid)

                elif action == "loop":
                    try:
                        from voice import get_music_queue
                        mq = get_music_queue(gid)
                        mq.loop = bool(d.get("v", False))
                        update_state(gid, loop=mq.loop)
                    except Exception:
                        pass

                elif action == "speed":
                    spd = max(0.5, min(2.0, float(d.get("v", 1.0))))
                    mixer = _get_mixer(gid)
                    if mixer:
                        mixer.set_speed(spd)
                        _sync_effects_for(gid)
                        _broadcast()

                elif action == "bass":
                    db = max(0, min(20, int(d.get("v", 0))))
                    mixer = _get_mixer(gid)
                    if mixer:
                        mixer.set_bass_boost(db)
                        _sync_effects_for(gid)
                        _broadcast()

                elif action == "effect":
                    fx = str(d.get("v", "none"))
                    mixer = _get_mixer(gid)
                    if mixer:
                        mixer.set_effect(fx)
                        _sync_effects_for(gid)
                        _broadcast()

                elif action == "clear_fx":
                    mixer = _get_mixer(gid)
                    if mixer:
                        mixer.clear_effects()
                        _sync_effects_for(gid)
                        _broadcast()

            except Exception as exc:
                log.warning("WS action error: %s", exc, exc_info=True)
    _clients.discard(ws)
    return ws


app = web.Application()
app.router.add_get("/", lambda r: web.Response(text=INDEX_HTML, content_type="text/html"))
app.router.add_get("/ws", handle_ws)

_server = None


async def start_web_mixer(port=7777):
    global _server, _loop
    if _server:
        return
    _loop = asyncio.get_running_loop()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    _server = runner
    log.info("Mixer web UI running at http://localhost:%d", port)
