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
    t = unicodedata.normalize("NFKD", t)
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
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


_SEP = r"[\s\-_]*"           # between any two alias tokens
_SEP_APOS = r"[\s\-_'’]*"    # …plus an apostrophe, where that is safe (below)


@lru_cache(maxsize=8192)
def _alias_pattern(alias: str) -> Pattern[str]:
    """Compile an alias to a boundary-anchored regex. Internal spaces become a
    flexible separator so 'bb lab' matches 'bb lab', 'bb-lab', and 'bblab', and
    'l oreal paris' matches "l'oreal paris"; the non-alnum lookarounds stop it
    matching inside a larger word.

    The apostrophe is a separator because _normalize turns it into a token BREAK
    ("L'Oréal Paris" → 'l oreal paris'), so without it the alias could never
    match the brand as real copy actually spells it, and own-brand visibility
    under-counted for every apostrophe brand. Both the ASCII quote and the
    typographic right-single-quote are accepted — real copy uses U+2019, and the
    diacritic fold leaves it untouched, so it never reduces to U+0027. (U+2018,
    U+02BC and U+FF07 are NOT accepted; that just leaves the old under-count in
    place for those rarer spellings, which is safe, not a new miss.)

    But an apostrophe is only allowed to bind INTO a token of >=2 chars, because
    _normalize splits two different things the same way:

        elision      "L'Oréal Paris" → l | oreal | paris   (binds into 'oreal')
        contraction  "It's Skin"     → it | s | skin        (binds into 's')

    Letting the apostrophe bind into the 1-char token would make the alias
    re-absorb an ordinary English contraction, so "It's Skin" (a real catalog
    brand) would match the sentence "whether it's skin texture or tone" — and in
    the own-brand FILTER paths that would silently drop a real competitor's
    citation, not merely over-credit visibility. Contraction brands lose nothing
    by this: callers OR-in a literal compare, and such text spells the brand
    exactly as stored, so the literal compare already matches it.

    Both classes are supersets of the plain separator, so this only ever ADDS
    matches over the literal compare — never removes one, per the module
    invariant.
    """
    parts = [p for p in alias.split(" ") if p]
    if not parts:
        return re.compile(r"(?!)")
    body = re.escape(parts[0])
    for part in parts[1:]:
        body += (_SEP_APOS if len(part) >= 2 else _SEP) + re.escape(part)
    return re.compile(r"(?<![a-z0-9])" + body + r"(?![a-z0-9])")


def text_mentions_brand(text_lower: str, aliases: Tuple[str, ...]) -> bool:
    """True if any alias appears in `text_lower` as a bounded token. The
    caller MUST lowercase `text_lower` (matches the engine's convention)."""
    if not text_lower or not aliases:
        return False
    for alias in aliases:
        if len(alias) >= _MIN_ALIAS_LEN and _alias_pattern(alias).search(text_lower):
            return True
    return False
