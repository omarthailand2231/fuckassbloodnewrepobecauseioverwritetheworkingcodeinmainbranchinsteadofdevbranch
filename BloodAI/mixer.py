"""
Blood Audio Mixer — thread-based real-time mixing for Discord.
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


class BloodMixerSource(discord.AudioSource):
    """Discord AudioSource that mixes music + TTS in real-time."""

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop

        self._music_buf: deque = deque()
        self._tts_buf: deque = deque()

        self._music_vol = 0.8
        self._duck_vol = 0.15
        self._effective_vol = 0.0   # Actual volume applied — fades smoothly toward target

        self._tts_active = False
        self._tts_lock = threading.Lock()
        self._has_music = False
        self._force_duck = False

        self._music_proc: Optional[subprocess.Popen] = None
        self._music_thread: Optional[threading.Thread] = None
        # Use an Event instead of a bool flag — avoids the reset race condition.
        self._stop_event = threading.Event()
        self._on_music_end: Optional[Callable] = None

        self._last_read_time = time.monotonic()

        # Audio flow watchdog: detect if FFmpeg isn't producing data
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

    def set_music_volume(self, vol: float):
        self._music_vol = max(0.0, min(2.0, vol))

    def set_force_duck(self, enabled: bool):
        self._force_duck = bool(enabled)

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
                    ["ffmpeg",
                     "-reconnect", "1", "-reconnect_streamed", "1",
                     "-reconnect_delay_max", "5",
                     "-i", stream_url,
                     "-f", "s16le", "-ar", "48000", "-ac", "2",
                     "-loglevel", "error", "-nostats", "pipe:1"],
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
        """Cancel any running pre-buffer."""
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
        self._effective_vol = 0.0   # Fade in from silence on every new track

        # Check for pre-buffered data matching this URL
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

        def _reader():
            proc = prebuf_proc
            stop = self._stop_event
            chunk_counter = 0
            try:
                # Dump pre-buffered frames first (instant — no gap)
                if prebuf_frames:
                    for chunk in prebuf_frames:
                        if stop.is_set():
                            break
                        self._music_buf.append(chunk)
                        chunk_counter += 1

                # Start new FFmpeg or continue with pre-buffered proc
                if proc is None:
                    proc = subprocess.Popen(
                        ["ffmpeg",
                         "-reconnect", "1", "-reconnect_streamed", "1",
                         "-reconnect_delay_max", "5",
                         "-i", stream_url,
                         "-f", "s16le", "-ar", "48000", "-ac", "2",
                         "-loglevel", "error", "-nostats",
                         "pipe:1"],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE
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
                        future.add_done_callback(_log_track_end_result)
                    except Exception as e:
                        log.warning("on_track_end callback failed: %s", e)

        def _log_track_end_result(future):
            try:
                future.result()
            except Exception as e:
                log.warning("on_track_end async result failed: %s", e)

        self._music_thread = threading.Thread(
            target=_reader, daemon=True, name="blood-music-feed"
        )
        self._music_thread.start()
        if prebuf_frames:
            log.info("Music feed started (gapless, %d pre-buffered): %s...",
                     len(prebuf_frames), stream_url[:60])
        else:
            log.info("Music feed thread started for: %s...", stream_url[:60])

    def stop_music(self):
        """Stop current music stream and clear buffer. Blocks until thread exits."""
        self._stop_event.set()
        self._has_music = False
        self._force_duck = False
        self._music_buf.clear()
        self._cancel_prebuffer()

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
        # Clear again — thread may have written frames between kill and join
        self._music_buf.clear()
        # NOTE: do NOT reset _stop_event here. start_music() creates a fresh one.

    def feed_tts_sync(self, mp3_data: bytes):
        """Convert MP3→PCM, feed into TTS buffer, block until drained."""
        with self._tts_lock:
            self._tts_active = True
            try:
                proc = subprocess.Popen(
                    ["ffmpeg", "-i", "pipe:0",
                     "-f", "s16le", "-ar", "48000", "-ac", "2",
                     "-loglevel", "quiet", "pipe:1"],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL
                )
                proc.stdin.write(mp3_data)
                proc.stdin.close()

                while True:
                    chunk = proc.stdout.read(FRAME_SIZE)
                    if not chunk:
                        break
                    if len(chunk) < FRAME_SIZE:
                        chunk += b'\x00' * (FRAME_SIZE - len(chunk))
                    self._tts_buf.append(chunk)
                proc.stdout.close()
                proc.wait()
            except Exception as e:
                log.warning("TTS feed error: %s", e)
                self._tts_buf.clear()

            try:
                drain_start = time.monotonic()
                while self._tts_buf:
                    time.sleep(0.02)
                    if time.monotonic() - drain_start > 15:
                        log.warning("[MIXER] TTS drain timeout — clearing stuck buffer")
                        self._tts_buf.clear()
                        break
                    if time.monotonic() - self._last_read_time > 1.0:
                        log.warning("[MIXER] AudioPlayer stopped — clearing TTS buffer")
                        self._tts_buf.clear()
                        break
                time.sleep(0.1)
            finally:
                # Always clear ducking state — never leave music stuck at duck volume
                self._tts_buf.clear()
                self._tts_active = False

    def read(self) -> bytes:
        """Called by Discord's AudioPlayer thread every 20ms."""
        self._last_read_time = time.monotonic()
        self._read_call_count += 1
        music = self._music_buf.popleft() if self._music_buf else None
        tts = self._tts_buf.popleft() if self._tts_buf else None

        # Log first music chunk delivered to Discord
        if music and not self._buf_had_data_once:
            log.info("[AUDIO] First music chunk delivered to Discord (read #%d)", self._read_call_count)

        # Watchdog: detect silent audio flow (FFmpeg dead but Discord still calling read)
        if self._has_music and not self._stop_event.is_set():
            if music:
                self._buf_had_data_once = True
                self._silence_streak = 0
            else:
                self._silence_streak += 1
                # If we've been returning silence for 3+ seconds (150 calls at 20ms each)
                # and never had data, FFmpeg likely failed to start
                if self._silence_streak > 150 and not self._buf_had_data_once:
                    log.error("[AUDIO WATCHDOG] %d read() calls, music_buf always empty — FFmpeg failed?", self._read_call_count)
                    self._silence_streak = -999999  # Prevent spam
                # If we HAD data but now don't for 5+ seconds, stream died mid-playback
                elif self._silence_streak > 250 and self._buf_had_data_once:
                    log.error("[AUDIO WATCHDOG] No music data for 5+ seconds — stream died?")
                    self._silence_streak = -999999

        ducking = self.is_ducking
        tts_frames_left = len(self._tts_buf)

        # Pre-fade: when TTS buffer is nearly empty, start fading music back up
        # early so the unduck is already in progress when the last word ends.
        pre_fading = ducking and 0 < tts_frames_left <= _PRE_FADE_FRAMES

        # Determine target volume
        target_vol = self._duck_vol if (ducking and not pre_fading) else self._music_vol

        # Smooth fade: step _effective_vol toward target each frame
        if self._effective_vol < target_vol:
            rate = _UNDUCK_RATE if ducking or pre_fading else _SONG_FADEIN_RATE
            self._effective_vol = min(target_vol, self._effective_vol + rate)
        elif self._effective_vol > target_vol:
            self._effective_vol = max(target_vol, self._effective_vol - _DUCK_RATE)

        mvol = self._effective_vol

        if music is None and tts is None:
            return SILENCE

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

    def is_opus(self) -> bool:
        return False

    def cleanup(self):
        """Called when Discord stops playing this source."""
        self._cancel_prebuffer()
        self.stop_music()
        self._tts_buf.clear()
        self._tts_active = False