"""
tools/page_catalog_batch.py

Streamlit page for the Stay Independent "Batch Catalog Label Copy" workflow.

Given a Spotify Artist URL/URI, this page fetches the artist's entire own
catalog (Albums / Singles / EPs -- excluding "appears on" credits on other
artists' releases), lets the user pick which releases to process, and then
generates the official Stay_-_Label_Copy_Template.docx for every selected
release, packaged into a single downloadable .zip file.

This page is intentionally a thin orchestration layer. All the heavy lifting
is delegated to the existing, already-hardened data layer:

    - utils.label_copy_engine.build_label_copy_data
    - utils.docx_engine.generate_label_copy_docx / make_label_copy_filename
    - utils.github_fetcher.get_label_copy_template_config /
      fetch_private_label_copy_template_bytes

The Spotify HTTP plumbing (auth headers, retries, rate-limit backoff,
response caching) is *not* re-implemented here. `_spotify_get_json` and
`_fetch_spotify_album_bundle` are imported straight from
tools.page_label_copy, which already owns the single, tested implementation
of that layer. This keeps behavior identical between the "single release"
and "batch" pages, and means any future Spotify quirk fix only has to
happen in one place.

To keep the batch fast, MusicBrainz/iTunes/Tidal enrichment fetchers are
intentionally left unset when calling `build_label_copy_data` -- each
release is built from Spotify data alone, exactly as in the single-release
page when those integrations are unavailable.
"""

from __future__ import annotations

import io
import re
import unicodedata
import zipfile
from collections.abc import Mapping
from typing import Any

import pandas as pd
import streamlit as st

from utils.docx_engine import generate_label_copy_docx, make_label_copy_filename
from utils.github_fetcher import (
    fetch_private_label_copy_template_bytes,
    get_label_copy_template_config,
)
from utils.label_copy_engine import build_label_copy_data

# Reuse the existing, hardened Spotify data layer instead of duplicating it.
from tools.page_label_copy import (
    SPOTIFY_API_BASE,
    _fetch_spotify_album_bundle,
    _spotify_get_json,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SPOTIFY_ARTIST_ID_RE = re.compile(r"^[A-Za-z0-9]{22}$")
SPOTIFY_ARTIST_URI_RE = re.compile(r"(?i)^spotify:artist:([A-Za-z0-9]{22})$")
SPOTIFY_ARTIST_URL_RE = re.compile(
    r"(?i)^https?://open\.spotify\.com/(?:intl-[a-z]{2}/)?artist/"
    r"([A-Za-z0-9]{22})(?:[/?#].*)?$"
)

# Everything except "appears_on" -- that's the whole point of this page.
RELEASE_GROUPS = "album,single,compilation"

SESSION_ARTIST_KEY = "catalog_batch_artist"
SESSION_RELEASES_KEY = "catalog_batch_releases"
SESSION_ZIP_KEY = "catalog_batch_zip"


# ---------------------------------------------------------------------------
# Small text / parsing helpers
# (kept local + private, matching this codebase's per-module convention
# rather than a shared utils module -- see page_label_copy.py, page_catalog.py)
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


def _as_int(value: Any, default: int = 0) -> int:
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_filename_component(value: Any, fallback: str = "Artist") -> str:
    normalized = unicodedata.normalize("NFKD", _clean_text(value))
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    ascii_text = re.sub(r"[^A-Za-z0-9]+", "_", ascii_text).strip("_")
    return ascii_text or fallback


def _parse_artist_input(value: Any) -> tuple[str | None, str | None]:
    """Parses a Spotify artist link/URI/ID. Returns (artist_id, error_note)."""
    text = _clean_text(value)
    if not text:
        return None, "Εισαγάγετε σύνδεσμο ή URI καλλιτέχνη Spotify."

    uri_match = SPOTIFY_ARTIST_URI_RE.fullmatch(text)
    if uri_match:
        return uri_match.group(1), None

    url_match = SPOTIFY_ARTIST_URL_RE.fullmatch(text)
    if url_match:
        return url_match.group(1), None

    if SPOTIFY_ARTIST_ID_RE.fullmatch(text):
        return text, None

    return (
        None,
        "Ο σύνδεσμος δεν αναγνωρίστηκε. Χρησιμοποιήστε Spotify artist URL "
        "(open.spotify.com/artist/...), spotify:artist:ID, ή artist ID 22 χαρακτήρων.",
    )


# ---------------------------------------------------------------------------
# Spotify data fetching (artist + catalog listing)
# ---------------------------------------------------------------------------

def _fetch_artist(token: str, artist_id: str) -> tuple[dict[str, Any] | None, str | None]:
    return _spotify_get_json(token, f"{SPOTIFY_API_BASE}/artists/{artist_id}")


def _display_type(item: Mapping[str, Any]) -> str:
    album_type = _clean_text(item.get("album_type")).lower()
    total_tracks = _as_int(item.get("total_tracks"))
    if album_type == "single" and total_tracks >= 4:
        # Spotify only distinguishes album/single/compilation; anything sold
        # as a "single" with 4+ tracks reads as an EP to most catalog teams.
        return "EP"
    if album_type == "single":
        return "Single"
    if album_type == "compilation":
        return "Compilation"
    return "Album"


def _fetch_artist_releases(
    token: str,
    artist_id: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    """
    Fetches every release owned by the artist (Album/Single/EP), excluding
    "appears_on" credits, paginating through the full result set, and
    deduplicating repeat entries (regional re-releases, deluxe re-uploads,
    etc.) by a normalized Name + Type key.

    Note on UPC dedup: the /artists/{id}/albums listing endpoint returns
    SimplifiedAlbumObjects, which do not include external_ids (UPC).
    Fetching full album details for every candidate just to dedupe on UPC
    would mean one extra Spotify round-trip per release before the user has
    even picked anything -- defeating the purpose of a fast catalog listing.
    We dedupe on normalized (name + album_type) instead, keeping the edition
    with the most tracks as the representative one; true UPC-level
    resolution still happens per-release later, via build_label_copy_data,
    for whichever releases the user actually selects.
    """
    notes: list[str] = []
    raw_items: list[dict[str, Any]] = []

    url = f"{SPOTIFY_API_BASE}/artists/{artist_id}/albums"
    # NOTE: as of the post-Feb-2026 Spotify API changes referenced elsewhere
    # in this app (see app.py docstring), this endpoint now rejects
    # limit=50 with a "400 Invalid limit" response. 20 is Spotify's own
    # documented default for this endpoint and is confirmed safe -- we just
    # let pagination (via the "next" link, below) do the rest of the work.
    params: Mapping[str, Any] | None = {
        "include_groups": RELEASE_GROUPS,
        "limit": 20,
        "offset": 0,
    }
    while url:
        page, note = _spotify_get_json(token, url, params)
        if note and note not in notes:
            # --- TEMP DIAGNOSTIC --------------------------------------
            # `_spotify_get_json` swallows the response body; fire one raw,
            # uncached request so we can see Spotify's actual error.message.
            try:
                import requests as _debug_requests
                debug_resp = _debug_requests.get(
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                    params=dict(params or {}),
                    timeout=(5, 15),
                )
                notes.append(
                    f"[debug] HTTP {debug_resp.status_code} for {debug_resp.url} "
                    f"-> {debug_resp.text[:400]}"
                )
            except Exception as debug_exc:
                notes.append(f"[debug] raw request failed: {debug_exc}")
            # --- END TEMP DIAGNOSTIC -----------------------------------
            notes.append(note)
        if not page:
            break
        for item in page.get("items", []):
            if isinstance(item, Mapping):
                raw_items.append(dict(item))
        url = _clean_text(page.get("next"))
        params = None

    # Defensive filter: include_groups already excludes "appears_on", but we
    # never want a compilation credit slipping into the artist's own batch.
    own_releases = [
        item for item in raw_items
        if _clean_text(item.get("album_group")) != "appears_on"
    ]

    deduped: dict[str, dict[str, Any]] = {}
    for item in own_releases:
        key = f"{_comparison_key(item.get('name'))}|{_clean_text(item.get('album_type')).lower()}"
        existing = deduped.get(key)
        if existing is None or _as_int(item.get("total_tracks")) > _as_int(existing.get("total_tracks")):
            deduped[key] = item

    releases = list(deduped.values())
    releases.sort(key=lambda item: _clean_text(item.get("release_date")), reverse=True)
    return releases, notes


# ---------------------------------------------------------------------------
# Selection table
# ---------------------------------------------------------------------------

def _releases_dataframe(releases: list[dict[str, Any]]) -> pd.DataFrame:
    rows = [
        {
            "Select": True,
            "Release Name": _clean_text(item.get("name")) or "(Χωρίς τίτλο)",
            "Type": _display_type(item),
            "Release Date": _clean_text(item.get("release_date")) or "—",
            "Total Tracks": _as_int(item.get("total_tracks")),
            "release_id": _clean_text(item.get("id")),
        }
        for item in releases
    ]
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Batch generation
# ---------------------------------------------------------------------------

def _generate_batch_zip(
    token: str,
    selected_items: list[dict[str, Any]],
    *,
    status: Any,
    progress_bar: Any,
) -> tuple[bytes, list[str], list[str]]:
    """
    Iterates over the selected releases, builds a LabelCopyData for each from
    Spotify data alone, renders it against the official template, and writes
    the result into an in-memory .zip. A failure on one release is logged
    and skipped rather than aborting the whole batch.

    Returns (zip_bytes, successful_release_names, failure_notes).
    """
    # Fetch the DOCX template bytes ONCE, outside the per-release loop.
    template_config = get_label_copy_template_config()
    template_bytes = fetch_private_label_copy_template_bytes(**template_config)

    zip_buffer = io.BytesIO()
    successful: list[str] = []
    failures: list[str] = []
    used_filenames: set[str] = set()

    total = len(selected_items)
    with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zip_file:
        for index, item in enumerate(selected_items, start=1):
            release_name = _clean_text(item.get("name")) or f"Release {index}"
            release_id = _clean_text(item.get("id"))

            status.update(label=f"Επεξεργασία {index}/{total}: {release_name}")
            status.write(f"🎵 **{release_name}** — ανάκτηση από Spotify...")

            if not release_id:
                failures.append(f"{release_name}: λείπει το Spotify release ID.")
                status.write(f"❌ {release_name}: λείπει το release ID.")
                progress_bar.progress(index / max(total, 1))
                continue

            try:
                spotify_release, spotify_tracks, fetch_notes = _fetch_spotify_album_bundle(
                    token, release_id
                )
                if not spotify_release or not spotify_tracks:
                    detail = "; ".join(fetch_notes) or "άγνωστο σφάλμα ανάκτησης."
                    failures.append(f"{release_name}: {detail}")
                    status.write(f"❌ {release_name}: {detail}")
                    progress_bar.progress(index / max(total, 1))
                    continue

                # Skip MusicBrainz/iTunes/Tidal enrichment on purpose here --
                # Spotify-only data keeps a full-catalog batch fast.
                data = build_label_copy_data(
                    spotify_release,
                    spotify_tracks,
                    ensure_single_release=True,
                )

                docx_buffer = generate_label_copy_docx(template_bytes, data)

                filename = make_label_copy_filename(
                    data.get("project_name"),
                    extension="docx",
                    issue_date=data.get("issue_date"),
                )
                # Guard against two releases producing an identical filename
                # (e.g. duplicate titles generated on the same day).
                final_filename = filename
                suffix = 2
                while final_filename in used_filenames:
                    stem = filename.rsplit(".", 1)[0]
                    final_filename = f"{stem}_{suffix}.docx"
                    suffix += 1
                used_filenames.add(final_filename)

                zip_file.writestr(final_filename, docx_buffer.getvalue())
                successful.append(release_name)
                status.write(f"✅ **{release_name}** → `{final_filename}`")
            except Exception as exc:
                failures.append(f"{release_name}: {exc}")
                status.write(f"❌ {release_name}: {exc}")

            progress_bar.progress(index / max(total, 1))

    return zip_buffer.getvalue(), successful, failures


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

def page_catalog_batch(token: str, spotify_user: str) -> None:
    st.title("Batch Catalog Label Copy")
    st.caption(
        "Εισαγάγετε έναν σύνδεσμο καλλιτέχνη Spotify για να δημιουργήσετε αυτόματα "
        "τα επίσημα Label Copy (.docx) για όλη την κατηγορία κυκλοφοριών του, "
        "συσκευασμένα σε ένα .zip."
    )

    st.markdown("### Καλλιτέχνης")
    artist_input = st.text_input(
        "Σύνδεσμος / URI Καλλιτέχνη Spotify",
        placeholder="https://open.spotify.com/artist/... ή spotify:artist:...",
        key="catalog_batch_artist_input",
    )
    artist_id, parse_note = _parse_artist_input(artist_input)
    if artist_input and parse_note:
        st.caption(parse_note)

    fetch_trigger = st.button(
        "Ανάκτηση Κατηγορίας",
        type="primary",
        width="stretch",
        disabled=artist_id is None,
        key="catalog_batch_fetch_button",
    )

    if fetch_trigger and artist_id:
        with st.spinner("Ανάκτηση καλλιτέχνη και κυκλοφοριών από το Spotify..."):
            artist, artist_note = _fetch_artist(token, artist_id)
            releases, fetch_notes = ([], [])
            if artist:
                releases, fetch_notes = _fetch_artist_releases(token, artist_id)

        if not artist:
            st.error(f"Δεν ήταν δυνατή η ανάκτηση καλλιτέχνη: {artist_note}")
        elif not releases:
            st.warning(
                "Δεν βρέθηκαν δικές του/της κυκλοφορίες (Album/Single/EP) για αυτόν τον καλλιτέχνη."
            )
            for note in fetch_notes:
                st.caption(f"⚠️ {note}")
        else:
            st.session_state[SESSION_ARTIST_KEY] = {
                "id": artist_id,
                "name": _clean_text(artist.get("name")) or "Artist",
            }
            st.session_state[SESSION_RELEASES_KEY] = releases
            # A fresh catalog fetch invalidates any previously generated ZIP.
            st.session_state.pop(SESSION_ZIP_KEY, None)
            st.toast(f"Βρέθηκαν {len(releases)} μοναδικές κυκλοφορίες.", icon="✅")
            for note in fetch_notes:
                st.caption(f"⚠️ {note}")

    artist_state = st.session_state.get(SESSION_ARTIST_KEY)
    releases = st.session_state.get(SESSION_RELEASES_KEY)
    if not artist_state or not releases:
        return

    st.divider()
    st.markdown(f"### Κατάλογος: {artist_state['name']}")
    st.caption(
        f"{len(releases)} μοναδικές κυκλοφορίες (Albums/Singles/EPs). "
        "Επιλέξτε ποιες θέλετε να παραχθούν."
    )

    releases_df = _releases_dataframe(releases)
    edited_df = st.data_editor(
        releases_df,
        hide_index=True,
        width="stretch",
        key=f"catalog_batch_editor_{artist_state['id']}",
        column_order=["Select", "Release Name", "Type", "Release Date", "Total Tracks"],
        column_config={
            "Select": st.column_config.CheckboxColumn("Επιλογή", default=True),
            "Release Name": st.column_config.TextColumn("Τίτλος Κυκλοφορίας", disabled=True),
            "Type": st.column_config.TextColumn("Τύπος", disabled=True),
            "Release Date": st.column_config.TextColumn("Ημερομηνία Κυκλοφορίας", disabled=True),
            "Total Tracks": st.column_config.NumberColumn("Tracks", disabled=True),
        },
    )

    selected_ids = set(edited_df.loc[edited_df["Select"].astype(bool), "release_id"])
    selected_items = [item for item in releases if _clean_text(item.get("id")) in selected_ids]

    st.caption(f"Επιλεγμένες κυκλοφορίες: **{len(selected_items)} / {len(releases)}**")

    generate_trigger = st.button(
        "Generate Label Copies",
        type="primary",
        width="stretch",
        disabled=not selected_items,
        key="catalog_batch_generate_button",
    )

    if generate_trigger and selected_items:
        st.divider()
        st.markdown("#### Ζωντανή Δραστηριότητα")
        progress_bar = st.progress(0.0)
        with st.status(f"Δημιουργία {len(selected_items)} Label Copies...", expanded=True) as status:
            try:
                zip_bytes, successful, failures = _generate_batch_zip(
                    token,
                    selected_items,
                    status=status,
                    progress_bar=progress_bar,
                )
                if successful:
                    status.update(
                        label=f"Ολοκληρώθηκε: {len(successful)} επιτυχή, {len(failures)} αποτυχίες.",
                        state="complete" if not failures else "error",
                    )
                else:
                    status.update(label="Αποτυχία: καμία κυκλοφορία δεν παρήχθη.", state="error")
            except Exception as exc:
                status.update(label=f"Αποτυχία δημιουργίας batch: {exc}", state="error")
                st.error(f"Μη αναμενόμενο σφάλμα κατά τη δημιουργία του batch: {exc}")
                return

        st.session_state[SESSION_ZIP_KEY] = {
            "bytes": zip_bytes,
            "filename": f"{_safe_filename_component(artist_state['name'])}_LabelCopies.zip",
            "successful": successful,
            "failures": failures,
        }
        st.toast("Η παραγωγή batch ολοκληρώθηκε.", icon="✅")

    batch_result = st.session_state.get(SESSION_ZIP_KEY)
    if not batch_result:
        return

    st.divider()
    st.markdown("### 📦 Αποτελέσματα & Εξαγωγή")
    m1, m2 = st.columns(2)
    m1.metric("Επιτυχή Label Copies", len(batch_result["successful"]))
    m2.metric("Αποτυχίες", len(batch_result["failures"]))

    st.download_button(
        label=f"⬇️ Λήψη ZIP ({batch_result['filename']})",
        data=batch_result["bytes"],
        file_name=batch_result["filename"],
        mime="application/zip",
        width="stretch",
        type="primary",
        key="catalog_batch_zip_download",
    )

    if batch_result["failures"]:
        with st.expander(f"Σφάλματα ({len(batch_result['failures'])})", expanded=False):
            for note in batch_result["failures"]:
                st.write(f"• {note}")


__all__ = ["page_catalog_batch"]
