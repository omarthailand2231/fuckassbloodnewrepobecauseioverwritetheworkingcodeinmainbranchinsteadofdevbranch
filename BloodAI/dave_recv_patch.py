"""
DAVE (E2EE) decryption for discord-ext-voice-recv's RECEIVE path.

Discord voice is end-to-end encrypted since March 2026 (DAVE protocol).
discord.py 2.7 + davey negotiates the E2EE session and encrypts what we SEND,
but voice_recv 0.5.2a179 (latest upstream) only strips the transport layer on
RECEIVE — so in an E2EE call every incoming frame is still DAVE ciphertext,
the opus decoder sees garbage ("corrupted stream"), and transcription dies.
See ~/Downloads/VC_bug_find.md §3 and voice_recv issue #53.

This module wraps each AudioReader's decryptor at the exact seam where the
transport-decrypted payload is produced (reader.py:141) and, when discord.py's
DaveSession is live, decrypts the E2EE layer with the SAME session object
discord.py maintains (commits/welcomes/rekeys stay discord.py's job).

Import once, early (top of bot.py), BEFORE any voice connection exists.
Kill switch: DAVE_RECV_PATCH=off (env) skips installation entirely.

Failure philosophy (matches the resilient opus decoder in voice.py): never
raise into voice_recv's socket callback, never feed ciphertext downstream —
substitute the 3-byte opus silence frame and count. Substituted silence is
inert: for unknown ssrcs reader.py drops silence packets outright, and for
known ssrcs the decoder emits 20ms of quiet that the STT junk filter ignores.
"""
import logging
import os
import weakref

log = logging.getLogger("blood.dave_recv")

_armed = weakref.WeakKeyDictionary()  # AudioReader -> counters (debug/status)


def get_status() -> dict:
    """Counters for every live wrapped reader — for ad-hoc debugging."""
    return {repr(r): dict(s) for r, s in _armed.items()}


def _install() -> None:
    if os.getenv("DAVE_RECV_PATCH", "on").lower() in ("off", "0", "false", "no"):
        log.warning("[DAVE-RECV] disabled via DAVE_RECV_PATCH env — E2EE receive stays broken")
        return
    try:
        import davey
    except ImportError:
        # No davey -> the bot can't negotiate E2EE -> received media is plain
        # opus and stock voice_recv handles it. Nothing to do.
        log.info("[DAVE-RECV] davey not installed — patch not needed, skipping")
        return
    try:
        from discord.ext.voice_recv import reader as reader_mod
        from discord.ext.voice_recv.rtp import OPUS_SILENCE
    except Exception as e:
        log.error("[DAVE-RECV] voice_recv not importable (%s) — patch NOT applied", e)
        return

    AudioReader = reader_mod.AudioReader
    if getattr(AudioReader, "_dave_recv_patched", False):
        return  # idempotent (module double-import, hotreload quirks)

    MEDIA_AUDIO = davey.MediaType.audio
    _orig_init = AudioReader.__init__

    def _wrap_decryptor(reader) -> None:
        vc = reader.voice_client               # set by original __init__ (reader.py:49)
        orig_decrypt_rtp = reader.decryptor.decrypt_rtp   # bound instance attr (reader.py:200)
        state = {"ok": 0, "fail": 0, "nouser": 0, "bypass": 0, "announced": False}

        def dave_decrypt_rtp(packet):
            # 1) Transport layer off — unchanged. update_secret_key() only swaps
            #    decryptor.box, so this wrapper survives key rotation. Transport
            #    CryptoError propagates exactly as before (reader.py:148 drops it).
            data = orig_decrypt_rtp(packet)

            # 2) Is this call E2EE right now? Same condition discord.py uses for
            #    its send side (voice_state.py `can_encrypt`). Re-read per packet:
            #    the session appears/reinits/downgrades mid-call and we must track it.
            conn = getattr(vc, "_connection", None)
            session = getattr(conn, "dave_session", None)
            if (
                session is None
                or getattr(conn, "dave_protocol_version", 0) == 0
                or not session.ready
            ):
                state["bypass"] += 1
                return data                    # plaintext call / transition window

            # 3) Silence keepalives (0xF8FFFE) are sent outside the E2EE layer.
            if data == OPUS_SILENCE:
                return data

            if not state["announced"]:
                state["announced"] = True
                log.info("[DAVE-RECV] E2EE active (protocol v%s) — decrypting received audio",
                         getattr(conn, "dave_protocol_version", "?"))

            # 4) DAVE decrypt needs the SENDER. Audio ssrcs are mapped from the
            #    speaking/client_connect ops (voice_recv gateway.py:78,93); video
            #    ssrcs and first-instant packets aren't in the map -> substitute
            #    silence (reader.py:172 then drops it for unknown ssrcs).
            user_id = vc._get_id_from_ssrc(packet.ssrc)
            if user_id is None:
                state["nouser"] += 1
                return OPUS_SILENCE

            # 5) The E2EE layer itself. davey handles per-user passthrough
            #    windows internally (DecryptionStats.passthroughs) and raises
            #    ValueError on genuine failure ("Failed to decrypt:
            #    NoDecryptorForUser"). Never let ciphertext continue downstream.
            try:
                out = session.decrypt(user_id, MEDIA_AUDIO, bytes(data))
                state["ok"] += 1
                if state["ok"] == 1:
                    log.info("[DAVE-RECV] first frame decrypted OK (user %s) — receive path is live",
                             user_id)
                return out
            except Exception as e:
                n = state["fail"] = state["fail"] + 1
                if n in (1, 5, 25, 100) or n % 500 == 0:
                    log.warning("[DAVE-RECV] decrypt failed (#%d, ok=%d nouser=%d): %s — "
                                "substituting silence", n, state["ok"], state["nouser"], e)
                return OPUS_SILENCE

        reader.decryptor.decrypt_rtp = dave_decrypt_rtp
        _armed[reader] = state
        log.info("[DAVE-RECV] reader armed for guild %s",
                 getattr(getattr(vc, "guild", None), "id", "?"))

    def _patched_init(self, *args, **kwargs):
        _orig_init(self, *args, **kwargs)
        try:
            _wrap_decryptor(self)
        except Exception as e:
            # Never break reader construction — worst case we revert to the
            # stock (E2EE-blind) behavior for this reader and say so loudly.
            log.error("[DAVE-RECV] failed to arm reader: %s — this session is E2EE-blind", e)

    AudioReader.__init__ = _patched_init
    AudioReader._dave_recv_patched = True
    log.info("[DAVE-RECV] installed (davey protocol v%s, voice_recv %s)",
             getattr(davey, "DAVE_PROTOCOL_VERSION", "?"),
             getattr(__import__("discord.ext.voice_recv", fromlist=["__version__"]),
                     "__version__", "?"))


_install()
