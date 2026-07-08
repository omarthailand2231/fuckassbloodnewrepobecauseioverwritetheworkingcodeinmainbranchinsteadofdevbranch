# Blood — Discord AI Agent Bot

> **Persona:** the bot now runs the **Claude Fable 6** personality (warm, helpful, honest, direct) and is live in Discord as **Clawd**. "Blood" remains the codebase/project name (repo, classes, internal identifiers); the user-facing assistant is Claude. The persona is defined in `personality_fable5.md` and loaded by `build_system_prompt()` in `bot.py`.

## What this is
A full-featured Discord AI agent with voice presence, real-time audio mixing, an AI music radio with a DJ host, moderation, a coin economy, stock-market trading, web browsing, image vision, and persistent memory. Supports optional STT + TTS for live voice conversations with simultaneous music playback and smooth audio transitions.

## Tech stack
- **Runtime**: Python 3.14 + discord.py 2.7 (+ discord-ext-voice-recv)
- **AI (chat + vision)**: 9arm gateway `qwen3.6-35b-a3b` (OpenAI-compatible) — **primary**, with automatic failover to Xiaomi MiMo → Fireworks/Moonshot → Groq. Vision runs on the same gateway (qwen is multimodal) with a Groq `llama-4-scout` fallback.
- **Voice**: Groq Whisper (STT — optional, `STT_ENABLED`) + Edge-TTS (general speech) + ElevenLabs (radio DJ voice, "George") + discord-ext-voice-recv
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
tools.py             — Tool definitions + executors (50+ tools)
provider.py          — Multi-provider chat client: gateway → MiMo → Moonshot failover + vision
config.py            — Permission tiers, role IDs, tool gates, provider + feature toggles
personality_fable5.md— Claude Fable 6 base system prompt (the bot's personality)
memory.py            — Memory manager (RAM + channels + summaries + coins + market)
market.py            — Real-time stock/crypto/commodity prices + charts
mood.py              — Emotional-state helpers (resets on restart)
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
| `/joinvc [channel]` | Join VC (TTS + music; live STT listening only when `STT_ENABLED=true`). Omit channel to auto-join yours |
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
| `/radio` | Start Clawd Radio — AI DJ plays indie/chill music with commentary (ElevenLabs voice) |
| `/stopradio` | Stop Clawd Radio |
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
| `/hotreload` (`/reboot`, `/restart`) | **Owner only.** Restart the bot in-place with fresh code via `os.execv` (same terminal/PID; reloads code + `.env`). The new "online" log is the restart confirmation. |

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

### Clawd Radio (`/radio`)
Clawd becomes a radio host in a dedicated thread — plays taste-aware music, shows a live player panel, and talks between songs like a real DJ.

- **Dedicated thread + live player panel** (`radio_panel.py`): `/radio` opens a `📻 Clawd Radio` thread and drops a single control panel into it — cover art, track name, a progress bar that ticks every 5s, and **Like / Dislike / Skip / Pause / Stop** buttons.
- **Sticks to the bottom**: any message in the thread (from a user **or** the bot) deletes and re-sends the panel so it always sits at the bottom, like a pinned now-playing bar. A self-id guard + debounce prevent repost loops.
- **Taste-aware curation**: `_radio_recommendations()` blends the liked songs of **whoever is currently in the VC** (`_collect_room_taste()` over `_listener_ids`), mixes ~60% taste / ~30% discovery / ~10% wildcard, and reads the room (time of day + recent chat via `_radio_vibe_hint()`) so the set leans toward the current mood.
- **Dislike stacks; skip doesn't**: `record_feedback()` accumulates `dislike_weights` per song and escalates to the **whole artist** once dislikes stack (≥2). A **skip is just "not in the mood"** → per-session cooldown (`record_radio_skip()`), never a taste change. An explicit like overrides artist-level avoidance. A hard gate (`_radio_passes()`) drops disliked / skipped / recently-played songs before they can queue.
- **Per-guild history**: recently-played is keyed per guild, so one server never suppresses another's songs.
- **Instant startup / parallel preload / 3-song buffer / gapless transitions**: unchanged — startup waits for the slowest single extraction (`asyncio.gather()`), `_fill_radio_queue()` refills in parallel under a lock, and `start_music()` hands off a pre-buffered FFmpeg process for zero-gap transitions.
- **Cover art**: `extract_track()` captures the yt-dlp thumbnail onto `MusicTrack.thumbnail` for the panel image.
- **Voice**: **ElevenLabs** ("George", `eleven_multilingual_v2`) via `text_to_speech_elevenlabs()`, with Edge-TTS as automatic fallback if ElevenLabs errors or no key is set

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

# ── Chat + vision provider (PRIMARY): 9arm gateway, OpenAI-compatible ──
GATEWAY_API_KEY=...
GATEWAY_BASE_URL=https://gateway.9arm.co/v1
GATEWAY_MODEL=qwen3.6-35b-a3b
USE_GATEWAY_API=true

# ── Fallback providers (used automatically, in order, if the gateway is down) ──
MIMO_API_KEY=...              # Xiaomi MiMo (OpenAI-compatible)
MIMO_BASE_URL=https://token-plan-sgp.xiaomimimo.com/v1
MIMO_MODEL=mimo-v2.5-pro
MOONSHOT_API_KEY=...          # Fireworks AI key (fw_...) — Kimi
GROQ_API_KEY=...              # Groq — also the VISION fallback (llama-4-scout) + Whisper STT

# ── Feature toggles ──
STT_ENABLED=false             # voice-channel transcription (Groq Whisper). false = music/TTS only, no audio receive
MEMES_ENABLED=false           # auto reaction GIFs/memes after replies

# ── Optional integrations ──
TAVILY_API_KEY=...            # web search
ELEVENLABS_API_KEY=...        # radio DJ voice ("George"); falls back to Edge-TTS if unset/erroring
ELEVENLABS_RADIO_VOICE_ID=JBFqnCBsd6RMkjVDRZzb
SPOTIFY_CLIENT_ID=...         # mood-based music recommendations
SPOTIFY_CLIENT_SECRET=...
```
> Note: `.env` is git-ignored (secrets stay local). See `.env.example` for the template.

## Configuration & feature toggles
| Var | Default | Effect |
|-----|---------|--------|
| `USE_GATEWAY_API` | `true` | Use the 9arm gateway as the primary chat/vision provider. If false (and no `GATEWAY_API_KEY`), falls back to MiMo/Moonshot. |
| `STT_ENABLED` | `true` | When `false`, the bot joins voice for **music + TTS only** — it does not attach the audio receiver, so there's no opus decoding or transcription (and no related log noise). |
| `MEMES_ENABLED` | `true` | When `false`, no auto reaction GIF/meme after replies and the meme list is dropped from the prompt. |

### Provider failover (`call_ai`)
Chat tries providers in order — **Gateway → MiMo → Moonshot** — failing over fast (short retry budget on non-final providers, raise-on-last-attempt) so a gateway outage (e.g. HTTP 502) doesn't take the bot down. If all fail, it returns a graceful "providers are down" message instead of throwing. The gateway stays first, so it automatically resumes once healthy. **Vision** (`call_vision` / `call_fast_vision`) uses the gateway first, then Groq `llama-4-scout`.

## Recent changes
- **New `ask_user` tool**: lets the AI pause mid-task and ask a genuine clarifying question. Posts a thread (or replies directly in DMs/existing threads) with 2-5 clickable option buttons plus an "Other / custom answer" button (opens a modal, freeform text — the calling AI interprets things like "1 + 3, I'd rather do both" itself from the raw answer). The user can also just reply in the thread instead of clicking; `bot.py`'s `on_message` routes those replies to `_handle_ask_user_reply`, which asks the model whether a final decision has been reached (`DECIDED: ...`) or the discussion should continue (`REPLY: ...`). Times out after `ask_user_timeout_sec` (default 30 min) if nobody answers.
- **Goals flipped from AI-initiated to user-initiated**: `set_goal` is no longer an AI tool. Instead, `/set_goal <text>` (any user) creates the goal and immediately kicks off a dedicated work loop (`_run_goal_loop` in `bot.py`) — paced tool-calling rounds (`goal_loop_interval_sec`, default 25s) that keep going, posting progress in-channel, until the AI calls `complete_goal` or a wall-clock safety cap (`goal_loop_max_seconds`, default 15 min) is hit, at which point the goal stays active but the loop pauses. `complete_goal`/`list_goals` are unchanged (still AI tools).
- **Removed unused tools**: `give_coins`, `send_meme`, `request_capability`, `update_emotional_state`, and the entire remote-terminal-control feature (`run_terminal_command`, `open_url_browser`, `view_screen`, `keyboard_type`, `press_key`, `mouse_click`, `mouse_move`, `scroll_screen`, `/openterminal`, `/closeterminal`, `/fastimg`, the disabled Playwright browser-DOM tools, and their supporting session/screenshot-loop code). The coin economy (`/buy`, `/sell`, `/portfolio`) and auto meme-reaction pass are unaffected — only the AI-callable tools were cut.
- **Persona bump → Claude Fable 6** (display name + system prompt only; backend is still the 9arm gateway `qwen3.6-35b-a3b`, unchanged).
- **Persona → Claude Fable 5** (loaded from `personality_fable5.md`); all internal sub-prompts (meme picker, reflection, QA checker, background agent, radio/voice DJ) reframed from the old "Blood/House" persona to Claude. Radio rebranded **Clawd Radio**.
- **Providers**: primary chat + vision on the 9arm gateway (`qwen3.6-35b-a3b`) with MiMo/Moonshot/Groq failover. Vision repointed off MiMo (quota-exhausted) to the gateway + Groq fallback.
- **`/hotreload`** owner command for in-place restarts (`os.execv`).
- **STT** and **memes/GIFs** are now toggleable (currently off).
- **Fixes**: per-user concurrency lock no longer strands users on "i'm still working…"; injection screening exempts owners/admins and the AI classifier is robust to reasoning-model output; the prompt-leak guard no longer false-fires on the bot identifying itself; music cut-offs reduced via FFmpeg reconnect/timeout flags; VC listener survives corrupt opus frames; history-compaction caps raised so skills/tool results survive across turns.
