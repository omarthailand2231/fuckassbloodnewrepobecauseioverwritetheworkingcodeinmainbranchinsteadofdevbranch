"""Clawd Radio — Discord thread + live music-player panel.

`/radio` opens a dedicated thread and drops a single control panel into it:
a live embed with cover art, track name, a progress bar that ticks every ~5s,
and Like / Dislike / Skip / Pause / Stop buttons. Whenever anyone (or the bot)
posts in the thread, the panel is deleted and re-sent so it always sits at the
very bottom — exactly like a pinned now-playing bar.

The panel is the single source of truth for the now-playing display; voice.py
pushes track changes here via `set_track()` instead of spamming text lines.
"""

import asyncio
import logging
import time
from typing import Optional

import discord

log = logging.getLogger("blood.radio_panel")

# guild_id -> RadioPanel, and thread_id -> RadioPanel (for the on_message hook)
_panels: dict[str, "RadioPanel"] = {}
_panels_by_thread: dict[int, "RadioPanel"] = {}

UPDATE_SECONDS = 5
REPOST_DEBOUNCE = 1.5
THREAD_NAME = "📻 Clawd Radio"


def get_panel(guild_id: str) -> Optional["RadioPanel"]:
    return _panels.get(str(guild_id))


def get_panel_by_thread(thread_id: int) -> Optional["RadioPanel"]:
    return _panels_by_thread.get(thread_id)


async def teardown(guild_id: str):
    panel = _panels.get(str(guild_id))
    if panel:
        await panel.stop()


# ── formatting helpers ────────────────────────────────────────────────────────

def _fmt_time(seconds: float) -> str:
    s = int(max(0, seconds))
    m, s = divmod(s, 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _progress_bar(elapsed: float, duration: float, width: int = 22) -> str:
    if not duration or duration <= 0:
        return "🔴 ───────  live  ───────"
    frac = max(0.0, min(1.0, elapsed / duration))
    pos = int(frac * (width - 1))
    return "".join("🔘" if i == pos else "▬" for i in range(width))


_SOURCE_COLOR = {
    "youtube": 0xFF0000,
    "spotify": 0x1DB954,
    "soundcloud": 0xFF7700,
    "deezer": 0xA238FF,
}


# ── the button bar ─────────────────────────────────────────────────────────────

class RadioControls(discord.ui.View):
    def __init__(self, panel: "RadioPanel"):
        super().__init__(timeout=None)
        self.panel = panel
        gid = panel.guild_id

        # Stable custom_ids so re-rendering the panel never invalidates a button
        # mid-click (each guild has exactly one panel, so per-guild ids are unique).
        like = discord.ui.Button(emoji="❤️", style=discord.ButtonStyle.success,
                                 row=0, custom_id=f"cr:like:{gid}")
        like.callback = self._like
        self.add_item(like)

        dislike = discord.ui.Button(emoji="👎", style=discord.ButtonStyle.danger,
                                    row=0, custom_id=f"cr:dislike:{gid}")
        dislike.callback = self._dislike
        self.add_item(dislike)

        skip = discord.ui.Button(emoji="⏭️", label="Skip", style=discord.ButtonStyle.secondary,
                                 row=0, custom_id=f"cr:skip:{gid}")
        skip.callback = self._skip
        self.add_item(skip)

        paused = panel.paused
        pause = discord.ui.Button(
            emoji=("▶️" if paused else "⏸️"),
            label=("Resume" if paused else "Pause"),
            style=discord.ButtonStyle.secondary, row=0, custom_id=f"cr:pause:{gid}",
        )
        pause.callback = self._pause
        self.add_item(pause)

        stop = discord.ui.Button(emoji="⏹️", label="Stop", style=discord.ButtonStyle.secondary,
                                 row=0, custom_id=f"cr:stop:{gid}")
        stop.callback = self._stop
        self.add_item(stop)

    def _current_title(self) -> Optional[str]:
        return self.panel.track.title if self.panel.track else None

    async def _like(self, interaction: discord.Interaction):
        title = self._current_title()
        if not title:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        # Ack FIRST (within Discord's 3s window). record_feedback embeds text with a
        # sentence-transformer (~2-3s) — running it before responding expires the
        # interaction token (10062). Respond, then do the slow work off the loop.
        await interaction.response.send_message(
            f"❤️ Liked **{title}** — I'll lean into this for you.", ephemeral=True)
        try:
            from voice import record_feedback_async
            await record_feedback_async(str(interaction.user.id), title, True)
        except Exception as e:
            log.warning("[PANEL] like failed: %s", e)

    async def _dislike(self, interaction: discord.Interaction):
        title = self._current_title()
        if not title:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"👎 Noted — disliking **{title}** (this stacks, so I'll back off that "
            f"artist harder each time). Skipping it.", ephemeral=True)
        try:
            from voice import record_feedback_async, skip_music
            if interaction.guild:
                await skip_music(interaction.guild)        # skip instantly…
            await record_feedback_async(str(interaction.user.id), title, False)  # …then record off-loop
        except Exception as e:
            log.warning("[PANEL] dislike failed: %s", e)

    async def _skip(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "⏭️ Skipping — just not the moment for it (no taste hit).", ephemeral=True)
        try:
            from voice import skip_music
            if interaction.guild:
                await skip_music(interaction.guild)
        except Exception as e:
            log.warning("[PANEL] skip failed: %s", e)

    async def _pause(self, interaction: discord.Interaction):
        guild = interaction.guild
        vc = guild.voice_client if guild else None
        if not vc:
            await interaction.response.send_message("Not connected to voice.", ephemeral=True)
            return
        try:
            if vc.is_paused():
                vc.resume()
                self.panel.mark_paused(False)
                await interaction.response.send_message("▶️ Resumed.", ephemeral=True)
            else:
                vc.pause()
                self.panel.mark_paused(True)
                await interaction.response.send_message("⏸️ Paused.", ephemeral=True)
        except Exception as e:
            log.warning("[PANEL] pause toggle failed: %s", e)
            await interaction.response.send_message("Couldn't toggle pause.", ephemeral=True)
            return
        await self.panel.refresh(rebuild_view=True)

    async def _stop(self, interaction: discord.Interaction):
        await interaction.response.send_message("⏹️ Signing off the radio…", ephemeral=True)
        guild = interaction.guild
        try:
            from voice import stop_radio, stop_music
            if guild:
                await stop_radio(str(guild.id))
                await stop_music(guild)
        except Exception as e:
            log.warning("[PANEL] stop failed: %s", e)


# ── the panel ──────────────────────────────────────────────────────────────────

class RadioPanel:
    def __init__(self, bot, guild: discord.Guild, thread: discord.abc.Messageable):
        self.bot = bot
        self.guild = guild
        self.guild_id = str(guild.id)
        self.thread = thread

        self.track = None
        self._started_at = time.monotonic()
        self.paused = False
        self.reconnecting = False
        self._paused_accum = 0.0
        self._paused_at = 0.0

        self.message: Optional[discord.Message] = None
        self._own_ids: set[int] = set()
        self._lock = asyncio.Lock()
        self._updater_task: Optional[asyncio.Task] = None
        self._repost_task: Optional[asyncio.Task] = None
        self._dirty = False
        self.stopped = False

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def start(self):
        """Post the initial placeholder panel and begin the live updater."""
        embed, view = self._render()
        try:
            self.message = await self.thread.send(embed=embed, view=view)
            self._own_ids.add(self.message.id)
        except Exception as e:
            log.warning("[PANEL] initial send failed: %s", e)
            return
        self._updater_task = asyncio.create_task(self._updater())

    async def set_track(self, track):
        """A new song started — leave the finished song as a permanent history line in
        the thread, reset the clock, and re-post the live panel to the bottom."""
        if self.stopped:
            return
        prev = self.track
        self.track = track
        self._started_at = time.monotonic()
        self.paused = False
        self.reconnecting = False
        self._paused_accum = 0.0
        self._paused_at = 0.0
        if prev is not None:
            await self._post_history(prev)
        await self.repost()

    async def _post_history(self, track):
        """Drop a permanent one-line record of a played song into the thread. These are
        never deleted, so the thread becomes a scrolling history with the panel below."""
        if self.stopped:
            return
        try:
            title = (getattr(track, "title", "") or "Unknown")[:200]
            url = getattr(track, "url", "") or ""
            dur = getattr(track, "duration", 0) or 0
            name = f"[{title}]({url})" if url else title
            extra = f" · {_fmt_time(dur)}" if dur else ""
            stamp = f" · <t:{int(time.time())}:t>"
            msg = await self.thread.send(f"🎵 {name}{extra}{stamp}")
            # Mark as ours so the stick-to-bottom hook ignores it (set_track reposts
            # the panel explicitly right after, so it still ends up at the bottom).
            self._own_ids.add(msg.id)
        except Exception as e:
            log.debug("[PANEL] history post failed: %s", e)

    def _apply_clock_freeze(self):
        """Freeze the progress clock whenever playback is stalled (paused OR
        reconnecting) so the bar doesn't race ahead of the audio."""
        frozen = self.paused or self.reconnecting
        now = time.monotonic()
        if frozen and self._paused_at == 0.0:
            self._paused_at = now
        elif not frozen and self._paused_at:
            self._paused_accum += max(0.0, now - self._paused_at)
            self._paused_at = 0.0

    def mark_paused(self, paused: bool):
        self.paused = bool(paused)
        self._apply_clock_freeze()

    async def set_reconnecting(self, value: bool):
        value = bool(value)
        if value == self.reconnecting:
            return
        self.reconnecting = value
        self._apply_clock_freeze()
        await self.refresh(rebuild_view=True)

    async def stop(self, final_text: str = "📻 Clawd Radio signed off. Thanks for listening."):
        if self.stopped:
            return
        self.stopped = True
        _panels.pop(self.guild_id, None)
        if self.message:
            _panels_by_thread.pop(getattr(self.thread, "id", None), None)
        for task in (self._updater_task, self._repost_task):
            if task and not task.done():
                task.cancel()
        # Replace the panel with a static sign-off (no buttons) and archive the thread.
        async with self._lock:
            if self.message:
                try:
                    embed = discord.Embed(description=final_text, color=0x2B2D31)
                    await self.message.edit(embed=embed, view=None)
                except Exception:
                    pass
        try:
            if isinstance(self.thread, discord.Thread):
                await self.thread.edit(archived=True)
        except Exception:
            pass

    # ── elapsed / rendering ────────────────────────────────────────────────────

    def _elapsed(self) -> float:
        now = time.monotonic()
        # _paused_at is set whenever the clock is frozen (paused OR reconnecting).
        paused_extra = (now - self._paused_at) if self._paused_at else 0.0
        return max(0.0, now - self._started_at - self._paused_accum - paused_extra)

    def _build_view(self):
        return RadioControls(self)

    def _render(self):
        return self._render_embed(), self._build_view()

    def _render_embed(self):
        track = self.track
        if not track:
            embed = discord.Embed(
                title="📻 Clawd Radio",
                description="Tuning in… finding the first track for the room.",
                color=0x5865F2,
            )
            embed.set_footer(text="Clawd Radio")
            return embed

        duration = getattr(track, "duration", 0) or 0
        elapsed = min(self._elapsed(), duration) if duration else self._elapsed()
        bar = _progress_bar(elapsed, duration)
        if duration:
            timeline = f"`{_fmt_time(elapsed)}` {bar} `{_fmt_time(duration)}`"
        else:
            timeline = f"{bar}  `{_fmt_time(elapsed)}`"

        if self.reconnecting:
            status = "📡 Reconnecting…"
        elif self.paused:
            status = "⏸️ Paused"
        else:
            status = "▶️ On air"
        color = _SOURCE_COLOR.get(getattr(track, "source_type", ""), 0x5865F2)

        embed = discord.Embed(
            title=track.title[:250],
            url=getattr(track, "url", None) or None,
            description=f"{status}\n\n{timeline}",
            color=color,
        )
        thumb = getattr(track, "thumbnail", "") or ""
        if thumb:
            embed.set_image(url=thumb)

        # Up next, if the queue has something staged.
        try:
            from voice import get_music_queue
            mq = get_music_queue(self.guild_id)
            if mq.queue:
                embed.add_field(name="Up next", value=mq.queue[0].title[:200], inline=False)
        except Exception:
            pass

        req = getattr(track, "requester", "") or "Clawd Radio"
        embed.set_footer(text=f"Clawd Radio • {req}")
        return embed

    # ── live updater (edits in place every few seconds) ────────────────────────

    async def _updater(self):
        try:
            while not self.stopped:
                await asyncio.sleep(UPDATE_SECONDS)
                if self.stopped or self.paused or self.reconnecting or not self.track:
                    continue
                await self.refresh()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.debug("[PANEL] updater stopped: %s", e)

    async def refresh(self, rebuild_view: bool = False):
        """Edit the current panel message in place.

        The 5s progress tick edits the embed only (leaving the button view intact,
        so a click is never invalidated mid-render). Pass rebuild_view=True when the
        controls themselves change (e.g. Pause↔Resume)."""
        if self.stopped or not self.message:
            return
        embed = self._render_embed()
        view = self._build_view() if rebuild_view else None
        async with self._lock:
            if self.stopped or not self.message:
                return
            try:
                if view is not None:
                    await self.message.edit(embed=embed, view=view)
                else:
                    await self.message.edit(embed=embed)
            except discord.NotFound:
                pass
            except Exception as e:
                log.debug("[PANEL] refresh edit failed: %s", e)

    # ── stick-to-bottom repost ─────────────────────────────────────────────────

    async def repost(self):
        """Delete the panel and re-send it so it lands at the bottom of the thread."""
        if self.stopped:
            return
        embed, view = self._render()
        async with self._lock:
            if self.stopped:
                return
            old = self.message
            try:
                new_msg = await self.thread.send(embed=embed, view=view)
                self.message = new_msg
                self._own_ids.add(new_msg.id)
                if len(self._own_ids) > 40:
                    # Discord ids are snowflakes (monotonic), so the largest are the
                    # most recent — keep those for self-filtering.
                    self._own_ids = set(sorted(self._own_ids)[-40:])
            except Exception as e:
                log.warning("[PANEL] repost send failed: %s", e)
                return
            if old is not None:
                try:
                    await old.delete()
                except Exception:
                    pass

    async def on_thread_message(self, message: discord.Message):
        """Called for every message in the thread; bounce the panel to the bottom."""
        if self.stopped:
            return
        if message.id in self._own_ids:
            return  # our own panel (re)post — ignore, or we'd loop forever
        self._dirty = True
        if self._repost_task and not self._repost_task.done():
            return  # a debounced repost is already pending; it will coalesce this
        self._repost_task = asyncio.create_task(self._debounced_repost())

    async def _debounced_repost(self):
        try:
            await asyncio.sleep(REPOST_DEBOUNCE)
            if self._dirty and not self.stopped:
                self._dirty = False
                await self.repost()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.debug("[PANEL] debounced repost failed: %s", e)


# ── entry point used by /radio ─────────────────────────────────────────────────

async def start_panel(bot, guild: discord.Guild,
                      parent_channel: discord.abc.Messageable,
                      author: Optional[discord.abc.User] = None) -> Optional["RadioPanel"]:
    """Create (or reuse) the radio thread and post a fresh control panel into it.

    Returns the panel, or None if the thread/panel couldn't be created (caller
    should fall back to plain text in that case).
    """
    # Tear down any previous panel for this guild first.
    old = _panels.get(str(guild.id))
    if old:
        await old.stop()

    thread: Optional[discord.abc.Messageable] = None
    try:
        if isinstance(parent_channel, discord.Thread):
            thread = parent_channel
        elif hasattr(parent_channel, "create_thread"):
            thread = await parent_channel.create_thread(
                name=THREAD_NAME,
                type=discord.ChannelType.public_thread,
                auto_archive_duration=1440,
            )
        else:
            return None
    except Exception as e:
        log.warning("[PANEL] thread creation failed: %s", e)
        return None

    panel = RadioPanel(bot, guild, thread)
    _panels[str(guild.id)] = panel
    if isinstance(thread, discord.Thread):
        _panels_by_thread[thread.id] = panel
    await panel.start()

    if author is not None and isinstance(thread, discord.Thread):
        try:
            await thread.add_user(author)
        except Exception:
            pass

    return panel
