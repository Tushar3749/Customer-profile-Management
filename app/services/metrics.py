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
    DeliveryPartner, PaymentMethod, OrderSource, PickUpLocation, ...)."""
    return f"LTRIM(RTRIM(REPLACE(REPLACE(REPLACE({column}, CHAR(9), ' '), CHAR(13), ' '), CHAR(10), ' ')))"


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
    tab work for why that one was skipped."""
    return fetch_all(
        f"""
        SELECT {_trim('Invoice')} AS Invoice,
               TRY_CONVERT(date, CreationDate) AS CreationDate,
               TRY_CONVERT(date, ShippingDate) AS ShippingDate,
               TRY_CONVERT(date, ShippedAt) AS ShippedAt,
               TRY_CONVERT(date, DeliveredAt) AS DeliveredAt, Status,
               {_trim('ProductName')} AS ProductName,
               {_trim('ProductSKU')} AS ProductSKU,
               ProductQty, UnitPrice, TotalPrice,
               DeliveryFee, PaymentsPaidAmount, DueAmount,
               {_trim('DeliveryPartner')} AS DeliveryPartner,
               DeliveryID,
               {_trim('PaymentMethod')} AS PaymentMethod,
               CancelledFlaggedReason,
               {_trim('DeliveryPartnerStatus')} AS DeliveryPartnerStatus,
               {_trim('PickUpLocation')} AS PickUpLocation,
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


def get_delivery_performance(phone: str) -> list[dict]:
    """CONFIRMED FIX 2026-08-30 — Total/SuccessRatePct previously counted
    raw TestMaster rows (one per product line item), not distinct
    invoices, unlike every sibling breakdown query below. For a real
    customer with ~5 items/order on average this inflated Total roughly
    5x (131 counted vs. 26-30 real orders) and skewed SuccessRatePct
    toward whichever invoices happen to have more line items. Now uses
    COUNT(DISTINCT ...Invoice) in both numerator and denominator, same
    pattern as get_core_metrics()'s CancelRatePct. DeliveryPartner is also
    now generically trimmed — see _trim()/get_order_rows()."""
    partner, inv = _trim('DeliveryPartner'), _trim('Invoice')
    rows = fetch_all(
        f"""
        SELECT {partner} AS DeliveryPartner,
          COUNT(DISTINCT {inv}) AS Total,
          COUNT(DISTINCT CASE WHEN Status = 'Delivered' THEN {inv} END) * 100.0
            / NULLIF(COUNT(DISTINCT {inv}), 0) AS SuccessRatePct
        FROM Test.dbo.TestMaster
        WHERE CustomerPhoneNumber = ? AND DeliveryPartner IS NOT NULL
        GROUP BY {partner}
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


def _get_breakdown(phone: str, column: str) -> list[dict]:
    """Shared implementation for channel/payment/pickup-location breakdown:
    percentage of this customer's DISTINCT invoices per value of `column`.
    Grouped from a DISTINCT (Invoice, column) subquery so a multi-line-item
    invoice contributes once, not once per item — same double-counting
    hazard as the CancelRatePct bug. Invoice and the target column are both
    generically trimmed (see _trim()/get_order_rows()) so a whitespace-only
    variant of the same invoice/value doesn't fragment the percentages."""
    inv, col = _trim('Invoice'), _trim(column)
    rows = fetch_all(
        f"""
        SELECT Label, COUNT(*) * 100.0 / SUM(COUNT(*)) OVER () AS Pct
        FROM (
            SELECT DISTINCT {inv} AS Invoice, {col} AS Label
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


def get_channel_breakdown(phone: str) -> list[dict]:
    """Order channel (OrderSource column) breakdown by % of invoices."""
    return _get_breakdown(phone, 'OrderSource')


def get_payment_breakdown(phone: str) -> list[dict]:
    """Payment method breakdown by % of invoices."""
    return _get_breakdown(phone, 'PaymentMethod')


def get_pickup_location_breakdown(phone: str) -> list[dict]:
    """Which warehouse (PickUpLocation) this customer's orders most often
    ship from, by % of invoices. CONFIRMED 2026-08-30 — 62.5% populated
    table-wide, well worth surfacing; District/SUB_DISTRICT/ThanaName were
    checked at the same time and are 0-5% populated, so those are skipped."""
    return _get_breakdown(phone, 'PickUpLocation')


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
