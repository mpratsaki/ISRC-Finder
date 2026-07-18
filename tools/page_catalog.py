"""
tools/page_catalog.py

"Γεννήτρια Catalog" page: lets the logged-in user pick one of their own
Spotify playlists, generates the enriched Stay Independent Catalog Excel
file, uploads it to Supabase Storage, and logs it to export_history.
"""

import time

import pandas as pd
import requests
import streamlit as st

from core.auth_spotify import fetch_user_playlists, fetch_playlist_tracks
from core.database import init_supabase
from utils.github_fetcher import (
    get_private_ipi_config,
    fetch_private_ipi_list_bytes,
    build_ipi_lookup_from_bytes,
)
from utils.excel_engine import generate_new_catalog, make_catalog_filename


def page_catalog_generator(token, spotify_user):
    st.title("Γεννήτρια Catalog")

    # Φόρτωση IPI List
    try:
        private_ipi_config = get_private_ipi_config()
        with st.spinner("Φόρτωση IPI LIST από το GitHub..."):
            ipi_file_bytes = fetch_private_ipi_list_bytes(**private_ipi_config)
            ipi_lookup, ipi_source_rows = build_ipi_lookup_from_bytes(ipi_file_bytes)
    except Exception as e:
        st.error("Αδυναμία φόρτωσης IPI LIST από το ιδιωτικό repository.")
        st.caption(f"Λεπτομέρεια συστήματος: {e}")
        st.stop()

    # Ανάκτηση Playlists
    try:
        playlists = fetch_user_playlists(token)
    except requests.HTTPError as e:
        st.error(f"Σφάλμα επικοινωνίας με το Spotify: {e}")
        st.stop()

    if not playlists:
        st.warning("Δεν βρέθηκαν playlists στον λογαριασμό σας.")
        st.stop()

    st.markdown("### Επιλογή Δεδομένων")
    playlist_names = [p["name"] for p in playlists]

    col_sel, col_btn = st.columns([3, 1], vertical_alignment="bottom")
    with col_sel:
        selected_name = st.selectbox("Επιλέξτε Playlist για εξαγωγή:", playlist_names)
        selected_playlist = next(p for p in playlists if p["name"] == selected_name)

    with col_btn:
        generate_trigger = st.button("Δημιουργία Catalog", type="primary", width="stretch")

    if generate_trigger:
        st.divider()
        try:
            tracks = fetch_playlist_tracks(token, selected_playlist["id"])

            if not tracks:
                st.warning("Η playlist είναι κενή.")
                st.stop()

            st.markdown("#### Live Activity")
            live_status = st.empty()
            progress_bar = st.progress(0.0)

            def update_generation_progress(current, total, title):
                progress_value = current / max(total, 1)
                progress_bar.progress(progress_value)
                html_content = f"""
                <div class="live-activity-box">
                    <span style="color:#aaa; font-size:14px;">Επεξεργασία {current} από {total}</span><br>
                    <strong style="font-size:18px;">🎵 {title}</strong>
                </div>
                """
                live_status.markdown(html_content, unsafe_allow_html=True)

            buffer, report = generate_new_catalog(
                tracks,
                ipi_lookup=ipi_lookup,
                progress_callback=update_generation_progress,
            )

            live_status.empty()
            progress_bar.empty()
            st.toast("Η δημιουργία του Excel ολοκληρώθηκε!", icon="🎉")

            output_filename = make_catalog_filename(selected_playlist["name"])

            # ΠΡΟΣΘΗΚΗ 2: Αποθήκευση στο Supabase Storage
            supabase = init_supabase()
            file_public_url = None

            if supabase and spotify_user:
                try:
                    # Μετατροπή του BytesIO σε raw bytes
                    file_bytes = buffer.getvalue()

                    # Δημιουργία μοναδικού ονόματος για το αρχείο στο bucket
                    timestamp = int(time.time())
                    storage_path = f"{spotify_user}/{timestamp}_{output_filename}"

                    # Upload στο bucket με όνομα "catalogs" (πρέπει να το φτιάξεις στο Supabase!)
                    supabase.storage.from_("catalogs").upload(
                        file=file_bytes,
                        path=storage_path,
                        file_options={"content-type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
                    )
                    # Ανάκτηση του Public URL
                    file_public_url = supabase.storage.from_("catalogs").get_public_url(storage_path)

                    # Εγγραφή ιστορικού στη Βάση Δεδομένων με το URL
                    supabase.table("export_history").insert({
                        "spotify_user": spotify_user,
                        "playlist_name": selected_playlist["name"],
                        "track_count": len(tracks),
                        "file_url": file_public_url  # Η νέα στήλη
                    }).execute()

                except Exception as e:
                    st.error(f"🚨 Σφάλμα επικοινωνίας με το Supabase: {e}")
                    st.toast("Δεν ενημερώθηκε το ιστορικό.", icon="⚠️")

            # Εμφάνιση Αποτελεσμάτων
            st.markdown("### 📊 Αποτελέσματα & Εξαγωγή")
            tab_summary, tab_preview, tab_logs = st.tabs(["Σύνοψη", "Προεπισκόπηση", "Σφάλματα & Logs"])

            with tab_summary:
                m1, m2, m3 = st.columns(3)
                m1.metric("Σύνολο Τραγουδιών", len(tracks))
                m2.metric("Επιτυχείς Αντιστοιχίσεις IPI", report.get('ipi_matches', 0))
                m3.metric("Προειδοποιήσεις", len(report['health_warnings']) + len(report['tidal_fallbacks']))

                st.markdown("<br>", unsafe_allow_html=True)

                _, col_down, _ = st.columns([1, 2, 1])
                with col_down:
                    st.download_button(
                        label="⬇️ Λήψη Ολοκληρωμένου Excel",
                        data=buffer.getvalue(),
                        file_name=output_filename,
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        width="stretch",
                        type="primary"
                    )

            with tab_preview:
                if report["filled"]:
                    df_preview = pd.DataFrame(report["filled"])
                    df_preview["contributors"] = df_preview["contributors"].apply(lambda x: ", ".join(x))
                    st.dataframe(
                        df_preview,
                        width="stretch",
                        hide_index=True,
                        column_config={
                            "title": "Τίτλος",
                            "contributors": "Δημιουργοί",
                            "isrc": "ISRC",
                            "source": "Πηγή Credits",
                            "notes": "Σημειώσεις"
                        }
                    )

            with tab_logs:
                if report["health_warnings"]:
                    st.error(f"Βρέθηκαν {len(report['health_warnings'])} προβληματικά ISRC")
                    for title, isrc in report["health_warnings"]:
                        st.write(f"• **{title}** | ISRC: `{isrc}`")
                else:
                    st.success("Κανένα πρόβλημα με τα ISRC!")

                st.divider()

                if report["tidal_fallbacks"]:
                    st.warning(f"Βρέθηκαν {len(report['tidal_fallbacks'])} τραγούδια χωρίς Tidal Credits")
                    with st.expander("Προβολή λίστας", expanded=False):
                        for title in report["tidal_fallbacks"]:
                            st.write(f"• **{title}**")

        except Exception as e:
            st.error(f"Μη αναμενόμενο σφάλμα κατά τη δημιουργία: {e}")
