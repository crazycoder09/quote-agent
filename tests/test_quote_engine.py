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

    def price(self, items, countertops=(), assembled=False, shipping=False, extras=None, installation=False):
        from catalog.quote_engine import price_quote

        return price_quote(self.cat, list(items), list(countertops), assembled, shipping, extras, installation)

    @staticmethod
    def sell(cost, business):
        from catalog.pricing import sell

        return float(sell(Decimal(str(cost)), business))

    def test_hcc_sample_order_matches_workbook(self):
        import openpyxl

        wb = openpyxl.load_workbook(SOURCES / "HCC" / "HCC Signature Series Price List,, (1).xlsm", data_only=True)
        order = wb["Order"]
        line = order["C9"].value
        items = [self.item(order[f"B{r}"].value, "HCC", line, int(order[f"A{r}"].value or 1))
                 for r in range(13, 63) if order[f"B{r}"].value]
        q, problems, _ = self.price(items)
        self.assertEqual(problems, [])
        # wholesale cost = the workbook's own "Your Price" total; customer price = each line's cost + markup
        self.assertAlmostEqual(q["sections"][0]["cost"], float(order["J64"].value), places=2)
        expected_sell = sum(self.sell(i["unit_cost"], "HCC") * i["quantity"] for i in q["sections"][0]["items"])
        self.assertAlmostEqual(q["sections"][0]["subtotal"], round(expected_sell, 2), places=2)
        q2, _, _ = self.price(items, assembled=True)
        fee_cost = (Decimal(str(order["J64"].value)) * Decimal("0.10")).quantize(Decimal("0.01"))
        self.assertAlmostEqual(q2["sections"][0]["cost"], float(Decimal(str(order["J64"].value)) + fee_cost), places=2)
        self.assertAlmostEqual(q2["sections"][0]["assembly_fee"], self.sell(fee_cost, "HCC"), places=2)

    def test_kcd_assembly_per_cabinet_with_pickup_extra(self):
        it = self.item("B15", "KCD", "Brooklyn Fawn", 2)
        row = self.cat.get_item(it["item_id"])
        extra = Decimal(self.cat.businesses["KCD"]["pricing"]["assembly"]["pickup_extra_per_cabinet"])
        q, problems, _ = self.price([it], assembled=True, shipping=False)
        self.assertEqual(problems, [])
        self.assertAlmostEqual(q["sections"][0]["assembly_fee"], self.sell((row["assembly_price"] + extra) * 2, "KCD"), places=2)
        q, _, _ = self.price([it], assembled=True, shipping=True)
        self.assertAlmostEqual(q["sections"][0]["assembly_fee"], self.sell(row["assembly_price"] * 2, "KCD"), places=2)
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
            self.assertAlmostEqual(q["sections"][0]["items"][0]["unit_cost"], float(price), places=2)
            self.assertAlmostEqual(q["sections"][0]["items"][0]["unit_price"], self.sell(price, "PELICAN"), places=2)

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
        self.assertAlmostEqual(piece["material_amount"], self.sell(rate * 40, "ZSOLT"), places=2)
        self.assertAlmostEqual(piece["services"][0]["amount"], round(self.sell(lf["amount"], "ZSOLT") * 20, 2), places=2)
        parts = sum(s["total"] for s in q["sections"]) + q["countertops"]["total"] + q["totals"]["tax"]
        self.assertAlmostEqual(q["totals"]["grand_total"], round(parts, 2), places=2)
        # margin = customer price - wholesale cost (products, assembly, countertops)
        products = sum(s["total"] for s in q["sections"]) + q["countertops"]["total"]
        cost = sum(s["cost"] for s in q["sections"]) + q["countertops"]["cost"]
        self.assertAlmostEqual(q["totals"]["margin"], round(products - cost, 2), places=2)

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


    def test_sales_tax_rules(self):
        from catalog.pricing import tax_rate

        cab = self.item("W2136", "HCC", "Colonial White", 2)
        color = self.cat._q("select c.name from colors c join material_groups g on g.id=c.group_id limit 1")[0]["name"]
        q, p, _ = self.price([cab], [{"color": color, "sqft": 30}])
        self.assertEqual(p, [])
        s = q["sections"][0]
        self.assertAlmostEqual(s["tax"], float((Decimal(str(s["subtotal"])) * tax_rate()).quantize(Decimal("0.01"))), places=2)
        self.assertAlmostEqual(q["totals"]["tax"], s["tax"], places=2)  # countertops add no tax
        q, _, _ = self.price([cab], [{"color": color, "sqft": 30}], shipping=True, installation=True)
        self.assertEqual(q["totals"]["tax"], 0.0)  # installed items are not taxed

    def test_installation_price_and_time(self):
        from catalog.pricing import rules

        floor = self.cat._q("select sku from items where category='flooring' and json_extract(extra,'$.sqft_per_unit') "
                            "is not null limit 1")[0]["sku"]
        items = [self.item("W2136", "HCC", "Colonial White", 3), self.item(floor, "PELICAN", qty=2)]
        q, p, _ = self.price(items, installation=True)
        self.assertEqual(p, [])
        r = rules()["installation"]["rates"]
        inst = {l["category"]: l for l in q["installation"]["lines"]}
        self.assertAlmostEqual(inst["cabinet"]["amount"], 3 * r["cabinet"]["price"], places=2)
        per = self.cat.lookup(floor, "PELICAN").items[0]["extra"]["sqft_per_unit"]
        self.assertAlmostEqual(inst["flooring"]["quantity"], round(2 * per, 2), places=2)
        hours = (3 * r["cabinet"]["minutes"] + 2 * per * r["flooring"]["minutes"]) / 60
        self.assertAlmostEqual(q["installation"]["hours"], round(hours, 1), places=1)
        self.assertGreaterEqual(q["installation"]["days"], 1)
        q2, _, _ = self.price(items, installation=False)
        self.assertIsNone(q2["installation"])

    def test_find_by_size(self):
        exact, near = self.cat.find_by_size("wall", 30, 36, None, "HCC", ["colonial-white"])
        self.assertIn("W3036", [i["code"] for i in exact])
        exact, _ = self.cat.find_by_size("bridge", 30, 12, 24, "HCC", ["colonial-white"])
        self.assertIn("W301224", [i["code"] for i in exact])
        exact, near = self.cat.find_by_size("base", 16, None, None, "HCC", ["colonial-white"])
        self.assertEqual(exact, [])
        self.assertTrue(near)  # nearest real widths, never a made-up size

    def test_flooring_quantity_waste(self):
        import json
        import math

        from agent.quote_tools import flooring_quantity

        row = self.cat._q("select id, extra from items where category='flooring' and json_extract(extra,'$.sqft_per_unit') "
                          "is not null limit 1")[0]
        per = json.loads(row["extra"])["sqft_per_unit"]
        with_waste = json.loads(flooring_quantity.invoke({"item_id": row["id"], "area_sqft": 400, "area_includes_waste": False}))
        self.assertEqual(with_waste["quantity"], math.ceil(round(440 / per, 6)))
        included = json.loads(flooring_quantity.invoke({"item_id": row["id"], "area_sqft": 400, "area_includes_waste": True}))
        self.assertEqual(included["quantity"], math.ceil(round(400 / per, 6)))


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


@unittest.skipUnless(SOURCES.exists(), "client price files not present (cabinates/ is not in git)")
class WorkflowTest(unittest.TestCase):
    """create_quote rules, privacy of cost, photos, and staff sales / margin reports (temporary storage)."""

    @classmethod
    def setUpClass(cls):
        import agent.memory as mem
        from catalog.store import get_catalog

        cls.mem, cls.cat = mem, get_catalog()
        cls.saved = dict(mem._singletons)
        cls.old = (mem.STORAGE_DIR, mem.CHECKPOINT_DB_PATH, mem.STORE_DB_PATH)
        tmp = Path(tempfile.mkdtemp(prefix="workflow-test-"))
        mem._singletons.clear()
        mem.STORAGE_DIR, mem.CHECKPOINT_DB_PATH, mem.STORE_DB_PATH = tmp, tmp / "c.db", tmp / "s.db"

    @classmethod
    def tearDownClass(cls):
        cls.mem._singletons.clear()
        cls.mem._singletons.update(cls.saved)
        cls.mem.STORAGE_DIR, cls.mem.CHECKPOINT_DB_PATH, cls.mem.STORE_DB_PATH = cls.old

    def quote(self, role="customer", **kw):
        import json

        from agent.quote_tools import create_quote

        m = self.cat.lookup("W2136", "HCC", ["colonial-white"])
        args = {"customer_name": "Flow Test", "customer_phone": "4075550190", "assembled": False, "shipping": False,
                "items": [{"item_id": m.items[0]["id"], "code": "W2136", "quantity": 2}], **kw}
        cfg = {"configurable": {"thread_id": "flow-" + role, "role": role}}
        return json.loads(create_quote.invoke(args, config=cfg))

    def test_install_or_delivery_needs_address(self):
        out = self.quote(installation=True)
        self.assertEqual(out["status"], "not_created")
        self.assertTrue(any("address" in p for p in out["problems"]))
        out = self.quote(shipping=True)
        self.assertEqual(out["status"], "not_created")
        out = self.quote(installation=True, customer_address="12 Main St, Orlando, FL 32801")
        self.assertEqual(out["status"], "created")
        self.assertIn("installation", out)
        self.assertEqual(out["sales_tax"], 0.0)

    def test_cost_only_for_staff(self):
        from agent.quote_tools import find_products

        customer = self.quote()
        self.assertNotIn("staff_only", customer)
        staff = self.quote(role="staff")
        self.assertIn("margin_percent", staff["staff_only"])
        args = {"codes": ["W2136"], "business": "HCC", "line": "Colonial White"}
        self.assertNotIn("cost", find_products.invoke(args, config={"configurable": {"role": "customer"}}))
        self.assertIn("staff: cost", find_products.invoke(args, config={"configurable": {"role": "staff"}}))

    def test_line_images(self):
        import json

        from agent.quote_tools import show_line_images

        out = json.loads(show_line_images.invoke({"lines": ["Colonial White", "Brooklyn Fawn"]}))
        self.assertEqual([i["line"] for i in out["images"]], ["Colonial White"])
        self.assertTrue((ROOT / out["images"][0]["door"]).exists() and (ROOT / out["images"][0]["kitchen"]).exists())
        self.assertTrue(any("Brooklyn Fawn" in m for m in out["missing"]))

    def test_sales_report_and_status(self):
        import json

        from agent.quote_tools import sales_report, set_quote_status

        a, b = self.quote(), self.quote()
        staff = {"configurable": {"role": "staff"}}
        self.assertIn("error", json.loads(sales_report.invoke({}, config={"configurable": {"role": "customer"}})))
        self.assertIn("error", json.loads(set_quote_status.invoke({"quote_number": a["quote_number"], "status": "won"},
                                                                  config={"configurable": {"role": "customer"}})))
        self.assertTrue(json.loads(set_quote_status.invoke({"quote_number": a["quote_number"], "status": "sold"},
                                                           config=staff))["saved"])
        set_quote_status.invoke({"quote_number": b["quote_number"], "status": "lost"}, config=staff)
        report = json.loads(sales_report.invoke({}, config=staff))
        quotes = [self.mem.get_quote(n) for n in (a["quote_number"], b["quote_number"])]
        self.assertEqual(report["won_sales"]["quotes"], 1)
        self.assertAlmostEqual(report["won_sales"]["margin"], quotes[0]["totals"]["margin"], places=2)
        self.assertGreaterEqual(report["all_quotes"]["quotes"], 2)
        self.assertIn("HCC", report["by_business"])


class RevisionChainTest(unittest.TestCase):
    def test_sold_older_version_counts_once(self):
        from agent.quote_tools import _latest_quotes

        qs = [{"quote_number": "HQ-1", "created_at": "1", "status": "won"},
              {"quote_number": "HQ-2", "created_at": "2", "revises": "HQ-1"},
              {"quote_number": "HQ-3", "created_at": "3"},
              {"quote_number": "HQ-4", "created_at": "4", "revises": "HQ-3"}]
        self.assertEqual(sorted(q["quote_number"] for q in _latest_quotes(qs)), ["HQ-1", "HQ-4"])
