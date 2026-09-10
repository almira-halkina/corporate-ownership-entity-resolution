# Findings

> **Status.** The analyses below are complete and run end to end, but the
> figures are from the **fixture corpus**, not the real registers. They
> demonstrate what each analysis produces and how to read it. Section 6 is the
> template for the national run; every number there is produced by
> `make run && make analyse` and nothing needs to be written by hand.
>
> Fixture figures are labelled as such throughout. Nothing here is a claim about
> any real company or person — the corpus is synthetic and the names are
> fictitious.

---

## 1. What resolution actually bought

The central question for a pipeline like this is not "did it merge records"
but "what became answerable that was not answerable before".

**Fixture corpus, 2,656 source records:**

| Quantity | Value |
|---|---|
| Source records | 2,656 |
| Canonical entities after resolution | 1,786 |
| Records absorbed by merging | 870 (32.8%) |
| Entities spanning more than one source | 210 |
| One-hop control paths | 1,165 |
| **Multi-hop control paths** | **152** |
| Companies with an indirect owner | 107 |
| Sanctions exposure — direct (1 hop) | 83 companies |
| **Sanctions exposure — indirect (2+ hops)** | **12 companies** |

The last row is the point of the whole exercise.

Direct exposure is what a conventional screening tool already finds: the
sanctioned party is named on the company's own filing. **Indirect exposure only
exists because records were resolved.** A multi-hop chain requires matching a
company *name typed by a filer* on one PSC record against a *company on the
register* — there is no shared identifier to join on. Without that match the
chain stops at hop one and those companies look clean.

On the fixture corpus, resolution increased detected sanctions exposure by
**14.5%** (83 → 95 companies). Whether the real register produces a larger or
smaller uplift is an empirical question the national run answers.

## 2. Where control chains terminate

Every chain is followed upward to an entity with no owner above it. What that
terminal entity *is* determines whether beneficial ownership was actually
established.

**Fixture corpus, 1,318 company entities:**

| Outcome | Companies | Share |
|---|---|---|
| Resolves to a natural person | 926 | 70.3% |
| Terminates in a company (non-secrecy jurisdiction) | 76 | 5.8% |
| **Terminates in a secrecy jurisdiction** | **90** | 6.8% |
| No control edge at all | 359 | 27.2% |
| Chain passes through a fiduciary layer | 254 | 19.3% |

Three distinct failure modes, deliberately counted separately because they have
different remedies:

- **No control edge** — the company filed no PSC record, or filed a statement
  declaring no identifiable controller. A registry compliance problem.
- **Terminates in a secrecy jurisdiction** — the chain is complete as far as
  open data goes, and stops at a BVI or Cayman entity whose register publishes
  nothing. No amount of better matching fixes this; it is a data-availability
  boundary.
- **Passes through a fiduciary layer** — a trust, firm or nominee arrangement.
  The named PSC is a legal placeholder and the real controlling party sits
  behind a structure the register does not expose.

Only the first is a matching problem. Reporting them as one "unresolved"
bucket would conflate a fixable pipeline gap with a legal transparency limit —
and the distinction is exactly what a policy reader needs.

## 3. Opacity by jurisdiction

Terminal jurisdictions, ranked by companies controlled (fixture corpus):

| Jurisdiction | Companies | Controlling entities | Secrecy jurisdiction |
|---|---|---|---|
| SC (Seychelles) | 6 | 6 | Yes |
| MT (Malta) | 5 | 5 | Yes |
| VG (British Virgin Islands) | 5 | 5 | Yes |
| NL (Netherlands) | 4 | 4 | No |
| RU (Russia) | 3 | 3 | No |
| LU (Luxembourg) | 3 | 3 | Yes |

On real data this table is the headline coverage finding, and it is directly
comparable to published work — Global Witness's *The Companies We Keep* and
OpenCorporates' opacity research both report variants of it. The mean hop count
per jurisdiction is the interesting secondary column: chains terminating in
secrecy jurisdictions are typically *longer*, which is what layering looks like
when measured rather than asserted.

## 4. Control without equity

Ownership percentage is the obvious ranking, and on its own it is misleading.

The pipeline separately counts chains where at least one hop asserts control
with **no percentage at all** — the right to appoint and remove directors, or
significant influence. A person holding zero shares but the power to appoint the
board controls the company completely.

Any screening approach that ranks by percentage misses them entirely, and that
is not a hypothetical: it is the structure a party wanting to stay below a
percentage threshold would choose. Chains flagged `control_without_equity` are
surfaced in `sanctions_exposure` output as a first-class column rather than
being folded into a null percentage.

Similarly, percentages are propagated as **intervals**, never midpoints. A
two-hop chain of `25-50%` through `50-75%` yields `12.5-37.5%`. Reporting
"18.75%" would assert a precision the register does not publish.

## 5. Circular ownership

`circular_ownership` finds entities that ultimately control themselves. The
fixture corpus contains none, which is expected — the generator does not
construct them.

They matter on real data for two reasons. Some are legitimate group structures.
Others are deliberate obfuscation, since a cycle makes naive upward traversal
non-terminating and defeats tools that do not guard for it. Either way,
enumerating them is a prerequisite for trusting any beneficial-owner result,
because a cycle means the "ultimate" owner reported by an unguarded traversal is
arbitrary.

Both the recursive SQL and the Cypher traversal carry explicit cycle guards; the
cycle query deliberately relaxes the guard to find what the guard excludes.

## 6. Template for the national run

Produced by `make data && make run && make analyse`. Written to
`outputs/analysis/analysis.json`.

```
Scale
  Companies parsed                        ______
  PSC filings parsed                      ______
  PSC statements (no identifiable owner)  ______   (____%)
  OpenSanctions entities                  ______
  Records → canonical entities            ______ → ______

Resolution impact
  Records absorbed by merging             ______   (____%)
  Cross-source entities                   ______
  Multi-hop control paths                 ______
  Sanctions exposure, direct              ______ companies
  Sanctions exposure, indirect            ______ companies   ← the headline
  Uplift from resolution                  ____%

Opacity
  Resolves to a natural person            ____%
  Terminates in a secrecy jurisdiction    ____%
  No PSC filing at all                    ____%
  Chain via a fiduciary layer             ____%

Quality (held-out registration numbers, real data)
  Labelled links                          ______
  Blocking recall                         ____
  End-to-end recall                       ____
```

### Questions the national run should answer

1. **How much sanctions exposure is invisible at one hop?** The single most
   defensible claim this project can make.
2. **What share of UK companies cannot be resolved to a natural person from open
   data?** Directly comparable to Global Witness's published PSC work.
3. **Which jurisdictions terminate the most chains, and are those chains
   longer?** Layering, measured.
4. **How many companies are controlled without equity?** The population a
   percentage-threshold screen structurally misses.
5. **Does the LLM adjudicator earn its cost?** Precision/recall with and without,
   plus cost per corrected decision. If it does not, that is the finding.

### Before publishing any of this

- **Record the snapshot date and SHA-256.** Companies House replaces the PSC
  file every morning; a figure without a snapshot date is not reproducible.
  `data/raw/manifest.json` has both.
- **Check the hub-entity list by hand.** Entities controlling implausibly many
  companies are the cheapest available check on cluster quality at a scale where
  no labels exist. Some are genuine holding groups, some are nominee directors,
  and some are over-merged clusters — the last would corrupt every downstream
  figure.
- **State the limits.** These are *filings*, not verified facts. A PSC record
  asserts what someone declared to a registry that does not independently verify
  it. Resolution error is real and measured; the honest framing is "the open
  record indicates", never "X controls Y".
- **Do not name individuals.** The methodology is the contribution. Naming real
  people on the basis of a probabilistic match is neither necessary for the
  point nor defensible if the match is wrong.

---

## Reproducing

```bash
make demo      # fixture figures above
make analyse   # writes outputs/analysis/analysis.json
make data && make run   # the real thing
```
