# Meitu onboarding acceptance and bounded repair

This change repairs queue execution and provides a canary contract. It does not repair production by deployment alone, and the historical Meitu roster is input evidence only. The fixture matrix is `data/review_canaries/meitu_brand_retailer_matrix.json`; its five targets are pending until fresh measurements are supplied. Present stock, matching line/shade, currency and destination eligibility must be proven independently.

## Queue contract

Every curated job carries domain, brand, category_path, market (currently US only), source_role, retailer_name, only_vendors, require_currency, emit_real_variants, base_listings_only, max_products, max_scan_products, enrich_missing_inci and max_pdp_inci_fetches. Vendor-filtered jobs must explicitly choose `retailer` or `brand_official`. Defaults enable real variants and INCI recovery. Currency and complete discovery are mandatory at the unattended execution boundary. A missing field cannot bypass these checks on old queued payloads.

For a retailer, use `source_role=retailer`, a seller label distinct from the product maker, and the feed's exact vendor spelling. Work keys include normalized brand/vendor selection, effective US partition, seller/source role, expected currency, category and execution budgets. Two brands at one retailer therefore remain separate jobs; exact duplicates remain idempotent. A source label is also part of the key.

A failed or capped read never produces a partial ingest plan. The queue retains its existing bounded retry policy (three attempts by default); each retry re-reads from page one. `CrawlIncomplete.next_page` is diagnostic, **not a durable resume checkpoint**. Budget exhaustion requires reviewing/increasing a budget or narrowing the cohort before re-enqueue; retries alone cannot enlarge a cap. The batch's own completeness report is persisted with the successful job result, avoiding cross-job debug-state races.

Legacy pending rows still have domain-only keys. Before enabling new enqueues, inspect:

```sql
SELECT id, status, dedup_key, payload, source, attempts
FROM catalog_onboard_queue
WHERE kind='curated_brand' AND status IN ('pending','processing')
  AND dedup_key NOT LIKE 'curated:v2:%'
ORDER BY created_at;
```

Review their actual subset/role/currency intent. Do not automatically rewrite in-flight jobs. Drain, explicitly retire, or re-enqueue reviewed pending rows with the complete contract; otherwise old and new keys can schedule the same work concurrently. This release does not infer missing vendor filters from a brand label.

## Acceptance matrix and evidence

1. Use Flower Beauty and Stila US rows to verify merchant variant IDs and product-level lip categories. Choose currently sold exact lines/shades; the Meitu 2017-era line name is not proof that a listing still exists.
2. Use the live-proven A'PIEU Honey & Milk Lip Oil (5g), GTIN `8809530070499`, at asianbeautyessentials.com (variant `43603819692287`) and eyurs.com (variant `41807436316855`). Fresh public observations exhausted 677/434 products and selected 17/3 A'PIEU products, with native USD at both sellers. Complete extraction and distinct seller identities are proven in local plans. Post-deployment canonical attachment, search/PDP/offer visibility and second-ingest idempotence remain pending; matching brand alone is insufficient.
3. Use VELY VELY from the SG roster at cocomo.sg for a large retailer scan. Prove exhaustion beyond the initial pages, exact vendor selection, native SGD price and SG destination eligibility.
4. Use 3CE and a non-Shopify candidate retailer for the alternative extractor path. If the retailer cannot offer the requested currency/market/line, record unsupported/failed, then choose a reviewed replacement case. Do not convert a native price or relabel the serving market to force a pass.

The curated queue's current Path-C writer is US-partitioned. It deliberately rejects `market=SG`; that protects correctness but does **not** complete SG onboarding. For SG, use the existing explicit market-aware `scripts/onboard_external_brand_from_crawl.py` lane after reviewed extraction. Every extracted row must explicitly carry `market='SG'`, the freshly proven native `price_currency`, `offer_type='retailer'` for retailer observations, current destination URL and real variant data. Use `--extracted-at` to preserve the actual observation time.

For a reviewed exact-product SG cohort, inspect the existing lane without writes:

```sh
python -m scripts.onboard_external_brand_from_crawl --file reviewed-sg-cohort.json --extracted-at <actual-ISO-observation-time> --no-serving
```

No `--apply` is present. Keep its publication gate enabled. A future authorized controlled ingest should first use `--no-serving`, inspect all materialized identities/offers and price/market values, and separately establish serving eligibility before promotion. Do not feed a 2026-09-04 snapshot as if extracted today. The stored `cohort_meitu_sg_tier1.json` contains historical hints and incomplete image/description fields; it is not a ready-to-apply canary.

Record fresh deployed backend/gateway revisions and source artifact references with complete scan counts, exact product/variant/merchant keys, returned currencies and market. Capture actual search, PDP and offers responses, plus a controlled second-ingest product/SKU/offer key diff and identity failure list. The saved offers must identify each retailer, native variant, market, currency and actual retailer destination; a shared canonical product key alone does not prove that both offers resolve. The evidence schema is documented in `scripts/validate_meitu_canary_evidence.py`. Screen the resulting observations with:

```sh
python -m scripts.validate_meitu_canary_evidence --manifest data/review_canaries/meitu_brand_retailer_matrix.json --evidence fresh-observations.json --output acceptance.json
```

Omitting `--evidence` produces five pending cases and exit status 1. The evaluator checks saved evidence only; it does not perform network requests or attest to an artifact's truth. Keep and review the underlying raw responses. Acceptance requires all intended supported cases to pass; unsupported rows remain visible in the denominator.

## Existing crawl-seed recall scope

The #2169 writer fix did not move existing `tool='external_brand_crawl'` rows. First obtain a bounded exact-ID list via a read-only audit; no domain-wide inference or wildcard update is used by the repair tool. A useful discovery query is:

```sql
SELECT id, market, external_product_id, tool, status
FROM external_product_seeds
WHERE id LIKE 'external_brand_crawl::%' AND tool='external_brand_crawl'
  AND status='active'
ORDER BY id LIMIT 1000;
```

Put only reviewed exact IDs into a JSON array. Prepare a read-only plan:

```sh
python -m scripts.audit_crawl_seed_recall_scope --seed-ids-file reviewed-ids.json --output scope-plan.json
```

The plan records before values and blocks target-scope collisions, missing rows, missing market/external-product identity and non-active/non-crawl rows. Verify migration 044's valid unique index `idx_external_product_seeds_active_unique` on `(market, tool, external_product_id)` with its active/non-NULL predicate before any apply: row locks protect existing rows, while that index makes a concurrent target-scope insert fail the repair atomically. The exact post-precheck concurrent insert interleaving is covered by a real PostgreSQL regression. Blocked cohorts cannot apply; narrow and audit again. After the plan is reviewed and execution is authorized, a separate invocation can apply it:

```sh
python -m scripts.audit_crawl_seed_recall_scope --apply-plan scope-plan.json --output scope-result.json
```

Apply locks exact rows, compares every recorded before value, checks collisions again and changes only tool/updated_at. Any stale row or uniqueness failure rolls back the whole transaction. Keep both plan and result; before values are retained for a separately reviewed reversal. Re-audit the same IDs afterward and verify their intended agent scope through fresh recall. This procedure does not fix sibling-brand duplicate keys, category errors, missing variants or stale product content; those need separate scoped plans. No production repair was run as part of this change.
