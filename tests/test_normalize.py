"""Normalisation unit tests.

Each case is a variation actually present in the source registers, not a
synthetic edge case. Where a test looks unusually specific — the prefix-form
legal-name cases, the postcode grammar — it is guarding a bug that silently
costs recall rather than raising an error.
"""

from __future__ import annotations

import pytest

from ownership_er.normalize.addresses import (
    address_key,
    normalize_postcode,
    postcode_district,
)
from ownership_er.normalize.control import parse_nature, parse_natures
from ownership_er.normalize.countries import code_country, is_secrecy_jurisdiction
from ownership_er.normalize.names import (
    company_fingerprint,
    detect_legal_form,
    person_fingerprint,
    person_name_parts,
    phonetic_key,
    strip_person_titles,
    transliterate,
)
from ownership_er.sources.companies_house import normalize_company_number


class TestCompanyNames:
    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("Northgate Holdings Limited", "NORTHGATE HOLDINGS LTD"),
            ("Northgate Holdings Ltd.", "Northgate  Holdings  Limited"),
            # Prefix-form legal names: the Russian, Czech and Polish convention.
            # A suffix-only strip leaves these in different blocks entirely.
            ("OOO Severstal", "Severstal OOO"),
            ("ZAO Alfa Group", "Alfa Group ZAO"),
            ("Meridian Capital S.A.R.L.", "Meridian Capital SARL"),
        ],
    )
    def test_equivalent_names_share_a_fingerprint(self, a: str, b: str) -> None:
        assert company_fingerprint(a) == company_fingerprint(b)

    @pytest.mark.parametrize(
        ("a", "b"),
        [
            # Numerals distinguish real companies and must survive normalisation.
            ("Property 12 Limited", "Property 21 Limited"),
            ("Sterling Capital Limited", "Sterling Capital Partners Limited"),
        ],
    )
    def test_distinct_companies_keep_distinct_fingerprints(self, a: str, b: str) -> None:
        assert company_fingerprint(a) != company_fingerprint(b)

    def test_legal_form_is_detected_in_either_position(self) -> None:
        assert detect_legal_form("Acme Limited") == ("acme", "LTD")
        assert detect_legal_form("OOO Acme") == ("acme", "OOO")

    def test_empty_input_is_safe(self) -> None:
        assert company_fingerprint(None) == ""
        assert company_fingerprint("") == ""


class TestPersonNames:
    def test_titles_and_postnominals_are_stripped(self) -> None:
        assert strip_person_titles("Dr James Whitfield PhD") == "james whitfield"
        assert strip_person_titles("Mr John Smith Jr") == "john smith"

    def test_structured_fields_beat_display_name_parsing(self) -> None:
        # The registry knows which token is the surname; heuristics do not, and
        # get it wrong for names that are not ordered the Anglophone way.
        parts = person_name_parts("Chen Wei Ming", forename="Wei Ming", surname="Chen")
        assert parts == ("wei ming", "", "chen")

    def test_fingerprint_is_order_insensitive(self) -> None:
        assert person_fingerprint("James Whitfield") == person_fingerprint("Whitfield James")

    def test_fingerprint_excludes_middle_names(self) -> None:
        # Middle-name presence varies between filings for the same person, so it
        # must not partition the blocking space.
        assert person_fingerprint("James Andrew Whitfield") == person_fingerprint("James Whitfield")

    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("Kowalczyk", "Kowalchyk"),
            ("Shevchenko", "Schevchenko"),
            ("Petrov", "Petroff"),
        ],
    )
    def test_transcription_variants_share_a_phonetic_key(self, a: str, b: str) -> None:
        assert phonetic_key(a) == phonetic_key(b)

    def test_cyrillic_is_romanised(self) -> None:
        assert transliterate("Северсталь").startswith("severstal")

    def test_diacritics_are_folded(self) -> None:
        assert person_fingerprint("Łukasz Zieliński") == person_fingerprint("Lukasz Zielinski")


class TestCompanyNumbers:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("1234567", "01234567"),
            ("01234567", "01234567"),
            (" 1234567 ", "01234567"),
            ("SC 123456", "SC123456"),
            ("sc123456", "SC123456"),
        ],
    )
    def test_numbers_are_zero_padded(self, raw: str, expected: str) -> None:
        assert normalize_company_number(raw) == expected

    def test_empty_is_safe(self) -> None:
        assert normalize_company_number(None) == ""


class TestPostcodes:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("EC1V9NR", "EC1V 9NR"),
            ("ec1v 9nr", "EC1V 9NR"),
            (" M1  1AE ", "M1 1AE"),
            ("SW1A 1AA", "SW1A 1AA"),
        ],
    )
    def test_valid_postcodes_are_canonicalised(self, raw: str, expected: str) -> None:
        assert normalize_postcode(raw) == expected

    @pytest.mark.parametrize("raw", ["", "NOTAPOSTCODE", "12345", "01234567"])
    def test_invalid_input_yields_empty(self, raw: str) -> None:
        # Strictness matters: a loose pattern matches company numbers and house
        # numbers, which then pollute the postcode blocking key.
        assert normalize_postcode(raw) == ""

    def test_district_extraction(self) -> None:
        assert postcode_district("EC1V 9NR") == "EC1V"

    def test_address_key_combines_number_and_postcode(self) -> None:
        a = address_key("12 High Street, London EC1V 9NR")
        b = address_key("12, High St, London, EC1V 9NR")
        assert a == b == "12|EC1V 9NR"


class TestCountries:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("British", "GB"),
            ("BRITISH", "GB"),
            ("United Kingdom", "GB"),
            ("English", "GB"),
            ("Scottish", "GB"),
            ("Russian", "RU"),
            ("Russian Federation", "RU"),
            ("Cypriot", "CY"),
            ("BVI", "VG"),
            ("GB", "GB"),
        ],
    )
    def test_demonyms_and_names_code_to_iso2(self, raw: str, expected: str) -> None:
        assert code_country(raw) == expected

    def test_dual_nationality_takes_the_first(self) -> None:
        assert code_country("British, Irish") == "GB"

    def test_unmapped_returns_empty_rather_than_guessing(self) -> None:
        assert code_country("Wakandan") == ""

    def test_secrecy_jurisdictions(self) -> None:
        assert is_secrecy_jurisdiction("VG")
        assert not is_secrecy_jurisdiction("GB")


class TestControlParsing:
    def test_equity_band_is_an_interval(self) -> None:
        s = parse_nature("ownership-of-shares-25-to-50-percent")
        assert (s.kind, s.min_percent, s.max_percent) == ("SHARES", 25.0, 50.0)

    def test_trust_capacity_is_flagged(self) -> None:
        s = parse_nature("ownership-of-shares-75-to-100-percent-as-trust")
        assert s.capacity == "TRUST"
        assert s.is_indirect_capacity
        assert s.max_percent == 100.0

    def test_control_without_equity_is_hard_control(self) -> None:
        # A person with no shares but the right to appoint directors controls
        # the company outright; ranking by percentage alone would miss them.
        s = parse_nature("right-to-appoint-and-remove-directors")
        assert s.kind == "APPOINT_DIRECTORS"
        assert s.min_percent is None
        assert s.is_hard_control

    def test_unknown_vocabulary_does_not_raise(self) -> None:
        assert parse_nature("some-future-nature-of-control").kind == "OTHER"

    def test_aggregation_takes_the_widest_band(self) -> None:
        result = parse_natures(
            [
                "ownership-of-shares-25-to-50-percent",
                "voting-rights-75-to-100-percent",
                "right-to-appoint-and-remove-directors",
            ]
        )
        # Equity statements take precedence over voting ones when both are
        # present: they describe the same holding, and the shareholding is the
        # claim the register actually quantifies.
        assert result.min_percent == 25.0
        assert result.max_percent == 50.0
        assert result.has_hard_control is True
        assert set(result.kinds) == {"SHARES", "VOTING", "APPOINT_DIRECTORS"}

    def test_empty_input(self) -> None:
        result = parse_natures(None)
        assert result.min_percent is None
        assert result.kinds == []
        assert result.has_hard_control is False
