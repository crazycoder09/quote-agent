"""
pricing.py
----------
The ONE place where a unit price is calculated from catalog data. The quote engine (next step)
and the review export both use it, so the numbers shown to the client for review are exactly the
numbers customers will get.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, Optional, Sequence, Tuple

from catalog.normalize import money


def unit_price(list_price: Optional[Decimal], pricing: Dict[str, Any], line_multiplier: Optional[Decimal] = None,
               price_type: Optional[str] = None, quantity: int = 1,
               tiers: Sequence[Tuple[int, Decimal]] = ()) -> Optional[Decimal]:
    """Selling price for one unit, rounded to cents (Excel ROUND, half away from zero)."""
    if list_price is None:
        return None
    basis = pricing.get("basis")
    if basis == "list_price_x_multiplier":                       # HCC
        return money(list_price * Decimal(str(pricing["multiplier"])))
    if basis == "list_price_x_line_multiplier":                  # KCD
        if price_type == "list" or line_multiplier is None:
            return money(list_price)
        return money(list_price * line_multiplier)
    if basis == "net_price_x_markup":                            # Pelican (quantity breaks first)
        base = list_price
        for min_qty, price in sorted(tiers):
            if quantity >= min_qty:
                base = price
        return money(base * Decimal(str(pricing.get("markup", "1"))))
    raise ValueError(f"Unknown pricing basis: {basis}")
