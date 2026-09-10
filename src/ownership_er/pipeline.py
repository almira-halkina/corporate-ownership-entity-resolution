"""Stage functions. The CLI is a thin wrapper over these.

Each stage reads and writes the DuckDB warehouse and is independently
re-runnable, which is what makes the pipeline debuggable: a matcher change
re-runs matching and clustering in seconds without re-parsing 12M JSON lines.
Stage boundaries are the same ones the Airflow DAG in ``orchestration/`` uses
as task boundaries.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from ownership_er.block import build_blocking_keys, generate_candidate_pairs
from ownership_er.config import Settings, get_settings
from ownership_er.schema import Record, Relationship
from ownership_er.sources import (
    parse_company_csv,
    parse_opensanctions_jsonl,
    parse_psc_jsonl,
)
from ownership_er.warehouse import (
    connect,
    init_schema,
    insert_records,
    insert_relationships,
    table_count,
)

__all__ = [
    "stage_block",
    "stage_cluster",
    "stage_match",
    "stage_normalize",
]


def _batched_insert_records(con: Any, records: Any, batch: int = 20_000) -> int:
    total = 0
    buf: list[Record] = []
    for rec in records:
        buf.append(rec)
        if len(buf) >= batch:
            total += insert_records(con, buf)
            buf = []
    if buf:
        total += insert_records(con, buf)
    return total


def stage_normalize(
    *,
    company_path: Path | None = None,
    psc_path: Path | None = None,
    opensanctions_path: Path | None = None,
    settings: Settings | None = None,
    reset: bool = True,
) -> dict[str, Any]:
    """Parse every available source into ``records`` and ``relationships``."""
    settings = settings or get_settings()
    settings.paths.ensure()
    stats: dict[str, Any] = {}

    with connect(settings) as con:
        init_schema(con)
        if reset:
            con.execute("DELETE FROM records")
            con.execute("DELETE FROM relationships")

        if company_path and company_path.exists():
            stats["ch_companies"] = _batched_insert_records(con, parse_company_csv(company_path))

        if psc_path and psc_path.exists():
            kinds: Counter[str] = Counter()
            rec_buf: list[Record] = []
            rel_buf: list[Relationship] = []
            n_records = n_rels = 0
            for record, rel, kind in parse_psc_jsonl(psc_path):
                kinds[kind] += 1
                if record is not None:
                    rec_buf.append(record)
                if rel is not None:
                    rel_buf.append(rel)
                if len(rec_buf) >= 20_000:
                    n_records += insert_records(con, rec_buf)
                    rec_buf = []
                if len(rel_buf) >= 20_000:
                    n_rels += insert_relationships(con, rel_buf)
                    rel_buf = []
            if rec_buf:
                n_records += insert_records(con, rec_buf)
            if rel_buf:
                n_rels += insert_relationships(con, rel_buf)
            stats["ch_psc_records"] = n_records
            stats["ch_psc_relationships"] = n_rels
            # Statement and exemption rows are counted, never parsed as
            # entities. The count is a finding in its own right: it is the
            # share of the register that declares no identifiable owner.
            stats["ch_psc_kinds"] = dict(kinds)
            stats["ch_psc_statements"] = sum(
                v for k, v in kinds.items() if "statement" in k or k == "exemptions"
            )
            stats["ch_psc_super_secure"] = kinds.get(
                "super-secure-person-with-significant-control", 0
            )

        if opensanctions_path and opensanctions_path.exists():
            stats["opensanctions"] = _batched_insert_records(
                con, parse_opensanctions_jsonl(opensanctions_path)
            )

        stats["total_records"] = table_count(con, "records")
        stats["total_relationships"] = table_count(con, "relationships")
        by_type = con.execute(
            "SELECT entity_type, source, count(*) FROM records GROUP BY 1, 2 ORDER BY 1, 2"
        ).fetchall()
        stats["by_type"] = [{"entity_type": t, "source": s, "n": int(n)} for t, s, n in by_type]

    return stats


def stage_block(
    *,
    settings: Settings | None = None,
    truth_pairs_table: str | None = None,
) -> dict[str, Any]:
    """Materialise blocking keys and generate candidate pairs."""
    from ownership_er.block import blocking_report

    settings = settings or get_settings()
    with connect(settings) as con:
        init_schema(con)
        key_counts = build_blocking_keys(con)
        pair_stats = generate_candidate_pairs(con)
        report = blocking_report(con, truth_pairs_table=truth_pairs_table)

    out = settings.paths.eval_dir
    out.mkdir(parents=True, exist_ok=True)
    (out / "blocking_report.json").write_text(
        json.dumps({"keys": key_counts, "pairs": pair_stats, "report": report}, indent=2),
        encoding="utf-8",
    )
    return {"keys": key_counts, **pair_stats, "report": report}


def stage_match(
    *,
    matcher: str = "rules",
    settings: Settings | None = None,
    reset: bool = True,
) -> dict[str, Any]:
    """Score candidate pairs with the named matcher."""
    settings = settings or get_settings()

    with connect(settings) as con:
        init_schema(con)
        impl = _build_matcher(matcher, settings)
        if reset:
            con.execute("DELETE FROM pair_scores WHERE matcher = ?", [impl.name])
        for entity_type in ("Person", "Company"):
            impl.score_pairs(con, entity_type)

        rows = con.execute(
            """
            SELECT decision, count(*) FROM pair_scores
            WHERE matcher = ? GROUP BY 1
            """,
            [impl.name],
        ).fetchall()
        counts = {d: int(n) for d, n in rows}
        total = sum(counts.values())

    return {
        "matcher": impl.name,
        "scored": total,
        "accept": counts.get("accept", 0),
        "uncertain": counts.get("uncertain", 0),
        "reject": counts.get("reject", 0),
        "uncertain_fraction": (counts.get("uncertain", 0) / total) if total else 0.0,
    }


def _build_matcher(name: str, settings: Settings) -> Any:
    if name == "rules":
        from ownership_er.match.rules import RuleMatcher

        return RuleMatcher(accept=settings.match.auto_accept, reject=settings.match.auto_reject)
    if name == "splink":
        from ownership_er.match.splink_model import SplinkMatcher

        return SplinkMatcher(accept=settings.match.auto_accept, reject=settings.match.auto_reject)
    if name == "llm":
        from ownership_er.match.llm import LLMAdjudicator

        return LLMAdjudicator(settings=settings)
    raise ValueError(f"unknown matcher {name!r}; expected rules, splink or llm")


def stage_cluster(
    *,
    matcher: str = "rules",
    settings: Settings | None = None,
    split_conflicts: bool = True,
) -> dict[str, Any]:
    """Build connected components over accepted pairs, then split conflicts."""
    from ownership_er.cluster import build_clusters

    settings = settings or get_settings()
    with connect(settings) as con:
        init_schema(con)
        return build_clusters(con, matcher=matcher, split_conflicts=split_conflicts)
