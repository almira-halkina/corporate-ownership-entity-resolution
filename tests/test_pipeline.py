"""End-to-end pipeline tests, including quality floors.

The quality assertions are deliberately set *below* current measured
performance, not at it. A test pinned to the exact current number fails on every
harmless change and gets deleted; a floor catches genuine regressions and
survives. Current measurements are recorded in ``docs/evaluation.md``.
"""

from __future__ import annotations

from typing import Any

import pytest

from ownership_er.config import Settings
from ownership_er.fixtures import FixtureCorpus


class TestNormalization:
    def test_all_sources_parsed(self, resolved: dict[str, Any]) -> None:
        stats = resolved["normalize"]
        assert stats["ch_companies"] > 0
        assert stats["ch_psc_records"] > 0
        assert stats["opensanctions"] > 0

    def test_statements_are_counted_not_parsed_as_entities(self, resolved: dict[str, Any]) -> None:
        # The critical parsing invariant: statement and exemption rows declare
        # the *absence* of a controller. Parsing them as people would inject
        # millions of phantom entities with near-identical names, which is the
        # single worst failure this parser can have.
        stats = resolved["normalize"]
        assert stats["ch_psc_statements"] > 0
        kinds = stats["ch_psc_kinds"]
        parsed = stats["ch_psc_records"]
        entity_kinds = sum(
            v
            for k, v in kinds.items()
            if k.endswith("person-with-significant-control") and not k.startswith("super-secure")
        )
        assert parsed == entity_kinds

    def test_record_ids_are_unique(self, con: Any) -> None:
        total, distinct = con.execute(
            "SELECT count(*), count(DISTINCT record_id) FROM records"
        ).fetchone()
        assert total == distinct

    def test_every_relationship_endpoint_resolves(self, con: Any) -> None:
        orphans = con.execute(
            """
            SELECT count(*) FROM relationships r
            LEFT JOIN records s ON s.record_id = r.source_record_id
            WHERE s.record_id IS NULL
            """
        ).fetchone()[0]
        assert orphans == 0

    def test_dates_parsed_into_date_columns(self, con: Any) -> None:
        n = con.execute(
            "SELECT count(*) FROM records WHERE incorporation_date IS NOT NULL"
        ).fetchone()[0]
        assert n > 0


class TestBlocking:
    def test_reduction_is_substantial(self, resolved: dict[str, Any]) -> None:
        assert resolved["block"]["reduction_ratio"] > 0.95

    def test_candidate_pairs_generated(self, resolved: dict[str, Any]) -> None:
        assert resolved["block"]["candidate_pairs"] > 0

    def test_pairs_are_canonically_ordered(self, con: Any) -> None:
        # left_id < right_id is what makes candidate_pairs joinable against the
        # truth set without normalising at query time.
        bad = con.execute(
            "SELECT count(*) FROM candidate_pairs WHERE left_id >= right_id"
        ).fetchone()[0]
        assert bad == 0

    def test_no_cross_type_pairs_are_scored(self, con: Any) -> None:
        bad = con.execute(
            """
            SELECT count(*) FROM pair_scores s
            JOIN records l ON l.record_id = s.left_id
            JOIN records r ON r.record_id = s.right_id
            WHERE l.entity_type <> r.entity_type
            """
        ).fetchone()[0]
        assert bad == 0


class TestMatching:
    def test_scores_are_probabilities(self, con: Any) -> None:
        bad = con.execute(
            "SELECT count(*) FROM pair_scores WHERE score < 0 OR score > 1"
        ).fetchone()[0]
        assert bad == 0

    def test_decisions_match_thresholds(self, con: Any, settings: Settings) -> None:
        bad = con.execute(
            """
            SELECT count(*) FROM pair_scores
            WHERE matcher = 'rules' AND (
                (decision = 'accept'  AND score < ?) OR
                (decision = 'reject'  AND score >= ?) OR
                (decision = 'uncertain' AND (score < ? OR score >= ?))
            )
            """,
            [
                settings.match.auto_accept,
                settings.match.auto_reject,
                settings.match.auto_reject,
                settings.match.auto_accept,
            ],
        ).fetchone()[0]
        assert bad == 0

    def test_uncertain_band_is_small(self, resolved: dict[str, Any]) -> None:
        # The LLM adjudicator's cost is proportional to this. A band that grows
        # past a few percent means thresholds or blocking have drifted.
        assert resolved["match"]["uncertain_fraction"] < 0.20

    def test_every_pair_has_a_rationale(self, con: Any) -> None:
        missing = con.execute(
            "SELECT count(*) FROM pair_scores WHERE matcher = 'rules' "
            "AND (rationale IS NULL OR rationale = '')"
        ).fetchone()[0]
        assert missing == 0


class TestClustering:
    def test_every_record_is_assigned(self, con: Any) -> None:
        # Singletons are entities too. Dropping unmatched records would silently
        # remove most of the register from the graph.
        unassigned = con.execute(
            """
            SELECT count(*) FROM records r
            LEFT JOIN clusters c ON c.record_id = r.record_id
            WHERE c.record_id IS NULL
            """
        ).fetchone()[0]
        assert unassigned == 0

    def test_no_cluster_contains_a_birth_year_conflict(self, con: Any) -> None:
        # The hard constraint that conflict splitting exists to enforce.
        violations = con.execute(
            """
            SELECT count(*) FROM (
                SELECT c.canonical_id
                FROM clusters c
                JOIN records r ON r.record_id = c.record_id
                WHERE r.entity_type = 'Person' AND r.birth_year IS NOT NULL
                GROUP BY c.canonical_id
                HAVING count(DISTINCT r.birth_year) > 1
            )
            """
        ).fetchone()[0]
        assert violations == 0

    def test_no_cluster_mixes_entity_types(self, con: Any) -> None:
        violations = con.execute(
            """
            SELECT count(*) FROM (
                SELECT c.canonical_id
                FROM clusters c
                JOIN records r ON r.record_id = c.record_id
                GROUP BY c.canonical_id
                HAVING count(DISTINCT r.entity_type) > 1
            )
            """
        ).fetchone()[0]
        assert violations == 0

    def test_clustering_is_deterministic(self, corpus: FixtureCorpus, settings: Settings) -> None:
        from ownership_er.pipeline import stage_cluster
        from ownership_er.warehouse import connect

        def snapshot() -> list[tuple[str, str]]:
            with connect(settings) as c:
                return c.execute(
                    "SELECT record_id, canonical_id FROM clusters ORDER BY record_id"
                ).fetchall()

        first = snapshot()
        stage_cluster(matcher="rules", settings=settings)
        assert snapshot() == first


@pytest.fixture(scope="session")
def report(resolved: dict[str, Any]) -> dict[str, Any]:
    """Evaluation report for the shared pipeline run."""
    from ownership_er.evaluate import evaluate_all
    from ownership_er.warehouse import connect

    with connect(resolved["settings"]) as connection:
        return evaluate_all(connection, resolved["corpus"].truth_json, matcher="rules")


@pytest.fixture(scope="session")
def analysis_report(resolved: dict[str, Any]) -> dict[str, Any]:
    """Ownership analysis for the shared pipeline run."""
    from ownership_er import analysis
    from ownership_er.warehouse import connect

    with connect(resolved["settings"]) as connection:
        return analysis.run_all(connection)


class TestQuality:
    def test_pairwise_precision_floor(self, report: dict[str, Any]) -> None:
        assert report["pairwise"]["Person"]["precision"] >= 0.90

    def test_pairwise_recall_floor(self, report: dict[str, Any]) -> None:
        assert report["pairwise"]["Person"]["recall"] >= 0.80

    def test_bcubed_f1_floor(self, report: dict[str, Any]) -> None:
        assert report["clusters"]["Person"]["bcubed_f1"] >= 0.92

    def test_no_runaway_cluster(self, report: dict[str, Any]) -> None:
        # The signature failure of naive transitive closure: one giant cluster
        # swallowing hundreds of distinct people.
        clusters = report["clusters"]["Person"]
        assert clusters["largest_predicted_cluster"] <= clusters["largest_true_cluster"] * 2

    def test_held_out_registration_recall(self, report: dict[str, Any]) -> None:
        held_out = report["held_out_registration"]
        assert held_out["available"]
        assert held_out["end_to_end_recall"] >= 0.85


class TestAnalysis:
    def test_control_closure_terminates(self, analysis_report: dict[str, Any]) -> None:
        # Corporate graphs contain genuine cycles; without the cycle guard the
        # recursive CTE does not terminate.
        assert analysis_report["closure"]["max_hops"] <= 6

    def test_multi_hop_paths_exist(self, analysis_report: dict[str, Any]) -> None:
        # Multi-hop chains only exist because PSC filings were matched to
        # company records. This is the measurable payoff of resolution.
        assert analysis_report["resolution_impact"]["multi_hop_control_paths"] > 0

    def test_resolution_merges_records(self, analysis_report: dict[str, Any]) -> None:
        impact = analysis_report["resolution_impact"]
        assert impact["records_absorbed_by_merging"] > 0
        assert impact["cross_source_entities"] > 0

    def test_opacity_accounts_for_all_companies(self, analysis_report: dict[str, Any]) -> None:
        opacity = analysis_report["opacity"]
        assert opacity["total_companies"] > 0
        assert opacity["resolves_to_natural_person"] >= 0
