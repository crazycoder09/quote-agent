"""
quote_document.py
-----------------
How a saved quote is shown: the downloadable PDF and short text summaries for the agent's memory.
Presentation only - every number comes from create_quote (catalog/quote_engine.py).

Quotes have one SECTION PER BUSINESS (HCC, KCD, Pelican ...) plus an optional countertop section,
laid out like a supplier "Quick Quote": a header per section (line, assembly, shipping), the item
table (Quantity | Item # | Description | Unit price | Total), the section's assembly fee and total,
then a summary with the grand total. Quotes saved before multi-business support (one brand, flat
item list) are converted on the fly by normalize_quote().
"""

from __future__ import annotations

import unicodedata
from typing import Any, Dict, List

from fpdf import FPDF
from fpdf.fonts import FontFace

PLUM = (43, 23, 69)
SLATE = (100, 116, 139)
INK = (15, 23, 42)
BAND = (243, 238, 255)


def normalize_quote(q: Dict[str, Any]) -> Dict[str, Any]:
    """Return the quote in the sectioned shape (old single-brand quotes are converted)."""
    if q.get("sections") is not None:
        return q
    t = q.get("totals", {})
    pct = q.get("assembly_fee_percent", 0)
    items = [{**i, "line": q.get("line", ""), "code": i.get("sku"), "item_id": None} for i in q.get("items", [])]
    section = {"business": q.get("brand", ""), "business_name": q.get("brand_display", q.get("brand", "")),
               "lines": [q.get("line", "")] if q.get("line") else [], "assembled": bool(q.get("assembled")),
               "assembly_label": f"Assembly fee ({pct:g}%)" if q.get("assembled") else "",
               "items": items, "item_count": sum(i["quantity"] for i in items),
               "subtotal": t.get("subtotal", 0.0), "assembly_fee": t.get("assembly_fee", 0.0),
               "total": round(t.get("subtotal", 0.0) + t.get("assembly_fee", 0.0), 2)}
    return {**q, "sections": [section], "countertops": None, "extras": q.get("extras", []),
            "totals": {"items_subtotal": t.get("subtotal", 0.0), "assembly_fee": t.get("assembly_fee", 0.0),
                       "countertops": 0.0, "extras_total": t.get("extras_total", 0.0),
                       "grand_total": t.get("grand_total", 0.0)}, "legacy": True}


def section_title(s: Dict[str, Any]) -> str:
    return s["business_name"] + (f" - {', '.join(s['lines'])}" if s.get("lines") else "")


def quote_brief(q: Dict[str, Any], full: bool = False) -> str:
    """One line (full=False) or the complete content with item_ids (full=True, for edits)."""
    q = normalize_quote(q)
    total = q["totals"]["grand_total"]
    if not full:
        parts = []
        for s in q["sections"]:
            items = ", ".join(f"{i['quantity']} x {i.get('code') or i['sku']}" for i in s["items"])
            parts.append(f"{s['business']}{' ' + '/'.join(s['lines']) if s.get('lines') else ''}: {items}")
        if q.get("countertops"):
            parts.append("countertops: " + ", ".join(f"{p['color']} {p['sqft']:g} sq ft" for p in q["countertops"]["pieces"]))
        return f"{q['quote_number']} ({q['date']}): {'; '.join(parts)}; total ${total:,.2f}"
    head = (f"{q['quote_number']} ({q['date']}) - {'assembled' if q.get('assembled') else 'not assembled'}, "
            f"{'shipping' if q.get('shipping') else 'pickup'}" + (f", revises {q['revises']}" if q.get("revises") else ""))
    lines = [head]
    for s in q["sections"]:
        lines.append(f"  [{s['business']}] {section_title(s)}")
        for i in s["items"]:
            ref = f"item_id {i['item_id']}, " if i.get("item_id") else ""
            lines.append(f"    - {i['quantity']} x {i.get('code') or i['sku']} ({ref}{i.get('line') or 'no line'}) "
                         f"{i['description'][:60]}" + (f" [{i['note']}]" if i.get("note") else ""))
    if q.get("countertops"):
        for p in q["countertops"]["pieces"]:
            svc = ", ".join(f"{x['code']} x{x['quantity']:g}" for x in p["services"])
            lines.append(f"  [countertop] {p['label']}: {p['color']} {p['sqft']:g} sq ft" + (f"; services: {svc}" if svc else ""))
    for x in q.get("extras", []):
        lines.append(f"  - extra: {x['label']} {x['quantity']} {x['unit']} @ {x['rate']} ({x['detail']})")
    if q.get("unpriced_items"):
        lines.append(f"  - not priced: {', '.join(q['unpriced_items'])}")
    if q.get("legacy"):
        lines.append("  (older quote format: look the items up again with find_products before editing)")
    lines.append(f"  total ${total:,.2f}")
    return "\n".join(lines)


def _latin1(text: Any) -> str:
    """Core PDF fonts are Latin-1: normalize fancy characters (e.g. full-width brackets)."""
    text = unicodedata.normalize("NFKC", str(text or ""))
    for a, b in (("‑", "-"), ("–", "-"), ("—", "-"), ("‘", "'"), ("’", "'"),
                 ("“", '"'), ("”", '"'), ("×", "x")):
        text = text.replace(a, b)
    return text.encode("latin-1", "replace").decode("latin-1")


def _money(x: float) -> str:
    return f"${x:,.2f}"


class _QuotePDF(FPDF):
    footer_text = ""

    def footer(self) -> None:
        self.set_y(-12)
        self.set_font("Helvetica", "", 8)
        self.set_text_color(*SLATE)
        self.cell(0, 5, _latin1(self.footer_text), align="L")
        self.cell(0, 5, f"Page {self.page_no()} of {{nb}}", align="R")


def render_quote_pdf(q: Dict[str, Any]) -> bytes:
    q = normalize_quote(q)
    t, cust, comp = q["totals"], q["customer"], q["company"]
    pdf = _QuotePDF(format="Letter")
    pdf.footer_text = f"Quote {q['quote_number']}"
    pdf.set_auto_page_break(auto=True, margin=16)
    pdf.set_margins(15, 14, 15)
    pdf.add_page()
    width = pdf.w - 30

    # Title (company + "Quote") and quote details
    pdf.set_text_color(*INK)
    pdf.set_font("Helvetica", "B", 20)
    pdf.cell(0, 10, _latin1(comp.get("name") or "Quote"), align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 7, "Quote", align="C", new_x="LMARGIN", new_y="NEXT")
    contact = "  |  ".join(x for x in (comp.get("address"), comp.get("phone"), comp.get("email")) if x)
    if contact:
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(*SLATE)
        pdf.cell(0, 5, _latin1(contact), align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    top = pdf.get_y()
    pdf.set_text_color(*INK)
    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(90, 5, "Prepared for", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    for line in (cust["name"], cust["phone"], cust.get("email"), cust.get("address")):
        if line:
            pdf.cell(90, 5, _latin1(line), new_x="LMARGIN", new_y="NEXT")
    left_bottom = pdf.get_y()
    pdf.set_y(top)
    meta = [("Quotation number:", q["quote_number"]), ("Date:", q["date"]), ("Valid until:", q["valid_until"])]
    if q.get("revises"):
        meta.append(("Revises:", q["revises"]))
    for label, value in meta:
        pdf.set_x(125)
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(32, 5, label)
        pdf.set_font("Helvetica", "", 9)
        pdf.cell(0, 5, _latin1(value), new_x="LMARGIN", new_y="NEXT")
    pdf.set_y(max(left_bottom, pdf.get_y()) + 5)

    head = FontFace(emphasis="BOLD", fill_color=BAND, color=INK)
    shipping = "yes" if q.get("shipping") else "no (pickup)"

    def section_header(title: str, details: str) -> None:
        if pdf.get_y() > pdf.h - 60:
            pdf.add_page()
        pdf.set_draw_color(*PLUM)
        pdf.set_fill_color(*PLUM)
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(width, 7, _latin1(title), fill=True, new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(*INK)
        pdf.set_fill_color(255, 255, 255)  # the item table must not inherit the band color
        pdf.set_font("Helvetica", "", 9)
        pdf.cell(width, 6, _latin1(details), border="B", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(1)

    def right_row(label: str, amount: float, bold: bool = False) -> None:
        pdf.set_font("Helvetica", "B" if bold else "", 9.5 if bold else 9)
        pdf.cell(width - 30, 6, _latin1(label), align="R")
        pdf.cell(30, 6, _money(amount), align="R", new_x="LMARGIN", new_y="NEXT")

    # One section per business
    for s in q["sections"]:
        multi_line = len(s.get("lines") or []) > 1
        details = ((f"Line: {', '.join(s['lines'])}    " if s.get("lines") else "")
                   + f"Assembly: {'yes' if s.get('assembled') else 'no'}    Shipping: {shipping}")
        section_header(s["business_name"], details)
        pdf.set_font("Helvetica", "", 8)
        with pdf.table(col_widths=(12, 38, 86, 24, 25), text_align=("CENTER", "LEFT", "LEFT", "RIGHT", "RIGHT"),
                       headings_style=head, line_height=5, borders_layout="HORIZONTAL_LINES", width=width) as table:
            table.row(["Qty", "Item #", "Description", "Unit price", "Total"])
            for i in s["items"]:
                desc = i["description"]
                if multi_line and i.get("line"):
                    desc = f"[{i['line']}] {desc}"
                if i.get("unit") and i["unit"] != "each":
                    desc += f" (per {i['unit']}{', ' + i['unit_note'] if i.get('unit_note') else ''})"
                if i.get("note"):
                    desc += f" - {i['note']}"
                table.row([str(i["quantity"]), _latin1(i.get("sku") or i.get("code")), _latin1(desc),
                           _money(i["unit_price"]), _money(i["total"])])
        pdf.ln(1)
        if s.get("assembly_fee"):
            right_row(f"{s['assembly_label'] or 'Assembly'}:", s["assembly_fee"])
        count = s["item_count"]
        right_row(f"{count} item{'s' if count != 1 else ''}  -  {s['business_name']} total:", s["total"], bold=True)
        pdf.ln(4)

    # Countertops (priced per square foot)
    ct = q.get("countertops")
    if ct:
        section_header(ct["business_name"], f"Fabricated and installed, priced per square foot    Shipping: {shipping}")
        pdf.set_font("Helvetica", "", 8)
        with pdf.table(col_widths=(18, 92, 25, 25, 25), text_align=("CENTER", "LEFT", "RIGHT", "RIGHT", "RIGHT"),
                       headings_style=head, line_height=5, borders_layout="HORIZONTAL_LINES", width=width) as table:
            table.row(["Qty", "Description", "Unit", "Rate", "Total"])
            for p in ct["pieces"]:
                label = f"{p['label']}: {p['color']} ({p['group']})" + (f" - {p['note']}" if p.get("note") else "")
                table.row([f"{p['sqft']:g}", _latin1(label), "sq ft", _money(p["rate"]), _money(p["material_amount"])])
                for x in p["services"]:
                    unit = {"lf": "linear ft", "sqft": "sq ft"}.get(x["unit"], x["unit"])
                    table.row([f"{x['quantity']:g}", _latin1(f"   {x['name']}"), unit, _money(x["rate"]), _money(x["amount"])])
        pdf.ln(1)
        right_row(f"{ct['business_name']} total:", ct["total"], bold=True)
        pdf.ln(4)

    # Summary
    if pdf.get_y() > pdf.h - 70:
        pdf.add_page()
    pdf.set_draw_color(*INK)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(width, 7, "Summary", border="B", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(1)
    for s in q["sections"]:
        right_row(f"{s['business_name']}:", s["total"])
    if ct:
        right_row(f"{ct['business_name']}:", ct["total"])
    for x in q.get("extras", []):
        right_row(f"{x['label']} - {x['detail']}:", x["amount"])
    pdf.line(pdf.w - 15 - 80, pdf.get_y() + 1, pdf.w - 15, pdf.get_y() + 1)
    pdf.ln(2)
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(width - 40, 8, "Total" + (" before shipping" if q.get("shipping") else ""), align="R")
    pdf.cell(40, 8, _money(t["grand_total"]), align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    # Notes
    pdf.set_font("Helvetica", "", 8.5)
    pdf.set_text_color(*SLATE)
    notes: List[str] = []
    if q.get("unpriced_items"):
        notes.append("To be confirmed by our designer (not priced): " + ", ".join(q["unpriced_items"]))
    notes.append("Shipping requested - the office will send a separate shipping quote."
                 if q.get("shipping") else "Pickup / no shipping.")
    if q.get("notes"):
        notes.append("Notes: " + q["notes"])
    if q.get("footer"):
        notes.append(q["footer"])
    for n in notes:
        pdf.multi_cell(0, 4.5, _latin1(n), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(1)
    return bytes(pdf.output())
