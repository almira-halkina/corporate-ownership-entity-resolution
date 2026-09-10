"""Probabilistic matcher: Fellegi-Sunter with EM, via Splink 4 on DuckDB.

Why this sits alongside the rule matcher
----------------------------------------
The rule matcher's weights are asserted. Reasonable, documented, defensible —
but asserted. Fellegi-Sunter estimates the equivalent quantities from the data
itself, learning for each comparison level two probabilities: ``m``, the chance
of observing that level given the pair *is* a match, and ``u``, the chance of
observing it given the pair is *not*. The match weight is ``log2(m/u)``, and
because ``u`` is estimated from random pairs, the model automatically discovers
what the rule matcher has to be told: that agreement on a rare surname is worth
far more than agreement on a common one.

Splink is used rather than a hand-rolled EM implementation because it is the
reference open-source implementation of this model — built by the UK Ministry
of Justice, and the tool UK government analysts doing record linkage on
administrative data actually reach for. It also runs its whole computation as
SQL against the same DuckDB file this pipeline already uses, so there is no
serialisation boundary and no second copy of the data.

Term frequency adjustment is the feature that matters most here. Without it,
matching "Smith" is worth exactly as much as matching "Kowalczyk". With it, the
model discounts the former and rewards the latter in proportion to how often
each actually occurs in this dataset — which on a corporate register dominated
by a small pool of common surnames is the difference between a usable model and
an unusable one.

The comparison with the rule matcher is reported honestly in
``docs/evaluation.md``, including where the rule matcher wins.
"""

from __future__ import annotations

from typing import Any

import duckdb

__all__ = ["SPLINK_AVAILABLE", "SplinkMatcher"]

try:  # pragma: no cover - optional extra
    import splink.comparison_library as cl
    from splink import DuckDBAPI, Linker, SettingsCreator, block_on

    SPLINK_AVAILABLE = True
except Exception:  # pragma: no cover
    SPLINK_AVAILABLE = False


class SplinkMatcher:
    """Fellegi-Sunter matcher trained by expectation-maximisation.

    Training deliberately uses no labels. ``estimate_u_using_random_sampling``
    derives ``u`` from random record pairs, which are overwhelmingly
    non-matches, and EM then estimates ``m`` from blocked comparisons. The
    labelled data is thereby preserved for evaluation rather than being spent on
    fitting — which is what keeps the reported numbers meaningful, since a model
    tuned on the same pairs it is scored against tells you nothing.
    """

    name = "splink"

    def __init__(
        self,
        *,
        accept: float = 0.92,
        reject: float = 0.62,
        max_pairs_for_training: int = 2_000_000,
        seed: int = 20260805,
    ) -> None:
        if not SPLINK_AVAILABLE:  # pragma: no cover
            raise ImportError(
                "Splink is not installed. Install the optional extra:\n"
                "    pip install -e '.[splink]'"
            )
        self.accept = accept
        self.reject = reject
        self.max_pairs_for_training = max_pairs_for_training
        self.seed = seed
        self.trained_settings: dict[str, Any] | None = None

    # -- settings -----------------------------------------------------------

    def _person_settings(self) -> Any:
        """Comparison levels for individuals.

        Levels are ordered strongest to weakest within each comparison, which
        is what Splink's model expects, and each is a *distinct* level rather
        than a threshold on a single score — that separation is what lets EM
        learn a different weight for "exact match" than for "close but not
        exact", instead of forcing them onto one linear scale.
        """
        return SettingsCreator(
            link_type="dedupe_only",
            comparisons=[
                cl.NameComparison(
                    "last_name",
                    jaro_winkler_thresholds=[0.95, 0.88, 0.80],
                ).configure(term_frequency_adjustments=True),
                cl.NameComparison(
                    "first_name",
                    jaro_winkler_thresholds=[0.95, 0.88, 0.80],
                ).configure(term_frequency_adjustments=True),
                cl.ExactMatch("birth_year").configure(term_frequency_adjustments=True),
                cl.ExactMatch("birth_month"),
                cl.ExactMatch("nationality").configure(term_frequency_adjustments=True),
                cl.LevenshteinAtThresholds("postcode", [1, 2]),
                cl.JaccardAtThresholds("address_norm", [0.8, 0.5]),
            ],
            blocking_rules_to_generate_predictions=[
                block_on("name_fp", "birth_year"),
                block_on("last_name", "birth_year"),
                block_on("first_name", "birth_year"),
                block_on("name_phonetic", "birth_year"),
                block_on("name_fp"),
                block_on("last_name", "postcode"),
            ],
            retain_intermediate_calculation_columns=True,
        )

    def _company_settings(self) -> Any:
        return SettingsCreator(
            link_type="dedupe_only",
            comparisons=[
                cl.NameComparison("name_fp", jaro_winkler_thresholds=[0.95, 0.88, 0.80]).configure(
                    term_frequency_adjustments=True
                ),
                cl.ExactMatch("reg_number").configure(term_frequency_adjustments=True),
                cl.ExactMatch("jurisdiction"),
                cl.LevenshteinAtThresholds("postcode", [1, 2]),
                cl.JaccardAtThresholds("address_norm", [0.8, 0.5]),
            ],
            blocking_rules_to_generate_predictions=[
                block_on("reg_number"),
                block_on("name_fp"),
                block_on("name_phonetic"),
                block_on("postcode"),
            ],
            retain_intermediate_calculation_columns=True,
        )

    # -- scoring ------------------------------------------------------------

    def score_pairs(self, con: duckdb.DuckDBPyConnection, entity_type: str) -> int:
        """Train on ``records`` of one type and write predictions to ``pair_scores``."""
        source = con.execute(
            """
            SELECT record_id AS unique_id, name_fp, name_phonetic,
                   coalesce(nullif(first_name, ''), NULL)  AS first_name,
                   coalesce(nullif(last_name, ''), NULL)   AS last_name,
                   birth_year, birth_month,
                   coalesce(nullif(nationality, ''), NULL) AS nationality,
                   coalesce(nullif(postcode, ''), NULL)    AS postcode,
                   coalesce(nullif(address_norm, ''), NULL) AS address_norm,
                   coalesce(nullif(reg_number, ''), NULL)  AS reg_number,
                   coalesce(nullif(jurisdiction, ''), NULL) AS jurisdiction
            FROM records
            WHERE entity_type = ?
            """,
            [entity_type],
        ).df()

        if len(source) < 2:
            return 0

        settings = self._person_settings() if entity_type == "Person" else self._company_settings()
        linker = Linker(source, settings, db_api=DuckDBAPI())

        linker.training.estimate_probability_two_random_records_match(
            [block_on("name_fp", "birth_year")]
            if entity_type == "Person"
            else [block_on("reg_number")],
            recall=0.8,
        )
        linker.training.estimate_u_using_random_sampling(
            max_pairs=min(self.max_pairs_for_training, 1_000_000), seed=self.seed
        )

        # EM is run twice with different blocking rules. Each pass holds the
        # blocked-on columns fixed and can only learn the others, so a single
        # pass leaves some parameters unestimated; two passes blocking on
        # different columns cover the full set between them.
        em_rules = (
            [block_on("last_name", "birth_year"), block_on("name_fp")]
            if entity_type == "Person"
            else [block_on("name_fp"), block_on("postcode")]
        )
        for rule in em_rules:
            try:
                linker.training.estimate_parameters_using_expectation_maximisation(rule)
            except Exception:  # pragma: no cover - degenerate blocks on tiny inputs
                continue

        self.trained_settings = linker.misc.save_model_to_json()

        predictions = linker.inference.predict(
            threshold_match_probability=max(0.0, self.reject - 0.2)
        ).as_pandas_dataframe()
        if predictions.empty:
            return 0

        predictions = predictions.rename(
            columns={
                "unique_id_l": "left_id",
                "unique_id_r": "right_id",
                "match_probability": "score",
            }
        )[["left_id", "right_id", "score", "match_weight"]]

        con.register("splink_predictions", predictions)
        con.execute(
            f"""
            INSERT INTO pair_scores
                (left_id, right_id, matcher, score, decision, features, rationale)
            SELECT
                least(left_id, right_id)    AS left_id,
                greatest(left_id, right_id) AS right_id,
                '{self.name}',
                max(score),
                CASE
                    WHEN max(score) >= {self.accept} THEN 'accept'
                    WHEN max(score) <  {self.reject} THEN 'reject'
                    ELSE 'uncertain'
                END,
                NULL,
                'fellegi-sunter match_weight=' || printf('%.3f', max(match_weight))
            FROM splink_predictions
            GROUP BY 1, 2
            """
        )
        con.unregister("splink_predictions")

        row = con.execute(
            "SELECT count(*) FROM pair_scores WHERE matcher = ?", [self.name]
        ).fetchone()
        return int(row[0]) if row else 0
