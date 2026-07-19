"""
tools/page_musicbrainz_search.py

Phase 1 — MusicBrainz Universal Search landing page.

The page searches the six requested core entity types and lets the user choose
one unambiguous result. Artist, Recording and Release selections can be handed
off to the existing MusicBrainz Explorer pipelines through Streamlit session
state. Label, Work and Release Group selections remain search-and-open results
until their dedicated Phase 2 views are implemented.
"""

import hashlib

import musicbrainzngs
import pandas as pd
import streamlit as st

from utils.musicbrainz_api import (
    mb_artist_credit_phrase,
    mb_entity_url,
    mb_error_message,
    mb_format_length,
    mb_iswc,
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

EXPLORER_HANDOFF_TARGETS = {
    "artist": "Artist Auditor",
    "recording": "ISRC / ISWC Resolver",
    "release": "Catalog Barcode Scanner",
}


# --------------------------------------------------------------------------
# Result formatting
# --------------------------------------------------------------------------
def _safe_text(value, fallback="—"):
    text = str(value or "").strip()
    return text if text else fallback


def _area_name(entity):
    if not isinstance(entity, dict):
        return "—"

    area = entity.get("area") or {}
    if isinstance(area, dict) and area.get("name"):
        return str(area["name"])

    begin_area = entity.get("begin-area") or {}
    if isinstance(begin_area, dict) and begin_area.get("name"):
        return str(begin_area["name"])

    return _safe_text(entity.get("country"))


def _life_span(entity):
    life_span = entity.get("life-span") or {}
    if not isinstance(life_span, dict):
        return "—"

    begin = str(life_span.get("begin") or "").strip()
    end = str(life_span.get("end") or "").strip()

    if begin and end:
        return f"{begin} → {end}"
    if begin:
        return f"{begin} →"
    if end:
        return f"→ {end}"
    return "—"


def _secondary_types(entity):
    values = entity.get("secondary-type-list") or []
    if not isinstance(values, list):
        return ""
    return ", ".join(str(value) for value in values if str(value).strip())


def _recording_isrcs(entity):
    values = entity.get("isrc-list") or []
    if not isinstance(values, list):
        return ""
    return ", ".join(str(value) for value in values if str(value).strip())


def _result_title(entity_type, entity):
    if entity_type in {"artist", "label"}:
        return _safe_text(entity.get("name"))
    return _safe_text(entity.get("title"))


def _result_type(entity_type, entity):
    if entity_type == "release":
        release_group = entity.get("release-group") or {}
        if isinstance(release_group, dict):
            primary_type = release_group.get("primary-type") or release_group.get("type")
            if primary_type:
                return str(primary_type)
        return _safe_text(entity.get("status"))

    if entity_type == "release-group":
        primary = entity.get("primary-type") or entity.get("type") or ""
        secondary = _secondary_types(entity)
        if primary and secondary:
            return f"{primary} · {secondary}"
        return _safe_text(primary or secondary)

    if entity_type == "recording":
        return "Video" if str(entity.get("video") or "").lower() == "true" else "Audio"

    return _safe_text(entity.get("type"))


def _result_date(entity_type, entity):
    if entity_type in {"artist", "label"}:
        return _life_span(entity)
    if entity_type == "release-group":
        return _safe_text(entity.get("first-release-date"))
    if entity_type == "recording":
        return _safe_text(entity.get("first-release-date"))
    if entity_type == "release":
        return _safe_text(entity.get("date"))
    return "—"


def _result_identifier(entity_type, entity):
    if entity_type == "release":
        return _safe_text(entity.get("barcode"))
    if entity_type == "recording":
        return _recording_isrcs(entity) or "—"
    if entity_type == "label":
        return _safe_text(entity.get("label-code"))
    if entity_type == "work":
        return mb_iswc(entity) or "—"
    return "—"


def _result_row(entity_type, entity, position):
    mbid = str(entity.get("id") or "").strip()
    artist_credit = mb_artist_credit_phrase(entity)

    return {
        "#": position,
        "Score": int(entity.get("score") or 0),
        "Όνομα / Τίτλος": _result_title(entity_type, entity),
        "Artist Credit": artist_credit or "—",
        "Τύπος": _result_type(entity_type, entity),
        "Χώρα / Περιοχή": _area_name(entity),
        "Ημερομηνία / Περίοδος": _result_date(entity_type, entity),
        "Disambiguation": _safe_text(entity.get("disambiguation")),
        "ISRC / ISWC / Barcode / Label Code": _result_identifier(entity_type, entity),
        "MBID": mbid or "—",
        "MusicBrainz": mb_entity_url(entity_type, mbid),
    }


def _selection_label(entity_type, entity, position):
    title = _result_title(entity_type, entity)
    artist = mb_artist_credit_phrase(entity)
    date_value = _result_date(entity_type, entity)
    country = _area_name(entity)
    disambiguation = str(entity.get("disambiguation") or "").strip()
    score = str(entity.get("score") or "0")

    parts = [f"{position}. [{score}] {title}"]
    if artist:
        parts.append(artist)
    if date_value != "—":
        parts.append(date_value)
    if country != "—":
        parts.append(country)
    if disambiguation:
        parts.append(disambiguation)

    label = " — ".join(parts)
    return label if len(label) <= 220 else f"{label[:217]}..."


def _search_fingerprint(state):
    raw = "|".join(
        [
            str(state.get("entity_type") or ""),
            str(state.get("query") or ""),
            str(state.get("strict") or False),
            str(state.get("lucene_query") or False),
            str(len(state.get("results") or [])),
        ]
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _render_selected_result(entity_type, selected):
    title = _result_title(entity_type, selected)
    artist = mb_artist_credit_phrase(selected)
    mbid = str(selected.get("id") or "").strip()

    with st.container(border=True):
        st.markdown(f"### ✅ {title}")
        if artist:
            st.caption(artist)

        d1, d2, d3, d4 = st.columns(4)
        d1.metric("Τύπος", _result_type(entity_type, selected))
        d2.metric("Χώρα / Περιοχή", _area_name(selected))
        d3.metric("Ημερομηνία", _result_date(entity_type, selected))
        d4.metric("Score", str(selected.get("score") or "0"))

        disambiguation = str(selected.get("disambiguation") or "").strip()
        if disambiguation:
            st.info(f"ℹ️ {disambiguation}")

        identifier = _result_identifier(entity_type, selected)
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
            target = EXPLORER_HANDOFF_TARGETS.get(entity_type)
            if target and mbid:
                if st.button(
                    f"➡️ Χρήση στο {target}",
                    type="primary",
                    width="stretch",
                    key=f"mb_universal_handoff_{entity_type}_{mbid}",
                ):
                    st.session_state["mb_explorer_handoff"] = {
                        "entity_type": entity_type,
                        "mbid": mbid,
                        "result": selected,
                        "source_query": st.session_state.get(
                            "mb_universal_search_state", {}
                        ).get("query", ""),
                    }
                    st.session_state.current_page = "MusicBrainz Explorer"
                    st.rerun()
            else:
                st.button(
                    "Δεν υπάρχει ακόμη αντίστοιχο Auditor",
                    width="stretch",
                    disabled=True,
                    key=f"mb_universal_no_handoff_{entity_type}_{mbid}",
                )

        if entity_type not in EXPLORER_HANDOFF_TARGETS:
            st.caption(
                "Η Phase 1 ολοκληρώνει την αναζήτηση, την αποσαφήνιση και το MBID. "
                "Η αναλυτική in-app προβολή αυτού του entity δεν προστίθεται ακόμη."
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
        "Τα αποτελέσματα αποθηκεύονται προσωρινά για 1 ώρα."
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
                    "strict": strict,
                    "lucene_query": lucene_query,
                    "results": results,
                }
            except musicbrainzngs.MusicBrainzError as exc:
                st.session_state.pop("mb_universal_search_state", None)
                st.error(mb_error_message(exc))
            except Exception as exc:
                st.session_state.pop("mb_universal_search_state", None)
                st.error(f"Μη αναμενόμενο σφάλμα: {exc}")

    state = st.session_state.get("mb_universal_search_state")
    if not state:
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

    rows = [
        _result_row(entity_type, result, position)
        for position, result in enumerate(results, start=1)
        if isinstance(result, dict)
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

    valid_results = [result for result in results if isinstance(result, dict)]
    if not valid_results:
        st.warning("Τα αποτελέσματα δεν είχαν αναγνωρίσιμη δομή.")
        return

    fingerprint = _search_fingerprint(state)
    selected_index = st.selectbox(
        "Επιλέξτε το σωστό αποτέλεσμα",
        options=list(range(len(valid_results))),
        format_func=lambda index: _selection_label(
            entity_type,
            valid_results[index],
            index + 1,
        ),
        key=f"mb_universal_selected_{fingerprint}",
    )

    selected = valid_results[selected_index]
    _render_selected_result(entity_type, selected)
