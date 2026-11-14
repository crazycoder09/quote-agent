"""
quote_tools.py
--------------
The agent's tools. They give the agent ACCESS (the product catalog of every business, memory, a
calculator, the quote document); the agent decides which business, line and items fit the customer,
guided by the owner's playbook. Prices are never decided by the AI: create_quote prices every line
from the catalog with each business's rules (catalog/quote_engine.py).

Catalog (all businesses: HCC, KCD, Pelican, countertops)
    find_products          product codes -> items (item_id + price once business and line are known)
    search_products        descriptions ("white farmhouse sink", "36 inch hood") -> items
    countertop_options     countertop colors -> price group, rates, cutouts / edges / services
    find_by_size           "30-inch wide wall cabinet" -> real cabinets of that type and size (both brands)
    flooring_quantity      area (+10% waste unless included) -> boxes / sheets to order
    show_line_images       sample door + finished kitchen photos of door styles (shown in the chat)
Math
    calculator             exact arithmetic (quantities: sq ft, handles)
Clarify (human-in-the-loop)
    ask_customer           pause the graph, show clickable options, resume with the answer
Quote
    create_quote           numbered quote, one section per business; refuses anything it can't price exactly
    get_saved_quote        reopen a past quote
Long-term memory
    find_customer          returning customer: details, preferences, past quotes
    save_customer_details  remember contact details / preferences right away
    learn_rule             (staff only) teach a new business rule
Staff reports (staff only - cost and margin are never shown to customers)
    sales_report           quotes, sales, cost, margin, tax by period / business
    set_quote_status       mark a quote won (sold) / lost / quoted
Escalation
    hand_off_to_designer   flag the chat "Needs you" for a human designer
Uploads
    read_uploaded_files    what the vision worker found in the customer's layouts / photos / PDFs
"""

from __future__ import annotations

import ast
import json
import operator
import re
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.types import interrupt
from pydantic import BaseModel, Field

from agent import memory
from agent.agents import load_settings
from agent.quote_document import quote_brief
from catalog.categories import CATEGORIES
from catalog.pricing import unconfirmed, waste_percent
from catalog.store import CatalogUnavailable, fmt_money, get_catalog


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


def _configurable(config: Optional[RunnableConfig]) -> dict:
    return (config or {}).get("configurable") or {}


def _is_staff(config: Optional[RunnableConfig]) -> bool:
    return _configurable(config).get("role") == "staff"


# ---------------------------------------------------------------------------
# Catalog tools
# ---------------------------------------------------------------------------

def _flags(item: Dict[str, Any]) -> str:
    out = []
    if not item["quotable"]:
        out.append(f"NOT QUOTABLE: {item['not_quotable_reason']}")
    if item["needs_confirmation"]:
        out.append(f"NEEDS STAFF CONFIRMATION: {item['needs_confirmation']}")
    if item["unit"] != "each":
        out.append(f"per {item['unit']}" + (f" ({item['unit_note']})" if item["unit_note"] else ""))
    if item["tiers"]:
        out.append("quantity prices: " + ", ".join(f"{q}+ {fmt_money(p)}" for q, p in item["tiers"]))
    if item["extra"].get("special_order"):
        out.append("special order")
    return (" | " + " | ".join(out)) if out else ""


_STAFF_VIEW = {"on": False}  # set per tool call: staff also see the wholesale cost


def _item_row(item: Dict[str, Any]) -> str:
    price = fmt_money(item["unit_price"]) if item["unit_price"] is not None else "no price"
    line = f" | {item['line_name']}" if item["line_name"] else ""
    cost = f" (staff: cost {fmt_money(item['unit_cost'])})" if _STAFF_VIEW["on"] and item.get("unit_cost") else ""
    return (f"item_id={item['id']} | {item['business_id']}{line} | {item['code']} | {item['description']} | "
            f"{price} each{cost}{_flags(item)}")


def _short_desc(item: Dict[str, Any]) -> str:
    """KCD descriptions start with the line name ('Brooklyn Fawn Single Door ...') - drop it when grouping lines."""
    desc = item["description"]
    if item["line_name"] and desc.lower().startswith(item["line_name"].split(" (")[0].lower()):
        desc = desc[len(item["line_name"].split(" (")[0]):].strip(" -")
    return desc


def _group_rows(items: List[Dict[str, Any]], limit: int = 10) -> List[str]:
    """One row per item when it is unique; one row per product when it is offered in several lines."""
    groups: Dict[tuple, List[Dict[str, Any]]] = {}
    for it in items:
        groups.setdefault((it["business_id"], it["code_key"]), []).append(it)
    rows = []
    for (bid, _), its in list(groups.items())[:limit]:
        if len(its) == 1:
            rows.append("  " + _item_row(its[0]))
            continue
        prices = [i["unit_price"] for i in its if i["unit_price"] is not None]
        lines = [i["line_name"] or "-" for i in its]
        rows.append(f"  {bid} | {its[0]['code']} | {_short_desc(its[0])} | in {len(its)} lines: {', '.join(lines)}"
                    + (f" | {fmt_money(min(prices))}-{fmt_money(max(prices))}" if prices and min(prices) != max(prices)
                       else (f" | {fmt_money(prices[0])}" if prices else ""))
                    + f" -> call again with business=\"{bid}\" and the customer's line to get the item_id")
    if len(groups) > limit:
        rows.append(f"  ... {len(groups) - limit} more")
    return rows


def _scope(cat, business: Optional[str], line: Optional[str]) -> tuple:
    """(business id or None, [line ids] or None, error text or None)"""
    bid = None
    if business:
        bid = cat.resolve_business(business)
        if not bid:
            return None, None, f"Unknown business '{business}'. Businesses: {', '.join(cat.businesses)}"
    line_ids = None
    if line:
        found = cat.resolve_line(line, bid)
        if not found:
            names = [l["name"] for (b, _), l in cat.lines.items() if not bid or b == bid]
            return bid, None, (f"No line/color '{line}'" + (f" in {bid}" if bid else "") + ". Lines: " + ", ".join(names)
                               + ". Map the customer's color with the playbook, or ask them.")
        line_ids = [l["id"] for l in found]
        if not bid and len({l["business"] for l in found}) == 1:
            bid = found[0]["business"]
    return bid, line_ids, None


@tool
def find_products(codes: List[str], business: Optional[str] = None, line: Optional[str] = None,
                  config: RunnableConfig = None) -> str:
    """Look up product CODES in the catalog of every business, e.g. ['W2136', 'B15', 'PL-150', 'HACT3036'].
    Pass business (HCC, KCD, PELICAN) and line (the customer's color / door style / finish, e.g.
    'Colonial White', 'Brooklyn Fawn') when known: each code then returns ONE item with its item_id and
    price. Without them you see which businesses and lines offer each code. Put ALL codes of one
    business + line in one call. Results say EXACT, or NO EXACT MATCH with the closest real items
    (different items - ask the customer, never substitute)."""
    try:
        cat = get_catalog()
    except CatalogUnavailable as exc:
        return _json({"error": str(exc)})
    _STAFF_VIEW["on"] = _is_staff(config)
    bid, line_ids, err = _scope(cat, business, line)
    if err:
        return err
    out = []
    for code in codes[:40]:
        m = cat.lookup(code, bid, line_ids)
        head = {"exact": "EXACT", "alias": "EXACT (another name)", "hinge": "EXACT (hinge side)",
                "prefix": "NO EXACT MATCH", "closest": "NO EXACT MATCH", "none": "NOT FOUND"}[m.how]
        out.append(f'"{code}": {head}' + (f" - {m.note}" if m.note else ""))
        out.extend(_group_rows(m.items))
    return "\n".join(out)


@tool
def search_products(query: str, category: Optional[str] = None, business: Optional[str] = None,
                    line: Optional[str] = None, max_results: int = 8, config: RunnableConfig = None) -> str:
    """Find products by DESCRIPTION across every business, e.g. 'white farmhouse sink 33', '36 inch range hood',
    'matte black pull', 'brushed gold faucet', 'quartz slab calacatta'. Optional filters: category (one of:
    cabinet, filler_panel_molding, cabinet_accessory, hood, hardware, shelf, modification, kitchen_sink,
    bathroom_sink, sink_accessory, faucet, bathtub, toilet, shower, vanity_top, bath_accessory, flooring,
    flooring_trim, tile, quartz_slab, part), business (HCC, KCD, PELICAN) and line (color / finish).
    Products offered in several lines show once - pass business + line to get the item_id. Use real results only."""
    try:
        cat = get_catalog()
    except CatalogUnavailable as exc:
        return _json({"error": str(exc)})
    _STAFF_VIEW["on"] = _is_staff(config)
    bid, line_ids, err = _scope(cat, business, line)
    if err:
        return err
    if category and not cat.category_keys(category):
        return f"Unknown category '{category}'. Categories: {', '.join(CATEGORIES)}"
    out = []
    # Anything in the query that looks like a product code (PL-150, W2136, HACT3036) is also looked up
    # exactly - so a code is never missed because it was asked as a "search".
    for code in dict.fromkeys(re.findall(r"\b[A-Za-z]{1,8}-?\d[A-Za-z0-9\-/.]*", query)):
        m = cat.lookup(code, bid, line_ids)
        if m.how in ("exact", "hinge", "prefix"):
            out.append(f'Code "{code}": ' + ("EXACT" if m.how != "prefix" else "these codes start with it")
                       + (f" - {m.note}" if m.note and m.how != "prefix" else ""))
            out.extend(_group_rows(m.items, limit=15))
    groups = cat.search(query, category, bid, line_ids, limit=max(1, min(max_results, 15)))
    if groups:
        items = [i for g in groups for i in g["items"]]
        out.append(f"Results for '{query}':")
        out.extend(_group_rows(items, limit=len(groups)))
    if not out:
        return f"No products match '{query}'" + (f" in category {category}" if category else "") + \
            ". Try other words, or ask the customer for the code."
    return "\n".join(out)


@tool
def countertop_options(color: Optional[str] = None, material: Optional[str] = None, services: bool = False) -> str:
    """Countertops (fabricated + installed, priced per square foot by color group).
    color='Calacatta Oro' -> its price group and $/sq ft. material='quartz' (granite, quartz, dekton, porcelain,
    eclos) -> its groups, rates and colors. No arguments -> every group with its colors.
    services=true -> cutouts, edges, tear-out and fees with their codes (pass these codes to create_quote).
    Square feet = each wall run (ft) x 2 ft deep, islands length x width (use the calculator)."""
    try:
        cat = get_catalog()
    except CatalogUnavailable as exc:
        return _json({"error": str(exc)})
    bid = cat.countertop_business()
    if not bid:
        return "No countertop price guide in the catalog."
    out = []
    if color:
        found = cat.countertop_color(color)
        if not found:
            out.append(f"No countertop color '{color}'. Call countertop_options() for all colors.")
        groups_per_color: Dict[str, set] = {}
        for c in found:
            groups_per_color.setdefault(c["name_key"], set()).add(c["group_id"])
        for c in found:
            if len(groups_per_color[c["name_key"]]) > 1:
                out.append(f"{c['name']}: listed in {len(groups_per_color[c['name_key']])} price groups in the guide (data "
                           "conflict) - NOT quotable until staff confirm. Don't ask the customer to pick a price: say a "
                           "designer will confirm it, or offer another color.")
                groups_per_color[c["name_key"]] = set()  # print once
                continue
            if not groups_per_color[c["name_key"]]:
                continue
            rate = f"{fmt_money(c['rate'])}/sq ft" if c["rate"] is not None else "NO PRICE - staff must price"
            out.append(f"{c['name']} -> {c['group_name'] or c['group_id']}: {rate}" + (f" ({c['note']})" if c["note"] else ""))
    if material or not (color or services):
        for g in cat.countertop_groups(material):
            colors = cat.colors_in_group(g["id"])
            out.append(f"{g['name']}: {fmt_money(g['rate'])}/sq ft" + (f" - {g['note']}" if g["note"] else "")
                       + (f" - colors: {', '.join(colors)}" if colors else " - (no colors listed)"))
        if not out:
            out.append(f"No countertop material '{material}'. Materials: granite, quartz, eclos, dekton, porcelain")
    if services:
        out.append("Services (code | name | price | applies to):")
        for c in cat.charges(bid):
            price = "staff price" if c["amount"] is None or c["needs_staff"] else \
                f"{fmt_money(c['amount'])}" + ("" if c["unit"] == "each" else f" per {c['unit']}")
            out.append(f"  {c['code']} | {c['name']} | {price} | {c['scope']}" + (f" | {c['note']}" if c["note"] else ""))
    return "\n".join(out)


@tool
def find_by_size(cabinet_type: str, width: float, height: Optional[float] = None, depth: Optional[float] = None,
                 business: Optional[str] = None, line: Optional[str] = None, config: RunnableConfig = None) -> str:
    """Find cabinets by TYPE and SIZE when the customer describes them instead of giving a code
    ("30-inch wide wall cabinet", "36 x 15 bridge over the fridge", "18 inch drawer base").
    cabinet_type: wall, bridge (short wall cabinet above a fridge/range/window, <= 24" tall; over a fridge
    usually 24" deep), base, sink_base, drawer_base, corner, tall, pantry, oven, vanity, any.
    Sizes in inches: width (required), height and depth when given. Pass business + line (the color) to get
    item_ids. Returns exact sizes, or the nearest real sizes (ask - never substitute silently)."""
    try:
        cat = get_catalog()
    except CatalogUnavailable as exc:
        return _json({"error": str(exc)})
    _STAFF_VIEW["on"] = _is_staff(config)
    kind = cabinet_type.strip().lower().replace(" ", "_").replace("-", "_")
    kind = {"upper": "wall", "lower": "base", "sink": "sink_base", "drawer": "drawer_base", "drawers": "drawer_base",
            "lazy_susan": "corner", "blind_corner": "corner", "utility": "tall", "refrigerator": "bridge",
            "over_fridge": "bridge", "fridge": "bridge"}.get(kind, kind)
    if kind not in cat.SIZE_KINDS:
        return f"Unknown cabinet_type '{cabinet_type}'. Use one of: {', '.join(cat.SIZE_KINDS)}"
    bid, line_ids, err = _scope(cat, business, line)
    if err:
        return err
    exact, near = cat.find_by_size(kind, float(width), height, depth, bid, line_ids)
    size = f'{width:g}"W' + (f' x {height:g}"H' if height else "") + (f' x {depth:g}"D' if depth else "")
    if exact:
        return f"{kind} {size}: EXACT SIZE\n" + "\n".join(_group_rows(exact, limit=12))
    if near:
        return (f"{kind} {size}: NO EXACT SIZE. Nearest REAL sizes (different items - ask the customer, never "
                "substitute):\n" + "\n".join(_group_rows(near, limit=12)))
    return f"No {kind} cabinet near {size}" + (f" in {bid}" if bid else "") + ". Ask for the code or other sizes."


@tool
def flooring_quantity(item_id: int, area_sqft: float, area_includes_waste: bool) -> str:
    """How many boxes (flooring) or sheets (tile / mosaic) to order for an area. Before calling, ASK the
    customer whether their square footage already includes waste; if not, pass area_includes_waste=false
    and the recommended waste (10%) is added. Returns the quantity to put in create_quote."""
    import math

    try:
        cat = get_catalog()
    except CatalogUnavailable as exc:
        return _json({"error": str(exc)})
    item = cat.get_item(int(item_id))
    if item is None:
        return _json({"error": f"item_id {item_id} does not exist"})
    per = item["extra"].get("sqft_per_unit")
    if not per:
        return _json({"error": f"{item['sku']} has no coverage per {item['unit']} - ask how many to order"})
    waste = waste_percent()
    area = float(area_sqft) * (1 if area_includes_waste else 1 + float(waste) / 100)
    qty = math.ceil(round(area / float(per), 6))
    unit = "boxes" if item["unit"] == "box" else "sheets" if item["category"] == "tile" else "units"
    return _json({"item_id": item["id"], "code": item["code"], "quantity": qty, "unit": unit,
                  "coverage_per_unit_sqft": per, "area_ordered_for_sqft": round(area, 2),
                  "covers_sqft": round(qty * float(per), 2),
                  "waste": "included by the customer" if area_includes_waste else f"+{waste:g}% added (recommended)",
                  "next": f"Use quantity={qty} for item_id {item['id']} in create_quote. Tell the customer the area, "
                          "the waste allowance and how many " + unit + " that is."})


def _image_root():
    from pathlib import Path

    return Path(__file__).resolve().parent.parent / "assets" / "catalog_images"


@tool
def show_line_images(lines: List[str], business: Optional[str] = None) -> str:
    """Show the customer the SAMPLE DOOR and a FINISHED KITCHEN photo of door styles / colors, e.g.
    lines=['Colonial White', 'Colonial Navy']. Use it when they are choosing or comparing colors, ask what a
    color looks like, or before they commit to a line. The photos appear in the chat under your reply."""
    try:
        cat = get_catalog()
    except CatalogUnavailable as exc:
        return _json({"error": str(exc)})
    bid = cat.resolve_business(business) if business else None
    shown, missing = [], []
    for name in lines[:6]:
        found = cat.resolve_line(name, bid)
        if not found:
            missing.append(f"{name} (not a line in the catalog)")
            continue
        line = found[0]
        folder = _image_root() / line["business"] / line["id"]
        door, kitchen = folder / "door.jpg", folder / "kitchen.jpg"
        if not door.exists() and not kitchen.exists():
            missing.append(f"{line['name']} (no photos yet)")
            continue
        shown.append({"business": line["business"], "line": line["name"],
                      "door": str(door.relative_to(_image_root().parent.parent)) if door.exists() else None,
                      "kitchen": str(kitchen.relative_to(_image_root().parent.parent)) if kitchen.exists() else None})
    return _json({"images": shown, "missing": missing,
                  "note": "The photos are displayed to the customer under your reply - don't describe the image files."})


# ---------------------------------------------------------------------------
# Calculator
# ---------------------------------------------------------------------------

_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.Pow: operator.pow, ast.Mod: operator.mod,
    ast.USub: operator.neg, ast.UAdd: operator.pos,
}


def _eval(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.left), _eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "round":
        return round(*[_eval(a) for a in node.args])
    raise ValueError("Only numbers, + - * / ** %, parentheses and round() are allowed.")


@tool
def calculator(expression: str) -> str:
    """Evaluate arithmetic exactly, e.g. '12*2 + 14*2' or '412*2 + 15.5'.
    Always use this instead of doing math in your head."""
    try:
        return _json({"expression": expression, "result": _eval(ast.parse(expression, mode="eval"))})
    except Exception as exc:  # noqa: BLE001
        return _json({"error": str(exc)})


# ---------------------------------------------------------------------------
# Quote
# ---------------------------------------------------------------------------

class QuoteLine(BaseModel):
    item_id: int = Field(description="From find_products/search_products")
    code: str = Field(description="Code shown with that item_id (checked)")
    quantity: int = Field(ge=1)
    note: Optional[str] = Field(default=None, description="e.g. hinge side")


class CountertopService(BaseModel):
    code: str = Field(description="From countertop_options(services=true)")
    quantity: float = Field(default=1, description="Count, or linear ft (/LF), or sq ft (/SF)")


class Countertop(BaseModel):
    label: str = Field(default="Countertop", description="e.g. Kitchen, Island")
    color: str = Field(description="As listed by countertop_options")
    sqft: float = Field(gt=0)
    services: List[CountertopService] = Field(default_factory=list)
    note: Optional[str] = None


class QuoteExtra(BaseModel):
    label: str = Field(description="e.g. Installation (estimate)")
    quantity: float
    unit: str
    rate: float = Field(description="Per unit, from the playbook")
    detail: str = Field(description="How the quantity was worked out")


class CreateQuoteInput(BaseModel):
    customer_name: str
    customer_phone: str
    customer_email: Optional[str] = None
    customer_address: Optional[str] = None
    items: List[QuoteLine] = Field(default_factory=list, description="Products from any business")
    countertops: List[Countertop] = Field(default_factory=list)
    assembled: bool = Field(description="Cabinets assembled?")
    shipping: bool = Field(description="Delivery/shipping (true) or pickup (false) - delivery needs the address")
    installation: bool = Field(default=False, description="We install (priced from the install sheet, with time "
                                                           "estimate; installed items are not taxed) - needs the address")
    extras: List[QuoteExtra] = Field(default_factory=list, description="Other playbook estimates only (not installation)")
    unpriced_items: List[str] = Field(default_factory=list, description="For a designer to price/confirm")
    revises: Optional[str] = Field(default=None, description="Quote number this one replaces (edits)")
    source_upload_id: Optional[str] = Field(default=None, description="When items came from attached files")
    customer_confirmed_list: bool = Field(default=False, description="True after the customer said yes to the list")
    notes: str = ""


@tool(args_schema=CreateQuoteInput)
def create_quote(
    customer_name: str,
    customer_phone: str,
    assembled: bool,
    shipping: bool,
    customer_email: Optional[str] = None,
    customer_address: Optional[str] = None,
    installation: bool = False,
    items: Optional[List[Any]] = None,
    countertops: Optional[List[Any]] = None,
    extras: Optional[List[Any]] = None,
    unpriced_items: Optional[List[str]] = None,
    revises: Optional[str] = None,
    source_upload_id: Optional[str] = None,
    customer_confirmed_list: bool = False,
    notes: str = "",
    config: RunnableConfig = None,
) -> str:
    """Create the numbered quote. Every price comes from the catalog (never from you): one section per
    business; assembly, quantity prices, countertop rates, installation (price + time), and sales tax are
    applied by the owner's rules. Shows the quote card + PDF to the customer. Returns problems to fix if
    anything can't be priced exactly."""
    from catalog.quote_engine import price_quote

    try:
        cat = get_catalog()
    except CatalogUnavailable as exc:
        return _json({"status": "not_created", "problems": [str(exc)]})
    as_dict = lambda x: x if isinstance(x, dict) else x.model_dump()  # noqa: E731
    items_ = [as_dict(i) for i in items or []]
    counters = [{**as_dict(c), "services": [as_dict(s) for s in as_dict(c).get("services") or []]} for c in countertops or []]
    extras_ = [as_dict(x) for x in extras or []]
    priced, problems, warnings = price_quote(cat, items_, counters, assembled, shipping, extras_, installation)
    if (installation or shipping) and not (customer_address or "").strip():
        problems.append("Installation and delivery need the customer's address - ask for it (street, city, ZIP), "
                        "then call create_quote again with customer_address.")

    thread_id = _configurable(config).get("thread_id")
    upload = (memory.get_thread(thread_id) or {}).get("active_upload") if thread_id else None
    if upload and not upload.get("quoted"):
        # Quote built from uploaded files: every line must be accounted for, and the customer must confirm.
        lines = upload.get("lines", [])
        covered = len(items_) + len(counters) + len(unpriced_items or [])
        if covered < len(lines):
            problems.append(
                f"The customer's files have {len(lines)} lines but this quote accounts for only {covered} "
                f"({len(items_)} items + {len(counters)} countertops + {len(unpriced_items or [])} unpriced). Every line "
                "must be a quoted item or listed in unpriced_items (e.g. 'B15 trash pull-out - designer to confirm'). "
                "Lines from the files: " + "; ".join(lines))
        if not customer_confirmed_list:
            problems.append("These items came from the customer's files: show the final list (qty x code, plus anything "
                            "unpriced) with ask_customer and get a yes first, then call create_quote with "
                            "customer_confirmed_list=true.")
    if memory.customer_key(customer_phone) is None:
        problems.append("A valid customer phone number is required.")
    if problems:
        return _json({"status": "not_created", "problems": problems,
                      "fix": "Correct these (look items up again if needed), then call create_quote again."})

    settings = load_settings()
    company = settings.get("company", {})
    cust_key = memory.upsert_customer(customer_phone, name=customer_name, email=customer_email, address=customer_address)
    today = date.today()
    quote = {
        "quote_number": "",
        "format": 2,
        "created_at": memory.now(),
        "date": today.isoformat(),
        "valid_until": (today + timedelta(days=int(company.get("quote_valid_days", 30)))).isoformat(),
        "company": {k: company.get(k, "") for k in ("name", "phone", "email", "address")},
        "customer": {"name": customer_name, "phone": customer_phone,
                     "email": customer_email or "", "address": customer_address or ""},
        **priced,
        "unpriced_items": unpriced_items or [],
        "assembled": assembled,
        "shipping": shipping,
        "installation_requested": installation,
        "status": "quoted",
        "notes": notes,
        "revises": revises,
        "footer": company.get("quote_footer", ""),
        "thread_id": thread_id,
        "catalog_built": cat.meta.get("built_at"),
    }
    with memory.quote_lock:  # number + save together: parallel users never get the same number
        quote["quote_number"] = memory.next_quote_number(company.get("quote_number_prefix", "HQ-"))
        memory.save_quote(cust_key, quote)
    if upload and not upload.get("quoted") and thread_id:
        memory.update_thread(thread_id, active_upload={**upload, "quoted": True})
    if thread_id:
        memory.update_thread(thread_id, customer=cust_key, status=f"Quote {quote['quote_number']}")

    return _json({
        "status": "created",
        "quote_number": quote["quote_number"],
        **({"revises": revises} if revises else {}),
        "sections": [{"business": s["business"], "lines": s["lines"],
                      "items": [{"code": i["code"], "qty": i["quantity"], "unit_price": i["unit_price"], "total": i["total"]}
                                for i in s["items"]],
                      "assembly_fee": s["assembly_fee"], "total": s["total"]} for s in priced["sections"]],
        **({"countertops": [{"label": p["label"], "color": p["color"], "sqft": p["sqft"], "total": p["total"]}
                            for p in priced["countertops"]["pieces"]]} if priced["countertops"] else {}),
        **({"installation": {"total": priced["installation"]["total"], "estimated_hours": priced["installation"]["hours"],
                             "estimated_days": priced["installation"]["days"]}} if priced.get("installation") else {}),
        "extras": [{"label": x["label"], "amount": x["amount"]} for x in priced["extras"]],
        "sales_tax": priced["totals"]["tax"], "tax_rate_percent": priced["totals"]["tax_rate_percent"],
        "grand_total": priced["totals"]["grand_total"], "valid_until": quote["valid_until"],
        **({"staff_only": {"wholesale_cost": priced["totals"]["cost"], "margin": priced["totals"]["margin"],
                           "margin_percent": priced["totals"]["margin_percent"],
                           "placeholders_in_use": unconfirmed()}} if _is_staff(config) else {}),
        **({"warnings": warnings} if warnings else {}),
        "shown_to_customer": "The quote card with a PDF download is displayed below your reply - don't repeat the "
                             "table. Mention sales tax / installation time if they apply.",
    })


@tool
def get_saved_quote(quote_number: str, phone: str) -> str:
    """Reopen a past quote by its number (e.g. HQ-1003) to review or revise it.
    Requires the phone number the quote was made for."""
    quote = memory.get_quote(quote_number)
    if not quote or memory.customer_key(quote["customer"]["phone"]) != memory.customer_key(phone):
        return _json({"found": False})
    return "found:\n" + quote_brief(quote, full=True)


# ---------------------------------------------------------------------------
# Long-term memory tools
# ---------------------------------------------------------------------------

@tool
def find_customer(phone: Optional[str] = None, name: Optional[str] = None, config: RunnableConfig = None) -> str:
    """Look up a returning customer: saved contact details, everything we remember
    about them (preferences, kitchen details) and their past quotes.
    Customers are looked up by phone; staff may also search by name."""
    if phone:
        key = memory.customer_key(phone)
        profile = memory.get_customer(key) if key else None
        if not profile:
            return _json({"found": False})
        thread_id = _configurable(config).get("thread_id")
        if thread_id:
            memory.update_thread(thread_id, customer=key)
        past = [quote_brief(q) for q in memory.quotes_for_customer(key)]
        return _json({"found": True, "profile": profile, "past_quotes": past})
    if name:
        if not _is_staff(config):
            return _json({"found": False, "message": "For privacy, ask the customer for their phone number."})
        return _json({"matches": memory.find_customers_by_name(name)})
    return _json({"error": "Give a phone number (or a name, staff only)."})


@tool
def save_customer_details(
    phone: str,
    name: Optional[str] = None,
    email: Optional[str] = None,
    address: Optional[str] = None,
    facts: Optional[List[str]] = None,
    config: RunnableConfig = None,
) -> str:
    """Remember a customer right away: contact details plus short facts worth
    keeping for future chats (e.g. 'prefers white shaker', 'L-shaped kitchen 12ft x 14ft',
    'wants installation', 'remodel finishing in March')."""
    key = memory.upsert_customer(phone, name=name, email=email, address=address, facts=facts)
    if not key:
        return _json({"saved": False, "reason": "A valid phone number is required."})
    thread_id = _configurable(config).get("thread_id")
    if thread_id:
        memory.update_thread(thread_id, customer=key)
    return _json({"saved": True})


@tool
def learn_rule(rule: str, config: RunnableConfig = None) -> str:
    """STAFF ONLY: permanently learn a business rule the owner/staff teaches,
    e.g. 'Espresso color goes to JSI' or 'BCB36 from layouts = HCC BBC42/45'.
    It is applied in every future conversation."""
    if not _is_staff(config):
        return _json({"saved": False, "reason": "Only staff can teach business rules."})
    memory.add_learned_rule(rule)
    return _json({"saved": True, "rule": rule})


# ---------------------------------------------------------------------------
# Staff reports: margin tracking + sales (never offered to customers)
# ---------------------------------------------------------------------------

def _latest_quotes(quotes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One quote per job. Revisions form a chain (HQ-1002 revises HQ-1001): the version marked won counts
    (that's what was sold); otherwise the latest version counts."""
    by_number = {q["quote_number"]: q for q in quotes}

    def root(q):
        seen = set()
        while q.get("revises") in by_number and q["quote_number"] not in seen:
            seen.add(q["quote_number"])
            q = by_number[q["revises"]]
        return q["quote_number"]

    chains: Dict[str, List[Dict[str, Any]]] = {}
    for q in quotes:
        chains.setdefault(root(q), []).append(q)
    out = []
    for chain in chains.values():
        won = [q for q in chain if q.get("status") == "won"]
        pick = won or chain
        out.append(max(pick, key=lambda q: (q.get("created_at", ""), q["quote_number"])))
    return out


@tool
def sales_report(start_date: Optional[str] = None, end_date: Optional[str] = None, status: str = "all",
                 config: RunnableConfig = None) -> str:
    """STAFF ONLY: sales and profit-margin report from the saved quotes. Dates YYYY-MM-DD (default: last 30
    days, inclusive). status: all | quoted | won | lost. Shows quote count and value, won (sold) value,
    wholesale cost, margin $ and %, sales tax, installation, per business, and the biggest quotes."""
    if not _is_staff(config):
        return _json({"error": "Reports are for staff only."})
    from collections import defaultdict
    from datetime import date as _date

    end = _date.fromisoformat(end_date) if end_date else _date.today()
    start = _date.fromisoformat(start_date) if start_date else end - timedelta(days=30)
    quotes = [q for q in _latest_quotes(memory.all_quotes())
              if start.isoformat() <= str(q.get("date", "")) <= end.isoformat()]
    tracked = [q for q in quotes if "cost" in q.get("totals", {})]
    if status != "all":
        tracked = [q for q in tracked if q.get("status", "quoted") == status]
    if not tracked:
        return _json({"period": f"{start} to {end}", "quotes": 0,
                      "note": "No quotes with cost tracking in this period" + (f" (status {status})" if status != "all" else "")})
    money2 = lambda v: round(float(v), 2)  # noqa: E731

    def summarize(qs):
        t = lambda k: sum(q["totals"].get(k, 0) for q in qs)  # noqa: E731
        products = t("items_subtotal") + t("assembly_fee") + t("countertops")
        return {"quotes": len(qs), "quoted_value": money2(t("grand_total")), "products_value": money2(products),
                "wholesale_cost": money2(t("cost")), "margin": money2(t("margin")),
                "margin_percent": round(t("margin") / products * 100, 1) if products else 0.0,
                "sales_tax": money2(t("tax")), "installation": money2(t("installation"))}

    by_business = defaultdict(lambda: {"value": 0.0, "cost": 0.0})
    for q in tracked:
        for sec in q.get("sections", []):
            by_business[sec["business"]]["value"] += sec["total"]
            by_business[sec["business"]]["cost"] += sec.get("cost", 0)
        if q.get("countertops"):
            by_business[q["countertops"]["business"]]["value"] += q["countertops"]["total"]
            by_business[q["countertops"]["business"]]["cost"] += q["countertops"].get("cost", 0)
    won = [q for q in tracked if q.get("status") == "won"]
    report = {
        "period": f"{start} to {end}", "status_filter": status,
        "all_quotes": summarize(tracked),
        "won_sales": summarize(won) if won else {"quotes": 0},
        "conversion_rate_percent": round(len(won) / len(tracked) * 100, 1),
        "by_business": {b: {"value": money2(v["value"]), "cost": money2(v["cost"]), "margin": money2(v["value"] - v["cost"]),
                            "margin_percent": round((v["value"] - v["cost"]) / v["value"] * 100, 1) if v["value"] else 0.0}
                        for b, v in sorted(by_business.items())},
        "biggest_quotes": [{"quote": q["quote_number"], "customer": q["customer"]["name"], "date": q["date"],
                            "status": q.get("status", "quoted"), "total": q["totals"]["grand_total"],
                            "margin_percent": q["totals"]["margin_percent"]}
                           for q in sorted(tracked, key=lambda q: -q["totals"]["grand_total"])[:5]],
        "placeholders_in_use": unconfirmed(),
        "not_tracked": len(quotes) - len([q for q in quotes if "cost" in q.get("totals", {})]),
    }
    return _json(report)


@tool
def set_quote_status(quote_number: str, status: str, note: str = "", config: RunnableConfig = None) -> str:
    """STAFF ONLY: record what happened to a quote - status won (sold / ordered), lost, or quoted. Sales
    reports count won quotes as sales."""
    if not _is_staff(config):
        return _json({"error": "Only staff can change a quote's status."})
    status = status.strip().lower()
    status = {"sold": "won", "ordered": "won", "order": "won", "open": "quoted"}.get(status, status)
    if status not in ("quoted", "won", "lost"):
        return _json({"error": "status must be won, lost or quoted"})
    q = memory.set_quote_status(quote_number, status, note)
    if not q:
        return _json({"error": f"Quote {quote_number} not found"})
    newer = [x["quote_number"] for x in memory.all_quotes() if x.get("revises") == q["quote_number"]]
    return _json({"saved": True, "quote": q["quote_number"], "status": status, "total": q["totals"]["grand_total"],
                  **({"note": f"{q['quote_number']} was later revised by {', '.join(newer)} - if the customer ordered "
                              "the revised version, mark that one instead."} if newer else {})})


# ---------------------------------------------------------------------------
# Human-in-the-loop: clarify with clickable options
# ---------------------------------------------------------------------------

@tool
def ask_customer(question: str, options: List[str], allow_multiple: bool = False) -> str:
    """Pause and ask the customer to choose when their info is wrong, missing or
    ambiguous (unknown code, color we don't carry, unclear quantity, assembled or
    not, which business/line/color ...). Give 2-8 short, concrete options taken from the
    catalog/playbook (all of them when a product has up to 8 colors); the customer clicks one (or
    several if allow_multiple) or types their own answer. Call it ALONE (not together with other
    tools). Returns the answer."""
    answer = interrupt({"question": question, "options": options[:8], "allow_multiple": allow_multiple})
    return _json({"customer_answer": answer})


@tool
def hand_off_to_designer(reason: str, summary: str, urgency: str = "normal", config: RunnableConfig = None) -> str:
    """Flag this chat for a human designer when you can't resolve it well: the customer
    asks for a person, stays frustrated after you tried, needs something outside the
    catalog (custom sizes, design advice, a brand not loaded), or reports a
    problem with an order. `summary` = what they need + what's done so far.
    urgency: 'normal' or 'urgent'. Then tell the customer a designer will follow up."""
    thread_id = _configurable(config).get("thread_id")
    if not thread_id:
        return _json({"saved": False})
    memory.add_handoff(thread_id, reason, summary, urgency if urgency in ("normal", "urgent") else "normal")
    return _json({"saved": True, "status": "A designer has been notified and will follow up."})


@tool
def read_uploaded_files(upload_id: str, config: RunnableConfig = None) -> str:
    """Get what was found in the files (layout drawings, photos of lists, PDFs) the customer
    attached - codes exactly as written, quantities, locations, colors, measurements,
    customer details and anything unsure. Treat it as a draft to check, not as final."""
    from agent.uploads import jobs

    thread_id = _configurable(config).get("thread_id")
    if not thread_id:
        return _json({"error": "No conversation id."})
    st = jobs.status(thread_id, upload_id)
    if st.get("state") == "done":
        text = jobs.result_text(thread_id, upload_id)
        if not text:
            return _json({"error": "Result missing."})
        lines = jobs.extracted_lines(thread_id, upload_id)
        memory.update_thread(thread_id, active_upload={"id": upload_id, "lines": lines, "quoted": False})
        return text + (
            "\n\nNEXT (keep it to a few steps): 1) convert designer codes per the playbook (BER36-L->LS36; a trailing "
            "L/R hinge side is found automatically - no question needed); 2) pick the business + line from the color on "
            "the document (playbook), then ONE find_products call with ALL codes for that business + line; "
            "3) for each code with NO EXACT MATCH or UNSURE, ask_customer with the real alternatives; 4) ONE "
            "ask_customer showing the final list (qty x code) for a yes; 5) create_quote with the item_ids, "
            "source_upload_id=" + upload_id + " and customer_confirmed_list=true.")
    if st.get("state") == "error":
        return _json({"state": "error", "detail": st.get("error"),
                      "next": "Apologise, and ask the customer to type the list or try a clearer photo/PDF."})
    return _json({"state": st.get("state"), "progress": st.get("progress"), "status": st.get("status"),
                  "next": "Still reading - tell the customer you're reading their files and will continue "
                          "automatically when done. Don't guess the contents."})


CUSTOMER_TOOLS = [
    find_products, search_products, find_by_size, countertop_options, flooring_quantity, show_line_images, calculator,
    ask_customer, create_quote, get_saved_quote, find_customer, hand_off_to_designer, read_uploaded_files,
]
STAFF_TOOLS = CUSTOMER_TOOLS + [learn_rule, sales_report, set_quote_status]

TOOL_LABELS = {
    "find_products": "Looking up your items",
    "search_products": "Searching the catalog",
    "countertop_options": "Checking countertop colors and prices",
    "find_by_size": "Finding cabinets by size",
    "flooring_quantity": "Working out boxes needed",
    "show_line_images": "Showing door styles",
    "sales_report": "Preparing the sales report",
    "set_quote_status": "Updating the quote status",
    "calculator": "Calculating",
    "ask_customer": "Asking you to choose",
    "create_quote": "Creating the quote",
    "get_saved_quote": "Opening a saved quote",
    "find_customer": "Checking customer history",
    "save_customer_details": "Saving customer details",
    "learn_rule": "Learning a new rule",
    "hand_off_to_designer": "Handing over to a designer",
    "read_uploaded_files": "Reading your files",
}
