"""
tools/musicbrainz_entity_ui.py

Shared Streamlit-only helpers for the Phase 2 MusicBrainz entity views.

This module performs no network requests. It centralises search-result
formatting, relation/alias normalisation and the reusable search panel used by
Label Auditor, Release Group / Album View and the standalone Work Explorer.
"""

import hashlib
from typing import Iterable, Optional

import musicbrainzngs
import pandas as pd
import streamlit as st

from utils.musicbrainz_api import (
    mb_artist_credit_phrase,
    mb_entity_url,
    mb_error_message,
    mb_iswc,
    mb_search_entities,
)

RELATION_TARGET_SPECS = (
    ("area", "Περιοχή", "name"),
    ("artist", "Καλλιτέχνης", "name"),
    ("event", "Event", "name"),
    ("instrument", "Όργανο", "name"),
    ("label", "Δισκογραφική", "name"),
    ("place", "Τοποθεσία", "name"),
    ("recording", "Ηχογράφηση", "title"),
    ("release", "Κυκλοφορία", "title"),
    ("release-group", "Release Group", "title"),
    ("series", "Σειρά", "name"),
    ("url", "URL", "resource"),
    ("work", "Μουσικό έργο", "title"),
)


# --------------------------------------------------------------------------
# Generic text/entity helpers
# --------------------------------------------------------------------------
def safe_text(value, fallback="—"):
    text = str(value or "").strip()
    return text if text else fallback


def area_name(entity):
    if not isinstance(entity, dict):
        return "—"

    area = entity.get("area") or {}
    if isinstance(area, dict) and area.get("name"):
        return str(area["name"])

    begin_area = entity.get("begin-area") or {}
    if isinstance(begin_area, dict) and begin_area.get("name"):
        return str(begin_area["name"])

    return safe_text(entity.get("country"))


def life_span_text(entity):
    if not isinstance(entity, dict):
        return "—"

    life_span = entity.get("life-span") or {}
    if not isinstance(life_span, dict):
        return "—"

    begin = str(life_span.get("begin") or "").strip()
    end = str(life_span.get("end") or "").strip()
    ended = str(life_span.get("ended") or "").strip().lower()

    if begin and end:
        return f"{begin} → {end}"
    if begin:
        suffix = " (έληξε)" if ended == "true" else " →"
        return f"{begin}{suffix}"
    if end:
        return f"→ {end}"
    return "—"


def annotation_text(entity):
    if not isinstance(entity, dict):
        return ""

    annotation = entity.get("annotation")
    if isinstance(annotation, dict):
        return str(annotation.get("text") or "").strip()
    return str(annotation or "").strip()


def secondary_types(entity):
    values = entity.get("secondary-type-list") or []
    if not isinstance(values, list):
        return ""
    return ", ".join(str(value) for value in values if str(value).strip())


def related_artist_phrase(entity, include_roles=False):
    """Return artist names from artist relationships, useful for Work search."""
    if not isinstance(entity, dict):
        return ""

    values = []
    for relation in entity.get("artist-relation-list") or []:
        if not isinstance(relation, dict):
            continue
        artist = relation.get("artist") or {}
        if not isinstance(artist, dict):
            continue
        name = str(artist.get("name") or "").strip()
        if not name:
            continue
        role = str(relation.get("type") or "").strip()
        display = f"{name} ({role})" if include_roles and role else name
        if display not in values:
            values.append(display)
    return ", ".join(values)


def result_title(entity_type, entity):
    if entity_type in {"artist", "label"}:
        return safe_text(entity.get("name"))
    return safe_text(entity.get("title"))


def result_artist(entity_type, entity):
    artist = mb_artist_credit_phrase(entity)
    if artist:
        return artist
    if entity_type == "work":
        return related_artist_phrase(entity, include_roles=True)
    return ""


def result_type(entity_type, entity):
    if entity_type == "release":
        release_group = entity.get("release-group") or {}
        if isinstance(release_group, dict):
            primary_type = release_group.get("primary-type") or release_group.get("type")
            if primary_type:
                return str(primary_type)
        return safe_text(entity.get("status"))

    if entity_type == "release-group":
        primary = entity.get("primary-type") or entity.get("type") or ""
        secondary = secondary_types(entity)
        if primary and secondary:
            return f"{primary} · {secondary}"
        return safe_text(primary or secondary)

    if entity_type == "recording":
        return "Video" if str(entity.get("video") or "").lower() == "true" else "Audio"

    return safe_text(entity.get("type"))


def result_date(entity_type, entity):
    if entity_type in {"artist", "label"}:
        return life_span_text(entity)
    if entity_type in {"release-group", "recording"}:
        return safe_text(entity.get("first-release-date"))
    if entity_type == "release":
        return safe_text(entity.get("date"))
    return "—"


def result_identifier(entity_type, entity):
    if entity_type == "release":
        return safe_text(entity.get("barcode"))
    if entity_type == "recording":
        isrcs = entity.get("isrc-list") or []
        if isinstance(isrcs, list):
            values = [str(value).strip() for value in isrcs if str(value).strip()]
            return ", ".join(values) if values else "—"
        return "—"
    if entity_type == "label":
        return safe_text(entity.get("label-code"))
    if entity_type == "work":
        return mb_iswc(entity) or "—"
    return "—"


def search_result_row(entity_type, entity, position):
    mbid = str(entity.get("id") or "").strip()
    artist = result_artist(entity_type, entity)
    return {
        "#": position,
        "Score": int(entity.get("score") or 0),
        "Όνομα / Τίτλος": result_title(entity_type, entity),
        "Artist / Δημιουργοί": artist or "—",
        "Τύπος": result_type(entity_type, entity),
        "Χώρα / Περιοχή": area_name(entity),
        "Ημερομηνία / Περίοδος": result_date(entity_type, entity),
        "Disambiguation": safe_text(entity.get("disambiguation")),
        "ISRC / ISWC / Barcode / Label Code": result_identifier(entity_type, entity),
        "MBID": mbid or "—",
        "MusicBrainz": mb_entity_url(entity_type, mbid),
    }


def search_selection_label(entity_type, entity, position):
    title = result_title(entity_type, entity)
    artist = result_artist(entity_type, entity)
    date_value = result_date(entity_type, entity)
    country = area_name(entity)
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


def search_fingerprint(entity_type, query, strict, lucene_query, results):
    raw = "|".join(
        [
            str(entity_type or ""),
            str(query or ""),
            str(bool(strict)),
            str(bool(lucene_query)),
            str(len(results or [])),
        ]
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


# --------------------------------------------------------------------------
# Reusable search panel
# --------------------------------------------------------------------------
def render_entity_search_panel(
    *,
    entity_type,
    state_key,
    key_prefix,
    query_label,
    placeholder,
    action_label,
    default_limit=10,
    show_advanced=True,
):
    """
    Render a complete cached MusicBrainz search/disambiguation flow.

    Returns the selected raw result only when the user presses ``action_label``.
    The caller decides whether to lookup it locally or route it elsewhere.
    """
    with st.form(f"{key_prefix}_form", clear_on_submit=False):
        query = st.text_input(
            query_label,
            placeholder=placeholder,
            key=f"{key_prefix}_query",
        )

        option_columns = st.columns(3 if show_advanced else 2)
        with option_columns[0]:
            result_limit = st.selectbox(
                "Μέγιστα αποτελέσματα",
                options=[5, 10, 25, 50],
                index=[5, 10, 25, 50].index(default_limit)
                if default_limit in [5, 10, 25, 50]
                else 1,
                key=f"{key_prefix}_limit",
            )
        with option_columns[1]:
            strict = st.checkbox(
                "Όλοι οι όροι να ταιριάζουν",
                value=False,
                key=f"{key_prefix}_strict",
            )
        if show_advanced:
            with option_columns[2]:
                lucene_query = st.checkbox(
                    "Advanced Lucene query",
                    value=False,
                    key=f"{key_prefix}_lucene",
                    help=(
                        "Όταν είναι ενεργό, το κείμενο αποστέλλεται ως raw Lucene "
                        "query. Αφήστε το κλειστό για απλή αναζήτηση."
                    ),
                )
        else:
            lucene_query = False

        submitted = st.form_submit_button(
            "🔎 Αναζήτηση στο MusicBrainz",
            type="primary",
            width="stretch",
        )

    if submitted:
        clean_query = " ".join(str(query or "").split()).strip()
        if not clean_query:
            st.warning("Εισάγετε όνομα ή τίτλο για αναζήτηση.")
            st.session_state.pop(state_key, None)
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
                st.session_state[state_key] = {
                    "entity_type": entity_type,
                    "query": clean_query,
                    "strict": bool(strict),
                    "lucene_query": bool(lucene_query),
                    "results": results,
                }
            except musicbrainzngs.MusicBrainzError as exc:
                st.session_state.pop(state_key, None)
                st.error(mb_error_message(exc))
            except Exception as exc:
                st.session_state.pop(state_key, None)
                st.error(f"Μη αναμενόμενο σφάλμα: {exc}")

    state = st.session_state.get(state_key)
    if not isinstance(state, dict):
        return None

    results = [
        result
        for result in state.get("results") or []
        if isinstance(result, dict)
    ]

    st.caption(
        f"Query: `{state.get('query')}` · Βρέθηκαν {len(results)} αποτελέσματα."
    )

    if not results:
        st.warning(
            "Δεν βρέθηκαν αποτελέσματα. Δοκιμάστε διαφορετική γραφή, λιγότερους "
            "όρους ή απενεργοποιήστε το strict matching."
        )
        return None

    rows = [
        search_result_row(entity_type, result, position)
        for position, result in enumerate(results, start=1)
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
        results,
    )
    selected_index = st.selectbox(
        "Επιλέξτε το σωστό αποτέλεσμα",
        options=list(range(len(results))),
        format_func=lambda index: search_selection_label(
            entity_type,
            results[index],
            index + 1,
        ),
        key=f"{key_prefix}_selected_{fingerprint}",
    )

    selected = results[selected_index]
    selected_mbid = str(selected.get("id") or "").strip()

    action_columns = st.columns(2)
    with action_columns[0]:
        st.link_button(
            "🔗 Άνοιγμα στο MusicBrainz",
            mb_entity_url(entity_type, selected_mbid),
            width="stretch",
        )
    with action_columns[1]:
        clicked = st.button(
            action_label,
            type="primary",
            width="stretch",
            key=f"{key_prefix}_use_{selected_mbid or fingerprint}",
            disabled=not bool(selected_mbid),
        )

    return selected if clicked and selected_mbid else None


# --------------------------------------------------------------------------
# Aliases, annotations and relationships
# --------------------------------------------------------------------------
def alias_rows(entity):
    rows = []
    if not isinstance(entity, dict):
        return rows

    for alias in entity.get("alias-list") or []:
        if not isinstance(alias, dict):
            continue
        primary_value = str(alias.get("primary") or "").strip().lower()
        rows.append(
            {
                "Όνομα": alias.get("alias") or alias.get("name") or "—",
                "Τύπος": alias.get("type") or "—",
                "Sort Name": alias.get("sort-name") or "—",
                "Locale": alias.get("locale") or "—",
                "Από": alias.get("begin-date") or "—",
                "Έως": alias.get("end-date") or "—",
                "Primary": "✅" if primary_value in {"primary", "true", "1"} else "",
            }
        )
    return rows


def relation_attribute_text(relation):
    if not isinstance(relation, dict):
        return "—"

    raw_attributes = relation.get("attribute-list") or relation.get("attributes") or []
    if not isinstance(raw_attributes, list):
        raw_attributes = [raw_attributes]

    values = []
    for attribute in raw_attributes:
        if isinstance(attribute, dict):
            name = str(
                attribute.get("attribute")
                or attribute.get("name")
                or attribute.get("type")
                or ""
            ).strip()
            value = str(attribute.get("value") or "").strip()
            credited = str(attribute.get("credited-as") or "").strip()
            text = name
            if value:
                text = f"{text}: {value}" if text else value
            if credited:
                text = f"{text} [{credited}]" if text else credited
        else:
            text = str(attribute or "").strip()

        if text and text not in values:
            values.append(text)

    return ", ".join(values) if values else "—"


def _relation_target(relation, target_type, name_field):
    target = relation.get(target_type)
    if isinstance(target, dict):
        target_id = str(target.get("id") or relation.get("target-id") or "").strip()
        name = str(
            target.get(name_field)
            or target.get("name")
            or target.get("title")
            or target.get("resource")
            or ""
        ).strip()
        return name or "—", target_id, target

    raw_target = relation.get("target") or relation.get("target-id") or target
    raw_text = str(raw_target or "").strip()
    return raw_text or "—", "", {}


def relationship_rows(entity, target_types: Optional[Iterable[str]] = None):
    rows = []
    if not isinstance(entity, dict):
        return rows

    allowed = set(target_types) if target_types is not None else None

    for target_type, category, name_field in RELATION_TARGET_SPECS:
        if allowed is not None and target_type not in allowed:
            continue

        relation_list = entity.get(f"{target_type}-relation-list") or []
        for relation in relation_list:
            if not isinstance(relation, dict):
                continue

            target_name, target_id, target = _relation_target(
                relation,
                target_type,
                name_field,
            )

            if target_type == "url":
                relation_url = target_name if target_name.startswith(("http://", "https://")) else ""
            else:
                relation_url = mb_entity_url(target_type, target_id) if target_id else ""

            target_credit = str(relation.get("target-credit") or "").strip()
            begin = str(relation.get("begin") or "").strip()
            end = str(relation.get("end") or "").strip()
            ended = str(relation.get("ended") or "").strip().lower()

            if begin and end:
                period = f"{begin} → {end}"
            elif begin:
                period = f"{begin} →"
            elif end:
                period = f"→ {end}"
            elif ended == "true":
                period = "Έχει λήξει"
            else:
                period = "—"

            rows.append(
                {
                    "Κατηγορία": category,
                    "Ρόλος / Σχέση": str(relation.get("type") or "—").title(),
                    "Κατεύθυνση": relation.get("direction") or "—",
                    "Συνδεδεμένο με": target_name,
                    "Credited as": target_credit or "—",
                    "Attributes": relation_attribute_text(relation),
                    "Περίοδος": period,
                    "MBID": target_id or "—",
                    "MusicBrainz / URL": relation_url,
                }
            )

    return rows


def render_aliases(entity, empty_message):
    rows = alias_rows(entity)
    if rows:
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    else:
        st.warning(empty_message)


def render_annotation(entity):
    text = annotation_text(entity)
    if text:
        st.info(text)
    else:
        st.caption("Δεν υπάρχει annotation στο MusicBrainz.")


def render_relationship_table(rows, empty_message):
    if rows:
        dataframe = pd.DataFrame(rows)
        column_config = {}
        if "MusicBrainz / URL" in dataframe.columns:
            column_config["MusicBrainz / URL"] = st.column_config.LinkColumn(
                "MusicBrainz / URL",
                display_text="Άνοιγμα 🔗",
            )
        st.dataframe(
            dataframe,
            width="stretch",
            hide_index=True,
            column_config=column_config,
        )
    else:
        st.warning(empty_message)

