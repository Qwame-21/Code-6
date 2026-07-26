"""
AID PLUS+ — Power & Connectivity Management
=============================================
PowerManager: solar/battery/mains state machine, INA228 monitoring.
ConnectivityManager: WiFi → eSIM-MTN → eSIM-Vodafone → Offline failover.
Both run background threads for continuous monitoring.
"""
from __future__ import annotations
import time, threading, random, json
from datetime import datetime

from aidplus.config import *
from aidplus.db import DatabaseManager

class PowerManager:
    """
    B29: ADW-1 Power Management — Battery-Primary Architecture.

    The Adwene ADW-1 runs from its 52V LiFePO4 battery pack at all times.
    The battery is NEVER bypassed — it is the sole power source for the system.

    Charging sources (managed by MPPT charge controller):
      PRIMARY   → Solar panels (rooftop/facade mounted, always present)
      SECONDARY → Grid supply tap (supplementary charging only, optional)
                  e.g. airport, supermarket — reduces solar dependency indoors

    The software never distinguishes between solar-only and solar+grid charging.
    It only cares about battery SOC and whether charging is happening.

    Battery spec (production target):
      52V LiFePO4 pack | ~2.3 kWh | INA228 shunt monitor (I2C)
      Full day operation target: 100W idle / 250W peak dispensing

    Auto-recharge behaviour:
      When SOC drops to POWER_LOW_PCT (20%), the system signals the charge
      controller to prioritise charging. The system KEEPS RUNNING.
      There is no shutdown unless SOC reaches POWER_CRITICAL_PCT (5%).

    Customer visibility: NONE. Power state is internal only.
    Admin visibility: Full telemetry in admin panel and DB logs.
    """

    # Charging source labels (for telemetry only — not shown to customer)
    SRC_SOLAR    = "SOLAR+BATTERY"
    SRC_BATTERY  = "BATTERY"
    SRC_CHARGING = "CHARGING"     # grid supplementing solar charge

    def __init__(self, db: 'DatabaseManager'):
        self._db              = db
        self._state           = POWER_STATE_OK
        self._source          = self.SRC_SOLAR      # assume solar charging on boot
        self._battery_pct     = 100.0
        self._solar_v         = 13.2                # nominal 52V system, INA228 reads real
        self._charging        = True                # assume charging until hardware says otherwise
        self._start_time      = time.time()
        self._last_log_time   = 0.0
        self._last_recharge_signal = 0.0            # throttle recharge signals
        self._shutdown_requested   = False
        self._shutdown_callbacks: list = []

    # ── Hardware read (GPIO / INA219) ──────────────────────────────────────────
    def read_hardware(self) -> dict:
        """
        Read battery SOC and charging state from INA228 over I2C.
        Prototype: Raspberry Pi GPIO + INA228 breakout (I2C addr 0x40).
        Production: Custom ADW-1 PCB with dedicated power management MCU.

        Simulation produces realistic day-cycle behaviour:
          - Solar charging active ~70% of time (cloud/night variation)
          - Battery slowly discharges when not charging
          - Auto-signals recharge controller at LOW threshold

        Never raises — retains last known values on any hardware error.
        """
        if HW_SIMULATION_MODE:
            solar_active = random.random() > 0.3
            self._solar_v    = 13.2 if solar_active else 0.0
            self._charging   = solar_active
            if solar_active:
                self._source      = self.SRC_SOLAR
                self._battery_pct = min(100.0, self._battery_pct + 0.3)
            else:
                self._source      = self.SRC_BATTERY
                self._battery_pct = max(5.0, self._battery_pct - random.uniform(0, 0.05))
        else:
            try:
                # INA228 over I2C — reads shunt voltage, bus voltage, SOC
                # Prototype wiring: SDA=GPIO2, SCL=GPIO3, ADDR=0x40
                import smbus2
                bus = smbus2.SMBus(1)
                # Register 0x05 = VBUS (bus voltage)
                raw_v = bus.read_word_data(0x40, 0x05)
                bus_v = ((raw_v & 0xFF) << 8 | (raw_v >> 8)) * 3.125e-3
                self._solar_v  = bus_v
                self._charging = bus_v >= POWER_SOLAR_MIN_V
                self._source   = self.SRC_SOLAR if self._charging else self.SRC_BATTERY
                # SOC from register 0x0D (energy accumulator) — simplified linear map
                raw_e = bus.read_word_data(0x40, 0x0D)
                self._battery_pct = max(0.0, min(100.0, raw_e / 655.35))
            except Exception:
                pass   # retain last known values — system continues

        # ── State machine ──────────────────────────────────────────────────
        if self._battery_pct <= POWER_CRITICAL_PCT:
            self._state = POWER_STATE_CRITICAL
        elif self._battery_pct <= POWER_LOW_PCT:
            self._state = POWER_STATE_LOW
        else:
            self._state = POWER_STATE_OK

        # ── Auto-recharge signal at LOW threshold ──────────────────────────
        # Signals charge controller to prioritise input — system keeps running
        if self._state == POWER_STATE_LOW:
            now = time.time()
            if now - self._last_recharge_signal > 300:  # signal every 5min max
                self._last_recharge_signal = now
                self._signal_recharge_priority()

        return self._snapshot()

    def _signal_recharge_priority(self) -> None:
        """
        Tell the MPPT charge controller to prioritise charging over load.
        Prototype: GPIO pulse on GPIO_POWER_SOLAR_DETECT (repurposed as output).
        Production: I2C command to dedicated power MCU.
        Silent — never raises.
        """
        try:
            if not HW_SIMULATION_MODE:
                import RPi.GPIO as GPIO
                GPIO.output(GPIO_POWER_SOLAR_DETECT, GPIO.HIGH)
                time.sleep(0.1)
                GPIO.output(GPIO_POWER_SOLAR_DETECT, GPIO.LOW)
        except Exception:
            pass

    def _read_ina219_pct(self) -> float:
        """Legacy stub — superseded by INA228 I2C read in read_hardware()."""
        return self._battery_pct

    def _snapshot(self) -> dict:
        return {
            "source":       self._source,
            "state":        self._state,
            "battery_pct":  self._battery_pct,
            "solar_v":      self._solar_v,
            "charging":     self._charging,
            "uptime_secs":  int(time.time() - self._start_time),
        }

    # ── Telemetry logging ──────────────────────────────────────────────────────
    def maybe_log_telemetry(self) -> None:
        """
        Log power telemetry at POWER_LOG_INTERVAL_SECS intervals.
        Fully non-blocking — a DB or table error is silently swallowed.
        The system must never halt because of a telemetry write failure.
        """
        now = time.time()
        if now - self._last_log_time < POWER_LOG_INTERVAL_SECS:
            return
        self._last_log_time = now
        snap   = self._snapshot()
        uptime = snap["uptime_secs"]
        wh     = uptime / 3600.0 * 10.0   # ~10W average draw estimate
        try:
            with self._db._conn() as con:
                con.execute(
                    "INSERT INTO power_telemetry "
                    "(unit_id, logged_at, source, state, battery_pct, "
                    "solar_v, wh_consumed, uptime_secs) VALUES (?,?,?,?,?,?,?,?)",
                    (ADWENE_SERIAL, datetime.now().isoformat(),
                     snap["source"], snap["state"], snap["battery_pct"],
                     snap["solar_v"], round(wh, 2), uptime))
        except Exception:
            pass   # telemetry is best-effort — never block the system

    # ── State accessors ────────────────────────────────────────────────────────
    @property
    def state(self) -> str:
        return self._state

    @property
    def source(self) -> str:
        return self._source

    @property
    def battery_pct(self) -> float:
        return self._battery_pct

    @property
    def is_critical(self) -> bool:
        return self._state == POWER_STATE_CRITICAL

    @property
    def is_low(self) -> bool:
        return self._state in (POWER_STATE_LOW, POWER_STATE_CRITICAL)

    # ── Shutdown handler registration ─────────────────────────────────────────
    def register_shutdown_callback(self, cb) -> None:
        """Register a callback invoked before critical power shutdown."""
        self._shutdown_callbacks.append(cb)

    def request_graceful_shutdown(self, reason: str = "POWER_CRITICAL") -> None:
        """
        Triggered when battery hits CRITICAL threshold.
        Runs all registered callbacks in order, then logs and exits.
        """
        if self._shutdown_requested:
            return
        self._shutdown_requested = True
        print(f"\n⚡ POWER CRITICAL — initiating graceful shutdown. Reason: {reason}")
        with self._db._conn() as con:
            con.execute(
                "INSERT INTO power_telemetry "
                "(unit_id, logged_at, source, state, battery_pct, "
                "solar_v, wh_consumed, uptime_secs) VALUES (?,?,?,?,?,?,?,?)",
                (ADWENE_SERIAL, datetime.now().isoformat(),
                 self._source, "SHUTDOWN",
                 self._battery_pct, self._solar_v, 0.0,
                 int(time.time() - self._start_time)))
        for cb in self._shutdown_callbacks:
            try:
                cb(reason)
            except Exception as e:
                print(f"  Shutdown callback error: {e}")
        self._db.log_audit("SYSTEM", "POWER_SHUTDOWN",
                            detail=f"battery={self._battery_pct:.1f}% reason={reason}")
        print("  All queues flushed. System halting safely.")
        # In production: os.system("sudo shutdown -h now")

    def status_line(self) -> str:
        """Admin-facing power status. Never shown to customers."""
        chg   = "⚡ CHARGING" if self._charging else "─ DISCHARGING"
        state = {"OK": "OK", "LOW": "LOW ⚠", "CRITICAL": "CRITICAL 🚨"}.get(self._state, "OK")
        return (f"☀ {self._source}  |  Bat: {self._battery_pct:.0f}%"
                f"  |  {chg}  |  {state}"
                f"  |  Solar: {self._solar_v:.1f}V")


# ─────────────────────────────────────────────────────────────────────────────
# B25: ConnectivityManager
# WiFi → eSIM primary (MTN) → eSIM fallback (Vodafone) → Offline queue mode
# ─────────────────────────────────────────────────────────────────────────────

class ConnectivityManager:
    """
    B29: ADW-1 Connectivity Management — Background-Thread, Offline-First.

    Design principles:
    ─────────────────
    1. OFFLINE IS NORMAL. The system starts pessimistic and proves connectivity
       before using it. 90%+ uptime is expected but never assumed.
    2. MAIN THREAD IS NEVER BLOCKED. All probes run in a background daemon
       thread. Customer UI always stays responsive.
    3. LAST KNOWN STATE. Between probe cycles, the system uses its last
       confirmed connectivity state — no blocking checks mid-transaction.
    4. GRACEFUL CLOUD DEGRADATION:
         • OTA, Nyansa signals, MoMo webhooks → silent offline queue
         • Teleconsult → QR code redirect to Aid Plus mobile app
         • Core dispensing, customer auth, CUPSCAN → always available
    5. CUSTOMER SEES NOTHING about connectivity state unless a specific
       cloud feature is unavailable, in which case they get a clean redirect.

    Priority stack: WiFi → eSIM-MTN → eSIM-Vodafone → Offline
    """

    PRIORITY_STACK = [CONN_WIFI, CONN_ESIM_PRIMARY, CONN_ESIM_FALLBACK, CONN_OFFLINE]

    # Deep link base for mobile app redirects (app team registers this scheme)
    APP_DEEP_LINK_BASE = "aidplus://kiosk-redirect"

    def __init__(self, db: 'DatabaseManager'):
        self._db                  = db
        self._current             = CONN_OFFLINE   # offline-first
        self._last_check          = 0.0
        self._check_interval      = 45.0           # background probe every 45s
        self._queue_flush_running = False
        self._lock                = __import__('threading').Lock()
        # Start background probe thread immediately
        self._start_background_probe()

    # ── Background probe ───────────────────────────────────────────────────────
    def _start_background_probe(self) -> None:
        """Launch a daemon thread that probes connectivity every 45 seconds."""
        import threading
        t = threading.Thread(target=self._probe_loop, daemon=True, name="ADW-ConnProbe")
        t.start()

    def _probe_loop(self) -> None:
        """Background connectivity probe — never touches the main thread."""
        import urllib.request
        while True:
            try:
                time.sleep(self._check_interval)
                detected = CONN_OFFLINE
                for method in self.PRIORITY_STACK:
                    if method == CONN_OFFLINE:
                        detected = CONN_OFFLINE
                        break
                    try:
                        req = urllib.request.Request(
                            CONN_CHECK_URL,
                            headers={"X-ADW-ID": ADWENE_SERIAL,
                                     "X-ADW-Variant": ADW_VARIANT_AS})
                        urllib.request.urlopen(req, timeout=CONN_CHECK_TIMEOUT_S)
                        detected = method
                        break
                    except Exception:
                        continue
                with self._lock:
                    prev = self._current
                    self._current = detected
                    came_online = (prev == CONN_OFFLINE and detected != CONN_OFFLINE)
                if came_online:
                    self._trigger_queue_drain()
            except Exception:
                pass   # probe thread must never crash

    def check(self) -> str:
        """Return last known connectivity state — non-blocking."""
        with self._lock:
            return self._current

    def maybe_check(self) -> str:
        """Alias for check() — kept for compatibility."""
        return self.check()

    @property
    def is_online(self) -> bool:
        return self._current != CONN_OFFLINE

    @property
    def current(self) -> str:
        return self._current

    # ── Offline queue ──────────────────────────────────────────────────────────
    def enqueue(self, endpoint: str, payload: dict,
                method: str = "POST") -> int:
        """
        Queue an API call for later delivery.
        Returns the row ID, or -1 if queueing failed (never raises).
        """
        try:
            now = datetime.now().isoformat()
            with self._db._conn() as con:
                cur = con.execute(
                    "INSERT INTO offline_queue "
                    "(queued_at, endpoint, method, payload, retry_count, "
                    "next_retry_at, status) VALUES (?,?,?,?,0,?,'pending')",
                    (now, endpoint, method, json.dumps(payload), now))
                return cur.lastrowid
        except Exception:
            return -1   # queueing failed — caller continues normally

    def teleconsult_qr_redirect(self, customer_id: str = "",
                                 unit_id: str = "") -> str:
        """
        Generate a deep link URL for the Aid Plus mobile app teleconsult redirect.
        Used when the system is offline and teleconsult is unavailable.

        Deep link format:
            aidplus://kiosk-redirect?action=teleconsult
                &customer_id=XXXX&unit=ADW-XXXX&source=kiosk

        The mobile app team registers the 'aidplus://' URL scheme.
        When scanned, the app opens directly to the teleconsult screen
        with the customer pre-identified.

        In production: rendered as QR code on the touchscreen UI.
        In development: URL is printed to terminal for testing.

        Returns the deep link string (never raises).
        """
        try:
            uid  = unit_id or ADWENE_SERIAL
            cid  = customer_id or "guest"
            link = (f"{self.APP_DEEP_LINK_BASE}"
                    f"?action=teleconsult&customer_id={cid}"
                    f"&unit={uid}&source=kiosk")
            return link
        except Exception:
            return f"{self.APP_DEEP_LINK_BASE}?action=teleconsult"

    def _trigger_queue_drain(self) -> None:
        """Start background queue drain in a daemon thread."""
        if self._queue_flush_running:
            return
        import threading
        t = threading.Thread(target=self._drain_queue, daemon=True)
        t.start()

    def _drain_queue(self) -> None:
        """
        Drain the offline_queue with exponential backoff.
        Runs in a background thread when connectivity is restored.
        """
        import json, urllib.request, urllib.error
        self._queue_flush_running = True
        try:
            while True:
                with self._db._conn() as con:
                    rows = con.execute(
                        "SELECT id, endpoint, method, payload, retry_count "
                        "FROM offline_queue "
                        "WHERE status='pending' AND "
                        "      (next_retry_at IS NULL OR next_retry_at <= ?) "
                        "ORDER BY id ASC LIMIT 20",
                        (datetime.now().isoformat(),)).fetchall()
                if not rows:
                    break
                for row in rows:
                    qid, endpoint, method, payload_str, retries = row
                    try:
                        data = json.dumps(json.loads(payload_str)).encode()
                        req  = urllib.request.Request(
                            endpoint, data=data, method=method,
                            headers={"Content-Type": "application/json",
                                     "X-ADW-ID": ADWENE_SERIAL})
                        urllib.request.urlopen(req, timeout=10)
                        with self._db._conn() as con:
                            con.execute(
                                "UPDATE offline_queue SET status='sent' WHERE id=?",
                                (qid,))
                    except Exception as e:
                        retries += 1
                        interval = CONN_RETRY_INTERVALS[
                            min(retries, len(CONN_RETRY_INTERVALS) - 1)]
                        next_retry = (datetime.now() +
                                      timedelta(seconds=interval)).isoformat()
                        with self._db._conn() as con:
                            con.execute(
                                "UPDATE offline_queue SET retry_count=?, "
                                "next_retry_at=?, last_error=? WHERE id=?",
                                (retries, next_retry, str(e)[:200], qid))
                time.sleep(1)
        finally:
            self._queue_flush_running = False

    def queue_depth(self) -> int:
        """
        Return number of pending offline queue items.
        Returns 0 safely if table unavailable or any error occurs.
        """
        try:
            with self._db._conn() as con:
                return con.execute(
                    "SELECT COUNT(*) FROM offline_queue WHERE status='pending'"
                ).fetchone()[0]
        except Exception:
            return 0

    def status_line(self) -> str:
        """Admin-facing connectivity status. Never shown to customers."""
        try:
            with self._lock:
                cur = self._current
            icons  = {"WiFi": "WiFi", "eSIM-MTN": "eSIM-MTN",
                      "eSIM-Vodafone": "eSIM-VF", "Offline": "Offline"}
            label  = icons.get(cur, "Offline")
            q      = self.queue_depth()
            queued = f"  |  Queue: {q} pending" if q > 0 else ""
            status = "ONLINE" if cur != CONN_OFFLINE else "OFFLINE"
            return f"[{status}]  {label}{queued}"
        except Exception:
            return "[OFFLINE]  Offline"


# ─────────────────────────────────────────────────────────────────────────────
# B25: CUPSCAN Multi-Drop Batch Flow + Weight Anomaly + Reject Chute
# ─────────────────────────────────────────────────────────────────────────────

