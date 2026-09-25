# Curated feed category evidence

This implementation reuses the category work reviewed in Claude PR #2158
(tip `6a4fba64ed13b33969559417c39a814a6e73cada`) without merging the older branch.
The mapper and review-only repair planner now call one `_resolve_category` policy;
`product_category_path` is only its path-returning wrapper.

Reused from #2158:

- Count distinct matching taxonomy paths before accepting `product_type`. Ordered
  regex matches do not decide ambiguous types such as Blush & Highlighter.
- Carry category confidence through the ingestion payload whitelist and into the
  product insert. Merchant product-type evidence is 0.9; storefront fallback is 0.3.
  Older manual records that supply no confidence retain the existing 0.7 default.
- Preserve `category_label_source='enrichment_agent_v1'`. That field participates in
  canonical-scope lane semantics and is not a free-form provenance label.
- Retain Powder Kiss Lipstick, Powder Kiss Liquid Lipcolour, Powder Kiss Velvet
  Blur Slim Stick and Strobe Cream as negative marketing-title fixtures. General
  title regex classification can file these real lip products as face powder.

Two deliberate differences from #2158 resolve independently measured defects:

1. **Strong product evidence can disagree with a coarse storefront shelf.** A
   storefront default such as `beauty/skincare` is not evidence that every item is
   skincare. An unambiguous merchant `Brush` type may resolve to `beauty/tools/brush`.
   The old area veto would preserve the measured MISSHA error. An explicitly supplied
   taxonomy leaf remains protected unless the direct contradiction guard below
   refuses the evidence; non-beauty feed routing is unchanged.
2. **Titles assign categories only through two bounded exceptions**, with confidence 0.8. A tool noun
   suffix such as Layering Fit Brush or Foundation Brush #101 may resolve a row whose
   type is broad/unclassified. Formula contexts, included applicators and joined
   bundles (`with`, `and`, `+`, `&`, `/`) do not qualify. A specific competing formula
   type still wins. Separately, an explicit lip-oil title can refine generic Lip Care
   or Lip Treatment(s) shelves. This is pinned to the real two-retailer observation
   sharing GTIN 8809530070499; it does not overrule explicit Lip Balm or Lipstick types.

Generic and ambiguous product types otherwise retain the coarse path at low confidence.
They do not become confident leaves merely because the old regex treats a generic
shelf name as a product class. The shallow-category backfill from PR #2159 is the
separate route for unresolved residue; this feed change does not run that backfill.

`scripts/plan_curated_category_repair.py` emits expected-old/proposed-new values from
an existing catalog snapshot and calls the same resolver. It has no database access
or apply mode. Deploying these source changes alone does not rewrite historical rows.

The snapshot planner can separately challenge a known wrong leaf with
`--review-existing-leaves`. Each such row must include `category_evidence` containing
the original merchant title, product_type, source_url and observed_at. It records
that evidence plus the expected old path, and never proposes a coarse downgrade.
This is still a review-only manifest: it does not update any database row. Ordinary
feed mapping and the shallow backfill protect existing specific leaves except for
the explicit primary-mapper evidence refusals described below.

## Contradictory merchant types

The 2026-09-12 public source review found Stila's **Blush & Bronze Hydro-Blur Cheek
Duo** labelled `Eye Liner`, and **Stay All Day Chroma-Flash Liquid Eye Liner** labelled
`Blush`. Their own titles and descriptions establish the contradiction; trusting
the type would deepen a broad category to the wrong leaf.

The mapper now refuses a narrow conflict between explicit eyeliner and cheek
product nouns in the title and the competing merchant type. This is an abstention,
not title-based reassignment. Even repeating the wrong leaf as the caller's category
does not make the product resolved. Fresh mapping leaves `category_path` unresolved;
primary plan acceptance blocks it. Title-only shade labels such as `Blush` are not
sufficient contradiction evidence. A hybrid explicitly naming both families keeps
its supported type. Serum Concealer, Sun Serum and Lip & Cheek Cream also retain
their valid merchant categories. Cross-sell/body copy is not a category assignment
input to this guard.

`Lip Color` and `Lip Colour` are generic shelves, not proof of lipstick. The captured
Flower **Plump Up Gloss Stick** demonstrates the distinction: its own description
calls it a gloss-balm hybrid. Literal merchant alternatives `Lip Gloss/Oil` and
`Lip Gloss (Lip Tint)` also abstain even when legacy regexes recognize just one part.
These inputs cannot gain a specific category through first-match ordering.

The repair wrapper represents these abstentions as **no change**. It never proposes
an empty replacement category. Exact-product cleanup manifests require their own
review of source URL/native identity, expected old category and update timestamp;
no category change implies a currency conversion or market migration. Source
fixtures and regression controls are in
`tests/fixtures/category_type_conflicts_public_observations.json` and
`tests/services/test_curated_category_evidence.py`.
