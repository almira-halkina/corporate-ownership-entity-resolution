"""Evaluation: pairwise metrics, B-cubed cluster metrics, sweeps and ablations.

Ground truth, and where it comes from
-------------------------------------
Entity resolution has no natural labels, and how a project obtains them
determines whether its reported numbers mean anything. Three independent
sources are used here, deliberately, because each is blind to different errors:

**Synthetic corruption** (``fixtures.py``). Entities are corrupted into multiple
records by a known process, so the true partition is known exactly. This is the
only source that measures *recall* honestly — real-world labelling can never
tell you about the true pairs blocking silently dropped, because you never saw
them. Its weakness is that the corruption model is a hypothesis about how the
register is messy, so good scores here prove the matcher handles the modelled
variation and nothing more.

**Held-out registration numbers** (real data, no labelling effort). Corporate
PSC filings frequently state the parent's UK company number. That number is a
near-deterministic link to the company product. Removing it from the features
and asking the matcher to recover the link from name and address alone yields
tens of thousands of genuine labelled pairs on real data. This is the strongest
evidence in the project: real distribution, real noise, no annotator, and no
opportunity to fool oneself.

**Hand-labelled sample.** A stratified sample across the score range, labelled
manually. Small, and the only source that catches errors the other two share.

Metrics
-------
Pairwise precision and recall are reported because they are directly
interpretable. B-cubed is reported alongside because pairwise metrics are
misleading for clustering: merging two clusters of size 50 costs 2,500 pairwise
errors from one bad edge, so pairwise scores are dominated by whatever happened
to the largest clusters. B-cubed weights every *record* equally instead, which
is what an analyst experiences — they look up one entity at a time and care
whether that entity is right.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb

from ownership_er.warehouse import bulk_insert

__all__ = [
    "ClusterMetrics",
    "PairMetrics",
    "bcubed_metrics",
    "error_taxonomy",
    "evaluate_all",
    "held_out_registration_eval",
    "load_truth_pairs",
    "pairwise_metrics",
    "threshold_sweep",
]


@dataclass(slots=True)
class PairMetrics:
    """Pairwise decision quality against a truth set."""

    true_positives: int
    false_positives: int
    false_negatives: int
    candidate_pairs: int
    truth_pairs: int
    truth_pairs_in_candidates: int

    @property
    def precision(self) -> float:
        denom = self.true_positives + self.false_positives
        return self.true_positives / denom if denom else 0.0

    @property
    def recall(self) -> float:
        return self.true_positives / self.truth_pairs if self.truth_pairs else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    @property
    def blocking_recall(self) -> float:
        """Share of true pairs that survived blocking — the ceiling on recall."""
        return self.truth_pairs_in_candidates / self.truth_pairs if self.truth_pairs else 0.0

    @property
    def recall_within_candidates(self) -> float:
        """Recall measured only over pairs blocking actually offered.

        Reported next to end-to-end recall so a shortfall is correctly
        attributed. If this number is high but end-to-end recall is low, the
        matcher is fine and blocking is the problem — a distinction that
        determines what to fix next, and one a single recall figure hides.
        """
        return (
            self.true_positives / self.truth_pairs_in_candidates
            if self.truth_pairs_in_candidates
            else 0.0
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
            "blocking_recall": round(self.blocking_recall, 4),
            "recall_within_candidates": round(self.recall_within_candidates, 4),
            "true_positives": self.true_positives,
            "false_positives": self.false_positives,
            "false_negatives": self.false_negatives,
            "candidate_pairs": self.candidate_pairs,
            "truth_pairs": self.truth_pairs,
            "truth_pairs_in_candidates": self.truth_pairs_in_candidates,
        }


@dataclass(slots=True)
class ClusterMetrics:
    """B-cubed metrics, plus cluster-shape diagnostics."""

    bcubed_precision: float
    bcubed_recall: float
    n_records: int
    n_predicted_clusters: int
    n_true_clusters: int
    largest_predicted: int
    largest_true: int
    over_merged_records: int = 0
    under_merged_records: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def bcubed_f1(self) -> float:
        p, r = self.bcubed_precision, self.bcubed_recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "bcubed_precision": round(self.bcubed_precision, 4),
            "bcubed_recall": round(self.bcubed_recall, 4),
            "bcubed_f1": round(self.bcubed_f1, 4),
            "n_records": self.n_records,
            "n_predicted_clusters": self.n_predicted_clusters,
            "n_true_clusters": self.n_true_clusters,
            "largest_predicted_cluster": self.largest_predicted,
            "largest_true_cluster": self.largest_true,
            "over_merged_records": self.over_merged_records,
            "under_merged_records": self.under_merged_records,
            **self.extra,
        }


# ---------------------------------------------------------------------------
# Truth loading
# ---------------------------------------------------------------------------


def load_truth_pairs(
    con: duckdb.DuckDBPyConnection,
    truth_path: Path,
    *,
    table: str = "truth_pairs",
) -> dict[str, int]:
    """Materialise ground-truth equivalence classes and their induced pairs.

    A truth class must contain *every* record referring to one real entity,
    across all sources. Building it from within-register duplicates alone is a
    subtle but serious mistake: the matcher would correctly link an
    OpenSanctions record to that person's PSC filings, find no such pair in the
    truth set, and be scored as a false positive. Precision would then fall
    exactly in proportion to how well the cross-source linking worked — the
    metric would punish the capability the pipeline exists to provide.

    So classes are assembled from two inputs and then expanded:

    * within-register duplicate filings for the same person;
    * cross-source links from OpenSanctions entities to those same people.

    The expansion matters because one OpenSanctions entity yields several
    records — one per name variant — whose ids are only known after parsing.
    They are recovered by stripping the ``#n`` suffix and joining back to
    ``records``, so the truth set stays correct however many aliases an upstream
    entity happens to carry.

    Pairs are ordered ``left_id < right_id`` to match ``candidate_pairs``, so
    the two join directly without normalising at query time.
    """
    truth = json.loads(truth_path.read_text(encoding="utf-8"))
    clusters: dict[str, list[str]] = truth.get("person_clusters", {})
    sanctions_links: dict[str, str] = truth.get("sanctions_links", {})
    corporate_links: dict[str, str] = truth.get("corporate_links", {})

    con.execute("DROP TABLE IF EXISTS truth_clusters")
    con.execute("CREATE TABLE truth_clusters (record_id VARCHAR, truth_id VARCHAR)")

    member_rows: list[tuple[str, str]] = []
    for truth_id, members in clusters.items():
        member_rows.extend((m, truth_id) for m in sorted(set(members)))
    if member_rows:
        bulk_insert(
            con,
            "truth_clusters",
            ["record_id", "truth_id"],
            {
                "record_id": [r[0] for r in member_rows],
                "truth_id": [r[1] for r in member_rows],
            },
        )

    # Cross-source links. Values naming a person class extend that class;
    # values naming a company record are company linkage and are handled by
    # `held_out_registration_eval` instead.
    con.execute("DROP TABLE IF EXISTS truth_sanctions_links")
    con.execute("CREATE TABLE truth_sanctions_links (os_record_id VARCHAR, truth_id VARCHAR)")
    if sanctions_links:
        bulk_insert(
            con,
            "truth_sanctions_links",
            ["os_record_id", "truth_id"],
            {
                "os_record_id": list(sanctions_links.keys()),
                "truth_id": list(sanctions_links.values()),
            },
        )

    # Expand each linked OpenSanctions entity to all of its name-variant records.
    expanded = con.execute(
        """
        INSERT INTO truth_clusters
        SELECT r.record_id, l.truth_id
        FROM records r
        JOIN truth_sanctions_links l
          ON regexp_replace(r.record_id, '#[0-9]+$', '') = l.os_record_id
        WHERE r.entity_type = 'Person'
          AND l.truth_id LIKE 'TRUE-P-%'
        RETURNING record_id
        """
    ).fetchall()

    con.execute("DROP TABLE IF EXISTS truth_corporate_links")
    con.execute(
        "CREATE TABLE truth_corporate_links (psc_record_id VARCHAR, company_record_id VARCHAR)"
    )
    if corporate_links:
        bulk_insert(
            con,
            "truth_corporate_links",
            ["psc_record_id", "company_record_id"],
            {
                "psc_record_id": list(corporate_links.keys()),
                "company_record_id": list(corporate_links.values()),
            },
        )

    # Induce pairs from the completed classes. Restricted to records that were
    # actually parsed, so a truth entry for a record the parser dropped shows up
    # as a missing record rather than an unreachable false negative.
    con.execute(f"DROP TABLE IF EXISTS {table}")
    con.execute(
        f"""
        CREATE TABLE {table} AS
        SELECT
            least(a.record_id, b.record_id)    AS left_id,
            greatest(a.record_id, b.record_id) AS right_id,
            a.truth_id                         AS truth_id
        FROM truth_clusters a
        JOIN truth_clusters b
          ON a.truth_id = b.truth_id AND a.record_id < b.record_id
        WHERE a.record_id IN (SELECT record_id FROM records)
          AND b.record_id IN (SELECT record_id FROM records)
        """
    )

    n_members = int((con.execute("SELECT count(*) FROM truth_clusters").fetchone() or [0])[0])
    n_pairs = int((con.execute(f"SELECT count(*) FROM {table}").fetchone() or [0])[0])
    return {
        "truth_clusters": len(clusters),
        "truth_members": n_members,
        "truth_members_from_sanctions": len(expanded),
        "truth_pairs": n_pairs,
        "sanctions_links": len(sanctions_links),
        "corporate_links": len(corporate_links),
    }


# ---------------------------------------------------------------------------
# Pairwise metrics
# ---------------------------------------------------------------------------


def pairwise_metrics(
    con: duckdb.DuckDBPyConnection,
    *,
    matcher: str = "rules",
    entity_type: str = "Person",
    accept_decisions: tuple[str, ...] = ("accept",),
    truth_table: str = "truth_pairs",
) -> PairMetrics:
    """Precision, recall and F1 of accepted pairs against the truth set."""
    placeholders = ", ".join("?" for _ in accept_decisions)

    truth_total = int((con.execute(f"SELECT count(*) FROM {truth_table}").fetchone() or [0])[0])
    in_candidates = int(
        (
            con.execute(
                f"""
                SELECT count(*) FROM {truth_table} t
                JOIN candidate_pairs c
                  ON c.left_id = t.left_id AND c.right_id = t.right_id
                """
            ).fetchone()
            or [0]
        )[0]
    )
    n_candidates = int(
        (
            con.execute(
                """
                SELECT count(*) FROM candidate_pairs c
                JOIN records l ON l.record_id = c.left_id
                WHERE l.entity_type = ?
                """,
                [entity_type],
            ).fetchone()
            or [0]
        )[0]
    )
    accepted = con.execute(
        f"""
        SELECT
            count(*) AS n_accepted,
            count(*) FILTER (WHERE t.left_id IS NOT NULL) AS n_true
        FROM pair_scores s
        JOIN records l ON l.record_id = s.left_id
        LEFT JOIN {truth_table} t
          ON t.left_id = s.left_id AND t.right_id = s.right_id
        WHERE s.matcher = ?
          AND s.decision IN ({placeholders})
          AND l.entity_type = ?
        """,
        [matcher, *accept_decisions, entity_type],
    ).fetchone()

    n_accepted = int(accepted[0]) if accepted else 0
    tp = int(accepted[1]) if accepted else 0
    return PairMetrics(
        true_positives=tp,
        false_positives=n_accepted - tp,
        false_negatives=truth_total - tp,
        candidate_pairs=n_candidates,
        truth_pairs=truth_total,
        truth_pairs_in_candidates=in_candidates,
    )


# ---------------------------------------------------------------------------
# B-cubed
# ---------------------------------------------------------------------------


def bcubed_metrics(
    con: duckdb.DuckDBPyConnection,
    *,
    entity_type: str = "Person",
) -> ClusterMetrics:
    """B-cubed precision and recall over records that have a truth label.

    For each record, precision is the share of its predicted cluster that
    shares its true label, and recall is the share of its true cluster that
    landed in its predicted cluster. Averaging over records rather than pairs
    is what stops one over-merged giant from dominating the score.
    """
    rows = con.execute(
        """
        SELECT c.record_id, c.canonical_id, t.truth_id
        FROM clusters c
        JOIN truth_clusters t ON t.record_id = c.record_id
        JOIN records r ON r.record_id = c.record_id
        WHERE r.entity_type = ?
        """,
        [entity_type],
    ).fetchall()

    if not rows:
        return ClusterMetrics(0.0, 0.0, 0, 0, 0, 0, 0)

    predicted: dict[str, list[str]] = defaultdict(list)
    truth: dict[str, list[str]] = defaultdict(list)
    pred_of: dict[str, str] = {}
    truth_of: dict[str, str] = {}
    for record_id, canonical_id, truth_id in rows:
        predicted[canonical_id].append(record_id)
        truth[truth_id].append(record_id)
        pred_of[record_id] = canonical_id
        truth_of[record_id] = truth_id

    precision_sum = 0.0
    recall_sum = 0.0
    over_merged = 0
    under_merged = 0
    for record_id in pred_of:
        pred_members = predicted[pred_of[record_id]]
        true_members = truth[truth_of[record_id]]
        pred_set = set(pred_members)
        true_set = set(true_members)
        correct = len(pred_set & true_set)
        p = correct / len(pred_set)
        r = correct / len(true_set)
        precision_sum += p
        recall_sum += r
        if p < 1.0:
            over_merged += 1
        if r < 1.0:
            under_merged += 1

    n = len(pred_of)
    return ClusterMetrics(
        bcubed_precision=precision_sum / n,
        bcubed_recall=recall_sum / n,
        n_records=n,
        n_predicted_clusters=len(predicted),
        n_true_clusters=len(truth),
        largest_predicted=max(len(v) for v in predicted.values()),
        largest_true=max(len(v) for v in truth.values()),
        over_merged_records=over_merged,
        under_merged_records=under_merged,
    )


# ---------------------------------------------------------------------------
# Sweeps and ablations
# ---------------------------------------------------------------------------


def threshold_sweep(
    con: duckdb.DuckDBPyConnection,
    *,
    matcher: str = "rules",
    entity_type: str = "Person",
    truth_table: str = "truth_pairs",
    steps: int = 41,
) -> list[dict[str, Any]]:
    """Precision/recall/F1 across the full score range.

    Produces the curve that justifies the operating thresholds, rather than
    presenting a single chosen point as though it fell out of the data. In a
    screening context the right operating point is not the F1 maximum — a
    missed sanctions link costs more than a false one an analyst discards — so
    the curve is what the threshold decision should be argued from.
    """
    truth_total = int((con.execute(f"SELECT count(*) FROM {truth_table}").fetchone() or [0])[0])
    if not truth_total:
        return []

    rows = con.execute(
        f"""
        SELECT s.score, (t.left_id IS NOT NULL) AS is_true
        FROM pair_scores s
        JOIN records l ON l.record_id = s.left_id
        LEFT JOIN {truth_table} t
          ON t.left_id = s.left_id AND t.right_id = s.right_id
        WHERE s.matcher = ? AND l.entity_type = ?
        """,
        [matcher, entity_type],
    ).fetchall()

    out: list[dict[str, Any]] = []
    for i in range(steps):
        threshold = i / (steps - 1)
        tp = sum(1 for score, is_true in rows if score >= threshold and is_true)
        fp = sum(1 for score, is_true in rows if score >= threshold and not is_true)
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / truth_total
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
        out.append(
            {
                "threshold": round(threshold, 3),
                "precision": round(precision, 4),
                "recall": round(recall, 4),
                "f1": round(f1, 4),
                "accepted": tp + fp,
            }
        )
    return out


def error_taxonomy(
    con: duckdb.DuckDBPyConnection,
    truth_path: Path,
    *,
    matcher: str = "rules",
    limit: int = 25,
) -> dict[str, Any]:
    """Attribute false negatives to the corruptions that caused them.

    The single most useful evaluation artefact, because it converts an
    aggregate score into a work list. Knowing recall is 0.94 says nothing about
    what to do; knowing that 60% of the misses involve a transliterated surname
    with no birth year says exactly which blocking key to add.
    """
    truth = json.loads(truth_path.read_text(encoding="utf-8"))
    corruption_by_record: dict[str, list[str]] = {
        entry["record_id"]: entry["corruptions"] for entry in truth.get("corruption_log", [])
    }

    missed = con.execute(
        """
        SELECT t.left_id, t.right_id,
               (c.left_id IS NOT NULL) AS in_candidates,
               s.score, s.decision, s.rationale
        FROM truth_pairs t
        LEFT JOIN candidate_pairs c
          ON c.left_id = t.left_id AND c.right_id = t.right_id
        LEFT JOIN pair_scores s
          ON s.left_id = t.left_id AND s.right_id = t.right_id AND s.matcher = ?
        WHERE s.decision IS DISTINCT FROM 'accept'
        """,
        [matcher],
    ).fetchall()

    by_corruption: dict[str, int] = defaultdict(int)
    by_stage: dict[str, int] = defaultdict(int)
    examples: list[dict[str, Any]] = []

    for left, right, in_candidates, score, decision, rationale in missed:
        stage = "blocking" if not in_candidates else f"matching:{decision or 'unscored'}"
        by_stage[stage] += 1
        corruptions = set(corruption_by_record.get(left, [])) | set(
            corruption_by_record.get(right, [])
        )
        for corruption in corruptions or {"none_recorded"}:
            by_corruption[corruption] += 1
        if len(examples) < limit:
            examples.append(
                {
                    "left_id": left,
                    "right_id": right,
                    "lost_at": stage,
                    "score": round(float(score), 4) if score is not None else None,
                    "corruptions": sorted(corruptions),
                    "rationale": rationale,
                }
            )

    false_positives = con.execute(
        """
        SELECT s.left_id, s.right_id, s.score, s.rationale
        FROM pair_scores s
        JOIN records l ON l.record_id = s.left_id
        LEFT JOIN truth_pairs t
          ON t.left_id = s.left_id AND t.right_id = s.right_id
        WHERE s.matcher = ? AND s.decision = 'accept'
          AND t.left_id IS NULL AND l.entity_type = 'Person'
        ORDER BY s.score DESC
        LIMIT ?
        """,
        [matcher, limit],
    ).fetchall()

    return {
        "false_negatives": len(missed),
        "lost_by_stage": dict(by_stage),
        "false_negatives_by_corruption": dict(sorted(by_corruption.items(), key=lambda kv: -kv[1])),
        "false_negative_examples": examples,
        "false_positive_examples": [
            {
                "left_id": left,
                "right_id": right,
                "score": round(float(score), 4),
                "rationale": rationale,
            }
            for left, right, score, rationale in false_positives
        ],
    }


def held_out_registration_eval(
    con: duckdb.DuckDBPyConnection, *, matcher: str = "rules"
) -> dict[str, Any]:
    """Evaluate company linkage on real data using stated registration numbers.

    Corporate PSC filings that state a UK company number provide labelled links
    at no annotation cost. The matcher is scored on whether it recovers those
    links *without* the registration-number feature — otherwise the evaluation
    would only confirm that equal numbers are equal.

    This is the headline real-data number, because it is the only one measured
    on the genuine distribution with labels nobody chose.
    """
    truth_exists = con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name = 'truth_corporate_links'"
    ).fetchone()
    if not truth_exists or not int(truth_exists[0]):
        return {"available": False, "reason": "truth_corporate_links not materialised"}

    total = int((con.execute("SELECT count(*) FROM truth_corporate_links").fetchone() or [0])[0])
    if not total:
        return {"available": False, "reason": "no corporate links with stated numbers"}

    recovered = con.execute(
        """
        SELECT count(*) FROM truth_corporate_links t
        JOIN clusters a ON a.record_id = t.psc_record_id
        JOIN clusters b ON b.record_id = t.company_record_id
        WHERE a.canonical_id = b.canonical_id
        """,
    ).fetchone()

    in_candidates = con.execute(
        """
        SELECT count(*) FROM truth_corporate_links t
        JOIN candidate_pairs c
          ON (c.left_id = t.psc_record_id AND c.right_id = t.company_record_id)
          OR (c.right_id = t.psc_record_id AND c.left_id = t.company_record_id)
        """
    ).fetchone()

    n_recovered = int(recovered[0]) if recovered else 0
    n_candidates = int(in_candidates[0]) if in_candidates else 0
    return {
        "available": True,
        "matcher": matcher,
        "labelled_links": total,
        "links_surviving_blocking": n_candidates,
        "links_recovered": n_recovered,
        "blocking_recall": round(n_candidates / total, 4),
        "end_to_end_recall": round(n_recovered / total, 4),
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def evaluate_all(
    con: duckdb.DuckDBPyConnection,
    truth_path: Path,
    *,
    matcher: str = "rules",
    out_dir: Path | None = None,
) -> dict[str, Any]:
    """Run every evaluation and optionally write the report."""
    counts = load_truth_pairs(con, truth_path)

    report: dict[str, Any] = {
        "matcher": matcher,
        "truth": counts,
        "pairwise": {},
        "clusters": {},
    }
    for entity_type in ("Person",):
        report["pairwise"][entity_type] = pairwise_metrics(
            con, matcher=matcher, entity_type=entity_type
        ).as_dict()
        report["clusters"][entity_type] = bcubed_metrics(con, entity_type=entity_type).as_dict()

    report["threshold_sweep"] = threshold_sweep(con, matcher=matcher)
    report["errors"] = error_taxonomy(con, truth_path, matcher=matcher)
    report["held_out_registration"] = held_out_registration_eval(con, matcher=matcher)

    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"evaluation_{matcher}.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )
    return report
