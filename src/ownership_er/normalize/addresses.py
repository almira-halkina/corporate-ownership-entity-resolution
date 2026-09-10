"""Address and postcode normalisation.

Address is the single most useful non-name feature in this dataset. A shared
registered address is weak evidence on its own — company formation agents
register tens of thousands of companies at one address, so treating address
equality as a strong signal creates enormous false-positive clusters. The
module therefore computes both a normalised address and the information needed
to *discount* it: how many distinct entities share it.
"""

from __future__ import annotations

import re
from functools import lru_cache

from ownership_er.normalize.names import normalize_text

__all__ = [
    "address_key",
    "is_uk_postcode",
    "normalize_address",
    "normalize_postcode",
    "postcode_area",
    "postcode_district",
]

# UK postcode grammar, per the Royal Mail specification: one or two letters,
# one or two digits with an optional trailing letter, then a space, then the
# inward code (digit + two letters). Deliberately anchored and strict — a loose
# pattern matches company numbers and house numbers and pollutes the key.
_UK_POSTCODE_RE = re.compile(
    r"^([A-Z]{1,2}[0-9][A-Z0-9]?)\s*([0-9][A-Z]{2})$",
    flags=re.IGNORECASE,
)

_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s]")

# Tokens that appear in essentially every UK address and carry no discriminative
# value once the postcode is present.
_ADDRESS_STOPWORDS: frozenset[str] = frozenset(
    {
        "flat",
        "apartment",
        "apt",
        "unit",
        "suite",
        "floor",
        "fl",
        "room",
        "street",
        "st",
        "road",
        "rd",
        "avenue",
        "ave",
        "lane",
        "ln",
        "drive",
        "dr",
        "close",
        "court",
        "ct",
        "place",
        "pl",
        "square",
        "sq",
        "way",
        "terrace",
        "gardens",
        "grove",
        "hill",
        "park",
        "house",
        "building",
        "bldg",
        "block",
        "the",
        "of",
        "and",
        "at",
        "po",
        "box",
        "c",
        "o",
        "care",
        "united",
        "kingdom",
        "uk",
        "england",
        "scotland",
        "wales",
        "northern",
        "ireland",
        "great",
        "britain",
        "gb",
    }
)


@lru_cache(maxsize=200_000)
def normalize_postcode(value: str | None) -> str:
    """Return a canonical ``OUTWARD INWARD`` postcode, or ``""`` if not valid."""
    if not value:
        return ""
    raw = _WS_RE.sub("", str(value)).upper()
    if len(raw) < 5 or len(raw) > 8:
        return ""
    candidate = f"{raw[:-3]} {raw[-3:]}"
    m = _UK_POSTCODE_RE.match(candidate)
    if not m:
        return ""
    return f"{m.group(1).upper()} {m.group(2).upper()}"


def is_uk_postcode(value: str | None) -> bool:
    return bool(normalize_postcode(value))


def postcode_area(value: str | None) -> str:
    """Leading alphabetic area, e.g. ``EC`` from ``EC1V 9NR``."""
    pc = normalize_postcode(value)
    if not pc:
        return ""
    m = re.match(r"^([A-Z]{1,2})", pc)
    return m.group(1) if m else ""


def postcode_district(value: str | None) -> str:
    """Outward code, e.g. ``EC1V`` from ``EC1V 9NR``."""
    pc = normalize_postcode(value)
    return pc.split(" ")[0] if pc else ""


def normalize_address(*parts: str | None) -> str:
    """Join and normalise address lines into a single comparable string."""
    joined = " ".join(str(p) for p in parts if p)
    if not joined:
        return ""
    text = normalize_text(joined)
    text = _PUNCT_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def address_key(*parts: str | None) -> str:
    """A blocking-grade address key: building number plus postcode.

    Uses the first numeric token as a proxy for the building or flat number and
    pairs it with the postcode. This is far more selective than the full string
    (which varies in how the same address is typed) while remaining stable
    across ``"12 High St"`` and ``"12, High Street"``.
    """
    joined = normalize_address(*parts)
    if not joined:
        return ""
    postcode = ""
    for token in reversed(joined.split()):
        pc = normalize_postcode(token)
        if pc:
            postcode = pc
            break
    if not postcode:
        # Fall back to trying the trailing two tokens as a split postcode.
        toks = joined.split()
        if len(toks) >= 2:
            postcode = normalize_postcode("".join(toks[-2:]))
    number = ""
    for token in joined.split():
        if token.isdigit():
            number = token
            break
    if postcode and number:
        return f"{number}|{postcode}"
    if postcode:
        return f"|{postcode}"
    significant = [t for t in joined.split() if t not in _ADDRESS_STOPWORDS]
    return " ".join(significant[:4])
