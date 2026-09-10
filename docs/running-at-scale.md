# Running the full national pipeline

The demo runs on committed fixtures with no downloads. This is the guide for the
real thing: ~5.6M companies, ~12M PSC filings, and the full OpenSanctions
dataset.

---

## Requirements

| Resource | Minimum | Comfortable |
|---|---|---|
| Disk | 25 GB free | 40 GB |
| RAM | 8 GB | 16 GB |
| Cores | 4 | 8 |
| Time | ~2 hours | ~45 min |

Disk breaks down as roughly 4 GB of compressed downloads, 12 GB of DuckDB
warehouse at peak, and headroom for the candidate-pair join to spill. The
warehouse can be deleted afterwards — every stage is reproducible.

Neo4j is optional. All analyses run in DuckDB; the graph is for interactive
exploration.

---

## Step by step

### 1. Install

```bash
git clone https://github.com/almirahalkina/corporate-ownership-entity-resolution
cd corporate-ownership-entity-resolution
make install
```

### 2. Configure for scale

The defaults are sized for a laptop running the demo. Raise them:

```bash
cp .env.example .env
```

```ini
OER_RUNTIME_DUCKDB_MEMORY_LIMIT=8GB     # ~half your RAM
OER_RUNTIME_DUCKDB_THREADS=8            # your core count
```

Setting the memory limit above physical RAM is worse than setting it too low —
DuckDB spills to disk gracefully but swaps catastrophically.

### 3. Download

```bash
make data
```

Fetches the most recent PSC snapshot (resolved by probing, since Companies House
keeps only a short window), the current month's company product, and the
OpenSanctions default dataset. Streams to disk, verifies against
`Content-Length`, and records URL, date, size and SHA-256 in
`data/raw/manifest.json`.

**Pin the snapshot if you intend to publish anything from this run.** These
sources are mutable — the PSC file is replaced every morning:

```ini
OER_SOURCE_SNAPSHOT_DATE=2026-08-04
```

Resumable: re-running skips existing files unless `--force`. A partial download
is written to `.part` and renamed only on success, so an interrupted transfer is
never mistaken for a complete one.

### 4. Run

```bash
make run
```

Or stage by stage, which is what you want the first time:

```bash
oer normalize --no-fixtures      # ~15-25 min, the longest stage
oer block                        # ~10-20 min
oer match --matcher rules        # ~15-30 min
oer cluster --matcher rules      # ~5-10 min
oer analyse                      # ~5-15 min
```

Every stage is independently re-runnable against the warehouse, so a matcher
change re-runs two stages in minutes rather than re-parsing 12M JSON lines.

### 5. Sanity checks before trusting anything

```bash
# Statement rows should be ~5% of PSC and must not appear as entities
oer normalize --no-fixtures | grep -A5 ch_psc_kinds

# Blocking: reduction ratio should exceed 0.999, oversized blocks should be few
cat outputs/eval/blocking_report.json | python -m json.tool | head -40

# The single most valuable manual check
python - <<'PY'
import json
from ownership_er.warehouse import connect
from ownership_er import analysis
with connect() as con:
    for row in analysis.hub_entities(con, min_controlled=50, limit=30):
        print(row["companies_controlled"], row["source_records"], row["entity_name"])
PY
```

**Read that last list carefully.** Entities controlling implausibly many
companies are genuine holding groups, nominee directors, or over-merged
clusters. At national scale there are no labels, so this is the cheapest
available check on cluster quality — and an over-merged giant corrupts every
downstream figure.

### 6. Evaluate on real data

```bash
oer evaluate --truth fixtures/ground_truth.json --matcher rules
```

The synthetic metrics do not apply to real data, but
`held_out_registration` does — it uses corporate PSC filings that state a UK
company number as labels. **This is the number to quote.**

### 7. Optional: the graph

```bash
make neo4j-up
make load-graph          # ~20-40 min for ~6M nodes / ~10M edges
```

Browser at <http://localhost:7474>, credentials `neo4j` /
`ownership-dev-password`.

Sample traversal:

```cypher
MATCH path = (owner:Person)-[:CONTROLS*1..6]->(target:Company)
WHERE target.reg_number = '01234567'
  AND NOT EXISTS { MATCH (:Entity)-[:CONTROLS]->(owner) }
RETURN owner.name, owner.is_sanctioned, length(path) AS hops
ORDER BY hops;
```

### 8. Optional: LLM adjudication

```bash
echo 'ANTHROPIC_API_KEY=sk-ant-...' >> .env
oer match --matcher llm
```

Only the uncertain band is sent (~5% of pairs). Estimate cost first:

```bash
python - <<'PY'
from ownership_er.warehouse import connect
with connect() as con:
    n = con.execute(
        "SELECT count(*) FROM pair_scores WHERE matcher='rules' AND decision='uncertain'"
    ).fetchone()[0]
    print(f"{n:,} pairs, ~{n * 450 / 1e6 * 3:.2f} USD input + output")
PY
```

Cap it with `OER_MATCH_LLM_MAX_PAIRS`. Verdicts are cached, so re-runs are free.
A guardrail refuses to run if the band exceeds 35% of pairs — that means
misconfiguration, not genuine ambiguity.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Out of Memory Error` in DuckDB | Limit above physical RAM | Lower `OER_RUNTIME_DUCKDB_MEMORY_LIMIT` |
| `block` runs for hours | A degenerate blocking key | Check `oversized_blocks` in the report; lower that key's `max_block_size` |
| Huge clusters after `cluster` | Over-merging | Raise `OER_MATCH_AUTO_ACCEPT`; verify conflict splitting is on |
| `No PSC snapshot found` | Snapshot window rolled over | Set `OER_SOURCE_SNAPSHOT_DATE` explicitly |
| Neo4j load times out | Batch too large for heap | Lower `OER_NEO4J_BATCH_SIZE`; raise the container heap |
| `pip install '.[ftm]'` fails on ICU | Missing system libicu | `brew install icu4c pkg-config` / `apt-get install libicu-dev`. Optional — the pipeline runs without it |

---

## Recording the run

For anything you intend to publish or put on a CV, record:

1. Snapshot dates and SHA-256 from `data/raw/manifest.json`
2. Git commit of the pipeline
3. `outputs/eval/evaluation_rules.json` and `blocking_report.json`
4. `outputs/analysis/analysis.json`
5. Wall time and machine specs

Without the snapshot date, a figure from this pipeline is not reproducible —
the underlying register will have changed by the next morning.
