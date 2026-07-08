"""
Local speech-to-text using mlx-whisper (Apple Silicon GPU / Metal).

Replaces the Groq Whisper API path. Runs large-v3 entirely on-device — no
network, no API key, and (with the full / quantized large-v3 weights) better
Thai + Thai-English code-mixed accuracy than the distilled "turbo" model that
Groq serves.

Model is cached in-process by mlx_whisper.transcribe.ModelHolder (keyed on the
repo path), so it loads once on first use and stays resident. All work here is
CPU/GPU-bound and synchronous, so callers MUST run transcribe_pcm() in a thread
executor — never directly on the event loop. A single process-wide lock keeps
the warm-up and the worker from touching the model concurrently.
"""
import logging
import os
import threading
import time

import numpy as np

log = logging.getLogger("bloodai.stt")

# Default: 4-bit large-v3 — ~1GB RAM, real large-v3 multilingual weights
# (Thai >> turbo), fast on the M-series GPU. Sized for an 8GB machine running
# the rest of the bot alongside it. Override with STT_MODEL:
#   mlx-community/whisper-large-v3-mlx          fp16, ~3GB, best accuracy (16GB+)
#   mlx-community/whisper-large-v3-mlx-4bit     ~1GB, default
#   mlx-community/whisper-large-v3-turbo        ~1.6GB, fastest, weaker Thai
STT_MODEL = os.getenv("STT_MODEL", "mlx-community/whisper-large-v3-mlx-4bit")

# Auto-detect language by default (best for code-mixed Thai/English). Set
# STT_LANGUAGE=th or =en to force a single language.
STT_LANGUAGE = os.getenv("STT_LANGUAGE") or None

TARGET_RATE = 16000
_MIN_SAMPLES = int(TARGET_RATE * 0.2)  # ignore < 0.2s of audio after resample

_lock = threading.Lock()
_loaded = False


def _pcm_to_float_mono16k(pcm: bytes, sample_rate: int, channels: int) -> np.ndarray:
    """Discord delivers 48kHz 16-bit stereo PCM; Whisper wants 16kHz mono float32
    in [-1, 1]. Downmix to mono, then resample (48k->16k is an exact /3 integer
    decimation with a box-filter anti-alias; arbitrary rates fall back to linear
    interpolation)."""
    audio = np.frombuffer(pcm, dtype=np.int16)
    if audio.size == 0:
        return np.zeros(0, dtype=np.float32)
    if channels > 1:
        usable = (audio.size // channels) * channels
        audio = audio[:usable].reshape(-1, channels).mean(axis=1)
    audio = audio.astype(np.float32) / 32768.0
    if sample_rate != TARGET_RATE:
        if sample_rate % TARGET_RATE == 0:
            factor = sample_rate // TARGET_RATE
            n = (len(audio) // factor) * factor
            if n:
                audio = audio[:n].reshape(-1, factor).mean(axis=1)
        else:
            new_len = int(round(len(audio) * TARGET_RATE / sample_rate))
            if new_len > 0:
                audio = np.interp(
                    np.linspace(0, len(audio), new_len, endpoint=False),
                    np.arange(len(audio)), audio,
                )
    return np.ascontiguousarray(audio, dtype=np.float32)


def is_loaded() -> bool:
    return _loaded


def warm_up() -> bool:
    """Load the model once (downloads the weights on the very first run). Safe to
    call repeatedly and from a background thread. Returns True when ready."""
    global _loaded
    if _loaded:
        return True
    with _lock:
        if _loaded:
            return True
        try:
            import mlx_whisper
            t0 = time.monotonic()
            silence = np.zeros(TARGET_RATE // 2, dtype=np.float32)
            mlx_whisper.transcribe(
                silence, path_or_hf_repo=STT_MODEL,
                language=STT_LANGUAGE, temperature=0.0,
                condition_on_previous_text=False, verbose=False,
            )
            _loaded = True
            log.info("[STT] mlx-whisper model '%s' ready in %.1fs",
                     STT_MODEL, time.monotonic() - t0)
            return True
        except Exception as e:
            log.error("[STT] failed to load mlx-whisper model '%s': %s", STT_MODEL, e)
            return False


def _clean_transcription(text: str) -> str:
    """Post-filter Whisper output to kill the repetition loops it still
    occasionally emits ("...get my baby. ...get my baby. ..." ×9). Collapses any
    short unit repeated back-to-back, plus consecutive duplicate sentences.
    Works across scripts (English, Thai), returns '' for empty input."""
    import re
    raw = (text or "").strip()
    if not raw:
        return ""
    # Collapse an immediate phrase loop: a 3–60 char unit repeated 3+ more times.
    collapsed = re.sub(r"(.{3,60}?)(?:\s*\1){3,}", r"\1", raw).strip()
    # Collapse consecutive duplicate sentences (punctuation-separated).
    parts = [p.strip() for p in re.split(r"(?<=[.!?。])\s+|\n+", collapsed) if p.strip()]
    out = []
    for p in parts:
        if not out or p != out[-1]:
            out.append(p)
    return " ".join(out) if out else collapsed


def transcribe_pcm(pcm: bytes, sample_rate: int = 48000, channels: int = 2) -> str:
    """Transcribe raw 16-bit PCM. SYNCHRONOUS + heavy — call via run_in_executor.
    Returns '' on empty/failed transcription."""
    try:
        import mlx_whisper
    except Exception as e:
        log.error("[STT] mlx_whisper not installed: %s", e)
        return ""
    audio = _pcm_to_float_mono16k(pcm, sample_rate, channels)
    if audio.size < _MIN_SAMPLES:
        return ""
    global _loaded
    with _lock:
        try:
            result = mlx_whisper.transcribe(
                audio,
                path_or_hf_repo=STT_MODEL,
                language=STT_LANGUAGE,
                task="transcribe",
                # Tuple = temperature FALLBACK. When a segment decodes into a
                # repetition loop (high compression ratio) or low confidence,
                # Whisper retries at a higher temperature instead of emitting the
                # looped garbage. A scalar 0.0 disables this — which is what caused
                # the "...get my baby." ×9 hallucinations. Keep the default tuple.
                temperature=(0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
                condition_on_previous_text=False,   # each utterance is independent
                compression_ratio_threshold=2.4,    # repetitive output -> retry/drop
                logprob_threshold=-1.0,             # low-confidence output -> retry/drop
                no_speech_threshold=0.6,            # silence/noise -> emit nothing
                verbose=False,
            )
            _loaded = True
            return _clean_transcription(result.get("text") or "")
        except Exception as e:
            log.warning("[STT] local transcription failed: %s", e)
            return ""


if __name__ == "__main__":
    # Quick self-test: python stt_local.py [audio.wav]
    import sys
    logging.basicConfig(level=logging.INFO)
    print(f"Model: {STT_MODEL}  language: {STT_LANGUAGE or 'auto'}")
    if len(sys.argv) > 1:
        import wave
        with wave.open(sys.argv[1], "rb") as wf:
            ch, sr = wf.getnchannels(), wf.getframerate()
            pcm = wf.readframes(wf.getnframes())
        t0 = time.monotonic()
        text = transcribe_pcm(pcm, sample_rate=sr, channels=ch)
        print(f"[{time.monotonic() - t0:.1f}s] {text!r}")
    else:
        print("warming up...", warm_up())
