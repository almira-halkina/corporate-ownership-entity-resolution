"""Clustering: connected components, then conflict-aware splitting.

Why connected components alone are not enough
---------------------------------------------
Taking the transitive closure of accepted pairs is the standard first move, and
it is right in spirit — if A matches B and B matches C, then A, B and C are one
entity. But the operation is unforgiving: a *single* false-positive edge merges
two unrelated components permanently, and because merges chain, the damage
compounds. On a register containing thousands of people plausibly described as
"Mr J Smith, British", naive transitive closure reliably produces one enormous
cluster containing several hundred distinct individuals — a failure mode
severe enough that a system exhibiting it is worse than no system, because the
resulting "entity" is confidently wrong.

The fix
-------
Some record attributes are *mutually exclusive by construction*. One natural
person has one year of birth. One company has one registration number in one
jurisdiction. Those constraints are not similarity signals to be weighed — they
are hard facts that make a proposed cluster impossible regardless of how
strongly its edges score.

:func:`build_clusters` therefore runs two passes:

1. **Union-find over accepted edges** to get the candidate components.
2. **Conflict-aware agglomerative re-clustering** inside any component that
   contains a contradiction. Edges are re-added strongest-first, and a merge is
   refused when it would place two incompatible attribute values in one
   cluster. Because the strongest evidence is consumed first, the split follows
   the seams the evidence actually supports rather than an arbitrary cut.

This is a constrained variant of correlation clustering. The exact problem is
NP-hard; the greedy pass is the standard approximation and, on data where the
constraints are genuinely hard, it recovers the right partition in the cases
that matter. Every split is recorded so the effect is measurable rather than
assumed — ``docs/evaluation.md`` reports precision with and without it.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import duckdb

from ownership_er.warehouse import bulk_insert

__all__ = ["COMPANY_CONFLICTS", "PERSON_CONFLICTS", "ConflictRule", "UnionFind", "build_clusters"]


class UnionFind:
    """Union-find with path compression and union by size."""

    __slots__ = ("parent", "size")

    def __init__(self) -> None:
        self.parent: dict[str, str] = {}
        self.size: dict[str, int] = {}

    def add(self, item: str) -> None:
        if item not in self.parent:
            self.parent[item] = item
            self.size[item] = 1

    def find(self, item: str) -> str:
        self.add(item)
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != root:  # path compression
            self.parent[item], item = root, self.parent[item]
        return root

    def union(self, a: str, b: str) -> str:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return ra
        if self.size[ra] < self.size[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        self.size[ra] += self.size[rb]
        return ra

    def components(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = defaultdict(list)
        for item in self.parent:
            out[self.find(item)].append(item)
        return dict(out)


# ---------------------------------------------------------------------------
# Conflict rules
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ConflictRule:
    """An attribute whose values cannot legitimately differ within one entity."""

    field: str
    label: str
    rationale: str
    # Some attributes tolerate a small number of distinct values: a person may
    # hold two nationalities, a company may be known by a historical and a
    # current registration number. `max_distinct` encodes that tolerance rather
    # than forcing a binary choice between "hard constraint" and "ignored".
    max_distinct: int = 1


PERSON_CONFLICTS: tuple[ConflictRule, ...] = (
    ConflictRule(
        field="birth_year",
        label="birth_year",
        rationale=(
            "A natural person has one year of birth. Companies House validates "
            "the field at filing, so two different stated years is the single "
            "most reliable proof that a cluster spans two people."
        ),
        max_distinct=1,
    ),
    ConflictRule(
        field="nationality",
        label="nationality",
        rationale=(
            "Tolerates two values, not one: dual nationality is common and "
            "filers pick whichever passport was to hand. Three or more distinct "
            "ISO codes in one cluster is over-merging, not dual citizenship."
        ),
        max_distinct=2,
    ),
)

COMPANY_CONFLICTS: tuple[ConflictRule, ...] = (
    ConflictRule(
        field="reg_number",
        label="registration_number",
        rationale=(
            "One legal entity holds one registration number per register. Two "
            "distinct numbers means two companies, however similar the names."
        ),
        max_distinct=1,
    ),
    ConflictRule(
        field="jurisdiction",
        label="jurisdiction",
        rationale=(
            "Companies incorporated in different jurisdictions are distinct "
            "legal persons even under an identical name — which is exactly the "
            "structure used to make a group look like one entity when it is not. "
            "Tolerates two values because OpenSanctions and Companies House "
            "occasionally disagree on how to code the same registry."
        ),
        max_distinct=2,
    ),
)


@dataclass
class _ClusterState:
    """Accumulated attribute values for a cluster during agglomeration."""

    members: list[str] = field(default_factory=list)
    values: dict[str, set[Any]] = field(default_factory=lambda: defaultdict(set))

    def conflicts_with(self, other: _ClusterState, rules: tuple[ConflictRule, ...]) -> str | None:
        """Return the label of the first violated rule, or ``None`` if compatible."""
        for rule in rules:
            merged = self.values.get(rule.field, set()) | other.values.get(rule.field, set())
            if len(merged) > rule.max_distinct:
                return rule.label
        return None

    def absorb(self, other: _ClusterState) -> None:
        self.members.extend(other.members)
        for k, v in other.values.items():
            self.values[k].update(v)


def _load_attributes(
    con: duckdb.DuckDBPyConnection, entity_type: str, rules: tuple[ConflictRule, ...]
) -> dict[str, dict[str, Any]]:
    fields = sorted({r.field for r in rules})
    cols = ", ".join(fields)
    rows = con.execute(
        f"SELECT record_id, {cols} FROM records WHERE entity_type = ?", [entity_type]
    ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        record_id = row[0]
        attrs: dict[str, Any] = {}
        for name, value in zip(fields, row[1:], strict=True):
            # Empty strings are absence, not a value. Treating "" as a distinct
            # value would make every sparse record conflict with every other.
            if value is None or value == "":
                continue
            attrs[name] = value
        out[record_id] = attrs
    return out


def _split_component(
    members: list[str],
    edges: list[tuple[str, str, float]],
    attributes: dict[str, dict[str, Any]],
    rules: tuple[ConflictRule, ...],
) -> list[list[str]]:
    """Re-cluster one component, refusing merges that violate a conflict rule."""
    states: dict[str, _ClusterState] = {}
    for member in members:
        state = _ClusterState(members=[member])
        for k, v in attributes.get(member, {}).items():
            state.values[k].add(v)
        states[member] = state

    uf = UnionFind()
    for member in members:
        uf.add(member)

    # Strongest evidence first, so high-confidence merges happen before the
    # constraint budget is spent on marginal ones.
    for left, right, _score in sorted(edges, key=lambda e: -e[2]):
        rl, rr = uf.find(left), uf.find(right)
        if rl == rr:
            continue
        if states[rl].conflicts_with(states[rr], rules) is not None:
            continue
        root = uf.union(rl, rr)
        other = rr if root == rl else rl
        states[root].absorb(states[other])
        del states[other]

    return [state.members for state in states.values()]


def build_clusters(
    con: duckdb.DuckDBPyConnection,
    *,
    matcher: str = "rules",
    split_conflicts: bool = True,
    include_uncertain: bool = False,
) -> dict[str, Any]:
    """Cluster accepted pairs into canonical entities.

    Singletons are included. A record that matched nothing is still an entity —
    dropping it would silently remove most of the register from the graph, since
    the majority of companies have exactly one PSC filing and no duplicate.
    """
    decisions = ("accept", "uncertain") if include_uncertain else ("accept",)
    placeholders = ", ".join("?" for _ in decisions)

    con.execute("DELETE FROM clusters")
    con.execute("DELETE FROM canonical_entities")

    stats: dict[str, Any] = {"matcher": matcher, "entity_types": {}}
    total_conflicts: dict[str, int] = defaultdict(int)

    for entity_type, rules in (("Person", PERSON_CONFLICTS), ("Company", COMPANY_CONFLICTS)):
        edges = con.execute(
            f"""
            SELECT s.left_id, s.right_id, s.score
            FROM pair_scores s
            JOIN records l ON l.record_id = s.left_id
            WHERE s.matcher = ?
              AND s.decision IN ({placeholders})
              AND l.entity_type = ?
            """,
            [matcher, *decisions, entity_type],
        ).fetchall()

        all_records = [
            r[0]
            for r in con.execute(
                "SELECT record_id FROM records WHERE entity_type = ?", [entity_type]
            ).fetchall()
        ]
        if not all_records:
            continue

        # Pass 1 — transitive closure.
        uf = UnionFind()
        for record_id in all_records:
            uf.add(record_id)
        for left, right, _score in edges:
            uf.union(left, right)
        components = uf.components()

        attributes = _load_attributes(con, entity_type, rules)
        edges_by_component: dict[str, list[tuple[str, str, float]]] = defaultdict(list)
        for left, right, score in edges:
            edges_by_component[uf.find(left)].append((left, right, float(score)))

        # Pass 2 — conflict-aware splitting.
        final: list[list[str]] = []
        n_split = 0
        n_conflicted = 0
        for root, members in components.items():
            if not split_conflicts or len(members) < 2:
                final.append(members)
                continue

            merged_values: dict[str, set[Any]] = defaultdict(set)
            for member in members:
                for k, v in attributes.get(member, {}).items():
                    merged_values[k].add(v)
            violated = [
                r.label for r in rules if len(merged_values.get(r.field, set())) > r.max_distinct
            ]
            if not violated:
                final.append(members)
                continue

            n_conflicted += 1
            for label in violated:
                total_conflicts[label] += 1
            parts = _split_component(members, edges_by_component[root], attributes, rules)
            n_split += len(parts) - 1
            final.extend(parts)

        # Persist. The canonical id is derived from the smallest member id so it
        # is deterministic across runs and traceable back to a real filing —
        # a random UUID would be stable only until the next run, which makes
        # stored decisions unattributable.
        col_record: list[str] = []
        col_canonical: list[str] = []
        col_size: list[int] = []
        col_round: list[int] = []
        for members in final:
            canonical = f"ent:{min(members)}"
            original = components.get(uf.find(members[0]), members)
            round_marker = 1 if len(members) < len(original) else 0
            for member in members:
                col_record.append(member)
                col_canonical.append(canonical)
                col_size.append(len(members))
                col_round.append(round_marker)
        bulk_insert(
            con,
            "clusters",
            ["record_id", "canonical_id", "cluster_size", "split_round", "matcher"],
            {
                "record_id": col_record,
                "canonical_id": col_canonical,
                "cluster_size": col_size,
                "split_round": col_round,
                "matcher": [matcher] * len(col_record),
            },
        )

        sizes = [len(m) for m in final]
        stats["entity_types"][entity_type] = {
            "records": len(all_records),
            "accepted_edges": len(edges),
            "components_before_split": len(components),
            "clusters_after_split": len(final),
            "components_with_conflicts": n_conflicted,
            "extra_clusters_from_splitting": n_split,
            "largest_cluster": max(sizes) if sizes else 0,
            "singletons": sum(1 for s in sizes if s == 1),
            "multi_record_clusters": sum(1 for s in sizes if s > 1),
        }

    _materialise_canonical_entities(con)
    stats["conflicts_by_rule"] = dict(total_conflicts)
    stats["canonical_entities"] = int(
        (con.execute("SELECT count(*) FROM canonical_entities").fetchone() or [0])[0]
    )
    return stats


def _materialise_canonical_entities(con: duckdb.DuckDBPyConnection) -> None:
    """Collapse each cluster into one canonical row.

    Attribute selection is by frequency, then by source precedence: Companies
    House over OpenSanctions for registry facts, because the registry is the
    authority on its own filings. The full name list is retained rather than
    discarded — the alias set *is* the intelligence product for a screening
    use case, and reducing an entity to its most common spelling throws away
    exactly the variants an analyst needs to search on.
    """
    con.execute(
        """
        INSERT INTO canonical_entities
        SELECT
            c.canonical_id,
            any_value(r.entity_type)                              AS entity_type,
            mode(r.name)                                          AS name,
            list_distinct(list(r.name))                           AS all_names,
            mode(r.birth_year) FILTER (WHERE r.birth_year IS NOT NULL) AS birth_year,
            mode(r.nationality) FILTER (WHERE r.nationality <> '')     AS nationality,
            mode(r.country)     FILTER (WHERE r.country <> '')         AS country,
            mode(r.jurisdiction) FILTER (WHERE r.jurisdiction <> '')   AS jurisdiction,
            mode(r.reg_number)  FILTER (WHERE r.reg_number <> '')      AS reg_number,
            mode(r.postcode)    FILTER (WHERE r.postcode <> '')        AS postcode,
            list_distinct(flatten(list(r.topics)))                AS topics,
            list_distinct(list(r.source))                         AS sources,
            list(r.record_id)                                     AS record_ids,
            count(*)                                              AS n_records
        FROM clusters c
        JOIN records r ON r.record_id = c.record_id
        GROUP BY c.canonical_id
        """
    )
