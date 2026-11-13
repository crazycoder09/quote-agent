"""
build.py
--------
Runs every importer, links businesses, validates, and publishes:

    data/catalog.db                  the product catalog the agent will use
    data/reports/catalog_report.md   what was imported, every check, every issue (for a human)
    data/exports/master_catalog.xlsx one clean sheet per kind of data, for the client to review

Nothing is published when an import has errors (unless forced) - the previous catalog stays.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

from catalog.categories import label
from catalog.importers import IMPORTERS
from catalog.model import Business, ImportResult
from catalog.pricing import unit_price
from catalog.validate import validate

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "catalog.json"


@dataclass
class BuildOutcome:
    results: List[ImportResult]
    errors: int
    warnings: int
    failed_checks: int
    published: bool
    db_path: Path
    report_path: Path
    export_path: Optional[Path]


def load_config(path: Path = CONFIG) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12] if path.exists() else "missing"


def run_imports(cfg: Dict[str, Any], root: Path = ROOT, only: Optional[List[str]] = None) -> List[ImportResult]:
    results = []
    for bid, bcfg in cfg["businesses"].items():
        if only and bid not in only:
            continue
        source = root / bcfg["source"]
        if not source.exists():
            res = ImportResult(Business(bid, bcfg.get("name", bid), bcfg.get("kind", ""), [bcfg["source"]], {}))
            res.issue("error", "source_missing", f"Source file not found: {bcfg['source']}")
            results.append(res)
            continue
        try:
            results.append(IMPORTERS[bcfg["importer"]](bid, bcfg, root))
        except Exception as exc:  # noqa: BLE001 - a broken file must not crash the others; it is reported
            res = ImportResult(Business(bid, bcfg.get("name", bid), bcfg.get("kind", ""), [bcfg["source"]], {}))
            res.issue("error", "import_failed", f"{type(exc).__name__}: {exc}")
            results.append(res)
    validate(results)
    return results


def build(cfg: Optional[Dict[str, Any]] = None, root: Path = ROOT, out_dir: Optional[Path] = None,
          force: bool = False, export: bool = True) -> BuildOutcome:
    from catalog.db import write_catalog

    cfg = cfg or load_config()
    results = run_imports(cfg, root)
    out = cfg["output"]
    base = out_dir or root
    db_path, report_path = base / out["db"], base / out["report"]
    export_path = base / out["export"] if export else None

    errors = sum(1 for r in results for i in r.issues if i.severity == "error")
    warnings = sum(1 for r in results for i in r.issues if i.severity == "warning")
    failed = sum(1 for r in results for c in r.checks if not c.passed)
    publish = force or (errors == 0 and failed == 0)
    meta = {f"source:{bid}": f"{b['source']} sha256:{_sha(root / b['source'])}" for bid, b in cfg["businesses"].items()}
    if publish:
        write_catalog(db_path, results, meta)
        if export_path:
            write_export(export_path, results)
    write_report(report_path, results, cfg, root, published=publish, db_path=db_path)
    return BuildOutcome(results, errors, warnings, failed, publish, db_path, report_path, export_path if publish else None)


# ---------------------------------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------------------------------

def write_report(path: Path, results: List[ImportResult], cfg: Dict[str, Any], root: Path, published: bool,
                 db_path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    out = [f"# Catalog build report", "", f"Built {datetime.now():%Y-%m-%d %H:%M}. "
           + (f"**Published** to `{db_path.relative_to(root) if db_path.is_relative_to(root) else db_path}`."
              if published else "**NOT published** - fix the errors below (the previous catalog is unchanged)."), ""]
    out += ["## Summary", "", "| Business | Items | Quotable | Lines | Charges | Color groups | Colors | Checks | Errors | Warnings |",
            "|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        sev = Counter(i.severity for i in r.issues)
        ok = sum(c.passed for c in r.checks)
        out.append(f"| {r.business.id} | {len(r.items)} | {sum(i.quotable for i in r.items)} | {len(r.lines)} | "
                   f"{len(r.charges)} | {len(r.groups)} | {len(r.colors)} | {ok}/{len(r.checks)} | "
                   f"{sev['error']} | {sev['warning']} |")
    out += ["", "## Sources", ""]
    for bid, b in cfg["businesses"].items():
        p = root / b["source"]
        stamp = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d") if p.exists() else "missing"
        out.append(f"- **{bid}**: `{b['source']}` (modified {stamp}, sha256 {_sha(p)})")
    for r in results:
        b = r.business
        out += ["", f"## {b.id} - {b.name}", "", f"_{b.kind}_", ""]
        if r.counts:
            out.append("Counts: " + ", ".join(f"{k}: {v}" for k, v in r.counts.items()))
            out.append("")
        if b.pricing:
            rules = {k: v for k, v in b.pricing.items() if k not in ("needs_confirmation", "notes")}
            out.append("Pricing rules: `" + json.dumps(rules, ensure_ascii=False) + "`")
            out.append("")
        cats = Counter(i.category for i in r.items if i.quotable)
        if cats:
            out.append("Quotable items by category: " + ", ".join(f"{label(c)} {n}" for c, n in cats.most_common()))
            out.append("")
        out += ["**Checks against the workbook**", ""]
        for c in r.checks:
            out.append(f"- {'PASS' if c.passed else '**FAIL**'} - {c.name} ({c.checked} checked)"
                       + (f" - {c.detail}" if c.detail else ""))
        if not r.checks:
            out.append("- (none)")
        needs = list(b.pricing.get("needs_confirmation", [])) if b.pricing else []
        if needs:
            out += ["", "**Needs the client's confirmation**", ""] + [f"- {n}" for n in needs]
        grouped = defaultdict(list)
        for i in r.issues:
            grouped[(i.severity, i.kind)].append(i)
        if grouped:
            out += ["", "**Issues** (error = blocks publishing, warning = a person should look, info = fixed automatically)", ""]
            order = {"error": 0, "warning": 1, "info": 2}
            for (sev, kind), items in sorted(grouped.items(), key=lambda kv: (order[kv[0][0]], kv[0][1])):
                out.append(f"- **{sev}** `{kind}` x{len(items)}")
                for i in items[:6]:
                    out.append(f"    - {i.message}" + (f" _({i.ref})_" if i.ref else ""))
                if len(items) > 6:
                    out.append(f"    - ... {len(items) - 6} more (see the Issues sheet in the export)")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------------------------------
# Review export (Excel)
# ---------------------------------------------------------------------------------------------------

def write_export(path: Path, results: List[ImportResult]) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    head_font, head_fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="2B1745")

    def sheet(title: str, headers: List[str], data: List[List[Any]], widths: Optional[Dict[int, int]] = None) -> None:
        ws = wb.create_sheet(title)
        ws.append(headers)
        for c in ws[1]:
            c.font, c.fill = head_font, head_fill
        for row in data:
            ws.append([float(v) if isinstance(v, Decimal) else v for v in row])
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        for i, h in enumerate(headers, start=1):
            ws.column_dimensions[get_column_letter(i)].width = (widths or {}).get(i, max(10, min(45, len(h) + 4)))

    items = []
    for r in results:
        lines = {l.id: l for l in r.lines}
        for it in r.items:
            line = lines.get(it.line)
            price = unit_price(it.list_price, r.business.pricing, line.multiplier if line else None,
                               it.extra.get("price_type"), 1, it.tiers) if it.quotable else None
            items.append([r.business.id, line.name if line else "", it.code, it.sku, it.description, label(it.category),
                          it.section, it.unit, it.unit_note, it.list_price, price,
                          "; ".join(f"{q}+: {p}" for q, p in it.tiers), it.assembly_price,
                          it.width_in, it.height_in, it.depth_in, "yes" if it.quotable else "no",
                          it.not_quotable_reason, it.extra.get("needs_confirmation", ""), it.source_ref])
    sheet("Items", ["Business", "Line / color", "Code", "Supplier SKU", "Description", "Category", "Section",
                    "Unit", "Unit note", "List price", "Unit price (rules applied)", "Quantity breaks",
                    "Assembly price", "Width in", "Height in", "Depth in", "Quotable", "Why not quotable",
                    "Needs confirmation", "Source"], items, {5: 60, 20: 55})
    sheet("Countertop rates", ["Business", "Group", "Material", "Name", "$ per sq ft", "Note", "Source"],
          [[g.business, g.id, g.material, g.name, g.rate_per_sqft, g.note, g.source_ref] for r in results for g in r.groups])
    sheet("Countertop colors", ["Business", "Color", "Group", "Note", "Source"],
          [[c.business, c.name, c.group_id, c.note, c.source_ref] for r in results for c in r.colors])
    sheet("Services & charges", ["Business", "Code", "Name", "Kind", "Amount", "Unit", "Applies to", "Needs staff price",
                                 "Note", "As written", "Source"],
          [[c.business, c.code, c.name, c.kind, c.amount, c.unit, c.scope, "yes" if c.needs_staff else "",
            c.note, c.raw, c.source_ref] for r in results for c in r.charges], {3: 40, 9: 50})
    sheet("Not offered", ["Business", "Line / color", "Supplier SKU", "Description", "Why not offered", "Source"],
          [[r.business.id, it.line or "", it.sku, it.description, it.not_quotable_reason, it.source_ref]
           for r in results for it in r.items if not it.quotable], {4: 60, 5: 45})
    sheet("Checks", ["Business", "Check", "Result", "Rows checked", "Detail"],
          [[c.business, c.name, "PASS" if c.passed else "FAIL", c.checked, c.detail] for r in results for c in r.checks],
          {2: 70, 5: 60})
    sheet("Issues", ["Business", "Severity", "Kind", "Message", "Source"],
          [[i.business, i.severity, i.kind, i.message, i.ref] for r in results for i in r.issues], {4: 90, 5: 45})
    del wb["Sheet"]
    tmp = path.with_suffix(".tmp.xlsx")
    wb.save(tmp)
    tmp.replace(path)


def encrypt_catalog(db_path: Path, key: str) -> Path:
    """Compressed + encrypted copy for deployments from a public repo (PRICE_SHEET_KEY).
    The app decrypts it in memory (catalog/store.py) - prices never sit on disk in plain text."""
    import zlib

    from cryptography.fernet import Fernet

    enc = db_path.with_suffix(db_path.suffix + ".enc")
    enc.write_bytes(Fernet(key.encode()).encrypt(zlib.compress(db_path.read_bytes(), 9)))
    return enc


def summary_lines(outcome: BuildOutcome) -> List[str]:
    lines = []
    for r in outcome.results:
        sev = Counter(i.severity for i in r.issues)
        ok = sum(c.passed for c in r.checks)
        lines.append(f"  {r.business.id:8s} items {len(r.items):5d} (quotable {sum(i.quotable for i in r.items):5d}) "
                     f"| checks {ok}/{len(r.checks)} | errors {sev['error']} warnings {sev['warning']} info {sev['info']}")
    return lines


