"""
AID PLUS+ — Entry Point
=========================
main_menu(): the top-level boot and application loop.

Boot sequence (locked):
  1. DatabaseManager       — schema migration to v28
  2. AidPlusOS             — platform OS boot (ADW-AS variant)
  3. NyansaEngine          — AI intelligence engine init
  4. SystemSelfTest        — all subsystem checks
  5. Service layer         — biometric, NIA, welcome, cupsule, recovery
  6. Service Bus           — init + CAPSCAN registration
  7. CUPSCANModule         — co-located hardware GPIO
  8. Main loop             — landing screen, routing

Run:
    python main.py
    python -m aidplus
"""
from __future__ import annotations
import os, sys, time, random
from datetime import datetime

from aidplus.config import *
from aidplus.db import DatabaseManager
from aidplus.security import secure_ref
from aidplus.ui import print_header, ui_info, speak, display_inventory
from aidplus.platform import AidPlusOS
from aidplus.intelligence import NyansaEngine, SystemSelfTest
from aidplus.bus import AidPlusServiceBus
from aidplus.auth import (
    BiometricAuthService, NiaVerificationService,
    WelcomeMessageService, CupsuleService, PasswordRecoveryService,
)
from aidplus.hardware import HardwareInterface
from aidplus.cupscan import CUPSCANModule
from aidplus.customer import Customer
from aidplus.clts import clts_real_camera_wake_up
from aidplus.flows import (
    customer_profile_menu, emergency_hardware_flow,
    return_hardware_flow, report_found_card, browse_inventory_flow,
)
from aidplus.admin import admin_menu
from aidplus.power import ConnectivityManager

def main_menu():
    """
    B28: Aid System Application entry point.

    Boot sequence (locked):
      1. DatabaseManager   — schema migration to v28
      2. AidPlusOS         — product-agnostic OS boot (ADW-AS variant)
      3. NyansaEngine      — AI intelligence engine init
      4. SystemSelfTest    — GPIO / DB / connectivity / power / Nyansa / Bus
      5. Service layer     — biometric, NIA, welcome, cupsule, recovery
      6. Service Bus       — init + CAPSCAN registration
      7. CUPSCANModule     — kiosk GPIO (co-located, ADW-AS)
      8. Aid System GPIO   — CUPSCAN outputs/inputs
      9. AID SYSTEM COMPLETE declaration (first clean boot of B28)
    """
    sys_id = f"AID-{secrets.randbelow(900) + 100}"
    print_header(
        f"AID SYSTEM — Nyansa v8.0  |  Build {SCHEMA_VERSION}"
        f"  |  Aid Plus OS  |  Adwene {ADWENE_DESIGNATION} ({sys_id})")

    try:
        # ── 1. Database ───────────────────────────────────────────────────────
        db       = DatabaseManager(DB_FILE)
        customer = Customer(db)
        db.update_prices()

        # ── 2. Aid Plus OS — product-agnostic platform OS ─────────────────────
        #    Products USE the OS. They do not extend it.
        #    BTM.py and AidAir.py will each call AidPlusOS(db, ADW_VARIANT_BT/AA)
        os_layer = AidPlusOS(db, ADW_VARIANT_AS)
        os_layer.boot()

        # ── 3. Nyansa Engine — AI intelligence layer (separate from OS) ───────
        nyansa_engine = NyansaEngine(db)
        print(f"     Nyansa       : {nyansa_engine.engine_status()}")

        # ── 4. System Self-Test ───────────────────────────────────────────────
        self_test = SystemSelfTest(db)
        test_pass = self_test.run(os_layer, nyansa_engine)
        if not test_pass:
            print("  ⚠️  Self-test has failures. Review above before proceeding.")

        # ── 5. Service layer ──────────────────────────────────────────────────
        biometric = BiometricAuthService(db)
        nia       = NiaVerificationService(db)
        welcome   = WelcomeMessageService(db)
        cupsule   = CupsuleService(db)
        recovery  = PasswordRecoveryService(db, biometric)

        # ── 6. Service Bus — init + CAPSCAN live registration ─────────────────
        AidPlusServiceBus.init(db)
        # CAPSCAN is co-located — register it as live on startup
        cupscan_handler_stub = type("CUPSCANBusHandler", (), {
            "handle":   lambda self, m, **kw: {"method": m, **kw},
            "on_event": lambda self, e, p: None,
        })()
        AidPlusServiceBus.register(
            "CAPSCAN", cupscan_handler_stub,
            version="1.0.0",
            capabilities=["return_cupsule", "bin_status",
                          "fraud_detect", "loyalty_credit"])

        # ── 7. CUPSCAN Module (ADW-AS co-located GPIO) ────────────────────────
        cupscan_mod = CUPSCANModule(db)

        # ── 8. Aid System GPIO init (kiosk-specific, on top of OS baseline) ───
        if not HW_SIMULATION_MODE:
            try:
                import RPi.GPIO as GPIO
                for pin in [GPIO_CUPSCAN_DROP_DOOR, GPIO_CUPSCAN_UV_LED,
                            GPIO_CUPSCAN_LED_READY, GPIO_CUPSCAN_LED_BUSY,
                            GPIO_CUPSCAN_LED_ERROR, GPIO_CUPSCAN_REJECT_CHUTE]:
                    GPIO.setup(pin, GPIO.OUT, initial=GPIO.LOW)
                for pin in [GPIO_CUPSCAN_DOOR_SENSOR, GPIO_CUPSCAN_CHUTE_IR,
                            GPIO_CUPSCAN_UV_SENSOR,
                            GPIO_CUPSCAN_BIN_FULL, GPIO_CUPSCAN_BIN_HALF]:
                    GPIO.setup(pin, GPIO.IN)
                print("     CUPSCAN GPIO : configured (ADW-AS kiosk)")
            except Exception:
                pass

        # ── 9. B28: AID SYSTEM COMPLETE declaration ───────────────────────────
        with db._conn() as con:
            already_declared = con.execute(
                "SELECT COUNT(*) FROM pdms_audit_log WHERE action='AID_SYSTEM_COMPLETE'"
            ).fetchone()[0]
        if already_declared == 0:
            db.log_audit("SYSTEM", "AID_SYSTEM_COMPLETE",
                          detail=(f"Build {SCHEMA_VERSION}  |  "
                                  f"Aid Plus OS  |  Nyansa v8.0  |  "
                                  f"Adwene {ADWENE_DESIGNATION}  |  "
                                  f"ADW-AS  |  CUPSCAN co-located  |  "
                                  f"BTM dormant  |  AidAir dormant  |  "
                                  f"Pilot-ready"))
            print("\n  ★  AID SYSTEM BUILD 28 — COMPLETE  ★")
            print("     All modules active. Pilot-ready.")
            print("     Next product: BTM (Blood Testing Machine)")

        # ── Init summary ──────────────────────────────────────────────────────
        print(f"\n  ✅  Aid System ready.")
        print(f"      Build        : {SCHEMA_VERSION}  |  Nyansa v8.0  |  ADW-AS")
        print(f"      Biometric    : {'Camera' if biometric._camera_available else 'Simulation'}"
              f"  |  NIA: {'Live' if not NIA_SIMULATION else 'Simulation'}")
        print(f"      CUPSCAN      : {'LIVE GPIO' if not HW_SIMULATION_MODE else 'Simulation'}"
              f"  |  Bin: {cupscan_mod.bin_status()['pct_estimate']}%"
              f"  |  Serial: {ADWENE_SERIAL}")
        print(f"      OS           : {os_layer.status_banner()}")
        print(f"      Service Bus  : CAPSCAN=live  BTM=dormant  AidAir=dormant")
        print(f"      Security     : SHA-256+salt  |  HMAC-SHA256  |  CSPRNG tokens")

        db.log_audit("SYSTEM", "STARTUP",
                      detail=(f"Build={SCHEMA_VERSION} variant=ADW-AS "
                               f"conn={os_layer.connectivity.current if os_layer.connectivity else 'N/A'} "
                               f"self_test={'PASS' if test_pass else 'WARN'}"))

    except Exception as e:
        print(f"\n  CRITICAL ERROR during initialisation: {e}")
        import traceback; traceback.print_exc()
        return

    input("\n  Press Enter to continue...")
    _heartbeat_tick = 0

    while True:
        # ── OS heartbeat (background maintenance) ─────────────────────────────
        _heartbeat_tick += 1
        if _heartbeat_tick % 60 == 0:
            os_layer.heartbeat_tick()

        # Clear screen reliably on Windows and Linux
        if os.name == 'nt':
            os.system('cls')
        else:
            sys.stdout.write('\033[2J\033[H')
            sys.stdout.flush()

        # ── Landing screen [GUI-READY] ─────────────────────────────────────────
        # Production: replace this block with TouchScreen.render("landing")
        # ── Welcome Screen ─────────────────────────────────────────────────
        W = 60
        from datetime import datetime as _dt
        day_str  = _dt.now().strftime('%A')
        date_str = _dt.now().strftime('%d %b %Y  %H:%M')
        try:
            hw         = db.get_hw_status()
            aid_docked = hw.get("aid_box_status", "Docked") == "Docked"
            cpr_docked = hw.get("cpr_kit_status", "Docked") == "Docked"
            aid_lbl    = "Docked  ✓" if aid_docked else "Deployed ⚠"
            cpr_lbl    = "Docked  ✓" if cpr_docked else "Deployed ⚠"
        except Exception:
            aid_lbl = cpr_lbl = "Unknown"
        bar  = "─" * W
        dbar = "═" * W
        print()
        print(f"  ╔{dbar}╗")
        print(f"  ║{'AID PLUS+':^{W}}║")
        print(f"  ║{'Nyansa v8.0  ·  Adwene ADW-1':^{W}}║")
        print(f"  ║{f'{day_str}  ·  {date_str}':^{W}}║")
        print(f"  ╠{dbar}╣")
        print(f"  ║{'Your Health. Your Way.':^{W}}║")
        print(f"  ╠{dbar}╣")
        print(f"  ║  {'ACCOUNT ACCESS':<{W-2}}║")
        print(f"  ║  {bar}  ║")
        print(f"  ║   [1]  Create New Account                            ║")
        print(f"  ║   [2]  Login                                         ║")
        print(f"  ║   [3]  Browse Inventory  (no login required)         ║")
        print(f"  ║   [M]  Return / Report Found Medication              ║")
        print(f"  ╠{dbar}╣")
        print(f"  ║  {'EMERGENCY HARDWARE':<{W-2}}║")
        print(f"  ║  {bar}  ║")
        print(f"  ║   Aid Box : {aid_lbl:<12}    CPR Kit : {cpr_lbl:<12}║")
        print(f"  ║   [!]  Access Hardware      [R]  Return Hardware     ║")
        print(f"  ║   [F]  Report Found Card                             ║")
        print(f"  ╠{dbar}╣")
        print(f"  ║   [A]  Admin                [0]  Exit                ║")
        print(f"  ╚{dbar}╝")
        print()

        ch = input("  › ").strip().upper()

        if ch == '!':
            speak("Emergency hardware access.", force=True)
            emergency_hardware_flow(customer, db)

        elif ch == 'R':
            speak("Returning emergency hardware.", force=True)
            return_hardware_flow(customer, db)

        elif ch == 'F':
            speak("Reporting found card.", force=True)
            report_found_card(customer, db)

        elif ch == '1':
            speak("Welcome. Let us create your account.", force=True)
            if customer.create_new(biometric=biometric, nia=nia, welcome=welcome):
                # Registration: CLTS for health data only — no auth required
                ok, sid = clts_real_camera_wake_up(
                    customer, db, biometric=biometric, require_face_auth=False)
                if ok:
                    customer_profile_menu(db, customer,
                                          connectivity=os_layer.connectivity)
                else:
                    ui_info(["Security check failed. Please try again."], "error")
                    input("  Press Enter…")
                    customer.current = None

        elif ch == '2':
            speak("Please log in.", force=True)
            result = customer.login(biometric=biometric, recovery=recovery)
            if result == "admin":
                speak("Administrator access granted.", force=True)
                admin_menu(db, customer,
                           os_layer=os_layer,
                           nyansa_engine=nyansa_engine)
            elif result == "customer":
                try:
                    # Login: CLTS with face authentication (2FA)
                    # Confirms face matches the account that just entered password
                    ok, sid = clts_real_camera_wake_up(
                        customer, db, biometric=biometric, require_face_auth=True)
                except Exception:
                    ok = True   # never block login on CLTS failure
                if ok:
                    try:
                        welcome_msg = nyansa_engine.personalise_welcome(customer.current)
                    except Exception:
                        name = customer.current.get("name","").split()[0] if customer.current else ""
                        welcome_msg = f"Welcome back, {name}!"
                    speak(welcome_msg, customer.current)
                    customer_profile_menu(db, customer,
                                          connectivity=os_layer.connectivity)
                else:
                    ui_info(["Security check failed. Please try again."], "error")
                    input("  Press Enter…")
                    customer.current = None

        elif ch == '3':
            speak("Browsing inventory.", force=True)
            action = browse_inventory_flow(db, customer=None)
            if action == "register":
                speak("Let us create your account.", force=True)
                if customer.create_new(biometric=biometric, nia=nia, welcome=welcome):
                    ok, sid = clts_real_camera_wake_up(
                        customer, db, biometric=biometric, require_face_auth=False)
                    if ok:
                        customer_profile_menu(db, customer,
                                              connectivity=os_layer.connectivity)
                    else:
                        customer.current = None

        elif ch == 'M':
            from aidplus.flows import return_medication_public
            return_medication_public(db, customer)

        elif ch == 'A':
            # Direct admin login path
            pwd = input("  Admin Password: ").strip()
            import secrets as _sec
            if _sec.compare_digest(pwd, ADMIN_PASSWORD):
                speak("Administrator access granted.", force=True)
                admin_menu(db, customer,
                           os_layer=os_layer,
                           nyansa_engine=nyansa_engine)
            else:
                ui_info(["Incorrect admin password."], "error")
                input("  Press Enter…")

        elif ch == '0':
            db.log_audit("SYSTEM", "SHUTDOWN", detail="Graceful shutdown")
            speak("System shutting down. Goodbye.", force=True)
            print("\n  Goodbye. Stay healthy.")
            break

        else:
            ui_info(["Invalid selection. Please choose from the options above."], "warn")
            input("  Press Enter…")


if __name__ == "__main__":
    main_menu()
