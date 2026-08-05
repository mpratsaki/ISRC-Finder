import streamlit as st
import base64
import hashlib
import hmac
import time
import requests
import io
import pandas as pd
from pydub import AudioSegment

def identify_snippet(audio_segment, start_ms, host, access_key, access_secret):
    """Κόβει 15 δευτερόλεπτα από το start_ms και το στέλνει στο API."""
    snippet = audio_segment[start_ms : start_ms + 15000]
    
    audio_buffer = io.BytesIO()
    snippet.export(audio_buffer, format="wav")
    audio_buffer.seek(0)
    
    timestamp = str(int(time.time()))
    string_to_sign = '\n'.join(["POST", "/v1/identify", access_key, "audio", "1", timestamp])
    sign = base64.b64encode(hmac.new(
        access_secret.encode('ascii'), 
        string_to_sign.encode('ascii'), 
        digestmod=hashlib.sha1
    ).digest()).decode('ascii')
    
    data = {
        'access_key': access_key,
        'sample_bytes': audio_buffer.getbuffer().nbytes,
        'timestamp': timestamp,
        'signature': sign,
        'data_type': "audio",
        'signature_version': "1"
    }
    
    files = {'sample': ('snippet.wav', audio_buffer, 'audio/wav')}
    response = requests.post(f"https://{host}/v1/identify", files=files, data=data)
    
    try:
        return response.json()
    except ValueError:
        return {}

def page_acrcloud_scanner(token=None):
    st.title("🎵 ACRCloud Auto Scanner")
    st.write("Ανέβασε ένα DJ Mix ή μεγάλο αρχείο ήχου και άσε το εργαλείο να βρει όλα τα κομμάτια, ISRCs και UPCs.")

    # --- ΑΥΤΟΜΑΤΗ ΑΝΑΓΝΩΣΗ ΑΠΟ ΤΑ STREAMLIT SECRETS ---
    # Χρησιμοποιούμε το .get() για να μην κρασάρει αν κάποιο κλειδί λείπει ή έχει γραφτεί λάθος
    default_host = st.secrets.get("ACRCLOUD_HOST", "identify-eu-west-1.acrcloud.com")
    default_key = st.secrets.get("ACRCLOUD_KEY", "")
    default_secret = st.secrets.get("ACRCLOUD_SECRET", "")

    # Το κάναμε expanded=False για να μην πιάνει χώρο στην οθόνη, αφού τα κλειδιά μπαίνουν αυτόματα!
    with st.expander("⚙️ API Credentials (Φορτώθηκαν αυτόματα)", expanded=False):
        col1, col2, col3 = st.columns(3)
        with col1:
            host_input = st.text_input("Host", value=default_host)
        with col2:
            key_input = st.text_input("Access Key", type="password", value=default_key)
        with col3:
            secret_input = st.text_input("Access Secret", type="password", value=default_secret)

    st.markdown("---")

    uploaded_file = st.file_uploader("📂 Επίλεξε αρχείο ήχου", type=['mp3', 'wav', 'm4a', 'flac'])

    if uploaded_file is not None:
        if st.button("🚀 Έναρξη Σάρωσης", type="primary"):
            
            if not key_input or not secret_input:
                st.error("⚠️ Παρακαλώ συμπλήρωσε το Access Key και το Access Secret (ή έλεγξε τα Streamlit Secrets σου)!")
                return
            
            with st.spinner("Φόρτωση ολόκληρου του κομματιού στη μνήμη..."):
                full_audio = AudioSegment.from_file(uploaded_file)
                duration_ms = len(full_audio)
            
            step_ms = 20000 
            valid_timestamps = list(range(0, duration_ms - 15000, step_ms))
            if not valid_timestamps:
                valid_timestamps = [0]
            
            st.success(f"✅ Το αρχείο φορτώθηκε επιτυχώς! Μήκος: {duration_ms // 1000} δευτερόλεπτα. Θα γίνουν {len(valid_timestamps)} σαρώσεις.")
            
            progress_bar = st.progress(0)
            status_text = st.empty()
            
            all_unique_results = {}
            
            for i, start_time in enumerate(valid_timestamps, 1):
                sec = start_time // 1000
                status_text.info(f"⏳ Σάρωση δείγματος από το {sec}ο δευτερόλεπτο... ({i}/{len(valid_timestamps)})")
                
                result = identify_snippet(full_audio, start_time, host_input, key_input, secret_input)
                
                if result.get('status', {}).get('code') == 0:
                    matches = result.get('metadata', {}).get('music', [])
                    for track in matches:
                        if track.get('score', 0) < 50:
                            continue
                            
                        isrc = track.get('external_ids', {}).get('isrc', 'NO_ISRC')
                        artist = track.get('artists', [{'name': 'Άγνωστος Καλλιτέχνης'}])[0]['name']
                        title = track.get('title', 'Άγνωστος Τίτλος')
                        
                        unique_key = isrc if isrc != 'NO_ISRC' else f"{artist} - {title}"
                        
                        if unique_key not in all_unique_results:
                            all_unique_results[unique_key] = track
                
                progress_bar.progress(i / len(valid_timestamps))
                time.sleep(0.5) 
                
            status_text.success(f"🎉 Η σάρωση ολοκληρώθηκε! Βρέθηκαν συνολικά {len(all_unique_results)} μοναδικά κομμάτια.")
            
            if all_unique_results:
                table_data = []
                for track_info in all_unique_results.values():
                    table_data.append({
                        "Artist": track_info.get('artists', [{'name': 'Άγνωστος'}])[0]['name'],
                        "Title": track_info.get('title', 'Άγνωστος Τίτλος'),
                        "Label": track_info.get('label', 'Άγνωστο Label'),
                        "ISRC": track_info.get('external_ids', {}).get('isrc', 'Δεν βρέθηκε'),
                        "UPC": track_info.get('external_ids', {}).get('upc', 'Δεν βρέθηκε'),
                        "Spotify ID": track_info.get('external_metadata', {}).get('spotify', {}).get('track', {}).get('id', 'Δεν βρέθηκε')
                    })
                
                df = pd.DataFrame(table_data)
                st.dataframe(df, use_container_width=True)
                
                csv_buffer = df.to_csv(index=False, encoding="utf-8-sig")
                st.download_button(
                    label="📥 Κατέβασμα ως CSV",
                    data=csv_buffer,
                    file_name="acrcloud_mix_results.csv",
                    mime="text/csv"
                )
            else:
                st.warning("Δεν βρέθηκε καμία αναγνωρίσιμη μουσική στο αρχείο.")
