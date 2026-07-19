"""
tools/page_musicbrainz_search.py

Phase 2 — MusicBrainz Universal Search landing page.

The page searches the six core entity types used by the app, presents a full
disambiguation table and routes every selected result to a dedicated in-app
view:
- Artist, Recording and Release → established MusicBrainz Explorer pipelines
- Label → Label Auditor
- Release Group → Album Editions view
- Work → standalone Work Explorer
"""

import musicbrainzngs
import pandas as pd
import streamlit as st

from tools.musicbrainz_entity_ui import (
    area_name,
    result_artist,
    result_date,
    result_identifier,
    result_title,
    result_type,
    search_fingerprint,
    search_result_row,
    search_selection_label,
)
from utils.musicbrainz_api import (
    mb_entity_url,
    mb_error_message,
    mb_search_entities,
)

ENTITY_OPTIONS = {
    "Καλλιτέχνης (Artist)": "artist",
    "Κυκλοφορία (Release)": "release",
    "Album / Release Group": "release-group",
    "Ηχογράφηση (Recording)": "recording",
    "Δισκογραφική (Label)": "label",
    "Μουσικό έργο (Work)": "work",
}

ENTITY_NAMES = {
    "artist": "Καλλιτέχνης",
    "release": "Κυκλοφορία",
    "release-group": "Release Group",
    "recording": "Ηχογράφηση",
    "label": "Δισκογραφική",
    "work": "Μουσικό έργο",
}

HANDOFF_TARGETS = {
    "artist": {
        "label": "Artist Auditor",
        "page": "MusicBrainz Explorer",
        "session_key": "mb_explorer_handoff",
    },
    "recording": {
        "label": "ISRC / ISWC Resolver",
        "page": "MusicBrainz Explorer",
        "session_key": "mb_explorer_handoff",
    },
    "release": {
        "label": "Catalog Barcode Scanner",
        "page": "MusicBrainz Explorer",
        "session_key": "mb_explorer_handoff",
    },
    "label": {
        "label": "Label Auditor",
        "page": "MusicBrainz Label Auditor",
        "session_key": "mb_label_handoff",
    },
    "release-group": {
        "label": "Album Editions view",
        "page": "MusicBrainz Release Group",
        "session_key": "mb_release_group_handoff",
    },
    "work": {
        "label": "Work Explorer",
        "page": "MusicBrainz Work Explorer",
        "session_key": "mb_work_handoff",
    },
}


# --------------------------------------------------------------------------
# Selected-result card and handoff
# --------------------------------------------------------------------------
def _render_selected_result(entity_type, selected):
    title = result_title(entity_type, selected)
    artist = result_artist(entity_type, selected)
    mbid = str(selected.get("id") or "").strip()
    target = HANDOFF_TARGETS.get(entity_type)

    with st.container(border=True):
        st.markdown(f"### ✅ {title}")
        if artist:
            st.caption(artist)

        detail_columns = st.columns(4)
        detail_columns[0].metric("Τύπος", result_type(entity_type, selected))
        detail_columns[1].metric("Χώρα / Περιοχή", area_name(selected))
        detail_columns[2].metric("Ημερομηνία", result_date(entity_type, selected))
        detail_columns[3].metric("Score", str(selected.get("score") or "0"))

        disambiguation = str(selected.get("disambiguation") or "").strip()
        if disambiguation:
            st.info(f"ℹ️ {disambiguation}")

        identifier = result_identifier(entity_type, selected)
        if identifier != "—":
            st.markdown("**Σχετικός κωδικός**")
            st.code(identifier, language=None)

        st.markdown("**MusicBrainz MBID**")
        st.code(mbid or "—", language=None)

        button_columns = st.columns(2)
        with button_columns[0]:
            st.link_button(
                "🔗 Άνοιγμα στο MusicBrainz",
                mb_entity_url(entity_type, mbid),
                width="stretch",
            )

        with button_columns[1]:
            handoff_clicked = st.button(
                f"➡️ Χρήση στο {target['label'] if target else 'dedicated view'}",
                type="primary",
                width="stretch",
                disabled=not bool(target and mbid),
                key=f"mb_universal_handoff_{entity_type}_{mbid or 'missing'}",
            )

        if handoff_clicked and target and mbid:
            payload = {
                "entity_type": entity_type,
                "mbid": mbid,
                "result": selected,
                "source_query": st.session_state.get(
                    "mb_universal_search_state", {}
                ).get("query", ""),
            }
            st.session_state[target["session_key"]] = payload
            st.session_state.current_page = target["page"]
            st.rerun()

        if target:
            st.caption(
                f"Το αποτέλεσμα θα μεταφερθεί στο **{target['label']}** με το "
                "MBID και το επιλεγμένο search stub, χωρίς νέα search call."
            )


# --------------------------------------------------------------------------
# Page
# --------------------------------------------------------------------------
def page_musicbrainz_search():
    st.title("🔎 MusicBrainz Universal Search")
    st.caption(
        "Αναζητήστε καλλιτέχνη, κυκλοφορία, album/release group, ηχογράφηση, "
        "δισκογραφική ή μουσικό έργο χωρίς να γνωρίζετε προηγουμένως MBID, "
        "ISRC ή barcode."
    )

    st.info(
        "Κάθε αναζήτηση είναι ξεχωριστή κλήση προς το MusicBrainz. "
        "Τα αποτελέσματα αποθηκεύονται προσωρινά για 1 ώρα και όλα τα core "
        "entities διαθέτουν πλέον ενεργό in-app handoff."
    )

    with st.form("mb_universal_search_form", clear_on_submit=False):
        entity_label = st.selectbox(
            "Τύπος οντότητας",
            options=list(ENTITY_OPTIONS.keys()),
            key="mb_universal_entity_label",
        )

        query = st.text_input(
            "Όνομα ή τίτλος",
            placeholder="π.χ. Queen, Abbey Road, Imagine, XL Recordings",
            key="mb_universal_query",
        )

        option_columns = st.columns(3)
        with option_columns[0]:
            result_limit = st.selectbox(
                "Μέγιστα αποτελέσματα",
                options=[5, 10, 25, 50],
                index=1,
                key="mb_universal_limit",
            )
        with option_columns[1]:
            strict = st.checkbox(
                "Όλοι οι όροι να ταιριάζουν",
                value=False,
                key="mb_universal_strict",
                help=(
                    "Χρησιμοποιεί strict=True του musicbrainzngs. "
                    "Μπορεί να μειώσει τα πιο χαλαρά αποτελέσματα."
                ),
            )
        with option_columns[2]:
            lucene_query = st.checkbox(
                "Advanced Lucene query",
                value=False,
                key="mb_universal_lucene",
                help=(
                    "Όταν είναι ενεργό, το κείμενο αποστέλλεται ως raw Lucene query. "
                    "Αφήστε το κλειστό για απλή αναζήτηση ονόματος/τίτλου."
                ),
            )

        submitted = st.form_submit_button(
            "🔎 Αναζήτηση στο MusicBrainz",
            type="primary",
            width="stretch",
        )

    if submitted:
        clean_query = " ".join(str(query or "").split()).strip()
        entity_type = ENTITY_OPTIONS[entity_label]

        if not clean_query:
            st.warning("Εισάγετε όνομα ή τίτλο για αναζήτηση.")
            st.session_state.pop("mb_universal_search_state", None)
        else:
            try:
                with st.spinner("Αναζήτηση στο MusicBrainz..."):
                    results = mb_search_entities(
                        entity_type=entity_type,
                        query=clean_query,
                        limit=result_limit,
                        strict=strict,
                        lucene_query=lucene_query,
                    )

                st.session_state["mb_universal_search_state"] = {
                    "entity_type": entity_type,
                    "entity_name": ENTITY_NAMES[entity_type],
                    "query": clean_query,
                    "strict": bool(strict),
                    "lucene_query": bool(lucene_query),
                    "results": results,
                }
            except musicbrainzngs.MusicBrainzError as exc:
                st.session_state.pop("mb_universal_search_state", None)
                st.error(mb_error_message(exc))
            except Exception as exc:
                st.session_state.pop("mb_universal_search_state", None)
                st.error(f"Μη αναμενόμενο σφάλμα: {exc}")

    state = st.session_state.get("mb_universal_search_state")
    if not isinstance(state, dict):
        return

    entity_type = state.get("entity_type")
    results = state.get("results") or []
    entity_name = state.get("entity_name") or ENTITY_NAMES.get(entity_type, "Οντότητα")

    st.divider()
    st.markdown(f"### Αποτελέσματα: {entity_name}")
    st.caption(
        f"Query: `{state.get('query')}` · Βρέθηκαν {len(results)} αποτελέσματα."
    )

    if not results:
        st.warning(
            "Δεν βρέθηκαν αποτελέσματα. Δοκιμάστε διαφορετική γραφή, λιγότερους "
            "όρους ή απενεργοποιήστε το strict matching."
        )
        return

    valid_results = [result for result in results if isinstance(result, dict)]
    if not valid_results:
        st.warning("Τα αποτελέσματα δεν είχαν αναγνωρίσιμη δομή.")
        return

    rows = [
        search_result_row(entity_type, result, position)
        for position, result in enumerate(valid_results, start=1)
    ]

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

    fingerprint = search_fingerprint(
        entity_type,
        state.get("query"),
        state.get("strict"),
        state.get("lucene_query"),
        valid_results,
    )
    selected_index = st.selectbox(
        "Επιλέξτε το σωστό αποτέλεσμα",
        options=list(range(len(valid_results))),
        format_func=lambda index: search_selection_label(
            entity_type,
            valid_results[index],
            index + 1,
        ),
        key=f"mb_universal_selected_{fingerprint}",
    )

    _render_selected_result(entity_type, valid_results[selected_index])
