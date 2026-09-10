"""Loading resolved entities and control edges into Neo4j.

Resolution has to happen before the graph is built, not after. Loading raw
filings and deduplicating in Cypher would leave the same beneficial owner as
eleven separate nodes, and every ownership traversal would stop at the first
hop — the graph would be structurally unable to answer the question it exists
to answer. So edges are re-pointed from ``record_id`` to ``canonical_id``
first, and only then written.

Re-pointing merges parallel edges. Once eleven filings collapse to one person,
several may assert control over the same company. Those are combined into a
single edge holding the widest percentage band and the union of control kinds,
with ``n_filings`` retained so an analyst can see how much evidence sits behind
it.

The loader takes an injected driver, which is what makes the write path
testable against a fake without a live server, and ``--dry-run`` emits the
Cypher to a file for inspection before anything is written to a real database.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import duckdb

from ownership_er.config import GraphConfig, Settings, get_settings

__all__ = ["CONSTRAINTS", "GraphLoader", "canonical_nodes_sql", "resolved_edges_sql"]


# Uniqueness constraints double as indexes in Neo4j, and without them a MERGE
# on 5M nodes degrades to a full scan per statement — the difference between a
# load that finishes in minutes and one that does not finish.
CONSTRAINTS: tuple[str, ...] = (
    "CREATE CONSTRAINT entity_id IF NOT EXISTS FOR (e:Entity) REQUIRE e.canonical_id IS UNIQUE",
    "CREATE INDEX entity_name IF NOT EXISTS FOR (e:Entity) ON (e.name)",
    "CREATE INDEX entity_type IF NOT EXISTS FOR (e:Entity) ON (e.entity_type)",
    "CREATE INDEX entity_jurisdiction IF NOT EXISTS FOR (e:Entity) ON (e.jurisdiction)",
    "CREATE INDEX entity_reg IF NOT EXISTS FOR (e:Entity) ON (e.reg_number)",
    "CREATE INDEX company_number IF NOT EXISTS FOR (c:Company) ON (c.reg_number)",
)

MERGE_NODE = """
UNWIND $rows AS row
MERGE (e:Entity {canonical_id: row.canonical_id})
SET e.name          = row.name,
    e.entity_type   = row.entity_type,
    e.all_names     = row.all_names,
    e.birth_year    = row.birth_year,
    e.nationality   = row.nationality,
    e.country       = row.country,
    e.jurisdiction  = row.jurisdiction,
    e.reg_number    = row.reg_number,
    e.postcode      = row.postcode,
    e.topics        = row.topics,
    e.sources       = row.sources,
    e.n_records     = row.n_records,
    e.is_sanctioned = row.is_sanctioned,
    e.is_pep        = row.is_pep,
    e.risk_topics   = row.topics
"""

# Labels are applied in a second pass because Neo4j cannot parameterise a label
# in a MERGE. Two passes over 5M nodes is still far cheaper than 5M individual
# statements.
LABEL_PERSON = """
UNWIND $ids AS id
MATCH (e:Entity {canonical_id: id}) SET e:Person
"""

LABEL_COMPANY = """
UNWIND $ids AS id
MATCH (e:Entity {canonical_id: id}) SET e:Company
"""

MERGE_EDGE = """
UNWIND $rows AS row
MATCH (owner:Entity {canonical_id: row.owner_id})
MATCH (asset:Entity {canonical_id: row.asset_id})
MERGE (owner)-[c:CONTROLS]->(asset)
SET c.min_percent      = row.min_percent,
    c.max_percent      = row.max_percent,
    c.control_kinds    = row.control_kinds,
    c.capacities       = row.capacities,
    c.has_hard_control = row.has_hard_control,
    c.via_fiduciary    = row.via_fiduciary,
    c.n_filings        = row.n_filings,
    c.first_notified   = row.first_notified,
    c.last_ceased      = row.last_ceased,
    c.is_active        = row.is_active
"""


def canonical_nodes_sql() -> str:
    """Canonical entities with risk flags folded in."""
    return """
    SELECT
        canonical_id,
        coalesce(name, '')                          AS name,
        coalesce(entity_type, 'LegalEntity')        AS entity_type,
        coalesce(all_names, [])                     AS all_names,
        birth_year,
        coalesce(nationality, '')                   AS nationality,
        coalesce(country, '')                       AS country,
        coalesce(jurisdiction, '')                  AS jurisdiction,
        coalesce(reg_number, '')                    AS reg_number,
        coalesce(postcode, '')                      AS postcode,
        coalesce(topics, [])                        AS topics,
        coalesce(sources, [])                       AS sources,
        n_records,
        list_any_value(list_filter(coalesce(topics, []),
            t -> t IN ('sanction', 'sanction.linked', 'sanction.counter',
                       'export.control', 'export.risk')) ) IS NOT NULL
                                                    AS is_sanctioned,
        list_any_value(list_filter(coalesce(topics, []),
            t -> t IN ('role.pep', 'role.rca', 'role.oligarch'))) IS NOT NULL
                                                    AS is_pep
    FROM canonical_entities
    """


def resolved_edges_sql() -> str:
    """Control edges re-pointed onto canonical entities and de-duplicated.

    ``target_record_id`` is left-joined: a PSC filing can name a company that
    is absent from the company snapshot — dissolved before the snapshot date,
    or registered abroad. Dropping those edges would silently delete exactly
    the cross-border links the analysis is looking for, so the raw identifier is
    kept as the node key and the entity simply carries less detail.
    """
    return """
    SELECT
        coalesce(cs.canonical_id, 'ent:' || r.source_record_id) AS owner_id,
        coalesce(ct.canonical_id, 'ent:' || r.target_record_id) AS asset_id,
        min(r.min_percent)                          AS min_percent,
        max(r.max_percent)                          AS max_percent,
        list_distinct(flatten(list(r.control_kinds))) AS control_kinds,
        list_distinct(flatten(list(r.capacities)))    AS capacities,
        bool_or(r.has_hard_control)                 AS has_hard_control,
        bool_or(r.via_fiduciary)                    AS via_fiduciary,
        count(*)                                    AS n_filings,
        min(r.notified_on)                          AS first_notified,
        max(r.ceased_on)                            AS last_ceased,
        bool_or(r.ceased_on IS NULL)                AS is_active
    FROM relationships r
    LEFT JOIN clusters cs ON cs.record_id = r.source_record_id
    LEFT JOIN clusters ct ON ct.record_id = r.target_record_id
    GROUP BY 1, 2
    """


def _chunks(rows: list[dict[str, Any]], size: int) -> Iterator[list[dict[str, Any]]]:
    for i in range(0, len(rows), size):
        yield rows[i : i + size]


def _to_dicts(cursor: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    columns = [d[0] for d in cursor.description or []]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


class GraphLoader:
    """Writes canonical entities and resolved control edges to Neo4j."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        config: GraphConfig | None = None,
        driver: Any | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.config = config or self.settings.graph
        self._driver = driver

    @property
    def driver(self) -> Any:
        if self._driver is None:  # pragma: no cover - needs a live server
            try:
                from neo4j import GraphDatabase
            except ImportError as exc:
                raise ImportError(
                    "Graph loading needs the optional extra:\n    pip install -e '.[graph]'"
                ) from exc
            self._driver = GraphDatabase.driver(
                self.config.uri, auth=(self.config.user, self.config.password)
            )
        return self._driver

    # -- extraction ---------------------------------------------------------

    def fetch_nodes(self, con: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
        return _to_dicts(con.execute(canonical_nodes_sql()))

    def fetch_edges(self, con: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
        rows = _to_dicts(con.execute(resolved_edges_sql()))
        for row in rows:
            for key in ("first_notified", "last_ceased"):
                if row.get(key) is not None:
                    row[key] = str(row[key])
        return rows

    # -- loading ------------------------------------------------------------

    def load(
        self,
        con: duckdb.DuckDBPyConnection,
        *,
        dry_run_path: Path | None = None,
        reset: bool = False,
    ) -> dict[str, Any]:
        """Load the resolved graph. With ``dry_run_path``, write payloads instead."""
        nodes = self.fetch_nodes(con)
        edges = self.fetch_edges(con)

        if dry_run_path is not None:
            dry_run_path.parent.mkdir(parents=True, exist_ok=True)
            with dry_run_path.open("w", encoding="utf-8") as fh:
                fh.write("// Constraints\n")
                for stmt in CONSTRAINTS:
                    fh.write(stmt + ";\n")
                fh.write("\n// Nodes (payload preview)\n")
                fh.write(MERGE_NODE.strip() + "\n")
                fh.write(f"// $rows: {len(nodes):,} entities, first 3:\n")
                for node in nodes[:3]:
                    fh.write("//   " + json.dumps(node, default=str) + "\n")
                fh.write("\n// Edges (payload preview)\n")
                fh.write(MERGE_EDGE.strip() + "\n")
                fh.write(f"// $rows: {len(edges):,} edges, first 3:\n")
                for edge in edges[:3]:
                    fh.write("//   " + json.dumps(edge, default=str) + "\n")
            return {
                "dry_run": True,
                "nodes": len(nodes),
                "edges": len(edges),
                "path": str(dry_run_path),
            }

        batch = self.config.batch_size
        with self.driver.session(database=self.config.database) as session:
            if reset:
                session.run("MATCH (n:Entity) DETACH DELETE n")
            for stmt in CONSTRAINTS:
                session.run(stmt)

            for chunk in _chunks(nodes, batch):
                session.run(MERGE_NODE, rows=chunk)

            people = [n["canonical_id"] for n in nodes if n["entity_type"] == "Person"]
            companies = [n["canonical_id"] for n in nodes if n["entity_type"] != "Person"]
            for chunk in _chunks([{"id": i} for i in people], batch):
                session.run(LABEL_PERSON, ids=[c["id"] for c in chunk])
            for chunk in _chunks([{"id": i} for i in companies], batch):
                session.run(LABEL_COMPANY, ids=[c["id"] for c in chunk])

            for chunk in _chunks(edges, batch):
                session.run(MERGE_EDGE, rows=chunk)

        return {
            "dry_run": False,
            "nodes": len(nodes),
            "edges": len(edges),
            "people": len(people),
            "companies": len(companies),
        }

    def close(self) -> None:
        if self._driver is not None:
            self._driver.close()
