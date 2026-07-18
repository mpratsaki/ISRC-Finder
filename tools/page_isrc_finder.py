"""
tools/page_isrc_finder.py

"ISRC Finder" page: takes an ISRC code and resolves it to the matching
Spotify track(s) via the Search API, surfacing the top match plus any
alternate releases/markets sharing the same ISRC.
"""

import re

import requests
import streamlit as st

from core.auth_spotify import _api_get

ISRC_PATTERN = re.compile(r"^[A-Z0-9]{12}$")


def _clean_isrc(raw_value):
    return re.sub(r"[\s-]+", "", str(raw_value or "")).strip().upper()


def _track_artists(item):
    return ", ".join(a.get("name", "") for a in item.get("artists", []) if a.get("name"))


def _render_track_card(item):
    track_name = item.get("name") or "—"
    artists = _track_artists(item) or "—"
    album = (item.get("album") or {}).get("name") or "—"
    spotify_url = (item.get("external_urls") or {}).get("spotify")

    st.markdown(f"**{track_name}**")
    st.write(f"🎤 Καλλιτέχνης/ες: {artists}")
    st.write(f"💿 Άλμπουμ: {album}")
    if spotify_url:
        st.link_button("🔗 Άνοιγμα στο Spotify", spotify_url, width="stretch")
    else:
        st.caption("Δεν βρέθηκε Spotify link για αυτό το track.")


def page_isrc_finder(token):
    st.title("🔎 ISRC Finder")
    st.caption("Βρείτε το αντίστοιχο Spotify track link(s) από ένα ISRC code.")

    col_input, col_btn = st.columns([3, 1], vertical_alignment="bottom")
    with col_input:
        isrc_input = st.text_input(
            "ISRC",
            placeholder="π.χ. GBAYE0601498",
            key="isrc_finder_input",
        )
    with col_btn:
        search_trigger = st.button("Αναζήτηση", type="primary", width="stretch")

    if not search_trigger:
        return

    clean_isrc = _clean_isrc(isrc_input)

    if not clean_isrc:
        st.warning("Εισάγετε ένα ISRC.")
        return

    if not ISRC_PATTERN.match(clean_isrc):
        st.error(
            f"Το `{clean_isrc}` δεν φαίνεται να είναι έγκυρο ISRC "
            "(αναμένονται 12 αλφαριθμητικοί χαρακτήρες, π.χ. CCXXXYYNNNNN)."
        )
        return

    try:
        with st.spinner("Αναζήτηση στο Spotify..."):
            data = _api_get(
                token,
                "https://api.spotify.com/v1/search",
                params={"q": f"isrc:{clean_isrc}", "type": "track", "limit": 5},
            )
    except requests.HTTPError as e:
        st.error(f"Σφάλμα επικοινωνίας με το Spotify: {e}")
        return
    except Exception as e:
        st.error(f"Μη αναμενόμενο σφάλμα: {e}")
        return

    items = data.get("tracks", {}).get("items", [])

    if not items:
        st.warning(f"Δεν βρέθηκε κανένα Spotify track για το ISRC `{clean_isrc}`.")
        return

    st.divider()

    top_match = items[0]

    st.success(f"✅ Βρέθηκε αντιστοιχία για το ISRC `{clean_isrc}`")
    _render_track_card(top_match)

    other_matches = items[1:]
    if other_matches:
        with st.expander(f"Άλλες εκδόσεις αυτού του ISRC ({len(other_matches)})"):
            for idx, item in enumerate(other_matches):
                _render_track_card(item)
                if idx < len(other_matches) - 1:
                    st.divider()
