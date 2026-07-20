"""
tools/page_audio_id.py

Εργαλείο Αναγνώρισης Ήχου (Audio ID) μέσω AcoustID.
Επιτρέπει τον έλεγχο QA σε audio files πριν την κυκλοφορία για την 
εύρεση διπλότυπων ηχογραφήσεων/ISRCs στο MusicBrainz.
"""

import tempfile
import os
import pandas as pd
import streamlit as st

from utils.acoustid_api import generate_audio_fingerprint, lookup_acoustid
from utils.musicbrainz_api import mb_entity_url

def page_audio_id():
    st.title("AcoustID: Αναγνώριση Ήχου")
    st.caption(
        "Ανεβάστε ένα αρχείο ήχου (.mp3, .wav, .flac) για να δημιουργήσετε το ακουστικό του αποτύπωμα "
        "και να βρείτε αν η ηχογράφηση υπάρχει ήδη στο MusicBrainz (Αποφυγή διπλότυπων ISRC)."
    )

    if not st.secrets.get("ACOUSTID_API_KEY"):
        st.warning("⚠️ Λείπει το `ACOUSTID_API_KEY` από τα secrets. Η λειτουργία δεν μπορεί να εκτελεστεί.")
        return

    uploaded_file = st.file_uploader("Επιλέξτε αρχείο ήχου", type=["mp3", "wav", "flac", "ogg"])

    if uploaded_file is not None:
        if st.button("🔍 Ανάλυση & Αναγνώριση", type="primary", width="stretch"):
            with st.spinner("Παραγωγή ακουστικού αποτυπώματος (Fingerprinting)..."):
                try:
                    # Αποθήκευση στο δίσκο προσωρινά για το fpcalc
                    with tempfile.NamedTemporaryFile(delete=False, suffix=f".{uploaded_file.name.split('.')[-1]}") as tmp:
                        tmp.write(uploaded_file.getvalue())
                        tmp_path = tmp.name

                    duration, fingerprint = generate_audio_fingerprint(tmp_path)
                    os.unlink(tmp_path)  # Καθαρισμός

                    if not fingerprint:
                        st.error("Δεν ήταν δυνατή η παραγωγή αποτυπώματος για αυτό το αρχείο.")
                        return

                except Exception as e:
                    st.error(str(e))
                    return

            with st.spinner("Αναζήτηση στη βάση του AcoustID..."):
                try:
                    result = lookup_acoustid(duration, fingerprint)
                except Exception as e:
                    st.error(str(e))
                    return

            status = result.get("status")
            if status != "ok":
                st.error(f"Το API επέστρεψε σφάλμα: {status}")
                return

            matches = result.get("results", [])
            if not matches:
                st.info("Δεν βρέθηκε καμία αντιστοιχία στη βάση του AcoustID. Η ηχογράφηση φαίνεται να είναι εντελώς νέα!")
                return

            st.success(f"Βρέθηκαν {len(matches)} πιθανές αντιστοιχίσεις.")
            
            # Εξαγωγή και μορφοποίηση δεδομένων
            rows = []
            for match in matches:
                score = match.get("score", 0)
                recordings = match.get("recordings", [])
                for rec in recordings:
                    artists = [a.get("name") for a in rec.get("artists", [])]
                    artist_str = " / ".join(artists) if artists else "Άγνωστος Καλλιτέχνης"
                    
                    rows.append({
                        "Ακρίβεια": f"{int(score * 100)}%",
                        "Τίτλος": rec.get("title", "—"),
                        "Καλλιτέχνης": artist_str,
                        "Διάρκεια MB": f"{rec.get('duration', 0)} δευτ.",
                        "Recording MBID": rec.get("id"),
                        "Link": mb_entity_url("recording", rec.get("id"))
                    })

            if rows:
                st.dataframe(
                    pd.DataFrame(rows),
                    width="stretch",
                    hide_index=True,
                    column_config={
                        "Link": st.column_config.LinkColumn("MusicBrainz", display_text="Άνοιγμα 🔗")
                    }
                )
            else:
                st.warning("Το AcoustID βρήκε match, αλλά χωρίς συνδεδεμένα MusicBrainz Recordings.")
