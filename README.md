# Corporate Ownership Entity Resolution

A record-linkage pipeline that resolves beneficial-ownership records from **UK
Companies House** and **OpenSanctions** into canonical entities, then loads them
into a graph database so ownership and control can be traversed across
jurisdictions.

The question it answers: *given a UK company, who ultimately controls it — and
is any of them sanctioned?* That question is unanswerable from the raw
registers, because the controlling party almost never appears on the target
company's own filing, and because the same person is filed under a dozen
different spellings.

```
acquire → normalize → block → match → cluster → load → analyse → evaluate
```

[![CI](https://github.com/almirahalkina/corporate-ownership-entity-resolution/actions/workflows/ci.yml/badge.svg)](https://github.com/almirahalkina/corporate-ownership-entity-resolution/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.10%20|%203.11%20|%203.12-blue)
![License](https://img.shields.io/badge/license-MIT-green)

---

## Run it in two minutes

No downloads, no Docker, no API keys. A synthetic corpus in the exact wire
format of both real sources is committed to the repository.

```bash
git clone https://github.com/almirahalkina/corporate-ownership-entity-resolution
cd corporate-ownership-entity-resolution
make install
make demo
```

`make demo` parses both sources, generates candidate pairs, scores them,
clusters them, splits contradictions, traverses the resulting ownership graph,
and scores itself against ground truth. It takes about 30 seconds.

To run against the real registers (~4GB, 20–60 min):

```bash
make data        # download Companies House + OpenSanctions snapshots
make run         # full pipeline
make neo4j-up    # optional: graph database for interactive traversal
make load-graph
```

---

## The problem

Three facts about the source data drive every design decision here.

**One person appears many times, spelled differently each time.** A beneficial
owner of eleven companies files eleven separate PSC records. Across those,
titles vary, middle names come and go, and non-Anglophone names are
transliterated by whichever scheme the filer used — `Yevgeniy`, `Evgeny`,
`Evgenii`, `Eugene`. Nothing in the register links them.

**Control is deliberately indirect.** A UK company's PSC filing names its
immediate controller, which is frequently another company — often registered in
a jurisdiction that publishes nothing. Following control to a natural person
means chaining filings together, and each hop requires matching a *name typed
by a filer* against a *company on the register*.

**Sanctions data lives in a separate universe.** OpenSanctions publishes
sanctioned persons and entities with their own aliases and identifiers, sharing
no key with Companies House. Connecting the two is a matching problem, and it is
where the risk signal actually comes from.

Entity resolution is what turns three disconnected piles of records into one
traversable graph.

---

## Results on the fixture corpus

Measured by `make demo` on 1,200 companies, 400 distinct people corrupted into
964 filings, and 140 sanctions entities. Full method in
[`docs/evaluation.md`](docs/evaluation.md).

| Metric | Value | What it measures |
|---|---|---|
| **B-cubed F1** | **0.989** | Cluster quality, weighting each record equally |
| B-cubed precision | 0.995 | Share of each cluster that belongs there |
| B-cubed recall | 0.984 | Share of each true entity that got collected |
| Pairwise precision | 0.980 | Accepted pairs that are true matches |
| Pairwise recall | 0.921 | True pairs recovered end to end |
| Recall within candidates | **1.000** | Of pairs blocking offered, the matcher missed none |
| Blocking reduction ratio | 0.9993 | Quadratic space eliminated |
| Held-out registration recall | **1.000** | Real-data link recovery with the identifier hidden |

The two bolded diagnostics matter more than the headline. **Recall within
candidates is 1.000, so every single miss is lost at blocking, not matching** —
the matcher is not the bottleneck and tuning it further would be wasted effort.
That finding is what drove adding the `p_first_dob` blocking key, which lifted
blocking recall from 0.840 to 0.925.

**Held-out registration recall** is the number to trust most. Corporate PSC
filings often state the parent's UK company number, which is a
near-deterministic link. Hiding it and asking the matcher to recover the link
from name and address alone yields labelled pairs on *real* data that nobody
chose — no annotator, no corruption model, no way to fool yourself.

---

## How it works

### 1. Normalisation

Both sources are parsed into one flat record schema, with names, addresses,
countries and control statements normalised. Details in
[`docs/data-dictionary.md`](docs/data-dictionary.md).

Two decisions here do disproportionate work:

- **Legal forms are stripped in either position.** `OOO Severstal` and
  `Severstal OOO` are the same company; a suffix-only strip leaves them in
  different blocks.
- **PSC "statements" are counted, never parsed as entities.** Around 5% of the
  register consists of declarations that *no* controller exists. Parsing those
  as people would inject millions of phantom entities with near-identical
  names — and the absence of a declared owner is itself a finding worth
  counting.

### 2. Blocking

Comparing all pairs is quadratic: 12M PSC filings is 7.4 × 10¹³ pairs. Blocking
replaces that with a union of cheap equality joins on derived keys, achieving a
**0.9993 reduction ratio**.

Six person keys and five company keys are unioned, because each has an
independent blind spot — a name-fingerprint key misses transliteration variants,
a phonetic key misses name-order swaps, a birth-year key misses records where
the registrar withheld it. Every key's pair completeness and cost is reported
per run, so key selection is an evidenced decision rather than folklore.

Blocking recall is a hard ceiling on end-to-end recall: a pair never generated
cannot be recovered by any downstream model.

### 3. Matching

Three interchangeable matchers behind one interface:

| Matcher | Method | Role |
|---|---|---|
| `rules` | Additive log-odds over interpretable features | Default. Auditable — every decision carries a per-feature breakdown |
| `splink` | Fellegi–Sunter + EM ([Splink 4](https://github.com/moj-analytical-services/splink)) | Learns weights from data, including term-frequency adjustment |
| `llm` | Claude adjudication of the uncertain band only | Tests whether an LLM beats the deterministic matcher on hard cases |

Every feature that can be unobserved returns `NULL`, not `0`. "Both records
state a nationality and they differ" is strong evidence against a match; "one
record omits nationality" is no evidence at all. Collapsing the two teaches the
model that missing data implies non-match — and since missingness is not random,
that bias lands squarely on the cross-border records the pipeline exists to
resolve.

The LLM adjudicator only ever sees pairs scored between the reject and accept
thresholds — about 5% of pairs here. That bounds its cost, bounds its blast
radius, and makes the measured delta attributable to it alone. This follows
OpenSanctions' *OpenSanctions Pairs* benchmark (2026), which found off-the-shelf
LLMs outperforming their production rule-based matcher on 755k labelled pairs —
a result worth testing rather than assuming.

### 4. Clustering and conflict splitting

Transitive closure over accepted pairs, then **conflict-aware splitting**.

Closure alone is unforgiving: one false-positive edge merges two unrelated
components permanently, and merges chain. On a register with thousands of
plausible "Mr J Smith, British" records, naive closure reliably produces one
enormous cluster containing hundreds of distinct people.

The fix uses attributes that are mutually exclusive *by construction* — one
person has one birth year; one company has one registration number per
jurisdiction. Components containing a contradiction are re-clustered by adding
edges strongest-first and refusing any merge that would place two incompatible
values together. This is a constrained variant of correlation clustering.

Tolerances are explicit rather than binary: nationality permits two distinct
values, because dual citizenship is common and filers pick whichever passport
was to hand — but three is over-merging.

### 5. Graph load and traversal

Resolved entities and re-pointed control edges load into **Neo4j**. Resolution
must happen first: loading raw filings and deduplicating in Cypher would leave
one owner as eleven nodes, and every traversal would stop at the first hop.

Cypher queries cover beneficial-owner traversal, indirect control paths,
circular ownership, sanctions exposure propagation, and opacity analysis.

Ownership is propagated as an **interval**, never a point estimate. Companies
House publishes bands (`25-50%`), so a two-hop chain of `25-50%` through
`50-75%` yields `12.5-37.5%`. Where a hop asserts control without a percentage —
the right to appoint directors — percentage arithmetic is abandoned and the path
is flagged as control-bearing instead. A person with no shares but the power to
appoint the board controls the company completely, and any analysis ranking by
percentage alone would miss them. That is not a hypothetical structure; it is
the one you would choose to stay off a screening tool.

The same analyses also run as **recursive SQL in DuckDB**, so every published
figure reproduces from a clean clone with no database at all. Two independent
implementations of transitive control also cross-check each other.

---

## Design decisions

Full reasoning in [`docs/design-decisions.md`](docs/design-decisions.md).

| Decision | Choice | Why |
|---|---|---|
| Compute engine | DuckDB, not Spark | 12M records and ~10⁸ candidate pairs run on one laptop in minutes. Spark's threshold is ~10⁹ pairs; blocking keys are materialised as a table so they are already the partition keys if that day comes |
| Bulk loading | Arrow, not `executemany` | Measured 60 rows/sec vs ~4 orders of magnitude faster. The difference between runnable at national scale and not |
| Phonetic key | Soundex, not Metaphone | Measured: Soundex collapses 6/6 transliteration doublets in this data, Metaphone 3/6 |
| Primary keys | None on hot tables | DuckDB's ART index costs ingestion throughput and resident memory at 12M rows; uniqueness is guaranteed upstream and asserted in tests |
| FollowTheMoney | Emitted, not depended on | `followthemoney` needs PyICU/libicu, which most people cannot install without friction. Schema conformance is enforced in CI instead |
| Interpretability | Every pair carries a rationale | "The gradient boosting said so" does not survive a compliance audit |

---

## Repository layout

```
src/ownership_er/
  acquire.py          Downloads with SHA-256 manifests — these sources are mutable
  normalize/          Names, addresses, countries, control statements
  sources/            Companies House and OpenSanctions parsers
  block.py            Candidate generation + per-key completeness reporting
  match/              Rules, Splink, LLM adjudicator behind one interface
  cluster.py          Union-find + conflict-aware splitting
  graph/              Neo4j loader and Cypher traversal library
  analysis.py         Ownership analyses as recursive SQL
  evaluate.py         Pairwise, B-cubed, sweeps, ablations, error taxonomy
  fixtures.py         Synthetic corpus generator with known ground truth
docs/                 Architecture, design decisions, evaluation, findings
orchestration/        Airflow DAG mirroring the CLI stages
tests/                110 tests, no network or database required
```

## Commands

```bash
make demo             # full pipeline on fixtures (~30s)
make check            # lint, type-check, test
make evaluate         # score every matcher against ground truth
make analyse          # ownership, exposure and opacity analyses
make neo4j-up         # start Neo4j in Docker
make load-graph       # load the resolved graph
make export           # export canonical entities as FollowTheMoney JSON
oer --help            # all stages individually
```

## Data sources

| Source | Contents | Licence |
|---|---|---|
| [Companies House PSC snapshot](https://download.companieshouse.gov.uk/en_pscdata.html) | ~12M people-with-significant-control filings, daily | Open Government Licence v3.0 |
| [Companies House company data](https://download.companieshouse.gov.uk/en_output.html) | ~5.6M registered companies, monthly | Open Government Licence v3.0 |
| [OpenSanctions default dataset](https://www.opensanctions.org/datasets/default/) | Consolidated sanctions, PEPs, entities of interest | CC-BY-NC 4.0 (commercial use requires a licence) |

Snapshots are mutable — Companies House replaces the PSC file every morning. All
downloads are recorded with URL, date, size and SHA-256, because a figure
reported without a snapshot date is not reproducible.

## Limitations

- **Blocking is the binding constraint**, not matching. Recall within candidates
  is 1.000, so further matcher tuning buys nothing; the remaining misses need
  new keys.
- **Splink is undertrained at fixture scale** (F1 0.915 vs the rule matcher's
  0.949). EM cannot estimate `m` reliably from 1,107 records and emits explicit
  warnings that several comparison levels were never observed. It reaches
  precision 1.000 with zero over-merged records and loses on recall — the two
  matchers fail in opposite directions. The comparison is only meaningful at
  full national scale, and [`docs/evaluation.md`](docs/evaluation.md) says so
  rather than quietly reporting the flattering number.
- **Fixture metrics are not real-data metrics.** The corruption model is a
  hypothesis about how the register is messy. The held-out registration
  evaluation exists precisely because it does not share that assumption.
- **Foreign registration numbers are not normalised** across jurisdictions,
  since their formats are not comparable.
- **The graph is a snapshot.** Historical ownership requires the change
  timeline, which the free products do not provide.

## Licence

MIT. Source data carries its own terms — see the table above.
