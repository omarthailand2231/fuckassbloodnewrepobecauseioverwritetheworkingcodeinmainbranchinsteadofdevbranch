# Blood — Discord AI Agent Bot

## What this is
Blood is a full-featured Discord AI agent with voice presence, real-time audio mixing, AI-powered music radio with a DJ persona, moderation, coin economy, stock market trading, web browsing, and persistent memory. Supports STT/TTS for live voice conversations with simultaneous music playback and smooth audio transitions.

## Tech stack
- **Runtime**: Python 3.14 + discord.py 2.7 (with DAVE encryption)
- **AI**: Fireworks AI (Kimi K2.5) primary / Groq fallback chain
- **Voice**: Groq Whisper (STT) + Edge-TTS (all speech) + discord-ext-voice-recv
- **Audio Mixer**: Thread-based PCM mixer with smooth volume fades, live ducking, gapless prebuffering (`mixer.py`)
- **Music**: yt-dlp (YouTube/Spotify/SoundCloud) + AI-powered recommendations
- **Finance**: Yahoo Finance (`yfinance`) — real-time prices
- **Search**: Tavily — web search/scraping
- **Memory**: ChromaDB + sentence-transformers (vector) + local file-based (no external DB)

## File structure
```
bot.py          — Main bot, event loop, commands, agentic tool-call loop
voice.py        — Voice: STT, TTS, music, DJ, Radio DJ, AI intent, transcripts, taste learning
mixer.py        — Thread-based real-time audio mixer (music + TTS ducking + smooth fades)
web_mixer.py    — Mixer web UI dashboard (localhost:7777)
tools.py        — Tool definitions + executors (30+ tools)
provider.py     — Fireworks/Groq/DeepSeek API client with smart fallback + vision
config.py       — Permission tiers, role IDs, tool gates, model chain
memory.py       — Memory manager (RAM + channels + summaries + coins + market)
market.py       — Real-time stock/crypto/commodity prices + charts
mood.py         — Blood's emotional state (resets on restart)
memory/
  <guild_id>/
    channels/<channel_id>.md — Per-channel rolling chat log
    memory.md / memory_2.md  — Summaries + pinned facts
    actions.md               — Immutable moderation action ledger
    users.xml                — User interaction graph
    coins.json / market.json — Economy data
data/
  transcripts/<guild_id>/    — VC session transcripts (JSON)
  music_taste/<user_id>.json — Per-user music taste profiles
```

## Memory system (multi-tier)

### Per-channel logs (`channels/<channel_id>.md`)
- Every message gets logged to its channel's own file
- Format: `[YYYY-MM-DD HH:MM] Username: message`
- Trimmed to last 200 lines per channel
- NOT injected into every prompt — Blood uses `recall_memory` tool to search on demand

### Actions Ledger (`actions.md`)
- Permanent timestamped log of every successful `timeout`, `ban`, `kick`, `unmute`, and `delete_messages`
- Automatically searched by `recall_memory` so Blood can definitively answer "Why did you ban X?"

### memory_2.md (summaries)
- AI writes here via `save_summary` tool
- This IS injected into every system prompt (capped at 400 chars)

### users.xml (relationships)
- Tracks interaction graph: who interacted with whom and how many times. Updated on every message.

## Permission tiers
```
blacklisted < user < mod < admin < owner
```

### Autonomous tools (no permission required from invoker)
Blood can use these on its own judgment:
- `timeout_user` — max 2 minutes when autonomous
- `recall_memory` — search the logs (uses smart ALL-word contextual matching)
- `get_user_info` — look someone up
- `save_summary` — pin a fact
- `web_search` — natively pulls live search results
- `image_search` — find and send images
- `analyze_image` — process attached image URLs via Vision AI
- `internal_reasoning` — silent reasoning step
- `give_coins` — reward or punish users with BHC coins (Blood decides)

## Commands

### General
| Command | Description |
|---------|-------------|
| `@Blood <message>` | Talk to Blood (main interaction) |
| `/help` | Show all commands |
| `/reset` | Clear Blood's short-term memory for the channel |

### Voice Channel
| Command | Description |
|---------|-------------|
| `/joinvc [channel]` | Join VC with STT listening + TTS. Omit channel to auto-join yours |
| `/leavevc` | Leave VC and save transcript |
| `/transcript [page]` | View recent VC transcripts (5/page) with .txt export + summary |

### Music
| Command | Description |
|---------|-------------|
| `/play <song/url>` | Play from YouTube/Spotify/SoundCloud (auto-joins VC) |
| `/skip` | Skip current song |
| `/stop` | Stop playback and clear queue |
| `/queue` | Show current queue |
| `/np` | Show what's currently playing |
| `/volume <0-100>` | Set music volume |
| `/radio` | Start Blood Radio — AI DJ plays indie/chill music with commentary |
| `/stopradio` | Stop Blood Radio |
| `/like` | Like current song (improves your recommendations) |
| `/dislike` | Dislike current song (skips + adjusts taste) |

### Economy
| Command | Description |
|---------|-------------|
| `/bal` | Check BHC coin balance |
| `/pay @user <amount>` | Transfer coins |
| `/daily` | Claim daily coins |
| `/leaderboard` | Top 10 richest users |
| `/market <ticker>` | View stock/crypto chart |
| `/buy <ticker> <amount>` | Invest BHC coins |
| `/sell <ticker>` | Sell position |
| `/portfolio` | View holdings + P&L |

### Admin
| Command | Description |
|---------|-------------|
| `/openterminal` | Open remote terminal session |
| `/closeterminal` | Close remote terminal session |

## Voice System (`voice.py`)

### How it works
1. Blood joins VC via `/joinvc` or when asked
2. Listens to all users via `discord-ext-voice-recv`
3. Buffers per-user PCM audio, detects silence gaps (1.5s)
4. Sends audio to **Groq Whisper** for transcription
5. Checks for wake words (`"blood"`, `"hey blood"`, `"เลือด"`, `"บลัด"`)
6. AI classifies intent (music command? general chat?)
7. Generates short response (1-2 sentences max — no yapping)
8. Converts to speech via **Edge-TTS** and plays in VC

### Text+VC Dual Reply
If a user is in VC with Blood but chats in text, Blood replies in text **and** speaks via TTS simultaneously.

### Thread-Based Audio Mixer (`mixer.py`)
- **Music feed**: FFmpeg subprocess → reader thread → deque buffer
- **TTS feed**: FFmpeg subprocess → PCM frames → deque buffer
- **Mixing**: Discord's AudioPlayer calls `read()` every 20ms, mixes both PCM streams in real-time
- **Smooth fades**: All volume transitions are interpolated — no instant jumps
- **Duck fade**: ~0.4s fade-down when Blood speaks, ~0.7s fade-up after speech ends
- **Pre-fade**: Unduck begins 500ms *before* TTS ends for a seamless crossover
- **Song fade-in**: Every new track fades in from silence (~0.5s) instead of starting abruptly
- **Stuck-ducking guard**: `feed_tts_sync()` uses `try/finally` to guarantee ducking is always cleared
- **Back-pressure**: Reader thread pauses when buffer exceeds 250 frames to prevent memory bloat
- **Web UI**: Real-time mixer dashboard at `localhost:7777` with live VU meters

### Smooth Volume Fade Constants
```
_DUCK_RATE        = 0.030  # per read() call — full duck in ~0.4s
_UNDUCK_RATE      = 0.018  # per read() call — full unduck in ~0.7s
_SONG_FADEIN_RATE = 0.025  # per read() call — full fade-in in ~0.5s
_PRE_FADE_FRAMES  = 25     # start unduck this many frames before TTS ends (~500ms)
```

### Blood Radio (`/radio`)
Blood becomes a radio host — plays background/indie/chill music and talks between songs like a real DJ.

- **Instant startup**: `/radio` responds immediately. Song loading, preloading, and voice warmup all happen in background tasks.
- **Parallel preloading**: Fetches `RADIO_QUEUE_TARGET+1` (4) songs **simultaneously** at startup via `asyncio.gather()`. Startup waits for the slowest single extraction, not 4 in a row.
- **3-song queue buffer**: Always maintains 3 songs pre-extracted and ready. Refill runs in background after every track change.
- **Parallel refill**: `_fill_radio_queue()` gets multiple song queries then extracts them in parallel. Uses `asyncio.Lock` to prevent concurrent over-fills.
- **Gapless transitions**: Pre-buffers next track's PCM into memory while current song plays. `start_music()` handoffs the pre-buffered FFmpeg process — zero gap.
- **AI commentary cadence**: Speaks after every 1–5 songs (randomized). Commentary uses time-of-day, track transition context, listener chat.
- **Queue-stale detection**: If songs are in queue but nothing is playing (e.g. after a `/play` interruption), `_radio_loop` detects and restarts playback within 3 seconds.
- **Task tracking**: `RadioDJ._loop_task` and `RandomDJ._loop_task` store the live asyncio Task so auto-rejoin never spawns duplicate loops.
- **Voice**: Edge-TTS (ElevenLabs optional — swap `text_to_speech()` to `text_to_speech_elevenlabs()` in `_speak_radio()` when paid plan active)

### AI Music Intent Classification
Instead of rigid keyword matching, Blood uses AI to understand natural language music commands:
- `"this slaps"` / `"banger"` → **like** (records positive feedback)
- `"nah this ain't it"` / `"mid"` → **dislike** (records negative + auto-skips)
- `"next"` / `"play something else"` → **skip**
- `"play Blinding Lights"` → **play:Blinding Lights**
- Only runs when music is playing or text contains music-related words (saves API calls)

### AI-Powered Music Recommendations
- Sends user's liked/disliked/recently played history to LLM
- AI generates diverse recommendations: 60% similar, 30% discovery, 10% wildcard
- Batched: generates 15 songs at once, caches them, only calls AI every ~15 tracks
- Recently played list (last 30) prevents repeats across batches
- Filters: max 15min duration, skips covers/karaoke/audiobooks/podcasts

### Auto-Rejoin After Disconnect
- `_rejoining_guilds` set debounces concurrent rejoin triggers — Discord's 4006 reconnect storm fires `on_voice_state_update` multiple times, but only the first attempt runs
- Radio/DJ loops only restart if their `asyncio.Task` is confirmed dead (`_loop_task.done()`)
- `join_and_listen()` calls `stop_listening()` before re-attaching a new listener, handles "already receiving" gracefully

## Key behaviors
- **Temporal Awareness**: Real-time UTC clock injected into every prompt
- **Auto Loop Detection**: Hard block after 3 identical tool calls
- **Deep Thread Crawling**: Recurses up to 3 reply levels for context
- **Thought leak prevention**: Discards leaked reasoning steps
- **Mod action verification**: Retries up to 4 times
- **Fuzzy channel matching**: Handles Unicode fonts, special chars, partial names
- **Always-on voice buffering**: Audio is never dropped even while STT is processing

## Environment variables
```
DISCORD_TOKEN=...
GROQ_API_KEY=...
MOONSHOT_API_KEY=...          # Fireworks AI key (fw_...)
TAVILY_API_KEY=...            # optional, for web search
ELEVENLABS_API_KEY=...        # optional, enables ElevenLabs TTS for radio DJ
ELEVENLABS_RADIO_VOICE_ID=... # optional, ElevenLabs voice ID for radio host
SPOTIFY_CLIENT_ID=...         # optional, enables Spotify mood-based recommendations
SPOTIFY_CLIENT_SECRET=...
```
