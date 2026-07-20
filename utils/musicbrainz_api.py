"""
utils/musicbrainz_api.py

Centralised, cached MusicBrainz Web Service helpers for the Stay Independent
Tool.

All MusicBrainz network calls live in this module. Streamlit pages should only
handle UI and orchestration. The musicbrainzngs client keeps requests
sequential and enforces its built-in one-request-per-second rate limiter.
"""

import re
import urllib.parse
from typing import Callable, Dict, List

import musicbrainzngs
import streamlit as st

# --------------------------------------------------------------------------
# MusicBrainz configuration
# --------------------------------------------------------------------------
MB_APP_NAME = "StayIndependentTool"
MB_APP_VERSION = "2.0"
MB_CONTACT = "johnnakas03@gmail.com"

MB_CACHE_TTL_SECONDS = 3600
MB_SEARCH_DEFAULT_LIMIT = 10
MB_SEARCH_MAX_LIMIT = 100
MB_BROWSE_DEFAULT_LIMIT = 50
MB_BROWSE_MAX_LIMIT = 100
MB_UUID_PATTERN = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

MB_ENTITY_TYPES = (
    "area", "artist", "event", "instrument", "label", "place",
    "recording", "release", "release-group", "series", "url", "work",
)

MB_SEARCH_ENTITY_TYPES = (
    "artist", "release", "release-group", "recording", "label", "work",
)

MB_RELATION_INCLUDES = (
    "area-rels", "artist-rels", "label-rels", "place-rels", "event-rels",
    "recording-rels", "release-rels", "release-group-rels", "series-rels",
    "url-rels", "work-rels", "instrument-rels",
)

musicbrainzngs.set_useragent(MB_APP_NAME, MB_APP_VERSION, MB_CONTACT)


# --------------------------------------------------------------------------
# Generic parsing and formatting helpers
# --------------------------------------------------------------------------
def extract_mbid(raw_value):
    text = str(raw_value or "").strip()
    if not text:
        return None
    match = MB_UUID_PATTERN.search(text)
    return match.group(0).lower() if match else None

def mb_entity_url(entity_type, mbid):
    clean_entity = str(entity_type or "").strip().lower()
    clean_mbid = extract_mbid(mbid)
    if clean_entity not in MB_ENTITY_TYPES or not clean_mbid:
        return "https://musicbrainz.org"
    return f"https://musicbrainz.org/{clean_entity}/{clean_mbid}"

def mb_artist_credit_phrase(entity):
    if not isinstance(entity, dict):
        return ""
    phrase = entity.get("artist-credit-phrase")
    if phrase:
        return str(phrase)
    parts = []
    for credit in entity.get("artist-credit") or []:
        if isinstance(credit, str):
            parts.append(credit)
            continue
        if not isinstance(credit, dict):
            continue
        artist = credit.get("artist") or {}
        credit_name = credit.get("name") or artist.get("name") or ""
        parts.append(str(credit_name))
        join_phrase = credit.get("joinphrase")
        if join_phrase:
            parts.append(str(join_phrase))
    return "".join(parts).strip()

def mb_iswc(work):
    if not isinstance(work, dict):
        return ""
    single = str(work.get("iswc") or "").strip()
    if single:
        return single
    iswc_list = work.get("iswc-list") or []
    if isinstance(iswc_list, list) and iswc_list:
        return ", ".join(str(value).strip() for value in iswc_list if str(value).strip())
    return ""

def mb_format_length(milliseconds):
    try:
        total_seconds = int(int(milliseconds) / 1000)
    except (TypeError, ValueError):
        return "—"
    return f"{total_seconds // 60}:{total_seconds % 60:02d}"

def _clean_query(query):
    return " ".join(str(query or "").split()).strip()

def _normalise_limit(limit):
    try:
        parsed = int(limit)
    except (TypeError, ValueError):
        parsed = MB_SEARCH_DEFAULT_LIMIT
    return max(1, min(parsed, MB_SEARCH_MAX_LIMIT))

def _normalise_browse_limit(limit):
    try:
        parsed = int(limit)
    except (TypeError, ValueError):
        parsed = MB_BROWSE_DEFAULT_LIMIT
    return max(1, min(parsed, MB_BROWSE_MAX_LIMIT))

def _normalise_offset(offset):
    try:
        parsed = int(offset)
    except (TypeError, ValueError):
        parsed = 0
    return max(0, parsed)

def _safe_int(value, fallback=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback

def _run_entity_search(
    search_function: Callable,
    result_key: str,
    field_name: str,
    query: str,
    limit: int,
    strict: bool,
    lucene_query: bool,
) -> List[dict]:
    clean_query = _clean_query(query)
    if not clean_query:
        return []
    request_kwargs = {
        "limit": _normalise_limit(limit),
        "strict": bool(strict),
    }
    if lucene_query:
        request_kwargs["query"] = clean_query
    else:
        request_kwargs[field_name] = clean_query

    data = search_function(**request_kwargs)
    return data.get(result_key) or []

def _release_browse_payload(data, offset, limit):
    releases = data.get("release-list") or []
    if not isinstance(releases, list):
        releases = []
    total_count = _safe_int(data.get("release-count"), len(releases))
    return {
        "release-list": releases,
        "release-count": max(total_count, len(releases)),
        "release-offset": _normalise_offset(offset),
        "release-limit": _normalise_browse_limit(limit),
    }

# --------------------------------------------------------------------------
# Cached MusicBrainz lookup calls
# --------------------------------------------------------------------------
@st.cache_data(ttl=MB_CACHE_TTL_SECONDS, show_spinner=False)
def mb_get_artist(mbid):
    data = musicbrainzngs.get_artist_by_id(
        mbid,
        includes=["releases", "url-rels", "artist-rels", "work-rels", "aliases"],
    )
    return data.get("artist") or {}

@st.cache_data(ttl=MB_CACHE_TTL_SECONDS, show_spinner=False)
def mb_get_recordings_by_isrc(isrc):
    try:
        data = musicbrainzngs.get_recordings_by_isrc(
            isrc,
            includes=["work-rels", "artists"],
        )
        work_rels_requested = True
    except musicbrainzngs.InvalidIncludeError:
        data = musicbrainzngs.get_recordings_by_isrc(
            isrc,
            includes=["artists"],
        )
        work_rels_requested = False

    result = data.get("isrc") or {}
    if not work_rels_requested:
        for recording in result.get("recording-list") or []:
            if isinstance(recording, dict):
                recording["_work_rels_unavailable"] = True
    return result

@st.cache_data(ttl=MB_CACHE_TTL_SECONDS, show_spinner=False)
def mb_get_recording(recording_mbid):
    data = musicbrainzngs.get_recording_by_id(
        recording_mbid,
        includes=["work-rels", "artists", "isrcs"],
    )
    return data.get("recording") or {}

@st.cache_data(ttl=MB_CACHE_TTL_SECONDS, show_spinner=False)
def mb_get_work(work_mbid):
    data = musicbrainzngs.get_work_by_id(
        work_mbid,
        includes=["artist-rels"],
    )
    return data.get("work") or {}

@st.cache_data(ttl=MB_CACHE_TTL_SECONDS, show_spinner=False)
def mb_search_releases_by_barcode(barcode):
    data = musicbrainzngs.search_releases(barcode=barcode, limit=10)
    return data.get("release-list") or []

@st.cache_data(ttl=MB_CACHE_TTL_SECONDS, show_spinner=False)
def mb_get_release(release_mbid):
    data = musicbrainzngs.get_release_by_id(
        release_mbid,
        includes=["recordings", "labels", "artists", "media"],
    )
    return data.get("release") or {}

@st.cache_data(ttl=MB_CACHE_TTL_SECONDS, show_spinner=False)
def mb_get_label(label_mbid):
    includes = ["aliases", "annotation", *MB_RELATION_INCLUDES]
    data = musicbrainzngs.get_label_by_id(label_mbid, includes=includes)
    return data.get("label") or {}

@st.cache_data(ttl=MB_CACHE_TTL_SECONDS, show_spinner=False)
def mb_browse_label_releases(label_mbid, limit=MB_BROWSE_DEFAULT_LIMIT, offset=0):
    clean_limit = _normalise_browse_limit(limit)
    clean_offset = _normalise_offset(offset)
    data = musicbrainzngs.browse_releases(
        label=label_mbid,
        includes=["artist-credits", "labels", "release-groups", "media"],
        limit=clean_limit,
        offset=clean_offset,
    )
    return _release_browse_payload(data, clean_offset, clean_limit)

@st.cache_data(ttl=MB_CACHE_TTL_SECONDS, show_spinner=False)
def mb_get_release_group(release_group_mbid):
    includes = ["artists", "artist-credits", "aliases", "annotation", *MB_RELATION_INCLUDES]
    data = musicbrainzngs.get_release_group_by_id(
        release_group_mbid,
        includes=includes,
    )
    return data.get("release-group") or {}

@st.cache_data(ttl=MB_CACHE_TTL_SECONDS, show_spinner=False)
def mb_browse_release_group_releases(release_group_mbid, limit=MB_BROWSE_DEFAULT_LIMIT, offset=0):
    clean_limit = _normalise_browse_limit(limit)
    clean_offset = _normalise_offset(offset)
    data = musicbrainzngs.browse_releases(
        release_group=release_group_mbid,
        includes=["artist-credits", "labels", "release-groups", "media"],
        limit=clean_limit,
        offset=clean_offset,
    )
    return _release_browse_payload(data, clean_offset, clean_limit)

@st.cache_data(ttl=MB_CACHE_TTL_SECONDS, show_spinner=False)
def mb_get_work_full(work_mbid):
    includes = ["aliases", "annotation", *MB_RELATION_INCLUDES]
    data = musicbrainzngs.get_work_by_id(work_mbid, includes=includes)
    return data.get("work") or {}

# --------------------------------------------------------------------------
# Cached search-first calls
# --------------------------------------------------------------------------
@st.cache_data(ttl=MB_CACHE_TTL_SECONDS, show_spinner=False)
def mb_search_artists(query, limit=MB_SEARCH_DEFAULT_LIMIT, strict=False, lucene_query=False):
    return _run_entity_search(musicbrainzngs.search_artists, "artist-list", "artist", query, limit, strict, lucene_query)

@st.cache_data(ttl=MB_CACHE_TTL_SECONDS, show_spinner=False)
def mb_search_releases(query, limit=MB_SEARCH_DEFAULT_LIMIT, strict=False, lucene_query=False):
    return _run_entity_search(musicbrainzngs.search_releases, "release-list", "release", query, limit, strict, lucene_query)

@st.cache_data(ttl=MB_CACHE_TTL_SECONDS, show_spinner=False)
def mb_search_release_groups(query, limit=MB_SEARCH_DEFAULT_LIMIT, strict=False, lucene_query=False):
    return _run_entity_search(musicbrainzngs.search_release_groups, "release-group-list", "releasegroup", query, limit, strict, lucene_query)

@st.cache_data(ttl=MB_CACHE_TTL_SECONDS, show_spinner=False)
def mb_search_recordings(query, limit=MB_SEARCH_DEFAULT_LIMIT, strict=False, lucene_query=False):
    return _run_entity_search(musicbrainzngs.search_recordings, "recording-list", "recording", query, limit, strict, lucene_query)

@st.cache_data(ttl=MB_CACHE_TTL_SECONDS, show_spinner=False)
def mb_search_labels(query, limit=MB_SEARCH_DEFAULT_LIMIT, strict=False, lucene_query=False):
    return _run_entity_search(musicbrainzngs.search_labels, "label-list", "label", query, limit, strict, lucene_query)

@st.cache_data(ttl=MB_CACHE_TTL_SECONDS, show_spinner=False)
def mb_search_works(query, limit=MB_SEARCH_DEFAULT_LIMIT, strict=False, lucene_query=False):
    return _run_entity_search(musicbrainzngs.search_works, "work-list", "work", query, limit, strict, lucene_query)

_SEARCH_DISPATCH: Dict[str, Callable] = {
    "artist": mb_search_artists,
    "release": mb_search_releases,
    "release-group": mb_search_release_groups,
    "recording": mb_search_recordings,
    "label": mb_search_labels,
    "work": mb_search_works,
}

def mb_search_entities(entity_type, query, limit=MB_SEARCH_DEFAULT_LIMIT, strict=False, lucene_query=False):
    clean_entity = str(entity_type or "").strip().lower()
    search_function = _SEARCH_DISPATCH.get(clean_entity)
    if search_function is None:
        supported = ", ".join(MB_SEARCH_ENTITY_TYPES)
        raise ValueError(f"Unsupported MusicBrainz entity type: {entity_type}. Supported values: {supported}.")
    return search_function(query=query, limit=limit, strict=strict, lucene_query=lucene_query)

# --------------------------------------------------------------------------
# User-facing error translation
# --------------------------------------------------------------------------
def mb_error_message(exc):
    if isinstance(exc, musicbrainzngs.ResponseError):
        cause = getattr(exc, "cause", None)
        code = getattr(cause, "code", None)
        if code == 400: return "Το MusicBrainz απέρριψε το αίτημα (400). Ελέγξτε τη σύνταξη της αναζήτησης ή τον κωδικό που δώσατε."
        if code == 401: return "Το MusicBrainz απέρριψε την ταυτοποίηση (401). Ελέγξτε τα credentials."
        if code == 404: return "Το MusicBrainz δεν βρήκε αυτή την εγγραφή (404). Ελέγξτε τον κωδικό."
        if code == 503: return "Το MusicBrainz επιστρέφει 503 (rate limit / υπερφόρτωση). Δοκιμάστε ξανά σε λίγο."
        return f"Μη έγκυρη απάντηση από το MusicBrainz: {exc}"
    if isinstance(exc, musicbrainzngs.NetworkError): return f"Αδυναμία σύνδεσης με το MusicBrainz: {exc}"
    if isinstance(exc, musicbrainzngs.InvalidSearchFieldError): return f"Μη έγκυρο πεδίο αναζήτησης: {exc}"
    if isinstance(exc, musicbrainzngs.UsageError): return f"Μη έγκυρο αίτημα προς το MusicBrainz: {exc}"
    if isinstance(exc, musicbrainzngs.WebServiceError): return f"Σφάλμα υπηρεσίας MusicBrainz: {exc}"
    return f"Σφάλμα MusicBrainz: {exc}"

_mb_error_message = mb_error_message
_mb_artist_credit_phrase = mb_artist_credit_phrase
_mb_iswc = mb_iswc
_mb_format_length = mb_format_length

# ==========================================================================
# PHASE 3: Authenticated Submissions & Actions
# ==========================================================================

def mb_submit_tags(entity_type, mbid, tags_list):
    """Submits tags (genres, descriptors) to an entity."""
    clean_tags = [t.strip() for t in tags_list if t.strip()]
    if not clean_tags:
        return
    kwargs = {f"{entity_type}_tags": {mbid: clean_tags}}
    musicbrainzngs.submit_tags(**kwargs)

def mb_submit_rating(entity_type, mbid, rating_1_to_100):
    """Submits a user rating (0-100) to an entity."""
    kwargs = {f"{entity_type}_ratings": {mbid: rating_1_to_100}}
    musicbrainzngs.submit_ratings(**kwargs)

def mb_submit_isrcs(recording_mbid, isrcs_list):
    """Submits a list of ISRCs to a recording."""
    clean_isrcs = [i.strip().upper() for i in isrcs_list if i.strip()]
    if not clean_isrcs:
        return
    musicbrainzngs.submit_isrcs(recording_mbid, clean_isrcs)

def mb_submit_barcodes(release_mbid, barcode):
    """Submits a barcode to a release."""
    clean_barcode = re.sub(r"\D+", "", str(barcode or ""))
    if not clean_barcode:
        return
    musicbrainzngs.submit_barcodes({release_mbid: clean_barcode})

def mb_add_to_collection(collection_mbid, release_mbids_list):
    """Adds a list of Release MBIDs to a specified Collection MBID."""
    clean_mbids = [extract_mbid(m) for m in release_mbids_list if extract_mbid(m)]
    if not clean_mbids:
        return
    # The API supports up to 400 releases per request; loop if necessary.
    chunk_size = 300
    for i in range(0, len(clean_mbids), chunk_size):
        chunk = clean_mbids[i:i + chunk_size]
        musicbrainzngs.add_releases_to_collection(collection_mbid, chunk)

def mb_build_seeded_url(entity_type, action="create", **kwargs):
    """
    Builds a pre-filled MusicBrainz edit form URL.
    Example: mb_build_seeded_url("artist", "create", name="Queen", sort_name="Queen")
    """
    base = f"https://musicbrainz.org/{entity_type}/{action}"
    params = {}
    
    # Map kwargs to correct seeded param names (e.g., edit-artist.name)
    prefix = f"edit-{entity_type}"
    for key, value in kwargs.items():
        if not value:
            continue
        # Convert snake_case to hyphen-case
        mb_key = key.replace("_", "-")
        params[f"{prefix}.{mb_key}"] = value

    query_string = urllib.parse.urlencode(params)
    return f"{base}?{query_string}" if query_string else base
