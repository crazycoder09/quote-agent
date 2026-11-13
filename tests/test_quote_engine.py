"""
Quote engine + catalog tools + quote document.  Run:  python -m unittest discover -s tests -v

Needs the client's price files (cabinates/, not in git) - skipped otherwise. Expected values are read
from the workbooks themselves (e.g. the HCC sample order's own total), never typed into this file.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SOURCES = ROOT / "cabinates"


def setUpModule():
    if not SOURCES.exists():
        return
    from catalog.build import build

    tmp = Path(tempfile.mkdtemp(prefix="engine-test-"))
    outcome = build(root=ROOT, out_dir=tmp, export=False)
    assert outcome.published, "catalog build failed"
    os.environ["CATALOG_DB"] = str(outcome.db_path)


@unittest.skipUnless(SOURCES.exists(), "client price files not present (cabinates/ is not in git)")
class EngineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from catalog.store import get_catalog

        cls.cat = get_catalog()

    def item(self, code, business=None, line=None, qty=1, note=None):
        lids = [l["id"] for l in self.cat.resolve_line(line, business)] if line else None
        m = self.cat.lookup(code, business, lids)
        self.assertEqual(len(m.items), 1, f"{code}: {[(i['sku'], i['line_id']) for i in m.items]}")
        return {"item_id": m.items[0]["id"], "code": code, "quantity": qty, "note": note}

    def price(self, items, countertops=(), assembled=False, shipping=False, extras=None):
        from catalog.quote_engine import price_quote

        return price_quote(self.cat, list(items), list(countertops), assembled, shipping, extras)

    def test_hcc_sample_order_matches_workbook(self):
        import openpyxl

        wb = openpyxl.load_workbook(SOURCES / "HCC" / "HCC Signature Series Price List,, (1).xlsm", data_only=True)
        order = wb["Order"]
        line = order["C9"].value
        items = [self.item(order[f"B{r}"].value, "HCC", line, int(order[f"A{r}"].value or 1))
                 for r in range(13, 63) if order[f"B{r}"].value]
        q, problems, _ = self.price(items)
        self.assertEqual(problems, [])
        self.assertAlmostEqual(q["totals"]["grand_total"], float(order["J64"].value), places=2)
        q2, _, _ = self.price(items, assembled=True)
        expected_fee = float((Decimal(str(order["J64"].value)) * Decimal("0.10")).quantize(Decimal("0.01")))
        self.assertAlmostEqual(q2["sections"][0]["assembly_fee"], expected_fee, places=2)

    def test_kcd_assembly_per_cabinet_with_pickup_extra(self):
        it = self.item("B15", "KCD", "Brooklyn Fawn", 2)
        row = self.cat.get_item(it["item_id"])
        extra = Decimal(self.cat.businesses["KCD"]["pricing"]["assembly"]["pickup_extra_per_cabinet"])
        q, problems, _ = self.price([it], assembled=True, shipping=False)
        self.assertEqual(problems, [])
        self.assertAlmostEqual(q["sections"][0]["assembly_fee"], float((row["assembly_price"] + extra) * 2), places=2)
        q, _, _ = self.price([it], assembled=True, shipping=True)
        self.assertAlmostEqual(q["sections"][0]["assembly_fee"], float(row["assembly_price"] * 2), places=2)
        q, _, _ = self.price([it], assembled=False)
        self.assertEqual(q["sections"][0]["assembly_fee"], 0.0)

    def test_pelican_quantity_breaks(self):
        r = self.cat._q("select i.sku from items i join price_tiers t on t.item_id=i.id where i.business_id='PELICAN' "
                        "and i.quotable=1 group by i.id having count(*) >= 3 limit 1")[0]
        it = self.item(r["sku"], "PELICAN")
        tiers = self.cat.get_item(it["item_id"])["tiers"]
        for min_qty, price in tiers:
            q, problems, _ = self.price([{**it, "quantity": min_qty}])
            self.assertEqual(problems, [])
            self.assertAlmostEqual(q["sections"][0]["items"][0]["unit_price"], float(price), places=2)

    def test_mixed_quote_sections_and_totals(self):
        items = [self.item("W2136", "HCC", "Colonial White", 2), self.item("B15", "KCD", "Brooklyn Fawn"),
                 self.item("PL-150 Alpina", "PELICAN")]
        color = self.cat._q("select c.name from colors c join material_groups g on g.id=c.group_id "
                            "where g.material='quartz' group by c.name_key having count(*)=1 limit 1")[0]["name"]
        lf = next(c for c in self.cat.charges("ZSOLT") if c["unit"] == "lf" and c["amount"] and not c["needs_staff"])
        ct = [{"label": "Kitchen", "color": color, "sqft": 40, "services": [{"code": lf["code"], "quantity": 20}]}]
        q, problems, _ = self.price(items, ct, assembled=True)
        self.assertEqual(problems, [])
        self.assertEqual([s["business"] for s in q["sections"]], ["HCC", "KCD", "PELICAN"])
        piece = q["countertops"]["pieces"][0]
        rate = self.cat.countertop_color(color)[0]["rate"]
        self.assertAlmostEqual(piece["material_amount"], float(rate * 40), places=2)
        self.assertAlmostEqual(piece["services"][0]["amount"], float(lf["amount"] * 20), places=2)
        parts = sum(s["total"] for s in q["sections"]) + q["countertops"]["total"]
        self.assertAlmostEqual(q["totals"]["grand_total"], round(parts, 2), places=2)

    def test_guards(self):
        good = self.item("W2136", "HCC", "Colonial White")
        _, p, _ = self.price([{**good, "code": "B15"}])
        self.assertTrue(p and "not 'B15'" in p[0])
        _, p, _ = self.price([{"item_id": 99999999, "code": "X", "quantity": 1}])
        self.assertTrue(p and "does not exist" in p[0])
        conf = self.cat._q("select id, sku from items where needs_confirmation is not null and quotable=1 limit 1")
        if conf:
            _, p, _ = self.price([{"item_id": conf[0]["id"], "code": conf[0]["sku"], "quantity": 1}])
            self.assertTrue(p and "needs staff confirmation" in p[0])
        dup = self.cat._q("select name from colors group by name_key having count(distinct group_id) > 1 limit 1")
        if dup:
            _, p, _ = self.price([], [{"color": dup[0]["name"], "sqft": 10}])
            self.assertTrue(p and "data conflict" in p[0] and "don't ask the customer to pick a price" in p[0])
        _, p, _ = self.price([], [{"color": "Not A Real Color", "sqft": 10}])
        self.assertTrue(p and "not in the countertop guide" in p[0])
        staff = next((c for c in self.cat.charges("ZSOLT") if c["needs_staff"]), None)
        color = self.cat._q("select c.name from colors c join material_groups g on g.id=c.group_id limit 1")[0]["name"]
        if staff:
            _, p, _ = self.price([], [{"color": color, "sqft": 10, "services": [{"code": staff["code"]}]}])
            self.assertTrue(p and "no fixed price" in p[0])
        _, p, _ = self.price([], [])
        self.assertEqual(p, ["The quote has no items"])

    def test_hinge_suffix_accepted(self):
        hinge = self.item("B15", "HCC", "Colonial White")
        _, p, _ = self.price([{**hinge, "code": "B15L"}])
        self.assertEqual(p, [])

    def test_model_without_color_asks(self):
        m = self.cat.lookup("PL-150", "PELICAN")
        self.assertEqual(m.how, "prefix")  # bare model -> its color variants (ask which), never a guess
        self.assertGreater(len(m.items), 1)

    def test_parallel_quotes_get_unique_numbers(self):
        import json
        import threading

        from agent.quote_tools import create_quote

        tmp = Path(tempfile.mkdtemp(prefix="mem-test-"))
        import agent.memory as mem
        saved = dict(mem._singletons)
        mem._singletons.clear()
        old = (mem.STORAGE_DIR, mem.CHECKPOINT_DB_PATH, mem.STORE_DB_PATH)
        mem.STORAGE_DIR, mem.CHECKPOINT_DB_PATH, mem.STORE_DB_PATH = tmp, tmp / "c.db", tmp / "s.db"
        try:
            item = self.item("W2136", "HCC", "Colonial White")
            numbers, errors = [], []

            def make(n):
                try:
                    out = json.loads(create_quote.invoke(
                        {"customer_name": f"P{n}", "customer_phone": f"40755501{n:02d}", "assembled": False,
                         "shipping": False, "items": [item]}, config={"configurable": {"thread_id": f"par{n}"}}))
                    numbers.append(out["quote_number"])
                except Exception as exc:  # noqa: BLE001
                    errors.append(repr(exc))

            threads = [threading.Thread(target=make, args=(n,)) for n in range(20)]
            [t.start() for t in threads]
            [t.join() for t in threads]
            self.assertEqual(errors, [])
            self.assertEqual(len(numbers), 20)
            self.assertEqual(len(set(numbers)), 20)
        finally:
            mem._singletons.clear()
            mem._singletons.update(saved)
            mem.STORAGE_DIR, mem.CHECKPOINT_DB_PATH, mem.STORE_DB_PATH = old


@unittest.skipUnless(SOURCES.exists(), "client price files not present (cabinates/ is not in git)")
class DocumentTest(unittest.TestCase):
    def test_pdf_and_brief_for_new_and_old_quotes(self):
        from agent.quote_document import normalize_quote, quote_brief, render_quote_pdf
        from catalog.quote_engine import price_quote
        from catalog.store import get_catalog

        cat = get_catalog()
        m = cat.lookup("W2136", "HCC", ["colonial-white"])
        priced, problems, _ = price_quote(cat, [{"item_id": m.items[0]["id"], "code": "W2136", "quantity": 2}],
                                          [], True, False)
        self.assertEqual(problems, [])
        base = {"quote_number": "HQ-9999", "date": "2026-11-13", "valid_until": "2026-12-13", "company": {"name": "Test Co"},
                "customer": {"name": "Test", "phone": "4075550100"}, "assembled": True, "shipping": False,
                "unpriced_items": ["X1 - designer"], "notes": "", "footer": "footer"}
        new = {**base, **priced}
        self.assertTrue(render_quote_pdf(new).startswith(b"%PDF"))
        self.assertIn("item_id", quote_brief(new, full=True))
        legacy = {**base, "brand": "HCC", "brand_display": "HCC", "line": "Colonial White", "multiplier": 0.32,
                  "assembly_fee_percent": 10.0, "extras": [],
                  "items": [{"sku": "W2136", "description": "Wall", "quantity": 2, "unit_price": 1.0, "total": 2.0}],
                  "totals": {"subtotal": 2.0, "assembly_fee": 0.2, "extras_total": 0.0, "grand_total": 2.2}}
        self.assertEqual(normalize_quote(legacy)["sections"][0]["total"], 2.2)
        self.assertTrue(render_quote_pdf(legacy).startswith(b"%PDF"))


@unittest.skipUnless(SOURCES.exists(), "client price files not present (cabinates/ is not in git)")
class ToolsTest(unittest.TestCase):
    def test_find_products_needs_line_for_item_id(self):
        from agent.quote_tools import find_products

        out = find_products.invoke({"codes": ["W2136"]})
        self.assertIn("call again", out)
        self.assertNotIn("item_id=", out)
        out = find_products.invoke({"codes": ["W2136"], "business": "HCC", "line": "Colonial White"})
        self.assertIn("item_id=", out)
        self.assertIn("Unknown business", find_products.invoke({"codes": ["W2136"], "business": "Nope"}))

    def test_search_and_countertops(self):
        from agent.quote_tools import countertop_options, search_products

        self.assertIn("item_id=", search_products.invoke({"query": "farmhouse sink", "category": "kitchen_sink"}))
        self.assertIn("in ", search_products.invoke({"query": "range hood 36"}))
        self.assertIn("Unknown category", search_products.invoke({"query": "x", "category": "spaceships"}))
        self.assertIn("/sq ft", countertop_options.invoke({"material": "quartz"}))
        self.assertIn("Services", countertop_options.invoke({"services": True}))


if __name__ == "__main__":
    unittest.main()
