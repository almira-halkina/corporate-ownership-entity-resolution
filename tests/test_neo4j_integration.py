"""Integration tests against a live Neo4j.

Skipped unless a server is reachable, so the default suite stays offline. CI
runs these against a Neo4j service container; locally, `make neo4j-up` first.

These cover what a fake driver cannot: that the Cypher is actually valid, that
the constraints apply, and that the traversals return what the equivalent
DuckDB analyses return.
"""

from __future__ import annotations

from typing import Any

import pytest

from ownership_er.config import Settings
from ownership_er.graph.loader import GraphLoader
from ownership_er.graph.queries import (
    CIRCULAR_OWNERSHIP,
    CROSS_BORDER_CHAINS,
    HUB_ENTITIES,
    OPAQUE_STRUCTURES,
)
from ownership_er.normalize.countries import SECRECY_JURISDICTIONS

pytestmark = pytest.mark.needs_neo4j


@pytest.fixture(scope="module")
def driver() -> Any:
    neo4j = pytest.importorskip("neo4j")
    settings = Settings()
    try:
        drv = neo4j.GraphDatabase.driver(
            settings.graph.uri, auth=(settings.graph.user, settings.graph.password)
        )
        drv.verify_connectivity()
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"no Neo4j at {settings.graph.uri}: {exc}")
    yield drv
    drv.close()


@pytest.fixture(scope="module")
def loaded(driver: Any, resolved: dict[str, Any]) -> dict[str, Any]:
    from ownership_er.warehouse import connect

    loader = GraphLoader(settings=resolved["settings"], driver=driver)
    with connect(resolved["settings"]) as con:
        return loader.load(con, reset=True)


class TestGraphLoad:
    def test_node_and_edge_counts_match_the_warehouse(
        self, driver: Any, loaded: dict[str, Any]
    ) -> None:
        with driver.session() as session:
            nodes = session.run("MATCH (e:Entity) RETURN count(e) AS n").single()["n"]
            edges = session.run("MATCH ()-[c:CONTROLS]->() RETURN count(c) AS n").single()["n"]
        assert nodes == loaded["nodes"]
        assert edges == loaded["edges"]

    def test_labels_are_applied(self, driver: Any, loaded: dict[str, Any]) -> None:
        with driver.session() as session:
            people = session.run("MATCH (p:Person) RETURN count(p) AS n").single()["n"]
            companies = session.run("MATCH (c:Company) RETURN count(c) AS n").single()["n"]
        assert people == loaded["people"]
        assert companies == loaded["companies"]

    def test_uniqueness_constraint_exists(self, driver: Any, loaded: dict[str, Any]) -> None:
        with driver.session() as session:
            names = [r["name"] for r in session.run("SHOW CONSTRAINTS")]
        assert "entity_id" in names

    def test_load_is_idempotent(
        self, driver: Any, loaded: dict[str, Any], resolved: dict[str, Any]
    ) -> None:
        from ownership_er.warehouse import connect

        loader = GraphLoader(settings=resolved["settings"], driver=driver)
        with connect(resolved["settings"]) as con:
            loader.load(con, reset=False)
        with driver.session() as session:
            nodes = session.run("MATCH (e:Entity) RETURN count(e) AS n").single()["n"]
        # MERGE throughout, so a second load must not duplicate anything.
        assert nodes == loaded["nodes"]


class TestTraversals:
    def test_hub_entities_query_runs(self, driver: Any, loaded: dict[str, Any]) -> None:
        with driver.session() as session:
            rows = list(session.run(HUB_ENTITIES, min_controlled=2, limit=10))
        assert all(r["controlled"] >= 2 for r in rows)

    def test_circular_ownership_query_runs(self, driver: Any, loaded: dict[str, Any]) -> None:
        with driver.session() as session:
            rows = list(session.run(CIRCULAR_OWNERSHIP, limit=10))
        # May legitimately be empty; the query must execute and terminate.
        assert isinstance(rows, list)

    def test_opaque_structures_query_runs(self, driver: Any, loaded: dict[str, Any]) -> None:
        with driver.session() as session:
            rows = list(
                session.run(
                    OPAQUE_STRUCTURES,
                    secrecy_jurisdictions=sorted(SECRECY_JURISDICTIONS),
                    limit=10,
                )
            )
        assert isinstance(rows, list)

    def test_cross_border_chains_query_runs(self, driver: Any, loaded: dict[str, Any]) -> None:
        with driver.session() as session:
            rows = list(session.run(CROSS_BORDER_CHAINS))
        assert all(r["terminal_jurisdiction"] != "GB" for r in rows)


class TestParityWithSql:
    def test_cypher_and_sql_agree_on_multi_hop_reach(
        self, driver: Any, loaded: dict[str, Any], resolved: dict[str, Any]
    ) -> None:
        """Two independent implementations of transitive control must agree.

        The whole reason the analyses exist twice — recursive SQL for
        reproducibility, Cypher for interactive use — is that either could be
        wrong on its own. This is the check that catches it.
        """
        from ownership_er import analysis
        from ownership_er.warehouse import connect

        with connect(resolved["settings"]) as con:
            analysis.build_control_closure(con)
            sql_reach = {
                row[0]
                for row in con.execute(
                    "SELECT DISTINCT asset_id FROM control_closure WHERE hops > 1"
                ).fetchall()
            }

        with driver.session() as session:
            cypher_reach = {
                r["id"]
                for r in session.run(
                    """
                    MATCH (a:Entity)-[:CONTROLS*2..6]->(b:Entity)
                    RETURN DISTINCT b.canonical_id AS id
                    """
                )
            }

        assert sql_reach == cypher_reach
