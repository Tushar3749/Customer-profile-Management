"""
Pydantic response models.

Responsibility:
- Define response schemas for GET /api/customer/{phone},
  POST /api/customer-calls/{id}/update, and GET /api/search/recent.
- Field names/structure should mirror the frontend prototype's
  CUSTOMERS object wherever reasonably possible (exact shape to be
  confirmed per-endpoint before wiring to the database).

CONFIRMED 2026-08-12: the prototype's CUSTOMERS object stores every
dynamic string as a {bn, en} pair (name, address, courier, reason,
note, ...). Real SQL Server data (name, address, order/call fields) is
stored in a single language per row — whatever the agent/customer
typed — so the backend cannot manufacture a translation for those.
This schema returns that DB-sourced dynamic text as plain strings and
returns status/tier/segment/consent as language-neutral codes (e.g.
"delivered", "gold", "given") so the frontend's existing LABELS dict
renders the bn/en label itself. The one exception is AISummary: Gemini
genuinely generates both languages per call, so those fields carry
real _bn/_en pairs, unlike the rest of this schema.
"""

import datetime as dt
from typing import Optional

from pydantic import BaseModel

# CONFIRMED FIX 2026-08-12: `Order.date` and `Optional[datetime.date]` both
# use the bare name "date" — importing `date`/`datetime` directly (via
# `from datetime import date, datetime`) makes a field literally named
# `date` shadow the imported type during pydantic's internal annotation
# resolution (it resolves names against the class's own attributes,
# `vars(cls)`, which by then contains `date = None`). That silently turns
# `Optional[date]` into `Optional[None]` == NoneType, causing pydantic to
# reject every real date value ("Input should be None"). Importing the
# module under an alias (`dt`) and always writing `dt.date`/`dt.datetime`
# avoids the name collision entirely, for every field below and any
# future one.


class Address(BaseModel):
    text: str
    primary: bool


class Badge(BaseModel):
    icon: str
    color: str
    label: str
    filter: Optional[str] = None


class OrderItem(BaseModel):
    image: Optional[str] = None
    product_name: str
    product_sku: Optional[str] = None
    qty: int
    unit_price: float
    # CancelledReturnedDamagedQty — a per-line-item quantity, same shape as
    # qty, not a repeated invoice-level value. 0 for the ~27% of rows where
    # TestMaster doesn't populate it, not a real "zero damaged" claim.
    damaged_qty: float = 0


class OrderTimeline(BaseModel):
    """CreationDate/ShippingDate/ShippedAt/DeliveredAt — TestMaster stores
    all four as varchar, hence TRY_CONVERT(date, ...) on every one in the
    query (metrics.py get_order_rows()). Any step not yet reached is None,
    not a fabricated date — the frontend's timeline stepper reads that as
    "not there yet" rather than treating a missing step as an error."""
    order_date: Optional[dt.date] = None
    shipping_date: Optional[dt.date] = None
    shipped_at: Optional[dt.date] = None
    delivered_at: Optional[dt.date] = None


class Order(BaseModel):
    invoice: str
    status: str  # delivered | cancelled | returned | intransit
    date: Optional[dt.date] = None
    delivery_fee: float
    paid_amount: float
    due_amount: float
    courier: Optional[str] = None
    delivery_id: Optional[str] = None
    payment_method: Optional[str] = None
    cancelled_flagged_reason: Optional[str] = None
    # DeliveryPartnerStatus: the courier's OWN tracking status — a
    # separate TestMaster column from the business `status` above, and can
    # disagree with it (e.g. status='intransit' but the courier already
    # marked delivered). CONFIRMED 2026-08-30, 45% populated table-wide.
    delivery_partner_status: Optional[str] = None
    pickup_location: Optional[str] = None
    timeline: OrderTimeline
    items: list[OrderItem]


class ProductPreference(BaseModel):
    product_name: str
    product_sku: Optional[str] = None
    # How many separate orders included this product (COUNT(DISTINCT
    # Invoice)) — the "times bought" figure. Distinct from bought_qty
    # (total pieces across all those orders), which a single bulk order
    # can inflate without the product actually being a repeat purchase.
    bought_count: int
    bought_qty: int
    returned_qty: int
    return_rate_pct: float
    is_top: bool = False


class Metrics(BaseModel):
    total_orders: int
    ltv: float
    aov: float
    cancel_rate_pct: float
    return_rate_pct: float
    recency_days: Optional[int] = None
    last_order_date: Optional[dt.date] = None
    # CONFIRMED SOURCE 2026-09-01 — same MIN(CreationDate) already computed
    # in get_core_metrics() and previously used ONLY internally (for the
    # New-customer badge's tenure_days), never surfaced in the response.
    # Frontend displays this as the identity card's "গ্রাহক থেকে" (customer
    # since) field, same unformatted-ISO-date convention as last_order_date
    # above (no separate bn/en formatting today).
    first_order_date: Optional[dt.date] = None


class CallStats(BaseModel):
    total_attempts: int
    reach_rate_pct: float
    conversion_rate_pct: float
    unreachable_streak: int


class CallHistoryEntry(BaseModel):
    date: Optional[dt.datetime] = None
    type: Optional[str] = None
    status: Optional[str] = None  # positive | follow | negative
    previous_status: Optional[str] = None
    note: Optional[str] = None
    agent_name: Optional[str] = None


class DeliveryPartnerPerformance(BaseModel):
    courier: str
    total: int
    success_rate_pct: float


class DeliverySummary(BaseModel):
    """CancelledReturnedDamagedQty vs. total ProductQty ordered — see
    metrics.get_damage_rate()."""
    total_qty: float = 0
    damaged_qty: float = 0
    damage_rate_pct: float = 0
    # ADDED 2026-09-08 — metrics.compute_shipping_delay_pct(). None (not 0)
    # when no order has both ShippingDate and ShippedAt populated — see
    # that function's docstring for the "unknown, not 0" reasoning.
    delay_pct: Optional[float] = None
    # metrics.has_partial_delivery() — any order with courier status
    # 'partial_delivered'.
    has_partial_delivery: bool = False


class BreakdownItem(BaseModel):
    label: str
    pct: float


class DetailedBreakdownItem(BreakdownItem):
    """Payment method / channel breakdown — richer than the plain
    label+pct BreakdownItem (used by pickup_breakdown), with per-category
    sales/invoice/qty/return detail from the same GROUP BY. See
    metrics._get_breakdown_with_detail()."""
    total_sales: float = 0
    total_invoices: int = 0
    total_qty: float = 0
    total_returns: int = 0


class OrderRemark(BaseModel):
    """A cleaned AdditionalNote/InternalNotes value — see
    metrics.get_order_remarks()/_clean_remark_text() for what's stripped
    (audit trailers, warehouse-change system logs, malformed JSON note
    wrappers) before a row counts as a real remark. `date` is the plain
    order date (CreationDate); the frontend formats it per-language with
    the same formatCallDate() helper the Calls tab already uses, so it
    isn't pre-formatted Bengali text here."""
    invoice: str
    date: Optional[dt.date] = None
    note: str


class CallStatusBreakdownItem(BaseModel):
    status: str
    count: int
    pct: float


class AISummary(BaseModel):
    # CONFIRMED 2026-08-12 — Gemini generates both languages in one call
    # (single prompt, JSON output with _bn/_en keys), so the response
    # carries both and the frontend's language toggle picks between them
    # client-side rather than the backend picking one.
    short_bn: Optional[str] = None
    short_en: Optional[str] = None
    full_bn: list[str] = []
    full_en: list[str] = []
    call_pattern_bn: Optional[str] = None
    call_pattern_en: Optional[str] = None
    consent_signal: Optional[str] = None  # given | do_not_call | unclear
    interested_in_bn: list[str] = []
    interested_in_en: list[str] = []
    notes_count: int = 0
    generated_at: Optional[dt.datetime] = None
    is_cached: bool = True


class CustomerProfileResponse(BaseModel):
    phone: str
    # name/address: CONFIRMED 2026-08-12 (Test.dbo.TestMaster, most
    # recent order). segment/reliability_score: CONFIRMED 2026-08-12 —
    # see routers/customer.py for how each is derived.
    name: Optional[str] = None
    tier: Optional[str] = None  # gold | green | red
    segment: Optional[str] = None
    addresses: list[Address] = []
    # due_amount REMOVED 2026-09-01 — was a customer-level SUM of
    # due_amount across every order regardless of status, so a cancelled
    # order's stray due_amount data still counted toward it, misleading.
    # Per-order Order.due_amount (a single real invoice's due) is separate
    # and unaffected.
    reliability_score: Optional[int] = None
    # ADDED 2026-09-07 — transparency context for reliability_score, NOT a
    # change to the score formula itself (still reach_rate*0.4 +
    # delivery_rate*0.4 + (100-cancel_rate)*0.2, see
    # metrics.calculate_reliability_score()). Always present when
    # reliability_score is; explains what the number is actually measuring
    # so an agent doesn't read it as a general trustworthiness score.
    reliability_score_context_bn: Optional[str] = None
    reliability_score_context_en: Optional[str] = None
    # metrics.get_agent_initiated_order_count() — how many of this
    # customer's orders were confirmed by an agent on a call (dcm.dbo.
    # order_by_agent), as opposed to self-service website/app orders that
    # never generated a call. 0 here means reach/reliability data is
    # thin relative to the customer's real order volume, which is what
    # reliability_score_limited_data_note_bn/en (set only when this is 0)
    # calls out.
    agent_initiated_order_count: int = 0
    reliability_score_limited_data_note_bn: Optional[str] = None
    reliability_score_limited_data_note_en: Optional[str] = None
    # CONFIRMED SOURCE 2026-09-01 — metrics.get_avg_delivery_days()
    # (ShippedAt -> DeliveredAt, per distinct invoice). None means no order
    # has both dates populated yet, not "0 days" — frontend must render
    # that as "no data", never as a literal 0.
    avg_delivery_days: Optional[float] = None
    # ADDED 2026-09-01 — metrics.derive_order_frequency(). category is
    # 'weekly' | 'monthly' | 'occasional' | 'none' (English code, same
    # backend-code + frontend-i18n-lookup pattern as tier/segment/order
    # status); average_order_gap_days is None when there are fewer than 2
    # orders to measure a gap between.
    order_frequency_category: Optional[str] = None
    average_order_gap_days: Optional[float] = None
    # ADDED 2026-09-07 — metrics.get_active_call_id(). The dcm.customer_calls
    # row id the frontend's Action Panel PATCHes via POST
    # /customer-calls/{id}/update; None only when a phone has no
    # customer_calls row at all (order-only customer, never call-assigned).
    active_call_id: Optional[int] = None
    badges: list[Badge] = []
    metrics: Metrics
    orders: list[Order]
    order_remarks: list[OrderRemark] = []
    # ADDED 2026-09-08 — metrics.get_delivery_related_remarks(): the subset
    # of order_remarks above whose text specifically mentions delivery
    # (courier, call-before-delivery, address, timing). Same OrderRemark
    # shape, a filtered view rather than a separate source.
    delivery_related_remarks: list[OrderRemark] = []
    products: list[ProductPreference]
    call_stats: CallStats
    calls: list[CallHistoryEntry]
    call_status_breakdown: list[CallStatusBreakdownItem] = []
    delivery: list[DeliveryPartnerPerformance]
    delivery_summary: DeliverySummary
    pickup_breakdown: list[BreakdownItem] = []
    channel_breakdown: list[DetailedBreakdownItem] = []
    payment_breakdown: list[DetailedBreakdownItem] = []
    ai_summary: AISummary


class CallUpdateRequest(BaseModel):
    status: str
    remark: Optional[str] = None
    # Optional reassignment — dcm.customer_calls.previous_agent_id exists
    # specifically to track this, so a change here is a real, supported
    # write path, not a guess.
    assigned_agent_id: Optional[int] = None
    # CONFIRMED 2026-08-12 — dcm.customer_calls.next_followup_date added
    # via migrations/003_add_next_followup_date.sql.
    next_followup_date: Optional[dt.date] = None


class CallUpdateResponse(BaseModel):
    customer_call_id: int
    status: str
    remark: Optional[str] = None
    previous_status: Optional[str] = None
    assigned_agent_id: Optional[int] = None
    previous_agent_id: Optional[int] = None
    next_followup_date: Optional[dt.date] = None
    performed_by_id: int
    updated_at: dt.datetime


class RecentSearchItem(BaseModel):
    phone: str
    name: Optional[str] = None
    searched_at: dt.datetime
