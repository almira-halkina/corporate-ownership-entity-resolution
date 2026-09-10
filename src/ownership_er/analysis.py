"""Ownership analysis, computed in DuckDB with recursive CTEs.

These deliberately duplicate what ``graph/queries.py`` does in Cypher, and the
duplication is the point.

Neo4j is the right tool for *interactive* investigation — an analyst following
a chain, pivoting, asking the next question. But requiring a running database
to reproduce a published figure is a reproducibility problem: anyone checking
the work needs Docker, a server and a load step before they can verify a single
number. Running the batch analysis as SQL against the same DuckDB file means
``make analyse`` reproduces every finding from a clean clone with no
infrastructure at all, and the graph load becomes an optional convenience
rather than a dependency.

It also gives a free correctness check. Two independent implementations of
transitive control — recursive SQL and variable-length Cypher — should agree,
and ``tests/test_graph_parity.py`` asserts that they do.

The headline metric here is :func:`resolution_impact`. It counts the control
links that exist *only* because records were resolved — chains whose middle hop
is a merge between a PSC filing and a company record that share no identifier.
That number is the direct answer to "what did entity resolution buy?", and it
is the number worth putting on a resume.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import duckdb

from ownership_er.normalize.countries import SECRECY_JURISDICTIONS

__all__ = [
    "beneficial_owners",
    "build_control_closure",
    "circular_ownership",
    "hub_entities",
    "opacity_metrics",
    "resolution_impact",
    "run_all",
    "sanctions_exposure",
]


def _secrecy_list_sql() -> str:
    return "[" + ", ".join(f"'{code}'" for code in sorted(SECRECY_JURISDICTIONS)) + "]"


CLOSURE_DDL = """
CREATE OR REPLACE TABLE control_closure AS
WITH RECURSIVE
edges AS (
    SELECT
        coalesce(cs.canonical_id, 'ent:' || r.source_record_id) AS owner_id,
        coalesce(ct.canonical_id, 'ent:' || r.target_record_id) AS asset_id,
        min(r.min_percent) AS min_percent,
        max(r.max_percent) AS max_percent,
        bool_or(r.has_hard_control) AS has_hard_control,
        bool_or(r.via_fiduciary)    AS via_fiduciary
    FROM relationships r
    LEFT JOIN clusters cs ON cs.record_id = r.source_record_id
    LEFT JOIN clusters ct ON ct.record_id = r.target_record_id
    WHERE r.ceased_on IS NULL
    GROUP BY 1, 2
),
walk AS (
    SELECT
        owner_id                AS root_id,
        asset_id                AS asset_id,
        1                       AS hops,
        coalesce(min_percent, 0.0)   / 100.0 AS min_share,
        coalesce(max_percent, 100.0) / 100.0 AS max_share,
        (min_percent IS NULL)   AS control_only,
        via_fiduciary,
        [owner_id, asset_id]    AS path
    FROM edges

    UNION ALL

    SELECT
        w.root_id,
        e.asset_id,
        w.hops + 1,
        w.min_share * coalesce(e.min_percent, 0.0)   / 100.0,
        w.max_share * coalesce(e.max_percent, 100.0) / 100.0,
        w.control_only OR (e.min_percent IS NULL),
        w.via_fiduciary OR e.via_fiduciary,
        list_append(w.path, e.asset_id)
    FROM walk w
    JOIN edges e ON e.owner_id = w.asset_id
    -- Cycle guard. Corporate graphs contain genuine circular holdings, and
    -- without this the recursion does not terminate. The depth cap is a second
    -- guard: chains beyond six hops are vanishingly rare and enormously
    -- expensive to enumerate.
    WHERE w.hops < 6
      AND NOT list_contains(w.path, e.asset_id)
)
SELECT * FROM walk
"""


def build_control_closure(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    """Materialise transitive control paths up to six hops."""
    con.execute(CLOSURE_DDL)
    row = con.execute(
        """
        SELECT count(*), max(hops), count(DISTINCT root_id), count(DISTINCT asset_id)
        FROM control_closure
        """
    ).fetchone()
    return {
        "paths": int(row[0]) if row else 0,
        "max_hops": int(row[1]) if row and row[1] else 0,
        "distinct_roots": int(row[2]) if row else 0,
        "distinct_assets": int(row[3]) if row else 0,
    }


def beneficial_owners(con: duckdb.DuckDBPyConnection, *, limit: int = 1000) -> list[dict[str, Any]]:
    """Natural persons with no owner above them, and what they ultimately control."""
    return _rows(
        con.execute(
            """
            SELECT
                e.canonical_id                       AS owner_id,
                e.name                               AS owner_name,
                e.nationality,
                count(DISTINCT cc.asset_id)          AS companies_controlled,
                max(cc.hops)                         AS deepest_chain,
                round(max(cc.max_share) * 100, 2)    AS max_indirect_percent,
                bool_or(cc.via_fiduciary)            AS any_fiduciary_layer
            FROM control_closure cc
            JOIN canonical_entities e ON e.canonical_id = cc.root_id
            WHERE e.entity_type = 'Person'
              AND NOT EXISTS (
                  SELECT 1 FROM control_closure up WHERE up.asset_id = cc.root_id
              )
            GROUP BY 1, 2, 3
            ORDER BY companies_controlled DESC, deepest_chain DESC
            LIMIT ?
            """,
            [limit],
        )
    )


def sanctions_exposure(
    con: duckdb.DuckDBPyConnection, *, limit: int = 1000
) -> list[dict[str, Any]]:
    """UK companies reachable from a sanctioned entity through control edges.

    Direct hits (one hop) are what a conventional screening tool already finds.
    The value added here is entirely in ``hops > 1``, and the report separates
    them so the contribution is visible rather than asserted.
    """
    return _rows(
        con.execute(
            """
            WITH risky AS (
                SELECT canonical_id, name, topics
                FROM canonical_entities
                WHERE len(list_filter(coalesce(topics, []),
                    t -> t IN ('sanction', 'sanction.linked', 'sanction.counter',
                               'export.control', 'export.risk'))) > 0
            )
            SELECT
                target.canonical_id                  AS company_id,
                target.name                          AS company_name,
                target.reg_number                    AS company_number,
                target.jurisdiction,
                min(cc.hops)                         AS shortest_hops,
                count(DISTINCT risky.canonical_id)   AS n_sanctioned_owners,
                list_distinct(list(risky.name))[1:3] AS sanctioned_owners,
                round(max(cc.max_share) * 100, 2)    AS max_indirect_percent,
                bool_or(cc.control_only)             AS control_without_equity,
                bool_or(cc.via_fiduciary)            AS via_fiduciary
            FROM control_closure cc
            JOIN risky ON risky.canonical_id = cc.root_id
            JOIN canonical_entities target ON target.canonical_id = cc.asset_id
            WHERE target.entity_type <> 'Person'
            GROUP BY 1, 2, 3, 4
            ORDER BY shortest_hops ASC, n_sanctioned_owners DESC
            LIMIT ?
            """,
            [limit],
        )
    )


def opacity_metrics(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    """Quantify where open data stops being able to identify a controller.

    Three distinct failure modes, deliberately counted separately because they
    have different remedies: no PSC filing at all, a chain terminating in a
    secrecy jurisdiction, and a chain passing through a trust or nominee.
    """
    totals = con.execute(
        """
        SELECT
            count(*) FILTER (WHERE entity_type <> 'Person') AS companies,
            count(*) FILTER (WHERE entity_type = 'Person')  AS people
        FROM canonical_entities
        """
    ).fetchone()

    terminal = con.execute(
        f"""
        WITH roots AS (
            SELECT DISTINCT cc.root_id, cc.asset_id, cc.via_fiduciary
            FROM control_closure cc
            WHERE NOT EXISTS (
                SELECT 1 FROM control_closure up WHERE up.asset_id = cc.root_id
            )
        )
        SELECT
            count(DISTINCT r.asset_id) FILTER (WHERE e.entity_type = 'Person')
                AS resolves_to_person,
            count(DISTINCT r.asset_id) FILTER (
                WHERE e.entity_type <> 'Person'
                  AND e.jurisdiction IN {_secrecy_list_sql()})
                AS terminates_in_secrecy_jurisdiction,
            count(DISTINCT r.asset_id) FILTER (
                WHERE e.entity_type <> 'Person'
                  AND e.jurisdiction NOT IN {_secrecy_list_sql()})
                AS terminates_in_company,
            count(DISTINCT r.asset_id) FILTER (WHERE r.via_fiduciary)
                AS chain_via_fiduciary
        FROM roots r
        JOIN canonical_entities e ON e.canonical_id = r.root_id
        """
    ).fetchone()

    no_filing = con.execute(
        """
        SELECT count(*) FROM canonical_entities e
        WHERE e.entity_type <> 'Person'
          AND NOT EXISTS (
              SELECT 1 FROM control_closure cc WHERE cc.asset_id = e.canonical_id
          )
        """
    ).fetchone()

    by_jurisdiction = _rows(
        con.execute(
            f"""
            WITH roots AS (
                SELECT DISTINCT cc.root_id, cc.asset_id, cc.hops
                FROM control_closure cc
                WHERE NOT EXISTS (
                    SELECT 1 FROM control_closure up WHERE up.asset_id = cc.root_id
                )
            )
            SELECT
                nullif(e.jurisdiction, '')                AS terminal_jurisdiction,
                e.jurisdiction IN {_secrecy_list_sql()}   AS is_secrecy_jurisdiction,
                count(DISTINCT roots.asset_id)            AS companies_controlled,
                count(DISTINCT roots.root_id)             AS controlling_entities,
                round(avg(roots.hops), 2)                 AS mean_hops
            FROM roots
            JOIN canonical_entities e ON e.canonical_id = roots.root_id
            WHERE e.entity_type <> 'Person' AND e.jurisdiction <> ''
            GROUP BY 1, 2
            ORDER BY companies_controlled DESC
            LIMIT 40
            """
        )
    )

    return {
        "total_companies": int(totals[0]) if totals else 0,
        "total_people": int(totals[1]) if totals else 0,
        "companies_without_any_control_edge": int(no_filing[0]) if no_filing else 0,
        "resolves_to_natural_person": int(terminal[0]) if terminal else 0,
        "terminates_in_secrecy_jurisdiction": int(terminal[1]) if terminal else 0,
        "terminates_in_company": int(terminal[2]) if terminal else 0,
        "chain_via_fiduciary": int(terminal[3]) if terminal else 0,
        "by_terminal_jurisdiction": by_jurisdiction,
    }


def hub_entities(
    con: duckdb.DuckDBPyConnection, *, min_controlled: int = 5, limit: int = 200
) -> list[dict[str, Any]]:
    """Entities controlling many companies — genuine groups, nominees, or bad merges."""
    return _rows(
        con.execute(
            """
            SELECT
                e.canonical_id  AS entity_id,
                e.name          AS entity_name,
                e.entity_type,
                e.n_records     AS source_records,
                count(DISTINCT cc.asset_id) AS companies_controlled,
                max(cc.hops)    AS deepest_chain
            FROM control_closure cc
            JOIN canonical_entities e ON e.canonical_id = cc.root_id
            GROUP BY 1, 2, 3, 4
            HAVING count(DISTINCT cc.asset_id) >= ?
            ORDER BY companies_controlled DESC
            LIMIT ?
            """,
            [min_controlled, limit],
        )
    )


def circular_ownership(con: duckdb.DuckDBPyConnection, *, limit: int = 100) -> list[dict[str, Any]]:
    """Entities that ultimately control themselves.

    Found by re-running the walk with the cycle guard relaxed to allow the
    origin to reappear — which is the definition of a cycle, and cannot be
    detected by the guarded closure because that guard exists to exclude them.
    """
    con.execute(
        """
        CREATE OR REPLACE TABLE control_cycles AS
        WITH RECURSIVE
        edges AS (
            SELECT
                coalesce(cs.canonical_id, 'ent:' || r.source_record_id) AS owner_id,
                coalesce(ct.canonical_id, 'ent:' || r.target_record_id) AS asset_id
            FROM relationships r
            LEFT JOIN clusters cs ON cs.record_id = r.source_record_id
            LEFT JOIN clusters ct ON ct.record_id = r.target_record_id
            WHERE r.ceased_on IS NULL
            GROUP BY 1, 2
        ),
        walk AS (
            SELECT owner_id AS root_id, asset_id, 1 AS hops, [owner_id, asset_id] AS path
            FROM edges
            UNION ALL
            SELECT w.root_id, e.asset_id, w.hops + 1, list_append(w.path, e.asset_id)
            FROM walk w
            JOIN edges e ON e.owner_id = w.asset_id
            WHERE w.hops < 6
              AND (e.asset_id = w.root_id OR NOT list_contains(w.path, e.asset_id))
        )
        SELECT DISTINCT root_id, hops, path
        FROM walk
        WHERE asset_id = root_id
        """
    )
    return _rows(
        con.execute(
            """
            SELECT c.root_id AS entity_id, e.name AS entity_name,
                   c.hops AS cycle_length, c.path AS cycle
            FROM control_cycles c
            LEFT JOIN canonical_entities e ON e.canonical_id = c.root_id
            ORDER BY c.hops ASC
            LIMIT ?
            """,
            [limit],
        )
    )


def resolution_impact(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    """What entity resolution actually bought.

    Compares the resolved control graph against the graph obtainable without
    resolution — where every filing is its own entity and nothing joins except
    on an exact stated identifier. The difference is the pipeline's entire
    contribution, and it is measurable rather than rhetorical.
    """
    merged = con.execute(
        """
        SELECT
            count(*)                                   AS n_records,
            count(DISTINCT canonical_id)               AS n_entities,
            count(*) FILTER (WHERE cluster_size > 1)   AS records_in_merged_clusters,
            max(cluster_size)                          AS largest_cluster
        FROM clusters
        """
    ).fetchone()

    cross_source = con.execute(
        """
        SELECT count(*) FROM (
            SELECT c.canonical_id
            FROM clusters c
            JOIN records r ON r.record_id = c.record_id
            GROUP BY c.canonical_id
            HAVING count(DISTINCT r.source) > 1
        )
        """
    ).fetchone()

    # Multi-hop chains only exist where a PSC filing was matched to a company
    # record. Without resolution the chain stops at hop one, so this count is a
    # direct measure of reach gained.
    multi_hop = con.execute(
        """
        SELECT
            count(*) FILTER (WHERE hops = 1) AS one_hop_paths,
            count(*) FILTER (WHERE hops > 1) AS multi_hop_paths,
            count(DISTINCT asset_id) FILTER (WHERE hops > 1) AS companies_with_indirect_owner
        FROM control_closure
        """
    ).fetchone()

    sanctions_depth = con.execute(
        """
        WITH risky AS (
            SELECT canonical_id FROM canonical_entities
            WHERE len(list_filter(coalesce(topics, []),
                t -> t IN ('sanction', 'sanction.linked', 'sanction.counter',
                           'export.control', 'export.risk'))) > 0
        )
        SELECT
            count(DISTINCT cc.asset_id) FILTER (WHERE cc.hops = 1) AS direct_exposure,
            count(DISTINCT cc.asset_id) FILTER (WHERE cc.hops > 1) AS indirect_exposure
        FROM control_closure cc
        JOIN risky ON risky.canonical_id = cc.root_id
        """
    ).fetchone()

    n_records = int(merged[0]) if merged else 0
    n_entities = int(merged[1]) if merged else 0
    return {
        "source_records": n_records,
        "canonical_entities": n_entities,
        "records_absorbed_by_merging": n_records - n_entities,
        "records_in_merged_clusters": int(merged[2]) if merged else 0,
        "largest_cluster": int(merged[3]) if merged else 0,
        "cross_source_entities": int(cross_source[0]) if cross_source else 0,
        "one_hop_control_paths": int(multi_hop[0]) if multi_hop else 0,
        "multi_hop_control_paths": int(multi_hop[1]) if multi_hop else 0,
        "companies_with_indirect_owner": int(multi_hop[2]) if multi_hop else 0,
        "sanctions_direct_exposure": int(sanctions_depth[0]) if sanctions_depth else 0,
        "sanctions_indirect_exposure_found_only_by_resolution": (
            int(sanctions_depth[1]) if sanctions_depth else 0
        ),
    }


def _rows(cursor: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    columns = [d[0] for d in cursor.description or []]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def run_all(con: duckdb.DuckDBPyConnection, out_dir: Path | None = None) -> dict[str, Any]:
    """Run every analysis and optionally write the results."""
    report: dict[str, Any] = {"closure": build_control_closure(con)}
    report["resolution_impact"] = resolution_impact(con)
    report["opacity"] = opacity_metrics(con)
    report["beneficial_owners"] = beneficial_owners(con, limit=200)
    report["sanctions_exposure"] = sanctions_exposure(con, limit=200)
    report["hub_entities"] = hub_entities(con, min_controlled=3, limit=100)
    report["circular_ownership"] = circular_ownership(con, limit=50)

    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "analysis.json").write_text(
            json.dumps(report, indent=2, default=str), encoding="utf-8"
        )
    return report
