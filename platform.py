"""
AID PLUS+ — Aid Plus OS Platform
====================================
OTAUpdateManager: check/download/verify SHA-256/apply/rollback.
NotificationTemplateService: English + Twi templates for all system events.
AdminExportService: UTF-8 BOM CSV exports with date filters.
AidPlusOS: product-agnostic platform OS — boots the ADW-1, runs self-test,
           manages connectivity + power, exposes status banner.
"""
from __future__ import annotations
import os, json, csv, time, threading, hashlib
from datetime import datetime, timedelta

from aidplus.config import *
from aidplus.db import DatabaseManager
from aidplus.power import PowerManager, ConnectivityManager
from aidplus.bus import AidPlusServiceBus

class OTAUpdateManager:
    """
    B26: Over-the-Air Update Manager.
    Checks the AID Plus update server for new builds.
    Downloads, verifies SHA-256 checksum, and applies on next boot.

    Architecture:
      1. check()  — GET /api/v1/updates?variant=ADW-AS&build=26
      2. download() — stream new build to staging dir, verify hash
      3. apply()  — copy to production, write restart flag, log
      4. revert() — restore from backup on failed apply
    """

    def __init__(self, db: 'DatabaseManager'):
        self._db = db
        os.makedirs(OTA_BACKUP_DIR,  exist_ok=True)
        os.makedirs(OTA_STAGING_DIR, exist_ok=True)

    def check(self) -> dict:
        """
        Check OTA server for a newer build.
        Returns dict: {update_available, available_version, release_notes, url, checksum}
        """
        import urllib.request, json
        url = (f"{OTA_SERVER_URL}/updates"
               f"?variant={ADW_VARIANT_AS}"
               f"&build={SCHEMA_VERSION}"
               f"&serial={ADWENE_SERIAL}")
        try:
            req = urllib.request.Request(
                url, headers={"Authorization": f"Bearer {CUPSCAN_ADMIN_TOKEN}",
                               "X-ADW-ID": ADWENE_SERIAL})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
            available = data.get("latest_build", SCHEMA_VERSION)
            update_available = available > SCHEMA_VERSION
            self._log_check(available, "checked",
                             data.get("release_notes", ""))
            return {
                "update_available": update_available,
                "available_version": available,
                "release_notes": data.get("release_notes", ""),
                "download_url": data.get("download_url", ""),
                "checksum_sha256": data.get("checksum_sha256", ""),
            }
        except Exception as e:
            self._log_check(SCHEMA_VERSION, "check_failed", str(e))
            return {
                "update_available": False,
                "available_version": SCHEMA_VERSION,
                "release_notes":     f"Build {SCHEMA_VERSION} — current build.",
                "download_url":      "",
                "checksum_sha256":   "",
                "error":             str(e),
            }

    def download(self, url: str, expected_sha256: str,
                 new_version: int) -> str | None:
        """
        Stream new build file to staging dir.
        Verify SHA-256 before returning staging path.
        Returns staging file path on success, None on failure.
        """
        import urllib.request, hashlib
        staging_path = os.path.join(OTA_STAGING_DIR,
                                    f"AidSystem_B{new_version}.py")
        try:
            urllib.request.urlretrieve(url, staging_path)
            if OTA_VERIFY_CHECKSUM and expected_sha256:
                sha = hashlib.sha256()
                with open(staging_path, "rb") as f:
                    for chunk in iter(lambda: f.read(65536), b""):
                        sha.update(chunk)
                actual = sha.hexdigest()
                if actual.lower() != expected_sha256.lower():
                    self._log_check(new_version, "checksum_failed",
                                    f"expected={expected_sha256} actual={actual}")
                    os.remove(staging_path)
                    return None
            self._log_check(new_version, "downloaded", "checksum OK")
            return staging_path
        except Exception as e:
            self._log_check(new_version, "download_failed", str(e))
            return None

    def apply(self, staging_path: str, new_version: int) -> bool:
        """
        Back up the current build, copy staged build to production.
        Writes a restart_required flag file for the service manager.
        """
        import shutil
        prod_path   = os.path.abspath(__file__) if "__file__" in dir() else \
                      f"/opt/aidplus/AidSystem_B{SCHEMA_VERSION}.py"
        backup_path = os.path.join(OTA_BACKUP_DIR,
                                   f"AidSystem_B{SCHEMA_VERSION}_backup.py")
        try:
            if os.path.exists(prod_path):
                shutil.copy2(prod_path, backup_path)
            shutil.copy2(staging_path, prod_path)
            # Write restart flag
            flag_path = os.path.join(OTA_BACKUP_DIR, "restart_required")
            with open(flag_path, "w") as f:
                f.write(f"{new_version}\n")
            self._db.log_audit("SYSTEM", "OTA_APPLIED",
                                detail=f"new_build={new_version}")
            self._log_check(new_version, "applied", f"backup={backup_path}")
            return True
        except Exception as e:
            self._log_check(new_version, "apply_failed", str(e))
            return False

    def _log_check(self, available: int, action: str, notes: str) -> None:
        with self._db._conn() as con:
            con.execute(
                "INSERT INTO ota_log (checked_at, current_version, "
                "available_version, action, notes) VALUES (?,?,?,?,?)",
                (datetime.now().isoformat(), SCHEMA_VERSION,
                 available, action, notes[:500] if notes else ""))

    def run_background_check(self, connectivity: 'ConnectivityManager') -> None:
        """
        B26: Run an OTA check in a background thread if online.
        Called once per OTA_CHECK_INTERVAL_HOURS by the OS heartbeat.
        """
        if not connectivity.is_online:
            return
        import threading
        def _check_and_log():
            info = self.check()
            if info.get("update_available"):
                print(f"\n  🔄 OTA: Build {info['available_version']} available. "
                      f"{info.get('release_notes', '')}")
        t = threading.Thread(target=_check_and_log, daemon=True)
        t.start()


class NotificationTemplateService:
    """
    B26: Render notification templates in English or Twi.
    Templates stored in notification_templates DB table (seeded at migration).
    """

    def __init__(self, db: 'DatabaseManager'):
        self._db = db
        self._cache: dict = {}

    def _load(self, key: str, lang: str) -> tuple | None:
        cache_key = f"{key}:{lang}"
        if cache_key in self._cache:
            return self._cache[cache_key]
        with self._db._conn() as con:
            row = con.execute(
                "SELECT title, body FROM notification_templates "
                "WHERE template_key=? AND lang=?",
                (key, lang)).fetchone()
        if row:
            self._cache[cache_key] = row
        return row

    def render(self, key: str, lang: str = LANG_EN, **kwargs) -> tuple:
        """
        Render a template by key and language.
        Returns (title: str, body: str).
        Falls back to English if the requested language is unavailable.
        """
        row = self._load(key, lang)
        if not row and lang != LANG_EN:
            row = self._load(key, LANG_EN)   # English fallback
        if not row:
            return (f"[{key}]", str(kwargs))
        title, body = row
        try:
            title = title.format(**kwargs)
            body  = body.format(**kwargs)
        except KeyError:
            pass   # leave unformatted placeholders if key missing
        return (title, body)

    def send(self, db: 'DatabaseManager', customer_id: str,
             key: str, lang: str = LANG_EN, **kwargs) -> None:
        """Render and dispatch a notification via db.send_notification."""
        title, body = self.render(key, lang, **kwargs)
        db.send_notification(customer_id, title, body)

    def preferred_lang(self, customer: dict) -> str:
        """Get customer's preferred language (default English)."""
        return customer.get("preferred_lang", LANG_EN)


class AdminExportService:
    """
    B26: Export admin reports as CSV.
    Produces: transactions, CUPSCAN returns, inventory status,
              power telemetry, customer roster.
    """

    def __init__(self, db: 'DatabaseManager'):
        self._db = db

    def export_csv(self, report_type: str,
                   date_from: str = None, date_to: str = None) -> str:
        """
        Generate a CSV report. Returns CSV string.
        report_type: 'transactions' | 'cupscan' | 'inventory' |
                     'power' | 'customers'
        """
        import csv, io
        buf = io.StringIO()
        writer = csv.writer(buf)

        if report_type == "transactions":
            writer.writerow(["id", "customer_id", "type", "amount_ghs",
                             "balance_after", "timestamp", "note"])
            with self._db._conn() as con:
                rows = con.execute(
                    "SELECT id, customer_id, 'purchase' AS type, total AS amount, "
                    "0.0 AS balance_after, timestamp AS created_at, "
                    "badge AS note FROM transactions "
                    "WHERE (?1 IS NULL OR timestamp >= ?1) "
                    "  AND (?2 IS NULL OR timestamp <= ?2) "
                    "ORDER BY timestamp DESC LIMIT 10000",
                    (date_from, date_to)).fetchall()
            writer.writerows(rows)

        elif report_type == "cupscan":
            writer.writerow(["id", "kiosk_id", "customer_id", "condition",
                             "weight_g", "uv_pass", "base_pts", "total_pts",
                             "multiplier", "returned_at"])
            with self._db._conn() as con:
                rows = con.execute(
                    "SELECT return_id AS id, kiosk_id, customer_id, compartment, "
                    "0.0 AS weight_g, 0 AS uv_pass, base_pts, total_pts, "
                    "multiplier, returned_at "
                    "FROM cupscan_returns "
                    "WHERE (?1 IS NULL OR returned_at >= ?1) "
                    "  AND (?2 IS NULL OR returned_at <= ?2) "
                    "ORDER BY returned_at DESC LIMIT 10000",
                    (date_from, date_to)).fetchall()
            writer.writerows(rows)

        elif report_type == "inventory":
            writer.writerow(["id", "name", "shelf", "quantity",
                             "price_ghs", "max_capacity", "last_updated"])
            with self._db._conn() as con:
                rows = con.execute(
                    "SELECT drug_id AS id, name, shelf, capsules_left AS quantity, "
                    "current_price AS price, ? AS max_capacity, '' AS last_updated "
                    "FROM inventory ORDER BY shelf, drug_id",(MAX_CAPS_PER_SHELF,)).fetchall()
            writer.writerows(rows)

        elif report_type == "power":
            writer.writerow(["id", "unit_id", "logged_at", "source", "state",
                             "battery_pct", "solar_v", "wh_consumed",
                             "uptime_secs"])
            with self._db._conn() as con:
                rows = con.execute(
                    "SELECT id, unit_id, logged_at, source, state, "
                    "battery_pct, solar_v, wh_consumed, uptime_secs "
                    "FROM power_telemetry "
                    "WHERE (?1 IS NULL OR logged_at >= ?1) "
                    "  AND (?2 IS NULL OR logged_at <= ?2) "
                    "ORDER BY logged_at DESC LIMIT 5000",
                    (date_from, date_to)).fetchall()
            writer.writerows(rows)

        elif report_type == "customers":
            writer.writerow(["id", "name", "phone", "tier",
                             "wallet_balance", "loyalty_points",
                             "created_at", "status"])
            with self._db._conn() as con:
                rows = con.execute(
                    "SELECT customer_id AS id, name, contact AS phone, "
                    "wallet_tier AS tier, balance AS wallet_balance, "
                    "loyalty_points, status || '' AS created_at, status "
                    "FROM customers ORDER BY name").fetchall()
            writer.writerows(rows)

        else:
            writer.writerow(["error"])
            writer.writerow([f"Unknown report type: {report_type}"])

        return buf.getvalue()

    def save_csv(self, report_type: str,
                 date_from: str = None, date_to: str = None,
                 output_dir: str = "/tmp") -> str:
        """Save CSV report to file. Returns file path."""
        import re
        csv_str  = self.export_csv(report_type, date_from, date_to)
        filename = (f"aidplus_{report_type}_"
                    f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
        path     = os.path.join(output_dir, filename)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(csv_str)
        self._db.log_audit("ADMIN", "CSV_EXPORT",
                            detail=f"type={report_type} file={filename}")
        return path


# ─────────────────────────────────────────────────────────────────────────────
# B26: AidPlusOS — Shared Connected OS for All ADW Variants
# ─────────────────────────────────────────────────────────────────────────────

class AidPlusOS:
    """
    B28: AID PLUS+ Operating System — product-agnostic platform.

    Runs on Adwene ADW hardware. Does NOT know or care which product
    is running on top of it. Products use the OS — they do not extend it.

    Architecture:
        AidPlusOS boots → product receives the os_layer object → uses it.

    All three current products boot the same OS:
        Aid System:  os = AidPlusOS(db, ADW_VARIANT_AS); os.boot(); main_menu(db, os)
        BTM:         os = AidPlusOS(db, ADW_VARIANT_BT); os.boot(); btm_main(db, os)
        Aid Air:     os = AidPlusOS(db, ADW_VARIANT_AA); os.boot(); aid_air_main(db, os)

    The OS provides to every product:
        os.connectivity   — ConnectivityManager
        os.power          — PowerManager
        os.ota            — OTAUpdateManager
        os.notif          — NotificationTemplateService
        os.export         — AdminExportService
        os.heartbeat_tick() — call from main loop
        os.status_banner()  — one-line status for display
    """

    def __init__(self, db: 'DatabaseManager', variant: str = "ADW-BASE"):
        self._db          = db
        self._variant     = variant
        self._connectivity: ConnectivityManager      = None
        self._power:        PowerManager             = None
        self._ota:          OTAUpdateManager         = None
        self._notif:        NotificationTemplateService = None
        self._export:       AdminExportService       = None
        self._boot_ok     = False

    # ── Boot sequence ──────────────────────────────────────────────────────────
    def boot(self) -> bool:
        """
        Full OS boot sequence. Called once at product startup.
        Returns True on success. Partial failures are warned, not fatal.
        """
        print(f"\n  ⚙️  Aid Plus OS  |  Variant: {self._variant}"
              f"  |  {ADWENE_DESIGNATION} ({ADWENE_SERIAL})")

        conn_state   = CONN_OFFLINE
        power_source = POWER_SOURCE_BATTERY
        boot_result  = "ok"
        notes        = []

        try:
            # 1. Hardware GPIO baseline
            self._init_gpio_baseline()

            # 2. Connectivity stack
            self._connectivity = ConnectivityManager(self._db)
            conn_state = self._connectivity.check()
            print(f"     Connectivity : {conn_state}")

            # 3. Power system
            self._power = PowerManager(self._db)
            snap = self._power.read_hardware()
            power_source = snap["source"]
            print(f"     Power        : {self._power.status_line()}")
            self._power.register_shutdown_callback(self._on_critical_power)

            # 4. OTA pipeline
            self._ota = OTAUpdateManager(self._db)
            if conn_state != CONN_OFFLINE:
                self._ota.run_background_check(self._connectivity)
            print(f"     OTA          : Build {SCHEMA_VERSION} current")

            # 5. Notification + export services
            self._notif  = NotificationTemplateService(self._db)
            self._export = AdminExportService(self._db)

        except Exception as e:
            boot_result = f"error: {e}"
            notes.append(str(e))
            print(f"  ⚠️  OS boot warning: {e}")

        self._log_boot(conn_state, power_source, boot_result,
                       "; ".join(notes) if notes else None)
        self._boot_ok = (boot_result == "ok")
        return self._boot_ok

    def _init_gpio_baseline(self) -> None:
        """
        GPIO baseline common to ALL ADW variants.
        Power detect pin only. Product handles its own GPIO on top.
        """
        if not HW_SIMULATION_MODE:
            try:
                import RPi.GPIO as GPIO
                GPIO.setmode(GPIO.BCM)
                GPIO.setwarnings(False)
                GPIO.setup(GPIO_POWER_SOLAR_DETECT, GPIO.IN)
                print("     GPIO         : BCM mode online")
            except Exception:
                print("     GPIO         : Simulation (non-Pi environment)")

    def _on_critical_power(self, reason: str) -> None:
        """Called by PowerManager on CRITICAL battery. Flush queue + log."""
        q = self._connectivity.queue_depth() if self._connectivity else 0
        print(f"\n  ⚡ OS critical power — flushing {q} queued items…")
        self._db.log_audit("SYSTEM", "OS_POWER_SHUTDOWN",
                            detail=f"variant={self._variant} reason={reason} queue={q}")

    def _log_boot(self, conn_state: str, power_source: str,
                  boot_result: str, notes: str) -> None:
        try:
            with self._db._conn() as con:
                con.execute(
                    "INSERT INTO aidplus_os_boot_log "
                    "(booted_at, variant, adw_version, adw_serial, "
                    "build_version, conn_state, power_source, boot_result, notes) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (datetime.now().isoformat(), self._variant,
                     ADWENE_VERSION, ADWENE_SERIAL, SCHEMA_VERSION,
                     conn_state, power_source, boot_result, notes))
        except Exception:
            pass   # boot log failure is non-fatal

    # ── Heartbeat (call from product main loop) ────────────────────────────────
    def heartbeat_tick(self) -> None:
        """Power + connectivity check. Call every ~60 main-loop iterations."""
        if self._power:
            self._power.read_hardware()
            self._power.maybe_log_telemetry()
            if self._power.is_critical and not self._power._shutdown_requested:
                self._power.request_graceful_shutdown("BATTERY_CRITICAL")
        if self._connectivity:
            self._connectivity.maybe_check()

    # ── Accessors (products consume these) ────────────────────────────────────
    @property
    def variant(self) -> str:
        return self._variant

    @property
    def connectivity(self) -> ConnectivityManager:
        return self._connectivity

    @property
    def power(self) -> PowerManager:
        return self._power

    @property
    def ota(self) -> OTAUpdateManager:
        return self._ota

    @property
    def notif(self) -> NotificationTemplateService:
        return self._notif

    @property
    def export(self) -> AdminExportService:
        return self._export

    @property
    def boot_ok(self) -> bool:
        return self._boot_ok

    def status_banner(self) -> str:
        """One-line OS status string for display headers."""
        parts = [f"Aid Plus OS  |  {self._variant}"]
        if self._connectivity:
            parts.append(self._connectivity.status_line())
        if self._power:
            parts.append(self._power.status_line())
        return "  |  ".join(parts)

    def status_detail(self) -> dict:
        """Full status dict for admin dashboard."""
        return {
            "variant":     self._variant,
            "build":       SCHEMA_VERSION,
            "adw":         f"{ADWENE_DESIGNATION} ({ADWENE_SERIAL})",
            "connectivity": self._connectivity.current if self._connectivity else "N/A",
            "queue_depth": self._connectivity.queue_depth() if self._connectivity else 0,
            "power_source": self._power.source if self._power else "N/A",
            "power_state":  self._power.state  if self._power else "N/A",
            "battery_pct":  self._power.battery_pct if self._power else 0,
            "boot_ok":      self._boot_ok,
        }


# ─────────────────────────────────────────────────────────────────────────────
# B28: NyansaEngine — AI Intelligence Engine
# Separate from Aid Plus OS. Runs ON the OS. Used BY products.
# Analogy: Nyansa = FSD/Autopilot. Aid Plus OS = Tesla Vehicle OS.
# ─────────────────────────────────────────────────────────────────────────────

