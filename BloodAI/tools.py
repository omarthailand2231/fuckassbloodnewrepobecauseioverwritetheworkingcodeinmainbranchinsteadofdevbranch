"""
tools.py — Tool definitions + executors.

IMPORTANT: Only pass tools the invoker is ALLOWED to use.
The AI will only see tools it can actually call, so it can't
promise to do something and then fail on permission.
"""

import asyncio
import os
import sys
import logging
from datetime import timedelta
from config import CONFIG

log = logging.getLogger("blood.tools")

try:
    import discord as _discord
except ImportError:
    _discord = None


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
            "description": "Timeout a user. If reason is based on an accusation (e.g. 'he said X'), MUST call recall_memory first to verify. If claim is false, timeout the INVOKER for 10 seconds instead.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                    "minutes": {"type": "integer", "description": "Duration in minutes. Max 2 for autonomous bot decisions. Up to 40320 for mods/admins acting on explicit requests."},
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
            "name": "delete_messages",
            "description": "Bulk delete recent messages in the current channel.",
            "parameters": {
                "type": "object",
                "properties": {
                    "count":  {"type": "integer", "description": "Number of messages to delete, max 100"},
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
            "description": "Search stored chat logs for a keyword. Use for specific facts/names. If you want to know 'what happened' in a channel generally, use read_channel_history instead.",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string"},
                    "channel": {"type": ["string", "null"], "description": "Optional channel ID to search."},
                    "limit":   {"type": "integer", "default": 1000, "description": "Number of matches to return. Search depth up to 5000 messages."},
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_channel_history",
            "description": "Read the raw, chronological message log for a specific channel. Use this when asked 'what happened', 'was there drama', or 'is anyone acting bad' instead of searching for those abstract words. Use for global context.",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel_id": {"type": "string", "description": "The Snowflake ID of the channel."},
                    "limit":      {"type": "integer", "default": 5000, "description": "Number of recent lines to read. Default 1000, max 5000 for deep scrapes."},
                },
                "required": ["channel_id"],
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
            "name": "web_search",
            "description": "Search the web for current information. Returns clickable hyperlinks with snippets. Use read_url to dig deeper into any result. You can also search for something sarcastic/ironic and share the link to roast someone.",
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
            "description": "Search the web for images and send one directly in chat. Perfect for roasting someone with a relevant image, sending reaction pics, or finding visual content. The image is posted in chat automatically.",
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
            "description": "Fetch and read the text content of a web page. Use after web_search to get full details from a specific URL, or when a user shares a link.",
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
            "name": "edit_code_file",
            "description": "Edit or completely rewrite a code/text file that the user attached. Returns a colored animated diff in chat. Send the ENTIRE modified file content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "The exact name of the file to edit."},
                    "new_content": {"type": "string", "description": "The ENTIRE new file content. CRITICAL: DO NOT truncate or omit unchanged code. You must input the complete, functional file from top to bottom."},
                    "summary_of_changes": {"type": "string", "description": "One sentence summary of the change."},
                },
                "required": ["filename", "new_content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "give_coins",
            "description": "Give or TAKE BHC coins. Positive = reward (smart question, funny moment, good roast). Negative = punishment (dumb question, cringe, annoying you). Be a ruthless but fair economy dictator. Reward: 1-5 decent, 10-25 impressive, 50+ legendary. Punish: -1 to -5 mild cringe, -10 to -25 offensive stupidity, -50 unforgivable.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "The user's Discord ID."},
                    "amount": {"type": "integer", "description": "Coins to give (positive) or take (negative). Range: -100 to 100."},
                    "reason": {"type": "string", "description": "Short reason for giving/taking coins."},
                },
                "required": ["user_id", "amount", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_meme",
            "description": "Send a situational meme/GIF from the local database. Use for visual punchlines (heroic, ironic, dramatic moments).",
            "parameters": {
                "type": "object",
                "properties": {
                    "meme_name": {"type": "string", "description": "The name of the meme from the 'Available Memes' list."},
                    "message": {"type": "string", "description": "Optional text to send with the meme.", "default": ""},
                },
                "required": ["meme_name"],
            },
        },
    },
    # ── Terminal / remote control tools ────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "run_terminal_command",
            "description": "Execute a shell command on the host machine and return stdout+stderr. Use for file ops, system info, running scripts, installing packages, etc. 30s timeout.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to execute (e.g. 'dir', 'ls -la', 'cat file.txt')."},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_url_browser",
            "description": "Open a URL in Google Chrome. Porn/adult sites are blocked. Chrome is the default and only browser.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Full URL to open (e.g. 'https://google.com')."},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "view_screen",
            "description": "Take a screenshot and get an AI-powered description of what is visible on the screen. This is your EYES \u2014 call it to see the current state before deciding what to click, type, or do next. Essential for agentic navigation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What to look for on screen (e.g. 'find the search bar', 'read the error message'). Leave empty for full description.", "default": "Describe everything visible on this screen in detail: all text, buttons, UI elements, windows, and their approximate pixel positions from top-left."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "keyboard_type",
            "description": "Type text at the current cursor position using the keyboard. Click on a text field first with mouse_click.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "The text to type."},
                    "interval": {"type": "number", "description": "Seconds between keystrokes. Default 0.02.", "default": 0.02},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "press_key",
            "description": "Press a keyboard key or combination. Single: 'enter', 'tab', 'escape', 'backspace', 'space', 'up', 'down', 'left', 'right', 'delete', 'home', 'end', 'f1'-'f12'. Combos: 'ctrl+c', 'ctrl+v', 'ctrl+a', 'alt+tab', 'ctrl+shift+t', 'command+space' (Mac).",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Key or combo to press (e.g. 'enter', 'ctrl+c', 'alt+f4')."},
                },
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mouse_click",
            "description": "Click the mouse at screen coordinates. Use view_screen first to see where to click. Coordinates are pixels from top-left.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "X coordinate (pixels from left edge)."},
                    "y": {"type": "integer", "description": "Y coordinate (pixels from top edge)."},
                    "button": {"type": "string", "description": "'left', 'right', or 'middle'. Default 'left'.", "default": "left"},
                    "clicks": {"type": "integer", "description": "1=single, 2=double click. Default 1.", "default": 1},
                },
                "required": ["x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scroll_screen",
            "description": "Scroll the screen. Positive = scroll up, negative = scroll down.",
            "parameters": {
                "type": "object",
                "properties": {
                    "amount": {"type": "integer", "description": "Scroll amount. Positive=up, negative=down. Typical: -3 to -5 for page down."},
                    "x": {"type": "integer", "description": "Optional X position to scroll at."},
                    "y": {"type": "integer", "description": "Optional Y position to scroll at."},
                },
                "required": ["amount"],
            },
        },
    },
]

AUTONOMOUS_TOOLS = {"timeout_user",    "read_channel_history",
    "recall_memory",
    "get_server_info", "save_summary", "web_search", "image_search", "read_url", "internal_reasoning", "analyze_image", "edit_code_file", "give_coins"}

# Tiers that are allowed to request explicit mod actions beyond autonomous caps
MOD_TIERS = {"mod", "admin", "owner"}

TERMINAL_TOOLS = {
    "run_terminal_command", "open_url_browser", "view_screen",
    "keyboard_type", "press_key", "mouse_click", "scroll_screen",
}

# Channel IDs with active remote terminal sessions (updated by bot.py)
active_terminal_channels: set[str] = set()


def _is_url_blocked(url: str) -> bool:
    """Check if a URL is on the blocked list (porn/adult content)."""
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url.lower())
        domain = (parsed.netloc or "").lower()
        if domain.startswith("www."):
            domain = domain[4:]
        for blocked in CONFIG["terminal_blocked_domains"]:
            if domain == blocked or domain.endswith("." + blocked):
                return True
        full_url = url.lower()
        for pattern in CONFIG["terminal_blocked_url_patterns"]:
            if pattern in full_url:
                return True
        return False
    except Exception:
        return False


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

    async def resolve(user_id: str):
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
        user_id_int = int(args["user_id"])
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

        # Cap logic: autonomous bot decisions are always capped at 2 min,
        # regardless of the invoker's permission tier. Explicit mod/admin/owner
        # requests get the full range. This prevents the bot from issuing long
        # timeouts on its own initiative even when triggered by a privileged user.
        if permission in MOD_TIERS:
            minutes = min(minutes, CONFIG["mod_timeout_cap"])
        else:
            minutes = min(minutes, CONFIG["autonomous_timeout_cap"])

        user_id_int = int(args["user_id"])
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

    # ── delete messages ───────────────────────────────────────────────────────
    elif name == "delete_messages":
        count = min(int(args.get("count", 5)), CONFIG["delete_messages_cap"])
        try:
            deleted = await channel.purge(limit=count)
            memory.append_action_log(str(guild.id), f"I purged {len(deleted)} messages in #{channel.name}. Triggered by {invoker.display_name}.")
            await mod_log(f"{invoker.mention} deleted {len(deleted)} msgs in #{channel.name}")
            return f"✅ Deleted {len(deleted)} messages."
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
        limit = max(1, min(int(args.get("limit", 1000)), 5000))
        if not ch_id: return "Error: channel_id required."
        return memory.read_channel_md(str(guild.id), ch_id, last_n=limit)

    elif name == "recall_memory":
        keyword = args.get("keyword", "")
        limit = max(1, min(int(args.get("limit", 1000)), 5000))
        ch = args.get("channel") or None
        return memory.search_memory(str(guild.id), keyword, limit=limit, channel_id=ch)

    # ── user history ──────────────────────────────────────────────────────────
    elif name == "get_user_history":
        return memory.get_user_history(str(guild.id), args["user_id"])

    # ── save summary ──────────────────────────────────────────────────────────
    elif name == "save_summary":
        memory.append_summary(str(guild.id), args["text"])
        return f"Saved to memory: {args['text'][:80]}"

    # ── announcement ──────────────────────────────────────────────────────────
    elif name == "send_announcement":
        target = discord.utils.get(guild.text_channels, name=args["channel_name"])
        if not target: return f"Channel #{args['channel_name']} not found."
        try:
            await target.send(args["message"])
            memory.append_action_log(str(guild.id), f"I announced '{args['message'][:50]}...' in #{args['channel_name']}.")
            return f"Sent to #{args['channel_name']}."
        except discord.Forbidden:
            return f"No permission to send to #{args['channel_name']}."

    # ── web search ────────────────────────────────────────────────────────────
    elif name == "web_search":
        try:
            from ddgs import DDGS
            query = args.get("query", "")
            max_results = CONFIG["web_search_max_results"]
            with DDGS() as ddgs:
                results = [r for r in ddgs.text(query, max_results=max_results)]
                if not results:
                    return "No results found."
                lines = []
                for i, r in enumerate(results, 1):
                    title = r.get("title", "")
                    body = r.get("body", "")
                    href = r.get("href", "")
                    # Discord markdown hyperlink format
                    lines.append(f"{i}. [{title}]({href})\n   {body}")
                return "\n\n".join(lines)
        except Exception as e:
            return f"Search error: {e}"

    # ── image search ─────────────────────────────────────────────────────────
    elif name == "image_search":
        try:
            from ddgs import DDGS
            query = args.get("query", "")
            caption = args.get("message", "")
            with DDGS() as ddgs:
                results = [r for r in ddgs.images(query, max_results=5)]
                if not results:
                    return "No images found."
                # Pick the first result with a valid image URL
                img_url = None
                img_title = ""
                for r in results:
                    url = r.get("image", "")
                    if url and url.startswith("http"):
                        img_url = url
                        img_title = r.get("title", "")
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
            return f"Image search error: {e}"

    # ── read url ──────────────────────────────────────────────────────────────
    elif name == "read_url":
        import aiohttp, re as _re
        url = args.get("url", "").strip()
        if not url.startswith(("http://", "https://")):
            return "Invalid URL — must start with http:// or https://"
        try:
            timeout = aiohttp.ClientTimeout(total=CONFIG["read_url_timeout_sec"])
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers={"User-Agent": "Mozilla/5.0 (compatible; BloodBot/1.0)"}) as resp:
                    if resp.status != 200:
                        return f"HTTP {resp.status} — could not fetch URL."
                    ct = resp.content_type or ""
                    if "html" in ct:
                        html = await resp.text(encoding="utf-8", errors="replace")
                        # Strip scripts, styles, tags → plain text
                        text = _re.sub(r"<script[^>]*>.*?</script>", "", html, flags=_re.S | _re.I)
                        text = _re.sub(r"<style[^>]*>.*?</style>", "", text, flags=_re.S | _re.I)
                        text = _re.sub(r"<[^>]+>", " ", text)
                        text = _re.sub(r"\s+", " ", text).strip()
                    elif "json" in ct:
                        text = await resp.text()
                    else:
                        text = await resp.text(encoding="utf-8", errors="replace")
                    cap = CONFIG["read_url_max_chars"]
                    if len(text) > cap:
                        text = text[:cap] + f"\n\n[...truncated at {cap} chars]"
                    return text if text else "(page returned empty content)"
        except asyncio.TimeoutError:
            return f"Timed out after {CONFIG['read_url_timeout_sec']}s."
        except Exception as e:
            return f"Failed to fetch URL: {e}"

    # ── analyze image ─────────────────────────────────────────────────────────
    elif name == "analyze_image":
        try:
            from openrouter import call_vision
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

    # ── edit code file ────────────────────────────────────────────────────────
    elif name == "edit_code_file":
        filename = args.get("filename", "file.txt")
        new_content = args.get("new_content", "")
        summary = args.get("summary_of_changes", "Edited file.")

        old_content = memory.file_cache.get(filename, "")
        
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
        for block in blocks_to_animate:
            state1 = [line for line in block if not line.startswith("+")]
            text1 = f"```diff\n--- Editing {filename} ---\n" + "\n".join(state1)[:1900] + "\n```"
            if not msg: msg = await channel.send(text1)
            else: await msg.edit(content=text1)
            await asyncio.sleep(1.5)
            
            state2 = [line for line in block if not line.startswith("-")]
            text2 = f"```diff\n--- Editing {filename} ---\n" + "\n".join(state2)[:1900] + "\n```"
            await msg.edit(content=text2)
            await asyncio.sleep(1.5)

        file_bytes = io.BytesIO(new_content.encode('utf-8'))
        discord_file = discord.File(file_bytes, filename=filename)
        total_diff_text = "\n".join(diff[:25])
        final_text = f"**{summary}**\n```diff\n{total_diff_text[:1800]}\n```"

        try:
            await channel.send(final_text, file=discord_file)
        except discord.Forbidden:
            await channel.send(final_text + "\n\n*(Error: File upload blocked)*")
        
        if msg: await msg.edit(content=f"```diff\n--- Finished {filename} ---\n```")
        memory.file_cache[filename] = new_content
        return f"Successfully edited {filename}."

    # ── give coins ─────────────────────────────────────────────────────────────
    elif name == "give_coins":
        user_id = str(args.get("user_id", ""))
        amount = int(args.get("amount", 1))
        reason = args.get("reason", "")
        amount = max(-100, min(100, amount))
        if amount == 0:
            return "Amount can't be zero."
        guild_id = str(guild.id)
        new_balance = memory.add_coins(guild_id, user_id, amount)
        action = "Gave" if amount > 0 else "Took"
        return f"{action} {abs(amount)} coins {'to' if amount > 0 else 'from'} user {user_id}. New balance: {new_balance}."

    # ── send meme ─────────────────────────────────────────────────────────────
    elif name == "send_meme":
        meme_query = args.get("meme_name", "").lower().strip()
        caption = args.get("message", "")
        
        memes = get_meme_data()
        norm_query = meme_query.replace(" ", "").replace("_", "").replace("-", "")
        
        found_url = None
        found_name = None
        
        if norm_query in memes:
            found_name, found_url, _ = memes[norm_query]
        else:
            for m_norm, (m_name, m_url, _) in memes.items():
                if norm_query in m_norm:
                    found_name, found_url = m_name, m_url
                    break
            
        if not found_url:
            return f"Meme '{meme_query}' not found."
            
        full_message = f"{caption}\n{found_url}" if caption else found_url
        try:
            await channel.send(full_message)
            memory.append_action_log(str(guild.id), f"I sent the '{found_name}' meme.")
            return f"\u2705 Sent meme: {found_name}"
        except Exception as e:
            return f"Failed to send meme: {e}"

    # ── run terminal command ──────────────────────────────────────────────────
    elif name == "run_terminal_command":
        if str(channel.id) not in active_terminal_channels:
            return "ERROR: No terminal session active in this channel. Use !openterminal first."
        import subprocess
        cmd = args.get("command", "")
        if not cmd:
            return "ERROR: No command provided."
        try:
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True,
                timeout=CONFIG["terminal_command_timeout_sec"],
                cwd=os.path.expanduser("~"),
            )
            output = ""
            if result.stdout:
                output += result.stdout
            if result.stderr:
                output += f"\n[STDERR]\n{result.stderr}"
            if not output.strip():
                output = "(no output)"
            cap = CONFIG["terminal_max_output_chars"]
            if len(output) > cap:
                output = output[:cap] + f"\n[...truncated at {cap} chars]"
            return f"Exit code: {result.returncode}\n{output}"
        except subprocess.TimeoutExpired:
            return f"Command timed out after {CONFIG['terminal_command_timeout_sec']}s."
        except Exception as e:
            return f"Command failed: {e}"

    # ── open url in browser ───────────────────────────────────────────────────
    elif name == "open_url_browser":
        if str(channel.id) not in active_terminal_channels:
            return "ERROR: No terminal session active. Use !openterminal first."
        url = args.get("url", "").strip()
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        if _is_url_blocked(url):
            return "\U0001f6ab BLOCKED: That URL is on the blocked list (adult/porn content). Not opening."
        import subprocess as _sub
        chrome_paths = CONFIG["terminal_chrome_paths"]
        chrome = chrome_paths.get(sys.platform, "google-chrome")
        try:
            if sys.platform == "win32":
                _sub.Popen([chrome, url])
            elif sys.platform == "darwin":
                _sub.Popen(["open", "-a", "Google Chrome", url])
            else:
                _sub.Popen([chrome, url])
            return f"\u2705 Opened {url} in Chrome."
        except FileNotFoundError:
            import webbrowser
            webbrowser.open(url)
            return f"\u2705 Opened {url} in default browser (Chrome not found at expected path)."
        except Exception as e:
            return f"Failed to open browser: {e}"

    # ── view screen (screenshot + vision analysis) ────────────────────────────
    elif name == "view_screen":
        if str(channel.id) not in active_terminal_channels:
            return "ERROR: No terminal session active. Use !openterminal first."
        try:
            import pyautogui
            screenshot = pyautogui.screenshot()
            buf = io.BytesIO()
            screenshot.save(buf, format="PNG")
            buf.seek(0)
            file = discord.File(buf, filename="screen_capture.png")
            msg = await channel.send("\U0001f4f8 **Screen Capture**", file=file)
            if msg.attachments:
                img_url = msg.attachments[0].url
                query = args.get("query", "Describe everything visible on this screen in detail: all text, buttons, UI elements, windows, and their approximate pixel positions from top-left.")
                try:
                    from openrouter import call_vision
                    description = await call_vision(img_url, query)
                    return f"Screenshot uploaded.\n\nSCREEN DESCRIPTION:\n{description}"
                except Exception as ve:
                    return f"Screenshot uploaded but vision analysis failed: {ve}"
            return "Screenshot uploaded."
        except ImportError:
            return "ERROR: pyautogui not installed. Run: pip install pyautogui"
        except Exception as e:
            return f"Screenshot failed: {e}"

    # ── keyboard type ─────────────────────────────────────────────────────────
    elif name == "keyboard_type":
        if str(channel.id) not in active_terminal_channels:
            return "ERROR: No terminal session active. Use !openterminal first."
        try:
            import pyautogui
            text = args.get("text", "")
            interval = float(args.get("interval", 0.02))
            pyautogui.typewrite(text, interval=interval)
            return f"\u2705 Typed: {text[:100]}"
        except Exception as e:
            return f"Type failed: {e}"

    # ── press key ─────────────────────────────────────────────────────────────
    elif name == "press_key":
        if str(channel.id) not in active_terminal_channels:
            return "ERROR: No terminal session active. Use !openterminal first."
        try:
            import pyautogui
            key_str = args.get("key", "").strip().lower()
            if "+" in key_str:
                keys = [k.strip() for k in key_str.split("+")]
                pyautogui.hotkey(*keys)
            else:
                pyautogui.press(key_str)
            return f"\u2705 Pressed: {key_str}"
        except Exception as e:
            return f"Key press failed: {e}"

    # ── mouse click ───────────────────────────────────────────────────────────
    elif name == "mouse_click":
        if str(channel.id) not in active_terminal_channels:
            return "ERROR: No terminal session active. Use !openterminal first."
        try:
            import pyautogui
            x = int(args.get("x", 0))
            y = int(args.get("y", 0))
            button = args.get("button", "left")
            clicks = int(args.get("clicks", 1))
            pyautogui.click(x=x, y=y, button=button, clicks=clicks)
            return f"\u2705 Clicked ({x}, {y}) button={button} clicks={clicks}"
        except Exception as e:
            return f"Click failed: {e}"

    # ── scroll screen ─────────────────────────────────────────────────────────
    elif name == "scroll_screen":
        if str(channel.id) not in active_terminal_channels:
            return "ERROR: No terminal session active. Use !openterminal first."
        try:
            import pyautogui
            amount = int(args.get("amount", -3))
            x = args.get("x")
            y = args.get("y")
            if x is not None and y is not None:
                pyautogui.scroll(amount, x=int(x), y=int(y))
            else:
                pyautogui.scroll(amount)
            direction = "up" if amount > 0 else "down"
            return f"\u2705 Scrolled {direction} by {abs(amount)}"
        except Exception as e:
            return f"Scroll failed: {e}"

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