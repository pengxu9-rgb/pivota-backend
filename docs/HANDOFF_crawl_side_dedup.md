# Handoff: crawl-side dedup for external-seed duplicate listings

## Problem
The `external_brand_crawl` seeder ingests merchant Shopify stores **one
`external_product_seeds` row per Shopify product ID**, with no dedup. When a
merchant has duplicate listings of the same product in their own admin (common —
Shopify's "Duplicate product" leaves `(Copy)`, `(Copy_T1)`, `(Convert_a)` titles
and `-1` / `-2` / `-copy` handles), every duplicate becomes its own active seed.
Because each junk title mints a **distinct `content_key`**
(`services/catalog_identity.py:make_content_key` = brand+title), the serving-side
near-dup collapse (PIVOTA-Agent #1738/#1739/#1740, keyed on `content_key`) cannot
merge them, and they leak into `find_products_multi` search/PLP.

**Concrete example already cleaned up:** Jumiso USA "20% NIACINAMIDE High Potency
Dark Spot Serum" existed as 21 active seeds (clean base handle + 20 duplicate
listings). See commit `3a477f51` /
`db/migrations/146_deactivate_jumiso_niacinamide_dup_seeds.sql`.

## What's already done (do NOT redo)
- **Serving fix (shipped):** PIVOTA-Agent #1738/#1739/#1740, flags
  `PIVOT_BEAUTY_TOKEN_RELEVANCE_RANK_ENABLED` +
  `PIVOT_BEAUTY_NEAR_DUP_COLLAPSE_ENABLED`. Collapses same-`content_key` dupes but
  not distinct-`content_key` ones.
- **Data cleanup (done, prod Postgres-xMr6):** the Jumiso cluster is collapsed to
  one canonical (`external_brand_crawl::jumiso_us_8485953503393`). Reversible;
  migration 146 is idempotent.
- This handoff is **only** about the recurrence source: the crawler will
  re-insert these on the next crawl of jumiso.us (each Shopify duplicate still has
  a distinct product ID → distinct seed → distinct `content_key`).

## Task
Find the `external_brand_crawl` writer and add dedup so duplicate listings of one
product don't each become a serving row.

> **CORRECTION (2026-08-07).** The "not in pivota-backend" verdict below is
> wrong and cost a later investigation time. The writer **is** in this repo:
> `scripts/onboard_external_brand_from_crawl.py`, which declares
> `TOOL = "external_brand_crawl"` and mints exactly those seed ids. The dedup
> this handoff asked for shipped there in 577a6c8f (#1247), and the
> ad-campaign-landing-page gate that dedup cannot cover shipped alongside it
> (PIVOTA-Agent#1926, `services/shopify_publication_signal.py`).

**Where to look — the writer is NOT in pivota-backend** (verified: no code writes
seed IDs of the form `external_brand_crawl::<merchant>_<shopify_product_id>`, and
`external_product_seeds` INSERTs in this repo are only the
`catalog_enrichment_agent_v1` tool + `seed_data_writer`). Check:
- The **PIVOTA-Agent** repo (owns the crawlers and
  `scripts/sync-external-seeds-to-catalog.cjs`).
- Any standalone crawler / ingest service that produces `external_brand_crawl::`
  IDs.
- Grep both repos for `external_brand_crawl`, the Shopify products-crawl
  entrypoint, and where `external_product_seeds` rows are inserted with a `tool`
  reflecting the brand crawl.

## Dedup strategies to evaluate (pick per findings)
1. **At-crawl collapse (preferred):** when a Shopify store yields multiple
   products with the same normalized brand+title (or same base handle stripped of
   `-N` / `-copy` / `(copy…)`), seed only the canonical (clean base handle,
   in-stock, most recent) and skip the rest. Reuse the "clean base handle vs
   `-1/-2/-copy`/`(Copy…)`" heuristic from the cleanup script.
2. **Title/handle junk filter:** never seed a listing whose title matches
   `\(\s*(copy|convert)` or whose handle ends in `-copy`, `-copy_*`,
   `-convert_*`, or `-\d+` when a suffix-free sibling exists. (Kills the obvious
   junk; strategy 1 also handles bare-title `-1/-2/-3` re-lists.)
3. **Post-crawl dedup pass:** a job that, per merchant + `content_key`-family,
   keeps one active seed and deactivates siblings — productizing
   `scripts/cleanup_niacinamide_test_variants.py` to run store-wide.

Prefer 1+2 (stop the rows at the source) over 3 (mop up after). Whatever you
choose, it must **also suppress the `catalog_products` mirror** — remember the
two-mirror gotcha: deactivating a seed does NOT clean its mirror (the mirror
never tombstones dropped seeds and the stale-catalog sweep excludes
`external_seed`). See `scripts/mirror_external_seeds_to_catalog_products.py` and
migrations 139 / 146.

## Acceptance criteria
- A re-crawl of jumiso.us does **not** re-create the 20 duplicate serving rows for
  this product.
- One canonical serves per real product; duplicate Shopify listings are collapsed
  (skipped at crawl, or deactivated + mirror-suppressed).
- Idempotent and dry-run-able; no hard-deletes.
- A test/fixture with a store containing `(Copy_T1)` / `(Convert_a)` / `-2`-handle
  duplicates asserts only the canonical is seeded.

## Useful prod probe (read-only, via Railway)
Project is linked to **Pivota Infra / production / Postgres-xMr6**. Connect with
asyncpg using `DATABASE_PUBLIC_URL` (the internal `DATABASE_URL` host isn't
reachable off Railway's network):

```
railway run --service Postgres-xMr6 -- bash -c 'DATABASE_URL="$DATABASE_PUBLIC_URL" python your_probe.py'
```

Find other polluted clusters store-wide:

```sql
SELECT split_part(id,'::',2) AS crawl_ns,
       regexp_replace(lower(title), '\s*\(\s*(copy|convert).*$', '') AS base_title,
       count(*) AS n,
       count(*) FILTER (WHERE title ~* '\(\s*(copy|convert)') AS junk
FROM external_product_seeds
WHERE id LIKE 'external_brand_crawl::%' AND status='active'
GROUP BY 1,2 HAVING count(*) > 1
ORDER BY n DESC LIMIT 50;
```

## Related
- Cleanup runner: `scripts/cleanup_niacinamide_test_variants.py`
- Read-only probe: `scripts/ops_niacinamide_dup_seed_probe.sql`
- Migration: `db/migrations/146_deactivate_jumiso_niacinamide_dup_seeds.sql`
- Precedent: `db/migrations/139_tombstone_cross_merchant_redundant_external_seed.sql`
- Project memory: `external-seed-mirror-cleanup-gotcha`
