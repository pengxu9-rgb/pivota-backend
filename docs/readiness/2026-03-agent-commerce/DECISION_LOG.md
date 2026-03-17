# Decision Log

## 2026-03-17

### Chosen merchant/platform

- merchant: `merch_efbc46b4619cfbdf`
- platform: Shopify
- reason: this repo already has the strongest normalized catalog and merchant write-back primitives on the Shopify path

### Canonical alpha source path

- primary catalog source: normalized `products_cache` rows for Shopify
- fallback catalog source: live Shopify Admin product fetch
- reason: the cache already aligns to the repo’s normalized product model, while live fetch is the only practical fallback when cache is missing

### Canonical policy source

- source: additive merchant policy config in `readiness/fixtures/alpha_merchant_policies.json`
- reason: fulfillment/returns data is still absent as a normalized live source in the backend

### Canonical checkout path

- readiness endpoints remain the external alpha surface
- the readiness service now owns checkout session creation and order-sync orchestration
- reason: this avoids exposing fragmented legacy routes while still reusing local order persistence and Shopify primitives underneath

### Payment handling for alpha

- payment execution is capability-checked, not fully executed, on the new alpha path
- reason: local repo evidence supports PSP capability detection and existing legacy payment flows, but not a clean reusable live PSP execution abstraction for this phase

### Order status truth

- canonical outward truth: readiness order-sync journal/session state
- fallback: local `orders` row when journal detail is missing
- reason: the journal gives replay-safe external state, while `orders` remains the backing local system of record for created orders
