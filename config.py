"""
AID PLUS+ — Configuration Module
=================================
Single source of truth for ALL imports and ALL constants.
Every other module imports from here.

Usage:
    from aidplus.config import *          # bring everything into scope
    from aidplus.config import DB_FILE    # import specific constants
"""
from __future__ import annotations

# ── Standard Library ──────────────────────────────────────────────────────────
import sqlite3
import os
import sys
import hashlib
import secrets
import hmac as _hmac
import json
import csv
import threading
import random
import time
from datetime import datetime, timedelta

# ── Optional: OpenCV (camera/vision) ──────────────────────────────────────────
try:
    import cv2
    import numpy as np
    CV2_AVAILABLE = True
except ImportError:
    cv2 = None
    np  = None
    CV2_AVAILABLE = False

# ── Optional: Flask + PyJWT (REST API) ────────────────────────────────────────
try:
    import flask as _flask_check
    import jwt as pyjwt
    HAS_FLASK = True
except ImportError:
    HAS_FLASK = False
    pyjwt = None

# ── Optional: RPi.GPIO (hardware — sets simulation mode) ──────────────────────
try:
    import RPi.GPIO as GPIO
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    HW_SIMULATION_MODE = False
except ImportError:
    GPIO               = None
    HW_SIMULATION_MODE = True

# ── Optional: smbus2 (MLX90614 IR temperature sensor) ────────────────────────
try:
    import smbus2
    HAS_SMBUS = True
except ImportError:
    smbus2    = None
    HAS_SMBUS = False

# ── Optional: ReportLab (PDF generation) ─────────────────────────────────────
try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors as rl_colors
    from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle,
                                    Paragraph, Spacer, HRFlowable)
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    HAS_REPORTLAB = True
except ImportError:
    HAS_REPORTLAB = False
    A4 = rl_colors = SimpleDocTemplate = Table = TableStyle = None
    Paragraph = Spacer = HRFlowable = getSampleStyleSheet = ParagraphStyle = None
    cm = None

# ── Optional: qrcode (MoMo top-up QR codes) ──────────────────────────────────
try:
    import qrcode
    HAS_QRCODE = True
except ImportError:
    qrcode     = None
    HAS_QRCODE = False

# ── Optional: pyttsx3 (Text-to-Speech / CLTS Speaker) ────────────────────────
try:
    import pyttsx3
    TTS_ENGINE = pyttsx3.init()
    TTS_ENGINE.setProperty('rate', 140)
    TTS_ENGINE.setProperty('volume', 0.95)
    for _voice in TTS_ENGINE.getProperty('voices'):
        if "EN" in _voice.id.upper() or "ENGLISH" in _voice.name.upper():
            TTS_ENGINE.setProperty('voice', _voice.id)
            break
    HAS_TTS = True
except Exception:
    HAS_TTS    = False
    TTS_ENGINE = None

# ── Optional: AI Vision models (face detection + gender) ─────────────────────
try:
    face_net          = cv2.dnn.readNet("face_detection_yunet_2023mar.onnx")
    USE_YUNET         = True
    gender_net        = cv2.dnn.readNet("gender_net.caffemodel", "gender_deploy.prototxt")
    GENDER_LIST       = ['Male', 'Female']
    MODEL_MEAN_VALUES = (78.4263377603, 87.7689143744, 114.895847746)
    HAS_GENDER_MODEL  = True
    print("AI Core: CNN Detection & DNN Gender Models Active.")
except Exception as _e:
    face_net          = None
    gender_net        = None
    USE_YUNET         = False
    HAS_GENDER_MODEL  = False
    GENDER_LIST       = ['Male', 'Female']
    MODEL_MEAN_VALUES = (78.4263377603, 87.7689143744, 114.895847746)
    print(f"AI Core: Manual Fallback Mode. Reason: {_e}")


# ═══════════════════════════════════════════════════════════════════════════════
# CORE SYSTEM CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════════
DB_FILE                    = "aid_system.db"
ADMIN_PASSWORD             = os.environ.get("AID_ADMIN_PASSWORD", "AidSystem@2025!")
SCHEMA_VERSION             = 28
HW_DEPOSIT                 = 4.00
HW_REFUND                  = 2.00
MAX_CAPS_PER_SHELF         = 150
MAX_MEGA_PER_SHELF         = 50
PURCHASE_LIMIT_PER_DRUG    = 2
TOTAL_ITEM_LIMIT           = 3
DAILY_PURCHASE_LIMIT_HOURS = 24
BONUS_RATE                 = 0.05
BONUS_PER_RETURN           = 0.02
MIN_AGE                    = 16
MIN_BALANCE                = 5.0
LOW_STOCK_THRESHOLD        = 10
LOW_STOCK_MEGA_THRESHOLD   = 5
FACE_SIG_LENGTH            = 128

# [S3] Explicit allowlist — only these column names may be used in update_hw_field
ALLOWED_HW_FIELDS = frozenset({
    "aid_box_status", "cpr_kit_status",
    "aid_box_usage",  "cpr_kit_usage",
    "maintenance_revenue", "card_sales_revenue",
})

# ── Initial Inventory ─────────────────────────────────────────────────────────
INITIAL_DRUGS = [
    {"name": "Paracetamol 500mg",  "base_price": 2.50, "shelf": 1},
    {"name": "Ibuprofen 400mg",    "base_price": 3.00, "shelf": 2},
    {"name": "Amoxicillin 250mg",  "base_price": 5.50, "shelf": 3},
    {"name": "Cetirizine 10mg",    "base_price": 1.80, "shelf": 4},
    {"name": "Omeprazole 20mg",    "base_price": 4.20, "shelf": 5},
    {"name": "Metformin 500mg",    "base_price": 2.90, "shelf": 6},
    {"name": "Aspirin 75mg",       "base_price": 1.50, "shelf": 7},
]
INITIAL_MEGA_ITEMS = [
    {"name": "Paracetamol Syrup 120mg/5ml",   "base_price": 12.00, "shelf": 8},
    {"name": "Amoxicillin Suspension 125mg",  "base_price": 18.00, "shelf": 9},
    {"name": "Multivitamin Syrup",            "base_price": 15.00, "shelf": 10},
    {"name": "Cough Syrup (Promethazine)",    "base_price": 10.00, "shelf": 11},
    {"name": "Iron Supplement Syrup",         "base_price": 14.00, "shelf": 12},
    {"name": "Antacid Liquid (Gaviscon)",     "base_price": 9.00,  "shelf": 13},
]

LOWER_SECTION_NAME_DISPLAY = "--- LOWER SECTION (Syrups/Liquids/Bottles) ---"
SHELF_BARCODES = {1:"PAR500", 2:"IBU400", 3:"AMX250", 4:"CET10", 5:"OMP20", 6:"MET500", 7:"ASP75"}
MEGA_SHELF_BARCODES = {8:"PARSYR", 9:"AMXSUS", 10:"MULVIT", 11:"CGHSYR", 12:"IROSYR", 13:"ANTLIQ"}
ALL_BARCODES_TO_SHELF = {**{v:k for k,v in SHELF_BARCODES.items()},
                          **{v:k for k,v in MEGA_SHELF_BARCODES.items()}}

# ── Business Rules ────────────────────────────────────────────────────────────
RESTOCK_LEAD_TIME_DAYS     = 3
RESTOCK_SAFETY_FACTOR      = 1.3
TICKET_SLA_HOURS           = 48
CONSULT_REQUIRED_DRUGS     = ["Amoxicillin 250mg", "Metformin 500mg",
                               "Amoxicillin Suspension 125mg"]
UNIT_ID                    = "AID-UNIT-001"
OTA_MANIFEST_URL           = "https://updates.aidsystem.io/manifest.json"
OTA_STAGING_DIR            = "pending_updates"
OTA_BACKUP_DIR             = "backups"
OTA_CRASH_WINDOW_SECS      = 60
SCHEDULER_INTERVAL_SECS    = 3600

# ── API Configuration ─────────────────────────────────────────────────────────
API_HOST                   = "0.0.0.0"
API_PORT                   = 5000
API_SECRET_KEY             = secrets.token_hex(32)
JWT_EXPIRY_HOURS           = 8
API_ROLES                  = ("admin", "doctor", "distrib_centre", "readonly")

# ── GPIO Pin Map — Upper Capsule Bays (solenoid triggers) ────────────────────
GPIO_SHELF_MAP = {
    1: 5, 2: 6, 3: 12, 4: 13, 5: 17, 6: 27, 7: 22,
}
GPIO_LED_AID_RED   = 23
GPIO_LED_AID_GREEN = 24
GPIO_LED_CPR_RED   = 25
GPIO_LED_CPR_GREEN = 8
GPIO_LED_READY     = 18
GPIO_ICE_LEFT      = 20
GPIO_ICE_RIGHT     = 16
GPIO_TRIGGER_PULSE_MS = 150
GPIO_IR_TIMEOUT_MS    = 800

# ── GPIO — Lower Bay (bottle-lift stepper motor) ──────────────────────────────
GPIO_LOWER_BAY_STEP = {8:29, 9:31, 10:33, 11:35, 12:37, 13:38}
GPIO_LOWER_BAY_DIR  = {8:32, 9:36, 10:40, 11:38, 12:16, 13:12}
GPIO_LOWER_BAY_POS  = {8:36, 9:38, 10:40, 11:36, 12:38, 13:40}
GPIO_TRANSFER_ARM_SERVO   = 41
GPIO_OUTLET_NOZZLE        = 42
GPIO_OUTLET_PRESENCE      = 43
GPIO_LOWER_STEPS_UP       = 200
GPIO_LOWER_STEP_DELAY_MS  = 2
GPIO_TRANSFER_ARM_EXTEND  = 7.5
GPIO_TRANSFER_ARM_RETRACT = 2.5

# ── GPIO — Light Loop (Aid Air drone / tunnel verification) ───────────────────
GPIO_LIGHT_LOOP_CONVEYOR = 44
GPIO_LIGHT_LOOP_QR_TRIG  = 45
GPIO_LIGHT_LOOP_QR_INPUT = 46
GPIO_LIGHT_LOOP_WEIGHT   = 47
GPIO_LIGHT_LOOP_IR_SEAL  = 48
GPIO_LIGHT_LOOP_BAY_DOOR = 49
LIGHT_LOOP_TIMEOUT_MS    = 3000

# ── CLTS Hardware (Camera · Light · Thermometer · Speaker) ───────────────────
GPIO_CLTS_LED_ILLUMIN  = 70
GPIO_CLTS_LED_STATUS   = 71
GPIO_CLTS_MOTION_PIR   = 72
GPIO_CLTS_SPEAKER_EN   = 73
GPIO_CLTS_THERMO_TRIG  = 74

CLTS_LED_BRIGHT_NORMAL = 60.0
CLTS_LED_BRIGHT_DIM    = 90.0
CLTS_LED_BRIGHT_OFF    = 0.0
CLTS_TEMP_FEVER        = 37.8
CLTS_TEMP_HIGH_FEVER   = 38.5
CLTS_FACE_TIMEOUT_SECS = 20
CLTS_FACE_MAX_ATTEMPTS = 3

# ── Temperature Sensor (MLX90614 IR) ─────────────────────────────────────────
MLX90614_I2C_ADDR  = 0x5A
MLX90614_TOBJ_REG  = 0x07

# ── Card Reader (UART) ────────────────────────────────────────────────────────
CARD_READER_PORT   = "/dev/ttyUSB0"
CARD_READER_BAUD   = 9600

# ── Reporting ─────────────────────────────────────────────────────────────────
REPORTS_DIR        = "reports"
REPORT_BRAND_COLOR = (0, 0.53, 0.71)

# ── Database Backend ──────────────────────────────────────────────────────────
DB_MODE            = os.environ.get("AID_DB_MODE", "sqlite")
DB_URL             = os.environ.get("AID_DB_URL",  "")

# ── Push Notifications ────────────────────────────────────────────────────────
FCM_SERVER_KEY     = os.environ.get("AID_FCM_KEY", "")

# ── Mobile Money ──────────────────────────────────────────────────────────────
MOMO_SECRET        = os.environ.get("AID_MOMO_SECRET", "")

# ── NIA (National Identification Authority) ───────────────────────────────────
NIA_API_URL        = os.environ.get("AID_NIA_URL", "https://api.nia.gov.gh/v1/verify")
NIA_API_KEY        = os.environ.get("AID_NIA_KEY", "")
NIA_TIMEOUT_SECS   = 10
NIA_SIMULATION     = NIA_API_KEY == ""

# ── Authentication & Security ─────────────────────────────────────────────────
SYNC_TOKEN_TTL_HOURS     = 24
RESET_TOKEN_TTL_MINS     = 10
BIOMETRIC_CONFIDENCE_MIN = 0.72
LIVENESS_CHECK_ENABLED   = True

SERVICE_BUS_JWT_EXPIRY_HRS = 1   # inter-service JWT token TTL (hours)

# ── CUPSCAN / Cupsule Loyalty ─────────────────────────────────────────────────
# ── Cupsule Return Loyalty Programme ─────────────────────────────────────────
# 1 Cupsule returned = 1 loyalty point (instant, at CUPSCAN verification)
# 10 points = ₵2.00 bonus credit (significant reward for 10 returns)
# Bonus spendable ONLY on: drug purchases, ride tickets, AidPlus products
# 24-hour item return window enforced silently — not disclosed to customers
CUPSULE_POINTS_INTACT       = 1      # intact Cupsule → 1 point
CUPSULE_POINTS_EMPTY        = 1      # empty Cupsule → 1 point
CUPSULE_POINTS_DAMAGED      = 0      # damaged → 0 points
LOYALTY_REDEEM_THRESHOLD    = 10     # minimum 10 points to redeem
LOYALTY_POINTS_PER_REDEEM   = 10     # 10 points consumed per redemption
LOYALTY_BONUS_PER_REDEEM    = 2.00   # ₵2.00 bonus per 10 points
LOYALTY_REDEEMABLE_ON       = ("purchase", "ride_pay", "movie_tkt")
GHS_PER_POINT          = float(os.environ.get("CUPSCAN_GHS_PER_PT", "0.004"))
CUPSULE_RETAIL         = float(os.environ.get("CUPSCAN_RETAIL",      "0.50"))
CUPSCAN_DAILY_CAP      = int(os.environ.get("CUPSCAN_DAILY_CAP",    "10"))
CUPSCAN_BONUS_CAP_PTS  = int(os.environ.get("CUPSCAN_BONUS_CAP",    "80"))

GPIO_CUPSCAN_DROP_DOOR    = 50
GPIO_CUPSCAN_DOOR_SENSOR  = 51
GPIO_CUPSCAN_CHUTE_IR     = 52
GPIO_CUPSCAN_WEIGHT_CS    = 53
GPIO_CUPSCAN_UV_LED       = 54
GPIO_CUPSCAN_UV_SENSOR    = 55
GPIO_CUPSCAN_BIN_FULL     = 56
GPIO_CUPSCAN_BIN_HALF     = 57
GPIO_CUPSCAN_LED_READY    = 58
GPIO_CUPSCAN_LED_BUSY     = 59
GPIO_CUPSCAN_LED_ERROR    = 60
GPIO_CUPSCAN_REJECT_CHUTE = 61

CUPSCAN_DOOR_OPEN_SECS     = 8
CUPSCAN_UV_DWELL_MS        = 300
CUPSCAN_WEIGHT_EMPTY_G     = 0.5
CUPSCAN_WEIGHT_MIN_G       = 8.0
CUPSCAN_WEIGHT_MAX_G       = 20.0
CUPSCAN_CAMERA_WARMUP_MS   = 400
CUPSCAN_VERIFY_TIMEOUT_MS  = 6000
CUPSCAN_WEIGHT_PARTIAL_G   = 6.0
CUPSCAN_WEIGHT_CONTAM_G    = 18.0
CUPSCAN_WEIGHT_ANOMALY_G   = 30.0
CUPSCAN_REJECT_PULSE_MS    = 500

# ── Power Management ──────────────────────────────────────────────────────────
POWER_SOURCE_MAINS    = "MAINS"
POWER_SOURCE_SOLAR    = "SOLAR"
POWER_SOURCE_BATTERY  = "BATTERY"
POWER_STATE_OK        = "OK"
POWER_STATE_LOW       = "LOW"
POWER_STATE_CRITICAL  = "CRITICAL"
POWER_CRITICAL_PCT    = 5
POWER_LOW_PCT         = 20
POWER_SOLAR_MIN_V     = 11.5
GPIO_POWER_SOLAR_DETECT = 62
GPIO_POWER_BATTERY_ADC  = 63
POWER_LOG_INTERVAL_SECS = 300

# ── Connectivity ──────────────────────────────────────────────────────────────
CONN_WIFI           = "WiFi"
CONN_ESIM_PRIMARY   = "eSIM-MTN"
CONN_ESIM_FALLBACK  = "eSIM-Vodafone"
CONN_OFFLINE        = "Offline"
CONN_CHECK_URL      = os.environ.get("AID_CONN_CHECK_URL",
                      "https://api.aidsystem.io/ping")
CONN_CHECK_TIMEOUT_S   = 5
CONN_RETRY_INTERVALS   = [30, 60, 120, 300, 600]

# ── OTA Updates ───────────────────────────────────────────────────────────────
OTA_SERVER_URL          = os.environ.get("AID_OTA_URL",
                          "https://updates.aidsystem.io/api/v1")
OTA_CHECK_INTERVAL_HOURS = 24
OTA_BACKUP_DIR           = os.environ.get("AID_OTA_BACKUP",  "/var/aidplus/backups")
OTA_STAGING_DIR          = os.environ.get("AID_OTA_STAGING", "/var/aidplus/staging")
OTA_VERIFY_CHECKSUM      = True

# ── Adwene Hardware Platform ──────────────────────────────────────────────────
ADW_VARIANT_AS      = "ADW-AS"
ADW_VARIANT_BT      = "ADW-BT"
ADW_VARIANT_AA      = "ADW-AA"
ADWENE_DESIGNATION  = "ADW-1"
ADWENE_PLATFORM     = "Adwene"
ADWENE_VERSION      = os.environ.get("ADW_VERSION", "ADW-1.0")
ADWENE_SERIAL       = os.environ.get("ADW_SERIAL",  "ADW-UNSET")

# ── Language Codes ────────────────────────────────────────────────────────────
LANG_EN = "en"
LANG_TW = "tw"

# ── Wallet Tiers ──────────────────────────────────────────────────────────────
WALLET_TIERS = {
    "G0":      {"limit":  70.0, "price":  0.0, "name": "G0 Standard"},
    "G1":      {"limit": 120.0, "price":  8.0, "name": "G1"},
    "G2":      {"limit": 200.0, "price": 15.0, "name": "G2"},
    "G3":      {"limit": 300.0, "price": 25.0, "name": "G3"},
    "G3+plus": {"limit": 700.0, "price": 50.0, "name": "G3+ Plus"},
}
