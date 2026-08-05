"""
core/auth_spotify.py

Spotify configuration, Authorization Code OAuth flow (per-user login),
token refresh, and Spotify Web API data fetching (playlists, tracks, user).
"""

import re
import time
import urllib.parse

import requests
import streamlit as st

SCOPE = "playlist-read-private playlist-read-collaborative"


# --------------------------------------------------------------------------
# Credentials & config
# --------------------------------------------------------------------------
def get_config():
    """
    Reads Spotify config from Streamlit secrets (Settings -> Secrets on Streamlit
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


def fetch_current_user(token):
    data = _api_get(token, "https://api.spotify.com/v1/me")
    return data.get("display_name") or data.get("id") or "Άγνωστος Χρήστης"
