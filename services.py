"""
AID PLUS+ — Business Services Layer
=====================================
SupportService: ticket lifecycle (Open → In Progress → Resolved/Escalated).
TeleconsultService: doctor queue management, consult approval.
NyansaIntelligence: demand prediction, cross-sell, lapse analysis.
PromotionEngine: promotions (percent/fixed/buy-X-get-Y), segment targeting.
RestockEngine: auto-generates restock orders, tracks delivery pipeline.
OTAService: over-the-air software update engine.
NotificationService: outbound dispatch (email/SMS stub, FCM-ready).
SchedulerService: background scheduled tasks.

BUILD 29 CHANGES — TeleconsultService:
  - _migrate(): auto-adds request_type + notes columns to teleconsult_records
  - request_consult(): accepts request_type and notes kwargs
  - get_queue(): includes request_type and notes in SELECT for admin display
"""
from __future__ import annotations
import json, csv, os, random, time, threading
from datetime import datetime, timedelta

from aidplus.config import *
from aidplus.security import secure_ref
from aidplus.db import DatabaseManager
from aidplus.bus import AidPlusServiceBus

class SupportService:
    """[B13] Customer support ticket lifecycle management."""

    CATEGORIES = ('billing','hardware','account','medical','general')
    PRIORITIES  = ('low','normal','high','urgent')

    def __init__(self, db: DatabaseManager):
        self.db = db

    def open_ticket(self, customer_id: str, category: str, subject: str,
                    description: str, priority: str = "normal") -> str:
        if category not in self.CATEGORIES:
            raise ValueError(f"Invalid category. Use: {self.CATEGORIES}")
        if priority not in self.PRIORITIES:
            raise ValueError(f"Invalid priority. Use: {self.PRIORITIES}")
        ticket_id = secure_ref("TKT")
        now = datetime.now().isoformat()
        with self.db._conn() as con:
            con.execute(
                "INSERT INTO support_tickets (ticket_id,customer_id,category,priority,"
                "status,subject,description,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (ticket_id, customer_id, category, priority, "open",
                 subject[:120], description[:500], now, now))
        self.db.log_audit(customer_id, "ADMIN_ACTION", "support_tickets", ticket_id,
                          f"Opened: [{category}] {subject}")
        return ticket_id

    def update_ticket(self, ticket_id: str, status: str,
                      notes: str = "", actor: str = "ADMIN") -> bool:
        valid = ('open','in_progress','awaiting_customer','resolved','escalated')
        if status not in valid: return False
        now          = datetime.now().isoformat()
        resolved_at  = now if status == "resolved"  else None
        escalated_at = now if status == "escalated" else None
        with self.db._conn() as con:
            if not con.execute("SELECT ticket_id FROM support_tickets WHERE ticket_id=?",
                               (ticket_id,)).fetchone(): return False
            con.execute(
                "UPDATE support_tickets SET status=?,resolution_notes=?,updated_at=?,"
                "resolved_at=COALESCE(?,resolved_at),escalated_at=COALESCE(?,escalated_at)"
                " WHERE ticket_id=?",
                (status, notes, now, resolved_at, escalated_at, ticket_id))
        self.db.log_audit(actor, "STATUS_CHANGE", "support_tickets", ticket_id,
                          f"Status → {status}")
        return True

    def get_customer_tickets(self, customer_id: str) -> list:
        with self.db._conn() as con:
            return [dict(r) for r in con.execute(
                "SELECT * FROM support_tickets WHERE customer_id=? ORDER BY created_at DESC",
                (customer_id,))]

    def get_open_tickets(self) -> list:
        with self.db._conn() as con:
            return [dict(r) for r in con.execute(
                "SELECT * FROM support_tickets WHERE status NOT IN ('resolved','escalated')"
                " ORDER BY priority DESC, created_at ASC")]

    def get_overdue_tickets(self, hours: int = TICKET_SLA_HOURS) -> list:
        cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
        with self.db._conn() as con:
            return [dict(r) for r in con.execute(
                "SELECT * FROM support_tickets WHERE status NOT IN ('resolved','escalated')"
                " AND created_at<? ORDER BY created_at ASC", (cutoff,))]

    def auto_escalate_overdue(self) -> int:
        overdue = self.get_overdue_tickets()
        count = 0
        for t in overdue:
            self.update_ticket(t["ticket_id"], "escalated",
                               f"Auto-escalated: open >{TICKET_SLA_HOURS}h", "SYSTEM")
            self.db.send_notification(t["customer_id"], "Ticket Escalated",
                                      f"Your ticket '{t['subject']}' has been escalated.")
            count += 1
        return count

    def get_summary(self) -> dict:
        with self.db._conn() as con:
            rows = con.execute(
                "SELECT status, COUNT(*) as cnt FROM support_tickets GROUP BY status"
            ).fetchall()
        s = {r["status"]: r["cnt"] for r in rows}
        s["overdue"] = len(self.get_overdue_tickets())
        return s


class TeleconsultService:
    """[B13] TelePharmacy consultation queue and doctor approval system."""

    def __init__(self, db: DatabaseManager):
        self.db = db
        self._migrate()

    def _migrate(self):
        """
        [B29] One-time column migration.
        Adds request_type and notes to teleconsult_records if not already present.
        Safe to call on every startup — ALTER TABLE is wrapped in try/except.
        """
        with self.db._conn() as con:
            for col, typedef in [
                ("request_type", "TEXT NOT NULL DEFAULT 'GENERAL'"),
                ("notes",        "TEXT NOT NULL DEFAULT ''"),
            ]:
                try:
                    con.execute(
                        f"ALTER TABLE teleconsult_records ADD COLUMN {col} {typedef}")
                except Exception:
                    pass  # Column already exists — safe to ignore

    def request_consult(self, customer_id: str, drug_names: list,
                        priority: int = 2,
                        request_type: str = "GENERAL",
                        notes: str = "") -> dict:
        """
        Queue a teleconsult request.
        request_type: 'GENERAL' | 'PRESCRIPTION'
        notes: patient-described condition (used by doctor on admin side)
        """
        consult_id = secure_ref("CON")
        drug_str   = ", ".join(drug_names)
        now        = datetime.now().isoformat()
        with self.db._conn() as con:
            con.execute(
                "INSERT INTO teleconsult_records (consult_id,customer_id,drug_names,"
                "status,requested_at,request_type,notes) VALUES (?,?,?,?,?,?,?)",
                (consult_id, customer_id, drug_str, "queued", now,
                 request_type, notes[:500]))
            con.execute(
                "INSERT INTO teleconsult_queue (customer_id,consult_id,priority,joined_at,status)"
                " VALUES (?,?,?,?,?)",
                (customer_id, consult_id, priority, now, "waiting"))
        self.db.log_audit(customer_id, "ADMIN_ACTION", "teleconsult_records", consult_id,
                          f"Consult requested [{request_type}]: {drug_str}")
        pos = self.get_queue_position(consult_id)
        return {"consult_id": consult_id, "queue_position": pos,
                "drug_names": drug_str, "status": "queued"}

    def get_queue_position(self, consult_id: str) -> int:
        with self.db._conn() as con:
            row = con.execute(
                "SELECT COUNT(*) as pos FROM teleconsult_queue WHERE status='waiting'"
                " AND queue_id<=(SELECT queue_id FROM teleconsult_queue WHERE consult_id=?)",
                (consult_id,)).fetchone()
            return row["pos"] if row else 0

    def get_queue(self) -> list:
        with self.db._conn() as con:
            return [dict(r) for r in con.execute(
                "SELECT tq.*, tr.drug_names, tr.request_type, tr.notes, "
                "c.name as customer_name "
                "FROM teleconsult_queue tq "
                "JOIN teleconsult_records tr ON tq.consult_id=tr.consult_id "
                "JOIN customers c ON tq.customer_id=c.customer_id "
                "WHERE tq.status='waiting' ORDER BY tq.priority, tq.joined_at")]

    def admin_resolve_consult(self, consult_id: str, decision: str,
                               doctor_note: str = "") -> bool:
        if decision not in ("approved","rejected"): return False
        now = datetime.now().isoformat()
        with self.db._conn() as con:
            con.execute(
                "UPDATE teleconsult_records SET status=?,decision=?,doctor_note=?,resolved_at=?"
                " WHERE consult_id=?",
                (decision, decision, doctor_note, now, consult_id))
            con.execute("UPDATE teleconsult_queue SET status='done' WHERE consult_id=?",
                        (consult_id,))
            row = con.execute(
                "SELECT customer_id,drug_names FROM teleconsult_records WHERE consult_id=?",
                (consult_id,)).fetchone()
        if row:
            icon = "✅" if decision == "approved" else "❌"
            msg  = f"{icon} Consult {decision.upper()} for {row['drug_names']}. {doctor_note}"
            self.db.send_notification(row["customer_id"], "Teleconsult Result", msg)
            self.db.log_audit("ADMIN", f"TELECONSULT_{decision.upper()}",
                              "teleconsult_records", consult_id, f"{row['drug_names']}")
        return True

    def is_purchase_approved(self, customer_id: str, drug_names: list) -> tuple:
        needs = [d for d in drug_names if any(cd in d for cd in CONSULT_REQUIRED_DRUGS)]
        if not needs: return True, "No consultation required."
        with self.db._conn() as con:
            for drug in needs:
                approved = con.execute(
                    "SELECT consult_id FROM teleconsult_records WHERE customer_id=?"
                    " AND decision='approved' AND drug_names LIKE ? AND resolved_at>?",
                    (customer_id, f"%{drug}%",
                     (datetime.now() - timedelta(hours=24)).isoformat())).fetchone()
                if not approved:
                    return False, f"Teleconsult required for {drug}. Visit the consult menu."
        return True, "All consultations approved."

    def get_summary(self) -> dict:
        q = self.get_queue()
        with self.db._conn() as con:
            total = con.execute("SELECT COUNT(*) FROM teleconsult_records").fetchone()[0]
            today = con.execute(
                "SELECT COUNT(*) FROM teleconsult_records WHERE DATE(requested_at)=DATE('now')"
            ).fetchone()[0]
        return {"waiting": len(q), "total_all_time": total, "today": today}


# ═══════════════════════════════════════════════════════════════════════════════
# BUILD 14 — Nyansa INTELLIGENCE ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

class NyansaIntelligence:
    """
    [B14] The Nyansa AI Brain.
    Demand prediction, cross-sell analysis, lapse detection, price anomalies.
    All insights stored in nyansa_insights. Pure data layer — zero UI calls.
    """

    def __init__(self, db: DatabaseManager):
        self.db = db

    def run_full_analysis(self) -> list:
        results = []
        results += self._analyse_demand()
        results += self._analyse_cross_sell()
        results += self._analyse_customer_lapses()
        results += self._analyse_peak_hours()
        results += self._analyse_price_anomalies()
        return results

    def predict_demand(self, drug_id: int, drug_name: str,
                        current_stock: int, is_mega: bool = False) -> dict:
        tbl = "mega_inventory" if is_mega else "inventory"
        with self.db._conn() as con:
            shelf_row = con.execute(f"SELECT shelf FROM {tbl} WHERE drug_id=?",
                                    (drug_id,)).fetchone()
            if not shelf_row: return {}
            shelf_num = shelf_row["shelf"]
            cutoff    = (datetime.now() - timedelta(days=30)).isoformat()
            row = con.execute(
                "SELECT SUM(ti.qty) as total_qty,"
                " COUNT(DISTINCT DATE(t.timestamp)) as days "
                "FROM transaction_items ti JOIN transactions t "
                "ON ti.transaction_id=t.id "
                "WHERE ti.shelf_num=? AND t.timestamp>?",
                (shelf_num, cutoff)).fetchone()
            # Promotion boost factor
            promo_boost = 1.0
            upcoming = con.execute(
                "SELECT discount_value,discount_type FROM promotions "
                "WHERE (drug_id=? OR drug_id IS NULL) AND status IN ('draft','active')"
                " AND start_date<=? AND end_date>=?",
                (drug_id,
                 (datetime.now() + timedelta(days=RESTOCK_LEAD_TIME_DAYS)).isoformat(),
                 datetime.now().isoformat())).fetchall()
            for p in upcoming:
                if p["discount_type"] == "percent":
                    promo_boost = max(promo_boost, 1 + p["discount_value"] / 100)

        total_qty   = row["total_qty"] or 0
        active_days = max(row["days"] or 1, 1)
        avg_daily   = total_qty / active_days
        predicted   = avg_daily * RESTOCK_LEAD_TIME_DAYS * RESTOCK_SAFETY_FACTOR * promo_boost
        return {
            "drug_id":        drug_id,
            "drug_name":      drug_name,
            "avg_daily_sales": round(avg_daily, 2),
            "predicted_demand": round(predicted, 1),
            "needs_restock":  current_stock <= int(predicted),
            "promo_boost":    round(promo_boost, 2),
            "confidence":     min(1.0, active_days / 15),
        }

    def _analyse_demand(self) -> list:
        insights = []
        for item in self.db.get_all_shelves() + self.db.get_all_mega_shelves():
            is_mega = bool(item.get("is_mega"))
            stock   = item.get("units_left" if is_mega else "capsules_left", 0)
            pred    = self.predict_demand(item["drug_id"], item["name"], stock, is_mega)
            if not pred.get("needs_restock"): continue
            threshold = LOW_STOCK_MEGA_THRESHOLD if is_mega else LOW_STOCK_THRESHOLD
            itype = "restock_urgent" if stock <= threshold else "demand_surge"
            ins = self._save_insight(
                insight_type = itype,
                drug_id      = item["drug_id"],
                confidence   = pred["confidence"],
                title        = f"Restock needed: {item['name']}",
                description  = (f"Stock: {stock}. Avg daily: {pred['avg_daily_sales']}. "
                                f"Lead-time demand: {pred['predicted_demand']}."
                                + (f" Promo boost: {pred['promo_boost']}x"
                                   if pred["promo_boost"] > 1 else "")),
                action       = f"Generate restock order for {item['name']}.")
            if ins: insights.append(ins)
        return insights

    def _analyse_cross_sell(self) -> list:
        insights = []
        with self.db._conn() as con:
            rows = con.execute(
                "SELECT ti1.name as a, ti2.name as b, COUNT(*) as cnt "
                "FROM transaction_items ti1 "
                "JOIN transaction_items ti2 ON ti1.transaction_id=ti2.transaction_id"
                " AND ti1.name < ti2.name "
                "GROUP BY ti1.name, ti2.name HAVING cnt >= 3 "
                "ORDER BY cnt DESC LIMIT 5").fetchall()
        for r in rows:
            ins = self._save_insight(
                insight_type = "cross_sell",
                confidence   = min(1.0, r["cnt"] / 10),
                title        = f"Bundle opportunity: {r['a']} + {r['b']}",
                description  = f"Co-purchased {r['cnt']} times. Bundle or co-placement recommended.",
                action       = f"Create bundle promotion: {r['a']} + {r['b']}.")
            if ins: insights.append(ins)
        return insights

    def _analyse_customer_lapses(self) -> list:
        active_cutoff = (datetime.now() - timedelta(days=7)).isoformat()
        lapsed_cutoff = (datetime.now() - timedelta(days=30)).isoformat()
        with self.db._conn() as con:
            count = con.execute(
                "SELECT COUNT(DISTINCT c.customer_id) FROM customers c "
                "JOIN transactions t ON c.customer_id=t.customer_id "
                "GROUP BY c.customer_id "
                "HAVING MAX(t.timestamp) BETWEEN ? AND ?",
                (lapsed_cutoff, active_cutoff)).fetchone()
        n = count[0] if count else 0
        if n == 0: return []
        ins = self._save_insight(
            insight_type     = "customer_lapse",
            confidence       = 0.85,
            customer_segment = "inactive_30d",
            title            = f"{n} customers inactive 30+ days",
            description      = f"{n} previously active customers haven't returned in a month.",
            action           = "Send re-engagement wellness notification.")
        return [ins] if ins else []

    def _analyse_peak_hours(self) -> list:
        stats = self.db.get_clts_stats()
        if not stats["peak_hours"]: return []
        peak = stats["peak_hours"][0]
        low  = stats["peak_hours"][-1] if len(stats["peak_hours"]) > 1 else None
        ins = self._save_insight(
            insight_type = "peak_hour",
            confidence   = 0.75,
            title        = f"Peak traffic: {peak['time_of_day'].upper()}",
            description  = (f"{peak['cnt']} sessions in {peak['time_of_day']}."
                            + (f" Lowest: {low['time_of_day']} ({low['cnt']})." if low else "")),
            action       = f"Schedule promos and restock checks for {peak['time_of_day']}.")
        return [ins] if ins else []

    def _analyse_price_anomalies(self) -> list:
        insights = []
        for item in self.db.get_all_shelves() + self.db.get_all_mega_shelves():
            tbl   = "mega_inventory" if item.get("is_mega") else "inventory"
            hist  = self.db.get_price_history(item["drug_id"], tbl, limit=10)
            if len(hist) < 5: continue
            avg_p = sum(h["price"] for h in hist) / len(hist)
            drift = abs(item["current_price"] - avg_p) / avg_p if avg_p > 0 else 0
            if drift < 0.15: continue
            ins = self._save_insight(
                insight_type = "price_anomaly",
                drug_id      = item["drug_id"],
                confidence   = min(1.0, drift),
                title        = f"Price anomaly: {item['name']}",
                description  = (f"Current ₵{item['current_price']:.2f} is {drift*100:.0f}%"
                                f" from avg ₵{avg_p:.2f}."),
                action       = "Review base price or flag for renegotiation.")
            if ins: insights.append(ins)
        return insights

    def _save_insight(self, insight_type: str, title: str, description: str,
                       action: str, confidence: float = 0.5,
                       drug_id: int = None, customer_segment: str = "") -> dict | None:
        with self.db._conn() as con:
            if con.execute(
                "SELECT insight_id FROM nyansa_insights WHERE insight_type=? AND drug_id=?"
                " AND status='pending' AND DATE(generated_at)=DATE('now')",
                (insight_type, drug_id)).fetchone():
                return None
            cur = con.execute(
                "INSERT INTO nyansa_insights (insight_type,generated_at,drug_id,"
                "customer_segment,confidence_score,title,description,"
                "recommended_action,status) VALUES (?,?,?,?,?,?,?,?,?)",
                (insight_type, datetime.now().isoformat(), drug_id, customer_segment,
                 round(confidence, 3), title, description, action, "pending"))
            row_id = cur.lastrowid
        with self.db._conn() as con:
            r = con.execute("SELECT * FROM nyansa_insights WHERE insight_id=?", (row_id,)).fetchone()
            return dict(r) if r else None

    def get_pending_insights(self, limit: int = 20) -> list:
        with self.db._conn() as con:
            return [dict(r) for r in con.execute(
                "SELECT * FROM nyansa_insights WHERE status='pending'"
                " ORDER BY confidence_score DESC, generated_at DESC LIMIT ?", (limit,))]

    def action_insight(self, insight_id: int, actor: str = "ADMIN") -> bool:
        with self.db._conn() as con:
            con.execute(
                "UPDATE nyansa_insights SET status='actioned',actioned_at=?,actioned_by=?"
                " WHERE insight_id=?",
                (datetime.now().isoformat(), actor, insight_id))
        return True

    def dismiss_insight(self, insight_id: int, actor: str = "ADMIN") -> bool:
        with self.db._conn() as con:
            con.execute(
                "UPDATE nyansa_insights SET status='dismissed',actioned_at=?,actioned_by=?"
                " WHERE insight_id=?",
                (datetime.now().isoformat(), actor, insight_id))
        return True

    def get_summary(self) -> dict:
        pending = self.get_pending_insights(5)
        stats   = self.db.get_clts_stats()
        with self.db._conn() as con:
            total    = con.execute("SELECT COUNT(*) FROM nyansa_insights").fetchone()[0]
            actioned = con.execute(
                "SELECT COUNT(*) FROM nyansa_insights WHERE status='actioned'").fetchone()[0]
        return {"pending_insights": pending, "total": total,
                "actioned": actioned, "clts_stats": stats}


class PromotionEngine:
    """[B14] Promotion creation, targeting, and cart-time application."""

    def __init__(self, db: DatabaseManager):
        self.db = db

    def create_promotion(self, name: str, discount_type: str, discount_value: float,
                          start_date: str, end_date: str, drug_id: int = None,
                          target_segment: str = "all", min_qty: int = 1,
                          buy_qty: int = 1, get_qty: int = 0,
                          created_by: str = "ADMIN") -> str:
        promo_id = secure_ref("PRO")
        with self.db._conn() as con:
            con.execute(
                "INSERT INTO promotions (promo_id,name,drug_id,discount_type,discount_value,"
                "buy_qty,get_qty,start_date,end_date,target_segment,min_purchase_qty,"
                "status,created_by,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (promo_id, name, drug_id, discount_type, discount_value, buy_qty, get_qty,
                 start_date, end_date, target_segment, min_qty,
                 "draft", created_by, datetime.now().isoformat()))
        self.db.log_audit(created_by, "ADMIN_ACTION", "promotions", promo_id,
                          f"Promotion created: {name}")
        return promo_id

    def activate_promotion(self, promo_id: str) -> bool:
        with self.db._conn() as con:
            con.execute("UPDATE promotions SET status='active' WHERE promo_id=?", (promo_id,))
        return True

    def get_active_promotions(self, drug_id: int = None) -> list:
        now = datetime.now().isoformat()
        with self.db._conn() as con:
            if drug_id is not None:
                rows = con.execute(
                    "SELECT * FROM promotions WHERE status='active'"
                    " AND start_date<=? AND end_date>=? AND (drug_id=? OR drug_id IS NULL)",
                    (now, now, drug_id)).fetchall()
            else:
                rows = con.execute(
                    "SELECT * FROM promotions WHERE status='active'"
                    " AND start_date<=? AND end_date>=?", (now, now)).fetchall()
            return [dict(r) for r in rows]

    def apply_best_promotion(self, cart_item: dict, customer: dict) -> dict:
        tier    = customer.get("wallet_tier", "G0")
        loyalty = customer.get("loyalty_points", 0)
        nhis    = bool(customer.get("nhis_session_active"))
        promos  = self.get_active_promotions()
        best_saving = 0.0; best_promo = None
        orig_price  = cart_item["price_per"]
        for p in promos:
            seg = p["target_segment"]
            if seg == "nhis"             and not nhis:                    continue
            if seg == "g2_plus"          and tier not in ("G2","G3","G3+plus"): continue
            if seg == "loyalty_100plus"  and loyalty < 100:               continue
            if cart_item["qty"] < p["min_purchase_qty"]:                  continue
            if p["discount_type"] == "percent":
                saving = orig_price * p["discount_value"] / 100
            elif p["discount_type"] == "fixed":
                saving = min(p["discount_value"], orig_price)
            else:
                saving = 0.0
            if saving > best_saving:
                best_saving = saving; best_promo = p
        if best_promo and best_saving > 0:
            cart_item = dict(cart_item)
            cart_item["price_per"]     = round(orig_price - best_saving, 2)
            cart_item["promo_applied"] = best_promo["name"]
            with self.db._conn() as con:
                con.execute("UPDATE promotions SET times_applied=times_applied+1 WHERE promo_id=?",
                            (best_promo["promo_id"],))
        return cart_item

    def expire_old_promotions(self) -> int:
        now = datetime.now().isoformat()
        with self.db._conn() as con:
            c = con.execute(
                "UPDATE promotions SET status='expired' WHERE status='active' AND end_date<?",
                (now,))
            return c.rowcount

    def get_all_promotions(self) -> list:
        with self.db._conn() as con:
            return [dict(r) for r in con.execute(
                "SELECT * FROM promotions ORDER BY created_at DESC")]


class RestockEngine:
    """[B14] Auto restock order generation and full lifecycle management."""

    def __init__(self, db: DatabaseManager, nyansa: NyansaIntelligence):
        self.db   = db
        self.nyansa = nyansa

    def calculate_needs(self) -> list:
        needs = []
        for item in self.db.get_all_shelves() + self.db.get_all_mega_shelves():
            is_mega = bool(item.get("is_mega"))
            stock   = item.get("units_left" if is_mega else "capsules_left", 0)
            max_cap = MAX_MEGA_PER_SHELF if is_mega else MAX_CAPS_PER_SHELF
            pred    = self.nyansa.predict_demand(item["drug_id"], item["name"], stock, is_mega)
            if pred.get("needs_restock"):
                qty = max(1, int(max_cap - stock + pred["predicted_demand"]))
                needs.append({"drug_id": item["drug_id"], "drug_name": item["name"],
                              "current_stock": stock, "qty_to_order": qty,
                              "shelf": item["shelf"], "is_mega": is_mega, "pred": pred})
        return needs

    def generate_order(self, drug_id: int, drug_name: str,
                        qty: int, current_stock: int,
                        generated_by: str = "NYANSA_AUTO") -> str:
        order_id = secure_ref("ORD")
        expected = (datetime.now() + timedelta(days=RESTOCK_LEAD_TIME_DAYS)).isoformat()
        with self.db._conn() as con:
            con.execute(
                "INSERT INTO restock_orders (order_id,drug_id,drug_name,quantity_ordered,"
                "current_stock,generated_by,generated_at,status,expected_delivery)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (order_id, drug_id, drug_name, qty, current_stock,
                 generated_by, datetime.now().isoformat(), "draft", expected))
        self.db.log_audit(generated_by, "ADMIN_ACTION", "restock_orders", order_id,
                          f"{qty}x {drug_name}")
        return order_id

    def auto_generate_all(self) -> list:
        orders = []
        for n in self.calculate_needs():
            with self.db._conn() as con:
                if con.execute(
                    "SELECT order_id FROM restock_orders WHERE drug_id=?"
                    " AND status IN ('draft','sent','confirmed','shipped')",
                    (n["drug_id"],)).fetchone():
                    continue
            oid = self.generate_order(n["drug_id"], n["drug_name"],
                                      n["qty_to_order"], n["current_stock"])
            orders.append(oid)
        return orders

    def send_order(self, order_id: str, supplier_ref: str = "") -> bool:
        with self.db._conn() as con:
            con.execute("UPDATE restock_orders SET status='sent',supplier_ref=? WHERE order_id=?",
                        (supplier_ref or secure_ref("SUP"), order_id))
        self.db.log_audit("ADMIN", "ADMIN_ACTION", "restock_orders", order_id, "Sent to distribution centre")
        return True

    def receive_order(self, order_id: str, received_qty: int) -> bool:
        with self.db._conn() as con:
            row = con.execute("SELECT * FROM restock_orders WHERE order_id=?",
                              (order_id,)).fetchone()
            if not row: return False
            order = dict(row)
            # Increment inventory in same connection
            item_upper = con.execute("SELECT drug_id,capsules_left FROM inventory WHERE drug_id=?",
                                     (order["drug_id"],)).fetchone()
            if item_upper:
                new_stock = min(item_upper["capsules_left"] + received_qty, MAX_CAPS_PER_SHELF)
                con.execute("UPDATE inventory SET capsules_left=? WHERE drug_id=?",
                            (new_stock, order["drug_id"]))
            else:
                item_mega = con.execute("SELECT drug_id,units_left FROM mega_inventory WHERE drug_id=?",
                                        (order["drug_id"],)).fetchone()
                if item_mega:
                    new_stock = min(item_mega["units_left"] + received_qty, MAX_MEGA_PER_SHELF)
                    con.execute("UPDATE mega_inventory SET units_left=? WHERE drug_id=?",
                                (new_stock, order["drug_id"]))
            con.execute(
                "UPDATE restock_orders SET status='received',received_qty=?,received_at=?"
                " WHERE order_id=?",
                (received_qty, datetime.now().isoformat(), order_id))
        self.db.log_audit("ADMIN", "ADMIN_ACTION", "restock_orders", order_id,
                          f"Received {received_qty}x {order['drug_name']} — stock updated")
        return True

    def update_status(self, order_id: str, status: str, notes: str = "") -> bool:
        with self.db._conn() as con:
            con.execute("UPDATE restock_orders SET status=?,notes=? WHERE order_id=?",
                        (status, notes, order_id))
        return True

    def get_pending(self) -> list:
        with self.db._conn() as con:
            return [dict(r) for r in con.execute(
                "SELECT * FROM restock_orders WHERE status NOT IN ('received','cancelled')"
                " ORDER BY generated_at")]

    def get_all(self) -> list:
        with self.db._conn() as con:
            return [dict(r) for r in con.execute(
                "SELECT * FROM restock_orders ORDER BY generated_at DESC")]

    def export_csv(self, filepath: str = "restock_orders_export.csv") -> str:
        import csv
        orders = self.get_all()
        fields = ["order_id","drug_name","quantity_ordered","current_stock",
                  "status","generated_by","generated_at","expected_delivery",
                  "received_qty","received_at","notes"]
        with open(filepath, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader(); w.writerows(orders)
        return filepath


# ═══════════════════════════════════════════════════════════════════════════════
# BUILD 15 — CONNECTIVITY & OPERATIONS SERVICES
# Pure service layer — no print(), no input(), no time.sleep() inside methods.
# ═══════════════════════════════════════════════════════════════════════════════

class OTAService:
    """
    [B15-A] Over-the-Air software update engine.
    Fetches version manifest, verifies SHA-256 checksum, stages new build,
    and coordinates with launcher.py for safe deployment + rollback.
    """

    def __init__(self, db: DatabaseManager):
        self.db = db
        os.makedirs(OTA_STAGING_DIR, exist_ok=True)
        os.makedirs(OTA_BACKUP_DIR,  exist_ok=True)

    def check_for_update(self) -> dict:
        """
        Simulates fetching the remote version manifest.
        In production: replaces urllib call with requests.get(OTA_MANIFEST_URL).
        Returns manifest dict with available_version, checksum, download_url.
        """
        import hashlib
        # Simulated manifest — production fetches from OTA_MANIFEST_URL
        simulated_manifest = {
            "available_version": SCHEMA_VERSION,   # same = no update available
            "min_compatible":    12,
            "release_notes":     f"Build {SCHEMA_VERSION} — current build.",
            "checksum":          self._checksum_current_file(),
            "download_url":      OTA_MANIFEST_URL,
            "file_size_bytes":   os.path.getsize(__file__) if os.path.exists(__file__) else 0,
        }
        self.db.ping_unit()
        return simulated_manifest

    def _checksum_current_file(self) -> str:
        import hashlib
        try:
            with open(__file__, "rb") as f:
                return hashlib.sha256(f.read()).hexdigest()
        except Exception:
            return ""

    def verify_staged_file(self, staged_path: str, expected_checksum: str) -> bool:
        import hashlib, secrets
        if not os.path.exists(staged_path): return False
        with open(staged_path, "rb") as f:
            actual = hashlib.sha256(f.read()).hexdigest()
        return secrets.compare_digest(actual, expected_checksum)

    def stage_update(self, from_version: int, to_version: int,
                      source_path: str, checksum: str) -> dict:
        """
        Copies update file to staging directory and creates update record.
        Does NOT replace the running file — launcher.py handles that.
        """
        staged_path = os.path.join(OTA_STAGING_DIR, f"AidSystem_v{to_version}.py")
        try:
            import shutil
            shutil.copy2(source_path, staged_path)
            if not self.verify_staged_file(staged_path, checksum):
                os.remove(staged_path)
                return {"success": False, "error": "Checksum verification failed."}
            # Write pending update marker
            marker = os.path.join(OTA_STAGING_DIR, "pending.txt")
            with open(marker, "w") as f:
                f.write(f"{to_version}\n{staged_path}\n{checksum}\n")
            uid = self.db.record_update(from_version, to_version, "staged", checksum,
                                        f"Staged at {staged_path}")
            return {"success": True, "update_id": uid,
                    "staged_path": staged_path, "message": "Update staged successfully."}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def apply_update(self, update_id: str) -> dict:
        """
        Backs up current file and replaces with staged version.
        Records OTA_APPLY to PDMS audit log.
        Production: launcher.py handles this at next clean startup.
        """
        marker = os.path.join(OTA_STAGING_DIR, "pending.txt")
        if not os.path.exists(marker):
            return {"success": False, "error": "No staged update found."}
        try:
            with open(marker) as f:
                lines      = f.read().strip().split("\n")
                to_version = int(lines[0])
                staged     = lines[1]
                checksum   = lines[2]
            # Back up current
            backup = os.path.join(OTA_BACKUP_DIR, f"AidSystem_v{SCHEMA_VERSION}_backup.py")
            import shutil
            shutil.copy2(__file__, backup)
            # Replace running file
            shutil.copy2(staged, __file__)
            os.remove(marker)
            self.db.complete_update(update_id, "applied")
            self.db.log_audit("SYSTEM", "OTA_APPLY", "system_updates", update_id,
                              f"v{SCHEMA_VERSION}→v{to_version} | backup: {backup}")
            return {"success": True, "message": f"Update to v{to_version} applied. Restart required."}
        except Exception as e:
            self.db.complete_update(update_id, "failed")
            return {"success": False, "error": str(e)}

    def rollback(self, to_version: int = None) -> dict:
        """
        Restores last backup. Called by launcher.py on crash detection.
        """
        backups = sorted([
            f for f in os.listdir(OTA_BACKUP_DIR) if f.endswith(".py")
        ], reverse=True)
        if not backups:
            return {"success": False, "error": "No backups available."}
        target = os.path.join(OTA_BACKUP_DIR, backups[0])
        try:
            import shutil
            shutil.copy2(target, __file__)
            self.db.log_audit("SYSTEM", "OTA_ROLLBACK", "system_updates",
                              None, f"Rolled back to {target}")
            return {"success": True, "message": f"Rolled back to {target}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_update_history(self) -> list:
        return self.db.get_update_history()


class NotificationService:
    """
    [B15-B] Outbound notification dispatch.
    Simulates email/SMS. Production: swap body with SMTP/Twilio.
    Logs every dispatch attempt to notification_dispatch table.
    """

    def __init__(self, db: DatabaseManager):
        self.db = db

    def dispatch(self, customer_id: str, subject: str, message: str,
                  channel: str = "in_app") -> dict:
        """
        Send a notification via specified channel.
        Channels: in_app (always works), email, sms (simulated).
        """
        try:
            if channel == "in_app":
                self.db.send_notification(customer_id, subject, message)
                status = "sent"
            elif channel == "email":
                # Production: call SMTP here
                status = "simulated"
            elif channel == "sms":
                # Production: call Twilio here
                status = "simulated"
            else:
                status = "failed"
            dispatch_id = self.db.log_dispatch(customer_id, channel,
                                                subject, message, status)
            return {"success": True, "dispatch_id": dispatch_id,
                    "channel": channel, "status": status}
        except Exception as e:
            self.db.log_dispatch(customer_id, channel, subject, message,
                                  "failed", str(e))
            return {"success": False, "error": str(e)}

    def dispatch_all_channels(self, customer_id: str,
                               subject: str, message: str) -> list:
        """Send via all channels the customer has enabled."""
        c = self.db.get_customer(customer_id)
        results = [self.dispatch(customer_id, subject, message, "in_app")]
        if c and c.get("notifications", {}).get("email"):
            results.append(self.dispatch(customer_id, subject, message, "email"))
        if c and c.get("notifications", {}).get("sms"):
            results.append(self.dispatch(customer_id, subject, message, "sms"))
        return results

    def broadcast(self, subject: str, message: str,
                   segment: str = "all") -> int:
        """
        Send to all customers (or a segment).
        segment: 'all' | 'nhis' | 'g2_plus' | 'active'
        """
        customers = self.db.get_all_customers()
        count = 0
        for c in customers:
            if segment == "nhis"    and not c.get("nhis_active"): continue
            if segment == "g2_plus" and c.get("wallet_tier","G0") \
               not in ("G2","G3","G3+plus"):                       continue
            if segment == "active"  and c.get("status") != "Active": continue
            self.dispatch(c["customer_id"], subject, message, "in_app")
            count += 1
        return count

    def get_dispatch_summary(self) -> dict:
        log = self.db.get_dispatch_log(limit=200)
        total  = len(log)
        by_status = {}
        for entry in log:
            s = entry["status"]
            by_status[s] = by_status.get(s, 0) + 1
        return {"total": total, "by_status": by_status}


class SchedulerService:
    """
    [B15-C] Background task scheduler.
    Runs recurring maintenance tasks on a configurable interval.
    All tasks are pure service-layer calls — no UI.
    """

    TASKS = [
        "nyansa_analysis",
        "promo_expiry",
        "sla_escalation",
        "rx_expiry",
        "unit_ping",
    ]

    def __init__(self, db: DatabaseManager):
        self.db          = db
        self._running    = False
        self._thread     = None
        self._last_run   = {}

    def _run_task(self, task_name: str) -> str:
        """Execute a single named task. Returns result summary string."""
        log_id = self.db.start_task_log(task_name)
        start  = datetime.now()
        try:
            result = ""
            if task_name == "nyansa_analysis":
                nyansa   = NyansaIntelligence(self.db)
                promos = PromotionEngine(self.db)
                ins    = nyansa.run_full_analysis()
                promos.expire_old_promotions()
                result = f"{len(ins)} new insight(s) generated."

            elif task_name == "promo_expiry":
                pe     = PromotionEngine(self.db)
                count  = pe.expire_old_promotions()
                result = f"{count} promotion(s) expired."

            elif task_name == "sla_escalation":
                ss     = SupportService(self.db)
                count  = ss.auto_escalate_overdue()
                result = f"{count} ticket(s) escalated."

            elif task_name == "rx_expiry":
                self.db.expire_prescriptions()
                result = "Prescription expiry sweep completed."

            elif task_name == "unit_ping":
                self.db.ping_unit()
                result = f"Unit {UNIT_ID} last_seen updated."

            duration = int((datetime.now() - start).total_seconds() * 1000)
            self.db.complete_task_log(log_id, result, duration_ms=duration)
            self._last_run[task_name] = datetime.now().isoformat()
            return result

        except Exception as e:
            duration = int((datetime.now() - start).total_seconds() * 1000)
            self.db.complete_task_log(log_id, error=str(e), duration_ms=duration)
            return f"ERROR: {e}"

    def run_all_tasks(self) -> dict:
        """Run all scheduled tasks once. Returns results dict."""
        results = {}
        for task in self.TASKS:
            results[task] = self._run_task(task)
        return results

    def run_task(self, task_name: str) -> str:
        if task_name not in self.TASKS:
            return f"Unknown task: {task_name}"
        return self._run_task(task_name)

    def _scheduler_loop(self, interval_secs: int):
        while self._running:
            self.run_all_tasks()
            # Sleep in small chunks so stop() responds quickly
            for _ in range(interval_secs * 2):
                if not self._running: break
                time.sleep(0.5)

    def start(self, interval_secs: int = SCHEDULER_INTERVAL_SECS):
        if self._running: return
        self._running = True
        import threading
        self._thread = threading.Thread(
            target=self._scheduler_loop,
            args=(interval_secs,),
            daemon=True,
            name="AIDScheduler"
        )
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread: self._thread.join(timeout=5)

    def get_status(self) -> dict:
        return {
            "running":   self._running,
            "last_runs": self._last_run,
            "log":       self.db.get_scheduler_log(limit=10),
        }


# ═══════════════════════════════════════════════════════════════════════════════
# BUILD 16 — REST API & WEB DASHBOARD
# ═══════════════════════════════════════════════════════════════════════════════
