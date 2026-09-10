# Design decisions

Each entry states the decision, the alternative, the reasoning, and — where one
exists — the measurement that settled it.

---

## 1. DuckDB, not Spark

**Alternative:** PySpark, which is what Sayari, Palantir and most commercial
entity-resolution stacks run in production.

**Decision:** single-node DuckDB.

The working set is ~5.6M companies and ~12M PSC filings. That is large enough
that pandas cannot hold the candidate-pair join, and small enough that a
distributed engine is pure overhead: no cluster, no serialisation boundary, no
scheduler, no driver/executor memory tuning.

The threshold worth naming is the **candidate-pair join, not the record count**.
Blocking here emits ~10⁸ pairs, which DuckDB handles out-of-core by spilling to
disk. Past roughly 10⁹ pairs — reached at ~100M input records, or by loosening
blocking substantially — single-node stops being right and the join must be
partitioned across machines.

The migration path is deliberately short. Blocking keys are **materialised as a
table** rather than computed inline in the join, so the partitioning strategy is
already expressed: `blocking_keys.key_value` is exactly what a Spark rewrite
would shuffle on. Every stage is SQL over tables with no Python in the hot path,
so the port is mechanical.

Choosing Spark here would have been resume-driven design — a slower pipeline
that nobody could run from a clone.

---

## 2. Arrow bulk insert, not `executemany`

**Measured, not assumed.** The initial implementation used
`con.executemany(INSERT ...)` over lists of tuples. On this 30-column schema
that measured **~60 rows/sec** — about four days for 12M PSC records. DuckDB's
prepared-statement path pays per-row interpreter and constraint-check overhead
that dominates completely at this width.

Replacing it with a columnar Arrow table registered as a view and inserted with
`INSERT INTO ... SELECT` took the same data to **1.6 seconds for 2,656
records** — roughly four orders of magnitude. This is the difference between the
pipeline being runnable at national scale and not.

Type handling is explicit rather than inferred: an Arrow schema pins every
column, and date columns use `TRY_CAST` so a malformed date in one filing nulls
that field instead of aborting a 50,000-row batch.

---

## 3. No primary keys on hot tables

**Alternative:** `record_id VARCHAR PRIMARY KEY`, which is the obvious default.

DuckDB backs a primary key with an ART index maintained on **every insert**. At
12M wide rows that costs both ingestion throughput and a large resident memory
footprint — on a laptop, the difference between finishing and thrashing.

Uniqueness is guaranteed upstream instead. Record ids are derived from
registry-assigned identifiers: `links.self` for PSC filings, company number for
companies, OpenSanctions id for sanctions entities. The invariant is asserted in
`tests/test_pipeline.py::test_record_ids_are_unique` rather than enforced by the
engine — a check that runs in CI costs nothing at load time.

Join indexes are created *after* bulk loading, since maintaining an index during
ingestion costs more than building it once at the end.

---

## 4. Soundex, not Metaphone

**Measured.** Metaphone is the usual default and is generally the better
algorithm. On *this* data it is the wrong choice:

| Pair | Metaphone | Soundex |
|---|---|---|
| Kowalczyk / Kowalchyk | ✗ `KWLKSK` / `KWLXK` | ✓ `K422` |
| Shevchenko / Schevchenko | ✗ `XFXNK` / `SXFXNK` | ✓ `S125` |
| Abramovich / Abramovic | ✗ `ABRMFX` / `ABRMFK` | ✓ `A165` |
| Petrov / Petroff | ✓ | ✓ |
| Sokolov / Sokoloff | ✓ | ✓ |
| Zielinski / Zielinsky | ✓ | ✓ |

Soundex collapses 6/6; Metaphone 3/6, splitting exactly the Slavic consonant
clusters that dominate transliteration variance in this register.

Soundex is coarser and collides more. That is acceptable in a **blocking** key,
where recall is the binding constraint and precision is the matcher's job — and
the cost is bounded anyway, because every phonetic key is paired with birth year
rather than used alone.

---

## 5. FollowTheMoney emitted, not depended on

**Alternative:** depend on `followthemoney` and use its model directly.

FtM is the interchange schema of this ecosystem — OpenSanctions, OCCRP Aleph,
and most open corporate-intelligence tooling. Emitting it is what makes this
pipeline's output loadable by other people's tools.

But `followthemoney` requires PyICU, which requires a system libicu. That is
awkward on macOS (`brew install icu4c` plus environment variables), absent from
many CI images, and — verified during development — simply unavailable in
restricted environments. A hard dependency would mean most people who clone this
repository cannot run it.

So the core package emits schema-conformant FtM JSON directly, and
`oer validate-ftm` round-trips every emitted entity through the real library
whenever the optional `[ftm]` extra is installed. A dedicated CI job installs
libicu and runs that validation, so the conformance guarantee is **enforced
rather than asserted**.

The same reasoning applies to ICU transliteration: used when available, with a
dependency-free Cyrillic fallback otherwise.

---

## 6. Three-valued feature logic

Every comparison feature returns `NULL` — not `0` — when either side is missing.

The distinction is load-bearing. "Both records state a nationality and they
differ" is strong evidence *against* a match. "One record omits nationality" is
no evidence either way. Collapsing both to zero teaches the model that missing
data implies non-match.

That would be merely imprecise if missingness were random. It is not: foreign
filers omit different fields than UK ones, and PSC records for individuals
resident abroad are systematically sparser. The bias would therefore land
squarely on the cross-border records the pipeline exists to resolve.

The rule matcher skips `NULL` terms entirely, so a sparse record accumulates
less evidence in either direction and lands nearer the prior — the honest
position. Splink models missingness as its own comparison level.

---

## 7. Two thresholds, not one

A single threshold forces every borderline pair into a confident answer. Two
thresholds preserve the third option: *this needs more than string similarity to
settle*.

That band is what the LLM adjudicator consumes, and what a human review queue
would consume in production. In a compliance setting it is arguably the most
useful output — knowing which decisions are shaky is worth more than a
marginally better aggregate score.

Operating point 0.92 / 0.62 was chosen from the threshold sweep, **not** at the
F1 maximum (0.975). The costs are asymmetric: a missed sanctions link is a
compliance failure; a false positive is a pair an analyst discards in seconds.
Optimising F1 treats those as equal. The chosen band also holds the uncertain
region at ~5% of pairs, bounding adjudication cost.

---

## 8. Conflict-aware splitting, not plain transitive closure

Closure is the standard first move and is right in spirit: if A matches B and B
matches C, they are one entity. But it is unforgiving — a single false-positive
edge merges two unrelated components permanently, and merges chain.

On a register containing thousands of plausible "Mr J Smith, British" records,
naive closure reliably produces one enormous cluster containing hundreds of
distinct people. That is severe enough that the system is worse than no system,
because the resulting entity is confidently wrong.

The fix uses attributes that are mutually exclusive **by construction** — one
person has one birth year; one company has one registration number per
jurisdiction. These are not similarity signals to be weighed; they make a
proposed cluster impossible regardless of edge scores.

Components containing a contradiction are re-clustered greedily, strongest edge
first, refusing merges that would violate a constraint. This is a constrained
variant of correlation clustering; the exact problem is NP-hard and greedy is
the standard approximation.

Tolerances are explicit, not binary: nationality allows two distinct values
(dual citizenship is common), but three is over-merging.

Measured effect: B-cubed precision 0.9725 → 0.9946, largest cluster 16 → 12
(the true maximum), recall unchanged.

---

## 9. Percentages propagated as intervals

Companies House publishes ownership as **bands** (`25-50%`), not point
estimates. Multiplying band midpoints down a chain produces a single
confident-looking number the source does not support.

Both bounds are multiplied instead, so a two-hop chain of `25-50%` through
`50-75%` yields `12.5-37.5%`.

Where a hop asserts control **without** a percentage — right to appoint and
remove directors, significant influence — percentage arithmetic is abandoned
entirely and the path is flagged as control-bearing. Multiplying an unknown by
anything is not a number.

This matters beyond pedantry. A person holding no shares but the right to
appoint the board controls the company completely. Any analysis ranking by
percentage alone misses them — and that is precisely the structure a party
wanting to stay off a screening tool would choose.

---

## 10. Analyses duplicated in SQL and Cypher

The ownership analyses exist twice: as Cypher in `graph/queries.py` and as
recursive CTEs in `analysis.py`.

Neo4j is right for **interactive** investigation — an analyst following a chain,
pivoting, asking the next question. But requiring a running database to
reproduce a published figure is a reproducibility problem: a reviewer needs
Docker, a server and a load step before verifying a single number.

Running the batch analysis as SQL against the DuckDB file already in the repo
means `make analyse` reproduces every finding from a clean clone with no
infrastructure, and the graph load becomes an optional convenience.

The duplication also buys a correctness check: two independent implementations
of transitive control should agree, and a parity test asserts they do.

---

## 11. Statements parsed as absence, not as entities

Roughly 5% of PSC records are not controlling parties at all. They are
*statements*: "the company knows of no person with significant control", "steps
to identify have not been completed", or a super-secure record where details are
withheld for personal safety.

Parsing those as entities would inject millions of phantom people with
near-identical names directly into the resolution set — the worst possible input
to a clustering algorithm.

They are counted instead, and the count is a finding in its own right: it is the
share of the register that declares no identifiable owner.
`tests/test_pipeline.py::test_statements_are_counted_not_parsed_as_entities`
pins the invariant.

---

## 12. Interpretability as a requirement

Every scored pair carries a per-feature rationale, emitted for rejected pairs as
well as accepted ones:

```
name_fp_jw=0.84(+1.68), last_jw=1.00(+1.40), birth_year_match=1.00(+2.20),
nationality_match=0.00(-1.50)
```

This costs storage and some throughput. It is kept because in a compliance
context an analyst has to answer *why did the system merge these two people?*,
and "the gradient boosting said so" does not survive an audit. Reconstructing an
explanation after the fact from stored features is exactly the friction that
stops people auditing at all.

The same reasoning drives the choice of a plain additive log-odds model over a
tree ensemble, and a hand-rolled logistic calibration over scikit-learn: the
fitted model stays **identical in structure** to the hand-weighted one, so the
two can be compared coefficient by coefficient and a surprising fitted weight
can be interrogated.

---

## 13. Ground truth from three independent sources

Covered in [`evaluation.md`](evaluation.md), but the decision belongs here:
synthetic corruption is the only source that measures recall honestly, held-out
registration numbers are the only source measured on the real distribution with
labels nobody chose, and a hand-labelled sample is the only source that catches
errors the other two share.

Reporting one of them alone would be reporting a number that cannot be wrong in
the ways that matter.

---

## Rejected alternatives

| Considered | Rejected because |
|---|---|
| `dedupe` library | Requires interactive labelling; poor fit for a reproducible batch pipeline |
| `nomenklatura` matcher directly | Depends on `followthemoney` → PyICU. Its *approach* is followed; swapping it in via the `[ftm]` extra is a small change |
| Neo4j GDS for clustering | Adds a database dependency to a stage that runs fine in-process, and blocks the no-infrastructure reproduction path |
| Embedding-based blocking | Attractive for multilingual names, but needs a model, a vector index, and GPU throughput at 12M records — for a gain the measured error taxonomy does not currently justify. Revisit once character n-gram keys are exhausted |
| Airflow as the primary interface | The CLI is the interface; the DAG calls it. A DAG that reimplements stages drifts from them, and the drift surfaces in production |
