"""
HCC - Heritage Craft Cabinetry, Signature Series ("Worksheet" + "Order" tabs).

Layout (verified 2026-11-13, also in knowledge/sheet_guides/HCC.md):
    Worksheet row 27 = header; products from row 29 until "Totals:"; section titles in column A.
    Columns L..X = list price (MSRP) of each of the 13 color lines (order of the "DOOR STYLE" choices).
    H7 = multiplier. Your price = MSRP x multiplier (I = F*H7). Assembly fee = 10% of the order total.
    Order tab = a filled sample order -> used to prove our numbers match the workbook's.
"""

from __future__ import annotations

import difflib
import re
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict

import openpyxl

from catalog.categories import cabinet_type, classify_trim_or_accessory
from catalog.model import Business, ImportResult, Item, Line
from catalog.normalize import clean_text, match_key, money, parse_dims, to_decimal


def _num(value: Any) -> Decimal | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return Decimal(str(value))
    return to_decimal(value)


def import_hcc(bid: str, cfg: Dict[str, Any], root: Path) -> ImportResult:
    path = root / cfg["source"]
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb[cfg["tab"]]

    def ref(cell: str, tab: str = cfg["tab"]) -> str:
        return f"{path.name} | {tab}!{cell}"

    multiplier = _num(ws[cfg["multiplier_cell"]].value)
    assembly_pct = Decimal(str(cfg.get("assembly_percent_default", 10)))
    tab, _, cell = cfg["assembly_label_cell"].partition("!")
    label = clean_text(wb[tab][cell].value) if tab in wb.sheetnames else ""
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", label)
    business = Business(
        id=bid, name=cfg["name"], kind=cfg["kind"], source_files=[cfg["source"]], documents=cfg.get("documents", []),
        pricing={
            "basis": "list_price_x_multiplier",
            "multiplier": str(multiplier) if multiplier is not None else None,
            "multiplier_source": f"{cfg['tab']}!{cfg['multiplier_cell']}",
            "unit_price": "list price of the chosen line x multiplier, rounded to cents",
            "assembly": {"type": "percent_of_cabinet_subtotal", "percent": str(assembly_pct),
                         "source": cfg["assembly_label_cell"], "when": "only if the customer wants it assembled"},
            "shipping": "not priced - the office sends a shipping quote",
        })
    res = ImportResult(business)
    if multiplier is None or not (Decimal("0.05") <= multiplier <= Decimal("2")):
        res.issue("error", "multiplier", f"Multiplier in {cfg['multiplier_cell']} is not a sensible number: "
                  f"{ws[cfg['multiplier_cell']].value!r}", ref(cfg["multiplier_cell"]))
    if m:
        assembly_pct = Decimal(m.group(1))
        business.pricing["assembly"]["percent"] = str(assembly_pct)
    else:
        res.issue("warning", "assembly", f"Assembly % label not found in {cfg['assembly_label_cell']}; "
                  f"using {assembly_pct}%", cfg["assembly_label_cell"])

    # --- lines: customer-facing names from config, checked against the sheet's own choice list -----
    first, last = cfg["line_choice_rows"]
    sheet_names = [clean_text(ws[f"B{r}"].value) for r in range(first, last + 1)]
    if len(cfg["lines"]) != len(sheet_names):
        res.issue("error", "lines", f"Sheet lists {len(sheet_names)} door styles, config has {len(cfg['lines'])}")
    for i, spec in enumerate(cfg["lines"]):
        sheet_name = sheet_names[i] if i < len(sheet_names) else ""
        internal = clean_text(ws[f"{spec['column']}{cfg['header_row']}"].value)
        if sheet_name != spec["name"]:
            ratio = difflib.SequenceMatcher(None, sheet_name.lower(), spec["name"].lower()).ratio()
            if ratio >= 0.8:
                res.issue("info", "line_name_typo", f"Sheet says '{sheet_name}', using '{spec['name']}'", ref(f"B{first + i}"))
            else:
                res.issue("error", "line_order", f"Door style #{i + 1} in the sheet is '{sheet_name}' but config "
                          f"expects '{spec['name']}' (column {spec['column']}) - the layout changed", ref(f"B{first + i}"))
        res.lines.append(Line(bid, spec["id"], spec["name"], internal_name=internal, multiplier=multiplier,
                              aliases=[a for a in {sheet_name, internal} if a and a != spec["name"]], sort=i))

    # --- products ----------------------------------------------------------------------------------
    section = cfg.get("first_section", "")
    rows = 0
    for r in range(cfg["first_product_row"], ws.max_row + 1):
        a, b = ws[f"A{r}"].value, ws[f"B{r}"].value
        if isinstance(a, str) and a.strip().lower().startswith("totals"):
            break
        if a not in (None, "") and b in (None, "") and isinstance(a, str):
            section = clean_text(a)
            continue
        if b in (None, ""):
            continue
        rows += 1
        sku = clean_text(b)
        code = re.sub(r"\(.*?\)", "", sku).strip()  # 'SC8(batten molding)' -> 'SC8'
        if code != sku:
            res.issue("info", "code_cleaned", f"'{sku}' -> code '{code}' (note in brackets removed)", ref(f"B{r}"))
        desc = clean_text(ws[f"C{r}"].value)
        w, h, d = parse_dims(desc)
        if section.lower() == "sample":
            category = "sample"
        elif section.lower() == "accessories":
            category = classify_trim_or_accessory(f"{sku} {desc}")
        else:
            category = "cabinet"
        extra = {"cabinet_type": cabinet_type(f"{section} {desc}")} if category == "cabinet" else {}
        for line, spec in zip(res.lines, cfg["lines"]):
            col = spec["column"]
            price = _num(ws[f"{col}{r}"].value)
            item = Item(bid, sku, code, desc, category, price, ref(f"{col}{r}"), line=line.id, section=section,
                        weight_lb=ws[f"G{r}"].value if isinstance(ws[f"G{r}"].value, (int, float)) else None,
                        volume_m3=ws[f"H{r}"].value if isinstance(ws[f"H{r}"].value, (int, float)) else None,
                        width_in=w, height_in=h, depth_in=d, extra=dict(extra))
            if price is None or price <= 0:
                item.quotable, item.not_quotable_reason = False, "no price in the sheet"
                res.issue("error" if category != "sample" else "warning", "missing_price",
                          f"{sku} has no price for {line.name}", ref(f"{col}{r}"))
            res.items.append(item)
    res.counts = {"product rows": rows, "lines": len(res.lines), "items (rows x lines)": len(res.items),
                  "sections": len({i.section for i in res.items})}

    _verify(res, wb, ws, cfg, multiplier, assembly_pct, ref)
    return res


def _verify(res: ImportResult, wb, ws, cfg, multiplier, assembly_pct, ref) -> None:
    """Prove our reading matches the workbook's own formulas (values Excel cached when it was saved)."""
    if multiplier is None:
        return
    by_key = {(i.line, match_key(i.sku)): i for i in res.items}
    first, last = cfg["line_choice_rows"]
    chosen = next((i for i, r in enumerate(range(first, last + 1)) if ws[f"A{r}"].value == 1), None)

    # 1. Column F (MSRP of the line currently chosen in the sheet) and column I (your price = F x H7).
    if chosen is not None:
        line = res.lines[chosen]
        bad_f, bad_i, n = [], [], 0
        for item in (i for i in res.items if i.line == line.id):
            r = int(re.search(r"(\d+)$", item.source_ref).group(1))
            f, i_val = _num(ws[f"F{r}"].value), _num(ws[f"I{r}"].value)
            n += 1
            if f != item.list_price:
                bad_f.append(f"row {r}: F={f} ours={item.list_price}")
            if i_val is not None and money(item.list_price * multiplier) != money(i_val):
                bad_i.append(f"row {r}: I={i_val} ours={money(item.list_price * multiplier)}")
        res.check(f"Worksheet column F = our price for the selected line ({line.name})", not bad_f, n, "; ".join(bad_f[:5]))
        res.check("Worksheet column I (your price) = our price x multiplier", not bad_i, n, "; ".join(bad_i[:5]))
    else:
        res.check("Worksheet selected line found", False, 0, "No line marked with 1 in column A")

    # 2. The Order tab: a real filled order (line, items, totals).
    if "Order" not in wb.sheetnames:
        return
    order = wb["Order"]
    line_name = clean_text(order["C9"].value)
    line = next((l for l in res.lines if l.name.lower() == line_name.lower() or line_name in l.aliases), None)
    if line is None:
        res.check("Order tab line recognised", False, 0, f"'{line_name}' is not a known line")
        return
    problems, total, n = [], Decimal("0"), 0
    for r in range(13, 63):
        sku, qty = order[f"B{r}"].value, order[f"A{r}"].value
        if not sku:
            continue
        n += 1
        item = by_key.get((line.id, match_key(sku)))
        if item is None:
            problems.append(f"{sku}: not in catalog")
            continue
        unit = money(item.list_price * multiplier)
        line_total = money(unit * int(qty or 1))
        total += line_total
        f, i_val, j = _num(order[f"F{r}"].value), _num(order[f"I{r}"].value), _num(order[f"J{r}"].value)
        if f != item.list_price or money(i_val) != unit or money(j) != line_total:
            problems.append(f"{sku}: sheet F/I/J={f}/{i_val}/{j} ours={item.list_price}/{unit}/{line_total}")
    sheet_total = _num(order["J64"].value)
    if sheet_total is not None and money(sheet_total) != money(total):
        problems.append(f"total: sheet {sheet_total} ours {total}")
    res.check(f"Order tab sample order ({n} items, {line.name}) reproduces exactly", not problems, n,
              "; ".join(problems[:5]) or f"total {money(total)}")
