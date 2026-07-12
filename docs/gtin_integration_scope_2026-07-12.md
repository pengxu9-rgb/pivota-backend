# GTIN Enrichment — Integration Scope (Fix Plan G — T3)

**Date:** 2026-07-12 · **Status:** SCOPE ONLY — zero data writes in this PR ·
**Standing decision this honors:** gtin-enrichment scope (2026-06-30) — *a wrong
GTIN is worse than none.*

## 1. Why a scope, not a backfill

A GTIN (UPC/EAN/GTIN-14) is the one globally-authoritative product identifier a
frontier agent can use to cross-reference a SKU against any other catalog. It is
therefore the highest-leverage "depth" signal after structured attributes — but
it is also the one where a **fabricated value is actively harmful**: a wrong GTIN
silently merges two different products or points an agent at the wrong item.
Unlike `llm_attributes` (which we can extract from the product's own copy and
ground), a GTIN cannot be inferred from text — it must come from an authoritative
name→GTIN source. So T3 produces an integration plan, not data.

## 2. Current state (prod-verified 2026-07-12)

- **Schema exists.** `catalog_products.gtin TEXT` (migration
  `178_catalog_products_gtin.sql`, self-healed in `db/schema_guard.py:1072`),
  partial index `idx_catalog_products_gtin WHERE gtin IS NOT NULL`. GTIN is a
  **match attribute, not key-material** — `content_key` stays brand+title-only;
  GTIN bridges legacy rows to new rows.
- **Coverage is ~empty.** The only internal source of a GTIN today is
  `catalog_skus.barcode`, harvested by `scripts/backfill_catalog_products_gtin.py`
  (real writes, guarded `WHERE gtin IS NULL`, GS1-canonicalized via
  `services.catalog_identity.normalize_gtin` / `pick_gtin13`). Per the ADR-011
  rollout handoff, running it "would populate ~1 row" — almost no `catalog_skus`
  carry a barcode. Go-forward, `scripts/mirror_external_seeds_to_catalog_products.py`
  stamps a GTIN when crawled `seed_data` carries one (`gtin/gtin13/gtin14/barcode/
  upc/ean`), and `services/intake_identity.py` has a Tier-0 GTIN matcher — but the
  crawl rarely captures a barcode.
- **Net:** the plumbing (column, index, canonicalizer, go-forward stampers) is
  built; the DATA is missing because no authoritative external lookup is wired.

## 3. The only trustworthy source: GS1

Consumer barcodes are issued by **GS1**. The authoritative name→GTIN /
GTIN→attributes lookup is **GS1 (US) Verified by GS1 / GS1 Data Hub** — the
brand-owner registry. Third-party barcode APIs (barcodelookup, UPCitemdb, etc.)
are **crowd-sourced and unreliable** — using them reintroduces exactly the
"wrong GTIN is worse than none" failure, so they are out of scope for a trust
layer.

### Credential needed
- A **GS1 US member account** (the brand owner's, or a licensed data-services
  agreement) with **Verified by GS1 / GS1 Data Hub API** access. This is a
  credentialed, contractual integration — not a public API key.
- Alternative: a **licensed GDSN data pool** (e.g. 1WorldSync) if broader
  attribute sync is wanted later. Heavier; not needed for GTIN-only.

### Cost (order of magnitude — confirm at contracting)
- GS1 US membership + Data Hub access: **annual licensing** (hundreds–low
  thousands USD/yr depending on prefix count / query tier), plus per-query or
  tiered-volume fees. Not a per-token cost like the LLM pass.
- This is a **fixed operational cost**, so it only pays off against a **narrow,
  high-value head-SKU target** — never the whole 9K catalog.

## 4. Head-SKU-only target (do NOT attempt the full catalog)

Scope the lookup to the **top-cited brands' head SKUs** — the products an agent is
actually asked about — so the fixed GS1 cost buys the highest citation value:

1. Rank brands by citation demand: `host_recurrence` + `citation_observations`
   (Channel-Graph), intersected with serving-eligible beauty products
   (`index_pipeline_state.serving_eligible IS TRUE`).
2. Take the **top ~200–500 head SKUs** (one canonical SKU per brand's hero
   products), preferring rows that already carry a resolved vertical +
   structural-depth `llm_attributes` (this PR's output) so GTIN completes an
   otherwise-rich record.
3. Resolve each via GS1 by **exact brand + product name (+ net content / size)** —
   the `volume` attribute this PR extracts is a useful disambiguator.

## 5. Write discipline (when it ships — NOT in this PR)

- Reuse `scripts/backfill_catalog_products_gtin.py`'s exact write shape: guarded
  `UPDATE ... SET gtin = :gtin WHERE product_key = :pk AND gtin IS NULL`
  (never overwrite), GS1-canonicalized via `normalize_gtin`, dry-run default.
- **Confidence gate:** only write a GTIN on an **exact** brand+name(+size) match
  from GS1. A fuzzy/ambiguous match writes NOTHING — honest emptiness beats a
  wrong identifier. Log the ambiguous cohort for manual review.
- Never fold GTIN into `content_key` (identity stays brand+title; GTIN is a match
  attribute only — the migration-178 contract).

## 6. Deliverable checklist for the implementation PR (future)

- [ ] GS1 US Data Hub credential provisioned (founder/BD — contractual).
- [ ] Head-SKU target list generated from citation demand (query in §4).
- [ ] `scripts/enrich_gtin_from_gs1.py` — GS1 client + exact-match gate + reuse of
      the guarded write + dry-run + per-SKU confidence log.
- [ ] Dry-run report: match rate, ambiguous rate, projected write count.
- [ ] Founder go/no-go on the GS1 contract, sized against the head-SKU match rate.

**No code or data changes for GTIN ship in this PR.**
