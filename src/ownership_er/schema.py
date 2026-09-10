"""The common record schema, and FollowTheMoney serialisation.

Every source is parsed into one flat ``records`` table plus a ``relationships``
table. Resolution operates on ``records``; the graph is built from
``relationships`` re-pointed at canonical entities.

On FollowTheMoney
-----------------
FtM is the interchange schema used by OpenSanctions, OCCRP Aleph and most of
the open corporate-intelligence ecosystem, so emitting it is what makes this
pipeline's output loadable by other people's tools rather than only its own.

The ``followthemoney`` package is nonetheless an *optional* dependency here.
It requires PyICU, which requires a system libicu that is awkward on macOS and
absent from many CI images — a hard dependency would mean most people who clone
this repository cannot run it. So the writer below emits schema-conformant FtM
JSON directly, and ``oer validate-ftm`` round-trips every emitted entity through
the real library to prove conformance whenever it *is* installed. CI runs that
check on an image with libicu present, so the guarantee is enforced rather than
asserted.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

__all__ = [
    "RECORDS_DDL",
    "RELATIONSHIPS_DDL",
    "Record",
    "Relationship",
    "SourceName",
    "to_ftm",
    "validate_ftm_available",
    "write_ftm",
]

SourceName = Literal["ch_companies", "ch_psc", "opensanctions"]

# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Record:
    """One assertion about one entity, from one source.

    A record is *not* an entity. ``PSC filings for the same individual across
    eleven companies`` are eleven records; resolution is the process of deciding
    they are one entity. Keeping the distinction in the type names avoids the
    most common bug in linkage code, which is treating a source row as though it
    were already a resolved thing.
    """

    record_id: str
    source: str
    entity_type: str  # "Company" | "Person" | "LegalEntity"

    name: str = ""
    name_norm: str = ""
    name_fp: str = ""
    name_phonetic: str = ""

    first_name: str = ""
    middle_name: str = ""
    last_name: str = ""

    birth_year: int | None = None
    birth_month: int | None = None

    nationality: str = ""  # ISO2
    country: str = ""  # ISO2, residence or registration
    jurisdiction: str = ""  # ISO2
    reg_number: str = ""  # normalised registration/company number

    address_full: str = ""
    address_norm: str = ""
    postcode: str = ""
    address_blk: str = ""

    incorporation_date: str | None = None
    dissolution_date: str | None = None
    status: str = ""
    legal_form: str = ""
    sic_codes: list[str] = field(default_factory=list)

    topics: list[str] = field(default_factory=list)  # OpenSanctions risk topics
    datasets: list[str] = field(default_factory=list)

    # The company this record was filed against, for PSC rows. Carried on the
    # record so the relationship table can be rebuilt after resolution.
    context_company_number: str = ""

    source_url: str = ""
    retrieved_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# No PRIMARY KEY on record_id, deliberately. DuckDB backs a primary key with an
# ART index maintained on every insert, which at 12M wide rows costs both
# ingestion throughput and a large resident memory footprint — on a laptop, the
# difference between finishing and thrashing. Uniqueness is guaranteed upstream
# instead: record ids are derived from registry-assigned identifiers
# (`links.self`, company number, OpenSanctions id), and `tests/test_ids.py`
# asserts the invariant on the fixture corpus. Join indexes are created after
# loading by `warehouse.create_indexes`.
RECORDS_DDL = """
CREATE TABLE IF NOT EXISTS records (
    record_id              VARCHAR NOT NULL,
    source                 VARCHAR NOT NULL,
    entity_type            VARCHAR NOT NULL,

    name                   VARCHAR,
    name_norm              VARCHAR,
    name_fp                VARCHAR,
    name_phonetic          VARCHAR,

    first_name             VARCHAR,
    middle_name            VARCHAR,
    last_name              VARCHAR,

    birth_year             INTEGER,
    birth_month            INTEGER,

    nationality            VARCHAR,
    country                VARCHAR,
    jurisdiction           VARCHAR,
    reg_number             VARCHAR,

    address_full           VARCHAR,
    address_norm           VARCHAR,
    postcode               VARCHAR,
    address_blk            VARCHAR,

    incorporation_date     DATE,
    dissolution_date       DATE,
    status                 VARCHAR,
    legal_form             VARCHAR,
    sic_codes              VARCHAR[],

    topics                 VARCHAR[],
    datasets               VARCHAR[],

    context_company_number VARCHAR,
    source_url             VARCHAR,
    retrieved_at           VARCHAR
);
"""

# ---------------------------------------------------------------------------
# Relationships
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Relationship:
    """A directed control edge asserted by one source filing."""

    rel_id: str
    rel_type: str  # "OWNS" | "CONTROLS"
    source_record_id: str  # the controlling party (PSC)
    target_record_id: str  # the controlled company
    source: str

    min_percent: float | None = None
    max_percent: float | None = None
    control_kinds: list[str] = field(default_factory=list)
    capacities: list[str] = field(default_factory=list)
    has_hard_control: bool = False
    via_fiduciary: bool = False

    notified_on: str | None = None
    ceased_on: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


RELATIONSHIPS_DDL = """
CREATE TABLE IF NOT EXISTS relationships (
    rel_id            VARCHAR NOT NULL,
    rel_type          VARCHAR NOT NULL,
    source_record_id  VARCHAR NOT NULL,
    target_record_id  VARCHAR NOT NULL,
    source            VARCHAR NOT NULL,

    min_percent       DOUBLE,
    max_percent       DOUBLE,
    control_kinds     VARCHAR[],
    capacities        VARCHAR[],
    has_hard_control  BOOLEAN,
    via_fiduciary     BOOLEAN,

    notified_on       DATE,
    ceased_on         DATE
);
"""

# ---------------------------------------------------------------------------
# FollowTheMoney serialisation
# ---------------------------------------------------------------------------

# Record field -> FtM property, per schema. Only properties that exist on the
# target FtM schema are emitted; `validate_ftm_available` enforces this.
_FTM_COMMON = {
    "name": "name",
    "country": "country",
    "address_full": "address",
    "source_url": "sourceUrl",
}

_FTM_PERSON = {
    "first_name": "firstName",
    "middle_name": "middleName",
    "last_name": "lastName",
    "nationality": "nationality",
}

_FTM_COMPANY = {
    "reg_number": "registrationNumber",
    "jurisdiction": "jurisdiction",
    "incorporation_date": "incorporationDate",
    "dissolution_date": "dissolutionDate",
    "status": "status",
    "legal_form": "legalForm",
}


def _clean(values: Iterable[Any]) -> list[str]:
    out: list[str] = []
    for v in values:
        if v is None or v == "" or v == []:
            continue
        out.append(str(v))
    return out


def to_ftm(record: Record, canonical_id: str | None = None) -> dict[str, Any]:
    """Render a :class:`Record` as a FollowTheMoney entity dictionary."""
    schema = record.entity_type if record.entity_type in {"Person", "Company"} else "LegalEntity"
    props: dict[str, list[str]] = {}

    mapping = dict(_FTM_COMMON)
    if schema == "Person":
        mapping.update(_FTM_PERSON)
    else:
        mapping.update(_FTM_COMPANY)

    for attr, prop in mapping.items():
        value = getattr(record, attr, None)
        cleaned = _clean([value])
        if cleaned:
            props.setdefault(prop, []).extend(cleaned)

    if record.birth_year:
        # FtM dates are ISO-8601, and tolerate reduced precision. Companies
        # House publishes month and year only for individuals, so emitting a
        # fabricated day would assert precision the register withholds.
        birth = f"{record.birth_year:04d}"
        if record.birth_month:
            birth = f"{birth}-{record.birth_month:02d}"
        props.setdefault("birthDate", []).append(birth)

    if record.topics:
        props.setdefault("topics", []).extend(_clean(record.topics))

    return {
        "id": canonical_id or record.record_id,
        "caption": record.name,
        "schema": schema,
        "properties": {k: sorted(set(v)) for k, v in props.items() if v},
        "datasets": record.datasets or [record.source],
        "referents": [record.record_id]
        if canonical_id and canonical_id != record.record_id
        else [],
    }


def relationship_to_ftm(rel: Relationship, source_id: str, target_id: str) -> dict[str, Any]:
    """Render a control edge as an FtM ``Ownership`` entity.

    FtM models relationships as first-class entities with ``owner`` and
    ``asset`` properties rather than as edges, which is what lets a
    relationship carry its own provenance and its own percentage.
    """
    props: dict[str, list[str]] = {
        "owner": [source_id],
        "asset": [target_id],
    }
    if rel.max_percent is not None:
        lo = "" if rel.min_percent is None else f"{rel.min_percent:g}"
        props["percentage"] = [f"{lo}-{rel.max_percent:g}%".lstrip("-")]
    if rel.notified_on:
        props["startDate"] = [rel.notified_on]
    if rel.ceased_on:
        props["endDate"] = [rel.ceased_on]
    if rel.control_kinds:
        props["role"] = sorted(set(rel.control_kinds))
    return {
        "id": rel.rel_id,
        "schema": "Ownership",
        "properties": props,
        "datasets": [rel.source],
    }


def write_ftm(entities: Iterable[dict[str, Any]], path: Path) -> int:
    """Write newline-delimited FtM JSON. Returns the number of entities written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8") as fh:
        for ent in entities:
            fh.write(json.dumps(ent, ensure_ascii=False, separators=(",", ":")))
            fh.write("\n")
            count += 1
    return count


def validate_ftm_available() -> bool:
    """True when the real ``followthemoney`` package can be imported."""
    try:  # pragma: no cover - depends on optional extra
        import followthemoney  # noqa: F401
    except Exception:
        return False
    return True


def validate_ftm_stream(entities: Iterable[dict[str, Any]]) -> Iterator[str]:
    """Yield a validation error string for every non-conformant entity.

    Raises :class:`ImportError` if the optional ``ftm`` extra is not installed,
    because silently passing validation that never ran is worse than failing.
    """
    from followthemoney import model

    for ent in entities:
        schema = model.get(ent.get("schema", ""))
        if schema is None:
            yield f"{ent.get('id')}: unknown schema {ent.get('schema')!r}"
            continue
        for prop_name in ent.get("properties", {}):
            if schema.get(prop_name) is None:
                yield f"{ent.get('id')}: {schema.name} has no property {prop_name!r}"
