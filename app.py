#!/usr/bin/env python3
"""
app.py
MMS Matrix - The Stay Independent Catalog Utility
Streamlit web version (Playlist-to-Excel Generator)

Version update:
- Adds Tidal ISRC credits lookup for songwriter/producer contributors, with
  Spotify main artists as a safe fallback.
- Loads the IPI LIST ground-truth Excel automatically from a private GitHub
  repository using Streamlit secrets, so users do not upload sensitive files.
- Adds nickname/legal-name/IPI/PRO matching from the private IPI LIST.
- Updates the catalog export to TITLE / ROLE / WRITERS / ISRC / IPI / PRO / NOTES.

IMPORTANT (post Feb-2026 Spotify API changes):
Spotify no longer returns playlist contents via Client Credentials, and even
with a logged-in user, playlist items are only returned for playlists that
user OWNS or COLLABORATES ON. So this app makes each visitor log in with
their own Spotify account, and lets them pick from THEIR playlists only.

Spotify also currently caps unverified ("Development Mode") apps to 5
authorized Spotify accounts total - add testers in the app's dashboard under
"User Management" if you need more than yourself using it.
"""

import base64
import io
import re
import time
import unicodedata
import urllib.parse
from datetime import datetime
from decimal import Decimal, InvalidOperation
from openpyxl.styles import Alignment, Font, PatternFill, Border, Side

import openpyxl
import requests
import streamlit as st
from openpyxl.styles import Alignment, Font

SCOPE = "playlist-read-private playlist-read-collaborative"

# unofficial public Tidal client token
TIDAL_TOKEN = "gsFXkJqGrUNoYMQPZe4k3WKwijnrp8iGSwn3bApe"
TIDAL_COUNTRY_CODE = "US"
TIDAL_REQUEST_TIMEOUT_SECONDS = 10
TIDAL_SLEEP_BETWEEN_CALLS_SECONDS = 0.1

TIDAL_ALLOWED_ROLE_KEYS = {"composer", "lyricist", "writer", "author", "producer"}
TIDAL_EXCLUDED_ROLE_KEYS = {
    "musicpublisher",
    "mixingengineer",
    "masteringengineer",
    "recordingengineer",
    "programmer",
    "musician",
    "featured",
    "mainartist",
}

# Private GitHub IPI LIST source. The Excel file must live in a private
# repository. Store the read-only token and file location in Streamlit secrets.
GITHUB_API_VERSION = "2022-11-28"
GITHUB_CONTENTS_TIMEOUT_SECONDS = 20
IPI_LIST_CACHE_TTL_SECONDS = 15 * 60


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


def get_private_ipi_config():
    """
    Reads the private GitHub source for the IPI LIST ground-truth Excel.

    Required Streamlit secrets:
      IPI_GITHUB_OWNER  -> GitHub user/org that owns the private repository
      IPI_GITHUB_REPO   -> private repository name
      IPI_GITHUB_PATH   -> path to the xlsx file inside the repository
      IPI_GITHUB_TOKEN  -> fine-grained PAT with Contents: Read-only on that repo

    Optional Streamlit secret:
      IPI_GITHUB_REF    -> branch, tag, or commit SHA. Defaults to "main".
    """
    required_keys = [
        "IPI_GITHUB_OWNER",
        "IPI_GITHUB_REPO",
        "IPI_GITHUB_PATH",
        "IPI_GITHUB_TOKEN",
    ]
    missing = [key for key in required_keys if not str(st.secrets.get(key, "")).strip()]
    if missing:
        raise RuntimeError("Λείπουν Streamlit secrets για το IPI LIST: " + ", ".join(missing))

    return {
        "owner": str(st.secrets["IPI_GITHUB_OWNER"]).strip(),
        "repo": str(st.secrets["IPI_GITHUB_REPO"]).strip(),
        "path": str(st.secrets["IPI_GITHUB_PATH"]).strip(),
        "ref": str(st.secrets.get("IPI_GITHUB_REF", "main")).strip() or "main",
        "token": str(st.secrets["IPI_GITHUB_TOKEN"]).strip(),
    }


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
# Tidal credits lookup by ISRC
# --------------------------------------------------------------------------
def _role_key(role):
    return re.sub(r"[\s_\-]+", "", str(role or "").strip()).lower()


def _is_allowed_tidal_role(role):
    """Allow only songwriter/producer roles; exclude publishers, engineers, artists, etc."""
    role_text = str(role or "").strip()
    if not role_text:
        return False

    key = _role_key(role_text)
    if key in TIDAL_EXCLUDED_ROLE_KEYS:
        return False
    if key in TIDAL_ALLOWED_ROLE_KEYS:
        return True

    # Defensive handling for rare combined role strings such as "Composer/Lyricist".
    parts = re.split(r"[,;/|&]+", role_text)
    return any(_role_key(part) in TIDAL_ALLOWED_ROLE_KEYS for part in parts)


def _extract_items(data):
    """Tidal responses normally contain an 'items' list; keep this tolerant."""
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []

    items = data.get("items")
    if isinstance(items, list):
        return items

    tracks = data.get("tracks")
    if isinstance(tracks, dict) and isinstance(tracks.get("items"), list):
        return tracks["items"]

    return []


def _tidal_get(url, params=None, retries=3):
    """
    Safe Tidal GET wrapper. Never raises to the UI path.
    Returns (json_data, note_if_failed).
    """
    headers = {"X-Tidal-Token": TIDAL_TOKEN}
    last_note = "Tidal request failed — used Spotify artists as fallback"

    for _ in range(retries):
        try:
            resp = requests.get(
                url,
                headers=headers,
                params=params,
                timeout=TIDAL_REQUEST_TIMEOUT_SECONDS,
            )

            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After", "1")
                try:
                    wait_seconds = max(float(retry_after), 0.0)
                except (TypeError, ValueError):
                    wait_seconds = 1.0
                time.sleep(wait_seconds)
                last_note = "Tidal rate limit — used Spotify artists as fallback"
                continue

            if not resp.ok:
                return None, f"Tidal HTTP {resp.status_code} — used Spotify artists as fallback"

            return resp.json(), None

        except requests.RequestException:
            last_note = "Tidal request failed — used Spotify artists as fallback"
        except ValueError:
            return None, "Tidal response invalid — used Spotify artists as fallback"
        finally:
            time.sleep(TIDAL_SLEEP_BETWEEN_CALLS_SECONDS)

    return None, last_note


def fetch_tidal_contributors_by_isrc(isrc):
    """
    Finds the first Tidal track by ISRC and returns unique contributor names for
    Composer/Lyricist/Writer/Author/Producer roles only.
    Returns (names, note_if_fallback_needed).
    """
    try:
        clean_isrc = str(isrc or "").replace("-", "").strip().upper()
        if not clean_isrc:
            return [], "Missing ISRC — used Spotify artists as fallback"
        if not validate_isrc(clean_isrc):
            return [], "ISRC format invalid — used Spotify artists as fallback"

        search_data, note = _tidal_get(
            "https://api.tidal.com/v1/tracks",
            params={"isrc": clean_isrc, "countryCode": TIDAL_COUNTRY_CODE},
        )
        if note or not search_data:
            return [], note or "Tidal credits not found — used Spotify artists as fallback"

        track_items = _extract_items(search_data)
        if not track_items:
            return [], "Tidal credits not found — used Spotify artists as fallback"

        tidal_track_id = track_items[0].get("id") if isinstance(track_items[0], dict) else None
        if not tidal_track_id:
            return [], "Tidal track ID missing — used Spotify artists as fallback"

        contributors_data, note = _tidal_get(
            f"https://api.tidal.com/v1/tracks/{tidal_track_id}/contributors",
            params={"countryCode": TIDAL_COUNTRY_CODE},
        )
        if note or not contributors_data:
            return [], note or "Tidal credits not found — used Spotify artists as fallback"

        contributor_items = _extract_items(contributors_data)
        if not contributor_items:
            return [], "Tidal credits not found — used Spotify artists as fallback"

        names = []
        seen = set()
        for item in contributor_items:
            if not isinstance(item, dict):
                continue

            name = str(item.get("name") or "").strip()
            role = item.get("role")
            if not name or not _is_allowed_tidal_role(role):
                continue

            key = _lookup_key(name)
            if key in seen:
                continue
            seen.add(key)
            names.append(name)

        if not names:
            return [], "Tidal credits not found — used Spotify artists as fallback"

        return names, None

    except Exception:
        return [], "Tidal lookup failed — used Spotify artists as fallback"


# --------------------------------------------------------------------------
# Private GitHub IPI LIST source
# --------------------------------------------------------------------------
def _github_contents_api_url(owner, repo, path):
    encoded_path = "/".join(
        urllib.parse.quote(part, safe="")
        for part in str(path).strip("/").split("/")
        if part
    )
    return f"https://api.github.com/repos/{owner}/{repo}/contents/{encoded_path}"


@st.cache_data(ttl=IPI_LIST_CACHE_TTL_SECONDS, show_spinner=False)
def fetch_private_ipi_list_bytes(owner, repo, path, ref, token):
    """
    Fetches the private IPI LIST Excel from GitHub using the repository contents
    API. The token must be stored in Streamlit secrets, not in the repository.
    """
    url = _github_contents_api_url(owner, repo, path)
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.raw+json",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
        "User-Agent": "mms-matrix-catalog-generator",
    }

    response = requests.get(
        url,
        headers=headers,
        params={"ref": ref},
        timeout=GITHUB_CONTENTS_TIMEOUT_SECONDS,
    )

    if response.status_code in (401, 403):
        raise RuntimeError("Το GitHub token δεν έχει πρόσβαση στο ιδιωτικό IPI LIST repo.")
    if response.status_code == 404:
        raise RuntimeError("Δεν βρέθηκε το IPI LIST αρχείο στο ιδιωτικό GitHub repo.")

    response.raise_for_status()

    content_type = response.headers.get("Content-Type", "").lower()
    if "application/json" in content_type:
        # Defensive fallback if GitHub returns the default JSON representation
        # instead of raw bytes. The 'content' field is Base64 encoded.
        data = response.json()
        encoded_content = str(data.get("content") or "").replace("\n", "")
        if not encoded_content:
            raise RuntimeError("Το GitHub API δεν επέστρεψε περιεχόμενο για το IPI LIST.")
        file_bytes = base64.b64decode(encoded_content)
    else:
        file_bytes = response.content

    if not file_bytes:
        raise RuntimeError("Το IPI LIST αρχείο είναι κενό.")

    # .xlsx files are ZIP containers and normally start with PK. This catches
    # accidental HTML/JSON error payloads before openpyxl tries to parse them.
    if not file_bytes.startswith(b"PK"):
        raise RuntimeError("Το αρχείο που φορτώθηκε από GitHub δεν φαίνεται να είναι έγκυρο .xlsx.")

    return file_bytes


# --------------------------------------------------------------------------
# IPI LIST lookup helpers
# --------------------------------------------------------------------------
def _clean_text(value):
    if value is None:
        return ""
    return str(value).strip()


def _lookup_key(value):
    """Case-insensitive lookup key matching nickname.strip().lower()."""
    text = _clean_text(value)
    text = re.sub(r"\s+", " ", text)
    return text.lower()


def _parse_ipi(value):
    """Return IPI as an int where possible, so Excel stores it as a number."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else int(value)

    text = str(value).strip()
    if not text:
        return None

    compact = text.replace(" ", "").replace(",", "")
    try:
        number = Decimal(compact)
        if number == number.to_integral_value():
            return int(number)
    except (InvalidOperation, ValueError):
        pass

    digits_only = re.sub(r"\D+", "", text)
    if digits_only:
        return int(digits_only)

    return None


@st.cache_data(show_spinner=False)
def build_ipi_lookup_from_bytes(file_bytes):
    """
    Builds:
      {
        nickname.strip().lower(): {"legal": LEGAL NAME, "ipi": IPI, "pro": PRO},
        legal_name.strip().lower(): {"legal": LEGAL NAME, "ipi": IPI, "pro": PRO},
      }
    """
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True, read_only=True)
    try:
        if "IPI LIST" not in wb.sheetnames:
            raise ValueError("Το αρχείο δεν περιέχει sheet με όνομα 'IPI LIST'.")

        ws = wb["IPI LIST"]
        header_row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), None)
        if not header_row:
            raise ValueError("Το sheet 'IPI LIST' δεν έχει headers.")

        header_map = {
            str(header).strip().upper(): idx
            for idx, header in enumerate(header_row)
            if header is not None and str(header).strip()
        }
        required_headers = ["NICKNAME", "LEGAL NAME", "IPI", "PRO"]
        missing_headers = [h for h in required_headers if h not in header_map]
        if missing_headers:
            raise ValueError("Λείπουν headers από το IPI LIST: " + ", ".join(missing_headers))

        def get_cell(row, header_name):
            idx = header_map[header_name]
            return row[idx] if idx < len(row) else None

        lookup = {}
        source_row_count = 0

        for row in ws.iter_rows(min_row=2, values_only=True):
            nickname = _clean_text(get_cell(row, "NICKNAME"))
            legal_name = _clean_text(get_cell(row, "LEGAL NAME"))
            ipi = _parse_ipi(get_cell(row, "IPI"))
            pro = _clean_text(get_cell(row, "PRO"))

            if not nickname and not legal_name:
                continue

            entry = {
                "legal": legal_name or nickname,
                "ipi": ipi,
                "pro": pro,
            }

            nickname_key = _lookup_key(nickname)
            legal_key = _lookup_key(legal_name)

            if nickname_key and nickname_key not in lookup:
                lookup[nickname_key] = entry
            if legal_key and legal_key not in lookup:
                lookup[legal_key] = entry

            source_row_count += 1

        return lookup, source_row_count

    finally:
        wb.close()


def _contributor_row(name, ipi_lookup):
    raw_name = _clean_text(name)
    match = ipi_lookup.get(_lookup_key(raw_name)) if ipi_lookup else None
    if match:
        return {
            "raw": raw_name,
            "writer": match.get("legal") or raw_name,
            "ipi": match.get("ipi"),
            "pro": match.get("pro") or "",
            "matched": True,
        }

    return {
        "raw": raw_name,
        "writer": raw_name,
        "ipi": None,
        "pro": "",
        "matched": False,
    }


def _build_contributor_rows(names, ipi_lookup):
    rows = []
    seen = set()

    for name in names:
        if not _clean_text(name):
            continue

        row = _contributor_row(name, ipi_lookup)
        key = _lookup_key(row["writer"])
        if key in seen:
            continue
        seen.add(key)
        rows.append(row)

    return rows


# --------------------------------------------------------------------------
# Excel Generator (Zero-to-Excel) - builds in-memory
# --------------------------------------------------------------------------
def generate_new_catalog(tracks, ipi_lookup=None, progress_callback=None):
    ipi_lookup = ipi_lookup or {}

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Stay Independent Catalog"

    report = {
        "filled": [],
        "health_warnings": [],
        "tidal_fallbacks": [],
        "ipi_matches": 0,
    }

    # --- Ορισμός Στυλ (Γραμματοσειρές, Στοιχίσεις, Χρώματα, Περιγράμματα) ---
    header_font = Font(bold=True)
    center_alignment = Alignment(horizontal="center", vertical="center")
    top_alignment = Alignment(vertical="top", wrap_text=True)

    # Πιο έντονο μαύρο περίγραμμα (medium style)
    black_border = Border(
        left=Side(style='medium', color='000000'),
        right=Side(style='medium', color='000000'),
        top=Side(style='medium', color='000000'),
        bottom=Side(style='medium', color='000000')
    )

    # Γκρι γέμισμα για το διαχωριστικό κελί
    gray_fill = PatternFill(start_color="D9D9D9", end_color="D9D9D9", fill_type="solid")

    headers = ["TITLE", "ROLE", "WRITERS", "ISRC", "IPI", "PRO", "NOTES"]
    for col_num, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_num)
        cell.value = header
        cell.font = header_font
        cell.alignment = center_alignment
        cell.border = black_border

    ws.freeze_panes = "A2"
    ws.column_dimensions["A"].width = 35
    ws.column_dimensions["B"].width = 15
    ws.column_dimensions["C"].width = 32
    ws.column_dimensions["D"].width = 20
    ws.column_dimensions["E"].width = 16
    ws.column_dimensions["F"].width = 18
    ws.column_dimensions["G"].width = 55

    insert_at = 2
    total_tracks = len(tracks)

    for track_index, track in enumerate(tracks, start=1):
        title = track.get("name") or ""
        spotify_artists = track.get("artists") or []
        isrc = track.get("isrc")
        clean_isrc = str(isrc or "").replace("-", "").strip().upper()

        if progress_callback:
            progress_callback(track_index, total_tracks, title)

        notes = []
        tidal_names = []
        tidal_note = None
        should_try_tidal = bool(clean_isrc) and validate_isrc(clean_isrc)

        if clean_isrc and not validate_isrc(clean_isrc):
            report["health_warnings"].append((title, isrc))
            notes.append("ISRC format invalid")
        elif not clean_isrc:
            notes.append("Missing ISRC")

        if should_try_tidal:
            tidal_names, tidal_note = fetch_tidal_contributors_by_isrc(clean_isrc)

        if tidal_names:
            contributor_names = tidal_names
            contributor_source = "tidal"
        else:
            contributor_names = spotify_artists
            contributor_source = "spotify_fallback"

            if should_try_tidal:
                note = tidal_note or "Tidal credits not found — used Spotify artists as fallback"
                notes.append(note)
                report["tidal_fallbacks"].append(title)
            elif clean_isrc:
                notes.append("Tidal lookup skipped — used Spotify artists as fallback")
            else:
                notes.append("Tidal lookup skipped — used Spotify artists as fallback")

        contributor_rows = _build_contributor_rows(contributor_names, ipi_lookup)
        report["ipi_matches"] += sum(1 for row in contributor_rows if row["matched"])

        # Δημιουργούμε ακριβώς όσες γραμμές χρειάζονται για τους writers
        needed_rows = max(1, len(contributor_rows))
        notes_text = "; ".join(dict.fromkeys(note for note in notes if note))

        for i in range(needed_rows):
            current_row = insert_at + i

            # Επαναλαμβάνουμε TITLE, ISRC και NOTES σε ΚΑΘΕ γραμμή του πλαισίου
            ws.cell(row=current_row, column=1).value = title
            if isrc:
                ws.cell(row=current_row, column=4).value = isrc
            if notes_text:
                ws.cell(row=current_row, column=7).value = notes_text

            if i < len(contributor_rows):
                contributor = contributor_rows[i]
                ws.cell(row=current_row, column=3).value = contributor["writer"]

                if contributor["ipi"] is not None:
                    ipi_cell = ws.cell(row=current_row, column=5)
                    ipi_cell.value = contributor["ipi"]
                    ipi_cell.number_format = "0"

                if contributor["pro"]:
                    ws.cell(row=current_row, column=6).value = contributor["pro"]

            # Εφαρμογή του μαύρου περιγράμματος σε όλα τα κελιά της τρέχουσας γραμμής
            for col_num in range(1, 8):
                cell = ws.cell(row=current_row, column=col_num)
                cell.alignment = top_alignment
                cell.border = black_border

        # --- Προσθήκη Γκρι Διαχωριστικής Γραμμής ---
        separator_row = insert_at + needed_rows
        for col_num in range(1, 8):
            cell = ws.cell(row=separator_row, column=col_num)
            cell.fill = gray_fill
            cell.border = black_border

        report["filled"].append(
            {
                "title": title,
                "contributors": [row["writer"] for row in contributor_rows] if contributor_rows else [],
                "isrc": isrc,
                "source": contributor_source,
                "notes": notes_text,
            }
        )

        # Το επόμενο τραγούδι ξεκινά μετά το block των writers + 1 γραμμή για το διαχωριστικό
        insert_at += needed_rows + 1

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer, report


# --------------------------------------------------------------------------
# Filename helper
# --------------------------------------------------------------------------
def make_catalog_filename(playlist_name):
    safe_name = re.sub(r"\s+", "_", str(playlist_name or "playlist").strip())
    safe_name = unicodedata.normalize("NFKD", safe_name).encode("ascii", "ignore").decode("ascii")
    safe_name = re.sub(r"[^A-Za-z0-9_-]+", "", safe_name).strip("_")
    if not safe_name:
        safe_name = "playlist"

    date_part = datetime.now().strftime("%Y%m%d")
    return f"Catalog_{safe_name}_{date_part}.xlsx"


# --------------------------------------------------------------------------
# Streamlit UI
# --------------------------------------------------------------------------
# Για το εικονίδιο πάνω στην καρτέλα του browser βάζεις το όνομα του αρχείου
st.set_page_config(page_title="Stay Independent Catalog Generator", page_icon="StayLogo.png")

# Για να εμφανιστεί το λογότυπο μέσα στη σελίδα
st.image("Staylogo.png", width=200) # Μπορείς να αλλάξεις το width (πλάτος) για να φαίνεται στο μέγεθος που θες

# Ο τίτλος πλέον καθαρός, χωρίς τη νότα
st.title("Stay Independent Catalog Generator")

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

st.subheader("1. IPI LIST")
try:
    private_ipi_config = get_private_ipi_config()
    with st.spinner("Φόρτωση IPI LIST από το ιδιωτικό GitHub repo..."):
        ipi_file_bytes = fetch_private_ipi_list_bytes(**private_ipi_config)
        ipi_lookup, ipi_source_rows = build_ipi_lookup_from_bytes(ipi_file_bytes)
    st.success(f"Το IPI LIST φορτώθηκε ως ground truth: {ipi_source_rows} εγγραφές.")
except Exception as e:
    st.error(
        "Δεν ήταν δυνατή η φόρτωση του IPI LIST από το ιδιωτικό GitHub repo. "
        "Η δημιουργία Excel σταματά για να μην παραχθεί catalog χωρίς ground truth."
    )
    st.caption(f"Τεχνική λεπτομέρεια: {e}")
    st.stop()

try:
    with st.spinner("Φόρτωση των playlists σας..."):
        playlists = fetch_user_playlists(token)
except requests.HTTPError as e:
    st.error(f"Σφάλμα επικοινωνίας με το Spotify: {e}")
    st.stop()

if not playlists:
    st.warning("Δεν βρέθηκαν playlists στο λογαριασμό σας.")
    st.stop()

st.subheader("2. Playlist")
playlist_names = [p["name"] for p in playlists]
selected_name = st.selectbox("Επιλέξτε playlist", playlist_names)
selected_playlist = next(p for p in playlists if p["name"] == selected_name)

if st.button("Δημιουργία Excel ✔️", type="primary"):
    try:
        with st.spinner("Ανάκτηση τραγουδιών από Spotify..."):
            tracks = fetch_playlist_tracks(token, selected_playlist["id"])

        if not tracks:
            st.warning("Δεν βρέθηκαν τραγούδια σε αυτή την playlist.")
            st.stop()

        st.success(f"Βρέθηκαν {len(tracks)} τραγούδια!")

        progress_bar = st.progress(0.0)
        status_placeholder = st.empty()

        def update_generation_progress(current, total, title):
            progress_value = (current - 1) / max(total, 1)
            progress_bar.progress(progress_value)
            status_placeholder.write(f"🔍 Ανάκτηση credits για «{title}»...")

        buffer, report = generate_new_catalog(
            tracks,
            ipi_lookup=ipi_lookup,
            progress_callback=update_generation_progress,
        )

        progress_bar.progress(1.0)
        status_placeholder.success("Το Excel δημιουργήθηκε επιτυχώς.")

        if report["health_warnings"]:
            st.warning("⚠️ Προειδοποιήσεις ISRC:")
            for title, isrc in report["health_warnings"]:
                st.write(f"- **{title}**: Άκυρο ISRC ({isrc})")

        if report["tidal_fallbacks"]:
            st.warning("⚠️ Τα παρακάτω τραγούδια δεν βρέθηκαν στο Tidal (χρησιμοποιήθηκαν καλλιτέχνες Spotify αντί για credits):")
            for title in report["tidal_fallbacks"]:
                st.write(f"- **{title}**")

        if ipi_lookup:
            st.info(f"Έγιναν {report['ipi_matches']} αντιστοιχίσεις IPI/PRO στο Excel.")

        output_filename = make_catalog_filename(selected_playlist["name"])
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
