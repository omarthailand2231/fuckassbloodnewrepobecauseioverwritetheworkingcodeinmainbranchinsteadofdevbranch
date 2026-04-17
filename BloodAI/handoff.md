# Blood Bot — Handoff Document
Version: 3.27.2-S
Last updated: 2026-03-27

## What Blood is
A Discord AI agent bot running on Groq (free tier) utilizing `moonshotai/kimi-k2-instruct-0905`. Responds to @mentions, moderates autonomously,
remembers everything, analyzes images via Vision API, reads entire threads natively, and wakes up on its own. Built over ~20 hours
of live iteration with a very active server stress-testing it constantly.

## File structure
```
bot.py          — Main bot, event loop, dashboard, debug commands
config.py       — Permissions, role IDs, channel blacklist
memory.py       — Multi-tier memory: per-channel logs + per-user logs + XML user store
tools.py        — Tool definitions + executors with verification & retry
openrouter.py   — Groq API client: call_ai() + call_vision()
README.md       — Architecture overview (formerly prompt.md)
memory/
  <guild_id>/
    channels/
      <channel_id>.md — Per-channel rolling chat log (last 200 lines each)
    memory.md    — Legacy global chat log (kept for historical search)
    memory_2.md  — Summaries + pinned facts (injected into every system prompt)
    actions.md   — Immutable ledger of all moderation actions taken by Blood
    users.xml    — User profiles + interaction graph
    users/
      <user_id>.md — Per-user message log (last 200 lines each)
```

## Architecture

### API pipeline
- `call_ai()` — reactive, `moonshotai/kimi-k2-instruct-0905` primary, full personality. `meta-llama/llama-4-scout-17b-16e-instruct` is used EXCLUSIVELY for the `analyze_image` tool.

### Event loop
- **Reactive**: responds to @mentions — full agentic loop, tools, mod actions.
- **Dashboard**: `discord.ext.tasks` loop, updates operational dashboard every 5 min.

### Memory system
- `channels/<channel_id>.md` — per-channel rolling logs (200 lines each). Each channel gets
  its own file named by Discord channel ID. Searched by `recall_memory` — can target a
  specific channel or search all channels globally.
- `actions.md` — permanently logs successfully dispatched mod actions (timeout/ban/kick/etc).
  It is included globally in `recall_memory` so Blood always remembers what it did natively.
- `memory_2.md` — summaries + pinned facts. IS injected into system prompt (capped at 400 chars).
  Blood writes here via `save_summary` tool when something notable happens.
- `users.xml` — interaction graph, first/last seen, who talked to whom
- **Temporal Awareness** — exact UTC clock string injected at the top of the prompt.
- **Deep Thread Parsing** — when a user replies to a message and tags Blood, Blood natively
  recursively reads up to 3 parent layers into the conversation and drops it perfectly into the prompt.

### Tool routing
- Action words detected or mod/admin/owner invoker: full tool set
- Otherwise: slim tool set (timeout, recall, save, search, unmute, user info, reasoning, vision)

### Mod action verification
- `timeout_user`, `ban_user`, `kick_user` all verify the action actually worked after execution
- Uses Discord API to confirm: `timed_out_until` for timeout, `fetch_ban` for ban, `fetch_member` NotFound for kick
- Retries up to 4 total attempts (1 initial + 3 retries) with delays between
- `discord.Forbidden` exits immediately (no point retrying permissions)
- Returns `❌ Failed to X after 4 attempts` instead of fake "✅ Done" on failure

### Thought leak prevention & Error Catching
- **Root cause fix**: System prompt does NOT mention `internal_reasoning` by name or lay out
  step-by-step workflows. This was the primary cause of models narrating their reasoning in
  visible text. The prompt now says "Your response is ONLY the final message to the user."
- **Safety net**: `LEAK_PATTERNS`, `is_leaked_reasoning()`, `clean_response()` catch anything
  that still slips through. Up to 10 retries on leaked output.
- **Infinite Loop detection**: `bot.py` tracks the signature of executing tools. 3 consecutive
  identical tool-calls trigger a hard block, terminating the request and dumping memory.
- **Manual Reset**: users can fire `!HReset` or `!debug clear` to dump short-term RAM context.

## Permission tiers
blacklisted < user < mod < admin < owner
- Autonomous tools (Blood uses on its own): timeout_user, recall_memory, get_user_info,
  save_summary, web_search, internal_reasoning, analyze_image
- Autonomous timeout cap: 2 min. Mod+ explicit requests: up to 40320 min.

## Key design decisions — DO NOT UNDO THESE

1. **Tool list is filtered before sending to AI** — Blood only sees tools it can actually call.
   This prevents "I'll ban them" + permission denied error. Never send full tool list to all users.

2. **delete_messages is admin-only** — was raised from mod after Blood purged a channel out of spite.

3. **user_id always coerced to string** — models send integers, Discord API needs strings. The
   coercion is in bot.py right after json.loads(). Don't remove it.

4. **timeout_user hard-guards empty user_id** — if no user_id, skip the call entirely with an
   error message to the tool result. Prevents targeting random users.

5. **internal_reasoning is silent** — the tool exists, Blood calls it, executor just prints to
   terminal and returns "ok". Never visible in Discord. Don't add user-facing output to it.

6. **System prompt does NOT mention internal_reasoning by name** — the old prompt had a numbered
   workflow (1→2→3→4→5) that trained the model to narrate its reasoning. This was the root cause
   of thought leaking. The tool definition speaks for itself. Don't add reasoning instructions
   back to the system prompt.

7. **Memory is NOT in every system prompt** — per-channel logs are call-on-demand via recall_memory.
   Only memory_2.md summaries (max 400 chars) go in the system prompt. This is why tokens
   dropped from 2000+ to ~1200 per request.

8. **Injection detection is code-side, not prompt-side** — is_injection() runs before the AI
   ever sees the message. The system prompt does NOT list the patterns (they'd reverse-engineer it).
   Threshold is >= 2 pattern matches to avoid false positives.

9. **Mod actions are verified** — timeout/ban/kick all confirm the action worked via Discord API
   after execution. Up to 4 attempts. Don't remove verification or revert to fire-and-forget.

10. **Per-channel log files use channel IDs** — not channel names (names can change, IDs are stable).
    Discord auto-renders `#channel_id` as readable names anyway.

## DO NOT DO list
- Don't raise delete_messages permission back to mod
- Don't remove user_id string coercion
- Don't add reasoning/workflow instructions to the system prompt (root cause of leaking)
- Don't send the full tool list to user-tier invokers
- Don't inject memory logs into the system prompt (use recall_memory tool instead)
- Don't list injection patterns in the system prompt
- Don't remove mod action verification (timeout/ban/kick must confirm they worked)
- Don't switch channel log filenames from IDs to names

## Current known issues / quirks
- No persistent conversation history across restarts (RAM only — intentional)
- Token display shows "?" if usage data missing (some models don't return it)

## What was being worked on at handoff
- **Completed**: Per-channel logging system
- **Completed**: Mod action verification with retries
- **Completed**: Loop detection & Manual RAM reseting
- **Completed**: Multi-modal vision capability via `llama-4-scout`
- **Completed**: Deep recursive Thread crawling (3-levels)
- **Completed**: `actions.md` action ledger + Temporal real-time UTC clock + DDGS api rewrite.

### Planned features (not yet implemented)
- Follow-up messages: after responding, Blood can queue a second message 10-30s later
- Typing delay: asyncio.sleep scaled to response length (simulate reading time)
- Empathy detector: classify if incoming message is distressed, soften tone
- Clarifying questions: ask one question before acting on ambiguous requests
- Cross-channel awareness: Blood comments on what's happening elsewhere

## Prompting notes
- Kimi-k2-instruct-0905 has the best personality fit. Use it for reactive.
- Scout (llama-4-scout) is reliable for tool calls and vision.
- Models sometimes return internal_reasoning as plain text — is_leaked_reasoning() catches it
- The internal_reasoning tool works better than <think> tags for controlling reasoning visibility
- System prompt must NOT describe reasoning workflows — causes models to narrate steps

## Versioning system
Format: MM.DD.VERSION-STATUS
- C = updated that day
- U = no update that day
- S = stable
- D = working on / in development

## Environment variables
```
DISCORD_TOKEN=...
GROQ_API_KEY=gsk_...
GROQ_MODEL=moonshotai/kimi-k2-instruct-0905
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
2. Read README.md for architecture overview
3. Current files are the source of truth — don't rewrite from scratch
4. The user is Vinny. He built this overnight. He knows what he wants.
5. When in doubt: less is more. Blood works because it's constrained well.
