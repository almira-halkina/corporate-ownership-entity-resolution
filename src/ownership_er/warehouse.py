"""DuckDB warehouse: connection management, DDL, and bulk load helpers.

Why DuckDB rather than Spark
----------------------------
The full working set here is roughly 5.6M companies and 12M PSC filings. That
is large enough that pandas will not hold the candidate-pair join in memory,
and small enough that a distributed engine is pure overhead: an out-of-core
columnar engine on one machine processes it in minutes with no cluster, no
serialisation boundary and no scheduler.

The threshold worth naming is the candidate-pair join, not the record count.
Blocking on this dataset emits on the order of 10^8 pairs, which DuckDB handles
by spilling to disk. Past roughly 10^9 pairs — reached at around 100M input
records, or by loosening blocking substantially — single-node stops being the
right answer and the join has to be partitioned across machines. At that point
the correct move is Spark with the same blocking keys as the partition keys,
which is why the blocking stage materialises keys as a table rather than
computing them inline: the partitioning strategy is already expressed.

See ``docs/design-decisions.md`` for the full argument.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa

from ownership_er.config import Settings, get_settings
from ownership_er.schema import RECORDS_DDL, RELATIONSHIPS_DDL, Record, Relationship

__all__ = [
    "bulk_insert",
    "connect",
    "create_indexes",
    "init_schema",
    "insert_records",
    "insert_relationships",
    "table_count",
]


PAIR_DDL = """
CREATE TABLE IF NOT EXISTS candidate_pairs (
    left_id    VARCHAR NOT NULL,
    right_id   VARCHAR NOT NULL,
    block_keys VARCHAR[],
    n_keys     INTEGER
);
"""

SCORE_DDL = """
CREATE TABLE IF NOT EXISTS pair_scores (
    left_id   VARCHAR NOT NULL,
    right_id  VARCHAR NOT NULL,
    matcher   VARCHAR NOT NULL,
    score     DOUBLE  NOT NULL,
    decision  VARCHAR NOT NULL,      -- accept | reject | uncertain
    features  JSON,
    rationale VARCHAR
);
"""

CLUSTER_DDL = """
CREATE TABLE IF NOT EXISTS clusters (
    record_id     VARCHAR NOT NULL,
    canonical_id  VARCHAR NOT NULL,
    cluster_size  INTEGER,
    split_round   INTEGER DEFAULT 0,
    matcher       VARCHAR
);
"""

CANONICAL_DDL = """
CREATE TABLE IF NOT EXISTS canonical_entities (
    canonical_id   VARCHAR NOT NULL,
    entity_type    VARCHAR,
    name           VARCHAR,
    all_names      VARCHAR[],
    birth_year     INTEGER,
    nationality    VARCHAR,
    country        VARCHAR,
    jurisdiction   VARCHAR,
    reg_number     VARCHAR,
    postcode       VARCHAR,
    topics         VARCHAR[],
    sources        VARCHAR[],
    record_ids     VARCHAR[],
    n_records      INTEGER
);
"""

ALL_DDL = (RECORDS_DDL, RELATIONSHIPS_DDL, PAIR_DDL, SCORE_DDL, CLUSTER_DDL, CANONICAL_DDL)


@contextmanager
def connect(
    settings: Settings | None = None,
    *,
    read_only: bool = False,
    path: Path | None = None,
) -> Iterator[duckdb.DuckDBPyConnection]:
    """Open the warehouse with tuned memory and thread limits."""
    settings = settings or get_settings()
    db_path = path or settings.paths.warehouse
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path), read_only=read_only)
    try:
        con.execute(f"SET memory_limit='{settings.runtime.duckdb_memory_limit}'")
        con.execute(f"SET threads={settings.runtime.duckdb_threads}")
        con.execute("SET preserve_insertion_order=false")
        yield con
    finally:
        con.close()


def init_schema(con: duckdb.DuckDBPyConnection) -> None:
    """Create every table the pipeline uses. Idempotent."""
    for ddl in ALL_DDL:
        con.execute(ddl)


def _dataclass_columns(cls: type) -> list[str]:
    return [f.name for f in dataclass_fields(cls)]


def _batched(items: Iterable[Any], size: int) -> Iterator[list[Any]]:
    batch: list[Any] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


# Arrow schemas for the bulk-load path.
#
# The obvious implementation — `executemany` over a list of tuples — measured at
# roughly 60 rows/sec on this schema, which is about four days for 12M PSC
# records. DuckDB's prepared-statement path pays per-row interpreter and
# constraint-check overhead that dominates completely at this width.
#
# Handing DuckDB a columnar Arrow table instead lets it ingest a whole batch as
# a single vectorised scan. Same data, same types, ~4 orders of magnitude
# faster. This is the difference between the pipeline being runnable at national
# scale and not.
_RECORD_ARROW_SCHEMA = pa.schema(
    [
        ("record_id", pa.string()),
        ("source", pa.string()),
        ("entity_type", pa.string()),
        ("name", pa.string()),
        ("name_norm", pa.string()),
        ("name_fp", pa.string()),
        ("name_phonetic", pa.string()),
        ("first_name", pa.string()),
        ("middle_name", pa.string()),
        ("last_name", pa.string()),
        ("birth_year", pa.int32()),
        ("birth_month", pa.int32()),
        ("nationality", pa.string()),
        ("country", pa.string()),
        ("jurisdiction", pa.string()),
        ("reg_number", pa.string()),
        ("address_full", pa.string()),
        ("address_norm", pa.string()),
        ("postcode", pa.string()),
        ("address_blk", pa.string()),
        ("incorporation_date", pa.string()),
        ("dissolution_date", pa.string()),
        ("status", pa.string()),
        ("legal_form", pa.string()),
        ("sic_codes", pa.list_(pa.string())),
        ("topics", pa.list_(pa.string())),
        ("datasets", pa.list_(pa.string())),
        ("context_company_number", pa.string()),
        ("source_url", pa.string()),
        ("retrieved_at", pa.string()),
    ]
)

_RELATIONSHIP_ARROW_SCHEMA = pa.schema(
    [
        ("rel_id", pa.string()),
        ("rel_type", pa.string()),
        ("source_record_id", pa.string()),
        ("target_record_id", pa.string()),
        ("source", pa.string()),
        ("min_percent", pa.float64()),
        ("max_percent", pa.float64()),
        ("control_kinds", pa.list_(pa.string())),
        ("capacities", pa.list_(pa.string())),
        ("has_hard_control", pa.bool_()),
        ("via_fiduciary", pa.bool_()),
        ("notified_on", pa.string()),
        ("ceased_on", pa.string()),
    ]
)

# Columns typed DATE in the DDL but carried as ISO strings through parsing.
# TRY_CAST rather than CAST: a malformed date in one filing should null that
# field, not abort a batch of 20,000 rows.
_DATE_COLUMNS = {"incorporation_date", "dissolution_date", "notified_on", "ceased_on"}


def _select_list(columns: list[str]) -> str:
    return ", ".join(f"TRY_CAST({c} AS DATE) AS {c}" if c in _DATE_COLUMNS else c for c in columns)


def bulk_insert(
    con: duckdb.DuckDBPyConnection,
    table: str,
    columns: list[str],
    column_data: dict[str, list[Any]],
    schema: pa.Schema | None = None,
) -> int:
    """Insert column-oriented data via Arrow. Returns the number of rows."""
    n = len(column_data[columns[0]]) if columns else 0
    if not n:
        return 0
    arrow_table = pa.table({c: column_data[c] for c in columns}, schema=schema)
    view = f"_bulk_{table}"
    con.register(view, arrow_table)
    try:
        con.execute(
            f"INSERT INTO {table} ({', '.join(columns)}) SELECT {_select_list(columns)} FROM {view}"
        )
    finally:
        con.unregister(view)
    return n


def _insert_dataclasses(
    con: duckdb.DuckDBPyConnection,
    table: str,
    cls: type,
    rows: Iterable[Any],
    schema: pa.Schema,
    batch_size: int = 50_000,
) -> int:
    cols = _dataclass_columns(cls)
    total = 0
    for batch in _batched(rows, batch_size):
        column_data: dict[str, list[Any]] = {c: [getattr(row, c) for row in batch] for c in cols}
        total += bulk_insert(con, table, cols, column_data, schema=schema)
    return total


def insert_records(con: duckdb.DuckDBPyConnection, records: Iterable[Record]) -> int:
    return _insert_dataclasses(con, "records", Record, records, _RECORD_ARROW_SCHEMA)


def insert_relationships(con: duckdb.DuckDBPyConnection, rels: Iterable[Relationship]) -> int:
    return _insert_dataclasses(con, "relationships", Relationship, rels, _RELATIONSHIP_ARROW_SCHEMA)


def create_indexes(con: duckdb.DuckDBPyConnection) -> None:
    """Create join indexes after bulk loading.

    Built after the load, not before: maintaining an index during ingestion
    costs more than building it once at the end, and the pipeline never queries
    these tables mid-load.
    """
    for stmt in (
        "CREATE INDEX IF NOT EXISTS idx_records_id ON records (record_id)",
        "CREATE INDEX IF NOT EXISTS idx_records_type ON records (entity_type)",
        "CREATE INDEX IF NOT EXISTS idx_rel_source ON relationships (source_record_id)",
        "CREATE INDEX IF NOT EXISTS idx_rel_target ON relationships (target_record_id)",
    ):
        con.execute(stmt)


def table_count(con: duckdb.DuckDBPyConnection, table: str) -> int:
    try:
        result = con.execute(f"SELECT count(*) FROM {table}").fetchone()
    except duckdb.CatalogException:
        return 0
    return int(result[0]) if result else 0
