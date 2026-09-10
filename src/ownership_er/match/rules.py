"""Weighted rule matcher: an additive log-odds model over comparison features.

This is the reference matcher, and the baseline everything else is measured
against. It follows the shape OpenSanctions' ``nomenklatura`` uses in
production — a bounded set of interpretable features, each contributing a
weight, combined into a single score — because in a compliance context an
analyst has to be able to answer "why did the system merge these two people?",
and "the gradient boosting said so" is not an answer that survives an audit.

Scoring
-------
Each observed feature contributes to a log-odds sum::

    logodds = prior + sum_f [ v_f * w_agree_f + (1 - v_f) * w_disagree_f ]

where ``v_f`` in [0, 1] is the feature value and unobserved features contribute
nothing at all. The result passes through a logistic to give a score in (0, 1).

Two properties follow, and both matter. Partial agreement contributes
proportionally, so a Jaro-Winkler of 0.85 is not rounded to a binary. And
because the sum runs only over observed features, a sparse record is not
penalised for its sparsity — it simply accumulates less evidence in either
direction and lands nearer the prior, which is the honest position.

Weights
-------
The defaults below are hand-set from the structure of the data: a birth-year
disagreement is heavily negative because Companies House validates it at filing
and a mismatch is rarely a typo, whereas an address disagreement is only mildly
negative because people move. :func:`calibrate` refits them by logistic
regression on labelled pairs, and ``docs/evaluation.md`` reports both the priors
and the fitted values so the difference is visible rather than buried.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

import duckdb

from ownership_er.match.base import Decision, decide
from ownership_er.match.features import pair_feature_sql

__all__ = ["COMPANY_WEIGHTS", "PERSON_WEIGHTS", "FeatureWeight", "RuleMatcher"]


@dataclass(frozen=True, slots=True)
class FeatureWeight:
    """Log-odds contribution of one feature at full agreement and full disagreement."""

    agree: float
    disagree: float
    note: str = ""


# ---------------------------------------------------------------------------
# Person weights
# ---------------------------------------------------------------------------

PERSON_WEIGHTS: dict[str, FeatureWeight] = {
    "name_fp_jw": FeatureWeight(
        2.6,
        -3.0,
        "Order-insensitive full-name similarity. The workhorse feature; heavily "
        "weighted in both directions because two people who share a birth year, "
        "nationality and address but not a name are not the same person.",
    ),
    "name_token_jaccard": FeatureWeight(
        1.1,
        -0.8,
        "Token overlap. Complements the edit-distance view by rewarding shared "
        "rare tokens even when the strings differ in length.",
    ),
    "last_jw": FeatureWeight(
        1.4,
        -1.6,
        "Surname similarity, weighted above forename: surnames are more stable "
        "across filings and more discriminative than a small forename pool.",
    ),
    "first_jw": FeatureWeight(
        0.9,
        -0.9,
        "Forename similarity. Deliberately modest — anglicisation "
        "(Yevgeniy/Eugene) produces true matches with near-zero string similarity.",
    ),
    "cross_name_jw": FeatureWeight(
        1.0,
        -0.4,
        "Best of straight and swapped field alignment. Rewards recovery from "
        "transposed name fields without punishing the ordinary case.",
    ),
    "phonetic_exact": FeatureWeight(
        1.2,
        -0.2,
        "Metaphone equality. Asymmetric by design: agreement is real evidence, "
        "but disagreement is weak because the codes are coarse and collide freely.",
    ),
    "birth_year_match": FeatureWeight(
        2.2,
        -4.5,
        "The strongest single discriminator. Companies House validates it at "
        "filing, so a mismatch is far more likely to be a different person than "
        "a typo — hence the largest negative weight in the model.",
    ),
    "birth_month_match": FeatureWeight(
        0.9,
        -1.4,
        "Weaker than year: month is more often mis-keyed, and off-by-one errors "
        "are observable in the register.",
    ),
    "middle_compat": FeatureWeight(
        0.6,
        -1.0,
        "Compatible middle names. NULL when either is absent, so the very common "
        "present-vs-absent case contributes nothing rather than counting against.",
    ),
    "nationality_match": FeatureWeight(
        0.8,
        -1.5,
        "Only usable because nationality is ISO-coded upstream; on the raw free "
        "text this feature would be noise.",
    ),
    "country_match": FeatureWeight(
        0.5,
        -0.5,
        "Country of residence. Weak in both directions — people relocate, and "
        "filings are not updated when they do.",
    ),
    "postcode_level": FeatureWeight(
        1.3,
        -0.6,
        "Graded postcode agreement. Positive weight exceeds the negative because "
        "a shared full postcode is strong evidence while a difference is routine.",
    ),
    "address_jaccard": FeatureWeight(
        0.7,
        -0.3,
        "Address token overlap, discounted heavily: service addresses shared by "
        "thousands of filers make address agreement much weaker than it appears.",
    ),
    "address_blk_match": FeatureWeight(
        0.6,
        -0.2,
        "Building-level address equality. Corroborating, never decisive.",
    ),
}

# ---------------------------------------------------------------------------
# Company weights
# ---------------------------------------------------------------------------

COMPANY_WEIGHTS: dict[str, FeatureWeight] = {
    "name_fp_jw": FeatureWeight(
        2.8,
        -3.2,
        "Legal-form-stripped, order-insensitive name similarity.",
    ),
    "name_token_jaccard": FeatureWeight(1.2, -1.0, "Token overlap of the full name."),
    "phonetic_exact": FeatureWeight(0.9, -0.2, "Metaphone equality of the base name."),
    "reg_number_match": FeatureWeight(
        5.0,
        -2.5,
        "Registration number equality — as close to deterministic as this data "
        "offers. The asymmetry is intentional: agreement is near-conclusive, but "
        "disagreement is only moderate evidence because filers frequently enter a "
        "foreign or historical number for the same entity.",
    ),
    "jurisdiction_match": FeatureWeight(
        0.9,
        -2.0,
        "Two companies registered in different jurisdictions are different legal "
        "persons even under an identical name — the negative weight carries that.",
    ),
    "country_match": FeatureWeight(0.5, -0.8, "Country of registration or operation."),
    "postcode_level": FeatureWeight(
        1.1,
        -0.4,
        "Registered-office postcode. Weakened by formation agents concentrating "
        "thousands of registrations at single addresses.",
    ),
    "address_jaccard": FeatureWeight(0.6, -0.3, "Registered address token overlap."),
    "legal_form_match": FeatureWeight(
        0.5,
        -0.7,
        "Legal form. Modest, since the same group's entities differ in form and "
        "filers abbreviate inconsistently.",
    ),
    "incorporation_match": FeatureWeight(
        1.4, -1.2, "Incorporation date equality — highly selective when present."
    ),
}

# Prior log-odds that an arbitrary *candidate* pair is a true match. Set from
# the base rate after blocking, not before: blocking has already discarded the
# overwhelming majority of non-matches, so a prior derived from the full
# quadratic space would be far too pessimistic and would suppress genuine
# matches supported by only two or three observed features.
DEFAULT_PRIOR = -2.2


def _sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    z = math.exp(x)
    return z / (1.0 + z)


def _score_expression(weights: dict[str, FeatureWeight], prior: float) -> str:
    """Build the additive log-odds expression in SQL."""
    terms = [f"{prior}"]
    for name, w in weights.items():
        terms.append(f"coalesce({name} * {w.agree} + (1.0 - {name}) * {w.disagree}, 0.0)")
    return " + ".join(terms)


def _rationale_expression(weights: dict[str, FeatureWeight]) -> str:
    """Build a human-readable explanation listing the top contributing features.

    Emitted for every pair, not only accepted ones. An analyst reviewing a
    borderline decision needs to see what drove it, and reconstructing that
    after the fact from stored features is exactly the friction that stops
    people from auditing at all.
    """
    parts = []
    for name, w in weights.items():
        parts.append(
            f"CASE WHEN {name} IS NULL THEN NULL "
            f"ELSE '{name}=' || printf('%.2f', {name}) || "
            f"'(' || printf('%+.2f', {name} * {w.agree} + (1.0 - {name}) * {w.disagree}) || ')' END"
        )
    return f"array_to_string(list_filter([{', '.join(parts)}], x -> x IS NOT NULL), ', ')"


class RuleMatcher:
    """Interpretable log-odds matcher over the comparison features."""

    name = "rules"

    def __init__(
        self,
        *,
        person_weights: dict[str, FeatureWeight] | None = None,
        company_weights: dict[str, FeatureWeight] | None = None,
        prior: float = DEFAULT_PRIOR,
        accept: float = 0.92,
        reject: float = 0.62,
        store_features: bool = True,
    ) -> None:
        self.person_weights = person_weights or PERSON_WEIGHTS
        self.company_weights = company_weights or COMPANY_WEIGHTS
        self.prior = prior
        self.accept = accept
        self.reject = reject
        self.store_features = store_features

    def weights_for(self, entity_type: str) -> dict[str, FeatureWeight]:
        return self.person_weights if entity_type == "Person" else self.company_weights

    def score_pairs(self, con: duckdb.DuckDBPyConnection, entity_type: str) -> int:
        """Score every candidate pair of ``entity_type`` in one SQL pass."""
        weights = self.weights_for(entity_type)
        feature_sql = pair_feature_sql(entity_type)
        logodds = _score_expression(weights, self.prior)
        rationale = _rationale_expression(weights)
        feature_names = list(weights)

        features_json = (
            "to_json(struct_pack(" + ", ".join(f"{n} := {n}" for n in feature_names) + "))"
            if self.store_features
            else "NULL"
        )

        con.execute(
            f"""
            INSERT INTO pair_scores
                (left_id, right_id, matcher, score, decision, features, rationale)
            WITH f AS ({feature_sql}),
            scored AS (
                SELECT *, ({logodds}) AS logodds FROM f
            )
            SELECT
                left_id,
                right_id,
                '{self.name}' AS matcher,
                1.0 / (1.0 + exp(-logodds)) AS score,
                CASE
                    WHEN 1.0 / (1.0 + exp(-logodds)) >= {self.accept} THEN 'accept'
                    WHEN 1.0 / (1.0 + exp(-logodds)) <  {self.reject} THEN 'reject'
                    ELSE 'uncertain'
                END AS decision,
                {features_json} AS features,
                {rationale} AS rationale
            FROM scored
            """
        )
        row = con.execute(
            "SELECT count(*) FROM pair_scores WHERE matcher = ?", [self.name]
        ).fetchone()
        return int(row[0]) if row else 0

    # -- calibration --------------------------------------------------------

    def score_one(self, features: dict[str, float | None], entity_type: str) -> float:
        """Score a single feature dict in Python. Used by tests and the LLM prompt."""
        weights = self.weights_for(entity_type)
        logodds = self.prior
        for name, w in weights.items():
            v = features.get(name)
            if v is None:
                continue
            logodds += v * w.agree + (1.0 - v) * w.disagree
        return _sigmoid(logodds)

    def explain(self, features: dict[str, float | None], entity_type: str) -> list[str]:
        """Per-feature contributions, largest absolute first."""
        weights = self.weights_for(entity_type)
        rows: list[tuple[str, float]] = []
        for name, w in weights.items():
            v = features.get(name)
            if v is None:
                continue
            rows.append((name, v * w.agree + (1.0 - v) * w.disagree))
        rows.sort(key=lambda t: abs(t[1]), reverse=True)
        return [f"{n} {c:+.2f}" for n, c in rows]


def calibrate(
    labelled: list[tuple[dict[str, float | None], int]],
    entity_type: str,
    *,
    l2: float = 1.0,
    iterations: int = 300,
    learning_rate: float = 0.15,
) -> tuple[dict[str, FeatureWeight], float]:
    """Refit weights by regularised logistic regression on labelled pairs.

    Deliberately a plain gradient descent over the same additive form rather
    than a call into scikit-learn. The point of the exercise is to keep the
    fitted model *identical in structure* to the hand-weighted one, so the two
    can be compared coefficient by coefficient and a surprising fitted weight
    can be interrogated. Swapping in a different functional form would make the
    comparison meaningless, and adding a heavy dependency to fit a dozen
    parameters is not a trade worth making.

    Missing features contribute nothing to the gradient, matching how they are
    treated at scoring time.
    """
    names = sorted({k for feats, _ in labelled for k in feats})
    if not names or not labelled:
        return {}, DEFAULT_PRIOR

    w_agree = dict.fromkeys(names, 0.0)
    w_disagree = dict.fromkeys(names, 0.0)
    bias = DEFAULT_PRIOR
    n = len(labelled)

    for _ in range(iterations):
        g_agree = dict.fromkeys(names, 0.0)
        g_disagree = dict.fromkeys(names, 0.0)
        g_bias = 0.0
        for feats, label in labelled:
            z = bias
            for name in names:
                v = feats.get(name)
                if v is None:
                    continue
                z += v * w_agree[name] + (1.0 - v) * w_disagree[name]
            err = _sigmoid(z) - label
            g_bias += err
            for name in names:
                v = feats.get(name)
                if v is None:
                    continue
                g_agree[name] += err * v
                g_disagree[name] += err * (1.0 - v)

        bias -= learning_rate * g_bias / n
        for name in names:
            w_agree[name] -= learning_rate * (g_agree[name] / n + l2 * w_agree[name] / n)
            w_disagree[name] -= learning_rate * (g_disagree[name] / n + l2 * w_disagree[name] / n)

    fitted = {
        name: FeatureWeight(
            agree=round(w_agree[name], 3),
            disagree=round(w_disagree[name], 3),
            note="fitted by logistic regression on the labelled set",
        )
        for name in names
    }
    return fitted, round(bias, 3)


def weights_to_json(weights: dict[str, FeatureWeight], prior: float) -> str:
    return json.dumps(
        {
            "prior": prior,
            "weights": {
                k: {"agree": v.agree, "disagree": v.disagree, "note": v.note}
                for k, v in weights.items()
            },
        },
        indent=2,
    )


def decision_for(score: float, accept: float, reject: float) -> Decision:
    return decide(score, accept, reject)


def _unused(_: Any) -> None:  # pragma: no cover - keeps linters honest about re-exports
    return None
