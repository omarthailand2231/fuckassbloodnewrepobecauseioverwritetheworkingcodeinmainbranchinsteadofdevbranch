"""
Blood Audio Mixer — mixes music + TTS with ducking, outputs single stream to Discord.

Architecture:
- Music stream (PCM 48kHz stereo) → channel 0
- TTS stream (PCM 48kHz stereo) → channel 1
- Mixer applies volume ducking and sums to output
- Single FFmpegAudioSource reads from mixer output queue
"""

import asyncio
import logging
import struct
from collections import deque
from typing import Optional

import discord

log = logging.getLogger("blood.mixer")

# Audio config — must match Discord's expectations
SAMPLE_RATE = 48000
CHANNELS = 2
SAMPLE_WIDTH = 2  # 16-bit
FRAME_SIZE = 3840  # 20ms @ 48kHz stereo 16-bit = 48000 * 2 * 2 * 0.02


class AudioMixer:
    """
    Real-time audio mixer with ducking support.
    Mixes multiple sources into a single output stream.
    """

    def __init__(self):
        # Source queues: name -> deque of PCM bytes
        self._sources: dict[str, deque] = {}
        self._volumes: dict[str, float] = {}
        self._ducking: dict[str, float] = {}  # active duck multiplier

        # Output queue — Discord reads from here
        self.output_queue: deque = deque(maxlen=100)  # ~2s buffer

        self._running = False
        self._task: Optional[asyncio.Task] = None

        # Ducking config
        self._duck_target = 0.15  # music volume when ducking
        self._duck_fade_samples = 2400  # 50ms fade @ 48kHz

    def add_source(self, name: str, volume: float = 1.0):
        """Register a named audio source."""
        self._sources[name] = deque(maxlen=200)  # ~4s per source
        self._volumes[name] = volume
        self._ducking[name] = 1.0
        log.info("Added mixer source: %s", name)

    def remove_source(self, name: str):
        """Remove an audio source."""
        if name in self._sources:
            del self._sources[name]
            del self._volumes[name]
            del self._ducking[name]

    def reset(self):
        """Clear all sources and reset state."""
        self.stop()
        self._sources.clear()
        self._volumes.clear()
        self._ducking.clear()
        self.output_queue.clear()

    def feed(self, name: str, pcm_data: bytes):
        """Feed PCM audio to a source."""
        if name in self._sources:
            self._sources[name].append(pcm_data)

    def set_volume(self, name: str, volume: float):
        """Set source volume (0.0 to 1.0+)."""
        self._volumes[name] = max(0.0, min(2.0, volume))

    def start_duck(self, name: str):
        """Start ducking a source (lower volume)."""
        self._ducking[name] = self._duck_target

    def stop_duck(self, name: str):
        """Stop ducking a source (restore volume)."""
        self._ducking[name] = 1.0

    def start(self):
        """Start the mixer loop."""
        if self._running and self._task and not self._task.done():
            return  # Already running
        self._running = True
        self._task = asyncio.create_task(self._mix_loop())
        log.info("Audio mixer started")

    def stop(self):
        """Stop the mixer loop."""
        self._running = False
        if self._task:
            try:
                self._task.cancel()
            except Exception:
                pass
            self._task = None
        log.info("Audio mixer stopped")

    async def _mix_loop(self):
        """Main mixing loop — runs at 50Hz (20ms chunks)."""
        while self._running:
            try:
                await asyncio.sleep(0.02)  # 20ms = 50Hz

                if not self._sources:
                    # Output silence
                    self.output_queue.append(b'\x00' * FRAME_SIZE)
                    continue

                # Collect frames from all sources
                frames: dict[str, bytes] = {}
                for name, queue in self._sources.items():
                    if queue:
                        frames[name] = queue.popleft()
                    else:
                        # Source underrun — silence
                        frames[name] = b'\x00' * FRAME_SIZE

                # Mix frames
                mixed = self._mix_frames(frames)
                self.output_queue.append(mixed)

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning("Mixer loop error: %s", e)

    def _mix_frames(self, frames: dict[str, bytes]) -> bytes:
        """Mix multiple PCM frames with per-source volume/ducking.
        Pure Python implementation (audioop removed in Python 3.13+)."""
        if not frames:
            return b'\x00' * FRAME_SIZE

        # Convert to list of samples (signed 16-bit) for mixing
        # Stereo = 2 channels, so total samples = FRAME_SIZE // 2
        num_samples = FRAME_SIZE // SAMPLE_WIDTH
        mixed = [0] * num_samples

        for name, pcm in frames.items():
            # Pad or trim to exact frame size
            if len(pcm) < FRAME_SIZE:
                pcm = pcm + b'\x00' * (FRAME_SIZE - len(pcm))
            elif len(pcm) > FRAME_SIZE:
                pcm = pcm[:FRAME_SIZE]

            # Apply volume + ducking
            vol = self._volumes.get(name, 1.0) * self._ducking.get(name, 1.0)

            # Unpack and mix
            # '<' = little-endian, 'h' = signed short (16-bit)
            samples = struct.unpack(f'<{num_samples}h', pcm)
            for i, s in enumerate(samples):
                mixed[i] += int(s * vol)

        # Clamp to 16-bit range and pack back to bytes
        clamped = [max(-32768, min(32767, s)) for s in mixed]
        return struct.pack(f'<{num_samples}h', *clamped)

    def read(self) -> bytes:
        """Read mixed audio for Discord. Returns 20ms PCM chunk."""
        if self.output_queue:
            return self.output_queue.popleft()
        return b'\x00' * FRAME_SIZE


# Global mixer instance
_mixer: Optional[AudioMixer] = None


def get_mixer() -> AudioMixer:
    """Get or create the global audio mixer."""
    global _mixer
    if _mixer is None:
        _mixer = AudioMixer()
    return _mixer


def reset_mixer():
    """Reset the global mixer."""
    global _mixer
    if _mixer:
        _mixer.stop()
    _mixer = None


class MixedAudioSource(discord.AudioSource):
    """
    Discord AudioSource that reads from the global mixer.
    This allows mixing multiple sources into one Discord stream.
    """

    def __init__(self, mixer: AudioMixer):
        self.mixer = mixer
        self._buffer = b''

    def read(self) -> bytes:
        """Discord calls this repeatedly for audio data."""
        return self.mixer.read()

    def is_opus(self) -> bool:
        return False

    def cleanup(self):
        """Stop mixer when Discord stops playing."""
        try:
            self.mixer.stop()
        except Exception:
            pass


# ── Integration helpers ───────────────────────────────────────────────────────

async def start_mixed_playback(voice_client, music_url: str, start_paused: bool = False):
    """
    Start music playback through the mixer.
    Returns the mixer instance for TTS injection.
    """
    import discord
    from voice import FFMPEG_OPTS

    mixer = get_mixer()
    mixer.add_source("music", volume=1.0)
    mixer.add_source("tts", volume=1.0)

    # Start FFmpeg for music in background, feeding into mixer
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-re", "-i", music_url,
        "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS),
        "-loglevel", "quiet", "pipe:1",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )

    # Task to read FFmpeg output and feed mixer
    async def feed_music():
        while True:
            try:
                chunk = await proc.stdout.read(FRAME_SIZE)
                if not chunk:
                    break
                mixer.feed("music", chunk)
            except Exception:
                break

    asyncio.create_task(feed_music())

    # Start mixer
    mixer.start()

    # Connect mixer to Discord
    source = MixedAudioSource(mixer)

    def on_music_end(error):
        if error:
            log.warning("Music ended with error: %s", error)
        # Could trigger next track here

    if not start_paused:
        voice_client.play(source, after=on_music_end)

    return mixer


async def speak_with_duck(voice_client, tts_pcm: bytes, mixer: AudioMixer):
    """
    Play TTS through mixer with music ducking.
    """
    # Duck music
    mixer.start_duck("music")
    await asyncio.sleep(0.05)  # 50ms fade

    # Feed TTS in chunks
    for i in range(0, len(tts_pcm), FRAME_SIZE):
        chunk = tts_pcm[i:i + FRAME_SIZE]
        if len(chunk) < FRAME_SIZE:
            chunk = chunk + b'\x00' * (FRAME_SIZE - len(chunk))
        mixer.feed("tts", chunk)
        await asyncio.sleep(0.02)  # 20ms per chunk

    # Clear TTS buffer
    mixer._sources["tts"].clear()

    # Restore music
    await asyncio.sleep(0.1)
    mixer.stop_duck("music")
