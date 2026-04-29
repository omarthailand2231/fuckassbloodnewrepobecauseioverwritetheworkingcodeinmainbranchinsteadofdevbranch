# Blood — Research Notes & Architecture Decisions

Technical notes on systems worth reusing or learning from. Written for anyone building a similar Discord AI bot.

---

## 1. Thread-Based Audio Mixer for Discord

**Problem**: Discord.py's `AudioPlayer` runs in its own thread and calls `source.read()` every 20ms. You can only have one `AudioSource` playing at a time. If you want music + TTS simultaneously, you need a mixer.

**Why not async?** Tried an async mixer first — it caused `CryptoError decoding packet data` and timing issues because async loops can't guarantee 20ms timing. Discord's AudioPlayer thread already has perfect timing, so we use it.

**Architecture** (`mixer.py`):
```
Discord AudioPlayer thread
    └── calls mixer.read() every 20ms
         ├── pops 1 frame from music_buf (deque)
         ├── pops 1 frame from tts_buf (deque)
         ├── mixes PCM samples (clamp to int16)
         └── returns 3840 bytes (20ms @ 48kHz stereo 16-bit)

Music reader thread
    └── reads FFmpeg stdout → appends to music_buf
    └── back-pressure: sleeps when buf > 50 frames

TTS reader thread
    └── reads FFmpeg stdout → appends to tts_buf
    └── started on-demand when Blood speaks
```

**Key constants**:
- Frame size: 3840 bytes (20ms @ 48kHz, stereo, 16-bit)
- 1920 samples per frame
- Duck volume: 0.15 (music drops to 15% when TTS active)
- Back-pressure threshold: 50 frames (~1 second)

**Gotchas**:
- Don't use `deque(maxlen=N)` for the music buffer — it silently drops oldest frames, causing chipmunk/speedup effect. Use manual back-pressure in the reader thread instead.
- Use `array.array('h', data)` for PCM manipulation — `struct.unpack` is too slow for 1920 samples per frame at 50fps.
- `numpy` would be even faster but `array.array` has zero dependencies and works on Python 3.14+.
- FFmpeg args must include `-ac 2 -ar 48000 -f s16le` to match Discord's expected format.

---

## 2. Voice Receive + STT Pipeline

**Problem**: Getting reliable speech-to-text from Discord voice is hard. Audio comes in per-user 20ms PCM chunks via `discord-ext-voice-recv`. You need to buffer, detect silence, and batch-send to an STT API.

**Pipeline**:
```
on_voice_data(user, pcm_bytes)  ← called by discord-ext-voice-recv
    └── UserAudioBuffer.add_pcm(pcm_bytes)
         ├── Appends to per-user circular buffer
         ├── Detects silence (< threshold for 1.5s)
         └── When silence detected → flush to STT

_process_loop (async, runs every 0.5s)
    └── For each user with enough audio:
         ├── Grab PCM bytes
         ├── Convert PCM → WAV (in-memory)
         ├── POST to Groq Whisper API
         └── Route transcription text
```

**Critical**: Never drop audio in `on_voice_data`. Early versions had a guard that skipped buffering while STT was processing — this caused missed wake words. The `UserAudioBuffer` has its own `MAX_BUFFER_BYTES` limit, so just always buffer.

**STT Throttle**: Minimum 2 seconds between transcriptions per user to avoid overloading Groq's API.

**Junk filter**: Whisper hallucinates on silence. Filter out:
- Repeated characters/punctuation
- Very short text (< 2 chars after strip)
- Known hallucination patterns (e.g., "...", "♪", "MBC 뉴스")
- URLs and emoji-only text

---

## 3. AI Music Recommendations (Spotify-like)

**Problem**: Template-based YouTube searches ("songs similar to X") just return the same song. Users hear the same 3 tracks on loop.

**Solution**: Use the LLM itself as a recommendation engine.

**How it works**:
1. Collect user taste data: `{"liked": [...], "disliked": [...]}`
2. Send to AI with a strict prompt:
   - Include last 20 liked, 10 disliked, 15 recently played
   - Request 15 songs in `Artist - Song Title` format
   - Mix ratio: 60% similar, 30% discovery, 10% wildcard
3. Parse response: reject lines with reasoning text, enforce `" - "` format
4. Cache the batch — only call AI every ~15 tracks
5. Search YouTube with `"Artist - Song Title audio"` to find each track

**Parsing pitfall**: The AI sometimes outputs reasoning ("Actually, looking at...") mixed with song names. Filter with:
```python
# Reject lines that look like AI reasoning
bad_words = ("actually", "maybe", "could be", "let me", "i think", "note:", "here")
if any(w in line.lower() for w in bad_words):
    skip
```

**YouTube search tips**:
- Append `" audio"` to queries to prefer official audio uploads over music videos
- Use `ytsearch5:` (search 5 results) instead of `ytsearch:` (only 1) — then pick the best
- Filter by duration: skip anything > 15 minutes (catches audiobooks, podcasts)
- Filter by title: skip "cover", "karaoke", "reaction", "audiobook", "tutorial"

---

## 4. AI Intent Classification vs Keywords

**Problem**: Keyword matching for music commands is brittle. "skip" works but "nah change it" doesn't.

**Solution**: Small AI call to classify intent. One-word response, ~30 tokens.

**System prompt**:
```
You classify user speech into music intents. Reply with ONLY one word:
like, dislike, skip, stop, random, queue, play:QUERY, none
```

**Optimization**: Don't run on every voice input — that's expensive and causes event loop congestion (missed Discord gateway heartbeats). Pre-filter:
- If music is playing → always run classifier
- If no music → only run if text contains music-hint words (play, skip, song, etc.)
- URLs bypass classifier entirely (regex is faster)

---

## 5. Discord Gateway Heartbeat + Event Loop Health

**Problem**: `"Shard has stopped responding to the gateway. Closing and restarting."` — Discord kills the bot's connection if heartbeats are missed.

**Root cause**: Too many concurrent async operations (AI API calls, yt-dlp extractions, TTS generation) saturate the event loop. The gateway heartbeat (~41.25s interval) doesn't get serviced in time.

**Mitigations**:
- Minimize unnecessary API calls (pre-filter before AI classifier)
- Use `run_in_executor` for blocking operations (yt-dlp, FFmpeg)
- Keep the event loop lean — don't put heavy computation in coroutines
- Rate-limit STT processing (min 2s between transcriptions)
- Consider a dedicated thread for gateway heartbeat in high-load scenarios

---

## 6. Multi-User DJ Priority Rotation

**Design**: When multiple users join `/randommusic`, the first user (priority) gets more songs.

```
1 user:  [A, A, A, A, ...]
2 users: [A, A, B, A, A, B, ...]  (priority gets 2:1)
3 users: [A, A, A, B, C, A, A, A, B, C, ...]  (priority gets N:1:1)
N users: priority gets N songs, everyone else gets 1, then cycle
```

**Why**: The person who started the DJ session has invested interest. Others who join later get some rotation but don't hijack the vibe.

---

## 7. Memory Architecture (No External DB)

**Design choice**: All memory is file-based. No Redis, no PostgreSQL, no MongoDB. This means:
- Zero infrastructure — just `git clone` and run
- Memory survives restarts (it's on disk)
- Easy to inspect/debug (just `cat memory/guild_id/channels/123.md`)
- Easy to back up (just `cp -r memory/`)

**Tradeoff**: Doesn't scale to 1000+ guilds. Fine for small-medium bots.

**Tiers**:
| Tier | Storage | Injected into prompt? | Search method |
|------|---------|----------------------|---------------|
| Channel logs | `channels/<id>.md` | No | `recall_memory` tool (ALL-word match) |
| Summaries | `memory_2.md` | Yes (400 char cap) | Always visible |
| Actions | `actions.md` | No | Auto-searched by recall |
| User graph | `users.xml` | No | Parsed on demand |

---

## 8. Provider Fallback Chain

**Problem**: AI APIs go down. A lot. If your bot depends on one API, it dies when that API dies.

**Solution**: Cascading fallback with automatic retry:
```
Kimi K2.5 (Fireworks) → DeepSeek R1 (Fireworks) → Groq (Llama) → error message
```

Each model has different strengths:
- **Kimi K2.5**: Best reasoning, tool calling
- **DeepSeek R1**: Good fallback, thinking model
- **Groq**: Fast but less capable, free tier

The fallback is transparent — Blood doesn't know which model answered. Response format is normalized.

---

## 9. Agentic Tool-Call Loop

**Design**: Blood doesn't just respond — it uses tools in a loop until satisfied.

```
User message → AI response
  ├── If tool_calls in response:
  │    ├── Execute tools (parallel when possible)
  │    ├── Append results to conversation
  │    └── Call AI again (loop, max 8 iterations)
  └── If no tool_calls:
       └── Send final text response
```

**Safety**:
- Loop detection: block after 3 identical tool calls
- Max iterations: 8 (prevents infinite loops)
- Autonomous tool limits (e.g., timeout_user max 2 min when self-initiated)
- Permission checks per tool per tier

---

## 10. Voice Data Flow (Complete)

```
User speaks in Discord VC
    │
    ▼
discord-ext-voice-recv decodes Opus → PCM
    │
    ▼
on_voice_data(user, pcm_bytes)
    │  Always buffers. Never drops.
    ▼
UserAudioBuffer (per-user, circular, max ~10s)
    │  Detects 1.5s silence gap
    ▼
_process_loop (every 0.5s)
    │  Rate-limited: min 2s between STT calls
    ▼
PCM → WAV (in-memory) → Groq Whisper API
    │
    ▼
Transcription text
    │  Junk filter → wake word check
    ▼
_check_music_request (AI intent classifier, if relevant)
    │  or
_generate_response (general AI response)
    │
    ▼
Edge-TTS → MP3 bytes
    │
    ▼
If music playing:
    └── mixer.feed_tts_sync(mp3_data) → mixes with music
If no music:
    └── vc.play(FFmpegSource) → plays directly
```

---

## 11. Spotify Mood-Based DJ System

**Problem**: AI-only recommendations plateau fast — the LLM suggests the same artists repeatedly and has no understanding of audio characteristics.

**Solution**: Hybrid system — AI detects mood from chat context, Spotify API provides musically-accurate recommendations.

**Flow per DJ pick**:
```
1. AI mood detection (cached 10 min):
   - Input: last 10 chat messages + last 3 skipped + last 3 liked + time of day
   - Output: one word — chill, hype, sad, focus, angry, neutral
   
2. Map mood → Spotify audio feature targets:
   - chill  → valence 0.3-0.6, energy 0.2-0.5, tempo 70-110
   - hype   → valence 0.6-1.0, energy 0.7-1.0, tempo 120-160
   - sad    → valence 0.0-0.3, energy 0.1-0.4, tempo 60-100
   - etc.

3. Resolve Spotify IDs for user's top 5 liked songs (cached)

4. Call Spotify /recommendations with seed tracks + mood targets → 15 tracks

5. Cache batch, serve one at a time

6. Search each track on YouTube/SoundCloud via extract_track
```

**Fallback**: If Spotify API fails (no keys, rate limit, no liked songs), falls back to pure AI recommendations. The user never notices — same interface.

**Auth**: Client Credentials flow (no user login needed). Token cached until expiry.

---

## 12. yt-dlp Age-Restricted Fallback

**Problem**: YouTube age-restricts random music videos. `yt-dlp` fails with "Sign in to confirm your age" — kills the entire search.

**Solution**: Multi-result search with per-entry error handling + platform fallback.

```
ytsearch5:"Artist - Song audio"
    ├── entry 1: age-restricted → skip
    ├── entry 2: too long (>15min) → skip
    ├── entry 3: "cover" in title → skip
    ├── entry 4: ✓ good → use this
    └── entry 5: (never reached)

If ALL 5 YouTube entries fail:
    └── scsearch3:"Artist - Song"  (SoundCloud fallback)
         ├── Same _is_bad() filter applies
         └── First good result → use it
```

**Key**: Wrap each `ydl.extract_info()` in its own try/catch. The age-restricted error happens per-entry, not per-search. Don't let one bad entry kill the whole batch.

---

## 13. Reliable Music Skip (stop_music)

**Problem**: `skip_music` said "Skipped" but the old song kept playing. The FFmpeg process was killed but audio frames were still in the buffer.

**Root cause**: `proc.stdout.read(FRAME_SIZE)` is a blocking call. Even after `proc.kill()`, the reader thread could be mid-read, and frames written between `kill()` and `thread.join()` would linger in the deque.

**Fix** (order matters):
```python
def stop_music(self):
    self._music_stopping = True      # 1. Signal reader thread to stop
    self._has_music = False
    self._music_buf.clear()           # 2. Clear buffer FIRST (read() returns silence immediately)
    proc.stdout.close()               # 3. Close stdout (unblocks reader's .read())
    proc.kill()                       # 4. Kill FFmpeg
    thread.join(timeout=2)            # 5. Wait for reader thread
    self._music_buf.clear()           # 6. Clear AGAIN (catch frames written between 2-5)
```

**Also**: When a track ends naturally, the reader thread must wait for the buffer to drain before firing `on_music_end`. Otherwise the last ~5 seconds of every song gets cut off:
```python
# In reader thread finally block:
while self._music_buf and not self._music_stopping:
    time.sleep(0.05)  # wait for Discord's read() to consume remaining frames
```

---

## 14. Tool Permission Architecture

**Problem**: Regular users couldn't use music via `@blood play X` even though slash commands worked. Blood would say "I can't do that" and tell them to use `/play`.

**Root cause**: `tool_permissions` config defaults unlisted tools to `["owner"]`:
```python
allowed = CONFIG["tool_permissions"].get(tool_name, ["owner"])
```

Music tools weren't listed → only owner got them → AI literally couldn't see `play_music` for regular users.

**Fix**: Explicitly list every tool that should be available to users:
```python
"play_music":   ["user", "mod", "admin", "owner"],
"skip_music":   ["user", "mod", "admin", "owner"],
```

**Lesson**: The default-to-owner pattern is safe (least privilege) but means new tools are invisible until explicitly granted. Always add new tools to `tool_permissions` when creating them.

**Prompt reinforcement**: Even with tools available, the AI may still choose not to use them. Adding a `MUSIC:` section to the system prompt ("ALWAYS use the tool, never say do it yourself") was necessary to get reliable tool usage.
