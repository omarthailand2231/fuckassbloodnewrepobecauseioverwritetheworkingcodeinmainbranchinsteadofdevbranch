
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

_stt_session: Optional[aiohttp.ClientSession] = None

def _get_stt_session() -> aiohttp.ClientSession:
    global _stt_session
    if _stt_session is None or _stt_session.closed:
        _stt_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15),
            connector=aiohttp.TCPConnector(limit=2, ttl_dns_cache=300)
        )
    return _stt_session

logging.getLogger("discord.ext.voice_recv.gateway").setLevel(logging.CRITICAL)
logging.getLogger("discord.ext.voice_recv.router").setLevel(logging.CRITICAL)
logging.getLogger("discord.ext.voice_recv.reader").setLevel(logging.CRITICAL)

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_STT_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_STT_MODEL = "whisper-large-v3-turbo"

WAKE_WORDS = ["blood", "hey blood", "เลือด", "บลัด"]

TTS_VOICE = "en-US-GuyNeural"
TTS_RATE = "+10%"

SAMPLE_RATE = 48000
CHANNELS = 2
SILENCE_THRESHOLD_SEC = 1.5
MAX_SPEECH_SEC = 30
MIN_SPEECH_SEC = 0.3

MUSIC_VOLUME = 0.5
DUCK_VOLUME = 0.15
DUCK_FADE_IN = 0.5
DUCK_FADE_OUT = 0.3

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
    def __init__(self, guild_id: str):
        self.guild_id = guild_id
        self.queue: list[MusicTrack] = []
        self.current: Optional[MusicTrack] = None
        self.volume = MUSIC_VOLUME
        self.loop = False
        self.paused = False
        self._mixer = None

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
        if self.queue:
            self.current = self.queue.pop(0)
            return self.current
        self.current = None
        return None

    @property
    def is_empty(self):
        return len(self.queue) == 0 and self.current is None

    def cleanup_mixer(self):
        if self._mixer:
            try:
                self._mixer.cleanup()
            except Exception:
                pass
            self._mixer = None


_music_queues: dict[str, MusicQueue] = {}


def get_music_queue(guild_id: str) -> MusicQueue:
    if guild_id not in _music_queues:
        _music_queues[guild_id] = MusicQueue(guild_id)
    return _music_queues[guild_id]


def _bind_web_mixer_guild(guild_id: Optional[str]):
    try:
        import web_mixer as wm
        wm.bind_guild(guild_id)
    except Exception:
        pass


def _update_web_mixer(**kwargs):
    try:
        import web_mixer as wm
        wm.update_state(**kwargs)
    except Exception:
        pass


def _voice_client_source(vc: Optional[discord.VoiceClient]):
    player = getattr(vc, "_player", None)
    return getattr(player, "source", None)


def _stop_voice_playback(vc):
    """Stop outgoing audio without tearing down voice receive state.

    Never calls vc.stop() on VoiceRecvClient — that kills the listener.
    Uses stop_playing() if available, falls back to _player.stop(),
    then pause() as last resort (always safe).
    """
    if not vc:
        return
    stop_playing = getattr(vc, "stop_playing", None)
    if callable(stop_playing):
        stop_playing()
        return
    player = getattr(vc, "_player", None)
    if player is not None:
        try:
            player.stop()
            return
        except Exception:
            pass
    if vc.is_playing():
        vc.pause()


def _voice_client_has_mixer(vc: Optional[discord.VoiceClient], mixer) -> bool:
    if not vc or not mixer:
        return False
    if _voice_client_source(vc) is not mixer:
        return False
    return vc.is_playing() or vc.is_paused()


def has_active_music(guild: Optional[discord.Guild]) -> bool:
    if not guild:
        return False
    mq = get_music_queue(str(guild.id))
    if not mq.current:
        return False
    mixer = mq._mixer
    vc = guild.voice_client
    return bool(mixer and (mixer.has_music or _voice_client_has_mixer(vc, mixer)))


def get_now_playing_track(guild: Optional[discord.Guild]) -> Optional[MusicTrack]:
    if not guild:
        return None
    mq = get_music_queue(str(guild.id))
    mixer = mq._mixer
    if not mq.current or not mixer or not mixer.has_music:
        return None
    return mq.current


def _sync_web_mixer_music(guild: Optional[discord.Guild]):
    if not guild:
        return
    guild_id = str(guild.id)
    _bind_web_mixer_guild(guild_id)
    mq = get_music_queue(guild_id)
    mixer = mq._mixer
    current = get_now_playing_track(guild)
    music_active = current is not None
    force_duck = bool(mixer.force_duck) if mixer else False
    ducked = bool(mixer and mixer.has_music and mixer.is_ducking)
    _update_web_mixer(
        music_active=music_active,
        music_title=current.title if current else "",
        music_level=mq.volume if music_active else 0.0,
        music_volume=mq.volume,
        ducked=ducked,
        force_duck=force_duck,
    )


def _sync_web_mixer_tts(guild: Optional[discord.Guild], *, active: bool, text: str = "", level: float = 0.0):
    if not guild:
        return
    guild_id = str(guild.id)
    mq = get_music_queue(guild_id)
    mixer = mq._mixer
    ducked = bool(mixer and mixer.has_music and mixer.is_ducking)
    force_duck = bool(mixer.force_duck) if mixer else False
    _update_web_mixer(
        tts_active=active,
        tts_text=text,
        tts_level=level,
        ducked=ducked,
        force_duck=force_duck,
    )


async def extract_track(query: str, requester: str = "") -> Optional[MusicTrack]:
    import re as _re

    _skip_words = {"cover", "covers", "covered", "karaoke", "instrumental",
                   "reaction", "react", "tutorial", "lesson", "how to play",
                   "audiobook", "podcast", "lecture", "asmr", "meditation",
                   "full movie", "documentary", "10 hours", "how to",
                   "correcting", "fix ", "app tutorial"}
    MAX_DURATION = 900
    q_lower = query.lower()

    def _is_bad(title: str, duration: int) -> bool:
        if duration and duration > MAX_DURATION:
            return True
        t = title.lower()
        if any(w in t for w in _skip_words):
            return True
        if "remix" not in q_lower and "remix" in t:
            return True
        return False

    def _make_track(info, src="youtube"):
        stream_url = info.get("url") or info.get("webpage_url", "")
        if "soundcloud" in info.get("extractor", "").lower():
            src = "soundcloud"
        elif "spotify" in query.lower():
            src = "spotify"
        return MusicTrack(
            title=info.get("title", "Unknown"),
            url=info.get("webpage_url", query),
            stream_url=stream_url,
            duration=int(info.get("duration", 0) or 0),
            requester=requester,
            source_type=src,
        )

    def _extract():
        import yt_dlp
        is_url = _re.match(r"https?://", query.strip())
        with yt_dlp.YoutubeDL(YTDL_OPTS) as ydl:
            if is_url:
                info = ydl.extract_info(query.strip(), download=False)
                return _make_track(info) if info else None
            search_q = query.strip()
            if "audio" not in search_q.lower() and "mv" not in search_q.lower():
                search_q += " audio"
            try:
                yt_results = ydl.extract_info(f"ytsearch5:{search_q}", download=False)
                entries = (yt_results or {}).get("entries") or []
            except Exception as e:
                log.warning("YouTube search failed for '%s': %s", search_q[:60], e)
                entries = []
            for entry in entries:
                if not entry:
                    continue
                if _is_bad(entry.get("title", ""), entry.get("duration", 0)):
                    continue
                try:
                    vid_url = entry.get("url") or entry.get("webpage_url")
                    if vid_url and not entry.get("url"):
                        entry = ydl.extract_info(vid_url, download=False)
                    if entry and entry.get("url"):
                        return _make_track(entry, "youtube")
                except Exception as e:
                    log.debug("YouTube entry failed (age-gate?): %s", str(e)[:100])
                    continue
            log.info("YouTube failed for '%s' — trying SoundCloud", query[:60])
            try:
                sc_results = ydl.extract_info(f"scsearch3:{query.strip()}", download=False)
                sc_entries = (sc_results or {}).get("entries") or []
                for entry in sc_entries:
                    if not entry:
                        continue
                    if _is_bad(entry.get("title", ""), entry.get("duration", 0)):
                        continue
                    return _make_track(entry, "soundcloud")
            except Exception as e:
                log.warning("SoundCloud fallback also failed: %s", str(e)[:100])
            return None

    try:
        return await asyncio.get_event_loop().run_in_executor(None, _extract)
    except Exception as e:
        log.warning("yt-dlp extraction failed for '%s': %s", query[:80], e)
        return None


# ── Playback ──────────────────────────────────────────────────────────────────

async def play_track(guild: discord.Guild, track: MusicTrack,
                     text_channel: Optional[discord.TextChannel] = None):
    """Play a track through a FRESH BloodMixerSource each time.

    Always creates a new mixer per track — never reuses a potentially dead one
    whose _stop_event may already be set, which would cause start_music() to
    exit immediately and produce silence.
    """
    vc = guild.voice_client
    if not vc or not vc.is_connected():
        return

    guild_id = str(guild.id)
    mq = get_music_queue(guild_id)
    mq.current = track

    try:
        from mixer import BloodMixerSource
        loop = asyncio.get_running_loop()

        # Stop and discard the old mixer — its _stop_event may already be set
        # from a previous stop_music() call, which would cause the new
        # start_music() reader thread to exit immediately (silent playback).
        old_mixer = mq._mixer
        if old_mixer:
            try:
                old_mixer.stop_music()
            except Exception:
                pass

        # Fresh mixer — _stop_event starts cleared, ready for a new stream
        mixer = BloodMixerSource(loop)
        mq._mixer = mixer

        # Stop whatever Discord is currently playing before attaching new mixer
        if vc.is_playing() or vc.is_paused():
            _stop_voice_playback(vc)
            await asyncio.sleep(0.05)

        async def on_track_end():
            await _play_next(guild, text_channel)

        # Start FFmpeg FIRST so it produces audio before Discord starts calling read()
        mixer.start_music(track.stream_url, volume=mq.volume, on_end=on_track_end)
        await asyncio.sleep(0.1)  # Give FFmpeg time to start and fill buffer
        _sync_web_mixer_music(guild)

        def _on_player_stop(error):
            def _log_result(future):
                try:
                    future.result()
                except Exception as exc:
                    log.warning("Player-stop sync failed for '%s': %s", track.title, exc)
            try:
                future = asyncio.run_coroutine_threadsafe(
                    _handle_player_stop(guild, track, text_channel, error),
                    loop,
                )
                future.add_done_callback(_log_result)
            except Exception as exc:
                log.warning("Failed to schedule player-stop sync for '%s': %s", track.title, exc)

        vc.play(mixer, after=_on_player_stop)

        if text_channel:
            dur = f" ({track.duration // 60}:{track.duration % 60:02d})" if track.duration else ""
            icon = {"youtube": "🔴", "spotify": "🟢", "soundcloud": "🟠"}.get(track.source_type, "🎵")
            try:
                await text_channel.send(
                    f"{icon} **Now playing:** {track.title}{dur} — requested by {track.requester}"
                )
            except Exception:
                pass

        log.info("Playing: %s (requested by %s)", track.title, track.requester)
    except Exception as e:
        log.warning("Failed to play track: %s", e)
        await _play_next(guild, text_channel)


async def _play_next(guild: discord.Guild,
                     text_channel: Optional[discord.TextChannel] = None):
    guild_id = str(guild.id)
    mq = get_music_queue(guild_id)
    nxt = mq.next()
    if nxt:
        await play_track(guild, nxt, text_channel)
    else:
        mq.current = None
        if mq._mixer:
            mq._mixer.stop_music()
        vc = guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            _stop_voice_playback(vc)
        mq._mixer = None
        _sync_web_mixer_music(guild)


async def _handle_player_stop(guild: discord.Guild, expected_track: MusicTrack,
                              text_channel: Optional[discord.TextChannel],
                              error: Optional[Exception]):
    await asyncio.sleep(0.1)
    guild_id = str(guild.id)
    mq = get_music_queue(guild_id)
    if mq.current is not expected_track:
        return
    mixer = mq._mixer
    vc = guild.voice_client
    if mixer and _voice_client_has_mixer(vc, mixer):
        return
    if error:
        log.warning("Voice player stopped for '%s': %s", expected_track.title, error)
    if mq.queue:
        log.warning("Voice player stopped mid-track on '%s' — advancing queue", expected_track.title)
        await _play_next(guild, text_channel)
        return
    log.warning("Voice player stopped mid-track on '%s' — clearing stale state", expected_track.title)
    mq.current = None
    if mixer and not mixer.has_music:
        mq._mixer = None
    _sync_web_mixer_music(guild)


async def play_music(guild: discord.Guild, query: str, requester: str = "",
                     text_channel: Optional[discord.TextChannel] = None,
                     requester_id: Optional[str] = None) -> str:
    guild_id = str(guild.id)
    vc = guild.voice_client
    if not vc or not vc.is_connected():
        return "❌ Not in a voice channel. Use /joinvc first."
    track = await extract_track(query, requester)
    if not track:
        return f"❌ Could not find anything for: {query}"
    if requester_id and track.title:
        record_feedback(requester_id, track.title, positive=True)
    mq = get_music_queue(guild_id)
    is_playing = has_active_music(guild)
    if is_playing:
        mq.add(track)
        pos = len(mq.queue)
        return f"📋 **Queued #{pos}:** {track.title}"
    else:
        await play_track(guild, track, text_channel)
        return f"🎵 **Playing:** {track.title}"


async def skip_music(guild: discord.Guild) -> str:
    guild_id = str(guild.id)
    mq = get_music_queue(guild_id)
    if not mq.current and (not mq._mixer or not mq._mixer.has_music):
        return "Nothing is playing."
    old_title = mq.current.title if mq.current else "track"
    if mq._mixer:
        mq._mixer.stop_music()
    nxt = mq.skip()
    if nxt:
        await play_track(guild, nxt)
    else:
        mq.current = None
        dj = get_random_dj(guild_id)
        if dj.is_active:
            log.info("Skip '%s' with active DJ — DJ loop will queue next", old_title)
        else:
            vc = guild.voice_client
            if vc and (vc.is_playing() or vc.is_paused()):
                _stop_voice_playback(vc)
            mq._mixer = None
        _sync_web_mixer_music(guild)
    log.info("Skipped '%s'", old_title)
    return "⏭️ Skipped."


async def stop_music(guild: discord.Guild) -> str:
    guild_id = str(guild.id)
    mq = get_music_queue(guild_id)
    mq.cleanup_mixer()
    mq.clear()
    vc = guild.voice_client
    if vc and (vc.is_playing() or vc.is_paused()):
        _stop_voice_playback(vc)
    _sync_web_mixer_music(guild)
    return "⏹️ Stopped and cleared queue."


def get_queue_info(guild_id: str, guild: Optional[discord.Guild] = None) -> str:
    mq = get_music_queue(guild_id)
    lines = []
    current = get_now_playing_track(guild) if guild else mq.current
    if current:
        dur = f" ({current.duration // 60}:{current.duration % 60:02d})" if current.duration else ""
        lines.append(f"▶️ **Now:** {current.title}{dur}")
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
    vol = max(0.0, min(1.0, vol))
    mq = get_music_queue(guild_id)
    mq.volume = vol
    if mq._mixer:
        mq._mixer.set_music_volume(vol)
    _update_web_mixer(
        music_level=vol if mq._mixer and mq._mixer.has_music and mq.current else 0.0,
        music_volume=vol,
    )
    return f"🔊 Volume set to {int(vol * 100)}%"


# ── Music Taste / Recommendation System ──────────────────────────────────────

TASTE_DIR = os.path.join(os.path.dirname(__file__), "data", "music_taste")
os.makedirs(TASTE_DIR, exist_ok=True)

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


_taste_embed_model = None

def _get_taste_model():
    global _taste_embed_model
    if _taste_embed_model is not None:
        return _taste_embed_model
    try:
        import memory
        mem = memory.Memory
        if hasattr(mem, '_embed_model') and mem._embed_model is not None:
            _taste_embed_model = mem._embed_model
            return _taste_embed_model
    except Exception:
        pass
    from sentence_transformers import SentenceTransformer
    _taste_embed_model = SentenceTransformer("all-MiniLM-L6-v2")
    return _taste_embed_model


def _is_taste_relevant(entry: str, existing_liked: list[str], threshold: float = 0.35) -> bool:
    if len(existing_liked) < 5:
        return True
    try:
        model = _get_taste_model()
        entry_emb = model.encode(entry)
        recent_liked = existing_liked[-20:]
        liked_embs = model.encode(recent_liked)
        import numpy as np
        sims = np.dot(liked_embs, entry_emb) / (
            np.linalg.norm(liked_embs, axis=1) * np.linalg.norm(entry_emb) + 1e-8
        )
        best = float(np.max(sims))
        log.debug("[TASTE] '%s' best similarity: %.3f (threshold: %.2f)", entry[:40], best, threshold)
        return best >= threshold
    except Exception as e:
        log.debug("[TASTE] Embedding check failed, accepting: %s", e)
        return True


def _has_repeated_pattern(entry: str, existing_liked: list[str], min_matches: int = 2) -> bool:
    entry_lower = entry.lower()
    artist = entry_lower.split(" - ")[0].strip() if " - " in entry_lower else ""
    if not artist or len(artist) < 2:
        return False
    count = sum(1 for s in existing_liked if artist in s.lower())
    return count >= min_matches


def record_feedback(user_id: str, track_title: str, positive: bool):
    data = _load_taste(user_id)
    entry = track_title.strip()
    if positive:
        if entry not in data["liked"]:
            if (_is_taste_relevant(entry, data["liked"])
                    or _has_repeated_pattern(entry, data["liked"])):
                data["liked"].append(entry)
                log.debug("[TASTE] Added to liked: %s", entry[:50])
            else:
                log.debug("[TASTE] Rejected from liked (not relevant): %s", entry[:50])
                return
        data["disliked"] = [d for d in data["disliked"] if d != entry]
    else:
        if entry not in data["disliked"]:
            data["disliked"].append(entry)
        data["liked"] = [l for l in data["liked"] if l != entry]
    data["liked"] = data["liked"][-100:]
    data["disliked"] = data["disliked"][-100:]
    _save_taste(user_id, data)


_rec_cache: dict[str, list[str]] = {}
_recent_played: dict[str, list[str]] = {}
RECENT_MAX = 30

_mood_cache: dict[str, dict] = {}
MOOD_CACHE_TTL = 600


async def _detect_mood(user_id: str, guild_id: str) -> str:
    from provider import call_ai
    cached = _mood_cache.get(user_id)
    if cached and time.time() - cached.get("timestamp", 0) < MOOD_CACHE_TTL:
        return cached["mood"]
    recent_chat = []
    for entry in _convo_buffer.get(guild_id, [])[-10:]:
        if entry.get("user_name") != "Blood":
            recent_chat.append(entry.get("text", ""))
    recent = _recent_played.get(user_id, [])[-5:]
    data = _load_taste(user_id)
    recent_skips = [d for d in data.get("disliked", [])[-5:] if d in recent]
    from datetime import datetime, timezone, timedelta
    local_hour = (datetime.now(timezone.utc) + timedelta(hours=7)).hour
    if 0 <= local_hour < 6:
        time_hint = "very late night / early morning"
    elif 6 <= local_hour < 12:
        time_hint = "morning"
    elif 12 <= local_hour < 17:
        time_hint = "afternoon"
    elif 17 <= local_hour < 21:
        time_hint = "evening"
    else:
        time_hint = "night"
    chat_text = "; ".join(recent_chat[-5:]) if recent_chat else "no recent chat"
    recent_text = ", ".join(recent) if recent else "none"
    skip_text = f"Recently skipped: {', '.join(recent_skips)}" if recent_skips else ""
    prompt = (
        f"Given this context, classify the user's current mood for music.\n"
        f"- Recent chat: {chat_text}\n"
        f"- Recently played songs: {recent_text}\n"
        f"- {skip_text}\n"
        f"- Time: {time_hint}\n\n"
        f"Reply with ONLY one word: chill, hype, sad, focus, angry, or neutral"
    )
    try:
        result = await call_ai(
            system="You classify a user's mood for music. Reply with ONLY one word.",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=10,
        )
        mood = result.get("message", {}).get("content", "neutral").strip().lower()
        if mood not in ("chill", "hype", "sad", "focus", "angry", "neutral"):
            mood = "neutral"
        log.info("Detected mood for user %s: %s", user_id, mood)
    except Exception as e:
        log.warning("Mood detection failed: %s", e)
        mood = "neutral"
    _mood_cache[user_id] = {"mood": mood, "timestamp": time.time()}
    return mood


async def _spotify_recommendations(user_id: str, guild_id: str, count: int = 15) -> list[str]:
    try:
        from spotify import resolve_spotify_ids, get_mood_recommendations
    except ImportError:
        return []
    data = _load_taste(user_id)
    liked = data.get("liked", [])[-10:]
    recent = _recent_played.get(user_id, [])[-15:]
    if not liked:
        return []
    mood = await _detect_mood(user_id, guild_id)
    seed_ids = await resolve_spotify_ids(liked[-5:])
    if not seed_ids:
        return []
    songs = await get_mood_recommendations(mood, seed_ids, count + 5)
    if not songs:
        return []
    filtered = [s for s in songs if s not in recent]
    log.info("Spotify mood=%s → %d recommendations (from %d)", mood, len(filtered), len(songs))
    return filtered[:count]


async def _ai_recommendations(user_id: str, count: int = 15) -> list[str]:
    import random
    from provider import call_ai
    data = _load_taste(user_id)
    liked = data.get("liked", [])[-20:]
    disliked = data.get("disliked", [])[-10:]
    recent = _recent_played.get(user_id, [])[-15:]
    if liked:
        taste_info = f"Songs they liked: {', '.join(liked)}"
    else:
        taste_info = "No listening history yet — suggest a diverse mix of popular and interesting songs across genres."
    dislike_info = f"\nSongs they DISLIKED (avoid similar): {', '.join(disliked)}" if disliked else ""
    recent_info = f"\nRecently played (DO NOT repeat these): {', '.join(recent)}" if recent else ""
    prompt = (
        f"You are a music recommendation engine like Spotify or YouTube Music.\n"
        f"Generate {count} song recommendations for this listener.\n\n"
        f"{taste_info}{dislike_info}{recent_info}\n\n"
        f"Rules:\n"
        f"- Output ONLY a list, one song per line, format: Artist - Song Title\n"
        f"- NO numbering, NO bullets, NO extra text\n"
        f"- Every song must be a REAL, existing song\n"
        f"- Mix: 60% similar to their taste, 30% discovery (new genres/artists), 10% wildcard\n"
        f"- NEVER repeat an artist more than twice\n"
        f"- NEVER include songs from the recently played list\n"
        f"- Prefer original recordings, not covers or live versions\n"
        f"- Include a good variety of energy levels (chill, upbeat, hype)\n"
    )
    try:
        result = await call_ai(
            system="You are a music recommendation engine. Output ONLY song names, one per line.",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=600,
        )
        text = result.get("message", {}).get("content", "")
        songs = []
        for line in text.strip().split("\n"):
            line = line.strip().lstrip("0123456789.-) •●").strip()
            if (line and 5 < len(line) < 100
                    and " - " in line
                    and not any(w in line.lower() for w in ("actually", "maybe", "could be", "let me", "i think", "note:", "here"))):
                songs.append(line)
        if songs:
            random.shuffle(songs)
            return songs
    except Exception as e:
        log.warning("AI recommendation failed: %s", e)
    fallback = [
        "Dua Lipa - Levitating", "The Weeknd - Blinding Lights",
        "Tame Impala - The Less I Know The Better", "Arctic Monkeys - Do I Wanna Know",
        "Billie Eilish - bad guy", "Harry Styles - As It Was",
        "Tyler The Creator - See You Again", "SZA - Kill Bill",
        "Olivia Rodrigo - good 4 u", "Post Malone - Circles",
        "Mac DeMarco - Chamber of Reflection", "Glass Animals - Heat Waves",
        "Doja Cat - Say So", "BTS - Dynamite", "BLACKPINK - How You Like That",
    ]
    import random
    random.shuffle(fallback)
    return [s for s in fallback if s not in recent][:count]


async def _generate_recommendations(user_id: str, count: int = 15, guild_id: str = "") -> list[str]:
    if guild_id:
        songs = await _spotify_recommendations(user_id, guild_id, count)
        if songs:
            return songs
        log.debug("Spotify recs unavailable — falling back to AI")
    return await _ai_recommendations(user_id, count)


LIKED_SONG_CHANCE = 0.30

async def get_next_song(user_id: str, guild_id: str = "") -> str:
    import random as _rng
    data = _load_taste(user_id)
    liked = data.get("liked", [])
    recent = _recent_played.get(user_id, [])
    if liked and _rng.random() < LIKED_SONG_CHANCE:
        available = [s for s in liked if s not in recent]
        if not available:
            available = liked
        song = _rng.choice(available)
        _track_played(user_id, song)
        log.debug("[DJ] Playing liked song: %s", song[:50])
        return song
    if user_id in _rec_cache and _rec_cache[user_id]:
        song = _rec_cache[user_id].pop(0)
        _track_played(user_id, song)
        return song
    songs = await _generate_recommendations(user_id, guild_id=guild_id)
    if not songs:
        return "popular music mix 2025"
    song = songs.pop(0)
    _rec_cache[user_id] = songs
    _track_played(user_id, song)
    return song


def _track_played(user_id: str, song: str):
    if user_id not in _recent_played:
        _recent_played[user_id] = []
    _recent_played[user_id].append(song)
    if len(_recent_played[user_id]) > RECENT_MAX:
        _recent_played[user_id] = _recent_played[user_id][-RECENT_MAX:]


# ── Random Music DJ System ────────────────────────────────────────────────────

class RandomDJ:
    def __init__(self, guild_id: str):
        self.guild_id = guild_id
        self.participants: list[str] = []
        self.user_names: dict[str, str] = {}
        self.active = False
        self._rotation_idx = 0
        self._priority_remaining = 0

    def add_user(self, user_id: str, display_name: str):
        if user_id not in self.participants:
            self.participants.append(user_id)
            self.user_names[user_id] = display_name

    def remove_user(self, user_id: str):
        if user_id in self.participants:
            self.participants.remove(user_id)
            self.user_names.pop(user_id, None)
            if not self.participants:
                self.active = False
                self._rotation_idx = 0
                self._priority_remaining = 0

    def get_next_user(self) -> Optional[str]:
        if not self.participants:
            return None
        n = len(self.participants)
        if n == 1:
            return self.participants[0]
        priority_user = self.participants[0]
        priority_count = n
        if self._priority_remaining > 0:
            self._priority_remaining -= 1
            return priority_user
        else:
            non_priority = self.participants[1:]
            if self._rotation_idx >= len(non_priority):
                self._rotation_idx = 0
                self._priority_remaining = priority_count - 1
                return priority_user
            else:
                user = non_priority[self._rotation_idx]
                self._rotation_idx += 1
                return user

    @property
    def is_active(self):
        return self.active and len(self.participants) > 0


_random_djs: dict[str, RandomDJ] = {}


def get_random_dj(guild_id: str) -> RandomDJ:
    if guild_id not in _random_djs:
        _random_djs[guild_id] = RandomDJ(guild_id)
    return _random_djs[guild_id]


async def start_random_music(guild: discord.Guild, user_id: str, display_name: str,
                             text_channel: Optional[discord.TextChannel] = None) -> str:
    guild_id = str(guild.id)
    dj = get_random_dj(guild_id)
    already_in = user_id in dj.participants
    dj.add_user(user_id, display_name)
    if already_in:
        return f"🎲 You're already in the DJ rotation! ({len(dj.participants)} participants)"
    if not dj.active:
        dj.active = True
        asyncio.create_task(_dj_loop(guild, text_channel))
        return f"🎲 **Random DJ started!** Playing music based on your taste. Say 'I like this' or 'skip' to teach me."
    else:
        pos = dj.participants.index(user_id) + 1
        priority_name = dj.user_names.get(dj.participants[0], "???")
        return (f"🎲 Joined DJ rotation! Position #{pos}/{len(dj.participants)}. "
                f"Priority: {priority_name} (plays {len(dj.participants)} songs per cycle)")


async def stop_random_music(guild_id: str, user_id: str) -> str:
    dj = get_random_dj(guild_id)
    if user_id not in dj.participants:
        return "You're not in the DJ rotation."
    dj.remove_user(user_id)
    if not dj.participants:
        dj.active = False
        return "🎲 DJ stopped — no participants left."
    return f"🎲 Left DJ rotation. {len(dj.participants)} still going."


async def _dj_loop(guild: discord.Guild, text_channel: Optional[discord.TextChannel]):
    guild_id = str(guild.id)
    dj = get_random_dj(guild_id)
    mq = get_music_queue(guild_id)
    while dj.is_active:
        vc = guild.voice_client
        if not vc or not vc.is_connected():
            dj.active = False
            break
        mixer_busy = has_active_music(guild)
        current_track = get_now_playing_track(guild)
        if not mixer_busy and not current_track:
            next_user = dj.get_next_user()
            if not next_user:
                await asyncio.sleep(2)
                continue
            query = await get_next_song(next_user, guild_id=guild_id)
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
        await asyncio.sleep(3)


def cleanup_user_from_dj(guild_id: str, user_id: str):
    dj = get_random_dj(guild_id)
    dj.remove_user(user_id)
    if not dj.participants:
        dj.active = False


# ── Text+VC Dual Reply Support ────────────────────────────────────────────────

async def speak_in_vc(guild: discord.Guild, text: str, bot_instance):
    if not guild.voice_client or not guild.voice_client.is_connected():
        return
    guild_id = str(guild.id)
    sink = _active_sinks.get(guild_id)
    if not sink:
        return
    await sink._speak(text)


def is_user_in_blood_vc(guild: discord.Guild, user) -> bool:
    if not guild.voice_client or not guild.voice_client.channel:
        return False
    return user in guild.voice_client.channel.members


# ── Transcript Storage ────────────────────────────────────────────────────────

_transcripts: dict[str, list[dict]] = defaultdict(list)
_vc_sessions: dict[str, list[dict]] = defaultdict(list)
_active_sessions: dict[str, dict] = {}
_convo_buffer: dict[str, list[dict]] = defaultdict(list)
CONVO_BUFFER_SIZE = 20
_active_conversation: dict[str, set] = defaultdict(set)
_conversation_timeout = 30

MAX_BUFFER_BYTES = SAMPLE_RATE * CHANNELS * 2 * 10
MIN_STT_BYTES = SAMPLE_RATE * CHANNELS * 2 * 0.5


class UserAudioBuffer:
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


def pcm_to_wav(pcm_data: bytes, sample_rate: int = SAMPLE_RATE,
               channels: int = CHANNELS) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_data)
    return buf.getvalue()


async def transcribe_audio(pcm_data: bytes) -> Optional[str]:
    if not GROQ_API_KEY:
        log.warning("No GROQ_API_KEY — cannot transcribe")
        return None
    loop = asyncio.get_event_loop()
    wav_data = await loop.run_in_executor(None, pcm_to_wav, pcm_data)
    if len(wav_data) < 1024:
        return None
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    form = aiohttp.FormData()
    form.add_field("file", wav_data, filename="audio.wav", content_type="audio/wav")
    form.add_field("model", GROQ_STT_MODEL)
    form.add_field("response_format", "json")
    form.add_field("language", "th")
    try:
        session = _get_stt_session()
        async with session.post(GROQ_STT_URL, headers=headers, data=form) as resp:
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


async def text_to_speech(text: str, voice: str = TTS_VOICE) -> Optional[bytes]:
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
    def __init__(self, mp3_data: bytes):
        self._process = None
        self._mp3_data = mp3_data
        self._stdout = None

    async def start(self):
        self._process = await asyncio.create_subprocess_exec(
            "ffmpeg", "-i", "pipe:0", "-f", "s16le", "-ar", "48000",
            "-ac", "2", "-loglevel", "quiet", "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self._process.stdin.write(self._mp3_data)
        await self._process.stdin.drain()
        self._process.stdin.close()
        self._stdout = self._process.stdout

    def read(self) -> bytes:
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


def contains_wake_word(text: str) -> bool:
    lower = text.lower()
    return any(w in lower for w in WAKE_WORDS)


def is_relevant_to_conversation(text: str, guild_id: str) -> bool:
    if contains_wake_word(text):
        return True
    guild_convos = _active_conversation.get(guild_id)
    if guild_convos:
        return True
    return False


TRANSCRIPT_DIR = os.path.join(os.path.dirname(__file__), "data", "transcripts")
os.makedirs(TRANSCRIPT_DIR, exist_ok=True)


def add_transcript_entry(guild_id: str, user_id: int, user_name: str,
                         text: str, channel_name: str):
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "user_id": user_id,
        "user_name": user_name,
        "text": text,
        "channel_name": channel_name,
    }
    _transcripts[guild_id].append(entry)
    _convo_buffer[guild_id].append(entry)
    if len(_convo_buffer[guild_id]) > CONVO_BUFFER_SIZE:
        _convo_buffer[guild_id] = _convo_buffer[guild_id][-CONVO_BUFFER_SIZE:]
    if guild_id in _active_sessions:
        _active_sessions[guild_id]["transcript"].append(entry)


def start_vc_session(guild_id: str, channel_name: str):
    session = {
        "channel_name": channel_name,
        "joined_at": datetime.now(timezone.utc).isoformat(),
        "left_at": None,
        "transcript": [],
    }
    _active_sessions[guild_id] = session


def end_vc_session(guild_id: str):
    if guild_id not in _active_sessions:
        return
    session = _active_sessions.pop(guild_id)
    session["left_at"] = datetime.now(timezone.utc).isoformat()
    _vc_sessions[guild_id].append(session)
    if len(_vc_sessions[guild_id]) > 50:
        _vc_sessions[guild_id] = _vc_sessions[guild_id][-50:]
    _save_session(guild_id, session)


def _save_session(guild_id: str, session: dict):
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
    sessions = list(_vc_sessions.get(guild_id, []))
    guild_dir = os.path.join(TRANSCRIPT_DIR, guild_id)
    if os.path.isdir(guild_dir):
        files = sorted(
            [f for f in os.listdir(guild_dir) if f.endswith(".json")],
            reverse=True
        )
        for fname in files[:limit * 2]:
            path = os.path.join(guild_dir, fname)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    s = json.load(f)
                if not any(existing.get("joined_at") == s.get("joined_at") for existing in sessions):
                    sessions.append(s)
            except Exception:
                pass
    sessions.sort(key=lambda s: s.get("joined_at", ""), reverse=True)
    return sessions[:limit]


def format_transcript_txt(session: dict) -> str:
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
    transcript = session.get("transcript", [])
    if not transcript:
        return "No speech was transcribed during this session."
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
        self._process_task = asyncio.create_task(self._process_loop())

    async def _process_loop(self):
        while self._running:
            try:
                await asyncio.sleep(1.0)
                now = time.monotonic()
                for uid, buf in list(self.user_buffers.items()):
                    if buf.is_speaking and buf.silence_duration() > SILENCE_THRESHOLD_SEC:
                        pcm = buf.harvest()
                        if pcm and len(pcm) >= int(MIN_STT_BYTES):
                            if now - self._last_stt_time < 2.0:
                                continue
                            if not self._stt_lock.locked():
                                self._last_stt_time = now
                                asyncio.create_task(self._guarded_handle(uid, buf.user_name, pcm))
                    elif buf.is_speaking and buf.speech_duration() > MAX_SPEECH_SEC:
                        pcm = buf.harvest()
                        if pcm and len(pcm) >= int(MIN_STT_BYTES):
                            if not self._stt_lock.locked():
                                self._last_stt_time = now
                                asyncio.create_task(self._guarded_handle(uid, buf.user_name, pcm))
                    elif not buf.is_speaking and buf.silence_duration() > 10:
                        buf.clear()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning("Voice process loop error: %s", e)

    async def _guarded_handle(self, user_id: int, user_name: str, pcm_data: bytes):
        async with self._stt_lock:
            await self._handle_speech(user_id, user_name, pcm_data)

    @staticmethod
    def _is_junk(text: str) -> bool:
        import re as _re
        t = text.strip()
        if len(t) < 2:
            return True
        if _re.search(r"https?://\S+", t):
            return True
        cleaned = _re.sub(r"[\U0001F000-\U0001FFFF\u2600-\u27BF\u2700-\u27BF\uFE00-\uFE0F\u200D]", "", t)
        cleaned = _re.sub(r"<a?:\w+:\d+>", "", cleaned)
        if not cleaned.strip():
            return True
        if t.startswith(("/", "!", ".", "?", "$")):
            return True
        hallucinations = [
            "thank you", "thanks for watching", "subscribe", "like and subscribe",
            "you", "bye", ".", "...", "ขอบคุณ", "สวัสดีครับ", "สวัสดีค่ะ",
            "ฝากกดไลค์", "ฝากกดติดตาม", "♪", "🎵",
        ]
        if t.lower().strip(".!? ") in hallucinations:
            return True
        return False

    async def _handle_speech(self, user_id: int, user_name: str, pcm_data: bytes):
        text = await transcribe_audio(pcm_data)
        if not text:
            return
        if self._is_junk(text):
            log.debug("[VC STT] Ignored junk: %s", text[:50])
            return
        log.info("[VC STT] %s: %s", user_name, text)
        add_transcript_entry(self.guild_id, user_id, user_name, text, self.channel_name)
        has_wake = contains_wake_word(text)
        is_relevant = is_relevant_to_conversation(text, self.guild_id)
        if has_wake or is_relevant:
            _active_conversation[self.guild_id] = set()
            response = await self._generate_response(user_id, user_name, text)
            if response:
                add_transcript_entry(
                    self.guild_id, self.bot.user.id, "Blood",
                    response, self.channel_name
                )
                await self._speak(response)
                if self.text_channel:
                    try:
                        await self.text_channel.send(
                            f"🎙️ **{user_name}**: {text}\n💬 **Blood**: {response}"
                        )
                    except Exception:
                        pass
                asyncio.create_task(self._conversation_timeout())

    async def _generate_response(self, user_id: int, user_name: str, text: str) -> Optional[str]:
        music_result = await self._check_music_request(text, user_name)
        if music_result:
            return music_result
        try:
            from provider import call_ai
            from config import CONFIG
            system = (
                "You are Blood, in a voice channel. RULES:\n"
                "- MAX 1-2 sentences. You're speaking aloud, not typing.\n"
                "- No markdown, no emojis, no formatting. Plain speech only.\n"
                "- Be witty but brief. Don't explain jokes. Don't ramble.\n"
                "- If someone asks to play music, just say 'on it' or 'sure' — the music system handles the rest.\n"
                "- You still have all your normal tools and can use them.\n"
                f"Speaker: {user_name}"
            )
            context_msgs = []
            for entry in _convo_buffer.get(self.guild_id, [])[-10:]:
                role = "assistant" if entry.get("user_name") == "Blood" else "user"
                prefix = "" if role == "assistant" else f"[{entry.get('user_name', '?')}] "
                context_msgs.append({"role": role, "content": f"{prefix}{entry.get('text', '')}"})
            if not context_msgs:
                context_msgs = [{"role": "user", "content": f"[{user_name}] {text}"}]
            result = await call_ai(system, context_msgs, max_tokens=150)
            content = result.get("message", {}).get("content", "").strip()
            return content if content else None
        except Exception as e:
            log.warning("Voice response generation failed: %s", e)
            return None

    async def _classify_music_intent(self, text: str) -> dict:
        from provider import call_ai
        try:
            result = await call_ai(
                system=(
                    "You classify user speech into music intents. Reply with ONLY one word from this list:\n"
                    "like - user enjoys the current song (e.g. 'this slaps', 'banger', 'ดี', 'เพราะ')\n"
                    "dislike - user dislikes current song (e.g. 'this is mid', 'nah', 'ไม่ชอบ', 'ห่วย', 'change it')\n"
                    "skip - user wants next song (e.g. 'next', 'skip this', 'ข้าม')\n"
                    "stop - user wants music to stop (e.g. 'turn it off', 'stop', 'หยุดเพลง')\n"
                    "random - user wants random/surprise music (e.g. 'play something', 'surprise me', 'เปิดอะไรก็ได้')\n"
                    "queue - user asks what's playing (e.g. 'what song is this', 'เพลงอะไร')\n"
                    "play:QUERY - user wants a specific song (e.g. 'play Blinding Lights' → 'play:Blinding Lights')\n"
                    "none - not music related at all\n\n"
                    "Reply with ONLY the intent. For play, include the query after colon."
                ),
                messages=[{"role": "user", "content": text}],
                max_tokens=30,
            )
            reply = result.get("message", {}).get("content", "none").strip().lower()
            if reply.startswith("play:"):
                return {"intent": "play", "query": reply[5:].strip()}
            return {"intent": reply if reply in ("like", "dislike", "skip", "stop", "random", "queue", "none") else "none"}
        except Exception as e:
            log.warning("Music intent classification failed: %s", e)
            return {"intent": "none"}

    async def _check_music_request(self, text: str, requester: str) -> Optional[str]:
        import re as _re
        url_match = _re.search(r"(https?://\S+)", text)
        guild = self.bot.get_guild(int(self.guild_id))
        if not guild:
            return None
        current_track = get_now_playing_track(guild)
        has_music = current_track is not None
        requester_id = None
        for uid, buf in self.user_buffers.items():
            if buf.user_name == requester:
                requester_id = str(uid)
                break
        if url_match:
            result = await play_music(guild, url_match.group(1), requester, self.text_channel,
                                      requester_id=requester_id)
            return result
        _music_hints = {"play", "song", "music", "skip", "stop", "next", "like", "dislike",
                        "hate", "love", "sucks", "banger", "random", "queue", "playing",
                        "เพลง", "เปิด", "ข้าม", "หยุด", "ชอบ", "ไม่ชอบ", "ห่วย", "เปลี่ยน", "เพราะ", "ดี"}
        lower = text.lower()
        might_be_music = has_music or any(w in lower for w in _music_hints)
        if not might_be_music:
            return None
        intent_data = await self._classify_music_intent(text)
        intent = intent_data.get("intent", "none")
        if intent == "like" and current_track and requester_id:
            record_feedback(requester_id, current_track.title, positive=True)
            return f"Noted, you like {current_track.title}. I'll remember that."
        if intent == "dislike" and current_track and requester_id:
            record_feedback(requester_id, current_track.title, positive=False)
            await skip_music(guild)
            return "Got it, skipping. I'll play less of that for you."
        if intent == "skip":
            return await skip_music(guild)
        if intent == "stop":
            return await stop_music(guild)
        if intent == "queue":
            return get_queue_info(self.guild_id, guild)
        if intent == "random" and requester_id:
            return await start_random_music(guild, requester_id, requester, self.text_channel)
        if intent == "play":
            query = intent_data.get("query", "")
            if query:
                return await play_music(guild, query, requester, self.text_channel,
                                        requester_id=requester_id)
        return None

    async def _speak(self, text: str):
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
        mixer_active = mixer and (mixer.has_music or _voice_client_has_mixer(vc, mixer))
        if mixer_active:
            try:
                _sync_web_mixer_tts(guild, active=True, text=text, level=1.0)
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, mixer.feed_tts_sync, mp3_data)
                log.info("Spoke in VC (mixed with music): %s", text[:60])
            except Exception as e:
                log.warning("TTS via mixer failed: %s", e)
            finally:
                _sync_web_mixer_tts(guild, active=False, text="", level=0.0)
        else:
            try:
                if vc.is_playing() or vc.is_paused():
                    _stop_voice_playback(vc)
                source = FFmpegPCMAudioPipe(mp3_data)
                source.prepare()
                _sync_web_mixer_tts(guild, active=True, text=text, level=1.0)
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
            finally:
                _sync_web_mixer_tts(guild, active=False, text="", level=0.0)

    async def _conversation_timeout(self):
        await asyncio.sleep(_conversation_timeout)
        if self.guild_id in _active_conversation:
            _active_conversation.pop(self.guild_id, None)

    _voice_data_count = 0

    def on_voice_data(self, user, pcm_data: bytes):
        if user.bot:
            return
        uid = user.id
        if uid not in self.user_buffers:
            self.user_buffers[uid] = UserAudioBuffer(uid, user.display_name)
            log.info("[VC] New speaker detected: %s (ID:%s)", user.display_name, uid)
        self.user_buffers[uid].add_pcm(pcm_data)
        self._voice_data_count += 1
        if self._voice_data_count in (1, 50, 500):
            log.info("[VC] Voice packets received: %d (from %s, %d bytes)",
                     self._voice_data_count, user.display_name, len(pcm_data))

    def stop(self):
        self._running = False
        if self._process_task:
            self._process_task.cancel()


# ── Active Sinks Registry ─────────────────────────────────────────────────────

_active_sinks: dict[str, BloodAudioSink] = {}
_intentional_leave: set[str] = set()


def get_active_sink(guild_id: str) -> Optional[BloodAudioSink]:
    return _active_sinks.get(guild_id)


async def join_and_listen(guild: discord.Guild, vc_channel: discord.VoiceChannel,
                          text_channel: Optional[discord.TextChannel], bot_instance) -> str:
    guild_id = str(guild.id)
    try:
        import discord.ext.voice_recv as voice_recv
    except ImportError:
        return "❌ Voice receive not available (discord-ext-voice-recv not installed)"
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
    for _ in range(20):
        if voice_client.is_connected():
            break
        await asyncio.sleep(0.2)
    else:
        return "❌ Voice connection timed out"
    sink = BloodAudioSink(guild_id, vc_channel.name, bot_instance, text_channel)
    _active_sinks[guild_id] = sink
    start_vc_session(guild_id, vc_channel.name)
    def callback(user, data: voice_recv.VoiceData):
        sink.on_voice_data(user, data.pcm)
    try:
        voice_client.listen(voice_recv.BasicSink(callback))
        log.info("[VC] Listener started on '%s' (voice_recv.BasicSink)", vc_channel.name)
    except Exception as e:
        log.error("[VC] Failed to start listener: %s", e)
        return f"❌ Failed to start listening: {e}"
    sink.start_processing()
    try:
        import web_mixer as wm
        await wm.start_web_mixer()
        _bind_web_mixer_guild(guild_id)
        wm.update_state(
            music_active=False,
            music_title="",
            music_level=0.0,
            music_volume=get_music_queue(guild_id).volume,
            tts_active=False,
            tts_text="",
            tts_level=0.0,
            ducked=False,
            force_duck=False,
        )
    except Exception as e:
        log.warning("Web mixer not started: %s", e)
    log.info("Joined VC '%s' in %s — listening", vc_channel.name, guild.name)
    return f"✅ Joined **{vc_channel.name}** — listening & recording. Say 'Blood' or 'hey Blood' to talk to me!"


async def leave_voice(guild: discord.Guild) -> str:
    guild_id = str(guild.id)
    _intentional_leave.add(guild_id)
    sink = _active_sinks.pop(guild_id, None)
    if sink:
        sink.stop()
    dj = get_random_dj(guild_id)
    if dj.is_active:
        dj.active = False
        dj.participants.clear()
    mq = get_music_queue(guild_id)
    mq.cleanup_mixer()
    mq.clear()
    end_vc_session(guild_id)
    if guild.voice_client:
        await guild.voice_client.disconnect(force=True)
    _active_conversation.pop(guild_id, None)
    _update_web_mixer(
        music_active=False,
        music_title="",
        music_level=0.0,
        music_volume=mq.volume,
        tts_active=False,
        tts_text="",
        tts_level=0.0,
        ducked=False,
        force_duck=False,
    )
    _bind_web_mixer_guild(None)
    async def _clear_flag():
        await asyncio.sleep(5)
        _intentional_leave.discard(guild_id)
    asyncio.create_task(_clear_flag())
    return "✅ Left voice channel. Transcript saved."
