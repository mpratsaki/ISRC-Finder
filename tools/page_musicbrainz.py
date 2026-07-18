"""
tools/page_musicbrainz.py

TOOL D: MusicBrainz Explorer — deep metadata extraction (Artist Auditor,
ISRC/ISWC Resolver, Catalog Barcode Scanner).

All network calls live in module-level cached helpers so Streamlit reruns
(tab switching, widget interaction) never re-hit the MusicBrainz servers.
musicbrainzngs enforces the 1 req/sec courtesy rate limit internally.
"""

import re

import musicbrainzngs
import pandas as pd
import streamlit as st

from utils.tidal_api import validate_isrc

# --------------------------------------------------------------------------
# MusicBrainz configuration
# MusicBrainz REQUIRES a descriptive User-Agent with contact info; requests
# without one get throttled or blocked outright. The library also enforces the
# 1 request/second rate limit for us automatically.
# --------------------------------------------------------------------------
MB_APP_NAME = "StayIndependentTool"
MB_APP_VERSION = "2.0"
MB_CONTACT = "johnnakas03@gmail.com"  # <-- ΑΛΛΑΞΕ ΤΟ σε πραγματικό email
MB_CACHE_TTL_SECONDS = 3600
MB_UUID_PATTERN = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

musicbrainzngs.set_useragent(MB_APP_NAME, MB_APP_VERSION, MB_CONTACT)


# --- MusicBrainz parsing helpers -----------------------------------------
def extract_mbid(raw_value):
    """
    Pulls a MusicBrainz UUID out of anything the user pastes:
    a bare MBID, a full https://musicbrainz.org/artist/<mbid> URL, a URL with
    query params or a trailing /releases path, etc. Returns None if absent.
    """
    text = str(raw_value or "").strip()
    if not text:
        return None
    match = MB_UUID_PATTERN.search(text)
    return match.group(0).lower() if match else None


def _mb_artist_credit_phrase(entity):
    """
    Flattens a MusicBrainz artist-credit list into a display string.
    The list interleaves dicts ({'artist': {...}, 'joinphrase': ' feat. '})
    with bare join strings, so handle both shapes.
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
        elif isinstance(credit, dict):
            artist = credit.get("artist") or {}
            parts.append(str(artist.get("name") or ""))
            if credit.get("joinphrase"):
                parts.append(str(credit["joinphrase"]))
    return "".join(parts).strip()


def _mb_iswc(work):
    """Works may expose a single 'iswc' or an 'iswc-list'. Normalise to a string."""
    if not isinstance(work, dict):
        return ""
    single = str(work.get("iswc") or "").strip()
    if single:
        return single
    iswc_list = work.get("iswc-list") or []
    if isinstance(iswc_list, list) and iswc_list:
        return ", ".join(str(x).strip() for x in iswc_list if str(x).strip())
    return ""


def _mb_format_length(milliseconds):
    """Converts a MusicBrainz length (ms, as a string) into m:ss."""
    try:
        total_seconds = int(int(milliseconds) / 1000)
    except (TypeError, ValueError):
        return "—"
    return f"{total_seconds // 60}:{total_seconds % 60:02d}"


# --- Cached MusicBrainz API calls ----------------------------------------
@st.cache_data(ttl=MB_CACHE_TTL_SECONDS, show_spinner=False)
def mb_get_artist(mbid):
    """Full artist lookup: discography, external links, relationships, aliases."""
    data = musicbrainzngs.get_artist_by_id(
        mbid,
        includes=["releases", "url-rels", "artist-rels", "work-rels", "aliases"],
    )
    return data.get("artist") or {}


@st.cache_data(ttl=MB_CACHE_TTL_SECONDS, show_spinner=False)
def mb_get_recordings_by_isrc(isrc):
    """Resolves an ISRC to its recording(s), including the linked Work(s)."""
    data = musicbrainzngs.get_recordings_by_isrc(
        isrc,
        includes=["work-rels", "artists"],
    )
    return data.get("isrc") or {}


@st.cache_data(ttl=MB_CACHE_TTL_SECONDS, show_spinner=False)
def mb_get_work(work_mbid):
    """
    Second-hop lookup. The work stub returned by a recording contains the ISWC
    but NOT the composer/lyricist credits — those need their own artist-rels
    lookup on the Work entity itself.
    """
    data = musicbrainzngs.get_work_by_id(work_mbid, includes=["artist-rels"])
    return data.get("work") or {}


@st.cache_data(ttl=MB_CACHE_TTL_SECONDS, show_spinner=False)
def mb_search_releases_by_barcode(barcode):
    """Barcode (UPC/EAN) search across the global release index."""
    data = musicbrainzngs.search_releases(barcode=barcode, limit=10)
    return data.get("release-list") or []


@st.cache_data(ttl=MB_CACHE_TTL_SECONDS, show_spinner=False)
def mb_get_release(release_mbid):
    """Full release lookup for label info and the complete tracklist."""
    data = musicbrainzngs.get_release_by_id(
        release_mbid,
        includes=["recordings", "labels", "artists", "media"],
    )
    return data.get("release") or {}


def _mb_error_message(exc):
    """Turns a musicbrainzngs exception into something a label manager can read."""
    if isinstance(exc, musicbrainzngs.ResponseError):
        cause = getattr(exc, "cause", None)
        code = getattr(cause, "code", None)
        if code == 404:
            return "Το MusicBrainz δεν βρήκε αυτή την εγγραφή (404). Ελέγξτε τον κωδικό."
        if code == 503:
            return "Το MusicBrainz επιστρέφει 503 (rate limit / υπερφόρτωση). Δοκιμάστε ξανά σε λίγο."
        return f"Μη έγκυρη απάντηση από το MusicBrainz: {exc}"
    if isinstance(exc, musicbrainzngs.NetworkError):
        return f"Αδυναμία σύνδεσης με το MusicBrainz: {exc}"
    return f"Σφάλμα MusicBrainz: {exc}"


# --- The page -------------------------------------------------------------
def page_musicbrainz():
    st.title("🧬 MusicBrainz Explorer")
    st.caption(
        "Deep metadata extraction από την open-source μουσική εγκυκλοπαίδεια. "
        "Τα αποτελέσματα αποθηκεύονται προσωρινά για 1 ώρα."
    )

    tab_artist, tab_isrc, tab_barcode = st.tabs(
        ["Artist Auditor", "ISRC / ISWC Resolver", "Catalog Barcode Scanner"]
    )

    # ======================================================================
    # TAB 1 — Artist Auditor
    # ======================================================================
    with tab_artist:
        st.markdown("### Έλεγχος προφίλ καλλιτέχνη")
        st.caption(
            "Επικολλήστε MusicBrainz Artist ID ή ολόκληρο URL — το MBID εξάγεται αυτόματα."
        )

        artist_input = st.text_input(
            "MusicBrainz Artist ID ή URL",
            placeholder="https://musicbrainz.org/artist/xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
            key="mb_artist_input",
        )

        if st.button("Έλεγχος Καλλιτέχνη", type="primary", width="stretch", key="mb_artist_btn"):
            mbid = extract_mbid(artist_input)

            if not mbid:
                st.error(
                    "Δεν βρέθηκε έγκυρο MBID. Χρειάζεται UUID της μορφής "
                    "`xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`."
                )
            else:
                try:
                    with st.spinner("Fetching from MusicBrainz..."):
                        artist = mb_get_artist(mbid)
                except (musicbrainzngs.ResponseError, musicbrainzngs.WebServiceError) as e:
                    st.error(_mb_error_message(e))
                    artist = None
                except Exception as e:
                    st.error(f"Μη αναμενόμενο σφάλμα: {e}")
                    artist = None

                if artist:
                    st.divider()

                    # --- Identity ---------------------------------------------
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
                        "✏️ Διόρθωση στο MusicBrainz",
                        f"https://musicbrainz.org/artist/{mbid}/edit",
                        width="stretch",
                    )

                    st.divider()

                    # --- Aliases ----------------------------------------------
                    st.markdown("### 🪪 Ονόματα & Aliases")
                    alias_list = artist.get("alias-list") or []
                    if alias_list:
                        alias_rows = []
                        for alias in alias_list:
                            if not isinstance(alias, dict):
                                continue
                            alias_rows.append({
                                "Όνομα": alias.get("alias") or alias.get("name") or "—",
                                "Τύπος": alias.get("type") or "—",
                                "Sort Name": alias.get("sort-name") or "—",
                                "Γλώσσα/Locale": alias.get("locale") or "—",
                                "Primary": "✅" if str(alias.get("primary") or "").lower() == "primary" else "",
                            })
                        st.dataframe(pd.DataFrame(alias_rows), width="stretch", hide_index=True)
                    else:
                        st.warning(
                            "⚠️ Δεν έχουν καταχωρηθεί aliases. Το legal name / performance "
                            "name δεν είναι διακριτά — χρειάζεται χειροκίνητη καταχώρηση."
                        )

                    st.divider()

                    # --- External links ---------------------------------------
                    st.markdown("### 🌐 Εξωτερικοί Σύνδεσμοι")
                    url_rels = artist.get("url-relation-list") or []
                    if url_rels:
                        link_rows = []
                        for rel in url_rels:
                            if not isinstance(rel, dict):
                                continue
                            target = rel.get("target")
                            if isinstance(target, dict):
                                target = target.get("resource")
                            link_rows.append({
                                "Τύπος": str(rel.get("type") or "—").title(),
                                "URL": target or "—",
                            })
                        st.dataframe(
                            pd.DataFrame(link_rows),
                            width="stretch",
                            hide_index=True,
                            column_config={
                                "URL": st.column_config.LinkColumn("URL", display_text="Άνοιγμα 🔗"),
                            },
                        )
                    else:
                        st.warning("⚠️ Κανένα εξωτερικό link (socials / streaming) καταχωρημένο.")

                    st.divider()

                    # --- Discography ------------------------------------------
                    st.markdown("### 📀 Δισκογραφία")
                    releases = artist.get("release-list") or []
                    if releases:
                        release_rows = []
                        for rel in releases:
                            if not isinstance(rel, dict):
                                continue
                            release_rows.append({
                                "Τίτλος": rel.get("title") or "—",
                                "Ημερομηνία": rel.get("date") or "—",
                                "Χώρα": rel.get("country") or "—",
                                "Status": rel.get("status") or "—",
                                "Barcode": rel.get("barcode") or "—",
                                "MBID": rel.get("id") or "—",
                            })
                        release_rows.sort(key=lambda r: r["Ημερομηνία"], reverse=True)
                        st.dataframe(pd.DataFrame(release_rows), width="stretch", hide_index=True)
                        st.caption(
                            f"{len(release_rows)} releases. Το MusicBrainz lookup επιστρέφει "
                            "μέχρι 25 ανά κλήση — πλήρης δισκογραφία απαιτεί browse με pagination."
                        )
                    else:
                        st.warning("⚠️ Δεν βρέθηκαν releases συνδεδεμένα με αυτόν τον καλλιτέχνη.")

                    st.divider()

                    # --- Relationships ----------------------------------------
                    st.markdown("### 🔗 Σχέσεις (Relationships)")
                    artist_rels = artist.get("artist-relation-list") or []
                    work_rels = artist.get("work-relation-list") or []

                    rel_rows = []
                    for rel in artist_rels:
                        if not isinstance(rel, dict):
                            continue
                        target = rel.get("artist") or {}
                        rel_rows.append({
                            "Κατηγορία": "Artist",
                            "Ρόλος": str(rel.get("type") or "—").title(),
                            "Κατεύθυνση": rel.get("direction") or "—",
                            "Συνδεδεμένο με": target.get("name") or "—",
                        })
                    for rel in work_rels:
                        if not isinstance(rel, dict):
                            continue
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
                        st.warning(
                            "🚨 **Κανένα relationship καταχωρημένο.** Το προφίλ δεν έχει "
                            "συνδέσεις με works (composer / lyricist / arranger) ούτε με "
                            "άλλους καλλιτέχνες (member of, collaborator, producer). "
                            "Αυτό σημαίνει ότι **οι δημιουργοί δεν είναι ανιχνεύσιμοι "
                            "αυτόματα** από PROs και metadata aggregators — απαιτείται "
                            "χειροκίνητη επιμέλεια στο MusicBrainz."
                        )
                        st.link_button(
                            "🔧 Προσθήκη relationships τώρα",
                            f"https://musicbrainz.org/artist/{mbid}/edit",
                            type="primary",
                            width="stretch",
                        )

    # ======================================================================
    # TAB 2 — ISRC / ISWC Resolver
    # ======================================================================
    with tab_isrc:
        st.markdown("### Recording → Work resolution")
        st.caption(
            "Το ISRC ταυτοποιεί την **ηχογράφηση**. Το ISWC ταυτοποιεί τη **σύνθεση**. "
            "Αυτή η γέφυρα είναι που πληρώνει τους writers."
        )

        isrc_input = st.text_input(
            "ISRC",
            placeholder="π.χ. GBAYE0601498",
            key="mb_isrc_input",
        )

        if st.button("Ανάλυση ISRC", type="primary", width="stretch", key="mb_isrc_btn"):
            clean_isrc = str(isrc_input or "").replace("-", "").strip().upper()

            if not clean_isrc:
                st.warning("Εισάγετε ένα ISRC.")
            elif not validate_isrc(clean_isrc):
                st.error(f"Το `{clean_isrc}` δεν έχει έγκυρη μορφή ISRC (CC-XXX-YY-NNNNN).")
            else:
                try:
                    with st.spinner("Fetching from MusicBrainz..."):
                        isrc_data = mb_get_recordings_by_isrc(clean_isrc)
                except (musicbrainzngs.ResponseError, musicbrainzngs.WebServiceError) as e:
                    st.error(_mb_error_message(e))
                    isrc_data = None
                except Exception as e:
                    st.error(f"Μη αναμενόμενο σφάλμα: {e}")
                    isrc_data = None

                if isrc_data is not None:
                    recordings = isrc_data.get("recording-list") or []

                    if not recordings:
                        st.warning(
                            f"Το ISRC `{clean_isrc}` δεν αντιστοιχεί σε καμία ηχογράφηση "
                            "στο MusicBrainz. Πιθανόν να μην έχει ευρετηριαστεί ακόμα."
                        )
                    else:
                        st.success(f"Βρέθηκαν {len(recordings)} ηχογραφήσεις για το `{clean_isrc}`.")
                        st.divider()

                        for rec_index, recording in enumerate(recordings, start=1):
                            if not isinstance(recording, dict):
                                continue

                            rec_title = recording.get("title") or "—"
                            rec_id = recording.get("id") or ""
                            performers = _mb_artist_credit_phrase(recording) or "—"

                            st.markdown(f"#### 🎧 {rec_index}. {rec_title}")

                            d1, d2, d3 = st.columns(3)
                            d1.markdown("**Ερμηνευτές**")
                            d1.write(performers)
                            d2.markdown("**Διάρκεια**")
                            d2.write(_mb_format_length(recording.get("length")))
                            d3.markdown("**Recording MBID**")
                            d3.code(rec_id or "—", language=None)

                            work_rels = recording.get("work-relation-list") or []
                            linked_works = [
                                rel.get("work")
                                for rel in work_rels
                                if isinstance(rel, dict) and isinstance(rel.get("work"), dict)
                            ]

                            if not linked_works:
                                st.error(
                                    "❌ **Καμία σύνδεση με Work.** Η ηχογράφηση δεν είναι "
                                    "συνδεδεμένη με σύνθεση, άρα δεν υπάρχει ISWC ούτε "
                                    "ανιχνεύσιμοι composers. Κρίσιμο κενό για publishing."
                                )
                                if rec_id:
                                    st.link_button(
                                        "🔧 Σύνδεση με Work στο MusicBrainz",
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
                                            with st.spinner("Fetching from MusicBrainz..."):
                                                work_full = mb_get_work(work_id)
                                        except (musicbrainzngs.ResponseError,
                                                musicbrainzngs.WebServiceError) as e:
                                            st.warning(
                                                "Το Work stub βρέθηκε αλλά το πλήρες lookup "
                                                f"απέτυχε: {_mb_error_message(e)}"
                                            )
                                        except Exception as e:
                                            st.warning(f"Αποτυχία lookup του Work: {e}")

                                    merged_work = {**work_stub, **(work_full or {})}
                                    iswc = _mb_iswc(merged_work)

                                    with st.container(border=True):
                                        st.markdown(f"##### 🎼 Work: {work_full.get('title') or work_title}")

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

                                        # Composers / lyricists από τα artist-rels του Work
                                        writer_rels = merged_work.get("artist-relation-list") or []
                                        writer_rows = []
                                        for rel in writer_rels:
                                            if not isinstance(rel, dict):
                                                continue
                                            person = rel.get("artist") or {}
                                            writer_rows.append({
                                                "Ρόλος": str(rel.get("type") or "—").title(),
                                                "Όνομα": person.get("name") or "—",
                                                "Legal / Sort Name": person.get("sort-name") or "—",
                                                "Artist MBID": person.get("id") or "—",
                                            })

                                        st.markdown("**Δημιουργοί (Composers / Lyricists)**")
                                        if writer_rows:
                                            st.dataframe(
                                                pd.DataFrame(writer_rows),
                                                width="stretch",
                                                hide_index=True,
                                            )
                                        else:
                                            st.warning(
                                                "⚠️ Το Work δεν έχει συνδεδεμένους composers / "
                                                "lyricists — δεν μπορεί να επιβεβαιωθεί η "
                                                "πατρότητα του έργου."
                                            )

                                        if work_id:
                                            st.link_button(
                                                "✏️ Επεξεργασία Work",
                                                f"https://musicbrainz.org/work/{work_id}/edit",
                                                width="stretch",
                                            )

                            if rec_index < len(recordings):
                                st.divider()

    # ======================================================================
    # TAB 3 — Catalog Barcode Scanner
    # ======================================================================
    with tab_barcode:
        st.markdown("### Barcode reconciliation (UPC / EAN)")
        st.caption(
            "Ελέγξτε αν μια κυκλοφορία της δισκογραφικής είναι σωστά ευρετηριασμένη "
            "στη διεθνή βάση."
        )

        barcode_input = st.text_input(
            "Barcode (UPC / EAN)",
            placeholder="π.χ. 5099749534728",
            key="mb_barcode_input",
        )

        if st.button("Σάρωση Barcode", type="primary", width="stretch", key="mb_barcode_btn"):
            barcode = re.sub(r"\D+", "", str(barcode_input or ""))

            if not barcode:
                st.warning("Εισάγετε ένα barcode (μόνο ψηφία).")
            else:
                try:
                    with st.spinner("Fetching from MusicBrainz..."):
                        results = mb_search_releases_by_barcode(barcode)
                except (musicbrainzngs.ResponseError, musicbrainzngs.WebServiceError) as e:
                    st.error(_mb_error_message(e))
                    results = None
                except Exception as e:
                    st.error(f"Μη αναμενόμενο σφάλμα: {e}")
                    results = None

                if results is not None:
                    if not results:
                        st.error(
                            f"❌ Το barcode `{barcode}` **δεν υπάρχει** στο MusicBrainz. "
                            "Η κυκλοφορία δεν είναι ευρετηριασμένη διεθνώς — "
                            "χάνεται σε aggregators και μουσικές εφαρμογές."
                        )
                    else:
                        top = results[0]
                        release_id = top.get("id")

                        release = top
                        try:
                            if release_id:
                                with st.spinner("Fetching from MusicBrainz..."):
                                    full = mb_get_release(release_id)
                                if full:
                                    release = {**top, **full}
                        except (musicbrainzngs.ResponseError, musicbrainzngs.WebServiceError) as e:
                            st.warning(
                                "Βρέθηκε το release αλλά το πλήρες lookup απέτυχε: "
                                f"{_mb_error_message(e)}"
                            )
                        except Exception as e:
                            st.warning(f"Αποτυχία πλήρους lookup: {e}")

                        if len(results) > 1:
                            st.info(
                                f"Βρέθηκαν {len(results)} releases με αυτό το barcode. "
                                "Εμφανίζεται το κορυφαίο αποτέλεσμα."
                            )

                        st.divider()

                        # --- Release header ---------------------------------
                        st.markdown(f"## 💿 {release.get('title') or '—'}")
                        st.caption(_mb_artist_credit_phrase(release) or "—")

                        # Label info
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
                        b4.metric("Barcode", release.get("barcode") or barcode)

                        l1, l2 = st.columns(2)
                        with l1:
                            st.markdown("**Label**")
                            st.write(", ".join(label_names) if label_names else "—")
                            if not label_names:
                                st.caption("⚠️ Δεν έχει καταχωρηθεί δισκογραφική.")
                        with l2:
                            st.markdown("**Catalog Number**")
                            st.write(", ".join(catalog_numbers) if catalog_numbers else "—")

                        if release_id:
                            st.link_button(
                                "🔗 Προβολή στο MusicBrainz",
                                f"https://musicbrainz.org/release/{release_id}",
                                width="stretch",
                            )

                        st.divider()

                        # --- Tracklist --------------------------------------
                        st.markdown("### 🎵 Tracklist")
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
                                track_rows.append({
                                    "Δίσκος": f"{medium_format} {medium_position}".strip(),
                                    "#": track.get("number") or track.get("position") or "—",
                                    "Τίτλος": rec.get("title") or track.get("title") or "—",
                                    "Διάρκεια": _mb_format_length(
                                        track.get("length") or rec.get("length")
                                    ),
                                    "Recording MBID": rec.get("id") or "—",
                                })

                        if track_rows:
                            st.dataframe(pd.DataFrame(track_rows), width="stretch", hide_index=True)
                            st.caption(f"Σύνολο: {len(track_rows)} tracks σε {len(media_list)} medium(s).")
                        else:
                            st.warning(
                                "⚠️ Η κυκλοφορία υπάρχει αλλά **δεν έχει tracklist**. "
                                "Χρειάζεται καταχώρηση των κομματιών για να είναι χρήσιμη."
                            )
