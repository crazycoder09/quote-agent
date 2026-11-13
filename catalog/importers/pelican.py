"""
Pelican International - NetSuite price list export (Excel 2003 XML saved as .xls).

One tab, three blocks, each starting with its own header row "Item | Description | Unit | Currency | ...":
    block 1/2: quartz slabs with QUANTITY BREAK prices (columns '0', '4', '10', '20' / '0', '5', '10' =
               minimum quantity for that price)
    block 3:   everything else with a single 'Unit Price'
Rules applied here (each one shows up in the report):
    $0 samples / swag / catalogs / displays      -> not quotable
    "(old)" items                                -> not quotable (superseded)
    "Special Order ..."                          -> quotable, flagged special order
    same code used for two different products     -> codes made unique from the description (e.g. 12mil / 20mil)
    same description at two different prices     -> both kept, flagged "needs confirmation"
    flooring sold per box                         -> unit = box, pieces and sq ft per box kept
"""

from __future__ import annotations

import re
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from catalog.categories import classify_plumbing
from catalog.model import Business, ImportResult, Item
from catalog.normalize import clean_text, match_key, parse_dims, to_decimal
from catalog.readers import read_spreadsheetml


def _box_note(desc: str) -> Tuple[str, Optional[float]]:
    pcs = re.search(r"(\d+)\s*pc\s*/\s*box", desc, re.I)
    sqft = re.search(r"([\d.]+)\s*(?:sq\.?\s*ft|sqft|sf)\s*/\s*box", desc, re.I)
    parts = []
    if pcs:
        parts.append(f"{pcs.group(1)} pcs per box")
    if sqft:
        parts.append(f"{sqft.group(1)} sq ft per box")
    return ", ".join(parts), (float(sqft.group(1)) if sqft else None)


def import_pelican(bid: str, cfg: Dict[str, Any], root: Path) -> ImportResult:
    path = root / cfg["source"]
    sheets = read_spreadsheetml(path)
    tab, rows = next(iter(sheets.items()))
    markup = Decimal(str(cfg.get("markup", 1.0)))
    date = next((r[1] for r in rows[:10] if r and r[0].strip().lower() == "date" and len(r) > 1), "")
    business = Business(
        id=bid, name=cfg["name"], kind=cfg["kind"], source_files=[cfg["source"]], documents=cfg.get("documents", []),
        pricing={
            "basis": "net_price_x_markup",
            "markup": str(markup),
            "price_list_date": date,
            "unit_price": "Pelican price x markup, rounded to cents; quartz slabs use the quantity-break price "
                          "for the quantity ordered",
            "shipping": "not in the price list",
            "needs_confirmation": [
                "Is the Pelican price list the selling price or the cost? (markup is 1.0 until confirmed)",
                "Quantity breaks: columns 0/4/10/20 are read as 'from this many slabs' (1-3, 4-9, 10-19, 20+)",
                "Vinyl flooring: is the price per box (as listed with pcs/box and sq ft/box)?",
            ],
        })
    res = ImportResult(business)
    ref = lambda r: f"{path.name} | {tab}!row {r}"  # noqa: E731

    tiers: List[Optional[int]] = []
    blocks, raw_items = 0, []
    for idx, row in enumerate(rows, start=1):
        cells = [clean_text(c) for c in row]
        if not any(cells):
            continue
        if cells[0].lower() == "item" and len(cells) > 4:
            blocks += 1
            heads = cells[4:]
            tiers = [int(h) if re.fullmatch(r"\d+", h) else None for h in heads]
            if tiers and tiers[0] is None:
                tiers = [None]  # single 'Unit Price' column
            continue
        if not tiers or not cells[0] or len(cells) < 5:
            continue
        raw_items.append((idx, cells, list(tiers)))

    dup_codes = defaultdict(list)
    for idx, cells, _ in raw_items:
        dup_codes[match_key(cells[0])].append(idx)

    for idx, cells, tier_heads in raw_items:
        sku, desc, unit_raw, currency = cells[0], cells[1] or cells[0], cells[2], cells[3]
        prices = [to_decimal(cells[4 + i]) if 4 + i < len(cells) else None for i in range(len(tier_heads))]
        code = sku
        if len(dup_codes[match_key(sku)]) > 1:
            mil = re.search(r"(\d+)\s*mil", desc, re.I)
            code = f"{sku} {mil.group(1)}mil" if mil and mil.group(1) not in sku else f"{sku} #{dup_codes[match_key(sku)].index(idx) + 1}"
            res.issue("warning", "duplicate_code", f"Code '{sku}' is used for more than one product; "
                      f"row {idx} is now '{code}' ({desc})", ref(idx))
        category = classify_plumbing(sku, desc)
        unit_note, sqft_per_box = _box_note(desc)
        unit = "box" if "/box" in desc.lower() else ("each" if unit_raw.lower() in ("each", "") else unit_raw.lower())
        w, h, d = parse_dims(desc)
        item = Item(bid, sku, code, desc, category, prices[0] if prices else None, ref(idx),
                    section=category, unit=unit, unit_note=unit_note, width_in=w, height_in=h, depth_in=d,
                    extra={"model": re.match(r"(PL-[0-9A-Z]+)", sku.upper()).group(1)
                           if re.match(r"PL-[0-9A-Z]+", sku.upper()) else None,
                           **({"sqft_per_box": sqft_per_box} if sqft_per_box else {})})
        if currency and currency.upper() != "USD":
            item.quotable, item.not_quotable_reason = False, f"currency {currency}"
            res.issue("error", "currency", f"{sku}: currency is {currency}", ref(idx))
        if len(tier_heads) > 1 or tier_heads[0] is not None:
            for head, price in zip(tier_heads, prices):
                if head is not None and price is not None:
                    item.tiers.append((max(1, head), price))
            ordered = [p for _, p in item.tiers]
            if ordered != sorted(ordered, reverse=True):
                res.issue("warning", "tier_order", f"{sku}: quantity-break prices don't go down with quantity {ordered}", ref(idx))
        low = f"{sku} {desc}".lower()
        if item.list_price is None:
            item.quotable, item.not_quotable_reason = False, "no price"
            res.issue("error" if category not in ("sample", "literature") else "info", "missing_price",
                      f"{sku}: no price", ref(idx))
        elif item.list_price == 0:
            item.quotable = False
            if category in ("sample", "literature"):
                item.not_quotable_reason = "free sample / marketing item"
            else:
                item.not_quotable_reason = "price is 0 in the price list"
                res.issue("warning", "zero_price", f"{sku} ({desc}) has price 0 - not quotable until priced", ref(idx))
        if category == "literature":
            item.quotable, item.not_quotable_reason = False, item.not_quotable_reason or "marketing item"
        if "(old)" in low:
            item.quotable, item.not_quotable_reason = False, "old version (superseded)"
            res.issue("info", "old_item", f"{sku}: marked (old) - not quotable", ref(idx))
        if low.startswith("special order"):
            item.extra["special_order"] = True
        if unit_raw == "" and unit == "each":
            res.issue("info", "unit_default", f"{sku}: no unit in the list; using 'each'", ref(idx))
        res.items.append(item)

    # Same product listed twice at different prices -> a human must pick the right one.
    # "Same product" = identical description, same name (first word of the code), and no code adds a
    # distinguishing word the description doesn't have: 'Acorn' vs 'Acorn 20mil Dryback Planks' (same
    # floor) is a conflict; 'PL-FC3321SB White' vs '... Black' (colors) and mosaics that share a generic
    # description ('Carbon Hex' / 'Moonstone Hex') are different products.
    generic = {"planks", "plank", "flooring", "floor", "laminate", "vinyl", "tile", "tiles", "slab", "new"}

    def distinguishing(item: Item) -> set:
        words = re.findall(r"[a-z0-9]+", item.sku.lower())[1:]
        desc = set(re.findall(r"[a-z0-9]+", item.description.lower()))
        return {w for w in words if w not in desc and w not in generic}

    by_desc = defaultdict(list)
    for item in res.items:
        if item.quotable and item.category not in ("sample", "literature"):
            first = match_key(item.sku.split()[0]) if item.sku.split() else ""
            by_desc[(match_key(item.description), item.unit, first)].append(item)
    # One product, one entry: keep the most descriptive code ('Acorn 20mil Dryback Planks', 'PL-150 Alpina'),
    # drop the others ('Acorn', bare 'PL-150'). When the duplicates disagree on price the kept entry is
    # the higher one (never under-quote) - every such case is listed in the report for the client.
    duplicates = conflicts = 0
    for group in by_desc.values():
        if len(group) < 2 or any(distinguishing(i) for i in group):
            continue
        keep = max(group, key=lambda i: (len(i.sku), i.list_price or 0))
        prices = {i.list_price for i in group}
        if len(prices) > 1 and keep.list_price != max(prices):
            keep = max(group, key=lambda i: (i.list_price or 0, len(i.sku)))
        for i in group:
            if i is keep:
                continue
            i.quotable, i.not_quotable_reason = False, f"duplicate of '{keep.sku}'"
            duplicates += 1
        if len(prices) > 1:
            conflicts += 1
            dropped = ", ".join(f"{i.sku} = {i.list_price}" for i in group if i is not keep)
            res.issue("warning", "duplicate_price", f"'{keep.description}': kept {keep.sku} = {keep.list_price}, "
                      f"removed {dropped} (same product, different price - higher kept; confirm with the client)",
                      keep.source_ref)
        else:
            res.issue("info", "duplicate", f"Kept {keep.sku}, removed " + ", ".join(i.sku for i in group if i is not keep)
                      + " (same product, same price)", keep.source_ref)

    res.counts = {"blocks": blocks, "price rows": len(raw_items), "items": len(res.items),
                  "quotable": sum(i.quotable for i in res.items),
                  "with quantity breaks": sum(1 for i in res.items if i.tiers),
                  "duplicates removed": duplicates, "of which different price": conflicts}
    res.check("Every price row in the file became an item", len(res.items) == len(raw_items), len(raw_items))
    numeric = sum(1 for i in res.items if i.list_price is not None)
    res.check("Every item price was read as a number", numeric == len(res.items), len(res.items),
              f"{len(res.items) - numeric} not numeric")
    return res
