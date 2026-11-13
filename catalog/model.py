"""
model.py
--------
The common shape every importer produces, whatever the supplier's file looks like.

    Business      who sells it + its pricing rules (multiplier, assembly, tiers ...)
    Line          a door style / color / finish inside a business (HCC Colonial White, KCD Brooklyn Fawn)
    Item          one sellable thing at one price (an HCC W2136 in Colonial White is one item)
    Charge        a service, edge, cutout or modification priced by rule (per each / sq ft / linear ft)
    MaterialGroup countertop price group (Quartz Tier 4 = $x per sq ft)
    Color         countertop color -> its price group
    Alias         another name for a code (e.g. a catalog model number that maps to a price-list code)
    Issue         anything the importer had to fix, skip or could not decide (goes to the report)
    Check         a verification against the workbook's own computed numbers
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class Business:
    id: str
    name: str
    kind: str
    source_files: List[str]
    pricing: Dict[str, Any]
    documents: List[str] = field(default_factory=list)


@dataclass
class Line:
    business: str
    id: str
    name: str
    internal_name: str = ""
    multiplier: Optional[Decimal] = None
    aliases: List[str] = field(default_factory=list)
    sort: int = 0


@dataclass
class Item:
    business: str
    sku: str                      # exactly as the supplier writes it
    code: str                     # what a customer/designer would type (no line prefix)
    description: str
    category: str                 # shared taxonomy (see categories.py)
    list_price: Optional[Decimal]  # price in the source before multiplier / markup
    source_ref: str               # "file.xlsm | Worksheet!L38"
    line: Optional[str] = None
    section: str = ""             # the supplier's own section / group title
    unit: str = "each"
    unit_note: str = ""           # e.g. "17 pcs / box, 41.99 sq ft / box"
    assembly_price: Optional[Decimal] = None
    weight_lb: Optional[float] = None
    volume_m3: Optional[float] = None
    width_in: Optional[float] = None
    height_in: Optional[float] = None
    depth_in: Optional[float] = None
    quotable: bool = True
    not_quotable_reason: str = ""
    tiers: List[Tuple[int, Decimal]] = field(default_factory=list)  # (min quantity, unit price)
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Charge:
    business: str
    code: str
    name: str
    kind: str                     # service | edge | cutout | modification | removal | fee
    amount: Optional[Decimal]
    unit: str                     # each | sqft | lf | door | cut | drawer
    source_ref: str
    scope: str = "all"            # countertops: all | granite_quartz | porcelain | dekton
    needs_staff: bool = False     # can't be fully priced automatically
    note: str = ""
    raw: str = ""


@dataclass
class MaterialGroup:
    business: str
    id: str                       # e.g. quartz-tier-4
    material: str                 # granite | quartz | eclos | dekton | porcelain
    name: str                     # "Quartz Tier 4"
    rate_per_sqft: Optional[Decimal]
    source_ref: str
    note: str = ""


@dataclass
class Color:
    business: str
    name: str
    group_id: Optional[str]
    note: str = ""
    source_ref: str = ""


@dataclass
class Alias:
    alias: str
    business: str
    target_code: str              # match key of the target item code (or model prefix)
    kind: str                     # catalog_model | designer_code | supplier_name
    confirmed: bool = True
    note: str = ""


@dataclass
class Issue:
    business: str
    severity: str                 # error (blocks publishing) | warning (needs a human look) | info (auto-fixed)
    kind: str
    message: str
    ref: str = ""


@dataclass
class Check:
    business: str
    name: str
    passed: bool
    checked: int
    detail: str = ""


@dataclass
class ImportResult:
    business: Business
    lines: List[Line] = field(default_factory=list)
    items: List[Item] = field(default_factory=list)
    charges: List[Charge] = field(default_factory=list)
    groups: List[MaterialGroup] = field(default_factory=list)
    colors: List[Color] = field(default_factory=list)
    aliases: List[Alias] = field(default_factory=list)
    issues: List[Issue] = field(default_factory=list)
    checks: List[Check] = field(default_factory=list)
    counts: Dict[str, int] = field(default_factory=dict)   # accounting: source rows -> kept / skipped

    def issue(self, severity: str, kind: str, message: str, ref: str = "") -> None:
        self.issues.append(Issue(self.business.id, severity, kind, message, ref))

    def check(self, name: str, passed: bool, checked: int, detail: str = "") -> None:
        self.checks.append(Check(self.business.id, name, passed, checked, detail))
