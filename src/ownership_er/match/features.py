"""Pairwise comparison features, computed in SQL.

Features are expressed as DuckDB SQL rather than Python callables. That is a
deliberate performance decision: at full scale the candidate set is ~10^8 pairs,
and materialising those as Python objects to run string comparisons row by row
takes hours, where the same comparisons as vectorised SQL over a columnar
engine take minutes. DuckDB ships ``jaro_winkler_similarity``, ``levenshtein``
and ``damerau_levenshtein`` natively, which covers everything the matcher needs.

Three-valued logic
------------------
Every feature that can be unobserved returns ``NULL`` rather than ``0`` when
either side is missing. The distinction is load-bearing. "Both records state a
nationality and they differ" is strong evidence *against* a match; "one record
omits nationality" is no evidence either way. Collapsing the two into a single
zero teaches the model that missing data implies non-match, and since missingness
is not random — foreign filers omit different fields than UK ones — that bias
lands disproportionately on exactly the cross-border records the pipeline exists
to resolve.

The scorers below therefore consume ``NULL`` explicitly: the rule matcher skips
the term and renormalises, and Splink models it as its own comparison level.
"""

from __future__ import annotations

__all__ = ["COMPANY_FEATURES", "FEATURE_NAMES", "PERSON_FEATURES", "pair_feature_sql"]


# Token-level Jaccard. DuckDB's built-in `jaccard` is character-bigram based,
# which scores "James Whitfield" against "Whitfield James" as merely similar;
# on tokens it is correctly 1.0, and word order is precisely the variation this
# feature is meant to be blind to.
_TOKEN_JACCARD = """
    CASE
        WHEN l.{col} = '' OR r.{col} = '' THEN NULL
        ELSE (
            len(list_intersect(str_split(l.{col}, ' '), str_split(r.{col}, ' ')))::DOUBLE
            / nullif(len(list_distinct(
                list_concat(str_split(l.{col}, ' '), str_split(r.{col}, ' ')))), 0)
        )
    END
"""


def _jw(col: str) -> str:
    return f"""
    CASE WHEN l.{col} = '' OR r.{col} = '' THEN NULL
         ELSE jaro_winkler_similarity(l.{col}, r.{col}) END
    """


def _exact(col: str) -> str:
    return f"""
    CASE WHEN l.{col} = '' OR r.{col} = '' THEN NULL
         WHEN l.{col} = r.{col} THEN 1.0 ELSE 0.0 END
    """


def _exact_int(col: str) -> str:
    return f"""
    CASE WHEN l.{col} IS NULL OR r.{col} IS NULL THEN NULL
         WHEN l.{col} = r.{col} THEN 1.0 ELSE 0.0 END
    """


# Middle names are compatible when either is absent, when they are equal, or
# when one is the initial of the other. Absence is compatibility, not evidence:
# the same person's filings routinely disagree on whether a middle name was
# supplied at all, so treating "present vs absent" as a mismatch would penalise
# the most common true-match pattern in the register.
_MIDDLE_COMPAT = """
    CASE
        WHEN l.middle_name = '' OR r.middle_name = '' THEN NULL
        WHEN l.middle_name = r.middle_name THEN 1.0
        WHEN length(l.middle_name) = 1 AND starts_with(r.middle_name, l.middle_name) THEN 1.0
        WHEN length(r.middle_name) = 1 AND starts_with(l.middle_name, r.middle_name) THEN 1.0
        ELSE 0.0
    END
"""

# Postcode agreement is graded. A full match is strong; sharing only the
# outward district is weak but real, and is what survives a house move within
# the same area or a filer abbreviating the code.
_POSTCODE_LEVEL = """
    CASE
        WHEN l.postcode = '' OR r.postcode = '' THEN NULL
        WHEN l.postcode = r.postcode THEN 1.0
        WHEN split_part(l.postcode, ' ', 1) = split_part(r.postcode, ' ', 1) THEN 0.5
        ELSE 0.0
    END
"""

# Cross-name comparison: catches records where forename and surname were
# entered in opposite fields, which is common for names that do not follow
# Anglophone ordering and would otherwise look like a total mismatch.
_CROSS_NAME = """
    CASE
        WHEN l.first_name = '' OR r.first_name = ''
          OR l.last_name = '' OR r.last_name = '' THEN NULL
        ELSE greatest(
            (jaro_winkler_similarity(l.first_name, r.last_name)
             + jaro_winkler_similarity(l.last_name, r.first_name)) / 2.0,
            (jaro_winkler_similarity(l.first_name, r.first_name)
             + jaro_winkler_similarity(l.last_name, r.last_name)) / 2.0
        )
    END
"""

PERSON_FEATURES: dict[str, str] = {
    "name_fp_jw": _jw("name_fp"),
    "name_token_jaccard": _TOKEN_JACCARD.format(col="name_norm"),
    "last_jw": _jw("last_name"),
    "first_jw": _jw("first_name"),
    "cross_name_jw": _CROSS_NAME,
    "phonetic_exact": _exact("name_phonetic"),
    "birth_year_match": _exact_int("birth_year"),
    "birth_month_match": _exact_int("birth_month"),
    "middle_compat": _MIDDLE_COMPAT,
    "nationality_match": _exact("nationality"),
    "country_match": _exact("country"),
    "postcode_level": _POSTCODE_LEVEL,
    "address_jaccard": _TOKEN_JACCARD.format(col="address_norm"),
    "address_blk_match": _exact("address_blk"),
}

COMPANY_FEATURES: dict[str, str] = {
    "name_fp_jw": _jw("name_fp"),
    "name_token_jaccard": _TOKEN_JACCARD.format(col="name_norm"),
    "phonetic_exact": _exact("name_phonetic"),
    "reg_number_match": _exact("reg_number"),
    "jurisdiction_match": _exact("jurisdiction"),
    "country_match": _exact("country"),
    "postcode_level": _POSTCODE_LEVEL,
    "address_jaccard": _TOKEN_JACCARD.format(col="address_norm"),
    "legal_form_match": _exact("legal_form"),
    "incorporation_match": """
        CASE WHEN l.incorporation_date IS NULL OR r.incorporation_date IS NULL THEN NULL
             WHEN l.incorporation_date = r.incorporation_date THEN 1.0 ELSE 0.0 END
    """,
}

FEATURE_NAMES: dict[str, list[str]] = {
    "Person": list(PERSON_FEATURES),
    "Company": list(COMPANY_FEATURES),
}


def pair_feature_sql(entity_type: str, *, pair_source: str = "candidate_pairs") -> str:
    """Return SQL computing every feature for candidate pairs of one entity type.

    Pairs are restricted to same-type comparisons. Cross-type pairs (a Person
    against a Company) are never true matches under this schema, and generating
    them would only add noise for the matcher to reject.
    """
    features = PERSON_FEATURES if entity_type == "Person" else COMPANY_FEATURES
    selects = ",\n        ".join(f"({expr.strip()}) AS {name}" for name, expr in features.items())
    return f"""
    SELECT
        p.left_id,
        p.right_id,
        p.n_keys,
        p.block_keys,
        l.entity_type AS entity_type,
        {selects}
    FROM {pair_source} p
    JOIN records l ON l.record_id = p.left_id
    JOIN records r ON r.record_id = p.right_id
    WHERE l.entity_type = '{entity_type}' AND r.entity_type = '{entity_type}'
    """
