"""
Zsolt - countertop price guide ("US Cabinets <year> Price Guide", one tab) + SQFT Program brochure (PDF).

Price guide layout (checked by header cells; any move = error "layout changed"):
    A3 Granite  (A4:B9  Group 1-6, $/sq ft)     D3 Quartz (D4:E12 Tier 1-9, $/sq ft)
    A13 Eclos   (A14:A15 "Eclos Group 2 ... $139.00" - price inside the label)
    A16 Additional Services (A17:C36)           D16 Edge Details (D17:E36)
    A38 Porcelain (A39:B40)                      D38 DEKTON (D39:E41 Group 1-3)
    A42 notes; A43:C51 porcelain services; D43:D44 Dekton edges ("... - $40 per LF")
Prices are often text ("$5/LF", "$40 Each", "NO CHARGE", "$200 + Material + Mitered Edge") -> parse_rate.
The brochure lists which color belongs to which group/tier - that decides the price, so it is
extracted into a table (never searched loosely).
"""

from __future__ import annotations

import re
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

import openpyxl

from catalog.model import Business, Charge, Color, ImportResult, MaterialGroup
from catalog.normalize import clean_text, match_key, parse_rate, slug, to_decimal

HEADERS = {"A3": "granite", "D3": "quartz", "A13": "eclos", "A16": "additional services", "D16": "edge details",
           "A38": "porcelain", "D38": "dekton"}


def _kind(name: str) -> str:
    n = name.lower()
    if "edge" in n or "bullnose" in n or "bevel" in n or "ogee" in n or "radius" in n or "eased" in n:
        return "edge"
    if "cutout" in n or "cut out" in n:
        return "cutout"
    if "tear out" in n or "tear-out" in n:
        return "removal"
    if "fee" in n or "charge" in n or "delivery" in n:
        return "fee"
    return "service"


def import_zsolt(bid: str, cfg: Dict[str, Any], root: Path) -> ImportResult:
    path = root / cfg["source"]
    ws = openpyxl.load_workbook(path, data_only=True).worksheets[0]
    ref = lambda cell: f"{path.name} | {ws.title}!{cell}"  # noqa: E731
    title = clean_text(ws["A2"].value)
    notes = [clean_text(x) for x in str(ws["A42"].value or "").split("Note:") if clean_text(x)]
    business = Business(
        id=bid, name=cfg["name"], kind=cfg["kind"], source_files=[cfg["source"], cfg.get("colors_pdf", "")],
        documents=cfg.get("documents", []),
        pricing={
            "basis": "per_sqft_by_material_group",
            "price_guide": title,
            "unit_price": "sq ft x the color's group/tier rate; plus cutouts, edges and services from the guide",
            "notes": notes,
            "needs_confirmation": [
                "Are the guide's rates selling prices for fabricated + installed countertops?",
                "Quartz is also sold as slabs by Pelican - which one should the assistant offer by default?",
            ],
        })
    res = ImportResult(business)

    for cell, expected in HEADERS.items():
        if expected not in clean_text(ws[cell].value).lower():
            res.issue("error", "layout_changed", f"Expected '{expected}' in {cell}, found {ws[cell].value!r} - "
                      "the price guide layout changed; update catalog/importers/zsolt.py", ref(cell))

    # --- material groups ($ per sq ft) ---------------------------------------------------------------
    def add_group(gid: str, material: str, name: str, value: Any, cell: str, note: str = "") -> None:
        rate = to_decimal(value)
        if rate is None or rate <= 0:
            res.issue("error", "missing_rate", f"{name}: no rate ({value!r})", ref(cell))
        res.groups.append(MaterialGroup(bid, gid, material, name, rate, ref(cell), note))

    for r in range(4, 13):
        a, d = clean_text(ws[f"A{r}"].value), clean_text(ws[f"D{r}"].value)
        if m := re.fullmatch(r"Group (\d+)", a):
            add_group(f"granite-group-{m.group(1)}", "granite", f"Granite Group {m.group(1)}", ws[f"B{r}"].value, f"B{r}")
        if m := re.fullmatch(r"Tier (\d+)", d):
            add_group(f"quartz-tier-{m.group(1)}", "quartz", f"Quartz Tier {m.group(1)}", ws[f"E{r}"].value, f"E{r}")
    for r in (14, 15):
        text = clean_text(ws[f"A{r}"].value)
        if m := re.fullmatch(r"Eclos Group (\d+)\s+\$?([\d,.]+)", text):
            add_group(f"eclos-group-{m.group(1)}", "eclos", f"Eclos Group {m.group(1)}", m.group(2), f"A{r}")
            res.issue("info", "price_in_label", f"'{text}' -> Eclos Group {m.group(1)}", ref(f"A{r}"))
    for r in (39, 40):
        label = clean_text(ws[f"A{r}"].value)
        if label:
            gid = "porcelain-6mm" if "6mm" in label.lower() else "porcelain-2cm"
            add_group(gid, "porcelain", label.split(" Eased")[0].split(" Showers")[0],
                      ws[f"B{r}"].value, f"B{r}", note=label)
    for r in (39, 40, 41):
        label = clean_text(ws[f"D{r}"].value)
        if m := re.search(r"Dekton Colors Group (\d+)", label):
            add_group(f"dekton-group-{m.group(1)}", "dekton", f"Dekton Group {m.group(1)} (2cm)", ws[f"E{r}"].value, f"E{r}")

    # --- services, cutouts, edges ------------------------------------------------------------------------
    codes: Dict[str, int] = defaultdict(int)

    def add_charge(name: str, value: Any, cell: str, scope: str, kind: Optional[str] = None) -> None:
        for part in [p for p in str(value).split("\n") if p.strip()] if isinstance(value, str) else [value]:
            rate = parse_rate(part)
            sub = ""
            if isinstance(part, str) and "\n" in str(value):  # "$125 Vanity\n$250 Kitchen" / size ranges
                sub = re.sub(r"\$?\s*\d[\d,.]*\s*(each|/lf|/sf)?", "", clean_text(part), flags=re.I).strip(" -")
            full = f"{name} - {sub}" if sub else name
            code = slug(f"{full} {scope}" if scope != "standard" else full)
            codes[code] += 1
            if codes[code] > 1:
                code = f"{code}-{codes[code]}"
            note = rate["note"]
            res.charges.append(Charge(bid, code, full, kind or _kind(name), rate["amount"], rate["unit"], ref(cell),
                                      scope=scope, needs_staff=rate["needs_staff"], note=note, raw=rate["raw"]))
            dekton = re.search(r"\(\*\$(\d+(?:\.\d+)?)\)", rate["raw"])
            if dekton:  # "$25/LF (*$40.00)" -> Dekton costs more
                res.charges.append(Charge(bid, slug(f"{full} dekton"), f"{re.sub(r'\s*\(\*Dekton\)', '', full)} (Dekton)",
                                          kind or _kind(name), Decimal(dekton.group(1)).quantize(Decimal("0.01")),
                                          rate["unit"], ref(cell), scope="dekton", needs_staff=rate["needs_staff"]))

    for r in range(17, 37):
        a, c, d, e = ws[f"A{r}"].value, ws[f"C{r}"].value, ws[f"D{r}"].value, ws[f"E{r}"].value
        if a and c not in (None, ""):
            add_charge(clean_text(a), c, f"C{r}", "standard")
        if d and e not in (None, ""):
            add_charge(clean_text(d), e, f"E{r}", "standard", kind="edge")
    for r in range(43, 52):
        a, c, d = ws[f"A{r}"].value, ws[f"C{r}"].value, ws[f"D{r}"].value
        if a and c not in (None, ""):
            add_charge(clean_text(a), c, f"C{r}", "porcelain")
        if d:  # "Sharknose Edge for DEKTON - $40 per LF"
            name, _, price = clean_text(d).rpartition(" - ")
            add_charge(name or clean_text(d), price or d, f"D{r}", "dekton", kind="edge")

    # --- colors -> group (brochure) ---------------------------------------------------------------------
    pdf = cfg.get("colors_pdf")
    if pdf and (root / pdf).exists():
        _colors(res, root / pdf)
    else:
        res.issue("error", "colors", f"Color brochure not found: {pdf}")

    res.counts = {"material groups": len(res.groups), "services / edges / cutouts": len(res.charges),
                  "colors": len(res.colors), "needs staff price": sum(c.needs_staff for c in res.charges)}
    # Services with no fixed price ("Custom Estimate Needed", "$200 + Material ...") are not offered to the
    # assistant - anything like that goes to the designer (unpriced_items). Listed in the report.
    for c in [c for c in res.charges if c.amount is None or c.needs_staff]:
        res.issue("info", "service_not_offered", f"'{c.name}' ({c.raw}) needs a staff price - not offered for "
                  "automatic quotes", c.source_ref)
        res.charges.remove(c)
    rated = {g.id for g in res.groups if g.rate_per_sqft}
    unpriced = [c.name for c in res.colors if c.group_id not in rated]
    res.check("Every color maps to exactly one priced group", not unpriced and
              len({match_key(c.name) for c in res.colors}) == len(res.colors), len(res.colors), ", ".join(unpriced[:8]))
    res.check("Every service offered has a fixed price", all(c.amount is not None and not c.needs_staff
                                                            for c in res.charges), len(res.charges))
    numeric = [g for g in res.groups if g.rate_per_sqft]
    res.check("Every material group has a numeric $/sq ft rate", len(numeric) == len(res.groups), len(res.groups))
    return res


_HEADING = re.compile(r"^(?:(2CM|12MM)\s+PORCELAIN|DEKTON\s+GROUP\s+(\d+)|(?:QUARTZ\s+)?TIER\s+(\d+)|GRANITE\s+GROUP\s+(\d+))$")
_CONTACT = re.compile(r"@|\d{3}-\d{3}-\d{4}|park lane|lake wales|,\s*fl\b", re.I)


def _colors(res: ImportResult, pdf: Path) -> None:
    from pypdf import PdfReader

    bid = res.business.id
    group: Optional[str] = None
    seen: Dict[str, List[Color]] = defaultdict(list)
    for page_no, page in enumerate(PdfReader(str(pdf)).pages, start=1):
        for raw in (page.extract_text(extraction_mode="layout") or "").splitlines():
            line = re.sub(r"\s+", " ", raw).strip()
            if not line or _CONTACT.search(line):
                continue
            m = _HEADING.match(line.upper())
            if m:
                thick, dek, tier, granite = m.groups()
                group = ("porcelain-2cm" if thick == "2CM" else "porcelain-12mm" if thick == "12MM"
                         else f"dekton-group-{dek}" if dek else f"quartz-tier-{tier}" if tier else f"granite-group-{granite}")
                continue
            if group is None:
                continue
            for name in (p.strip() for p in re.split(r"\s{3,}", raw.strip()) if p.strip()):
                parts = [clean_text(p) for p in name.split(" - ")]
                color = Color(bid, parts[0], group, "; ".join(parts[1:]), f"{pdf.name} | page {page_no}")
                res.colors.append(color)
                seen[match_key(color.name)].append(color)
    # One color, one price: a color listed in two groups keeps the HIGHER-priced group (never under-quote);
    # a color repeated in the same group keeps one entry. Both are reported.
    rate = {g.id: g.rate_per_sqft for g in res.groups}
    for key, colors in seen.items():
        groups = sorted({c.group_id for c in colors})
        keep = max(colors, key=lambda c: (rate.get(c.group_id) or 0))
        if len(groups) > 1:
            res.issue("warning", "color_in_two_groups",
                      f"'{keep.name}' is listed in {', '.join(groups)} - kept {keep.group_id} (higher price); "
                      "confirm with the client", keep.source_ref)
        for c in colors:
            if c is not keep:
                res.colors.remove(c)
    # Colors with no rate in the price guide (e.g. 12mm porcelain) can't be quoted -> removed, reported.
    for gid in sorted({c.group_id for c in res.colors} - {g for g, r in rate.items() if r}):
        names = [c.name for c in res.colors if c.group_id == gid]
        res.issue("warning", "unpriced_colors", f"Removed {len(names)} colors in '{gid}' - no rate in the price "
                  f"guide: {', '.join(names)}", str(pdf.name))
        res.colors = [c for c in res.colors if c.group_id != gid]
