"""
core/auth_musicbrainz.py

Authentication handler for MusicBrainz.
Uses HTTP Digest Auth (Username/Password) which is natively supported by 
the musicbrainzngs library for submitting tags, ratings, ISRCs, and collections.
"""

import musicbrainzngs
import streamlit as st

def get_mb_credentials():
    """
    Reads MusicBrainz credentials from Streamlit secrets.
    Required keys in st.secrets:
      MB_USERNAME
      MB_PASSWORD
    """
    username = st.secrets.get("MB_USERNAME")
    password = st.secrets.get("MB_PASSWORD")
    return username, password

def init_mb_auth():
    """
    Authenticates the musicbrainzngs client if credentials are provided.
    Returns True if authenticated successfully, False otherwise.
    """
    username, password = get_mb_credentials()
    if not username or not password:
        return False
    
    try:
        musicbrainzngs.auth(username, password)
        st.session_state["mb_authenticated"] = True
        return True
    except Exception as e:
        st.session_state["mb_authenticated"] = False
        return False

def is_mb_authenticated():
    """Checks if the user has a valid authenticated session for MusicBrainz."""
    return st.session_state.get("mb_authenticated", False)
