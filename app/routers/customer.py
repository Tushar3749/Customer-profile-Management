"""
/api/customer/{phone} and sub-routes.

Responsibility:
- Accept a phone number in any common input format, normalize it via
  services.phone.normalize_phone().
- Call services.metrics for orders, core metrics, product return rates,
  call stats, and delivery performance.
- Call services.ai_summary to read the cached AI summary fields.
- Assemble and return one consolidated JSON payload matching the
  frontend prototype's CUSTOMERS object shape.
"""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, status

from app.auth import get_current_agent
from app.db import execute
from app.models.schemas import CustomerProfileResponse
from app.services import ai_summary, metrics
from app.services.phone import normalize_phone

router = APIRouter(tags=["customer"])


def _days_since(d: date | None) -> int | None:
    return (date.today() - d).days if d else None


@router.get("/customer/{phone}", response_model=CustomerProfileResponse)
def get_customer_profile(phone: str, agent: dict = Depends(get_current_agent)):
    normalized = normalize_phone(phone)

    # CONFIRMED 2026-08-12 — side-effect logging for GET /api/search/recent.
    execute(
        "INSERT INTO dcm.dbo.search_history (agent_id, searched_phone) VALUES (?, ?)",
        (agent['id'], normalized),
    )

    order_rows = metrics.get_order_rows(normalized)
    orders = metrics.group_orders_by_invoice(order_rows)
    identity = metrics.get_current_identity(order_rows)

    core = metrics.get_core_metrics(normalized)
    order_remarks = metrics.get_order_remarks(normalized)
    products = metrics.get_product_return_rates(normalized)

    call_stats_row = metrics.get_call_stats(normalized)
    call_history = metrics.get_call_history_ordered(normalized)

    # Neither the order-history queries nor the call-history queries
    # returned anything at all for this phone — nothing to show, so this
    # is a genuine "customer not found" rather than a real 0-order/0-call
    # profile with no data source. (Previously this endpoint always
    # returned 200 with a mostly-empty payload, which broke the
    # frontend's "customer not found" error state.)
    if not order_rows and not call_history:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")

    unreachable_streak = metrics.compute_unreachable_streak(call_history)

    delivery_rows = metrics.get_delivery_performance(normalized)
    pickup_rows = metrics.get_pickup_location_breakdown(normalized)
    damage_rate = metrics.get_damage_rate(normalized)
    channel_rows = metrics.get_channel_breakdown(normalized)
    payment_rows = metrics.get_payment_breakdown(normalized)
    call_status_rows = metrics.get_call_status_breakdown(normalized)

    delivery_related_remarks = metrics.get_delivery_related_remarks(normalized)
    # Both computed from the already-grouped `orders` list above — no
    # extra query for either.
    damage_rate['delay_pct'] = metrics.compute_shipping_delay_pct(orders)
    damage_rate['has_partial_delivery'] = metrics.has_partial_delivery(orders)

    recency_days = _days_since(core.get('LastOrderDate'))
    tenure_days = _days_since(core.get('FirstOrderDate'))

    badges = metrics.derive_badges(core, call_stats_row, recency_days, tenure_days)
    tier = metrics.derive_tier(core.get('LifetimeValue') or 0, core.get('AOV') or 0)

    # CONFIRMED 2026-08-12 — segment is just the highest-priority badge;
    # derive_badges() already appends VIP/Loyal before New/Dormant/At-Risk,
    # so badges[0] is the priority pick with no separate logic needed.
    segment = badges[0]['label'] if badges else None

    avg_delivery_days = metrics.get_avg_delivery_days(normalized)
    delivery_success_rate_pct = metrics.compute_overall_delivery_success_rate(delivery_rows)
    reliability_score = metrics.calculate_reliability_score(
        call_stats_row.get('ReachRatePct'),
        delivery_success_rate_pct,
        core.get('CancelRatePct'),
    )

    order_frequency_category, avg_order_gap_days = metrics.derive_order_frequency(
        core.get('InvoiceCount') or 0, core.get('FirstOrderDate'), core.get('LastOrderDate'),
    )

    cached_summary = ai_summary.get_cached_summary(normalized)
    active_call_id = metrics.get_active_call_id(normalized)

    agent_initiated_order_count = metrics.get_agent_initiated_order_count(normalized)
    # CONFIRMED 2026-09-07 — context text only, the reliability_score
    # formula above is untouched. The limited-data note is additive
    # (shown alongside, not instead of, the base context) and only fires
    # when this customer has literally zero agent-confirmed orders, i.e.
    # the reach/reliability numbers above are computed from a call history
    # that doesn't reflect most (or any) of their real ordering.
    reliability_score_limited_data_note_bn = None
    reliability_score_limited_data_note_en = None
    if agent_initiated_order_count == 0:
        reliability_score_limited_data_note_bn = (
            'এই গ্রাহক মূলত সরাসরি ওয়েবসাইট/অ্যাপ থেকে অর্ডার করেন, '
            'কল-ভিত্তিক ডেটা সীমিত হতে পারে।'
        )
        reliability_score_limited_data_note_en = (
            'This customer mostly orders directly via website/app — '
            'call-based data may be limited.'
        )

    return CustomerProfileResponse(
        phone=normalized,
        # name/address: CONFIRMED 2026-08-12 — Test.dbo.TestMaster
        # .CustomerName/.CustomerAddress from the most recent order only.
        name=identity['name'],
        addresses=(
            [{'text': identity['address'], 'primary': True}]
            if identity['address'] else []
        ),
        # segment/reliability_score: CONFIRMED 2026-08-12 — see the
        # computations above. due_amount (customer-level aggregate) was
        # removed 2026-09-01 — it summed due_amount across ALL orders
        # regardless of status, so a cancelled order's stray due_amount
        # data still counted toward it, misleadingly. Per-order due_amount
        # (Order.due_amount, still real/accurate for a single invoice) is
        # untouched.
        segment=segment,
        reliability_score=reliability_score,
        reliability_score_context_bn='এই স্কোর মূলত কল-রেসপন্স ও ডেলিভারি সাফল্যের ভিত্তিতে হিসাব করা।',
        reliability_score_context_en='This score is calculated mainly from call response and delivery success.',
        agent_initiated_order_count=agent_initiated_order_count,
        reliability_score_limited_data_note_bn=reliability_score_limited_data_note_bn,
        reliability_score_limited_data_note_en=reliability_score_limited_data_note_en,
        avg_delivery_days=avg_delivery_days,
        order_frequency_category=order_frequency_category,
        average_order_gap_days=avg_order_gap_days,
        active_call_id=active_call_id,
        tier=tier,
        badges=badges,
        metrics={
            'total_orders': core.get('InvoiceCount') or 0,
            'ltv': core.get('LifetimeValue') or 0,
            'aov': core.get('AOV') or 0,
            # Rounded here at the response boundary, not inside
            # get_core_metrics() itself — derive_badges()'s At-Risk
            # threshold check and calculate_reliability_score() both read
            # `core`'s CancelRatePct/ReturnRatePct at full precision above;
            # rounding at the source would let e.g. a real 29.96% round up
            # to 30.0 and wrongly cross the >=30 At-Risk cutoff.
            'cancel_rate_pct': round(core.get('CancelRatePct') or 0, 1),
            'return_rate_pct': round(core.get('ReturnRatePct') or 0, 1),
            'recency_days': recency_days,
            'last_order_date': core.get('LastOrderDate'),
            'first_order_date': core.get('FirstOrderDate'),
        },
        orders=[
            {
                'invoice': o['invoice'],
                'status': metrics.normalize_order_status(o['status']),
                'date': o['date'],
                'delivery_fee': o['delivery_fee'] or 0,
                'paid_amount': o['paid_amount'] or 0,
                'due_amount': o['due_amount'] or 0,
                'courier': o['courier'],
                'delivery_id': o['delivery_id'],
                'payment_method': o['payment_method'],
                'cancelled_flagged_reason': o['cancelled_flagged_reason'],
                'delivery_partner_status': o['delivery_partner_status'],
                'pickup_location': o['pickup_location'],
                'timeline': o['timeline'],
                'items': o['items'],
            }
            for o in orders
        ],
        order_remarks=[
            {'invoice': r['invoice'], 'date': r['date'], 'note': r['note']}
            for r in order_remarks
        ],
        delivery_related_remarks=[
            {'invoice': r['invoice'], 'date': r['date'], 'note': r['note']}
            for r in delivery_related_remarks
        ],
        products=[
            {
                'product_name': p['ProductName'],
                'product_sku': p['ProductSKU'],
                'bought_count': p['OrderCount'] or 0,
                'bought_qty': p['BoughtQty'] or 0,
                'returned_qty': p['Returned'] or 0,
                'return_rate_pct': p['ReturnRatePct'],
            }
            for p in products
        ],
        call_stats={
            'total_attempts': call_stats_row.get('TotalAttempts') or 0,
            'reach_rate_pct': call_stats_row.get('ReachRatePct') or 0,
            'conversion_rate_pct': call_stats_row.get('ConversionRatePct') or 0,
            'unreachable_streak': unreachable_streak,
        },
        calls=[
            {
                'date': c['created_at'],
                'type': c['action'],
                'status': c['new_status'],
                'previous_status': c['previous_status'],
                'note': c['remark'],
                'agent_name': c.get('AgentName'),
            }
            for c in call_history
        ],
        call_status_breakdown=[
            {'status': r['Status'], 'count': r['Cnt'], 'pct': r['Pct']}
            for r in call_status_rows
        ],
        delivery=[
            {
                'courier': d['DeliveryPartner'] or '',
                'total': d['Total'] or 0,
                'success_rate_pct': d['SuccessRatePct'] or 0,
            }
            for d in delivery_rows
        ],
        delivery_summary=damage_rate,
        pickup_breakdown=[
            {'label': r['Label'], 'pct': r['Pct']} for r in pickup_rows
        ],
        channel_breakdown=[
            {
                'label': r['Label'], 'pct': r['Pct'],
                'total_sales': r['TotalSales'], 'total_invoices': r['TotalInvoices'],
                'total_qty': r['TotalQty'], 'total_returns': r['TotalReturns'],
            }
            for r in channel_rows
        ],
        payment_breakdown=[
            {
                'label': r['Label'], 'pct': r['Pct'],
                'total_sales': r['TotalSales'], 'total_invoices': r['TotalInvoices'],
                'total_qty': r['TotalQty'], 'total_returns': r['TotalReturns'],
            }
            for r in payment_rows
        ],
        ai_summary={
            'short_bn': (cached_summary or {}).get('summary_short_bn'),
            'short_en': (cached_summary or {}).get('summary_short_en'),
            'full_bn': (cached_summary or {}).get('summary_full_bn', []),
            'full_en': (cached_summary or {}).get('summary_full_en', []),
            'call_pattern_bn': (cached_summary or {}).get('call_pattern_bn'),
            'call_pattern_en': (cached_summary or {}).get('call_pattern_en'),
            'consent_signal': (cached_summary or {}).get('consent_signal'),
            'interested_in_bn': (cached_summary or {}).get('interested_in_bn', []),
            'interested_in_en': (cached_summary or {}).get('interested_in_en', []),
            'notes_count': (cached_summary or {}).get('notes_count', 0),
            'generated_at': (cached_summary or {}).get('generated_at'),
        },
    )
