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


def _item_row(item: Dict[str, Any]) -> str:
    price = fmt_money(item["unit_price"]) if item["unit_price"] is not None else "no price"
    line = f" | {item['line_name']}" if item["line_name"] else ""
    return (f"item_id={item['id']} | {item['business_id']}{line} | {item['code']} | {item['description']} | "
            f"{price} each{_flags(item)}")


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
def find_products(codes: List[str], business: Optional[str] = None, line: Optional[str] = None) -> str:
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
                    line: Optional[str] = None, max_results: int = 8) -> str:
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
    shipping: bool = Field(description="Shipping (true) or pickup (false)")
    extras: List[QuoteExtra] = Field(default_factory=list, description="Playbook estimates only (installation)")
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
    business, assembly / quantity prices / countertop rates applied by the business's rules. Shows the
    quote card + PDF to the customer. Returns problems to fix if anything can't be priced exactly."""
    from catalog.quote_engine import price_quote

    try:
        cat = get_catalog()
    except CatalogUnavailable as exc:
        return _json({"status": "not_created", "problems": [str(exc)]})
    as_dict = lambda x: x if isinstance(x, dict) else x.model_dump()  # noqa: E731
    items_ = [as_dict(i) for i in items or []]
    counters = [{**as_dict(c), "services": [as_dict(s) for s in as_dict(c).get("services") or []]} for c in countertops or []]
    extras_ = [as_dict(x) for x in extras or []]
    priced, problems, _warnings = price_quote(cat, items_, counters, assembled, shipping, extras_)

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
        "extras": [{"label": x["label"], "amount": x["amount"]} for x in priced["extras"]],
        "grand_total": priced["totals"]["grand_total"], "valid_until": quote["valid_until"],
        "shown_to_customer": "The quote card with a PDF download is displayed below your reply - don't repeat the table.",
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
    find_products, search_products, countertop_options, calculator,
    ask_customer, create_quote, get_saved_quote, find_customer, hand_off_to_designer, read_uploaded_files,
]
STAFF_TOOLS = CUSTOMER_TOOLS + [learn_rule]

TOOL_LABELS = {
    "find_products": "Looking up your items",
    "search_products": "Searching the catalog",
    "countertop_options": "Checking countertop colors and prices",
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
