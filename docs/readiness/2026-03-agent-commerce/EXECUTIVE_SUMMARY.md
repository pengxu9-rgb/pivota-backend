# Executive Summary

## Verdict

Pivota is **not yet a real readiness infra for agent-native commerce**.

The live codebase has several meaningful building blocks:

- catalog extraction depth in `~/dev/Pivota-catalog-intelligence`
- a reusable normalized product shape in `models/standard_product.py`
- partial merchant catalog caching/sync in `routes/product_sync.py`, `routes/universal_product_sync.py`, and `db/products.py`
- partial UCP business-proxy code in `routes/ucp_business_proxy_routes.py`
- partial merchant-native Shopify order write-back intent in `routes/order_routes.py` and `services/shopify_transactions_service.py`

But the system still fails the readiness-infra bar because the current implementation is fragmented across repos, mixes cache with source-of-truth, lacks normalized reviews/confidence, lacks Google Merchant Center support, leaves UCP unmounted in `main.py`, and has brittle legacy checkout/order/webhook code paths with concrete defects.

## Why It Fails Today

1. There is no canonical readiness data model spanning catalog, offers, inventory, fulfillment policy, checkout capability, and order state.
2. Product data is mostly a cache layer (`products_cache`), not a durable normalized commerce source of truth.
3. Review ingestion and confidence scoring for commerce products are effectively absent. `PIVOTA-Agent` expects review summary fields, but the backend does not provide a corresponding normalized pipeline.
4. Google Merchant Center feed/export support is absent in live code.
5. UCP code exists, but it is not wired into the main FastAPI app today.
6. Checkout/payment/order flows are split across multiple stacks and contain brittle defects:
   - `routes/agent_api.py` calls `get_cached_products()` with the wrong signature.
   - `db/orders.py` performs runtime `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` on write failures.
   - `routes/order_routes.py` and `routes/webhook_routes.py` reference `store_info` in unsafe ways.
7. Merchant credentials are inconsistently handled. Encrypted connector credentials exist, but raw store tokens are still written into `merchant_stores.api_key`.

## Top Strengths

- Strong catalog extraction and market-awareness in `Pivota-catalog-intelligence`.
- A usable normalized product/variant model in `models/standard_product.py`.
- Real intent toward merchant-native commerce in Shopify order creation, refund handling, and transaction annotation.
- A real UCP codebase with signed offer/session concepts, request signing policy, and tests under `ucp/tests`.
- Enough existing components to prove feasibility with a narrow feature-flagged thin slice.

## Top Blockers

- No canonical readiness/source-of-truth layer.
- No normalized reviews/confidence service.
- No Google channel implementation.
- UCP routes exist but are not mounted in `main.py`.
- Checkout/payment/order paths are fragmented and partially admin-gated.
- Observability is table-heavy but not productized into merchant/SKU readiness diagnostics.

## Recommended Next Step

Use the implemented thin slice as the bootstrap path:

- synthetic merchant
- canonical readiness snapshot
- internal UCP-style export
- stubbed checkout session
- stubbed order-sync journal

Then replace the synthetic source with one real merchant adapter and converge on one source-of-truth for product freshness, checkout capability, and order state before attempting ChatGPT or Google channel claims.

