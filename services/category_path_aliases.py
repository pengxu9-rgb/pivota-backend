"""Non-taxonomy `category_path` values that appeared in production, and where they belong.

WHY THIS EXISTS. 710 serving-eligible rows (6.8%) sat on 117 distinct paths that are neither a
taxonomy leaf nor an ancestor of one — measured 2026-09-09, and counted from then on by
`serving_eligible_off_taxonomy_path` in services/catalog_invariant_checks.py. Such a row has NO
category door: it cannot match recall's hard prefix (`category_path LIKE 'beauty/makeup/lip/%'`),
and #2122's ancestor rule cannot rescue it either, because that needs the stored path to be a
strict ANCESTOR of the query prefix and a sibling typo is not an ancestor of anything. It reaches a
category query only if the trigram text scan happens to pick it up.

They are near-misses, not nonsense:

    beauty/skincare/tone/toner        315 rows    real: beauty/skincare/treat/toner
    beauty/makeup/lips/lip-gloss        9 rows    real: beauty/makeup/lip/gloss

WHO WROTE THEM. Every regex-derived writer is 0% off-taxonomy, because those fold through
CATEGORY_PATTERNS and can only emit a real leaf. All 710 come from three lanes that write a
free-form string:

    codex_review_v1            146 / 225    64.9%
    taxonomy_reconciler_v1     315 / 820    38.4%   (all 315 toners)
    reviewed_ext_seed_mirror   249 / 1212   20.5%

⚠️ NONE OF THOSE THREE EXISTS IN THIS REPOSITORY, or anywhere in its history. They were run from
scripts that were never committed, or that have since been deleted. That is why the fence for this
cannot be "validate in the writer": there is no writer here to fix, and the next uncommitted script
would bypass it anyway. The fence is `services/category_path_guard.py`, applied where every lane
must pass regardless of who wrote it.

WHAT THIS FILE IS NOT. Not a taxonomy. `CATEGORY_PATTERNS` in services/pdp_category_classifier.py
is the taxonomy; every value on the right-hand side below is asserted to be a leaf of it, so this
file cannot invent a category by typo the way the paths it repairs did.
"""

from __future__ import annotations

from typing import Dict, Optional

from services.pdp_category_classifier import CATEGORY_PATTERNS

TAXONOMY_LEAVES = frozenset(path for _label, path, _pattern in CATEGORY_PATTERNS)


# --- the map -----------------------------------------------------------------------------------
#
# Keyed on the exact stored string, lowercase, as recall would see it. Grouped by the real leaf so
# the reasoning is visible: a reader should be able to disagree with one line without unpicking the
# rest.

ALIASES: Dict[str, str] = {}


def _alias(target: str, *sources: str) -> None:
    for source in sources:
        assert source not in ALIASES, "duplicate alias source: %s" % source
        ALIASES[source] = target


# TONER. ⚠️ THE DIRECTION HERE IS THE REVERSE OF THIS FILE'S FIRST VERSION, and the reversal is
# the whole lesson. `beauty/skincare/tone/toner` is not a typo: it is the canonical target, written
# deliberately 315 times by PIVOTA-Agent's `reconcile-catalog-category-taxonomy.cjs`. I called it
# corruption because I grepped one repo, found no writer, and concluded none existed.
#
# Industry standard settles which way to converge: Google Product Taxonomy 5976 and Shopify
# hb-3-2-9-17 both make `Toners & Astringents` a direct child of Skin Care, a peer of cleansers,
# moisturizers and treatments — never nested inside a treatments node. `treat/toner` was the
# non-standard spelling all along, and `beauty/skincare/tone/toner` is now a leaf in
# CATEGORY_PATTERNS, so those 315 rows need no data change at all.
#
# A face mist is the same shelf — hydrating, applied after cleansing, before moisturiser — and the
# regex classifier already folds `mist` to the toner leaf.
_alias(
    "beauty/skincare/tone/toner",
    "beauty/skincare/treat/toner",
    "beauty/skincare/toners",
    "beauty/skincare/mist",
)

# TREATMENTS. The taxonomy has no eye or acne leaf; both are treatments, which is where an eye
# cream already lands when the regex writer classifies one.
_alias(
    "beauty/skincare/treat/treatment",
    "beauty/skincare/eye-care",
    "beauty/skincare/eye-cream",
    "beauty/skincare/eye/balm",
    "beauty/skincare/eye-patches",
    "beauty/skincare/eye/brightener",
    "beauty/skincare/eye/treatment",
    "beauty/skincare/eye/eye_oil",
    "beauty/skincare/acne-patch",
    "beauty/skincare/acne-treatment",
    "beauty/skincare/treatments",
    "beauty/skincare/essence",
    "beauty/skincare/face-balm",
    "beauty/skincare/face/treatments",
)

_alias("beauty/skincare/treat/serum", "beauty/skincare/serums", "beauty/skincare/cleanser/serum")

_alias(
    "beauty/skincare/treat/exfoliant",
    "beauty/skincare/exfoliator/powder",
    "beauty/skincare/exfoliate/face-scrub",
    "beauty/skincare/exfoliator/peel",
    "beauty/skincare/exfoliator/scrub",
)

_alias(
    "beauty/skincare/treat/mask",
    "beauty/skincare/mask/overnight",
    "beauty/skincare/mask/aha",
    "beauty/skincare/lip/lip_mask",
    "beauty/skincare/lip-care/lip-mask",
)

_alias(
    "beauty/skincare/moisturize/oil",
    "beauty/skincare/face-oil",
    "beauty/skincare/oil",
    "beauty/skincare/oil/carrier",
)

_alias(
    "beauty/skincare/moisturize/cream",
    "beauty/skincare/moisturizer/balm",
    "beauty/skincare/moisturizer/gel",
    "beauty/skincare/moisturizers",
    "beauty/skincare/moisturizer/lotion",
    "beauty/skincare/moisturizer/milk",
    "beauty/skincare/moisturizer/night_cream",
    "beauty/skincare/moisturizer/tinted",
    "beauty/skincare/face/lotion",
    "beauty/skincare/hand/cream",
    "beauty/skincare/hand/nail",
)

_alias(
    "beauty/skincare/cleanse/cleanser",
    "beauty/skincare/cleanser/gel",
    "beauty/skincare/cleansers",
    "beauty/skincare/cleanser/foam",
    "beauty/skincare/cleanser/makeup_remover",
    "beauty/accessories/cleansing-pads",
)

# LIP. `lips` for `lip`, and a redundant `lip-` on the leaf. The taxonomy has no plumper leaf;
# a plumping balm is a balm.
_alias(
    "beauty/makeup/lip/balm",
    "beauty/makeup/lips/lip-balm",
    "beauty/makeup/lips/plumper",
    "beauty/makeup/lips/lip_plumper",
    "beauty/makeup/multi-purpose/color-balm",
    "beauty/skincare/lip",
    "beauty/skincare/lip/plumper",
    "beauty/skincare/lip/plumping",
    "beauty/skincare/lips/plumper",
)
_alias("beauty/makeup/lip/gloss", "beauty/makeup/lips/lip-gloss")
_alias("beauty/makeup/lip/liner", "beauty/makeup/lips/lip-liner")
_alias("beauty/makeup/lip/tint", "beauty/makeup/lips/lip-tint")
_alias("beauty/makeup/lip/oil", "beauty/makeup/lips/lip_oil")

# EYE / FACE MAKEUP.
_alias("beauty/makeup/eye/eyeliner", "beauty/makeup/eyes/eyeliner")
_alias("beauty/makeup/eye/brow", "beauty/makeup/eyebrow")
_alias("beauty/makeup/eye/mascara", "beauty/makeup/eyes/lash")
_alias("beauty/makeup/face/foundation", "beauty/makeup/base")
_alias("beauty/tools/sponge", "beauty/makeup/tools/sponge")

# BODY. FOUR spellings of the same shelf — `body-care`, `bodycare`, `skincare/body`,
# `personal_care` — plus bath, which the taxonomy folds into body care.
_alias(
    "beauty/body/care",
    "beauty/body-care/deodorant",
    "beauty/body-care/body-wash",
    "beauty/body-care/body-oils",
    "beauty/body-care/bath-soaks",
    "beauty/body-care/hand-wash",
    "beauty/body-care/body-scrubs",
    "beauty/body-care/body-balms",
    "beauty/bodycare/body-oil",
    "beauty/bodycare/body-lotion",
    "beauty/bodycare/body-cream",
    "beauty/bodycare/body-scrub",
    "beauty/bodycare/body-wash",
    "beauty/bodycare/scrub",
    "beauty/bodycare/soap",
    "beauty/bodycare/body-milk",
    "beauty/bath/shower_gel",
    "beauty/bath/soap",
    "beauty/bath/shower/oil",
    "beauty/personal_care/deodorant",
    "beauty/skincare/body/body-oil",
    "beauty/skincare/body/butter",
    "beauty/skincare/body/acne-treatment",
    "beauty/skincare/body/treatment",
)

# SETS. A kit is a kit whatever shelf its contents come from.
_alias(
    "beauty/sets/gift-set",
    "beauty/skincare/sets",
    "beauty/skincare/bundle",
    "beauty/skincare/toner/set",
    "beauty/bodycare/sets",
    "beauty/makeup/sets",
    "beauty/mystery-box",
    "beauty/makeup/eyes/lip",
)


# --- the gaps ----------------------------------------------------------------------------------
#
# Paths with NO honest target. Mapping these would be worse than leaving them: filing a nail polish
# under `beauty/makeup/face/blush` makes it findable by the wrong query, which is a different and
# louder defect than not being findable at all. They stay off-taxonomy, stay counted by the
# invariant, and need a TAXONOMY DECISION — new leaves — not a rename.
#
# Kept as data, not a comment, so the count in the report cannot drift from the list.

TAXONOMY_GAPS: Dict[str, str] = {
    "beauty/makeup/nails/nail-polish": "no nail-colour leaf (devices/nail is a device)",
    "beauty/makeup/nails/cuticle-oil": "no nail-colour leaf",
    "beauty/makeup/nails/nail-polish-remover": "no nail-colour leaf",
    "wellness/supplements": "no wellness root",
    "beauty/accessories/makeup-bag": "no accessories leaf",
    "beauty/accessories": "no accessories leaf",
    "beauty/accessories/pouch": "no accessories leaf",
    "beauty/accessories/headband": "no accessories leaf",
    "beauty/accessory/soap_dish": "no accessories leaf",
    "beauty/bath/accessory/soap-saver": "no accessories leaf",
    "beauty/travel/organizer": "no accessories leaf",
    "beauty/travel/toiletry-bag": "no accessories leaf",
    "home/fragrance/candle": "no home root",
    "home/bath/towels/hooded": "no home root",
    "beauty/oral-care/toothpaste": "no oral-care leaf",
    "beauty/oral-care/teeth-whitening-devices": "no oral-care leaf",
    "beauty/beard-care/oil": "no men's-grooming leaf",
    "beauty/men/beard/oil": "no men's-grooming leaf",
    "beauty/spa/body_treatment": "a SERVICE, not a product",
    "beauty/aromatherapy/pen": "no aromatherapy leaf",
    "beauty/makeup/setting-spray": "no setting-spray leaf; face/powder is a different product",
    "beauty/makeup/eyes/lashes": "no false-lash leaf",
    "beauty/skincare/tools": "tools/* are brushes and sponges only",
    "beauty/skincare/tools/gua-sha": "tools/* are brushes and sponges only",
    "beauty/skincare/tools/exfoliator": "tools/* are brushes and sponges only",
    "beauty/skincare/sunscreen/refillable-case": "an accessory, not a sunscreen",
}


# --- invariants on this file itself --------------------------------------------------------------
#
# The failure being repaired is "somebody wrote a path that is not in the taxonomy". A map that can
# do the same thing while repairing it is worthless, so the targets are checked at import.

assert not (set(ALIASES) & set(TAXONOMY_GAPS)), "a path is both aliased and declared a gap"
_BAD = sorted(set(ALIASES.values()) - TAXONOMY_LEAVES)
assert not _BAD, "alias target is not a taxonomy leaf: %s" % _BAD
_SELF = sorted(p for p in ALIASES if p in TAXONOMY_LEAVES)
assert not _SELF, "aliasing away a path that IS a real leaf: %s" % _SELF


# The prefixes recall actually asks for — the leaf's PARENT plus a slash, per
# `category_path_prefix_for_query`. Mirrors services/catalog_invariant_checks.py, which counts the
# same cohorts in SQL; kept in Python here because this module runs on the write path.
LEAF_PARENTS = frozenset(
    path.rsplit("/", 1)[0] if "/" in path else path for path in TAXONOMY_LEAVES
)
ANCESTOR_NODES = frozenset(
    "/".join(path.split("/")[:i])
    for path in TAXONOMY_LEAVES
    for i in range(1, len(path.split("/")))
)
# The roots the recall taxonomy actually indexes. NOT the same as the classifier's `_ALLOWED_ROOTS`,
# which has eleven: `sports`, `toys`, `food` and friends are legitimate classifications that recall
# simply has no prefix for, and it is not this module's business to reject them.
TAXONOMY_ROOTS = frozenset(path.split("/")[0] for path in TAXONOMY_LEAVES)


def has_category_door(path: Optional[str]) -> bool:
    """Can category recall reach this path AS STORED?

    Two doors, and this asks for them the way recall does — case-sensitively, without trimming,
    because that is precisely why a mis-cased path is unreachable:

      1. the hard prefix, `category_path LIKE '<leaf-parent>/%'`, which earns the +90 depth score;
      2. #2122's ancestor admission, for a path that is a strict ancestor of a query prefix.

    Note a path can be reachable WITHOUT being a leaf: `electronics/laptops/gaming` matches
    `LIKE 'electronics/%'` because `electronics` is the parent of the `electronics/ereader` leaf.
    Rejecting it for not being a leaf would throw away a working classification.
    """
    if not path:
        return False
    if path in ANCESTOR_NODES:
        return True
    return any(path.startswith(parent + "/") for parent in LEAF_PARENTS)


def resolve(path: Optional[str]) -> Optional[str]:
    """The taxonomy leaf a stored path should have been, or None if there is no honest answer.

    Case- and slash-insensitive on the way IN only: recall itself is neither, which is exactly why
    a mis-cased path is unreachable, so this has to catch what recall cannot. What comes back is
    always a canonical leaf, never a normalised version of the input.
    """
    if not path:
        return None
    key = path.strip().lower().strip("/")
    if not key:
        return None
    if key in TAXONOMY_LEAVES:
        return key
    return ALIASES.get(key)
