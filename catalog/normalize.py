"""
normalize.py
------------
Small, deterministic helpers that turn messy spreadsheet text into clean values.
Every importer uses these, so the same input is always cleaned the same way.
"""

from __future__ import annotations

import re
import unicodedata
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Dict, Optional, Tuple

CENT = Decimal("0.01")

# Text that was saved as UTF-8 and re-read as Windows-1252 ("â€\x9d" instead of a closing quote).
_MOJIBAKE_MARKERS = ("â€", "Ã", "Â")
_QUOTES = {
    "“": '"', "”": '"', "″": '"', "ʺ": '"',
    "‘": "'", "’": "'", "′": "'",
    "–": "-", "—": "-", "−": "-", " ": " ",
}


def fix_mojibake(text: str) -> str:
    if not any(m in text for m in _MOJIBAKE_MARKERS):
        return text
    try:
        return text.encode("cp1252", errors="strict").decode("utf-8", errors="strict")
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    # Partially broken text: repair the common sequences one by one.
    for bad, good in (("â€\x9d", '"'), ("â€œ", '"'), ("â€™", "'"), ("â€˜", "'"), ("â€“", "-"),
                      ("â€”", "-"), ("Â", "")):
        text = text.replace(bad, good)
    return text


def clean_text(value: Any) -> str:
    """Readable single-line text: mojibake fixed, full-width chars folded (NFKC), straight quotes,
    two apostrophes ('') read as inches ("), whitespace collapsed."""
    if value is None:
        return ""
    text = fix_mojibake(str(value))
    text = unicodedata.normalize("NFKC", text)
    for bad, good in _QUOTES.items():
        text = text.replace(bad, good)
    text = text.replace("''", '"')
    return re.sub(r"\s+", " ", text).strip()


def match_key(code: Any) -> str:
    """Lookup key for a product code: upper case, letters and digits only.
    'TK8 1/4"' -> 'TK814', 'pl-100 alpina' -> 'PL100ALPINA', 'W2136' -> 'W2136'."""
    return re.sub(r"[^A-Z0-9]", "", clean_text(code).upper())


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", clean_text(text).lower()).strip("-")


def to_decimal(value: Any) -> Optional[Decimal]:
    """A money/number value from a cell: 412, 412.0, '1,129.99', '$85.00' -> Decimal (2 places).
    Returns None for empty or non-numeric text."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)
    text = clean_text(value).replace("$", "").replace(",", "").strip()
    if not text or not re.fullmatch(r"-?\d+(\.\d+)?", text):
        return None
    try:
        return Decimal(text).quantize(CENT, rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return None


def money(value: Decimal) -> Decimal:
    """Round to cents the way the workbooks do (Excel ROUND = half away from zero)."""
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def cents(value: Optional[Decimal]) -> Optional[int]:
    return None if value is None else int((value * 100).to_integral_value(rounding=ROUND_HALF_UP))


# --- prices written as text (countertop guide, KCD modifications) -----------------------------------

_UNIT_WORDS = {"sf": "sqft", "sqft": "sqft", "lf": "lf", "each": "each", "ea": "each", "door": "door",
               "cut": "cut", "drawer": "drawer"}


def parse_rate(value: Any) -> Dict[str, Any]:
    """
    Read a price written as text into {amount, unit, needs_staff, note}.
        100 / "100"            -> 100 each
        "$5/LF", "$30/Lf"      -> 5 per linear foot
        "$4/SF"                -> 4 per square foot
        "$40 Each", "$350/each"-> 40 each
        "$60 per door"         -> 60 per door
        "NO CHARGE", "Standard"-> 0
        "$200 + Material ..."  -> 200 each, needs_staff (the rest can't be priced automatically)
        "Custom Estimate Needed" -> no amount, needs_staff
    """
    raw = clean_text(value)
    out: Dict[str, Any] = {"amount": None, "unit": "each", "needs_staff": False, "note": "", "raw": raw}
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        out["amount"] = to_decimal(value)
        return out
    low = raw.lower()
    if not raw:
        out["needs_staff"] = True
        return out
    if low in ("no charge", "standard", "free"):
        out["amount"] = Decimal("0.00")
        out["note"] = raw
        return out
    m = re.search(r"\$?\s*(\d[\d,]*(?:\.\d+)?)", raw)
    if not m:
        out["needs_staff"] = True
        out["note"] = raw
        return out
    out["amount"] = to_decimal(m.group(1))
    rest = raw[m.end():].strip()
    unit = re.match(r"^(?:/|per\s+|\s*)\s*([a-z]+)", rest, re.I)
    if unit and unit.group(1).lower() in _UNIT_WORDS:
        out["unit"] = _UNIT_WORDS[unit.group(1).lower()]
        rest = rest[unit.end():].strip(" -")
    if "+" in raw or re.search(r"\bplus\b|material|extra|custom", low):
        out["needs_staff"] = True
    out["note"] = rest.strip(" -")
    return out


# --- dimensions in descriptions ---------------------------------------------------------------------

_NUM = r"(\d+(?:\.\d+)?(?:\s+\d+/\d+)?|\d+/\d+)"
_DIM = re.compile(_NUM + r'\s*(?:"|in\.?|inch(?:es)?)\s*-?\s*([WHTD])\b', re.I)


def _to_inches(text: str) -> float:
    total = 0.0
    for part in text.split():
        if "/" in part:
            a, b = part.split("/")
            total += float(a) / float(b)
        else:
            total += float(part)
    return round(total, 4)


def parse_dims(description: str) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """(width, height, depth) in inches from text like '21"W 36"T 12"D' or '30" W x 36" H'.
    Only values with an explicit W/H/T/D letter are used; T (tall) means height."""
    found: Dict[str, float] = {}
    for num, letter in _DIM.findall(clean_text(description)):
        key = {"T": "H"}.get(letter.upper(), letter.upper())
        found.setdefault(key, _to_inches(num))
    return found.get("W"), found.get("H"), found.get("D")
