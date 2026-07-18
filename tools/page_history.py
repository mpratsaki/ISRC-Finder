"""
tools/page_history.py

"Ιστορικό & Αρχεία" page: shows a user's previous exports from Supabase,
lets them download the generated Excel files, or permanently delete
selected records (both the DB row and the Storage object).
"""

import time

import pandas as pd
import streamlit as st

from core.database import init_supabase


def page_history(token, spotify_user):
    st.title("📂 Ιστορικό Εξαγωγών")
    st.markdown("Εδώ μπορείτε να δείτε τις προηγούμενες εξαγωγές σας, να κατεβάσετε τα αρχεία Excel, ή να τα διαγράψετε οριστικά.")

    supabase = init_supabase()
    if not supabase:
        st.warning("Το ιστορικό δεν είναι διαθέσιμο (Λείπουν τα credentials του Supabase στα Secrets).")
        return

    try:
        # Φέρνουμε τα δεδομένα - φέρνουμε και το μοναδικό "id" της εγγραφής
        response = supabase.table("export_history") \
            .select("id, playlist_name, track_count, exported_at, file_url") \
            .eq("spotify_user", spotify_user) \
            .order("exported_at", desc=True) \
            .execute()

        if response.data:
            df_history = pd.DataFrame(response.data)
            df_history["exported_at"] = pd.to_datetime(df_history["exported_at"]).dt.tz_convert("Europe/Athens").dt.strftime("%d-%m-%Y %H:%M:%S")

            # Προσθέτουμε μια προσωρινή στήλη (Checkbox) στην αρχή του Dataframe
            df_history.insert(0, "Επιλογή", False)

            st.markdown("### Λίστα Αρχείων")
            st.caption("Επιλέξτε το κουτάκι αριστερά από τις εγγραφές που θέλετε να διαγράψετε.")

            # Χρήση data_editor για διαδραστικότητα
            edited_df = st.data_editor(
                df_history,
                width="stretch",
                hide_index=True,
                column_config={
                    "id": None,  # Κρύβουμε το ID από τον χρήστη για να είναι καθαρό το UI
                    "Επιλογή": st.column_config.CheckboxColumn("Διαγραφή;", default=False),
                    "playlist_name": st.column_config.TextColumn("Τίτλος Playlist", disabled=True),
                    "track_count": st.column_config.NumberColumn("Τραγούδια", disabled=True),
                    "exported_at": st.column_config.TextColumn("Ημερομηνία & Ώρα", disabled=True),
                    "file_url": st.column_config.LinkColumn(
                        "Αρχείο Excel",
                        help="Πατήστε για να κατεβάσετε το αρχείο",
                        display_text="Κατέβασμα 📥",
                        disabled=True
                    )
                }
            )

            # Εντοπισμός των γραμμών που ο χρήστης τσέκαρε
            rows_to_delete = edited_df[edited_df["Επιλογή"] == True]

            if not rows_to_delete.empty:
                st.warning(f"Έχετε επιλέξει {len(rows_to_delete)} αρχεία προς διαγραφή. Η ενέργεια δεν αναιρείται.")

                if st.button("🗑️ Οριστική Διαγραφή Επιλεγμένων", type="primary"):
                    with st.spinner("Διαγραφή σε εξέλιξη..."):
                        success_count = 0

                        for _, row in rows_to_delete.iterrows():
                            record_id = row["id"]
                            file_url = row["file_url"]

                            # Βήμα 1: Διαγραφή του αρχείου από το Supabase Storage
                            if file_url:
                                try:
                                    # Το URL είναι της μορφής: .../public/catalogs/User/1234_File.xlsx
                                    # Το κόβουμε για να πάρουμε μόνο το "User/1234_File.xlsx"
                                    if "/public/catalogs/" in file_url:
                                        storage_path = file_url.split("/public/catalogs/")[-1]
                                        # Προσοχή: Η remove δέχεται λίστα με paths
                                        supabase.storage.from_("catalogs").remove([storage_path])
                                except Exception as e:
                                    st.error(f"Αδυναμία διαγραφής αρχείου από το Storage: {e}")

                            # Βήμα 2: Διαγραφή της εγγραφής από τη βάση δεδομένων (Table)
                            try:
                                supabase.table("export_history").delete().eq("id", record_id).execute()
                                success_count += 1
                            except Exception as e:
                                st.error(f"Αδυναμία διαγραφής εγγραφής {record_id} από τη βάση: {e}")

                        if success_count > 0:
                            st.success(f"Διαγράφηκαν επιτυχώς {success_count} εγγραφές/αρχεία!")
                            time.sleep(1.5)  # Μικρή παύση για να προλάβει ο χρήστης να διαβάσει το μήνυμα
                            st.rerun()  # Ανανέωση της σελίδας για να ενημερωθεί ο πίνακας
        else:
            st.info("Δεν υπάρχει ιστορικό εξαγωγών για τον λογαριασμό σας.")
    except Exception as e:
        st.error(f"Αδυναμία ανάκτησης ιστορικού: {e}")
