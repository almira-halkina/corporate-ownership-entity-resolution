"""Parsing Companies House ``natures_of_control`` into quantified ownership.

The registry does not publish exact shareholdings. It publishes banded
statements drawn from a controlled vocabulary, e.g.::

    ownership-of-shares-25-to-50-percent
    voting-rights-75-to-100-percent-as-trust
    right-to-appoint-and-remove-directors
    significant-influence-or-control

Two consequences shape everything downstream.

First, ownership is an *interval*, not a number. Multiplying bands along a
chain has to propagate intervals, or the result implies a precision the source
does not have. :func:`parse_natures` therefore returns explicit minimum and
maximum percentages and the traversal code multiplies both bounds.

Second, control is not only equity. A person holding no shares but the right to
appoint and remove directors controls the company completely. Any analysis that
ranks by percentage alone will miss them, which is exactly the structure a
party wanting to stay off a screening tool would choose.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "CONTROL_KINDS",
    "ControlStatement",
    "ControlSummary",
    "parse_nature",
    "parse_natures",
]

# Trailing qualifiers describing the capacity in which control is held.
_CAPACITY_RE = re.compile(
    r"-(as-(?:trust|firm)(?:-limited-liability-partnership)?|limited-liability-partnership)$"
)
_BAND_RE = re.compile(r"(\d+)-to-(\d+)-percent")

CONTROL_KINDS: dict[str, str] = {
    "ownership-of-shares": "SHARES",
    "voting-rights": "VOTING",
    "right-to-appoint-and-remove-directors": "APPOINT_DIRECTORS",
    "right-to-appoint-and-remove-members": "APPOINT_MEMBERS",
    "right-to-appoint-and-remove-person": "APPOINT_PERSON",
    "right-to-share-surplus-assets": "SURPLUS_ASSETS",
    "significant-influence-or-control": "SIGNIFICANT_INFLUENCE",
    "part-right-to-share-surplus-assets": "SURPLUS_ASSETS",
}


@dataclass(frozen=True, slots=True)
class ControlStatement:
    """One parsed statement of control."""

    raw: str
    kind: str
    min_percent: float | None
    max_percent: float | None
    capacity: str | None  # "TRUST", "FIRM", "LLP" or None (held directly)

    @property
    def is_equity(self) -> bool:
        return self.kind == "SHARES"

    @property
    def is_hard_control(self) -> bool:
        """True where the statement implies control regardless of shareholding."""
        return self.kind in {
            "APPOINT_DIRECTORS",
            "APPOINT_MEMBERS",
            "APPOINT_PERSON",
        } or (
            self.max_percent is not None
            and self.min_percent is not None
            and self.min_percent >= 50.0
        )

    @property
    def is_indirect_capacity(self) -> bool:
        """Held through a trust, firm or LLP rather than directly.

        A material flag: the named PSC is a nominee or fiduciary layer, so the
        real controlling party sits behind a structure the register does not
        expose.
        """
        return self.capacity is not None


def parse_nature(nature: str) -> ControlStatement:
    """Parse a single ``natures_of_control`` string."""
    raw = (nature or "").strip().lower()
    if not raw:
        return ControlStatement(
            raw="", kind="UNKNOWN", min_percent=None, max_percent=None, capacity=None
        )

    capacity: str | None = None
    cap_match = _CAPACITY_RE.search(raw)
    body = raw
    if cap_match:
        token = cap_match.group(1)
        if "trust" in token:
            capacity = "TRUST"
        elif "firm" in token:
            capacity = "FIRM"
        else:
            capacity = "LLP"
        body = raw[: cap_match.start()]

    min_pct: float | None = None
    max_pct: float | None = None
    band = _BAND_RE.search(body)
    if band:
        min_pct = float(band.group(1))
        max_pct = float(band.group(2))
        body = body[: band.start()].rstrip("-")

    kind = CONTROL_KINDS.get(body, "")
    if not kind:
        # Prefix match handles vocabulary additions without failing the record.
        for prefix, mapped in CONTROL_KINDS.items():
            if body.startswith(prefix):
                kind = mapped
                break
    if not kind:
        kind = "OTHER"

    return ControlStatement(
        raw=nature, kind=kind, min_percent=min_pct, max_percent=max_pct, capacity=capacity
    )


@dataclass(frozen=True, slots=True)
class ControlSummary:
    """All statements on one PSC filing, aggregated into edge attributes.

    A typed result rather than a plain dict. The dict version type-checked as
    ``dict[str, object]``, which erased every field type and forced casts at
    each of the half-dozen call sites that build a relationship — exactly the
    places where confusing ``min_percent`` with ``max_percent`` would produce a
    plausible-looking wrong number rather than a crash.
    """

    kinds: list[str]
    min_percent: float | None
    max_percent: float | None
    has_hard_control: bool
    via_fiduciary: bool
    capacities: list[str]
    raw: list[str]


def parse_natures(natures: list[str] | None) -> ControlSummary:
    """Aggregate all statements on one PSC filing into edge attributes.

    Where several equity statements are present the widest band is taken, since
    they describe the same holding under different qualifications rather than
    additive stakes.
    """
    statements = [parse_nature(n) for n in (natures or []) if n]
    if not statements:
        return ControlSummary(
            kinds=[],
            min_percent=None,
            max_percent=None,
            has_hard_control=False,
            via_fiduciary=False,
            capacities=[],
            raw=[],
        )

    equity = [s for s in statements if s.is_equity and s.min_percent is not None]
    voting = [s for s in statements if s.kind == "VOTING" and s.min_percent is not None]
    quantified = equity or voting

    # Bounds are filtered to non-None before comparison rather than relying on
    # min()/max() to cope, so the types stay honest.
    mins = [s.min_percent for s in quantified if s.min_percent is not None]
    maxes = [s.max_percent for s in quantified if s.max_percent is not None]

    return ControlSummary(
        kinds=sorted({s.kind for s in statements}),
        min_percent=min(mins) if mins else None,
        max_percent=max(maxes) if maxes else None,
        has_hard_control=any(s.is_hard_control for s in statements),
        via_fiduciary=any(s.is_indirect_capacity for s in statements),
        capacities=sorted({s.capacity for s in statements if s.capacity}),
        raw=[s.raw for s in statements],
    )
