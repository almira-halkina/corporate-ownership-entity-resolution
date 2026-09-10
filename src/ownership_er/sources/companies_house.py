"""Parsing Companies House snapshots into the common record schema.

Two products are consumed:

``BasicCompanyDataAsOneFile-<date>.zip``
    Multi-part CSV, one row per registered company (~5.6M). Dates are
    ``DD/MM/YYYY``; several headers carry a leading space in the published file.

``persons-with-significant-control-snapshot-<date>.zip``
    Newline-delimited JSON, one object per PSC filing (~12M). Each object is
    ``{"company_number": ..., "data": {...}}`` where the shape of ``data``
    depends on ``kind``.

The ``kind`` discriminator matters more than it looks. Roughly one PSC record
in twenty is not a controlling party at all but a *statement* — "the company
knows of no person with significant control", "the company is exempt", or a
super-secure record where the individual's details are withheld for personal
safety. Parsing those as entities would inject millions of phantom people into
the resolution set, all with near-identical names. They are captured
separately, because the absence of a declared owner is itself a finding worth
counting.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ownership_er.normalize.addresses import address_key, normalize_address, normalize_postcode
from ownership_er.normalize.control import parse_natures
from ownership_er.normalize.countries import code_country, code_nationality
from ownership_er.normalize.names import (
    company_fingerprint,
    detect_legal_form,
    normalize_text,
    person_name_parts,
    phonetic_key,
)
from ownership_er.schema import Record, Relationship

__all__ = [
    "PSC_ENTITY_KINDS",
    "PSC_STATEMENT_KINDS",
    "iter_zip_members",
    "normalize_company_number",
    "parse_company_csv",
    "parse_psc_jsonl",
]

SOURCE_COMPANIES = "ch_companies"
SOURCE_PSC = "ch_psc"

PSC_ENTITY_KINDS = {
    "individual-person-with-significant-control": "Person",
    "corporate-entity-person-with-significant-control": "Company",
    "legal-person-person-with-significant-control": "LegalEntity",
}

PSC_STATEMENT_KINDS = {
    "persons-with-significant-control-statement",
    "super-secure-person-with-significant-control",
    "exemptions",
}


def normalize_company_number(value: str | None) -> str:
    """Zero-pad to the 8-character Companies House format.

    Filers enter ``1234567``, ``01234567`` and ``SC 123456`` for the same
    company. Without padding, the registration-number join — the strongest
    identifier in the dataset, and the basis of the evaluation's ground
    truth — silently misses a large share of true links.
    """
    if not value:
        return ""
    raw = "".join(ch for ch in str(value).upper() if ch.isalnum())
    if not raw:
        return ""
    if raw.isdigit():
        return raw.zfill(8)
    # Prefixed numbers (SC, NI, OC, SO, NC, FC, GB, IP, RS, ...) pad the digits.
    prefix = "".join(ch for ch in raw if ch.isalpha())
    digits = "".join(ch for ch in raw if ch.isdigit())
    if prefix and digits:
        return f"{prefix}{digits.zfill(8 - len(prefix))}"
    return raw


def _parse_ch_date(value: str | None) -> str | None:
    """Parse ``DD/MM/YYYY`` (CSV) or ``YYYY-MM-DD`` (JSON) to ISO, else ``None``."""
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _stable_suffix(*parts: Any) -> str:
    payload = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def iter_zip_members(path: Path, suffix: str) -> Iterator[tuple[str, io.TextIOWrapper]]:
    """Yield ``(member_name, text_stream)`` for members matching ``suffix``.

    Streams rather than extracting: the PSC snapshot is several GB uncompressed
    and there is no reason to require that much free disk to parse it.
    """
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            if info.is_dir() or not info.filename.lower().endswith(suffix):
                continue
            with zf.open(info) as raw:
                yield info.filename, io.TextIOWrapper(raw, encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Company product
# ---------------------------------------------------------------------------


def _company_record(row: dict[str, str], retrieved_at: str) -> Record:
    number = normalize_company_number(row.get("CompanyNumber"))
    name = (row.get("CompanyName") or "").strip()
    base, form = detect_legal_form(name)

    address_full = normalize_address(
        row.get("RegAddress.CareOf"),
        row.get("RegAddress.POBox"),
        row.get("RegAddress.AddressLine1"),
        row.get("RegAddress.AddressLine2"),
        row.get("RegAddress.PostTown"),
        row.get("RegAddress.County"),
        row.get("RegAddress.PostCode"),
    )
    postcode = normalize_postcode(row.get("RegAddress.PostCode"))

    sic = [(row.get(f"SICCode.SicText_{i}") or "").split(" - ")[0].strip() for i in range(1, 5)]

    return Record(
        record_id=f"ch:company:{number}",
        source=SOURCE_COMPANIES,
        entity_type="Company",
        name=name,
        name_norm=normalize_text(name),
        name_fp=company_fingerprint(name),
        name_phonetic=phonetic_key(base),
        nationality="",
        country=code_country(row.get("RegAddress.Country")) or "GB",
        jurisdiction="GB",
        reg_number=number,
        address_full=address_full,
        address_norm=address_full,
        postcode=postcode,
        address_blk=address_key(address_full),
        incorporation_date=_parse_ch_date(row.get("IncorporationDate")),
        dissolution_date=_parse_ch_date(row.get("DissolutionDate")),
        status=(row.get("CompanyStatus") or "").strip(),
        legal_form=form or (row.get("CompanyCategory") or "").strip(),
        sic_codes=[s for s in sic if s],
        datasets=[SOURCE_COMPANIES],
        source_url=(row.get("URI") or "").strip(),
        retrieved_at=retrieved_at,
    )


def parse_company_csv(path: Path, retrieved_at: str | None = None) -> Iterator[Record]:
    """Yield a :class:`Record` per company from the zipped or plain CSV product."""
    stamp = retrieved_at or date.today().isoformat()

    def _rows(stream: io.TextIOBase) -> Iterator[Record]:
        reader = csv.DictReader(stream)
        if reader.fieldnames:
            reader.fieldnames = [f.strip() for f in reader.fieldnames]
        for row in reader:
            clean = {(k or "").strip(): v for k, v in row.items()}
            if not (clean.get("CompanyNumber") or "").strip():
                continue
            yield _company_record(clean, stamp)

    if path.suffix.lower() == ".zip":
        for _name, stream in iter_zip_members(path, ".csv"):
            yield from _rows(stream)
    else:
        with path.open("r", encoding="utf-8", errors="replace", newline="") as fh:
            yield from _rows(fh)


# ---------------------------------------------------------------------------
# PSC product
# ---------------------------------------------------------------------------


def _psc_filing_id(company_number: str, data: dict[str, Any]) -> str:
    """Stable identifier for one PSC filing.

    ``links.self`` ends in a registry-assigned hash that is stable across daily
    snapshots, so it is preferred. Falling back to a content hash keeps the id
    deterministic for records where the link is absent, which matters because
    re-running the pipeline must not renumber records or every stored decision
    becomes unattributable.
    """
    link = ((data.get("links") or {}).get("self") or "").rstrip("/")
    if link:
        tail = link.rsplit("/", 1)[-1]
        if tail:
            return f"ch:psc:{company_number}:{tail}"
    return (
        f"ch:psc:{company_number}:"
        f"{_stable_suffix(data.get('name'), data.get('notified_on'), data.get('kind'))}"
    )


def _psc_record(company_number: str, data: dict[str, Any], retrieved_at: str) -> Record | None:
    kind = data.get("kind") or ""
    entity_type = PSC_ENTITY_KINDS.get(kind)
    if entity_type is None:
        return None

    record_id = _psc_filing_id(company_number, data)
    name = (data.get("name") or "").strip()
    addr = data.get("address") or {}
    address_full = normalize_address(
        addr.get("care_of"),
        addr.get("po_box"),
        addr.get("premises"),
        addr.get("address_line_1"),
        addr.get("address_line_2"),
        addr.get("locality"),
        addr.get("region"),
        addr.get("postal_code"),
        addr.get("country"),
    )
    postcode = normalize_postcode(addr.get("postal_code"))

    common: dict[str, Any] = {
        "record_id": record_id,
        "source": SOURCE_PSC,
        "entity_type": entity_type,
        "name": name,
        "name_norm": normalize_text(name),
        "address_full": address_full,
        "address_norm": address_full,
        "postcode": postcode,
        "address_blk": address_key(address_full),
        "context_company_number": company_number,
        "datasets": [SOURCE_PSC],
        "source_url": ((data.get("links") or {}).get("self") or ""),
        "retrieved_at": retrieved_at,
    }

    if entity_type == "Person":
        elements = data.get("name_elements") or {}
        first, middle, last = person_name_parts(
            name,
            elements.get("forename"),
            elements.get("middle_name"),
            elements.get("surname"),
        )
        dob = data.get("date_of_birth") or {}
        return Record(
            **common,
            name_fp=" ".join(sorted(t for t in (first, last) if t)),
            name_phonetic=phonetic_key(f"{first} {last}".strip()),
            first_name=first,
            middle_name=middle,
            last_name=last,
            birth_year=int(dob["year"]) if dob.get("year") else None,
            birth_month=int(dob["month"]) if dob.get("month") else None,
            nationality=code_nationality(data.get("nationality")),
            country=code_country(data.get("country_of_residence")),
        )

    ident = data.get("identification") or {}
    reg_number = ident.get("registration_number")
    registered_in = code_country(ident.get("country_registered") or ident.get("place_registered"))
    base, form = detect_legal_form(name)
    return Record(
        **common,
        name_fp=company_fingerprint(name),
        name_phonetic=phonetic_key(base),
        country=registered_in or code_country(addr.get("country")),
        jurisdiction=registered_in,
        # A UK registration number is normalised so it can be joined to the
        # company product; foreign numbers are kept verbatim because their
        # formats are not comparable.
        reg_number=(
            normalize_company_number(reg_number)
            if registered_in == "GB" and reg_number
            else (str(reg_number).strip() if reg_number else "")
        ),
        legal_form=(ident.get("legal_form") or form or "").strip(),
    )


def _psc_relationship(
    psc_record_id: str, company_number: str, data: dict[str, Any]
) -> Relationship:
    control = parse_natures(data.get("natures_of_control"))
    # An edge is OWNS when the filing quantifies a stake and CONTROLS when it
    # asserts control without one — the appoint-directors and
    # significant-influence cases, which are control in full but carry no
    # percentage to propagate.
    rel_type = "OWNS" if control.min_percent is not None else "CONTROLS"
    return Relationship(
        rel_id=f"rel:{psc_record_id}->{company_number}",
        rel_type=rel_type,
        source_record_id=psc_record_id,
        target_record_id=f"ch:company:{company_number}",
        source=SOURCE_PSC,
        min_percent=control.min_percent,
        max_percent=control.max_percent,
        control_kinds=list(control.kinds),
        capacities=list(control.capacities),
        has_hard_control=control.has_hard_control,
        via_fiduciary=control.via_fiduciary,
        notified_on=_parse_ch_date(data.get("notified_on")),
        ceased_on=_parse_ch_date(data.get("ceased_on")),
    )


def parse_psc_jsonl(
    path: Path, retrieved_at: str | None = None
) -> Iterator[tuple[Record | None, Relationship | None, str]]:
    """Yield ``(record, relationship, kind)`` per PSC line.

    ``record`` and ``relationship`` are ``None`` for statement and exemption
    rows; ``kind`` is always returned so the caller can tally them.
    """
    stamp = retrieved_at or date.today().isoformat()

    def _lines(stream: io.TextIOBase) -> Iterator[tuple[Record | None, Relationship | None, str]]:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                yield None, None, "malformed"
                continue
            company_number = normalize_company_number(obj.get("company_number"))
            data = obj.get("data") or {}
            kind = data.get("kind") or "unknown"
            if not company_number:
                yield None, None, "no_company_number"
                continue
            record = _psc_record(company_number, data, stamp)
            if record is None:
                yield None, None, kind
                continue
            yield record, _psc_relationship(record.record_id, company_number, data), kind

    if path.suffix.lower() == ".zip":
        for _name, stream in iter_zip_members(path, ".txt"):
            yield from _lines(stream)
        for _name, stream in iter_zip_members(path, ".json"):
            yield from _lines(stream)
    else:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            yield from _lines(fh)
