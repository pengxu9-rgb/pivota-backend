# Meitu onboarding acceptance and bounded repair

This change repairs queue execution and provides a canary contract. It does not repair production by deployment alone, and the historical Meitu roster is input evidence only. The fixture matrix is `data/review_canaries/meitu_brand_retailer_matrix.json`; its targets are pending until fresh measurements are supplied. Present stock, matching line/shade, currency and destination eligibility must be proven independently.

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
5. Use Pyunkang Yul Deep Clear Cleansing Balm, GTIN `08809486681497`, at eyurs.com and ohlolly.com to prove the two-retailer LANE while the lip cohort is blocked. This case is **not** Meitu lip coverage: it is a cleanser, and it declares `required_category_prefix: beauty/skincare/cleanse/` — the shelf both hosts' merchant product types actually resolve to — rather than being admitted under the lip shelf. It exists because case 2 is blocked twice over: asianbeautyessentials.com was first observed refusing TLS at 2026-09-15T04:39Z and was still refusing at 2026-09-16T01:19Z (site-side — it is unreachable from an ordinary browser outside our network too), and its lip oil publishes no INCI at eyurs.com, so no `beauty_sku_ingredients` row is written and `inci_source` cannot be a stored fact there. Measured 2026-09-16 on the crawl subnet: eyurs 434 products, ohlolly 510, both Shopify and native USD, the same barcode obtained at each host via the lane's own GTIN path, and an INCI list published at both (868/869 chars). Of ten probed US retailers only these three serve `/products.json` at all, they share 12 normalised titles across ~1,500 products, and none of the 12 has a lip product_type — title-level intersection is discovery only, never identity. Merchant variant IDs were recorded from the 2026-09-16 dry-runs into `observed_source_variants`. Run it with `--only-gtin 8809486681497`: the vendor cohort at each host also contains products whose merchant `product_type` this taxonomy does not map (plural forms such as `Cleansers`/`Moisturizers`, plus `Cotton Pads`, `Hair Conditioner`, `Exfoliator`, `Accessories`), which land `category_unresolved` and make apply refuse the whole cohort. Do NOT alias those plurals to their singulars to unblock it: eyurs.com labels its **Essence Toner** `product_type: Cleansers`, so the alias would file a toner as a cleanser with confidence — converting an honest null into a wrong answer. Those unmapped types are a real retailer-lane taxonomy gap that the blocked status was correctly reporting; narrowing the cohort sets it aside for this case, it does not fix it.

`--only-gtin` narrows the **plan**, not the crawl: the whole feed is still read and GTIN recovery still spends its `--max-pdp-identity-fetches` budget on products the filter later discards, so a budget too small to recover the target's barcode makes the filter refuse a product that is actually there. It matches the PDP's own barcode or any merchant variant's — but a multi-variant product surfaces its barcodes only under `--emit-real-variants`/`--fold-shades`, and without those its record carries no GTIN and the filter reports it as not found — every requested GTIN must be valid and must match somewhere in the run, and a host matching none of them fails the run. Passing this case therefore proves the lane for **one product per host**, not for a vendor cohort. Passing this case proves the lane converges and serves; it says nothing about lip coverage, and it does not by itself certify ingredient authority — the validator compares the declared `inci_source`, it does not read `beauty_sku_ingredients`.

A case's `required_category_prefix` **defaults to the lip shelf** when absent, and a declared value must be a real taxonomy shelf (`LEAF_PARENTS`, e.g. `beauty/makeup/lip/`, `beauty/skincare/cleanse/`); `beauty/` or an ancestor like `beauty/skincare/` is refused, because an unbounded prefix would switch the check off as surely as a blank one. The Meitu cohort is lip-only, so a case that forgets to declare a category must not thereby accept any category; a blank declaration is treated as absent rather than as "accept everything". A case covering another shelf declares it and is then held to that shelf just as strictly.

The curated queue's current Path-C writer is US-partitioned. It deliberately rejects `market=SG`; that protects correctness but does **not** complete SG onboarding. For SG, use the existing explicit market-aware `scripts/onboard_external_brand_from_crawl.py` lane after reviewed extraction. Every extracted row must explicitly carry `market='SG'`, the freshly proven native `price_currency`, `offer_type='retailer'` for retailer observations, current destination URL and real variant data. Use `--extracted-at` to preserve the actual observation time.

For a reviewed exact-product SG cohort, inspect the existing lane without writes:

```sh
python -m scripts.onboard_external_brand_from_crawl --file reviewed-sg-cohort.json --extracted-at <actual-ISO-observation-time> --no-serving
```

No `--apply` is present. Keep its publication gate enabled. A future authorized controlled ingest should first use `--no-serving`, inspect all materialized identities/offers and price/market values, and separately establish serving eligibility before promotion. Do not feed a 2026-09-04 snapshot as if extracted today. The stored `cohort_meitu_sg_tier1.json` contains historical hints and incomplete image/description fields; it is not a ready-to-apply canary.

Record fresh deployed backend/gateway revisions and source artifact references with complete scan counts, exact product/variant/merchant keys, returned currencies and market. Capture actual search, PDP and offers responses, plus a controlled second-ingest product/SKU/offer key diff and identity failure list. The saved offers must identify each retailer, native variant, market, currency and actual retailer destination; a shared canonical product key alone does not prove that both offers resolve. The evidence schema is documented in `scripts/validate_meitu_canary_evidence.py`. The database-backed half of that file is produced by `scripts/collect_curated_canary_evidence.py`, not typed:

```sh
IMAGE=us-west1-docker.pkg.dev/pivota-shared/pivota/backend:<sha> \
  scripts/ops/run_oneoff_job.sh -m scripts.collect_curated_canary_evidence \
  --manifest data/review_canaries/meitu_brand_retailer_matrix.json --case-id <case>
```

It prints the JSON to **stdout** (the one-off runner deletes the job on every exit path, so a file written inside the container is gone), and prints `NOTE`/`DIGEST`/`SUMMARY` lines to stderr. The digest covers the database-backed subset: compare it against the file you were handed. That is a tamper *check*, not attestation — provenance fields are strings a person can type, and this makes an edit detectable by a reviewer who looks, nothing more. The job refuses to emit at all when it cannot resolve the image's commit sha, rather than producing a file the validator rejects for a reason that looks like a data problem.

It reads products, SKUs (variant id and its provenance, which live inside `sku_payload`), offers and the `beauty_sku_ingredients` row, and stamps `evidence_provenance`. A value it cannot read is emitted as `null` with a reason, never as a plausible default — a default is indistinguishable from a measurement once it is in the JSON. The live surfaces (`search_product_keys`, `pdp_product_keys`, `offer_product_keys`), the crawl report, the second-ingest diffs and `identity_failures` are **not collected**: they are door responses and job outputs, not rows. They stay `null`, which FAILS validation until the actual probes are run and merged — `null` means never asked, `[]` means asked and empty, and collapsing the two is how a never-measured surface reads as a measured result.

Each product must also carry `inci_row {present, source_system, raw_inci_chars}`, and the declared `inci_source` must equal the stored `source_system`. The curated mapper stamps `inci_source` on every retailer record while the row is written only when the seller published ingredients, so without this a product whose PDP carries no INCI passed the authority check. Measured 2026-09-16: the A'PIEU Honey & Milk Lip Oil at eyurs.com is exactly that product, which is why case 2 cannot pass on its current target even once asianbeautyessentials.com answers again. Screen the resulting observations with:

```sh
python -m scripts.validate_meitu_canary_evidence --manifest data/review_canaries/meitu_brand_retailer_matrix.json --evidence fresh-observations.json --output acceptance.json
```

Omitting `--evidence` produces one pending case per manifest row and exit status 1. The evaluator checks saved evidence only; it does not perform network requests or attest to an artifact's truth. Keep and review the underlying raw responses. Acceptance requires all intended supported cases to pass; unsupported rows remain visible in the denominator.

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

## Optional GTIN recovery and canonical attachment

The final live canary found that both retailer bulk feeds omit barcodes while their product `.js` responses expose them. Opt in with `enrich_missing_gtin=true` / `--enrich-missing-gtin` and `max_pdp_identity_fetches` / `--max-pdp-identity-fetches` (default 100). Recovery is disabled by default, runs after vendor selection and before shade folding, and only fills missing barcodes after exact product, handle, vendor and native-variant checks. Same-storefront redirects are bounded; prices, titles and existing identifiers are preserved. The batch's `gtin_recovery` report distinguishes attempted/recovered/failed/capped products and actual detail requests.

A read-only canary command is:

```sh
python -m scripts.onboard_curated_brands --domain asianbeautyessentials.com --category beauty --brand "A'PIEU" --only-vendor "A'PIEU" --source-role retailer --require-currency USD --emit-real-variants --enrich-missing-gtin --max-pdp-identity-fetches 100 --max-products 100 --max-scan-products 1500
```

There is no `--apply`. Repeat for `eyurs.com` and review the plans. The final observations recovered valid GTINs for all 17 and 3 selected products, respectively, and both lip-oil PDP plan rows retained `08809530070499`. Their mapped variants were unavailable; this demonstrates identity observation and plan construction, not a purchasable live offer. Choose a currently available shared item for purchase-eligible serving acceptance.

The existing `ENABLE_INTAKE_IDENTITY_ENRICHMENT` flag defaults off. Inspect the effective deployment setting and its rollout prerequisites; this release does not flip it. Use the sequential per-row apply path for the fresh two-retailer acceptance case. The batch executor resolves its new rows before inserting, so fresh cross-title rows cannot discover one another within that same batch. Existing curated product-group memberships are preserved; deploying the mapper does not merge old split groups.

Evidence must distinguish retailer listing `product_key` from the actual shared `content_key` and `product_group_id`. When the buyer-facing lookup key differs, record `canonical_product_key` for search/PDP/offer-surface observations. Seller-specific offer tuples retain the listing key and native variant. Matching GTINs or a mocked identity-gate test alone do not prove production group convergence. Review any GTIN/title-drift flags and verify actual membership and seller offers after the controlled ingest.


## Primary ingestion contract

Unproven currency fails direct JSONL planning and curated enumeration, including official mode without `require_currency`. Currency must be an explicit three-letter observation; there is no USD default or conversion. This validates shape and presence, not membership in an ISO registry.

A caller's broad category shelf stays in `category_input_path` for review. If product evidence cannot resolve a supported leaf, curated mapping emits `category_path=null` and `category_resolution_status=unresolved`; the planned PDP remains draft. Queue and CLI apply refuse the unresolved cohort before database writes. Dry-run plans report blocked categories explicitly. No serving flags or existing production rows are changed.

Queue and CLI apply compare actual PDP/SKU/offer counts with the reviewed plan. Missing PDPs/offers, zero usable SKUs, or unexplained lost SKU rows are partial failure, with counts retained in the queue error. Only explicitly counted natural-key SKU deduplication explains a reduced SKU count. Some rows may already have committed when a write error occurs; the job retries its stable natural keys rather than claiming completion.

A retailer apply refuses (`retailer_listing_migration_required`, before any write) when an older catalog row whose key is not `ext:retailer:` owns a planned listing URL, suppressed or not. A dry run only sees this with `--check-legacy-listings`: it runs one SELECT on `catalog_products` over the same finder and SQL the apply uses, prints `legacy listings: {...}` (per-listing legacy owners, `conflict_count`, `suppressed_conflict_count`, and the exact `apply_refusal`), and exits 2 on a conflict or when the check cannot run (a non-Postgres `DATABASE_URL` included). Without the flag the line says `unchecked`, so `ready_to_apply` on the `primary ingestion:` line is the plan verdict only. Run the flagged dry run as the same in-VPC one-off job as the apply (crawls only with `SUBNET=pivota-crawl`), never from a laptop. Wave 1 (2026-09-18) read `ready_to_apply` everywhere, then the ohlolly.com apply was refused on `prod::external_seed::external_seed::ext_bf55156550aa86a7eb921ff2`.

GTIN remains optional: a real product without a barcode is not rejected solely for that absence. Optional detail recovery and successful writes do not prove shared identity or search. Reports keep `primary_search_verified=false` and `shared_identity_verified=false`; acceptance requires a primary query with supplemental lanes disabled and each expected seller/native-variant/destination tuple present.


Primary feed replay on 2026-09-12 (optional GTIN recovery disabled) read AsianBeautyEssentials 677 products across 3 pages, selected all 17 A'PIEU products, and planned 17 PDPs / 34 SKUs / 34 offers with zero unresolved categories. Eyurs read 434 products across 2 pages, selected 3, and planned 3 PDPs / 6 SKUs / 6 offers. These are source-read and pure-plan outcomes, not production ingestion or search proof.

The last four category cases were resolved from exact merchant product types: `Lip Scrub` maps to the existing lip-care leaf `beauty/makeup/lip/balm` (the taxonomy already includes lip scrubs there), `Sun Protection` maps to sunscreen, and `Foot Care` maps to existing `beauty/body/care`. The original product type is preserved as `category_source_product_type` in record and persisted enrichment metadata. `Footwear`, `Foot Care Shoes`, `Sun Protection Accessories`, and mixed `Lip Scrub & Cleanser` remain unresolved; no broad title classifier was introduced.
