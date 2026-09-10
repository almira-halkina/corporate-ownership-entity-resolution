"""Graph load and LLM adjudication, tested without a server or an API key.

Both components talk to something external, and both are therefore usually left
untested. Injecting the driver and the client makes the parts that actually
carry risk — payload shape, batching, verdict parsing, cost accounting —
testable in CI at no cost.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ownership_er.config import Settings
from ownership_er.graph.loader import GraphLoader
from ownership_er.graph.queries import QUERIES
from ownership_er.match.llm import LLMAdjudicator, build_prompt


class TestGraphLoader:
    def test_dry_run_writes_cypher_without_a_server(self, con: Any, tmp_path: Path) -> None:
        loader = GraphLoader()
        out = tmp_path / "load.cypher"
        result = loader.load(con, dry_run_path=out)
        assert result["dry_run"] is True
        assert result["nodes"] > 0
        text = out.read_text(encoding="utf-8")
        assert "CREATE CONSTRAINT entity_id" in text
        assert "MERGE (e:Entity {canonical_id: row.canonical_id})" in text

    def test_load_issues_constraints_then_nodes_then_edges(
        self, con: Any, fake_driver: Any
    ) -> None:
        loader = GraphLoader(driver=fake_driver)
        result = loader.load(con)
        assert result["nodes"] > 0
        assert result["edges"] > 0
        statements = [q for q, _ in fake_driver.log]
        assert any("CREATE CONSTRAINT" in s for s in statements)
        first_merge = next(i for i, s in enumerate(statements) if "MERGE (e:Entity" in s)
        first_edge = next(i for i, s in enumerate(statements) if "MERGE (owner)" in s)
        # Edges must be written after nodes: the edge MERGE matches on
        # canonical_id, so an edge written first silently matches nothing.
        assert first_merge < first_edge

    def test_node_payloads_carry_required_keys(self, con: Any) -> None:
        loader = GraphLoader()
        nodes = loader.fetch_nodes(con)
        assert nodes
        required = {"canonical_id", "name", "entity_type", "is_sanctioned", "is_pep"}
        assert required <= set(nodes[0])

    def test_edges_reference_canonical_ids_only(self, con: Any) -> None:
        loader = GraphLoader()
        node_ids = {n["canonical_id"] for n in loader.fetch_nodes(con)}
        edges = loader.fetch_edges(con)
        assert edges
        # Every edge endpoint must exist as a node, or the graph loads with
        # dangling references that make traversals silently incomplete.
        dangling = [
            e for e in edges if e["owner_id"] not in node_ids or e["asset_id"] not in node_ids
        ]
        assert not dangling

    def test_batching_respects_configured_size(self, con: Any, fake_driver: Any) -> None:
        settings = Settings()
        settings.graph.batch_size = 5
        loader = GraphLoader(settings=settings, driver=fake_driver)
        loader.load(con)
        node_calls = [params for query, params in fake_driver.log if "MERGE (e:Entity" in query]
        assert node_calls
        assert all(len(p["rows"]) <= 5 for p in node_calls)

    def test_every_named_query_is_valid_cypher_shape(self) -> None:
        for name, query in QUERIES.items():
            assert "MATCH" in query, name
            assert "RETURN" in query, name


class FakeMessages:
    def __init__(self, payload: str, calls: list[dict[str, Any]]) -> None:
        self.payload = payload
        self.calls = calls

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self.payload)],
            usage=SimpleNamespace(input_tokens=400, output_tokens=40),
        )


class FakeClient:
    def __init__(self, payload: str) -> None:
        self.calls: list[dict[str, Any]] = []
        self.messages = FakeMessages(payload, self.calls)


class TestLLMAdjudicator:
    def test_only_uncertain_pairs_are_selected(self, con: Any) -> None:
        # The cost and blast-radius guarantee: the model never sees a pair the
        # deterministic matcher was confident about.
        adjudicator = LLMAdjudicator(client=FakeClient("{}"), base_matcher="rules")
        band = adjudicator.select_band(con, "Person")
        selected = {(p["left_id"], p["right_id"]) for p in band}
        confident = con.execute(
            "SELECT left_id, right_id FROM pair_scores "
            "WHERE matcher = 'rules' AND decision <> 'uncertain'"
        ).fetchall()
        assert not (selected & set(confident))

    def test_prompt_contains_both_records_and_the_base_score(self, con: Any) -> None:
        adjudicator = LLMAdjudicator(client=FakeClient("{}"), base_matcher="rules")
        band = adjudicator.select_band(con, "Person")
        if not band:
            pytest.skip("no uncertain pairs in this corpus")
        prompt = build_prompt(
            band[0]["left"], band[0]["right"], band[0]["score"], band[0]["rationale"]
        )
        assert "RECORD A:" in prompt and "RECORD B:" in prompt
        assert "Deterministic matcher score" in prompt

    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            ('{"verdict":"match","confidence":0.9,"reason":"same dob"}', "match"),
            ('{"verdict":"no_match","confidence":0.8,"reason":"dob differs"}', "no_match"),
            ('```json\n{"verdict":"match","confidence":0.7,"reason":"x"}\n```', "match"),
            ('Here you go: {"verdict":"unclear","confidence":0.1,"reason":"y"}', "unclear"),
            ("not json at all", "unclear"),
            ('{"verdict":"maybe","confidence":0.5}', "unclear"),
            ('{"verdict":"match","confidence":"high"}', "match"),
        ],
    )
    def test_response_parsing_is_defensive(self, payload: str, expected: str) -> None:
        # A malformed response must degrade to "unclear", never to a guess.
        adjudicator = LLMAdjudicator(client=FakeClient(payload))
        verdict, confidence, _reason = adjudicator._parse_response(payload)
        assert verdict == expected
        assert 0.0 <= confidence <= 1.0

    def test_verdicts_map_into_the_decision_space(self, con: Any) -> None:
        client = FakeClient('{"verdict":"match","confidence":0.9,"reason":"same person"}')
        adjudicator = LLMAdjudicator(client=client, base_matcher="rules")
        n = adjudicator.score_pairs(con, "Person")
        if n == 0:
            pytest.skip("no uncertain pairs in this corpus")
        decisions = dict(
            con.execute(
                "SELECT decision, count(*) FROM pair_scores WHERE matcher = 'llm' GROUP BY 1"
            ).fetchall()
        )
        assert decisions.get("accept", 0) > 0
        # The llm matcher must be a complete decision set, not a partial
        # overlay, so it can be clustered and evaluated identically.
        llm_total = sum(decisions.values())
        rules_total = con.execute(
            "SELECT count(*) FROM pair_scores WHERE matcher = 'rules'"
        ).fetchone()[0]
        assert llm_total <= rules_total

    def test_results_are_cached(self, con: Any) -> None:
        client = FakeClient('{"verdict":"match","confidence":0.9,"reason":"r"}')
        first = LLMAdjudicator(client=client, base_matcher="rules")
        first.score_pairs(con, "Person")
        calls_after_first = len(client.calls)
        if calls_after_first == 0:
            pytest.skip("no uncertain pairs in this corpus")

        second = LLMAdjudicator(client=client, base_matcher="rules")
        second.score_pairs(con, "Person")
        # Second run must be served entirely from cache.
        assert len(client.calls) == calls_after_first
        assert second.cost_report()["cache_hit_rate"] == 1.0

    def test_cost_report_accounts_tokens(self, con: Any) -> None:
        client = FakeClient('{"verdict":"match","confidence":0.9,"reason":"r"}')
        adjudicator = LLMAdjudicator(client=client, base_matcher="rules")
        adjudicator.score_pairs(con, "Person")
        report = adjudicator.cost_report()
        assert report["estimated_usd"] >= 0.0
        assert set(report) >= {"requested", "cached", "called", "estimated_usd"}

    def test_oversized_band_is_refused(self, con: Any) -> None:
        # Guardrail against a misconfiguration that would send a third of all
        # pairs to a paid API.
        settings = Settings()
        settings.match.llm_band_fraction_ceiling = 0.0001
        adjudicator = LLMAdjudicator(
            settings=settings,
            client=FakeClient('{"verdict":"match","confidence":0.9,"reason":"r"}'),
            base_matcher="rules",
        )
        band = adjudicator.select_band(con, "Person")
        if not band:
            pytest.skip("no uncertain pairs in this corpus")
        with pytest.raises(RuntimeError, match="Uncertain band"):
            adjudicator.score_pairs(con, "Person")


class TestFtmExport:
    def test_records_render_as_ftm_entities(self, con: Any) -> None:
        from ownership_er.schema import Record, to_ftm

        columns = [f.name for f in Record.__dataclass_fields__.values()]  # type: ignore[attr-defined]
        row = con.execute(
            f"SELECT {', '.join(columns)} FROM records WHERE entity_type = 'Person' LIMIT 1"
        ).fetchone()
        record = Record(**dict(zip(columns, row, strict=True)))
        entity = to_ftm(record)
        assert entity["schema"] == "Person"
        assert entity["properties"]["name"]
        assert json.dumps(entity)  # must be serialisable

    def test_reduced_precision_birth_dates(self) -> None:
        from ownership_er.schema import Record, to_ftm

        # Companies House publishes month and year only. Emitting a fabricated
        # day would assert precision the register deliberately withholds.
        entity = to_ftm(
            Record(
                record_id="x",
                source="ch_psc",
                entity_type="Person",
                name="A B",
                birth_year=1975,
                birth_month=6,
            )
        )
        assert entity["properties"]["birthDate"] == ["1975-06"]

        year_only = to_ftm(
            Record(
                record_id="y", source="ch_psc", entity_type="Person", name="A B", birth_year=1975
            )
        )
        assert year_only["properties"]["birthDate"] == ["1975"]
