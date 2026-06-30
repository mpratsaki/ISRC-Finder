#!/usr/bin/env python3
"""
app.py
MMS Matrix - The Stay Independent Catalog Utility
Streamlit web version (Playlist-to-Excel Generator)

Uses Spotify's Client Credentials flow (app-only auth, no user login)
so it only works with PUBLIC playlists, but anyone can use the hosted
app with zero Spotify login.
"""

import io
import os
import re
import time

import openpyxl
import requests
import streamlit as st
from openpyxl.styles import Alignment, Font

# --------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------
def get_credentials():
    """
    Reads credentials from Streamlit secrets first (used on Streamlit
    Community Cloud), falling back to environment variables (used for
    local testing with a .env file you DO NOT commit to git).
    """
    client_id = st.secrets.get("SPOTIFY_CLIENT_ID", os.getenv("SPOTIFY_CLIENT_ID"))
    client_secret = st.secrets.get("SPOTIFY_CLIENT_SECRET", os.getenv("SPOTIFY_CLIENT_SECRET"))
    return client_id, client_secret


# --------------------------------------------------------------------------
# Spotify authentication (Client Credentials flow - app only, no user login)
# --------------------------------------------------------------------------
@st.cache_resource(ttl=3500)  # Spotify tokens last ~3600s, refresh a bit early
def get_access_token(client_id, client_secret):
    resp = requests.post(
        "https://accounts.spotify.com/api/token",
        data={"grant_type": "client_credentials"},
        auth=(client_id, client_secret),
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


# --------------------------------------------------------------------------
# Spotify data fetching & validation
# --------------------------------------------------------------------------
def extract_playlist_id(playlist_arg):
    m = re.search(r"playlist[/:]([a-zA-Z0-9]+)", playlist_arg)
    if m:
        return m.group(1).split("?")[0]
    return playlist_arg.strip()


def _api_get(token, url, params=None, retries=3):
    headers = {"Authorization": f"Bearer {token}"}
    for attempt in range(retries):
        resp = requests.get(url, headers=headers, params=params)
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 2))
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json()
    resp.raise_for_status()


def fetch_playlist_tracks(token, playlist_id):
    tracks = []
    url = f"https://api.spotify.com/v1/playlists/{playlist_id}/tracks"
    params = {
        "fields": "items(track(id,name,artists(name),external_ids)),next",
        "limit": 50,
        "offset": 0,
    }

    while url:
        data = _api_get(token, url, params=params)
        items = data.get("items")
        if items is None:
            raise RuntimeError("Το Spotify δεν επέστρεψε τραγούδια. Ελέγξτε το link.")

        for entry in items:
            track = entry.get("track")
            if not track:
                continue
            isrc = (track.get("external_ids") or {}).get("isrc")
            tracks.append(
                {
                    "id": track["id"],
                    "name": track["name"],
                    "artists": [a["name"] for a in track.get("artists", [])],
                    "isrc": isrc,
                }
            )

        url = data.get("next")
        params = None

    return tracks


def validate_isrc(isrc):
    if not isrc:
        return False
    clean_isrc = str(isrc).replace("-", "").strip()
    pattern = re.compile(r"^[A-Z]{2}[A-Z0-9]{3}\d{2}\d{5}$", re.IGNORECASE)
    return bool(pattern.match(clean_isrc))


# --------------------------------------------------------------------------
# Excel Generator (Zero-to-Excel) - now builds in-memory, not to disk
# --------------------------------------------------------------------------
def generate_new_catalog(tracks):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Stay Independent Catalog"

    report = {"filled": [], "health_warnings": []}

    headers = ["TITLE", "ROLE", "WRITERS", "ISRC", "NOTES"]
    for col_num, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_num)
        cell.value = header
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal="center", vertical="center")

    ws.column_dimensions["A"].width = 35
    ws.column_dimensions["B"].width = 15
    ws.column_dimensions["C"].width = 25
    ws.column_dimensions["D"].width = 20
    ws.column_dimensions["E"].width = 30

    insert_at = 2

    for track in tracks:
        title = track["name"]
        artists = track["artists"]
        isrc = track["isrc"]

        if isrc and not validate_isrc(isrc):
            report["health_warnings"].append((title, isrc))

        needed_rows = max(6, len(artists))

        for i in range(needed_rows):
            current_row = insert_at + i
            if i == 0:
                ws.cell(row=current_row, column=1).value = title
                if isrc:
                    ws.cell(row=current_row, column=4).value = isrc
            if i < len(artists):
                ws.cell(row=current_row, column=3).value = artists[i]

        report["filled"].append((title, artists, isrc))
        insert_at += needed_rows + 1

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer, report


# --------------------------------------------------------------------------
# Streamlit UI
# --------------------------------------------------------------------------
st.set_page_config(page_title="Stay Independent Catalog Generator", page_icon="🎵")

st.title("🎵 Stay Independent Catalog Generator")
st.write("Επικολλήστε το link μιας **δημόσιας** Spotify playlist για να δημιουργηθεί το Excel κατάλογος.")

client_id, client_secret = get_credentials()
if not client_id or not client_secret:
    st.error(
        "Δεν βρέθηκαν τα διαπιστευτήρια Spotify. Αν τρέχετε τοπικά, ορίστε "
        "SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET στο περιβάλλον. Στο Streamlit "
        "Cloud, προσθέστε τα στα app secrets (Settings → Secrets)."
    )
    st.stop()

playlist_input = st.text_input("Spotify Playlist link ή ID", placeholder="https://open.spotify.com/playlist/...")

if st.button("Δημιουργία Excel ✔️", type="primary"):
    if not playlist_input.strip():
        st.warning("Παρακαλώ δώστε ένα link ή ID playlist.")
        st.stop()

    playlist_id = extract_playlist_id(playlist_input)

    try:
        with st.spinner("Σύνδεση με το Spotify API..."):
            token = get_access_token(client_id, client_secret)

        with st.spinner("Ανάκτηση τραγουδιών..."):
            tracks = fetch_playlist_tracks(token, playlist_id)

        if not tracks:
            st.warning("Δεν βρέθηκαν τραγούδια. Ελέγξτε ότι η playlist είναι δημόσια.")
            st.stop()

        st.success(f"Βρέθηκαν {len(tracks)} τραγούδια!")

        with st.spinner("Δημιουργία Excel..."):
            buffer, report = generate_new_catalog(tracks)

        if report["health_warnings"]:
            st.warning("⚠️ Προειδοποιήσεις ISRC:")
            for title, isrc in report["health_warnings"]:
                st.write(f"- **{title}**: Άκυρο ISRC ({isrc})")

        output_filename = f"Release_{playlist_id[:8]}.xlsx"
        st.download_button(
            label="⬇️ Λήψη Excel",
            data=buffer,
            file_name=output_filename,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    except requests.HTTPError as e:
        st.error(f"Σφάλμα επικοινωνίας με το Spotify: {e}")
    except Exception as e:
        st.error(f"Κάτι πήγε στραβά: {e}")
