"""
catalog
-------
Turns every business's price files (xlsm / xls / xlsx / PDF) into ONE clean, validated product
catalog (data/catalog.db). The agent searches and prices from the catalog - it never reads raw sheets.

    normalize.py   cleaning helpers (text, codes, money, text prices, dimensions)
    model.py       the common data shape every importer produces
    categories.py  one product taxonomy shared by all businesses
    importers/     one importer per supplier layout (hcc, kcd, pelican, zsolt)
    validate.py    cross-business checks (errors block publishing)
    pricing.py     the single place unit prices are calculated
    db.py          SQLite writer/reader (+ full-text search index)
    build.py       run everything -> catalog.db + report + review export

Build:  python scripts/build_catalog.py
"""
