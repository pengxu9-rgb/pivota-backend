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

# Latin letters whose diacritic is welded INTO the letterform (a stroke, a bar,
# a ligature) rather than layered on as a combining mark. Unicode gives them NO
# decomposition at all, so the decompose-and-drop-marks fold above is a no-op on
# them and the [^a-z0-9] collapse then deletes them outright — silently mangling
# the brand: 'Æther Beauty' → 'ther beauty', 'Straße' → 'stra e', 'Søstrene' →
# 's strene'. Those junk aliases can never match the ASCII spellings and hosts
# they are compared against ('aetherbeauty.com'), which is the same under-count
# [[the diacritic fold]] exists to fix — it just does not reach these letters.
#
# So we transliterate them explicitly. This is the standard ASCII romanization
# (what ICU/unidecode produce), and the multi-char expansions are the point: the
# German 'ß' is 'ss' and the ligature 'æ' is 'ae' in every ASCII spelling of a
# brand that uses them.
#
# Applied AFTER the decompose, so a letter that carries BOTH a stroke and a
# combining mark lands correctly too ('ǣ' = æ+macron → NFD → 'æ' → 'ae').
#
# Every entry maps a LETTER to LETTERS, never a symbol to alphanumerics — so
# unlike NFKD (see `compat` below) this cannot manufacture a brand mention out
# of unrelated punctuation, and it is safe on both the alias and the text side.
# Keys are lowercase only: both callers lowercase before folding, and any
# uppercase residue is scrubbed by the lowercase-only classes downstream exactly
# as it is today.
#
# It does collapse a real distinction, and that is inherent to romanizing at all
# (ICU and unidecode do the same): a brand 'Sol' now matches the Norwegian word
# 'søl', 'Ore' the Danish 'øre'. Accepted — the same collision already exists for
# any ASCII brand named after a common word, the catalog is ~90% ASCII/Korean
# brands, and the alternative is leaving every stroked brand unmatchable.
_UNDECOMPOSABLE_LATIN = {
    "ß": "ss", "æ": "ae", "œ": "oe", "ĳ": "ij",
    "ø": "o", "ł": "l", "đ": "d", "ð": "d", "þ": "th",
    "ħ": "h", "ŧ": "t", "ı": "i", "ŋ": "n", "ſ": "s",
}
_UNDECOMPOSABLE_TABLE = str.maketrans(_UNDECOMPOSABLE_LATIN)
# Guard so non-Latin copy never pays for the translate. str.translate walks the
# whole string, and Korean/CJK answer text (a hot path here) contains none of
# these letters, so the pass would be pure cost for a guaranteed no-op.
_UNDECOMPOSABLE_RE = re.compile("[" + "".join(_UNDECOMPOSABLE_LATIN) + "]")


def _romanize(s: str) -> str:
    """Map the undecomposable Latin letters to ASCII. No-op unless one is
    present, so ASCII and CJK strings are returned untouched."""
    if s.isascii() or not _UNDECOMPOSABLE_RE.search(s):
        return s
    return s.translate(_UNDECOMPOSABLE_TABLE)


def _fold_diacritics(s: str, *, compat: bool = False, translit: bool = True) -> str:
    """Fold accented letters to their ASCII base letter: decompose, drop the
    combining marks, then transliterate the letters Unicode refuses to
    decompose. 'kérastase' → 'kerastase'; 'æther' → 'aether'; 'straße' →
    'strasse'.

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
    folded = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return folded if not translit else _romanize(folded)


def _normalize_legacy(text: Optional[str]) -> str:
    """`_normalize` without the undecomposable-letter transliteration — i.e. the
    alias spelling this module produced before that map existed, where ß æ ø ł đ
    were simply deleted by the [^a-z0-9] collapse ('Æther Beauty' → 'ther
    beauty').

    derive_brand_aliases keeps these mangled forms ALONGSIDE the romanized ones,
    because they are junk that MATCHES: 'ther beauty' hits the raw text 'æther
    beauty' — the pattern's boundary class is [a-z0-9], so the 'æ' the collapse
    deleted reads as a word boundary. Dropping them in favour of the correct
    'aether beauty' would REMOVE matches the engine makes today, which the module
    invariant forbids. Costs nothing for the ASCII brands that are the norm: both
    normalizations agree and the dedup in _add collapses them, so 'COSRX' still
    yields exactly ('cosrx',).

    Be honest about what this buys, though. For REALISTIC copy the romanized
    alias already covers the real mention (text 'æther beauty' romanizes to
    'aether beauty' and matches the correct alias), so what the legacy spelling
    actually preserves is mostly the OLD FALSE POSITIVES: the matches it uniquely
    makes are pathological glue ('zætherbeauty', 'æther beautyß'), and for a
    brand whose mangled form is a generic fragment it is outright bad — 'Þór
    Skin' → 'or skin' matches "great for hair or skin health". That FP is
    PRE-EXISTING (today it is the brand's ONLY alias, so this change strictly
    improves such a brand by adding the correct 'thor skin' next to it), and
    correcting it means deliberately removing a match — a separate decision from
    this fix, and one that should be made against the invariant explicitly rather
    than smuggled in here.
    """
    return _normalize(text, translit=False)


def _normalize(text: Optional[str], *, translit: bool = True) -> str:
    """Lowercase, fold diacritics to their ASCII base letter (NFKD, combining
    marks dropped, undecomposable letters romanized), strip ®/™/(r)/(tm), reduce
    to alnum tokens joined by single spaces. 'Crème de la Mer®' → 'creme de la
    mer', 'Kérastase' → 'kerastase', 'Æther Beauty' → 'aether beauty'.

    The fold must happen BEFORE the non-alnum collapse: without it an accented
    letter becomes a token BREAK, splitting the brand into junk tokens
    ('Kérastase' → 'k rastase' / de-spaced 'krastase') that can never match the
    ASCII text these aliases are compared against — answer copy, source titles,
    and registrable host labels ('kerastase-usa'). That silent miss is what let
    a rival's own storefront through _flag_competitor_by_name and into 'Get
    cited on' outreach (prod run 83e8fcb4, competitor 'Kérastase' vs
    kerastase-usa.com).

    The same is true one level down for the letters Unicode will not decompose
    at all (ß æ ø ł đ): the collapse DELETES them, so 'Æther Beauty' became the
    junk 'ther beauty' and 'Straße' the junk 'stra e'. `translit` romanizes them
    instead; pass translit=False for the legacy spelling (see _normalize_legacy).
    """
    t = (text or "").lower()
    # Fold BEFORE the mark strip, so a fullwidth/compatibility spelling of the
    # marks normalizes into the ASCII forms the strip below already knows:
    # 'Brand（ｔｍ）' → '(tm)' → stripped. (Stripping first would leave a bogus
    # 'tm' token behind.) The literal '™' needs no such care either way — NFKD
    # maps it to UPPERCASE 'TM', which the lowercase-only class below scrubs.
    t = _fold_diacritics(t, compat=True, translit=translit)
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

    A brand written with an undecomposable letter (ß æ ø ł đ) contributes BOTH
    the romanized spelling and the legacy mangled one — see _normalize_legacy for
    why dropping the latter would violate the module invariant. For every other
    brand the two normalizations agree and the dedup below collapses them.
    """
    aliases: List[str] = []
    seen = set()

    def _add(candidate: str) -> None:
        candidate = candidate.strip()
        if len(candidate) >= _MIN_ALIAS_LEN and candidate not in seen:
            seen.add(candidate)
            aliases.append(candidate)

    def _add_forms(norm: str) -> None:
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

    def _add_brand_forms(value: str) -> None:
        _add_forms(_normalize(value))
        _add_forms(_normalize_legacy(value))

    _add_brand_forms(brand or "")
    for vendor in vendors or ():
        _add_brand_forms(vendor)

    reg = _registrable_name_from_host(host)
    if reg:
        _add(_normalize(reg).replace(" ", ""))
        _add(_normalize_legacy(reg).replace(" ", ""))

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
    caller MUST lowercase `text_lower` (matches the engine's convention).

    The text side is folded the same way the alias side is (`_fold_diacritics`),
    because the brands that need this spell themselves WITH the accents in
    exactly the copy we match against: aliases are ASCII ('kerastase', 'estee
    lauder') but the answer text and cited source titles say 'kérastase elixir
    ultime' / 'estée lauder advanced night repair'. Folding only the aliases
    (PR #1391) fixed the rival-storefront direction and left this one silently
    under-counting own-brand visibility and attribution.

    We search every spelling the fold can produce, because folding can DESTROY a
    word boundary as well as create a match — a non-ASCII letter is not in the
    patterns' [a-z0-9] boundary class but its ASCII fold is. Searching all of
    them is what keeps the module's only-ADDS-matches invariant airtight:

      - the full fold ('kérastase' → 'kerastase', 'æther' → 'aether') — the
        match this function exists to make;
      - the marks-only fold, which folds the accents but leaves the
        undecomposable letters (ß æ ø ł đ) standing. Transliterating them can eat
        a boundary the accent fold had kept: alias 'kerastase' matches
        'kérastaseø' via this spelling ('ø' ends the word) but not via the full
        fold, which reads 'kerastaseo' as one longer word;
      - the raw text, for the same reason one level up — 'bb labé' matches the
        alias 'bb lab' and the folded 'bb labe' does not.

    Text with no foldable Latin letter in it — plain ASCII answer copy, and the
    Korean/CJK titles that are the other hot path here — takes none of this: both
    folds are provably the identity on it (every key of the romanize map lies
    inside _FOLDABLE_LATIN_RE, so if that finds nothing the romanize cannot fire
    either), and it searches the raw string alone, exactly as it did before the
    fold existed. Only genuinely accented or stroked text builds a second or
    third haystack.
    """
    if not text_lower or not aliases:
        return False
    if text_lower.isascii() or not _FOLDABLE_LATIN_RE.search(text_lower):
        haystacks = (text_lower,)
    else:
        # One decomposition pass: the romanized spelling is a cheap table
        # translate off the marks-only one, not a second NFD over the string.
        marks_only = _fold_diacritics(text_lower, translit=False)
        haystacks = tuple(dict.fromkeys((_romanize(marks_only), marks_only, text_lower)))
    for alias in aliases:
        if len(alias) < _MIN_ALIAS_LEN:
            continue
        pattern = _alias_pattern(alias)
        if any(pattern.search(h) for h in haystacks):
            return True
    return False
