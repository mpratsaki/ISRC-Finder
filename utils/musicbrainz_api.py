"""
utils/musicbrainz_api.py

Centralised, cached MusicBrainz Web Service helpers for the Stay Independent
Tool.

All MusicBrainz network calls live in this module. Streamlit pages should only
handle UI and orchestration. The musicbrainzngs client keeps requests
sequential and enforces its built-in one-request-per-second rate limiter.

Phase 2 adds full lookup/browse support for Labels, Release Groups and Works
without changing the established Artist, Recording and Release pipelines.
"""

import re
from typing import Callable, Dict, List

import musicbrainzngs
import streamlit as st

# --------------------------------------------------------------------------
# MusicBrainz configuration
# --------------------------------------------------------------------------
# Keep the existing User-Agent configuration unchanged. MusicBrainz requires a
# descriptive application name/version and contact address.
MB_APP_NAME = "StayIndependentTool"
MB_APP_VERSION = "2.0"
MB_CONTACT = "johnnakas03@gmail.com"  # Replace only if the project contact changes.

MB_CACHE_TTL_SECONDS = 3600
MB_SEARCH_DEFAULT_LIMIT = 10
MB_SEARCH_MAX_LIMIT = 100
MB_BROWSE_DEFAULT_LIMIT = 50
MB_BROWSE_MAX_LIMIT = 100
MB_UUID_PATTERN = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

# Entity types supported by the canonical MusicBrainz website URL helper.
MB_ENTITY_TYPES = (
    "area",
    "artist",
    "event",
    "instrument",
    "label",
    "place",
    "recording",
    "release",
    "release-group",
    "series",
    "url",
    "work",
)

# Entity types exposed by the app's Universal Search selector.
MB_SEARCH_ENTITY_TYPES = (
    "artist",
    "release",
    "release-group",
    "recording",
    "label",
    "work",
)

# Relationship includes supported by the installed musicbrainzngs client for
# Label, Release Group and Work lookups. Keeping them in one tuple prevents the
# three dedicated views from drifting into different relationship coverage.
MB_RELATION_INCLUDES = (
    "area-rels",
    "artist-rels",
    "label-rels",
    "place-rels",
    "event-rels",
    "recording-rels",
    "release-rels",
    "release-group-rels",
    "series-rels",
    "url-rels",
    "work-rels",
    "instrument-rels",
)

musicbrainzngs.set_useragent(MB_APP_NAME, MB_APP_VERSION, MB_CONTACT)


# --------------------------------------------------------------------------
# Generic parsing and formatting helpers
# --------------------------------------------------------------------------
def extract_mbid(raw_value):
    """
    Extract a MusicBrainz UUID from a bare MBID or any pasted MusicBrainz URL.

    Examples accepted:
    - xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
    - https://musicbrainz.org/artist/<mbid>
    - https://musicbrainz.org/release/<mbid>?foo=bar
    """
    text = str(raw_value or "").strip()
    if not text:
        return None

    match = MB_UUID_PATTERN.search(text)
    return match.group(0).lower() if match else None


def mb_entity_url(entity_type, mbid):
    """Return the canonical MusicBrainz website URL for an entity and MBID."""
    clean_entity = str(entity_type or "").strip().lower()
    clean_mbid = extract_mbid(mbid)

    if clean_entity not in MB_ENTITY_TYPES or not clean_mbid:
        return "https://musicbrainz.org"

    return f"https://musicbrainz.org/{clean_entity}/{clean_mbid}"


def mb_artist_credit_phrase(entity):
    """
    Flatten a MusicBrainz artist-credit list into a display string.

    musicbrainzngs may return either an ``artist-credit-phrase`` or a mixed list
    containing artist-credit dictionaries and bare join strings.
    """
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
    """Normalise a Work's single ``iswc`` or ``iswc-list`` to one string."""
    if not isinstance(work, dict):
        return ""

    single = str(work.get("iswc") or "").strip()
    if single:
        return single

    iswc_list = work.get("iswc-list") or []
    if isinstance(iswc_list, list) and iswc_list:
        return ", ".join(
            str(value).strip()
            for value in iswc_list
            if str(value).strip()
        )

    return ""


def mb_format_length(milliseconds):
    """Convert a MusicBrainz length in milliseconds to ``m:ss``."""
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
    """Execute one MusicBrainz entity search and return its entity list."""
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
        # Supplying the entity-specific field makes musicbrainzngs escape
        # Lucene special characters, which is safer for normal name searches.
        request_kwargs[field_name] = clean_query

    data = search_function(**request_kwargs)
    return data.get(result_key) or []


def _release_browse_payload(data, offset, limit):
    """Normalise a musicbrainzngs release browse response."""
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
# Cached MusicBrainz lookup calls migrated from tools/page_musicbrainz.py
# --------------------------------------------------------------------------
@st.cache_data(ttl=MB_CACHE_TTL_SECONDS, show_spinner=False)
def mb_get_artist(mbid):
    """Full artist lookup: releases, URLs, relationships and aliases."""
    data = musicbrainzngs.get_artist_by_id(
        mbid,
        includes=["releases", "url-rels", "artist-rels", "work-rels", "aliases"],
    )
    return data.get("artist") or {}


@st.cache_data(ttl=MB_CACHE_TTL_SECONDS, show_spinner=False)
def mb_get_recordings_by_isrc(isrc):
    """Resolve an ISRC to its recording(s).

    Some musicbrainzngs builds accept ``work-rels`` on an ISRC lookup while
    version 0.7.1 validates the ISRC endpoint more narrowly. Preserve the
    existing one-call path where supported and fall back to a valid artist
    include otherwise. The UI performs a cached recording-by-MBID lookup when
    Work relationships are not present in this response.
    """
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

    # Tag each recording explicitly so callers can tell "work-rels was
    # requested and came back empty" apart from "work-rels was never
    # requested because this build's include validation rejected it".
    # Without this, an empty work-relation-list (the common, legitimate
    # case for a recording with no linked Work) is indistinguishable from
    # a dropped include, and every such recording silently costs a second
    # MusicBrainz API call to confirm what was already known.
    if not work_rels_requested:
        for recording in result.get("recording-list") or []:
            if isinstance(recording, dict):
                recording["_work_rels_unavailable"] = True

    return result


@st.cache_data(ttl=MB_CACHE_TTL_SECONDS, show_spinner=False)
def mb_get_recording(recording_mbid):
    """
    Lookup one recording by MBID for the search-first fallback flow.

    This is the MBID equivalent of the existing ISRC resolver and provides the
    Work relationships required for the Recording → Work → writers chain.
    """
    data = musicbrainzngs.get_recording_by_id(
        recording_mbid,
        includes=["work-rels", "artists", "isrcs"],
    )
    return data.get("recording") or {}


@st.cache_data(ttl=MB_CACHE_TTL_SECONDS, show_spinner=False)
def mb_get_work(work_mbid):
    """Second-hop Work lookup including composer/lyricist relationships."""
    data = musicbrainzngs.get_work_by_id(
        work_mbid,
        includes=["artist-rels"],
    )
    return data.get("work") or {}


@st.cache_data(ttl=MB_CACHE_TTL_SECONDS, show_spinner=False)
def mb_search_releases_by_barcode(barcode):
    """Search the global release index by UPC/EAN barcode."""
    data = musicbrainzngs.search_releases(barcode=barcode, limit=10)
    return data.get("release-list") or []


@st.cache_data(ttl=MB_CACHE_TTL_SECONDS, show_spinner=False)
def mb_get_release(release_mbid):
    """Full release lookup for label information and complete tracklist."""
    data = musicbrainzngs.get_release_by_id(
        release_mbid,
        includes=["recordings", "labels", "artists", "media"],
    )
    return data.get("release") or {}


# --------------------------------------------------------------------------
# Cached Phase 2 core-entity lookups and browse calls
# --------------------------------------------------------------------------
@st.cache_data(ttl=MB_CACHE_TTL_SECONDS, show_spinner=False)
def mb_get_label(label_mbid):
    """
    Full Label lookup including aliases, annotation and all supported
    relationship target types.

    Releases are intentionally loaded through ``mb_browse_label_releases`` so
    the UI can page beyond the 25 linked entities available in a lookup.
    """
    includes = ["aliases", "annotation", *MB_RELATION_INCLUDES]
    data = musicbrainzngs.get_label_by_id(label_mbid, includes=includes)
    return data.get("label") or {}


@st.cache_data(ttl=MB_CACHE_TTL_SECONDS, show_spinner=False)
def mb_browse_label_releases(
    label_mbid,
    limit=MB_BROWSE_DEFAULT_LIMIT,
    offset=0,
):
    """Return one cached, paginated page of releases linked to a Label."""
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
    """
    Full Release Group lookup with artist credit, aliases, annotation and
    relationships. Editions are loaded separately through a browse request.
    """
    includes = [
        "artists",
        "artist-credits",
        "aliases",
        "annotation",
        *MB_RELATION_INCLUDES,
    ]
    data = musicbrainzngs.get_release_group_by_id(
        release_group_mbid,
        includes=includes,
    )
    return data.get("release-group") or {}


@st.cache_data(ttl=MB_CACHE_TTL_SECONDS, show_spinner=False)
def mb_browse_release_group_releases(
    release_group_mbid,
    limit=MB_BROWSE_DEFAULT_LIMIT,
    offset=0,
):
    """Return one cached page of editions belonging to a Release Group."""
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
    """
    Standalone Work lookup with aliases, annotation, Work attributes and all
    supported relationships, including creators and linked recordings.

    ``mb_get_work`` remains intentionally lightweight for the established
    Recording → Work resolver; this full helper is used only by Phase 2.
    """
    includes = ["aliases", "annotation", *MB_RELATION_INCLUDES]
    data = musicbrainzngs.get_work_by_id(work_mbid, includes=includes)
    return data.get("work") or {}


# --------------------------------------------------------------------------
# Cached search-first calls
# --------------------------------------------------------------------------
@st.cache_data(ttl=MB_CACHE_TTL_SECONDS, show_spinner=False)
def mb_search_artists(
    query,
    limit=MB_SEARCH_DEFAULT_LIMIT,
    strict=False,
    lucene_query=False,
):
    """Search artists by name or by a raw Lucene query."""
    return _run_entity_search(
        musicbrainzngs.search_artists,
        "artist-list",
        "artist",
        query,
        limit,
        strict,
        lucene_query,
    )


@st.cache_data(ttl=MB_CACHE_TTL_SECONDS, show_spinner=False)
def mb_search_releases(
    query,
    limit=MB_SEARCH_DEFAULT_LIMIT,
    strict=False,
    lucene_query=False,
):
    """Search releases by title or by a raw Lucene query."""
    return _run_entity_search(
        musicbrainzngs.search_releases,
        "release-list",
        "release",
        query,
        limit,
        strict,
        lucene_query,
    )


@st.cache_data(ttl=MB_CACHE_TTL_SECONDS, show_spinner=False)
def mb_search_release_groups(
    query,
    limit=MB_SEARCH_DEFAULT_LIMIT,
    strict=False,
    lucene_query=False,
):
    """Search release groups by title or by a raw Lucene query."""
    return _run_entity_search(
        musicbrainzngs.search_release_groups,
        "release-group-list",
        "releasegroup",
        query,
        limit,
        strict,
        lucene_query,
    )


@st.cache_data(ttl=MB_CACHE_TTL_SECONDS, show_spinner=False)
def mb_search_recordings(
    query,
    limit=MB_SEARCH_DEFAULT_LIMIT,
    strict=False,
    lucene_query=False,
):
    """Search recordings by title or by a raw Lucene query."""
    return _run_entity_search(
        musicbrainzngs.search_recordings,
        "recording-list",
        "recording",
        query,
        limit,
        strict,
        lucene_query,
    )


@st.cache_data(ttl=MB_CACHE_TTL_SECONDS, show_spinner=False)
def mb_search_labels(
    query,
    limit=MB_SEARCH_DEFAULT_LIMIT,
    strict=False,
    lucene_query=False,
):
    """Search labels by name or by a raw Lucene query."""
    return _run_entity_search(
        musicbrainzngs.search_labels,
        "label-list",
        "label",
        query,
        limit,
        strict,
        lucene_query,
    )


@st.cache_data(ttl=MB_CACHE_TTL_SECONDS, show_spinner=False)
def mb_search_works(
    query,
    limit=MB_SEARCH_DEFAULT_LIMIT,
    strict=False,
    lucene_query=False,
):
    """Search works by title or by a raw Lucene query."""
    return _run_entity_search(
        musicbrainzngs.search_works,
        "work-list",
        "work",
        query,
        limit,
        strict,
        lucene_query,
    )


_SEARCH_DISPATCH: Dict[str, Callable] = {
    "artist": mb_search_artists,
    "release": mb_search_releases,
    "release-group": mb_search_release_groups,
    "recording": mb_search_recordings,
    "label": mb_search_labels,
    "work": mb_search_works,
}


def mb_search_entities(
    entity_type,
    query,
    limit=MB_SEARCH_DEFAULT_LIMIT,
    strict=False,
    lucene_query=False,
):
    """Dispatch a Universal Search to the correct cached helper."""
    clean_entity = str(entity_type or "").strip().lower()
    search_function = _SEARCH_DISPATCH.get(clean_entity)

    if search_function is None:
        supported = ", ".join(MB_SEARCH_ENTITY_TYPES)
        raise ValueError(
            f"Unsupported MusicBrainz entity type: {entity_type}. "
            f"Supported values: {supported}."
        )

    return search_function(
        query=query,
        limit=limit,
        strict=strict,
        lucene_query=lucene_query,
    )


# --------------------------------------------------------------------------
# User-facing error translation
# --------------------------------------------------------------------------
def mb_error_message(exc):
    """Translate musicbrainzngs exceptions into clear Greek UI messages."""
    if isinstance(exc, musicbrainzngs.ResponseError):
        cause = getattr(exc, "cause", None)
        code = getattr(cause, "code", None)

        if code == 400:
            return (
                "Το MusicBrainz απέρριψε το αίτημα (400). Ελέγξτε τη σύνταξη "
                "της αναζήτησης ή τον κωδικό που δώσατε."
            )
        if code == 401:
            return "Το MusicBrainz απέρριψε την ταυτοποίηση (401)."
        if code == 404:
            return (
                "Το MusicBrainz δεν βρήκε αυτή την εγγραφή (404). "
                "Ελέγξτε τον κωδικό."
            )
        if code == 503:
            return (
                "Το MusicBrainz επιστρέφει 503 (rate limit / υπερφόρτωση). "
                "Δοκιμάστε ξανά σε λίγο."
            )

        return f"Μη έγκυρη απάντηση από το MusicBrainz: {exc}"

    if isinstance(exc, musicbrainzngs.NetworkError):
        return f"Αδυναμία σύνδεσης με το MusicBrainz: {exc}"

    if isinstance(exc, musicbrainzngs.InvalidSearchFieldError):
        return f"Μη έγκυρο πεδίο αναζήτησης MusicBrainz: {exc}"

    if isinstance(exc, musicbrainzngs.UsageError):
        return f"Μη έγκυρο αίτημα προς το MusicBrainz: {exc}"

    if isinstance(exc, musicbrainzngs.WebServiceError):
        return f"Σφάλμα υπηρεσίας MusicBrainz: {exc}"

    return f"Σφάλμα MusicBrainz: {exc}"


# Backwards-friendly aliases for code that imports the old private helpers.
_mb_error_message = mb_error_message
_mb_artist_credit_phrase = mb_artist_credit_phrase
_mb_iswc = mb_iswc
_mb_format_length = mb_format_length
