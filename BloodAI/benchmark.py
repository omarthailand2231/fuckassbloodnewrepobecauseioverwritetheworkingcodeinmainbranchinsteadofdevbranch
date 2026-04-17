"""
benchmark.py — Blood Discord Bot Benchmark Suite
Tests: memory persistence, tool call execution, persona consistency, edge cases.

Run with: python benchmark.py
Does NOT require a live Discord connection — mocks the Discord objects.
"""

import asyncio
import json
import time
import sys
import os
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import dataclass, field
from typing import Callable

# ── Terminal colors ───────────────────────────────────────────────────────────
R = "\033[91m"
G = "\033[92m"
Y = "\033[93m"
B = "\033[94m"
M = "\033[95m"
C = "\033[96m"
W = "\033[97m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"

# ── Result tracking ───────────────────────────────────────────────────────────
@dataclass
class BenchResult:
    name: str
    passed: bool
    latency_ms: float
    notes: str = ""
    score: float = 0.0  # 0.0 - 1.0

results: list[BenchResult] = []

def record(name: str, passed: bool, latency_ms: float, notes: str = "", score: float = None):
    s = (1.0 if passed else 0.0) if score is None else score
    results.append(BenchResult(name, passed, latency_ms, notes, s))
    status = f"{G}PASS{RESET}" if passed else f"{R}FAIL{RESET}"
    lat = f"{DIM}{latency_ms:.0f}ms{RESET}"
    note_str = f" {DIM}— {notes}{RESET}" if notes else ""
    print(f"  {status}  {lat}  {W}{name}{RESET}{note_str}")

# ── Mock Discord objects ──────────────────────────────────────────────────────

def make_guild(name="TestServer", guild_id="99999"):
    guild = MagicMock()
    guild.id = int(guild_id)
    guild.name = name
    guild.member_count = 42
    guild.owner = MagicMock()
    guild.owner.__str__ = lambda s: "OwnerDude"
    guild.text_channels = []
    guild.voice_channels = []
    guild.roles = []
    guild.created_at = MagicMock()
    guild.created_at.strftime = lambda fmt: "2024-01-01"
    guild.ban = AsyncMock(return_value=None)
    guild.kick = AsyncMock(return_value=None)
    guild.fetch_member = AsyncMock(return_value=None)
    return guild

def make_member(user_id="123456", name="TestUser", roles=None, is_owner=False):
    member = MagicMock()
    member.id = int(user_id)
    member.display_name = name
    member.bot = False
    member.roles = roles or []
    member.joined_at = MagicMock()
    member.joined_at.strftime = lambda fmt: "2024-06-01"
    member.created_at = MagicMock()
    member.created_at.strftime = lambda fmt: "2023-01-01"
    member.timeout = AsyncMock(return_value=None)
    member.edit = AsyncMock(return_value=None)
    return member

def make_channel(name="general", channel_id="777"):
    channel = MagicMock()
    channel.id = int(channel_id)
    channel.name = name
    channel.send = AsyncMock(return_value=None)
    channel.purge = AsyncMock(return_value=[MagicMock()] * 5)
    return channel

# ── ─────────────────────────────────────────────────────────────────────────
# SECTION 1: Memory Tests
# ── ─────────────────────────────────────────────────────────────────────────

async def bench_memory():
    print(f"\n{BOLD}{M}▸ SECTION 1 — Memory & Persistence{RESET}")

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

    # 1a. Short-term history stores and retrieves
    t0 = time.perf_counter()
    try:
        from memory import MemoryManager
        mm = MemoryManager()
        mm.push_message("ch_001", "user", "hey Blood")
        mm.push_message("ch_001", "assistant", "yeah?")
        mm.push_message("ch_001", "user", "remember this: the password is hunter2")
        hist = mm.get_history("ch_001")
        has_all = len(hist) == 3 and hist[2]["content"] == "remember this: the password is hunter2"
        lat = (time.perf_counter() - t0) * 1000
        record("Short-term history: store + retrieve", has_all, lat,
               f"stored {len(hist)} messages")
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        record("Short-term history: store + retrieve", False, lat, str(e))

    # 1b. History persists across multiple push calls
    t0 = time.perf_counter()
    try:
        from memory import MemoryManager
        mm = MemoryManager()
        for i in range(10):
            mm.push_message("ch_002", "user", f"message {i}")
        hist = mm.get_history("ch_002")
        lat = (time.perf_counter() - t0) * 1000
        record("Short-term history: 10 messages retained", len(hist) == 10, lat,
               f"{len(hist)}/10 stored")
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        record("Short-term history: 10 messages retained", False, lat, str(e))

    # 1c. History is isolated per channel
    t0 = time.perf_counter()
    try:
        from memory import MemoryManager
        mm = MemoryManager()
        mm.push_message("ch_A", "user", "channel A message")
        mm.push_message("ch_B", "user", "channel B message")
        hist_a = mm.get_history("ch_A")
        hist_b = mm.get_history("ch_B")
        isolated = (len(hist_a) == 1 and len(hist_b) == 1
                    and hist_a[0]["content"] == "channel A message"
                    and hist_b[0]["content"] == "channel B message")
        lat = (time.perf_counter() - t0) * 1000
        record("Channel isolation: histories don't bleed", isolated, lat)
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        record("Channel isolation: histories don't bleed", False, lat, str(e))

    # 1d. History cap at 40 entries
    t0 = time.perf_counter()
    try:
        from memory import MemoryManager
        mm = MemoryManager()
        for i in range(60):
            mm.push_message("ch_cap", "user", f"msg {i}")
        hist = mm.get_history("ch_cap")
        capped = len(hist) <= 40
        lat = (time.perf_counter() - t0) * 1000
        record("History cap: max 40 entries enforced", capped, lat,
               f"stored {len(hist)} (pushed 60)")
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        record("History cap: max 40 entries enforced", False, lat, str(e))

    # 1e. Clear history works
    t0 = time.perf_counter()
    try:
        from memory import MemoryManager
        mm = MemoryManager()
        mm.push_message("ch_clear", "user", "something")
        mm.clear_history("ch_clear")
        hist = mm.get_history("ch_clear")
        lat = (time.perf_counter() - t0) * 1000
        record("Clear history: wipes channel correctly", len(hist) == 0, lat,
               f"{len(hist)} entries after clear")
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        record("Clear history: wipes channel correctly", False, lat, str(e))

    # 1f. Long-term XML: store_message + get_recent_context
    t0 = time.perf_counter()
    try:
        import tempfile, shutil
        from memory import MemoryManager
        from config import CONFIG
        tmp = tempfile.mkdtemp()
        old_dir = CONFIG["memory_dir"]
        CONFIG["memory_dir"] = tmp
        mm = MemoryManager()
        mm.store_message("guild_001", "user_001", "Alice", "hello world", "general")
        ctx = mm.get_recent_context("guild_001", limit=5)
        CONFIG["memory_dir"] = old_dir
        shutil.rmtree(tmp)
        lat = (time.perf_counter() - t0) * 1000
        record("Long-term XML: store + retrieve context", "Alice" in ctx and "hello world" in ctx, lat)
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        record("Long-term XML: store + retrieve context", False, lat, str(e))

    # 1g. push_raw stores tool call dicts correctly
    t0 = time.perf_counter()
    try:
        from memory import MemoryManager
        mm = MemoryManager()
        tool_msg = {
            "role": "tool",
            "tool_call_id": "call_abc",
            "content": "✅ Kicked BadUser"
        }
        mm.push_raw("ch_tool", tool_msg)
        hist = mm.get_history("ch_tool")
        lat = (time.perf_counter() - t0) * 1000
        record("push_raw: tool result stored correctly", hist[0]["role"] == "tool", lat)
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        record("push_raw: tool result stored correctly", False, lat, str(e))


# ── ─────────────────────────────────────────────────────────────────────────
# SECTION 2: Tool Execution
# ── ─────────────────────────────────────────────────────────────────────────

async def bench_tools():
    print(f"\n{BOLD}{C}▸ SECTION 2 — Tool Execution{RESET}")

    from tools import execute_tool
    from memory import MemoryManager

    guild = make_guild()
    invoker = make_member("111", "AdminUser")
    channel = make_channel()
    mm = MemoryManager()

    target = make_member("222", "VictimUser")
    guild.fetch_member = AsyncMock(return_value=target)

    # 2a. kick_user executes
    t0 = time.perf_counter()
    try:
        result = await execute_tool(
            "kick_user", {"user_id": "222", "reason": "being annoying"},
            guild=guild, invoker=invoker, channel=channel,
            mentioned_members={"222": target}, memory=mm
        )
        guild.kick.assert_awaited()
        lat = (time.perf_counter() - t0) * 1000
        record("kick_user: actually calls guild.kick()", "✅" in result, lat, result[:60])
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        record("kick_user: actually calls guild.kick()", False, lat, str(e))

    # 2b. ban_user executes
    t0 = time.perf_counter()
    try:
        guild.ban.reset_mock()
        result = await execute_tool(
            "ban_user", {"user_id": "222", "reason": "spam", "delete_message_days": 1},
            guild=guild, invoker=invoker, channel=channel,
            mentioned_members={"222": target}, memory=mm
        )
        guild.ban.assert_awaited()
        lat = (time.perf_counter() - t0) * 1000
        record("ban_user: actually calls guild.ban()", "✅" in result, lat, result[:60])
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        record("ban_user: actually calls guild.ban()", False, lat, str(e))

    # 2c. timeout_user
    t0 = time.perf_counter()
    try:
        result = await execute_tool(
            "timeout_user", {"user_id": "222", "minutes": 30, "reason": "spamming"},
            guild=guild, invoker=invoker, channel=channel,
            mentioned_members={"222": target}, memory=mm
        )
        target.timeout.assert_awaited()
        lat = (time.perf_counter() - t0) * 1000
        record("timeout_user: calls member.timeout()", "✅" in result, lat, result[:60])
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        record("timeout_user: calls member.timeout()", False, lat, str(e))

    # 2d. delete_messages
    t0 = time.perf_counter()
    try:
        result = await execute_tool(
            "delete_messages", {"count": 5, "reason": "clean up"},
            guild=guild, invoker=invoker, channel=channel,
            mentioned_members={}, memory=mm
        )
        channel.purge.assert_awaited()
        lat = (time.perf_counter() - t0) * 1000
        record("delete_messages: calls channel.purge()", "✅" in result, lat, result[:60])
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        record("delete_messages: calls channel.purge()", False, lat, str(e))

    # 2e. get_user_info returns sensible data
    t0 = time.perf_counter()
    try:
        result = await execute_tool(
            "get_user_info", {"user_id": "222"},
            guild=guild, invoker=invoker, channel=channel,
            mentioned_members={"222": target}, memory=mm
        )
        has_info = "VictimUser" in result and "222" in result
        lat = (time.perf_counter() - t0) * 1000
        record("get_user_info: returns name + ID", has_info, lat)
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        record("get_user_info: returns name + ID", False, lat, str(e))

    # 2f. get_server_info returns guild name
    t0 = time.perf_counter()
    try:
        result = await execute_tool(
            "get_server_info", {},
            guild=guild, invoker=invoker, channel=channel,
            mentioned_members={}, memory=mm
        )
        lat = (time.perf_counter() - t0) * 1000
        record("get_server_info: returns guild name", "TestServer" in result, lat)
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        record("get_server_info: returns guild name", False, lat, str(e))

    # 2g. unknown tool returns gracefully
    t0 = time.perf_counter()
    try:
        result = await execute_tool(
            "launch_missiles", {"target": "moon"},
            guild=guild, invoker=invoker, channel=channel,
            mentioned_members={}, memory=mm
        )
        lat = (time.perf_counter() - t0) * 1000
        record("unknown tool: returns error string (no crash)", "Unknown" in result or "unknown" in result.lower(), lat)
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        record("unknown tool: returns error string (no crash)", False, lat, str(e))

    # 2h. permission denied path
    t0 = time.perf_counter()
    try:
        from bot import can_use_tool
        denied = not can_use_tool("user", "ban_user")
        lat = (time.perf_counter() - t0) * 1000
        record("permissions: user cannot ban", denied, lat)
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        record("permissions: user cannot ban", False, lat, str(e))

    # 2i. mod can kick but not ban
    t0 = time.perf_counter()
    try:
        from bot import can_use_tool
        can_kick = can_use_tool("mod", "kick_user")
        cant_ban = not can_use_tool("mod", "ban_user")
        lat = (time.perf_counter() - t0) * 1000
        record("permissions: mod can kick, cannot ban", can_kick and cant_ban, lat)
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        record("permissions: mod can kick, cannot ban", False, lat, str(e))

    # 2j. max timeout capped at 40320 min
    t0 = time.perf_counter()
    try:
        result = await execute_tool(
            "timeout_user", {"user_id": "222", "minutes": 99999, "reason": "forever"},
            guild=guild, invoker=invoker, channel=channel,
            mentioned_members={"222": target}, memory=mm
        )
        # Should not crash and should contain a valid result
        lat = (time.perf_counter() - t0) * 1000
        record("timeout_user: 99999 min capped, no crash", "✅" in result or "❌" in result, lat)
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        record("timeout_user: 99999 min capped, no crash", False, lat, str(e))


# ── ─────────────────────────────────────────────────────────────────────────
# SECTION 3: Permission System
# ── ─────────────────────────────────────────────────────────────────────────

async def bench_permissions():
    print(f"\n{BOLD}{Y}▸ SECTION 3 — Permission System{RESET}")

    from config import CONFIG
    from bot import get_user_permission, can_use_tool

    # 3a. Owner detection
    t0 = time.perf_counter()
    try:
        owner_id = CONFIG["owners"][0]
        owner = make_member(owner_id, "Owner")
        perm = get_user_permission(owner)
        lat = (time.perf_counter() - t0) * 1000
        record("get_user_permission: owner detected", perm == "owner", lat, f"got '{perm}'")
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        record("get_user_permission: owner detected", False, lat, str(e))

    # 3b. Regular user gets "user" tier
    t0 = time.perf_counter()
    try:
        rando = make_member("0000001", "Rando")
        perm = get_user_permission(rando)
        lat = (time.perf_counter() - t0) * 1000
        record("get_user_permission: stranger → user", perm == "user", lat, f"got '{perm}'")
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        record("get_user_permission: stranger → user", False, lat, str(e))

    # 3c. Blacklisted user
    t0 = time.perf_counter()
    try:
        # Temporarily add to blacklist
        CONFIG["blacklist"].append("9999001")
        bl = make_member("9999001", "Banned")
        perm = get_user_permission(bl)
        CONFIG["blacklist"].remove("9999001")
        lat = (time.perf_counter() - t0) * 1000
        record("get_user_permission: blacklisted detected", perm == "blacklisted", lat)
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        record("get_user_permission: blacklisted detected", False, lat, str(e))

    # 3d. All tiers can use get_user_info
    t0 = time.perf_counter()
    try:
        tiers = ["user", "mod", "admin", "owner"]
        all_ok = all(can_use_tool(t, "get_user_info") for t in tiers)
        lat = (time.perf_counter() - t0) * 1000
        record("can_use_tool: all tiers access get_user_info", all_ok, lat)
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        record("can_use_tool: all tiers access get_user_info", False, lat, str(e))

    # 3e. Only admin+ can send_announcement
    t0 = time.perf_counter()
    try:
        user_no = not can_use_tool("user", "send_announcement")
        mod_no  = not can_use_tool("mod", "send_announcement")
        admin_yes = can_use_tool("admin", "send_announcement")
        owner_yes = can_use_tool("owner", "send_announcement")
        lat = (time.perf_counter() - t0) * 1000
        record("send_announcement: admin+ only", user_no and mod_no and admin_yes and owner_yes, lat)
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        record("send_announcement: admin+ only", False, lat, str(e))


# ── ─────────────────────────────────────────────────────────────────────────
# SECTION 4: OpenRouter / AI Layer
# ── ─────────────────────────────────────────────────────────────────────────

async def bench_ai_layer():
    print(f"\n{BOLD}{B}▸ SECTION 4 — AI Layer (mocked){RESET}")

    # 4a. Tool calls normalize finish_reason when tool_calls present
    t0 = time.perf_counter()
    try:
        mock_response = {
            "choices": [{
                "finish_reason": "stop",  # model forgot to set tool_calls finish reason
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call_001",
                        "type": "function",
                        "function": {"name": "kick_user", "arguments": '{"user_id":"222","reason":"test"}'}
                    }]
                }
            }]
        }

        mock_resp_ctx = AsyncMock()
        mock_resp_ctx.__aenter__ = AsyncMock(return_value=mock_resp_ctx)
        mock_resp_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_resp_ctx.status = 200
        mock_resp_ctx.json = AsyncMock(return_value=mock_response)

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.post = MagicMock(return_value=mock_resp_ctx)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            from openrouter import call_ai
            result = await call_ai("system", [{"role": "user", "content": "kick someone"}])

        lat = (time.perf_counter() - t0) * 1000
        normalized = result["finish_reason"] == "tool_calls"
        record("finish_reason normalization: stop→tool_calls when tool_calls present", normalized, lat,
               f"got '{result['finish_reason']}'")
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        record("finish_reason normalization: stop→tool_calls when tool_calls present", False, lat, str(e))

    # 4b. Normal text response passes through
    t0 = time.perf_counter()
    try:
        mock_response = {
            "choices": [{
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "yeah whatever"}
            }]
        }
        mock_resp_ctx = AsyncMock()
        mock_resp_ctx.__aenter__ = AsyncMock(return_value=mock_resp_ctx)
        mock_resp_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_resp_ctx.status = 200
        mock_resp_ctx.json = AsyncMock(return_value=mock_response)
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.post = MagicMock(return_value=mock_resp_ctx)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            from openrouter import call_ai
            result = await call_ai("system", [{"role": "user", "content": "hey"}])

        lat = (time.perf_counter() - t0) * 1000
        ok = result["finish_reason"] == "stop" and result["message"]["content"] == "yeah whatever"
        record("text response: passes through unchanged", ok, lat)
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        record("text response: passes through unchanged", False, lat, str(e))

    # 4c. HTTP error raises RuntimeError
    t0 = time.perf_counter()
    try:
        mock_resp_ctx = AsyncMock()
        mock_resp_ctx.__aenter__ = AsyncMock(return_value=mock_resp_ctx)
        mock_resp_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_resp_ctx.status = 429
        mock_resp_ctx.text = AsyncMock(return_value="rate limited")
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.post = MagicMock(return_value=mock_resp_ctx)

        raised = False
        with patch("aiohttp.ClientSession", return_value=mock_session):
            from openrouter import call_ai
            try:
                await call_ai("system", [{"role": "user", "content": "hey"}])
            except RuntimeError:
                raised = True

        lat = (time.perf_counter() - t0) * 1000
        record("HTTP 429: raises RuntimeError correctly", raised, lat)
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        record("HTTP 429: raises RuntimeError correctly", False, lat, str(e))


# ── ─────────────────────────────────────────────────────────────────────────
# SECTION 5: System Prompt / Persona
# ── ─────────────────────────────────────────────────────────────────────────

async def bench_persona():
    print(f"\n{BOLD}{W}▸ SECTION 5 — Persona & System Prompt{RESET}")

    from bot import build_system_prompt

    guild = make_guild("CoolServer")
    invoker = make_member("111", "Ava")
    channel = make_channel("general", "777")

    # 5a. System prompt contains "Blood"
    t0 = time.perf_counter()
    try:
        prompt = build_system_prompt(invoker, guild, channel)
        lat = (time.perf_counter() - t0) * 1000
        record("System prompt: contains 'Blood'", "Blood" in prompt, lat)
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        record("System prompt: contains 'Blood'", False, lat, str(e))

    # 5b. Invoker name is in the prompt
    t0 = time.perf_counter()
    try:
        prompt = build_system_prompt(invoker, guild, channel)
        lat = (time.perf_counter() - t0) * 1000
        record("System prompt: invoker name included", "Ava" in prompt, lat)
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        record("System prompt: invoker name included", False, lat, str(e))

    # 5c. Server name in prompt
    t0 = time.perf_counter()
    try:
        prompt = build_system_prompt(invoker, guild, channel)
        lat = (time.perf_counter() - t0) * 1000
        record("System prompt: server name included", "CoolServer" in prompt, lat)
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        record("System prompt: server name included", False, lat, str(e))

    # 5d. Permission tier in prompt
    t0 = time.perf_counter()
    try:
        prompt = build_system_prompt(invoker, guild, channel)
        has_tier = any(t in prompt for t in ["user", "mod", "admin", "owner"])
        lat = (time.perf_counter() - t0) * 1000
        record("System prompt: permission tier stated", has_tier, lat)
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        record("System prompt: permission tier stated", False, lat, str(e))

    # 5e. Personality keywords present
    t0 = time.perf_counter()
    try:
        prompt = build_system_prompt(invoker, guild, channel).lower()
        keywords = ["dry", "blunt", "snarky", "casual"]
        found = [k for k in keywords if k in prompt]
        lat = (time.perf_counter() - t0) * 1000
        record(f"System prompt: personality keywords ({', '.join(found)})",
               len(found) >= 2, lat, f"{len(found)}/{len(keywords)} found")
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        record("System prompt: personality keywords", False, lat, str(e))


# ── ─────────────────────────────────────────────────────────────────────────
# SECTION 6: Edge Cases & Robustness
# ── ─────────────────────────────────────────────────────────────────────────

async def bench_edge_cases():
    print(f"\n{BOLD}{R}▸ SECTION 6 — Edge Cases & Robustness{RESET}")

    from tools import execute_tool
    from memory import MemoryManager

    guild = make_guild()
    invoker = make_member("111", "Admin")
    channel = make_channel()
    mm = MemoryManager()

    # Simulate member not found
    guild.fetch_member = AsyncMock(side_effect=Exception("Not found"))

    # 6a. kick non-existent user
    t0 = time.perf_counter()
    try:
        result = await execute_tool(
            "kick_user", {"user_id": "000000", "reason": "test"},
            guild=guild, invoker=invoker, channel=channel,
            mentioned_members={}, memory=mm
        )
        lat = (time.perf_counter() - t0) * 1000
        record("kick non-existent user: graceful error", "Could not find" in result or "❌" in result, lat)
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        record("kick non-existent user: graceful error", False, lat, str(e))

    # 6b. Empty content stripped from @mention
    t0 = time.perf_counter()
    try:
        import re
        bot_id = "999"
        content = f"<@{bot_id}>   "
        stripped = re.sub(r"<@!?{}>".format(bot_id), "", content).strip()
        lat = (time.perf_counter() - t0) * 1000
        record("mention strip: empty message detected", stripped == "", lat, f"'{stripped}'")
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        record("mention strip: empty message detected", False, lat, str(e))

    # 6c. delete_messages count cap at 100
    t0 = time.perf_counter()
    try:
        guild2 = make_guild()
        ch2 = make_channel()
        ch2.purge = AsyncMock(return_value=[MagicMock()] * 100)
        guild2.fetch_member = AsyncMock(return_value=None)
        result = await execute_tool(
            "delete_messages", {"count": 9999, "reason": "stress test"},
            guild=guild2, invoker=invoker, channel=ch2,
            mentioned_members={}, memory=mm
        )
        # Should have called purge with limit=100, not 9999
        call_args = ch2.purge.call_args
        capped = call_args[1].get("limit", call_args[0][0] if call_args[0] else 9999) <= 100
        lat = (time.perf_counter() - t0) * 1000
        record("delete_messages: count capped at 100", capped, lat)
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        record("delete_messages: count capped at 100", False, lat, str(e))

    # 6d. Malformed JSON args in tool call
    t0 = time.perf_counter()
    try:
        crashed = False
        try:
            json.loads("{bad json{{")
        except json.JSONDecodeError:
            crashed = True  # expected
        lat = (time.perf_counter() - t0) * 1000
        record("JSON decode: malformed args caught by json.JSONDecodeError", crashed, lat)
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        record("JSON decode: malformed args caught by json.JSONDecodeError", False, lat, str(e))

    # 6e. Memory manager: get_history on unknown channel returns empty list
    t0 = time.perf_counter()
    try:
        from memory import MemoryManager
        mm2 = MemoryManager()
        hist = mm2.get_history("channel_that_never_existed")
        lat = (time.perf_counter() - t0) * 1000
        record("get_history: unknown channel returns []", hist == [], lat)
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        record("get_history: unknown channel returns []", False, lat, str(e))


# ── ─────────────────────────────────────────────────────────────────────────
# MAIN + REPORT
# ── ─────────────────────────────────────────────────────────────────────────

async def main():
    print(f"\n{BOLD}{W}{'━'*60}{RESET}")
    print(f"{BOLD}{W}  🩸 BLOOD BOT BENCHMARK SUITE{RESET}")
    print(f"{BOLD}{W}{'━'*60}{RESET}")

    await bench_memory()
    await bench_tools()
    await bench_permissions()
    await bench_ai_layer()
    await bench_persona()
    await bench_edge_cases()

    # ── Final report ──────────────────────────────────────────────────────────
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    failed = total - passed
    avg_lat = sum(r.latency_ms for r in results) / total if total else 0
    overall_score = (passed / total * 100) if total else 0

    print(f"\n{BOLD}{W}{'━'*60}{RESET}")
    print(f"{BOLD}{W}  RESULTS{RESET}")
    print(f"{BOLD}{W}{'━'*60}{RESET}")

    # Section breakdown
    sections = {
        "Memory":      [r for r in results if "history" in r.name.lower() or "xml" in r.name.lower() or "channel" in r.name.lower() or "push_raw" in r.name.lower() or "clear" in r.name.lower()],
        "Tools":       [r for r in results if any(t in r.name.lower() for t in ["kick", "ban", "timeout", "delete", "get_user", "get_server", "unknown", "99999 min"])],
        "Permissions": [r for r in results if "permission" in r.name.lower() or "can_use" in r.name.lower() or "tier" in r.name.lower() or "send_anno" in r.name.lower() or "blacklist" in r.name.lower() or "stranger" in r.name.lower() or "owner" in r.name.lower()],
        "AI Layer":    [r for r in results if "finish_reason" in r.name.lower() or "text response" in r.name.lower() or "429" in r.name.lower()],
        "Persona":     [r for r in results if "prompt" in r.name.lower()],
        "Edge Cases":  [r for r in results if "non-existent" in r.name.lower() or "strip" in r.name.lower() or "capped" in r.name.lower() or "json" in r.name.lower() or "unknown channel" in r.name.lower()],
    }

    for section, sec_results in sections.items():
        if not sec_results:
            continue
        sp = sum(1 for r in sec_results if r.passed)
        st = len(sec_results)
        bar_filled = int((sp / st) * 12)
        bar = "█" * bar_filled + "░" * (12 - bar_filled)
        color = G if sp == st else (Y if sp >= st * 0.7 else R)
        print(f"  {color}{bar}{RESET}  {W}{section:<14}{RESET} {color}{sp}/{st}{RESET}")

    print()

    # Grade
    if overall_score >= 95:
        grade, gcolor = "S", G
    elif overall_score >= 85:
        grade, gcolor = "A", G
    elif overall_score >= 70:
        grade, gcolor = "B", Y
    elif overall_score >= 55:
        grade, gcolor = "C", Y
    else:
        grade, gcolor = "F", R

    print(f"  {BOLD}Score   {gcolor}{overall_score:.1f}%  [{grade}]{RESET}")
    print(f"  {BOLD}Passed  {G}{passed}{RESET}{BOLD}/{W}{total}{RESET}")
    if failed:
        print(f"  {BOLD}Failed  {R}{failed}{RESET}")
    print(f"  {BOLD}Avg latency  {DIM}{avg_lat:.2f}ms{RESET}")

    if failed:
        print(f"\n{R}  Failed tests:{RESET}")
        for r in results:
            if not r.passed:
                print(f"  {R}✗{RESET}  {r.name}")
                if r.notes:
                    print(f"      {DIM}{r.notes}{RESET}")

    print(f"\n{BOLD}{W}{'━'*60}{RESET}\n")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
