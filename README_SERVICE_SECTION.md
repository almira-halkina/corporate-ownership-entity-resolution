## Live service

The pipeline above is batch. Running it per request is impossible — the
candidate join alone touches every record — so the repository also ships an
online path, and the split between the two is the interesting part.

**`oer build-index`** flattens the warehouse into a read-only artifact:
canonical entities with risk flags resolved, control edges re-pointed onto
canonical ids, a denormalised name index, and the *precomputed* sanctions
closure. **`oer serve`** reads that artifact and nothing else — no matcher, no
clusterer, no source parser is importable from the request path, which is what
keeps tail latency flat.

The artifact is a single 1786-entity DuckDB file baked into the
image, so the whole service is one container with no external database. Neo4j
remains the right answer at national scale and `ownership_er.graph` still
targets it; requiring a graph server to answer questions about
1786 entities would be infrastructure theatre. Both backends reuse
the same `canonical_nodes_sql()` and `resolved_edges_sql()`, so they cannot
drift, and a test asserts the precomputed closure agrees with a live traversal.

```bash
oer normalize --fixtures && oer block && oer match && oer cluster
oer build-index        # 1786 entities, 1228 edges, 95 exposure links
oer serve --port 8080  # http://127.0.0.1:8080
```

Or `docker compose up service`, which builds the index during the image build
and needs nothing else running.

### API

| Endpoint | Answers |
|---|---|
| `GET /api/search?q=` | Name to candidate entities, ranked exact → prefix → contains, matching on any alias |
| `GET /api/entity/{id}` | Canonical entity, alias set, edge counts |
| `GET /api/entity/{id}/ownership?direction=up\|down` | Bounded control walk, percentages as intervals |
| `GET /api/entity/{id}/sanctions` | Sanctioned parties upstream, with the chain that connects them |
| `GET /api/sanctions/exposed?min_hops=2` | Companies whose own filings name nobody sanctioned |
| `GET /health`, `GET /metrics` | Index provenance; Prometheus-format counters and latency quantiles |

`min_hops=2` is the view the project exists to produce. On the fixture corpus
**12 companies are controlled by a sanctioned party
that never appears on their own filing** — invisible without resolution,
because the party files under a different spelling one or more hops up.

### Measured

Single container, 2 shared vCPUs, fixture corpus of 1786 entities
and 1228 control edges. Reproduce with `python bench/sweep.py`.

| Concurrency | Throughput (rps) | p50 (ms) | p95 (ms) | p99 (ms) | Errors |
|---|---|---|---|---|---|
| 1 | 131 | 7.8 | 10.1 | 11.4 | 0 |
| 4 | 205 | 19.0 | 30.7 | 36.2 | 0 |
| 16 | 226 | 68.5 | 111.2 | 130.2 | 0 |
| 32 | 218 | 139.3 | 237.9 | 304.9 | 0 |

Throughput plateaus around 226 rps and
latency grows linearly past roughly 4 concurrent requests, which is where two
cores saturate. That knee is why `fly.toml` sets a soft concurrency limit of 8:
past it the machine should queue rather than accept work it cannot start.

By endpoint, at 16 concurrent clients:

| Endpoint | Requests | p50 (ms) | p95 (ms) | p99 (ms) |
|---|---|---|---|---|
| `search` | 2479 | 69.9 | 113.5 | 139.9 |
| `ownership` | 1140 | 69.3 | 117.0 | 143.8 |
| `sanctions` | 635 | 66.8 | 103.4 | 128.7 |
| `entity` | 229 | 56.0 | 96.8 | 113.1 |

Cold start, process launch to first HTTP 200 on a real query:
**708 ms** (runs: 757, 708, 674 ms).
Index build: **84 ms**. That cold start is
why the deployment scales to zero between visits.

### What the load test found

The first run at 16 concurrent clients returned a **19% error rate**
(`bench/results_before_fix.json`), with exceptions that read like data
corruption rather than a concurrency fault — `'float' object is not iterable`,
`tuple index out of range`. The cause was one shared DuckDB connection across
FastAPI's thread pool: two threads calling `execute` on the same handle
interleave and read each other's result sets. Fixed with one lazily-created
cursor per worker thread.

| | Before | After |
|---|---|---|
| Errors in 20s at c=16 | 593 / 3115 (19%) | **0 / 4483** |
| Throughput | 155 rps | **224 rps** |
| p99 | 313 ms | **138 ms** |

No functional test catches this, because it does not occur below two
concurrent requests. The load test now runs in CI on every push and the build
fails on a single error under load.
