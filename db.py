"""
AID PLUS+ — Database Manager
==============================
The single data access layer for the entire system.
All SQL operations live here. Nothing else touches the database directly.

Build 28 — 51 tables, WAL journal, FK enforcement, schema migration engine.
"""
from __future__ import annotations
import sqlite3
import os
import json
import csv
import random
import time
import threading
from datetime import datetime, timedelta

from aidplus.config import *
from aidplus.security import hash_password, verify_password, secure_id, secure_ref

class DatabaseManager:
    """
    Single access point for every database operation.

    Schema overview (18 tables, 0 JSON columns, schema_version tracking):
    ┌──────────────────────────────┬─────────────────────────────────────────┐
    │ Table                        │ Purpose                                 │
    ├──────────────────────────────┼─────────────────────────────────────────┤
    │ schema_version               │ Tracks applied migration level [A1]     │
    │ inventory                    │ Upper-shelf capsules/pills              │
    │ mega_inventory               │ Lower-shelf syrups/liquids              │
    │ customers                    │ Core account fields + password_salt [S1]│
    │ customer_face_signatures     │ Biometric float vector (128 rows/user)  │
    │ customer_health_trends       │ Timestamped temp/weight readings        │
    │ customer_cart                │ Live shopping cart rows                 │
    │ notifications                │ System inbox messages                   │
    │ wallet_history               │ Every debit/credit event                │
    │ transactions                 │ Purchase receipt headers                │
    │ transaction_items            │ Line items per receipt                  │
    │ feedback                     │ Customer-submitted reports              │
    │ returned_items               │ Return counts per drug                  │
    │ return_photos                │ Audit photo paths per return            │
    │ hardware_status              │ Kit dock state & revenue counters       │
    │ active_hardware_codes        │ Live deployment tokens                  │
    │ emergency_logs               │ Full deploy/return audit trail          │
    │ pdms_audit_log               │ Immutable compliance audit trail [A2]   │
    └──────────────────────────────┴─────────────────────────────────────────┘
    """

    def __init__(self, db_path: str = DB_FILE):
        self.db_path = db_path
        self._init_schema()
        self._migrate()          # [A1] apply any pending schema upgrades
        self._fix_active_hardware_codes_pk()
        self._migrate_medication_returns()
        self._seed_inventory()
        self._seed_hardware()
        self._reset_hardware_to_docked()

    # ── Connection factory ─────────────────────────────────────────────────────
    def _conn(self) -> sqlite3.Connection:
        # timeout=10 — retry for up to 10s if DB is locked by another connection
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys  = ON")
        conn.execute("PRAGMA journal_mode  = WAL")
        conn.execute("PRAGMA synchronous   = NORMAL")
        conn.execute("PRAGMA temp_store    = MEMORY")
        conn.execute("PRAGMA busy_timeout  = 10000")  # 10s busy wait
        return conn

    # ══════════════════════════════════════════════════════════════════════════
    # SCHEMA  [A1]
    # ══════════════════════════════════════════════════════════════════════════
    def _init_schema(self):
        with self._conn() as con:
            con.executescript("""
            -- ── Schema version tracking ──────────────────────────────────────
            -- Nyansa consumption tracking per purchase item
            CREATE TABLE IF NOT EXISTS consumption_logs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id     TEXT NOT NULL,
                transaction_id  TEXT NOT NULL,
                drug_name       TEXT NOT NULL,
                qty_purchased   INTEGER NOT NULL,
                pills_per_unit  INTEGER NOT NULL DEFAULT 1,
                dose_per_day    REAL    NOT NULL DEFAULT 1.0,
                timing          TEXT    NOT NULL DEFAULT "morning",
                expected_days   REAL    NOT NULL,
                purchase_date   TEXT    NOT NULL,
                has_prescription INTEGER NOT NULL DEFAULT 0,
                prescription_ref TEXT,
                doses_reported  INTEGER NOT NULL DEFAULT 0,
                adherence_pct   REAL    NOT NULL DEFAULT 0.0,
                risk_level      TEXT    NOT NULL DEFAULT "green",
                notes           TEXT,
                last_updated    TEXT    NOT NULL
            );
            CREATE TABLE IF NOT EXISTS consumption_qa (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id     TEXT NOT NULL,
                drug_name       TEXT NOT NULL,
                question        TEXT NOT NULL,
                answer          TEXT NOT NULL,
                asked_at        TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER NOT NULL DEFAULT 0
            );

            -- ── Inventory ────────────────────────────────────────────────────
            CREATE TABLE IF NOT EXISTS inventory (
                drug_id       INTEGER PRIMARY KEY,
                name          TEXT    NOT NULL,
                shelf         INTEGER UNIQUE NOT NULL,
                is_mega       INTEGER NOT NULL DEFAULT 0
                              CHECK (is_mega = 0),
                current_price REAL    NOT NULL CHECK (current_price >= 0),
                base_price    REAL    NOT NULL CHECK (base_price    >= 0),
                capsules_left INTEGER NOT NULL DEFAULT 150
                              CHECK (capsules_left >= 0),
                last_updated  TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS mega_inventory (
                drug_id       INTEGER PRIMARY KEY,
                name          TEXT    NOT NULL,
                shelf         INTEGER UNIQUE NOT NULL,
                is_mega       INTEGER NOT NULL DEFAULT 1
                              CHECK (is_mega = 1),
                current_price REAL    NOT NULL CHECK (current_price >= 0),
                base_price    REAL    NOT NULL CHECK (base_price    >= 0),
                units_left    INTEGER NOT NULL DEFAULT 50
                              CHECK (units_left >= 0),
                last_updated  TEXT    NOT NULL
            );

            -- ── Customers  (password_salt added via migration if upgrading) ──
            CREATE TABLE IF NOT EXISTS customers (
                customer_id               TEXT PRIMARY KEY,
                name                      TEXT NOT NULL,
                dob                       TEXT NOT NULL,
                age                       INTEGER NOT NULL CHECK (age >= 0),
                gender                    TEXT,
                address                   TEXT,
                password                  TEXT NOT NULL,
                password_salt             TEXT,
                email                     TEXT DEFAULT '',
                contact                   TEXT DEFAULT '',
                -- ── Security ──────────────────────────────────────────────
                security_question         TEXT DEFAULT '',
                security_answer           TEXT DEFAULT '',
                security_answer_salt      TEXT DEFAULT '',
                -- ── Health profile ────────────────────────────────────────
                health_info               TEXT DEFAULT '',
                health_status             TEXT NOT NULL DEFAULT 'Unknown',
                health_consent            INTEGER NOT NULL DEFAULT 0
                                          CHECK (health_consent IN (0,1)),
                blood_group               TEXT DEFAULT '',
                allergies                 TEXT DEFAULT '',
                chronic_conditions        TEXT DEFAULT '',
                current_medications       TEXT DEFAULT '',
                -- ── NHIS ──────────────────────────────────────────────────
                nhis_active               INTEGER NOT NULL DEFAULT 0
                                          CHECK (nhis_active IN (0,1)),
                nhis_number               TEXT DEFAULT '',
                nhis_last_verified        TEXT,
                nhis_session_active       INTEGER NOT NULL DEFAULT 0
                                          CHECK (nhis_session_active IN (0,1)),
                -- ── Identity verification ─────────────────────────────────
                ghana_card_number         TEXT DEFAULT '',
                ghana_card_verified       INTEGER NOT NULL DEFAULT 0
                                          CHECK (ghana_card_verified IN (0,1)),
                identity_verified         INTEGER NOT NULL DEFAULT 0
                                          CHECK (identity_verified IN (0,1)),
                -- ── Wallet ────────────────────────────────────────────────
                balance                   REAL NOT NULL DEFAULT 0.0
                                          CHECK (balance >= 0),
                bonus                     REAL NOT NULL DEFAULT 0.0
                                          CHECK (bonus   >= 0),
                wallet_tier               TEXT NOT NULL DEFAULT 'G0',
                loyalty_points            INTEGER NOT NULL DEFAULT 0,
                lifetime_points           INTEGER NOT NULL DEFAULT 0,
                return_count              INTEGER NOT NULL DEFAULT 0,
                -- ── Card & access ─────────────────────────────────────────
                card_type                 TEXT NOT NULL DEFAULT 'digital',
                card_uid                  TEXT UNIQUE,
                has_physical_card         INTEGER NOT NULL DEFAULT 0
                                          CHECK (has_physical_card IN (0,1)),
                status                    TEXT NOT NULL DEFAULT 'Active'
                                          CHECK (status IN ('Active','Suspended','Terminated')),
                login_attempts            INTEGER NOT NULL DEFAULT 0,
                lockout_until             TEXT,
                prescription_active_until TEXT,
                -- ── Preferences ───────────────────────────────────────────
                voice_guidance            INTEGER NOT NULL DEFAULT 0
                                          CHECK (voice_guidance IN (0,1)),
                notification_email        INTEGER NOT NULL DEFAULT 0
                                          CHECK (notification_email IN (0,1)),
                notification_sms          INTEGER NOT NULL DEFAULT 0
                                          CHECK (notification_sms  IN (0,1)),
                -- ── Mobile sync token ──────────────────────────────────────────
                sync_token                TEXT,
                sync_token_expires        TEXT
            );

            -- ── Face biometrics ───────────────────────────────────────────────
            CREATE TABLE IF NOT EXISTS customer_face_signatures (
                customer_id TEXT    NOT NULL,
                position    INTEGER NOT NULL CHECK (position BETWEEN 0 AND 127),
                value       REAL    NOT NULL,
                PRIMARY KEY (customer_id, position),
                FOREIGN KEY (customer_id)
                    REFERENCES customers(customer_id) ON DELETE CASCADE
            );

            -- ── Health trends ─────────────────────────────────────────────────
            CREATE TABLE IF NOT EXISTS customer_health_trends (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id TEXT    NOT NULL,
                trend_type  TEXT    NOT NULL CHECK (trend_type IN ('temp','weight')),
                value       REAL    NOT NULL,
                recorded_at TEXT    NOT NULL,
                FOREIGN KEY (customer_id)
                    REFERENCES customers(customer_id) ON DELETE CASCADE
            );

            -- ── Shopping cart ─────────────────────────────────────────────────
            CREATE TABLE IF NOT EXISTS customer_cart (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id           TEXT    NOT NULL,
                shelf_num             INTEGER NOT NULL,
                name                  TEXT    NOT NULL,
                qty                   INTEGER NOT NULL CHECK (qty > 0),
                price_per             REAL    NOT NULL CHECK (price_per  >= 0),
                base_price            REAL    NOT NULL CHECK (base_price >= 0),
                is_mega_item          INTEGER NOT NULL DEFAULT 0
                                      CHECK (is_mega_item IN (0,1)),
                nhis_discounted       INTEGER NOT NULL DEFAULT 0
                                      CHECK (nhis_discounted IN (0,1)),
                nhis_discounted_primary INTEGER NOT NULL DEFAULT 0
                                      CHECK (nhis_discounted_primary IN (0,1)),
                UNIQUE (customer_id, shelf_num),
                FOREIGN KEY (customer_id)
                    REFERENCES customers(customer_id) ON DELETE CASCADE
            );

            -- ── Notifications ─────────────────────────────────────────────────
            CREATE TABLE IF NOT EXISTS notifications (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id TEXT    NOT NULL,
                timestamp   TEXT    NOT NULL,
                tag         TEXT,
                message     TEXT,
                FOREIGN KEY (customer_id)
                    REFERENCES customers(customer_id) ON DELETE CASCADE
            );

            -- ── Wallet history ────────────────────────────────────────────────
            CREATE TABLE IF NOT EXISTS wallet_history (
                id            TEXT PRIMARY KEY,
                customer_id   TEXT NOT NULL,
                type          TEXT NOT NULL,
                timestamp     TEXT NOT NULL,
                created_at    TEXT NOT NULL DEFAULT (datetime('now')),
                amount        REAL NOT NULL,
                balance_after REAL NOT NULL DEFAULT 0.0,
                description   TEXT DEFAULT '',
                note          TEXT DEFAULT '',
                FOREIGN KEY (customer_id)
                    REFERENCES customers(customer_id) ON DELETE CASCADE
            );

            -- ── Transactions ──────────────────────────────────────────────────
            CREATE TABLE IF NOT EXISTS transactions (
                id             TEXT PRIMARY KEY,
                transaction_id TEXT UNIQUE,
                customer_id    TEXT NOT NULL,
                timestamp      TEXT NOT NULL,
                badge          TEXT,
                total          REAL NOT NULL DEFAULT 0.0 CHECK (total        >= 0),
                nhis_savings   REAL NOT NULL DEFAULT 0.0 CHECK (nhis_savings >= 0),
                status         TEXT NOT NULL DEFAULT 'complete',
                unit_id        TEXT NOT NULL DEFAULT 'ADW-UNSET'
            );

            CREATE TABLE IF NOT EXISTS transaction_items (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                transaction_id  TEXT    NOT NULL,
                name            TEXT    NOT NULL,
                shelf_num       INTEGER NOT NULL,
                qty             INTEGER NOT NULL CHECK (qty      > 0),
                price_per       REAL    NOT NULL CHECK (price_per  >= 0),
                base_price      REAL    NOT NULL CHECK (base_price >= 0),
                is_mega_item    INTEGER NOT NULL DEFAULT 0
                                CHECK (is_mega_item    IN (0,1)),
                nhis_discounted INTEGER NOT NULL DEFAULT 0
                                CHECK (nhis_discounted IN (0,1)),
                FOREIGN KEY (transaction_id)
                    REFERENCES transactions(id) ON DELETE CASCADE
            );

            -- ── Feedback ──────────────────────────────────────────────────────
            CREATE TABLE IF NOT EXISTS feedback (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT NOT NULL,
                customer_id TEXT NOT NULL,
                name        TEXT,
                tag         TEXT,
                message     TEXT NOT NULL
            );

            -- ── Returns ───────────────────────────────────────────────────────
            CREATE TABLE IF NOT EXISTS returned_items (
                drug_id INTEGER PRIMARY KEY,
                count   INTEGER NOT NULL DEFAULT 0 CHECK (count >= 0)
            );
            -- Per-transaction medication return tracking
            -- Multiple rows per item allowed (partial returns)
            CREATE TABLE IF NOT EXISTS medication_returns (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                transaction_id  TEXT NOT NULL,
                item_id         INTEGER NOT NULL DEFAULT 0,
                item_name       TEXT NOT NULL DEFAULT '',
                qty_returned    INTEGER NOT NULL DEFAULT 1,
                customer_id     TEXT NOT NULL,
                returned_at     TEXT NOT NULL,
                amount_refunded REAL NOT NULL DEFAULT 0.0,
                status          TEXT NOT NULL DEFAULT 'refunded'
            );

            CREATE TABLE IF NOT EXISTS return_photos (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                drug_id     INTEGER NOT NULL,
                photo_path  TEXT    NOT NULL,
                captured_at TEXT    NOT NULL,
                FOREIGN KEY (drug_id)
                    REFERENCES returned_items(drug_id) ON DELETE CASCADE
            );

            -- ── Hardware ──────────────────────────────────────────────────────
            CREATE TABLE IF NOT EXISTS hardware_status (
                id                  INTEGER PRIMARY KEY DEFAULT 1,
                aid_box_status      TEXT NOT NULL DEFAULT 'Docked'
                                    CHECK (aid_box_status  IN ('Docked','Deployed')),
                cpr_kit_status      TEXT NOT NULL DEFAULT 'Docked'
                                    CHECK (cpr_kit_status  IN ('Docked','Deployed')),
                aid_box_usage       INTEGER NOT NULL DEFAULT 0 CHECK (aid_box_usage  >= 0),
                cpr_kit_usage       INTEGER NOT NULL DEFAULT 0 CHECK (cpr_kit_usage  >= 0),
                maintenance_revenue REAL    NOT NULL DEFAULT 0.0,
                card_sales_revenue  REAL    NOT NULL DEFAULT 0.0
            );

            CREATE TABLE IF NOT EXISTS active_hardware_codes (
                customer_id TEXT NOT NULL,
                code        TEXT NOT NULL,
                component   TEXT NOT NULL,
                start_time  TEXT NOT NULL,
                deposit     REAL NOT NULL DEFAULT 0.0,
                PRIMARY KEY (customer_id, component)
            );

            CREATE TABLE IF NOT EXISTS emergency_logs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp       TEXT    NOT NULL,
                customer_id     TEXT    NOT NULL,
                patient_tag     TEXT    DEFAULT '',
                action          TEXT    NOT NULL CHECK (action IN ('DEPLOY','RETURN')),
                component       TEXT    NOT NULL,
                deposit         REAL    NOT NULL DEFAULT 0.0,
                trans_id        TEXT    DEFAULT '',
                time_delta_mins REAL    NOT NULL DEFAULT 0.0,
                used_claim      TEXT    DEFAULT '',
                seal_status     TEXT    DEFAULT '',
                audit_required  INTEGER NOT NULL DEFAULT 0
                                CHECK (audit_required IN (0,1)),
                refund          REAL    NOT NULL DEFAULT 0.0,
                admin_noted     INTEGER NOT NULL DEFAULT 0
                                CHECK (admin_noted IN (0,1))
            );

            -- ── PDMS Compliance Audit Log  [A2] ──────────────────────────────
            -- Append-only. Rows are NEVER updated or deleted — full trail.
            -- actor_id : customer_id, 'SYSTEM', or 'ADMIN'
            -- action   : CREATE_ACCOUNT, LOGIN_OK, LOGIN_FAIL, PASSWORD_CHANGE,
            --            BALANCE_ADJ, STATUS_CHANGE, HW_DEPLOY, HW_RETURN,
            --            PURCHASE, RETURN_ITEM, DELETE_ACCOUNT, ADMIN_ACTION,
            --            LOCKOUT, UNLOCK, UPGRADE, CARD_PURCHASE
            CREATE TABLE IF NOT EXISTS pdms_audit_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT    NOT NULL,
                actor_id    TEXT    NOT NULL,
                action      TEXT    NOT NULL,
                table_name  TEXT,
                record_id   TEXT,
                detail      TEXT
            );

            -- ── Indexes ───────────────────────────────────────────────────────
            CREATE INDEX IF NOT EXISTS idx_transactions_customer
                ON transactions(customer_id, timestamp);
            CREATE INDEX IF NOT EXISTS idx_wallet_customer
                ON wallet_history(customer_id, timestamp);
            CREATE INDEX IF NOT EXISTS idx_notifications_customer
                ON notifications(customer_id);
            CREATE INDEX IF NOT EXISTS idx_cart_customer
                ON customer_cart(customer_id);
            CREATE INDEX IF NOT EXISTS idx_trends_customer
                ON customer_health_trends(customer_id, trend_type);
            CREATE INDEX IF NOT EXISTS idx_face_sig_customer
                ON customer_face_signatures(customer_id);
            CREATE INDEX IF NOT EXISTS idx_return_photos_drug
                ON return_photos(drug_id);
            CREATE INDEX IF NOT EXISTS idx_pdms_actor
                ON pdms_audit_log(actor_id, timestamp);
            CREATE INDEX IF NOT EXISTS idx_pdms_action
                ON pdms_audit_log(action, timestamp);

            -- ══════════════════════════════════════════════════════════════════
            -- BUILD 13 TABLES
            -- ══════════════════════════════════════════════════════════════════

            -- ── PRESCRIPTIONS [B13] ──────────────────────────────────────────
            CREATE TABLE IF NOT EXISTS prescriptions (
                prescription_id     TEXT PRIMARY KEY,
                drug_id             INTEGER,
                quantity_authorized INTEGER NOT NULL DEFAULT 1
                                    CHECK (quantity_authorized > 0),
                valid_until         TEXT,
                nhis_approved       INTEGER NOT NULL DEFAULT 0
                                    CHECK (nhis_approved IN (0,1)),
                customer_id         TEXT NOT NULL,
                drug_name           TEXT NOT NULL,
                dosage_instructions TEXT DEFAULT '',
                issued_by           TEXT DEFAULT 'SYSTEM',
                issued_at           TEXT NOT NULL,
                expires_at          TEXT NOT NULL,
                status              TEXT NOT NULL DEFAULT 'active'
                                    CHECK (status IN ('active','used','expired','cancelled')),
                consult_id          TEXT DEFAULT '',
                FOREIGN KEY (customer_id)
                    REFERENCES customers(customer_id) ON DELETE CASCADE
            );

            -- ── SUPPORT TICKETS [B13] ─────────────────────────────────────────
            CREATE TABLE IF NOT EXISTS support_tickets (
                ticket_id        TEXT PRIMARY KEY,
                customer_id      TEXT NOT NULL,
                category         TEXT NOT NULL
                                 CHECK (category IN ('billing','hardware','account','medical','general')),
                priority         TEXT NOT NULL DEFAULT 'normal'
                                 CHECK (priority IN ('low','normal','high','urgent')),
                status           TEXT NOT NULL DEFAULT 'open'
                                 CHECK (status IN ('open','in_progress','awaiting_customer',
                                                   'resolved','escalated')),
                subject          TEXT NOT NULL,
                description      TEXT NOT NULL,
                created_at       TEXT NOT NULL,
                updated_at       TEXT NOT NULL,
                resolved_at      TEXT,
                resolved_by      TEXT DEFAULT '',
                resolution_notes TEXT DEFAULT '',
                assigned_to      TEXT DEFAULT 'SYSTEM',
                escalated_at     TEXT,
                FOREIGN KEY (customer_id)
                    REFERENCES customers(customer_id) ON DELETE CASCADE
            );

            -- ── TELECONSULT [B13] ─────────────────────────────────────────────
            CREATE TABLE IF NOT EXISTS teleconsult_records (
                consult_id   TEXT PRIMARY KEY,
                customer_id  TEXT NOT NULL,
                drug_names   TEXT NOT NULL,
                status       TEXT NOT NULL DEFAULT 'queued'
                             CHECK (status IN ('queued','in_session','approved',
                                               'rejected','cancelled')),
                doctor_note  TEXT DEFAULT '',
                decision     TEXT DEFAULT '',
                requested_at TEXT NOT NULL,
                resolved_at  TEXT,
                FOREIGN KEY (customer_id)
                    REFERENCES customers(customer_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS teleconsult_queue (
                queue_id    INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id TEXT    NOT NULL,
                consult_id  TEXT    NOT NULL,
                priority    INTEGER NOT NULL DEFAULT 2 CHECK (priority IN (1,2)),
                joined_at   TEXT    NOT NULL,
                status      TEXT    NOT NULL DEFAULT 'waiting'
                            CHECK (status IN ('waiting','in_session','done'))
            );

            -- ══════════════════════════════════════════════════════════════════
            -- BUILD 14 TABLES
            -- ══════════════════════════════════════════════════════════════════

            -- ── CLTS SESSION LOG [B14] ────────────────────────────────────────
            CREATE TABLE IF NOT EXISTS clts_session_log (
                session_id      INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id     TEXT,
                session_at      TEXT    NOT NULL,
                detected_gender TEXT    DEFAULT 'Unknown',
                temperature     REAL,
                mood_estimate   TEXT    DEFAULT 'neutral'
                                CHECK (mood_estimate IN ('neutral','distressed','fatigued','anxious','alert')),
                face_matched    INTEGER NOT NULL DEFAULT 0 CHECK (face_matched IN (0,1)),
                led_to_purchase INTEGER NOT NULL DEFAULT 0 CHECK (led_to_purchase IN (0,1)),
                time_of_day     TEXT    NOT NULL
                                CHECK (time_of_day IN ('morning','afternoon','evening','night')),
                day_of_week     TEXT    NOT NULL
            );

            -- ── NYANSA INSIGHTS [B14] ──────────────────────────────────────────
            CREATE TABLE IF NOT EXISTS nyansa_insights (
                insight_id         INTEGER PRIMARY KEY AUTOINCREMENT,
                insight_type       TEXT NOT NULL
                                   CHECK (insight_type IN (
                                   'demand_surge','customer_lapse','cross_sell',
                                   'seasonal_trend','restock_urgent','promotion_opportunity',
                                   'price_anomaly','peak_hour',
                                   'PURCHASE','RETURN','HEALTH_INTAKE','TELECONSULT',
                                   'REGISTRATION','LOGIN','MANUAL_ANALYSIS','SIGNAL')),
                insight_data       TEXT,
                generated_at       TEXT NOT NULL DEFAULT (datetime('now')),
                drug_id            INTEGER,
                customer_segment   TEXT DEFAULT '',
                confidence_score   REAL NOT NULL DEFAULT 0.0
                                   CHECK (confidence_score BETWEEN 0 AND 1),
                title              TEXT NOT NULL DEFAULT '',
                description        TEXT NOT NULL DEFAULT '',
                recommended_action TEXT NOT NULL DEFAULT '',
                status             TEXT NOT NULL DEFAULT 'active'
                                   CHECK (status IN ('pending','actioned','dismissed','active')),
                created_at         TEXT NOT NULL DEFAULT (datetime('now')),
                actioned_at        TEXT,
                actioned_by        TEXT
            );

            -- ── PROMOTIONS [B14] ─────────────────────────────────────────────
            CREATE TABLE IF NOT EXISTS promotions (
                promo_id         TEXT PRIMARY KEY,
                name             TEXT NOT NULL,
                drug_id          INTEGER,
                discount_type    TEXT NOT NULL
                                 CHECK (discount_type IN ('percent','fixed','buy_x_get_y')),
                discount_value   REAL NOT NULL CHECK (discount_value >= 0),
                buy_qty          INTEGER NOT NULL DEFAULT 1,
                get_qty          INTEGER NOT NULL DEFAULT 0,
                start_date       TEXT NOT NULL,
                end_date         TEXT NOT NULL,
                target_segment   TEXT NOT NULL DEFAULT 'all'
                                 CHECK (target_segment IN ('all','nhis','g2_plus','loyalty_100plus')),
                min_purchase_qty INTEGER NOT NULL DEFAULT 1,
                status           TEXT NOT NULL DEFAULT 'draft'
                                 CHECK (status IN ('draft','active','expired','cancelled')),
                created_by       TEXT NOT NULL DEFAULT 'ADMIN',
                created_at       TEXT NOT NULL,
                times_applied    INTEGER NOT NULL DEFAULT 0
            );

            -- ── RESTOCK ORDERS [B14] ─────────────────────────────────────────
            CREATE TABLE IF NOT EXISTS restock_orders (
                order_id         TEXT PRIMARY KEY,
                drug_id          INTEGER NOT NULL,
                drug_name        TEXT    NOT NULL,
                quantity_ordered INTEGER NOT NULL CHECK (quantity_ordered > 0),
                current_stock    INTEGER NOT NULL,
                generated_by     TEXT    NOT NULL DEFAULT 'NYANSA_AUTO',
                generated_at     TEXT    NOT NULL,
                status           TEXT    NOT NULL DEFAULT 'draft'
                                 CHECK (status IN ('draft','sent','confirmed',
                                                   'shipped','received','cancelled')),
                supplier_ref     TEXT    DEFAULT '',
                expected_delivery TEXT,
                received_at      TEXT,
                received_qty     INTEGER DEFAULT 0,
                notes            TEXT    DEFAULT ''
            );

            -- ── PRICE HISTORY [B14] ──────────────────────────────────────────
            CREATE TABLE IF NOT EXISTS price_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                drug_id     INTEGER NOT NULL,
                table_name  TEXT    NOT NULL
                            CHECK (table_name IN ('inventory','mega_inventory')),
                price       REAL    NOT NULL,
                recorded_at TEXT    NOT NULL
            );

            -- ── NEW INDEXES ───────────────────────────────────────────────────
            CREATE INDEX IF NOT EXISTS idx_tickets_customer
                ON support_tickets(customer_id, status);
            CREATE INDEX IF NOT EXISTS idx_tickets_status
                ON support_tickets(status, created_at);
            CREATE INDEX IF NOT EXISTS idx_consult_customer
                ON teleconsult_records(customer_id, status);
            CREATE INDEX IF NOT EXISTS idx_clts_session
                ON clts_session_log(session_at, time_of_day);
            CREATE INDEX IF NOT EXISTS idx_insights_type
                ON nyansa_insights(insight_type, status);
            CREATE INDEX IF NOT EXISTS idx_promos_status
                ON promotions(status, start_date, end_date);
            CREATE INDEX IF NOT EXISTS idx_restock_status
                ON restock_orders(status, generated_at);
            CREATE INDEX IF NOT EXISTS idx_price_history
                ON price_history(drug_id, recorded_at);
            CREATE INDEX IF NOT EXISTS idx_prescriptions_customer
                ON prescriptions(customer_id, status);

            -- ══════════════════════════════════════════════════════════════════
            -- BUILD 15 TABLES
            -- ══════════════════════════════════════════════════════════════════

            -- ── OTA SYSTEM UPDATES [B15] ──────────────────────────────────────
            CREATE TABLE IF NOT EXISTS system_updates (
                update_id       TEXT PRIMARY KEY,
                unit_id         TEXT    NOT NULL DEFAULT 'AID-UNIT-001',
                from_version    INTEGER NOT NULL,
                to_version      INTEGER NOT NULL,
                initiated_at    TEXT    NOT NULL,
                completed_at    TEXT,
                status          TEXT    NOT NULL DEFAULT 'pending'
                                CHECK (status IN ('pending','staged','applied',
                                                  'failed','rolled_back')),
                checksum        TEXT    DEFAULT '',
                file_size_bytes INTEGER DEFAULT 0,
                version_from    INTEGER NOT NULL DEFAULT 0,
                version_to      INTEGER NOT NULL DEFAULT 0,
                initiated_by    TEXT    NOT NULL DEFAULT 'SYSTEM',
                notes           TEXT    DEFAULT ''
            );

            -- ── NOTIFICATION DISPATCH LOG [B15] ──────────────────────────────
            CREATE TABLE IF NOT EXISTS notification_dispatch (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                notification_id INTEGER,
                customer_id     TEXT    NOT NULL,
                channel         TEXT    NOT NULL CHECK (channel IN ('email','sms','in_app')),
                subject         TEXT    NOT NULL,
                message         TEXT    NOT NULL,
                status          TEXT    NOT NULL DEFAULT 'pending'
                                CHECK (status IN ('pending','sent','failed','simulated')),
                attempted_at    TEXT    NOT NULL,
                error_detail    TEXT    DEFAULT ''
            );

            -- ── SCHEDULER LOG [B15] ───────────────────────────────────────────
            CREATE TABLE IF NOT EXISTS scheduler_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                task_name       TEXT    NOT NULL,
                started_at      TEXT    NOT NULL,
                completed_at    TEXT,
                duration_ms     INTEGER DEFAULT 0,
                status          TEXT    NOT NULL DEFAULT 'running'
                                CHECK (status IN ('running','completed','failed')),
                result_summary  TEXT    DEFAULT '',
                error_detail    TEXT    DEFAULT ''
            );

            -- ══════════════════════════════════════════════════════════════════
            -- BUILD 16 TABLES
            -- ══════════════════════════════════════════════════════════════════

            -- ── UNIT REGISTRY [B16] ───────────────────────────────────────────
            CREATE TABLE IF NOT EXISTS unit_registry (
                unit_id         TEXT PRIMARY KEY,
                unit_name       TEXT    NOT NULL,
                location        TEXT    NOT NULL,
                region          TEXT    DEFAULT '',
                installed_at    TEXT    NOT NULL,
                last_seen       TEXT,
                current_version INTEGER NOT NULL DEFAULT 16,
                status          TEXT    NOT NULL DEFAULT 'active'
                                CHECK (status IN ('active','offline','maintenance')),
                notes           TEXT    DEFAULT ''
            );

            -- ── API TOKENS [B16] ──────────────────────────────────────────────
            CREATE TABLE IF NOT EXISTS api_tokens (
                token_id        TEXT PRIMARY KEY,
                actor_id        TEXT    NOT NULL,
                role            TEXT    NOT NULL
                                CHECK (role IN ('admin','doctor','distrib_centre','readonly')),
                issued_at       TEXT    NOT NULL,
                expires_at      TEXT    NOT NULL,
                last_used       TEXT,
                revoked         INTEGER NOT NULL DEFAULT 0 CHECK (revoked IN (0,1)),
                description     TEXT    DEFAULT ''
            );

            -- ── NEW INDEXES ───────────────────────────────────────────────────
            CREATE INDEX IF NOT EXISTS idx_system_updates_unit
                ON system_updates(unit_id, initiated_at);
            CREATE INDEX IF NOT EXISTS idx_notif_dispatch_customer
                ON notification_dispatch(customer_id, status);
            CREATE INDEX IF NOT EXISTS idx_scheduler_log_task
                ON scheduler_log(task_name, started_at);
            CREATE INDEX IF NOT EXISTS idx_api_tokens_actor
                ON api_tokens(actor_id, revoked);

            -- ══════════════════════════════════════════════════════════════════
            -- BUILD 17 TABLES
            -- ══════════════════════════════════════════════════════════════════

            -- ── DEVICE TOKENS for Push Notifications [B17] ───────────────────
            CREATE TABLE IF NOT EXISTS device_tokens (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id     TEXT    NOT NULL,
                token           TEXT    NOT NULL UNIQUE,
                platform        TEXT    NOT NULL CHECK (platform IN ('android','ios','web')),
                registered_at   TEXT    NOT NULL,
                last_seen       TEXT,
                active          INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1))
            );

            -- ── MOMO PAYMENT WEBHOOKS [B17] ───────────────────────────────────
            CREATE TABLE IF NOT EXISTS momo_webhooks (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                reference       TEXT    NOT NULL UNIQUE,
                customer_id     TEXT    NOT NULL,
                amount          REAL    NOT NULL,
                status          TEXT    NOT NULL CHECK (status IN ('pending','success','failed','duplicate')),
                payload         TEXT    NOT NULL DEFAULT '',
                received_at     TEXT    NOT NULL,
                processed_at    TEXT
            );

            -- ══════════════════════════════════════════════════════════════════
            -- BUILD 18 TABLES
            -- ══════════════════════════════════════════════════════════════════

            -- ── THERMAL READINGS LOG [B18] ────────────────────────────────────
            CREATE TABLE IF NOT EXISTS thermal_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id     TEXT,
                ambient_temp    REAL    NOT NULL,
                object_temp     REAL    NOT NULL,
                sensor_mode     TEXT    NOT NULL DEFAULT 'real'
                                CHECK (sensor_mode IN ('real','simulated')),
                recorded_at     TEXT    NOT NULL,
                flagged         INTEGER NOT NULL DEFAULT 0 CHECK (flagged IN (0,1)),
                flag_reason     TEXT    DEFAULT ''
            );

            -- ── DISPENSE HARDWARE LOG [B18] ───────────────────────────────────
            CREATE TABLE IF NOT EXISTS dispense_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                shelf_num       INTEGER NOT NULL,
                drug_name       TEXT    NOT NULL,
                gpio_pin        INTEGER NOT NULL DEFAULT 0,
                triggered_at    TEXT    NOT NULL,
                confirmed_at    TEXT,
                mode            TEXT    NOT NULL DEFAULT 'real'
                                CHECK (mode IN ('real','simulated')),
                status          TEXT    NOT NULL DEFAULT 'pending'
                                CHECK (status IN ('pending','confirmed','jammed','failed')),
                customer_id     TEXT,
                transaction_id  TEXT
            );

            -- ── BUILD 19 TABLE ─────────────────────────────────────────────────
            -- ── GENERATED REPORTS [B19] ───────────────────────────────────────
            CREATE TABLE IF NOT EXISTS generated_reports (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                report_type     TEXT    NOT NULL,
                title           TEXT    NOT NULL,
                file_path       TEXT    NOT NULL,
                unit_id         TEXT    NOT NULL DEFAULT 'ALL',
                period_start    TEXT,
                period_end      TEXT,
                generated_by    TEXT    NOT NULL DEFAULT 'SYSTEM',
                generated_at    TEXT    NOT NULL,
                file_size_bytes INTEGER DEFAULT 0
            );

            -- ── Build 20: Cupsule tracking ────────────────────────────────────
            CREATE TABLE IF NOT EXISTS cupsule_issued (
                cupsule_id      TEXT    PRIMARY KEY,
                customer_id     TEXT    NOT NULL,
                transaction_id  TEXT    NOT NULL,
                shelf_num       INTEGER NOT NULL,
                drug_name       TEXT    NOT NULL,
                drug_class      TEXT    NOT NULL DEFAULT 'OTC',
                issued_at       TEXT    NOT NULL,
                unit_id         TEXT    NOT NULL,
                returned        INTEGER NOT NULL DEFAULT 0
                                CHECK (returned IN (0,1)),
                returned_at     TEXT,
                points_awarded  INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (customer_id) REFERENCES customers(customer_id)
            );

            -- ── Build 20: AID PLUS+ Service Bus registry ──────────────────────
            CREATE TABLE IF NOT EXISTS service_bus_registry (
                service_name    TEXT    PRIMARY KEY,
                contract_id     TEXT    NOT NULL,
                version         TEXT    NOT NULL,
                registered_at   TEXT    NOT NULL,
                last_heartbeat  TEXT,
                status          TEXT    NOT NULL DEFAULT 'active'
                                CHECK (status IN ('active','inactive','error')),
                capabilities    TEXT    NOT NULL DEFAULT '[]',
                host_unit_id    TEXT
            );

            -- ── Build 20: Password recovery log ──────────────────────────────
            CREATE TABLE IF NOT EXISTS password_reset_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id     TEXT    NOT NULL,
                reset_token     TEXT    NOT NULL UNIQUE,
                method          TEXT    NOT NULL
                                CHECK (method IN ('qr_app','card_tap','ghana_card')),
                issued_at       TEXT    NOT NULL,
                expires_at      TEXT    NOT NULL,
                used            INTEGER NOT NULL DEFAULT 0,
                used_at         TEXT,
                ip_address      TEXT,
                FOREIGN KEY (customer_id) REFERENCES customers(customer_id)
            );

            -- ── NEW INDEXES ───────────────────────────────────────────────────
            CREATE INDEX IF NOT EXISTS idx_thermal_log_customer
                ON thermal_log(customer_id, recorded_at);
            CREATE INDEX IF NOT EXISTS idx_dispense_log_shelf
                ON dispense_log(shelf_num, triggered_at);
            CREATE INDEX IF NOT EXISTS idx_device_tokens_customer
                ON device_tokens(customer_id, active);
            CREATE INDEX IF NOT EXISTS idx_momo_webhooks_ref
                ON momo_webhooks(reference, status);
            CREATE INDEX IF NOT EXISTS idx_cupsule_customer
                ON cupsule_issued(customer_id, issued_at);
            CREATE INDEX IF NOT EXISTS idx_cupsule_returned
                ON cupsule_issued(returned, returned_at);
            CREATE INDEX IF NOT EXISTS idx_reset_token
                ON password_reset_log(reset_token, used, expires_at);

            -- ── Build 23: CUPSCAN tables ─────────────────────────────────────
            CREATE TABLE IF NOT EXISTS cupscan_kiosks (
                kiosk_id        TEXT PRIMARY KEY,
                site_id         TEXT,
                site_name       TEXT,
                location        TEXT NOT NULL DEFAULT '',
                status          TEXT NOT NULL DEFAULT 'active',
                last_ping       TEXT,
                last_heartbeat  TEXT,
                firmware        TEXT,
                firmware_ver    TEXT,
                registered_at   TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS cupscan_returns (
                return_id       INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id     TEXT    NOT NULL,
                kiosk_id        TEXT,
                card_uid        TEXT,
                drug_id         INTEGER,
                quantity        INTEGER NOT NULL DEFAULT 1,
                condition       TEXT,
                compartment     INTEGER DEFAULT 1,
                base_pts        INTEGER NOT NULL DEFAULT 0,
                bonus_pts       INTEGER NOT NULL DEFAULT 0,
                multiplier      REAL    NOT NULL DEFAULT 1.0,
                total_pts       INTEGER NOT NULL DEFAULT 0,
                points_awarded  INTEGER NOT NULL DEFAULT 0,
                streak_days     INTEGER NOT NULL DEFAULT 0,
                is_bonus_window INTEGER NOT NULL DEFAULT 0,
                co2_saved_g     REAL    NOT NULL DEFAULT 0.0,
                water_saved_l   REAL    NOT NULL DEFAULT 0.0,
                returned_at     TEXT    NOT NULL DEFAULT (datetime('now')),
                synced_at       TEXT,
                batch_id        TEXT,
                fraud_flag      INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (customer_id) REFERENCES customers(customer_id)
            );
            CREATE TABLE IF NOT EXISTS cupscan_daily_counts (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id     TEXT NOT NULL,
                count_date      TEXT NOT NULL,
                date_str        TEXT NOT NULL DEFAULT '',
                return_count    INTEGER NOT NULL DEFAULT 0,
                points_total    INTEGER NOT NULL DEFAULT 0,
                bonus_pts_given INTEGER NOT NULL DEFAULT 0,
                UNIQUE(customer_id, count_date)
            );

            -- ── Build 25: Power telemetry and offline queue ───────────────────
            CREATE TABLE IF NOT EXISTS power_telemetry (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                unit_id         TEXT    NOT NULL DEFAULT 'ADW-UNSET',
                logged_at       TEXT    NOT NULL DEFAULT (datetime('now')),
                source          TEXT    NOT NULL DEFAULT 'SOLAR+BATTERY',
                state           TEXT    NOT NULL DEFAULT 'OK',
                battery_pct     REAL    NOT NULL DEFAULT 100.0,
                solar_v         REAL    NOT NULL DEFAULT 0.0,
                solar_w         REAL    NOT NULL DEFAULT 0.0,
                voltage         REAL,
                current_ma      REAL,
                wh_consumed     REAL    NOT NULL DEFAULT 0.0,
                uptime_secs     INTEGER NOT NULL DEFAULT 0,
                charging        INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS offline_queue (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                queued_at       TEXT NOT NULL DEFAULT (datetime('now')),
                endpoint        TEXT NOT NULL,
                method          TEXT NOT NULL DEFAULT 'POST',
                payload         TEXT NOT NULL,
                retry_count     INTEGER NOT NULL DEFAULT 0,
                next_retry_at   TEXT,
                status          TEXT NOT NULL DEFAULT 'pending',
                last_error      TEXT
            );

            -- ── Build 26: OTA log ─────────────────────────────────────────────
            CREATE TABLE IF NOT EXISTS ota_log (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                checked_at        TEXT    NOT NULL DEFAULT (datetime('now')),
                current_version   INTEGER NOT NULL,
                available_version INTEGER,
                action            TEXT    NOT NULL,
                detail            TEXT,
                notes             TEXT
            );

            -- ── Build 27: OS infrastructure tables ───────────────────────────
            CREATE TABLE IF NOT EXISTS notification_templates (
                template_id     TEXT PRIMARY KEY,
                template_key    TEXT NOT NULL DEFAULT '',
                channel         TEXT NOT NULL,
                lang            TEXT NOT NULL DEFAULT 'en',
                title           TEXT NOT NULL DEFAULT '',
                body            TEXT NOT NULL DEFAULT '',
                subject_tpl     TEXT DEFAULT '',
                body_tpl        TEXT NOT NULL DEFAULT '',
                active          INTEGER NOT NULL DEFAULT 1,
                created_at      TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS aidplus_os_boot_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                booted_at       TEXT NOT NULL DEFAULT (datetime('now')),
                variant         TEXT NOT NULL,
                adw_version     TEXT NOT NULL DEFAULT '',
                adw_serial      TEXT NOT NULL DEFAULT 'ADW-UNSET',
                build           INTEGER NOT NULL DEFAULT 0,
                build_version   INTEGER NOT NULL DEFAULT 0,
                conn_state      TEXT NOT NULL DEFAULT 'Offline',
                power_source    TEXT NOT NULL DEFAULT 'SOLAR+BATTERY',
                boot_result     TEXT NOT NULL DEFAULT 'OK',
                self_test_pass  INTEGER NOT NULL DEFAULT 0,
                notes           TEXT DEFAULT '',
                detail          TEXT
            );
            CREATE TABLE IF NOT EXISTS nyansa_engine_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                logged_at       TEXT NOT NULL DEFAULT (datetime('now')),
                event_type      TEXT NOT NULL,
                customer_id     TEXT,
                detail          TEXT
            );
            CREATE TABLE IF NOT EXISTS self_test_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                tested_at       TEXT NOT NULL DEFAULT (datetime('now')),
                component       TEXT NOT NULL,
                result          TEXT NOT NULL,
                detail          TEXT
            );

            -- ── Indexes for new tables ────────────────────────────────────────
            CREATE INDEX IF NOT EXISTS idx_cupscan_returns_customer
                ON cupscan_returns(customer_id, returned_at);
            CREATE INDEX IF NOT EXISTS idx_cupscan_daily_lookup
                ON cupscan_daily_counts(customer_id, count_date);
            CREATE INDEX IF NOT EXISTS idx_power_tel_time
                ON power_telemetry(logged_at);
            CREATE INDEX IF NOT EXISTS idx_offline_q_status
                ON offline_queue(status, next_retry_at);
            """)

    # ══════════════════════════════════════════════════════════════════════════
    # MIGRATIONS  [A1]
    # Each migration block is guarded by the current version number.
    # New builds add a new block — existing blocks are never modified.
    # ══════════════════════════════════════════════════════════════════════════
    def _get_schema_version(self) -> int:
        with self._conn() as con:
            row = con.execute("SELECT version FROM schema_version").fetchone()
            return row["version"] if row else 0

    def _set_schema_version(self, version: int):
        with self._conn() as con:
            if con.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 0:
                con.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
            else:
                con.execute("UPDATE schema_version SET version=?", (version,))

    def _migrate(self):
        current = self._get_schema_version()

        # Fresh install: base schema already created all tables correctly.
        # Skip historical migrations entirely.
        if current == 0:
            self._set_schema_version(28)
            print("DB: Fresh install → Build 28 ready.")
            return

        if current < 12:
            with self._conn() as con:
                cols = [r[1] for r in con.execute("PRAGMA table_info(customers)").fetchall()]
                if "password_salt" not in cols:
                    con.execute("ALTER TABLE customers ADD COLUMN password_salt TEXT")
            self._set_schema_version(12)
            print("DB Migration: → Build 12 applied.")

        if current < 14:
            self._set_schema_version(14)
            print("DB Migration: → Build 14 applied.")

        if current < 19:
            with self._conn() as con:
                # Add unit_id to restock_orders if not present
                cols_r = [r[1] for r in con.execute(
                    "PRAGMA table_info(restock_orders)").fetchall()]
                if "unit_id" not in cols_r:
                    con.execute(
                        "ALTER TABLE restock_orders ADD COLUMN unit_id TEXT DEFAULT 'AID-UNIT-001'")
                cols_t = [r[1] for r in con.execute(
                    "PRAGMA table_info(transactions)").fetchall()]
                if "unit_id" not in cols_t:
                    con.execute(
                        "ALTER TABLE transactions ADD COLUMN unit_id TEXT DEFAULT 'AID-UNIT-001'")
            self._set_schema_version(19)
            print("DB Migration: → Build 19 applied.")

        if current < 20:
            # Build 20: extended health intake columns on customers
            with self._conn() as con:
                existing = [r[1] for r in con.execute(
                    "PRAGMA table_info(customers)").fetchall()]
                new_cols = [
                    ("blood_group",           "TEXT DEFAULT ''"),
                    ("allergies",             "TEXT DEFAULT '[]'"),
                    ("chronic_conditions",    "TEXT DEFAULT '[]'"),
                    ("current_medications",   "TEXT DEFAULT '[]'"),
                    ("ghana_card_number",     "TEXT DEFAULT ''"),
                    ("ghana_card_verified",   "INTEGER DEFAULT 0"),
                    ("identity_verified",     "INTEGER DEFAULT 0"),
                    ("health_consent",        "INTEGER DEFAULT 0"),
                    ("security_question",     "TEXT DEFAULT ''"),
                    ("security_answer",       "TEXT DEFAULT ''"),
                    ("sync_token",            "TEXT"),
                    ("sync_token_expires",    "TEXT"),
                ]
                for col, typedef in new_cols:
                    if col not in existing:
                        con.execute(
                            f"ALTER TABLE customers ADD COLUMN {col} {typedef}")
            self._set_schema_version(20)
            print("DB Migration: → Build 20 applied.")

        if current < 21:
            with self._conn() as con:
                existing = [r[1] for r in con.execute(
                    "PRAGMA table_info(customers)").fetchall()]
                b21_cols = [
                    ("card_uid",             "TEXT DEFAULT NULL"),
                    ("security_answer_salt", "TEXT DEFAULT ''"),
                ]
                for col, typedef in b21_cols:
                    if col not in existing:
                        con.execute(
                            f"ALTER TABLE customers ADD COLUMN {col} {typedef}")
                # Unique index on card_uid (CREATE INDEX IF NOT EXISTS is safe)
                con.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_customers_card_uid "
                    "ON customers(card_uid) WHERE card_uid IS NOT NULL")
            self._set_schema_version(21)
            print("DB Migration: → Build 21 applied.")

        if current < 23:
            # ── B23: CUPSCAN integration tables ───────────────────────────────
            with self._conn() as con:
                con.executescript("""
                    CREATE TABLE IF NOT EXISTS cupscan_kiosks (
                        kiosk_id        TEXT    PRIMARY KEY,
                        site_id         TEXT    NOT NULL DEFAULT '',
                        site_name       TEXT    NOT NULL DEFAULT '',
                        registered_at   TEXT    NOT NULL,
                        last_heartbeat  TEXT,
                        firmware        TEXT    DEFAULT '',
                        status          TEXT    NOT NULL DEFAULT 'ACTIVE'
                                        CHECK (status IN ('ACTIVE','OFFLINE','MAINTENANCE')),
                        bin_intact_pct  REAL    NOT NULL DEFAULT 0.0,
                        bin_partial_pct REAL    NOT NULL DEFAULT 0.0,
                        bin_anomaly_pct REAL    NOT NULL DEFAULT 0.0,
                        bin_contam_pct  REAL    NOT NULL DEFAULT 0.0,
                        queue_depth     INTEGER NOT NULL DEFAULT 0,
                        returns_today   INTEGER NOT NULL DEFAULT 0
                    );
                    CREATE TABLE IF NOT EXISTS cupscan_returns (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        kiosk_id        TEXT    NOT NULL DEFAULT 'UNKNOWN',
                        customer_id     TEXT,
                        card_uid        TEXT,
                        compartment     TEXT    NOT NULL DEFAULT 'UNKNOWN',
                        base_pts        INTEGER NOT NULL DEFAULT 0,
                        bonus_pts       INTEGER NOT NULL DEFAULT 0,
                        total_pts       INTEGER NOT NULL DEFAULT 0,
                        multiplier      REAL    NOT NULL DEFAULT 1.0,
                        is_bonus_window INTEGER NOT NULL DEFAULT 0,
                        streak_days     INTEGER NOT NULL DEFAULT 0,
                        co2_saved_g     REAL    NOT NULL DEFAULT 0.0,
                        water_saved_l   REAL    NOT NULL DEFAULT 0.0,
                        returned_at     TEXT    NOT NULL,
                        synced_at       TEXT    NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS cupscan_daily_counts (
                        customer_id     TEXT    NOT NULL,
                        date_str        TEXT    NOT NULL,
                        return_count    INTEGER NOT NULL DEFAULT 0,
                        bonus_pts_given INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY (customer_id, date_str)
                    );
                    CREATE INDEX IF NOT EXISTS idx_cupscan_ret_customer
                        ON cupscan_returns(customer_id, returned_at);
                    CREATE INDEX IF NOT EXISTS idx_cupscan_ret_kiosk
                        ON cupscan_returns(kiosk_id, returned_at);
                """)
            self._set_schema_version(23)
            print("DB Migration: → Build 23 applied (CUPSCAN tables).")

        if current < 24:
            # nyansa_insights already created correctly in base schema.
            # Nothing to rename — this migration is a safe no-op.
            with self._conn() as con:
                con.execute("""
                    CREATE TABLE IF NOT EXISTS nyansa_insights (
                        insight_id       INTEGER PRIMARY KEY AUTOINCREMENT,
                        insight_type     TEXT NOT NULL,
                        generated_at     TEXT NOT NULL,
                        drug_id          INTEGER,
                        customer_segment TEXT DEFAULT '',
                        confidence       REAL DEFAULT 0.8,
                        action_taken     INTEGER DEFAULT 0,
                        expires_at       TEXT,
                        customer_id      TEXT,
                        insight_data     TEXT,
                        status           TEXT NOT NULL DEFAULT 'active',
                        created_at       TEXT NOT NULL DEFAULT (datetime('now'))
                    )
                """)
                try:
                    con.execute(
                        "CREATE INDEX IF NOT EXISTS idx_nyansa_insights_type "
                        "ON nyansa_insights(insight_type, status)"
                    )
                except Exception:
                    pass
            self._set_schema_version(24)
            print("DB Migration: → Build 24 applied.")
            print("DB Migration: → Build 24 applied (Nyansa rename, ADW-1 platform).")

        if current < 26:
            # ── B25+B26: Power telemetry, offline queue, OTA log,
            #             notification templates, Aid Plus OS boot log ──────────
            with self._conn() as con:
                con.executescript("""
                    -- B25: Power telemetry — records every 5 minutes
                    CREATE TABLE IF NOT EXISTS power_telemetry (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        unit_id         TEXT    NOT NULL DEFAULT 'LOCAL',
                        logged_at       TEXT    NOT NULL,
                        source          TEXT    NOT NULL,  -- MAINS/SOLAR/BATTERY
                        state           TEXT    NOT NULL,  -- OK/LOW/CRITICAL
                        battery_pct     REAL,
                        solar_v         REAL,
                        wh_consumed     REAL,
                        uptime_secs     INTEGER
                    );
                    CREATE INDEX IF NOT EXISTS idx_power_tel_time
                        ON power_telemetry(logged_at);

                    -- B25: Offline queue — persists API calls while offline
                    CREATE TABLE IF NOT EXISTS offline_queue (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        queued_at       TEXT    NOT NULL,
                        endpoint        TEXT    NOT NULL,
                        method          TEXT    NOT NULL DEFAULT 'POST',
                        payload         TEXT    NOT NULL,  -- JSON string
                        retry_count     INTEGER NOT NULL DEFAULT 0,
                        next_retry_at   TEXT,
                        status          TEXT    NOT NULL DEFAULT 'pending',
                        last_error      TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_offline_q_status
                        ON offline_queue(status, next_retry_at);

                    -- B26: OTA update log
                    CREATE TABLE IF NOT EXISTS ota_log (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        checked_at      TEXT    NOT NULL,
                        current_version INTEGER NOT NULL,
                        available_version INTEGER,
                        action          TEXT    NOT NULL,  -- checked/downloaded/applied/failed
                        notes           TEXT
                    );

                    -- B26: Notification templates (English + Twi)
                    CREATE TABLE IF NOT EXISTS notification_templates (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        template_key    TEXT    NOT NULL,
                        lang            TEXT    NOT NULL DEFAULT 'en',
                        title           TEXT    NOT NULL,
                        body            TEXT    NOT NULL,
                        UNIQUE (template_key, lang)
                    );

                    -- B26: Aid Plus OS boot log — tracks all ADW variant startups
                    CREATE TABLE IF NOT EXISTS aidplus_os_boot_log (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        booted_at       TEXT    NOT NULL,
                        variant         TEXT    NOT NULL,  -- ADW-AS/ADW-BT/ADW-AA
                        adw_version     TEXT    NOT NULL,
                        adw_serial      TEXT    NOT NULL,
                        build_version   INTEGER NOT NULL,
                        conn_state      TEXT,
                        power_source    TEXT,
                        boot_result     TEXT    NOT NULL DEFAULT 'ok',
                        notes           TEXT
                    );
                """)
            self._set_schema_version(26)
            # Seed notification templates
            self._seed_notification_templates()
            print("DB Migration: → Build 26 applied (Power, OfflineQueue, OTA, "
                  "NotifTemplates, AidPlusOS boot log).")

        if current < 28:
            # ── B28: Nyansa engine log, self-test log, dormant bus registry ──
            with self._conn() as con:
                con.executescript("""
                    -- B28: Nyansa engine signal log (no PII)
                    CREATE TABLE IF NOT EXISTS nyansa_engine_log (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        logged_at       TEXT    NOT NULL,
                        signal_type     TEXT    NOT NULL,
                        signal_value    TEXT,
                        age_bracket     TEXT    DEFAULT 'U',
                        gender_cat      TEXT    DEFAULT 'U',
                        district        TEXT    DEFAULT 'unknown',
                        build_version   INTEGER NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_nyansa_eng_type
                        ON nyansa_engine_log(signal_type, logged_at);

                    -- B28: Self-test log — one row per boot
                    CREATE TABLE IF NOT EXISTS self_test_log (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        tested_at       TEXT    NOT NULL,
                        db_result       TEXT,
                        gpio_result     TEXT,
                        conn_result     TEXT,
                        power_result    TEXT,
                        nyansa_result   TEXT,
                        bus_result      TEXT,
                        overall         TEXT    NOT NULL DEFAULT 'PASS',
                        notes           TEXT
                    );

                    -- B28: Dormant service bus registry
                    -- Stores expected products so dashboard can show
                    -- LIVE vs DORMANT vs NOT_YET_DEPLOYED status
                    CREATE TABLE IF NOT EXISTS service_bus_registry (
                        service_name    TEXT    PRIMARY KEY,
                        variant         TEXT    NOT NULL,
                        status          TEXT    NOT NULL DEFAULT 'dormant',
                        registered_at   TEXT,
                        last_heartbeat  TEXT,
                        notes           TEXT
                    );
                """)
                # Pre-register all known products in the bus registry
                bus_products = [
                    ("CAPSCAN",  ADW_VARIANT_AS, "live",
                     "Co-located CUPSCANModule on ADW-AS"),
                    ("BTM",      ADW_VARIANT_BT, "dormant",
                     "Blood Testing Machine — not yet deployed"),
                    ("AID_AIR",  ADW_VARIANT_AA, "dormant",
                     "Aid Air drone system — not yet deployed"),
                ]
                con.executemany(
                    "INSERT OR IGNORE INTO service_bus_registry "
                    "(service_name, variant, status, registered_at, notes) "
                    "VALUES (?,?,?,?,?)",
                    [(n, v, s, datetime.now().isoformat(), note)
                     for n, v, s, note in bus_products])
            self._set_schema_version(28)
            print("DB Migration: → Build 28 applied (NyansaEngine log, "
                  "SelfTest log, ServiceBus registry, dormant sockets).")

        # Seed the local unit into unit_registry if not present
        with self._conn() as con:
            if con.execute("SELECT COUNT(*) FROM unit_registry WHERE unit_id=?",
                           (UNIT_ID,)).fetchone()[0] == 0:
                con.execute(
                    "INSERT INTO unit_registry (unit_id,unit_name,location,installed_at,"
                    "current_version,status) VALUES (?,?,?,?,?,?)",
                    (UNIT_ID, f"AID System {UNIT_ID}", "Primary Location",
                     datetime.now().isoformat(), SCHEMA_VERSION, "active"))


    # ── Seed helpers ───────────────────────────────────────────────────────────
    def _seed_notification_templates(self):
        """B26: Seed English and Twi notification templates."""
        templates = [
            # ── Account ───────────────────────────────────────────────────
            ("WELCOME",          LANG_EN,
             "Welcome to AID PLUS+",
             "Your account is ready. You can now purchase medications and earn "
             "points by returning Cupsules. Stay healthy!"),
            ("WELCOME",          LANG_TW,
             "Akwaaba wɔ AID PLUS+",
             "Wo account ato hɔ. Wobɛtumi de sika tɔ nnurow na wonya points "
             "wɔ Cupsule a wogya no mu. Wo ho ntena yie!"),
            ("ACCOUNT_LOCKED",   LANG_EN,
             "Account Temporarily Locked",
             "Your account has been locked after multiple failed login attempts. "
             "Please contact support or visit the kiosk to unlock."),
            ("ACCOUNT_LOCKED",   LANG_TW,
             "Account no Akyere Boo",
             "Wode password pa no mmienu so enti account no akyere boo. "
             "Mepa wo kyɛw, kɔ kiosk no so na wɔde wo ma."),

            # ── Purchase ──────────────────────────────────────────────────
            ("PURCHASE_RECEIPT", LANG_EN,
             "Purchase Receipt",
             "You purchased {item_name} x{qty} for GHS {amount}. "
             "Wallet balance: GHS {balance}. Cupsule serial: {cupsule_id}."),
            ("PURCHASE_RECEIPT", LANG_TW,
             "Tɔbea Krataa",
             "Wotɔɔ {item_name} x{qty} GHS {amount} ho. "
             "Wallet mu sika: GHS {balance}. Cupsule no nkyekyem: {cupsule_id}."),
            ("LOW_BALANCE",      LANG_EN,
             "Low Wallet Balance",
             "Your wallet balance is GHS {balance}. Top up at any AID System "
             "kiosk or via the AID PLUS+ app."),
            ("LOW_BALANCE",      LANG_TW,
             "Wallet Sika Sua",
             "Wo wallet mu sika yɛ GHS {balance}. Fa sika kɔ AID System "
             "kiosk biara anaa AID PLUS+ app no so."),

            # ── CUPSCAN ───────────────────────────────────────────────────
            ("CUPSCAN_RECEIPT",  LANG_EN,
             "Cupsule Return Receipt",
             "Cupsule accepted — {condition}. Weight: {weight_g}g. "
             "+{pts} points{bonus_note}. New balance: {balance} pts. "
             "CO₂ saved: {co2}g | Water saved: {water}L. Thank you!"),
            ("CUPSCAN_RECEIPT",  LANG_TW,
             "Cupsule No Gya Ho Krataa",
             "Wogye Cupsule no — {condition}. Duru: {weight_g}g. "
             "+{pts} points{bonus_note}. Points foforo: {balance}. "
             "CO₂ faree: {co2}g | Nsuo faree: {water}L. Meda wo ase!"),
            ("CUPSCAN_REJECTED", LANG_EN,
             "Cupsule Not Accepted",
             "Your Cupsule could not be accepted: {reason}. "
             "Please ensure the cup is a genuine AID PLUS+ Cupsule."),
            ("CUPSCAN_REJECTED", LANG_TW,
             "Wangye Cupsule No",
             "Wangye wo Cupsule no: {reason}. "
             "Hu sɛ Cupsule no yɛ ohu AID PLUS+ Cupsule."),
            ("CUPSCAN_BATCH_DONE", LANG_EN,
             "Cupsule Batch Return Complete",
             "{accepted} of {total} cups accepted — +{pts_total} points earned. "
             "{rejected} returned to you. Balance: {balance} pts."),
            ("CUPSCAN_BATCH_DONE", LANG_TW,
             "Cupsule Nhyiamu No Wie",
             "{accepted} wɔ {total} cups mu agye — +{pts_total} points nya. "
             "{rejected} agye ama wo. Points: {balance}."),

            # ── Power ─────────────────────────────────────────────────────
            ("POWER_LOW",        LANG_EN,
             "Power Warning — Battery Low",
             "The AID System kiosk at your location is running on low battery "
             "({battery_pct}%). Service may be limited. Solar charging in progress."),
            ("POWER_LOW",        LANG_TW,
             "Nnuro Kɔ Hɔ — Batiri Sua",
             "AID System kiosk a ɛwɔ wo hɔ no batiri sua ({battery_pct}%). "
             "Sɛ ɔsiesie bɛn a, wɔbɛsiesie. Solar power nso reyɛ adwuma."),
            ("POWER_CRITICAL",   LANG_EN,
             "Kiosk Shutting Down — Power Critical",
             "The kiosk battery is critically low ({battery_pct}%). "
             "The system will shut down shortly to protect your data. "
             "All pending transactions have been saved."),
            ("POWER_CRITICAL",   LANG_TW,
             "Kiosk Reto Fi — Nnuro Mu Wɔ Ahosɛpɛw",
             "Kiosk batiri sua koraa ({battery_pct}%). System no bɛto fi ntɛm "
             "sɛ ɛhwɛ wo data no. Transactions nyinaa ato hɔ."),

            # ── Health ────────────────────────────────────────────────────
            ("MEDICATION_REMINDER", LANG_EN,
             "Medication Reminder",
             "Don't forget to take your {medication_name} today. "
             "Consistent use gives the best results."),
            ("MEDICATION_REMINDER", LANG_TW,
             "Nnurow Nsusuw",
             "Nna sɛ wo tumi {medication_name} nnidi. "
             "Sɛ wode ano kɔ a, ɛbɛboa wo yiye."),
            ("REORDER_REMINDER",    LANG_EN,
             "Time to Reorder",
             "Your {medication_name} supply is running low. Visit your nearest "
             "AID System kiosk or order via the AID PLUS+ app."),
            ("REORDER_REMINDER",    LANG_TW,
             "Nsan Kɔ Tɔ",
             "Wo {medication_name} sua. Kɔ AID System kiosk a ɛkurɔ wo hɔ "
             "anaa fa AID PLUS+ app no so tɔ."),

            # ── OTA ───────────────────────────────────────────────────────
            ("OTA_APPLIED",      LANG_EN,
             "System Updated",
             "AID System has been updated to Build {new_version}. "
             "Your service continues with the latest improvements."),
        ]
        with self._conn() as con:
            con.executemany(
                "INSERT OR IGNORE INTO notification_templates "
                "(template_key, lang, title, body) VALUES (?,?,?,?)",
                templates)

    def _reset_hardware_to_docked(self):
        """Auto-correct stale Deployed status with no active return code."""
        with self._conn() as con:
            active = {r[0] for r in con.execute(
                "SELECT DISTINCT component FROM active_hardware_codes").fetchall()}
            hw = con.execute("SELECT * FROM hardware_status WHERE id=1").fetchone()
            if not hw:
                return
            if hw["aid_box_status"] == "Deployed" and "aid box" not in active:
                con.execute("UPDATE hardware_status SET aid_box_status='Docked' WHERE id=1")
            if hw["cpr_kit_status"] == "Deployed" and "cpr kit" not in active:
                con.execute("UPDATE hardware_status SET cpr_kit_status='Docked' WHERE id=1")


    def _fix_active_hardware_codes_pk(self):
        """
        Migrate active_hardware_codes from single PK (customer_id) to
        composite PK (customer_id, component) so two hardware items can
        be tracked per customer simultaneously.
        Safe to run on both old and new databases.
        """
        with self._conn() as con:
            # Check if table already has composite PK
            cols = con.execute(
                "PRAGMA table_info(active_hardware_codes)").fetchall()
            pk_cols = [c["name"] for c in cols if c["pk"] > 0]
            if "component" in pk_cols:
                return  # Already correct schema
            # Old schema — rebuild with composite PK
            # 1. Rename old table
            try:
                con.execute("ALTER TABLE active_hardware_codes "
                            "RENAME TO active_hardware_codes_old")
            except Exception:
                return  # Table doesn't exist yet
            # 2. Create new table with composite PK
            con.execute("""
                CREATE TABLE active_hardware_codes (
                    customer_id TEXT NOT NULL,
                    code        TEXT NOT NULL,
                    component   TEXT NOT NULL,
                    start_time  TEXT NOT NULL,
                    deposit     REAL NOT NULL DEFAULT 0.0,
                    PRIMARY KEY (customer_id, component)
                )""")
            # 3. Copy existing data
            try:
                con.execute("""
                    INSERT OR IGNORE INTO active_hardware_codes
                    SELECT customer_id, code, component, start_time, deposit
                    FROM active_hardware_codes_old""")
                con.execute("DROP TABLE active_hardware_codes_old")
            except Exception:
                pass


    def _migrate_medication_returns(self):
        """
        Rebuild medication_returns table without ANY unique constraint.
        Supports multiple partial return records per item.
        Safe to run on every boot — no-ops if already correct.
        """
        with self._conn() as con:
            # Check if ANY unique index exists on medication_returns
            has_unique = False
            for row in con.execute("PRAGMA index_list(medication_returns)").fetchall():
                if row[2]:  # unique=1
                    has_unique = True
                    break

            # Also check if old single-column UNIQUE on transaction_id exists
            # (original schema before item-level returns)
            cols = {r[1] for r in con.execute(
                "PRAGMA table_info(medication_returns)").fetchall()}
            missing_cols = not all(c in cols for c in
                                   ("item_id", "item_name", "qty_returned", "status"))

            if not has_unique and not missing_cols:
                return  # Already correct — nothing to do

            # Rebuild: rename old table, create new, copy data, drop old
            try:
                con.execute(
                    "ALTER TABLE medication_returns RENAME TO medication_returns_old")
                con.execute("""
                    CREATE TABLE medication_returns (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        transaction_id  TEXT NOT NULL,
                        item_id         INTEGER NOT NULL DEFAULT 0,
                        item_name       TEXT NOT NULL DEFAULT '',
                        qty_returned    INTEGER NOT NULL DEFAULT 1,
                        customer_id     TEXT NOT NULL,
                        returned_at     TEXT NOT NULL,
                        amount_refunded REAL NOT NULL DEFAULT 0.0,
                        status          TEXT NOT NULL DEFAULT 'refunded'
                    )""")
                # Copy existing data — adapt old schema to new
                con.execute("""
                    INSERT INTO medication_returns
                        (transaction_id, item_id, item_name, qty_returned,
                         customer_id, returned_at, amount_refunded, status)
                    SELECT
                        transaction_id,
                        COALESCE(item_id, 0),
                        COALESCE(item_name, ''),
                        COALESCE(qty_returned, 1),
                        customer_id,
                        returned_at,
                        COALESCE(amount_refunded, 0.0),
                        COALESCE(status, 'refunded')
                    FROM medication_returns_old
                """)
                con.execute("DROP TABLE medication_returns_old")
            except Exception:
                # If rename fails table doesn't exist yet — will be created fresh
                pass

    def _seed_inventory(self):
        with self._conn() as con:
            if con.execute("SELECT COUNT(*) FROM inventory").fetchone()[0] == 0:
                now = datetime.now().isoformat()
                for i, d in enumerate(INITIAL_DRUGS):
                    con.execute(
                        "INSERT INTO inventory VALUES (?,?,?,0,?,?,?,?)",
                        (i + 1, d["name"], d["shelf"],
                         d["base_price"], d["base_price"], MAX_CAPS_PER_SHELF, now)
                    )
            if con.execute("SELECT COUNT(*) FROM mega_inventory").fetchone()[0] == 0:
                now = datetime.now().isoformat()
                for i, d in enumerate(INITIAL_MEGA_ITEMS):
                    con.execute(
                        "INSERT INTO mega_inventory VALUES (?,?,?,1,?,?,?,?)",
                        (i + 101, d["name"], d["shelf"],
                         d["base_price"], d["base_price"], MAX_MEGA_PER_SHELF, now)
                    )

    def _seed_hardware(self):
        with self._conn() as con:
            if con.execute("SELECT COUNT(*) FROM hardware_status").fetchone()[0] == 0:
                con.execute("INSERT INTO hardware_status (id) VALUES (1)")

    # ══════════════════════════════════════════════════════════════════════════
    # PDMS AUDIT LOG  [A2] [A3]
    # ══════════════════════════════════════════════════════════════════════════
    def log_audit(self, actor_id: str, action: str,
                  table_name: str = None, record_id: str = None,
                  detail: str = None):
        """
        Central audit logger.  Called from every significant mutation.
        Append-only — this method never updates or deletes rows.
        """
        with self._conn() as con:
            con.execute(
                "INSERT INTO pdms_audit_log "
                "(timestamp, actor_id, action, table_name, record_id, detail) "
                "VALUES (?,?,?,?,?,?)",
                (datetime.now().isoformat(), actor_id, action,
                 table_name, record_id, detail)
            )

    def get_audit_log(self, actor_id: str = None,
                      action: str = None, limit: int = 100) -> list:
        with self._conn() as con:
            if actor_id and action:
                rows = con.execute(
                    "SELECT * FROM pdms_audit_log "
                    "WHERE actor_id=? AND action=? ORDER BY timestamp DESC LIMIT ?",
                    (actor_id, action, limit)
                ).fetchall()
            elif actor_id:
                rows = con.execute(
                    "SELECT * FROM pdms_audit_log WHERE actor_id=? "
                    "ORDER BY timestamp DESC LIMIT ?",
                    (actor_id, limit)
                ).fetchall()
            elif action:
                rows = con.execute(
                    "SELECT * FROM pdms_audit_log WHERE action=? "
                    "ORDER BY timestamp DESC LIMIT ?",
                    (action, limit)
                ).fetchall()
            else:
                rows = con.execute(
                    "SELECT * FROM pdms_audit_log ORDER BY timestamp DESC LIMIT ?",
                    (limit,)
                ).fetchall()
            return [dict(r) for r in rows]

    def get_audit_count(self) -> int:
        with self._conn() as con:
            return con.execute(
                "SELECT COUNT(*) FROM pdms_audit_log"
            ).fetchone()[0]

    # ══════════════════════════════════════════════════════════════════════════
    # INVENTORY
    # ══════════════════════════════════════════════════════════════════════════
    def get_all_shelves(self) -> list:
        with self._conn() as con:
            return [dict(r) for r in
                    con.execute("SELECT * FROM inventory ORDER BY shelf")]

    def get_all_mega_shelves(self) -> list:
        with self._conn() as con:
            return [dict(r) for r in
                    con.execute("SELECT * FROM mega_inventory ORDER BY shelf")]

    def get_item_by_shelf(self, shelf_num: int) -> dict | None:
        with self._conn() as con:
            row = con.execute(
                "SELECT * FROM inventory WHERE shelf=?", (shelf_num,)
            ).fetchone()
            if row:
                return dict(row)
            row = con.execute(
                "SELECT * FROM mega_inventory WHERE shelf=?", (shelf_num,)
            ).fetchone()
            return dict(row) if row else None

    def get_item_by_barcode(self, barcode: str) -> dict | None:
        sn = ALL_BARCODES_TO_SHELF.get(barcode.upper())
        return self.get_item_by_shelf(sn) if sn else None

    def get_stock(self, item: dict) -> int:
        return item.get("units_left", 0) if item.get("is_mega") \
               else item.get("capsules_left", 0)

    def update_prices(self):
        """Archive old price to price_history then apply market drift. [B14]"""
        now = datetime.now().isoformat()
        with self._conn() as con:
            for drug_id, base, curr in con.execute(
                "SELECT drug_id, base_price, current_price FROM inventory"
            ).fetchall():
                con.execute(
                    "INSERT INTO price_history (drug_id,table_name,price,recorded_at) VALUES (?,?,?,?)",
                    (drug_id, "inventory", curr, now))
                con.execute(
                    "UPDATE inventory SET current_price=?, last_updated=? WHERE drug_id=?",
                    (round(base * random.uniform(0.9, 1.1), 2), now, drug_id))
            for drug_id, base, curr in con.execute(
                "SELECT drug_id, base_price, current_price FROM mega_inventory"
            ).fetchall():
                con.execute(
                    "INSERT INTO price_history (drug_id,table_name,price,recorded_at) VALUES (?,?,?,?)",
                    (drug_id, "mega_inventory", curr, now))
                con.execute(
                    "UPDATE mega_inventory SET current_price=?, last_updated=? WHERE drug_id=?",
                    (round(base * random.uniform(0.9, 1.1), 2), now, drug_id))

    def get_price_history(self, drug_id: int, table_name: str, limit: int = 30) -> list:
        with self._conn() as con:
            return [dict(r) for r in con.execute(
                "SELECT price, recorded_at FROM price_history "
                "WHERE drug_id=? AND table_name=? ORDER BY recorded_at DESC LIMIT ?",
                (drug_id, table_name, limit))]

    def dispense(self, shelf_num: int, qty: int, is_mega: bool) -> tuple:
        item = self.get_item_by_shelf(shelf_num)
        if not item:
            return False, "Invalid shelf"
        stock = self.get_stock(item)
        if stock < qty:
            return False, "Insufficient stock"
        new_stock = stock - qty
        self.__nyansa_logistics_alert(item["name"], new_stock)
        with self._conn() as con:
            if is_mega:
                con.execute(
                    "UPDATE mega_inventory SET units_left=? WHERE shelf=?",
                    (new_stock, shelf_num)
                )
                if new_stock <= LOW_STOCK_MEGA_THRESHOLD:
                    print(f"ALERT: Low stock — {item['name']} ({new_stock} left)")
            else:
                con.execute(
                    "UPDATE inventory SET capsules_left=? WHERE shelf=?",
                    (new_stock, shelf_num)
                )
                if new_stock <= LOW_STOCK_THRESHOLD:
                    print(f"ALERT: Low stock — {item['name']} ({new_stock} left)")
        return True, f"Dispensed {qty} × {item['name']}"

    def refill_upper(self, shelf_num: int):
        with self._conn() as con:
            con.execute(
                "UPDATE inventory SET capsules_left=? WHERE shelf=?",
                (MAX_CAPS_PER_SHELF, shelf_num)
            )

    def refill_mega(self, shelf_num: int):
        with self._conn() as con:
            con.execute(
                "UPDATE mega_inventory SET units_left=? WHERE shelf=?",
                (MAX_MEGA_PER_SHELF, shelf_num)
            )

    # ── Wallet convenience wrappers ───────────────────────────────────────────
    def credit_wallet(self, customer_id: str, amount: float, note: str = "") -> None:
        """Credit amount to customer wallet. Alias for add_wallet_entry."""
        self.add_wallet_entry(customer_id, "credit", amount, note)
        with self._conn() as con:
            con.execute(
                "UPDATE customers SET balance=balance+? WHERE customer_id=?",
                (amount, customer_id))

    def debit_wallet(self, customer_id: str, amount: float, note: str = "") -> None:
        """Debit amount from customer wallet. Alias for add_wallet_entry."""
        self.add_wallet_entry(customer_id, "debit", -abs(amount), note)
        with self._conn() as con:
            con.execute(
                "UPDATE customers SET balance=MAX(0,balance-?) WHERE customer_id=?",
                (abs(amount), customer_id))

    # ── Cart convenience wrappers ─────────────────────────────────────────────
    def add_to_cart(self, customer_id: str, shelf_num: int, qty: int) -> None:
        """Add or update item in customer cart."""
        with self._conn() as con:
            existing = con.execute(
                "SELECT id, qty FROM customer_cart WHERE customer_id=? AND shelf_num=?",
                (customer_id, shelf_num)).fetchone()
            if existing:
                con.execute(
                    "UPDATE customer_cart SET qty=? WHERE id=?",
                    (existing["qty"] + qty, existing["id"]))
            else:
                con.execute(
                    "INSERT INTO customer_cart (customer_id, shelf_num, qty, added_at) "
                    "VALUES (?,?,?,?)",
                    (customer_id, shelf_num, qty,
                     __import__('datetime').datetime.now().isoformat()))

    def remove_from_cart(self, customer_id: str, shelf_num: int) -> None:
        """Remove item from customer cart."""
        with self._conn() as con:
            con.execute(
                "DELETE FROM customer_cart WHERE customer_id=? AND shelf_num=?",
                (customer_id, shelf_num))

    # ── CUPSCAN daily alias ───────────────────────────────────────────────────
    def cupscan_get_daily(self, customer_id: str,
                           date_str: str = "") -> dict:
        """Alias for cupscan_get_daily_count with today as default date."""
        from datetime import datetime
        d = date_str or datetime.now().strftime("%Y-%m-%d")
        return self.cupscan_get_daily_count(customer_id, d)

    def decrement_upper(self, shelf_num: int, qty: int = 1) -> bool:
        """Decrement capsules_left for an upper-section shelf by qty. Returns True on success."""
        with self._conn() as con:
            row = con.execute(
                "SELECT capsules_left FROM inventory WHERE shelf=?",
                (shelf_num,)).fetchone()
            if not row or row["capsules_left"] < qty:
                return False
            new_stock = row["capsules_left"] - qty
            con.execute(
                "UPDATE inventory SET capsules_left=? WHERE shelf=?",
                (new_stock, shelf_num))
            if new_stock <= LOW_STOCK_THRESHOLD:
                self.__nyansa_logistics_alert(
                    f"Shelf {shelf_num}", new_stock)
        return True

    def decrement_mega(self, shelf_num: int, qty: int = 1) -> bool:
        """Decrement units_left for a mega-section shelf by qty. Returns True on success."""
        with self._conn() as con:
            row = con.execute(
                "SELECT units_left FROM mega_inventory WHERE shelf=?",
                (shelf_num,)).fetchone()
            if not row or row["units_left"] < qty:
                return False
            new_stock = row["units_left"] - qty
            con.execute(
                "UPDATE mega_inventory SET units_left=? WHERE shelf=?",
                (new_stock, shelf_num))
            if new_stock <= LOW_STOCK_MEGA_THRESHOLD:
                self.__nyansa_logistics_alert(
                    f"Mega Shelf {shelf_num}", new_stock)
        return True

    def __nyansa_logistics_alert(self, name: str, remaining: int):
        if remaining <= LOW_STOCK_THRESHOLD:
            print(f"\n[Nyansa Logistics]: Predictive surge detected for {name}!")
            print("ACTION: Automatic restocking request sent to Central Warehouse.")

    # ── Returns ────────────────────────────────────────────────────────────────
    def record_return(self, drug_id: int, photo_path: str | None):
        with self._conn() as con:
            existing = con.execute(
                "SELECT count FROM returned_items WHERE drug_id=?", (drug_id,)
            ).fetchone()
            if existing:
                con.execute(
                    "UPDATE returned_items SET count=count+1 WHERE drug_id=?", (drug_id,)
                )
            else:
                con.execute(
                    "INSERT INTO returned_items (drug_id, count) VALUES (?,1)", (drug_id,)
                )
            if photo_path:
                con.execute(
                    "INSERT INTO return_photos (drug_id, photo_path, captured_at) VALUES (?,?,?)",
                    (drug_id, photo_path, datetime.now().isoformat())
                )

    def get_total_returned(self) -> int:
        with self._conn() as con:
            row = con.execute(
                "SELECT SUM(count) AS total FROM returned_items"
            ).fetchone()
            return row["total"] or 0

    def get_return_photos(self, drug_id: int) -> list:
        with self._conn() as con:
            return [dict(r) for r in con.execute(
                "SELECT * FROM return_photos WHERE drug_id=? ORDER BY captured_at",
                (drug_id,)
            )]

    def return_capsule(self, shelf_num: int) -> tuple:
        item = self.get_item_by_shelf(shelf_num)
        if not item or item.get("is_mega"):
            return False, "Only Upper Section (capsule) items are eligible for returns."
        drug_id = item["drug_id"]
        os.makedirs("returns", exist_ok=True)
        cap        = cv2.VideoCapture(0)
        photo_path = None
        start_t    = time.time()
        print("\n--- CAPTURING RETURN PROOF ---")
        print("Point the capsule at the camera. Press 's' to save.")
        while time.time() - start_t < 20:
            ret, frame = cap.read()
            if not ret:
                break
            cv2.imshow("Capture return proof — press 's' to save", frame)
            if cv2.waitKey(1) == ord('s'):
                ts         = datetime.now().strftime('%Y%m%d_%H%M%S')
                photo_path = f"returns/{drug_id}_{ts}.jpg"
                cv2.imwrite(photo_path, frame)
                break
        cap.release()
        cv2.destroyAllWindows()
        self.record_return(drug_id, photo_path)
        if photo_path:
            print(f"✅ Photo proof saved: {photo_path}")
        return True, f"1 × {item['name']} capsule returned to backend storage"

    # ══════════════════════════════════════════════════════════════════════════
    # FACE SIGNATURES
    # ══════════════════════════════════════════════════════════════════════════
    def save_face_signature(self, customer_id: str, signature: list,
                             con: sqlite3.Connection = None):
        """
        Writes the 128-float biometric vector.
        Accepts an optional open connection for use inside atomic transactions.
        """
        def _write(c):
            c.execute(
                "DELETE FROM customer_face_signatures WHERE customer_id=?",
                (customer_id,)
            )
            c.executemany(
                "INSERT INTO customer_face_signatures "
                "(customer_id, position, value) VALUES (?,?,?)",
                [(customer_id, i, float(v)) for i, v in enumerate(signature)]
            )
        if con:
            _write(con)
        else:
            with self._conn() as c:
                _write(c)

    def get_face_signature(self, customer_id: str) -> list:
        with self._conn() as con:
            rows = con.execute(
                "SELECT value FROM customer_face_signatures "
                "WHERE customer_id=? ORDER BY position",
                (customer_id,)
            ).fetchall()
            return [r["value"] for r in rows]

    # ══════════════════════════════════════════════════════════════════════════
    # HEALTH TRENDS
    # ══════════════════════════════════════════════════════════════════════════
    def add_health_trend(self, customer_id: str, trend_type: str, value: float):
        with self._conn() as con:
            con.execute(
                "INSERT INTO customer_health_trends "
                "(customer_id, trend_type, value, recorded_at) VALUES (?,?,?,?)",
                (customer_id, trend_type, value, datetime.now().isoformat())
            )

    def get_health_trends(self, customer_id: str) -> dict:
        with self._conn() as con:
            rows = con.execute(
                "SELECT trend_type, value FROM customer_health_trends "
                "WHERE customer_id=? ORDER BY recorded_at",
                (customer_id,)
            ).fetchall()
        trends: dict = {"temp": [], "weight": []}
        for r in rows:
            trends[r["trend_type"]].append(r["value"])
        return trends

    # ══════════════════════════════════════════════════════════════════════════
    # SHOPPING CART
    # ══════════════════════════════════════════════════════════════════════════
    def get_cart(self, customer_id: str) -> list:
        with self._conn() as con:
            return [dict(r) for r in con.execute(
                "SELECT shelf_num, name, qty, price_per, base_price, "
                "is_mega_item, nhis_discounted, nhis_discounted_primary "
                "FROM customer_cart WHERE customer_id=? ORDER BY id",
                (customer_id,)
            )]

    def upsert_cart_item(self, customer_id: str, item: dict):
        with self._conn() as con:
            existing = con.execute(
                "SELECT id FROM customer_cart WHERE customer_id=? AND shelf_num=?",
                (customer_id, item["shelf_num"])
            ).fetchone()
            if existing:
                con.execute(
                    "UPDATE customer_cart SET qty=?, price_per=?, base_price=?, "
                    "nhis_discounted=?, nhis_discounted_primary=? "
                    "WHERE customer_id=? AND shelf_num=?",
                    (
                        item["qty"],
                        item["price_per"],
                        item.get("base_price", item["price_per"]),
                        1 if item.get("nhis_discounted")         else 0,
                        1 if item.get("nhis_discounted_primary") else 0,
                        customer_id, item["shelf_num"],
                    )
                )
            else:
                con.execute(
                    "INSERT INTO customer_cart "
                    "(customer_id, shelf_num, name, qty, price_per, base_price, "
                    "is_mega_item, nhis_discounted, nhis_discounted_primary) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        customer_id,
                        item["shelf_num"],
                        item["name"],
                        item["qty"],
                        item["price_per"],
                        item.get("base_price", item["price_per"]),
                        1 if item.get("is_mega_item")            else 0,
                        1 if item.get("nhis_discounted")         else 0,
                        1 if item.get("nhis_discounted_primary") else 0,
                    )
                )

    def remove_cart_item(self, customer_id: str, shelf_num: int):
        with self._conn() as con:
            con.execute(
                "DELETE FROM customer_cart WHERE customer_id=? AND shelf_num=?",
                (customer_id, shelf_num)
            )

    def clear_cart(self, customer_id: str):
        with self._conn() as con:
            con.execute(
                "DELETE FROM customer_cart WHERE customer_id=?", (customer_id,)
            )

    # ══════════════════════════════════════════════════════════════════════════
    # CUSTOMERS
    # ══════════════════════════════════════════════════════════════════════════
    def _hydrate_customer(self, c: dict) -> dict:
        """Attach relational sub-objects to a flat customer row dict."""
        cid = c["customer_id"]
        c["face_signature"] = self.get_face_signature(cid)
        c["health_trends"]  = self.get_health_trends(cid)
        c["cart"]           = self.get_cart(cid)
        c["notifications"]  = {
            "email": bool(c.get("notification_email")),
            "sms":   bool(c.get("notification_sms")),
        }
        return c

    def get_customer(self, customer_id: str) -> dict | None:
        with self._conn() as con:
            row = con.execute(
                "SELECT * FROM customers WHERE customer_id=?", (customer_id,)
            ).fetchone()
        if not row:
            return None
        return self._hydrate_customer(dict(row))

    def get_all_customers(self) -> list:
        """
        [P1] Batch-hydrates every customer in exactly 4 queries total:
             1 for customers, 1 for face signatures, 1 for health trends,
             1 for carts.  Eliminates the N+1 problem from Build 11.
        """
        with self._conn() as con:
            customers = [dict(r) for r in con.execute("SELECT * FROM customers")]
            if not customers:
                return []
            cids = [c["customer_id"] for c in customers]
            placeholders = ",".join("?" * len(cids))

            # Bulk load face signatures
            sig_rows = con.execute(
                f"SELECT customer_id, position, value FROM customer_face_signatures "
                f"WHERE customer_id IN ({placeholders}) ORDER BY customer_id, position",
                cids
            ).fetchall()
            sigs: dict = {}
            for r in sig_rows:
                sigs.setdefault(r["customer_id"], []).append(r["value"])

            # Bulk load health trends
            trend_rows = con.execute(
                f"SELECT customer_id, trend_type, value FROM customer_health_trends "
                f"WHERE customer_id IN ({placeholders}) ORDER BY customer_id, recorded_at",
                cids
            ).fetchall()
            trends: dict = {}
            for r in trend_rows:
                bucket = trends.setdefault(r["customer_id"],
                                           {"temp": [], "weight": []})
                bucket[r["trend_type"]].append(r["value"])

            # Bulk load carts
            cart_rows = con.execute(
                f"SELECT customer_id, shelf_num, name, qty, price_per, base_price, "
                f"is_mega_item, nhis_discounted, nhis_discounted_primary "
                f"FROM customer_cart WHERE customer_id IN ({placeholders}) ORDER BY id",
                cids
            ).fetchall()
            carts: dict = {}
            for r in cart_rows:
                carts.setdefault(r["customer_id"], []).append(dict(r))

        # Assemble
        result = []
        for c in customers:
            cid = c["customer_id"]
            c["face_signature"] = sigs.get(cid, [])
            c["health_trends"]  = trends.get(cid, {"temp": [], "weight": []})
            c["cart"]           = carts.get(cid, [])
            c["notifications"]  = {
                "email": bool(c.get("notification_email")),
                "sms":   bool(c.get("notification_sms")),
            }
            result.append(c)
        return result

    def create_customer(self, data: dict):
        """
        [P2] Atomic: customer row + face signature in single transaction.
        [P3] Collision-safe ID regeneration.
        [S1] Hashes password before storing.
        [B21-A] All B20 extended health fields written atomically.
        [B21-D] Security answer hashed with its own independent salt.
        [A2] Writes PDMS audit entry.
        """
        customer_id = data["customer_id"]
        with self._conn() as con:
            # [P3] Collision check
            if con.execute(
                "SELECT COUNT(*) FROM customers WHERE customer_id=?",
                (customer_id,)
            ).fetchone()[0] > 0:
                customer_id = secure_id(8)
                data["customer_id"] = customer_id

            # [S1] Hash password
            pwd_hash, pwd_salt = hash_password(data["password"])

            # [B21-D] Hash security answer with its own independent salt
            sec_raw              = data.get("security_answer", "")
            sec_hash, sec_salt   = hash_password(sec_raw) if sec_raw else ("", "")

            # [P2] All writes in single atomic block — [B21-A] all fields included
            con.execute("""
                INSERT INTO customers (
                    customer_id, name, dob, age, gender, address,
                    password, password_salt,
                    email, contact, health_info,
                    nhis_active, nhis_number, nhis_last_verified,
                    health_status, balance, bonus,
                    wallet_tier, card_type, status,
                    voice_guidance, notification_email, notification_sms,
                    loyalty_points, lifetime_points,
                    blood_group, allergies, chronic_conditions,
                    current_medications, ghana_card_number,
                    ghana_card_verified, identity_verified,
                    security_question, security_answer, security_answer_salt,
                    health_consent, card_uid
                ) VALUES (
                    ?,?,?,?,?,?,?,?,?,?,?,
                    ?,?,?,?,0.0,0.0,
                    'G0','digital','Active',0,?,?,0,0,
                    ?,?,?,?,?,0,0,?,?,?,?,?
                )
            """, (
                customer_id,
                data["name"], data["dob"], data["age"],
                data.get("gender", ""), data.get("address", ""),
                pwd_hash, pwd_salt,
                data.get("email", ""), data.get("contact", ""),
                data.get("health_info", ""),
                1 if data.get("nhis_active") else 0,
                data.get("nhis_number", ""), None,
                "Unknown" if not data.get("health_info") else "Review pending",
                1 if data.get("email")   else 0,
                1 if data.get("contact") else 0,
                # B20/B21 extended fields
                data.get("blood_group", ""),
                data.get("allergies", "[]"),
                data.get("chronic_conditions", "[]"),
                data.get("current_medications", "[]"),
                data.get("ghana_card_number", ""),
                data.get("security_question", ""),
                sec_hash, sec_salt,
                1 if data.get("health_consent") else 0,
                data.get("card_uid", None),
            ))
        # [A2] Audit — happens before face sig so customer row is committed first
        self.log_audit("SYSTEM", "CREATE_ACCOUNT", "customers", customer_id,
                       f"New account: {data['name']}")

        # Save face signature AFTER commit so FK constraint passes
        if data.get("face_signature"):
            try:
                self.save_face_signature(customer_id, data["face_signature"])
            except Exception:
                pass  # non-fatal — can re-enroll later

    def save_customer(self, c: dict):
        """
        Persists all mutable scalar fields.
        Does NOT touch password_salt or security_answer_salt.
        [B21-B] Extended to persist all B20+B21 fields.
        """
        with self._conn() as con:
            con.execute("""
                UPDATE customers SET
                    name=?,                     password=?,
                    email=?,                    contact=?,
                    health_info=?,              nhis_active=?,
                    nhis_number=?,              nhis_last_verified=?,
                    nhis_session_active=?,      health_status=?,
                    balance=?,                  bonus=?,
                    wallet_tier=?,              card_type=?,
                    status=?,                   login_attempts=?,
                    lockout_until=?,            voice_guidance=?,
                    return_count=?,             loyalty_points=?,
                    lifetime_points=?,          has_physical_card=?,
                    prescription_active_until=?,notification_email=?,
                    notification_sms=?,
                    blood_group=?,              allergies=?,
                    chronic_conditions=?,       current_medications=?,
                    ghana_card_number=?,        ghana_card_verified=?,
                    identity_verified=?,        security_question=?,
                    health_consent=?,           card_uid=?,
                    sync_token=?,               sync_token_expires=?
                WHERE customer_id=?
            """, (
                c["name"],             c["password"],
                c.get("email",""),     c.get("contact",""),
                c.get("health_info",""),
                1 if c.get("nhis_active")         else 0,
                c.get("nhis_number",""),
                c.get("nhis_last_verified"),
                1 if c.get("nhis_session_active") else 0,
                c.get("health_status","Unknown"),
                c.get("balance",  0.0),
                c.get("bonus",    0.0),
                c.get("wallet_tier","G0"),
                c.get("card_type","digital"),
                c.get("status","Active"),
                c.get("login_attempts", 0),
                c.get("lockout_until"),
                1 if c.get("voice_guidance")      else 0,
                c.get("return_count",  0),
                c.get("loyalty_points",  0),
                c.get("lifetime_points", 0),
                1 if c.get("has_physical_card")   else 0,
                c.get("prescription_active_until"),
                1 if c.get("notifications",{}).get("email") else 0,
                1 if c.get("notifications",{}).get("sms")   else 0,
                # B20/B21 fields
                c.get("blood_group", ""),
                c.get("allergies", "[]"),
                c.get("chronic_conditions", "[]"),
                c.get("current_medications", "[]"),
                c.get("ghana_card_number", ""),
                1 if c.get("ghana_card_verified") else 0,
                1 if c.get("identity_verified")   else 0,
                c.get("security_question", ""),
                1 if c.get("health_consent")      else 0,
                c.get("card_uid"),
                c.get("sync_token"),
                c.get("sync_token_expires"),
                c["customer_id"],
            ))

    def update_password(self, customer_id: str, new_password: str):
        """
        [S1] Hashes the new password and saves both hash and fresh salt.
        Always use this method — never set password directly via save_customer.
        """
        pwd_hash, pwd_salt = hash_password(new_password)
        with self._conn() as con:
            con.execute(
                "UPDATE customers SET password=?, password_salt=? WHERE customer_id=?",
                (pwd_hash, pwd_salt, customer_id)
            )
        self.log_audit(customer_id, "PASSWORD_CHANGE", "customers", customer_id,
                       "Password updated")

    def verify_customer_password(self, customer_id: str, candidate: str) -> bool:
        """
        [S1] Verifies password.  Handles auto-upgrade for legacy plaintext rows.
        Returns True on match and transparently re-hashes plaintext records.
        """
        with self._conn() as con:
            row = con.execute(
                "SELECT password, password_salt FROM customers WHERE customer_id=?",
                (customer_id,)
            ).fetchone()
        if not row:
            return False
        stored_hash = row["password"]
        stored_salt = row["password_salt"]

        if stored_salt is None:
            # Legacy Build-11 record: compare plaintext then upgrade
            if stored_hash == candidate:
                self.update_password(customer_id, candidate)
                self.log_audit("SYSTEM", "ADMIN_ACTION", "customers", customer_id,
                               "Plaintext password auto-upgraded to SHA-256+salt")
                return True
            return False

        return verify_password(stored_hash, stored_salt, candidate)

    def delete_customer(self, customer_id: str, actor_id: str = "ADMIN"):
        """CASCADE handles child tables. Audit entry written before deletion."""
        c = self.get_customer(customer_id)
        name = c["name"] if c else "Unknown"
        self.log_audit(actor_id, "DELETE_ACCOUNT", "customers", customer_id,
                       f"Deleted account: {name}")
        with self._conn() as con:
            con.execute("DELETE FROM customers    WHERE customer_id=?", (customer_id,))
            con.execute("DELETE FROM transactions WHERE customer_id=?", (customer_id,))
            con.execute("DELETE FROM feedback     WHERE customer_id=?", (customer_id,))

    # ── Notifications ──────────────────────────────────────────────────────────
    def send_notification(self, customer_id: str, subject: str, message: str):
        with self._conn() as con:
            con.execute(
                "INSERT INTO notifications (customer_id,timestamp,tag,message) VALUES (?,?,?,?)",
                (customer_id, datetime.now().isoformat(),
                 f"[SYSTEM ALERT: {subject}]", message)
            )

    def get_notifications(self, customer_id: str) -> list:
        with self._conn() as con:
            return [dict(r) for r in con.execute(
                "SELECT * FROM notifications WHERE customer_id=? ORDER BY timestamp DESC",
                (customer_id,)
            )]

    def delete_notification(self, notif_id: int):
        with self._conn() as con:
            con.execute("DELETE FROM notifications WHERE id=?", (notif_id,))

    # ── Wallet history ─────────────────────────────────────────────────────────
    def add_wallet_entry(self, customer_id: str, trans_type: str,
                         amount: float, description: str = "",
                         trans_id: str = "") -> str:
        if not trans_id:
            trans_id = secure_ref(trans_type[:3].upper())    # [S2]
        with self._conn() as con:
            con.execute(
                "INSERT INTO wallet_history "
                "(id, customer_id, type, timestamp, created_at, "
                "amount, balance_after, description, note) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (trans_id, customer_id, trans_type,
                 datetime.now().isoformat(),
                 datetime.now().isoformat(),
                 amount, 0.0, description, "")
            )
        return trans_id

    def get_wallet_history(self, customer_id: str) -> list:
        with self._conn() as con:
            return [dict(r) for r in con.execute(
                "SELECT * FROM wallet_history WHERE customer_id=? ORDER BY timestamp DESC",
                (customer_id,)
            )]

    # ── Transactions ───────────────────────────────────────────────────────────
    def record_transaction(self, customer_id: str, items: list,
                           total: float, badge: str,
                           nhis_savings: float = 0.0) -> str:
        trans_id = secure_ref("PUR")    # [S2]
        with self._conn() as con:
            con.execute(
                "INSERT INTO transactions "
                "(id, transaction_id, customer_id, timestamp, badge, "
                "total, nhis_savings, status, unit_id) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (trans_id, trans_id, customer_id,
                 datetime.now().isoformat(),
                 badge, total, nhis_savings, 'complete', ADWENE_SERIAL)
            )
            con.executemany(
                "INSERT INTO transaction_items "
                "(transaction_id,name,shelf_num,qty,price_per,base_price,"
                "is_mega_item,nhis_discounted) VALUES (?,?,?,?,?,?,?,?)",
                [
                    (
                        trans_id, item["name"], item.get("shelf_num", 0),
                        item["qty"], item["price_per"],
                        item.get("base_price", item["price_per"]),
                        1 if item.get("is_mega_item") else 0,
                        1 if (item.get("nhis_discounted") or
                              item.get("nhis_discounted_primary")) else 0,
                    )
                    for item in items
                ]
            )
        self.log_audit(customer_id, "PURCHASE", "transactions", trans_id,
                       f"₵{total:.2f} | {len(items)} item(s)")
        return trans_id

    def get_all_transactions(self) -> list:
        with self._conn() as con:
            rows = con.execute(
                "SELECT * FROM transactions ORDER BY timestamp DESC"
            ).fetchall()
            out = []
            for r in rows:
                t = dict(r)
                t["items"] = [dict(i) for i in con.execute(
                    "SELECT * FROM transaction_items WHERE transaction_id=?",
                    (t["id"],)
                )]
                out.append(t)
            return out

    def get_customer_transactions(self, customer_id: str,
                                   hours: int = DAILY_PURCHASE_LIMIT_HOURS) -> list:
        cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
        with self._conn() as con:
            rows = con.execute(
                "SELECT * FROM transactions "
                "WHERE customer_id=? AND timestamp>? "
                "AND COALESCE(status,'completed') != 'donated' "
                "ORDER BY timestamp",
                (customer_id, cutoff)
            ).fetchall()
            out = []
            for r in rows:
                t = dict(r)
                items = [dict(i) for i in con.execute(
                    "SELECT * FROM transaction_items WHERE transaction_id=?",
                    (t["id"],)
                )]
                # Subtract returned quantities so limit reflects only held drugs
                for item in items:
                    ret = con.execute(
                        "SELECT COALESCE(SUM(qty_returned),0) "
                        "FROM medication_returns "
                        "WHERE transaction_id=? AND item_id=?",
                        (t["id"], item["id"])).fetchone()[0]
                    item["qty"] = max(0, item["qty"] - ret)
                # Only include items still held (not fully returned)
                t["items"] = [i for i in items if i["qty"] > 0]
                out.append(t)
            return out

    def get_daily_slot_count(self, customer_id: str,
                                hours: int = DAILY_PURCHASE_LIMIT_HOURS) -> int:
        """
        Count drugs purchased in last 24h for the purchase LIMIT check.
        [B29 FIX] Subtracts returned quantities so that returning a drug
        within the same 24h window frees up the slot for a new purchase.
        The 24h window itself still resets naturally after DAILY_PURCHASE_LIMIT_HOURS.
        """
        cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
        with self._conn() as con:
            purchased = con.execute(
                "SELECT COALESCE(SUM(ti.qty), 0) "
                "FROM transaction_items ti "
                "JOIN transactions t ON t.id = ti.transaction_id "
                "WHERE t.customer_id=? AND t.timestamp>?",
                (customer_id, cutoff)).fetchone()[0]

            returned = con.execute(
                "SELECT COALESCE(SUM(qty_returned), 0) "
                "FROM medication_returns "
                "WHERE customer_id=? AND returned_at>?",
                (customer_id, cutoff)).fetchone()[0]

        return max(0, int(purchased) - int(returned))

    # ── Feedback ───────────────────────────────────────────────────────────────
    def add_feedback(self, customer_id: str, name: str, tag: str, message: str):
        with self._conn() as con:
            con.execute(
                "INSERT INTO feedback (timestamp,customer_id,name,tag,message) VALUES (?,?,?,?,?)",
                (datetime.now().isoformat(), customer_id, name, tag, message)
            )

    def get_all_feedback(self) -> list:
        with self._conn() as con:
            return [dict(r) for r in con.execute(
                "SELECT * FROM feedback ORDER BY timestamp DESC"
            )]

    def clear_feedback(self):
        with self._conn() as con:
            con.execute("DELETE FROM feedback")

    def delete_feedback(self, feedback_id: int):
        with self._conn() as con:
            con.execute("DELETE FROM feedback WHERE id=?", (feedback_id,))

    # ── Hardware ───────────────────────────────────────────────────────────────
    def reset_hardware_to_docked(self) -> None:
        """Reset all hardware to Docked state — used for fresh testing."""
        with self._conn() as con:
            con.execute("""UPDATE hardware_status SET
                aid_box_status='Docked', cpr_kit_status='Docked',
                aid_box_usage=0, cpr_kit_usage=0
                WHERE id=1""")
        self.log_audit("SYSTEM","HW_RESET",detail="Hardware reset to Docked")

    def get_hw_status(self) -> dict:
        with self._conn() as con:
            row = con.execute(
                "SELECT * FROM hardware_status WHERE id=1"
            ).fetchone()
            return dict(row) if row else {}

    def update_hw_field(self, field: str, value):
        """
        [S3] Field name is validated against the allowlist before interpolation.
        Raises ValueError immediately on any unrecognised field name.
        """
        if field not in ALLOWED_HW_FIELDS:
            raise ValueError(
                f"update_hw_field: '{field}' is not an allowed hardware field. "
                f"Allowed: {sorted(ALLOWED_HW_FIELDS)}"
            )
        with self._conn() as con:
            con.execute(
                f"UPDATE hardware_status SET {field}=? WHERE id=1", (value,)
            )

    def get_active_code(self, customer_id: str, component: str = None) -> dict | None:
        """Get active code. If component given, get that specific item. Otherwise get first."""
        with self._conn() as con:
            if component:
                row = con.execute(
                    "SELECT * FROM active_hardware_codes WHERE customer_id=? AND component=?",
                    (customer_id, component)
                ).fetchone()
            else:
                row = con.execute(
                    "SELECT * FROM active_hardware_codes WHERE customer_id=? ORDER BY start_time DESC",
                    (customer_id,)
                ).fetchone()
            return dict(row) if row else None

    def get_all_active_codes(self, customer_id: str) -> list:
        """Get all active deployments for a customer."""
        with self._conn() as con:
            rows = con.execute(
                "SELECT * FROM active_hardware_codes WHERE customer_id=? ORDER BY start_time",
                (customer_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    def set_active_code(self, customer_id: str, code: str,
                        component: str, deposit: float):
        """Store return code per customer+component — allows two simultaneous deployments."""
        with self._conn() as con:
            # Delete existing entry for this specific component first
            con.execute(
                "DELETE FROM active_hardware_codes WHERE customer_id=? AND component=?",
                (customer_id, component)
            )
            con.execute(
                "INSERT INTO active_hardware_codes VALUES (?,?,?,?,?)",
                (customer_id, code, component, datetime.now().isoformat(), deposit)
            )

    def clear_active_code(self, customer_id: str, component: str = None):
        """Clear code for a specific component, or all codes for the customer."""
        with self._conn() as con:
            if component:
                con.execute(
                    "DELETE FROM active_hardware_codes WHERE customer_id=? AND component=?",
                    (customer_id, component)
                )
            else:
                con.execute(
                    "DELETE FROM active_hardware_codes WHERE customer_id=?",
                    (customer_id,)
                )

    def has_active_code(self, customer_id: str) -> bool:
        """Check if customer has any active hardware deployment."""
        with self._conn() as con:
            count = con.execute(
                "SELECT COUNT(*) FROM active_hardware_codes WHERE customer_id=?",
                (customer_id,)
            ).fetchone()[0]
        return count > 0

    # ── Emergency logs ─────────────────────────────────────────────────────────
    def add_emergency_log(self, **kw):
        with self._conn() as con:
            con.execute("""
                INSERT INTO emergency_logs
                (timestamp,customer_id,patient_tag,action,component,deposit,
                 trans_id,time_delta_mins,used_claim,seal_status,
                 audit_required,refund,admin_noted)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0)
            """, (
                datetime.now().isoformat(),
                kw.get("customer_id",""),
                kw.get("patient_tag",""),
                kw.get("action",""),
                kw.get("component",""),
                kw.get("deposit",      0.0),
                kw.get("trans_id",     ""),
                kw.get("time_delta_mins", 0.0),
                kw.get("used_claim",   ""),
                kw.get("seal_status",  ""),
                1 if kw.get("audit_required") else 0,
                kw.get("refund",       0.0),
            ))
        self.log_audit(
            kw.get("customer_id","SYSTEM"),
            f"HW_{kw.get('action','EVENT')}",
            "emergency_logs",
            kw.get("customer_id"),
            f"{kw.get('component','')} | deposit=₵{kw.get('deposit',0.0):.2f}"
        )

    def get_emergency_logs(self) -> list:
        with self._conn() as con:
            return [dict(r) for r in con.execute(
                "SELECT * FROM emergency_logs ORDER BY timestamp"
            )]

    def get_pending_audits(self) -> list:
        with self._conn() as con:
            return [dict(r) for r in con.execute(
                "SELECT * FROM emergency_logs "
                "WHERE audit_required=1 AND admin_noted=0"
            )]

    def mark_audit_noted(self, log_id: int):
        with self._conn() as con:
            con.execute(
                "UPDATE emergency_logs SET admin_noted=1 WHERE id=?", (log_id,)
            )

    # ── Analytics ──────────────────────────────────────────────────────────────
    def total_med_revenue(self) -> float:
        with self._conn() as con:
            row = con.execute(
                "SELECT SUM(total) AS s FROM transactions"
            ).fetchone()
            return row["s"] or 0.0

    def total_upgrade_revenue(self) -> float:
        with self._conn() as con:
            row = con.execute(
                "SELECT SUM(ABS(amount)) AS s FROM wallet_history "
                "WHERE type='upgrade' AND amount<0"
            ).fetchone()
            return row["s"] or 0.0

    def total_sales_qty(self) -> int:
        with self._conn() as con:
            row = con.execute(
                "SELECT SUM(qty) AS s FROM transaction_items"
            ).fetchone()
            return row["s"] or 0

    def total_stock_value(self) -> float:
        val  = sum(s["capsules_left"] * s["current_price"]
                   for s in self.get_all_shelves())
        val += sum(s["units_left"]    * s["current_price"]
                   for s in self.get_all_mega_shelves())
        return val

    # ── Prescriptions [B13] ───────────────────────────────────────────────────
    def create_prescription(self, customer_id: str, drug_name: str,
                             qty: int, dosage: str = "", consult_id: str = "",
                             hours_valid: int = 24) -> str:
        rx_id = secure_ref("RX")
        with self._conn() as con:
            con.execute(
                "INSERT INTO prescriptions (prescription_id,customer_id,drug_name,"
                "quantity_authorized,dosage_instructions,issued_at,expires_at,status,consult_id)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (rx_id, customer_id, drug_name, qty, dosage,
                 datetime.now().isoformat(),
                 (datetime.now() + timedelta(hours=hours_valid)).isoformat(),
                 "active", consult_id))
        self.log_audit(customer_id, "PRESCRIPTION_ISSUED", "prescriptions", rx_id,
                       f"{drug_name} x{qty}")
        return rx_id

    def get_active_prescriptions(self, customer_id: str) -> list:
        now = datetime.now().isoformat()
        with self._conn() as con:
            return [dict(r) for r in con.execute(
                "SELECT * FROM prescriptions WHERE customer_id=? AND status='active' AND expires_at>?",
                (customer_id, now))]

    def expire_prescriptions(self):
        now = datetime.now().isoformat()
        with self._conn() as con:
            con.execute(
                "UPDATE prescriptions SET status='expired'"
                " WHERE expires_at<? AND status='active'", (now,))

    # ── CLTS Session Log [B14] ────────────────────────────────────────────────
    def log_clts_session(self, customer_id: str | None, detected_gender: str,
                          temperature: float, face_matched: bool,
                          mood_estimate: str = "neutral") -> int:
        now  = datetime.now()
        hour = now.hour
        tod  = ("morning"   if 5  <= hour < 12 else
                "afternoon" if 12 <= hour < 17 else
                "evening"   if 17 <= hour < 21 else "night")
        dow  = now.strftime("%A")
        with self._conn() as con:
            cur = con.execute(
                "INSERT INTO clts_session_log (customer_id,session_at,detected_gender,"
                "temperature,mood_estimate,face_matched,led_to_purchase,time_of_day,day_of_week)"
                " VALUES (?,?,?,?,?,?,0,?,?)",
                (customer_id, now.isoformat(), detected_gender, temperature,
                 mood_estimate, 1 if face_matched else 0, tod, dow))
            return cur.lastrowid

    def mark_session_purchased(self, session_id: int):
        with self._conn() as con:
            con.execute("UPDATE clts_session_log SET led_to_purchase=1 WHERE session_id=?",
                        (session_id,))

    def get_clts_stats(self) -> dict:
        with self._conn() as con:
            total   = con.execute("SELECT COUNT(*) FROM clts_session_log").fetchone()[0]
            matched = con.execute(
                "SELECT COUNT(*) FROM clts_session_log WHERE face_matched=1").fetchone()[0]
            bought  = con.execute(
                "SELECT COUNT(*) FROM clts_session_log WHERE led_to_purchase=1").fetchone()[0]
            gender  = [dict(r) for r in con.execute(
                "SELECT detected_gender, COUNT(*) as cnt FROM clts_session_log"
                " GROUP BY detected_gender ORDER BY cnt DESC")]
            peaks   = [dict(r) for r in con.execute(
                "SELECT time_of_day, COUNT(*) as cnt FROM clts_session_log"
                " GROUP BY time_of_day ORDER BY cnt DESC")]
        return {"total_sessions": total, "face_matched": matched,
                "led_to_purchase": bought, "gender_breakdown": gender,
                "peak_hours": peaks}

    # ── OTA / System Updates [B15] ────────────────────────────────────────────
    def record_update(self, from_v: int, to_v: int, status: str,
                       checksum: str = "", notes: str = "") -> str:
        uid = secure_ref("UPD")
        with self._conn() as con:
            con.execute(
                "INSERT INTO system_updates (update_id,unit_id,from_version,to_version,"
                "initiated_at,status,checksum,notes) VALUES (?,?,?,?,?,?,?,?)",
                (uid, UNIT_ID, from_v, to_v, datetime.now().isoformat(),
                 status, checksum, notes))
        self.log_audit("SYSTEM", f"OTA_{status.upper()}", "system_updates", uid,
                       f"v{from_v}→v{to_v}")
        return uid

    def complete_update(self, update_id: str, status: str):
        with self._conn() as con:
            con.execute(
                "UPDATE system_updates SET status=?,completed_at=? WHERE update_id=?",
                (status, datetime.now().isoformat(), update_id))

    def get_update_history(self) -> list:
        with self._conn() as con:
            return [dict(r) for r in con.execute(
                "SELECT * FROM system_updates ORDER BY initiated_at DESC")]

    # ── Notification Dispatch [B15] ───────────────────────────────────────────
    def log_dispatch(self, customer_id: str, channel: str,
                      subject: str, message: str,
                      status: str = "simulated",
                      error: str = "") -> int:
        with self._conn() as con:
            cur = con.execute(
                "INSERT INTO notification_dispatch (customer_id,channel,subject,message,"
                "status,attempted_at,error_detail) VALUES (?,?,?,?,?,?,?)",
                (customer_id, channel, subject, message,
                 status, datetime.now().isoformat(), error))
            return cur.lastrowid

    def get_dispatch_log(self, limit: int = 50) -> list:
        with self._conn() as con:
            return [dict(r) for r in con.execute(
                "SELECT * FROM notification_dispatch ORDER BY attempted_at DESC LIMIT ?",
                (limit,))]

    # ── Scheduler Log [B15] ───────────────────────────────────────────────────
    def start_task_log(self, task_name: str) -> int:
        with self._conn() as con:
            cur = con.execute(
                "INSERT INTO scheduler_log (task_name,started_at,status) VALUES (?,?,?)",
                (task_name, datetime.now().isoformat(), "running"))
            return cur.lastrowid

    def complete_task_log(self, log_id: int, summary: str = "",
                           error: str = "", duration_ms: int = 0):
        status = "failed" if error else "completed"
        with self._conn() as con:
            con.execute(
                "UPDATE scheduler_log SET status=?,completed_at=?,duration_ms=?,"
                "result_summary=?,error_detail=? WHERE id=?",
                (status, datetime.now().isoformat(), duration_ms,
                 summary, error, log_id))

    def get_scheduler_log(self, limit: int = 30) -> list:
        with self._conn() as con:
            return [dict(r) for r in con.execute(
                "SELECT * FROM scheduler_log ORDER BY started_at DESC LIMIT ?", (limit,))]

    # ── Unit Registry [B16] ───────────────────────────────────────────────────
    def get_all_units(self) -> list:
        with self._conn() as con:
            return [dict(r) for r in con.execute(
                "SELECT * FROM unit_registry ORDER BY installed_at")]

    def ping_unit(self, unit_id: str = None):
        uid = unit_id or UNIT_ID
        with self._conn() as con:
            con.execute("UPDATE unit_registry SET last_seen=? WHERE unit_id=?",
                        (datetime.now().isoformat(), uid))

    def get_unit_analytics(self, unit_id: str = None) -> dict:
        uid = unit_id or UNIT_ID
        with self._conn() as con:
            rev = con.execute(
                "SELECT SUM(total) FROM transactions WHERE unit_id=?",
                (uid,)).fetchone()[0] or 0.0
            orders = con.execute(
                "SELECT COUNT(*) FROM restock_orders WHERE unit_id=? AND status NOT IN ('received','cancelled')",
                (uid,)).fetchone()[0]
        return {"unit_id": uid, "revenue": rev, "pending_orders": orders}

    # ── API Token Management [B16] ────────────────────────────────────────────
    def create_api_token(self, actor_id: str, role: str,
                          description: str = "") -> dict:
        if role not in API_ROLES:
            raise ValueError(f"Invalid role. Use: {API_ROLES}")
        token_id  = secure_ref("TOK")
        issued_at = datetime.now()
        expires   = issued_at + timedelta(hours=JWT_EXPIRY_HOURS)
        with self._conn() as con:
            con.execute(
                "INSERT INTO api_tokens (token_id,actor_id,role,issued_at,"
                "expires_at,revoked,description) VALUES (?,?,?,?,?,0,?)",
                (token_id, actor_id, role, issued_at.isoformat(),
                 expires.isoformat(), description))
        self.log_audit(actor_id, "ADMIN_ACTION", "api_tokens", token_id,
                       f"Token issued: role={role}")
        return {"token_id": token_id, "actor_id": actor_id,
                "role": role, "expires_at": expires.isoformat()}

    def revoke_api_token(self, token_id: str):
        with self._conn() as con:
            con.execute("UPDATE api_tokens SET revoked=1 WHERE token_id=?", (token_id,))

    def get_api_tokens(self, actor_id: str = None) -> list:
        with self._conn() as con:
            if actor_id:
                return [dict(r) for r in con.execute(
                    "SELECT * FROM api_tokens WHERE actor_id=? ORDER BY issued_at DESC",
                    (actor_id,))]
            return [dict(r) for r in con.execute(
                "SELECT * FROM api_tokens ORDER BY issued_at DESC")]

    # ── Device Tokens [B17] ───────────────────────────────────────────────────
    def register_device_token(self, customer_id: str,
                               token: str, platform: str) -> bool:
        try:
            with self._conn() as con:
                con.execute(
                    "INSERT OR REPLACE INTO device_tokens "
                    "(customer_id,token,platform,registered_at,last_seen,active) "
                    "VALUES (?,?,?,?,?,1)",
                    (customer_id, token, platform,
                     datetime.now().isoformat(), datetime.now().isoformat()))
            return True
        except Exception:
            return False

    def get_device_tokens(self, customer_id: str) -> list:
        with self._conn() as con:
            return [dict(r) for r in con.execute(
                "SELECT * FROM device_tokens WHERE customer_id=? AND active=1",
                (customer_id,))]

    # ── MoMo Webhooks [B17] ───────────────────────────────────────────────────
    def record_momo_webhook(self, reference: str, customer_id: str,
                             amount: float, status: str,
                             payload: str = "") -> int:
        with self._conn() as con:
            cur = con.execute(
                "INSERT OR IGNORE INTO momo_webhooks "
                "(reference,customer_id,amount,status,payload,received_at) "
                "VALUES (?,?,?,?,?,?)",
                (reference, customer_id, amount, status,
                 payload, datetime.now().isoformat()))
            return cur.lastrowid

    def process_momo_webhook(self, reference: str) -> bool:
        with self._conn() as con:
            row = con.execute(
                "SELECT * FROM momo_webhooks WHERE reference=?",
                (reference,)).fetchone()
            if not row or dict(row)["status"] != "pending":
                return False
            con.execute(
                "UPDATE momo_webhooks SET status='success',processed_at=? "
                "WHERE reference=?",
                (datetime.now().isoformat(), reference))
        return True

    # ── Thermal Log [B18] ─────────────────────────────────────────────────────
    def log_thermal(self, ambient: float, obj_temp: float,
                     customer_id: str = None,
                     mode: str = "simulated") -> int:
        flagged    = 1 if obj_temp >= 38.0 else 0
        flag_reason = "High temperature detected" if flagged else ""
        with self._conn() as con:
            cur = con.execute(
                "INSERT INTO thermal_log "
                "(customer_id,ambient_temp,object_temp,sensor_mode,"
                "recorded_at,flagged,flag_reason) VALUES (?,?,?,?,?,?,?)",
                (customer_id, ambient, obj_temp, mode,
                 datetime.now().isoformat(), flagged, flag_reason))
            return cur.lastrowid

    def get_thermal_stats(self, customer_id: str = None) -> dict:
        with self._conn() as con:
            if customer_id:
                row = con.execute(
                    "SELECT COUNT(*) AS total, AVG(object_temp) AS avg_temp, "
                    "SUM(flagged) AS flagged_count, MAX(recorded_at) AS last_reading "
                    "FROM thermal_log WHERE customer_id=?",
                    (customer_id,)).fetchone()
            else:
                row = con.execute(
                    "SELECT COUNT(*) AS total, AVG(object_temp) AS avg_temp, "
                    "SUM(flagged) AS flagged_count FROM thermal_log").fetchone()
        return dict(row) if row else {}

    def get_all_face_signatures(self) -> dict:
        """Return {customer_id: [float, ...]} for all enrolled customers."""
        with self._conn() as con:
            rows = con.execute(
                "SELECT customer_id, position, value "
                "FROM customer_face_signatures "
                "ORDER BY customer_id, position").fetchall()
        result: dict = {}
        for r in rows:
            cid = r["customer_id"]
            if cid not in result:
                result[cid] = []
            result[cid].append(r["value"])
        return result

    def get_customer_by_card_uid(self, card_uid: str) -> dict | None:
        """Look up a customer by their physical card UID."""
        with self._conn() as con:
            row = con.execute(
                "SELECT * FROM customers WHERE card_uid=?",
                (card_uid,)).fetchone()
            if row:
                return dict(row)
            # Fallback: card_uid used as customer_id prefix
            row2 = con.execute(
                "SELECT * FROM customers WHERE customer_id=?",
                (card_uid,)).fetchone()
            return dict(row2) if row2 else None

    # ── Dispense Log [B18] ────────────────────────────────────────────────────
    def log_dispense(self, shelf_num: int, drug_name: str,
                      gpio_pin: int, customer_id: str = None,
                      transaction_id: str = None,
                      mode: str = "simulated") -> int:
        with self._conn() as con:
            cur = con.execute(
                "INSERT INTO dispense_log "
                "(shelf_num,drug_name,gpio_pin,triggered_at,mode,"
                "status,customer_id,transaction_id) VALUES (?,?,?,?,?,?,?,?)",
                (shelf_num, drug_name, gpio_pin,
                 datetime.now().isoformat(), mode,
                 "pending", customer_id, transaction_id))
            return cur.lastrowid

    def confirm_dispense(self, log_id: int, status: str = "confirmed"):
        with self._conn() as con:
            con.execute(
                "UPDATE dispense_log SET status=?,confirmed_at=? WHERE id=?",
                (status, datetime.now().isoformat(), log_id))

    def get_dispense_history(self, limit: int = 50) -> list:
        with self._conn() as con:
            return [dict(r) for r in con.execute(
                "SELECT * FROM dispense_log ORDER BY triggered_at DESC LIMIT ?",
                (limit,))]

    # ── Generated Reports [B19] ───────────────────────────────────────────────
    def record_report(self, report_type: str, title: str,
                       file_path: str, period_start: str = None,
                       period_end: str = None,
                       generated_by: str = "ADMIN",
                       unit_id: str = "ALL") -> int:
        size = os.path.getsize(file_path) if os.path.exists(file_path) else 0
        with self._conn() as con:
            cur = con.execute(
                "INSERT INTO generated_reports "
                "(report_type,title,file_path,unit_id,period_start,"
                "period_end,generated_by,generated_at,file_size_bytes) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (report_type, title, file_path, unit_id,
                 period_start, period_end, generated_by,
                 datetime.now().isoformat(), size))
            return cur.lastrowid

    def get_reports(self, report_type: str = None) -> list:
        with self._conn() as con:
            if report_type:
                return [dict(r) for r in con.execute(
                    "SELECT * FROM generated_reports WHERE report_type=? "
                    "ORDER BY generated_at DESC", (report_type,))]
            return [dict(r) for r in con.execute(
                "SELECT * FROM generated_reports ORDER BY generated_at DESC")]

    # ── Build 20: Cupsule methods ──────────────────────────────────────────────
    def issue_cupsule(self, customer_id: str, transaction_id: str,
                      shelf_num: int, drug_name: str,
                      drug_class: str = "OTC", unit_id: str = None) -> str:
        """Generate and record a Cupsule issuance. Returns the cupsule_id."""
        ts  = datetime.now()
        cid = f"CUP-{ts.strftime('%Y%m%d')}-{secure_id(8).upper()}"
        with self._conn() as con:
            con.execute("""
                INSERT INTO cupsule_issued (
                    cupsule_id, customer_id, transaction_id,
                    shelf_num, drug_name, drug_class, issued_at, unit_id
                ) VALUES (?,?,?,?,?,?,?,?)
            """, (cid, customer_id, transaction_id, shelf_num,
                  drug_name, drug_class, ts.isoformat(),
                  unit_id or UNIT_ID))
        return cid

    def mark_cupsule_returned(self, cupsule_id: str, points: int = 0) -> bool:
        with self._conn() as con:
            rows = con.execute(
                "UPDATE cupsule_issued SET returned=1, returned_at=?, "
                "points_awarded=? WHERE cupsule_id=? AND returned=0",
                (datetime.now().isoformat(), points, cupsule_id)).rowcount
        return rows > 0

    def get_cupsule(self, cupsule_id: str) -> dict | None:
        with self._conn() as con:
            row = con.execute(
                "SELECT * FROM cupsule_issued WHERE cupsule_id=?",
                (cupsule_id,)).fetchone()
            return dict(row) if row else None

    def get_customer_cupsules(self, customer_id: str) -> list:
        with self._conn() as con:
            return [dict(r) for r in con.execute(
                "SELECT * FROM cupsule_issued WHERE customer_id=? "
                "ORDER BY issued_at DESC", (customer_id,))]

    # ── Build 20: Sync token methods ──────────────────────────────────────────
    def generate_sync_token(self, customer_id: str) -> str:
        """Generate a one-time app sync token for a customer. Stored in customers row."""
        token   = secrets.token_urlsafe(32)
        expires = (datetime.now() + timedelta(hours=SYNC_TOKEN_TTL_HOURS)).isoformat()
        with self._conn() as con:
            con.execute(
                "UPDATE customers SET sync_token=?, sync_token_expires=? "
                "WHERE customer_id=?",
                (token, expires, customer_id))
        return token

    def consume_sync_token(self, token: str) -> dict | None:
        """Validate and consume a sync token. Returns customer or None."""
        with self._conn() as con:
            row = con.execute(
                "SELECT * FROM customers WHERE sync_token=?", (token,)).fetchone()
            if not row:
                return None
            c = dict(row)
            # Check expiry
            try:
                if datetime.fromisoformat(c["sync_token_expires"]) < datetime.now():
                    return None
            except (TypeError, ValueError):
                return None
            # Consume — clear token
            con.execute(
                "UPDATE customers SET sync_token=NULL, sync_token_expires=NULL "
                "WHERE customer_id=?", (c["customer_id"],))
            return c

    # ── Build 20: Password reset token methods ─────────────────────────────────
    def generate_reset_token(self, customer_id: str, method: str) -> str:
        token   = secrets.token_urlsafe(24)
        issued  = datetime.now()
        expires = (issued + timedelta(minutes=RESET_TOKEN_TTL_MINS)).isoformat()
        with self._conn() as con:
            con.execute("""
                INSERT INTO password_reset_log
                    (customer_id, reset_token, method, issued_at, expires_at)
                VALUES (?,?,?,?,?)
            """, (customer_id, token, method, issued.isoformat(), expires))
        return token

    def consume_reset_token(self, token: str) -> dict | None:
        """Validate, consume, and return the associated customer, or None."""
        with self._conn() as con:
            row = con.execute(
                "SELECT * FROM password_reset_log "
                "WHERE reset_token=? AND used=0", (token,)).fetchone()
            if not row:
                return None
            r = dict(row)
            try:
                if datetime.fromisoformat(r["expires_at"]) < datetime.now():
                    return None
            except (TypeError, ValueError):
                return None
            con.execute(
                "UPDATE password_reset_log SET used=1, used_at=? "
                "WHERE reset_token=?",
                (datetime.now().isoformat(), token))
            return self.get_customer(r["customer_id"])

    # ── Build 20: Service Bus registry methods ────────────────────────────────
    def register_service(self, service_name: str, contract_id: str,
                         version: str, capabilities: list,
                         host_unit_id: str = None):
        with self._conn() as con:
            con.execute("""
                INSERT INTO service_bus_registry
                    (service_name, contract_id, version, registered_at,
                     last_heartbeat, status, capabilities, host_unit_id)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(service_name) DO UPDATE SET
                    version=excluded.version,
                    last_heartbeat=excluded.last_heartbeat,
                    status='active',
                    capabilities=excluded.capabilities
            """, (service_name, contract_id, version,
                  datetime.now().isoformat(), datetime.now().isoformat(),
                  "active", str(capabilities), host_unit_id or UNIT_ID))

    def get_registered_services(self) -> list:
        with self._conn() as con:
            return [dict(r) for r in con.execute(
                "SELECT * FROM service_bus_registry ORDER BY service_name")]

    # ── Build 23: CUPSCAN DB Methods ──────────────────────────────────────────

    def cupscan_register_kiosk(self, kiosk_id: str, site_id: str,
                                site_name: str, firmware: str) -> bool:
        now = datetime.now().isoformat()
        with self._conn() as con:
            existing = con.execute(
                "SELECT kiosk_id FROM cupscan_kiosks WHERE kiosk_id=?",
                (kiosk_id,)).fetchone()
            if existing:
                con.execute(
                    "UPDATE cupscan_kiosks SET firmware=?,last_heartbeat=?,status='ACTIVE' WHERE kiosk_id=?",
                    (firmware, now, kiosk_id))
            else:
                con.execute(
                    "INSERT INTO cupscan_kiosks (kiosk_id,site_id,site_name,registered_at,last_heartbeat,firmware) VALUES (?,?,?,?,?,?)",
                    (kiosk_id, site_id, site_name, now, now, firmware))
        self.log_audit("CUPSCAN", "ADMIN_ACTION",
                       detail=f"Kiosk {'re-registered' if existing else 'registered'}: {kiosk_id}")
        return True

    def cupscan_heartbeat(self, kiosk_id: str, payload: dict):
        now = datetime.now().isoformat()
        with self._conn() as con:
            con.execute("""
                UPDATE cupscan_kiosks SET
                    last_heartbeat  = ?,  status          = 'ACTIVE',
                    bin_intact_pct  = ?,  bin_partial_pct = ?,
                    bin_anomaly_pct = ?,  bin_contam_pct  = ?,
                    queue_depth     = ?,  returns_today   = ?
                WHERE kiosk_id = ?
            """, (now,
                  payload.get("bin_intact_pct",  0.0),
                  payload.get("bin_partial_pct", 0.0),
                  payload.get("bin_anomaly_pct", 0.0),
                  payload.get("bin_contam_pct",  0.0),
                  payload.get("queue_depth",     0),
                  payload.get("returns_today",   0),
                  kiosk_id))

    def cupscan_get_customer_by_card(self, card_uid: str) -> dict | None:
        """Thin safe projection — returns only what CUPSCAN needs."""
        c = self.get_customer_by_card_uid(card_uid)
        if not c:
            return None
        return {
            "customer_id":    c["customer_id"],
            "name":           c["name"],
            "loyalty_points": c.get("loyalty_points", 0),
            "lifetime_points": c.get("lifetime_points", 0),
            "status":         c["status"],
            "wallet_tier":    c.get("wallet_tier", "G0"),
        }

    def cupscan_apply_points(self, customer_id: str, delta: int,
                              reason: str = "CUPSCAN_RETURN") -> dict:
        with self._conn() as con:
            row = con.execute(
                "SELECT loyalty_points, lifetime_points FROM customers WHERE customer_id=?",
                (customer_id,)).fetchone()
            if not row:
                return {"ok": False, "error": "customer_not_found"}
            new_bal  = max(0, row[0] + delta)
            new_life = row[1] + max(0, delta)
            con.execute(
                "UPDATE customers SET loyalty_points=?,lifetime_points=? WHERE customer_id=?",
                (new_bal, new_life, customer_id))
        self.log_audit(customer_id, "CUPSCAN_POINTS",
                       detail=f"delta={delta} reason={reason} new_bal={new_bal}")
        return {"ok": True, "new_balance": new_bal, "lifetime_points": new_life}

    def cupscan_get_card_updates(self, since: str) -> list:
        with self._conn() as con:
            rows = con.execute("""
                SELECT customer_id, name, loyalty_points, lifetime_points,
                       status, card_uid, wallet_tier
                FROM customers
                WHERE COALESCE(updated_at, created_at, '') > ?
                ORDER BY COALESCE(updated_at, created_at) DESC
                LIMIT 500
            """, (since,)).fetchall()
        return [dict(r) for r in rows]

    def cupscan_record_return(self, kiosk_id: str, ret: dict) -> int:
        with self._conn() as con:
            cur = con.execute("""
                INSERT INTO cupscan_returns
                  (kiosk_id,customer_id,card_uid,compartment,base_pts,bonus_pts,
                   total_pts,multiplier,is_bonus_window,streak_days,
                   co2_saved_g,water_saved_l,returned_at,synced_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (kiosk_id,
                  ret.get("customer_id"),
                  ret.get("card_uid"),
                  ret.get("compartment", "UNKNOWN"),
                  ret.get("base_pts",    0),
                  ret.get("bonus_pts",   0),
                  ret.get("total_pts",   0),
                  ret.get("multiplier",  1.0),
                  1 if ret.get("is_bonus_window") else 0,
                  ret.get("streak_days", 0),
                  ret.get("co2_saved_g", 0.0),
                  ret.get("water_saved_l", 0.0),
                  ret.get("returned_at", datetime.now().isoformat()),
                  datetime.now().isoformat()))
        return cur.lastrowid

    def cupscan_get_daily_count(self, customer_id: str,
                                 date_str: str | None = None) -> dict:
        today = date_str or datetime.now().strftime("%Y-%m-%d")
        with self._conn() as con:
            row = con.execute(
                "SELECT return_count, bonus_pts_given FROM cupscan_daily_counts WHERE customer_id=? AND date_str=?",
                (customer_id, today)).fetchone()
        if not row:
            return {"return_count": 0, "bonus_pts_given": 0, "date": today}
        return {"return_count": row[0], "bonus_pts_given": row[1], "date": today}

    def cupscan_increment_daily(self, customer_id: str,
                                 bonus_pts: int, date_str: str | None = None):
        today = date_str or datetime.now().strftime("%Y-%m-%d")
        with self._conn() as con:
            con.execute("""
                INSERT INTO cupscan_daily_counts (customer_id,date_str,return_count,bonus_pts_given)
                VALUES (?,?,1,?)
                ON CONFLICT(customer_id,date_str) DO UPDATE SET
                    return_count    = return_count    + 1,
                    bonus_pts_given = bonus_pts_given + excluded.bonus_pts_given
            """, (customer_id, today, bonus_pts))

    def cupscan_get_all_kiosks(self) -> list:
        with self._conn() as con:
            return [dict(r) for r in con.execute(
                "SELECT * FROM cupscan_kiosks ORDER BY registered_at DESC").fetchall()]

    def cupscan_platform_stats(self) -> dict:
        today = datetime.now().strftime("%Y-%m-%d")
        with self._conn() as con:
            total  = con.execute("SELECT COUNT(*) FROM cupscan_returns").fetchone()[0]
            today_r = con.execute(
                "SELECT COUNT(*) FROM cupscan_returns WHERE returned_at LIKE ?",
                (f"{today}%",)).fetchone()[0]
            pts_today = con.execute(
                "SELECT COALESCE(SUM(total_pts),0) FROM cupscan_returns WHERE returned_at LIKE ?",
                (f"{today}%",)).fetchone()[0]
            active_k = con.execute(
                "SELECT COUNT(*) FROM cupscan_kiosks WHERE status='ACTIVE'").fetchone()[0]
            co2_total = con.execute(
                "SELECT COALESCE(SUM(co2_saved_g),0) FROM cupscan_returns").fetchone()[0]
        return {
            "total_returns":  total,
            "returns_today":  today_r,
            "pts_today":      pts_today,
            "active_kiosks":  active_k,
            "co2_saved_kg":   round(co2_total / 1000, 3),
            "ghs_value_today": round(pts_today * GHS_PER_POINT, 2),
        }

    # ── wipe_all ───────────────────────────────────────────────────────────────
    def wipe_all(self):
        with self._conn() as con:
            for tbl in [
                "transaction_items", "transactions",
                "wallet_history",    "notifications",
                "customer_cart",     "customer_face_signatures",
                "customer_health_trends",
                "feedback",          "return_photos",
                "returned_items",    "active_hardware_codes",
                "emergency_logs",    "customers",
                "prescriptions",     "support_tickets",
                "teleconsult_records","teleconsult_queue",
                "clts_session_log",  "nyansa_insights",
                "promotions",        "restock_orders",
                "price_history",     "pdms_audit_log",
                "system_updates",    "notification_dispatch",
                "scheduler_log",     "api_tokens",
                "device_tokens",     "momo_webhooks",
                "thermal_log",       "dispense_log",
                "generated_reports",
            ]:
                con.execute(f"DELETE FROM {tbl}")
            con.execute("""
                UPDATE hardware_status SET
                    maintenance_revenue=0.0, card_sales_revenue=0.0,
                    aid_box_usage=0,         cpr_kit_usage=0,
                    aid_box_status='Docked', cpr_kit_status='Docked'
                WHERE id=1
            """)


# ═══════════════════════════════════════════════════════════════════════════════
# SERVICE LAYER — Build 13
# Pure business logic. No print(), no input(), no time.sleep().
# ═══════════════════════════════════════════════════════════════════════════════
