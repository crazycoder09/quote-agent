"""
Build the product catalog from every business's price files.

    python scripts/build_catalog.py            import + validate + publish data/catalog.db (only if clean)
    python scripts/build_catalog.py --check    import + validate + report only (nothing published)
    python scripts/build_catalog.py --force    publish even with errors (not recommended)
    python scripts/build_catalog.py --encrypt  also write data/catalog.db.enc with PRICE_SHEET_KEY (for deploys)

Exit code 0 = published and every check passed; 1 = errors or failed checks.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from catalog.build import build, encrypt_catalog, summary_lines  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="validate only, publish nothing")
    parser.add_argument("--force", action="store_true", help="publish even if there are errors")
    parser.add_argument("--encrypt", action="store_true", help="also write an encrypted copy (PRICE_SHEET_KEY)")
    parser.add_argument("--no-export", action="store_true", help="skip the Excel review export")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")

    out_dir = Path(tempfile.mkdtemp(prefix="catalog-check-")) if args.check else None
    outcome = build(root=ROOT, out_dir=out_dir, force=args.force, export=not args.no_export)
    print("Catalog build")
    print("\n".join(summary_lines(outcome)))
    print(f"errors {outcome.errors} | warnings {outcome.warnings} | failed checks {outcome.failed_checks}")
    print(f"report: {outcome.report_path}")
    if outcome.published and not args.check:
        print(f"published: {outcome.db_path}")
        if outcome.export_path:
            print(f"review export: {outcome.export_path}")
        if args.encrypt:
            key = os.getenv("PRICE_SHEET_KEY")
            if not key:
                print("PRICE_SHEET_KEY is not set - encrypted copy not written")
                return 1
            print(f"encrypted: {encrypt_catalog(outcome.db_path, key)}")
    elif not outcome.published:
        print("NOT published - see the report")
    return 0 if outcome.published and outcome.errors == 0 and outcome.failed_checks == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
