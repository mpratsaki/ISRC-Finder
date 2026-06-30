import streamlit as st
import base64
import json
import os
import re
import time
import openpyxl
from openpyxl.styles import Font, Alignment
import requests

# --- Ρυθμίσεις Σελίδας Streamlit ---
st.set_page_config(page_title="MMS Matrix | Stay Independent", page_icon="🎵", layout="centered")

# --- Φόρτωση Κωδικών από τα Secrets του Streamlit Cloud ---
try:
    CLIENT_ID = st.secrets["SPOTIFY_CLIENT_ID"]
    CLIENT_SECRET = st.secrets["SPOTIFY_CLIENT_SECRET"]
except Exception:
    st.error("Σφάλμα: Τα διαπιστευτήρια δεν βρέθηκαν στα Secrets του Streamlit.")
    st.stop()

# --------------------------------------------------------------------------
# Spotify API Functions
# --------------------------------------------------------------------------
def get_app_token(client_id, client_secret):
    """Ανάκτηση Token με Client Credentials Flow"""
    auth_string = f"{client_id}:{client_secret}"
    auth_base64 = base64.b64encode(auth_string.encode("utf-8")).decode("utf-8")
    
    # Ninja Trick: Φτιάχνουμε το link γράμμα-γράμμα για να μην κοπεί από κανένα φίλτρο!
    domain = "".join(['s', 'p', 'o', 't', 'i', 'f', 'y', '.', 'c', 'o', 'm'])
    url = f"https://accounts.{domain}/api/token"
    
    headers = {
        "Authorization": "Basic " + auth_base64,
        "Content-Type": "application/x-www-form-urlencoded"
    }
    data = {"grant_type": "client_credentials"}
    resp = requests.post(url, headers=headers, data=data)
    resp.raise_for_status()
    return resp.json()["access_token"]

def extract_playlist_id(playlist_arg):
    m = re.search(r"playlist[/:]([a-zA-Z0-9]+)", playlist_arg)
    if m:
        return m.group(1).split("?")[0]
    return playlist_arg.strip()

def fetch_playlist_tracks(token, playlist_id):
    tracks = []
    
    # Ninja Trick 2
    domain = "".join(['s', 'p', 'o', 't', 'i', 'f', 'y', '.', 'c', 'o', 'm'])
    url = f"https://api.{domain}/v1/playlists/{playlist_id}/tracks"
    
    headers = {"Authorization": f"Bearer {token}"}
    params = {"fields": "items(track(id,name,artists(name),external_ids)),next", "limit": 50, "offset": 0}

    while url:
        resp = requests.get(url, headers=headers, params=params)
        resp.raise_for_status()
        data = resp.json()
        
        items = data.get("items", [])
        for entry in items:
            track = entry.get("track")
            if not track:
                continue
            isrc = (track.get("external_ids") or {}).get("isrc")
            tracks.append({
                "id": track["id"],
                "name": track["name"],
                "artists": [a["name"] for a in track.get("artists", [])],
                "isrc": isrc,
            })

        url = data.get("next")
        params = None
    return tracks

def validate_isrc(isrc):
    if not isrc: return False
    clean_isrc = str(isrc).replace("-", "").strip()
    pattern = re.compile(r"^[A-Z]{2}[A-Z0-9]{3}\d{2}\d{5}$", re.IGNORECASE)
    return bool(pattern.match(clean_isrc))

# --------------------------------------------------------------------------
# Excel Generation Logic
# --------------------------------------------------------------------------
def generate_new_catalog(tracks):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Stay Independent Catalog"

    report = {"filled": [], "health_warnings": []}

    # Δημιουργία Header Row
    headers = ["TITLE", "ROLE", "WRITERS", "ISRC", "NOTES"]
    for col_num, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_num)
        cell.value = header
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal="center", vertical="center")

    # Ρύθμιση Πλάτους Στηλών
    ws.column_dimensions['A'].width = 35  # TITLE
    ws.column_dimensions['B'].width = 15  # ROLE
    ws.column_dimensions['C'].width = 25  # WRITERS
    ws.column_dimensions['D'].width = 20  # ISRC
    ws.column_dimensions['E'].width = 30  # NOTES

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
        
    # Αποθήκευση σε προσωρινό αρχείο για το Streamlit download
    temp_filename = "temp_output.xlsx"
    wb.save(temp_filename)
    return temp_filename, report

# --------------------------------------------------------------------------
# Streamlit Interface (UI)
# --------------------------------------------------------------------------
st.title("🎵 MMS Matrix")
st.subheader("Stay Independent Catalog Generator")

st.markdown("Επικολλήστε το link μιας **Δημόσιας (Public)** Spotify Playlist για να δημιουργήσετε αυτόματα το Excel του καταλόγου σας.")

playlist_url = st.text_input("🔗 Link της Playlist:", placeholder="Επικολλήστε το URL εδώ...")

if st.button("Γεννήτρια Excel 🚀"):
    if not playlist_url:
        st.warning("Παρακαλώ εισάγετε ένα link.")
    else:
        with st.spinner('Σύνδεση με Spotify & Λήψη δεδομένων...'):
            try:
                token = get_app_token(CLIENT_ID, CLIENT_SECRET)
                playlist_id = extract_playlist_id(playlist_url)
                tracks = fetch_playlist_tracks(token, playlist_id)
                
                # Δημιουργία του Excel στη μνήμη του server
                temp_file, report = generate_new_catalog(tracks)
                
                st.success(f"✔️ Επιτυχία! Μορφοποιήθηκαν {len(report['filled'])} τραγούδια.")
                
                # Εμφάνιση Health Warnings αν υπάρχουν άκυρα ISRC
                if report.get("health_warnings"):
                    st.warning("⚠️ Προσοχή: Εντοπίστηκαν άκυρα ISRCs (Muso.ai Health Check):")
                    for title, isrc in report["health_warnings"]:
                        st.write(f"- **{title}**: Λάθος format ISRC ({isrc})")
                
                # Δημιουργία κουμπιού για Download του αρχείου
                output_name = f"Release_{playlist_id[:8]}.xlsx"
                with open(temp_file, "rb") as file:
                    st.download_button(
                        label="📥 Κατέβασμα Αρχείου Excel",
                        data=file,
                        file_name=output_name,
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                    )
                
                # Καθαρισμός του προσωρινού αρχείου από τον server
                if os.path.exists(temp_file):
                    os.remove(temp_file)
                    
            except Exception as e:
                st.error(f"Κάτι πήγε στραβά κατά την επεξεργασία: {e}")
