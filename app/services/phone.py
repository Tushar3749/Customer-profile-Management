"""
Phone number normalization.

Responsibility:
- normalize_phone(raw): convert any common input format (with/without
  +880, with/without leading 0) to the 880-prefixed digit-only form.
- Must mirror the frontend's normalizePhone() JS function exactly so
  backend lookups agree with what the UI sends.
"""


def normalize_phone(raw: str) -> str:
    digits = ''.join(ch for ch in raw if ch.isdigit())
    if digits.startswith('880'):
        return digits
    if digits.startswith('0'):
        return '880' + digits[1:]
    if len(digits) == 10:
        return '880' + digits
    return digits
