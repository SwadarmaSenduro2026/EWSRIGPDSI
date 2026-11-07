import copy
import hashlib
import hmac
import html
import io
import json
import math
import threading
import time
from datetime import datetime, timedelta, timezone

from matplotlib.figure import Figure  # tanpa pyplot: aman untuk banyak perangkat sekaligus
import numpy as np
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from streamlit_autorefresh import st_autorefresh

# ==========================================================
# EWS PEMBORAN - PROTOTIPE SIMULASI
# Data dan ambang hanya untuk demonstrasi, bukan setpoint rig.
#
# FITUR:
# - Sinkron multi-device (parameter, posisi simulasi, riwayat).
# - Peran pengguna: admin (ubah pengaturan) dan viewer (hanya lihat).
# - Acknowledge alarm (ACK) yang berlaku untuk semua device.
# - Deteksi data terlambat dan sensor tidak valid.
# - Penahan alarm berulang: debounce + hysteresis.
# - Alarm laju perubahan (rate of change).
# - Notifikasi WhatsApp (gateway Fonnte).
# - Log audit.
# ==========================================================

st.set_page_config(
    page_title="EWS Telemetry SCADA - Simulasi",
    layout="wide",
    initial_sidebar_state="auto",
)

WIB = timezone(timedelta(hours=7))


def call_wide(function, *args, **kwargs):
    """Panggil elemen Streamlit selebar kolom; kompatibel lintas versi Streamlit."""
    for extra in ({"use_container_width": True}, {"width": "stretch"}, {}):
        try:
            return function(*args, **kwargs, **extra)
        except TypeError:
            continue
    return function(*args, **kwargs)

# ==========================================================
# KONSTANTA
# ==========================================================
DATA_FILE = "data_pemboran_sintetis_10_menit_anomali.xlsx"
AUTOPILOT_FILE = "data_autopilot_sintetis_600_baris.xlsx"
FIELD_FILE = "NEAC-001_MLU_Drilling_Ascii_500m_1815baris.xlsx"
# Data asli 1815 baris (47-500 m) dipangkas ke kedalaman <= 200 m (sekitar 600 baris)
# agar playback tidak terlalu panjang dan tidak terlalu pendek.
FIELD_MAX_DEPTH_M = 200.0

# Lima kanal monitoring yang tersedia pada dataset.
PARAMETERS = ["ROP", "WOB", "Torque", "StandpipePressure", "RPM"]

UNITS = {
    "ROP": "m/h",
    "WOB": "kN",
    "Torque": "kN.m",
    "StandpipePressure": "psi",
    "RPM": "rpm",
}

# Ambang rendah khusus demonstrasi alarm (nilai awal/default).
# JANGAN digunakan sebagai setpoint operasi nyata.
DEFAULT_THRESHOLDS = {
    "ROP": {"yellow": 5.0, "red": 8.0, "magenta": 10.0},
    "WOB": {"yellow": 20.0, "red": 30.0, "magenta": 40.0},
    "Torque": {"yellow": 2.0, "red": 3.0, "magenta": 4.0},
    "StandpipePressure": {
        "yellow": 3000.0, "red": 4000.0, "magenta": 5000.0
    },
    "RPM": {"yellow": 20.0, "red": 30.0, "magenta": 40.0},
}

THRESHOLD_STEP = {
    "ROP": 0.5,
    "WOB": 1.0,
    "Torque": 0.1,
    "StandpipePressure": 50.0,
    "RPM": 1.0,
}

LEVEL_KEYS = [("yellow", "Kuning"), ("red", "Merah"), ("magenta", "Magenta")]

# Ambang LAJU PERUBAHAN (satuan per MENIT, nilai mutlak naik atau turun).
# Hanya demonstrasi; sesuaikan dengan karakter data rig yang sebenarnya.
ROC_SUFFIX = "~laju"
DEFAULT_ROC_THRESHOLDS = {
    "ROP": {"yellow": 3.0, "red": 6.0},
    "WOB": {"yellow": 10.0, "red": 20.0},
    "Torque": {"yellow": 1.0, "red": 2.0},
    "StandpipePressure": {"yellow": 1000.0, "red": 2000.0},
    "RPM": {"yellow": 8.0, "red": 15.0},
}
ROC_STEP = {
    "ROP": 0.5,
    "WOB": 1.0,
    "Torque": 0.1,
    "StandpipePressure": 50.0,
    "RPM": 1.0,
}
ROC_LEVEL_KEYS = [("yellow", "Kuning"), ("red", "Merah")]
ROC_WINDOW_OPTIONS = [5, 10, 20, 30]

# Rentang nilai masuk akal; di luar rentang (atau kosong) dianggap sensor rusak.
PLAUSIBLE_RANGE = {
    "ROP": (0.0, 500.0),
    "WOB": (0.0, 500.0),
    "Torque": (0.0, 100.0),
    "StandpipePressure": (0.0, 15000.0),
    "RPM": (0.0, 300.0),
}

SCENARIOS = [
    "Skenario terburuk sintetis",
    "Dataset sintetis asli",
    "Dataset auto pilot (600 baris)",
    "Data lapangan NEAC-001 (47-200 m, sekitar 600 baris)",
]
INTERVAL_OPTIONS = [1, 2, 3, 5, 10]
BATCH_OPTIONS = [1, 5, 10, 20]
STALE_OPTIONS = [5, 10, 15, 30, 60]
NO_FAULTY_SENSOR = "Tidak ada"


# Ambang dan setpoint awal per skenario. Skenario sintetis memakai ambang rendah
# demonstrasi; data lapangan NEAC-001 memakai ambang berbasis persentil data itu
# sendiri (kuning ~P90, merah ~P97, magenta ~maks) untuk analisis/playback.
# Semuanya BUKAN setpoint operasi nyata dan dapat diubah admin di sidebar.
FIELD_THRESHOLDS = {
    "ROP": {"yellow": 39.0, "red": 40.5, "magenta": 42.0},
    "WOB": {"yellow": 34.0, "red": 42.0, "magenta": 48.0},
    "Torque": {"yellow": 1.20, "red": 1.35, "magenta": 1.45},
    "StandpipePressure": {"yellow": 1010.0, "red": 1040.0, "magenta": 1070.0},
    "RPM": {"yellow": 62.0, "red": 64.0, "magenta": 66.0},
}
FIELD_ROC_THRESHOLDS = {
    "ROP": {"yellow": 1.8, "red": 2.8},
    "WOB": {"yellow": 1.8, "red": 2.4},
    "Torque": {"yellow": 0.03, "red": 0.08},
    "StandpipePressure": {"yellow": 12.0, "red": 20.0},
    "RPM": {"yellow": 1.0, "red": 2.0},
}
_SYNTH_PRESET = {
    "thresholds": DEFAULT_THRESHOLDS,
    "roc_thresholds": DEFAULT_ROC_THRESHOLDS,
    "ap_wob_sp": 16.0, "ap_rpm_sp": 15.0, "ap_rop_target": 4.5,
}
SCENARIO_PRESETS = {
    SCENARIOS[0]: _SYNTH_PRESET,
    SCENARIOS[1]: _SYNTH_PRESET,
    SCENARIOS[2]: _SYNTH_PRESET,
    SCENARIOS[3]: {
        "thresholds": FIELD_THRESHOLDS,
        "roc_thresholds": FIELD_ROC_THRESHOLDS,
        "ap_wob_sp": 22.0, "ap_rpm_sp": 60.0, "ap_rop_target": 35.0,
    },
}
DEFAULT_SCENARIO = SCENARIOS[3]

DEFAULT_PARAMS = {
    "scenario": DEFAULT_SCENARIO,
    "interval": 1,
    "batch_size": 1,
    "auto": True,
    "alarm_enabled": True,
    "repeat_alarm": True,
    "loop_data": True,
    "thresholds": copy.deepcopy(SCENARIO_PRESETS[DEFAULT_SCENARIO]["thresholds"]),
    # laju perubahan
    "roc_enabled": True,
    "roc_window": 10,
    "roc_thresholds": copy.deepcopy(SCENARIO_PRESETS[DEFAULT_SCENARIO]["roc_thresholds"]),
    # penahan alarm
    "debounce_samples": 3,
    "hysteresis_pct": 5.0,
    # gangguan data
    "stale_after": 15,
    "test_outage": False,
    "test_faulty_sensor": NO_FAULTY_SENSOR,
    # WhatsApp
    "wa_enabled": False,
    "wa_min_level": 2,
    # auto pilot Fuzzy-PID
    "autopilot": False,
    "ap_wob_sp": SCENARIO_PRESETS[DEFAULT_SCENARIO]["ap_wob_sp"],
    "ap_rpm_sp": SCENARIO_PRESETS[DEFAULT_SCENARIO]["ap_rpm_sp"],
    "ap_rop_target": SCENARIO_PRESETS[DEFAULT_SCENARIO]["ap_rop_target"],
    "ap_rise_med_pct": 15.0,
    "ap_rise_high_pct": 30.0,
}

PARAM_LABELS = {
    "scenario": "Skenario",
    "interval": "Interval pembaruan (detik)",
    "batch_size": "Baris per pembaruan",
    "auto": "Simulasi otomatis",
    "alarm_enabled": "Alarm suara (semua device)",
    "repeat_alarm": "Ulangi alarm magenta",
    "loop_data": "Ulangi simulasi dari awal",
    "roc_enabled": "Alarm laju perubahan",
    "roc_window": "Jendela laju perubahan (pembacaan)",
    "debounce_samples": "Debounce (pembacaan berturut-turut)",
    "hysteresis_pct": "Hysteresis (%)",
    "stale_after": "Batas data terlambat (detik)",
    "test_outage": "Uji: putuskan aliran data",
    "test_faulty_sensor": "Uji: matikan sensor",
    "wa_enabled": "Notifikasi WhatsApp",
    "wa_min_level": "Level minimum WhatsApp",
    "autopilot": "Mode auto pilot (Fuzzy-PID)",
    "ap_wob_sp": "Auto pilot: setpoint WOB (kN)",
    "ap_rpm_sp": "Auto pilot: setpoint RPM",
    "ap_rop_target": "Auto pilot: target ROP (m/h)",
    "ap_rise_med_pct": "Auto pilot: kenaikan sedang (%)",
    "ap_rise_high_pct": "Auto pilot: kenaikan tinggi (%)",
}

MAX_CATCHUP_STEPS = 30
EVENTS_MAX = 500
AUDIT_MAX = 2000

# Peran yang boleh melakukan ACK alarm. Ubah menjadi {"admin"} jika ACK
# hanya boleh dilakukan admin.
ACK_ROLES = {"admin", "viewer"}

# WhatsApp (gateway Fonnte)
WA_API_URL = "https://api.fonnte.com/send"
WA_COOLDOWN_SECONDS = 60
WA_PREFIX = "[SIMULASI]"  # hapus prefiks ini jika sudah memakai data rig nyata

LEVELS = {
    0: {"name": "HIJAU", "color": "#16803C", "description": "Normal"},
    1: {"name": "KUNING", "color": "#C58B00", "description": "Waspada"},
    2: {"name": "MERAH", "color": "#D62828", "description": "Bahaya simulasi"},
    3: {"name": "MAGENTA", "color": "#C000A5", "description": "Kritis simulasi"},
}

# Pola suara per level; level 4 = gangguan data/sensor.
ALARM_PATTERNS = {
    1: [(880, 180), (0, 120), (880, 180)],
    2: [(1100, 200), (0, 100), (1100, 200), (0, 100), (1100, 400)],
    3: [
        (1400, 250), (0, 80), (1400, 250), (0, 80),
        (1400, 250), (0, 80), (1400, 500),
    ],
    4: [(600, 300), (0, 150), (600, 300), (0, 150), (600, 300)],
}

# Pola getar (ms) untuk HP/tablet Android. iPhone/iPad tidak mendukung getar.
VIBRATION_PATTERNS = {
    1: [200, 100, 200],
    2: [300, 100, 300, 100, 500],
    3: [500, 100, 500, 100, 500, 100, 800],
    4: [400, 200, 400, 200, 400],
}

ALARM_FRESH_SECONDS = 8  # event alarm/ACK lebih lama dari ini tidak diputar ulang

STATE_VERSION = "v6-neac001"  # naikkan jika struktur state bersama berubah



# ==========================================================
# TEMA (DARK / LIGHT): mengikuti mode aktif Streamlit
# ==========================================================
PALETTES = {
    "dark": {
        "bg": "#0F151C", "surface": "#161F29", "surface2": "#1D2833",
        "border": "#2E3C4A", "text": "#E9EEF3", "muted": "#9AAEC0",
        "accent": "#4DB2D8", "chart_grid": "#33424F",
        "lv0": "#3DBE72", "lv1": "#E5B124", "lv2": "#F0524F",
        "lv3": "#D84BC0", "lverr": "#93A1AE",
    },
    "light": {
        "bg": "#F2F4F7", "surface": "#FFFFFF", "surface2": "#EAEEF3",
        "border": "#C9D2DC", "text": "#14202B", "muted": "#546577",
        "accent": "#0A6C99", "chart_grid": "#D5DCE4",
        "lv0": "#17803F", "lv1": "#A87400", "lv2": "#C62828",
        "lv3": "#A8168B", "lverr": "#5B6772",
    },
}


def detect_theme():
    """Mode tema yang sedang aktif di browser: 'dark', 'light', atau None."""
    try:
        theme = st.context.theme
        for attr in ("type", "base"):
            value = getattr(theme, attr, None)
            if value in ("dark", "light"):
                return value
    except Exception:
        pass
    return None


def _palette_vars(p):
    return (
        f"--bg:{p['bg']};--surface:{p['surface']};--surface2:{p['surface2']};"
        f"--border:{p['border']};--text:{p['text']};--muted:{p['muted']};"
        f"--accent:{p['accent']};--lv0:{p['lv0']};--lv1:{p['lv1']};"
        f"--lv2:{p['lv2']};--lv3:{p['lv3']};--lverr:{p['lverr']};"
    )


BASE_CSS = """
.stApp{background:var(--bg);color:var(--text)}
[data-testid="stHeader"]{background:var(--bg)}
[data-testid="stSidebar"]{background:var(--surface);border-right:1px solid var(--border)}
[data-testid="stMainBlockContainer"],.block-container{padding-top:2.4rem;max-width:1500px}
h1,h2,h3,h4{color:var(--text);letter-spacing:.01em}
[data-testid="stCaptionContainer"]{color:var(--muted)}
div.stButton>button,div.stDownloadButton>button{border-radius:4px;border:1px solid var(--border);font-weight:600}
[data-testid="stExpander"]{border:1px solid var(--border);border-radius:6px}

.lv0{--c:var(--lv0);--pn:#16803C}
.lv1{--c:var(--lv1);--pn:#E0A800}
.lv2{--c:var(--lv2);--pn:#C62828}
.lv3{--c:var(--lv3);--pn:#A8168B}
.lverr{--c:var(--lverr);--pn:#5B6772}

.ews-header{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;padding:14px 18px;margin-bottom:12px;background:var(--surface);border:1px solid var(--border);border-left:5px solid var(--accent);border-radius:6px}
.ews-title{font-size:22px;font-weight:800;letter-spacing:.06em;text-transform:uppercase;color:var(--text);line-height:1.2}
.ews-sub{font-size:12px;color:var(--muted);letter-spacing:.08em;text-transform:uppercase;margin-top:3px}
.tag{display:inline-block;font-size:11px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;padding:4px 10px;border:1px solid var(--border);border-radius:3px;color:var(--muted);background:var(--surface2);margin-left:6px}
.tag.sim{color:var(--lv1);border-color:var(--lv1)}

.notice{padding:9px 14px;margin:0 0 10px 0;border:1px solid var(--border);border-left:4px solid var(--accent);background:var(--surface);border-radius:4px;font-size:13px;color:var(--text)}
.notice.warn{border-left-color:var(--lv1)}
.notice.err{border-left-color:var(--lv2)}

.status-panel{padding:20px 24px 16px;border-radius:6px;margin:0 0 12px 0;color:#fff;background:var(--pn)}
.status-panel.lv1{color:#1b1400}
.status-kicker{font-size:12px;font-weight:700;letter-spacing:.14em;text-transform:uppercase;opacity:.92}
.status-title{font-size:40px;font-weight:800;letter-spacing:.03em;line-height:1.15;margin:2px 0 4px}
.status-msg{font-size:14px}
.status-meta{margin-top:12px;padding-top:10px;border-top:1px solid rgba(255,255,255,.4);display:flex;flex-wrap:wrap;gap:4px 22px;font-size:12px;letter-spacing:.03em}
.status-panel.lv1 .status-meta{border-top-color:rgba(0,0,0,.25)}
.status-progress{height:5px;background:rgba(255,255,255,.3);border-radius:3px;margin-top:10px}
.status-progress>div{height:100%;background:#fff;border-radius:3px}
.status-panel.lv1 .status-progress{background:rgba(0,0,0,.18)}
.status-panel.lv1 .status-progress>div{background:#1b1400}

.section-title,.side-title{font-weight:800;letter-spacing:.14em;text-transform:uppercase;color:var(--muted);border-bottom:1px solid var(--border)}
.section-title{font-size:12px;margin:22px 0 10px;padding-bottom:6px}
.side-title{font-size:11px;margin:4px 0 10px;padding-bottom:6px}

.grid-legend{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:6px}
.legend-item{background:var(--surface);border:1px solid var(--border);border-left:5px solid var(--c);border-radius:4px;padding:8px 12px}
.legend-item b{display:block;font-size:12px;letter-spacing:.08em;color:var(--text)}
.legend-item span{font-size:12px;color:var(--muted)}

.grid-cards{display:grid;grid-template-columns:repeat(5,1fr);gap:10px}
.grid-cards.four{grid-template-columns:repeat(4,1fr)}
.card{background:var(--surface);border:1px solid var(--border);border-top:4px solid var(--c);border-radius:4px;padding:12px 14px;min-width:0}
.card-label{font-size:11px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
.card-value{font-size:30px;font-weight:800;line-height:1.15;margin:4px 0 6px;color:var(--text);font-variant-numeric:tabular-nums}
.card-unit{font-size:13px;font-weight:600;color:var(--muted);margin-left:4px}
.grid-cards + .notice,.grid-cards + div + .notice{margin-top:10px}
.card-rate{font-size:12px;color:var(--muted);margin-top:6px;display:flex;align-items:center;gap:6px;flex-wrap:wrap}

.chip{display:inline-flex;align-items:center;gap:6px;font-size:11px;font-weight:800;letter-spacing:.08em;text-transform:uppercase;color:var(--c)}
.chip::before{content:"";width:9px;height:9px;border-radius:50%;background:var(--c);display:inline-block}

.tbl-wrap{max-height:340px;overflow:auto;border:1px solid var(--border);border-radius:4px;background:var(--surface);margin-bottom:8px}
.ews-table{width:100%;border-collapse:collapse;font-size:13px}
.ews-table th{position:sticky;top:0;background:var(--surface2);color:var(--muted);text-align:left;font-size:11px;letter-spacing:.1em;text-transform:uppercase;padding:8px 12px;border-bottom:1px solid var(--border)}
.ews-table td{padding:7px 12px;border-bottom:1px solid var(--border);color:var(--text);vertical-align:top}
.ews-table tr:nth-child(even) td{background:var(--surface2)}
.ews-table td:first-child{white-space:nowrap}

.ack-banner{padding:11px 16px;border-radius:4px;margin:0 0 8px;font-weight:700;font-size:14px;letter-spacing:.03em;color:#fff}
.ack-pending{background:#B3261E;animation:ewsblink 1.2s ease-in-out infinite}
.ack-done{background:var(--surface);border:1px solid var(--lv0);border-left:5px solid var(--lv0);color:var(--text);font-weight:600}
@keyframes ewsblink{50%{opacity:.6}}
.st-key-btn_ack button{background:#B3261E !important;border:0 !important;color:#fff !important;min-height:48px;font-weight:800;letter-spacing:.08em;text-transform:uppercase}
.st-key-btn_ack button p{color:#fff !important;font-weight:800}

.login-title{text-align:center;font-size:26px;font-weight:800;letter-spacing:.08em;text-transform:uppercase;margin:6vh 0 2px;color:var(--text)}
.login-sub{text-align:center;font-size:12px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted);margin-bottom:22px}

@media (max-width:1000px){.grid-cards{grid-template-columns:repeat(3,1fr)}.grid-cards.four{grid-template-columns:repeat(2,1fr)}}
@media (max-width:640px){
  [data-testid="stMainBlockContainer"],.block-container{padding:3rem .7rem 2rem !important}
  .ews-header{padding:10px 12px}
  .ews-title{font-size:17px}
  .grid-legend{grid-template-columns:repeat(2,1fr)}
  .grid-cards,.grid-cards.four{grid-template-columns:repeat(2,1fr);gap:8px}
  .card{padding:10px}
  .card-value{font-size:22px}
  .status-panel{padding:14px 14px 12px}
  .status-title{font-size:28px}
  .status-meta{font-size:11px}
}
"""


CABIN_CSS = """
.view-note{font-size:12px;color:var(--muted);margin:2px 0 8px}
.cabin-bar{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin-bottom:10px}
.cabin-stat{background:var(--surface);border:1px solid var(--border);border-radius:4px;padding:8px 12px;min-width:0}
.cabin-stat .k{font-size:10px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:var(--muted)}
.cabin-stat .v{font-size:24px;font-weight:800;line-height:1.2;font-variant-numeric:tabular-nums;color:var(--text)}
.cabin-stat .u{font-size:12px;font-weight:600;color:var(--muted);margin-left:4px}
.cabin-stat .bar{height:8px;background:var(--surface2);border-radius:4px;margin-top:6px;overflow:hidden}
.cabin-stat .bar>div{height:100%;background:var(--accent)}
.cabin-gauges{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin-bottom:10px}
.gauge{background:var(--surface);border:1px solid var(--border);border-top:4px solid var(--c);border-radius:4px;padding:8px 10px 10px;text-align:center;min-width:0}
.gauge .g-label{font-size:11px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
.gauge svg{width:100%;height:auto;display:block;margin:2px 0}
.gauge .g-value{font-size:28px;font-weight:800;line-height:1.1;font-variant-numeric:tabular-nums;color:var(--text)}
.gauge .g-unit{font-size:12px;font-weight:600;color:var(--muted);margin-left:4px}
.gauge .g-sub{font-size:11px;color:var(--muted);margin-top:4px}
.annun{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin-bottom:10px}
.lamp{padding:10px 6px;border-radius:4px;text-align:center;font-size:11px;font-weight:800;letter-spacing:.08em;text-transform:uppercase;border:1px solid var(--border);background:var(--surface2);color:var(--muted);min-width:0}
.lamp.ok{border-color:var(--lv0);color:var(--lv0);background:var(--surface)}
.lamp.on{background:var(--c);border-color:var(--c);color:#fff}
.lamp.on.lv1{color:#1b1400}
.lamp.blink{animation:ewsblink 1.2s ease-in-out infinite}
.lamp small{display:block;font-size:10px;font-weight:600;letter-spacing:.04em;text-transform:none;opacity:.9;margin-top:2px}
.strip-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin-bottom:10px}
.strip{background:var(--surface);border:1px solid var(--border);border-radius:4px;padding:8px 10px;min-width:0}
.strip .s-head{display:flex;justify-content:space-between;align-items:baseline;gap:6px}
.strip .s-name{font-size:11px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
.strip .s-val{font-size:14px;font-weight:800;color:var(--text);font-variant-numeric:tabular-nums}
.strip svg{width:100%;height:72px;display:block;margin-top:4px}
.ap-panel{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:10px}
.legend-line{display:flex;flex-wrap:wrap;gap:6px 16px;font-size:11px;color:var(--muted);margin:2px 0 8px}
.legend-line i{display:inline-block;width:18px;height:3px;vertical-align:middle;margin-right:5px;border-radius:2px}
@media (max-width:1000px){
  .cabin-bar,.cabin-gauges,.annun,.strip-grid{grid-template-columns:repeat(3,1fr)}
  .ap-panel{grid-template-columns:repeat(2,1fr)}
}
@media (max-width:640px){
  .cabin-bar,.cabin-gauges,.annun,.strip-grid,.ap-panel{grid-template-columns:repeat(2,1fr);gap:8px}
  .cabin-stat .v{font-size:19px}
  .gauge .g-value{font-size:22px}
}
.stElementContainer:has(> [data-testid="stVerticalBlock"]:empty){display:none}
"""


def build_css(mode):
    """CSS tema. Jika mode tidak terdeteksi, ikuti pengaturan sistem (OS)."""
    if mode in PALETTES:
        root = ":root{" + _palette_vars(PALETTES[mode]) + "}"
    else:
        root = (
            ":root{" + _palette_vars(PALETTES["dark"]) + "}"
            "@media (prefers-color-scheme: light){:root{"
            + _palette_vars(PALETTES["light"]) + "}}"
        )
    return "<style>" + root + BASE_CSS + CABIN_CSS + "</style>"


THEME = detect_theme()
PAL = PALETTES.get(THEME, PALETTES["dark"])

# ==========================================================
# PENYIMPANAN BERSAMA (dipakai semua pengunjung / device)
# ==========================================================
def fresh_state():
    now = time.time()
    return {
        "index": 0,
        "previous_level": -1,
        "last_alarm_level": -1,
        "wa_last_level": -1,
        "events": [],
        "last_advance": now,
        "last_data_time": now,
        "signal_levels": None,   # None = hitung ulang tanpa debounce
        "up_pending": {},
        "rates": {},
        "bad_sensors": [],
        "fault_keys": set(),
        "ack_required": False,
        "ack": None,
        "alarm": None,
        "wa_last": {},
        "ap": None,      # keadaan pengendali auto pilot
        "n": 1,          # jumlah baris sumber data saat ini
    }


@st.cache_resource
def get_shared(version):
    return {
        "lock": threading.RLock(),
        "params": copy.deepcopy(DEFAULT_PARAMS),
        "state": fresh_state(),
        "audit": [],
        "updated_at": None,
        "updated_by": "",
    }


shared = get_shared(STATE_VERSION)


def audit_log(user, action, detail=""):
    """Catat aksi ke log audit (aman dipanggil dari thread mana pun)."""
    with shared["lock"]:
        shared["audit"].insert(0, {
            "Waktu": datetime.now(WIB).strftime("%Y-%m-%d %H:%M:%S"),
            "Pengguna": str(user)[:40],
            "Aksi": action,
            "Detail": str(detail)[:300],
        })
        del shared["audit"][AUDIT_MAX:]


def session_user():
    return st.session_state.get("authenticated_user", "user")


def session_role():
    return st.session_state.get("role", "viewer")


# ==========================================================
# LOGIN + PERAN
# ==========================================================
def verify_password(password, salt_hex, expected_hash_hex):
    try:
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), 310_000
        )
        return hmac.compare_digest(actual, bytes.fromhex(expected_hash_hex))
    except (ValueError, TypeError):
        return False


def load_users():
    """
    Baca pengguna dari secrets.
    Format baru : [users.NAMA] role, password_salt, password_hash
    Format lama : [auth] username, password_salt, password_hash (jadi admin)
    """
    users = {}
    try:
        raw = st.secrets["users"]
        for name in raw:
            cfg = raw[name]
            role = str(cfg.get("role", "viewer")).strip().lower()
            users[str(name)] = {
                "salt": str(cfg["password_salt"]),
                "hash": str(cfg["password_hash"]),
                "role": role if role in ("admin", "viewer") else "viewer",
            }
    except Exception:
        pass
    try:
        auth = st.secrets["auth"]
        role = str(auth.get("role", "admin")).strip().lower()
        users.setdefault(str(auth["username"]), {
            "salt": str(auth["password_salt"]),
            "hash": str(auth["password_hash"]),
            "role": role if role in ("admin", "viewer") else "admin",
        })
    except Exception:
        pass
    return users


def login_screen():
    st.markdown(build_css(THEME), unsafe_allow_html=True)
    left, center, right = st.columns([1, 1.1, 1])
    with center:
        st.markdown(
            '<div class="login-title">EWS Telemetry</div>'
            '<div class="login-sub">Drilling Monitoring System</div>',
            unsafe_allow_html=True,
        )
        st.subheader("Login pengguna")
        st.caption("Masukkan username dan password yang telah dikonfigurasi.")
        with st.form("login_form"):
            username = st.text_input("Username", autocomplete="username")
            password = st.text_input("Password", type="password", autocomplete="current-password")
            submitted = call_wide(st.form_submit_button, "MASUK KE DASHBOARD")
        if submitted:
            username = username.strip()
            users = load_users()
            if not users:
                st.error("Konfigurasi login belum tersedia. Ikuti petunjuk file pendamping.")
            else:
                user = users.get(username)
                if user:
                    ok = verify_password(password, user["salt"], user["hash"])
                else:
                    verify_password(password, "00" * 16, "00" * 32)  # samakan waktu proses
                    ok = False
                if ok:
                    st.session_state["authenticated"] = True
                    st.session_state["authenticated_user"] = username
                    st.session_state["role"] = user["role"]
                    audit_log(username, "LOGIN", f"peran: {user['role']}")
                    st.rerun()
                else:
                    audit_log(username or "-", "LOGIN GAGAL", "username atau password salah")
                    st.error("Username atau password tidak sesuai.")
        st.caption("Prototipe | Data sintetis | Bukan sistem operasi rig")


if not st.session_state.get("authenticated", False):
    login_screen()
    st.stop()


# ==========================================================
# ALARM SUARA + GETAR DI BROWSER (Web Audio + navigator.vibrate)
# ==========================================================
ALARM_LIB_JS = """
(function(){
  var P = window.parent;
  if (!P.ewsAlarm) {
    var A = P.ewsAlarm = {ctx:null, enabled:false, lastId:null, lastStopId:null,
                          busyUntil:0, nodes:[],
                          patterns:__PATTERNS__, vibrations:__VIBRATIONS__};
    A.unlock = function(){
      try {
        var AC = P.AudioContext || P.webkitAudioContext;
        if (!A.ctx) A.ctx = new AC();
        if (A.ctx.state !== 'running') A.ctx.resume();
        var o = A.ctx.createOscillator(), g = A.ctx.createGain();
        g.gain.value = 0.0001; o.connect(g); g.connect(A.ctx.destination);
        o.start(); o.stop(A.ctx.currentTime + 0.05);
      } catch(e) {}
    };
    A.beepPattern = function(pattern){
      if (!A.ctx) return;
      if (A.ctx.currentTime >= A.busyUntil) A.nodes = [];
      var t = A.ctx.currentTime + 0.05;
      pattern.forEach(function(step){
        var f = step[0], d = step[1] / 1000;
        if (f > 0) {
          var o = A.ctx.createOscillator(), g = A.ctx.createGain();
          o.type = 'square'; o.frequency.value = f;
          g.gain.setValueAtTime(0.0001, t);
          g.gain.linearRampToValueAtTime(0.15, t + 0.01);
          g.gain.linearRampToValueAtTime(0.0001, t + d);
          o.connect(g); g.connect(A.ctx.destination);
          o.start(t); o.stop(t + d);
          A.nodes.push(o);
        }
        t += d;
      });
      A.busyUntil = t;
    };
    A.vibrate = function(pattern){
      try {
        if (P.navigator && P.navigator.vibrate) P.navigator.vibrate(pattern);
      } catch(e) {}
    };
    A.stopAll = function(){
      A.nodes.forEach(function(o){ try { o.stop(0); } catch(e) {} });
      A.nodes = [];
      A.busyUntil = 0;
      A.vibrate(0);
    };
    A.stop = function(id){
      if (id === A.lastStopId) return;
      A.lastStopId = id;
      A.stopAll();
    };
    A.handle = function(id, level){
      if (id === A.lastId) return;
      A.lastId = id;
      if (!A.enabled) return;
      if (A.ctx && A.ctx.currentTime < A.busyUntil) return;
      A.vibrate(A.vibrations[level] || A.vibrations[3]);
      if (!A.ctx) return;
      if (A.ctx.state !== 'running') A.ctx.resume();
      A.beepPattern(A.patterns[level] || A.patterns[3]);
    };
    ['pointerdown', 'touchend', 'click', 'keydown'].forEach(function(ev){
      P.document.addEventListener(ev, function(){ if (A.enabled) A.unlock(); },
                                  {passive:true, capture:true});
    });
  }
  return P.ewsAlarm;
})()
""".replace("__PATTERNS__", json.dumps(ALARM_PATTERNS)).replace(
    "__VIBRATIONS__", json.dumps(VIBRATION_PATTERNS)
)

ALARM_CONTROLLER_TEMPLATE = """
<style>
body{margin:0;font-family:"Segoe UI",Arial,sans-serif;color:__TEXT__;background:transparent}
button{width:100%;padding:10px;border-radius:4px;border:1px solid __BORDER__;
       background:__BTN__;color:__TEXT__;font-size:13px;font-weight:700;
       letter-spacing:.06em;text-transform:uppercase;cursor:pointer}
button.on{background:__ACCENT__;border-color:__ACCENT__;color:#fff}
#s{font-size:12px;color:__MUTED__;margin-top:6px}
</style>
<button id="b"></button><div id="s"></div>
<script>
var A = __LIB__;
var b = document.getElementById('b'), s = document.getElementById('s');
var canVib = !!(window.parent.navigator && window.parent.navigator.vibrate);
function render(){
  b.textContent = A.enabled ? 'Matikan suara dan getar' : 'Aktifkan suara dan getar';
  b.className = A.enabled ? 'on' : '';
  if (A.enabled) {
    s.textContent = 'Alarm AKTIF di perangkat ini' +
      (canVib ? ' (suara dan getar).' : ' (suara saja; getar tidak didukung).');
  } else {
    s.textContent = 'Nonaktif. Ketuk tombol sekali untuk mengaktifkan.';
  }
}
b.onclick = function(){
  A.enabled = !A.enabled;
  if (A.enabled) {
    A.unlock();
    A.beepPattern([[880,150],[0,80],[880,150]]);
    A.vibrate([150, 80, 150]);
  } else {
    A.stopAll();
  }
  render();
};
render();
</script>
""".replace("__LIB__", ALARM_LIB_JS)


def controller_html(pal):
    """HTML tombol suara/getar, warnanya mengikuti tema aktif."""
    return (
        ALARM_CONTROLLER_TEMPLATE
        .replace("__TEXT__", pal["text"])
        .replace("__MUTED__", pal["muted"])
        .replace("__BORDER__", pal["border"])
        .replace("__BTN__", pal["surface2"])
        .replace("__ACCENT__", pal["accent"])
    )


def alarm_player_html(alarm):
    return (
        "<script>var A = " + ALARM_LIB_JS + ";"
        f"A.handle({int(alarm['id'])}, {int(alarm['level'])});</script>"
    )


def ack_stop_html(ack):
    return (
        "<script>var A = " + ALARM_LIB_JS + ";"
        f"A.stop({int(ack['id'])});</script>"
    )


def emit_alarm(state, level):
    """Catat event bunyi; browser tiap device yang akan membunyikannya."""
    previous = state.get("alarm")
    new_id = int(time.time() * 1000)
    if previous and new_id <= previous["id"]:
        new_id = previous["id"] + 1
    state["alarm"] = {"id": new_id, "level": level, "time": time.time()}


def raise_alarm(state, level, sound):
    """Alarm baru: wajib di-ACK. Bunyi hanya jika alarm suara aktif."""
    state["ack_required"] = True
    state["ack"] = None
    if sound:
        emit_alarm(state, level)


# ==========================================================
# DATA
# ==========================================================
@st.cache_data
def load_data(path):
    frame = pd.read_excel(path, sheet_name="Telemetry")
    frame["timestamp"] = pd.to_datetime(
        frame["timestamp"], errors="coerce", utc=True
    )

    for parameter in PARAMETERS:
        if parameter not in frame.columns:
            raise KeyError(
                f"Kolom wajib '{parameter}' tidak ditemukan pada dataset {path}."
            )
        frame[parameter] = pd.to_numeric(frame[parameter], errors="coerce")

    frame = frame.dropna(
        subset=["timestamp", "ROP", "WOB", "Torque", "StandpipePressure", "RPM"]
    ).reset_index(drop=True)

    return frame


@st.cache_data
def load_field_data(path):
    """Muat workbook NEAC-001, pangkas ke kedalaman <= FIELD_MAX_DEPTH_M, dan normalkan satuan.

    Timestamp di sini adalah indeks playback sintetis (1 menit/baris), bukan
    timestamp akuisisi lapangan. Jarak sampel kedalaman tetap 0,25 m.
    Kolom tambahan (TVD, hook load, RPM total) hanya untuk tampilan kabin driller.
    """
    raw = pd.read_excel(path, sheet_name=0)
    required = ["Depth: TMD [m]", "WOB Avg [klb]", "ROP INVERSE CF [min/m]",
                "RPM Avg [rpm]", "Torque Avg [lbf·ft]", "Pressure: SPP Avg [psi]"]
    missing = [c for c in required if c not in raw.columns]
    if missing:
        raise ValueError("Kolom wajib tidak ditemukan: " + ", ".join(missing))

    def numeric(column):
        if column in raw.columns:
            return pd.to_numeric(raw[column], errors="coerce")
        return pd.Series(np.nan, index=raw.index)

    out = pd.DataFrame()
    out["Depth_m"] = numeric("Depth: TMD [m]")
    out["TVD_m"] = numeric("Depth: TVD [m]")
    out["ROP_inverse_min_per_m"] = numeric("ROP INVERSE CF [min/m]")
    inv = out["ROP_inverse_min_per_m"].replace(0, np.nan)
    out["ROP"] = 60.0 / inv  # m/h, dengan asumsi kolom benar-benar min/m
    out["WOB_original_klbf"] = numeric("WOB Avg [klb]")
    out["WOB"] = out["WOB_original_klbf"] * 4.448221615  # kN
    out["WOH"] = numeric("WOH Avg [klb]") * 4.448221615  # kN (hook load)
    out["Torque_original_lbf_ft"] = numeric("Torque Avg [lbf·ft]")
    out["Torque"] = out["Torque_original_lbf_ft"] * 0.001355817948  # kN.m
    out["StandpipePressure"] = numeric("Pressure: SPP Avg [psi]")
    out["RPM"] = numeric("RPM Avg [rpm]")
    out["RPM_total"] = numeric("RPM Total Avg [rpm]")

    out = out[out["Depth_m"] <= FIELD_MAX_DEPTH_M]
    out = out.replace([np.inf, -np.inf], np.nan)
    out = out.dropna(
        subset=["Depth_m", "ROP", "WOB", "Torque", "StandpipePressure", "RPM"]
    ).reset_index(drop=True)
    out["timestamp"] = pd.date_range("2026-01-01", periods=len(out), freq="min", tz="UTC")
    out["sample_spacing_m"] = out["Depth_m"].diff().fillna(0.0)
    return out


@st.cache_data(show_spinner=False)
def create_worst_case(source):
    """Buat tren sintetis yang meningkat bertahap hingga magenta."""
    frame = source.copy()
    n = len(frame)
    t = np.arange(n)
    phase = t / max(n - 1, 1)

    ramp1 = np.clip((phase - 0.10) / 0.35, 0, 1)
    ramp2 = np.clip((phase - 0.40) / 0.30, 0, 1)
    ramp3 = np.clip((phase - 0.65) / 0.25, 0, 1)

    frame["ROP"] = (
        2 + 7 * ramp1 + 9 * ramp2 + 12 * ramp3
        + 0.2 * np.sin(t / 8)
    )
    frame["WOB"] = (
        8 + 17 * ramp1 + 22 * ramp2 + 25 * ramp3
        + 0.5 * np.sin(t / 10)
    )
    frame["Torque"] = (
        0.5 + 2.5 * ramp1 + 3 * ramp2 + 4 * ramp3
        + 0.1 * np.sin(t / 7)
    )
    frame["StandpipePressure"] = (
        1000 + 2500 * ramp1 + 3500 * ramp2 + 4500 * ramp3
        + 80 * np.sin(t / 12)
    )
    frame["RPM"] = (
        10 + 18 * ramp1 + 22 * ramp2 + 25 * ramp3
        + 1.0 * np.sin(t / 9)
    )

    return frame


# ==========================================================
# MESIN EVALUASI: sinyal -> debounce/hysteresis -> level
# ==========================================================
def level_for(value, limits, factor=1.0):
    """Level (0-3) untuk satu nilai. factor<1 dipakai untuk hysteresis turun."""
    if value >= limits["magenta"] * factor:
        return 3
    if value >= limits["red"] * factor:
        return 2
    if value >= limits["yellow"] * factor:
        return 1
    return 0


def get_signals(source, index, params):
    """
    Kumpulkan sinyal yang dievaluasi pada baris `index`:
    nilai tiap parameter + laju perubahan (per menit).
    Sensor kosong / di luar rentang masuk daftar `bad` dan tidak dievaluasi.
    """
    row = source.iloc[index].copy()
    faulty = params["test_faulty_sensor"]
    if faulty in PARAMETERS:
        row[faulty] = np.nan  # simulasi sensor mati

    signals = {}
    bad = []
    for name in PARAMETERS:
        value = float(row[name])
        low, high = PLAUSIBLE_RANGE[name]
        if np.isnan(value) or value < low or value > high:
            bad.append(name)
            continue
        signals[name] = (value, params["thresholds"][name])

    rates = {}
    window = int(params["roc_window"])
    if params["roc_enabled"] and index >= window:
        past = source.iloc[index - window]
        seconds = (row["timestamp"] - past["timestamp"]).total_seconds()
        if seconds > 0:
            for name in PARAMETERS:
                if name in bad:
                    continue
                past_value = float(past[name])
                if np.isnan(past_value):
                    continue
                rate = (float(row[name]) - past_value) * 60.0 / seconds
                rates[name] = rate
                limits = params["roc_thresholds"][name]
                signals[name + ROC_SUFFIX] = (
                    abs(rate),
                    {
                        "yellow": limits["yellow"],
                        "red": limits["red"],
                        "magenta": float("inf"),  # laju maksimal level merah
                    },
                )

    return row, signals, bad, rates


def update_levels(signals, params, state):
    """
    Perbarui level tiap sinyal dengan:
    - DEBOUNCE   : naik level hanya jika terlampaui N pembacaan berturut-turut.
    - HYSTERESIS : turun level hanya jika nilai turun di bawah ambang - X%.
    Dipanggil sekali per pembacaan data baru (bukan tiap refresh layar).
    """
    previous = state["signal_levels"]
    first = previous is None
    if first:
        previous = {}
        state["up_pending"] = {}

    keep_factor = 1.0 - float(params["hysteresis_pct"]) / 100.0
    needed = max(1, int(params["debounce_samples"]))
    result = {}

    for name, (value, limits) in signals.items():
        raw = level_for(value, limits)

        if first or name not in previous:
            current = raw  # inisialisasi: langsung, tanpa debounce
            state["up_pending"].pop(name, None)
        else:
            current = previous[name]
            if raw > current:
                pending = state["up_pending"].get(name)
                if pending is None:
                    pending = {"count": 1, "min": raw}
                else:
                    pending = {
                        "count": pending["count"] + 1,
                        "min": min(pending["min"], raw),
                    }
                if pending["count"] >= needed:
                    current = max(current, pending["min"])
                    state["up_pending"].pop(name, None)
                else:
                    state["up_pending"][name] = pending
            else:
                state["up_pending"].pop(name, None)
                if raw < current:
                    held = level_for(value, limits, factor=keep_factor)
                    if held < current:
                        current = held

        result[name] = current

    state["signal_levels"] = result


# ==========================================================
# WHATSAPP (Fonnte)
# ==========================================================
def get_wa_config():
    """Baca kredensial dari secrets (token TIDAK boleh ada di kode/repo)."""
    try:
        cfg = st.secrets["whatsapp"]
        token = str(cfg["token"]).strip()
        targets = str(cfg["targets"]).replace(" ", "")
        if not token or not targets:
            return None
        return {
            "token": token,
            "targets": targets,
            "url": str(cfg.get("api_url", WA_API_URL)),
            "country_code": str(cfg.get("country_code", "62")),
            "app_url": str(cfg.get("app_url", "")),
        }
    except Exception:
        return None


def build_wa_message(title, lines, cfg):
    parts = [f"{WA_PREFIX} EWS PEMBORAN PDSI".strip(), title] + list(lines)
    parts.append(
        "Waktu: " + datetime.now(WIB).strftime("%d-%m-%Y %H:%M:%S") + " WIB"
    )
    if cfg and cfg.get("app_url"):
        parts.append(cfg["app_url"])
    return "\n".join(parts)


def _wa_worker(cfg, message, kind):
    """Berjalan di thread terpisah agar dashboard tidak tertahan."""
    try:
        import requests

        response = requests.post(
            cfg["url"],
            headers={"Authorization": cfg["token"]},
            data={
                "target": cfg["targets"],
                "message": message,
                "countryCode": cfg["country_code"],
            },
            timeout=10,
        )
        try:
            body = response.json()
        except Exception:
            body = {}
        ok = bool(body.get("status", response.ok))
        reason = body.get("reason") or f"HTTP {response.status_code}"
        audit_log(
            "SISTEM",
            "WA TERKIRIM" if ok else "WA GAGAL",
            f"{kind}" + ("" if ok else f": {reason}"),
        )
    except Exception as error:
        audit_log("SISTEM", "WA GAGAL", f"{kind}: {type(error).__name__}")


def notify_wa(state, kind, message, cfg, cooldown=WA_COOLDOWN_SECONDS):
    """Kirim WhatsApp dengan batas frekuensi per jenis pesan (anti-spam)."""
    if not cfg:
        audit_log("SISTEM", "WA DILEWATI", f"{kind}: kredensial WhatsApp belum diisi di secrets")
        return
    now = time.time()
    if now - state["wa_last"].get(kind, 0) < cooldown:
        return
    state["wa_last"][kind] = now
    threading.Thread(
        target=_wa_worker, args=(cfg, message, kind), daemon=True
    ).start()


# ==========================================================
# EVALUASI PER LANGKAH
# ==========================================================
def evaluate(source, params, state, new_data, wa_cfg):
    """Hitung level pada baris saat ini; catat eskalasi, alarm, dan WhatsApp."""
    n = len(source)
    index = max(0, min(state["index"], n - 1))
    row, signals, bad, rates = get_signals(source, index, params)

    if new_data or state["signal_levels"] is None:
        update_levels(signals, params, state)
    else:
        state["signal_levels"] = {
            k: v for k, v in state["signal_levels"].items() if k in signals
        }

    levels = state["signal_levels"]
    level = max(levels.values(), default=0)
    state["rates"] = rates
    state["bad_sensors"] = bad

    # Level turun -> izinkan alarm/WA berikutnya saat naik lagi.
    if level < state["last_alarm_level"]:
        state["last_alarm_level"] = level
    if level < state["wa_last_level"]:
        state["wa_last_level"] = level

    triggered = []
    for name, lv in levels.items():
        if lv < 1:
            continue
        if name.endswith(ROC_SUFFIX):
            base = name[: -len(ROC_SUFFIX)]
            triggered.append(f"{base} laju {rates.get(base, 0):+.1f}/mnt")
        elif name in signals:
            triggered.append(f"{name} {signals[name][0]:.1f}")

    # Catat eskalasi
    if level > state["previous_level"] and level > 0:
        state["events"].insert(0, {
            "Waktu": row["timestamp"].strftime("%Y-%m-%d %H:%M:%S UTC"),
            "Level": LEVELS[level]["name"],
            "Parameter": ", ".join(triggered),
        })
        del state["events"][EVENTS_MAX:]

    # Alarm baru (naik level) atau ulang magenta selama belum di-ACK
    if level > state["last_alarm_level"]:
        if level > 0:
            raise_alarm(state, level, sound=params["alarm_enabled"])
        state["last_alarm_level"] = level
    elif (
        level == 3
        and new_data
        and params["alarm_enabled"]
        and params["repeat_alarm"]
        and params["auto"]
        and state["ack_required"]
    ):
        emit_alarm(state, 3)

    # WhatsApp saat naik level
    if level > state["wa_last_level"]:
        if params["wa_enabled"] and level >= params["wa_min_level"]:
            message = build_wa_message(
                f"STATUS: {LEVELS[level]['name']} ({LEVELS[level]['description']})",
                ["Penyebab: " + ", ".join(triggered)],
                wa_cfg,
            )
            notify_wa(state, f"level{level}", message, wa_cfg)
        state["wa_last_level"] = level

    state["previous_level"] = level
    return level


def update_faults(state, params, stale, age, effective_stale, wa_cfg):
    """Deteksi gangguan data: data terlambat dan sensor tidak valid."""
    current = set()
    if stale:
        current.add("STALE")
    for name in state["bad_sensors"]:
        current.add("SENSOR:" + name)

    previous = state["fault_keys"]
    stamp = datetime.now(WIB).strftime("%Y-%m-%d %H:%M:%S WIB")

    def describe(key):
        if key == "STALE":
            return f"Data terlambat ({age:.0f} detik, batas {effective_stale:.0f})"
        return f"Sensor {key.split(':', 1)[1]} tidak valid"

    for key in sorted(current - previous):
        state["events"].insert(0, {
            "Waktu": stamp, "Level": "GANGGUAN DATA", "Parameter": describe(key),
        })
        raise_alarm(state, 4, sound=params["alarm_enabled"])
        if params["wa_enabled"]:
            message = build_wa_message(
                "GANGGUAN DATA", [describe(key), "Status EWS tidak dapat dipercaya."], wa_cfg
            )
            notify_wa(state, "fault", message, wa_cfg)

    for key in sorted(previous - current):
        label = "Data kembali normal" if key == "STALE" else f"Sensor {key.split(':', 1)[1]} pulih"
        state["events"].insert(0, {
            "Waktu": stamp, "Level": "PULIH", "Parameter": label,
        })

    del state["events"][EVENTS_MAX:]
    state["fault_keys"] = current



# ==========================================================
# AUTO PILOT FUZZY-PID
# Data rig diakuisisi, diproses pengendali PID yang gain-nya di-tuning
# logika fuzzy, lalu keluarannya dihaluskan. Pengereman putaran (RPM) dan
# WOB dihitung dari kenaikan torsi dan standpipe pressure. Model proses yang
# dipakai adalah pendekatan sederhana untuk simulasi, bukan model rig nyata.
# ==========================================================

AP_A_ROP, AP_B_ROP, AP_C_TORQUE = 0.8, 0.5, 0.3   # eksponen model pendekatan
AP_TORQUE_WOB_EXP = 0.3   # sensitivitas torsi terhadap WOB (0 = tidak bergantung, 1 = sebanding)
AP_WARMUP = 40         # langkah awal: baseline belajar cepat dan pengereman ditahan
AP_MAX_BOOST = 0.12      # kenaikan maksimum WOB (12%) untuk menaikkan ROP
AP_ATTACK, AP_RELEASE = 0.03, 0.02   # laju naik/turun tingkat pengereman per detik
AP_CHANNELS = ["ROP", "WOB", "Torque", "StandpipePressure", "RPM"]

# gain dasar tiap kanal: kp, ki, kd, batas laju perubahan (fraksi nilai acuan per detik)
AP_BASE_GAINS = {
    "WOB": (0.20, 0.004, 0.25, 0.06),
    "RPM": (0.25, 0.004, 0.25, 0.06),
    "ROP": (0.30, 0.005, 0.25, 0.10),
    "Torque": (0.30, 0.005, 0.25, 0.10),
    "StandpipePressure": (0.25, 0.004, 0.25, 0.05),
}

# Tabel aturan fuzzy (baris: |e| = Kecil, Sedang, Besar; kolom: |de| = Kecil, Sedang, Besar)
# nilai = penyesuaian relatif terhadap gain dasar (Sugeno orde-nol)
FUZZY_KP = [[-0.30, -0.20, 0.00], [0.00, 0.20, 0.30], [0.40, 0.50, 0.60]]
FUZZY_KI = [[0.30, 0.20, 0.00], [0.00, -0.10, -0.20], [-0.30, -0.40, -0.50]]
FUZZY_KD = [[-0.20, 0.00, 0.20], [0.00, 0.20, 0.40], [0.00, 0.30, 0.50]]


def _clip(x, lo, hi):
    return lo if x < lo else hi if x > hi else x


def _mf3(x):
    """Keanggotaan fuzzy Kecil/Sedang/Besar untuk nilai ternormalisasi x >= 0."""
    small = max(0.0, 1.0 - x / 0.4)
    medium = max(0.0, 1.0 - abs(x - 0.4) / 0.3)
    big = _clip((x - 0.3) / 0.4, 0.0, 1.0)
    return small, medium, big


def fuzzy_gain_adjust(e_norm, de_norm):
    """Inferensi fuzzy: (|e|, |de|) -> penyesuaian (dKp, dKi, dKd)."""
    mu_e = _mf3(abs(e_norm))
    mu_d = _mf3(abs(de_norm))
    num = [0.0, 0.0, 0.0]
    den = 0.0
    for i in range(3):
        for j in range(3):
            w = min(mu_e[i], mu_d[j])
            if w <= 0:
                continue
            den += w
            num[0] += w * FUZZY_KP[i][j]
            num[1] += w * FUZZY_KI[i][j]
            num[2] += w * FUZZY_KD[i][j]
    if den <= 0:
        return 0.0, 0.0, 0.0
    return num[0] / den, num[1] / den, num[2] / den


def new_channel(value):
    return {"y": float(value), "e1": 0.0, "I": 0.0, "dy": 0.0, "kp": 0.0, "ki": 0.0, "kd": 0.0}


def fpid_step(channel, name, ref, nominal):
    """Satu langkah PID yang gain-nya di-tuning fuzzy; mengembalikan keluaran halus."""
    kp0, ki0, kd0, rate = AP_BASE_GAINS[name]
    nominal = max(abs(nominal), 1e-6)
    e = ref - channel["y"]
    de = e - channel["e1"]
    d_kp, d_ki, d_kd = fuzzy_gain_adjust(e / (0.15 * nominal), de / (0.05 * nominal))
    kp, ki, kd = kp0 * (1 + d_kp), ki0 * (1 + d_ki), kd0 * (1 + d_kd)

    channel["I"] = _clip(channel["I"] + e, -3.0 * nominal, 3.0 * nominal)
    du = kp * e + ki * channel["I"] - kd * channel["dy"]
    du = _clip(du, -rate * nominal, rate * nominal)

    channel["y"] += du
    channel["dy"] = du
    channel["e1"] = e
    channel["kp"], channel["ki"], channel["kd"] = kp, ki, kd
    return channel["y"]


def _low(x, med):
    """Keanggotaan 'naik kecil' (1 jika <= 1/3 ambang sedang, 0 jika >= ambang sedang)."""
    return _clip((med - x) / max(med * 2.0 / 3.0, 1e-6), 0.0, 1.0)


def fuzzy_brake(rise_t, rise_p, med, high):
    """
    Inferensi fuzzy pengereman putaran (RPM) dan WOB.
    Masukan: kenaikan torsi (%) dan standpipe pressure (%) terhadap baseline.
    Keluaran: tingkat pengereman 0..1 (Sugeno, t-norm min).
    """
    def mf(x):
        low = _low(x, med)
        mid = _clip(1.0 - abs(x - med) / max(high - med, 1e-6), 0.0, 1.0)
        top = _clip((x - med) / max(high - med, 1e-6), 0.0, 1.0)
        return low, mid, top

    mt, mp = mf(rise_t), mf(rise_p)
    rules = [  # [torsi][tekanan] -> tingkat pengereman
        [0.00, 0.05, 0.10],
        [0.05, 0.50, 0.80],
        [0.15, 0.80, 1.00],
    ]
    num = den = 0.0
    for i in range(3):
        for j in range(3):
            w = min(mt[i], mp[j])
            num += w * rules[i][j]
            den += w
    return num / den if den > 0 else 0.0


def ap_new(row, params):
    raw = {p: float(row[p]) for p in AP_CHANNELS}
    k0 = raw["ROP"] / max(raw["WOB"] ** AP_A_ROP * raw["RPM"] ** AP_B_ROP, 1e-6)
    ct0 = raw["Torque"] / max(raw["WOB"] * raw["RPM"] ** AP_C_TORQUE, 1e-6)
    return {
        "ema": dict(raw),
        "K": k0, "Ct": ct0,
        "Ct_fast": ct0, "base_Ct": ct0, "base_P": raw["StandpipePressure"], "n": 0,
        "base_T": raw["Torque"], "wob_slow": raw["WOB"], "rpm_slow": raw["RPM"], "rop_max": raw["ROP"],
        "nom": {"Torque": raw["Torque"], "StandpipePressure": raw["StandpipePressure"]},
        "brake": 0.0, "boost": 0.0,
        "rise_t": 0.0, "rise_p": 0.0,
        "ch": {p: new_channel(raw[p]) for p in AP_CHANNELS},
        "hist": {k: [] for k in (
            "t", "idx", "depth", "brake", "boost", "wob_cmd", "rpm_cmd",
            *[f"raw_{p}" for p in AP_CHANNELS], *[f"ap_{p}" for p in AP_CHANNELS],
        )},
        "last_index": None,
        "active": True,
        "scenario": params["scenario"],
        "uid": int(time.time() * 1000),
    }


def ap_step(ap, row, params, index, timestamp):
    sane = {
        "ROP": (0.0, 500.0), "WOB": (0.0, 500.0), "Torque": (0.0, 100.0),
        "StandpipePressure": (0.0, 15000.0), "RPM": (0.0, 300.0),
    }
    raw = {}
    for p in AP_CHANNELS:
        v = float(row[p])
        lo, hi = sane[p]
        raw[p] = v if (np.isfinite(v) and lo <= v <= hi) else ap["ema"][p]

    ema = ap["ema"]
    for p in AP_CHANNELS:
        ema[p] += 0.25 * (raw[p] - ema[p])

    if raw["WOB"] > 1e-6 and raw["RPM"] > 1e-6 and raw["ROP"] > 0:
        k_inst = raw["ROP"] / (raw["WOB"] ** AP_A_ROP * raw["RPM"] ** AP_B_ROP)
        ct_inst = raw["Torque"] / (raw["WOB"] * raw["RPM"] ** AP_C_TORQUE)
        ap["K"] += 0.08 * (k_inst - ap["K"])
        ap["Ct"] += 0.08 * (ct_inst - ap["Ct"])
        ap["Ct_fast"] += 0.15 * (ct_inst - ap["Ct_fast"])

    # --- indikator risiko dari torsi dan tekanan (vs baseline) ---
    ap["n"] += 1
    if ap["n"] <= 25:   # acuan torsi dan tekanan = rata-rata masa pemanasan awal
        ap["nom"]["Torque"] += (ema["Torque"] - ap["nom"]["Torque"]) / ap["n"]
        ap["nom"]["StandpipePressure"] += (
            ema["StandpipePressure"] - ap["nom"]["StandpipePressure"]
        ) / ap["n"]
    # Kenaikan torsi dinilai dari DUA sudut dan diambil yang lebih kecil: torsi mentah dan
    # torsi per satuan WOB. Dengan begitu lonjakan WOB sesaat (torsi ikut naik) atau WOB yang
    # berayun saat torsi datar tidak memicu pengereman; yang memicu hanya torsi naik tanpa WOB naik.
    rise_raw = (ema["Torque"] / max(ap["base_T"], 1e-9) - 1.0) * 100.0
    rise_ct = (ap["Ct_fast"] / max(ap["base_Ct"], 1e-9) - 1.0) * 100.0
    ap["rise_t"] = min(rise_raw, rise_ct)
    ap["rise_p"] = (ema["StandpipePressure"] / max(ap["base_P"], 1e-6) - 1.0) * 100.0
    med, high = float(params["ap_rise_med_pct"]), float(params["ap_rise_high_pct"])
    target = fuzzy_brake(ap["rise_t"], ap["rise_p"], med, high) if ap["n"] > AP_WARMUP else 0.0
    ap["brake"] += _clip(target - ap["brake"], -AP_RELEASE, AP_ATTACK)

    if True:   # baseline terus belajar: cepat saat awal, lambat saat ada indikasi bahaya
        rate = 0.25 if ap["n"] < AP_WARMUP else (0.02 if ap["brake"] < 0.10 else 0.004)
        ap["base_Ct"] += rate * (ap["Ct_fast"] - ap["base_Ct"])
        ap["base_T"] += rate * (ema["Torque"] - ap["base_T"])
        # tekanan memang naik perlahan seiring kedalaman: baseline mengikuti tren lambat itu
        rate_p = 0.25 if ap["n"] < AP_WARMUP else (0.05 if ap["brake"] < 0.10 else 0.01)
        ap["base_P"] += rate_p * (ema["StandpipePressure"] - ap["base_P"])
    slow_rate = 0.25 if ap["n"] < AP_WARMUP else 0.05
    ap["wob_slow"] += slow_rate * (raw["WOB"] - ap["wob_slow"])
    ap["rpm_slow"] += slow_rate * (raw["RPM"] - ap["rpm_slow"])

    # --- booster ROP: naikkan WOB bila ROP di bawah target dan risiko rendah ---
    rop_y = ap["ch"]["ROP"]["y"]
    deficit = max(0.0, (params["ap_rop_target"] - rop_y) / max(params["ap_rop_target"], 1e-6))
    want = _clip((deficit - 0.10) / 0.30, 0.0, 1.0)
    gate = _clip(1.0 - ap["brake"] / 0.30, 0.0, 1.0)   # booster mati bila ada indikasi bahaya
    boost_target = AP_MAX_BOOST * want * gate
    ap["boost"] += _clip(boost_target - ap["boost"], -0.004, 0.004)

    b = ap["brake"]
    wob_ref = params["ap_wob_sp"] * (1.0 + ap["boost"]) * (1.0 - 0.5 * b)
    rpm_ref = params["ap_rpm_sp"] * (1.0 - 0.7 * b)

    ch = ap["ch"]
    wob_y = fpid_step(ch["WOB"], "WOB", wob_ref, params["ap_wob_sp"])
    rpm_y = fpid_step(ch["RPM"], "RPM", rpm_ref, params["ap_rpm_sp"])

    rop_model = ap["K"] * max(wob_y, 1e-6) ** AP_A_ROP * max(rpm_y, 1e-6) ** AP_B_ROP
    wob_ratio = _clip(max(wob_y, 1e-6) / max(ap["wob_slow"], 1e-6), 0.4, 1.6)
    rpm_ratio = _clip(max(rpm_y, 1e-6) / max(ap["rpm_slow"], 1e-6), 0.3, 1.6)
    torque_model = ema["Torque"] * wob_ratio ** AP_TORQUE_WOB_EXP * rpm_ratio ** AP_C_TORQUE
    ap["rop_max"] = max(ema["ROP"], ap["rop_max"] * 0.998)
    rop_model = min(rop_model, 1.15 * ap["rop_max"])   # batas kewajaran model
    rop_out = fpid_step(ch["ROP"], "ROP", rop_model, params["ap_rop_target"])
    torque_out = fpid_step(ch["Torque"], "Torque", torque_model, ap["nom"]["Torque"])
    spp_out = fpid_step(ch["StandpipePressure"], "StandpipePressure", ema["StandpipePressure"], ap["nom"]["StandpipePressure"])

    h = ap["hist"]
    h["t"].append(timestamp); h["idx"].append(index)
    try:
        h["depth"].append(float(row["Depth_m"]))
    except (KeyError, TypeError, ValueError):
        h["depth"].append(float("nan"))
    h["brake"].append(b); h["boost"].append(ap["boost"])
    h["wob_cmd"].append(wob_ref); h["rpm_cmd"].append(rpm_ref)
    for p in AP_CHANNELS:
        h[f"raw_{p}"].append(raw[p])
    h["ap_ROP"].append(rop_out); h["ap_WOB"].append(wob_y); h["ap_Torque"].append(torque_out)
    h["ap_StandpipePressure"].append(spp_out); h["ap_RPM"].append(rpm_y)
    ap["last_index"] = index


def ap_sync(source, params, state):
    """
    Akuisisi dan proses data rig oleh auto pilot. Setiap aktivasi dimulai dari
    kondisi saat ini (transfer tanpa lonjakan); baris yang terlewat diproses
    berurutan agar dinamika pengendali tetap konsisten.
    """
    ap = state["ap"]
    if not params["autopilot"]:
        if ap:
            ap["active"] = False
        return

    index = max(0, min(state["index"], len(source) - 1))
    restart = (
        ap is None
        or not ap.get("active")
        or ap["scenario"] != params["scenario"]
        or ap["last_index"] is None
        or index < ap["last_index"]
    )
    if restart:
        row = source.iloc[index]
        ap = state["ap"] = ap_new(row, params)
        ap_step(ap, row, params, index, source["timestamp"].iloc[index])
        return

    for i in range(ap["last_index"] + 1, index + 1):
        ap_step(ap, source.iloc[i], params, i, source["timestamp"].iloc[i])


def process_simulation(data, ap_data=None, field_data=None):
    """
    Majukan simulasi berdasarkan waktu server (bukan per sesi browser).
    Aman dipanggil banyak device sekaligus: lock memastikan simulasi
    hanya maju sekali per interval.
    """
    wa_cfg = get_wa_config()

    with shared["lock"]:
        params = shared["params"]
        state = shared["state"]

        if params["scenario"] == SCENARIOS[0]:
            source = create_worst_case(data)
        elif params["scenario"] == SCENARIOS[2] and ap_data is not None:
            source = ap_data.copy()
        elif params["scenario"] == SCENARIOS[3] and field_data is not None:
            source = field_data.copy()
        else:
            source = data.copy()

        n = len(source)
        state["n"] = n
        now = time.time()
        interval = max(int(params["interval"]), 1)
        paused = not params["auto"]
        steps = 0

        if params["auto"] and not params["test_outage"]:
            steps = int((now - state["last_advance"]) // interval)
            if steps > 0:
                state["last_advance"] += steps * interval
                state["last_data_time"] = now
        else:
            state["last_advance"] = now
            if paused:
                state["last_data_time"] = now  # dijeda bukan berarti data basi

        steps = min(steps, MAX_CATCHUP_STEPS)

        for _ in range(steps):
            next_index = state["index"] + params["batch_size"]

            if next_index >= n:
                if params["loop_data"]:
                    next_index = 0
                    state["previous_level"] = -1
                    state["last_alarm_level"] = -1
                    state["wa_last_level"] = -1
                    state["signal_levels"] = None
                else:
                    next_index = n - 1

            state["index"] = next_index
            evaluate(source, params, state, True, wa_cfg)

        if steps == 0:
            evaluate(source, params, state, False, wa_cfg)

        ap_sync(source, params, state)

        effective_stale = max(float(params["stale_after"]), 2.0 * interval + 1.0)
        age = now - state["last_data_time"]
        stale = (not paused) and age > effective_stale
        update_faults(state, params, stale, age, effective_stale, wa_cfg)

        level = max(state["signal_levels"].values(), default=0)
        if level == 0 and not state["fault_keys"]:
            state["ack_required"] = False
            state["ack"] = None

        alarm = dict(state["alarm"]) if state["alarm"] else None
        ack = dict(state["ack"]) if state["ack"] else None
        ap_state = state["ap"]
        ap_snapshot = None
        if ap_state and ap_state["hist"]["t"]:
            ap_snapshot = {
                "hist": {k: list(v) for k, v in ap_state["hist"].items()},
                "gains": {
                    p: (c["kp"], c["ki"], c["kd"]) for p, c in ap_state["ch"].items()
                },
                "brake": ap_state["brake"],
                "boost": ap_state["boost"],
                "rise_t": ap_state["rise_t"],
                "rise_p": ap_state["rise_p"],
                "nom": dict(ap_state["nom"]),
                "uid": ap_state.get("uid", 0),
            }
        snapshot = {
            "ap": ap_snapshot,
            "ap_on": bool(params["autopilot"]),
            "index": max(0, min(state["index"], n - 1)),
            "events": list(state["events"]),
            "params": copy.deepcopy(params),
            "level": level,
            "signal_levels": dict(state["signal_levels"]),
            "rates": dict(state["rates"]),
            "bad_sensors": list(state["bad_sensors"]),
            "fault_keys": set(state["fault_keys"]),
            "stale": stale,
            "stale_age": age,
            "stale_limit": effective_stale,
            "paused": paused,
            "ack_required": state["ack_required"],
            "ack": ack,
            "ack_fresh": bool(ack and time.time() - ack["time"] < ALARM_FRESH_SECONDS),
            "alarm": alarm,
            "alarm_fresh": bool(
                alarm and time.time() - alarm["time"] < ALARM_FRESH_SECONDS
            ),
        }

    return source, snapshot


# ==========================================================
# CALLBACK WIDGET (dipanggil saat pengguna mengubah sesuatu)
# ==========================================================
def _touch():
    shared["updated_at"] = datetime.now(WIB)
    shared["updated_by"] = session_user()


def _require_admin(action):
    if session_role() == "admin":
        return True
    audit_log(session_user(), "DITOLAK", f"{action} (bukan admin)")
    return False


def _invalidate_levels():
    shared["state"]["signal_levels"] = None


def _apply_preset(params, scenario):
    preset = SCENARIO_PRESETS.get(scenario)
    if not preset:
        return
    params["thresholds"] = copy.deepcopy(preset["thresholds"])
    params["roc_thresholds"] = copy.deepcopy(preset["roc_thresholds"])
    for key in ("ap_wob_sp", "ap_rpm_sp", "ap_rop_target"):
        params[key] = preset[key]


def apply_scenario_preset():
    """Kembalikan ambang, ambang laju, dan setpoint auto pilot ke preset skenario aktif."""
    if not _require_admin("terapkan preset skenario"):
        return
    with shared["lock"]:
        _apply_preset(shared["params"], shared["params"]["scenario"])
        _invalidate_levels()
        _touch()
        audit_log(session_user(), "PRESET SKENARIO", f"diterapkan ulang: {shared['params']['scenario']}")


def toggle_play():
    """Jeda atau lanjutkan simulasi (tombol di kabin driller)."""
    if not _require_admin("jeda/lanjutkan simulasi"):
        return
    with shared["lock"]:
        shared["params"]["auto"] = not shared["params"]["auto"]
        _touch()
        audit_log(
            session_user(), "UBAH PARAMETER",
            "Simulasi otomatis: " + ("dilanjutkan" if shared["params"]["auto"] else "dijeda"),
        )


def toggle_autopilot():
    """Aktifkan atau matikan auto pilot (tombol di kabin driller)."""
    if not _require_admin("ubah mode auto pilot"):
        return
    with shared["lock"]:
        shared["params"]["autopilot"] = not shared["params"]["autopilot"]
        _touch()
        audit_log(
            session_user(), "UBAH PARAMETER",
            "Mode auto pilot: " + ("aktif" if shared["params"]["autopilot"] else "nonaktif"),
        )


def update_param(name, key):
    if not _require_admin(f"ubah {name}"):
        return
    with shared["lock"]:
        old = shared["params"].get(name)
        new = st.session_state[key]
        if old == new:
            return
        shared["params"][name] = new
        if name in ("roc_enabled", "roc_window", "test_faulty_sensor"):
            _invalidate_levels()
        _touch()
        audit_log(
            session_user(), "UBAH PARAMETER",
            f"{PARAM_LABELS.get(name, name)}: dari {old} menjadi {new}",
        )
        if name == "scenario":
            # Ganti skenario = mulai ulang simulasi dengan ambang/setpoint skenario itu.
            _apply_preset(shared["params"], new)
            shared["state"] = fresh_state()
            audit_log(session_user(), "PRESET SKENARIO", f"ambang dan setpoint auto pilot untuk: {new}")


def update_threshold(parameter, level_name, key):
    if not _require_admin("ubah ambang"):
        return
    with shared["lock"]:
        old = shared["params"]["thresholds"][parameter][level_name]
        new = float(st.session_state[key])
        if old == new:
            return
        shared["params"]["thresholds"][parameter][level_name] = new
        _invalidate_levels()
        _touch()
        audit_log(
            session_user(), "UBAH AMBANG",
            f"{parameter} {level_name}: dari {old:g} menjadi {new:g}",
        )


def update_roc_threshold(parameter, level_name, key):
    if not _require_admin("ubah ambang laju"):
        return
    with shared["lock"]:
        old = shared["params"]["roc_thresholds"][parameter][level_name]
        new = float(st.session_state[key])
        if old == new:
            return
        shared["params"]["roc_thresholds"][parameter][level_name] = new
        _invalidate_levels()
        _touch()
        audit_log(
            session_user(), "UBAH AMBANG LAJU",
            f"{parameter} {level_name}: dari {old:g} menjadi {new:g} per menit",
        )


def reset_simulation():
    if not _require_admin("reset simulasi"):
        return
    with shared["lock"]:
        shared["state"] = fresh_state()
        _touch()
        audit_log(session_user(), "RESET SIMULASI", "posisi, riwayat, dan ACK dikosongkan")


def jump_to_last():
    if not _require_admin("ke data terakhir"):
        return
    with shared["lock"]:
        state = shared["state"]
        total_rows = state["n"]
        state["index"] = total_rows - 1
        state["previous_level"] = -1
        state["signal_levels"] = None
        state["last_advance"] = time.time()
        state["last_data_time"] = time.time()
        _touch()
        audit_log(session_user(), "KE DATA TERAKHIR", f"baris {total_rows}")


def reset_params_to_default():
    if not _require_admin("reset parameter ke default"):
        return
    with shared["lock"]:
        shared["params"] = copy.deepcopy(DEFAULT_PARAMS)
        _invalidate_levels()
        _touch()
        audit_log(session_user(), "RESET PARAMETER", "semua parameter dikembalikan ke default")


def ack_alarm():
    """ACK: hentikan bunyi/getar di semua device dan catat pelakunya."""
    user = session_user()
    if session_role() not in ACK_ROLES:
        audit_log(user, "DITOLAK", "ACK alarm (peran tidak diizinkan)")
        return
    wa_cfg = get_wa_config()
    with shared["lock"]:
        state = shared["state"]
        if not state["ack_required"]:
            return
        now = time.time()
        state["ack_required"] = False
        state["ack"] = {"by": user, "time": now, "id": int(now * 1000)}
        level = max((state["signal_levels"] or {}).values(), default=0)
        audit_log(
            user, "ACK ALARM",
            f"level {LEVELS[level]['name']}" + (
                ", gangguan data: " + ", ".join(sorted(state["fault_keys"]))
                if state["fault_keys"] else ""
            ),
        )
        if shared["params"]["wa_enabled"]:
            message = build_wa_message(
                "ALARM DIAKUI", [f"Oleh: {user}", f"Level saat ini: {LEVELS[level]['name']}"], wa_cfg
            )
            notify_wa(state, "ack", message, wa_cfg, cooldown=0)


def send_wa_test():
    if not _require_admin("tes WhatsApp"):
        return
    cfg = get_wa_config()
    user = session_user()
    if not cfg:
        audit_log(user, "WA TES GAGAL", "kredensial WhatsApp belum diisi di secrets")
        try:
            st.toast("Kredensial WhatsApp belum diisi di secrets.")
        except Exception:
            pass
        return
    with shared["lock"]:
        message = build_wa_message("PESAN TES", [f"Dikirim oleh: {user}"], cfg)
        audit_log(user, "WA TES", "pesan tes dikirim")
        notify_wa(shared["state"], "test", message, cfg, cooldown=0)
    try:
        st.toast("Pesan tes dikirim. Cek WhatsApp dan log audit.")
    except Exception:
        pass

# ==========================================================
# HELPER TAMPILAN (HTML)
# ==========================================================
RUN_START = time.time()
role = session_role()
is_admin = role == "admin"
current_user = session_user()

DISPLAY_NAME = {"StandpipePressure": "Standpipe Pressure"}
SHORT_NAME = {"ROP": "ROP", "WOB": "WOB", "Torque": "Torsi", "StandpipePressure": "SPP", "RPM": "RPM"}
DECIMALS = {"ROP": 1, "WOB": 1, "Torque": 2, "StandpipePressure": 0, "RPM": 0}
LEVEL_CLASS = {
    "HIJAU": "lv0", "KUNING": "lv1", "MERAH": "lv2", "MAGENTA": "lv3",
    "GANGGUAN DATA": "lverr", "PULIH": "lv0",
}
AP_COLORS = {
    "ROP": "#2F80ED", "WOB": "#27AE60", "Torque": "#EB5757",
    "StandpipePressure": "#9B51E0", "RPM": "#F2994A",
}
AP_CHANNEL_LIST = ["ROP", "WOB", "Torque", "StandpipePressure", "RPM"]
STRIP_POINTS = 120  # jumlah sampel pada grafik mini kabin


def _h(text):
    """Ratakan HTML satu baris agar tidak terbaca sebagai blok kode markdown."""
    return "".join(line.strip() for line in text.splitlines())


def notice(text, kind=""):
    return _h(f'<div class="notice {kind}">{text}</div>')


def html_table(rows, columns, chip_column=None):
    head = "".join(f"<th>{html.escape(c)}</th>" for c in columns)
    body = []
    for row in rows:
        cells = []
        for column in columns:
            value = html.escape(str(row.get(column, "")))
            if column == chip_column:
                css = LEVEL_CLASS.get(str(row.get(column, "")), "lv0")
                cells.append(f'<td><span class="chip {css}">{value}</span></td>')
            else:
                cells.append(f"<td>{value}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    return _h(
        '<div class="tbl-wrap"><table class="ews-table"><thead><tr>'
        + head + "</tr></thead><tbody>" + "".join(body) + "</tbody></table></div>"
    )


def _num(value, decimals=1, fallback="-"):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if not np.isfinite(number):
        return fallback
    return f"{number:,.{decimals}f}"


def show_image(target, png_bytes):
    """Tampilkan gambar PNG selebar kolom; kompatibel lintas versi Streamlit."""
    for extra in ({"use_container_width": True}, {"width": "stretch"}, {}):
        try:
            return target.image(png_bytes, **extra)
        except TypeError:
            continue
    return target.image(png_bytes)


# ==========================================================
# GRAFIK PNG (Figure langsung, bukan pyplot; hasil di-cache dan dipakai bersama)
# ==========================================================
def _fig_png(fig):
    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", bbox_inches="tight", facecolor=fig.get_facecolor())
    return buffer.getvalue()


def _style_axes(fig, ax, pal):
    fig.patch.set_facecolor(pal["surface"])
    ax.set_facecolor(pal["surface"])
    ax.tick_params(colors=pal["muted"], labelsize=8)
    ax.yaxis.label.set_color(pal["muted"])
    ax.xaxis.label.set_color(pal["muted"])
    ax.title.set_color(pal["text"])
    for spine in ax.spines.values():
        spine.set_color(pal["border"])
    ax.grid(alpha=0.6, color=pal["chart_grid"], linewidth=0.6)


@st.cache_data(show_spinner=False, max_entries=80)
def render_trend_png(cache_key, theme, parameter, limits, x_label, _x, _values):
    pal = PALETTES.get(theme, PALETTES["dark"])
    fig = Figure(figsize=(6.4, 2.5), dpi=80)
    ax = fig.subplots()
    _style_axes(fig, ax, pal)
    name = DISPLAY_NAME.get(parameter, parameter)
    ax.plot(_x, _values, color=pal["accent"], linewidth=1.8, label=name)
    ax.axhline(limits["yellow"], color="#D9A400", linestyle="--", linewidth=1, label="Kuning")
    ax.axhline(limits["red"], color="#D32F2F", linestyle="--", linewidth=1, label="Merah")
    ax.axhline(limits["magenta"], color="#B0158F", linestyle="--", linewidth=1, label="Magenta")
    ax.set_ylabel(UNITS[parameter], fontsize=9)
    ax.set_xlabel(x_label, fontsize=8)
    ax.set_title(f"Tren {name}", fontsize=11, loc="left")
    ax.legend(
        fontsize=7, facecolor=pal["surface"], edgecolor=pal["border"],
        labelcolor=pal["text"], loc="upper left",
    )
    if x_label == "Waktu":
        fig.autofmt_xdate()
    fig.tight_layout()
    return _fig_png(fig)


@st.cache_data(show_spinner=False, max_entries=24)
def render_ap_png(cache_key, theme, show_raw, nominal, x_label, _hist):
    pal = PALETTES.get(theme, PALETTES["dark"])
    fig = Figure(figsize=(11, 3.9), dpi=80)
    ax = fig.subplots()
    _style_axes(fig, ax, pal)

    if x_label == "Kedalaman TMD (m)":
        x = np.array(_hist["depth"], dtype=float)
    else:
        x = pd.to_datetime(_hist["t"])

    def series(key):
        return np.array(_hist[key], dtype=float)

    ap_max = 100.0
    raw_peaks = []
    for name in AP_CHANNEL_LIST:
        scale = max(float(nominal[name]), 1e-9)
        ap_line = series(f"ap_{name}") / scale * 100.0
        ap_max = max(ap_max, float(np.nanmax(ap_line)))
        if show_raw:
            raw_line = series(f"raw_{name}") / scale * 100.0
            raw_peaks.append(float(np.nanpercentile(raw_line, 99)))
            ax.plot(x, raw_line, color=AP_COLORS[name], alpha=0.28, linewidth=0.8)
        ax.plot(
            x, ap_line, color=AP_COLORS[name], linewidth=2.2,
            label=f"{DISPLAY_NAME.get(name, name)} ({UNITS[name]})",
        )

    brake = series("brake")
    if np.any(brake > 0.10):
        ax.fill_between(
            x, 0, 1, where=brake > 0.10, transform=ax.get_xaxis_transform(),
            color="#D32F2F", alpha=0.20, label="Pengereman aktif",
        )

    top = max(130.0, ap_max * 1.12)
    if raw_peaks:
        top = min(max(top, max(raw_peaks) * 1.05), 450.0)
    ax.set_ylim(0, top)
    ax.axhline(100, color=pal["muted"], linestyle=":", linewidth=1)
    ax.set_ylabel("Persen terhadap nilai acuan (%)", fontsize=9)
    ax.set_xlabel(x_label, fontsize=8)
    ax.set_title(
        "Hasil auto pilot Fuzzy-PID (garis tebal) dibanding data mentah rig (samar)",
        fontsize=11, loc="left",
    )
    ax.legend(
        loc="upper center", bbox_to_anchor=(0.5, -0.17), ncol=6, fontsize=7,
        facecolor=pal["surface"], edgecolor=pal["border"], labelcolor=pal["text"],
    )
    if x_label == "Waktu":
        fig.autofmt_xdate()
    fig.tight_layout()
    return _fig_png(fig)


# ==========================================================
# KOMPONEN KABIN DRILLER (SVG/HTML murni, ringan dan ikut tema)
# ==========================================================
def _arc(cx, cy, r, f0, f1):
    a0, a1 = math.pi * (1.0 - f0), math.pi * (1.0 - f1)
    x0, y0 = cx + r * math.cos(a0), cy - r * math.sin(a0)
    x1, y1 = cx + r * math.cos(a1), cy - r * math.sin(a1)
    return f"M{x0:.2f} {y0:.2f} A{r} {r} 0 0 1 {x1:.2f} {y1:.2f}"


def gauge_html(parameter, value, limits, level, bad, rate_text, ap_value=None):
    """Satu gauge setengah lingkaran dengan zona hijau/kuning/merah/magenta."""
    name = SHORT_NAME[parameter]
    decimals = DECIMALS[parameter]
    css = "lverr" if bad else f"lv{level}"
    vmax = float(limits["magenta"]) * 1.2
    cx, cy, radius = 100.0, 100.0, 78.0

    def frac(v):
        return min(max(float(v) / vmax, 0.0), 1.0)

    zones = [
        (0.0, frac(limits["yellow"]), "var(--lv0)"),
        (frac(limits["yellow"]), frac(limits["red"]), "var(--lv1)"),
        (frac(limits["red"]), frac(limits["magenta"]), "var(--lv2)"),
        (frac(limits["magenta"]), 1.0, "var(--lv3)"),
    ]
    parts = [
        f'<path d="{_arc(cx, cy, radius, 0.0005, 0.9995)}" fill="none" '
        'style="stroke:var(--border)" stroke-width="15" stroke-linecap="butt"/>'
    ]
    for f0, f1, color in zones:
        if f1 - f0 > 0.001:
            parts.append(
                f'<path d="{_arc(cx, cy, radius, f0, min(f1, 0.9995))}" fill="none" '
                f'style="stroke:{color}" stroke-width="11" stroke-linecap="butt"/>'
            )

    if bad or value is None or not np.isfinite(float(value)):
        needle = ""
        shown = "-"
    else:
        angle = math.pi * (1.0 - frac(value))
        nx, ny = cx + (radius - 8) * math.cos(angle), cy - (radius - 8) * math.sin(angle)
        needle = (
            f'<line x1="{cx}" y1="{cy}" x2="{nx:.2f}" y2="{ny:.2f}" '
            'style="stroke:var(--text)" stroke-width="3.5" stroke-linecap="round"/>'
        )
        shown = _num(value, decimals)

    marker = ""
    if ap_value is not None and np.isfinite(float(ap_value)) and not bad:
        a_angle = math.pi * (1.0 - frac(ap_value))
        mx, my = cx + (radius + 1) * math.cos(a_angle), cy - (radius + 1) * math.sin(a_angle)
        marker = (
            f'<circle cx="{mx:.2f}" cy="{my:.2f}" r="5.5" style="fill:var(--accent);'
            'stroke:var(--surface)" stroke-width="2"/>'
        )

    svg = (
        '<svg viewBox="0 0 200 118" xmlns="http://www.w3.org/2000/svg" role="img">'
        + "".join(parts) + needle + marker
        + f'<circle cx="{cx}" cy="{cy}" r="6" style="fill:var(--text)"/>'
        + '<text x="16" y="115" font-size="10" style="fill:var(--muted)" text-anchor="middle">0</text>'
        + f'<text x="184" y="115" font-size="10" style="fill:var(--muted)" text-anchor="middle">{vmax:,.{decimals if vmax < 100 else 0}f}</text>'
        + "</svg>"
    )
    chip_text = "SENSOR ERROR" if bad else LEVELS[level]["name"]
    return (
        f'<div class="gauge {css}"><div class="g-label">{name}</div>{svg}'
        f'<div class="g-value">{shown}<span class="g-unit">{UNITS[parameter]}</span></div>'
        f'<div class="chip {css}">{chip_text}</div>'
        f'<div class="g-sub">{rate_text}</div></div>'
    )


def strip_html(parameter, raw_values, ap_values, limits, css):
    """Grafik mini (SVG) dari sampel terbaru; garis tipis = mentah, garis tebal = auto pilot."""
    name = SHORT_NAME[parameter]
    decimals = DECIMALS[parameter]
    raw_values = np.asarray(raw_values, dtype=float)
    finite = raw_values[np.isfinite(raw_values)]
    last = _num(raw_values[-1] if len(raw_values) else None, decimals)
    if len(finite) < 2:
        return f'<div class="strip {css}"><div class="s-head"><span class="s-name">{name}</span><span class="s-val">{last}</span></div></div>'

    ap_arr = None if ap_values is None else np.asarray(ap_values, dtype=float)
    pool = finite if ap_arr is None else np.concatenate([finite, ap_arr[np.isfinite(ap_arr)]])
    vmin, vmax = float(pool.min()), float(pool.max())
    pad = max((vmax - vmin) * 0.12, abs(vmax) * 0.01, 1e-6)
    vmin, vmax = vmin - pad, vmax + pad
    width, height = 200.0, 72.0
    n = len(raw_values)

    def point(i, v):
        x = width * i / max(n - 1, 1)
        y = height - (float(v) - vmin) / (vmax - vmin) * (height - 6.0) - 3.0
        return f"{x:.1f},{y:.1f}"

    def polyline(values, stroke, stroke_width, opacity):
        pts = [point(i, v) for i, v in enumerate(values) if np.isfinite(v)]
        if len(pts) < 2:
            return ""
        return (
            f'<polyline points="{" ".join(pts)}" fill="none" style="stroke:{stroke}" '
            f'stroke-width="{stroke_width}" stroke-opacity="{opacity}" '
            'stroke-linejoin="round" vector-effect="non-scaling-stroke"/>'
        )

    lines = []
    for key, color in (("yellow", "var(--lv1)"), ("red", "var(--lv2)"), ("magenta", "var(--lv3)")):
        v = float(limits[key])
        if vmin < v < vmax:
            y = height - (v - vmin) / (vmax - vmin) * (height - 6.0) - 3.0
            lines.append(
                f'<line x1="0" y1="{y:.1f}" x2="{width}" y2="{y:.1f}" style="stroke:{color}" '
                'stroke-width="1" stroke-dasharray="4 3" vector-effect="non-scaling-stroke"/>'
            )
    body = "".join(lines) + polyline(raw_values, "var(--muted)", 1.2, 0.8)
    if ap_arr is not None:
        body += polyline(ap_arr, "var(--accent)", 2.2, 1)
    svg = f'<svg viewBox="0 0 {width:.0f} {height:.0f}" preserveAspectRatio="none">{body}</svg>'
    return (
        f'<div class="strip {css}"><div class="s-head"><span class="s-name">{name}</span>'
        f'<span class="s-val">{last} {UNITS[parameter]}</span></div>{svg}</div>'
    )


def lamp_html(label, state_css, text="", blink=False):
    """Lampu annunciator. state_css: 'ok', 'idle', atau 'on lvX'."""
    extra = " blink" if blink else ""
    sub = f"<small>{html.escape(text)}</small>" if text else ""
    return f'<div class="lamp {state_css}{extra}">{label}{sub}</div>'


def stat_html(label, value, unit="", progress=None):
    bar = ""
    if progress is not None:
        bar = f'<div class="bar"><div style="width:{min(max(progress, 0.0), 100.0):.1f}%"></div></div>'
    unit_html = f'<span class="u">{unit}</span>' if unit and value != "-" else ""
    return f'<div class="cabin-stat"><div class="k">{label}</div><div class="v">{value}{unit_html}</div>{bar}</div>'


def ap_card_html(label, value, unit, css, chip, extra=""):
    extra_html = f'<div class="card-rate">{extra}</div>' if extra else ""
    return (
        f'<div class="card {css}"><div class="card-label">{label}</div>'
        f'<div class="card-value">{value}<span class="card-unit">{unit}</span></div>'
        f'<div class="chip {css}">{chip}</div>{extra_html}</div>'
    )


def brake_style(brake):
    if brake >= 0.70:
        return "lv3", "Kuat"
    if brake >= 0.40:
        return "lv2", "Sedang"
    if brake >= 0.10:
        return "lv1", "Ringan"
    return "lv0", "Tidak aktif"


def _mean_abs_change(values):
    values = np.asarray(values, dtype=float)
    return float(np.mean(np.abs(np.diff(values)))) if len(values) > 1 else 0.0


# ==========================================================
# HALAMAN
# ==========================================================
THEME_NAME = THEME or "dark"
st.markdown(build_css(THEME), unsafe_allow_html=True)

with st.sidebar:
    st.markdown('<div class="side-title">EWS Telemetry</div>', unsafe_allow_html=True)
    st.markdown(f"Pengguna: **{current_user}**")
    st.markdown(
        f'<span class="tag" style="margin-left:0">{"ADMIN" if is_admin else "VIEWER"}</span>',
        unsafe_allow_html=True,
    )
    st.caption(
        "Dapat mengubah pengaturan." if is_admin else
        "Hanya melihat" + (" dan ACK alarm." if "viewer" in ACK_ROLES else ".")
    )
    if call_wide(st.button, "Keluar / Logout"):
        audit_log(current_user, "LOGOUT", "")
        st.session_state["authenticated"] = False
        st.session_state.pop("authenticated_user", None)
        st.session_state.pop("role", None)
        st.rerun()

st.markdown(_h(f"""
<div class="ews-header">
  <div>
    <div class="ews-title">EWS Telemetry Monitoring</div>
    <div class="ews-sub">Early Warning System - Drilling Operations</div>
  </div>
  <div>
    <span class="tag sim">Simulasi / playback</span>
    <span class="tag">{"Admin" if is_admin else "Viewer"}</span>
  </div>
</div>"""), unsafe_allow_html=True)
st.markdown(
    notice(
        "<b>Simulasi dan playback data.</b> Tidak terhubung ke rig atau Amphion PDSI. "
        "Ambang hanya untuk pengujian perangkat lunak, bukan setpoint operasi.",
        "warn",
    ),
    unsafe_allow_html=True,
)


def try_load(path):
    """Muat dataset sintetis; (None, None) jika file tidak ada, (None, pesan) jika rusak."""
    try:
        return load_data(path), None
    except FileNotFoundError:
        return None, None
    except Exception as error:
        return None, f"Gagal membaca {path}: {error}"


data, load_error_1 = try_load(DATA_FILE)
ap_data, load_error_2 = try_load(AUTOPILOT_FILE)
try:
    field_data = load_field_data(FIELD_FILE)
    load_error_3 = None
except FileNotFoundError:
    field_data, load_error_3 = None, None
except Exception as error:
    field_data, load_error_3 = None, f"Gagal membaca {FIELD_FILE}: {error}"
for load_error in (load_error_1, load_error_2, load_error_3):
    if load_error:
        st.error(load_error)
        st.stop()
if data is None and ap_data is None and field_data is None:
    st.error(
        f"File dataset tidak ditemukan. Letakkan {FIELD_FILE} atau {AUTOPILOT_FILE} "
        "di folder yang sama dengan program ini."
    )
    st.stop()
if data is None:
    data = ap_data if ap_data is not None else field_data  # fallback bila dataset lama tidak ada

# Refresh otomatis. Interval menyesuaikan lama proses agar putaran tidak saling menumpuk
# (penyebab elemen hilang-muncul di versi sebelumnya).
last_run = float(st.session_state.get("_run_secs", 0.3))
refresh_ms = 1000 if last_run < 0.6 else 2000 if last_run < 1.3 else 3000
st_autorefresh(interval=refresh_ms, key="ews_refresh")

# Ambil nilai parameter bersama terbaru.
with shared["lock"]:
    current = copy.deepcopy(shared["params"])
    updated_at = shared["updated_at"]
    updated_by = shared["updated_by"]

# Samakan nilai widget dengan nilai bersama SEBELUM widget dibuat.
st.session_state["w_scenario"] = current["scenario"]
st.session_state["w_interval"] = current["interval"]
st.session_state["w_batch_size"] = current["batch_size"]
st.session_state["w_auto"] = current["auto"]
st.session_state["w_alarm_enabled"] = current["alarm_enabled"]
st.session_state["w_repeat_alarm"] = current["repeat_alarm"]
st.session_state["w_loop_data"] = current["loop_data"]
st.session_state["w_roc_enabled"] = current["roc_enabled"]
st.session_state["w_roc_window"] = current["roc_window"]
st.session_state["w_debounce"] = int(current["debounce_samples"])
st.session_state["w_hyst"] = float(current["hysteresis_pct"])
st.session_state["w_stale_after"] = current["stale_after"]
st.session_state["w_test_outage"] = current["test_outage"]
st.session_state["w_test_sensor"] = current["test_faulty_sensor"]
st.session_state["w_wa_enabled"] = current["wa_enabled"]
st.session_state["w_wa_min_level"] = current["wa_min_level"]
st.session_state["w_autopilot"] = current["autopilot"]
st.session_state["w_ap_wob"] = float(current["ap_wob_sp"])
st.session_state["w_ap_rpm"] = float(current["ap_rpm_sp"])
st.session_state["w_ap_rop"] = float(current["ap_rop_target"])
st.session_state["w_ap_med"] = float(current["ap_rise_med_pct"])
st.session_state["w_ap_high"] = float(current["ap_rise_high_pct"])

if current["scenario"] == SCENARIOS[3] and field_data is None:
    st.error(f"File data lapangan {FIELD_FILE} tidak ditemukan di folder aplikasi.")
    st.stop()

locked = not is_admin


def inline_warning(condition, text):
    """Peringatan di slot tetap: jumlah elemen sidebar tidak berubah-ubah."""
    slot = st.empty()
    if condition:
        slot.error(text)


# ---------------------------- SIDEBAR ----------------------------
with st.sidebar:
    st.markdown('<div class="side-title">Kontrol simulasi</div>', unsafe_allow_html=True)
    if is_admin:
        st.caption("Perubahan di sini berlaku untuk semua device.")
    else:
        st.markdown(
            notice("Pengaturan terkunci. Hanya admin yang dapat mengubahnya."),
            unsafe_allow_html=True,
        )

    st.selectbox(
        "Skenario", SCENARIOS, key="w_scenario", disabled=locked,
        on_change=update_param, args=("scenario", "w_scenario"),
    )
    st.selectbox(
        "Interval pembaruan", INTERVAL_OPTIONS, key="w_interval", disabled=locked,
        format_func=lambda x: f"{x} detik",
        on_change=update_param, args=("interval", "w_interval"),
    )
    st.selectbox(
        "Baris per pembaruan", BATCH_OPTIONS, key="w_batch_size", disabled=locked,
        on_change=update_param, args=("batch_size", "w_batch_size"),
    )
    st.toggle(
        "Jalankan simulasi otomatis", key="w_auto", disabled=locked,
        on_change=update_param, args=("auto", "w_auto"),
    )
    st.toggle(
        "Mode auto pilot (Fuzzy-PID)", key="w_autopilot", disabled=locked,
        on_change=update_param, args=("autopilot", "w_autopilot"),
    )
    st.toggle(
        "Aktifkan alarm suara (semua device)", key="w_alarm_enabled", disabled=locked,
        on_change=update_param, args=("alarm_enabled", "w_alarm_enabled"),
    )
    st.toggle(
        "Ulangi alarm magenta", key="w_repeat_alarm", disabled=locked,
        on_change=update_param, args=("repeat_alarm", "w_repeat_alarm"),
    )
    st.checkbox(
        "Ulangi simulasi dari awal", key="w_loop_data", disabled=locked,
        on_change=update_param, args=("loop_data", "w_loop_data"),
    )

    st.markdown(
        '<div class="side-title" style="margin-top:14px">Suara dan getar (perangkat ini)</div>',
        unsafe_allow_html=True,
    )
    components.html(controller_html(PAL), height=100)

    with st.expander("Pengaturan auto pilot (Fuzzy-PID)"):
        st.caption(
            "Setpoint yang dipertahankan pengendali. Pengereman putaran aktif saat "
            "torsi dan standpipe pressure naik bersamaan melewati persentase di bawah "
            "ini terhadap baseline."
        )
        st.number_input(
            "Setpoint WOB (kN)", min_value=1.0, max_value=100.0, step=0.5,
            format="%g", key="w_ap_wob", disabled=locked,
            on_change=update_param, args=("ap_wob_sp", "w_ap_wob"),
        )
        st.number_input(
            "Setpoint RPM", min_value=1.0, max_value=200.0, step=0.5,
            format="%g", key="w_ap_rpm", disabled=locked,
            on_change=update_param, args=("ap_rpm_sp", "w_ap_rpm"),
        )
        st.number_input(
            "Target ROP (m/h)", min_value=0.5, max_value=100.0, step=0.5,
            format="%g", key="w_ap_rop", disabled=locked,
            on_change=update_param, args=("ap_rop_target", "w_ap_rop"),
        )
        st.number_input(
            "Kenaikan sedang (%)", min_value=5.0, max_value=50.0, step=1.0,
            format="%g", key="w_ap_med", disabled=locked,
            on_change=update_param, args=("ap_rise_med_pct", "w_ap_med"),
        )
        st.number_input(
            "Kenaikan tinggi (%)", min_value=10.0, max_value=100.0, step=1.0,
            format="%g", key="w_ap_high", disabled=locked,
            on_change=update_param, args=("ap_rise_high_pct", "w_ap_high"),
        )
        inline_warning(
            current["ap_rise_med_pct"] >= current["ap_rise_high_pct"],
            "Kenaikan sedang harus lebih kecil dari kenaikan tinggi",
        )

    with st.expander("Ambang alarm (demonstrasi)"):
        for parameter in PARAMETERS:
            st.markdown(f"**{DISPLAY_NAME.get(parameter, parameter)}** ({UNITS[parameter]})")
            cols = st.columns(3)
            for col, (level_name, label) in zip(cols, LEVEL_KEYS):
                key = f"w_th_{parameter}_{level_name}"
                st.session_state[key] = float(current["thresholds"][parameter][level_name])
                col.number_input(
                    label, key=key, disabled=locked,
                    step=THRESHOLD_STEP[parameter], format="%g",
                    on_change=update_threshold, args=(parameter, level_name, key),
                )
            limits = current["thresholds"][parameter]
            inline_warning(
                not (limits["yellow"] < limits["red"] < limits["magenta"]),
                f"{parameter}: urutan harus Kuning < Merah < Magenta",
            )

    with st.expander("Penahan alarm dan laju perubahan"):
        st.caption(
            "Debounce: level baru naik hanya jika ambang terlampaui beberapa "
            "pembacaan berturut-turut. Hysteresis: level baru turun jika nilai "
            "sudah di bawah ambang dikurangi persentase ini."
        )
        st.number_input(
            "Debounce (pembacaan berturut-turut)", min_value=1, max_value=10,
            step=1, key="w_debounce", disabled=locked,
            on_change=update_param, args=("debounce_samples", "w_debounce"),
        )
        st.number_input(
            "Hysteresis (%)", min_value=0.0, max_value=30.0, step=1.0,
            format="%g", key="w_hyst", disabled=locked,
            on_change=update_param, args=("hysteresis_pct", "w_hyst"),
        )
        st.divider()
        st.toggle(
            "Alarm laju perubahan", key="w_roc_enabled", disabled=locked,
            on_change=update_param, args=("roc_enabled", "w_roc_enabled"),
        )
        st.selectbox(
            "Jendela perhitungan laju", ROC_WINDOW_OPTIONS, key="w_roc_window",
            disabled=locked, format_func=lambda x: f"{x} pembacaan terakhir",
            on_change=update_param, args=("roc_window", "w_roc_window"),
        )
        st.caption("Ambang laju = perubahan per MENIT (naik atau turun).")
        for parameter in PARAMETERS:
            st.markdown(f"**{DISPLAY_NAME.get(parameter, parameter)}** ({UNITS[parameter]}/mnt)")
            cols = st.columns(2)
            for col, (level_name, label) in zip(cols, ROC_LEVEL_KEYS):
                key = f"w_roc_{parameter}_{level_name}"
                st.session_state[key] = float(current["roc_thresholds"][parameter][level_name])
                col.number_input(
                    label, key=key, disabled=locked,
                    step=ROC_STEP[parameter], format="%g",
                    on_change=update_roc_threshold, args=(parameter, level_name, key),
                )
            limits = current["roc_thresholds"][parameter]
            inline_warning(
                not limits["yellow"] < limits["red"],
                f"{parameter}: laju Kuning harus lebih kecil dari Merah",
            )

    with st.expander("Gangguan data dan uji"):
        st.selectbox(
            "Data dianggap terlambat setelah", STALE_OPTIONS, key="w_stale_after",
            disabled=locked, format_func=lambda x: f"{x} detik",
            on_change=update_param, args=("stale_after", "w_stale_after"),
        )
        st.caption("Batas efektif otomatis minimal 2x interval + 1 detik.")
        st.toggle(
            "Uji: putuskan aliran data", key="w_test_outage", disabled=locked,
            on_change=update_param, args=("test_outage", "w_test_outage"),
        )
        st.selectbox(
            "Uji: matikan sensor", [NO_FAULTY_SENSOR] + PARAMETERS,
            key="w_test_sensor", disabled=locked,
            on_change=update_param, args=("test_faulty_sensor", "w_test_sensor"),
        )
        st.caption("Fitur uji hanya untuk memeriksa peringatan gangguan data.")

    with st.expander("Notifikasi WhatsApp"):
        wa_ready = get_wa_config() is not None
        st.caption(
            "Kredensial: "
            + ("terdeteksi di secrets" if wa_ready else "belum diisi di secrets")
        )
        st.toggle(
            "Kirim notifikasi WhatsApp", key="w_wa_enabled", disabled=locked,
            on_change=update_param, args=("wa_enabled", "w_wa_enabled"),
        )
        st.selectbox(
            "Kirim mulai level", [1, 2, 3], key="w_wa_min_level", disabled=locked,
            format_func=lambda x: LEVELS[x]["name"],
            on_change=update_param, args=("wa_min_level", "w_wa_min_level"),
        )
        st.caption(
            "Gangguan data dan ACK alarm juga dikirim. Batas 1 pesan per "
            f"{WA_COOLDOWN_SECONDS} detik untuk tiap jenis pesan."
        )
        call_wide(
            st.button, "Kirim pesan tes",
            disabled=locked or not wa_ready, on_click=send_wa_test,
        )

    call_wide(
        st.button, "Terapkan preset ambang skenario ini",
        disabled=locked, on_click=apply_scenario_preset, key="btn_preset",
    )
    call_wide(
        st.button, "Kembalikan semua parameter ke default",
        disabled=locked, on_click=reset_params_to_default, key="btn_default",
    )
    st.caption(
        "Terakhir diubah: "
        + (f"{updated_at.strftime('%d-%m-%Y %H:%M:%S')} WIB oleh {updated_by}" if updated_at else "-")
    )

    st.divider()

    call_wide(st.button, "Reset simulasi", disabled=locked, on_click=reset_simulation, key="btn_reset")
    call_wide(st.button, "Lompat ke data terakhir", disabled=locked, on_click=jump_to_last, key="btn_jump")

# ---------------------------- KERANGKA HALAMAN ----------------------------
# Semua bagian dibuat SELALU dengan urutan tetap. Isinya boleh berubah tiap detik,
# tetapi jumlah elemen tingkat atas tidak, sehingga grafik dan tabel tidak dibuat ulang.
view = st.radio(
    "Tampilan", ["Kabin Driller", "Dasbor EWS"], horizontal=True,
    key="view_mode", label_visibility="collapsed",
)
cabin_view = view == "Kabin Driller"

sec_status = st.container()
sec_ack = st.container()
sec_alerts = st.container()
sec_cabin = st.container()
sec_cards = st.container()
sec_ap = st.container()
sec_legend = st.container()
sec_trend = st.container()
sec_events = st.container()
sec_audit = st.container()
sec_notes = st.container()
sec_players = st.container()

# ---------------------------- PROSES SIMULASI ----------------------------
simulation_data, snap = process_simulation(data, ap_data, field_data)

params = snap["params"]
THRESHOLDS = params["thresholds"]
scenario = params["scenario"]
events_list = snap["events"]
level = snap["level"]
signal_levels = snap["signal_levels"]
rates = snap["rates"]
bad_sensors = snap["bad_sensors"]
ap_view = snap["ap"]
ap_on = snap["ap_on"]

n = len(simulation_data)
index = snap["index"]
visible = simulation_data.iloc[:index + 1]
latest = visible.iloc[-1]
has_depth = "Depth_m" in simulation_data.columns and bool(np.isfinite(float(latest.get("Depth_m", np.nan))))
info = LEVELS[level]

messages = {
    0: "Semua sinyal berada di bawah ambang kuning.",
    1: "Waspada: setidaknya satu sinyal mencapai ambang kuning.",
    2: "Bahaya simulasi: setidaknya satu sinyal mencapai ambang merah.",
    3: (
        "Kritis simulasi: setidaknya satu sinyal mencapai ambang magenta. "
        "Ini bukan perintah operasi rig nyata."
    ),
}

# ---------------------------- STATUS ----------------------------
with sec_status:
    if snap["stale"]:
        panel_class = "lverr"
        panel_label = "DATA TERLAMBAT"
        panel_message = (
            f"Tidak ada data baru selama {snap['stale_age']:.0f} detik "
            f"(batas {snap['stale_limit']:.0f} detik). "
            "Status EWS tidak dapat dipercaya."
        )
    else:
        panel_class = f"lv{level}"
        panel_label = info["name"]
        panel_message = messages[level]

    sample_time = latest["timestamp"].strftime("%Y-%m-%d %H:%M:%S UTC")
    depth_text = f"<span>Kedalaman: {_num(latest.get('Depth_m'), 2)} m</span>" if has_depth else ""
    progress_pct = (index + 1) / n * 100
    st.markdown(_h(f"""
    <div class="status-panel {panel_class}">
      <div class="status-kicker">Status EWS simulasi</div>
      <div class="status-title">{panel_label}</div>
      <div class="status-msg">{html.escape(panel_message)}</div>
      <div class="status-meta">
        {depth_text}
        <span>Sampel: {sample_time}</span>
        <span>Mode: {html.escape(scenario)}</span>
        <span>Data: {index + 1} / {n}</span>
      </div>
      <div class="status-progress"><div style="width:{progress_pct:.1f}%"></div></div>
    </div>"""), unsafe_allow_html=True)

# ---------------------------- ACK ----------------------------
alarm_active = level > 0 or bool(snap["fault_keys"])
with sec_ack:
    if alarm_active:
        if snap["ack_required"]:
            st.markdown(
                _h('<div class="ack-banner ack-pending">ALARM BELUM DIAKUI. Tekan ACK untuk '
                   "menghentikan bunyi dan getar di semua perangkat.</div>"),
                unsafe_allow_html=True,
            )
            call_wide(
                st.button, "ACK / AKUI ALARM", key="btn_ack", type="primary",
                on_click=ack_alarm, disabled=role not in ACK_ROLES,
            )
            if role not in ACK_ROLES:
                st.caption("Peran Anda tidak diizinkan melakukan ACK.")
        elif snap["ack"]:
            ack_info = snap["ack"]
            ack_time = datetime.fromtimestamp(ack_info["time"], WIB).strftime("%H:%M:%S")
            st.markdown(
                _h(f'<div class="ack-banner ack-done">Alarm diakui oleh '
                   f"<b>{html.escape(str(ack_info['by']))}</b> pukul {ack_time} WIB</div>"),
                unsafe_allow_html=True,
            )

# ---------------------------- PERINGATAN DATA ----------------------------
with sec_alerts:
    slot_field = st.empty()
    slot_sensor = st.empty()
    slot_paused = st.empty()
    slot_missing = st.empty()
    if scenario == SCENARIOS[3] and field_data is not None:
        d_first = _num(simulation_data["Depth_m"].iloc[0], 1)
        d_last = _num(simulation_data["Depth_m"].iloc[-1], 1)
        slot_field.markdown(
            notice(
                f"<b>Data lapangan NEAC-001:</b> {n} baris, kedalaman {d_first} sampai {d_last} m "
                f"(dipangkas dari 1815 baris asli sampai {FIELD_MAX_DEPTH_M:.0f} m). Satuan dikonversi: "
                "klbf ke kN, lbf.ft ke kN.m, min/m ke m/h. Timestamp adalah indeks playback sintetis "
                "(1 menit per baris), bukan waktu akuisisi lapangan.",
            ),
            unsafe_allow_html=True,
        )
    if bad_sensors:
        slot_sensor.markdown(
            notice(
                "<b>Sensor tidak valid:</b> " + html.escape(", ".join(bad_sensors))
                + ". Nilai kosong atau di luar rentang; sensor ini tidak ikut dievaluasi.",
                "err",
            ),
            unsafe_allow_html=True,
        )
    if snap["paused"]:
        slot_paused.markdown(
            notice("Simulasi sedang dijeda. Data tidak diperbarui."),
            unsafe_allow_html=True,
        )
    if scenario == SCENARIOS[2] and ap_data is None:
        slot_missing.markdown(
            notice(f"File <b>{AUTOPILOT_FILE}</b> tidak ditemukan di repo; dashboard memakai dataset lama.", "err"),
            unsafe_allow_html=True,
        )

# ==========================================================
# TAMPILAN KABIN DRILLER
# ==========================================================
if cabin_view:
    ap_last = None
    if ap_view:
        hist_c = ap_view["hist"]
        ap_last = {name: hist_c[f"ap_{name}"][-1] for name in AP_CHANNEL_LIST}

    with sec_cabin:
        slot_stats = st.empty()
        slot_gauges = st.empty()
        slot_lamps = st.empty()
        controls = st.container()
        slot_ap = st.empty()
        slot_strips = st.empty()

        # --- bilah status: kedalaman, hook load, RPM total, posisi data ---
        if has_depth:
            d_start = float(simulation_data["Depth_m"].iloc[0])
            d_end = float(simulation_data["Depth_m"].iloc[-1])
            d_now = float(latest["Depth_m"])
            depth_progress = (d_now - d_start) / max(d_end - d_start, 1e-9) * 100.0
        else:
            depth_progress = None
        stats = [
            stat_html("Kedalaman TMD", _num(latest.get("Depth_m"), 2), "m", depth_progress),
            stat_html("Kedalaman TVD", _num(latest.get("TVD_m"), 2), "m"),
            stat_html("Hook load (WOH)", _num(latest.get("WOH"), 1), "kN"),
            stat_html("RPM total", _num(latest.get("RPM_total"), 1), "rpm"),
            stat_html("Data diputar", f"{index + 1} / {n}", "", (index + 1) / n * 100.0),
        ]
        slot_stats.markdown(_h('<div class="cabin-bar">' + "".join(stats) + "</div>"), unsafe_allow_html=True)

        # --- gauge utama ---
        gauges = []
        for parameter in ["WOB", "ROP", "RPM", "Torque", "StandpipePressure"]:
            bad = parameter in bad_sensors
            rate_text = f"Laju {rates[parameter]:+.1f} per menit" if parameter in rates else "&nbsp;"
            gauges.append(
                gauge_html(
                    parameter,
                    None if bad else float(latest[parameter]),
                    THRESHOLDS[parameter],
                    signal_levels.get(parameter, 0),
                    bad,
                    rate_text,
                    ap_last[parameter] if (ap_last and ap_on) else None,
                )
            )
        slot_gauges.markdown(_h('<div class="cabin-gauges">' + "".join(gauges) + "</div>"), unsafe_allow_html=True)

        # --- annunciator ---
        brake_now = float(ap_view["brake"]) if ap_view else 0.0
        brake_css, brake_text = brake_style(brake_now)
        lamps = []
        for parameter in ["WOB", "ROP", "RPM", "Torque", "StandpipePressure"]:
            if parameter in bad_sensors:
                lamps.append(lamp_html(SHORT_NAME[parameter], "on lverr", "Sensor"))
            else:
                lv = signal_levels.get(parameter, 0)
                lamps.append(
                    lamp_html(SHORT_NAME[parameter], f"on lv{lv}", LEVELS[lv]["name"]) if lv > 0
                    else lamp_html(SHORT_NAME[parameter], "ok", "Normal")
                )
        roc_level = max((v for k, v in signal_levels.items() if k.endswith(ROC_SUFFIX)), default=0)
        lamps.append(
            lamp_html("Laju", f"on lv{roc_level}", LEVELS[roc_level]["name"]) if roc_level > 0
            else lamp_html("Laju", "ok", "Normal")
        )
        data_fault = snap["stale"] or bool(bad_sensors)
        lamps.append(
            lamp_html("Data", "on lverr", "Terlambat" if snap["stale"] else "Sensor") if data_fault
            else lamp_html("Data", "ok", "Normal")
        )
        lamps.append(lamp_html("Auto pilot", "ok" if ap_on else "idle", "Aktif" if ap_on else "Nonaktif"))
        lamps.append(
            lamp_html("Rem putaran", f"on {brake_css}", f"{brake_now * 100:.0f}%") if brake_now >= 0.10
            else lamp_html("Rem putaran", "ok" if ap_on else "idle", "Tidak aktif")
        )
        lamps.append(
            lamp_html("ACK", "on lv2", "Belum diakui", blink=True) if snap["ack_required"]
            else lamp_html("ACK", "ok", "Aman")
        )
        slot_lamps.markdown(_h('<div class="annun">' + "".join(lamps) + "</div>"), unsafe_allow_html=True)

        # --- tombol kendali kabin (admin) ---
        with controls:
            cols = st.columns(3)
            call_wide(
                cols[0].button,
                "Jeda simulasi" if params["auto"] else "Lanjutkan simulasi",
                key="cab_play", disabled=locked, on_click=toggle_play,
            )
            call_wide(
                cols[1].button,
                "Matikan auto pilot" if ap_on else "Aktifkan auto pilot",
                key="cab_ap", disabled=locked, on_click=toggle_autopilot,
            )
            call_wide(
                cols[2].button, "Reset simulasi",
                key="cab_reset", disabled=locked, on_click=reset_simulation,
            )

        # --- panel auto pilot ---
        if ap_view:
            hist = ap_view["hist"]
            
            def series(key):
                return np.array(hist[key], dtype=float)
            
            recent = slice(-30, None)
            rop_ap = float(np.mean(series("ap_ROP")[recent]))
            rop_raw = float(np.mean(series("raw_ROP")[recent]))
            rop_gain = (rop_ap / rop_raw - 1.0) * 100.0 if rop_raw > 0 else 0.0
            boost = float(ap_view["boost"])
            cards_c = [
                ap_card_html("Perintah WOB", _num(hist["wob_cmd"][-1], 1), "kN",
                             "lv1" if boost >= 0.01 else "lv0",
                             f"Booster +{boost * 100:.0f}%" if boost >= 0.01 else "Stabil"),
                ap_card_html("Perintah RPM", _num(hist["rpm_cmd"][-1], 1), "rpm",
                             brake_css, "Direm" if brake_now >= 0.10 else "Normal"),
                ap_card_html("Pengereman", f"{brake_now * 100:.0f}", "%", brake_css, brake_text),
                ap_card_html("ROP auto pilot", _num(rop_ap, 1), "m/h", "lv0",
                             "Rata-rata 30 langkah", f"Mentah {rop_raw:.1f}, selisih {rop_gain:+.0f}%"),
            ]
            
            extra = ""
            if brake_now >= 0.10:
                extra = notice(
                    f"<b>Pengereman putaran aktif ({brake_now * 100:.0f}%).</b> Torsi "
                    f"{ap_view['rise_t']:+.0f}% dan tekanan {ap_view['rise_p']:+.0f}% terhadap baseline. "
                    "RPM dan WOB diturunkan otomatis.",
                    "err" if brake_now >= 0.40 else "warn",
                )
            
            # --- TAMBAHAN: Grafik Auto Pilot di Kabin Driller ---
            nominal = {
                "ROP": float(params["ap_rop_target"]),
                "WOB": float(params["ap_wob_sp"]),
                "RPM": float(params["ap_rpm_sp"]),
                "Torque": float(ap_view["nom"]["Torque"]),
                "StandpipePressure": float(ap_view["nom"]["StandpipePressure"]),
            }
            depth_arr = series("depth")
            ap_x_label = "Kedalaman TMD (m)" if np.all(np.isfinite(depth_arr)) else "Waktu"
            ap_key = (
                int(ap_view["uid"]), len(hist["t"]), int(hist["idx"][-1]),
                True, ap_x_label, # Tampilkan raw data (True) secara default di kabin
            )
            
            # Buat Container khusus grafik agar layout kabin tidak berantakan
            ap_chart_html = _h('<div class="section-title">Visualisasi Setpoint Fuzzy-PID vs Aktual</div>')
            slot_ap.markdown(
                _h('<div class="section-title">Kinerja Auto Pilot Fuzzy-PID</div><div class="ap-panel">'
                   + "".join(cards_c) + "</div>" + extra + ap_chart_html),
                unsafe_allow_html=True,
            )
            
            # Render dan tampilkan grafik
            png = render_ap_png(ap_key, THEME_NAME, True, nominal, ap_x_label, hist)
            show_image(slot_ap, png)
            
        else:
            slot_ap.markdown(
                _h('<div class="section-title">Auto pilot Fuzzy-PID</div>')
                + notice("Mode auto pilot nonaktif. Admin dapat mengaktifkannya dengan tombol di atas atau di sidebar."),
                unsafe_allow_html=True,
            )

        # --- grafik mini tren (SVG) ---
        tail = visible.tail(STRIP_POINTS)
        ap_lookup = {}
        if ap_view:
            positions = ap_view["hist"]["idx"]
            for name in AP_CHANNEL_LIST:
                ap_lookup[name] = dict(zip(positions, ap_view["hist"][f"ap_{name}"]))
        strips = []
        for parameter in ["WOB", "ROP", "RPM", "Torque", "StandpipePressure"]:
            ap_series = None
            if ap_lookup:
                ap_series = [ap_lookup[parameter].get(i, np.nan) for i in tail.index]
            strips.append(
                strip_html(
                    parameter, tail[parameter].to_numpy(), ap_series, THRESHOLDS[parameter],
                    f"lv{signal_levels.get(parameter, 0)}",
                )
            )
        legend_line = (
            '<div class="legend-line">'
            '<span><i style="background:var(--muted)"></i>Data mentah rig</span>'
            '<span><i style="background:var(--accent)"></i>Hasil auto pilot</span>'
            '<span><i style="background:var(--lv1)"></i>Ambang kuning</span>'
            '<span><i style="background:var(--lv2)"></i>Ambang merah</span>'
            '<span><i style="background:var(--lv3)"></i>Ambang magenta</span></div>'
        )
        slot_strips.markdown(
            _h('<div class="section-title">Tren terbaru</div>' + legend_line
               + '<div class="strip-grid">' + "".join(strips) + "</div>"),
            unsafe_allow_html=True,
        )

# ==========================================================
# TAMPILAN DASBOR EWS
# ==========================================================
if not cabin_view:
    # ---- kartu parameter ----
    with sec_cards:
        cards = []
        for parameter in PARAMETERS:
            name = DISPLAY_NAME.get(parameter, parameter)
            if parameter in bad_sensors:
                css = "lverr"
                value_text = "-"
                chip = '<div class="chip lverr">Sensor error</div>'
            else:
                lv = signal_levels.get(parameter, 0)
                css = f"lv{lv}"
                value_text = _num(latest[parameter], DECIMALS[parameter] if parameter != "StandpipePressure" else 0)
                chip = f'<div class="chip lv{lv}">{LEVELS[lv]["name"]}</div>'
            rate_html = ""
            if parameter in rates:
                roc_lv = signal_levels.get(parameter + ROC_SUFFIX, 0)
                roc_chip = (
                    "" if roc_lv == 0
                    else f'<span class="chip lv{roc_lv}">{LEVELS[roc_lv]["name"]}</span>'
                )
                rate_html = f'<div class="card-rate">Laju {rates[parameter]:+.2f} per menit {roc_chip}</div>'
            cards.append(
                f'<div class="card {css}"><div class="card-label">{name}</div>'
                f'<div class="card-value">{value_text}<span class="card-unit">{UNITS[parameter]}</span></div>'
                f"{chip}{rate_html}</div>"
            )
        st.markdown(
            '<div class="section-title">Monitoring telemetri</div>'
            + _h('<div class="grid-cards">' + "".join(cards) + "</div>"),
            unsafe_allow_html=True,
        )

    # ---- auto pilot ----
    with sec_ap:
        st.markdown(
            '<div class="section-title">Auto pilot Fuzzy-PID (pengereman putaran)</div>',
            unsafe_allow_html=True,
        )
        slot_mode = st.empty()
        slot_cards = st.empty()
        slot_brake = st.empty()
        slot_check = st.empty()
        slot_chart = st.empty()
        slot_caption = st.empty()
        slot_kpi = st.empty()
        slot_gain = st.empty()

        show_raw = slot_check.checkbox(
            "Tampilkan data mentah rig (garis samar)", value=True, key="ap_show_raw"
        )
        if ap_on:
            slot_mode.markdown(
                notice(
                    "Mode auto pilot <b>AKTIF</b>. Data rig diakuisisi dan diproses pengendali "
                    "Fuzzy-PID; hasil yang telah dihaluskan tampil pada grafik di bawah."
                ),
                unsafe_allow_html=True,
            )
        else:
            slot_mode.markdown(
                notice(
                    "Mode auto pilot <b>nonaktif</b>. "
                    + ("Menampilkan hasil terakhir. " if ap_view else "")
                    + "Admin dapat mengaktifkannya melalui sidebar."
                ),
                unsafe_allow_html=True,
            )

        if ap_view:
            hist = ap_view["hist"]

            def series(key):
                return np.array(hist[key], dtype=float)

            brake = float(ap_view["brake"])
            boost = float(ap_view["boost"])
            brake_css, brake_text = brake_style(brake)
            recent = slice(-30, None)
            rop_ap = float(np.mean(series("ap_ROP")[recent]))
            rop_raw = float(np.mean(series("raw_ROP")[recent]))
            rop_gain = (rop_ap / rop_raw - 1.0) * 100.0 if rop_raw > 0 else 0.0

            ap_cards = [
                ap_card_html("Perintah WOB", _num(hist["wob_cmd"][-1], 1), "kN",
                             "lv1" if boost >= 0.01 else "lv0",
                             f"Booster +{boost * 100:.0f}%" if boost >= 0.01 else "Stabil"),
                ap_card_html("Perintah RPM", _num(hist["rpm_cmd"][-1], 1), "rpm",
                             brake_css, "Direm" if brake >= 0.10 else "Normal"),
                ap_card_html("Pengereman", f"{brake * 100:.0f}", "%", brake_css, brake_text),
                ap_card_html("ROP auto pilot", _num(rop_ap, 1), "m/h", "lv0",
                             "Rata-rata 30 langkah", f"Mentah {rop_raw:.1f}, selisih {rop_gain:+.0f}%"),
            ]
            slot_cards.markdown(
                _h('<div class="grid-cards four">' + "".join(ap_cards) + "</div>"),
                unsafe_allow_html=True,
            )
            if brake >= 0.10:
                slot_brake.markdown(
                    notice(
                        f"<b>Pengereman putaran aktif ({brake * 100:.0f}%).</b> Torsi "
                        f"{ap_view['rise_t']:+.0f}% dan standpipe pressure {ap_view['rise_p']:+.0f}% "
                        "terhadap baseline. RPM dan WOB diturunkan otomatis.",
                        "err" if brake >= 0.40 else "warn",
                    ),
                    unsafe_allow_html=True,
                )

            nominal = {
                "ROP": float(params["ap_rop_target"]),
                "WOB": float(params["ap_wob_sp"]),
                "RPM": float(params["ap_rpm_sp"]),
                "Torque": float(ap_view["nom"]["Torque"]),
                "StandpipePressure": float(ap_view["nom"]["StandpipePressure"]),
            }
            depth_arr = series("depth")
            ap_x_label = "Kedalaman TMD (m)" if np.all(np.isfinite(depth_arr)) else "Waktu"
            ap_key = (
                int(ap_view["uid"]), len(hist["t"]), int(hist["idx"][-1]),
                bool(show_raw), ap_x_label,
            )
            png = render_ap_png(ap_key, THEME_NAME, bool(show_raw), nominal, ap_x_label, hist)
            show_image(slot_chart, png)

            slot_caption.caption(
                "100% adalah nilai acuan: setpoint auto pilot untuk WOB, RPM, dan ROP, serta "
                "baseline awal untuk torsi dan standpipe pressure. Area merah menandai saat "
                "pengereman putaran aktif."
            )

            kpi_rows = [
                {"Indikator": "Fluktuasi WOB (rata-rata perubahan per langkah, kN)",
                 "Data mentah": f"{_mean_abs_change(series('raw_WOB')):.2f}",
                 "Auto pilot": f"{_mean_abs_change(series('ap_WOB')):.2f}"},
                {"Indikator": "Fluktuasi ROP (rata-rata perubahan per langkah, m/h)",
                 "Data mentah": f"{_mean_abs_change(series('raw_ROP')):.2f}",
                 "Auto pilot": f"{_mean_abs_change(series('ap_ROP')):.2f}"},
                {"Indikator": "ROP rata-rata (m/h)",
                 "Data mentah": f"{np.mean(series('raw_ROP')):.2f}",
                 "Auto pilot": f"{np.mean(series('ap_ROP')):.2f}"},
                {"Indikator": "Torsi puncak (kN.m)",
                 "Data mentah": f"{np.max(series('raw_Torque')):.2f}",
                 "Auto pilot": f"{np.max(series('ap_Torque')):.2f}"},
                {"Indikator": "Standpipe pressure puncak (psi)",
                 "Data mentah": f"{np.max(series('raw_StandpipePressure')):,.0f}",
                 "Auto pilot": f"{np.max(series('ap_StandpipePressure')):,.0f}"},
                {"Indikator": "RPM rata-rata",
                 "Data mentah": f"{np.mean(series('raw_RPM')):.1f}",
                 "Auto pilot": f"{np.mean(series('ap_RPM')):.1f}"},
                {"Indikator": "Langkah dengan pengereman aktif (>10%)",
                 "Data mentah": "-",
                 "Auto pilot": f"{int(np.sum(series('brake') > 0.10))}"},
            ]
            slot_kpi.markdown(
                '<div class="section-title">Ringkasan kinerja auto pilot</div>'
                + html_table(kpi_rows, ["Indikator", "Data mentah", "Auto pilot"])
                + _h(f'<div class="view-note">Dihitung dari {len(hist["t"])} sampel yang telah diproses sejak '
                     "mode auto pilot diaktifkan. Data mentah dan hasil auto pilot sama-sama berasal dari "
                     "simulasi/playback; hasil auto pilot adalah proyeksi model, bukan kontrol nyata atas rig.</div>"),
                unsafe_allow_html=True,
            )
            gain_rows = [
                {
                    "Kanal": f"{DISPLAY_NAME.get(name, name)} ({UNITS[name]})",
                    "Kp": f"{ap_view['gains'][name][0]:.3f}",
                    "Ki": f"{ap_view['gains'][name][1]:.4f}",
                    "Kd": f"{ap_view['gains'][name][2]:.3f}",
                }
                for name in AP_CHANNEL_LIST
            ]
            slot_gain.markdown(
                '<div class="section-title">Gain hasil tuning fuzzy saat ini</div>'
                + html_table(gain_rows, ["Kanal", "Kp", "Ki", "Kd"])
                + '<div class="view-note">Gain dasar tiap kanal disesuaikan logika fuzzy setiap langkah '
                  "berdasarkan besar galat dan laju perubahan galat.</div>",
                unsafe_allow_html=True,
            )

    # ---- legenda ----
    with sec_legend:
        legend_items = "".join(
            f'<div class="legend-item lv{number}"><b>{LEVELS[number]["name"]}</b>'
            f'<span>{LEVELS[number]["description"]}</span></div>'
            for number in range(4)
        )
        st.markdown(
            '<div class="section-title">Keterangan level</div>'
            + _h(f'<div class="grid-legend">{legend_items}</div>'),
            unsafe_allow_html=True,
        )

    # ---- grafik tren parameter ----
    with sec_trend:
        st.markdown('<div class="section-title">Grafik tren parameter</div>', unsafe_allow_html=True)
        st.caption(
            "Grafik menampilkan maksimal 120 sampel terbaru. Gambar dibuat satu kali per langkah "
            "data dan dipakai bersama semua perangkat, sehingga tidak berkedip."
        )
        selected = st.multiselect(
            "Pilih parameter yang ditampilkan (lebih sedikit grafik = lebih ringan)",
            PARAMETERS,
            default=["ROP", "StandpipePressure"],
            format_func=lambda x: DISPLAY_NAME.get(x, x),
            key="trend_selected",
        )
        trend_columns = st.columns(2)
        plot_data = visible.tail(120)
        use_depth = has_depth and "Depth_m" in plot_data.columns and bool(plot_data["Depth_m"].notna().all())
        x_values = plot_data["Depth_m"] if use_depth else plot_data["timestamp"]
        x_label = "Kedalaman TMD (m)" if use_depth else "Waktu"
        for position, parameter in enumerate(selected):
            png = render_trend_png(
                (parameter, int(index), scenario, len(plot_data), x_label),
                THEME_NAME, parameter, THRESHOLDS[parameter], x_label,
                x_values, plot_data[parameter],
            )
            show_image(trend_columns[position % 2], png)

# ==========================================================
# RIWAYAT, LOG AUDIT, CATATAN (kedua tampilan)
# ==========================================================
with sec_events:
    st.markdown('<div class="section-title">Riwayat eskalasi EWS</div>', unsafe_allow_html=True)
    slot_event_table = st.empty()
    slot_event_caption = st.empty()
    slot_event_download = st.empty()
    shown = 8 if cabin_view else 100
    if events_list:
        slot_event_table.markdown(
            html_table(events_list[:shown], ["Waktu", "Level", "Parameter"], chip_column="Level"),
            unsafe_allow_html=True,
        )
        slot_event_caption.caption(f"Menampilkan {min(len(events_list), shown)} dari {len(events_list)} catatan terbaru.")
        slot_event_download.download_button(
            "Unduh riwayat CSV",
            pd.DataFrame(events_list).to_csv(index=False).encode("utf-8-sig"),
            file_name="riwayat_ews.csv",
            mime="text/csv",
        )
    else:
        slot_event_table.markdown(notice("Belum ada eskalasi alarm yang tercatat."), unsafe_allow_html=True)

with sec_audit:
    if is_admin and not cabin_view:
        with st.expander("Log audit (khusus admin)"):
            slot_audit_table = st.empty()
            slot_audit_caption = st.empty()
            slot_audit_download = st.empty()
            with shared["lock"]:
                audit_rows = list(shared["audit"])
            if audit_rows:
                slot_audit_table.markdown(
                    html_table(audit_rows[:200], ["Waktu", "Pengguna", "Aksi", "Detail"]),
                    unsafe_allow_html=True,
                )
                slot_audit_caption.caption(
                    f"Menampilkan {min(len(audit_rows), 200)} entri terbaru dari {len(audit_rows)} "
                    "(maksimal 2000 disimpan di memori server)."
                )
                slot_audit_download.download_button(
                    "Unduh log audit CSV",
                    pd.DataFrame(audit_rows).to_csv(index=False).encode("utf-8-sig"),
                    file_name="log_audit_ews.csv",
                    mime="text/csv",
                )
            else:
                slot_audit_table.markdown(notice("Belum ada aktivitas tercatat."), unsafe_allow_html=True)

with sec_notes:
    with st.expander("Catatan keselamatan dan keamanan"):
        st.write(
            "Data dan ambang pada program ini hanya untuk demonstrasi dan analisis offline. "
            "Model tidak memvalidasi kondisi formasi, pipe sticking, kick, loss circulation, "
            "maupun penyebab NPT. Jangan gunakan sebagai pengganti alarm, setpoint, atau "
            "prosedur keselamatan rig resmi."
        )
        st.write(
            "Login membatasi akses ke prototipe ini, tetapi bukan pengganti keamanan "
            "SCADA industri. Batasi akses ke Wi-Fi tepercaya, gunakan password unik, "
            "dan jangan buka port aplikasi ke internet."
        )

# ---- perintah bunyi/getar dan perintah berhenti ke browser tiap device ----
with sec_players:
    if snap["ack_required"] and snap["alarm"] and snap["alarm_fresh"]:
        components.html(alarm_player_html(snap["alarm"]), height=0)
    if snap["ack"] and snap["ack_fresh"]:
        components.html(ack_stop_html(snap["ack"]), height=0)

# catat lama proses untuk mengatur interval refresh berikutnya
st.session_state["_run_secs"] = 0.5 * last_run + 0.5 * (time.time() - RUN_START)
