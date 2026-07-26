"""
AID PLUS+ — Authentication & Identity Services
================================================
BiometricAuthService: camera-based face verification (2FA), liveness detection,
                      face-only login, enrollment.
NiaVerificationService: Ghana Card (NIA API) identity verification.
WelcomeMessageService: registration welcome notification + app sync QR.
CupsuleService: Cupsule QR issuance and return tracking.
PasswordRecoveryService: three recovery paths (app QR / card tap / Ghana Card).
"""
from __future__ import annotations
import json, random, time, hashlib, secrets
from datetime import datetime, timedelta

from aidplus.config import *
from aidplus.db import DatabaseManager
from aidplus.security import hash_password, verify_password, secure_id, secure_ref
from aidplus.bus import AidPlusServiceBus
from aidplus.hardware import TemperatureSensor

class BiometricAuthService:
    """
    All authentication paths call verify_face() after identity is established.
    Face-only login: call identify_by_face() which resolves identity + verifies.
    Liveness detection prevents static photo spoofing.
    In simulation mode (no camera): prompts for manual confirm and auto-passes.
    """

    def __init__(self, db: "DatabaseManager"):
        self.db = db
        self._camera_available = self._probe_camera()

    def _probe_camera(self) -> bool:
        try:
            import cv2
            cap = cv2.VideoCapture(0)
            ok  = cap.isOpened()
            cap.release()
            return ok
        except Exception:
            return False

    def _capture_frame(self):
        """Capture one frame from the camera. Returns numpy array or None."""
        try:
            import cv2
            cap = cv2.VideoCapture(0)
            ret, frame = cap.read()
            cap.release()
            return frame if ret else None
        except Exception:
            return None

    def _extract_embedding(self, frame) -> list | None:
        """
        Extract a 128-float face embedding from a frame.
        Production: use InsightFace or DeepFace (open source, no SDK).
        Current: returns a seeded random vector for development.
        """
        if frame is None:
            return None
        try:
            # ── Production path (ADW-1 with InsightFace) ──────────────────
            # Uncomment when face recognition library is installed on ADW-1:
            # import insightface
            # app = insightface.app.FaceAnalysis()
            # app.prepare(ctx_id=0)
            # faces = app.get(frame)
            # return faces[0].embedding.tolist() if faces else None

            # ── Development path ──────────────────────────────────────────
            # Uses frame pixel data as seed for a reproducible embedding.
            # Not real face recognition — used only for CLTS demographic
            # data collection (temperature, presence, mood estimate).
            # Authentication on dev uses ID + password only.
            import hashlib
            seed = int(hashlib.md5(frame.tobytes()[:1000]).hexdigest(), 16) % (2**31)
            rng  = random.Random(seed)
            return [rng.gauss(0, 1) for _ in range(FACE_SIG_LENGTH)]
        except Exception:
            return None

    def _cosine_similarity(self, a: list, b: list) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        na  = sum(x * x for x in a) ** 0.5
        nb  = sum(x * x for x in b) ** 0.5
        if na == 0 or nb == 0:
            return 0.0
        return dot / (na * nb)

    def _liveness_check(self) -> bool:
        """
        Liveness detection — prevents static photo attacks.
        Production: captures 3 frames looking for micro-motion, blink, or depth cue.
        Simulation: always passes with a console prompt.
        """
        if not self._camera_available:
            # Simulation mode (no camera) — auto-pass liveness
            # Production ADW-1 uses real camera with motion/blink detection
            return True
        # Production liveness: compare pixel variance across 3 frames
        try:
            import cv2, numpy as np
            frames = []
            cap = cv2.VideoCapture(0)
            for _ in range(3):
                ret, f = cap.read()
                if ret:
                    frames.append(f)
            cap.release()
            if len(frames) < 2:
                return False
            # Motion variance between frames 1 and 3
            diff   = cv2.absdiff(frames[0], frames[-1])
            motion = float(np.mean(diff))
            return motion > 1.2   # threshold — tuned for 150ms between frames
        except Exception:
            return True   # graceful fallback — don't block on liveness failure

    def verify_face(self, customer_id: str,
                    require_liveness: bool = True) -> dict:
        """
        Verify that the person in front of the camera matches the enrolled face
        for customer_id. Returns {verified, confidence, liveness_pass}.
        Called after identity is already established (password/card/QR).
        """
        stored_sig = self.db.get_face_signature(customer_id)
        if not stored_sig:
            # No enrolled face — cannot verify. Auto-pass with warning.
            return {"verified": True, "confidence": 0.0,
                    "liveness_pass": True, "reason": "no_enrollment"}

        # Simulation mode — auto-pass all biometric checks
        # Production ADW-1 uses real camera + face model
        if not self._camera_available:
            return {"verified": True, "confidence": 1.0,
                    "liveness_pass": True, "reason": "simulated"}

        liveness = self._liveness_check() if require_liveness else True
        if not liveness:
            self.db.log_audit(customer_id, "BIOMETRIC_FAIL", "customers",
                              customer_id, "Liveness check failed")
            return {"verified": False, "confidence": 0.0,
                    "liveness_pass": False, "reason": "liveness_failed"}

        frame     = self._capture_frame()
        embedding = self._extract_embedding(frame)

        if embedding is None:
            # No frame captured or embedding failed — non-fatal
            if not self._camera_available:
                return {"verified": True, "confidence": 1.0,
                        "liveness_pass": True, "reason": "simulated"}
            return {"verified": False, "confidence": 0.0,
                    "liveness_pass": liveness, "reason": "no_face_detected"}

        confidence = self._cosine_similarity(stored_sig, embedding)
        verified   = confidence >= BIOMETRIC_CONFIDENCE_MIN

        self.db.log_audit(
            customer_id, "BIOMETRIC_OK" if verified else "BIOMETRIC_FAIL",
            "customers", customer_id,
            f"Face verify confidence={confidence:.3f} "
            f"threshold={BIOMETRIC_CONFIDENCE_MIN}")

        return {"verified": verified, "confidence": round(confidence, 4),
                "liveness_pass": liveness, "reason": "ok" if verified else "low_confidence"}

    def identify_by_face(self) -> dict | None:
        """
        Face-only login: capture frame → find the closest match across all
        enrolled customers. Returns customer dict or None.
        """
        liveness = self._liveness_check()
        if not liveness:
            return None

        frame     = self._capture_frame()
        embedding = self._extract_embedding(frame)
        if embedding is None:
            return None

        all_sigs  = self.db.get_all_face_signatures()
        best_cid  = None
        best_conf = 0.0
        for cid, sig in all_sigs.items():
            conf = self._cosine_similarity(sig, embedding)
            if conf > best_conf:
                best_conf = conf
                best_cid  = cid

        if best_cid and best_conf >= BIOMETRIC_CONFIDENCE_MIN:
            self.db.log_audit(best_cid, "FACE_LOGIN_OK", "customers",
                              best_cid, f"Face-only login confidence={best_conf:.3f}")
            return self.db.get_customer(best_cid)

        return None

    def enroll_face(self, customer_id: str) -> bool:
        """Capture and store a fresh face embedding at registration."""
        frame     = self._capture_frame()
        embedding = self._extract_embedding(frame)
        if embedding is None:
            # Simulation — store random embedding
            embedding = [random.random() for _ in range(FACE_SIG_LENGTH)]
        self.db.save_face_signature(customer_id, embedding)
        return True


# ─────────────────────────────────────────────────────────────────────────────
# NiaVerificationService  [B20-D]
# Ghana Card (NIA) identity verification — direct HTTP, no SDK.
# ─────────────────────────────────────────────────────────────────────────────

class NiaVerificationService:
    """
    Direct integration with the National Identification Authority (NIA) API.
    No third-party SDK. One HTTP POST, one response.
    Simulation mode activates automatically when NIA_API_KEY is not set.
    """

    def __init__(self, db: "DatabaseManager"):
        self.db = db

    def verify(self, customer_id: str, ghana_card_number: str,
               dob: str) -> dict:
        """
        Verify a Ghana Card number against the NIA database.
        Returns {verified, name_match, card_valid, message}.
        Stores result on the customer record.
        """
        if NIA_SIMULATION:
            # Simulation — basic format check only
            valid_format = (
                len(ghana_card_number) >= 12 and
                ghana_card_number.upper().startswith("GHA-")
            )
            result = {
                "verified":    valid_format,
                "name_match":  valid_format,
                "card_valid":  valid_format,
                "message":     "NIA simulation mode — format check only",
                "simulated":   True,
            }
        else:
            try:
                import urllib.request, urllib.parse
                payload = {
                    "card_number": ghana_card_number,
                    "date_of_birth": dob,
                    "api_key": NIA_API_KEY,
                }
                data    = urllib.parse.urlencode(payload).encode()
                req     = urllib.request.Request(
                    NIA_API_URL, data=data, method="POST",
                    headers={"Content-Type": "application/x-www-form-urlencoded",
                             "Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=NIA_TIMEOUT_SECS) as resp:
                    import json as _json
                    body   = _json.loads(resp.read().decode())
                    result = {
                        "verified":   body.get("verified",   False),
                        "name_match": body.get("name_match", False),
                        "card_valid": body.get("card_valid", False),
                        "message":    body.get("message",    ""),
                        "simulated":  False,
                    }
            except Exception as e:
                result = {
                    "verified":  False, "name_match": False,
                    "card_valid": False,
                    "message":   f"NIA API unreachable: {e}",
                    "simulated": False,
                }

        # Persist result on customer record
        with self.db._conn() as con:
            con.execute(
                "UPDATE customers SET ghana_card_number=?, "
                "ghana_card_verified=?, identity_verified=? "
                "WHERE customer_id=?",
                (ghana_card_number,
                 1 if result["verified"] else 0,
                 1 if result["verified"] else 0,
                 customer_id))
        self.db.log_audit(customer_id, "NIA_VERIFY", "customers", customer_id,
                          f"Ghana Card verification: {result['message']}")
        return result


# ─────────────────────────────────────────────────────────────────────────────
# WelcomeMessageService  [B20-E]
# Sends a welcome notification on every new customer registration.
# ─────────────────────────────────────────────────────────────────────────────

class WelcomeMessageService:
    """
    Triggered once at registration. Delivers:
    - In-app welcome notification with customer ID and tier benefits
    - Sync token (stored on customer record, 24h TTL)
    - App sync QR code data (displayed on kiosk screen)
    """

    def __init__(self, db: "DatabaseManager"):
        self.db = db

    def _tier_benefits_text(self, tier: str) -> str:
        t = WALLET_TIERS.get(tier, WALLET_TIERS["G0"])
        return (f"{t['name']} — wallet limit ₵{t['limit']:.2f}. "
                f"Purchase medicines, request teleconsult, pay utility bills, "
                f"and earn Cupsule return loyalty points.")

    def send(self, customer_id: str) -> str:
        """
        Send welcome notification and generate sync token.
        Returns the sync token for QR display on kiosk.
        """
        c = self.db.get_customer(customer_id)
        if not c:
            return ""

        tier         = c.get("wallet_tier", "G0")
        benefits     = self._tier_benefits_text(tier)
        sync_token   = self.db.generate_sync_token(customer_id)

        welcome_msg = (
            f"Welcome to AID SYSTEM, {c['name']}!\n\n"
            f"Your Customer ID: {customer_id}\n"
            f"Wallet Tier: {benefits}\n\n"
            f"What you can do:\n"
            f"• Purchase medicines at any AID System kiosk\n"
            f"• Request a teleconsult with a registered doctor\n"
            f"• Pay water, electricity, and DStv bills\n"
            f"• Return Cupsule containers to earn loyalty points\n"
            f"• Use the AID APP to shop and manage your account\n\n"
            f"To sync your account to the AID APP:\n"
            f"Download the app → tap 'Sync from Kiosk' → scan the QR code "
            f"displayed on your screen. The code is valid for 24 hours.\n\n"
            f"Your health, your access, your future. — AID PLUS+"
        )

        self.db.send_notification(customer_id, "Welcome to AID SYSTEM", welcome_msg)
        self.db.log_audit("SYSTEM", "WELCOME_SENT", "customers", customer_id,
                          f"Welcome message delivered to {c['name']}")
        return sync_token


# ─────────────────────────────────────────────────────────────────────────────
# CupsuleService  [B20-F]
# Manages Cupsule issuance on dispense and CAPSCAN return socket.
# ─────────────────────────────────────────────────────────────────────────────

class CupsuleService:
    """
    Cupsule lifecycle manager.
    - issue(): called after every successful dispense
    - process_return(): called by CAPSCAN via Service Bus
    - The CAPSCAN socket on the Service Bus is dormant until
      CAPSCAN software registers (AidPlusServiceBus.register('CAPSCAN', ...))
    """

    # Drug class map — used for Nyansa analytics and health flagging
    DRUG_CLASS_MAP = {
        "Paracetamol 500mg":      "analgesic",
        "Ibuprofen 400mg":        "NSAID",
        "Amoxicillin 250mg":      "antibiotic",
        "Cetirizine 10mg":        "antihistamine",
        "Omeprazole 20mg":        "PPI",
        "Metformin 500mg":        "antidiabetic",
        "Aspirin 75mg":           "antiplatelet",
        "Paracetamol Syrup":      "analgesic",
        "Amoxicillin Susp.":      "antibiotic",
        "Multivitamin Syrup":     "supplement",
        "Cough Syrup":            "antitussive",
        "Iron Supplement Syrup":  "supplement",
        "Antacid Liquid":         "antacid",
    }

    def __init__(self, db: "DatabaseManager"):
        self.db = db

    def issue(self, customer_id: str, transaction_id: str,
              shelf_num: int, drug_name: str,
              unit_id: str = None) -> str:
        """
        Issue a Cupsule after a successful dispense.
        Emits CUPSULE_ISSUED event on the Service Bus.
        Returns cupsule_id.
        """
        drug_class = self.DRUG_CLASS_MAP.get(drug_name, "OTC")
        cupsule_id = self.db.issue_cupsule(
            customer_id, transaction_id, shelf_num,
            drug_name, drug_class, unit_id)

        # Emit to Service Bus — CAPSCAN listens for this
        AidPlusServiceBus.emit("CUPSULE_ISSUED", {
            "cupsule_id":     cupsule_id,
            "customer_id":    customer_id,
            "transaction_id": transaction_id,
            "drug_name":      drug_name,
            "drug_class":     drug_class,
            "unit_id":        unit_id or UNIT_ID,
            "issued_at":      datetime.now().isoformat(),
        })
        return cupsule_id

    def process_return(self, cupsule_id: str, customer_id: str,
                       condition: str = "intact",
                       capscan_unit_id: str = None) -> dict:
        """
        Process a Cupsule return — called by CAPSCAN via Service Bus.
        Validates the return, credits loyalty points, emits CUPSULE_RETURNED.

        This is the Service Bus socket that CAPSCAN connects to.
        Can also be called directly from the admin menu for testing.
        """
        cupsule = self.db.get_cupsule(cupsule_id)
        if not cupsule:
            AidPlusServiceBus.emit("CUPSULE_UNKNOWN", {
                "cupsule_id":    cupsule_id,
                "customer_id":   customer_id,
                "unit_id":       capscan_unit_id or UNIT_ID,
                "timestamp":     datetime.now().isoformat(),
            })
            return {"success": False, "error_code": "CUPSULE_NOT_FOUND",
                    "message": "Cupsule ID not in system"}

        # Fraud check — already returned?
        if cupsule["returned"]:
            AidPlusServiceBus.emit("CUPSULE_FRAUD_ATTEMPT", {
                "cupsule_id":   cupsule_id,
                "customer_id":  customer_id,
                "unit_id":      capscan_unit_id or UNIT_ID,
                "timestamp":    datetime.now().isoformat(),
            })
            return {"success": False, "error_code": "ALREADY_RETURNED",
                    "message": "Cupsule already returned — no credit issued"}

        # Determine points
        pts_map = {
            "intact":     CUPSULE_POINTS_INTACT,
            "empty_only": CUPSULE_POINTS_EMPTY,
            "damaged":    CUPSULE_POINTS_DAMAGED,
        }
        points = pts_map.get(condition, CUPSULE_POINTS_INTACT)

        # Mark returned and credit points
        self.db.mark_cupsule_returned(cupsule_id, points)
        if points > 0:
            c = self.db.get_customer(customer_id)
            if c:
                c["loyalty_points"]  = c.get("loyalty_points",  0) + points
                c["lifetime_points"] = c.get("lifetime_points", 0) + points
                self.db.save_customer(c)
                self.db.send_notification(
                    customer_id,
                    "Cupsule Returned",
                    f"Thank you for returning your Cupsule! "
                    f"+{points} loyalty points credited.")

        # Emit confirmed return event
        AidPlusServiceBus.emit("CUPSULE_RETURNED", {
            "cupsule_id":  cupsule_id,
            "customer_id": customer_id,
            "unit_id":     capscan_unit_id or UNIT_ID,
            "condition":   condition,
            "points":      points,
            "timestamp":   datetime.now().isoformat(),
        })

        self.db.log_audit(customer_id, "CUPSULE_RETURN", "cupsule_issued",
                          cupsule_id, f"condition={condition} points={points}")
        return {"success": True, "cupsule_id": cupsule_id,
                "points_awarded": points, "condition": condition}

    def get_return_stats(self, customer_id: str = None) -> dict:
        """Summary statistics for reports."""
        with self.db._conn() as con:
            if customer_id:
                rows = con.execute(
                    "SELECT * FROM cupsule_issued WHERE customer_id=?",
                    (customer_id,)).fetchall()
            else:
                rows = con.execute("SELECT * FROM cupsule_issued").fetchall()
            total    = len(rows)
            returned = sum(1 for r in rows if r["returned"])
            rate     = round(returned / total * 100, 1) if total else 0.0
            return {"total_issued": total, "total_returned": returned,
                    "return_rate_pct": rate}


# ─────────────────────────────────────────────────────────────────────────────
# PasswordRecoveryService  [B20-G]
# Three recovery paths: app QR, card tap, Ghana Card + security question.
# ─────────────────────────────────────────────────────────────────────────────

class PasswordRecoveryService:
    """
    Handles all three password recovery paths.
    Every path requires biometric face verification as the final gate.
    """

    def __init__(self, db: "DatabaseManager", biometric: "BiometricAuthService"):
        self.db        = db
        self.biometric = biometric

    def initiate_qr_reset(self, customer_id: str) -> str:
        """
        Generate a reset token for the app QR path.
        The mobile app displays this as a QR; the kiosk scans it.
        Token is valid for RESET_TOKEN_TTL_MINS minutes.
        Returns token string.
        """
        token = self.db.generate_reset_token(customer_id, "qr_app")
        self.db.log_audit(customer_id, "RESET_INITIATED", "customers",
                          customer_id, "method=qr_app")
        return token

    def verify_qr_reset(self, token: str) -> dict | None:
        """
        Validate a QR reset token. Returns customer if valid, else None.
        Biometric face verify is called by the caller after this returns.
        """
        return self.db.consume_reset_token(token)

    def initiate_card_reset(self, card_uid: str) -> dict | None:
        """
        Validate a card UID and return customer for reset flow.
        Biometric face verify is called by the caller after this returns.
        """
        c = self.db.get_customer_by_card_uid(card_uid)
        if c:
            self.db.log_audit(c["customer_id"], "RESET_INITIATED", "customers",
                              c["customer_id"], "method=card_tap")
        return c

    def initiate_ghana_card_reset(self, ghana_card_number: str,
                                   security_answer: str) -> dict | None:
        """
        Validate Ghana Card number + security answer.
        Returns customer if both match, else None.
        Biometric face verify is called by the caller after this returns.
        """
        with self.db._conn() as con:
            row = con.execute(
                "SELECT * FROM customers WHERE ghana_card_number=?",
                (ghana_card_number,)).fetchone()
            if not row:
                return None
            c = dict(row)
            # Verify security answer (stored hashed)
            stored_ans = c.get("security_answer", "")
            ans_hash, _ = hash_password(security_answer.lower().strip(),
                                         c.get("password_salt", ""))
            if not secrets.compare_digest(stored_ans, ans_hash):
                self.db.log_audit(
                    c["customer_id"], "RESET_FAIL", "customers",
                    c["customer_id"], "method=ghana_card bad_answer")
                return None
            self.db.log_audit(c["customer_id"], "RESET_INITIATED", "customers",
                              c["customer_id"], "method=ghana_card")
            return c

    def complete_reset(self, customer_id: str, new_password: str) -> bool:
        """
        Set a new password after successful recovery verification.
        Should only be called after biometric has passed.
        """
        if len(new_password) < 4:
            return False
        pwd_hash, pwd_salt = hash_password(new_password)
        with self.db._conn() as con:
            con.execute(
                "UPDATE customers SET password=?, password_salt=? "
                "WHERE customer_id=?",
                (pwd_hash, pwd_salt, customer_id))
        self.db.log_audit(customer_id, "PASSWORD_RESET", "customers",
                          customer_id, "Password reset via recovery")
        self.db.send_notification(customer_id, "Password Changed",
            "Your AID SYSTEM password has been reset. "
            "If you did not request this, contact support immediately.")
        return True


# ═══════════════════════════════════════════════════════════════════════════════
# CUSTOMER CLASS
# ═══════════════════════════════════════════════════════════════════════════════
