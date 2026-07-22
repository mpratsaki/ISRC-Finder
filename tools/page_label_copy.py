"""
tools/page_label_copy.py

Streamlit page for the Stay Independent "Label Copy" workflow.

The page performs four distinct steps:
1. Resolve a release from a Spotify album URL, an Apple Music/iTunes album URL,
   or one of the logged-in user's Spotify playlists.
2. Build canonical LabelCopyData through the Phase 1 data layer.
3. Present release, track and credit fields in ``st.data_editor`` for explicit
   correction and confirmation.
4. Render DOCX/PDF exports, then attempt Supabase persistence without ever
   blocking the local download buttons.

Approved provider policy:
- Spotify supplies release identity and track data.
- The free iTunes Search/Lookup API supplies genre enrichment and cross-checks.
- Existing ``utils.musicbrainz_api`` wrappers supply recording/work
  relationships when available.
- TIDAL APIs and paid Apple Music/MusicKit APIs are not used.
"""

from __future__ import annotations

import copy
import difflib
import hashlib
import html
import importlib
import inspect
import re
import time
import unicodedata
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any
from utils.tidal_api import fetch_tidal_credits_full_by_isrc

import pandas as pd
import requests
import streamlit as st

from core.auth_spotify import fetch_user_playlists
from core.database import init_supabase
from utils.apple_music_api import extract_itunes_collection_id, fetch_itunes_release
from utils.docx_engine import generate_label_copy_docx, make_label_copy_filename
from utils.github_fetcher import (
    fetch_private_label_copy_template_bytes,
    get_label_copy_template_config,
)
from utils.label_copy_engine import (
    ROLE_DEFINITIONS,
    VALID_PRODUCT_TYPES,
    build_label_copy_data,
    make_musicbrainz_credit_fetcher,
    normalize_credit_map,
    normalize_isrc,
    validate_isrc,
    validate_label_copy_data,
)
from utils.pdf_engine import (
    PdfFontError,
    PdfRenderError,
    generate_label_copy_pdf,
    make_label_copy_pdf_filename,
)


SPOTIFY_API_BASE = "https://api.spotify.com/v1"
SPOTIFY_REQUEST_TIMEOUT = (5, 25)
SPOTIFY_REQUEST_RETRIES = 3
SPOTIFY_CACHE_TTL_SECONDS = 15 * 60

SPOTIFY_ALBUM_ID_RE = re.compile(r"^[A-Za-z0-9]{22}$")
SPOTIFY_ALBUM_URI_RE = re.compile(r"(?i)^spotify:album:([A-Za-z0-9]{22})$")
SPOTIFY_ALBUM_URL_RE = re.compile(
    r"(?i)^https?://open\.spotify\.com/(?:intl-[a-z]{2}/)?album/"
    r"([A-Za-z0-9]{22})(?:[/?#].*)?$"
)
TIDAL_ALBUM_URL_RE = re.compile(
    r"(?i)^https?://(?:listen\.)?tidal\.com/(?:browse/)?album/(\d+)(?:[/?#].*)?$"
)

SESSION_ACTIVE_KEY = "label_copy_active_release_key"
SESSION_STATE_PREFIX = "label_copy_release_"

SOURCE_BADGES = {
    "spotify": "🟢 Spotify",
    "itunes": "🔵 iTunes",
    "apple": "🔵 iTunes",
    "musicbrainz": "🟣 MusicBrainz",
    "derived": "🟠 Derived",
    "system": "⚙️ System",
    "static": "⚙️ Static",
    "default": "🟡 Default",
    "suggestion": "🟡 Suggestion",
    "manual": "✍️ Manual",
    "missing": "🔴 Missing",
}

RELEASE_EDITOR_FIELDS = (
    ("project_name", "Τίτλος Κυκλοφορίας"),
    ("artists", "Καλλιτέχνης/ες"),
    ("product_type", "Τύπος Προϊόντος"),
    ("upc", "UPC / EAN"),
    ("release_date", "Ημερομηνία Κυκλοφορίας"),
    ("release_date_precision", "Ακρίβεια Ημερομηνίας"),
    ("label_imprint", "Label Imprint"),
    ("company", "Εταιρεία"),
    ("publisher", "Εκδότης (Publisher)"),
    ("metadata_language", "Γλώσσα Metadata"),
    ("genre", "Είδος"),
    ("subgenre", "Υποείδος"),
    ("p_line.year", "(P) Έτος"),
    ("p_line.owner", "(P) Δικαιούχος"),
    ("c_line.year", "(C) Έτος"),
    ("c_line.owner", "(C) Δικαιούχος"),
)

CONFIRMABLE_RELEASE_FIELDS = {
    "publisher": "publisher_confirmed",
    "metadata_language": "metadata_language_confirmed",
}


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------
def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    try:
        missing = pd.isna(value)
        if not hasattr(missing, "__len__") and bool(missing):
            return ""
    except (TypeError, ValueError):
        pass
    return re.sub(r"\s+", " ", str(value).strip())


def _comparison_key(value: Any) -> str:
    text = unicodedata.normalize("NFKD", _clean_text(value)).casefold()
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _similarity(left: Any, right: Any) -> float:
    left_key = _comparison_key(left)
    right_key = _comparison_key(right)
    if not left_key or not right_key:
        return 0.0
    if left_key == right_key:
        return 1.0
    return difflib.SequenceMatcher(None, left_key, right_key).ratio()


def _as_int(value: Any, default: int | None = None) -> int | None:
    if value is None or isinstance(value, bool):
        return default
    try:
        if pd.isna(value):
            return default
    except (TypeError, ValueError):
        pass
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _unique_texts(values: Iterable[Any]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _clean_text(value)
        key = _comparison_key(text)
        if not text or not key or key in seen:
            continue
        seen.add(key)
        output.append(text)
    return output


def _split_names(value: Any) -> list[str]:
    text = _clean_text(value)
    if not text:
        return []
    return _unique_texts(
        part
        for part in re.split(r"\s*(?:,|;|\n)\s*", text)
        if _clean_text(part)
    )


def _source_badge(source: Any) -> str:
    key = _clean_text(source).lower()
    return SOURCE_BADGES.get(key, f"⚪ {key or 'unknown'}")


def _source_value(data: Mapping[str, Any], field: str) -> str:
    sources = data.get("sources")
    if isinstance(sources, Mapping):
        return _clean_text(sources.get(field)) or "missing"
    return "missing"


def _track_source_summary(track: Mapping[str, Any]) -> str:
    sources = track.get("sources")
    if not isinstance(sources, Mapping):
        return _source_badge("missing")
    ordered = []
    for field in ("title", "isrc", "genre", "credits", "lyrics_language", "audio_channel"):
        source = _clean_text(sources.get(field))
        if source:
            ordered.append(source)
    unique = _unique_texts(ordered)
    return " · ".join(_source_badge(source) for source in unique) or _source_badge("missing")


def _stable_state_key(value: Any) -> str:
    digest = hashlib.sha1(_clean_text(value).encode("utf-8")).hexdigest()[:16]
    return f"{SESSION_STATE_PREFIX}{digest}"


def _render_duration_editor(duration_ms: Any) -> str:
    milliseconds = max(_as_int(duration_ms, 0) or 0, 0)
    total_seconds = int(round(milliseconds / 1000.0))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def _parse_duration_editor(value: Any, fallback_ms: int = 0) -> int:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        parsed = _as_int(value, fallback_ms)
        return max(parsed or 0, 0)

    text = _clean_text(value)
    if not text:
        return max(fallback_ms, 0)
    if re.fullmatch(r"\d+", text):
        return max(int(text), 0)

    parts = text.split(":")
    if len(parts) not in (2, 3) or not all(part.isdigit() for part in parts):
        raise ValueError(f"Μη έγκυρη διάρκεια «{text}». Χρησιμοποιήστε M:SS ή H:MM:SS.")
    numbers = [int(part) for part in parts]
    if len(numbers) == 2:
        minutes, seconds = numbers
        hours = 0
    else:
        hours, minutes, seconds = numbers
    if seconds > 59 or (len(numbers) == 3 and minutes > 59):
        raise ValueError(f"Μη έγκυρη διάρκεια «{text}».")
    return ((hours * 3600) + (minutes * 60) + seconds) * 1000


def _is_filled(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(_clean_text(value))
    if isinstance(value, Mapping):
        return any(_is_filled(item) for item in value.values())
    if isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray)):
        return any(_is_filled(item) for item in value)
    return True


def _count_auto_filled_fields(data: Mapping[str, Any]) -> int:
    count = 0
    release_fields = (
        "project_name",
        "artists",
        "product_type",
        "upc",
        "release_date",
        "label_imprint",
        "company",
        "publisher",
        "metadata_language",
        "genre",
        "subgenre",
        "p_line",
        "c_line",
    )
    for field in release_fields:
        source = _source_value(data, field)
        if _is_filled(data.get(field)) and source not in {"manual", "missing"}:
            count += 1

    tracks = data.get("tracks")
    if isinstance(tracks, list):
        for track in tracks:
            if not isinstance(track, Mapping):
                continue
            sources = track.get("sources") if isinstance(track.get("sources"), Mapping) else {}
            for field in (
                "title",
                "duration_ms",
                "primary_artists",
                "featured_artists",
                "isrc",
                "genre",
                "subgenre",
                "lyrics_language",
                "parental_advisory",
                "publisher",
                "resource_type",
                "audio_channel",
                "p_line",
                "credits",
            ):
                source = _clean_text(sources.get(field)) or "missing"
                if _is_filled(track.get(field)) and source not in {"manual", "missing"}:
                    count += 1
    return count


def _append_unique(target: list[str], value: Any) -> None:
    text = _clean_text(value)
    if text and text not in target:
        target.append(text)


# ---------------------------------------------------------------------------
# Release input parsing
# ---------------------------------------------------------------------------
def _parse_release_input(value: Any) -> tuple[str | None, str | None, str | None]:
    text = _clean_text(value)
    if not text:
        return None, None, "Εισαγάγετε σύνδεσμο κυκλοφορίας."

    spotify_uri = SPOTIFY_ALBUM_URI_RE.fullmatch(text)
    if spotify_uri:
        return "spotify", spotify_uri.group(1), None

    spotify_url = SPOTIFY_ALBUM_URL_RE.fullmatch(text)
    if spotify_url:
        return "spotify", spotify_url.group(1), None

    if SPOTIFY_ALBUM_ID_RE.fullmatch(text):
        return "spotify", text, None

    apple_collection_id = extract_itunes_collection_id(text)
    if apple_collection_id and "music.apple.com" in text.lower():
        return "apple", apple_collection_id, None

    tidal_url = TIDAL_ALBUM_URL_RE.fullmatch(text)
    if tidal_url:
        return "tidal", tidal_url.group(1), None

    return (
        None,
        None,
        "Ο σύνδεσμος δεν αναγνωρίστηκε. Χρησιμοποιήστε Spotify album URL/URI, "
        "Apple Music album URL ή Spotify album ID 22 χαρακτήρων. Τα Spotify IDs "
        "είναι case-sensitive.",
    )


# ---------------------------------------------------------------------------
# Safe Spotify network layer
# ---------------------------------------------------------------------------
def _retry_after_seconds(value: Any, default: float = 1.0) -> float:
    try:
        return max(0.0, min(float(value), 30.0))
    except (TypeError, ValueError):
        return default


@st.cache_data(ttl=SPOTIFY_CACHE_TTL_SECONDS, show_spinner=False)
def _spotify_get_json(
    token: str,
    url: str,
    params: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Bounded, cached Spotify GET. Never raises into the page path."""
    headers = {"Authorization": f"Bearer {token}"}
    last_note = "Αποτυχία επικοινωνίας με το Spotify."

    for attempt in range(SPOTIFY_REQUEST_RETRIES):
        try:
            response = requests.get(
                url,
                headers=headers,
                params=dict(params or {}),
                timeout=SPOTIFY_REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            last_note = f"Αποτυχία δικτύου Spotify: {exc}"
            if attempt + 1 < SPOTIFY_REQUEST_RETRIES:
                time.sleep(0.5 * (2**attempt))
                continue
            return None, last_note

        if response.status_code == 429:
            last_note = "Το Spotify επέβαλε προσωρινό rate limit."
            if attempt + 1 < SPOTIFY_REQUEST_RETRIES:
                time.sleep(_retry_after_seconds(response.headers.get("Retry-After")))
                continue
            return None, last_note

        if 500 <= response.status_code < 600:
            last_note = f"Το Spotify επέστρεψε προσωρινό HTTP {response.status_code}."
            if attempt + 1 < SPOTIFY_REQUEST_RETRIES:
                time.sleep(0.5 * (2**attempt))
                continue
            return None, last_note

        if response.status_code == 401:
            return None, "Το Spotify access token έληξε ή δεν είναι έγκυρο."
        if response.status_code == 403:
            return None, "Το Spotify απέρριψε την πρόσβαση στο ζητούμενο resource."
        if response.status_code == 404:
            return None, "Δεν βρέθηκε η ζητούμενη κυκλοφορία στο Spotify."
        if not response.ok:
            return None, f"Spotify HTTP {response.status_code}."

        try:
            payload = response.json()
        except ValueError:
            return None, "Το Spotify επέστρεψε μη έγκυρη JSON απάντηση."
        if not isinstance(payload, Mapping):
            return None, "Το Spotify επέστρεψε μη αναμενόμενη δομή δεδομένων."
        return dict(payload), None

    return None, last_note


def _fetch_spotify_album_bundle(
    token: str,
    album_id: str,
    *,
    progress_callback: Callable[[int, int, str], Any] | None = None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[str]]:
    notes: list[str] = []
    album, note = _spotify_get_json(token, f"{SPOTIFY_API_BASE}/albums/{album_id}")
    if note:
        _append_unique(notes, note)
    if not album:
        return None, [], notes

    simplified_tracks: list[dict[str, Any]] = []
    url = f"{SPOTIFY_API_BASE}/albums/{album_id}/tracks"
    params: Mapping[str, Any] | None = {"limit": 50, "offset": 0}
    while url:
        page, page_note = _spotify_get_json(token, url, params)
        if page_note:
            _append_unique(notes, page_note)
        if not page:
            break
        for item in page.get("items", []):
            if isinstance(item, Mapping):
                simplified_tracks.append(dict(item))
        url = _clean_text(page.get("next"))
        params = None

    if not simplified_tracks:
        embedded = album.get("tracks")
        if isinstance(embedded, Mapping) and isinstance(embedded.get("items"), list):
            simplified_tracks = [
                dict(item) for item in embedded["items"] if isinstance(item, Mapping)
            ]

    tracks: list[dict[str, Any]] = []
    total = len(simplified_tracks)
    for index, simplified in enumerate(simplified_tracks, start=1):
        track_id = _clean_text(simplified.get("id"))
        full_track = None
        detail_note = None
        if track_id:
            full_track, detail_note = _spotify_get_json(
                token,
                f"{SPOTIFY_API_BASE}/tracks/{track_id}",
            )
        if detail_note:
            _append_unique(notes, f"{simplified.get('name') or track_id}: {detail_note}")
        track = dict(full_track or simplified)
        if not isinstance(track.get("album"), Mapping):
            track["album"] = {
                "id": album.get("id"),
                "name": album.get("name"),
                "artists": album.get("artists", []),
                "album_type": album.get("album_type"),
                "release_date": album.get("release_date"),
                "release_date_precision": album.get("release_date_precision"),
            }
        tracks.append(track)
        if progress_callback:
            try:
                progress_callback(index, max(total, 1), _clean_text(track.get("name")) or f"Track {index}")
            except Exception:
                pass

    return album, tracks, notes


def _fetch_playlist_album_id(
    token: str,
    playlist_id: str,
) -> tuple[str | None, int, list[str]]:
    notes: list[str] = []
    album_ids: list[str] = []
    item_count = 0
    url = f"{SPOTIFY_API_BASE}/playlists/{playlist_id}/items"
    params: Mapping[str, Any] | None = {
        "fields": "items(item(id,name,type,album(id,name))),next",
        "limit": 50,
        "offset": 0,
    }

    while url:
        page, note = _spotify_get_json(token, url, params)
        if note:
            _append_unique(notes, note)
        if not page:
            break
        items = page.get("items")
        if not isinstance(items, list):
            _append_unique(
                notes,
                "Δεν επιστράφηκε περιεχόμενο playlist. Η playlist πρέπει να είναι δική σας ή collaborative.",
            )
            break
        for entry in items:
            if not isinstance(entry, Mapping):
                continue
            item = entry.get("item")
            if not isinstance(item, Mapping) or _clean_text(item.get("type")) == "episode":
                continue
            album = item.get("album")
            if not isinstance(album, Mapping):
                continue
            album_id = _clean_text(album.get("id"))
            if album_id:
                album_ids.append(album_id)
                item_count += 1
        url = _clean_text(page.get("next"))
        params = None

    unique_album_ids = _unique_texts(album_ids)
    if not unique_album_ids:
        _append_unique(notes, "Η playlist δεν περιέχει αναγνωρίσιμα Spotify album tracks.")
        return None, item_count, notes
    if len(unique_album_ids) > 1:
        _append_unique(
            notes,
            f"Η playlist περιέχει tracks από {len(unique_album_ids)} διαφορετικές κυκλοφορίες.",
        )
        return None, item_count, notes
    return unique_album_ids[0], item_count, notes


def _spotify_album_search_score(
    candidate: Mapping[str, Any],
    *,
    artist: str,
    album: str,
    expected_track_count: int | None,
) -> float:
    title_score = _similarity(candidate.get("name"), album)
    candidate_artists = ", ".join(
        _clean_text(item.get("name"))
        for item in candidate.get("artists", [])
        if isinstance(item, Mapping)
    )
    artist_score = _similarity(candidate_artists, artist) if artist else 0.75
    candidate_count = _as_int(candidate.get("total_tracks"))
    if expected_track_count and candidate_count:
        count_score = max(
            0.0,
            1.0 - abs(candidate_count - expected_track_count) / max(expected_track_count, 1),
        )
    else:
        count_score = 0.5
    return (title_score * 0.62) + (artist_score * 0.30) + (count_score * 0.08)


def _search_spotify_album(
    token: str,
    *,
    artist: str,
    album: str,
    expected_track_count: int | None = None,
) -> tuple[str | None, str | None]:
    query_parts = [f'album:"{album}"']
    if _clean_text(artist):
        query_parts.append(f'artist:"{artist}"')
    payload, note = _spotify_get_json(
        token,
        f"{SPOTIFY_API_BASE}/search",
        {"q": " ".join(query_parts), "type": "album", "limit": 20},
    )
    if not payload:
        return None, note or "Δεν βρέθηκε αντίστοιχη κυκλοφορία στο Spotify."

    albums = payload.get("albums")
    items = albums.get("items") if isinstance(albums, Mapping) else None
    if not isinstance(items, list):
        return None, "Το Spotify Search δεν επέστρεψε albums."

    scored: list[tuple[float, Mapping[str, Any]]] = []
    for candidate in items:
        if not isinstance(candidate, Mapping):
            continue
        score = _spotify_album_search_score(
            candidate,
            artist=artist,
            album=album,
            expected_track_count=expected_track_count,
        )
        scored.append((score, candidate))
    if not scored:
        return None, "Δεν βρέθηκε αντίστοιχη κυκλοφορία στο Spotify."

    scored.sort(key=lambda pair: pair[0], reverse=True)
    score, candidate = scored[0]
    album_id = _clean_text(candidate.get("id"))
    if not album_id or score < 0.64:
        return None, f"Η καλύτερη Spotify αντιστοίχιση είχε χαμηλή βεβαιότητα ({score:.0%})."
    note = None
    if score < 0.85:
        note = f"Η Apple→Spotify αντιστοίχιση χρειάζεται επιβεβαίωση ({score:.0%})."
    return album_id, note


# ---------------------------------------------------------------------------
# Existing MusicBrainz wrapper adapters
# ---------------------------------------------------------------------------
def _unwrap_fetcher_result(result: Any) -> tuple[Any, str | None]:
    if isinstance(result, tuple) and len(result) == 2:
        return result[0], _clean_text(result[1]) or None
    return result, None


def _filtered_kwargs(func: Callable[..., Any], kwargs: Mapping[str, Any]) -> dict[str, Any]:
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return dict(kwargs)
    if any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    ):
        return dict(kwargs)
    return {key: value for key, value in kwargs.items() if key in signature.parameters}


def _call_variants(
    func: Callable[..., Any],
    variants: Sequence[tuple[tuple[Any, ...], Mapping[str, Any]]],
) -> tuple[Any, str | None]:
    type_errors: list[str] = []
    for args, kwargs in variants:
        try:
            result = func(*args, **_filtered_kwargs(func, kwargs))
            data, note = _unwrap_fetcher_result(result)
            if data is not None:
                return data, note
            if note:
                return None, note
        except TypeError as exc:
            type_errors.append(str(exc))
            continue
        except Exception as exc:
            return None, str(exc)
    return None, type_errors[-1] if type_errors else "Δεν επέστρεψε δεδομένα ο MusicBrainz wrapper."


def _first_callable(module: Any, names: Sequence[str]) -> tuple[Callable[..., Any] | None, str]:
    for name in names:
        candidate = getattr(module, name, None)
        if callable(candidate):
            return candidate, name
    return None, ""


def _musicbrainz_adapters() -> tuple[
    Callable[..., Any] | None,
    Callable[..., Any] | None,
    list[str],
]:
    notes: list[str] = []
    try:
        module = importlib.import_module("utils.musicbrainz_api")
    except Exception as exc:
        return None, None, [f"MusicBrainz wrappers unavailable: {exc}"]

    recording_search, recording_search_name = _first_callable(
        module,
        (
            "mb_search_recordings_by_isrc",
            "mb_find_recordings_by_isrc",
            "mb_search_recording_by_isrc",
            "mb_search_recordings",
            "mb_search_recording",
        ),
    )
    get_recording, get_recording_name = _first_callable(
        module,
        ("mb_get_recording", "get_recording"),
    )
    get_work, get_work_name = _first_callable(
        module,
        ("mb_get_work", "get_work"),
    )

    credit_fetcher: Callable[..., Any] | None = None
    if recording_search and get_recording:

        def find_recordings_by_isrc(isrc: str, **_: Any) -> tuple[Any, str | None]:
            query = f'isrc:"{isrc}"'
            if "isrc" in recording_search_name:
                variants = (
                    ((isrc,), {}),
                    ((), {"isrc": isrc}),
                    ((query,), {"limit": 10}),
                )
            else:
                variants = (
                    ((query,), {"limit": 10}),
                    ((), {"query": query, "limit": 10}),
                    ((), {"isrc": isrc, "limit": 10}),
                )
            return _call_variants(recording_search, variants)

        def fetch_recording(
            recording_id: str,
            *,
            includes: Sequence[str] | None = None,
            **_: Any,
        ) -> tuple[Any, str | None]:
            include_list = list(includes or ["artist-rels", "work-rels"])
            return _call_variants(
                get_recording,
                (
                    ((recording_id,), {"includes": include_list}),
                    ((), {"recording_id": recording_id, "includes": include_list}),
                    ((), {"mbid": recording_id, "includes": include_list}),
                    ((recording_id,), {"inc": include_list}),
                ),
            )

        def fetch_work(
            work_id: str,
            *,
            includes: Sequence[str] | None = None,
            **_: Any,
        ) -> tuple[Any, str | None]:
            if not get_work:
                return None, "Δεν υπάρχει mb_get_work wrapper."
            include_list = list(includes or ["artist-rels"])
            return _call_variants(
                get_work,
                (
                    ((work_id,), {"includes": include_list}),
                    ((), {"work_id": work_id, "includes": include_list}),
                    ((), {"mbid": work_id, "includes": include_list}),
                    ((work_id,), {"inc": include_list}),
                ),
            )

        credit_fetcher = make_musicbrainz_credit_fetcher(
            find_recordings_by_isrc=find_recordings_by_isrc,
            get_recording=fetch_recording,
            get_work=fetch_work if get_work else None,
        )
        notes.append(
            "MusicBrainz credits: "
            f"{recording_search_name} → {get_recording_name}"
            + (f" → {get_work_name}" if get_work_name else "")
        )
    else:
        notes.append(
            "Δεν βρέθηκαν συμβατοί MusicBrainz recording/work wrappers· τα credits θα μείνουν για manual entry."
        )

    release_barcode_search, release_barcode_name = _first_callable(
        module,
        (
            "mb_search_releases_by_barcode",
            "mb_find_releases_by_barcode",
            "mb_search_release_by_barcode",
        ),
    )
    generic_release_search, generic_release_name = _first_callable(
        module,
        ("mb_search_releases", "mb_search_release"),
    )

    release_fetcher: Callable[..., Any] | None = None
    if release_barcode_search or generic_release_search:

        def fetch_release(
            *,
            upc: str = "",
            barcode: str = "",
            title: str = "",
            artist: str = "",
            track_count: int | None = None,
            **_: Any,
        ) -> tuple[Any, str | None]:
            clean_barcode = re.sub(r"\D+", "", _clean_text(upc or barcode))
            if clean_barcode and release_barcode_search:
                data, note = _call_variants(
                    release_barcode_search,
                    (
                        ((clean_barcode,), {}),
                        ((), {"barcode": clean_barcode}),
                        ((), {"upc": clean_barcode}),
                    ),
                )
                if data is not None:
                    return data, note

            if generic_release_search:
                query_parts = []
                if clean_barcode:
                    query_parts.append(f"barcode:{clean_barcode}")
                if title:
                    query_parts.append(f'release:"{title}"')
                if artist:
                    query_parts.append(f'artist:"{artist}"')
                query = " AND ".join(query_parts) or title
                return _call_variants(
                    generic_release_search,
                    (
                        ((query,), {"limit": 20}),
                        ((), {"query": query, "limit": 20}),
                        ((), {"barcode": clean_barcode, "release": title, "artist": artist}),
                    ),
                )
            return None, "Δεν βρέθηκε MusicBrainz release wrapper."

        release_fetcher = fetch_release
        used = release_barcode_name or generic_release_name
        notes.append(f"MusicBrainz release fallback: {used}")

    return credit_fetcher, release_fetcher, notes


# ---------------------------------------------------------------------------
# Canonical resolution orchestration
# ---------------------------------------------------------------------------
def _activity_html(current: int, total: int, title: str, detail: str = "") -> str:
    safe_title = html.escape(_clean_text(title))
    safe_detail = html.escape(_clean_text(detail))
    counter = f"Επεξεργασία {current} από {total}" if total else "Επεξεργασία"
    detail_html = f"<br><span style='color:#aaa'>{safe_detail}</span>" if safe_detail else ""
    return f"""
    <div class="live-activity-box">
        <span style="color:#aaa; font-size:14px;">{counter}</span><br>
        <strong style="font-size:18px;">🎵 {safe_title}</strong>{detail_html}
    </div>
    """


def _resolve_release(
    token: str,
    selection: dict[str, Any],
    *,
    progress_bar: Any,
    live_status: Any,
) -> tuple[dict[str, Any], list[str]]:
    logs: list[str] = []
    provider = _clean_text(selection.get("provider"))
    release_id = _clean_text(selection.get("release_id"))

    live_status.markdown(
        _activity_html(1, 5, "Ανάκτηση κυκλοφορίας", provider.upper()),
        unsafe_allow_html=True,
    )
    progress_bar.progress(0.08)

    explicit_itunes_release = None
    if provider == "apple":
        explicit_itunes_release, apple_note = fetch_itunes_release(
            collection_id=release_id,
            country="GR",
        )
        if apple_note:
            _append_unique(logs, f"iTunes: {apple_note}")
        if not explicit_itunes_release:
            raise RuntimeError("Δεν ήταν δυνατή η ανάκτηση της Apple Music/iTunes κυκλοφορίας.")

        artist = _clean_text(explicit_itunes_release.get("artist_name"))
        album = _clean_text(explicit_itunes_release.get("collection_name"))
        expected_count = _as_int(explicit_itunes_release.get("track_count"))
        spotify_album_id, match_note = _search_spotify_album(
            token,
            artist=artist,
            album=album,
            expected_track_count=expected_count,
        )
        if match_note:
            _append_unique(logs, match_note)
        if not spotify_album_id:
            raise RuntimeError(
                "Η Apple κυκλοφορία βρέθηκε, αλλά δεν αντιστοιχίστηκε με επαρκή βεβαιότητα στο Spotify."
            )
        release_id = spotify_album_id

    elif provider == "playlist":
        playlist_album_id, playlist_item_count, playlist_notes = _fetch_playlist_album_id(
            token,
            release_id,
        )
        for note in playlist_notes:
            _append_unique(logs, note)
        if not playlist_album_id:
            raise RuntimeError(
                "Η επιλεγμένη playlist δεν αντιστοιχεί σε μία μοναδική Spotify κυκλοφορία."
            )
        release_id = playlist_album_id
        selection["playlist_item_count"] = playlist_item_count

    elif provider == "tidal":
        raise RuntimeError(
            "Ο σύνδεσμος TIDAL αναγνωρίστηκε, αλλά η αυτόματη TIDAL πρόσβαση είναι απενεργοποιημένη "
            "βάσει της εγκεκριμένης πολιτικής API. Χρησιμοποιήστε το αντίστοιχο Spotify ή Apple Music URL."
        )

    if provider not in {"spotify", "apple", "playlist"}:
        raise RuntimeError("Μη υποστηριζόμενος provider κυκλοφορίας.")

    live_status.markdown(
        _activity_html(2, 5, "Spotify release & tracklist", release_id),
        unsafe_allow_html=True,
    )
    progress_bar.progress(0.18)

    def spotify_track_progress(current: int, total: int, title: str) -> None:
        progress = 0.18 + (0.27 * (current / max(total, 1)))
        progress_bar.progress(min(progress, 0.45))
        live_status.markdown(
            _activity_html(current, total, title, "Ανάκτηση Spotify ISRC και πλήρων track fields"),
            unsafe_allow_html=True,
        )

    spotify_release, spotify_tracks, spotify_notes = _fetch_spotify_album_bundle(
        token,
        release_id,
        progress_callback=spotify_track_progress,
    )
    for note in spotify_notes:
        _append_unique(logs, f"Spotify: {note}")
    if not spotify_release or not spotify_tracks:
        raise RuntimeError("Δεν ήταν δυνατή η ανάκτηση της Spotify κυκλοφορίας και των tracks της.")

    if provider == "playlist":
        playlist_count = _as_int(selection.get("playlist_item_count"), 0) or 0
        album_count = len(spotify_tracks)
        if playlist_count and playlist_count != album_count:
            _append_unique(
                logs,
                f"Η playlist είχε {playlist_count} tracks· το Label Copy χρησιμοποιεί ολόκληρη την κυκλοφορία ({album_count} tracks).",
            )

    live_status.markdown(
        _activity_html(3, 5, "iTunes genre enrichment", "Free iTunes Search/Lookup API"),
        unsafe_allow_html=True,
    )
    progress_bar.progress(0.50)

    credit_fetcher, release_fetcher, mb_notes = _musicbrainz_adapters()
    for note in mb_notes:
        _append_unique(logs, note)

    live_status.markdown(
        _activity_html(4, 5, "MusicBrainz credits", "artist-rels & work-rels"),
        unsafe_allow_html=True,
    )
    progress_bar.progress(0.56)

    def build_progress(current: int, total: int, title: str) -> None:
        progress = 0.56 + (0.40 * (current / max(total, 1)))
        progress_bar.progress(min(progress, 0.96))
        live_status.markdown(
            _activity_html(current, total, title, "Σύνθεση canonical LabelCopyData"),
            unsafe_allow_html=True,
        )

# Check if Tidal is enabled in Secrets
    use_tidal = st.secrets.get("ENABLE_TIDAL_FULL_CREDITS", False)
    
    data = build_label_copy_data(
        spotify_release,
        spotify_tracks,
        itunes_release=explicit_itunes_release,
        itunes_fetcher=None if explicit_itunes_release is not None else fetch_itunes_release,
        musicbrainz_release_fetcher=release_fetcher,
        musicbrainz_credits_fetcher=credit_fetcher,
        tidal_credits_fetcher=fetch_tidal_credits_full_by_isrc if use_tidal else None, # <--- ΠΡΟΣΤΕΘΗΚΕ
        progress_callback=build_progress,
        ensure_single_release=True,
    )

    provider_matches = data.setdefault("provider_matches", {})
    provider_matches["input_provider"] = provider
    provider_matches["resolved_spotify_release_id"] = release_id
    if provider == "apple":
        provider_matches["input_apple_collection_id"] = _clean_text(selection.get("release_id"))
    if provider == "playlist":
        provider_matches["input_spotify_playlist_id"] = _clean_text(selection.get("release_id"))

    progress_bar.progress(1.0)
    live_status.markdown(
        _activity_html(5, 5, "Η επίλυση ολοκληρώθηκε", data.get("project_name")),
        unsafe_allow_html=True,
    )
    return data, logs


# ---------------------------------------------------------------------------
# Review editor serialization
# ---------------------------------------------------------------------------
def _nested_value(data: Mapping[str, Any], field_key: str) -> Any:
    if "." not in field_key:
        value = data.get(field_key)
        if field_key == "artists":
            return ", ".join(_unique_texts(value if isinstance(value, list) else []))
        return value
    root, child = field_key.split(".", 1)
    nested = data.get(root)
    return nested.get(child) if isinstance(nested, Mapping) else ""


def _release_editor_rows(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for field_key, label in RELEASE_EDITOR_FIELDS:
        root = field_key.split(".", 1)[0]
        source = _source_value(data, root)
        confirmation_key = CONFIRMABLE_RELEASE_FIELDS.get(root)
        confirmed = bool(data.get(confirmation_key)) if confirmation_key else True
        value = _nested_value(data, field_key)
        if value is None:
            value = ""
        elif not isinstance(value, str):
            value = str(value)
        rows.append(
            {
                "field_key": field_key,
                "Πεδίο": label,
                "Τιμή": value,
                "Πηγή": _source_badge(source),
                "Επιβεβαιωμένο": confirmed,
            }
        )
    return rows


def _track_editor_rows(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    tracks = data.get("tracks")
    if not isinstance(tracks, list):
        return rows
    for index, track in enumerate(tracks, start=1):
        if not isinstance(track, Mapping):
            continue
        p_line = track.get("p_line") if isinstance(track.get("p_line"), Mapping) else {}
        rows.append(
            {
                "track_index": index - 1,
                "Α/Α": index,
                "Disc": _as_int(track.get("disc_number"), 1) or 1,
                "Track": _as_int(track.get("track_number"), index) or index,
                "Τίτλος": _clean_text(track.get("title")),
                "Διάρκεια": _render_duration_editor(track.get("duration_ms")),
                "Primary Artist(s)": ", ".join(_unique_texts(track.get("primary_artists", []))),
                "Featured Artist(s)": ", ".join(_unique_texts(track.get("featured_artists", []))),
                "ISRC": _clean_text(track.get("isrc")),
                "Genre": _clean_text(track.get("genre")),
                "Subgenre": _clean_text(track.get("subgenre")),
                "Lyrics Language": _clean_text(track.get("lyrics_language")),
                "Επιβεβ. Lyrics": bool(track.get("lyrics_language_confirmed")),
                "Parental Advisory": _clean_text(track.get("parental_advisory")),
                "Επιβεβ. Advisory": bool(track.get("parental_advisory_confirmed")),
                "Publisher": _clean_text(track.get("publisher")),
                "Resource Type": _clean_text(track.get("resource_type")) or "Audio",
                "Audio Channel": _clean_text(track.get("audio_channel")) or "Stereo",
                "Επιβεβ. Channel": bool(track.get("audio_channel_confirmed")),
                "(P) Έτος": _as_int(p_line.get("year")),
                "(P) Owner": _clean_text(p_line.get("owner")),
                "Πηγή": _track_source_summary(track),
            }
        )
    return rows


def _role_editor_label(role_id: str, labels: Mapping[str, Any]) -> str:
    if role_id in ROLE_DEFINITIONS:
        definition = ROLE_DEFINITIONS[role_id]
        return _clean_text(definition.get("pdf_label")) or _clean_text(
            definition.get("display_label")
        )
    return _clean_text(labels.get(role_id)) or role_id.removeprefix("other:").replace(
        "_", " "
    ).title()


def _credit_editor_rows(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    tracks = data.get("tracks")
    if not isinstance(tracks, list):
        return rows
    for index, track in enumerate(tracks, start=1):
        if not isinstance(track, Mapping):
            continue
        title = _clean_text(track.get("title")) or f"Track {index}"
        raw_credits = track.get("credits") if isinstance(track.get("credits"), Mapping) else {}
        labels = track.get("credit_labels") if isinstance(track.get("credit_labels"), Mapping) else {}
        if raw_credits:
            for role_id, names in raw_credits.items():
                role_label = _role_editor_label(_clean_text(role_id), labels)
                rows.append(
                    {
                        "track_index": index - 1,
                        "role_id": _clean_text(role_id),
                        "original_role_label": role_label,
                        "Α/Α": index,
                        "Τίτλος": title,
                        "Ρόλος": role_label,
                        "Ονόματα": ", ".join(_unique_texts(names if isinstance(names, list) else [names])),
                        "Πηγή": _source_badge(
                            _clean_text((track.get("sources") or {}).get("credits"))
                            if isinstance(track.get("sources"), Mapping)
                            else "missing"
                        ),
                    }
                )
        else:
            rows.append(
                {
                    "track_index": index - 1,
                    "role_id": "",
                    "original_role_label": "",
                    "Α/Α": index,
                    "Τίτλος": title,
                    "Ρόλος": "",
                    "Ονόματα": "",
                    "Πηγή": _source_badge("missing"),
                }
            )
    return rows


def _mark_manual_source(entity: dict[str, Any], field: str) -> None:
    sources = entity.setdefault("sources", {})
    if isinstance(sources, dict):
        sources[field] = "manual"
    provenance = entity.setdefault("provenance", {})
    if isinstance(provenance, dict):
        provenance[field] = {
            "source": "manual",
            "confidence": "high",
            "confirmed": True,
            "detail": "Επεξεργασία χρήστη στο Label Copy review editor.",
        }


def _apply_release_edits(
    data: dict[str, Any],
    rows: Sequence[Mapping[str, Any]],
    original: Mapping[str, Any],
) -> None:
    for row in rows:
        field_key = _clean_text(row.get("field_key"))
        if not field_key:
            continue
        raw_value = row.get("Τιμή")
        if field_key == "artists":
            value: Any = _split_names(raw_value)
        elif field_key.endswith(".year"):
            value = _as_int(raw_value)
        else:
            value = _clean_text(raw_value)

        if field_key == "product_type" and value not in VALID_PRODUCT_TYPES:
            raise ValueError(
                "Το Product Type πρέπει να είναι Album, Single, EP ή Compilation."
            )
        if field_key == "release_date_precision" and value not in {
            "day",
            "month",
            "year",
            "unknown",
        }:
            raise ValueError(
                "Το Release Date Precision πρέπει να είναι day, month, year ή unknown."
            )

        if "." in field_key:
            root, child = field_key.split(".", 1)
            nested = data.setdefault(root, {})
            if not isinstance(nested, dict):
                nested = {}
                data[root] = nested
            old_value = _nested_value(original, field_key)
            nested[child] = value
            if value != old_value:
                _mark_manual_source(data, root)
        else:
            old_value = original.get(field_key)
            data[field_key] = value
            if value != old_value:
                _mark_manual_source(data, field_key)

        root_field = field_key.split(".", 1)[0]
        confirmation_key = CONFIRMABLE_RELEASE_FIELDS.get(root_field)
        if confirmation_key:
            data[confirmation_key] = bool(row.get("Επιβεβαιωμένο"))
            provenance = data.setdefault("provenance", {})
            if isinstance(provenance, dict) and isinstance(provenance.get(root_field), dict):
                provenance[root_field]["confirmed"] = bool(row.get("Επιβεβαιωμένο"))


def _apply_track_edits(
    data: dict[str, Any],
    rows: Sequence[Mapping[str, Any]],
    original: Mapping[str, Any],
) -> None:
    tracks = data.get("tracks")
    original_tracks = original.get("tracks")
    if not isinstance(tracks, list) or not isinstance(original_tracks, list):
        return

    for row in rows:
        index = _as_int(row.get("track_index"))
        if index is None or index < 0 or index >= len(tracks):
            continue
        track = tracks[index]
        original_track = original_tracks[index]
        if not isinstance(track, dict) or not isinstance(original_track, Mapping):
            continue

        field_values: dict[str, Any] = {
            "number": _as_int(row.get("Α/Α"), index + 1) or index + 1,
            "disc_number": _as_int(row.get("Disc"), 1) or 1,
            "track_number": _as_int(row.get("Track"), index + 1) or index + 1,
            "title": _clean_text(row.get("Τίτλος")),
            "duration_ms": _parse_duration_editor(
                row.get("Διάρκεια"),
                fallback_ms=_as_int(track.get("duration_ms"), 0) or 0,
            ),
            "primary_artists": _split_names(row.get("Primary Artist(s)")),
            "featured_artists": _split_names(row.get("Featured Artist(s)")),
            "isrc": normalize_isrc(row.get("ISRC")),
            "genre": _clean_text(row.get("Genre")),
            "subgenre": _clean_text(row.get("Subgenre")),
            "lyrics_language": _clean_text(row.get("Lyrics Language")) or None,
            "parental_advisory": _clean_text(row.get("Parental Advisory")) or None,
            "publisher": _clean_text(row.get("Publisher")),
            "resource_type": _clean_text(row.get("Resource Type")) or "Audio",
            "audio_channel": _clean_text(row.get("Audio Channel")) or "Stereo",
        }
        for field, value in field_values.items():
            if value != original_track.get(field):
                _mark_manual_source(track, field)
            track[field] = value

        track["lyrics_language_confirmed"] = bool(row.get("Επιβεβ. Lyrics"))
        track["parental_advisory_confirmed"] = bool(row.get("Επιβεβ. Advisory"))
        track["audio_channel_confirmed"] = bool(row.get("Επιβεβ. Channel"))

        p_line = track.setdefault("p_line", {})
        if not isinstance(p_line, dict):
            p_line = {}
            track["p_line"] = p_line
        p_year = _as_int(row.get("(P) Έτος"))
        p_owner = _clean_text(row.get("(P) Owner"))
        original_p = original_track.get("p_line") if isinstance(original_track.get("p_line"), Mapping) else {}
        p_line["year"] = p_year
        p_line["owner"] = p_owner
        p_line["confirmed"] = True
        if p_year != original_p.get("year") or p_owner != _clean_text(original_p.get("owner")):
            _mark_manual_source(track, "p_line")


def _apply_credit_edits(
    data: dict[str, Any],
    rows: Sequence[Mapping[str, Any]],
    original: Mapping[str, Any],
) -> None:
    tracks = data.get("tracks")
    original_tracks = original.get("tracks")
    if not isinstance(tracks, list) or not isinstance(original_tracks, list):
        return

    grouped: dict[int, list[Mapping[str, Any]]] = {}
    for row in rows:
        index = _as_int(row.get("track_index"))
        if index is None:
            display_number = _as_int(row.get("Α/Α"))
            index = (display_number - 1) if display_number else None
        if index is None or index < 0 or index >= len(tracks):
            continue
        grouped.setdefault(index, []).append(row)

    for index, track in enumerate(tracks):
        if not isinstance(track, dict):
            continue
        merged_credits: dict[str, list[str]] = OrderedDict()
        merged_labels: dict[str, str] = {}

        for row in grouped.get(index, []):
            role_label = _clean_text(row.get("Ρόλος"))
            names = _split_names(row.get("Ονόματα"))
            if not role_label or not names:
                continue
            stored_role_id = _clean_text(row.get("role_id"))
            original_label = _clean_text(row.get("original_role_label"))
            role_token = stored_role_id if stored_role_id and role_label == original_label else role_label
            normalized, labels = normalize_credit_map({role_token: names})
            for role_id, role_names in normalized.items():
                merged_credits[role_id] = _unique_texts(
                    [*merged_credits.get(role_id, []), *role_names]
                )
                merged_labels.setdefault(role_id, labels.get(role_id, role_label))

        original_track = original_tracks[index] if index < len(original_tracks) else {}
        original_credits = (
            original_track.get("credits")
            if isinstance(original_track, Mapping) and isinstance(original_track.get("credits"), Mapping)
            else {}
        )
        track["credits"] = dict(merged_credits)
        track["credit_labels"] = merged_labels
        if dict(merged_credits) != dict(original_credits):
            _mark_manual_source(track, "credits")


def _apply_review_edits(
    original_data: Mapping[str, Any],
    release_rows: Sequence[Mapping[str, Any]],
    track_rows: Sequence[Mapping[str, Any]],
    credit_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    reviewed = copy.deepcopy(dict(original_data))
    _apply_release_edits(reviewed, release_rows, original_data)
    _apply_track_edits(reviewed, track_rows, original_data)
    _apply_credit_edits(reviewed, credit_rows, original_data)

    tracks = reviewed.get("tracks")
    if isinstance(tracks, list):
        reviewed["total_duration_ms"] = sum(
            max(_as_int(track.get("duration_ms"), 0) or 0, 0)
            for track in tracks
            if isinstance(track, Mapping)
        )

    reviewed["warnings"] = validate_label_copy_data(reviewed)
    return reviewed


# ---------------------------------------------------------------------------
# Supabase persistence
# ---------------------------------------------------------------------------
def _upload_exports_to_supabase(
    *,
    spotify_user: str,
    project_name: str,
    track_count: int,
    docx_bytes: bytes,
    docx_filename: str,
    pdf_bytes: bytes,
    pdf_filename: str,
) -> tuple[list[str], str | None]:
    """Uploads both files and inserts one export_history row per file."""
    supabase = init_supabase()
    if not supabase or not _clean_text(spotify_user):
        return [], "Το Supabase δεν είναι διαθέσιμο· τα downloads παραμένουν ενεργά."

    timestamp = int(time.time())
    urls: list[str] = []
    files = (
        (
            docx_filename,
            docx_bytes,
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
        (pdf_filename, pdf_bytes, "application/pdf"),
    )

    for filename, payload, content_type in files:
        storage_path = f"{spotify_user}/{timestamp}_{filename}"
        supabase.storage.from_("labelcopies").upload(
            file=payload,
            path=storage_path,
            file_options={"content-type": content_type},
        )
        public_url = supabase.storage.from_("labelcopies").get_public_url(storage_path)
        urls.append(public_url)
        supabase.table("export_history").insert(
            {
                "spotify_user": spotify_user,
                "playlist_name": project_name,
                "track_count": track_count,
                "file_url": public_url,
            }
        ).execute()

    return urls, None


# ---------------------------------------------------------------------------
# UI rendering
# ---------------------------------------------------------------------------
def _render_release_editor(
    state: dict[str, Any],
    widget_suffix: str,
) -> pd.DataFrame:
    seed = state.get("release_editor_rows") or _release_editor_rows(state["data"])
    edited = st.data_editor(
        pd.DataFrame(seed),
        key=f"label_copy_release_editor_{widget_suffix}",
        hide_index=True,
        width="stretch",
        disabled=["Πεδίο", "Πηγή"],
        column_config={
            "field_key": None,
            "Πεδίο": st.column_config.TextColumn("Πεδίο", width="medium"),
            "Τιμή": st.column_config.TextColumn("Τιμή", width="large"),
            "Πηγή": st.column_config.TextColumn("Πηγή", width="medium"),
            "Επιβεβαιωμένο": st.column_config.CheckboxColumn("Επιβεβαιωμένο"),
        },
    )
    state["release_editor_rows"] = edited.to_dict("records")
    return edited


def _render_track_editor(
    state: dict[str, Any],
    widget_suffix: str,
) -> pd.DataFrame:
    seed = state.get("track_editor_rows") or _track_editor_rows(state["data"])
    edited = st.data_editor(
        pd.DataFrame(seed),
        key=f"label_copy_track_editor_{widget_suffix}",
        hide_index=True,
        width="stretch",
        disabled=["Πηγή"],
        column_config={
            "track_index": None,
            "Α/Α": st.column_config.NumberColumn("Α/Α", min_value=1, step=1, width="small"),
            "Disc": st.column_config.NumberColumn("Disc", min_value=1, step=1, width="small"),
            "Track": st.column_config.NumberColumn("Track", min_value=1, step=1, width="small"),
            "Τίτλος": st.column_config.TextColumn("Τίτλος", width="large"),
            "Διάρκεια": st.column_config.TextColumn("Διάρκεια", help="M:SS ή H:MM:SS"),
            "Parental Advisory": st.column_config.SelectboxColumn(
                "Parental Advisory",
                options=["", "Explicit", "Clean", "Non-Applicable", "Unknown"],
            ),
            "Επιβεβ. Lyrics": st.column_config.CheckboxColumn("Επιβεβ. Lyrics"),
            "Επιβεβ. Advisory": st.column_config.CheckboxColumn("Επιβεβ. Advisory"),
            "Επιβεβ. Channel": st.column_config.CheckboxColumn("Επιβεβ. Channel"),
            "(P) Έτος": st.column_config.NumberColumn("(P) Έτος", min_value=1900, max_value=2200),
            "Πηγή": st.column_config.TextColumn("Πηγή", width="large"),
        },
    )
    state["track_editor_rows"] = edited.to_dict("records")
    return edited


def _render_credit_editor(
    state: dict[str, Any],
    widget_suffix: str,
) -> pd.DataFrame:
    seed = state.get("credit_editor_rows") or _credit_editor_rows(state["data"])
    edited = st.data_editor(
        pd.DataFrame(seed),
        key=f"label_copy_credit_editor_{widget_suffix}",
        hide_index=True,
        width="stretch",
        num_rows="dynamic",
        disabled=["Τίτλος", "Πηγή"],
        column_config={
            "track_index": None,
            "role_id": None,
            "original_role_label": None,
            "Α/Α": st.column_config.NumberColumn("Α/Α", min_value=1, step=1, width="small"),
            "Τίτλος": st.column_config.TextColumn("Τίτλος", width="large"),
            "Ρόλος": st.column_config.TextColumn(
                "Ρόλος",
                help="π.χ. Written by, Music by, Arranged by, Drums, Recitation",
                width="medium",
            ),
            "Ονόματα": st.column_config.TextColumn(
                "Ονόματα",
                help="Πολλαπλά ονόματα χωρισμένα με κόμμα",
                width="large",
            ),
            "Πηγή": st.column_config.TextColumn("Πηγή", width="medium"),
        },
    )
    state["credit_editor_rows"] = edited.to_dict("records")
    return edited


def _render_preview(data: Mapping[str, Any]) -> None:
    release_rows = [
        {"Πεδίο": "Τίτλος Κυκλοφορίας", "Τιμή": data.get("project_name")},
        {"Πεδίο": "Καλλιτέχνης/ες", "Τιμή": ", ".join(_unique_texts(data.get("artists", [])))},
        {"Πεδίο": "Τύπος Προϊόντος", "Τιμή": data.get("product_type")},
        {"Πεδίο": "UPC", "Τιμή": data.get("upc")},
        {"Πεδίο": "Ημερομηνία Κυκλοφορίας", "Τιμή": data.get("release_date")},
        {"Πεδίο": "Label Imprint", "Τιμή": data.get("label_imprint")},
        {"Πεδίο": "Εκδότης (Publisher)", "Τιμή": data.get("publisher")},
        {"Πεδίο": "Γλώσσα Metadata", "Τιμή": data.get("metadata_language")},
        {"Πεδίο": "Είδος / Υποείδος", "Τιμή": " / ".join(
            part for part in (_clean_text(data.get("genre")), _clean_text(data.get("subgenre"))) if part
        )},
    ]
    st.dataframe(pd.DataFrame(release_rows), hide_index=True, width="stretch")
    st.caption(
        "Στο PDF οι Featured Artists συγχωνεύονται στη γραμμή Primary Artist(s). "
        "Στο DOCX παραμένουν σε ξεχωριστή γραμμή."
    )

    tracks = data.get("tracks")
    if not isinstance(tracks, list):
        return
    for index, track in enumerate(tracks, start=1):
        if not isinstance(track, Mapping):
            continue
        title = _clean_text(track.get("title")) or f"Track {index}"
        with st.expander(f"{index}. {title}", expanded=False):
            metadata = {
                "Disc / Track": f"{track.get('disc_number', 1)} / {track.get('track_number', index)}",
                "Duration": _render_duration_editor(track.get("duration_ms")),
                "ISRC": track.get("isrc") or "—",
                "Primary Artist(s)": ", ".join(_unique_texts(track.get("primary_artists", []))) or "—",
                "Featured Artist(s)": ", ".join(_unique_texts(track.get("featured_artists", []))) or "—",
                "Genre": " / ".join(
                    part for part in (_clean_text(track.get("genre")), _clean_text(track.get("subgenre"))) if part
                ) or "—",
                "Lyrics Language": track.get("lyrics_language") or "—",
                "Parental Advisory": track.get("parental_advisory") or "—",
                "Publisher": track.get("publisher") or "—",
                "Audio Channel": track.get("audio_channel") or "—",
            }
            st.json(metadata)

            credits = track.get("credits") if isinstance(track.get("credits"), Mapping) else {}
            labels = track.get("credit_labels") if isinstance(track.get("credit_labels"), Mapping) else {}
            if credits:
                credit_rows = []
                for role_id, names in credits.items():
                    credit_rows.append(
                        {
                            "Ρόλος": _role_editor_label(_clean_text(role_id), labels),
                            "Ονόματα": ", ".join(_unique_texts(names if isinstance(names, list) else [names])),
                        }
                    )
                st.dataframe(pd.DataFrame(credit_rows), hide_index=True, width="stretch")
            else:
                st.warning("Δεν υπάρχουν επιβεβαιωμένα credits για αυτό το track.")


def _render_logs(state: Mapping[str, Any], data: Mapping[str, Any]) -> None:
    current_warnings = validate_label_copy_data(data)
    resolution_logs = _unique_texts(state.get("logs", []))

    if current_warnings:
        st.warning(f"Τρέχουσες προειδοποιήσεις: {len(current_warnings)}")
        for warning in current_warnings:
            st.write(f"• {warning}")
    else:
        st.success("Όλα τα απαιτούμενα πεδία έχουν συμπληρωθεί ή επιβεβαιωθεί.")

    st.divider()
    missing_isrcs = []
    for track in data.get("tracks", []):
        if isinstance(track, Mapping) and not validate_isrc(track.get("isrc")):
            missing_isrcs.append(_clean_text(track.get("title")) or "Άγνωστο track")
    if missing_isrcs:
        st.error(f"Μη έγκυρα ή ελλιπή ISRC: {len(missing_isrcs)}")
        for title in missing_isrcs:
            st.write(f"• **{title}**")
    else:
        st.success("Όλα τα ISRC έχουν έγκυρη μορφή.")

    st.divider()
    st.info(
        "TIDAL credits δεν χρησιμοποιούνται. Τα αυτόματα credits προέρχονται μόνο από "
        "MusicBrainz artist-rels/work-rels και απαιτούν έλεγχο χρήστη."
    )
    if resolution_logs:
        with st.expander("API και logs αντιστοίχισης", expanded=False):
            for note in resolution_logs:
                st.write(f"• {note}")
    else:
        st.caption("Δεν καταγράφηκαν πρόσθετα API logs.")

    persistence_note = _clean_text(state.get("persistence_note"))
    if persistence_note:
        st.divider()
        st.caption(f"Supabase: {persistence_note}")


def page_label_copy(token: str, spotify_user: str) -> None:
    st.title("Label Copy")
    st.caption(
        "Δημιουργεί αυτόματα το label copy μιας κυκλοφορίας, επιτρέπει πλήρη έλεγχο "
        "και διόρθωση των metadata και εξάγει Word και PDF έτοιμο για εκτύπωση."
    )

    st.markdown("### Επιλογή Κυκλοφορίας")
    mode = st.radio(
        "Τρόπος επιλογής",
        ("Σύνδεσμος Κυκλοφορίας", "Από Playlist"),
        horizontal=True,
        label_visibility="collapsed",
        key="label_copy_input_mode",
    )

    selection: dict[str, Any] | None = None
    if mode == "Σύνδεσμος Κυκλοφορίας":
        release_input = st.text_input(
            "Σύνδεσμος Album / Single / EP",
            placeholder=(
                "Spotify album URL / spotify:album:ID / Apple Music album URL"
            ),
            key="label_copy_release_input",
        )
        provider, release_id, parse_note = _parse_release_input(release_input)
        if release_input and parse_note:
            st.caption(parse_note)
        if provider and release_id:
            selection = {
                "provider": provider,
                "release_id": release_id,
                "display_name": release_input,
                "selection_key": f"{provider}:{release_id}",
            }
    else:
        try:
            playlists = fetch_user_playlists(token)
        except Exception as exc:
            st.error(f"Σφάλμα ανάκτησης Spotify playlists: {exc}")
            playlists = []

        if not playlists:
            st.warning("Δεν βρέθηκαν διαθέσιμες δικές σας ή collaborative playlists.")
        else:
            playlist_names = [item["name"] for item in playlists]
            selected_name = st.selectbox(
                "Επιλέξτε Playlist:",
                playlist_names,
                key="label_copy_playlist_select",
            )
            selected_playlist = next(
                item for item in playlists if item["name"] == selected_name
            )
            selection = {
                "provider": "playlist",
                "release_id": selected_playlist["id"],
                "display_name": selected_playlist["name"],
                "selection_key": f"playlist:{selected_playlist['id']}",
            }

    resolve_trigger = st.button(
        "Δημιουργία Label Copy",
        type="primary",
        width="stretch",
        disabled=selection is None,
        key="label_copy_resolve_button",
    )

    if resolve_trigger and selection:
        st.divider()
        st.markdown("#### Ζωντανή Δραστηριότητα")
        live_status = st.empty()
        progress_bar = st.progress(0.0)
        try:
            data, logs = _resolve_release(
                token,
                selection,
                progress_bar=progress_bar,
                live_status=live_status,
            )
            provider_matches = data.get("provider_matches")
            resolved_album_id = (
                _clean_text(provider_matches.get("resolved_spotify_release_id"))
                if isinstance(provider_matches, Mapping)
                else ""
            )
            state_identity = (
                f"spotify-album:{resolved_album_id}"
                if resolved_album_id
                else selection["selection_key"]
            )
            state_key = _stable_state_key(state_identity)
            st.session_state[state_key] = {
                "data": data,
                "selection": dict(selection),
                "logs": _unique_texts([*logs, *data.get("warnings", [])]),
                "release_editor_rows": _release_editor_rows(data),
                "track_editor_rows": _track_editor_rows(data),
                "credit_editor_rows": _credit_editor_rows(data),
                "reviewed_data": None,
                "docx_bytes": None,
                "pdf_bytes": None,
                "docx_filename": None,
                "pdf_filename": None,
                "persistence_note": "",
            }
            st.session_state[SESSION_ACTIVE_KEY] = state_key
            st.toast("Η αυτόματη επίλυση metadata ολοκληρώθηκε.", icon="✅")
        except Exception as exc:
            progress_bar.empty()
            live_status.empty()
            st.error(f"Αποτυχία δημιουργίας Label Copy: {exc}")

    active_state_key = st.session_state.get(SESSION_ACTIVE_KEY)
    state = st.session_state.get(active_state_key) if active_state_key else None
    if not isinstance(state, dict) or not isinstance(state.get("data"), Mapping):
        return

    st.divider()
    st.markdown("### Επεξεργασία & Επιβεβαίωση")
    st.caption(
        "Οι πηγές εμφανίζονται με χρωματική ένδειξη. Συμπληρώστε τα κενά πεδία, "
        "διορθώστε τυχόν ασυμφωνίες και ενεργοποιήστε τις επιβεβαιώσεις πριν την εξαγωγή."
    )

    widget_suffix = active_state_key.removeprefix(SESSION_STATE_PREFIX)
    with st.expander("Πεδία κυκλοφορίας", expanded=True):
        release_editor = _render_release_editor(state, widget_suffix)
    with st.expander("Μεταδεδομένα ανά track", expanded=True):
        track_editor = _render_track_editor(state, widget_suffix)
    with st.expander("Συντελεστές ανά track", expanded=True):
        credit_editor = _render_credit_editor(state, widget_suffix)
        st.caption(
            "Για νέα credits προσθέστε γραμμή, ορίστε Α/Α track, φυσική ονομασία ρόλου "
            "και ονόματα χωρισμένα με κόμμα."
        )

    generate_files = st.button(
        "Παραγωγή Word & PDF",
        type="primary",
        width="stretch",
        key=f"label_copy_render_{widget_suffix}",
    )

    if generate_files:
        try:
            reviewed_data = _apply_review_edits(
                state["data"],
                release_editor.to_dict("records"),
                track_editor.to_dict("records"),
                credit_editor.to_dict("records"),
            )

            with st.spinner("Φόρτωση DOCX template και παραγωγή αρχείων..."):
                template_config = get_label_copy_template_config()
                template_bytes = fetch_private_label_copy_template_bytes(**template_config)
                docx_buffer = generate_label_copy_docx(template_bytes, reviewed_data)
                pdf_buffer = generate_label_copy_pdf(reviewed_data)

            docx_filename = make_label_copy_filename(
                reviewed_data.get("project_name"),
                extension="docx",
                issue_date=reviewed_data.get("issue_date"),
            )
            pdf_filename = make_label_copy_pdf_filename(
                reviewed_data.get("project_name"),
                issue_date=reviewed_data.get("issue_date"),
            )

            state["reviewed_data"] = reviewed_data
            state["docx_bytes"] = docx_buffer.getvalue()
            state["pdf_bytes"] = pdf_buffer.getvalue()
            state["docx_filename"] = docx_filename
            state["pdf_filename"] = pdf_filename
            state["persistence_note"] = ""

            st.toast("Τα Word και PDF δημιουργήθηκαν επιτυχώς.", icon="✅")
        except (PdfFontError, PdfRenderError) as exc:
            st.error(f"Σφάλμα PDF/Unicode font: {exc}")
        except Exception as exc:
            st.error(f"Μη αναμενόμενο σφάλμα κατά την εξαγωγή: {exc}")

    reviewed_data = state.get("reviewed_data")
    docx_bytes = state.get("docx_bytes")
    pdf_bytes = state.get("pdf_bytes")
    if not isinstance(reviewed_data, Mapping) or not docx_bytes or not pdf_bytes:
        return

    st.markdown("### 📄 Αποτελέσματα & Εξαγωγή")
    tab_summary, tab_preview, tab_logs = st.tabs(
        ["Σύνοψη", "Προεπισκόπηση", "Σφάλματα & Logs"]
    )

with tab_summary:
        current_warnings = validate_label_copy_data(reviewed_data)
        m1, m2, m3 = st.columns(3)
        m1.metric("Σύνολο Τραγουδιών", len(reviewed_data.get("tracks", [])))
        m2.metric("Πεδία που συμπληρώθηκαν αυτόματα", _count_auto_filled_fields(reviewed_data))
        m3.metric("Προειδοποιήσεις", len(current_warnings))

        st.markdown("<br>", unsafe_allow_html=True)
        
        st.markdown("### 1. Λήψη Αρχικού Word (.docx)")
        st.download_button(
            label="⬇️ Λήψη Word (.docx)",
            data=docx_bytes,
            file_name=state["docx_filename"],
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            width="stretch",
            type="primary",
            key=f"label_copy_docx_download_{widget_suffix}",
        )
        
        st.divider()
        
        st.markdown("### 2. Τελικό PDF & Αποθήκευση")
        st.caption("Μπορείτε να χρησιμοποιήσετε το αυτόματο PDF, ή να κατεβάσετε το Word, να το διορθώσετε τοπικά, να το κάνετε Export ως PDF και να το ανεβάσετε εδώ για το αρχείο.")
        
        pdf_mode = st.radio(
            "Μορφή PDF",
            ["Αυτόματη δημιουργία", "Ανέβασμα διορθωμένου PDF"],
            horizontal=True,
            key=f"pdf_mode_{widget_suffix}"
        )
        
        final_pdf_bytes = pdf_bytes
        if pdf_mode == "Ανέβασμα διορθωμένου PDF":
            uploaded_pdf = st.file_uploader("Ανεβάστε το τελικό PDF", type=["pdf"])
            if uploaded_pdf is not None:
                candidate_bytes = uploaded_pdf.read()
                if not candidate_bytes.startswith(b"%PDF"):
                    st.error("Το αρχείο δεν είναι έγκυρο PDF.")
                    final_pdf_bytes = None
                else:
                    final_pdf_bytes = candidate_bytes
                    st.success("Το χειροκίνητο PDF είναι έτοιμο.")
            else:
                final_pdf_bytes = None
                
        if final_pdf_bytes:
            st.download_button(
                label="⬇️ Λήψη PDF",
                data=final_pdf_bytes,
                file_name=state["pdf_filename"],
                mime="application/pdf",
                width="stretch",
                type="primary",
                key=f"label_copy_pdf_download_{widget_suffix}",
            )
            
            st.markdown("<br>", unsafe_allow_html=True)
            if st.button("💾 Αποθήκευση στο Ιστορικό (Supabase)", type="primary", width="stretch", key=f"save_history_{widget_suffix}"):
                with st.spinner("Αποθήκευση..."):
                    try:
                        urls, persistence_note = _upload_exports_to_supabase(
                            spotify_user=spotify_user,
                            project_name=_clean_text(reviewed_data.get("project_name")),
                            track_count=len(reviewed_data.get("tracks", [])),
                            docx_bytes=state["docx_bytes"],
                            docx_filename=state["docx_filename"],
                            pdf_bytes=final_pdf_bytes,
                            pdf_filename=state["pdf_filename"],
                        )
                        state["supabase_urls"] = urls
                        if persistence_note:
                            st.warning(persistence_note)
                        else:
                            st.success("Τα αρχεία αποθηκεύτηκαν επιτυχώς στο Supabase!")
                    except Exception as exc:
                        st.error(f"Δεν ολοκληρώθηκε η αποθήκευση: {exc}")

    with tab_preview:
        _render_preview(reviewed_data)

    with tab_logs:
        _render_logs(state, reviewed_data)


__all__ = ["page_label_copy"]
