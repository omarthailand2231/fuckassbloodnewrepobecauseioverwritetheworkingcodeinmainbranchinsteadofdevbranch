"""Full server backup and restore for Blood bot."""

import os
import json
import uuid
import time
import base64
import asyncio
import io
import logging
from datetime import datetime, timezone
from typing import Optional

import aiohttp
import discord

log = logging.getLogger("blood.backup")

BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backups")

_locks: dict[str, asyncio.Lock] = {}


def get_lock(guild_id: str) -> asyncio.Lock:
    if guild_id not in _locks:
        _locks[guild_id] = asyncio.Lock()
    return _locks[guild_id]


def _ensure_dir():
    os.makedirs(BACKUP_DIR, exist_ok=True)


async def _download(url: str) -> Optional[bytes]:
    if not url:
        return None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(str(url)) as resp:
                if resp.status == 200:
                    return await resp.read()
    except Exception as e:
        log.warning("Download failed %s: %s", url, e)
    return None


def _b64(data: Optional[bytes]) -> Optional[str]:
    return base64.b64encode(data).decode() if data else None


def _unb64(s: Optional[str]) -> Optional[bytes]:
    return base64.b64decode(s) if s else None


def _ch_type(ch) -> str:
    if isinstance(ch, discord.ForumChannel):
        return "forum"
    if isinstance(ch, discord.StageChannel):
        return "stage"
    if isinstance(ch, discord.VoiceChannel):
        return "voice"
    if isinstance(ch, discord.TextChannel):
        return "news" if ch.is_news() else "text"
    return "text"


def _overwrites_to_list(obj) -> list[dict]:
    result = []
    for target, ow in obj.overwrites.items():
        allow, deny = ow.pair()
        result.append({
            "id": str(target.id),
            "type": "role" if isinstance(target, discord.Role) else "member",
            "allow": allow.value,
            "deny": deny.value,
        })
    return result


def _build_overwrites(data: list[dict], role_map: dict,
                      guild: discord.Guild, old_guild_id: str) -> dict:
    overwrites = {}
    for ow in data:
        old_id = ow["id"]
        perm_ow = discord.PermissionOverwrite.from_pair(
            discord.Permissions(ow.get("allow", 0)),
            discord.Permissions(ow.get("deny", 0)),
        )
        if ow["type"] == "role":
            if old_id in role_map:
                overwrites[role_map[old_id]] = perm_ow
            elif old_id == old_guild_id:
                overwrites[guild.default_role] = perm_ow
        else:
            try:
                member = guild.get_member(int(old_id))
                if member:
                    overwrites[member] = perm_ow
            except (ValueError, TypeError):
                pass
    return overwrites


# ── Backup ───────────────────────────────────────────────────────────────────

async def create_backup(guild: discord.Guild, requester_id: str,
                        progress=None) -> dict:
    backup_id = str(uuid.uuid4())

    async def _p(msg):
        if progress:
            await progress(msg)

    data = {
        "backup_id": backup_id,
        "version": 1,
        "guild_id": str(guild.id),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "created_by": requester_id,
    }

    # ── Server settings ──
    await _p("Saving server settings...")
    icon_data, banner_data, splash_data = await asyncio.gather(
        _download(guild.icon.url if guild.icon else None),
        _download(guild.banner.url if guild.banner else None),
        _download(guild.splash.url if guild.splash else None),
    )

    data["server"] = {
        "name": guild.name,
        "description": guild.description,
        "icon_b64": _b64(icon_data),
        "banner_b64": _b64(banner_data),
        "splash_b64": _b64(splash_data),
        "verification_level": guild.verification_level.value,
        "default_notifications": guild.default_notifications.value,
        "explicit_content_filter": guild.explicit_content_filter.value,
        "afk_timeout": guild.afk_timeout,
        "afk_channel_id": str(guild.afk_channel.id) if guild.afk_channel else None,
        "system_channel_id": str(guild.system_channel.id) if guild.system_channel else None,
        "system_channel_flags": guild.system_channel_flags.value,
        "rules_channel_id": str(guild.rules_channel.id) if guild.rules_channel else None,
        "public_updates_channel_id": (
            str(guild.public_updates_channel.id) if guild.public_updates_channel else None
        ),
        "preferred_locale": str(guild.preferred_locale),
    }

    # ── Roles ──
    await _p(f"Backing up {len(guild.roles)} roles...")
    data["roles"] = []
    for role in sorted(guild.roles, key=lambda r: r.position):
        r_icon = await _download(role.icon.url if role.icon else None)
        data["roles"].append({
            "id": str(role.id),
            "name": role.name,
            "color": role.color.value,
            "hoist": role.hoist,
            "position": role.position,
            "permissions": role.permissions.value,
            "mentionable": role.mentionable,
            "is_default": role.is_default(),
            "managed": role.managed,
            "icon_b64": _b64(r_icon),
            "unicode_emoji": role.unicode_emoji,
        })

    # ── Emojis ──
    await _p(f"Backing up {len(guild.emojis)} emojis...")
    data["emojis"] = []
    for emoji in guild.emojis:
        img = await _download(str(emoji.url))
        data["emojis"].append({
            "name": emoji.name,
            "animated": emoji.animated,
            "image_b64": _b64(img),
        })

    # ── Stickers ──
    await _p(f"Backing up {len(guild.stickers)} stickers...")
    data["stickers"] = []
    for sticker in guild.stickers:
        img = await _download(str(sticker.url))
        data["stickers"].append({
            "name": sticker.name,
            "description": sticker.description or "",
            "emoji": sticker.emoji or "\U0001f516",
            "image_b64": _b64(img),
        })

    # ── Categories ──
    categories = [c for c in guild.channels if isinstance(c, discord.CategoryChannel)]
    non_categories = [c for c in guild.channels if not isinstance(c, discord.CategoryChannel)]

    data["categories"] = []
    for cat in sorted(categories, key=lambda c: c.position):
        data["categories"].append({
            "id": str(cat.id),
            "name": cat.name,
            "position": cat.position,
            "nsfw": cat.nsfw,
            "overwrites": _overwrites_to_list(cat),
        })

    # ── Channels + messages ──
    data["channels"] = []
    total = len(non_categories)
    for idx, ch in enumerate(sorted(non_categories, key=lambda c: c.position)):
        await _p(f"Backing up channel {idx + 1}/{total}: #{ch.name}")

        ch_data = {
            "id": str(ch.id),
            "name": ch.name,
            "type": _ch_type(ch),
            "category_id": str(ch.category_id) if ch.category_id else None,
            "position": ch.position,
            "overwrites": _overwrites_to_list(ch),
            "messages": [],
        }

        if isinstance(ch, (discord.TextChannel, discord.ForumChannel)):
            ch_data["topic"] = ch.topic
            ch_data["nsfw"] = ch.nsfw
            ch_data["slowmode_delay"] = ch.slowmode_delay

        if isinstance(ch, (discord.VoiceChannel, discord.StageChannel)):
            ch_data["bitrate"] = ch.bitrate
            ch_data["user_limit"] = ch.user_limit

        if isinstance(ch, discord.StageChannel):
            ch_data["topic"] = getattr(ch, "topic", None)

        if isinstance(ch, discord.TextChannel):
            try:
                count = 0
                async for msg in ch.history(limit=50):
                    ch_data["messages"].append({
                        "author_id": str(msg.author.id),
                        "author_name": msg.author.display_name,
                        "author_bot": msg.author.bot,
                        "author_avatar": (
                            str(msg.author.display_avatar.url)
                            if msg.author.display_avatar else None
                        ),
                        "content": msg.content,
                        "timestamp": msg.created_at.isoformat(),
                        "pinned": msg.pinned,
                        "attachments": [
                            {"filename": a.filename, "url": str(a.url), "size": a.size}
                            for a in msg.attachments
                        ],
                        "embeds": [e.to_dict() for e in msg.embeds],
                    })
                    count += 1
                ch_data["messages"].reverse()
                if count > 0:
                    await _p(f"  -> {count} messages from #{ch.name}")
            except discord.Forbidden:
                await _p(f"  ! no read permission for #{ch.name}")
            except Exception as e:
                log.warning("Error reading #%s: %s", ch.name, e)

        data["channels"].append(ch_data)

    # ── Members ──
    member_count = sum(1 for m in guild.members if not m.bot)
    await _p(f"Saving {member_count} member role assignments...")
    data["members"] = []
    for member in guild.members:
        role_ids = [str(r.id) for r in member.roles if not r.is_default()]
        if not role_ids and not member.nick:
            continue
        data["members"].append({
            "id": str(member.id),
            "name": member.name,
            "display_name": member.display_name,
            "nick": member.nick,
            "bot": member.bot,
            "role_ids": role_ids,
        })

    return data


# ── Save / Load / List ───────────────────────────────────────────────────────

def save_backup(data: dict) -> str:
    _ensure_dir()
    bid = data["backup_id"]
    path = os.path.join(BACKUP_DIR, f"{bid}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    size_mb = os.path.getsize(path) / (1024 * 1024)
    log.info("Backup saved: %s (%.1f MB)", bid, size_mb)
    return bid


def load_backup_file(backup_id: str) -> Optional[dict]:
    _ensure_dir()
    path = os.path.join(BACKUP_DIR, f"{backup_id}.json")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def list_backups(guild_id: str = None) -> list[dict]:
    _ensure_dir()
    results = []
    for fname in os.listdir(BACKUP_DIR):
        if not fname.endswith(".json"):
            continue
        try:
            path = os.path.join(BACKUP_DIR, fname)
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
            if guild_id and d.get("guild_id") != guild_id:
                continue
            total_msgs = sum(len(c.get("messages", [])) for c in d.get("channels", []))
            results.append({
                "backup_id": d["backup_id"],
                "guild_name": d.get("server", {}).get("name", "?"),
                "created_at": d.get("created_at", "?"),
                "created_by": d.get("created_by", "?"),
                "roles": len([r for r in d.get("roles", [])
                              if not r.get("is_default") and not r.get("managed")]),
                "channels": len(d.get("channels", [])) + len(d.get("categories", [])),
                "messages": total_msgs,
                "members": len(d.get("members", [])),
                "emojis": len(d.get("emojis", [])),
            })
        except Exception:
            continue
    return sorted(results, key=lambda x: x.get("created_at", ""), reverse=True)


# ── Progress helpers ──────────────────────────────────────────────────────────

_PULSE = ["·", "✢", "✳", "✶", "✻", "✽", "✻", "✶", "✳", "✢"]


def _bar(pct, width=20):
    filled = round(pct / 100 * width)
    return "▰" * filled + "▱" * (width - filled)


def _progress_color(pct):
    if pct < 50:
        r, g = 255, int(pct / 50 * 255)
    else:
        r, g = int((100 - pct) / 50 * 255), 255
    return (r << 16) | (g << 8)


def _format_eta(seconds):
    if seconds < 0 or seconds > 36000:
        return "calculating…"
    s = int(seconds)
    if s < 60:
        return f"~{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"~{m}m {s}s"
    h, m = divmod(m, 60)
    return f"~{h}h {m}m"


def _make_progress_embed(phase, overall_pct, ch_progress=None,
                         frame=0, eta_str=None):
    done = overall_pct >= 100
    if done:
        title = "✅ Restore Complete!"
    else:
        star = _PULSE[frame % len(_PULSE)]
        title = f"{star} Restoring Server…"
    embed = discord.Embed(
        title=title,
        color=_progress_color(min(overall_pct, 100)),
    )
    embed.add_field(name="Phase", value=phase, inline=False)
    if ch_progress:
        lines = []
        for name in sorted(ch_progress, key=lambda n: -ch_progress[n][0] / max(ch_progress[n][1], 1)):
            sent, total = ch_progress[name]
            pct = (sent / total * 100) if total > 0 else 100
            mark = " ✅" if sent >= total else ""
            lines.append(f"`{_bar(pct, 10)}` {pct:3.0f}% #{name}{mark}")
        if lines:
            embed.add_field(name="Channels", value="\n".join(lines[:20]), inline=False)
    overall_line = f"`{_bar(overall_pct)}` **{overall_pct:.0f}%**"
    if eta_str and not done:
        overall_line += f"  ⏱ {eta_str}"
    embed.add_field(name="Overall", value=overall_line, inline=False)
    return embed


# ── Restore ──────────────────────────────────────────────────────────────────

async def restore_backup(guild: discord.Guild, data: dict,
                         progress=None, *,
                         settings: bool = True, roles: bool = True,
                         channels: bool = True, messages: bool = True,
                         emojis: bool = True, members: bool = True,
                         ) -> bool:
    role_map: dict[str, discord.Role] = {}
    category_map: dict[str, discord.CategoryChannel] = {}
    channel_map: dict[str, discord.abc.GuildChannel] = {}
    old_guild_id = data.get("guild_id", "")
    bot_top = guild.me.top_role
    server = data.get("server", {})

    # ── Progress channel (locked, top of server) ──
    progress_ch = None
    progress_msg = None
    try:
        progress_ch = await guild.create_text_channel(
            name="⏳│backup-restore",
            overwrites={
                guild.default_role: discord.PermissionOverwrite(
                    send_messages=False, view_channel=True),
                guild.me: discord.PermissionOverwrite(
                    send_messages=True, view_channel=True, manage_channels=True),
            },
            position=0,
            reason="Backup restore — progress",
        )
        progress_msg = await progress_ch.send(
            embed=_make_progress_embed("Starting…", 0))
    except Exception as e:
        log.warning("Can't create progress channel: %s", e)

    # ── Progress state ──
    _start_time = time.time()
    _state = {"phase": "", "pct": 0, "ch": {}, "frame": 0, "done": False}

    def _eta():
        elapsed = time.time() - _start_time
        pct = _state["pct"]
        if pct < 3 or elapsed < 2:
            return "calculating…"
        rate = pct / elapsed
        remaining = (100 - pct) / rate
        return _format_eta(remaining)

    async def _p(phase=None, pct=None):
        if phase:
            _state["phase"] = phase
        if pct is not None:
            _state["pct"] = pct
        if progress and phase:
            try:
                await progress(phase)
            except Exception:
                pass

    async def _pulse_loop():
        while not _state["done"]:
            _state["frame"] += 1
            if progress_msg:
                try:
                    await progress_msg.edit(embed=_make_progress_embed(
                        _state["phase"], _state["pct"], _state["ch"] or None,
                        frame=_state["frame"], eta_str=_eta()))
                except Exception:
                    pass
            await asyncio.sleep(1.1)

    async def _force_update():
        _state["frame"] += 1
        if progress_msg:
            try:
                await progress_msg.edit(embed=_make_progress_embed(
                    _state["phase"], _state["pct"], _state["ch"] or None,
                    frame=_state["frame"], eta_str=_eta()))
            except Exception:
                pass

    pulse_task = asyncio.create_task(_pulse_loop())

    try:
        # 1 — Server settings (0-5%)
        if settings:
            await _p("⚙️ Restoring server settings…", 0)
            edit_kw: dict = {"name": server.get("name", guild.name)}
            if server.get("description") is not None:
                edit_kw["description"] = server["description"]
            try:
                edit_kw["verification_level"] = discord.VerificationLevel(
                    server.get("verification_level", 0))
            except Exception:
                pass
            try:
                edit_kw["default_notifications"] = discord.NotificationLevel(
                    server.get("default_notifications", 0))
            except Exception:
                pass
            try:
                edit_kw["explicit_content_filter"] = discord.ContentFilter(
                    server.get("explicit_content_filter", 0))
            except Exception:
                pass
            if server.get("afk_timeout"):
                edit_kw["afk_timeout"] = server["afk_timeout"]
            icon_bytes = _unb64(server.get("icon_b64"))
            if icon_bytes:
                edit_kw["icon"] = icon_bytes
            banner_bytes = _unb64(server.get("banner_b64"))
            if banner_bytes and guild.premium_tier >= 2:
                edit_kw["banner"] = banner_bytes
            splash_bytes = _unb64(server.get("splash_b64"))
            if splash_bytes and guild.premium_tier >= 1:
                edit_kw["splash"] = splash_bytes
            try:
                await guild.edit(**edit_kw)
            except Exception as e:
                log.warning("Server edit partial fail: %s", e)
            await asyncio.sleep(1)
            await _p("✅ Server settings", 5)
        else:
            await _p("⏭️ Skipped server settings", 5)

        # 2-3 — Delete existing emojis + stickers (5-10%)
        if emojis:
            await _p("\U0001f5d1️ Removing emojis & stickers…", 5)
            for emoji in list(guild.emojis):
                try:
                    await emoji.delete(reason="Backup restore")
                    await asyncio.sleep(0.3)
                except Exception:
                    pass
            for sticker in list(guild.stickers):
                try:
                    await sticker.delete(reason="Backup restore")
                    await asyncio.sleep(0.3)
                except Exception:
                    pass
            await _p("✅ Emojis & stickers cleared", 10)
        else:
            await _p("⏭️ Skipped emojis & stickers", 10)

        # 4 — Delete existing roles (10-18%)
        if roles:
            await _p("\U0001f5d1️ Removing existing roles…", 10)
            for role in sorted(guild.roles, key=lambda r: r.position, reverse=True):
                if role.is_default() or role.managed or role >= bot_top:
                    continue
                try:
                    await role.delete(reason="Backup restore")
                    await asyncio.sleep(1.2)
                except Exception as e:
                    log.warning("Can't delete role %s: %s", role.name, e)
            await _p("✅ Roles cleared", 18)
        else:
            await _p("⏭️ Skipped roles", 18)

        # 5 — Delete existing channels (skip progress channel) (18-25%)
        if channels:
            await _p("\U0001f5d1️ Removing existing channels…", 18)
            for ch in list(guild.channels):
                if progress_ch and ch.id == progress_ch.id:
                    continue
                try:
                    await ch.delete(reason="Backup restore")
                    await asyncio.sleep(0.3)
                except Exception as e:
                    log.warning("Can't delete channel %s: %s", getattr(ch, "name", "?"), e)
            await _p("✅ Channels cleared", 25)
        else:
            await _p("⏭️ Skipped channels", 25)

        # 6 — Create roles (25-35%)
        if roles:
            backup_roles = [r for r in data.get("roles", [])
                            if not r.get("is_default") and not r.get("managed")]
            await _p(f"\U0001f527 Creating {len(backup_roles)} roles…", 25)
            for rd in sorted(backup_roles, key=lambda r: r["position"]):
                try:
                    kw = {
                        "name": rd["name"],
                        "color": discord.Color(rd.get("color", 0)),
                        "hoist": rd.get("hoist", False),
                        "mentionable": rd.get("mentionable", False),
                        "permissions": discord.Permissions(rd.get("permissions", 0)),
                        "reason": "Backup restore",
                    }
                    icon_b = _unb64(rd.get("icon_b64"))
                    if icon_b and guild.premium_tier >= 2:
                        kw["display_icon"] = icon_b
                    elif rd.get("unicode_emoji"):
                        kw["display_icon"] = rd["unicode_emoji"]
                    new_role = await guild.create_role(**kw)
                    role_map[rd["id"]] = new_role
                    await asyncio.sleep(1.2)
                except Exception as e:
                    log.warning("Can't create role %s: %s", rd["name"], e)

            everyone = next((r for r in data.get("roles", []) if r.get("is_default")), None)
            if everyone:
                try:
                    await guild.default_role.edit(
                        permissions=discord.Permissions(everyone.get("permissions", 0)),
                        reason="Backup restore",
                    )
                except Exception as e:
                    log.warning("Can't edit @everyone: %s", e)

            # 6b — Role hierarchy (highest first)
            sorted_roles = sorted(
                [(rd, role_map[rd["id"]]) for rd in backup_roles if rd["id"] in role_map],
                key=lambda x: x[0]["position"], reverse=True,
            )
            if sorted_roles:
                await _p(f"\U0001f4cf Setting role hierarchy…", 30)
                for rd, new_role in sorted_roles:
                    target_pos = min(rd["position"], bot_top.position - 1)
                    if target_pos < 1:
                        target_pos = 1
                    try:
                        await new_role.edit(position=target_pos, reason="Backup restore")
                        await asyncio.sleep(1.2)
                    except Exception as e:
                        log.warning("Can't set position for %s: %s", new_role.name, e)
            await _p("✅ Roles created", 35)
        else:
            await _p("⏭️ Skipped roles", 35)

        # 7 — Categories (35-38%)
        cats = data.get("categories", [])
        if channels:
            await _p(f"\U0001f4c1 Creating {len(cats)} categories…", 35)
            for cd in sorted(cats, key=lambda c: c["position"]):
                ow = _build_overwrites(cd.get("overwrites", []), role_map, guild, old_guild_id)
                try:
                    new_cat = await guild.create_category(
                        name=cd["name"], overwrites=ow, reason="Backup restore",
                    )
                    category_map[cd["id"]] = new_cat
                    await asyncio.sleep(0.3)
                except Exception as e:
                    log.warning("Can't create category %s: %s", cd["name"], e)
            await _p("✅ Categories created", 38)
        else:
            await _p("⏭️ Skipped categories", 38)

        # 8 — Channels (38-48%)
        ch_data = data.get("channels", [])
        if channels:
            await _p(f"\U0001f4fa Creating {len(ch_data)} channels…", 38)
            for cd in sorted(ch_data, key=lambda c: c["position"]):
                cat = category_map.get(cd.get("category_id"))
                ow = _build_overwrites(cd.get("overwrites", []), role_map, guild, old_guild_id)
                ct = cd.get("type", "text")
                try:
                    if ct in ("text", "news"):
                        new_ch = await guild.create_text_channel(
                            name=cd["name"], category=cat,
                            topic=cd.get("topic"), nsfw=cd.get("nsfw", False),
                            slowmode_delay=cd.get("slowmode_delay", 0),
                            overwrites=ow, reason="Backup restore",
                        )
                        if ct == "news":
                            try:
                                await new_ch.edit(type=discord.ChannelType.news)
                            except Exception:
                                pass
                    elif ct == "voice":
                        new_ch = await guild.create_voice_channel(
                            name=cd["name"], category=cat,
                            bitrate=min(cd.get("bitrate", 64000), guild.bitrate_limit),
                            user_limit=cd.get("user_limit", 0),
                            overwrites=ow, reason="Backup restore",
                        )
                    elif ct == "stage":
                        new_ch = await guild.create_stage_channel(
                            name=cd["name"], category=cat,
                            topic=cd.get("topic", ""),
                            overwrites=ow, reason="Backup restore",
                        )
                    elif ct == "forum":
                        new_ch = await guild.create_forum(
                            name=cd["name"], category=cat,
                            topic=cd.get("topic"),
                            slowmode_delay=cd.get("slowmode_delay", 0),
                            overwrites=ow, reason="Backup restore",
                        )
                    else:
                        new_ch = await guild.create_text_channel(
                            name=cd["name"], category=cat,
                            overwrites=ow, reason="Backup restore",
                        )
                    channel_map[cd["id"]] = new_ch
                    await asyncio.sleep(0.3)
                except Exception as e:
                    log.warning("Can't create channel %s: %s", cd["name"], e)
            await _p("✅ Channels created", 48)
        else:
            await _p("⏭️ Skipped channels", 48)

        # 9 — Messages via parallel webhooks (48-88%)
        msg_channels = [c for c in ch_data if c.get("messages")] if (channels and messages) else []
        if msg_channels:
            total_msgs = sum(len(c["messages"]) for c in msg_channels)
            n_ch = len(msg_channels) or 1
            _msg_sleep = max(0.4, n_ch / 45.0)
            await _p(f"\U0001f4ac Restoring {total_msgs:,} messages ({n_ch} channels)…", 48)

            _msgs_done = {"n": 0}

            async def _restore_channel_msgs(cd):
                new_ch = channel_map.get(cd["id"])
                if not new_ch or not isinstance(new_ch, discord.TextChannel):
                    return
                msgs = cd["messages"]
                if not msgs:
                    return
                ch_name = new_ch.name
                _state["ch"][ch_name] = [0, len(msgs)]

                try:
                    webhook = await new_ch.create_webhook(name="Blood Backup")
                except Exception as e:
                    log.warning("Can't create webhook in #%s: %s", ch_name, e)
                    _state["ch"][ch_name] = [len(msgs), len(msgs)]
                    return

                for i, md in enumerate(msgs):
                    content = md.get("content", "") or ""
                    if md.get("attachments"):
                        att = "\n".join(
                            f"\U0001f4ce `{a['filename']}`" for a in md["attachments"])
                        content = f"{content}\n{att}" if content else att
                    embeds = []
                    for ed in md.get("embeds", []):
                        try:
                            embeds.append(discord.Embed.from_dict(ed))
                        except Exception:
                            pass
                    if not content and not embeds:
                        _state["ch"][ch_name][0] = i + 1
                        _msgs_done["n"] += 1
                        _state["pct"] = min(48 + (_msgs_done["n"] / max(total_msgs, 1)) * 40, 88)
                        continue
                    try:
                        await webhook.send(
                            content=content[:2000] if content else None,
                            username=md.get("author_name", "Unknown")[:80],
                            avatar_url=md.get("author_avatar"),
                            embeds=embeds[:10] if embeds else discord.utils.MISSING,
                            wait=False,
                        )
                        await asyncio.sleep(_msg_sleep)
                    except Exception as e2:
                        log.warning("Msg send fail #%s: %s", ch_name, e2)
                    _state["ch"][ch_name][0] = i + 1
                    _msgs_done["n"] += 1
                    _state["pct"] = min(48 + (_msgs_done["n"] / max(total_msgs, 1)) * 40, 88)

                try:
                    await webhook.delete()
                except Exception:
                    pass

            await asyncio.gather(*[_restore_channel_msgs(cd) for cd in msg_channels])
            _state["ch"] = {}
            await _p("✅ Messages restored", 88)
            await _force_update()
        else:
            await _p("⏭️ Skipped messages", 88)

        # 10 — Member roles + nicknames (88-93%)
        if members:
            member_data = data.get("members", [])
            await _p(f"\U0001f465 Assigning roles to {len(member_data)} members…", 88)
            for md in member_data:
                try:
                    member = guild.get_member(int(md["id"]))
                except (ValueError, TypeError):
                    continue
                if not member:
                    continue
                to_add = []
                for old_rid in md.get("role_ids", []):
                    nr = role_map.get(old_rid)
                    if nr and nr < bot_top:
                        to_add.append(nr)
                if md.get("nick") and member != guild.me:
                    try:
                        await member.edit(nick=md["nick"], reason="Backup restore")
                    except Exception:
                        pass
                if to_add:
                    try:
                        await member.add_roles(*to_add, reason="Backup restore")
                        await asyncio.sleep(0.3)
                    except Exception as e:
                        log.warning("Can't assign roles to %s: %s", md.get("name"), e)
            await _p("✅ Member roles assigned", 93)
        else:
            await _p("⏭️ Skipped member roles", 93)

        # 11 — Emojis (93-97%)
        if emojis:
            emoji_data = data.get("emojis", [])
            if emoji_data:
                await _p(f"\U0001f600 Restoring {len(emoji_data)} emojis…", 93)
                for ed in emoji_data:
                    img = _unb64(ed.get("image_b64"))
                    if img:
                        try:
                            await guild.create_custom_emoji(
                                name=ed["name"], image=img, reason="Backup restore")
                            await asyncio.sleep(1)
                        except Exception as e:
                            log.warning("Can't create emoji %s: %s", ed["name"], e)

            # 12 — Stickers (97-99%)
            sticker_data = data.get("stickers", [])
            if sticker_data:
                await _p(f"\U0001f3f7️ Restoring {len(sticker_data)} stickers…", 97)
                for sd in sticker_data:
                    img = _unb64(sd.get("image_b64"))
                    if img:
                        try:
                            f = discord.File(io.BytesIO(img),
                                             filename=f"{sd['name']}.png")
                            await guild.create_sticker(
                                name=sd["name"],
                                description=sd.get("description", "Restored"),
                                emoji=sd.get("emoji", "\U0001f516"),
                                file=f, reason="Backup restore",
                            )
                            await asyncio.sleep(1)
                        except Exception as e:
                            log.warning("Can't create sticker %s: %s", sd["name"], e)
        else:
            await _p("⏭️ Skipped emojis & stickers", 97)

        # 13 — Special channels (99-100%)
        if settings and channels:
            sp_kw = {}
            for key, old_key, ch_type in [
                ("afk_channel", "afk_channel_id", discord.VoiceChannel),
                ("system_channel", "system_channel_id", discord.TextChannel),
                ("rules_channel", "rules_channel_id", discord.TextChannel),
                ("public_updates_channel", "public_updates_channel_id", discord.TextChannel),
            ]:
                old_id = server.get(old_key)
                if old_id and old_id in channel_map and isinstance(channel_map[old_id], ch_type):
                    sp_kw[key] = channel_map[old_id]
            if sp_kw:
                try:
                    await guild.edit(**sp_kw, reason="Backup restore — special channels")
                except Exception as e:
                    log.warning("Can't set special channels: %s", e)

        _state["done"] = True
        pulse_task.cancel()
        await _p("✅ Restore complete!", 100)
        await _force_update()
        await asyncio.sleep(3)

        if progress_ch:
            try:
                await progress_ch.delete(reason="Backup restore complete")
            except Exception:
                pass

        return True

    except Exception as e:
        _state["done"] = True
        pulse_task.cancel()
        log.error("Restore failed: %s", e, exc_info=True)
        if progress_msg:
            try:
                await progress_msg.edit(embed=discord.Embed(
                    title="❌ Restore Failed",
                    description=str(e),
                    color=0xff0000,
                ))
            except Exception:
                pass
        await _p(f"❌ Restore failed: {e}")
        if progress_ch:
            try:
                await asyncio.sleep(10)
                await progress_ch.delete()
            except Exception:
                pass
        return False
