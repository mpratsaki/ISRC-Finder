"""
utils/coverart_api.py

Επικοινωνία με το Cover Art Archive (coverartarchive.org).
Παρέχει caching για την αποφυγή περιττών κλήσεων και προστασία από 404/503.
"""

import requests
import streamlit as st

CAA_BASE_URL = "https://coverartarchive.org"
USER_AGENT = "StayIndependentTool/2.0 ( johnnakas03@gmail.com )"

@st.cache_data(ttl=86400, show_spinner=False)
def fetch_cover_art_url(mbid, entity_type="release"):
    """
    Αναζητά το URL της μπροστινής εικόνας (front cover) για ένα Release ή Release Group.
    Επιστρέφει το URL αν βρεθεί, αλλιώς None.
    """
    if entity_type not in ["release", "release-group"]:
        return None

    url = f"{CAA_BASE_URL}/{entity_type}/{mbid}"
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}

    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            images = data.get("images", [])
            
            # Αναζήτηση για εικόνα με flag 'front'
            for img in images:
                if img.get("front"):
                    return img.get("image")
            
            # Fallback στην πρώτη διαθέσιμη εικόνα αν δεν υπάρχει explicitly 'front'
            if images:
                return images[0].get("image")
                
    except Exception:
        # Αγνοούμε σφάλματα (π.χ. timeout) για να μην κρασάρει το UI
        pass
        
    return None
