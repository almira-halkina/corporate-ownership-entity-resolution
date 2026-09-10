"""Shared fixtures.

The corpus is generated once per session into a temporary directory and the
pipeline is run against a temporary DuckDB file, so tests never touch the
committed fixtures or a developer's warehouse.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from ownership_er.config import Settings
from ownership_er.fixtures import FixtureCorpus, generate_corpus


@pytest.fixture(scope="session")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> FixtureCorpus:
    """A small synthetic corpus with known ground truth."""
    out = tmp_path_factory.mktemp("corpus")
    return generate_corpus(out, n_companies=300, n_people=120, n_sanctioned=25, seed=1234)


@pytest.fixture(scope="session")
def settings(tmp_path_factory: pytest.TempPathFactory) -> Settings:
    """Settings pointed at a temporary warehouse."""
    root = tmp_path_factory.mktemp("warehouse")
    s = Settings()
    s.paths.data = root / "data"
    s.paths.outputs = root / "outputs"
    s.runtime.duckdb_memory_limit = "1GB"
    s.runtime.duckdb_threads = 2
    s.paths.ensure()
    return s


@pytest.fixture(scope="session")
def resolved(corpus: FixtureCorpus, settings: Settings) -> dict[str, Any]:
    """Run the pipeline once; every downstream test reads the same warehouse."""
    from ownership_er.pipeline import stage_block, stage_cluster, stage_match, stage_normalize

    normalize = stage_normalize(
        company_path=corpus.companies_csv,
        psc_path=corpus.psc_jsonl,
        opensanctions_path=corpus.opensanctions_jsonl,
        settings=settings,
    )
    block = stage_block(settings=settings)
    match = stage_match(matcher="rules", settings=settings)
    cluster = stage_cluster(matcher="rules", settings=settings)
    return {
        "normalize": normalize,
        "block": block,
        "match": match,
        "cluster": cluster,
        "corpus": corpus,
        "settings": settings,
    }


@pytest.fixture()
def con(resolved: dict[str, Any]) -> Iterator[Any]:
    """A connection to the warehouse *after* the pipeline has run.

    Depends on ``resolved`` rather than ``settings`` so the tables exist.
    Depending only on settings yields an empty database and every downstream
    test fails with a confusing catalog error.
    """
    from ownership_er.warehouse import connect

    with connect(resolved["settings"]) as connection:
        yield connection


class FakeSession:
    """Records Cypher statements instead of executing them."""

    def __init__(self, log: list[tuple[str, dict[str, Any]]]) -> None:
        self.log = log

    def run(self, query: str, **params: Any) -> None:
        self.log.append((query.strip(), params))

    def __enter__(self) -> FakeSession:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class FakeDriver:
    """A Neo4j driver stand-in, so the load path is testable without a server."""

    def __init__(self) -> None:
        self.log: list[tuple[str, dict[str, Any]]] = []

    def session(self, **_kwargs: Any) -> FakeSession:
        return FakeSession(self.log)

    def close(self) -> None:
        return None


@pytest.fixture()
def fake_driver() -> FakeDriver:
    return FakeDriver()


@pytest.fixture()
def fixtures_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "fixtures"
