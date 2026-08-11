# ADR-022: The merchant-promotions lane is deleted; any rebuild must follow checkout authority

**Status:** Accepted (2026-08-11)
**Decision owner:** peng
**Supersedes:** the manual promo-type gate from #1728 / PIVOTA-Agent #1954 (both now deleted along with the routes they guarded)

## Context

The promotions feature spanned five repos: DB-backed `promotions` (+ a write-only
`catalog_promotions` mirror), a Shopify discount sync (auto-triggered in the
background at quote time), an infra-side applier in `quote_service`, a
`store_discount_evidence` display lane attached to product cards and quote
payloads across ~10 serving routes, merchant/gateway authoring APIs, and three
authoring UIs (merchants-portal, creator-ui, agent-ui).

It was never used in production. Measured 2026-08-11 via the live API before
deletion: the prod `promotions` table held **exactly 17 rows, all
`PIVOTA_AUDIT_20260421A/B` Shopify-synced fixtures for one test merchant**
(`merch_efbc46b4619cfbdf`). Zero real merchants, zero real campaigns.

It was also structurally dishonest, in ways an audit chain surfaced one layer at
a time during 2026-08:

1. **The display/apply trapdoor.** The quote engine applied only
   `MULTI_BUY_DISCOUNT`; synced `FREE_SHIPPING` (and any manual `FLASH_SALE`)
   would display to shoppers and never change a price. #1728 gated manual
   creation — but the *sync* path bypassed the gate, so the live producer kept
   feeding the trapdoor.
2. **The gate guarded an unreachable door.** The gateway's create path always
   assigned an id and therefore issued `PATCH /agent/internal/promotions/<uuid>`;
   the backend gate was POST-only and its PATCH 404s on unknown ids. Gateway
   promo creation had been broken in `PROMOTIONS_MODE=remote` (prod) all along.
3. **Two pricing engines.** Applying discounts infra-side while Shopify applies
   its own at checkout is a permanent drift generator, and it multiplies per
   platform (Wix/WooCommerce are on the portal roadmap).
4. **Lane-conditional honesty.** Even the "working" `MULTI_BUY_DISCOUNT` was
   honest only on the platform-charged lane; on delegated/UCP checkout the
   merchant platform computes the total and the infra discount evaporates.

## Decision

Delete the lane end-to-end, everywhere:

- **backend:** promotions/sync/evidence services + routes, the `quote_service`
  applier and background quote-time sync, the order-path legacy discount, the
  `store_discount_evidence` field on quote/card models and all passthroughs
  (including `promotions_synced` on the incentives-reconcile response), the
  `preflight_shopify_discounts` script + its `agent-reliability-suite` workflow
  step, both tables (migration `125_drop_promotions_tables.sql`; migration 062
  tombstoned with IF EXISTS guards).
  `savings_presentation_service` is **kept**: it composes quote-truth inputs
  (Shopify `promotion_lines`, discount-code evidence, payment offers); its
  `store_discount_evidence` input is now always absent.
  `_parse_shopify_next_page_info` moved verbatim into
  `external_conversion_poller` (its one remaining consumer).
- **PIVOTA-Agent:** promotion store, `/api/merchant/promotions` routes, the
  #1954 gate, the deals-enrichment serving decoration.
- **merchants-portal / creator-ui:** authoring surfaces deleted.
- **agent-ui:** `/ops/promotions` becomes a **self-contained Partner Preview
  demo** (in-memory data, no network writes, clearly badged). It shows the
  *target* design below and doubles as its product spec. FLASH_SALE may appear
  there — in a sandbox it is a storyboard, not a lie.

Deletion pins live in `tests/test_tier2_prototype_deletions.py`
(`TestPromotionsLaneStaysDeleted`), same pattern as the payment-belt pins.

## The invariant any rebuild must satisfy

> **A promotion may be surfaced to an agent only if the system that computes
> that order's final charge enforces it.**

Enforcement authority follows checkout authority, per order. Concretely:

- **Don't build a second pricing engine.** The index stores *observed offer
  economics* with provenance (like prices), predicts at quote, and reconciles
  quoted-vs-charged after checkout. Enforcement lives with whoever runs the
  checkout.
- **AI-channel-exclusive deals** (the one genuinely differentiated product here,
  formerly the half-wired `expose_to_creators` fields) are built as
  **agent-applied discount codes materialized into Shopify** via API: the agent
  presents the code, Shopify enforces it, the merchant sees it in their own
  admin, sync observes it like any other discount. No parallel engine.
- **Display is evidence-gated:** a deal renders only if the quote layer marks it
  predictable for the lane the order will use.
- **Rebuild trigger:** a real partner/merchant commitment — the agent-ui demo
  exists to elicit exactly that. Not before.

## Recovery

Everything is recoverable at the pre-deletion commit (parent of the commit that
lands with this ADR). The 17 fixture rows were created by the deleted
`shopify_discount_fixture_service` (via the deleted sync preflight route) and
are recreatable from that same commit.

Deliberately NOT deleted, after review: `scripts/check_discount_order_canaries.py`
and its test. Despite the name it audits the SURVIVING quote-first Shopify
discount-code path (over-refund invariants, refund-webhook double-count on
external-PSP orders, missing order links) — invariants written after real paid
canary defects. Only name-adjacent, not lane-coupled.
