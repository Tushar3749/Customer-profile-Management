import re


def normalize_phone(raw: str) -> str:
    """'01973070917' / '+8801973070917' / '8801973070917' -> '1973070917' (core digits)."""
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("880"):
        return digits[3:]
    if digits.startswith("0"):
        return digits[1:]
    return digits
