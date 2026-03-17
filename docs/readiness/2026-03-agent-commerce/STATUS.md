# Status

As of March 17, 2026, the one-merchant readiness alpha is implemented for `merch_efbc46b4619cfbdf` behind feature flags.

Implemented:

- real Shopify-backed readiness source for one merchant
- explicit source-of-truth contract for six field families
- canonical readiness-owned checkout/order-sync path
- machine-readable report/export/checkout/order-sync responses with blockers, warnings, provenance, and freshness
- synthetic regression path preserved
- captured real-merchant fixtures and regression tests

Validated:

- `python3 -m py_compile` on the readiness modules and router
- `python3 -m pytest readiness/tests -q`
- result: `10 passed`

Not live-validated in this checkout:

- no local `.env`
- no local `DATABASE_URL`
- no local live merchant credential verification from this workspace

Major remaining risks:

- merchant-native payment execution is still capability-checked, not executed, on the alpha path
- merchant fulfillment/returns policy is still manual config, not live-ingested
- real Shopify order write-back was tested through mocked responses and captured fixtures, not a live local merchant call from this checkout
