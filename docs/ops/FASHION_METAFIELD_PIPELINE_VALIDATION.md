# Fashion metafield pipeline — staging validation runbook

> ⚠️ **Production is GCP Cloud Run (`pivota-prod`, `us-west1`) since 2026-08-22. Railway is the
> ROLLBACK.** The `railway ...` commands below have NOT been rewritten — they were left as-is
> rather than translated by guesswork, because the procedures here were never re-verified against
> GCP. Running one changes the platform nobody is served from: the incident continues while the
> dial reads as turned. Translate with
> [operating_on_gcp_production.md](../runbooks/operating_on_gcp_production.md) before acting, or treat this
> document as a historical record of how the Railway rollout was done.


**Pipeline owner:** catalog enrichment (Phase O-5b)
**Last verified:** —

Validates end-to-end that a Shopify metafield set in a merchant's Admin UI flows through every layer of the pipeline and surfaces as authoritative `fashion_meta` on the gateway PDP response.

```
Shopify Admin metafield
  → adapters.product_adapters.ShopifyProductAdapter.fetch_products (PR #546)
  → product.platform_metadata.metafields
  → services.catalog_sync_service.ingest_standard_products (PR #545)
  → catalog_products.material/care/size_guide (+ source + confidence)
  → PIVOTA-Agent canonicalCatalogSearch SELECT (PR #1393)
  → product.fashion_meta (provenance-tagged shape)
  → pdpBuilder.pickFashionMeta confidence gate (PR #1391)
  → merchant-facing PDP renders the value
```

## Prerequisites

1. **PRs merged + deployed (both repos)**
   - `pivota-backend`: #540, #541, #542, #543, #544, #545, #546
   - `PIVOTA-Agent`: #1391, #1393
2. **Migration applied.** `schema_guard.ensure_required_schema_light` auto-applies mig 094 on startup; verify with:
   ```bash
   railway run -- node -e "..."  # see scripts/catalog_survey_TMP.js pattern
   # Expect: material, care, size_guide all listed
   ```
3. **Feature flag flipped in the deployed env.** Set on the **Pivota Infra → pivota-acp** service in Railway:
   ```
   SHOPIFY_METAFIELD_INGEST_ENABLED=true
   ```
   Restart / redeploy after setting.
4. **Test merchant + product.** Use `merch_efbc46b4619cfbdf` + `10064562225449` (PawStyle pet sweater) or any active Shopify-connected merchant product.

## Run

```bash
cd ~/dev/pivota-backend-quality-gate

# Default: sets a "VALIDATION_RUN: 100% test linen" metafield,
# syncs, queries catalog_products, hits the gateway PDP, then
# deletes the test metafield on exit.
./.venv/bin/python scripts/validate_fashion_metafield_pipeline.py \
    --merchant-id merch_efbc46b4619cfbdf \
    --product-id 10064562225449 \
    --material '100% test linen' \
    --gateway-base https://agent.pivota.cc
```

### Useful variants

```bash
# Validate care_instructions instead of material:
... --key care_instructions

# Leave the metafield in place for manual inspection:
... --no-cleanup

# Hit staging gateway instead of prod:
... --gateway-base https://agent-staging.pivota.cc
```

## Exit codes

| Code | Meaning |
|---|---|
| 0 | All 4 checks passed — pipeline is live |
| 1 | Pipeline broken — see per-step diagnostic in output |
| 2 | Setup error — credentials / network / args |

## Common failure modes

**`✗ EXPECTED value=… but found None`** at Step 3
- Most likely `SHOPIFY_METAFIELD_INGEST_ENABLED` is not actually set on the deployed env. The script's local env doesn't determine the sync behavior — it's the Railway/Docker env on the pivota-acp service.
- Or: the sync didn't pick up your specific product because nothing else changed. Add `--material` with a fresh suffix to bump the metafield value, OR run with `--limit=250` after manually editing the script.

**`✗ Gateway returned HTTP 502`** at Step 4
- gateway hasn't redeployed PRs #1391/#1393 yet — check the PIVOTA-Agent `web` service deploy timestamp.

**`✗ product.fashion_meta.material is missing from PDP response`** at Step 4
- The catalog_products row IS populated (Step 3 passed), but the gateway isn't surfacing it. Most likely the canonicalCatalogSearch SELECT change (#1393) hasn't landed — verify with `git log origin/main --oneline -- src/services/canonicalCatalogSearch.js` in the PIVOTA-Agent repo.

**`✗ EXPECTED source='merchant_payload' but found 'llm_extraction_v1'`** at Step 3
- The metafield-consumer extractor returned `None` (probably namespace or key mismatch) AND the LLM fallback ran. Check `services/fashion_field_payload_extractor.py:_MATERIAL_KEYS` for the namespace/key combos it accepts.

## Cleanup if the script crashed mid-run

The script auto-cleans the test metafield on exit (`finally:` block). If it was killed before that ran, look for `VALIDATION_RUN:` prefixed metafield values in Shopify Admin → Products → {product} → Metafields, and delete by hand.

## Cost note

Each run: ~3 Shopify API calls (list metafields, upsert, delete) + 1 full catalog sync of up to 5 products + 1 gateway PDP request. Total wall-clock is dominated by the sync (~5–10s). Safe to run repeatedly.
