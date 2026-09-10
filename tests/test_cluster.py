"""Unit tests for union-find and conflict-aware splitting.

Constructed cases rather than pipeline output, because the behaviour under test
is precisely what the aggregate metrics hide: a single bad edge chaining two
correct clusters into one wrong one.
"""

from __future__ import annotations

from ownership_er.cluster import (
    PERSON_CONFLICTS,
    ConflictRule,
    UnionFind,
    _ClusterState,
    _split_component,
)


class TestUnionFind:
    def test_transitive_merge(self) -> None:
        uf = UnionFind()
        uf.union("a", "b")
        uf.union("b", "c")
        assert uf.find("a") == uf.find("c")

    def test_disjoint_sets_stay_disjoint(self) -> None:
        uf = UnionFind()
        uf.union("a", "b")
        uf.union("c", "d")
        assert uf.find("a") != uf.find("c")

    def test_components_partition_all_items(self) -> None:
        uf = UnionFind()
        for item in "abcdef":
            uf.add(item)
        uf.union("a", "b")
        uf.union("c", "d")
        components = uf.components()
        assert sum(len(v) for v in components.values()) == 6
        assert len(components) == 4  # {a,b}, {c,d}, {e}, {f}

    def test_repeated_union_is_idempotent(self) -> None:
        uf = UnionFind()
        uf.union("a", "b")
        root = uf.find("a")
        uf.union("a", "b")
        assert uf.find("a") == root


class TestConflictSplitting:
    def test_incompatible_birth_years_are_separated(self) -> None:
        # The canonical failure: A-B and B-C both score well, so transitive
        # closure merges all three, but A and C state different birth years.
        # Splitting must break the chain at its weakest link.
        members = ["a", "b", "c"]
        edges = [("a", "b", 0.97), ("b", "c", 0.94)]
        attributes = {
            "a": {"birth_year": 1970},
            "b": {},  # no birth year: compatible with both
            "c": {"birth_year": 1985},
        }
        parts = _split_component(members, edges, attributes, PERSON_CONFLICTS)
        assert len(parts) == 2
        grouped = {frozenset(p) for p in parts}
        # The strongest edge survives; the weaker one is cut.
        assert frozenset({"a", "b"}) in grouped
        assert frozenset({"c"}) in grouped

    def test_compatible_cluster_is_left_intact(self) -> None:
        members = ["a", "b", "c"]
        edges = [("a", "b", 0.97), ("b", "c", 0.95)]
        attributes = {
            "a": {"birth_year": 1970},
            "b": {"birth_year": 1970},
            "c": {"birth_year": 1970},
        }
        parts = _split_component(members, edges, attributes, PERSON_CONFLICTS)
        assert len(parts) == 1
        assert set(parts[0]) == {"a", "b", "c"}

    def test_strongest_edges_are_consumed_first(self) -> None:
        # Given a forced choice, the split should follow the evidence: the
        # 0.99 edge survives and the 0.70 edge is cut, not the reverse.
        members = ["a", "b", "c"]
        edges = [("a", "b", 0.70), ("b", "c", 0.99)]
        attributes = {
            "a": {"birth_year": 1970},
            "b": {},
            "c": {"birth_year": 1985},
        }
        parts = _split_component(members, edges, attributes, PERSON_CONFLICTS)
        grouped = {frozenset(p) for p in parts}
        assert frozenset({"b", "c"}) in grouped
        assert frozenset({"a"}) in grouped

    def test_dual_nationality_is_tolerated(self) -> None:
        # max_distinct=2 for nationality: dual citizenship is common and must
        # not split a cluster, but three distinct codes is over-merging.
        rules = (
            ConflictRule(field="nationality", label="nationality", rationale="", max_distinct=2),
        )
        attributes = {
            "a": {"nationality": "GB"},
            "b": {"nationality": "IE"},
        }
        parts = _split_component(["a", "b"], [("a", "b", 0.95)], attributes, rules)
        assert len(parts) == 1

    def test_three_nationalities_split(self) -> None:
        rules = (
            ConflictRule(field="nationality", label="nationality", rationale="", max_distinct=2),
        )
        attributes = {
            "a": {"nationality": "GB"},
            "b": {"nationality": "IE"},
            "c": {"nationality": "RU"},
        }
        parts = _split_component(
            ["a", "b", "c"], [("a", "b", 0.95), ("b", "c", 0.93)], attributes, rules
        )
        assert len(parts) == 2

    def test_empty_attributes_never_conflict(self) -> None:
        # Absence is not a value. If empty strings counted as distinct values,
        # every sparse record would conflict with every other sparse record.
        state_a = _ClusterState(members=["a"])
        state_b = _ClusterState(members=["b"])
        assert state_a.conflicts_with(state_b, PERSON_CONFLICTS) is None
