"""
pricing.py
----------
The ONE place where prices are calculated, so the review export, the quote engine and the agent's
tools always agree.

    unit_cost()     WHOLESALE cost of one unit from the supplier's price list and rules
                    (HCC: list x multiplier; KCD: list x line multiplier; Pelican: quantity-break price)
    sell()          customer price = cost x (1 + markup%)   (markup per business, config/pricing.json)
    rules()         the owner's pricing rules (markup, sales tax, installation rates, flooring waste),
                    re-read automatically when config/pricing.json is saved
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

from catalog.normalize import money

ROOT = Path(__file__).resolve().parent.parent
PRICING_PATH = ROOT / "config" / "pricing.json"
_cache: Dict[str, Any] = {"mtime": None, "rules": None}


def rules() -> Dict[str, Any]:
    mtime = PRICING_PATH.stat().st_mtime if PRICING_PATH.exists() else None
    if _cache["rules"] is None or _cache["mtime"] != mtime:
        _cache.update(mtime=mtime, rules=json.loads(PRICING_PATH.read_text(encoding="utf-8")) if mtime else {})
    return _cache["rules"]


def markup_percent(business: str) -> Decimal:
    m = rules().get("markup", {})
    value = (m.get("by_business") or {}).get(business)
    return Decimal(str(m.get("default_percent", 0) if value is None else value))


def sell(cost: Optional[Decimal], business: str) -> Optional[Decimal]:
    """Customer price from wholesale cost: cost x (1 + markup%), rounded to cents."""
    if cost is None:
        return None
    return money(cost * (1 + markup_percent(business) / 100))


def unit_cost(list_price: Optional[Decimal], pricing: Dict[str, Any], line_multiplier: Optional[Decimal] = None,
              price_type: Optional[str] = None, quantity: int = 1,
              tiers: Sequence[Tuple[int, Decimal]] = ()) -> Optional[Decimal]:
    """Wholesale cost for one unit, rounded to cents (Excel ROUND, half away from zero)."""
    if list_price is None:
        return None
    basis = pricing.get("basis")
    if basis == "list_price_x_multiplier":                       # HCC: MSRP x dealer multiplier
        return money(list_price * Decimal(str(pricing["multiplier"])))
    if basis == "list_price_x_line_multiplier":                  # KCD: list x line multiplier
        if price_type == "list" or line_multiplier is None:
            return money(list_price)
        return money(list_price * line_multiplier)
    if basis == "net_price_x_markup":                            # Pelican: dealer price, quantity breaks
        base = list_price
        for min_qty, price in sorted(tiers):
            if quantity >= min_qty:
                base = price
        return money(base)
    raise ValueError(f"Unknown pricing basis: {basis}")


# --- sales tax / installation / flooring -----------------------------------------------------------------

def tax_rate() -> Decimal:
    return Decimal(str(rules().get("sales_tax", {}).get("rate_percent", 0))) / 100


def is_taxable(category: str, installed: bool) -> bool:
    t = rules().get("sales_tax", {})
    if installed and t.get("installed_items_exempt", True):
        return False
    return category in set(t.get("taxable_categories", []))


def install_rate(category: str) -> Optional[Dict[str, Any]]:
    return (rules().get("installation", {}).get("rates") or {}).get(category)


def crew_hours_per_day() -> Decimal:
    return Decimal(str(rules().get("installation", {}).get("crew_hours_per_day", 8)))


def waste_percent() -> Decimal:
    return Decimal(str(rules().get("flooring", {}).get("waste_percent", 10)))


def waste_categories() -> set:
    return set(rules().get("flooring", {}).get("categories", ["flooring", "tile"]))


def unconfirmed() -> list:
    """Which owner values are still placeholders (shown to staff)."""
    r = rules()
    out = []
    if not r.get("markup", {}).get("confirmed"):
        out.append("markup")
    if not r.get("installation", {}).get("confirmed"):
        out.append("installation rates")
    return out
