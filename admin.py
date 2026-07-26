"""
AID PLUS+ — Admin Menu & Panel
================================
Full admin interface:
  - admin_menu: top-level admin routing
  - OS status panel (power, connectivity, OTA, service bus)
  - CUPSCAN platform dashboard
  - NIA verification log
  - Light Loop test
  - Cupsule management
  - All analytics submenus
"""
from __future__ import annotations
import os, csv, json, random, time
from datetime import datetime, timedelta

from aidplus.config import *
from aidplus.db import DatabaseManager
from aidplus.security import secure_id, secure_ref
from aidplus.ui import print_header, ui_info, ui_section, speak
from aidplus.customer import Customer
from aidplus.services import (
    SupportService, TeleconsultService, NyansaIntelligence,
    PromotionEngine, RestockEngine, OTAService,
    NotificationService, SchedulerService,
)
from aidplus.bus import AidPlusServiceBus
from aidplus.auth import (BiometricAuthService, WelcomeMessageService,
    CupsuleService, NiaVerificationService, PasswordRecoveryService)
from aidplus.platform import AidPlusOS, AdminExportService
from aidplus.intelligence import NyansaEngine, SystemSelfTest, admin_csv_export_menu
from aidplus.reporting import ReportingService
from aidplus.api import APIServer
from aidplus.flows import (
    collocated_cupsule_return, admin_maintenance_projections,
    admin_check_stock, admin_check_customer_data, admin_check_transactions,
    admin_pdms_audit_log, admin_manage_communications,
    admin_get_analytics, admin_revenue_report, hardware_analytics_report,
    admin_audit_dashboard, admin_reset_sanitization, clear_all_data,
    admin_support_tickets, admin_teleconsult_queue, admin_prescriptions,
    admin_intelligence_dashboard, admin_restock_orders, admin_promotions,
    support_menu_customer, admin_hardware_menu, admin_reports_menu,
    admin_ota_menu, admin_broadcast, admin_scheduler_status,
    admin_dispatch_log, admin_api_server, admin_api_tokens,
)
from aidplus.power import PowerManager, ConnectivityManager
from aidplus.hardware import HardwareInterface, TemperatureSensor, CardReader
from aidplus.cupscan import CUPSCANModule, DispenserManager

def admin_menu(db: DatabaseManager, customer: Customer,
               os_layer: 'AidPlusOS' = None,
               nyansa_engine: 'NyansaEngine' = None):
    """
    B28: Unified Admin Menu — single loop, full feature set.
    Accepts os_layer and nyansa_engine from main_menu for live OS panel.
    """
    support_svc  = SupportService(db)
    consult_svc  = TeleconsultService(db)
    nyansa       = NyansaIntelligence(db)
    promo_engine = PromotionEngine(db)
    restock_eng  = RestockEngine(db, nyansa)
    notif_svc    = NotificationService(db)
    scheduler    = SchedulerService(db)
    ota_svc      = OTAService(db)
    api_server   = APIServer(db)
    hw           = HardwareInterface(db)
    export_svc   = AdminExportService(db) if os_layer is None else os_layer.export

    scheduler.start()
    hw.sync_leds_to_db()

    while True:
        hw_st        = db.get_hw_status()
        maint_alert  = (
            "\n  ⚠️  SANITIZATION REQUIRED!"
            if hw_st.get("aid_box_usage", 0) >= 5
            or hw_st.get("cpr_kit_usage", 0) >= 5 else "")
        fb_count     = len(db.get_all_feedback())
        fb_tag       = f" ({fb_count} Pending)" if fb_count else ""
        tkts         = support_svc.get_summary()
        tkt_open     = tkts.get("open", 0) + tkts.get("in_progress", 0)
        tkt_tag      = f" ({tkt_open} open)" if tkt_open else ""
        q_waiting    = consult_svc.get_summary()["waiting"]
        q_tag        = f" ({q_waiting} waiting)" if q_waiting else ""
        pending_ord  = len(restock_eng.get_pending())
        ord_tag      = f" ({pending_ord})" if pending_ord else ""
        api_status   = "🟢 ON" if api_server._running else "⚪ OFF"
        sched_status = "🟢 ON" if scheduler._running else "⚪ OFF"
        hw_mode      = "SIM" if HW_SIMULATION_MODE else "LIVE"
        audit_cnt    = len(db.get_pending_audits())
        audit_tag    = f" ({audit_cnt}!)" if audit_cnt else ""

        # B28: Live OS status for header
        os_banner = os_layer.status_banner() if os_layer else ""

        print_header(
            f"ADMINISTRATOR MENU — Nyansa v8.0 | Build {SCHEMA_VERSION} | ADW-AS")
        if maint_alert:
            print(maint_alert)
        if os_banner:
            print(f"\n  OS  {os_banner}")

        print("\n─── MANAGEMENT ──────────────────────────────────────")
        print("  [1] Inventory Management")
        print(f"  [2] Reports & Communications{fb_tag}")
        print("  [3] Customer Data Access")
        print("\n─── CLINICAL & SUPPORT ──────────────────────────────")
        print(f"  [T] Support Tickets{tkt_tag}")
        print(f"  [D] Doctor Teleconsult Queue{q_tag}")
        print(f"  [RX] Prescriptions")
        print("\n─── NYANSA INTELLIGENCE ENGINE ──────────────────────")
        print("  [I] Intelligence Dashboard")
        print("  [CA] Consumption Anomalies")
        print("  [N] Run Nyansa Analysis Now")
        print(f"  [O] Restock Orders{ord_tag}")
        print("  [M] Promotions Manager")
        print("\n─── AID PLUS OS — PLATFORM ──────────────────────────")
        print(f"  [Y] OS Status & Self-Test")          # B28
        print(f"  [U] OTA Updates")
        print(f"  [B] Broadcast Notification")
        print(f"  [J] Scheduler  [{sched_status}]")
        print(f"  [L] Dispatch Log")
        print(f"  [A] API Server  [{api_status}]")
        print(f"  [W] API Tokens")
        print("\n─── HARDWARE & CUPSCAN ──────────────────────────────")
        print(f"  [H] Hardware Interface  [{hw_mode}]")
        print(f"  [E] CUPSCAN Platform Dashboard")
        print("\n─── SERVICE BUS & CUPSULE ───────────────────────────")
        print(f"  [V] Service Bus Status  (live + dormant)")  # B28
        print(f"  [C] Cupsule Management")
        print(f"  [K] NIA Verification Log")
        print(f"  [Z] Light Loop Test")
        print("\n─── ANALYTICS & REPORTS ─────────────────────────────")
        print("  [4] Transactions   [5] Analytics   [6] Hardware")
        print(f"  [G] Audit Returns{audit_tag}   [R] Revenue   [P] PDMS Log")
        print(f"  [F] Export CSV Reports")                    # B28
        print(f"  [Q] Reports & BI")
        print("\n─── MAINTENANCE ──────────────────────────────────────")
        print("  [7] Wipe All Data (DANGER)   [S] Reset Sanitization")
        print("\n  [0] Logout")
        print("─────────────────────────────────────────────────────")

        ch = input("  Option > ").strip().upper()

        if   ch == '1': admin_check_stock(db)
        elif ch == '2': admin_manage_communications(customer, db)
        elif ch == '3': admin_check_customer_data(customer, db)
        elif ch == 'T': admin_support_tickets(support_svc, db)
        elif ch == 'D': admin_teleconsult_queue(consult_svc, db)
        elif ch == 'RX': admin_prescriptions(db)
        elif ch == 'I': admin_intelligence_dashboard(
                            db, nyansa, promo_engine, restock_eng,
                            support_svc, consult_svc)
        elif ch == 'CA': admin_consumption_anomalies(db)
        elif ch == 'N':
            print("\n  🧠 Running Nyansa full analysis…")
            results = nyansa.run_full_analysis()
            promo_engine.expire_old_promotions()
            print(f"  ✅ {len(results)} new insight(s) generated.")
            if nyansa_engine:
                nyansa_engine.log_signal("MANUAL_ANALYSIS",
                                         f"insights={len(results)}")
            input("\n  Press Enter…")
        elif ch == 'O': admin_restock_orders(restock_eng, db)
        elif ch == 'M': admin_promotions(promo_engine, db)
        # ── B28: OS Status & Self-Test ────────────────────────────────────
        elif ch == 'Y': admin_os_status_panel(db, os_layer, nyansa_engine)
        elif ch == 'U': admin_ota_menu(ota_svc)
        elif ch == 'B': admin_broadcast(notif_svc)
        elif ch == 'J': admin_scheduler_status(scheduler)
        elif ch == 'L': admin_dispatch_log(db)
        elif ch == 'A': admin_api_server(api_server)
        elif ch == 'W': admin_api_tokens(db)
        elif ch == 'H': admin_hardware_menu(db)
        elif ch == 'E': admin_cupscan_platform(db)
        elif ch == 'V': admin_service_bus_status(db)          # upgraded B28
        elif ch == 'C': admin_cupsule_management(db)
        elif ch == 'K': admin_nia_log(db)
        elif ch == 'Z': admin_light_loop_test(db)
        elif ch == '4': admin_check_transactions(db)
        elif ch == '5': admin_get_analytics(db)
        elif ch == '6': hardware_analytics_report(db)
        elif ch == 'G': admin_audit_dashboard(db)
        elif ch == 'S': admin_reset_sanitization(db)
        elif ch == 'R': admin_revenue_report(db)
        elif ch == 'P': admin_pdms_audit_log(db)
        elif ch == '£':
            admin_maintenance_projections(db)
        elif ch == 'F':                                        # B28 CSV export
            if export_svc:
                admin_csv_export_menu(db, export_svc)
            else:
                print("  Export service not available.")
                input("\n  Press Enter…")
        elif ch == 'Q': admin_reports_menu(db)
        elif ch == '7':
            if clear_all_data(db):
                db._seed_inventory()
                db._seed_hardware()
        elif ch == '0':
            scheduler.stop()
            if api_server._running:
                api_server.stop()
            hw.cleanup()
            db.log_audit("ADMIN", "LOGIN_OK", detail="Admin logout")
            print("  Admin logged out.")
            break
        else:
            print("  Invalid option.")
            input("\n  Press Enter…")


# ─────────────────────────────────────────────────────────────────────────────
# BUILD 20+21: ADMIN HANDLER FUNCTIONS
# ─────────────────────────────────────────────────────────────────────────────

def admin_os_status_panel(db: DatabaseManager,
                          os_layer: 'AidPlusOS' = None,
                          nyansa_engine: 'NyansaEngine' = None) -> None:
    """
    B28: [Y] Live Aid Plus OS Status & Self-Test panel.
    Shows connectivity, power, OTA status, Nyansa engine, Service Bus.
    Runs a fresh self-test and displays results.
    """
    print_header(
        f"AID PLUS OS — Status Panel  |  Build {SCHEMA_VERSION}  |  ADW-AS")

    # OS layer snapshot
    if os_layer:
        detail = os_layer.status_detail()
        print(f"\n  Variant      : {detail['variant']}")
        print(f"  Platform     : {detail['adw']}")
        print(f"  Build        : {detail['build']}")
        print(f"  Boot OK      : {'✅' if detail['boot_ok'] else '⚠️'}")
        print(f"\n  Connectivity : {detail['connectivity']}")
        if detail['queue_depth'] > 0:
            print(f"  Offline Queue: {detail['queue_depth']} pending items")
        print(f"  Power Source : {detail['power_source']}")
        print(f"  Power State  : {detail['power_state']}")
        print(f"  Battery      : {detail['battery_pct']:.0f}%")
    else:
        print("\n  OS layer not available — system started without AidPlusOS.")

    # Nyansa engine status
    print(f"\n  Nyansa       : {nyansa_engine.engine_status() if nyansa_engine else 'Not initialised'}")

    # OTA last check from DB
    with db._conn() as con:
        ota_row = con.execute(
            "SELECT checked_at, available_version, action FROM ota_log "
            "ORDER BY checked_at DESC LIMIT 1").fetchone()
    if ota_row:
        print(f"\n  Last OTA     : {ota_row[0][:19]}  "
              f"build={ota_row[1]}  action={ota_row[2]}")
    else:
        print("\n  Last OTA     : No check recorded yet")

    # Last boot log
    with db._conn() as con:
        boot_row = con.execute(
            "SELECT booted_at, conn_state, power_source, boot_result "
            "FROM aidplus_os_boot_log ORDER BY booted_at DESC LIMIT 1"
        ).fetchone()
    if boot_row:
        print(f"  Last Boot    : {boot_row[0][:19]}  "
              f"conn={boot_row[1]}  pwr={boot_row[2]}  result={boot_row[3]}")

    # Run self-test
    print(f"\n  Running self-test…")
    if os_layer and nyansa_engine:
        tester = SystemSelfTest(db)
        tester.run(os_layer, nyansa_engine)
        overall = "✅ ALL PASS" if SystemSelfTest.FAIL not in tester.results.values() \
                  else "⚠️  WARNINGS/FAILURES — review above"
        print(f"\n  Self-Test Result: {overall}")
    else:
        print("  Self-test requires os_layer and nyansa_engine.")

    input("\n  Press Enter…")


def admin_service_bus_status(db: DatabaseManager) -> None:
    """
    B28: [V] Full Service Bus status — live registrations + dormant sockets.
    Shows which products are live, which are dormant (hardware not deployed),
    and what integration contracts are locked for future products.
    """
    print_header(
        f"SERVICE BUS STATUS — Build {SCHEMA_VERSION}  |  Integration Contracts v1.0")

    full = AidPlusServiceBus.full_status()
    db_reg = db.get_registered_services()

    print(f"\n  {'SERVICE':<14} {'STATUS':<10} {'VERSION/CONTRACT':<20} NOTE")
    print("  " + "─" * 64)
    for name, info in sorted(full.items()):
        status  = info.get("status", "unknown")
        icon    = "✅" if status == "live" else "○ "
        version = info.get("version", info.get("contract", "—"))
        note    = info.get("note", "")
        reg_at  = info.get("registered_at", "")
        detail  = reg_at[:16] if reg_at else note[:36]
        print(f"  {icon} {name:<12}  {status:<10} {version:<20} {detail}")

    print(f"\n  DB Registry ({len(db_reg)} entries):")
    for r in db_reg:
        hb  = r.get("last_heartbeat", "—")[:19] if r.get("last_heartbeat") else "—"
        print(f"    {r['service_name']:<14} v{r['version']:<8} "
              f"status={r['status']:<10} hb={hb}")

    # Show offline queue if any
    with db._conn() as con:
        q_depth = con.execute(
            "SELECT COUNT(*) FROM offline_queue WHERE status='pending'"
        ).fetchone()[0]
    if q_depth > 0:
        print(f"\n  ⚠️  Offline queue: {q_depth} pending item(s) awaiting delivery")

    try:
        from aidplus.config import SERVICE_BUS_JWT_EXPIRY_HRS as _ttl
    except ImportError:
        _ttl = 1  # default 1 hour
    print(f"\n  Service Bus JWT TTL : {_ttl}h")
    print(f"  Dormant sockets mean: contract locked, hardware not yet deployed.")
    print(f"  When BTM/Aid Air deploy, they call AidPlusOS.boot() then register.")
    input("\n  Press Enter…")




def admin_cupsule_management(db: DatabaseManager):
    """[B21-H] View Cupsule issuance/return stats and manage returns."""
    svc = CupsuleService(db)
    while True:
        stats = svc.get_return_stats()
        print_header("CUPSULE MANAGEMENT — Build 20+")
        print(f"  Total issued:   {stats['total_issued']}")
        print(f"  Total returned: {stats['total_returned']}")
        print(f"  Return rate:    {stats['return_rate_pct']}%")
        print(f"\n[1] View unreturned Cupsules")
        print(f"[2] Process manual return (test/admin)")
        print(f"[3] View returns by customer")
        print(f"[0] Back")
        ch = input("Option > ").strip()

        if ch == '0':
            break
        elif ch == '1':
            with db._conn() as con:
                rows = con.execute(
                    "SELECT cupsule_id, customer_id, drug_name, issued_at "
                    "FROM cupsule_issued WHERE returned=0 "
                    "ORDER BY issued_at DESC LIMIT 30").fetchall()
            if not rows:
                print("No outstanding Cupsules.")
            else:
                print(f"\n{'CUPSULE ID':<28} {'CUSTOMER':<14} {'DRUG':<22} ISSUED")
                print("-" * 80)
                for r in rows:
                    print(f"  {r['cupsule_id']:<28} {r['customer_id']:<14} "
                          f"{r['drug_name']:<22} {r['issued_at'][:16]}")
            input("\nPress Enter…")

        elif ch == '2':
            cid_in    = input("Cupsule ID: ").strip()
            cust_id   = input("Customer ID: ").strip()
            condition = input("Condition (intact/empty_only/damaged): ").strip() or "intact"
            result    = svc.process_return(cid_in, cust_id, condition)
            if result.get("pass", result.get("success", False)):
                print(f"✅ Return processed — {result['points_awarded']} points awarded.")
            else:
                print(f"❌ {result.get('message', result.get('error_code'))}")
            input("\nPress Enter…")

        elif ch == '3':
            cust_id = input("Customer ID: ").strip()
            cups    = db.get_customer_cupsules(cust_id)
            if not cups:
                print("No Cupsules found for that customer.")
            else:
                print(f"\n{'CUPSULE ID':<28} {'DRUG':<22} {'STATUS':<10} ISSUED")
                print("-" * 75)
                for c in cups:
                    status = "Returned" if c["returned"] else "Outstanding"
                    print(f"  {c['cupsule_id']:<28} {c['drug_name']:<22} "
                          f"{status:<10} {c['issued_at'][:16]}")
            input("\nPress Enter…")

        else:
            print("Invalid.")


def admin_nia_log(db: DatabaseManager):
    """[B21-H] View NIA Ghana Card verification audit log."""
    print_header("NIA VERIFICATION LOG — Build 20+")
    with db._conn() as con:
        rows = con.execute(
            "SELECT actor_id, action, record_id, detail, timestamp "
            "FROM pdms_audit_log WHERE action='NIA_VERIFY' "
            "ORDER BY timestamp DESC LIMIT 50").fetchall()
    if not rows:
        print("No NIA verification events recorded.")
    else:
        print(f"{'CUSTOMER':<14} {'RESULT':<40} TIMESTAMP")
        print("-" * 75)
        for r in rows:
            detail = r["detail"][:38] if r["detail"] else "—"
            print(f"  {r['actor_id']:<14} {detail:<40} {r['timestamp'][:16]}")
    print(f"\nTotal NIA events: {len(rows)}")
    input("\nPress Enter…")


def admin_light_loop_test(db: DatabaseManager):
    """[B21-H] Test the Light Loop verification tunnel."""
    print_header("LIGHT LOOP TEST — Build 21")
    print("The Light Loop verifies a packaged order before loading into a drone.")
    print("This test runs the full verification sequence.")
    hw = HardwareInterface(db)
    mode = "SIMULATION" if hw.sim_mode else "LIVE GPIO"
    print(f"\nHardware mode: {mode}")
    order_id = input("Order / Cupsule ID to test (or press Enter for test ID): ").strip()
    if not order_id:
        order_id = f"TEST-{secure_id(8)}"
    print(f"\nRunning Light Loop for order: {order_id} …")
    result = hw.activate_light_loop(order_id)
    if result.get("pass", result.get("success", False)):
        print("✅ LIGHT LOOP PASS — all checks passed.")
        print("   Drone payload bay door opened.")
        print("   LIGHT_LOOP_PASS_CONFIRMED emitted on Service Bus.")
    else:
        print("❌ LIGHT LOOP FAIL")
        if "failed" in result:
            print(f"   Failed checks: {', '.join(result['failed'])}")
        if "error" in result:
            print(f"   Error: {result['error']}")
        print("   LIGHT_LOOP_PASS_FAILED emitted on Service Bus.")
    checks = result.get("checks", {})
    if checks:
        print("\n   Check details:")
        for k, v in checks.items():
            icon = "✅" if v else "❌"
            print(f"     {icon} {k}")
    input("\nPress Enter…")


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════════
# BUILD 23 — CUPSCAN PLATFORM ADMIN DASHBOARD
# ═══════════════════════════════════════════════════════════════════════════════

def admin_cupscan_platform(db: DatabaseManager):
    """[E] CUPSCAN Platform Dashboard — live kiosk overview for administrators."""
    module = CUPSCANModule(db)
    while True:
        os.system('cls' if os.name == 'nt' else 'clear')
        print_header("CUPSCAN PLATFORM DASHBOARD — Adwene ADW-1 | Build 24")

        stats   = db.cupscan_platform_stats()
        kiosks  = db.cupscan_get_all_kiosks()
        hw      = module.hardware_health()
        bin_st  = module.bin_status()

        print(f"\n  ┌{'─'*60}┐")
        print(f"  │  {'CUPSCAN Module — Hardware Status':<58}  │")
        print(f"  ├{'─'*60}┤")
        print(f"  │  Platform     : Adwene {ADWENE_DESIGNATION} / {ADWENE_VERSION:<36}│")
        print(f"  │  Mode         : {'SIMULATION' if HW_SIMULATION_MODE else 'LIVE GPIO':<38}│")
        print(f"  │  Drop door    : {hw.get('drop_door','?'):<38}│")
        print(f"  │  UV circuit   : {hw.get('uv_circuit','?'):<38}│")
        print(f"  │  Weight cell  : {hw.get('weight_cell','?'):<38}│")
        print(f"  │  Camera       : {hw.get('camera','?'):<38}│")
        print(f"  │  Bin level    : {'⚠️ FULL' if bin_st['full'] else ('⚠️ HALF' if bin_st['half'] else '✅ OK'):<38}│")
        print(f"  ├{'─'*60}┤")
        print(f"  │  {'Platform Statistics':<58}  │")
        print(f"  ├{'─'*60}┤")
        print(f"  │  Active remote kiosks : {stats['active_kiosks']:<34}│")
        print(f"  │  Returns today        : {stats['returns_today']:<34}│")
        print(f"  │  Points issued today  : {stats['pts_today']:<34}│")
        print(f"  │  GHS value today      : GHS {stats['ghs_value_today']:.2f}{'':<29}│")
        print(f"  │  CO₂ saved (total)    : {stats['co2_saved_kg']} kg{'':<31}│")
        print(f"  │  Total returns ever   : {stats['total_returns']:<34}│")
        print(f"  └{'─'*60}┘")

        if kiosks:
            print(f"\n  {'KIOSK ID':<22} {'STATUS':<12} {'TODAY':<8} {'BIN INTACT':<12} {'LAST HEARTBEAT'}")
            print(f"  {'─'*76}")
            for k in kiosks:
                hb  = (k.get("last_heartbeat") or "never")[:16]
                st  = k.get("status", "?")
                dot = "●" if st == "ACTIVE" else "○"
                print(f"  {dot} {k['kiosk_id']:<21} {st:<12} "
                      f"{k.get('returns_today',0):<8} "
                      f"{k.get('bin_intact_pct',0):.0f}%{'':10} {hb}")

        print(f"\n  [1] Recent returns (last 20)   [2] Top returning customers")
        print(f"  [3] Daily breakdown             [H] CUPSCAN hardware self-test")
        print(f"  [0] Back")
        ch = input("\n  Option > ").strip().upper()

        if ch == '1':
            _cupscan_recent_returns(db)
        elif ch == '2':
            _cupscan_top_customers(db)
        elif ch == '3':
            _cupscan_daily_breakdown(db)
        elif ch == 'H':
            print("\n  Running CUPSCAN hardware self-test…")
            result = module.hardware_health()
            for k, v in result.items():
                print(f"    {k:<20}: {v}")
            input("\n  Press Enter…")
        elif ch == '0':
            break


def _cupscan_recent_returns(db: DatabaseManager):
    with db._conn() as con:
        rows = con.execute("""
            SELECT r.returned_at, r.customer_id, r.compartment,
                   r.total_pts, r.co2_saved_g, r.kiosk_id
            FROM cupscan_returns r ORDER BY r.returned_at DESC LIMIT 20
        """).fetchall()
    print_header("CUPSCAN — RECENT RETURNS (last 20)")
    if not rows:
        print("  No returns recorded yet.")
    else:
        print(f"  {'TIME':<20} {'CUSTOMER':<16} {'COMPARTMENT':<14} {'PTS':<6} {'CO₂g':<8} KIOSK")
        print(f"  {'─'*76}")
        for r in rows:
            print(f"  {(r[0] or '')[:16]:<20} {str(r[1] or 'anon'):<16} {r[2]:<14} {r[3]:<6} {r[4]:<8.1f} {r[5]}")
    input("\n  Press Enter…")


def _cupscan_top_customers(db: DatabaseManager):
    with db._conn() as con:
        rows = con.execute("""
            SELECT customer_id, COUNT(*) AS returns,
                   SUM(total_pts) AS pts, SUM(co2_saved_g) AS co2
            FROM cupscan_returns WHERE customer_id IS NOT NULL
            GROUP BY customer_id ORDER BY returns DESC LIMIT 10
        """).fetchall()
    print_header("CUPSCAN — TOP RETURNING CUSTOMERS")
    if not rows:
        print("  No data yet.")
    else:
        print(f"  {'CUSTOMER ID':<22} {'RETURNS':<10} {'PTS EARNED':<14} CO₂ SAVED (g)")
        print(f"  {'─'*58}")
        for r in rows:
            print(f"  {r[0]:<22} {r[1]:<10} {r[2]:<14} {r[3]:.1f}")
    input("\n  Press Enter…")


def _cupscan_daily_breakdown(db: DatabaseManager):
    with db._conn() as con:
        rows = con.execute("""
            SELECT strftime('%Y-%m-%d', returned_at) AS day,
                   COUNT(*) AS returns, SUM(total_pts) AS pts
            FROM cupscan_returns
            GROUP BY day ORDER BY day DESC LIMIT 14
        """).fetchall()
    print_header("CUPSCAN — LAST 14 DAYS")
    if not rows:
        print("  No data yet.")
    else:
        print(f"  {'DATE':<14} {'RETURNS':<10} PTS ISSUED")
        print(f"  {'─'*36}")
        for r in rows:
            bar = "█" * min(int(r[1]), 30)
            print(f"  {r[0]:<14} {r[1]:<10} {r[2]}  {bar}")
    input("\n  Press Enter…")


# ═══════════════════════════════════════════════════════════════════════════════
# BUILD 23 — CO-LOCATED CUPSULE RETURN (customer terminal mode)
# ═══════════════════════════════════════════════════════════════════════════════

def admin_consumption_anomalies(db: "DatabaseManager") -> None:
    """[CA] Show flagged consumption anomalies from PDMS audit log."""
    from aidplus.ui import print_header
    while True:
        print_header("CONSUMPTION ANOMALY REVIEW")
        with db._conn() as con:
            rows = con.execute(
                "SELECT actor_id, detail, timestamp FROM pdms_audit_log "
                "WHERE action='CONSUMPTION_ANOMALY_FLAGGED' "
                "ORDER BY timestamp DESC LIMIT 50"
            ).fetchall()
        if not rows:
            print("  ✅ No consumption anomalies flagged.")
            input("  Press Enter…"); return

        print(f"  {'#':<4} {'Customer':<14} {'Date':<12}  Detail")
        print("  " + "─" * 72)
        for i, row in enumerate(rows, 1):
            detail = (row["detail"] or "")[:50]
            dt     = str(row["timestamp"] or "")[:10]
            print(f"  [{i:<2}] {row['actor_id']:<14} {dt:<12}  {detail}")
        print("  " + "─" * 72)
        print(f"  {len(rows)} anomaly/anomalies on record")
        print("\n  [0] Back")
        if input("  › ").strip() == '0':
            break

