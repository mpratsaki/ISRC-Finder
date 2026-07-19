"""
tools/page_metadata.py

"Metadata Health" page: scans the private IPI LIST for writers missing an
IPI number or PRO affiliation, falling back to demo data if the live list
cannot be loaded.
"""

import pandas as pd
import streamlit as st

from utils.github_fetcher import (
    get_private_ipi_config,
    fetch_private_ipi_list_bytes,
    build_ipi_lookup_from_bytes,
    _scan_ipi_health,
)


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
