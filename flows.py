"""
AID PLUS+ — Customer & Utility Flows
=======================================
All interactive customer flows:
  - MoMo payment simulation, receipt printing, prescription check
  - Cart management (persist, manage, checkout)
  - Drug purchasing flow with NHIS and loyalty
  - Emergency hardware access/return
  - Utility bill payment (ECG, Ghana Water, DSTV, GoTV, Vodafone)
  - Ride tickets (Aid Cab EV, Aid Air drone)
  - Movie tickets
  - Wallet management (deposit, withdraw, upgrade, physical card)
  - Message hub (notifications inbox)
  - Customer profile management
  - Purchase history
  - Personalised Nyansa health review
  - Found card reporting
  - Customer dashboard (customer_profile_menu)
  - Cupsule return (collocated terminal mode)
"""
from __future__ import annotations
import os, sys, json, random, time, csv
from datetime import datetime, timedelta

from aidplus.config import *
from aidplus.clts import CLTSUnit, clts_real_camera_wake_up, clts_wake_up_simulation
from aidplus.db import DatabaseManager
from aidplus.security import hash_password, verify_password, secure_id, secure_ref, secure_code
from aidplus.ui import (
    print_header, ui_section, ui_info, ui_qr, speak,
    generate_sparkline, generate_return_code, display_inventory,
)
from aidplus.customer import Customer, check_cart_safety, check_cart_safety_primary
from aidplus.auth import (
    BiometricAuthService, NiaVerificationService,
    WelcomeMessageService, CupsuleService, PasswordRecoveryService,
)
from aidplus.services import (
    SupportService, TeleconsultService, NyansaIntelligence,
    PromotionEngine, RestockEngine, OTAService,
    NotificationService, SchedulerService,
)
from aidplus.hardware import HardwareInterface, TemperatureSensor
from aidplus.cupscan import CUPSCANModule, DispenserManager, cupscan_multi_drop_flow
from aidplus.bus import AidPlusServiceBus
from aidplus.power import PowerManager, ConnectivityManager

def safe_input(prompt: str = "", default: str = "0") -> str:
    """Input wrapper — handles Ctrl+C and EOF gracefully by returning default."""
    try:
        return input(prompt)
    except (KeyboardInterrupt, EOFError):
        print()
        return default


from aidplus.intelligence import NyansaEngine, DRUG_ADVICE, _nyansa_drug_lookup, HEALTH_CONDITION_WARNINGS
from aidplus.platform import AidPlusOS, AdminExportService
from aidplus.reporting import ReportingService
from aidplus.api import APIServer

def simulated_momo_payment(amount: float) -> bool:
    ref = secure_ref("AID")    # [S2]
    print("\n" + "="*30)
    print("      === MoMo PAYMENT QR ===")
    for _ in range(3):
        print("   " + " ".join(["████"]*6))
    print(f"\n   Reference: {ref}")
    print(f"   Amount:    ₵{amount:.2f}")
    print("="*30)
    print("\nAwaiting confirmation (~8s)…", flush=True)
    time.sleep(8)
    print("✅ Payment Received!")
    return True

def print_receipt(items: list, total: float, badge: str,
                  transaction_id: str = ""):
    W = 62
    print()
    print("  " + "═" * W)
    print(f"  {'AID PLUS+  ·  PURCHASE RECEIPT':^{W}}")
    print("  " + "═" * W)
    print(f"  Date  : {datetime.now().strftime('%Y-%m-%d  %H:%M:%S')}")
    print(f"  Badge : {badge}")
    if transaction_id:
        print(f"  Txn ID: {transaction_id}")
    print("  " + "─" * W)
    print(f"  {'Item':<30} {'Qty':>4}  {'Unit Price':>10}  {'Subtotal':>9}")
    print("  " + "─" * W)
    for item in items:
        src  = "Bottle" if item.get("is_mega_item") else "Capsule"
        sub  = item["qty"] * item["price_per"]
        name = f"{item['name']} ({src})"[:30]
        print(f"  {name:<30} {item['qty']:>4}  ₵{item['price_per']:>9.2f}  ₵{sub:>8.2f}")
    print("  " + "─" * W)
    print(f"  {'TOTAL':>48}  ₵{total:>8.2f}")
    print("  " + "═" * W)
    if transaction_id:
        print(f"  Keep your Txn ID for returns: {transaction_id}")
    print()


# ═══════════════════════════════════════════════════════════════════════════════
# PRESCRIPTION CHECK
# ═══════════════════════════════════════════════════════════════════════════════
def check_prescription_mode(customer: Customer) -> bool:
    if not customer.current:
        return False
    exp = customer.current.get("prescription_active_until")
    if exp:
        try:
            return datetime.fromisoformat(exp) > datetime.now()
        except ValueError:
            pass
    return False


# ═══════════════════════════════════════════════════════════════════════════════
# CART MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════════
def persist_cart(db: DatabaseManager, customer_id: str, cart: list):
    for item in cart:
        if item.get("qty", 0) > 0:
            db.upsert_cart_item(customer_id, item)
        else:
            db.remove_cart_item(customer_id, item["shelf_num"])

def manage_cart(db: DatabaseManager, customer: Customer,
                cart: list) -> list | None:
    """
    Cart manager — customer picks drugs by number or barcode scan.
    Returns confirmed cart on [C]heckout, None on cancel.
    """
    all_shelves  = db.get_all_shelves()
    mega_shelves = db.get_all_mega_shelves()
    all_items    = all_shelves + mega_shelves

    while True:
        # Slot count — returns within 24h free up slots for new purchases [B29 FIX]
        recent_qty   = customer.get_daily_slot_count()
        cur_qty      = sum(i["qty"] for i in cart)
        active_limit = TOTAL_ITEM_LIMIT          # 3 — no exceptions
        slots        = max(0, active_limit - recent_qty - cur_qty)
        total        = sum(i["price_per"] * i["qty"] for i in cart)

        os.system("cls" if os.name == "nt" else "clear")
        print_header("BUY MEDICATION")
        print(f"  Daily limit : {recent_qty + cur_qty}/{TOTAL_ITEM_LIMIT}  "
              f"| Slots free: {slots}")

        # ── Cart ──────────────────────────────────────────────────────────────
        if cart:
            print(f"  \n  {'─'*54}")
            print(f"  CART  ({cur_qty} item(s))  —  Est. total: ₵{total:.2f}")
            print(f"  {'─'*54}")
            for ci, item in enumerate(cart, 1):
                sub = item["qty"] * item["price_per"]
                print(f"    {ci}. {item['name']:<30} x{item['qty']}  ₵{sub:.2f}")
            print(f"  {'─'*54}")
        else:
            print("\n  Cart is empty — select a number to add a drug.\n")

        # ── Drug catalogue ────────────────────────────────────────────────────
        if slots > 0:
            print(f"\n  {'#':<5} {'Drug':<32} {'Stock':>5}  {'Price':>8}")
            print(f"  {'─'*55}")
            for j, item in enumerate(all_items, 1):
                stock = item.get("capsules_left", item.get("units_left", 0))
                tag   = " [Bottle]" if item.get("is_mega") else ""
                print(f"  [{j:<2}] {item['name']+tag:<32} {stock:>5}  "
                      f"₵{item['current_price']:>7.2f}")
            print(f"  {'─'*55}")

        print()
        if cart:
            print("  [#] Add drug   [R] Remove   [C] Checkout   [X] Cancel")
        else:
            print("  [#] Select drug number above   [X] Cancel")
        if slots == 0:
            print("  ⚠  Daily limit reached — press [C] to checkout or [X] to cancel.")

        action = safe_input("  › ").strip().upper()

        # ── Add by number ─────────────────────────────────────────────────────
        if action.isdigit():
            if slots < 1:
                print("  Daily limit reached."); input("  Press Enter…"); continue
            try:
                j = int(action) - 1
                if not (0 <= j < len(all_items)):
                    print("  Invalid number."); input("  Press Enter…"); continue
            except ValueError:
                continue

            item_data = all_items[j]
            shelf_num = item_data["shelf"]
            is_mega   = bool(item_data.get("is_mega"))
            stock     = db.get_stock(item_data)
            if stock == 0:
                print(f"  {item_data['name']} is out of stock."); input("  Press Enter…"); continue

            limit_per    = 1 if is_mega else PURCHASE_LIMIT_PER_DRUG
            same         = next((i for i in cart if i["shelf_num"] == shelf_num), None)
            cur_item_qty = same["qty"] if same else 0
            max_qty      = min(limit_per - cur_item_qty, slots, stock)
            if max_qty < 1:
                print(f"  Limit reached for {item_data['name']} "
                      f"(max {limit_per} per 24h, {cur_item_qty} already in cart).")
                input("  Press Enter…"); continue

            try:
                qty_s = safe_input(f"  Quantity (1–{max_qty}) → ").strip()
                qty   = int(qty_s) if qty_s else 1
            except ValueError:
                qty = 1
            if not (1 <= qty <= max_qty):
                print(f"  Must be 1–{max_qty}."); input("  Press Enter…"); continue

            if same:
                same["qty"] += qty
                target = same
            else:
                cart.append({
                    "shelf_num":              shelf_num,
                    "name":                   item_data["name"],
                    "qty":                    qty,
                    "price_per":              item_data["current_price"],
                    "base_price":             item_data["base_price"],
                    "is_mega_item":           is_mega,
                    "nhis_discounted":        False,
                    "nhis_discounted_primary": False,
                })
                target = cart[-1]

            # NHIS discount
            nhis_drugs = ["Paracetamol", "Ibuprofen", "Amoxicillin",
                          "Cetirizine", "Omeprazole"]
            if (customer.current.get("nhis_session_active")
                    and any(d in item_data["name"] for d in nhis_drugs)
                    and not target.get("nhis_discounted_primary")):
                orig                              = target["price_per"]
                target["price_per"]               = round(orig * 0.6, 2)
                target["nhis_discounted_primary"] = True
                print(f"  NHIS 40% discount: ₵{orig:.2f} → ₵{target['price_per']:.2f}")

            persist_cart(db, customer.current["customer_id"], cart)
            print(f"  ✓ Added {qty} × {item_data['name']} to cart.")

            # ── Per-drug advisory + prescription dose prompt ─────────────
            try:
                from aidplus.intelligence import (
                    DRUG_CONSUMPTION_PROFILE, get_safe_dose_advice, SAFE_DAILY_DOSES)
                dkey  = item_data["name"].lower()
                prof  = None
                sdose = None
                for k in DRUG_CONSUMPTION_PROFILE:
                    if k in dkey or dkey in k:
                        prof = DRUG_CONSUMPTION_PROFILE[k]; break
                for k in SAFE_DAILY_DOSES:
                    if k in dkey or dkey in k:
                        sdose = SAFE_DAILY_DOSES[k]; break
                if prof or sdose:
                    print(f"  " + "─" * 52)
                    print(f"  📋 DRUG ADVISORY — {item_data['name'].upper()}")
                    if sdose:
                        rec = sdose["recommended"]
                        timing = sdose["timing_options"].get(rec, ["morning"])
                        print(f"  Recommended dose : {rec}x per day "
                              f"({", ".join(timing)})")
                        print(f"  Maximum safe     : {sdose['safe_max']}x per day")
                        print(f"  Advice           : {sdose['notes']}")
                    # Ask this drug's prescription dose specifically
                    print()
                    freq_in = safe_input(
                        f"  How many times per day will YOU take {item_data['name']}? "
                        f"(1/2/3, Enter = recommended {sdose['recommended'] if sdose else 2}): "
                    ).strip()
                    if freq_in.isdigit():
                        freq_val = int(freq_in)
                        advice   = get_safe_dose_advice(item_data["name"], freq_val)
                        if not advice["safe"]:
                            print(f"  ⚠  {advice['warning']}")
                            print(f"  Recorded as {advice['recommended']}x/day.")
                            target["doses_per_day"] = advice["recommended"]
                        else:
                            if advice.get("note"):
                                print(f"  ✓ {advice['note']}")
                            target["doses_per_day"] = freq_val
                        target["timing"] = advice["timing"]
                    print(f"  " + "─" * 52)
            except Exception:
                pass

            speak(f"Added {item_data['name']} to cart.", customer.current)
            input("  Press Enter to continue…")

        elif action == "C":
            if not cart:
                print("  Cart is empty."); input("  Press Enter…"); continue
            persist_cart(db, customer.current["customer_id"], cart)
            return cart

        elif action == "R":
            if not cart:
                print("  Cart is empty."); continue
            try:
                ri  = int(safe_input("  Item number to remove: ")) - 1
                if not (0 <= ri < len(cart)):
                    print("  Invalid."); continue
                cur = cart[ri]["qty"]
                rem = int(safe_input(f"  How many to remove (1–{cur}): ") or "1")
                if not (1 <= rem <= cur):
                    print("  Invalid."); continue
                cart[ri]["qty"] -= rem
                if cart[ri]["qty"] == 0:
                    db.remove_cart_item(customer.current["customer_id"],
                                        cart[ri]["shelf_num"])
                    del cart[ri]
                    print("  Item removed.")
                else:
                    persist_cart(db, customer.current["customer_id"], cart)
                    print(f"  Removed {rem} unit(s).")
            except (ValueError, IndexError):
                print("  Invalid input.")
            input("  Press Enter…")

        elif action == "X":
            persist_cart(db, customer.current["customer_id"], cart)
            return None

        else:
            print("  Type a number to add a drug, [C] checkout, [R] remove, [X] cancel.")
            input("  Press Enter…")


def checkout(db: DatabaseManager, customer: Customer, cart: list,
             rx_ref: str = None, rx_doses: dict = None,
             rx_timing: dict = None, default_freq: int = 2):
    if not cart:
        print("Cart is empty."); db.clear_cart(customer.current["customer_id"]); return

    for item in cart:
        if not customer.nyansa_health_partner(item["name"]):
            print("Transaction paused — safety review required."); return

    for item in cart:
        item_data = db.get_item_by_shelf(item["shelf_num"])
        if not item_data or db.get_stock(item_data) < item["qty"]:
            print(f"CRITICAL: {item['name']} stock changed. Purchase cancelled.")
            return

    recent_qty   = customer.get_recent_total_qty()
    cart_qty     = sum(i["qty"] for i in cart)
    active_limit = TOTAL_ITEM_LIMIT  # 3 drugs max, no exceptions
    if recent_qty + cart_qty > active_limit:
        print_header("PURCHASE LIMIT EXCEEDED")
        print(f"24h total: {recent_qty} + cart: {cart_qty} > limit: {active_limit}")
        print(f"Reset in: {customer.get_purchase_limit_reset_time()}")
        return

    total           = sum(i["price_per"] * i["qty"] for i in cart)
    balance         = customer.current.get("balance", 0.0)
    bonus           = customer.current.get("bonus",   0.0)
    total_available = balance + bonus

    warnings = check_cart_safety(cart, customer.current.get("health_info", ""))
    if warnings:
        print("\n" + "!"*40 + "\n     ⚠️  Nyansa MEDICAL SAFETY WARNINGS  ⚠️")
        for w in warnings: print(f"  - {w}")
        print("!"*40)
        if safe_input("  Proceed? (y/n): ").lower().strip() != 'y':
            print("Purchase paused."); return

    primary_w = check_cart_safety_primary(cart, customer.current.get("health_info", ""))
    if primary_w:
        print("\n⚠️  ADDITIONAL SAFETY WARNINGS:")
        for w in primary_w: print(f"  - {w}")
        if safe_input("  Proceed anyway? (y/n): ").lower().strip() != 'y':
            print("Purchase cancelled."); return

    nhis_savings = sum(
        i["qty"] * (i.get("base_price", i["price_per"]) - i["price_per"])
        for i in cart if i.get("nhis_discounted_primary")
    )
    if nhis_savings > 0:
        print(f"\n--- NHIS SAVINGS THIS PURCHASE: ₵{nhis_savings:.2f} ---")

    print_header("CHECKOUT: PAYMENT")
    speak(f"Your total is {total:.2f} cedis.", customer.current)
    print(f"Total: ₵{total:.2f}  |  Balance: ₵{balance:.2f}  "
          f"|  Bonus: ₵{bonus:.2f}  |  Available: ₵{total_available:.2f}")

    if total_available < total:
        shortfall = round(total - total_available, 2)
        print(f"\n  ✗ Insufficient funds.")
        print(f"  Total due     : ₵{total:.2f}")
        print(f"  Your balance  : ₵{balance:.2f}")
        print(f"  Your bonus    : ₵{bonus:.2f}")
        print(f"  You are short : ₵{shortfall:.2f}")
        print(f"\n  Please top up your wallet to continue.")
        print(f"  Go to [4] Wallet & Top Up from your dashboard.")
        input("  Press Enter…")
        return

    print("\n[1] Pay via Balance  [2] Bonus first  [3] MoMo QR  [X] Cancel")
    pay = safe_input("  Payment method > ").strip().upper()
    if pay == 'X':
        print("Checkout cancelled."); return

    paid_balance = paid_bonus = 0.0
    if pay == '3':
        simulated_momo_payment(total)
    elif pay == '1':
        paid_balance = min(total, balance)
        paid_bonus   = min(total - paid_balance, bonus)
    elif pay == '2':
        paid_bonus   = min(total, bonus)
        paid_balance = min(total - paid_bonus, balance)
    else:
        print("Invalid choice."); return

    customer.current["balance"] -= paid_balance
    customer.current["bonus"]   -= paid_bonus
    customer.save()

    print("\n--- Dispensing Items ---")
    all_ok = True
    for item in cart:
        ok, msg = db.dispense(
            item["shelf_num"], item["qty"], bool(item.get("is_mega_item"))
        )
        print(f"  > {item['name']} x{item['qty']}: "
              f"{'✅ SUCCESS' if ok else '❌ FAIL'} ({msg})")
        if not ok: all_ok = False

    if all_ok:
        trans_id = customer.record_purchase(cart, total, customer.current.get("card_type","digital")) or ""
        # Seed Nyansa consumption tracking for each purchased drug
        if trans_id:
            try:
                from aidplus.intelligence import create_consumption_log
                from datetime import datetime as _dpc
                for _item in cart:
                    # Use prescription dosage if available, else customer preference
                    _drug_key = _item["name"].lower()
                    _dpd = (rx_doses or {}).get(_drug_key, default_freq)
                    _tim = (rx_timing or {}).get(_drug_key, None)
                    create_consumption_log(
                        db=db,
                        customer_id=customer.current["customer_id"],
                        transaction_id=trans_id,
                        drug_name=_item["name"],
                        qty_purchased=_item["qty"],
                        purchase_date=_dpc.now().isoformat(),
                        has_prescription=bool(rx_ref),
                        prescription_ref=rx_ref,
                        doses_per_day_override=float(_dpd),
                    )
            except Exception:
                pass
        if paid_balance > 0:
            pb = round(paid_balance * BONUS_RATE, 2)
            customer.add_bonus(pb)
            customer.record_wallet_transaction(
                "bonus_earn", pb, f"Bonus from ₵{paid_balance:.2f} payment"
            )
            print(f"🎉 Bonus earned: +₵{pb:.2f}")
        db.clear_cart(customer.current["customer_id"])
        customer.current["cart"] = []
        print_receipt(cart, total, customer.current.get("card_type","digital"), trans_id)
        print(f"COMPLETE. Balance: ₵{customer.current['balance']:.2f}"
              f" | Bonus: ₵{customer.current['bonus']:.2f}")
    else:
        print("\n⚠️  FATAL: One or more items failed to dispense. Contact support.")
    input("\nPress Enter…")


# ═══════════════════════════════════════════════════════════════════════════════
# BUY DRUGS FLOW
# ═══════════════════════════════════════════════════════════════════════════════
def buy_drugs_flow(db: DatabaseManager, customer: Customer):
    """
    Full purchase flow with:
    - Prescription management (stores dosage, adjusts consumption algorithm)
    - NHIS discount
    - Customer daily consumption rate selection
    """
    from aidplus.intelligence import get_safe_dose_advice
    cid = customer.current["customer_id"]

    # ── Prescription ─────────────────────────────────────────────────────
    rx_ref        = None
    rx_doses      = {}   # {drug_name: doses_per_day}
    rx_timing     = {}   # {drug_name: [timing slots]}

    if not check_prescription_mode(customer):
        has_rx = safe_input("  Do you have a prescription? (y/n): ").lower().strip()
        if has_rx == "y":
            rx_ref = safe_input("  Prescription code or reference (or Enter to skip): ").strip()
            if rx_ref:
                # Look up prescription in DB
                with db._conn() as _rx:
                    rx_row = _rx.execute(
                        "SELECT * FROM prescriptions WHERE prescription_id=? "
                        "AND customer_id=? AND status='active'",
                        (rx_ref, cid)).fetchone()
                if rx_row:
                    print(f"  ✅ Prescription found: {rx_row['drug_name']} — {rx_row['dosage_instructions']}")
                    customer.current["prescription_active_until"] = (
                        datetime.now() + timedelta(hours=1)).isoformat()
                    customer.save()
                else:
                    # Not in DB — ask them to describe it
                    print("  Prescription code not on record. Please describe your prescription:")
                    rx_drug = safe_input("  Which drug was prescribed? ").strip()
                    if rx_drug:
                        rx_freq_input = safe_input(
                            f"  How many times per day for {rx_drug}? (1/2/3): ").strip()
                        try:
                            rx_freq = int(rx_freq_input)
                        except ValueError:
                            rx_freq = 2
                        advice = get_safe_dose_advice(rx_drug, rx_freq)
                        if not advice["safe"]:
                            print(f"  {advice['warning']}")
                            print(f"  System will use the recommended {advice['recommended']}x/day.")
                            rx_freq = advice["recommended"]
                        else:
                            print(f"  ✓ {advice.get('note', '')}")
                        rx_doses[rx_drug.lower()] = rx_freq
                        rx_timing[rx_drug.lower()] = advice["timing"]
                        print(f"  Dosing recorded: {rx_freq}x/day ({", ".join(advice['timing'])})")
                        customer.current["prescription_active_until"] = (
                            datetime.now() + timedelta(hours=1)).isoformat()
                        customer.save()
    else:
        print("  ✅ Prescription mode active.")
        rx_ref = customer.current.get("prescription_ref", "")

    # ── Customer consumption rate preference ─────────────────────────────
    print()
    print("  How often do you typically take medication?")
    print("  [1] Once daily       (1x/day — morning)")
    print("  [2] Twice daily      (2x/day — morning & evening)  ← most common")
    print("  [3] Three times daily (3x/day — morning, afternoon & evening)")
    freq_sel = safe_input("  › ").strip()
    default_freq = {"1": 1, "2": 2, "3": 3}.get(freq_sel, 2)
    # Store as session preference for consumption algorithm
    customer.current["session_doses_per_day"] = default_freq

    # ── NHIS ─────────────────────────────────────────────────────────────
    customer.current["nhis_session_active"] = False
    if customer.current.get("nhis_active"):
        if safe_input("  Apply NHIS discount this session? (y/n): ").lower().strip() == 'y':
            ok, msg = customer.verify_nhis()
            print(f"{'✅' if ok else '❌'} {msg}")

    cart    = db.get_cart(cid)
    managed = manage_cart(db, customer, cart)

    if managed:
        checkout(db, customer, managed,
                 rx_ref=rx_ref, rx_doses=rx_doses, rx_timing=rx_timing,
                 default_freq=default_freq)
    else:
        print("  Purchase cancelled.")


# ═══════════════════════════════════════════════════════════════════════════════
# HARDWARE
# ═══════════════════════════════════════════════════════════════════════════════
def emergency_hardware_flow(customer: Customer, db: DatabaseManager):
    print_header("🚨 Nyansa EMERGENCY HARDWARE ACCESS 🚨")
    hw = db.get_hw_status()
    print("1. AID BOX (₵4.00 Deposit)\n2. CPR KIT (₵4.00 Deposit)")
    h_choice = input("Select hardware: ").strip()
    if h_choice not in ('1', '2'):
        print("Invalid choice."); return

    comp_key     = "aid_box"  if h_choice == '1' else "cpr_kit"
    comp_name    = "aid box"  if h_choice == '1' else "cpr kit"
    status_field = f"{comp_key}_status"
    usage_field  = f"{comp_key}_usage"

    if hw.get(status_field) == "Deployed":
        print(f"🚫 UNAVAILABLE: {comp_name.upper()} is already deployed.")
        input("\nPress Enter..."); return
    if hw.get(usage_field, 0) >= 5:
        print(f"🚫 OFFLINE: Sanitization required ({hw.get(usage_field)}/5 uses).")
        input("\nPress Enter..."); return

    cid = input("Scan Membership Card / Enter ID: ").strip()
    c   = db.get_customer(cid)
    if not c:
        print("❌ ID not found."); return

    old_current      = customer.current
    customer.current = c
    try:
        print("Accessing as: (1) Registered Patient  (2) Bystander")
        is_bystander = input("Choice > ").strip() == '2'
        patient_tag  = cid if not is_bystander else "John Doe"
        fee          = HW_DEPOSIT
        if not is_bystander and c.get("wallet_tier") in ("G2","G3","G3+plus"):
            print("✨ GOLD STATUS: ₵2.00 emergency discount applied.")
            fee = 2.00

        if not clts_real_camera_wake_up(customer, db)[0]:
            print("❌ Security mismatch."); return
        balance = c.get("balance", 0.0)
        bonus   = c.get("bonus",   0.0)
        if balance + bonus < fee:
            print(f"\n  ✗ Insufficient funds to access emergency hardware.")
            print(f"    Required  : ₵{fee:.2f} deposit")
            print(f"    Balance   : ₵{balance:.2f}")
            print(f"    Bonus     : ₵{bonus:.2f}")
            print(f"    Total     : ₵{balance+bonus:.2f}")
            print(f"    Shortfall : ₵{fee-(balance+bonus):.2f}")
            print(f"    Top up your wallet to proceed.")
            input("  Press Enter…"); return
        # Pay from bonus first, then balance — inform customer
        paid_bonus   = min(fee, bonus)
        paid_balance = min(fee - paid_bonus, balance)
        if paid_bonus > 0 and paid_balance > 0:
            print(f"  ℹ️  Payment: ₵{paid_bonus:.2f} from bonus + ₵{paid_balance:.2f} from balance")
        elif paid_bonus > 0:
            print(f"  ℹ️  Payment: ₵{paid_bonus:.2f} from your bonus credit")
        c["bonus"]   = round(bonus   - paid_bonus,   2)
        c["balance"] = round(balance - paid_balance, 2)
        db.save_customer(c)
        ret_code = secure_code(6)          # [S2]
        trans_id = secure_ref("HW")        # [S2]
        db.add_wallet_entry(cid, "Hardware Deposit", -fee,
                            f"Emergency {comp_name.upper()} Access. Code: {ret_code}",
                            trans_id)
        db.update_hw_field(status_field, "Deployed")
        db.set_active_code(cid, ret_code, comp_name, fee)
        db.send_notification(cid, "EMERGENCY HARDWARE RETURN CODE",
                             f"{comp_name.upper()} return code: {ret_code}\n"
                             f"Keep this safe — you will need it to return the "
                             f"{comp_name.upper()} and collect your ₵{HW_REFUND:.2f} refund.")
        db.add_emergency_log(
            customer_id=cid, patient_tag=patient_tag, action="DEPLOY",
            component=comp_name.upper(), deposit=fee, trans_id=trans_id
        )
        print(f"✅ {comp_name.upper()} RELEASED. Return code: {ret_code}")
        input("\nPress Enter to return to main menu...")
    finally:
        customer.current = old_current

def return_hardware_flow(customer: Customer, db: DatabaseManager):
    print_header("RETURN EMERGENCY HARDWARE")
    cid = input("Enter Membership ID: ").strip()
    if not db.has_active_code(cid):
        print("❌ No active deployments found for this ID.")
        input("\nPress Enter..."); return

    # Show all active deployments for this customer
    active_codes = db.get_all_active_codes(cid)
    if len(active_codes) > 1:
        print("\nActive deployments:")
        for i, ac in enumerate(active_codes, 1):
            print(f"  [{i}] {ac['component'].upper()}")
        choice = input("Select which to return (number): ").strip()
        try:
            code_data = active_codes[int(choice) - 1]
        except (ValueError, IndexError):
            print("❌ Invalid choice."); input("\nPress Enter..."); return
    else:
        code_data = active_codes[0]

    print(f"\n  Return code is the 6-digit number shown when you took the {code_data["component"].upper()}.")
    print(f"  It was also sent to your AID PLUS+ inbox.")
    user_code = input(f"  Enter return code for {code_data["component"].upper()}: ").strip()
    if not secrets.compare_digest(user_code.strip(), code_data["code"].strip()):  # [S2]
        print("\n❌ Return code does not match. Check your inbox notification for the correct code.")
        input("\nPress Enter..."); return

    comp     = code_data["component"]
    comp_key = comp.replace(" ", "_")
    start_t  = datetime.fromisoformat(code_data["start_time"])
    mins     = (datetime.now() - start_t).total_seconds() / 60
    d_type   = "Short-Term" if mins < 15 else "Long-Term"

    print(f"[{d_type} Deployment] Verify kit status:")
    used_status = input("Q1: Was the kit used? (y/n): ").lower().strip()
    seal_status = input("Q2: Is the security seal intact? (y/n): ").lower().strip()
    is_broken   = seal_status == 'n'
    is_used     = used_status == 'y'
    audit_flag  = (not is_used and is_broken)

    print(f"🔄 Processing return… place {comp.upper()} in the return slot.")
    db.update_hw_field(f"{comp_key}_status", "Docked")
    db.clear_active_code(cid, component=comp)

    c = db.get_customer(cid)
    c["balance"] = c.get("balance", 0.0) + HW_REFUND
    db.save_customer(c)
    db.add_wallet_entry(cid, "Hardware Refund", HW_REFUND, f"Return of {comp.upper()}")

    if mins >= 15 and is_used:
        c["return_count"] = c.get("return_count", 0) + 1
        db.save_customer(c)
        print("⭐ Loyalty: Genuine emergency use logged. +1 Return Credit.")

    if is_broken:
        hw        = db.get_hw_status()
        new_usage = hw.get(f"{comp_key}_usage", 0) + 1
        db.update_hw_field(f"{comp_key}_usage", new_usage)
        print("⚠️  Sanitization: Seal broken — unit queued for maintenance.")

    db.add_emergency_log(
        customer_id=cid, action="RETURN", component=comp.upper(),
        time_delta_mins=round(mins, 1), used_claim=used_status,
        seal_status=seal_status, audit_required=audit_flag, refund=HW_REFUND
    )
    if audit_flag:
        print("❗ DISCREPANCY: Seal broken but 'not used' claimed — flagged for audit.")
    print(f"💰 Refund of ₵{HW_REFUND:.2f} applied.")
    input("\nPress Enter...")


# ═══════════════════════════════════════════════════════════════════════════════
# SERVICES
# ═══════════════════════════════════════════════════════════════════════════════
def pay_utility_bill(customer: Customer, db: DatabaseManager):
    """
    [GUI-READY] Utility bill payment.
    Prices based on real Ghana utility tariffs (2024/2025).
    ECG: tiered residential rate ~GHS 50-350/month
    Ghana Water: ~GHS 20-180/month
    DSTV/GoTV: fixed package prices from MultiChoice Ghana
    """
    print_header("PAY UTILITY BILL")
    W = 62
    providers = {
        '1': {"name": "ECG (Electricity)",    "min": 50.0,  "max": 350.0, "unit": "kWh bill"},
        '2': {"name": "Ghana Water Company",  "min": 20.0,  "max": 180.0, "unit": "water bill"},
        '3': {"name": "DSTV",                 "packages": {
                  "1": ("Padi ₵30/mo",         30.0),
                  "2": ("Compact ₵112/mo",     112.0),
                  "3": ("Compact+ ₵171/mo",    171.0),
                  "4": ("Premium ₵248/mo",     248.0),
              }},
        '4': {"name": "GoTV",                 "packages": {
                  "1": ("GoTV Value ₵15/mo",   15.0),
                  "2": ("GoTV Plus ₵25/mo",    25.0),
                  "3": ("GoTV Max ₵40/mo",     40.0),
              }},
        '5': {"name": "Vodafone Broadband",   "packages": {
                  "1": ("10GB ₵55/mo",         55.0),
                  "2": ("25GB ₵99/mo",         99.0),
                  "3": ("Unlimited ₵199/mo",  199.0),
              }},
    }

    print(f"\n╔{'═'*W}╗")
    print(f"║{'SELECT UTILITY PROVIDER'.center(W)}║")
    print(f"╠{'═'*W}╣")
    for k, v in providers.items():
        name = v['name']
        print(f"║  [{k}]  {name.ljust(W-6)}║")
    print(f"║  [0]  Back{' '*(W-7)}║")
    print(f"╚{'═'*W}╝")

    p = safe_input("  › ").strip()
    if p == '0' or p not in providers:
        if p != '0': print("Invalid.")
        return

    prov   = providers[p]
    pname  = prov["name"]
    amount = 0.0

    if "packages" in prov:
        print(f"\n  {pname} — Select package:")
        for k, (label, price) in prov["packages"].items():
            print(f"  [{k}]  {label}")
        pkg = safe_input("  › ").strip()
        if pkg not in prov["packages"]:
            print("Invalid."); input("  Press Enter…"); return
        label, amount = prov["packages"][pkg]
        print(f"\n  Package: {label}")
    else:
        acc = input(f"  Enter {pname} meter/account number: ").strip()
        if not acc:
            print("Account number required."); input("  Press Enter…"); return
        try:
            amount = round(float(input(f"  Amount to pay (₵{prov['min']:.0f}–₵{prov['max']:.0f}): ")), 2)
        except ValueError:
            print("Invalid amount."); input("  Press Enter…"); return
        if not (prov['min'] <= amount <= prov['max']):
            print(f"  Amount must be between ₵{prov['min']:.0f} and ₵{prov['max']:.0f}.")
            input("  Press Enter…"); return

    bal = customer.current.get("balance", 0.0)
    print(f"\n  Provider : {pname}")
    print(f"  Amount   : ₵{amount:.2f}")
    print(f"  Balance  : ₵{bal:.2f}")

    if bal < amount:
        print(f"  ✗ Insufficient balance (need ₵{amount:.2f}, have ₵{bal:.2f}).")
        input("  Press Enter…"); return

    if input("\n  Confirm payment? (y/n): ").lower().strip() == 'y':
        customer.current["balance"] -= amount
        ref = secure_ref("UTIL")
        db.add_wallet_entry(customer.current["customer_id"],
                            "bill_pay", -amount, f"Paid {pname} ({ref})")
        db.send_notification(customer.current["customer_id"], "Utility Payment",
                             f"✅ Paid ₵{amount:.2f} to {pname}. Ref: {ref}")
        customer.save()
        print(f"\n  ✅ Payment successful!")
        print(f"  Reference : {ref}")
        print(f"  New balance: ₵{customer.current['balance']:.2f}")
    else:
        print("  Cancelled.")
    input("  Press Enter…")

def buy_ride_tickets(customer: Customer, db: DatabaseManager):
    print_header("BUY RIDE TICKETS")
    print("[1] Aid Cab (EV Rides)\n[2] Aid Air (Drone Delivery)")
    choice = input("Select service: ").strip()
    cost = 0.0; service_name = ""
    if choice == '1':
        service_name = "Aid Cab"
        print("[1] Accra Mall→Osu (12km)\n[2] Legon→Circle (15km)\n[3] Custom")
        r    = input("Route: ")
        dist = 12 if r=='1' else 15 if r=='2' else 0
        if r == '3':
            try: dist = float(input("Distance (km): "))
            except ValueError: dist = 0
        if dist > 0: cost = 10.0 + 2.0 * dist
        else: print("Invalid distance."); return
    elif choice == '2':
        service_name = "Aid Air"
        print("[1] Local ₵30\n[2] Regional ₵120\n[3] Long-haul ₵250")
        z    = input("Zone: ")
        cost = {'1':30.0,'2':120.0,'3':250.0}.get(z, 0)
        if not cost: print("Invalid zone."); return
    else: print("Invalid."); return

    print(f"\nTrip cost: ₵{cost:.2f}")
    if customer.current.get("balance", 0.0) >= cost:
        if input("Confirm? (y/n): ").lower() == 'y':
            customer.current["balance"] -= cost
            tag  = service_name.split()[1].upper()
            code = secure_ref(f"AID-{tag}")    # [S2]
            ride_desc = f"Service: {service_name} | Ref: {code} | Cost: ₵{cost:.2f}"
            db.add_wallet_entry(customer.current["customer_id"],
                                "ride_pay", -cost, ride_desc)
            db.send_notification(customer.current["customer_id"], "Ride Ticket",
                                 f"{service_name} confirmed. Code: {code}")
            customer.save()
            print(f"✅ Ticket: {code}")
        else: print("Cancelled.")
    else: print("Insufficient funds.")
    input("\nPress Enter…")

def buy_movie_ticket(customer: Customer, db: DatabaseManager):
    print_header("BUY MOVIE TICKET")
    cinemas = {'1':'Silverbird','2':'West Hills','3':'Kumasi City'}
    for k, v in cinemas.items(): print(f"[{k}] {v}")
    cinema = cinemas.get(safe_input("  Select cinema: ").strip())
    if not cinema: print("Invalid."); return
    movies = ["The Matrix Resurrections","Spider-Man: No Way Home","Black Panther 2"]
    for i, m in enumerate(movies): print(f"[{i+1}] {m}")
    try:
        idx   = int(safe_input("  Select movie: ")) - 1
        if not (0 <= idx < len(movies)): raise ValueError
        movie = movies[idx]
    except ValueError: print("Invalid."); return
    print("[1] Standard ₵35  [2] VIP ₵55 (includes free popcorn)")
    t     = safe_input("  Ticket type: ").strip()
    price = {'1':35.0,'2':55.0}.get(t, 0)
    label = {'1':'Standard','2':'VIP'}.get(t)
    if not label: print("Invalid."); return
    print(f"\nTotal: ₵{price:.2f} ({movie} — {label})")
    if customer.current.get("balance", 0.0) >= price:
        if safe_input("  Confirm? (y/n): ").lower() == 'y':
            customer.current["balance"] -= price
            seat = f"{random.choice('ABCD')}{random.randint(1,20)}"
            code = secure_ref("MOV")    # [S2]
            note = f"Ticket: {code} | Seat: {seat}"
            if t == '2': note += " | Free Popcorn 🍿"
            # Store full booking detail in wallet_history for purchase history display
            full_desc = (f"Cinema: {cinema} | Movie: {movie} | "
                         f"Ticket: {label} | Seat: {seat} | Ref: {code}"
                         + (" | Free Popcorn 🍿" if t == '2' else ""))
            db.add_wallet_entry(customer.current["customer_id"],
                                "movie_tkt", -price, full_desc)
            db.send_notification(customer.current["customer_id"], "Movie Ticket", note)
            customer.save()
            print(f"✅ Booking confirmed! {note}")
        else: print("Cancelled.")
    else: print("Insufficient funds.")
    input("\nPress Enter…")

def utilities_and_services_menu(customer: Customer, db: DatabaseManager):
    while True:
        print_header("UTILITIES & SERVICES DASHBOARD")
        print(f"Wallet Balance: ₵{customer.current.get('balance', 0.0):.2f}")
        print("[1] Pay Utility Bill  [2] Buy Ride Ticket"
              "  [3] Buy Movie Ticket  [0] Back")
        ch = input("Option > ").strip()
        if ch == '1': pay_utility_bill(customer, db)
        elif ch == '2': buy_ride_tickets(customer, db)
        elif ch == '3': buy_movie_ticket(customer, db)
        elif ch == '0': break
        else: print("Invalid."); input("Press Enter…")


# ═══════════════════════════════════════════════════════════════════════════════
# WALLET MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════════
def upgrade_to_physical_card(customer: Customer, db: DatabaseManager):
    print_header("ORDER PHYSICAL AID CARD")
    print("Price: ₵7.00 | NFC-enabled, Lost & Found protection.")
    if customer.current.get("has_physical_card"):
        print("ℹ️  You already have a Physical Aid Card.")
        input("\nPress Enter..."); return
    if customer.current.get("balance", 0.0) < 7.0:
        print(f"❌ Insufficient balance (have ₵{customer.current.get('balance',0.0):.2f})")
        input("\nPress Enter..."); return
    if input("Confirm ₵7.00 purchase? (y/n): ").lower().strip() == 'y':
        customer.current["balance"]           -= 7.0
        customer.current["has_physical_card"]  = True
        hw_rev = db.get_hw_status().get("card_sales_revenue", 0.0)
        db.update_hw_field("card_sales_revenue", hw_rev + 7.0)
        db.add_wallet_entry(customer.current["customer_id"],
                            "upgrade", -7.0, "Purchased Physical AID Card")
        db.log_audit(customer.current["customer_id"], "CARD_PURCHASE",
                     "customers", customer.current["customer_id"],
                     "Physical AID Card purchased ₵7.00")
        customer.save()
        print("🎉 SUCCESS! Physical Card dispensed — please collect below.")
        time.sleep(1)
    else: print("Cancelled.")
    input("\nPress Enter…")

def manage_wallet(customer: Customer, db: DatabaseManager):
    while True:
        c         = customer.current
        tier_key  = c.get("wallet_tier", "G0")
        tier_info = WALLET_TIERS[tier_key]
        tier_keys = list(WALLET_TIERS.keys())
        cur_idx   = tier_keys.index(tier_key)
        next_key  = tier_keys[cur_idx + 1] if cur_idx + 1 < len(tier_keys) else None
        limit     = tier_info["limit"]

        print_header("MANAGE WALLET")
        print(f"Tier: {tier_info['name']} | Balance: ₵{c.get('balance',0.0):.2f}"
              f" | Bonus: ₵{c.get('bonus',0.0):.2f} | Limit: ₵{limit:.2f}")
        print("[1] History  [2] Deposit  [3] Withdraw"
              "  [4] Upgrades  [6] Physical Card  [5] Back")
        wch = input("> ").strip()

        if wch == '1':
            history = db.get_wallet_history(c["customer_id"])
            print("\n--- TRANSACTION HISTORY ---")
            if not history: print("No transactions recorded.")
            else:
                for e in history:
                    sign = "+" if e["amount"] >= 0 else "-"
                    desc = e.get("description", "")[:50]
                    try: dt = datetime.fromisoformat(e["timestamp"]).strftime("%Y-%m-%d %H:%M")
                    except ValueError: dt = e["timestamp"]
                    print(f" [{dt}] {e['type'].upper():<12}: "
                          f"{sign}₵{abs(e['amount']):.2f} — {desc}")
            input("\nPress Enter…")

        elif wch == '2':
            print("[1] Cash Top-up  [2] MoMo QR  [3] Back")
            dc = input("Option > ").strip()
            if dc == '3': continue
            max_add = limit - c.get("balance", 0.0)
            if max_add <= 0.01:
                print("Wallet is full."); continue
            try:
                amt = round(float(input(f"Deposit amount (max ₵{max_add:.2f}): ")), 2)
            except ValueError:
                print("Invalid."); continue
            if amt <= 0 or amt > max_add:
                print(f"Must be ₵0.01–₵{max_add:.2f}."); continue
            if dc == '2':
                simulated_momo_payment(amt)
                db.add_wallet_entry(c["customer_id"], "deposit_momo", amt, "Top-up via MoMo QR")
            else:
                db.add_wallet_entry(c["customer_id"], "deposit", amt, "Cash Top-up")
            c["balance"] = c.get("balance", 0.0) + amt
            customer.save()
            print(f"✅ Deposited ₵{amt:.2f}. New balance: ₵{c['balance']:.2f}")

        elif wch == '3':
            MIN_HOLD = 5.0
            avail    = c.get("balance", 0.0) - MIN_HOLD
            if avail <= 0:
                print(f"Must maintain ₵{MIN_HOLD:.2f} minimum balance."); continue
            try:
                amt = round(float(input(f"Withdraw amount (max ₵{avail:.2f}): ")), 2)
            except ValueError:
                print("Invalid."); continue
            if amt <= 0 or amt > avail:
                print("Invalid amount."); continue
            c["balance"] -= amt
            db.add_wallet_entry(c["customer_id"], "withdrawal", -amt, "Cashless Withdrawal")
            customer.save()
            print(f"✅ Withdrew ₵{amt:.2f}. Balance: ₵{c['balance']:.2f}")

        elif wch == '4':
            print_header("AVAILABLE UPGRADES")
            if not next_key:
                print("You are at the maximum tier (G3+ Plus).")
                input("Press Enter..."); continue
            upgradable = tier_keys[cur_idx + 1:]
            sel_map    = {}
            for i, k in enumerate(upgradable):
                ui = WALLET_TIERS[k]
                print(f" [{i+1}] {ui['name']} — Limit: ₵{ui['limit']:.2f}"
                      f" | Cost: ₵{ui['price']:.2f}")
                sel_map[str(i + 1)] = k
            print("\n[0] Back")
            uc = input("Option > ").strip()
            if uc == '0': continue
            if uc in sel_map:
                tgt  = WALLET_TIERS[sel_map[uc]]
                cost = tgt["price"]
                if c.get("balance", 0.0) + c.get("bonus", 0.0) < cost:
                    print("Insufficient funds."); input("Press Enter..."); continue
                if input(f"Confirm ₵{cost:.2f}? (y/n): ").lower() == 'y':
                    paid_b = min(cost, c.get("balance", 0.0))
                    paid_x = min(cost - paid_b, c.get("bonus", 0.0))
                    old_tier         = c["wallet_tier"]
                    c["balance"]    -= paid_b
                    c["bonus"]      -= paid_x
                    c["wallet_tier"] = sel_map[uc]
                    db.add_wallet_entry(c["customer_id"], "upgrade", -cost,
                                        f"Upgraded to {tgt['name']}")
                    db.send_notification(c["customer_id"], "Upgrade",
                                         f"Congratulations! Upgraded to {tgt['name']}.")
                    db.log_audit(c["customer_id"], "UPGRADE", "customers",
                                 c["customer_id"],
                                 f"Tier: {old_tier} → {sel_map[uc]} | ₵{cost:.2f}")
                    customer.save()
                    print(f"✅ Now {tgt['name']}.")
                    input("Press Enter…")
                else: print("Cancelled.")
            else: print("Invalid selection.")

        elif wch == '5': break
        elif wch == '6': upgrade_to_physical_card(customer, db)
        else: print("Invalid.")


# ═══════════════════════════════════════════════════════════════════════════════
# MESSAGE HUB
# ═══════════════════════════════════════════════════════════════════════════════
def message_hub(customer: Customer, db: DatabaseManager):
    """Inbox — view and delete notifications. Supports multi-select delete."""
    while True:
        c    = customer.current
        msgs = db.get_notifications(c["customer_id"])
        print_header("MESSAGES & NOTIFICATIONS")
        if not msgs:
            print("  Your inbox is empty.")
            input("  Press Enter…"); return

        print(f"  {'#':<4} {'Date':<12} {'Subject':<28} Preview")
        print("  " + "─"*68)
        for i, m in enumerate(msgs, 1):
            dt      = m.get("timestamp","")[:10]
            subject = m.get("tag","")[:26]
            preview = m.get("message","")[:24]
            print(f"  [{i:<2}] {dt:<12} {subject:<28} {preview}")
        print("  " + "─"*68)
        print("  Enter message number to view  |  D <nums> to delete  |  DA to delete all  |  0 to back")
        print("  Example delete: D 1 3 5   or   DA")
        ch = safe_input("  › ").strip().upper()

        if ch == '0':
            return
        elif ch == 'DA':
            if input("  Delete ALL messages? (y/n): ").lower() == 'y':
                with db._conn() as con:
                    con.execute("DELETE FROM notifications WHERE customer_id=?",
                                (c["customer_id"],))
                print("  ✓ All messages deleted.")
                input("  Press Enter…")
        elif ch.startswith('D ') or (len(ch) > 1 and ch[0] == 'D' and ch[1:].strip().isdigit()):
            nums_str = ch[1:].strip()
            try:
                nums = [int(x)-1 for x in nums_str.split() if x.isdigit()]
                valid = [msgs[n] for n in nums if 0 <= n < len(msgs)]
                if valid:
                    ids = [m["id"] for m in valid if "id" in m]
                    with db._conn() as con:
                        for mid in ids:
                            con.execute("DELETE FROM notifications WHERE id=?", (mid,))
                    print(f"  ✓ Deleted {len(valid)} message(s).")
                    input("  Press Enter…")
            except Exception:
                print("  Invalid selection.")
                input("  Press Enter…")
        else:
            try:
                idx_msg = int(ch) - 1
                if 0 <= idx_msg < len(msgs):
                    m = msgs[idx_msg]
                    print_header(m.get("tag","Notification"))
                    print(f"  Date: {m.get('timestamp','')[:19]}")
                    print(f"\n  {m.get('message','')}")
                    print()
                    act = input("  [D] Delete   [0] Back: ").strip().upper()
                    if act == 'D' and "id" in m:
                        with db._conn() as con:
                            con.execute("DELETE FROM notifications WHERE id=?", (m["id"],))
                        print("  ✓ Deleted.")
                        input("  Press Enter…")
            except (ValueError, IndexError):
                print("  Invalid choice.")
                input("  Press Enter…")


def manage_customer_profile(customer: Customer, db: DatabaseManager) -> str | None:
    """
    [GUI-READY] → ProfileEditor screen.
    All edits require explicit confirmation before saving.
    Blank input = keep current value (no accidental overwrites).
    Name changes require password re-verification for security.
    Address uses structured input for Ghana Health Service compatibility.
    """
    while True:
        c      = customer.current
        os.system('cls' if os.name == 'nt' else 'clear')
        trends = db.get_health_trends(c["customer_id"])
        W = 62
        print(f"\n╔{'═'*W}╗")
        print(f"║{'AID PLUS+  ·  My Profile'.center(W)}║")
        print(f"╠{'═'*W}╣")
        print(f"║  ID     : {c['customer_id']:<51}║")
        print(f"║  Status : {c.get('status','Active'):<51}║")
        print(f"╠{'═'*W}╣")
        print(f"║  {'PERSONAL'.ljust(W-2)}║")
        print(f"║  [1]  Name      : {c['name']:<43}║")
        print(f"║  [2]  Address   : {str(c.get('address','Not set'))[:43]:<43}║")
        print(f"║  [3]  Contact   : {c.get('contact','')} / {c.get('email','')}  ║".ljust(W+3)+"║")
        print(f"╠{'═'*W}╣")
        print(f"║  {'HEALTH'.ljust(W-2)}║")
        print(f"║  [4]  Allergies  : {str(c.get("allergies","None"))[:43]:<43}║")
        print(f"║  [4]  Conditions : {str(c.get("chronic_conditions","None"))[:43]:<43}║")
        print(f"║  [H]  Add Health Reading (Temp / Weight)              ║")
        print(f"╠{'═'*W}╣")
        print(f"║  {'SECURITY & PREFERENCES'.ljust(W-2)}║")
        print(f"║  [5]  Change Password                                 ║")
        print(f"║  [6]  Notifications  "
              f"(Email:{'ON' if c.get('notification_email') else 'OFF'}  "
              f"SMS:{'ON' if c.get('notification_sms') else 'OFF'}){'':18}║")
        print(f"║  [V]  Voice Guidance : {'ON ' if c.get('voice_guidance') else 'OFF'}{'':38}║")
        print(f"╠{'═'*W}╣")
        print(f"║  [D]  Delete Account (PERMANENT){'':29}║")
        print(f"║  [0]  Back{'':51}║")
        print(f"╚{'═'*W}╝")
        ch = safe_input("  › ").strip().upper()

        if ch == '1':
            # Name change — pros: legal name correction, marriage, typo fix
            # cons: fraud risk if unrestricted. Requires password confirmation.
            print_header("CHANGE NAME")
            print(f"  Current name: {c['name']}")
            print("  Note: Name changes are logged and may require ID verification.")
            print("  Leave blank to cancel.")
            new_name = input("  New full name: ").strip()
            if not new_name:
                print("  No changes made.")
            elif new_name == c["name"]:
                print("  Name is unchanged.")
            else:
                # Require password confirmation for name changes
                pwd_check = input("  Confirm your password to proceed: ").strip()
                from hashlib import sha256
                pwd_hash, _ = hash_password(pwd_check)
                if not verify_password(c["password"], c.get("password_salt",""), pwd_check):
                    print("  ✗ Incorrect password. Name change cancelled.")
                else:
                    confirm = input(f"  Change name to '{new_name}'? (y/n): ").strip().lower()
                    if confirm == 'y':
                        old_name = c["name"]
                        c["name"] = new_name
                        customer.save()
                        db.log_audit(c["customer_id"], "NAME_CHANGE", "customers",
                                     c["customer_id"],
                                     f"Name changed: {old_name} → {new_name}")
                        print(f"  ✓ Name updated to {new_name}.")
                    else:
                        print("  Cancelled.")
            input("  Press Enter…")

        elif ch == '2':
            # Structured address input for Ghana Health Service compatibility
            print_header("UPDATE ADDRESS")
            print("  Your address helps us coordinate health services if needed.")
            print("  This information is shared with the Ghana Health Service")
            print("  and support public health programmes in your community.")
            print("  Leave any field blank to keep current value.\n")
            current_addr = c.get("address", "")
            # Parse existing if structured
            print(f"  Current address: {current_addr or 'Not set'}")
            print()
            region   = input("  Region (e.g. Greater Accra): ").strip()
            district = input("  District / Area: ").strip()
            town     = input("  Town / Community: ").strip()
            street   = input("  Street / Landmark: ").strip()
            digital  = input("  Ghana Post GPS / Digital Address (optional): ").strip()

            # Only update fields that were provided
            parts = []
            if street:   parts.append(street)
            if town:     parts.append(town)
            if district: parts.append(district)
            if region:   parts.append(region)
            if digital:  parts.append(f"GPS:{digital}")

            if not parts:
                print("  No changes made.")
            else:
                new_addr = ", ".join(parts)
                print(f"\n  New address: {new_addr}")
                confirm = input("  Confirm this address? (y/n): ").strip().lower()
                if confirm == 'y':
                    c["address"] = new_addr
                    customer.save()
                    db.log_audit(c["customer_id"], "ADDRESS_UPDATE", "customers",
                                 c["customer_id"], f"Address updated")
                    print("  ✓ Address saved.")
                else:
                    print("  Cancelled.")
            input("  Press Enter…")

        elif ch == '3':
            print_header("UPDATE CONTACT")
            print(f"  Current email  : {c.get('email','') or 'Not set'}")
            print(f"  Current phone  : {c.get('contact','') or 'Not set'}")
            print("  Leave blank to keep current value.\n")
            new_email   = input("  New email (blank to keep): ").strip()
            new_contact = input("  New phone (blank to keep): ").strip()
            if not new_email and not new_contact:
                print("  No changes made.")
            else:
                if new_email:   c["email"]   = new_email
                if new_contact: c["contact"] = new_contact
                confirm = input("  Save changes? (y/n): ").strip().lower()
                if confirm == 'y':
                    c["notification_email"] = 1 if c.get("email")   else 0
                    c["notification_sms"]   = 1 if c.get("contact") else 0
                    customer.save()
                    print("  ✓ Contact updated.")
                else:
                    print("  Cancelled.")
            input("  Press Enter…")

        elif ch == '4':
            print_header("UPDATE HEALTH PROFILE")
            print("  Leave any field blank to keep current value.\n")

            print(f"  Current Allergies  : {c.get('allergies', 'None')}")
            new_allergies = input("  New allergies (e.g. penicillin, aspirin): ").strip()

            print(f"\n  Current Conditions : {c.get('chronic_conditions', 'None')}")
            new_conditions = input("  Chronic conditions (e.g. diabetes, hypertension): ").strip()

            print(f"\n  Current Medications: {c.get('current_medications', 'None')}")
            new_medications = input("  Current medications you take regularly: ").strip()

            print(f"\n  Current Blood Group: {c.get('blood_group', 'Unknown')}")
            new_blood = input("  Blood group (A+/A-/B+/B-/O+/O-/AB+/AB-): ").strip().upper()

            changes = False
            if new_allergies:
                c["allergies"]   = new_allergies
                c["health_info"] = new_allergies  # keep health_info in sync
                changes = True
            if new_conditions:
                c["chronic_conditions"] = new_conditions
                changes = True
            if new_medications:
                c["current_medications"] = new_medications
                changes = True
            if new_blood and new_blood in ("A+","A-","B+","B-","O+","O-","AB+","AB-"):
                c["blood_group"] = new_blood
                changes = True
            elif new_blood:
                print("  ✗ Invalid blood group format — not saved.")

            if changes:
                c["health_status"] = "Updated"
                customer.save()
                db.log_audit(c["customer_id"], "PROFILE_UPDATE", "customers",
                             c["customer_id"], "Health profile updated by customer")
                print("\n  ✓ Health profile saved. Nyansa will apply this immediately.")
                # Trigger Nyansa re-analysis with new profile
                try:
                    from aidplus.intelligence import NyansaEngine
                    eng = NyansaEngine(db)
                    eng.log_signal("HEALTH_INTAKE", "profile_update",
                                   district=c.get("address",""))
                except Exception:
                    pass
            else:
                print("  No changes made.")
            input("  Press Enter…")

        elif ch == 'H':
            print_header("ADD HEALTH READING")
            print("  Leave blank to skip any reading.\n")
            t = input("  Current Temperature °C: ").strip()
            if t:
                try:
                    db.add_health_trend(c["customer_id"], "temp", float(t))
                    print(f"  ✓ Temperature {t}°C saved.")
                except ValueError:
                    print("  Invalid value — numbers only.")
            w = input("  Current Weight kg: ").strip()
            if w:
                try:
                    db.add_health_trend(c["customer_id"], "weight", float(w))
                    print(f"  ✓ Weight {w}kg saved.")
                except ValueError:
                    print("  Invalid value — numbers only.")
            input("  Press Enter…")

        elif ch == '5':
            print_header("CHANGE PASSWORD")
            current_pwd = input("  Current password: ").strip()
            if not verify_password(c["password"],
                                   c.get("password_salt", ""),
                                   current_pwd):
                print("  ✗ Incorrect current password.")
            else:
                new_pwd = input("  New password (min 8 characters): ").strip()
                if len(new_pwd) < 8:
                    print("  ✗ Password too short (minimum 8 characters).")
                else:
                    confirm_pwd = input("  Confirm new password: ").strip()
                    if new_pwd != confirm_pwd:
                        print("  ✗ Passwords do not match.")
                    else:
                        db.update_password(c["customer_id"], new_pwd)
                        db.log_audit(c["customer_id"], "PASSWORD_CHANGE",
                                     "customers", c["customer_id"],
                                     "Password changed by customer from My Profile")
                        print("  ✓ Password updated successfully.")
            input("  Press Enter…")

        elif ch == '6':
            c["notification_email"] = 1 if input(
                f"  Email notifications? (y/n) [current: {'ON' if c.get('notification_email') else 'OFF'}]: "
            ).lower() == 'y' else 0
            c["notification_sms"] = 1 if input(
                f"  SMS notifications?   (y/n) [current: {'ON' if c.get('notification_sms') else 'OFF'}]: "
            ).lower() == 'y' else 0
            confirm = input("  Save notification settings? (y/n): ").strip().lower()
            if confirm == 'y':
                customer.save()
                print("  ✓ Notification settings saved.")
            else:
                print("  Cancelled.")
            input("  Press Enter…")

        elif ch == 'V':
            c["voice_guidance"] = 0 if c.get("voice_guidance") else 1
            confirm = input(
                f"  Turn voice guidance {'OFF' if c.get('voice_guidance') else 'ON'}? (y/n): "
            ).strip().lower()
            if confirm == 'y':
                customer.save()
                state = "enabled" if c["voice_guidance"] else "disabled"
                print(f"  ✓ Voice guidance {state}.")
                if c["voice_guidance"]:
                    speak("Voice guidance activated.", c)
            else:
                c["voice_guidance"] = 0 if c.get("voice_guidance") else 1  # revert
                print("  Cancelled.")
            input("  Press Enter…")

        elif ch == 'D':
            print_header("DELETE ACCOUNT")
            print("  ⚠  This action is permanent and cannot be undone.")
            print("  All your data, wallet balance and history will be removed.\n")
            confirm1 = input("  Are you sure you want to delete your account? (y/n): ").strip().lower()
            if confirm1 == 'y':
                confirm2 = input(f"  Type your Customer ID to confirm: ").strip()
                if confirm2 == c["customer_id"]:
                    db.delete_customer(c["customer_id"],
                                       actor_id=c["customer_id"])
                    customer.current = None
                    print("  Account permanently deleted.")
                    return 'deleted'
                else:
                    print("  ✗ ID mismatch. Deletion cancelled.")
            else:
                print("  Cancelled.")
            input("  Press Enter…")

        elif ch == '0':
            break
        else:
            print("  Invalid option.")
            input("  Press Enter…")


# ═══════════════════════════════════════════════════════════════════════════════
# PURCHASE HISTORY
# ═══════════════════════════════════════════════════════════════════════════════
def view_purchase_history(customer: Customer, db: DatabaseManager):
    """
    Purchase History — numbered receipt browser.
    Select any transaction number to view full receipt detail.
    """
    cid = customer.current["customer_id"]

    # Collect all transaction types
    drug_txs = [t for t in db.get_all_transactions() if t["customer_id"] == cid]
    svc_types = {"bill_pay": "Utilities", "ride_pay": "Ride Share",
                 "movie_tkt": "Entertainment", "Hardware Deposit": "Hardware",
                 "Hardware Refund": "Hardware"}
    svc_txs = [t for t in db.get_wallet_history(cid) if t["type"] in svc_types]

    combined = []
    for t in drug_txs:
        items = t.get("items", [])
        desc  = ", ".join(f"{i['qty']}x {i['name']}" for i in items)                 if items else "Dispensing event"
        combined.append({
            "timestamp":   t["timestamp"],
            "description": desc,
            "system":      "Pharmacy",
            "amount":      t.get("total", 0.0),
            "txn_id":      t.get("id", t.get("transaction_id", "—")),
            "items":       items,
            "type":        "drug",
        })
    for t in svc_txs:
        combined.append({
            "timestamp":   t["timestamp"],
            "description": t.get("description", ""),
            "system":      svc_types.get(t["type"], "Service"),
            "amount":      abs(t["amount"]),
            "txn_id":      t.get("id", "—"),
            "items":       [],
            "type":        t["type"],
        })

    combined.sort(key=lambda x: x["timestamp"], reverse=True)

    while True:
        os.system('cls' if os.name == 'nt' else 'clear')
        print_header("PURCHASE HISTORY  ·  ALL SYSTEMS")
        if not combined:
            print("  No transaction history found.")
            input("  Press Enter…")
            return

        W = 74
        print(f"  {'#':<4} {'Date & Time':<20}  {'System':<13}  {'Amount':>8}  Transaction ID")
        print("  " + "─" * W)
        for i, h in enumerate(combined, 1):
            dt  = h["timestamp"][:19].replace("T", " ")
            tid = str(h.get("txn_id", "—"))[:16]
            amt = f"₵{h['amount']:.2f}"
            print(f"  [{i:<2}] {dt:<20}  {h['system']:<13}  {amt:>8}  {tid}")
        print("  " + "─" * W)
        print("\n  Enter a number to view full receipt  |  [0] Back")
        ch = safe_input("  › ").strip()

        if ch == '0':
            return

        try:
            sel = int(ch) - 1
            if sel < 0 or sel >= len(combined):
                continue
            h = combined[sel]

            # ── Full receipt view ─────────────────────────────────────────────
            os.system('cls' if os.name == 'nt' else 'clear')
            print()
            print("  " + "═" * 60)
            print(f"  {'AID PLUS+  ·  TRANSACTION RECEIPT':^60}")
            print("  " + "═" * 60)
            print(f"  Date      : {h['timestamp'][:19].replace('T', ' ')}")
            print(f"  System    : {h['system']}")
            print(f"  Txn ID    : {h.get('txn_id', '—')}")
            print(f"  Type      : {h['type'].replace('_', ' ').title()}")
            print("  " + "─" * 60)

            if h["items"]:
                print(f"  {'Item':<30} {'Qty':>4}  {'Price':>8}  {'Total':>8}  Status")
                print("  " + "─" * 70)
                for item in h["items"]:
                    sub  = item["qty"] * item["price_per"]
                    name = item["name"][:30]
                    # Fetch return status
                    with db._conn() as _hc:
                        rr = _hc.execute(
                            "SELECT COALESCE(SUM(qty_returned),0), MAX(status) "
                            "FROM medication_returns "
                            "WHERE transaction_id=? AND item_id=?",
                            (str(h.get("txn_id","")),
                             item.get("id", item.get("shelf_num", 0)))
                        ).fetchone()
                    rqty = rr[0] or 0
                    rst  = rr[1] or ""
                    if rqty >= item["qty"]:
                        stag = "↩ Returned"
                    elif rqty > 0:
                        stag = f"↩ {rqty}/{item['qty']} returned"
                    elif "donated" in rst:
                        stag = "♻ Donated"
                    else:
                        stag = "✓ Purchased"
                    print(f"  {name:<30} {item['qty']:>4}  "
                          f"₵{item['price_per']:>7.2f}  ₵{sub:>8.2f}  {stag}")
                print("  " + "─" * 70)
            else:
                # Service transaction — display full description
                stype = h.get("type", "")
                print(f"  Type    : {h['system']}")
                print(f"  Details : {h['description']}")
                print("  " + "─" * 60)

            print(f"  {'TOTAL':>52}  ₵{h['amount']:>7.2f}")
            print("  " + "═" * 60)

            # Check if this transaction has been returned
            with db._conn() as con:
                ret = con.execute(
                    "SELECT item_name, qty_returned, amount_refunded, status, returned_at "
                    "FROM medication_returns WHERE transaction_id=?",
                    (str(h.get("txn_id", "")),)).fetchall()
            if ret:
                print("\n  RETURN HISTORY:")
                for r in ret:
                    print(f"    {r[0] or 'Full transaction':<28} "
                          f"qty:{r[1]}  refund:₵{r[2]:.2f}  "
                          f"{r[3]}  {str(r[4])[:10]}")
            print()
            input("  Press Enter to go back…")

        except (ValueError, IndexError):
            continue


def _nyansa_drug_lookup(drug_name: str) -> dict | None:
    """Fuzzy match drug name against Nyansa knowledge base."""
    name = drug_name.lower().strip()
    for key in DRUG_ADVICE:
        if key in name or name in key:
            return DRUG_ADVICE[key]
    return None

def get_personalized_review(customer: Customer, db: DatabaseManager):
    """
    Nyansa Personalised Health Review.
    Checks customer health profile against every drug they recently purchased.
    Uses DRUG_ADVICE for clinical guidance and HEALTH_CONDITION_WARNINGS
    for condition-specific interaction alerts.
    Updates dynamically whenever customer profile changes.
    """
    import os as _os
    _os.system('cls' if _os.name == 'nt' else 'clear')
    print_header("NYANSA HEALTH REVIEW")
    c   = customer.current
    cid = c["customer_id"]
    age = c.get("age", 0)

    # ── Build patient profile from all health fields ──────────────────────────
    raw_allergies   = c.get("allergies",           "").lower()
    raw_conditions  = c.get("chronic_conditions",   "").lower()
    raw_medications = c.get("current_medications",  "").lower()
    raw_health_info = c.get("health_info",           "").lower()
    full_profile    = f"{raw_allergies} {raw_conditions} {raw_health_info}"

    W = 62
    print(f"\n  Patient   : {c['name']}  |  Age: {age}  |  Blood: {c.get('blood_group','—')}")
    print(f"  Allergies : {c.get('allergies','None recorded')}")
    print(f"  Conditions: {c.get('chronic_conditions','None recorded')}")
    if raw_medications:
        print(f"  Current Medications: {c.get('current_medications')}")
    print(f"  {'─'*W}")

    # ── Recent purchases ──────────────────────────────────────────────────────
    with db._conn() as con:
        # Exclude fully-returned items from Nyansa advice
        recent_rows = con.execute(
            "SELECT ti.id, ti.name, ti.qty, ti.price_per, t.timestamp, ti.transaction_id "
            "FROM transaction_items ti "
            "JOIN transactions t ON ti.transaction_id = t.id "
            "WHERE t.customer_id=? "
            "AND COALESCE(t.status,'completed') != 'donated' "
            "ORDER BY t.timestamp DESC LIMIT 15",
            (cid,)).fetchall()
        recent = []
        for row in recent_rows:
            ret_qty = con.execute(
                "SELECT COALESCE(SUM(qty_returned),0) FROM medication_returns "
                "WHERE transaction_id=? AND item_id=?",
                (row["transaction_id"], row["id"])).fetchone()[0]
            net_qty = row["qty"] - ret_qty
            if net_qty > 0:
                recent.append({
                    "name": row["name"], "qty": net_qty,
                    "price_per": row["price_per"], "timestamp": row["timestamp"]
                })

    # ── Drug advice for each recent purchase ─────────────────────────────────
    flagged_warnings = []

    if recent:
        print(f"\n  RECENT PURCHASES — NYANSA ADVICE:")
        print(f"  {'─'*W}")
        for item in recent:
            drug_name = item["name"]
            advice    = _nyansa_drug_lookup(drug_name)
            dt        = item["timestamp"][:10] if item["timestamp"] else "—"
            print(f"\n  ◆ {drug_name.upper()}  (×{item['qty']} on {dt})")

            if advice:
                print(f"    Use    : {advice['use']}")
                print(f"    Dose   : {advice['dose']}")
                print(f"    Food   : {advice['food']}")
                if advice.get("warn"):
                    print(f"    Caution: {advice['warn']}")

                # Check contraindications
                for contra in advice.get("contra", []):
                    if contra.lower() in full_profile:
                        msg = f"⚠ {drug_name}: CONTRAINDICATED — profile lists '{contra}'"
                        flagged_warnings.append(msg)
                        print(f"    {msg}")

                # Pregnancy warnings
                if "pregnancy" in full_profile and advice.get("preg"):
                    preg_note = advice["preg"]
                    if any(w in preg_note.lower() for w in ["avoid", "caution", "not recommended"]):
                        msg = f"⚠ {drug_name}: Pregnancy note — {preg_note}"
                        flagged_warnings.append(msg)
                        print(f"    ⚕ Pregnancy: {preg_note}")
            else:
                print(f"    Nyansa has no specific guidance for this item.")
                print(f"    Read the package leaflet carefully.")
    else:
        print("\n  No recent purchases found.")

    # ── Condition-based advice ────────────────────────────────────────────────
    print(f"\n  {'─'*W}")
    print("  CONDITION-BASED ADVICE:")
    found_condition_advice = False
    for condition, advice in HEALTH_CONDITION_WARNINGS.items():
        # Fuzzy match: check if any word in the condition key appears in the patient profile
        cond_words = condition.lower().replace(' ', '_').split('_') + condition.lower().split()
        cond_words = [w for w in cond_words if len(w) > 3]  # skip short words
        matched = any(w in full_profile for w in cond_words)
        # Also check direct substring
        if not matched:
            matched = condition.lower() in full_profile

        if matched:
            found_condition_advice = True
            print(f"\n  [{condition.upper()}]")
            print(f"    ⚠  {advice['warning']}")
            print(f"    ✓  {advice['action']}")
            if recent:
                recent_names = [r["name"].lower() for r in recent]
                for concern in advice["check_drugs"]:
                    if any(concern.lower() in n for n in recent_names):
                        msg = f"ACTIVE CONCERN: {condition} × {concern}"
                        if msg not in flagged_warnings:
                            flagged_warnings.append(msg)
                            print(f"    ❗ {msg}")


    # ── Warnings summary ─────────────────────────────────────────────────────
    if flagged_warnings:
        print(f"\n  ╔{'═'*W}╗")
        print(f"  ║{'⚠  IMPORTANT CLINICAL WARNINGS'.center(W)}║")
        print(f"  ╠{'═'*W}╣")
        for w in flagged_warnings:
            for chunk in [w[i:i+W-4] for i in range(0, len(w), W-4)]:
                print(f"  ║  {chunk.ljust(W-2)}║")
        print(f"  ╚{'═'*W}╝")

    # ── Disclaimer ───────────────────────────────────────────────────────────
    print(f"\n  {'─'*W}")
    print("  ⚕  Nyansa provides general guidance only. It does not replace")
    print("     a pharmacist, doctor, or qualified medical professional.")
    print("     For persistent symptoms, prescriptions, or emergencies —")
    print("     visit a clinic or hospital immediately.")
    print(f"  {'─'*W}")

    # ── Nyansa Consumption Probability Tracker ───────────────────────────
    try:
        from aidplus.intelligence import analyse_customer_drug_consumption
        consumption = analyse_customer_drug_consumption(c["customer_id"], db)
        if consumption:
            print(f"\n  {'─'*W}")
            print("  🔬 NYANSA — DRUG CONSUMPTION TRACKER")
            print(f"  {'─'*W}")
            for item in consumption[:8]:
                pct  = item.get("consumed_pct") or 0
                bar  = ("█" * int(pct * 10)).ljust(10, "░")
                print(f"  ◆ {item['drug'][:30]:<30}  [{bar}] {int(pct*100):>3}%")
                print(f"    {item['message']}")
                if item["status"] == "likely_remaining" and item["days_ago"] > (item["days_expected"] or 0) * 2:
                    print("    ⚠  Overdue — check if course was completed")
            print(f"  {'─'*W}")
    except Exception:
        pass

    input("\n  Press Enter to return…")


def report_found_card(customer: Customer, db: DatabaseManager):
    print_header("REPORT FOUND CARD")
    cid = input("Enter/scan the ID on the found card: ").strip()
    c   = db.get_customer(cid)
    if c:
        db.send_notification(cid, "LOST CARD REPORTED",
            "Someone found your Membership Card and reported it at a terminal. "
            "Visit the nearest service centre for pickup.")
        print(f"✅ Notification sent to {c['name']}. Please drop card in secure box.")
    else: print("❌ ID not recognised by the system.")
    input("\nPress Enter…")


# ═══════════════════════════════════════════════════════════════════════════════
# CUSTOMER MAIN MENU
# ═══════════════════════════════════════════════════════════════════════════════
def customer_profile_menu(db: DatabaseManager, customer: Customer,
                          connectivity: 'ConnectivityManager' = None):
    """
    [GUI-READY] → CustomerDashboard screen.
    Shows personalised dashboard with tier, balance, and all service options.
    connectivity passed in to handle teleconsult offline redirect.
    """
    while True:
        # Reload from DB every cycle — ensures loyalty points, balance,
        # bonus all reflect the latest values after any operation
        _fresh = db.get_customer(customer.current["customer_id"])
        if _fresh:
            customer.current.update(_fresh)
        c          = customer.current
        first_name = c["name"].split()[0].title()
        tier_name  = WALLET_TIERS[c.get("wallet_tier","G0")]["name"]
        inbox_cnt  = len(db.get_notifications(c["customer_id"]))
        notif_tag  = f"  ({inbox_cnt} new)" if inbox_cnt else ""
        lim_used   = customer.get_daily_slot_count()    # 24h slot rule
        lim_reset  = customer.get_purchase_limit_reset_time()
        balance    = c.get("balance", 0.0)
        bonus      = c.get("bonus",   0.0)
        points     = c.get("loyalty_points", 0)

        os.system('cls' if os.name == 'nt' else 'clear')

        # ── Build display values ──────────────────────────────────────────────
        W    = 60
        dbar = '═' * W
        bar  = '─' * W
        until_next = (LOYALTY_REDEEM_THRESHOLD - (points % LOYALTY_REDEEM_THRESHOLD)) \
                     if points % LOYALTY_REDEEM_THRESHOLD != 0 else 0
        pts_line = (f"{points} pts  ·  {until_next} more = ₵{LOYALTY_BONUS_PER_REDEEM:.2f} bonus"
                    if until_next > 0 else
                    f"{points} pts  ·  Ready to redeem!")
        lim_line = f"Today: {lim_used}/{TOTAL_ITEM_LIMIT} purchases  ·  Resets {lim_reset}"
        from datetime import datetime as _dt
        now_str = _dt.now().strftime('%H:%M  ·  %d %b %Y')

        print()
        print(f"  ╔{dbar}╗")
        print(f"  ║{'AID PLUS+  ·  Nyansa v8.0':^{W}}║")
        print(f"  ╠{dbar}╣")
        print(f"  ║  Hello, {first_name}!{now_str:>{W-11}}║")
        print(f"  ╠{dbar}╣")
        print(f"  ║  ID       {c['customer_id']:<15}  Tier   {tier_name:<15}  ║")
        print(f"  ║  Balance  ₵{balance:<14.2f}  Bonus  ₵{bonus:<14.2f}  ║")
        print(f"  ║  {pts_line:<{W-2}}║")
        print(f"  ║  {lim_line:<{W-2}}║")
        print(f"  ╠{dbar}╣")
        print(f"  ║  {'PHARMACY':^{W}}║")
        print(f"  ║  {bar}  ║")
        print(f"  ║   [1]  Browse Inventory     [2]  Buy Medication       ║")
        print(f"  ║   [8]  Return Medication    [K]  Return Cupsule       ║")
        print(f"  ║   [3]  Nyansa Health Review                           ║")
        print(f"  ║   [RX] Request Prescription (Teleconsult)             ║")
        print(f"  ╠{dbar}╣")
        print(f"  ║  {'WALLET & SERVICES':^{W}}║")
        print(f"  ║  {bar}  ║")
        print(f"  ║   [4]  Wallet & Top Up      [L]  Loyalty Points       ║")
        print(f"  ║   [5]  Utilities & Services                           ║")
        print(f"  ╠{dbar}╣")
        print(f"  ║  {'ACCOUNT':^{W}}║")
        print(f"  ║  {bar}  ║")
        print(f"  ║   [6]  Messages{notif_tag:<12}   [7]  My Profile          ║")
        print(f"  ║   [9]  Purchase History     [S]  Support              ║")
        print(f"  ╠{dbar}╣")
        print(f"  ║   [0]  Logout{' ':>{W-11}}║")
        print(f"  ╚{dbar}╝")
        print()
        try:
            ch = safe_input("  › ").strip().upper()
        except (KeyboardInterrupt, EOFError):
            break

        if   ch == '1':
            browse_inventory_flow(db, customer)
        elif ch == '2': buy_drugs_flow(db, customer)
        elif ch == '3': get_personalized_review(customer, db)
        elif ch == 'RX': request_prescription_teleconsult(customer, db)
        elif ch == '4': manage_wallet(customer, db)
        elif ch == '5': utilities_and_services_menu(customer, db)
        elif ch == '6': message_hub(customer, db)
        elif ch == '7':
            result = manage_customer_profile(customer, db)
            if result == 'deleted': break
        elif ch == '8':
            # RETURN MEDICATION
            # Two paths:
            #   Actual customer (logged in): QR scan → 2% penalty deducted → refund
            #   Bystander who found dropped drugs: ID entry → reward loyalty points
            print_header("RETURN MEDICATION")
            print()
            print("  [1]  I purchased this medication — I want to return it")
            print("  [2]  I found this medication — returning on someone's behalf")
            print("  [0]  Back")
            rtype = safe_input("  › ").strip()

            if rtype == '1':
                # ── Customer return — show all returnable items, loop until done ──
                print()
                cid_ret = c["customer_id"]
                while True:
                    # Build list of returnable transactions (within 10 min)
                    from datetime import datetime as _dtr
                    with db._conn() as _con:
                        _raw = [dict(r) for r in _con.execute(
                            "SELECT t.id, t.timestamp, t.total "
                            "FROM transactions t "
                            "WHERE t.customer_id=? "
                            "AND COALESCE(t.status,'completed') != 'donated' "
                            "ORDER BY t.timestamp DESC LIMIT 20",
                            (cid_ret,)).fetchall()]
                        # Keep only transactions that still have returnable items
                        recent_txns = []
                        for _t in _raw:
                            _rows = _con.execute(
                                "SELECT ti.qty, "
                                "COALESCE((SELECT SUM(mr.qty_returned) "
                                " FROM medication_returns mr "
                                " WHERE mr.transaction_id=ti.transaction_id "
                                " AND mr.item_id=ti.id),0) "
                                "FROM transaction_items ti WHERE ti.transaction_id=?",
                                (_t["id"],)).fetchall()
                            if any(row[0] - row[1] > 0 for row in _rows):
                                recent_txns.append(_t)
                    if not recent_txns:
                        print("  No recent transactions found.")
                        input("  Press Enter…"); break

                    # Show returnable transactions with return-window indicators
                    from datetime import datetime as _dts
                    _now = _dts.now()
                    print("  " + "─" * 68)
                    print(f"  {'#':<4} {'Transaction ID':<18} {'Date & Time':<20} {'Age':<14} {'Total':>7}")
                    print("  " + "─" * 68)
                    for j, txn in enumerate(recent_txns, 1):
                        dt    = txn["timestamp"][:16].replace("T", " ")
                        mins  = (_now - _dts.fromisoformat(txn["timestamp"][:19])).total_seconds() / 60
                        age   = f"{int(mins)}m" if mins < 60 else f"{int(mins//60)}h {int(mins%60)}m"
                        mark  = " ⏱ refundable" if mins <= 10 else (" 📦 donate only" if mins > 1440 else " ✗ no refund")
                        print(f"  [{j}]  {str(txn['id'])[:16]:<18} {dt:<20} {age+mark:<14} ₵{txn['total']:>7.2f}")
                    print("  " + "─" * 68)
                    print("  ⏱ = refundable  ✗ = no refund  📦 = donate via CUPSCAN")
                    print("  Enter number to select  |  [0] Done")
                    txn_sel = safe_input("  › ").strip()
                    if txn_sel == '0':
                        break
                    try:
                        txn_row_raw = dict(recent_txns[int(txn_sel)-1])
                    except (ValueError, IndexError):
                        print("  Invalid selection."); input("  Press Enter…"); continue

                    qr_data = str(txn_row_raw["id"])
                    from datetime import datetime as _dtr2
                    purchased_at = _dtr2.fromisoformat(txn_row_raw["timestamp"][:19])
                    mins_ago     = (_dtr2.now() - purchased_at).total_seconds() / 60
                    total_txn    = float(txn_row_raw["total"])

                    if mins_ago > 1440:
                        # After 24h — no refund; route through CUPSCAN donation
                        print(f"\n  This transaction is {int(mins_ago//60)}h old.")
                        print("  No refund is available after 24 hours.")
                        print("  You may donate the medication/packaging via CUPSCAN.")
                        print("  You will earn loyalty points for clean empty returns.")
                        if safe_input("  Proceed with donation? (y/n): ").lower() != 'y':
                            continue
                        has_drugs = safe_input(
                            "  Are there drugs still inside the packaging? (y/n): ").lower() == 'y'
                        # Get items from this transaction
                        with db._conn() as _rcon:
                            t_items_all = [dict(r) for r in _rcon.execute(
                                "SELECT id, name, qty FROM transaction_items "
                                "WHERE transaction_id=?", (qr_data,)).fetchall()]
                        # Record all items as donated
                        pts = 0
                        with db._conn() as _con:
                            for it in t_items_all:
                                _con.execute(
                                    "INSERT INTO medication_returns "
                                    "(transaction_id, item_id, item_name, qty_returned,"
                                    " customer_id, returned_at, amount_refunded, status) "
                                    "VALUES (?,?,?,?,?,?,0.0,?)",
                                    (qr_data, it["id"], it["name"], it["qty"],
                                     cid_ret, _dtr2.now().isoformat(),
                                     "donated_with_drugs" if has_drugs else "donated"))
                        # Award points only if packaging returned empty
                        if not has_drugs:
                            pts = CUPSULE_POINTS_INTACT
                            db.cupscan_apply_points(cid_ret, pts, "DONATION_EMPTY")
                            db.send_notification(cid_ret, "Donation Reward",
                                f"+{pts} loyalty point for returning empty packaging.")
                            print(f"  ✅ Empty packaging accepted. +{pts} loyalty point.")
                        else:
                            print("  ⚠  Packaging with drugs will be safely disposed by maintenance.")
                        # Mark transaction as donated so it no longer appears as returnable
                        with db._conn() as _upd:
                            _upd.execute(
                                "UPDATE transactions SET status='donated' WHERE id=?",
                                (qr_data,))
                        db.log_audit(cid_ret, "RETURN_DONATED",
                                     detail=f"Txn:{qr_data} has_drugs:{has_drugs} pts:{pts}")
                        print("  Transaction removed from your return list.")
                        input("  Press Enter…"); continue

                    if mins_ago > 10:
                        print(f"\n  ✗ This transaction is {int(mins_ago)} minutes old.")
                        print("  Returns only accepted within 10 minutes of purchase.")
                        input("  Press Enter…"); continue

                    # ── Within 10 minutes — show items ───────────────────────────
                    with db._conn() as _con:
                        t_items = [dict(r) for r in _con.execute(
                            "SELECT id, name, qty, price_per FROM transaction_items "
                            "WHERE transaction_id=?", (qr_data,)).fetchall()]

                    # Only show items not fully returned yet
                    # Get total qty already returned per item in one clean query
                    with db._conn() as _rc:
                        returned_qtys = {
                            row[0]: row[1]
                            for row in _rc.execute(
                                "SELECT item_id, SUM(qty_returned) "
                                "FROM medication_returns "
                                "WHERE transaction_id=? GROUP BY item_id",
                                (qr_data,)).fetchall()
                        }
                    returnable = []
                    for it in t_items:
                        ret_qty   = returned_qtys.get(it["id"], 0)
                        remaining = it["qty"] - ret_qty
                        if remaining > 0:
                            it_copy = dict(it)
                            it_copy["qty"] = remaining
                            returnable.append(it_copy)

                    if not returnable:
                        print("  ✓ All items in this transaction have been returned.")
                        input("  Press Enter…"); continue

                    print(f"\n  Transaction {qr_data[:16]}  —  returnable items:")
                    print(f"  ─" * 30)
                    for j, it in enumerate(returnable, 1):
                        sub = it["qty"] * it["price_per"]
                        print(f"  [{j}] {it['name']:<32} x{it['qty']}  ₵{sub:.2f}")
                    print(f"  [A] Return all  [0] Cancel")
                    sel = safe_input("  › ").strip().upper()

                    if sel == '0':
                        continue
                    if sel == 'A':
                        selected = [dict(i) for i in returnable]
                    else:
                        try:
                            item = dict(returnable[int(sel)-1])
                        except (ValueError, IndexError):
                            print("  Invalid."); input("  Press Enter…"); continue
                        if item["qty"] > 1:
                            print(f"  You have {item['qty']} × {item['name']}.")
                            qty_in = input(f"  How many to return? (1–{item['qty']}): ").strip()
                            try:
                                qty_ret = int(qty_in)
                                if not 1 <= qty_ret <= item["qty"]:
                                    raise ValueError
                                item["qty"] = qty_ret
                            except ValueError:
                                print("  Invalid quantity."); input("  Press Enter…"); continue
                        selected = [item]

                    ret_total = sum(i["qty"] * i["price_per"] for i in selected)
                    fee       = round(ret_total * 0.05, 2)
                    refund    = round(ret_total - fee, 2)
                    items_desc = ", ".join(f"{i['qty']}x {i['name']}" for i in selected)

                    print(f"\n  Returning  : {items_desc}")
                    print(f"  Subtotal   : ₵{ret_total:.2f}")
                    print(f"  Fee (5%)   : ₵{fee:.2f}")
                    print(f"  Refund     : ₵{refund:.2f}")
                    sealed_ret = safe_input(
                        "  Is the packaging sealed/unopened? (y/n): ").lower()
                    if sealed_ret == "y":
                        print("  ℹ  Sealed — recorded as unused.")
                    else:
                        print("  ℹ  Opened — recorded as used/partial.")
                    if safe_input("  Confirm return? (y/n): ").lower() != 'y':
                        continue

                    # ── Process refund ────────────────────────────────────────
                    customer.current["balance"] = round(
                        customer.current.get("balance", 0.0) + refund, 2)
                    customer.save()
                    db.add_wallet_entry(cid_ret, "return", refund,
                                        f"Return: {items_desc} | Txn: {qr_data[:10]}")
                    with db._conn() as _con:
                        for it in selected:
                            _con.execute(
                                "INSERT INTO medication_returns "
                                "(transaction_id, item_id, item_name, qty_returned,"
                                " customer_id, returned_at, amount_refunded, status) "
                                "VALUES (?,?,?,?,?,?,?,'refunded')",
                                (qr_data, it["id"], it["name"], it["qty"],
                                 cid_ret, _dtr2.now().isoformat(),
                                 round(it["qty"] * it["price_per"] * 0.95, 2)))
                    # Reverse bonus cashback earned on returned items (5% was given at purchase)
                    bonus_earned_on_returned = round(ret_total * BONUS_RATE, 2)
                    cur_bonus = customer.current.get("bonus", 0.0)
                    bonus_reversed = min(bonus_earned_on_returned, cur_bonus)
                    if bonus_reversed > 0:
                        customer.current["bonus"] = round(cur_bonus - bonus_reversed, 2)
                        customer.save()
                        db.add_wallet_entry(
                            cid_ret, "bonus_deduct", -bonus_reversed,
                            f"Bonus reversed on return: {len(selected)} item(s)")
                    db.log_audit(cid_ret, "RETURN_ACCEPTED", "medication_returns", qr_data,
                                 f"{len(selected)} item(s) | refund ₵{refund:.2f} | "
                                 f"bonus reversed ₵{bonus_reversed:.2f}")
                    speak(f"Return accepted. ₵{refund:.2f} refunded.", c)
                    print(f"  ✅ ₵{refund:.2f} added to your wallet.")
                    if bonus_reversed > 0:
                        print(f"  ℹ  ₵{bonus_reversed:.2f} bonus credit reversed "
                              f"(was earned at purchase time).")
                    # Check if all items in this transaction are now returned
                    with db._conn() as _cc:
                        _remaining = _cc.execute(
                            "SELECT COUNT(*) FROM transaction_items ti "
                            "WHERE ti.transaction_id=? "
                            "AND ti.qty > COALESCE(("
                            "  SELECT SUM(mr.qty_returned) FROM medication_returns mr "
                            "  WHERE mr.transaction_id=ti.transaction_id "
                            "  AND mr.item_id=ti.id),0)",
                            (qr_data,)).fetchone()[0]
                    # ── Always clear returned items from active cart ──────────
                    # Fix B29: previously only cleared on full return (_remaining==0).
                    # Partial returns left returned drugs in customer_cart, blocking
                    # new purchases of the same drug. Now always removes them.
                    with db._conn() as _cc2:
                        for _it in selected:
                            _cc2.execute(
                                "DELETE FROM customer_cart WHERE customer_id=? "
                                "AND shelf_num IN ("
                                "  SELECT shelf_num FROM transaction_items "
                                "  WHERE transaction_id=? AND name=?)",
                                (cid_ret, qr_data, _it["name"]))
                    if hasattr(customer, "current") and customer.current:
                        _returned_names = {_i["name"] for _i in selected}
                        customer.current["cart"] = [
                            _i for _i in customer.current.get("cart", [])
                            if _i.get("name") not in _returned_names
                        ]
                    if _remaining == 0:
                        print(f"  ✅ All items from this transaction returned. Cart cleared.")
                    else:
                        print(f"  ✅ Returned items removed. You can return more items if needed.")
                    input("  Press Enter to continue or go back to menu…")

            elif rtype == '2':
                # ── BYSTANDER RETURN — reward finder ─────────────────────────
                print()
                print("  Thank you for returning found medication.")
                print("  Please enter the ID on the medication packaging")
                print("  or the Transaction ID on the receipt if found with it.")
                print()
                txn_id = input("  Transaction ID (or press Enter to skip): ").strip()
                finder_id = input("  Your AID PLUS+ Customer ID (or press Enter if not a customer): ").strip()

                is_fraud = False
                orig_cid = None

                if txn_id:
                    with db._conn() as _con:
                        txn_row = _con.execute(
                            "SELECT * FROM transactions WHERE id=? OR transaction_id=?",
                            (txn_id, txn_id)).fetchone()
                    if txn_row:
                        orig_cid = txn_row["customer_id"]
                        # ── FRAUD CHECK: is the finder the original buyer? ─────
                        if finder_id and finder_id == orig_cid:
                            is_fraud = True
                            db.log_audit(orig_cid, "FRAUD_BYSTANDER_SELF",
                                         "transactions", txn_id,
                                         "Customer entered own transaction ID on bystander path")
                            print()
                            print("  ⚠  This transaction belongs to your account.")
                            print("  You are using the 'Found medication' path for your own purchase.")
                            print()
                            print("  You CAN still return it — but with a higher 10% penalty fee")
                            print("  (vs 5% on Option 1) and NO loyalty points.")
                            print("  No Cupsule drop required for this path.")
                            print()
                            proceed = safe_input("  Process penalised return? (y/n): ").lower()
                            if proceed == "y":
                                # SECURITY: must drop physical drugs in CUPSCAN first
                                print()
                                print("  ⚠  SECURITY STEP REQUIRED")
                                print("  You must drop the physical medication into the")
                                print("  CUPSCAN unit before the refund is processed.")
                                print("  This prevents retaining the drugs while claiming a refund.")
                                print()
                                # Ask seal status
                                sealed = safe_input(
                                    "  Is the packaging sealed/unopened? (y/n): ").lower()
                                if sealed == "y":
                                    print("  ℹ  Sealed packaging returned — noted as unused.")
                                else:
                                    print("  ℹ  Opened packaging — noted as used/partial.")
                                dropped = safe_input(
                                    "  Confirm you are now dropping the drugs in the CUPSCAN (y/n): "
                                ).lower()
                                if dropped != "y":
                                    print("  Refund not processed — drugs must be surrendered.")
                                    input("  Press Enter…")
                                else:
                                    # Process refund only after physical confirmation
                                    with db._conn() as _fr:
                                        txn_total = _fr.execute(
                                            "SELECT total FROM transactions WHERE id=?",
                                            (txn_id,)).fetchone()
                                    if txn_total:
                                        total_amt = float(txn_total[0])
                                        penalty   = round(total_amt * 0.10, 2)
                                        refund    = round(total_amt - penalty, 2)
                                        fr_cust   = db.get_customer(orig_cid)
                                        if fr_cust:
                                            fr_cust["balance"] = round(
                                                fr_cust.get("balance", 0.0) + refund, 2)
                                            db.save_customer(fr_cust)
                                            db.add_wallet_entry(
                                                orig_cid, "return", refund,
                                                f"Penalised return 10% | bystander path | "
                                                f"sealed:{sealed == 'y'} | {txn_id}")
                                            with db._conn() as _fri:
                                                _fri.execute(
                                                    "UPDATE transactions SET status='returned' WHERE id=?",
                                                    (txn_id,))
                                        db.log_audit(orig_cid, "RETURN_PENALISED_BYSTANDER",
                                                     "transactions", txn_id,
                                                     f"10% penalty | refund ₵{refund:.2f} | "
                                                     f"sealed:{sealed=='y'} | drugs surrendered")
                                        print(f"  ✅ ₵{refund:.2f} refunded (10% penalty applied).")
                                        print("  No loyalty points awarded.")
                                    input("  Press Enter…")
                            else:
                                print("  Return cancelled. Use Option [1] for standard 5% return.")
                            input("  Press Enter…")
                        else:
                            # Legitimate found medication — notify original buyer
                            db.send_notification(orig_cid, "Medication Found",
                                "Your medication was found and returned to an AID PLUS+ terminal. "
                                "Please visit to collect or confirm. "
                                f"Reference: {txn_id}")
                            db.log_audit("SYSTEM", "MEDICATION_FOUND",
                                         "transactions", txn_id,
                                         f"Found by {'customer:'+finder_id if finder_id else 'anonymous'}")
                    else:
                        # Transaction ID not found in system
                        print("  ✗ Transaction ID not recognised in our system.")
                        if finder_id:
                            db.log_audit(finder_id, "BYSTANDER_INVALID_TXN",
                                         detail=f"Entered unknown txn_id: {txn_id}")
                        input("  Press Enter…")

                if not is_fraud and finder_id:
                    finder = db.get_customer(finder_id)
                    if finder:
                        # Only reward if finder is NOT the original buyer
                        if orig_cid and finder_id == orig_cid:
                            print("  No reward issued — you are the original purchaser.")
                        else:
                            reward = 5
                            db.cupscan_apply_points(finder_id, reward, "GOOD_SAMARITAN")
                            db.send_notification(finder_id, "Good Samaritan Reward 🌟",
                                f"+{reward} loyalty points for returning found medication.")
                            print(f"  ✅ Thank you! +{reward} loyalty points added to your account.")
                    else:
                        print("  ✅ Thank you for returning this medication.")
                        print("     Create an AID PLUS+ account to earn points on future returns.")
                elif not is_fraud:
                    print("  ✅ Thank you for returning this medication. Your good deed is noted.")
                input("  Press Enter…")


        elif ch == 'L':
            while True:
                print_header("LOYALTY STATUS")
                print(f"Current points:  {c.get('loyalty_points',  0)}")
                print(f"Lifetime earned: {c.get('lifetime_points', 0)}")
                print("1 Cupsule returned = 1 point  |  10 points = \u20b91.00 bonus  |  Points never expire")
                print("  Bonus redeemable on drugs, ride tickets, and AidPlus products.")
                print("\n[R] Redeem  [0] Back")
                lch = input("Action > ").strip().upper()
                if lch == 'R':
                    ok, msg = customer.redeem_loyalty_points()
                    if not ok: print(f"❌ {msg}")
                    input("\nPress Enter…"); break
                elif lch == '0': break
        elif ch == 'S':
            # Teleconsult requires connectivity — redirect if offline
            conn_ok = (connectivity is None or connectivity.is_online)
            if not conn_ok:
                os.system('cls' if os.name == 'nt' else 'clear')
                deep_link = connectivity.teleconsult_qr_redirect(
                    customer_id=c.get("customer_id",""),
                    unit_id=ADWENE_SERIAL)
                print_header("TELECONSULT")
                ui_qr(deep_link,
                      "Teleconsult is not available on this terminal right now.",
                      "Open your Aid Plus app to connect with a consultant")
                ui_info([
                    "Scan the code above with your phone camera.",
                    "The Aid Plus app will open directly to teleconsult.",
                    "Your account details will be pre-loaded.",
                ], "normal")
                input("  Press Enter to return…")
            else:
                support_menu_customer(customer, db)
        elif ch == 'K': collocated_cupsule_return(customer, db)
        elif ch == '9': view_purchase_history(customer, db)
        elif ch == '0':
            speak("Logging out. Goodbye.", c)
            print(f"Goodbye, {c['name']}.")
            customer.current = None
            break
        else: print("Invalid."); input("\nPress Enter…")


# ═══════════════════════════════════════════════════════════════════════════════
# ADMIN MENU
# ═══════════════════════════════════════════════════════════════════════════════
def admin_check_stock(db: DatabaseManager):
    while True:
        print_header("ADMIN: INVENTORY MANAGEMENT")
        display_inventory(db)
        print("[1] Refill Upper Shelf  [2] Refill Lower Shelf  [0] Back")
        ch = input("> ").strip()
        if ch == '1':
            try:
                sn   = int(input("Upper shelf number (1–7): "))
                item = db.get_item_by_shelf(sn)
                if item and not item.get("is_mega"):
                    db.refill_upper(sn)
                    db.log_audit("ADMIN", "ADMIN_ACTION", "inventory", str(sn),
                                 f"Shelf {sn} refilled to {MAX_CAPS_PER_SHELF}")
                    print(f"Shelf {sn} refilled to {MAX_CAPS_PER_SHELF}.")
                else: print("Invalid upper shelf.")
            except ValueError: print("Invalid input.")
        elif ch == '2':
            try:
                sn   = int(input("Lower shelf number (8–13): "))
                item = db.get_item_by_shelf(sn)
                if item and item.get("is_mega"):
                    db.refill_mega(sn)
                    db.log_audit("ADMIN", "ADMIN_ACTION", "mega_inventory", str(sn),
                                 f"Shelf {sn} refilled to {MAX_MEGA_PER_SHELF}")
                    print(f"Shelf {sn} refilled to {MAX_MEGA_PER_SHELF}.")
                else: print("Invalid lower shelf.")
            except ValueError: print("Invalid input.")
        elif ch == '0': break
        else: print("Invalid.")
        input("\nPress Enter…")

def admin_check_customer_data(customer: Customer, db: DatabaseManager):
    while True:
        print_header("ADMIN: CUSTOMER DATA")
        customers = db.get_all_customers()    # [P1] batch-loaded
        if not customers: print("No accounts found."); break
        print(f"Total customers: {len(customers)}")
        for i, c in enumerate(customers):
            hist  = db.get_wallet_history(c["customer_id"])
            spent = sum(abs(e["amount"]) for e in hist
                        if e["amount"] < 0 and e["type"] in
                        ("purchase","bill_pay","ride_pay","movie_tkt","upgrade"))
            print(f"[{i+1:02d}] ID:{c['customer_id']} | {c['name']:<22}"
                  f"| ₵{c.get('balance',0.0):.2f} | Spent:₵{spent:.2f}")

        print("\n[V] View/Modify  [D] Delete  [R] Revoke Lock  [0] Back")
        try:
            ch = safe_input("Action > ").strip().upper()
        except (KeyboardInterrupt, EOFError):
            break

        if ch == 'V':
            try:
                idx = int(safe_input("  Customer #: ")) - 1
                if not (0 <= idx < len(customers)):
                    print("Invalid #."); continue
                c   = customers[idx]
                cid = c["customer_id"]
                print(f"\n--- {c['name']} ({cid}) ---")
                print(f"  Status: {c.get('status')} | Tier: {c.get('wallet_tier','G0')}"
                      f" | Balance: ₵{c.get('balance',0.0):.2f}")
                print(f"  Health: {c.get('health_info','—')}")
                mod = safe_input("  [1] Status  [2] Health  [3] Balance  [0] Back: ").strip()
                fc  = db.get_customer(cid)
                if mod == '1':
                    ns = safe_input("  New status (Active/Suspended/Terminated): ").strip().capitalize()
                    if ns in ('Active','Suspended','Terminated'):
                        old_status   = fc["status"]
                        fc["status"] = ns
                        db.save_customer(fc)
                        db.send_notification(cid,"Status Update",f"Status changed to {ns}.")
                        db.log_audit("ADMIN", "STATUS_CHANGE", "customers", cid,
                                     f"Status: {old_status} → {ns}")
                        print(f"Updated to {ns}.")
                    else: print("Invalid status.")
                elif mod == '2':
                    fc["health_status"] = safe_input("  New health status: ").strip().capitalize()
                    db.save_customer(fc); print("Updated.")
                elif mod == '3':
                    try:
                        adj = float(safe_input("  Balance adjustment (+/-): "))
                        old_bal      = fc.get("balance",0.0)
                        fc["balance"] = old_bal + adj
                        db.save_customer(fc)
                        db.add_wallet_entry(cid,"admin_adj",adj,"Admin Balance Adjustment")
                        db.log_audit("ADMIN", "BALANCE_ADJ", "customers", cid,
                                     f"₵{old_bal:.2f} → ₵{fc['balance']:.2f} (adj ₵{adj:+.2f})")
                        print(f"New balance: ₵{fc['balance']:.2f}")
                    except ValueError: print("Invalid amount.")
            except ValueError: print("Invalid input.")

        elif ch == 'D':
            try:
                idx = int(safe_input("  Customer # to DELETE: ")) - 1
                if 0 <= idx < len(customers):
                    c = customers[idx]
                    if safe_input(f"  Delete {c['name']}? (y/n): ").lower() == 'y':
                        db.delete_customer(c["customer_id"], actor_id="ADMIN")
                        print(f"Customer {c['customer_id']} and all data deleted.")
                    else: print("Cancelled.")
                else: print("Invalid #.")
            except ValueError: print("Invalid input.")

        elif ch == 'R':
            try:
                idx = int(safe_input("  Customer # to unlock: ")) - 1
                if 0 <= idx < len(customers):
                    c  = customers[idx]
                    fc = db.get_customer(c["customer_id"])
                    if fc.get("lockout_until"):
                        fc["lockout_until"]  = None
                        fc["status"]         = "Active"
                        fc["login_attempts"] = 0
                        db.save_customer(fc)
                        db.send_notification(c["customer_id"], "Lockout Revoked",
                                             "Your account lockout was manually revoked.")
                        db.log_audit("ADMIN", "UNLOCK", "customers", c["customer_id"],
                                     f"Lockout manually revoked for {c['name']}")
                        print(f"Lockout revoked for {c['name']}.")
                    else: print("Account is not currently locked.")
                else: print("Invalid #.")
            except ValueError: print("Invalid input.")

        elif ch == '0': break
        else: print("Invalid.")
        input("\nPress Enter…")

def admin_check_transactions(db: DatabaseManager):
    print_header("ADMIN: TRANSACTION HISTORY")
    txs = db.get_all_transactions()
    if not txs: print("No transactions recorded."); input("\nPress Enter…"); return
    print(f"Total transactions: {len(txs)}")
    for t in txs[:20]:
        items = t.get("items", [])
        info  = f"{len(items)} item(s)" if items else "Service/HW"
        print(f"[{t['timestamp'][:19]}] {t.get('id',''):<16}"
              f"| CID:{t['customer_id']} | ₵{t.get('total',0.0):.2f} | {info[:30]}")
    input("\nPress Enter…")

def admin_pdms_audit_log(db: DatabaseManager):
    """[A2] Admin view into the PDMS compliance audit trail."""
    while True:
        print_header("ADMIN: PDMS AUDIT LOG")
        total = db.get_audit_count()
        print(f"Total audit entries: {total}")
        print("[1] Last 50 entries   [2] By customer ID   [3] By action type   [0] Back")
        ch = input("Option > ").strip()

        if ch == '1':
            entries = db.get_audit_log(limit=50)
            print(f"\n{'Timestamp':<20} | {'Actor':<12} | {'Action':<20} | Details")
            print("-" * 80)
            for e in entries:
                dt  = e["timestamp"][:19].replace('T', ' ')
                act = e["actor_id"][:12]
                act_type = e["action"][:20]
                det = (e.get("detail") or "")[:26]
                print(f"{dt:<20} | {act:<12} | {act_type:<20} | {det}")
            input("\nPress Enter…")

        elif ch == '2':
            cid     = input("Customer ID: ").strip()
            entries = db.get_audit_log(actor_id=cid, limit=30)
            if not entries:
                print("No audit entries found for that ID.")
            else:
                for e in entries:
                    dt = e["timestamp"][:19].replace('T', ' ')
                    print(f"[{dt}] {e['action']:<22} — {e.get('detail','')[:50]}")
            input("\nPress Enter…")

        elif ch == '3':
            print("Actions: LOGIN_OK  LOGIN_FAIL  PURCHASE  BALANCE_ADJ")
            print("         CREATE_ACCOUNT  DELETE_ACCOUNT  STATUS_CHANGE")
            print("         HW_DEPLOY  HW_RETURN  UPGRADE  LOCKOUT  UNLOCK")
            act     = input("Action type: ").strip().upper()
            entries = db.get_audit_log(action=act, limit=30)
            if not entries:
                print(f"No entries for action '{act}'.")
            else:
                for e in entries:
                    dt = e["timestamp"][:19].replace('T', ' ')
                    print(f"[{dt}] {e['actor_id']:<12} — {e.get('detail','')[:50]}")
            input("\nPress Enter…")

        elif ch == '0': break
        else: print("Invalid.")

def admin_manage_communications(customer: Customer, db: DatabaseManager):
    while True:
        print_header("ADMIN: COMMUNICATIONS CENTRE")
        fb = db.get_all_feedback()
        print(f"Pending reports: {len(fb)}")
        print("[1] View Reports  [2] Send Notification  [0] Back")
        ch = input("Option > ").strip()

        if ch == '1':
            if not fb: print("No reports."); input("\nPress Enter…"); continue
            for i, f in enumerate(fb):
                print(f"[{i+1:02d}] {f['timestamp'][:10]}"
                      f" | {f['customer_id']} | {f['message'][:60]}…")
            sub = input("[V] View  [D] Clear All  [X] Return > ").strip().upper()
            if sub == 'V':
                try:
                    n = int(input("Report #: "))
                    if 1 <= n <= len(fb):
                        f = fb[n-1]
                        print(f"\n--- REPORT #{n} ---\nDate: {f['timestamp']}"
                              f"\nCustomer: {f['name']} ({f['customer_id']})"
                              f"\nMessage:\n{f['message']}")
                    else: print("Invalid.")
                except ValueError: print("Invalid.")
                input("Press Enter…")
            elif sub == 'D':
                if input("Clear ALL reports? (y/n): ").lower() == 'y':
                    db.clear_feedback()
                    db.log_audit("ADMIN", "ADMIN_ACTION", "feedback", None,
                                 "All feedback records cleared")
                    print("All reports cleared.")
                input("Press Enter…")

        elif ch == '2':
            print_header("SEND SYSTEM NOTIFICATION")
            customers = db.get_all_customers()
            for c in customers: print(f"  {c['customer_id']}: {c['name']}")
            print("  ALL: Broadcast to everyone")
            tid = input("\nCustomer ID (or ALL): ").strip()
            msg = input("Message: ").strip()
            if not msg: print("Cannot send empty message."); input("Press Enter…"); continue
            count = 0
            if tid.upper() == 'ALL':
                for c in customers:
                    db.send_notification(c["customer_id"],"AID SYSTEM Alert", msg)
                    count += 1
            elif db.get_customer(tid):
                db.send_notification(tid, "AID SYSTEM Message", msg); count = 1
            else: print("Customer ID not found."); input("Press Enter…"); continue
            db.log_audit("ADMIN", "ADMIN_ACTION", "notifications", tid,
                         f"Broadcast to {count} recipient(s): {msg[:40]}")
            print(f"✅ Sent to {count} recipient(s).")
            input("Press Enter…")

        elif ch == '0': break
        else: print("Invalid.")

def admin_get_analytics(db: DatabaseManager):
    print_header("ADMIN: SYSTEM ANALYTICS & FINANCIALS")
    hw        = db.get_hw_status()
    med_rev   = db.total_med_revenue()
    upg_rev   = db.total_upgrade_revenue()
    maint_rev = hw.get("maintenance_revenue", 0.0)
    card_rev  = hw.get("card_sales_revenue",  0.0)
    audit_cnt = db.get_audit_count()

    # Hardware deployment revenue (deposits collected)
    with db._conn() as con:
        hw_dep_row = con.execute(
            "SELECT COALESCE(SUM(ABS(amount)),0) FROM wallet_history "
            "WHERE type='Hardware Deposit'").fetchone()
        hw_dep_rev = hw_dep_row[0] if hw_dep_row else 0.0
        hw_refund_row = con.execute(
            "SELECT COALESCE(SUM(amount),0) FROM wallet_history "
            "WHERE type='Hardware Refund'").fetchone()
        hw_refund_total = hw_refund_row[0] if hw_refund_row else 0.0
        # Bonus cashback issued to customers
        bonus_issued_row = con.execute(
            "SELECT COALESCE(SUM(amount),0) FROM wallet_history "
            "WHERE type='bonus_earn'").fetchone()
        bonus_issued = bonus_issued_row[0] if bonus_issued_row else 0.0
        # Loyalty points total outstanding
        pts_row = con.execute(
            "SELECT COALESCE(SUM(loyalty_points),0), "
            "COALESCE(SUM(lifetime_points),0) FROM customers").fetchone()
        pts_outstanding = pts_row[0] if pts_row else 0
        pts_lifetime    = pts_row[1] if pts_row else 0
        # Hardware usage counts
        aid_usage = hw.get("aid_box_usage", 0)
        cpr_usage = hw.get("cpr_kit_usage", 0)
        # Penalty revenue from medication returns
        penalty_row = con.execute(
            "SELECT COUNT(*), COALESCE(SUM(amount_refunded),0) "
            "FROM medication_returns").fetchone()
        returns_count   = penalty_row[0] if penalty_row else 0
        returns_refunded = penalty_row[1] if penalty_row else 0.0

    net_hw_rev  = hw_dep_rev - abs(hw_refund_total)
    total_rev   = med_rev + upg_rev + maint_rev + card_rev + net_hw_rev

    W = 55
    print(f"\n  {'─'*W}")
    print(f"  {'FINANCIAL SUMMARY'.center(W)}")
    print(f"  {'─'*W}")
    print(f"  Drug Sales Revenue   : ₵{med_rev:>10.2f}")
    print(f"  Wallet Tier Upgrades : ₵{upg_rev:>10.2f}")
    print(f"  Physical Cards       : ₵{card_rev:>10.2f}")
    print(f"  Hardware Deposits    : ₵{hw_dep_rev:>10.2f}")
    print(f"  Hardware Refunds     : ₵{abs(hw_refund_total):>10.2f}  (returned to customers)")
    print(f"  Net Hardware Revenue : ₵{net_hw_rev:>10.2f}")
    print(f"  Maintenance Fees     : ₵{maint_rev:>10.2f}")
    print(f"  {'─'*W}")
    print(f"  TOTAL REVENUE        : ₵{total_rev:>10.2f}")

    print(f"\n  {'─'*W}")
    print(f"  {'LOYALTY & BONUS ECONOMICS'.center(W)}")
    print(f"  {'─'*W}")
    print(f"  Bonus Cashback Issued  : ₵{bonus_issued:>8.2f}  (5% on purchases)")
    print(f"  Points Outstanding     : {pts_outstanding:>8}  pts (₵{pts_outstanding*LOYALTY_BONUS_PER_REDEEM/LOYALTY_POINTS_PER_REDEEM:.2f} liability)")
    print(f"  Lifetime Points Earned : {pts_lifetime:>8}  pts (all time)")

    print(f"\n  {'─'*W}")
    print(f"  {'HARDWARE USAGE'.center(W)}")
    print(f"  {'─'*W}")
    print(f"  Aid Box deployments    : {aid_usage:>5}  uses  (sanitize at 5)")
    print(f"  CPR Kit deployments    : {cpr_usage:>5}  uses  (sanitize at 5)")
    print(f"  Aid Box status         : {hw.get('aid_box_status','—')}")
    print(f"  CPR Kit status         : {hw.get('cpr_kit_status','—')}")

    print(f"\n  {'─'*W}")
    print(f"  {'RETURNS & INVENTORY'.center(W)}")
    print(f"  {'─'*W}")
    print(f"  Medication Returns     : {returns_count:>5}  transactions")
    print(f"  Refunded (after fee)   : ₵{returns_refunded:>8.2f}")
    print(f"  Items Sold (qty)       : {db.total_sales_qty():>5}")
    print(f"  Current Stock Value    : ₵{db.total_stock_value():>8.2f}")

    print(f"\n  {'─'*W}")
    print(f"  {'SYSTEM'.center(W)}")
    print(f"  {'─'*W}")
    print(f"  Total Customers        : {len(db.get_all_customers()):>5}")
    print(f"  PDMS Audit Entries     : {audit_cnt:>5}")
    print(f"  Build                  : {SCHEMA_VERSION}")
    print(f"  {'─'*W}")
    input("\nPress Enter…")

def admin_revenue_report(db: DatabaseManager):
    print_header("FINANCIAL & MAINTENANCE REPORT")
    hw        = db.get_hw_status()
    med_rev   = db.total_med_revenue()
    upg_rev   = db.total_upgrade_revenue()
    maint_rev = hw.get("maintenance_revenue", 0.0)
    annual    = maint_rev * 12
    print(f"  💊 Medication Revenue:    ₵{med_rev:.2f}")
    print(f"  💳 Tier Upgrade Revenue:  ₵{upg_rev:.2f}")
    print(f"  🛠️  Maintenance Fund:      ₵{maint_rev:.2f}")
    print(f"  📈 Annual Projection:     ₵{annual:.2f}")
    print(f"\n  💰 GRAND TOTAL: ₵{med_rev + upg_rev + maint_rev:.2f}")
    print("\n--- GROWTH FORECAST ---")
    for label, factor in [("CURRENT",1.0),("2× GROWTH",2.0),("5× GROWTH",5.0)]:
        print(f"  {label:<12} | {'█'*int(factor*5)} ₵{annual*factor:,.2f}")
    input("\nPress Enter…")

def hardware_analytics_report(db: DatabaseManager):
    print_header("HARDWARE ANALYTICS & STATUS")
    logs    = db.get_emergency_logs()
    hw      = db.get_hw_status()
    deploys = [l for l in logs if l.get("action") == "DEPLOY"]
    returns = [l for l in logs if l.get("action") == "RETURN"]
    print(f"Total Deployments:    {len(deploys)}")
    print(f"Total Returns:        {len(returns)}")
    print(f"Active (Unreturned):  {len(deploys) - len(returns)}")
    print(f"\n--- DOCK STATUS ---")
    print(f"AID BOX: {hw.get('aid_box_status','Unknown')}")
    print(f"CPR KIT: {hw.get('cpr_kit_status','Unknown')}")
    print(f"\n--- UNIT MAINTENANCE ---")
    for kit, key in [("AID BOX","aid_box"),("CPR KIT","cpr_kit")]:
        uses    = hw.get(f"{key}_usage", 0)
        warning = " ⚠️  SANITIZATION REQUIRED" if uses >= 5 else " ✅ READY"
        print(f"  {kit}: {uses} use(s){warning}")
    if logs:
        print("\n--- RECENT ACTIVITY (last 5) ---")
        for l in logs[-5:]:
            a = "🚀 Deployed" if l["action"]=="DEPLOY" else "📥 Returned"
            print(f"  [{l['timestamp'][:16].replace('T',' ')}] {a}"
                  f" | {l.get('component')} | {l.get('customer_id')}")
    input("\nPress Enter…")

def admin_audit_dashboard(db: DatabaseManager):
    while True:
        print_header("ADMIN: PENDING AUDIT RETURNS")
        audits = db.get_pending_audits()
        if not audits:
            print("No returns flagged for audit."); input("\nPress Enter…"); return
        print(f"Flagged items: {len(audits)}")
        for i, l in enumerate(audits):
            dt = l["timestamp"][:16].replace('T', ' ')
            print(f"[{i+1:02d}] {dt} | {l['customer_id']} | "
                  f"{l.get('time_delta_mins','?')} min | AUDIT REQUIRED")
        print("[V] View  [P] Apply ₵5 Penalty  [C] Clear Flag  [0] Back")
        ch = input("Choice > ").strip().upper()
        if ch == '0': break
        try:
            if ch in ('V','P','C'):
                idx = int(input("Item #: ")) - 1
                if not (0 <= idx < len(audits)):
                    print("Invalid #."); continue
                log = audits[idx]
                cid = log["customer_id"]
                if ch == 'V':
                    print(f"\nUser:      {cid}")
                    print(f"Duration:  {log.get('time_delta_mins')} min")
                    print(f"Q1 (Used): {log.get('used_claim','').upper()}")
                    print(f"Q2 (Seal): {log.get('seal_status','').upper()}")
                    print("Discrepancy: Seal BROKEN but user claimed NOT USED.")
                elif ch == 'P':
                    c = db.get_customer(cid)
                    if c:
                        old_bal      = c.get("balance", 0.0)
                        c["balance"] = max(0.0, old_bal - 5.0)
                        db.save_customer(c)
                        maint = db.get_hw_status().get("maintenance_revenue", 0.0)
                        db.update_hw_field("maintenance_revenue", maint + 5.0)
                        db.add_wallet_entry(cid, "Penalty", -5.0,
                                            "Seal Mismanagement Penalty")
                        db.send_notification(cid, "HARDWARE FEE",
                            "₵5.00 penalty applied for Hardware Seal Mismanagement.")
                        db.mark_audit_noted(log["id"])
                        db.log_audit("ADMIN", "ADMIN_ACTION", "emergency_logs",
                                     str(log["id"]),
                                     f"₵5 penalty applied to {cid}")
                        print("⚖️  Penalty applied. Flag cleared.")
                    else: print("Customer not found.")
                elif ch == 'C':
                    db.mark_audit_noted(log["id"])
                    db.log_audit("ADMIN", "ADMIN_ACTION", "emergency_logs",
                                 str(log["id"]), f"Audit flag cleared for {cid}")
                    print("✅ Audit flag cleared.")
        except (ValueError, TypeError):
            print("Invalid input.")
        input("\nPress Enter…")

def admin_reset_sanitization(db: DatabaseManager):
    print_header("ADMIN: RESET SANITIZATION")
    hw = db.get_hw_status()
    print(f"1. AID BOX (Current uses: {hw.get('aid_box_usage',0)})")
    print(f"2. CPR KIT (Current uses: {hw.get('cpr_kit_usage',0)})")
    ch = input("Reset (1/2) or 0 to back: ").strip()
    if   ch == '1':
        db.update_hw_field("aid_box_usage", 0)
        db.log_audit("ADMIN","ADMIN_ACTION","hardware_status","1","AID BOX sanitization reset")
        print("✅ AID BOX reset.")
    elif ch == '2':
        db.update_hw_field("cpr_kit_usage", 0)
        db.log_audit("ADMIN","ADMIN_ACTION","hardware_status","1","CPR KIT sanitization reset")
        print("✅ CPR KIT reset.")
    elif ch == '0': return
    else: print("Invalid choice.")
    input("\nPress Enter…")

def clear_all_data(db: DatabaseManager) -> bool:
    print_header("CRITICAL SYSTEM WIPE")
    print("\n!! WARNING: Permanent and irreversible. !!")
    if input("Type 'DELETE ALL DATA' to proceed: ") != "DELETE ALL DATA":
        print("Wipe cancelled."); return False
    db.log_audit("ADMIN", "ADMIN_ACTION", None, None,
                 "FULL SYSTEM WIPE executed")
    db.wipe_all()
    print("All transactional and customer data wiped.")
    return True

def admin_support_tickets(svc: SupportService, db: DatabaseManager):
    while True:
        auto_count = svc.auto_escalate_overdue()
        if auto_count: print(f"⚠️  {auto_count} ticket(s) auto-escalated (SLA breach).")
        summary = svc.get_summary()
        print_header("ADMIN: SUPPORT TICKETS")
        for k, v in summary.items():
            print(f"  {k.replace('_',' ').title():<22}: {v}")
        open_tickets = svc.get_open_tickets()
        if open_tickets:
            print("\n--- OPEN TICKETS ---")
            for i, t in enumerate(open_tickets):
                print(f"[{i+1:02d}] [{t['priority'].upper():<6}] [{t['category'].upper():<8}]"
                      f" {t['ticket_id']} | {t['subject'][:40]}")
        print("\n[V] View/Update  [0] Back")
        ch = safe_input("Option > ").strip().upper()
        if ch == '0': break
        if ch == 'V' and open_tickets:
            try:
                idx = int(input("Ticket #: ")) - 1
                if not (0 <= idx < len(open_tickets)):
                    print("Invalid #."); continue
                t = open_tickets[idx]
                print(f"\n--- {t['ticket_id']} ---")
                print(f"Customer: {t['customer_id']}  |  Category: {t['category']}")
                print(f"Subject: {t['subject']}")
                print(f"Description:\n{t['description']}")
                print(f"Created: {t['created_at'][:19]}")
                print("\n[1] In Progress  [2] Awaiting Customer  [3] Resolve  [4] Escalate  [0] Back")
                action = input("> ").strip()
                status_map = {'1':'in_progress','2':'awaiting_customer','3':'resolved','4':'escalated'}
                if action in status_map:
                    notes = input("Notes (optional): ").strip()
                    if svc.update_ticket(t["ticket_id"], status_map[action], notes):
                        if status_map[action] == "resolved":
                            db.send_notification(t["customer_id"], "Ticket Resolved",
                                                 f"Your ticket '{t['subject']}' has been resolved. {notes}")
                        print(f"✅ Ticket status → {status_map[action]}.")
                    else: print("Update failed.")
            except ValueError: print("Invalid input.")
            input("\nPress Enter…")
        else: input("\nPress Enter…")


def admin_teleconsult_queue(svc: TeleconsultService, db: DatabaseManager):
    """[B29] Admin teleconsult queue with clinician consent gate for prescriptions."""
    while True:
        print_header("ADMIN: TELECONSULT DOCTOR QUEUE")
        queue   = svc.get_queue()
        summary = svc.get_summary()
        print(f"Waiting: {summary['waiting']}  | Today: {summary['today']}  "
              f"| All-time: {summary['total_all_time']}")

        if not queue:
            print("\nQueue is empty.")
        else:
            print("\n--- WAITING PATIENTS ---")
            for i, q in enumerate(queue):
                pri      = "🔴 URGENT" if q["priority"] == 1 else "🟡 NORMAL"
                req_type = q.get("request_type", "GENERAL")
                rx_tag   = " 💊 RX REQUEST" if req_type == "PRESCRIPTION" else ""
                print(f"[{i+1:02d}] {pri}{rx_tag} | {q['customer_name']:<20} "
                      f"| {q['drug_names'][:35]} | {q['joined_at'][:16]}")
                if req_type == "PRESCRIPTION" and q.get("notes"):
                    print(f"       Patient note: {q['notes'][:70]}")

        print("\n[A] Approve  [R] Reject  [0] Back")
        ch = safe_input("Option > ").strip().upper()
        if ch == '0':
            break

        if ch in ('A', 'R') and queue:
            try:
                idx = int(safe_input("Patient #: ").strip()) - 1
                if not (0 <= idx < len(queue)):
                    print("Invalid #."); input("Press Enter…"); continue

                q        = queue[idx]
                decision = "approved" if ch == 'A' else "rejected"

                note = safe_input("Clinical note (required): ").strip()
                if not note:
                    print("  ⚠️  A clinical note is required before proceeding.")
                    input("Press Enter…"); continue

                req_type = q.get("request_type", "GENERAL")
                if req_type == "PRESCRIPTION" and decision == "approved":
                    print()
                    print("  ┌──────────────────────────────────────────────────────┐")
                    print("  │      CLINICIAN CONSENT — PRESCRIPTION ISSUANCE       │")
                    print("  │                                                      │")
                    print("  │  By confirming you declare that:                     │")
                    print("  │  • You are a licensed doctor or pharmacist.          │")
                    print("  │  • You have reviewed the patient's stated condition. │")
                    print("  │  • This prescription is clinically appropriate.      │")
                    print("  │  • You accept professional responsibility for this   │")
                    print("  │    issuance under Aid Plus clinical policy.          │")
                    print("  └──────────────────────────────────────────────────────┘")
                    print()
                    confirm = safe_input(
                        "  Type CONSENT CONFIRMED to issue the prescription: "
                    ).strip()
                    if confirm != "CONSENT CONFIRMED":
                        print("  ❌ Prescription NOT issued — consent phrase not matched.")
                        print("     Consult status unchanged. Review and retry.")
                        input("Press Enter…"); continue

                    drug_list = [d.strip() for d in q["drug_names"].split(",") if d.strip()]
                    for drug in drug_list:
                        db.create_prescription(
                            q["customer_id"], drug,
                            qty=1, dosage=note,
                            consult_id=q["consult_id"],
                        )
                    db.log_audit(
                        "ADMIN", "RX_CONSENT_CONFIRMED", "prescriptions",
                        q["consult_id"],
                        f"Issued for: {q['drug_names']} | Note: {note[:60]}",
                    )
                    print("  ✅ Prescription(s) issued and linked to patient Aid Card.")

                if svc.admin_resolve_consult(q["consult_id"], decision, note):
                    icon = "✅" if decision == "approved" else "❌"
                    print(f"  {icon} Consult {decision.upper()} for {q['customer_name']}.")
                else:
                    print("  ⚠️  Could not update consult record — check DB.")

            except ValueError:
                print("Invalid input.")
            input("\nPress Enter…")
        else:
            input("\nPress Enter…")


def admin_prescriptions(db: DatabaseManager):
    print_header("ADMIN: PRESCRIPTION RECORDS")
    db.expire_prescriptions()
    customers = db.get_all_customers()
    for c in customers:
        rxs = db.get_active_prescriptions(c["customer_id"])
        if rxs:
            print(f"\n  {c['name']} ({c['customer_id']}):")
            for rx in rxs:
                print(f"    RX {rx['prescription_id']} | {rx['drug_name']}"
                      f" x{rx['quantity_authorized']} | expires {rx['expires_at'][:16]}")
    input("\nPress Enter…")


def admin_intelligence_dashboard(db: DatabaseManager, nyansa: NyansaIntelligence,
                                  promo_engine: PromotionEngine,
                                  restock_eng: RestockEngine,
                                  support_svc: SupportService,
                                  consult_svc: TeleconsultService):
    while True:
        print_header("Nyansa INTELLIGENCE DASHBOARD")

        # Stock health
        shelves = db.get_all_shelves() + db.get_all_mega_shelves()
        low_stock = [s for s in shelves if
                     (s.get("capsules_left",0) <= LOW_STOCK_THRESHOLD if not s.get("is_mega")
                      else s.get("units_left",0) <= LOW_STOCK_MEGA_THRESHOLD)]

        # Revenue today
        today_cutoff = datetime.now().replace(hour=0,minute=0,second=0).isoformat()
        with db._conn() as con:
            today_rev = con.execute(
                "SELECT SUM(total) FROM transactions WHERE timestamp>?",
                (today_cutoff,)).fetchone()[0] or 0.0

        # Summaries
        tkt_s  = support_svc.get_summary()
        con_s  = consult_svc.get_summary()
        ins    = nyansa.get_pending_insights(5)
        clts   = db.get_clts_stats()
        orders = restock_eng.get_pending()
        promos = promo_engine.get_active_promotions()

        print(f"\n{'─'*58}")
        print(f"  📦 LOW STOCK ITEMS:     {len(low_stock):>3}  |  "
              f"💰 Today's Revenue: ₵{today_rev:.2f}")
        print(f"  🎫 Support Tickets:     {tkt_s.get('open',0):>3} open, "
              f"{tkt_s.get('overdue',0)} overdue")
        print(f"  🩺 Teleconsult Queue:   {con_s['waiting']:>3} waiting")
        print(f"  📋 Restock Orders:      {len(orders):>3} pending")
        print(f"  🏷️  Active Promotions:   {len(promos):>3}")
        print(f"  👁️  CLTS Sessions Today: "
              f"{clts['total_sessions']:>3} total, "
              f"{clts['led_to_purchase']} led to purchase")
        if clts["gender_breakdown"]:
            gd = " | ".join(f"{g['detected_gender']}: {g['cnt']}" for g in clts["gender_breakdown"])
            print(f"  👤 Gender breakdown:    {gd}")
        print(f"{'─'*58}")

        if ins:
            print(f"\n🧠 TOP Nyansa INSIGHTS ({len(ins)} pending):")
            for i, insight in enumerate(ins):
                conf_bar = "█" * int(insight["confidence_score"] * 10)
                print(f"  [{i+1}] [{insight['insight_type'].upper():<18}] "
                      f"[{conf_bar:<10}] {insight['title'][:42]}")
                print(f"       → {insight['recommended_action'][:55]}")
        else:
            print("\n🧠 No pending insights. Run analysis to generate.")

        if low_stock:
            print(f"\n📦 LOW STOCK ALERTS:")
            for s in low_stock[:5]:
                stock = s.get("capsules_left", s.get("units_left",0))
                print(f"  ⚠️  Shelf {s['shelf']:>2}: {s['name']:<28} Stock: {stock}")

        print(f"\n[A] Action insight  [D] Dismiss insight  "
              f"[G] Generate orders  [R] Run analysis  [0] Back")
        ch = safe_input("Option > ").strip().upper()

        if ch == 'A' and ins:
            try:
                idx = int(input("Insight #: ")) - 1
                if 0 <= idx < len(ins):
                    nyansa.action_insight(ins[idx]["insight_id"])
                    print("✅ Insight marked as actioned.")
            except ValueError: print("Invalid.")
            input("Press Enter…")

        elif ch == 'D' and ins:
            try:
                idx = int(input("Insight #: ")) - 1
                if 0 <= idx < len(ins):
                    nyansa.dismiss_insight(ins[idx]["insight_id"])
                    print("✅ Insight dismissed.")
            except ValueError: print("Invalid.")
            input("Press Enter…")

        elif ch == 'G':
            order_ids = restock_eng.auto_generate_all()
            if order_ids:
                print(f"✅ {len(order_ids)} restock order(s) generated:")
                for oid in order_ids: print(f"   {oid}")
            else: print("No new orders needed — all drugs adequately stocked or orders already pending.")
            input("Press Enter…")

        elif ch == 'R':
            print("🧠 Running Nyansa full analysis...")
            results = nyansa.run_full_analysis()
            promo_engine.expire_old_promotions()
            print(f"✅ {len(results)} new insight(s) generated.")
            input("Press Enter…")

        elif ch == '0': break
        else: input("Press Enter…")


def admin_restock_orders(eng: RestockEngine, db: DatabaseManager):
    while True:
        print_header("ADMIN: RESTOCK ORDERS")
        orders = eng.get_pending()
        all_orders = eng.get_all()
        print(f"Pending: {len(orders)}  |  All-time: {len(all_orders)}")
        if orders:
            print("\n--- PENDING ORDERS ---")
            for i, o in enumerate(orders):
                print(f"[{i+1:02d}] {o['order_id']:<14} | {o['drug_name']:<28}"
                      f"| Qty: {o['quantity_ordered']:>4} | Status: {o['status']:<12}"
                      f"| ETA: {(o.get('expected_delivery') or '')[:10]}")
        print("\n[S] Send order  [C] Confirm  [R] Receive  "
              "[G] Auto-generate  [E] Export CSV  [0] Back")
        ch = safe_input("Option > ").strip().upper()

        if ch == '0': break
        elif ch == 'G':
            oids = eng.auto_generate_all()
            print(f"✅ {len(oids)} order(s) generated." if oids else "No new orders needed.")
            input("Press Enter…")
        elif ch == 'E':
            path = eng.export_csv()
            print(f"✅ Exported to: {path}")
            input("Press Enter…")
        elif ch in ('S','C','R') and orders:
            try:
                idx = int(input("Order #: ")) - 1
                if not (0 <= idx < len(orders)):
                    print("Invalid."); continue
                o = orders[idx]
                if ch == 'S':
                    ref = input("Supplier reference (blank=auto): ").strip()
                    eng.send_order(o["order_id"], ref)
                    print(f"✅ Order {o['order_id']} sent to distribution centre.")
                elif ch == 'C':
                    eng.update_status(o["order_id"], "confirmed")
                    print(f"✅ Order {o['order_id']} confirmed.")
                elif ch == 'R':
                    qty = int(input(f"Received quantity (ordered: {o['quantity_ordered']}): "))
                    if eng.receive_order(o["order_id"], qty):
                        print(f"✅ {qty} units of {o['drug_name']} received and shelf updated.")
                    else: print("Failed.")
            except ValueError: print("Invalid input.")
            input("Press Enter…")
        else: input("Press Enter…")


def admin_promotions(eng: PromotionEngine, db: DatabaseManager):
    while True:
        print_header("ADMIN: PROMOTIONS MANAGER")
        promos = eng.get_all_promotions()
        active = [p for p in promos if p["status"] == "active"]
        draft  = [p for p in promos if p["status"] == "draft"]
        print(f"Active: {len(active)}  |  Draft: {len(draft)}")
        if promos:
            print("\n--- ALL PROMOTIONS ---")
            for i, p in enumerate(promos[:15]):
                print(f"[{i+1:02d}] [{p['status'].upper():<8}] {p['name']:<30}"
                      f"| {p['discount_type']} {p['discount_value']}"
                      f" | {p['start_date'][:10]}→{p['end_date'][:10]}"
                      f" | Applied: {p['times_applied']}")
        print("\n[C] Create promotion  [A] Activate draft  [0] Back")
        ch = safe_input("Option > ").strip().upper()
        if ch == '0': break
        elif ch == 'C':
            print_header("CREATE PROMOTION")
            name    = input("Promotion name: ").strip()
            print("Discount type: [1] Percent  [2] Fixed amount  [3] Buy X Get Y")
            dt_map  = {'1':'percent','2':'fixed','3':'buy_x_get_y'}
            dt      = dt_map.get(input("Type: ").strip(), "percent")
            try:
                val = float(input("Discount value (% or ₵): "))
                print("Target: [1] All  [2] NHIS  [3] G2+  [4] Loyalty 100+")
                seg_map = {'1':'all','2':'nhis','3':'g2_plus','4':'loyalty_100plus'}
                seg     = seg_map.get(input("Target: ").strip(), "all")
                start   = input("Start date (YYYY-MM-DD): ").strip()
                end     = input("End date (YYYY-MM-DD): ").strip()
                if not start: start = datetime.now().strftime("%Y-%m-%d")
                if not end:   end   = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
                promo_id = eng.create_promotion(name, dt, val,
                                                f"{start}T00:00:00", f"{end}T23:59:59",
                                                target_segment=seg)
                print(f"✅ Promotion created: {promo_id}")
                if input("Activate now? (y/n): ").lower() == 'y':
                    eng.activate_promotion(promo_id)
                    print("✅ Promotion activated.")
            except ValueError: print("Invalid value.")
            input("Press Enter…")
        elif ch == 'A' and draft:
            try:
                idx = int(input("Draft # to activate (from list above): ")) - 1
                all_promos = eng.get_all_promotions()
                d_promos   = [p for p in all_promos if p["status"]=="draft"]
                if 0 <= idx < len(d_promos):
                    eng.activate_promotion(d_promos[idx]["promo_id"])
                    print(f"✅ '{d_promos[idx]['name']}' activated.")
                else: print("Invalid.")
            except ValueError: print("Invalid input.")
            input("Press Enter…")
        else: input("Press Enter…")


def support_menu_customer(customer: Customer, db: DatabaseManager):
    """[B13] Customer-facing support ticket and teleconsult interface."""
    svc         = SupportService(db)
    consult_svc = TeleconsultService(db)

    while True:
        print_header("SUPPORT & TELECONSULT")
        my_tickets   = svc.get_customer_tickets(customer.current["customer_id"])
        open_tickets = [t for t in my_tickets if t["status"] not in ("resolved","escalated")]
        print(f"My tickets: {len(my_tickets)}  |  Open: {len(open_tickets)}")
        print("\n[1] Open support ticket")
        print("[2] View my tickets")
        print("[3] Request doctor teleconsult (general)")
        print("[4] Request prescription via teleconsult")
        print("[0] Back")
        ch = safe_input("Option > ").strip()

        if ch == '1':
            print("\nCategory: [1] Billing  [2] Hardware  [3] Account  [4] Medical  [5] General")
            cat_map = {'1':'billing','2':'hardware','3':'account','4':'medical','5':'general'}
            cat = cat_map.get(safe_input("Category: ").strip(), "general")
            print("Priority: [1] Low  [2] Normal  [3] High  [4] Urgent")
            pri_map = {'1':'low','2':'normal','3':'high','4':'urgent'}
            pri = pri_map.get(safe_input("Priority: ").strip(), "normal")
            subj = safe_input("Subject (brief): ").strip()
            desc = safe_input("Describe your issue in detail:\n> ").strip()
            if subj and desc:
                tid = svc.open_ticket(customer.current["customer_id"], cat, subj, desc, pri)
                db.send_notification(customer.current["customer_id"], "Ticket Opened",
                                     f"Ticket {tid} submitted. We'll respond within {TICKET_SLA_HOURS}h.")
                print(f"✅ Ticket opened: {tid}")
            else:
                print("Subject and description required.")
            input("Press Enter…")

        elif ch == '2':
            if not my_tickets:
                print("No tickets found."); input("Press Enter…"); continue
            print("\n--- MY TICKETS ---")
            for t in my_tickets:
                icon = "✅" if t["status"]=="resolved" else "🔄" if t["status"]=="in_progress" else "🕐"
                print(f"{icon} [{t['status'].upper():<18}] {t['subject'][:40]}"
                      f"\n   Created: {t['created_at'][:16]}  |  ID: {t['ticket_id']}")
                if t.get("resolution_notes"):
                    print(f"   Resolution: {t['resolution_notes'][:60]}")
            input("\nPress Enter…")

        elif ch == '3':
            print_header("REQUEST DOCTOR TELECONSULT")
            print(f"Consultation required for: {', '.join(CONSULT_REQUIRED_DRUGS)}")
            print("Enter the drug name(s) you need consulted (comma separated):")
            drug_input = safe_input("> ").strip()
            drugs = [d.strip() for d in drug_input.split(",") if d.strip()]
            if not drugs:
                print("No drugs entered."); input("Press Enter…"); continue
            print("Priority: [1] Urgent (medical emergency)  [2] Normal")
            pri = 1 if safe_input("Priority: ").strip() == '1' else 2
            result = consult_svc.request_consult(
                customer.current["customer_id"], drugs, priority=pri)
            print(f"\n✅ Consultation requested!")
            print(f"   Consult ID:     {result['consult_id']}")
            print(f"   Queue position: {result['queue_position']}")
            print(f"   Status:         {result['status']}")
            print(f"\nYou will be notified when a doctor reviews your request.")
            db.send_notification(customer.current["customer_id"], "Teleconsult Queued",
                                 f"Your consult for {', '.join(drugs)} is #{result['queue_position']} in queue.")
            input("\nPress Enter…")

        elif ch == '4':
            request_prescription_teleconsult(customer, db)

        elif ch == '0':
            break
        else:
            print("Invalid."); input("Press Enter…")




def request_prescription_teleconsult(customer: "Customer", db: "DatabaseManager",
                                      connectivity=None) -> None:
    """
    [B29] Customer prescription request via teleconsult.
    Step 1 — describe condition + drugs.
    Step 2 — informed consent (must type YES).
    Step 3 — submitted to queue with request_type=PRESCRIPTION.
    Admin side enforces CONSENT CONFIRMED before prescription is issued.
    """
    consult_svc = TeleconsultService(db)

    print_header("REQUEST PRESCRIPTION — TELECONSULT")
    ui_info([
        "A licensed doctor or pharmacist will review your request.",
        "If approved, a prescription will be added to your Aid Card.",
        "You will be notified when your consultation is complete.",
    ])

    if connectivity and not connectivity.is_online():
        deep_link = connectivity.teleconsult_qr_redirect(
            customer.current.get("customer_id", ""), "prescription_request"
        )
        ui_qr(deep_link,
               "Teleconsult offline — use the Aid Plus app",
               "Scan to open Aid Plus teleconsult on your phone")
        return

    customer_id = customer.current.get("customer_id", "")

    # Step 1
    print("\n  ── Step 1 of 3 — Describe your medical need ──────────────────")
    condition = safe_input(
        "  Condition or symptoms\n"
        "  (e.g. recurring headache — requesting Paracetamol): "
    ).strip()
    if not condition:
        print("  Cancelled — no condition entered.")
        input("  Press Enter…")
        return

    drug_raw   = safe_input(
        "  Medication(s) requested\n"
        "  (comma-separated, or press Enter to let doctor decide): "
    ).strip()
    drug_names = [d.strip() for d in drug_raw.split(",") if d.strip()] or ["To be determined by clinician"]

    # Step 2 — Consent
    print("\n  ── Step 2 of 3 — Informed Consent ────────────────────────────")
    print("""
  ┌──────────────────────────────────────────────────────┐
  │          PATIENT CONSENT — TELECONSULT RX            │
  │                                                      │
  │  By proceeding you confirm that:                     │
  │  • The information you provided is accurate.         │
  │  • You consent to review by a licensed Aid Plus      │
  │    doctor or pharmacist.                             │
  │  • A prescription may be issued OR declined based    │
  │    on clinical judgement.                            │
  │  • Your data is processed per the Aid Plus Privacy   │
  │    Policy.                                           │
  │  • This is NOT an emergency service. For             │
  │    emergencies dial 193 or go to the nearest         │
  │    hospital immediately.                             │
  └──────────────────────────────────────────────────────┘""")

    consent = safe_input("\n  Type YES to give consent and submit: ").strip().upper()
    if consent != "YES":
        print("  Request cancelled — consent not given.")
        input("  Press Enter…")
        return

    # Step 3 — Submit
    print("\n  ── Step 3 of 3 — Submitting… ──────────────────────────────────")
    result = consult_svc.request_consult(
        customer_id  = customer_id,
        drug_names   = drug_names,
        priority     = 2,
        request_type = "PRESCRIPTION",
        notes        = condition,
    )

    if not result:
        ui_info(["Could not submit — please try again or speak to staff."], style="error")
        input("  Press Enter…")
        return

    db.log_audit(customer_id, "TELECONSULT_RX_REQUEST", "teleconsult_records",
                 result.get("consult_id", ""), f"Condition: {condition[:80]}")

    print()
    ui_info([
        f"✅ Request submitted.",
        f"   Reference     : {result.get('consult_id', '—')}",
        f"   Queue position: #{result.get('queue_position', '?')}",
        "",
        "You will be notified when a doctor has reviewed your case.",
        "Keep your Aid Card nearby.",
    ], style="success")

    speak("Your prescription request has been submitted. "
          "You will be notified when a doctor has reviewed your case.",
          customer.current)
    input("\n  Press Enter to return to menu…")


def admin_hardware_menu(db: DatabaseManager):
    hw    = HardwareInterface(db)
    ts    = TemperatureSensor(db)
    dm    = DispenserManager(db, hw)
    while True:
        health = hw.hardware_health()
        mode   = "🖥️  SIMULATION" if health["simulation_mode"] else "🔌 GPIO LIVE"
        print_header(f"ADMIN: HARDWARE INTERFACE [B18]  {mode}")
        print(f"Shelves: {health['shelf_count']}  |  "
              f"GPIO available: {'Yes' if health['gpio_available'] else 'No (simulation)'}")
        print("\n--- LED STATUS (synced to DB) ---")
        hw.sync_leds_to_db()
        hwst = db.get_hw_status()
        print(f"  AID Box : {hwst.get('aid_box_status','—')}")
        print(f"  CPR Kit : {hwst.get('cpr_kit_status','—')}")
        print("\n[T] Read temperature   [D] Test dispense (shelf)   [L] Dispense log")
        print("[I] Ice pockets        [H] Full health report      [0] Back")
        ch = safe_input("Option > ").strip().upper()
        if ch == '0': break
        elif ch == 'T':
            print("\n📡 Reading temperature sensor…")
            r = ts.read()
            flag = " ⚠️  FEVER DETECTED" if r["flagged"] else ""
            print(f"  Mode    : {r['mode'].upper()}")
            print(f"  Ambient : {r['ambient']}°C")
            print(f"  Object  : {r['temperature']}°C{flag}")
            if r["flagged"]: print(f"  Reason  : {r['flag_reason']}")
            input("\nPress Enter…")
        elif ch == 'D':
            try:
                shelf = int(input("Shelf number to test: ").strip())
                qty   = int(input("Quantity (1-3): ").strip())
                print(f"\n⚙️  Firing trigger on shelf {shelf}…")
                result = dm.dispense(shelf, qty)
                sym    = "✅" if result["success"] else "❌"
                print(f"{sym} {result}")
            except ValueError:
                print("Invalid input.")
            input("\nPress Enter…")
        elif ch == 'L':
            log = db.get_dispense_history(limit=20)
            print_header("DISPENSE LOG (Last 20)")
            print(f"{'Shelf':<7}{'Drug':<25}{'Status':<12}{'Mode':<12}{'Time'}")
            print("─" * 70)
            for e in log:
                print(f"  {e['shelf_num']:<5}{e['drug_name']:<25}"
                      f"{e['status']:<12}{e['mode']:<12}"
                      f"{e['triggered_at'][:16]}")
            input("\nPress Enter…")
        elif ch == 'I':
            print("Ice Pocket: [L] Left ON  [R] Right ON  [B] Both ON  [X] Both OFF")
            ic = input("> ").strip().upper()
            if ic == 'L':   hw.set_ice_pocket("left",  True)
            elif ic == 'R': hw.set_ice_pocket("right", True)
            elif ic == 'B': hw.set_ice_pocket("both",  True)
            elif ic == 'X': hw.set_ice_pocket("both",  False)
            print("✅ Ice pocket relay updated.")
            input("\nPress Enter…")
        elif ch == 'H':
            print("\n--- FULL HARDWARE HEALTH REPORT ---")
            for k, v in health.items():
                print(f"  {k}: {v}")
            therm = ts.get_stats()
            print(f"\n--- THERMAL LOG STATS ---")
            for k, v in therm.items():
                print(f"  {k}: {v}")
            input("\nPress Enter…")
        else: input("\nPress Enter…")


def admin_reports_menu(db: DatabaseManager):
    svc = ReportingService(db)
    while True:
        print_header("ADMIN: REPORTS & BUSINESS INTELLIGENCE [B19]")
        print(f"ReportLab: {'✅ PDF mode' if HAS_REPORTLAB else '⚠️  CSV fallback (install reportlab)'}")
        reports = svc.get_all_reports()
        print(f"Reports generated so far: {len(reports)}")
        if reports:
            print(f"\n--- RECENT REPORTS ---")
            for r in reports[:5]:
                size_kb = r.get("file_size_bytes", 0) // 1024
                print(f"  [{r['report_type']:<20}] {r['title'][:40]:<40} "
                      f"{size_kb}KB  {r['generated_at'][:16]}")
        print("\n--- GENERATE REPORT ---")
        print("[1] Monthly Revenue")
        print("[2] Drug Consumption Trends")
        print("[3] NHIS Utilisation")
        print("[4] Stock Turnover Analysis")
        print("[5] Customer Demographics (CLTS)")
        print("[6] Multi-Unit Comparison")
        print("[H] View full report history   [0] Back")
        ch = safe_input("Option > ").strip().upper()
        if ch == '0': break
        elif ch == '1':
            now = datetime.now()
            try:
                y = input(f"Year  [{now.year}]: ").strip() or str(now.year)
                m = input(f"Month [{now.month}]: ").strip() or str(now.month)
                print("\n📊 Generating monthly revenue report…")
                path = svc.report_monthly_revenue(int(y), int(m))
                print(f"✅ Saved: {path}")
            except Exception as e: print(f"❌ {e}")
            input("\nPress Enter…")
        elif ch == '2':
            days = input("Days window [30/60/90]: ").strip() or "30"
            print(f"\n📊 Generating drug consumption report ({days}d)…")
            try:
                path = svc.report_drug_consumption(int(days))
                print(f"✅ Saved: {path}")
            except Exception as e: print(f"❌ {e}")
            input("\nPress Enter…")
        elif ch == '3':
            now = datetime.now()
            try:
                y = input(f"Year  [{now.year}]: ").strip() or str(now.year)
                m = input(f"Month [{now.month}]: ").strip() or str(now.month)
                print("\n📊 Generating NHIS utilisation report…")
                path = svc.report_nhis_utilisation(int(y), int(m))
                print(f"✅ Saved: {path}")
            except Exception as e: print(f"❌ {e}")
            input("\nPress Enter…")
        elif ch == '4':
            print("\n📊 Generating stock turnover analysis…")
            try:
                path = svc.report_stock_turnover()
                print(f"✅ Saved: {path}")
            except Exception as e: print(f"❌ {e}")
            input("\nPress Enter…")
        elif ch == '5':
            print("\n📊 Generating CLTS demographics report…")
            try:
                path = svc.report_clts_demographics()
                print(f"✅ Saved: {path}")
            except Exception as e: print(f"❌ {e}")
            input("\nPress Enter…")
        elif ch == '6':
            print("\n📊 Generating multi-unit comparison report…")
            try:
                path = svc.report_multi_unit()
                print(f"✅ Saved: {path}")
            except Exception as e: print(f"❌ {e}")
            input("\nPress Enter…")
        elif ch == 'H':
            print_header("ALL GENERATED REPORTS")
            print(f"{'Type':<22}{'Title':<40}{'Size':<8}{'Generated'}")
            print("─" * 85)
            for r in reports:
                size_kb = r.get("file_size_bytes", 0) // 1024
                print(f"  {r['report_type']:<20}{r['title'][:38]:<40}"
                      f"{size_kb}KB  {r['generated_at'][:16]}")
            input("\nPress Enter…")
        else: input("\nPress Enter…")


def admin_ota_menu(ota_svc: OTAService):
    while True:
        ota = ota_svc
        print_header("ADMIN: OTA SOFTWARE UPDATES [B15]")
        history = ota.get_update_history()
        print(f"Current build: v{SCHEMA_VERSION}  |  Unit: {UNIT_ID}")
        print(f"Update records: {len(history)}")
        if history:
            print("\n--- RECENT UPDATES ---")
            for h in history[:5]:
                print(f"  {h['update_id']} | v{h['from_version']}→v{h['to_version']}"
                      f" | {h['status'].upper()} | {h['initiated_at'][:16]}")
        print("\n[C] Check for update  [H] Full history  [R] Rollback  [0] Back")
        ch = safe_input("Option > ").strip().upper()
        if ch == '0': break
        elif ch == 'C':
            print("\n🔍 Checking for updates…")
            manifest = ota.check_for_update()
            av = manifest.get("available_version", SCHEMA_VERSION)
            if av <= SCHEMA_VERSION:
                print(f"✅ System is up to date (v{SCHEMA_VERSION}).")
            else:
                print(f"🆕 Update available: v{av}")
                print(f"   Release notes: {manifest.get('release_notes','—')}")
                print(f"   File size: {manifest.get('file_size_bytes',0):,} bytes")
                if input("Stage this update? (y/n): ").lower() == 'y':
                    result = ota.stage_update(SCHEMA_VERSION, av,
                                              manifest.get("download_url",""),
                                              manifest.get("checksum",""))
                    print(f"{'✅' if result['success'] else '❌'} {result.get('message',result.get('error',''))}")
            input("\nPress Enter…")
        elif ch == 'H':
            print("\n--- FULL UPDATE HISTORY ---")
            for h in history:
                print(f"  [{h['status'].upper():<12}] v{h['from_version']}→v{h['to_version']}"
                      f" | {h['initiated_at'][:16]}"
                      + (f" → {h['completed_at'][:16]}" if h.get('completed_at') else ""))
            input("\nPress Enter…")
        elif ch == 'R':
            print("⚠️  WARNING: This will restore the last backup file.")
            if input("Confirm rollback? (yes/no): ").strip().lower() == 'yes':
                result = ota.rollback()
                print(f"{'✅' if result['success'] else '❌'} {result.get('message',result.get('error',''))}")
            input("\nPress Enter…")
        else: input("\nPress Enter…")


def admin_broadcast(notif: NotificationService):
    print_header("ADMIN: BROADCAST NOTIFICATION [B15]")
    print("Target: [1] All customers  [2] NHIS holders  [3] G2+ tier  [4] Active only")
    seg_map = {'1':'all','2':'nhis','3':'g2_plus','4':'active'}
    seg = seg_map.get(input("Target: ").strip(), "all")
    subject = input("Subject: ").strip()
    message = input("Message:\n> ").strip()
    if not subject or not message:
        print("Subject and message required."); input("Press Enter…"); return
    count = notif.broadcast(subject, message, seg)
    print(f"✅ Broadcast sent to {count} customer(s).")
    input("\nPress Enter…")


def admin_scheduler_status(sched: SchedulerService):
    print_header("ADMIN: SCHEDULER STATUS [B15]")
    status = sched.get_status()
    running = "🟢 RUNNING" if status["running"] else "⚪ STOPPED"
    print(f"Status: {running}  |  Interval: {SCHEDULER_INTERVAL_SECS}s")
    if status["last_runs"]:
        print("\n--- LAST RUN TIMES ---")
        for task, ts in status["last_runs"].items():
            print(f"  {task:<25}: {ts[:19]}")
    if status["log"]:
        print("\n--- RECENT TASK LOG ---")
        print(f"{'Task':<25}{'Status':<12}{'Duration':<12}{'Result'}")
        print("─" * 70)
        for entry in status["log"]:
            dur = f"{entry.get('duration_ms',0)}ms"
            print(f"  {entry['task_name']:<23}{entry['status']:<12}{dur:<12}"
                  f"{(entry.get('result_summary',''))[:30]}")
    print("\n[R] Run all tasks now  [T] Run specific task  [0] Back")
    ch = safe_input("Option > ").strip().upper()
    if ch == 'R':
        print("\n⚙️  Running all tasks…")
        results = sched.run_all_tasks()
        for task, res in results.items():
            print(f"  {task}: {res}")
        input("\nPress Enter…")
    elif ch == 'T':
        print("Tasks: " + ", ".join(SchedulerService.TASKS))
        task = input("Task name: ").strip()
        res  = sched.run_task(task)
        print(f"Result: {res}")
        input("\nPress Enter…")


def admin_dispatch_log(db: DatabaseManager):
    print_header("ADMIN: NOTIFICATION DISPATCH LOG [B15]")
    log = db.get_dispatch_log(limit=30)
    if not log:
        print("No dispatch records yet."); input("\nPress Enter…"); return
    print(f"{'Customer':<16}{'Channel':<10}{'Status':<12}{'Subject':<35}{'Time'}")
    print("─" * 85)
    for entry in log:
        print(f"  {entry['customer_id']:<14}{entry['channel']:<10}{entry['status']:<12}"
              f"{entry['subject'][:33]:<35}{entry['attempted_at'][:16]}")
    input("\nPress Enter…")


def admin_api_server(api: APIServer):
    while True:
        print_header("ADMIN: REST API SERVER [B16]")
        status = api.status()
        state  = "🟢 RUNNING" if status["running"] else "⚪ STOPPED"
        print(f"Status: {state}")
        if status["running"]:
            print(f"URL:       {status['url']}")
            print(f"Dashboard: {status['dashboard']}")
            print(f"\nAPI Endpoints available:")
            endpoints = [
                "POST /api/auth/login         — Customer / admin login",
                "POST /api/auth/doctor_login  — Doctor login",
                "POST /api/auth/distrib_login — Distribution centre login",
                "GET  /api/inventory          — Full shelf list",
                "GET  /api/customers          — All customers (admin)",
                "GET/POST /api/tickets        — Support tickets",
                "GET/POST /api/consult/*      — Teleconsult queue",
                "GET/POST /api/restock/*      — Restock orders",
                "GET  /api/insights           — Nyansa insights",
                "POST /api/insights/run       — Trigger analysis",
                "GET/POST /api/promotions     — Promotions",
                "GET  /api/analytics          — Revenue & stats",
                "GET  /api/units              — Unit registry",
                "GET  /api/ota/check          — OTA version check",
                "GET  /dashboard             — Web dashboard",
                "GET  /api/health            — Health check",
            ]
            for ep in endpoints: print(f"  {ep}")
        print(f"\n{'[X] Stop API' if status['running'] else '[S] Start API'}  [0] Back")
        ch = safe_input("Option > ").strip().upper()
        if ch == '0': break
        elif ch == 'S' and not status["running"]:
            result = api.start()
            print(f"{'✅' if result['success'] else '❌'} {result['message']}")
            if result.get("dashboard"):
                print(f"   Dashboard: {result['dashboard']}")
            input("\nPress Enter…")
        elif ch == 'X' and status["running"]:
            api.stop()
            print("✅ API server stopped.")
            input("\nPress Enter…")
        else: input("\nPress Enter…")


def admin_api_tokens(db: DatabaseManager):
    print_header("ADMIN: API TOKENS [B16]")
    tokens = db.get_api_tokens()
    print(f"Total tokens issued: {len(tokens)}")
    if tokens:
        print(f"\n{'Token ID':<16}{'Actor':<16}{'Role':<18}{'Expires':<20}{'Revoked'}")
        print("─" * 80)
        for t in tokens[:15]:
            rev = "🔴 YES" if t["revoked"] else "✅ NO"
            print(f"  {t['token_id']:<14}{t['actor_id']:<16}{t['role']:<18}"
                  f"{t['expires_at'][:16]:<20}{rev}")
    print("\n[I] Issue new token  [R] Revoke token  [0] Back")
    ch = safe_input("Option > ").strip().upper()
    if ch == 'I':
        print("Roles: " + " | ".join(API_ROLES))
        actor = input("Actor ID: ").strip()
        role  = input("Role: ").strip()
        desc  = input("Description (optional): ").strip()
        try:
            info = db.create_api_token(actor, role, desc)
            print(f"✅ Token issued: {info['token_id']}")
            print(f"   Expires: {info['expires_at'][:16]}")
        except ValueError as e: print(f"❌ {e}")
        input("\nPress Enter…")
    elif ch == 'R' and tokens:
        tid = input("Token ID to revoke: ").strip()
        db.revoke_api_token(tid)
        print(f"✅ Token {tid} revoked.")
        input("\nPress Enter…")




def collocated_cupsule_return(customer: Customer, db: DatabaseManager,
                               connectivity: "ConnectivityManager" = None,
                               power: "PowerManager" = None):
    """
    [K] Return Cupsule — identifies which drug the Cupsule is from.
    Customer can select from their recent purchases or mark as unidentified.
    Points awarded after CUPSCAN verification.
    """
    print_header("RETURN CUPSULE")
    c   = customer.current
    cid = c["customer_id"]

    # Show only drugs customer still holds (purchased but NOT fully returned)
    # Fully returned drugs had their packaging returned with the medication.
    with db._conn() as _con:
        raw_items = _con.execute(
            "SELECT ti.id, ti.name, ti.qty, t.id AS txn_id, t.timestamp "
            "FROM transaction_items ti "
            "JOIN transactions t ON t.id = ti.transaction_id "
            "WHERE t.customer_id=? "
            "AND COALESCE(t.status,'completed') NOT IN ('donated','donated_with_drugs') "
            "ORDER BY t.timestamp DESC LIMIT 20",
            (cid,)).fetchall()
        # Filter: only include items where net qty (purchased - returned) > 0
        recent = []
        seen   = set()
        for row in raw_items:
            ret_qty = _con.execute(
                "SELECT COALESCE(SUM(qty_returned),0) FROM medication_returns "
                "WHERE transaction_id=? AND item_id=?",
                (row["txn_id"], row["id"])).fetchone()[0]
            net = row["qty"] - ret_qty
            if net > 0 and row["name"] not in seen:
                seen.add(row["name"])
                recent.append({
                    "name": row["name"],
                    "net_qty": net,
                    "timestamp": row["timestamp"]
                })

    print()
    print("  Which drug is this Cupsule from?")
    print("  (Only shows drugs you currently hold — already-returned items excluded)")
    print()
    if recent:
        for j, item in enumerate(recent, 1):
            dt = item["timestamp"][:10]
            print(f"  [{j}] {item['name']:<32}  x{item['net_qty']}  (bought {dt})")
    print("  [O]  Other / Not on list")
    print("  [0]  Skip identification")
    print()
    drug_id_sel = safe_input("  › ").strip().upper()

    identified_drug = None
    if drug_id_sel.isdigit():
        try:
            identified_drug = recent[int(drug_id_sel)-1]["name"]
            print(f"  ✓ Identified as: {identified_drug}")
        except (IndexError, ValueError):
            pass
    elif drug_id_sel == 'O':
        identified_drug = safe_input("  Drug name (or press Enter to skip): ").strip() or "Unidentified"

    if identified_drug:
        db.log_audit(cid, "CUPSULE_IDENTIFIED",
                     detail=f"Drug: {identified_drug}")

        # ── Nyansa consumption check + targeted Q&A ─────────────────────
        try:
            from aidplus.intelligence import (
                analyse_customer_drug_consumption, get_smart_questions,
                CONSUMPTION_QUESTIONS
            )
            consumption_data = analyse_customer_drug_consumption(cid, db)
            drug_data = next(
                (d for d in consumption_data
                 if identified_drug.lower() in d["drug"].lower()), None)

            # Get qty info for this specific drug from the recent list
            drug_item = next(
                (r for r in recent if r["name"] == identified_drug), None)
            net_qty   = drug_item["net_qty"] if drug_item else 1

            pct = drug_data.get("consumed_pct", 0.0) if drug_data else 0.0
            days_ago = drug_data.get("days_ago", 0.0) if drug_data else 0.0

            # Trigger check whenever < 90% estimated consumed
            if pct is not None and pct < 0.90:
                bar  = ("█" * int(pct * 10)).ljust(10, "░")
                risk = (drug_data or {}).get("risk_level", "green")
                risk_icon = {"green": "🟢", "amber": "🟡", "red": "🔴"}.get(risk, "⚪")

                print()
                print(f"  ━━━━  NYANSA CONSUMPTION CHECK  ━━━━")
                print(f"  Drug         : {identified_drug}")
                print(f"  You hold     : {net_qty} unit(s)")
                print(f"  Est. consumed: [{bar}] {int(pct*100)}%  {risk_icon}")
                print(f"  Owned for    : {int(days_ago)} day(s)")
                if drug_data:
                    print(f"  Note         : {drug_data['message']}")
                    print(f"  Expected dose: {drug_data['doses_per_day']}x/day"
                          f" ({", ".join(drug_data['timing'])})"
                          if drug_data.get("timing") else "")
                print()

                # Always ask how many containers being returned
                try:
                    returning_qty = int(safe_input(
                        f"  How many {identified_drug} containers are you returning"
                        f" (1–{net_qty}): ").strip() or "1")
                    returning_qty = max(1, min(returning_qty, net_qty))
                except ValueError:
                    returning_qty = 1
                print(f"  Returning: {returning_qty} of {net_qty} unit(s)")
                print()

                # Ask targeted questions
                flags     = (drug_data or {}).get("flags", [])
                questions = get_smart_questions(flags) if flags else [
                    "Did you take this medication as directed?",
                    f"How many doses of {identified_drug} have you taken so far?",
                    "Did you experience any side effects that caused you to stop?",
                    "Has your condition improved and you no longer need it?",
                ]
                print("  Please answer a few quick questions:")
                print("  " + "─" * 50)
                qa_answers = []
                for qi, q in enumerate(questions[:4], 1):
                    ans = safe_input(f"  Q{qi}. {q}\n  › ").strip()
                    qa_answers.append(f"Q: {q} | A: {ans}")
                    # Store QA in database
                    try:
                        with db._conn() as _qa:
                            from datetime import datetime as _dqa
                            _qa.execute(
                                "INSERT INTO consumption_qa "
                                "(customer_id,drug_name,question,answer,asked_at) "
                                "VALUES (?,?,?,?,?)",
                                (cid, identified_drug, q, ans,
                                 _dqa.now().isoformat()))
                    except Exception:
                        pass
                print("  " + "─" * 50)
                print()

                # Final confirmation
                confirm = safe_input(
                    "  Confirm you are done with this medication and want to return "
                    f"{returning_qty} container(s)? (y/n): "
                ).lower().strip()
                if confirm != "y":
                    print("  Cupsule return cancelled.")
                    input("  Press Enter…")
                    return

                # Flag for admin if consumption is low
                if pct < 0.50:
                    db.log_audit(cid, "CONSUMPTION_ANOMALY_FLAGGED",
                                 detail=(f"Drug: {identified_drug} | "
                                         f"{int(pct*100)}% consumed | "
                                         f"Returning {returning_qty}/{net_qty} | "
                                         f"QA: {"; ".join(qa_answers)}"))
                    try:
                        db.send_notification("ADMIN", "⚠ Consumption Anomaly",
                                             f"Customer {cid}: {identified_drug} "
                                             f"only {int(pct*100)}% consumed. "
                                             f"Returning {returning_qty}/{net_qty}.")
                    except Exception:
                        pass
                    print("  This has been flagged for admin review.")
        except Exception:
            pass  # Never block on intelligence error

    print()
    # Store drug identification in session log before CUPSCAN begins
    if identified_drug:
        db.log_audit(cid, "CUPSULE_PRE_DROP",
                     detail=f"Drug identified as: {identified_drug}")
    module = CUPSCANModule(db)
    cupscan_multi_drop_flow(
        customer=customer,
        db=db,
        cupscan_mod=module,
        connectivity=connectivity,
        power=power,
    )

    # ── Post-drop: mark drug as Cupsule-returned in medication_returns ─────
    # Wrapped in try/except — a DB lock here must never crash the Cupsule flow.
    if identified_drug:
        try:
            import time as _time
            from datetime import datetime as _dtt
            _time.sleep(0.3)  # brief pause to let CUPSCAN release its connection
            with db._conn() as _post:
                row = _post.execute(
                    "SELECT ti.id, ti.qty, ti.transaction_id "
                    "FROM transaction_items ti "
                    "JOIN transactions t ON t.id = ti.transaction_id "
                    "WHERE t.customer_id=? AND LOWER(ti.name) LIKE ? "
                    "AND ti.qty > COALESCE(("
                    "  SELECT SUM(mr.qty_returned) FROM medication_returns mr "
                    "  WHERE mr.transaction_id=ti.transaction_id AND mr.item_id=ti.id"
                    "),0) ORDER BY t.timestamp DESC LIMIT 1",
                    (cid, f"%{identified_drug.lower()[:10]}%")).fetchone()
                if row:
                    _post.execute(
                        "INSERT INTO medication_returns "
                        "(transaction_id,item_id,item_name,qty_returned,"
                        " customer_id,returned_at,amount_refunded,status) "
                        "VALUES (?,?,?,?,?,?,0.0,'cupsule_returned')",
                        (row["transaction_id"], row["id"], identified_drug,
                         row["qty"], cid, _dtt.now().isoformat()))
            # log_audit in a separate connection after the insert is committed
            if row:
                db.log_audit(cid, "CUPSULE_RETURN_LINKED",
                             detail=f"Drug {identified_drug} linked to cupsule return")
        except Exception as _ex:
            # Non-fatal — points were already awarded; just log and continue
            db.log_audit(cid, "CUPSULE_LINK_ERROR",
                         detail=f"Could not link {identified_drug}: {_ex}")


def browse_inventory_flow(db: DatabaseManager, customer: "Customer" = None):
    """
    Browse inventory with context-aware actions.
    - From main menu (guest): shows inventory, prompts to create account to buy.
    - From customer menu (logged in): shows inventory, offers Buy Drugs or Back.
    """
    display_inventory(db)

    if customer is None or customer.current is None:
        # Guest / unauthenticated path
        print("\n" + "─" * 58)
        print("  To purchase medication you need an AID PLUS+ account.")
        print("\n  [1]  Create Account     [0]  Back")
        ch = safe_input("  › ").strip()
        return "register" if ch == "1" else None
    else:
        # Authenticated customer path
        print("\n" + "─" * 58)
        print("  [1]  Buy Drugs           [0]  Back to menu")
        ch = safe_input("  › ").strip()
        if ch == "1":
            buy_drugs_flow(db, customer)
        return None



def return_medication_public(db: DatabaseManager, customer: Customer):
    """
    Return Medication — Main welcome menu.
    Path 1: Logged-in customer returning own purchase (2% fee).
    Path 2: Bystander (any person) who found dropped medication.
             No transaction ID required — system identifies via CLTS + purchase history.
    Anti-fraud: CLTS face check prevents buyer posing as bystander to avoid penalty.
    """
    import os as _os
    _os.system('cls' if _os.name == 'nt' else 'clear')
    print_header("RETURN MEDICATION")
    print()
    print("  [1]  I purchased this — returning my own medication")
    print("  [2]  I found this medication — returning on someone's behalf")
    print("  [0]  Back")
    choice = safe_input("  › ").strip()

    if choice == '1':
        # ── CUSTOMER RETURN: login → 2% fee ──────────────────────────────────
        cid = input("  Your Customer ID: ").strip()
        c   = db.get_customer(cid)
        if not c:
            print("  ✗ Customer ID not found.")
            input("  Press Enter…"); return

        # Identity verification — password in simulation, face in production
        if HW_SIMULATION_MODE:
            from aidplus.security import verify_password
            pwd = input("  Confirm password: ").strip()
            if not verify_password(c["password"], c.get("password_salt",""), pwd):
                print("  ✗ Identity verification failed.")
                input("  Press Enter…"); return
        else:
            from aidplus.clts import CLTSUnit
            from aidplus.auth import BiometricAuthService
            unit = CLTSUnit(db, biometric=BiometricAuthService(db))
            auth = unit.authenticate_face(cid)
            if not auth["verified"]:
                print("  ✗ Face verification failed. Access denied.")
                input("  Press Enter…"); return

        # Check recent purchases
        with db._conn() as _con:
            recent_txns = _con.execute(
                "SELECT id, total, timestamp FROM transactions "
                "WHERE customer_id=? ORDER BY timestamp DESC LIMIT 10",
                (cid,)).fetchall()

        if not recent_txns:
            print("  ✗ No recent purchases found.")
            input("  Press Enter…"); return

        print("\n  Recent purchases:")
        for i, t in enumerate(recent_txns, 1):
            dt = t["timestamp"][:10] if hasattr(t,"keys") else t[2][:10]
            amt = t["total"] if hasattr(t,"keys") else t[1]
            tid = t["id"] if hasattr(t,"keys") else t[0]
            print(f"  [{i}] {dt}  ₵{amt:.2f}  (Ref: {tid})")

        sel = input("\n  Select transaction to return (number): ").strip()
        try:
            txn = recent_txns[int(sel)-1]
        except (ValueError, IndexError):
            print("  ✗ Invalid selection.")
            input("  Press Enter…"); return

        txn_id = txn["id"] if hasattr(txn,"keys") else txn[0]
        total  = float(txn["total"] if hasattr(txn,"keys") else txn[1])

        with db._conn() as _con:
            already = _con.execute(
                "SELECT COUNT(*) FROM medication_returns WHERE transaction_id=?",
                (txn_id,)).fetchone()[0]
        if already:
            print("  ✗ This transaction has already been returned.")
            input("  Press Enter…"); return

        penalty = round(total * 0.02, 2)
        refund  = round(total - penalty, 2)
        print(f"\n  Purchase total : ₵{total:.2f}")
        print(f"  Processing fee : ₵{penalty:.2f}  (2% — handling & disposal)")
        print(f"  Refund amount  : ₵{refund:.2f}")
        print()
        print("  Medication is safely disposed. It cannot be resold.")

        if input("  Confirm return? (y/n): ").lower().strip() == 'y':
            c["balance"] = round(c.get("balance",0.0) + refund, 2)
            db.save_customer(c)
            db.add_wallet_entry(cid, "return", refund,
                                f"Medication return (2% fee applied): {txn_id}")
            from datetime import datetime as _dt
            with db._conn() as _con:
                _con.execute(
                    "INSERT INTO medication_returns "
                    "(transaction_id, customer_id, returned_at, amount_refunded) "
                    "VALUES (?,?,?,?)",
                    (txn_id, cid, _dt.now().isoformat(), refund))
            db.log_audit(cid, "RETURN_ACCEPTED", "medication_returns", txn_id,
                         f"Main menu return | refund ₵{refund:.2f} | fee ₵{penalty:.2f}")
            speak(f"Return accepted. ₵{refund:.2f} refunded to your wallet.", c)
            print(f"  ✅ ₵{refund:.2f} credited to account {cid}.")
        input("  Press Enter…")

    elif choice == '2':
        # ── BYSTANDER / FOUND DRUG PATH ──────────────────────────────────────
        # No receipt or transaction ID needed from finder.
        # System uses CLTS camera to identify the finder.
        # Anti-fraud: if finder's face matches a recent buyer of the same drug,
        # they are redirected to the customer return path (cannot avoid the 2% fee).
        print()
        print("  Thank you for reporting found medication.")
        print("  Stand in front of the camera for identity check.")
        print()

        finder_id   = None
        finder_name = "Anonymous"
        is_customer = False
        fraud_flag  = False

        # ── Identity check via CLTS ───────────────────────────────────────────
        if HW_SIMULATION_MODE:
            fid_input = input("  Enter your Customer ID (or press Enter if not a customer): ").strip()
            if fid_input:
                finder_c = db.get_customer(fid_input)
                if finder_c:
                    finder_id   = fid_input
                    finder_name = finder_c["name"]
                    is_customer = True
                    print(f"  ✓ Identity confirmed: {finder_name}")
                else:
                    print("  Customer ID not found. Proceeding as anonymous.")
        else:
            from aidplus.auth import BiometricAuthService
            bio = BiometricAuthService(db)
            match = bio.identify_by_face()
            if match:
                finder_id   = match["customer_id"]
                finder_name = match["name"]
                is_customer = True
                print(f"  ✓ Face recognised: {finder_name}")
            else:
                print("  Face not recognised. Proceeding as anonymous.")

        # ── Anti-fraud check ──────────────────────────────────────────────────
        # If this person is a customer, check if they recently bought a drug
        # that they could be trying to return via bystander path to avoid the 2% fee
        if is_customer and finder_id:
            with db._conn() as _con:
                recent_buys = _con.execute(
                    "SELECT t.id FROM transactions t "
                    "JOIN transaction_items ti ON ti.transaction_id = t.id "
                    "WHERE t.customer_id=? AND t.timestamp >= datetime('now','-24 hours') "
                    "AND t.id NOT IN (SELECT transaction_id FROM medication_returns)",
                    (finder_id,)).fetchall()
            if recent_buys:
                fraud_flag = True
                print()
                print("  ⚠  Your account has a recent purchase that has not been returned.")
                print("  Our records suggest this may be medication you purchased.")
                print("  To return medication you bought, please use Option [1].")
                print("  This is a security measure to protect all customers.")
                db.log_audit(finder_id, "FRAUD_ATTEMPT_DETECTED",
                             detail="Customer attempted bystander return with own recent purchase")
                input("  Press Enter…")
                return

        # ── Notify recent nearby buyers ───────────────────────────────────────
        # Find customers who purchased drugs in the last 4 hours — potential owners
        with db._conn() as _con:
            recent_buyers = _con.execute(
                "SELECT DISTINCT t.customer_id, c.name FROM transactions t "
                "JOIN customers c ON c.customer_id = t.customer_id "
                "WHERE t.timestamp >= datetime('now','-4 hours') "
                "AND t.customer_id != ? "
                "AND t.id NOT IN (SELECT transaction_id FROM medication_returns) "
                "LIMIT 5",
                (finder_id or "NONE",)).fetchall()

        notified = 0
        for buyer in recent_buyers:
            db.send_notification(
                buyer[0],
                "Did you lose your medication?",
                "Someone found medication near an AID PLUS+ terminal. "
                "If you believe you may have dropped medication, you can confirm "
                "and recover it for a small 2% handling fee. "
                "Reply YES from your profile to claim, or ignore this message."
            )
            notified += 1

        db.log_audit("SYSTEM", "FOUND_DRUG_REPORTED",
                     detail=f"Finder={'customer:'+finder_id if finder_id else 'anonymous'} "
                            f"| Notified {notified} recent buyers")

        # ── Reward the finder ─────────────────────────────────────────────────
        if is_customer and finder_id and not fraud_flag:
            db.cupscan_apply_points(finder_id, 5, "GOOD_SAMARITAN")
            db.send_notification(finder_id, "Good Samaritan Reward",
                "+5 loyalty points for returning found medication. "
                "Your act of honesty helps keep medication accessible to everyone.")
            print()
            print(f"  ✅ Thank you, {finder_name}!")
            print(f"  +5 loyalty points added to your account.")
            if notified > 0:
                print(f"  {notified} recent buyer(s) have been notified to check if they lost medication.")
        else:
            print()
            print("  ✅ Thank you for your honesty.")
            if notified > 0:
                print(f"  {notified} recent customer(s) have been notified.")
            print("  Create an AID PLUS+ account to earn loyalty points on future returns.")

        input("  Press Enter…")


def admin_maintenance_projections(db: DatabaseManager):
    """
    Maintenance Fund tracker and Annual Projections.
    Maintenance Fund = 15% of hardware deposit revenue (ring-fenced for ops costs).
    Annual Projections = Nyansa 90-day demand × 4 extrapolated to 12 months.
    """
    from datetime import datetime as _dt, timedelta as _td
    print_header("MAINTENANCE FUND & ANNUAL PROJECTIONS")
    W = 62

    # ── Maintenance Fund ──────────────────────────────────────────────────────
    MAINTENANCE_RESERVE_RATE = 0.15   # 15% of hardware deposit revenue
    with db._conn() as con:
        hw_deposits = abs(con.execute(
            "SELECT COALESCE(SUM(amount),0) FROM wallet_history "
            "WHERE type='Hardware Deposit'").fetchone()[0])
        hw_refunds  = abs(con.execute(
            "SELECT COALESCE(SUM(amount),0) FROM wallet_history "
            "WHERE type='Hardware Refund'").fetchone()[0])
        total_drug_rev = con.execute(
            "SELECT COALESCE(SUM(total),0) FROM transactions").fetchone()[0]
        bonus_paid = abs(con.execute(
            "SELECT COALESCE(SUM(amount),0) FROM wallet_history "
            "WHERE type='bonus_earn'").fetchone()[0])
        # 90-day drug revenue
        rev_90d = con.execute(
            "SELECT COALESCE(SUM(total),0) FROM transactions "
            "WHERE timestamp > ?",
            ((_dt.now()-_td(days=90)).isoformat(),)).fetchone()[0]
        # 90-day transaction count
        txn_90d = con.execute(
            "SELECT COUNT(*) FROM transactions WHERE timestamp > ?",
            ((_dt.now()-_td(days=90)).isoformat(),)).fetchone()[0]
        # Drug velocity (top 3)
        top_drugs = con.execute("""
            SELECT ti.name, SUM(ti.qty) as total_sold
            FROM transaction_items ti
            JOIN transactions t ON ti.transaction_id = t.id
            WHERE t.timestamp > ?
            GROUP BY ti.name ORDER BY total_sold DESC LIMIT 3
        """, ((_dt.now()-_td(days=90)).isoformat(),)).fetchall()

    maintenance_fund  = round(hw_deposits * MAINTENANCE_RESERVE_RATE, 2)
    hw_net            = round(hw_deposits - hw_refunds, 2)
    fund_used_est     = 0.0   # tracked when admin logs maintenance work (future)
    fund_available    = round(maintenance_fund - fund_used_est, 2)

    print(f"\n  {'─'*W}")
    print(f"  MAINTENANCE FUND  (15% of hardware deposit revenue)")
    print(f"  {'─'*W}")
    print(f"  Total HW Deposits       : ₵{hw_deposits:>9.2f}")
    print(f"  Total HW Refunds        : ₵{hw_refunds:>9.2f}")
    print(f"  Net HW Revenue          : ₵{hw_net:>9.2f}")
    print(f"  Maintenance Reserve     : ₵{maintenance_fund:>9.2f}  (15% of deposits)")
    print(f"  Maintenance Used        : ₵{fund_used_est:>9.2f}  (log via Maintenance Log)")
    print(f"  FUND AVAILABLE          : ₵{fund_available:>9.2f}")
    print()
    print(f"  Reserve covers:")
    print(f"    • Sterilization kits and supplies")
    print(f"    • Field technician visits")
    print(f"    • Hardware replacement parts")
    print(f"    • Physical cleaning and repainting")

    # ── Annual Projections ────────────────────────────────────────────────────
    # Based on last 90 days — extrapolate to 12 months (× 4.0)
    PROJECTION_FACTOR = 365.0 / 90.0  # annualise from 90-day window
    proj_drug_rev     = round(rev_90d * PROJECTION_FACTOR, 2)
    proj_txn_count    = round(txn_90d * PROJECTION_FACTOR)
    proj_bonus_cost   = round(bonus_paid * PROJECTION_FACTOR, 2)
    proj_maintenance  = round(maintenance_fund * PROJECTION_FACTOR, 2)
    proj_net          = round(proj_drug_rev + hw_net * PROJECTION_FACTOR
                              - proj_bonus_cost - proj_maintenance, 2)

    print(f"\n  {'─'*W}")
    print(f"  ANNUAL PROJECTIONS  (based on last 90 days × {PROJECTION_FACTOR:.1f})")
    print(f"  {'─'*W}")
    print(f"  Projected Drug Revenue  : ₵{proj_drug_rev:>10.2f}")
    print(f"  Projected Transactions  : {proj_txn_count:>10,}")
    print(f"  Projected Bonus Costs   : ₵{proj_bonus_cost:>10.2f}  (cashback to customers)")
    print(f"  Projected Maintenance   : ₵{proj_maintenance:>10.2f}  (15% of HW reserve)")
    print(f"  ─────────────────────────────────────────")
    print(f"  PROJECTED NET OPERATING : ₵{proj_net:>10.2f}")
    print()
    if proj_net > 0:
        print(f"  ✅ Positive operating outlook.")
    else:
        print(f"  ⚠  Operating at a loss — review pricing and bonus rates.")

    # ── Drug demand forecast ──────────────────────────────────────────────────
    if top_drugs:
        print(f"\n  TOP DRUGS — Annual Demand Forecast:")
        for drug in top_drugs:
            name  = drug[0] if not hasattr(drug,'keys') else drug['name']
            sold  = drug[1] if not hasattr(drug,'keys') else drug['total_sold']
            proj  = round(sold * PROJECTION_FACTOR)
            print(f"    {name:<30}: ~{proj:,} units/year")

    print(f"\n  NOTE: Projections assume consistent demand. Seasonal variation,")
    print(f"  new locations, and promotions will affect actual results.")
    print(f"  {'─'*W}")
    input("\n  Press Enter…")

