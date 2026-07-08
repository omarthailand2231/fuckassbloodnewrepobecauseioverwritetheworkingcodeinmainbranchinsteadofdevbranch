"""
tools.py — Tool definitions + executors.

IMPORTANT: Only pass tools the invoker is ALLOWED to use.
The AI will only see tools it can actually call, so it can't
promise to do something and then fail on permission.
"""

import asyncio
import os
import sys
import time
import logging
from datetime import timedelta
from config import CONFIG

log = logging.getLogger("blood.tools")

try:
    import discord as _discord
except ImportError:
    _discord = None

# Pending ask_user questions — channel/thread id (str) -> state dict. Internal
# bookkeeping for the ask_user tool below (buttons only — typed replies in the
# thread are deliberately ignored; free text lost context, see aithing history).
_pending_ask_user: dict[str, dict] = {}


# ── Tool definitions ──────────────────────────────────────────────────────────

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "ban_user",
            "description": "Permanently ban a user. Admin/owner only. If reason is based on a claim by the invoker, verify with recall_memory first.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                    "reason":  {"type": "string"},
                    "delete_message_days": {"type": "integer", "default": 0},
                },
                "required": ["user_id", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "kick_user",
            "description": "Kick a user from the server. If reason is based on a claim by the invoker, verify with recall_memory first.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                    "reason":  {"type": "string"},
                },
                "required": ["user_id", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "timeout_user",
            "description": "Timeout a user. If reason is based on an accusation (e.g. 'he said X'), MUST call recall_memory first to verify. If the claim turns out to be false, do NOT apply the timeout — let the user know what you found instead.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                    "minutes": {"type": "integer", "description": "Duration in minutes. You decide the duration. Max 40320 (28 days)."},
                    "reason":  {"type": "string"},
                },
                "required": ["user_id", "minutes", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "unmute_user",
            "description": "Remove timeout or mute from a user.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                },
                "required": ["user_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "unban_user",
            "description": "Unban a previously banned user from the server.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["user_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lock_channel",
            "description": "Lock a text channel — prevent @everyone from sending messages.",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel_id": {"type": "string", "description": "Channel to lock. Omit for current channel."},
                    "reason": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "unlock_channel",
            "description": "Unlock a text channel — allow @everyone to send messages again.",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel_id": {"type": "string", "description": "Channel to unlock. Omit for current channel."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_member",
            "description": "Move a user to a different voice channel.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                    "channel_name": {"type": "string", "description": "Name of the voice channel to move them to."},
                },
                "required": ["user_id", "channel_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mute_voice",
            "description": "Server mute a user in voice channels (prevent them from speaking).",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["user_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "deafen_voice",
            "description": "Server deafen a user in voice channels (prevent them from hearing).",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["user_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_messages",
            "description": "Bulk delete messages. Supports >100 via automatic pagination. Defaults to current channel unless channel_id is specified.",
            "parameters": {
                "type": "object",
                "properties": {
                    "count":  {"type": "integer", "description": "Number of messages to delete (max 500)."},
                    "channel_id": {"type": "string", "description": "Target channel ID. Omit to use the current channel."},
                    "reason": {"type": "string"},
                },
                "required": ["count"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_user_info",
            "description": "Get Discord info about a user: roles, join date, etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                },
                "required": ["user_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_image",
            "description": "Analyze an image URL from the chat to see what it is.",
            "parameters": {
                "type": "object",
                "properties": {
                    "image_url": {"type": "string", "description": "The URL of the image to analyze"},
                    "prompt": {"type": "string", "description": "What to ask about the image (e.g. 'Describe this image in detail' or 'Read the text in this meme')", "default": "Describe this image in detail focusing on any text, memes, or notable subjects."},
                },
                "required": ["image_url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_server_info",
            "description": "Get info about the server.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall_memory",
            "description": "Semantic + keyword search across all stored chat logs, DMs, summaries, and actions. Understands meaning — not just exact words. IMPORTANT: Results are APPROXIMATE matches, NOT proof. Do NOT use recall_memory results alone to justify punishments — cross-reference with read_channel_history for exact quotes before taking mod actions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string"},
                    "channel": {"type": ["string", "null"], "description": "Optional channel ID to search."},
                    "limit":   {"type": "integer", "default": 30, "description": "Max results to return (default 30, max 100). Uses semantic ranking."},
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_channel_history",
            "description": "Read the raw, chronological message log for a specific channel OR a DM conversation. Use this when asked 'what happened', 'was there drama', or 'is anyone acting bad'. For DMs, pass user_id instead of channel_id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel_id": {"type": "string", "description": "The channel ID to read. Omit if reading DMs."},
                    "user_id": {"type": "string", "description": "Read the DM history with this user. Use instead of channel_id for DM logs."},
                    "limit":      {"type": "integer", "default": 5000, "description": "Number of recent lines to read. Default 1000, max 5000 for deep scrapes."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_user_history",
            "description": "Get full history and interactions for a specific user.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                },
                "required": ["user_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_summary",
            "description": "Save an important fact or summary to long-term memory (memory_2.md). Use when user says 'remember this' or something notable happens.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "The fact or summary to save"},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_announcement",
            "description": "Send a message to a specific channel as the bot.",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel_name": {"type": "string"},
                    "message":      {"type": "string"},
                },
                "required": ["channel_name", "message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "manage_role",
            "description": "Add or remove a role from a user. Can also create a new role if it doesn't exist.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "The user to modify."},
                    "role_name": {"type": "string", "description": "Name of the role to add/remove."},
                    "action": {"type": "string", "enum": ["add", "remove", "create"], "description": "'add' to assign, 'remove' to strip, 'create' to make a new role."},
                    "color": {"type": "string", "description": "Hex color for new role (e.g. '#ff0000'). Only used with 'create'.", "default": ""},
                },
                "required": ["role_name", "action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "manage_channel",
            "description": "Create, delete, or rename a text/voice channel.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["create", "delete", "rename"], "description": "What to do."},
                    "channel_name": {"type": "string", "description": "Name of the channel (existing for delete/rename, new for create)."},
                    "new_name": {"type": "string", "description": "New name (only for rename action).", "default": ""},
                    "channel_type": {"type": "string", "enum": ["text", "voice"], "description": "Type of channel to create.", "default": "text"},
                },
                "required": ["action", "channel_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "join_voice",
            "description": "Join or leave a voice channel. When joining, the assistant listens via STT (Groq Whisper), responds via TTS, and records transcripts. Say 'leave' to disconnect and save transcript.",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel_name": {"type": "string", "description": "Name of the voice channel to join. Use 'leave' to disconnect."},
                },
                "required": ["channel_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "play_music",
            "description": "Play music in voice channel. Supports YouTube URLs, Spotify URLs, SoundCloud URLs, or search by song name/artist. YouTube is used by default for search. If already playing, adds to queue.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "URL or search query (e.g. 'Never Gonna Give You Up' or 'https://youtube.com/...')"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skip_music",
            "description": "Skip the current song and play the next one in queue.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stop_music",
            "description": "Stop music playback and clear the queue.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "music_queue",
            "description": "Show the current music queue and what's playing.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_from_queue",
            "description": "Remove a specific upcoming track from the music queue by its position number (1 = next up). Does not affect the song currently playing. Use music_queue first to see positions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "position": {"type": "integer", "description": "1-based queue position to remove (1 = next up)."},
                },
                "required": ["position"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_in_queue",
            "description": "Reorder the music queue by moving a track from one position to another (1-based). Use to bump a song up or push it back. Does not affect the current song.",
            "parameters": {
                "type": "object",
                "properties": {
                    "from_position": {"type": "integer", "description": "Current 1-based position of the track to move."},
                    "to_position": {"type": "integer", "description": "Target 1-based position."},
                },
                "required": ["from_position", "to_position"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "clear_queue",
            "description": "Clear all upcoming tracks from the music queue. The song currently playing keeps playing; only the pending queue is emptied.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "music_volume",
            "description": "Set music volume (0-100).",
            "parameters": {
                "type": "object",
                "properties": {
                    "volume": {"type": "integer", "description": "Volume level 0-100."},
                },
                "required": ["volume"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_nickname",
            "description": "Change a user's nickname in the server.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "The user whose nickname to change."},
                    "nickname": {"type": "string", "description": "New nickname. Empty string to reset."},
                },
                "required": ["user_id", "nickname"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web for current information. Returns clickable hyperlinks with snippets. Use read_url to dig deeper into any result. Share relevant links when they help answer the user's question.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "image_search",
            "description": "Search the web for images and send one directly in chat. Great for sharing a relevant image, sending reaction pics, or finding visual content to illustrate a point. The image is posted in chat automatically.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Image search query."},
                    "message": {"type": "string", "description": "Optional caption to send with the image.", "default": ""},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_url",
            "description": "Fetch and read the text content of a web page. Use after web_search to get full details from a specific URL, or when a user shares a link. Uses Tavily Extract for clean markdown, falls back to raw scraping.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The full URL to fetch (https://...)."},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "crawl_website",
            "description": "Crawl a website starting from a URL. Follows links to discover and extract content from multiple pages. Great for documentation sites, wikis, or any site you need to deeply explore.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Starting URL to crawl from."},
                    "instructions": {"type": "string", "description": "Optional guidance for the crawler, e.g. 'Find pricing info' or 'Get all API docs'."},
                    "max_depth": {"type": "integer", "description": "How many link levels deep to crawl. Default 1.", "default": 1},
                    "limit": {"type": "integer", "description": "Max pages to return. Default 10, max 50.", "default": 10},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "extract_urls",
            "description": "Extract clean content from one or more URLs at once. Better than read_url for batch extraction. Returns markdown content from each page.",
            "parameters": {
                "type": "object",
                "properties": {
                    "urls": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of URLs to extract content from.",
                    },
                },
                "required": ["urls"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "internal_reasoning",
            "description": "Internal reasoning step. Use BEFORE any moderation action or multi-step decision. Think through: what do I know, what do I need to verify, what is the right action. Silent — produces no visible output.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reasoning": {
                        "type": "string",
                        "description": "Your step-by-step reasoning before acting."
                    },
                },
                "required": ["reasoning"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_user",
            "description": "Ask the user a clarifying question when you're genuinely blocked on a decision only they can make — not for things you can reasonably decide yourself. Posts clickable option buttons (plus 'Other' for a free-text answer via a popup, and 'Cancel') in a thread, @mentions the asker, and waits. Only the person who triggered you can answer; typed replies in the thread are ignored — answers come from the buttons only. If they pick 'Other' and type something like '1 + 3, I'd rather do both', interpret that as referring to your numbered options plus their own note.",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "The question to ask, phrased clearly and self-contained."},
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "2 to 5 short, clickable option labels. Don't include a generic 'other/custom' option — that's added automatically.",
                    },
                },
                "required": ["question", "options"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_code_file",
            "description": "Edit a code/text file. Two modes: (1) PATCH mode — provide find_replace with search/replace pairs for surgical edits. (2) FULL mode — provide new_content with the entire file (only for small files or full rewrites). Prefer PATCH mode for large files to avoid truncation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "The exact name of the file to edit."},
                    "find_replace": {"type": "array", "description": "Array of {find, replace} objects. Each 'find' must be a unique exact substring from the original file. Preferred for large files.", "items": {"type": "object", "properties": {"find": {"type": "string"}, "replace": {"type": "string"}}, "required": ["find", "replace"]}},
                    "new_content": {"type": "string", "description": "Full file rewrite. Only use for small files or when replacing everything."},
                    "summary_of_changes": {"type": "string", "description": "One sentence summary of the change."},
                },
                "required": ["filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_server_members",
            "description": "Get a list of all server members with their IDs and display names. Use this to find users for mass actions like DMs, coin ops, etc. Returns up to 500 members.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Max members to return. Default 500.", "default": 500},
                },
            },
        },
    },
    # ── DM & self-advocacy tools ───────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "send_dm",
            "description": "Send a direct message to a user. Use sparingly — DMs lose impact if overused. Good for: warnings, private praise, gentle reminders, follow-ups.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "The user's Discord ID."},
                    "message": {"type": "string", "description": "The DM content."},
                },
                "required": ["user_id", "message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "schedule_task",
            "description": "Schedule a task to be executed later. The bot will post the action in the specified channel when the time comes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "description": "What to do (e.g. 'remind', 'announce', 'check on user')."},
                    "channel_id": {"type": "string", "description": "Channel to post in when due."},
                    "delay_minutes": {"type": "integer", "description": "Minutes from now until execution."},
                    "context": {"type": "string", "description": "Additional context or message content. To ping a user, use <@user_id> format."},
                },
                "required": ["action", "channel_id", "delay_minutes"],
            },
        },
    },
    # ── AGI scaffold tools ──────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "complete_goal",
            "description": "Mark a goal as completed or abandoned. Use when you've achieved a goal or it's no longer relevant.",
            "parameters": {
                "type": "object",
                "properties": {
                    "goal_id": {"type": "integer", "description": "The goal ID number (from list_goals)."},
                    "outcome": {"type": "string", "enum": ["completed", "abandoned"], "default": "completed"},
                },
                "required": ["goal_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_goals",
            "description": "List your current goals. Use to check what you should be working on or to find a goal_id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": ["active", "completed", "abandoned", "all"], "default": "active"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_skill",
            "description": "Save a reusable skill/instruction to your skills folder. Use when you figure out how to do something complex — save the approach so you can reference it next time. Skills persist across restarts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Short skill name (used as filename, e.g. 'deploy_bot', 'debug_api_errors'). No spaces, use underscores."},
                    "content": {"type": "string", "description": "The skill instructions in markdown. Be specific: what worked, what didn't, step-by-step."},
                },
                "required": ["name", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_skills",
            "description": "List all saved skills. Check this before tackling a complex task — you may have solved it before.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_skill",
            "description": "Read a saved skill file for instructions on how to handle a task. Only call this when the task clearly matches a skill's domain (e.g. frontend-design for web UI tasks). A visible '*reading X skill*' message will appear in chat.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Skill name (without .md extension)."},
                },
                "required": ["name"],
            },
        },
    },
]

AUTONOMOUS_TOOLS = {
    "timeout_user", "read_channel_history", "recall_memory",
    "get_server_info", "save_summary", "web_search", "image_search",
    "read_url", "crawl_website", "extract_urls", "internal_reasoning", "analyze_image", "edit_code_file",
    "send_dm", "ask_user",
    "schedule_task", "delete_messages", "get_server_members",
    "complete_goal", "list_goals", "save_skill", "list_skills", "read_skill",
}

# Tiers that are allowed to request explicit mod actions beyond autonomous caps
MOD_TIERS = {"mod", "admin", "owner"}


# ── Executor ──────────────────────────────────────────────────────────────────

async def execute_tool(name, args, guild, invoker, channel, mentioned_members, memory, permission="owner") -> str:
    import sys
    import io
    import json
    import difflib
    discord = _discord or sys.modules.get("discord")

    class _Stub:
        class Forbidden(Exception): pass
        class utils:
            @staticmethod
            def get(iterable, **kwargs):
                key, val = next(iter(kwargs.items()))
                return next((i for i in iterable if getattr(i, key, None) == val), None)
            @staticmethod
            def utcnow():
                from datetime import datetime, timezone
                return datetime.now(timezone.utc)

    if discord is None:
        discord = _Stub

    def _clean_id(raw: str) -> str:
        """Strip <@!...>, <@...>, and whitespace so mentions and raw IDs both work."""
        import re as _re_id
        m = _re_id.search(r"(\d{17,20})", str(raw))
        return m.group(1) if m else str(raw).strip()

    async def resolve(user_id: str):
        user_id = _clean_id(user_id)
        if user_id in mentioned_members:
            return mentioned_members[user_id]
        try:
            return await guild.fetch_member(int(user_id))
        except Exception:
            return None

    async def mod_log(text: str):
        ch_name = CONFIG.get("mod_log_channel")
        if ch_name:
            lc = discord.utils.get(guild.text_channels, name=ch_name)
            if lc:
                await lc.send(f"**[BOT ACTION]** {text}")

    def _normalize(s: str) -> str:
        """Normalize channel name: strip special chars, unicode fonts, bold, etc."""
        import unicodedata, re as _re_n
        # NFKD decomposes fancy unicode (bold/italic/fullwidth) to ASCII equivalents
        s = unicodedata.normalize("NFKD", s)
        # Strip non-ASCII, non-alphanumeric except hyphens/underscores/spaces
        s = _re_n.sub(r"[^\w\s\-]", "", s)
        return s.strip().lower().replace(" ", "-")

    def _fuzzy_find_channel(query: str, channel_list):
        """Find a channel by name, ID, or fuzzy match. Handles special chars & unicode fonts."""
        query = query.strip()
        if not query:
            return None
        # 1) Try by ID first
        import re as _re_ch
        id_match = _re_ch.search(r"(\d{17,20})", query)
        if id_match:
            ch_id = int(id_match.group(1))
            ch = guild.get_channel(ch_id)
            if ch and ch in channel_list:
                return ch
        # 2) Exact name match
        exact = discord.utils.get(channel_list, name=query)
        if exact:
            return exact
        # 3) Normalized match (strips unicode fonts, special chars)
        norm_q = _normalize(query)
        for ch in channel_list:
            if _normalize(ch.name) == norm_q:
                return ch
        # 4) Fuzzy substring match
        for ch in channel_list:
            if norm_q in _normalize(ch.name) or _normalize(ch.name) in norm_q:
                return ch
        # 5) difflib closest match
        names = {_normalize(ch.name): ch for ch in channel_list}
        close = difflib.get_close_matches(norm_q, names.keys(), n=1, cutoff=0.5)
        if close:
            return names[close[0]]
        return None

    def _find_text_channel(query: str):
        return _fuzzy_find_channel(query, guild.text_channels)

    def _find_voice_channel(query: str):
        return _fuzzy_find_channel(query, guild.voice_channels)

    def _find_any_channel(query: str):
        return _fuzzy_find_channel(query, list(guild.text_channels) + list(guild.voice_channels))

    # ── ban ───────────────────────────────────────────────────────────────────
    if name == "ban_user":
        m = await resolve(args["user_id"])
        if not m: return f"Could not find user {args['user_id']}."
        reason = args.get("reason", "no reason")
        user_obj = m  # keep reference for ban verification
        retries = CONFIG["mod_action_retries"]
        for attempt in range(1, retries + 1):
            try:
                await guild.ban(m, reason=f"[Blood] {reason} — by {invoker}", delete_message_days=min(args.get("delete_message_days", 0), 7))
                # Verify ban
                await asyncio.sleep(0.5)
                try:
                    await guild.fetch_ban(user_obj)
                    # Ban confirmed
                    memory.record_interaction(str(guild.id), str(invoker.id), invoker.display_name, str(m.id), m.display_name)
                    memory.append_action_log(str(guild.id), f"I banned '{m.display_name}' ({m.id}). Reason: {reason}. Triggered by {invoker.display_name}.")
                    await mod_log(f"{invoker.mention} banned {m.mention} — {reason}")
                    return f"✅ Banned {m.display_name}. Reason: {reason}"
                except Exception:
                    # Ban not verified, retry
                    if attempt < retries:
                        await asyncio.sleep(1)
                        continue
            except discord.Forbidden:
                return "No permission to ban that user."
            except Exception as e:
                if attempt >= retries:
                    return f"❌ Failed to ban {m.display_name} after {retries} attempts: {e}"
                await asyncio.sleep(1)
        return f"❌ Failed to ban {m.display_name} after {retries} attempts — action could not be verified."

    # ── kick ──────────────────────────────────────────────────────────────────
    elif name == "kick_user":
        m = await resolve(args["user_id"])
        if not m: return f"Could not find user {args['user_id']}."
        reason = args.get("reason", "no reason")
        user_id_int = int(_clean_id(args["user_id"]))
        retries = CONFIG["mod_action_retries"]
        for attempt in range(1, retries + 1):
            try:
                await guild.kick(m, reason=f"[Blood] {reason} — by {invoker}")
                # Verify kick — member should no longer be fetchable
                await asyncio.sleep(0.5)
                try:
                    await guild.fetch_member(user_id_int)
                    # Still in server — kick didn't work
                    if attempt < retries:
                        await asyncio.sleep(1)
                        continue
                except Exception:
                    # Member gone — kick confirmed
                    memory.record_interaction(str(guild.id), str(invoker.id), invoker.display_name, str(m.id), m.display_name)
                    memory.append_action_log(str(guild.id), f"I kicked '{m.display_name}' ({m.id}). Reason: {reason}. Triggered by {invoker.display_name}.")
                    await mod_log(f"{invoker.mention} kicked {m.mention} — {reason}")
                    return f"✅ Kicked {m.display_name}."
            except discord.Forbidden:
                return "No permission to kick."
            except Exception as e:
                if attempt >= retries:
                    return f"❌ Failed to kick {m.display_name} after {retries} attempts: {e}"
                await asyncio.sleep(1)
        return f"❌ Failed to kick {m.display_name} after {retries} attempts — member is still in the server."

    # ── timeout ───────────────────────────────────────────────────────────────
    elif name == "timeout_user":
        m = await resolve(args["user_id"])
        if not m: return f"Could not find user {args['user_id']}."
        
        if str(m.id) == str(guild.me.id):
            return "ERROR: cannot timeout self."

        minutes = int(args.get("minutes", 1))
        reason = args.get("reason", "no reason")

        # Single cap for all timeouts — Blood decides the duration
        minutes = min(minutes, CONFIG["timeout_cap"])

        user_id_int = int(_clean_id(args["user_id"]))
        retries = CONFIG["mod_action_retries"]
        for attempt in range(1, retries + 1):
            until = discord.utils.utcnow() + timedelta(minutes=minutes)
            try:
                await m.timeout(until, reason=f"[Blood] {reason}")
                # Verify timeout — re-fetch member and check timed_out_until
                await asyncio.sleep(0.5)
                try:
                    refreshed = await guild.fetch_member(user_id_int)
                    if refreshed.timed_out_until and refreshed.timed_out_until > discord.utils.utcnow():
                        # Timeout confirmed
                        memory.append_action_log(str(guild.id), f"I timed out '{m.display_name}' ({m.id}) for {minutes} min. Reason: {reason}. Triggered by {invoker.display_name}.")
                        await mod_log(f"Timed out {m.mention} for {minutes}m — {reason} (by {invoker.mention})")
                        return f"✅ Timed out {m.display_name} for {minutes} min. Reason: {reason}"
                    else:
                        # Not actually timed out
                        if attempt < retries:
                            await asyncio.sleep(1)
                            continue
                except Exception:
                    # Fetch failed but timeout call succeeded — trust it
                    memory.append_action_log(str(guild.id), f"I timed out '{m.display_name}' ({m.id}) for {minutes} min (unverified). Reason: {reason}. Triggered by {invoker.display_name}.")
                    await mod_log(f"Timed out {m.mention} for {minutes}m — {reason} (by {invoker.mention})")
                    return f"✅ Timed out {m.display_name} for {minutes} min. Reason: {reason} (unverified)"
            except discord.Forbidden:
                return "No permission to timeout."
            except Exception as e:
                if attempt >= retries:
                    return f"❌ Failed to timeout {m.display_name} after {retries} attempts: {e}"
                await asyncio.sleep(1)
        return f"❌ Failed to timeout {m.display_name} after {retries} attempts — timeout did not apply."

    # ── unmute ────────────────────────────────────────────────────────────────
    elif name == "unmute_user":
        m = await resolve(args["user_id"])
        if not m: return f"Could not find {args['user_id']}."
        try:
            await m.timeout(None, reason=f"Unmute by {invoker}")
            memory.append_action_log(str(guild.id), f"I unmuted '{m.display_name}' ({m.id}). Triggered by {invoker.display_name}.")
            await mod_log(f"Unmuted {m.mention} (by {invoker.mention})")
            return f"Unmuted {m.display_name}."
        except Exception as e:
            return f"Unmute failed: {e}"

    # ── unban ─────────────────────────────────────────────────────────────────
    elif name == "unban_user":
        user_id = _clean_id(args["user_id"])
        reason = args.get("reason", "no reason")
        try:
            user_obj = await guild.fetch_ban(discord.Object(id=int(user_id)))
            if not user_obj:
                return f"User {args['user_id']} is not banned."
            await guild.unban(user_obj.user, reason=f"[Blood] {reason} — by {invoker}")
            memory.append_action_log(str(guild.id), f"I unbanned '{user_obj.user.display_name}' ({user_id}). Triggered by {invoker.display_name}.")
            await mod_log(f"Unbanned {user_obj.user.mention} — {reason} (by {invoker.mention})")
            return f"Unbanned {user_obj.user.display_name}."
        except discord.NotFound:
            return f"User {args['user_id']} is not banned."
        except discord.Forbidden:
            return "No permission to unban."
        except Exception as e:
            return f"Unban failed: {e}"

    # ── lock channel ────────────────────────────────────────────────────────────
    elif name == "lock_channel":
        target_ch = channel
        if args.get("channel_id"):
            ch_id = _clean_id(args["channel_id"])
            resolved = guild.get_channel(int(ch_id))
            if resolved is None:
                return f"Could not find channel {args['channel_id']}."
            target_ch = resolved
        reason = args.get("reason", "no reason")
        try:
            everyone = guild.default_role
            overwrite = target_ch.overwrites_for(everyone)
            overwrite.send_messages = False
            await target_ch.set_permissions(everyone, overwrite=overwrite, reason=f"[Blood] {reason} — by {invoker}")
            memory.append_action_log(str(guild.id), f"I locked #{target_ch.name}. Triggered by {invoker.display_name}.")
            await mod_log(f"Locked #{target_ch.name} — {reason} (by {invoker.mention})")
            return f"🔒 Locked #{target_ch.name}."
        except discord.Forbidden:
            return "No permission to lock channel."
        except Exception as e:
            return f"Lock failed: {e}"

    # ── unlock channel ──────────────────────────────────────────────────────────
    elif name == "unlock_channel":
        target_ch = channel
        if args.get("channel_id"):
            ch_id = _clean_id(args["channel_id"])
            resolved = guild.get_channel(int(ch_id))
            if resolved is None:
                return f"Could not find channel {args['channel_id']}."
            target_ch = resolved
        try:
            everyone = guild.default_role
            overwrite = target_ch.overwrites_for(everyone)
            overwrite.send_messages = None  # Reset to default
            await target_ch.set_permissions(everyone, overwrite=overwrite, reason=f"[Blood] Unlocked by {invoker}")
            memory.append_action_log(str(guild.id), f"I unlocked #{target_ch.name}. Triggered by {invoker.display_name}.")
            await mod_log(f"Unlocked #{target_ch.name} (by {invoker.mention})")
            return f"🔓 Unlocked #{target_ch.name}."
        except discord.Forbidden:
            return "No permission to unlock channel."
        except Exception as e:
            return f"Unlock failed: {e}"

    # ── move member ─────────────────────────────────────────────────────────────
    elif name == "move_member":
        m = await resolve(args["user_id"])
        if not m: return f"Could not find user {args['user_id']}."
        if not m.voice or not m.voice.channel:
            return f"{m.display_name} is not in a voice channel."
        ch_name = args.get("channel_name", "").strip()
        target_vc = _find_voice_channel(ch_name)
        if not target_vc:
            return f"Voice channel '{ch_name}' not found. Check SERVER CHANNELS in your prompt."
        try:
            await m.move_to(target_vc, reason=f"[Blood] Moved by {invoker}")
            memory.append_action_log(str(guild.id), f"I moved '{m.display_name}' to #{target_vc.name}. Triggered by {invoker.display_name}.")
            await mod_log(f"Moved {m.mention} to #{target_vc.name} (by {invoker.mention})")
            return f"Moved {m.display_name} to #{target_vc.name}."
        except discord.Forbidden:
            return "No permission to move members."
        except Exception as e:
            return f"Move failed: {e}"

    # ── mute voice ─────────────────────────────────────────────────────────────
    elif name == "mute_voice":
        m = await resolve(args["user_id"])
        if not m: return f"Could not find user {args['user_id']}."
        reason = args.get("reason", "no reason")
        try:
            await m.edit(mute=True, reason=f"[Blood] {reason} — by {invoker}")
            memory.append_action_log(str(guild.id), f"I server-muted '{m.display_name}'. Triggered by {invoker.display_name}.")
            await mod_log(f"Server-muted {m.mention} — {reason} (by {invoker.mention})")
            return f"🔇 Server-muted {m.display_name}."
        except discord.Forbidden:
            return "No permission to mute."
        except Exception as e:
            return f"Mute failed: {e}"

    # ── deafen voice ────────────────────────────────────────────────────────────
    elif name == "deafen_voice":
        m = await resolve(args["user_id"])
        if not m: return f"Could not find user {args['user_id']}."
        reason = args.get("reason", "no reason")
        try:
            await m.edit(deafen=True, reason=f"[Blood] {reason} — by {invoker}")
            memory.append_action_log(str(guild.id), f"I server-deafened '{m.display_name}'. Triggered by {invoker.display_name}.")
            await mod_log(f"Server-deafened {m.mention} — {reason} (by {invoker.mention})")
            return f"🔇🔇 Server-deafened {m.display_name}."
        except discord.Forbidden:
            return "No permission to deafen."
        except Exception as e:
            return f"Deafen failed: {e}"

    # ── delete messages ───────────────────────────────────────────────────────
    elif name == "delete_messages":
        count = min(int(args.get("count", 5)), 500)
        # Resolve target channel — default to current
        target_ch = channel
        if args.get("channel_id"):
            ch_id = _clean_id(args["channel_id"])
            resolved_ch = guild.get_channel(int(ch_id))
            if resolved_ch is None:
                return f"Could not find channel {args['channel_id']}."
            target_ch = resolved_ch
        try:
            total_deleted = 0
            remaining = count
            while remaining > 0:
                batch = min(remaining, 100)
                deleted = await target_ch.purge(limit=batch)
                total_deleted += len(deleted)
                remaining -= len(deleted)
                if len(deleted) < batch:
                    break  # no more messages to delete
                if remaining > 0:
                    await asyncio.sleep(1)  # rate-limit courtesy
            memory.append_action_log(str(guild.id), f"I purged {total_deleted} messages in #{target_ch.name}. Triggered by {invoker.display_name}.")
            await mod_log(f"{invoker.mention} deleted {total_deleted} msgs in #{target_ch.name}")
            return f"✅ Deleted {total_deleted} messages in #{target_ch.name}."
        except discord.Forbidden:
            return "No permission to delete messages here."
        except Exception as e:
            return f"Delete failed: {e}"

    # ── user info ─────────────────────────────────────────────────────────────
    elif name == "get_user_info":
        m = await resolve(args["user_id"])
        if not m: return f"Could not find user {args['user_id']}."
        roles = [r.name for r in m.roles if r.name != "@everyone"]
        joined = m.joined_at.strftime("%Y-%m-%d") if m.joined_at else "unknown"
        return (f"{m.display_name} (ID: {m.id})\n"
                f"Created: {m.created_at.strftime('%Y-%m-%d')} | Joined: {joined}\n"
                f"Roles: {', '.join(roles) or 'none'}")

    # ── server info ───────────────────────────────────────────────────────────
    elif name == "get_server_info":
        return (f"{guild.name} | Members: {guild.member_count} | "
                f"Channels: {len(guild.text_channels)} text, {len(guild.voice_channels)} voice | "
                f"Created: {guild.created_at.strftime('%Y-%m-%d')}")

    # ── recall memory ─────────────────────────────────────────────────────────
    elif name == "read_channel_history":
        ch_id = str(args.get("channel_id", ""))
        user_id = str(args.get("user_id", ""))
        limit = max(1, min(int(args.get("limit", 1000)), 5000))
        if user_id:
            ch_id = f"dm_{user_id}"
        if not ch_id: return "Error: channel_id or user_id required."
        return memory.read_channel_md(str(guild.id), ch_id, last_n=limit)

    elif name == "recall_memory":
        keyword = args.get("keyword", "")
        limit = max(1, min(int(args.get("limit", 30)), 100))
        ch = args.get("channel") or None
        import asyncio as _aio
        _loop = _aio.get_running_loop()
        results = await _loop.run_in_executor(
            None, lambda: memory.hybrid_search(str(guild.id), keyword, limit=limit, channel_id=ch)
        )
        if results and results != f"Nothing found for '{keyword}'.":
            results = "⚠️ CONFIDENCE NOTE: These are approximate semantic matches, NOT exact evidence. Do NOT punish users based solely on these results. Use read_channel_history to verify exact quotes before taking mod actions.\n\n" + results
        return results

    # ── user history ──────────────────────────────────────────────────────────
    elif name == "get_user_history":
        return memory.get_user_history(str(guild.id), args["user_id"])

    # ── save summary ──────────────────────────────────────────────────────────
    elif name == "save_summary":
        memory.append_summary(str(guild.id), args["text"])
        return f"Saved to memory: {args['text'][:80]}"

    # ── announcement ──────────────────────────────────────────────────────────
    elif name == "send_announcement":
        target = _find_text_channel(args["channel_name"])
        if not target: return f"Channel #{args['channel_name']} not found. Check SERVER CHANNELS in your prompt."
        try:
            await target.send(args["message"])
            memory.append_action_log(str(guild.id), f"I announced '{args['message'][:50]}...' in #{args['channel_name']}.")
            return f"Sent to #{args['channel_name']}."
        except discord.Forbidden:
            return f"No permission to send to #{args['channel_name']}."

    # ── manage role ─────────────────────────────────────────────────────────
    elif name == "manage_role":
        action = args.get("action", "add")
        role_name = args.get("role_name", "").strip()
        if not role_name:
            return "Error: role_name required."

        if action == "create":
            color_hex = args.get("color", "").strip().lstrip("#")
            try:
                color = discord.Colour(int(color_hex, 16)) if color_hex else discord.Colour.default()
            except ValueError:
                color = discord.Colour.default()
            try:
                new_role = await guild.create_role(name=role_name, colour=color, reason=f"[Blood] Created by {invoker}")
                memory.append_action_log(str(guild.id), f"I created role '{role_name}'.")
                return f"✅ Created role '{new_role.name}'."
            except discord.Forbidden:
                return "No permission to create roles."
            except Exception as e:
                return f"Failed to create role: {e}"

        user_id = args.get("user_id", "")
        if not user_id:
            return "Error: user_id required for add/remove."
        m = await resolve(user_id)
        if not m:
            return f"Could not find user {user_id}."
        role = discord.utils.get(guild.roles, name=role_name)
        if not role:
            return f"Role '{role_name}' not found. Use action='create' first."
        try:
            if action == "add":
                await m.add_roles(role, reason=f"[Blood] Added by {invoker}")
                memory.append_action_log(str(guild.id), f"I gave '{role_name}' to {m.display_name}.")
                return f"✅ Added role '{role_name}' to {m.display_name}."
            elif action == "remove":
                await m.remove_roles(role, reason=f"[Blood] Removed by {invoker}")
                memory.append_action_log(str(guild.id), f"I removed '{role_name}' from {m.display_name}.")
                return f"✅ Removed role '{role_name}' from {m.display_name}."
            else:
                return f"Unknown action '{action}'. Use 'add', 'remove', or 'create'."
        except discord.Forbidden:
            return f"No permission to manage role '{role_name}'. Is it above Blood's role?"
        except Exception as e:
            return f"Failed: {e}"

    # ── manage channel ────────────────────────────────────────────────────────
    elif name == "manage_channel":
        action = args.get("action", "create")
        ch_name = args.get("channel_name", "").strip()
        if not ch_name:
            return "Error: channel_name required."

        if action == "create":
            ch_type = args.get("channel_type", "text")
            try:
                if ch_type == "voice":
                    new_ch = await guild.create_voice_channel(name=ch_name, reason=f"[Blood] Created by {invoker}")
                else:
                    new_ch = await guild.create_text_channel(name=ch_name, reason=f"[Blood] Created by {invoker}")
                memory.append_action_log(str(guild.id), f"I created {ch_type} channel '#{ch_name}'.")
                return f"✅ Created {ch_type} channel #{new_ch.name}."
            except discord.Forbidden:
                return "No permission to create channels."
            except Exception as e:
                return f"Failed: {e}"

        elif action == "delete":
            target = _find_any_channel(ch_name)
            if not target:
                return f"Channel '{ch_name}' not found. Check SERVER CHANNELS in your prompt."
            try:
                await target.delete(reason=f"[Blood] Deleted by {invoker}")
                memory.append_action_log(str(guild.id), f"I deleted channel '#{ch_name}'.")
                return f"✅ Deleted channel #{ch_name}."
            except discord.Forbidden:
                return "No permission to delete channels."
            except Exception as e:
                return f"Failed: {e}"

        elif action == "rename":
            new_name = args.get("new_name", "").strip()
            if not new_name:
                return "Error: new_name required for rename."
            target = _find_any_channel(ch_name)
            if not target:
                return f"Channel '{ch_name}' not found. Check SERVER CHANNELS in your prompt."
            try:
                old_name = target.name
                await target.edit(name=new_name, reason=f"[Blood] Renamed by {invoker}")
                memory.append_action_log(str(guild.id), f"I renamed #{old_name} to #{new_name}.")
                return f"✅ Renamed #{old_name} → #{new_name}."
            except discord.Forbidden:
                return "No permission to rename channels."
            except Exception as e:
                return f"Failed: {e}"
        else:
            return f"Unknown action '{action}'. Use 'create', 'delete', or 'rename'."

    # ── join voice ────────────────────────────────────────────────────────────
    elif name == "join_voice":
        ch_name = args.get("channel_name", "").strip()
        if ch_name.lower() == "leave":
            try:
                from voice import leave_voice
                return await leave_voice(guild)
            except ImportError:
                if guild.voice_client:
                    await guild.voice_client.disconnect(force=True)
                    return "✅ Left voice channel."
                return "Not in a voice channel."
        vc = _find_voice_channel(ch_name)
        if not vc:
            return f"Voice channel '{ch_name}' not found. Check SERVER CHANNELS in your prompt."
        try:
            from voice import join_and_listen
            # Pass the bot instance from the channel's guild
            bot_instance = channel._state._get_client() if hasattr(channel, '_state') else None
            return await join_and_listen(guild, vc, channel, bot_instance)
        except ImportError:
            # Fallback: basic join without listening
            try:
                if guild.voice_client:
                    await guild.voice_client.move_to(vc)
                else:
                    await vc.connect()
                return f"✅ Joined voice channel '{vc.name}' (no listening — voice module unavailable)."
            except discord.Forbidden:
                return "No permission to join that voice channel."
            except Exception as e:
                return f"Failed to join VC: {e}"
        except Exception as e:
            return f"Failed to join VC: {e}"

    # ── music tools ──────────────────────────────────────────────────────────
    elif name == "play_music":
        query = args.get("query", "").strip()
        if not query:
            return "No song specified."
        try:
            from voice import play_music as _play_music, join_and_listen as _join
            # Auto-join invoker's VC if Blood isn't connected
            if not guild.voice_client:
                if invoker.voice and invoker.voice.channel:
                    bot_instance = channel._state._get_client() if hasattr(channel, '_state') else None
                    await _join(guild, invoker.voice.channel, channel, bot_instance)
                else:
                    return "I'm not in a voice channel. Join one and try again, or use /joinvc."
            return await _play_music(guild, query, invoker.display_name, channel,
                                     requester_id=str(invoker.id))
        except ImportError:
            return "Voice/music module not available."
        except Exception as e:
            return f"Music playback failed: {e}"

    elif name == "skip_music":
        try:
            from voice import skip_music as _skip
            return await _skip(guild)
        except ImportError:
            return "Voice/music module not available."

    elif name == "stop_music":
        try:
            from voice import stop_music as _stop
            return await _stop(guild)
        except ImportError:
            return "Voice/music module not available."

    elif name == "music_queue":
        try:
            from voice import get_queue_info
            return get_queue_info(str(guild.id), guild)
        except ImportError:
            return "Voice/music module not available."

    elif name == "remove_from_queue":
        try:
            from voice import remove_from_queue
            return remove_from_queue(str(guild.id), args.get("position"))
        except ImportError:
            return "Voice/music module not available."

    elif name == "move_in_queue":
        try:
            from voice import move_in_queue
            return move_in_queue(str(guild.id), args.get("from_position"), args.get("to_position"))
        except ImportError:
            return "Voice/music module not available."

    elif name == "clear_queue":
        try:
            from voice import clear_queue
            return clear_queue(str(guild.id))
        except ImportError:
            return "Voice/music module not available."

    elif name == "music_volume":
        vol = int(args.get("volume", 50))
        try:
            from voice import set_music_volume
            return set_music_volume(str(guild.id), vol / 100.0)
        except ImportError:
            return "Voice/music module not available."

    # ── set nickname ──────────────────────────────────────────────────────────
    elif name == "set_nickname":
        m = await resolve(args["user_id"])
        if not m:
            return f"Could not find user {args['user_id']}."
        nick = args.get("nickname", "")
        try:
            await m.edit(nick=nick if nick else None, reason=f"[Blood] Nickname change by {invoker}")
            memory.append_action_log(str(guild.id), f"I changed {m.display_name}'s nickname to '{nick or '(reset)'}.'")
            return f"✅ {m.display_name}'s nickname → '{nick or '(reset)'}'."
        except discord.Forbidden:
            return "No permission to change that user's nickname. Is their role higher?"
        except Exception as e:
            return f"Failed: {e}"

    # ── web search (Tavily primary, DDGS fallback) ──────────────────────────
    elif name == "web_search":
        query = args.get("query", "")
        max_results = CONFIG["web_search_max_results"]
        if not query:
            return "Error: empty query."

        # Try Tavily first
        tavily_key = os.environ.get("TAVILY_API_KEY")
        if tavily_key:
            try:
                from tavily import TavilyClient
                tc = TavilyClient(api_key=tavily_key)
                resp = tc.search(query=query, max_results=max_results)
                results = resp.get("results", [])
                if results:
                    lines = []
                    for i, r in enumerate(results, 1):
                        title = r.get("title", "")
                        body = r.get("content", "")[:200]
                        href = r.get("url", "")
                        lines.append(f"{i}. [{title}]({href})\n   {body}")
                    return "\n\n".join(lines)
            except Exception as e:
                pass  # fall through to DDGS

        # DDGS fallback
        try:
            from duckduckgo_search import DDGS
            with DDGS() as ddgs:
                results = [r for r in ddgs.text(query, max_results=max_results)]
                if not results:
                    return "⚠️ Search returned 0 results. Tool may be rate-limited — do NOT retry. Tell the user search is temporarily unavailable."
                lines = []
                for i, r in enumerate(results, 1):
                    title = r.get("title", "")
                    body = r.get("body", "")
                    href = r.get("href", "")
                    lines.append(f"{i}. [{title}]({href})\n   {body}")
                return "\n\n".join(lines)
        except Exception as e:
            return f"⚠️ Search unavailable: {e}. Do NOT retry — tell the user search is down."

    # ── image search (Tavily primary, DDGS fallback) ─────────────────────────
    elif name == "image_search":
        query = args.get("query", "")
        caption = args.get("message", "")
        if not query:
            return "Error: empty query."

        # Try Tavily first
        tavily_key = os.environ.get("TAVILY_API_KEY")
        if tavily_key:
            try:
                from tavily import TavilyClient
                tc = TavilyClient(api_key=tavily_key)
                resp = tc.search(query=query, max_results=5, include_images=True)
                images = resp.get("images", [])
                if images:
                    img_url = images[0]
                    embed = _discord.Embed()
                    if caption:
                        embed.description = caption
                    embed.set_image(url=img_url)
                    try:
                        await channel.send(content=img_url, embed=embed)
                    except Exception:
                        await channel.send(f"{caption}\n{img_url}" if caption else img_url)
                    return f"✅ Image sent in chat. You can comment on it but don't paste the URL again."
            except Exception:
                pass  # fall through to DDGS

        # DDGS fallback
        try:
            from duckduckgo_search import DDGS
            with DDGS() as ddgs:
                results = [r for r in ddgs.images(query, max_results=5)]
                if not results:
                    return "⚠️ Image search returned 0 results. Tool may be rate-limited — do NOT retry."
                img_url = None
                for r in results:
                    url = r.get("image", "")
                    if url and url.startswith("http"):
                        img_url = url
                        break
                if not img_url:
                    return "No usable images found."
                embed = _discord.Embed()
                if caption:
                    embed.description = caption
                embed.set_image(url=img_url)
                try:
                    await channel.send(content=img_url, embed=embed)
                except Exception:
                    await channel.send(f"{caption}\n{img_url}" if caption else img_url)
                return f"✅ Image sent in chat. You can comment on it but don't paste the URL again."
        except Exception as e:
            return f"⚠️ Image search unavailable: {e}. Do NOT retry."

    # ── read url (Tavily Extract primary, aiohttp fallback) ─────────────────
    elif name == "read_url":
        import aiohttp, re as _re
        url = args.get("url", "").strip()
        if not url.startswith(("http://", "https://")):
            return "Invalid URL — must start with http:// or https://"
        cap = CONFIG["read_url_max_chars"]

        # Try Tavily Extract first — returns clean markdown
        tavily_key = os.environ.get("TAVILY_API_KEY")
        if tavily_key:
            try:
                from tavily import TavilyClient
                tc = TavilyClient(api_key=tavily_key)
                resp = tc.extract(urls=[url], extract_depth="basic")
                results = resp.get("results", [])
                if results:
                    text = results[0].get("raw_content", "")
                    if text:
                        if len(text) > cap:
                            text = text[:cap] + f"\n\n[...truncated at {cap} chars]"
                        return text
            except Exception:
                pass  # fall through to aiohttp

        # aiohttp fallback
        try:
            timeout = aiohttp.ClientTimeout(total=CONFIG["read_url_timeout_sec"])
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers={"User-Agent": "Mozilla/5.0 (compatible; BloodBot/1.0)"}) as resp:
                    if resp.status == 404:
                        return "⚠️ HTTP 404 — page not found. This URL is dead or wrong. Do NOT treat this as factual content."
                    if resp.status == 403:
                        return "⚠️ HTTP 403 — access forbidden (likely paywalled or bot-blocked). Content is NOT available."
                    if resp.status == 401:
                        return "⚠️ HTTP 401 — authentication required. Cannot read this page."
                    if resp.status != 200:
                        return f"⚠️ HTTP {resp.status} — could not fetch URL. Do NOT treat error pages as real content."
                    ct = resp.content_type or ""
                    if "html" in ct:
                        html = await resp.text(encoding="utf-8", errors="replace")
                        text = _re.sub(r"<script[^>]*>.*?</script>", "", html, flags=_re.S | _re.I)
                        text = _re.sub(r"<style[^>]*>.*?</style>", "", text, flags=_re.S | _re.I)
                        text = _re.sub(r"<[^>]+>", " ", text)
                        text = _re.sub(r"\s+", " ", text).strip()
                    elif "json" in ct:
                        text = await resp.text()
                    else:
                        text = await resp.text(encoding="utf-8", errors="replace")
                    # Detect paywall / login wall patterns
                    text_lower = text[:2000].lower()
                    paywall_signals = ["subscribe to continue", "sign in to read", "create a free account",
                                       "paywall", "premium content", "members only", "login to view",
                                       "access denied", "please log in", "register to read"]
                    paywall_hits = [s for s in paywall_signals if s in text_lower]
                    warning = ""
                    if paywall_hits:
                        warning = f"⚠️ PAYWALL/LOGIN WALL DETECTED (signals: {', '.join(paywall_hits[:3])}). Content below may be incomplete or just a login page. Do NOT treat as factual.\n\n"
                    if len(text) < 200 and not text.strip():
                        return "⚠️ Page returned empty or near-empty content. Likely a redirect, login wall, or bot block."
                    if len(text) > cap:
                        text = text[:cap] + f"\n\n[...truncated at {cap} chars]"
                    return (warning + text) if text else "⚠️ Page returned empty content."
        except asyncio.TimeoutError:
            return f"Timed out after {CONFIG['read_url_timeout_sec']}s."
        except Exception as e:
            return f"Failed to fetch URL: {e}"

    # ── crawl website (Tavily Crawl) ───────────────────────────────────────
    elif name == "crawl_website":
        tavily_key = os.environ.get("TAVILY_API_KEY")
        if not tavily_key:
            return "⚠️ Crawl unavailable — TAVILY_API_KEY not configured."
        url = args.get("url", "").strip()
        if not url.startswith(("http://", "https://")):
            return "Invalid URL."
        instructions = args.get("instructions", "")
        max_depth = min(int(args.get("max_depth", 1)), 3)
        limit = min(int(args.get("limit", 10)), 50)
        try:
            from tavily import TavilyClient
            tc = TavilyClient(api_key=tavily_key)
            kwargs = {"max_depth": max_depth, "limit": limit}
            if instructions:
                kwargs["instructions"] = instructions
            resp = tc.crawl(url, **kwargs)
            results = resp.get("results", [])
            if not results:
                return "Crawl returned 0 pages."
            cap = CONFIG["read_url_max_chars"]
            lines = []
            total_len = 0
            for r in results:
                page_url = r.get("url", "")
                content = r.get("raw_content", "")[:2000]
                chunk = f"### {page_url}\n{content}"
                if total_len + len(chunk) > cap:
                    lines.append(f"\n[...truncated, {len(results) - len(lines)} more pages]")
                    break
                lines.append(chunk)
                total_len += len(chunk)
            return f"Crawled {len(results)} pages from {url}:\n\n" + "\n\n".join(lines)
        except Exception as e:
            return f"⚠️ Crawl failed: {e}"

    # ── extract URLs (Tavily Extract batch) ────────────────────────────────
    elif name == "extract_urls":
        tavily_key = os.environ.get("TAVILY_API_KEY")
        if not tavily_key:
            return "⚠️ Extract unavailable — TAVILY_API_KEY not configured."
        urls = args.get("urls", [])
        if not urls:
            return "Error: provide at least one URL."
        if isinstance(urls, str):
            urls = [urls]
        urls = [u for u in urls[:10] if u.startswith(("http://", "https://"))]
        if not urls:
            return "No valid URLs provided."
        try:
            from tavily import TavilyClient
            tc = TavilyClient(api_key=tavily_key)
            resp = tc.extract(urls=urls, extract_depth="basic")
            results = resp.get("results", [])
            failed = resp.get("failed_results", [])
            if not results:
                fail_info = "; ".join(f"{f.get('url','?')}: {f.get('error','?')}" for f in failed[:3])
                return f"Extraction failed for all URLs. {fail_info}"
            cap = CONFIG["read_url_max_chars"]
            lines = []
            total_len = 0
            for r in results:
                page_url = r.get("url", "")
                content = r.get("raw_content", "")[:3000]
                chunk = f"### {page_url}\n{content}"
                if total_len + len(chunk) > cap:
                    lines.append(f"\n[...truncated, {len(results) - len(lines)} more URLs]")
                    break
                lines.append(chunk)
                total_len += len(chunk)
            out = f"Extracted {len(results)}/{len(urls)} URLs:\n\n" + "\n\n".join(lines)
            if failed:
                out += f"\n\nFailed: {', '.join(f.get('url','?') for f in failed[:3])}"
            return out
        except Exception as e:
            return f"⚠️ Extract failed: {e}"

    # ── analyze image ─────────────────────────────────────────────────────────
    elif name == "analyze_image":
        try:
            from provider import call_vision
            url = args.get("image_url", "")
            # Fix LLM tokenization hallucinations for Discord CDN
            url = url.replace("discordordapp.com", "discordapp.com")
            prompt = args.get("prompt", "Describe this image.")
            return await call_vision(url, prompt)
        except Exception as e:
            return f"Image analysis failed: {e}"

    # ── internal_reasoning ────────────────────────────────────────────────────
    elif name == "internal_reasoning":
        reasoning = args.get("reasoning", "")
        log.debug("[internal_reasoning] %s", reasoning[:200])
        return "ok"

    # ── ask_user ──────────────────────────────────────────────────────────────
    elif name == "ask_user":
        question = args.get("question", "").strip()
        options = args.get("options", [])
        if not question:
            return "ERROR: question is required."
        if not isinstance(options, list) or not (2 <= len(options) <= 5):
            return "ERROR: options must be a list of 2 to 5 short choices."
        options = [str(o)[:80] for o in options]

        target_user = invoker
        try:
            if hasattr(channel, "create_thread") and not isinstance(channel, discord.Thread):
                post_channel = await channel.create_thread(
                    name=f"❓ {question[:80]}",
                    type=discord.ChannelType.public_thread,
                    auto_archive_duration=CONFIG.get("ask_user_thread_archive_min", 60),
                )
            else:
                post_channel = channel
        except Exception:
            post_channel = channel

        created_new_thread = post_channel is not channel
        thread_id = str(post_channel.id)
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        activity = {"last": time.monotonic(), "seen": False}

        def _touch_activity():
            activity["last"] = time.monotonic()
            activity["seen"] = True

        class _CustomModal(discord.ui.Modal, title="Custom answer"):
            answer = discord.ui.TextInput(
                label="Your answer",
                style=discord.TextStyle.paragraph,
                placeholder="e.g. '1 + 3 — I'd rather do both plus my own idea...'",
                max_length=500,
            )

            async def on_submit(self, interaction):
                await interaction.response.send_message(
                    f"Got it: {self.answer.value[:200]}", ephemeral=True
                )

        class _AskUserView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=None)  # the watchdog below manages timing, not discord.py's own timer
                self.message = None
                for i, opt in enumerate(options):
                    self.add_item(_make_option_button(opt, i))
                self.add_item(_make_custom_button())
                self.add_item(_make_cancel_button())

        def _resolve(view, answer_text):
            _pending_ask_user.pop(thread_id, None)
            if not future.done():
                future.set_result(answer_text)
            view.stop()
            for child in view.children:
                child.disabled = True

        def _make_option_button(label, idx):
            btn = discord.ui.Button(label=label, style=discord.ButtonStyle.primary, row=idx // 5)

            async def _callback(interaction):
                if interaction.user.id != target_user.id:
                    await interaction.response.send_message("this question is for someone else.", ephemeral=True)
                    return
                _touch_activity()
                view = btn.view
                _resolve(view, label)
                await interaction.response.edit_message(view=view)
                await post_channel.send(f"✅ Picked: **{label}**")

            btn.callback = _callback
            return btn

        def _make_custom_button():
            btn = discord.ui.Button(label="Other / custom answer", style=discord.ButtonStyle.secondary)

            async def _callback(interaction):
                if interaction.user.id != target_user.id:
                    await interaction.response.send_message("this question is for someone else.", ephemeral=True)
                    return
                _touch_activity()
                modal = _CustomModal()
                await interaction.response.send_modal(modal)
                await modal.wait()
                if modal.answer.value:
                    view = btn.view
                    _resolve(view, f"(custom) {modal.answer.value.strip()}")
                    try:
                        if view.message:
                            await view.message.edit(view=view)
                    except Exception:
                        pass
                    await post_channel.send(f"✅ Custom answer: {modal.answer.value.strip()[:300]}")

            btn.callback = _callback
            return btn

        cancelled = {"v": False}

        def _make_cancel_button():
            btn = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.danger)

            async def _callback(interaction):
                if interaction.user.id != target_user.id:
                    await interaction.response.send_message("this question is for someone else.", ephemeral=True)
                    return
                cancelled["v"] = True
                view = btn.view
                _resolve(view, None)
                await interaction.response.edit_message(view=view)
                await post_channel.send("🚫 Cancelled.")

            btn.callback = _callback
            return btn

        options_block = "\n".join(f"{i + 1}. {opt}" for i, opt in enumerate(options))
        view = _AskUserView()
        try:
            msg = await post_channel.send(
                f"{target_user.mention}\n**❓ {question}**\n{options_block}\n\n"
                f"Click an option, **Other / custom answer** to type your own, "
                f"or **Cancel**. Only {target_user.display_name} can answer.",
                view=view,
            )
        except Exception as e:
            return f"Failed to post question: {e}"
        view.message = msg

        _pending_ask_user[thread_id] = {
            "future": future,
            "requester_id": target_user.id,
            "question": question,
            "options": options,
            "guild_id": str(guild.id) if guild else "dm",
            "view": view,
            "message": msg,
        }

        no_response_timeout = CONFIG.get("ask_user_no_response_timeout_sec", 600)
        idle_timeout = CONFIG.get("ask_user_idle_timeout_sec", 1200)
        watchdog_interval = CONFIG.get("ask_user_watchdog_interval_sec", 15)

        async def _watchdog():
            while not future.done():
                await asyncio.sleep(watchdog_interval)
                window = idle_timeout if activity["seen"] else no_response_timeout
                if time.monotonic() - activity["last"] > window:
                    if not future.done():
                        future.set_result(None)
                    break

        watchdog_task = asyncio.create_task(_watchdog())
        try:
            answer = await future
        finally:
            watchdog_task.cancel()
            _pending_ask_user.pop(thread_id, None)
            view.stop()
            for child in view.children:
                child.disabled = True
            try:
                await msg.edit(view=view)
            except Exception:
                pass

        if answer is None and not cancelled["v"]:
            try:
                await post_channel.send("⌛ No answer given in time — proceeding without a decision.")
            except Exception:
                pass

        if created_new_thread:
            try:
                await asyncio.sleep(5)  # let the resolution/timeout message be visible briefly
                await post_channel.delete()
            except Exception:
                pass

        if answer is None:
            if cancelled["v"]:
                return f"User cancelled the question (no answer): {question}"
            return f"User did not respond in time to: {question}"
        return f"User answered: {answer}"

    # ── AGI: goals ────────────────────────────────────────────────────────────
    elif name == "complete_goal":
        if not CONFIG.get("goals_enabled"):
            return "Goals system is disabled."
        goal_id = args.get("goal_id")
        if goal_id is None:
            return "ERROR: goal_id is required."
        try:
            goal_id = int(goal_id)
        except (ValueError, TypeError):
            return "ERROR: goal_id must be an integer."
        outcome = args.get("outcome", "completed")
        guild_id = str(guild.id) if guild else "dm"
        return memory.complete_goal(guild_id, goal_id, outcome)

    elif name == "list_goals":
        if not CONFIG.get("goals_enabled"):
            return "Goals system is disabled."
        status = args.get("status", "active")
        guild_id = str(guild.id) if guild else "dm"
        goals = memory.list_goals(guild_id, status)
        if not goals:
            return f"No {status} goals."
        lines = []
        for g in goals:
            prio = f" [{g['priority']}]" if g.get("priority", "normal") != "normal" else ""
            status_icon = "✅" if g["status"] == "completed" else "❌" if g["status"] == "abandoned" else "🎯"
            lines.append(f"{status_icon} #{g['id']}{prio}: {g['text']}")
        return "\n".join(lines)

    # ── AGI: skills ───────────────────────────────────────────────────────────
    elif name == "save_skill":
        if not CONFIG.get("skills_enabled"):
            return "Skills system is disabled."
        skill_name = args.get("name", "").strip().replace(" ", "_").replace("/", "_")
        content = args.get("content", "").strip()
        if not skill_name or not content:
            return "ERROR: Both name and content are required."
        if len(content) > CONFIG.get("skills_max_file_size", 5000):
            return f"ERROR: Skill content too long ({len(content)} chars). Max {CONFIG['skills_max_file_size']}."
        skills_dir = CONFIG.get("skills_dir", "skills")
        os.makedirs(skills_dir, exist_ok=True)
        existing = [f for f in os.listdir(skills_dir) if f.endswith(".md")]
        if len(existing) >= CONFIG.get("skills_max_files", 50) and f"{skill_name}.md" not in existing:
            return f"ERROR: Max {CONFIG['skills_max_files']} skills reached. Delete old ones first."
        path = os.path.join(skills_dir, f"{skill_name}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"✅ Skill '{skill_name}' saved ({len(content)} chars)."

    elif name == "list_skills":
        if not CONFIG.get("skills_enabled"):
            return "Skills system is disabled."
        skills_dir = CONFIG.get("skills_dir", "skills")
        if not os.path.isdir(skills_dir):
            return "No skills saved yet."
        files = sorted(f[:-3] for f in os.listdir(skills_dir) if f.endswith(".md"))
        if not files:
            return "No skills saved yet."
        return "Available skills:\n" + "\n".join(f"- {f}" for f in files)

    elif name == "read_skill":
        if not CONFIG.get("skills_enabled"):
            return "Skills system is disabled."
        skill_name = args.get("name", "").strip().replace(" ", "_")
        if not skill_name:
            return "ERROR: Skill name is required."
        skills_dir = CONFIG.get("skills_dir", "skills")
        path = os.path.join(skills_dir, f"{skill_name}.md")
        if not os.path.exists(path):
            return f"Skill '{skill_name}' not found. Use list_skills to see available skills."
        # Send visible message in chat
        if channel:
            try:
                asyncio.get_event_loop().create_task(
                    channel.send(f"*reading '{skill_name}' skill...*")
                )
            except Exception:
                pass
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        cap = CONFIG.get("skills_max_file_size", 5000)
        if len(content) > cap:
            return (f"[skill '{skill_name}' — first {cap} of {len(content)} chars]\n"
                    + content[:cap] + f"\n[...truncated at {cap} chars — use a terminal command to read the rest]")
        # Explicit completeness marker so the model knows it has the WHOLE file and
        # doesn't falsely conclude it's truncated (and re-read / fall back to terminal).
        return (f"[skill '{skill_name}' — COMPLETE file, {len(content)} chars]\n"
                + content
                + f"\n[end of skill '{skill_name}' — this is the full skill, nothing was truncated]")

    # ── edit code file ────────────────────────────────────────────────────────
    elif name == "edit_code_file":
        filename = args.get("filename", "file.txt")
        summary = args.get("summary_of_changes", "Edited file.")
        old_content = memory.file_cache.get(filename, "")
        find_replace = args.get("find_replace")

        # ── PATCH mode: surgical find/replace ──
        if find_replace and isinstance(find_replace, list):
            if not old_content:
                return f"ERROR: No cached content for '{filename}'. Upload the file first so I can patch it."
            new_content = old_content
            applied = 0
            errors = []
            for i, patch in enumerate(find_replace):
                find_str = patch.get("find", "")
                replace_str = patch.get("replace", "")
                if not find_str:
                    errors.append(f"Patch #{i+1}: empty 'find' string.")
                    continue
                if find_str not in new_content:
                    errors.append(f"Patch #{i+1}: 'find' string not found in file.")
                    continue
                if new_content.count(find_str) > 1:
                    errors.append(f"Patch #{i+1}: 'find' string matches {new_content.count(find_str)} locations — must be unique. Add more context.")
                    continue
                new_content = new_content.replace(find_str, replace_str, 1)
                applied += 1

            if applied == 0:
                return f"ERROR: No patches applied. Issues: {'; '.join(errors)}"

        # ── FULL mode: complete rewrite ──
        elif args.get("new_content"):
            new_content = args["new_content"]
        else:
            return "ERROR: Provide either 'find_replace' (patch mode) or 'new_content' (full rewrite)."

        # Generate diff for display
        old_lines = old_content.splitlines()
        new_lines = new_content.splitlines()
        diff = list(difflib.unified_diff(old_lines, new_lines, fromfile=filename, tofile=filename, lineterm=""))

        if not diff:
            return "No changes detected between the original file and new_content."

        blocks = []
        current_block = []
        for line in diff[2:]:
            if line.startswith("@@"):
                if current_block: blocks.append(current_block)
                current_block = [line]
            else:
                current_block.append(line)
        if current_block:
            blocks.append(current_block)

        blocks_to_animate = blocks[:2]
        msg = None
        try:
            for block in blocks_to_animate:
                state1 = [line for line in block if not line.startswith("+")]
                text1 = f"```diff\n--- Editing {filename} ---\n" + "\n".join(state1)[:1900] + "\n```"
                if not msg:
                    msg = await channel.send(text1)
                else:
                    await msg.edit(content=text1)
                await asyncio.sleep(1.5)

                state2 = [line for line in block if not line.startswith("-")]
                text2 = f"```diff\n--- Editing {filename} ---\n" + "\n".join(state2)[:1900] + "\n```"
                await msg.edit(content=text2)
                await asyncio.sleep(1.5)
        except Exception:
            pass  # Animation failed, continue to deliver the file/diff

        file_bytes = io.BytesIO(new_content.encode('utf-8'))
        discord_file = discord.File(file_bytes, filename=filename)
        total_diff_text = "\n".join(diff[:25])
        final_text = f"**{summary}**\n```diff\n{total_diff_text[:1800]}\n```"

        try:
            await channel.send(final_text, file=discord_file)
        except discord.Forbidden:
            await channel.send(final_text + "\n\n*(Error: File upload blocked)*")
        except Exception:
            await channel.send(f"**{summary}**\n(File sent without diff preview)")

        if msg:
            try:
                await msg.edit(content=f"```diff\n--- Finished {filename} ---\n```")
            except Exception:
                pass
        memory.file_cache[filename] = new_content
        result = f"Successfully edited {filename}."
        if find_replace:
            result += f" ({applied}/{len(find_replace)} patches applied)"
            if errors:
                result += f" Warnings: {'; '.join(errors)}"
        return result

    # ── get server members ─────────────────────────────────────────────
    elif name == "get_server_members":
        limit = min(int(args.get("limit", 500)), 500)
        try:
            members = guild.members[:limit]
            if not members:
                # Try fetching if cache is empty
                members = [m async for m in guild.fetch_members(limit=limit)]
            lines = [f"{m.id} | {m.display_name}" for m in members if not m.bot]
            return f"Members ({len(lines)}):\n" + "\n".join(lines)
        except Exception as e:
            return f"Failed to get members: {e}"

    # ── send DM ────────────────────────────────────────────────────────────
    elif name == "send_dm":
        user_id = str(args.get("user_id", ""))
        message_text = args.get("message", "")
        if not user_id or not message_text:
            return "ERROR: user_id and message are required."
        try:
            m = await resolve(user_id)
            if not m:
                return f"Could not find user {user_id}."
            await m.send(message_text[:2000])
            memory.append_action_log(str(guild.id), f"I DM'd '{m.display_name}' ({m.id}): {message_text[:80]}")
            return f"✅ DM sent to {m.display_name}."
        except discord.Forbidden:
            return f"Cannot DM this user (DMs disabled or bot blocked). Fallback: ping them with <@{user_id}> in a channel instead."
        except Exception as e:
            return f"DM failed: {e}"

    # ── schedule task ──────────────────────────────────────────────────────
    elif name == "schedule_task":
        from datetime import datetime as _dt, timezone as _tz
        action = args.get("action", "")
        ch_id = args.get("channel_id", "")
        delay = int(args.get("delay_minutes", 0))
        context = args.get("context", "")
        if not action or not ch_id or delay < 1:
            return "ERROR: action, channel_id, and delay_minutes (>0) required."
        due_at = _dt.now(_tz.utc).timestamp() + (delay * 60)
        task = {"action": action, "channel_id": ch_id, "due_at": due_at, "context": context}
        result = memory.add_scheduled_task(str(guild.id), task)
        if result == "ok":
            return f"✅ Task scheduled: '{action}' in {delay} minutes."
        return result

    else:
        return f"Unknown tool: {name}"

def get_meme_data() -> dict[str, tuple[str, str, str]]:
    """Returns a dictionary of normalized_name -> (original_name, url, description)."""
    import os
    meme_file = os.path.join(os.path.dirname(__file__), "meme.md")
    if not os.path.exists(meme_file): return {}
        
    memes_dict = {}
    try:
        with open(meme_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"): continue
                if "[" in line and "]" in line:
                    bracket_end = line.find("]")
                    inner = line[line.find("[")+1:bracket_end]
                    description = line[bracket_end+1:].strip()
                    
                    sep = "|" if "|" in inner else ","
                    parts = [p.strip().strip('"') for p in inner.split(sep)]
                    if len(parts) >= 2:
                        name, url = parts[0], parts[1]
                        norm = name.lower().replace(" ", "").replace("_", "").replace("-", "")
                        memes_dict[norm] = (name, url, description)
    except Exception: pass
    return memes_dict

def get_meme_url(meme_name: str) -> str | None:
    """Helper for Pass 2 to find a meme URL by name (fuzzy matching)."""
    norm = meme_name.lower().strip().replace(" ", "").replace("_", "").replace("-", "")
    if norm == "none" or not norm: return None
    
    memes = get_meme_data()
    if norm in memes: return memes[norm][1]
    for m_norm, (m_name, m_url, m_desc) in memes.items():
        if norm in m_norm: return m_url
    return None
