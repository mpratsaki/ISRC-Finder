"""
tools/page_isrc_finder.py

"ISRC Finder" page: takes a Spotify track link (or URI) and resolves it to
its ISRC code via the Spotify "Get Track" API, along with basic track info.
"""

import re

import requests
import streamlit as st

from core.auth_spotify import _api_get

# Matches a 22-char base62 Spotify track ID out of:
#   https://open.spotify.com/track/<id>?si=...
#   https://open.spotify.com/intl-xx/track/<id>
#   spotify:track:<id>
#   or a bare <id> pasted directly
SPOTIFY_TRACK_ID_PATTERN = re.compile(r"track[/:]([A-Za-z0-9]{22})", re.IGNORECASE)
BARE_ID_PATTERN = re.compile(r"^[A-Za-z0-9]{22}$")


def _extract_track_id(raw_value):
    text = str(raw_value or "").strip()
    if not text:
        return None

    match = SPOTIFY_TRACK_ID_PATTERN.search(text)
    if match:
        return match.group(1)

    if BARE_ID_PATTERN.match(text):
        return text

    return None


def _track_artists(item):
    return ", ".join(a.get("name", "") for a in item.get("artists", []) if a.get("name"))


def page_isrc_finder(token):
    st.title("ISRC Finder")
    st.caption("Επικολλήστε ένα Spotify track link (ή URI) για να βρείτε το ISRC του.")

    col_input, col_btn = st.columns([3, 1], vertical_alignment="bottom")
    with col_input:
        link_input = st.text_input(
            "Spotify Track Link",
            placeholder="https://open.spotify.com/track/xxxxxxxxxxxxxxxxxxxxxx",
            key="isrc_finder_input",
        )
    with col_btn:
        search_trigger = st.button("Εύρεση ISRC", type="primary", width="stretch")

    if not search_trigger:
        return

    track_id = _extract_track_id(link_input)

    if not link_input.strip():
        st.warning("Επικολλήστε ένα Spotify track link.")
        return

    if not track_id:
        st.error(
            "Δεν αναγνωρίστηκε έγκυρο Spotify track ID. Χρησιμοποιήστε ένα link της "
            "μορφής `https://open.spotify.com/track/...`, ένα URI `spotify:track:...`, "
            "ή το ίδιο το track ID."
        )
        return

    try:
        with st.spinner("Αναζήτηση στο Spotify..."):
            data = _api_get(token, f"https://api.spotify.com/v1/tracks/{track_id}")
    except requests.HTTPError as e:
        status_code = getattr(e.response, "status_code", None)
        if status_code == 404:
            st.error(
                f"Το Spotify δεν βρήκε track με ID `{track_id}`. Τα Spotify track IDs "
                "είναι case-sensitive (πεζά/κεφαλαία έχουν σημασία) — αν το link έχει "
                "μετατραπεί ολόκληρο σε κεφαλαία (π.χ. από αυτόματη διόρθωση), "
                "επικολλήστε ξανά το αρχικό link απευθείας από το Spotify."
            )
        else:
            st.error(f"Σφάλμα επικοινωνίας με το Spotify: {e}")
        return
    except Exception as e:
        st.error(f"Μη αναμενόμενο σφάλμα: {e}")
        return

    isrc = (data.get("external_ids") or {}).get("isrc")
    track_name = data.get("name") or "—"
    artists = _track_artists(data) or "—"
    album = (data.get("album") or {}).get("name") or "—"
    spotify_url = (data.get("external_urls") or {}).get("spotify")

    st.divider()

    if isrc:
        st.success(f"✅ Βρέθηκε ISRC για το **{track_name}**")
        st.code(isrc, language=None)
    else:
        st.warning(f"Το track **{track_name}** δεν έχει καταχωρημένο ISRC στο Spotify.")

    st.markdown(f"**{track_name}**")
    st.write(f"🎤 Καλλιτέχνης/ες: {artists}")
    st.write(f"💿 Άλμπουμ: {album}")
    if spotify_url:
        st.link_button("🔗 Άνοιγμα στο Spotify", spotify_url, width="stretch")
