"""
tools/page_musicbrainz.py

TOOL D: MusicBrainz Explorer — deep metadata extraction with Phase 1
search-first fallbacks.

The three established pipelines remain intact:
- Artist Auditor
- ISRC / ISWC Resolver
- Catalog Barcode Scanner

All MusicBrainz network calls live in utils/musicbrainz_api.py. This page only
renders UI, coordinates cached API helpers and transfers Universal Search
selections into the relevant existing pipeline.
"""

import hashlib
import re

import musicbrainzngs
import pandas as pd
import streamlit as st

from utils.musicbrainz_api import (
    extract_mbid,
    mb_artist_credit_phrase,
    mb_entity_url,
    mb_error_message,
    mb_format_length,
    mb_get_artist,
    mb_get_recording,
    mb_get_recordings_by_isrc,
    mb_get_release,
    mb_get_work,
    mb_iswc,
    mb_search_entities,
    mb_search_releases_by_barcode,
)
from utils.tidal_api import validate_isrc

SEARCH_RESULT_LIMIT = 10


# --------------------------------------------------------------------------
# Shared UI helpers
# --------------------------------------------------------------------------
def _safe_text(value, fallback="—"):
    text = str(value or "").strip()
    return text if text else fallback


def _entity_title(entity_type, entity):
    if entity_type == "artist":
        return _safe_text(entity.get("name"))
    return _safe_text(entity.get("title"))


def _entity_type(entity_type, entity):
    if entity_type == "artist":
        return _safe_text(entity.get("type"))

    if entity_type == "recording":
        return "Video" if str(entity.get("video") or "").lower() == "true" else "Audio"

    if entity_type == "release":
        release_group = entity.get("release-group") or {}
        if isinstance(release_group, dict):
            value = release_group.get("primary-type") or release_group.get("type")
            if value:
                return str(value)
        return _safe_text(entity.get("status"))

    return _safe_text(entity.get("type"))


def _entity_country(entity):
    country = str(entity.get("country") or "").strip()
    if country:
        return country

    area = entity.get("area") or {}
    if isinstance(area, dict) and area.get("name"):
        return str(area["name"])

    return "—"


def _entity_date(entity_type, entity):
    if entity_type == "artist":
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

    if entity_type == "recording":
        return _safe_text(entity.get("first-release-date"))

    if entity_type == "release":
        return _safe_text(entity.get("date"))

    return "—"


def _search_result_row(entity_type, entity, position):
    return {
        "#": position,
        "Score": int(entity.get("score") or 0),
        "Όνομα / Τίτλος": _entity_title(entity_type, entity),
        "Artist Credit": mb_artist_credit_phrase(entity) or "—",
        "Τύπος": _entity_type(entity_type, entity),
        "Χώρα": _entity_country(entity),
        "Ημερομηνία": _entity_date(entity_type, entity),
        "Disambiguation": _safe_text(entity.get("disambiguation")),
        "MBID": _safe_text(entity.get("id")),
    }


def _search_selection_label(entity_type, entity, position):
    title = _entity_title(entity_type, entity)
    artist = mb_artist_credit_phrase(entity)
    date_value = _entity_date(entity_type, entity)
    country = _entity_country(entity)
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


def _search_state_fingerprint(entity_type, query, results):
    raw = f"{entity_type}|{query}|{len(results)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _render_search_fallback(
    *,
    entity_type,
    expander_label,
    input_label,
    placeholder,
    key_prefix,
    action_label,
):
    """
    Render a persistent, one-request search fallback inside an existing tab.

    Returns the selected raw MusicBrainz result only when the user explicitly
    presses the action button. The caller then feeds its MBID into the existing
    lookup pipeline.
    """
    selected_for_pipeline = None
    state_key = f"{key_prefix}_state"

    with st.expander(expander_label):
        st.caption(
            "Η αναζήτηση κάνει μία κλήση στο MusicBrainz. Μετά την επιλογή, "
            "το πλήρες lookup κάνει μία ακόμη κλήση."
        )

        with st.form(f"{key_prefix}_form", clear_on_submit=False):
            query = st.text_input(
                input_label,
                placeholder=placeholder,
                key=f"{key_prefix}_query",
            )
            strict = st.checkbox(
                "Όλοι οι όροι να ταιριάζουν",
                value=False,
                key=f"{key_prefix}_strict",
            )
            submitted = st.form_submit_button(
                "🔎 Αναζήτηση",
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
                            limit=SEARCH_RESULT_LIMIT,
                            strict=strict,
                            lucene_query=False,
                        )
                    st.session_state[state_key] = {
                        "query": clean_query,
                        "results": results,
                    }
                except musicbrainzngs.MusicBrainzError as exc:
                    st.session_state.pop(state_key, None)
                    st.error(mb_error_message(exc))
                except Exception as exc:
                    st.session_state.pop(state_key, None)
                    st.error(f"Μη αναμενόμενο σφάλμα: {exc}")

        state = st.session_state.get(state_key)
        if not state:
            return None

        results = [
            result
            for result in state.get("results") or []
            if isinstance(result, dict)
        ]

        if not results:
            st.warning("Δεν βρέθηκαν αποτελέσματα.")
            return None

        rows = [
            _search_result_row(entity_type, result, position)
            for position, result in enumerate(results, start=1)
        ]
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

        fingerprint = _search_state_fingerprint(
            entity_type,
            state.get("query"),
            results,
        )
        selected_index = st.selectbox(
            "Επιλέξτε το σωστό αποτέλεσμα",
            options=list(range(len(results))),
            format_func=lambda index: _search_selection_label(
                entity_type,
                results[index],
                index + 1,
            ),
            key=f"{key_prefix}_selection_{fingerprint}",
        )
        selected = results[selected_index]
        selected_mbid = str(selected.get("id") or "").strip()

        action_columns = st.columns(2)
        with action_columns[0]:
            st.link_button(
                "🔗 Προβολή στο MusicBrainz",
                mb_entity_url(entity_type, selected_mbid),
                width="stretch",
            )
        with action_columns[1]:
            if st.button(
                action_label,
                type="primary",
                width="stretch",
                key=f"{key_prefix}_use_{selected_mbid}",
            ):
                if not selected_mbid:
                    st.error("Το επιλεγμένο αποτέλεσμα δεν έχει έγκυρο MBID.")
                else:
                    selected_for_pipeline = selected

    return selected_for_pipeline


def _consume_explorer_handoff():
    """Apply a one-time Universal Search handoff before tab widgets are built."""
    handoff = st.session_state.pop("mb_explorer_handoff", None)
    if not isinstance(handoff, dict):
        return None

    entity_type = str(handoff.get("entity_type") or "").strip().lower()
    mbid = extract_mbid(handoff.get("mbid"))
    result = handoff.get("result") if isinstance(handoff.get("result"), dict) else {}

    if not mbid:
        return {
            "level": "error",
            "message": "Η επιλογή από το Universal Search δεν είχε έγκυρο MBID.",
        }

    title = _entity_title(entity_type, result)

    if entity_type == "artist":
        st.session_state["mb_artist_input"] = mbid
        st.session_state["mb_artist_pending_lookup"] = mbid
        return {
            "level": "success",
            "message": (
                f"Ο καλλιτέχνης **{title}** μεταφέρθηκε στο Artist Auditor "
                "και γίνεται αυτόματο lookup."
            ),
        }

    if entity_type == "recording":
        isrc_list = result.get("isrc-list") or []
        if isinstance(isrc_list, list) and isrc_list:
            st.session_state["mb_isrc_input"] = str(isrc_list[0])
        st.session_state["mb_recording_pending_lookup"] = mbid
        return {
            "level": "success",
            "message": (
                f"Η ηχογράφηση **{title}** μεταφέρθηκε στο ISRC / ISWC Resolver "
                "και γίνεται lookup με Recording MBID. Ανοίξτε το αντίστοιχο tab."
            ),
        }

    if entity_type == "release":
        barcode = re.sub(r"\D+", "", str(result.get("barcode") or ""))
        if barcode:
            st.session_state["mb_barcode_input"] = barcode
        st.session_state["mb_release_pending_lookup"] = mbid
        st.session_state["mb_release_pending_stub"] = result
        return {
            "level": "success",
            "message": (
                f"Η κυκλοφορία **{title}** μεταφέρθηκε στο Catalog Barcode Scanner "
                "και γίνεται lookup με Release MBID. Ανοίξτε το αντίστοιχο tab."
            ),
        }

    return {
        "level": "warning",
        "message": (
            "Το επιλεγμένο entity δεν αντιστοιχεί σε ένα από τα τρία υπάρχοντα "
            "Explorer pipelines."
        ),
    }


def _render_handoff_notice(notice):
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


# --------------------------------------------------------------------------
# Artist Auditor rendering
# --------------------------------------------------------------------------
def _render_artist_audit(artist, mbid):
    st.divider()

    name = artist.get("name") or "—"
    artist_type = artist.get("type") or "Άγνωστο"
    sort_name = artist.get("sort-name") or "—"
    country = artist.get("country") or "—"
    disambiguation = artist.get("disambiguation") or ""
    life_span = artist.get("life-span") or {}

    st.markdown(f"## {name}")
    if disambiguation:
        st.caption(disambiguation)

    i1, i2, i3, i4 = st.columns(4)
    i1.metric("Τύπος", artist_type)
    i2.metric("Χώρα", country)
    i3.metric("Sort Name", " ")
    i3.caption(sort_name)
    i4.metric("Ενεργός από", life_span.get("begin") or "—")

    st.link_button(
        "Διόρθωση στο MusicBrainz",
        f"https://musicbrainz.org/artist/{mbid}/edit",
        width="stretch",
    )

    st.divider()

    # --- Aliases ----------------------------------------------------------
    st.markdown("Ονόματα & Aliases")
    alias_list = artist.get("alias-list") or []
    if alias_list:
        alias_rows = []
        for alias in alias_list:
            if not isinstance(alias, dict):
                continue
            alias_rows.append(
                {
                    "Όνομα": alias.get("alias") or alias.get("name") or "—",
                    "Τύπος": alias.get("type") or "—",
                    "Sort Name": alias.get("sort-name") or "—",
                    "Γλώσσα/Locale": alias.get("locale") or "—",
                    "Primary": (
                        "✅"
                        if str(alias.get("primary") or "").lower() == "primary"
                        else ""
                    ),
                }
            )
        st.dataframe(pd.DataFrame(alias_rows), width="stretch", hide_index=True)
    else:
        st.warning(
            "Δεν έχουν καταχωρηθεί aliases. Το legal name / performance "
            "name δεν είναι διακριτά — χρειάζεται χειροκίνητη καταχώρηση."
        )

    st.divider()

    # --- External links ---------------------------------------------------
    st.markdown("Εξωτερικοί Σύνδεσμοι")
    url_rels = artist.get("url-relation-list") or []
    if url_rels:
        link_rows = []
        for rel in url_rels:
            if not isinstance(rel, dict):
                continue
            target = rel.get("target")
            if isinstance(target, dict):
                target = target.get("resource")
            link_rows.append(
                {
                    "Τύπος": str(rel.get("type") or "—").title(),
                    "URL": target or "—",
                }
            )
        st.dataframe(
            pd.DataFrame(link_rows),
            width="stretch",
            hide_index=True,
            column_config={
                "URL": st.column_config.LinkColumn(
                    "URL",
                    display_text="Άνοιγμα 🔗",
                ),
            },
        )
    else:
        st.warning("Κανένα εξωτερικό link (socials / streaming) καταχωρημένο.")

    st.divider()

    # --- Discography ------------------------------------------------------
    st.markdown("Δισκογραφία")
    releases = artist.get("release-list") or []
    if releases:
        release_rows = []
        for rel in releases:
            if not isinstance(rel, dict):
                continue
            release_rows.append(
                {
                    "Τίτλος": rel.get("title") or "—",
                    "Ημερομηνία": rel.get("date") or "—",
                    "Χώρα": rel.get("country") or "—",
                    "Status": rel.get("status") or "—",
                    "Barcode": rel.get("barcode") or "—",
                    "MBID": rel.get("id") or "—",
                }
            )
        release_rows.sort(key=lambda row: row["Ημερομηνία"], reverse=True)
        st.dataframe(pd.DataFrame(release_rows), width="stretch", hide_index=True)
        st.caption(
            f"{len(release_rows)} releases. Το MusicBrainz lookup επιστρέφει "
            "μέχρι 25 ανά κλήση — πλήρης δισκογραφία απαιτεί browse με pagination."
        )
    else:
        st.warning("Δεν βρέθηκαν releases συνδεδεμένα με αυτόν τον καλλιτέχνη.")

    st.divider()

    # --- Relationships ----------------------------------------------------
    st.markdown("Σχέσεις (Relationships)")
    artist_rels = artist.get("artist-relation-list") or []
    work_rels = artist.get("work-relation-list") or []

    rel_rows = []
    for rel in artist_rels:
        if not isinstance(rel, dict):
            continue
        target = rel.get("artist") or {}
        rel_rows.append(
            {
                "Κατηγορία": "Artist",
                "Ρόλος": str(rel.get("type") or "—").title(),
                "Κατεύθυνση": rel.get("direction") or "—",
                "Συνδεδεμένο με": target.get("name") or "—",
            }
        )

    for rel in work_rels:
        if not isinstance(rel, dict):
            continue
        target = rel.get("work") or {}
        rel_rows.append(
            {
                "Κατηγορία": "Work",
                "Ρόλος": str(rel.get("type") or "—").title(),
                "Κατεύθυνση": rel.get("direction") or "—",
                "Συνδεδεμένο με": target.get("title") or "—",
            }
        )

    if rel_rows:
        r1, r2 = st.columns(2)
        r1.metric("Artist Relationships", len(artist_rels))
        r2.metric("Work Relationships", len(work_rels))
        st.dataframe(pd.DataFrame(rel_rows), width="stretch", hide_index=True)
    else:
        st.warning(
            "**Κανένα relationship καταχωρημένο.** Το προφίλ δεν έχει "
            "συνδέσεις με works (composer / lyricist / arranger) ούτε με "
            "άλλους καλλιτέχνες (member of, collaborator, producer). "
            "Αυτό σημαίνει ότι **οι δημιουργοί δεν είναι ανιχνεύσιμοι "
            "αυτόματα** από PROs και metadata aggregators — απαιτείται "
            "χειροκίνητη επιμέλεια στο MusicBrainz."
        )
        st.link_button(
            "Προσθήκη relationships τώρα",
            f"https://musicbrainz.org/artist/{mbid}/edit",
            type="primary",
            width="stretch",
        )


# --------------------------------------------------------------------------
# Recording → Work rendering
# --------------------------------------------------------------------------
def _render_recordings(recordings, success_message=None):
    if success_message:
        st.success(success_message)

    st.divider()

    for rec_index, recording in enumerate(recordings, start=1):
        if not isinstance(recording, dict):
            continue

        rec_id = recording.get("id") or ""
        if rec_id and recording.get("_work_rels_unavailable"):
            try:
                with st.spinner("Fetching Recording relationships..."):
                    recording_full = mb_get_recording(rec_id)
                if recording_full:
                    recording = {**recording, **recording_full}
            except musicbrainzngs.MusicBrainzError as exc:
                st.warning(
                    "Η ηχογράφηση βρέθηκε, αλλά το relationship lookup "
                    f"απέτυχε: {mb_error_message(exc)}"
                )
            except Exception as exc:
                st.warning(f"Αποτυχία relationship lookup: {exc}")

        rec_title = recording.get("title") or "—"
        rec_id = recording.get("id") or rec_id
        performers = mb_artist_credit_phrase(recording) or "—"

        st.markdown(f"{rec_index}. {rec_title}")

        d1, d2, d3 = st.columns(3)
        d1.markdown("**Ερμηνευτές**")
        d1.write(performers)
        d2.markdown("**Διάρκεια**")
        d2.write(mb_format_length(recording.get("length")))
        d3.markdown("**Recording MBID**")
        d3.code(rec_id or "—", language=None)

        isrc_list = recording.get("isrc-list") or []
        if isinstance(isrc_list, list) and isrc_list:
            st.caption(
                "ISRC(s): "
                + ", ".join(str(value) for value in isrc_list if str(value).strip())
            )

        work_rels = recording.get("work-relation-list") or []
        linked_works = [
            rel.get("work")
            for rel in work_rels
            if isinstance(rel, dict) and isinstance(rel.get("work"), dict)
        ]

        if not linked_works:
            st.error(
                "**Καμία σύνδεση με Work.** Η ηχογράφηση δεν είναι "
                "συνδεδεμένη με σύνθεση, άρα δεν υπάρχει ISWC ούτε "
                "ανιχνεύσιμοι composers. Κρίσιμο κενό για publishing."
            )
            if rec_id:
                st.link_button(
                    "Σύνδεση με Work στο MusicBrainz",
                    f"https://musicbrainz.org/recording/{rec_id}/edit",
                    width="stretch",
                )
        else:
            for work_stub in linked_works:
                work_id = work_stub.get("id")
                work_title = work_stub.get("title") or "—"

                work_full = {}
                if work_id:
                    try:
                        with st.spinner("Fetching Work from MusicBrainz..."):
                            work_full = mb_get_work(work_id)
                    except musicbrainzngs.MusicBrainzError as exc:
                        st.warning(
                            "Το Work stub βρέθηκε αλλά το πλήρες lookup "
                            f"απέτυχε: {mb_error_message(exc)}"
                        )
                    except Exception as exc:
                        st.warning(f"Αποτυχία lookup του Work: {exc}")

                merged_work = {**work_stub, **(work_full or {})}
                iswc = mb_iswc(merged_work)

                with st.container(border=True):
                    st.markdown(
                        f"##### 🎼 Work: {work_full.get('title') or work_title}"
                    )

                    w1, w2 = st.columns(2)
                    with w1:
                        st.markdown("**ISWC**")
                        if iswc:
                            st.code(iswc, language=None)
                        else:
                            st.error("Δεν έχει καταχωρηθεί ISWC")
                    with w2:
                        st.markdown("**Work MBID**")
                        st.code(work_id or "—", language=None)

                    writer_rels = merged_work.get("artist-relation-list") or []
                    writer_rows = []
                    for rel in writer_rels:
                        if not isinstance(rel, dict):
                            continue
                        person = rel.get("artist") or {}
                        writer_rows.append(
                            {
                                "Ρόλος": str(rel.get("type") or "—").title(),
                                "Όνομα": person.get("name") or "—",
                                "Legal / Sort Name": person.get("sort-name") or "—",
                                "Artist MBID": person.get("id") or "—",
                            }
                        )

                    st.markdown("**Δημιουργοί (Composers / Lyricists)**")
                    if writer_rows:
                        st.dataframe(
                            pd.DataFrame(writer_rows),
                            width="stretch",
                            hide_index=True,
                        )
                    else:
                        st.warning(
                            "Το Work δεν έχει συνδεδεμένους composers / "
                            "lyricists — δεν μπορεί να επιβεβαιωθεί η "
                            "πατρότητα του έργου."
                        )

                    if work_id:
                        st.link_button(
                            "Επεξεργασία Work",
                            f"https://musicbrainz.org/work/{work_id}/edit",
                            width="stretch",
                        )

        if rec_index < len(recordings):
            st.divider()


# --------------------------------------------------------------------------
# Release rendering
# --------------------------------------------------------------------------
def _render_release(release, release_id, fallback_barcode=""):
    st.divider()

    st.markdown(f"{release.get('title') or '—'}")
    st.caption(mb_artist_credit_phrase(release) or "—")

    label_names = []
    catalog_numbers = []
    for info in release.get("label-info-list") or []:
        if not isinstance(info, dict):
            continue
        label = info.get("label") or {}
        if label.get("name"):
            label_names.append(str(label["name"]))
        if info.get("catalog-number"):
            catalog_numbers.append(str(info["catalog-number"]))

    b1, b2, b3, b4 = st.columns(4)
    b1.metric("Ημ. Κυκλοφορίας", release.get("date") or "—")
    b2.metric("Χώρα", release.get("country") or "—")
    b3.metric("Status", release.get("status") or "—")
    b4.metric("Barcode", release.get("barcode") or fallback_barcode or "—")

    l1, l2 = st.columns(2)
    with l1:
        st.markdown("**Label**")
        st.write(", ".join(label_names) if label_names else "—")
        if not label_names:
            st.caption("Δεν έχει καταχωρηθεί δισκογραφική.")
    with l2:
        st.markdown("**Catalog Number**")
        st.write(", ".join(catalog_numbers) if catalog_numbers else "—")

    if release_id:
        st.link_button(
            "Προβολή στο MusicBrainz",
            f"https://musicbrainz.org/release/{release_id}",
            width="stretch",
        )

    st.divider()

    st.markdown("Tracklist")
    media_list = release.get("medium-list") or []
    track_rows = []

    for medium in media_list:
        if not isinstance(medium, dict):
            continue
        medium_format = medium.get("format") or "Medium"
        medium_position = medium.get("position") or ""
        for track in medium.get("track-list") or []:
            if not isinstance(track, dict):
                continue
            rec = track.get("recording") or {}
            track_rows.append(
                {
                    "Δίσκος": f"{medium_format} {medium_position}".strip(),
                    "#": track.get("number") or track.get("position") or "—",
                    "Τίτλος": rec.get("title") or track.get("title") or "—",
                    "Διάρκεια": mb_format_length(
                        track.get("length") or rec.get("length")
                    ),
                    "Recording MBID": rec.get("id") or "—",
                }
            )

    if track_rows:
        st.dataframe(pd.DataFrame(track_rows), width="stretch", hide_index=True)
        st.caption(
            f"Σύνολο: {len(track_rows)} tracks σε {len(media_list)} medium(s)."
        )
    else:
        st.warning(
            "Η κυκλοφορία υπάρχει αλλά **δεν έχει tracklist**. "
            "Χρειάζεται καταχώρηση των κομματιών για να είναι χρήσιμη."
        )


# --------------------------------------------------------------------------
# The page
# --------------------------------------------------------------------------
def page_musicbrainz():
    handoff_notice = _consume_explorer_handoff()

    st.title("MusicBrainz Explorer")
    st.caption(
        "Deep metadata extraction από την open-source μουσική εγκυκλοπαίδεια. "
        "Τα αποτελέσματα αποθηκεύονται προσωρινά για 1 ώρα."
    )

    _render_handoff_notice(handoff_notice)

    tab_artist, tab_isrc, tab_barcode = st.tabs(
        ["Artist Auditor", "ISRC / ISWC Resolver", "Catalog Barcode Scanner"]
    )

    # ======================================================================
    # TAB 1 — Artist Auditor
    # ======================================================================
    with tab_artist:
        st.markdown("Έλεγχος προφίλ καλλιτέχνη")
        st.caption(
            "Επικολλήστε MusicBrainz Artist ID ή ολόκληρο URL — "
            "το MBID εξάγεται αυτόματα."
        )

        artist_input = st.text_input(
            "MusicBrainz Artist ID ή URL",
            placeholder=(
                "https://musicbrainz.org/artist/"
                "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
            ),
            key="mb_artist_input",
        )

        artist_lookup_mbid = st.session_state.pop(
            "mb_artist_pending_lookup",
            None,
        )

        if st.button(
            "Έλεγχος Καλλιτέχνη",
            type="primary",
            width="stretch",
            key="mb_artist_btn",
        ):
            artist_lookup_mbid = extract_mbid(artist_input)
            if not artist_lookup_mbid:
                st.error(
                    "Δεν βρέθηκε έγκυρο MBID. Χρειάζεται UUID της μορφής "
                    "`xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`."
                )

        selected_artist = _render_search_fallback(
            entity_type="artist",
            expander_label="🔎 Δεν έχετε MBID; Αναζητήστε καλλιτέχνη",
            input_label="Όνομα καλλιτέχνη",
            placeholder="π.χ. Queen",
            key_prefix="mb_artist_search",
            action_label="Χρήση στο Artist Auditor",
        )
        if selected_artist:
            artist_lookup_mbid = extract_mbid(selected_artist.get("id"))

        if artist_lookup_mbid:
            try:
                with st.spinner("Fetching from MusicBrainz..."):
                    artist = mb_get_artist(artist_lookup_mbid)
            except musicbrainzngs.MusicBrainzError as exc:
                st.error(mb_error_message(exc))
                artist = None
            except Exception as exc:
                st.error(f"Μη αναμενόμενο σφάλμα: {exc}")
                artist = None

            if artist:
                _render_artist_audit(artist, artist_lookup_mbid)

    # ======================================================================
    # TAB 2 — ISRC / ISWC Resolver
    # ======================================================================
    with tab_isrc:
        st.markdown("### Recording → Work resolution")
        st.caption(
            "Το ISRC ταυτοποιεί την **ηχογράφηση**. Το ISWC ταυτοποιεί τη "
            "**σύνθεση**. Αυτή η γέφυρα είναι που πληρώνει τους writers."
        )

        isrc_input = st.text_input(
            "ISRC",
            placeholder="π.χ. GBAYE0601498",
            key="mb_isrc_input",
        )

        recording_lookup_mbid = st.session_state.pop(
            "mb_recording_pending_lookup",
            None,
        )
        isrc_to_lookup = None

        if st.button(
            "Ανάλυση ISRC",
            type="primary",
            width="stretch",
            key="mb_isrc_btn",
        ):
            clean_isrc = str(isrc_input or "").replace("-", "").strip().upper()
            recording_lookup_mbid = None

            if not clean_isrc:
                st.warning("Εισάγετε ένα ISRC.")
            elif not validate_isrc(clean_isrc):
                st.error(
                    f"Το `{clean_isrc}` δεν έχει έγκυρη μορφή ISRC "
                    "(CC-XXX-YY-NNNNN)."
                )
            else:
                isrc_to_lookup = clean_isrc

        selected_recording = _render_search_fallback(
            entity_type="recording",
            expander_label="🔎 Δεν έχετε ISRC; Αναζητήστε ηχογράφηση",
            input_label="Τίτλος ηχογράφησης",
            placeholder="π.χ. Imagine John Lennon",
            key_prefix="mb_recording_search",
            action_label="Χρήση στο ISRC / ISWC Resolver",
        )
        if selected_recording:
            recording_lookup_mbid = extract_mbid(selected_recording.get("id"))
            isrc_to_lookup = None

        if isrc_to_lookup:
            try:
                with st.spinner("Fetching from MusicBrainz..."):
                    isrc_data = mb_get_recordings_by_isrc(isrc_to_lookup)
            except musicbrainzngs.MusicBrainzError as exc:
                st.error(mb_error_message(exc))
                isrc_data = None
            except Exception as exc:
                st.error(f"Μη αναμενόμενο σφάλμα: {exc}")
                isrc_data = None

            if isrc_data is not None:
                recordings = isrc_data.get("recording-list") or []

                if not recordings:
                    st.warning(
                        f"Το ISRC `{isrc_to_lookup}` δεν αντιστοιχεί σε καμία "
                        "ηχογράφηση στο MusicBrainz. Πιθανόν να μην έχει "
                        "ευρετηριαστεί ακόμα."
                    )
                else:
                    _render_recordings(
                        recordings,
                        success_message=(
                            f"Βρέθηκαν {len(recordings)} ηχογραφήσεις για το "
                            f"`{isrc_to_lookup}`."
                        ),
                    )

        elif recording_lookup_mbid:
            try:
                with st.spinner("Fetching Recording from MusicBrainz..."):
                    recording = mb_get_recording(recording_lookup_mbid)
            except musicbrainzngs.MusicBrainzError as exc:
                st.error(mb_error_message(exc))
                recording = None
            except Exception as exc:
                st.error(f"Μη αναμενόμενο σφάλμα: {exc}")
                recording = None

            if recording:
                _render_recordings(
                    [recording],
                    success_message=(
                        "Η επιλεγμένη ηχογράφηση φορτώθηκε με Recording MBID."
                    ),
                )

    # ======================================================================
    # TAB 3 — Catalog Barcode Scanner
    # ======================================================================
    with tab_barcode:
        st.markdown("### Barcode reconciliation (UPC / EAN)")
        st.caption(
            "Ελέγξτε αν μια κυκλοφορία της δισκογραφικής είναι σωστά "
            "ευρετηριασμένη στη διεθνή βάση."
        )

        barcode_input = st.text_input(
            "Barcode (UPC / EAN)",
            placeholder="π.χ. 5099749534728",
            key="mb_barcode_input",
        )

        release_lookup_mbid = st.session_state.pop(
            "mb_release_pending_lookup",
            None,
        )
        release_lookup_stub = st.session_state.pop(
            "mb_release_pending_stub",
            None,
        )
        barcode_to_lookup = None

        if st.button(
            "Σάρωση Barcode",
            type="primary",
            width="stretch",
            key="mb_barcode_btn",
        ):
            barcode = re.sub(r"\D+", "", str(barcode_input or ""))
            release_lookup_mbid = None
            release_lookup_stub = None

            if not barcode:
                st.warning("Εισάγετε ένα barcode (μόνο ψηφία).")
            else:
                barcode_to_lookup = barcode

        selected_release = _render_search_fallback(
            entity_type="release",
            expander_label="🔎 Δεν έχετε barcode; Αναζητήστε κυκλοφορία",
            input_label="Τίτλος κυκλοφορίας",
            placeholder="π.χ. Abbey Road The Beatles",
            key_prefix="mb_release_search",
            action_label="Χρήση στο Catalog Barcode Scanner",
        )
        if selected_release:
            release_lookup_mbid = extract_mbid(selected_release.get("id"))
            release_lookup_stub = selected_release
            barcode_to_lookup = None

        if barcode_to_lookup:
            try:
                with st.spinner("Fetching from MusicBrainz..."):
                    results = mb_search_releases_by_barcode(barcode_to_lookup)
            except musicbrainzngs.MusicBrainzError as exc:
                st.error(mb_error_message(exc))
                results = None
            except Exception as exc:
                st.error(f"Μη αναμενόμενο σφάλμα: {exc}")
                results = None

            if results is not None:
                if not results:
                    st.error(
                        f"Το barcode `{barcode_to_lookup}` **δεν υπάρχει** στο "
                        "MusicBrainz. Η κυκλοφορία δεν είναι ευρετηριασμένη "
                        "διεθνώς — χάνεται σε aggregators και μουσικές εφαρμογές."
                    )
                else:
                    top = results[0]
                    release_id = top.get("id")
                    release = top

                    try:
                        if release_id:
                            with st.spinner("Fetching full Release from MusicBrainz..."):
                                full = mb_get_release(release_id)
                            if full:
                                release = {**top, **full}
                    except musicbrainzngs.MusicBrainzError as exc:
                        st.warning(
                            "Βρέθηκε το release αλλά το πλήρες lookup απέτυχε: "
                            f"{mb_error_message(exc)}"
                        )
                    except Exception as exc:
                        st.warning(f"Αποτυχία πλήρους lookup: {exc}")

                    if len(results) > 1:
                        st.info(
                            f"Βρέθηκαν {len(results)} releases με αυτό το barcode. "
                            "Εμφανίζεται το κορυφαίο αποτέλεσμα."
                        )

                    _render_release(
                        release,
                        release_id,
                        fallback_barcode=barcode_to_lookup,
                    )

        elif release_lookup_mbid:
            release = (
                release_lookup_stub
                if isinstance(release_lookup_stub, dict)
                else {}
            )

            try:
                with st.spinner("Fetching Release from MusicBrainz..."):
                    full_release = mb_get_release(release_lookup_mbid)
                if full_release:
                    release = {**release, **full_release}
            except musicbrainzngs.MusicBrainzError as exc:
                st.warning(
                    "Το release επιλέχθηκε, αλλά το πλήρες lookup απέτυχε: "
                    f"{mb_error_message(exc)}"
                )
            except Exception as exc:
                st.warning(f"Αποτυχία πλήρους lookup: {exc}")

            if release:
                st.success("Η επιλεγμένη κυκλοφορία φορτώθηκε με Release MBID.")
                _render_release(
                    release,
                    release_lookup_mbid,
                    fallback_barcode=str(release.get("barcode") or ""),
                )
            else:
                st.warning("Δεν ήταν δυνατή η φόρτωση της επιλεγμένης κυκλοφορίας.")
