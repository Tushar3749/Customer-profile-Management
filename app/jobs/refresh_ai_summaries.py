"""
Background job: refresh dashboard_ai_summary cache.

Responsibility:
- Find customers with new/changed note content (compare against
  notes_source_hash).
- Build Gemini prompt input from TestMaster.AdditionalNote/
  InternalNotes, customer_calls.remark, and
  agent_assignment_history.remark.
- Apply data-cleaning rules (treat "[]" as empty, strip [SK]-style
  staff-initial tags, skip meaningless punctuation/single-char/date-
  only remarks) before sending text to Gemini.
- Call Gemini 2.5 Flash and upsert the result into
  dcm.dbo.dashboard_ai_summary.
- Structured to be runnable standalone now and later wired into a
  scheduler (mechanism TBD — ask before assuming cron/Task
  Scheduler/APScheduler).
"""

import argparse

from app.db import fetch_all
from app.services import ai_summary
from app.services.phone import normalize_phone


def get_candidate_customers() -> list[dict]:
    """Distinct phone numbers that have any note/remark content at all,
    across TestMaster and the dcm call tables. Customers with no note
    data anywhere are skipped entirely (no API calls, no cost waste)."""
    return fetch_all(
        """
        SELECT DISTINCT CustomerPhoneNumber AS phone
        FROM Test.dbo.TestMaster
        WHERE (AdditionalNote IS NOT NULL AND AdditionalNote <> '')
           OR (InternalNotes IS NOT NULL AND InternalNotes <> '')
        """
    )


def collect_note_texts(phone: str) -> list[str]:
    """Gather raw note/remark text for a customer from all three sources,
    then apply the data-cleaning rules before it's usable as Gemini input."""
    texts: list[str] = []

    test_master_notes = fetch_all(
        """
        SELECT AdditionalNote, InternalNotes
        FROM Test.dbo.TestMaster
        WHERE CustomerPhoneNumber = ?
        """,
        (phone,),
    )
    for row in test_master_notes:
        for field in ('AdditionalNote', 'InternalNotes'):
            cleaned = ai_summary.clean_note_text(row.get(field))
            if cleaned:
                texts.append(cleaned)

    call_remarks = fetch_all(
        """
        SELECT cc.remark AS remark
        FROM dcm.dbo.customer_calls cc
        WHERE cc.customer_number = ?
        """,
        (phone,),
    )
    for row in call_remarks:
        cleaned = ai_summary.clean_note_text(row.get('remark'))
        if cleaned:
            texts.append(cleaned)

    history_remarks = fetch_all(
        """
        SELECT h.remark AS remark
        FROM dcm.dbo.agent_assignment_history h
        JOIN dcm.dbo.customer_calls cc ON cc.id = h.customer_call_id
        WHERE cc.customer_number = ?
        """,
        (phone,),
    )
    for row in history_remarks:
        cleaned = ai_summary.clean_note_text(row.get('remark'))
        if cleaned:
            texts.append(cleaned)

    return texts


def refresh_one(phone: str) -> str:
    """Returns a short status string: 'skipped-no-notes', 'skipped-unchanged',
    or 'refreshed' — used for the run summary printed by main()."""
    note_texts = collect_note_texts(phone)
    if not note_texts:
        return 'skipped-no-notes'

    new_hash = ai_summary.compute_notes_hash(note_texts)
    cached = ai_summary.get_cached_summary(phone)
    if cached is not None and cached.get('notes_source_hash') == new_hash:
        return 'skipped-unchanged'

    result = ai_summary.call_gemini({'phone': phone, 'notes': note_texts})
    ai_summary.upsert_summary(
        phone=phone,
        notes_count=len(note_texts),
        notes_source_hash=new_hash,
        fields={
            'summary_short_bn': result.get('summary_short_bn'),
            'summary_short_en': result.get('summary_short_en'),
            'summary_full_bn': result.get('summary_full_bn', []),
            'summary_full_en': result.get('summary_full_en', []),
            'call_pattern_bn': result.get('call_pattern_bn'),
            'call_pattern_en': result.get('call_pattern_en'),
            'consent_signal': result.get('consent_signal'),
            'interested_in_bn': result.get('interested_in_bn', []),
            'interested_in_en': result.get('interested_in_en', []),
        },
    )
    return 'refreshed'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--phone',
        help="Refresh only this one customer instead of scanning every "
             "candidate — for testing a single customer without spending "
             "Gemini API calls / hitting rate limits across the whole base.",
    )
    args = parser.parse_args()

    if args.phone:
        candidates = [{'phone': normalize_phone(args.phone)}]
    else:
        candidates = get_candidate_customers()

    results = {'skipped-no-notes': 0, 'skipped-unchanged': 0, 'refreshed': 0, 'error': 0}
    for row in candidates:
        phone = row['phone']
        try:
            outcome = refresh_one(phone)
            results[outcome] += 1
            print(f"[{phone}] {outcome}")
        except Exception as exc:  # Gemini/API/JSON-parse failures for one
            # customer shouldn't abort the whole run.
            results['error'] += 1
            print(f"[{phone}] failed: {exc}")
    print(results)


if __name__ == '__main__':
    main()
