"""
KCD - "KCD Request for Quote" workbook.

The visible order form only looks things up; the real data is in hidden tabs:
    SKU List         all products: C=SKU, D=description, E=list price, F=weight, G=assembly price,
                     I=pricing group (line), J=product type (C cabinet, A accessory, H hood, P part, S sample, O other),
                     K=cubic meters, L=purchase unit
    Items by line    which SKUs each line offers (the shaker white SLAB and 5-PIECE lines share many SKUs)
    Inputs           line list, delivery / assembly options; H1 = $3 extra per cabinet when assembled + picked up
    Modification Pricing (visible) - 14 modifications with text prices
Order-form rules (formulas in 'KCD Request for Quote'):
    your price = ROUND(list x line multiplier, 2) if the item has a pricing group ("Mult."), else list price
    assembly   = qty x (assembly price + $3 if cabinet and pick-up), only when "Assembled"
    line multipliers are typed in J7:J22 next to the line names in H7:H22.
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

import openpyxl

from catalog.categories import cabinet_type, classify_trim_or_accessory
from catalog.model import Business, ImportResult, Item, Line
from catalog.normalize import clean_text, match_key, money, parse_dims, parse_rate, to_decimal

RFQ = "KCD Request for Quote"
NOT_ON_ORDER_FORM = {"US", "PW", "NW"}  # lines with SKUs in the price list but no column on the order form


def _num(value: Any) -> Optional[Decimal]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return Decimal(str(value))
    return to_decimal(value)


def _category(sku: str, desc: str, group: str, ptype: str) -> str:
    if group == "Literature":
        return "literature"
    if sku.upper().startswith("MOD") or desc.lower().startswith("modification") or "hood mod" in desc.lower():
        return "modification"
    if ptype == "H" or group == "Hoods":
        return "hood"
    if ptype == "C":
        return "cabinet"
    if ptype == "S" or group == "Samples":
        return "sample"
    if ptype in ("P", "O"):
        return "part"
    if sku.upper().startswith("DH-") or group == "Decorative Hardware":
        return "hardware"
    if group == "Floating Shelves" or re.search(r"-W?FS\d", sku.upper()):
        return "shelf"
    return classify_trim_or_accessory(f"{sku} {desc}")


def import_kcd(bid: str, cfg: Dict[str, Any], root: Path) -> ImportResult:
    path = root / cfg["source"]
    wb = openpyxl.load_workbook(path, data_only=True)
    rfq, sku_ws, ibl, inputs = wb[RFQ], wb["SKU List"], wb["Items by line"], wb["Inputs"]

    def ref(tab: str, cell: str) -> str:
        return f"{path.name} | {tab}!{cell}"

    # --- line multipliers typed on the order form ----------------------------------------------------
    sheet_mult: Dict[str, Decimal] = {}
    for r in range(7, 23):
        name, mult = clean_text(rfq[f"H{r}"].value), _num(rfq[f"J{r}"].value)
        if name:
            sheet_mult[name.lower()] = mult
    override = cfg.get("multiplier_override")
    pickup_extra = _num(inputs["H1"].value) or Decimal("0")
    business = Business(
        id=bid, name=cfg["name"], kind=cfg["kind"], source_files=[cfg["source"]], documents=cfg.get("documents", []),
        pricing={
            "basis": "list_price_x_line_multiplier",
            "multiplier_source": f"{RFQ}!J7:J22" if override is None else "config/catalog.json multiplier_override",
            "unit_price": "ROUND(list price x line multiplier, 2); items without a pricing group use the list price",
            "assembly": {"type": "per_item", "source": "SKU List column G (assembly price per item)",
                         "pickup_extra_per_cabinet": str(pickup_extra),
                         "when": "only if the customer wants it assembled; +$" + str(pickup_extra)
                                 + " per cabinet when assembled and picked up (Inputs!H1)"},
            "shipping": "delivery / LTL / pick up - shipping is not priced in the workbook",
            "needs_confirmation": [],
        })
    res = ImportResult(business)
    if override is None and sheet_mult and all(v == Decimal("1") for v in sheet_mult.values()):
        msg = ("Every KCD line multiplier in the workbook is 1 (J7:J22), so prices are KCD list prices. "
               "Confirm the dealer multiplier and set multiplier_override in config/catalog.json.")
        business.pricing["needs_confirmation"].append(msg)
        res.issue("warning", "multiplier", msg, ref(RFQ, "J7:J22"))

    # --- lines -----------------------------------------------------------------------------------------
    line_by_id: Dict[str, Line] = {}
    for i, spec in enumerate(cfg["lines"]):
        names = [spec["name"], *spec.get("aliases", [])]
        mult = Decimal(str(override)) if override is not None else next(
            (sheet_mult[n.lower()] for n in names if n.lower() in sheet_mult), None)
        if mult is None:
            # Line not on the order form (hood/shelf finishes, or lines the form doesn't offer):
            # the form applies the selected line's multiplier to everything - all equal here.
            values = set(sheet_mult.values())
            mult = values.pop() if len(values) == 1 else None
            if mult is None:
                res.issue("error", "multiplier", f"No multiplier for line {spec['name']}")
        line = Line(bid, spec["id"], spec["name"], multiplier=mult, aliases=spec.get("aliases", []), sort=i)
        res.lines.append(line)
        line_by_id[spec["id"]] = line
    prefixes = {l.id for l in res.lines} - {"SW5"}
    group_to_line = {l.name.lower(): l.id for l in res.lines}
    group_to_line.update({a.lower(): l.id for l in res.lines for a in l.aliases})
    group_to_line["shaker white"] = "SW"

    # --- which SKUs each line offers on the order form -------------------------------------------------
    members: Dict[str, set] = {}
    for c in range(1, ibl.max_column + 1):
        head = clean_text(ibl.cell(1, c).value)
        if head:
            members[head] = {clean_text(ibl.cell(r, c).value) for r in range(2, ibl.max_row + 1) if ibl.cell(r, c).value}
    sw5_members = members.get("SW 5PC", set())

    # --- products ----------------------------------------------------------------------------------------
    seen_rows, missing_unit, kept = 0, 0, 0
    for r in range(2, sku_ws.max_row + 1):
        raw_sku = sku_ws.cell(r, 3).value
        if raw_sku in (None, ""):
            continue
        seen_rows += 1
        sku = clean_text(raw_sku)
        desc = clean_text(sku_ws.cell(r, 4).value) or sku
        price = _num(sku_ws.cell(r, 5).value)
        assembly = _num(sku_ws.cell(r, 7).value)
        group = clean_text(sku_ws.cell(r, 9).value)
        ptype = clean_text(sku_ws.cell(r, 10).value).upper()
        unit_raw = clean_text(sku_ws.cell(r, 12).value)
        if not unit_raw:
            missing_unit += 1

        # line + customer-facing code: "BF-B15" -> line BF, code B15; "SW-B15-5PC" -> line SW5, code B15
        line_ids: List[Optional[str]] = [None]
        code = sku
        head, _, rest = sku.partition("-")
        if head.upper() in prefixes and rest:
            code = rest
            if head.upper() == "SW" and re.search(r"5\s*PC", rest, re.I):
                line_ids = ["SW5"]
                code = re.sub(r"-?5PC$", "", rest, flags=re.I).strip("-") if rest.upper().endswith("5PC") else rest
            elif head.upper() == "SW" and sku in sw5_members:
                line_ids = ["SW", "SW5"]          # shared by the slab and 5-piece lines on the order form
            else:
                line_ids = [head.upper()]
        elif group and group.lower() in group_to_line:
            line_ids = ["SW5" if re.search(r"5\s*PC", sku, re.I) and group_to_line[group.lower()] == "SW"
                        else group_to_line[group.lower()]]

        category = _category(sku, desc, group, ptype)
        w, h, d = parse_dims(desc)
        unit, unit_note = "each", ""
        if unit_raw.lower().startswith("box"):
            unit, unit_note = "package", (re.search(r"\((\d+)/pkg\)", desc).group(0) if re.search(r"\((\d+)/pkg\)", desc) else unit_raw)
        quotable, reason, needs = True, "", ""
        if price is None:
            quotable, reason = False, "no price in the price list"
            res.issue("warning" if category in ("sample", "literature") or sku.upper().startswith("YN-COUNTERTOP") else "error",
                      "missing_price", f"{sku} ({desc}) has no price", ref("SKU List", f"E{r}"))
        free_option = False
        if price == 0:
            if category == "modification":
                free_option = True
                res.issue("info", "free_option", f"{sku} ({desc}) is a no-charge option", ref("SKU List", f"E{r}"))
            else:
                quotable, reason = False, "price is 0"
                res.issue("warning", "zero_price", f"{sku} ({desc}) has price 0", ref("SKU List", f"E{r}"))
        if category == "literature":
            quotable, reason = False, "dealer marketing material"
        if any(l in NOT_ON_ORDER_FORM for l in line_ids if l):
            quotable, reason = False, "line is not offered on KCD's order form - confirm it can be ordered"
            needs = reason
        extra = {"pricing_group": group or None, "product_type": ptype or None,
                 "internal_id": sku_ws.cell(r, 2).value,
                 "price_type": "multiplier" if group else "list",
                 **({"free_option": True} if free_option else {})}
        if category == "cabinet":
            extra["cabinet_type"] = cabinet_type(desc)
        for line_id in line_ids:
            item = Item(bid, sku, code, desc, category, price, ref("SKU List", f"E{r}"), line=line_id,
                        section=group or ptype, unit=unit, unit_note=unit_note, assembly_price=assembly,
                        weight_lb=sku_ws.cell(r, 6).value if isinstance(sku_ws.cell(r, 6).value, (int, float)) else None,
                        volume_m3=sku_ws.cell(r, 11).value if isinstance(sku_ws.cell(r, 11).value, (int, float)) else None,
                        width_in=w, height_in=h, depth_in=d, quotable=quotable, not_quotable_reason=reason,
                        extra=dict(extra, **({"needs_confirmation": needs} if needs else {})))
            res.items.append(item)
            kept += 1
    if missing_unit:
        res.issue("info", "unit_default", f"{missing_unit} SKUs have no purchase unit; using 'each'", ref("SKU List", "L:L"))
    not_on_form = sorted({l for i in res.items for l in [i.line] if l in NOT_ON_ORDER_FORM})
    if not_on_form:
        n = sum(1 for i in res.items if i.line in NOT_ON_ORDER_FORM)
        res.issue("warning", "line_not_on_form",
                  f"{n} items in lines {', '.join(line_by_id[l].name for l in not_on_form)} are in the price list "
                  "but not on the order form - marked not quotable until the client confirms", ref("SKU List", "I:I"))

    # The order form's per-line SKU lists must resolve in the price list. Excel's VLOOKUP ignores case
    # ('YN-Drawer Slide-12"' finds 'YN-DRAWER SLIDE-12"'), so compare by match key. Section titles
    # ('WALL CABINETS', 'VANITIES') are the header names listed in Inputs!B2:B27.
    headers = {match_key(inputs[f"B{r}"].value) for r in range(2, 28) if inputs[f"B{r}"].value}
    known = {match_key(i.sku) for i in res.items}
    listed = {s for skus in members.values() for s in skus if match_key(s) not in headers}
    missing_members = sorted(s for s in listed if match_key(s) not in known)
    for s in missing_members:
        res.issue("warning", "order_form_sku_missing", f"{s} is on KCD's order form but not in the price list - "
                  "it cannot be quoted (the workbook shows no price for it either)", ref("Items by line", "A:Q"))
    # A handful of gaps is the supplier's data; many would mean we read the layout wrong.
    res.check("Order-form line lists resolve in the price list (>= 99%)",
              len(missing_members) <= max(1, len(listed) // 100), len(listed),
              f"{len(missing_members)} missing: {', '.join(missing_members[:10])}")

    _modifications(res, wb, ref)
    res.counts = {"price list rows": seen_rows, "items (incl. SKUs shared by 2 lines)": kept,
                  "lines": len(res.lines),
                  "modifications offered": sum(1 for i in res.items if i.section == "Modifications")}
    _verify(res, wb, rfq, inputs, line_by_id)
    return res


def _modifications(res: ImportResult, wb, ref) -> None:
    ws = wb["Modification Pricing"]
    for r in range(7, ws.max_row + 1):
        code, name, cost = ws[f"A{r}"].value, ws[f"B{r}"].value, ws[f"C{r}"].value
        if not code or cost in (None, ""):
            continue
        rate = parse_rate(cost)
        detail = clean_text(ws[f"E{r}"].value)
        if rate["amount"] is None or rate["needs_staff"]:
            # "$115 + $15 per drawer", "$30 + cost of shelf" - not one fixed price -> a designer prices it
            res.issue("info", "modification_not_offered", f"{clean_text(code)} {clean_text(name)}: '{rate['raw']}' "
                      "has no single fixed price - not offered for automatic quotes", ref("Modification Pricing", f"C{r}"))
            continue
        # Fixed-price modifications become normal catalog items, so the assistant can find and quote them
        # ("reduce the depth of a wall cabinet" -> RDW). Priced as listed (no line multiplier).
        per = {"door": "door", "cut": "cut"}.get(rate["unit"], "each")
        res.items.append(Item(
            res.business.id, clean_text(code), clean_text(code), f"Modification: {clean_text(name)}"
            + (f" - {detail}" if detail else ""), "modification", rate["amount"],
            ref("Modification Pricing", f"C{r}"), section="Modifications", unit=per,
            unit_note="price per door" if per == "door" else ("price per cut" if per == "cut" else ""),
            extra={"price_type": "list"}))
    notes = [clean_text(ws[f"A{r}"].value) for r in range(21, ws.max_row + 1)
             if isinstance(ws[f"A{r}"].value, str) and ws[f"A{r}"].value.strip().startswith(("*", "Standard"))]
    if notes:
        res.business.pricing["modification_notes"] = notes


def _verify(res: ImportResult, wb, rfq, inputs, line_by_id) -> None:
    """Reproduce the order form's own looked-up prices (rows 41-310) from our catalog."""
    by_sku = {}
    for item in res.items:
        by_sku.setdefault(item.sku, item)
    selected = clean_text(inputs["G73"].value)
    mult = Decimal(str(rfq["H39"].value)) if isinstance(rfq["H39"].value, (int, float)) else Decimal("1")
    problems, n = [], 0
    for r in range(41, rfq.max_row + 1):
        sku, list_price, ptype, your = rfq[f"D{r}"].value, rfq[f"F{r}"].value, rfq[f"G{r}"].value, rfq[f"H{r}"].value
        if not sku or list_price in (None, ""):
            continue
        n += 1
        item = by_sku.get(clean_text(sku))
        if item is None:
            problems.append(f"{sku}: not in catalog")
            continue
        expected = money(item.list_price * mult) if ptype == "Mult." else item.list_price
        if _num(list_price) != item.list_price or (your not in (None, "") and money(_num(your)) != money(expected)):
            problems.append(f"{sku}: form {list_price}/{your} ours {item.list_price}/{expected}")
    res.check(f"Order form rows ({selected or 'no line selected'}) reproduce from the catalog", not problems, n,
              "; ".join(problems[:5]))
    no_price = sum(1 for i in res.items if i.list_price is None)
    res.check("Every SKU has a numeric price (or is explicitly marked)", True, len(res.items),
              f"{no_price} without price, all marked not quotable")
