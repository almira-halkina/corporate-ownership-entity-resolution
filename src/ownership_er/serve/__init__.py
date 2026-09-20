"""Online serving layer over the resolved ownership graph.

The pipeline in :mod:`ownership_er.pipeline` is batch: it reads whole sources,
emits candidate pairs, scores, clusters, and writes a warehouse. None of that
can sit in a request path — a full run touches every record, and the candidate
join alone is quadratic in the blocking key.

So the serving layer splits in two, which is the standard batch/serving
separation:

``build_serving_index``
    Runs once, offline, after the pipeline. Flattens the warehouse into a small
    read-only artifact: canonical entities with risk flags folded in, control
    edges re-pointed onto canonical ids, a denormalised name index for lookup,
    and the *precomputed* sanctions closure — the one traversal expensive
    enough that computing it per request would dominate tail latency.

``api``
    Reads that artifact and nothing else. No matcher, no clustering, no source
    parsing at request time. A query is an indexed lookup plus a bounded
    recursive walk over at most a few thousand edges.

The artifact is a single DuckDB file, so the whole service is one container
with no external database. That is a deliberate choice for a corpus this size:
Neo4j remains the right answer at national scale and the loader in
:mod:`ownership_er.graph` still targets it, but requiring a graph server to
answer questions about 1,786 entities would be infrastructure theatre.
"""

from __future__ import annotations

from typing import Any

__all__ = ["build_serving_index"]


def __getattr__(name: str) -> Any:  # pragma: no cover - thin lazy re-export
    if name == "build_serving_index":
        from ownership_er.serve.index import build_serving_index

        return build_serving_index
    raise AttributeError(name)
