# Blood — Discord AI Agent Bot

## What this is
Blood is a full-featured Discord AI agent with voice presence, real-time audio mixing, AI-powered music DJ, moderation, coin economy, stock market trading, web browsing, and persistent memory. Supports STT/TTS for live voice conversations with simultaneous music playback.

## Tech stack
- **Runtime**: Python 3.14 + discord.py 2.7 (with DAVE encryption)
- **AI**: Fireworks AI (Kimi K2.5) primary / Groq fallback chain
- **Voice**: Groq Whisper (STT) + Edge-TTS (speech) + discord-ext-voice-recv
- **Audio Mixer**: Thread-based PCM mixer with live ducking (`mixer.py`)
- **Music**: yt-dlp (YouTube/Spotify/SoundCloud) + AI-powered recommendations
- **Finance**: Yahoo Finance (`yfinance`) — real-time prices
- **Search**: Tavily — web search/scraping
- **Memory**: ChromaDB + sentence-transformers (vector) + local file-based (no external DB)

## File structure
```
bot.py          — Main bot, event loop, commands, agentic tool-call loop
voice.py        — Voice: STT, TTS, music, DJ, AI intent, transcripts, taste learning
mixer.py        — Thread-based real-time audio mixer (music + TTS ducking)
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
| `/randommusic` | Start DJ mode — Blood picks music based on your taste |
| `/stopdj` | Leave the DJ rotation |
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
- **TTS feed**: FFmpeg subprocess → reader thread → deque buffer
- **Mixing**: Discord's AudioPlayer calls `read()` every 20ms, mixes both PCM streams
- **Auto-ducking**: Music volume drops to 15% when Blood speaks, smoothly restores after
- **Back-pressure**: Reader thread pauses when buffer exceeds 50 frames to prevent memory bloat
- **Web UI**: Real-time mixer dashboard at `localhost:7777` with live VU meters

### AI Music Intent Classification
Instead of rigid keyword matching, Blood uses AI to understand natural language music commands:
- `"this slaps"` / `"banger"` → **like** (records positive feedback)
- `"nah this ain't it"` / `"mid"` → **dislike** (records negative + auto-skips)
- `"next"` / `"play something else"` → **skip**
- `"play Blinding Lights"` → **play:Blinding Lights**
- Only runs when music is playing or text contains music-related words (saves API calls)

### AI-Powered Music Recommendations
Like Spotify/YouTube Music — uses AI to generate batches of 15 songs:
- Sends user's liked/disliked/recently played history to LLM
- AI generates diverse recommendations: 60% similar, 30% discovery, 10% wildcard
- Batched: generates 15 songs at once, caches them, only calls AI every ~15 tracks
- Recently played list (last 30) prevents repeats across batches
- Filters: max 15min duration, skips covers/karaoke/audiobooks/podcasts
- Search appends `"audio"` keyword to prefer audio uploads over music videos

### Random DJ System (`/randommusic`)
- Per-user taste profiles stored in `data/music_taste/`
- Songs you request are auto-liked
- Like/dislike via voice, slash commands, or 👍/👎 reactions
- **Multi-user priority rotation**: first user gets N songs (N = total users), others get 1 each
- Users auto-removed from DJ when they leave VC

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
FIREWORKS_API_KEY=...
TAVILY_API_KEY=...    # optional, for web search
```
