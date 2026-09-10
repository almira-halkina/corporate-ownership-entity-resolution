# Planned: semantic (embedding) blocking key

Status: **not implemented.** This is a design note for the next blocking key,
written before the work so the acceptance criteria are fixed in advance rather
than chosen after seeing the numbers.

---

## Why this key, and why now

Blocking recall is a hard ceiling on end-to-end recall: a pair that blocking
never generates cannot be recovered by any downstream matcher. The current
evaluation makes that concrete —

| Diagnostic | Current | Meaning |
|---|---|---|
| Recall within candidates | **1.000** | The matcher misses nothing blocking hands it |
| Blocking recall | 0.925 | Every end-to-end miss is lost here |
| Blocking reduction ratio | 0.9993 | Quadratic space eliminated |
| B-cubed F1 | 0.989 | Current cluster quality |

Recall within candidates at 1.000 means further matcher tuning buys nothing.
The only remaining recall is at blocking. This is the same reasoning that drove
adding `p_first_dob`, which lifted blocking recall from 0.840 to 0.925.

The residual misses are concentrated in the failure mode the existing keys are
structurally blind to: **transliteration variance**. A name-fingerprint key
compares normalised character content, so `Yevgeniy` / `Evgeny` / `Evgenii` /
`Eugene` land in different blocks. A phonetic key (Soundex/Metaphone family) is
tuned to English orthography and degrades on Slavic, Turkic and Arabic
transliteration. A birth-year key fails wherever the registrar withheld the
date. Each key has an independent blind spot, which is why they are unioned —
but none of them recovers a name that was romanised under a different scheme.

A semantic key over name embeddings attacks exactly that blind spot.

---

## Approach

Add `p_name_embedding` (and optionally `c_name_embedding`) as one more key in
the existing union, behind the same interface as every other key in
`src/ownership_er/block.py`. It emits candidate pairs; nothing downstream
changes.

1. **Embed** normalised names from `normalize/names.py` with a small
   multilingual sentence-embedding model. The failure mode is cross-script and
   cross-romanisation, so the model must be multilingual, not English-only.
   Character-level or subword models are likely to beat sentence models here —
   worth testing both rather than assuming.
2. **Index** the vectors and take k nearest neighbours per record above a
   cosine floor. Exact search is fine at fixture scale; use an ANN index
   (FAISS / hnswlib) for the ~12M-filing real run. Cache vectors on disk keyed
   by normalised name so re-runs are cheap and the demo stays fast.
3. **Union** with the existing keys. Because it is a union, this key can only
   add candidate pairs — it cannot remove recall. The cost lands on the
   reduction ratio and on runtime, and both must be reported.
4. **Report** per-key pair completeness and cost in the same per-run table as
   every other key, so this stays an evidenced decision rather than folklore.

### Offline constraint

`make demo` currently runs in ~30 seconds with no downloads and no API keys.
That property is worth more than this key is. Either vendor a small model, or
commit precomputed vectors for the fixture corpus and make the live embedding
path optional. Do not let the demo acquire a network dependency.

---

## What could go wrong

- **Name embeddings over-merge common names.** Semantic similarity does not
  distinguish two genuinely different `John Smith`s, and may not even
  distinguish `John Smith` from `Jane Smith` as sharply as an exact key does.
  The conflict-aware splitter is the backstop, but precision must be measured,
  not assumed.
- **Reduction ratio degradation.** kNN with a generous floor can reintroduce a
  large share of the quadratic space. Tune `k` and the floor against cost.
- **The model may simply not encode transliteration equivalence.** General
  sentence embeddings are trained on semantics, not orthographic variance;
  `Yevgeniy` and `Evgeny` may not be close in that space at all.

---

## Acceptance criteria

Fix these before running anything.

| Metric | Requirement |
|---|---|
| Blocking recall | **> 0.925** (target ≥ 0.96) |
| Recall within candidates | stays 1.000 |
| B-cubed F1 | ≥ 0.989 (no precision regression) |
| B-cubed precision | ≥ 0.995 |
| Blocking reduction ratio | ≥ 0.995 |
| `make demo` runtime | ≤ 60s, still offline |
| Held-out registration recall | stays 1.000 |

**A negative result is a valid outcome and still worth shipping.** If the key
does not clear the bar, keep it behind a flag, write up why in
`docs/evaluation.md`, and report the measured delta. "We tested an embedding
key against a labelled benchmark and it did not beat the phonetic key, here is
the number" is a stronger artefact than an untested claim that it would.

---

## Why this is worth a weekend

Three things Brattle's Data & AI Engineer posting names explicitly, and that
appear nowhere in the current portfolio, are closed by this one change:

- **embeddings**
- **vector search**
- **model evaluation of an AI component against a benchmark**

They are closed inside a project that already has ground truth, an evaluation
harness, CI, and a documented decision history — so the delta is measurable and
the whole thing is defensible in an interview. Building a separate toy RAG
project would demonstrate none of that.

### After it is done

- Resume, project bullet: add the embedding/vector-search key and its measured
  effect on blocking recall.
- Resume, skills: `embeddings`, `vector search / ANN indexing` move from absent
  to earned.
- Cover letter: this becomes the second AI-judgment example alongside the
  bounded LLM adjudicator — an AI component adopted or rejected on a measured
  number rather than on vibes.
