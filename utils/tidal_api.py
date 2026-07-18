"""
utils/tidal_api.py

ISRC validation and Tidal contributor-credits lookup (unofficial public
Tidal client token). Filters contributors to songwriter/producer roles only.
"""

import re
import time

import requests

from utils.github_fetcher import _lookup_key

# unofficial public Tidal client token
TIDAL_TOKEN = "gsFXkJqGrUNoYMQPZe4k3WKwijnrp8iGSwn3bApe"
TIDAL_COUNTRY_CODE = "US"
TIDAL_REQUEST_TIMEOUT_SECONDS = 10
TIDAL_SLEEP_BETWEEN_CALLS_SECONDS = 0.1

TIDAL_ALLOWED_ROLE_KEYS = {"composer", "lyricist", "writer", "author", "producer"}
TIDAL_EXCLUDED_ROLE_KEYS = {
    "musicpublisher",
    "mixingengineer",
    "masteringengineer",
    "recordingengineer",
    "programmer",
    "musician",
    "featured",
    "mainartist",
}


def validate_isrc(isrc):
    if not isrc:
        return False
    clean_isrc = str(isrc).replace("-", "").strip()
    pattern = re.compile(r"^[A-Z]{2}[A-Z0-9]{3}\d{2}\d{5}$", re.IGNORECASE)
    return bool(pattern.match(clean_isrc))


# --------------------------------------------------------------------------
# Tidal credits lookup by ISRC
# --------------------------------------------------------------------------
def _role_key(role):
    return re.sub(r"[\s_\-]+", "", str(role or "").strip()).lower()


def _is_allowed_tidal_role(role):
    """Allow only songwriter/producer roles; exclude publishers, engineers, artists, etc."""
    role_text = str(role or "").strip()
    if not role_text:
        return False

    key = _role_key(role_text)
    if key in TIDAL_EXCLUDED_ROLE_KEYS:
        return False
    if key in TIDAL_ALLOWED_ROLE_KEYS:
        return True

    # Defensive handling for rare combined role strings such as "Composer/Lyricist".
    parts = re.split(r"[,;/|&]+", role_text)
    return any(_role_key(part) in TIDAL_ALLOWED_ROLE_KEYS for part in parts)


def _extract_items(data):
    """Tidal responses normally contain an 'items' list; keep this tolerant."""
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []

    items = data.get("items")
    if isinstance(items, list):
        return items

    tracks = data.get("tracks")
    if isinstance(tracks, dict) and isinstance(tracks.get("items"), list):
        return tracks["items"]

    return []


def _tidal_get(url, params=None, retries=3):
    """
    Safe Tidal GET wrapper. Never raises to the UI path.
    Returns (json_data, note_if_failed).
    """
    headers = {"X-Tidal-Token": TIDAL_TOKEN}
    last_note = "Tidal request failed — used Spotify artists as fallback"

    for _ in range(retries):
        try:
            resp = requests.get(
                url,
                headers=headers,
                params=params,
                timeout=TIDAL_REQUEST_TIMEOUT_SECONDS,
            )

            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After", "1")
                try:
                    wait_seconds = max(float(retry_after), 0.0)
                except (TypeError, ValueError):
                    wait_seconds = 1.0
                time.sleep(wait_seconds)
                last_note = "Tidal rate limit — used Spotify artists as fallback"
                continue

            if not resp.ok:
                return None, f"Tidal HTTP {resp.status_code} — used Spotify artists as fallback"

            return resp.json(), None

        except requests.RequestException:
            last_note = "Tidal request failed — used Spotify artists as fallback"
        except ValueError:
            return None, "Tidal response invalid — used Spotify artists as fallback"
        finally:
            time.sleep(TIDAL_SLEEP_BETWEEN_CALLS_SECONDS)

    return None, last_note


def fetch_tidal_contributors_by_isrc(isrc):
    """
    Finds the first Tidal track by ISRC and returns unique contributor names for
    Composer/Lyricist/Writer/Author/Producer roles only.
    Returns (names, note_if_fallback_needed).
    """
    try:
        clean_isrc = str(isrc or "").replace("-", "").strip().upper()
        if not clean_isrc:
            return [], "Missing ISRC — used Spotify artists as fallback"
        if not validate_isrc(clean_isrc):
            return [], "ISRC format invalid — used Spotify artists as fallback"

        search_data, note = _tidal_get(
            "https://api.tidal.com/v1/tracks",
            params={"isrc": clean_isrc, "countryCode": TIDAL_COUNTRY_CODE},
        )
        if note or not search_data:
            return [], note or "Tidal credits not found — used Spotify artists as fallback"

        track_items = _extract_items(search_data)
        if not track_items:
            return [], "Tidal credits not found — used Spotify artists as fallback"

        tidal_track_id = track_items[0].get("id") if isinstance(track_items[0], dict) else None
        if not tidal_track_id:
            return [], "Tidal track ID missing — used Spotify artists as fallback"

        contributors_data, note = _tidal_get(
            f"https://api.tidal.com/v1/tracks/{tidal_track_id}/contributors",
            params={"countryCode": TIDAL_COUNTRY_CODE},
        )
        if note or not contributors_data:
            return [], note or "Tidal credits not found — used Spotify artists as fallback"

        contributor_items = _extract_items(contributors_data)
        if not contributor_items:
            return [], "Tidal credits not found — used Spotify artists as fallback"

        names = []
        seen = set()
        for item in contributor_items:
            if not isinstance(item, dict):
                continue

            name = str(item.get("name") or "").strip()
            role = item.get("role")
            if not name or not _is_allowed_tidal_role(role):
                continue

            key = _lookup_key(name)
            if key in seen:
                continue
            seen.add(key)
            names.append(name)

        if not names:
            return [], "Tidal credits not found — used Spotify artists as fallback"

        return names, None

    except Exception:
        return [], "Tidal lookup failed — used Spotify artists as fallback"
