"""
Customer Intelligence Dashboard
---------------------------------
/api/profile  — phone lookup: PostgreSQL call log + SQL Server order history
/api/dashboard — aggregate KPIs from both databases (5-min in-memory cache)
"""

import json
import os
import shutil
import threading
import time
import urllib.request
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from utils import normalize_phone
import db_sqlserver as ss

load_dotenv()

UPLOAD_DIR = Path("static/uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

DATABASE_URL = os.getenv("DATABASE_URL")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
_GEMINI_MODEL = "gemini-2.5-flash"
TABLE_NAME = os.getenv("TABLE_NAME", "customer_calls")
TABLE_SCHEMA = os.getenv("TABLE_SCHEMA", "public")

if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL missing. Copy .env.example to .env and fill in your real host."
    )

app = FastAPI(title="Customer Intelligence Dashboard")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@contextmanager
def get_conn():
    conn = psycopg2.connect(DATABASE_URL)
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Column auto-detection for PostgreSQL customer_calls table
# ---------------------------------------------------------------------------
_COLUMN_CACHE = None


def fetch_columns():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = %s AND table_name = %s
                ORDER BY ordinal_position
                """,
                (TABLE_SCHEMA, TABLE_NAME),
            )
            rows = cur.fetchall()
    if not rows:
        raise HTTPException(
            status_code=500,
            detail=f"Table {TABLE_SCHEMA}.{TABLE_NAME} pawa jay nai. Naam thik ache to?",
        )
    return rows


def detect(columns, keywords):
    lower = {c[0].lower(): c[0] for c in columns}
    for kw in keywords:
        for low, original in lower.items():
            if kw in low:
                return original
    return None


def _gemini_summarize(remarks: list) -> str | None:
    """Call Gemini 2.5 Flash to summarize remarks. Returns None on any failure."""
    if not GEMINI_API_KEY or not remarks:
        return None
    try:
        lines = []
        for r in remarks[:30]:
            date = (r.get("date") or "?")[:10]
            src = r.get("source", "")
            txt = r.get("text", "")
            lines.append(f"[{date}] ({src}) {txt}")
        prompt = (
            "Nichar customer-er call o order notes gula poro. "
            "2-3 line-er ekta summary dao: ei customer shomporke ki pattern ba recurring issue dekhcho? "
            "Bangla o English mix kore likhte paro. Sudhu summary, kono heading ba bullet point na.\n\n"
            + "\n".join(lines)
        )
        payload = json.dumps({
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": 250},
        }).encode()
        req = urllib.request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/{_GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read())
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception:
        return None


def get_column_map():
    global _COLUMN_CACHE
    if _COLUMN_CACHE is not None:
        return _COLUMN_CACHE
    columns = fetch_columns()
    cmap = {
        "phone": detect(columns, ["phone", "mobile", "contact_number", "number"]),
        "name": detect(columns, ["customer_name", "name"]),
        "status": detect(columns, ["status"]),
        "call_date": detect(columns, ["call_date", "order_date", "date"]),
        "note": detect(columns, ["note", "remark", "comment"]),
        "order_code": detect(columns, ["order_code", "order_id", "invoice", "code"]),
        "address": detect(columns, ["address"]),
        "created_at": detect(columns, ["created_at"]),
        "updated_at": detect(columns, ["updated_at"]),
        "data_source": detect(columns, ["datasourceid", "data_source"]),
    }
    if not cmap["phone"]:
        raise HTTPException(
            status_code=500,
            detail="Table-e phone/mobile naam-er kono column khuje pawa gelo na.",
        )
    _COLUMN_CACHE = {"map": cmap, "all_columns": [c[0] for c in columns]}
    return _COLUMN_CACHE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_date(d) -> datetime | None:
    if not d:
        return None
    s = str(d)[:19]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None


# ---------------------------------------------------------------------------
# Caches
# ---------------------------------------------------------------------------
_DASH_CACHE: dict = {"data": None, "ts": 0.0}
_DASH_TTL = 300

# SS phones for conversion rate — computed in background (can take 60+ seconds)
_SS_PHONES: dict = {"phones": None, "ts": 0.0, "loading": False}


def _warm_ss_phones_bg():
    if _SS_PHONES["loading"]:
        return
    _SS_PHONES["loading"] = True
    try:
        phones = ss.get_all_order_phones()
        _SS_PHONES["phones"] = phones
        _SS_PHONES["ts"] = time.time()
        _DASH_CACHE["data"] = None  # force dashboard re-compute with conversion rate
    except Exception:
        pass
    finally:
        _SS_PHONES["loading"] = False


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

@app.get("/api/columns")
def list_columns():
    return get_column_map()


@app.get("/api/profile")
def profile(phone: str = Query(..., min_length=6)):  # noqa: C901
    info = get_column_map()
    cmap = info["map"]

    core = normalize_phone(phone)
    if len(core) < 7:
        raise HTTPException(status_code=400, detail="Phone number ta thik dao, onek choto hoye gese.")

    phone_col   = cmap["phone"]
    order_col   = cmap["created_at"] or cmap["call_date"] or phone_col
    status_col  = cmap["status"]
    date_col    = cmap["call_date"]
    note_col    = cmap["note"]
    name_col    = cmap["name"]
    order_code_col = cmap["order_code"]
    address_col    = cmap["address"]
    created_col    = cmap["created_at"]
    updated_col    = cmap["updated_at"]
    datasrc_col    = cmap["data_source"]

    sql = f'''
        SELECT *
        FROM "{TABLE_SCHEMA}"."{TABLE_NAME}"
        WHERE regexp_replace("{phone_col}"::text, '\\D', '', 'g') LIKE %s
        ORDER BY "{order_col}" DESC
    '''

    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (f"%{core}",))
            rows = cur.fetchall()

    if not rows:
        return {
            "phone": phone, "found": False, "total_calls": 0,
            "call_insights": {}, "order_insights": {}, "return_metrics": {},
            "cross_insights": {}, "timeline": [],
        }

    total_calls = len(rows)

    # ── Build timeline ───────────────────────────────────────────────────────
    timeline = []
    status_counts: dict = {}
    names_seen: set = set()
    addresses_seen: set = set()
    order_codes_seen: set = set()
    notes_list: list = []
    source_map: dict = defaultdict(int)
    month_map: dict = defaultdict(int)
    resolution_times: list = []
    all_dates: list = []

    for r in rows:
        status_val = r.get(status_col) if status_col else None
        if status_val:
            status_counts[status_val] = status_counts.get(status_val, 0) + 1
        if name_col and r.get(name_col):
            names_seen.add(str(r[name_col]))
        if address_col and r.get(address_col):
            addresses_seen.add(str(r[address_col]))
        if order_code_col and r.get(order_code_col):
            order_codes_seen.add(str(r[order_code_col]))
        if note_col and r.get(note_col) and str(r[note_col]).strip():
            notes_list.append(str(r[note_col]).strip())
        if datasrc_col and r.get(datasrc_col):
            source_map[str(r[datasrc_col])] += 1

        raw_date = r.get(date_col) if date_col else None
        raw_created = r.get(created_col) if created_col else None
        call_dt = _parse_date(str(raw_date or raw_created or ""))
        if call_dt:
            all_dates.append(call_dt)
            month_map[call_dt.strftime("%Y-%m")] += 1

        # Resolution time: updated_at - created_at
        if updated_col and created_col and r.get(updated_col) and r.get(created_col):
            cr = _parse_date(str(r[created_col]))
            up = _parse_date(str(r[updated_col]))
            if cr and up and up > cr:
                resolution_times.append((up - cr).total_seconds() / 3600)

        timeline.append({
            "date":       str(raw_date) if raw_date else None,
            "created_at": str(raw_created) if raw_created else None,
            "status":     status_val,
            "note":       r.get(note_col) if note_col else None,
            "order_code": r.get(order_code_col) if order_code_col else None,
            "raw":        {k: (str(v) if v is not None else None) for k, v in r.items()},
        })

    # ── Call-level computations ──────────────────────────────────────────────
    first_dt = min(all_dates) if all_dates else None
    latest_dt = max(all_dates) if all_dates else None
    lifecycle_days = (latest_dt - first_dt).days if first_dt and latest_dt else 0

    # Status change count (consecutive different statuses in time order)
    sorted_tl = sorted(
        timeline,
        key=lambda t: t.get("date") or t.get("created_at") or "",
    )
    status_changes = sum(
        1 for i in range(1, len(sorted_tl))
        if sorted_tl[i]["status"] != sorted_tl[i-1]["status"]
    )

    follow_up_count = sum(
        1 for t in timeline
        if t.get("status") and "follow up" in str(t["status"]).lower()
    )
    avg_resolution_hours = (
        round(sum(resolution_times) / len(resolution_times), 1)
        if resolution_times else None
    )

    total_calls_pct = total_calls or 1
    status_breakdown_pct = {
        s: {"count": c, "pct": round(c * 100 / total_calls_pct, 1)}
        for s, c in sorted(status_counts.items(), key=lambda x: -x[1])
    }
    source_breakdown = sorted(
        [{"source": k, "count": v} for k, v in source_map.items()], key=lambda x: -x["count"]
    )

    latest = timeline[0]
    first_contact = timeline[-1]["date"] or timeline[-1]["created_at"]
    latest_call_date = latest["date"] or latest["created_at"]

    call_insights = {
        "total_calls":           total_calls,
        "first_contact_date":    first_contact,
        "latest_call_date":      latest_call_date,
        "customer_lifecycle_days": lifecycle_days,
        "calls_per_month":       dict(sorted(month_map.items())),
        "status_breakdown":      status_breakdown_pct,
        "latest_status":         latest["status"],
        "status_changes_count":  status_changes,
        "follow_up_count":       follow_up_count,
        "avg_resolution_hours":  avg_resolution_hours,
        "notes_summary":         notes_list,
        "source_breakdown":      source_breakdown,
        "names_used":            sorted(names_seen),
        "addresses_used":        sorted(addresses_seen),
    }

    # ── SQL Server data ──────────────────────────────────────────────────────
    orders: dict = {"found": False, "ss_available": True}
    order_insights: dict = {}
    return_metrics: dict = {}

    try:
        orders = ss.get_orders_for_phone(core)
        orders["ss_available"] = True

        if orders.get("found"):
            order_insights = {
                "total_orders":           orders["total_orders"],
                "total_items_purchased":  orders["total_items_purchased"],
                "total_spend":            orders["total_spend"],
                "avg_basket":             orders["avg_basket"],
                "avg_items_per_order":    orders["avg_items_per_order"],
                "total_discount_received": orders["total_discount_received"],
                "avg_delivery_days":      orders["avg_delivery_days"],
                "delivery_partner_breakdown": orders["delivery_partner_breakdown"],
                "top_products":           orders["top_products"],
                "repeat_product_rate":    orders["repeat_product_rate"],
                "unique_products_bought": orders["unique_products_bought"],
                "order_source_breakdown": orders["order_source_breakdown"],
                "customer_tag":           orders["customer_tag"],
                "customer_tag_history":   orders["customer_tag_history"],
                "districts_used":         orders["districts_used"],
                "district_consistent":    orders["district_consistent"],
                "status_breakdown":       orders["status_breakdown"],
                "substatus_breakdown":    orders["substatus_breakdown"],
                "last_order_date":        orders["last_order_date"],
                "last_delivery_status":   orders["last_delivery_status"],
            }
            return_metrics = {
                "total_flagged_orders":   orders["total_flagged_orders"],
                "return_rate_by_order":   orders["return_rate_by_order"],
                "return_rate_by_qty":     orders["return_rate_by_qty"],
                "return_reasons":         orders["return_reasons"],
                "substatus_breakdown":    orders["substatus_breakdown"],
                "total_due_amount":       orders["total_due_amount"],
                "total_paid_amount":      orders["total_paid_amount"],
                "partial_payment_count":  orders["partial_payment_count"],
                "payment_method_breakdown": orders["payment_method_breakdown"],
            }

        # ── Call-to-order match (±3 days) ──────────────────────────────────
        if orders.get("found") and orders.get("orders"):
            ss_dates = [_parse_date(o["date"]) for o in orders["orders"]]
            ss_dates = [d for d in ss_dates if d]
            op_kw = {"order placed", "recently ordered"}
            for t in timeline:
                if t.get("status") and t["status"].lower() in op_kw:
                    call_dt = _parse_date(t.get("date") or t.get("created_at"))
                    t["order_placed_match"] = (
                        any(abs((call_dt - sd).days) <= 3 for sd in ss_dates)
                        if call_dt and ss_dates else None
                    )

    except Exception as e:
        orders = {"found": False, "ss_available": False, "ss_error": str(e)}

    # ── Purchase pattern + last order detail ─────────────────────────────────
    purchase_pattern = orders.get("purchase_pattern") or {}
    last_order_detail = orders.get("last_order_detail") or {}
    if orders.get("found"):
        order_insights["purchase_pattern"] = purchase_pattern
        order_insights["last_order_detail"] = last_order_detail

    # ── Remarks assembly (PG call notes + SS order notes) ────────────────────
    remarks_list: list = []

    # 1. PG call notes
    for t in timeline:
        note = t.get("note")
        if note and str(note).strip():
            remarks_list.append({
                "date":   t.get("date") or t.get("created_at"),
                "source": "call_note",
                "text":   str(note).strip(),
            })

    # 2. SS order remarks
    for r in (orders.get("ss_remarks") or []):
        remarks_list.append(r)

    # Sort most-recent first
    remarks_list.sort(key=lambda r: (r.get("date") or "")[:19], reverse=True)

    ai_summary = _gemini_summarize(remarks_list) if GEMINI_API_KEY and remarks_list else None
    remarks_summary = {
        "remarks":      remarks_list,
        "ai_summary":   ai_summary,
        "ai_available": bool(ai_summary),
    }

    # ── Cross-table insights ─────────────────────────────────────────────────
    # 1. Call-to-order conversion (from annotated timeline)
    op_calls = [t for t in timeline if t.get("status") and t["status"].lower() in {"order placed", "recently ordered"}]
    matched_op = sum(1 for t in op_calls if t.get("order_placed_match") is True)
    conversion_rate = round(matched_op * 100 / len(op_calls), 1) if op_calls else None

    # 2. Unreachable-to-order: calls marked Unreachable/Not Interested, then customer ordered anyway
    negative_kw = {"unreachable", "not interested", "switch off", "invalid data"}
    neg_calls = [t for t in timeline if t.get("status") and t["status"].lower() in negative_kw]
    orders_after_neg = 0
    if neg_calls and orders.get("found") and orders.get("orders"):
        neg_dates = [_parse_date(t.get("date") or t.get("created_at")) for t in neg_calls]
        neg_dates = [d for d in neg_dates if d]
        latest_neg = max(neg_dates) if neg_dates else None
        if latest_neg:
            orders_after_neg = sum(
                1 for o in orders["orders"]
                if _parse_date(o.get("date")) and _parse_date(o["date"]) > latest_neg
            )

    # 3. Value segment
    # Thresholds — spend >5000 = high ticket BD ecommerce buyer;
    # return >30% or due >2000 = notable risk; 3+ orders low return = loyal;
    # 5+ calls, <2 orders = engagement without conversion (common cold-call scenario)
    spend   = orders.get("total_spend", 0) or 0
    tot_ord = orders.get("total_orders", 0) or 0
    ret_rt  = (return_metrics.get("return_rate_by_order") or 0)
    due_amt = (return_metrics.get("total_due_amount") or 0)

    if spend >= 5000:
        segment, seg_reason = "High Value", f"Lifetime spend ৳{spend:,.0f} ≥ ৳5,000"
    elif ret_rt > 30 or due_amt > 2000:
        parts = []
        if ret_rt > 30: parts.append(f"return rate {ret_rt}% > 30%")
        if due_amt > 2000: parts.append(f"due ৳{due_amt:,.0f} > ৳2,000")
        segment, seg_reason = "At Risk", " & ".join(parts)
    elif tot_ord >= 3 and ret_rt < 10:
        segment, seg_reason = "Loyal", f"{tot_ord} orders, only {ret_rt}% return"
    elif total_calls >= 5 and tot_ord < 2:
        segment, seg_reason = "Frequent Caller", f"{total_calls} calls but only {tot_ord} order(s)"
    elif tot_ord == 0 and total_calls <= 3:
        segment, seg_reason = "New / Untested", "No orders yet, early-stage contact"
    else:
        segment, seg_reason = "Regular", "Standard engagement profile"

    cross_insights = {
        "call_to_order_conversion": {
            "order_placed_calls": len(op_calls),
            "matched_orders":     matched_op,
            "conversion_rate_pct": conversion_rate,
        },
        "unreachable_to_order": {
            "negative_status_calls": len(neg_calls),
            "orders_after_negative": orders_after_neg,
            "recovered":             orders_after_neg > 0,
        },
        "customer_value_segment": segment,
        "segment_reason":         seg_reason,
    }

    return {
        "phone":    phone,
        "found":    True,
        "call_insights":   call_insights,
        "order_insights":  order_insights,
        "return_metrics":  return_metrics,
        "cross_insights":  cross_insights,
        "remarks_summary": remarks_summary,
        "timeline":        timeline,
        "orders":          orders,
        # ── backward-compat flat keys ──
        "total_calls":        total_calls,
        "names_seen":         sorted(names_seen),
        "addresses_seen":     sorted(addresses_seen),
        "order_codes_seen":   sorted(order_codes_seen),
        "status_breakdown":   status_counts,
        "latest_status":      latest["status"],
        "latest_date":        latest_call_date,
        "first_contact_date": first_contact,
    }


@app.get("/api/dashboard")
def get_dashboard():
    now = time.time()
    if _DASH_CACHE["data"] and now - _DASH_CACHE["ts"] < _DASH_TTL:
        return _DASH_CACHE["data"]

    result: dict = {}
    op_phones_set: set = set()

    # ---- PostgreSQL: call status breakdown + order-placed phones ----
    try:
        info = get_column_map()
        cmap = info["map"]
        status_col = cmap["status"]
        phone_col = cmap["phone"]

        with get_conn() as conn:
            with conn.cursor() as cur:
                if status_col:
                    cur.execute(
                        f'SELECT "{status_col}", COUNT(*) '
                        f'FROM "{TABLE_SCHEMA}"."{TABLE_NAME}" '
                        f'GROUP BY "{status_col}" ORDER BY COUNT(*) DESC'
                    )
                    rows = cur.fetchall()
                    total_calls = sum(r[1] for r in rows)
                    result["call_status_breakdown"] = [
                        {
                            "status": r[0] or "Unknown",
                            "count": r[1],
                            "pct": round(r[1] * 100 / total_calls, 1) if total_calls else 0,
                        }
                        for r in rows
                    ]
                    result["total_calls"] = total_calls
                else:
                    result["call_status_breakdown"] = []
                    result["total_calls"] = 0

                # Phones with "Order Placed" / "Recently Ordered" for conversion rate
                if status_col and phone_col:
                    cur.execute(
                        f'SELECT regexp_replace("{phone_col}"::text, \'\\D\', \'\', \'g\') '
                        f'FROM "{TABLE_SCHEMA}"."{TABLE_NAME}" '
                        f'WHERE LOWER("{status_col}") = ANY(%s)',
                        (["order placed", "recently ordered"],),
                    )
                    op_phones_set = {
                        normalize_phone(r[0] or "") for r in cur.fetchall() if r[0]
                    }
                    op_phones_set = {p for p in op_phones_set if len(p) >= 7}
    except Exception as e:
        result.setdefault("call_status_breakdown", [])
        result.setdefault("total_calls", 0)
        result["pg_error"] = str(e)

    # ---- SQL Server: all aggregate metrics ----
    try:
        ss_data = ss.get_dashboard_sqlserver()
        result.update(ss_data)
    except Exception as e:
        result["ss_error"] = str(e)

    # ---- Conversion rate (cross-DB) — uses background-loaded SS phones ----
    total_op = len(op_phones_set)
    if _SS_PHONES["phones"] is not None:
        ss_phones = _SS_PHONES["phones"]
        matched = len(op_phones_set & ss_phones)
        result["conversion"] = {
            "order_placed_calls": total_op,
            "matched_orders": matched,
            "rate_pct": round(matched * 100 / total_op, 1) if total_op else 0,
            "status": "ready",
        }
    else:
        result["conversion"] = {
            "order_placed_calls": total_op,
            "matched_orders": None,
            "rate_pct": None,
            "status": "calculating",
        }
        # Trigger background fetch if not already running
        if not _SS_PHONES["loading"]:
            threading.Thread(target=_warm_ss_phones_bg, daemon=True).start()

    _DASH_CACHE["data"] = result
    _DASH_CACHE["ts"] = now
    return result


# ---------------------------------------------------------------------------
# Heatmap endpoints
# ---------------------------------------------------------------------------

@app.get("/api/orders/heatmap")
def orders_heatmap(
    start: str = Query(None),
    end: str = Query(None),
):
    today = datetime.now().date()
    if not end:
        end = str(today)
    if not start:
        start = str(today - timedelta(days=120))
    try:
        data = ss.get_heatmap_data(start, end)
        return {"start": start, "end": end, "data": data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/orders/day")
def orders_day(date: str = Query(...)):
    try:
        orders = ss.get_day_orders(date)
        return {"date": date, "orders": orders}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Logo upload / serve
# ---------------------------------------------------------------------------

@app.get("/api/logo")
def get_logo():
    for ext in (".png", ".jpg", ".jpeg", ".svg"):
        f = UPLOAD_DIR / f"logo{ext}"
        if f.exists():
            return {"url": f"/uploads/logo{ext}"}
    raise HTTPException(status_code=404, detail="লোগো পাওয়া যায়নি।")


@app.post("/api/logo")
async def upload_logo(file: UploadFile = File(...)):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in {".png", ".jpg", ".jpeg", ".svg"}:
        raise HTTPException(status_code=400, detail="শুধু .png, .jpg, .jpeg, .svg ফাইল আপলোড করা যাবে।")
    for old in UPLOAD_DIR.glob("logo.*"):
        old.unlink(missing_ok=True)
    dest = UPLOAD_DIR / f"logo{ext}"
    with dest.open("wb") as fh:
        shutil.copyfileobj(file.file, fh)
    return {"url": f"/uploads/logo{ext}"}


# Static frontend served last (catch-all)
app.mount("/", StaticFiles(directory="static", html=True), name="static")
