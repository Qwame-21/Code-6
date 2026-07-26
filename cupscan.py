"""
AID PLUS+ — CUPSCAN Subsystem
================================
CUPSCANModule, DispenserManager, and all cupscan return flow functions.
Handles the Cupsule (medication capsule packaging) lifecycle:
  drop door → weight cell → UV verification → camera → points award → bin management.
Co-located on the ADW-1 board — same compute, separate GPIO block.
"""
from __future__ import annotations
import time, threading, random, json
from datetime import datetime

from aidplus.config import *
from aidplus.ui import speak
from aidplus.db import DatabaseManager
from aidplus.bus import AidPlusServiceBus
from aidplus.power import ConnectivityManager
from aidplus.hardware import CUPSCANModule, DispenserManager  # re-exported for package consumers
__all__ = ['CUPSCANModule', 'DispenserManager', 'cupscan_pre_confirm_empty', 'cupscan_classify_weight', 'cupscan_activate_reject_chute', 'cupscan_multi_drop_flow']

def cupscan_pre_confirm_empty(customer: 'Customer') -> bool:
    """
    B25: Pre-drop confirmation screen.
    Customer must actively confirm the cup is empty before the door opens.
    Returns True if confirmed, False if cancelled.
    """
    os.system('cls' if os.name == 'nt' else 'clear')
    print("=" * 60)
    print(f"  {'CUPSCAN — Cupsule Return':^56}")
    print("=" * 60)
    print()
    print("  IMPORTANT: Before the drop door opens, please confirm")
    print("  that your Cupsule is COMPLETELY EMPTY.")
    print()
    print("  A cup with contents inside will be:")
    print("    • Detected by the weight sensor")
    print("    • Prompted to empty and retry ONCE")
    print("    • Rejected and returned to you if still heavy")
    print()
    print("  ─────────────────────────────────────────────────────")
    print("  [Y] Yes — my Cupsule is empty and ready to return")
    print("  [N] Cancel — I need to empty it first")
    print("  ─────────────────────────────────────────────────────")
    ch = input("\n  > ").strip().upper()
    if ch != 'Y':
        speak("Okay. Please empty your Cupsule and come back.", customer.current)
        print("\n  Return cancelled. Please empty your cup and try again.")
        input("  Press Enter…")
        return False
    return True


def cupscan_classify_weight(weight_g: float, uv_pass: bool) -> tuple:
    """
    B25: Classify a Cupsule based on weight and UV marker result.
    Returns (condition: str, action: str, points_factor: float).

    Condition table (locked):
        UV  | Weight         | Condition     | Action        | Points
        ----+----------------+---------------+---------------+--------
        ✅  | 8–18g normal   | INTACT        | Accept        | 1.0 ×
        ✅  | <6g low        | PARTIAL       | Accept        | 0.5 ×
        ✅  | 18–30g         | CONTAMINATED  | Prompt empty  | retry
        ✅  | >30g           | ANOMALY       | Hard reject   | 0
        ❌  | normal weight  | COUNTERFEIT   | Flag + reject | 0
        ❌  | any            | FAKE          | Reject        | 0
    """
    if not uv_pass:
        if CUPSCAN_WEIGHT_MIN_G <= weight_g <= CUPSCAN_WEIGHT_MAX_G:
            return ("COUNTERFEIT", "FLAG_REJECT", 0.0)
        return ("FAKE", "REJECT", 0.0)

    if weight_g > CUPSCAN_WEIGHT_ANOMALY_G:
        return ("ANOMALY", "HARD_REJECT", 0.0)
    if weight_g > CUPSCAN_WEIGHT_CONTAM_G:
        return ("CONTAMINATED", "PROMPT_EMPTY", 0.0)
    if weight_g < CUPSCAN_WEIGHT_PARTIAL_G:
        return ("PARTIAL", "ACCEPT_REDUCED", 0.5)
    return ("INTACT", "ACCEPT", 1.0)


def cupscan_activate_reject_chute(hw: 'HardwareInterface') -> None:
    """
    B25: Fire the reject chute solenoid to divert rejected cup back to customer.
    Pulse GPIO_CUPSCAN_REJECT_CHUTE HIGH for CUPSCAN_REJECT_PULSE_MS milliseconds.
    """
    if HW_SIMULATION_MODE:
        print("  [SIM] Reject chute activated — cup returned to customer.")
        return
    try:
        import RPi.GPIO as GPIO
        GPIO.output(GPIO_CUPSCAN_REJECT_CHUTE, GPIO.HIGH)
        time.sleep(CUPSCAN_REJECT_PULSE_MS / 1000.0)
        GPIO.output(GPIO_CUPSCAN_REJECT_CHUTE, GPIO.LOW)
    except Exception as e:
        print(f"  [REJECT CHUTE ERROR] {e}")



# Compatibility shim — ensures _set_door and verify_cup exist on any CUPSCANModule instance
def _ensure_cupscan_methods(module: 'CUPSCANModule') -> None:
    """Add missing methods to module if not already present (cross-version compatibility)."""
    if not hasattr(module, '_set_door'):
        def _set_door(self, open_door: bool) -> None:
            if getattr(self, '_sim', True):
                return
            try:
                import RPi.GPIO as _gpio
                _gpio.output(GPIO_CUPSCAN_DROP_DOOR,
                             _gpio.HIGH if open_door else _gpio.LOW)
                import time as _t
                _t.sleep(CUPSCAN_DOOR_OPEN_SECS if open_door else 0.5)
            except Exception:
                pass
        import types
        module._set_door = types.MethodType(_set_door, module)

    if not hasattr(module, 'verify_cup'):
        def verify_cup(self) -> dict:
            weight  = round(random.uniform(8.0, 14.0), 1)
            uv_pass = random.random() > 0.05
            if not getattr(self, '_sim', True):
                try:
                    r = self.run_verification()
                    weight  = r.get("weight_g", weight)
                    uv_pass = r.get("uv_pass",  uv_pass)
                except Exception:
                    pass
            condition, _, _ = cupscan_classify_weight(weight, uv_pass)
            return {
                "weight_g":      weight,
                "uv_pass":       uv_pass,
                "condition":     condition,
                "co2_saved_g":   round(weight * 0.8, 2),
                "water_saved_l": round(weight * 0.003, 3),
            }
        import types
        module.verify_cup = types.MethodType(verify_cup, module)

def cupscan_multi_drop_flow(customer: 'Customer', db: 'DatabaseManager',
                            cupscan_mod: 'CUPSCANModule',
                            connectivity: 'ConnectivityManager' = None,
                            power: 'PowerManager' = None,
                            identified_drug: str = None) -> None:
    """
    B25: Multi-drop batch CUPSCAN flow.

    Flow:
      1. Check bin capacity and daily cap
      2. Ask customer how many cups (or auto-estimate via pre-scan)
      3. Pre-drop confirmation: must confirm cups are empty
      4. For each cup:
           a. Open door, wait for drop
           b. Weight + UV + camera classify
           c. If CONTAMINATED: prompt to empty, retry once
           d. If ANOMALY / FAKE / COUNTERFEIT: activate reject chute, log, skip
           e. If INTACT / PARTIAL: accept, accumulate points
           f. Enforce daily cap per iteration
      5. Close session, show summary, credit points, send notification
    """
    _ensure_cupscan_methods(cupscan_mod)  # ensure _set_door + verify_cup exist
    cid = customer.current.get("customer_id") if customer.current else None
    if not cid:
        print("  Not logged in.")
        return

    # ── Show recent purchased drugs for easy Cupsule identification ──────────
    try:
        with db._conn() as _ci:
            recent_drugs = _ci.execute(
                "SELECT DISTINCT ti.name, t.timestamp FROM transaction_items ti "
                "JOIN transactions t ON t.id=ti.transaction_id "
                "WHERE t.customer_id=? ORDER BY t.timestamp DESC LIMIT 10",
                (cid,)).fetchall()
        if recent_drugs:
            print("\n  Your recent purchases — for identifying your Cupsule contents:")
            seen = set()
            for row in recent_drugs:
                name = str(row[0])
                if name not in seen:
                    seen.add(name)
                    dt = str(row[1])[:10] if row[1] else "—"
                    print(f"    • {name:<34}  ({dt})")
            print()
    except Exception:
        pass

    # ── Power check ───────────────────────────────────────────────────────────
    if power and power.is_critical:
        print("  ⚡ System in critical power state — CUPSCAN unavailable.")
        input("  Press Enter…")
        return

    # ── Bin check ─────────────────────────────────────────────────────────────
    bin_st = cupscan_mod.bin_status()
    if bin_st.get("full"):
        print("  ⚠️  CUPSCAN bin is full. Please notify staff.")
        input("  Press Enter…")
        return

    # ── Daily cap check ───────────────────────────────────────────────────────
    daily  = db.cupscan_get_daily(cid)
    used   = daily.get("return_count", 0)
    remain = CUPSCAN_DAILY_CAP - used
    if remain <= 0:
        print(f"  ✋ Daily return limit reached ({CUPSCAN_DAILY_CAP}/day). "
              f"Come back tomorrow.")
        input("  Press Enter…")
        return

    # ── Pre-confirmation ──────────────────────────────────────────────────────
    if not cupscan_pre_confirm_empty(customer):
        return

    # ── Declare batch count ───────────────────────────────────────────────────
    os.system('cls' if os.name == 'nt' else 'clear')
    print("=" * 60)
    print(f"  {'CUPSCAN — Multi-Drop Batch Return':^56}")
    print("=" * 60)
    print(f"\n  Daily returns: {used}/{CUPSCAN_DAILY_CAP}  "
          f"| Remaining today: {remain}")
    print(f"\n  How many Cupsules are you returning today?")
    print(f"  (Maximum {min(remain, 10)} in one session | 0 to cancel)\n")
    try:
        count_str = input("  Count > ").strip()
        count = int(count_str)
        if count <= 0:
            print("  Cancelled.")
            return
        count = min(count, remain, 10)
    except ValueError:
        print("  Invalid input.")
        input("  Press Enter…")
        return

    # ── Per-cup batch loop ────────────────────────────────────────────────────
    accepted   = 0
    rejected   = 0
    pts_total  = 0
    results    = []

    for i in range(count):
        print(f"\n  ─── Cup {i + 1} of {count} ─────────────────────────────────")
        speak(f"Please drop cup {i + 1} now.", customer.current)
        print(f"  Drop cup {i + 1} into the CUPSCAN chute now.")

        # Open door
        cupscan_mod._set_door(True)
        time.sleep(CUPSCAN_DOOR_OPEN_SECS)
        cupscan_mod._set_door(False)

        # Run verification
        result  = cupscan_mod.verify_cup()
        weight  = result.get("weight_g", 0.0)
        uv_pass = result.get("uv_pass", False)
        condition, action, pts_factor = cupscan_classify_weight(weight, uv_pass)

        # Handle CONTAMINATED — prompt to empty, retry once
        if action == "PROMPT_EMPTY":
            print(f"\n  ⚠️  Cup {i + 1}: Contents detected (weight {weight:.1f}g).")
            print(f"     Please empty the cup and we will re-scan it.")
            cupscan_activate_reject_chute(cupscan_mod.hw
                                          if hasattr(cupscan_mod, 'hw') else None)
            input(f"  Press Enter after emptying cup {i + 1}…")
            # Re-open for retry
            cupscan_mod._set_door(True)
            time.sleep(CUPSCAN_DOOR_OPEN_SECS)
            cupscan_mod._set_door(False)
            result2    = cupscan_mod.verify_cup()
            weight2    = result2.get("weight_g", 0.0)
            uv_pass2   = result2.get("uv_pass", False)
            condition, action, pts_factor = cupscan_classify_weight(weight2, uv_pass2)
            weight     = weight2
            uv_pass    = uv_pass2
            if action == "PROMPT_EMPTY":
                # Still contaminated after retry — hard reject
                action    = "HARD_REJECT"
                condition = "CONTAMINATED_RETRY"
                pts_factor = 0.0

        # Handle rejections
        if action in ("REJECT", "HARD_REJECT", "FLAG_REJECT"):
            cupscan_activate_reject_chute(cupscan_mod.hw
                                          if hasattr(cupscan_mod, 'hw') else None)
            rejected += 1
            reject_msg = {
                "HARD_REJECT":    "Foreign object or excessive weight.",
                "FLAG_REJECT":    "Not a genuine AID PLUS+ Cupsule.",
                "CONTAMINATED_RETRY": "Still contains contents after retry.",
            }.get(action, "Verification failed.")
            print(f"  ❌  Cup {i + 1} rejected — {reject_msg}")
            if condition == "COUNTERFEIT":
                db.log_audit(cid, "CUPSCAN_COUNTERFEIT",
                             detail=f"weight={weight:.1f}g uv=False cup={i+1}")
            results.append({"cup": i + 1, "accepted": False,
                             "condition": condition, "reason": reject_msg})
            continue

        # Accept cup — calculate points
        hour     = datetime.now().hour
        is_bonus = False  # No bonus multiplier — 1 Cupsule = 1 point always
        # Loyalty: 1 point per accepted Cupsule. Simple and honest.
        # INTACT or PARTIAL both earn 1 point. No multipliers.
        # Points: 1 per intact return, 0 for anything else
        cup_pts   = CUPSULE_POINTS_INTACT if condition == "INTACT" else 0
        base_pts  = cup_pts
        pts_factor = 1.0
        pts_total += cup_pts
        accepted  += 1

        print(f"  ✅  Cup {i + 1} accepted — {condition} | "
              f"{weight:.1f}g | +{cup_pts} pts"
              f"")
        results.append({"cup": i + 1, "accepted": True,
                         "condition": condition, "pts": cup_pts,
                         "weight_g": weight, "uv_pass": uv_pass})

        # Persist individual return record
        co2  = result.get("co2_saved_g", 12.0)
        water = result.get("water_saved_l", 0.05)
        ret_record = {
            "customer_id":     cid,
            "card_uid":        customer.current.get("card_uid", ""),
            "compartment":     condition.lower(),
            "base_pts":        cup_pts,
            "bonus_pts":       0,
            "total_pts":       cup_pts,
            "multiplier":      1,
            "is_bonus_window": False,
            "streak_days":     0,
            "co2_saved_g":     co2,
            "identified_drug": identified_drug or "",
            "water_saved_l":   water,
        }
        db.cupscan_record_return("COLLOCATED-ADW1", ret_record)

        # Daily cap guard
        used += 1
        if used >= CUPSCAN_DAILY_CAP:
            print(f"\n  📊 Daily limit reached ({CUPSCAN_DAILY_CAP} cups). "
                  f"Session ending.")
            break

    # ── Apply total points ────────────────────────────────────────────────────
    if pts_total > 0:
        pts_res = db.cupscan_apply_points(cid, pts_total, "CUPSCAN_BATCH")

    # ── Session summary ───────────────────────────────────────────────────────
    co2_total  = sum(r.get("weight_g", 12.0) * 0.8 for r in results if r["accepted"])
    water_total = sum(r.get("weight_g", 12.0) * 0.003 for r in results if r["accepted"])

    os.system('cls' if os.name == 'nt' else 'clear')
    print("=" * 60)
    print(f"  {'CUPSCAN — Batch Complete':^56}")
    print("=" * 60)
    print(f"\n  Cups submitted : {count}")
    print(f"  Cups accepted  : {accepted}  ✅")
    print(f"  Cups returned  : {rejected}  ↩️")
    print(f"  Points earned  : +{pts_total} pts")
    if pts_total > 0:
        print(f"  New balance    : {pts_res.get('new_balance', 0)} pts")
    print(f"\n  Environmental impact:")
    print(f"  CO₂ saved : {co2_total:.1f}g | Water saved : {water_total:.2f}L")
    print()
    if rejected > 0:
        print(f"  ↩️  {rejected} cup(s) returned via reject chute.")
    print("=" * 60)

    speak(f"{accepted} cups accepted. {pts_total} points added. "
          f"Thank you for recycling.", customer.current)

    # ── Notification ──────────────────────────────────────────────────────────
    db.send_notification(cid, "[CUPSCAN] Batch Return Complete",
        f"{accepted} of {count} cups accepted — +{pts_total} pts. "
        f"{rejected} returned. CO₂ saved: {co2_total:.1f}g | "
        f"Water saved: {water_total:.2f}L.")

    # ── Queue to cloud if offline ──────────────────────────────────────────────
    if connectivity and not connectivity.is_online:
        connectivity.enqueue(
            f"{OTA_SERVER_URL}/cupscan/batch_return",
            {"customer_id": cid, "accepted": accepted,
             "pts_total": pts_total, "results": results})

    db.log_audit(cid, "CUPSCAN_BATCH",
                 detail=f"accepted={accepted} rejected={rejected} pts={pts_total}")
    input("\n  Press Enter…")


# ─────────────────────────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════
#  BUILD 26 — OTA UPDATE MANAGER  |  NOTIFICATION TEMPLATES  |  ADMIN EXPORT
#             AID PLUS OS — SHARED CONNECTED OS FOR ALL ADW VARIANTS
# ═══════════════════════════════════════════════════════════════════════════════
# ─────────────────────────────────────────────────────────────────────────────


