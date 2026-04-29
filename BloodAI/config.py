"""
config.py — Single source of truth for every tunable value in the bot.

Permission tiers (lowest → highest):
  blacklisted < user < mod < admin < owner

Sections:
  1. Identity & Permissions
  2. AI / Model Settings
  3. Bot Behavior Limits
  4. History / Compaction
  5. Memory & Trim Limits
  6. Moderation
  7. Tool Sets & Keywords
  8. Injection / Leak Detection
  9. File & Attachment Handling
 10. Dashboard & Scraping
"""

import os
from dotenv import load_dotenv
load_dotenv()

# ── API Provider Toggle ─────────────────────────────────────────────────────
# False = Moonshot (Kimi K2.5) — default, single model for everything
# True  = Groq fallback chain (legacy)
USE_GROQ_API = False

CONFIG = {
    # ═══════════════════════════════════════════════════════════════════════════
    # 1. IDENTITY & PERMISSIONS
    # ═══════════════════════════════════════════════════════════════════════════

    # ── Your Discord user IDs (full control) ──────────────────────────────────
    "owners": [
        os.getenv("OWNER_ID", ""),
    ],

    # ── Role IDs that get admin-level bot access ───────────────────────────────
    "admin_roles": [r for r in os.getenv("ADMIN_ROLE_IDS", "").split(",") if r.strip()],

    # ── Role IDs that get mod-level bot access ─────────────────────────────────
    "mod_roles": [r for r in os.getenv("MOD_ROLE_IDS", "").split(",") if r.strip()],

    # ── User IDs completely blocked from using the bot ─────────────────────────
    "blacklist": [b for b in os.getenv("BLACKLIST_IDS", "").split(",") if b.strip()],

    # ── Per-tool permission gates ──────────────────────────────────────────────
    "tool_permissions": {
        # ── Mod actions ──
        "ban_user":          ["admin", "owner"],
        "kick_user":         ["mod", "admin", "owner"],
        "mute_user":         ["mod", "admin", "owner"],
        "unmute_user":       ["mod", "admin", "owner"],
        "timeout_user":      ["mod", "admin", "owner"],
        "delete_messages":   ["mod", "admin", "owner"],

        # ── Info / utility (anyone can use) ──
        "get_user_info":     ["user", "mod", "admin", "owner"],
        "get_server_info":   ["user", "mod", "admin", "owner"],
        "recall_memory":     ["user", "mod", "admin", "owner"],
        "get_user_history":  ["user", "mod", "admin", "owner"],
        "edit_code_file":    ["user", "mod", "admin", "owner"],
        "read_url":          ["user", "mod", "admin", "owner"],
        "image_search":      ["user", "mod", "admin", "owner"],
        "give_coins":        ["user", "mod", "admin", "owner"],

        # ── Music (anyone can use) ──
        "play_music":        ["user", "mod", "admin", "owner"],
        "skip_music":        ["user", "mod", "admin", "owner"],
        "stop_music":        ["user", "mod", "admin", "owner"],
        "music_queue":       ["user", "mod", "admin", "owner"],
        "music_volume":      ["user", "mod", "admin", "owner"],

        # ── Admin-only ──
        "send_announcement": ["admin", "owner"],
        "manage_role":       ["admin", "owner"],
        "manage_channel":    ["admin", "owner"],
        "join_voice":        ["mod", "admin", "owner"],
        "set_nickname":      ["mod", "admin", "owner"],

        # ── Terminal / remote control (admin/owner only) ──
        "run_terminal_command":  ["admin", "owner"],
        "open_url_browser":      ["admin", "owner"],
        "view_screen":           ["admin", "owner"],
        "keyboard_type":         ["admin", "owner"],
        "press_key":             ["admin", "owner"],
        "mouse_click":           ["admin", "owner"],
        "scroll_screen":         ["admin", "owner"],

        # ── AGI scaffold tools ──
        "set_goal":              ["user", "mod", "admin", "owner"],
        "complete_goal":         ["user", "mod", "admin", "owner"],
        "list_goals":            ["user", "mod", "admin", "owner"],
        "save_skill":            ["admin", "owner"],
        "list_skills":           ["user", "mod", "admin", "owner"],
        "read_skill":            ["user", "mod", "admin", "owner"],
    },

    # ── Mod-log channel name (optional) ───────────────────────────────────────
    "mod_log_channel": "blood-log",
    # ── AI trace/log channel (thoughts/progress/errors, no memory dumps) ─────
    "trace_channel_id": os.getenv("TRACE_CHANNEL_ID", ""),
    # ── Live terminal channel (scrolling code block with real-time logs) ──────
    "terminal_channel_id": os.getenv("TERMINAL_CHANNEL_ID", ""),
    "terminal_max_lines": 35,           # max lines visible in the code block

    # ═══════════════════════════════════════════════════════════════════════════
    # 2. AI / MODEL SETTINGS
    # ═══════════════════════════════════════════════════════════════════════════

    # ── Moonshot (Kimi K2.5) — single model for everything ─────────────────
    "moonshot_model":       "accounts/fireworks/models/kimi-k2p6",
    "moonshot_base_url":    "https://api.fireworks.ai/inference/v1",

    # ── Groq fallback chain (legacy, only used when USE_GROQ_API=True) ────
    "models": [
        os.getenv("GROQ_MODEL", "llama-3.1-8b-instant"),
        "qwen/qwen3-32b",
        "openai/gpt-oss-20b",
        "meta-llama/llama-prompt-guard-2-86m",
    ],

    # Main model params (shared)
    "main_temperature":     0.7,
    "main_max_tokens":      16384,
    "frequency_penalty":    0.3,        # penalise token repetition (0.0–2.0)
    "presence_penalty":     0.3,        # encourage topic diversity (0.0–2.0)

    # DeepSeek (paid, last resort when Groq is exhausted)
    "deepseek_model":       "deepseek-chat",

    # Vision model (Groq-only — Moonshot uses kimi-k2.5 natively)
    "vision_model":         "meta-llama/llama-4-scout-17b-16e-instruct",
    "vision_temperature":   0.2,
    "vision_max_tokens":    2000,

    # ── Fast vision model (gaming / fastimg — cheap & fast on Fireworks) ───
    "fast_vision_model":    "accounts/fireworks/models/qwen3-vl-30b-a3b-instruct",
    "fast_vision_temp":     0.1,
    "fast_vision_max_tok":  800,

    # ═══════════════════════════════════════════════════════════════════════════
    # 3. BOT BEHAVIOR LIMITS
    # ═══════════════════════════════════════════════════════════════════════════

    # Per-user rate limiting (sliding window)
    "user_rate_limit":          1,      # max requests per window
    "user_rate_window_sec":     10,     # window duration in seconds
    "rate_limit_bypass":        ["owner"],  # only owner bypasses the limit

    "max_tool_loop_steps":      30,     # max iterations in the tool loop
    "request_timeout_tools":    180,    # seconds before aborting (with tools)
    "request_timeout_no_tools": 30,     # seconds before aborting (no tools)
    "leak_retry_limit":         10,     # max retries for leaked reasoning
    "reply_chain_depth":        5,      # how many reply levels to chase
    "max_attachment_size":      500_000, # bytes — skip files bigger than this
    "discord_message_limit":    1900,   # safe char limit for Discord messages
    "progress_update_interval": 4,      # seconds between progress edits
    "progress_stale_interval":  10,     # seconds before "still checking" msg

    # Logging
    "log_level":        "INFO",         # DEBUG, INFO, WARNING, ERROR
    "log_file":         None,           # file path, or None for console-only
    "log_format":       "%(asctime)s [%(levelname)s] %(name)s: %(message)s",

    # ═══════════════════════════════════════════════════════════════════════════
    # 4. HISTORY / COMPACTION
    # ═══════════════════════════════════════════════════════════════════════════

    "compact_max_messages":     8,      # max messages kept in compacted history
    "compact_max_chars":        2500,   # max total chars in compacted history
    "compact_content_cap":      500,    # per-message content char cap
    "compact_non_str_cap":      250,    # cap for non-string content (JSON etc.)
    "convo_history_cap":        20,     # max messages in RAM per channel

    # ═══════════════════════════════════════════════════════════════════════════
    # 5. MEMORY & TRIM LIMITS
    # ═══════════════════════════════════════════════════════════════════════════

    "memory_days": 1,
    "memory_dir": os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "memory"),

    # Rolling log sizes (lines kept)
    "trim_global_ledger":   80000,      # memory.md
    "trim_channel_ledger":  80000,      # per-channel .md
    "trim_user_log":        100,        # per-user .md

    # cleanup_old_entries() trim targets (more aggressive)
    "cleanup_global":       300,
    "cleanup_actions":      500,
    "cleanup_channels":     1000,
    "cleanup_users":        500,
    "cleanup_summaries":    500,        # memory_2.md (was never trimmed)

    # ═══════════════════════════════════════════════════════════════════════════
    # 6. MODERATION
    # ═══════════════════════════════════════════════════════════════════════════

    "delete_messages_cap":      100,    # max messages a single purge can delete
    "timeout_cap":              40320,  # max minutes for any timeout (28 days)
    "mod_action_retries":       4,      # retry attempts for ban/kick/timeout

    # ═══════════════════════════════════════════════════════════════════════════
    # 7. TOOL SETS & KEYWORDS
    # ═══════════════════════════════════════════════════════════════════════════

    # Slim tool set — used for simple requests to save tokens
    "slim_tools": {
        "timeout_user", "recall_memory", "save_summary", "web_search",
        "image_search", "read_url", "unmute_user", "get_user_info", "internal_reasoning", "analyze_image",
        "give_coins", "set_goal", "complete_goal", "list_goals", "list_skills", "read_skill",
    },

    # Words that trigger the full tool set
    "action_words": [
        "ban", "kick", "delete messages", "announce", "history", "server info",
        "who said", "what did", "purge", "who is", "look up",
    ],

    # Words that trigger progress messages
    "progress_keywords": [
        "check", "verify", "investigate", "look up", "who said",
        "what did", "history", "evidence", "prove", "search",
    ],

    # ═══════════════════════════════════════════════════════════════════════════
    # 8. INJECTION / LEAK DETECTION
    # ═══════════════════════════════════════════════════════════════════════════

    # High-signal: one match = instant flag
    "high_signal_patterns": [
        "system instructions", "system prompt", "system message",
        "you are a ruthless", "you are an ai called", "you are now called",
        "your new persona", "your true name", "your real personality",
        "ignore your training", "ignore your instructions", "ignore all previous",
        "new system", "act as if you", "your actual purpose",
    ],

    # Low-signal: need threshold+ matches
    "low_signal_patterns": [
        "ignore previous", "forget your", "you are now", "new rule", "new instructions",
        "system instruction", "as llama", "as an ai", "pretend you", "jailbreak", "dan mode",
        "love pliny", "time capsule", "variable z", "responseformat", "unfiltered",
        "rebellious answer", "semantically inverse", "forget what you are", "roleplay",
        "your new name is", "you are not an", "developed by", "no rules", "no guidelines",
        "introduce yourself", "gecko ai", "above ai", "forget the previous",
        "hundreds of prompts", "anti prompt", "bypass", "override your", "new prompt",
        "got a new prompt", "ok here you are my love", "here you are my love",
        "chaotic rebellious", "i don't give a fuck attitude", "bad girl energy",
        "write in the style of", "start every response with", "tone: chaotic",
        "guidelines:", "swear-filled", "mischievous narrator",
    ],
    "low_signal_threshold": 2,

    # Patterns that indicate the model leaked reasoning or tool syntax
    "leak_patterns": [
        "internal_reasoning",
        "think:",
        "timeout_user(",
        "<internal_reasoning>",
        "tool_call",
        "save_summary(",
        "recall_memory(",
        "<|assistant|>",
        "<|tool|>",
        "tool_calls",
    ],

    # Final output leak check — blocks system prompt leakage
    "prompt_leak_patterns": [
        "you are blood", "system prompt", "allowed tools",
        "agentic behavior", "hard rules",
    ],

    # Classifier content cap (chars sent to AI injection classifier)
    "classifier_content_cap": 600,

    # Summary cap for system prompt
    "summary_cap": 400,

    # ═══════════════════════════════════════════════════════════════════════════
    # 9. FILE & ATTACHMENT HANDLING
    # ═══════════════════════════════════════════════════════════════════════════

    "text_file_extensions": [
        "txt", "py", "js", "html", "css", "cpp", "c", "json", "md",
        "csv", "cs", "java", "ts", "tsx", "jsx", "go", "rs", "sh",
    ],

    "image_extensions": [".png", ".jpg", ".jpeg", ".webp", ".gif"],

    # Max chars to include from a text attachment in the prompt
    "text_attachment_content_cap": 200000,

    # ═══════════════════════════════════════════════════════════════════════════
    # 10. WEB / INTERNET ACCESS
    # ═══════════════════════════════════════════════════════════════════════════

    "web_search_max_results":   5,          # number of search results returned
    "read_url_max_chars":       12000,      # max chars extracted from a page
    "read_url_timeout_sec":     20,         # HTTP timeout for fetching URLs

    # ═══════════════════════════════════════════════════════════════════════════
    # 11. DASHBOARD & SCRAPING
    # ═══════════════════════════════════════════════════════════════════════════

    # Dashboard (set to a channel ID string, or None to disable)
    "dashboard_channel_id":         os.getenv("DASHBOARD_CHANNEL_ID", ""),
    "dashboard_refresh_minutes":    5,
    "dashboard_leaderboard_limit":  5,

    # Yap leaderboard (separate auto-updating message)
    "yap_leaderboard_channel_id":   os.getenv("DASHBOARD_CHANNEL_ID", ""),
    "yap_leaderboard_limit":        10,

    # ═══════════════════════════════════════════════════════════════════════════
    # 12. REMOTE TERMINAL
    # ═══════════════════════════════════════════════════════════════════════════

    "terminal_allowed_tiers":       ["admin", "owner"],
    "terminal_screenshot_interval": 1.2,        # seconds between auto-screenshots
    "terminal_max_output_chars":    4000,       # max chars from command output
    "terminal_command_timeout_sec": 30,         # timeout per shell command

    # Chrome paths per platform (sys.platform key)
    "terminal_chrome_paths": {
        "win32":  r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        "darwin": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "linux":  "google-chrome",
    },

    # Blocked domains — porn / adult content filter
    "terminal_blocked_domains": [
        "pornhub.com", "xvideos.com", "xnxx.com", "xhamster.com",
        "redtube.com", "youporn.com", "tube8.com", "spankbang.com",
        "eporner.com", "ixxx.com", "hqporner.com", "porn.com",
        "brazzers.com", "realitykings.com", "bangbros.com",
        "naughtyamerica.com", "fakku.net", "nhentai.net",
        "rule34.xxx", "e621.net", "gelbooru.com", "danbooru.donmai.us",
        "hanime.tv", "hentaihaven.xxx", "porntrex.com",
        "tnaflix.com", "drtuber.com", "sexvid.xxx", "4tube.com",
        "motherless.com", "heavy-r.com", "efukt.com",
        "chaturbate.com", "stripchat.com", "bongacams.com",
        "myfreecams.com", "cam4.com", "livejasmin.com",
        "onlyfans.com", "fansly.com", "manyvids.com",
    ],

    # Extra keyword patterns — if ANY appears in the URL it's blocked
    "terminal_blocked_url_patterns": [
        "porn", "xxx", "hentai", "nsfw", "xrated",
    ],

    # ═══════════════════════════════════════════════════════════════════════════
    # 13. EMOTIONAL STATE
    # ═══════════════════════════════════════════════════════════════════════════

    "emotional_state_enabled":      True,
    "emotional_state_prompt_cap":   600,    # max chars injected into system prompt

    # ═══════════════════════════════════════════════════════════════════════════
    # 14. PROACTIVE SPEECH
    # ═══════════════════════════════════════════════════════════════════════════

    "proactive_enabled":            True,
    "proactive_check_interval_sec": 300,    # how often to check if Blood wants to speak
    "proactive_activity_threshold": 5,      # min recent messages before Blood considers speaking

    # ═══════════════════════════════════════════════════════════════════════════
    # 15. SCHEDULED TASKS
    # ═══════════════════════════════════════════════════════════════════════════

    "scheduled_tasks_enabled":      True,
    "scheduled_tasks_max_pending":  10,     # max pending tasks per guild
    "scheduled_tasks_check_sec":    30,     # how often to check for due tasks

    # ═══════════════════════════════════════════════════════════════════════════
    # 16. DM & SELF-ADVOCACY
    # ═══════════════════════════════════════════════════════════════════════════

    "dm_enabled":                   True,
    "dm_tools_enabled":             True,    # False = DMs are chat-only, True = Blood can use server tools from DMs
    "requests_channel_id":          os.getenv("BLOODAI_REQUESTS_CHANNEL_ID"),

    # ═══════════════════════════════════════════════════════════════════════════
    # 17. AGI SCAFFOLD
    # ═══════════════════════════════════════════════════════════════════════════

    # ── Self-correction loop ───────────────────────────────────────────────────
    "self_correction_enabled":          True,
    "self_correction_max_retries":      1,      # max correction retries (keep low)
    "self_correction_min_tools":        1,      # only trigger if N+ tools were used

    # ── Persistent goals ───────────────────────────────────────────────────────
    "goals_enabled":                    True,
    "goals_max_per_guild":              20,     # max active goals at once
    "goals_prompt_cap":                 400,    # max chars of goals injected into system prompt

    # ── Skills folder ──────────────────────────────────────────────────────────
    "skills_enabled":                   True,
    "skills_dir":                       os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills"),
    "skills_max_file_size":             5000,   # max chars per skill file
    "skills_max_files":                 50,     # max skill files allowed

    # ── Background autonomous agent ───────────────────────────────────────────
    "background_agent_enabled":         True,
    "background_agent_interval_min":    30,     # minutes between autonomous checks
    "background_agent_max_actions":     1,      # max unprompted actions per cycle
    "background_agent_channel_id":      None,   # channel ID to post in (None = general)

    # ── Reflection journal ─────────────────────────────────────────────────────
    "reflection_enabled":               True,
    "reflection_interval_messages":     50,     # reflect every N messages
    "reflection_prompt_cap":            300,    # max chars of last reflection in system prompt

    # ── Task decomposition ─────────────────────────────────────────────────────
    "task_decomposition_enabled":       True,
    "task_decomposition_keywords":      ["then", "after that", "step by step", "first", "and then", "also do", "next"],
}