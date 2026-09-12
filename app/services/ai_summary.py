"""
Gemini AI summary generation + cache table access.

Responsibility:
- Read cached summary fields from dcm.dbo.dashboard_ai_summary for the
  live customer search path (never calls Gemini synchronously here).
- Build the Gemini prompt input from note/remark sources and call the
  Gemini 2.5 Flash API (used only by the background job / manual
  refresh endpoint).
- Upsert results into dashboard_ai_summary.

CONFIRMED 2026-08-12: the Gemini system prompt below (SYSTEM_PROMPT) is
the finalized version supplied by the user — not a placeholder. It asks
Gemini to produce both bn/en variants of every field in one call, so
the background job makes exactly one API call per customer, not two.
"""

import hashlib
import json
import re
from typing import Optional

import google.generativeai as genai

from app.config import settings
from app.db import execute, fetch_one

GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_TEMPERATURE = 0.25

# Staff-initial tags like [SK], [RS], [FN], [NH] to strip before sending
# text to the model and before it's allowed to appear in any output.
STAFF_TAG_PATTERN = re.compile(r"\[[A-Za-z]{1,4}\]")

SYSTEM_PROMPT = """Tumi GhorerBazar-er ekta customer-service AI assistant. Tomake customer-er call notes ebong order remarks deya hobe (Bangla, English, othoba Banglish mixed thakte pare) — tumi eigulo poRe ekta concise summary banabe agent-der jonno, jate ora quickly bujhte pare customer-er sathe age ki hoyeche.

Output MUST be valid JSON, ei exact structure-e:
{
  "summary_short_bn": "<1-2 line bangla summary>",
  "summary_short_en": "<1-2 line english summary>",
  "summary_full_bn": ["<bangla timeline point 1>", "<point 2>", ...],
  "summary_full_en": ["<english timeline point 1>", "<point 2>", ...],
  "call_pattern_bn": "<bangla e call behavior pattern, jemon 'shokal e call dhorena, bikele dhore'>",
  "call_pattern_en": "<english version>",
  "consent_signal": "given" | "do_not_call" | "unclear",
  "interested_in_bn": ["<product/category interest bangla te>"],
  "interested_in_en": ["<same in english>"]
}

Rules:
- Notes-e "[]" thakle seta empty/ignore koro, real content na.
- [SK], [RS], [FN] style staff-initial tag gulo ignore koro, output-e kokhono dio na.
- Punctuation-only ba single-character remark (jemon "v", "...........") - eigulo ignore koro, real content na.
- Consent signal: customer jodi explicitly bole "call koro na" / "don't call" - eta "do_not_call". Customer positive response dile "given". Na bujha gele "unclear".
- Objective thako, judgmental language use koro na.
- Output shudhu valid JSON, kono extra text/markdown formatting chara."""


def get_cached_summary(phone: str) -> Optional[dict]:
    """Read-only lookup for the live search path. Never calls Gemini."""
    row = fetch_one(
        """
        SELECT customer_phone, notes_count, notes_source_hash,
               summary_short_bn, summary_short_en,
               summary_full_bn, summary_full_en, call_pattern_bn, call_pattern_en,
               consent_signal, interested_in_bn, interested_in_en, generated_at
        FROM dcm.dbo.dashboard_ai_summary
        WHERE customer_phone = ?
        """,
        (phone,),
    )
    if row is None:
        return None
    for field in ('summary_full_bn', 'summary_full_en', 'interested_in_bn', 'interested_in_en'):
        row[field] = json.loads(row[field]) if row[field] else []
    return row


def compute_notes_hash(note_texts: list[str]) -> bytes:
    """Hash of all concatenated note text used to build the last summary.
    The background job recomputes this before calling Gemini; if
    unchanged, it skips the API call entirely."""
    joined = "\n".join(note_texts)
    return hashlib.sha256(joined.encode("utf-8")).digest()


def is_meaningless_remark(text: str) -> bool:
    """Skip remarks that are just punctuation, a single character, a bare
    date, or otherwise carry no real sentence content."""
    stripped = text.strip()
    if len(stripped) <= 1:
        return True
    if not re.search(r"[A-Za-zঀ-৿]", stripped):
        # no Latin or Bangla letters at all (dates, punctuation-only, etc.)
        return True
    return False


def clean_note_text(text: Optional[str]) -> Optional[str]:
    """Apply the data-cleaning rules from CLAUDE_CODE_PROMPT.md before a
    piece of note/remark text is allowed into the Gemini prompt input."""
    if text is None:
        return None
    if text.strip() == "[]":
        return None
    cleaned = STAFF_TAG_PATTERN.sub("", text).strip()
    if not cleaned or is_meaningless_remark(cleaned):
        return None
    return cleaned


def upsert_summary(phone: str, notes_count: int, notes_source_hash: bytes, fields: dict) -> None:
    """Upsert into dashboard_ai_summary. Called only from the background
    job / manual refresh endpoint — never from the live search path."""
    execute(
        """
        MERGE dcm.dbo.dashboard_ai_summary AS target
        USING (SELECT ? AS customer_phone) AS src
        ON target.customer_phone = src.customer_phone
        WHEN MATCHED THEN UPDATE SET
            notes_count = ?, notes_source_hash = ?,
            summary_short_bn = ?, summary_short_en = ?,
            summary_full_bn = ?, summary_full_en = ?,
            call_pattern_bn = ?, call_pattern_en = ?,
            consent_signal = ?, interested_in_bn = ?, interested_in_en = ?,
            generated_at = SYSUTCDATETIME()
        WHEN NOT MATCHED THEN INSERT
            (customer_phone, notes_count, notes_source_hash,
             summary_short_bn, summary_short_en, summary_full_bn, summary_full_en,
             call_pattern_bn, call_pattern_en, consent_signal,
             interested_in_bn, interested_in_en, generated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, SYSUTCDATETIME());
        """,
        (
            phone,
            notes_count, notes_source_hash,
            fields.get('summary_short_bn'), fields.get('summary_short_en'),
            json.dumps(fields.get('summary_full_bn', []), ensure_ascii=False),
            json.dumps(fields.get('summary_full_en', []), ensure_ascii=False),
            fields.get('call_pattern_bn'), fields.get('call_pattern_en'),
            fields.get('consent_signal'),
            json.dumps(fields.get('interested_in_bn', []), ensure_ascii=False),
            json.dumps(fields.get('interested_in_en', []), ensure_ascii=False),
            phone,
            notes_count, notes_source_hash,
            fields.get('summary_short_bn'), fields.get('summary_short_en'),
            json.dumps(fields.get('summary_full_bn', []), ensure_ascii=False),
            json.dumps(fields.get('summary_full_en', []), ensure_ascii=False),
            fields.get('call_pattern_bn'), fields.get('call_pattern_en'),
            fields.get('consent_signal'),
            json.dumps(fields.get('interested_in_bn', []), ensure_ascii=False),
            json.dumps(fields.get('interested_in_en', []), ensure_ascii=False),
        ),
    )


def _build_user_message(note_texts: list[str]) -> str:
    """No separate user-prompt template was supplied alongside
    SYSTEM_PROMPT — only the system instruction was finalized. This is
    a minimal, direct wrapper around the cleaned notes rather than an
    invented template; replace if a specific user-prompt format is
    given later."""
    bullets = "\n".join(f"- {t}" for t in note_texts)
    return f"Customer notes/remarks (cleaned, chronological order not guaranteed):\n{bullets}"


def call_gemini(prompt_input: dict) -> dict:
    """Call Gemini 2.5 Flash with SYSTEM_PROMPT, temperature 0.25,
    response_mime_type=application/json (per handoff spec section 8).
    One call produces both bn and en fields."""
    genai.configure(api_key=settings.gemini_api_key)
    model = genai.GenerativeModel(
        model_name=GEMINI_MODEL,
        system_instruction=SYSTEM_PROMPT,
        generation_config={
            "temperature": GEMINI_TEMPERATURE,
            "response_mime_type": "application/json",
        },
    )
    user_message = _build_user_message(prompt_input.get('notes', []))
    response = model.generate_content(user_message)
    return json.loads(response.text)
