# Data dictionary

## Sources

### Companies House — Free Company Data Product

Multi-part CSV, ~5.6M rows, published on the 1st of each month.
<https://download.companieshouse.gov.uk/en_output.html>

| Source column | Target | Notes |
|---|---|---|
| `CompanyName` | `name`, `name_fp`, `name_phonetic` | Legal form stripped from the fingerprint |
| `CompanyNumber` | `reg_number`, `record_id` | Zero-padded to 8 characters |
| `RegAddress.*` | `address_full`, `postcode`, `address_blk` | |
| `CompanyCategory` | `legal_form` | Fallback when no form is detected in the name |
| `CompanyStatus` | `status` | Active / Dissolved / Liquidation / … |
| `IncorporationDate` | `incorporation_date` | `DD/MM/YYYY` in source |
| `DissolutionDate` | `dissolution_date` | |
| `SICCode.SicText_1..4` | `sic_codes` | Code split from description |
| `URI` | `source_url` | |

Gotchas: several published headers carry a **leading space**, so headers are
stripped before use. Dates are `DD/MM/YYYY`, not ISO.

### Companies House — PSC snapshot

Newline-delimited JSON, ~12M rows, refreshed daily before 10:00 GMT.
<https://download.companieshouse.gov.uk/en_pscdata.html>

Each line is `{"company_number": ..., "data": {...}}`, where the shape of `data`
depends on `kind`.

| `kind` | Parsed as | Share |
|---|---|---|
| `individual-person-with-significant-control` | `Person` record + control edge | ~80% |
| `corporate-entity-person-with-significant-control` | `Company` record + control edge | ~15% |
| `legal-person-person-with-significant-control` | `LegalEntity` record + control edge | <1% |
| `persons-with-significant-control-statement` | **counted only** | ~5% |
| `super-secure-person-with-significant-control` | **counted only** | <0.1% |
| `exemptions` | **counted only** | <0.1% |

The last three are declarations that no identifiable controller exists. Parsing
them as entities would inject millions of phantom people with near-identical
names into the resolution set. They are counted instead — and the count is a
finding, being the share of the register that declares no identifiable owner.

Individual fields: `name`, `name_elements` (`forename`, `middle_name`,
`surname`, `title` — present ~85% of the time and preferred over parsing the
display name), `date_of_birth` (**month and year only**, by statute),
`nationality` (free text), `country_of_residence`, `address`,
`natures_of_control`, `notified_on`, `ceased_on`, `links.self`.

Corporate fields: `identification` with `registration_number`,
`country_registered`, `place_registered`, `legal_form`, `legal_authority`.

### OpenSanctions — default dataset

FollowTheMoney entities as newline-delimited JSON, rebuilt continuously.
<https://www.opensanctions.org/datasets/default/>

Properties consumed: `name`, `alias`, `previousName`, `weakAlias` (each variant
becomes its own record, up to 12), `birthDate` (mixed precision — only year and
month retained), `nationality`, `country`, `jurisdiction`,
`registrationNumber`, `address`, `postalCode`, `topics`, `sourceUrl`.

Licence: CC-BY-NC 4.0. Commercial use requires a licence from OpenSanctions.

---

## `records` table

| Column | Type | Description |
|---|---|---|
| `record_id` | VARCHAR | Source-scoped stable id |
| `source` | VARCHAR | `ch_companies` \| `ch_psc` \| `opensanctions` |
| `entity_type` | VARCHAR | `Person` \| `Company` \| `LegalEntity` |
| `name` | VARCHAR | Display name as filed |
| `name_norm` | VARCHAR | Lowercased, romanised, punctuation stripped |
| `name_fp` | VARCHAR | Order-insensitive fingerprint, legal form removed |
| `name_phonetic` | VARCHAR | Soundex per token |
| `first_name` / `middle_name` / `last_name` | VARCHAR | Structured parts |
| `birth_year` / `birth_month` | INTEGER | Day is never published |
| `nationality` / `country` / `jurisdiction` | VARCHAR | ISO 3166-1 alpha-2, `''` if uncodeable |
| `reg_number` | VARCHAR | Normalised for UK; verbatim for foreign registers |
| `address_full` / `address_norm` | VARCHAR | Joined and normalised |
| `postcode` | VARCHAR | `OUTWARD INWARD`, `''` if not a valid UK postcode |
| `address_blk` | VARCHAR | `{building number}\|{postcode}` blocking key |
| `incorporation_date` / `dissolution_date` | DATE | |
| `status` / `legal_form` | VARCHAR | |
| `sic_codes` | VARCHAR[] | |
| `topics` | VARCHAR[] | OpenSanctions risk vocabulary |
| `datasets` | VARCHAR[] | Contributing upstream datasets |
| `context_company_number` | VARCHAR | Company a PSC filing relates to |
| `source_url` / `retrieved_at` | VARCHAR | Provenance |

**Empty string vs NULL.** Text columns use `''` for "not stated" so SQL
comparisons stay total; numeric and date columns use `NULL`. Comparison features
map both to `NULL` so that missingness contributes no evidence — see
[`design-decisions.md`](design-decisions.md) §6.

## `relationships` table

| Column | Type | Description |
|---|---|---|
| `rel_id` | VARCHAR | `rel:{psc_record_id}->{company_number}` |
| `rel_type` | VARCHAR | `OWNS` (quantified) \| `CONTROLS` (unquantified) |
| `source_record_id` | VARCHAR | The controlling party |
| `target_record_id` | VARCHAR | The controlled company |
| `min_percent` / `max_percent` | DOUBLE | Band bounds; `NULL` for non-equity control |
| `control_kinds` | VARCHAR[] | `SHARES`, `VOTING`, `APPOINT_DIRECTORS`, … |
| `capacities` | VARCHAR[] | `TRUST`, `FIRM`, `LLP` — held via a fiduciary layer |
| `has_hard_control` | BOOLEAN | Control regardless of shareholding |
| `via_fiduciary` | BOOLEAN | The named PSC is a nominee layer |
| `notified_on` / `ceased_on` | DATE | `ceased_on IS NULL` means currently in force |

### `natures_of_control` vocabulary

| Pattern | Kind | Percent |
|---|---|---|
| `ownership-of-shares-{lo}-to-{hi}-percent` | `SHARES` | band |
| `voting-rights-{lo}-to-{hi}-percent` | `VOTING` | band |
| `right-to-appoint-and-remove-directors` | `APPOINT_DIRECTORS` | none — hard control |
| `right-to-appoint-and-remove-members` | `APPOINT_MEMBERS` | none — hard control |
| `right-to-share-surplus-assets-{lo}-to-{hi}-percent` | `SURPLUS_ASSETS` | band |
| `significant-influence-or-control` | `SIGNIFICANT_INFLUENCE` | none |

Suffixes `-as-trust`, `-as-firm` and `-limited-liability-partnership` set
`capacities` and `via_fiduciary`. That flag is material: it means the named PSC
is a nominee or fiduciary layer, so the real controlling party sits behind a
structure the register does not expose.

Unknown values map to `OTHER` rather than raising, so a vocabulary addition
upstream degrades one field instead of failing a 12M-row load.

## Derived tables

| Table | Contents |
|---|---|
| `blocking_keys` | `(record_id, key_name, key_value, entity_type)` |
| `candidate_pairs` | `(left_id, right_id, block_keys[], n_keys)`, ordered `left < right` |
| `pair_scores` | `(left_id, right_id, matcher, score, decision, features, rationale)` |
| `clusters` | `(record_id, canonical_id, cluster_size, split_round, matcher)` |
| `canonical_entities` | One row per resolved entity, retaining the full alias list |
| `control_closure` | Transitive control paths to 6 hops, with interval shares |
| `llm_cache` | Adjudication verdicts keyed by pair + prompt version + model |

## Blocking keys

### Person

| Key | Definition | Rationale |
|---|---|---|
| `p_fp_dob` | fingerprint + birth year | Highest precision |
| `p_fp` | fingerprint | Recovers records with no birth year |
| `p_phon_dob` | soundex + birth year | Transliteration variants |
| `p_last_dob` | surname + birth year | Anglicised forenames (Yevgeniy/Eugene) |
| `p_first_dob` | forename + birth year | Transliterated surnames (Petrov/Petroff) |
| `p_last_init_pc` | surname + postcode | No birth year available |
| `p_dob_pc` | birth year + postcode | Survives a wholesale name change |

### Company

| Key | Definition | Rationale |
|---|---|---|
| `c_reg` | registration number | Near-deterministic; basis of the held-out evaluation |
| `c_fp` | fingerprint | Prefix- and suffix-form legal names |
| `c_phon` | soundex | Transcription variants of foreign parents |
| `c_head_pc` | first token + postcode | Renamed companies at a stable address |
| `c_addr` | address key + name prefix | Address alone is unusable — formation agents |

Blocks exceeding each key's `max_block_size` are excluded and **reported**, not
silently truncated, since truncation hides a recall loss.

## Risk topics

Sanctions nexus: `sanction`, `sanction.linked`, `sanction.counter`,
`export.control`, `export.risk`.

Wider vocabulary retained: `role.pep`, `role.rca`, `role.oligarch`, `crime.*`,
`poi`, `debarment`, `asset.frozen`, `wanted`, `gov.soe`, `corp.shell`,
`corp.disqual`.

## Secrecy jurisdictions

Registers that do not publish beneficial ownership, or publish it only to
authorities: `VG KY BM BS BZ SC MU MH PA AI TC KN WS VU CW AW VC AG DM GD LI MC
AD SM JE GG IM GI CY MT LU AE`.

Presence in a chain is **not evidence of wrongdoing**. It is a reason the chain
cannot be resolved further from open data — which is exactly the quantity the
opacity analysis measures.
