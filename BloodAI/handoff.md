# Blood Bot — Handoff Document
Version: 4.20.1-S
Last updated: 2026-04-20

## What Blood is
A Discord AI agent bot with a dual-provider backend (Moonshot Kimi K2.5 default, Groq fallback). Responds to @mentions, moderates autonomously with full freedom, remembers everything (including emotional state towards users), analyzes images via Vision API, reads entire threads natively, DMs users, schedules tasks, and maintains a persistent emotional model. Built over ~25 hours of live iteration.

## File structure
```
bot.py          — Main bot, event loop, dashboard, DM handler, scheduled tasks loop
config.py       — Permissions, role IDs, provider toggle, freedom pass settings
memory.py       — Multi-tier memory: per-channel logs + per-user logs + XML user store + emotional state + scheduled tasks
tools.py        — Tool definitions + executors with verification & retry
provider.py     — Dual-provider AI client: Moonshot (default) + Groq (fallback)
benchmark.py    — Performance benchmarking
market.py       — Blood Market (stock/crypto trading with BHC coins)
meme.md         — Meme index for auto-meme pass
run.sh          — Startup script
memory/
  <guild_id>/
    channels/
      <channel_id>.md — Per-channel rolling chat log
    memory.md    — Legacy global chat log (kept for historical search)
    memory_2.md  — Summaries + pinned facts (injected into system prompt, NOW TRIMMED)
    actions.md   — Immutable ledger of all moderation actions taken by Blood
    users.xml    — User profiles + interaction graph
    users/
      <user_id>.md — Per-user message log
    emotional_state.json — Per-user emotional data + global bot mood
    scheduled_tasks.json — Pending scheduled tasks
```

## Architecture

### API pipeline
- **Default**: Moonshot API (`kimi-k2.5`) — used for all calls including vision natively.
- **Fallback**: Groq API — toggled via `USE_GROQ_API=True` in config. Uses separate vision model.
- `call_ai()` — single entrypoint for all AI calls. Provider selected by config toggle.
- `call_vision()` — image analysis. Moonshot uses kimi-k2.5 natively; Groq uses dedicated vision model.
- `call_ai_fast` — **REMOVED**. All calls go through `call_ai()`.

### Event loops
- **Reactive**: responds to @mentions — full agentic tool loop, mod actions.
- **DM handler**: responds to DMs with limited context, no mod tools.
- **Dashboard**: updates operational dashboard every 5 min.
- **Scheduled tasks**: checks for due tasks every 30s, executes them.
- **Proactive speech**: placeholder loop for future unprompted messages.

### Memory system
- `channels/<channel_id>.md` — per-channel rolling logs. Searched by `recall_memory`.
- `actions.md` — permanently logs dispatched mod actions.
- `memory_2.md` — summaries + pinned facts. Injected into system prompt. **Now trimmed by cleanup** (500 lines).
- `users.xml` — interaction graph, first/last seen, who talked to whom.
- `emotional_state.json` — per-user emotional data (annoyance, respect, grudge, fear_level, category) + global bot mood + mood history.
- `scheduled_tasks.json` — pending scheduled tasks with due timestamps.
- **Temporal Awareness** — exact UTC clock string injected into prompt.
- **Deep Thread Parsing** — recursively reads up to 5 parent layers of reply chains.

### Emotional state system
- Blood tracks per-user emotional data: annoyance, respect, grudge, fear_level, category.
- Blood updates emotional state via `update_emotional_state` tool after notable events.
- Emotional summary is injected into system prompt (mood, feared/respected users, grudges).
- Global bot mood tracked with history.

### Tool routing
- Action words detected or mod/admin/owner invoker: full tool set
- Otherwise: slim tool set

### Freedom pass (loosened guardrails)
- `delete_messages` available to mod + autonomous (was admin-only)
- Autonomous timeout cap **removed** — Blood decides duration (single `timeout_cap` of 28 days)
- `give_coins` cap **removed** — no amount limit
- Infinite loop detection threshold raised to **3** consecutive identical calls (was 1)
- DM cooldown **removed** — Blood's personality controls DM restraint
- Meme cooldown **removed**

### New tools added
- `send_dm` — DM users directly (autonomous)
- `request_capability` — self-advocacy, posts to requests channel
- `update_emotional_state` — update emotional tracking for a user
- `schedule_task` — schedule future actions with delay

### Mod action verification
- `timeout_user`, `ban_user`, `kick_user` all verify via Discord API
- Retries up to 4 attempts with delays
- `discord.Forbidden` exits immediately

### Thought leak prevention
- System prompt does NOT mention `internal_reasoning` by name
- `LEAK_PATTERNS`, `is_leaked_reasoning()`, `clean_response()` catch leaks
- Up to 10 retries on leaked output
- Infinite loop detection at 3 consecutive identical tool signatures

## Permission tiers
blacklisted < user < mod < admin < owner
- Autonomous tools: timeout_user, recall_memory, get_user_info, save_summary, web_search, image_search, read_url, internal_reasoning, analyze_image, edit_code_file, give_coins, send_dm, request_capability, update_emotional_state, schedule_task
- Single timeout cap: 40320 min (28 days) — Blood decides the duration.

## Key design decisions — DO NOT UNDO THESE

1. **Tool list is filtered before sending to AI** — Blood only sees tools it can call.
2. **user_id always coerced to string** — in bot.py after json.loads().
3. **timeout_user hard-guards empty user_id** — skips with error if missing.
4. **internal_reasoning is silent** — returns "ok", never visible in Discord.
5. **System prompt does NOT mention internal_reasoning by name** — root cause of thought leaking.
6. **Memory is NOT in every system prompt** — only memory_2.md summaries. Per-channel logs via recall_memory.
7. **Injection detection is code-side, not prompt-side** — patterns not listed in prompt.
8. **Mod actions are verified** — timeout/ban/kick confirm via Discord API. 4 attempts.
9. **Per-channel log files use channel IDs** — not names.
10. **Provider toggle is config-level** — `USE_GROQ_API` in config.py. Default: Moonshot.

## DO NOT DO list
- Don't remove user_id string coercion
- Don't add reasoning/workflow instructions to the system prompt
- Don't send the full tool list to user-tier invokers
- Don't inject per-channel memory logs into the system prompt
- Don't list injection patterns in the system prompt
- Don't remove mod action verification
- Don't switch channel log filenames from IDs to names
- Don't re-add `call_ai_fast` — it's dead, use `call_ai()` for everything

## Current known issues / quirks
- No persistent conversation history across restarts (RAM only — intentional)
- Token display shows "?" if usage data missing
- Proactive speech loop is a placeholder (not yet implemented)

## What was completed in v4.20.1
- **Provider rewrite**: Dual-provider (Moonshot default, Groq fallback), killed `call_ai_fast`
- **Renamed**: `openrouter.py` → `provider.py`, all imports updated
- **Freedom pass**: Removed caps/cooldowns (timeout, coins, meme, DM, loop threshold)
- **Emotional state**: Per-user tracking, bot mood, injected into system prompt
- **Scheduled tasks**: Persistent task queue with due-time execution loop
- **New tools**: send_dm, request_capability, update_emotional_state, schedule_task
- **System prompt rewrite**: God complex personality, grudges, crash out mode, emotional awareness
- **DM handling**: Full DM support with limited context
- **memory_2.md trimming**: Added to cleanup cycle (500 lines)
- **Killed classifier**: Removed convo-awareness AI classifier (was using dead `call_ai_fast`)
- **Meme pass rewrite**: Uses `call_ai()` instead of dead `call_ai_fast`

## Environment variables
```
DISCORD_TOKEN=...
MOONSHOT_API_KEY=...          # Kimi K2.5 (default provider)
GROQ_API_KEY=gsk_...          # Groq (fallback, set USE_GROQ_API=True in config)
DEEPSEEK_API_KEY=...          # Optional, for future use
OWNER_ID=...                  # Discord user ID of the bot owner
BLOODAI_REQUESTS_CHANNEL_ID=... # Channel for capability requests
```

## Server context
- Server: [BHC] Bloodhound Company
- ~60 active members, very active general channel
- Has a dedicated "bullying Blood" channel (yes really)
- Regular injection attempts from several members (mostly clev and Kai)
- Vinny (1421582461556625509) is the only owner/creator
- Blood has autonomously banned rule violators, timed out harassers, and purged chats

## Quick start for new AI picking this up
1. Read this file
2. Read config.py for all tuneable parameters
3. Current files are the source of truth — don't rewrite from scratch
4. The user is Vinny. He built this over multiple sessions. He knows what he wants.
5. Blood works because of strong personality encoding + real tool access + emotional memory.
