"""PIVOTA-Agent's `INTENTIONALLY_DISTINCT` list, vendored so this repo cannot violate it silently.

WHY THIS FILE EXISTS. `src/services/beautyTaxonomy.js` in PIVOTA-Agent carries a list of paths that
share a leaf word with a canonical entry and are DELIBERATELY not merged, under the comment: "kept
here so a future pass does not 'helpfully' collapse them."

On 2026-09-10 a future pass helpfully collapsed seven of them. I diffed my alias map against the
gateway's canonical paths and its alias table, found one conflict (toner), and shipped — never
checking the third table, the one whose entire purpose is to say NO. The backfill then merged the
serving-eligible rows behind those seven paths in production.

  beauty/skincare/sets              -> beauty/sets/gift-set
  beauty/bodycare/sets              -> beauty/sets/gift-set
  beauty/makeup/sets                -> beauty/sets/gift-set
  beauty/skincare/eye/balm          -> beauty/skincare/treat/treatment
  beauty/skincare/hand/cream        -> beauty/skincare/moisturize/cream
  beauty/skincare/moisturizer/balm  -> beauty/skincare/moisturize/cream
  beauty/skincare/oil               -> beauty/skincare/moisturize/oil

The gateway is right, and so is the industry standard it happens to match: Google Product Taxonomy
and Shopify both categorise kits BY DOMAIN — `Anti-Aging Skin Care Kits`, `Facial Cleansing Kits`
under Skin Care — rather than in one global sets bucket. A skincare set and a makeup set are not
the same shelf. The non-standard part was this repo having a single `beauty/sets/gift-set`.

⚠️ VENDORED, AND NOTHING NOTICES WHEN THE GATEWAY EDITS ITS LIST. Re-vendor by hand.

An earlier version of this docstring pointed at `taxonomy_code_vs_table_drift` as the mitigation.
That was wrong and worth stating plainly: PIVOTA-Agent has no reader and no writer for
`category_taxonomy` — every row in that table today was written by this repo's seeder — so a
gateway edit to `INTENTIONALLY_DISTINCT` can never surface in the drift check. A mitigation that
cannot fire is worse than none, because it stops anyone looking.

The durable fix is a `do_not_merge` marker column on `category_taxonomy` plus a gateway write
path, so this list stops being a copy. That is a migration and a cross-repo change; until then,
this file is the enforceable form and the count is pinned by a test so a bad re-vendor is loud.

Regenerate with:

    node -e "console.log(JSON.stringify(require('./src/services/beautyTaxonomy.js').INTENTIONALLY_DISTINCT,null,2))"
"""

from __future__ import annotations

# Verbatim from PIVOTA-Agent src/services/beautyTaxonomy.js @ 205e9f1cd, 2026-09-10.
GATEWAY_SOURCE_COMMIT = "205e9f1cd"

INTENTIONALLY_DISTINCT = frozenset({
    "beauty/makeup/lip/oil",
    "beauty/skincare/moisturize/oil",
    "beauty/skincare/oil",
    "beauty/body/oil",
    "beauty/haircare/mask",
    "beauty/haircare/scalp-treatment/mask",
    "beauty/skincare/eye/balm",
    "beauty/skincare/moisturizer/balm",
    "beauty/skincare/eye/serum",
    "beauty/skincare/body/moisturizer",
    "beauty/skincare/hand/cream",
    "beauty/skincare/sets",
    "beauty/haircare/sets",
    "beauty/bodycare/sets",
    "beauty/fragrance/sets",
    "beauty/makeup/sets",
})
