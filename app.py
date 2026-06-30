import streamlit as st
import os
import re
import openpyxl
from openpyxl.styles import Font, Alignment
import requests
import base64
import time

# --- Ρυθμίσεις Σελίδας ---
st.set_page_config(page_title="MMS Matrix | Stay Independent", page_icon="🎵", layout="centered")

# --- Κρυφοί Κωδικοί (Θα τους βάλουμε στα Secrets του Server) ---
# Στο Streamlit Cloud δεν έχουμε .env, χρησιμοποιούμε st.secrets
try:
    CLIENT_ID = st.secrets["SPOTIFY_CLIENT_ID"]
    CLIENT_SECRET = st.secrets["SPOTIFY_CLIENT_SECRET"]
except FileNotFoundError:
    st.error("Σφάλμα: Τα διαπιστευτήρια του Spotify δεν βρέθηκαν στα Secrets του server.")
    st.stop()

# (ΟΛΕΣ ΟΙ ΣΥΝΑΡΤΗΣΕΙΣ SPOTIFY & EXCEL ΠΟΥ ΗΔΗ ΕΧΟΥΜΕ ΜΠΑΙΝΟΥΝ ΕΔΩ: 
# _refresh_token, get_access_token, fetch_playlist_tracks, validate_isrc, generate_new_catalog)

# (Σημείωση: Στο Web App, η αυθεντικοποίηση με Spotify πρέπει να γίνει με 'Client Credentials Flow' 
# αντί για 'Authorization Code' για να μην ζητάει login από κάθε χρήστη, αφού δεν μας ενδιαφέρουν private playlists. 
# Αν η playlist είναι public, αυτό είναι 100 φορές πιο εύκολο!)

def get_app_token(client_id, client_secret):
    """Απλό Server-to-Server token για public playlists"""
    auth_string = f"{client_id}:{client_secret}"
    auth_base64 = base64.b64encode(auth_string.encode("utf-8")).decode("utf-8")
    url = "https://accounts.spotify.com/api/token"
    headers = {
        "Authorization": "Basic " + auth_base64,
        "Content-Type": "application/x-www-form-urlencoded"
    }
    data = {"grant_type": "client_credentials"}
    result = requests.post(url, headers=headers, data=data)
    json_result = json.loads(result.content)
    return json_result["access_token"]

# --- Το Γραφικό Περιβάλλον του Web App ---
st.title("🎵 MMS Matrix")
st.subheader("Stay Independent Catalog Generator")

st.markdown("Επικολλήστε το link μιας **Δημόσιας (Public)** Spotify Playlist για να δημιουργήσετε αυτόματα το Excel του καταλόγου σας.")

playlist_url = st.text_input("🔗 Link της Playlist:", placeholder="π.χ. https://open.spotify.com/playlist/...")

if st.button("Γεννήτρια Excel 🚀"):
    if not playlist_url:
        st.warning("Παρακαλώ εισάγετε ένα link.")
    else:
        with st.spinner('Σύνδεση με Spotify & Λήψη δεδομένων...'):
            try:
                token = get_app_token(CLIENT_ID, CLIENT_SECRET)
                playlist_id = extract_playlist_id(playlist_url)
                tracks = fetch_playlist_tracks(token, playlist_id)
                
                output_filename = f"Release_{playlist_id[:8]}.xlsx"
                report = generate_new_catalog(tracks, output_filename)
                
                st.success(f"Επιτυχία! Βρέθηκαν και μορφοποιήθηκαν {len(report['filled'])} τραγούδια.")
                
                if report.get("health_warnings"):
                    st.warning("⚠️ Προσοχή: Βρέθηκαν άκυρα ISRCs σε κάποια τραγούδια!")
                
                # Κουμπί για Download του Excel
                with open(output_filename, "rb") as file:
                    st.download_button(
                        label="📥 Κατέβασμα Αρχείου Excel",
                        data=file,
                        file_name=output_filename,
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                    )
            except Exception as e:
                st.error(f"Κάτι πήγε στραβά: {e}")