"""
utils/apple_music_api.py

Unauthenticated iTunes Search/Lookup integration used by the Label Copy data
layer for genre enrichment and release cross-checks.

Despite the historical module name, this file intentionally does *not*
implement MusicKit or the paid Apple Music API. The approved implementation
uses only the public iTunes Search API endpoints at itunes.apple.com.

All public network helpers follow a never-raises contract and return
``(data, note)``. ``data`` is a normalized dictionary on success; ``note`` is
``None`` for a clean success and a Greek diagnostic message otherwise.
"""

from __future__ import annotations

import difflib
import re
import time
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

import requests

try:  # Streamlit is available in production; this fallback keeps unit imports light.
    import streamlit as st
except ImportError:  # pragma: no cover - exercised only outside the app environment.
    st = None


ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
ITUNES_LOOKUP_URL = "https://itunes.apple.com/lookup"
ITUNES_DEFAULT_COUNTRY = "GR"
ITUNES_FALLBACK_COUNTRY = "US"
ITUNES_CACHE_TTL_SECONDS = 15 * 60
ITUNES_REQUEST_TIMEOUT_SECONDS = 12
ITUNES_MAX_RETRIES = 3
ITUNES_SEARCH_LIMIT = 25
ITUNES_MINIMUM_MATCH_SCORE = 0.72
ITUNES_USER_AGENT = "stay-independent-label-copy/1.0"

APPLE_COLLECTION_ID_RE = re.compile(
    r"(?:music\.apple\.com/(?:[a-z]{2}/)?album/(?:[^/?#]+/)?|/album/)(\d+)",
    re.IGNORECASE,
)
BARE_COLLECTION_ID_RE = re.compile(r"^\d{4,20}$")


class _ItunesRequestError(RuntimeError):
    """Internal exception used so failed requests are not cached."""


def _cache_data(*, ttl: int):
    if st is None:
        def decorator(func):
            return func
        return decorator
    return st.cache_data(ttl=ttl, show_spinner=False)


def _retry_after_seconds(value: Any, default: float = 1.0) -> float:
    try:
        return max(0.0, min(float(value), 30.0))
    except (TypeError, ValueError):
        return default


def _request_json_uncached(url: str, params: Mapping[str, Any]) -> dict[str, Any]:
    headers = {
        "Accept": "application/json",
        "User-Agent": ITUNES_USER_AGENT,
    }
    last_message = "Αποτυχία επικοινωνίας με το iTunes Search API."

    for attempt in range(ITUNES_MAX_RETRIES):
        try:
            response = requests.get(
                url,
                headers=headers,
                params=dict(params),
                timeout=ITUNES_REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            last_message = f"Αποτυχία επικοινωνίας με το iTunes Search API: {exc}"
            if attempt + 1 < ITUNES_MAX_RETRIES:
                time.sleep(0.5 * (2 ** attempt))
                continue
            raise _ItunesRequestError(last_message) from exc

        if response.status_code == 429:
            last_message = "Το iTunes Search API επέβαλε προσωρινό περιορισμό κλήσεων."
            if attempt + 1 < ITUNES_MAX_RETRIES:
                time.sleep(_retry_after_seconds(response.headers.get("Retry-After")))
                continue
            raise _ItunesRequestError(last_message)

        if 500 <= response.status_code < 600:
            last_message = f"Το iTunes Search API επέστρεψε HTTP {response.status_code}."
            if attempt + 1 < ITUNES_MAX_RETRIES:
                time.sleep(0.5 * (2 ** attempt))
                continue
            raise _ItunesRequestError(last_message)

        if not response.ok:
            raise _ItunesRequestError(
                f"Το iTunes Search API επέστρεψε HTTP {response.status_code}."
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise _ItunesRequestError(
                "Το iTunes Search API επέστρεψε μη έγκυρη JSON απάντηση."
            ) from exc

        if not isinstance(payload, dict):
            raise _ItunesRequestError(
                "Το iTunes Search API επέστρεψε απάντηση μη αναμενόμενης μορφής."
            )
        return payload

    raise _ItunesRequestError(last_message)


@_cache_data(ttl=ITUNES_CACHE_TTL_SECONDS)
def _request_json_cached(
    url: str,
    params_items: tuple[tuple[str, str], ...],
) -> dict[str, Any]:
    return _request_json_uncached(url, dict(params_items))


def _safe_itunes_get(
    url: str,
    params: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    """Cached, bounded-retry GET wrapper with a never-raises public contract."""
    try:
        normalized_items = tuple(
            sorted((str(key), str(value)) for key, value in params.items() if value is not None)
        )
        return _request_json_cached(url, normalized_items), None
    except _ItunesRequestError as exc:
        return None, str(exc)
    except Exception as exc:  # Defensive: Streamlit cache/runtime errors must not reach the UI.
        return None, f"Μη αναμενόμενο σφάλμα κατά την κλήση του iTunes Search API: {exc}"


def extract_itunes_collection_id(value: Any) -> str | None:
    """Extracts an Apple/iTunes collection ID from a URL, URI-like value, or bare ID."""
    text = str(value or "").strip()
    if not text:
        return None

    match = APPLE_COLLECTION_ID_RE.search(text)
    if match:
        return match.group(1)

    if BARE_COLLECTION_ID_RE.fullmatch(text):
        return text
    return None


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


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


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_nonnegative_int(value: Any) -> int | None:
    parsed = _as_int(value)
    if parsed is None:
        return None
    return max(parsed, 0)


def _result_kind(item: Mapping[str, Any]) -> str:
    wrapper_type = _clean_text(item.get("wrapperType")).lower()
    kind = _clean_text(item.get("kind")).lower()
    collection_type = _clean_text(item.get("collectionType")).lower()

    if wrapper_type == "collection" or collection_type:
        return "collection"
    if wrapper_type == "track" or kind == "song":
        return "track"
    return "unknown"


def _normalize_collection(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "collection_id": _as_int(item.get("collectionId")),
        "collection_name": _clean_text(item.get("collectionName")),
        "artist_name": _clean_text(item.get("artistName")),
        "artist_id": _as_int(item.get("artistId")),
        "release_date": _clean_text(item.get("releaseDate")),
        "track_count": _as_nonnegative_int(item.get("trackCount")),
        "disc_count": _as_nonnegative_int(item.get("discCount")),
        "primary_genre_name": _clean_text(item.get("primaryGenreName")),
        "collection_explicitness": _clean_text(item.get("collectionExplicitness")),
        "collection_view_url": _clean_text(item.get("collectionViewUrl")),
        "copyright": _clean_text(item.get("copyright")),
        "country": _clean_text(item.get("country")),
        "currency": _clean_text(item.get("currency")),
        "raw": dict(item),
    }


def _normalize_track(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "track_id": _as_int(item.get("trackId")),
        "collection_id": _as_int(item.get("collectionId")),
        "track_name": _clean_text(item.get("trackName")),
        "collection_name": _clean_text(item.get("collectionName")),
        "artist_name": _clean_text(item.get("artistName")),
        "artist_id": _as_int(item.get("artistId")),
        "disc_number": _as_nonnegative_int(item.get("discNumber")),
        "disc_count": _as_nonnegative_int(item.get("discCount")),
        "track_number": _as_nonnegative_int(item.get("trackNumber")),
        "track_count": _as_nonnegative_int(item.get("trackCount")),
        "duration_ms": _as_nonnegative_int(item.get("trackTimeMillis")),
        "release_date": _clean_text(item.get("releaseDate")),
        "primary_genre_name": _clean_text(item.get("primaryGenreName")),
        "track_explicitness": _clean_text(item.get("trackExplicitness")),
        "track_view_url": _clean_text(item.get("trackViewUrl")),
        "preview_url": _clean_text(item.get("previewUrl")),
        "country": _clean_text(item.get("country")),
        "raw": dict(item),
    }


def _items_from_payload(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        return []
    return [dict(item) for item in raw_results if isinstance(item, Mapping)]


def _album_match_score(
    candidate: Mapping[str, Any],
    *,
    artist: str,
    album: str,
    expected_track_count: int | None,
) -> float:
    title_score = _similarity(candidate.get("collectionName"), album)
    artist_score = _similarity(candidate.get("artistName"), artist)

    candidate_count = _as_int(candidate.get("trackCount"))
    if expected_track_count and candidate_count:
        count_score = max(
            0.0,
            1.0 - (abs(candidate_count - expected_track_count) / max(expected_track_count, 1)),
        )
    else:
        count_score = 0.5

    return round((title_score * 0.58) + (artist_score * 0.34) + (count_score * 0.08), 4)


def _choose_collection_candidate(
    items: Sequence[Mapping[str, Any]],
    *,
    artist: str = "",
    album: str = "",
    expected_track_count: int | None = None,
    preferred_collection_id: int | None = None,
) -> tuple[dict[str, Any] | None, float]:
    collections = [item for item in items if _result_kind(item) == "collection"]
    if not collections:
        # Some lookup responses contain only song rows. Derive one collection row
        # from the first track so the normalized result remains useful.
        tracks = [item for item in items if _result_kind(item) == "track"]
        if tracks:
            first = tracks[0]
            derived = {
                "wrapperType": "collection",
                "collectionId": first.get("collectionId"),
                "collectionName": first.get("collectionName"),
                "artistName": first.get("artistName"),
                "artistId": first.get("artistId"),
                "releaseDate": first.get("releaseDate"),
                "trackCount": first.get("trackCount"),
                "discCount": first.get("discCount"),
                "primaryGenreName": first.get("primaryGenreName"),
                "collectionExplicitness": first.get("collectionExplicitness"),
                "collectionViewUrl": first.get("collectionViewUrl"),
                "country": first.get("country"),
                "currency": first.get("currency"),
            }
            collections = [derived]

    if preferred_collection_id is not None:
        for candidate in collections:
            if _as_int(candidate.get("collectionId")) == preferred_collection_id:
                return dict(candidate), 1.0

    if not collections:
        return None, 0.0

    if not artist and not album:
        return dict(collections[0]), 1.0

    scored = [
        (
            _album_match_score(
                candidate,
                artist=artist,
                album=album,
                expected_track_count=expected_track_count,
            ),
            candidate,
        )
        for candidate in collections
    ]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    best_score, best_candidate = scored[0]
    return dict(best_candidate), best_score


def _normalize_release_payload(
    payload: Mapping[str, Any],
    *,
    country: str,
    matched_by: str,
    artist: str = "",
    album: str = "",
    expected_track_count: int | None = None,
    preferred_collection_id: int | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    items = _items_from_payload(payload)
    if not items:
        return None, "Δεν βρέθηκε αντίστοιχη κυκλοφορία στο iTunes."

    collection_item, score = _choose_collection_candidate(
        items,
        artist=artist,
        album=album,
        expected_track_count=expected_track_count,
        preferred_collection_id=preferred_collection_id,
    )
    if collection_item is None:
        return None, "Η απάντηση του iTunes δεν περιείχε αναγνωρίσιμη κυκλοφορία."

    if matched_by == "search" and score < ITUNES_MINIMUM_MATCH_SCORE:
        return (
            None,
            "Η καλύτερη αντιστοίχιση iTunes είχε χαμηλή βεβαιότητα "
            f"({score:.0%}) και απορρίφθηκε.",
        )

    collection = _normalize_collection(collection_item)
    collection_id = collection.get("collection_id")

    tracks = []
    for item in items:
        if _result_kind(item) != "track":
            continue
        normalized_track = _normalize_track(item)
        if collection_id is not None and normalized_track.get("collection_id") != collection_id:
            continue
        tracks.append(normalized_track)

    tracks.sort(
        key=lambda track: (
            track.get("disc_number") or 0,
            track.get("track_number") or 0,
            track.get("track_id") or 0,
        )
    )

    collection.update(
        {
            "tracks": tracks,
            "matched_by": matched_by,
            "match_score": score,
            "storefront_country": country.upper(),
            "source": "itunes",
        }
    )

    note = None
    if matched_by == "search" and score < 0.85:
        note = (
            "Η αντιστοίχιση iTunes έγινε με μέτρια βεβαιότητα "
            f"({score:.0%}) και χρειάζεται επιβεβαίωση."
        )
    return collection, note


def _country_sequence(country: str | None) -> tuple[str, ...]:
    primary = _clean_text(country).upper() or ITUNES_DEFAULT_COUNTRY
    countries = [primary]
    if primary != ITUNES_FALLBACK_COUNTRY:
        countries.append(ITUNES_FALLBACK_COUNTRY)
    return tuple(countries)


def lookup_itunes_release_by_collection_id(
    collection_id: str | int,
    *,
    country: str = ITUNES_DEFAULT_COUNTRY,
) -> tuple[dict[str, Any] | None, str | None]:
    """Looks up an iTunes album by collection ID and includes its song rows."""
    parsed_id = extract_itunes_collection_id(collection_id)
    if not parsed_id:
        return None, "Ο αναγνωριστικός αριθμός Apple/iTunes album δεν είναι έγκυρος."

    notes: list[str] = []
    for storefront in _country_sequence(country):
        payload, note = _safe_itunes_get(
            ITUNES_LOOKUP_URL,
            {
                "id": parsed_id,
                "entity": "song",
                "country": storefront,
            },
        )
        if note:
            notes.append(note)
            continue
        if payload:
            normalized, normalize_note = _normalize_release_payload(
                payload,
                country=storefront,
                matched_by="collection_id",
                preferred_collection_id=int(parsed_id),
            )
            if normalized:
                if storefront != _country_sequence(country)[0]:
                    fallback_note = (
                        f"Η κυκλοφορία βρέθηκε στο iTunes storefront {storefront} "
                        "αντί του αρχικού storefront."
                    )
                    normalize_note = normalize_note or fallback_note
                return normalized, normalize_note
            if normalize_note:
                notes.append(normalize_note)

    return None, notes[-1] if notes else "Δεν βρέθηκε η κυκλοφορία στο iTunes."


def lookup_itunes_release_by_upc(
    upc: Any,
    *,
    country: str = ITUNES_DEFAULT_COUNTRY,
) -> tuple[dict[str, Any] | None, str | None]:
    """Looks up a release by an already-known UPC/EAN and includes song rows."""
    clean_upc = re.sub(r"\D+", "", str(upc or ""))
    if not clean_upc:
        return None, "Δεν δόθηκε έγκυρο UPC/EAN για αναζήτηση στο iTunes."

    notes: list[str] = []
    storefronts = _country_sequence(country)
    for storefront in storefronts:
        payload, note = _safe_itunes_get(
            ITUNES_LOOKUP_URL,
            {
                "upc": clean_upc,
                "entity": "song",
                "country": storefront,
            },
        )
        if note:
            notes.append(note)
            continue
        if payload:
            normalized, normalize_note = _normalize_release_payload(
                payload,
                country=storefront,
                matched_by="upc",
            )
            if normalized:
                normalized["lookup_upc"] = clean_upc
                if storefront != storefronts[0]:
                    normalize_note = normalize_note or (
                        f"Το UPC βρέθηκε στο iTunes storefront {storefront} "
                        "αντί του αρχικού storefront."
                    )
                return normalized, normalize_note
            if normalize_note:
                notes.append(normalize_note)

    return None, notes[-1] if notes else "Δεν βρέθηκε κυκλοφορία με αυτό το UPC στο iTunes."


def search_itunes_release(
    *,
    artist: str,
    album: str,
    expected_track_count: int | None = None,
    country: str = ITUNES_DEFAULT_COUNTRY,
    limit: int = ITUNES_SEARCH_LIMIT,
) -> tuple[dict[str, Any] | None, str | None]:
    """Searches albums by artist/title, selects a scored candidate, then expands it."""
    artist = _clean_text(artist)
    album = _clean_text(album)
    if not album:
        return None, "Απαιτείται τίτλος κυκλοφορίας για αναζήτηση στο iTunes."

    term = " ".join(part for part in (artist, album) if part)
    storefronts = _country_sequence(country)
    notes: list[str] = []

    for storefront in storefronts:
        payload, note = _safe_itunes_get(
            ITUNES_SEARCH_URL,
            {
                "term": term,
                "media": "music",
                "entity": "album",
                "limit": max(1, min(int(limit), 200)),
                "country": storefront,
            },
        )
        if note:
            notes.append(note)
            continue
        if not payload:
            continue

        items = _items_from_payload(payload)
        candidate, score = _choose_collection_candidate(
            items,
            artist=artist,
            album=album,
            expected_track_count=expected_track_count,
        )
        if candidate is None:
            notes.append("Δεν βρέθηκε αναγνωρίσιμο album στα αποτελέσματα iTunes.")
            continue
        if score < ITUNES_MINIMUM_MATCH_SCORE:
            notes.append(
                "Η καλύτερη αντιστοίχιση iTunes είχε χαμηλή βεβαιότητα "
                f"({score:.0%}) και απορρίφθηκε."
            )
            continue

        collection_id = _as_int(candidate.get("collectionId"))
        if collection_id is None:
            notes.append("Η αντιστοίχιση iTunes δεν είχε collection ID.")
            continue

        expanded, expanded_note = lookup_itunes_release_by_collection_id(
            collection_id,
            country=storefront,
        )
        if not expanded:
            notes.append(expanded_note or "Αποτυχία ανάκτησης των iTunes tracks.")
            continue

        expanded["matched_by"] = "search"
        expanded["match_score"] = score
        if storefront != storefronts[0] and not expanded_note:
            expanded_note = (
                f"Η κυκλοφορία βρέθηκε στο iTunes storefront {storefront} "
                "αντί του αρχικού storefront."
            )
        if score < 0.85:
            score_note = (
                "Η αντιστοίχιση iTunes έγινε με μέτρια βεβαιότητα "
                f"({score:.0%}) και χρειάζεται επιβεβαίωση."
            )
            expanded_note = expanded_note or score_note
        return expanded, expanded_note

    return None, notes[-1] if notes else "Δεν βρέθηκε αντίστοιχη κυκλοφορία στο iTunes."


def fetch_itunes_release(
    *,
    collection_id: str | int | None = None,
    upc: Any = None,
    artist: str = "",
    album: str = "",
    expected_track_count: int | None = None,
    country: str = ITUNES_DEFAULT_COUNTRY,
) -> tuple[dict[str, Any] | None, str | None]:
    """
    High-level iTunes release resolver.

    Resolution order:
      1. Explicit Apple/iTunes collection ID.
      2. Already-known UPC/EAN.
      3. Scored artist/title album search.

    The function never treats the UPC endpoint as UPC discovery: it only uses
    it when a UPC is already available from Spotify, MusicBrainz, or the user.
    """
    attempts: list[tuple[str, str | None]] = []

    if collection_id is not None and str(collection_id).strip():
        data, note = lookup_itunes_release_by_collection_id(collection_id, country=country)
        if data:
            return data, note
        attempts.append(("collection ID", note))

    if str(upc or "").strip():
        data, note = lookup_itunes_release_by_upc(upc, country=country)
        if data:
            return data, note
        attempts.append(("UPC", note))

    if _clean_text(album):
        data, note = search_itunes_release(
            artist=artist,
            album=album,
            expected_track_count=expected_track_count,
            country=country,
        )
        if data:
            return data, note
        attempts.append(("τίτλο/καλλιτέχνη", note))

    useful_notes = [f"{method}: {note}" for method, note in attempts if note]
    if useful_notes:
        return None, " | ".join(useful_notes)
    return None, "Δεν υπήρχαν επαρκή στοιχεία για αναζήτηση κυκλοφορίας στο iTunes."


def match_itunes_track(
    spotify_track: Mapping[str, Any],
    itunes_tracks: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, Any] | None, float]:
    """
    Matches a Spotify track to a normalized iTunes track.

    Disc/track coordinates are decisive when present. Title and duration are
    used as cross-checks and as a fallback for malformed store metadata.
    """
    spotify_title = _clean_text(spotify_track.get("name") or spotify_track.get("title"))
    spotify_disc = _as_int(spotify_track.get("disc_number"))
    spotify_number = _as_int(spotify_track.get("track_number"))
    spotify_duration = _as_int(spotify_track.get("duration_ms"))

    candidates: list[tuple[float, Mapping[str, Any]]] = []
    for candidate in itunes_tracks:
        title_score = _similarity(candidate.get("track_name"), spotify_title)
        coordinate_score = 0.0

        candidate_disc = _as_int(candidate.get("disc_number"))
        candidate_number = _as_int(candidate.get("track_number"))
        if spotify_disc and spotify_number and candidate_disc and candidate_number:
            coordinate_score = 1.0 if (
                spotify_disc == candidate_disc and spotify_number == candidate_number
            ) else 0.0
        elif spotify_number and candidate_number:
            coordinate_score = 1.0 if spotify_number == candidate_number else 0.0
        else:
            coordinate_score = 0.5

        candidate_duration = _as_int(candidate.get("duration_ms"))
        if spotify_duration is not None and candidate_duration is not None:
            difference = abs(spotify_duration - candidate_duration)
            duration_score = max(0.0, 1.0 - (difference / 12_000.0))
        else:
            duration_score = 0.5

        score = (coordinate_score * 0.52) + (title_score * 0.38) + (duration_score * 0.10)
        candidates.append((score, candidate))

    if not candidates:
        return None, 0.0

    candidates.sort(key=lambda pair: pair[0], reverse=True)
    score, candidate = candidates[0]
    if score < 0.62:
        return None, score
    return dict(candidate), round(score, 4)


# Backwards-readable alias for callers that prefer the "lookup" terminology.
lookup_itunes_release = fetch_itunes_release


__all__ = [
    "ITUNES_DEFAULT_COUNTRY",
    "extract_itunes_collection_id",
    "fetch_itunes_release",
    "lookup_itunes_release",
    "lookup_itunes_release_by_collection_id",
    "lookup_itunes_release_by_upc",
    "match_itunes_track",
    "search_itunes_release",
]
