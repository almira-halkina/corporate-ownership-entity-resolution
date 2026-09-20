"""Load test for the ownership service.

Reports what a reviewer actually needs to know about a running service:
sustained throughput, tail latency per endpoint, and cold start. Deliberately
small and dependency-light (httpx only) so it runs in CI on every push rather
than existing as a number somebody measured once and pasted into a README.

Method notes, because the numbers are meaningless without them:

* **Warm-up requests are discarded.** DuckDB compiles a query plan on first
  execution; including that in a latency distribution measures the compiler,
  not the service. Cold start is reported separately and on purpose.
* **Latency is measured per request, not per batch**, and percentiles come from
  the full sorted sample rather than a streaming estimate.
* **Concurrency is real**, not simulated: N coroutines each issue requests in a
  closed loop for the whole window, so reported RPS is sustained rather than
  peak.
* The **query mix is weighted towards search** because that is the entry point
  for every session, with traversal and the precomputed sanctions lookup behind
  it in the proportion a browsing user generates.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

DEFAULT_BASE = "http://127.0.0.1:8099"

# Prefixes that exist in the fixture corpus. Randomising the term matters:
# hammering one string measures the query cache, not the index.
TERMS = [
    "eastbrook", "ravensworth", "blackwater", "harbourview", "crestwood",
    "pinnacle", "ashcroft", "ludgate", "redwood", "fairhaven", "beaconsfield",
    "macgregor", "nowak", "karimoff", "larsen", "beuamont", "ltd", "plc",
]


@dataclass
class Sample:
    endpoint: str
    ms: float
    ok: bool


@dataclass
class Result:
    samples: list[Sample] = field(default_factory=list)

    def add(self, s: Sample) -> None:
        self.samples.append(s)

    @staticmethod
    def _pct(xs: list[float], p: float) -> float:
        if not xs:
            return 0.0
        xs = sorted(xs)
        idx = min(len(xs) - 1, max(0, round(p * (len(xs) - 1))))
        return round(xs[idx], 2)

    def summary(self, endpoint: str | None = None) -> dict[str, float | int]:
        subset = [s for s in self.samples if endpoint is None or s.endpoint == endpoint]
        lat = [s.ms for s in subset if s.ok]
        errors = sum(1 for s in subset if not s.ok)
        return {
            "requests": len(subset),
            "errors": errors,
            "p50_ms": self._pct(lat, 0.50),
            "p95_ms": self._pct(lat, 0.95),
            "p99_ms": self._pct(lat, 0.99),
            "max_ms": round(max(lat), 2) if lat else 0.0,
            "mean_ms": round(statistics.fmean(lat), 2) if lat else 0.0,
        }


async def _one(client: httpx.AsyncClient, ids: list[str]) -> Sample:
    roll = random.random()
    if roll < 0.55:
        endpoint, url = "search", f"/api/search?q={random.choice(TERMS)}"
    elif roll < 0.80:
        endpoint, url = "ownership", f"/api/entity/{random.choice(ids)}/ownership?direction=up"
    elif roll < 0.95:
        endpoint, url = "sanctions", f"/api/entity/{random.choice(ids)}/sanctions"
    else:
        endpoint, url = "entity", f"/api/entity/{random.choice(ids)}"
    t0 = time.perf_counter()
    try:
        r = await client.get(url)
        ok = r.status_code == 200
    except Exception:
        ok = False
    return Sample(endpoint, (time.perf_counter() - t0) * 1000, ok)


async def _worker(client: httpx.AsyncClient, ids: list[str], deadline: float, out: Result) -> None:
    while time.perf_counter() < deadline:
        out.add(await _one(client, ids))


async def run(base: str, concurrency: int, seconds: int, warmup: int) -> dict:
    limits = httpx.Limits(max_connections=concurrency * 2, max_keepalive_connections=concurrency * 2)
    async with httpx.AsyncClient(base_url=base, timeout=30.0, limits=limits) as client:
        health = (await client.get("/health")).json()

        # Entity ids to traverse: a mix of sanctions-exposed companies (the
        # expensive path) and arbitrary search hits (the common one).
        exposed = (await client.get("/api/sanctions/exposed?limit=40")).json()
        ids = [r["id"] for r in exposed.get("results", [])]
        seed = (await client.get("/api/search?q=ltd&limit=40")).json()
        ids += [r["id"] for r in seed.get("results", [])]
        ids = list(dict.fromkeys(ids)) or ["ent:ch:company:01000059"]

        for _ in range(warmup):
            await _one(client, ids)

        result = Result()
        started = time.perf_counter()
        deadline = started + seconds
        await asyncio.gather(
            *[_worker(client, ids, deadline, result) for _ in range(concurrency)]
        )
        wall = time.perf_counter() - started

    per_endpoint = {
        name: result.summary(name)
        for name in ("search", "ownership", "sanctions", "entity")
    }
    overall = result.summary()
    return {
        "config": {"concurrency": concurrency, "seconds": seconds, "warmup_requests": warmup},
        "index": health.get("index", {}),
        "throughput_rps": round(len(result.samples) / wall, 1),
        "overall": overall,
        "by_endpoint": per_endpoint,
    }


async def cold_start(base: str, attempts: int = 5) -> dict:
    """Time the first successful request against a freshly started process.

    Run separately from the throughput test: it is a property of the deploy,
    not of the steady state, and it is the number that decides whether a
    scale-to-zero host is acceptable for a demo link.
    """
    async with httpx.AsyncClient(base_url=base, timeout=60.0) as client:
        times = []
        for _ in range(attempts):
            t0 = time.perf_counter()
            r = await client.get(f"/api/search?q={random.choice(TERMS)}")
            times.append((time.perf_counter() - t0) * 1000)
            if r.status_code != 200:
                break
        return {"first_request_ms": round(times[0], 2) if times else None,
                "subsequent_ms": [round(t, 2) for t in times[1:]]}


def main() -> None:
    ap = argparse.ArgumentParser(description="Load test the ownership service.")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--concurrency", type=int, default=16)
    ap.add_argument("--seconds", type=int, default=20)
    ap.add_argument("--warmup", type=int, default=50)
    ap.add_argument("--json-out", default=None)
    ap.add_argument("--cold-start-only", action="store_true")
    args = ap.parse_args()

    random.seed(20260919)
    if args.cold_start_only:
        report = asyncio.run(cold_start(args.base))
    else:
        report = asyncio.run(run(args.base, args.concurrency, args.seconds, args.warmup))
    text = json.dumps(report, indent=2)
    print(text)
    if args.json_out:
        Path(args.json_out).write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
