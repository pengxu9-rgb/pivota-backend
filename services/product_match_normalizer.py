"""Shared normalizer for cross-merchant product matching (phase 1, PR "M1").

Pure functions, no I/O and no database; importing it loads no HTTP, database, config or LLM module
(the brand families come from services/brand_spelling.py, a test pins this). Every matching tier of the cross-merchant design
(T0a GTIN, T0b exact title, T0c title core, and the vetoes on all of them) reads its signals
from here, so the strategy, the census and later the agent verifier agree on one reading of a
listing title.

THE IDENTITY DEFINITION THIS MODULE IMPLEMENTS (Peng, 2026-10-16)
-----------------------------------------------------------------
A product is brand + line + formulation/edition. SIZE and SHADE are variants of it, not
identity. So `title_core` strips size, pack and shade out of the title and returns them
SEPARATELY; two listings are the same buyable item only when the core, the size (incl. pack
count), the shade and the conflict tokens all agree. Phase 1 merges same-size only, which is why
`match_key_t0c` carries the parsed size in the key.

That definition DIFFERS from two existing rules, deliberately:
  - catalog_identity.normalize_title / content_key keep every digit ("sizes are identity": 30ml
    and 50ml mint two content_keys). This module is a MATCHING normalizer and never mints a
    content_key; T0b still uses normalize_title verbatim (exact title, digits kept).
  - The DeepSeek official-match judge (services/pdp_matcher, StyleKorean lane) says "size does
    not differ" and matches across sizes. Here size is compared, not ignored: it is a variant
    attribute that must be EQUAL for a phase-1 merge.

WHERE EACH RULE CAME FROM
-------------------------
Brand (`canonical_brand`):
  - catalog_identity.normalize_brand: lowercase, strip (R)/(TM) and corporate suffixes.
  - match_brand (was pdp_matcher.retailer_match): drop trailing storefront words official/flagship.
  - brand_key (was curated_brand_feed._brand_key): alphanumeric-only casefold with "&" read as
    "and" (folds "Dr.Jart+"/"Dr. Jart", "Round Lab"/"Roundlab", "A'PIEU"/"Apieu").
  - RETAILER_BRAND_SPELLINGS: the MEASURED families (Tom Ford Beauty/Tom Ford, Christian Dior/Dior,
    ETUDE HOUSE/ETUDE, Kosé/Kose, SK-Ⅱ/SK-II, O HUI (오휘)/O HUI ...). The family key is the
    canonical brand. All three now live in services/brand_spelling.py (one copy; the old modules
    re-export them).
  - NEW here: accents folded before the family lookup (Lancôme -> lancome even for brands not in
    the map), and trailing cosmetics/skincare/region words dropped ("Benefit Cosmetics",
    "MISSHA US"). The gateway's brandCore also strips beauty/paris/professional/makeup; those are
    NOT stripped here because they are part of real names (Rare Beauty, Fenty Beauty vs Fenty
    Skin, L'Oreal Paris vs L'Oreal Professionnel) and the curated families already cover the
    measured cases (Tom Ford Beauty). Placeholders (Generic, Unbranded, N/A, Unknown, No Brand ...)
    and a retailer written as the brand (Amazon, YesStyle, Stylevana, Olive Young) canonicalize to
    "" -- PLACEHOLDER_BRAND_KEYS -- so they never key a match.

Title core (`title_core`) -- ported from TWO normalizers and reconciled:
  - PIVOTA-Agent src/services/retailerOfferIdentity.js titleCore: NFKD accent fold, "&" -> and,
    drop (TM)/(R), apostrophes, SIZE_RE (ml/l/fl oz/oz/g/kg/count/ct/pcs/sheets/pads/capsules/
    tablets, "N pack", "pack of N"), PACK_COUNT_RE (packCountMismatch: two stated counts must
    agree), and "shades kept apart".
    DIFFERENCES: (1) the gateway removes every brand TOKEN anywhere ("Glow Recipe" deletes the
    "Glow" of "Watermelon Glow"); here the brand is removed only as a whole contiguous phrase.
    (2) Its FORM_VARIANT_RE strips mini/jumbo/travel/value size/refill/tester as non-identity;
    here those are CONFLICT tokens (a refill or a tester is never the same buyable item).
    (3) Its normalizeText drops non-ASCII; here Hangul/CJK letters are kept (a Hangul-only token
    must not vanish into an empty core). (4) Its STOP_TOKENS drop skin/face/facial; here only
    pure function words are dropped (face lotion vs body lotion). (5) Single characters are kept:
    dropping them merged "Vitamin C Serum" with "Vitamin E Serum". "x" too (NARS "Orgasm X" is
    not "Orgasm"); only a leading "x" left by brand removal ("COSRX x Olive Young") is dropped.
  - pivota-backend services/pdp_matcher/retailer_match.retailer_match_key: unit-ANCHORED size
    stripping (a number is dropped only with a unit, so "96 mucin", "aha 7", "aloe 54.2" stay),
    decimal sizes read from the raw title, NxM pack notation, leading brand prefix, the promo
    list. DIFFERENCES: (1) its _PROMO strips set/kit/duo/bundle/gift/value/jumbo; here those are
    conflict tokens, and so are its new/renewal/limited/special-edition words (each can mark a
    reformulation or a different pack; a lone "new" one-sided is a veto). (2) It strips "free";
    here "free" stays ("Fragrance Free" is a different formula). (3) It strips "mg"; here mg is a
    STRENGTH and stays in the core (500mg vs 1000mg). (4) It keeps hyphens inside tokens
    ("dive-in"); here hyphens split ("DIVE-IN" == "Dive In").
  - Both normalizers DROP "+" ("Lotion+" == "Lotion"). Here no "+" is ever dropped. Read after
    sizes, SPF50+/PA+++ and "1+1" packs are consumed, and after a brand's own "+" (Dr.Jart+) went
    with the brand: a "+" glued to a word or a percent, or followed by no word ("Duo+", "B5+",
    "1%+", "Duo + SPF30 40ml", "Duo (+)", "Duo [+]") and the word "plus" are the conflict token
    `plus`; a "+" BETWEEN two words, "&" and "and" are one conjunction word in the core
    (CONJUNCTION_PLACEHOLDER, "join"), and exact_title_key reads them the same way.
  - Word ORDER: both compare token sets. T0c here keys on the ORDERED core ("Cream to Powder" vs
    "Powder to Cream"), and a format word moved to the conflicts leaves a positional placeholder
    ("fmt") in the core, so Laneige "Cream Skin" and "Skin Cream" stay apart; two or more format
    words also emit format_order:<a>><b> ("Gel to Cream" vs "Cream to Gel").
  - line_alias.spf_rating_conflict: SPF ratings must agree. Here every SPF/PA value is a conflict
    token on every tier (the alias lane's curated one-sided tolerance is not applied).

Size (`parse_size`): there was NO shared size normalizer (four regexes, none converting fl oz).
Nominal beauty conversions: printed packs say "1.0 fl oz / 30 ml", not 29.57 ml, so fl oz uses a
nominal table first and rounds otherwise; weight oz rounds and snaps to a nominal gram size
within 2%. A bare "oz" is weight unless the title pairs it with an equal ml value or names a
fragrance concentration / spray / mist (US fragrance titles write "3.4 oz" for 100 ml).
Fractions ("1/2 oz", "1 1/2 oz", "½ oz") are read as fractions; an improper or unreadable one
leaves the title with NO primary size. Values under 10 keep two decimals (4.25 g != 4.2 g).

GTIN (`gtin_canonical`, `gtin_is_restricted`): catalog_identity.validated_source_gtin (GS1
check digit, zero-pad to 14) plus GS1 prefix rules for restricted circulation, coupons and
ISBN/ISSN (sources at GS1_RESTRICTED_PREFIXES).

SAFETY DIRECTION. Every heuristic here errs toward NOT matching: an undetected shade stays in
the core (so two shades cannot meet), a one-sided conflict token is a disagreement, a title
with two different sizes has no primary size and so no T0c key, and two titles with no size
are "size_unknown" in veto_reasons (phase 1 needs a proven equal size).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Callable, Dict, FrozenSet, List, Mapping, NamedTuple, Optional, Sequence, Tuple, Union

from services.brand_spelling import RETAILER_BRAND_SPELLINGS, match_brand
from services.brand_spelling import brand_key as _brand_key
from services.catalog_identity import normalize_title, validated_source_gtin

__all__ = [
    "CONFLICT_LEXICON",
    "FLOZ_NOMINAL_ML",
    "FORMAT_LEXICON",
    "GS1_RESTRICTED_PREFIXES",
    "WEIGHT_NOMINAL_G",
    "ConflictRule",
    "ParsedSize",
    "Quantity",
    "TitleCore",
    "canonical_brand",
    "conflict_family",
    "conflict_tokens",
    "exact_title_key",
    "gtin_canonical",
    "gtin_is_restricted",
    "gtin_restriction",
    "match_key_t0c",
    "parse_size",
    "size_key",
    "title_core",
    "veto_reasons",
]


# ---------------------------------------------------------------------------
# Text folding
# ---------------------------------------------------------------------------

_VULGAR_FRACTION_RE = re.compile("[\u00bc-\u00be\u2150-\u215e]")
_MARKS_RE = re.compile("[\u2122\u2120\u00ae\u00a9]")  # ™ ℠ ® ©
_TEXT_MARKS_RE = re.compile(r"\((?:r|tm)\)", re.IGNORECASE)


def _fold(text: Any) -> str:
    """NFKC (full-width digits, the ㎖ sign, "º" -> o), accents dropped, casefolded.

    Accents are dropped by NFD + removing combining marks + NFC. The final NFC recomposes Hangul
    syllables (NFD splits them into jamo, which are letters, not marks), so Korean text survives
    intact while "Crème"/"Lancôme" fold to "creme"/"lancome".
    """
    if text is None:
        return ""
    # Vulgar fractions first: NFKC turns "1½" into "11⁄2" (eleven halves), so each becomes " n/d".
    # Trademark marks BEFORE NFKC: NFKC spells "™" as "TM" and "℠" as "SM", which then glue onto the
    # word ("Surge™" -> "surgetm") and hide a "+" in front of them ("Lotion+™").
    s = _MARKS_RE.sub(" ", str(text))
    s = _VULGAR_FRACTION_RE.sub(lambda m: " " + unicodedata.normalize("NFKC", m.group(0)).replace("⁄", "/"), s)
    s = unicodedata.normalize("NFKC", s)
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = unicodedata.normalize("NFC", s).casefold()
    s = s.replace("&", " and ").replace("’", "'").replace("‘", "'").replace("`", "'")
    # Textual "(TM)" / "(R)" only. NOT "(C)": a textual (C) is the cool-undertone shade code
    # ("Teint Idole 220 (C)") or a vitamin ("Vitamin (C) Suspension"); the © sign itself is
    # already gone (_MARKS_RE, before NFKC).
    s = _TEXT_MARKS_RE.sub(" ", s)
    # "N°5" / "Nº5" (NFKC: "no5") / "No.5" / "No 5" are one spelling: a NUMBERED LINE (Chanel N°5,
    # Olaplex No.3), so it stays in the core as "no 5" rather than being read as a shade code.
    s = re.sub(r"\bn(?:o\.?|°)\s*(?=\d)", "no ", s)
    return s


def _squash(text: str) -> str:
    return "".join(c for c in text if c.isalnum())


# ---------------------------------------------------------------------------
# 1. Brand
# ---------------------------------------------------------------------------

# Trailing words some stores append to a brand that do not change it: a vertical word that the
# measured families show is optional ("Benefit Cosmetics" / "Benefit", "Kosas Cosmetics"), and a
# region/storefront label ("MISSHA US", "Beauty of Joseon UK", "... Global"). "beauty" is
# deliberately absent (Rare Beauty, Fenty Beauty); "shop"/"store" too (The Face Shop).
_BRAND_TRAILING_NOISE = frozenset({
    "cosmetics", "cosmetic", "skincare",
    "us", "usa", "uk", "sg", "au", "eu", "global", "intl", "international",
})


# Values that fill a brand field without naming a brand: placeholders, and a RETAILER written as
# the brand (marketplace exports). Two listings sharing one of these share no maker, so they
# canonicalize to "" and never key a match. Compared as brand keys (alphanumeric, accents folded),
# after the trailing-noise strip ("Olive Young Global" -> "oliveyoung").
PLACEHOLDER_BRAND_KEYS = frozenset({
    "generic", "unbranded", "nobrand", "na", "none", "null", "nil", "unknown", "other", "others",
    "default", "brand", "various", "misc", "notapplicable", "test",
    "amazon", "yesstyle", "stylevana", "oliveyoung", "oliveyoungglobal",
})


def canonical_brand(raw: Any) -> str:
    """The brand's comparison key: a RETAILER_BRAND_SPELLINGS family when the brand is in a
    measured family, else its accent-folded alphanumeric key. "" for no brand.

    "COSRX Official" -> "cosrx"; "Round Lab" / "ROUNDLAB" -> "roundlab"; "Christian Dior" ->
    "dior"; "Dr.Jart+" -> "drjart"; "Lancôme" -> "lancome"; "Benefit Cosmetics" -> "benefit".
    """
    if raw is None:
        return ""
    return _canonical_brand(str(raw))


@lru_cache(maxsize=65536)
def _canonical_brand(raw: str) -> str:
    if not raw.strip():
        return ""
    base = match_brand(raw)  # normalize_brand + trailing official/flagship
    tokens = base.split()
    while len(tokens) > 1 and tokens[-1].strip(".,") in _BRAND_TRAILING_NOISE:
        tokens.pop()
    text = " ".join(tokens)
    # The map carries accented spellings as keys of their own (_brand_key keeps accents), so look
    # the unfolded key up first, then the folded one.
    family = RETAILER_BRAND_SPELLINGS.get(_brand_key(text))
    if family:
        return family  # a measured family is a real brand, never a placeholder
    folded = _brand_key(_fold(text))
    family = RETAILER_BRAND_SPELLINGS.get(folded, folded)
    return "" if family in PLACEHOLDER_BRAND_KEYS else family


def _brand_forms(raw_brand: Any) -> FrozenSet[str]:
    return _brand_forms_cached(None if raw_brand is None else str(raw_brand))


@lru_cache(maxsize=65536)
def _brand_forms_cached(raw_brand: Optional[str]) -> FrozenSet[str]:
    """Squashed spellings of a brand that may appear in its own titles: the raw brand, the
    canonical family key and every measured spelling of that family. Each >= 2 characters."""
    forms = set()
    if raw_brand is not None and str(raw_brand).strip():
        forms.add(_squash(_fold(raw_brand)))
        forms.add(_squash(_fold(match_brand(str(raw_brand)))))
        fam = canonical_brand(raw_brand)
        forms.add(fam)
        forms.update(_squash(_fold(k)) for k, v in RETAILER_BRAND_SPELLINGS.items() if v == fam)
    return frozenset(f for f in forms if len(f) >= 2)


# ---------------------------------------------------------------------------
# 2. Size
# ---------------------------------------------------------------------------

class Quantity(NamedTuple):
    """One stated size, unit normalized to ml | g | count."""

    value: float
    unit: str


@dataclass(frozen=True)
class ParsedSize:
    """What a title says about size.

    primary: the one size the listing is, or None when the title states none OR states two
             different sizes (a range, a set, "30ml / 50ml") -- ambiguous is not a size.
    sizes:   every distinct stated quantity after unit normalization (equivalent imperial and
             metric spellings of one size collapse: "1.7 fl oz / 50 ml" is one size).
    pack_count: units in the box ("2 x 30ml", "set of 3", "3-pack", "1+1"); 1 when unstated.
    """

    primary: Optional[Quantity]
    sizes: Tuple[Quantity, ...] = ()
    pack_count: int = 1


# Nominal fluid-ounce -> ml, as printed on beauty packs. Anything not listed rounds
# value x 29.5735. Spec values (Peng): 0.33, 0.5, 1.0/1.01, 1.7/1.69, 2.5, 3.3/3.4, 4.2, 5,
# 6.7/6.76, 8.4/8.45, 16.9. Added: the metric round sizes' usual US labels (0.17=5, 0.25=7.5,
# 0.34=10, 0.68=20, 0.84/0.85=25, 1.35=40, 2.03=60, 2.53=75, 3.04=90, 3.38=100, 4.05=120,
# 5.07=150, 10.1=300, 13.5=400, 33.8=1000), 1.6=50 (European fragrance labels print 50 ml as
# "1.6 fl oz") and 8.0=236 (US labels truncate 236.6; rounding would say 237). Not added: 2.0
# (59 ml is a real US size), 4.0 (118 rounds right), 16.0 (473 rounds right).
FLOZ_NOMINAL_ML: Dict[float, float] = {
    0.17: 5, 0.25: 7.5, 0.33: 10, 0.34: 10, 0.5: 15, 0.51: 15, 0.68: 20, 0.84: 25, 0.85: 25,
    1.0: 30, 1.01: 30, 1.35: 40, 1.6: 50, 1.69: 50, 1.7: 50, 2.03: 60, 2.5: 75, 2.53: 75, 3.04: 90,
    3.3: 100, 3.38: 100, 3.4: 100, 4.05: 120, 4.2: 125, 5.0: 150, 5.07: 150, 6.7: 200,
    6.76: 200, 8.0: 236, 8.4: 250, 8.45: 250, 10.1: 300, 13.5: 400, 16.9: 500, 33.8: 1000,
}
# Weight oz -> g rounds value x 28.3495, then snaps to one of these within 2% (2.5 oz = 70.9 g is
# printed "70 g"; 0.35 oz = 9.9 g is "10 g"). 1.7 oz = 48 g stays 48 (50 is 3.6% away).
WEIGHT_NOMINAL_G: Tuple[int, ...] = (
    3, 5, 7, 8, 10, 12, 14, 15, 20, 25, 28, 30, 35, 40, 45, 50, 55, 60, 70, 75, 80, 85, 90, 100,
    110, 113, 120, 125, 150, 170, 200, 225, 227, 250, 300, 340, 350, 400, 450, 454, 500,
)
_SNAP_TOLERANCE = 0.02
# Two spellings in ONE title are the same size when within this fraction ("2.5 oz / 70 g"), or
# within 0.3 g/ml -- 0.01 oz, the step at which labels truncate small sizes ("4.8 g / 0.16 oz":
# 0.16 oz is 4.54 g, 5% off at that size).
_SAME_TITLE_TOLERANCE = 0.03
_SAME_TITLE_SLACK = 0.3

_NUM = r"(\d+(?:[.,]\d{1,2})?)"
_UNIT_ALTS: Tuple[Tuple[str, str], ...] = (
    # (canonical unit, regex) -- longest spellings first inside each alternative.
    ("floz", r"fl\.?\s*oz\.?|fluid\s+ounces?|floz|fl\.?\s*ounces?"),
    ("oz", r"oz\.?|ounces?"),
    ("ml", r"millilit(?:er|re)s?|mls|ml"),
    ("kg", r"kilograms?|kgs?"),
    ("l", r"lit(?:er|re)s?|ltrs?|l"),
    ("g", r"grams?|grammes?|grs|gr|gms?|g"),
    ("count", r"counts?|ct|pcs|pc|pieces?|ea|sheets?|pads?|patch(?:es)?|masks?|capsules?|caps|"
              r"tablets?|softgels?|sticks?|pairs?|wipes?|strips?"),
)
_UNIT_RE = "|".join(f"(?P<u_{name}>{alt})" for name, alt in _UNIT_ALTS)
# A unit ends at a non-alphanumeric (or at the "x" of "50mlx2"), and not at "-letter"
# ("15 L-Ascorbic" is not 15 liters).
_UNIT_END = r"(?![a-wyz0-9])(?!x[a-z])(?!-[a-z])"
_QTY = rf"{_NUM}\s*(?:{_UNIT_RE}){_UNIT_END}"
# Never read the denominator of "1/2 oz" as "2 oz": a number right after "digit/" is not a size.
_QTY_RE = re.compile(rf"(?<![\w.])(?<!\d/)(?<!\d⁄){_QTY}")
# "1/2 oz", "1 1/2 oz", "1-1/2 fl oz", "½ oz" (folded to " 1/2"). A fraction that is not a proper
# one (3/2, x/0) makes the title's size ambiguous rather than guessed.
_FRACTION_QTY_RE = re.compile(
    rf"(?<![\w./])(?:(\d{{1,3}})[\s-]+)?(\d{{1,2}})\s*[/⁄]\s*(\d{{1,2}})\s*(?:{_UNIT_RE}){_UNIT_END}"
)
_LEFTOVER_FRACTION_RE = re.compile(rf"\d\s*[/⁄]\s*\d+\s*(?:{_UNIT_RE}){_UNIT_END}")
# A multiplier: "×"/"*" with any spacing; a plain "x" spaced on BOTH sides or on NEITHER ("2 x 30ml",
# "2x30ml"). "5X 150ml" is Missha's "5X" line, not five bottles.
_X = r"(?:\s*[×*]\s*|\s+x\s+|x)"
# "2 x 30ml", "3ea x 24 patches", "2pcs x 50ml"
_PACK_BEFORE_RE = re.compile(rf"(?<![\w.])(\d{{1,3}})\s*(?:pcs?|ea|packs?)?{_X}(?P<q>{_NUM}\s*(?:{_UNIT_RE}){_UNIT_END})")
# "50ml x 2", "25ml x 10 sheets"
_PACK_AFTER_RE = re.compile(
    rf"(?<![\w.])(?P<q>{_NUM}\s*(?:{_UNIT_RE}){_UNIT_END}){_X}(\d{{1,3}})(?:\s*(?:pcs?|ea|packs?|units?|"
    r"counts?|ct|sheets?|pads?|patch(?:es)?|masks?))?(?![\w.])"
)
# "15-30ml", "15ml - 30ml", "15~30 ml"
_RANGE_RE = re.compile(
    rf"(?<![\w.])(?:{_NUM}[-–~]{_NUM}\s*(?P<r1>ml|g|oz|fl\.?\s*oz\.?)|"
    rf"{_NUM}\s*(?P<r2a>ml|g)\s*[-–~]\s*{_NUM}\s*(?P<r2b>ml|g)){_UNIT_END}"
)
_PACK_PHRASES: Tuple[Tuple[re.Pattern, Callable[[re.Match], int]], ...] = (
    (re.compile(r"\b(?:pack|set|box|bundle)\s+of\s+(\d{1,3})\b"), lambda m: int(m.group(1))),
    (re.compile(r"(?<![\w.])(\d{1,3})\s*-?\s*(?:packs?|pk)\b"), lambda m: int(m.group(1))),
    (re.compile(r"\btwin[\s-]*pack\b|\bdouble[\s-]*pack\b"), lambda m: 2),
    (re.compile(r"\btriple[\s-]*pack\b"), lambda m: 3),
    # Korean promo "1+1" (buy one, get one) = two units; "2+1" = three.
    (re.compile(r"(?<![\w+.])([1-3])\s*\+\s*([1-3])(?![\w+%.])"), lambda m: int(m.group(1)) + int(m.group(2))),
    # A stray multiplier: "Hydrogel Mask (x 5)".
    (re.compile(r"(?<![\w.])[x×]\s?(\d{1,3})(?![\w.])"), lambda m: int(m.group(1))),
)
_FRAGRANCE_CONTEXT_RE = re.compile(
    r"\b(?:eau\s+de|edp|edt|parfum|perfume|cologne|spray|mist|extrait)\b"
)


def _num(text: str) -> float:
    return float(text.replace(",", "."))


def _unit_of(m: re.Match) -> str:
    for name, _alt in _UNIT_ALTS:
        if m.group(f"u_{name}"):
            return name
    raise ValueError("no unit group matched")  # pragma: no cover - the regex requires one


def _round_size(value: float) -> float:
    """Two decimals under 10 (4.25 g and 4.2 g are different lipsticks), one above."""
    r = round(value, 2) if value < 10 else round(value, 1)
    return float(int(r)) if r == int(r) else r


def _floz_to_ml(value: float) -> float:
    nominal = FLOZ_NOMINAL_ML.get(round(value, 2))
    return float(nominal) if nominal is not None else float(round(value * 29.5735))


def _ozw_to_g(value: float) -> float:
    """Snap to a nominal gram size within 2%, else round -- to two decimals under 10 g, like
    every other size (0.15 oz = 4.25 g and 0.14 oz = 3.97 g are different pencils)."""
    exact = value * 28.3495
    best = min(WEIGHT_NOMINAL_G, key=lambda n: abs(n - exact))
    if abs(best - exact) <= _SNAP_TOLERANCE * best:
        return float(best)
    return round(exact, 2) if exact < 10 else float(round(exact))


def _close(a: float, b: float, tol: float = _SAME_TITLE_TOLERANCE) -> bool:
    return abs(a - b) <= max(tol * max(a, b), _SAME_TITLE_SLACK)


class _RawQty(NamedTuple):
    value: float
    unit: str  # floz | oz | ml | l | g | kg | count


@dataclass
class _SizeScan:
    raw: List[_RawQty] = field(default_factory=list)
    packs: List[int] = field(default_factory=list)
    spans: List[Tuple[int, int]] = field(default_factory=list)
    ambiguous: bool = False


def _scan_sizes(text: str) -> _SizeScan:
    """Find every size / pack expression in FOLDED text. Spans are positions in `text`."""
    scan = _SizeScan()
    work = list(text)

    def take(start: int, end: int) -> None:
        scan.spans.append((start, end))
        for i in range(start, end):
            work[i] = " "

    def current() -> str:
        return "".join(work)

    def qty_from(fragment: str) -> Optional[_RawQty]:
        m = _QTY_RE.search(fragment)
        if not m:
            return None
        return _RawQty(_num(m.group(1)), _unit_of(m))

    for m in list(_FRACTION_QTY_RE.finditer(current())):
        whole, num, den = int(m.group(1) or 0), int(m.group(2)), int(m.group(3))
        if 0 < num < den:
            scan.raw.append(_RawQty(whole + num / den, _unit_of(m)))
        else:
            scan.ambiguous = True
        take(m.start(), m.end())
    if _LEFTOVER_FRACTION_RE.search(current()):
        scan.ambiguous = True
    for m in list(_PACK_BEFORE_RE.finditer(current())):
        q = qty_from(m.group("q"))
        if q:
            scan.packs.append(int(m.group(1)))
            scan.raw.append(q)
            take(m.start(), m.end())
    for m in list(_PACK_AFTER_RE.finditer(current())):
        q = qty_from(m.group("q"))
        if q:
            scan.packs.append(int(m.groups()[-1]))
            scan.raw.append(q)
            take(m.start(), m.end())
    for m in list(_RANGE_RE.finditer(current())):
        if m.group("r1"):
            nums = [g for g in m.groups()[:2]]
            unit = m.group("r1")
        else:
            nums = [m.groups()[3], m.groups()[5]]
            unit = m.group("r2a")
        unit_name = "floz" if unit.startswith("fl") else unit
        lo, hi = _num(nums[0]), _num(nums[1])
        if 0 < lo < hi:
            scan.raw.extend([_RawQty(lo, unit_name), _RawQty(hi, unit_name)])
            take(m.start(), m.end())
    for m in list(_QTY_RE.finditer(current())):
        scan.raw.append(_RawQty(_num(m.group(1)), _unit_of(m)))
        take(m.start(), m.end())
    for pattern, count in _PACK_PHRASES:
        for m in list(pattern.finditer(current())):
            n = count(m)
            if n >= 1:
                scan.packs.append(n)
                take(m.start(), m.end())
    return scan


def _resolve(scan: _SizeScan, text: str) -> ParsedSize:
    raw = [q for q in scan.raw if q.value > 0]
    metric_ml = [q.value * (1000 if q.unit == "l" else 1) for q in raw if q.unit in ("ml", "l")]
    metric_g = [q.value * (1000 if q.unit == "kg" else 1) for q in raw if q.unit in ("g", "kg")]
    fragrance = bool(_FRAGRANCE_CONTEXT_RE.search(text))
    ml: List[float] = list(metric_ml)
    g: List[float] = list(metric_g)
    count: List[float] = []
    for q in raw:
        if q.unit == "count":
            count.append(float(int(q.value)))
        elif q.unit == "floz":
            conv = _floz_to_ml(q.value)
            if not any(_close(conv, v) for v in metric_ml):
                ml.append(conv)
        elif q.unit == "oz":
            as_ml, as_g = _floz_to_ml(q.value), _ozw_to_g(q.value)
            if any(_close(as_ml, v) for v in metric_ml):
                continue  # "1.7 oz / 50 ml": the oz is the same fluid size
            if any(_close(q.value * 28.3495, v) for v in metric_g):
                continue  # "2.5 oz / 70 g": the oz is the same weight
            if metric_ml or (fragrance and not metric_g):
                ml.append(as_ml)
            else:
                g.append(as_g)
    sizes: List[Quantity] = []
    for unit, values in (("ml", ml), ("g", g), ("count", count)):
        for v in values:
            qty = Quantity(_round_size(v), unit)
            if qty not in sizes:
                sizes.append(qty)
    pack = max(scan.packs) if scan.packs else 1
    measures = [s for s in sizes if s.unit in ("ml", "g")]
    counts = [s for s in sizes if s.unit == "count"]
    primary: Optional[Quantity] = None
    if scan.ambiguous:
        pass  # an unreadable fraction: no size rather than a wrong one
    elif len(measures) == 1:
        primary = measures[0]
        if len(counts) == 1 and pack == 1:
            # "Sheet Mask 25ml 10 sheets": the count is how many come in the box.
            pack = int(counts[0].value)
    elif not measures and len(counts) == 1:
        primary = counts[0]
    return ParsedSize(primary=primary, sizes=tuple(sizes), pack_count=max(pack, 1))


def parse_size(text: Any) -> ParsedSize:
    """Sizes stated in a title or variant label. See ParsedSize. Never raises."""
    folded = _fold(text)
    return _resolve(_scan_sizes(folded), folded)


def size_key(parsed: ParsedSize) -> Optional[Tuple[float, str, int]]:
    """(value, unit, pack_count) of the primary size, or None. "2 x 30ml" != "30ml"."""
    if parsed.primary is None:
        return None
    return (parsed.primary.value, parsed.primary.unit, parsed.pack_count)


# ---------------------------------------------------------------------------
# 3. Conflict tokens -- ONE table
# ---------------------------------------------------------------------------

TokenSpec = Union[str, Callable[[re.Match], str]]


@dataclass(frozen=True)
class ConflictRule:
    """One lexicon row. `token` is the normalized output (a callable for valued tokens such as
    spf50). `family` groups rows for veto reporting. Rows are tried IN ORDER on folded text and
    each match is blanked, so a longer phrase must precede its sub-phrase."""

    token: TokenSpec
    family: str
    pattern: str
    note: str


def _spf(m: re.Match) -> str:
    return f"spf{int(m.group(1))}{'+' if m.group(2) else ''}"


def _pa(m: re.Match) -> str:
    return "pa" + "+" * min(len(m.group(1)), 4)


def _version(m: re.Match) -> str:
    n = m.group(1).replace(",", ".")
    if "." in n:
        n = n.rstrip("0").rstrip(".")
    return f"v{n}"


def _edition(m: re.Match) -> str:
    return f"{m.group(1)}_edition"


def _gen(m: re.Match) -> str:
    return f"gen{int(m.group(1))}"


CONFLICT_LEXICON: Tuple[ConflictRule, ...] = (
    # -- fragrance concentration: EDT, EDP, parfum, extrait and mists are different products.
    ConflictRule("extrait", "concentration", r"\bextrait(?:\s+de\s+parfum)?\b", "Extrait de parfum, the strongest concentration."),
    ConflictRule("edp", "concentration", r"\beau\s+de\s+parfum\b|\bedp\b", "Eau de parfum."),
    ConflictRule("edt", "concentration", r"\beau\s+de\s+toilette\b|\bedt\b", "Eau de toilette."),
    ConflictRule("cologne", "concentration", r"\beau\s+de\s+cologne\b|\bedc\b|\bcologne\b", "Eau de cologne / cologne."),
    ConflictRule("eau_fraiche", "concentration", r"\beau\s+fraiche\b", "Eau fraiche (lighter than EDT)."),
    ConflictRule("parfum", "concentration", r"\bparfum\b|\bpure\s+perfume\b", "Parfum (after EDP/extrait are consumed)."),
    ConflictRule("hair_mist", "concentration", r"\bhair\s+(?:mist|perfume)\b", "Hair mist is not the fragrance it is named after."),
    ConflictRule("body_mist", "concentration", r"\b(?:body|fragrance|perfume|parfum)\s+mist\b", "Body / fragrance mist."),
    ConflictRule("body_spray", "concentration", r"\bbody\s+spray\b", "Body spray."),
    # -- sun protection: the rating is part of the formula (SPF30 vs SPF50, KR PA++++ vs US).
    ConflictRule(_spf, "spf", r"\bspf\s*(\d{1,3})\s*(\+?)", "SPF value; '50+' (KR/EU label class) is kept distinct from '50'."),
    ConflictRule("spf", "spf", r"\bspf\b", "SPF stated without a value."),
    ConflictRule(_pa, "pa", r"\bpa\s*(\+{1,4})", "PA rating (+ .. ++++), Asian UVA label."),
    # -- "+" editions. By the time this rule runs, sizes and "1+1" packs (parse_size), the brand's
    # own "+" (_blank_brand), SPF/PA ratings (the rules above, run first) and every CONJUNCTION "+"
    # (_CONJUNCTION_RE, symmetric between two words) are gone. So every "+" still standing is an
    # edition -- glued on one side (Duo+, B5+, Retinol 1%+, "B5 +Balm"), bracketed (Duo (+), [+]),
    # or before a size / rating / the end (Duo + SPF30 40ml) -- and no "+" is ever dropped
    # silently. The word "plus" is the same marker.
    ConflictRule(
        "plus", "version",
        r"\+|\bplus\b",
        "Every '+' left once sizes, SPF/PA, packs, the brand and conjunctions are read; the word 'plus'.",
    ),
    # -- version / reformulation markers.
    ConflictRule("new_formula", "version", r"\b(?:new|improved)\s+formula(?:tion)?\b", "Declared new formula."),
    ConflictRule("reformulated", "version", r"\breformulat(?:ed|ion)\b", "Declared reformulation."),
    ConflictRule("renewal", "version", r"\brenew(?:al|ed)\b", "K-beauty 'renewal'/'renewed': new packaging, often a formula change."),
    ConflictRule("new", "version", r"\bnew\b(?!\s+(?:york|zealand))", "A lone 'new' (after 'new formula'); one-sided it vetoes."),
    ConflictRule(_edition, "edition", r"\b(holiday|anniversary|collector)s?\s+edition\b", "Holiday / anniversary / collector edition, each its own token."),
    ConflictRule("limited_edition", "edition", r"\blimited(?:\s+edition)?\b", "Limited edition."),
    ConflictRule("special_edition", "edition", r"\bspecial\s+edition\b", "Special edition."),
    ConflictRule(_version, "version", r"\b(?:v|ver\.?|version)\s*(\d+(?:[.,]\d+)?)(?![\w.])", "v2 / ver. 2 / version 2.0."),
    ConflictRule(_version, "version", r"(?<![\w.])([2-9][.,]0)(?![\w.%])", "Bare 2.0 .. 9.0 (sizes are consumed first, so '2.0 fl oz' never reaches here)."),
    ConflictRule(_gen, "version", r"\b(\d{1,2})(?:st|nd|rd|th)\s+gen(?:eration)?\b", "6th generation."),
    ConflictRule(_gen, "version", r"\bgen(?:eration)?\s*(\d{1,2})\b", "gen 6."),
    ConflictRule("rx", "version", r"\brx\b", "Rx line (e.g. prescription-strength edition)."),
    ConflictRule("advanced", "version", r"\badvanced\b", "'Advanced' edition of a line."),
    ConflictRule("intense", "version", r"\bintense\b", "'Intense' edition (fragrance and skincare)."),
    ConflictRule("ex", "version", r"(?<![\w-])ex(?![\w-])", "K-beauty 'EX' re-release (Laneige Water Sleeping Mask EX)."),
    # -- regional formula: KR/JP/EU vs US formulations differ (sunscreens, actives).
    ConflictRule("market_us", "market", r"\b(?:us|usa|u\.s\.|american)\s+(?:version|formula(?:tion)?|ver)\b", "US version."),
    ConflictRule("market_kr", "market", r"\b(?:kr|korean?|k)\s+(?:version|formula(?:tion)?|ver)\b", "Korean version."),
    ConflictRule("market_jp", "market", r"\b(?:jp|japan(?:ese)?)\s+(?:version|formula(?:tion)?|ver)\b", "Japanese version."),
    ConflictRule("market_eu", "market", r"\b(?:eu|european?|uk)\s+(?:version|formula(?:tion)?|ver)\b", "EU / UK version."),
    ConflictRule("market_global", "market", r"\b(?:global|international|intl)\s+(?:version|formula(?:tion)?|ver)\b", "Global version."),
    # -- flags: never the same buyable item as the full-size single unit.
    ConflictRule("refill", "flag", r"\brefills?\b", "Refill ('refillable' is the full product and stays in the core)."),
    ConflictRule("tester", "flag", r"\btesters?\b", "Tester."),
    ConflictRule("unboxed", "flag", r"\bunboxed\b|\b(?:no|without)\s+box\b|\b(?:damaged|dented)\s+box\b|\bbox\s+damaged\b", "Unboxed / damaged box."),
    ConflictRule("sample", "flag", r"\bdeluxe\s+samples?\b|\bsamples?(?:\s+size)?\b|\btrial\s+size\b|\bvials?\b", "Sample / trial / vial."),
    ConflictRule("mini", "flag", r"\bdeluxe\s+mini\b|\bminis?\b|\bminiature\b|\btravel[\s-]*size[d]?\b|\btravel\b", "Mini / travel size."),
    ConflictRule("value_size", "flag", r"\bvalue[\s-]*size[d]?\b|\bjumbo(?:\s+size)?\b|\bsuper[\s-]*size[d]?\b|\b(?:big|mega|family)\s+size\b", "Value / jumbo size."),
    ConflictRule("multipack", "flag", r"\bmulti[\s-]*pack\b|\bvalue\s+pack\b", "Multi-pack (a stated pack count > 1 also raises this)."),
    ConflictRule("set", "flag", r"\b(?:gift|value|discovery|starter|trial)\s+sets?\b|\bsets?\b|\bkits?\b|\bbundles?\b|\bcollection\b|\bcoffret\b", "Set / kit / bundle / collection."),
    ConflictRule("duo", "flag", r"\bduos?\b", "Duo."),
    ConflictRule("trio", "flag", r"\btrios?\b", "Trio."),
    ConflictRule("gift", "flag", r"\bfree\s*gifts?\b|\bgwp\b|\bgift\s+with\s+purchase\b|\bgifts?\b", "Gift / GWP."),
)
# Format words: compared only as the product's FORMAT, extracted AFTER shade detection (so a
# shade called "Light Beige" is a shade, not a formula). "gel cream" is one format.
FORMAT_LEXICON: Tuple[ConflictRule, ...] = (
    ConflictRule("gel_cream", "format", r"\bgel[\s-]*cream\b|\bcream[\s-]*gel\b", "Gel-cream."),
    ConflictRule("gel", "format", r"\bgel\b", "Gel format."),
    ConflictRule("cream", "format", r"\bcream\b|\bcreme\b", "Cream format ('creme' after accent fold)."),
    ConflictRule("rich", "format", r"\brich\b", "Rich texture edition."),
    ConflictRule("light", "format", r"\blight\b|\blite\b", "Light texture edition."),
)
_COMPILED: Tuple[Tuple[ConflictRule, re.Pattern], ...] = tuple((r, re.compile(r.pattern)) for r in CONFLICT_LEXICON)
# Ratings are read FIRST (their "+" is part of the rating: SPF50+, PA++++), then conjunctions are
# decided (_CONJUNCTION_RE), then every other rule in table order.
_RATING_FAMILIES = frozenset({"spf", "pa"})
_COMPILED_RATINGS = tuple((r, c) for r, c in _COMPILED if r.family in _RATING_FAMILIES)
_COMPILED_AFTER_RATINGS = tuple((r, c) for r, c in _COMPILED if r.family not in _RATING_FAMILIES)
_COMPILED_FORMAT: Tuple[Tuple[ConflictRule, re.Pattern], ...] = tuple((r, re.compile(r.pattern)) for r in FORMAT_LEXICON)
_FAMILY_BY_STATIC_TOKEN: Dict[str, str] = {
    r.token: r.family for r in CONFLICT_LEXICON + FORMAT_LEXICON if isinstance(r.token, str)
}


def conflict_family(token: str) -> str:
    """Lexicon family of a conflict token (valued tokens resolve by prefix)."""
    if token in _FAMILY_BY_STATIC_TOKEN:
        return _FAMILY_BY_STATIC_TOKEN[token]
    if token.startswith("format_order:"):
        return "format"
    if token.endswith("_edition"):
        return "edition"
    if token.startswith("spf"):
        return "spf"
    if token.startswith("pa+"):
        return "pa"
    if token.startswith(("v", "gen")):
        return "version"
    return "other"


# What a FORMAT word leaves in the core: its POSITION survives, so "Cream Skin" (Laneige) and
# "Skin Cream" keep different ordered cores ("fmt skin" vs "skin fmt") although both move "cream"
# to the conflict tokens. Not a lexicon token, so it never reads as a conflict on a re-pass.
FORMAT_PLACEHOLDER = "fmt"

# What a CONJUNCTION leaves in the core (review rounds 3-4 of be#2636): a "+" between two words,
# "&" (folded to "and") and the word "and" between two words are ONE positional core word. The
# `plus` rule already took every "+" that is glued, bracketed or not followed by a word, so what
# remains joins two words. One word for all three spellings, so "Rice + Probiotics" = "Rice &
# Probiotics" = "Rice and Probiotics", "AHA + BHA" = "AHA & BHA", "Vitamin C+E" = "Vitamin C & E".
# Decided in _one_pass right after SPF/PA and BEFORE conflict/format words are removed, and kept
# even if a neighbour is removed later: "Toner+Refill" -> "toner join" != "Toner Refill",
# "Cream+Serum" -> "fmt join serum" = "Cream & Serum".
# Never silently dropped: "Cicaplast B5 + Multi-Purpose Balm" is not "B5 Multi-Purpose Balm",
# "Rice + Probiotics" is not "Rice Probiotics" (the safe direction). ACCEPTED COST: "Hair & Body
# Wash" no longer meets "Hair Body Wash". A brand's own "&" (Dolce & Gabbana, Sand & Sky) is gone
# with the brand before this runs. Not a conflict token (a conjunction is not an edition); one
# alphanumeric word that is no lexicon token, so it is stable on a re-pass.
CONJUNCTION_PLACEHOLDER = "join"
# A "+" is a conjunction ONLY when it is symmetric between two words: spaced on both sides
# ("Rice + Probiotics", "Niacinamide 10% + Zinc") or glued on both ("Vitamin C+E", "Duo+M",
# "Cream+Serum"). One-sided ("Duo+ 40ml", "B5+ Multi-Purpose Balm", "Rice +Probiotics") is the
# `plus` edition, on T0c (the lexicon rule) and T0b (exact_title_key) alike.
_CONJUNCTION_PLUS = r"\s+\+\s+|\+"
_CONJUNCTION_PLUS_RE = re.compile(rf"(?<=[^\W_]|%)(?:{_CONJUNCTION_PLUS})(?=[^\W_])")
# T0c decides "&" (folded to "and") and the word "and" between two words at the same point, so
# the three spellings stay one conjunction whatever is removed around them later.
_RATING_PLUS_RE = re.compile(r"\bspf\s*\d{1,3}\s*\+|\bpa\s*\+{1,4}")
_CONJUNCTION_RE = re.compile(rf"(?<=[^\W_]|%)(?:{_CONJUNCTION_PLUS}|\s+and\s+)(?=[^\W_])")


def _extract(work: List[str], rules: Sequence[Tuple[ConflictRule, re.Pattern]], *,
             placeholder: str = "") -> List[str]:
    found: List[Tuple[int, str]] = []
    for rule, pattern in rules:
        for m in list(pattern.finditer("".join(work))):
            tok = rule.token(m) if callable(rule.token) else rule.token
            found.append((m.start(), tok))
            for i in range(m.start(), m.end()):
                work[i] = " "
            if placeholder and m.end() - m.start() >= len(placeholder):
                work[m.start():m.start() + len(placeholder)] = list(placeholder)
    tokens = [t for _pos, t in found]
    if placeholder:
        ordered = [t for _pos, t in sorted(found)]
        if len(set(ordered)) > 1:
            # "Gel to Cream" vs "Cream to Gel": same words, different products.
            tokens.append("format_order:" + ">".join(ordered))
    return tokens


def conflict_tokens(text: Any) -> FrozenSet[str]:
    """Normalized tokens that must AGREE for two listings to be one buyable item: fragrance
    concentration, SPF/PA, version/reformulation, regional formula, format (gel/cream/rich/
    light), and flags (refill, tester, unboxed, sample, mini, value_size, set, duo, trio, gift,
    multipack). A stated pack count > 1 adds "multipack". See CONFLICT_LEXICON / FORMAT_LEXICON."""
    return title_core(None, text).conflicts


# ---------------------------------------------------------------------------
# 4. Title core
# ---------------------------------------------------------------------------

# Pure function words. Not skin/face/facial (gateway STOP_TOKENS does): they distinguish lines.
# NOT "x": NARS "Orgasm X" is a different blush from "Orgasm". Only a LEADING "x" left by brand
# removal ("COSRX x Olive Young ...", a collab) is dropped -- see _clean.
# NOT "and": between two words it is the conjunction word (CONJUNCTION_PLACEHOLDER) -- see _clean.
_STOP_TOKENS = frozenset({"the", "of", "for", "with", "a", "an", "by"})
# Promo / retail-descriptor words that never name a product. NOT here: free (fragrance free),
# double (Etude Double Lasting), plus (a conflict token), refill/set/kit/duo/gift/value/jumbo
# (conflict tokens), original (a scent/variant name).
# Not here (conflict tokens instead, review of be#2636): new, renewal/renewed, limited edition,
# special edition -- each can mark a different formula or pack. A lone "edition"/"special" stays
# in the core.
_PROMO_TOKENS = frozenset({
    "exclusive", "online", "bestseller", "bestsellers", "sale", "hot", "deal", "authentic",
    "genuine", "official",
})
_PROMO_PHRASES_RE = re.compile(
    r"\bk[\s-]?beauty\b|\bkorean\s+(?:skin\s*care|cosmetics?|beauty|makeup)\b|"
    r"\b100\s*%\s*(?:authentic|genuine|original)\b|\bfree\s+shipping\b|"
    r"\bbest[\s-]*sell(?:er|ers|ing)\b|#\s?1\s+(?:best[\s-]*sell(?:er|ing)|rated|selling)\b"
)
# Words that make a trailing " - ..." segment a product description, not a shade name.
_PRODUCT_NOUNS = frozenset({
    "serum", "essence", "toner", "cream", "moisturizer", "moisturiser", "cleanser", "cleansing",
    "mask", "masks", "sunscreen", "sunblock", "sun", "lotion", "ampoule", "oil", "balm", "foam",
    "gel", "mist", "spray", "pad", "pads", "patch", "patches", "foundation", "cushion", "primer",
    "powder", "concealer", "lipstick", "gloss", "mascara", "eyeliner", "liner", "shampoo",
    "conditioner", "treatment", "emulsion", "skin", "face", "facial", "hair", "body", "stick",
    "care", "skincare", "makeup", "hydrating", "moisturizing", "moisturising", "soothing",
    "brightening", "korean", "perfume", "fragrance", "eye", "lip", "lips", "acne", "pimple",
    "spot", "peel", "exfoliator", "scrub", "wash", "water", "milk", "tonic", "soap", "deodorant",
})
# Dropped from the core of a title that names a fragrance concentration only.
_FRAGRANCE_DISPENSER_TOKENS = frozenset({"spray", "natural", "vaporisateur", "vapo", "vaporizer", "atomizer"})
_SEPARATOR_RE = re.compile(r"\s+[-–—|]\s+|\s*\|\s*")
_TRAILING_BRACKET_RE = re.compile(r"[(\[]([^()\[\]]*)[)\]]\s*$")
_SHADE_LABEL_RE = re.compile(r"\b(?:shade|colou?r|tone)\s*:\s*([^|()\[\],]+)")
_SHADE_CODE_RES: Tuple[re.Pattern, ...] = (
    # "#21", "#01 Rosy Glow" -- a code plus at most three name words up to the end of the title.
    re.compile(r"#\s?([0-9a-z]{1,4})((?:\s+[^\W\d_]+){0,3})\s*$"),
    re.compile(r"#\s?([0-9a-z]{1,4})\b"),
    # Foundation codes: 2N1, 21N, 1.5W, 3C2 -- with up to three name words when at the end.
    re.compile(r"(?<![\w.])(\d{1,2}(?:\.\d)?[nwc]\d{0,2})(?![\w.])((?:\s+[^\W\d_]+){0,3})\s*$"),
    re.compile(r"(?<![\w.])(\d{1,2}(?:\.\d)?[nwc]\d{0,2})(?![\w.])"),
)
_TOKEN_RE = re.compile(r"\d+\.\d+|[^\W_]+")


def _tokens(text: str) -> List[str]:
    """Words of folded text: apostrophes join ("kiehl's" -> kiehls), hyphens and other
    punctuation split, decimals stay one token, integers lose leading zeros ("01" -> "1")."""
    out = []
    for tok in _TOKEN_RE.findall(text.replace("'", "")):
        out.append(str(int(tok)) if tok.isdecimal() else tok)
    return out


def _clean(text: str) -> List[str]:
    """Core words of a stretch of text whose brand _blank_brand has already removed and whose
    conjunctions _one_pass has already turned into CONJUNCTION_PLACEHOLDER. That word is KEPT even
    when a neighbour was later removed ("Toner+Refill" -> "toner join": not "Toner Refill"); only
    a repeat collapses. An "and" still standing was at an edge ("Rice and", "And Rice"): dropped."""
    text = _PROMO_PHRASES_RE.sub(" ", text)
    out: List[str] = []
    for t in _tokens(text):
        if t in _STOP_TOKENS or t in _PROMO_TOKENS or t == "and":
            continue
        if t == CONJUNCTION_PLACEHOLDER and out and out[-1] == CONJUNCTION_PLACEHOLDER:
            continue
        out.append(t)
    return out


@lru_cache(maxsize=65536)
def _brand_text_re(forms: FrozenSet[str]) -> Optional[re.Pattern]:
    """The brand as written in a title, punctuation-insensitive, with a glued "+" ("Dr.Jart+"),
    so the brand's own "+" never reads as a "+" edition, and a collab "x" right after it."""
    alts = sorted((f for f in forms if len(f) >= 2), key=len, reverse=True)
    if not alts:
        return None
    body = "|".join(r"[\W_]*".join(re.escape(c) for c in f) for f in alts)
    # The collab "x" needs a WORD after it ("COSRX x Olive Young"); "NARS X 4.8g" keeps its x.
    return re.compile(rf"(?<![^\W_])(?:{body})\+?(?![^\W_])(?:\s+x(?=\s+[^\W\d_]))?")


def _blank_brand(text: str, forms: FrozenSet[str]) -> str:
    pattern = _brand_text_re(forms)
    return pattern.sub(lambda m: " " * len(m.group(0)), text) if pattern else text


@dataclass(frozen=True)
class TitleCore:
    """A listing title split into identity (core) and variant/edition parts.

    core:      ordered core string (brand, size, pack, promo, shade and conflict words removed).
    tokens:    the core's token set; T0c compares these for equality.
    conflicts: conflict tokens (must agree; see CONFLICT_LEXICON).
    shade:     detected shade text ("" when none was detected; undetected shade words stay in
               the core, which can only prevent a match, never cause one).
    shade_tokens: the shade's token set.
    size:      parse_size of the title.
    """

    core: str
    tokens: FrozenSet[str]
    conflicts: FrozenSet[str]
    shade: str
    shade_tokens: FrozenSet[str]
    size: ParsedSize


def _shade_like(seg: str, rest: str) -> Optional[List[str]]:
    cand = _clean(seg)
    main = _clean(rest)
    if 1 <= len(cand) <= 3 and len(main) >= 2 and not (set(cand) & _PRODUCT_NOUNS):
        return cand
    return None


def _trailing_shade(s: str) -> Tuple[str, List[str]]:
    """A trailing "(X)" / "[X]" or " - X" / " | X" segment of 1..3 words that names no product
    type, after a main part of >= 2 words, is the shade. An empty bracket (its size or SPF was
    already taken out) is dropped and the separator form is tried on what precedes it."""
    bm = _TRAILING_BRACKET_RE.search(s)
    if bm:
        cand = _shade_like(bm.group(1), s[: bm.start()])
        if cand:
            return s[: bm.start()], cand
        if not _clean(bm.group(1)):
            s = s[: bm.start()]
        else:
            return s, []
    parts = _SEPARATOR_RE.split(s)
    if len(parts) >= 2:
        rest = " ".join(parts[:-1])
        cand = _shade_like(parts[-1], rest)
        if cand:
            return rest, cand
    return s, []


def _one_pass(text: str, forms: FrozenSet[str]) -> Tuple[List[str], List[str], List[str], _SizeScan]:
    text = _blank_brand(text, forms)
    scan = _scan_sizes(text)
    work = list(text)
    for start, end in scan.spans:
        for i in range(start, end):
            work[i] = " "
    conflicts = _extract(work, _COMPILED_RATINGS)
    # Conjunctions are decided HERE: after sizes and SPF/PA (their "+" is theirs), BEFORE any
    # conflict or format word is removed -- else "Duo+M" / "Cream+Serum" / "Toner+Refill" lose a
    # neighbour ("duo", "cream", "refill") first and the "+" between them is no longer between
    # two words.
    work = list(_CONJUNCTION_RE.sub(f" {CONJUNCTION_PLACEHOLDER} ", "".join(work)))
    conflicts.extend(_extract(work, _COMPILED_AFTER_RATINGS))
    shade: List[str] = []
    s = "".join(work)
    m = _SHADE_LABEL_RE.search(s)
    if m:
        shade.extend(_clean(m.group(1)))
        s = s[: m.start()] + " " * (m.end() - m.start()) + s[m.end():]
    if not shade:
        s, shade = _trailing_shade(s)
    for pattern in _SHADE_CODE_RES:
        m = pattern.search(s)
        if m:
            shade.extend(_clean(" ".join(g for g in m.groups() if g)))
            s = s[: m.start()] + " " * (m.end() - m.start()) + s[m.end():]
    work = list(s)
    conflicts.extend(_extract(work, _COMPILED_FORMAT, placeholder=FORMAT_PLACEHOLDER))
    core = _clean("".join(work))
    return core, conflicts, shade, scan


def title_core(brand: Any, title: Any) -> TitleCore:
    """Split a listing title into its identity core and its variant/edition parts.

    Order: fold (marks stripped before NFKC) -> the brand out, as a phrase anywhere, any measured
    spelling, with its own glued "+" and a following collab "x" -> sizes and packs out ->
    conflict tokens out -> shade out (label, trailing " - X" / "(X)" segment of <= 3
    non-product words, codes like #21 / 2N1) -> format words out (each leaves
    FORMAT_PLACEHOLDER at its position) -> promo phrases, stop words and promo words out.
    Repeated on its own output until stable, so title_core is idempotent.
    """
    forms = _brand_forms(brand)
    folded = _fold(title)
    core, conflicts, shade, scan = _one_pass(folded, forms)
    size = _resolve(scan, folded)
    all_conflicts = set(conflicts)
    for _ in range(4):
        again, more_conflicts, more_shade, _scan = _one_pass(" ".join(core), forms)
        all_conflicts.update(more_conflicts)
        shade.extend(t for t in more_shade if t not in shade)
        if again == core:
            break
        core = again
    if size.pack_count > 1:
        all_conflicts.add("multipack")
    if any(conflict_family(t) == "concentration" for t in all_conflicts):
        # "Eau de Toilette Spray" / "EDP Natural Spray" / "Vaporisateur": the dispenser of a
        # fragrance is not its identity (the brand store rarely says "spray").
        core = [t for t in core if t not in _FRAGRANCE_DISPENSER_TOKENS]
    shade_dedup: List[str] = []
    for t in shade:
        if t not in shade_dedup:
            shade_dedup.append(t)
    return TitleCore(
        core=" ".join(core),
        tokens=frozenset(core),
        conflicts=frozenset(all_conflicts),
        shade=" ".join(shade_dedup),
        shade_tokens=frozenset(shade_dedup),
        size=size,
    )


# ---------------------------------------------------------------------------
# 5. GTIN
# ---------------------------------------------------------------------------

# GS1 prefixes that do not identify one trade item across companies, at the GTIN-13 level
# (GS1 General Specifications 2.1/2.4 "GS1 Prefixes" table and "Restricted Circulation Numbers";
# GS1 US coupon guideline for the UPC 5-prefix):
#   020-029  restricted circulation, MO-defined (variable-measure / in-store codes)
#   040-049  restricted circulation within a company (merchants' internal SKUs)
#   050-059  coupons (UPC number system 5)
#   200-299  restricted circulation, MO-defined (variable measure, in-store)
#   977      ISSN (serials); 978-979 ISBN/ISMN (books, sheet music)
#   980      refund receipts; 981-984 common-currency coupons; 990-999 coupons
# A GTIN-8 (zero-padded "000000" + 8) starting 0 or 2 is an RCN-8 (restricted). A GTIN-14 with
# indicator digit 9 is a variable-measure trade item (no fixed content). Indicator 1-8 (a case of
# a consumer item) is NOT restricted, but it is not a consumer unit either; left to the caller.
GS1_RESTRICTED_PREFIXES: Tuple[Tuple[str, str, str], ...] = (
    ("020", "029", "restricted_circulation"),
    ("040", "049", "restricted_circulation_company"),
    ("050", "059", "coupon"),
    ("200", "299", "restricted_circulation"),
    ("977", "977", "issn"),
    ("978", "979", "isbn"),
    ("980", "980", "refund_receipt"),
    ("981", "984", "coupon"),
    ("990", "999", "coupon"),
)


def gtin_canonical(raw: Any) -> Optional[str]:
    """14-digit GTIN when `raw` is a check-digit-valid GTIN-8/12/13/14, else None.
    Delegates to catalog_identity.validated_source_gtin (the global Tier-0 rule); integers are
    accepted as their decimal spelling."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        raw = str(raw)
    return validated_source_gtin(raw) if isinstance(raw, str) else None


def gtin_restriction(gtin14: Optional[str]) -> Optional[str]:
    """Why a canonical GTIN-14 cannot identify a product across merchants, or None."""
    if not gtin14 or not re.fullmatch(r"\d{14}", gtin14):
        return "malformed"
    if gtin14[0] == "9":
        return "variable_measure"
    if gtin14.startswith("000000") and gtin14[6] in "02":
        return "rcn8_restricted"
    p3 = gtin14[1:4]
    for lo, hi, reason in GS1_RESTRICTED_PREFIXES:
        if lo <= p3 <= hi:
            return reason
    return None


def gtin_is_restricted(gtin14: Optional[str]) -> bool:
    return gtin_restriction(gtin14) is not None


# ---------------------------------------------------------------------------
# 6. Tier keys and vetoes
# ---------------------------------------------------------------------------

def _field(listing: Any, name: str) -> Any:
    if isinstance(listing, Mapping):
        return listing.get(name)
    return getattr(listing, name, None)


def exact_title_key(brand: Any, title: Any) -> Optional[Tuple[str, str]]:
    """T0b: (canonical_brand, catalog_identity.normalize_title) -- exact title, digits kept.

    The title is read through _fold first (trademark signs and a textual "(TM)" / "(R)" gone --
    normalize_title's NFKD would spell "™" "TM" and glue it on --, accents folded, "&" -> "and").
    Then no "+" is dropped (normalize_title would drop it):
      - the brand's own glued "+" goes ("Dr.Jart+ Cicapair" = "Dr. Jart Cicapair");
      - a "+" symmetric between two words (spaced both sides or glued both sides) reads "and",
        like "&" ("Rice + Probiotics" = "Rice & Probiotics" = "Rice and Probiotics"; "B5 +
        Multi-Purpose Balm" != "B5 Multi-Purpose Balm");
      - any other "+" (an edition: "Duo+ 40ml", "B5+ Multi-Purpose Balm", "Duo (+)", "SPF50+")
        reads "plus".
    T0b does not blank sizes or SPF/PA first, so "Duo + SPF30" reads "duo and spf30" here while
    title_core calls that "+" an edition; the signature veto (title_core conflicts) is applied to
    every T0b group, so the two tiers still decide alike.
    normalize_title itself is left alone (it mints content_key); this only changes the matching key."""
    b = canonical_brand(brand)
    t = None
    if title is not None:
        text = _fold(title)
        pattern = _brand_text_re(_brand_forms(brand))
        if pattern is not None:
            text = pattern.sub(lambda m: m.group(0).replace("+", " "), text)
        # Ratings first, as on T0c: "SPF50+PA++++" keeps its 50+ (not "spf50 and pa").
        text = _RATING_PLUS_RE.sub(lambda m: m.group(0).replace("+", " plus "), text)
        text = _CONJUNCTION_PLUS_RE.sub(" and ", text).replace("+", " plus ")
        t = normalize_title(text)
    return (b, t) if b and t else None


def match_key_t0c(listing: Any) -> Optional[Tuple[str, Tuple[str, ...], Tuple[float, str, int]]]:
    """T0c deterministic key: (canonical_brand, ORDERED title-core tokens, (size, unit, pack_count)).

    Ordered, not a token set (review of be#2636): "Cream to Powder" vs "Powder to Cream", "Oil to
    Milk" vs "Milk to Oil", Laneige "Cream Skin" vs "Skin Cream" are different products. The cost
    is recall where two stores order the same words differently ("Snail 96 Mucin Essence" vs
    "96 Snail Mucin Essence"); the census reports that as the T0c_token_set tier.

    None when the brand or the core is empty, or no single primary size parses. The key does
    NOT carry shade or conflict tokens: a caller grouping by it MUST then apply veto_reasons (or
    partition by (conflicts, shade_tokens)) -- that is the "hard vetoes on every tier" step.
    `listing` is a mapping or object with `brand` and `title`.
    """
    brand = _field(listing, "brand")
    cb = canonical_brand(brand)
    if not cb:
        return None
    tc = title_core(brand, _field(listing, "title"))
    sk = size_key(tc.size)
    if not tc.tokens or sk is None:
        return None
    return (cb, tuple(tc.core.split()), sk)


def veto_reasons(a: TitleCore, b: TitleCore) -> List[str]:
    """Why two title cores are NOT one buyable item, empty when nothing vetoes. One-sided
    tokens are disagreements too ("EDP" vs nothing is not known to agree), and two titles with
    no parseable size are "size_unknown" (phase 1 needs a proven equal size)."""
    reasons = [f"conflict:{t}" for t in sorted(a.conflicts ^ b.conflicts)]
    if a.shade_tokens != b.shade_tokens:
        reasons.append("shade")
    sa, sb = size_key(a.size), size_key(b.size)
    if sa != sb:
        reasons.append("size")
    elif sa is None:
        reasons.append("size_unknown")  # phase 1 merges same-size only: no size, no proof
    return reasons
