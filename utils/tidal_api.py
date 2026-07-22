"""
utils/tidal_api.py

ISRC validation and Tidal contributor-credits lookup (unofficial public
Tidal client token). Filters contributors to songwriter/producer roles only.

Resilience model (Tier 1 only, by design):
  - Token pool: multiple tokens read from Streamlit secrets / env, cycled
    round-robin. A token that gets a 401 is marked dead for this process
    and skipped going forward; we move to the next token in the pool.
  - Retry logic: exponential backoff with jitter on 429 and 5xx.
  - Structured logging: every token-death and rate-limit event is logged
    with the token's identity (masked) so you know when to rotate it
    manually in secrets.toml. Nothing here tries to auto-source new
    tokens or impersonate other clients — dead tokens stay dead until a
    human replaces them in config.
"""

import logging
import random
import re
import time

import requests

from utils.github_fetcher import _lookup_key

try:
    import streamlit as st
except ImportError:  # allows this module to be imported/tested outside Streamlit
    st = None

logger = logging.getLogger("tidal_api")
if not logger.handlers:
    # Sensible default so notes show up even if the host app hasn't configured logging.
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)

TIDAL_COUNTRY_CODE = "US"
TIDAL_REQUEST_TIMEOUT_SECONDS = 10
TIDAL_SLEEP_BETWEEN_CALLS_SECONDS = 0.1

# Retry / backoff tuning
TIDAL_MAX_ATTEMPTS_PER_TOKEN = 3       # retries for transient errors (429/5xx) per token
TIDAL_BACKOFF_BASE_SECONDS = 0.5
TIDAL_BACKOFF_MAX_SECONDS = 20.0
TIDAL_BACKOFF_JITTER_FRACTION = 0.3    # +/- 30% jitter applied to computed backoff

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
# Token pool (Tier 1: your own tokens, configured by you, cycled round-robin)
# --------------------------------------------------------------------------
class _TokenPool:
    """
    Round-robin pool over a list of tokens you configured yourself.

    - Tokens are read once from Streamlit secrets (TIDAL_TOKENS = [...]) or
      the TIDAL_TOKENS env var (comma-separated) as a fallback.
    - A token that receives a 401 is marked dead for the lifetime of this
      process (not persisted, not re-fetched from anywhere) and skipped on
      subsequent calls. This is purely "stop using a token that's already
      broken" - there is no mechanism here to acquire replacement tokens.
    - When all tokens are dead, get_next() returns None and the caller
      should fail over to the Spotify-artists fallback path.
    """

    def __init__(self):
        self._tokens = self._load_tokens()
        self._dead = set()
        self._cursor = 0

    @staticmethod
    def _mask(token):
        if not token:
            return "<empty>"
        return f"{token[:4]}...{token[-4:]}" if len(token) > 8 else "****"

    def _load_tokens(self):
        tokens = []

        if st is not None:
            try:
                secret_tokens = st.secrets.get("TIDAL_TOKENS")
                if secret_tokens:
                    tokens = list(secret_tokens)
            except Exception:
                pass

        if not tokens:
            import os

            env_tokens = os.environ.get("TIDAL_TOKENS", "")
            tokens = [t.strip() for t in env_tokens.split(",") if t.strip()]

        tokens = [t for t in tokens if t]
        if not tokens:
            logger.warning("Tidal token pool is empty — no TIDAL_TOKENS configured")
        else:
            logger.info("Tidal token pool loaded: %d token(s)", len(tokens))

        return tokens

    def get_next(self):
        """Return the next live token, or None if the whole pool is dead."""
        if not self._tokens:
            return None

        for _ in range(len(self._tokens)):
            token = self._tokens[self._cursor % len(self._tokens)]
            self._cursor += 1
            if token not in self._dead:
                return token

        return None

    def mark_dead(self, token):
        if token in self._dead:
            return
        self._dead.add(token)
        logger.error(
            "Tidal token marked DEAD after 401 (%d/%d tokens now dead) — replace it in "
            "secrets.toml TIDAL_TOKENS. token=%s",
            len(self._dead), len(self._tokens), self._mask(token),
        )

    def live_count(self):
        return len(self._tokens) - len(self._dead)

    def all_dead(self):
        return bool(self._tokens) and len(self._dead) >= len(self._tokens)


_token_pool = _TokenPool()


# --------------------------------------------------------------------------
# Role filtering (unchanged)
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


def _compute_backoff_seconds(attempt, retry_after_header=None):
    """
    Exponential backoff with jitter, honoring a server-provided Retry-After
    when present (429s) but never exceeding the configured cap.
    """
    if retry_after_header is not None:
        try:
            base_wait = max(float(retry_after_header), 0.0)
        except (TypeError, ValueError):
            base_wait = TIDAL_BACKOFF_BASE_SECONDS * (2 ** attempt)
    else:
        base_wait = TIDAL_BACKOFF_BASE_SECONDS * (2 ** attempt)

    base_wait = min(base_wait, TIDAL_BACKOFF_MAX_SECONDS)
    jitter = base_wait * TIDAL_BACKOFF_JITTER_FRACTION
    return max(0.0, base_wait + random.uniform(-jitter, jitter))


def _tidal_get(url, params=None):
    """
    Resilient Tidal GET wrapper. Never raises to the UI path.

    Behavior:
      - Picks a live token from the pool.
      - On 429 or 5xx: retries the SAME token with exponential backoff +
        jitter, up to TIDAL_MAX_ATTEMPTS_PER_TOKEN attempts, logging each
        retry.
      - On 401: logs the token as dead, advances to the next live token in
        the pool, and retries the request fresh (does not burn a
        transient-error attempt).
      - When the pool is exhausted (all tokens dead, or no tokens
        configured), returns a note telling the caller to fall back.

    Returns (json_data, note_if_failed).
    """
    if not _token_pool._tokens:
        return None, "No Tidal tokens configured — used Spotify artists as fallback"

    tokens_tried = set()

    while True:
        token = _token_pool.get_next()
        if token is None:
            logger.error("Tidal token pool exhausted (%d/%d dead)", len(_token_pool._dead), len(_token_pool._tokens))
            return None, "All Tidal tokens exhausted — used Spotify artists as fallback"

        if token in tokens_tried:
            # We've cycled back around without finding a new live token to try.
            return None, "All Tidal tokens exhausted — used Spotify artists as fallback"
        tokens_tried.add(token)

        headers = {"X-Tidal-Token": token}
        masked = _token_pool._mask(token)

        for attempt in range(TIDAL_MAX_ATTEMPTS_PER_TOKEN):
            try:
                resp = requests.get(
                    url,
                    headers=headers,
                    params=params,
                    timeout=TIDAL_REQUEST_TIMEOUT_SECONDS,
                )
            except requests.RequestException as exc:
                logger.warning(
                    "Tidal request error (token=%s, attempt=%d/%d): %s",
                    masked, attempt + 1, TIDAL_MAX_ATTEMPTS_PER_TOKEN, exc,
                )
                if attempt < TIDAL_MAX_ATTEMPTS_PER_TOKEN - 1:
                    time.sleep(_compute_backoff_seconds(attempt))
                    continue
                return None, "Tidal request failed — used Spotify artists as fallback"
            finally:
                time.sleep(TIDAL_SLEEP_BETWEEN_CALLS_SECONDS)

            if resp.status_code == 401:
                logger.error("Tidal token unauthorized (401) — token=%s", masked)
                _token_pool.mark_dead(token)
                break  # move to next token in the outer while loop

            if resp.status_code == 429:
                wait_seconds = _compute_backoff_seconds(attempt, resp.headers.get("Retry-After"))
                logger.warning(
                    "Tidal rate limited (429, token=%s, attempt=%d/%d) — backing off %.2fs",
                    masked, attempt + 1, TIDAL_MAX_ATTEMPTS_PER_TOKEN, wait_seconds,
                )
                if attempt < TIDAL_MAX_ATTEMPTS_PER_TOKEN - 1:
                    time.sleep(wait_seconds)
                    continue
                return None, "Tidal rate limit — used Spotify artists as fallback"

            if 500 <= resp.status_code < 600:
                wait_seconds = _compute_backoff_seconds(attempt)
                logger.warning(
                    "Tidal server error (%d, token=%s, attempt=%d/%d) — backing off %.2fs",
                    resp.status_code, masked, attempt + 1, TIDAL_MAX_ATTEMPTS_PER_TOKEN, wait_seconds,
                )
                if attempt < TIDAL_MAX_ATTEMPTS_PER_TOKEN - 1:
                    time.sleep(wait_seconds)
                    continue
                return None, f"Tidal HTTP {resp.status_code} — used Spotify artists as fallback"

            if not resp.ok:
                # Other 4xx: not retryable, not a token problem — fail fast.
                logger.warning("Tidal HTTP %d (non-retryable, token=%s)", resp.status_code, masked)
                return None, f"Tidal HTTP {resp.status_code} — used Spotify artists as fallback"

            try:
                return resp.json(), None
            except ValueError:
                return None, "Tidal response invalid — used Spotify artists as fallback"

        # fell through from a 401 break — loop back to outer while to grab next token
        continue


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
        logger.exception("Unexpected error in fetch_tidal_contributors_by_isrc")
        return [], "Tidal lookup failed — used Spotify artists as fallback"

# --------------------------------------------------------------------------
# NEW: full, unfiltered Tidal credits for Label Copy
# --------------------------------------------------------------------------
def fetch_tidal_credits_full_by_isrc(isrc):
    """
    Βρίσκει το πρώτο Tidal track με βάση το ISRC και επιστρέφει ΟΛΟΥΣ τους
    contributors, χωρίς φιλτράρισμα, ομαδοποιημένους ανά ΩΜΟ (raw) Tidal role.
    """
    try:
        clean_isrc = str(isrc or "").replace("-", "").strip().upper()
        if not clean_isrc:
            return {}, "Missing ISRC — Tidal credits skipped"
        if not validate_isrc(clean_isrc):
            return {}, "ISRC format invalid — Tidal credits skipped"

        search_data, note = _tidal_get(
            "https://api.tidal.com/v1/tracks",
            params={"isrc": clean_isrc, "countryCode": TIDAL_COUNTRY_CODE},
        )
        if note or not search_data:
            return {}, note or "Tidal track not found for ISRC"

        track_items = _extract_items(search_data)
        if not track_items:
            return {}, "Tidal track not found for ISRC"

        tidal_track_id = track_items[0].get("id") if isinstance(track_items[0], dict) else None
        if not tidal_track_id:
            return {}, "Tidal track ID missing"

        contributors_data, note = _tidal_get(
            f"https://api.tidal.com/v1/tracks/{tidal_track_id}/contributors",
            params={"countryCode": TIDAL_COUNTRY_CODE},
        )
        if note or not contributors_data:
            return {}, note or "Tidal contributors not found"

        contributor_items = _extract_items(contributors_data)
        if not contributor_items:
            return {}, "Tidal contributors not found"

        raw_role_credits = {}
        seen_per_role = {}
        for item in contributor_items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            role = str(item.get("role") or "").strip()
            if not name or not role:
                continue
            seen = seen_per_role.setdefault(role, set())
            key = _lookup_key(name)
            if key in seen:
                continue
            seen.add(key)
            raw_role_credits.setdefault(role, []).append(name)

        if not raw_role_credits:
            return {}, "Tidal contributors not found"

        return raw_role_credits, None

    except Exception:
        logger.exception("Unexpected error in fetch_tidal_credits_full_by_isrc")
        return {}, "Tidal full-credits lookup failed"


# --------------------------------------------------------------------------
# Optional: expose pool health so you can surface it in a Streamlit admin panel
# --------------------------------------------------------------------------
def get_token_pool_health():
    """Returns a small dict you can render in a sidebar/admin page."""
    return {
        "configured": len(_token_pool._tokens),
        "live": _token_pool.live_count(),
        "dead": len(_token_pool._dead),
        "exhausted": _token_pool.all_dead(),
    }
