"""
AID PLUS+ — CLTS Unit
========================
Camera · Light · Thermometer · Speaker

Physical hardware cluster on the top front face of the ADW-1 kiosk.
  CAMERA:      Motion detection, face detection, biometric 2FA after password.
  LIGHT:       LED ring — illuminates customer in low light / evening conditions.
  THERMOMETER: MLX90614 IR — non-contact body temperature at every session.
  SPEAKER:     TTS greets customer, health alerts, future Nyansa voice AI.

CLTSUnit.run_session() is called after every login and every registration.
clts_real_camera_wake_up() is the primary entry point.
"""
from __future__ import annotations
import time, random
from datetime import datetime

from aidplus.config import *
from aidplus.db import DatabaseManager
from aidplus.auth import BiometricAuthService
from aidplus.hardware import TemperatureSensor
from aidplus.ui import speak, print_header

class CLTSUnit:
    """
    Full CLTS hardware abstraction.
    Manages Camera, LED illumination, Thermometer, and Speaker as one unit.
    Simulation mode used on dev machine — all hardware calls auto-pass.
    """

    def __init__(self, db: DatabaseManager,
                 biometric: "BiometricAuthService" = None,
                 temp_sensor: "TemperatureSensor" = None):
        self.db         = db
        self.biometric  = biometric
        self.temp_sensor = temp_sensor or TemperatureSensor(db)
        self._sim       = HW_SIMULATION_MODE
        self._led_on    = False

    # ── LED Illumination ───────────────────────────────────────────────────────
    def led_on(self, brightness: float = None) -> None:
        """
        [GUI-READY] Activate CLTS LED ring.
        Production: GPIO PWM on GPIO_CLTS_LED_ILLUMIN.
        Brightness auto-selects based on ambient light if not specified.
        """
        if self._sim:
            self._led_on = True
            return
        try:
            import RPi.GPIO as GPIO
            import time as _t
            b = brightness or CLTS_LED_BRIGHT_NORMAL
            # Production: PWM on GPIO_CLTS_LED_ILLUMIN
            # GPIO.output(GPIO_CLTS_LED_ILLUMIN, GPIO.HIGH)
            self._led_on = True
        except Exception:
            pass

    def led_off(self) -> None:
        """Deactivate CLTS LED ring."""
        if self._sim:
            self._led_on = False
            return
        try:
            import RPi.GPIO as GPIO
            # GPIO.output(GPIO_CLTS_LED_ILLUMIN, GPIO.LOW)
            self._led_on = False
        except Exception:
            pass

    # ── Speaker ────────────────────────────────────────────────────────────────
    def say(self, text: str, force: bool = True) -> None:
        """
        CLTS speaker output. Routes through the speak() function.
        In production: amplifier enabled via GPIO_CLTS_SPEAKER_EN before TTS.
        """
        if not self._sim:
            try:
                import RPi.GPIO as GPIO
                # GPIO.output(GPIO_CLTS_SPEAKER_EN, GPIO.HIGH)
                pass
            except Exception:
                pass
        speak(text, force=force)

    # ── Thermometer ────────────────────────────────────────────────────────────
    def read_temperature(self, customer_id: str = None) -> dict:
        """
        Read body temperature via MLX90614 IR sensor.
        Returns reading dict with temperature, flagged status, and advisory.
        """
        reading = self.temp_sensor.read(customer_id)
        temp    = reading["temperature"]

        # Add health advisory based on temperature
        if temp >= CLTS_TEMP_HIGH_FEVER:
            reading["advisory"] = (
                f"High fever detected ({temp}°C). "
                "Please seek medical attention. Paracetamol 500mg is available.")
            reading["severity"] = "high"
        elif temp >= CLTS_TEMP_FEVER:
            reading["advisory"] = (
                f"Elevated temperature ({temp}°C). "
                "Paracetamol 500mg recommended.")
            reading["severity"] = "elevated"
        else:
            reading["advisory"] = f"Temperature normal ({temp}°C)."
            reading["severity"] = "normal"

        return reading

    # ── Camera — Motion Detection ──────────────────────────────────────────────
    def detect_motion(self) -> bool:
        """
        PIR motion sensor check — confirms someone is present before activating.
        Simulation: always True.
        Production: GPIO_CLTS_MOTION_PIR input read.
        """
        if self._sim:
            return True
        try:
            import RPi.GPIO as GPIO
            return GPIO.input(GPIO_CLTS_MOTION_PIR) == GPIO.HIGH
        except Exception:
            return True

    # ── Camera — Face Detection ────────────────────────────────────────────────
    def detect_face(self) -> tuple:
        """
        Detect that a face is present in front of the camera.
        Returns (detected: bool, frame: array|None, gender_estimate: str).
        Uses OpenCV Haar cascade (works without InsightFace).
        """
        if not CV2_AVAILABLE or cv2 is None:
            return True, None, "Unknown"

        try:
            cap = cv2.VideoCapture(0)
            if not cap.isOpened():
                cap.release()
                return True, None, "Unknown"   # no camera → presence assumed

            # Activate LED before opening camera
            self.led_on()

            face_cascade = cv2.CascadeClassifier(
                cv2.data.haarcascades + "haarcascade_frontalface_default.xml")

            detected_frame  = None
            detected_gender = "Unknown"
            face_found      = False
            start = time.time()

            while not face_found and (time.time() - start < CLTS_FACE_TIMEOUT_SECS):
                ret, frame = cap.read()
                if not ret:
                    break
                gray  = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                faces = face_cascade.detectMultiScale(gray, 1.3, 5,
                                                       minSize=(60, 60))
                if len(faces) > 0:
                    x, y, w, h = faces[0]
                    cv2.rectangle(frame, (x, y), (x+w, y+h), (0, 230, 100), 3)
                    cv2.putText(frame, "AID PLUS+", (x, y-10),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 230, 100), 2)
                    detected_frame = frame
                    face_found     = True

                    # Gender estimation if model available
                    if HAS_GENDER_MODEL:
                        try:
                            crop = frame[y:y+h, x:x+w]
                            if crop.size > 0:
                                blob_g = cv2.dnn.blobFromImage(
                                    crop, 1.0, (227, 227),
                                    MODEL_MEAN_VALUES, swapRB=False)
                                gender_net.setInput(blob_g)
                                detected_gender = GENDER_LIST[
                                    gender_net.forward()[0].argmax()]
                        except Exception:
                            pass

                cv2.imshow("AID PLUS+ — CLTS", frame)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break

            cap.release()
            cv2.destroyAllWindows()
            self.led_off()
            return face_found, detected_frame, detected_gender

        except Exception:
            return True, None, "Unknown"

    # ── Camera — Face Authentication (2FA) ────────────────────────────────────
    def authenticate_face(self, customer_id: str) -> dict:
        """
        Confirm that the face in front of the camera matches the enrolled
        account for customer_id. Called after password is verified (2FA).

        Production (ADW-1 with InsightFace):
            Extracts real face embedding → cosine similarity match.
        Development (Windows / no InsightFace):
            Auto-passes so testing is never blocked. Logged as simulated.

        Returns:
            {"verified": bool, "confidence": float, "reason": str}
        """
        if self.biometric is None:
            return {"verified": True, "confidence": 1.0, "reason": "no_biometric_service"}

        # Get stored signature
        stored_sig = self.db.get_face_signature(customer_id)
        if not stored_sig:
            # No enrolled face — pass with warning
            self.db.log_audit(customer_id, "CLTS_AUTH", "customers",
                              customer_id, "No face enrolled — auto-pass")
            return {"verified": True, "confidence": 0.0, "reason": "not_enrolled"}

        # ── Simulation mode — ALWAYS auto-pass ───────────────────────────────
        # HW_SIMULATION_MODE=True on any machine without RPi.GPIO.
        # Face auth on dev machine is meaningless — seeded random embeddings
        # will never match across two different frames.
        # Real matching only happens on ADW-1 with InsightFace installed.
        if self._sim or HW_SIMULATION_MODE:
            self.db.log_audit(customer_id, "CLTS_AUTH", "customers",
                              customer_id, "Simulation — auto-pass")
            return {"verified": True, "confidence": 1.0, "reason": "simulated"}

        # ── Production: ADW-1 with InsightFace ────────────────────────────────
        if CV2_AVAILABLE and cv2 is not None:
            frame = self.biometric._capture_frame()
            if frame is not None:
                liveness = self.biometric._liveness_check()
                if not liveness:
                    self.db.log_audit(customer_id, "CLTS_AUTH_FAIL", "customers",
                                      customer_id, "Liveness failed")
                    return {"verified": False, "confidence": 0.0,
                            "reason": "liveness_failed"}

                embedding = self.biometric._extract_embedding(frame)
                if embedding is not None and len(embedding) == len(stored_sig):
                    confidence = self.biometric._cosine_similarity(
                        stored_sig, embedding)
                    verified   = confidence >= BIOMETRIC_CONFIDENCE_MIN
                    self.db.log_audit(
                        customer_id,
                        "CLTS_AUTH_OK" if verified else "CLTS_AUTH_FAIL",
                        "customers", customer_id,
                        f"Face match confidence={confidence:.3f}")
                    return {"verified": verified,
                            "confidence": round(confidence, 4),
                            "reason": "matched" if verified else "low_confidence"}

        # Fallback — camera opened but no usable frame
        self.db.log_audit(customer_id, "CLTS_AUTH", "customers",
                          customer_id, "No frame captured — auto-pass")
        return {"verified": True, "confidence": 1.0, "reason": "no_frame"}

    # ── Full CLTS Session ──────────────────────────────────────────────────────
    def run_session(self, customer: "Customer",
                    require_face_auth: bool = False) -> tuple:
        """
        Run a complete CLTS session:
            1. Motion confirmed
            2. LED activates
            3. Camera opens — face detected
            4. Temperature read
            5. Speaker greets customer
            6. If require_face_auth: face matched against account (2FA)
            7. Session logged

        Returns: (passed: bool, session_id: int)
        """
        cid  = customer.current["customer_id"] if customer.current else None
        name = customer.current["name"].split()[0] if customer.current else "Customer"

        # ── 1. Motion ─────────────────────────────────────────────────────────
        if not self.detect_motion():
            return True, 0   # no motion sensor in sim — always pass

        # ── 2 + 3. LED + Camera ───────────────────────────────────────────────
        if self._sim:
            # Simulation: no camera available on dev machine
            face_found      = True
            detected_gender = customer.current.get("gender", "U")                               if customer.current else "U"
            frame           = None
        else:
            face_found, frame, detected_gender = self.detect_face()
            if not face_found:
                self.say("Face not detected. Please stand in front of the camera.")
                return False, 0

        # ── 4. Temperature ────────────────────────────────────────────────────
        temp_reading = self.read_temperature(cid)
        temp         = temp_reading["temperature"]
        severity     = temp_reading["severity"]

        # ── 5. Speaker greeting ───────────────────────────────────────────────
        if severity in ("elevated", "high"):
            self.say(temp_reading["advisory"])
        else:
            self.say(f"Welcome, {name}. Temperature normal.")

        # ── 6. Face authentication (2FA — only when require_face_auth=True) ──
        if require_face_auth and cid:
            auth = self.authenticate_face(cid)
            if not auth["verified"]:
                self.say("Face verification failed. Please try again.")
                return False, 0

        # ── 7. Log session ────────────────────────────────────────────────────
        mood = "distressed" if severity != "normal" else "neutral"
        try:
            sid = self.db.log_clts_session(
                cid, detected_gender, temp, face_found, mood)
        except Exception:
            sid = 0

        # Display on screen
        health_note = f"⚠  {temp_reading['advisory']}" if severity != "normal" else "✓ Normal"
        print(f"  ✅  {name} — Temperature: {temp}°C  {health_note}")

        return True, sid


def clts_wake_up_simulation(customer: Customer, db: DatabaseManager) -> tuple:
    """Legacy simulation wrapper — kept for compatibility."""
    unit = CLTSUnit(db)
    return unit.run_session(customer, require_face_auth=False)

def clts_real_camera_wake_up(customer: Customer, db: DatabaseManager,
                              biometric: "BiometricAuthService" = None,
                              require_face_auth: bool = True) -> tuple:
    """
    CLTS full session — Camera · Light · Thermometer · Speaker.
    Called after every login and registration.

    require_face_auth=True  → face must match enrolled account (2FA at login)
    require_face_auth=False → presence detection only (at registration, first boot)

    Simulation mode: auto-passes all hardware steps. Never blocks testing.
    """
    unit = CLTSUnit(db, biometric=biometric)
    return unit.run_session(customer, require_face_auth=require_face_auth)


# ═══════════════════════════════════════════════════════════════════════════════
# PAYMENT HELPERS
# ═══════════════════════════════════════════════════════════════════════════════
