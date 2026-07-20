#!/usr/bin/env python3
"""
app.py
Stay Independent Tool - The Stay Independent Catalog Utility
Streamlit web version (Playlist-to-Excel Generator)

Version 2.0 - "Swiss Army Knife Edition"
- Modular, state-based page routing (each page is a standalone function).
- Scalable custom sidebar navigation with active-page highlighting.
- New tools: Musixmatch Sync Checker, Metadata Health, Smart Links (Odesli),
  MusicBrainz Explorer (under development), Settings.
- Backend logic (Spotify auth, Tidal ISRC lookup, private IPI LIST loading,
  Supabase queries, Excel generation) is UNCHANGED.

IMPORTANT (post Feb-2026 Spotify API changes):
Spotify no longer returns playlist contents via Client Credentials, and even
with a logged-in user, playlist items are only returned for playlists that
user OWNS or COLLABORATES ON. So this app makes each visitor log in with
their own Spotify account, and lets them pick from THEIR playlists only.

This file is ONLY the presentation / routing shell. All backend logic lives
in core/, utils/, and tools/.
"""

import os

import requests
import streamlit as st

from core.auth_spotify import (
    get_config,
    build_authorize_url,
    exchange_code_for_token,
    get_valid_token,
    fetch_current_user,
)
from tools.page_catalog import page_catalog_generator
from tools.page_history import page_history
from tools.page_isrc_finder import page_isrc_finder
from tools.page_metadata import page_metadata_health
from tools.page_musicbrainz import page_musicbrainz
from tools.page_musicbrainz_label import page_musicbrainz_label
from tools.page_musicbrainz_release_group import page_musicbrainz_release_group
from tools.page_musicbrainz_search import page_musicbrainz_search
from tools.page_musicbrainz_work import page_musicbrainz_work
from tools.page_settings import page_settings
from core.auth_musicbrainz import init_mb_auth, is_mb_authenticated
from tools.page_audio_id import page_audio_id

st.set_page_config(
    page_title="Stay Independent Tool",
    page_icon="StayLogo2.jpg",
    layout="wide",
    initial_sidebar_state="expanded",
)

# --- Ενσωμάτωση Custom CSS ---
st.markdown("""
    <style>
    .stButton>button {
        border-radius: 8px;
        transition: all 0.3s ease;
    }
    .stButton>button:hover {
        transform: translateY(-2px);
        box-shadow: 0 4px 12px rgba(0,0,0,0.15);
    }
    .live-activity-box {
        padding: 15px;
        border-radius: 10px;
        background: linear-gradient(135deg, #2a2a2a, #1f1f1f);
        border-left: 5px solid #1DB954;
        margin-bottom: 20px;
        box-shadow: 0 4px 8px rgba(0,0,0,0.2);
    }
    .block-container {
        padding-top: 2rem;
    }
    .under-construction {
        padding: 40px 25px;
        border-radius: 14px;
        text-align: center;
        background: repeating-linear-gradient(
            45deg, #2b2b2b, #2b2b2b 18px, #242424 18px, #242424 36px
        );
        border: 2px dashed #FFB020;
        box-shadow: 0 6px 18px rgba(0,0,0,0.25);
        margin-bottom: 25px;
    }
    .under-construction h2 { color:#FFB020; margin-bottom:8px; }
    .under-construction p  { color:#ddd; font-size:16px; }
    /* Try to give the sidebar a column layout so the system block can sit lower */
    section[data-testid="stSidebar"] div[data-testid="stSidebarUserContent"] {
        display: flex;
        flex-direction: column;
    }
    </style>
""", unsafe_allow_html=True)


# ==========================================================================
# PAGE: Landing / Login (no sidebar, centered) - logged-out state
# ==========================================================================
def render_landing_page(client_id, redirect_uri):
    st.markdown("<br><br>", unsafe_allow_html=True)

    # Smaller, aesthetically balanced logo via a narrow center column.
    logo_left, logo_center, logo_right = st.columns([4, 2, 4])
    with logo_center:
        if os.path.exists("StayLogo2.jpg"):
            st.image("StayLogo2.jpg", width="stretch")

    st.markdown(
        "<h1 style='text-align:center;'>Stay Independent Tool</h1>",
        unsafe_allow_html=True,
    )
    st.markdown(
        "<p style='text-align:center; font-size:18px; color:#aaa;'>"
        "Welcome to the Stay Independent multi-tool. Please log in with your "
        "Spotify account to access the Catalog Generator and your Export History."
        "</p>",
        unsafe_allow_html=True,
    )

    st.markdown("<br>", unsafe_allow_html=True)

    auth_url = build_authorize_url(client_id, redirect_uri)
    _, btn_col, _ = st.columns([1, 2, 1])
    with btn_col:
        st.link_button("Σύνδεση με Spotify", auth_url, type="primary", width="stretch")


# ==========================================================================
# SIDEBAR NAVIGATION (logged-in) - custom, state-based, highlighted
# ==========================================================================
def _nav_button(label, page_key):
    """Renders a sidebar nav button; highlights the active page via type='primary'."""
    is_active = st.session_state.get("current_page") == page_key
    if st.sidebar.button(
        label,
        width="stretch",
        type="primary" if is_active else "secondary",
        key=f"nav_{page_key}",
    ):
        if not is_active:
            st.session_state.current_page = page_key
            st.rerun()


def render_sidebar(spotify_user):
    with st.sidebar:
        if os.path.exists("StayLogo2.jpg"):
            st.image("StayLogo2.jpg", width="stretch")
        else:
            st.markdown("## 🎵 Stay Independent Tool")

        st.markdown("### Stay Independent Tool\n*Swiss Army Knife*")
        st.success(f"🟢 Συνδεδεμένος: **{spotify_user}**")
        
        # --- NEW PHASE 3 SNIPPET START ---
        # Initialize MB auth silently in the background if possible
        if "mb_auth_attempted" not in st.session_state:
            init_mb_auth()
            st.session_state["mb_auth_attempted"] = True
            
        if is_mb_authenticated():
            st.success("🟢 MusicBrainz: **Συνδεδεμένος**")
        else:
            st.warning("🟡 MusicBrainz: **Μόνο Ανάγνωση**")
        # --- NEW PHASE 3 SNIPPET END ---
        
        st.divider()

# --- Top section: Εργαλεία ---
    st.sidebar.markdown("###Εργαλεία")
    _nav_button("Γεννήτρια Catalog", "Γεννήτρια Catalog")
    _nav_button("ISRC Finder", "ISRC Finder")
    _nav_button("Metadata Health", "Metadata Health")
    _nav_button("MusicBrainz Universal Search", "MusicBrainz Search")
    _nav_button("MusicBrainz Label Auditor", "MusicBrainz Label Auditor")
    _nav_button("MusicBrainz Album Editions", "MusicBrainz Release Group")
    _nav_button("MusicBrainz Work Explorer", "MusicBrainz Work Explorer")
    _nav_button("MusicBrainz Explorer (Beta)", "MusicBrainz Explorer")
    _nav_button("AcoustID Audio Scanner", "AcoustID Scanner")

    # --- Spacer to push the System block lower ---
    st.sidebar.markdown("<div style='height: 2.5rem'></div>", unsafe_allow_html=True)
    st.sidebar.divider()

    # --- Bottom section: Σύστημα ---
    st.sidebar.markdown("###Σύστημα")
    _nav_button("Ιστορικό & Αρχεία", "Ιστορικό & Αρχεία")
    _nav_button("Ρυθμίσεις", "Ρυθμίσεις")

    if st.sidebar.button("Αποσύνδεση", width="stretch", key="nav_logout"):
        st.session_state.pop("token_data", None)
        st.session_state.pop("current_page", None)
        st.rerun()

    st.sidebar.divider()
    st.sidebar.caption("Stay Independent Tool © 2026")
# ==========================================================================
# MAIN APPLICATION FLOW
# ==========================================================================
client_id, client_secret, redirect_uri = get_config()

# --- Auth Callback Handling ---
query_params = st.query_params
if "error" in query_params:
    st.error(f"Η σύνδεση με το Spotify απέτυχε: {query_params['error']}")
    st.query_params.clear()
elif "code" in query_params and "token_data" not in st.session_state:
    try:
        token_data = exchange_code_for_token(client_id, client_secret, redirect_uri, query_params["code"])
        st.session_state["token_data"] = token_data
        st.toast("Επιτυχής σύνδεση στο Spotify!", icon="✅")
    except requests.HTTPError as e:
        st.error(f"Σφάλμα κατά την ανταλλαγή του code: {e}")
    st.query_params.clear()
    st.rerun()

token = get_valid_token()

# --- Logged-out: centered landing page, no sidebar ---
if not token:
    render_landing_page(client_id, redirect_uri)
    st.stop()

# --- Logged-in: resolve user, init routing state, render shell ---
try:
    spotify_user = fetch_current_user(token)
except Exception:
    spotify_user = "Άγνωστος Χρήστης"

if "current_page" not in st.session_state:
    st.session_state.current_page = "Γεννήτρια Catalog"

render_sidebar(spotify_user)

# --- State-based router ---
current_page = st.session_state.current_page

if current_page == "Γεννήτρια Catalog":
    page_catalog_generator(token, spotify_user)
elif current_page == "ISRC Finder":
    page_isrc_finder(token)
elif current_page == "Metadata Health":
    page_metadata_health()
elif current_page == "MusicBrainz Search":
    page_musicbrainz_search()
elif current_page == "MusicBrainz Label Auditor":
    page_musicbrainz_label()
elif current_page == "MusicBrainz Release Group":
    page_musicbrainz_release_group()
elif current_page == "MusicBrainz Work Explorer":
    page_musicbrainz_work()
elif current_page == "MusicBrainz Explorer":
    page_musicbrainz()
elif current_page == "AcoustID Scanner":
    page_audio_id()
elif current_page == "Ιστορικό & Αρχεία":
    page_history(token, spotify_user)
elif current_page == "Ρυθμίσεις":
    page_settings(spotify_user)
else:
    # Fallback safety net
    st.session_state.current_page = "Γεννήτρια Catalog"
    st.rerun()
