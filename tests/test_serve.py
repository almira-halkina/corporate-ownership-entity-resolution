"""Tests for the serving index and the API.

These run against a real index built from the fixture corpus, not a mock: the
point of the serving layer is that it answers from a flattened artifact, and a
mocked connection would test nothing about whether the flattening is correct.
"""

from __future__ import annotations

import duckdb
import pytest
from fastapi.testclient import TestClient

from ownership_er.serve.api import create_app
from ownership_er.serve.index import build_serving_index


@pytest.fixture(scope="module")
def index_path(tmp_path_factory: pytest.TempPathFactory):
    out = tmp_path_factory.mktemp("serving") / "serving.duckdb"
    stats = build_serving_index(out_path=out)
    assert stats["entities"] > 0, "fixture warehouse missing; run the pipeline first"
    return out


@pytest.fixture(scope="module")
def client(index_path):
    with TestClient(create_app(index_path)) as c:
        yield c


# ------------------------------------------------------------------- index


def test_index_has_every_table(index_path) -> None:
    con = duckdb.connect(str(index_path), read_only=True)
    tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
    assert {"entities", "edges", "name_index", "sanctions_exposure", "meta"} <= tables


def test_every_entity_is_searchable(index_path) -> None:
    """The name index must cover every entity, or search silently loses rows."""
    con = duckdb.connect(str(index_path), read_only=True)
    missing = con.execute(
        "SELECT count(*) FROM entities e WHERE NOT EXISTS "
        "(SELECT 1 FROM name_index n WHERE n.canonical_id = e.canonical_id)"
    ).fetchone()[0]
    assert missing == 0


def test_closure_starts_only_at_sanctioned_entities(index_path) -> None:
    con = duckdb.connect(str(index_path), read_only=True)
    bad = con.execute(
        "SELECT count(*) FROM sanctions_exposure se "
        "JOIN entities e ON e.canonical_id = se.risk_id WHERE NOT e.is_sanctioned"
    ).fetchone()[0]
    assert bad == 0


def test_closure_paths_are_acyclic_and_bounded(index_path) -> None:
    con = duckdb.connect(str(index_path), read_only=True)
    rows = con.execute("SELECT hops, path_ids FROM sanctions_exposure").fetchall()
    assert rows
    for hops, path in rows:
        assert 1 <= hops <= 6
        assert len(path) == hops + 1, "path length must equal hops + 1"
        assert len(set(path)) == len(path), "a path must not revisit an entity"


def test_intervals_are_ordered_or_control_only(index_path) -> None:
    """min <= max, and both null exactly when the chain is control-only.

    This is the honesty property of the propagation: a chain containing an
    unquantified hop must not report a number.
    """
    con = duckdb.connect(str(index_path), read_only=True)
    for lo, hi, control_only in con.execute(
        "SELECT min_percent, max_percent, control_only FROM sanctions_exposure"
    ).fetchall():
        if control_only:
            assert lo is None and hi is None
        else:
            assert lo is not None and hi is not None and lo <= hi + 1e-9


# --------------------------------------------------------------------- api


def test_health_reports_index_metadata(client) -> None:
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert int(body["index"]["entities"]) > 0


def test_metrics_is_prometheus_text(client) -> None:
    text = client.get("/metrics").text
    assert "oer_requests_total" in text
    assert 'oer_request_latency_ms{quantile="p99"}' in text


def test_search_ranks_exact_above_substring(client) -> None:
    hit = client.get("/api/search", params={"q": "ltd"}).json()
    assert hit["count"] > 0
    order = {"exact": 0, "prefix": 1, "contains": 2}
    tiers = [r["match"]["tier"] for r in hit["results"]]
    assert tiers == sorted(tiers, key=lambda t: order[t])


def test_search_matches_an_alias_not_only_the_primary_name(client) -> None:
    """Resolution keeps every spelling; search has to use them."""
    target = None
    for r in client.get("/api/search", params={"q": "dr", "limit": 100}).json()["results"]:
        extra = [a for a in r["aliases"] if a != r["name"]]
        if extra:
            target = (r["id"], extra[0])
            break
    if target is None:
        pytest.skip("fixture corpus produced no multi-alias entity")
    entity_id, alias = target
    found = client.get("/api/search", params={"q": alias, "limit": 50}).json()
    assert entity_id in {r["id"] for r in found["results"]}


def test_unknown_entity_is_404(client) -> None:
    assert client.get("/api/entity/ent:does:not:exist").status_code == 404


def test_ownership_walk_is_bounded_by_max_hops(client) -> None:
    entity_id = client.get("/api/sanctions/exposed", params={"limit": 1}).json()["results"][0]["id"]
    for bound in (1, 2, 6):
        body = client.get(
            f"/api/entity/{entity_id}/ownership", params={"direction": "up", "max_hops": bound}
        ).json()
        assert all(n["hops"] <= bound for n in body["nodes"])


def test_ownership_directions_are_inverse(client) -> None:
    """If A is upstream of B, B must be downstream of A."""
    child = client.get("/api/sanctions/exposed", params={"limit": 1}).json()["results"][0]["id"]
    up = client.get(f"/api/entity/{child}/ownership", params={"direction": "up"}).json()
    assert up["count"] > 0
    parent = up["nodes"][0]["id"]
    down = client.get(f"/api/entity/{parent}/ownership", params={"direction": "down"}).json()
    assert child in {n["id"] for n in down["nodes"]}


def test_indirect_exposure_exists_and_is_flagged(client) -> None:
    """The finding the project exists to surface must be reachable.

    `indirect_only` means no sanctioned party appears on the company's own
    filing, yet one controls it through the chain.
    """
    indirect = client.get(
        "/api/sanctions/exposed", params={"min_hops": 2, "limit": 5}
    ).json()["results"]
    assert indirect, "fixture corpus should contain indirectly-exposed companies"
    body = client.get(f"/api/entity/{indirect[0]['id']}/sanctions").json()
    assert body["has_exposure"] and body["indirect_only"]
    assert body["entity"]["is_sanctioned"] is False
    assert all(h["hops"] >= 2 for h in body["hits"])
    assert all(len(h["path"]) == h["hops"] + 1 for h in body["hits"])


def test_sanctions_endpoint_agrees_with_the_ownership_walk(client) -> None:
    """The precomputed closure must match a live traversal.

    The closure exists only as a latency optimisation, so a disagreement
    between the two is a bug in the optimisation.
    """
    exposed = client.get("/api/sanctions/exposed", params={"limit": 8}).json()["results"]
    assert exposed
    for row in exposed:
        entity_id = row["id"]
        walked = {
            n["id"]
            for n in client.get(
                f"/api/entity/{entity_id}/ownership", params={"direction": "up"}
            ).json()["nodes"]
            if n["is_sanctioned"]
        }
        precomputed = {
            h["owner"]["id"]
            for h in client.get(f"/api/entity/{entity_id}/sanctions").json()["hits"]
        }
        assert walked == precomputed, f"closure and traversal disagree for {entity_id}"
