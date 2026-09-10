"""Country, nationality and jurisdiction coding.

Companies House stores nationality as free text supplied by the filer, so the
same nationality arrives as ``British``, ``BRITISH``, ``Uk``, ``U.K.``,
``English`` and ``Great Britain``. Left uncoded, nationality is useless as a
matcher feature and actively harmful as a conflict rule — it would split
clusters that differ only in spelling. Coding it to ISO 3166-1 alpha-2 turns it
into one of the more reliable discriminators available for individuals.
"""

from __future__ import annotations

from functools import lru_cache

from ownership_er.normalize.names import normalize_text

__all__ = ["SECRECY_JURISDICTIONS", "code_country", "code_nationality", "is_secrecy_jurisdiction"]

# Demonyms and country-name variants -> ISO 3166-1 alpha-2. Scoped to what
# actually appears in UK PSC filings at meaningful volume rather than
# attempting exhaustive coverage; unmapped values are preserved as-is and
# reported by the normalisation stage so the table can be extended from data.
_COUNTRY_ALIASES: dict[str, str] = {
    # United Kingdom and constituent nations
    "british": "GB",
    "uk": "GB",
    "u k": "GB",
    "united kingdom": "GB",
    "great britain": "GB",
    "gb": "GB",
    "england": "GB",
    "english": "GB",
    "scotland": "GB",
    "scottish": "GB",
    "wales": "GB",
    "welsh": "GB",
    "northern ireland": "GB",
    "northern irish": "GB",
    "britain": "GB",
    "united kingdom of great britain and northern ireland": "GB",
    "uk citizen": "GB",
    "british citizen": "GB",
    # Ireland
    "irish": "IE",
    "ireland": "IE",
    "republic of ireland": "IE",
    "eire": "IE",
    # Europe
    "french": "FR",
    "france": "FR",
    "german": "DE",
    "germany": "DE",
    "deutschland": "DE",
    "italian": "IT",
    "italy": "IT",
    "italia": "IT",
    "spanish": "ES",
    "spain": "ES",
    "espana": "ES",
    "portuguese": "PT",
    "portugal": "PT",
    "dutch": "NL",
    "netherlands": "NL",
    "holland": "NL",
    "the netherlands": "NL",
    "belgian": "BE",
    "belgium": "BE",
    "swiss": "CH",
    "switzerland": "CH",
    "austrian": "AT",
    "austria": "AT",
    "swedish": "SE",
    "sweden": "SE",
    "norwegian": "NO",
    "norway": "NO",
    "danish": "DK",
    "denmark": "DK",
    "finnish": "FI",
    "finland": "FI",
    "icelandic": "IS",
    "iceland": "IS",
    "polish": "PL",
    "poland": "PL",
    "czech": "CZ",
    "czech republic": "CZ",
    "czechia": "CZ",
    "slovak": "SK",
    "slovakia": "SK",
    "hungarian": "HU",
    "hungary": "HU",
    "romanian": "RO",
    "romania": "RO",
    "bulgarian": "BG",
    "bulgaria": "BG",
    "greek": "GR",
    "greece": "GR",
    "cypriot": "CY",
    "cyprus": "CY",
    "maltese": "MT",
    "malta": "MT",
    "luxembourg": "LU",
    "luxembourgish": "LU",
    "luxembourger": "LU",
    "estonian": "EE",
    "estonia": "EE",
    "latvian": "LV",
    "latvia": "LV",
    "lithuanian": "LT",
    "lithuania": "LT",
    "slovenian": "SI",
    "slovenia": "SI",
    "croatian": "HR",
    "croatia": "HR",
    "serbian": "RS",
    "serbia": "RS",
    "bosnian": "BA",
    "bosnia and herzegovina": "BA",
    "albanian": "AL",
    "albania": "AL",
    "macedonian": "MK",
    "north macedonia": "MK",
    "montenegrin": "ME",
    "montenegro": "ME",
    "moldovan": "MD",
    "moldova": "MD",
    # Russia / CIS
    "russian": "RU",
    "russia": "RU",
    "russian federation": "RU",
    "ukrainian": "UA",
    "ukraine": "UA",
    "belarusian": "BY",
    "belarussian": "BY",
    "belarus": "BY",
    "byelorussian": "BY",
    "kazakh": "KZ",
    "kazakhstan": "KZ",
    "kazakhstani": "KZ",
    "uzbek": "UZ",
    "uzbekistan": "UZ",
    "azerbaijani": "AZ",
    "azerbaijan": "AZ",
    "armenian": "AM",
    "armenia": "AM",
    "georgian": "GE",
    "georgia": "GE",
    "kyrgyz": "KG",
    "kyrgyzstan": "KG",
    "tajik": "TJ",
    "tajikistan": "TJ",
    "turkmen": "TM",
    "turkmenistan": "TM",
    # Americas
    "american": "US",
    "usa": "US",
    "u s a": "US",
    "united states": "US",
    "united states of america": "US",
    "us": "US",
    "canadian": "CA",
    "canada": "CA",
    "mexican": "MX",
    "mexico": "MX",
    "brazilian": "BR",
    "brazil": "BR",
    "argentine": "AR",
    "argentinian": "AR",
    "argentina": "AR",
    "chilean": "CL",
    "chile": "CL",
    "colombian": "CO",
    "colombia": "CO",
    "venezuelan": "VE",
    "venezuela": "VE",
    "panamanian": "PA",
    "panama": "PA",
    # Middle East / Africa
    "israeli": "IL",
    "israel": "IL",
    "turkish": "TR",
    "turkey": "TR",
    "turkiye": "TR",
    "emirati": "AE",
    "uae": "AE",
    "united arab emirates": "AE",
    "saudi": "SA",
    "saudi arabian": "SA",
    "saudi arabia": "SA",
    "qatari": "QA",
    "qatar": "QA",
    "kuwaiti": "KW",
    "kuwait": "KW",
    "bahraini": "BH",
    "bahrain": "BH",
    "omani": "OM",
    "oman": "OM",
    "lebanese": "LB",
    "lebanon": "LB",
    "jordanian": "JO",
    "jordan": "JO",
    "iranian": "IR",
    "iran": "IR",
    "iraqi": "IQ",
    "iraq": "IQ",
    "syrian": "SY",
    "syria": "SY",
    "egyptian": "EG",
    "egypt": "EG",
    "nigerian": "NG",
    "nigeria": "NG",
    "south african": "ZA",
    "south africa": "ZA",
    "kenyan": "KE",
    "kenya": "KE",
    "ghanaian": "GH",
    "ghana": "GH",
    "moroccan": "MA",
    "morocco": "MA",
    "libyan": "LY",
    "libya": "LY",
    # Asia-Pacific
    "chinese": "CN",
    "china": "CN",
    "prc": "CN",
    "peoples republic of china": "CN",
    "hong kong": "HK",
    "hong kong sar": "HK",
    "hongkong": "HK",
    "taiwanese": "TW",
    "taiwan": "TW",
    "japanese": "JP",
    "japan": "JP",
    "korean": "KR",
    "south korea": "KR",
    "republic of korea": "KR",
    "north korean": "KP",
    "north korea": "KP",
    "dprk": "KP",
    "indian": "IN",
    "india": "IN",
    "pakistani": "PK",
    "pakistan": "PK",
    "bangladeshi": "BD",
    "bangladesh": "BD",
    "sri lankan": "LK",
    "sri lanka": "LK",
    "singaporean": "SG",
    "singapore": "SG",
    "malaysian": "MY",
    "malaysia": "MY",
    "indonesian": "ID",
    "indonesia": "ID",
    "thai": "TH",
    "thailand": "TH",
    "vietnamese": "VN",
    "vietnam": "VN",
    "filipino": "PH",
    "philippine": "PH",
    "philippines": "PH",
    "australian": "AU",
    "australia": "AU",
    "new zealander": "NZ",
    "new zealand": "NZ",
    # Offshore and dependent territories that dominate corporate PSC filings
    "jersey": "JE",
    "guernsey": "GG",
    "isle of man": "IM",
    "manx": "IM",
    "gibraltar": "GI",
    "gibraltarian": "GI",
    "british virgin islands": "VG",
    "bvi": "VG",
    "virgin islands british": "VG",
    "cayman islands": "KY",
    "caymanian": "KY",
    "cayman": "KY",
    "bermuda": "BM",
    "bermudian": "BM",
    "bahamas": "BS",
    "bahamian": "BS",
    "belize": "BZ",
    "belizean": "BZ",
    "seychelles": "SC",
    "seychellois": "SC",
    "mauritius": "MU",
    "mauritian": "MU",
    "marshall islands": "MH",
    "liechtenstein": "LI",
    "monaco": "MC",
    "monegasque": "MC",
    "andorra": "AD",
    "san marino": "SM",
    "anguilla": "AI",
    "turks and caicos islands": "TC",
    "turks and caicos": "TC",
    "saint kitts and nevis": "KN",
    "st kitts and nevis": "KN",
    "nevis": "KN",
    "samoa": "WS",
    "vanuatu": "VU",
    "curacao": "CW",
    "aruba": "AW",
    "saint vincent and the grenadines": "VC",
    "barbados": "BB",
    "barbadian": "BB",
    "antigua and barbuda": "AG",
    "dominica": "DM",
    "grenada": "GD",
}

# Jurisdictions whose corporate registers do not disclose beneficial ownership
# publicly, or disclose it only to authorities. Presence of one of these in an
# ownership chain is not evidence of wrongdoing — it is a reason the chain
# cannot be resolved further from open data, which is precisely what the
# opacity analysis needs to quantify.
SECRECY_JURISDICTIONS: frozenset[str] = frozenset(
    {
        "VG",
        "KY",
        "BM",
        "BS",
        "BZ",
        "SC",
        "MU",
        "MH",
        "PA",
        "AI",
        "TC",
        "KN",
        "WS",
        "VU",
        "CW",
        "AW",
        "VC",
        "AG",
        "DM",
        "GD",
        "LI",
        "MC",
        "AD",
        "SM",
        "JE",
        "GG",
        "IM",
        "GI",
        "CY",
        "MT",
        "LU",
        "AE",
    }
)

_ISO2 = set(_COUNTRY_ALIASES.values())


@lru_cache(maxsize=100_000)
def code_country(value: str | None) -> str:
    """Map a free-text country or demonym to ISO 3166-1 alpha-2.

    Returns ``""`` when the value cannot be coded, so callers can distinguish
    "not stated" from "stated but unrecognised" by checking the raw field.
    """
    if not value:
        return ""
    raw = str(value).strip()
    if len(raw) == 2 and raw.upper() in _ISO2:
        return raw.upper()
    norm = normalize_text(raw)
    if not norm:
        return ""
    if norm in _COUNTRY_ALIASES:
        return _COUNTRY_ALIASES[norm]

    # Multi-nationality fields such as "British, Irish" or "British/Cypriot".
    # Split the RAW value, not the normalised one: `normalize_text` strips
    # punctuation, so by then "British, Irish" has already become
    # "british irish" and every separator has been erased. Dual nationality is
    # common among PSCs of UK companies, so failing here would silently drop
    # the nationality feature for exactly the cross-border individuals the
    # pipeline most needs to resolve.
    lowered = raw.lower()
    for sep in (",", "/", ";", "&", " and "):
        if sep in lowered:
            head = normalize_text(lowered.split(sep)[0])
            if head in _COUNTRY_ALIASES:
                return _COUNTRY_ALIASES[head]
    return ""


def code_nationality(value: str | None) -> str:
    """Alias of :func:`code_country`, kept separate for call-site readability."""
    return code_country(value)


def is_secrecy_jurisdiction(iso2: str | None) -> bool:
    return bool(iso2) and str(iso2).upper() in SECRECY_JURISDICTIONS
