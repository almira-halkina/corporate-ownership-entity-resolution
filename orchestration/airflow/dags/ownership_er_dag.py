"""Airflow DAG mirroring the CLI stages.

The pipeline's real interface is the CLI, and this DAG calls it rather than
importing the internals. That is deliberate: it keeps a single execution path,
so the thing running on a schedule is exactly the thing tested in CI and run by
hand. A DAG that reimplements the stages drifts from them, and the drift is
only discovered in production.

Task boundaries match stage boundaries, so a failure is retried from the last
completed stage rather than from the 4GB download.

Not required to run this project — ``make demo`` and ``make run`` cover local
use. It is here because the production shape of this pipeline is a scheduled
job: Companies House republishes the PSC snapshot every morning, so the
resolution is only as current as its last run.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.bash import BashOperator
from airflow.operators.python import PythonOperator

PROJECT_DIR = os.environ.get("OER_PROJECT_DIR", "/opt/ownership-er")
OER = f"cd {PROJECT_DIR} && oer"

DEFAULT_ARGS = {
    "owner": "data-engineering",
    "depends_on_past": False,
    "email_on_failure": True,
    "retries": 2,
    "retry_delay": timedelta(minutes=5),
    # The download is the only task that fails for transient reasons; the
    # compute stages fail deterministically and retrying them wastes time.
    "retry_exponential_backoff": True,
}


def check_quality(**context: object) -> None:
    """Fail the DAG when resolution quality regresses.

    A silent quality regression is worse than a crash: the pipeline keeps
    producing a graph, downstream consumers keep trusting it, and the errors
    surface as wrong answers to ownership questions weeks later. This gate is
    what turns that into a pager alert.
    """
    import json
    from pathlib import Path

    report_path = Path(PROJECT_DIR) / "outputs" / "eval" / "evaluation_rules.json"
    if not report_path.exists():
        raise FileNotFoundError(f"No evaluation report at {report_path}")

    report = json.loads(report_path.read_text(encoding="utf-8"))
    held_out = report.get("held_out_registration", {})

    # Measured on real data with labels nobody chose: corporate PSC filings
    # that state a UK company number. The floor is deliberately below observed
    # performance so it catches regressions rather than noise.
    if held_out.get("available") and held_out["end_to_end_recall"] < 0.85:
        raise ValueError(
            f"Held-out registration recall {held_out['end_to_end_recall']:.3f} "
            f"below the 0.85 floor — investigate before publishing this run."
        )

    bcubed = report.get("clusters", {}).get("Person", {}).get("bcubed_f1")
    if bcubed is not None and bcubed < 0.92:
        raise ValueError(f"B-cubed F1 {bcubed:.3f} below the 0.92 floor.")


with DAG(
    dag_id="corporate_ownership_entity_resolution",
    description="Resolve Companies House + OpenSanctions into an ownership graph.",
    default_args=DEFAULT_ARGS,
    # Companies House publishes the PSC snapshot before 10:00 GMT.
    schedule="0 11 * * *",
    start_date=datetime(2026, 8, 1),
    catchup=False,
    max_active_runs=1,
    tags=["entity-resolution", "sanctions", "graph"],
) as dag:
    fetch = BashOperator(
        task_id="fetch",
        bash_command=f"{OER} fetch --source all",
        execution_timeout=timedelta(hours=2),
    )

    normalize = BashOperator(
        task_id="normalize",
        bash_command=f"{OER} normalize --no-fixtures",
        execution_timeout=timedelta(hours=1),
    )

    block = BashOperator(
        task_id="block",
        bash_command=f"{OER} block",
        execution_timeout=timedelta(hours=1),
    )

    match = BashOperator(
        task_id="match",
        bash_command=f"{OER} match --matcher rules",
        execution_timeout=timedelta(hours=2),
    )

    adjudicate = BashOperator(
        task_id="adjudicate_uncertain",
        bash_command=f"{OER} match --matcher llm",
        # Optional by design: the deterministic result is complete on its own,
        # so an API outage degrades quality slightly rather than failing the run.
        trigger_rule="all_success",
        retries=1,
        execution_timeout=timedelta(hours=1),
    )

    cluster = BashOperator(
        task_id="cluster",
        bash_command=f"{OER} cluster --matcher rules --split-conflicts",
        execution_timeout=timedelta(hours=1),
        trigger_rule="all_done",  # proceed even if adjudication failed
    )

    evaluate = BashOperator(
        task_id="evaluate",
        bash_command=f"{OER} evaluate --truth fixtures/ground_truth.json --matcher rules",
        execution_timeout=timedelta(minutes=30),
    )

    quality_gate = PythonOperator(
        task_id="quality_gate",
        python_callable=check_quality,
    )

    analyse = BashOperator(
        task_id="analyse",
        bash_command=f"{OER} analyse",
        execution_timeout=timedelta(hours=1),
    )

    load_graph = BashOperator(
        task_id="load_graph",
        bash_command=f"{OER} load-graph",
        execution_timeout=timedelta(hours=2),
    )

    export = BashOperator(
        task_id="export_ftm",
        bash_command=f"{OER} export-ftm --out outputs/entities.ftm.json",
        execution_timeout=timedelta(minutes=30),
    )

    (
        fetch
        >> normalize
        >> block
        >> match
        >> adjudicate
        >> cluster
        >> evaluate
        >> quality_gate
        >> [analyse, load_graph, export]
    )
