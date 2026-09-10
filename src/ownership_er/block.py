"""Candidate generation (blocking).

Comparing every pair of records is quadratic: 12M PSC filings is 7.4 x 10^13
pairs, which is not a tuning problem but an impossibility. Blocking replaces
that with a union of cheap equality joins on derived keys, and the whole
pipeline's recall ceiling is set here — a true pair that never appears in a
block cannot be recovered by any matcher downstream, however good.

The design follows three rules.

**Multiple weak keys, unioned, beat one strong key.** Each key has a blind
spot: a name-fingerprint key misses transliteration variants, a phonetic key
misses name-order swaps, a date-of-birth key misses records where the registrar
withheld it. Their blind spots are largely independent, so the union recovers
far more than the best single key while each stays cheap.

**Every key is reported, not just used.** :func:`blocking_report` computes pair
completeness (what share of known true pairs a key generates) and reduction
ratio (how much of the quadratic space it eliminates) per key, so the choice of
keys is an evidenced decision rather than folklore. Keys that cost pairs without
adding completeness get dropped on the numbers.

**Degenerate blocks are capped and surfaced.** A key like postcode has a long
tail: one formation-agent address in London carries tens of thousands of
companies, and that single block would emit more pairs than the rest of the
dataset combined. Blocks above ``max_block_size`` are excluded and listed in the
report, because silently truncating them would hide a recall loss.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import duckdb

__all__ = [
    "COMPANY_KEYS",
    "PERSON_KEYS",
    "BlockingKey",
    "blocking_report",
    "build_blocking_keys",
    "generate_candidate_pairs",
]


@dataclass(frozen=True, slots=True)
class BlockingKey:
    """One blocking strategy, expressed as a SQL value over ``records``."""

    name: str
    entity_type: str  # "Person" | "Company"
    expression: str  # SQL producing the key, or NULL to skip a record
    rationale: str
    max_block_size: int = 500


# ---------------------------------------------------------------------------
# Person keys
# ---------------------------------------------------------------------------

PERSON_KEYS: tuple[BlockingKey, ...] = (
    BlockingKey(
        name="p_fp_dob",
        entity_type="Person",
        expression="""
            CASE WHEN name_fp <> '' AND birth_year IS NOT NULL
                 THEN name_fp || '|' || CAST(birth_year AS VARCHAR) END
        """,
        rationale=(
            "Sorted first+last name with birth year. The highest-precision key: "
            "survives name-order swaps and title variation, and birth year makes "
            "common-name collisions rare."
        ),
        max_block_size=200,
    ),
    BlockingKey(
        name="p_fp",
        entity_type="Person",
        expression="CASE WHEN name_fp <> '' THEN name_fp END",
        rationale=(
            "Sorted name without birth year. Recovers the ~5% of filings where "
            "date of birth is absent, at the cost of larger blocks for common names."
        ),
        max_block_size=300,
    ),
    BlockingKey(
        name="p_phon_dob",
        entity_type="Person",
        expression="""
            CASE WHEN name_phonetic <> '' AND birth_year IS NOT NULL
                 THEN name_phonetic || '|' || CAST(birth_year AS VARCHAR) END
        """,
        rationale=(
            "Metaphone of first+last with birth year. This is the transliteration "
            "key: Shevchenko/Schevchenko and Kowalczyk/Kowalchyk collide phonetically "
            "but not lexically, so no edit-distance key recovers them."
        ),
        max_block_size=200,
    ),
    BlockingKey(
        name="p_last_dob",
        entity_type="Person",
        expression="""
            CASE WHEN last_name <> '' AND birth_year IS NOT NULL
                 THEN last_name || '|' || CAST(birth_year AS VARCHAR) END
        """,
        rationale=(
            "Surname with birth year. Catches forename variants that are neither "
            "lexically nor phonetically close — Yevgeniy/Eugene, Katarzyna/Catherine — "
            "which are anglicisations rather than transcriptions."
        ),
        max_block_size=200,
    ),
    BlockingKey(
        name="p_first_dob",
        entity_type="Person",
        expression="""
            CASE WHEN first_name <> '' AND birth_year IS NOT NULL
                 THEN first_name || '|' || CAST(birth_year AS VARCHAR) END
        """,
        rationale=(
            "Forename with birth year — the mirror of p_last_dob, and added "
            "*because the evaluation demanded it*, not on intuition. The first "
            "error taxonomy showed every remaining false negative was lost at "
            "blocking rather than matching, and the misses were dominated by "
            "pairs whose surnames were transliterated differently while the "
            "forename was stable (Petrov/Petroff, Sokolov/Sokoloff). No "
            "surname-anchored key can reach those. Adding this key lifted "
            "blocking recall from 0.840 to 0.925 and B-cubed F1 from 0.972 to "
            "0.987, for 281 extra candidate pairs on the fixture corpus; "
            "docs/evaluation.md carries the before-and-after."
        ),
        max_block_size=200,
    ),
    BlockingKey(
        name="p_last_init_pc",
        entity_type="Person",
        expression="""
            CASE WHEN last_name <> '' AND postcode <> ''
                 THEN last_name || '|' || postcode END
        """,
        rationale=(
            "Surname with postcode. The key for records missing a birth year "
            "entirely; a shared residential postcode is highly selective."
        ),
        max_block_size=100,
    ),
    BlockingKey(
        name="p_dob_pc",
        entity_type="Person",
        expression="""
            CASE WHEN birth_year IS NOT NULL AND postcode <> ''
                 THEN CAST(birth_year AS VARCHAR) || '|' || postcode END
        """,
        rationale=(
            "Birth year with postcode, ignoring the name entirely. The only key "
            "that survives a wholesale name change — marriage, transliteration of "
            "both name parts, or a filer entering a different legal name."
        ),
        max_block_size=100,
    ),
)

# ---------------------------------------------------------------------------
# Company keys
# ---------------------------------------------------------------------------

COMPANY_KEYS: tuple[BlockingKey, ...] = (
    BlockingKey(
        name="c_reg",
        entity_type="Company",
        expression="CASE WHEN reg_number <> '' THEN reg_number END",
        rationale=(
            "Registration number. Near-deterministic where present, and the basis "
            "of the held-out evaluation: corporate PSC filings that state a UK "
            "company number provide labelled links for free."
        ),
        max_block_size=50,
    ),
    BlockingKey(
        name="c_fp",
        entity_type="Company",
        expression="CASE WHEN name_fp <> '' THEN name_fp END",
        rationale=(
            "Sorted name tokens with the legal form stripped. Collapses "
            "'OOO Severstal' and 'Severstal, O.A.O.' — prefix-form and suffix-form "
            "conventions that a suffix-only strip leaves in different blocks."
        ),
        max_block_size=300,
    ),
    BlockingKey(
        name="c_phon",
        entity_type="Company",
        expression="CASE WHEN name_phonetic <> '' THEN name_phonetic END",
        rationale="Metaphone of the base name, for transcription variants of foreign parents.",
        max_block_size=300,
    ),
    BlockingKey(
        name="c_head_pc",
        entity_type="Company",
        expression="""
            CASE WHEN name_fp <> '' AND postcode <> ''
                 THEN split_part(name_fp, ' ', 1) || '|' || postcode END
        """,
        rationale=(
            "First name token with registered postcode. Catches renamed or "
            "abbreviated companies at a stable address."
        ),
        max_block_size=100,
    ),
    BlockingKey(
        name="c_addr",
        entity_type="Company",
        expression="""
            CASE WHEN address_blk <> '' AND name_fp <> ''
                 THEN address_blk || '|' || substr(name_fp, 1, 4) END
        """,
        rationale=(
            "Building-level address with a name prefix. Address alone is unusable "
            "as a key — formation agents register tens of thousands of companies at "
            "one address — so it is always paired with a name fragment."
        ),
        max_block_size=100,
    ),
)

ALL_KEYS: tuple[BlockingKey, ...] = PERSON_KEYS + COMPANY_KEYS


# ---------------------------------------------------------------------------
# Key materialisation
# ---------------------------------------------------------------------------


def build_blocking_keys(
    con: duckdb.DuckDBPyConnection,
    keys: tuple[BlockingKey, ...] = ALL_KEYS,
) -> dict[str, int]:
    """Materialise ``(record_id, key_name, key_value)`` for every key.

    Materialised rather than computed inline in the join, for two reasons: the
    per-key statistics the report needs are a simple aggregate over this table,
    and the key values are exactly the partition keys a distributed rewrite
    would shuffle on, so the migration path to Spark is already expressed.
    """
    con.execute("DROP TABLE IF EXISTS blocking_keys")
    con.execute(
        """
        CREATE TABLE blocking_keys (
            record_id   VARCHAR NOT NULL,
            key_name    VARCHAR NOT NULL,
            key_value   VARCHAR NOT NULL,
            entity_type VARCHAR NOT NULL
        )
        """
    )
    counts: dict[str, int] = {}
    for key in keys:
        con.execute(
            f"""
            INSERT INTO blocking_keys
            SELECT record_id, '{key.name}', {key.expression}, entity_type
            FROM records
            WHERE entity_type = '{key.entity_type}'
              AND ({key.expression}) IS NOT NULL
              AND ({key.expression}) <> ''
            """
        )
        row = con.execute(
            "SELECT count(*) FROM blocking_keys WHERE key_name = ?", [key.name]
        ).fetchone()
        counts[key.name] = int(row[0]) if row else 0

    con.execute("CREATE INDEX idx_bk_value ON blocking_keys (key_name, key_value)")
    return counts


def _oversized_blocks_cte(keys: tuple[BlockingKey, ...]) -> str:
    """SQL identifying blocks that exceed their key's size cap."""
    cases = " ".join(f"WHEN '{k.name}' THEN {k.max_block_size}" for k in keys)
    return f"""
        block_sizes AS (
            SELECT key_name, key_value, count(*) AS n
            FROM blocking_keys
            GROUP BY 1, 2
        ),
        caps AS (
            SELECT key_name, key_value, n,
                   CASE key_name {cases} ELSE 250 END AS cap
            FROM block_sizes
        ),
        usable AS (
            SELECT key_name, key_value FROM caps WHERE n <= cap AND n > 1
        )
    """


def generate_candidate_pairs(
    con: duckdb.DuckDBPyConnection,
    keys: tuple[BlockingKey, ...] = ALL_KEYS,
) -> dict[str, Any]:
    """Emit de-duplicated candidate pairs into ``candidate_pairs``.

    Pairs are ordered ``left_id < right_id`` so that a pair found by three
    different keys is one row carrying three key names, not three rows. The key
    list is retained because agreement across independent keys is itself
    evidence, and the matcher uses it as a feature.
    """
    con.execute("DELETE FROM candidate_pairs")
    con.execute(
        f"""
        WITH {_oversized_blocks_cte(keys)}
        , pairs AS (
            SELECT
                least(a.record_id, b.record_id)    AS left_id,
                greatest(a.record_id, b.record_id) AS right_id,
                a.key_name
            FROM blocking_keys a
            JOIN blocking_keys b
              ON a.key_name = b.key_name
             AND a.key_value = b.key_value
             AND a.record_id < b.record_id
            JOIN usable u
              ON u.key_name = a.key_name AND u.key_value = a.key_value
        )
        INSERT INTO candidate_pairs
        SELECT left_id, right_id, list(DISTINCT key_name) AS block_keys,
               count(DISTINCT key_name) AS n_keys
        FROM pairs
        GROUP BY left_id, right_id
        """
    )

    total = con.execute("SELECT count(*) FROM candidate_pairs").fetchone()
    skipped = con.execute(
        f"""
        WITH {_oversized_blocks_cte(keys)}
        SELECT count(*) AS n_blocks, coalesce(sum(n), 0) AS n_records
        FROM caps WHERE n > cap
        """
    ).fetchone()

    n_records = con.execute("SELECT count(*) FROM records").fetchone()
    n_rec = int(n_records[0]) if n_records else 0
    naive = n_rec * (n_rec - 1) // 2

    n_pairs = int(total[0]) if total else 0
    return {
        "candidate_pairs": n_pairs,
        "naive_pairs": naive,
        "reduction_ratio": (1.0 - n_pairs / naive) if naive else 0.0,
        "oversized_blocks": int(skipped[0]) if skipped else 0,
        "records_in_oversized_blocks": int(skipped[1]) if skipped else 0,
    }


def blocking_report(
    con: duckdb.DuckDBPyConnection,
    truth_pairs_table: str | None = None,
    keys: tuple[BlockingKey, ...] = ALL_KEYS,
) -> list[dict[str, Any]]:
    """Per-key statistics, including pair completeness when truth is available.

    ``truth_pairs_table`` must expose ``left_id`` and ``right_id`` ordered the
    same way as ``candidate_pairs``. Without it, only cost metrics are returned —
    completeness genuinely cannot be estimated from unlabelled data, and
    reporting a guess would be worse than reporting nothing.
    """
    rows: list[dict[str, Any]] = []
    by_name = {k.name: k for k in keys}

    stats = con.execute(
        f"""
        WITH {_oversized_blocks_cte(keys)}
        SELECT
            c.key_name,
            count(*)                                        AS n_blocks,
            sum(CASE WHEN c.n <= c.cap THEN 1 ELSE 0 END)   AS n_usable_blocks,
            sum(CASE WHEN c.n >  c.cap THEN 1 ELSE 0 END)   AS n_oversized,
            max(c.n)                                        AS largest_block,
            sum(CASE WHEN c.n <= c.cap AND c.n > 1
                     THEN c.n * (c.n - 1) / 2 ELSE 0 END)   AS emitted_pairs
        FROM caps c
        GROUP BY 1
        ORDER BY 1
        """
    ).fetchall()

    completeness: dict[str, float] = {}
    if truth_pairs_table:
        truth_total_row = con.execute(f"SELECT count(*) FROM {truth_pairs_table}").fetchone()
        truth_total = int(truth_total_row[0]) if truth_total_row else 0
        if truth_total:
            found = con.execute(
                f"""
                WITH {_oversized_blocks_cte(keys)}
                , key_pairs AS (
                    SELECT DISTINCT a.key_name,
                           least(a.record_id, b.record_id)    AS left_id,
                           greatest(a.record_id, b.record_id) AS right_id
                    FROM blocking_keys a
                    JOIN blocking_keys b
                      ON a.key_name = b.key_name
                     AND a.key_value = b.key_value
                     AND a.record_id < b.record_id
                    JOIN usable u
                      ON u.key_name = a.key_name AND u.key_value = a.key_value
                )
                SELECT kp.key_name, count(*) AS n_true
                FROM key_pairs kp
                JOIN {truth_pairs_table} t
                  ON t.left_id = kp.left_id AND t.right_id = kp.right_id
                GROUP BY 1
                """
            ).fetchall()
            completeness = {r[0]: int(r[1]) / truth_total for r in found}

    for key_name, n_blocks, n_usable, n_over, largest, emitted in stats:
        key = by_name.get(key_name)
        rows.append(
            {
                "key": key_name,
                "entity_type": key.entity_type if key else "",
                "blocks": int(n_blocks),
                "usable_blocks": int(n_usable or 0),
                "oversized_blocks": int(n_over or 0),
                "largest_block": int(largest or 0),
                "emitted_pairs": int(emitted or 0),
                "pair_completeness": completeness.get(key_name),
                "rationale": key.rationale if key else "",
            }
        )
    return rows
