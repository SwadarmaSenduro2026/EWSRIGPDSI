# EWSRIGPDSI – Streamlit EWS Drilling (versi stabilisasi)

Aplikasi demonstrasi pemantauan EWS pengeboran yang tetap mempertahankan **Fuzzy-PID, Kabin Driller, dashboard EWS, alarm, ACK, WhatsApp, serta login admin/viewer**. Dataset NEAC-001 asli tetap disertakan. Ambang dan simulasi **bukan setpoint operasi rig**, dan aplikasi ini **bukan SCADA keselamatan industri**.

## Deploy kembali ke GitHub dan Streamlit

1. Ganti file `dashboard_ews_scada_neac001_final.py` pada repository GitHub menggunakan versi dari paket ini. Pertahankan nama file supaya pengaturan *main file path* pada Streamlit tidak perlu diubah.
2. Biarkan dataset `NEAC-001_MLU_Drilling_Ascii_500m_1815baris.xlsx` pada direktori yang sama dengan file `.py`. Tetap gunakan `requirements.txt` yang disertakan.
3. Di konfigurasi aplikasi Streamlit, pastikan **main file path** adalah `dashboard_ews_scada_neac001_final.py` pada root repository. Setelah commit, lakukan deploy/reboot aplikasi.
4. Login membutuhkan **Streamlit Secrets** yang berisi paling tidak satu akun `admin` dan/atau `viewer`. Password tidak disimpan dalam source code. Lihat `docs/secrets.toml.example` dan buat salt/hash dengan perintah di bawah.
5. Fitur WhatsApp membutuhkan token dan target Fonnte di Streamlit Secrets; jangan masukkan token ke GitHub. Status pengiriman dapat dicek di log audit.

```bash
python tools/generate_hash.py --user admin --role admin
python tools/generate_hash.py --user viewer --role viewer
```

Tempatkan blok `[users.admin]` dan `[users.viewer]` dari kedua keluaran itu ke pengaturan Secrets aplikasi Streamlit, bukan ke GitHub. Untuk percobaan lokal saja, gunakan `.streamlit/secrets.toml` yang masuk `.gitignore`; jangan pernah commit berkas ini.

## Dataset dan fallback

- Dataset **NEAC-001** tersedia dan teruji terbaca; setelah dipotong pada kedalaman 200 m menghasilkan **612 baris** (kedalaman 47–200 m).
- Dataset sintetis dan auto pilot yang dulu dirujuk **tidak ikut disertakan pada ZIP asli**. Skenario terkait tetap tersedia, tetapi aplikasi memperingatkan bahwa sumber cadangan digunakan bila file tidak ada. Jangan menafsirkan fallback sebagai dataset sintetis asli.
- File dibaca relatif ke lokasi script, bukan direktori kerja (*working directory*). Cache pembacaan data dibedakan dengan waktu modifikasi file.
- Dataset yang kosong, kehilangan kolom wajib, atau tidak memiliki baris valid ditangani dengan pesan yang dapat dipahami; file opsional yang rusak tidak otomatis menghentikan skenario lain.

## Pengujian

Di komputer yang mempunyai akses instalasi dependensi:

```bash
pip install -r requirements.txt
streamlit run dashboard_ews_scada_neac001_final.py
# Untuk pengujian regresi offline (pytest tidak diperlukan untuk deployment):
pip install pytest
pytest -q tests/test_stabilization.py
```

**Hasil lingkungan audit:** sintaks berhasil dikompilasi, 22 pengujian regresi lulus menggunakan UI Streamlit simulasi (*stub*) dan pembaca Excel sesungguhnya. Pustaka `streamlit` serta `streamlit-autorefresh` tidak tersedia di lingkungan audit dan tidak dapat dipasang karena tidak ada akses jaringan; oleh karena itu, tampilan browser nyata, suara/getar, pengiriman Fonnte, dan sinkronisasi antar-instance server tidak diklaim lulus pengujian. Lihat `docs/AUDIT_STABILISASI.md`.

## Batasan sinkronisasi

`st.cache_resource` dan `threading.RLock` menyediakan state bersama antarsesi browser selama semua sesi terhubung ke **proses Python Streamlit yang sama**. Tidak ada basis data persistensi: reboot server mereset simulasi/ACK/log, dan banyak replica server **tidak** saling menyinkronkan state. Perlu penyimpanan bersama (mis. database/Redis) bila nanti deploy memakai lebih dari satu instance; fitur ini **tidak ditambahkan** pada tahap stabilisasi untuk mempertahankan arsitektur/desain.
