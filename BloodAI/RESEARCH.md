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
         ├── steps _effective_vol toward target (smooth fade)
         ├── mixes PCM samples (clamp to int16)
         └── returns 3840 bytes (20ms @ 48kHz stereo 16-bit)

Music reader thread
    └── reads FFmpeg stdout → appends to music_buf
    └── back-pressure: sleeps when buf > 250 frames

TTS reader thread
    └── reads FFmpeg stdout → appends to tts_buf
    └── started on-demand when Blood speaks
```

**Key constants**:
- Frame size: 3840 bytes (20ms @ 48kHz, stereo, 16-bit)
- 1920 samples per frame
- Duck volume: 0.15 (music drops to 15% when TTS active)
- Back-pressure threshold: 250 frames (~5 seconds)

**Gotchas**:
- Don't use `deque(maxlen=N)` for the music buffer — it silently drops oldest frames, causing chipmunk/speedup effect. Use manual back-pressure in the reader thread instead.
- Use `array.array('h', data)` for PCM manipulation — `struct.unpack` is too slow for 1920 samples per frame at 50fps.
- `numpy` would be even faster but `array.array` has zero dependencies and works on Python 3.14+.
- FFmpeg args must include `-ac 2 -ar 48000 -f s16le` to match Discord's expected format.

---

## 2. Smooth Audio Fades (Duck / Unduck / Song Fade-in)

**Problem**: Instant volume jumps when music ducks for TTS or when a new song starts sound jarring and cheap. Real radio/broadcast always fades.

**Solution**: Track `_effective_vol` in the mixer and interpolate it toward the target every `read()` call (every 20ms).

**Implementation** (`mixer.py`):
```python
# Per read() call (20ms frame):
target_vol = _duck_vol if ducking else _music_vol

# Pre-fade: start unduck while TTS is still finishing
if ducking and 0 < len(tts_buf) <= _PRE_FADE_FRAMES:
    target_vol = _music_vol  # override — music rises before last word ends

if _effective_vol < target_vol:
    _effective_vol = min(target_vol, _effective_vol + _UNDUCK_RATE)
elif _effective_vol > target_vol:
    _effective_vol = max(target_vol, _effective_vol - _DUCK_RATE)
```

**Fade constants**:
```
_DUCK_RATE        = 0.030   # 0.8→0.15 in ~22 frames ≈ 0.4s  (fast — voice is prominent)
_UNDUCK_RATE      = 0.018   # 0.15→0.8 in ~36 frames ≈ 0.7s  (slow — music eases back)
_SONG_FADEIN_RATE = 0.025   # 0→0.8 in ~32 frames ≈ 0.5s     (new track fade-in)
_PRE_FADE_FRAMES  = 25      # ~500ms before TTS buffer empties  (early unduck start)
```

**Song fade-in**: `_effective_vol` is reset to `0.0` in `start_music()`. Every new track fades in from silence. No abrupt starts.

**Pre-fade crossover**: When the TTS buffer has ≤25 frames (~500ms) remaining, `target_vol` flips back to `_music_vol` even while TTS is still active. Music and voice overlap briefly at the end, creating a natural crossover feel instead of an abrupt unmute.

**Broadcast audio principle**: Fade-down faster than fade-up. The voice should appear instantly prominent; the music should ease back in gently. `_DUCK_RATE > _UNDUCK_RATE` encodes this.

---

## 3. Voice Receive + STT Pipeline

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

## 4. AI Music Recommendations (Spotify-like)

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

## 5. AI Intent Classification vs Keywords

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

## 6. Discord Gateway Heartbeat + Event Loop Health

**Problem**: `"Shard has stopped responding to the gateway. Closing and restarting."` — Discord kills the bot's connection if heartbeats are missed.

**Root cause**: Too many concurrent async operations (AI API calls, yt-dlp extractions, TTS generation) saturate the event loop. The gateway heartbeat (~41.25s interval) doesn't get serviced in time.

**Mitigations**:
- Minimize unnecessary API calls (pre-filter before AI classifier)
- Use `run_in_executor` for blocking operations (yt-dlp, FFmpeg)
- Keep the event loop lean — don't put heavy computation in coroutines
- Rate-limit STT processing (min 2s between transcriptions)
- Consider a dedicated thread for gateway heartbeat in high-load scenarios

---

## 7. Multi-User DJ Priority Rotation

**Design**: When multiple users join `/randommusic`, the first user (priority) gets more songs.

```
1 user:  [A, A, A, A, ...]
2 users: [A, A, B, A, A, B, ...]  (priority gets 2:1)
3 users: [A, A, A, B, C, A, A, A, B, C, ...]  (priority gets N:1:1)
N users: priority gets N songs, everyone else gets 1, then cycle
```

**Why**: The person who started the DJ session has invested interest. Others who join later get some rotation but don't hijack the vibe.

---

## 8. Memory Architecture (No External DB)

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

## 9. Provider Fallback Chain

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

## 10. Agentic Tool-Call Loop

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

## 11. Voice Data Flow (Complete)

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
    └── mixer.feed_tts_sync(mp3_data) → fades music down, mixes TTS, fades back up
If no music:
    └── vc.play(FFmpegSource) → plays directly
```

---

## 12. Spotify Mood-Based DJ System

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

## 13. yt-dlp Age-Restricted Fallback

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

## 14. Reliable Music Skip (stop_music)

**Problem**: `skip_music` said "Skipped" but the old song kept playing. The FFmpeg process was killed but audio frames were still in the buffer.

**Root cause**: `proc.stdout.read(FRAME_SIZE)` is a blocking call. Even after `proc.kill()`, the reader thread could be mid-read, and frames written between `kill()` and `thread.join()` would linger in the deque.

**Fix** (order matters):
```python
def stop_music(self):
    self._stop_event.set()         # 1. Signal reader thread to stop
    self._has_music = False
    self._music_buf.clear()        # 2. Clear buffer FIRST (read() returns silence immediately)
    proc.stdout.close()            # 3. Close stdout (unblocks reader's .read())
    proc.kill()                    # 4. Kill FFmpeg
    thread.join(timeout=2)         # 5. Wait for reader thread
    self._music_buf.clear()        # 6. Clear AGAIN (catch frames written between 2-5)
```

**Also**: When a track ends naturally, the reader thread must wait for the buffer to drain before firing `on_music_end`. Otherwise the last ~5 seconds of every song gets cut off.

---

## 15. Tool Permission Architecture

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

---

## 16. Gapless Audio Prebuffering

**Problem**: When one song ends and the next starts, there's a 1–3 second silence gap. FFmpeg needs time to connect, start decoding, and fill the buffer.

**Solution**: Start FFmpeg for the next track in a background thread while the current one is still playing. Buffer ~5 seconds of PCM, then hand off both the buffered frames AND the running FFmpeg process when the transition happens.

**Architecture** (`mixer.py`):
```
Current track playing
    │
    ▼
prebuffer_next(next_url)
    └── Background thread starts FFmpeg
    └── Reads up to 250 frames (~5s) into _prebuf deque
    └── Keeps FFmpeg process alive, sets _prebuf_ready event

Track ends → start_music(next_url)
    ├── Detects _prebuf_url matches → gapless path
    ├── Dumps prebuf frames into music_buf (instant)
    ├── Hands FFmpeg proc to reader thread (continues reading)
    └── Zero gap — music_buf was never empty
```

**Key details**:
- The FFmpeg process is NOT killed — it's transferred from the prefetch thread to the main reader thread
- If the prebuffered URL doesn't match (user skipped, etc.), the prebuffer is cancelled and a fresh FFmpeg starts
- `_prebuf_ready` event prevents `start_music()` from using a half-filled prebuffer

---

## 17. Radio DJ: Parallel Queue Preloading

**Problem**: Old radio system pre-queued one song at a time at the 50% mark. Short songs ended before the next song was extracted (yt-dlp takes 5–30s). Queue went empty → music stopped.

**Solution**: Always maintain `RADIO_QUEUE_TARGET = 3` songs in the queue. Fetch them in **parallel** using `asyncio.gather()`.

**Startup**:
```python
# Get 4 queries (1 to play now + 3 to queue)
queries = [await _get_radio_song(rdj) for _ in range(RADIO_QUEUE_TARGET + 1)]

# Extract all 4 in parallel — total time = slowest single extraction, not sum
results = await asyncio.gather(*[extract_track(q) for q in queries])

tracks = [r for r in results if r and not isinstance(r, Exception)]
first_track = tracks[0]
for t in tracks[1:]:
    mq.add(t)  # Queue the rest immediately
```

**Continuous refill** (`_fill_radio_queue()`):
- Called after every track change and every gapless transition
- Acquires `rdj._fill_lock` (asyncio.Lock) to prevent concurrent over-fills
- Calculates `needed = RADIO_QUEUE_TARGET - len(mq.queue)`
- Gets N queries sequentially (fast, just pops from `rdj._rec_cache`)
- Extracts N tracks in parallel
- Prebuffers `mq.queue[0]` after fill

**Critical edge case — frozen queue**: After refill adds songs but music isn't playing (e.g. a `/play` interrupted the queue), `_radio_loop` must detect this:
```python
if not current and not has_active_music(guild):
    if mq.queue:
        # Songs ready but nothing playing — restart
        nxt = mq.next()
        await play_track(guild, nxt, text_channel)
    else:
        asyncio.create_task(_fill_radio_queue(rdj, guild_id))
```
The `not mq.queue` condition alone is wrong — after fill completes, the queue is no longer empty but nobody started playing.

---

## 18. Auto-Rejoin Debouncing (4006 Reconnect Storm)

**Problem**: Discord close code 4006 ("session no longer valid") causes a rapid connect/disconnect cycle. Each disconnect fires `on_voice_state_update`, which triggers our auto-rejoin code. Without deduplication, 3+ concurrent rejoin attempts race against each other and each other's sessions. Each successful connect invalidates the others → more 4006s → infinite loop. Multiple radio loops get spawned simultaneously.

**Root cause sequence**:
1. Bot disconnected (code 4014 — forced kick)
2. Library starts 4006 reconnect cycle
3. Each library disconnect fires `on_voice_state_update(before=channel, after=None)`
4. Our code fires auto-rejoin for each event → N concurrent rejoin coroutines
5. Each creates a new voice session → invalidates all others → more 4006s
6. Each successful rejoin sees `rdj.is_active=True` → spawns new `_radio_loop` task
7. 2–3 radio loops running simultaneously

**Fix 1 — Debounce with a set**:
```python
_rejoining_guilds: set[str] = set()

async def on_voice_state_update(member, before, after):
    if member.id == bot.user.id:
        if before.channel and not after.channel:
            if guild_id in _rejoining_guilds:
                return  # Already in progress
            _rejoining_guilds.add(guild_id)
            try:
                await asyncio.sleep(3)
                result = await join_and_listen(...)
                # restart loops only if their task is done
            finally:
                _rejoining_guilds.discard(guild_id)
```

**Fix 2 — Task-based loop restart**:
```python
# Old (broken): starts duplicate if any condition is met
if rdj.is_active and not (mq._mixer and mq._mixer.has_music):
    asyncio.create_task(_radio_loop(...))

# New (correct): only restart if the task actually died
if rdj.is_active and (rdj._loop_task is None or rdj._loop_task.done()):
    rdj._loop_task = asyncio.create_task(_radio_loop(...))
```

Store `_loop_task` in both `RadioDJ` and `RandomDJ`. Set it in `start_radio()` and `start_random_music()`.

**Fix 3 — Handle "Already receiving audio"**:
When the library reconnects before our 3-second sleep fires, the voice client already has a listener. `voice_client.listen()` throws "Already receiving audio". This used to abort the rejoin and skip the loop restart check.

```python
try:
    if hasattr(vc, 'is_listening') and vc.is_listening():
        vc.stop_listening()
    vc.listen(voice_recv.BasicSink(callback))
except Exception as e:
    if "already receiving" in str(e).lower():
        pass  # Library reconnected first — listener is fine
    else:
        raise
```

---

## 19. TTS Drain Timeout + Stuck Ducking Fix

**Problem (original)**: `feed_tts_sync()` drain loop had no exit — if Discord's AudioPlayer stopped calling `read()`, TTS frames were never consumed, `_tts_active` stayed True, music was stuck at 15% volume permanently.

**Problem (newer)**: Even with the 15-second timeout, a disconnect during TTS could jump past the drain loop via an exception, leaving `_tts_active = True` since the assignment was after the try/except.

**Fix**: Use `try/finally` to guarantee cleanup:
```python
def feed_tts_sync(self, mp3_data):
    with self._tts_lock:
        self._tts_active = True
        try:
            # FFmpeg conversion into tts_buf
            ...
        except Exception as e:
            log.warning("TTS feed error: %s", e)
            self._tts_buf.clear()  # Don't drain what we couldn't fill

        try:
            drain_start = time.monotonic()
            while self._tts_buf:
                time.sleep(0.02)
                if time.monotonic() - drain_start > 15:
                    self._tts_buf.clear()
                    break
                if time.monotonic() - self._last_read_time > 1.0:
                    self._tts_buf.clear()  # AudioPlayer stopped
                    break
            time.sleep(0.1)
        finally:
            self._tts_buf.clear()   # Guarantee no leftover frames
            self._tts_active = False  # Guarantee duck is always released
```

**Why the double clear**: The `finally` clear ensures no orphaned frames can re-trigger ducking on the next TTS call if somehow the drain didn't finish.

---

## 20. Radio Queue + User /play Interaction

**Problem**: If a user calls `/play` while `/radio` is active, the user's song gets added to `mq.queue`. This interacts with `_fill_radio_queue()`'s `needed = RADIO_QUEUE_TARGET - len(mq.queue)` calculation — the user song inflates the count and delays radio refills.

**Bigger problem**: If the user's `/play` call triggers `play_track()` directly (because `has_active_music()` was False at that moment, e.g. during a queue-empty transition), a new mixer is created. `_play_next()` fires when that song ends. It calls `_fill_radio_queue()` in the background. The fill adds songs, but `_play_next()` already returned — nobody calls `play_track()` on the newly queued songs. They sit frozen.

**Why the naïve "queue-dry" check fails**:
```python
# This never fires after fill completes:
if not current and not has_active_music(guild) and not mq.queue:
    # mq.queue is NOT empty — fill ran and added songs
    # So this condition is always False
    # Songs sit in queue forever
```

**Fix**: Split the "not playing" check from the "queue empty" check:
```python
if not current and not has_active_music(guild):
    if mq.queue:
        # Songs ready but nothing playing — restart playback
        nxt = mq.next()
        await play_track(guild, nxt, text_channel)
        asyncio.create_task(_fill_radio_queue(rdj, guild_id))
    else:
        # Queue truly empty — trigger fill
        asyncio.create_task(_fill_radio_queue(rdj, guild_id))
```

The `_radio_loop` polls every 3 seconds, so worst-case the frozen songs sit for 3s before this branch detects and restarts.

**Also**: When `_play_next()` detects radio is active but queue is empty, stop the mixer cleanly so `has_active_music()` returns False — this lets the loop's "not playing" branch fire correctly:
```python
if rdj.is_active and not mq.queue:
    mq.current = None
    if mq._mixer:
        mq._mixer.stop_music()
        mq._mixer = None
    asyncio.create_task(_fill_radio_queue(rdj, guild_id))
    return
```
