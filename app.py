#!/usr/bin/env python3
"""
app.py
MMS Matrix - The Stay Independent Catalog Utility
Streamlit web version (Playlist-to-Excel Generator)

IMPORTANT (post Feb-2026 Spotify API changes):
Spotify no longer returns playlist contents via Client Credentials, and even
with a logged-in user, playlist items are only returned for playlists that
user OWNS or COLLABORATES ON. So this app makes each visitor log in with
their own Spotify account, and lets them pick from THEIR playlists only.

Spotify also currently caps unverified ("Development Mode") apps to 5
authorized Spotify accounts total - add testers in the app's dashboard under
"User Management" if you need more than yourself using it.
"""

import io
import re
import time
import urllib.parse

import openpyxl
import requests
import streamlit as st
from openpyxl.styles import Alignment, Font

SCOPE = "playlist-read-private playlist-read-collaborative"

# --------------------------------------------------------------------------
# Credentials & config
# --------------------------------------------------------------------------
def get_config():
    """
    Reads config from Streamlit secrets (Settings -> Secrets on Streamlit
    Community Cloud). Required keys:
      SPOTIFY_CLIENT_ID
      SPOTIFY_CLIENT_SECRET
      REDIRECT_URI   -> must exactly match a Redirect URI registered in your
                         Spotify Dashboard app, e.g. https://yourapp.streamlit.app
                         (use http://localhost:8501 while testing locally)
    """
    try:
        client_id = st.secrets["SPOTIFY_CLIENT_ID"]
        client_secret = st.secrets["SPOTIFY_CLIENT_SECRET"]
        redirect_uri = st.secrets["REDIRECT_URI"]
    except KeyError as e:
        st.error(f"Λείπει η ρύθμιση {e} από τα Streamlit secrets.")
        st.stop()
    return client_id, client_secret, redirect_uri


# --------------------------------------------------------------------------
# Spotify authentication (Authorization Code flow - per-user login)
# --------------------------------------------------------------------------
def build_authorize_url(client_id, redirect_uri):
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": SCOPE,
    }
    return "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode(params)


def exchange_code_for_token(client_id, client_secret, redirect_uri, code):
    resp = requests.post(
        "https://accounts.spotify.com/api/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        },
        auth=(client_id, client_secret),
    )
    resp.raise_for_status()
    data = resp.json()
    data["expires_at"] = time.time() + data.get("expires_in", 3600)
    return data


def refresh_access_token(client_id, client_secret, refresh_token):
    resp = requests.post(
        "https://accounts.spotify.com/api/token",
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        auth=(client_id, client_secret),
    )
    resp.raise_for_status()
    data = resp.json()
    data.setdefault("refresh_token", refresh_token)
    data["expires_at"] = time.time() + data.get("expires_in", 3600)
    return data


def get_valid_token():
    """Returns a valid access token from session_state, refreshing if needed."""
    token_data = st.session_state.get("token_data")
    if not token_data:
        return None

    if token_data["expires_at"] > time.time() + 30:
        return token_data["access_token"]

    client_id, client_secret, _ = get_config()
    try:
        token_data = refresh_access_token(client_id, client_secret, token_data["refresh_token"])
        st.session_state["token_data"] = token_data
        return token_data["access_token"]
    except requests.HTTPError:
        st.session_state.pop("token_data", None)
        return None


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


def fetch_user_playlists(token):
    """Returns the logged-in user's own + collaborative playlists."""
    playlists = []
    url = "https://api.spotify.com/v1/me/playlists"
    params = {"limit": 50}

    while url:
        data = _api_get(token, url, params=params)
        for item in data.get("items", []):
            playlists.append({"id": item["id"], "name": item["name"]})
        url = data.get("next")
        params = None

    return playlists


def fetch_playlist_tracks(token, playlist_id):
    tracks = []
    url = f"https://api.spotify.com/v1/playlists/{playlist_id}/items"
    params = {
        "fields": "items(item(id,name,artists(name),external_ids)),next",
        "limit": 50,
        "offset": 0,
    }

    while url:
        data = _api_get(token, url, params=params)
        items = data.get("items")
        if items is None:
            raise RuntimeError(
                "Δεν επιστράφηκε περιεχόμενο playlist. Βεβαιωθείτε ότι η playlist "
                "είναι δική σας ή collaborative."
            )

        for entry in items:
            track = entry.get("item")
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
# Excel Generator (Zero-to-Excel) - builds in-memory
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

client_id, client_secret, redirect_uri = get_config()

# Step 1: handle the redirect back from Spotify (?code=... in the URL)
query_params = st.query_params
if "error" in query_params:
    st.error(f"Η σύνδεση με το Spotify απέτυχε: {query_params['error']}")
    st.query_params.clear()
elif "code" in query_params and "token_data" not in st.session_state:
    try:
        token_data = exchange_code_for_token(client_id, client_secret, redirect_uri, query_params["code"])
        st.session_state["token_data"] = token_data
    except requests.HTTPError as e:
        st.error(f"Σφάλμα κατά την ανταλλαγή του code: {e}")
    st.query_params.clear()
    st.rerun()

token = get_valid_token()

# Step 2: not logged in -> show login link
if not token:
    auth_url = build_authorize_url(client_id, redirect_uri)
    st.write("Συνδεθείτε με το Spotify σας για να δείτε τις playlists σας (δικές σας ή collaborative).")
    st.link_button("🔑 Σύνδεση με Spotify", auth_url, type="primary")
    st.stop()

# Step 3: logged in -> show their playlists
col1, col2 = st.columns([3, 1])
with col2:
    if st.button("Αποσύνδεση"):
        st.session_state.pop("token_data", None)
        st.rerun()

try:
    with st.spinner("Φόρτωση των playlists σας..."):
        playlists = fetch_user_playlists(token)
except requests.HTTPError as e:
    st.error(f"Σφάλμα επικοινωνίας με το Spotify: {e}")
    st.stop()

if not playlists:
    st.warning("Δεν βρέθηκαν playlists στο λογαριασμό σας.")
    st.stop()

playlist_names = [p["name"] for p in playlists]
selected_name = st.selectbox("Επιλέξτε playlist", playlist_names)
selected_playlist = next(p for p in playlists if p["name"] == selected_name)

if st.button("Δημιουργία Excel ✔️", type="primary"):
    try:
        with st.spinner("Ανάκτηση τραγουδιών..."):
            tracks = fetch_playlist_tracks(token, selected_playlist["id"])

        if not tracks:
            st.warning("Δεν βρέθηκαν τραγούδια σε αυτή την playlist.")
            st.stop()

        st.success(f"Βρέθηκαν {len(tracks)} τραγούδια!")

        with st.spinner("Δημιουργία Excel..."):
            buffer, report = generate_new_catalog(tracks)

        if report["health_warnings"]:
            st.warning("⚠️ Προειδοποιήσεις ISRC:")
            for title, isrc in report["health_warnings"]:
                st.write(f"- **{title}**: Άκυρο ISRC ({isrc})")

        output_filename = f"Release_{selected_playlist['id'][:8]}.xlsx"
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
