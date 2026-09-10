# Evaluation

Reproduce everything here with:

```bash
make demo
make evaluate
```

Raw output lands in `outputs/eval/`.

---

## 1. Where ground truth comes from

Entity resolution has no natural labels, and how a project obtains them
determines whether its numbers mean anything. Three independent sources are
used, because each is blind to different errors.

### Synthetic corruption — the only honest measure of recall

`fixtures.py` generates entities and corrupts them into multiple records by a
known process, so the true partition is known by construction.

This is the only source that measures recall honestly. Hand-labelling can only
tell you about pairs you looked at, and you never look at the true pairs
blocking silently dropped — so a recall figure from a hand-labelled sample is
really a recall figure conditional on having been sampled, which is not the
quantity anyone wants.

Its weakness is the mirror image: the corruption model is a *hypothesis* about
how the register is messy. Good scores prove the matcher handles the modelled
variation, and nothing more.

Corruptions applied, each observed in real PSC filings:

| Corruption | Rate | Example |
|---|---|---|
| `title_change` | 75% | `Whitfield` → `Dr Whitfield` |
| `middle_name_dropped` | 45% | `James A Whitfield` → `James Whitfield` |
| `transliteration` | 40%/name part | `Yevgeniy` → `Evgeny`, `Petrov` → `Petroff` |
| `diacritic_stripped` | 55% | `Zieliński` → `Zielinski` |
| `address_reformat` | 30% | `High Street` → `High St` |
| `initial_only` | 25% | `Andrew` → `A` |
| `typo` | 18% | adjacent-character transposition |
| `case_change` | 12% | `Whitfield` → `WHITFIELD` |
| `name_order_swap` | 10% | forename and surname entered in opposite fields |
| `nationality_variant` | varies | `British` / `BRITISH` / `United Kingdom` / `English` |
| date of birth absent | 5% | field omitted entirely |
| `name_elements` absent | 15% | forces display-name parsing |

### Held-out registration numbers — real data, labels nobody chose

Corporate PSC filings frequently state the parent's UK company number, which is
a near-deterministic link to the company product. Removing it from the features
and asking the matcher to recover the link from name and address alone yields
labelled pairs on the genuine distribution, at zero annotation cost.

**This is the strongest evidence in the project.** Real noise, real
distribution, no annotator, and no opportunity to fool oneself — the labels
existed before anyone thought to evaluate against them.

### Hand-labelled sample

A stratified sample across the score range. Small, and the only source that
catches errors the other two share.

---

## 2. Metrics, and why both are reported

**Pairwise** precision/recall is directly interpretable but misleading for
clustering: merging two clusters of 50 costs 2,500 pairwise errors from a single
bad edge, so pairwise scores are dominated by whatever happened to the largest
clusters.

**B-cubed** weights every *record* equally instead. For each record, precision
is the share of its predicted cluster that shares its true label; recall is the
share of its true cluster that landed in its predicted cluster. This matches
what an analyst experiences — they look up one entity at a time and care whether
*that* entity is right.

Both are reported. Where they disagree, B-cubed is the one to believe for
cluster quality.

---

## 3. Headline results

Fixture corpus: 1,200 companies, 400 distinct people → 964 PSC filings, 140
sanctions entities, 1,318 company records. Matcher: `rules`. Thresholds:
accept ≥ 0.92, reject < 0.62.

### Persons

| Metric | Value |
|---|---|
| Pairwise precision | 0.9795 |
| Pairwise recall | 0.9213 |
| Pairwise F1 | 0.9495 |
| **Recall within candidates** | **1.0000** |
| B-cubed precision | 0.9946 |
| B-cubed recall | 0.9841 |
| **B-cubed F1** | **0.9893** |
| True clusters / predicted | 400 / 410 |
| Largest true / predicted cluster | 12 / 12 |
| Over-merged records | 16 |
| Under-merged records | 61 |

### Companies, on real-shaped labels

| Metric | Value |
|---|---|
| Labelled links (stated registration numbers) | 103 |
| Links surviving blocking | 103 |
| Links recovered end to end | 103 |
| **Blocking recall / end-to-end recall** | **1.000 / 1.000** |

### The diagnostic that matters most

```
lost_by_stage: {"blocking": 139, "matching": 0}
recall_within_candidates: 1.000
```

**Every false negative is lost at blocking. The matcher misses nothing it is
shown.**

This changes what to work on. A single recall figure of 0.92 invites matcher
tuning; this decomposition says matcher tuning would buy exactly zero. Blocking
is the entire remaining gap, and blocking recall is a hard ceiling — a pair
never generated cannot be recovered downstream, however good the model.

---

## 4. How that diagnostic changed the design

The first run scored **blocking recall 0.840, B-cubed F1 0.972**, with all
misses at blocking. The error taxonomy attributed them to pairs whose *surnames*
had been transliterated differently while the forename stayed stable —
`Petrov`/`Petroff`, `Sokolov`/`Sokoloff`.

Every person key at the time was anchored on surname or full name:

| Key | Anchor |
|---|---|
| `p_fp_dob`, `p_fp` | full name |
| `p_phon_dob` | phonetic full name |
| `p_last_dob`, `p_last_init_pc` | surname |
| `p_dob_pc` | birth year + postcode |

No key could reach a pair whose surname differed. Adding `p_first_dob`
(forename + birth year) as the mirror of `p_last_dob`:

| | Before | After |
|---|---|---|
| Blocking recall | 0.840 | **0.925** |
| B-cubed F1 | 0.972 | **0.987** |
| Candidate pairs | 519 | 800 |

An 8.5-point recall gain for 281 extra pairs. The point is not the key itself —
it is that the evaluation identified it. Aggregate F1 would have shown a
mediocre number with no indication of what to do about it.

---

## 5. Per-key blocking report

Pair completeness is the share of *all* true pairs a key generates on its own.
They sum past 1.0 because keys overlap — that redundancy is the design.

| Key | Type | Blocks | Largest | Pairs emitted | Pair completeness |
|---|---|---|---|---|---|
| `p_first_dob` | Person | 551 | 12 | 1,380 | **0.624** |
| `p_dob_pc` | Person | 397 | 8 | 1,090 | 0.617 |
| `p_phon_dob` | Person | 619 | 11 | 989 | 0.560 |
| `p_fp` | Person | 645 | 14 | 1,162 | 0.443 |
| `p_last_dob` | Person | 669 | 11 | 880 | 0.420 |
| `p_fp_dob` | Person | 718 | 8 | 696 | 0.394 |
| `p_last_init_pc` | Person | 628 | 8 | 579 | 0.328 |
| `c_phon` | Company | 487 | 13 | 2,818 | — |
| `c_fp` | Company | 502 | 13 | 2,795 | — |
| `c_reg` | Company | 1,318 | 4 | 127 | — |

Reading it:

- **No single key exceeds 0.63.** Any pipeline relying on one blocking strategy
  loses at least a third of its true pairs before matching begins.
- **`p_dob_pc` reaches 0.617 using no name at all.** It is the only key that
  survives a wholesale name change — marriage, or both name parts transliterated
  differently.
- **`p_fp_dob` is the most precise but least complete** (0.394). Precision and
  completeness trade off exactly as expected, which is why the union is used.
- **`c_reg` emits only 127 pairs** because registration numbers are nearly
  unique. Cheap and near-deterministic where present.
- **`c_addr` and `c_head_pc` emit zero pairs** on this corpus, because synthetic
  addresses are near-unique. On real data they carry real weight — formation
  agents concentrate thousands of registrations at single addresses — so their
  value cannot be assessed here. Stated rather than quietly dropped.

Combined: **2,423 candidate pairs from 3,500,571 possible — a reduction ratio of
0.9993**, retaining 92.1% of true pairs.

---

## 6. Threshold sweep

| Threshold | Precision | Recall | F1 | Accepted |
|---|---|---|---|---|
| 0.50 | 0.9276 | 0.9213 | 0.9245 | 1,755 |
| 0.60 | 0.9389 | 0.9213 | 0.9300 | 1,734 |
| 0.70 | 0.9588 | 0.9213 | 0.9397 | 1,698 |
| 0.80 | 0.9656 | 0.9213 | 0.9429 | 1,686 |
| 0.90 | 0.9754 | 0.9213 | 0.9476 | 1,669 |
| **0.92** | **0.9795** | **0.9213** | **0.9495** | 1,662 |
| 0.95 | 0.9854 | 0.9168 | 0.9499 | 1,644 |
| 0.975 | 0.9890 | 0.9145 | 0.9503 | 1,634 |

Recall is flat at 0.9213 from 0.50 to 0.90 — because the missing pairs are not
below threshold, they are absent from the candidate set entirely. The same
finding, visible a second way.

F1 technically peaks at 0.975. **0.92 is chosen anyway.** In a screening
context the costs are asymmetric: a missed sanctions link is a compliance
failure, while a false positive is a pair an analyst discards in seconds.
Optimising F1 treats those as equal, which they are not. The 0.92/0.62 band also
keeps the uncertain region at ~5% of pairs, which is what bounds LLM
adjudication cost.

---

## 7. Error taxonomy

False negatives by corruption present on either record (a pair can carry
several):

| Corruption | False negatives |
|---|---|
| `title_change` | 112 |
| `typo` | 84 |
| `nationality_variant` | 80 |
| `address_reformat` | 59 |
| `transliteration` | 55 |
| `middle_name_dropped` | 36 |
| `name_order_swap` | 36 |
| `case_change` | 33 |
| none recorded | 11 |

`title_change` tops the list at 75% base rate — it is a marker of "this record
was corrupted at all", not a cause. The informative signal is `typo` (84 misses
at an 18% base rate) and `transliteration` (55 at 40%): both damage the *inside*
of a token, which defeats prefix-anchored and phonetic keys alike. A character
n-gram or edit-distance-tolerant blocking key is the natural next addition.

The 11 misses with no recorded corruption are OpenSanctions alias pairs where
two different aliases of one entity share neither forename nor surname —
genuinely unreachable without a cross-alias key.

---

## 8. Matcher comparison

Same candidate pairs, same thresholds, same clustering.

| Matcher | Pairwise P | Pairwise R | Pairwise F1 | B-cubed P | B-cubed R | B-cubed F1 |
|---|---|---|---|---|---|---|
| `rules` | 0.980 | **0.921** | **0.949** | 0.995 | **0.984** | **0.989** |
| `splink` | **1.000** | 0.843 | 0.915 | **1.000** | 0.948 | 0.973 |

**The rule matcher wins here, and the result should not be over-read.**

Splink's EM had 1,107 person records to work with and emitted explicit warnings
that several `m` parameters were never estimated — comparison levels that simply
never occurred in training. Fellegi–Sunter estimates two probabilities per
comparison level from data; at this scale there is not enough data, and it falls
back to defaults.

The shape of the difference is informative. Splink reaches **precision 1.000 and
B-cubed precision 1.000 — zero false positives, zero over-merged records** —
which is what a well-calibrated probabilistic model looks like when it is
confident. It pays for that with recall: 168 records sit in incomplete clusters
against the rule matcher's 61, and `recall_within_candidates` drops to 0.915,
meaning Splink is the only matcher here that actually rejects true pairs it was
shown. Those rejections are concentrated exactly where the untrained parameters
are.

So the two matchers fail in opposite directions, which is a genuinely useful
property: for a screening application where a missed link is the expensive
error, the rule matcher is preferable; for one where an analyst's time is the
constraint and every hit must be real, Splink's zero-false-positive behaviour is
worth the recall.

**This comparison is only meaningful at full national scale**, where EM has 12M
records and term-frequency adjustment can do its real work — discounting
agreement on `Smith` while rewarding agreement on `Kowalczyk`, which the rule
matcher cannot learn because its weights are fixed. The expectation is that
Splink overtakes the rule matcher there. Until that run happens, this table says
what was measured, not what is hoped for.

### LLM adjudication

The harness is complete and tested against a mocked client; the measured
comparison requires an API key and the full-scale run. What is fixed by design:

- Only the uncertain band is routed (~5% of scored pairs here), bounding cost
  and blast radius.
- Verdicts are cached in DuckDB keyed by pair and prompt version.
- Malformed or low-confidence responses leave a pair uncertain rather than
  guessing — an unresolved pair is a known unknown; a fabricated resolution is
  not.
- A guardrail refuses to run when the band exceeds 35% of pairs, which means
  misconfiguration rather than genuine ambiguity.

The reporting template requires precision/recall with and without adjudication,
cost per corrected decision, and disagreement analysis against the rule matcher.
**If it does not earn its cost, the report will say so.**

---

## 9. Conflict splitting ablation

Same accepted edges; the only difference is whether contradictory components are
re-clustered.

| | Clusters | Largest cluster | B-cubed P | B-cubed R | B-cubed F1 |
|---|---|---|---|---|---|
| Closure only | 459 | 16 | 0.9725 | 0.9841 | 0.9782 |
| **+ conflict splitting** | **468** | **12** | **0.9946** | 0.9841 | **0.9893** |

Splitting found contradictions in 7 person components (incompatible birth years)
and 24 company components (incompatible registration numbers), producing 9
additional person clusters.

Three things to note:

1. **Precision rises 2.2 points; recall does not move at all.** Splitting is
   free — it only ever separates records that could not have belonged together,
   so it cannot cost a true merge.
2. **The largest cluster falls from 16 to 12, which is exactly the true
   maximum.** Closure had produced a cluster larger than any real entity in the
   corpus — the signature of chained false merges, caught by the constraint
   rather than by a threshold.
3. **Conflicts are far more common among companies (24) than people (7)**,
   because company names are more repetitive and registration numbers are a
   sharper constraint than birth years.

The effect is this modest *because the corpus is well-behaved*. The mechanism
guards against a failure that is catastrophic rather than gradual: on the real
register, a single false edge between two common-name clusters merges hundreds
of distinct people into one entity that is confidently wrong.
`tests/test_cluster.py` constructs that case directly, since aggregate metrics
on a clean corpus cannot demonstrate it.

---

## 10. What these numbers do not establish

- **Fixture metrics are not real-data metrics.** The corruption model is a
  hypothesis. The held-out registration evaluation is the check that does not
  share it — and it is the number to weight most heavily.
- **Company resolution is under-evaluated.** 103 labelled links is a small
  sample, and it covers only companies whose parent stated a UK number. Foreign
  parents, the hardest and most interesting case, have no labels here.
- **No temporal validation.** Ownership changes; this is a snapshot.
- **Splink is not fairly assessed** at this scale, as above.
- **B-cubed recall of 0.984 still means 61 records sit in incomplete clusters.**
  At national scale that proportion is tens of thousands of people whose filings
  are split across two entities.

## 11. Reproducing

```bash
make fixtures                                   # regenerate the corpus (seeded)
make demo                                       # pipeline + evaluation
oer evaluate --truth fixtures/ground_truth.json --matcher rules
oer match --matcher splink && oer cluster --matcher splink
oer evaluate --truth fixtures/ground_truth.json --matcher splink
```

CI regenerates the fixtures from seed and diffs them against the committed copy,
so the corpus these numbers refer to cannot drift silently. It then enforces
floors of B-cubed F1 ≥ 0.92, precision ≥ 0.90, recall ≥ 0.80.
