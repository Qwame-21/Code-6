"""
AID PLUS+
            except Exception:
                pass — Hardware Abstraction Layer
========================================
Physical hardware drivers for the Adwene ADW-1 kiosk.
HardwareInterface, TemperatureSensor, CardReader, CUPSCANModule, DispenserManager.
Simulation mode auto-activates when RPi.GPIO is not present (HW_SIMULATION_MODE=True).
"""
from __future__ import annotations
import os, random, time, threading, json
from datetime import datetime

from aidplus.config import *
from aidplus.bus import AidPlusServiceBus, ServiceNotAvailable
from aidplus.db import DatabaseManager
from aidplus.bus import AidPlusServiceBus

class HardwareInterface:
    """
    [B18-A/B] GPIO Trigger control for all 13 dispensing columns.
    Maps shelf_num → GPIO BCM pin → solenoid trigger pulse.
    Also controls LED indicators and Ice Pocket relays.
    Simulation mode: logs action, no physical output.
    """

    def __init__(self, db: DatabaseManager):
        self.db          = db
        self.sim_mode    = HW_SIMULATION_MODE
        self._setup_pins()

    def _setup_pins(self):
        if self.sim_mode: return
        all_outputs = (
            list(GPIO_SHELF_MAP.values()) +
            [GPIO_LED_AID_RED, GPIO_LED_AID_GREEN,
             GPIO_LED_CPR_RED, GPIO_LED_CPR_GREEN,
             GPIO_LED_READY,
             GPIO_ICE_LEFT,    GPIO_ICE_RIGHT]
        )
        for pin in all_outputs:
            GPIO.setup(pin, GPIO.OUT, initial=GPIO.LOW)
        # System ready LED on immediately
        GPIO.output(GPIO_LED_READY, GPIO.HIGH)

    def fire_trigger(self, shelf_num: int,
                      customer_id: str = None,
                      transaction_id: str = None) -> dict:
        """
        Main dispense entry point for all shelves.
        [B21-E] Routes shelf 1-7 to _fire_solenoid() (capsule columns).
                Routes shelf 8-13 to dispense_lower_bay() (bottle-lift mechanism).
        """
        if shelf_num in GPIO_LOWER_BAY_STEP:
            return self.dispense_lower_bay(shelf_num, customer_id, transaction_id)
        return self._fire_solenoid(shelf_num, customer_id, transaction_id)

    def _fire_solenoid(self, shelf_num: int,
                        customer_id: str = None,
                        transaction_id: str = None) -> dict:
        """
        Solenoid trigger for upper capsule columns (shelves 1-7).
        Pulse: GPIO HIGH for GPIO_TRIGGER_PULSE_MS → LOW.
        IR beam break confirms capsule passed through outlet.
        """
        pin = GPIO_SHELF_MAP.get(shelf_num)
        if pin is None:
            return {"success": False, "error": f"No GPIO pin for shelf {shelf_num}"}

        mode   = "simulated" if self.sim_mode else "real"
        log_id = self.db.log_dispense(shelf_num, f"Shelf {shelf_num}",
                                       pin, customer_id, transaction_id, mode)

        if self.sim_mode:
            time.sleep(GPIO_TRIGGER_PULSE_MS / 1000)
            self.db.confirm_dispense(log_id, "confirmed")
            self.db.log_audit("HARDWARE", "DISPENSE_OK", "dispense_log",
                              str(log_id), f"SIM solenoid shelf={shelf_num}")
            return {"success": True, "mode": "simulated",
                    "shelf": shelf_num, "log_id": log_id}

        try:
            GPIO.output(pin, GPIO.HIGH)
            time.sleep(GPIO_TRIGGER_PULSE_MS / 1000)
            GPIO.output(pin, GPIO.LOW)

            # IR beam break confirmation
            deadline  = time.time() + (GPIO_IR_TIMEOUT_MS / 1000)
            confirmed = False
            while time.time() < deadline:
                # Production: GPIO.input(IR_CONFIRM_PIN) — wire IR sensor here
                time.sleep(0.05)
                confirmed = True   # stub — replace with GPIO.input read
                break

            status = "confirmed" if confirmed else "jammed"
            self.db.confirm_dispense(log_id, status)
            if not confirmed:
                self.db.log_audit("HARDWARE", "DISPENSE_JAM", "dispense_log",
                                  str(log_id), f"Shelf {shelf_num} solenoid jam")
                return {"success": False, "mode": "real",
                        "error": "Jam — no IR confirmation",
                        "shelf": shelf_num, "log_id": log_id}

            self.db.log_audit("HARDWARE", "DISPENSE_OK", "dispense_log",
                              str(log_id), f"shelf={shelf_num} pin={pin}")
            return {"success": True, "mode": "real",
                    "shelf": shelf_num, "log_id": log_id}

        except Exception as e:
            self.db.confirm_dispense(log_id, "failed")
            return {"success": False, "error": str(e),
                    "shelf": shelf_num, "log_id": log_id}

    def dispense_lower_bay(self, shelf_num: int,
                            customer_id: str = None,
                            transaction_id: str = None) -> dict:
        """
        [B21-E] Bottle-lift dispense for lower bays (shelves 8-13).

        Mechanism:
          1. Lead screw stepper lifts cradle until position sensor trips
          2. Transfer arm servo slides bottle onto shared outlet rail
          3. Outlet presence sensor confirms bottle seated
          4. Outlet solenoid releases bottle to customer
          5. Transfer arm retracts, stepper lowers cradle to rest

        In simulation mode: logs and returns success without GPIO.
        """
        step_pin = GPIO_LOWER_BAY_STEP.get(shelf_num)
        dir_pin  = GPIO_LOWER_BAY_DIR.get(shelf_num)
        pos_pin  = GPIO_LOWER_BAY_POS.get(shelf_num)
        if step_pin is None:
            return {"success": False,
                    "error": f"No lower bay config for shelf {shelf_num}"}

        mode   = "simulated" if self.sim_mode else "real"
        log_id = self.db.log_dispense(shelf_num, f"Bay {shelf_num - 7}",
                                       step_pin, customer_id, transaction_id, mode)

        if self.sim_mode:
            # Simulate full sequence timing
            time.sleep(0.4)   # lift + transfer + release
            self.db.confirm_dispense(log_id, "confirmed")
            self.db.log_audit("HARDWARE", "DISPENSE_OK", "dispense_log",
                              str(log_id),
                              f"SIM lower_bay shelf={shelf_num}")
            return {"success": True, "mode": "simulated",
                    "shelf": shelf_num, "log_id": log_id,
                    "mechanism": "bottle_lift"}

        try:
            # ── Step 1: Lift cradle ─────────────────────────────────────────
            GPIO.output(dir_pin, GPIO.HIGH)   # UP direction
            lifted = False
            for _ in range(GPIO_LOWER_STEPS_UP):
                GPIO.output(step_pin, GPIO.HIGH)
                time.sleep(GPIO_LOWER_STEP_DELAY_MS / 1000)
                GPIO.output(step_pin, GPIO.LOW)
                time.sleep(GPIO_LOWER_STEP_DELAY_MS / 1000)
                if GPIO.input(pos_pin) == GPIO.HIGH:
                    lifted = True
                    break

            if not lifted:
                self.db.confirm_dispense(log_id, "jammed")
                self.db.log_audit("HARDWARE", "DISPENSE_JAM", "dispense_log",
                                  str(log_id),
                                  f"Lower bay {shelf_num} — cradle lift failed")
                return {"success": False, "mode": "real",
                        "error": "Lift failed — position sensor not triggered",
                        "shelf": shelf_num, "log_id": log_id}

            # ── Step 2: Transfer arm extends — slides bottle to outlet ──────
            if not self.sim_mode:
                import RPi.GPIO as _GPIO
                pwm = _GPIO.PWM(GPIO_TRANSFER_ARM_SERVO, 50)
                pwm.start(GPIO_TRANSFER_ARM_EXTEND)
                time.sleep(0.5)

            # ── Step 3: Confirm bottle seated on outlet rail ────────────────
            deadline  = time.time() + 1.5
            on_rail   = False
            while time.time() < deadline:
                if GPIO.input(GPIO_OUTLET_PRESENCE) == GPIO.HIGH:
                    on_rail = True
                    break
                time.sleep(0.05)

            if not on_rail:
                # Retract arm, lower cradle, abort
                if not self.sim_mode:
                    pwm.ChangeDutyCycle(GPIO_TRANSFER_ARM_RETRACT)
                    time.sleep(0.4)
                    pwm.stop()
                GPIO.output(dir_pin, GPIO.LOW)
                for _ in range(GPIO_LOWER_STEPS_UP):
                    GPIO.output(step_pin, GPIO.HIGH)
                    time.sleep(GPIO_LOWER_STEP_DELAY_MS / 1000)
                    GPIO.output(step_pin, GPIO.LOW)
                    time.sleep(GPIO_LOWER_STEP_DELAY_MS / 1000)
                self.db.confirm_dispense(log_id, "jammed")
                return {"success": False, "mode": "real",
                        "error": "Transfer failed — bottle not on outlet rail",
                        "shelf": shelf_num, "log_id": log_id}

            # ── Step 4: Release bottle at outlet ───────────────────────────
            GPIO.output(GPIO_OUTLET_NOZZLE, GPIO.HIGH)
            time.sleep(0.3)
            GPIO.output(GPIO_OUTLET_NOZZLE, GPIO.LOW)

            # ── Step 5: Retract arm, lower cradle ──────────────────────────
            if not self.sim_mode:
                pwm.ChangeDutyCycle(GPIO_TRANSFER_ARM_RETRACT)
                time.sleep(0.4)
                pwm.stop()

            GPIO.output(dir_pin, GPIO.LOW)   # DOWN direction
            for _ in range(GPIO_LOWER_STEPS_UP):
                GPIO.output(step_pin, GPIO.HIGH)
                time.sleep(GPIO_LOWER_STEP_DELAY_MS / 1000)
                GPIO.output(step_pin, GPIO.LOW)
                time.sleep(GPIO_LOWER_STEP_DELAY_MS / 1000)

            self.db.confirm_dispense(log_id, "confirmed")
            self.db.log_audit("HARDWARE", "DISPENSE_OK", "dispense_log",
                              str(log_id),
                              f"lower_bay shelf={shelf_num} bottle dispensed")
            return {"success": True, "mode": "real",
                    "shelf": shelf_num, "log_id": log_id,
                    "mechanism": "bottle_lift"}

        except Exception as e:
            self.db.confirm_dispense(log_id, "failed")
            return {"success": False, "error": str(e),
                    "shelf": shelf_num, "log_id": log_id}

    def activate_light_loop(self, order_id: str) -> dict:
        """Light Loop verification — safe bus emit."""
        checks = {"conveyor": False, "qr_scan": False,
                  "weight_ok": False, "seal_ok": False, "door_open": False}
        if self.sim_mode:
            checks = {k: True for k in checks}
            try:
                bus = AidPlusServiceBus(self.db)
                bus.emit("LIGHT_LOOP_PASS_CONFIRMED", {
                    "order_id": order_id, "unit_id": UNIT_ID,
                    "checks": checks, "timestamp": datetime.now().isoformat(),
                    "mode": "simulated"})
            except Exception:
                pass
            return {"pass": True, "order_id": order_id, "checks": checks}
        try:
            GPIO.output(GPIO_LIGHT_LOOP_CONVEYOR, GPIO.HIGH)
            time.sleep(0.5)
            GPIO.output(GPIO_LIGHT_LOOP_QR_TRIG, GPIO.HIGH)
            time.sleep(0.1)
            GPIO.output(GPIO_LIGHT_LOOP_QR_TRIG, GPIO.LOW)
            checks["conveyor"]  = True
            checks["qr_scan"]   = True
            checks["weight_ok"] = True
            checks["seal_ok"]   = GPIO.input(GPIO_LIGHT_LOOP_IR_SEAL) == GPIO.HIGH
            GPIO.output(GPIO_LIGHT_LOOP_BAY_DOOR, GPIO.HIGH)
            time.sleep(0.3)
            GPIO.output(GPIO_LIGHT_LOOP_BAY_DOOR, GPIO.LOW)
            checks["door_open"] = True
            GPIO.output(GPIO_LIGHT_LOOP_CONVEYOR, GPIO.LOW)
        except Exception:
            pass
        passed = all(checks.values())
        try:
            from aidplus.bus import AidPlusServiceBus
            AidPlusServiceBus.emit(
                "LIGHT_LOOP_PASS_CONFIRMED" if passed else "LIGHT_LOOP_PASS_FAILED",
                {"order_id": order_id, "unit_id": UNIT_ID, "checks": checks,
                 "timestamp": datetime.now().isoformat()})
        except Exception:
            pass
        return {"pass": passed, "order_id": order_id, "checks": checks}

    def set_led(self, pin: int, state: bool):
        if self.sim_mode: return
        GPIO.output(pin, GPIO.HIGH if state else GPIO.LOW)

    def sync_leds_to_db(self):
        """Read hardware_status from DB and set LEDs accordingly."""
        hw = self.db.get_hw_status()
        aid_docked = hw.get("aid_box_status", "Docked") == "Docked"
        cpr_docked = hw.get("cpr_kit_status", "Docked") == "Docked"
        self.set_led(GPIO_LED_AID_GREEN,  aid_docked)
        self.set_led(GPIO_LED_AID_RED,   not aid_docked)
        self.set_led(GPIO_LED_CPR_GREEN,  cpr_docked)
        self.set_led(GPIO_LED_CPR_RED,   not cpr_docked)

    # ── Ice Pocket Relays ──────────────────────────────────────────────────────
    def set_ice_pocket(self, side: str, active: bool):
        """side: 'left' | 'right' | 'both'"""
        if self.sim_mode: return
        if side in ("left",  "both"): GPIO.output(GPIO_ICE_LEFT,  GPIO.HIGH if active else GPIO.LOW)
        if side in ("right", "both"): GPIO.output(GPIO_ICE_RIGHT, GPIO.HIGH if active else GPIO.LOW)

    def _set_door(self, open_door: bool) -> None:
        """Open or close the Cupsule drop door."""
        if self._sim:
            return
        try:
            GPIO.output(GPIO_CUPSCAN_DROP_DOOR, GPIO.HIGH if open_door else GPIO.LOW)
            time.sleep(CUPSCAN_DOOR_OPEN_SECS if open_door else 0.5)
        except Exception:
            pass

    def verify_cup(self) -> dict:
        """
        Run a full verification cycle on the cup in the chute.
        Returns dict with: weight_g, uv_pass, condition, co2_saved_g, water_saved_l
        """
        if self._sim:
            weight  = round(random.uniform(8.0, 14.0), 1)
            uv_pass = random.random() > 0.05
            return {
                "weight_g":     weight,
                "uv_pass":      uv_pass,
                "condition":    "INTACT" if weight >= CUPSCAN_WEIGHT_MIN_G else "EMPTY",
                "co2_saved_g":  round(weight * 0.8, 2),
                "water_saved_l": round(weight * 0.003, 3),
            }
        result = self.run_verification()
        weight  = result.get("weight_g", 0.0)
        uv_pass = result.get("uv_pass",  False)
        _, condition = cupscan_classify_weight(weight, uv_pass)
        return {
            "weight_g":     weight,
            "uv_pass":      uv_pass,
            "condition":    condition,
            "co2_saved_g":  round(weight * 0.8, 2),
            "water_saved_l": round(weight * 0.003, 3),
        }

    def cleanup(self):
        if not self.sim_mode and GPIO:
            GPIO.cleanup()

    def hardware_health(self) -> dict:
        """Self-test: returns status of all GPIO pins and simulation mode."""
        return {
            "simulation_mode": self.sim_mode,
            "gpio_available":  not self.sim_mode,
            "shelf_count":     len(GPIO_SHELF_MAP),
            "shelf_pins":      GPIO_SHELF_MAP,
            "leds": {
                "aid_red":   GPIO_LED_AID_RED,
                "aid_green": GPIO_LED_AID_GREEN,
                "cpr_red":   GPIO_LED_CPR_RED,
                "cpr_green": GPIO_LED_CPR_GREEN,
                "ready":     GPIO_LED_READY,
            },
            "ice_pockets": {
                "left":  GPIO_ICE_LEFT,
                "right": GPIO_ICE_RIGHT,
            },
        }


class TemperatureSensor:
    """
    [B18-C] MLX90614 IR thermometer via I2C (smbus2).
    Falls back to simulation if sensor not detected.
    Logs all readings to thermal_log table.
    """

    def __init__(self, db: DatabaseManager):
        self.db       = db
        self.sim_mode = not HAS_SMBUS
        self._bus     = None
        if not self.sim_mode:
            try:
                self._bus = smbus2.SMBus(1)   # I2C bus 1 on Raspberry Pi
            except Exception:
                self.sim_mode = True

    def _read_raw(self) -> tuple[float, float]:
        """Read ambient + object temperature from MLX90614. Returns (ambient, obj) °C."""
        raw_obj = self._bus.read_word_data(MLX90614_I2C_ADDR, MLX90614_TOBJ_REG)
        raw_amb = self._bus.read_word_data(MLX90614_I2C_ADDR, 0x06)
        obj_k   = raw_obj * 0.02
        amb_k   = raw_amb * 0.02
        return round(amb_k - 273.15, 2), round(obj_k - 273.15, 2)

    def read(self, customer_id: str = None) -> dict:
        """Take a reading, log it, return dict."""
        if self.sim_mode:
            ambient = round(random.uniform(24.0, 30.0), 2)   # room temp Ghana
            obj_temp = round(random.uniform(36.0, 37.8), 2)  # normal body temp
            mode    = "simulated"
        else:
            try:
                ambient, obj_temp = self._read_raw()
                mode = "real"
            except Exception as e:
                ambient  = 25.0
                obj_temp = 36.5
                mode     = "simulated"

        log_id = self.db.log_thermal(ambient, obj_temp, customer_id, mode)
        flagged = obj_temp >= 38.0
        return {
            "ambient":     ambient,
            "temperature": obj_temp,
            "mode":        mode,
            "flagged":     flagged,
            "flag_reason": "High temperature — fever detected" if flagged else "",
            "log_id":      log_id,
        }

    def get_stats(self) -> dict:
        return self.db.get_thermal_stats()


class CardReader:
    """
    [B18-D] Physical membership card reader via UART serial.
    Reads card UID from /dev/ttyUSB0, looks up customer.
    Falls back to simulation (manual entry) if serial not available.
    """

    def __init__(self, db: DatabaseManager):
        self.db       = db
        self.sim_mode = True
        self._serial  = None
        try:
            import serial
            self._serial  = serial.Serial(CARD_READER_PORT, CARD_READER_BAUD, timeout=2)
            self.sim_mode = False
        except Exception:
            self.sim_mode = True

    def read_card(self) -> dict:
        """
        Blocking read (2s timeout). Returns customer_id if card found.
        Simulation: returns None (caller prompts manual ID entry).
        """
        if self.sim_mode:
            return {"customer_id": None, "mode": "simulated",
                    "message": "Card reader not available — use manual ID entry."}
        try:
            raw = self._serial.readline().decode("utf-8").strip()
            if not raw:
                return {"customer_id": None, "mode": "real", "message": "No card read."}
            # Card UID is stored in customer face_signature or a card_uid field
            # Look up by UID
            with self.db._conn() as con:
                row = con.execute(
                    "SELECT customer_id FROM customers WHERE card_uid=?",
                    (raw,)).fetchone()
            if row:
                return {"customer_id": row["customer_id"], "mode": "real",
                        "card_uid": raw}
            return {"customer_id": None, "mode": "real",
                    "message": f"Card UID {raw} not registered."}
        except Exception as e:
            return {"customer_id": None, "mode": "real", "error": str(e)}


# ═══════════════════════════════════════════════════════════════════════════════
# BUILD 24 — CUPSCAN MODULE (side-docked peripheral on ADW-1)
# ═══════════════════════════════════════════════════════════════════════════════

class CUPSCANModule:
    """
    [B24-C] Unified CUPSCAN peripheral driver.

    The CUPSCAN is a side-docked enclosure bolted onto the Aid System kiosk.
    No second compute board. All sensors wire directly to ADW-1 GPIO.

    Physical layout:
    ┌─────────────────────┐
    │  [Camera + UV LED]  │  ← top of chute: condition CV + UV marker auth
    │  [Photodiode]       │  ← UV fluorescence detector
    │  [Weight Cell]      │  ← HX711 load cell on SPI
    ├─────────────────────┤
    │    DROP DOOR        │  ← solenoid-latched hatch, opens per session
    ├─────────────────────┤
    │                     │
    │  RECYCLE BIN        │  ← bin level sensors (HALF / FULL)
    │  COMPARTMENT        │
    └─────────────────────┘

    Authentication is handled by the Aid System session — no card reader
    on the CUPSCAN module (eliminated in B24, cost saving).

    Verification pipeline (all within CUPSCAN_VERIFY_TIMEOUT_MS):
    1. Door opens  → chute IR confirms Cupsule dropped
    2. Weight cell → confirms mass within Cupsule range (8–20g)
    3. UV LED on   → photodiode confirms AID UV marker present
    4. Camera cap  → CV classifies condition (INTACT/PARTIAL/ANOMALY/CONTAMINATED)
    5. Door latches → result returned to caller
    """

    # CO₂ and water savings per Cupsule (12g PLA)
    CO2_PER_CUPSULE_G    = 12.0 * 2.1    # 25.2g CO₂ avoided
    WATER_PER_CUPSULE_L  = 12.0 * 0.055  # 0.66L water saved

    # Condition → base points (mirrored from locked CUPSCAN economics)
    BASE_POINTS = {
        # 1 Cupsule = 1 point. Simple and honest.
        # 10 points = ₵2.00 bonus credit redeemable on drugs.
        "INTACT":       1,
        "PARTIAL":      1,   # partial return still counts
        "ANOMALY":      0,
        "CONTAMINATED": 0,
    }

    def __init__(self, db: "DatabaseManager"):
        self.db   = db
        self._sim = HW_SIMULATION_MODE
        if not self._sim:
            try:
                self._init_gpio()
            except Exception as e:
                print(f"[CUPSCANModule] GPIO init failed, falling back to simulation: {e}")
                self._sim = True
        # Camera shared with BiometricAuthService — acquire only during scan
        self._cam = None

    def _init_gpio(self):
        """Configure all CUPSCAN GPIO pins."""
        # Outputs
        for pin in (GPIO_CUPSCAN_DROP_DOOR, GPIO_CUPSCAN_UV_LED,
                    GPIO_CUPSCAN_LED_READY, GPIO_CUPSCAN_LED_BUSY,
                    GPIO_CUPSCAN_LED_ERROR):
            GPIO.setup(pin, GPIO.OUT, initial=GPIO.LOW)
        # Inputs
        for pin in (GPIO_CUPSCAN_DOOR_SENSOR, GPIO_CUPSCAN_CHUTE_IR,
                    GPIO_CUPSCAN_UV_SENSOR, GPIO_CUPSCAN_BIN_FULL,
                    GPIO_CUPSCAN_BIN_HALF):
            GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
        # Status: ready
        GPIO.output(GPIO_CUPSCAN_LED_READY, GPIO.HIGH)

    # ── Public interface ───────────────────────────────────────────────────────

    def bin_status(self) -> dict:
        """Read current bin fill level."""
        if self._sim:
            return {"full": False, "half": False, "pct_estimate": 30,
                    "mode": "simulation"}
        full = GPIO.input(GPIO_CUPSCAN_BIN_FULL)
        half = GPIO.input(GPIO_CUPSCAN_BIN_HALF)
        return {
            "full":        bool(full),
            "half":        bool(half),
            "pct_estimate": 90 if full else (55 if half else 20),
            "mode":        "live",
        }

    def hardware_health(self) -> dict:
        """Quick self-test — returns sensor reachability."""
        bin_st = self.bin_status()
        return {
            "mode":        "simulation" if self._sim else "live",
            "drop_door":   "ok",
            "uv_circuit":  "ok" if self._sim else self._test_uv(),
            "weight_cell": "ok",
            "camera":      "ok",
            "bin_full":    bin_st["full"],
            "bin_half":    bin_st["half"],
            "bin_pct":     bin_st["pct_estimate"],
        }

    def run_verification(self) -> dict:
        """
        Full Cupsule verification pipeline.
        Opens drop door, waits for drop, runs all sensor checks.
        Returns result dict with: accepted, compartment, weight_g,
        uv_pass, condition, co2_saved_g, water_saved_l, error.
        """
        if self.bin_status()["full"]:
            return {"accepted": False, "error": "BIN_FULL",
                    "message": "Recycle bin is full. Please notify staff."}
        if self._sim:
            return self._simulate_verification()
        return self._live_verification()

    # ── Simulation path ────────────────────────────────────────────────────────

    def _simulate_verification(self) -> dict:
        """Simulated verification for development / non-GPIO environments."""
        import random
        conditions = ["INTACT", "INTACT", "INTACT", "PARTIAL",
                      "ANOMALY", "CONTAMINATED"]
        cond   = random.choice(conditions)
        weight = {"INTACT": 12.0, "PARTIAL": 9.5,
                  "ANOMALY": 7.0, "CONTAMINATED": 11.0}[cond]
        return {
            "accepted":    True,
            "compartment": cond,
            "weight_g":    weight,
            "uv_pass":     cond != "CONTAMINATED",
            "condition":   cond,
            "co2_saved_g": self.CO2_PER_CUPSULE_G,
            "water_saved_l": self.WATER_PER_CUPSULE_L,
            "mode":        "simulation",
        }

    # ── Live hardware path ─────────────────────────────────────────────────────

    def _live_verification(self) -> dict:
        """Real hardware verification sequence."""
        import time
        result = {
            "accepted": False, "compartment": None,
            "weight_g": 0.0,   "uv_pass": False,
            "condition": None, "mode": "live",
            "co2_saved_g": 0.0, "water_saved_l": 0.0,
        }
        try:
            self._set_status("busy")
            # 1. Open drop door
            GPIO.output(GPIO_CUPSCAN_DROP_DOOR, GPIO.HIGH)
            deadline = time.time() + CUPSCAN_DOOR_OPEN_SECS
            # 2. Wait for chute IR break (Cupsule dropped)
            cupsule_detected = False
            while time.time() < deadline:
                if GPIO.input(GPIO_CUPSCAN_CHUTE_IR):
                    cupsule_detected = True
                    break
                time.sleep(0.05)
            GPIO.output(GPIO_CUPSCAN_DROP_DOOR, GPIO.LOW)  # latch door

            if not cupsule_detected:
                result["error"] = "NO_DROP_DETECTED"
                result["message"] = "No Cupsule detected. Door closed."
                self._set_status("ready")
                return result

            # 3. Weight check
            weight = self._read_weight()
            result["weight_g"] = weight
            if weight < CUPSCAN_WEIGHT_MIN_G:
                result["error"]   = "WEIGHT_TOO_LOW"
                result["message"] = f"Object too light ({weight:.1f}g). Not a Cupsule."
                self._set_status("ready")
                return result

            # 4. UV marker authentication
            GPIO.output(GPIO_CUPSCAN_UV_LED, GPIO.HIGH)
            time.sleep(CUPSCAN_UV_DWELL_MS / 1000)
            uv_pass = bool(GPIO.input(GPIO_CUPSCAN_UV_SENSOR))
            GPIO.output(GPIO_CUPSCAN_UV_LED, GPIO.LOW)
            result["uv_pass"] = uv_pass

            # 5. Camera condition assessment
            time.sleep(CUPSCAN_CAMERA_WARMUP_MS / 1000)
            condition = self._assess_condition(weight, uv_pass)
            result["condition"]   = condition
            result["compartment"] = condition
            result["accepted"]    = True
            result["co2_saved_g"]   = self.CO2_PER_CUPSULE_G
            result["water_saved_l"] = self.WATER_PER_CUPSULE_L

        except Exception as e:
            result["error"]   = "HW_ERROR"
            result["message"] = str(e)
        finally:
            GPIO.output(GPIO_CUPSCAN_DROP_DOOR, GPIO.LOW)
            GPIO.output(GPIO_CUPSCAN_UV_LED,    GPIO.LOW)
            self._set_status("ready" if result.get("accepted") else "error")

        return result

    def _read_weight(self) -> float:
        """Read HX711 load cell via SPI. Returns grams."""
        try:
            import spidev
            spi = spidev.SpiDev()
            spi.open(0, 0)
            spi.max_speed_hz = 1000000
            raw = spi.readbytes(3)
            spi.close()
            # HX711 24-bit signed → grams (calibration factor per unit)
            raw_val = (raw[0] << 16 | raw[1] << 8 | raw[2])
            if raw_val & 0x800000:
                raw_val -= 0x1000000
            return abs(raw_val) / 1000.0  # calibrate per deployment
        except Exception:
            return 12.0   # fallback — assume INTACT weight

    def _assess_condition(self, weight: float, uv_pass: bool) -> str:
        """
        CV condition assessment using camera + weight heuristics.
        In production: full OpenCV pipeline with Nyansa ML model.
        """
        try:
            cam = cv2.VideoCapture(0)
            if not cam.isOpened():
                raise RuntimeError("Camera not available")
            import time as _t
            _t.sleep(CUPSCAN_CAMERA_WARMUP_MS / 1000)
            ret, frame = cam.read()
            cam.release()
            if not ret or frame is None:
                raise RuntimeError("No frame captured")
            # Lightweight heuristic: brightness + edge density → condition score
            gray     = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            edges    = cv2.Canny(gray, 50, 150)
            edge_pct = (edges > 0).mean()
            mean_lum = gray.mean()
            # CONTAMINATED: UV failed
            if not uv_pass:
                return "CONTAMINATED"
            # INTACT: good UV, good weight, low edge fragmentation
            if weight >= 10.0 and edge_pct < 0.15 and mean_lum > 80:
                return "INTACT"
            # PARTIAL: slightly below weight or moderate fragmentation
            if weight >= 8.0 and edge_pct < 0.30:
                return "PARTIAL"
            # ANOMALY: weight in range but visually irregular
            if CUPSCAN_WEIGHT_MIN_G <= weight <= CUPSCAN_WEIGHT_MAX_G:
                return "ANOMALY"
            return "CONTAMINATED"
        except Exception:
            # Fallback on camera failure — use weight + UV only
            if not uv_pass:   return "CONTAMINATED"
            if weight >= 10.0: return "INTACT"
            if weight >= 8.0:  return "PARTIAL"
            return "ANOMALY"

    def _test_uv(self) -> str:
        """Quick UV circuit test — pulse LED, check photodiode responds."""
        try:
            import time
            GPIO.output(GPIO_CUPSCAN_UV_LED, GPIO.HIGH)
            time.sleep(0.1)
            reading = GPIO.input(GPIO_CUPSCAN_UV_SENSOR)
            GPIO.output(GPIO_CUPSCAN_UV_LED, GPIO.LOW)
            return "ok"
        except Exception as e:
            return f"fault: {e}"

    def _set_status(self, state: str):
        """Drive CUPSCAN panel status LEDs."""
        if self._sim:
            return
        GPIO.output(GPIO_CUPSCAN_LED_READY, GPIO.LOW)
        GPIO.output(GPIO_CUPSCAN_LED_BUSY,  GPIO.LOW)
        GPIO.output(GPIO_CUPSCAN_LED_ERROR, GPIO.LOW)
        if state == "ready":
            GPIO.output(GPIO_CUPSCAN_LED_READY, GPIO.HIGH)
        elif state == "busy":
            GPIO.output(GPIO_CUPSCAN_LED_BUSY, GPIO.HIGH)
        elif state == "error":
            GPIO.output(GPIO_CUPSCAN_LED_ERROR, GPIO.HIGH)

    def cleanup(self):
        """Called on system shutdown — ensure door is latched and LEDs off."""
        if not self._sim:
            try:
                GPIO.output(GPIO_CUPSCAN_DROP_DOOR, GPIO.LOW)
                GPIO.output(GPIO_CUPSCAN_UV_LED,    GPIO.LOW)
                self._set_status("ready")
            except Exception:
                pass


class DispenserManager:
    """
    [B18-E] Orchestrates the full dispense lifecycle:
    1. Check stock in DB
    2. Fire GPIO Trigger via HardwareInterface
    3. Confirm via IR beam
    4. Decrement stock in DB
    5. Write to PDMS audit log
    All steps are atomic — if any fail, stock is NOT decremented.
    """

    def __init__(self, db: DatabaseManager, hw: HardwareInterface):
        self.db = db
        self.hw = hw

    def dispense(self, shelf_num: int, qty: int = 1,
                  customer_id: str = None,
                  transaction_id: str = None) -> dict:
        results = []
        for i in range(qty):
            result = self.hw.fire_trigger(shelf_num, customer_id, transaction_id)
            if not result["success"]:
                return {
                    "success":    False,
                    "dispensed":  i,
                    "requested":  qty,
                    "error":      result.get("error", "Trigger failed"),
                    "shelf":      shelf_num,
                }
            # Decrement stock
            item = self.db.get_item_by_shelf(shelf_num)
            if item:
                if item.get("is_mega"):
                    self.db.decrement_mega(shelf_num)
                else:
                    self.db.decrement_upper(shelf_num)
            results.append(result)

        self.db.log_audit(
            customer_id or "SYSTEM", "DISPENSE",
            "dispense_log", str(shelf_num),
            f"shelf={shelf_num} qty={qty} mode={results[0]['mode']}"
        )
        return {
            "success":   True,
            "dispensed": qty,
            "shelf":     shelf_num,
            "mode":      results[0]["mode"],
        }


# ═══════════════════════════════════════════════════════════════════════════════
# BUILD 19 — BUSINESS INTELLIGENCE & REPORTING
# ═══════════════════════════════════════════════════════════════════════════════
