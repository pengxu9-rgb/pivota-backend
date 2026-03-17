# Current State Inventory

## Architecture Map

### Live Repos Used In This Audit

1. `~/dev/pivota-acp-revert/pivota_infra_main`
   - effective backend used for this audit and thin-slice implementation
   - main app entrypoint: `main.py`
   - owns most real commerce infrastructure: normalized products, orders, PSP paths, fulfillment, refunds, platform imports, and the unmounted UCP business-proxy code
2. `~/dev/PIVOTA-Agent`
   - LLM/BFF gateway
   - main entrypoint: `src/server.js`
   - proxies or wraps backend commerce behavior but is not itself the readiness system of record
3. `~/dev/Pivota-catalog-intelligence`
   - strongest catalog extraction stack
   - main extraction logic: `server/src/services/extractors/extractV2.ts`
   - useful upstream source for readiness, but not integrated as canonical backend truth

### Important Repo Resolution Note

The requested `~/dev/pivota-backend` checkout was not present locally. This audit therefore used `~/dev/pivota-acp-revert/pivota_infra_main` as the live backend implementation surface.

## Score Summary

| Domain | Score | Summary |
| --- | --- | --- |
| A. Catalog readiness | 2/5 | partial normalization and sync, but mostly cache-shaped |
| B. Offer / price / inventory readiness | 2/5 | fields exist, freshness and truth are weak |
| C. Variant readiness | 3/5 | usable variant model and adapter coverage |
| D. Reviews / confidence signals | 1/5 | expectations exist, normalized ingestion does not |
| E. Fulfillment / policy readiness | 1/5 | tracking exists, policy normalization largely absent |
| F. Checkout / payment readiness | 2/5 | multiple partial stacks, not one reliable orchestration layer |
| G. Order write-back / state sync | 2/5 | Shopify write-back exists, sync/replay is brittle |
| H. Channel adapter readiness | 1/5 | UCP partial, ChatGPT feed absent, Google absent |
| I. Observability / diagnostics readiness | 1/5 | logs/tables exist, readiness diagnostics do not |
| J. Security / compliance / operational readiness | 2/5 | mixed secret handling; some encryption, some raw token storage |
| K. Modularization / skills readiness | 2/5 | enough seams exist, but platform boundaries are not clean |

## Source-Of-Truth Map

| Data Family | Current Reality | Evidence | Risk |
| --- | --- | --- | --- |
| Catalog/title/description/media | mostly `products_cache` plus realtime merchant API fallback | `db/products.py`, `services/product_query_service.py` | cache is treated like truth |
| Variants | embedded in `StandardProduct.variants` JSON payload | `models/standard_product.py`, `adapters/product_adapters.py` | no canonical variant table or bundle model |
| Price/currency | cached in `StandardProduct` and variant payloads | `models/standard_product.py`, `routes/product_sync.py` | Shopify adapter hardcodes USD in places; stale cache windows |
| Inventory | cached quantity plus occasional live API access | `db/products.py`, `routes/order_routes.py` | freshness undefined; fail-open inventory checks |
| Reviews | no normalized backend truth | `PIVOTA-Agent/src/server.js`, `PIVOTA-Agent/src/pdpBuilder.js` | gateway expects fields backend does not normalize |
| Fulfillment policy | mostly absent; fulfillment tracking derived from orders | `routes/fulfillment_api.py` | no shipping/returns contract |
| Payment capability | split across merchant onboarding, payment routing, payment execution, and PSP tables | `routes/payment_routes.py`, `routes/payment_execution_routes.py`, `db/payment_router.py` | no single canonical answer |
| Order state | `orders` plus `platform_orders` plus webhook tables | `db/orders.py`, `db/platform_orders.py`, `db/migrations/024_webhook_events.sql` | conflicting sources and runtime migrations |
| Channel export truth | absent before thin slice | audit result | no merchant/SKU readiness report or export layer |

## Domain Audit

### A. Catalog Readiness

**What exists today**

- reusable normalized schema in `models/standard_product.py`
- product sync from merchant platforms into `products_cache` in `routes/product_sync.py` and `routes/universal_product_sync.py`
- hybrid query path between cache and realtime in `services/product_query_service.py`
- extraction-grade catalog discovery in `Pivota-catalog-intelligence/server/src/services/extractors/extractV2.ts`

**Where it lives**

- `models/standard_product.py`
- `adapters/product_adapters.py`
- `routes/product_sync.py`
- `services/product_query_service.py`
- `db/products.py`
- `Pivota-catalog-intelligence/server/src/services/extractors/types.ts`

**What is missing**

- durable canonical normalized catalog store
- explicit taxonomy/category normalization layer
- robust brand/category/attribute completeness reporting
- canonical source-of-truth assignment for title/media/description

**Risk / brittleness**

- `products_cache` is a cache layer but is treated like working truth
- sync TTLs are long and mostly static
- merchant API fallback can silently change behavior per merchant

**Manual vs productized**

- merchant store connection and sync behavior still depend on operational setup and token hygiene

### B. Offer / Price / Inventory Readiness

**What exists today**

- price, compare-at price, currency, and inventory fields on products/variants
- cache metadata such as `cached_at`, `expires_at`, `ttl_seconds`
- limited live merchant querying

**Where it lives**

- `models/standard_product.py`
- `db/products.py`
- `services/product_query_service.py`
- `routes/order_routes.py`

**What is missing**

- field-level freshness guarantees
- clear polling/webhook update paths per merchant/platform
- source-of-truth ownership per field
- propagation-latency reporting

**Risk / brittleness**

- inventory checks in `routes/order_routes.py` are best-effort and fail open
- long cache windows undermine checkout confidence

**Manual vs productized**

- operator-managed sync is still central; platform-specific behavior is not abstracted cleanly

### C. Variant Readiness

**What exists today**

- parent product with embedded variants
- variant options, SKU, barcode, price, compare-at price, and image

**Where it lives**

- `models/standard_product.py`
- `adapters/product_adapters.py`
- `routes/agent_products.py`

**What is missing**

- canonical parent/child persistence model
- bundle/set semantics
- variant-selection rules for channel adapters

**Risk / brittleness**

- variant truth is trapped in JSON payloads
- no stable cross-channel variant identity policy beyond platform IDs

**Manual vs productized**

- variant correctness depends on upstream merchant data quality and adapter behavior

### D. Reviews / Confidence Signals

**What exists today**

- `PIVOTA-Agent` expects review summary and rating/count fields
- an internal proof issuer exists for buyer review verification tokens

**Where it lives**

- `PIVOTA-Agent/src/server.js`
- `PIVOTA-Agent/src/pdpBuilder.js`
- `proof_issuer_main.py`
- `routes/reviews_proof_issuer.py`

**What is missing**

- normalized review ingestion
- verified purchase linkage to product/SKU truth
- review freshness model
- ranking/explanation/confidence service

**Risk / brittleness**

- gateway expectations are ahead of backend capability
- proof issuer is only one narrow verification component, not a review readiness system

**Manual vs productized**

- effectively manual/nonexistent for commerce review normalization today

### E. Fulfillment / Policy Readiness

**What exists today**

- order tracking and fulfillment status API for agents
- refund routes that can annotate/cancel Shopify orders

**Where it lives**

- `routes/fulfillment_api.py`
- `routes/refund_api.py`
- `db/platform_orders.py`

**What is missing**

- normalized shipping options
- estimated delivery windows as structured data
- return/refund policy mapping
- merchant-of-record rules suitable for Google/UCP-style constraints

**Risk / brittleness**

- fulfillment APIs are mostly downstream order views, not policy readiness infrastructure

**Manual vs productized**

- policy data remains implicit or manual

### F. Checkout / Payment Readiness

**What exists today**

- legacy order + payment-intent flow in `routes/order_routes.py`
- alternative payment routing stack in `routes/payment_routes.py` and `orchestrator/payment_orchestrator.py`
- merchant-bound payment execution path in `routes/payment_execution_routes.py`
- UCP business-proxy checkout/session code exists in `routes/ucp_business_proxy_routes.py`

**What is missing**

- one canonical checkout orchestration stack
- non-admin checkout execution path suitable for production agent commerce
- consistent idempotency/retry model across PSPs
- explicit shipping/address/payment selection contract

**Risk / brittleness**

- `routes/order_routes.py` is admin-gated via `Depends(require_admin)`
- `db/orders.py` mutates schema at runtime on write failure
- payment routing has overlapping tables and governance models

**Manual vs productized**

- substantial operator knowledge is still required to know which payment path is “real”

### G. Order Write-Back / State Sync

**What exists today**

- local order records
- Shopify order creation after payment success
- webhook tables and partial webhook handlers
- platform order import sidecar cache

**Where it lives**

- `db/orders.py`
- `routes/order_routes.py`
- `routes/webhook_routes.py`
- `db/platform_orders.py`
- `db/migrations/024_webhook_events.sql`

**What is missing**

- canonical order-state machine
- reconciliation worker and replay tooling
- payment/refund/cancel sync contract across merchants/platforms
- eventual consistency handling by design instead of best effort

**Risk / brittleness**

- `routes/order_routes.py` references `store_info` in a nested task path that is easy to misuse
- `routes/webhook_routes.py` references `store_info` unsafely
- `platform_orders` logic appears to assume columns not shown in the defining table model

**Manual vs productized**

- strong dependency on manual debugging and admin endpoints

### H. Channel Adapter Readiness

**ChatGPT product feeds**

- absent in live backend code
- `PIVOTA-Agent` is an LLM/BFF surface and has docs/openapi material, but not a merchant readiness feed exporter

**ACP-style checkout**

- partial wrapper/docs exist in `PIVOTA-Agent`
- backend has ACP-adjacent code, but it is not a stable readiness layer

**Google Merchant Center / UCP-style Google flows**

- absent in live code; only conceptual mentions were found

**UCP**

- best partial implementation in `routes/ucp_business_proxy_routes.py`, `routes/ucp_checkout_ui_routes.py`, and `ucp/tests`
- not mounted in `main.py` before this thin slice

### I. Observability / Diagnostics Readiness

**What exists today**

- multiple log/event tables
- webhook event tables
- UCP tests for policy/signing behavior

**Where it lives**

- `db/migrations/002_production_tables.sql`
- `db/migrations/024_webhook_events.sql`
- `src/acp/outbox_queue.py`
- `ucp/tests`

**What is missing**

- merchant/channel/SKU readiness dashboards
- dead-letter/replay as a coherent system
- explanation of why a merchant or SKU is not discovery/checkout ready

**Risk / brittleness**

- outbox/replay behavior is partial and placeholder-heavy

### J. Security / Compliance / Operational Readiness

**What exists today**

- encrypted connector credential storage path
- internal-token gates in parts of the system
- Stripe/Shopify webhook verification logic

**Where it lives**

- `services/crypto_service.py`
- `db/connector_credentials.py`
- `routes/admin_connector_credentials.py`
- `routes/webhook_routes.py`

**What is missing**

- consistent credential handling for all merchant/store secrets
- strong environment isolation defaults
- audit-log-backed readiness operations

**Risk / brittleness**

- `routes/merchant_store_connections.py` writes raw platform credentials into `merchant_stores.api_key`
- `db/merchant_onboarding.py` includes `TODO: Encrypt this in production` for PSP sandbox keys
- `config/settings.py` has an unsafe default JWT secret

### K. Modularization / “Skills” Readiness

**What exists today**

- clear seams around product normalization, product querying, UCP, PSP routing, and platform imports

**What can be extracted**

- catalog normalization engine
- readiness scoring/reporting
- offer/inventory freshness service
- checkout orchestrator
- order-sync journal/reconciliation service
- channel exporter layer

**What is risky**

- modules are present, but cross-cutting source-of-truth rules are missing
- too many flows still depend on repo-specific operational knowledge

## Secondary Impact

### Discoverability

- strongest leverage comes from catalog normalization, media/title quality, variant coverage, and future review ingestion

### Execution Reliability

- currently limited by split checkout/payment/order paths and weak freshness guarantees

### Conversion Probability

- missing structured reviews, shipping windows, and reliable checkout capability all suppress conversion confidence

### Merchant / Channel Scalability

- the current architecture can support one-off merchants but not clean horizontal channel expansion yet

### Future GEO/AEO / Ads Upside

- catalog-intelligence diagnostics, merchant/SKU readiness scoring, and future review/confidence layers could support GEO/AEO and sponsored-demand tooling later, but those are not core capabilities today

