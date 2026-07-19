"""
tools/page_musicbrainz_work.py

Phase 2 — standalone MusicBrainz Work Explorer.

The page searches compositions directly by title (with optional Lucene syntax)
or looks them up by Work MBID/URL. It audits ISWC, language, Work attributes,
aliases, creators, linked recordings, related works and other relationships.
All network calls remain in utils/musicbrainz_api.py.
"""

import musicbrainzngs
import pandas as pd
import streamlit as st

from tools.musicbrainz_entity_ui import (
    relation_attribute_text,
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
    mb_entity_url,
    mb_error_message,
    mb_format_length,
    mb_get_work_full,
    mb_iswc,
)


# --------------------------------------------------------------------------
# Handoff and relation formatting
# --------------------------------------------------------------------------
def _consume_work_handoff():
    """Consume a one-time Universal Search handoff before widgets are built."""
    handoff = st.session_state.pop("mb_work_handoff", None)
    if not isinstance(handoff, dict):
        return None

    entity_type = str(handoff.get("entity_type") or "").strip().lower()
    mbid = extract_mbid(handoff.get("mbid"))
    result = handoff.get("result") if isinstance(handoff.get("result"), dict) else {}

    if entity_type != "work" or not mbid:
        return {
            "level": "error",
            "message": "Η επιλογή από το Universal Search δεν είχε έγκυρο Work MBID.",
        }

    st.session_state["mb_work_input"] = mbid
    st.session_state["mb_work_pending_lookup"] = mbid
    st.session_state["mb_work_pending_stub"] = result

    title = result.get("title") or "το επιλεγμένο μουσικό έργο"
    return {
        "level": "success",
        "message": (
            f"Το Work **{title}** μεταφέρθηκε στο standalone Work Explorer "
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


def _relation_period(relation):
    begin = str(relation.get("begin") or "").strip()
    end = str(relation.get("end") or "").strip()
    ended = str(relation.get("ended") or "").strip().lower()

    if begin and end:
        return f"{begin} → {end}"
    if begin:
        return f"{begin} →"
    if end:
        return f"→ {end}"
    if ended == "true":
        return "Έχει λήξει"
    return "—"


def _creator_rows(work):
    rows = []
    for relation in work.get("artist-relation-list") or []:
        if not isinstance(relation, dict):
            continue
        artist = relation.get("artist") or {}
        if not isinstance(artist, dict):
            artist = {}
        artist_id = extract_mbid(artist.get("id"))
        rows.append(
            {
                "Ρόλος": str(relation.get("type") or "—").title(),
                "Όνομα": safe_text(artist.get("name")),
                "Legal / Sort Name": safe_text(artist.get("sort-name")),
                "Credited as": safe_text(relation.get("target-credit")),
                "Attributes": relation_attribute_text(relation),
                "Περίοδος": _relation_period(relation),
                "Artist MBID": artist_id or "—",
                "MusicBrainz": mb_entity_url("artist", artist_id),
            }
        )
    return rows


def _recording_rows(work):
    rows = []
    for relation in work.get("recording-relation-list") or []:
        if not isinstance(relation, dict):
            continue
        recording = relation.get("recording") or {}
        if not isinstance(recording, dict):
            recording = {}
        recording_id = extract_mbid(recording.get("id"))
        rows.append(
            {
                "Σχέση": str(relation.get("type") or "—").title(),
                "Τίτλος Recording": safe_text(recording.get("title")),
                "Artist Credit": mb_artist_credit_phrase(recording) or "—",
                "Διάρκεια": mb_format_length(recording.get("length")),
                "Video": (
                    "✅"
                    if str(recording.get("video") or "").strip().lower() == "true"
                    else ""
                ),
                "Credited as": safe_text(relation.get("target-credit")),
                "Attributes": relation_attribute_text(relation),
                "Recording MBID": recording_id or "—",
                "MusicBrainz": mb_entity_url("recording", recording_id),
            }
        )
    return rows


def _related_work_rows(work):
    rows = []
    for relation in work.get("work-relation-list") or []:
        if not isinstance(relation, dict):
            continue
        related = relation.get("work") or {}
        if not isinstance(related, dict):
            related = {}
        related_id = extract_mbid(related.get("id"))
        rows.append(
            {
                "Σχέση": str(relation.get("type") or "—").title(),
                "Κατεύθυνση": safe_text(relation.get("direction")),
                "Τίτλος Work": safe_text(related.get("title")),
                "Τύπος": safe_text(related.get("type")),
                "ISWC": mb_iswc(related) or "—",
                "Attributes": relation_attribute_text(relation),
                "Work MBID": related_id or "—",
                "MusicBrainz": mb_entity_url("work", related_id),
            }
        )
    return rows


def _work_attribute_rows(work):
    rows = []
    for attribute in work.get("attribute-list") or []:
        if isinstance(attribute, dict):
            rows.append(
                {
                    "Attribute": safe_text(
                        attribute.get("attribute") or attribute.get("type")
                    ),
                    "Value": safe_text(attribute.get("value")),
                }
            )
        else:
            text = str(attribute or "").strip()
            if text:
                rows.append({"Attribute": text, "Value": "—"})
    return rows


def _render_linked_table(rows, link_column, empty_message):
    if not rows:
        st.warning(empty_message)
        return

    st.dataframe(
        pd.DataFrame(rows),
        width="stretch",
        hide_index=True,
        column_config={
            link_column: st.column_config.LinkColumn(
                link_column,
                display_text="Άνοιγμα 🔗",
            ),
        },
    )


# --------------------------------------------------------------------------
# Entity rendering
# --------------------------------------------------------------------------
def _render_work_header(work, work_mbid):
    title = work.get("title") or "—"
    disambiguation = str(work.get("disambiguation") or "").strip()
    iswc = mb_iswc(work)
    creators = work.get("artist-relation-list") or []
    recordings = work.get("recording-relation-list") or []

    st.divider()
    st.markdown(f"## 🎼 {title}")
    if disambiguation:
        st.info(f"ℹ️ {disambiguation}")

    identity_columns = st.columns(5)
    identity_columns[0].metric("Τύπος Work", safe_text(work.get("type")))
    identity_columns[1].metric("Γλώσσα", safe_text(work.get("language")))
    identity_columns[2].metric("ISWC", iswc or "—")
    identity_columns[3].metric("Creators", len(creators))
    identity_columns[4].metric("Linked Recordings", len(recordings))

    st.markdown("**Work MBID**")
    st.code(work_mbid, language=None)

    if not iswc:
        st.error(
            "❌ Δεν έχει καταχωρηθεί ISWC. Το Work υπάρχει, αλλά λείπει ο "
            "διεθνής κωδικός της σύνθεσης."
        )
    if not creators:
        st.warning(
            "⚠️ Δεν υπάρχουν artist relationships για composer / lyricist / "
            "arranger ή άλλο δημιουργικό ρόλο."
        )
    if not recordings:
        st.warning(
            "⚠️ Δεν υπάρχουν linked recordings. Η σύνθεση δεν συνδέεται με "
            "καταχωρημένη ηχογράφηση στο MusicBrainz."
        )

    action_columns = st.columns(2)
    with action_columns[0]:
        st.link_button(
            "🔗 Προβολή στο MusicBrainz",
            mb_entity_url("work", work_mbid),
            width="stretch",
        )
    with action_columns[1]:
        st.link_button(
            "✏️ Επεξεργασία Work",
            f"https://musicbrainz.org/work/{work_mbid}/edit",
            width="stretch",
        )


def _render_work_details(work):
    st.divider()
    st.markdown("### 🧾 Work Attributes")
    attribute_rows = _work_attribute_rows(work)
    if attribute_rows:
        st.dataframe(pd.DataFrame(attribute_rows), width="stretch", hide_index=True)
    else:
        st.caption("Δεν υπάρχουν ειδικά Work attributes.")

    st.divider()
    st.markdown("### 📝 Annotation")
    render_annotation(work)

    st.divider()
    st.markdown("### 🔤 Aliases")
    render_aliases(
        work,
        "Δεν έχουν καταχωρηθεί aliases για αυτό το μουσικό έργο.",
    )

    st.divider()
    st.markdown("### ✍️ Δημιουργοί / Artist Relationships")
    creator_rows = _creator_rows(work)
    _render_linked_table(
        creator_rows,
        "MusicBrainz",
        "Δεν υπάρχουν creators ή άλλες artist relationships στο Work.",
    )

    st.divider()
    st.markdown("### 🎙️ Linked Recordings")
    recording_rows = _recording_rows(work)
    _render_linked_table(
        recording_rows,
        "MusicBrainz",
        "Δεν υπάρχουν recordings συνδεδεμένα με αυτό το Work.",
    )

    st.divider()
    st.markdown("### 🧩 Related Works")
    related_work_rows = _related_work_rows(work)
    _render_linked_table(
        related_work_rows,
        "MusicBrainz",
        "Δεν υπάρχουν part-of, derivative ή άλλα Work-to-Work relationships.",
    )

    st.divider()
    st.markdown("### 🔗 Άλλα Relationships")
    other_target_types = {
        "area",
        "event",
        "instrument",
        "label",
        "place",
        "release",
        "release-group",
        "series",
        "url",
    }
    other_rows = relationship_rows(work, target_types=other_target_types)
    render_relationship_table(
        other_rows,
        "Δεν υπάρχουν άλλα relationships ή εξωτερικά URLs για αυτό το Work.",
    )


# --------------------------------------------------------------------------
# Page
# --------------------------------------------------------------------------
def page_musicbrainz_work():
    notice = _consume_work_handoff()

    st.title("🎼 MusicBrainz Work Explorer")
    st.caption(
        "Αναζητήστε απευθείας μια σύνθεση χωρίς ISRC και ελέγξτε ISWC, "
        "δημιουργούς, linked recordings και Work relationships."
    )
    st.info(
        "Η αναζήτηση και το πλήρες Work lookup αποθηκεύονται προσωρινά για "
        "1 ώρα. Το lookup είναι ανεξάρτητο από το Recording → Work resolver."
    )
    _render_notice(notice)

    with st.container(border=True):
        st.markdown("### 🔎 Αναζήτηση Work με τίτλο")
        selected = render_entity_search_panel(
            entity_type="work",
            state_key="mb_work_search_state",
            key_prefix="mb_work_search",
            query_label="Τίτλος σύνθεσης / μουσικού έργου",
            placeholder="π.χ. Imagine, Summertime, Τα παιδιά του Πειραιά",
            action_label="➡️ Άνοιγμα στο Work Explorer",
            default_limit=10,
        )

    with st.expander("🆔 Έχετε ήδη Work MBID ή URL;"):
        with st.form("mb_work_lookup_form", clear_on_submit=False):
            work_input = st.text_input(
                "MusicBrainz Work ID ή URL",
                placeholder=(
                    "https://musicbrainz.org/work/"
                    "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
                ),
                key="mb_work_input",
            )
            direct_submitted = st.form_submit_button(
                "Ανάλυση Work",
                type="primary",
                width="stretch",
            )

    lookup_mbid = extract_mbid(st.session_state.pop("mb_work_pending_lookup", None))
    pending_stub = st.session_state.pop("mb_work_pending_stub", None)

    if direct_submitted:
        lookup_mbid = extract_mbid(work_input)
        pending_stub = None
        if not lookup_mbid:
            st.error(
                "Δεν βρέθηκε έγκυρο Work MBID. Χρειάζεται UUID της μορφής "
                "`xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`."
            )

    if isinstance(selected, dict):
        selected_mbid = extract_mbid(selected.get("id"))
        if selected_mbid:
            lookup_mbid = selected_mbid
            pending_stub = selected
        else:
            st.error("Το επιλεγμένο Work αποτέλεσμα δεν είχε έγκυρο MBID.")

    if lookup_mbid:
        try:
            with st.spinner("Fetching full Work from MusicBrainz..."):
                work = mb_get_work_full(lookup_mbid)
        except musicbrainzngs.MusicBrainzError as exc:
            st.session_state.pop("mb_work_view_state", None)
            st.error(mb_error_message(exc))
            work = None
        except Exception as exc:
            st.session_state.pop("mb_work_view_state", None)
            st.error(f"Μη αναμενόμενο σφάλμα: {exc}")
            work = None

        if work:
            st.session_state["mb_work_view_state"] = {
                "mbid": lookup_mbid,
                "work": work,
                "source_stub": pending_stub if isinstance(pending_stub, dict) else {},
            }

    state = st.session_state.get("mb_work_view_state")
    if not isinstance(state, dict):
        return

    work = state.get("work")
    work_mbid = extract_mbid(state.get("mbid"))
    if not isinstance(work, dict) or not work_mbid:
        st.session_state.pop("mb_work_view_state", None)
        st.error("Το αποθηκευμένο Work αποτέλεσμα δεν είχε έγκυρη δομή.")
        return

    _render_work_header(work, work_mbid)
    _render_work_details(work)
