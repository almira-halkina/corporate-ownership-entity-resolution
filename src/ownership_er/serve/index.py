"""Build the read-only serving index from the batch warehouse.

Everything expensive happens here, once, so that the request path is a lookup.
The output is a single DuckDB file containing five tables:

``entities``
    One row per canonical entity, with ``is_sanctioned`` and ``is_pep``
    resolved from the topic list rather than recomputed per request.
``edges``
    Control edges re-pointed from ``record_id`` onto ``canonical_id`` and
    de-duplicated, exactly as the Neo4j loader does it — the same SQL is
    reused so the two backends cannot drift.
``name_index``
    One row per (entity, name variant). Resolution keeps the full alias set
    because the alias set *is* the product for a screening use case, so search
    has to match on any variant, not just the modal spelling.
``sanctions_exposure``
    Precomputed transitive closure from every sanctioned entity down to every
    company it controls, with hop count and the propagated ownership interval.
    This is the query the project exists to answer and the only one whose cost
    grows with graph depth rather than with the result size, so it is paid for
    at build time.
``meta``
    Build provenance and corpus counts, served on ``/health`` so a running
    instance can always say which index it is answering from.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb

from ownership_er.config import Settings, get_settings
from ownership_er.graph.loader import canonical_nodes_sql, resolved_edges_sql

__all__ = ["SERVING_DDL", "build_serving_index", "default_index_path"]


# Depth bound on the precomputed closure. Corporate ownership graphs contain
# genuine cycles, so the walk is bounded and visited entities are not revisited
# on the same path. Six hops matches the Cypher traversals in
# `ownership_er.graph.queries`, so both backends answer the same question.
MAX_HOPS = 6


SERVING_DDL = """
CREATE TABLE entities (
    canonical_id  VARCHAR PRIMARY KEY,
    name          VARCHAR,
    entity_type   VARCHAR,
    all_names     VARCHAR[],
    birth_year    INTEGER,
    nationality   VARCHAR,
    country       VARCHAR,
    jurisdiction  VARCHAR,
    reg_number    VARCHAR,
    postcode      VARCHAR,
    topics        VARCHAR[],
    sources       VARCHAR[],
    n_records     INTEGER,
    is_sanctioned BOOLEAN,
    is_pep        BOOLEAN
);

CREATE TABLE edges (
    owner_id         VARCHAR NOT NULL,
    asset_id         VARCHAR NOT NULL,
    min_percent      DOUBLE,
    max_percent      DOUBLE,
    control_kinds    VARCHAR[],
    capacities       VARCHAR[],
    has_hard_control BOOLEAN,
    via_fiduciary    BOOLEAN,
    n_filings        INTEGER,
    is_active        BOOLEAN
);

CREATE TABLE name_index (
    canonical_id VARCHAR NOT NULL,
    variant      VARCHAR NOT NULL,
    variant_norm VARCHAR NOT NULL,
    is_primary   BOOLEAN
);

CREATE TABLE sanctions_exposure (
    asset_id      VARCHAR NOT NULL,
    risk_id       VARCHAR NOT NULL,
    hops          INTEGER,
    min_percent   DOUBLE,
    max_percent   DOUBLE,
    control_only  BOOLEAN,
    via_fiduciary BOOLEAN,
    path_ids      VARCHAR[]
);

CREATE TABLE meta (
    key   VARCHAR PRIMARY KEY,
    value VARCHAR
);
"""


def default_index_path(settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    return settings.paths.data / "warehouse" / "serving.duckdb"


def _normalise_for_search(con: duckdb.DuckDBPyConnection) -> None:
    """Populate the denormalised name index.

    Normalisation is done in SQL rather than by calling
    :func:`ownership_er.normalize.names.normalize_text` per row, because the
    index has to be rebuilt on every pipeline run and a Python round trip per
    alias is the difference between a two-second build and a two-minute one.
    The transformation is deliberately the conservative subset — case fold,
    strip punctuation, collapse whitespace — since search matches against it
    with both prefix and containment, and anything more aggressive starts
    merging distinct names.
    """
    con.execute(
        """
        INSERT INTO name_index
        SELECT
            canonical_id,
            variant,
            trim(regexp_replace(regexp_replace(lower(variant), '[^a-z0-9 ]', ' ', 'g'),
                                '\\s+', ' ', 'g')) AS variant_norm,
            variant = name                          AS is_primary
        FROM (
            SELECT canonical_id, name, unnest(list_distinct(
                       list_concat(coalesce(all_names, []), [name]))) AS variant
            FROM entities
        )
        WHERE variant IS NOT NULL AND length(trim(variant)) > 0
        """
    )


def _build_sanctions_closure(con: duckdb.DuckDBPyConnection) -> int:
    """Walk downward from every sanctioned entity to everything it controls.

    Implemented as an iterative frontier expansion rather than a single
    ``WITH RECURSIVE``: the walk has to carry a visited-set per path to survive
    cycles, and a recursive CTE cannot express that without materialising the
    path array and re-scanning it, which is what this does explicitly and more
    cheaply.

    Percentage propagation multiplies the *bounds*, never midpoints, matching
    ``ownership_er.graph.queries``: a 25-50%% hop through a 50-75%% hop yields
    12.5-37.5%%. Where any hop asserts control without a percentage, the
    arithmetic is abandoned and the path is flagged ``control_only``, because
    multiplying an unknown by anything is not a number.
    """
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE frontier AS
        SELECT
            e.asset_id                                   AS asset_id,
            n.canonical_id                               AS risk_id,
            1                                            AS hops,
            coalesce(e.min_percent, 0.0) / 100.0         AS min_share,
            coalesce(e.max_percent, 100.0) / 100.0       AS max_share,
            (e.min_percent IS NULL)                      AS control_only,
            coalesce(e.via_fiduciary, false)             AS via_fiduciary,
            [n.canonical_id, e.asset_id]                 AS path_ids
        FROM entities n
        JOIN edges e ON e.owner_id = n.canonical_id
        WHERE n.is_sanctioned AND coalesce(e.is_active, true)
        """
    )
    con.execute("CREATE OR REPLACE TEMP TABLE closure AS SELECT * FROM frontier")

    for _ in range(MAX_HOPS - 1):
        con.execute(
            """
            CREATE OR REPLACE TEMP TABLE next_frontier AS
            SELECT
                e.asset_id,
                f.risk_id,
                f.hops + 1                                        AS hops,
                f.min_share * coalesce(e.min_percent, 0.0) / 100.0  AS min_share,
                f.max_share * coalesce(e.max_percent, 100.0) / 100.0 AS max_share,
                f.control_only OR (e.min_percent IS NULL)          AS control_only,
                f.via_fiduciary OR coalesce(e.via_fiduciary, false) AS via_fiduciary,
                list_append(f.path_ids, e.asset_id)                AS path_ids
            FROM frontier f
            JOIN edges e ON e.owner_id = f.asset_id
            WHERE coalesce(e.is_active, true)
              AND NOT list_contains(f.path_ids, e.asset_id)
            """
        )
        added = con.execute("SELECT count(*) FROM next_frontier").fetchone()
        if not added or added[0] == 0:
            break
        con.execute("INSERT INTO closure SELECT * FROM next_frontier")
        con.execute("CREATE OR REPLACE TEMP TABLE frontier AS SELECT * FROM next_frontier")

    # Keep the shortest path per (asset, risk) pair. An analyst wants the most
    # direct evidence of the link first; the longer routes are noise once the
    # link is established.
    con.execute(
        """
        INSERT INTO sanctions_exposure
        SELECT asset_id, risk_id, hops,
               CASE WHEN control_only THEN NULL ELSE round(min_share * 100, 4) END,
               CASE WHEN control_only THEN NULL ELSE round(max_share * 100, 4) END,
               control_only, via_fiduciary, path_ids
        FROM (
            SELECT *, row_number() OVER (
                PARTITION BY asset_id, risk_id ORDER BY hops ASC
            ) AS rn
            FROM closure
        )
        WHERE rn = 1
        """
    )
    row = con.execute("SELECT count(*) FROM sanctions_exposure").fetchone()
    return row[0] if row else 0


def build_serving_index(
    *,
    settings: Settings | None = None,
    out_path: Path | None = None,
) -> dict[str, Any]:
    """Flatten the batch warehouse into the read-only serving artifact."""
    settings = settings or get_settings()
    warehouse = settings.paths.warehouse
    if not warehouse.exists():
        raise FileNotFoundError(
            f"no warehouse at {warehouse}; run `oer run-all --fixtures` first"
        )

    out_path = out_path or default_index_path(settings)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()

    started = time.perf_counter()
    con = duckdb.connect(str(out_path))
    try:
        for stmt in SERVING_DDL.strip().split(";"):
            if stmt.strip():
                con.execute(stmt)

        # The warehouse is attached read-only and made the default search path,
        # so the SQL borrowed from the Neo4j loader resolves its bare table
        # names against the warehouse unchanged. Writes are qualified with the
        # serving catalog explicitly. Reusing that SQL rather than restating it
        # is what stops the graph and serving backends from drifting apart.
        serving_db = con.execute("SELECT current_database()").fetchone()[0]
        con.execute(f"ATTACH '{warehouse}' AS wh (READ_ONLY)")
        con.execute("SET search_path='wh'")

        con.execute(
            f"INSERT INTO {serving_db}.entities SELECT * FROM ({canonical_nodes_sql()})"
        )
        con.execute(
            f"""INSERT INTO {serving_db}.edges
                SELECT owner_id, asset_id, min_percent, max_percent, control_kinds,
                       capacities, has_hard_control, via_fiduciary, n_filings, is_active
                FROM ({resolved_edges_sql()})"""
        )
        con.execute(f"SET search_path='{serving_db}'")
        con.execute("DETACH wh")
        _normalise_for_search(con)
        n_exposure = _build_sanctions_closure(con)

        # Indexes built after load, not before: maintaining them during a bulk
        # insert costs more than one build at the end.
        for stmt in (
            "CREATE INDEX idx_name_norm ON name_index (variant_norm)",
            "CREATE INDEX idx_name_entity ON name_index (canonical_id)",
            "CREATE INDEX idx_edges_owner ON edges (owner_id)",
            "CREATE INDEX idx_edges_asset ON edges (asset_id)",
            "CREATE INDEX idx_exposure_asset ON sanctions_exposure (asset_id)",
            "CREATE INDEX idx_exposure_risk ON sanctions_exposure (risk_id)",
        ):
            con.execute(stmt)

        counts = {
            "entities": con.execute("SELECT count(*) FROM entities").fetchone()[0],
            "edges": con.execute("SELECT count(*) FROM edges").fetchone()[0],
            "name_variants": con.execute("SELECT count(*) FROM name_index").fetchone()[0],
            "sanctioned_entities": con.execute(
                "SELECT count(*) FROM entities WHERE is_sanctioned"
            ).fetchone()[0],
            "sanctions_exposure_links": n_exposure,
            "companies_with_exposure": con.execute(
                "SELECT count(DISTINCT asset_id) FROM sanctions_exposure"
            ).fetchone()[0],
        }
        elapsed = round(time.perf_counter() - started, 3)
        meta = {
            **{k: str(v) for k, v in counts.items()},
            "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "build_seconds": str(elapsed),
            "max_hops": str(MAX_HOPS),
            "source_warehouse": str(warehouse),
        }
        con.executemany(
            "INSERT INTO meta VALUES (?, ?)", list(meta.items())
        )
        con.execute("CHECKPOINT")
    finally:
        con.close()

    size_mb = round(out_path.stat().st_size / 1_048_576, 2)
    return {"path": str(out_path), "size_mb": size_mb, "build_seconds": elapsed, **counts}


if __name__ == "__main__":  # pragma: no cover
    print(json.dumps(build_serving_index(), indent=2))
