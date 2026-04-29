"""
Blood Spotify Integration — audio features + mood-based recommendations.

Uses Spotify Web API (Client Credentials flow) to:
- Search tracks and get Spotify IDs
- Fetch audio features (valence, energy, danceability, tempo, acousticness)
- Get recommendations seeded by tracks + target audio features
"""

import os
import logging
import time
from typing import Optional

log = logging.getLogger("blood.spotify")

# ── Config ────────────────────────────────────────────────────────────────────

SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "")

# ── Auth Token Cache ─────────────────────────────────────────────────────────

_token: Optional[str] = None
_token_expires: float = 0.0


async def _get_token() -> Optional[str]:
    """Get a valid Spotify access token (Client Credentials flow)."""
    global _token, _token_expires
    if _token and time.time() < _token_expires - 60:
        return _token

    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        log.warning("No Spotify credentials — set SPOTIFY_CLIENT_ID + SPOTIFY_CLIENT_SECRET")
        return None

    import aiohttp
    import base64

    auth_str = f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}"
    b64_auth = base64.b64encode(auth_str.encode()).decode()

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://accounts.spotify.com/api/token",
                headers={"Authorization": f"Basic {b64_auth}",
                         "Content-Type": "application/x-www-form-urlencoded"},
                data={"grant_type": "client_credentials"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    log.warning("Spotify auth failed: %d", resp.status)
                    return None
                data = await resp.json()
                _token = data["access_token"]
                _token_expires = time.time() + data.get("expires_in", 3600)
                log.info("Spotify token acquired (expires in %ds)", data.get("expires_in", 3600))
                return _token
    except Exception as e:
        log.warning("Spotify auth error: %s", e)
        return None


async def _api_get(endpoint: str, params: dict = None) -> Optional[dict]:
    """Make an authenticated GET request to Spotify API."""
    token = await _get_token()
    if not token:
        return None

    import aiohttp
    url = f"https://api.spotify.com/v1/{endpoint}"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                params=params or {},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 401:
                    # Token expired — force refresh
                    global _token_expires
                    _token_expires = 0
                    return None
                if resp.status != 200:
                    log.debug("Spotify API %s returned %d", endpoint, resp.status)
                    return None
                return await resp.json()
    except Exception as e:
        log.warning("Spotify API error: %s", e)
        return None


# ── Public API ───────────────────────────────────────────────────────────────

async def search_track(query: str) -> Optional[dict]:
    """Search for a track on Spotify.
    Returns: {"id": "...", "name": "...", "artist": "...", "uri": "..."} or None."""
    data = await _api_get("search", {"q": query, "type": "track", "limit": 1})
    if not data:
        return None
    items = data.get("tracks", {}).get("items", [])
    if not items:
        return None
    t = items[0]
    artists = ", ".join(a["name"] for a in t.get("artists", []))
    return {
        "id": t["id"],
        "name": t["name"],
        "artist": artists,
        "uri": t["uri"],
    }


async def get_audio_features(track_ids: list[str]) -> list[dict]:
    """Get audio features for a list of track IDs (max 100).
    Returns list of feature dicts with: valence, energy, danceability, tempo, acousticness."""
    if not track_ids:
        return []
    ids_str = ",".join(track_ids[:100])
    data = await _api_get("audio-features", {"ids": ids_str})
    if not data:
        return []
    features = []
    for f in data.get("audio_features", []):
        if f:
            features.append({
                "id": f.get("id"),
                "valence": f.get("valence", 0.5),
                "energy": f.get("energy", 0.5),
                "danceability": f.get("danceability", 0.5),
                "tempo": f.get("tempo", 120),
                "acousticness": f.get("acousticness", 0.5),
                "instrumentalness": f.get("instrumentalness", 0.0),
            })
    return features


async def get_recommendations(
    seed_track_ids: list[str],
    target_features: dict,
    limit: int = 15,
) -> list[str]:
    """Get Spotify recommendations based on seed tracks and target audio features.
    target_features: {"target_valence": 0.7, "target_energy": 0.8, ...}
    Returns list of "Artist - Title" strings."""
    if not seed_track_ids:
        return []

    params = {
        "seed_tracks": ",".join(seed_track_ids[:5]),
        "limit": min(limit, 50),
    }
    # Add target features
    for key, val in target_features.items():
        params[key] = val

    data = await _api_get("recommendations", params)
    if not data:
        return []

    results = []
    for track in data.get("tracks", []):
        artists = ", ".join(a["name"] for a in track.get("artists", []))
        name = track.get("name", "")
        if artists and name:
            results.append(f"{artists} - {name}")
    return results


# ── Mood → Spotify Feature Mapping ──────────────────────────────────────────

MOOD_FEATURES = {
    "chill": {
        "target_valence": 0.4,
        "target_energy": 0.3,
        "target_danceability": 0.4,
        "min_acousticness": 0.2,
    },
    "hype": {
        "target_valence": 0.8,
        "target_energy": 0.85,
        "target_danceability": 0.75,
        "max_acousticness": 0.3,
    },
    "sad": {
        "target_valence": 0.15,
        "target_energy": 0.3,
        "max_danceability": 0.5,
        "min_acousticness": 0.3,
    },
    "focus": {
        "target_energy": 0.4,
        "target_valence": 0.4,
        "min_instrumentalness": 0.3,
        "max_danceability": 0.6,
    },
    "angry": {
        "target_energy": 0.9,
        "target_valence": 0.3,
        "min_danceability": 0.4,
        "max_acousticness": 0.2,
    },
    "neutral": {
        "target_valence": 0.5,
        "target_energy": 0.5,
        "target_danceability": 0.5,
    },
}


async def get_mood_recommendations(
    mood: str,
    seed_track_ids: list[str],
    limit: int = 15,
) -> list[str]:
    """Get recommendations matching a mood.
    mood: one of 'chill', 'hype', 'sad', 'focus', 'angry', 'neutral'
    seed_track_ids: Spotify track IDs from user's liked songs
    Returns list of "Artist - Title" strings."""
    features = MOOD_FEATURES.get(mood, MOOD_FEATURES["neutral"])
    return await get_recommendations(seed_track_ids, features, limit)


# ── Spotify ID Cache ─────────────────────────────────────────────────────────

# In-memory cache: "Artist - Title" -> spotify_id
_id_cache: dict[str, str] = {}


async def resolve_spotify_ids(song_titles: list[str]) -> list[str]:
    """Look up Spotify IDs for a list of 'Artist - Title' strings.
    Returns list of IDs (skips unresolvable ones)."""
    ids = []
    for title in song_titles:
        if title in _id_cache:
            ids.append(_id_cache[title])
            continue
        result = await search_track(title)
        if result:
            _id_cache[title] = result["id"]
            ids.append(result["id"])
    return ids
