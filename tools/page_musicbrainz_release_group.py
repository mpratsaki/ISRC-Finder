"""
tools/page_musicbrainz_release_group.py

Phase 2 — dedicated MusicBrainz Release Group / Album view.

The page supports search-first discovery and direct MBID/URL lookup, then shows
the Release Group identity and every linked regional/format Release edition
through explicit cached pagination. It never performs per-release lookups or
an automatic unbounded "fetch all" loop.
"""

import musicbrainzngs
import pandas as pd
import streamlit as st

from tools.musicbrainz_entity_ui import (
    relationship_rows,
    render_aliases,
    render_annotation,
    render_entity_search_panel,
    render_relationship_table,
    safe_text,
    secondary_types,
)
from utils.musicbrainz_api import (
    extract_mbid,
    mb_artist_credit_phrase,
    mb_browse_release_group_releases,
    mb_entity_url,
    mb_error_message,
    mb_get_release_group,
)

EDITION_PAGE_SIZE_OPTIONS = [25, 50, 100]


# --------------------------------------------------------------------------
# Handoff and state helpers
# --------------------------------------------------------------------------
def _consume_release_group_handoff():
    """Consume a one-time Universal Search handoff before widgets are built."""
    handoff = st.session_state.pop("mb_release_group_handoff", None)
    if not isinstance(handoff, dict):
        return None

    entity_type = str(handoff.get("entity_type") or "").strip().lower()
    mbid = extract_mbid(handoff.get("mbid"))
    result = handoff.get("result") if isinstance(handoff.get("result"), dict) else {}

    if entity_type != "release-group" or not mbid:
        return {
            "level": "error",
            "message": (
                "Η επιλογή από το Universal Search δεν είχε έγκυρο "
                "Release Group MBID."
            ),
        }

    st.session_state["mb_release_group_input"] = mbid
    st.session_state["mb_release_group_pending_lookup"] = mbid
    st.session_state["mb_release_group_pending_stub"] = result

    title = result.get("title") or "το επιλεγμένο album"
    return {
        "level": "success",
        "message": (
            f"Το Release Group **{title}** μεταφέρθηκε στο Album Editions view "
            "και γίνεται αυτόματο lookup."
        ),
    }


def _render_notice(notice):
    if not notice:
        return

    level = notice.get("level")
    message = notice.get("message") or ""
    if level == "success":
        st.success(message)
    elif level == "warning":
        st.warning(message)
    else:
        st.error(message)


def _reset_edition_cursor(release_group_mbid):
    page_size = st.session_state.get("mb_release_group_edition_page_size", 50)
    if page_size not in EDITION_PAGE_SIZE_OPTIONS:
        page_size = 50
    st.session_state["mb_release_group_edition_cursor"] = {
        "release_group_mbid": release_group_mbid,
        "page_size": int(page_size),
        "offsets": [0],
        "index": 0,
    }


def _get_edition_cursor(release_group_mbid, page_size):
    cursor = st.session_state.get("mb_release_group_edition_cursor")
    if not isinstance(cursor, dict):
        cursor = {}

    offsets = cursor.get("offsets")
    if not isinstance(offsets, list) or not offsets:
        offsets = [0]

    needs_reset = (
        cursor.get("release_group_mbid") != release_group_mbid
        or int(cursor.get("page_size") or 0) != int(page_size)
    )

    if needs_reset:
        cursor = {
            "release_group_mbid": release_group_mbid,
            "page_size": int(page_size),
            "offsets": [0],
            "index": 0,
        }
    else:
        cursor["offsets"] = [max(0, int(value)) for value in offsets]
        cursor["index"] = max(
            0,
            min(int(cursor.get("index") or 0), len(cursor["offsets"]) - 1),
        )

    st.session_state["mb_release_group_edition_cursor"] = cursor
    return cursor


# --------------------------------------------------------------------------
# Release edition formatting
# --------------------------------------------------------------------------
def _release_formats_list(release):
    formats = []
    for medium in release.get("medium-list") or []:
        if not isinstance(medium, dict):
            continue
        value = str(medium.get("format") or "").strip()
        if value and value not in formats:
            formats.append(value)
    return formats


def _release_formats(release):
    values = _release_formats_list(release)
    return ", ".join(values) if values else "—"


def _release_track_count(release):
    direct_count = release.get("medium-track-count")
    try:
        if direct_count is not None:
            return int(direct_count)
    except (TypeError, ValueError):
        pass

    total = 0
    found = False
    for medium in release.get("medium-list") or []:
        if not isinstance(medium, dict):
            continue

        medium_count = medium.get("track-count")
        try:
            if medium_count is not None:
                total += int(medium_count)
                found = True
                continue
        except (TypeError, ValueError):
            pass

        tracks = medium.get("track-list") or []
        if isinstance(tracks, list) and tracks:
            total += len(tracks)
            found = True

    return total if found else "—"


def _release_label_info(release):
    labels = []
    catalog_numbers = []

    for info in release.get("label-info-list") or []:
        if not isinstance(info, dict):
            continue

        label = info.get("label") or {}
        if isinstance(label, dict):
            label_name = str(label.get("name") or "").strip()
            if label_name and label_name not in labels:
                labels.append(label_name)

        catalog_number = str(info.get("catalog-number") or "").strip()
        if catalog_number and catalog_number not in catalog_numbers:
            catalog_numbers.append(catalog_number)

    return (
        ", ".join(labels) if labels else "—",
        ", ".join(catalog_numbers) if catalog_numbers else "—",
    )


def _release_secondary_types(release):
    release_group = release.get("release-group") or {}
    if not isinstance(release_group, dict):
        return "—"
    return secondary_types(release_group) or "—"


def _edition_row(release):
    release_id = str(release.get("id") or "").strip()
    label_names, catalog_numbers = _release_label_info(release)

    return {
        "Τίτλος Edition": safe_text(release.get("title")),
        "Artist Credit": mb_artist_credit_phrase(release) or "—",
        "Ημερομηνία": safe_text(release.get("date")),
        "Χώρα": safe_text(release.get("country")),
        "Status": safe_text(release.get("status")),
        "Format(s)": _release_formats(release),
        "Media": len(release.get("medium-list") or []),
        "Tracks": _release_track_count(release),
        "Packaging": safe_text(release.get("packaging")),
        "Secondary Type(s)": _release_secondary_types(release),
        "Label(s)": label_names,
        "Catalog Number(s)": catalog_numbers,
        "Barcode": safe_text(release.get("barcode")),
        "Disambiguation": safe_text(release.get("disambiguation")),
        "Release MBID": release_id or "—",
        "MusicBrainz": mb_entity_url("release", release_id),
    }


# --------------------------------------------------------------------------
# Entity rendering
# --------------------------------------------------------------------------
def _render_release_group_header(release_group, release_group_mbid):
    title = release_group.get("title") or "—"
    artist_credit = mb_artist_credit_phrase(release_group) or "—"
    disambiguation = str(release_group.get("disambiguation") or "").strip()
    secondary = secondary_types(release_group)

    st.divider()
    st.markdown(f"## 💿 {title}")
    st.caption(artist_credit)
    if disambiguation:
        st.info(f"ℹ️ {disambiguation}")

    identity_columns = st.columns(5)
    identity_columns[0].metric(
        "Primary Type",
        safe_text(
            release_group.get("primary-type") or release_group.get("type")
        ),
    )
    identity_columns[1].metric("Secondary Type(s)", secondary or "—")
    identity_columns[2].metric(
        "Πρώτη κυκλοφορία",
        safe_text(release_group.get("first-release-date")),
    )
    identity_columns[3].metric(
        "Artist Credit",
        "Καταχωρημένο" if artist_credit != "—" else "Λείπει",
    )
    identity_columns[4].metric(
        "Relationships",
        len(relationship_rows(release_group)),
    )

    st.markdown("**Release Group MBID**")
    st.code(release_group_mbid, language=None)

    action_columns = st.columns(2)
    with action_columns[0]:
        st.link_button(
            "🔗 Προβολή στο MusicBrainz",
            mb_entity_url("release-group", release_group_mbid),
            width="stretch",
        )
    with action_columns[1]:
        st.link_button(
            "✏️ Επεξεργασία Release Group",
            f"https://musicbrainz.org/release-group/{release_group_mbid}/edit",
            width="stretch",
        )


def _render_release_group_metadata(release_group):
    st.divider()
    st.markdown("### 📝 Annotation")
    render_annotation(release_group)

    st.divider()
    st.markdown("### 🔤 Εναλλακτικοί τίτλοι & Aliases")
    render_aliases(
        release_group,
        "Δεν έχουν καταχωρηθεί aliases για αυτό το Release Group.",
    )

    st.divider()
    st.markdown("### 🔗 Relationships")
    rows = relationship_rows(release_group)

    relation_columns = st.columns(4)
    relation_columns[0].metric("Σύνολο", len(rows))
    relation_columns[1].metric(
        "Artist",
        len(release_group.get("artist-relation-list") or []),
    )
    relation_columns[2].metric(
        "Series",
        len(release_group.get("series-relation-list") or []),
    )
    relation_columns[3].metric(
        "URLs",
        len(release_group.get("url-relation-list") or []),
    )

    render_relationship_table(
        rows,
        "Δεν υπάρχουν καταχωρημένα relationships για αυτό το Release Group.",
    )


def _render_release_group_editions(release_group_mbid):
    st.divider()
    st.markdown("### 🌍 Regional / Format Editions")
    st.caption(
        "Κάθε row είναι ξεχωριστό Release μέσα στο ίδιο Release Group. "
        "Οι editions φορτώνονται με explicit pagination: μία cached browse "
        "κλήση ανά σελίδα, χωρίς per-release lookups ή αυτόματο fetch-all loop."
    )

    page_size = st.selectbox(
        "Editions ανά σελίδα",
        options=EDITION_PAGE_SIZE_OPTIONS,
        index=EDITION_PAGE_SIZE_OPTIONS.index(
            st.session_state.get("mb_release_group_edition_page_size", 50)
            if st.session_state.get("mb_release_group_edition_page_size", 50)
            in EDITION_PAGE_SIZE_OPTIONS
            else 50
        ),
        key="mb_release_group_edition_page_size",
    )

    cursor = _get_edition_cursor(release_group_mbid, page_size)
    offset = cursor["offsets"][cursor["index"]]

    try:
        with st.spinner("Φόρτωση Release Group editions..."):
            page = mb_browse_release_group_releases(
                release_group_mbid,
                limit=page_size,
                offset=offset,
            )
    except musicbrainzngs.MusicBrainzError as exc:
        st.error(mb_error_message(exc))
        return
    except Exception as exc:
        st.error(f"Μη αναμενόμενο σφάλμα κατά το browse editions: {exc}")
        return

    editions = [
        release
        for release in page.get("release-list") or []
        if isinstance(release, dict)
    ]
    total_count = int(page.get("release-count") or len(editions))
    start = offset + 1 if editions else 0
    end = offset + len(editions)

    countries = sorted(
        {
            str(release.get("country") or "").strip()
            for release in editions
            if str(release.get("country") or "").strip()
        }
    )
    formats = sorted(
        {
            value
            for release in editions
            for value in _release_formats_list(release)
        }
    )

    summary_columns = st.columns(4)
    summary_columns[0].metric("Σύνολο editions", total_count)
    summary_columns[1].metric(
        "Τρέχον εύρος",
        f"{start}–{end}" if editions else "0",
    )
    summary_columns[2].metric("Χώρες στη σελίδα", len(countries))
    summary_columns[3].metric("Formats στη σελίδα", len(formats))

    filter_columns = st.columns(3)
    with filter_columns[0]:
        text_filter = st.text_input(
            "Φίλτρο κειμένου",
            placeholder="title, label, catalog number ή barcode",
            key=f"mb_release_group_text_filter_{release_group_mbid}_{offset}",
        )
    with filter_columns[1]:
        country_filter = st.multiselect(
            "Χώρα",
            options=countries,
            key=f"mb_release_group_country_filter_{release_group_mbid}_{offset}",
        )
    with filter_columns[2]:
        format_filter = st.multiselect(
            "Format",
            options=formats,
            key=f"mb_release_group_format_filter_{release_group_mbid}_{offset}",
        )

    rows = [_edition_row(release) for release in editions]
    clean_text_filter = str(text_filter or "").strip().casefold()

    if clean_text_filter:
        searchable_columns = [
            "Τίτλος Edition",
            "Artist Credit",
            "Label(s)",
            "Catalog Number(s)",
            "Barcode",
            "Disambiguation",
        ]
        rows = [
            row
            for row in rows
            if clean_text_filter
            in " | ".join(
                str(row.get(column) or "") for column in searchable_columns
            ).casefold()
        ]

    if country_filter:
        selected_countries = set(country_filter)
        rows = [row for row in rows if row.get("Χώρα") in selected_countries]

    if format_filter:
        selected_formats = set(format_filter)
        rows = [
            row
            for row in rows
            if selected_formats.intersection(
                {
                    value.strip()
                    for value in str(row.get("Format(s)") or "").split(",")
                    if value.strip()
                }
            )
        ]

    if rows:
        st.dataframe(
            pd.DataFrame(rows),
            width="stretch",
            hide_index=True,
            column_config={
                "MusicBrainz": st.column_config.LinkColumn(
                    "MusicBrainz",
                    display_text="Άνοιγμα 🔗",
                ),
            },
        )
    elif editions:
        st.warning("Τα φίλτρα δεν ταιριάζουν με edition της τρέχουσας σελίδας.")
    else:
        st.warning("Δεν βρέθηκαν Release editions για αυτό το Release Group.")

    next_offset = offset + len(editions)
    has_previous = cursor["index"] > 0
    has_next = bool(editions) and next_offset < total_count

    navigation_columns = st.columns(2)
    with navigation_columns[0]:
        previous_clicked = st.button(
            "⬅️ Προηγούμενη σελίδα",
            width="stretch",
            disabled=not has_previous,
            key=(
                f"mb_release_group_prev_{release_group_mbid}_"
                f"{cursor['index']}_{page_size}"
            ),
        )
    with navigation_columns[1]:
        next_clicked = st.button(
            "Επόμενη σελίδα ➡️",
            width="stretch",
            disabled=not has_next,
            key=(
                f"mb_release_group_next_{release_group_mbid}_"
                f"{cursor['index']}_{page_size}"
            ),
        )

    if previous_clicked:
        cursor["index"] -= 1
        st.session_state["mb_release_group_edition_cursor"] = cursor
        st.rerun()

    if next_clicked:
        cursor["offsets"] = cursor["offsets"][: cursor["index"] + 1]
        cursor["offsets"].append(next_offset)
        cursor["index"] += 1
        st.session_state["mb_release_group_edition_cursor"] = cursor
        st.rerun()


# --------------------------------------------------------------------------
# Page
# --------------------------------------------------------------------------
def page_musicbrainz_release_group():
    notice = _consume_release_group_handoff()

    st.title("💿 MusicBrainz Album / Release Group Editions")
    st.caption(
        "Album-level lookup και χαρτογράφηση όλων των regional, format, label "
        "και catalog-number editions που ανήκουν στο ίδιο Release Group."
    )
    st.info(
        "Το identity lookup και κάθε edition page αποθηκεύονται προσωρινά για "
        "1 ώρα. Δεν χρησιμοποιείται concurrency ή αυτόματο fetch-all loop."
    )
    _render_notice(notice)

    with st.container(border=True):
        st.markdown("### 🆔 Lookup με Release Group MBID")
        with st.form("mb_release_group_lookup_form", clear_on_submit=False):
            release_group_input = st.text_input(
                "MusicBrainz Release Group ID ή URL",
                placeholder=(
                    "https://musicbrainz.org/release-group/"
                    "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
                ),
                key="mb_release_group_input",
            )
            direct_submitted = st.form_submit_button(
                "Προβολή Album Editions",
                type="primary",
                width="stretch",
            )

    with st.expander(
        "🔎 Δεν έχετε Release Group MBID; Αναζητήστε album",
        expanded=True,
    ):
        selected = render_entity_search_panel(
            entity_type="release-group",
            state_key="mb_release_group_search_state",
            key_prefix="mb_release_group_search",
            query_label="Τίτλος album / release group",
            placeholder=(
                "π.χ. Abbey Road, Kind of Blue, The Dark Side of the Moon"
            ),
            action_label="➡️ Άνοιγμα στις Album Editions",
            default_limit=10,
        )

    lookup_mbid = extract_mbid(
        st.session_state.pop("mb_release_group_pending_lookup", None)
    )
    pending_stub = st.session_state.pop("mb_release_group_pending_stub", None)

    if direct_submitted:
        lookup_mbid = extract_mbid(release_group_input)
        pending_stub = None
        if not lookup_mbid:
            st.error(
                "Δεν βρέθηκε έγκυρο Release Group MBID. Χρειάζεται UUID της "
                "μορφής `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`."
            )

    if isinstance(selected, dict):
        selected_mbid = extract_mbid(selected.get("id"))
        if selected_mbid:
            lookup_mbid = selected_mbid
            pending_stub = selected
        else:
            st.error("Το επιλεγμένο Release Group δεν είχε έγκυρο MBID.")

    if lookup_mbid:
        try:
            with st.spinner("Fetching Release Group from MusicBrainz..."):
                release_group = mb_get_release_group(lookup_mbid)
        except musicbrainzngs.MusicBrainzError as exc:
            st.session_state.pop("mb_release_group_view_state", None)
            st.error(mb_error_message(exc))
            release_group = None
        except Exception as exc:
            st.session_state.pop("mb_release_group_view_state", None)
            st.error(f"Μη αναμενόμενο σφάλμα: {exc}")
            release_group = None

        if release_group:
            st.session_state["mb_release_group_view_state"] = {
                "mbid": lookup_mbid,
                "release_group": release_group,
                "source_stub": (
                    pending_stub if isinstance(pending_stub, dict) else {}
                ),
            }
            _reset_edition_cursor(lookup_mbid)

    state = st.session_state.get("mb_release_group_view_state")
    if not isinstance(state, dict):
        return

    release_group = state.get("release_group")
    release_group_mbid = extract_mbid(state.get("mbid"))

    if not isinstance(release_group, dict) or not release_group_mbid:
        st.session_state.pop("mb_release_group_view_state", None)
        st.error("Το αποθηκευμένο Release Group αποτέλεσμα δεν είχε έγκυρη δομή.")
        return

    _render_release_group_header(release_group, release_group_mbid)
    _render_release_group_metadata(release_group)
    _render_release_group_editions(release_group_mbid)
