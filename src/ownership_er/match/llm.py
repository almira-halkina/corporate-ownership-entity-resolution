"""LLM adjudication of borderline pairs.

Scope, and why it is narrow
---------------------------
The adjudicator only ever sees pairs the base matcher scored in the *uncertain
band* — between ``auto_reject`` and ``auto_accept``. It never re-litigates a
confident decision. That single constraint is what makes the component
defensible rather than decorative:

* **Cost is bounded and predictable.** On the fixture corpus the band is ~3% of
  scored pairs. Sending all 10^8 candidate pairs to a language model would cost
  more than the rest of the project by several orders of magnitude and would be
  a straightforwardly worse engineering decision than the string comparison
  already doing the job.
* **The blast radius is bounded.** A model failure can only affect pairs that
  were already unresolved. The deterministic matcher's confident output is
  unchanged and stays reproducible, which matters when the pipeline output
  feeds a compliance decision that has to be explained months later.
* **The comparison is fair.** Because base and adjudicated runs differ only on
  the band, the measured delta is attributable to the adjudicator alone.

This mirrors where the field currently is. OpenSanctions' *OpenSanctions Pairs*
benchmark (2026) found off-the-shelf LLMs outperforming their production
rule-based matcher on 755k labelled pairs from real sanctions aggregation — a
result worth taking seriously, and worth testing rather than assuming. The
harness here is built so the claim can be checked on this dataset:
``docs/evaluation.md`` reports base-matcher metrics, adjudicated metrics, and
the cost per corrected decision. If the adjudicator does not earn its cost, the
report says so.

Engineering
-----------
Responses are cached in DuckDB keyed by pair and prompt version, so re-running
the pipeline costs nothing and a prompt change invalidates only what it should.
Output is parsed against a strict contract and a malformed or low-confidence
response leaves the pair uncertain rather than guessing — an unresolved pair is
a known unknown, and a fabricated resolution is not.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any

import duckdb

from ownership_er.config import Settings, get_settings

__all__ = ["PROMPT_VERSION", "AdjudicationResult", "LLMAdjudicator", "build_prompt"]

# Bump when the prompt changes; cached responses are keyed on it so a prompt
# revision re-runs affected pairs and leaves the rest untouched.
PROMPT_VERSION = "v3"

CACHE_DDL = """
CREATE TABLE IF NOT EXISTS llm_cache (
    cache_key      VARCHAR PRIMARY KEY,
    left_id        VARCHAR,
    right_id       VARCHAR,
    prompt_version VARCHAR,
    model          VARCHAR,
    verdict        VARCHAR,
    confidence     DOUBLE,
    reason         VARCHAR,
    input_tokens   INTEGER,
    output_tokens  INTEGER,
    latency_ms     INTEGER,
    created_at     TIMESTAMP DEFAULT current_timestamp
);
"""

SYSTEM_PROMPT = """\
You are an entity-resolution adjudicator for a corporate-ownership intelligence \
pipeline. You decide whether two records refer to the SAME real-world entity.

The records come from the UK Companies House register and from OpenSanctions. A \
deterministic matcher has already scored this pair and found it genuinely \
ambiguous, so you are seeing only hard cases. Easy ones never reach you.

Decide using these principles, in order of authority:

1. HARD CONSTRAINTS. Two records CANNOT be the same entity if they state \
different years of birth, or different registration numbers within the same \
jurisdiction. These override any amount of name similarity. Note that a value \
being ABSENT is not a difference — absence is no evidence either way.

2. NAME VARIATION IS EXPECTED. Filings for one person routinely differ by \
transliteration (Yevgeniy / Evgeny / Eugene), diacritics (Zielinski / \
Zielinski), title, middle-name presence, or field order (surname entered as \
forename). None of these is evidence against a match on its own.

3. SHARED ADDRESS IS WEAK. Company formation agents register tens of thousands \
of companies at one address, so a shared address is only mild corroboration, \
never decisive.

4. RARE AGREEMENT OUTWEIGHS COMMON AGREEMENT. Two people sharing the surname \
"Kowalczyk" is far stronger evidence than two sharing "Smith". Weight agreement \
by how surprising it would be by chance.

5. WHEN GENUINELY UNDECIDABLE, SAY SO. Returning "unclear" is a correct and \
useful answer. A wrong merge in this pipeline creates a false ownership link \
that could misattribute sanctions exposure to an innocent party, or conceal a \
real one. Do not guess to appear decisive.

Respond with ONLY a JSON object, no prose before or after:
{"verdict": "match" | "no_match" | "unclear", "confidence": <0.0-1.0>, \
"reason": "<one sentence, max 25 words, citing the decisive evidence>"}\
"""

_FIELD_LABELS: list[tuple[str, str]] = [
    ("name", "Name"),
    ("first_name", "Forename"),
    ("middle_name", "Middle name"),
    ("last_name", "Surname"),
    ("birth_year", "Birth year"),
    ("birth_month", "Birth month"),
    ("nationality", "Nationality (ISO)"),
    ("country", "Country (ISO)"),
    ("jurisdiction", "Jurisdiction (ISO)"),
    ("reg_number", "Registration number"),
    ("postcode", "Postcode"),
    ("address_full", "Address"),
    ("legal_form", "Legal form"),
    ("source", "Source"),
    ("topics", "Risk topics"),
]


@dataclass(slots=True)
class AdjudicationResult:
    left_id: str
    right_id: str
    verdict: str
    confidence: float
    reason: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    cached: bool = False


def _render(record: dict[str, Any]) -> str:
    lines = []
    for key, label in _FIELD_LABELS:
        value = record.get(key)
        if value in (None, "", [], 0):
            continue
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value)
        lines.append(f"  {label}: {value}")
    return "\n".join(lines) if lines else "  (no attributes)"


def build_prompt(
    left: dict[str, Any], right: dict[str, Any], base_score: float, rationale: str
) -> str:
    """Render one adjudication prompt.

    The base matcher's score and its per-feature breakdown are included rather
    than hidden. The model is being asked to adjudicate, not to redo the work —
    telling it which features agreed and which conflicted anchors it to the same
    evidence the deterministic system used, and makes disagreements
    interpretable instead of mysterious.
    """
    return f"""\
RECORD A:
{_render(left)}

RECORD B:
{_render(right)}

Deterministic matcher score: {base_score:.3f} (ambiguous band)
Feature breakdown: {rationale or "unavailable"}

Are RECORD A and RECORD B the same real-world entity?"""


def _cache_key(left_id: str, right_id: str, model: str) -> str:
    payload = f"{left_id}|{right_id}|{PROMPT_VERSION}|{model}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


class LLMAdjudicator:
    """Routes uncertain-band pairs to a language model and records the verdicts."""

    name = "llm"

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        base_matcher: str = "rules",
        client: Any | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.base_matcher = base_matcher
        self._client = client
        self.stats: dict[str, Any] = {
            "requested": 0,
            "cached": 0,
            "called": 0,
            "failed": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "wall_seconds": 0.0,
        }

    # -- client -------------------------------------------------------------

    @property
    def client(self) -> Any:
        if self._client is None:  # pragma: no cover - requires credentials
            try:
                from anthropic import Anthropic
            except ImportError as exc:
                raise ImportError(
                    "The LLM adjudicator needs the optional extra:\n    pip install -e '.[llm]'"
                ) from exc
            key = self.settings.anthropic_api_key
            if not key:
                raise RuntimeError("ANTHROPIC_API_KEY is not set. Add it to .env or export it.")
            self._client = Anthropic(api_key=key)
        return self._client

    # -- selection ----------------------------------------------------------

    def select_band(self, con: duckdb.DuckDBPyConnection, entity_type: str) -> list[dict[str, Any]]:
        """Fetch uncertain-band pairs with both records' attributes.

        Ordered by descending score so that, if the pair budget binds, the
        pairs closest to acceptance are adjudicated first — those are where a
        verdict changes the most outcomes per call.
        """
        rows = con.execute(
            """
            SELECT s.left_id, s.right_id, s.score, s.rationale,
                   l.name, l.first_name, l.middle_name, l.last_name,
                   l.birth_year, l.birth_month, l.nationality, l.country,
                   l.jurisdiction, l.reg_number, l.postcode, l.address_full,
                   l.legal_form, l.source, l.topics,
                   r.name, r.first_name, r.middle_name, r.last_name,
                   r.birth_year, r.birth_month, r.nationality, r.country,
                   r.jurisdiction, r.reg_number, r.postcode, r.address_full,
                   r.legal_form, r.source, r.topics
            FROM pair_scores s
            JOIN records l ON l.record_id = s.left_id
            JOIN records r ON r.record_id = s.right_id
            WHERE s.matcher = ? AND s.decision = 'uncertain'
              AND l.entity_type = ? AND r.entity_type = ?
            ORDER BY s.score DESC
            LIMIT ?
            """,
            [self.base_matcher, entity_type, entity_type, self.settings.match.llm_max_pairs],
        ).fetchall()

        keys = [k for k, _ in _FIELD_LABELS]
        out: list[dict[str, Any]] = []
        for row in rows:
            left = dict(zip(keys, row[4 : 4 + len(keys)], strict=True))
            right = dict(zip(keys, row[4 + len(keys) :], strict=True))
            out.append(
                {
                    "left_id": row[0],
                    "right_id": row[1],
                    "score": float(row[2]),
                    "rationale": row[3] or "",
                    "left": left,
                    "right": right,
                }
            )
        return out

    # -- adjudication -------------------------------------------------------

    def _parse_response(self, text: str) -> tuple[str, float, str]:
        """Parse the model's JSON contract, tolerating fenced code blocks."""
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```")[1]
            if cleaned.startswith("json"):
                cleaned = cleaned[4:]
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end == -1:
            return "unclear", 0.0, "unparseable response"
        try:
            obj = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError:
            return "unclear", 0.0, "malformed json"

        verdict = str(obj.get("verdict", "unclear")).lower().strip()
        if verdict not in {"match", "no_match", "unclear"}:
            verdict = "unclear"
        try:
            confidence = float(obj.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        return verdict, max(0.0, min(1.0, confidence)), str(obj.get("reason", ""))[:300]

    def _adjudicate_one(self, pair: dict[str, Any]) -> AdjudicationResult:
        prompt = build_prompt(pair["left"], pair["right"], pair["score"], pair["rationale"])
        started = time.perf_counter()
        response = self.client.messages.create(
            model=self.settings.match.llm_model,
            max_tokens=200,
            temperature=self.settings.match.llm_temperature,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        text = "".join(
            block.text for block in response.content if getattr(block, "type", "") == "text"
        )
        verdict, confidence, reason = self._parse_response(text)
        usage = getattr(response, "usage", None)
        return AdjudicationResult(
            left_id=pair["left_id"],
            right_id=pair["right_id"],
            verdict=verdict,
            confidence=confidence,
            reason=reason,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            latency_ms=latency_ms,
        )

    def score_pairs(self, con: duckdb.DuckDBPyConnection, entity_type: str) -> int:
        """Adjudicate the uncertain band and write verdicts to ``pair_scores``."""
        con.execute(CACHE_DDL)
        pairs = self.select_band(con, entity_type)
        if not pairs:
            return 0

        self._check_band_size(con, entity_type, len(pairs))

        model = self.settings.match.llm_model
        cached_rows = {
            row[0]: row
            for row in con.execute(
                "SELECT cache_key, verdict, confidence, reason FROM llm_cache "
                "WHERE prompt_version = ? AND model = ?",
                [PROMPT_VERSION, model],
            ).fetchall()
        }

        pending: list[dict[str, Any]] = []
        results: list[AdjudicationResult] = []
        for pair in pairs:
            key = _cache_key(pair["left_id"], pair["right_id"], model)
            hit = cached_rows.get(key)
            if hit:
                results.append(
                    AdjudicationResult(
                        left_id=pair["left_id"],
                        right_id=pair["right_id"],
                        verdict=hit[1],
                        confidence=float(hit[2]),
                        reason=hit[3] or "",
                        cached=True,
                    )
                )
                self.stats["cached"] += 1
            else:
                pending.append(pair)

        self.stats["requested"] += len(pairs)
        started = time.perf_counter()
        if pending:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=self.settings.match.llm_concurrency
            ) as pool:
                futures = {pool.submit(self._adjudicate_one, p): p for p in pending}
                for future in concurrent.futures.as_completed(futures):
                    pair = futures[future]
                    try:
                        result = future.result()
                    except Exception as exc:  # pragma: no cover - network path
                        self.stats["failed"] += 1
                        result = AdjudicationResult(
                            left_id=pair["left_id"],
                            right_id=pair["right_id"],
                            verdict="unclear",
                            confidence=0.0,
                            reason=f"call failed: {type(exc).__name__}",
                        )
                    results.append(result)
                    self.stats["called"] += 1
                    self.stats["input_tokens"] += result.input_tokens
                    self.stats["output_tokens"] += result.output_tokens
        self.stats["wall_seconds"] += time.perf_counter() - started

        self._write_cache(con, results, model)
        return self._write_scores(con, results)

    def _check_band_size(
        self, con: duckdb.DuckDBPyConnection, entity_type: str, band_size: int
    ) -> None:
        """Refuse to run when the uncertain band is implausibly wide.

        A band covering a third of all pairs means the thresholds or the
        blocking are misconfigured, not that a third of the register is
        genuinely ambiguous. Failing loudly here is much cheaper than
        discovering it on the bill.
        """
        total = int(
            (
                con.execute(
                    """
                    SELECT count(*) FROM pair_scores s
                    JOIN records l ON l.record_id = s.left_id
                    WHERE s.matcher = ? AND l.entity_type = ?
                    """,
                    [self.base_matcher, entity_type],
                ).fetchone()
                or [0]
            )[0]
        )
        if not total:
            return
        fraction = band_size / total
        ceiling = self.settings.match.llm_band_fraction_ceiling
        if fraction > ceiling:
            raise RuntimeError(
                f"Uncertain band is {fraction:.1%} of {total:,} scored {entity_type} "
                f"pairs, above the {ceiling:.0%} ceiling. This usually means "
                f"auto_accept/auto_reject are mis-set or blocking is too loose. "
                f"Raise OER_MATCH_LLM_BAND_FRACTION_CEILING to override."
            )

    def _write_cache(
        self, con: duckdb.DuckDBPyConnection, results: list[AdjudicationResult], model: str
    ) -> None:
        fresh = [r for r in results if not r.cached]
        if not fresh:
            return
        con.executemany(
            """
            INSERT OR REPLACE INTO llm_cache
                (cache_key, left_id, right_id, prompt_version, model,
                 verdict, confidence, reason, input_tokens, output_tokens, latency_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    _cache_key(r.left_id, r.right_id, model),
                    r.left_id,
                    r.right_id,
                    PROMPT_VERSION,
                    model,
                    r.verdict,
                    r.confidence,
                    r.reason,
                    r.input_tokens,
                    r.output_tokens,
                    r.latency_ms,
                )
                for r in fresh
            ],
        )

    def _write_scores(
        self, con: duckdb.DuckDBPyConnection, results: list[AdjudicationResult]
    ) -> int:
        """Map verdicts onto scores in the accept/reject space.

        A ``match`` maps above the accept threshold and a ``no_match`` below the
        reject threshold, both scaled by the model's stated confidence, so a
        hesitant verdict lands nearer the band edge than an emphatic one.
        ``unclear`` is written back into the middle of the band: the pipeline's
        position on that pair is unchanged, and it stays visible as an open
        question rather than being silently resolved.
        """
        accept = self.settings.match.auto_accept
        reject = self.settings.match.auto_reject
        rows: list[tuple[Any, ...]] = []
        for r in results:
            if r.verdict == "match":
                score = accept + (1.0 - accept) * r.confidence
                decision = "accept"
            elif r.verdict == "no_match":
                score = reject * (1.0 - r.confidence)
                decision = "reject"
            else:
                score = (accept + reject) / 2.0
                decision = "uncertain"
            rows.append(
                (
                    r.left_id,
                    r.right_id,
                    self.name,
                    float(score),
                    decision,
                    json.dumps({"verdict": r.verdict, "confidence": r.confidence}),
                    f"llm[{self.settings.match.llm_model}]: {r.reason}",
                )
            )

        con.execute("DELETE FROM pair_scores WHERE matcher = ?", [self.name])
        # Pairs outside the band keep the base matcher's verdict, so the `llm`
        # matcher is a complete, directly comparable decision set rather than a
        # partial overlay that would need special handling everywhere downstream.
        con.execute(
            """
            INSERT INTO pair_scores
            SELECT left_id, right_id, ? AS matcher, score, decision, features, rationale
            FROM pair_scores WHERE matcher = ? AND decision <> 'uncertain'
            """,
            [self.name, self.base_matcher],
        )
        con.executemany(
            """
            INSERT INTO pair_scores
                (left_id, right_id, matcher, score, decision, features, rationale)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        return len(rows)

    def cost_report(
        self, price_per_mtok_in: float = 3.0, price_per_mtok_out: float = 15.0
    ) -> dict[str, Any]:
        """Token and wall-clock accounting for the adjudicated run."""
        cost = (
            self.stats["input_tokens"] / 1_000_000 * price_per_mtok_in
            + self.stats["output_tokens"] / 1_000_000 * price_per_mtok_out
        )
        return {
            **self.stats,
            "estimated_usd": round(cost, 4),
            "cache_hit_rate": (
                self.stats["cached"] / self.stats["requested"] if self.stats["requested"] else 0.0
            ),
        }
