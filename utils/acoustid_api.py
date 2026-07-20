"""
utils/acoustid_api.py

Ανάλυση αρχείων ήχου (Audio Fingerprinting) και ταυτοποίηση μέσω AcoustID.
"""

import subprocess
import json
import requests
import streamlit as st

def get_acoustid_key():
    """Ανάκτηση του AcoustID API key από τα Streamlit secrets."""
    return st.secrets.get("ACOUSTID_API_KEY")

def generate_audio_fingerprint(file_path):
    """
    Παράγει το fingerprint ενός αρχείου ήχου χρησιμοποιώντας το τοπικό εργαλείο fpcalc.
    Επιστρέφει (duration, fingerprint).
    """
    try:
        # Εκτέλεση του εργαλείου chromaprint (fpcalc)
        result = subprocess.run(
            ['fpcalc', '-json', file_path],
            capture_output=True,
            text=True,
            check=True
        )
        data = json.loads(result.stdout)
        return data.get("duration"), data.get("fingerprint")
    except FileNotFoundError:
        raise RuntimeError(
            "Το εργαλείο 'fpcalc' δεν βρέθηκε. Αν είστε στο Streamlit Cloud, "
            "δημιουργήστε ένα αρχείο `packages.txt` με το περιεχόμενο `libchromaprint-tools`."
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Αποτυχία ανάλυσης αρχείου (fpcalc error): {e.stderr}")
    except Exception as e:
        raise RuntimeError(f"Απροσδόκητο σφάλμα κατά την ανάλυση: {e}")

@st.cache_data(ttl=3600, show_spinner=False)
def lookup_acoustid(duration, fingerprint):
    """
    Στέλνει το fingerprint στο AcoustID API και επιστρέφει αντιστοιχίσεις MusicBrainz.
    """
    api_key = get_acoustid_key()
    if not api_key:
        raise ValueError("Λείπει το ACOUSTID_API_KEY από τα st.secrets.")
    
    url = "https://api.acoustid.org/v2/lookup"
    payload = {
        "client": api_key,
        "meta": "recordings+compress",  # Επιστρέφει Recordings του MB
        "duration": int(duration),
        "fingerprint": fingerprint
    }
    
    try:
        resp = requests.post(url, data=payload, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        raise RuntimeError(f"Αποτυχία επικοινωνίας με AcoustID: {e}")
