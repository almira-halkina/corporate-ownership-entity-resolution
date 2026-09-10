"""Synthetic corpus generation, in the exact wire format of each real source.

Two jobs, and it is worth being clear that they are different.

**Offline runnability.** Companies House and OpenSanctions snapshots are
gigabytes and are not redistributable in a git repository. Without a committed
fixture corpus, nobody can clone this repo and see it work — they would have to
download 4GB first. The generator writes files byte-compatible with the real
products (same CSV headers, same JSON nesting, same controlled vocabularies) so
every stage runs unmodified against them.

**A benchmark with known answers.** The generator plants duplicates by
*corrupting known entities*, so the true clustering is known by construction.
That gives a labelled evaluation set on demand, at any difficulty, which is the
only way to measure recall — the quantity you cannot estimate from a hand-
labelled sample of accepted pairs, because you never see what blocking dropped.

The corruption model is empirical, not arbitrary. Each transformation below
reproduces a variation actually observed in PSC filings for the same person:
registrars and filers differ on titles, middle names, transliteration schemes,
name order for non-Anglophone names, and diacritics. Percentages are set to
make the fixture hard enough to be informative rather than to flatter the
matcher; the ``difficulty`` argument scales them.
"""

from __future__ import annotations

import csv
import json
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["CORRUPTIONS", "FixtureCorpus", "generate_corpus"]

# ---------------------------------------------------------------------------
# Name pools — chosen to exercise the normalisation paths that matter:
# Cyrillic transliteration variants, diacritics, particles, and non-Anglophone
# name ordering. Entirely fictitious.
# ---------------------------------------------------------------------------

_FORENAMES = [
    "James",
    "Sarah",
    "Mohammed",
    "Yevgeniy",
    "Aleksandr",
    "Katarzyna",
    "Siobhan",
    "Dmitri",
    "Chen",
    "Priya",
    "Olusegun",
    "Hanna",
    "Lukasz",
    "Nadia",
    "Tomasz",
    "Elena",
    "Ibrahim",
    "Astrid",
    "Rustam",
    "Fiona",
    "Andrei",
    "Mei",
    "Rafael",
    "Ingrid",
    "Yusuf",
    "Oksana",
    "Piotr",
    "Amara",
    "Viktor",
    "Lucia",
]

_SURNAMES = [
    "Whitfield",
    "Kowalczyk",
    "Shevchenko",
    "Al-Rashid",
    "O'Donnell",
    "Nakamura",
    "Petrov",
    "Van der Berg",
    "Fitzgerald",
    "Novak",
    "Ivanova",
    "Mensah",
    "Lindqvist",
    "Abramovich",
    "Zieliński",
    "Haddad",
    "MacGregor",
    "Okonkwo",
    "Rasmussen",
    "Beaumont",
    "Sokolov",
    "Ferreira",
    "Dubois",
    "Karimov",
    "Andersson",
    "Marchetti",
    "Nowak",
    "Hussain",
    "Larsen",
    "Voronin",
]

_COMPANY_HEADS = [
    "Northgate",
    "Sterling",
    "Meridian",
    "Ashcroft",
    "Blackwater",
    "Kingsway",
    "Harbourview",
    "Redwood",
    "Silverline",
    "Ironbridge",
    "Fairhaven",
    "Crestwood",
    "Longacre",
    "Beaconsfield",
    "Thornbury",
    "Whitehall",
    "Ravensworth",
    "Gatehouse",
    "Camberwell",
    "Pinnacle",
    "Aldgate",
    "Eastbrook",
    "Marlborough",
    "Ludgate",
]

_COMPANY_TAILS = [
    "Holdings",
    "Capital",
    "Investments",
    "Properties",
    "Ventures",
    "Partners",
    "Group",
    "Trading",
    "Assets",
    "Management",
    "Enterprises",
    "Resources",
]

_LEGAL_FORMS = ["LIMITED", "LTD", "PLC", "LLP"]

_FOREIGN_FORMS = [
    ("CY", "LIMITED"),
    ("VG", "LTD"),
    ("KY", "LTD"),
    ("LU", "SARL"),
    ("NL", "B.V."),
    ("RU", "OOO"),
    ("AE", "FZE"),
    ("PA", "S.A."),
    ("JE", "LIMITED"),
    ("SC", "LIMITED"),
    ("MT", "LIMITED"),
    ("CH", "AG"),
]

_STREETS = [
    "High Street",
    "Church Lane",
    "Station Road",
    "Victoria Road",
    "Mill Lane",
    "Queens Road",
    "Kings Avenue",
    "Park View",
    "Bridge Street",
    "Manor Way",
]

_TOWNS = [
    "London",
    "Manchester",
    "Birmingham",
    "Leeds",
    "Glasgow",
    "Bristol",
    "Edinburgh",
    "Cardiff",
    "Liverpool",
    "Sheffield",
]

_POSTCODE_DISTRICTS = [
    "EC1V",
    "W1D",
    "SW1A",
    "M1",
    "B3",
    "LS1",
    "G2",
    "BS1",
    "EH1",
    "CF10",
    "L1",
    "S1",
    "N1",
    "SE1",
    "WC2H",
]

_NATURES = [
    ["ownership-of-shares-75-to-100-percent"],
    ["ownership-of-shares-50-to-75-percent"],
    ["ownership-of-shares-25-to-50-percent"],
    ["ownership-of-shares-25-to-50-percent", "voting-rights-25-to-50-percent"],
    ["voting-rights-75-to-100-percent"],
    ["right-to-appoint-and-remove-directors"],
    ["significant-influence-or-control"],
    ["ownership-of-shares-75-to-100-percent-as-trust"],
    ["ownership-of-shares-50-to-75-percent-as-firm"],
]

_TITLES = ["Mr", "Mrs", "Ms", "Dr", "Miss", "Professor", "Sir"]

_NATIONALITY_VARIANTS = {
    "GB": ["British", "BRITISH", "United Kingdom", "Uk", "English", "Scottish"],
    "RU": ["Russian", "RUSSIAN", "Russian Federation"],
    "UA": ["Ukrainian", "Ukraine"],
    "PL": ["Polish", "Poland"],
    "IE": ["Irish", "Ireland"],
    "CY": ["Cypriot", "Cyprus"],
    "AE": ["Emirati", "United Arab Emirates"],
    "CN": ["Chinese", "China"],
    "IN": ["Indian", "India"],
    "NG": ["Nigerian", "Nigeria"],
}

# Transliteration doublets: the same underlying name written under two
# different romanisation conventions, which is the dominant cause of
# non-matching duplicates in any register that accepts free-text names.
_TRANSLIT_VARIANTS = {
    "Yevgeniy": ["Evgeny", "Evgenii", "Yevgeny", "Eugene"],
    "Aleksandr": ["Alexander", "Alexandr", "Aleksander"],
    "Dmitri": ["Dmitry", "Dmitrii", "Dimitri"],
    "Andrei": ["Andrey", "Andrii", "Andre"],
    "Shevchenko": ["Schevchenko", "Shevtchenko"],
    "Petrov": ["Petroff", "Petrow"],
    "Sokolov": ["Sokoloff", "Sokolow"],
    "Voronin": ["Woronin", "Voroneen"],
    "Kowalczyk": ["Kowalchyk", "Kovalchik"],
    "Zieliński": ["Zielinski", "Zielinsky"],
    "Karimov": ["Kerimov", "Karimoff"],
    "Abramovich": ["Abramovic", "Abramowitsch"],
    "Ivanova": ["Ivanoff", "Iwanowa"],
    "Oksana": ["Oxana", "Aksana"],
    "Katarzyna": ["Katarzina", "Catherine"],
}

CORRUPTIONS = (
    "title_change",
    "middle_name_dropped",
    "transliteration",
    "typo",
    "name_order_swap",
    "diacritic_stripped",
    "initial_only",
    "double_space",
    "case_change",
    "address_reformat",
    "nationality_variant",
)


@dataclass
class FixtureCorpus:
    """Paths written, plus the ground truth needed to score a run."""

    companies_csv: Path
    psc_jsonl: Path
    opensanctions_jsonl: Path
    truth_json: Path
    stats: dict[str, Any] = field(default_factory=dict)


def _postcode(rng: random.Random) -> str:
    district = rng.choice(_POSTCODE_DISTRICTS)
    return f"{district} {rng.randint(1, 9)}{rng.choice('ABDEFGHJLNPQRSTUWXYZ')}{rng.choice('ABDEFGHJLNPQRSTUWXYZ')}"


def _company_number(rng: random.Random, seq: int) -> str:
    return f"{seq:08d}" if rng.random() > 0.08 else f"SC{seq:06d}"


def _corrupt_name(
    rng: random.Random,
    first: str,
    middle: str,
    last: str,
    difficulty: float,
) -> tuple[str, str, str, list[str]]:
    """Apply a random subset of realistic corruptions. Returns parts + labels."""
    applied: list[str] = []
    f, m, ls = first, middle, last

    if middle and rng.random() < 0.45 * difficulty:
        m = ""
        applied.append("middle_name_dropped")

    if middle and m and rng.random() < 0.25 * difficulty:
        m = m[0]
        applied.append("initial_only")

    for attr, value in (("first", f), ("last", ls)):
        if value in _TRANSLIT_VARIANTS and rng.random() < 0.40 * difficulty:
            variant = rng.choice(_TRANSLIT_VARIANTS[value])
            if attr == "first":
                f = variant
            else:
                ls = variant
            applied.append("transliteration")

    if rng.random() < 0.18 * difficulty:
        target = ls if len(ls) > 4 else f
        if len(target) > 4:
            pos = rng.randrange(1, len(target) - 1)
            swapped = list(target)
            swapped[pos], swapped[pos + 1] = swapped[pos + 1], swapped[pos]
            corrupted = "".join(swapped)
            if target is ls:
                ls = corrupted
            else:
                f = corrupted
            applied.append("typo")

    if rng.random() < 0.10 * difficulty:
        f, ls = ls, f
        applied.append("name_order_swap")

    if any(ch in ls for ch in "ńśćłżźąę") and rng.random() < 0.55 * difficulty:
        table = str.maketrans("ńśćłżźąę", "nsclzzae")
        ls = ls.translate(table)
        applied.append("diacritic_stripped")

    if rng.random() < 0.12 * difficulty:
        ls = ls.upper()
        applied.append("case_change")

    return f, m, ls, applied


def _display_name(
    rng: random.Random, first: str, middle: str, last: str, difficulty: float
) -> tuple[str, str | None, list[str]]:
    applied: list[str] = []
    title: str | None = None
    if rng.random() < 0.75:
        title = rng.choice(_TITLES)
        applied.append("title_change")
    parts = [p for p in (title, first, middle, last) if p]
    name = " ".join(parts)
    if rng.random() < 0.08 * difficulty:
        name = name.replace(" ", "  ", 1)
        applied.append("double_space")
    return name, title, applied


def generate_corpus(
    out_dir: Path,
    *,
    n_companies: int = 1_200,
    n_people: int = 400,
    n_sanctioned: int = 60,
    difficulty: float = 1.0,
    seed: int = 20260805,
) -> FixtureCorpus:
    """Write a synthetic corpus and its ground truth to ``out_dir``.

    ``difficulty`` scales every corruption probability; ``1.0`` produces a
    corpus roughly as hard as the real PSC register, and higher values are
    useful for stress-testing where the matcher degrades.
    """
    rng = random.Random(seed)
    out_dir.mkdir(parents=True, exist_ok=True)

    companies_csv = out_dir / "companies_sample.csv"
    psc_jsonl = out_dir / "psc_sample.jsonl"
    os_jsonl = out_dir / "opensanctions_sample.jsonl"
    truth_json = out_dir / "ground_truth.json"

    # ---------------- people (the entities to be re-discovered) -------------
    people: list[dict[str, Any]] = []
    for i in range(n_people):
        first = rng.choice(_FORENAMES)
        middle = rng.choice(_FORENAMES) if rng.random() < 0.45 else ""
        last = rng.choice(_SURNAMES)
        nationality = rng.choice(list(_NATIONALITY_VARIANTS))
        people.append(
            {
                "person_id": f"TRUE-P-{i:05d}",
                "first": first,
                "middle": middle,
                "last": last,
                "birth_year": rng.randint(1945, 2001),
                "birth_month": rng.randint(1, 12),
                "nationality": nationality,
                "country": nationality if rng.random() < 0.6 else "GB",
                "address": {
                    "premises": str(rng.randint(1, 250)),
                    "address_line_1": rng.choice(_STREETS),
                    "locality": rng.choice(_TOWNS),
                    "postal_code": _postcode(rng),
                    "country": "United Kingdom",
                },
            }
        )

    # ---------------- companies --------------------------------------------
    companies: list[dict[str, Any]] = []
    for i in range(n_companies):
        head = rng.choice(_COMPANY_HEADS)
        tail = rng.choice(_COMPANY_TAILS)
        suffix = f" {rng.randint(2, 40)}" if rng.random() < 0.18 else ""
        form = rng.choice(_LEGAL_FORMS)
        companies.append(
            {
                "number": _company_number(rng, 1_000_000 + i),
                "name": f"{head} {tail}{suffix} {form}",
                "premises": str(rng.randint(1, 250)),
                "street": rng.choice(_STREETS),
                "town": rng.choice(_TOWNS),
                "postcode": _postcode(rng),
                "incorporated": f"{rng.randint(1, 28):02d}/{rng.randint(1, 12):02d}/{rng.randint(1990, 2024)}",
                "status": rng.choices(["Active", "Dissolved", "Liquidation"], weights=[85, 12, 3])[
                    0
                ],
                "sic": rng.choice(
                    [
                        "64209 - Activities of other holding companies n.e.c.",
                        "68209 - Other letting and operating of own or leased real estate",
                        "70100 - Activities of head offices",
                        "82990 - Other business support service activities n.e.c.",
                        "47190 - Other retail sale in non-specialised stores",
                    ]
                ),
            }
        )

    # ---------------- PSC filings ------------------------------------------
    psc_rows: list[dict[str, Any]] = []
    truth_person_records: dict[str, list[str]] = {}
    corruption_log: list[dict[str, Any]] = []

    company_pool = list(companies)
    rng.shuffle(company_pool)
    cursor = 0

    for person in people:
        n_filings = rng.choices([1, 2, 3, 4, 5, 8], weights=[38, 26, 16, 10, 6, 4])[0]
        for _ in range(n_filings):
            if cursor >= len(company_pool):
                cursor = 0
                rng.shuffle(company_pool)
            company = company_pool[cursor]
            cursor += 1

            f, m, ls, applied = _corrupt_name(
                rng, person["first"], person["middle"], person["last"], difficulty
            )
            name, title, name_applied = _display_name(rng, f, m, ls, difficulty)
            applied.extend(name_applied)

            nat_pool = _NATIONALITY_VARIANTS[person["nationality"]]
            nationality = rng.choice(nat_pool)
            if nationality != nat_pool[0]:
                applied.append("nationality_variant")

            address = dict(person["address"])
            if rng.random() < 0.30 * difficulty:
                address["address_line_1"] = (
                    address["address_line_1"]
                    .replace("Street", "St")
                    .replace("Road", "Rd")
                    .replace("Lane", "Ln")
                )
                applied.append("address_reformat")

            # Date of birth is occasionally absent, and month occasionally
            # differs by one — both observed in the real register.
            dob: dict[str, int] = {}
            if rng.random() > 0.05:
                dob = {"month": person["birth_month"], "year": person["birth_year"]}
                if rng.random() < 0.03 * difficulty:
                    dob["month"] = max(1, min(12, dob["month"] + rng.choice([-1, 1])))

            filing_hash = f"{person['person_id']}-{company['number']}-{rng.randrange(16**8):08x}"
            record_id = f"ch:psc:{company['number']}:{filing_hash}"

            name_elements: dict[str, str] = {"forename": f, "surname": ls}
            if m:
                name_elements["middle_name"] = m
            if title:
                name_elements["title"] = title
            # Some filings omit name_elements entirely, forcing display-name parsing.
            include_elements = rng.random() > 0.15 * difficulty

            row: dict[str, Any] = {
                "company_number": company["number"],
                "data": {
                    "kind": "individual-person-with-significant-control",
                    "name": name,
                    "address": address,
                    "date_of_birth": dob,
                    "nationality": nationality,
                    "country_of_residence": (
                        "England" if person["country"] == "GB" else person["country"]
                    ),
                    "natures_of_control": rng.choice(_NATURES),
                    "notified_on": f"{rng.randint(2016, 2025)}-"
                    f"{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}",
                    "links": {
                        "self": f"/company/{company['number']}"
                        f"/persons-with-significant-control/individual/{filing_hash}"
                    },
                },
            }
            if include_elements:
                row["data"]["name_elements"] = name_elements
            if rng.random() < 0.06:
                row["data"]["ceased_on"] = f"{rng.randint(2019, 2025)}-06-30"

            psc_rows.append(row)
            truth_person_records.setdefault(person["person_id"], []).append(record_id)
            corruption_log.append(
                {
                    "person_id": person["person_id"],
                    "record_id": record_id,
                    "corruptions": sorted(set(applied)),
                }
            )

    # ---------------- corporate PSC filings --------------------------------
    # These create the multi-layer ownership chains the traversal is for, and
    # supply free ground truth: where a corporate PSC states a UK registration
    # number, the true link to the company product is known.
    truth_corporate_links: dict[str, str] = {}
    n_corporate = max(1, int(len(companies) * 0.22))
    for i in range(n_corporate):
        child = rng.choice(companies)
        if rng.random() < 0.55:
            parent = rng.choice(companies)
            if parent["number"] == child["number"]:
                continue
            parent_name = parent["name"]
            # The parent's name is re-typed by the filer, so it rarely matches
            # the register string exactly.
            if rng.random() < 0.45:
                parent_name = parent_name.replace("LIMITED", "Ltd").replace("  ", " ")
            if rng.random() < 0.20:
                parent_name = parent_name.upper()
            reg_number = parent["number"] if rng.random() < 0.70 else None
            country_registered = "England"
            legal_form = "Private Limited Company"
            filing_hash = f"CORP-{i:05d}-{rng.randrange(16**8):08x}"
            record_id = f"ch:psc:{child['number']}:{filing_hash}"
            if reg_number:
                truth_corporate_links[record_id] = f"ch:company:{parent['number']}"
        else:
            iso, form = rng.choice(_FOREIGN_FORMS)
            parent_name = f"{rng.choice(_COMPANY_HEADS)} {rng.choice(_COMPANY_TAILS)} {form}"
            reg_number = (
                f"HE{rng.randint(100000, 499999)}"
                if iso == "CY"
                else str(rng.randint(100000, 999999))
            )
            country_registered = iso
            legal_form = "Foreign Company"
            filing_hash = f"CORP-{i:05d}-{rng.randrange(16**8):08x}"
            record_id = f"ch:psc:{child['number']}:{filing_hash}"

        identification: dict[str, Any] = {
            "country_registered": country_registered,
            "legal_authority": "Companies Act 2006",
            "legal_form": legal_form,
            "place_registered": country_registered,
        }
        if reg_number:
            identification["registration_number"] = reg_number

        psc_rows.append(
            {
                "company_number": child["number"],
                "data": {
                    "kind": "corporate-entity-person-with-significant-control",
                    "name": parent_name,
                    "address": {
                        "address_line_1": rng.choice(_STREETS),
                        "locality": rng.choice(_TOWNS),
                        "postal_code": _postcode(rng),
                        "country": country_registered,
                    },
                    "identification": identification,
                    "natures_of_control": rng.choice(_NATURES),
                    "notified_on": f"{rng.randint(2016, 2025)}-"
                    f"{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}",
                    "links": {
                        "self": f"/company/{child['number']}"
                        f"/persons-with-significant-control/corporate-entity/{filing_hash}"
                    },
                },
            }
        )

    # ---------------- statements and exemptions ----------------------------
    # ~5% of the real register. Included because a parser that treats these as
    # entities produces millions of phantom people, and that failure should be
    # caught by the fixture rather than in production.
    for _ in range(max(1, int(len(psc_rows) * 0.05))):
        company = rng.choice(companies)
        psc_rows.append(
            {
                "company_number": company["number"],
                "data": {
                    "kind": "persons-with-significant-control-statement",
                    "statement": rng.choice(
                        [
                            "no-individual-or-entity-with-signficant-control",
                            "steps-to-find-psc-not-yet-completed",
                            "psc-exists-but-not-identified",
                        ]
                    ),
                    "notified_on": "2017-07-01",
                    "links": {
                        "self": f"/company/{company['number']}"
                        f"/persons-with-significant-control-statements/x"
                    },
                },
            }
        )
    for _ in range(max(1, int(len(psc_rows) * 0.004))):
        company = rng.choice(companies)
        psc_rows.append(
            {
                "company_number": company["number"],
                "data": {
                    "kind": "super-secure-person-with-significant-control",
                    "description": "super-secure-persons-with-significant-control",
                    "links": {
                        "self": f"/company/{company['number']}"
                        f"/persons-with-significant-control/super-secure/x"
                    },
                },
            }
        )

    rng.shuffle(psc_rows)

    # ---------------- OpenSanctions entities -------------------------------
    # A subset are deliberately the *same people* as PSC filers, with the name
    # written under a different transliteration. Those are the cross-source
    # links the exposure analysis depends on, and their truth is recorded.
    os_entities: list[dict[str, Any]] = []
    truth_sanctions_links: dict[str, str] = {}
    sanctioned_people = rng.sample(people, min(n_sanctioned, len(people)))

    for i, person in enumerate(sanctioned_people):
        os_id = f"NK-{i:05d}"
        names = [f"{person['first']} {person['last']}"]
        for src, variants in _TRANSLIT_VARIANTS.items():
            if src == person["first"]:
                names.extend(f"{v} {person['last']}" for v in variants[:2])
            if src == person["last"]:
                names.extend(f"{person['first']} {v}" for v in variants[:2])
        if person["middle"]:
            names.append(f"{person['first']} {person['middle']} {person['last']}")

        topics = rng.choice(
            [
                ["sanction"],
                ["sanction", "role.pep"],
                ["role.pep"],
                ["sanction", "role.oligarch"],
                ["crime.fin"],
                ["export.control"],
            ]
        )
        os_entities.append(
            {
                "id": os_id,
                "caption": names[0],
                "schema": "Person",
                "properties": {
                    "name": [names[0]],
                    "alias": names[1:],
                    "birthDate": [
                        f"{person['birth_year']}-{person['birth_month']:02d}"
                        if rng.random() < 0.6
                        else str(person["birth_year"])
                    ],
                    "nationality": [_NATIONALITY_VARIANTS[person["nationality"]][0]],
                    "country": [_NATIONALITY_VARIANTS[person["nationality"]][0]],
                    "topics": topics,
                    "sourceUrl": [f"https://www.opensanctions.org/entities/{os_id}/"],
                },
                "datasets": [
                    rng.choice(
                        [
                            "us_ofac_sdn",
                            "eu_fsf",
                            "gb_hmt_sanctions",
                            "ua_nsdc_sanctions",
                            "everypolitician",
                        ]
                    )
                ],
                "referents": [],
            }
        )
        truth_sanctions_links[f"os:{os_id}"] = person["person_id"]

    # Sanctioned corporate entities, some matching UK companies by name.
    for i in range(max(1, n_sanctioned // 3)):
        company = rng.choice(companies)
        os_id = f"NK-C-{i:05d}"
        os_entities.append(
            {
                "id": os_id,
                "caption": company["name"],
                "schema": "Company",
                "properties": {
                    "name": [company["name"]],
                    "alias": [company["name"].replace("LIMITED", "Ltd")],
                    "jurisdiction": ["gb"],
                    "country": ["United Kingdom"],
                    "registrationNumber": ([company["number"]] if rng.random() < 0.5 else []),
                    "topics": [rng.choice(["sanction", "sanction.linked", "corp.shell"])],
                    "sourceUrl": [f"https://www.opensanctions.org/entities/{os_id}/"],
                },
                "datasets": ["us_ofac_sdn"],
                "referents": [],
            }
        )
        truth_sanctions_links[f"os:{os_id}"] = f"ch:company:{company['number']}"

    # Unrelated sanctions entities — noise that must *not* match anything.
    for i in range(max(1, n_sanctioned)):
        os_id = f"NK-N-{i:05d}"
        os_entities.append(
            {
                "id": os_id,
                "caption": f"{rng.choice(_FORENAMES)} {rng.choice(_SURNAMES)}",
                "schema": "Person",
                "properties": {
                    "name": [f"{rng.choice(_FORENAMES)} {rng.choice(_SURNAMES)}"],
                    "birthDate": [str(rng.randint(1930, 1999))],
                    "nationality": ["Iranian"],
                    "topics": ["sanction"],
                },
                "datasets": ["us_ofac_sdn"],
                "referents": [],
            }
        )

    # ---------------- write ------------------------------------------------
    _write_companies_csv(companies_csv, companies)

    with psc_jsonl.open("w", encoding="utf-8") as fh:
        for row in psc_rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    with os_jsonl.open("w", encoding="utf-8") as fh:
        for ent in os_entities:
            fh.write(json.dumps(ent, ensure_ascii=False) + "\n")

    truth = {
        "person_clusters": truth_person_records,
        "corporate_links": truth_corporate_links,
        "sanctions_links": truth_sanctions_links,
        "corruption_log": corruption_log,
        "params": {
            "n_companies": n_companies,
            "n_people": n_people,
            "n_sanctioned": n_sanctioned,
            "difficulty": difficulty,
            "seed": seed,
        },
    }
    truth_json.write_text(json.dumps(truth, indent=2), encoding="utf-8")

    stats = {
        "companies": len(companies),
        "psc_rows": len(psc_rows),
        "opensanctions_entities": len(os_entities),
        "true_people": len(truth_person_records),
        "person_records": sum(len(v) for v in truth_person_records.values()),
        "true_pairs": sum(len(v) * (len(v) - 1) // 2 for v in truth_person_records.values()),
        "corporate_links": len(truth_corporate_links),
        "sanctions_links": len(truth_sanctions_links),
    }
    return FixtureCorpus(companies_csv, psc_jsonl, os_jsonl, truth_json, stats)


_CSV_HEADERS = [
    "CompanyName",
    "CompanyNumber",
    "RegAddress.CareOf",
    "RegAddress.POBox",
    "RegAddress.AddressLine1",
    "RegAddress.AddressLine2",
    "RegAddress.PostTown",
    "RegAddress.County",
    "RegAddress.Country",
    "RegAddress.PostCode",
    "CompanyCategory",
    "CompanyStatus",
    "CountryOfOrigin",
    "DissolutionDate",
    "IncorporationDate",
    "SICCode.SicText_1",
    "SICCode.SicText_2",
    "SICCode.SicText_3",
    "SICCode.SicText_4",
    "URI",
]


def _write_companies_csv(path: Path, companies: list[dict[str, Any]]) -> None:
    """Write the company product with its real header names and date format."""
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=_CSV_HEADERS)
        writer.writeheader()
        for c in companies:
            writer.writerow(
                {
                    "CompanyName": c["name"],
                    "CompanyNumber": c["number"],
                    "RegAddress.CareOf": "",
                    "RegAddress.POBox": "",
                    "RegAddress.AddressLine1": f"{c['premises']} {c['street']}",
                    "RegAddress.AddressLine2": "",
                    "RegAddress.PostTown": c["town"],
                    "RegAddress.County": "",
                    "RegAddress.Country": "United Kingdom",
                    "RegAddress.PostCode": c["postcode"],
                    "CompanyCategory": "Private Limited Company",
                    "CompanyStatus": c["status"],
                    "CountryOfOrigin": "United Kingdom",
                    "DissolutionDate": "",
                    "IncorporationDate": c["incorporated"],
                    "SICCode.SicText_1": c["sic"],
                    "SICCode.SicText_2": "",
                    "SICCode.SicText_3": "",
                    "SICCode.SicText_4": "",
                    "URI": f"http://business.data.gov.uk/id/company/{c['number']}",
                }
            )
