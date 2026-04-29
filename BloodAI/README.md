# Blood — Discord AI Agent Bot

## What this is
Blood is a full-featured Discord AI agent with voice presence, music DJ, moderation, coin economy, stock market trading, web browsing, and persistent memory. Supports STT/TTS for live voice conversations.

## Tech stack
- Python 3.14 + discord.py 2.7 (with DAVE encryption)
- Fireworks AI (Kimi K2.5) primary / Groq fallback chain
- Groq Whisper (STT) + Edge-TTS (speech)
- yt-dlp (YouTube/Spotify/SoundCloud music)
- discord-ext-voice-recv (voice listening)
- Yahoo Finance (`yfinance`) — real-time prices
- Tavily — web search/scraping
- ChromaDB + sentence-transformers — vector memory
- Local file-based memory (no external DB)

## File structure
```
bot.py          — Main bot, event loop, commands, agentic tool-call loop
voice.py        — Voice system: STT, TTS, music, DJ, transcripts, taste learning
tools.py        — Tool definitions + executors (30+ tools)
provider.py     — Fireworks/Groq API client with smart fallback + vision
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
- Permanent timestamped log of every successful `timeout`, `ban`, `kick`, `unmute`, and `delete_messages`.
- Automatically searched by `recall_memory` so Blood can definitively answer "Why did you ban X?"

### memory_2.md (summaries)
- AI writes here via `save_summary` tool.
- This IS injected into every system prompt (capped at 400 chars).

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
6. Generates short response (1-2 sentences max — no yapping)
7. Converts to speech via **Edge-TTS** and plays in VC

### Text+VC Dual Reply
If a user is in VC with Blood but chats in text, Blood replies in text **and** speaks via TTS simultaneously.

### Music Playback
- **yt-dlp** extracts audio from YouTube (priority), Spotify, SoundCloud
- Queue system with auto-advance
- Volume ducking: music lowers to 15% when Blood speaks, restores after

### Random DJ System (`/randommusic`)
- Per-user taste profiles stored in `data/music_taste/`
- Songs you request are auto-liked
- `/like` and `/dislike` (or voice: "I like this", "this sucks") shape recommendations
- **Multi-user priority rotation**: first user gets N songs (N = total users), others get 1 each, then loop
- Users auto-removed from DJ when they leave VC
- 👍/👎 reactions on DJ messages also count as feedback

## Key behaviors
- **Temporal Awareness**: Real-time UTC clock injected into every prompt
- **Auto Loop Detection**: Hard block after 3 identical tool calls
- **Deep Thread Crawling**: Recurses up to 3 reply levels for context
- **Thought leak prevention**: Discards leaked reasoning steps
- **Mod action verification**: Retries up to 4 times
- **Fuzzy channel matching**: Handles Unicode fonts, special chars, partial names

## Environment variables
```
DISCORD_TOKEN=...
GROQ_API_KEY=...
FIREWORKS_API_KEY=...
```
