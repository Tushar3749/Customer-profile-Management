"""
SQL Server module — dbo.testMaster order data.
All heavy queries are cached in-memory for 5 minutes.
"""

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime

from utils import normalize_phone

def _cfg():
    return {
        "host": os.getenv("SQLSERVER_HOST", ""),
        "db": os.getenv("SQLSERVER_DB", "test"),
        "user": os.getenv("SQLSERVER_USER", ""),
        "pwd": os.getenv("SQLSERVER_PASSWORD", ""),
    }

_REQUIRED_COLS = {
    "CreationDate", "ShippedAt", "DeliveredAt", "Invoice",
    "CustomerName", "CustomerPhoneNumber", "CustomerAddress",
    "ProductSKU", "ProductName", "ProductQty", "UnitPrice",
    "TotalPrice", "SalesAmount", "DeliveryFee", "PaymentsPaidAmount",
    "DueAmount", "Status", "SubStatus", "PaymentMethod",
    "OrderSource", "DeliveryPartner", "DeliveryPartnerStatus",
    "District", "SUB_DISTRICT", "ProductCode",
}

# Optional columns — included in SELECT only if they exist in the table
_OPTIONAL_COLS = [
    "CustomerTag", "ProductDiscount", "PriceAfterDiscount", "OrderDiscount",
    "CancelledFlaggedReason", "CancelledReturnedDamagedQty",
    "AdditionalNote", "InternalNotes",
]

_COLS_VALIDATED = False
_AVAILABLE_COLS: set = set()
_CACHE: dict = {}
_CACHE_TTL = 300  # 5 minutes


@contextmanager
def get_conn():
    try:
        import pyodbc
    except ImportError:
        raise RuntimeError(
            "pyodbc installed nai. 'pip install pyodbc' diye install koro."
        )

    cfg = _cfg()
    if not cfg["host"]:
        raise RuntimeError(
            "SQLSERVER_HOST .env e set kora nai. .env.example dekho."
        )

    conn_str = (
        "DRIVER={ODBC Driver 18 for SQL Server};"
        f"SERVER={cfg['host']};"
        f"DATABASE={cfg['db']};"
        f"UID={cfg['user']};"
        f"PWD={cfg['pwd']};"
        "TrustServerCertificate=yes;"
        "Connect Timeout=10"
    )
    try:
        conn = pyodbc.connect(conn_str)
    except Exception as e:
        msg = str(e)
        if "driver" in msg.lower() or "ODBC" in msg:
            raise RuntimeError(
                "ODBC Driver 18 for SQL Server pawa jay nai. "
                "https://learn.microsoft.com/en-us/sql/connect/odbc/download-odbc-driver-for-sql-server "
                "theke download koro."
            ) from e
        raise
    try:
        yield conn
    finally:
        conn.close()


def _validate_cols():
    global _COLS_VALIDATED, _AVAILABLE_COLS
    if _COLS_VALIDATED:
        return
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA='dbo' AND TABLE_NAME='testMaster'"
        )
        existing = {r[0] for r in cur.fetchall()}
    missing = _REQUIRED_COLS - existing
    if missing:
        raise RuntimeError(f"SQL Server table-e ei columns nai: {sorted(missing)}")
    _AVAILABLE_COLS = existing
    _COLS_VALIDATED = True


def _cached(key: str, fn):
    now = time.time()
    entry = _CACHE.get(key)
    if entry and now - entry["ts"] < _CACHE_TTL:
        return entry["data"]
    data = fn()
    _CACHE[key] = {"ts": now, "data": data}
    return data


def _fmt(v) -> str | None:
    return str(v) if v is not None else None


def _dt(v) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    s = str(v)[:19]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None


def _flt(v) -> float:
    return float(v) if v is not None else 0.0


def _itn(v) -> int:
    return int(v) if v is not None else 0


# ---------------------------------------------------------------------------
# Profile: per-customer full insight (single query + Python aggregation)
# ---------------------------------------------------------------------------

_FLAGGED_STATUSES = {"FLAGGED", "CANCELLED", "CANCELED", "Returned", "Return Request", "CANCEL"}
_DELIVERED_STATUSES = {"DELIVERED", "COMPLETED"}


def get_orders_for_phone(core_phone: str) -> dict:
    _validate_cols()

    # Build SELECT — required cols always, optional only if present in schema
    opt_in_table = [c for c in _OPTIONAL_COLS if c in _AVAILABLE_COLS]
    opt_str = (", " + ", ".join(opt_in_table)) if opt_in_table else ""

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT Invoice, CreationDate, ShippedAt, DeliveredAt,
                   CustomerName, CustomerAddress,
                   ProductSKU, ProductName, ProductQty, UnitPrice,
                   TotalPrice, SalesAmount, DeliveryFee, PaymentsPaidAmount, DueAmount,
                   Status, SubStatus, DeliveryPartner, DeliveryPartnerStatus,
                   PaymentMethod, OrderSource, District, SUB_DISTRICT, ProductCode{opt_str}
            FROM dbo.testMaster
            WHERE CustomerPhoneNumber LIKE ?
            ORDER BY CreationDate DESC
            """,
            (f"%{core_phone}",),
        )
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]

    if not rows:
        return {"found": False}

    last = rows[0]

    # ── 1. Volume & Value ──────────────────────────────────────────────────
    invoices        = {r["Invoice"] for r in rows if r["Invoice"]}
    total_orders    = len(invoices)
    total_items     = sum(_itn(r["ProductQty"]) for r in rows)
    total_spend     = sum(_flt(r["SalesAmount"]) for r in rows)
    total_discount  = sum(_flt(r.get("ProductDiscount")) + _flt(r.get("OrderDiscount")) for r in rows)
    total_due       = sum(_flt(r["DueAmount"]) for r in rows)
    total_paid      = sum(_flt(r["PaymentsPaidAmount"]) for r in rows)
    avg_basket      = round(total_spend / total_orders, 2) if total_orders else 0.0
    avg_items_ord   = round(total_items / total_orders, 1) if total_orders else 0.0

    # ── 2. Return / Cancellation ───────────────────────────────────────────
    flagged_rows         = [r for r in rows if (r["Status"] or "") in _FLAGGED_STATUSES]
    total_flagged        = sum(1 for r in rows if r["Status"] == "FLAGGED")
    flagged_or_returned  = len(flagged_rows)
    total_returned_qty   = sum(_itn(r.get("CancelledReturnedDamagedQty")) for r in rows)
    return_rate_by_order = round(flagged_or_returned * 100 / total_orders, 1) if total_orders else 0.0
    return_rate_by_qty   = round(total_returned_qty * 100 / total_items, 1) if total_items else 0.0

    return_reasons: dict = {}
    for r in rows:
        reason = (r.get("CancelledFlaggedReason") or "").strip()
        if reason:
            return_reasons[reason] = return_reasons.get(reason, 0) + 1

    substatus_map: dict = {}
    for r in rows:
        ss = (r.get("SubStatus") or "").strip()
        if ss:
            substatus_map[ss] = substatus_map.get(ss, 0) + 1

    # ── 3. Order Status ────────────────────────────────────────────────────
    status_map: dict = {}
    for r in rows:
        s = (r["Status"] or "Unknown").strip()
        status_map[s] = status_map.get(s, 0) + 1

    # ── 4. Delivery Performance ───────────────────────────────────────────
    delivery_days = []
    for r in rows:
        if r["Status"] in _DELIVERED_STATUSES:
            cd, dd = _dt(r["CreationDate"]), _dt(r["DeliveredAt"])
            if cd and dd:
                delta = (dd - cd).days
                if 0 <= delta <= 90:
                    delivery_days.append(delta)
    avg_delivery_days = round(sum(delivery_days) / len(delivery_days), 1) if delivery_days else None

    dp_map: dict = {}
    for r in rows:
        dp = (r.get("DeliveryPartner") or "Unknown").strip()
        if dp not in dp_map:
            dp_map[dp] = {"total": 0, "delivered": 0}
        dp_map[dp]["total"] += 1
        if (r["Status"] or "") in _DELIVERED_STATUSES:
            dp_map[dp]["delivered"] += 1
    dp_breakdown = sorted(
        [{"partner": k, "total": v["total"], "delivered": v["delivered"],
          "success_rate": round(v["delivered"] * 100 / v["total"], 1) if v["total"] else 0.0}
         for k, v in dp_map.items()],
        key=lambda x: -x["total"],
    )

    # ── 5. Payment Behavior ────────────────────────────────────────────────
    pm_map: dict = {}
    for r in rows:
        pm = (r.get("PaymentMethod") or "Unknown").strip()
        if pm not in pm_map:
            pm_map[pm] = {"count": 0, "due": 0.0}
        pm_map[pm]["count"] += 1
        pm_map[pm]["due"] += _flt(r["DueAmount"])
    pm_breakdown = sorted(
        [{"method": k, "count": v["count"], "due": round(v["due"], 2)} for k, v in pm_map.items()],
        key=lambda x: -x["count"],
    )
    partial_payment_count = sum(
        1 for r in rows
        if _flt(r["PaymentsPaidAmount"]) > 0 and _flt(r["PaymentsPaidAmount"]) < _flt(r["TotalPrice"])
    )

    # ── 6. Product Preferences ────────────────────────────────────────────
    prod_map: dict = {}
    for r in rows:
        p = (r.get("ProductName") or "Unknown").strip()
        if p not in prod_map:
            prod_map[p] = {"qty": 0, "revenue": 0.0, "times_ordered": 0}
        prod_map[p]["qty"]          += _itn(r["ProductQty"])
        prod_map[p]["revenue"]      += _flt(r["SalesAmount"])
        prod_map[p]["times_ordered"] += 1
    top_products = sorted(prod_map.items(), key=lambda x: -x[1]["qty"])[:10]
    top_products_list = [
        {"name": k, "qty": v["qty"], "revenue": round(v["revenue"], 2),
         "times_ordered": v["times_ordered"]}
        for k, v in top_products
    ]
    unique_products    = len(prod_map)
    repeat_products    = sum(1 for v in prod_map.values() if v["times_ordered"] > 1)
    repeat_product_rate = round(repeat_products * 100 / unique_products, 1) if unique_products else 0.0

    # ── 7. Channel & Segment ──────────────────────────────────────────────
    src_map: dict = {}
    for r in rows:
        src = (r.get("OrderSource") or "Unknown").strip()
        src_map[src] = src_map.get(src, 0) + 1
    source_breakdown = sorted(
        [{"source": k, "count": v} for k, v in src_map.items()], key=lambda x: -x["count"]
    )

    tag_history = []
    seen_tags: set = set()
    for r in reversed(rows):
        tag = (r.get("CustomerTag") or "").strip()
        if tag and tag not in seen_tags:
            seen_tags.add(tag)
            tag_history.append({"tag": tag, "date": _fmt(r["CreationDate"])})
    current_tag = (last.get("CustomerTag") or "").strip() or None

    district_set: set = set()
    for r in rows:
        d = (r.get("District") or "").strip()
        s = (r.get("SUB_DISTRICT") or "").strip()
        if d:
            district_set.add(f"{d}{(' / ' + s) if s else ''}")
    district_consistent = len(district_set) <= 1

    # ── 8. Last Order Detail ───────────────────────────────────────────────
    # rows are sorted DESC so first row's Invoice is the most recent
    most_recent_invoice = rows[0].get("Invoice") if rows else None
    last_inv_rows = [r for r in rows if r.get("Invoice") == most_recent_invoice] if most_recent_invoice else []
    lr0 = last_inv_rows[0] if last_inv_rows else {}

    last_order_products = [
        {
            "name": r.get("ProductName"),
            "qty": _itn(r.get("ProductQty")),
            "unit_price": _flt(r.get("UnitPrice")),
            "total_price": _flt(r.get("TotalPrice")),
            "sku": r.get("ProductSKU"),
        }
        for r in last_inv_rows
    ]
    # SalesAmount per row; DueAmount/PaymentsPaidAmount/DeliveryFee are order-level (take from first row)
    last_sales = sum(_flt(r.get("SalesAmount")) for r in last_inv_rows)

    last_order_detail = {
        "invoice":          lr0.get("Invoice"),
        "creation_date":    _fmt(lr0.get("CreationDate")),
        "status":           lr0.get("Status"),
        "sub_status":       lr0.get("SubStatus"),
        "products":         last_order_products,
        "sales_amount":     last_sales,
        "delivery_fee":     _flt(lr0.get("DeliveryFee")),
        "paid_amount":      _flt(lr0.get("PaymentsPaidAmount")),
        "due_amount":       _flt(lr0.get("DueAmount")),
        "payment_method":   lr0.get("PaymentMethod"),
        "delivery_partner": lr0.get("DeliveryPartner"),
        "dp_status":        lr0.get("DeliveryPartnerStatus"),
        "delivered_at":     _fmt(lr0.get("DeliveredAt")),
        "district":         lr0.get("District"),
        "sub_district":     lr0.get("SUB_DISTRICT"),
        "customer_address": lr0.get("CustomerAddress"),
    }

    # ── 9. Purchase Pattern ───────────────────────────────────────────────
    inv_dt_map: dict = {}
    for r in rows:
        inv = r.get("Invoice")
        dt = _dt(r.get("CreationDate"))
        if inv and dt:
            # keep the earliest date per invoice (chronological anchor)
            if inv not in inv_dt_map or dt < inv_dt_map[inv]:
                inv_dt_map[inv] = dt

    sorted_inv_dates = sorted(inv_dt_map.values())
    n_unique_orders = len(sorted_inv_dates)
    today_dt = datetime.now()
    last_order_days_ago = (today_dt - sorted_inv_dates[-1]).days if sorted_inv_dates else None

    if n_unique_orders <= 1:
        pp_label = "New / One-time"
        avg_gap = None
    else:
        gaps = [(sorted_inv_dates[i + 1] - sorted_inv_dates[i]).days for i in range(n_unique_orders - 1)]
        avg_gap = round(sum(gaps) / len(gaps), 1)
        if avg_gap <= 35:
            pp_label = "Monthly / Frequent Buyer"
        elif avg_gap <= 100:
            pp_label = "Quarterly Buyer"
        else:
            pp_label = "Seasonal / Occasional Buyer"

    reorder_due = False
    if last_order_days_ago is not None:
        if pp_label == "Monthly / Frequent Buyer" and last_order_days_ago > 40:
            reorder_due = True
        elif pp_label == "Quarterly Buyer" and last_order_days_ago > 110:
            reorder_due = True
        elif pp_label == "Seasonal / Occasional Buyer" and last_order_days_ago > 200:
            reorder_due = True

    purchase_pattern = {
        "label": pp_label,
        "avg_days_between_orders": avg_gap,
        "last_order_days_ago": last_order_days_ago,
        "reorder_due": reorder_due,
    }

    # ── 10. SS Remarks (order-level notes) ────────────────────────────────
    remark_cols = [
        ("CancelledFlaggedReason", "cancel/return"),
        ("AdditionalNote",         "order_note"),
        ("InternalNotes",          "internal_note"),
    ]
    seen_remark_keys: set = set()
    ss_remarks = []
    for r in rows:
        inv = r.get("Invoice") or ""
        dt_str = _fmt(r.get("CreationDate"))
        for col, source in remark_cols:
            val = (r.get(col) or "").strip() if r.get(col) is not None else ""
            if val:
                key = (inv, col, val)
                if key not in seen_remark_keys:
                    seen_remark_keys.add(key)
                    ss_remarks.append({
                        "date": dt_str,
                        "source": source,
                        "invoice": inv,
                        "text": val,
                    })

    # ── 11. Flat orders list ────────────────────────────────────────────────
    orders_list = [
        {
            "invoice":          r["Invoice"],
            "date":             _fmt(r["CreationDate"]),
            "shipped":          _fmt(r["ShippedAt"]),
            "delivered":        _fmt(r["DeliveredAt"]),
            "product":          r["ProductName"],
            "qty":              _itn(r["ProductQty"]),
            "total_price":      _flt(r["TotalPrice"]),
            "sales_amount":     _flt(r["SalesAmount"]),
            "due":              _flt(r["DueAmount"]),
            "paid":             _flt(r["PaymentsPaidAmount"]),
            "status":           r["Status"],
            "sub_status":       r["SubStatus"],
            "payment_method":   r["PaymentMethod"],
            "delivery_partner": r["DeliveryPartner"],
            "dp_status":        r["DeliveryPartnerStatus"],
            "district":         r["District"],
        }
        for r in rows
    ]

    return {
        "found": True,
        # basics
        "total_orders":       total_orders,
        "total_spend":        round(total_spend, 2),
        "avg_basket":         avg_basket,
        "last_order_date":    _fmt(last["CreationDate"]),
        "last_delivery_status": (last.get("DeliveryPartnerStatus") or last.get("Status") or ""),
        # volume & value
        "total_items_purchased":   total_items,
        "avg_items_per_order":     avg_items_ord,
        "total_discount_received": round(total_discount, 2),
        "total_due_amount":        round(total_due, 2),
        "total_paid_amount":       round(total_paid, 2),
        # return metrics
        "total_flagged_orders":  total_flagged,
        "return_rate_by_order":  return_rate_by_order,
        "return_rate_by_qty":    return_rate_by_qty,
        "return_reasons":        sorted([{"reason": k, "count": v} for k, v in return_reasons.items()], key=lambda x: -x["count"]),
        "substatus_breakdown":   sorted([{"substatus": k, "count": v} for k, v in substatus_map.items()], key=lambda x: -x["count"]),
        # status
        "status_breakdown": status_map,
        # delivery
        "avg_delivery_days":          avg_delivery_days,
        "delivery_partner_breakdown": dp_breakdown,
        # payment
        "payment_method_breakdown": pm_breakdown,
        "partial_payment_count":    partial_payment_count,
        # products
        "top_products":           top_products_list,
        "repeat_product_rate":    repeat_product_rate,
        "unique_products_bought": unique_products,
        # channel & segment
        "order_source_breakdown": source_breakdown,
        "customer_tag":           current_tag,
        "customer_tag_history":   tag_history,
        "districts_used":         sorted(district_set),
        "district_consistent":    district_consistent,
        # NEW
        "last_order_detail":  last_order_detail,
        "purchase_pattern":   purchase_pattern,
        "ss_remarks":         ss_remarks,
        # full list (kept for backward compat)
        "orders": orders_list,
    }


# ---------------------------------------------------------------------------
# Heatmap: daily order counts + single-day order list
# ---------------------------------------------------------------------------

def get_heatmap_data(start_date: str, end_date: str) -> list:
    """Daily order counts for heatmap. Returns [{date, order_count, total_qty}]."""
    _validate_cols()
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT CAST(CreationDate AS DATE),
                   COUNT(DISTINCT Invoice),
                   SUM(ISNULL(CAST(ProductQty AS INT), 0))
            FROM dbo.testMaster WITH (NOLOCK)
            WHERE CAST(CreationDate AS DATE) >= CAST(? AS DATE)
              AND CAST(CreationDate AS DATE) <= CAST(? AS DATE)
            GROUP BY CAST(CreationDate AS DATE)
            ORDER BY 1
            """,
            (start_date, end_date),
        )
        return [
            {"date": str(r[0])[:10], "order_count": int(r[1] or 0), "total_qty": int(r[2] or 0)}
            for r in cur.fetchall()
        ]


def get_day_orders(date: str) -> list:
    """Full order list for a single date, grouped by Invoice."""
    _validate_cols()
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT Invoice, CustomerName, CustomerPhoneNumber,
                   ProductName, ProductQty, SalesAmount,
                   Status, SubStatus, PaymentMethod, DeliveryPartner, District
            FROM dbo.testMaster WITH (NOLOCK)
            WHERE CAST(CreationDate AS DATE) = CAST(? AS DATE)
            ORDER BY Invoice, ProductName
            """,
            (date,),
        )
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]

    inv_map: dict = {}
    for r in rows:
        inv = _fmt(r.get("Invoice")) or ""
        if inv not in inv_map:
            inv_map[inv] = {
                "invoice": inv,
                "customer_name": _fmt(r.get("CustomerName")) or "",
                "customer_phone": _fmt(r.get("CustomerPhoneNumber")) or "",
                "status": _fmt(r.get("Status")) or "",
                "sub_status": _fmt(r.get("SubStatus")) or "",
                "payment_method": _fmt(r.get("PaymentMethod")) or "",
                "delivery_partner": _fmt(r.get("DeliveryPartner")) or "",
                "district": _fmt(r.get("District")) or "",
                "products": [],
                "total_amount": 0.0,
                "total_qty": 0,
            }
        inv_map[inv]["products"].append({
            "name": _fmt(r.get("ProductName")) or "",
            "qty": _itn(r.get("ProductQty")),
        })
        inv_map[inv]["total_amount"] = round(
            inv_map[inv]["total_amount"] + _flt(r.get("SalesAmount")), 2
        )
        inv_map[inv]["total_qty"] += _itn(r.get("ProductQty"))

    return list(inv_map.values())


# ---------------------------------------------------------------------------
# Dashboard: aggregate queries (all cached)
# ---------------------------------------------------------------------------

def _all_order_phones() -> set:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT DISTINCT CustomerPhoneNumber FROM dbo.testMaster WITH (NOLOCK) "
            "WHERE CustomerPhoneNumber IS NOT NULL AND CustomerPhoneNumber != ''"
        )
        return {normalize_phone(r[0]) for r in cur.fetchall()}


def get_all_order_phones() -> set:
    return _cached("ss_phones", _all_order_phones)


def _q_aov():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT SUM(SalesAmount), COUNT(DISTINCT Invoice) FROM dbo.testMaster WITH (NOLOCK) WHERE SalesAmount IS NOT NULL AND SalesAmount > 0")
        r = cur.fetchone()
        total_sales, inv_count = float(r[0] or 0), int(r[1] or 0)
        return {"aov": round(total_sales / inv_count, 2) if inv_count else 0}


def _q_top_qty():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT TOP 10 ProductName, SUM(ProductQty) as qty FROM dbo.testMaster WITH (NOLOCK) WHERE ProductName IS NOT NULL AND ProductName != '' GROUP BY ProductName ORDER BY qty DESC")
        return {"top_products_by_qty": [{"name": r[0], "qty": int(r[1] or 0)} for r in cur.fetchall()]}


def _q_top_rev():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT TOP 10 ProductName, SUM(SalesAmount) as rev FROM dbo.testMaster WITH (NOLOCK) WHERE ProductName IS NOT NULL AND ProductName != '' GROUP BY ProductName ORDER BY rev DESC")
        return {"top_products_by_revenue": [{"name": r[0], "revenue": round(float(r[1] or 0), 2)} for r in cur.fetchall()]}


def _q_order_status():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT Status, COUNT(*) as cnt FROM dbo.testMaster WITH (NOLOCK) WHERE Status IS NOT NULL AND Status != '' GROUP BY Status ORDER BY cnt DESC")
        return {"order_status_funnel": [{"status": r[0], "count": int(r[1])} for r in cur.fetchall()]}


def _q_repeat():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(DISTINCT CASE WHEN order_count > 1 THEN CustomerPhoneNumber END) * 100.0 /
                   NULLIF(COUNT(DISTINCT CustomerPhoneNumber), 0)
            FROM (
                SELECT CustomerPhoneNumber, COUNT(DISTINCT Invoice) as order_count
                FROM dbo.testMaster WITH (NOLOCK)
                WHERE CustomerPhoneNumber IS NOT NULL AND CustomerPhoneNumber != ''
                  AND CreationDate >= DATEADD(YEAR, -2, GETDATE())
                GROUP BY CustomerPhoneNumber
            ) t
        """)
        r = cur.fetchone()
        return {"repeat_rate": round(float(r[0] or 0), 1)}


def _q_districts():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT TOP 10 District, COUNT(*) as cnt FROM dbo.testMaster WITH (NOLOCK) WHERE District IS NOT NULL AND District != '' GROUP BY District ORDER BY cnt DESC")
        return {"district_distribution": [{"district": r[0], "count": int(r[1])} for r in cur.fetchall()]}


def _q_delivery():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT DeliveryPartner,
                SUM(CASE WHEN Status IN ('DELIVERED','COMPLETED') THEN 1 ELSE 0 END),
                SUM(CASE WHEN Status IN ('FLAGGED','CANCELLED','CANCELED','Returned','Return Request','COMPLAIN') THEN 1 ELSE 0 END),
                COUNT(*) as total
            FROM dbo.testMaster WITH (NOLOCK)
            WHERE DeliveryPartner IS NOT NULL AND DeliveryPartner != ''
            GROUP BY DeliveryPartner ORDER BY total DESC
        """)
        return {"delivery_partner_performance": [{"partner": r[0], "delivered": int(r[1] or 0), "flagged": int(r[2] or 0), "total": int(r[3])} for r in cur.fetchall()]}


def _q_payment():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT PaymentMethod, COUNT(*) as cnt FROM dbo.testMaster WITH (NOLOCK) WHERE PaymentMethod IS NOT NULL AND PaymentMethod != '' GROUP BY PaymentMethod ORDER BY cnt DESC")
        pm_grouped: dict = {}
        for row in cur.fetchall():
            key = row[0] if not row[0].strip().isdigit() else "Other"
            pm_grouped[key] = pm_grouped.get(key, 0) + int(row[1])
        return {"payment_method_breakdown": sorted([{"method": k, "count": v} for k, v in pm_grouped.items()], key=lambda x: -x["count"])}


def _q_due():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT SUM(DueAmount), COUNT(CASE WHEN DueAmount > 0 THEN 1 END) FROM dbo.testMaster WITH (NOLOCK) WHERE DueAmount IS NOT NULL")
        r = cur.fetchone()
        return {"due_overview": {"total_due": round(float(r[0] or 0), 2), "orders_with_due": int(r[1] or 0)}}


def _dashboard_queries() -> dict:
    _validate_cols()
    fns = [_q_aov, _q_top_qty, _q_top_rev, _q_order_status, _q_repeat, _q_districts, _q_delivery, _q_payment, _q_due]
    result: dict = {}
    with ThreadPoolExecutor(max_workers=len(fns)) as ex:
        futures = {ex.submit(fn): fn.__name__ for fn in fns}
        for future in as_completed(futures):
            result.update(future.result())
    return result


def get_dashboard_sqlserver() -> dict:
    return _cached("ss_dashboard", _dashboard_queries)
