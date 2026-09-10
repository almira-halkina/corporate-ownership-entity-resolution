"""Configuration.

Settings resolve in this order: explicit argument -> environment variable
(``OER_`` prefix) -> ``.env`` file -> default. The pipeline never reads secrets
from a config file that could be committed.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _repo_root() -> Path:
    """Locate the repository root by walking up from this file."""
    return Path(__file__).resolve().parents[2]


class Paths(BaseSettings):
    """Filesystem layout. All paths are absolute after validation."""

    model_config = SettingsConfigDict(env_prefix="OER_PATH_", env_file=".env", extra="ignore")

    root: Path = Field(default_factory=_repo_root)
    data: Path = Field(default=Path("data"))
    fixtures: Path = Field(default=Path("fixtures"))
    outputs: Path = Field(default=Path("outputs"))

    @property
    def raw(self) -> Path:
        return self.data / "raw"

    @property
    def interim(self) -> Path:
        return self.data / "interim"

    @property
    def processed(self) -> Path:
        return self.data / "processed"

    @property
    def warehouse(self) -> Path:
        """DuckDB database file backing every stage."""
        return self.data / "warehouse" / "ownership.duckdb"

    @property
    def eval_dir(self) -> Path:
        return self.outputs / "eval"

    @property
    def analysis_dir(self) -> Path:
        return self.outputs / "analysis"

    @property
    def figures_dir(self) -> Path:
        return self.outputs / "figures"

    @property
    def llm_cache(self) -> Path:
        return self.data / "interim" / "llm_cache.duckdb"

    @field_validator("data", "fixtures", "outputs")
    @classmethod
    def _absolutise(cls, v: Path) -> Path:
        return v if v.is_absolute() else (_repo_root() / v)

    def ensure(self) -> None:
        """Create every directory the pipeline writes to."""
        for p in (
            self.raw,
            self.interim,
            self.processed,
            self.warehouse.parent,
            self.eval_dir,
            self.analysis_dir,
            self.figures_dir,
        ):
            p.mkdir(parents=True, exist_ok=True)


class SourceConfig(BaseSettings):
    """Upstream data sources.

    Companies House publishes a full company snapshot as multi-part CSV and a
    people-with-significant-control snapshot as newline-delimited JSON, both
    refreshed daily and free of charge. OpenSanctions publishes its consolidated
    default dataset as FollowTheMoney entities in newline-delimited JSON.
    """

    model_config = SettingsConfigDict(env_prefix="OER_SOURCE_", env_file=".env", extra="ignore")

    companies_base: str = "https://download.companieshouse.gov.uk"
    psc_snapshot_template: str = "https://download.companieshouse.gov.uk/persons-with-significant-control-snapshot-{date}.zip"
    company_snapshot_template: str = (
        "https://download.companieshouse.gov.uk/BasicCompanyDataAsOneFile-{date}.zip"
    )
    opensanctions_url: str = (
        "https://data.opensanctions.org/datasets/latest/default/entities.ftm.json"
    )
    # Companies House publishes on the 1st of each month for the company product;
    # PSC is daily. Left unset means "resolve the most recent available".
    snapshot_date: str | None = None
    request_timeout: int = 120
    max_retries: int = 4


class MatchConfig(BaseSettings):
    """Thresholds governing the match -> cluster hand-off.

    Scores in ``[0, 1]``. Pairs at or above ``auto_accept`` become graph edges
    without review; pairs below ``auto_reject`` are discarded. The band between
    the two is the *uncertain band* — the only region routed to the LLM
    adjudicator, which is what keeps its cost bounded.
    """

    model_config = SettingsConfigDict(env_prefix="OER_MATCH_", env_file=".env", extra="ignore")

    auto_accept: float = 0.92
    auto_reject: float = 0.62
    llm_enabled: bool = False
    llm_model: str = "claude-sonnet-5"
    llm_max_pairs: int = 5_000
    llm_concurrency: int = 8
    llm_temperature: float = 0.0
    # Guardrail: refuse to run if the uncertain band is implausibly large,
    # which almost always means blocking or thresholds are misconfigured.
    llm_band_fraction_ceiling: float = 0.35

    @field_validator("auto_reject")
    @classmethod
    def _ordered(cls, v: float, info: object) -> float:
        return v


class GraphConfig(BaseSettings):
    """Neo4j connection and load behaviour."""

    model_config = SettingsConfigDict(env_prefix="OER_NEO4J_", env_file=".env", extra="ignore")

    uri: str = "bolt://localhost:7687"
    user: str = "neo4j"
    password: str = "ownership-dev-password"
    database: str = "neo4j"
    batch_size: int = 10_000


class RuntimeConfig(BaseSettings):
    """Execution knobs that trade memory against speed."""

    model_config = SettingsConfigDict(env_prefix="OER_RUNTIME_", env_file=".env", extra="ignore")

    duckdb_memory_limit: str = "4GB"
    duckdb_threads: int = 4
    # Cap on candidate pairs emitted by any single blocking key. Keys that blow
    # through this are almost always degenerate (e.g. every record with a null
    # postcode) and are reported rather than silently truncated.
    max_pairs_per_key: int = 50_000_000
    random_seed: int = 20260805


class Settings(BaseSettings):
    """Top-level settings object passed through the pipeline."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    paths: Paths = Field(default_factory=Paths)
    sources: SourceConfig = Field(default_factory=SourceConfig)
    match: MatchConfig = Field(default_factory=MatchConfig)
    graph: GraphConfig = Field(default_factory=GraphConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)

    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    return Settings()
