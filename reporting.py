"""
AID PLUS+ — Reporting Service
==============================
PDF and CSV report generation for business intelligence.
Uses ReportLab when available; falls back to CSV-only mode.
Reports: revenue, drug trends, NHIS, stock turnover, demographics, multi-unit.
"""
from __future__ import annotations
import os, csv, random
from datetime import datetime, timedelta

from aidplus.config import *
from aidplus.ui import print_header, ui_info, ui_qr, speak
from aidplus.db import DatabaseManager

class ReportingService:
    """
    [B19-A] PDF report generation via ReportLab.
    All reports are saved to REPORTS_DIR and recorded in generated_reports table.
    Falls back to CSV if ReportLab not available.
    """

    def __init__(self, db: DatabaseManager):
        self.db = db
        os.makedirs(REPORTS_DIR, exist_ok=True)

    def _pdf_path(self, name: str) -> str:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        return os.path.join(REPORTS_DIR, f"{name}_{ts}.pdf")

    def _csv_path(self, name: str) -> str:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        return os.path.join(REPORTS_DIR, f"{name}_{ts}.csv")

    def _brand_header(self, elements, styles, title: str, subtitle: str = ""):
        """Add AID SYSTEM branded header to a ReportLab document."""
        header_style = ParagraphStyle(
            "AIDHeader", parent=styles["Heading1"],
            textColor=rl_colors.HexColor("#00A8B5"),
            fontSize=22, spaceAfter=4)
        sub_style = ParagraphStyle(
            "AIDSub", parent=styles["Normal"],
            textColor=rl_colors.HexColor("#555555"),
            fontSize=10, spaceAfter=2)
        ts_style = ParagraphStyle(
            "AIDTS", parent=styles["Normal"],
            textColor=rl_colors.HexColor("#888888"),
            fontSize=8, spaceAfter=12)

        elements.append(Paragraph("⚕ AID SYSTEM — Nyansa Intelligence", header_style))
        elements.append(Paragraph(title, sub_style))
        elements.append(Paragraph(
            f"Generated: {datetime.now().strftime('%d %B %Y, %H:%M')}  |  "
            f"Unit: {UNIT_ID}  |  Build v{SCHEMA_VERSION}", ts_style))
        elements.append(HRFlowable(width="100%",
                                    color=rl_colors.HexColor("#00A8B5"),
                                    thickness=1.5))
        elements.append(Spacer(1, 0.4*cm))

    def _table_style(self) -> TableStyle:
        return TableStyle([
            ("BACKGROUND",  (0, 0), (-1, 0), rl_colors.HexColor("#00A8B5")),
            ("TEXTCOLOR",   (0, 0), (-1, 0), rl_colors.white),
            ("FONTNAME",    (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE",    (0, 0), (-1, 0), 9),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1),
             [rl_colors.HexColor("#F7FAFB"), rl_colors.white]),
            ("FONTSIZE",    (0, 1), (-1, -1), 8),
            ("GRID",        (0, 0), (-1, -1), 0.4, rl_colors.HexColor("#DDDDDD")),
            ("TOPPADDING",  (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING",(0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ])

    # ── Report 1: Monthly Revenue ─────────────────────────────────────────────
    def report_monthly_revenue(self, year: int = None,
                                month: int = None,
                                generated_by: str = "ADMIN") -> str:
        now   = datetime.now()
        year  = year  or now.year
        month = month or now.month
        start = f"{year}-{month:02d}-01"
        import calendar
        last_day = calendar.monthrange(year, month)[1]
        end   = f"{year}-{month:02d}-{last_day}"

        with self.db._conn() as con:
            txns = [dict(r) for r in con.execute(
                "SELECT t.*, c.name AS customer_name, c.nhis_active "
                "FROM transactions t "
                "LEFT JOIN customers c ON t.customer_id=c.customer_id "
                "WHERE t.timestamp BETWEEN ? AND ? "
                "ORDER BY t.timestamp",
                (start, end + "T23:59:59"))]
            items = [dict(r) for r in con.execute(
                "SELECT ti.drug_name, SUM(ti.quantity) AS total_qty, "
                "SUM(ti.subtotal) AS total_revenue "
                "FROM transaction_items ti "
                "JOIN transactions t ON ti.transaction_id=t.transaction_id "
                "WHERE t.timestamp BETWEEN ? AND ? "
                "GROUP BY ti.drug_name ORDER BY total_revenue DESC",
                (start, end + "T23:59:59"))]

        total_rev  = sum(t["total"] for t in txns)
        nhis_rev   = sum(t["total"] for t in txns if t.get("nhis_active"))
        cash_txns  = len(txns)
        month_name = datetime.strptime(f"{year}-{month:02d}-01", "%Y-%m-%d").strftime("%B %Y")

        if not HAS_REPORTLAB:
            return self._monthly_revenue_csv(
                month_name, txns, items, total_rev, nhis_rev, generated_by)

        path = self._pdf_path(f"revenue_{year}{month:02d}")
        doc  = SimpleDocTemplate(path, pagesize=A4,
                                  leftMargin=2*cm, rightMargin=2*cm,
                                  topMargin=2*cm,  bottomMargin=2*cm)
        styles   = getSampleStyleSheet()
        elements = []
        self._brand_header(elements, styles,
                            f"Monthly Revenue Report — {month_name}")

        # Summary stats
        stat_data = [
            ["Metric", "Value"],
            ["Total Revenue",    f"₵{total_rev:,.2f}"],
            ["NHIS Revenue",     f"₵{nhis_rev:,.2f}"],
            ["Cash/Wallet Rev.", f"₵{total_rev - nhis_rev:,.2f}"],
            ["Transactions",     str(cash_txns)],
            ["Avg. per Txn",     f"₵{total_rev/max(cash_txns,1):,.2f}"],
        ]
        t = Table(stat_data, colWidths=[8*cm, 8*cm])
        t.setStyle(self._table_style())
        elements += [Paragraph("Summary", styles["Heading2"]),
                     Spacer(1, 0.2*cm), t, Spacer(1, 0.5*cm)]

        # Top drugs table
        if items:
            drug_data = [["Drug", "Qty Sold", "Revenue", "% of Total"]]
            for it in items[:15]:
                pct = (it["total_revenue"] / max(total_rev, 0.01)) * 100
                drug_data.append([
                    it["drug_name"],
                    str(int(it["total_qty"])),
                    f"₵{it['total_revenue']:,.2f}",
                    f"{pct:.1f}%"
                ])
            dt = Table(drug_data, colWidths=[8*cm, 3*cm, 4*cm, 3*cm])
            dt.setStyle(self._table_style())
            elements += [Paragraph("Top Drugs by Revenue", styles["Heading2"]),
                         Spacer(1, 0.2*cm), dt, Spacer(1, 0.5*cm)]

        doc.build(elements)
        self.db.record_report("monthly_revenue",
                               f"Monthly Revenue — {month_name}",
                               path, start, end, generated_by)
        return path

    def _monthly_revenue_csv(self, month_name, txns, items,
                              total_rev, nhis_rev, generated_by) -> str:
        path = self._csv_path(f"revenue_{month_name.replace(' ','_')}")
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["AID SYSTEM — Monthly Revenue", month_name])
            w.writerow(["Total Revenue", f"₵{total_rev:,.2f}"])
            w.writerow(["NHIS Revenue",  f"₵{nhis_rev:,.2f}"])
            w.writerow(["Transactions",  len(txns)])
            w.writerow([])
            w.writerow(["Drug", "Qty Sold", "Revenue"])
            for it in items:
                w.writerow([it["drug_name"], it["total_qty"],
                            f"₵{it['total_revenue']:,.2f}"])
        self.db.record_report("monthly_revenue",
                               f"Monthly Revenue — {month_name}",
                               path, generated_by=generated_by)
        return path

    # ── Report 2: Drug Consumption Trends ─────────────────────────────────────
    def report_drug_consumption(self, days: int = 30,
                                 generated_by: str = "ADMIN") -> str:
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        with self.db._conn() as con:
            rows = [dict(r) for r in con.execute(
                "SELECT ti.drug_name, "
                "SUM(ti.quantity) AS total_qty, "
                "COUNT(DISTINCT t.transaction_id) AS txn_count, "
                "AVG(ti.quantity) AS avg_per_txn, "
                "SUM(ti.subtotal) AS total_revenue "
                "FROM transaction_items ti "
                "JOIN transactions t ON ti.transaction_id=t.transaction_id "
                "WHERE t.timestamp >= ? "
                "GROUP BY ti.drug_name ORDER BY total_qty DESC",
                (cutoff,))]

        if not HAS_REPORTLAB:
            path = self._csv_path(f"consumption_{days}d")
            with open(path, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["Drug", "Qty", "Txns", "Avg/Txn", "Revenue"])
                for r in rows:
                    w.writerow([r["drug_name"], r["total_qty"],
                                r["txn_count"], f"{r['avg_per_txn']:.1f}",
                                f"₵{r['total_revenue']:,.2f}"])
            self.db.record_report("consumption", f"Drug Consumption {days}d", path,
                                   generated_by=generated_by)
            return path

        path     = self._pdf_path(f"consumption_{days}d")
        doc      = SimpleDocTemplate(path, pagesize=A4,
                                      leftMargin=2*cm, rightMargin=2*cm,
                                      topMargin=2*cm,  bottomMargin=2*cm)
        styles   = getSampleStyleSheet()
        elements = []
        self._brand_header(elements, styles,
                            f"Drug Consumption Trends — Last {days} Days")

        tbl_data = [["Drug", "Qty Sold", "Transactions",
                     "Avg/Txn", "Revenue"]]
        for r in rows:
            tbl_data.append([
                r["drug_name"],
                str(int(r["total_qty"])),
                str(r["txn_count"]),
                f"{r['avg_per_txn']:.1f}",
                f"₵{r['total_revenue']:,.2f}",
            ])
        if not rows:
            tbl_data.append(["No sales data in period", "", "", "", ""])

        t = Table(tbl_data, colWidths=[7*cm, 3*cm, 3.5*cm, 2.5*cm, 4*cm])
        t.setStyle(self._table_style())
        elements += [t, Spacer(1, 0.5*cm)]
        doc.build(elements)
        self.db.record_report("consumption",
                               f"Drug Consumption — {days}d",
                               path, generated_by=generated_by)
        return path

    # ── Report 3: NHIS Utilisation ────────────────────────────────────────────
    def report_nhis_utilisation(self, year: int = None,
                                 month: int = None,
                                 generated_by: str = "ADMIN") -> str:
        now   = datetime.now()
        year  = year  or now.year
        month = month or now.month
        start = f"{year}-{month:02d}-01"
        import calendar
        end   = f"{year}-{month:02d}-{calendar.monthrange(year,month)[1]}"
        month_name = datetime.strptime(start, "%Y-%m-%d").strftime("%B %Y")

        with self.db._conn() as con:
            nhis_txns = [dict(r) for r in con.execute(
                "SELECT t.transaction_id, t.customer_id, c.name, "
                "t.total, t.timestamp, c.nhis_id "
                "FROM transactions t "
                "JOIN customers c ON t.customer_id=c.customer_id "
                "WHERE c.nhis_active=1 AND t.timestamp BETWEEN ? AND ? "
                "ORDER BY t.timestamp",
                (start, end + "T23:59:59"))]
            nhis_items = [dict(r) for r in con.execute(
                "SELECT ti.drug_name, SUM(ti.quantity) AS qty, "
                "SUM(ti.subtotal) AS revenue "
                "FROM transaction_items ti "
                "JOIN transactions t ON ti.transaction_id=t.transaction_id "
                "JOIN customers c ON t.customer_id=c.customer_id "
                "WHERE c.nhis_active=1 AND t.timestamp BETWEEN ? AND ? "
                "GROUP BY ti.drug_name ORDER BY qty DESC",
                (start, end + "T23:59:59"))]

        total_nhis_rev = sum(t["total"] for t in nhis_txns)

        if not HAS_REPORTLAB:
            path = self._csv_path(f"nhis_{year}{month:02d}")
            with open(path, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["AID SYSTEM NHIS Utilisation Report", month_name])
                w.writerow(["Total NHIS Transactions", len(nhis_txns)])
                w.writerow(["Total NHIS Revenue", f"₵{total_nhis_rev:,.2f}"])
                w.writerow([]); w.writerow(["NHIS ID", "Name", "Date", "Amount"])
                for t in nhis_txns:
                    w.writerow([t.get("nhis_id","—"), t["name"],
                                t["timestamp"][:10], f"₵{t['total']:,.2f}"])
            self.db.record_report("nhis", f"NHIS Utilisation {month_name}",
                                   path, start, end, generated_by)
            return path

        path     = self._pdf_path(f"nhis_{year}{month:02d}")
        doc      = SimpleDocTemplate(path, pagesize=A4,
                                      leftMargin=2*cm, rightMargin=2*cm,
                                      topMargin=2*cm,  bottomMargin=2*cm)
        styles   = getSampleStyleSheet()
        elements = []
        self._brand_header(elements, styles,
                            f"NHIS Utilisation Report — {month_name}",
                            "Ghana National Health Insurance Scheme")

        summary = [
            ["Metric", "Value"],
            ["NHIS Transactions",  str(len(nhis_txns))],
            ["Total NHIS Revenue", f"₵{total_nhis_rev:,.2f}"],
            ["Unique Patients",    str(len(set(t["customer_id"] for t in nhis_txns)))],
            ["Avg. Claim Value",   f"₵{total_nhis_rev/max(len(nhis_txns),1):,.2f}"],
        ]
        st = Table(summary, colWidths=[8*cm, 8*cm])
        st.setStyle(self._table_style())
        elements += [Paragraph("Summary", styles["Heading2"]),
                     Spacer(1, 0.2*cm), st, Spacer(1, 0.5*cm)]

        if nhis_txns:
            hdr = [["NHIS ID", "Patient Name", "Date", "Amount"]]
            rows = [[t.get("nhis_id","—"), t["name"],
                     t["timestamp"][:10], f"₵{t['total']:,.2f}"]
                    for t in nhis_txns[:50]]
            rt = Table(hdr + rows, colWidths=[4*cm, 6*cm, 4*cm, 4*cm])
            rt.setStyle(self._table_style())
            elements += [Paragraph("Transaction Detail", styles["Heading2"]),
                         Spacer(1, 0.2*cm), rt]

        doc.build(elements)
        self.db.record_report("nhis", f"NHIS Utilisation — {month_name}",
                               path, start, end, generated_by)
        return path

    # ── Report 4: Stock Turnover Analysis ─────────────────────────────────────
    def report_stock_turnover(self, generated_by: str = "ADMIN") -> str:
        shelves = self.db.get_all_shelves() + self.db.get_all_mega_shelves()
        cutoff30 = (datetime.now() - timedelta(days=30)).isoformat()
        with self.db._conn() as con:
            velocity = {r["drug_name"]: r["total_qty"]
                        for r in con.execute(
                "SELECT ti.drug_name, SUM(ti.quantity) AS total_qty "
                "FROM transaction_items ti "
                "JOIN transactions t ON ti.transaction_id=t.transaction_id "
                "WHERE t.timestamp >= ? GROUP BY ti.drug_name",
                (cutoff30,))}

        rows = []
        for s in shelves:
            stock = s.get("capsules_left", s.get("units_left", 0))
            cap   = s.get("capacity", s.get("mega_capacity", 1))
            sold  = velocity.get(s["name"], 0)
            daily = sold / 30
            dos   = round(stock / max(daily, 0.01))   # days of stock remaining
            rows.append({
                "name":    s["name"],
                "shelf":   s["shelf"],
                "stock":   stock,
                "capacity":cap,
                "sold_30d":sold,
                "daily_v": round(daily, 1),
                "dos":     dos,
            })
        rows.sort(key=lambda x: x["dos"])

        if not HAS_REPORTLAB:
            path = self._csv_path("stock_turnover")
            with open(path, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["Drug","Shelf","Stock","Capacity",
                            "Sold 30d","Daily Velocity","Days of Stock"])
                for r in rows:
                    w.writerow([r["name"], r["shelf"], r["stock"],
                                r["capacity"], r["sold_30d"],
                                r["daily_v"], r["dos"]])
            self.db.record_report("stock_turnover", "Stock Turnover Analysis",
                                   path, generated_by=generated_by)
            return path

        path     = self._pdf_path("stock_turnover")
        doc      = SimpleDocTemplate(path, pagesize=A4,
                                      leftMargin=2*cm, rightMargin=2*cm,
                                      topMargin=2*cm,  bottomMargin=2*cm)
        styles   = getSampleStyleSheet()
        elements = []
        self._brand_header(elements, styles, "Stock Turnover Analysis")

        hdr      = [["Drug", "Shelf", "Stock", "Cap.", "Sold 30d", "Daily", "Days Left"]]
        tbl_rows = []
        for r in rows:
            dos_color = rl_colors.HexColor(
                "#FF4444" if r["dos"] < 5 else
                "#FFA500" if r["dos"] < 14 else "#000000")
            tbl_rows.append([
                r["name"], str(r["shelf"]), str(r["stock"]),
                str(r["capacity"]), str(r["sold_30d"]),
                str(r["daily_v"]), str(r["dos"]) + " d",
            ])
        t = Table(hdr + tbl_rows,
                   colWidths=[6*cm, 2*cm, 2*cm, 2*cm, 3*cm, 2.5*cm, 3*cm])
        t.setStyle(self._table_style())
        elements += [t]
        doc.build(elements)
        self.db.record_report("stock_turnover", "Stock Turnover Analysis",
                               path, generated_by=generated_by)
        return path

    # ── Report 5: Customer Demographics (CLTS) ────────────────────────────────
    def report_clts_demographics(self, generated_by: str = "ADMIN") -> str:
        stats     = self.db.get_clts_stats()
        gender_bd = stats.get("gender_breakdown", [])
        peak_hrs  = stats.get("peak_hours", [])

        if not HAS_REPORTLAB:
            path = self._csv_path("clts_demographics")
            with open(path, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["AID SYSTEM CLTS Demographics"])
                w.writerow(["Total Sessions", stats.get("total_sessions", 0)])
                w.writerow(["Face Matched",   stats.get("face_matched", 0)])
                w.writerow(["Led to Purchase",stats.get("led_to_purchase", 0)])
                w.writerow([]); w.writerow(["Gender", "Count"])
                for g in gender_bd: w.writerow([g["detected_gender"], g["cnt"]])
                w.writerow([]); w.writerow(["Time of Day", "Sessions"])
                for h in peak_hrs: w.writerow([h["time_of_day"], h["cnt"]])
            self.db.record_report("demographics", "CLTS Demographics",
                                   path, generated_by=generated_by)
            return path

        path     = self._pdf_path("clts_demographics")
        doc      = SimpleDocTemplate(path, pagesize=A4,
                                      leftMargin=2*cm, rightMargin=2*cm,
                                      topMargin=2*cm,  bottomMargin=2*cm)
        styles   = getSampleStyleSheet()
        elements = []
        self._brand_header(elements, styles,
                            "Customer Demographics Report (CLTS Vision Data)")

        summary = [
            ["Metric", "Value"],
            ["Total Sessions",     str(stats.get("total_sessions", 0))],
            ["Face Matched",       str(stats.get("face_matched", 0))],
            ["Led to Purchase",    str(stats.get("led_to_purchase", 0))],
            ["Conversion Rate",    f"{stats.get('conversion_rate', 0):.1f}%"],
        ]
        st = Table(summary, colWidths=[8*cm, 8*cm])
        st.setStyle(self._table_style())
        elements += [Paragraph("Session Summary", styles["Heading2"]),
                     Spacer(1, 0.2*cm), st, Spacer(1, 0.4*cm)]

        if gender_bd:
            gd = [["Gender", "Sessions"]] + \
                 [[g["detected_gender"].title(), str(g["cnt"])] for g in gender_bd]
            gt = Table(gd, colWidths=[8*cm, 8*cm])
            gt.setStyle(self._table_style())
            elements += [Paragraph("Gender Breakdown", styles["Heading2"]),
                         Spacer(1, 0.2*cm), gt, Spacer(1, 0.4*cm)]

        if peak_hrs:
            pd_data = [["Time of Day", "Sessions"]] + \
                      [[h["time_of_day"].title(), str(h["cnt"])] for h in peak_hrs]
            pt = Table(pd_data, colWidths=[8*cm, 8*cm])
            pt.setStyle(self._table_style())
            elements += [Paragraph("Peak Activity Hours", styles["Heading2"]),
                         Spacer(1, 0.2*cm), pt]

        doc.build(elements)
        self.db.record_report("demographics", "CLTS Demographics",
                               path, generated_by=generated_by)
        return path

    # ── Report 6: Multi-Unit Comparison ───────────────────────────────────────
    def report_multi_unit(self, generated_by: str = "ADMIN") -> str:
        units = self.db.get_all_units()
        rows  = []
        for u in units:
            analytics = self.db.get_unit_analytics(u["unit_id"])
            rows.append({
                "unit_id":   u["unit_id"],
                "name":      u["unit_name"],
                "location":  u["location"],
                "status":    u["status"],
                "version":   u["current_version"],
                "revenue":   analytics["revenue"],
                "orders":    analytics["pending_orders"],
                "last_seen": (u.get("last_seen") or "—")[:16],
            })

        if not HAS_REPORTLAB:
            path = self._csv_path("multi_unit_comparison")
            with open(path, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["Unit ID","Name","Location","Status",
                            "Build","Revenue","Pending Orders","Last Seen"])
                for r in rows:
                    w.writerow([r["unit_id"], r["name"], r["location"],
                                r["status"], r["version"],
                                f"₵{r['revenue']:,.2f}",
                                r["orders"], r["last_seen"]])
            self.db.record_report("multi_unit", "Multi-Unit Comparison",
                                   path, generated_by=generated_by)
            return path

        path     = self._pdf_path("multi_unit_comparison")
        doc      = SimpleDocTemplate(path, pagesize=A4,
                                      leftMargin=2*cm, rightMargin=2*cm,
                                      topMargin=2*cm,  bottomMargin=2*cm)
        styles   = getSampleStyleSheet()
        elements = []
        self._brand_header(elements, styles, "Multi-Unit Network Comparison")

        hdr = [["Unit", "Location", "Status", "Build", "Revenue", "Orders", "Last Seen"]]
        tbl = hdr + [[
            r["name"], r["location"], r["status"].upper(),
            f"v{r['version']}", f"₵{r['revenue']:,.2f}",
            str(r["orders"]), r["last_seen"]
        ] for r in rows]
        t = Table(tbl, colWidths=[4*cm, 4*cm, 2.5*cm, 2*cm, 3*cm, 2*cm, 3*cm])
        t.setStyle(self._table_style())
        elements += [t]
        doc.build(elements)
        self.db.record_report("multi_unit", "Multi-Unit Comparison",
                               path, generated_by=generated_by)
        return path

    def get_all_reports(self) -> list:
        return self.db.get_reports()
# ═══════════════════════════════════════════════════════════════════════════════
# ─────────────────────────────────────────────────────────────────────────────
# UI LAYER  [GUI-READY]
# All terminal output functions below map 1:1 to touchscreen UI components.
# When the graphical interface is built, replace each function body with the
# corresponding touchscreen renderer. Business logic stays untouched.
#
#   print_header()   → TopBar component  (brand bar + screen title)
#   ui_menu()        → MenuGrid component (touch button grid)
#   ui_prompt()      → InputField component (on-screen keyboard)
#   ui_info()        → InfoCard component (status / confirmation card)
#   ui_qr()          → QRPanel component (QR code + instruction text)
#   speak()          → VoiceOut component (TTS + accessible audio)
# ─────────────────────────────────────────────────────────────────────────────

