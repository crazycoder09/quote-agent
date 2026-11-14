"""
Catalog tests.  Run:  python -m unittest discover -s tests -v

- Cleaning and pricing rules: always run (made-up numbers, no client data).
- Full build: runs only where the client's price files exist (cabinates/ is never committed).
  It must publish with 0 errors and every check passing - including reproducing the workbooks' own
  computed prices (HCC sample order, HCC 'Your Price' column, KCD order form).
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from catalog.normalize import clean_text, match_key, parse_dims, parse_rate, to_decimal  # noqa: E402
from catalog.pricing import sell, unit_cost  # noqa: E402

D = Decimal


class CleaningTest(unittest.TestCase):
    def test_mojibake_and_unicode(self):
        self.assertEqual(clean_text("Overlay Filler - 2.5â€\u009d W"), 'Overlay Filler - 2.5" W')
        self.assertEqual(clean_text("SC8（batten molding）"), "SC8(batten molding)")
        self.assertEqual(clean_text("Sink 33'' x 20 7/8''"), 'Sink 33" x 20 7/8"')
        self.assertEqual(clean_text("  a \n b  "), "a b")

    def test_match_key(self):
        self.assertEqual(match_key('TK8 1/4"'), "TK814")
        self.assertEqual(match_key("pl-100 alpina"), "PL100ALPINA")
        self.assertEqual(match_key("YN-Drawer Slide-12\""), match_key('YN-DRAWER SLIDE-12"'))

    def test_money(self):
        self.assertEqual(to_decimal("1,129.99"), D("1129.99"))
        self.assertEqual(to_decimal("$85.00"), D("85.00"))
        self.assertEqual(to_decimal(412), D("412.00"))
        self.assertIsNone(to_decimal("$5/LF"))
        self.assertIsNone(to_decimal(None))

    def test_text_prices(self):
        cases = {
            "$5/LF": (D("5.00"), "lf", False), "$4/SF": (D("4.00"), "sqft", False),
            "$40 Each": (D("40.00"), "each", False), "$350/each": (D("350.00"), "each", False),
            "$60 per door": (D("60.00"), "door", False), "NO CHARGE": (D("0.00"), "each", False),
            "Standard": (D("0.00"), "each", False), "$200 + Material + Mitered Edge": (D("200.00"), "each", True),
            "Custom Estimate Needed": (None, "each", True), 100.0: (D("100.00"), "each", False),
        }
        for raw, (amount, unit, staff) in cases.items():
            r = parse_rate(raw)
            self.assertEqual((r["amount"], r["unit"], r["needs_staff"]), (amount, unit, staff), raw)

    def test_dimensions(self):
        self.assertEqual(parse_dims('12"  Wall Cab- 1 dr_21"W 36"T 12"D'), (21, 36, 12))
        self.assertEqual(parse_dims('Vanity Combination Cabs_60"W 34 1/2"T 21"D'), (60, 34.5, 21))
        self.assertEqual(parse_dims('Angled Hood Classic Trim - 30" W x 36" H'), (30, 36, None))
        self.assertEqual(parse_dims("Kitchen Sink 33'' x 20 7/8''"), (None, None, None))  # no letters: unknown


class PricingTest(unittest.TestCase):
    def test_multiplier_rounds_like_excel(self):
        p = {"basis": "list_price_x_multiplier", "multiplier": "0.5"}
        self.assertEqual(unit_cost(D("100"), p), D("50.00"))
        self.assertEqual(unit_cost(D("0.05"), {"basis": "list_price_x_multiplier", "multiplier": "0.5"}), D("0.03"))

    def test_line_multiplier_and_list_items(self):
        p = {"basis": "list_price_x_line_multiplier"}
        self.assertEqual(unit_cost(D("200"), p, D("0.45")), D("90.00"))
        self.assertEqual(unit_cost(D("200"), p, D("0.45"), price_type="list"), D("200.00"))

    def test_quantity_breaks_and_markup(self):
        p = {"basis": "net_price_x_markup"}
        tiers = [(1, D("100")), (4, D("90")), (10, D("80"))]
        self.assertEqual(unit_cost(D("100"), p, quantity=3, tiers=tiers), D("100.00"))
        self.assertEqual(unit_cost(D("100"), p, quantity=4, tiers=tiers), D("90.00"))
        self.assertEqual(unit_cost(D("100"), p, quantity=25, tiers=tiers), D("80.00"))

    def test_customer_price_is_cost_plus_markup(self):
        from catalog.pricing import markup_percent

        m = markup_percent("HCC")
        self.assertEqual(sell(D("100.00"), "HCC"), (D("100") * (1 + m / 100)).quantize(D("0.01")))
        self.assertIsNone(sell(None, "HCC"))

    def test_missing_price(self):
        self.assertIsNone(unit_cost(None, {"basis": "list_price_x_multiplier", "multiplier": "1"}))


SOURCES = ROOT / "cabinates"


@unittest.skipUnless(SOURCES.exists(), "client price files not present (cabinates/ is not in git)")
class BuildTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from catalog.build import build

        cls.tmp = Path(tempfile.mkdtemp(prefix="catalog-test-"))
        cls.outcome = build(root=ROOT, out_dir=cls.tmp, export=False)
        cls.con = sqlite3.connect(cls.outcome.db_path)
        cls.con.row_factory = sqlite3.Row

    def q(self, sql, *params):
        return self.con.execute(sql, params).fetchall()

    def test_published_clean(self):
        self.assertTrue(self.outcome.published)
        self.assertEqual(self.outcome.errors, 0)
        failed = [f"{c.business}: {c.name} - {c.detail}" for r in self.outcome.results for c in r.checks if not c.passed]
        self.assertEqual(failed, [])

    def test_every_business_loaded(self):
        ids = {r["id"] for r in self.q("select id from businesses")}
        self.assertEqual(ids, {"HCC", "KCD", "PELICAN", "ZSOLT"})
        for bid in ("HCC", "KCD", "PELICAN"):
            self.assertGreater(self.q("select count(*) n from items where business_id=? and quotable=1", bid)[0]["n"], 0)
        self.assertGreater(self.q("select count(*) n from material_groups")[0]["n"], 0)
        self.assertGreater(self.q("select count(*) n from colors")[0]["n"], 0)

    def test_workbook_reproduction_checks_ran(self):
        names = [r["name"] for r in self.q("select name from checks where passed=1")]
        self.assertTrue(any("Order tab sample order" in n for n in names))
        self.assertTrue(any("column I (your price)" in n for n in names))
        self.assertTrue(any("Order form rows" in n for n in names))

    def test_hcc_every_product_in_every_line(self):
        rows = self.q("select count(distinct sku) skus, count(distinct line_id) lines, count(*) n from items where business_id='HCC'")[0]
        self.assertEqual(rows["n"], rows["skus"] * rows["lines"])
        self.assertEqual(rows["lines"], 13)

    def test_quotable_items_are_priced_and_unambiguous(self):
        bad = self.q("select sku from items where quotable=1 and (list_price_cents is null or list_price_cents < 0)")
        self.assertEqual(bad, [])
        dupes = self.q("select business_id, line_id, code_key, count(distinct list_price_cents) p from items "
                       "where quotable=1 group by business_id, line_id, code_key having p > 1")
        self.assertEqual([tuple(r) for r in dupes], [])

    def test_kcd_line_prefix_and_shared_lines(self):
        r = self.q("select line_id, code from items where business_id='KCD' and sku='BF-B15'")
        self.assertEqual([tuple(x) for x in r], [("BF", "B15")])
        five = self.q("select code from items where business_id='KCD' and line_id='SW5' and sku='SW-B15-5PC'")
        self.assertEqual([x["code"] for x in five], ["B15"])

    def test_pelican_rules(self):
        self.assertEqual(self.q("select count(*) n from items where business_id='PELICAN' "
                                "and lower(sku) like '%(old)%'")[0]["n"], 0)
        tiers = self.q("select t.min_qty, t.price_cents from price_tiers t join items i on i.id=t.item_id "
                       "where i.business_id='PELICAN' order by i.id, t.min_qty limit 4")
        self.assertTrue(tiers and tiers[0]["min_qty"] == 1)
        dupes = self.q("select count(*) n from excluded_items where reason like 'duplicate of%'")
        self.assertGreater(dupes[0]["n"], 0)  # duplicates removed (one product = one entry), listed for review

    def test_catalog_has_nothing_ambiguous(self):
        self.assertEqual(self.q("select count(*) n from items where quotable=0 or needs_confirmation is not null")[0]["n"], 0)
        self.assertEqual(self.q("select count(*) n from aliases")[0]["n"], 0)
        self.assertEqual(self.q("select count(*) n from (select name_key from colors group by 1 having count(*) > 1)")[0]["n"], 0)
        self.assertEqual(self.q("select count(*) n from charges where amount_cents is null or needs_staff=1")[0]["n"], 0)
        same = self.q("select count(*) n from (select business_id, line_id, code_key from items group by 1, 2, 3 "
                      "having count(*) > 1)")
        self.assertEqual(same[0]["n"], 0)

    def test_countertop_colors_map_to_groups(self):
        unmapped = self.q("select c.name from colors c left join material_groups g on g.id=c.group_id "
                          "where g.id is null or g.rate_per_sqft_cents is null")
        self.assertEqual(unmapped, [])

    def test_kcd_modifications_quotable(self):
        mods = self.q("select sku, unit from items where business_id='KCD' and section='Modifications'")
        self.assertTrue(mods)

    def test_search_index(self):
        hits = self.q("select count(*) n from items_fts where items_fts match 'range hood'")
        self.assertGreater(hits[0]["n"], 0)
        hits = self.q("select count(*) n from items_fts where items_fts match 'farmhouse sink'")
        self.assertGreater(hits[0]["n"], 0)

    def test_pricing_rules_stored(self):
        for row in self.q("select id, pricing from businesses"):
            self.assertIn("basis", json.loads(row["pricing"]), row["id"])


if __name__ == "__main__":
    unittest.main()
