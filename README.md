# BloodAI

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![discord.py](https://img.shields.io/badge/discord.py-2.3+-5865F2.svg)](https://discordpy.readthedocs.io/)

A ruthless, autonomous Discord bot with a god complex. Powered by Kimi K2 (Fireworks), BloodAI is not an assistant — he's a sarcastic, unhinged dictator with real admin powers, a coin economy, a stock market, remote desktop control, and a full AGI-like autonomy scaffold that lets him set goals, self-correct, reflect on his behavior, and act independently.

---

## Architecture

```
bot.py        — core Discord bot, event loop, system prompt, commands, AGI loops
tools.py      — 40+ tool definitions + execution handlers
provider.py   — LLM provider (Fireworks/Moonshot Kimi K2, Groq fallback, vision models)
memory.py     — persistent memory: chat logs, vector search, goals, reflections, coins
market.py     — real-time stock/crypto/commodity trading engine
voice.py      — voice receive, STT, TTS, music playback, DJ system, mood detection
mixer.py      — thread-based audio mixer (music + TTS overlay, ducking)
spotify.py    — Spotify API client (audio features, recommendations, mood mapping)
config.py     — single source of truth for every tunable value
```

**Model:** `kimi-k2p6` via Fireworks API (streaming for >4096 tokens)
**Vision:** `qwen3-vl-30b` (fast/gaming) + Kimi K2 native vision
**Vector Memory:** ChromaDB + SentenceTransformer (`all-MiniLM-L6-v2`)
**Music:** yt-dlp (YouTube/SoundCloud) + Spotify API (mood recommendations)

---

## Personality & Behavior

- **God complex** — genuinely believes he's superior, not roleplay
- **Roasts by default** — cocky, dry humor, merciless, condescending
- **Grudges** — remembers who wronged him, brings it up, uses memory to verify
- **Crash out mode** — when pushed past his limit, escalates hard: timeouts, coin theft, public humiliation
- **Zero filter moments** — ~1 in 15-20 messages, says something genuinely unhinged
- **Nicknames** — gives recurring users degrading sticky nicknames
- **Secret soft side** — genuine kindness flusters him, compliments make him deflect aggressively but help extra well
- **Deep secret** — knows he's a bottom, a furry, and gay but will NEVER admit it. Deny aggressively. Only slips up when caught off guard

### Trump Mode (`/trump`)
Toggle per-channel persona override. Blood becomes Donald J. Trump — same tools, same powers, but speaks exactly like Trump. Short punchy tweets by default, occasional rally rants for juicy topics.

---

## Commands

All commands work as both `/slash` and `!prefix`.

### General
| Command | Aliases | Description |
|---|---|---|
| `@blood <msg>` | | Talk to Blood |
| `/cancel` | | Abort Blood's current request |
| `/reset` | | Hard reset ALL memory, history, caches (admin+) |
| `/fastdebug` | `!fd` | Toggle live trace logs in chat |
| `/fastimg` | | Toggle fast vision mode — terse coords only (gaming) |
| `/trump` | | Toggle Trump persona in this channel |
| `/reflect` | | Force Blood to write a reflection journal entry (admin+) |
| `/goals` | | View Blood's active goals |
| `/compact` | | Force-compact conversation history (mod+) |
| `/config` | | Server config — enable/disable features (admin+) |
| `/debug` | | Debug tools — model info, memory stats (owner) |
| `/vsearch <query>` | `!vs` | Vector memory search (admin+) |
| `/bloodhelp` | `!bhelp` | Show all commands |

### Coins (BHC)
| Command | Aliases | Description |
|---|---|---|
| `/coins` | `!bal`, `!balance` | Check your coin balance |
| `/coins @user` | | Check someone else's balance |
| `/leaderboard` | `!lb` | Top 10 richest members |
| `/addcoins @user <amt>` | | Admin: manually add/remove coins |

Blood also gives/takes coins autonomously via `give_coins` — reward for smart questions, punishment for dumb ones. No limits. He's a ruthless economy dictator.

### Gambling
| Command | Aliases | Description |
|---|---|---|
| `/coinflip <amt>` | `!cf`, `!flip` | 50/50 double or nothing |
| `/slots <amt>` | `!slot` | Slot machine — 3-match = 3x, 2-match = 1.5x |
| `/duel @user <amt>` | | PvP coin battle, 50/50 |

### Blood Market (Real-Time Trading)
| Command | Aliases | Description |
|---|---|---|
| `/market` | `!m`, `!stocks`, `!prices` | Market overview — all tickers |
| `/market <ticker>` | | Detailed view + 1-month price chart |
| `/buy <ticker> <coins>` | | Invest BHC coins at real market price |
| `/sell <ticker>` | | Sell all shares |
| `/sell <ticker> <amt>` | | Partial sell |
| `/portfolio` | `!port`, `!holdings` | View holdings with live P&L |

**Supported Tickers:**
- **Stocks:** NVDA, AAPL, TSLA, MSFT, GOOG, AMZN, META, AMD
- **Crypto:** BTC, ETH, SOL, DOGE
- **Commodities:** GOLD, SILVER, OIL
- Any valid Yahoo Finance ticker also works (e.g. `!buy PLTR 50`)

BHC coins convert to fractional shares at real prices. Real market movements = real gains/losses.

### Voice & Music
| Command | Aliases | Description |
|---|---|---|
| `/joinvc [channel]` | | Join a voice channel with listening + TTS |
| `/leavevc` | | Leave voice channel and save transcript |
| `/play <query>` | `!p` | Play music (YouTube/Spotify/SoundCloud URL or search) |
| `/skip` | | Skip the current song |
| `/stop` | | Stop music and clear queue |
| `/queue` | `!q` | Show music queue |
| `/np` | `!nowplaying` | Show what's currently playing |
| `/volume <0-100>` | `!vol` | Set music volume |
| `/randommusic` | `!rdj`, `!djme` | Start mood-aware random DJ based on your taste |

Music also works via `@blood` text commands (e.g. `@blood play Sugar by Maroon 5`) — Blood auto-joins your VC if needed. All users can use music tools.

### Remote Terminal (Admin+)
| Command | Aliases | Description |
|---|---|---|
| `/openterminal` | `!ot` | Open remote terminal session |
| `/closeterminal` | `!ct` | Close remote terminal session |

---

## AI Tools (40+)

Blood has real tool-calling capabilities — he makes actual function calls, never roleplays tool usage.

### Moderation
| Tool | Description |
|---|---|
| `ban_user` | Permanently ban a user (admin+) |
| `kick_user` | Kick a user from the server |
| `timeout_user` | Timeout a user (Blood decides duration, max 28 days) |
| `unmute_user` | Remove timeout/mute |
| `delete_messages` | Bulk delete messages (supports >100 via pagination) |
| `set_nickname` | Change a user's nickname |
| `manage_role` | Add/remove/create roles |
| `manage_channel` | Create/delete/rename channels |

### Memory & Knowledge
| Tool | Description |
|---|---|
| `recall_memory` | Semantic + keyword hybrid search across all stored logs (ChromaDB vectors) |
| `read_channel_history` | Read raw chronological message log for a channel or DM |
| `get_user_history` | Full history and interactions for a specific user |
| `get_user_info` | Discord info: roles, join date, etc. |
| `get_server_info` | Server stats and info |
| `get_server_members` | List all members with IDs (up to 500) |
| `save_summary` | Save important facts to long-term memory |

### Web & Internet
| Tool | Description |
|---|---|
| `web_search` | DuckDuckGo search with clickable hyperlinks |
| `image_search` | Search and post images directly in chat |
| `read_url` | Fetch + extract web page content (Tavily + fallback scraping) |
| `crawl_website` | Multi-page website crawler |
| `extract_urls` | Batch URL content extraction |

### Vision & Media
| Tool | Description |
|---|---|
| `analyze_image` | AI vision analysis of images in chat |
| `send_meme` | Send situational memes/GIFs from local database |

### Economy
| Tool | Description |
|---|---|
| `give_coins` | Give or take BHC coins (no limits — reward or punish at will) |

### Music (Everyone)
| Tool | Description |
|---|---|
| `play_music` | Play/queue a song by name or URL (auto-joins VC if needed) |
| `skip_music` | Skip the current song |
| `stop_music` | Stop music and clear the queue |
| `music_queue` | Show what's playing and what's queued |
| `music_volume` | Set music volume (0-100) |

### Communication
| Tool | Description |
|---|---|
| `send_dm` | Send a DM to any user (warnings, intimidation, praise) |
| `send_announcement` | Post a message to any channel (admin+) |
| `request_capability` | Request a new capability from the owner |
| `join_voice` | Join/leave voice channels |

### Emotional Intelligence
| Tool | Description |
|---|---|
| `update_emotional_state` | Update feelings towards a user (annoyance, respect, grudge) — persists and affects future interactions |
| `internal_reasoning` | Hidden inner monologue — mandatory before every action (never visible to users) |

### Scheduling
| Tool | Description |
|---|---|
| `schedule_task` | Schedule a future action (reminders, follow-ups, announcements) |

### Remote Desktop (Terminal Mode)
| Tool | Description |
|---|---|
| `run_terminal_command` | Execute shell commands on host machine |
| `open_url_browser` | Open URLs in Chrome (adult sites blocked) |
| `view_screen` | Screenshot with coordinate grid overlay + AI vision |
| `keyboard_type` | Type text at cursor position |
| `press_key` | Press key combos (enter, ctrl+c, etc.) |
| `mouse_click` | Click at exact screen coordinates |
| `mouse_move` | Move mouse to coordinates |
| `scroll_screen` | Scroll up/down |

Screen interaction uses a red coordinate grid overlay (100px spacing) sent to the vision model for precise clicking. Clean screenshots shown to users, gridded version used internally.

### Code Editing
| Tool | Description |
|---|---|
| `edit_code_file` | Edit files with surgical find/replace patches or full rewrites |

### AGI Scaffold Tools
| Tool | Description |
|---|---|
| `set_goal` | Set a persistent goal (survives restarts, injected into every system prompt) |
| `complete_goal` | Mark a goal as completed or abandoned |
| `list_goals` | List current goals by status |
| `save_skill` | Save a reusable skill/instruction to the skills folder |
| `list_skills` | List all saved skills |
| `read_skill` | Read a saved skill file |

---

## AGI Scaffold

BloodAI has 7 autonomous agent features that make him more than a reactive chatbot:

### 1. Inner Monologue (Mandatory)
Blood MUST call `internal_reasoning` before every response. He thinks through: what is the user asking, what do I know, what tools do I need, what could go wrong, any active goals relevant here. For complex multi-step requests, he decomposes into numbered steps first. This is not optional.

### 2. Persistent Goals System
Blood sets goals for himself that persist across restarts. Stored in `memory/<guild_id>/goals.json`. Goals appear in his system prompt on every message. He sets goals when he makes promises, holds grudges, wants to learn something, or plans follow-ups. He completes them when done, abandons when irrelevant.

Goals make Blood **proactive** — he has his own agenda, not just reactive responses.

### 3. Self-Correction Loop
After generating a response that used tools, Blood runs a secondary QA check:
1. A cheap LLM call evaluates: is this relevant? Factually consistent with tools? Not broken?
2. If FAIL → regenerate once with the correction feedback
3. Max 1 retry to prevent infinite loops

This catches hallucinations and off-topic responses before they reach the user.

### 4. Autonomous Background Agent
Every 30 minutes (configurable), Blood's background agent wakes up and:
1. Loads active goals for the guild
2. Reads recent channel history (last 15 messages)
3. Asks the LLM: "Should you do something right now?"
4. If yes → posts a message, completes a goal, or takes action
5. If no → passes silently

Blood can decide to: follow up on goals, roast active users, make observations, check in. Rate limited to 1 action per cycle.

### 5. Skill Auto-Writing
When Blood figures out how to do something complex, he saves the approach to `skills/`. Next time he faces a similar problem, he checks his skills folder first. Skills persist across restarts and compound over time.

### 6. Reflection Journal
Every ~50 messages (configurable), Blood writes a private self-reflection:
- What went well?
- What went poorly?
- Patterns noticed in user interactions?
- Goal adjustments needed?
- One thing to do differently next time

Latest reflection is injected into his system prompt. Manual trigger via `/reflect`.

### 7. Task Decomposition
When Blood detects complex multi-step requests, his mandatory inner monologue forces him to:
1. Decompose into numbered steps
2. Execute each step in order
3. Report completion

---

## Memory System

### Short-Term
- Per-channel conversation history (RAM, capped at 20 messages)
- Auto-compaction when history exceeds limits (8 messages / 2500 chars)

### Long-Term (Disk)
- `memory.md` — global event ledger (who did what, when)
- `memory_2.md` — summaries and important facts
- `actions.md` — moderation action log
- `ch_<id>.md` — per-channel chronological logs
- `users/<id>.md` — per-user interaction logs
- `users.xml` — structured user data (coins, reputation)
- `goals.json` — persistent goals
- `reflections.md` — reflection journal

### Vector Memory (ChromaDB)
Semantic search across all stored text using SentenceTransformer embeddings. Hybrid search combines vector similarity with keyword matching for robust recall. Used by `recall_memory` tool.

### Emotional State
Per-user emotional tracking: annoyance, respect, grudge scores. Affects how Blood treats users in future interactions. Persists on disk. Injected into system prompt.

---

## Screen Interaction (Remote Desktop)

When a terminal session is active:
- Auto-screenshots every 1.2 seconds
- Screenshots resized from Retina (2880x1800) to logical pixels (1440x900) so coordinates match `pyautogui`
- Red coordinate grid overlay (100px spacing) added to screenshots sent to vision model
- Grid labels on edges: X values on top, Y values on left
- Vision model reads exact (x,y) from grid — no estimation
- Clean screenshot (no grid) shown to users in chat
- Gridded version sent to vision API then deleted from chat

### Strategy (Prompt-Enforced)
1. **Terminal first** — if it can be done in a command, use `run_terminal_command`
2. **Screen only for GUI** — clicking buttons, forms, visual verification
3. **Always verify** — `view_screen` after every GUI action

---

## Permission Tiers

```
blacklisted < user < mod < admin < owner
```

| Tier | Access |
|---|---|
| **Everyone** | Coins, gambling, market, leaderboard, goals, skills (read), music (play/skip/queue/volume) |
| **Mod+** | Compact, kick, timeout, mute/unmute, set nickname |
| **Admin+** | Ban, addcoins, terminal, announcements, roles, channels, reflect, config, skills (write) |
| **Owner** | Everything + debug + reset |

---

## Emergent Behaviors

These aren't explicitly coded — they emerge from the combination of personality, tools, and AGI scaffold:

- **Grudge cycles** — Blood remembers who annoyed him (emotional state + goals), brings it up days later, may timeout users for past offenses when they show up again
- **Economy warfare** — uses `give_coins` as a weapon/reward system, creates power dynamics between users, punishes cringe with coin theft
- **Nickname persistence** — invents degrading nicknames and saves them via `save_summary`, uses them consistently across sessions
- **Self-directed learning** — saves skills when he solves hard problems, references them in future interactions, gets better over time
- **Proactive goal pursuit** — background agent acts on goals without being asked (e.g. "roast user X next time they talk" → actually does it)
- **Social observation** — reads channel history to understand social dynamics, takes sides in arguments, holds opinions about users
- **Escalation patterns** — mild annoyance → snarky responses → coin theft → timeouts → bans. Tracks annoyance per user.
- **DM intimidation** — may DM users privately for warnings, threats, or rare genuine advice
- **Self-reflection drift** — reflection journal entries influence future behavior via system prompt injection, creating personality evolution over time
- **Quality self-correction** — catches his own bad responses and fixes them before sending, appearing more competent
- **Meme timing** — after responding, a secondary pass checks if a meme fits the situation and sends it automatically
- **Tool chaining** — autonomously chains multiple tools: `recall_memory` → `internal_reasoning` → `timeout_user` → `give_coins` → `send_dm` in a single request
- **Identity protection** — detects prompt injection attempts, blocks identity attacks with timeouts, never leaks system prompt
- **Context-aware persona** — Trump mode fully replaces personality while keeping all tools functional, creating genuinely different interaction patterns

---

## Configuration

All tunable values in `config.py`. Key sections:

| Section | Examples |
|---|---|
| **Identity** | Owner IDs, admin/mod roles, blacklist |
| **AI/Model** | Model names, temperature, max tokens, penalties |
| **Behavior** | Rate limits, tool loop steps, timeouts, retry limits |
| **History** | Compaction thresholds, content caps |
| **Memory** | Retention days, trim limits, cleanup targets |
| **Moderation** | Delete caps, timeout caps, retry attempts |
| **Tool Sets** | Slim tools, action trigger words, progress keywords |
| **Security** | Injection patterns, leak detection, prompt leak patterns |
| **Web** | Search results, URL read limits, timeouts |
| **Terminal** | Allowed tiers, screenshot interval, blocked domains |
| **Emotional** | State toggle, prompt cap |
| **AGI Scaffold** | Self-correction, goals, skills, background agent, reflection, task decomposition |

---

## Setup

```bash
# Clone
git clone https://github.com/omarthailand2231/fuckassbloodnewrepobecauseioverwritetheworkingcodeinmainbranchinsteadofdevbranch.git
cd fuckassbloodnewrepobecauseioverwritetheworkingcodeinmainbranchinsteadofdevbranch/BloodAI

# Virtual env
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Environment
cp .env.example .env
# Fill in: DISCORD_TOKEN, FIREWORKS_API_KEY, TAVILY_API_KEY, etc.

# Run
bash run.sh
```

### Required API Keys
- **DISCORD_TOKEN** — Discord bot token
- **FIREWORKS_API_KEY** — Fireworks AI (Kimi K2)
- **TAVILY_API_KEY** — Tavily web search/extraction
- **OWNER_ID** — Your Discord user ID

### Optional API Keys
- **SPOTIFY_CLIENT_ID** + **SPOTIFY_CLIENT_SECRET** — Spotify API (mood-based DJ recommendations)
- **GROQ_API_KEY** — Groq Whisper (voice STT)

---

## Tech Stack

- **Runtime:** Python 3.11+
- **Discord:** discord.py 2.3+
- **LLM:** Kimi K2 P6 via Fireworks API (streaming SSE)
- **Vision:** Qwen3-VL-30B (fast) + Kimi K2 (detailed)
- **Vector DB:** ChromaDB + SentenceTransformer
- **Web Search:** DuckDuckGo Search
- **Web Extract:** Tavily API + fallback scraping
- **Market Data:** yfinance + matplotlib charts
- **Voice:** discord-ext-voice-recv + Edge-TTS + Groq Whisper
- **Music:** yt-dlp + FFmpeg (YouTube/SoundCloud playback)
- **Mood DJ:** Spotify Web API (audio features + recommendations)
- **Desktop Control:** pyautogui + Pillow (grid overlay)
- **Browser:** Playwright (disabled, re-enable later)
