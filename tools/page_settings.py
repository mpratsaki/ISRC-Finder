"""
tools/page_settings.py

"Ρυθμίσεις" page: cache management and system info.
"""

import streamlit as st

APP_VERSION = "Stay Independent Tool v2.0 - Swiss Army Knife Edition"


def page_settings(spotify_user):
    st.title("⚙️ Ρυθμίσεις")

    tab_data, tab_system = st.tabs(["Δεδομένα", "Σύστημα"])

    with tab_data:
        st.markdown("### Διαχείριση Cache")
        st.caption(
            "Το IPI LIST και άλλα δεδομένα αποθηκεύονται προσωρινά για ταχύτητα. "
            "Καθαρίστε το cache αν ενημερώσατε το IPI LIST στο GitHub."
        )
        if st.button("🧹 Εκκαθάριση Προσωρινής Μνήμης (Clear Cache)", type="primary"):
            st.cache_data.clear()
            st.toast("Το cache καθαρίστηκε επιτυχώς!", icon="✅")
            st.success("Η προσωρινή μνήμη εκκαθαρίστηκε. Τα δεδομένα θα φορτωθούν ξανά.")

    with tab_system:
        st.markdown("### Πληροφορίες Συστήματος")
        st.text_input("Active Spotify User", value=spotify_user or "—", disabled=True)
        st.text_input("Έκδοση", value=APP_VERSION, disabled=True)
        st.caption("Stay Independent Tool © 2026")
