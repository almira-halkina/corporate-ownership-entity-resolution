"""Concurrency sweep. Writes the table that goes in the README.

One number is not a benchmark: a service can look fast at one client and fall
over at sixteen, and it can look slow at sixteen purely because it is already
saturated. Sweeping shows where the knee is, which is the number that decides
how the host should be configured.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from loadtest import cold_start, run  # type: ignore[import-not-found]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8099")
    ap.add_argument("--seconds", type=int, default=12)
    ap.add_argument("--levels", default="1,4,16,32")
    ap.add_argument("--out", default="bench/sweep.json")
    args = ap.parse_args()

    levels = [int(x) for x in args.levels.split(",")]
    report = {"levels": [], "cold_start": asyncio.run(cold_start(args.base))}
    for c in levels:
        r = asyncio.run(run(args.base, c, args.seconds, warmup=30))
        report["levels"].append(
            {"concurrency": c, "rps": r["throughput_rps"], **r["overall"]}
        )
        if "index" not in report:
            report["index"] = r["index"]

    print(f"{'conc':>5} {'rps':>9} {'p50 ms':>9} {'p95 ms':>9} {'p99 ms':>9} {'errors':>7}")
    for row in report["levels"]:
        print(
            f"{row['concurrency']:>5} {row['rps']:>9.1f} {row['p50_ms']:>9.2f} "
            f"{row['p95_ms']:>9.2f} {row['p99_ms']:>9.2f} {row['errors']:>7}"
        )
    Path(args.out).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"\nwritten to {args.out}")


if __name__ == "__main__":
    main()
