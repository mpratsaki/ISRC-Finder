"""
tools/page_metadata.py

"Metadata Health" page: scans the private IPI LIST for writers missing an
IPI number or PRO affiliation, falling back to demo data if the live list
cannot be loaded. Includes Phase 4 MusicBrainz Deep Scan.
"""

import time
import pandas as pd
import streamlit as st

from utils.github_fetcher import (
    get_private_ipi_config,
    fetch_private_ipi_list_bytes,
    build_ipi_lookup_from_bytes,
    _scan_ipi_health,
)
from utils.musicbrainz_api import extract_mbid
from utils.coverart_api import fetch_cover_art_url

def page_metadata_health():
    st.title("Metadata Health Dashboard")
    st.caption("Επισκόπηση της ποιότητας του IPI LIST — εντοπίστε writers χωρίς IPI ή PRO.")

    total = None
    problems = None

    # Try to compute REAL metrics from the private IPI LIST; fall back to demo.
    try:
        private_ipi_config = get_private_ipi_config()
        with st.spinner("Ανάλυση IPI LIST..."):
            ipi_file_bytes = fetch_private_ipi_list_bytes(**private_ipi_config)
            ipi_lookup, _ = build_ipi_lookup_from_bytes(ipi_file_bytes)
        total, problems = _scan_ipi_health(ipi_lookup)
        data_is_live = True
    except Exception as e:
        st.info("Χρήση demo δεδομένων (δεν φορτώθηκε το ζωντανό IPI LIST).")
        st.caption(f"Λεπτομέρεια: {e}")
        data_is_live = False
        total = 128
        problems = [
            {"Writer": "Nikos Papadopoulos", "IPI": "—", "PRO": "AEPI", "Πρόβλημα": "Missing IPI"},
            {"Writer": "Maria K.", "IPI": 250123456, "PRO": "—", "Πρόβλημα": "Missing PRO"},
            {"Writer": "John Doe", "IPI": "—", "PRO": "—", "Πρόβλημα": "Missing IPI, Missing PRO"},
        ]

    missing_ipi = sum(1 for p in problems if "Missing IPI" in p["Πρόβλημα"])
    missing_pro = sum(1 for p in problems if "Missing PRO" in p["Πρόβλημα"])

    m1, m2, m3 = st.columns(3)
    m1.metric("Σύνολο Writers στη βάση", total)
    m2.metric("Missing IPIs", missing_ipi, delta=f"-{missing_ipi}" if missing_ipi else "0",
              delta_color="inverse" if missing_ipi else "off")
    m3.metric("Missing PROs", missing_pro, delta=f"-{missing_pro}" if missing_pro else "0",
              delta_color="inverse" if missing_pro else "off")

    st.divider()

    if problems:
        st.warning(f"Βρέθηκαν {len(problems)} writers με ελλιπή metadata — χρειάζονται διόρθωση.")
        st.dataframe(
            pd.DataFrame(problems),
            width="stretch",
            hide_index=True,
            column_config={
                "Writer": st.column_config.TextColumn("Writer"),
                "IPI": st.column_config.TextColumn("IPI"),
                "PRO": st.column_config.TextColumn("PRO"),
                "Πρόβλημα": st.column_config.TextColumn("Πρόβλημα"),
            },
        )
    else:
        st.success("🎉 Όλοι οι writers έχουν πλήρη IPI & PRO metadata!")

    if data_is_live:
        st.caption("Τα παραπάνω προέρχονται από το ζωντανό IPI LIST (private GitHub).")

    # ======================================================================
    # Phase 4: MusicBrainz Deep Check (Opt-in)
    # ======================================================================
    st.divider()
    st.markdown("### 🔬 Deep Check on MusicBrainz (Releases & Cover Art)")
    st.caption(
        "Μαζικός έλεγχος κυκλοφοριών (Release MBIDs) για εντοπισμό ελλείψεων "
        "όπως **Missing Cover Art**. Η διαδικασία είναι αυστηρά opt-in λόγω "
        "των rate limits του MusicBrainz (σχεδόν 1 δευτερόλεπτο ανά κλήση)."
    )

    mbids_input = st.text_area(
        "Εισάγετε Release MBIDs (ένα ανά γραμμή):",
        placeholder="π.χ.\nxxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx\nyyyyyyyy-yyyy-yyyy-yyyy-yyyyyyyyyyyy",
        height=150
    )

    if mbids_input:
        raw_mbids = [m.strip() for m in mbids_input.split("\n") if m.strip()]
        clean_mbids = [extract_mbid(m) for m in raw_mbids if extract_mbid(m)]
        
        if clean_mbids:
            st.warning(f"Αυτή η ενέργεια θα κάνει {len(clean_mbids)} κλήσεις στο Cover Art Archive (εκτιμώμενος χρόνος: ~{len(clean_mbids)} δευτερόλεπτα). Συνέχεια;")
            
            if st.button("Εκκίνηση Deep Check", type="primary"):
                st.markdown("#### Live Activity")
                live_status = st.empty()
                progress_bar = st.progress(0.0)
                
                results = []
                total_items = len(clean_mbids)

                for i, mbid in enumerate(clean_mbids, start=1):
                    # Progress Update
                    progress_value = i / max(total_items, 1)
                    progress_bar.progress(progress_value)
                    live_status.markdown(
                        f"""
                        <div class="live-activity-box">
                            <span style="color:#aaa; font-size:14px;">Έλεγχος {i} από {total_items}</span><br>
                            <strong style="font-size:18px;">📀 Release: {mbid}</strong>
                        </div>
                        """, unsafe_allow_html=True
                    )
                    
                    # API Call
                    has_cover = fetch_cover_art_url(mbid, "release") is not None
                    
                    results.append({
                        "Release MBID": mbid,
                        "Cover Art": "✅ ΟΚ" if has_cover else "❌ Λείπει",
                    })
                    
                    # Delay to respect APIs
                    time.sleep(1)

                live_status.empty()
                progress_bar.empty()
                st.success("Το Deep Check ολοκληρώθηκε!")
                
                df_results = pd.DataFrame(results)
                missing_count = sum(1 for r in results if r["Cover Art"] == "❌ Λείπει")
                
                if missing_count > 0:
                    st.error(f"Βρέθηκαν {missing_count} κυκλοφορίες χωρίς εξώφυλλο (Cover Art).")
                else:
                    st.success("Όλες οι ελεγμένες κυκλοφορίες διαθέτουν εξώφυλλο.")
                    
                st.dataframe(df_results, width="stretch", hide_index=True)
