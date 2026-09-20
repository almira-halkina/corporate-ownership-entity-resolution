"""FastAPI service over the serving index.

Request-path invariants, all of them deliberate:

* **Read-only, one cursor per worker thread.** The index is immutable once
  built, so the process opens it once at startup with ``read_only=True``. It
  does *not* share that connection: FastAPI runs sync endpoints in a thread
  pool, and a DuckDB connection object is not safe for concurrent execution —
  two threads calling ``execute`` on the same handle interleave and read each
  other's result sets. The first load test at 16 concurrent clients returned a
  19% error rate for exactly this reason, with symptoms (``'float' object is
  not iterable``, ``tuple index out of range``) that look like data corruption
  rather than a concurrency bug, which is what makes it worth naming here.
  ``connection.cursor()`` returns an independent handle onto the same open
  database; one is created lazily per worker thread and reused. See
  ``bench/results_before_fix.json`` against ``bench/results.json``.
* **No pipeline imports.** Nothing in this module can reach a matcher, a
  clusterer, or a source parser. That is enforced by what it imports, and it is
  the property that keeps p99 flat: a request cannot accidentally trigger work
  that scales with the corpus.
* **Every traversal is bounded** — by hop count and by result count — because
  ownership graphs contain real cycles, and an unbounded walk on a cyclic graph
  does not terminate usefully.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from ownership_er.serve.index import MAX_HOPS, default_index_path

__all__ = ["app", "create_app"]

_STATIC = Path(__file__).parent / "static"

# Canonical ids are colon-delimited (`ent:ch:company:01000059`, `ent:os:NK-N-00010`)
# and never contain a slash, so routes use the plain path converter. `:path`
# would be greedy and swallow the `/sanctions` and `/ownership` suffixes.

# Risk topics that make an entity sanctioned rather than merely notable. Same
# list as the graph loader uses, kept here so the API does not depend on it.
_SANCTION_TOPICS = ("sanction", "sanction.linked", "sanction.counter", "export.control")


@dataclass
class _Walk:
    """One position in a bounded graph walk.

    A dataclass rather than a dict so the propagated interval and the
    visited-path stay typed: the arithmetic below multiplies bounds, and a
    silently-`object`-typed accumulator is exactly how an interval turns into
    a wrong number.
    """

    id: str
    path: list[str] = field(default_factory=list)
    lo: float = 1.0
    hi: float = 1.0
    control_only: bool = False
    fiduciary: bool = False


class _Latency:
    """Fixed-size reservoir of recent request latencies, in milliseconds.

    A ring buffer rather than a full histogram: /metrics exists so an operator
    (or a load test) can read tail latency off a running instance, not to feed
    a time-series database. 4096 samples is a few minutes of traffic at demo
    rates and costs 32 KB.
    """

    def __init__(self, size: int = 4096) -> None:
        self._buf: deque[float] = deque(maxlen=size)
        self.total = 0
        self.errors = 0

    def observe(self, ms: float) -> None:
        self._buf.append(ms)
        self.total += 1

    def percentiles(self) -> dict[str, float]:
        if not self._buf:
            return {"p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0}
        ordered = sorted(self._buf)
        n = len(ordered)

        def pct(p: float) -> float:
            idx = min(n - 1, max(0, round(p * (n - 1))))
            return round(ordered[idx], 3)

        return {"p50": pct(0.50), "p95": pct(0.95), "p99": pct(0.99), "max": round(ordered[-1], 3)}


def _row_to_entity(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "id": row[0],
        "name": row[1],
        "type": row[2],
        "aliases": list(row[3] or []),
        "jurisdiction": row[4] or None,
        "country": row[5] or None,
        "nationality": row[6] or None,
        "reg_number": row[7] or None,
        "topics": list(row[8] or []),
        "n_records": row[9],
        "is_sanctioned": bool(row[10]),
        "is_pep": bool(row[11]),
    }


# Always selected through the alias ``e`` so the same fragment works in a
# bare select and in a join without ambiguity.
_ENTITY_COLS = """
    e.canonical_id, e.name, e.entity_type, e.all_names, e.jurisdiction, e.country,
    e.nationality, e.reg_number, e.topics, e.n_records, e.is_sanctioned, e.is_pep
"""


def create_app(index_path: Path | None = None) -> FastAPI:
    resolved = index_path or Path(os.environ.get("OER_SERVING_INDEX", ""))
    if not resolved or str(resolved) == ".":
        resolved = default_index_path()

    state: dict[str, Any] = {}

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if not resolved.exists():
            raise RuntimeError(
                f"serving index not found at {resolved}. "
                "Build it with `oer build-index` after running the pipeline."
            )
        started = time.perf_counter()
        con = duckdb.connect(str(resolved), read_only=True)
        con.execute("SET threads=2")
        meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
        # Touch every table once so the first real request does not pay for
        # page-cache misses. Cold start is a number the load test reports, and
        # it should measure the process, not the first unlucky user.
        con.execute("SELECT count(*) FROM entities").fetchone()
        con.execute("SELECT count(*) FROM edges").fetchone()
        con.execute("SELECT count(*) FROM sanctions_exposure").fetchone()
        state["con"] = con
        state["meta"] = meta
        state["latency"] = _Latency()
        state["started_at"] = time.time()
        state["warmup_ms"] = round((time.perf_counter() - started) * 1000, 2)
        yield
        con.close()

    app = FastAPI(
        title="Beneficial Ownership & Sanctions Exposure API",
        version="1.0.0",
        description=(
            "Resolves corporate registry and sanctions records into canonical "
            "entities and answers who ultimately controls a company — including "
            "where the controlling party never appears on that company's own filing."
        ),
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def _timing(request: Request, call_next: Any) -> Any:
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            if "latency" in state:
                state["latency"].errors += 1
            raise
        elapsed_ms = (time.perf_counter() - start) * 1000
        if "latency" in state and request.url.path.startswith("/api"):
            state["latency"].observe(elapsed_ms)
            if response.status_code >= 500:
                state["latency"].errors += 1
        response.headers["X-Response-Time-ms"] = f"{elapsed_ms:.3f}"
        return response

    _local = threading.local()

    def con() -> duckdb.DuckDBPyConnection:
        """Cursor for the calling thread, created on first use and reused.

        Thread pool workers are long-lived, so this amortises to one cursor per
        worker rather than one per request.
        """
        cur = getattr(_local, "cursor", None)
        if cur is None:
            cur = state["con"].cursor()
            _local.cursor = cur
        return cur

    # ---------------------------------------------------------------- health

    @app.get("/health", tags=["ops"])
    def health() -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "uptime_seconds": round(time.time() - state["started_at"], 1),
                "warmup_ms": state["warmup_ms"],
                "index": state["meta"],
            }
        )

    @app.get("/metrics", tags=["ops"], response_class=PlainTextResponse)
    def metrics() -> str:
        lat = state["latency"]
        p = lat.percentiles()
        lines = [
            "# HELP oer_requests_total API requests served since start.",
            "# TYPE oer_requests_total counter",
            f"oer_requests_total {lat.total}",
            "# HELP oer_request_errors_total API requests that returned 5xx.",
            "# TYPE oer_request_errors_total counter",
            f"oer_request_errors_total {lat.errors}",
            "# HELP oer_request_latency_ms Recent request latency percentiles.",
            "# TYPE oer_request_latency_ms gauge",
        ]
        lines += [f'oer_request_latency_ms{{quantile="{k}"}} {v}' for k, v in p.items()]
        lines += [
            "# HELP oer_index_entities Canonical entities in the serving index.",
            "# TYPE oer_index_entities gauge",
            f"oer_index_entities {state['meta'].get('entities', 0)}",
            "# HELP oer_uptime_seconds Seconds since process start.",
            "# TYPE oer_uptime_seconds gauge",
            f"oer_uptime_seconds {round(time.time() - state['started_at'], 1)}",
        ]
        return "\n".join(lines) + "\n"

    # ---------------------------------------------------------------- search

    @app.get("/api/search", tags=["query"])
    def search(
        q: str = Query(..., min_length=2, max_length=120),
        limit: int = Query(20, ge=1, le=100),
    ) -> dict[str, Any]:
        """Resolve a name to candidate entities.

        Ranking is explicit rather than learned, because a screening analyst
        has to be able to say why a hit surfaced: exact normalised match, then
        prefix, then containment; ties broken by whether the variant is the
        entity's primary spelling and by how many source records back it.
        """
        started = time.perf_counter()
        needle = " ".join(q.lower().split())
        rows = con().execute(
            f"""
            WITH hits AS (
                SELECT ni.canonical_id,
                       max(CASE
                           WHEN ni.variant_norm = ?          THEN 3
                           WHEN ni.variant_norm LIKE ? || '%' THEN 2
                           ELSE 1 END)                        AS tier,
                       max(ni.is_primary::INT)                AS primary_hit,
                       any_value(ni.variant)                  AS matched_on
                FROM name_index ni
                WHERE ni.variant_norm LIKE '%' || ? || '%'
                GROUP BY ni.canonical_id
            )
            SELECT {_ENTITY_COLS}, h.tier, h.primary_hit, h.matched_on,
                   (SELECT count(*) FROM sanctions_exposure se
                     WHERE se.asset_id = e.canonical_id) AS exposure_links
            FROM hits h JOIN entities e ON e.canonical_id = h.canonical_id
            ORDER BY h.tier DESC, h.primary_hit DESC, e.is_sanctioned DESC,
                     e.n_records DESC, e.name
            LIMIT ?
            """,
            [needle, needle, needle, limit],
        ).fetchall()

        results = []
        for r in rows:
            ent = _row_to_entity(r)
            ent["match"] = {
                "tier": {3: "exact", 2: "prefix", 1: "contains"}[r[12]],
                "matched_on": r[14],
            }
            ent["sanctions_exposure_links"] = r[15]
            results.append(ent)
        return {
            "query": q,
            "count": len(results),
            "took_ms": round((time.perf_counter() - started) * 1000, 3),
            "results": results,
        }

    # ---------------------------------------------------------------- entity

    def _fetch_entity(entity_id: str) -> dict[str, Any]:
        row = con().execute(
            f"SELECT {_ENTITY_COLS} FROM entities e WHERE e.canonical_id = ?", [entity_id]
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"unknown entity {entity_id!r}")
        return _row_to_entity(row)

    @app.get("/api/entity/{entity_id}", tags=["query"])
    def entity(entity_id: str) -> dict[str, Any]:
        ent = _fetch_entity(entity_id)
        counts = con().execute(
            """
            SELECT
              (SELECT count(*) FROM edges WHERE owner_id = ?) AS controls,
              (SELECT count(*) FROM edges WHERE asset_id = ?) AS controlled_by,
              (SELECT count(*) FROM sanctions_exposure WHERE asset_id = ?) AS exposure
            """,
            [entity_id, entity_id, entity_id],
        ).fetchone()
        ent["edge_counts"] = {
            "controls": counts[0],
            "controlled_by": counts[1],
            "sanctions_exposure_links": counts[2],
        }
        return ent

    # ------------------------------------------------------------- ownership

    @app.get("/api/entity/{entity_id}/ownership", tags=["query"])
    def ownership(
        entity_id: str,
        direction: str = Query("up", pattern="^(up|down)$"),
        max_hops: int = Query(MAX_HOPS, ge=1, le=MAX_HOPS),
    ) -> dict[str, Any]:
        """Walk the control graph from one entity.

        ``up`` answers "who controls this", ``down`` answers "what does this
        control". Percentages propagate as intervals, never as midpoints:
        Companies House publishes bands, so a 25-50%% hop through a 50-75%% hop
        is 12.5-37.5%% and nothing narrower is defensible. A hop that asserts
        control without a percentage abandons the arithmetic and marks the path
        ``control_only``.
        """
        _fetch_entity(entity_id)
        started = time.perf_counter()
        up = direction == "up"
        step_from, step_to = ("asset_id", "owner_id") if up else ("owner_id", "asset_id")

        frontier: list[_Walk] = [_Walk(id=entity_id, path=[entity_id])]
        found: dict[str, dict[str, Any]] = {}
        truncated = False
        for hop in range(1, max_hops + 1):
            if not frontier:
                break
            ids = [f.id for f in frontier]
            placeholders = ",".join("?" * len(ids))
            rows = con().execute(
                f"""
                SELECT g.{step_from} AS from_id, g.{step_to} AS to_id,
                       g.min_percent, g.max_percent, g.control_kinds,
                       g.has_hard_control, g.via_fiduciary, g.n_filings,
                       e.name, e.entity_type, e.jurisdiction, e.is_sanctioned, e.is_pep
                FROM edges g JOIN entities e ON e.canonical_id = g.{step_to}
                WHERE g.{step_from} IN ({placeholders}) AND coalesce(g.is_active, true)
                """,
                ids,
            ).fetchall()
            by_from: dict[str, list[tuple[Any, ...]]] = {}
            for r in rows:
                by_from.setdefault(r[0], []).append(r)

            next_frontier: list[_Walk] = []
            for node in frontier:
                for r in by_from.get(node.id, []):
                    nxt = str(r[1])
                    if nxt in node.path:
                        continue  # cycle
                    lo = node.lo * ((r[2] if r[2] is not None else 0.0) / 100.0)
                    hi = node.hi * ((r[3] if r[3] is not None else 100.0) / 100.0)
                    control_only = node.control_only or r[2] is None
                    fiduciary = node.fiduciary or bool(r[6])
                    path = [*node.path, nxt]
                    prev = found.get(nxt)
                    if prev is None or hop < prev["hops"]:
                        found[nxt] = {
                            "id": nxt,
                            "name": r[8],
                            "type": r[9],
                            "jurisdiction": r[10] or None,
                            "is_sanctioned": bool(r[11]),
                            "is_pep": bool(r[12]),
                            "hops": hop,
                            "min_percent": None if control_only else round(lo * 100, 3),
                            "max_percent": None if control_only else round(hi * 100, 3),
                            "control_only": control_only,
                            "via_fiduciary": fiduciary,
                            "control_kinds": list(r[4] or []),
                            "n_filings": r[7],
                            "path": path,
                        }
                    if len(next_frontier) >= 2000:
                        truncated = True
                        continue
                    next_frontier.append(
                        _Walk(
                            id=nxt,
                            path=path,
                            lo=lo,
                            hi=hi,
                            control_only=control_only,
                            fiduciary=fiduciary,
                        )
                    )
            frontier = next_frontier

        nodes = sorted(found.values(), key=lambda n: (n["hops"], -(n["max_percent"] or 0)))
        return {
            "entity": entity_id,
            "direction": direction,
            "max_hops": max_hops,
            "count": len(nodes),
            "truncated": truncated,
            "took_ms": round((time.perf_counter() - started) * 1000, 3),
            "nodes": nodes,
        }

    # ------------------------------------------------------------- sanctions

    @app.get("/api/entity/{entity_id}/sanctions", tags=["query"])
    def sanctions(entity_id: str) -> dict[str, Any]:
        """Sanctioned parties with control over this entity, direct or not.

        Served from the precomputed closure, so the answer is a single indexed
        read regardless of how deep the chain runs. ``indirect_only`` is the
        finding the project exists to surface: the party is upstream but never
        appears on this company's own filing.
        """
        ent = _fetch_entity(entity_id)
        started = time.perf_counter()
        rows = con().execute(
            f"""
            SELECT se.risk_id, se.hops, se.min_percent, se.max_percent,
                   se.control_only, se.via_fiduciary, se.path_ids, {_ENTITY_COLS}
            FROM sanctions_exposure se
            JOIN entities e ON e.canonical_id = se.risk_id
            WHERE se.asset_id = ?
            ORDER BY se.hops ASC, se.max_percent DESC NULLS LAST
            """,
            [entity_id],
        ).fetchall()

        path_ids = {pid for r in rows for pid in (r[6] or [])}
        names: dict[str, str] = {}
        if path_ids:
            ph = ",".join("?" * len(path_ids))
            names = dict(
                con().execute(
                    f"SELECT canonical_id, name FROM entities WHERE canonical_id IN ({ph})",
                    list(path_ids),
                ).fetchall()
            )

        hits = []
        for r in rows:
            owner = _row_to_entity(r[7:19])
            hits.append(
                {
                    "owner": owner,
                    "hops": r[1],
                    "min_percent": r[2],
                    "max_percent": r[3],
                    "control_only": bool(r[4]),
                    "via_fiduciary": bool(r[5]),
                    "indirect_only": r[1] > 1,
                    "path": [{"id": pid, "name": names.get(pid, pid)} for pid in (r[6] or [])],
                }
            )
        return {
            "entity": ent,
            "count": len(hits),
            "has_exposure": bool(hits),
            "indirect_only": bool(hits) and all(h["hops"] > 1 for h in hits),
            "took_ms": round((time.perf_counter() - started) * 1000, 3),
            "hits": hits,
        }

    @app.get("/api/sanctions/exposed", tags=["query"])
    def exposed(
        limit: int = Query(50, ge=1, le=500),
        min_hops: int = Query(1, ge=1, le=MAX_HOPS),
    ) -> dict[str, Any]:
        """Companies under sanctioned control, most indirect first.

        ``min_hops=2`` is the interesting view: those are the companies whose
        own filings name nobody sanctioned.
        """
        rows = con().execute(
            f"""
            SELECT {_ENTITY_COLS}, min(se.hops) AS shortest, count(*) AS n_owners
            FROM sanctions_exposure se JOIN entities e ON e.canonical_id = se.asset_id
            WHERE se.hops >= ?
            GROUP BY {_ENTITY_COLS}
            ORDER BY shortest DESC, n_owners DESC, e.name
            LIMIT ?
            """,
            [min_hops, limit],
        ).fetchall()
        out = []
        for r in rows:
            ent = _row_to_entity(r)
            ent["shortest_hops"] = r[12]
            ent["sanctioned_owners"] = r[13]
            out.append(ent)
        return {"min_hops": min_hops, "count": len(out), "results": out}

    @app.get("/api/stats", tags=["ops"])
    def stats() -> dict[str, Any]:
        rows = con().execute(
            "SELECT hops, count(*) FROM sanctions_exposure GROUP BY 1 ORDER BY 1"
        ).fetchall()
        return {
            "index": state["meta"],
            "exposure_by_hops": {str(h): n for h, n in rows},
            "indirect_only_companies": con()
            .execute(
                """SELECT count(*) FROM (
                       SELECT asset_id FROM sanctions_exposure
                       GROUP BY asset_id HAVING min(hops) >= 2)"""
            )
            .fetchone()[0],
        }

    # -------------------------------------------------------------------- ui

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(_STATIC / "index.html")

    return app


app = create_app()
