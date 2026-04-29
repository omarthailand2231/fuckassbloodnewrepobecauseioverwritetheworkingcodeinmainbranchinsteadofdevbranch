"""
Blood Audio Mixer — thread-based real-time mixing for Discord.

Architecture:
- Discord's AudioPlayer calls read() every 20ms from its own thread
- Music: FFmpeg subprocess → reader thread → deque
- TTS: FFmpeg subprocess → reader thread → deque
- read() pops from both deques, mixes PCM, returns to Discord
- Auto-ducks music when TTS is active
- No async loops — timing is guaranteed by Discord's AudioPlayer thread
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

# 20ms @ 48kHz stereo 16-bit
FRAME_SIZE = 3840
NUM_SAMPLES = FRAME_SIZE // 2  # 1920 signed 16-bit samples
SILENCE = b'\x00' * FRAME_SIZE


def _scale_pcm(pcm: bytes, volume: float) -> bytes:
    """Scale PCM frame by volume (0.0–1.0)."""
    if volume >= 0.99:
        return pcm
    a = array.array('h')
    a.frombytes(pcm[:FRAME_SIZE])
    for i in range(len(a)):
        a[i] = max(-32768, min(32767, int(a[i] * volume)))
    return a.tobytes()


def _mix_two(pcm_a: bytes, vol_a: float, pcm_b: bytes, vol_b: float) -> bytes:
    """Mix two PCM frames with individual volume scaling."""
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
    """Discord AudioSource that mixes music + TTS in real-time.

    Mixing happens inside read(), called by Discord's AudioPlayer thread.
    No async loops needed — timing is guaranteed by Discord's own thread.

    Music feeds from a background thread (FFmpeg stdout).
    TTS feeds synchronously via feed_tts_sync() (call from run_in_executor).
    Auto-ducks music when TTS is active.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop

        # Audio buffers (thread-safe deques, no maxlen = no frame drops)
        self._music_buf: deque = deque()
        self._tts_buf: deque = deque()

        # Volume
        self._music_vol = 0.8
        self._duck_vol = 0.15

        # State
        self._tts_active = False
        self._tts_lock = threading.Lock()
        self._has_music = False

        # Music subprocess
        self._music_proc: Optional[subprocess.Popen] = None
        self._music_thread: Optional[threading.Thread] = None
        self._music_stopping = False
        self._on_music_end: Optional[Callable] = None

    @property
    def has_music(self) -> bool:
        return self._has_music

    def set_music_volume(self, vol: float):
        self._music_vol = max(0.0, min(2.0, vol))

    def start_music(self, stream_url: str, volume: float = 0.8,
                    on_end: Optional[Callable] = None):
        """Start streaming music from URL in a background thread.
        on_end: async callable invoked when track finishes naturally."""
        self.stop_music()
        self._music_vol = volume
        self._on_music_end = on_end
        self._has_music = True
        self._music_stopping = False

        def _reader():
            proc = None
            try:
                proc = subprocess.Popen(
                    ["ffmpeg",
                     "-reconnect", "1", "-reconnect_streamed", "1",
                     "-reconnect_delay_max", "5",
                     "-i", stream_url,
                     "-f", "s16le", "-ar", "48000", "-ac", "2",
                     "-loglevel", "quiet", "pipe:1"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
                )
                self._music_proc = proc
                while not self._music_stopping:
                    # Back-pressure: don't read too far ahead (~5s buffer)
                    while len(self._music_buf) > 250 and not self._music_stopping:
                        time.sleep(0.05)
                    if self._music_stopping:
                        break
                    chunk = proc.stdout.read(FRAME_SIZE)
                    if not chunk:
                        break
                    if len(chunk) < FRAME_SIZE:
                        chunk += b'\x00' * (FRAME_SIZE - len(chunk))
                    self._music_buf.append(chunk)
                proc.stdout.close()
                proc.wait()
            except Exception as e:
                if not self._music_stopping:
                    log.warning("Music feed error: %s", e)
            finally:
                # Wait for remaining buffer to drain before signaling end
                # (prevents cutting off the last few seconds of the song)
                if not self._music_stopping:
                    while self._music_buf and not self._music_stopping:
                        time.sleep(0.05)
                self._has_music = False
                if proc:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                cb = self._on_music_end
                if cb and not self._music_stopping:
                    try:
                        asyncio.run_coroutine_threadsafe(cb(), self._loop)
                    except Exception:
                        pass

        self._music_thread = threading.Thread(
            target=_reader, daemon=True, name="blood-music-feed"
        )
        self._music_thread.start()

    def stop_music(self):
        """Stop current music stream and clear buffer."""
        self._music_stopping = True
        self._has_music = False
        # Clear buffer first so read() returns silence immediately
        self._music_buf.clear()
        if self._music_proc:
            try:
                # Close stdout first to unblock the reader thread's .read()
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
        self._music_thread = None
        # Clear again in case reader thread wrote frames between kill and join
        self._music_buf.clear()
        self._music_stopping = False

    def feed_tts_sync(self, mp3_data: bytes):
        """Convert MP3→PCM, feed into TTS buffer, block until drained.
        Call from a thread (use run_in_executor from async code)."""
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

            # Wait for TTS buffer to drain so music stays ducked until speech ends
            while self._tts_buf:
                time.sleep(0.02)
            time.sleep(0.1)  # grace period for smooth unduck
            self._tts_active = False

    def read(self) -> bytes:
        """Called by Discord's AudioPlayer thread every 20ms.
        Mixes music + TTS, auto-ducks music when TTS is active."""
        music = self._music_buf.popleft() if self._music_buf else None
        tts = self._tts_buf.popleft() if self._tts_buf else None
        ducking = self._tts_active or bool(self._tts_buf)

        if music is None and tts is None:
            return SILENCE

        if music and tts:
            mvol = self._duck_vol if ducking else self._music_vol
            return _mix_two(music, mvol, tts, 1.0)
        elif music:
            mvol = self._duck_vol if ducking else self._music_vol
            if mvol >= 0.99:
                return music
            return _scale_pcm(music, mvol)
        else:
            return tts

    def is_opus(self) -> bool:
        return False

    def cleanup(self):
        """Called when Discord stops playing this source."""
        self.stop_music()
        self._tts_buf.clear()
        self._tts_active = False
