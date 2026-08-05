"""
tools/page_musicbrainz.py

TOOL D: MusicBrainz Explorer — deep metadata extraction with Phase 1
search-first fallbacks AND Phase 3 Authenticated Actions (Tags, Ratings, Submissions).
"""

import hashlib
import re

import musicbrainzngs
import pandas as pd
import streamlit as st

from core.auth_musicbrainz import init_mb_auth, is_mb_authenticated
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
    mb_submit_tags,
    mb_submit_rating,
    mb_submit_isrcs,
    mb_submit_barcodes,
    mb_build_seeded_url,
)
from utils.tidal_api import validate_isrc

SEARCH_RESULT_LIMIT = 10

# Initialize Auth silently if credentials exist
if not is_mb_authenticated():
    init_mb_auth()


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
        if begin and end: return f"{begin} → {end}"
        if begin: return f"{begin} →"
        if end: return f"→ {end}"
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
    if artist: parts.append(artist)
    if date_value != "—": parts.append(date_value)
    if country != "—": parts.append(country)
    if disambiguation: parts.append(disambiguation)
    label = " — ".join(parts)
    return label if len(label) <= 220 else f"{label[:217]}..."

def _search_state_fingerprint(entity_type, query, results):
    raw = f"{entity_type}|{query}|{len(results)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]

def _render_search_fallback(*, entity_type, expander_label, input_label, placeholder, key_prefix, action_label):
    selected_for_pipeline = None
    state_key = f"{key_prefix}_state"

    with st.expander(expander_label):
        with st.form(f"{key_prefix}_form", clear_on_submit=False):
            query = st.text_input(input_label, placeholder=placeholder, key=f"{key_prefix}_query")
            strict = st.checkbox("Όλοι οι όροι να ταιριάζουν", value=False, key=f"{key_prefix}_strict")
            submitted = st.form_submit_button("🔎 Αναζήτηση", width="stretch")

        if submitted:
            clean_query = " ".join(str(query or "").split()).strip()
            if not clean_query:
                st.warning("Εισάγετε όνομα ή τίτλο για αναζήτηση.")
                st.session_state.pop(state_key, None)
            else:
                try:
                    with st.spinner("Αναζήτηση στο MusicBrainz..."):
                        results = mb_search_entities(entity_type=entity_type, query=clean_query, limit=SEARCH_RESULT_LIMIT, strict=strict, lucene_query=False)
                    st.session_state[state_key] = {"query": clean_query, "results": results}
                except musicbrainzngs.MusicBrainzError as exc:
                    st.session_state.pop(state_key, None)
                    st.error(mb_error_message(exc))
                except Exception as exc:
                    st.session_state.pop(state_key, None)
                    st.error(f"Μη αναμενόμενο σφάλμα: {exc}")

        state = st.session_state.get(state_key)
        if not state:
            return None

        results = [r for r in state.get("results") or [] if isinstance(r, dict)]
        if not results:
            st.warning("Δεν βρέθηκαν αποτελέσματα.")
            return None

        rows = [_search_result_row(entity_type, result, position) for position, result in enumerate(results, start=1)]
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

        fingerprint = _search_state_fingerprint(entity_type, state.get("query"), results)
        selected_index = st.selectbox(
            "Επιλέξτε το σωστό αποτέλεσμα",
            options=list(range(len(results))),
            format_func=lambda index: _search_selection_label(entity_type, results[index], index + 1),
            key=f"{key_prefix}_selection_{fingerprint}",
        )
        selected = results[selected_index]
        selected_mbid = str(selected.get("id") or "").strip()

        action_columns = st.columns(2)
        with action_columns[0]:
            st.link_button("🔗 Προβολή στο MusicBrainz", mb_entity_url(entity_type, selected_mbid), width="stretch")
        with action_columns[1]:
            if st.button(action_label, type="primary", width="stretch", key=f"{key_prefix}_use_{selected_mbid}"):
                if not selected_mbid:
                    st.error("Το επιλεγμένο αποτέλεσμα δεν έχει έγκυρο MBID.")
                else:
                    selected_for_pipeline = selected

    return selected_for_pipeline


def _consume_explorer_handoff():
    handoff = st.session_state.pop("mb_explorer_handoff", None)
    if not isinstance(handoff, dict):
        return None
    entity_type = str(handoff.get("entity_type") or "").strip().lower()
    mbid = extract_mbid(handoff.get("mbid"))
    result = handoff.get("result") if isinstance(handoff.get("result"), dict) else {}

    if not mbid:
        return {"level": "error", "message": "Η επιλογή δεν είχε έγκυρο MBID."}

    title = _entity_title(entity_type, result)

    if entity_type == "artist":
        st.session_state["mb_artist_input"] = mbid
        st.session_state["mb_artist_pending_lookup"] = mbid
        return {"level": "success", "message": f"Ο καλλιτέχνης **{title}** μεταφέρθηκε."}

    if entity_type == "recording":
        isrc_list = result.get("isrc-list") or []
        if isinstance(isrc_list, list) and isrc_list:
            st.session_state["mb_isrc_input"] = str(isrc_list[0])
        st.session_state["mb_recording_pending_lookup"] = mbid
        return {"level": "success", "message": f"Η ηχογράφηση **{title}** μεταφέρθηκε."}

    if entity_type == "release":
        barcode = re.sub(r"\D+", "", str(result.get("barcode") or ""))
        if barcode: st.session_state["mb_barcode_input"] = barcode
        st.session_state["mb_release_pending_lookup"] = mbid
        st.session_state["mb_release_pending_stub"] = result
        return {"level": "success", "message": f"Η κυκλοφορία **{title}** μεταφέρθηκε."}

    return {"level": "warning", "message": "Το entity δεν αντιστοιχεί σε pipeline."}

def _render_handoff_notice(notice):
    if not notice: return
    level = notice.get("level")
    message = notice.get("message") or ""
    if level == "success": st.success(message)
    elif level == "warning": st.warning(message)
    else: st.error(message)

# --------------------------------------------------------------------------
# Auth Action Forms (Phase 3)
# --------------------------------------------------------------------------
def _render_auth_actions(entity_type, mbid):
    """Renders Rating and Tagging widgets if authenticated."""
    if not is_mb_authenticated():
        return
    
    with st.expander("⭐ / 🏷️ Αξιολόγηση & Ετικέτες (MB Account)"):
        c1, c2 = st.columns(2)
        with c1:
            rating = st.slider(f"Βαθμολογία", 0, 100, 50, step=20, key=f"rate_{mbid}")
            if st.button("Υποβολή Βαθμολογίας", key=f"btn_rate_{mbid}"):
                try:
                    with st.spinner("Υποβολή..."):
                        mb_submit_rating(entity_type, mbid, rating)
                    st.toast("Η βαθμολογία υποβλήθηκε!", icon="✅")
                except Exception as e:
                    st.error(f"Σφάλμα: {e}")
        
        with c2:
            tags_input = st.text_input("Ετικέτες (comma separated)", placeholder="π.χ. rock, pop", key=f"tag_{mbid}")
            if st.button("Υποβολή Ετικετών", key=f"btn_tag_{mbid}"):
                try:
                    tags_list = [t.strip() for t in tags_input.split(",") if t.strip()]
                    with st.spinner("Υποβολή..."):
                        mb_submit_tags(entity_type, mbid, tags_list)
                    st.toast("Οι ετικέτες υποβλήθηκαν!", icon="✅")
                except Exception as e:
                    st.error(f"Σφάλμα: {e}")

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
    if disambiguation: st.caption(disambiguation)

    i1, i2, i3, i4 = st.columns(4)
    i1.metric("Τύπος", artist_type)
    i2.metric("Χώρα", country)
    i3.metric("Sort Name", " ")
    i3.caption(sort_name)
    i4.metric("Ενεργός από", life_span.get("begin") or "—")

    act_col1, act_col2 = st.columns(2)
    with act_col1:
        st.link_button("Διόρθωση στο MusicBrainz", f"https://musicbrainz.org/artist/{mbid}/edit", width="stretch")
    with act_col2:
        seeded_alias_url = f"https://musicbrainz.org/artist/{mbid}/add-alias"
        st.link_button("➕ Προσθήκη Alias (Seeded)", seeded_alias_url, width="stretch")

    _render_auth_actions("artist", mbid)

    st.divider()

    st.markdown("Ονόματα & Aliases")
    alias_list = artist.get("alias-list") or []
    if alias_list:
        alias_rows = []
        for alias in alias_list:
            if not isinstance(alias, dict): continue
            alias_rows.append({
                "Όνομα": alias.get("alias") or alias.get("name") or "—",
                "Τύπος": alias.get("type") or "—",
                "Sort Name": alias.get("sort-name") or "—",
                "Γλώσσα": alias.get("locale") or "—",
                "Primary": "✅" if str(alias.get("primary") or "").lower() == "primary" else "",
            })
        st.dataframe(pd.DataFrame(alias_rows), width="stretch", hide_index=True)
    else:
        st.warning("Δεν έχουν καταχωρηθεί aliases.")

    st.divider()
    st.markdown("Εξωτερικοί Σύνδεσμοι")
    url_rels = artist.get("url-relation-list") or []
    if url_rels:
        link_rows = []
        for rel in url_rels:
            if not isinstance(rel, dict): continue
            target = rel.get("target")
            if isinstance(target, dict): target = target.get("resource")
            link_rows.append({"Τύπος": str(rel.get("type") or "—").title(), "URL": target or "—"})
        st.dataframe(pd.DataFrame(link_rows), width="stretch", hide_index=True,
                     column_config={"URL": st.column_config.LinkColumn("URL", display_text="Άνοιγμα 🔗")})
    else:
        st.warning("Κανένα εξωτερικό link καταχωρημένο.")

    st.divider()
    st.markdown("Δισκογραφία (Top 25)")
    releases = artist.get("release-list") or []
    if releases:
        release_rows = []
        for rel in releases:
            if not isinstance(rel, dict): continue
            release_rows.append({
                "Τίτλος": rel.get("title") or "—",
                "Ημερομηνία": rel.get("date") or "—",
                "Χώρα": rel.get("country") or "—",
                "Status": rel.get("status") or "—",
                "Barcode": rel.get("barcode") or "—",
                "MBID": rel.get("id") or "—",
            })
        release_rows.sort(key=lambda row: row["Ημερομηνία"], reverse=True)
        st.dataframe(pd.DataFrame(release_rows), width="stretch", hide_index=True)
    else:
        st.warning("Δεν βρέθηκαν releases.")

    st.divider()
    st.markdown("Σχέσεις (Relationships)")
    artist_rels = artist.get("artist-relation-list") or []
    work_rels = artist.get("work-relation-list") or []

    rel_rows = []
    for rel in artist_rels:
        if not isinstance(rel, dict): continue
        target = rel.get("artist") or {}
        rel_rows.append({
            "Κατηγορία": "Artist",
            "Ρόλος": str(rel.get("type") or "—").title(),
            "Κατεύθυνση": rel.get("direction") or "—",
            "Συνδεδεμένο με": target.get("name") or "—",
        })

    for rel in work_rels:
        if not isinstance(rel, dict): continue
        target = rel.get("work") or {}
        rel_rows.append({
            "Κατηγορία": "Work",
            "Ρόλος": str(rel.get("type") or "—").title(),
            "Κατεύθυνση": rel.get("direction") or "—",
            "Συνδεδεμένο με": target.get("title") or "—",
        })

    if rel_rows:
        r1, r2 = st.columns(2)
        r1.metric("Artist Relationships", len(artist_rels))
        r2.metric("Work Relationships", len(work_rels))
        st.dataframe(pd.DataFrame(rel_rows), width="stretch", hide_index=True)
    else:
        st.warning("Κανένα relationship καταχωρημένο.")
        st.link_button("Προσθήκη relationships τώρα", f"https://musicbrainz.org/artist/{mbid}/edit", type="primary", width="stretch")


# --------------------------------------------------------------------------
# Recording → Work rendering
# --------------------------------------------------------------------------
def _render_recordings(recordings, success_message=None, lookup_isrc=None):
    if success_message:
        st.success(success_message)

    st.divider()

    for rec_index, recording in enumerate(recordings, start=1):
        if not isinstance(recording, dict): continue

        rec_id = recording.get("id") or ""
        if rec_id and recording.get("_work_rels_unavailable"):
            try:
                with st.spinner("Fetching Recording relationships..."):
                    recording_full = mb_get_recording(rec_id)
                if recording_full: recording = {**recording, **recording_full}
            except musicbrainzngs.MusicBrainzError as exc:
                st.warning(f"Αποτυχία relationship lookup: {mb_error_message(exc)}")

        rec_title = recording.get("title") or "—"
        performers = mb_artist_credit_phrase(recording) or "—"

        st.markdown(f"#### {rec_index}. {rec_title}")
        d1, d2, d3 = st.columns(3)
        d1.markdown("**Ερμηνευτές**")
        d1.write(performers)
        d2.markdown("**Διάρκεια**")
        d2.write(mb_format_length(recording.get("length")))
        d3.markdown("**Recording MBID**")
        d3.code(rec_id or "—", language=None)

        isrc_list = recording.get("isrc-list") or []
        
        # --- Phase 3: Submit ISRC Logic ---
        has_isrcs = isinstance(isrc_list, list) and len(isrc_list) > 0
        if has_isrcs:
            st.caption("Υπάρχοντα ISRC(s): " + ", ".join(str(v) for v in isrc_list if str(v).strip()))
        
        missing_searched_isrc = lookup_isrc and lookup_isrc not in isrc_list
        if is_mb_authenticated() and missing_searched_isrc and rec_id:
            st.info(f"Το ISRC `{lookup_isrc}` δεν είναι καταχωρημένο σε αυτή την ηχογράφηση.")
            if st.button("📤 Υποβολή αυτού του ISRC στο MusicBrainz", key=f"sub_isrc_{rec_id}_{lookup_isrc}"):
                try:
                    with st.spinner("Υποβολή ISRC..."):
                        mb_submit_isrcs(rec_id, [lookup_isrc])
                    st.success("Το ISRC υποβλήθηκε επιτυχώς!")
                except Exception as e:
                    st.error(f"Σφάλμα κατά την υποβολή: {e}")

        _render_auth_actions("recording", rec_id)

        work_rels = recording.get("work-relation-list") or []
        linked_works = [r.get("work") for r in work_rels if isinstance(r, dict) and isinstance(r.get("work"), dict)]

        if not linked_works:
            st.error("Καμία σύνδεση με Work (Σύνθεση).")
            w_col1, w_col2 = st.columns(2)
            if rec_id:
                w_col1.link_button("Σύνδεση με Work στο MusicBrainz", f"https://musicbrainz.org/recording/{rec_id}/edit", width="stretch")
            
            seeded_work_url = mb_build_seeded_url("work", "create", title=rec_title)
            w_col2.link_button("➕ Δημιουργία νέου Work (Seeded)", seeded_work_url, width="stretch")
        else:
            for work_stub in linked_works:
                work_id = work_stub.get("id")
                work_title = work_stub.get("title") or "—"
                work_full = {}
                if work_id:
                    try:
                        with st.spinner("Fetching Work from MusicBrainz..."):
                            work_full = mb_get_work(work_id)
                    except Exception as exc:
                        st.warning(f"Αποτυχία lookup του Work: {exc}")

                merged_work = {**work_stub, **(work_full or {})}
                iswc = mb_iswc(merged_work)

                with st.container(border=True):
                    st.markdown(f"##### 🎼 Work: {work_full.get('title') or work_title}")
                    w1, w2 = st.columns(2)
                    w1.markdown("**ISWC**"); w1.code(iswc, language=None) if iswc else w1.error("Χωρίς ISWC")
                    w2.markdown("**Work MBID**"); w2.code(work_id or "—", language=None)

                    writer_rels = merged_work.get("artist-relation-list") or []
                    writer_rows = []
                    for rel in writer_rels:
                        if not isinstance(rel, dict): continue
                        person = rel.get("artist") or {}
                        writer_rows.append({
                            "Ρόλος": str(rel.get("type") or "—").title(),
                            "Όνομα": person.get("name") or "—",
                            "Artist MBID": person.get("id") or "—",
                        })

                    st.markdown("**Δημιουργοί**")
                    if writer_rows:
                        st.dataframe(pd.DataFrame(writer_rows), width="stretch", hide_index=True)
                    else:
                        st.warning("Το Work δεν έχει συνδεδεμένους δημιουργούς.")

                    if work_id:
                        st.link_button("Επεξεργασία Work", f"https://musicbrainz.org/work/{work_id}/edit", width="stretch")

        if rec_index < len(recordings): st.divider()


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
        if not isinstance(info, dict): continue
        label = info.get("label") or {}
        if label.get("name"): label_names.append(str(label["name"]))
        if info.get("catalog-number"): catalog_numbers.append(str(info["catalog-number"]))

    b1, b2, b3, b4 = st.columns(4)
    b1.metric("Ημ. Κυκλοφορίας", release.get("date") or "—")
    b2.metric("Χώρα", release.get("country") or "—")
    b3.metric("Status", release.get("status") or "—")
    
    current_barcode = release.get("barcode")
    b4.metric("Barcode", current_barcode or fallback_barcode or "—")

    # --- Phase 3: Submit Barcode Logic ---
    if is_mb_authenticated() and not current_barcode and fallback_barcode and release_id:
        if st.button("📤 Υποβολή αυτού του Barcode", key=f"sub_barcode_{release_id}"):
            try:
                with st.spinner("Υποβολή Barcode..."):
                    mb_submit_barcodes(release_id, fallback_barcode)
                st.success("Το Barcode υποβλήθηκε επιτυχώς!")
            except Exception as e:
                st.error(f"Σφάλμα κατά την υποβολή: {e}")

    l1, l2 = st.columns(2)
    with l1:
        st.markdown("**Label**")
        st.write(", ".join(label_names) if label_names else "—")
    with l2:
        st.markdown("**Catalog Number**")
        st.write(", ".join(catalog_numbers) if catalog_numbers else "—")

    if release_id:
        st.link_button("Προβολή στο MusicBrainz", f"https://musicbrainz.org/release/{release_id}", width="stretch")
        _render_auth_actions("release", release_id)

    st.divider()
    st.markdown("Tracklist")
    media_list = release.get("medium-list") or []
    track_rows = []

    for medium in media_list:
        if not isinstance(medium, dict): continue
        medium_format = medium.get("format") or "Medium"
        medium_position = medium.get("position") or ""
        for track in medium.get("track-list") or []:
            if not isinstance(track, dict): continue
            rec = track.get("recording") or {}
            track_rows.append({
                "Δίσκος": f"{medium_format} {medium_position}".strip(),
                "#": track.get("number") or track.get("position") or "—",
                "Τίτλος": rec.get("title") or track.get("title") or "—",
                "Διάρκεια": mb_format_length(track.get("length") or rec.get("length")),
                "Recording MBID": rec.get("id") or "—",
            })

    if track_rows:
        st.dataframe(pd.DataFrame(track_rows), width="stretch", hide_index=True)
    else:
        st.warning("Η κυκλοφορία υπάρχει αλλά δεν έχει tracklist.")


# --------------------------------------------------------------------------
# The page
# --------------------------------------------------------------------------
def page_musicbrainz():
    handoff_notice = _consume_explorer_handoff()

    st.title("MusicBrainz Explorer")
    if is_mb_authenticated():
        st.success("🟢 Συνδεδεμένος στο MusicBrainz. Οι υποβολές (Tag/Rate/ISRC) είναι ενεργοποιημένες.")
    else:
        st.info("ℹ️ Δεν έχετε συνδεθεί στο MusicBrainz (ή λείπουν τα credentials). Οι λειτουργίες υποβολής είναι απενεργοποιημένες.")

    _render_handoff_notice(handoff_notice)

    tab_artist, tab_isrc, tab_barcode = st.tabs(["Artist Auditor", "ISRC / ISWC Resolver", "Catalog Barcode Scanner"])

    with tab_artist:
        st.markdown("Έλεγχος προφίλ καλλιτέχνη")
        c1, c2 = st.columns([3, 1])
        with c1:
            artist_input = st.text_input("MusicBrainz Artist ID ή URL", key="mb_artist_input")
        with c2:
            st.markdown("<br>", unsafe_allow_html=True)
            seeded_artist_url = mb_build_seeded_url("artist", "create")
            st.link_button("➕ Νέος Καλλιτέχνης", seeded_artist_url, width="stretch")

        artist_lookup_mbid = st.session_state.pop("mb_artist_pending_lookup", None)

        if st.button("Έλεγχος Καλλιτέχνη", type="primary", width="stretch", key="mb_artist_btn"):
            artist_lookup_mbid = extract_mbid(artist_input)
            if not artist_lookup_mbid: st.error("Δεν βρέθηκε έγκυρο MBID.")

        selected_artist = _render_search_fallback(
            entity_type="artist", expander_label="🔎 Δεν έχετε MBID; Αναζητήστε καλλιτέχνη",
            input_label="Όνομα καλλιτέχνη", placeholder="π.χ. Queen", key_prefix="mb_artist_search",
            action_label="Χρήση στο Artist Auditor"
        )
        if selected_artist: artist_lookup_mbid = extract_mbid(selected_artist.get("id"))

        if artist_lookup_mbid:
            try:
                with st.spinner("Fetching from MusicBrainz..."):
                    artist = mb_get_artist(artist_lookup_mbid)
            except Exception as exc:
                st.error(f"Σφάλμα: {exc}")
                artist = None
            if artist: _render_artist_audit(artist, artist_lookup_mbid)

    with tab_isrc:
        st.markdown("### Recording → Work resolution")
        isrc_input = st.text_input("ISRC", placeholder="π.χ. GBAYE0601498", key="mb_isrc_input")
        recording_lookup_mbid = st.session_state.pop("mb_recording_pending_lookup", None)
        isrc_to_lookup = None

        if st.button("Ανάλυση ISRC", type="primary", width="stretch", key="mb_isrc_btn"):
            clean_isrc = str(isrc_input or "").replace("-", "").strip().upper()
            recording_lookup_mbid = None
            if not clean_isrc: st.warning("Εισάγετε ένα ISRC.")
            elif not validate_isrc(clean_isrc): st.error("Μη έγκυρη μορφή ISRC.")
            else: isrc_to_lookup = clean_isrc

        selected_recording = _render_search_fallback(
            entity_type="recording", expander_label="🔎 Δεν έχετε ISRC; Αναζητήστε ηχογράφηση",
            input_label="Τίτλος ηχογράφησης", placeholder="π.χ. Imagine John Lennon", key_prefix="mb_recording_search",
            action_label="Χρήση στο ISRC Resolver"
        )
        if selected_recording:
            recording_lookup_mbid = extract_mbid(selected_recording.get("id"))
            isrc_to_lookup = None

        if isrc_to_lookup:
            try:
                with st.spinner("Fetching from MusicBrainz..."):
                    isrc_data = mb_get_recordings_by_isrc(isrc_to_lookup)
            except Exception as exc:
                st.error(f"Σφάλμα: {exc}")
                isrc_data = None
            
            if isrc_data:
                recordings = isrc_data.get("recording-list") or []
                if not recordings:
                    st.warning("Το ISRC δεν βρέθηκε. Αν έχετε το Recording MBID, αναζητήστε το παρακάτω και κάντε υποβολή.")
                else:
                    _render_recordings(recordings, f"Βρέθηκαν {len(recordings)} ηχογραφήσεις.", lookup_isrc=isrc_to_lookup)

        elif recording_lookup_mbid:
            try:
                with st.spinner("Fetching Recording from MusicBrainz..."):
                    recording = mb_get_recording(recording_lookup_mbid)
            except Exception as exc:
                st.error(f"Σφάλμα: {exc}")
                recording = None
            if recording:
                _render_recordings([recording], "Η ηχογράφηση φορτώθηκε.", lookup_isrc=str(isrc_input or "").upper())

    with tab_barcode:
        st.markdown("### Barcode reconciliation (UPC / EAN)")
        barcode_input = st.text_input("Barcode (UPC / EAN)", placeholder="π.χ. 5099749534728", key="mb_barcode_input")
        release_lookup_mbid = st.session_state.pop("mb_release_pending_lookup", None)
        release_lookup_stub = st.session_state.pop("mb_release_pending_stub", None)
        barcode_to_lookup = None

        if st.button("Σάρωση Barcode", type="primary", width="stretch", key="mb_barcode_btn"):
            barcode = re.sub(r"\D+", "", str(barcode_input or ""))
            release_lookup_mbid = release_lookup_stub = None
            if not barcode: st.warning("Εισάγετε barcode (μόνο ψηφία).")
            else: barcode_to_lookup = barcode

        selected_release = _render_search_fallback(
            entity_type="release", expander_label="🔎 Δεν έχετε barcode; Αναζητήστε κυκλοφορία",
            input_label="Τίτλος κυκλοφορίας", placeholder="π.χ. Abbey Road The Beatles", key_prefix="mb_release_search",
            action_label="Χρήση στο Barcode Scanner"
        )
        if selected_release:
            release_lookup_mbid = extract_mbid(selected_release.get("id"))
            release_lookup_stub = selected_release
            barcode_to_lookup = None

        if barcode_to_lookup:
            try:
                with st.spinner("Fetching from MusicBrainz..."):
                    results = mb_search_releases_by_barcode(barcode_to_lookup)
            except Exception as exc:
                st.error(f"Σφάλμα: {exc}")
                results = None
            if results is not None:
                if not results:
                    st.error("Το barcode δεν υπάρχει στο MusicBrainz.")
                    seeded_release_url = mb_build_seeded_url("release", "add", barcode=barcode_to_lookup)
                    st.link_button("➕ Δημιουργία Release (Seeded)", seeded_release_url)
                else:
                    top = results[0]
                    release_id = top.get("id")
                    release = top
                    if release_id:
                        try:
                            with st.spinner("Fetching full Release..."):
                                full = mb_get_release(release_id)
                            if full: release = {**top, **full}
                        except Exception as exc:
                            st.warning(f"Αποτυχία πλήρους lookup: {exc}")
                    _render_release(release, release_id, barcode_to_lookup)

        elif release_lookup_mbid:
            release = release_lookup_stub if isinstance(release_lookup_stub, dict) else {}
            try:
                with st.spinner("Fetching Release..."):
                    full = mb_get_release(release_lookup_mbid)
                if full: release = {**release, **full}
            except Exception as exc:
                st.warning(f"Αποτυχία πλήρους lookup: {exc}")
            if release:
                _render_release(release, release_lookup_mbid, str(release.get("barcode") or ""))
