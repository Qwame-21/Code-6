"""
AID PLUS+ — Nyansa Intelligence Engine
=========================================
NyansaEngine: demand prediction, fraud scoring, CUPSCAN pattern analysis,
              reorder recommendations, personalised welcome messages.
SystemSelfTest: boot-time self-test covering GPIO, DB, connectivity, power,
                Nyansa, and Service Bus subsystems.
admin_csv_export_menu: admin UI for CSV report export.
DRUG_ADVICE: Nyansa drug knowledge base (10 OTC drugs with clinical guidance).
_nyansa_drug_lookup: fuzzy drug name matching against DRUG_ADVICE.
"""
from __future__ import annotations
import json, os, csv, random
from datetime import datetime, timedelta

from aidplus.config import *
from aidplus.db import DatabaseManager
from aidplus.bus import AidPlusServiceBus

class NyansaEngine:
    """
    B28: Nyansa v8.0 — AI Intelligence Engine for AID PLUS+.

    Nyansa is NOT the OS. It is an AI service that runs on top of Aid Plus OS,
    used by products to make intelligent decisions.

    Capabilities:
        health_insights()     — anonymised population health trend analysis
        fraud_score()         — CUPSCAN return fraud probability (0.0–1.0)
        recommend_reorder()   — predict when a customer needs to reorder
        personalise_welcome() — context-aware greeting based on history
        cupscan_pattern()     — detect unusual return patterns per customer
        log_signal()          — feed anonymised signal to Nyansa DB for learning
    """

    ENGINE_VERSION = "Nyansa v8.0"

    def __init__(self, db: 'DatabaseManager'):
        self._db = db

    # ── Health insights ────────────────────────────────────────────────────────
    def health_insights(self, district: str = None) -> list:
        """
        Return population-level health insights from anonymised purchase data.
        Never touches PII — aggregates by drug class, age bracket, district.
        """
        with self._db._conn() as con:
            rows = con.execute(
                """
                SELECT i.name, COUNT(*) as purchase_count,
                       strftime('%Y-%W', t.timestamp) as week
                FROM transactions t
                JOIN inventory i ON t.item_id = i.id
                WHERE 1=1
                  AND t.timestamp >= date('now', '-30 days')
                GROUP BY i.name, week
                ORDER BY purchase_count DESC
                LIMIT 10
                """).fetchall()
        insights = []
        for name, count, week in rows:
            insights.append({
                "item": name, "purchases_30d": count,
                "week": week, "trend": "high" if count > 5 else "normal"
            })
        return insights

    # ── Fraud scoring ──────────────────────────────────────────────────────────
    def fraud_score(self, customer_id: str) -> float:
        """
        Score CUPSCAN return fraud risk for a customer (0.0 = clean, 1.0 = high risk).
        Factors: return rate vs purchase rate, counterfeit events, account age.
        """
        with self._db._conn() as con:
            # Returns in last 7 days
            returns = con.execute(
                "SELECT COUNT(*) FROM cupscan_returns WHERE customer_id=? "
                "AND returned_at >= date('now','-7 days')",
                (customer_id,)).fetchone()[0]
            # Purchases in last 7 days
            purchases = con.execute(
                "SELECT COUNT(*) FROM transactions WHERE customer_id=? "
                "AND type='PURCHASE' AND created_at >= date('now','-7 days')",
                (customer_id,)).fetchone()[0]
            # Counterfeit events ever
            counterfeits = con.execute(
                "SELECT COUNT(*) FROM pdms_audit_log WHERE customer_id=? "
                "AND action='CUPSCAN_COUNTERFEIT'",
                (customer_id,)).fetchone()[0]

        score = 0.0
        if counterfeits > 0:
            score += min(0.5 * counterfeits, 0.8)
        if purchases == 0 and returns > 2:
            score += 0.4   # returning cups never purchased
        elif purchases > 0 and returns > purchases * 3:
            score += 0.3   # returning far more than purchased
        return min(score, 1.0)

    # ── Reorder recommendation ─────────────────────────────────────────────────
    def recommend_reorder(self, customer_id: str) -> list:
        """
        Return list of items the customer is likely due to reorder.
        Based on purchase interval pattern from transaction history.
        """
        with self._db._conn() as con:
            rows = con.execute(
                """
                SELECT i.name, MAX(t.timestamp) as last_purchase,
                       COUNT(*) as total_purchases
                FROM transactions t
                JOIN transaction_items ti ON ti.transaction_id = t.id
                JOIN inventory i ON ti.shelf_num = i.shelf
                WHERE t.customer_id = ?
                GROUP BY i.drug_id
                HAVING total_purchases >= 2
                ORDER BY last_purchase ASC
                LIMIT 5
                """, (customer_id,)).fetchall()
        recommendations = []
        for name, last_dt, count in rows:
            try:
                from datetime import datetime as dt2
                last = dt2.fromisoformat(last_dt)
                days_ago = (datetime.now() - last).days
                if days_ago >= 14:
                    recommendations.append({
                        "item": name,
                        "days_since_last": days_ago,
                        "purchases": count,
                        "urgency": "high" if days_ago >= 30 else "normal"
                    })
            except Exception:
                pass
        return recommendations

    # ── Personalised welcome ───────────────────────────────────────────────────
    def personalise_welcome(self, customer: dict) -> str:
        """
        Generate a context-aware welcome message for returning customers.
        Based on: time of day, purchase history, loyalty tier, streak.
        """
        name   = customer.get("name", "").split()[0] if customer.get("name") else "there"
        tier   = customer.get("tier", "G0")
        points = customer.get("loyalty_points", 0)
        hour   = datetime.now().hour

        greeting = ("Good morning" if hour < 12
                    else "Good afternoon" if hour < 17
                    else "Good evening")

        tier_note = ""
        if tier in ("G2", "G3", "G3+plus"):
            tier_note = f" Welcome back, {tier} member."
        if points > 500:
            tier_note += f" You have {points} loyalty points."

        reorders = self.recommend_reorder(customer.get("id", ""))
        reorder_note = ""
        if reorders:
            top = reorders[0]["item"]
            reorder_note = f" It may be time to restock your {top}."

        return f"{greeting}, {name}.{tier_note}{reorder_note}"

    # ── CUPSCAN pattern analysis ───────────────────────────────────────────────
    def cupscan_pattern(self, customer_id: str) -> dict:
        """
        Analyse CUPSCAN return patterns for anomaly detection.
        Returns summary with risk flags.
        """
        score = self.fraud_score(customer_id)
        with self._db._conn() as con:
            total_returns = con.execute(
                "SELECT COUNT(*) FROM cupscan_returns WHERE customer_id=?",
                (customer_id,)).fetchone()[0]
            reject_events = con.execute(
                "SELECT COUNT(*) FROM pdms_audit_log WHERE customer_id=? "
                "AND action IN ('CUPSCAN_REJECTED','CUPSCAN_COUNTERFEIT')",
                (customer_id,)).fetchone()[0]
        return {
            "customer_id":   customer_id,
            "total_returns": total_returns,
            "reject_events": reject_events,
            "fraud_score":   round(score, 2),
            "risk_level":    "high" if score >= 0.6 else
                             "medium" if score >= 0.3 else "low",
        }

    # ── Signal logging ─────────────────────────────────────────────────────────
    def log_signal(self, signal_type: str, value: str,
                   age_bracket: str = "U", gender_cat: str = "U",
                   district: str = "unknown") -> None:
        """
        Log anonymised signal to nyansa_insights for learning.
        No PII — customer_id never stored here.
        """
        with self._db._conn() as con:
            con.execute(
                "INSERT INTO nyansa_insights "
                "(insight_type, insight_data, status, created_at) "
                "VALUES (?,?,?,?)",
                (signal_type,
                 f"value={value} age={age_bracket} gender={gender_cat} "
                 f"district={district}",
                 "active", datetime.now().isoformat()))

    def engine_status(self) -> str:
        """One-line status for display."""
        with self._db._conn() as con:
            signals = con.execute(
                "SELECT COUNT(*) FROM nyansa_insights").fetchone()[0]
        return f"{self.ENGINE_VERSION}  |  {signals} signals"


# ─────────────────────────────────────────────────────────────────────────────
# B28: SystemSelfTest — Boot integrity check
# ─────────────────────────────────────────────────────────────────────────────

class SystemSelfTest:
    """
    B28: Self-test on every boot.
    Checks DB integrity, GPIO availability, connectivity,
    power system, Nyansa engine, Service Bus.
    Each subsystem returns PASS / WARN / FAIL.
    All results logged to audit. Fatal failures halt boot.
    """

    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"

    def __init__(self, db: 'DatabaseManager'):
        self._db = db
        self.results: dict = {}

    def run(self, os_layer,
            nyansa: NyansaEngine) -> bool:
        """
        Run all self-tests. Returns True if no FAIL results.
        Prints a formatted report.
        """
        print("\n  ── Aid Plus OS Self-Test ─────────────────────────────")
        self._test_db()
        self._test_gpio()
        self._test_connectivity(os_layer)
        self._test_power(os_layer)
        self._test_nyansa(nyansa)
        self._test_service_bus()
        self._print_report()
        self._log_results()
        return self.FAIL not in self.results.values()

    def _test_db(self) -> None:
        try:
            with self._db._conn() as con:
                tables = con.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
                ).fetchone()[0]
            self.results["Database"] = (self.PASS if tables >= 10
                                        else self.WARN)
        except Exception as e:
            self.results["Database"] = self.FAIL
            self.results["Database_error"] = str(e)[:80]

    def _test_gpio(self) -> None:
        if HW_SIMULATION_MODE:
            self.results["GPIO"] = self.WARN   # warn, not fail — simulation is valid
            return
        try:
            import RPi.GPIO
            self.results["GPIO"] = self.PASS
        except ImportError:
            self.results["GPIO"] = self.WARN   # non-Pi environment

    def _test_connectivity(self, os_layer: AidPlusOS) -> None:
        if os_layer.connectivity:
            state = os_layer.connectivity.current
            self.results["Connectivity"] = (
                self.PASS if state != CONN_OFFLINE else self.WARN)
            self.results["Conn_method"] = state
        else:
            self.results["Connectivity"] = self.FAIL

    def _test_power(self, os_layer: AidPlusOS) -> None:
        if os_layer.power:
            pct = os_layer.power.battery_pct
            src = os_layer.power.source
            if os_layer.power.is_critical:
                self.results["Power"] = self.FAIL
            elif os_layer.power.is_low:
                self.results["Power"] = self.WARN
            else:
                self.results["Power"] = self.PASS
            self.results["Power_detail"] = f"{src} {pct:.0f}%"
        else:
            self.results["Power"] = self.WARN

    def _test_nyansa(self, nyansa: NyansaEngine) -> None:
        try:
            nyansa.health_insights()   # smoke test — should not raise
            self.results["Nyansa"] = self.PASS
            self.results["Nyansa_version"] = nyansa.ENGINE_VERSION
        except Exception as e:
            self.results["Nyansa"] = self.WARN
            self.results["Nyansa_error"] = str(e)[:80]

    def _test_service_bus(self) -> None:
        try:
            # CAPSCAN is always registered (co-located CUPSCANModule)
            # BTM and Aid Air are dormant sockets — that is expected
            registered = list(AidPlusServiceBus._registry.keys())
            self.results["ServiceBus"] = self.PASS
            self.results["Bus_registered"] = (", ".join(registered)
                                               if registered else "none yet")
        except Exception as e:
            self.results["ServiceBus"] = self.WARN

    def _print_report(self) -> None:
        icons = {self.PASS: "✅", self.WARN: "⚠️ ", self.FAIL: "❌"}
        checks = ["Database", "GPIO", "Connectivity",
                  "Power", "Nyansa", "ServiceBus"]
        for check in checks:
            r = self.results.get(check, "N/A")
            icon = icons.get(r, "❓")
            detail = self.results.get(f"{check}_detail",
                     self.results.get(f"{check}_method",
                     self.results.get(f"{check}_version",
                     self.results.get(f"{check}_registered", ""))))
            detail_str = f"  {detail}" if detail else ""
            print(f"     {icon} {check:<16}{r}{detail_str}")
        print("  ─────────────────────────────────────────────────────")

    def _log_results(self) -> None:
        summary = "; ".join(f"{k}={v}" for k, v in self.results.items()
                            if k in ["Database", "GPIO", "Connectivity",
                                     "Power", "Nyansa", "ServiceBus"])
        self._db.log_audit("SYSTEM", "SELF_TEST",
                            detail=summary)


# ─────────────────────────────────────────────────────────────────────────────
# B26: Admin CSV export menu (called from admin_menu)
# ─────────────────────────────────────────────────────────────────────────────

def admin_csv_export_menu(db: 'DatabaseManager',
                          export_svc: 'AdminExportService') -> None:
    """B26: Admin sub-menu for CSV data exports."""
    while True:
        os.system('cls' if os.name == 'nt' else 'clear')
        print("=" * 60)
        print(f"  {'ADMIN — CSV Data Export':^56}")
        print("=" * 60)
        print("\n  Select report type:")
        print("  [1] Transactions")
        print("  [2] CUPSCAN Returns")
        print("  [3] Inventory Status")
        print("  [4] Power Telemetry")
        print("  [5] Customer Roster")
        print("  [0] Back")
        print()
        ch = input("  > ").strip()
        if ch == '0':
            break
        type_map = {
            "1": "transactions", "2": "cupscan",
            "3": "inventory",    "4": "power",
            "5": "customers",
        }
        rtype = type_map.get(ch)
        if not rtype:
            continue

        date_from = input("  From date (YYYY-MM-DD, blank = all): ").strip() or None
        date_to   = input("  To date   (YYYY-MM-DD, blank = all): ").strip() or None

        path = export_svc.save_csv(rtype, date_from, date_to, output_dir="/tmp")
        print(f"\n  ✅ Report saved: {path}")
        print(f"     Copy to USB or sync via cloud.")
        input("\n  Press Enter…")


# ─────────────────────────────────────────────────────────────────────────────
# AidPlusServiceBus  [B20-A]
# Central product registry for the AID PLUS+ ecosystem.
# Products register via register(); dormant sockets return ServiceNotAvailable.
# ─────────────────────────────────────────────────────────────────────────────


DRUG_ADVICE = {
    # ── Pain & Fever ──────────────────────────────────────────────────────────
    "paracetamol": {
        "use":    "Fever, mild to moderate pain, headache, period pain",
        "dose":   "Adults: 500mg–1g every 4–6 hours. Max 4g/day. Children: weight-based.",
        "warn":   "Never exceed 4g/day. Avoid alcohol. Overdose causes liver failure.",
        "contra": ["liver disease", "alcohol dependency", "hepatitis"],
        "food":   "Can be taken with or without food.",
        "window": "Max 3 days for fever without medical advice.",
        "conditions": ["safe in pregnancy", "safe for elderly", "safe for children"],
    },
    "ibuprofen": {
        "use":    "Pain, inflammation, arthritis, fever, menstrual cramps",
        "dose":   "Adults: 200–400mg every 4–6 hours with food. Max 1200mg/day OTC.",
        "warn":   "ALWAYS take with food. Can cause stomach ulcers, kidney stress.",
        "contra": ["stomach ulcer", "kidney disease", "heart disease", "pregnancy",
                   "aspirin allergy", "asthma", "blood thinners", "hypertension"],
        "food":   "Must be taken with food or milk — never on empty stomach.",
        "window": "Max 3 days for fever, 5 days for pain without medical advice.",
        "conditions": ["avoid in third trimester pregnancy", "caution in elderly",
                       "avoid with blood pressure medication"],
    },
    "aspirin": {
        "use":    "Pain, fever, anti-clotting (low dose 75mg for heart patients)",
        "dose":   "Pain/fever: 300–600mg every 4–6h. Low-dose: 75mg daily (doctor prescribed).",
        "warn":   "Do not give to children under 16. Risk of Reye's syndrome.",
        "contra": ["children under 16", "stomach ulcer", "bleeding disorders",
                   "ibuprofen use", "blood thinners", "pregnancy"],
        "food":   "Take with food or milk.",
        "window": "Short-term pain use only. Low-dose use requires doctor supervision.",
        "conditions": ["avoid in dengue fever", "caution in asthma"],
    },
    "diclofenac": {
        "use":    "Arthritis, muscle pain, sports injuries, dental pain",
        "dose":   "Adults: 25–50mg two to three times daily with food.",
        "warn":   "Can cause stomach bleeding. Monitor kidney function with long use.",
        "contra": ["stomach ulcer", "kidney disease", "heart failure", "pregnancy"],
        "food":   "Must be taken with food.",
        "window": "Short-term use. Long-term requires medical supervision.",
        "conditions": ["avoid in elderly without gastro-protection"],
    },

    # ── Antibiotics ───────────────────────────────────────────────────────────
    "amoxicillin": {
        "use":    "Bacterial infections: chest, ear, throat, skin, urinary tract",
        "dose":   "Adults: 250–500mg three times daily. Course: 5–7 days minimum.",
        "warn":   "COMPLETE the full course. Stopping early causes resistance.",
        "contra": ["penicillin allergy", "mononucleosis (glandular fever)"],
        "food":   "Can be taken with or without food.",
        "window": "Prescription required. Never self-prescribe antibiotics.",
        "conditions": ["dose adjustment in kidney disease", "safe in pregnancy under supervision"],
    },
    "metronidazole": {
        "use":    "Bacterial and parasitic infections, dental infections, stomach bugs",
        "dose":   "Adults: 200–400mg three times daily. Course: 5–7 days.",
        "warn":   "STRICTLY avoid alcohol during treatment AND for 48 hours after last dose.",
        "contra": ["first trimester pregnancy", "alcohol use", "liver disease"],
        "food":   "Take with food to reduce nausea.",
        "window": "Prescription required.",
        "conditions": ["causes metallic taste — normal, not harmful"],
    },
    "ciprofloxacin": {
        "use":    "Urinary tract infections, chest infections, typhoid, anthrax exposure",
        "dose":   "Adults: 250–500mg twice daily. Course as prescribed.",
        "warn":   "Can damage tendons, especially in elderly. Avoid sun exposure.",
        "contra": ["epilepsy", "children under 18 (except special cases)",
                   "pregnancy", "myasthenia gravis"],
        "food":   "Take 2 hours before or after dairy products and antacids.",
        "window": "Prescription required. Serious antibiotic — not first-line.",
        "conditions": ["tendon rupture risk", "QT prolongation risk in heart patients"],
    },
    "erythromycin": {
        "use":    "Alternative antibiotic for penicillin-allergic patients, chest infections",
        "dose":   "Adults: 250–500mg four times daily or 500mg–1g twice daily.",
        "warn":   "Can cause nausea and stomach cramps. Take with food.",
        "contra": ["liver disease", "some heart conditions"],
        "food":   "Take with food to reduce stomach upset.",
        "window": "Prescription required.",
        "conditions": ["safe alternative for penicillin allergy"],
    },

    # ── Malaria ───────────────────────────────────────────────────────────────
    "artemether-lumefantrine": {
        "use":    "Malaria treatment (first-line in Ghana)",
        "dose":   "Weight-based. Standard adult: 4 tablets twice daily for 3 days.",
        "warn":   "MUST complete all 6 doses over 3 days even if feeling better.",
        "contra": ["first trimester pregnancy (unless life-saving)", "severe liver disease"],
        "food":   "MUST take with a fatty meal — fat is essential for absorption.",
        "window": "Prescription required. Incomplete course causes drug resistance.",
        "conditions": ["avoid with grapefruit", "caution with QT-prolonging drugs"],
    },
    "chloroquine": {
        "use":    "Malaria prevention and treatment where still effective",
        "dose":   "Prevention: 500mg weekly. Treatment: as prescribed.",
        "warn":   "Vision changes, headache, dizziness may occur.",
        "contra": ["psoriasis", "retinal disease", "epilepsy", "chloroquine resistance areas"],
        "food":   "Take with food or milk.",
        "window": "Resistance is common in Ghana — confirm with doctor first.",
        "conditions": ["regular eye checks needed for long-term use"],
    },
    "artemisinin": {
        "use":    "Malaria treatment",
        "dose":   "As prescribed — combination therapy always preferred.",
        "warn":   "Always use in combination, never as monotherapy.",
        "contra": ["first trimester pregnancy"],
        "food":   "Take with food.",
        "window": "Prescription required.",
        "conditions": [],
    },

    # ── Diabetes ──────────────────────────────────────────────────────────────
    "metformin": {
        "use":    "Type 2 diabetes management — lowers blood glucose",
        "dose":   "Adults: 500mg twice daily with meals, increased gradually.",
        "warn":   "NEVER skip meals when taking. Risk of lactic acidosis with alcohol.",
        "contra": ["kidney disease", "liver disease", "heavy alcohol use",
                   "contrast dye procedures", "heart failure"],
        "food":   "Must be taken WITH meals — never on empty stomach.",
        "window": "Prescription required. Long-term daily medication.",
        "conditions": ["monitor kidney function regularly", "B12 deficiency risk long-term"],
    },
    "glibenclamide": {
        "use":    "Type 2 diabetes — stimulates insulin production",
        "dose":   "Adults: 2.5–5mg daily with breakfast.",
        "warn":   "Risk of hypoglycaemia (low blood sugar). Always eat with dose.",
        "contra": ["type 1 diabetes", "kidney disease", "liver disease", "pregnancy"],
        "food":   "Take with or just before breakfast.",
        "window": "Prescription required.",
        "conditions": ["carry glucose sweets in case of hypoglycaemia"],
    },

    # ── Blood Pressure ────────────────────────────────────────────────────────
    "amlodipine": {
        "use":    "High blood pressure, angina (chest pain)",
        "dose":   "Adults: 5–10mg once daily.",
        "warn":   "Ankle swelling common. Do not stop suddenly.",
        "contra": ["severe low blood pressure", "advanced aortic stenosis"],
        "food":   "Can be taken with or without food. Avoid grapefruit.",
        "window": "Prescription required. Daily long-term medication.",
        "conditions": ["monitor blood pressure weekly initially"],
    },
    "lisinopril": {
        "use":    "High blood pressure, heart failure, diabetes kidney protection",
        "dose":   "Adults: 2.5–10mg once daily.",
        "warn":   "Dry cough is common side effect. Rarely causes dangerous swelling.",
        "contra": ["pregnancy", "angioedema history", "kidney artery narrowing"],
        "food":   "Take at the same time each day. Avoid high-potassium foods.",
        "window": "Prescription required.",
        "conditions": ["monitor kidney function and potassium levels"],
    },

    # ── Respiratory ───────────────────────────────────────────────────────────
    "salbutamol": {
        "use":    "Asthma and breathing difficulty — relieves bronchospasm",
        "dose":   "Inhaler: 1–2 puffs when needed. Max 8 puffs per day.",
        "warn":   "Using more than 3 times a week means asthma is not well-controlled.",
        "contra": ["none absolute, caution in heart disease"],
        "food":   "Inhaler — not food-related.",
        "window": "Reliever inhaler — use as needed. Daily use requires review.",
        "conditions": ["heart rate increase normal — reduces with time"],
    },
    "cetirizine": {
        "use":    "Hay fever, allergic rhinitis, hives, skin allergies",
        "dose":   "Adults: 10mg once daily. Children 6–12: 5mg twice daily.",
        "warn":   "May cause drowsiness — avoid driving or operating machinery.",
        "contra": ["severe kidney disease (reduce dose)"],
        "food":   "Can be taken with or without food. Avoid alcohol.",
        "window": "Safe for short or long-term use as directed.",
        "conditions": ["less sedating than older antihistamines"],
    },

    # ── Stomach & Digestion ───────────────────────────────────────────────────
    "omeprazole": {
        "use":    "Acid reflux, heartburn, stomach ulcers, GERD",
        "dose":   "Adults: 20mg once daily before breakfast.",
        "warn":   "Long-term use can reduce magnesium and B12. Masks ulcer symptoms.",
        "contra": ["taking with clopidogrel (reduces effectiveness)"],
        "food":   "Take 30 minutes BEFORE first meal of the day.",
        "window": "Short-term: 4–8 weeks. Long-term requires medical supervision.",
        "conditions": ["bone density may reduce with very long-term use"],
    },
    "antacid": {
        "use":    "Heartburn, acid reflux, indigestion, stomach upset",
        "dose":   "1–2 tablets or 10ml after meals and at bedtime as needed.",
        "warn":   "Do not use more than 2 weeks continuously without advice.",
        "contra": ["kidney disease (aluminium-containing antacids)"],
        "food":   "Take 1–2 hours after meals or at bedtime.",
        "window": "If symptoms persist beyond 2 weeks, see a doctor.",
        "conditions": ["can interfere with absorption of other medications"],
    },
    "oral rehydration salts": {
        "use":    "Rehydration from diarrhoea, vomiting, heat exhaustion",
        "dose":   "Dissolve 1 sachet in 1 litre clean water. Sip slowly and continuously.",
        "warn":   "Use CLEAN water only. Discard unused solution after 24 hours.",
        "contra": [],
        "food":   "Sip slowly — drinking too fast can cause more vomiting.",
        "window": "If diarrhoea persists beyond 2 days or there is blood — seek medical care.",
        "conditions": ["critical for children with diarrhoea", "safe in pregnancy"],
    },
    "loperamide": {
        "use":    "Diarrhoea — slows gut movement",
        "dose":   "Adults: 2 capsules initially, then 1 after each loose stool. Max 8/day.",
        "warn":   "Do NOT use if there is blood in stool or high fever.",
        "contra": ["bloody diarrhoea", "high fever", "children under 12",
                   "antibiotic-associated colitis"],
        "food":   "Take with water.",
        "window": "Max 2 days without medical advice.",
        "conditions": ["use ORS alongside to replace fluids"],
    },

    # ── Vitamins & Supplements ────────────────────────────────────────────────
    "vitamin c": {
        "use":    "Immune support, antioxidant, iron absorption enhancement",
        "dose":   "Adults: 500mg–1000mg daily. Higher doses not more effective.",
        "warn":   "Very high doses (>2g/day) can cause kidney stones and diarrhoea.",
        "contra": [],
        "food":   "Take with food to reduce stomach upset.",
        "window": "Safe for daily long-term use at normal doses.",
        "conditions": ["enhances iron absorption — take with iron supplements"],
    },
    "zinc": {
        "use":    "Immune function, wound healing, child growth, diarrhoea treatment",
        "dose":   "Adults: 10–25mg daily. Children with diarrhoea: 20mg daily for 10–14 days.",
        "warn":   "Never take on empty stomach — causes nausea.",
        "contra": [],
        "food":   "Always take with food.",
        "window": "Do not exceed 40mg/day. Excess zinc reduces copper absorption.",
        "conditions": ["important supplement in malaria-endemic areas"],
    },
    "folic acid": {
        "use":    "Prevention of neural tube defects, anaemia treatment",
        "dose":   "Pregnancy prevention: 400mcg daily. Treatment: 5mg daily as prescribed.",
        "warn":   "High doses can mask B12 deficiency anaemia.",
        "contra": ["untreated B12 deficiency"],
        "food":   "Can be taken with or without food.",
        "window": "Start 3 months before pregnancy. Safe long-term.",
        "conditions": ["critical in first 12 weeks of pregnancy"],
    },
    "iron supplement": {
        "use":    "Iron deficiency anaemia, pregnancy support",
        "dose":   "Adults: 200mg ferrous sulphate (65mg elemental iron) once or twice daily.",
        "warn":   "Causes black stools — normal. Can cause constipation and nausea.",
        "contra": ["haemochromatosis", "repeated blood transfusions"],
        "food":   "Take on empty stomach if tolerated. Vitamin C improves absorption.",
        "window": "Continue 3 months after iron levels normalise.",
        "conditions": ["avoid with tetracycline antibiotics", "take 2h apart from antacids"],
    },
    "multivitamin": {
        "use":    "General nutritional support, supplementing diet gaps",
        "dose":   "One tablet daily as directed on pack.",
        "warn":   "Do not double-dose if you miss one. Not a substitute for food.",
        "contra": [],
        "food":   "Take with food for best absorption.",
        "window": "Safe for daily long-term use.",
        "conditions": ["fat-soluble vitamins A,D,E,K can accumulate — do not exceed dose"],
    },

    # ── Skin & Eyes ───────────────────────────────────────────────────────────
    "hydrocortisone cream": {
        "use":    "Mild eczema, insect bites, contact dermatitis, nappy rash",
        "dose":   "Apply thin layer to affected area 1–2 times daily.",
        "warn":   "Do not use on face for more than 5 days. Do not use near eyes.",
        "contra": ["infected skin (without antibiotic cover)", "acne", "rosacea"],
        "food":   "Topical — not food related.",
        "window": "Max 7 days continuous use without medical advice.",
        "conditions": ["children: use very short courses only"],
    },
    "clotrimazole": {
        "use":    "Fungal infections: athlete's foot, ringworm, thrush",
        "dose":   "Apply 2–3 times daily. Vaginal: 1 pessary at night for 3–6 nights.",
        "warn":   "Continue for 2 weeks after symptoms clear to prevent recurrence.",
        "contra": [],
        "food":   "Topical/vaginal — not food related.",
        "window": "If no improvement in 4 weeks, see a doctor.",
        "conditions": [],
    },

    # ── Cough & Cold ─────────────────────────────────────────────────────────
    "cough syrup": {
        "use":    "Dry cough relief — suppresses cough reflex",
        "dose":   "Adults: 5–10ml every 4–6 hours as directed on pack.",
        "warn":   "Many contain codeine — check label. Do not use for productive cough.",
        "contra": ["children under 6 (many formulations)", "asthma", "pregnancy"],
        "food":   "Can be taken with or without food.",
        "window": "If cough lasts more than 3 weeks — see a doctor.",
        "conditions": ["codeine-containing syrups are controlled — use cautiously"],
    },
    "promethazine": {
        "use":    "Allergy, nausea, vomiting, sleep aid, cough",
        "dose":   "Adults: 25mg at night or 10–25mg twice daily.",
        "warn":   "Strong sedative — do not drive. Avoid alcohol.",
        "contra": ["children under 2 (risk of respiratory depression)",
                   "sleep apnoea", "narrow-angle glaucoma"],
        "food":   "Take with food.",
        "window": "Short-term use only for sleep.",
        "conditions": ["elderly: increased fall risk due to sedation"],
    },
}


def _nyansa_drug_lookup(drug_name: str) -> dict | None:
    """Fuzzy match drug name against Nyansa knowledge base."""
    name = drug_name.lower().strip()
    for key in DRUG_ADVICE:
        if key in name or name in key:
            return DRUG_ADVICE[key]
    return None


HEALTH_CONDITION_WARNINGS = {
    "diabetes": {
        "check_drugs": ["cough syrup", "multivitamin syrup", "antacid liquid", "omeprazole", "ibuprofen"],
        "warning": "Check sugar content in all liquid medications. NSAIDs can impair kidney function already at risk in diabetics.",
        "action":  "Prefer sugar-free formulations. Use paracetamol for pain. Monitor blood sugar after any new medication.",
    },
    "type 2 diabetes": {
        "check_drugs": ["ibuprofen", "aspirin", "cough syrup"],
        "warning": "NSAIDs worsen diabetic kidney disease. High-sugar syrups destabilise blood glucose.",
        "action":  "Use paracetamol for pain. Request sugar-free cough formulations.",
    },
    "hypertension": {
        "check_drugs": ["ibuprofen", "aspirin", "antacid liquid", "cough syrup"],
        "warning": "Ibuprofen raises blood pressure. High-sodium antacids cause fluid retention. Decongestant cough syrups constrict blood vessels.",
        "action":  "Use paracetamol for pain. Choose low-sodium antacids. Avoid decongestants.",
    },
    "high blood pressure": {
        "check_drugs": ["ibuprofen", "aspirin", "antacid liquid"],
        "warning": "NSAIDs directly antagonise antihypertensive medications and raise BP.",
        "action":  "Paracetamol is the safe analgesic. Discuss all OTC purchases with your pharmacist.",
    },
    "kidney disease": {
        "check_drugs": ["ibuprofen", "metformin", "antacid liquid", "aspirin"],
        "warning": "NSAIDs and Metformin are contraindicated in chronic kidney disease.",
        "action":  "Use paracetamol at lowest effective dose. Never take ibuprofen. Stop Metformin if creatinine is elevated.",
    },
    "liver disease": {
        "check_drugs": ["paracetamol", "paracetamol syrup", "aspirin", "metronidazole"],
        "warning": "Paracetamol is hepatotoxic at standard doses in liver disease.",
        "action":  "Maximum paracetamol 2g/day. Avoid alcohol completely.",
    },
    "heart disease": {
        "check_drugs": ["ibuprofen", "aspirin", "antacid liquid"],
        "warning": "Ibuprofen increases cardiovascular events and fluid retention in heart failure.",
        "action":  "Use paracetamol only. If on low-dose aspirin do not add ibuprofen.",
    },
    "heart failure": {
        "check_drugs": ["ibuprofen", "antacid liquid"],
        "warning": "NSAIDs cause sodium and water retention, directly worsening heart failure.",
        "action":  "Ibuprofen is absolutely contraindicated. Paracetamol only.",
    },
    "asthma": {
        "check_drugs": ["ibuprofen", "aspirin", "cough syrup"],
        "warning": "Aspirin-exacerbated respiratory disease affects 10-20% of asthmatics. NSAIDs can trigger severe bronchospasm.",
        "action":  "Use paracetamol only. Check cough syrup ingredients — avoid antihistamines that thicken mucus.",
    },
    "stomach ulcer": {
        "check_drugs": ["ibuprofen", "aspirin", "chloroquine"],
        "warning": "NSAIDs directly damage the gastric mucosa. Can cause life-threatening GI bleeding.",
        "action":  "Ibuprofen and aspirin are absolutely contraindicated. Paracetamol only. Take omeprazole.",
    },
    "peptic ulcer": {
        "check_drugs": ["ibuprofen", "aspirin"],
        "warning": "Active ulcer disease — any NSAID can perforate or bleed.",
        "action":  "Strict NSAID avoidance. Take omeprazole as prescribed.",
    },
    "pregnancy": {
        "check_drugs": ["ibuprofen", "aspirin", "cough syrup", "metronidazole", "chloroquine"],
        "warning": "Third trimester ibuprofen causes premature closure of ductus arteriosus. Many cough syrups contain alcohol.",
        "action":  "Paracetamol is the only safe analgesic throughout pregnancy. Check all syrups for alcohol content.",
    },
    "breastfeeding": {
        "check_drugs": ["cough syrup", "cetirizine", "metronidazole"],
        "warning": "Medications pass into breast milk. Sedating antihistamines cause infant drowsiness.",
        "action":  "Paracetamol is safe. Consult pharmacist for all others.",
    },
    "penicillin allergy": {
        "check_drugs": ["amoxicillin", "amoxicillin suspension"],
        "warning": "CRITICAL ALLERGY ALERT: Amoxicillin is a penicillin. Cross-reaction risk. Anaphylaxis can be fatal.",
        "action":  "Do NOT take Amoxicillin. Inform every healthcare provider. Carry allergy alert card.",
    },
    "aspirin allergy": {
        "check_drugs": ["aspirin", "ibuprofen"],
        "warning": "Aspirin allergy often extends to all NSAIDs — cross-reactivity syndrome.",
        "action":  "Avoid all NSAIDs. Use paracetamol only.",
    },
    "anaemia": {
        "check_drugs": ["iron supplement syrup", "vitamin c", "aspirin"],
        "warning": "Aspirin increases GI blood loss, worsening anaemia.",
        "action":  "Take iron with Vitamin C. Avoid tea/coffee within 2 hours of iron. Avoid aspirin.",
    },
    "sickle cell disease": {
        "check_drugs": ["ibuprofen", "aspirin", "iron supplement syrup"],
        "warning": "Iron supplementation contraindicated in sickle cell. NSAIDs worsen renal complications.",
        "action":  "Do NOT take iron unless prescribed. Paracetamol for pain. Ensure adequate hydration.",
    },
    "malaria": {
        "check_drugs": ["chloroquine", "artemether", "ibuprofen"],
        "warning": "Chloroquine resistance widespread in Ghana. Do not mix antimalarials without advice.",
        "action":  "Complete full artemether course. Paracetamol for fever. Avoid ibuprofen — masks fever signs.",
    },
    "epilepsy": {
        "check_drugs": ["chloroquine", "cetirizine"],
        "warning": "Chloroquine lowers seizure threshold — contraindicated in epilepsy.",
        "action":  "Avoid chloroquine. Consult neurologist before any new medication.",
    },
    "thyroid disease": {
        "check_drugs": ["aspirin", "iron supplement syrup", "antacid liquid"],
        "warning": "Iron and antacids reduce absorption of levothyroxine if taken simultaneously.",
        "action":  "Take thyroid medication on empty stomach. Space iron and antacids at least 4 hours apart.",
    },
    "psoriasis": {
        "check_drugs": ["chloroquine"],
        "warning": "Chloroquine is absolutely contraindicated in psoriasis — triggers severe flares.",
        "action":  "Do NOT take chloroquine. Inform every prescriber about your psoriasis.",
    },
    "alcohol use": {
        "check_drugs": ["paracetamol", "metronidazole", "cough syrup"],
        "warning": "Alcohol + paracetamol is hepatotoxic. Metronidazole + alcohol causes severe vomiting.",
        "action":  "Avoid paracetamol if drinking regularly. Never take metronidazole with alcohol.",
    },
    "blood thinners": {
        "check_drugs": ["aspirin", "ibuprofen"],
        "warning": "NSAIDs + anticoagulants massively increase bleeding risk.",
        "action":  "Paracetamol only. Inform prescriber of ALL medications. Regular INR monitoring.",
    },
    "tuberculosis": {
        "check_drugs": ["paracetamol", "antacid liquid"],
        "warning": "TB treatment is hepatotoxic — paracetamol toxicity risk significantly elevated.",
        "action":  "Limit paracetamol to 2g/day maximum. Space antacids at least 2 hours from TB drugs.",
    },
    "hiv aids": {
        "check_drugs": ["ibuprofen", "aspirin"],
        "warning": "HIV medications have numerous drug interactions. NSAIDs increase bleeding risk.",
        "action":  "Consult your HIV clinician before any new medication. Paracetamol is safest.",
    },
}



# ── Drug Consumption Probability Engine ───────────────────────────────────────
# Typical treatment durations in days per drug.
# Based on standard clinical guidelines for OTC use in Ghana.
# ─────────────────────────────────────────────────────────────────────────────
# NYANSA CONSUMPTION ALGORITHM  v2.0
# Systematic, prescription-aware, adherence-tracked consumption probability
# ─────────────────────────────────────────────────────────────────────────────

DRUG_CONSUMPTION_PROFILE = {
    "paracetamol 500mg":      {"pills_per_pack": 1, "doses_per_day": 3, "max_days": 5,
                               "timing": ["morning","afternoon","evening"], "unit": "tablet",
                               "note": "Pain/fever — max 5 days"},
    "ibuprofen 400mg":        {"pills_per_pack": 1, "doses_per_day": 3, "max_days": 5,
                               "timing": ["morning","afternoon","evening"], "unit": "tablet",
                               "note": "Anti-inflammatory — take WITH food"},
    "amoxicillin 250mg":      {"pills_per_pack": 1, "doses_per_day": 3, "max_days": 7,
                               "timing": ["morning","afternoon","evening"], "unit": "capsule",
                               "note": "Antibiotic — complete full course"},
    "cetirizine 10mg":        {"pills_per_pack": 1, "doses_per_day": 1, "max_days": 7,
                               "timing": ["evening"], "unit": "tablet",
                               "note": "Antihistamine — evening preferred"},
    "omeprazole 20mg":        {"pills_per_pack": 1, "doses_per_day": 1, "max_days": 14,
                               "timing": ["morning"], "unit": "capsule",
                               "note": "30 min BEFORE first meal"},
    "metformin 500mg":        {"pills_per_pack": 1, "doses_per_day": 2, "max_days": 30,
                               "timing": ["morning","evening"], "unit": "tablet",
                               "note": "Chronic — WITH meals"},
    "aspirin 75mg":           {"pills_per_pack": 1, "doses_per_day": 1, "max_days": 30,
                               "timing": ["morning"], "unit": "tablet",
                               "note": "Cardioprotective — WITH food"},
    "chloroquine":            {"pills_per_pack": 1, "doses_per_day": 2, "max_days": 3,
                               "timing": ["morning","evening"], "unit": "tablet",
                               "note": "Malaria — full 3-day course"},
    "artemether":             {"pills_per_pack": 1, "doses_per_day": 2, "max_days": 3,
                               "timing": ["morning","evening"], "unit": "tablet",
                               "note": "ACT — full 3-day course essential"},
    "metronidazole":          {"pills_per_pack": 1, "doses_per_day": 3, "max_days": 7,
                               "timing": ["morning","afternoon","evening"], "unit": "tablet",
                               "note": "NO alcohol during course"},
    "vitamin c":              {"pills_per_pack": 1, "doses_per_day": 1, "max_days": 30,
                               "timing": ["morning"], "unit": "tablet",
                               "note": "Supplement"},
    "zinc":                   {"pills_per_pack": 1, "doses_per_day": 1, "max_days": 14,
                               "timing": ["morning"], "unit": "tablet",
                               "note": "With food"},
    "paracetamol syrup":      {"pills_per_pack": 1, "doses_per_day": 3, "max_days": 5,
                               "timing": ["morning","afternoon","evening"], "unit": "dose",
                               "note": "Paediatric — weight-based dose"},
    "amoxicillin suspension": {"pills_per_pack": 1, "doses_per_day": 3, "max_days": 7,
                               "timing": ["morning","afternoon","evening"], "unit": "dose",
                               "note": "Paediatric antibiotic — full course"},
    "multivitamin syrup":     {"pills_per_pack": 1, "doses_per_day": 1, "max_days": 30,
                               "timing": ["morning"], "unit": "dose",
                               "note": "Nutritional supplement"},
    "cough syrup":            {"pills_per_pack": 1, "doses_per_day": 3, "max_days": 7,
                               "timing": ["morning","afternoon","evening"], "unit": "dose",
                               "note": "Symptomatic relief"},
    "iron supplement syrup":  {"pills_per_pack": 1, "doses_per_day": 1, "max_days": 30,
                               "timing": ["morning"], "unit": "dose",
                               "note": "With Vitamin C, away from tea/coffee"},
    "antacid liquid":         {"pills_per_pack": 1, "doses_per_day": 3, "max_days": 14,
                               "timing": ["morning","afternoon","evening"], "unit": "dose",
                               "note": "30 min after meals"},
    "ors":                    {"pills_per_pack": 1, "doses_per_day": 4, "max_days": 3,
                               "timing": ["morning","midday","afternoon","evening"], "unit": "sachet",
                               "note": "Acute rehydration — dissolve in 200ml water"},
}

# Risk thresholds
RISK_OVERUSE_PCT  = 1.20   # > 20% over expected = amber
RISK_UNDERUSE_PCT = 0.70   # < 30% of expected after 3 days = amber
RISK_SEVERE_PCT   = 0.40   # < 60% of expected = red


def get_drug_profile(drug_name: str) -> dict | None:
    """Match drug name to profile using substring matching."""
    key = drug_name.lower().strip()
    for drug_key, prof in DRUG_CONSUMPTION_PROFILE.items():
        if drug_key in key or key in drug_key:
            return {**prof, "matched_key": drug_key}
    return None


def calculate_baseline(drug_name: str, qty_purchased: int,
                       doses_per_day_override: float = None) -> dict:
    """
    Step 1: Calculate expected consumption baseline.
    Returns full baseline dict for storage in consumption_logs.
    """
    profile = get_drug_profile(drug_name)
    if not profile:
        return {"error": f"No profile for {drug_name}"}

    doses_per_day  = doses_per_day_override or profile["doses_per_day"]
    pills_per_unit = profile.get("pills_per_pack", 1)
    total_doses    = qty_purchased * pills_per_unit
    expected_days  = total_doses / doses_per_day

    return {
        "drug_name":      drug_name,
        "qty_purchased":  qty_purchased,
        "pills_per_unit": pills_per_unit,
        "total_doses":    total_doses,
        "doses_per_day":  doses_per_day,
        "expected_days":  round(expected_days, 1),
        "timing":         profile["timing"],
        "unit":           profile["unit"],
        "note":           profile["note"],
        "max_days":       profile["max_days"],
        "risk_level":     "green",
    }


def calculate_adherence(baseline: dict, days_elapsed: float,
                        doses_reported: int = None) -> dict:
    """
    Steps 3–4: Calculate adherence and detect inconsistencies.
    If doses_reported is None, uses time-based estimate.
    Returns adherence dict with risk_level and flags.
    """
    expected_doses_by_now = min(
        baseline["total_doses"],
        days_elapsed * baseline["doses_per_day"]
    )
    if expected_doses_by_now == 0:
        return {"adherence_pct": 1.0, "risk_level": "green",
                "flags": [], "estimated_consumed": 0}

    if doses_reported is not None:
        actual = doses_reported
    else:
        actual = expected_doses_by_now  # assume on-track if no log

    adherence_pct = actual / expected_doses_by_now if expected_doses_by_now > 0 else 1.0
    consumed_pct  = actual / baseline["total_doses"]

    flags = []
    risk  = "green"

    # Overuse check
    if adherence_pct > RISK_OVERUSE_PCT:
        flags.append(f"Overuse: {int(adherence_pct*100)}% of expected taken")
        risk = "amber"

    # Underuse checks
    if days_elapsed >= 3 and consumed_pct < RISK_SEVERE_PCT:
        flags.append(f"Severe underuse: only {int(consumed_pct*100)}% consumed after {int(days_elapsed)}d")
        risk = "red"
    elif days_elapsed >= 3 and adherence_pct < RISK_UNDERUSE_PCT:
        flags.append(f"Underuse: {int(adherence_pct*100)}% adherence")
        risk = "amber"

    # Course overdue
    if days_elapsed > baseline["max_days"] * 1.5 and consumed_pct < 0.9:
        flags.append(f"Course overdue: {int(days_elapsed)}d elapsed, only {int(consumed_pct*100)}% used")
        risk = max(risk, "amber") if risk == "green" else risk

    return {
        "adherence_pct":      round(adherence_pct, 3),
        "consumed_pct":       round(consumed_pct, 3),
        "expected_by_now":    round(expected_doses_by_now, 1),
        "actual_doses":       actual,
        "risk_level":         risk,
        "flags":              flags,
        "estimated_consumed": round(consumed_pct * 100, 1),
    }


# Step 5: Intelligent question bank per flag type
CONSUMPTION_QUESTIONS = {
    "overuse": [
        "Are you taking this medication more often than prescribed?",
        "Are you experiencing persistent pain or symptoms that require extra doses?",
        "Have you consulted a doctor about increasing your dose?",
    ],
    "underuse": [
        "Have you been taking this medication as prescribed?",
        "Have you experienced any side effects that caused you to stop?",
        "Has your condition improved and you felt you no longer needed it?",
        "Are you having trouble remembering to take your medication?",
    ],
    "overdue": [
        "Did you complete the full course of this medication?",
        "How many tablets/doses do you have remaining?",
        "Was the medication effective for your condition?",
        "Did you receive advice from a pharmacist or doctor about this medication?",
    ],
}


def get_smart_questions(flags: list) -> list:
    """Step 5: Return context-appropriate questions based on detected flags."""
    questions = []
    for flag in flags:
        key = "overuse" if "Overuse" in flag else ("overdue" if "overdue" in flag else "underuse")
        questions.extend(CONSUMPTION_QUESTIONS.get(key, []))
    return list(dict.fromkeys(questions))[:4]  # deduplicate, max 4


def estimate_consumption(drug_name: str, qty_purchased: int,
                         days_since_purchase: float,
                         doses_reported: int = None) -> dict:
    """Public API: single call to get full consumption status."""
    baseline = calculate_baseline(drug_name, qty_purchased)
    if "error" in baseline:
        return {
            "consumed_pct": None, "status": "unknown",
            "days_expected": None, "message": baseline["error"],
        }

    adherence = calculate_adherence(baseline, days_since_purchase, doses_reported)
    pct = adherence["consumed_pct"]

    if pct >= 0.90:
        status  = "likely_consumed"
        message = f"Likely finished ({int(pct*100)}% consumed, {int(days_since_purchase)}d ago)"
    elif pct >= 0.50:
        remaining = round(baseline["expected_days"] - days_since_purchase, 1)
        remaining = max(0, remaining)
        status  = "in_progress"
        message = (f"In use — {int(pct*100)}% consumed"
                   + (f", ~{remaining}d remaining" if remaining > 0 else ""))
    elif days_since_purchase > baseline["max_days"] * 1.5:
        status  = "likely_remaining"
        message = f"Course overdue — may be unused ({int(pct*100)}% consumed)"
    else:
        status  = "likely_remaining"
        message = f"Early in course — {int(pct*100)}% consumed"

    return {
        "consumed_pct":   pct,
        "adherence_pct":  adherence["adherence_pct"],
        "status":         status,
        "days_expected":  baseline["expected_days"],
        "message":        message,
        "risk_level":     adherence["risk_level"],
        "flags":          adherence["flags"],
        "questions":      get_smart_questions(adherence["flags"]),
        "note":           baseline["note"],
        "timing":         baseline["timing"],
        "doses_per_day":  baseline["doses_per_day"],
    }


def create_consumption_log(db, customer_id: str, transaction_id: str,
                           drug_name: str, qty_purchased: int,
                           purchase_date: str,
                           doses_per_day_override: float = None,
                           has_prescription: bool = False,
                           prescription_ref: str = None) -> bool:
    """
    Store initial consumption baseline in consumption_logs.
    Called at time of purchase to seed the tracking record.
    """
    from datetime import datetime as _dt
    baseline = calculate_baseline(drug_name, qty_purchased, doses_per_day_override)
    if "error" in baseline:
        return False
    try:
        with db._conn() as con:
            # Only create if not already tracked
            existing = con.execute(
                "SELECT id FROM consumption_logs WHERE transaction_id=? AND drug_name=?",
                (transaction_id, drug_name)).fetchone()
            if not existing:
                con.execute(
                    "INSERT INTO consumption_logs "
                    "(customer_id, transaction_id, drug_name, qty_purchased, pills_per_unit, "
                    "dose_per_day, timing, expected_days, purchase_date, has_prescription, "
                    "prescription_ref, doses_reported, adherence_pct, risk_level, last_updated) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,0,0.0,'green',?)",
                    (customer_id, transaction_id, drug_name,
                     qty_purchased, baseline["pills_per_unit"],
                     baseline["doses_per_day"],
                     ",".join(baseline["timing"]),
                     baseline["expected_days"], purchase_date,
                     1 if has_prescription else 0, prescription_ref,
                     _dt.now().isoformat()))
        return True
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# SAFE DAILY DOSE TABLE — Evidence-based OTC recommendations
# safe_min: minimum effective dose per day
# safe_max: maximum safe dose per day
# recommended: what we recommend for most patients
# notes: clinical guidance
# ─────────────────────────────────────────────────────────────────────────────
SAFE_DAILY_DOSES = {
    "paracetamol 500mg": {
        "safe_min": 1, "safe_max": 4, "recommended": 2,
        "timing_options": {
            1: ["morning"],
            2: ["morning", "evening"],
            3: ["morning", "afternoon", "evening"],
        },
        "notes": "2x/day is most common. Max 4x/day for severe pain. Never exceed 4g (8 tablets) per day.",
        "abnormal_3x": False,
    },
    "ibuprofen 400mg": {
        "safe_min": 1, "safe_max": 3, "recommended": 2,
        "timing_options": {
            1: ["morning"],
            2: ["morning", "evening"],
            3: ["morning", "afternoon", "evening"],
        },
        "notes": "2x/day standard. 3x/day for acute inflammation — take WITH food. Not for >5 days without review.",
        "abnormal_3x": False,
    },
    "amoxicillin 250mg": {
        "safe_min": 2, "safe_max": 3, "recommended": 3,
        "timing_options": {
            2: ["morning", "evening"],
            3: ["morning", "afternoon", "evening"],
        },
        "notes": "3x/day is standard for most infections. 2x/day for some conditions. MUST complete full course.",
        "abnormal_3x": False,
    },
    "cetirizine 10mg": {
        "safe_min": 1, "safe_max": 1, "recommended": 1,
        "timing_options": {
            1: ["evening"],
        },
        "notes": "1x/day only. Evening dosing preferred. Taking more than 1 per day has no added benefit.",
        "abnormal_3x": True,
    },
    "omeprazole 20mg": {
        "safe_min": 1, "safe_max": 2, "recommended": 1,
        "timing_options": {
            1: ["morning"],
            2: ["morning", "evening"],
        },
        "notes": "1x/day (morning, 30 min before food) is standard. 2x/day only for severe GERD. Never 3x/day.",
        "abnormal_3x": True,
    },
    "metformin 500mg": {
        "safe_min": 1, "safe_max": 3, "recommended": 2,
        "timing_options": {
            1: ["morning"],
            2: ["morning", "evening"],
            3: ["morning", "midday", "evening"],
        },
        "notes": "2x/day with meals is standard. 3x/day for dose escalation only — doctor's guidance needed.",
        "abnormal_3x": False,
    },
    "aspirin 75mg": {
        "safe_min": 1, "safe_max": 1, "recommended": 1,
        "timing_options": {
            1: ["morning"],
        },
        "notes": "1x/day cardioprotective dose only. Higher doses need medical direction.",
        "abnormal_3x": True,
    },
    "chloroquine": {
        "safe_min": 1, "safe_max": 2, "recommended": 2,
        "timing_options": {
            1: ["evening"],
            2: ["morning", "evening"],
        },
        "notes": "2x/day for malaria treatment. 3-day course. MUST complete.",
        "abnormal_3x": True,
    },
    "artemether": {
        "safe_min": 2, "safe_max": 2, "recommended": 2,
        "timing_options": {
            2: ["morning", "evening"],
        },
        "notes": "2x/day only, fixed 3-day ACT course. Never modify dose.",
        "abnormal_3x": True,
    },
    "vitamin c": {
        "safe_min": 1, "safe_max": 2, "recommended": 1,
        "timing_options": {
            1: ["morning"],
            2: ["morning", "evening"],
        },
        "notes": "1x/day is sufficient for supplementation. High doses (>2g/day) cause diarrhoea.",
        "abnormal_3x": True,
    },
    "zinc": {
        "safe_min": 1, "safe_max": 2, "recommended": 1,
        "timing_options": {
            1: ["morning"],
            2: ["morning", "evening"],
        },
        "notes": "1x/day standard. Long-term 3x/day causes copper deficiency.",
        "abnormal_3x": True,
    },
    "ors": {
        "safe_min": 3, "safe_max": 6, "recommended": 4,
        "timing_options": {
            3: ["morning", "midday", "evening"],
            4: ["morning", "midday", "afternoon", "evening"],
        },
        "notes": "As needed for rehydration. 200ml per sachet. Frequency depends on fluid loss.",
        "abnormal_3x": False,
    },
}


def get_safe_dose_advice(drug_name: str, requested_freq: int) -> dict:
    """
    Validate a customer's stated consumption frequency against safe clinical limits.
    Returns: {safe: bool, recommended: int, warning: str or None, timing: list}
    """
    key = drug_name.lower().strip()
    profile = None
    for k, v in SAFE_DAILY_DOSES.items():
        if k in key or key in k:
            profile = v
            break
    if not profile:
        # Use default: 2x/day is the most common for any OTC drug
        return {
            "safe": True, "recommended": 2,
            "timing": ["morning", "evening"], "warning": None,
            "note": "Standard 2x/day (morning and evening) applies.",
        }

    timing = profile["timing_options"].get(
        requested_freq,
        profile["timing_options"].get(profile["recommended"], ["morning"])
    )

    if requested_freq > profile["safe_max"]:
        return {
            "safe": False,
            "recommended": profile["recommended"],
            "timing": profile["timing_options"][profile["recommended"]],
            "warning": (f"⚠  {drug_name} should NOT be taken {requested_freq}x/day. "
                        f"Maximum is {profile['safe_max']}x/day. "
                        f"{profile['notes']}"),
        }
    if profile["abnormal_3x"] and requested_freq == 3:
        return {
            "safe": False,
            "recommended": profile["recommended"],
            "timing": profile["timing_options"][profile["recommended"]],
            "warning": (f"⚠  3x/day is NOT appropriate for {drug_name}. "
                        f"Recommended: {profile['recommended']}x/day. "
                        f"{profile['notes']}"),
        }
    return {
        "safe": True,
        "recommended": requested_freq,
        "timing": timing,
        "warning": None,
        "note": profile["notes"],
    }


def analyse_customer_drug_consumption(customer_id: str, db) -> list:
    """
    Analyse all recent drug purchases for a customer and estimate consumption.
    Returns list of dicts sorted by concern level.
    Used by Nyansa health review and insights engine.
    """
    from datetime import datetime as _dt
    results = []

    with db._conn() as con:
        rows = con.execute(
            "SELECT ti.name, ti.qty, t.timestamp, t.id "
            "FROM transaction_items ti "
            "JOIN transactions t ON t.id = ti.transaction_id "
            "WHERE t.customer_id=? "
            "AND COALESCE(t.status,'completed') != 'donated' "
            "ORDER BY t.timestamp DESC LIMIT 30",
            (customer_id,)).fetchall()

        # Get returned qty per item
        returned = {}
        for row in rows:
            ret = con.execute(
                "SELECT COALESCE(SUM(mr.qty_returned),0) "
                "FROM medication_returns mr "
                "JOIN transaction_items ti ON ti.id = mr.item_id "
                "WHERE mr.transaction_id=? AND ti.name=?",
                (row[3], row[0])).fetchone()[0]
            returned[(row[3], row[0])] = ret

    now = _dt.now()
    for row in rows:
        tid, drug, qty, ts = row[3], row[0], row[1], row[2]
        ret_qty = returned.get((tid, drug), 0)
        net_qty = qty - ret_qty
        if net_qty <= 0:
            continue  # fully returned
        days_ago = (now - _dt.fromisoformat(ts[:19])).total_seconds() / 86400
        est = estimate_consumption(drug, net_qty, days_ago)
        est["drug"]      = drug
        est["qty"]       = net_qty
        est["days_ago"]  = round(days_ago, 1)
        est["txn_id"]    = tid
        results.append(est)

    # Sort: unknown last, overdue first, then by consumed_pct descending
    def sort_key(r):
        if r["status"] == "unknown": return (3, 0)
        if r["status"] == "likely_remaining" and r["days_ago"] > (r["days_expected"] or 0) * 2:
            return (0, -r["days_ago"])
        if r["status"] == "likely_consumed": return (1, -r.get("consumed_pct", 0))
        return (2, -r.get("consumed_pct", 0))

    results.sort(key=sort_key)
    return results

