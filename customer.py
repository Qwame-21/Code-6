"""
AID PLUS+ — Customer Entity
=============================
Customer: the central stateful object representing a logged-in customer session.
          Manages authentication (all paths), registration, profile, cart state,
          purchase limits, loyalty points, and wallet operations.
check_cart_safety / check_cart_safety_primary: medication interaction screening.
"""
from __future__ import annotations
import json, random, time, secrets
from datetime import datetime, timedelta

from aidplus.config import *
from aidplus.ui import print_header, speak
from aidplus.db import DatabaseManager
from aidplus.security import hash_password, verify_password, secure_id, secure_ref
from aidplus.bus import AidPlusServiceBus
from aidplus.auth import (
    BiometricAuthService, NiaVerificationService,
    WelcomeMessageService, CupsuleService, PasswordRecoveryService,
)

class Customer:
    def __init__(self, db: DatabaseManager):
        self.db      = db
        self.current: dict | None = None

    def save(self):
        if self.current:
            self.db.save_customer(self.current)

    def send_system_notification(self, customer_id: str, subject: str, message: str):
        self.db.send_notification(customer_id, subject, message)

    def get_recent_total_qty(self, hours: int = DAILY_PURCHASE_LIMIT_HOURS) -> int:
        """Display count — subtracts returned items (for Nyansa, profile display)."""
        if not self.current:
            return 0
        txs = self.db.get_customer_transactions(self.current["customer_id"], hours)
        return sum(item["qty"] for t in txs for item in t.get("items", []))

    def get_daily_slot_count(self, hours: int = DAILY_PURCHASE_LIMIT_HOURS) -> int:
        """Slot count — does NOT subtract returns. 24h security rule."""
        if not self.current:
            return 0
        return self.db.get_daily_slot_count(self.current["customer_id"], hours)

    def get_purchase_limit_reset_time(self, hours: int = DAILY_PURCHASE_LIMIT_HOURS) -> str:
        if not self.current:
            return "N/A"
        txs = self.db.get_customer_transactions(self.current["customer_id"], hours)
        if not txs:
            return "Available immediately"
        first_ts = min(datetime.fromisoformat(t["timestamp"]) for t in txs)
        left     = (first_ts + timedelta(hours=hours)) - datetime.now()
        if left.total_seconds() > 0:
            hrs, mins = divmod(int(left.total_seconds() / 60), 60)
            return f"{hrs}h {mins}m until reset"
        return "Limit reset"

    def create_new(self, biometric=None, nia=None, welcome=None):
        """
        [B20] Extended registration flow.
        Collects: identity, health profile, Ghana Card, consent.
        Enrolls face biometric. Sends welcome message with sync QR.
        """
        print_header("CREATE ACCOUNT — AID SYSTEM")
        print("Please have your Ghana Card ready if available.\n")

        # ── Section 1: Personal identity ─────────────────────────────────────
        name = input("Full Name: ").strip()
        if not name:
            print("Name is required."); return None

        dob = input("Date of Birth (YYYY-MM-DD): ").strip()
        try:
            age = (datetime.now() - datetime.strptime(dob, "%Y-%m-%d")).days // 365
            if age < MIN_AGE:
                print(f"Must be {MIN_AGE}+ years old."); return None
        except ValueError:
            print("Invalid date format."); return None

        gender  = input("Gender (M/F/O): ").strip().upper()
        print("  Your address helps coordinate health services in your area.")
        print("  Leave any field blank to fill in later from your profile.\n")
        reg_region   = input("  Region (e.g. Greater Accra): ").strip()
        reg_district = input("  District / Area: ").strip()
        reg_town     = input("  Town / Community: ").strip()
        reg_street   = input("  Street / Landmark: ").strip()
        reg_gps      = input("  Ghana Post GPS code (optional): ").strip()
        addr_parts   = [p for p in [reg_street, reg_town,
                                     reg_district, reg_region] if p]
        if reg_gps: addr_parts.append(f"GPS:{reg_gps}")
        address = ", ".join(addr_parts) if addr_parts else ""
        contact = input("Contact phone (required for recovery): ").strip()
        email   = input("Email (optional): ").strip()

        # ── Section 2: Security ───────────────────────────────────────────────
        print("\n--- Account Security ---")
        password = input("Create password (min 4 chars): ").strip()
        if len(password) < 4:
            print("Password too short."); return None

        sec_q = input("Security question (e.g. Mother\'s maiden name?): ").strip()
        sec_a = input("Answer: ").strip().lower()
        if not sec_q or not sec_a:
            print("Security question and answer are required for account recovery.")
            return None

        # ── Section 3: Ghana Card ─────────────────────────────────────────────
        print("\n--- Identity Verification ---")
        has_card = input("Do you have your Ghana Card? (y/n): ").lower().strip() == 'y'
        ghana_card_number = ""
        if has_card:
            ghana_card_number = input("Ghana Card Number (GHA-XXXXXXXXX-X): ").strip()

        # ── Section 4: NHIS ───────────────────────────────────────────────────
        print("\n--- NHIS ---")
        has_nhis = input("Active NHIS card? (y/n): ").lower().strip() == 'y'
        nhis_num = input("Enter NHIS number: ").strip() if has_nhis else ""

        # ── Section 5: Health profile ─────────────────────────────────────────
        print("\n--- Health Profile ---")
        blood_group = input(
            "Blood Group (A+/A-/B+/B-/O+/O-/AB+/AB- or skip): ").strip().upper()

        print("Allergies — blank line to finish:")
        allergies = []
        while True:
            a = input("  Allergy: ").strip()
            if not a: break
            allergies.append(a)

        print("Chronic conditions — blank line to finish:")
        conditions = []
        while True:
            c_in = input("  Condition: ").strip()
            if not c_in: break
            conditions.append(c_in)

        print("Current medications — blank line to finish:")
        medications = []
        while True:
            m = input("  Medication: ").strip()
            if not m: break
            medications.append(m)

        health_info_parts = []
        if allergies:    health_info_parts.append("Allergies: " + ", ".join(allergies))
        if conditions:   health_info_parts.append("Conditions: " + ", ".join(conditions))
        if medications:  health_info_parts.append("Meds: " + ", ".join(medications))
        health_info = " | ".join(health_info_parts)

        # ── Section 6: Consent ────────────────────────────────────────────────
        print("\n--- Data Consent ---")
        print("AID SYSTEM collects health data to improve your recommendations.")
        print("Anonymised data supports population health analysis with the Ghana MoH.")
        consent = input("I consent to the above (y/n): ").lower().strip() == 'y'
        if not consent:
            print("Consent is required to create an account."); return None

        # ── Step 7: Generate customer ID + face signature ────────────────────
        # Face enrollment happens AFTER customer row is committed to avoid FK error
        customer_id = secure_id(8)
        face_sig = [random.random() for _ in range(FACE_SIG_LENGTH)]  # placeholder

        # ── Step 8: Hash security answer ──────────────────────────────────────
        sec_hash, _ = hash_password(sec_a)

        # ── Step 9: Create account ─────────────────────────────────────────────
        data = {
            "customer_id":         customer_id,
            "name":                name,
            "dob":                 dob,
            "age":                 age,
            "gender":              gender,
            "address":             address,
            "password":            password,
            "email":               email,
            "contact":             contact,
            "health_info":         health_info,
            "nhis_active":         has_nhis,
            "nhis_number":         nhis_num,
            "face_signature":      face_sig,
            "blood_group":         blood_group,
            "allergies":           str(allergies),
            "chronic_conditions":  str(conditions),
            "current_medications": str(medications),
            "ghana_card_number":   ghana_card_number,
            "security_question":   sec_q,
            "security_answer":     sec_hash,
            "health_consent":      1,
        }
        self.db.create_customer(data)
        self.current = self.db.get_customer(customer_id)

        # ── Step 9b: Face enrollment (after customer row committed) ───────────
        print("\n--- Face Enrollment ---")
        print("Please look directly at the camera.")
        if biometric:
            try:
                biometric.enroll_face(customer_id)
            except Exception:
                pass   # face enrollment failure is non-fatal — customer can re-enroll later from profile

        # ── Step 10: NIA verification ─────────────────────────────────────────
        if ghana_card_number and nia:
            print("\nVerifying Ghana Card with NIA…")
            r = nia.verify(customer_id, ghana_card_number, dob)
            if r["verified"]:
                print("✅ Identity verified via Ghana Card.")
            else:
                print(f"⚠  NIA: {r['message']}. You can verify later from profile.")

        # ── Step 11: Welcome + sync QR ────────────────────────────────────────
        sync_token = welcome.send(customer_id) if welcome else \
                     self.db.generate_sync_token(customer_id)

        print(f"\n✅ Account created!")
        print(f"   Customer ID : {customer_id}")
        print(f"   Wallet Tier : {WALLET_TIERS['G0']['name']} "
              f"(Limit: ₵{WALLET_TIERS['G0']['limit']:.2f})")
        if sync_token:
            print(f"\n   ┌── SYNC TO AID APP ────────────────────────────────┐")
            print(f"   │  Download the AID APP → tap \'Sync from Kiosk\'    │")
            print(f"   │  Sync token: {sync_token[:28]}  │")
            print(f"   │  Valid 24 hours.                                   │")
            print(f"   └───────────────────────────────────────────────────┘")

        AidPlusServiceBus.emit("CUSTOMER_REGISTERED", {
            "customer_id": customer_id,
            "name":        name,
            "timestamp":   datetime.now().isoformat(),
            "unit_id":     UNIT_ID,
        })
        return self.current
    def login(self, biometric: "BiometricAuthService" = None,
              recovery: "PasswordRecoveryService" = None) -> str | None:
        """
        [B20] All login paths require biometric face verify as second factor.
        Paths: [1] ID + password  [2] Face only  [3] QR token  [4] Card tap
        Password recovery initiated from this menu when customer is locked out.
        """
        os.system('cls' if os.name == 'nt' else 'clear')
        W = 62
        print(f"\n╔{'═'*W}╗")
        print(f"║{'AID PLUS+  ·  Nyansa v8.0  ·  ADW-1'.center(W)}║")
        print(f"╠{'═'*W}╣")
        print(f"║{'SYSTEM LOGIN'.center(W)}║")
        print(f"╠{'═'*W}╣")
        print(f"║{'':62}║")
        print(f"║  {'[1]  Customer ID + Password'.ljust(W-2)}║")
        print(f"║  {'[2]  Face Recognition'.ljust(W-2)}║")
        print(f"║  {'[3]  Scan App QR Code'.ljust(W-2)}║")
        print(f"║  {'[4]  Tap Membership Card'.ljust(W-2)}║")
        print(f"║{'':62}║")
        print(f"╠{'═'*W}╣")
        print(f"║  {'[R]  Forgot Password / Account Recovery'.ljust(W-2)}║")
        print(f"║  {'[A]  Admin Login'.ljust(W-2)}║")
        print(f"║  {'[0]  Back'.ljust(W-2)}║")
        print(f"╚{'═'*W}╝")
        print(f"  Tip: Your Customer ID is the 8-digit number shown when you registered.")
        path = input("  › ").strip().upper()
        if path == '0': return None

        # ── Admin path ──────────────────────────────────────────────────────
        if path == "A":
            candidate = input("Admin Password: ").strip()
            if secrets.compare_digest(candidate, ADMIN_PASSWORD):
                print("\n--- SECONDARY AUTHENTICATION ---")
                if input("Time-Sensitive Code (Prototype: 9999): ").strip() == "9999":
                    # Biometric verify for admin too
                    if biometric:
                        print("Admin face verify…")
                        result = biometric._liveness_check()
                        if not result:
                            print("Liveness check failed."); return None
                    self.db.log_audit("ADMIN", "LOGIN_OK", detail="Admin login B20")
                    print("Admin login successful.")
                    return "admin"
                self.db.log_audit("ADMIN", "LOGIN_FAIL", detail="Secondary auth failed")
                print("Secondary authentication failed.")
            else:
                self.db.log_audit("ADMIN", "LOGIN_FAIL", detail="Bad admin password")
                print("Incorrect password.")
            return None

        # ── Face-only login [B20] ────────────────────────────────────────────
        if path == "2":
            # Face-only login requires the ADW-1 production hardware with
            # the InsightFace model installed. Not available in dev mode.
            # On production: biometric.identify_by_face() does real matching.
            print()
            print("  Face recognition login is available on the")
            print("  AID PLUS+ kiosk terminal (ADW-1).")
            print()
            print("  On this terminal please use:")
            print("  [1] Customer ID + Password")
            input("  Press Enter to return to login menu…")
            return None

        # ── QR token login [B20] ─────────────────────────────────────────────
        if path == "3":
            token = input("Enter QR token from your app: ").strip()
            c = self.db.consume_sync_token(token)
            if not c:
                # Try as password reset token
                c = self.db.consume_reset_token(token) if recovery else None
            if not c:
                print("Invalid or expired QR token."); return None
            # Biometric second factor required
            if biometric and not HW_SIMULATION_MODE:
                print("QR verified. Face confirmation required…")
                bio = biometric.verify_face(c["customer_id"])
                if not bio["verified"]:
                    print("Face verification failed. Access denied.")
                    self.db.log_audit(c["customer_id"], "LOGIN_FAIL",
                                      "customers", c["customer_id"],
                                      "QR+face: biometric failed")
                    return None
            self.current = c
            cid = c["customer_id"]
            self.db.log_audit(cid, "LOGIN_OK", "customers", cid,
                              "QR+biometric login")
            print(f"Welcome back, {c['name']}!")
            return "customer"

        # ── Card tap login [B20] ─────────────────────────────────────────────
        if path == "4":
            card_uid = input("Tap card or enter Card UID: ").strip()
            c = self.db.get_customer_by_card_uid(card_uid)
            if not c:
                print("Card not recognised."); return None
            if c.get("status") != "Active":
                print(f"Account is {c.get('status','Suspended')}. Contact support.")
                return None
            # Biometric second factor required
            if biometric and not HW_SIMULATION_MODE:
                print("Card verified. Face confirmation required…")
                bio = biometric.verify_face(c["customer_id"])
                if not bio["verified"]:
                    print("Face verification failed. Access denied.")
                    self.db.log_audit(c["customer_id"], "LOGIN_FAIL",
                                      "customers", c["customer_id"],
                                      "Card+face: biometric failed")
                    return None
            self.current = c
            cid = c["customer_id"]
            self.db.log_audit(cid, "LOGIN_OK", "customers", cid,
                              "Card+biometric login")
            print(f"Welcome back, {c['name']}!")
            return "customer"

        # ── Password recovery [B20] ──────────────────────────────────────────
        if path == "R":
            if not recovery:
                print("Recovery service unavailable. Contact support."); return None
            print("\nAccount Recovery")
            print(" [1] Use app QR code")
            print(" [2] Use membership card")
            print(" [3] Use Ghana Card + security question")
            rpath = input("> ").strip()

            c = None
            if rpath == "1":
                token = input("Enter reset token from your app: ").strip()
                c = recovery.verify_qr_reset(token)
            elif rpath == "2":
                uid = input("Tap card or enter Card UID: ").strip()
                c   = recovery.initiate_card_reset(uid)
            elif rpath == "3":
                gcard = input("Ghana Card Number: ").strip()
                ans   = input("Security answer: ").strip()
                c     = recovery.initiate_ghana_card_reset(gcard, ans)

            if not c:
                print("Recovery failed. Identity could not be verified.")
                return None

            # Biometric face verify is mandatory for all recovery paths
            if biometric and not HW_SIMULATION_MODE:
                print("Identity confirmed. Face verification required…")
                bio = biometric.verify_face(c["customer_id"])
                if not bio["verified"]:
                    print("Face verification failed. Recovery denied.")
                    return None

            new_pwd = input(f"Set new password for {c['name']}: ").strip()
            if recovery.complete_reset(c["customer_id"], new_pwd):
                print("✅ Password reset successfully. Please log in again.")
            else:
                print("Password reset failed — too short.")
            return None   # Redirect to fresh login after reset

        # ── Standard ID + password path ──────────────────────────────────────
        cid = input("Customer ID: ").strip()
        c   = self.db.get_customer(cid)
        if not c:
            self.db.log_audit("SYSTEM", "LOGIN_FAIL", record_id=cid,
                              detail="Customer ID not found")
            print(f"  ✗ Customer ID '{cid}' not found.")
            print("  Check the 8-digit number shown on your registration screen.")
            input("  Press Enter…")
            return None

        # Lockout check
        if c.get("lockout_until"):
            try:
                lock_time = datetime.fromisoformat(c["lockout_until"])
                if lock_time > datetime.now():
                    left = lock_time - datetime.now()
                    print(f"ACCOUNT LOCKED. Retry in: {str(left).split('.')[0]}.")
                    return None
                c["lockout_until"]  = None
                c["login_attempts"] = 0
                c["status"]         = "Active"
                self.db.save_customer(c)
                self.db.send_notification(cid, "Account Recovery",
                    "Your 2-hour lock has expired. Access restored.")
            except ValueError:
                c["lockout_until"] = None

        if c.get("status") != "Active":
            print(f"Account is {c.get('status','Suspended')}. Contact support.")
            return None

        for attempt in range(1, 4):
            if attempt == 3:
                print("\n!! FINAL ATTEMPT — account locks 2h on failure !!")
                t0  = time.time()
                pwd = input("Password (20 seconds): ").strip()
                if time.time() - t0 > 20:
                    pwd = "TIMEOUT"
            else:
                pwd = input("Password: ").strip()

            if self.db.verify_customer_password(cid, pwd):
                # [B20] Biometric second factor
                # Production: mandatory camera verification
                # Simulation: skipped — password alone is sufficient for dev testing
                if biometric and not HW_SIMULATION_MODE:
                    print("Password verified. Face confirmation required…")
                    bio = biometric.verify_face(cid)
                    if not bio["verified"]:
                        print("Face verification failed. Access denied.")
                        self.db.log_audit(cid, "LOGIN_FAIL", "customers", cid,
                                          "Password OK but biometric failed")
                        return None

                c["login_attempts"] = 0
                self.db.save_customer(c)
                self.current = c
                self.db.log_audit(cid, "LOGIN_OK", "customers", cid,
                                  f"Password+biometric login: {c['name']}")
                print(f"Welcome back, {c['name']}!")
                return "customer"

            self.db.log_audit(cid, "LOGIN_FAIL", "customers", cid,
                              f"Attempt {attempt}/3")
            remaining = 3 - attempt
            if remaining > 0:
                print(f"  ✗ Incorrect password. {remaining} attempt(s) remaining.")
            else:
                print("  ✗ Incorrect password.")

        # Three failures — lock account
        c["lockout_until"]  = (datetime.now() + timedelta(hours=2)).isoformat()
        c["status"]         = "Suspended"
        c["login_attempts"] = 0
        self.db.save_customer(c)
        self.db.log_audit(cid, "LOCKOUT", "customers", cid,
                          "Account locked after 3 failed attempts")
        print("Account locked for 2 hours.")
        return None

    def add_bonus(self, amount: float):
        self.current["bonus"] = self.current.get("bonus", 0.0) + amount
        self.save()

    def record_wallet_transaction(self, trans_type: str, amount: float,
                                   description: str = "", trans_id: str = ""):
        self.db.add_wallet_entry(
            self.current["customer_id"], trans_type, amount, description, trans_id
        )

    def record_purchase(self, items: list, total: float, badge: str):
        nhis_savings = sum(
            i["qty"] * (i.get("base_price", i["price_per"]) - i["price_per"])
            for i in items
            if i.get("nhis_discounted") or i.get("nhis_discounted_primary")
        )
        trans_id = self.db.record_transaction(
            self.current["customer_id"], items, total, badge, nhis_savings
        )
        # Loyalty points are earned ONLY from Cupsule returns via CUPSCAN.
        # Drug purchases earn bonus cashback (5%) — not loyalty points.
        self.save()
        desc = "; ".join(f"{i['name']} x{i['qty']}" for i in items)
        self.record_wallet_transaction("purchase", -total,
                                       f"Drug purchase: {desc}", trans_id)
        return trans_id  # returned so caller can display on receipt

    def verify_nhis(self) -> tuple:
        num = self.current.get("nhis_number", "").strip()
        if not num or len(num) != 10 or not num.isdigit():
            return False, "Invalid NHIS number format."
        # Simulation: always verify if number is correctly formatted
        self.current["nhis_last_verified"]  = datetime.now().isoformat()
        self.current["nhis_active"]         = True
        self.current["nhis_session_active"] = True
        self.save()
        return True, f"NHIS {num} verified. 40% discount active for this session."

    def redeem_loyalty_points(self, amount: int = None) -> tuple:
        """
        Convert loyalty points to bonus credit.
        10 points = ₵1.00 bonus, redeemable on drugs, ride tickets, AidPlus products.
        Minimum 10 points required to redeem.
        amount: number of points to redeem (default = all in multiples of 10)
        """
        pts = self.current.get("loyalty_points", 0)
        if pts < LOYALTY_REDEEM_THRESHOLD:
            return False, f"Need at least {LOYALTY_REDEEM_THRESHOLD} points to redeem. You have {pts}."
        if amount:
            # Redeem specific amount — must be multiple of 10
            amount = (amount // LOYALTY_POINTS_PER_REDEEM) * LOYALTY_POINTS_PER_REDEEM
            amount = min(amount, (pts // LOYALTY_POINTS_PER_REDEEM) * LOYALTY_POINTS_PER_REDEEM)
        else:
            amount = (pts // LOYALTY_POINTS_PER_REDEEM) * LOYALTY_POINTS_PER_REDEEM
        if amount == 0:
            return False, "Not enough points."
        value = (amount // LOYALTY_POINTS_PER_REDEEM) * LOYALTY_BONUS_PER_REDEEM
        self.current["loyalty_points"] -= amount
        self.current["bonus"]           = self.current.get("bonus", 0.0) + value
        self.save()
        self.db.add_wallet_entry(
            self.current["customer_id"], "loyalty_redeem",
            value, f"Redeemed {amount} points for ₵{value:.2f} bonus"
        )
        msg = (f"✅ {amount} points redeemed — ₵{value:.2f} bonus credit added.\n"
               f"   Bonus is redeemable on drug purchases, ride tickets,\n"
               f"   and AidPlus products.")
        print(f"\n{msg}")
        speak(f"Redeemed {amount} points for {value:.2f} cedis bonus.", self.current)
        return True, msg

    def award_cupsule_points(self, qty: int, condition: str = "intact") -> int:
        """
        Award loyalty points for Cupsule return via CUPSCAN.
        condition: 'intact' | 'empty' | 'damaged'
        Returns points awarded.
        """
        pts_map = {
            "intact":  CUPSULE_POINTS_INTACT,
            "empty":   CUPSULE_POINTS_EMPTY,
            "damaged": CUPSULE_POINTS_DAMAGED,
        }
        pts = pts_map.get(condition, 0) * qty
        if pts <= 0:
            return 0
        self.current["loyalty_points"]  = self.current.get("loyalty_points",  0) + pts
        self.current["lifetime_points"] = self.current.get("lifetime_points", 0) + pts
        self.save()
        self.db.add_wallet_entry(
            self.current["customer_id"], "loyalty_earn",
            0.0, f"Cupsule return: {qty}x {condition} = +{pts} pts"
        )
        total = self.current['loyalty_points']
        until_next = LOYALTY_REDEEM_THRESHOLD - (total % LOYALTY_REDEEM_THRESHOLD)
        msg = f"🌟 +{pts} point{'s' if pts > 1 else ''}! Total: {total}"
        if until_next < LOYALTY_REDEEM_THRESHOLD:
            msg += f" ({until_next} more until ₵{LOYALTY_BONUS_PER_REDEEM:.2f} bonus)"
        print(msg)
        speak(f"You earned {pts} loyalty point{'s' if pts > 1 else ''}.", self.current)
        return pts

    def nyansa_health_partner(self, drug_name: str) -> bool:
        print_header("Nyansa HEALTH PARTNER")
        h = self.current.get("health_info", "")
        if h and drug_name.split()[0].lower() in h.lower():
            print(f"⚠️  ALERT: Potential allergy to {drug_name} detected!")
            return False
        advice = {
            "Paracetamol": "Take with water. Avoid alcohol.",
            "Amoxicillin": "CRITICAL: Finish the entire course even if you feel better!",
            "Ibuprofen":   "Best taken after a meal to protect your stomach.",
        }
        key = next((k for k in advice if k in drug_name), None)
        print(f"Nyansa says: {advice.get(key, 'Stay hydrated and rest.')}")
        self.db.send_notification(
            self.current["customer_id"], "Nyansa Partner",
            f"Dose cycle started for {drug_name}."
        )
        return True

    def submit_feedback(self, message: str):
        self.db.add_feedback(
            self.current["customer_id"], self.current["name"],
            "[AID SYSTEM]", message
        )
        print("Feedback submitted successfully.")


# ═══════════════════════════════════════════════════════════════════════════════
# SAFETY CHECKS
# ═══════════════════════════════════════════════════════════════════════════════
def check_cart_safety(cart: list, health_info: str) -> list:
    warnings, names = [], [i["name"] for i in cart]
    h = (health_info or "").lower()
    if any("Amoxicillin" in n for n in names) and "penicillin" in h:
        warnings.append("ALLERGY ALERT: Amoxicillin detected — profile lists Penicillin allergy.")
    if any("Aspirin" in n for n in names) and "aspirin" in h:
        warnings.append("ALLERGY ALERT: Aspirin detected — profile lists Aspirin allergy.")
    if any("Ibuprofen" in n for n in names) and any("Aspirin" in n for n in names):
        warnings.append("INTERACTION: Ibuprofen + Aspirin increases gastric bleeding risk.")
    if any("Metformin" in n for n in names) and any(
        "syrup" in n.lower() or "suspension" in n.lower() for n in names
    ):
        warnings.append("DIET NOTE: Syrups may contain sugar impacting Metformin effectiveness.")
    if any("Cetirizine" in n for n in names):
        warnings.append("CAUTION: Cetirizine may cause drowsiness.")
    if any("Ibuprofen" in n for n in names) and ("ulcer" in h or "stomach" in h):
        warnings.append("CAUTION: Ibuprofen — use with care given stomach sensitivity.")
    return warnings

def check_cart_safety_primary(cart: list, health_info: str) -> list:
    warnings, names = [], [i["name"] for i in cart]
    h = (health_info or "").lower()
    if "penicillin" in h and any("Amoxicillin" in n for n in names):
        warnings.append("Amoxicillin reaction risk (penicillin allergy on profile)")
    if "aspirin" in h and any("Ibuprofen" in n for n in names):
        warnings.append("Ibuprofen risk (aspirin allergy on profile)")
    if any("Ibuprofen" in n for n in names) and any("Aspirin 75mg" in n for n in names):
        warnings.append("Combining NSAIDs (Ibuprofen + Aspirin) — increased bleeding risk")
    return warnings


# ═══════════════════════════════════════════════════════════════════════════════
# CLTS CAMERA
# ═══════════════════════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════════
# CLTS — Camera · Light · Thermometer · Speaker
# Physical unit mounted at the top front face of the ADW-1 kiosk.
#
# Responsibilities:
#   CAMERA      → Motion detection, face detection, face identity confirmation
#                 Links detected face to registered customer account (2FA)
#                 Collects anonymised demographic data for Nyansa analytics
#   LIGHT       → LED ring illumination — adapts brightness to ambient conditions
#                 Ensures camera accuracy in dim / evening / overcast conditions
#   THERMOMETER → MLX90614 IR sensor — non-contact body temperature screening
#                 Logs every reading; flags fever for health advisory
#   SPEAKER     → TTS voice output — greets customer, gives guidance, health alerts
#                 Future: live AI voice interaction with Nyansa
#
# Flow:
#   1. PIR detects motion → LED activates
#   2. Camera opens → face detection begins
#   3. Face detected → thermometer triggered
#   4. Speaker greets customer
#   5. If account exists → face matched against enrolled signature (2FA)
#   6. Session logged to clts_session_log
# ═══════════════════════════════════════════════════════════════════════════════

