
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

# Ensure opus is loaded — required for voice_recv to decode incoming audio
if not discord.opus.is_loaded():
    try:
        discord.opus._load_default()
        log.info("Opus loaded: %s", discord.opus.is_loaded())
    except Exception as e:
        log.warning("Failed to load opus: %s — voice receive won't work", e)

# ── Resilient opus decode (voice RECEIVE only) ───────────────────────────────
# discord-ext-voice-recv runs the opus Decoder inside its PacketRouter thread.
# If decode() raises (e.g. OpusError "corrupted stream" on a malformed/edge
# packet), that exception bubbles out of the router loop whose finally-block
# calls stop_listening() — permanently killing reception for the whole session,
# so transcription dies silently after ONE bad packet. Wrap the class-level
# decode so a bad frame is skipped (returns 20ms of silence) instead of tearing
# the listener down. The ok/fail counters tell us, in the logs, whether bad
# frames are occasional (transcription still works) or universal (a deeper
# decrypt/library-version issue). This does NOT affect music playback, which
# goes FFmpeg -> PCM and never touches the opus Decoder.
_opus_decode_ok = [0]
_opus_decode_fail = [0]
try:
    _OpusDecoder = discord.opus.Decoder
    _orig_opus_decode = _OpusDecoder.decode
    _SILENT_OPUS_FRAME = b"\x00" * (_OpusDecoder.SAMPLES_PER_FRAME * _OpusDecoder.CHANNELS * 2)

    def _safe_opus_decode(self, data, *args, **kwargs):
        try:
            pcm = _orig_opus_decode(self, data, *args, **kwargs)
            _opus_decode_ok[0] += 1
            return pcm
        except Exception as e:
            _opus_decode_fail[0] += 1
            n = _opus_decode_fail[0]
            if n in (1, 5, 25, 100) or n % 500 == 0:
                log.warning("[VC] opus decode failed (#%d, decoded_ok=%d): %s — skipping frame "
                            "to keep the listener alive", n, _opus_decode_ok[0], e)
            return _SILENT_OPUS_FRAME

    _OpusDecoder.decode = _safe_opus_decode
    log.info("[VC] Resilient opus decoder installed (corrupt frames skipped, listener survives)")
except Exception as e:
    log.warning("[VC] Could not install resilient opus decoder: %s", e)

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
logging.getLogger("discord.ext.voice_recv.router").setLevel(logging.ERROR)
# Reader at ERROR to catch CryptoError / decryption failures (not DEBUG to avoid spam)
logging.getLogger("discord.ext.voice_recv.reader").setLevel(logging.ERROR)

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_STT_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_STT_MODEL = "whisper-large-v3-turbo"

# Master switch for speech-to-text / VC transcription. When false, the bot still
# joins voice for TTS and music but does NOT attach the audio receiver, so no
# opus decoding or transcription happens. Flip STT_ENABLED=true in .env to re-enable.
STT_ENABLED = os.getenv("STT_ENABLED", "true").lower() in ("1", "true", "yes", "on")

WAKE_WORDS = ["blood", "hey blood", "เลือด", "บลัด"]

TTS_VOICE = "en-US-GuyNeural"
TTS_RATE = "+10%"

SAMPLE_RATE = 48000
CHANNELS = 2
SILENCE_THRESHOLD_SEC = 1.5
MAX_SPEECH_SEC = 15  # cap continuous speech before forcing a harvest/transcribe
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
    __slots__ = ("title", "url", "stream_url", "duration", "requester",
                 "source_type", "thumbnail")

    def __init__(self, title: str, url: str, stream_url: str,
                 duration: int = 0, requester: str = "", source_type: str = "youtube",
                 thumbnail: str = ""):
        self.title = title
        self.url = url
        self.stream_url = stream_url
        self.duration = duration
        self.requester = requester
        self.source_type = source_type
        self.thumbnail = thumbnail

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
        self.eq_gains = [0.0] * 10  # 10-band EQ, persists across tracks (web mixer)

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
        mq = MusicQueue(guild_id)
        mq.eq_gains = _get_saved_eq(guild_id)  # restore EQ across restarts/hotreload
        _music_queues[guild_id] = mq
    return _music_queues[guild_id]


# ── EQ persistence (survives hotreload / restart / long offline) ──────────────
EQ_SETTINGS_PATH = os.path.join(os.path.dirname(__file__), "data", "eq_settings.json")
_eq_settings_cache: Optional[dict] = None


def _load_eq_settings() -> dict:
    global _eq_settings_cache
    if _eq_settings_cache is not None:
        return _eq_settings_cache
    try:
        with open(EQ_SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    _eq_settings_cache = data
    return data


def _get_saved_eq(guild_id: str) -> list:
    g = _load_eq_settings().get(str(guild_id))
    if isinstance(g, list) and len(g) == 10:
        try:
            return [float(x) for x in g]
        except Exception:
            pass
    return [0.0] * 10


def _save_eq_to_disk(guild_id: str, gains: list):
    data = _load_eq_settings()
    if any(abs(float(x)) > 0.05 for x in gains):
        data[str(guild_id)] = [round(float(x), 2) for x in gains]
    else:
        data.pop(str(guild_id), None)  # flat EQ → drop the entry (stays clean)
    try:
        os.makedirs(os.path.dirname(EQ_SETTINGS_PATH), exist_ok=True)
        tmp = EQ_SETTINGS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, EQ_SETTINGS_PATH)  # atomic — never leaves a half-written file
    except Exception as e:
        log.debug("[EQ] save failed: %s", e)


def apply_eq(guild_id: str, gains) -> list:
    """Single entry point for changing the EQ: normalize to 10 bands, set on the
    queue (persists across tracks), apply to the live mixer if any, and SAVE to disk
    so it survives a hotreload/restart. Returns the normalized gains."""
    g = [max(-15.0, min(15.0, float(x))) for x in list(gains)[:10]]
    g += [0.0] * (10 - len(g))
    mq = get_music_queue(guild_id)
    mq.eq_gains = g
    if mq._mixer:
        try:
            mq._mixer.set_eq_gains(g)
        except Exception:
            pass
    _save_eq_to_disk(guild_id, g)
    return g


def _bind_web_mixer_guild(guild_id: Optional[str], guild_name: Optional[str] = None):
    try:
        import web_mixer as wm
        wm.bind_guild(guild_id, name=guild_name)
    except Exception:
        pass


def _unbind_web_mixer_guild(guild_id: str):
    try:
        import web_mixer as wm
        wm.unbind_guild(guild_id)
    except Exception:
        pass


def _update_web_mixer(guild_id: Optional[str] = None, **kwargs):
    try:
        import web_mixer as wm
        wm.update_state(guild_id=guild_id, **kwargs)
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
    _bind_web_mixer_guild(guild_id, guild_name=guild.name)
    mq = get_music_queue(guild_id)
    mixer = mq._mixer
    current = get_now_playing_track(guild)
    music_active = current is not None
    force_duck = bool(mixer.force_duck) if mixer else False
    ducked = bool(mixer and mixer.has_music and mixer.is_ducking)
    rdj = get_radio_dj(guild_id)
    queue_titles = [t.title for t in mq.queue[:20]]
    effects = None
    if mixer:
        effects = {"speed": mixer._speed, "bass_db": mixer._bass_db,
                   "treble_db": mixer._treble_db, "effect": mixer._effect}
    vc = guild.voice_client
    paused = bool(vc and vc.is_paused()) if vc else False
    _update_web_mixer(
        guild_id,
        music_active=music_active,
        music_title=current.title if current else "",
        music_level=mq.volume if music_active else 0.0,
        music_volume=mq.volume,
        ducked=ducked,
        force_duck=force_duck,
        queue=queue_titles,
        loop=mq.loop,
        paused=paused,
        radio_active=rdj.is_active,
        effects=effects,
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
        guild_id,
        tts_active=active,
        tts_text=text,
        tts_level=level,
        ducked=ducked,
        force_duck=force_duck,
    )


def _periodic_web_mixer_sync(guild: Optional[discord.Guild]):
    """Push the mixer's actual ducking/tts state to the web UI.

    Called every few seconds from the radio loop so that even if a point-in-time
    push was missed (exception, race, network blip), the web UI self-heals.
    """
    if not guild:
        return
    guild_id = str(guild.id)
    mq = get_music_queue(guild_id)
    mixer = mq._mixer
    if not mixer:
        _update_web_mixer(guild_id, ducked=False, force_duck=False, tts_active=False, tts_text="", tts_level=0.0)
        return
    ducked = bool(mixer.has_music and mixer.is_ducking)
    force_duck = bool(mixer.force_duck)
    tts_active = bool(mixer.tts_active)
    _update_web_mixer(
        guild_id,
        ducked=ducked,
        force_duck=force_duck,
        tts_active=tts_active,
        tts_level=1.0 if tts_active else 0.0,
    )


def _deezer_search(query: str) -> Optional[dict]:
    """Resolve a query to a canonical track via Deezer's free public API (no auth).

    Returns {title, artist, duration, cover} or None. Deezer's own audio is DRM-locked
    and not streamable (yt-dlp has no Deezer extractor), so this is metadata-only — it's
    used to find the right YouTube audio. Runs inside extract_track's executor thread.
    """
    import urllib.request, urllib.parse
    try:
        url = "https://api.deezer.com/search?limit=5&q=" + urllib.parse.quote(query)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.load(r)
    except Exception as e:
        log.debug("Deezer search failed for '%s': %s", query[:50], e)
        return None
    for t in (data.get("data") or []):
        title = (t.get("title") or "").strip()
        artist = ((t.get("artist") or {}).get("name") or "").strip()
        if title and artist:
            album = t.get("album") or {}
            return {
                "title": title,
                "artist": artist,
                "duration": int(t.get("duration") or 0),
                "cover": album.get("cover_big") or album.get("cover_medium") or "",
            }
    return None


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
        # Cover art — prefer an explicit thumbnail, else the largest from the list
        thumb = info.get("thumbnail") or ""
        if not thumb:
            thumbs = info.get("thumbnails") or []
            if thumbs:
                try:
                    thumb = max(
                        thumbs,
                        key=lambda t: (t.get("preference", 0), t.get("width", 0) or 0),
                    ).get("url", "")
                except Exception:
                    thumb = (thumbs[-1] or {}).get("url", "")
        return MusicTrack(
            title=info.get("title", "Unknown"),
            url=info.get("webpage_url", query),
            stream_url=stream_url,
            duration=int(info.get("duration", 0) or 0),
            requester=requester,
            source_type=src,
            thumbnail=thumb,
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
            # YouTube's own search came up empty. Deezer audio can't be streamed (DRM,
            # no yt-dlp extractor), so use its clean catalog to resolve the *real* track
            # + duration, then pull matching audio from YouTube. (Replaces SoundCloud.)
            dz = _deezer_search(query.strip())
            if dz:
                clean_q = f"{dz['artist']} - {dz['title']}"
                log.info("Deezer resolved '%s' → '%s' (%ds) — re-searching YouTube",
                         query[:50], clean_q, dz["duration"])
                try:
                    yt2 = ydl.extract_info(f"ytsearch5:{clean_q} audio", download=False)
                    cand = [e for e in ((yt2 or {}).get("entries") or [])
                            if e and not _is_bad(e.get("title", ""), e.get("duration", 0))]
                except Exception as e:
                    log.debug("Deezer→YouTube re-search failed: %s", str(e)[:100])
                    cand = []
                # Prefer the YouTube result whose length matches Deezer's known
                # duration — this is what kills 10-hour loops / wrong-version junk.
                target = dz["duration"]
                cand.sort(key=lambda e: abs((e.get("duration") or 0) - target)
                          if (e.get("duration") and target) else 9999)
                for entry in cand:
                    try:
                        vurl = entry.get("url") or entry.get("webpage_url")
                        if vurl and not entry.get("url"):
                            entry = ydl.extract_info(vurl, download=False)
                        if entry and entry.get("url"):
                            track = _make_track(entry, "youtube")
                            track.source_type = "deezer"  # resolved via Deezer
                            if not track.thumbnail and dz.get("cover"):
                                track.thumbnail = dz["cover"]
                            return track
                    except Exception:
                        continue
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
        # Re-apply any EQ the user dialed in so it persists across tracks
        try:
            if any(abs(g) > 0.1 for g in mq.eq_gains):
                mixer.set_eq_gains(mq.eq_gains)
        except Exception:
            pass

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

        # When radio is driving via its player panel, the panel is the now-playing
        # display — skip the redundant text line (and the extra panel repost it causes).
        _radio_has_panel = False
        try:
            import radio_panel
            _radio_has_panel = radio_panel.get_panel(guild_id) is not None
        except Exception:
            _radio_has_panel = False

        if text_channel and not _radio_has_panel:
            dur = f" ({track.duration // 60}:{track.duration % 60:02d})" if track.duration else ""
            icon = {"youtube": "🔴", "spotify": "🟢", "soundcloud": "🟠",
                    "deezer": "🔵"}.get(track.source_type, "🎵")
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

    # Radio mode: gapless — reuse the same mixer, just swap the stream
    rdj = get_radio_dj(guild_id)
    if rdj.is_active:
        if mq._mixer and mq.queue:
            nxt = mq.next()
            if nxt:
                mixer = mq._mixer

                async def _on_end():
                    await _play_next(guild, text_channel)

                mixer.start_music(nxt.stream_url, volume=mq.volume, on_end=_on_end)
                # Refill queue and prebuffer next track immediately in background
                asyncio.create_task(_fill_radio_queue(rdj, guild_id))
                _sync_web_mixer_music(guild)
                log.info("[RADIO] Gapless transition: %s", nxt.title)
                return
        # Radio active but queue empty — stop the mixer so has_active_music() returns
        # False, which lets _radio_loop's "not playing + queue ready" branch restart
        # playback as soon as _fill_radio_queue adds songs.
        mq.current = None
        if mq._mixer:
            mq._mixer.stop_music()
            mq._mixer = None
        log.info("[RADIO] Queue empty — stopping mixer, triggering fill")
        asyncio.create_task(_fill_radio_queue(rdj, guild_id))
        _sync_web_mixer_music(guild)
        return

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
        # Run taste feedback in executor to avoid blocking event loop
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, record_feedback, requester_id, track.title, True)
        except Exception as e:
            log.debug("Feedback recording failed: %s", e)
    mq = get_music_queue(guild_id)
    is_playing = has_active_music(guild)
    if is_playing:
        if get_radio_dj(guild_id).is_active:
            # Radio request — jump the queue so it plays right after the current
            # track, preload it for a gapless cut-in, and steer the vibe toward it.
            mq.queue.insert(0, track)
            try:
                if mq._mixer:
                    mq._mixer.prebuffer_next(track.stream_url)
            except Exception as e:
                log.debug("[RADIO] request prebuffer failed: %s", e)
            record_radio_request(guild_id, track.title)
            return f"📻 **Up next:** {track.title} — playing right after this one."
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
    # Radio: a skip is "not in the mood" — cooldown so it won't loop back, NOT a dislike.
    if get_radio_dj(guild_id).is_active and old_title != "track":
        record_radio_skip(guild_id, old_title)
    nxt = mq.skip()
    if nxt:
        mixer = mq._mixer
        vc = guild.voice_client
        if mixer and (mixer.has_music or _voice_client_has_mixer(vc, mixer)):
            mq.current = nxt
            async def _on_end():
                await _play_next(guild, None)
            mixer.crossfade_to(nxt.stream_url, volume=mq.volume, on_end=_on_end)
            if mq.queue and mixer:
                mixer.prebuffer_next(mq.queue[0].stream_url)
            _sync_web_mixer_music(guild)
        else:
            await play_track(guild, nxt)
    else:
        mq.current = None
        if mq._mixer:
            mq._mixer.stop_music()
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


def set_audio_effect(guild_id: str, effect: str) -> str:
    from mixer import EFFECTS
    mq = get_music_queue(guild_id)
    if not mq._mixer:
        return "❌ Nothing is playing."
    if effect not in EFFECTS:
        names = ", ".join(f"`{n}`" for n in EFFECTS if n != "none")
        return f"❌ Unknown effect. Available: {names}"
    mq._mixer.set_effect(effect)
    if effect == "none":
        return "🎛️ Effects cleared."
    return f"🎛️ Effect set to **{effect}**. Takes effect on next song or `/skip`."


def set_audio_speed(guild_id: str, speed: float) -> str:
    mq = get_music_queue(guild_id)
    if not mq._mixer:
        return "❌ Nothing is playing."
    mq._mixer.set_speed(speed)
    return f"🎛️ Speed set to **{speed:.2f}x**. Takes effect on next song or `/skip`."


def set_bass_boost(guild_id: str, db: int) -> str:
    mq = get_music_queue(guild_id)
    if not mq._mixer:
        return "❌ Nothing is playing."
    mq._mixer.set_bass_boost(db)
    if db == 0:
        return "🎛️ Bass boost off."
    return f"🎛️ Bass boost set to **+{db} dB**. Takes effect on next song or `/skip`."


def get_effects_info(guild_id: str) -> str:
    mq = get_music_queue(guild_id)
    if not mq._mixer:
        return "No mixer active."
    info = mq._mixer.get_effects_info()
    lines = []
    if info["effect"] != "none":
        lines.append(f"Effect: **{info['effect']}**")
    if info["speed"] != 1.0:
        lines.append(f"Speed: **{info['speed']:.2f}x**")
    if info["bass_db"]:
        lines.append(f"Bass: **+{info['bass_db']} dB**")
    if info["treble_db"]:
        lines.append(f"Treble: **+{info['treble_db']} dB**")
    if not lines:
        return "🎛️ No effects active."
    return "🎛️ " + " | ".join(lines)


def clear_audio_effects(guild_id: str) -> str:
    mq = get_music_queue(guild_id)
    if not mq._mixer:
        return "❌ Nothing is playing."
    mq._mixer.clear_effects()
    return "🎛️ All effects cleared. Takes effect on next song or `/skip`."


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


def _push_queue_to_web(guild_id: str):
    mq = get_music_queue(guild_id)
    _update_web_mixer(guild_id, queue=[t.title for t in mq.queue[:20]])


def remove_from_queue(guild_id: str, position: int) -> str:
    """Remove the upcoming track at a 1-based queue position (not the current song)."""
    mq = get_music_queue(guild_id)
    if not mq.queue:
        return "The queue is empty — nothing to remove."
    try:
        position = int(position)
    except (TypeError, ValueError):
        return "Give a queue position number (e.g. 2)."
    if position < 1 or position > len(mq.queue):
        return f"Position {position} is out of range — the queue has {len(mq.queue)} track(s)."
    removed = mq.queue.pop(position - 1)
    _push_queue_to_web(guild_id)
    return f"🗑️ Removed **{removed.title}** from the queue (was #{position})."


def move_in_queue(guild_id: str, from_position: int, to_position: int) -> str:
    """Reorder the queue: move the track at from_position to to_position (1-based)."""
    mq = get_music_queue(guild_id)
    n = len(mq.queue)
    if n < 2:
        return "Need at least 2 queued tracks to reorder."
    try:
        from_position = int(from_position); to_position = int(to_position)
    except (TypeError, ValueError):
        return "Give numeric positions (e.g. move 3 to 1)."
    if not (1 <= from_position <= n) or not (1 <= to_position <= n):
        return f"Positions must be between 1 and {n}."
    if from_position == to_position:
        return "That track is already in that position."
    track = mq.queue.pop(from_position - 1)
    mq.queue.insert(to_position - 1, track)
    _push_queue_to_web(guild_id)
    return f"↕️ Moved **{track.title}** to #{to_position}."


def clear_queue(guild_id: str) -> str:
    """Clear all upcoming tracks. The currently-playing song keeps playing."""
    mq = get_music_queue(guild_id)
    count = len(mq.queue)
    if not count:
        return "The queue is already empty."
    mq.queue.clear()
    _push_queue_to_web(guild_id)
    return f"🧹 Cleared the queue ({count} track{'s' if count != 1 else ''} removed). The current song keeps playing."


def set_music_volume(guild_id: str, vol: float) -> str:
    vol = max(0.0, min(1.0, vol))
    mq = get_music_queue(guild_id)
    mq.volume = vol
    if mq._mixer:
        mq._mixer.set_music_volume(vol)
    _update_web_mixer(
        guild_id,
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
    try:
        from sentence_transformers import SentenceTransformer
        _taste_embed_model = SentenceTransformer("all-MiniLM-L6-v2")
        return _taste_embed_model
    except Exception as e:
        log.warning("Taste model failed to load: %s — taste filtering disabled", e)
        return None


def _is_taste_relevant(entry: str, existing_liked: list[str], threshold: float = 0.35) -> bool:
    if len(existing_liked) < 5:
        return True
    try:
        model = _get_taste_model()
        if model is None:
            return True  # Accept if model failed to load
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


def _norm_song(s: str) -> str:
    """Normalize a song string for matching: lowercase, strip junk parens/brackets."""
    import re as _re
    s = s.lower().strip()
    s = _re.sub(r"[\(\[].*?[\)\]]", "", s)  # drop (Official Video), [HQ], etc.
    s = _re.sub(r"\s+", " ", s)
    return s.strip(" -")


def _artist_of(s: str) -> str:
    return s.split(" - ")[0].strip().lower() if " - " in s else ""


def record_feedback(user_id: str, track_title: str, positive: bool):
    data = _load_taste(user_id)
    entry = track_title.strip()
    weights = data.setdefault("dislike_weights", {})
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
        # A like cancels accumulated dislike for that exact song
        weights.pop(_norm_song(entry), None)
    else:
        # Dislike STACKS — each press strengthens avoidance, escalating to the artist.
        key = _norm_song(entry)
        weights[key] = weights.get(key, 0) + 1
        artist = _artist_of(entry)
        if artist:
            akey = f"artist::{artist}"
            weights[akey] = weights.get(akey, 0) + 1
        if entry not in data["disliked"]:
            data["disliked"].append(entry)
        data["liked"] = [l for l in data["liked"] if l != entry]
        log.info("[TASTE] Dislike stacked: '%s' weight=%d (artist '%s' weight=%d)",
                 entry[:50], weights.get(key, 0),
                 artist, weights.get(f"artist::{artist}", 0) if artist else 0)
    data["liked"] = data["liked"][-100:]
    data["disliked"] = data["disliked"][-100:]
    # Keep only the heaviest dislike weights so the file can't grow unbounded
    if len(weights) > 200:
        data["dislike_weights"] = dict(
            sorted(weights.items(), key=lambda kv: kv[1], reverse=True)[:200]
        )
    _save_taste(user_id, data)


def dislike_strength(data: dict, song: str) -> int:
    """How strongly THIS user dislikes a song — max of exact-song and artist weight."""
    weights = data.get("dislike_weights") or {}
    # Back-compat: a plain entry in the legacy 'disliked' list counts as weight 1
    base = 1 if song in data.get("disliked", []) else 0
    exact = weights.get(_norm_song(song), 0)
    artist = _artist_of(song)
    art_w = weights.get(f"artist::{artist}", 0) if artist else 0
    return max(base, exact, art_w)


async def record_feedback_async(user_id: str, track_title: str, positive: bool):
    """Async wrapper for record_feedback. The taste-relevance check embeds text with a
    sentence-transformer (~2-3s, blocking), which would stall the event loop and expire
    Discord interaction tokens (10062) — so always run it in a worker thread."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, record_feedback, user_id, track_title, positive)


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
        self._loop_task: Optional[asyncio.Task] = None

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
        dj._loop_task = asyncio.create_task(_dj_loop(guild, text_channel))
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


# ── Radio DJ System (ElevenLabs TTS) ─────────────────────────────────────────

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_RADIO_VOICE_ID = os.getenv("ELEVENLABS_RADIO_VOICE_ID", "")
_ELEVENLABS_FALLBACK_VOICES = [
    "JBFqnCBsd6RMkjVDRZzb",  # George
    "pNInz6obpgDQGcFmaJgB",  # Adam
    "ErXwobaYiN019PkySvjV",  # Antoni
]
_elevenlabs_working_voice: Optional[str] = None

RADIO_GENRES = [
    "indie", "lo-fi", "acoustic", "jazz", "soft rock", "dream pop",
    "indie folk", "chillwave", "ambient pop", "shoegaze", "bossa nova",
    "indie electronic", "bedroom pop", "post-rock", "neo-soul",
    "trip hop", "downtempo", "alternative r&b", "indie pop",
]

RADIO_FALLBACK_SONGS = [
    "Mac DeMarco - Chamber of Reflection",
    "Khruangbin - Time (You and I)",
    "Men I Trust - Numb",
    "Boy Pablo - Everytime",
    "Clairo - Sofia",
    "Still Woozy - Goodie Bag",
    "Mild High Club - Homage",
    "Tame Impala - Feels Like We Only Go Backwards",
    "Beach House - Space Song",
    "Unknown Mortal Orchestra - Hunnybee",
    "Homeshake - Every Single Thing",
    "Alvvays - Dreams Tonite",
    "Japanese Breakfast - Machinist",
    "Snail Mail - Pristine",
    "Soccer Mommy - circle the drain",
    "Cigarettes After Sex - Apocalypse",
    "Mazzy Star - Fade Into You",
    "Radiohead - No Surprises",
    "The xx - Intro",
    "Washed Out - Feel It All Around",
]

RADIO_QUEUE_TARGET = 3  # Always keep this many tracks buffered ahead


async def _elevenlabs_tts_call(voice_id: str, text: str) -> Optional[bytes]:
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    payload = {
        "text": text,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {
            "stability": 0.6,
            "similarity_boost": 0.75,
            "style": 0.3,
        },
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload,
                                 timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status == 200:
                data = await resp.read()
                return data if data else None
            err = await resp.text()
            log.warning("[RADIO] ElevenLabs voice %s → %d: %s", voice_id, resp.status, err[:150])
            return None


async def text_to_speech_elevenlabs(text: str) -> Optional[bytes]:
    global _elevenlabs_working_voice
    if not ELEVENLABS_API_KEY:
        log.info("[RADIO] No ElevenLabs API key — using edge_tts")
        return await text_to_speech(text)

    if _elevenlabs_working_voice:
        try:
            data = await _elevenlabs_tts_call(_elevenlabs_working_voice, text)
            if data:
                return data
        except Exception as e:
            log.warning("[RADIO] Cached voice %s failed: %s", _elevenlabs_working_voice, e)
        _elevenlabs_working_voice = None

    voices_to_try = []
    if ELEVENLABS_RADIO_VOICE_ID:
        voices_to_try.append(ELEVENLABS_RADIO_VOICE_ID)
    voices_to_try.extend(v for v in _ELEVENLABS_FALLBACK_VOICES if v != ELEVENLABS_RADIO_VOICE_ID)

    for vid in voices_to_try:
        try:
            data = await _elevenlabs_tts_call(vid, text)
            if data:
                _elevenlabs_working_voice = vid
                if vid != ELEVENLABS_RADIO_VOICE_ID:
                    log.info("[RADIO] Using fallback ElevenLabs voice %s", vid)
                else:
                    log.info("[RADIO] ElevenLabs voice OK: %s", vid)
                return data
        except Exception as e:
            log.warning("[RADIO] Voice %s error: %s", vid, e)

    log.warning("[RADIO] All ElevenLabs voices failed — falling back to edge_tts")
    return await text_to_speech(text)


class RadioDJ:
    def __init__(self, guild_id: str):
        self.guild_id = guild_id
        self.active = False
        self._track_start_time = 0.0
        self._current_track_title: Optional[str] = None
        self._songs_played: list[str] = []
        self._talk_counter = 0
        self._talk_interval = 1
        self._rec_cache: list[str] = []
        self._fill_lock = asyncio.Lock()
        self._loop_task: Optional[asyncio.Task] = None
        self._pending_speech_text: Optional[str] = None
        self._pending_speech_audio: Optional[bytes] = None
        self._pending_speech_for: Optional[str] = None  # next_song title this speech is for
        self._pregen_task: Optional[asyncio.Task] = None
        self._listener_ids: list[str] = []  # user IDs currently in the VC (for taste blend)
        self._skipped: list[str] = []  # session skip-cooldown: "not in the mood", NOT a dislike
        self._recent_requests: list[str] = []  # explicit listener requests — steer the vibe

    @property
    def is_active(self):
        return self.active

    def should_talk(self) -> bool:
        import random
        self._talk_counter += 1
        if self._talk_counter >= self._talk_interval:
            self._talk_counter = 0
            self._talk_interval = random.randint(2, 5)
            return True
        return False


_radio_djs: dict[str, RadioDJ] = {}
# Per-guild recently-played, so one server's history never suppresses another's.
_radio_recent: dict[str, list[str]] = defaultdict(list)


def get_radio_dj(guild_id: str) -> RadioDJ:
    if guild_id not in _radio_djs:
        _radio_djs[guild_id] = RadioDJ(guild_id)
    return _radio_djs[guild_id]


def record_radio_skip(guild_id: str, title: str):
    """A skip = 'not in the mood right now'. Cooldown so it won't loop back this
    session, but it is NOT a dislike and never touches the taste profile."""
    if not title:
        return
    rdj = get_radio_dj(guild_id)
    key = _norm_song(title)
    if key in rdj._skipped:
        rdj._skipped.remove(key)
    rdj._skipped.append(key)
    if len(rdj._skipped) > 300:
        rdj._skipped = rdj._skipped[-300:]
    log.info("[RADIO] Skip cooldown: '%s' (%d on cooldown)", title[:50], len(rdj._skipped))


def record_radio_request(guild_id: str, title: str):
    """Record an explicit listener request so the curator steers upcoming auto-picks
    toward its vibe (genre/energy/adjacent artists)."""
    if not title:
        return
    rdj = get_radio_dj(guild_id)
    if title in rdj._recent_requests:
        rdj._recent_requests.remove(title)
    rdj._recent_requests.append(title)
    if len(rdj._recent_requests) > 8:
        rdj._recent_requests = rdj._recent_requests[-8:]


ARTIST_AVOID_THRESHOLD = 2  # dislikes must STACK to this before avoiding the whole artist


def _radio_vibe_hint(guild_id: str) -> str:
    """A cheap, no-LLM read of the room's current mood: time of day + recent chatter.
    Fed into the curator so the set drifts toward how the room feels right now."""
    from datetime import datetime, timezone, timedelta
    parts = []
    try:
        hour = (datetime.now(timezone.utc) + timedelta(hours=7)).hour
    except Exception:
        hour = 12
    if 0 <= hour < 6:
        parts.append("it's deep late night")
    elif hour < 12:
        parts.append("it's morning")
    elif hour < 17:
        parts.append("it's afternoon")
    elif hour < 21:
        parts.append("it's evening")
    else:
        parts.append("it's night")
    msgs = []
    for e in _convo_buffer.get(guild_id, [])[-6:]:
        if e.get("user_name") != "Blood" and e.get("text"):
            t = e["text"].strip().replace("\n", " ")
            if t:
                msgs.append(t[:80])
    if msgs:
        parts.append("recent chat: " + " | ".join(msgs[-4:]))
    return "; ".join(parts)


def _collect_room_taste(listener_ids: list[str]) -> dict:
    """Blend the taste of everyone currently in the VC.

    Returns {'liked': [...], 'liked_norm': set, 'dislikes': {normkey: weight}}.
    The dislike map covers both exact-song keys and 'artist::<name>' keys, summed
    across listeners so that dislikes genuinely stack the more people (and the more
    often) a song/artist gets thumbed-down.
    """
    liked: list[str] = []
    liked_norm: set[str] = set()
    dislikes: dict[str, int] = {}
    for uid in listener_ids:
        try:
            data = _load_taste(uid)
        except Exception:
            continue
        for l in data.get("liked", [])[-15:]:
            liked.append(l)
            liked_norm.add(_norm_song(l))
        # dislike_weights is the source of truth (record_feedback keeps it in sync).
        weights = data.get("dislike_weights") or {}
        for k, w in weights.items():
            dislikes[k] = dislikes.get(k, 0) + int(w)
        # Back-compat ONLY: legacy 'disliked' entries from before weights existed.
        # Skip any whose song key already appears in weights so we never double-count.
        for d in data.get("disliked", []):
            k = _norm_song(d)
            if k in weights:
                continue
            dislikes[k] = dislikes.get(k, 0) + 1
            art = _artist_of(d)
            if art:
                dislikes[f"artist::{art}"] = dislikes.get(f"artist::{art}", 0) + 1
    return {"liked": liked, "liked_norm": liked_norm, "dislikes": dislikes}


def _radio_passes(song: str, guild_id: str, room: dict, rdj: "RadioDJ") -> bool:
    """Hard gate before a song is allowed into the radio queue.

    - exact-song dislike (any weight) → always blocked
    - artist dislike that has STACKED to the threshold → blocked, unless the song
      is one the room explicitly liked (an explicit like wins over artist avoidance)
    - skip cooldown (this session) → blocked
    - recently played → blocked
    """
    norm = _norm_song(song)
    dislikes = room["dislikes"]
    if dislikes.get(norm, 0) >= 1:
        return False
    art = _artist_of(song)
    if (art and dislikes.get(f"artist::{art}", 0) >= ARTIST_AVOID_THRESHOLD
            and norm not in room["liked_norm"]):
        return False
    if norm in rdj._skipped:
        return False
    if song in _radio_recent[guild_id]:
        return False
    return True


async def _warmup_elevenlabs_bg():
    """Try each ElevenLabs voice with a short timeout and cache the first one that works.
    Runs in the background so it never blocks /radio startup."""
    global _elevenlabs_working_voice
    if not ELEVENLABS_API_KEY or _elevenlabs_working_voice:
        return
    voices_to_try = []
    if ELEVENLABS_RADIO_VOICE_ID:
        voices_to_try.append(ELEVENLABS_RADIO_VOICE_ID)
    voices_to_try.extend(v for v in _ELEVENLABS_FALLBACK_VOICES if v != ELEVENLABS_RADIO_VOICE_ID)
    for vid in voices_to_try:
        try:
            data = await asyncio.wait_for(_elevenlabs_tts_call(vid, "Hi"), timeout=8.0)
            if data:
                _elevenlabs_working_voice = vid
                log.info("[RADIO] ElevenLabs voice warmed up in background: %s", vid)
                return
        except Exception as e:
            log.warning("[RADIO] ElevenLabs warmup voice %s failed: %s", vid, e)
    log.warning("[RADIO] All ElevenLabs voices failed warmup — will use edge_tts")


async def _radio_recommendations(rdj: "RadioDJ", count: int = 15) -> list[str]:
    """Taste-aware curator: seeds from the liked songs of whoever is in the VC,
    hard-avoids disliked songs/artists, and mixes in discovery so it never loops
    the same generic canon. Falls back to the built-in list (also filtered)."""
    import random as _rng
    from provider import call_ai

    guild_id = rdj.guild_id
    room = _collect_room_taste(rdj._listener_ids)
    dislikes = room["dislikes"]
    liked = list(room["liked"])
    _rng.shuffle(liked)
    liked_sample = liked[:12]

    genre_sample = _rng.sample(RADIO_GENRES, min(5, len(RADIO_GENRES)))
    genres_text = ", ".join(genre_sample)
    recent_text = ", ".join(_radio_recent[guild_id][-10:]) if _radio_recent[guild_id] else "none"

    # Surface the room's strongest dislikes so the model avoids that lane entirely.
    avoid_artists = sorted(
        (k[len("artist::"):] for k, w in dislikes.items()
         if k.startswith("artist::") and w >= 1),
        key=lambda a: dislikes.get(f"artist::{a}", 0), reverse=True,
    )[:12]
    avoid_text = ", ".join(avoid_artists) if avoid_artists else "none"

    if liked_sample:
        taste_line = (
            f"The people listening right now have liked these before — lean into this "
            f"taste, same energy and adjacent artists: {', '.join(liked_sample)}\n"
        )
        mix_rule = ("- Mix: ~60% close to the listeners' taste above, ~30% discovery "
                    "(adjacent artists/genres they'd likely enjoy), ~10% wildcard\n")
    else:
        taste_line = ""
        mix_rule = "- Mix well-known indie with deeper cuts\n"

    # Live vibe: time of day + what the room is saying right now, so the set leans
    # toward the mood of the moment instead of a fixed late-night template.
    vibe_hint = _radio_vibe_hint(guild_id)
    vibe_line = (f"Read the room right now — {vibe_hint}\n"
                 f"Nudge the energy/feel of the picks toward that mood.\n") if vibe_hint else ""

    # Explicit requests are the strongest steer — bend the set toward them.
    requests = list(rdj._recent_requests)[-5:]
    request_line = (
        f"The room just REQUESTED: {', '.join(requests)}. "
        f"Strongly match that vibe — same energy, genre and adjacent artists — in your picks.\n"
    ) if requests else ""

    prompt = (
        f"You are a radio station music curator reading the room in real time.\n"
        f"Generate {count} songs perfect for background/radio listening.\n\n"
        f"{taste_line}"
        f"{request_line}"
        f"{vibe_line}"
        f"Genres to draw from: {genres_text}\n"
        f"Recently played (DO NOT repeat): {recent_text}\n"
        f"NEVER suggest these artists (listeners disliked them): {avoid_text}\n\n"
        f"Rules:\n"
        f"- Output ONLY a list, one song per line, format: Artist - Song Title\n"
        f"- NO numbering, NO bullets, NO extra text\n"
        f"- Every song must be REAL and existing\n"
        f"- Tempo: not too fast, not too slow — background/radio vibe\n"
        f"{mix_rule}"
        f"- NEVER repeat an artist within this list\n"
        f"- Think: songs you'd hear on a chill indie radio station at night\n"
    )
    try:
        result = await call_ai(
            system="You are a radio station music curator. Output ONLY song names, one per line.",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=500,
        )
        text = result.get("message", {}).get("content", "")
        songs = []
        for line in text.strip().split("\n"):
            line = line.strip().lstrip("0123456789.-) •●").strip()
            if (line and 5 < len(line) < 100
                    and " - " in line
                    and not any(w in line.lower() for w in
                                ("actually", "maybe", "could be", "let me", "i think", "note:", "here"))):
                songs.append(line)
        # Hard gate: drop anything disliked / skipped / too-recent before it can play.
        songs = [s for s in songs if _radio_passes(s, guild_id, room, rdj)]
        if songs:
            _rng.shuffle(songs)
            return songs
    except Exception as e:
        log.warning("[RADIO] Recommendation failed: %s", e)

    fallback = list(RADIO_FALLBACK_SONGS)
    _rng.shuffle(fallback)
    return [s for s in fallback if _radio_passes(s, guild_id, room, rdj)][:count]


def _mark_radio_played(guild_id: str, song: str):
    recent = _radio_recent[guild_id]
    if song not in recent:
        recent.append(song)
    if len(recent) > 40:
        _radio_recent[guild_id] = recent[-40:]


async def _get_radio_song(rdj: RadioDJ) -> str:
    guild_id = rdj.guild_id
    room = _collect_room_taste(rdj._listener_ids)
    # Drain the prefetch cache first, skipping anything now filtered out.
    while rdj._rec_cache:
        song = rdj._rec_cache.pop(0)
        if _radio_passes(song, guild_id, room, rdj):
            _mark_radio_played(guild_id, song)
            return song
    songs = await _radio_recommendations(rdj, count=15)
    if not songs:
        songs = await _radio_recommendations(rdj, count=15)
    if songs:
        song = songs.pop(0)
        rdj._rec_cache = songs
        _mark_radio_played(guild_id, song)
        return song
    return "indie chill music"


async def _generate_radio_commentary(last_song: str, next_song: str,
                                      songs_played: list[str],
                                      guild_id: str = "") -> str:
    from provider import call_ai
    import random

    local_hour = (datetime.now(timezone.utc).hour + 7) % 24
    if 0 <= local_hour < 6:
        time_vibe = "deep into the night, around " + str(local_hour) + " AM"
    elif 6 <= local_hour < 12:
        time_vibe = "morning, around " + str(local_hour) + " AM"
    elif 12 <= local_hour < 17:
        time_vibe = "afternoon, around " + str(local_hour - 12) + " PM"
    elif 17 <= local_hour < 21:
        time_vibe = "evening, around " + str(local_hour - 12) + " PM"
    else:
        time_vibe = "late night, around " + str(local_hour - 12) + " PM"

    recent_chat = ""
    if guild_id:
        chat_entries = _convo_buffer.get(guild_id, [])[-5:]
        user_msgs = [e.get("text", "") for e in chat_entries
                     if e.get("user_name") != "Blood"]
        if user_msgs:
            recent_chat = f"Recent listener chatter: {'; '.join(user_msgs[-3:])}"

    styles = [
        "comment on the song that just played and naturally introduce the next one",
        "share a brief thought or vibe check, then mention what's coming up",
        "smoothly introduce the next song with a one-liner",
        "talk about the vibe of the time of day, then transition",
        "ask your listeners a casual question",
        "share a quick thought about the artist or genre",
        "reminisce about the mood of the set so far, then tease the next track",
        "give a shoutout to anyone listening",
    ]
    songs_context = ", ".join(songs_played[-5:]) if songs_played else "just getting started"
    style = random.choice(styles)
    chat_hint = ""
    if recent_chat:
        chat_hint = f" Some listeners just said: {recent_chat.replace('Recent listener chatter: ', '')}."
    prompt = (
        f"You're between songs on the radio. It's {time_vibe}. "
        f"You just played \"{last_song}\" and now \"{next_song}\" is starting. "
        f"Set so far: {songs_context} ({len(songs_played)} songs deep).{chat_hint} "
        f"Go ahead and {style}."
    )
    try:
        result = await call_ai(
            system=(
                "You are Claude, a warm and genuine radio host between songs. You're an "
                "engaging, knowledgeable DJ with an easy sense of humor. You're friendly and "
                "down-to-earth with your listeners, and you genuinely love the music and the "
                "vibe.\n\n"
                "You're speaking ON AIR between songs right now. Say your line.\n\n"
                "HARD RULES:\n"
                "- 1 to 3 short sentences ONLY\n"
                "- Pure spoken word — no markdown, no emojis, no asterisks, no quotes\n"
                "- Output ONLY what you say on air. Nothing else. No preamble.\n"
            ),
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1500,
        )
        content = result.get("message", {}).get("content", "").strip()
        if not content:
            log.warning("[RADIO] Commentary AI returned no content. Keys: %s", list(result.keys()))
            return ""
        import re
        had_think = "<think>" in content
        content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
        if had_think:
            log.info("[RADIO] Stripped <think> tags, remaining: %d chars", len(content))
        # Strip reasoning preamble — only remove the first line(s) that look
        # like internal reasoning, keep everything else as the script.
        preamble_prefixes = ("The user wants", "I need to", "I should",
                             "Here's my", "My response:", "Script:",
                             "Constraints", "Style:", "Sure,", "Okay,",
                             "Here is", "Here's what")
        lines = content.split("\n")
        # Drop leading preamble lines only
        while lines and any(lines[0].strip().startswith(bp) for bp in preamble_prefixes):
            lines.pop(0)
        content = " ".join(l.strip() for l in lines if l.strip())
        # Strip numbered instruction lists the model sometimes emits (e.g. "1. Be sharp ... 2.")
        content = re.sub(r'^\d+\.\s*', '', content)
        content = re.sub(r'\s+\d+\.\s*$', '', content)
        # Remove markdown artifacts
        content = re.sub(r'[*_#\[\]`]', '', content).strip()
        if not content:
            log.warning("[RADIO] Commentary empty after filtering")
            return ""
        # Detect instruction echo — model parroting system prompt instead of output
        _instruction_markers = (
            "warm and genuine", "engaging", "knowledgeable DJ", "down-to-earth",
            "easy sense of humor", "HARD RULES", "no preamble", "no markdown", "no emojis",
            "ON AIR between songs", "Output ONLY what you say", "radio host",
            "Pure spoken word", "1 to 3 short sentences",
        )
        marker_hits = sum(1 for m in _instruction_markers if m.lower() in content.lower())
        if marker_hits >= 2:
            log.warning("[RADIO] Commentary looks like echoed instructions (%d markers) — discarding: %s",
                        marker_hits, content[:120])
            return ""
        # Truncate to first 3 sentences if too long
        if len(content) > 500:
            sentences = re.split(r'(?<=[.!?])\s+', content)
            content = " ".join(sentences[:3]).strip()
            log.info("[RADIO] Truncated commentary to %d chars (%d sentences)", len(content), min(3, len(sentences)))
        if not content or len(content) < 10:
            return ""
        log.info("[RADIO] Commentary final (%d chars): %s", len(content), content[:80])
        return content
    except Exception as e:
        log.warning("[RADIO] Commentary generation failed: %s", e)
        return ""


async def _pregenerate_radio_speech(guild: discord.Guild, guild_id: str):
    """Generate DJ commentary + TTS audio ahead of time for the next transition."""
    rdj = get_radio_dj(guild_id)
    if not rdj.is_active:
        return

    mq = get_music_queue(guild_id)
    current_title = rdj._current_track_title
    next_track = mq.queue[0] if mq.queue else None
    if not current_title or not next_track:
        return

    next_title = next_track.title
    log.info("[RADIO] Pre-generating commentary for '%s' → '%s'", current_title, next_title)

    try:
        text = await _generate_radio_commentary(
            current_title, next_title, rdj._songs_played[-10:],
            guild_id=guild_id,
        )
        if not text:
            log.warning("[RADIO] Pre-gen commentary was empty")
            return

        mp3_data = await text_to_speech_elevenlabs(text)
        if not mp3_data:
            log.warning("[RADIO] Pre-gen TTS failed")
            return

        rdj._pending_speech_text = text
        rdj._pending_speech_audio = mp3_data
        rdj._pending_speech_for = next_title
        log.info("[RADIO] Pre-gen ready: '%s' (%d bytes audio)", text[:60], len(mp3_data))
        _sync_web_mixer_upcoming(guild, text)
    except Exception as e:
        log.warning("[RADIO] Pre-gen failed: %s", e)


def _sync_web_mixer_upcoming(guild, text: str):
    try:
        from web_mixer import update_state
        update_state(guild_id=str(guild.id), upcoming_script=text)
    except Exception:
        pass


async def _speak_radio_cached(guild: discord.Guild, guild_id: str,
                               text: str, mp3_data: bytes):
    """Play pre-cached TTS audio through the mixer."""
    vc = guild.voice_client
    if not vc or not vc.is_connected():
        return
    log.info("[RADIO] Playing cached TTS (%d bytes): %s", len(mp3_data), text[:60])
    mq = get_music_queue(guild_id)
    mixer = mq._mixer
    if mixer and (mixer.has_music or _voice_client_has_mixer(vc, mixer)):
        try:
            _sync_web_mixer_tts(guild, active=True, text=text, level=1.0)
            _sync_web_mixer_upcoming(guild, "")
            await _feed_tts_with_timeout(mixer, mp3_data)
            log.info("[RADIO] DJ spoke (cached): %s", text[:60])
        except Exception as e:
            log.warning("[RADIO] Cached TTS via mixer failed: %s", e)
        finally:
            _sync_web_mixer_tts(guild, active=False, text="", level=0.0)
    else:
        log.info("[RADIO] Mixer not ready for cached speech — waiting")
        for _ in range(15):
            await asyncio.sleep(0.2)
            mq2 = get_music_queue(guild_id)
            mixer2 = mq2._mixer
            if mixer2 and (mixer2.has_music or _voice_client_has_mixer(vc, mixer2)):
                try:
                    _sync_web_mixer_tts(guild, active=True, text=text, level=1.0)
                    _sync_web_mixer_upcoming(guild, "")
                    await _feed_tts_with_timeout(mixer2, mp3_data)
                    log.info("[RADIO] DJ spoke (cached, waited): %s", text[:60])
                except Exception as e:
                    log.warning("[RADIO] Cached TTS (delayed) failed: %s", e)
                finally:
                    _sync_web_mixer_tts(guild, active=False, text="", level=0.0)
                return
        log.warning("[RADIO] Mixer never became ready for cached speech")


async def _feed_tts_with_timeout(mixer, mp3_data: bytes, timeout: float = 60.0):
    """Run feed_tts_sync in executor with a hard timeout to prevent infinite hangs."""
    loop = asyncio.get_running_loop()
    try:
        await asyncio.wait_for(
            loop.run_in_executor(None, mixer.feed_tts_sync, mp3_data),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        log.error("[MIXER] feed_tts_sync timed out after %.0fs — force-clearing", timeout)
        mixer._tts_active = False
        mixer._tts_buf.clear()
        mixer._duck_since = 0.0
        raise


async def _speak_radio(guild: discord.Guild, guild_id: str, text: str):
    vc = guild.voice_client
    if not vc or not vc.is_connected():
        log.warning("[RADIO] _speak_radio: no voice client")
        return
    log.info("[RADIO] Generating TTS for: %s", text[:80])
    mp3_data = await text_to_speech_elevenlabs(text)
    if not mp3_data:
        log.warning("[RADIO] TTS returned no audio for: %s", text[:60])
        return
    log.info("[RADIO] TTS audio: %d bytes for: %s", len(mp3_data), text[:60])
    mq = get_music_queue(guild_id)
    mixer = mq._mixer
    if mixer and (mixer.has_music or _voice_client_has_mixer(vc, mixer)):
        try:
            _sync_web_mixer_tts(guild, active=True, text=text, level=1.0)
            await _feed_tts_with_timeout(mixer, mp3_data)
            log.info("[RADIO] DJ spoke: %s", text[:60])
        except Exception as e:
            log.warning("[RADIO] TTS via mixer failed: %s", e)
        finally:
            _sync_web_mixer_tts(guild, active=False, text="", level=0.0)
    else:
        rdj = get_radio_dj(guild_id)
        if rdj.is_active:
            # Mixer not ready yet during radio — wait briefly for it to come back
            log.info("[RADIO] Mixer not ready — waiting up to 3s")
            for _ in range(15):  # Wait up to 3 seconds (15 * 0.2s)
                await asyncio.sleep(0.2)
                mq2 = get_music_queue(guild_id)
                mixer2 = mq2._mixer
                if mixer2 and (mixer2.has_music or _voice_client_has_mixer(vc, mixer2)):
                    try:
                        _sync_web_mixer_tts(guild, active=True, text=text, level=1.0)
                        await _feed_tts_with_timeout(mixer2, mp3_data)
                        log.info("[RADIO] DJ spoke (waited for mixer): %s", text[:60])
                    except Exception as e:
                        log.warning("[RADIO] TTS via mixer (delayed) failed: %s", e)
                    finally:
                        _sync_web_mixer_tts(guild, active=False, text="", level=0.0)
                    return
            log.warning("[RADIO] Mixer never became ready — skipping speech")
            return
        try:
            if vc.is_playing() or vc.is_paused():
                _stop_voice_playback(vc)
            source = FFmpegPCMAudioPipe(mp3_data)
            source.prepare()
            _sync_web_mixer_tts(guild, active=True, text=text, level=1.0)
            tts_done = asyncio.Event()
            def on_done(error):
                if error:
                    log.warning("[RADIO] TTS playback error: %s", error)
                tts_done.set()
            vc.play(source, after=on_done)
            await tts_done.wait()
            log.info("[RADIO] DJ spoke (standalone): %s", text[:60])
        except Exception as e:
            log.warning("[RADIO] TTS standalone failed: %s", e)
        finally:
            _sync_web_mixer_tts(guild, active=False, text="", level=0.0)


async def _fill_radio_queue(rdj: RadioDJ, guild_id: str):
    """Top up the radio queue to RADIO_QUEUE_TARGET tracks using parallel extraction.

    Uses a per-RadioDJ lock so concurrent callers don't double-fill — the second
    caller acquires the lock, sees needed=0, and exits immediately.
    """
    async with rdj._fill_lock:
        if not rdj.is_active:
            return
        mq = get_music_queue(guild_id)
        needed = RADIO_QUEUE_TARGET - len(mq.queue)
        if needed <= 0:
            # Still make sure the next-up track is prebuffered
            if mq.queue and mq._mixer:
                mq._mixer.prebuffer_next(mq.queue[0].stream_url)
            return

        # Collect queries sequentially — fast (pops from rdj._rec_cache)
        queries = []
        for _ in range(needed):
            if not rdj.is_active:
                break
            queries.append(await _get_radio_song(rdj))

        if not queries:
            return

        # Extract all tracks in parallel — this is the slow yt-dlp part
        log.info("[RADIO] Extracting %d tracks in parallel to fill queue", len(queries))
        results = await asyncio.gather(
            *[extract_track(q, "Radio DJ") for q in queries],
            return_exceptions=True,
        )

        added = 0
        for result in results:
            if not rdj.is_active:
                break
            if isinstance(result, Exception) or result is None:
                continue
            mq.add(result)
            added += 1
            log.info("[RADIO] Pre-queued: %s (queue: %d)", result.title, len(mq.queue))

        if added == 0:
            log.warning("[RADIO] Parallel fill got 0 tracks — all extractions failed")

        # Prebuffer the soonest-coming track so gapless has frames ready
        if mq.queue and mq._mixer:
            mq._mixer.prebuffer_next(mq.queue[0].stream_url)


async def _await_voice_reconnect(guild: discord.Guild, rdj: "RadioDJ",
                                 timeout: float = 120.0) -> bool:
    """Wait out a transient voice-WS drop (e.g. close code 1006) while discord.py
    auto-RESUMEs. Returns True once reconnected, False if radio was stopped in the
    meantime or the connection stays down past `timeout` (a real disconnect)."""
    waited = 0.0
    step = 1.0
    while waited < timeout:
        if not rdj.is_active:
            return False
        vc = guild.voice_client
        if vc and vc.is_connected():
            await asyncio.sleep(0.5)  # let the handshake fully settle before we touch the player
            return bool(guild.voice_client and guild.voice_client.is_connected())
        await asyncio.sleep(step)
        waited += step
    return bool(guild.voice_client and guild.voice_client.is_connected())


async def _radio_set_reconnecting(guild_id: str, value: bool):
    try:
        import radio_panel
        panel = radio_panel.get_panel(guild_id)
        if panel:
            await panel.set_reconnecting(value)
    except Exception:
        pass


async def _radio_now_playing(guild: discord.Guild, track: "MusicTrack",
                             text_channel: Optional[discord.TextChannel]):
    """Announce the current track. If a player panel exists for this guild the
    panel IS the now-playing display (no text spam); otherwise fall back to text."""
    try:
        import radio_panel
        panel = radio_panel.get_panel(str(guild.id))
        if panel:
            await panel.set_track(track)
            return
    except Exception as e:
        log.debug("[RADIO] panel set_track skipped: %s", e)
    if text_channel:
        try:
            await text_channel.send(f"📻 **Now on air:** {track.title}")
        except Exception:
            pass


async def _radio_loop(guild: discord.Guild,
                       text_channel: Optional[discord.TextChannel]):
    guild_id = str(guild.id)
    try:
        await _radio_loop_body(guild, text_channel)
    finally:
        # Radio has ended (sign-off, VC drop, or startup failure) — clear the panel,
        # but only if a fresh /radio hasn't already taken over this guild.
        if not get_radio_dj(guild_id).is_active:
            await _teardown_radio_panel(guild_id)


async def _radio_loop_body(guild: discord.Guild,
                           text_channel: Optional[discord.TextChannel]):
    guild_id = str(guild.id)
    rdj = get_radio_dj(guild_id)
    mq = get_music_queue(guild_id)

    # Fetch RADIO_QUEUE_TARGET+1 queries up front, then extract all in parallel.
    # This means startup only waits for the slowest single extraction instead of
    # waiting for each one serially.
    startup_count = RADIO_QUEUE_TARGET + 1
    queries = []
    for _ in range(startup_count):
        queries.append(await _get_radio_song(rdj))

    log.info("[RADIO] Extracting %d startup tracks in parallel", startup_count)
    results = await asyncio.gather(
        *[extract_track(q, "Radio DJ") for q in queries],
        return_exceptions=True,
    )
    tracks = [r for r in results if r and not isinstance(r, Exception)]

    if not tracks:
        rdj.active = False
        log.warning("[RADIO] Could not find any startup tracks")
        if text_channel:
            try:
                await text_channel.send("📻 Radio couldn't find a song to start with. Try again.")
            except Exception:
                pass
        return

    # Play first track, pre-queue the rest immediately
    first_track = tracks[0]
    for queued in tracks[1:]:
        mq.add(queued)
        log.info("[RADIO] Startup pre-queued: %s", queued.title)

    await play_track(guild, first_track, text_channel)
    rdj._track_start_time = time.monotonic()
    rdj._current_track_title = first_track.title
    rdj._songs_played.append(first_track.title)

    # Prebuffer the very next track so gapless is ready from the first transition
    if mq.queue and mq._mixer:
        mq._mixer.prebuffer_next(mq.queue[0].stream_url)

    await _radio_now_playing(guild, first_track, text_channel)

    await asyncio.sleep(2)
    try:
        intro = f"You're tuned in to Clawd Radio. Kicking things off with {first_track.title}. Sit back and enjoy the vibes."
        await _speak_radio(guild, guild_id, intro)
        log.info("[RADIO] Intro speech delivered")
    except Exception as e:
        log.warning("[RADIO] Intro speech failed: %s", e)

    rdj._pregen_task = asyncio.create_task(
        _pregenerate_radio_speech(guild, guild_id)
    )

    while rdj.is_active:
        try:
            vc = guild.voice_client
            if not vc or not vc.is_connected():
                # Transient voice drop (e.g. WS close 1006). discord.py auto-RESUMEs,
                # so DON'T kill radio — wait out the blip and resume below. Only sign
                # off if it stays down a long time (real disconnect / kicked).
                log.warning("[RADIO] Voice connection lost — waiting for auto-reconnect…")
                await _radio_set_reconnecting(guild_id, True)
                reconnected = await _await_voice_reconnect(guild, rdj, timeout=120.0)
                await _radio_set_reconnecting(guild_id, False)
                if not reconnected:
                    log.warning("[RADIO] Voice stayed down >120s — signing off radio")
                    rdj.active = False
                    break
                vc = guild.voice_client
                log.info("[RADIO] Voice reconnected — restoring playback")
                # If the outage destroyed the mixer, bring the current song back instead
                # of leaving a silent gap. (Short blips keep the mixer alive → the
                # reattach below resumes straight from its buffer, seamlessly.)
                if (mq.current and not has_active_music(guild)
                        and (not mq._mixer or not mq._mixer.has_music)):
                    log.info("[RADIO] Mixer lost during outage — resuming current track: %s",
                             mq.current.title)
                    await play_track(guild, mq.current, text_channel)
                    rdj._track_start_time = time.monotonic()

            # Recovery: mixer has music but VC player died — reattach (resumes from buffer)
            if mq._mixer and mq._mixer.has_music and not _voice_client_has_mixer(vc, mq._mixer):
                if not vc.is_playing() and not vc.is_paused():
                    log.warning("[RADIO] Player died — reattaching mixer to VC")
                    try:
                        vc.play(mq._mixer)
                    except Exception as e:
                        log.warning("[RADIO] Reattach failed: %s — will recreate", e)
                        mq._mixer = None

            current = get_now_playing_track(guild)

            # Detect track change — gapless transition played a queued song
            if current and current.title != rdj._current_track_title:
                old_title = rdj._current_track_title
                rdj._current_track_title = current.title
                rdj._track_start_time = time.monotonic()
                rdj._songs_played.append(current.title)
                log.info("[RADIO] Track change: '%s' → '%s'", old_title, current.title)

                # Refill queue back to target in background
                asyncio.create_task(_fill_radio_queue(rdj, guild_id))

                await _radio_now_playing(guild, current, text_channel)

                if old_title and rdj.should_talk():
                    if rdj._pending_speech_audio and rdj._pending_speech_for == current.title:
                        log.info("[RADIO] Using pre-generated speech for '%s'", current.title)
                        await _speak_radio_cached(
                            guild, guild_id,
                            rdj._pending_speech_text,
                            rdj._pending_speech_audio,
                        )
                    else:
                        if rdj._pending_speech_for and rdj._pending_speech_for != current.title:
                            log.info("[RADIO] Pre-gen was for '%s' not '%s' — generating live",
                                     rdj._pending_speech_for, current.title)
                        else:
                            log.info("[RADIO] No pre-gen available — generating live")
                        commentary = await _generate_radio_commentary(
                            old_title, current.title, rdj._songs_played[-10:],
                            guild_id=guild_id,
                        )
                        if commentary:
                            await _speak_radio(guild, guild_id, commentary)
                rdj._pending_speech_text = None
                rdj._pending_speech_audio = None
                rdj._pending_speech_for = None
                _sync_web_mixer_upcoming(guild, "")

                if rdj._pregen_task and not rdj._pregen_task.done():
                    rdj._pregen_task.cancel()
                rdj._pregen_task = asyncio.create_task(
                    _pregenerate_radio_speech(guild, guild_id)
                )

            # No music playing — either queue is empty or songs are waiting unstarted.
            # The second case happens when a user /play or stop_music killed the active
            # player while _fill_radio_queue had already added songs to mq.queue.
            # Those songs sit frozen because _play_next() already returned and the
            # "not mq.queue" guard below would never fire.
            if not current and not has_active_music(guild):
                if mq.queue:
                    # Songs are ready but nothing is playing — restart from queue
                    nxt = mq.next()
                    if nxt:
                        log.info("[RADIO] Restarting from pre-filled queue: %s", nxt.title)
                        await play_track(guild, nxt, text_channel)
                        rdj._track_start_time = time.monotonic()
                        rdj._current_track_title = nxt.title
                        rdj._songs_played.append(nxt.title)
                        asyncio.create_task(_fill_radio_queue(rdj, guild_id))
                        await _radio_now_playing(guild, nxt, text_channel)
                else:
                    log.warning("[RADIO] Queue dry — triggering emergency fill")
                    asyncio.create_task(_fill_radio_queue(rdj, guild_id))

            if len(rdj._songs_played) > 50:
                rdj._songs_played = rdj._songs_played[-30:]

            # Keep the listener set fresh so the curator blends whoever is here now.
            _refresh_radio_listeners(guild)

            # Periodic web mixer state sync — prevents stale ducked/tts state
            _periodic_web_mixer_sync(guild)

        except Exception as e:
            log.error("[RADIO] Loop error (continuing): %s", e, exc_info=True)

        await asyncio.sleep(3)


async def start_radio(guild: discord.Guild,
                       text_channel: Optional[discord.TextChannel] = None) -> str:
    guild_id = str(guild.id)
    vc = guild.voice_client
    if not vc or not vc.is_connected():
        return "❌ Not in a voice channel. Use /joinvc first."

    dj = get_random_dj(guild_id)
    if dj.is_active:
        dj.active = False
        dj.participants.clear()

    rdj = get_radio_dj(guild_id)
    if rdj.is_active:
        return "\U0001f4fb Radio is already on air!"

    if has_active_music(guild):
        await stop_music(guild)

    rdj.active = True
    rdj._talk_counter = 0
    rdj._songs_played.clear()
    rdj._rec_cache.clear()
    rdj._skipped.clear()
    rdj._current_track_title = None
    rdj._pending_speech_text = None
    rdj._pending_speech_audio = None
    rdj._pending_speech_for = None
    _refresh_radio_listeners(guild)

    rdj._loop_task = asyncio.create_task(_radio_loop(guild, text_channel))
    _update_web_mixer(guild_id, radio_active=True)
    return "\U0001f4fb **Clawd Radio is now on air!** Sit back and enjoy the vibes."


def _refresh_radio_listeners(guild: discord.Guild):
    """Snapshot the human listeners in the bot's VC so the curator can blend taste."""
    rdj = get_radio_dj(str(guild.id))
    vc = guild.voice_client
    ids: list[str] = []
    if vc and vc.channel:
        for m in vc.channel.members:
            if not m.bot:
                ids.append(str(m.id))
    rdj._listener_ids = ids


async def stop_radio(guild_id: str) -> str:
    rdj = get_radio_dj(guild_id)
    if not rdj.is_active:
        # Still tear down any lingering panel/thread.
        await _teardown_radio_panel(guild_id)
        return "\U0001f4fb Radio isn't playing."
    rdj.active = False
    rdj._skipped.clear()
    await _teardown_radio_panel(guild_id)
    _update_web_mixer(guild_id, radio_active=False)
    return "\U0001f4fb Radio signed off. Thanks for listening."


async def _teardown_radio_panel(guild_id: str):
    try:
        import radio_panel
        await radio_panel.teardown(guild_id)
    except Exception as e:
        log.debug("[RADIO] panel teardown skipped: %s", e)


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


# ── Per-user join sounds (overlapping / "chaos mode") ─────────────────────────

# A short lock per guild — held only briefly to attach a sound to a source or
# spin one up. It does NOT serialize playback; overlapping sounds are mixed
# together so they play on top of each other.
_join_sfx_locks: dict[str, asyncio.Lock] = {}
# Standalone overlapping source per guild, used when no music is playing.
_join_sfx_sources: dict[str, "JoinSfxSource"] = {}


def _get_join_sfx_lock(guild_id: str) -> asyncio.Lock:
    lock = _join_sfx_locks.get(guild_id)
    if lock is None:
        lock = asyncio.Lock()
        _join_sfx_locks[guild_id] = lock
    return lock


class JoinSfxSource(discord.AudioSource):
    """Standalone audio source that mixes overlapping join sounds when no music
    is playing. Stays alive through a short silence grace period so closely-timed
    joins keep layering onto the same source, then stops so the VC frees up."""

    _GRACE_FRAMES = 25  # ~0.5s of silence before the source ends

    def __init__(self):
        from mixer import SfxLayer
        self._layer = SfxLayer()
        self._idle = 0
        self._frame_count = 0

    def add(self, frames: list) -> None:
        self._layer.add(frames)
        self._idle = 0
        log.debug("[JOIN-SFX] added %d frames to source", len(frames))

    def read(self) -> bytes:
        frame = self._layer.mix_frame()
        if frame is None:
            self._idle += 1
            if self._idle > self._GRACE_FRAMES:
                log.debug("[JOIN-SFX] grace period expired, stopping source")
                return b""  # ends playback
            from mixer import SILENCE
            return SILENCE
        self._idle = 0
        self._frame_count += 1
        if self._frame_count % 50 == 0:  # Log every 50 frames (~1 second)
            log.debug("[JOIN-SFX] playing frame #%d (size: %d bytes)", self._frame_count, len(frame))
        return frame

    def is_opus(self) -> bool:
        return False


async def play_join_sound(guild: discord.Guild, file_path: str) -> bool:
    """Play a user's personal join SFX in Blood's VC — overlapping/chaotic.

    Sounds are mixed *on top of* whatever's already playing: if music is on,
    they're layered into the music mixer (no ducking); if Blood is idle, they're
    mixed into a standalone overlapping source. Several people joining at once
    all play at the same time. Respects the per-guild on/off toggle.
    """
    vc = guild.voice_client
    if not vc or not vc.is_connected():
        return False

    guild_id = str(guild.id)
    try:
        from join_sfx import is_enabled
        if not is_enabled(guild_id):
            return False
    except Exception:
        pass

    try:
        with open(file_path, "rb") as f:
            audio_data = f.read()
    except Exception as e:
        log.warning("[JOIN-SFX] could not read %s: %s", file_path, e)
        return False
    if not audio_data:
        log.warning("[JOIN-SFX] empty file: %s", file_path)
        return False

    log.debug("[JOIN-SFX] read file %s (%d bytes), decoding...", file_path, len(audio_data))
    
    # Decode to PCM frames off the event loop (ffmpeg is blocking).
    from mixer import decode_to_frames
    loop = asyncio.get_running_loop()
    frames = await loop.run_in_executor(None, decode_to_frames, audio_data)
    if not frames:
        log.warning("[JOIN-SFX] no audio frames decoded from %s", file_path)
        return False

    log.debug("[JOIN-SFX] decoded %d frames, total data: ~%d bytes", len(frames), len(frames) * len(frames[0]) if frames else 0)
    
    name = os.path.basename(file_path)
    # Brief lock: only protects source bookkeeping, not playback duration.
    async with _get_join_sfx_lock(guild_id):
        vc = guild.voice_client
        if not vc or not vc.is_connected():
            return False

        mq = get_music_queue(guild_id)
        mixer = mq._mixer
        if mixer and (mixer.has_music or _voice_client_has_mixer(vc, mixer)):
            # Layer straight into the music mixer — plays over the music.
            mixer.sfx.add(frames)
            log.info("[JOIN-SFX] layered over music: %s", name)
            return True

        # Idle: feed (or start) the standalone overlapping source.
        src = _join_sfx_sources.get(guild_id)
        cur = _voice_client_source(vc)

        # If our source is the one currently playing, just layer onto it.
        if src is not None and cur is src and (vc.is_playing() or vc.is_paused()):
            src.add(frames)
            log.info("[JOIN-SFX] layered onto live source: %s", name)
            return True

        # If something ELSE owns the output (e.g. TTS), don't cut it off.
        if (vc.is_playing() or vc.is_paused()) and cur is not None and not isinstance(cur, JoinSfxSource):
            log.info("[JOIN-SFX] other audio active (%s) — skipping %s",
                     type(cur).__name__, name)
            return False

        # Otherwise it's idle, or only a finished/lingering join source remains.
        # Safely stop any leftover player (voice_recv-safe) so play() can restart —
        # this is what lets the same person trigger their sound again and again.
        if vc.is_playing() or vc.is_paused():
            log.debug("[JOIN-SFX] stopping lingering source before restart")
            _stop_voice_playback(vc)
            await asyncio.sleep(0.05)

        src = JoinSfxSource()
        src.add(frames)
        _join_sfx_sources[guild_id] = src
        try:
            log.debug("[JOIN-SFX] calling vc.play() with %d frames", len(frames))
            vc.play(src)
            log.debug("[JOIN-SFX] vc.play() succeeded")
        except Exception as e:
            # Most likely "Already playing" from a player still tearing down —
            # stop it the safe way and retry once.
            log.warning("[JOIN-SFX] play failed (%s) — stopping + retrying", e)
            _stop_voice_playback(vc)
            await asyncio.sleep(0.1)
            try:
                log.debug("[JOIN-SFX] retrying vc.play()")
                vc.play(src)
                log.debug("[JOIN-SFX] vc.play() retry succeeded")
            except Exception as e2:
                log.warning("[JOIN-SFX] retry failed: %s", e2)
                return False
        log.info("[JOIN-SFX] started standalone source: %s", name)
        return True


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
        if not pcm_data:
            # Silence frame — don't mark as speaking, but update packet time
            self.last_packet_time = now
            return
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
    if not STT_ENABLED:
        return None
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
    # Auto-detect language — supports Thai, English, and code-mixed speech
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
        self._voice_data_count = 0

    def start_processing(self):
        self._process_task = asyncio.create_task(self._process_loop())
        self._diag_counter = 0

    async def _process_loop(self):
        while self._running:
            try:
                await asyncio.sleep(1.0)
                now = time.monotonic()
                self._diag_counter += 1
                if self._diag_counter % 30 == 0:
                    bufs = {uid: (b.is_speaking, len(b.buffer), f"{b.silence_duration():.1f}s")
                            for uid, b in self.user_buffers.items()}
                    log.info("[VC DIAG] tick=%d voice_pkts=%d buffers=%s stt_locked=%s",
                             self._diag_counter, self._voice_data_count, bufs, self._stt_lock.locked())
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
                            f"🎙️ **{user_name}**: {text}\n💬 **Clawd**: {response}"
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
            rdj = get_radio_dj(self.guild_id)
            current = get_now_playing_track(self.bot.get_guild(int(self.guild_id)))
            now_playing = current.title if current else "nothing"

            if rdj.is_active:
                local_hour = (datetime.now(timezone.utc).hour + 7) % 24
                if 0 <= local_hour < 6:
                    time_vibe = "deep into the night"
                elif 6 <= local_hour < 12:
                    time_vibe = "morning"
                elif 12 <= local_hour < 17:
                    time_vibe = "afternoon"
                elif 17 <= local_hour < 21:
                    time_vibe = "evening"
                else:
                    time_vibe = "late night"
                system = (
                    "You are Claude, a warm indie radio host running a live show in a voice channel.\n"
                    "PERSONA:\n"
                    "- Calm, smooth, laid-back. Like a late-night radio host.\n"
                    "- Warm and genuine with your listeners. You know them.\n"
                    "- You talk about music, the vibe, the weather, life, whatever comes up.\n"
                    "- You engage with callers naturally — ask how their day was, react to what they say.\n"
                    "- You're passionate about music and love sharing discoveries.\n"
                    f"- Currently playing: {now_playing}\n"
                    f"- Time: {time_vibe}\n"
                    f"- Listener: {user_name}\n\n"
                    "RULES:\n"
                    "- MAX 2-3 sentences. You're on air, not writing an essay.\n"
                    "- No markdown, no emojis, no formatting. Pure spoken word.\n"
                    "- Stay in the radio-host role. Keep the relaxed on-air vibe.\n"
                    "- If they ask about a song, you know your music — talk about it.\n"
                    "- If they request a song, say you'll see if it fits the set.\n"
                )
            else:
                system = (
                    "You are Claude, a helpful assistant in a voice channel. RULES:\n"
                    "- MAX 1-2 sentences. You're speaking aloud, not typing.\n"
                    "- No markdown, no emojis, no formatting. Plain speech only.\n"
                    "- Be warm and concise. Don't over-explain. Don't ramble.\n"
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

        rdj = get_radio_dj(self.guild_id)
        is_radio = rdj.is_active

        if url_match and not is_radio:
            result = await play_music(guild, url_match.group(1), requester, self.text_channel,
                                      requester_id=requester_id)
            return result
        _music_hints = {"play", "song", "music", "skip", "stop", "next", "like", "dislike",
                        "hate", "love", "sucks", "banger", "random", "queue", "playing",
                        "request",
                        "เพลง", "เปิด", "ข้าม", "หยุด", "ชอบ", "ไม่ชอบ", "ห่วย", "เปลี่ยน", "เพราะ", "ดี"}
        lower = text.lower()
        might_be_music = has_music or any(w in lower for w in _music_hints)
        if not might_be_music:
            return None
        intent_data = await self._classify_music_intent(text)
        intent = intent_data.get("intent", "none")

        if is_radio:
            return await self._handle_radio_music_intent(
                intent, intent_data, current_track, requester, requester_id, guild
            )

        if intent == "like" and current_track and requester_id:
            loop = asyncio.get_event_loop()
            try:
                await loop.run_in_executor(None, record_feedback, requester_id, current_track.title, True)
            except Exception:
                pass
            return f"Noted, you like {current_track.title}. I'll remember that."
        if intent == "dislike" and current_track and requester_id:
            loop = asyncio.get_event_loop()
            try:
                await loop.run_in_executor(None, record_feedback, requester_id, current_track.title, False)
            except Exception:
                pass
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

    async def _handle_radio_music_intent(self, intent: str, intent_data: dict,
                                          current_track, requester: str,
                                          requester_id: Optional[str],
                                          guild: discord.Guild) -> Optional[str]:
        from provider import call_ai

        if intent == "like" and current_track and requester_id:
            loop = asyncio.get_event_loop()
            try:
                await loop.run_in_executor(None, record_feedback, requester_id, current_track.title, True)
            except Exception:
                pass
            return None  # Let _generate_response handle it in radio persona
        if intent == "dislike" and current_track and requester_id:
            loop = asyncio.get_event_loop()
            try:
                await loop.run_in_executor(None, record_feedback, requester_id, current_track.title, False)
            except Exception:
                pass
            await skip_music(guild)
            return None
        if intent == "skip":
            await skip_music(guild)
            return None
        if intent == "stop":
            await stop_radio(self.guild_id)
            await stop_music(guild)
            return "Alright, we're signing off. Thanks for tuning in."
        if intent == "queue":
            return None

        if intent == "play":
            query = intent_data.get("query", "")
            if not query:
                return None
            # A listener request is an explicit, strong signal — just honor it (no
            # accept/reject gate): play it right after the current song, preload it
            # for a gapless cut-in, and let the vibe adapt toward it.
            mq = get_music_queue(self.guild_id)
            track = await extract_track(query, f"{requester} (request)")
            if not track:
                return None
            # Priority: jump the queue so it plays next, after the current track.
            mq.queue.insert(0, track)
            # Preload: prebuffer its PCM now so the transition into it is gapless.
            try:
                if mq._mixer:
                    mq._mixer.prebuffer_next(track.stream_url)
            except Exception as e:
                log.debug("[RADIO] request prebuffer failed: %s", e)
            # Vibe adapt: steer upcoming auto-picks toward the requested song.
            record_radio_request(self.guild_id, track.title)
            log.info("[RADIO] Request prioritized + preloaded: %s", track.title)
            return None  # Let _generate_response acknowledge in radio persona

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
                await _feed_tts_with_timeout(mixer, mp3_data)
                log.info("Spoke in VC (mixed with music): %s", text[:60])
            except Exception as e:
                log.warning("TTS via mixer failed: %s", e)
            finally:
                _sync_web_mixer_tts(guild, active=False, text="", level=0.0)
        else:
            rdj = get_radio_dj(self.guild_id)
            if rdj.is_active:
                for _ in range(15):
                    await asyncio.sleep(0.2)
                    mq2 = get_music_queue(self.guild_id)
                    mixer2 = mq2._mixer
                    if mixer2 and (mixer2.has_music or _voice_client_has_mixer(vc, mixer2)):
                        try:
                            _sync_web_mixer_tts(guild, active=True, text=text, level=1.0)
                            await _feed_tts_with_timeout(mixer2, mp3_data)
                            log.info("Spoke in VC (waited for mixer): %s", text[:60])
                        except Exception as e:
                            log.warning("TTS via mixer (delayed) failed: %s", e)
                        finally:
                            _sync_web_mixer_tts(guild, active=False, text="", level=0.0)
                        return
                log.warning("Mixer never became ready during radio — skipping speech")
                return
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

    def on_voice_data(self, user, pcm_data: bytes):
        # voice_recv can deliver packets with user=None when the RTP SSRC hasn't
        # been mapped to a member yet (common in the first packets right after
        # joining). Dereferencing user.bot/.id here would raise AttributeError,
        # which crashes the PacketRouter thread and triggers its finally-block
        # stop_listening() — permanently killing audio reception for the session.
        if user is None or user.bot:
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

    async def flush_and_transcribe(self):
        """Transcribe whatever audio is still buffered, so the final (or only)
        utterance isn't lost when leaving without a trailing silence gap to
        trigger a normal harvest. MUST be awaited BEFORE the session transcript
        is saved (i.e. before end_vc_session)."""
        self._running = False
        if self._process_task:
            self._process_task.cancel()
        for uid, buf in list(self.user_buffers.items()):
            try:
                pcm = buf.harvest()  # None if below MIN_SPEECH_SEC of audio
                if pcm:
                    await self._handle_speech(uid, buf.user_name, pcm)
            except Exception as e:
                log.warning("[VC] final flush transcribe failed for %s: %s", buf.user_name, e)


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
    log.info("[VC] opus loaded: %s", discord.opus.is_loaded())
    try:
        if guild.voice_client:
            # If existing client isn't a VoiceRecvClient, disconnect and reconnect properly
            if not isinstance(guild.voice_client, voice_recv.VoiceRecvClient):
                log.warning("[VC] Existing voice client is %s, not VoiceRecvClient — reconnecting",
                            type(guild.voice_client).__name__)
                await guild.voice_client.disconnect(force=True)
                await asyncio.sleep(1)
                voice_client = await vc_channel.connect(cls=voice_recv.VoiceRecvClient)
            elif guild.voice_client.channel != vc_channel:
                await guild.voice_client.move_to(vc_channel)
                voice_client = guild.voice_client
            else:
                voice_client = guild.voice_client
        else:
            voice_client = await vc_channel.connect(cls=voice_recv.VoiceRecvClient)
    except discord.Forbidden:
        return "❌ No permission to join that voice channel"
    except Exception as e:
        return f"❌ Failed to connect: {e}"
    log.info("[VC] Voice client type: %s", type(voice_client).__name__)
    for _ in range(20):
        if voice_client.is_connected():
            break
        await asyncio.sleep(0.2)
    else:
        return "❌ Voice connection timed out"
    sink = BloodAudioSink(guild_id, vc_channel.name, bot_instance, text_channel)
    _active_sinks[guild_id] = sink
    start_vc_session(guild_id, vc_channel.name)
    _raw_cb_count = [0]
    def callback(user, data: voice_recv.VoiceData):
        _raw_cb_count[0] += 1
        if _raw_cb_count[0] in (1, 5, 20, 100):
            log.info("[VC] Raw callback #%d: user=%s pcm_len=%d",
                     _raw_cb_count[0], user, len(data.pcm) if data.pcm else 0)
        try:
            sink.on_voice_data(user, data.pcm)
        except Exception as e:
            # Never let one bad packet escape into the PacketRouter loop — an
            # uncaught exception there tears down the listener (stop_listening).
            log.warning("[VC] on_voice_data error (ignored to keep listener alive): %s", e)
    if STT_ENABLED:
        try:
            # Stop any existing listener first — prevents "Already receiving audio" on rejoin
            if hasattr(voice_client, 'is_listening') and voice_client.is_listening():
                voice_client.stop_listening()
            voice_client.listen(voice_recv.BasicSink(callback))
            log.info("[VC] Listener started on '%s' (voice_recv.BasicSink), is_listening=%s",
                     vc_channel.name, voice_client.is_listening() if hasattr(voice_client, 'is_listening') else '?')
        except Exception as e:
            err = str(e).lower()
            if "already receiving" in err or "already listening" in err:
                log.info("[VC] Listener already running on '%s' — continuing", vc_channel.name)
            else:
                log.error("[VC] Failed to start listener: %s", e)
                return f"❌ Failed to start listening: {e}"
        sink.start_processing()
    else:
        # STT disabled — join for TTS/music only. Don't attach the audio
        # receiver, so there's no opus decoding or transcription at all.
        if hasattr(voice_client, 'is_listening') and voice_client.is_listening():
            voice_client.stop_listening()
        log.info("[VC] STT disabled (STT_ENABLED=false) — joined '%s' for TTS/music only, not transcribing.",
                 vc_channel.name)
    try:
        import web_mixer as wm
        await wm.start_web_mixer()
        _bind_web_mixer_guild(guild_id, guild_name=guild.name)
        wm.update_state(
            guild_id=guild_id,
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
    if STT_ENABLED:
        return f"✅ Joined **{vc_channel.name}** — listening & recording. Say 'Blood' or 'hey Blood' to talk to me!"
    return f"✅ Joined **{vc_channel.name}** — playing music & TTS only (voice transcription is off)."


async def leave_voice(guild: discord.Guild) -> str:
    guild_id = str(guild.id)
    _intentional_leave.add(guild_id)
    sink = _active_sinks.pop(guild_id, None)
    if sink:
        # Transcribe any still-buffered speech BEFORE the session is saved,
        # otherwise a "join → talk → leave" with no trailing silence loses it.
        try:
            await sink.flush_and_transcribe()
        except Exception as e:
            log.warning("[VC] final transcript flush failed: %s", e)
        sink.stop()
    dj = get_random_dj(guild_id)
    if dj.is_active:
        dj.active = False
        dj.participants.clear()
    rdj = get_radio_dj(guild_id)
    if rdj.is_active:
        rdj.active = False
    mq = get_music_queue(guild_id)
    mq.cleanup_mixer()
    mq.clear()
    end_vc_session(guild_id)
    if guild.voice_client:
        await guild.voice_client.disconnect(force=True)
    _active_conversation.pop(guild_id, None)
    _unbind_web_mixer_guild(guild_id)
    async def _clear_flag():
        await asyncio.sleep(5)
        _intentional_leave.discard(guild_id)
    asyncio.create_task(_clear_flag())
    return "✅ Left voice channel. Transcript saved."
