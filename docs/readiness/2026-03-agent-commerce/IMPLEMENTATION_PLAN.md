# Implementation Plan

## P0

- Freeze the canonical readiness contract implemented in this thin slice:
  - `readiness/models.py`
  - `readiness/scoring.py`
  - `readiness/channel_exports/ucp.py`
  - `routes/readiness_internal.py`
- Replace the synthetic merchant source with one real merchant adapter while keeping the same report/export/checkout/order-sync interfaces.
- Define one source-of-truth contract for:
  - catalog/title/description/media
  - price/currency
  - inventory/availability
  - fulfillment policy
  - checkout capability
  - order status
- Collapse payment/order orchestration onto one path instead of mixing `order_routes.py`, `payment_routes.py`, `payment_execution_routes.py`, and the dual routing stack.
- Fix the concrete brittle defects uncovered in the audit before broad rollout.

## P1

- Mount and harden the existing UCP business proxy path in `main.py`.
- Replace stubbed checkout execution with a merchant-bound PSP abstraction.
- Normalize fulfillment/shipping/returns policy data into the readiness layer.
- Add real webhook verification, replay, and reconciliation behavior.
- Implement readiness diagnostics for a real merchant and expose them to internal ops tooling.

## P2

- Build Google Merchant Center feed generation and validation.
- Add ChatGPT/ACP product-feed and checkout compatibility adapters.
- Add normalized product reviews/confidence ingestion.
- Add merchant/channel dashboards with readiness trends, blocker counts, and freshness burn-down.

## Sequencing And Dependencies

1. Keep the thin-slice contracts stable.
2. Land one real merchant adapter behind feature flags.
3. Unify source-of-truth definitions.
4. Harden checkout + order-sync.
5. Add channel-specific exports and validators.
6. Add review/confidence and broader merchant coverage.

## Module / Skillization Plan

### Module First

- readiness scoring/reporting
- UCP export mapper
- provenance/freshness evaluator

### Service Next

- merchant adapter service
- checkout orchestrator
- order state sync service

### Worker Next

- inventory/offer refresh
- channel export scheduler
- webhook replay

### Internal Skills Later

- readiness audit
- channel export dry-run validation
- merchant onboarding validation
- order-sync replay analysis

