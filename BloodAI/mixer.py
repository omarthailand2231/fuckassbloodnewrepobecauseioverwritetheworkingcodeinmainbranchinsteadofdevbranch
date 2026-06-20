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


def _mix_many(frames: list) -> bytes:
    """Sum any number of 20ms PCM frames into one, clamped to int16 range."""
    if not frames:
        return SILENCE
    if len(frames) == 1:
        return frames[0]
    
    acc = [0] * NUM_SAMPLES
    for f in frames:
        if len(f) < FRAME_SIZE:
            f = f + b'\x00' * (FRAME_SIZE - len(f))
        a = array.array('h')
        a.frombytes(f[:FRAME_SIZE])
        for i in range(NUM_SAMPLES):
            acc[i] += a[i]
    
    out = array.array('h', [0] * NUM_SAMPLES)  # Fixed: create mutable array with proper size
    for i in range(NUM_SAMPLES):
        v = acc[i]
        out[i] = 32767 if v > 32767 else (-32768 if v < -32768 else v)
    
    result = out.tobytes()
    if len(result) != FRAME_SIZE:
        log.warning("[MIX] output frame size mismatch: expected %d, got %d", FRAME_SIZE, len(result))
    return result


def decode_to_frames(audio_data: bytes, timeout: float = 30.0) -> list:
    """Decode any audio bytes (mp3/wav/ogg/...) to a list of 20ms s16le/48k/stereo frames."""
    try:
        proc = subprocess.Popen(
            ["ffmpeg", "-i", "pipe:0",
             "-f", "s16le", "-ar", "48000", "-ac", "2",
             "-loglevel", "quiet", "pipe:1"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        pcm, err = proc.communicate(input=audio_data, timeout=timeout)
    except Exception as e:
        log.warning("[SFX] decode failed: %s", e)
        return []
    
    if not pcm:
        log.warning("[SFX] FFmpeg returned empty PCM (input: %d bytes). Error: %s", len(audio_data), err.decode('utf-8', errors='ignore') if err else "none")
        return []
    
    frames = []
    for off in range(0, len(pcm), FRAME_SIZE):
        chunk = pcm[off:off + FRAME_SIZE]
        if len(chunk) < FRAME_SIZE:
            chunk += b'\x00' * (FRAME_SIZE - len(chunk))
        frames.append(chunk)
    log.debug("[SFX] decoded %d bytes → %d frames", len(pcm), len(frames))
    return frames


class SfxLayer:
    """Mixes an arbitrary number of concurrent short clips into one stream.

    Each clip is a list of pre-decoded 20ms frames. ``mix_frame()`` pops one
    frame from every active clip and sums them, so overlapping sounds play
    *on top of each other* (intentionally chaotic). Thread-safe: ``add()`` can
    be called from the asyncio thread while ``mix_frame()`` runs on Discord's
    player thread.
    """

    def __init__(self):
        self._voices: list = []          # list[deque[bytes]]
        self._lock = threading.Lock()

    def add(self, frames: list) -> None:
        if not frames:
            return
        with self._lock:
            self._voices.append(deque(frames))

    @property
    def active(self) -> bool:
        with self._lock:
            return any(self._voices)

    def clear(self) -> None:
        with self._lock:
            self._voices.clear()

    def mix_frame(self):
        """Next mixed 20ms frame, or None if nothing is playing."""
        with self._lock:
            if not self._voices:
                return None
            frames, still = [], []
            for v in self._voices:
                if v:
                    frames.append(v.popleft())
                    if v:
                        still.append(v)
            self._voices = still
        if not frames:
            return None
        return _mix_many(frames)


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


# ── True graphic EQ (real-time biquad bank, applied in the PCM domain) ─────────

# 10-band ISO octave centres — what the web mixer's EQ graph drives.
EQ_BANDS = [31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]
EQ_GAIN_LIMIT = 15.0  # dB clamp per band


class Equalizer:
    """A cascade of RBJ peaking biquads (one per band) applied live to 48k stereo
    PCM. Gains are adjustable on the fly from the web UI — coefficients rebuild and
    filter state is preserved so there are no clicks. Falls back to passthrough if
    numpy/scipy aren't importable or anything goes wrong on the hot path."""

    def __init__(self, sample_rate: int = 48000):
        self.sr = sample_rate
        self.bands = list(EQ_BANDS)
        self.gains = [0.0] * len(self.bands)
        self._enabled = False
        self._lock = threading.Lock()
        self._sos = None
        self._zi_l = None
        self._zi_r = None
        self._np = None
        self._sosfilt = None
        try:
            import numpy as np
            from scipy.signal import sosfilt
            self._np = np
            self._sosfilt = sosfilt
        except Exception as e:
            log.warning("[EQ] numpy/scipy unavailable — EQ disabled: %s", e)

    def _peaking_sos(self, f0: float, gain_db: float, q: float = 1.41):
        """RBJ cookbook peaking-EQ second-order section, normalized by a0."""
        import math
        A = 10 ** (gain_db / 40.0)
        w0 = 2 * math.pi * f0 / self.sr
        cw, sw = math.cos(w0), math.sin(w0)
        alpha = sw / (2 * q)
        b0 = 1 + alpha * A
        b1 = -2 * cw
        b2 = 1 - alpha * A
        a0 = 1 + alpha / A
        a1 = -2 * cw
        a2 = 1 - alpha / A
        return [b0 / a0, b1 / a0, b2 / a0, 1.0, a1 / a0, a2 / a0]

    def _rebuild(self):
        np = self._np
        sos = np.array([self._peaking_sos(f, g) for f, g in zip(self.bands, self.gains)],
                       dtype=np.float64)
        self._sos = sos
        if self._zi_l is None or self._zi_l.shape[0] != sos.shape[0]:
            self._zi_l = np.zeros((sos.shape[0], 2), dtype=np.float64)
            self._zi_r = np.zeros((sos.shape[0], 2), dtype=np.float64)

    def set_gains(self, gains):
        n = len(self.bands)
        g = [float(max(-EQ_GAIN_LIMIT, min(EQ_GAIN_LIMIT, x))) for x in list(gains)[:n]]
        g += [0.0] * (n - len(g))
        with self._lock:
            self.gains = g
            self._enabled = bool(self._np) and any(abs(x) > 0.1 for x in g)
            if self._enabled:
                self._rebuild()

    def get_gains(self) -> list:
        return list(self.gains)

    def reset(self):
        self.set_gains([0.0] * len(self.bands))

    @property
    def enabled(self) -> bool:
        return self._enabled

    def process_scaled(self, pcm: bytes, scale: float = 1.0):
        """Apply the EQ then the music volume in ONE float pass and clip ONCE, so a
        boosted band keeps full headroom instead of hard-clipping at full scale before
        the later volume/duck attenuation (which would bake in irreversible distortion).
        Returns int16 bytes, or None if the EQ is disabled or anything fails — the caller
        then applies its own volume normally."""
        if not self._enabled or self._sos is None:
            return None
        try:
            np = self._np
            with self._lock:
                sos = self._sos
                samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float64)
                yL, self._zi_l = self._sosfilt(sos, samples[0::2], zi=self._zi_l)
                yR, self._zi_r = self._sosfilt(sos, samples[1::2], zi=self._zi_r)
                out = np.empty_like(samples)
                out[0::2] = yL
                out[1::2] = yR
                if scale != 1.0:
                    out *= scale
                np.clip(out, -32768, 32767, out=out)
                return out.astype(np.int16).tobytes()
        except Exception as e:
            log.debug("[EQ] process failed (passthrough): %s", e)
            return None


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

        # Overlapping join sounds, layered on top of music (no ducking — chaos)
        self.sfx = SfxLayer()

        # Live graphic EQ applied to the music frame (passthrough when flat)
        self._eq = Equalizer(48000)

        # Spectrum visualizer — recent output frames captured for an off-thread FFT.
        self._spec_frames: deque = deque(maxlen=10)
        self._spec_on = False
        self._spec_smooth = None

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

    # ── Live graphic EQ ──────────────────────────────────────────────────────
    def set_eq_gains(self, gains):
        self._eq.set_gains(gains)

    def get_eq_gains(self) -> list:
        return self._eq.get_gains()

    def reset_eq(self):
        self._eq.reset()

    # ── Spectrum visualizer ──────────────────────────────────────────────────
    def set_spectrum_on(self, on: bool):
        self._spec_on = bool(on)
        if not on:
            self._spec_frames.clear()
            self._spec_smooth = None

    def get_spectrum(self, nbins: int = 96):
        """FFT the recently captured output, log-bin it to nbins (31Hz→16kHz) and
        return a smoothed list of 0..1 magnitudes for the web visualizer. Runs in the
        caller's thread (web loop), never on the audio thread. None if unavailable."""
        np = self._eq._np
        if np is None:
            return None
        try:
            frames = list(self._spec_frames)
        except Exception:
            return None
        if not frames:
            return None
        try:
            data = np.concatenate(frames).astype(np.float64)
            n = len(data)
            if n < 512:
                return None
            mag = np.abs(np.fft.rfft(data * np.hanning(n)))
            freqs = np.fft.rfftfreq(n, 1.0 / 48000)
            edges = np.logspace(np.log10(31), np.log10(16000), nbins + 1)
            fmask = (freqs >= edges[0]) & (freqs < edges[-1])
            idx = np.clip(np.digitize(freqs[fmask], edges) - 1, 0, nbins - 1)
            sums = np.bincount(idx, weights=mag[fmask], minlength=nbins)
            counts = np.bincount(idx, minlength=nbins)
            amp = np.where(counts > 0, sums / np.maximum(counts, 1), 0.0) * (2.0 / (n * 32768.0))
            db = 20.0 * np.log10(amp + 1e-9)
            cur = np.clip((db + 70.0) / 70.0, 0.0, 1.0)  # -70dB..0dB → 0..1
            if self._spec_smooth is None or len(self._spec_smooth) != nbins:
                self._spec_smooth = cur
            else:
                self._spec_smooth = 0.5 * self._spec_smooth + 0.5 * cur
            return [round(float(x), 3) for x in self._spec_smooth]
        except Exception as e:
            log.debug("[SPEC] failed: %s", e)
            return None

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
            # Keep network streams (e.g. YouTube googlevideo) alive through
            # transient stalls/HTTP errors instead of EOF-ing early. An early EOF
            # is indistinguishable from a clean end downstream and cuts the song
            # off mid-track. -rw_timeout (µs) bounds a hung read so reconnect can
            # kick in rather than the read blocking forever. (ffmpeg 4.3+.)
            cmd += ["-reconnect", "1", "-reconnect_streamed", "1",
                    "-reconnect_on_network_error", "1",
                    "-reconnect_on_http_error", "4xx,5xx",
                    "-reconnect_delay_max", "10",
                    "-rw_timeout", "15000000"]
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
            reader_t0 = time.monotonic()
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
                            # chunks * 20ms = seconds of audio actually decoded.
                            # If this is far below the track length, the stream
                            # died early (stall/expired URL) rather than ending.
                            log.info("Music stream ended (FFmpeg returned empty after %d chunks ≈ %.1fs audio, %.1fs wall)",
                                     chunk_counter, chunk_counter * (FRAME_SIZE / (48000 * 2 * 2)),
                                     time.monotonic() - reader_t0)
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

        # Live EQ on the music frame, with the music volume folded into the SAME
        # float pass and clipped ONCE — so a boosted band keeps the headroom that the
        # volume/duck attenuation would otherwise have given it (no early full-scale
        # clipping). TTS/SFX are never EQ'd. Passthrough (None) → normal scaling below.
        if music is not None and self._eq.enabled:
            eqd = self._eq.process_scaled(music, mvol)
            if eqd is not None:
                music = eqd
                mvol = 1.0  # volume already applied inside the EQ pass

        # Overlapping join sounds — mixed in on top of everything, no ducking
        sfx = self.sfx.mix_frame()

        # 2. No music/TTS — emit SFX alone (or silence)
        if music is None and tts is None:
            return sfx if sfx is not None else SILENCE

        # 3. Mix music + TTS
        if music and tts:
            base = _mix_two(music, mvol, tts, 1.0)
        elif music:
            if self._read_call_count <= 5:
                is_silent = all(b == 0 for b in music[:100])
                log.info("[AUDIO] Music read #%d: vol=%.3f, ducking=%s, pre_fade=%s, silent=%s",
                         self._read_call_count, mvol, ducking, pre_fading, is_silent)
            base = music if mvol >= 0.99 else _scale_pcm(music, mvol)
        else:
            base = tts

        # 4. Overlay any active join sounds
        out = _mix_two(base, 1.0, sfx, 1.0) if sfx is not None else base

        # Capture the final output for the spectrum visualizer (cheap; FFT runs off-thread)
        if self._spec_on and out is not None and len(out) >= FRAME_SIZE:
            _np = self._eq._np
            if _np is not None:
                try:
                    self._spec_frames.append(_np.frombuffer(out, dtype=_np.int16)[0::2].copy())
                except Exception:
                    pass
        return out

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
