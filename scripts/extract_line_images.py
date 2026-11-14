"""
Extract each door style's SAMPLE DOOR image and FINISHED KITCHEN photo from a supplier spec book.

    python scripts/extract_line_images.py

Writes assets/catalog_images/<BUSINESS>/<line-id>/door.jpg and kitchen.jpg (resized, web quality).
The assistant shows them in the chat (tool show_line_images). Add photos for other brands by dropping
door.jpg / kitchen.jpg into the same folder layout - no code change needed.

HCC Signature Series spec book: one page per door style (pages 12-22) - the door sample is the tall
portrait image, the kitchen photo is the largest landscape image on the page.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from catalog.normalize import match_key  # noqa: E402

SPEC_BOOKS = {
    "HCC": {
        "pdf": "cabinates/HCC/HCC Signature Series Spec Book 2026 (2).pdf",
        "pages": range(12, 23),
        "lines": {  # name printed on the page -> line id in config/catalog.json
            "Colonial White": "colonial-white", "Colonial Grey": "colonial-grey", "Colonial Espresso": "colonial-espresso",
            "Colonial Navy": "colonial-navy", "Colonial Timber": "colonial-timber", "Sierra Desert": "sierra-desert",
            "Summit White": "summit-white", "Oxford White": "oxford-white", "Oxford Oak": "oxford-oak",
            "Regal Ivory": "regal-ivory", "Regal lvory": "regal-ivory", "Regal Truffle": "regal-truffle",
        },
        # verified page -> door style (used when a page's text can't be read, e.g. page 13's font encoding)
        "page_lines": {12: "colonial-white", 13: "colonial-grey", 14: "colonial-espresso", 15: "colonial-navy",
                       16: "colonial-timber", 17: "sierra-desert", 18: "summit-white", 19: "oxford-white",
                       20: "oxford-oak", 21: "regal-ivory", 22: "regal-truffle"},
    },
}
OUT = ROOT / "assets" / "catalog_images"


def _save(img: Image.Image, path: Path, max_side: int) -> None:
    img = img.convert("RGB")
    img.thumbnail((max_side, max_side), Image.LANCZOS)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "JPEG", quality=84, optimize=True)


def extract(business: str, spec: dict) -> list:
    """Crop the door and the kitchen photo from the RENDERED page (what a reader sees). The raw embedded
    images can't be used: the spec book reuses one grey door image and tints it on the page."""
    import pypdfium2 as pdfium
    import pypdfium2.raw as pdfium_c
    from pypdf import PdfReader

    logging.disable(logging.WARNING)  # the spec book has harmless broken cross-references
    path = str(ROOT / spec["pdf"])
    reader, pdf = PdfReader(path), pdfium.PdfDocument(path)
    scale = 200 / 72  # 200 dpi
    done = []
    for page_no in spec["pages"]:
        key = match_key((reader.pages[page_no - 1].extract_text() or "").replace("\ufb00", "ff"))
        line_id = next((lid for name, lid in spec["lines"].items() if match_key(name) in key), None)
        if line_id and spec.get("page_lines", {}).get(page_no, line_id) != line_id:
            print(f"  page {page_no}: text says {line_id}, page list says {spec['page_lines'][page_no]} - check the spec book")
            continue
        line_id = line_id or spec.get("page_lines", {}).get(page_no)
        if not line_id:
            print(f"  page {page_no}: no known door style on the page - skipped")
            continue
        page = pdf[page_no - 1]
        width, height = page.get_size()
        boxes = []  # placed images, in PDF points (origin bottom-left)
        for obj in page.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_IMAGE], max_depth=4):
            left, bottom, right, top = obj.get_bounds()
            # images may run past the page edge (bleed): only the visible part counts
            left, bottom, right, top = max(0, left), max(0, bottom), min(width, right), min(height, top)
            w, h = right - left, top - bottom
            if w > 20 and h > 20 and w < width * 0.9:
                boxes.append((left, bottom, right, top, w, h))
        portraits = [b for b in boxes if b[5] > 1.5 * b[4]]
        landscapes = [b for b in boxes if b[4] > b[5]]
        if not portraits or not landscapes:
            print(f"  page {page_no} ({line_id}): door or kitchen image not found - skipped")
            continue
        rendered = page.render(scale=scale).to_pil()

        def crop(b):
            left, bottom, right, top = b[:4]
            return rendered.crop((int(left * scale), int((height - top) * scale),
                                  int(right * scale), int((height - bottom) * scale)))

        door = crop(max(portraits, key=lambda b: b[4] * b[5]))
        kitchen = crop(max(landscapes, key=lambda b: b[4] * b[5]))
        folder = OUT / business / line_id
        _save(door, folder / "door.jpg", 520)
        _save(kitchen, folder / "kitchen.jpg", 1280)
        done.append(line_id)
        print(f"  page {page_no}: {line_id} -> door {door.size}, kitchen {kitchen.size}")
    pdf.close()
    return done


if __name__ == "__main__":
    for bid, spec in SPEC_BOOKS.items():
        if not (ROOT / spec["pdf"]).exists():
            print(f"{bid}: spec book not found ({spec['pdf']}) - skipped")
            continue
        print(f"{bid}:")
        lines = extract(bid, spec)
        print(f"{bid}: {len(lines)} door styles with photos")
