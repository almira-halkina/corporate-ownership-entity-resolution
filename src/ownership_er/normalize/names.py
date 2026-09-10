"""Name normalisation, fingerprinting and phonetic keying.

Everything downstream — blocking recall, feature quality, cluster purity —
is bounded by how well this module collapses cosmetic variation without
collapsing real distinctions. Two failure modes matter and pull in opposite
directions:

*Under-normalising* ("SEVERSTAL OAO" vs "OAO Severstal") leaves true matches in
different blocks, and blocking recall is an upper bound on end-to-end recall —
no downstream model recovers a pair that was never generated.

*Over-normalising* ("Smith Holdings I" vs "Smith Holdings II" -> "smith
holdings") manufactures collisions that the matcher then has to spend
discriminative power undoing.

The design therefore keeps several keys of decreasing strictness rather than one
canonical form, and lets the blocking stage choose which to union.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

import jellyfish

__all__ = [
    "company_fingerprint",
    "detect_legal_form",
    "initials_key",
    "normalize_text",
    "person_fingerprint",
    "person_name_parts",
    "phonetic_key",
    "sorted_token_key",
    "strip_person_titles",
    "transliterate",
]

_PUNCT_RE = re.compile(r"[^\w\s]", flags=re.UNICODE)
_WS_RE = re.compile(r"\s+")
_ROMAN_RE = re.compile(r"^(?:i{1,3}|iv|vi{0,3}|ix|xi{0,3})$")

# ---------------------------------------------------------------------------
# Legal forms
# ---------------------------------------------------------------------------
# Mapping from surface variants to a canonical legal-form tag. Kept broad
# because PSC corporate records name foreign parents in their own conventions:
# a UK company's controlling entity is frequently a Cypriot, Luxembourgish,
# BVI or Russian vehicle written as it appears on the foreign register.

LEGAL_FORMS: dict[str, str] = {
    # United Kingdom / Ireland
    "limited": "LTD",
    "ltd": "LTD",
    "ltd.": "LTD",
    "co ltd": "LTD",
    "company limited": "LTD",
    "public limited company": "PLC",
    "plc": "PLC",
    "p.l.c.": "PLC",
    "llp": "LLP",
    "limited liability partnership": "LLP",
    "lp": "LP",
    "limited partnership": "LP",
    "cic": "CIC",
    "community interest company": "CIC",
    "unlimited": "UNLTD",
    "teoranta": "LTD",
    "cyfyngedig": "LTD",
    # United States
    "inc": "INC",
    "inc.": "INC",
    "incorporated": "INC",
    "corp": "CORP",
    "corp.": "CORP",
    "corporation": "CORP",
    "llc": "LLC",
    "l.l.c.": "LLC",
    "l.p.": "LP",
    "pllc": "LLC",
    "co": "CO",
    "company": "CO",
    # German-speaking
    "gmbh": "GMBH",
    "mbh": "GMBH",
    "gmbh & co kg": "GMBH_KG",
    "ag": "AG",
    "aktiengesellschaft": "AG",
    "kg": "KG",
    "kgaa": "KGAA",
    "ug": "UG",
    "gesmbh": "GMBH",
    # France / Belgium / Luxembourg
    "sarl": "SARL",
    "s.a.r.l.": "SARL",
    "sa": "SA",
    "s.a.": "SA",
    "sas": "SAS",
    "s.a.s.": "SAS",
    "sasu": "SAS",
    "sca": "SCA",
    "sprl": "SPRL",
    "scs": "SCS",
    "sci": "SCI",
    # Netherlands / Belgium
    "bv": "BV",
    "b.v.": "BV",
    "nv": "NV",
    "n.v.": "NV",
    "cv": "CV",
    "cooperatief": "COOP",
    "stichting": "STICHTING",
    # Nordics
    "ab": "AB",
    "aktiebolag": "AB",
    "as": "AS",
    "a/s": "AS",
    "asa": "ASA",
    "aps": "APS",
    "oy": "OY",
    "oyj": "OYJ",
    "hf": "HF",
    "ehf": "EHF",
    # Southern Europe
    "spa": "SPA",
    "s.p.a.": "SPA",
    "srl": "SRL",
    "s.r.l.": "SRL",
    "sl": "SL",
    "s.l.": "SL",
    "sau": "SA",
    "lda": "LDA",
    "unipessoal lda": "LDA",
    # Central & Eastern Europe
    "sp z oo": "SPZOO",
    "sp. z o.o.": "SPZOO",
    "spolka z ograniczona odpowiedzialnoscia": "SPZOO",
    "sa spolka akcyjna": "SA",
    "as spolecnost": "AS",
    "sro": "SRO",
    "s.r.o.": "SRO",
    "kft": "KFT",
    "zrt": "ZRT",
    "nyrt": "NYRT",
    "doo": "DOO",
    "d.o.o.": "DOO",
    "dd": "DD",
    "ad": "AD",
    "eood": "EOOD",
    "ood": "OOD",
    "srl romania": "SRL",
    # Russian / CIS — the transliterated forms matter more than the Cyrillic,
    # because PSC filings are Latin-script even for Russian parents.
    "ooo": "OOO",
    "o.o.o.": "OOO",
    "zao": "ZAO",
    "oao": "OAO",
    "pao": "PAO",
    "ao": "AO",
    "pjsc": "PAO",
    "ojsc": "OAO",
    "cjsc": "ZAO",
    "jsc": "AO",
    "joint stock company": "AO",
    "public joint stock company": "PAO",
    "open joint stock company": "OAO",
    "closed joint stock company": "ZAO",
    "too": "TOO",
    "tov": "TOV",
    "chp": "CHP",
    # Middle East / Turkey
    "as turkey": "AS",
    "anonim sirketi": "AS",
    "limited sirketi": "LTD",
    "llc uae": "LLC",
    "fzco": "FZCO",
    "fze": "FZE",
    "fzc": "FZC",
    "wll": "WLL",
    "sal": "SAL",
    "ltd israel": "LTD",
    # Asia-Pacific
    "pty": "PTY",
    "pty ltd": "PTY_LTD",
    "proprietary limited": "PTY_LTD",
    "sdn bhd": "SDN_BHD",
    "sendirian berhad": "SDN_BHD",
    "bhd": "BHD",
    "berhad": "BHD",
    "pte": "PTE",
    "pte ltd": "PTE_LTD",
    "kk": "KK",
    "kabushiki kaisha": "KK",
    "yk": "YK",
    "gk": "GK",
    "co ltd japan": "LTD",
    "pt": "PT",
    "tbk": "TBK",
    "pvt": "PVT",
    "pvt ltd": "PVT_LTD",
    "private limited": "PVT_LTD",
    # Offshore vehicles that dominate UK PSC corporate filings
    "ibc": "IBC",
    "international business company": "IBC",
    "spc": "SPC",
    "segregated portfolio company": "SPC",
    "vcc": "VCC",
    "foundation": "FOUNDATION",
    "trust": "TRUST",
    "trustees": "TRUST",
    "holdings": "",  # descriptive, not a legal form — kept as a name token
}

# The lookup is keyed on *normalised* forms, not the literals above.
# `normalize_text` strips punctuation, so "s.a.r.l." arrives as "s a r l" and a
# raw-key lookup silently never fires — every SARL entity then keeps its legal
# form inside the fingerprint and fails to match the same company written
# "SARL". Normalising the keys at import time is what makes the table actually
# apply; `test_normalize.py` pins the SARL case specifically.
_NORMALISED_LEGAL_FORMS: dict[str, str] = {}
for _raw, _tag in LEGAL_FORMS.items():
    if not _tag:
        continue
    _key = _WS_RE.sub(" ", _PUNCT_RE.sub(" ", _raw.lower())).strip()
    if _key:
        _NORMALISED_LEGAL_FORMS.setdefault(_key, _tag)

# Longest-first so multi-word forms win over their own prefixes.
_LEGAL_FORM_KEYS = sorted(_NORMALISED_LEGAL_FORMS, key=lambda s: (-len(s.split()), -len(s)))

# Personal titles and honorifics that carry no identifying information.
PERSON_TITLES: frozenset[str] = frozenset(
    {
        "mr",
        "mrs",
        "ms",
        "miss",
        "mx",
        "dr",
        "prof",
        "professor",
        "sir",
        "dame",
        "lord",
        "lady",
        "baron",
        "baroness",
        "earl",
        "count",
        "countess",
        "duke",
        "duchess",
        "hon",
        "honourable",
        "rev",
        "reverend",
        "fr",
        "father",
        "capt",
        "captain",
        "col",
        "colonel",
        "gen",
        "general",
        "maj",
        "major",
        "lt",
        "lieutenant",
        "sgt",
        "sergeant",
        "adm",
        "admiral",
        "cdr",
        "the rt hon",
        "rt hon",
        "his excellency",
        "her excellency",
        "he",
        "herr",
        "frau",
        "monsieur",
        "madame",
        "mme",
        "mlle",
        "senor",
        "senora",
        "signor",
        "signora",
        "dott",
        "ing",
        "arch",
        "adv",
        "advocate",
    }
)

# Generational and post-nominal suffixes.
PERSON_SUFFIXES: frozenset[str] = frozenset(
    {
        "jr",
        "junior",
        "sr",
        "senior",
        "ii",
        "iii",
        "iv",
        "v",
        "phd",
        "md",
        "mba",
        "esq",
        "cbe",
        "obe",
        "mbe",
        "kbe",
        "dbe",
        "qc",
        "kc",
        "frcs",
        "fca",
        "aca",
        "acca",
        "cfa",
        "jp",
        "ma",
        "bsc",
        "msc",
        "ba",
    }
)

# ---------------------------------------------------------------------------
# Transliteration
# ---------------------------------------------------------------------------
# A deliberately small, dependency-free Cyrillic -> Latin table. The production
# answer is ICU (via `followthemoney`'s optional PyICU dependency), which covers
# Arabic, Han, Devanagari and the rest; that path is used automatically when the
# `ftm` extra is installed. This fallback exists so the repository stays
# installable with `pip install -e .` on a machine without libicu, which is the
# difference between a reviewer running the pipeline and giving up.

_CYRILLIC_MAP = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",
    "ё": "e",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "й": "y",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "kh",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "shch",
    "ъ": "",
    "ы": "y",
    "ь": "",
    "э": "e",
    "ю": "yu",
    "я": "ya",
    # Ukrainian / Belarusian additions
    "і": "i",
    "ї": "yi",
    "є": "ye",
    "ґ": "g",
    "ў": "w",
}

# Latin letters that Unicode decomposition does NOT handle. NFKD splits "ó"
# into "o" plus a combining accent, but "ł", "ø" and "đ" are atomic codepoints
# with no decomposition — stripping combining marks leaves them untouched, so
# "Zieliński" folds to "zielinski" while "Łukasz" stays "łukasz" and fails to
# match the same person filed as "Lukasz". Polish, Nordic and Balkan surnames
# are common enough in the UK register that this is a measurable recall loss,
# not a curiosity.
_LATIN_SPECIALS = {
    "ł": "l",
    "ø": "o",
    "đ": "d",
    "ð": "d",
    "þ": "th",
    "ß": "ss",
    "æ": "ae",
    "œ": "oe",
    "å": "a",
    "ı": "i",
    "ŀ": "l",
    "ħ": "h",
    "ŋ": "n",
    "ŧ": "t",
    "ĸ": "k",
}

_ICU_TRANSLITERATOR: object | None = None
_ICU_TRIED = False


def _icu_transliterate(text: str) -> str | None:
    """Use ICU if available; return ``None`` to signal the fallback path."""
    global _ICU_TRANSLITERATOR, _ICU_TRIED
    if not _ICU_TRIED:
        _ICU_TRIED = True
        try:  # pragma: no cover - depends on optional system library
            from icu import Transliterator

            _ICU_TRANSLITERATOR = Transliterator.createInstance("Any-Latin; Latin-ASCII; Lower")
        except Exception:
            _ICU_TRANSLITERATOR = None
    if _ICU_TRANSLITERATOR is None:
        return None
    return str(_ICU_TRANSLITERATOR.transliterate(text))  # type: ignore[attr-defined]


def transliterate(text: str) -> str:
    """Fold a name to lowercase ASCII, romanising non-Latin scripts where possible."""
    if not text:
        return ""
    viaicu = _icu_transliterate(text)
    if viaicu is not None:
        return viaicu
    out: list[str] = []
    for ch in text.lower():
        if ch in _CYRILLIC_MAP:
            out.append(_CYRILLIC_MAP[ch])
        elif ch in _LATIN_SPECIALS:
            out.append(_LATIN_SPECIALS[ch])
        else:
            out.append(ch)
    folded = unicodedata.normalize("NFKD", "".join(out))
    return "".join(c for c in folded if not unicodedata.combining(c))


# ---------------------------------------------------------------------------
# Core normalisation
# ---------------------------------------------------------------------------


@lru_cache(maxsize=200_000)
def normalize_text(value: str | None) -> str:
    """Lowercase, romanise, strip punctuation and collapse whitespace."""
    if not value:
        return ""
    text = transliterate(str(value))
    text = _PUNCT_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def detect_legal_form(name: str) -> tuple[str, str | None]:
    """Split a company name into ``(base_name, legal_form_tag)``.

    Handles the form appearing as a suffix (``ACME LIMITED``) or a prefix
    (``OOO ACME``), which is the usual convention for Russian, Czech and Polish
    entities and a common source of missed matches when only suffixes are
    stripped.
    """
    norm = normalize_text(name)
    if not norm:
        return "", None
    tokens = norm.split()
    for key in _LEGAL_FORM_KEYS:
        parts = key.split()
        n = len(parts)
        if len(tokens) > n and tokens[-n:] == parts:
            return " ".join(tokens[:-n]), _NORMALISED_LEGAL_FORMS[key]
        if len(tokens) > n and tokens[:n] == parts:
            return " ".join(tokens[n:]), _NORMALISED_LEGAL_FORMS[key]
    return norm, None


@lru_cache(maxsize=200_000)
def company_fingerprint(name: str | None) -> str:
    """Order-insensitive company key with the legal form removed.

    ``"OAO Severstal"``, ``"Severstal OAO"`` and ``"SEVERSTAL, O.A.O."`` all
    collapse to ``"severstal"``. Numerals are preserved verbatim, because
    ``"Property 12 Ltd"`` and ``"Property 21 Ltd"`` are different companies and
    token sorting would otherwise merge them.
    """
    if not name:
        return ""
    base, _form = detect_legal_form(name)
    if not base:
        return ""
    tokens = [t for t in base.split() if t]
    return " ".join(sorted(tokens))


def strip_person_titles(name: str) -> str:
    """Remove leading honorifics and trailing post-nominals."""
    tokens = normalize_text(name).split()
    while tokens and tokens[0] in PERSON_TITLES:
        tokens.pop(0)
    while tokens and (tokens[-1] in PERSON_SUFFIXES or _ROMAN_RE.match(tokens[-1] or "")):
        tokens.pop()
    return " ".join(tokens)


def person_name_parts(
    name: str | None,
    forename: str | None = None,
    middle_name: str | None = None,
    surname: str | None = None,
) -> tuple[str, str, str]:
    """Return ``(first, middle, last)``, preferring pre-split source fields.

    Companies House supplies ``name_elements`` for most individual PSC records.
    Where it does, trusting it beats re-parsing the display name — the registry
    already knows which token is the surname, and heuristics get that wrong for
    names that do not follow Anglophone ordering.
    """
    if surname:
        return (
            normalize_text(forename),
            normalize_text(middle_name),
            normalize_text(surname),
        )
    stripped = strip_person_titles(name or "")
    if not stripped:
        return "", "", ""
    tokens = stripped.split()
    if len(tokens) == 1:
        return "", "", tokens[0]
    return tokens[0], " ".join(tokens[1:-1]), tokens[-1]


def person_fingerprint(
    name: str | None,
    forename: str | None = None,
    middle_name: str | None = None,
    surname: str | None = None,
) -> str:
    """Order-insensitive person key over first and last name only.

    Middle names are excluded because their presence is inconsistent across
    filings for the same individual; they are recovered later as a matcher
    feature, where partial evidence can be weighted rather than being forced to
    a binary blocking decision.
    """
    first, _mid, last = person_name_parts(name, forename, middle_name, surname)
    tokens = sorted(t for t in (first, last) if t)
    return " ".join(tokens)


@lru_cache(maxsize=200_000)
def phonetic_key(value: str | None) -> str:
    """Soundex key over each token, for recall on transcription variants.

    Soundex rather than Metaphone, and the choice was measured rather than
    assumed. On the transliteration doublets that dominate this register,
    Soundex collapses all six of Kowalczyk/Kowalchyk, Shevchenko/Schevchenko,
    Petrov/Petroff, Sokolov/Sokoloff, Zielinski/Zielinsky and
    Abramovich/Abramovic; Metaphone collapses three, splitting exactly the
    Slavic consonant clusters that matter most here.

    Soundex is the coarser code and therefore collides more. That is an
    acceptable trade in a *blocking* key, where recall is the binding
    constraint and precision is the matcher's job — and the collision cost is
    bounded anyway, because every phonetic blocking key is paired with birth
    year rather than used alone.
    """
    norm = normalize_text(value)
    if not norm:
        return ""
    return " ".join(jellyfish.soundex(tok) for tok in norm.split() if tok)


def sorted_token_key(value: str | None) -> str:
    """Whitespace-normalised, alphabetically sorted tokens."""
    norm = normalize_text(value)
    return " ".join(sorted(norm.split())) if norm else ""


def initials_key(value: str | None) -> str:
    """First letter of each token — a very loose key, useful only in combination."""
    norm = normalize_text(value)
    return "".join(tok[0] for tok in norm.split() if tok) if norm else ""
