"""Per-user join sounds.

When a member joins a voice channel that Blood is sitting in, Blood plays a
short personal sound clip for them — if one has been dropped on disk.

Folder layout (everything lives under ``on_join_sfx/`` next to this file)::

    on_join_sfx/
        @someusername/
            whatever.mp3        <- played when @someusername joins VC
        @anotheruser/
            airhorn.mp3
        README.txt

One folder per member, named after their Discord ``@`` handle. Drop any audio
file (mp3 / wav / ogg / m4a / flac / opus / aac) inside the folder and it'll be
used as that user's join sound. No restart strictly required — the folder is
read at join time — but a restart re-creates folders for anyone new.

The structure is intentionally "one folder per user" so it can be expanded
later (e.g. an ``on_leave_sfx/`` sibling root, or per-user subfolders for other
events) without changing the on-disk convention.
"""

import os
import json
import logging
from typing import Optional

log = logging.getLogger("blood.join_sfx")

# Root folder that holds one sub-folder per member.
SFX_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "on_join_sfx")

# Audio formats FFmpeg can decode — anything here works as a join sound.
AUDIO_EXTS = (".mp3", ".wav", ".ogg", ".m4a", ".flac", ".opus", ".aac")

# How deep to look for an audio file inside a user's folder.
_MAX_DEPTH = 2

_README = """\
BLOOD — PER-USER JOIN SOUNDS
============================

When someone joins a voice channel that Blood is in, Blood plays their personal
sound clip — if there's one in their folder here.

HOW TO USE
----------
1. Find (or create) the folder named after the person's Discord @ handle.
   Example: for @lenny the folder is "@lenny".
   (Folders are auto-created for every server member when the bot starts.)
2. Drop an audio file into that folder. mp3, wav, ogg, m4a, flac, opus and aac
   all work. Name it whatever you like.
3. That's it. Next time that user joins the VC, the sound plays.
   If you just added the file, they only need to re-join — no bot restart needed.
   (Run /joinsfx in Discord to re-create folders for any new members.)

NOTES
-----
- If a folder has more than one audio file, the first one alphabetically is used.
- Matching is case-insensitive and also accepts the user's display name or their
  numeric user ID as the folder name, so "@Lenny", "lenny" and "123456789012345"
  would all match the same person.
- The sound is layered over any music that's playing (the music ducks briefly),
  or plays on its own if nothing else is going on.
"""


# ── Enable / disable toggle (per-guild, persisted) ────────────────────────────
# Defaults to ON. Turn off during serious talk so chaos doesn't interrupt.
_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "data", "join_sfx_state.json")
_state_cache: Optional[dict] = None


def _load_state() -> dict:
    global _state_cache
    if _state_cache is None:
        try:
            with open(_STATE_FILE, "r", encoding="utf-8") as f:
                _state_cache = json.load(f)
        except Exception:
            _state_cache = {}
    return _state_cache


def _save_state() -> None:
    try:
        os.makedirs(os.path.dirname(_STATE_FILE), exist_ok=True)
        with open(_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(_state_cache or {}, f)
    except Exception as e:
        log.warning("[JOIN-SFX] could not save state: %s", e)


def is_enabled(guild_id) -> bool:
    """Whether join sounds are on for this guild (default True)."""
    return bool(_load_state().get(str(guild_id), True))


def set_enabled(guild_id, enabled: bool) -> bool:
    state = _load_state()
    state[str(guild_id)] = bool(enabled)
    _save_state()
    return bool(enabled)


def _sanitize(name: str) -> str:
    """Make a string safe to use as a folder name on macOS/Windows/Linux."""
    bad = '<>:"/\\|?*\0'
    cleaned = "".join("_" if c in bad else c for c in str(name))
    cleaned = cleaned.strip().rstrip(".")
    return cleaned or "unknown"


def folder_name_for(member) -> str:
    """Canonical folder name for a member: ``@username``."""
    uname = (getattr(member, "name", None)
             or getattr(member, "display_name", None)
             or str(getattr(member, "id", "unknown")))
    return _sanitize(f"@{uname}")


def _candidate_names(member) -> list[str]:
    """Folder names that should match this member, most-specific first."""
    raw = []
    for base in (getattr(member, "name", None),
                 getattr(member, "global_name", None),
                 getattr(member, "display_name", None)):
        if base:
            raw.append(f"@{base}")
            raw.append(base)
    uid = str(getattr(member, "id", "") or "")
    if uid:
        raw.append(uid)
        raw.append(f"@{uid}")

    seen, out = set(), []
    for c in raw:
        s = _sanitize(c)
        key = s.lower()
        if key not in seen:
            seen.add(key)
            out.append(s)
    return out


def ensure_root() -> None:
    """Create the SFX root + README if missing."""
    os.makedirs(SFX_ROOT, exist_ok=True)
    readme = os.path.join(SFX_ROOT, "README.txt")
    if not os.path.exists(readme):
        try:
            with open(readme, "w", encoding="utf-8") as f:
                f.write(_README)
        except Exception as e:
            log.warning("[JOIN-SFX] could not write README: %s", e)


def ensure_user_folder(member) -> str:
    """Create (if needed) and return the folder path for a single member."""
    ensure_root()
    path = os.path.join(SFX_ROOT, folder_name_for(member))
    os.makedirs(path, exist_ok=True)
    return path


def ensure_guild_folders(guild) -> int:
    """Create a folder for every non-bot member of the guild.

    Returns the number of member folders ensured.
    """
    ensure_root()
    count = 0
    for m in getattr(guild, "members", []):
        if getattr(m, "bot", False):
            continue
        try:
            ensure_user_folder(m)
            count += 1
        except Exception as e:
            log.warning("[JOIN-SFX] could not create folder for %s: %s",
                        getattr(m, "display_name", "?"), e)
    return count


def _find_sound_in_dir(folder: str, depth: int = 0) -> Optional[str]:
    """First audio file in ``folder`` (alphabetical), searching subfolders too."""
    if not os.path.isdir(folder):
        return None
    try:
        entries = sorted(os.listdir(folder))
    except Exception:
        return None
    # Files in this directory take priority.
    for name in entries:
        if name.startswith("."):
            continue
        full = os.path.join(folder, name)
        if os.path.isfile(full) and name.lower().endswith(AUDIO_EXTS):
            return full
    # Then descend into subfolders (e.g. on_join_sfx/@user/on_join_sfx/x.mp3).
    if depth < _MAX_DEPTH:
        for name in entries:
            if name.startswith("."):
                continue
            full = os.path.join(folder, name)
            if os.path.isdir(full):
                found = _find_sound_in_dir(full, depth + 1)
                if found:
                    return found
    return None


def resolve_join_sound(member) -> Optional[str]:
    """Path to this member's join sound, or ``None`` if they don't have one."""
    if not os.path.isdir(SFX_ROOT):
        return None
    try:
        existing = {d.lower(): os.path.join(SFX_ROOT, d)
                    for d in os.listdir(SFX_ROOT)
                    if os.path.isdir(os.path.join(SFX_ROOT, d))}
    except Exception:
        return None
    for cand in _candidate_names(member):
        folder = existing.get(cand.lower())
        if folder:
            sound = _find_sound_in_dir(folder)
            if sound:
                return sound
    return None


def list_configured() -> list[str]:
    """Folder names that currently have an audio file in them (for status)."""
    if not os.path.isdir(SFX_ROOT):
        return []
    out = []
    try:
        for d in sorted(os.listdir(SFX_ROOT)):
            full = os.path.join(SFX_ROOT, d)
            if os.path.isdir(full) and _find_sound_in_dir(full):
                out.append(d)
    except Exception:
        pass
    return out
