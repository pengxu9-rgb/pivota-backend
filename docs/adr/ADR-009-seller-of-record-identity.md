# ADR-009: Seller-of-Record Identity and the Offer Layer

**Status:** Accepted — decisions ratified by founder 2026-07-07, with the no-fallback
amendment to open decision 1 (see "Resolved decisions")
**Date:** 2026-07-05
**Deciders:** Commerce-index / Trust & Identity owners (peng)
**Builds on:** ADR-007 (citable index vs commerce overlay), ADR-008 (brand-identity
reconciliation). Companion reference: `docs/IDENTITY_REFERENCE.md`.

## Context

Two identity gaps surfaced while closing the Tier-2 attribution loop (T2-1…T2-3,
2026-07-04/05), both rooted in the same conflation: **`merchant_id` means both the
TENANT (account that authenticates/bills) and the SELLER-OF-RECORD (economic subject of
an offer, conversion, and outcome).**

**Gap 1 — offers have no seller.** `external_product_seeds` rows (referral offers) carry
no seller identity at all: their identity is the ANCHOR merchant's `attached_product_key`
plus a raw `domain`. For self-anchored seeds (destination = the anchor's own store — the
Tier-2 un-integrated wedge) attribution is coincidentally correct because anchor == seller.
For cross-seller seeds (attached to merchant A's product, destination seller B) any closed
conversion would be attributed to A, who did not make the sale — the same class of
signal-poisoning the `click_matched` gate exists to prevent.

**Gap 2 — the shared `external_seed` bucket.** Crawl ingestion (ADR-008 Path B) lands
every crawled brand under one placeholder `merchant_id='external_seed'`. Everything
downstream keys `merchant_id` — attribution edges, `aggregated_outcomes`, `seller_trust`,
GSC submissions — so per-brand outcome signal (the moat) is structurally unbuildable for
crawled supply, and a brand that later onboards cannot inherit its history.

Verified facts (2026-07-05, prod read-only):
- `external_product_seeds`: 9,383 rows; `attached_product_key` = 8,004 `prod::`-format,
  720 bare/other, 659 NULL, **0 pipe-format**; Shopify-attached seeds have empty
  `attached_variant_id`.
- `_fetch_attached_seed_rows` (`routes/agent_shop_gateway.py:3471`) matches this column
  with PIPE-format keys → **dead path** (Trap T1 in the identity reference).
- The outcome loop went live 2026-07-04/05; the bucket has ~no outcome history yet —
  backfill is cheap NOW and becomes a data migration later.
- The per-brand-identity discipline already exists on one tier: W5 P3 upserts a minimal
  `catalog_merchants` row for every url_audit brand (`source_system='url_audit_intake'`).
  Crawl ingestion is the only supply path that skips it.

## Decision

### D1 — Three-layer identity model (front-facing = product group; seller lives on the offer)

1. **PRODUCT (decision layer):** the canonical product group — `product_group_id`, falling
   back to `content_key`. Cross-merchant, seller-free by construction. This is what agents
   reference and what alternatives/comparison/per-product outcomes key on.
2. **OFFER = (product × seller):** the ONLY place a seller column lives. Internal offers
   (connected catalogs) and external seeds are the same shape: seller_ref, price,
   availability, destination, transactability (buy-here vs referral). External seeds become
   first-class offers keyed by (pg/content_key, seller_ref); `attached_product_key` is
   demoted to a resolution alias.
3. **PAGE (serving layer):** `pivota_signature_id` stays the per-merchant public
   citation artifact (ADR-006/007). It is not the decision identity and carries no seller
   column (its merchant IS its subject). Sigs are write-once and public: **no identity
   re-key ever re-mints a sig.**

### D2 — Seller-of-record = a `catalog_merchants` row; observed rows are first-class

- Every supply-ingestion path mints (or resolves to) a real per-brand
  `catalog_merchants` row at ingestion — **the shared `external_seed` bucket is banned**
  (generalizing the W5 P3 rule). Observed sellers get `status='observed'`,
  `source_system=<ingestion path>`, `source_ref=<brand domain>`.
- Minting is deterministic and idempotent from the ADR-008 brand identity
  (normalized brand + eTLD+1 domain), exactly like sig minting: same brand+domain → same
  seller identity forever.
- **Graduation attaches, never re-keys:** observed → domain-verified
  (`brand_verified_graduation`) → claimed (`brand_claims`) → tenant attached
  (`merchant_onboarding`). The seller identity is stable across the whole ladder, so
  referral/citation/outcome history transfers to the onboarded brand for free.
- Because downstream tables already key `merchant_id`, making the seller a merchant row
  means T2/W8 attribution, outcomes, and seller_trust work unchanged — one identity
  system, no parallel "sellers" table.

### D3 — Thread seller through the attribution chain

- `external_product_seeds.seller_ref` (nullable TEXT → a catalog_merchants.merchant_id)
  + `seed_kind` (`self` | `cross`), derived at ingestion from destination domain vs the
  anchor's identity.
- T2-1 redirect ctx carries the seller (conversion subject) alongside the anchor
  (surface/host context). T2-2 closes conversions keyed by **seller**;
  the anchor becomes a separate attribution dimension. T2-3/W8 aggregate per seller.
- **Interim guard (ships first, independent):** until seller_ref lands, T2-2 refuses/flags
  closure with `seller_mismatch` when the converting store's identity ≠ the seed's
  destination seller — honest gap over silent misattribution, same philosophy as
  `click_matched`. Self-anchored seeds (the current pilot) are unaffected.

### D4 — Backfill the bucket now, via parity, never touching sigs

1. For each `external_seed`-owned catalog row and each seed: derive the brand identity
   (content_key already carries brand; domain from canonical/destination URL), mint/resolve
   the observed `catalog_merchants` row.
2. Dual-key window (the W1 RunFacts parity pattern): write `seller_ref` alongside the old
   `merchant_id`, log drift, cut consumers over, then re-key `merchant_id` on crawled rows.
   No big-bang.
3. **Constraints:** persisted `pivota_signature_id`/`pivota_canonical_url` values are
   frozen (write-once, publicly GSC-submitted); the ADR-008 fragmentation guard (always-on
   since W5 P2) is the collision tripwire; ADR-008 reconciliation handles brand-key merges
   — this ADR does not merge content identities, only re-subjects ownership.
4. **Timing:** before outcome data accumulates under wrong subjects. As of this ADR the
   converted-edge history under the bucket is ~zero.

## Prerequisite fix (independent correctness bug)

`_fetch_attached_seed_rows` must build storage-format keys via
`make_catalog_product_key(merchant, platform, pid)` (and prefix `prod::{merchant}::%`)
instead of pipe-format strings — the attached-ref offer lookup currently matches zero
prod rows (identity reference, Trap T1). This fix is valuable regardless of D1–D4 and
should ship first with a regression test pinning the storage format.

## Consequences

- Per-brand outcome/trust signal becomes buildable for crawled supply (the moat at crawl
  scale); a claiming brand inherits its full history.
- Cross-seller referral conversions become correctly attributable (and until then, are
  honestly refused rather than misattributed).
- Offer surfacing gains a uniform (product × seller) shape, which is also what a future
  buy-box/alternatives ranking needs.
- Costs: one seeds migration (seller_ref/seed_kind), an ingestion-time minting rule, the
  threading changes in T2-1/T2-2/T2-3, and the backfill. No sig churn, no content-identity
  merges, no downstream schema changes (subjects remain merchant_id-shaped).

## Resolved decisions (ratified 2026-07-07)

1. **The offer's product key is `product_group_id`, UNCONDITIONALLY.** The originally
   proposed "pg when present, content_key fallback" was rejected under the founder's
   no-fallback directive — a runtime fallback branch is itself the crutch pattern (two
   code paths, one of which rots). Instead the mainline is made total: where a product
   has no pg, ingestion/backfill mints a deterministic **singleton group**
   (`pg` derived from the product's `content_key`), so every offer keys on pg with zero
   branching. `content_key` remains a *derivation input*, never a runtime alternative.
2. **Observed-merchant id = deterministic `merch_obs_<hash(brand_identity::etld1)>`** —
   visibly distinct from tenant-created `merch_` ids, idempotent (same brand+domain →
   same identity forever), mintable by any ingestion path without coordination.
3. **Backfill starts now** (outcome history under the bucket is ~zero; W1-style parity
   window per D4) **and the 720 bare-format `attached_product_key` rows are repaired in
   the same pass** — re-derived to storage-format keys from seed_data/URLs. Rows that
   cannot be re-derived are flagged to a review queue, NOT silently left as a third
   format and NOT absorbed by a widened matcher (no-fallback: honest failure over a
   permanent format exception).
