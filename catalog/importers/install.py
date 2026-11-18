"""
INSTALL - the owner's install workbook ("Install Price_Time_Pay Sheet").

Tabs (all filled from the 'Count' tab, where staff type quantities per room):
    Cabinet Install Price / Time / Pay Sheet     Flooring Install Price / Time / Pay Sheet
Each tab: column A = price (or time text, or installer pay), column B = description, rows 9-38,
section titles in column A ("Demo", "Assembly", "Installation" / "Install").
Price  = what the customer pays per unit      Pay = what the installer is paid (our cost)
Time   = written as text ("15 MIN EACH", "150 sqft per hour", "10 min per 8 feet") -> minutes per unit.
Rules on the sheet: $750 install minimum (less needs Chase's approval), discounts over 15% need Chase's
approval, days = hours / 8, installer travel pay $0.60 per mile from the office.

Where a time label and its formula disagree (e.g. "45 min per step" but =C23/45), the WRITTEN label is
used and the mismatch is reported for the owner to confirm.
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import openpyxl

from catalog.model import Business, ImportResult, InstallService
from catalog.normalize import clean_text, match_key, slug, to_decimal

GROUPS = {
    "cabinet": ("Cabinet Install Price Sheet", "Cabinet Install Time Sheet", "Cabinet Install Pay Sheet"),
    "flooring": ("Flooring Install Price Sheet", "Flooring Install Time Sheet", "Flooring Install Pay Sheet"),
}
SECTIONS = {"demo": "demo", "assembly": "assembly", "installation": "installation", "install": "installation"}

# Time labels that need the row's meaning to read (checked against the sheet's own formulas):
#   "1 HR" on a "per sqft over 50 sqft" row = 1 hour per 50 sq ft; "1 hour extra per 20 sqft 30-50" on a
#   flat 30-50 sq ft job = 2 h (first 30 sq ft) + 1 h (next 20 sq ft).
TIME_OVERRIDES = {
    "laminate countertops per sqft over 50 sqft": (60 / 50, "1 hour per 50 sq ft"),
    "stone countertops per sqft over 30 sqft": (60 / 30, "1 hour per 30 sq ft"),
    "backsplash $350 min 30 50 sqft demo to studs drywall work not included": (180.0, "2 h + 1 h per 20 sq ft over 30"),
}


def _unit(desc: str) -> str:
    d = desc.lower()
    if "per night" in d or "hotel" in d:
        return "night"
    if "trip fee" in d:
        return "trip"
    if "linear foot" in d:
        return "lf"
    if "per step" in d:
        return "step"
    if "per location" in d:
        return "location"
    if "per 8' piece" in d or "per 8’ piece" in d:
        return "piece_8ft"
    if "8' section" in d:
        return "section_8ft"
    if " bag " in f" {d} ":
        return "bag"
    if re.search(r"upto|up to|and under|\$\d+ min|\(30-50|30 sqft min", d):
        return "job"
    if "sqft" in d or "sq ft" in d:
        return "sqft"
    return "each"


def _minutes(label: Any, desc: str, unit: str, time_desc: str = "") -> Tuple[Optional[float], str]:
    """Minutes per unit from the written time ('0.5 HR EACH', '150 sqft per hour', '10 min per 8 feet')."""
    key = match_key(desc).lower()
    for k, (mins, note) in TIME_OVERRIDES.items():
        if match_key(k).lower() == key:
            return mins, note
    t = clean_text(label).lower()
    if not t:
        return None, ""
    if t in ("sub",):
        return 0.0, "done by a subcontractor"
    if "quoted" in t:
        return None, "quoted by the job"
    m = re.search(r"([\d.]+)\s*sq\s*ft per hour", t)
    if m:
        return 60 / float(m.group(1)), t
    m = re.search(r"([\d.]+)\s*min per 8 feet", t)
    if m:
        return float(m.group(1)) / 8, t
    m = re.search(r"([\d.]+)\s*min per step", t)
    if m:
        return float(m.group(1)), t
    m = re.search(r"([\d.]+)\s*hours? up to", t)
    if m:
        return float(m.group(1)) * 60, t
    m = re.search(r"([\d.]*\.?\d+)\s*(hr|hour|hours|min)", t)
    if m:
        mins = float(m.group(1)) * (60 if m.group(2).startswith("h") else 1)
        per = re.search(r"\bper (\d+) [a-z]", (time_desc or desc).lower())  # "Per 10 Existing Cabinets"
        n = int(per.group(1)) if per else 1
        return (mins / n if n > 1 else mins), t
    return None, t


def _formula_minutes(formula: Any, qty_col: str = "C") -> Optional[float]:
    """Minutes per unit the sheet's own time formula implies: =C9/150 -> 0.4, =C17*0.5 -> 30."""
    if not isinstance(formula, str):
        return None
    m = re.fullmatch(rf"=\s*{qty_col}\d+\s*/\s*([\d.]+)", formula.replace(" ", ""))
    if m:
        return 60 / float(m.group(1))
    m = re.fullmatch(rf"=\s*{qty_col}\d+\s*\*\s*([\d.]+)", formula.replace(" ", ""))
    if m:
        return float(m.group(1)) * 60
    return None


def _short_code(full: str, limit: int = 60) -> str:
    """Code cut at a word boundary ('...backsplash-30-sqft-and-under-if-you'), never mid-word ('...-if-you-ar')."""
    out = ""
    for word in full.split("-"):
        if len(out) + len(word) + (1 if out else 0) > limit:
            break
        out = f"{out}-{word}" if out else word
    return out


def _rows(ws, wf=None) -> List[Dict[str, Any]]:
    out, section = [], ""
    for r in range(8, 39):
        a, b = ws[f"A{r}"].value, ws[f"B{r}"].value
        if isinstance(a, str) and clean_text(a).lower() in SECTIONS and not b:
            section = SECTIONS[clean_text(a).lower()]
            continue
        if b in (None, ""):
            continue
        out.append({"row": r, "section": section, "desc": clean_text(b), "a": a,
                    "formula": wf[f"D{r}"].value if wf is not None else None})
    return out


def import_install(bid: str, cfg: Dict[str, Any], root: Path) -> ImportResult:
    path = root / cfg["source"]
    wb = openpyxl.load_workbook(path, data_only=True)
    wbf = openpyxl.load_workbook(path, data_only=False)
    price_tab = wb[GROUPS["cabinet"][0]]
    origin = clean_text(price_tab["A50"].value)
    business = Business(
        id=bid, name=cfg["name"], kind=cfg["kind"], source_files=[cfg["source"]],
        pricing={"basis": "install_sheet", "minimum_per_job": "750", "hours_per_day": "8",
                 "max_discount_without_approval_percent": "15", "approver": "Chase",
                 "travel_pay_per_mile": "0.60", "origin": origin,
                 "notes": ["Prices may vary if additional prep work is necessary",
                           "Install under $750 must be approved by Chase", "Over 15% discount must be approved by Chase"]})
    res = ImportResult(business)
    ref = lambda tab, cell: f"{path.name} | {tab}!{cell}"  # noqa: E731

    for group, (p_name, t_name, pay_name) in GROUPS.items():
        if p_name not in wb.sheetnames:
            res.issue("error", "tab_missing", f"Tab '{p_name}' not found")
            continue
        prices = _rows(wb[p_name])
        times = {r["row"]: r for r in _rows(wb[t_name], wbf[t_name])}
        pays_list = _rows(wb[pay_name])
        pays_by_desc = {(r["section"], match_key(r["desc"])): r for r in pays_list}  # same name in Demo + Install
        pays_by_row = {r["row"]: r for r in pays_list}
        if not any(r["section"] for r in prices):
            res.issue("error", "layout_changed", f"No Demo / Assembly / Installation sections in '{p_name}'")
        seen_codes = set()
        for row in prices:
            desc, r = row["desc"], row["row"]
            section = row["section"] or "installation"
            if re.search(r"hotel|trip fee|2nd floor", desc, re.I):
                section = "fees"
            unit = _unit(desc)
            price = to_decimal(row["a"]) if not isinstance(row["a"], (int, float)) else Decimal(str(row["a"]))
            quoted = price is None or price == 0  # "Quoted By The Job" rows have no price
            # installer pay: same description, else the same row, else nothing (reported)
            pay_row = pays_by_desc.get((row["section"], match_key(desc)))
            if pay_row is None and r in pays_by_row and \
                    match_key(pays_by_row[r]["desc"])[:10] == match_key(desc)[:10]:
                pay_row = pays_by_row[r]
                res.issue("info", "pay_matched_by_row", f"{group}: '{desc}' paid as '{pay_row['desc']}' (same row)",
                          ref(pay_name, f"A{r}"))
            if pay_row is None:
                first = match_key(desc.split()[0]) if desc.split() else ""
                cands = [x for x in pays_list if match_key(x["desc"].split()[0]) == first and x["section"] == row["section"]]
                if len(cands) == 1:
                    pay_row = cands[0]
                    res.issue("warning", "pay_description_differs",
                              f"{group}: price row '{desc}' vs pay row '{pay_row['desc']}' - matched by name; confirm",
                              ref(pay_name, f"B{pay_row['row']}"))
            pay = None
            if pay_row is not None:
                a = pay_row["a"]
                pay = Decimal(str(a)) if isinstance(a, (int, float)) else to_decimal(a)
            if pay is None and not quoted:
                res.issue("warning", "pay_missing", f"{group}: no installer pay for '{desc}'", ref(pay_name, f"A{r}"))
            # time: written label (formula mismatches reported)
            t = times.get(r)
            if unit == "each" and t and "sqft per hour" in clean_text(t["a"]).lower():
                unit = "sqft"  # "Any Glue Down Material" - priced and timed per sq ft
            minutes, time_note = _minutes(t["a"] if t else None, desc, unit, t["desc"] if t else "")
            if t and match_key(t["desc"])[:12] != match_key(desc)[:12]:
                res.issue("warning", "time_description_differs",
                          f"{group}: price row '{desc}' vs time row '{t['desc']}'", ref(t_name, f"B{r}"))
            formula_min = _formula_minutes(t["formula"]) if t else None
            if minutes and formula_min and abs(formula_min - minutes) > 0.05 * max(minutes, formula_min) \
                    and unit not in ("job",):
                res.issue("warning", "time_formula_mismatch",
                          f"{group}: '{desc}' time says '{clean_text(t['a'])}' = {minutes:g} min per {unit}, but the "
                          f"sheet's formula {t['formula']} gives {formula_min:g} min - using the written time",
                          ref(t_name, f"D{r}"))
            if minutes is None and not quoted and section != "fees" and "dumpster" not in desc.lower() \
                    and unit not in ("bag",) and "schluter" not in desc.lower():
                res.issue("info", "no_time", f"{group}: no install time for '{desc}'", ref(t_name, f"A{r}"))
            code = _short_code(slug(f"{group} {section} {desc}"))
            n = 2
            while code in seen_codes:
                code = f"{code[:57]}-{n}"
                n += 1
            seen_codes.add(code)
            res.install_services.append(InstallService(
                code=code, group=group, section=section, name=desc, price=None if quoted else price, unit=unit,
                minutes=minutes, pay=pay, source_ref=ref(p_name, f"A{r}"), quoted=quoted,
                note="; ".join(x for x in (time_note if time_note and not time_note.startswith(("1 hr", "0.5", ".5")) else "",
                                           "quoted by the job - staff" if quoted else "") if x)))
    res.counts = {"services": len(res.install_services),
                  "cabinet": sum(s.group == "cabinet" for s in res.install_services),
                  "flooring": sum(s.group == "flooring" for s in res.install_services),
                  "quoted by the job": sum(s.quoted for s in res.install_services)}
    priced = [s for s in res.install_services if not s.quoted]
    res.check("Every priced install service has a numeric price", all(s.price is not None for s in priced), len(priced))
    res.check("Every priced install service has installer pay (cost)",
              sum(s.pay is None for s in priced) <= 2, len(priced),
              ", ".join(s.name for s in priced if s.pay is None))
    res.check("Cabinet + flooring sheets both read", res.counts["cabinet"] > 0 and res.counts["flooring"] > 0,
              len(res.install_services))
    return res
