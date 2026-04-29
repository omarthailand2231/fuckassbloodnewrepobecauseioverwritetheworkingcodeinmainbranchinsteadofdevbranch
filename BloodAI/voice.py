"""
Blood Voice System — Full VC presence with STT (Groq Whisper), TTS (edge-tts),
wake word detection, transcript recording, and smart conversation tracking.
"""

import os
import io
import asyncio
import logging
import tempfile
import time
import json
import wave
import struct
from datetime import datetime, timezone
from collections import defaultdict
from typing import Optional

import aiohttp
import discord

log = logging.getLogger("blood.voice")

# Silence noisy voice_recv spam (WS payload, corrupted stream, CryptoError, RTCP)
logging.getLogger("discord.ext.voice_recv.gateway").setLevel(logging.CRITICAL)
logging.getLogger("discord.ext.voice_recv.router").setLevel(logging.CRITICAL)
logging.getLogger("discord.ext.voice_recv.reader").setLevel(logging.CRITICAL)

# ── Config ────────────────────────────────────────────────────────────────────

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_STT_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_STT_MODEL = "whisper-large-v3-turbo"

# Wake words that activate Blood (case-insensitive, checked as substrings)
WAKE_WORDS = ["blood", "hey blood", "เลือด", "บลัด"]

# TTS voice — edge-tts voice ID (Microsoft Neural)
TTS_VOICE = "en-US-GuyNeural"
TTS_RATE = "+10%"

# Audio settings
SAMPLE_RATE = 48000  # Discord uses 48kHz
CHANNELS = 2         # Stereo
SILENCE_THRESHOLD_SEC = 1.5  # Silence gap to consider speech ended
MAX_SPEECH_SEC = 30  # Max single speech segment
MIN_SPEECH_SEC = 0.3  # Ignore very short blips

# ── Music Config ─────────────────────────────────────────────────────────────

MUSIC_VOLUME = 0.5         # Default music volume (0.0-1.0)
DUCK_VOLUME = 0.15         # Volume while Blood speaks
DUCK_FADE_IN = 0.5         # Seconds before speech to duck
DUCK_FADE_OUT = 0.3        # Seconds after speech to restore

YTDL_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
    "extract_flat": False,
}

FFMPEG_OPTS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn -loglevel quiet",
}


# ── Music Queue ──────────────────────────────────────────────────────────────

class MusicTrack:
    __slots__ = ("title", "url", "stream_url", "duration", "requester", "source_type")

    def __init__(self, title: str, url: str, stream_url: str,
                 duration: int = 0, requester: str = "", source_type: str = "youtube"):
        self.title = title
        self.url = url
        self.stream_url = stream_url
        self.duration = duration
        self.requester = requester
        self.source_type = source_type

    def __repr__(self):
        return f"<Track: {self.title}>"


class MusicQueue:
    """Per-guild music queue with volume control."""

    def __init__(self, guild_id: str):
        self.guild_id = guild_id
        self.queue: list[MusicTrack] = []
        self.current: Optional[MusicTrack] = None
        self.volume = MUSIC_VOLUME
        self.loop = False
        self.paused = False
        self._mixer = None  # BloodMixerSource instance

    def add(self, track: MusicTrack):
        self.queue.append(track)

    def next(self) -> Optional[MusicTrack]:
        if self.loop and self.current:
            return self.current
        if self.queue:
            self.current = self.queue.pop(0)
            return self.current
        self.current = None
        return None

    def clear(self):
        self.queue.clear()
        self.current = None

    def skip(self) -> Optional[MusicTrack]:
        """Skip current, ignore loop."""
        if self.queue:
            self.current = self.queue.pop(0)
            return self.current
        self.current = None
        return None

    @property
    def is_empty(self):
        return len(self.queue) == 0 and self.current is None

    def cleanup_mixer(self):
        """Stop mixer and its music feed."""
        if self._mixer:
            try:
                self._mixer.cleanup()
            except Exception:
                pass
            self._mixer = None


# guild_id -> MusicQueue
_music_queues: dict[str, MusicQueue] = {}


def get_music_queue(guild_id: str) -> MusicQueue:
    if guild_id not in _music_queues:
        _music_queues[guild_id] = MusicQueue(guild_id)
    return _music_queues[guild_id]


# ── yt-dlp extraction ───────────────────────────────────────────────────────

async def extract_track(query: str, requester: str = "") -> Optional[MusicTrack]:
    """Extract audio info from URL or search query using yt-dlp."""
    import re as _re

    # Words in title that indicate a cover/remix (skip these in search)
    _cover_words = {"cover", "covers", "covered", "karaoke", "instrumental",
                    "reaction", "react", "tutorial", "lesson", "how to play"}

    def _is_cover(title: str) -> bool:
        t = title.lower()
        return any(w in t for w in _cover_words)

    def _extract():
        import yt_dlp
        with yt_dlp.YoutubeDL(YTDL_OPTS) as ydl:
            # Detect if it's a URL
            is_url = _re.match(r"https?://", query.strip())
            if is_url:
                info = ydl.extract_info(query.strip(), download=False)
            else:
                # Search with 'audio' to prefer audio uploads over music videos
                search_q = query.strip()
                if "audio" not in search_q.lower() and "mv" not in search_q.lower():
                    search_q += " audio"
                info = ydl.extract_info(f"ytsearch5:{search_q}", download=False)
                if "entries" in info and info["entries"]:
                    # Pick first non-cover result
                    chosen = None
                    for entry in info["entries"]:
                        if entry and not _is_cover(entry.get("title", "")):
                            chosen = entry
                            break
                    info = chosen or info["entries"][0]

            if not info:
                return None

            # For Spotify URLs, yt-dlp may redirect to YouTube
            stream_url = info.get("url") or info.get("webpage_url", "")
            source = "youtube"
            if "soundcloud" in info.get("extractor", "").lower():
                source = "soundcloud"
            elif "spotify" in query.lower():
                source = "spotify"

            return MusicTrack(
                title=info.get("title", "Unknown"),
                url=info.get("webpage_url", query),
                stream_url=stream_url,
                duration=info.get("duration", 0),
                requester=requester,
                source_type=source,
            )

    try:
        return await asyncio.get_event_loop().run_in_executor(None, _extract)
    except Exception as e:
        log.warning("yt-dlp extraction failed for '%s': %s", query[:80], e)
        return None


# ── Playback ─────────────────────────────────────────────────────────────────

async def play_track(guild: discord.Guild, track: MusicTrack,
                     text_channel: Optional[discord.TextChannel] = None):
    """Play a track through BloodMixerSource (thread-based, supports TTS overlay)."""
    vc = guild.voice_client
    if not vc or not vc.is_connected():
        return

    guild_id = str(guild.id)
    mq = get_music_queue(guild_id)
    mq.current = track

    try:
        from mixer import BloodMixerSource

        # Get or create mixer
        mixer = mq._mixer
        if not mixer:
            mixer = BloodMixerSource(asyncio.get_event_loop())
            mq._mixer = mixer

        # Start vc.play with mixer if not already active
        if not vc.is_playing():
            vc.play(mixer)

        # Define what happens when this track ends naturally
        async def on_track_end():
            await _play_next(guild, text_channel)

        # Feed music into mixer (starts FFmpeg in a background thread)
        mixer.start_music(track.stream_url, volume=mq.volume, on_end=on_track_end)

        # Announce in text channel
        if text_channel:
            dur = f" ({track.duration // 60}:{track.duration % 60:02d})" if track.duration else ""
            icon = {"youtube": "🔴", "spotify": "🟢", "soundcloud": "🟠"}.get(track.source_type, "🎵")
            try:
                await text_channel.send(f"{icon} **Now playing:** {track.title}{dur} — requested by {track.requester}")
            except Exception:
                pass

        log.info("Playing: %s (requested by %s)", track.title, track.requester)
    except Exception as e:
        log.warning("Failed to play track: %s", e)
        await _play_next(guild, text_channel)


async def _play_next(guild: discord.Guild,
                     text_channel: Optional[discord.TextChannel] = None):
    """Play the next track in queue, or stop mixer."""
    guild_id = str(guild.id)
    mq = get_music_queue(guild_id)
    nxt = mq.next()
    if nxt:
        await play_track(guild, nxt, text_channel)
    else:
        mq.current = None
        # Queue empty — stop mixer so bot doesn't send silence
        if mq._mixer:
            mq._mixer.stop_music()
        vc = guild.voice_client
        if vc and vc.is_playing():
            vc.stop()
        mq._mixer = None


async def play_music(guild: discord.Guild, query: str, requester: str = "",
                     text_channel: Optional[discord.TextChannel] = None,
                     requester_id: Optional[str] = None) -> str:
    """High-level: extract track, add to queue, play if not playing."""
    guild_id = str(guild.id)
    vc = guild.voice_client
    if not vc or not vc.is_connected():
        return "❌ Not in a voice channel. Use /joinvc first."

    track = await extract_track(query, requester)
    if not track:
        return f"❌ Could not find anything for: {query}"

    # Auto-record positive taste — if user asked for it, they probably like it
    if requester_id and track.title:
        record_feedback(requester_id, track.title, positive=True)

    mq = get_music_queue(guild_id)
    # Check if music is currently playing (mixer has active music feed)
    is_playing = mq._mixer and mq._mixer.has_music and mq.current
    if is_playing:
        mq.add(track)
        pos = len(mq.queue)
        return f"📋 **Queued #{pos}:** {track.title}"
    else:
        mq.current = track
        await play_track(guild, track, text_channel)
        return f"🎵 **Playing:** {track.title}"


async def skip_music(guild: discord.Guild) -> str:
    """Skip current track."""
    guild_id = str(guild.id)
    mq = get_music_queue(guild_id)
    if not mq._mixer or not mq._mixer.has_music:
        return "Nothing is playing."
    mq._mixer.stop_music()  # kills FFmpeg thread, clears buffer
    nxt = mq.skip()
    if nxt:
        await play_track(guild, nxt)
    else:
        mq.current = None
        vc = guild.voice_client
        if vc and vc.is_playing():
            vc.stop()
        mq._mixer = None
    return "⏭️ Skipped."


async def stop_music(guild: discord.Guild) -> str:
    """Stop playback and clear queue."""
    guild_id = str(guild.id)
    mq = get_music_queue(guild_id)
    mq.cleanup_mixer()
    mq.clear()
    vc = guild.voice_client
    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop()
    return "⏹️ Stopped and cleared queue."


def get_queue_info(guild_id: str) -> str:
    """Return formatted queue info."""
    mq = get_music_queue(guild_id)
    lines = []
    if mq.current:
        dur = f" ({mq.current.duration // 60}:{mq.current.duration % 60:02d})" if mq.current.duration else ""
        lines.append(f"▶️ **Now:** {mq.current.title}{dur}")
    if mq.queue:
        for i, t in enumerate(mq.queue[:10], 1):
            dur = f" ({t.duration // 60}:{t.duration % 60:02d})" if t.duration else ""
            lines.append(f"`{i}.` {t.title}{dur}")
        if len(mq.queue) > 10:
            lines.append(f"... and {len(mq.queue) - 10} more")
    if not lines:
        return "Queue is empty."
    lines.append(f"\n🔊 Volume: {int(mq.volume * 100)}%{' 🔁 Loop ON' if mq.loop else ''}")
    return "\n".join(lines)


def set_music_volume(guild_id: str, vol: float) -> str:
    """Set volume (0.0-1.0)."""
    vol = max(0.0, min(1.0, vol))
    mq = get_music_queue(guild_id)
    mq.volume = vol
    if mq._mixer:
        mq._mixer.set_music_volume(vol)
    return f"🔊 Volume set to {int(vol * 100)}%"


# ── Music Taste / Recommendation System ──────────────────────────────────────

TASTE_DIR = os.path.join(os.path.dirname(__file__), "data", "music_taste")
os.makedirs(TASTE_DIR, exist_ok=True)

# In-memory cache: user_id -> {"liked": [...], "disliked": [...]}
_taste_cache: dict[str, dict] = {}


def _load_taste(user_id: str) -> dict:
    if user_id in _taste_cache:
        return _taste_cache[user_id]
    path = os.path.join(TASTE_DIR, f"{user_id}.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {"liked": [], "disliked": []}
    else:
        data = {"liked": [], "disliked": []}
    _taste_cache[user_id] = data
    return data


def _save_taste(user_id: str, data: dict):
    _taste_cache[user_id] = data
    path = os.path.join(TASTE_DIR, f"{user_id}.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass


def record_feedback(user_id: str, track_title: str, positive: bool):
    """Record user's music taste feedback."""
    data = _load_taste(user_id)
    entry = track_title.strip()
    if positive:
        if entry not in data["liked"]:
            data["liked"].append(entry)
        # Remove from disliked if present
        data["disliked"] = [d for d in data["disliked"] if d != entry]
    else:
        if entry not in data["disliked"]:
            data["disliked"].append(entry)
        data["liked"] = [l for l in data["liked"] if l != entry]
    # Keep last 100 each
    data["liked"] = data["liked"][-100:]
    data["disliked"] = data["disliked"][-100:]
    _save_taste(user_id, data)


# Track recently played per user to avoid repeats
_recent_plays: dict[str, list[str]] = {}  # user_id -> last N queries
RECENT_PLAYS_MAX = 20


def get_taste_query(user_id: str) -> str:
    """Build a YouTube search query based on user taste. Avoids recent repeats."""
    import random
    data = _load_taste(user_id)
    liked = data.get("liked", [])
    recent = _recent_plays.get(user_id, [])

    # Cold start — wide genre variety
    cold_genres = [
        "popular music 2025", "top hits 2025", "trending songs today",
        "new music this week", "hot 100 songs", "viral tiktok songs 2025",
        "chill vibes playlist", "lofi hip hop mix", "pop hits playlist",
        "rock classics mix", "edm festival mix", "best of 2024 music",
        "thai music 2025", "kpop new releases", "rap hits 2025",
        "indie discoveries", "jazz lounge", "rnb soul mix",
        "anime openings mix", "gaming soundtrack mix",
        "90s hits playlist", "2000s throwback mix", "workout music mix",
        "late night drive music", "sad songs playlist", "happy upbeat songs",
        "acoustic covers playlist", "lo-fi chill beats", "synthwave mix",
        "bedroom pop", "hyperpop mix", "classical music for studying",
    ]

    if not liked:
        # Pick from cold genres, avoid recent
        available = [g for g in cold_genres if g not in recent]
        if not available:
            available = cold_genres
        query = random.choice(available)
        _track_recent(user_id, query)
        return query

    # Build diverse queries from liked tracks
    # Shuffle liked so we don't always pick the same base
    pool = liked.copy()
    random.shuffle(pool)

    # Try to find a base that wasn't recently used
    base = None
    for candidate in pool:
        if candidate not in recent:
            base = candidate
            break
    if not base:
        base = random.choice(pool)

    # Diverse query templates — never just replay the same song
    templates = [
        f"{base} radio mix",
        f"songs similar to {base}",
        f"if you like {base}",
        f"{base} genre playlist",
        f"artists like {base}",
        f"more songs by the artist of {base}",
        f"{base} type beat music",
    ]

    # 30% chance: explore something new entirely
    if random.random() < 0.3:
        explore = [
            f"songs by the same artist as {base}",
            f"{base} album full",
            f"more from {base} artist",
            f"best of {base} genre",
        ] + cold_genres[:10]  # mix in some genre exploration
        query = random.choice(explore)
    else:
        query = random.choice(templates)

    _track_recent(user_id, query)
    return query


def _track_recent(user_id: str, query: str):
    """Record a query as recently used."""
    if user_id not in _recent_plays:
        _recent_plays[user_id] = []
    _recent_plays[user_id].append(query)
    if len(_recent_plays[user_id]) > RECENT_PLAYS_MAX:
        _recent_plays[user_id] = _recent_plays[user_id][-RECENT_PLAYS_MAX:]


# ── Random Music DJ System ────────────────────────────────────────────────────

class RandomDJ:
    """Multi-user random music DJ with priority rotation.

    Priority system:
    - First user to /randommusic gets priority
    - 1 user: play normally
    - 2 users: priority user plays 2, then user2 plays 1, repeat
    - 3 users: priority user plays 3, user2 plays 1, user3 plays 1, repeat
    - N users: priority user plays N, others play 1 each, repeat
    """

    def __init__(self, guild_id: str):
        self.guild_id = guild_id
        self.participants: list[str] = []  # user_ids in join order
        self.user_names: dict[str, str] = {}  # user_id -> display_name
        self.active = False
        self._rotation_idx = 0  # Tracks position in rotation cycle
        self._priority_remaining = 0  # How many priority songs left before rotating

    def add_user(self, user_id: str, display_name: str):
        if user_id not in self.participants:
            self.participants.append(user_id)
            self.user_names[user_id] = display_name

    def remove_user(self, user_id: str):
        if user_id in self.participants:
            self.participants.remove(user_id)
            self.user_names.pop(user_id, None)
            # Adjust rotation if needed
            if not self.participants:
                self.active = False
                self._rotation_idx = 0
                self._priority_remaining = 0

    def get_next_user(self) -> Optional[str]:
        """Get the next user whose music should play based on priority rotation."""
        if not self.participants:
            return None

        n = len(self.participants)
        if n == 1:
            return self.participants[0]

        # Priority user is always index 0 (first to join)
        priority_user = self.participants[0]
        priority_count = n  # priority user gets N songs per cycle

        # Build rotation cycle: [priority x N, user2, user3, ..., userN]
        if self._priority_remaining > 0:
            self._priority_remaining -= 1
            return priority_user
        else:
            # Rotate through non-priority users
            non_priority = self.participants[1:]
            if self._rotation_idx >= len(non_priority):
                # Cycle complete, reset to priority
                self._rotation_idx = 0
                self._priority_remaining = priority_count - 1  # -1 because we return priority now
                return priority_user
            else:
                user = non_priority[self._rotation_idx]
                self._rotation_idx += 1
                return user

    @property
    def is_active(self):
        return self.active and len(self.participants) > 0


# guild_id -> RandomDJ
_random_djs: dict[str, RandomDJ] = {}


def get_random_dj(guild_id: str) -> RandomDJ:
    if guild_id not in _random_djs:
        _random_djs[guild_id] = RandomDJ(guild_id)
    return _random_djs[guild_id]


async def start_random_music(guild: discord.Guild, user_id: str, display_name: str,
                             text_channel: Optional[discord.TextChannel] = None) -> str:
    """Add user to random DJ and start playing if not already."""
    guild_id = str(guild.id)
    dj = get_random_dj(guild_id)

    already_in = user_id in dj.participants
    dj.add_user(user_id, display_name)

    if already_in:
        return f"🎲 You're already in the DJ rotation! ({len(dj.participants)} participants)"

    if not dj.active:
        dj.active = True
        # Start the DJ loop
        asyncio.create_task(_dj_loop(guild, text_channel))
        return f"🎲 **Random DJ started!** Playing music based on your taste. Say 'I like this' or 'skip' to teach me."
    else:
        pos = dj.participants.index(user_id) + 1
        priority_name = dj.user_names.get(dj.participants[0], "???")
        return (f"🎲 Joined DJ rotation! Position #{pos}/{len(dj.participants)}. "
                f"Priority: {priority_name} (plays {len(dj.participants)} songs per cycle)")


async def stop_random_music(guild_id: str, user_id: str) -> str:
    """Remove user from random DJ."""
    dj = get_random_dj(guild_id)
    if user_id not in dj.participants:
        return "You're not in the DJ rotation."
    dj.remove_user(user_id)
    if not dj.participants:
        dj.active = False
        return "🎲 DJ stopped — no participants left."
    return f"🎲 Left DJ rotation. {len(dj.participants)} still going."


async def _dj_loop(guild: discord.Guild, text_channel: Optional[discord.TextChannel]):
    """Background loop that auto-queues random music based on rotation."""
    guild_id = str(guild.id)
    dj = get_random_dj(guild_id)
    mq = get_music_queue(guild_id)

    while dj.is_active:
        vc = guild.voice_client
        if not vc or not vc.is_connected():
            dj.active = False
            break

        # Only queue next when mixer has no music or no current track
        mixer_busy = mq._mixer and mq._mixer.has_music
        if not mixer_busy and not mq.current:
            next_user = dj.get_next_user()
            if not next_user:
                await asyncio.sleep(2)
                continue

            # Build search query based on user's taste
            query = get_taste_query(next_user)
            user_name = dj.user_names.get(next_user, "Someone")

            track = await extract_track(query, f"{user_name} (DJ)")
            if track:
                await play_track(guild, track, text_channel)
                if text_channel:
                    try:
                        await text_channel.send(
                            f"🎲 **DJ pick for {user_name}:** {track.title}\n"
                            f"*React with 👍/👎 or say 'like'/'dislike' to shape your recommendations*"
                        )
                    except Exception:
                        pass

        # Wait before checking again
        await asyncio.sleep(3)


def cleanup_user_from_dj(guild_id: str, user_id: str):
    """Remove user from DJ when they leave VC."""
    dj = get_random_dj(guild_id)
    dj.remove_user(user_id)
    if not dj.participants:
        dj.active = False


# ── Text+VC Dual Reply Support ────────────────────────────────────────────────

async def speak_in_vc(guild: discord.Guild, text: str, bot_instance):
    """Speak text in VC via TTS (used when user chats in text while in VC with Blood)."""
    if not guild.voice_client or not guild.voice_client.is_connected():
        return

    guild_id = str(guild.id)
    sink = _active_sinks.get(guild_id)
    if not sink:
        return

    await sink._speak(text)


def is_user_in_blood_vc(guild: discord.Guild, user) -> bool:
    """Check if a user is in the same VC as Blood."""
    if not guild.voice_client or not guild.voice_client.channel:
        return False
    return user in guild.voice_client.channel.members


# ── Transcript Storage ────────────────────────────────────────────────────────

# guild_id -> list of transcript entries
# Each entry: {timestamp, user_id, user_name, text, channel_name}
_transcripts: dict[str, list[dict]] = defaultdict(list)

# guild_id -> list of VC session records
# Each session: {channel_name, joined_at, left_at, participants, transcript_entries}
_vc_sessions: dict[str, list[dict]] = defaultdict(list)

# Currently active sessions: guild_id -> session dict
_active_sessions: dict[str, dict] = {}

# Conversation context buffer for smart replies (last N transcripts per guild)
_convo_buffer: dict[str, list[dict]] = defaultdict(list)
CONVO_BUFFER_SIZE = 20

# Track who Blood is in "conversation" with (recently spoke)
_active_conversation: dict[str, set] = defaultdict(set)
_conversation_timeout = 30  # seconds before conversation goes cold

# ── Audio Buffer per User ─────────────────────────────────────────────────────

# Max buffer size: ~10s of audio at 48kHz stereo 16-bit = ~1.9MB
MAX_BUFFER_BYTES = SAMPLE_RATE * CHANNELS * 2 * 10
# Minimum audio bytes to bother sending to STT (~0.5s)
MIN_STT_BYTES = SAMPLE_RATE * CHANNELS * 2 * 0.5


class UserAudioBuffer:
    """Accumulate PCM audio per user, detect silence, trigger STT."""

    def __init__(self, user_id: int, user_name: str):
        self.user_id = user_id
        self.user_name = user_name
        self.buffer = bytearray()
        self.last_packet_time = time.monotonic()
        self.speech_start = time.monotonic()
        self.is_speaking = False

    def add_pcm(self, pcm_data: bytes):
        now = time.monotonic()
        if not self.is_speaking:
            self.is_speaking = True
            self.speech_start = now
        # Cap buffer to prevent memory blowup
        if len(self.buffer) < MAX_BUFFER_BYTES:
            self.buffer.extend(pcm_data)
        self.last_packet_time = now

    def silence_duration(self) -> float:
        return time.monotonic() - self.last_packet_time

    def speech_duration(self) -> float:
        if not self.is_speaking:
            return 0
        return time.monotonic() - self.speech_start

    def harvest(self) -> Optional[bytes]:
        """Return accumulated PCM and reset, or None if too short."""
        if len(self.buffer) < int(SAMPLE_RATE * CHANNELS * 2 * MIN_SPEECH_SEC):
            self.buffer.clear()
            self.is_speaking = False
            return None
        data = bytes(self.buffer)
        self.buffer.clear()
        self.is_speaking = False
        return data

    def clear(self):
        self.buffer.clear()
        self.is_speaking = False


# ── PCM to WAV ────────────────────────────────────────────────────────────────

def pcm_to_wav(pcm_data: bytes, sample_rate: int = SAMPLE_RATE,
               channels: int = CHANNELS) -> bytes:
    """Convert raw PCM16 to WAV bytes for Whisper API."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_data)
    return buf.getvalue()


# ── Groq Whisper STT ─────────────────────────────────────────────────────────

async def transcribe_audio(pcm_data: bytes) -> Optional[str]:
    """Send PCM audio to Groq Whisper and return transcribed text."""
    if not GROQ_API_KEY:
        log.warning("No GROQ_API_KEY — cannot transcribe")
        return None

    wav_data = pcm_to_wav(pcm_data)

    # Skip very small files (< 1KB of actual audio)
    if len(wav_data) < 1024:
        return None

    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}

    form = aiohttp.FormData()
    form.add_field("file", wav_data, filename="audio.wav", content_type="audio/wav")
    form.add_field("model", GROQ_STT_MODEL)
    form.add_field("response_format", "json")
    form.add_field("language", "th")  # Thai primary, Whisper auto-detects mixed

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(GROQ_STT_URL, headers=headers, data=form,
                                    timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    err = await resp.text()
                    log.warning("Whisper STT error %d: %s", resp.status, err[:200])
                    return None
                data = await resp.json()
                text = data.get("text", "").strip()
                return text if text else None
    except Exception as e:
        log.warning("STT failed: %s", e)
        return None


# ── Edge TTS ──────────────────────────────────────────────────────────────────

async def text_to_speech(text: str, voice: str = TTS_VOICE) -> Optional[bytes]:
    """Convert text to speech using edge-tts, return MP3 bytes."""
    try:
        import edge_tts
        communicate = edge_tts.Communicate(text, voice, rate=TTS_RATE)

        buf = io.BytesIO()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                buf.write(chunk["data"])

        mp3_data = buf.getvalue()
        if not mp3_data:
            return None
        return mp3_data
    except Exception as e:
        log.warning("TTS failed: %s", e)
        return None


class MP3AudioSource(discord.AudioSource):
    """Play MP3 bytes through FFmpeg as a Discord audio source."""

    def __init__(self, mp3_data: bytes):
        self._process = None
        self._mp3_data = mp3_data
        self._stdout = None

    async def start(self):
        """Start ffmpeg process to decode MP3 to PCM."""
        self._process = await asyncio.create_subprocess_exec(
            "ffmpeg", "-i", "pipe:0", "-f", "s16le", "-ar", "48000",
            "-ac", "2", "-loglevel", "quiet", "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        # Feed MP3 data and close stdin
        self._process.stdin.write(self._mp3_data)
        await self._process.stdin.drain()
        self._process.stdin.close()
        self._stdout = self._process.stdout

    def read(self) -> bytes:
        """Read 20ms of PCM audio (3840 bytes at 48kHz stereo 16-bit)."""
        if self._stdout is None:
            return b""
        data = self._stdout._buffer[:3840] if hasattr(self._stdout, '_buffer') else b""
        return data

    def cleanup(self):
        if self._process:
            try:
                self._process.kill()
            except Exception:
                pass


class FFmpegPCMAudioPipe(discord.AudioSource):
    """Simpler approach: write MP3 to temp file, use FFmpegPCMAudio."""

    def __init__(self, mp3_data: bytes):
        self._mp3_data = mp3_data
        self._source = None
        self._tmpfile = None

    def prepare(self):
        self._tmpfile = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        self._tmpfile.write(self._mp3_data)
        self._tmpfile.close()
        self._source = discord.FFmpegPCMAudio(self._tmpfile.name)

    def read(self) -> bytes:
        if self._source:
            return self._source.read()
        return b""

    def cleanup(self):
        if self._source:
            self._source.cleanup()
        if self._tmpfile:
            try:
                os.unlink(self._tmpfile.name)
            except Exception:
                pass

    def is_opus(self):
        return False


# ── Wake Word Detection ───────────────────────────────────────────────────────

def contains_wake_word(text: str) -> bool:
    """Check if text contains a wake word."""
    lower = text.lower()
    return any(w in lower for w in WAKE_WORDS)


def is_relevant_to_conversation(text: str, guild_id: str) -> bool:
    """Check if the speech is part of an ongoing conversation with Blood."""
    # If it contains a wake word, always relevant
    if contains_wake_word(text):
        return True
    # If there's an active conversation (Blood recently spoke), consider relevant
    guild_convos = _active_conversation.get(guild_id)
    if guild_convos:
        return True
    return False


# ── Transcript Management ─────────────────────────────────────────────────────

TRANSCRIPT_DIR = os.path.join(os.path.dirname(__file__), "data", "transcripts")
os.makedirs(TRANSCRIPT_DIR, exist_ok=True)


def add_transcript_entry(guild_id: str, user_id: int, user_name: str,
                         text: str, channel_name: str):
    """Add a transcript entry with timestamp."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "user_id": user_id,
        "user_name": user_name,
        "text": text,
        "channel_name": channel_name,
    }
    _transcripts[guild_id].append(entry)
    _convo_buffer[guild_id].append(entry)
    # Trim conversation buffer
    if len(_convo_buffer[guild_id]) > CONVO_BUFFER_SIZE:
        _convo_buffer[guild_id] = _convo_buffer[guild_id][-CONVO_BUFFER_SIZE:]

    # Also append to active session
    if guild_id in _active_sessions:
        _active_sessions[guild_id]["transcript"].append(entry)


def start_vc_session(guild_id: str, channel_name: str):
    """Start tracking a new VC session."""
    session = {
        "channel_name": channel_name,
        "joined_at": datetime.now(timezone.utc).isoformat(),
        "left_at": None,
        "transcript": [],
    }
    _active_sessions[guild_id] = session


def end_vc_session(guild_id: str):
    """End the current VC session and save to disk."""
    if guild_id not in _active_sessions:
        return
    session = _active_sessions.pop(guild_id)
    session["left_at"] = datetime.now(timezone.utc).isoformat()

    # Save to persistent list
    _vc_sessions[guild_id].append(session)
    # Keep only last 50 sessions in memory
    if len(_vc_sessions[guild_id]) > 50:
        _vc_sessions[guild_id] = _vc_sessions[guild_id][-50:]

    # Save to disk
    _save_session(guild_id, session)


def _save_session(guild_id: str, session: dict):
    """Save a VC session transcript to disk as JSON."""
    guild_dir = os.path.join(TRANSCRIPT_DIR, guild_id)
    os.makedirs(guild_dir, exist_ok=True)
    filename = f"vc_{session['joined_at'].replace(':', '-').replace('.', '-')}.json"
    path = os.path.join(guild_dir, filename)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(session, f, ensure_ascii=False, indent=2)
        log.info("Saved VC transcript: %s", path)
    except Exception as e:
        log.warning("Failed to save transcript: %s", e)


def get_recent_sessions(guild_id: str, limit: int = 5) -> list[dict]:
    """Get the N most recent VC sessions (from memory + disk)."""
    # First check memory
    sessions = list(_vc_sessions.get(guild_id, []))

    # Also load from disk if needed
    guild_dir = os.path.join(TRANSCRIPT_DIR, guild_id)
    if os.path.isdir(guild_dir):
        files = sorted(
            [f for f in os.listdir(guild_dir) if f.endswith(".json")],
            reverse=True
        )
        for fname in files[:limit * 2]:  # Load extra to dedup
            path = os.path.join(guild_dir, fname)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    s = json.load(f)
                # Avoid duplicates (check by joined_at)
                if not any(existing.get("joined_at") == s.get("joined_at") for existing in sessions):
                    sessions.append(s)
            except Exception:
                pass

    # Sort by joined_at descending
    sessions.sort(key=lambda s: s.get("joined_at", ""), reverse=True)
    return sessions[:limit]


def format_transcript_txt(session: dict) -> str:
    """Format a session transcript as a plain text string."""
    lines = []
    ch = session.get("channel_name", "unknown")
    joined = session.get("joined_at", "?")
    left = session.get("left_at", "ongoing")
    lines.append(f"=== VC Transcript: #{ch} ===")
    lines.append(f"Started: {joined}")
    lines.append(f"Ended:   {left}")
    lines.append(f"{'=' * 40}")
    lines.append("")

    for entry in session.get("transcript", []):
        ts = entry.get("timestamp", "")
        # Extract just the time portion
        try:
            dt = datetime.fromisoformat(ts)
            ts_short = dt.strftime("%H:%M:%S")
        except Exception:
            ts_short = ts
        user = entry.get("user_name", "Unknown")
        text = entry.get("text", "")
        lines.append(f"[{ts_short}] {user}: {text}")

    if not session.get("transcript"):
        lines.append("(no speech transcribed)")

    return "\n".join(lines)


def summarize_transcript(session: dict) -> str:
    """Generate a quick summary of a transcript session."""
    transcript = session.get("transcript", [])
    if not transcript:
        return "No speech was transcribed during this session."

    # Count speakers
    speakers = defaultdict(int)
    total_words = 0
    for entry in transcript:
        speakers[entry.get("user_name", "Unknown")] += 1
        total_words += len(entry.get("text", "").split())

    ch = session.get("channel_name", "unknown")
    duration = ""
    try:
        joined = datetime.fromisoformat(session["joined_at"])
        left = datetime.fromisoformat(session.get("left_at") or session["joined_at"])
        mins = int((left - joined).total_seconds() / 60)
        duration = f" ({mins}m)" if mins > 0 else ""
    except Exception:
        pass

    speaker_list = ", ".join(f"{name} ({count} msgs)" for name, count in speakers.items())
    return (f"**#{ch}**{duration} — {len(transcript)} messages, ~{total_words} words\n"
            f"Speakers: {speaker_list}")


# ── Voice Receive Sink ────────────────────────────────────────────────────────

class BloodAudioSink:
    """Custom audio sink that buffers per-user PCM and triggers STT."""

    def __init__(self, guild_id: str, channel_name: str, bot_instance,
                 text_channel: Optional[discord.TextChannel] = None):
        self.guild_id = guild_id
        self.channel_name = channel_name
        self.bot = bot_instance
        self.text_channel = text_channel
        self.user_buffers: dict[int, UserAudioBuffer] = {}
        self._running = True
        self._process_task = None
        self._stt_lock = asyncio.Lock()
        self._last_stt_time = 0.0

    def start_processing(self):
        """Start the background task that checks for silence and transcribes."""
        self._process_task = asyncio.create_task(self._process_loop())

    async def _process_loop(self):
        """Periodically check user buffers for completed speech."""
        while self._running:
            try:
                await asyncio.sleep(0.5)  # Check every 500ms (was 300ms)
                now = time.monotonic()
                for uid, buf in list(self.user_buffers.items()):
                    # Check if user stopped speaking (silence gap)
                    if buf.is_speaking and buf.silence_duration() > SILENCE_THRESHOLD_SEC:
                        pcm = buf.harvest()
                        if pcm and len(pcm) >= int(MIN_STT_BYTES):
                            # Throttle: skip if last STT was < 2s ago
                            if now - self._last_stt_time < 2.0:
                                continue
                            if not self._stt_lock.locked():
                                self._last_stt_time = now
                                asyncio.create_task(self._guarded_handle(uid, buf.user_name, pcm))
                        elif pcm:
                            pass  # Too short, discard
                    # Force harvest if speaking too long
                    elif buf.is_speaking and buf.speech_duration() > MAX_SPEECH_SEC:
                        pcm = buf.harvest()
                        if pcm and len(pcm) >= int(MIN_STT_BYTES):
                            if not self._stt_lock.locked():
                                self._last_stt_time = now
                                asyncio.create_task(self._guarded_handle(uid, buf.user_name, pcm))
                    # Clear stale buffers (no packets for 10s)
                    elif not buf.is_speaking and buf.silence_duration() > 10:
                        buf.clear()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning("Voice process loop error: %s", e)

    async def _guarded_handle(self, user_id: int, user_name: str, pcm_data: bytes):
        """Run STT+response with concurrency guard."""
        async with self._stt_lock:
            await self._handle_speech(user_id, user_name, pcm_data)

    @staticmethod
    def _is_junk(text: str) -> bool:
        """Return True if STT output is junk we should ignore."""
        import re as _re
        t = text.strip()
        # Too short
        if len(t) < 2:
            return True
        # URL / link
        if _re.search(r"https?://\S+", t):
            return True
        # Emoji-only (unicode emoji or Discord custom :emoji:)
        cleaned = _re.sub(r"[\U0001F000-\U0001FFFF\u2600-\u27BF\u2700-\u27BF\uFE00-\uFE0F\u200D]", "", t)
        cleaned = _re.sub(r"<a?:\w+:\d+>", "", cleaned)  # Discord custom emoji
        if not cleaned.strip():
            return True
        # Command triggers (slash commands, prefix commands)
        if t.startswith(("/", "!", ".", "?", "$")):
            return True
        # Whisper hallucinations — common junk outputs
        hallucinations = [
            "thank you", "thanks for watching", "subscribe", "like and subscribe",
            "you", "bye", ".", "...", "ขอบคุณ", "สวัสดีครับ", "สวัสดีค่ะ",
            "ฝากกดไลค์", "ฝากกดติดตาม", "♪", "🎵",
        ]
        if t.lower().strip(".!? ") in hallucinations:
            return True
        return False

    async def _handle_speech(self, user_id: int, user_name: str, pcm_data: bytes):
        """Transcribe speech and decide whether to respond."""
        text = await transcribe_audio(pcm_data)
        if not text:
            return

        # Filter junk STT output
        if self._is_junk(text):
            log.debug("[VC STT] Ignored junk: %s", text[:50])
            return

        log.info("[VC STT] %s: %s", user_name, text)

        # Record transcript
        add_transcript_entry(self.guild_id, user_id, user_name, text, self.channel_name)

        # Check if Blood should respond
        has_wake = contains_wake_word(text)
        is_relevant = is_relevant_to_conversation(text, self.guild_id)

        if has_wake or is_relevant:
            # Mark conversation as active
            _active_conversation[self.guild_id] = set()

            # Generate Blood's response
            response = await self._generate_response(user_id, user_name, text)
            if response:
                # Record Blood's response in transcript
                add_transcript_entry(
                    self.guild_id, self.bot.user.id, "Blood",
                    response, self.channel_name
                )

                # TTS and play in VC
                await self._speak(response)

                # Also send to text channel if available
                if self.text_channel:
                    try:
                        await self.text_channel.send(
                            f"🎙️ **{user_name}**: {text}\n💬 **Blood**: {response}"
                        )
                    except Exception:
                        pass

                # Schedule conversation timeout
                asyncio.create_task(self._conversation_timeout())

    async def _generate_response(self, user_id: int, user_name: str, text: str) -> Optional[str]:
        """Generate Blood's response using the AI provider."""
        # Check if it's a music request first — handle directly
        music_result = await self._check_music_request(text, user_name)
        if music_result:
            return music_result

        try:
            from provider import call_ai
            from config import CONFIG

            # Compact voice-mode system prompt — NO YAPPING
            system = (
                "You are Blood, in a voice channel. RULES:\n"
                "- MAX 1-2 sentences. You're speaking aloud, not typing.\n"
                "- No markdown, no emojis, no formatting. Plain speech only.\n"
                "- Be witty but brief. Don't explain jokes. Don't ramble.\n"
                "- If someone asks to play music, just say 'on it' or 'sure' — the music system handles the rest.\n"
                "- You still have all your normal tools and can use them.\n"
                f"Speaker: {user_name}"
            )

            # Include recent conversation context
            context_msgs = []
            for entry in _convo_buffer.get(self.guild_id, [])[-10:]:
                role = "assistant" if entry.get("user_name") == "Blood" else "user"
                prefix = "" if role == "assistant" else f"[{entry.get('user_name', '?')}] "
                context_msgs.append({"role": role, "content": f"{prefix}{entry.get('text', '')}"})

            if not context_msgs:
                context_msgs = [{"role": "user", "content": f"[{user_name}] {text}"}]

            result = await call_ai(system, context_msgs, max_tokens=150)
            content = result.get("content", "").strip()
            return content if content else None
        except Exception as e:
            log.warning("Voice response generation failed: %s", e)
            return None

    async def _check_music_request(self, text: str, requester: str) -> Optional[str]:
        """Detect music requests in speech and handle them."""
        lower = text.lower()

        # Detect music-related keywords
        play_keywords = ["play ", "put on ", "play me ", "เปิดเพลง", "เล่นเพลง", "เปิด"]
        skip_keywords = ["skip", "next song", "next track", "ข้าม"]
        stop_keywords = ["stop music", "stop the music", "หยุดเพลง"]
        like_keywords = ["i like this", "this is good", "love this", "great song", "ชอบ", "เพราะ", "ดี"]
        dislike_keywords = ["i don't like this", "this sucks", "hate this", "bad song", "ไม่ชอบ", "ห่วย", "เปลี่ยน"]
        queue_keywords = ["what's playing", "queue", "now playing", "เพลงอะไร"]
        random_keywords = ["random music", "play something random", "surprise me", "เปิดอะไรก็ได้"]

        # URL paste detection
        import re as _re
        url_match = _re.search(r"(https?://\S+)", text)

        guild = self.bot.get_guild(int(self.guild_id))
        if not guild:
            return None

        # Like/dislike feedback (per user via VC)
        mq = get_music_queue(self.guild_id)
        # Get user_id from requester name -> look up in sink user_buffers
        requester_id = None
        for uid, buf in self.user_buffers.items():
            if buf.user_name == requester:
                requester_id = str(uid)
                break

        if mq.current and requester_id:
            for kw in like_keywords:
                if kw in lower:
                    record_feedback(requester_id, mq.current.title, positive=True)
                    return f"Noted, you like {mq.current.title}. I'll remember that."
            for kw in dislike_keywords:
                if kw in lower:
                    record_feedback(requester_id, mq.current.title, positive=False)
                    # Also skip
                    if guild.voice_client and guild.voice_client.is_playing():
                        guild.voice_client.stop()
                    return "Got it, skipping. I'll play less of that for you."

        # Random music request via voice
        for kw in random_keywords:
            if kw in lower:
                if requester_id:
                    result = await start_random_music(guild, requester_id, requester, self.text_channel)
                    return result

        # Skip
        for kw in skip_keywords:
            if kw in lower:
                result = await skip_music(guild)
                return result

        # Stop
        for kw in stop_keywords:
            if kw in lower:
                result = await stop_music(guild)
                return result

        # Queue info
        for kw in queue_keywords:
            if kw in lower:
                return get_queue_info(self.guild_id)

        # URL pasted
        if url_match:
            result = await play_music(guild, url_match.group(1), requester, self.text_channel,
                                      requester_id=requester_id)
            return result

        # Play request
        for kw in play_keywords:
            if kw in lower:
                # Extract the song query after the keyword
                idx = lower.index(kw) + len(kw)
                query = text[idx:].strip()
                if query:
                    result = await play_music(guild, query, requester, self.text_channel,
                                              requester_id=requester_id)
                    return result

        return None

    async def _speak(self, text: str):
        """Convert text to speech and play in voice channel.
        If music is playing through the mixer, TTS is mixed in with auto-ducking.
        If no music, plays TTS directly."""
        guild = self.bot.get_guild(int(self.guild_id))
        if not guild or not guild.voice_client:
            return

        mp3_data = await text_to_speech(text)
        if not mp3_data:
            log.warning("TTS returned no audio")
            return

        vc = guild.voice_client
        mq = get_music_queue(self.guild_id)
        mixer = mq._mixer

        if mixer and mixer.has_music:
            # Music is playing — feed TTS into mixer (auto-ducks music)
            try:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, mixer.feed_tts_sync, mp3_data)
                log.info("Spoke in VC (mixed with music): %s", text[:60])
            except Exception as e:
                log.warning("TTS via mixer failed: %s", e)
        else:
            # No music playing — play TTS directly
            try:
                source = FFmpegPCMAudioPipe(mp3_data)
                source.prepare()

                tts_done = asyncio.Event()

                def on_tts_done(error):
                    if error:
                        log.warning("TTS playback error: %s", error)
                    tts_done.set()

                vc.play(source, after=on_tts_done)
                await tts_done.wait()
                log.info("Spoke in VC: %s", text[:60])
            except Exception as e:
                log.warning("TTS failed: %s", e)

    async def _conversation_timeout(self):
        """Clear active conversation after timeout."""
        await asyncio.sleep(_conversation_timeout)
        if self.guild_id in _active_conversation:
            _active_conversation.pop(self.guild_id, None)

    def on_voice_data(self, user, pcm_data: bytes):
        """Called when voice data is received from a user."""
        if user.bot:
            return  # Ignore bots
        uid = user.id
        if uid not in self.user_buffers:
            self.user_buffers[uid] = UserAudioBuffer(uid, user.display_name)
        self.user_buffers[uid].add_pcm(pcm_data)

    def stop(self):
        """Stop the processing loop."""
        self._running = False
        if self._process_task:
            self._process_task.cancel()


# ── Active Sinks Registry ────────────────────────────────────────────────────

_active_sinks: dict[str, BloodAudioSink] = {}


def get_active_sink(guild_id: str) -> Optional[BloodAudioSink]:
    return _active_sinks.get(guild_id)


# ── High-level Join/Leave ─────────────────────────────────────────────────────

async def join_and_listen(guild: discord.Guild, vc_channel: discord.VoiceChannel,
                          text_channel: Optional[discord.TextChannel], bot_instance) -> str:
    """Join a voice channel and start listening + recording."""
    guild_id = str(guild.id)

    try:
        import discord.ext.voice_recv as voice_recv
    except ImportError:
        return "❌ Voice receive not available (discord-ext-voice-recv not installed)"

    # Connect or move
    try:
        if guild.voice_client:
            await guild.voice_client.move_to(vc_channel)
            voice_client = guild.voice_client
        else:
            voice_client = await vc_channel.connect(cls=voice_recv.VoiceRecvClient)
    except discord.Forbidden:
        return "❌ No permission to join that voice channel"
    except Exception as e:
        return f"❌ Failed to connect: {e}"

    # Create sink
    sink = BloodAudioSink(guild_id, vc_channel.name, bot_instance, text_channel)
    _active_sinks[guild_id] = sink

    # Start recording session
    start_vc_session(guild_id, vc_channel.name)

    # Start listening
    def callback(user, data: voice_recv.VoiceData):
        sink.on_voice_data(user, data.pcm)

    try:
        voice_client.listen(voice_recv.BasicSink(callback))
    except Exception as e:
        return f"❌ Failed to start listening: {e}"

    # Start processing loop
    sink.start_processing()

    # Start web mixer dashboard
    try:
        import web_mixer as wm
        await wm.start_web_mixer()
        wm.update_state(music_active=False, tts_active=False, ducked=False)
    except Exception as e:
        log.warning("Web mixer not started: %s", e)

    log.info("Joined VC '%s' in %s — listening", vc_channel.name, guild.name)
    return f"✅ Joined **{vc_channel.name}** — listening & recording. Say 'Blood' or 'hey Blood' to talk to me!"


async def leave_voice(guild: discord.Guild) -> str:
    """Leave voice channel and save transcript."""
    guild_id = str(guild.id)

    # Stop sink
    sink = _active_sinks.pop(guild_id, None)
    if sink:
        sink.stop()

    # Stop mixer
    mq = get_music_queue(guild_id)
    mq.cleanup_mixer()
    mq.clear()

    # End session
    end_vc_session(guild_id)

    # Disconnect
    if guild.voice_client:
        await guild.voice_client.disconnect(force=True)

    # Clear conversation state
    _active_conversation.pop(guild_id, None)

    return "✅ Left voice channel. Transcript saved."
