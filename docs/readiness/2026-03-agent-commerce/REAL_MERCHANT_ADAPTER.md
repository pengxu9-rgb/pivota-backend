# Real Merchant Adapter

## Target

- merchant: `merch_efbc46b4619cfbdf`
- platform: Shopify
- adapter: `readiness/sources/shopify_live.py`

## Resolution Order

### Shopify connection

1. `_get_shopify_config_for_merchant(merchant_id)`
2. `merchant_stores` primary store fallback
3. global Shopify env fallback already embedded in `_get_shopify_config_for_merchant`

### Catalog source

1. `products_cache` rows for `platform='shopify'`
2. live Shopify Admin product fetch via `ShopifyProductAdapter.fetch_products`

### PSP capability

1. latest active row in `merchant_psps`
2. no fallback execution path on the readiness alpha router

## Output

The adapter returns `MerchantSourceDataset` with:

- `merchant_alpha_mode=real_merchant_alpha`
- real merchant/product identity
- top-level `source_of_truth`
- `capability_status`
- merchant blockers/warnings
- merchant policy
- payment capability details
- normalized `StandardProduct` list
- per-product and per-variant diagnostics

## Current Limitation

- this workspace did not have a live `DATABASE_URL` on March 17, 2026
- adapter behavior was validated through captured fixtures and mocked DB/API dependencies
