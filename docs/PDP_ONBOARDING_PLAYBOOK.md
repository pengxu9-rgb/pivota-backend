# PDP Onboarding Playbook

How a new product — from any source — becomes a high-quality, properly
labeled canonical Pivota PDP. This doc is the source of truth for the
**onboarding standardization** track that follows the Phase 1-9 + 7b
recall architecture work.

- Status: **DRAFT — proposed**
- Created: 2026-05-08
- Owner: peng (decisions) + claude/codex (execution)
- Origin: 2026-05-08 strategic pivot away from corpus-probe optimization
  toward real merchant + seed onboarding quality.

---

## Why this matters

Phase 7b's recall engineering proved the architecture: when canonical
PDPs exist with rich labels, the gateway surfaces them. **Beauty went
from 0/9 lipstick to 9/9 lipstick** purely by populating the canonical
chain with high-quality data.

But the onboarding flow that *creates* canonical PDPs is currently three
separate paths with three different quality bars and no unified
labeling pipeline. A Pivota merchant connecting today via Shopify gets
products into the catalog with no review and minimal labels. An agent-
ingested PDP gets high-quality labels and a multi-merchant scope
upgrade. An external seed mirror produces a sig_* PDP with whatever
crawl quality the seed had.

This is a strategic gap, not a tactical one. As Pivota onboards more
merchants and ingests more external seeds, the **average canonical PDP
quality determines the entire surface's quality**.

---

## Three onboarding paths today

### Path A — Internal merchant sync (Shopify / Wix / WooCommerce)

```
Merchant connects → platform API/webhook → StandardProduct schema
  → catalog_sync_service.ingest_standard_products()
  → catalog_products INSERT (merchant_id, platform, source_system='shopify_products_sync')
  → also writes: catalog_skus, catalog_offers, beauty_* enrichment if matched
  → after-sync: prune_missing_catalog_products_for_source for stale rows
```

**Defaults set:**
- `merchant_id`: real merchant
- `platform`: shopify / wix / woocommerce
- `pdp_scope`: 'unverified' (Phase 6 default)
- `category_path`: regex-classified at INSERT time (Phase 2-redo)
- `category_confidence`: 0.85 if matched, else NULL
- `pivota_signature_id`: minted by mig 071 / Phase C-1

**Tags / attributes carried:**
- `vendor` → `brand`
- `product_type` → kept as-is
- StandardProduct's `tags[]` field → **silently dropped** at ingest (gap #1)
- `visible_attributes{}` → carried to catalog_skus
- `ingredient_ids[]` → carried to catalog_skus

**Quality gate:** none. Whatever the merchant's product feed contains
lands in catalog directly.

### Path B — External seed mirror

```
External seed lands in external_product_seeds (from various scrapers /
agents / harvest jobs)
  → mirror workflow (GHA, manual trigger):
    scripts/mirror_external_seeds_to_catalog_products.py --apply
  → catalog_products INSERT (merchant_id='external_seed', platform='external_seed',
    source_system='external_product_seeds_mirror_v1')
  → no catalog_skus / catalog_offers (those come from the seed's own
    fields if/when ingested via Phase 7a path)
  → INSERT-time regex category_path classification
  → sig minting on insert (Phase C-1)
```

**Defaults set:**
- `merchant_id`: literal 'external_seed' (not a real merchant)
- `platform`: 'external_seed'
- `pdp_scope`: 'unverified' (Phase 6 default)
- `category_path`: regex-classified at INSERT (Phase 2-redo's mirror-
  side classifier)
- `pivota_signature_id`: minted

**Quality gate:** none beyond what the seed already had.

**Volume reality:** 3936 rows currently in this path.

### Path C — Catalog enrichment agent (manual JSONL → Gemini validator → ingest)

```
Hand-curated <category>_pdp_candidates.jsonl
  → run_catalog_enrichment.py validate (Gemini-grounded URL validation)
  → <category>_validated.jsonl with validation_drop_reason fields
  → run_catalog_enrichment.py ingest (Phase 7a canonical chain)
  → catalog_products + catalog_skus + catalog_offers + external_product_seeds
    all written together
```

**Defaults set:**
- `merchant_id`: 'external_seed' (treated as canonical)
- `category_path`: from JSONL directly (no regex inference)
- `category_label_source`: 'enrichment_agent_v1'
- `pdp_scope`: 'multi_merchant_canonical' (post Phase 6 backfill)
- `pivota_signature_id`: minted

**Quality gate:** Gemini URL validator drops candidates that don't
resolve to live PDPs. The validator now persists per-candidate
`validation_drop_reason` (PR #363) and uses `maxOutputTokens=4096` to
avoid truncation (PR #365).

**Coverage:** beauty (lipstick / fragrance / makeup eye+face) + 13
electronics PDPs.

---

## Schema landscape — what's stored, what's read, what's dropped

### Fields on `catalog_products` today (mig 071 era)

| Column | Source path | Used by gateway? | Notes |
|---|---|---|---|
| product_key | composite of merchant/platform/source_product_id | yes | primary key |
| merchant_id, platform, source_product_id | platform API or seed | yes | identity tuple |
| catalog_track | enum: internal_merchant / external_referral | recall ranking | |
| truth_tier | enum: primary / observed / synthesized | recall filter | |
| readiness_tier | enum: referral_only / etc. | downstream offer logic | |
| source_system | path identifier | audit only | |
| source_ref | URL or batch id | audit | |
| title, description, vendor → brand | from sync / JSONL / seed | yes | |
| product_type, category | from sync / JSONL / seed | recall filter (loose) | |
| canonical_url, image_url | from sync / JSONL / seed | yes | |
| product_payload (jsonb) | full source payload | for fidelity / audit | |
| freshness_json (jsonb) | last-seen timestamps | TTL logic | |
| **category_path** | regex (Phase 2/2-redo) OR JSONL (Phase 4) | **yes — load-bearing for recall** | beauty/* + electronics/audio,reading/* |
| category_confidence | 0.85 (regex) or 1.0 (JSONL) | none today | could gate "show in search" |
| **category_label_source** | regex_backfill / regex_backfill_at_mirror / enrichment_agent_v1 | none today | useful for "trust" gating |
| **pdp_scope** | multi_merchant_canonical / merchant_owned / unverified | yes — recall ranking +200 | Phase 6 |
| pdp_scope_source | how it was set | audit | |
| pivota_signature_id | sha derived from identity | yes — sig_* public PDP | |
| pivota_canonical_url | denorm of sig URL | yes | |

### Fields on `catalog_skus`, `catalog_offers`, `catalog_merchants`

These exist for the canonical chain but only the agent path (C) populates
them consistently. Internal Shopify sync (path A) DOES write
catalog_skus/offers. External seed mirror (path B) does NOT — Phase 7b
consciously keeps the helper at product-level for that reason
(`includeSkuOffers=false` default).

### Fields that exist but are silently dropped

- **`StandardProduct.tags[]`** (path A) — populated by Shopify sync,
  not read by ingest_standard_products. Verify and either wire or
  remove.
- **`visible_attributes{}` / `ingredient_ids[]`** — written to
  catalog_skus on Path A but not on Path B (mirror) or Path C (agent
  ingestion writes empty `{}` and `[]`).

### Tag dimensions we have NO field for

- Price tier (under $50, $50-100, $100-200, $200+)
- Use-case (daily, special_occasion, gift, professional)
- Lifestyle (vegan, cruelty_free, sustainable, fragrance_free)
- Demographic (men, women, unisex, kids)
- Skin/hair attributes (oily, dry, sensitive — already partial via
  ingredient_ids but no dedicated tag)
- Concern (anti-aging, brightening, hydrating)

---

## The eight gaps

In ascending order of "how often this hurts":

1. **No unified label authority.** Three paths, three quality bars,
   zero consistency on which fields are required vs optional.
2. **`StandardProduct.tags[]` silently dropped** on Shopify ingest.
   Merchants who tag their products see no benefit.
3. **`pdp_scope='unverified'` is the default**, with no live promotion
   pipeline. New merchant products stay 'unverified' until someone
   manually backfills.
4. **`category_path` regex is beauty-only.** Electronics/home/fashion
   patterns added ad-hoc per Phase 4 batch. Other domains (food, books,
   pet) require a regex extension every time.
5. **No quality gate on Path B.** External seed mirror creates
   sig_* PDPs from any seed in the table. If seed quality is poor
   (thin description, broken image, etc.), the public PDP is poor.
6. **No tag taxonomy beyond `category_path`.** All the price/use-case/
   lifestyle dimensions a real e-commerce search needs aren't represented.
7. **No agent-driven labeling for Paths A and B.** Path C uses Gemini
   for URL validation; nothing uses an LLM to *enrich tags* for products
   that already exist in catalog from sync or mirror.
8. **No promotion lifecycle.** A merchant_owned PDP can never become
   multi_merchant_canonical without manual intervention, even when
   another merchant onboards the same product.

---

## Proposed standardized lifecycle

### Conceptual: every PDP traverses these stages

```
DRAFT → CANDIDATE → VALIDATED → PUBLISHED
                                  ↘ HOLD (low quality)
                                  ↘ ARCHIVED (delisted)
```

| Stage | Means | Gateway visibility |
|---|---|---|
| DRAFT | Just landed from sync/mirror/agent. Has identity (merchant_id+platform+source_product_id) and minimal fields. | not in recall |
| CANDIDATE | Identity populated, basic content (title + image + description) present. | not in recall |
| VALIDATED | Has category_path, has at least one resolved canonical_url, has tags >= N. | in recall, low rank |
| PUBLISHED | Multi-merchant evidence OR human review approved OR agent-curated with confidence ≥ threshold. | in recall, full rank |
| HOLD | Auto-flagged: thin description, no image, off-taxonomy. | not in recall |
| ARCHIVED | Source delisted product (Shopify webhook removed it). | not in recall, kept for sig redirect |

This becomes a new column: `catalog_products.pdp_lifecycle_stage`.

### Three responsibility tracks

1. **Identity layer** (today: live, all 3 paths) — merchant_id,
   platform, source_product_id, sig_id. No change needed.
2. **Content layer** (gap: not unified) — title, description, images,
   price, availability. Needs a per-stage gate: e.g. "DRAFT → CANDIDATE
   requires title + image + description ≥ 50 chars".
3. **Tag layer** (biggest gap) — category_path + new tag dimensions.
   Needs:
   a. A tag taxonomy v1 (decision required — see below)
   b. A labeling agent (Gemini or rule-based) that runs after content
      is in (Path A: post-sync, Path B: post-mirror)
   c. A promotion gate (CANDIDATE → VALIDATED requires tags ≥ N)

### Promotion lifecycle (PUBLISHED gating)

Two automatic triggers:
- **Multi-merchant evidence**: when ≥2 merchants have the same
  source_product_id OR matching brand+title+canonical_url, promote
  the PDP to `pdp_scope='multi_merchant_canonical'` and lifecycle to
  PUBLISHED. (Today Phase 6 has this as a one-shot backfill;
  needs to become live.)
- **Agent confidence**: PDPs ingested via Path C with confidence ≥ 0.9
  promote directly to PUBLISHED.

Manual override: human-curated review for borderline cases.

---

## Decisions you need to make (before any code lands)

These are the structural choices that determine the implementation
shape. Without these, anything I or codex builds will be guesswork.

### Decision 1 — Tag taxonomy v1 scope

Today catalog_products has `category_path` (1 dimension). Industry
norm has 3-5 dimensions. Pick:

- **Option 1A — Conservative**: keep just `category_path` + `tags[]`
  (free-form list). Cheap, flexible, but no facets / no filtering.
- **Option 1B — Faceted v1**: add 4 typed columns —
  `price_tier`, `use_case_tags[]`, `lifestyle_tags[]`,
  `demographic`. Each has a fixed enum.
- **Option 1C — Hybrid**: keep `tags[]` as free-form for narrow LLM-
  generated tags, but ALSO add the 4 typed columns for
  recall/filter use. (Recommended.)

### Decision 2 — Who labels what

For each layer (content, category_path, tags):
- **Path A merchant sync**: trust merchant data + LLM-fill missing?
  Or always re-label with our agent?
- **Path B external seed mirror**: agent-label everything? Or only
  promote to CANDIDATE if seed has tags?
- **Path C catalog enrichment agent**: already labeled via JSONL +
  Gemini. Keep as-is.

Recommended default: **a single LabelAgent runs post-ingest for all
3 paths**, fills gaps, never overwrites merchant-provided values
(merchant data has priority). Agent's confidence is recorded.

### Decision 3 — Quality gate for PUBLISHED

What's the minimum for a PDP to surface in recall?
- Today: just having a row + `category_path` populated.
- Proposed minimums:
  a. **Strict**: title + image + description ≥ 50 chars + category_path
     + 2+ tags + price > 0 + in_stock not unknown.
  b. **Permissive**: title + image + category_path. (Closer to
     today's de facto state.)
  c. **Tiered**: PUBLISHED rank ≥ Strict, VALIDATED rank ≥ Permissive,
     fall back to seed scan beneath VALIDATED.

### Decision 4 — Volume + cost model

Each Gemini call costs money. Auto-labeling all merchant products at
scale could be expensive. Pick:

- **Lazy**: label on first surface (recall hit) only. Cheap, but slow
  to bring new products to discoverability.
- **Eager**: label everything within 24h of ingest. Predictable cost,
  faster value.
- **Tiered**: eager for canonical_chain candidates (sig_* surfaced
  PDPs); lazy for merchant_owned long-tail.

### Decision 5 — Existing data — re-label or grandfather?

We have 4715 catalog_products today, ~3942 with category_path. If we
ship a new tag taxonomy v1, do we:
- **Grandfather**: old rows have NULL on new columns; only new
  ingests get tagged. Clean break.
- **Backfill**: run the LabelAgent over the full catalog. Costs more
  Gemini calls but gives a uniform shape immediately.
- **Backfill canonical-only**: only tag the 17 multi_merchant_canonical
  + agent-ingested PDPs. Cheapest path with the highest impact rows.

---

## Proposed phased plan (after decisions are made)

### Phase O-1 — Wire `tags[]` through Shopify sync (cheap, no new schema)

Single PR. Today's gap #2: `StandardProduct.tags[]` is populated but
ingest doesn't write it. Fix the silent drop. Adds a tags column to
catalog_products if missing, OR stores in product_payload jsonb. ≤1 day.

### Phase O-2 — Tag taxonomy v1 schema

Single migration. Adds the chosen columns from Decision 1 (likely
hybrid: `tags TEXT[]`, `price_tier VARCHAR(16)`, `use_case_tags TEXT[]`,
`lifestyle_tags TEXT[]`, `demographic VARCHAR(16)`). 1-2 days.

### Phase O-3 — LabelAgent v1

A new service that takes a `catalog_products` row + its source data
and returns the tag fields. Implementation choices:
- Gemini-grounded (consistent with existing Phase 4 validator)
- Rule-based regex/keyword (cheaper, less flexible)
- Hybrid: regex for high-confidence keywords, Gemini for the rest

Runs as a background job triggered by ingest. 3-5 days.

### Phase O-4 — Lifecycle stage column + promotion logic

Migration: `catalog_products.pdp_lifecycle_stage` enum. Worker that
runs on ingest + recurring sweep:
- Promotes DRAFT → CANDIDATE on content gate
- Promotes CANDIDATE → VALIDATED on tag gate
- Promotes VALIDATED → PUBLISHED on multi-merchant evidence OR confidence gate

Gateway recall reads `pdp_lifecycle_stage`. 5-7 days.

### Phase O-5 — Recall integration

Extend `_fetch_canonical_search_rows` (backend) and the helper in
PIVOTA-Agent to filter on `pdp_lifecycle_stage IN ('VALIDATED',
'PUBLISHED')` and rank PUBLISHED higher. 1-2 days.

### Phase O-6 — Backfill (per Decision 5)

Run LabelAgent over the chosen scope. Audit invariants (no row
demoted, no category_path overwritten without flag). 1-3 days
depending on scope.

---

## Open questions / risks

- **LLM labeling drift**: Gemini may classify the same product
  differently across runs. Mitigations: persist `confidence`, only
  re-label on schema-version bumps, manual override always wins.
- **Merchant data quality is highly variable**: some Shopify
  merchants are diligent, others tag everything as "general". Need
  to detect "merchant lazy mode" and trigger agent-fill.
- **Tag explosion**: free-form tags can balloon the recall index.
  Constrain to a vocabulary OR cluster post-hoc.
- **External seed mirror is a firehose**: 3936 rows today, could be
  much more. LabelAgent at scale is expensive — strongly consider
  tiered (Decision 4).
- **Demographic / sensitivity tags**: any auto-labeling that touches
  demographic / health needs human review at the policy level. Not
  blocking for v1 but flag for compliance review before O-2.

---

## Cross-links

- Master plan (recall arch): `docs/MASTER_PLAN.md`
- Phase 7b architecture: `docs/PHASE_7B_PLAN.md`
- Phase 4 electronics retrospective: `docs/PHASE_4_ELECTRONICS_PLAN.md`
  (the playbook that proved the pattern)
- StandardProduct schema: `models/standard_product.py:490`
- Shopify sync entry: `services/shopify_products_sync.py:104`
- Agent ingestion: `services/catalog_enrichment_agent/ingestion.py`
- Mirror script: `scripts/mirror_external_seeds_to_catalog_products.py`
- Sig generator (Phase C-1): mig 071
- Phase 2-redo classifier: `services/pdp_category_classifier.py`
- pdp_scope dimension: mig 070
