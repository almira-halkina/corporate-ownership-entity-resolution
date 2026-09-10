"""Matcher interface and the accept / uncertain / reject decision rule."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

import duckdb

__all__ = ["Decision", "Matcher", "ScoredPair", "decide", "write_scores"]


class Decision(str, Enum):
    ACCEPT = "accept"
    UNCERTAIN = "uncertain"
    REJECT = "reject"


@dataclass(slots=True)
class ScoredPair:
    left_id: str
    right_id: str
    matcher: str
    score: float
    decision: Decision
    features: dict[str, Any] | None = None
    rationale: str = ""


class Matcher(Protocol):
    """Anything that can score candidate pairs.

    Kept minimal so the three implementations — a hand-weighted rule model, a
    trained Fellegi-Sunter model, and an LLM judge — are genuinely
    interchangeable in the evaluation harness. If comparing them required
    special-casing each, the comparison would not be a fair one.
    """

    name: str

    def score_pairs(
        self, con: duckdb.DuckDBPyConnection, entity_type: str
    ) -> int:  # pragma: no cover - protocol
        """Score all candidate pairs of ``entity_type``, writing to ``pair_scores``."""
        ...


def decide(score: float, accept: float, reject: float) -> Decision:
    """Map a score to a decision using two thresholds.

    A single threshold forces every borderline pair into a confident answer.
    Two thresholds preserve the third option — "this needs more than string
    similarity to settle" — which is what the LLM adjudicator and, in a
    production deployment, the human review queue consume. In a compliance
    setting that band is the useful output: a false negative is a missed
    sanctions exposure, and knowing which decisions are shaky is worth more than
    a marginally better single number.
    """
    if score >= accept:
        return Decision.ACCEPT
    if score < reject:
        return Decision.REJECT
    return Decision.UNCERTAIN


def write_scores(
    con: duckdb.DuckDBPyConnection,
    pairs: list[ScoredPair],
    batch_size: int = 20_000,
) -> int:
    """Persist scored pairs to ``pair_scores``."""
    import json

    sql = """
        INSERT INTO pair_scores
            (left_id, right_id, matcher, score, decision, features, rationale)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """
    total = 0
    batch: list[tuple[Any, ...]] = []
    for p in pairs:
        batch.append(
            (
                p.left_id,
                p.right_id,
                p.matcher,
                float(p.score),
                p.decision.value,
                json.dumps(p.features) if p.features else None,
                p.rationale,
            )
        )
        if len(batch) >= batch_size:
            con.executemany(sql, batch)
            total += len(batch)
            batch = []
    if batch:
        con.executemany(sql, batch)
        total += len(batch)
    return total
