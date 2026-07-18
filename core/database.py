"""
core/database.py

Supabase client initialization (used for export history & catalog storage).
"""

import streamlit as st
from supabase import create_client, Client


@st.cache_resource
def init_supabase() -> Client:
    url = st.secrets.get("SUPABASE_URL")
    key = st.secrets.get("SUPABASE_KEY")
    if url and key:
        return create_client(url, key)
    return None
