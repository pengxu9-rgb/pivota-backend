# Gap Matrix

## Maturity Scorecard

| Capability Area | Current Support | Score | Notes |
| --- | --- | --- | --- |
| Merchant data readiness | Partial normalized product cache only | 2/5 | `StandardProduct` exists, but `products_cache` is still cache-shaped and freshness is weak |
| ChatGPT readiness | Gateway/docs only, no real product-feed adapter | 1/5 | `PIVOTA-Agent` is an LLM/BFF surface, not a readiness backend |
| Google readiness | Absent | 0/5 | no live GMC feed/export/sync code found |
| Internal orchestration readiness | Fragmented | 2/5 | multiple overlapping payment/order/routing paths, no canonical readiness model |

## Capability Matrix

| Target Capability | Current State | Missing Pieces | Priority |
| --- | --- | --- | --- |
| Normalized catalog | `models/standard_product.py`, `product_sync`, `products_cache` | durable canonical catalog tables, freshness rules, explicit source-of-truth mapping | P0 |
| Normalized variants | Variant model exists and adapters populate it | parent/child normalization policy, bundle/set model, canonical variant IDs | P0 |
| Normalized reviews/confidence | Agent-side expectations only; proof issuer exists separately | ingestion, freshness, verified-purchase linkage, ranking/confidence model | P1 |
| Fulfillment/policy mapping | Agent fulfillment tracking exists, not policy normalization | shipping options, returns schema, merchant-of-record rules, channel mapping | P1 |
| Price/inventory freshness | cached fields exist | live freshness SLAs, polling/webhooks, field-level provenance | P0 |
| ChatGPT product-feed generation | absent | feed contract, field coverage, validation tooling | P2 |
| ACP-compatible checkout layer | partial docs and gateway wrappers | canonical checkout contract, non-admin execution, merchant write-back hardening | P1 |
| Merchant-native order/payment handling | partial Shopify write-back and PSP code | one canonical orchestration stack, idempotency, retries, reconciliation | P0 |
| GMC-compatible feed generation | absent | feed schema, policy mapping, validation, export scheduling | P2 |
| UCP-compatible checkout/order layer | partial UCP module exists, not mounted | activation, webhook verification, real merchant binding, non-stubbed payment path | P1 |
| Readiness diagnostics per merchant/SKU | absent as a product surface | scoring, blocker taxonomy, source-of-truth map, machine-readable report | P0 |
| Observability/replay | partial tables and outbox placeholders | dashboards, DLQ/replay, merchant/channel diagnostics | P1 |

## Build vs Buy

### Build In-House

- canonical readiness model
- field-provenance and source-of-truth mapping
- readiness scoring and blocker taxonomy
- merchant adapter contract
- channel-export contract layer
- order-state journal/reconciliation model

### Use Managed/Existing Components

- PSP SDKs and hosted payment primitives for Stripe/Adyen
- merchant-platform SDKs where they reduce API drift
- managed telemetry/APM instead of bespoke tracing
- third-party Google feed validation tools before building a full validator

## Modularization Recommendations

### Reusable Modules

- `catalog normalization engine`
- `readiness scoring/reporting`
- `field provenance + freshness evaluator`
- `channel export mappers`

### Services

- `merchant adapter service`
- `reviews/confidence service`
- `checkout orchestration service`
- `order state sync service`

### Workers/Jobs

- `offer/inventory freshness refresh`
- `merchant order reconciliation`
- `channel export scheduler`
- `webhook replay / DLQ processor`

### Pluggable Adapters

- `merchant platform adapters` for Shopify/Wix/Amazon/etc.
- `channel adapters` for UCP, ACP/ChatGPT, GMC
- `PSP adapters`

### Future Internal Skills

- readiness audit per merchant
- channel export validation
- merchant onboarding dry-run
- order-sync replay / reconciliation triage

