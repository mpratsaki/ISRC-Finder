"""
tools/page_musicbrainz_label.py

Phase 2 — dedicated MusicBrainz Label Auditor.

The page supports direct Label MBID/URL lookup and search-first discovery. It
shows the label code, identity data, aliases, annotation, all requested
relationship target types and a cached paginated browse of the label's
releases. Network calls remain in utils/musicbrainz_api.py.
"""

import musicbrainzngs
import pandas as pd
import streamlit as st

from tools.musicbrainz_entity_ui import (
    area_name,
    life_span_text,
    relationship_rows,
    render_aliases,
    render_annotation,
    render_entity_search_panel,
    render_relationship_table,
    safe_text,
)
from utils.musicbrainz_api import (
    extract_mbid,
    mb_artist_credit_phrase,
    mb_browse_label_releases,
    mb_entity_url,
    mb_error_message,
    mb_get_label,
)

LABEL_PAGE_SIZE_OPTIONS = [25, 50, 100]


# --------------------------------------------------------------------------
# Handoff and formatting helpers
# --------------------------------------------------------------------------
def _consume_label_handoff():
    """Consume a one-time Universal Search handoff before widgets are built."""
    handoff = st.session_state.pop("mb_label_handoff", None)
    if not isinstance(handoff, dict):
        return None

    entity_type = str(handoff.get("entity_type") or "").strip().lower()
    mbid = extract_mbid(handoff.get("mbid"))
    result = handoff.get("result") if isinstance(handoff.get("result"), dict) else {}

    if entity_type != "label" or not mbid:
        return {
            "level": "error",
            "message": "Η επιλογή από το Universal Search δεν είχε έγκυρο Label MBID.",
        }

    st.session_state["mb_label_input"] = mbid
    st.session_state["mb_label_pending_lookup"] = mbid
    st.session_state["mb_label_pending_stub"] = result

    title = result.get("name") or "η επιλεγμένη δισκογραφική"
    return {
        "level": "success",
        "message": (
            f"Η δισκογραφική **{title}** μεταφέρθηκε στο Label Auditor "
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


def _label_code(value):
    text = str(value or "").strip()
    if not text:
        return "—"
    if text.upper().startswith("LC"):
        return text
    return f"LC {text}"


def _release_formats(release):
    formats = []
    for medium in release.get("medium-list") or []:
        if not isinstance(medium, dict):
            continue
        value = str(medium.get("format") or "").strip()
        if value and value not in formats:
            formats.append(value)
    return ", ".join(formats) if formats else "—"


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
            name = str(label.get("name") or "").strip()
            if name and name not in labels:
                labels.append(name)
        catalog_number = str(info.get("catalog-number") or "").strip()
        if catalog_number and catalog_number not in catalog_numbers:
            catalog_numbers.append(catalog_number)

    return (
        ", ".join(labels) if labels else "—",
        ", ".join(catalog_numbers) if catalog_numbers else "—",
    )


def _release_row(release):
    release_id = str(release.get("id") or "").strip()
    label_names, catalog_numbers = _release_label_info(release)
    release_group = release.get("release-group") or {}
    primary_type = "—"
    if isinstance(release_group, dict):
        primary_type = safe_text(
            release_group.get("primary-type") or release_group.get("type")
        )

    return {
        "Τίτλος": safe_text(release.get("title")),
        "Artist Credit": mb_artist_credit_phrase(release) or "—",
        "Ημερομηνία": safe_text(release.get("date")),
        "Χώρα": safe_text(release.get("country")),
        "Status": safe_text(release.get("status")),
        "Τύπος": primary_type,
        "Format(s)": _release_formats(release),
        "Media": len(release.get("medium-list") or []),
        "Tracks": _release_track_count(release),
        "Packaging": safe_text(release.get("packaging")),
        "Label(s)": label_names,
        "Catalog Number(s)": catalog_numbers,
        "Barcode": safe_text(release.get("barcode")),
        "MBID": release_id or "—",
        "MusicBrainz": mb_entity_url("release", release_id),
    }


def _reset_release_cursor(label_mbid):
    page_size = st.session_state.get("mb_label_release_page_size", 50)
    if page_size not in LABEL_PAGE_SIZE_OPTIONS:
        page_size = 50
    st.session_state["mb_label_release_cursor"] = {
        "label_mbid": label_mbid,
        "page_size": int(page_size),
        "offsets": [0],
        "index": 0,
    }


def _get_release_cursor(label_mbid, page_size):
    cursor = st.session_state.get("mb_label_release_cursor")
    if not isinstance(cursor, dict):
        cursor = {}

    offsets = cursor.get("offsets")
    if not isinstance(offsets, list) or not offsets:
        offsets = [0]

    needs_reset = (
        cursor.get("label_mbid") != label_mbid
        or int(cursor.get("page_size") or 0) != int(page_size)
    )
    if needs_reset:
        cursor = {
            "label_mbid": label_mbid,
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

    st.session_state["mb_label_release_cursor"] = cursor
    return cursor


# --------------------------------------------------------------------------
# Entity rendering
# --------------------------------------------------------------------------
def _render_label_header(label, label_mbid):
    name = label.get("name") or "—"
    disambiguation = str(label.get("disambiguation") or "").strip()
    label_code = _label_code(label.get("label-code"))

    st.divider()
    st.markdown(f"## 🏷️ {name}")
    if disambiguation:
        st.caption(disambiguation)

    identity_columns = st.columns(5)
    identity_columns[0].metric("Τύπος", safe_text(label.get("type")))
    identity_columns[1].metric("Label Code", label_code)
    identity_columns[2].metric("Χώρα / Περιοχή", area_name(label))
    identity_columns[3].metric("Περίοδος", life_span_text(label))
    identity_columns[4].metric("IPI", safe_text(label.get("ipi")))

    details_columns = st.columns(2)
    with details_columns[0]:
        st.markdown("**Sort Name**")
        st.write(safe_text(label.get("sort-name")))
    with details_columns[1]:
        ipi_values = label.get("ipi-list") or []
        st.markdown("**IPI list**")
        st.write(", ".join(str(value) for value in ipi_values) if ipi_values else "—")

    if label_code == "—":
        st.warning(
            "⚠️ Δεν έχει καταχωρηθεί Label Code. Αυτό δεν εμποδίζει την ύπαρξη "
            "της δισκογραφικής στο MusicBrainz, αλλά αποτελεί κενό ταυτοποίησης."
        )

    link_columns = st.columns(2)
    with link_columns[0]:
        st.link_button(
            "🔗 Προβολή στο MusicBrainz",
            mb_entity_url("label", label_mbid),
            width="stretch",
        )
    with link_columns[1]:
        st.link_button(
            "✏️ Επεξεργασία Label",
            f"https://musicbrainz.org/label/{label_mbid}/edit",
            width="stretch",
        )


def _render_label_metadata(label):
    st.divider()
    st.markdown("### 📝 Annotation")
    render_annotation(label)

    st.divider()
    st.markdown("### 🔤 Ονόματα & Aliases")
    render_aliases(
        label,
        "Δεν έχουν καταχωρηθεί aliases για αυτή τη δισκογραφική.",
    )

    st.divider()
    st.markdown("### 🔗 Relationships")
    rows = relationship_rows(label)

    relation_columns = st.columns(4)
    relation_columns[0].metric("Σύνολο", len(rows))
    relation_columns[1].metric(
        "Label",
        len(label.get("label-relation-list") or []),
    )
    relation_columns[2].metric(
        "Artist",
        len(label.get("artist-relation-list") or []),
    )
    relation_columns[3].metric(
        "URLs",
        len(label.get("url-relation-list") or []),
    )

    render_relationship_table(
        rows,
        "Δεν υπάρχουν καταχωρημένα relationships για αυτή τη δισκογραφική.",
    )


def _render_label_releases(label_mbid):
    st.divider()
    st.markdown("### 💿 Κυκλοφορίες της δισκογραφικής")
    st.caption(
        "Τα releases φορτώνονται με MusicBrainz browse pagination. Κάθε νέα "
        "σελίδα είναι μία ξεχωριστή cached κλήση· δεν εκτελούνται per-release lookups."
    )

    page_size = st.selectbox(
        "Releases ανά σελίδα",
        options=LABEL_PAGE_SIZE_OPTIONS,
        index=LABEL_PAGE_SIZE_OPTIONS.index(
            st.session_state.get("mb_label_release_page_size", 50)
            if st.session_state.get("mb_label_release_page_size", 50)
            in LABEL_PAGE_SIZE_OPTIONS
            else 50
        ),
        key="mb_label_release_page_size",
    )
    cursor = _get_release_cursor(label_mbid, page_size)
    offset = cursor["offsets"][cursor["index"]]

    try:
        with st.spinner("Φόρτωση releases της δισκογραφικής..."):
            page = mb_browse_label_releases(
                label_mbid,
                limit=page_size,
                offset=offset,
            )
    except musicbrainzngs.MusicBrainzError as exc:
        st.error(mb_error_message(exc))
        return
    except Exception as exc:
        st.error(f"Μη αναμενόμενο σφάλμα κατά το browse releases: {exc}")
        return

    releases = [
        release
        for release in page.get("release-list") or []
        if isinstance(release, dict)
    ]
    total_count = int(page.get("release-count") or len(releases))
    start = offset + 1 if releases else 0
    end = offset + len(releases)

    summary_columns = st.columns(3)
    summary_columns[0].metric("Σύνολο releases", total_count)
    summary_columns[1].metric("Τρέχον εύρος", f"{start}–{end}" if releases else "0")
    summary_columns[2].metric("Loaded page", cursor["index"] + 1)

    filter_text = st.text_input(
        "Φίλτρο στην τρέχουσα σελίδα",
        placeholder="τίτλος, artist, catalog number ή barcode",
        key=f"mb_label_release_filter_{label_mbid}",
    )
    clean_filter = str(filter_text or "").strip().casefold()

    rows = [_release_row(release) for release in releases]
    if clean_filter:
        rows = [
            row
            for row in rows
            if clean_filter
            in " | ".join(
                str(row.get(column) or "")
                for column in [
                    "Τίτλος",
                    "Artist Credit",
                    "Catalog Number(s)",
                    "Barcode",
                    "Label(s)",
                ]
            ).casefold()
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
    elif releases:
        st.warning("Το φίλτρο δεν ταιριάζει με release της τρέχουσας σελίδας.")
    else:
        st.warning("Δεν βρέθηκαν releases συνδεδεμένα με αυτή τη δισκογραφική.")

    next_offset = offset + len(releases)
    has_previous = cursor["index"] > 0
    has_next = bool(releases) and next_offset < total_count

    navigation_columns = st.columns(2)
    with navigation_columns[0]:
        previous_clicked = st.button(
            "⬅️ Προηγούμενη σελίδα",
            width="stretch",
            disabled=not has_previous,
            key=f"mb_label_prev_{label_mbid}_{cursor['index']}_{page_size}",
        )
    with navigation_columns[1]:
        next_clicked = st.button(
            "Επόμενη σελίδα ➡️",
            width="stretch",
            disabled=not has_next,
            key=f"mb_label_next_{label_mbid}_{cursor['index']}_{page_size}",
        )

    if previous_clicked:
        cursor["index"] -= 1
        st.session_state["mb_label_release_cursor"] = cursor
        st.rerun()

    if next_clicked:
        cursor["offsets"] = cursor["offsets"][: cursor["index"] + 1]
        cursor["offsets"].append(next_offset)
        cursor["index"] += 1
        st.session_state["mb_label_release_cursor"] = cursor
        st.rerun()


# --------------------------------------------------------------------------
# Page
# --------------------------------------------------------------------------
def page_musicbrainz_label():
    notice = _consume_label_handoff()

    st.title("🏷️ MusicBrainz Label Auditor")
    st.caption(
        "Αναζήτηση και πλήρες audit δισκογραφικής: Label Code, identity data, "
        "aliases, relationships και paginated releases."
    )
    st.info(
        "Το identity lookup και κάθε release page αποθηκεύονται προσωρινά για "
        "1 ώρα. Δεν χρησιμοποιείται concurrency."
    )
    _render_notice(notice)

    with st.container(border=True):
        st.markdown("### 🆔 Lookup με Label MBID")
        with st.form("mb_label_lookup_form", clear_on_submit=False):
            label_input = st.text_input(
                "MusicBrainz Label ID ή URL",
                placeholder=(
                    "https://musicbrainz.org/label/"
                    "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
                ),
                key="mb_label_input",
            )
            direct_submitted = st.form_submit_button(
                "Έλεγχος Δισκογραφικής",
                type="primary",
                width="stretch",
            )

    with st.expander("🔎 Δεν έχετε Label MBID; Αναζητήστε δισκογραφική", expanded=True):
        selected = render_entity_search_panel(
            entity_type="label",
            state_key="mb_label_search_state",
            key_prefix="mb_label_search",
            query_label="Όνομα δισκογραφικής",
            placeholder="π.χ. XL Recordings, ECM Records, Stay Independent",
            action_label="➡️ Άνοιγμα στο Label Auditor",
            default_limit=10,
        )

    lookup_mbid = extract_mbid(st.session_state.pop("mb_label_pending_lookup", None))
    pending_stub = st.session_state.pop("mb_label_pending_stub", None)

    if direct_submitted:
        lookup_mbid = extract_mbid(label_input)
        pending_stub = None
        if not lookup_mbid:
            st.error(
                "Δεν βρέθηκε έγκυρο Label MBID. Χρειάζεται UUID της μορφής "
                "`xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`."
            )

    if isinstance(selected, dict):
        selected_mbid = extract_mbid(selected.get("id"))
        if selected_mbid:
            lookup_mbid = selected_mbid
            pending_stub = selected
        else:
            st.error("Το επιλεγμένο Label αποτέλεσμα δεν είχε έγκυρο MBID.")

    if lookup_mbid:
        try:
            with st.spinner("Fetching Label from MusicBrainz..."):
                label = mb_get_label(lookup_mbid)
        except musicbrainzngs.MusicBrainzError as exc:
            st.session_state.pop("mb_label_view_state", None)
            st.error(mb_error_message(exc))
            label = None
        except Exception as exc:
            st.session_state.pop("mb_label_view_state", None)
            st.error(f"Μη αναμενόμενο σφάλμα: {exc}")
            label = None

        if label:
            st.session_state["mb_label_view_state"] = {
                "mbid": lookup_mbid,
                "label": label,
                "source_stub": pending_stub if isinstance(pending_stub, dict) else {},
            }
            _reset_release_cursor(lookup_mbid)

    state = st.session_state.get("mb_label_view_state")
    if not isinstance(state, dict):
        return

    label = state.get("label")
    label_mbid = extract_mbid(state.get("mbid"))
    if not isinstance(label, dict) or not label_mbid:
        st.session_state.pop("mb_label_view_state", None)
        st.error("Το αποθηκευμένο Label αποτέλεσμα δεν είχε έγκυρη δομή.")
        return

    _render_label_header(label, label_mbid)
    _render_label_metadata(label)
    _render_label_releases(label_mbid)
