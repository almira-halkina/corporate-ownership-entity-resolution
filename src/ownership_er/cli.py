"""Command-line interface.

Each stage is its own command so the pipeline can be re-entered at any point.
``oer run-all`` chains them for convenience, but the granular commands are the
real interface: iterating on the matcher means re-running two stages, not
re-parsing 12M JSON lines.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from ownership_er import __version__
from ownership_er.config import get_settings

app = typer.Typer(
    name="oer",
    help="Corporate ownership entity resolution: registry + sanctions -> ownership graph.",
    add_completion=False,
    no_args_is_help=True,
)
console = Console()


def _print_json(payload: Any) -> None:
    console.print_json(json.dumps(payload, default=str))


def _table(title: str, rows: list[dict[str, Any]], columns: list[str]) -> None:
    table = Table(title=title, header_style="bold")
    for column in columns:
        table.add_column(column, overflow="fold")
    for row in rows:
        table.add_row(*[str(row.get(c, "")) for c in columns])
    console.print(table)


@app.command()
def version() -> None:
    """Print the version."""
    console.print(f"ownership-er {__version__}")


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@app.command()
def fetch(
    source: str = typer.Option("all", help="all | companies-house | opensanctions"),
    force: bool = typer.Option(False, help="Re-download even if the file exists."),
) -> None:
    """Download source snapshots and record their provenance."""
    from ownership_er import acquire

    settings = get_settings()
    out: dict[str, Any] = {}
    if source in {"all", "companies-house"}:
        out.update(
            {k: v.__dict__ for k, v in acquire.fetch_companies_house(settings, force=force).items()}
        )
    if source in {"all", "opensanctions"}:
        out["opensanctions"] = acquire.fetch_opensanctions(settings, force=force).__dict__
    _print_json(out)


@app.command("make-fixtures")
def make_fixtures(
    out: Path = typer.Option(Path("fixtures"), help="Output directory."),
    companies: int = typer.Option(1200, help="Number of synthetic companies."),
    people: int = typer.Option(400, help="Number of distinct synthetic people."),
    sanctioned: int = typer.Option(60, help="Number of sanctioned entities."),
    difficulty: float = typer.Option(1.0, help="Scales every corruption probability."),
    seed: int = typer.Option(20260805, help="Random seed."),
) -> None:
    """Generate the synthetic corpus and its ground truth."""
    from ownership_er.fixtures import generate_corpus

    corpus = generate_corpus(
        out,
        n_companies=companies,
        n_people=people,
        n_sanctioned=sanctioned,
        difficulty=difficulty,
        seed=seed,
    )
    _print_json(
        {
            "paths": {
                "companies": str(corpus.companies_csv),
                "psc": str(corpus.psc_jsonl),
                "opensanctions": str(corpus.opensanctions_jsonl),
                "truth": str(corpus.truth_json),
            },
            "stats": corpus.stats,
        }
    )


# ---------------------------------------------------------------------------
# Pipeline stages
# ---------------------------------------------------------------------------


@app.command()
def normalize(
    companies: Path | None = typer.Option(None, help="Company CSV or ZIP."),
    psc: Path | None = typer.Option(None, help="PSC JSONL or ZIP."),
    opensanctions: Path | None = typer.Option(None, help="OpenSanctions FtM JSON."),
    fixtures: bool = typer.Option(False, help="Use the committed fixture corpus."),
) -> None:
    """Parse sources into the common record schema."""
    from ownership_er.pipeline import stage_normalize

    settings = get_settings()
    if fixtures:
        base = settings.paths.fixtures
        companies = base / "companies_sample.csv"
        psc = base / "psc_sample.jsonl"
        opensanctions = base / "opensanctions_sample.jsonl"

    stats = stage_normalize(company_path=companies, psc_path=psc, opensanctions_path=opensanctions)
    _print_json(stats)


@app.command()
def block(
    truth: Path | None = typer.Option(None, help="Ground truth, to report pair completeness."),
) -> None:
    """Build blocking keys and generate candidate pairs."""
    from ownership_er.evaluate import load_truth_pairs
    from ownership_er.pipeline import stage_block
    from ownership_er.warehouse import connect

    settings = get_settings()
    truth_table = None
    if truth and truth.exists():
        with connect(settings) as con:
            load_truth_pairs(con, truth)
        truth_table = "truth_pairs"

    stats = stage_block(truth_pairs_table=truth_table)
    report = stats.pop("report", [])
    _print_json(stats)
    if report:
        _table(
            "Blocking keys",
            report,
            [
                "key",
                "entity_type",
                "usable_blocks",
                "oversized_blocks",
                "largest_block",
                "emitted_pairs",
                "pair_completeness",
            ],
        )


@app.command()
def match(
    matcher: str = typer.Option("rules", help="rules | splink | llm"),
) -> None:
    """Score candidate pairs."""
    from ownership_er.pipeline import stage_match

    _print_json(stage_match(matcher=matcher))


@app.command()
def cluster(
    matcher: str = typer.Option("rules", help="Which matcher's decisions to cluster."),
    split_conflicts: bool = typer.Option(True, help="Split clusters with contradictions."),
) -> None:
    """Build canonical entities from accepted pairs."""
    from ownership_er.pipeline import stage_cluster

    _print_json(stage_cluster(matcher=matcher, split_conflicts=split_conflicts))


@app.command("load-graph")
def load_graph(
    dry_run: bool = typer.Option(False, help="Write Cypher to a file instead of loading."),
    reset: bool = typer.Option(False, help="Delete existing Entity nodes first."),
) -> None:
    """Load resolved entities and control edges into Neo4j."""
    from ownership_er.graph.loader import GraphLoader
    from ownership_er.warehouse import connect

    settings = get_settings()
    loader = GraphLoader(settings=settings)
    path = settings.paths.outputs / "load.cypher" if dry_run else None
    with connect(settings) as con:
        result = loader.load(con, dry_run_path=path, reset=reset)
    loader.close()
    _print_json(result)


@app.command()
def analyse(
    top: int = typer.Option(15, help="Rows to display per table."),
) -> None:
    """Run the ownership analyses and write the report."""
    from ownership_er import analysis
    from ownership_er.warehouse import connect

    settings = get_settings()
    with connect(settings) as con:
        report = analysis.run_all(con, out_dir=settings.paths.analysis_dir)

    _print_json(report["resolution_impact"])
    _print_json({k: v for k, v in report["opacity"].items() if k != "by_terminal_jurisdiction"})
    if report["opacity"]["by_terminal_jurisdiction"]:
        _table(
            "Where control chains terminate",
            report["opacity"]["by_terminal_jurisdiction"][:top],
            [
                "terminal_jurisdiction",
                "is_secrecy_jurisdiction",
                "companies_controlled",
                "controlling_entities",
                "mean_hops",
            ],
        )
    if report["sanctions_exposure"]:
        _table(
            "Sanctions exposure through ownership",
            report["sanctions_exposure"][:top],
            [
                "company_name",
                "company_number",
                "shortest_hops",
                "n_sanctioned_owners",
                "max_indirect_percent",
            ],
        )


@app.command()
def evaluate(
    truth: Path = typer.Option(..., help="Ground-truth JSON from make-fixtures."),
    matcher: str = typer.Option("rules", help="Which matcher to score."),
) -> None:
    """Score a matcher against ground truth."""
    from ownership_er.evaluate import evaluate_all
    from ownership_er.warehouse import connect

    settings = get_settings()
    with connect(settings) as con:
        report = evaluate_all(con, truth, matcher=matcher, out_dir=settings.paths.eval_dir)
    _print_json(
        {
            "truth": report["truth"],
            "pairwise": report["pairwise"],
            "clusters": report["clusters"],
            "held_out_registration": report["held_out_registration"],
            "lost_by_stage": report["errors"]["lost_by_stage"],
        }
    )


@app.command("validate-ftm")
def validate_ftm(
    limit: int = typer.Option(0, help="Validate at most N entities (0 = all)."),
) -> None:
    """Round-trip emitted entities through the real FollowTheMoney library."""
    from ownership_er.schema import to_ftm, validate_ftm_available, validate_ftm_stream
    from ownership_er.warehouse import connect

    if not validate_ftm_available():
        console.print(
            "[yellow]followthemoney is not installed. "
            "Install with: pip install -e '.[ftm]'[/yellow]"
        )
        raise typer.Exit(code=2)

    from ownership_er.schema import Record

    settings = get_settings()
    with connect(settings) as con:
        columns = [f.name for f in dataclasses.fields(Record)]
        sql = f"SELECT {', '.join(columns)} FROM records"
        if limit:
            sql += f" LIMIT {limit}"
        rows = con.execute(sql).fetchall()

    entities = [to_ftm(Record(**dict(zip(columns, row, strict=True)))) for row in rows]
    errors = list(validate_ftm_stream(entities))
    _print_json({"validated": len(entities), "errors": errors[:50], "n_errors": len(errors)})
    if errors:
        raise typer.Exit(code=1)


@app.command("export-ftm")
def export_ftm(
    out: Path = typer.Option(Path("outputs/entities.ftm.json"), help="Output path."),
) -> None:
    """Export canonical entities as FollowTheMoney newline-delimited JSON."""
    from ownership_er.schema import write_ftm
    from ownership_er.warehouse import connect

    settings = get_settings()
    with connect(settings) as con:
        rows = con.execute(
            """
            SELECT canonical_id, entity_type, name, all_names, birth_year,
                   nationality, country, jurisdiction, reg_number, topics, sources
            FROM canonical_entities
            """
        ).fetchall()

    def _entities() -> Any:
        for (
            cid,
            etype,
            name,
            all_names,
            birth_year,
            nationality,
            country,
            jurisdiction,
            reg_number,
            topics,
            sources,
        ) in rows:
            props: dict[str, list[str]] = {"name": list(all_names or [name or ""])}
            if birth_year:
                props["birthDate"] = [str(birth_year)]
            for key, value in (
                ("nationality", nationality),
                ("country", country),
                ("jurisdiction", jurisdiction),
                ("registrationNumber", reg_number),
            ):
                if value:
                    props[key] = [value]
            if topics:
                props["topics"] = list(topics)
            yield {
                "id": cid,
                "caption": name,
                "schema": "Person" if etype == "Person" else "Company",
                "properties": {k: v for k, v in props.items() if v},
                "datasets": list(sources or []),
            }

    count = write_ftm(_entities(), out)
    _print_json({"written": count, "path": str(out)})


@app.command("build-index")
def build_index(
    out: Path | None = typer.Option(None, "--out", help="Destination .duckdb file."),
) -> None:
    """Flatten the warehouse into the read-only serving index.

    Run after `cluster`. Everything expensive for the API — the name index and
    the sanctions closure — is computed here so the request path stays a lookup.
    """
    from ownership_er.serve.index import build_serving_index

    _print_json(build_serving_index(out_path=out))


@app.command("serve")
def serve(
    host: str = typer.Option("127.0.0.1", help="Bind address."),
    port: int = typer.Option(8080, help="Bind port."),
    index: Path | None = typer.Option(None, "--index", help="Serving index to read."),
    reload: bool = typer.Option(False, "--reload", help="Auto-reload on code change."),
) -> None:
    """Serve the ownership API and its single-page front end."""
    import os

    import uvicorn

    if index is not None:
        os.environ["OER_SERVING_INDEX"] = str(index)
    uvicorn.run("ownership_er.serve.api:app", host=host, port=port, reload=reload, log_level="info")


@app.command("run-all")
def run_all(
    fixtures: bool = typer.Option(True, help="Run against the committed fixture corpus."),
    matcher: str = typer.Option("rules", help="rules | splink | llm"),
    evaluate_after: bool = typer.Option(True, help="Score against ground truth."),
    load_neo4j: bool = typer.Option(False, help="Load into Neo4j (needs a server)."),
) -> None:
    """Run the whole pipeline end to end."""
    from ownership_er import analysis
    from ownership_er.evaluate import evaluate_all
    from ownership_er.pipeline import stage_block, stage_cluster, stage_match, stage_normalize
    from ownership_er.warehouse import connect

    settings = get_settings()
    base = settings.paths.fixtures
    companies: Path | None
    psc: Path | None
    opensanctions: Path | None
    truth: Path | None

    if fixtures:
        companies = base / "companies_sample.csv"
        psc = base / "psc_sample.jsonl"
        opensanctions = base / "opensanctions_sample.jsonl"
        truth = base / "ground_truth.json"
    else:
        from ownership_er.acquire import read_manifest

        manifest_paths = {k: Path(v["path"]) for k, v in read_manifest(settings).items()}
        companies = manifest_paths.get("ch_companies")
        psc = manifest_paths.get("ch_psc")
        opensanctions = manifest_paths.get("opensanctions")
        truth = None
        if not any((companies, psc, opensanctions)):
            console.print(
                "[red]No downloaded sources found.[/red] Run [bold]make data[/bold] "
                "first, or use --fixtures."
            )
            raise typer.Exit(code=1)

    console.rule("normalize")
    _print_json(
        stage_normalize(company_path=companies, psc_path=psc, opensanctions_path=opensanctions)
    )
    console.rule("block")
    stats = stage_block()
    stats.pop("report", None)
    _print_json(stats)
    console.rule(f"match ({matcher})")
    _print_json(stage_match(matcher=matcher))
    console.rule("cluster")
    _print_json(stage_cluster(matcher=matcher))
    console.rule("analyse")
    with connect(settings) as con:
        report = analysis.run_all(con, out_dir=settings.paths.analysis_dir)
    _print_json(report["resolution_impact"])

    if evaluate_after and truth and truth.exists():
        console.rule("evaluate")
        with connect(settings) as con:
            evaluation = evaluate_all(con, truth, matcher=matcher, out_dir=settings.paths.eval_dir)
        _print_json(
            {
                "pairwise": evaluation["pairwise"],
                "clusters": evaluation["clusters"],
                "held_out_registration": evaluation["held_out_registration"],
            }
        )

    if load_neo4j:
        console.rule("load-graph")
        from ownership_er.graph.loader import GraphLoader

        loader = GraphLoader(settings=settings)
        with connect(settings) as con:
            _print_json(loader.load(con))
        loader.close()


if __name__ == "__main__":  # pragma: no cover
    app()
