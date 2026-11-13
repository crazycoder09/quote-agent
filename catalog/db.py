"""
db.py
-----
Writes the catalog to one SQLite file (data/catalog.db) and opens it for reading.

Why SQLite: the catalog is read-only at runtime and ~15k rows, so every app instance can carry its
own copy - no database server, no network hop, thousands of reads per second. Its full-text index
(FTS5) gives fast keyword search. Prices are stored as integer cents (no float rounding).
The schema is plain SQL, so it can move to PostgreSQL unchanged when needed.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List

from catalog.categories import label
from catalog.model import ImportResult
from catalog.normalize import cents, match_key

SCHEMA_VERSION = "1"

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE businesses (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT, pricing TEXT NOT NULL,
    source_files TEXT, documents TEXT
);
CREATE TABLE lines (
    business_id TEXT NOT NULL, id TEXT NOT NULL, name TEXT NOT NULL, internal_name TEXT,
    multiplier TEXT, aliases TEXT, sort INTEGER, PRIMARY KEY (business_id, id)
);
CREATE TABLE items (
    id INTEGER PRIMARY KEY, business_id TEXT NOT NULL, line_id TEXT,
    sku TEXT NOT NULL, sku_key TEXT NOT NULL, code TEXT NOT NULL, code_key TEXT NOT NULL,
    description TEXT, category TEXT NOT NULL, section TEXT, cabinet_type TEXT,
    list_price_cents INTEGER, unit TEXT, unit_note TEXT, assembly_price_cents INTEGER,
    weight_lb REAL, volume_m3 REAL, width_in REAL, height_in REAL, depth_in REAL,
    quotable INTEGER NOT NULL, not_quotable_reason TEXT, needs_confirmation TEXT,
    source_ref TEXT NOT NULL, extra TEXT
);
CREATE INDEX items_code ON items (code_key);
CREATE INDEX items_sku ON items (sku_key);
CREATE INDEX items_business_line ON items (business_id, line_id, code_key);
CREATE INDEX items_category ON items (category, business_id);
CREATE TABLE price_tiers (
    item_id INTEGER NOT NULL, min_qty INTEGER NOT NULL, price_cents INTEGER NOT NULL,
    PRIMARY KEY (item_id, min_qty)
);
CREATE TABLE charges (
    id INTEGER PRIMARY KEY, business_id TEXT NOT NULL, code TEXT NOT NULL, name TEXT NOT NULL, kind TEXT,
    amount_cents INTEGER, unit TEXT, scope TEXT, needs_staff INTEGER, note TEXT, raw TEXT, source_ref TEXT
);
CREATE TABLE material_groups (
    business_id TEXT NOT NULL, id TEXT NOT NULL, material TEXT, name TEXT, rate_per_sqft_cents INTEGER,
    note TEXT, source_ref TEXT, PRIMARY KEY (business_id, id)
);
CREATE TABLE colors (
    id INTEGER PRIMARY KEY, business_id TEXT NOT NULL, name TEXT NOT NULL, name_key TEXT NOT NULL,
    group_id TEXT, note TEXT, source_ref TEXT
);
CREATE INDEX colors_name ON colors (name_key);
CREATE TABLE aliases (
    alias TEXT NOT NULL, alias_key TEXT NOT NULL, business_id TEXT NOT NULL, target_key TEXT NOT NULL,
    kind TEXT, confirmed INTEGER, note TEXT
);
CREATE INDEX aliases_key ON aliases (alias_key);
CREATE TABLE excluded_items (
    business_id TEXT, line_id TEXT, sku TEXT, description TEXT, reason TEXT, source_ref TEXT
);
CREATE TABLE issues (business_id TEXT, severity TEXT, kind TEXT, message TEXT, ref TEXT);
CREATE TABLE checks (business_id TEXT, name TEXT, passed INTEGER, checked INTEGER, detail TEXT);
CREATE VIRTUAL TABLE items_fts USING fts5(
    code, description, line, business, category, section,
    tokenize = 'porter unicode61 remove_diacritics 2'
);
"""


def _j(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def write_catalog(path: Path, results: List[ImportResult], meta: Dict[str, str]) -> None:
    """Build the database in a temp file, then replace the old one in one step (never half-written)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    try:
        con.executescript(SCHEMA)
        con.executemany("INSERT INTO meta VALUES (?, ?)",
                        {**meta, "schema_version": SCHEMA_VERSION, "built_at": datetime.now().isoformat(timespec="seconds")}.items())
        for res in results:
            _write_business(con, res)
        con.execute("INSERT INTO items_fts(items_fts) VALUES ('optimize')")
        con.commit()
        con.execute("VACUUM")
    finally:
        con.close()
    tmp.replace(path)


def _write_business(con: sqlite3.Connection, res: ImportResult) -> None:
    b = res.business
    con.execute("INSERT INTO businesses VALUES (?, ?, ?, ?, ?, ?)",
                (b.id, b.name, b.kind, _j(b.pricing), _j(b.source_files), _j(b.documents)))
    con.executemany("INSERT INTO lines VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [(l.business, l.id, l.name, l.internal_name, None if l.multiplier is None else str(l.multiplier),
                      _j(l.aliases), l.sort) for l in res.lines])
    line_names = {l.id: " ".join([l.name, *l.aliases]) for l in res.lines}
    # The assistant only ever sees what can be quoted exactly. Everything else (no price, $0, old,
    # duplicates, marketing, lines not orderable) is kept aside for the report - never offered.
    con.executemany("INSERT INTO excluded_items VALUES (?, ?, ?, ?, ?, ?)",
                    [(it.business, it.line, it.sku, it.description, it.not_quotable_reason, it.source_ref)
                     for it in res.items if not it.quotable])
    for it in (i for i in res.items if i.quotable):
        extra = dict(it.extra)
        needs = extra.pop("needs_confirmation", None)
        cab = extra.pop("cabinet_type", None)
        cur = con.execute(
            "INSERT INTO items (business_id, line_id, sku, sku_key, code, code_key, description, category, section,"
            " cabinet_type, list_price_cents, unit, unit_note, assembly_price_cents, weight_lb, volume_m3, width_in,"
            " height_in, depth_in, quotable, not_quotable_reason, needs_confirmation, source_ref, extra)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (it.business, it.line, it.sku, match_key(it.sku), it.code, match_key(it.code), it.description, it.category,
             it.section, cab, cents(it.list_price), it.unit, it.unit_note, cents(it.assembly_price), it.weight_lb,
             it.volume_m3, it.width_in, it.height_in, it.depth_in, int(it.quotable), it.not_quotable_reason, needs,
             it.source_ref, _j(extra) if extra else None))
        item_id = cur.lastrowid
        con.executemany("INSERT INTO price_tiers VALUES (?, ?, ?)", [(item_id, q, cents(p)) for q, p in it.tiers])
        con.execute("INSERT INTO items_fts (rowid, code, description, line, business, category, section) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (item_id, f"{it.code} {it.sku}", it.description, line_names.get(it.line, ""), f"{b.id} {b.name}",
                     label(it.category), " ".join(x for x in (it.section, cab or "") if x)))
    con.executemany(
        "INSERT INTO charges (business_id, code, name, kind, amount_cents, unit, scope, needs_staff, note, raw, source_ref)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(c.business, c.code, c.name, c.kind, cents(c.amount), c.unit, c.scope, int(c.needs_staff), c.note, c.raw,
          c.source_ref) for c in res.charges])
    con.executemany("INSERT INTO material_groups VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [(g.business, g.id, g.material, g.name, cents(g.rate_per_sqft), g.note, g.source_ref) for g in res.groups])
    con.executemany("INSERT INTO colors (business_id, name, name_key, group_id, note, source_ref) VALUES (?, ?, ?, ?, ?, ?)",
                    [(c.business, c.name, match_key(c.name), c.group_id, c.note, c.source_ref) for c in res.colors])
    con.executemany("INSERT INTO aliases VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [(a.alias, match_key(a.alias), a.business, a.target_code, a.kind, int(a.confirmed), a.note)
                     for a in res.aliases])
    con.executemany("INSERT INTO issues VALUES (?, ?, ?, ?, ?)",
                    [(i.business, i.severity, i.kind, i.message, i.ref) for i in res.issues])
    con.executemany("INSERT INTO checks VALUES (?, ?, ?, ?, ?)",
                    [(c.business, c.name, int(c.passed), c.checked, c.detail) for c in res.checks])


def open_catalog(path: Path) -> sqlite3.Connection:
    """Read-only connection (safe to share across threads for reads)."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def rows(con: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> List[sqlite3.Row]:
    return con.execute(sql, tuple(params)).fetchall()
