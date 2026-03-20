# Executive Summary

## Verdict

Pivota is **not yet a real readiness infra for agent-native commerce**.

The live codebase has several meaningful building blocks:

- catalog extraction depth in `~/dev/Pivota-catalog-intelligence`
- a reusable normalized product shape in `models/standard_product.py`
- partial merchant catalog caching/sync in `routes/product_sync.py`, `routes/universal_product_sync.py`, and `db/products.py`
- partial UCP business-proxy code in `routes/ucp_business_proxy_routes.py`
- partial merchant-native Shopify order write-back intent in `routes/order_routes.py` and `services/shopify_transactions_service.py`

But the system still fails the readiness-infra bar because the current implementation is fragmented across repos, mixes cache with source-of-truth, still lacks Google Merchant Center support, and still relies on brittle legacy checkout/order/webhook code paths outside the readiness-owned alpha path. The stricter gap is no longer "reviews or payment do not exist"; it is that those platform capabilities are still only partially converged into one canonical readiness contract.

This summary is still strongest on `internal commerce` readiness. A unified scorecard now lives in `UNIFIED_READINESS_SCORECARD.md`, and its latest production update on `2026-03-20` includes a full multi-merchant external-referral audit. That scorecard is the correct top-level framing when discussing agent-compatible commerce surfaces instead of checkout-only readiness.

## Why It Fails Today

1. There is no canonical readiness data model spanning catalog, offers, inventory, fulfillment policy, checkout capability, and order state.
2. Product data is mostly a cache layer (`products_cache`), not a durable normalized commerce source of truth.
3. Reviews Center and confidence primitives exist, but readiness only recently began projecting product-level review summaries from `review_group` / `product_reviews`. Broader freshness, ranking, and full coverage are still incomplete.
4. Google Merchant Center feed/export support is absent in live code.
5. Readiness UCP routes now exist behind flags, but checkout/payment/order flows are still split across multiple stacks and contain brittle defects outside the alpha path:
   - `routes/agent_api.py` calls `get_cached_products()` with the wrong signature.
   - `db/orders.py` performs runtime `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` on write failures.
   - `routes/order_routes.py` and `routes/webhook_routes.py` reference `store_info` in unsafe ways.
6. Merchant credentials are inconsistently handled. Encrypted connector credentials exist, but raw store tokens are still written into `merchant_stores.api_key`.

## Top Strengths

- Strong catalog extraction and market-awareness in `Pivota-catalog-intelligence`.
- A usable normalized product/variant model in `models/standard_product.py`.
- Real intent toward merchant-native commerce in Shopify order creation, refund handling, and transaction annotation.
- A real UCP codebase with signed offer/session concepts, request signing policy, and tests under `ucp/tests`.
- A real Reviews Center with cross-merchant grouping, confidence, verified-purchase states, and buyer review flows.
- Real PSP routing / authorize / capture / refund infrastructure outside the readiness router.
- Enough existing components to prove feasibility with a narrow feature-flagged thin slice.

## Top Blockers

- No canonical readiness/source-of-truth layer.
- Reviews/confidence is now partially projected into readiness, but not yet fully normalized across freshness, ranking, and all merchants.
- No Google channel implementation.
- Checkout/payment/order execution is not yet fully converged onto one readiness-owned canonical PSP path.
- Legacy checkout/payment/order paths remain fragmented and partially admin-gated.
- Observability is table-heavy but not productized into merchant/SKU readiness diagnostics.
- External referral surfaces (`external_product_seeds`, tracked redirects, outbound affiliate offers) now have real employee-safe governance and runtime health on the anchor merchant, but fleet-wide coverage remains sparse.

## Recommended Next Step

Use the implemented thin slice as the bootstrap path:

- synthetic merchant
- canonical readiness snapshot
- internal UCP-style export
- stubbed checkout session
- stubbed order-sync journal

Then finish converging the real-merchant alpha path: keep the current one-merchant Shopify adapter, wire Reviews Center more deeply into readiness ranking/diagnostics, collapse payment execution onto one explicit readiness-owned contract, and expand referral inventory coverage before attempting broader cross-merchant external-referral claims.

## Related Architecture

- See `PRODUCT_OPTIMIZATION_BACKEND_ARCHITECTURE.md` for the backend design of the merchant-facing `Agent Commerce Readiness & Optimization Workspace`, including the funnel model, remediation object model, action APIs, and the boundary between deterministic execution and future LLM-assisted optimization.
- See `UNIFIED_READINESS_SCORECARD.md` for the latest dual-track audit covering `Internal Commerce` and `External Referral` as one unified readiness scorecard.
- See `MULTI_MERCHANT_EXTERNAL_REFERRAL_AUDIT.md` for the March 20, 2026 production audit showing that external-referral runtime is healthy where coverage exists, while merchant-fleet coverage remains the rollout bottleneck.
