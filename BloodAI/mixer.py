"""
Blood Audio Mixer — thread-based real-time mixing for Discord.

Features:
  - Real-time music + TTS mixing with auto-ducking
  - Crossfade transitions between tracks
  - Audio effects: speed, bass boost, nightcore, slowed+reverb, 8D
  - Gapless playback via pre-buffering
"""

import array
import asyncio
import logging
import subprocess
import threading
import time
from collections import deque
from typing import Optional, Callable

import discord

log = logging.getLogger("blood.mixer")

FRAME_SIZE = 3840
NUM_SAMPLES = FRAME_SIZE // 2
SILENCE = b'\x00' * FRAME_SIZE

# Fade rates — applied once per read() call (every 20 ms)
_DUCK_RATE        = 0.030   # music fades DOWN when speech starts  → full duck in ~0.4 s
_UNDUCK_RATE      = 0.018   # music fades UP   when speech ends    → full unduck in ~0.7 s
_SONG_FADEIN_RATE = 0.025   # new-song fade-in from silence        → full vol in ~0.5 s
_PRE_FADE_FRAMES  = 25      # start unduck this many frames before TTS buffer empties (~500 ms)
_CROSSFADE_RATE   = 0.012   # crossfade out speed — ~1.3s to fade from 0.8→0


def _scale_pcm(pcm: bytes, volume: float) -> bytes:
    if volume >= 0.99:
        return pcm
    a = array.array('h')
    a.frombytes(pcm[:FRAME_SIZE])
    for i in range(len(a)):
        a[i] = max(-32768, min(32767, int(a[i] * volume)))
    return a.tobytes()


def _mix_two(pcm_a: bytes, vol_a: float, pcm_b: bytes, vol_b: float) -> bytes:
    if len(pcm_a) < FRAME_SIZE:
        pcm_a += b'\x00' * (FRAME_SIZE - len(pcm_a))
    if len(pcm_b) < FRAME_SIZE:
        pcm_b += b'\x00' * (FRAME_SIZE - len(pcm_b))
    a = array.array('h')
    a.frombytes(pcm_a[:FRAME_SIZE])
    b = array.array('h')
    b.frombytes(pcm_b[:FRAME_SIZE])
    n = min(len(a), len(b))
    out = array.array('h', [0] * n)
    for i in range(n):
        v = int(a[i] * vol_a) + int(b[i] * vol_b)
        if v > 32767:
            v = 32767
        elif v < -32768:
            v = -32768
        out[i] = v
    return out.tobytes()


# ── Audio effects: FFmpeg filter builders ────────────────────────────────────

EFFECTS = {
    "none":     "",
    "bass":     "bass=g={bass_db}:f=110:w=0.6",
    "nightcore": "asetrate=48000*1.25,aresample=48000",
    "slowed":   "atempo=0.85,aecho=0.8:0.88:60:0.4",
    "8d":       "apulsator=hz=0.125:amount=0.7",
    "vapor":    "atempo=0.8,aecho=0.8:0.85:40:0.5",
    "treble":   "treble=g={treble_db}:f=3000:w=0.5",
    "deep":     "bass=g=8:f=80:w=0.5,atempo=0.92",
}


def build_filter_chain(*, speed: float = 1.0, bass_db: int = 0,
                        treble_db: int = 0, effect: str = "none") -> str:
    parts = []

    if effect and effect != "none" and effect in EFFECTS:
        tpl = EFFECTS[effect]
        parts.append(tpl.format(bass_db=bass_db or 10, treble_db=treble_db or 6))

    if bass_db and effect not in ("bass", "deep"):
        parts.append(f"bass=g={bass_db}:f=110:w=0.6")

    if treble_db and effect != "treble":
        parts.append(f"treble=g={treble_db}:f=3000:w=0.5")

    if speed != 1.0 and effect not in ("nightcore", "slowed", "vapor", "deep"):
        s = max(0.5, min(2.0, speed))
        if s <= 0.5:
            parts.append("atempo=0.5")
        elif s >= 2.0:
            parts.append("atempo=2.0")
        else:
            parts.append(f"atempo={s:.3f}")

    return ",".join(p for p in parts if p)


class BloodMixerSource(discord.AudioSource):
    """Discord AudioSource that mixes music + TTS in real-time."""

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop

        self._music_buf: deque = deque()
        self._tts_buf: deque = deque()

        self._music_vol = 0.8
        self._duck_vol = 0.15
        self._effective_vol = 0.0

        self._tts_active = False
        self._tts_lock = threading.Lock()
        self._has_music = False
        self._force_duck = False
        self._duck_since: float = 0.0  # monotonic time ducking started
        self._DUCK_TIMEOUT = 45.0

        self._music_proc: Optional[subprocess.Popen] = None
        self._music_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._on_music_end: Optional[Callable] = None

        self._last_read_time = time.monotonic()

        self._read_call_count = 0
        self._buf_had_data_once = False
        self._silence_streak = 0

        # Pre-buffer for gapless transitions
        self._prebuf: deque = deque()
        self._prebuf_url: Optional[str] = None
        self._prebuf_proc: Optional[subprocess.Popen] = None
        self._prebuf_thread: Optional[threading.Thread] = None
        self._prebuf_stop = threading.Event()
        self._prebuf_ready = threading.Event()

        # Crossfade: old track fading out while new one fades in
        self._fadeout_buf: deque = deque()
        self._fadeout_vol: float = 0.0
        self._fadeout_proc: Optional[subprocess.Popen] = None
        self._fadeout_thread: Optional[threading.Thread] = None
        self._fadeout_stop: Optional[threading.Event] = None

        # Audio effects
        self._speed: float = 1.0
        self._bass_db: int = 0
        self._treble_db: int = 0
        self._effect: str = "none"

    # ── Properties ���──────────────────────────────────────────────────────────

    @property
    def has_music(self) -> bool:
        return self._has_music

    @property
    def force_duck(self) -> bool:
        return self._force_duck

    @property
    def tts_active(self) -> bool:
        return self._tts_active or bool(self._tts_buf)

    @property
    def is_ducking(self) -> bool:
        return self._force_duck or self.tts_active

    @property
    def is_crossfading(self) -> bool:
        return self._fadeout_vol > 0.0 and bool(self._fadeout_buf)

    # ── Volume / Effects ──────────────────────────────���──────────────────────

    def set_music_volume(self, vol: float):
        self._music_vol = max(0.0, min(2.0, vol))

    def _push_unduck_to_web(self):
        try:
            from web_mixer import update_state
            update_state(tts_active=False, tts_text="", tts_level=0.0,
                         ducked=False, force_duck=False)
        except Exception:
            pass

    def set_force_duck(self, enabled: bool):
        self._force_duck = bool(enabled)
        if not enabled:
            # Also clear any stuck TTS state so ducking fully releases
            self._tts_active = False
            self._tts_buf.clear()
            self._duck_since = 0.0

    def set_speed(self, factor: float):
        self._speed = max(0.5, min(2.0, factor))

    def set_bass_boost(self, db: int):
        self._bass_db = max(0, min(20, db))

    def set_treble_boost(self, db: int):
        self._treble_db = max(0, min(15, db))

    def set_effect(self, name: str):
        self._effect = name if name in EFFECTS else "none"

    def clear_effects(self):
        self._speed = 1.0
        self._bass_db = 0
        self._treble_db = 0
        self._effect = "none"

    def get_effects_info(self) -> dict:
        return {
            "speed": self._speed,
            "bass_db": self._bass_db,
            "treble_db": self._treble_db,
            "effect": self._effect,
            "filter_chain": self._current_filter_chain(),
        }

    def _current_filter_chain(self) -> str:
        return build_filter_chain(
            speed=self._speed, bass_db=self._bass_db,
            treble_db=self._treble_db, effect=self._effect,
        )

    # ── FFmpeg command builder ───────────────────────────────────────────────

    def _ffmpeg_cmd(self, stream_url: str, *, pipe_input: bool = False) -> list[str]:
        cmd = ["ffmpeg"]
        if not pipe_input:
            cmd += ["-reconnect", "1", "-reconnect_streamed", "1",
                    "-reconnect_delay_max", "5"]
        cmd += ["-i", "pipe:0" if pipe_input else stream_url]

        af = self._current_filter_chain()
        if af:
            cmd += ["-af", af]

        cmd += ["-f", "s16le", "-ar", "48000", "-ac", "2",
                "-loglevel", "error", "-nostats", "pipe:1"]
        return cmd

    # ── Pre-buffer ───────────────��─────────────────────────��─────────────────

    def prebuffer_next(self, stream_url: str):
        """Pre-start FFmpeg and buffer ~5s of audio for the next track."""
        self._cancel_prebuffer()
        self._prebuf_url = stream_url
        self._prebuf_stop = threading.Event()
        self._prebuf_ready = threading.Event()

        def _prefetch():
            stop = self._prebuf_stop
            proc = None
            try:
                proc = subprocess.Popen(
                    self._ffmpeg_cmd(stream_url),
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                count = 0
                while not stop.is_set() and count < 250:
                    chunk = proc.stdout.read(FRAME_SIZE)
                    if not chunk:
                        break
                    if len(chunk) < FRAME_SIZE:
                        chunk += b'\x00' * (FRAME_SIZE - len(chunk))
                    self._prebuf.append(chunk)
                    count += 1
                if not stop.is_set() and count > 0:
                    self._prebuf_proc = proc
                    self._prebuf_ready.set()
                    log.info("[MIXER] Pre-buffered %d frames for next track", count)
                else:
                    if proc:
                        try:
                            proc.kill()
                        except Exception:
                            pass
            except Exception as e:
                log.warning("[MIXER] Pre-buffer failed: %s", e)
                if proc:
                    try:
                        proc.kill()
                    except Exception:
                        pass

        self._prebuf_thread = threading.Thread(
            target=_prefetch, daemon=True, name="blood-prebuf"
        )
        self._prebuf_thread.start()

    def _cancel_prebuffer(self):
        self._prebuf_stop.set()
        if self._prebuf_thread and self._prebuf_thread.is_alive():
            self._prebuf_thread.join(timeout=2)
        self._prebuf_thread = None
        if self._prebuf_proc:
            try:
                self._prebuf_proc.kill()
            except Exception:
                pass
            self._prebuf_proc = None
        self._prebuf.clear()
        self._prebuf_url = None
        self._prebuf_ready.clear()

    # ── Crossfade ───────���──────────────────────────────���─────────────────────

    def _kill_fadeout(self):
        if self._fadeout_stop:
            self._fadeout_stop.set()
        if self._fadeout_proc:
            try:
                self._fadeout_proc.stdout.close()
            except Exception:
                pass
            try:
                self._fadeout_proc.kill()
            except Exception:
                pass
            self._fadeout_proc = None
        if self._fadeout_thread and self._fadeout_thread.is_alive():
            self._fadeout_thread.join(timeout=1)
        self._fadeout_thread = None
        self._fadeout_buf.clear()
        self._fadeout_vol = 0.0
        self._fadeout_stop = None

    def crossfade_to(self, stream_url: str, volume: float = 0.8,
                     on_end: Optional[Callable] = None):
        """Start a new track while crossfading out the current one.

        The old track's remaining buffered audio fades out over ~1.3s
        while the new track fades in from silence.
        """
        self._kill_fadeout()

        # Snapshot current state into fadeout
        self._fadeout_buf = deque(self._music_buf)
        self._fadeout_vol = self._effective_vol if self._effective_vol > 0.01 else self._music_vol
        self._fadeout_proc = self._music_proc
        self._fadeout_thread = self._music_thread
        self._fadeout_stop = self._stop_event

        # Detach old stream so stop_music doesn't kill the fadeout
        self._music_proc = None
        self._music_thread = None
        self._stop_event = threading.Event()

        # Tell old reader thread to stop feeding new frames into _music_buf
        # (fadeout buf already has the snapshot; the thread may add a few more
        #  to _music_buf before it sees the stop — that's fine, start_music
        #  clears _music_buf anyway.)
        if self._fadeout_stop:
            self._fadeout_stop.set()

        # Start new track — this clears _music_buf and begins fade-in from 0
        self._music_buf.clear()
        self._on_music_end = on_end
        self._has_music = True
        self._stop_event = threading.Event()
        self._buf_had_data_once = False
        self._silence_streak = 0
        self._effective_vol = 0.0

        prebuf_frames = None
        prebuf_proc = None
        if (self._prebuf_url == stream_url
                and self._prebuf_ready.is_set()
                and self._prebuf
                and self._prebuf_proc):
            prebuf_frames = list(self._prebuf)
            prebuf_proc = self._prebuf_proc
            self._prebuf.clear()
            self._prebuf_url = None
            self._prebuf_proc = None
            self._prebuf_ready.clear()
            log.info("[MIXER] Crossfade gapless: using %d pre-buffered frames", len(prebuf_frames))
        else:
            self._cancel_prebuffer()

        self._start_reader(stream_url, prebuf_frames, prebuf_proc)
        log.info("[MIXER] Crossfade started → %s...", stream_url[:60])

    # ── Music playback ───────────────���───────────────────────────────────────

    def _start_reader(self, stream_url: str,
                      prebuf_frames: Optional[list] = None,
                      prebuf_proc: Optional[subprocess.Popen] = None):
        stop = self._stop_event

        def _reader():
            proc = prebuf_proc
            chunk_counter = 0
            try:
                if prebuf_frames:
                    for chunk in prebuf_frames:
                        if stop.is_set():
                            break
                        self._music_buf.append(chunk)
                        chunk_counter += 1

                if proc is None:
                    proc = subprocess.Popen(
                        self._ffmpeg_cmd(stream_url),
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    )
                self._music_proc = proc
                while not stop.is_set():
                    while len(self._music_buf) > 250 and not stop.is_set():
                        time.sleep(0.05)
                    if stop.is_set():
                        break
                    chunk = proc.stdout.read(FRAME_SIZE)
                    if not chunk:
                        if not stop.is_set():
                            log.info("Music stream ended (FFmpeg returned empty, chunks=%d)", chunk_counter)
                        break
                    if len(chunk) < FRAME_SIZE:
                        chunk += b'\x00' * (FRAME_SIZE - len(chunk))
                    self._music_buf.append(chunk)
                    chunk_counter += 1
                    if chunk_counter == 1:
                        log.info("[AUDIO] First chunk from FFmpeg: %d bytes, buf_size=%d", len(chunk), len(self._music_buf))
                    elif chunk_counter % 100 == 0:
                        log.info("[AUDIO] FFmpeg chunk #%d, buf_size=%d", chunk_counter, len(self._music_buf))
                proc.stdout.close()
                try:
                    stderr_data = proc.stderr.read()
                    if stderr_data:
                        err_text = stderr_data.decode('utf-8', errors='ignore').strip()
                        if err_text and not stop.is_set():
                            log.error("[FFMPEG ERROR] %s", err_text[:500])
                except Exception:
                    pass
                finally:
                    proc.stderr.close()
                proc.wait()
                exit_code = proc.returncode
                if exit_code and exit_code != 0 and not stop.is_set():
                    log.warning("FFmpeg exited with code %d (stream may have died)", exit_code)
            except Exception as e:
                if not stop.is_set():
                    log.warning("Music feed error: %s", e)
            finally:
                if not stop.is_set():
                    drain_start = time.monotonic()
                    while self._music_buf and not stop.is_set():
                        time.sleep(0.05)
                        if time.monotonic() - drain_start > 30:
                            log.warning("Buffer drain timeout (30s) — forcing end")
                            break
                if self._stop_event is stop:
                    self._has_music = False
                if proc:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                cb = self._on_music_end
                if cb and not stop.is_set():
                    log.info("Track ended naturally — firing on_track_end callback")
                    try:
                        future = asyncio.run_coroutine_threadsafe(cb(), self._loop)
                        future.add_done_callback(_log_end)
                    except Exception as e:
                        log.warning("on_track_end callback failed: %s", e)

        def _log_end(future):
            try:
                future.result()
            except Exception as e:
                log.warning("on_track_end async result failed: %s", e)

        self._music_thread = threading.Thread(
            target=_reader, daemon=True, name="blood-music-feed"
        )
        self._music_thread.start()

    def start_music(self, stream_url: str, volume: float = 0.8,
                    on_end: Optional[Callable] = None):
        """Start streaming music from URL in a background thread."""
        self.stop_music()

        self._music_vol = volume
        self._on_music_end = on_end
        self._has_music = True
        self._stop_event = threading.Event()
        self._buf_had_data_once = False
        self._silence_streak = 0
        self._effective_vol = 0.0

        prebuf_frames = None
        prebuf_proc = None
        if (self._prebuf_url == stream_url
                and self._prebuf_ready.is_set()
                and self._prebuf
                and self._prebuf_proc):
            prebuf_frames = list(self._prebuf)
            prebuf_proc = self._prebuf_proc
            self._prebuf.clear()
            self._prebuf_url = None
            self._prebuf_proc = None
            self._prebuf_ready.clear()
            log.info("[MIXER] Gapless: using %d pre-buffered frames", len(prebuf_frames))
        else:
            self._cancel_prebuffer()

        self._start_reader(stream_url, prebuf_frames, prebuf_proc)
        if prebuf_frames:
            log.info("Music feed started (gapless, %d pre-buffered): %s...",
                     len(prebuf_frames), stream_url[:60])
        else:
            log.info("Music feed thread started for: %s...", stream_url[:60])

    def stop_music(self):
        """Stop current music stream and clear buffer."""
        self._stop_event.set()
        self._has_music = False
        self._force_duck = False
        self._music_buf.clear()
        self._cancel_prebuffer()
        self._kill_fadeout()

        if self._music_proc:
            try:
                self._music_proc.stdout.close()
            except Exception:
                pass
            try:
                self._music_proc.kill()
            except Exception:
                pass
            self._music_proc = None

        if self._music_thread and self._music_thread.is_alive():
            self._music_thread.join(timeout=2)
            if self._music_thread.is_alive():
                log.warning("Music feed thread did not exit in 2s")
        self._music_thread = None
        self._music_buf.clear()

    # ── TTS ──────��────────────────────────────────��──────────────────────────

    def feed_tts_sync(self, mp3_data: bytes):
        """Convert MP3→PCM, feed into TTS buffer, block until drained."""
        log.info("[MIXER] feed_tts_sync called with %d bytes of MP3", len(mp3_data))
        with self._tts_lock:
            self._tts_active = True
            self._duck_since = 0.0
            try:
                proc = subprocess.Popen(
                    ["ffmpeg", "-i", "pipe:0",
                     "-f", "s16le", "-ar", "48000", "-ac", "2",
                     "-loglevel", "quiet", "pipe:1"],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL
                )
                # Use communicate() to avoid pipe deadlock: writing 137KB MP3
                # can deadlock if FFmpeg's stdout pipe fills before we finish
                # writing to stdin.
                pcm_data, _ = proc.communicate(input=mp3_data, timeout=30)

                frame_count = 0
                offset = 0
                while offset < len(pcm_data):
                    chunk = pcm_data[offset:offset + FRAME_SIZE]
                    if len(chunk) < FRAME_SIZE:
                        chunk += b'\x00' * (FRAME_SIZE - len(chunk))
                    self._tts_buf.append(chunk)
                    frame_count += 1
                    offset += FRAME_SIZE
                log.info("[MIXER] TTS buffer filled: %d frames (%.1fs of audio)",
                         frame_count, frame_count * 0.02)
            except Exception as e:
                log.warning("TTS feed error: %s", e)
                self._tts_buf.clear()
                self._tts_active = False
                return

            try:
                drain_start = time.monotonic()
                while self._tts_buf:
                    time.sleep(0.02)
                    elapsed = time.monotonic() - drain_start
                    if elapsed > 40:
                        log.warning("[MIXER] TTS drain timeout (%.0fs) — remaining: %d frames",
                                    elapsed, len(self._tts_buf))
                        self._tts_buf.clear()
                        break
                    if time.monotonic() - self._last_read_time > 1.0:
                        log.warning("[MIXER] AudioPlayer stopped reading — clearing TTS (%d frames left)",
                                    len(self._tts_buf))
                        self._tts_buf.clear()
                        break
                drain_time = time.monotonic() - drain_start
                log.info("[MIXER] TTS drained in %.1fs", drain_time)
                time.sleep(0.1)
            finally:
                self._tts_buf.clear()
                self._tts_active = False

    # ── read() — the hot path ──────────────────────────────────────��─────────

    def read(self) -> bytes:
        """Called by Discord's AudioPlayer thread every 20ms."""
        self._last_read_time = time.monotonic()
        self._read_call_count += 1

        # Pop frames (thread-safe via try/except instead of check-then-pop)
        try:
            music = self._music_buf.popleft()
        except IndexError:
            music = None
        try:
            tts = self._tts_buf.popleft()
        except IndexError:
            tts = None
        try:
            fadeout = self._fadeout_buf.popleft()
        except IndexError:
            fadeout = None

        # Log first music chunk
        if music and not self._buf_had_data_once:
            log.info("[AUDIO] First music chunk delivered to Discord (read #%d)", self._read_call_count)

        # Watchdog
        if self._has_music and not self._stop_event.is_set():
            if music:
                self._buf_had_data_once = True
                self._silence_streak = 0
            else:
                self._silence_streak += 1
                if self._silence_streak > 150 and not self._buf_had_data_once:
                    log.error("[AUDIO WATCHDOG] %d read() calls, music_buf always empty — FFmpeg failed?", self._read_call_count)
                    self._silence_streak = -999999
                elif self._silence_streak > 250 and self._buf_had_data_once:
                    log.error("[AUDIO WATCHDOG] No music data for 5+ seconds — stream died?")
                    self._silence_streak = -999999

        # ── Crossfade: step fadeout volume down each frame ───────────────────
        if fadeout:
            self._fadeout_vol = max(0.0, self._fadeout_vol - _CROSSFADE_RATE)
            if self._fadeout_vol <= 0.005:
                self._fadeout_buf.clear()
                self._kill_fadeout()
                fadeout = None

        # ── Ducking timeout: force-unduck if stuck for too long ────────────
        ducking = self.is_ducking
        now = time.monotonic()
        if ducking:
            if self._duck_since == 0.0:
                self._duck_since = now
            elif now - self._duck_since > self._DUCK_TIMEOUT:
                log.warning("[MIXER] Ducking stuck for %.0fs — forcing unduck",
                            now - self._duck_since)
                self._tts_active = False
                self._tts_buf.clear()
                self._force_duck = False
                self._duck_since = 0.0
                ducking = False
                self._push_unduck_to_web()
        else:
            self._duck_since = 0.0

        tts_frames_left = len(self._tts_buf)
        pre_fading = ducking and 0 < tts_frames_left <= _PRE_FADE_FRAMES
        target_vol = self._duck_vol if (ducking and not pre_fading) else self._music_vol

        if self._effective_vol < target_vol:
            rate = _UNDUCK_RATE if ducking or pre_fading else _SONG_FADEIN_RATE
            self._effective_vol = min(target_vol, self._effective_vol + rate)
        elif self._effective_vol > target_vol:
            self._effective_vol = max(target_vol, self._effective_vol - _DUCK_RATE)

        mvol = self._effective_vol

        # ── Mix all sources ──────────────────────────────────────────────────
        # 1. Combine fadeout + new music into one "music" frame
        if fadeout and music:
            music = _mix_two(fadeout, self._fadeout_vol, music, mvol)
            mvol = 1.0  # already scaled
        elif fadeout and not music:
            music = _scale_pcm(fadeout, self._fadeout_vol)
            mvol = 1.0

        # 2. No audio at all
        if music is None and tts is None:
            return SILENCE

        # 3. Mix music + TTS
        if music and tts:
            return _mix_two(music, mvol, tts, 1.0)
        elif music:
            if self._read_call_count <= 5:
                is_silent = all(b == 0 for b in music[:100])
                log.info("[AUDIO] Music read #%d: vol=%.3f, ducking=%s, pre_fade=%s, silent=%s",
                         self._read_call_count, mvol, ducking, pre_fading, is_silent)
            if mvol >= 0.99:
                return music
            return _scale_pcm(music, mvol)
        else:
            return tts

    # ── Cleanup ��─────────────────────────────��───────────────────────────────

    def is_opus(self) -> bool:
        return False

    def cleanup(self):
        """Called when Discord stops playing this source."""
        self._cancel_prebuffer()
        self._kill_fadeout()
        self.stop_music()
        self._tts_buf.clear()
        self._tts_active = False
