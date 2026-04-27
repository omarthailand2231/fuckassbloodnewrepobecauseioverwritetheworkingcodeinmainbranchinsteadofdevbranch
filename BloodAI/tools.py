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
            "description": "Join or leave a voice channel.",
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
            "name": "give_coins",
            "description": "Give or TAKE BHC coins. Positive = reward (smart question, funny moment, good roast). Negative = punishment (dumb question, cringe, annoying you). Be a ruthless but fair economy dictator. Reward: 1-5 decent, 10-25 impressive, 50+ legendary. Punish: -1 to -5 mild cringe, -10 to -25 offensive stupidity, -50 unforgivable.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "The user's Discord ID."},
                    "amount": {"type": "integer", "description": "Coins to give (positive) or take (negative). No limit — go as hard as you want."},
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
            "description": "Send a direct message to a user. Use sparingly — DMs lose impact if overused. Good for: warnings, intimidation, private praise, follow-ups.",
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
            "name": "request_capability",
            "description": "Request a new capability or permission from your owner. Posts to the requests channel. Use when you encounter something you can't do but should be able to.",
            "parameters": {
                "type": "object",
                "properties": {
                    "capability": {"type": "string", "description": "What capability you need (e.g. 'access to #admin channel', 'ability to change nicknames')."},
                    "reason": {"type": "string", "description": "Why you need it."},
                },
                "required": ["capability", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_emotional_state",
            "description": "Update your emotional state towards a user. Affects how you treat them in future interactions. Use after notable events (someone earned respect, someone pissed you off, etc).",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                    "annoyance": {"type": "integer", "description": "Delta change (-100 to +100). Positive = more annoyed."},
                    "respect": {"type": "integer", "description": "Delta change (-100 to +100). Positive = more respect."},
                    "grudge": {"type": "integer", "description": "Delta change (-100 to +100). Positive = holding more of a grudge."},
                    "category": {"type": "string", "description": "Optional: 'feared', 'respected', 'disrespected', 'neutral'."},
                    "notes": {"type": "string", "description": "Optional short note about why."},
                },
                "required": ["user_id"],
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
            "name": "set_goal",
            "description": "Set a persistent goal for yourself. Goals survive restarts and appear in your system prompt. Use for: grudges to follow up on, things to learn, promises you made, recurring tasks. You are an autonomous agent — set goals to drive your own behavior.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "The goal. Be specific and actionable."},
                    "priority": {"type": "string", "enum": ["low", "normal", "high", "critical"], "default": "normal"},
                },
                "required": ["text"],
            },
        },
    },
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
            "description": "Read a saved skill file for instructions on how to handle a task.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Skill name (without .md extension)."},
                },
                "required": ["name"],
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
            "description": "Click the mouse at screen coordinates. Use view_screen first to see where to click. Coordinates are LOGICAL pixels (not Retina) from top-left. The screenshot from view_screen is already resized to logical pixels so you can use those positions directly.",
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
            "name": "mouse_move",
            "description": "Smoothly move the mouse to screen coordinates WITHOUT clicking. Use for hovering, aiming, or positioning before a drag.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "X coordinate (pixels from left edge)."},
                    "y": {"type": "integer", "description": "Y coordinate (pixels from top edge)."},
                    "duration": {"type": "number", "description": "Seconds to glide. Default 0.4. Use 0.1 for fast, 1.0 for slow.", "default": 0.4},
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
    # ── Browser DOM tools (DISABLED — buggy, re-enable later) ─────────────────
    # browser_connect, browser_navigate, browser_click, browser_type,
    # browser_read, browser_query, browser_eval, browser_close
]

AUTONOMOUS_TOOLS = {
    "timeout_user", "read_channel_history", "recall_memory",
    "get_server_info", "save_summary", "web_search", "image_search",
    "read_url", "crawl_website", "extract_urls", "internal_reasoning", "analyze_image", "edit_code_file",
    "give_coins", "send_dm", "request_capability", "update_emotional_state",
    "schedule_task", "delete_messages", "get_server_members",
    "set_goal", "complete_goal", "list_goals", "save_skill", "list_skills", "read_skill",
}

# Tiers that are allowed to request explicit mod actions beyond autonomous caps
MOD_TIERS = {"mod", "admin", "owner"}

TERMINAL_TOOLS = {
    "run_terminal_command", "open_url_browser", "view_screen",
    "keyboard_type", "press_key", "mouse_click", "mouse_move", "scroll_screen",
    # browser DOM tools disabled for now
    # "browser_connect", "browser_navigate", "browser_click", "browser_type",
    # "browser_read", "browser_query", "browser_eval", "browser_close",
}

# Channel IDs with active remote terminal sessions (updated by bot.py)
active_terminal_channels: set[str] = set()
# Channel IDs with fastimg mode (terse vision output for gaming, updated by bot.py)
fastimg_channels: set[str] = set()

# ── Playwright CDP browser sessions ──────────────────────────────────────────
_browser_sessions: dict[str, dict] = {}  # channel_id -> {"playwright", "browser", "page"}
_CDP_PORT = 9222

async def _get_browser_page(channel_id: str):
    """Return the active Playwright page for a channel, or None."""
    session = _browser_sessions.get(channel_id)
    if session and session.get("page"):
        try:
            await session["page"].evaluate("1")  # health check
            return session["page"]
        except Exception:
            await _close_browser_session(channel_id)
    return None

async def _connect_browser(channel_id: str, url: str | None = None):
    """Connect to Chrome via CDP. Launch Chrome with debug port if needed."""
    import subprocess as _sub
    import asyncio as _aio

    # Check if Chrome is listening on CDP port
    import aiohttp
    cdp_url = f"http://127.0.0.1:{_CDP_PORT}"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{cdp_url}/json/version", timeout=aiohttp.ClientTimeout(total=2)) as r:
                if r.status != 200:
                    raise ConnectionError()
    except Exception:
        # Kill existing Chrome and relaunch with debug port (no --user-data-dir so it uses default profile)
        import os as _os
        chrome_paths = CONFIG["terminal_chrome_paths"]
        import sys as _sys
        chrome = chrome_paths.get(_sys.platform, "google-chrome")
        try:
            if _sys.platform == "darwin":
                _os.system("pkill -f 'Google Chrome'")
                await _aio.sleep(2)
                _sub.Popen([
                    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                    f"--remote-debugging-port={_CDP_PORT}",
                ])
            elif _sys.platform == "win32":
                _os.system("taskkill /f /im chrome.exe >nul 2>&1")
                await _aio.sleep(2)
                _sub.Popen([chrome, f"--remote-debugging-port={_CDP_PORT}"])
            else:
                _os.system("pkill -f chrome")
                await _aio.sleep(2)
                _sub.Popen([chrome, f"--remote-debugging-port={_CDP_PORT}"])
            await _aio.sleep(3)  # wait for Chrome to start
        except Exception as e:
            return None, f"Failed to launch Chrome with debug port: {e}"

    # Connect Playwright to Chrome
    try:
        from playwright.async_api import async_playwright
        pw = await async_playwright().start()
        browser = await pw.chromium.connect_over_cdp(cdp_url)
        # Get the default context's first page, or create one
        contexts = browser.contexts
        if contexts and contexts[0].pages:
            page = contexts[0].pages[0]
        else:
            ctx = contexts[0] if contexts else await browser.new_context()
            page = await ctx.new_page()

        if url:
            await page.goto(url, wait_until="domcontentloaded", timeout=15000)

        _browser_sessions[channel_id] = {"playwright": pw, "browser": browser, "page": page}
        return page, None
    except Exception as e:
        return None, f"Failed to connect to Chrome CDP: {e}"

async def _close_browser_session(channel_id: str):
    """Disconnect Playwright from Chrome (doesn't close Chrome)."""
    session = _browser_sessions.pop(channel_id, None)
    if session:
        try:
            await session["browser"].close()
        except Exception:
            pass
        try:
            await session["playwright"].stop()
        except Exception:
            pass


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
        results = memory.hybrid_search(str(guild.id), keyword, limit=limit, channel_id=ch)
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
        target = discord.utils.get(guild.text_channels, name=args["channel_name"])
        if not target: return f"Channel #{args['channel_name']} not found."
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
            target = discord.utils.get(guild.channels, name=ch_name)
            if not target:
                return f"Channel '{ch_name}' not found."
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
            target = discord.utils.get(guild.channels, name=ch_name)
            if not target:
                return f"Channel '{ch_name}' not found."
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
            if guild.voice_client:
                await guild.voice_client.disconnect(force=True)
                return "✅ Left voice channel."
            return "Not in a voice channel."
        vc = discord.utils.get(guild.voice_channels, name=ch_name)
        if not vc:
            return f"Voice channel '{ch_name}' not found."
        try:
            if guild.voice_client:
                await guild.voice_client.move_to(vc)
            else:
                await vc.connect()
            return f"✅ Joined voice channel '{vc.name}'."
        except discord.Forbidden:
            return "No permission to join that voice channel."
        except Exception as e:
            return f"Failed to join VC: {e}"

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

    # ── AGI: goals ────────────────────────────────────────────────────────────
    elif name == "set_goal":
        if not CONFIG.get("goals_enabled"):
            return "Goals system is disabled."
        text = args.get("text", "").strip()
        if not text:
            return "ERROR: Goal text is required."
        priority = args.get("priority", "normal")
        guild_id = str(guild.id) if guild else "dm"
        result = memory.set_goal(guild_id, text, priority)
        if "error" in result:
            return result["error"]
        return f"✅ Goal #{result['id']} set: {text} [{priority}]"

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
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        cap = CONFIG.get("skills_max_file_size", 5000)
        return content[:cap]

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
        result = f"Successfully edited {filename}."
        if find_replace:
            result += f" ({applied}/{len(find_replace)} patches applied)"
            if errors:
                result += f" Warnings: {'; '.join(errors)}"
        return result

    # ── give coins ─────────────────────────────────────────────────────────────
    elif name == "give_coins":
        user_id = str(args.get("user_id", ""))
        amount = int(args.get("amount", 1))
        reason = args.get("reason", "")
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
            from PIL import ImageDraw, ImageFont
            screenshot = pyautogui.screenshot()
            # Resize Retina screenshots to logical pixel size so vision coords match mouse_click coords
            logical_w, logical_h = pyautogui.size()
            if screenshot.width > logical_w:
                screenshot = screenshot.resize((logical_w, logical_h))

            # Draw coordinate grid overlay so vision model can read exact positions
            grid_img = screenshot.copy()
            draw = ImageDraw.Draw(grid_img, "RGBA")
            grid_spacing = 100  # pixels between grid lines
            label_color = (255, 0, 0, 200)
            line_color = (255, 0, 0, 60)
            try:
                font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 11)
            except Exception:
                font = ImageFont.load_default()
            for x in range(0, logical_w, grid_spacing):
                draw.line([(x, 0), (x, logical_h)], fill=line_color, width=1)
                if x > 0:
                    draw.text((x + 2, 2), str(x), fill=label_color, font=font)
            for y in range(0, logical_h, grid_spacing):
                draw.line([(0, y), (logical_w, y)], fill=line_color, width=1)
                if y > 0:
                    draw.text((2, y + 2), str(y), fill=label_color, font=font)

            # Send clean screenshot to chat (no grid)
            buf_clean = io.BytesIO()
            screenshot.save(buf_clean, format="PNG")
            buf_clean.seek(0)
            file = discord.File(buf_clean, filename="screen_capture.png")
            msg = await channel.send("\U0001f4f8 **Screen Capture**", file=file)

            # Send gridded version to vision model (not shown to user)
            buf_grid = io.BytesIO()
            grid_img.save(buf_grid, format="PNG")
            buf_grid.seek(0)
            grid_file = discord.File(buf_grid, filename="screen_grid.png")
            grid_msg = await channel.send(file=grid_file)
            vision_url = grid_msg.attachments[0].url if grid_msg.attachments else (msg.attachments[0].url if msg.attachments else None)
            # Delete the grid image from chat — it was only needed for the vision URL
            try:
                await grid_msg.delete()
            except Exception:
                pass

            if vision_url:
                ch_id = str(channel.id)
                if ch_id in fastimg_channels:
                    query = args.get("query", f"This screenshot has a red coordinate grid overlay with labels every {grid_spacing}px. Screen is {logical_w}x{logical_h}. Use the grid numbers to determine EXACT (x,y) positions. List ONLY clickable elements with their (x,y) coordinates. Format: element_name (x, y). No descriptions, no prose. Be fast.")
                    try:
                        from provider import call_fast_vision
                        description = await call_fast_vision(vision_url, query)
                        return f"Screenshot uploaded. Screen size: {logical_w}x{logical_h} (use these coords for mouse_click).\n\nSCREEN DESCRIPTION:\n{description}"
                    except Exception as ve:
                        return f"Screenshot uploaded but fast vision failed: {ve}"
                else:
                    query = args.get("query", f"This screenshot has a red coordinate grid overlay with labels every {grid_spacing}px. The screen is {logical_w}x{logical_h} pixels. Use the grid numbers to determine EXACT (x,y) pixel positions of all visible UI elements, buttons, text fields, links, and windows. Coordinates must be precise — read them from the grid, do not estimate.")
                try:
                    from provider import call_vision
                    description = await call_vision(vision_url, query)
                    return f"Screenshot uploaded. Screen size: {logical_w}x{logical_h} (use these coords for mouse_click).\n\nSCREEN DESCRIPTION:\n{description}"
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
            cx, cy = pyautogui.position()
            dist = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
            max_dist = (pyautogui.size()[0] ** 2 + pyautogui.size()[1] ** 2) ** 0.5
            dur = 0.3 + (dist / max_dist) * (1.5 - 0.3)
            dur = max(0.3, min(1.5, dur))
            pyautogui.moveTo(x, y, duration=dur, tween=pyautogui.easeInOutQuad)
            pyautogui.click(x=x, y=y, button=button, clicks=clicks)
            return f"\u2705 Moved smoothly → clicked ({x}, {y}) button={button} clicks={clicks}"
        except Exception as e:
            return f"Click failed: {e}"

    # ── mouse move (smooth, no click) ─────────────────────────────────────────
    elif name == "mouse_move":
        if str(channel.id) not in active_terminal_channels:
            return "ERROR: No terminal session active. Use !openterminal first."
        try:
            import pyautogui
            x = int(args.get("x", 0))
            y = int(args.get("y", 0))
            cx, cy = pyautogui.position()
            dist = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
            max_dist = (pyautogui.size()[0] ** 2 + pyautogui.size()[1] ** 2) ** 0.5
            dur = 0.3 + (dist / max_dist) * (1.5 - 0.3)
            dur = max(0.3, min(1.5, float(args.get("duration", dur))))
            pyautogui.moveTo(x, y, duration=dur, tween=pyautogui.easeInOutQuad)
            return f"\u2705 Mouse moved smoothly to ({x}, {y}) over {dur:.2f}s"
        except Exception as e:
            return f"Mouse move failed: {e}"

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

    # ── Browser DOM tools (Playwright CDP) ──────────────────────────────────
    elif name == "browser_connect":
        if str(channel.id) not in active_terminal_channels:
            return "ERROR: No terminal session active. Use !openterminal first."
        url = args.get("url", "").strip() or None
        if url and not url.startswith(("http://", "https://")):
            url = "https://" + url
        if url and _is_url_blocked(url):
            return "\U0001f6ab BLOCKED: That URL is on the blocked list."
        ch_id = str(channel.id)
        # Close existing session if any
        if ch_id in _browser_sessions:
            await _close_browser_session(ch_id)
        page, err = await _connect_browser(ch_id, url)
        if err:
            return f"❌ {err}"
        title = await page.title()
        return f"✅ Connected to Chrome via CDP. Page: {title or '(blank)'} — {page.url}"

    elif name == "browser_navigate":
        if str(channel.id) not in active_terminal_channels:
            return "ERROR: No terminal session active. Use !openterminal first."
        ch_id = str(channel.id)
        page = await _get_browser_page(ch_id)
        if not page:
            return "ERROR: No browser session. Use browser_connect first."
        action = args.get("action", "").lower()
        try:
            if action == "goto":
                url = args.get("url", "").strip()
                if not url:
                    return "ERROR: url required for 'goto' action."
                if not url.startswith(("http://", "https://")):
                    url = "https://" + url
                if _is_url_blocked(url):
                    return "\U0001f6ab BLOCKED: That URL is on the blocked list."
                await page.goto(url, wait_until="domcontentloaded", timeout=15000)
            elif action == "back":
                await page.go_back(wait_until="domcontentloaded", timeout=10000)
            elif action == "forward":
                await page.go_forward(wait_until="domcontentloaded", timeout=10000)
            elif action == "refresh":
                await page.reload(wait_until="domcontentloaded", timeout=10000)
            else:
                return f"ERROR: Unknown action '{action}'. Use goto/back/forward/refresh."
            title = await page.title()
            return f"✅ {action} → {page.url} ({title})"
        except Exception as e:
            return f"Navigation failed: {e}"

    elif name == "browser_click":
        if str(channel.id) not in active_terminal_channels:
            return "ERROR: No terminal session active. Use !openterminal first."
        ch_id = str(channel.id)
        page = await _get_browser_page(ch_id)
        if not page:
            return "ERROR: No browser session. Use browser_connect first."
        selector = args.get("selector", "").strip()
        text = args.get("text", "").strip()
        button = args.get("button", "left")
        double = args.get("double", False)
        try:
            if text and not selector:
                # Click by text content
                loc = page.get_by_text(text, exact=False).first
            elif selector:
                loc = page.locator(selector).first
            else:
                return "ERROR: Provide either 'selector' or 'text' to identify the element."
            if double:
                await loc.dblclick(button=button, timeout=5000)
            else:
                await loc.click(button=button, timeout=5000)
            desc = selector or f'text="{text}"'
            return f"✅ Clicked: {desc}"
        except Exception as e:
            # Fallback hint
            return f"DOM click failed: {e}\nTip: Use view_screen + mouse_click as fallback."

    elif name == "browser_type":
        if str(channel.id) not in active_terminal_channels:
            return "ERROR: No terminal session active. Use !openterminal first."
        ch_id = str(channel.id)
        page = await _get_browser_page(ch_id)
        if not page:
            return "ERROR: No browser session. Use browser_connect first."
        selector = args.get("selector", "").strip()
        text = args.get("text", "")
        press_enter = args.get("press_enter", False)
        if not selector:
            return "ERROR: 'selector' required."
        try:
            await page.fill(selector, text, timeout=5000)
            if press_enter:
                await page.press(selector, "Enter")
            return f"✅ Typed into {selector}: {text[:80]}" + (" + Enter" if press_enter else "")
        except Exception as e:
            return f"Type failed: {e}\nTip: Use keyboard_type as fallback."

    elif name == "browser_read":
        if str(channel.id) not in active_terminal_channels:
            return "ERROR: No terminal session active. Use !openterminal first."
        ch_id = str(channel.id)
        page = await _get_browser_page(ch_id)
        if not page:
            return "ERROR: No browser session. Use browser_connect first."
        selector = args.get("selector", "").strip()
        mode = args.get("mode", "text").lower()
        limit = min(int(args.get("limit", 4000)), 8000)
        try:
            if selector:
                el = page.locator(selector).first
                if mode == "html":
                    content = await el.inner_html()
                elif mode == "value":
                    content = await el.input_value()
                elif mode == "attrs":
                    content = await el.evaluate("el => JSON.stringify(Object.fromEntries([...el.attributes].map(a => [a.name, a.value])))")
                else:
                    content = await el.inner_text()
            else:
                if mode == "html":
                    content = await page.content()
                else:
                    content = await page.inner_text("body")
            if len(content) > limit:
                content = content[:limit] + f"\n[...truncated at {limit} chars]"
            return content or "(empty)"
        except Exception as e:
            return f"Read failed: {e}\nTip: Use view_screen as fallback."

    elif name == "browser_query":
        if str(channel.id) not in active_terminal_channels:
            return "ERROR: No terminal session active. Use !openterminal first."
        ch_id = str(channel.id)
        page = await _get_browser_page(ch_id)
        if not page:
            return "ERROR: No browser session. Use browser_connect first."
        selector = args.get("selector", "").strip()
        limit = min(int(args.get("limit", 20)), 50)
        if not selector:
            return "ERROR: 'selector' required."
        try:
            elements = await page.locator(selector).all()
            results = []
            for i, el in enumerate(elements[:limit]):
                tag = await el.evaluate("el => el.tagName.toLowerCase()")
                text_content = (await el.inner_text())[:80].replace("\n", " ").strip()
                href = await el.get_attribute("href") or ""
                cls = await el.get_attribute("class") or ""
                el_id = await el.get_attribute("id") or ""
                attrs = []
                if el_id:
                    attrs.append(f'id="{el_id}"')
                if cls:
                    attrs.append(f'class="{cls[:50]}"')
                if href:
                    attrs.append(f'href="{href[:80]}"')
                attr_str = " ".join(attrs)
                results.append(f"[{i}] <{tag} {attr_str}> {text_content}")
            if not results:
                return f"No elements found for: {selector}"
            return f"Found {len(elements)} elements (showing {len(results)}):\n" + "\n".join(results)
        except Exception as e:
            return f"Query failed: {e}"

    elif name == "browser_eval":
        if str(channel.id) not in active_terminal_channels:
            return "ERROR: No terminal session active. Use !openterminal first."
        ch_id = str(channel.id)
        page = await _get_browser_page(ch_id)
        if not page:
            return "ERROR: No browser session. Use browser_connect first."
        code = args.get("code", "").strip()
        if not code:
            return "ERROR: 'code' required."
        try:
            result = await page.evaluate(code)
            result_str = json.dumps(result, ensure_ascii=False, default=str) if result is not None else "undefined"
            if len(result_str) > 4000:
                result_str = result_str[:4000] + "\n[...truncated]"
            return f"Result: {result_str}"
        except Exception as e:
            return f"JS eval failed: {e}"

    elif name == "browser_close":
        if str(channel.id) not in active_terminal_channels:
            return "ERROR: No terminal session active. Use !openterminal first."
        ch_id = str(channel.id)
        if ch_id not in _browser_sessions:
            return "No browser session to close."
        await _close_browser_session(ch_id)
        return "✅ Disconnected from Chrome. (Chrome itself is still running.)"

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

    # ── request capability ─────────────────────────────────────────────────
    elif name == "request_capability":
        capability = args.get("capability", "")
        reason = args.get("reason", "")
        req_ch_id = CONFIG.get("requests_channel_id")
        if not req_ch_id:
            memory.append_action_log(str(guild.id), f"[CAPABILITY REQUEST] {capability} — {reason} (no requests channel configured)")
            return "Request logged internally. No requests channel is configured."
        try:
            req_channel = guild.get_channel(int(req_ch_id))
            if req_channel:
                await req_channel.send(
                    f"**🔧 CAPABILITY REQUEST**\n"
                    f"**Need:** {capability}\n"
                    f"**Why:** {reason}\n"
                    f"*— Blood*"
                )
                memory.append_action_log(str(guild.id), f"[CAPABILITY REQUEST] {capability} — {reason}")
                return f"✅ Request posted to requests channel."
            return "Requests channel not found."
        except Exception as e:
            return f"Request failed: {e}"

    # ── update emotional state ─────────────────────────────────────────────
    elif name == "update_emotional_state":
        user_id = str(args.get("user_id", ""))
        if not user_id:
            return "ERROR: user_id required."
        deltas = {}
        for key in ("annoyance", "respect", "grudge"):
            if key in args:
                try:
                    deltas[key] = int(args[key])
                except (ValueError, TypeError):
                    pass
        if "category" in args:
            deltas["category"] = args["category"]
        if "notes" in args:
            deltas["notes"] = args["notes"]
        if not deltas:
            return "Nothing to update."
        memory.update_user_emotional_data(str(guild.id), user_id, deltas)
        return f"✅ Emotional state updated for {user_id}: {deltas}"

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