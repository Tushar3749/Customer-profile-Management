"""
Order / product / call / delivery aggregation queries and derived metrics.

Responsibility:
- Run the core SQL queries from the handoff spec (order history, core
  metrics, product return rate, call stats, delivery performance)
  against Test.dbo.TestMaster and dcm tables.
- Compute derived values not practical in T-SQL (e.g. consecutive-
  unreachable streak) in Python from ordered call history.
- Own the configurable business-rule constants (VIP/tier thresholds,
  escalation threshold) as named constants — not yet numerically
  confirmed by the business, flagged with '# TODO: confirm with
  business' at the point they are defined.
"""

import datetime as dt
import re

from app.db import fetch_all


def _to_float(value):
    """SQL Server DECIMAL/NUMERIC/MONEY columns come back from pyodbc as
    decimal.Decimal, which raises TypeError when mixed with a plain
    Python float in arithmetic (e.g. Decimal('5') * 0.4, or summing a
    Decimal with a float produced by int/int division). Every DB-sourced
    numeric value that this module does arithmetic on is normalized to
    float at the point it's read, so nothing downstream has to reason
    about Decimal again."""
    return None if value is None else float(value)


def _clean_nan_string(value):
    """DeliveryPartnerStatus (and other courier-sourced text columns) can
    contain the literal 3-character string "nan" instead of a real NULL —
    confirmed live on real rows, an artifact of an upstream pandas import
    that stringified missing values instead of writing NULL. Treat it the
    same as NULL rather than show "nan" to an agent."""
    if value is None:
        return None
    stripped = value.strip()
    return None if stripped.lower() == 'nan' else stripped


def _trim(column: str) -> str:
    """SQL fragment for a generic leading/trailing-whitespace trim.

    CONFIRMED FIX 2026-08-30 — real TestMaster rows have inconsistent
    whitespace padding: some via a plain leading space, some via CHAR(9)
    (tab). Plain LTRIM(RTRIM(...)) alone only strips spaces — verified
    live against this SQL Server instance, LTRIM(RTRIM(CHAR(9)+'ABC'))
    still returns a tab-prefixed string. Normalizing tab/CR/LF to a space
    first, then trimming, is what actually collapses both variants of the
    same value into one. Used everywhere a TestMaster text column is
    grouped, joined, or compared (Invoice, ProductSKU, ProductName,
    DeliveryPartner, PaymentMethod, OrderSource, PickUpLocation, ...).

    CONFIRMED FIX 2026-08-31 — also replaces CHAR(160) (non-breaking
    space). Verified live: OrderSource has a real row storing
    'DATA' + CHAR(160) + 'CALL' (an embedded NBSP, not a regular space)
    — a plain '= DATA CALL' comparison in the channel CASE mapping below
    would silently miss it and fall through to the ELSE/raw-passthrough
    branch without this."""
    return (
        f"LTRIM(RTRIM(REPLACE(REPLACE(REPLACE(REPLACE("
        f"{column}, CHAR(9), ' '), CHAR(13), ' '), CHAR(10), ' '), CHAR(160), ' ')))"
    )


# --- Configurable business-rule constants -----------------------------
# All thresholds below confirmed by business on 2026-08-12 (handoff spec
# section 10: "Open Decisions for Claude Code to Confirm, Not Assume").

VIP_TIER_LTV_THRESHOLD = 20000        # CONFIRMED 2026-08-12 (BDT)
VIP_TIER_AOV_THRESHOLD = 1000         # CONFIRMED 2026-08-12 (BDT)
LOYAL_TIER_LTV_THRESHOLD = 5000       # CONFIRMED 2026-08-12 (BDT)
LOYAL_TIER_ORDER_COUNT_THRESHOLD = 3  # CONFIRMED 2026-08-12
DORMANT_DAYS_THRESHOLD = 60           # CONFIRMED 2026-08-12
AT_RISK_CANCEL_RATE_PCT = 30          # CONFIRMED 2026-08-12
AT_RISK_RETURN_RATE_PCT = 20          # CONFIRMED 2026-08-12
NEW_CUSTOMER_DAYS_THRESHOLD = 30      # CONFIRMED 2026-08-12

# CONFIRMED 2026-08-12 — same value the prototype used as an example.
ESCALATION_UNREACHABLE_STREAK = 4

UNREACHABLE_STATUSES = ('Unreachable', 'Switch Off')
CONVERTED_STATUSES = ('Order Placed', 'Recently Ordered')

# TestMaster.Status has ~29 raw spellings in production data (case,
# spacing, hyphen/underscore variants, plus lifecycle states like
# PENDING/PROCESSING/APPROVED/SHIPPED that aren't one of the frontend's
# 4 known order statuses). This collapses only unambiguous same-word
# variants — not a business guess about which lifecycle states count as
# "in transit" — into that 4-value vocabulary. Anything else is passed
# through lowercased as-is; the frontend renders it as a plain "other"
# chip instead of crashing (previously it did: an order with any status
# outside this map threw inside renderOrders(), which aborted
# renderCustomer() before it reached renderProducts/renderCalls/
# renderDelivery/renderChannel — the actual cause of those 4 tabs
# rendering empty even though the API response had real data in them).
_STATUS_ALIASES = {
    'cancelled': 'cancelled', 'canceled': 'cancelled',
    'delivered': 'delivered',
    'returned': 'returned',
    'intransit': 'intransit', 'in transit': 'intransit',
    'in-transit': 'intransit', 'in_transit': 'intransit',
}


def normalize_order_status(raw_status: str | None) -> str:
    if not raw_status:
        return ''
    cleaned = raw_status.strip().lower()
    return _STATUS_ALIASES.get(cleaned, cleaned)


# --- Core SQL queries (handoff spec section 5) -------------------------

def get_order_rows(phone: str) -> list[dict]:
    """Raw line-item rows for a customer — one row per product per invoice.

    CONFIRMED 2026-08-30 — real TestMaster rows for this table have leading
    whitespace on Invoice/ProductSKU that isn't consistently one character:
    some rows use a plain leading space, others CHAR(9) (tab). Plain
    LTRIM(RTRIM(...)) only strips spaces on this SQL Server instance —
    verified live that LTRIM(RTRIM(CHAR(9)+'ABC')) still returns a
    tab-prefixed string — so _trim() (REPLACE tab/CR/LF to space, then
    LTRIM/RTRIM) is what's actually used here, and in every other query
    below that groups/joins on a TestMaster text column. Without it, the
    same invoice/SKU with and without the stray whitespace is treated as
    two different rows throughout this module (inflates total_orders/LTV/
    AOV, and shows duplicate product cards on the Products tab).

    Also selects the columns needed for the Delivery tab's courier-status
    badge, per-order timeline, pickup location and damage tracking —
    DeliveryPartnerStatus/PickUpLocation/CancelledReturnedDamagedQty/
    ShippingDate/ShippedAt weren't read before. District/SUB_DISTRICT/
    ThanaName were checked (2026-08-30) and are 0-5% populated in this
    table — not read here; see the address-breakdown note in the Delivery
    tab work for why that one was skipped.

    CONFIRMED FIX 2026-09-01 — ShippedAt/DeliveredAt/ShippingDate wrap the
    raw column in NULLIF(LTRIM(RTRIM(...)), '') before TRY_CONVERT, same
    pattern as get_avg_delivery_days() below. Verified live against this
    SQL Server instance: TRY_CONVERT(date, '') returns 1900-01-01, NOT
    NULL — a real, sizeable data issue (73,292 blank ShippedAt rows,
    392,120 blank DeliveredAt rows out of 5.8M, verified via direct COUNT)
    that, without this guard, makes an in-transit order's still-empty
    DeliveredAt "convert" to 1900-01-01 instead of staying null — which the
    frontend's renderDeliveryTimeline() reads as `t[s.key]` truthy and
    therefore marks the "Delivered" step as done. CreationDate is NOT
    wrapped the same way — verified 0 blank rows out of 5.8M, so there is
    no equivalent bug there today.

    CONFIRMED FIX 2026-09-08 — DeliveryID also needed _trim(): verified
    live, ~30% of populated DeliveryID values (431,804 of 1,430,682) carry
    the same leading/trailing tab/CR/LF/NBSP padding as Invoice/ProductSKU
    above. Went unnoticed until now because this column was only ever
    shown buried in the order-detail modal; the Delivery tab's new
    prominent tracking-ID card (with a copy button) is the first place a
    stray leading tab would actually corrupt what an agent reads out loud
    or copies to a customer.

    CONFIRMED FIX 2026-09-09 — DeliveryPartner/PickUpLocation also need
    semantic normalization on top of _trim(), not just whitespace
    cleanup: real rows spell the SAME courier/warehouse multiple distinct
    ways (verified live: 'Stead Fast' 2,092,178 rows + 'Steadfast'
    1,979,204 rows are one company; 'Banasree Warehouse' 1,414,955 +
    'Banasree' 567,399 are one warehouse). Every place that reads these
    two columns — this per-order query, get_delivery_performance()'s
    courier chart/success-rate, and get_pickup_location_breakdown() — now
    runs through the same _courier_case()/_pickup_location_case() mapping
    so a customer's per-order courier/pickup text always matches what the
    aggregate chart/summary say, instead of three independently-guessed
    spellings of the same real thing."""
    return fetch_all(
        f"""
        SELECT {_trim('Invoice')} AS Invoice,
               TRY_CONVERT(date, CreationDate) AS CreationDate,
               TRY_CONVERT(date, NULLIF(LTRIM(RTRIM(ShippingDate)), '')) AS ShippingDate,
               TRY_CONVERT(date, NULLIF(LTRIM(RTRIM(ShippedAt)), '')) AS ShippedAt,
               TRY_CONVERT(date, NULLIF(LTRIM(RTRIM(DeliveredAt)), '')) AS DeliveredAt, Status,
               {_trim('ProductName')} AS ProductName,
               {_trim('ProductSKU')} AS ProductSKU,
               ProductQty, UnitPrice, TotalPrice,
               DeliveryFee, PaymentsPaidAmount, DueAmount,
               {_courier_case(_trim('DeliveryPartner'))} AS DeliveryPartner,
               {_trim('DeliveryID')} AS DeliveryID,
               {_trim('PaymentMethod')} AS PaymentMethod,
               CancelledFlaggedReason,
               {_trim('DeliveryPartnerStatus')} AS DeliveryPartnerStatus,
               {_pickup_location_case(_trim('PickUpLocation'))} AS PickUpLocation,
               CancelledReturnedDamagedQty,
               CustomerName, CustomerAddress
        FROM Test.dbo.TestMaster
        WHERE CustomerPhoneNumber = ?
        ORDER BY TRY_CONVERT(date, CreationDate) DESC
        """,
        (phone,),
    )


def get_current_identity(order_rows: list[dict]) -> dict:
    """CustomerName/CustomerAddress can change between orders (address
    changes over time), so per the confirmed rule (2026-08-12) we take
    both from the single most recent order only — order_rows is already
    sorted by CreationDate DESC, so that's simply the first row. Older
    orders' addresses are intentionally not surfaced."""
    if not order_rows:
        return {'name': None, 'address': None}
    latest = order_rows[0]
    return {'name': latest.get('CustomerName'), 'address': latest.get('CustomerAddress')}


def group_orders_by_invoice(rows: list[dict]) -> list[dict]:
    """Group raw line-item rows into one order dict per Invoice, matching
    the accordion structure the frontend's renderOrders() expects."""
    orders: dict[str, dict] = {}
    for row in rows:
        invoice = row['Invoice']
        if invoice is None:
            # A line item with no invoice number isn't a displayable order
            # (Order.invoice is a required string in the response schema,
            # and COUNT(DISTINCT Invoice) in get_core_metrics() already
            # excludes NULLs per standard SQL semantics) — skip rather than
            # crash the whole profile response over a handful of bad rows.
            continue
        if invoice not in orders:
            orders[invoice] = {
                'invoice': invoice,
                'status': row['Status'],
                'date': row['CreationDate'],
                # DeliveryFee/PaymentsPaidAmount/DueAmount are DECIMAL/MONEY
                # columns — normalize to float (see _to_float).
                'delivery_fee': _to_float(row['DeliveryFee']),
                'paid_amount': _to_float(row['PaymentsPaidAmount']),
                'due_amount': _to_float(row['DueAmount']),
                'courier': row['DeliveryPartner'],
                'delivery_id': row['DeliveryID'],
                'payment_method': row['PaymentMethod'],
                'cancelled_flagged_reason': row['CancelledFlaggedReason'],
                # The courier's OWN tracking status — a separate column from
                # the business Status above, and can disagree with it (e.g.
                # Status='In-Transit' but the courier already marked
                # 'delivered'). "nan" strings cleaned to real None.
                'delivery_partner_status': _clean_nan_string(row['DeliveryPartnerStatus']),
                'pickup_location': row['PickUpLocation'],
                'timeline': {
                    'order_date': row['CreationDate'],
                    'shipping_date': row['ShippingDate'],
                    'shipped_at': row['ShippedAt'],
                    'delivered_at': row['DeliveredAt'],
                },
                'items': [],
            }
        orders[invoice]['items'].append({
            'product_name': row['ProductName'],
            'product_sku': row['ProductSKU'],
            # ProductQty/UnitPrice are NULL on some real rows — the response
            # schema requires non-null int/float here, so default rather
            # than let pydantic reject the whole profile with a 500.
            'qty': row['ProductQty'] or 0,
            'unit_price': _to_float(row['UnitPrice']) or 0.0,
            # CancelledReturnedDamagedQty is a per-line-item quantity (same
            # shape as ProductQty), not a repeated invoice-level value.
            'damaged_qty': _to_float(row['CancelledReturnedDamagedQty']) or 0.0,
        })
    return list(orders.values())


# --- Order remarks (AdditionalNote / InternalNotes cleaning) -----------
# CONFIRMED 2026-08-31 — real InternalNotes values mix three system-
# generated shapes with genuine human-written text, verified live against
# this table:
#  1. `[At HH:MM AM/PM On DD Mon YYYY By <agent name>]` — an audit trailer
#     the system appends to EVERY note (human or not); stripped from
#     anywhere in the string, not just the end, since it's never itself
#     part of what an agent wrote.
#  2. `[] | Warehouse changed to WH ID <N> by <agent> <id>. Note: <text>`
#     — a warehouse-reassignment system log with an optional trailing
#     human note. Real rows show the human part empty far more often than
#     not (`...Note: ` with nothing after) — those rows are pure noise
#     and skipped entirely, not shown as a blank remark.
#  3. `"[{\note\":\"<text>` — a mis-escaped/truncated JSON note wrapper
#     (upstream serialization bug: the closing `}]` is missing on every
#     real row seen). <text> itself is a genuine agent shorthand note
#     ("cnr", "call not answer", "off", ...) and is extracted, not
#     discarded — only the JSON wrapper syntax around it is noise.
# Also treated as non-meaningful (skipped, not shown as a remark):
# NULL, empty string, the literal strings "nan"/"N/A" (see
# _clean_nan_string() for the same "nan" artifact on a different column),
# a bare `[]`, and a value that's nothing but a courier tracking URL
# (auto-added, not agent-written context).
_AUDIT_TRAILER_RE = re.compile(
    r'\[At\s+\d{1,2}:\d{2}\s*[AP]M\s+On\s+\d{1,2}\s+\w+\s+\d{4}\s+By\s+[^\]]*\]',
    re.IGNORECASE,
)
_WAREHOUSE_CHANGED_RE = re.compile(
    r'(?:\[\]\s*\|\s*)?Warehouse changed to (?:Warehouse|WH)\s*ID\s*\d+[^.]*\.?\s*Note:\s*',
    re.IGNORECASE,
)
_JSON_NOTE_WRAPPER_RE = re.compile(
    r'^\s*"?\[\{\\?"?note\\?"?\s*:\s*\\?"(?P<note>.*)$',
    re.IGNORECASE | re.DOTALL,
)
_REMARK_JUNK_EXACT = {'nan', 'n/a', 'na', '[]', '""', "''", '-', '.'}
_URL_ONLY_RE = re.compile(r'^https?://\S+$', re.IGNORECASE)


def _clean_remark_text(raw: str | None) -> str | None:
    """Returns the meaningful human-written part of an AdditionalNote/
    InternalNotes value, or None if the row is pure system noise. See the
    module-level comment above for the three real noise shapes this
    strips (audit trailer, warehouse-change log, malformed JSON wrapper)."""
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        text = text[1:-1].strip()
    m = _JSON_NOTE_WRAPPER_RE.match(text)
    if m:
        # The trailing `\"` (or several) is what's left of the JSON
        # wrapper's own truncated closing syntax, not part of the note.
        text = re.sub(r'\\+"?\s*$', '', m.group('note')).strip()
    text = _WAREHOUSE_CHANGED_RE.sub('', text).strip()
    text = _AUDIT_TRAILER_RE.sub('', text).strip()
    text = re.sub(r'\s+', ' ', text).strip(' \'"|.,')
    if not text:
        return None
    if text.lower() in _REMARK_JUNK_EXACT:
        return None
    if _URL_ONLY_RE.match(text):
        return None
    return text


def get_order_remarks(phone: str, limit: int = 10) -> list[dict]:
    """Latest `limit` meaningful remarks across AdditionalNote and cleaned
    InternalNotes, newest first. One TestMaster row per product line item
    repeats the same two note columns for every item on an invoice — DISTINCT
    on (Invoice, CreationDate, AdditionalNote, InternalNotes) collapses that
    back to one candidate pair per invoice before cleaning, so a 5-item
    order doesn't produce the same remark 5 times."""
    inv = _trim('Invoice')
    rows = fetch_all(
        f"""
        SELECT DISTINCT {inv} AS Invoice,
               TRY_CONVERT(date, CreationDate) AS CreationDate,
               AdditionalNote, InternalNotes
        FROM Test.dbo.TestMaster
        WHERE CustomerPhoneNumber = ?
          AND (AdditionalNote IS NOT NULL OR InternalNotes IS NOT NULL)
        ORDER BY TRY_CONVERT(date, CreationDate) DESC
        """,
        (phone,),
    )
    remarks = []
    for row in rows:
        seen_for_invoice = set()
        for raw in (row.get('AdditionalNote'), row.get('InternalNotes')):
            cleaned = _clean_remark_text(raw)
            if cleaned and cleaned not in seen_for_invoice:
                seen_for_invoice.add(cleaned)
                remarks.append({
                    'invoice': row['Invoice'],
                    'date': row['CreationDate'],
                    'note': cleaned,
                })
    remarks.sort(key=lambda r: r['date'] or dt.date.min, reverse=True)
    return remarks[:limit]


# ADDED 2026-09-08 — Delivery tab: which of a customer's remarks are
# specifically about delivery (courier behavior, call-before-delivery
# requests, address/timing complaints), not just any note. English
# keywords are wrapped in \b (word-boundary) so e.g. "call" doesn't match
# inside "recall"/"called"; Bengali keywords are plain substrings instead
# — verified live that real remarks use compound words with no space
# ("সময়মতো দাও"), where \b between সময় and মতো never fires because both
# sides are Unicode word characters, so a Bengali \b match would silently
# miss the exact kind of note this is meant to catch.
_DELIVERY_KEYWORDS_EN = ['delivery', 'deliver', 'call', 'address', 'courier', 'shipment', 'ship']
_DELIVERY_KEYWORDS_BN = ['ডেলিভারি', 'কল', 'ফোন', 'ঠিকানা', 'কুরিয়ার', 'সময়']
_DELIVERY_KEYWORD_RE = re.compile(
    '|'.join(
        [rf'\b{re.escape(k)}\b' for k in _DELIVERY_KEYWORDS_EN]
        + [re.escape(k) for k in _DELIVERY_KEYWORDS_BN]
    ),
    re.IGNORECASE,
)


def get_delivery_related_remarks(phone: str, limit: int = 20) -> list[dict]:
    """Subset of get_order_remarks() whose (already-cleaned) text mentions
    delivery — a keyword filter on top of the existing cleaning pipeline,
    not a separate query/noise-stripping pass, so this can never drift out
    of sync with what _clean_remark_text() already treats as signal vs.
    noise. Reads a much larger candidate pool (up to 1000 already-cleaned
    remarks) than it returns, since delivery mentions are a minority of
    all remarks."""
    candidates = get_order_remarks(phone, limit=1000)
    return [r for r in candidates if _DELIVERY_KEYWORD_RE.search(r['note'])][:limit]


def compute_shipping_delay_pct(orders: list[dict]) -> float | None:
    """% of this customer's orders where the courier actually shipped
    (ShippedAt) LATER than the order's recorded ShippingDate — a real
    shipping delay, distinct from get_avg_delivery_days() (which measures
    ShippedAt -> DeliveredAt, transit time after shipping already
    happened). Only counts orders where both dates are populated; None
    (not 0) when no order has both — same "unknown, not 0" convention as
    get_avg_delivery_days()/calculate_reliability_score(). Takes the
    already-grouped `orders` list (group_orders_by_invoice() output) —
    no extra query."""
    with_both = [
        o for o in orders
        if o['timeline']['shipping_date'] and o['timeline']['shipped_at']
    ]
    if not with_both:
        return None
    delayed = sum(
        1 for o in with_both
        if (o['timeline']['shipped_at'] - o['timeline']['shipping_date']).days > 0
    )
    return round(delayed / len(with_both) * 100, 1)


def has_partial_delivery(orders: list[dict]) -> bool:
    """Whether ANY of this customer's orders has the courier's own
    DeliveryPartnerStatus = 'partial_delivered' (CONFIRMED LIVE 2026-09-08
    — real distinct values are 'delivered'/'pending'/'nan'/'in_review'/
    'cancelled'/'partial_delivered'/'N/A'/'unknown', already a clean
    lowercase-snake-case vocabulary, not a guess). A customer-satisfaction
    risk signal distinct from the business `status` field. Takes the
    already-grouped `orders` list — no extra query."""
    return any(
        (o.get('delivery_partner_status') or '').strip().lower() == 'partial_delivered'
        for o in orders
    )


def get_core_metrics(phone: str) -> dict:
    """Same as handoff spec query 5.2, plus MIN(CreationDate) AS
    FirstOrderDate — needed to compute tenure for the New-customer
    badge now that its threshold is confirmed. Not a new table/column
    guess, just an extra aggregate over the same CreationDate column
    the spec's own query already reads.

    CancelRatePct/ReturnRatePct count DISTINCT invoices in the numerator
    (COUNT(DISTINCT CASE WHEN ... THEN Invoice END)), not line-item rows
    (SUM(CASE WHEN ... THEN 1 ELSE 0 END)). TestMaster is one row per
    product per invoice, so a cancelled invoice with N line items would
    otherwise count as N cancellations against the (invoice-level)
    denominator — inflating the rate past 100% on any multi-item
    cancelled order."""
    inv = _trim('Invoice')
    row = fetch_all(
        f"""
        SELECT
          COUNT(DISTINCT {inv}) AS InvoiceCount,
          SUM(TotalPrice) AS LifetimeValue,
          SUM(TotalPrice) / NULLIF(COUNT(DISTINCT {inv}), 0) AS AOV,
          COUNT(DISTINCT CASE WHEN Status IN ('Cancelled', 'Canceled') THEN {inv} END) * 100.0
            / NULLIF(COUNT(DISTINCT {inv}), 0) AS CancelRatePct,
          COUNT(DISTINCT CASE WHEN Status = 'Returned' THEN {inv} END) * 100.0
            / NULLIF(COUNT(DISTINCT {inv}), 0) AS ReturnRatePct,
          MAX(TRY_CONVERT(date, CreationDate)) AS LastOrderDate,
          MIN(TRY_CONVERT(date, CreationDate)) AS FirstOrderDate
        FROM Test.dbo.TestMaster
        WHERE CustomerPhoneNumber = ?
        """,
        (phone,),
    )
    if not row:
        return {}
    result = row[0]
    # LifetimeValue/AOV/CancelRatePct/ReturnRatePct are all numeric/money
    # results from SQL — normalize to float so derive_tier()/derive_badges()/
    # calculate_reliability_score() and this response's arithmetic never
    # mix Decimal with a Python float literal.
    for field in ('LifetimeValue', 'AOV', 'CancelRatePct', 'ReturnRatePct'):
        result[field] = _to_float(result.get(field))
    return result


def get_product_return_rates(phone: str) -> list[dict]:
    # "Bought" used to mean SUM(ProductQty) (total pieces), and the frontend
    # displayed that number under a "times bought" label — wrong for a
    # customer who orders the same product in bulk vs. one who reorders it
    # often. OrderCount (COUNT(DISTINCT Invoice)) is the real "how many
    # times did they order this" figure; BoughtQty stays separately
    # available for "how many pieces total". Ranked by OrderCount (repeat
    # purchases = popularity) with BoughtQty as a tiebreaker.
    name, sku, inv = _trim('ProductName'), _trim('ProductSKU'), _trim('Invoice')
    rows = fetch_all(
        f"""
        SELECT {name} AS ProductName, {sku} AS ProductSKU,
          COUNT(DISTINCT CASE WHEN Status NOT IN ('Cancelled','Returned') THEN {inv} END) AS OrderCount,
          SUM(CASE WHEN Status NOT IN ('Cancelled','Returned') THEN ProductQty ELSE 0 END) AS BoughtQty,
          SUM(CASE WHEN Status = 'Returned' THEN ProductQty ELSE 0 END) AS Returned
        FROM Test.dbo.TestMaster
        WHERE CustomerPhoneNumber = ?
        GROUP BY {name}, {sku}
        ORDER BY OrderCount DESC, BoughtQty DESC
        """,
        (phone,),
    )
    for row in rows:
        # ProductQty is normally an int column so BoughtQty/Returned are
        # already plain int, but SUM() over a decimal quantity column
        # would come back as Decimal — float() defensively so this
        # division can never hit the Decimal/float mixing bug either.
        bought_qty = _to_float(row['BoughtQty']) or 0.0
        returned = _to_float(row['Returned']) or 0.0
        row['ReturnRatePct'] = round(returned / bought_qty * 100, 1) if bought_qty else 0.0
    return rows


# CONFIRMED 2026-08-15 — despite the assumption previously in
# get_call_status_breakdown()/get_call_stats()'s comments that
# action='status_changed' rows never land on new_status='Assigned', real
# data shows they do (pure internal reassignment bookkeeping written
# through the same 'status_changed' action). 'Assigned' isn't a real call
# outcome an agent produced — it's routing state — so every query below
# that reads agent_assignment_history for call history/stats/breakdown
# excludes it. Deny-list (not an allow-list) on purpose: agents' real
# statuses are free text with no DB enum, so an allow-list would silently
# hide any legitimate status not anticipated up front.
NON_MEANINGFUL_CALL_STATUSES = ['Assigned']

# Auto-written placeholder remark left behind when an agent changes status
# without typing an actual note — carries no information, so it's nulled
# out here rather than surfaced as if it were a real remark.
GENERIC_REMARK_TEXT = 'Status changed by agent'


def get_call_stats(phone: str) -> dict:
    placeholders = ','.join('?' * len(NON_MEANINGFUL_CALL_STATUSES))
    row = fetch_all(
        f"""
        SELECT
          COUNT(*) AS TotalAttempts,
          SUM(CASE WHEN h.new_status NOT IN ('Unreachable','Switch Off')
                THEN 1 ELSE 0 END) * 100.0 / NULLIF(COUNT(*), 0) AS ReachRatePct,
          SUM(CASE WHEN h.new_status IN ('Order Placed','Recently Ordered')
                THEN 1 ELSE 0 END) * 100.0 / NULLIF(COUNT(*), 0) AS ConversionRatePct
        FROM dcm.dbo.agent_assignment_history h
        JOIN dcm.dbo.customer_calls cc ON cc.id = h.customer_call_id
        WHERE cc.customer_number = ? AND h.action = 'status_changed'
          AND h.new_status NOT IN ({placeholders})
        """,
        (phone, *NON_MEANINGFUL_CALL_STATUSES),
    )
    if not row:
        return {}
    result = row[0]
    # ReachRatePct/ConversionRatePct are numeric SQL results — normalize
    # to float (same Decimal/float mixing reason as get_core_metrics).
    for field in ('ReachRatePct', 'ConversionRatePct'):
        result[field] = _to_float(result.get(field))
    return result


def get_call_history_ordered(phone: str) -> list[dict]:
    """Ordered (most recent first) status-change history, used both for
    the Call History tab and to compute the unreachable streak.

    LEFT JOINs dcm.dbo.users on performed_by_id (not agent_id) for
    AgentName — verified against real agent_assignment_history rows:
    for action='status_changed' (the only action this query selects),
    performed_by_id and agent_id are always the same value, so this
    matches the agent who actually changed the status. LEFT JOIN (not
    INNER) so a row with no matching/deleted user still comes back
    with AgentName=NULL instead of silently disappearing from history."""
    placeholders = ','.join('?' * len(NON_MEANINGFUL_CALL_STATUSES))
    return fetch_all(
        f"""
        SELECT h.new_status,
               CASE WHEN LTRIM(RTRIM(h.remark)) = ? THEN NULL ELSE h.remark END AS remark,
               h.created_at, h.action, h.previous_status, h.performed_by_id,
               u.name AS AgentName
        FROM dcm.dbo.agent_assignment_history h
        JOIN dcm.dbo.customer_calls cc ON cc.id = h.customer_call_id
        LEFT JOIN dcm.dbo.users u ON u.id = h.performed_by_id
        WHERE cc.customer_number = ? AND h.action = 'status_changed'
          AND h.new_status NOT IN ({placeholders})
        ORDER BY h.created_at DESC
        """,
        (GENERIC_REMARK_TEXT, phone, *NON_MEANINGFUL_CALL_STATUSES),
    )


def get_call_status_breakdown(phone: str) -> list[dict]:
    """% and count breakdown of new_status across this customer's FULL
    status-change history (not just the last 5 shown in the Call History
    tab list) — same action='status_changed' + NON_MEANINGFUL_CALL_STATUSES
    filter as get_call_stats()/get_call_history_ordered() so 'Assigned'
    rows (routing state, not a real call outcome) don't skew it."""
    placeholders = ','.join('?' * len(NON_MEANINGFUL_CALL_STATUSES))
    rows = fetch_all(
        f"""
        SELECT h.new_status AS Status, COUNT(*) AS Cnt,
               COUNT(*) * 100.0 / SUM(COUNT(*)) OVER () AS Pct
        FROM dcm.dbo.agent_assignment_history h
        JOIN dcm.dbo.customer_calls cc ON cc.id = h.customer_call_id
        WHERE cc.customer_number = ? AND h.action = 'status_changed'
          AND h.new_status NOT IN ({placeholders})
        GROUP BY h.new_status
        ORDER BY Cnt DESC
        """,
        (phone, *NON_MEANINGFUL_CALL_STATUSES),
    )
    for row in rows:
        row['Pct'] = round(_to_float(row['Pct']), 1)
    return rows


def compute_unreachable_streak(history_desc: list[dict]) -> int:
    """Count leading rows (most-recent-first) whose new_status is an
    unreachable-type status. Computed in Python per the spec, not SQL."""
    streak = 0
    for row in history_desc:
        if row['new_status'] in UNREACHABLE_STATUSES:
            streak += 1
        else:
            break
    return streak


def get_active_call_id(phone: str) -> int | None:
    """The dcm.customer_calls row the Action Panel's Save writes to.

    CONFIRMED LIVE 2026-09-07 — customer_number is NOT unique in
    dcm.customer_calls: a phone that's been re-imported/re-assigned across
    successive distribution cycles has one row per cycle (verified up to
    14 rows for a single phone). There's no FK anywhere that names one of
    them "the" row for a customer, so "active" here means the most
    recently created cycle — the same row get_call_stats()/
    get_call_history_ordered() already treat as current via their
    customer_number join, just resolved down to a single id instead of
    aggregated across all of them. TODO: confirm with business whether a
    still-open older cycle should ever take priority over a newer one."""
    row = fetch_all(
        """
        SELECT TOP 1 id
        FROM dcm.dbo.customer_calls
        WHERE customer_number = ?
        ORDER BY created_at DESC, id DESC
        """,
        (phone,),
    )
    return row[0]['id'] if row else None


def get_agent_initiated_order_count(phone: str) -> int:
    """How many of this customer's orders were placed BY AN AGENT on a
    call, not through self-service (website/app) — dcm.dbo.order_by_agent
    is a separate table from Test.dbo.TestMaster's general order data,
    populated only when an agent confirms an order during/after a call.

    CONFIRMED LIVE 2026-09-07 — customer_number here is NOT whitespace/
    NBSP-padded like some TestMaster text columns are (verified: 0 rows
    differ between raw and LTRIM/RTRIM/CHAR(160)-cleaned length across the
    whole table), so this skips the _trim() wrapper get_order_rows() etc.
    need — added back if that ever stops holding true.

    Used for reliability_score_context's low-call-data note: a customer at
    0 here has call-response/reach metrics that don't reflect much of
    their real ordering behavior, since most of it never went through a
    call at all."""
    row = fetch_all(
        """
        SELECT COUNT(*) AS AgentOrderCount
        FROM dcm.dbo.order_by_agent
        WHERE customer_number = ? AND order_status <> 'CANCELLED'
        """,
        (phone,),
    )
    return (row[0]['AgentOrderCount'] or 0) if row else 0


def get_delivery_performance(phone: str) -> list[dict]:
    """CONFIRMED FIX 2026-08-30 — Total/SuccessRatePct previously counted
    raw TestMaster rows (one per product line item), not distinct
    invoices, unlike every sibling breakdown query below. For a real
    customer with ~5 items/order on average this inflated Total roughly
    5x (131 counted vs. 26-30 real orders) and skewed SuccessRatePct
    toward whichever invoices happen to have more line items. Now uses
    COUNT(DISTINCT ...Invoice) in both numerator and denominator, same
    pattern as get_core_metrics()'s CancelRatePct. DeliveryPartner is also
    now generically trimmed — see _trim()/get_order_rows().

    CONFIRMED FIX 2026-09-09 — GROUP BY was on the raw trimmed
    DeliveryPartner text, so 'Stead Fast' and 'Steadfast' (the same real
    courier, see _courier_case()) landed in two separate rows, each with
    its own — wrong — SuccessRatePct computed from only half the
    customer's real deliveries with that courier. Grouping in a subquery
    by the normalized label (same pattern as _get_breakdown_with_detail()
    below) fixes this AND makes this the one place pickTopDelivery() /
    the courier chart / the delivery-summary sentence on the frontend all
    read from — they all consume this same function's output already, so
    normalizing here alone fixes every one of them at once."""
    partner_label, inv = _courier_case(_trim('DeliveryPartner')), _trim('Invoice')
    rows = fetch_all(
        f"""
        SELECT DeliveryPartner,
          COUNT(DISTINCT Invoice) AS Total,
          COUNT(DISTINCT CASE WHEN Status = 'Delivered' THEN Invoice END) * 100.0
            / NULLIF(COUNT(DISTINCT Invoice), 0) AS SuccessRatePct
        FROM (
          SELECT {inv} AS Invoice, {partner_label} AS DeliveryPartner, Status
          FROM Test.dbo.TestMaster
          WHERE CustomerPhoneNumber = ? AND DeliveryPartner IS NOT NULL
        ) t
        GROUP BY DeliveryPartner
        """,
        (phone,),
    )
    # SuccessRatePct is a numeric SQL result (Decimal via pyodbc) —
    # normalize to float here so compute_overall_delivery_success_rate()
    # (and anything else summing across couriers) never mixes Decimal
    # with a plain float, which is exactly what caused the original bug.
    for row in rows:
        row['SuccessRatePct'] = _to_float(row.get('SuccessRatePct'))
    return rows


def _get_breakdown(phone: str, column: str, case_builder=None) -> list[dict]:
    """Percentage-of-invoices breakdown for a raw TestMaster column,
    optionally normalized through a CASE-mapping `case_builder` (same
    signature as _payment_method_case()/_channel_case()/
    _pickup_location_case() below) before grouping — used by
    get_pickup_location_breakdown() only; payment method and channel have
    their own richer per-category-detail helper
    (_get_breakdown_with_detail()) instead of this one."""
    inv, col = _trim('Invoice'), _trim(column)
    label_expr = case_builder(col) if case_builder else col
    rows = fetch_all(
        f"""
        SELECT Label, COUNT(*) * 100.0 / SUM(COUNT(*)) OVER () AS Pct
        FROM (
            SELECT DISTINCT {inv} AS Invoice, {label_expr} AS Label
            FROM Test.dbo.TestMaster
            WHERE CustomerPhoneNumber = ? AND {column} IS NOT NULL
              AND {col} <> ''
        ) t
        GROUP BY Label
        ORDER BY Pct DESC
        """,
        (phone,),
    )
    for row in rows:
        row['Pct'] = round(_to_float(row['Pct']), 1)
    return rows


def get_pickup_location_breakdown(phone: str) -> list[dict]:
    """Which warehouse (PickUpLocation) this customer's orders most often
    ship from, by % of invoices. CONFIRMED 2026-08-30 — 62.5% populated
    table-wide, well worth surfacing; District/SUB_DISTRICT/ThanaName were
    checked at the same time and are 0-5% populated, so those are skipped.

    CONFIRMED FIX 2026-09-09 — now runs through _pickup_location_case()
    (was ungrouped raw text before), so 'Banasree Warehouse'/'Banasree'
    no longer show as two separate rows for the same real warehouse."""
    return _get_breakdown(phone, 'PickUpLocation', _pickup_location_case)


# CONFIRMED LIVE 2026-09-09 — real DeliveryPartner has 45 distinct trimmed
# spellings; these are the ones verified to be the SAME real courier
# written multiple ways (not a guess about all 45): 'Stead Fast'
# (2,092,178 rows) + 'Steadfast' (1,979,204 rows) — together the single
# largest courier by volume, previously split into two GROUP BY buckets
# wherever DeliveryPartner was grouped raw; 'HorseECourier'/'Horse E
# Courier'/'Horse E-Courier' (3 spacing/hyphenation variants, 63,405 rows
# combined); 'GB-Express'/'GB Express' (a punctuation variant). Deliberately
# NOT merged: 'Steadfast Express'/'Steadfast Chittagong' (real, distinct
# service tiers/branches of the same company, not spelling noise) and
# 'e-courier' (a different, unrelated courier company despite the
# superficially similar name to 'Horse E-Courier'). Anything not covered
# falls through unchanged (ELSE {trimmed_col}).
def _courier_case(trimmed_col: str) -> str:
    return f"""
        CASE
          WHEN {trimmed_col} IN ('Stead Fast', 'Steadfast') THEN 'Steadfast'
          WHEN {trimmed_col} IN ('HorseECourier', 'Horse E Courier', 'Horse E-Courier') THEN 'Horse E-Courier'
          WHEN {trimmed_col} IN ('GB-Express', 'GB Express') THEN 'GB Express'
          ELSE {trimmed_col}
        END
    """


# CONFIRMED LIVE 2026-09-09 — real PickUpLocation has 11 distinct trimmed
# values; 'Banasree Warehouse' (1,414,955 rows) + 'Banasree' (567,399
# rows) are the same warehouse, same pattern for 'Chapainawabganj
# Warehouse'/'Chapainawabganj'. Deliberately NOT merged: the '(Returnable)'
# variants ('Amulia Warehouse (Returnable)', 'Chittagong (Returnable)') —
# that qualifier marks a genuinely different pickup flow (return
# processing), not a spelling variant of the plain location.
def _pickup_location_case(trimmed_col: str) -> str:
    return f"""
        CASE
          WHEN {trimmed_col} IN ('Banasree Warehouse', 'Banasree') THEN N'Banasree Warehouse'
          WHEN {trimmed_col} IN ('Chapainawabganj Warehouse', 'Chapainawabganj') THEN N'Chapainawabganj Warehouse'
          ELSE {trimmed_col}
        END
    """


# CONFIRMED 2026-08-31 — real PaymentMethod values for this table have 31
# distinct raw spellings (whitespace variants aside, already handled by
# _trim()). This collapses them into the real payment-method vocabulary the
# business actually uses; anything not covered by an explicit WHEN falls
# through to ELSE (the trimmed raw value) rather than being silently
# dropped or miscategorized.
def _payment_method_case(trimmed_col: str) -> str:
    return f"""
        CASE
          WHEN {trimmed_col} IN ('Cash On Delivery (COD)', 'COD', 'Cash On Delivery', 'COD-Partial', 'Cash') THEN 'Cash On Delivery'
          WHEN {trimmed_col} LIKE '%Bkash%' OR {trimmed_col} LIKE '%BKash%' THEN 'bKash'
          WHEN {trimmed_col} LIKE '%Nagad%' OR {trimmed_col} LIKE '%NAGAD%' THEN 'Nagad'
          WHEN {trimmed_col} IN ('SSL Commerz', 'SSLCOMMERZ') THEN 'SSLCommerz'
          WHEN {trimmed_col} IN ('Gate Pass', 'GIFT', 'Bank Payment', 'Not Paid', 'Promotional Purpose', 'Amar Pay', 'CTG Shop Sale') THEN N'অন্যান্য'
          WHEN {trimmed_col} IN ('', '7', '0', 'Method #0') THEN N'অজানা'
          ELSE {trimmed_col}
        END
    """


# CONFIRMED 2026-08-31 — OrderSource mapping as given. Case-insensitive by
# default collation (verified live: 'FACEBOOK' = 'Facebook' matches on this
# instance), so the real ALL-CAPS variants (FACEBOOK/PHONE_CALL/TELESALES)
# match these WHENs without needing separate uppercase branches. Anything
# not covered by an explicit WHEN falls through to ELSE (trimmed raw value).
def _channel_case(trimmed_col: str) -> str:
    return f"""
        CASE
          WHEN {trimmed_col} = 'DATA CALL' THEN N'ডাটা কল'
          WHEN {trimmed_col} = 'PHONE_CALL' THEN N'ফোন কল'
          WHEN {trimmed_col} = 'Facebook' THEN N'ফেসবুক'
          WHEN {trimmed_col} = 'Telesales' THEN N'টেলিসেলস'
          WHEN {trimmed_col} = 'Mobile (Web)' THEN N'মোবাইল ওয়েবসাইট'
          WHEN {trimmed_col} IN ('Website', 'Web') THEN N'ওয়েবসাইট'
          WHEN {trimmed_col} = 'WhatsApp' THEN N'হোয়াটসঅ্যাপ'
          WHEN {trimmed_col} = 'Android' THEN N'অ্যান্ড্রয়েড অ্যাপ'
          WHEN {trimmed_col} = 'Offline' THEN N'অফলাইন'
          ELSE {trimmed_col}
        END
    """


def _get_breakdown_with_detail(phone: str, column: str, case_builder) -> list[dict]:
    """Shared body for get_payment_breakdown()/get_channel_breakdown():
    normalize `column` via `case_builder`'s CASE expression, group by the
    normalized label, and compute per-category sales/invoice/qty/return
    detail in the same GROUP BY — one query, not category count + 4 more.

    Rows whose rounded percentage is 0.0 are dropped in Python (a category
    can't literally have COUNT 0 in a GROUP BY result, but a single
    invoice against a customer with hundreds can round to 0.0% at 1
    decimal place — that's the case this actually guards against)."""
    inv = _trim('Invoice')
    label_expr = case_builder(_trim(column))
    rows = fetch_all(
        f"""
        SELECT Label,
          COUNT(DISTINCT Invoice) * 100.0 / SUM(COUNT(DISTINCT Invoice)) OVER () AS Pct,
          COUNT(DISTINCT Invoice) AS TotalInvoices,
          SUM(TotalPrice) AS TotalSales,
          SUM(ProductQty) AS TotalQty,
          COUNT(DISTINCT CASE WHEN Status = 'Returned' THEN Invoice END) AS TotalReturns
        FROM (
          SELECT {inv} AS Invoice, {label_expr} AS Label, TotalPrice, ProductQty, Status
          FROM Test.dbo.TestMaster
          WHERE CustomerPhoneNumber = ?
        ) t
        WHERE Label IS NOT NULL AND Label <> ''
        GROUP BY Label
        ORDER BY Pct DESC
        """,
        (phone,),
    )
    result = []
    for row in rows:
        pct = round(_to_float(row['Pct']) or 0.0, 1)
        if pct <= 0:
            continue
        result.append({
            'Label': row['Label'],
            'Pct': pct,
            'TotalInvoices': row['TotalInvoices'] or 0,
            'TotalSales': _to_float(row['TotalSales']) or 0.0,
            'TotalQty': _to_float(row['TotalQty']) or 0.0,
            'TotalReturns': row['TotalReturns'] or 0,
        })
    return result


def get_payment_breakdown(phone: str) -> list[dict]:
    """Payment method breakdown, normalized from real raw spellings (see
    _payment_method_case()) with per-category sales/invoice/qty/return
    detail."""
    return _get_breakdown_with_detail(phone, 'PaymentMethod', _payment_method_case)


def get_channel_breakdown(phone: str) -> list[dict]:
    """Order channel breakdown, normalized from real OrderSource values
    (see _channel_case()) with per-category sales/invoice/qty/return
    detail."""
    return _get_breakdown_with_detail(phone, 'OrderSource', _channel_case)


def get_damage_rate(phone: str) -> dict:
    """CancelledReturnedDamagedQty as a % of total ProductQty ordered — how
    much of what this customer received came back damaged/cancelled/
    returned at the product-quantity level (distinct from ReturnRatePct,
    which is an invoice-level % in get_core_metrics()). CONFIRMED
    2026-08-30 — 72.5% populated table-wide."""
    row = fetch_all(
        """
        SELECT SUM(ProductQty) AS TotalQty,
               SUM(CancelledReturnedDamagedQty) AS DamagedQty
        FROM Test.dbo.TestMaster
        WHERE CustomerPhoneNumber = ?
        """,
        (phone,),
    )
    total_qty = _to_float(row[0]['TotalQty']) if row else None
    damaged_qty = _to_float(row[0]['DamagedQty']) if row else None
    total_qty = total_qty or 0.0
    damaged_qty = damaged_qty or 0.0
    return {
        'total_qty': total_qty,
        'damaged_qty': damaged_qty,
        'damage_rate_pct': round(damaged_qty / total_qty * 100, 1) if total_qty else 0.0,
    }


def get_avg_delivery_days(phone: str) -> float | None:
    """Average ShippedAt -> DeliveredAt transit time in days, across this
    customer's orders that have BOTH dates populated. Deduplicated to one
    row per invoice first (same COUNT(DISTINCT Invoice) pattern as
    get_delivery_performance()/get_core_metrics()'s CancelRatePct) so an
    order with more line items doesn't get weighted more heavily in the
    average than one with fewer — the exact bias the delivery-success-rate
    fix above already had to correct for once. Returns None (not 0) when
    no order has both dates populated, so callers can distinguish "no
    delivery-timing data yet" from "0-day average".

    CONFIRMED FIX 2026-09-01 — verified live against this SQL Server
    instance: TRY_CONVERT(date, '') returns 1900-01-01, NOT NULL (a
    documented SQL Server quirk for empty-string date conversion, distinct
    from TRY_CONVERT(date, NULL) which correctly returns NULL). Real
    TestMaster rows have DeliveredAt='' (empty string, not NULL) for
    in-transit orders — without NULLIF(...,'') first, those rows silently
    "converted" to 1900-01-01 instead of failing, producing a ~46,000-day-
    negative average (caught via a direct DB check while verifying this
    function, before it ever reached the API)."""
    inv = _trim('Invoice')
    row = fetch_all(
        f"""
        SELECT AVG(CAST(DATEDIFF(day, ShippedAtD, DeliveredAtD) AS FLOAT)) AS AvgDays
        FROM (
            SELECT DISTINCT {inv} AS Invoice,
                   TRY_CONVERT(date, NULLIF(LTRIM(RTRIM(ShippedAt)), '')) AS ShippedAtD,
                   TRY_CONVERT(date, NULLIF(LTRIM(RTRIM(DeliveredAt)), '')) AS DeliveredAtD
            FROM Test.dbo.TestMaster
            WHERE CustomerPhoneNumber = ?
        ) t
        WHERE ShippedAtD IS NOT NULL AND DeliveredAtD IS NOT NULL
        """,
        (phone,),
    )
    if not row:
        return None
    avg_days = _to_float(row[0].get('AvgDays'))
    return round(avg_days, 1) if avg_days is not None else None


def compute_overall_delivery_success_rate(delivery_rows: list[dict]) -> float | None:
    """Volume-weighted average across all couriers — equivalent to an
    ungrouped Status='Delivered' percentage over every order, just
    computed from the already-fetched per-courier rows instead of a
    second query."""
    total = sum((d.get('Total') or 0) for d in delivery_rows)
    if not total:
        return None
    # get_delivery_performance() already normalizes SuccessRatePct to
    # float, so `or 0.0` (a float fallback, not int) keeps every term in
    # this sum a plain float — no Decimal ever reaches this arithmetic.
    delivered = sum(
        (d.get('Total') or 0) * (d.get('SuccessRatePct') or 0.0) / 100
        for d in delivery_rows
    )
    return delivered / total * 100


def calculate_reliability_score(
    reach_rate_pct: float | None,
    delivery_success_rate_pct: float | None,
    cancel_rate_pct: float | None,
) -> int | None:
    """Formula: weighted average, confirmed by business (2026-08-12) -
    adjust weights if needed later.

    reliability_score = reach*0.4 + delivery_success*0.4 + (100-cancel)*0.2

    None (not 0) if any input is missing — no order/call history means
    "unknown", not "bad"."""
    if reach_rate_pct is None or delivery_success_rate_pct is None or cancel_rate_pct is None:
        return None
    # Defensive float() here too: callers already pass floats (see
    # get_call_stats/compute_overall_delivery_success_rate/get_core_metrics),
    # but this is the function that actually multiplies by the 0.4/0.2
    # float literals — a raw Decimal reaching this line is exactly what
    # raised the original TypeError, so guard at the boundary as well.
    reach_rate_pct = float(reach_rate_pct)
    delivery_success_rate_pct = float(delivery_success_rate_pct)
    cancel_rate_pct = float(cancel_rate_pct)
    score = (
        (reach_rate_pct * 0.4)
        + (delivery_success_rate_pct * 0.4)
        + ((100 - cancel_rate_pct) * 0.2)
    )
    return round(score)


# --- Derived tags (badges) ---------------------------------------------

# Thresholds as given directly in the 2026-09-01 request that introduced
# this field — not independently business-confirmed the way the tier/
# at-risk/dormant constants above are (those trace to the 2026-08-12
# handoff spec). Flagging that distinction rather than mislabeling this
# CONFIRMED like the others.
ORDER_FREQUENCY_WEEKLY_MAX_GAP_DAYS = 10
ORDER_FREQUENCY_MONTHLY_MAX_GAP_DAYS = 45


def derive_order_frequency(
    total_orders: int,
    first_order_date,
    last_order_date,
) -> tuple[str, float | None]:
    """('weekly' | 'monthly' | 'occasional' | 'none', avg_gap_days | None).

    avg_gap_days = (last_order_date - first_order_date) / (total_orders - 1)
    — needs at least 2 orders to measure a gap at all; a single-order (or
    zero-order) customer returns ('none', None) rather than a division by
    zero. Frontend maps the category code to bn/en display text + the
    "গড়ে প্রতি X দিনে" subtitle, same pattern as tier/segment/order-status
    codes elsewhere in this API (English code from the backend, i18n
    lookup client-side)."""
    if total_orders < 2 or not first_order_date or not last_order_date:
        return 'none', None
    span_days = (last_order_date - first_order_date).days
    avg_gap = span_days / (total_orders - 1)
    if avg_gap <= ORDER_FREQUENCY_WEEKLY_MAX_GAP_DAYS:
        category = 'weekly'
    elif avg_gap <= ORDER_FREQUENCY_MONTHLY_MAX_GAP_DAYS:
        category = 'monthly'
    else:
        category = 'occasional'
    return category, round(avg_gap, 1)


def derive_tier(ltv: float, aov: float) -> str:
    """gold | green | red. Gold and green LTV/AOV cutoffs confirmed
    with business (2026-08-12) — see constants above."""
    if ltv >= VIP_TIER_LTV_THRESHOLD or aov >= VIP_TIER_AOV_THRESHOLD:
        return 'gold'
    if ltv >= LOYAL_TIER_LTV_THRESHOLD:
        return 'green'
    return 'red'


def derive_badges(
    core_metrics: dict,
    call_stats: dict,
    recency_days: int | None,
    tenure_days: int | None = None,
) -> list[dict]:
    """Badge derivation from the metrics the spec's queries actually
    provide. All thresholds used here are confirmed (2026-08-12) — see
    constants above."""
    badges = []
    ltv = core_metrics.get('LifetimeValue') or 0
    aov = core_metrics.get('AOV') or 0
    invoice_count = core_metrics.get('InvoiceCount') or 0
    cancel_rate = core_metrics.get('CancelRatePct') or 0
    return_rate = core_metrics.get('ReturnRatePct') or 0
    is_vip = ltv >= VIP_TIER_LTV_THRESHOLD or aov >= VIP_TIER_AOV_THRESHOLD

    if is_vip:
        badges.append({'icon': 'star', 'color': 'gbVIP', 'label': 'VIP Customer', 'filter': None})
    elif invoice_count >= LOYAL_TIER_ORDER_COUNT_THRESHOLD or ltv >= LOYAL_TIER_LTV_THRESHOLD:
        badges.append({'icon': 'refresh-cw', 'color': 'gbTrust', 'label': 'Loyal', 'filter': None})
    if tenure_days is not None and tenure_days < NEW_CUSTOMER_DAYS_THRESHOLD:
        badges.append({'icon': 'sparkle', 'color': 'gbIdentity', 'label': 'New', 'filter': None})
    if recency_days is not None and recency_days > DORMANT_DAYS_THRESHOLD:
        badges.append({'icon': 'moon', 'color': 'gbRisk', 'label': 'Dormant', 'filter': None})
    if cancel_rate >= AT_RISK_CANCEL_RATE_PCT or return_rate >= AT_RISK_RETURN_RATE_PCT:
        badges.append({'icon': 'alert-triangle', 'color': 'gbRisk', 'label': 'At-Risk', 'filter': 'at-risk'})
    return badges
