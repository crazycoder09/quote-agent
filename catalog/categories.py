"""
categories.py
-------------
One product taxonomy shared by every business, so search can say "kitchen sinks" or "range hoods"
no matter which supplier sells them. Categories are used to FIND products, never to price them.
"""

from __future__ import annotations

import re
from typing import Optional

CATEGORIES = {
    "cabinet": "Cabinets",
    "filler_panel_molding": "Fillers, panels, moldings & trim",
    "cabinet_accessory": "Cabinet organizers & accessories",
    "hood": "Range hoods & hood accessories",
    "hardware": "Knobs & pulls",
    "shelf": "Floating shelves",
    "modification": "Cabinet / hood modifications",
    "kitchen_sink": "Kitchen sinks",
    "bathroom_sink": "Bathroom sinks",
    "sink_accessory": "Sink grids, strainers, drains & disposals",
    "faucet": "Faucets & shower fixtures",
    "bathtub": "Bathtubs",
    "toilet": "Toilets & flush valves",
    "shower": "Shower doors, bases & panels",
    "vanity_top": "Vanity tops",
    "bath_accessory": "Bath accessories",
    "flooring": "Vinyl flooring",
    "flooring_trim": "Flooring trim & stair nose",
    "tile": "Tile & mosaics",
    "quartz_slab": "Quartz slabs",
    "part": "Replacement parts & touch-up",
    "sample": "Samples & displays",
    "literature": "Literature & marketing",
    "other": "Other",
}

CABINET_TYPES = ("wall", "base", "sink_base", "drawer_base", "corner", "tall", "vanity", "oven", "bridge", "special")


def _has(text: str, *words: str) -> bool:
    return any(w in text for w in words)


def cabinet_type(text: str) -> str:
    t = text.lower()
    if _has(t, "vanity"):
        return "vanity"
    if _has(t, "sink"):
        return "sink_base"
    if _has(t, "oven", "microwave"):
        return "oven"
    if _has(t, "pantry", "utility", "tall", " tc", "broom"):
        return "tall"
    if _has(t, "blind", "corner", "lazy susan", "diagonal"):
        return "corner"
    if _has(t, "bridge"):
        return "bridge"
    if _has(t, "drawer base", "drawer cabinet", "3 drawer", "2 drawer", "drawer"):
        return "drawer_base" if "base" in t or "drawer base" in t else ("wall" if "wall" in t else "drawer_base")
    if _has(t, "wall", "mullion", "glass", "end shelf"):
        return "wall"
    if _has(t, "base"):
        return "base"
    return "special"


_TRIM_WORDS = ("filler", "molding", "moulding", "panel", "toe kick", "skin", "scribe", "crown", "cover frame",
               "batten", "valance", "corbel", "bead board", "beadboard", "shiplap", "leg", "trim", "end shelf",
               "touch up", "shim", "overlay")


def classify_trim_or_accessory(text: str) -> str:
    t = text.lower()
    if _has(t, *_TRIM_WORDS):
        return "filler_panel_molding"
    return "cabinet_accessory"


def classify_plumbing(code: str, description: str) -> str:
    """Pelican-style product names -> category (order matters: most specific first)."""
    t = f"{code} {description}".lower()
    if _has(t, "shirt", "swag", "stress ball", "hat ", "koozie", "tumbler"):
        return "literature"
    if _has(t, "catalog", "brochure", "sell sheet", "flyer"):
        return "literature"
    if _has(t, "sample", "display", "chip set", "door set"):
        return "sample"
    if _has(t, "replacement", "cartridge", "push flush button", "flush button", "repair kit", "aerator"):
        return "part"
    if _has(t, "slab"):
        return "quartz_slab"
    if _has(t, "stair nose", "stairnose", "quarter round", "reducer", "t-molding", "end cap", "threshold",
           "baby threshold", "molding", "moulding", "baseboard", "underlayment"):
        return "flooring_trim"
    if _has(t, "vinyl flooring", "laminate flooring", "dryback", "click lock", "spc", "lvp", "planks", "plank") \
            or ("mil" in t and "box" in t):
        return "flooring"
    if _has(t, "deck plate"):
        return "faucet"
    if _has(t, "soap dish"):
        return "bath_accessory"
    if re.search(r"\b1[68]g\b", t) or re.search(r"\bpl-fc\d", t):  # 16/18-gauge stainless, fireclay models
        return "kitchen_sink"
    if _has(t, "mosaic", "tile", "subway", "hexagon", "penny round"):
        return "tile"
    if _has(t, "vanity top", "cultured marble"):
        return "vanity_top"
    if _has(t, "towel bar", "towel ring", "robe hook", "paper holder", "accessories set", "accessory set", "grab bar"):
        return "bath_accessory"
    if _has(t, "bottom grid", " grid", "strainer", "drain", "flange", "disposal", "soap dispenser", "colander",
           "cutting board", "air switch", "roll-up", "roll up rack", "dish rack", "overflow"):
        return "sink_accessory"
    if _has(t, "faucet", "shower head", "showerhead", "hand shower", "shower system", "shower valve", "tub spout",
           "body spray", "pot filler", "rough-in valve", "trim kit"):
        return "faucet"
    if _has(t, "toilet", "water closet", "bidet", "flushometer", "urinal", "toilet seat"):
        return "toilet"
    if _has(t, "shower door", "shower base", "shower pan", "shower panel", "shower wall", "tub door", "shower"):
        return "shower"
    if _has(t, "bathtub", "tub", "soaking"):
        return "bathtub"
    if _has(t, "bathroom sink", "vessel", "lavatory", "vanity sink", "pedestal", "wall mount sink", "wall-mount sink"):
        return "bathroom_sink"
    if _has(t, "sink"):
        if _has(t, "bath", "13\"", "vanity"):
            return "bathroom_sink"
        return "kitchen_sink"
    return "other"


def label(category: Optional[str]) -> str:
    return CATEGORIES.get(category or "other", "Other")
