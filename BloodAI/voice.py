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

    def start_processing(self):
        """Start the background task that checks for silence and transcribes."""
        self._process_task = asyncio.create_task(self._process_loop())

    async def _process_loop(self):
        """Periodically check user buffers for completed speech."""
        while self._running:
            try:
                await asyncio.sleep(0.3)
                for uid, buf in list(self.user_buffers.items()):
                    # Check if user stopped speaking (silence gap)
                    if buf.is_speaking and buf.silence_duration() > SILENCE_THRESHOLD_SEC:
                        pcm = buf.harvest()
                        if pcm:
                            asyncio.create_task(self._handle_speech(uid, buf.user_name, pcm))
                    # Force harvest if speaking too long
                    elif buf.is_speaking and buf.speech_duration() > MAX_SPEECH_SEC:
                        pcm = buf.harvest()
                        if pcm:
                            asyncio.create_task(self._handle_speech(uid, buf.user_name, pcm))
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning("Voice process loop error: %s", e)

    async def _handle_speech(self, user_id: int, user_name: str, pcm_data: bytes):
        """Transcribe speech and decide whether to respond."""
        text = await transcribe_audio(pcm_data)
        if not text:
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
        try:
            from provider import call_ai
            from config import CONFIG

            # Build a compact voice-mode system prompt
            system = (
                f"You are Blood, speaking in a voice channel. Keep responses SHORT (1-3 sentences max) "
                f"since they will be spoken aloud via TTS. Be witty and natural. "
                f"Don't use markdown, emojis, or formatting — just plain spoken text. "
                f"The user '{user_name}' said: \"{text}\""
            )

            # Include recent conversation context
            context_msgs = []
            for entry in _convo_buffer.get(self.guild_id, [])[-10:]:
                role = "assistant" if entry.get("user_name") == "Blood" else "user"
                prefix = "" if role == "assistant" else f"[{entry.get('user_name', '?')}] "
                context_msgs.append({"role": role, "content": f"{prefix}{entry.get('text', '')}"})

            if not context_msgs:
                context_msgs = [{"role": "user", "content": f"[{user_name}] {text}"}]

            result = await call_ai(system, context_msgs, max_tokens=200)
            content = result.get("content", "").strip()
            return content if content else None
        except Exception as e:
            log.warning("Voice response generation failed: %s", e)
            return None

    async def _speak(self, text: str):
        """Convert text to speech and play in voice channel."""
        guild = self.bot.get_guild(int(self.guild_id))
        if not guild or not guild.voice_client:
            return

        mp3_data = await text_to_speech(text)
        if not mp3_data:
            log.warning("TTS returned no audio")
            return

        try:
            source = FFmpegPCMAudioPipe(mp3_data)
            source.prepare()

            # Wait for any current audio to finish
            vc = guild.voice_client
            while vc.is_playing():
                await asyncio.sleep(0.1)

            vc.play(source, after=lambda e: log.warning("TTS playback error: %s", e) if e else None)

            # Wait for playback to finish
            while vc.is_playing():
                await asyncio.sleep(0.1)
        except Exception as e:
            log.warning("Failed to play TTS: %s", e)

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

    log.info("Joined VC '%s' in %s — listening", vc_channel.name, guild.name)
    return f"✅ Joined **{vc_channel.name}** — listening & recording. Say 'Blood' or 'hey Blood' to talk to me!"


async def leave_voice(guild: discord.Guild) -> str:
    """Leave voice channel and save transcript."""
    guild_id = str(guild.id)

    # Stop sink
    sink = _active_sinks.pop(guild_id, None)
    if sink:
        sink.stop()

    # End session
    end_vc_session(guild_id)

    # Disconnect
    if guild.voice_client:
        await guild.voice_client.disconnect(force=True)

    # Clear conversation state
    _active_conversation.pop(guild_id, None)

    return "✅ Left voice channel. Transcript saved."
