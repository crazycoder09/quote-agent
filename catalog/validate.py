"""
validate.py
-----------
Checks that apply to every business after import. An "error" blocks publishing the catalog.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import List

from catalog.categories import CATEGORIES
from catalog.model import ImportResult
from catalog.normalize import match_key

HIGH_PRICE = Decimal("25000")
LOW_PRICE = Decimal("0.05")


def validate(results: List[ImportResult]) -> None:
    codes_by_business = defaultdict(set)
    for res in results:
        bid = res.business.id
        line_ids = {l.id for l in res.lines}
        bad_cat, bad_line, bad_price, outliers = [], [], [], []
        keys = defaultdict(list)
        for item in res.items:
            if item.category not in CATEGORIES:
                bad_cat.append(f"{item.sku}: '{item.category}'")
            if item.line is not None and item.line not in line_ids:
                bad_line.append(f"{item.sku}: line '{item.line}'")
            free = bool(item.extra.get("free_option")) and item.list_price == 0  # e.g. "ship trim unattached"
            if item.quotable and not free and (item.list_price is None or item.list_price <= 0):
                bad_price.append(item.sku)
            if item.quotable and not free and item.list_price is not None \
                    and not (LOW_PRICE <= item.list_price <= HIGH_PRICE):
                outliers.append(f"{item.sku} = {item.list_price}")
            if item.quotable:
                keys[(item.line, match_key(item.code))].append(item)
                codes_by_business[match_key(item.code)].add(bid)
        res.check("All categories are in the shared taxonomy", not bad_cat, len(res.items), "; ".join(bad_cat[:5]))
        res.check("All lines referenced by items exist", not bad_line, len(res.items), "; ".join(bad_line[:5]))
        res.check("Every quotable item has a price above 0 (or is a listed no-charge option)", not bad_price,
                  len(res.items), ", ".join(bad_price[:10]))
        for o in outliers[:20]:
            res.issue("warning", "price_outlier", f"Unusual price: {o}")
        ambiguous = 0
        for (line, key), items in keys.items():
            if len(items) < 2:
                continue
            prices = {i.list_price for i in items}
            names = ", ".join(f"{i.sku} ({i.list_price})" for i in items)
            if len(prices) > 1:
                ambiguous += 1
                res.issue("error", "ambiguous_code", f"Code '{items[0].code}' in line {line or '-'} matches "
                          f"different prices: {names}", items[0].source_ref)
            else:
                res.issue("info", "same_code_same_price", f"{names} share code '{items[0].code}' "
                          f"(line {line or '-'}) at the same price", items[0].source_ref)
        res.check("Each code + line points to one price", ambiguous == 0, len(keys), f"{ambiguous} ambiguous")
    shared = sum(1 for b in codes_by_business.values() if len(b) > 1)
    if shared and results:
        results[0].issue("info", "cross_business_codes",
                         f"{shared} codes exist in more than one business (e.g. HCC B15 and KCD B15) - the assistant "
                         "must pick the business/line from the customer's color/style, or ask")
