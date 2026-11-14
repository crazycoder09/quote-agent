"""
quote_engine.py
---------------
Prices a quote that can mix businesses. The AI only says WHAT (item ids + codes + quantities,
countertop colors + square feet, install / delivery / assembly wanted); every number comes from the
catalog and the owner's rules (config/pricing.json):

    wholesale cost   from the supplier price list and its rules
                       HCC   ROUND(list x multiplier, 2)        KCD  ROUND(list x line multiplier, 2)
                       PELICAN quantity-break dealer price      countertops sq ft x group rate (+ services)
    customer price   cost x (1 + markup% of that business)
    assembly         supplier rule on cost (HCC % of the section, KCD per item + pick-up extra), marked up
    installation     install price sheet: price + labor time per unit (cabinets each, flooring/tile per sq ft)
    sales tax        rate x taxable product lines (no tax on countertops, services, or items we install)
    margin           customer price - wholesale cost (products, assembly, countertops) - for staff reports only

Each business becomes its own section of the quote. Anything that can't be priced exactly is a
"problem" - the quote is not created until it is fixed (or moved to unpriced_items).
"""

from __future__ import annotations

import math
import re
from collections import OrderedDict
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from catalog.normalize import match_key, money
from catalog.pricing import (crew_hours_per_day, install_rate, is_taxable, markup_percent, sell, tax_rate,
                             unconfirmed)
from catalog.store import Catalog

ZERO = Decimal("0")


def _f(x: Decimal) -> float:
    return float(money(x))


def _code_matches(cat: Catalog, item: Dict[str, Any], code: str) -> bool:
    key = match_key(code)
    if key in (item["code_key"], item["sku_key"]):
        return True
    m = re.fullmatch(r"(.+?)[- ]?([LR])", code.strip(), re.I)  # hinge side: B15L -> B15
    if m and match_key(m.group(1)) in (item["code_key"], item["sku_key"]):
        return True
    model = match_key(item["extra"].get("model") or "")
    for a in cat._q("select target_key from aliases where alias_key=? and business_id=?", (key, item["business_id"])):
        if a["target_key"] in (item["code_key"], model):
            return True
    return bool(model) and key == model  # 'PL-150' for 'PL-150 Alpina'


def price_quote(cat: Catalog, items: List[Dict[str, Any]], countertops: List[Dict[str, Any]], assembled: bool,
                shipping: bool, extras: Optional[List[Dict[str, Any]]] = None,
                installation: bool = False) -> Tuple[Dict[str, Any], List[str], List[str]]:
    problems: List[str] = []
    warnings: List[str] = []

    # Same item twice -> one line (quantity breaks depend on the total quantity)
    merged: "OrderedDict[Tuple[int, str], Dict[str, Any]]" = OrderedDict()
    for raw in items:
        key = (int(raw["item_id"]), (raw.get("note") or "").strip())
        if key in merged:
            merged[key]["quantity"] += int(raw["quantity"])
        else:
            merged[key] = {**raw, "item_id": int(raw["item_id"]), "quantity": int(raw["quantity"])}

    sections: "OrderedDict[str, Optional[Dict[str, Any]]]" = OrderedDict((bid, None) for bid in cat.business_order())
    for raw in merged.values():
        qty = raw["quantity"]
        code = str(raw.get("code") or "").strip()
        if qty < 1:
            problems.append(f"{code}: quantity must be at least 1")
            continue
        item = cat.get_item(raw["item_id"], qty)
        if item is None:
            problems.append(f"item_id {raw['item_id']} ('{code}') does not exist - use the item_id find_products/"
                            "search_products returned")
            continue
        if code and not _code_matches(cat, item, code):
            problems.append(f"item_id {item['id']} is {item['sku']} ({item['description']}), not '{code}' - use the "
                            f"item_id that find_products returned for '{code}'")
            continue
        if not item["quotable"]:
            problems.append(f"{item['sku']} can't be quoted: {item['not_quotable_reason']} - offer a real alternative "
                            "or put it in unpriced_items")
            continue
        if item["needs_confirmation"]:
            problems.append(f"{item['sku']} needs staff confirmation ({item['needs_confirmation']}) - don't quote it: "
                            "offer the alternative or put it in unpriced_items for a designer")
            continue
        if item["unit_price"] is None or item["unit_cost"] is None:
            problems.append(f"{item['sku']}: no price")
            continue
        bid = item["business_id"]
        if sections.get(bid) is None:
            sections[bid] = {"business": bid, "business_name": cat.businesses[bid]["name"], "items": [], "lines": []}
        sec = sections[bid]
        line = item["line_name"]
        if line and line not in sec["lines"]:
            sec["lines"].append(line)
        installed = bool(installation and install_rate(item["category"]))
        sec["items"].append({
            "item_id": item["id"], "sku": item["sku"], "code": item["code"],
            "requested_as": code if code and match_key(code) not in (item["code_key"], item["sku_key"]) else None,
            "description": item["description"], "line": line, "category": item["category"],
            "quantity": qty, "unit": item["unit"], "unit_note": item["unit_note"] or "",
            "unit_price": item["unit_price"], "total": money(item["unit_price"] * qty),
            "unit_cost": item["unit_cost"], "cost_total": money(item["unit_cost"] * qty),
            "installed": installed, "taxable": is_taxable(item["category"], installed),
            "sqft_per_unit": item["extra"].get("sqft_per_unit"),
            "note": (raw.get("note") or "").strip() or None,
            "_assembly": item["assembly_price"], "_tiers": bool(item["tiers"]),
        })

    rate = tax_rate()
    out_sections, install_lines = [], OrderedDict()
    for bid, sec in sections.items():
        if not sec:
            continue
        pricing = cat.businesses[bid]["pricing"]
        subtotal = sum((i["total"] for i in sec["items"]), ZERO)
        cost = sum((i["cost_total"] for i in sec["items"]), ZERO)
        fee_cost, fee_label = ZERO, ""
        rule = pricing.get("assembly") or {}
        if assembled and rule.get("type") == "percent_of_cabinet_subtotal":
            pct = Decimal(str(rule["percent"]))
            fee_cost, fee_label = money(cost * pct / 100), f"Assembly fee ({pct.normalize():f}%)"
        elif assembled and rule.get("type") == "per_item":
            extra = Decimal(str(rule.get("pickup_extra_per_cabinet") or 0)) if not shipping else ZERO
            for i in sec["items"]:
                if i["_assembly"] is not None:
                    fee_cost += (i["_assembly"] + (extra if i["category"] == "cabinet" else ZERO)) * i["quantity"]
            fee_cost, fee_label = money(fee_cost), "Assembly"
        fee = sell(fee_cost, bid) if fee_cost else ZERO
        taxable_amount = sum((i["total"] for i in sec["items"] if i["taxable"]), ZERO)
        tax = money(taxable_amount * rate)
        for i in sec["items"]:
            if i.pop("_tiers") and i["quantity"] > 1:
                i["note"] = "; ".join(x for x in (i["note"], "quantity price") if x)
            i.pop("_assembly")
            if i["installed"]:  # what installation covers, per category of the install price sheet
                r = install_rate(i["category"])
                key = i["category"]
                units = Decimal(str(i["quantity"]))
                if r.get("per") == "sqft":
                    if not i["sqft_per_unit"]:
                        warnings.append(f"{i['sku']}: coverage per unit unknown - installation counted per piece")
                    units = units * Decimal(str(i["sqft_per_unit"] or 1))
                row = install_lines.setdefault(key, {"category": key, "label": r.get("label", key), "per": r.get("per", "each"),
                                                     "rate": Decimal(str(r["price"])), "quantity": ZERO, "minutes": ZERO})
                row["quantity"] += units
                row["minutes"] += units * Decimal(str(r.get("minutes", 0)))
            for k in ("unit_price", "total", "unit_cost", "cost_total"):
                i[k] = _f(i[k])
        out_sections.append({
            "business": bid, "business_name": sec["business_name"], "lines": sec["lines"],
            "assembled": bool(assembled and rule.get("type")), "assembly_label": fee_label,
            "items": sec["items"], "item_count": sum(i["quantity"] for i in sec["items"]),
            "subtotal": _f(subtotal), "assembly_fee": _f(fee), "total": _f(subtotal + fee),
            "taxable_amount": _f(taxable_amount), "tax": _f(tax),
            "markup_percent": float(markup_percent(bid)), "cost": _f(cost + fee_cost),
        })

    ct_section = _countertops(cat, countertops or [], problems)

    installation_section = None
    if installation and install_lines:
        rows, total, minutes = [], ZERO, ZERO
        for r in install_lines.values():
            qty = r["quantity"].quantize(Decimal("0.01"))
            amount = money(qty * r["rate"])
            total += amount
            minutes += r["minutes"]
            rows.append({"category": r["category"], "label": r["label"], "quantity": float(qty), "unit": r["per"],
                         "rate": _f(r["rate"]), "amount": _f(amount)})
        hours = (minutes / 60).quantize(Decimal("0.1"))
        days = max(1, math.ceil(hours / crew_hours_per_day())) if hours else 0
        installation_section = {"lines": rows, "total": _f(total), "hours": float(hours), "days": days,
                                "estimate": "installation rates" in unconfirmed()}
    elif installation:
        warnings.append("Installation was requested but nothing in the quote is installed by us")

    extra_rows = []
    for x in extras or []:
        amount = money(Decimal(str(x["quantity"])) * Decimal(str(x["rate"])))
        extra_rows.append({**x, "amount": _f(amount)})

    D = lambda v: Decimal(str(v))  # noqa: E731
    items_subtotal = sum((D(s["subtotal"]) for s in out_sections), ZERO)
    assembly = sum((D(s["assembly_fee"]) for s in out_sections), ZERO)
    ct_total = D(ct_section["total"]) if ct_section else ZERO
    install_total = D(installation_section["total"]) if installation_section else ZERO
    extras_total = sum((D(x["amount"]) for x in extra_rows), ZERO)
    tax = sum((D(s["tax"]) for s in out_sections), ZERO)
    products = items_subtotal + assembly + ct_total
    cost = sum((D(s["cost"]) for s in out_sections), ZERO) + (D(ct_section["cost"]) if ct_section else ZERO)
    totals = {"items_subtotal": _f(items_subtotal), "assembly_fee": _f(assembly), "countertops": _f(ct_total),
              "installation": _f(install_total), "extras_total": _f(extras_total),
              "tax": _f(tax), "tax_rate_percent": float(rate * 100),
              "grand_total": _f(products + install_total + extras_total + tax),
              # staff only (margin tracking / reports) - never shown to customers
              "cost": _f(cost), "margin": _f(products - cost),
              "margin_percent": round(float((products - cost) / products * 100), 1) if products else 0.0}
    if not out_sections and not ct_section and not extra_rows and not problems:
        problems.append("The quote has no items")
    pricing_snapshot = {"markup_percent": {s["business"]: s["markup_percent"] for s in out_sections},
                        "tax_rate_percent": float(rate * 100), "placeholders": unconfirmed()}
    if ct_section:
        pricing_snapshot["markup_percent"][ct_section["business"]] = float(markup_percent(ct_section["business"]))
    return ({"sections": out_sections, "countertops": ct_section, "installation": installation_section,
             "extras": extra_rows, "totals": totals, "pricing_snapshot": pricing_snapshot}, problems, warnings)


def m_name(matches: List[Dict[str, Any]]) -> str:
    return matches[0]["name"]


def _countertops(cat: Catalog, pieces: List[Dict[str, Any]], problems: List[str]) -> Optional[Dict[str, Any]]:
    if not pieces:
        return None
    bid = cat.countertop_business()
    if not bid:
        problems.append("No countertop price guide in the catalog")
        return None
    out, total, cost_total = [], ZERO, ZERO
    for p in pieces:
        color, sqft = str(p.get("color") or "").strip(), Decimal(str(p.get("sqft") or 0))
        name = p.get("label") or "Countertop"
        if sqft <= 0:
            problems.append(f"{name}: square feet must be more than 0")
            continue
        matches = cat.countertop_color(color)
        exact = [m for m in matches if m["name_key"] == match_key(color)] or matches
        groups = {m["group_id"] for m in exact}
        if not exact:
            problems.append(f"Countertop color '{color}' is not in the countertop guide - call countertop_options "
                            "and offer real colors")
            continue
        if len(groups) > 1:
            names = ", ".join(sorted({f"{m['name']} ({m['group_name'] or m['group_id']})" for m in exact}))
            if len({match_key(m["name"]) for m in exact}) == 1:  # same color listed in two groups = guide conflict
                problems.append(f"'{m_name(exact)}' is listed in two price groups in the countertop guide ({names}) - "
                                "a data conflict, NOT a customer choice: don't ask the customer to pick a price. Leave it "
                                f"out and add '{m_name(exact)} countertop {p.get('sqft')} sq ft - designer to confirm the "
                                "price' to unpriced_items (quote the rest), or offer another color.")
            else:
                problems.append(f"'{color}' matches several colors: {names} - ask which color they mean")
            continue
        m = exact[0]
        if m["rate"] is None:
            problems.append(f"'{m['name']}' has no price in the countertop guide ({m['note']}) - put it in unpriced_items")
            continue
        material_cost = money(m["rate"] * sqft)
        material = sell(material_cost, bid)
        services, piece_total, piece_cost = [], material, material_cost
        for s in p.get("services") or []:
            charge = cat.charge(bid, s.get("code") or "")
            if charge is None:
                problems.append(f"Countertop service '{s.get('code')}' not found - use a code from "
                                "countertop_options(services=true)")
                continue
            if charge["amount"] is None or charge["needs_staff"]:
                problems.append(f"'{charge['name']}' has no fixed price ('{charge['raw']}') - leave it out and add it "
                                "to unpriced_items")
                continue
            qty = Decimal(str(s.get("quantity") or 1))
            unit = sell(charge["amount"], bid)
            amount = money(unit * qty)
            piece_total += amount
            piece_cost += money(charge["amount"] * qty)
            services.append({"code": charge["code"], "name": charge["name"], "quantity": float(qty),
                             "unit": charge["unit"], "rate": _f(unit), "amount": _f(amount)})
        total += piece_total
        cost_total += piece_cost
        out.append({"label": name, "color": m["name"], "group": m["group_name"], "material": m["material"],
                    "sqft": float(sqft), "rate": _f(sell(m["rate"], bid)), "material_amount": _f(material),
                    "services": services, "total": _f(piece_total), "cost": _f(piece_cost), "note": p.get("note") or None})
    if not out:
        return None
    return {"business": bid, "business_name": cat.businesses[bid]["name"], "pieces": out, "total": _f(total),
            "cost": _f(cost_total), "markup_percent": float(markup_percent(bid))}

