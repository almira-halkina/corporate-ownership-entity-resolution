"""Source-specific parsing into the common record schema."""

from ownership_er.normalize.addresses import (
    address_key,
    normalize_address,
    normalize_postcode,
    postcode_district,
)
from ownership_er.normalize.control import parse_natures
from ownership_er.normalize.countries import code_country, code_nationality
from ownership_er.normalize.names import (
    company_fingerprint,
    normalize_text,
    person_fingerprint,
    person_name_parts,
    phonetic_key,
)

__all__ = [
    "address_key",
    "code_country",
    "code_nationality",
    "company_fingerprint",
    "normalize_address",
    "normalize_postcode",
    "normalize_text",
    "parse_natures",
    "person_fingerprint",
    "person_name_parts",
    "phonetic_key",
    "postcode_district",
]
