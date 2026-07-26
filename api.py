"""
AID PLUS+ — REST API & Web Dashboard
======================================
Flask REST API exposing the full service layer over HTTP.
JWT authentication, role-based access control, web management dashboard.
_build_flask_app: constructs the Flask application with all routes.
_build_dashboard_html: returns the single-page management dashboard HTML.
APIServer: manages Flask lifecycle, admin menu integration.
"""
from __future__ import annotations
import json, os, time, threading, csv, random
from datetime import datetime, timedelta
import secrets

from aidplus.config import *
from aidplus.db import DatabaseManager
from aidplus.security import secure_ref


def _calc_hw_net(db) -> float:
    with db._conn() as con:
        dep = con.execute("SELECT COALESCE(ABS(SUM(amount)),0) FROM wallet_history WHERE type='Hardware Deposit'").fetchone()[0]
        ref = con.execute("SELECT COALESCE(ABS(SUM(amount)),0) FROM wallet_history WHERE type='Hardware Refund'").fetchone()[0]
    return round(dep - ref, 2)

def _calc_bonus_out(db) -> float:
    with db._conn() as con:
        val = con.execute("SELECT COALESCE(SUM(amount),0) FROM wallet_history WHERE type='bonus_earn'").fetchone()[0]
    return round(abs(val), 2)

def _calc_maintenance_fund(db) -> dict:
    """15% of hardware deposit revenue reserved for maintenance."""
    with db._conn() as con:
        deposits = con.execute(
            "SELECT COALESCE(ABS(SUM(amount)),0) FROM wallet_history WHERE type='Hardware Deposit'"
        ).fetchone()[0]
        hw = db.get_hw_status()
        last = con.execute(
            "SELECT timestamp FROM wallet_history WHERE type='Hardware Deposit' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    rate    = 0.15
    balance = round(deposits * rate, 2)
    return {
        "balance":           balance,
        "total_contributed": balance,
        "rate":              int(rate * 100),
        "last_entry":        (last[0] if last else None),
        "aid_box_sterilize_count": hw.get("aid_box_usage", 0),
        "cpr_kit_sterilize_count": hw.get("cpr_kit_usage", 0),
    }

def _calc_annual_projections(db) -> dict:
    """Project 12-month figures based on rolling 90-day velocity."""
    with db._conn() as con:
        oldest = con.execute("SELECT MIN(timestamp) FROM transactions").fetchone()[0]
        if not oldest:
            return {}
        from datetime import datetime
        days = max(1, (datetime.now() - datetime.fromisoformat(oldest[:19])).days)
        if days < 7:
            return {}
        drug_rev = con.execute("SELECT COALESCE(SUM(total),0) FROM transactions").fetchone()[0]
        hw_dep   = con.execute("SELECT COALESCE(ABS(SUM(amount)),0) FROM wallet_history WHERE type='Hardware Deposit'").fetchone()[0]
        hw_ref   = con.execute("SELECT COALESCE(ABS(SUM(amount)),0) FROM wallet_history WHERE type='Hardware Refund'").fetchone()[0]
        bonus    = con.execute("SELECT COALESCE(SUM(amount),0) FROM wallet_history WHERE type='bonus_earn'").fetchone()[0]
    factor       = 365 / days
    drug_12m     = round(drug_rev   * factor, 2)
    hw_12m       = round((hw_dep - hw_ref) * factor, 2)
    maint_12m    = round(hw_dep * factor * 0.15, 2)
    bonus_12m    = round(abs(bonus) * factor, 2)
    net_12m      = round(drug_12m + hw_12m - maint_12m - bonus_12m, 2)
    return {
        "drug_revenue_12m":   drug_12m,
        "hw_revenue_12m":     hw_12m,
        "maintenance_cost_12m": maint_12m,
        "bonus_projected_12m":  bonus_12m,
        "net_12m":            net_12m,
        "data_days":          days,
    }


def _build_flask_app(db: DatabaseManager) -> object:
    from aidplus.services import (SupportService, TeleconsultService, NyansaIntelligence,
                                   PromotionEngine, RestockEngine, OTAService, NotificationService)
    from aidplus.hardware import HardwareInterface, TemperatureSensor, DispenserManager
    from aidplus.auth import BiometricAuthService, PasswordRecoveryService, CupsuleService
    from aidplus.bus import AidPlusServiceBus
    from aidplus.reporting import ReportingService
    """
    [B16-A] Builds and returns a configured Flask application.
    All business logic routes through existing service classes.
    JWT authentication on every protected endpoint.
    """
    try:
        from flask import Flask, request, jsonify
        import jwt as pyjwt
    except ImportError:
        return None

    app = Flask(__name__)

    # ── CORS — allows browser HTML files to connect to this API ──────────────
    @app.after_request
    def _cors(response):
        origin = request.headers.get("Origin", "*")
        response.headers["Access-Control-Allow-Origin"]  = origin
        response.headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization"
        response.headers["Access-Control-Allow-Methods"] = "GET,POST,PUT,PATCH,DELETE,OPTIONS"
        response.headers["Access-Control-Allow-Credentials"] = "true"
        return response

    @app.route("/api/<path:p>", methods=["OPTIONS"])
    @app.route("/", methods=["OPTIONS"])
    def _preflight(p=""):
        return "", 204

    # ── JWT helpers ────────────────────────────────────────────────────────────
    def _make_token(actor_id: str, role: str) -> str:
        payload = {
            "sub":    actor_id,
            "role":   role,
            "iat":    datetime.utcnow(),
            "exp":    datetime.utcnow() + timedelta(hours=JWT_EXPIRY_HOURS),
            "unit":   UNIT_ID,
        }
        return pyjwt.encode(payload, API_SECRET_KEY, algorithm="HS256")

    def _decode_token(token: str) -> dict | None:
        try:
            return pyjwt.decode(token, API_SECRET_KEY, algorithms=["HS256"])
        except Exception:
            return None

    def _require_auth(*allowed_roles):
        """Decorator factory for role-based auth."""
        from functools import wraps
        def decorator(f):
            @wraps(f)
            def wrapper(*args, **kwargs):
                auth = request.headers.get("Authorization", "")
                if not auth.startswith("Bearer "):
                    return jsonify({"error": "Missing token"}), 401
                token   = auth.split(" ", 1)[1]
                payload = _decode_token(token)
                if not payload:
                    return jsonify({"error": "Invalid or expired token"}), 401
                if allowed_roles and payload.get("role") not in allowed_roles:
                    return jsonify({"error": "Insufficient permissions"}), 403
                request.actor_id = payload["sub"]
                request.role     = payload["role"]
                return f(*args, **kwargs)
            return wrapper
        return decorator

    # ── Auth endpoints ─────────────────────────────────────────────────────────
    @app.route("/api/auth/login", methods=["POST"])
    def api_login():
        data = request.get_json() or {}
        cid  = data.get("id", "").strip()
        pwd  = data.get("password", "").strip()
        if cid == "admin":
            if secrets.compare_digest(pwd, ADMIN_PASSWORD):
                token = _make_token("ADMIN", "admin")
                db.create_api_token("ADMIN", "admin", "API login")
                db.log_audit("ADMIN", "LOGIN_OK", detail="API login")
                return jsonify({"token": token, "role": "admin", "unit": UNIT_ID})
            return jsonify({"error": "Invalid credentials"}), 401
        c = db.get_customer(cid)
        if not c or not db.verify_customer_password(cid, pwd):
            return jsonify({"error": "Invalid credentials"}), 401
        token = _make_token(cid, "readonly")
        return jsonify({"token": token, "role": "readonly",
                        "name": c["name"], "unit": UNIT_ID})

    @app.route("/api/auth/doctor_login", methods=["POST"])
    def api_doctor_login():
        data = request.get_json() or {}
        code = data.get("code", "").strip()
        # Prototype: doctor code is "DOCTOR123"
        # Production: verify against a doctors table
        if code == "DOCTOR123":
            token = _make_token("DOCTOR_001", "doctor")
            return jsonify({"token": token, "role": "doctor"})
        return jsonify({"error": "Invalid doctor code"}), 401

    @app.route("/api/auth/distrib_login", methods=["POST"])
    def api_distrib_login():
        data = request.get_json() or {}
        if data.get("key") == "DISTRIB_SECRET":
            token = _make_token("DISTRIB_001", "distrib_centre")
            return jsonify({"token": token, "role": "distrib_centre"})
        return jsonify({"error": "Invalid key"}), 401

    # ── Inventory ──────────────────────────────────────────────────────────────
    @app.route("/api/inventory", methods=["GET"])
    @_require_auth()
    def api_inventory():
        shelves = db.get_all_shelves() + db.get_all_mega_shelves()
        return jsonify({"shelves": shelves, "count": len(shelves)})

    @app.route("/api/inventory/<int:shelf_num>", methods=["GET"])
    @_require_auth()
    def api_inventory_item(shelf_num):
        item = db.get_item_by_shelf(shelf_num)
        if not item: return jsonify({"error": "Not found"}), 404
        return jsonify(item)

    @app.route("/api/inventory/refill/<int:shelf_num>", methods=["POST"])
    @_require_auth("admin")
    def api_refill(shelf_num):
        item = db.get_item_by_shelf(shelf_num)
        if not item: return jsonify({"error": "Not found"}), 404
        if item.get("is_mega"): db.refill_mega(shelf_num)
        else:                    db.refill_upper(shelf_num)
        db.log_audit(request.actor_id, "ADMIN_ACTION", "inventory",
                     str(shelf_num), f"Shelf {shelf_num} refilled via API")
        return jsonify({"success": True, "shelf": shelf_num})

    # ── Customers ──────────────────────────────────────────────────────────────
    @app.route("/api/customers", methods=["GET"])
    @_require_auth("admin")
    def api_customers():
        customers = db.get_all_customers()
        safe = [{k: v for k, v in c.items()
                 if k not in ("password","password_salt","face_signature")}
                for c in customers]
        return jsonify({"customers": safe, "count": len(safe)})

    @app.route("/api/customers/<customer_id>", methods=["GET"])
    @_require_auth("admin")
    def api_customer(customer_id):
        c = db.get_customer(customer_id)
        if not c: return jsonify({"error": "Not found"}), 404
        safe = {k: v for k, v in c.items()
                if k not in ("password","password_salt","face_signature")}
        return jsonify(safe)

    # ── Support Tickets ────────────────────────────────────────────────────────
    @app.route("/api/tickets", methods=["GET"])
    @_require_auth("admin")
    def api_tickets_list():
        svc = SupportService(db)
        return jsonify({"tickets": svc.get_open_tickets(),
                        "summary": svc.get_summary()})

    @app.route("/api/tickets", methods=["POST"])
    @_require_auth()
    def api_tickets_create():
        data = request.get_json() or {}
        svc  = SupportService(db)
        try:
            tid = svc.open_ticket(
                data.get("customer_id", request.actor_id),
                data.get("category", "general"),
                data.get("subject", ""),
                data.get("description", ""),
                data.get("priority", "normal"))
            return jsonify({"ticket_id": tid, "success": True}), 201
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

    @app.route("/api/tickets/<ticket_id>", methods=["PATCH"])
    @_require_auth("admin")
    def api_tickets_update(ticket_id):
        data = request.get_json() or {}
        svc  = SupportService(db)
        ok   = svc.update_ticket(ticket_id, data.get("status",""),
                                 data.get("notes",""), request.actor_id)
        return jsonify({"success": ok})

    # ── Teleconsult ────────────────────────────────────────────────────────────
    @app.route("/api/consult/queue", methods=["GET"])
    @_require_auth("admin","doctor")
    def api_consult_queue():
        svc = TeleconsultService(db)
        return jsonify({"queue": svc.get_queue(), "summary": svc.get_summary()})

    @app.route("/api/consult/request", methods=["POST"])
    @_require_auth()
    def api_consult_request():
        data = request.get_json() or {}
        svc  = TeleconsultService(db)
        result = svc.request_consult(
            data.get("customer_id", request.actor_id),
            data.get("drug_names", []),
            data.get("priority", 2))
        return jsonify(result), 201

    @app.route("/api/consult/<consult_id>/resolve", methods=["POST"])
    @_require_auth("admin","doctor")
    def api_consult_resolve(consult_id):
        data = request.get_json() or {}
        svc  = TeleconsultService(db)
        ok   = svc.admin_resolve_consult(consult_id,
                                          data.get("decision",""),
                                          data.get("doctor_note",""))
        return jsonify({"success": ok})

    # ── Restock Orders ─────────────────────────────────────────────────────────
    @app.route("/api/restock", methods=["GET"])
    @_require_auth("admin","distrib_centre")
    def api_restock_list():
        nyansa = NyansaIntelligence(db)
        eng  = RestockEngine(db, nyansa)
        return jsonify({"orders": eng.get_pending(),
                        "all_count": len(eng.get_all())})

    @app.route("/api/restock/generate", methods=["POST"])
    @_require_auth("admin")
    def api_restock_generate():
        nyansa = NyansaIntelligence(db)
        eng  = RestockEngine(db, nyansa)
        oids = eng.auto_generate_all()
        return jsonify({"generated": oids, "count": len(oids)}), 201

    @app.route("/api/restock/<order_id>/send", methods=["POST"])
    @_require_auth("admin")
    def api_restock_send(order_id):
        nyansa = NyansaIntelligence(db)
        eng  = RestockEngine(db, nyansa)
        ok   = eng.send_order(order_id)
        return jsonify({"success": ok})

    @app.route("/api/restock/<order_id>/receive", methods=["POST"])
    @_require_auth("admin","distrib_centre")
    def api_restock_receive(order_id):
        data = request.get_json() or {}
        nyansa = NyansaIntelligence(db)
        eng  = RestockEngine(db, nyansa)
        ok   = eng.receive_order(order_id, int(data.get("received_qty", 0)))
        return jsonify({"success": ok})

    # ── Nyansa Insights ──────────────────────────────────────────────────────────
    @app.route("/api/insights", methods=["GET"])
    @_require_auth("admin")
    def api_insights():
        nyansa = NyansaIntelligence(db)
        return jsonify({"insights": nyansa.get_pending_insights(),
                        "summary":  nyansa.get_summary()})

    @app.route("/api/insights/run", methods=["POST"])
    @_require_auth("admin")
    def api_insights_run():
        nyansa   = NyansaIntelligence(db)
        promos = PromotionEngine(db)
        new    = nyansa.run_full_analysis()
        promos.expire_old_promotions()
        return jsonify({"new_insights": len(new)})

    @app.route("/api/insights/<int:insight_id>/action", methods=["POST"])
    @_require_auth("admin")
    def api_insight_action(insight_id):
        data = request.get_json() or {}
        nyansa = NyansaIntelligence(db)
        if data.get("dismiss"):
            nyansa.dismiss_insight(insight_id, request.actor_id)
        else:
            nyansa.action_insight(insight_id, request.actor_id)
        return jsonify({"success": True})

    # ── Promotions ─────────────────────────────────────────────────────────────
    @app.route("/api/promotions", methods=["GET"])
    @_require_auth("admin")
    def api_promotions():
        pe = PromotionEngine(db)
        return jsonify({"promotions": pe.get_all_promotions(),
                        "active": pe.get_active_promotions()})

    @app.route("/api/promotions", methods=["POST"])
    @_require_auth("admin")
    def api_promotions_create():
        data = request.get_json() or {}
        pe   = PromotionEngine(db)
        try:
            pid = pe.create_promotion(
                data["name"], data["discount_type"], float(data["discount_value"]),
                data["start_date"], data["end_date"],
                drug_id        = data.get("drug_id"),
                target_segment = data.get("target_segment","all"),
                created_by     = request.actor_id)
            if data.get("activate"): pe.activate_promotion(pid)
            return jsonify({"promo_id": pid, "success": True}), 201
        except (KeyError, ValueError) as e:
            return jsonify({"error": str(e)}), 400

    # ── Analytics ──────────────────────────────────────────────────────────────
    @app.route("/api/analytics", methods=["GET"])
    @_require_auth("admin")
    def api_analytics():
        unit_id = request.args.get("unit_id", UNIT_ID)
        return jsonify({
            "unit_id":         unit_id,
            "total_revenue":   db.total_med_revenue(),
            "total_qty_sold":  db.total_sales_qty(),
            "stock_value":     db.total_stock_value(),
            "upgrade_revenue": db.total_upgrade_revenue(),
            "audit_entries":   db.get_audit_count(),
            "clts_stats":      db.get_clts_stats(),
        })

    # ── Units ──────────────────────────────────────────────────────────────────
    @app.route("/api/units", methods=["GET"])
    @_require_auth("admin")
    def api_units():
        return jsonify({"units": db.get_all_units()})

    @app.route("/api/units/<unit_id>/analytics", methods=["GET"])
    @_require_auth("admin")
    def api_unit_analytics(unit_id):
        return jsonify(db.get_unit_analytics(unit_id))

    # ── OTA ────────────────────────────────────────────────────────────────────
    @app.route("/api/ota/check", methods=["GET"])
    @_require_auth("admin")
    def api_ota_check():
        ota = OTAService(db)
        return jsonify(ota.check_for_update())

    @app.route("/api/ota/history", methods=["GET"])
    @_require_auth("admin")
    def api_ota_history():
        return jsonify({"history": db.get_update_history()})

    # ── Web Management Dashboard ───────────────────────────────────────────────
    @app.route("/", methods=["GET"])
    @app.route("/dashboard", methods=["GET"])
    def api_dashboard():
        return _build_dashboard_html()

    @app.route("/api/dashboard/data", methods=["GET"])
    @_require_auth("admin")
    def api_dashboard_data():
        """JSON feed for the live dashboard."""
        shelves    = db.get_all_shelves() + db.get_all_mega_shelves()
        low_stock  = [s for s in shelves if
                      (s.get("capsules_left",0) <= LOW_STOCK_THRESHOLD
                       if not s.get("is_mega")
                       else s.get("units_left",0) <= LOW_STOCK_MEGA_THRESHOLD)]
        today_cut  = datetime.now().replace(hour=0,minute=0,second=0).isoformat()
        with db._conn() as con:
            today_rev = con.execute(
                "SELECT SUM(total) FROM transactions WHERE timestamp>?",
                (today_cut,)).fetchone()[0] or 0.0
        svc_s   = SupportService(db)
        con_s   = TeleconsultService(db)
        nyansa    = NyansaIntelligence(db)
        nyansa_s  = nyansa.get_summary()
        pe      = PromotionEngine(db)
        reng    = RestockEngine(db, nyansa)
        return jsonify({
            "unit_id":         UNIT_ID,
            "schema_version":  SCHEMA_VERSION,
            "timestamp":       datetime.now().isoformat(),
            "revenue_today":   round(today_rev, 2),
            "total_revenue":   round(db.total_med_revenue(), 2),
            "stock_value":     round(db.total_stock_value(), 2),
            "low_stock_count": len(low_stock),
            "low_stock_items": [{
                "name":  s["name"],
                "shelf": s["shelf"],
                "stock": s.get("capsules_left", s.get("units_left",0))
            } for s in low_stock],
            "tickets":         svc_s.get_summary(),
            "consult_queue":   con_s.get_summary(),
            "insights":        nyansa_s,
            "active_promos":   len(pe.get_active_promotions()),
            "restock_pending": len(reng.get_pending()),
            "clts_stats":      db.get_clts_stats(),
            "units":           db.get_all_units(),
            "power_source":    db.get_hw_status().get("power_source", "SOLAR+BATTERY"),
            "battery_pct":     db.get_hw_status().get("battery_pct", 100.0),
            "connectivity":    "Online",
            "offline_queue":   0,
            "hw_status":       db.get_hw_status(),
            "hw_net_revenue":  _calc_hw_net(db),
            "bonus_paid_out":  _calc_bonus_out(db),
            "maintenance_fund": _calc_maintenance_fund(db),
            "annual_projections": _calc_annual_projections(db),
        })

    @app.route("/api/health", methods=["GET"])
    def api_health():
        return jsonify({"status": "ok", "unit": UNIT_ID,
                        "version": SCHEMA_VERSION,
                        "time": datetime.now().isoformat(),
                        "hw_simulation": HW_SIMULATION_MODE,
                        "has_reportlab": HAS_REPORTLAB})

    # ── BUILD 17: MOBILE API ───────────────────────────────────────────────────

    @app.route("/api/mobile/register_token", methods=["POST"])
    @_require_auth()
    def api_register_device_token():
        data = request.get_json() or {}
        ok   = db.register_device_token(
            request.actor_id,
            data.get("token", ""),
            data.get("platform", "android"))
        return jsonify({"success": ok})

    @app.route("/api/mobile/home", methods=["GET"])
    @_require_auth()
    def api_mobile_home():
        cid = request.actor_id
        c   = db.get_customer(cid)
        if not c: return jsonify({"error": "Customer not found"}), 404
        with db._conn() as con:
            recent_txns = [dict(r) for r in con.execute(
                "SELECT transaction_id,total,timestamp,status "
                "FROM transactions WHERE customer_id=? "
                "ORDER BY timestamp DESC LIMIT 5", (cid,))]
            notifs = [dict(r) for r in con.execute(
                "SELECT title,message,created_at,is_read "
                "FROM notifications WHERE customer_id=? "
                "ORDER BY created_at DESC LIMIT 10", (cid,))]
        return jsonify({
            "name":           c["name"],
            "wallet_balance": c["wallet_balance"],
            "loyalty_points": c["loyalty_points"],
            "wallet_tier":    c.get("wallet_tier","G0"),
            "nhis_active":    bool(c.get("nhis_active")),
            "recent_txns":    recent_txns,
            "notifications":  notifs,
            "unread_count":   sum(1 for n in notifs if not n["is_read"]),
        })

    @app.route("/api/mobile/wallet/topup", methods=["POST"])
    @_require_auth()
    def api_mobile_wallet_topup():
        data   = request.get_json() or {}
        amount = float(data.get("amount", 0))
        if amount < 1:
            return jsonify({"error": "Minimum top-up is ₵1.00"}), 400
        ref = secure_ref("MOMO")
        db.record_momo_webhook(ref, request.actor_id, amount, "pending")
        db.log_audit(request.actor_id, "TOPUP_INITIATED", "wallet", ref, f"₵{amount}")
        return jsonify({
            "reference":    ref,
            "amount":       amount,
            "status":       "pending",
            "instructions": f"Dial *170# → My Approvals → Approve ₵{amount:.2f} to AID SYSTEM",
            "momo_prompt":  f"AID-{ref}",
        }), 201

    @app.route("/api/payment/momo/callback", methods=["POST"])
    def api_momo_callback():
        data      = request.get_json() or {}
        reference = data.get("ClientReference") or data.get("reference", "")
        status    = data.get("Status", "").lower()
        amount    = float(data.get("Amount") or data.get("amount", 0))
        if MOMO_SECRET:
            import hmac as _hmac
            sig      = request.headers.get("X-Hubtel-Signature", "")
            expected = _hmac.new(MOMO_SECRET.encode(),
                                  request.get_data(), hashlib.sha256).hexdigest()
            if not secrets.compare_digest(sig, expected):
                return jsonify({"error": "Invalid signature"}), 403
        with db._conn() as con:
            row = con.execute(
                "SELECT * FROM momo_webhooks WHERE reference=? AND status='pending'",
                (reference,)).fetchone()
        if not row:
            return jsonify({"status": "ignored"})
        row = dict(row)
        if status in ("success", "paid", "completed"):
            db.credit_wallet(row["customer_id"], amount, f"MoMo top-up {reference}")
            db.process_momo_webhook(reference)
            db.log_audit(row["customer_id"], "TOPUP_SUCCESS", "wallet", reference, f"₵{amount}")
            NotificationService(db).dispatch(
                row["customer_id"], "Top-up Successful 💰",
                f"₵{amount:.2f} added to your wallet.", "in_app")
        else:
            with db._conn() as con:
                con.execute("UPDATE momo_webhooks SET status='failed',processed_at=? WHERE reference=?",
                            (datetime.now().isoformat(), reference))
        return jsonify({"status": "processed"})

    @app.route("/api/mobile/inventory", methods=["GET"])
    @_require_auth()
    def api_mobile_inventory():
        shelves = db.get_all_shelves() + db.get_all_mega_shelves()
        result  = []
        for s in shelves:
            stock = s.get("capsules_left", s.get("units_left", 0))
            result.append({
                "shelf":       s["shelf"],
                "name":        s["name"],
                "price":       s.get("price", 0),
                "unit":        s.get("unit","pcs"),
                "in_stock":    stock > 0,
                "stock_level": stock,
                "is_mega":     bool(s.get("is_mega")),
                "image_key":   s["name"].lower().replace(" ", "_"),
            })
        return jsonify({"inventory": result, "count": len(result)})

    @app.route("/api/mobile/cart", methods=["GET","POST","DELETE"])
    @_require_auth()
    def api_mobile_cart():
        cid = request.actor_id
        if request.method == "GET":
            cart  = db.get_cart(cid)
            total = sum(i.get("subtotal", 0) for i in cart)
            return jsonify({"cart": cart, "total": round(total, 2), "count": len(cart)})
        if request.method == "POST":
            data  = request.get_json() or {}
            shelf = int(data.get("shelf", 0))
            qty   = int(data.get("qty", 1))
            item  = db.get_item_by_shelf(shelf)
            if not item: return jsonify({"error": "Item not found"}), 404
            db.add_to_cart(cid, shelf, qty)
            return jsonify({"success": True, "item": item["name"], "qty": qty})
        data  = request.get_json() or {}
        shelf = int(data.get("shelf", 0))
        db.remove_from_cart(cid, shelf)
        return jsonify({"success": True})

    @app.route("/api/mobile/checkout", methods=["POST"])
    @_require_auth()
    def api_mobile_checkout():
        cid  = request.actor_id
        cart = db.get_cart(cid)
        if not cart: return jsonify({"error": "Cart is empty"}), 400
        total = sum(i.get("subtotal", 0) for i in cart)
        c     = db.get_customer(cid)
        if c["wallet_balance"] < total:
            return jsonify({"error": "Insufficient wallet balance",
                            "balance": c["wallet_balance"], "total": total}), 400
        db.debit_wallet(cid, total, "Mobile checkout")
        tid = secure_ref("TXN")
        with db._conn() as con:
            con.execute(
                "INSERT INTO transactions (transaction_id,customer_id,"
                "total,status,timestamp,unit_id) VALUES (?,?,?,?,?,?)",
                (tid, cid, total, "completed", datetime.now().isoformat(), UNIT_ID))
            for item in cart:
                con.execute(
                    "INSERT INTO transaction_items "
                    "(transaction_id,drug_name,quantity,unit_price,subtotal) "
                    "VALUES (?,?,?,?,?)",
                    (tid, item["name"], item.get("qty", 1),
                     item.get("price", 0), item.get("subtotal", 0)))
        db.clear_cart(cid)
        db.log_audit(cid, "PURCHASE", "transactions", tid, f"Mobile ₵{total:.2f}")
        NotificationService(db).dispatch(
            cid, "Purchase Confirmed ✅",
            f"Order #{tid[:8]} — ₵{total:.2f}. Collect at the kiosk.", "in_app")
        return jsonify({"success": True, "transaction_id": tid, "total": total,
                        "collection_note": "Please collect your items at the kiosk dispenser."}), 201

    @app.route("/api/mobile/history", methods=["GET"])
    @_require_auth()
    def api_mobile_history():
        cid = request.actor_id
        with db._conn() as con:
            txns = [dict(r) for r in con.execute(
                "SELECT t.transaction_id,t.total,t.timestamp,t.status,"
                "GROUP_CONCAT(ti.drug_name,', ') AS drugs "
                "FROM transactions t "
                "LEFT JOIN transaction_items ti ON t.transaction_id=ti.transaction_id "
                "WHERE t.customer_id=? GROUP BY t.transaction_id "
                "ORDER BY t.timestamp DESC LIMIT 30", (cid,))]
        return jsonify({"history": txns, "count": len(txns)})

    @app.route("/api/mobile/profile", methods=["GET","PATCH"])
    @_require_auth()
    def api_mobile_profile():
        cid = request.actor_id
        if request.method == "GET":
            c = db.get_customer(cid)
            if not c: return jsonify({"error": "Not found"}), 404
            return jsonify({k: v for k, v in c.items()
                            if k not in ("password","password_salt","face_signature")})
        data   = request.get_json() or {}
        update = {k: v for k, v in data.items() if k in {"notifications"}}
        with db._conn() as con:
            for k, v in update.items():
                con.execute(f"UPDATE customers SET {k}=? WHERE customer_id=?", (str(v), cid))
        return jsonify({"success": True})

    @app.route("/api/mobile/notifications", methods=["GET"])
    @_require_auth()
    def api_mobile_notifications():
        cid = request.actor_id
        with db._conn() as con:
            notifs = [dict(r) for r in con.execute(
                "SELECT * FROM notifications WHERE customer_id=? "
                "ORDER BY created_at DESC LIMIT 30", (cid,))]
            con.execute("UPDATE notifications SET is_read=1 WHERE customer_id=?", (cid,))
        return jsonify({"notifications": notifs, "count": len(notifs)})

    # ── BUILD 19: REPORTS API ──────────────────────────────────────────────────

    @app.route("/api/reports", methods=["GET"])
    @_require_auth("admin")
    def api_reports_list():
        return jsonify({"reports": ReportingService(db).get_all_reports()})

    @app.route("/api/reports/monthly_revenue", methods=["POST"])
    @_require_auth("admin")
    def api_report_monthly():
        data = request.get_json() or {}
        path = ReportingService(db).report_monthly_revenue(
            year=data.get("year"), month=data.get("month"),
            generated_by=request.actor_id)
        return jsonify({"success": True, "file": path})

    @app.route("/api/reports/consumption", methods=["POST"])
    @_require_auth("admin")
    def api_report_consumption():
        data = request.get_json() or {}
        path = ReportingService(db).report_drug_consumption(
            days=int(data.get("days", 30)), generated_by=request.actor_id)
        return jsonify({"success": True, "file": path})

    @app.route("/api/reports/nhis", methods=["POST"])
    @_require_auth("admin")
    def api_report_nhis():
        data = request.get_json() or {}
        path = ReportingService(db).report_nhis_utilisation(
            year=data.get("year"), month=data.get("month"),
            generated_by=request.actor_id)
        return jsonify({"success": True, "file": path})

    @app.route("/api/reports/stock_turnover", methods=["POST"])
    @_require_auth("admin")
    def api_report_stock():
        path = ReportingService(db).report_stock_turnover(generated_by=request.actor_id)
        return jsonify({"success": True, "file": path})

    @app.route("/api/reports/demographics", methods=["POST"])
    @_require_auth("admin")
    def api_report_demo():
        path = ReportingService(db).report_clts_demographics(generated_by=request.actor_id)
        return jsonify({"success": True, "file": path})

    @app.route("/api/reports/multi_unit", methods=["POST"])
    @_require_auth("admin")
    def api_report_multi():
        path = ReportingService(db).report_multi_unit(generated_by=request.actor_id)
        return jsonify({"success": True, "file": path})

    # ── BUILD 18: HARDWARE API ─────────────────────────────────────────────────

    @app.route("/api/hardware/health", methods=["GET"])
    @_require_auth("admin")
    def api_hw_health():
        return jsonify(HardwareInterface(db).hardware_health())

    @app.route("/api/hardware/dispense", methods=["POST"])
    @_require_auth("admin")
    def api_hw_dispense():
        data  = request.get_json() or {}
        shelf = int(data.get("shelf", 0))
        qty   = int(data.get("qty", 1))
        hw    = HardwareInterface(db)
        return jsonify(DispenserManager(db, hw).dispense(
            shelf, qty, customer_id=request.actor_id))

    @app.route("/api/hardware/temperature", methods=["GET"])
    @_require_auth("admin")
    def api_hw_temperature():
        return jsonify(TemperatureSensor(db).read())

    @app.route("/api/hardware/dispense_log", methods=["GET"])
    @_require_auth("admin")
    def api_hw_dispense_log():
        return jsonify({"log": db.get_dispense_history()})

    # ── BUILD 20: AUTH & SYNC API ──────────────────────────────────────────────

    @app.route("/api/mobile/sync", methods=["POST"])
    def api_mobile_sync():
        """Exchange a kiosk-generated sync_token for a full customer JWT."""
        data  = request.get_json() or {}
        token = data.get("sync_token", "")
        if not token:
            return jsonify({"error": "sync_token required"}), 400
        c = db.consume_sync_token(token)
        if not c:
            return jsonify({"error": "Invalid or expired sync token"}), 401
        # Issue JWT
        payload = {
            "sub":  c["customer_id"],
            "role": "customer",
            "exp":  datetime.utcnow() + timedelta(hours=JWT_EXPIRY_HOURS),
        }
        import jwt as _jwt
        token_out = _jwt.encode(payload, API_SECRET_KEY, algorithm="HS256")
        # Return customer profile snapshot including last temperature
        last_temp = db.get_thermal_stats(c["customer_id"])
        return jsonify({
            "token":       token_out,
            "customer_id": c["customer_id"],
            "name":        c["name"],
            "wallet_tier": c.get("wallet_tier", "G0"),
            "balance":     c.get("balance", 0.0),
            "loyalty_points": c.get("loyalty_points", 0),
            "nhis_active": c.get("nhis_active", 0),
            "identity_verified": c.get("identity_verified", 0),
            "last_temperature": last_temp,
        })

    @app.route("/api/auth/request_reset", methods=["POST"])
    def api_request_reset():
        """Initiate a password reset — returns a reset token for QR display."""
        data = request.get_json() or {}
        cid  = data.get("customer_id", "")
        c    = db.get_customer(cid)
        if not c:
            # Return success anyway — don't reveal account existence
            return jsonify({"success": True, "message": "If account exists, token issued"})
        biometric_svc = BiometricAuthService(db)
        recovery_svc  = PasswordRecoveryService(db, biometric_svc)
        token         = recovery_svc.initiate_qr_reset(cid)
        return jsonify({
            "success": True,
            "reset_token": token,
            "expires_in_mins": RESET_TOKEN_TTL_MINS,
            "message": "Display this as QR on kiosk for customer to scan",
        })

    @app.route("/api/auth/complete_reset", methods=["POST"])
    def api_complete_reset():
        """Complete a password reset after token + face verify."""
        data     = request.get_json() or {}
        token    = data.get("reset_token", "")
        new_pwd  = data.get("new_password", "")
        biometric_svc = BiometricAuthService(db)
        recovery_svc  = PasswordRecoveryService(db, biometric_svc)
        c = recovery_svc.verify_qr_reset(token)
        if not c:
            return jsonify({"error": "Invalid or expired reset token"}), 401
        if not recovery_svc.complete_reset(c["customer_id"], new_pwd):
            return jsonify({"error": "Password too short (min 4 chars)"}), 400
        return jsonify({"success": True, "message": "Password reset complete"})

    @app.route("/api/cupsule/<cupsule_id>", methods=["GET"])
    @_require_auth("admin", "customer")
    def api_get_cupsule(cupsule_id):
        """Lookup a Cupsule by ID. Used by CAPSCAN for validation."""
        c = db.get_cupsule(cupsule_id)
        if not c:
            return jsonify({"error": "Cupsule not found"}), 404
        return jsonify(c)

    @app.route("/api/cupsule/return", methods=["POST"])
    @_require_auth("admin", "customer")
    def api_cupsule_return():
        """Process a Cupsule return — CAPSCAN socket."""
        data       = request.get_json() or {}
        cupsule_id = data.get("cupsule_id", "")
        customer_id= data.get("customer_id", "")
        condition  = data.get("condition", "intact")
        unit_id    = data.get("unit_id", UNIT_ID)
        result     = CupsuleService(db).process_return(
            cupsule_id, customer_id, condition, unit_id)
        if not result["success"]:
            return jsonify(result), 400
        return jsonify(result)

    @app.route("/api/service_bus/status", methods=["GET"])
    @_require_auth("admin")
    def api_bus_status():
        """Show registered products on the Service Bus."""
        return jsonify({
            "registered_services": AidPlusServiceBus.status(),
            "db_registry":         db.get_registered_services(),
        })

    @app.route("/api/mobile/cupsules", methods=["GET"])
    @_require_auth("customer")
    def api_mobile_cupsules():
        """Customer's Cupsule history — issued and returned."""
        cid   = request.actor_id
        cups  = db.get_customer_cupsules(cid)
        stats = CupsuleService(db).get_return_stats(cid)
        return jsonify({"cupsules": cups, "stats": stats})

    # ── BUILD 23: CUPSCAN KIOSK ENDPOINTS ─────────────────────────────────────

    def _verify_cupscan_hmac() -> bool:
        """Verify HMAC-SHA256 on incoming CUPSCAN requests."""
        sig  = request.headers.get("X-AID-Signature", "")
        body = request.get_data()
        msg  = (request.method + request.path).encode() + body
        expected = _hmac.new(HMAC_SECRET, msg, "sha256").hexdigest()
        return _hmac.compare_digest(sig, expected)

    @app.route("/health", methods=["GET"])
    def api_b23_health():
        stats = db.cupscan_platform_stats()
        return jsonify({
            "status":  "ok",
            "product": "Aid System",
            "build":   SCHEMA_VERSION,
            "time":    datetime.now().isoformat(),
            "cupscan": stats,
        })

    @app.route("/api/service/cupscan/register", methods=["POST"])
    def api_cupscan_register():
        if not _verify_cupscan_hmac():
            return jsonify({"ok": False, "error": "invalid_signature"}), 401
        body     = request.get_json(silent=True) or {}
        kiosk_id = body.get("kiosk_id")
        if not kiosk_id:
            return jsonify({"ok": False, "error": "kiosk_id required"}), 400
        db.cupscan_register_kiosk(
            kiosk_id,
            body.get("site_id",   "UNKNOWN"),
            body.get("site_name", ""),
            body.get("firmware",  ""))
        return jsonify({"ok": True, "kiosk_id": kiosk_id,
                        "server_time": datetime.now().isoformat()})

    @app.route("/api/service/cupscan/heartbeat", methods=["POST"])
    def api_cupscan_heartbeat():
        if not _verify_cupscan_hmac():
            return jsonify({"ok": False, "error": "invalid_signature"}), 401
        body     = request.get_json(silent=True) or {}
        kiosk_id = body.get("kiosk_id")
        if not kiosk_id:
            return jsonify({"ok": False, "error": "kiosk_id required"}), 400
        db.cupscan_heartbeat(kiosk_id, body)
        return jsonify({"ok": True, "server_time": datetime.now().isoformat()})

    @app.route("/api/cupsule/return/batch", methods=["POST"])
    def api_cupscan_batch():
        if not _verify_cupscan_hmac():
            return jsonify({"ok": False, "error": "invalid_signature"}), 401
        body     = request.get_json(silent=True) or {}
        kiosk_id = body.get("kiosk_id")
        returns  = body.get("returns", [])
        if not kiosk_id or not isinstance(returns, list):
            return jsonify({"ok": False, "error": "kiosk_id and returns[] required"}), 400
        results = []
        for ret in returns:
            cid     = ret.get("customer_id")
            pts     = int(ret.get("total_pts", 0))
            row_id  = db.cupscan_record_return(kiosk_id, ret)
            pts_res = {"ok": False, "error": "no_customer"}
            if cid:
                pts_res = db.cupscan_apply_points(cid, pts, "CUPSCAN_BATCH")
                if pts_res["ok"]:
                    db.cupscan_increment_daily(cid, int(ret.get("bonus_pts", 0)))
            results.append({"row_id": row_id, "pts_applied": pts_res})
        return jsonify({"ok": True, "processed": len(results), "results": results})

    @app.route("/api/customers/by_card/<card_uid>", methods=["GET"])
    def api_customer_by_card(card_uid):
        if not _verify_cupscan_hmac():
            return jsonify({"ok": False, "error": "invalid_signature"}), 401
        cust = db.cupscan_get_customer_by_card(card_uid)
        if not cust:
            return jsonify({"ok": False, "error": "not_found"}), 404
        daily = db.cupscan_get_daily_count(cust["customer_id"])
        cust["daily_returns"]    = daily["return_count"]
        cust["daily_bonus_pts"]  = daily["bonus_pts_given"]
        cust["daily_return_cap"] = CUPSCAN_DAILY_CAP
        cust["daily_bonus_cap"]  = CUPSCAN_BONUS_CAP_PTS
        return jsonify({"ok": True, "customer": cust})

    @app.route("/api/customers/points_delta", methods=["POST"])
    def api_points_delta():
        if not _verify_cupscan_hmac():
            return jsonify({"ok": False, "error": "invalid_signature"}), 401
        body  = request.get_json(silent=True) or {}
        cid   = body.get("customer_id")
        delta = int(body.get("delta", 0))
        if not cid:
            return jsonify({"ok": False, "error": "customer_id required"}), 400
        result = db.cupscan_apply_points(cid, delta, body.get("reason", "API"))
        return jsonify(result), 200 if result["ok"] else 404

    @app.route("/api/customers/card_updates", methods=["GET"])
    def api_card_updates():
        if not _verify_cupscan_hmac():
            return jsonify({"ok": False, "error": "invalid_signature"}), 401
        since   = request.args.get("since", "1970-01-01T00:00:00")
        updates = db.cupscan_get_card_updates(since)
        return jsonify({"ok": True, "count": len(updates), "updates": updates})

    @app.route("/api/notifications/push", methods=["POST"])
    def api_notifications_push():
        if not _verify_cupscan_hmac():
            return jsonify({"ok": False, "error": "invalid_signature"}), 401
        body    = request.get_json(silent=True) or {}
        cid     = body.get("customer_id")
        product = body.get("product", "UNKNOWN")
        subject = body.get("subject", "Notification")
        message = body.get("message", "")
        if not cid or not message:
            return jsonify({"ok": False, "error": "customer_id and message required"}), 400
        db.send_notification(cid, f"[{product}] {subject}", message)
        return jsonify({"ok": True, "delivered": True})

    @app.route("/api/admin/cupscan/kiosks", methods=["GET"])
    @_require_auth("admin")
    def api_admin_cupscan_kiosks():
        return jsonify({"ok": True, "kiosks": db.cupscan_get_all_kiosks()})

    @app.route("/api/admin/cupscan/stats", methods=["GET"])
    @_require_auth("admin")
    def api_admin_cupscan_stats():
        return jsonify({"ok": True, "stats": db.cupscan_platform_stats()})

    return app


def _build_dashboard_html() -> str:
    """AID PLUS+ Operations Dashboard — professional live management UI."""
    html = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>AID PLUS+ Console</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#f0f2f5;color:#1a202c;font-family:'Segoe UI',system-ui,sans-serif}
a{color:inherit;text-decoration:none}
:root{--green:#00aa44;--green-lt:#f0fff4;--green-dk:#007730;
      --amber:#d97706;--red:#e53e3e;--blue:#3182ce;--purple:#7c3aed;
      --border:#e2e8f0;--bg:#f0f2f5;--card:#fff;--text:#1a202c;--sub:#718096}

header{background:#fff;border-bottom:2px solid var(--border);
  padding:.75rem 2rem;display:flex;align-items:center;
  justify-content:space-between;box-shadow:0 2px 8px rgba(0,0,0,.06);
  position:sticky;top:0;z-index:100}
.brand{display:flex;align-items:center;gap:.75rem}
.brand-icon{width:36px;height:36px;background:var(--green);border-radius:9px;
  color:#fff;font-size:1.1rem;font-weight:900;display:flex;
  align-items:center;justify-content:center}
.brand-name{font-size:1rem;font-weight:800;letter-spacing:.3px}
.brand-sub{font-size:.7rem;color:var(--sub)}
.hdr-right{display:flex;align-items:center;gap:1rem}
.live{display:flex;align-items:center;gap:.35rem;font-size:.78rem;
  font-weight:700;color:#38a169;background:var(--green-lt);
  padding:.28rem .65rem;border-radius:20px;border:1px solid #c6f6d5}
.dot{width:7px;height:7px;background:#38a169;border-radius:50%;
  animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.meta{font-size:.78rem;color:var(--sub)}
.meta strong{color:var(--text)}
.btn-sm{background:#fff;border:1.5px solid var(--border);color:var(--sub);
  padding:.32rem .8rem;border-radius:8px;cursor:pointer;font-size:.78rem;
  transition:all .15s}
.btn-sm:hover{border-color:var(--red);color:var(--red)}

/* LOGIN */
.login-screen{min-height:90vh;display:flex;align-items:center;justify-content:center}
.login-card{background:#fff;border:1px solid var(--border);border-radius:16px;
  padding:2.5rem 2rem;width:360px;box-shadow:0 8px 32px rgba(0,0,0,.08)}
.login-card h2{font-size:1.3rem;font-weight:800;margin-bottom:.25rem}
.login-sub{color:var(--sub);font-size:.82rem;margin-bottom:1.8rem}
.field{margin-bottom:1rem}
label{display:block;font-size:.72rem;font-weight:700;text-transform:uppercase;
  letter-spacing:.6px;color:var(--sub);margin-bottom:.35rem}
input[type=text],input[type=password]{width:100%;padding:.65rem .9rem;
  border:1.5px solid var(--border);border-radius:9px;font-size:.88rem;
  color:var(--text);outline:none;background:#fafafa;transition:border .15s}
input:focus{border-color:var(--green);background:#fff}
.btn-login{width:100%;padding:.75rem;background:var(--green);color:#fff;
  border:none;border-radius:9px;font-weight:700;font-size:.9rem;cursor:pointer;
  transition:all .18s;letter-spacing:.3px}
.btn-login:hover{background:var(--green-dk);box-shadow:0 4px 14px rgba(0,170,68,.3)}
.err{color:var(--red);font-size:.78rem;margin-top:.6rem;text-align:center;
  background:#fff5f5;padding:.35rem .6rem;border-radius:6px;display:none}

/* DASHBOARD */
#dash{display:none;padding:1.5rem 2rem 3rem;max-width:1600px;margin:0 auto}
.dash-top{display:flex;align-items:flex-start;justify-content:space-between;
  margin-bottom:1.4rem;gap:1rem;flex-wrap:wrap}
.dash-top h2{font-size:1.25rem;font-weight:800}
.upd{color:var(--sub);font-size:.75rem;margin-top:.15rem}
.controls{display:flex;gap:.6rem;align-items:center}
.ctrl-sel{background:#fff;border:1.5px solid var(--border);
  padding:.4rem .9rem;border-radius:9px;font-size:.82rem;cursor:pointer;color:var(--text)}
.btn-refresh{display:flex;align-items:center;gap:.35rem;background:#fff;
  border:1.5px solid var(--border);color:var(--sub);padding:.4rem .9rem;
  border-radius:9px;font-size:.82rem;cursor:pointer;transition:all .15s}
.btn-refresh:hover{border-color:var(--green);color:var(--green)}
.spin{display:inline-block;animation:rot .6s linear infinite}
@keyframes rot{to{transform:rotate(360deg)}}

/* KPI STRIP */
.kpis{display:grid;grid-template-columns:repeat(auto-fill,minmax(155px,1fr));
  gap:.85rem;margin-bottom:1.2rem}
.kpi{background:#fff;border:1px solid var(--border);border-radius:14px;
  padding:1rem 1.2rem;cursor:default;transition:transform .12s,box-shadow .12s;
  position:relative;overflow:hidden}
.kpi:hover{transform:translateY(-2px);box-shadow:0 4px 18px rgba(0,0,0,.07)}
.kpi-ico{font-size:1.3rem;margin-bottom:.45rem;line-height:1}
.kpi-lbl{font-size:.67rem;font-weight:700;text-transform:uppercase;
  letter-spacing:.7px;color:var(--sub);margin-bottom:.3rem}
.kpi-val{font-size:1.85rem;font-weight:900;line-height:1}
.kpi-sub{font-size:.7rem;color:var(--sub);margin-top:.2rem}
.kpi-bar{height:3px;background:#f0f0f0;border-radius:2px;margin-top:.85rem}
.kpi-fill{height:100%;border-radius:2px;transition:width .9s}
.kpi.green .kpi-val{color:#276749}.kpi.green .kpi-fill{background:#48bb78}
.kpi.blue  .kpi-val{color:#2b6cb0}.kpi.blue  .kpi-fill{background:#63b3ed}
.kpi.amber .kpi-val{color:#92400e}.kpi.amber .kpi-fill{background:#fbbf24}
.kpi.red   .kpi-val{color:#c53030}.kpi.red   .kpi-fill{background:#fc8181}
.kpi.purp  .kpi-val{color:#6b21a8}.kpi.purp  .kpi-fill{background:#b794f4}

/* GRID LAYOUTS */
.g2{display:grid;grid-template-columns:1fr 1fr;gap:1rem;margin-bottom:1rem}
.g3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:1rem;margin-bottom:1rem}
@media(max-width:960px){.g2,.g3{grid-template-columns:1fr}}

/* PANEL */
.panel{background:#fff;border:1px solid var(--border);border-radius:14px;
  padding:1.1rem 1.3rem;box-shadow:0 1px 4px rgba(0,0,0,.04)}
.ph{display:flex;align-items:center;justify-content:space-between;
  margin-bottom:.9rem;padding-bottom:.65rem;border-bottom:1.5px solid #f5f5f5}
.pt{font-size:.73rem;font-weight:800;text-transform:uppercase;
  letter-spacing:.7px;color:#4a5568;display:flex;align-items:center;gap:.4rem}
.pb{font-size:.63rem;font-weight:700;padding:.14rem .45rem;border-radius:20px;
  background:#edf2f7;color:var(--sub)}

/* TABLE */
table{width:100%;border-collapse:collapse;font-size:.81rem}
th{color:var(--sub);font-weight:700;text-align:left;padding:.35rem .4rem;
  font-size:.67rem;text-transform:uppercase;letter-spacing:.5px;
  border-bottom:1px solid var(--border)}
td{padding:.45rem .4rem;border-bottom:1px solid #f9f9f9;color:#2d3748}
tr:last-child td{border-bottom:none}
tr:hover td{background:#fafafa}

/* BADGE */
.b{display:inline-flex;align-items:center;padding:.15rem .5rem;
  border-radius:20px;font-size:.65rem;font-weight:700;text-transform:uppercase}
.b.ok{background:#f0fff4;color:#276749}
.b.warn{background:#fffbeb;color:#92400e}
.b.err{background:#fff5f5;color:#c53030}
.b.info{background:#ebf8ff;color:#2b6cb0}
.b.purp{background:#faf5ff;color:#6b21a8}
.b.grey{background:#edf2f7;color:#4a5568}

/* STAT ROW */
.sr{display:flex;justify-content:space-between;align-items:center;
  padding:.42rem 0;border-bottom:1px solid #f9f9f9}
.sr:last-child{border-bottom:none}
.sk{font-size:.8rem;color:#4a5568}
.sv{font-size:.81rem;font-weight:700;color:var(--text)}

/* GAUGE */
.gauge{margin:.4rem 0}
.g-lbl{display:flex;justify-content:space-between;font-size:.75rem;
  color:#4a5568;margin-bottom:.25rem}
.g-bar{height:7px;background:#f0f0f0;border-radius:4px;overflow:hidden}
.g-fill{height:100%;border-radius:4px;transition:width 1s}
.g-fill.ok{background:linear-gradient(90deg,#48bb78,#38a169)}
.g-fill.low{background:linear-gradient(90deg,#fbbf24,#d97706)}
.g-fill.crit{background:linear-gradient(90deg,#fc8181,#e53e3e)}

/* HW CARDS */
.hw-g{display:grid;grid-template-columns:1fr 1fr;gap:.7rem}
.hw-c{background:#f8fafc;border:1.5px solid var(--border);border-radius:10px;
  padding:.85rem;text-align:center}
.hw-n{font-size:.68rem;font-weight:700;text-transform:uppercase;
  letter-spacing:.5px;color:var(--sub);margin-bottom:.3rem}
.hw-s{font-size:.95rem;font-weight:800}
.hw-s.dock{color:#38a169}.hw-s.dep{color:var(--red)}
.hw-u{font-size:.68rem;color:var(--sub);margin-top:.2rem}

/* INSIGHT ROW */
.ins{display:flex;gap:.6rem;align-items:flex-start;padding:.5rem 0;
  border-bottom:1px solid #f5f5f5}
.ins:last-child{border-bottom:none}
.ins-ic{width:30px;height:30px;border-radius:8px;background:#ebf8ff;
  display:flex;align-items:center;justify-content:center;flex-shrink:0;font-size:.85rem}
.ins-t{font-size:.81rem;font-weight:600;color:var(--text)}
.ins-d{font-size:.73rem;color:var(--sub);margin-top:.1rem}

/* NO DATA */
.nd{color:var(--sub);font-style:italic;font-size:.8rem;
  padding:.7rem 0;text-align:center}

/* SECTION DIVIDER */
.sec-hdr{font-size:.72rem;font-weight:800;text-transform:uppercase;
  letter-spacing:.8px;color:var(--sub);margin:1.2rem 0 .7rem;
  display:flex;align-items:center;gap:.5rem}
.sec-hdr::after{content:'';flex:1;height:1px;background:var(--border)}

/* FUND PANEL */
.fund-row{display:flex;align-items:center;justify-content:space-between;
  padding:.55rem 0;border-bottom:1px solid #f5f5f5}
.fund-row:last-child{border-bottom:none}
.fund-key{font-size:.81rem;color:#4a5568;display:flex;align-items:center;gap:.4rem}
.fund-val{font-size:.88rem;font-weight:800;color:var(--text)}
.fund-sub{font-size:.68rem;color:var(--sub)}
</style>
</head>
<body>

<header>
  <div class="brand">
    <div class="brand-icon">+</div>
    <div>
      <div class="brand-name">AID PLUS+</div>
      <div class="brand-sub">Adwene ADW-1 &nbsp;·&nbsp; Nyansa v8.0 &nbsp;·&nbsp; Management Console</div>
    </div>
  </div>
  <div class="hdr-right">
    <div class="live"><span class="dot"></span>Live</div>
    <div class="meta">Unit: <strong id="H-unit">—</strong></div>
    <div class="meta">Build: <strong id="H-build">—</strong></div>
    <div class="meta" id="H-time">—</div>
    <button class="btn-sm" onclick="signOut()">Sign out</button>
  </div>
</header>

<!-- ═══ LOGIN ═══ -->
<div class="login-screen" id="S-login">
  <div class="login-card">
    <h2>Management Console</h2>
    <p class="login-sub">Enter your administrator credentials to continue</p>
    <div class="field"><label>Username</label>
      <input type="text" id="L-id" value="admin" placeholder="admin"></div>
    <div class="field"><label>Password</label>
      <input type="password" id="L-pw" placeholder="Password"
             onkeydown="if(event.key==='Enter')login()"></div>
    <button class="btn-login" onclick="login()">Sign In &rarr;</button>
    <div class="err" id="L-err"></div>
  </div>
</div>

<!-- ═══ DASHBOARD ═══ -->
<div id="dash">
  <div class="dash-top">
    <div><h2>Operations Dashboard</h2>
      <div class="upd" id="D-upd">Loading…</div></div>
    <div class="controls">
      <select class="ctrl-sel" id="D-unit" onchange="refresh()">
        <option value="">All Units</option></select>
      <button class="btn-refresh" onclick="refresh()" id="D-btn">
        <span id="D-spin">↻</span> Refresh</button>
    </div>
  </div>

  <div class="kpis" id="D-kpis"></div>

  <div class="sec-hdr">⚡ Infrastructure</div>
  <div class="g2">
    <div class="panel">
      <div class="ph"><div class="pt">⚡ Power &amp; Connectivity</div></div>
      <div id="P-power"></div>
    </div>
    <div class="panel">
      <div class="ph">
        <div class="pt">📦 Hardware Status</div>
        <span class="pb" id="P-hw-badge">—</span>
      </div>
      <div id="P-hw"></div>
    </div>
  </div>

  <div class="sec-hdr">💊 Pharmacy</div>
  <div class="g2">
    <div class="panel">
      <div class="ph">
        <div class="pt">⚠ Low Stock Alerts</div>
        <span class="pb" id="P-stk-badge">—</span>
      </div>
      <div id="P-stk"></div>
    </div>
    <div class="panel">
      <div class="ph">
        <div class="pt">🔄 Restock Orders</div>
        <span class="pb" id="P-rst-badge">—</span>
      </div>
      <div id="P-rst"></div>
    </div>
  </div>

  <div class="sec-hdr">🧠 Intelligence</div>
  <div class="g2">
    <div class="panel">
      <div class="ph">
        <div class="pt">🧠 Nyansa Insights</div>
        <span class="pb" id="P-ins-badge">—</span>
      </div>
      <div id="P-ins"></div>
    </div>
    <div class="panel">
      <div class="ph">
        <div class="pt">👁 CLTS Session Analytics</div>
      </div>
      <div id="P-clts"></div>
    </div>
  </div>

  <div class="sec-hdr">🏥 Clinical Operations</div>
  <div class="g2">
    <div class="panel">
      <div class="ph"><div class="pt">🎫 Support Tickets</div></div>
      <div id="P-tkt"></div>
    </div>
    <div class="panel">
      <div class="ph"><div class="pt">🩺 Teleconsult Queue</div></div>
      <div id="P-con"></div>
    </div>
  </div>

  <div class="sec-hdr">💰 Financials</div>
  <div class="g3">
    <div class="panel">
      <div class="ph"><div class="pt">📈 Revenue</div></div>
      <div id="P-rev"></div>
    </div>
    <div class="panel">
      <div class="ph"><div class="pt">🔒 Maintenance Fund</div></div>
      <div id="P-fund"></div>
    </div>
    <div class="panel">
      <div class="ph"><div class="pt">📅 Annual Projections</div></div>
      <div id="P-proj"></div>
    </div>
  </div>
</div>

<script>
let TOK=null;
const $=id=>document.getElementById(id);
const gc=n=>typeof n==='number'?'₵'+n.toFixed(2):n||'—';
const gn=n=>typeof n==='number'?n.toLocaleString():n||0;

async function login(){
  const id=$('L-id').value.trim(), pw=$('L-pw').value.trim();
  $('L-err').style.display='none';
  try{
    const r=await fetch('/api/auth/login',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({id,password:pw})});
    const d=await r.json();
    if(!r.ok){$('L-err').textContent=d.error||'Login failed';$('L-err').style.display='block';return;}
    TOK=d.token;
    $('S-login').style.display='none';
    $('dash').style.display='block';
    loadUnits();refresh();
    setInterval(refresh,30000);
  }catch(e){$('L-err').textContent='Connection error';$('L-err').style.display='block';}
}
function signOut(){TOK=null;$('S-login').style.display='flex';$('dash').style.display='none';$('L-pw').value='';}
async function apiFetch(url){
  const r=await fetch(url,{headers:{'Authorization':'Bearer '+TOK}});
  return r.ok?r.json():null;
}
async function loadUnits(){
  const d=await apiFetch('/api/units');if(!d)return;
  const sel=$('D-unit');
  (d.units||[]).forEach(u=>{const o=document.createElement('option');
    o.value=u.unit_id;o.textContent=u.unit_name+' — '+(u.location||u.unit_id);
    sel.appendChild(o);});
}
async function refresh(){
  $('D-spin').className='spin';$('D-btn').disabled=true;
  const uid=$('D-unit').value;
  const d=await apiFetch('/api/dashboard/data'+(uid?'?unit_id='+uid:''));
  $('D-spin').className='';$('D-btn').disabled=false;
  if(!d){$('D-upd').textContent='Failed to load data';return;}

  $('H-unit').textContent=d.unit_id||'—';
  $('H-build').textContent='Build '+d.schema_version;
  $('H-time').textContent=new Date(d.timestamp).toLocaleTimeString();
  $('D-upd').textContent='Last updated '+new Date(d.timestamp).toLocaleString();

  // ── KPIs ──
  const ls=d.low_stock_count||0, od=d.tickets?.overdue||0;
  const bat=d.battery_pct||100;
  const kpis=[
    {ico:'💰',lbl:"Today's Revenue", val:gc(d.revenue_today), cls:'green', bar:Math.min(100,(d.revenue_today||0)/20*100), sub:'GHS'},
    {ico:'📊',lbl:'Total Revenue',   val:gc(d.total_revenue), cls:'blue',  bar:65, sub:'all time'},
    {ico:'🏪',lbl:'Stock Value',     val:gc(d.stock_value),   cls:'blue',  bar:70, sub:'inventory'},
    {ico:'⚠',lbl:'Low Stock',        val:ls,                  cls:ls>0?'red':'green', bar:Math.min(100,ls*12), sub:'items'},
    {ico:'🔋',lbl:'Battery',         val:bat.toFixed(0)+'%',  cls:bat>40?'green':bat>15?'amber':'red', bar:bat, sub:d.power_source||'—'},
    {ico:'🎫',lbl:'Open Tickets',    val:(d.tickets?.open||0)+(d.tickets?.in_progress||0), cls:od>0?'red':'amber', bar:30, sub:od+' overdue'},
    {ico:'🩺',lbl:'Consult Queue',   val:d.consult_queue?.waiting||0, cls:'amber', bar:25, sub:'waiting'},
    {ico:'🧠',lbl:'Insights',        val:d.insights?.pending_insights?.length||0, cls:'purp', bar:50, sub:'pending'},
    {ico:'🎯',lbl:'Active Promos',   val:d.active_promos||0,  cls:'green', bar:60, sub:'running'},
    {ico:'📦',lbl:'Restock Orders',  val:d.restock_pending||0,cls:d.restock_pending>0?'amber':'green', bar:30, sub:'pending'},
  ];
  $('D-kpis').innerHTML=kpis.map(k=>`<div class="kpi ${k.cls}">
    <div class="kpi-ico">${k.ico}</div>
    <div class="kpi-lbl">${k.lbl}</div>
    <div class="kpi-val">${k.val}</div>
    <div class="kpi-sub">${k.sub}</div>
    <div class="kpi-bar"><div class="kpi-fill" style="width:${Math.min(100,k.bar||0)}%"></div></div>
  </div>`).join('');

  // ── Power ──
  const bc=bat>40?'ok':bat>15?'low':'crit';
  $('P-power').innerHTML=`
    <div class="gauge"><div class="g-lbl"><span>Battery Level</span><span>${bat.toFixed(0)}%</span></div>
    <div class="g-bar"><div class="g-fill ${bc}" style="width:${bat}%"></div></div></div>
    <div class="sr"><span class="sk">Power Source</span><span class="sv"><span class="b ${d.power_source==='MAINS'?'ok':'info'}">${d.power_source||'SOLAR+BATTERY'}</span></span></div>
    <div class="sr"><span class="sk">Connectivity</span><span class="sv"><span class="b ${(d.connectivity||'').includes('Online')||d.connectivity==='WiFi'?'ok':'warn'}">${d.connectivity||'Checking…'}</span></span></div>
    <div class="sr"><span class="sk">Offline Queue</span><span class="sv">${d.offline_queue||0} requests</span></div>`;

  // ── Hardware ──
  const hw=d.hw_status||{};
  const aS=(hw.aid_box_status||'Unknown').toLowerCase();
  const cS=(hw.cpr_kit_status||'Unknown').toLowerCase();
  const aU=hw.aid_box_usage||0, cU=hw.cpr_kit_usage||0;
  $('P-hw-badge').textContent=(aS==='deployed'||cS==='deployed')?'Active':'All Docked';
  $('P-hw').innerHTML=`<div class="hw-g">
    <div class="hw-c"><div class="hw-n">Aid Box</div>
      <div class="hw-s ${aS==='docked'?'dock':'dep'}">${aS==='docked'?'✓ Docked':'⚠ Deployed'}</div>
      <div class="hw-u">${aU}/5 uses${aU>=4?' · <span style="color:var(--red)">Sterilize soon</span>':''}</div></div>
    <div class="hw-c"><div class="hw-n">CPR Kit</div>
      <div class="hw-s ${cS==='docked'?'dock':'dep'}">${cS==='docked'?'✓ Docked':'⚠ Deployed'}</div>
      <div class="hw-u">${cU}/5 uses${cU>=4?' · <span style="color:var(--red)">Sterilize soon</span>':''}</div></div>
  </div>`;

  // ── Low Stock ──
  const stk=d.low_stock_items||[];
  $('P-stk-badge').textContent=ls+' items';
  $('P-stk').innerHTML=ls===0?'<div class="nd">✓ All shelves adequately stocked</div>'
    :`<table><thead><tr><th>Drug</th><th>Shelf</th><th>Stock</th><th>Status</th></tr></thead><tbody>
    ${stk.map(i=>`<tr><td>${i.name}</td><td>${i.shelf}</td><td><strong>${i.stock}</strong></td>
    <td><span class="b ${i.stock===0?'err':'warn'}">${i.stock===0?'Empty':'Low'}</span></td></tr>`).join('')}
    </tbody></table>`;

  // ── Restock ──
  $('P-rst-badge').textContent=(d.restock_pending||0)+' pending';
  $('P-rst').innerHTML=`
    <div class="sr"><span class="sk">Pending Orders</span><span class="sv"><span class="b ${(d.restock_pending||0)>0?'warn':'ok'}">${d.restock_pending||0}</span></span></div>
    <div class="sr"><span class="sk">Active Promotions</span><span class="sv">${d.active_promos||0}</span></div>
    <div class="sr"><span class="sk">Low Stock Items</span><span class="sv"><span class="b ${ls>0?'err':'ok'}">${ls}</span></span></div>`;

  // ── Insights ──
  const ins=d.insights?.pending_insights||[];
  $('P-ins-badge').textContent=ins.length;
  $('P-ins').innerHTML=ins.length===0?'<div class="nd">✓ No pending insights — system healthy</div>'
    :ins.slice(0,5).map(i=>`<div class="ins">
      <div class="ins-ic">🔍</div>
      <div><div class="ins-t">${i.title||i.insight_type||'Insight'}</div>
      <div class="ins-d">${i.recommended_action||i.description||'—'}</div></div></div>`).join('');

  // ── CLTS ──
  const cl=d.clts_stats||{};
  const gb=(cl.gender_breakdown||[]).map(g=>`${g.detected_gender}: ${g.cnt}`).join(' · ')||'No data yet';
  $('P-clts').innerHTML=`
    <div class="sr"><span class="sk">Total Sessions</span><span class="sv">${cl.total_sessions||0}</span></div>
    <div class="sr"><span class="sk">Face Matched</span><span class="sv">${cl.face_matched||0}</span></div>
    <div class="sr"><span class="sk">Led to Purchase</span><span class="sv">${cl.led_to_purchase||0}</span></div>
    <div class="sr"><span class="sk">Avg Temperature</span><span class="sv">${cl.avg_temp?(cl.avg_temp.toFixed(1)+'°C'):'—'}</span></div>
    <div class="sr"><span class="sk">Gender Split</span><span class="sv" style="font-size:.74rem;color:var(--sub)">${gb}</span></div>`;

  // ── Tickets ──
  const tk=d.tickets||{};
  $('P-tkt').innerHTML=Object.keys(tk).length===0?'<div class="nd">No ticket data</div>'
    :`<table><thead><tr><th>Status</th><th>Count</th></tr></thead><tbody>
    ${Object.entries(tk).map(([k,v])=>`<tr><td style="text-transform:capitalize">${k.replace('_',' ')}</td>
    <td><span class="b ${k==='overdue'?'err':k==='resolved'?'ok':'warn'}">${v}</span></td></tr>`).join('')}
    </tbody></table>`;

  // ── Teleconsult ──
  const cq=d.consult_queue||{};
  $('P-con').innerHTML=`
    <div class="sr"><span class="sk">Waiting Now</span><span class="sv"><span class="b ${(cq.waiting||0)>0?'warn':'ok'}">${cq.waiting||0}</span></span></div>
    <div class="sr"><span class="sk">Completed Today</span><span class="sv">${cq.today||0}</span></div>
    <div class="sr"><span class="sk">All-time Total</span><span class="sv">${cq.total_all_time||0}</span></div>`;

  // ── Revenue ──
  const rev=d.revenue_today||0, tot=d.total_revenue||0;
  $('P-rev').innerHTML=`
    <div class="sr"><span class="sk">Today</span><span class="sv" style="color:#276749;font-size:1rem">${gc(rev)}</span></div>
    <div class="sr"><span class="sk">All Time</span><span class="sv">${gc(tot)}</span></div>
    <div class="sr"><span class="sk">Stock Value</span><span class="sv">${gc(d.stock_value)}</span></div>
    <div class="sr"><span class="sk">HW Net Revenue</span><span class="sv">${gc(d.hw_net_revenue)}</span></div>
    <div class="sr"><span class="sk">Bonus Paid Out</span><span class="sv">${gc(d.bonus_paid_out)}</span></div>`;

  // ── Maintenance Fund ──
  const mf=d.maintenance_fund||{};
  $('P-fund').innerHTML=mf.balance===undefined?'<div class="nd">No data</div>':`
    <div class="sr"><span class="sk">Fund Balance</span><span class="sv" style="color:#276749;font-size:1rem">${gc(mf.balance)}</span></div>
    <div class="sr"><span class="sk">Contributions (Lifetime)</span><span class="sv">${gc(mf.total_contributed)}</span></div>
    <div class="sr"><span class="sk">% of HW Revenue</span><span class="sv">${mf.rate||15}% set aside</span></div>
    <div class="sr"><span class="sk">Last Contribution</span><span class="sv" style="font-size:.75rem">${mf.last_entry||'—'}</span></div>
    <div class="sr"><span class="sk">Aid Box Sterilizes</span><span class="sv">${mf.aid_box_sterilize_count||0} events</span></div>
    <div class="sr"><span class="sk">CPR Kit Sterilizes</span><span class="sv">${mf.cpr_kit_sterilize_count||0} events</span></div>`;

  // ── Annual Projections ──
  const ap=d.annual_projections||{};
  $('P-proj').innerHTML=ap.drug_revenue_12m===undefined?'<div class="nd">Insufficient data for projection (need 30+ days)</div>':`
    <div class="sr"><span class="sk">Drug Revenue (12m)</span><span class="sv">${gc(ap.drug_revenue_12m)}</span></div>
    <div class="sr"><span class="sk">Hardware Revenue (12m)</span><span class="sv">${gc(ap.hw_revenue_12m)}</span></div>
    <div class="sr"><span class="sk">Maintenance Cost (12m)</span><span class="sv" style="color:var(--red)">${gc(ap.maintenance_cost_12m)}</span></div>
    <div class="sr"><span class="sk">Bonus Projected (12m)</span><span class="sv">${gc(ap.bonus_projected_12m)}</span></div>
    <div class="sr"><span class="sk">Net Projected (12m)</span><span class="sv" style="color:${ap.net_12m>=0?'#276749':'var(--red)'}"><strong>${gc(ap.net_12m)}</strong></span></div>
    <div class="sr"><span class="sk">Based on</span><span class="sv" style="font-size:.72rem;color:var(--sub)">${ap.data_days||0} days of data</span></div>`;
}
</script>
</body>
</html>"""
    from flask import Response
    return Response(html, mimetype="text/html")




class APIServer:
    """Manages Flask API lifecycle — start/stop, status, thread management."""
    def __init__(self, db: "DatabaseManager"):
        self.db       = db
        self._app     = None
        self._thread  = None
        self._running = False

    def start(self, host: str = API_HOST, port: int = API_PORT) -> dict:
        if self._running:
            return {"success": True, "message": "Already running.",
                    "url": self.url, "dashboard": self.url + "/dashboard"}
        try:
            self._app    = _build_flask_app(self.db)
            self._thread = __import__("threading").Thread(
                target=lambda: self._app.run(host=host, port=port,
                                             debug=False, use_reloader=False),
                daemon=True)
            self._thread.start()
            self._running = True
            return {"success": True,
                    "message": f"API server started on port {port}.",
                    "url": self.url,
                    "dashboard": self.url + "/dashboard"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def stop(self):
        self._running = False

    def status(self) -> dict:
        return {
            "running":   self._running,
            "url":       self.url if self._running else None,
            "dashboard": (self.url + "/dashboard") if self._running else None,
            "port":      API_PORT,
        }

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def url(self) -> str:
        return f"http://localhost:{API_PORT}"
