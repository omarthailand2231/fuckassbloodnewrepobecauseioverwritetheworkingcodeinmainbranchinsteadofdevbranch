# Blood — Discord AI Agent Bot

## What this is
Blood is a Discord bot that runs as an AI agent using Groq (free) as the LLM backend.
It responds when @mentioned, has tool-calling for moderation, sees images via Vision API, remembers everything, and runs a full coin economy with real-time stock market trading.

## Tech stack
- Python 3.14 + discord.py
- Groq API (free tier) — model fallback chain: `kimi-k2` → `qwen3-32b` → `llama-3.3-70b` → DeepSeek (paid last resort)
- Yahoo Finance (`yfinance`) — real-time stock/crypto/commodity prices
- `matplotlib` — chart generation
- Local file-based memory (no database)
- Native web search for reliable scraping

## File structure
```
bot.py          — Main bot, event loop, agentic tool-call loop, commands
config.py       — Permission tiers, role IDs, tool gates, model chain
memory.py       — Memory manager (RAM history + channels + summaries + coins + market)
tools.py        — Tool definitions (sent to AI) + executors with verification
openrouter.py   — Groq/DeepSeek API client with smart fallback + vision
market.py       — Real-time stock/crypto/commodity price fetching + chart generation
mood.py         — Blood's emotional state (resets on restart)
memory/
  <guild_id>/
    channels/
      <channel_id>.md — Per-channel rolling chat log (last 200 lines each)
    memory.md   — Legacy global chat log (kept for historical search)
    memory_2.md — Summaries + pinned facts (written by AI or on demand)
    actions.md  — Immutable timestamped ledger of executed moderation actions
    users.xml   — User profiles + interaction graph
    coins.json  — BHC coin balances (persists through !reset)
    market.json — Market portfolio positions
    users/
      <user_id>.md — Per-user message log (last 200 lines each)
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

## Key behaviors
- **Temporal Awareness**: System prompt is injected with the exact real-time UTC clock so Blood understands prompt timestamps.
- **Auto Loop Detection**: If Blood gets stuck looping the same tool 3 times, a "Hard Block" fires, purging its RAM memory and forcing a clean slate.
- **Deep Thread Crawling**: If a user replies to a message, Blood natively recurses up to 3 levels deep in the reply chain to inject the full thread context immediately.
- **Thought leak prevention**: Discards LLM responses if they leak raw reasoning steps.
- **Mod action verification**: timeout/ban/kick all verify the action worked after execution. Up to 4 attempts.

## Environment variables
```
DISCORD_TOKEN=...
GROQ_API_KEY=...
GROQ_MODEL=moonshotai/kimi-k2-instruct-0905,meta-llama/llama-4-scout-17b-16e-instruct
```
