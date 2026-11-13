"""
readers.py
----------
File readers that openpyxl can't handle on its own.

read_spreadsheetml(): "Excel 2003 XML" files (what NetSuite exports and names .xls, e.g. Pelican's
price list). Returns {sheet name: [[cell text, ...], ...]} with column positions preserved.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List

_SS = "{urn:schemas-microsoft-com:office:spreadsheet}"


def is_spreadsheetml(path: Path) -> bool:
    with open(path, "rb") as fh:
        head = fh.read(400).lstrip()
    return head.startswith(b"<?xml") and b"urn:schemas-microsoft-com:office:spreadsheet" in head + fh.read(2000)


def read_spreadsheetml(path: Path) -> Dict[str, List[List[str]]]:
    root = ET.parse(path).getroot()
    sheets: Dict[str, List[List[str]]] = {}
    for ws in root.iter(_SS + "Worksheet"):
        rows: List[List[str]] = []
        for row in ws.iter(_SS + "Row"):
            index = row.get(_SS + "Index")
            while index and len(rows) < int(index) - 1:  # skipped (empty) rows
                rows.append([])
            values: List[str] = []
            for cell in row.findall(_SS + "Cell"):
                col = cell.get(_SS + "Index")
                while col and len(values) < int(col) - 1:  # skipped (empty) cells
                    values.append("")
                data = cell.find(_SS + "Data")
                values.append("".join(data.itertext()).strip() if data is not None else "")
            rows.append(values)
        sheets[ws.get(_SS + "Name") or f"Sheet{len(sheets) + 1}"] = rows
    return sheets
