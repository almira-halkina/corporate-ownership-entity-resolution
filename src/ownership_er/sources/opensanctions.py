"""Parsing the OpenSanctions consolidated dataset.

OpenSanctions publishes FollowTheMoney entities as newline-delimited JSON. Each
entity carries multi-valued properties, a list of contributing ``datasets``, and
``topics`` — the controlled vocabulary that says *why* the entity is of interest
(``sanction``, ``role.pep``, ``crime.fin``, ``export.control``, and so on).

Two source characteristics drive the parsing choices here.

Multi-valued names are the point, not a nuisance. A sanctioned individual
routinely carries a dozen ``name`` and ``alias`` values across transliteration
schemes — ``Yevgeniy``/``Evgeny``/``Evgenii`` — and it is precisely those
variants that let an alias match a UK filing where the canonical spelling would
not. Each name variant is therefore emitted as its own record, all sharing the
upstream entity id, so blocking gets every spelling and clustering collapses
them again at the end.

Birth dates arrive at mixed precision (``1975``, ``1975-06``, ``1975-06-14``).
Comparing them as strings would treat a year-only value as a mismatch against a
full date for the same person, so only the year and month are retained — the
precision Companies House publishes anyway.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any, TextIO

from ownership_er.normalize.addresses import address_key, normalize_address, normalize_postcode
from ownership_er.normalize.countries import code_country, code_nationality
from ownership_er.normalize.names import (
    company_fingerprint,
    detect_legal_form,
    normalize_text,
    person_fingerprint,
    person_name_parts,
    phonetic_key,
)
from ownership_er.schema import Record

__all__ = ["RISK_TOPICS", "SANCTION_TOPICS", "parse_opensanctions_jsonl"]

SOURCE = "opensanctions"

# Topics that constitute a sanctions nexus, as opposed to general interest.
SANCTION_TOPICS: frozenset[str] = frozenset(
    {"sanction", "sanction.linked", "sanction.counter", "export.control", "export.risk"}
)

# The wider risk vocabulary retained on canonical entities for the exposure
# analysis. Kept explicit so a topic-vocabulary change upstream surfaces as
# unmapped values rather than silently dropping risk.
RISK_TOPICS: frozenset[str] = SANCTION_TOPICS | frozenset(
    {
        "role.pep",
        "role.rca",
        "role.oligarch",
        "crime",
        "crime.fin",
        "crime.theft",
        "crime.war",
        "crime.terror",
        "crime.traffick",
        "crime.boss",
        "crime.fraud",
        "poi",
        "debarment",
        "asset.frozen",
        "wanted",
        "gov.soe",
        "gov.igo",
        "gov.national",
        "corp.shell",
        "corp.disqual",
    }
)

_PERSON_SCHEMATA = {"Person"}
_COMPANY_SCHEMATA = {"Company", "Organization", "PublicBody", "LegalEntity"}


def _first(props: dict[str, list[str]], key: str) -> str:
    values = props.get(key) or []
    return str(values[0]).strip() if values else ""


def _birth_parts(props: dict[str, list[str]]) -> tuple[int | None, int | None]:
    raw = _first(props, "birthDate")
    if not raw or len(raw) < 4 or not raw[:4].isdigit():
        return None, None
    year = int(raw[:4])
    month: int | None = None
    if len(raw) >= 7 and raw[5:7].isdigit():
        candidate = int(raw[5:7])
        if 1 <= candidate <= 12:
            month = candidate
    return year, month


def _name_variants(props: dict[str, list[str]], limit: int = 12) -> list[str]:
    """Ordered, de-duplicated name and alias values."""
    seen: set[str] = set()
    out: list[str] = []
    for key in ("name", "alias", "previousName", "weakAlias"):
        for value in props.get(key) or []:
            text = str(value).strip()
            norm = normalize_text(text)
            if not norm or norm in seen:
                continue
            seen.add(norm)
            out.append(text)
            if len(out) >= limit:
                return out
    return out


def _entity_records(obj: dict[str, Any], retrieved_at: str) -> Iterator[Record]:
    entity_id = str(obj.get("id") or "").strip()
    schema = str(obj.get("schema") or "").strip()
    if not entity_id or not schema:
        return
    if schema not in _PERSON_SCHEMATA and schema not in _COMPANY_SCHEMATA:
        return

    props: dict[str, list[str]] = obj.get("properties") or {}
    names = _name_variants(props)
    if not names:
        return

    is_person = schema in _PERSON_SCHEMATA
    entity_type = "Person" if is_person else "Company"

    address_full = normalize_address(*(props.get("address") or [])[:1])
    postcode = normalize_postcode(_first(props, "postalCode"))
    country = code_country(_first(props, "country") or _first(props, "jurisdiction"))
    topics = sorted({str(t) for t in (props.get("topics") or [])})
    datasets = [str(d) for d in (obj.get("datasets") or [SOURCE])]
    birth_year, birth_month = _birth_parts(props)

    for index, name in enumerate(names):
        # Variant 0 keeps the upstream id so the canonical entity inherits it;
        # later variants are suffixed and collapse back during clustering.
        record_id = f"os:{entity_id}" if index == 0 else f"os:{entity_id}#{index}"

        if is_person:
            first, middle, last = person_name_parts(name)
            yield Record(
                record_id=record_id,
                source=SOURCE,
                entity_type=entity_type,
                name=name,
                name_norm=normalize_text(name),
                name_fp=person_fingerprint(name),
                name_phonetic=phonetic_key(f"{first} {last}".strip()),
                first_name=first,
                middle_name=middle,
                last_name=last,
                birth_year=birth_year,
                birth_month=birth_month,
                nationality=code_nationality(_first(props, "nationality")),
                country=country,
                address_full=address_full,
                address_norm=address_full,
                postcode=postcode,
                address_blk=address_key(address_full),
                topics=topics,
                datasets=datasets,
                source_url=_first(props, "sourceUrl"),
                retrieved_at=retrieved_at,
            )
        else:
            base, form = detect_legal_form(name)
            yield Record(
                record_id=record_id,
                source=SOURCE,
                entity_type=entity_type,
                name=name,
                name_norm=normalize_text(name),
                name_fp=company_fingerprint(name),
                name_phonetic=phonetic_key(base),
                country=country,
                jurisdiction=code_country(_first(props, "jurisdiction")) or country,
                reg_number=_first(props, "registrationNumber"),
                address_full=address_full,
                address_norm=address_full,
                postcode=postcode,
                address_blk=address_key(address_full),
                incorporation_date=(_first(props, "incorporationDate") or None),
                dissolution_date=(_first(props, "dissolutionDate") or None),
                status=_first(props, "status"),
                legal_form=_first(props, "legalForm") or (form or ""),
                topics=topics,
                datasets=datasets,
                source_url=_first(props, "sourceUrl"),
                retrieved_at=retrieved_at,
            )


def _open_text(path: Path) -> TextIO:
    """Open plain or gzipped newline-delimited JSON as text.

    OpenSanctions serves both, and which one you get depends on the URL. Both
    are decoded with ``errors="replace"`` rather than strict: a single malformed
    byte in a multi-gigabyte feed should cost one character, not the run.
    """
    if path.suffix.lower() == ".gz":
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return path.open("r", encoding="utf-8", errors="replace")


def parse_opensanctions_jsonl(path: Path, retrieved_at: str | None = None) -> Iterator[Record]:
    """Yield one :class:`Record` per name variant of every in-scope entity."""
    stamp = retrieved_at or date.today().isoformat()
    with _open_text(path) as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            yield from _entity_records(obj, stamp)
