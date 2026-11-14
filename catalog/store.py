"""
store.py
--------
Runtime, read-only access to the product catalog for the agent and the UI.

Loading: data/catalog.db if present, otherwise data/catalog.db.enc decrypted with PRICE_SHEET_KEY.
Either way the database is loaded into MEMORY (sqlite deserialize) - nothing decrypted touches disk,
every query is a memory lookup, and a rebuilt catalog is picked up automatically (file time check).

    get_catalog()                         the shared instance (None-safe: CatalogUnavailable if missing)
    cat.lookup(codes, business, line)     exact codes -> items (aliases, hinge suffix, closest real items)
    cat.search(query, category, ...)      descriptive search (full-text, grouped across lines)
    cat.countertop_color(name)            color -> price group + rate
    cat.unit_cost(item, quantity)         wholesale cost (supplier rules, catalog/pricing.py)
    cat.unit_price(item, quantity)        customer price = cost x (1 + markup%)
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from catalog.categories import CATEGORIES
from catalog.normalize import match_key, money
from catalog.pricing import sell, unit_cost

ROOT = Path(__file__).resolve().parent.parent
STOP_WORDS = {"a", "an", "the", "in", "with", "for", "of", "and", "or", "to", "inch", "inches", "in.", "x", "by",
              "i", "need", "want", "some", "please", "my", "our", "color", "colour"}


class CatalogUnavailable(RuntimeError):
    pass


def _cents(value: Optional[int]) -> Optional[Decimal]:
    return None if value is None else (Decimal(value) / 100).quantize(Decimal("0.01"))


@dataclass
class Match:
    """One code lookup: what was asked, how it matched, and the items found."""
    query: str
    how: str                       # exact | alias | hinge | prefix | closest | none
    items: List[Dict[str, Any]]
    note: str = ""


class Catalog:
    def __init__(self, con: sqlite3.Connection, source: str):
        self.con = con
        self.source = source
        self._lock = threading.Lock()
        self.meta = {r["key"]: r["value"] for r in self._q("select key, value from meta")}
        self.businesses: Dict[str, Dict[str, Any]] = {}
        for r in self._q("select * from businesses"):
            self.businesses[r["id"]] = {"id": r["id"], "name": r["name"], "kind": r["kind"],
                                        "pricing": json.loads(r["pricing"])}
        self.lines: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for r in self._q("select * from lines order by business_id, sort"):
            self.lines[(r["business_id"], r["id"])] = {
                "business": r["business_id"], "id": r["id"], "name": r["name"], "aliases": json.loads(r["aliases"] or "[]"),
                "multiplier": Decimal(r["multiplier"]) if r["multiplier"] else None}

    # -- low level -----------------------------------------------------------------------------------

    def _q(self, sql: str, params: Sequence[Any] = ()) -> List[sqlite3.Row]:
        with self._lock:
            return self.con.execute(sql, tuple(params)).fetchall()

    def business_order(self) -> List[str]:
        return list(self.businesses)

    def line(self, business: str, line_id: Optional[str]) -> Optional[Dict[str, Any]]:
        return self.lines.get((business, line_id)) if line_id else None

    def line_name(self, business: str, line_id: Optional[str]) -> str:
        line = self.line(business, line_id)
        return line["name"] if line else ""

    def resolve_line(self, text: Optional[str], business: Optional[str] = None) -> List[Dict[str, Any]]:
        """Lines whose name / alias / id matches the text ('Colonial White', 'BF', 'brooklyn fawn')."""
        if not text:
            return []
        key = match_key(text)
        exact, partial = [], []
        for (bid, _), line in self.lines.items():
            if business and bid != business:
                continue
            names = [line["name"], line["id"], *line["aliases"]]
            keys = [match_key(n) for n in names]
            if key in keys:
                exact.append(line)
            elif len(key) >= 4 and any(key in k or (len(k) >= 4 and k in key) for k in keys):
                partial.append(line)
        return exact or partial

    def resolve_business(self, text: Optional[str]) -> Optional[str]:
        if not text:
            return None
        key = match_key(text)
        for bid, b in self.businesses.items():
            if key in (match_key(bid), match_key(b["name"])) or (len(key) >= 3 and key in match_key(b["name"])):
                return bid
        return None

    def item_dict(self, r: sqlite3.Row, quantity: int = 1) -> Dict[str, Any]:
        item = dict(r)
        item["extra"] = json.loads(r["extra"] or "{}")
        item["list_price"] = _cents(r["list_price_cents"])
        item["assembly_price"] = _cents(r["assembly_price_cents"])
        item["tiers"] = [(t["min_qty"], _cents(t["price_cents"]))
                         for t in self._q("select min_qty, price_cents from price_tiers where item_id=? order by min_qty", (r["id"],))]
        item["line_name"] = self.line_name(r["business_id"], r["line_id"])
        item["unit_cost"] = self.unit_cost(item, quantity) if r["quotable"] else None
        item["unit_price"] = sell(item["unit_cost"], r["business_id"]) if r["quotable"] else None
        return item

    def unit_cost(self, item: Dict[str, Any], quantity: int = 1) -> Optional[Decimal]:
        """Wholesale cost of one unit (supplier price list + its rules)."""
        biz = self.businesses[item["business_id"]]
        line = self.line(item["business_id"], item["line_id"])
        return unit_cost(item["list_price"], biz["pricing"], line["multiplier"] if line else None,
                         item["extra"].get("price_type"), quantity, item["tiers"])

    def unit_price(self, item: Dict[str, Any], quantity: int = 1) -> Optional[Decimal]:
        """Customer price of one unit = wholesale cost x (1 + markup%)."""
        return sell(self.unit_cost(item, quantity), item["business_id"])

    def get_item(self, item_id: int, quantity: int = 1) -> Optional[Dict[str, Any]]:
        rows = self._q("select * from items where id=?", (item_id,))
        return self.item_dict(rows[0], quantity) if rows else None

    # -- exact code lookup -----------------------------------------------------------------------------

    def _by_keys(self, keys: Sequence[str], business: Optional[str], line_ids: Optional[Sequence[str]]) -> List[sqlite3.Row]:
        if not keys:
            return []
        marks = ",".join("?" * len(keys))
        sql = f"select * from items where (code_key in ({marks}) or sku_key in ({marks}))"
        params: List[Any] = [*keys, *keys]
        if business:
            sql += " and business_id=?"
            params.append(business)
        if line_ids:
            sql += f" and (line_id in ({','.join('?' * len(line_ids))}) or line_id is null)"
            params += list(line_ids)
        return self._q(sql + " order by business_id, line_id", params)

    def lookup(self, code: str, business: Optional[str] = None, line_ids: Optional[Sequence[str]] = None) -> Match:
        key = match_key(code)
        rows = self._by_keys([key], business, line_ids)
        if rows:
            return Match(code, "exact", [self.item_dict(r) for r in rows])
        # Another name for the code (e.g. a catalog model number) -> the price-list items it maps to
        aliases = self._q("select * from aliases where alias_key=?", (key,))
        for a in aliases:
            if business and a["business_id"] != business:
                continue
            rows = self._q("select * from items where business_id=? and (code_key=? or json_extract(extra, '$.model') is not null "
                           "and replace(replace(upper(json_extract(extra, '$.model')), '-', ''), ' ', '')=?) and quotable=1",
                           (a["business_id"], a["target_key"], a["target_key"]))
            if rows:
                note = a["note"] + ("" if a["confirmed"] else " UNCONFIRMED: tell the customer a designer will confirm it.")
                return Match(code, "alias", [self.item_dict(r) for r in rows], note)
        # Trailing hinge side: B15L / B15-R -> B15 (keep the side as a note)
        m = re.fullmatch(r"(.+?)[- ]?([LR])", code.strip(), re.I)
        if m and len(match_key(m.group(1))) >= 3:
            rows = self._by_keys([match_key(m.group(1))], business, line_ids)
            if rows:
                return Match(code, "hinge", [self.item_dict(r) for r in rows],
                             f"'{code}' = {m.group(1)} with hinge side {m.group(2).upper()} (priced as {m.group(1)}; keep the side in the item note)")
        # Codes that START with it (a model with color variants: PL-150 -> PL-150 Alpina, Chroma ...)
        if len(key) >= 4:
            sql = "select * from items where (code_key like ? or sku_key like ?)"
            params: List[Any] = [key + "%", key + "%"]
            if business:
                sql += " and business_id=?"
                params.append(business)
            if line_ids:
                sql += f" and (line_id in ({','.join('?' * len(line_ids))}) or line_id is null)"
                params += list(line_ids)
            rows = self._q(sql + " and quotable=1 order by length(code_key) limit 12", params)
            if rows:
                return Match(code, "prefix", [self.item_dict(r) for r in rows],
                             f"No item is exactly '{code}'; these codes start with it (different items - ask which one).")
        # Closest real items: shorten the code until something contains it
        prefix = key
        while len(prefix) > 3:
            prefix = prefix[:-1]
            sql = "select * from items where code_key like ? and quotable=1"
            params = [f"%{prefix}%"]
            if business:
                sql += " and business_id=?"
                params.append(business)
            if line_ids:
                sql += f" and (line_id in ({','.join('?' * len(line_ids))}) or line_id is null)"
                params += list(line_ids)
            rows = self._q(sql + " order by length(code_key) limit 8", params)
            if rows:
                return Match(code, "closest", [self.item_dict(r) for r in rows],
                             f"No item '{code}'. Closest REAL items (codes containing '{prefix}') - different items: ask, never substitute.")
        return Match(code, "none", [], f"No item '{code}' in the catalog.")

    # -- descriptive search ------------------------------------------------------------------------------

    @staticmethod
    def category_keys(text: Optional[str]) -> List[str]:
        if not text:
            return []
        key = match_key(text)
        exact = [c for c in CATEGORIES if match_key(c) == key or match_key(CATEGORIES[c]) == key]
        if exact:
            return exact
        return [c for c in CATEGORIES if key and (key in match_key(c) or key in match_key(CATEGORIES[c])
                                                  or key.rstrip("S") in match_key(CATEGORIES[c]))]

    def search(self, query: str, category: Optional[str] = None, business: Optional[str] = None,
               line_ids: Optional[Sequence[str]] = None, limit: int = 8) -> List[Dict[str, Any]]:
        """Full-text search; results grouped so one product offered in 16 finishes shows once."""
        words = [w for w in re.findall(r"[a-z0-9]+(?:\.[0-9]+)?", query.lower()) if w not in STOP_WORDS]
        if not words:
            return []
        cats = self.category_keys(category)
        found: List[sqlite3.Row] = []
        for joiner in (" AND ", " OR "):  # all words first; any word if nothing matched
            fts = joiner.join(f'"{w}"' for w in words)
            sql = ("select i.* from items_fts f join items i on i.id=f.rowid where items_fts match ? and i.quotable=1")
            params: List[Any] = [fts]
            if cats:
                sql += f" and i.category in ({','.join('?' * len(cats))})"
                params += cats
            if business:
                sql += " and i.business_id=?"
                params.append(business)
            if line_ids:
                sql += f" and (i.line_id in ({','.join('?' * len(line_ids))}) or i.line_id is null)"
                params += list(line_ids)
            found = self._q(sql + " order by bm25(items_fts) limit 300", params)
            if found:
                break
        groups: Dict[Tuple[str, str], List[sqlite3.Row]] = {}
        for r in found:
            groups.setdefault((r["business_id"], r["code_key"]), []).append(r)
            if len(groups) >= limit and (r["business_id"], r["code_key"]) not in groups:
                break
        out = []
        for (bid, _), rows in list(groups.items())[:limit]:
            items = [self.item_dict(r) for r in rows]
            out.append({"business": bid, "code": items[0]["code"], "items": items})
        return out

    # -- by size ("30-inch wide wall cabinet") ------------------------------------------------------------

    SIZE_KINDS = {
        "wall": ("wall", "bridge"), "bridge": ("bridge", "wall"), "base": ("base",), "sink_base": ("sink_base",),
        "drawer_base": ("drawer_base",), "corner": ("corner",), "tall": ("tall", "oven"), "pantry": ("tall",),
        "oven": ("oven", "tall"), "vanity": ("vanity",), "any": (),
    }

    def find_by_size(self, kind: str, width: float, height: Optional[float] = None, depth: Optional[float] = None,
                     business: Optional[str] = None, line_ids: Optional[Sequence[str]] = None,
                     tolerance: float = 3.0) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Cabinets of a type and size -> (exact matches, nearest real sizes). Heights/depths that a supplier
        doesn't print (KCD standard bases / walls) don't exclude an item; a bridge is a wall cabinet <= 24" tall."""
        types = self.SIZE_KINDS.get(kind, ())

        def query(w_low: float, w_high: float, exact: bool) -> List[sqlite3.Row]:
            sql = "select * from items where category='cabinet' and width_in between ? and ?"
            params: List[Any] = [w_low, w_high]
            if types:
                sql += f" and cabinet_type in ({','.join('?' * len(types))})"
                params += list(types)
            if kind == "bridge":
                sql += " and (height_in is null or height_in <= 24)"
            if height is not None:
                sql += " and (height_in is null or height_in between ? and ?)"
                params += [height - (0 if exact else tolerance), height + (0 if exact else tolerance)]
            if depth is not None:
                sql += " and (depth_in is null or depth_in between ? and ?)"
                params += [depth - (0 if exact else tolerance), depth + (0 if exact else tolerance)]
            if business:
                sql += " and business_id=?"
                params.append(business)
            if line_ids:
                sql += f" and (line_id in ({','.join('?' * len(line_ids))}) or line_id is null)"
                params += list(line_ids)
            sql += " order by abs(width_in - ?), abs(coalesce(height_in, ?) - ?), business_id, length(code)"
            params += [width, height or 0, height or 0]
            return self._q(sql + " limit 400", params)

        exact = [self.item_dict(r) for r in query(width, width, True)]
        near = [] if exact else [self.item_dict(r) for r in query(width - tolerance, width + tolerance, False)]
        return exact, near

    # -- countertops --------------------------------------------------------------------------------------

    def countertop_business(self) -> Optional[str]:
        rows = self._q("select distinct business_id from material_groups")
        return rows[0]["business_id"] if rows else None

    def countertop_groups(self, material: Optional[str] = None) -> List[Dict[str, Any]]:
        sql, params = "select * from material_groups", []
        if material:
            sql += " where material=? or name like ?"
            params = [material.lower(), f"%{material}%"]
        return [{**dict(r), "rate": _cents(r["rate_per_sqft_cents"])} for r in self._q(sql + " order by material, id", params)]

    def countertop_color(self, name: str) -> List[Dict[str, Any]]:
        key = match_key(name)
        rows = self._q("select c.*, g.name group_name, g.material, g.rate_per_sqft_cents from colors c "
                       "left join material_groups g on g.id=c.group_id and g.business_id=c.business_id where c.name_key=?", (key,))
        if not rows and len(key) >= 4:
            rows = self._q("select c.*, g.name group_name, g.material, g.rate_per_sqft_cents from colors c "
                           "left join material_groups g on g.id=c.group_id and g.business_id=c.business_id "
                           "where c.name_key like ? limit 10", (f"%{key}%",))
        return [{**dict(r), "rate": _cents(r["rate_per_sqft_cents"])} for r in rows]

    def colors_in_group(self, group_id: str) -> List[str]:
        return [r["name"] for r in self._q("select name from colors where group_id=? order by name", (group_id,))]

    def charges(self, business: Optional[str] = None) -> List[Dict[str, Any]]:
        sql, params = "select * from charges", []
        if business:
            sql += " where business_id=?"
            params = [business]
        return [{**dict(r), "amount": _cents(r["amount_cents"])} for r in self._q(sql + " order by id", params)]

    def charge(self, business: str, code_or_name: str) -> Optional[Dict[str, Any]]:
        key = match_key(code_or_name)
        for c in self.charges(business):
            if key in (match_key(c["code"]), match_key(c["name"])):
                return c
        return None

    # -- summaries --------------------------------------------------------------------------------------

    def overview(self) -> str:
        """Compact description of what each business sells (goes into the agent's instructions)."""
        out = []
        sellable = {(r["business_id"], r["line_id"]) for r in
                    self._q("select distinct business_id, line_id from items where quotable=1 and line_id is not null")}
        for bid, b in self.businesses.items():
            lines = [l["name"] for (lb, lid), l in self.lines.items() if lb == bid and (lb, lid) in sellable]
            p = b["pricing"]
            notes = []
            if (p.get("assembly") or {}).get("type") == "percent_of_cabinet_subtotal":
                notes.append(f"assembly +{p['assembly']['percent']}% if wanted")
            elif (p.get("assembly") or {}).get("type") == "per_item":
                notes.append("assembly priced per cabinet if wanted")
            if p.get("basis") == "net_price_x_markup":
                notes.append("quartz slabs cheaper by quantity")
            if p.get("basis") == "per_sqft_by_material_group":
                groups = self.countertop_groups()
                mats = sorted({g["material"] for g in groups})
                notes.append(f"priced per sq ft by color group ({', '.join(mats)}); cutouts, edges, tear-out extra")
            out.append(f"- {bid} = {b['name']}: {b['kind']}."
                       + (f" Lines/colors: {', '.join(lines)}." if lines else "")
                       + (f" ({'; '.join(notes)})" if notes else ""))
        try:  # brands the owner sells whose price files aren't loaded yet (config/catalog.json)
            pending = json.loads((ROOT / "config" / "catalog.json").read_text()).get("pending_businesses", [])
        except Exception:  # noqa: BLE001
            pending = []
        for p in pending:
            out.append(f"- {p['name']} - NOT LOADED YET ({p.get('sells', '')}): {p.get('status', 'no prices yet')}. "
                       "Can't quote it: a designer follows up; offer the closest loaded line.")
        return "\n".join(out)

    def status(self) -> List[Dict[str, Any]]:
        counts = {r["business_id"]: r["n"] for r in self._q("select business_id, count(*) n from items where quotable=1 group by 1")}
        colors = {r["business_id"]: r["n"] for r in self._q("select business_id, count(*) n from colors group by 1")}
        return [{"id": bid, "name": b["name"], "items": counts.get(bid, 0), "colors": colors.get(bid, 0)}
                for bid, b in self.businesses.items()]


# -- loading ------------------------------------------------------------------------------------------------

_cache: Dict[str, Any] = {"key": None, "catalog": None}
_load_lock = threading.Lock()


def catalog_paths() -> Tuple[Path, Path]:
    try:
        rel = json.loads((ROOT / "config" / "catalog.json").read_text())["output"]["db"]
    except Exception:  # noqa: BLE001
        rel = "data/catalog.db"
    db = Path(os.getenv("CATALOG_DB", ROOT / rel))
    return db, db.with_suffix(db.suffix + ".enc")


def _load_bytes() -> Tuple[bytes, str, float]:
    db, enc = catalog_paths()
    if db.exists():
        return db.read_bytes(), str(db.name), db.stat().st_mtime
    if enc.exists():
        key = os.getenv("PRICE_SHEET_KEY")
        if not key:
            raise CatalogUnavailable("PRICE_SHEET_KEY is missing - needed to open the encrypted catalog")
        import zlib

        from cryptography.fernet import Fernet, InvalidToken

        try:
            data = Fernet(key.encode()).decrypt(enc.read_bytes())
        except (InvalidToken, ValueError) as exc:
            raise CatalogUnavailable("PRICE_SHEET_KEY is wrong for the encrypted catalog") from exc
        if not data.startswith(b"SQLite format 3"):
            data = zlib.decompress(data)
        return data, f"{enc.name} (decrypted in memory)", enc.stat().st_mtime
    raise CatalogUnavailable("No product catalog found - run: python scripts/build_catalog.py")


def get_catalog() -> Catalog:
    db, enc = catalog_paths()
    path = db if db.exists() else enc
    stamp = (str(path), path.stat().st_mtime) if path.exists() else None
    with _load_lock:
        if _cache["catalog"] is not None and _cache["key"] == stamp:
            return _cache["catalog"]
        data, source, _ = _load_bytes()
        con = sqlite3.connect(":memory:", check_same_thread=False)
        con.deserialize(data)
        con.row_factory = sqlite3.Row
        _cache.update(key=stamp, catalog=Catalog(con, source))
        return _cache["catalog"]


def fmt_money(value: Optional[Decimal]) -> str:
    return "-" if value is None else f"${money(value):,.2f}"
