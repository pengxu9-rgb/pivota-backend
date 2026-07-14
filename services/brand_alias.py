"""Brand-alias normalization for the merchant-audit engine (Phase B).

The audit matched the merchant brand against third-party answer text / cited
source titles with a LITERAL compare (`brand_lower in text`). A merchant
recorded as "BB Lab Global" therefore never matched sources that say just
"BB Lab", so visibility + attribution under-counted and the verdict came back
a wrong INVISIBLE.

This module derives the set of aliases a real brand mention could take —
the suffix-stripped core, a de-spaced form, the registrable name from the
storefront domain, and any product vendors — and matches ANY of them.

Design invariant: it only ADDS matches over the literal compare, never
removes one. Callers keep their existing compare and OR-in `text_mentions_brand`,
so recall rises (the under-count bug is fixed) without changing a match the
engine already made.

Safety choices (to avoid false positives that would over-credit a brand):
  - Strip only *trailing* non-identity tokens (Global/Official/Inc/Shop…),
    never leading — so "The Ordinary" never collapses to the common word
    "ordinary".
  - Add a stripped core only when it keeps >=2 tokens — so "BB Lab Global"
    yields "bb lab" but "Magnesium Co" does NOT yield the generic "magnesium".
  - Every alias is >=3 chars and matched on non-alphanumeric boundaries, so
    "bb lab" matches "the bb lab serum" and "bblab" matches "bblab.com", but
    neither matches inside an unrelated word.
"""
from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from typing import List, Optional, Pattern, Tuple
from urllib.parse import urlparse

# Tokens that don't carry brand identity. Legal forms + storefront/marketing
# words. Deliberately EXCLUDES identity-bearing words (lab/labs, beauty,
# care, …) — and since we always keep the full form too, the worst case of an
# over-strip is a gated extra alias, never a dropped real one.
_LEGAL_SUFFIX = frozenset({
    "inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation",
    "co", "company", "gmbh", "srl", "sa", "ag", "plc", "bv", "oy", "ab",
})
_MARKETING_TOKENS = frozenset({
    "global", "official", "store", "stores", "shop", "shops", "online",
    "worldwide", "international", "intl", "brand", "brands",
})
_STRIPPABLE = _LEGAL_SUFFIX | _MARKETING_TOKENS

# Second-level public labels (mybrand.co.uk, foo.com.au) — when the host's
# penultimate label is one of these, the registrable name is one further left.
_PUBLIC_SECOND_LEVEL = frozenset({
    "co", "com", "org", "net", "gov", "edu", "ac",
})

_MIN_ALIAS_LEN = 3


# Precomposed Latin letters that carry a diacritic (Latin-1 Supplement through
# Latin Extended-B; Latin Extended Additional / Vietnamese), plus the combining
# marks themselves (text that arrives already decomposed). A string containing
# NONE of these cannot gain a single ASCII character from a canonical fold, so
# the fold is skipped — which keeps it off Hangul/CJK copy, where NFD explodes
# Korean syllables into jamo (~4x slower on the hot path) and can never produce
# an ASCII alias match anyway.
_FOLDABLE_LATIN_RE = re.compile(r"[À-ɏ̀-ͯḀ-ỿ]")


def _fold_diacritics(s: str, *, compat: bool = False) -> str:
    """Fold accented letters to their ASCII base letter: decompose, then drop
    the combining marks. 'kérastase' → 'kerastase'.

    `compat` selects the decomposition, and the two callers genuinely need
    different ones:

      - _normalize (the ALIAS side) passes compat=True for NFKD, because it also
        has to normalize a fullwidth trademark spelling ('Brand（ｔｍ）') into the
        ASCII '(tm)' that it knows how to strip.
      - text_mentions_brand (the TEXT side) must NOT use NFKD. Besides the 246
        accented letters we want, NFKD maps ~1,100 NON-alphanumeric codepoints
        INTO [a-z0-9] — 'º' '¹' '②' 'ﬁ' '½' — which manufactures brand mentions
        out of unrelated copy: alias 'no7' would match the Spanish "serum nº7",
        and 'the creme' (the stripped core of "The Creme Shop") would match "the
        crème brûlée". Those false positives cut BOTH ways — they over-credit
        own-brand visibility, and where a merchant's own aliases are used to
        filter a rival list they would silently DROP a real competitor. NFD
        folds the accents and leaves the symbols alone.

    ASCII input is returned untouched (both decompositions are the identity on
    it), which is the hot path: answer copy is nearly always plain ASCII.

    Does NOT lowercase. NFKD maps compatibility symbols to UPPERCASE ('™' →
    'TM'), and both callers depend on that staying uppercase so the
    lowercase-only alnum class / alias patterns scrub it rather than reading it
    as a real token.
    """
    if s.isascii():
        return s
    if not compat and not _FOLDABLE_LATIN_RE.search(s):
        return s
    decomposed = unicodedata.normalize("NFKD" if compat else "NFD", s)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _normalize(text: Optional[str]) -> str:
    """Lowercase, fold diacritics to their ASCII base letter (NFKD, combining
    marks dropped), strip ®/™/(r)/(tm), reduce to alnum tokens joined by single
    spaces. 'Crème de la Mer®' → 'creme de la mer', 'Kérastase' → 'kerastase'.

    The fold must happen BEFORE the non-alnum collapse: without it an accented
    letter becomes a token BREAK, splitting the brand into junk tokens
    ('Kérastase' → 'k rastase' / de-spaced 'krastase') that can never match the
    ASCII text these aliases are compared against — answer copy, source titles,
    and registrable host labels ('kerastase-usa'). That silent miss is what let
    a rival's own storefront through _flag_competitor_by_name and into 'Get
    cited on' outreach (prod run 83e8fcb4, competitor 'Kérastase' vs
    kerastase-usa.com)."""
    t = (text or "").lower()
    # Fold BEFORE the mark strip, so a fullwidth/compatibility spelling of the
    # marks normalizes into the ASCII forms the strip below already knows:
    # 'Brand（ｔｍ）' → '(tm)' → stripped. (Stripping first would leave a bogus
    # 'tm' token behind.) The literal '™' needs no such care either way — NFKD
    # maps it to UPPERCASE 'TM', which the lowercase-only class below scrubs.
    t = _fold_diacritics(t, compat=True)
    for mark in ("®", "™"):
        t = t.replace(mark, " ")
    t = re.sub(r"\(\s*(?:r|tm)\s*\)", " ", t)
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _registrable_name_from_host(host: Optional[str]) -> str:
    """'https://www.bblab.shop/x' → 'bblab'; 'shop.mybrand.com' → 'mybrand';
    'mybrand.co.uk' → 'mybrand'. Best-effort without a public-suffix list."""
    h = (host or "").strip().lower()
    if not h:
        return ""
    if "://" in h or "/" in h:
        h = urlparse(h if "://" in h else f"https://{h}").netloc or h
    h = h.split("/")[0].split(":")[0]
    if h.startswith("www."):
        h = h[4:]
    parts = [p for p in h.split(".") if p]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    # registrable label = the one just left of the TLD, skipping a known
    # second-level public label (co.uk, com.au).
    idx = len(parts) - 2
    if parts[idx] in _PUBLIC_SECOND_LEVEL and idx - 1 >= 0:
        idx -= 1
    return parts[idx]


@lru_cache(maxsize=4096)
def derive_brand_aliases(
    brand: Optional[str],
    host: Optional[str] = None,
    vendors: Optional[Tuple[str, ...]] = None,
) -> Tuple[str, ...]:
    """Return the deduped, order-stable alias set for a brand.

    Includes the normalized brand, its trailing-suffix-stripped core (only if
    >=2 tokens remain), de-spaced forms, the registrable host name, and any
    product vendors. Every alias is >=3 chars. `vendors` must be a tuple so
    the result is cacheable.
    """
    aliases: List[str] = []
    seen = set()

    def _add(candidate: str) -> None:
        candidate = candidate.strip()
        if len(candidate) >= _MIN_ALIAS_LEN and candidate not in seen:
            seen.add(candidate)
            aliases.append(candidate)

    def _add_brand_forms(value: str) -> None:
        norm = _normalize(value)
        if not norm:
            return
        _add(norm)
        _add(norm.replace(" ", ""))
        tokens = norm.split()
        if len(tokens) > 1:
            core = list(tokens)
            # strip TRAILING non-identity tokens only (Global, Official, Inc…)
            while len(core) > 1 and core[-1] in _STRIPPABLE:
                core.pop()
            # only keep a multi-token core — single-token cores ("magnesium")
            # are too generic to match safely.
            if len(core) >= 2 and len(core) != len(tokens):
                core_str = " ".join(core)
                _add(core_str)
                _add(core_str.replace(" ", ""))

    _add_brand_forms(brand or "")
    for vendor in vendors or ():
        _add_brand_forms(vendor)

    reg = _registrable_name_from_host(host)
    if reg:
        _add(_normalize(reg).replace(" ", ""))

    return tuple(aliases)


@lru_cache(maxsize=8192)
def _alias_pattern(alias: str) -> Pattern[str]:
    """Compile an alias to a boundary-anchored regex. Internal spaces become a
    flexible separator so 'bb lab' matches 'bb lab', 'bb-lab', and 'bblab';
    the non-alnum lookarounds stop it matching inside a larger word."""
    parts = [re.escape(p) for p in alias.split(" ") if p]
    body = r"[\s\-_]*".join(parts)
    return re.compile(r"(?<![a-z0-9])" + body + r"(?![a-z0-9])")


def text_mentions_brand(text_lower: str, aliases: Tuple[str, ...]) -> bool:
    """True if any alias appears in `text_lower` as a bounded token. The
    caller MUST lowercase `text_lower` (matches the engine's convention).

    The text side is folded the same way the alias side is (`_fold_diacritics`),
    because the brands that need this spell themselves WITH the accents in
    exactly the copy we match against: aliases are ASCII ('kerastase', 'estee
    lauder') but the answer text and cited source titles say 'kérastase elixir
    ultime' / 'estée lauder advanced night repair'. Folding only the aliases
    (PR #1391) fixed the rival-storefront direction and left this one silently
    under-counting own-brand visibility and attribution.

    We search the folded text and, when the fold actually changed something, the
    raw text too. The second pass costs nothing on the ASCII hot path (the fold
    is identity there, so there is no second string) and it keeps the module's
    only-ADDS-matches invariant airtight: an accented letter is NOT in the
    patterns' [a-z0-9] boundary class but its folded base letter IS, so folding
    can destroy a word boundary the raw text had — 'bb labé' matches the alias
    'bb lab' before the fold and 'bb labe' does not match after it.
    """
    if not text_lower or not aliases:
        return False
    folded = _fold_diacritics(text_lower)
    haystacks = (folded,) if folded == text_lower else (folded, text_lower)
    for alias in aliases:
        if len(alias) < _MIN_ALIAS_LEN:
            continue
        pattern = _alias_pattern(alias)
        if any(pattern.search(h) for h in haystacks):
            return True
    return False
