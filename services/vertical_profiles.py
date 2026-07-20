"""Multi-vertical profile registry + the single shared vertical resolver.

Phase 0 of the multi-vertical audit architecture
(PIVOTA-Agent/docs/vertical_expansion_electronics_pilot_scope.md, Part 1 +
Part 2 "Phase 0"). Beauty remains the primary vertical; this module makes the
audit pipeline *vertical-aware* without adding any new vertical's content.

Two responsibilities:

1. ``resolve_vertical`` — ONE shared resolver returning
   ``beauty | fashion | electronics | other``. It is built from the UNION of the
   three category resolvers that exist today, all verified against code on
   2026-07-08:
     * ``agent_center_bd_report_service._vertical_for`` (weakest — no
       earphone/earbud/audio/bone-conduction/tws tokens; it is NOT promoted
       as-is),
     * ``PIVOTA-Agent/src/server.js`` ``resolveCanonicalCategoryPathPrefixForQuery``
       (the most complete audio set),
     * ``PIVOTA-Agent/src/pdpBuilder.js`` ``ELECTRONICS_CATEGORY_RE`` /
       ``FASHION_CATEGORY_RE``.
   Resolution is per-SKU; a merchant/audit-level value is only a default and
   override. Beauty must not win ties on incidental weak tokens — a SKU whose
   text carries a stray "wellness" token but is clearly audio resolves
   ``electronics`` (see ``_ELECTRONICS_DEMOTES_BEAUTY``).

2. ``VerticalProfile`` registry — one profile per vertical. Components stop
   owning vertical knowledge (scattered constants) and start reading it from a
   profile. Phase 0 migrates the *existing beauty constants* into the ``beauty``
   profile behind identical behavior; ``generic`` is the default for an unknown
   vertical (beauty is a profile, not the fallback); ``electronics_audio`` is a
   Phase-0 STUB whose content Phase 1 fills.

Import discipline: this is a leaf module. It imports only the stdlib and
``competitor_brand_filter`` (itself a leaf). It must NEVER import
``agent_center_bd_report_service`` — that module imports this one, so the reverse
would be a cycle.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, List, Mapping, Optional, Tuple

# The competitor ingredient / category-form token sets live in
# competitor_brand_filter (150+ safety-sensitive tokens with their own
# documentation). We REFERENCE them into the beauty profile rather than retype
# them here, so the beauty profile "owns" them without any transcription risk to
# the competitor-drop logic. competitor_brand_filter does not import this module,
# so no cycle.
from services.competitor_brand_filter import (
    _CATEGORY_FORM as _BEAUTY_COMPETITOR_FORM_TOKENS,
    _INGREDIENTS as _BEAUTY_COMPETITOR_INGREDIENT_TOKENS,
)

# --------------------------------------------------------------------------- #
# Beauty vocabulary — MIGRATED here from agent_center_bd_report_service.py so the
# beauty profile is the single home for "what beauty is". The report service now
# imports these back as module-level aliases, keeping every call site
# byte-identical (see the golden-file regression guard).
# --------------------------------------------------------------------------- #

# Personal-care / beauty category head-nouns + the body-part qualifiers that pair
# with them ("hair oil", "face cream", "lip balm"). Used to extract a clean
# category from a product title when no structured category exists.
BEAUTY_CATEGORY_HEAD_NOUNS = frozenset({
    "oil", "butter", "treatment", "mask", "shampoo", "conditioner", "serum",
    "cream", "spray", "balm", "gel", "wax", "pomade", "tonic", "ampoule",
    "essence", "cleanser", "moisturizer", "moisturiser", "sunscreen", "sunblock",
    "toner", "lotion", "scrub", "peel", "mist", "foam", "wash", "soap",
    "exfoliant", "emulsion", "patch", "stick", "lipstick", "mascara",
    "foundation", "concealer", "primer", "powder", "blush", "supplement",
    "gummies", "gummy", "capsule", "tablet",
})
BEAUTY_CATEGORY_MODIFIERS = frozenset({
    "hair", "face", "facial", "skin", "body", "eye", "lip", "lips", "scalp",
    "foot", "hand", "sun", "night", "day", "leave", "curl", "curly",
})

# Title-fallback categories for store-less beauty brands audited by URL. Each
# rule = (trigger tokens, buyer-facing category label). Ordered — first match
# wins. These were hardcoded "beauty supplement" / "supplement" fallbacks in
# _category_for_unbranded_prompts; they are now beauty-profile members so that an
# unknown vertical falls to `generic` (which has no fallbacks) instead of
# collapsing every URL audit to "beauty supplement".
BEAUTY_CATEGORY_FALLBACKS: Tuple[Tuple[frozenset, str], ...] = (
    (frozenset({"collagen", "vitamin c", "niacin"}), "beauty supplement"),
    (frozenset({"supplement", "gummy", "gummies"}), "supplement"),
)

# Beauty-flavored variant-noise tokens a clean category must never be built from
# ("Triple Shine Grape" -> not a category). Blocklist for _noisy_prompt_category.
BEAUTY_NOISY_PROMPT_TOKENS = frozenset({"glow", "grape", "jelly", "orange", "shine"})

# Retailer / marketplace NAME tokens classify_host misses (its registry keys on
# hosts). The "category winner" panel is about a competing PRODUCT, so a store
# must never be selected as the winner.
BEAUTY_RETAILER_TOKENS = frozenset({
    "coupang", "gmarket", "qoo10", "shopee", "lazada", "aliexpress", "temu",
    "amazon", "walmart", "sephora", "ulta", "nordstrom", "costco",
    "oliveyoung", "musinsa", "kurly", "wconcept", "iherb", "yesstyle",
    "stylevana", "bluemercury",
})


# --------------------------------------------------------------------------- #
# Beauty DEVICE sub-profile vocabulary (VODANA pilot — hair-styling appliances:
# flat irons, straighteners, curling irons/wands, dryers, hot brushes). A beauty
# device is still the ``beauty`` VERTICAL (same persisted column), but must NOT be
# run through the INCI / ingredient-grounded machinery — it has no ingredients.
# The beauty topical-vs-device split is a RUNTIME profile choice (mirrors the
# electronics audio/drone split), decided from these tokens; see
# ``BEAUTY_DEVICE_PROFILE`` + ``_beauty_is_device`` below.
#
# These tokens ALSO join the beauty *resolver* keyword set (``_BEAUTY_DEVICE_KEYWORDS``
# is unioned into both resolver tiers) so an under-tagged device row — category
# just "Hair Styling Tools", no "beauty" word — still resolves ``beauty`` instead
# of collapsing to ``other``. Every entry is hair-tool-specific and verified NOT
# to be a substring of any topical-beauty/supplement word, so a cream / serum /
# supplement is never pulled to the device profile. (Bare "iron" is deliberately
# EXCLUDED — it is a clothes iron / iron supplement; only the phrase "flat iron"
# and the joined token "flatiron" qualify.)
_BEAUTY_DEVICE_TYPE_TOKENS = frozenset({
    "straightener", "straighteners", "styler", "stylers", "hairdryer",
    "blowdryer", "flatiron",
})
_BEAUTY_DEVICE_PHRASES: Tuple[str, ...] = (
    "flat iron", "hair straightener", "straightening brush", "curling iron",
    "curling wand", "curling brush", "hair curler", "hair dryer", "blow dryer",
    "blow dry brush", "hot air brush", "hot brush", "air styler", "hair styler",
    "styling iron", "styling wand", "hair styling tool", "hot styling",
)
# Union consulted by the resolver's beauty match (substring-based, like the rest
# of the beauty keyword set). Kept SEPARATE from the migrated
# ``_BEAUTY_CATEGORY_KEYWORDS`` so the Phase-0 golden guard keeps covering the
# topical set unchanged; device coverage is purely additive.
_BEAUTY_DEVICE_KEYWORDS: frozenset = _BEAUTY_DEVICE_TYPE_TOKENS | frozenset(_BEAUTY_DEVICE_PHRASES)


# --------------------------------------------------------------------------- #
# Resolver keyword sets — the UNION of the three resolvers.
# --------------------------------------------------------------------------- #

# Beauty is matched by SUBSTRING (mirrors the legacy _vertical_for exactly:
# "skin" in "skincare" == True), so switching to this resolver does not change
# any beauty classification.
_BEAUTY_CATEGORY_KEYWORDS: Tuple[str, ...] = (
    "beauty", "skin", "cosmetic", "makeup", "wellness", "supplement", "vitamin",
)
# Health-adjacent tokens that also appear off-beauty (a "wellness" earbud, a
# "vitamin"-water speaker gift). Beauty may not win a tie against a clear
# electronics signal when ITS ONLY matches are weak — see _ELECTRONICS_DEMOTES_BEAUTY.
_BEAUTY_WEAK_KEYWORDS = frozenset({"wellness", "supplement", "vitamin"})

# Beauty title-tier tokens: the substring keywords PLUS the head-nouns and
# fallback triggers, so a title-only beauty product ("Marine Collagen",
# "Sleep Gummies") still resolves beauty and its category fallback fires exactly
# as it did before this refactor. Only consulted on the title tier (see below).
_BEAUTY_TITLE_KEYWORDS: frozenset = frozenset(_BEAUTY_CATEGORY_KEYWORDS) | BEAUTY_CATEGORY_HEAD_NOUNS | _BEAUTY_DEVICE_KEYWORDS | {
    tok for tokens, _ in BEAUTY_CATEGORY_FALLBACKS for tok in tokens
}

# Fashion: legacy _vertical_for set UNION pdpBuilder.js FASHION_CATEGORY_RE.
# Matched by substring. Fashion never demotes beauty (see below), so broad tokens
# here can only refine a legacy "other", never flip a beauty product.
_FASHION_CATEGORY_KEYWORDS: Tuple[str, ...] = (
    "fashion", "apparel", "clothing", "sleepwear", "shirt", "dress", "shoe",
    "sneaker", "boot", "accessor", "jewel", "outerwear", "denim", "lingerie",
    "underwear", "swim", "skirt", "coat", "jacket", "sweater", "hoodie", "jean",
    "pant", "trouser", "sock", "scarf", "glove", "belt", "wear",
)

# Electronics: legacy _vertical_for set UNION pdpBuilder.js ELECTRONICS_CATEGORY_RE
# UNION server.js audio set (the completeness _vertical_for lacked). Matched by
# substring; curated so no member is a substring of a common beauty/supplement
# word (verified: none of these appear inside beauty vocabulary).
#
# DELIBERATELY EXCLUDED: "tablet"/"tablets" — a supplement dosage form (Pivota's
# core), NOT a computing tablet, so it must not pull a beauty SKU to electronics.
_ELECTRONICS_CATEGORY_KEYWORDS: Tuple[str, ...] = (
    "electronic", "electronics", "device", "laptop", "computer", "phone",
    "camera", "gaming", "smartwatch", "console", "monitor", "router",
    "headphone", "headphones", "earphone", "earphones", "earbud", "earbuds",
    "airpods", "speaker", "speakers", "audio", "soundbar", "microphone",
    "bluetooth", "wireless", "kindle", "kobo", "boox", "ereader",
    # Aerial imaging / drones (Phase-1b drone sub-vertical). Verified: none is a
    # substring of any beauty/supplement word. "camera" (above) already caught
    # HoverAir's "self-flying camera"; these catch drone-only text (e.g. "DJI
    # Neo camera drone", a bare "quadcopter").
    "drone", "drones", "quadcopter", "quadcopters", "fpv",
)
# Short/ambiguous electronics tokens matched WHOLE-WORD only, so "anc" does not
# fire inside "fragrance"/"radiance"/"balance" and "tv" does not fire inside
# arbitrary words. "uav" is whole-word so it never fires inside another token.
_ELECTRONICS_WORD_KEYWORDS = frozenset({"anc", "tws", "tv", "uav"})
# Multi-word electronics phrases matched against the normalized (hyphen->space)
# text — the niche audio signals a bare token set misses, plus the drone phrases
# that carry no standalone electronics token ("self flying camera").
_ELECTRONICS_PHRASES: Tuple[str, ...] = (
    "bone conduction", "noise cancelling", "noise cancellation", "open ear",
    "true wireless", "galaxy buds", "studio buds", "e reader", "ebook reader",
    "over ear", "in ear",
    "self flying", "flying camera", "camera drone", "follow me drone",
)

# Only ELECTRONICS demotes an incidental weak-beauty match. The Phase-0 pilot is
# audio and its tokens are unambiguous; fashion's tokens are broad, so letting
# fashion demote beauty would wrongly flip beauty-adjacent supplements
# ("women's wellness gummies"). Fashion therefore only refines a legacy "other".
_ELECTRONICS_DEMOTES_BEAUTY = True

_VALID_VERTICALS = frozenset({"beauty", "fashion", "electronics", "other"})


def _normalize(text: Any) -> str:
    """Lowercase; collapse hyphens/underscores/whitespace to single spaces so
    phrase matches ("bone-conduction" -> "bone conduction") land."""
    return re.sub(r"[\s_\-]+", " ", str(text or "").lower()).strip()


def _category_text(product: Mapping[str, Any]) -> str:
    """Same fields the legacy _vertical_for read: product_type + category +
    category_path. Title is NOT included here — it is a separate lower-priority
    tier so that _vertical_for's category-only semantics stay byte-identical."""
    return _normalize(
        " ".join(str(product.get(k) or "") for k in ("product_type", "category", "category_path"))
    )


def _matched_beauty(text: str, *, keywords: frozenset) -> set:
    return {kw for kw in keywords if kw in text}


def _matched_substr(text: str, keywords: Tuple[str, ...]) -> set:
    return {kw for kw in keywords if kw in text}


def _matched_electronics(text: str, tokens: set) -> set:
    hits = {kw for kw in _ELECTRONICS_CATEGORY_KEYWORDS if kw in text}
    hits |= {kw for kw in _ELECTRONICS_WORD_KEYWORDS if kw in tokens}
    hits |= {ph for ph in _ELECTRONICS_PHRASES if ph in text}
    return hits


def _classify(text: str, *, beauty_keywords: frozenset) -> str:
    """Score verticals over one text blob and pick a winner. Returns 'other' when
    nothing matches.

    Tie-break priority preserves the legacy _vertical_for order (beauty, then
    fashion, then electronics) so genuine ties resolve exactly as before. The one
    behavioral addition is demotion: when beauty's ONLY matches are weak tokens
    and there is a clear electronics signal, beauty is zeroed so the SKU resolves
    electronics.
    """
    if not text:
        return "other"
    tokens = set(re.findall(r"[a-z0-9]+", text))

    matched_beauty = _matched_beauty(text, keywords=beauty_keywords)
    matched_fashion = _matched_substr(text, _FASHION_CATEGORY_KEYWORDS)
    matched_electronics = _matched_electronics(text, tokens)

    scores = {
        "beauty": len(matched_beauty),
        "fashion": len(matched_fashion),
        "electronics": len(matched_electronics),
    }

    beauty_strong = bool(matched_beauty - _BEAUTY_WEAK_KEYWORDS)
    if (
        _ELECTRONICS_DEMOTES_BEAUTY
        and scores["beauty"]
        and not beauty_strong
        and scores["electronics"]
    ):
        scores["beauty"] = 0

    order = ("beauty", "fashion", "electronics")
    best = max(order, key=lambda v: (scores[v], -order.index(v)))
    if scores[best] <= 0:
        return "other"
    return best


def resolve_vertical(
    product: Mapping[str, Any],
    *,
    override: Optional[str] = None,
    title: Optional[str] = None,
) -> str:
    """Resolve one SKU's vertical: ``beauty | fashion | electronics | other``.

    Resolution order (Principle 1): audit/merchant override -> per-SKU
    product_type/category/category_path -> title heuristics -> ``other``.

    ``title`` is optional: pass it where a title-only (URL-audit) SKU must still
    resolve (e.g. the intake write path and the unbranded-category fallback);
    omit it to preserve the legacy category-only semantics of ``_vertical_for``.
    """
    if override:
        norm = str(override).strip().lower()
        if norm in _VALID_VERTICALS:
            return norm
        # Map a couple of common aliases; anything unknown falls through to
        # signal-based resolution rather than trusting a bad override.
        if norm in {"electronics_audio", "audio"}:
            return "electronics"
        if norm == "generic":
            return "other"

    result = _classify(
        _category_text(product),
        beauty_keywords=frozenset(_BEAUTY_CATEGORY_KEYWORDS) | _BEAUTY_DEVICE_KEYWORDS,
    )
    if result != "other":
        return result

    if title:
        # Title tier: broader beauty vocabulary (head-nouns + fallback triggers)
        # so "Marine Collagen" resolves beauty. Only reached when the category
        # tier found nothing, so it can never flip a category-tier result.
        return _classify(_normalize(title), beauty_keywords=_BEAUTY_TITLE_KEYWORDS)

    return "other"


# --------------------------------------------------------------------------- #
# Intake structure helpers (Fix Plan B) — shared by the two catalog_products
# write sites (ingest_standard_products + the external-seed mirror) so their
# category normalization and unresolved-vertical accounting cannot drift.
# Pure functions; no I/O. Additive to this leaf module.
# --------------------------------------------------------------------------- #

# The category signal fields resolve_vertical reads off a product mapping. A row
# whose vertical resolves to 'other' with ALL of these empty never carried any
# machine-readable structure at all — that is the "unresolved" cohort T3 counts
# and the intake brake trips on.
_VERTICAL_SIGNAL_FIELDS: Tuple[str, ...] = ("product_type", "category", "category_path")

# Default share of unresolved-vertical rows above which an intake run should fail
# (the founder's "stop ingesting structureless garbage" brake). Configurable per
# call site / env; this is only the fallback.
DEFAULT_UNRESOLVED_VERTICAL_FAIL_THRESHOLD = 0.20


def normalize_category(value: Any) -> Optional[str]:
    """Case/trim normalization for the free-text ``category`` column (T4).

    Lowercases and collapses internal whitespace so "Haircare", "haircare " and
    "Hair  Care" converge. This is CASE/TRIM ONLY — it performs NO semantic
    renaming (that is a separate ontology effort). Empty / whitespace-only input
    returns ``None`` so a blank stays NULL rather than an empty string.
    """
    if value is None:
        return None
    norm = re.sub(r"\s+", " ", str(value)).strip().lower()
    return norm or None


def is_vertical_unresolved(resolved: Optional[str], product: Mapping[str, Any]) -> bool:
    """T3: a row is "unresolved" when ``resolve_vertical`` returned ``'other'``
    AND it carried no category/product_type/category_path signal at all.

    A row that resolved to a real vertical is never unresolved. A row that
    resolved 'other' but DID carry category text (just no keyword match) is a
    lexicon gap, not a structure gap, so it is NOT counted here — that keeps the
    brake from tripping on genuinely-categorized-but-uncovered products.
    """
    if str(resolved or "").strip().lower() != "other":
        return False
    for key in _VERTICAL_SIGNAL_FIELDS:
        if str(product.get(key) or "").strip():
            return False
    return True


def summarize_unresolved_vertical(
    unresolved: int,
    total: int,
    *,
    threshold: float = DEFAULT_UNRESOLVED_VERTICAL_FAIL_THRESHOLD,
) -> dict:
    """Build the per-run intake summary + brake verdict for T3.

    Returns the unresolved count/total, the share, the threshold, a one-line
    ``summary`` string (``unresolved_vertical: N/M (P%)``), and ``should_fail``
    — True when the share strictly exceeds ``threshold``. An empty run
    (``total == 0``) never fails.
    """
    total = max(int(total), 0)
    unresolved = max(int(unresolved), 0)
    share = (unresolved / total) if total else 0.0
    return {
        "unresolved_vertical": unresolved,
        "total": total,
        "share": share,
        "threshold": threshold,
        "summary": f"unresolved_vertical: {unresolved}/{total} ({share * 100:.1f}%)",
        "should_fail": bool(total) and share > threshold,
    }


# --------------------------------------------------------------------------- #
# VerticalProfile registry.
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class BriefRules:
    """The two category-specific slots of the strategic-brief system prompt
    (everything else in that prompt is vertical-neutral). ``claim_rules`` replaces
    the beauty INGREDIENTS+CLAIMS block; ``cold_pitch_publishers`` replaces the
    mainstream-publisher list a merchant shouldn't be told to cold-pitch.

    A profile with ``brief_rules is None`` uses the INCUMBENT (beauty) prompt
    verbatim — see services.strategic_brief._render_system_prompt."""

    claim_rules: str
    cold_pitch_publishers: str


@dataclass(frozen=True)
class VerticalProfile:
    """One vertical's swappable content. Phase 0 populates ``beauty`` (migrated
    from today's constants), a neutral ``generic`` default, and a
    ``electronics_audio`` STUB (Phase 1 fills its content).

    Fields marked "Phase 1" are intentionally empty/placeholder in Phase 0 — they
    exist so the seam is visible, not because they carry content yet. Nothing in
    Phase 0 reads them.
    """

    name: str

    # --- category / head-noun resolution (Phase 0, live) ---
    category_head_nouns: frozenset = field(default_factory=frozenset)
    category_modifiers: frozenset = field(default_factory=frozenset)
    category_fallbacks: Tuple[Tuple[frozenset, str], ...] = ()
    noisy_prompt_tokens: frozenset = field(default_factory=frozenset)

    # --- competitor / retailer panels (Phase 0: data migrated) ---
    retailer_tokens: frozenset = field(default_factory=frozenset)
    competitor_ingredient_tokens: frozenset = field(default_factory=frozenset)
    competitor_form_tokens: frozenset = field(default_factory=frozenset)

    # --- Phase 1 placeholders (not read in Phase 0) ---
    attribute_strategy: str = "llm_extractor"   # beauty overrides to lexicon_first
    brief_rules: Optional["BriefRules"] = None
    publisher_avoid_list: Tuple[str, ...] = ()
    authority_hosts: Tuple[str, ...] = ()
    health_sensitive: Optional[bool] = None
    evidence_bindings: str = "none"
    grounded_coverage_disclosure: Optional[str] = None
    # "what helps with X" is a problem/concern-framed discovery shape ("what helps
    # with dry skin"). It only reads naturally when the vertical's use-cases are
    # genuine CONCERNS (beauty). For verticals whose use-cases are activities or
    # product types (electronics: "sports", "bone conduction headphones") it
    # produces junk ("what helps with bone conduction headphones") — so it's gated
    # off there. Default True preserves the historical beauty behavior.
    problem_framed_prompts: bool = True


BEAUTY_PROFILE = VerticalProfile(
    name="beauty",
    category_head_nouns=BEAUTY_CATEGORY_HEAD_NOUNS,
    category_modifiers=BEAUTY_CATEGORY_MODIFIERS,
    category_fallbacks=BEAUTY_CATEGORY_FALLBACKS,
    noisy_prompt_tokens=BEAUTY_NOISY_PROMPT_TOKENS,
    retailer_tokens=BEAUTY_RETAILER_TOKENS,
    competitor_ingredient_tokens=frozenset(_BEAUTY_COMPETITOR_INGREDIENT_TOKENS),
    competitor_form_tokens=frozenset(_BEAUTY_COMPETITOR_FORM_TOKENS),
    attribute_strategy="lexicon_first",
    evidence_bindings="inci_grounded",
    grounded_coverage_disclosure=None,
)

# Default for an unknown vertical. Neutral: no beauty head-nouns, no category
# fallbacks (an unknown URL audit no longer collapses to "beauty supplement"), no
# beauty competitor/retailer knowledge. This is what makes beauty *a* profile
# rather than *the* fallback.
GENERIC_PROFILE = VerticalProfile(
    name="generic",
    attribute_strategy="llm_extractor",
    evidence_bindings="none",
    grounded_coverage_disclosure=(
        "grounded-evidence dimensions are unavailable for this category"
    ),
)

# Phase-1 content. Each field was re-traced to its actual consumer before being
# filled (per the scope doc's "re-trace each table row to its call site" rule):
#   - category_head_nouns/category_modifiers -> _category_from_title (already
#     wired in Phase 0 to read the resolved profile);
#   - competitor_ingredient_tokens/competitor_form_tokens -> competitor_brand_filter
#     (drops type-name fake "brands" like "wireless earbuds");
#   - retailer_tokens -> _competitor_is_brandlike (keeps Best Buy/Newegg out of
#     the "category winner" panel);
#   - authority_hosts / publisher_avoid_list / health_sensitive / brief_rules are
#     data here; their consumers (cited-host classifier, strategic_brief) are
#     wired in Phase 1b.
# Audio-first (the Mojawa pilot). Generic-electronics PDP checks live in the
# existing per-vertical branch; don't build registry inheritance yet.
_ELECTRONICS_AUDIO_HEAD_NOUNS = frozenset({
    "headphones", "headphone", "earbuds", "earbud", "earphones", "earphone",
    "headset", "speaker", "speakers", "soundbar", "earpods", "buds",
    "microphone", "amplifier", "amp", "dac", "turntable", "receiver",
    "subwoofer", "monitors",
})
# Modifiers that pair before a head-noun ("wireless earbuds", "gaming headset",
# "bluetooth speaker", "open-ear headphones").
_ELECTRONICS_AUDIO_MODIFIERS = frozenset({
    "wireless", "bluetooth", "wired", "gaming", "portable", "open", "over",
    "in", "on", "noise", "true", "sport", "sports", "studio", "bookshelf",
})
# Category / TYPE + descriptor tokens: a competitor name built ENTIRELY of these
# is a product type, not a brand ("wireless earbuds", "noise cancelling
# headphones", "bone conduction earphones", "true wireless"). A real brand
# (Bose, Sony, Shokz, JBL) carries an identity token and survives.
_ELECTRONICS_AUDIO_TYPE_TOKENS = frozenset({
    # type nouns
    "headphones", "headphone", "earbuds", "earbud", "earphones", "earphone",
    "headset", "speaker", "speakers", "soundbar", "buds", "earpods",
    "microphone", "mic", "amplifier", "amp", "dac", "subwoofer",
    # descriptors that make a name generic
    "wireless", "wired", "bluetooth", "noise", "cancelling", "canceling",
    "cancellation", "anc", "tws", "bone", "conduction", "open", "over", "in",
    "on", "ear", "true", "portable", "waterproof", "sweatproof", "sport",
    "sports", "gaming", "hifi", "audiophile", "stereo", "mono", "surround",
    "wearable", "smart",
})
_ELECTRONICS_AUDIO_RETAILER_TOKENS = frozenset({
    "bestbuy", "newegg", "crutchfield", "adorama", "bhphoto", "microcenter",
    "amazon", "walmart", "costco", "target", "ebay", "aliexpress", "temu",
})
# Audio authority hosts — where citations are valued AND the outreach pitch-target
# list (partner-visible quality, per the scope doc). Consumer wired in Phase 1b.
_ELECTRONICS_AUDIO_AUTHORITY_HOSTS = (
    "rtings.com", "soundguys.com", "head-fi.org", "whathifi.com",
    "audiosciencereview.com", "techradar.com", "cnet.com", "theverge.com",
    "tomsguide.com", "wirecutter.com",
)
# Big mainstream outlets a merchant should NOT be told to cold-pitch (brief rule,
# Phase 1b): even a strong audio brand rarely lands these on a cold email.
_ELECTRONICS_AUDIO_PUBLISHER_AVOID = (
    "Wirecutter", "Rtings", "SoundGuys", "What Hi-Fi", "The Verge", "CNET",
)

ELECTRONICS_AUDIO_PROFILE = VerticalProfile(
    name="electronics_audio",
    category_head_nouns=_ELECTRONICS_AUDIO_HEAD_NOUNS,
    category_modifiers=_ELECTRONICS_AUDIO_MODIFIERS,
    category_fallbacks=(),          # no beauty-style fallbacks; unknown -> ""
    noisy_prompt_tokens=frozenset(),
    retailer_tokens=_ELECTRONICS_AUDIO_RETAILER_TOKENS,
    competitor_ingredient_tokens=frozenset(),   # electronics has no "ingredients"
    competitor_form_tokens=_ELECTRONICS_AUDIO_TYPE_TOKENS,
    attribute_strategy="llm_extractor",
    brief_rules=BriefRules(
        claim_rules=(
            '- SPECS: name specs in plain buyer terms and say what they DO for the '
            'buyer ("15-hour battery so it lasts a week of workouts", "IP68 so it '
            'survives a pool swim", "open-ear / bone conduction so you still hear '
            'traffic"). Do NOT dump a spec sheet, model-number soup, or a '
            'codec/driver-size list.\n'
            '- CLAIMS: a hard spec (waterproof rating, battery hours, wireless '
            'range) is fine when it appears in EVIDENCE — state the evidenced spec, '
            'never inflate it. Frame subjective superlatives ("best sound", "studio '
            'quality") as positioning, not proven fact, and do NOT use medical or '
            'health-efficacy language for a consumer audio product.'
        ),
        cold_pitch_publishers="Wirecutter, Rtings, SoundGuys, What Hi-Fi, etc.",
    ),
    publisher_avoid_list=_ELECTRONICS_AUDIO_PUBLISHER_AVOID,
    authority_hosts=_ELECTRONICS_AUDIO_AUTHORITY_HOSTS,
    # Do NOT swap in electronics tokens ("battery"/"waterproof") — that would
    # falsely flag earphones health-sensitive. Electronics is not health-sensitive.
    health_sensitive=False,
    # Audio use-cases are activities/product types, not concerns — "what helps
    # with X" is nonsensical here, so drop the problem-framed discovery shape.
    problem_framed_prompts=False,
    evidence_bindings="none",
    grounded_coverage_disclosure=(
        "grounded-evidence dimensions are unavailable for this category"
    ),
)


# --------------------------------------------------------------------------- #
# electronics_drone sub-profile (Phase-1b — self-flying camera drones; HoverAir
# pilot). A drone resolves to the `electronics` vertical (SAME persisted column
# value as audio) but must NOT inherit the audio profile's authority hosts /
# competitor type-filter / brief rules. The drone-vs-audio split is a RUNTIME
# profile choice made by ``resolve_profile`` from drone tokens in the SKU text;
# audio stays the default electronics sub-profile when no drone signal is present.
# --------------------------------------------------------------------------- #

# Drone TYPE tokens + phrases. A drone SKU carries at least one of these; an audio
# SKU carries none, so the two electronics sub-profiles never collide. Kept in
# sync with the drone additions to _ELECTRONICS_CATEGORY_KEYWORDS/_PHRASES above.
_DRONE_TYPE_TOKENS = frozenset({
    "drone", "drones", "quadcopter", "quadcopters", "uav", "fpv",
})
_DRONE_PHRASES: Tuple[str, ...] = (
    "self flying", "flying camera", "camera drone", "follow me drone",
)

_ELECTRONICS_DRONE_HEAD_NOUNS = frozenset({
    "drone", "drones", "quadcopter", "quadcopters", "camera", "uav",
})
# Modifiers that pair before the head-noun ("self-flying camera", "foldable
# drone", "follow-me drone", "mini drone", "pocket drone").
_ELECTRONICS_DRONE_MODIFIERS = frozenset({
    "self", "flying", "foldable", "follow", "me", "mini", "pocket", "aerial",
    "cinematic", "compact", "portable", "gimbal",
})
# TYPE + descriptor tokens: a "competitor" name built ENTIRELY of these is a
# product type, not a brand ("camera drone", "mini drone", "self-flying camera").
# A real brand (DJI, Autel, Skydio, HoverAir, Insta360) carries an identity token
# and survives the type-name filter.
_ELECTRONICS_DRONE_TYPE_TOKENS = frozenset({
    "drone", "drones", "quadcopter", "quadcopters", "uav", "fpv", "camera",
    "cam", "self", "flying", "follow", "me", "foldable", "mini", "pocket",
    "aerial", "cinematic", "compact", "portable", "gimbal", "4k", "8k",
})
_ELECTRONICS_DRONE_RETAILER_TOKENS = frozenset({
    "bestbuy", "bhphoto", "adorama", "amazon", "walmart", "costco", "target",
    "ebay", "newegg", "aliexpress", "temu",
})
# Drone authority hosts — where citations are valued AND the outreach pitch-target
# list. faa.gov is first-class here: the sub-250g registration rule is a genuine
# buying-decision lever for this category (ADR-002 decision intelligence).
_ELECTRONICS_DRONE_AUTHORITY_HOSTS = (
    "engadget.com", "thedronegirl.com", "dronexl.co", "dronesgator.com",
    "dpreview.com", "uavcoach.com", "pilotinstitute.com", "tomsguide.com",
    "techradar.com", "theverge.com", "faa.gov",
)
_ELECTRONICS_DRONE_PUBLISHER_AVOID = (
    "The Verge", "Engadget", "DPReview", "Tom's Guide", "TechRadar",
    "The Drone Girl",
)

ELECTRONICS_DRONE_PROFILE = VerticalProfile(
    name="electronics_drone",
    category_head_nouns=_ELECTRONICS_DRONE_HEAD_NOUNS,
    category_modifiers=_ELECTRONICS_DRONE_MODIFIERS,
    category_fallbacks=(),          # no beauty-style fallbacks; unknown -> ""
    noisy_prompt_tokens=frozenset(),
    retailer_tokens=_ELECTRONICS_DRONE_RETAILER_TOKENS,
    competitor_ingredient_tokens=frozenset(),   # drones have no "ingredients"
    competitor_form_tokens=_ELECTRONICS_DRONE_TYPE_TOKENS,
    attribute_strategy="llm_extractor",
    brief_rules=BriefRules(
        claim_rules=(
            '- SPECS: name specs in plain buyer terms and say what they DO for the '
            'buyer ("125g so it is exempt from FAA registration", "8K video so you '
            'can crop and reframe in post", "palm takeoff so there is no controller '
            'to learn", "obstacle avoidance so it dodges branches while tracking"). '
            'Do NOT dump a spec sheet or model-number soup.\n'
            '- REGULATORY: the sub-250g / FAA-registration line is a real buying '
            'decision lever — state it only when the exact takeoff weight is in '
            'EVIDENCE, and never imply a heavier model is registration-exempt.\n'
            '- CLAIMS: a hard spec (takeoff weight, flight time, tracking speed, '
            'video resolution) is fine when it appears in EVIDENCE — state the '
            'evidenced number, never inflate it. Frame subjective superlatives '
            '("best tracking", "cinematic") as positioning, not proven fact, and do '
            'NOT use safety-critical absolutes ("crash-proof", "cannot fail").'
        ),
        cold_pitch_publishers="The Drone Girl, DroneXL, Engadget, DPReview, etc.",
    ),
    publisher_avoid_list=_ELECTRONICS_DRONE_PUBLISHER_AVOID,
    authority_hosts=_ELECTRONICS_DRONE_AUTHORITY_HOSTS,
    health_sensitive=False,
    # Drone use-cases are activities/product types ("vlogging", "hiking"), not
    # concerns — "what helps with a vlogging drone" is junk, so drop the
    # problem-framed discovery shape (same rationale as audio).
    problem_framed_prompts=False,
    evidence_bindings="none",
    grounded_coverage_disclosure=(
        "grounded-evidence dimensions are unavailable for this category"
    ),
)


# --------------------------------------------------------------------------- #
# beauty_device sub-profile (VODANA pilot — beauty × 3C hair-styling appliances).
# A hair styler resolves to the `beauty` vertical (SAME persisted column value as
# a topical cosmetic — the topical/device split is a RUNTIME profile choice), but
# it is a DEVICE: it has no INCI, so it must NOT inherit the beauty profile's
# `inci_grounded` evidence bindings or `lexicon_first` attribute strategy. It is
# the beauty-side mirror of the electronics audio/drone split.
#
# The one deliberate difference from the electronics sub-profiles: beauty devices
# KEEP `problem_framed_prompts=True`. "what helps with frizzy hair / heat damage"
# is a genuine buyer CONCERN (unlike "what helps with a drone"), which is exactly
# what makes this category a beauty × electronics hybrid — spec-driven like
# electronics, concern-framed like beauty.
# --------------------------------------------------------------------------- #

_BEAUTY_DEVICE_HEAD_NOUNS = frozenset({
    "straightener", "iron", "dryer", "curler", "wand", "styler", "brush",
    "comb", "tool",
})
# Modifiers that pair before the head-noun ("flat iron", "hair dryer", "curling
# wand", "hot brush", "ionic dryer").
_BEAUTY_DEVICE_MODIFIERS = frozenset({
    "flat", "hair", "curling", "straightening", "blow", "hot", "air",
    "styling", "ionic", "ceramic", "tourmaline", "cordless",
})
# TYPE + descriptor tokens: a "competitor" name built ENTIRELY of these is a
# product type, not a brand ("flat iron", "curling iron", "hair dryer"). A real
# brand (Dyson, ghd, VODANA, T3, Shark, BaByliss) carries an identity token and
# survives the type-name filter.
_BEAUTY_DEVICE_COMPETITOR_TYPE_TOKENS = frozenset({
    "flat", "iron", "hair", "straightener", "straightening", "curling", "curler",
    "wand", "dryer", "blow", "hot", "air", "styler", "styling", "tool", "brush",
    "comb", "ionic", "ceramic", "tourmaline", "cordless", "professional", "salon",
    "device", "appliance",
})
_BEAUTY_DEVICE_RETAILER_TOKENS = frozenset({
    "amazon", "oliveyoung", "coupang", "sephora", "ulta", "target", "walmart",
    "bestbuy", "costco", "yesstyle", "stylevana", "stylekorean", "qoo10", "ebay",
})
# Beauty-device authority hosts — where device reviews are valued AND the outreach
# pitch-target list. These are beauty/tool reviewers, NOT the topical-skincare
# authority set (INCI-focused labs) and NOT the audio/drone tech reviewers.
_BEAUTY_DEVICE_AUTHORITY_HOSTS = (
    "allure.com", "byrdie.com", "wirecutter.com", "goodhousekeeping.com",
    "instyle.com", "cosmopolitan.com", "harpersbazaar.com", "refinery29.com",
    "nytimes.com", "goodhousekeeping.co.uk",
)
_BEAUTY_DEVICE_PUBLISHER_AVOID = (
    "Allure", "Byrdie", "Wirecutter", "Good Housekeeping", "InStyle", "Cosmopolitan",
)

BEAUTY_DEVICE_PROFILE = VerticalProfile(
    name="beauty_device",
    category_head_nouns=_BEAUTY_DEVICE_HEAD_NOUNS,
    category_modifiers=_BEAUTY_DEVICE_MODIFIERS,
    category_fallbacks=(),          # a device is never a "beauty supplement"
    noisy_prompt_tokens=frozenset(),
    retailer_tokens=_BEAUTY_DEVICE_RETAILER_TOKENS,
    competitor_ingredient_tokens=frozenset(),   # devices have no "ingredients"
    competitor_form_tokens=_BEAUTY_DEVICE_COMPETITOR_TYPE_TOKENS,
    attribute_strategy="llm_extractor",         # specs, NOT the beauty lexicon
    brief_rules=BriefRules(
        claim_rules=(
            '- SPECS: name device specs in plain buyer terms and say what they DO '
            'for the hair ("dual voltage 110-240V so it works on any trip", '
            '"ceramic/tourmaline plates so heat spreads evenly and snags less", '
            '"adjustable heat 150-230C so fine hair can stay on a low setting", '
            '"silicone Softbar so it glides without tugging"). Do NOT dump a spec '
            'sheet or model-number soup.\n'
            '- HAIR OUTCOMES: frizz control, shine, style longevity, and hair-type '
            'suitability (fine / thick / curly / color-treated) are the real '
            'decision levers — state them only when they appear in EVIDENCE, and '
            'frame heat-damage protection as reduced-damage styling, never as a '
            'repair or medical claim.\n'
            '- CLAIMS: a hard spec (wattage, plate temperature, voltage) is fine '
            'when it appears in EVIDENCE — state the evidenced number, never inflate '
            'it. Frame subjective superlatives ("salon-quality", "best straightener") '
            'as positioning, not proven fact, and do NOT use medical or '
            'health-efficacy language ("heals", "repairs damage") for a styling tool.'
        ),
        cold_pitch_publishers="Allure, Byrdie, Wirecutter, Good Housekeeping, etc.",
    ),
    publisher_avoid_list=_BEAUTY_DEVICE_PUBLISHER_AVOID,
    authority_hosts=_BEAUTY_DEVICE_AUTHORITY_HOSTS,
    # A styling tool uses heat but is not health-sensitive in the topical sense
    # (no ingredient safety / medical efficacy). Heat-damage is a claim lever,
    # handled in brief_rules, not a health-sensitivity flag.
    health_sensitive=False,
    # KEPT True (unlike audio/drone): a beauty device's use-cases are genuine hair
    # concerns ("frizzy hair", "heat damage", "flat fine hair"), so the
    # problem-framed discovery shape reads naturally. This is the hybrid.
    problem_framed_prompts=True,
    # Phase-1 for this sub-profile: no INCI grounding (that is the whole point of
    # splitting it out of BEAUTY_PROFILE). A device-spec evidence binding is a
    # later slice; "none" keeps it out of the ingredient-grounded path today.
    evidence_bindings="none",
    grounded_coverage_disclosure=(
        "grounded-evidence dimensions are unavailable for this category"
    ),
)


VERTICAL_PROFILES: Mapping[str, VerticalProfile] = {
    "beauty": BEAUTY_PROFILE,
    "beauty_device": BEAUTY_DEVICE_PROFILE,
    "generic": GENERIC_PROFILE,
    "electronics_audio": ELECTRONICS_AUDIO_PROFILE,
    "electronics_drone": ELECTRONICS_DRONE_PROFILE,
}

# How a resolved vertical (resolve_vertical's return) maps to a registered
# profile. ``fashion`` has no profile of its own in Phase 0 -> generic; the
# existing per-vertical fashion PDP-completeness branch is untouched and keeps
# its own logic.
_VERTICAL_TO_PROFILE = {
    "beauty": "beauty",
    "electronics": "electronics_audio",
    "fashion": "generic",
    "other": "generic",
}


def get_profile(vertical: Optional[str]) -> VerticalProfile:
    """Return the profile for a resolved vertical (or a registry key). Unknown ->
    generic. Beauty is never the fallback."""
    key = str(vertical or "").strip().lower()
    if key in VERTICAL_PROFILES:
        return VERTICAL_PROFILES[key]
    return VERTICAL_PROFILES.get(_VERTICAL_TO_PROFILE.get(key, "generic"), GENERIC_PROFILE)


def _electronics_is_drone(*texts: Any) -> bool:
    """True when any SKU text blob carries a drone TYPE signal. Splits the
    ``electronics`` vertical into its drone vs audio sub-profile. Audio is the
    default electronics sub-profile, so a text with no drone token stays audio —
    byte-identical to the pre-drone behavior for a genuine audio SKU."""
    for raw in texts:
        text = _normalize(raw)
        if not text:
            continue
        tokens = set(re.findall(r"[a-z0-9]+", text))
        if tokens & _DRONE_TYPE_TOKENS:
            return True
        if any(phrase in text for phrase in _DRONE_PHRASES):
            return True
    return False


def _beauty_is_device(*texts: Any) -> bool:
    """True when any SKU text blob carries a beauty-DEVICE signal (hair-styling
    appliance). Splits the ``beauty`` vertical into its topical vs device
    sub-profile. Topical is the default beauty sub-profile, so a text with no
    device token stays topical — byte-identical to the pre-device behavior for a
    genuine cream / serum / supplement SKU.

    Single TYPE tokens are matched WHOLE-WORD (so "styler" never fires inside an
    unrelated word); the multi-word phrases are matched as substrings of the
    hyphen-normalized text ("flat-iron" -> "flat iron")."""
    for raw in texts:
        text = _normalize(raw)
        if not text:
            continue
        tokens = set(re.findall(r"[a-z0-9]+", text))
        if tokens & _BEAUTY_DEVICE_TYPE_TOKENS:
            return True
        if any(phrase in text for phrase in _BEAUTY_DEVICE_PHRASES):
            return True
    return False


def resolve_profile_for_vertical(
    vertical: Optional[str],
    product: Optional[Mapping[str, Any]] = None,
    *,
    title: Optional[str] = None,
) -> VerticalProfile:
    """Map an already-resolved vertical STRING to its VerticalProfile, applying
    the electronics drone/audio sub-split when product text is available.

    This is the profile-selection counterpart to ``resolve_vertical`` (which owns
    the beauty|fashion|electronics|other decision). Both sub-splits — beauty
    topical/device and electronics audio/drone — are RUNTIME profile choices only;
    they are never persisted (``resolved_vertical`` stays ``beauty`` /
    ``electronics`` respectively). A caller that passes no product text gets the
    default sub-profile (topical for beauty, audio for electronics), unchanged."""
    key = str(vertical or "").strip().lower()
    if key in ("beauty", "electronics"):
        blobs: List[Any] = [title]
        if isinstance(product, Mapping):
            blobs.extend(
                product.get(k)
                for k in ("product_type", "category", "category_path", "title", "raw_title")
            )
        if key == "beauty" and _beauty_is_device(*blobs):
            return BEAUTY_DEVICE_PROFILE
        if key == "electronics" and _electronics_is_drone(*blobs):
            return ELECTRONICS_DRONE_PROFILE
    return get_profile(key)


def resolve_profile(
    product: Mapping[str, Any],
    *,
    override: Optional[str] = None,
    title: Optional[str] = None,
) -> VerticalProfile:
    """One-shot: resolve a SKU's vertical then its VerticalProfile (including the
    electronics drone/audio sub-split). Convenience wrapper over
    ``resolve_vertical`` + ``resolve_profile_for_vertical`` for call sites that
    hold the product mapping."""
    vertical = resolve_vertical(product, override=override, title=title)
    return resolve_profile_for_vertical(vertical, product, title=title)
