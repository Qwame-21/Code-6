"""
AID PLUS+ — Terminal UI Utilities
====================================
All terminal display helpers: print_header, speak (TTS), QR display,
inventory listing, sparklines, receipt printing, UI info panels.
"""
from __future__ import annotations
import os, sys, random
from datetime import datetime

from aidplus.config import *
from aidplus.db import DatabaseManager
from aidplus.security import secure_code, secure_ref

def print_header(title: str, subtitle: str = ""):
    """
    [GUI-READY] → TopBar component.
    Renders the branded screen header with optional subtitle line.
    """
    W = 62
    bar = "─" * W
    print(f"\n╔{'═'*W}╗")
    print(f"║{'AID PLUS+  ·  Nyansa v8.0  ·  ADW-1'.center(W)}║")
    print(f"╠{'═'*W}╣")
    print(f"║{title.upper().center(W)}║")
    if subtitle:
        print(f"║{subtitle.center(W)}║")
    print(f"╚{'═'*W}╝")

def ui_section(label: str):
    """[GUI-READY] → SectionDivider component. Groups menu options visually."""
    print(f"  {'─'*58}")
    print(f"  {label.upper()}")
    print(f"  {'─'*58}")

def ui_info(lines: list, style: str = "normal"):
    """
    [GUI-READY] → InfoCard component.
    style: 'normal' | 'success' | 'warn' | 'error'
    """
    icons = {"normal": "  ", "success": "✓ ", "warn": "⚠ ", "error": "✕ "}
    icon  = icons.get(style, "  ")
    for line in lines:
        print(f"  {icon}{line}")

def ui_qr(deep_link: str, message: str, instruction: str = "Scan with your phone"):
    """
    [GUI-READY] → QRPanel component.
    Development: prints the deep link URL clearly.
    Production: renders QR code bitmap on touchscreen.
    """
    W = 58
    print(f"\n  ┌{'─'*W}┐")
    print(f"  │{message.center(W)}│")
    print(f"  │{'─'*W}│")
    print(f"  │{'[  QR CODE  ]'.center(W)}│")
    print(f"  │{instruction.center(W)}│")
    print(f"  │{'─'*W}│")
    # Development: show the URL so it can be tested without a screen
    short = deep_link if len(deep_link) <= W else deep_link[:W-3]+"..."
    print(f"  │{short.center(W)}│")
    print(f"  └{'─'*W}┘\n")
    # GUI-READY: pass deep_link to QR renderer here
    # e.g. touchscreen.render_qr(deep_link, size=300, x=center, y=200)

def generate_return_code() -> str:
    return secure_code(6)    # [S2]

def speak(text: str, customer_data: dict = None, force: bool = False):
    enabled = force
    if not force and customer_data:
        enabled = bool(customer_data.get("voice_guidance"))
    elif not customer_data:
        enabled = True
    if not enabled or not TTS_ENGINE:
        if enabled:
            print(f"[VOICE] {text}")
        return
    try:
        TTS_ENGINE.say(text)
        TTS_ENGINE.runAndWait()
    except Exception:
        print(f"[VOICE FALLBACK] {text}")

def generate_sparkline(data: list) -> str:
    if not data:
        return "[ No Data ]"
    recent = data[-3:]
    if len(recent) == 1:
        return f"[ --*-- ] ({recent[0]})"
    last, prev = recent[-1], recent[-2]
    if   last > prev: return f"[ --*-^ ] ({last})"
    elif last < prev: return f"[ v-*-- ] ({last})"
    return              f"[ --*-- ] ({last})"


# ═══════════════════════════════════════════════════════════════════════════════
# INVENTORY DISPLAY
# ═══════════════════════════════════════════════════════════════════════════════
def display_inventory(db: DatabaseManager):
    print_header("FULL INVENTORY VIEW")
    print("\n--- UPPER SECTION (Capsules/Pills) ---")
    print(f"{'Shelf':<6}{'Name':<26}{'Price':>7}  {'Stock':<8}{'Barcode'}")
    print("-" * 58)
    for s in db.get_all_shelves():
        stock = f"{s['capsules_left']}/{MAX_CAPS_PER_SHELF}"
        print(f" {s['shelf']:<5}{s['name']:<26}₵{s['current_price']:>5.2f}"
              f"  {stock:<8}{SHELF_BARCODES[s['shelf']]}")
    print(f"\n{LOWER_SECTION_NAME_DISPLAY}")
    print(f"{'Shelf':<6}{'Name':<26}{'Price':>7}  {'Stock':<8}{'Barcode'}")
    print("-" * 58)
    for s in db.get_all_mega_shelves():
        stock = f"{s['units_left']}/{MAX_MEGA_PER_SHELF}"
        print(f" {s['shelf']:<5}{s['name']:<26}₵{s['current_price']:>5.2f}"
              f"  {stock:<8}{MEGA_SHELF_BARCODES[s['shelf']]}")
    print("=" * 60)


# ═══════════════════════════════════════════════════════════════════════════════
# BUILD 20 — SERVICE CLASSES
# ═══════════════════════════════════════════════════════════════════════════════

# ─────────────────────────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════
#  BUILD 25 — POWER MANAGER  |  CONNECTIVITY MANAGER  |  CUPSCAN ENHANCEMENTS
# ═══════════════════════════════════════════════════════════════════════════════
# ─────────────────────────────────────────────────────────────────────────────


