# Merchant Onboarding Readiness Audit

**Goal.** Before pushing for agent onboarding (which is what unblocks Stage 1's "≥5 stamped edges from live agent traffic" gate), prove every system a merchant touches works end-to-end. The Stage 0/1/2/3/4 deployment sequence assumes traffic flows; this audit doesn't.

**Method.** Enumerate every integration point in the merchant onboarding journey, classify current verification state, identify the concrete pass/fail criteria, and define a shakeout plan in staging (Stripe Test mode, since prod is now on Stripe Live keys).

**Out of scope.** Pre-v1.3 surfaces (agent discovery feed, catalog search ranking, product detail page rendering) — these are well-tested independently. This audit is monetization-specific.

## 0. Current environment state (2026-05-23)

| Surface | State |
|---|---|
| Production (`api.pivota.cc`) | Commit `770b6ed` (latest main), `db_ok=true`, 13 scheduler jobs registered (T6 active, T5 reaper active, T9 reaper active, T7/T8 paused) |
| Staging (`web-staging-staging-5257.up.railway.app`) | Redeployed 2026-05-23 to `770b6ed`. Stripe Test keys (`sk_test_*`). `STRIPE_BILLING_WEBHOOK_SECRET` provisioned via `we_1Ta1gd...` Test endpoint. Single Postgres shared with prod per `project_pivota_infra_single_db` |
| Stripe Test mode | 3 Test Prices exist (starter/growth/scale), webhook endpoint subscribes to 6 events (checkout.session.completed, customer.subscription.{updated,deleted,created}, invoice.{paid,payment_failed}) |
| Stripe Live mode | 3 Live Prices rotated 2026-05-22; webhook endpoint TBD (Live billing webhook secret + endpoint not yet provisioned — Stage 3 prerequisite) |

## 1. The merchant onboarding journey — system by system

The path a new merchant takes from "interested" to "earning attributed GMV":

```
A. signup → B. webhook mirror → C. catalog connect → D. agent attribution →
E. order paid → F. T9 stamp → G. T6 rollup → H. (paused) T7 invoice →
I. (paused) T8 settlement
```

Plus the orthogonal: **refund attribution** (J), which can fire at any time after E.

### A. Subscription signup (T4)

**What happens.** Merchant hits `POST /api/billing/checkout-session` with an authenticated merchant API key + `{price_id, success_url, cancel_url}`. Code at `routes/billing_routes.py:create_billing_checkout_session` calls `stripe_client.v1.customers.create` (if no existing Stripe customer for this merchant) and `stripe_client.v1.checkout.sessions.create`. Returns the Stripe checkout URL the merchant browser hits.

**Current verification.**
- Unit tests: zero for this endpoint
- Integration tests: zero
- Idempotency keys: added in PR #600 (`merchant_customer:{merchant_id}` and `checkout_session:{merchant_id}:{price_id}:{date_iso}`) — never exercised against real Stripe API
- E2E against Live: zero (the Live key rotation 2026-05-22 was config-only; nobody's signed up since)

**Pass criteria.**
- Authenticated merchant → checkout URL returned in <3s
- Stripe customer created with deterministic idempotency key
- Replay of the same request inside 24h returns the same checkout URL (Stripe cache hit)
- `merchants.stripe_customer_id` populated post-success
- Browser-side completion of the Stripe Checkout → `checkout.session.completed` webhook fires

**Verification plan (staging).** POST against staging with a Test merchant API key; assert the response shape, confirm Stripe customer exists in Stripe Test Dashboard, replay the request, confirm idempotent.

### B. Stripe webhook mirror (T4)

**What happens.** Stripe sends 6 event types to `/webhooks/stripe/billing`. Handler at `routes/billing_routes.py:handle_stripe_billing_webhook` verifies signature, inserts to `stripe_events` ledger (`status='pending'`), dispatches to per-event handler, marks `processed`/`ignored`/`failed`.

**Current verification.**
- Unit tests: 7 in `tests/test_billing_webhook_retry_semantics.py` covering retry semantics (PR #599)
- Clock harness exercises `checkout.session.completed` against Stripe Test mode — blocked from prod now (PR #604)
- Reclaim path (`_claim_retryable_event`) for failed/stale-pending events: never exercised against real Stripe retry behavior

**Pass criteria.**
- 6 event types each fire the right per-event handler
- Replay of an already-processed event → 200 + `status='duplicate'`
- Reprocess of a `failed` event → row transitions `failed → pending → processed`
- Concurrent delivery of the same event → first wins, second gets 409
- Invalid signature → 400, no row written

**Verification plan (staging).** Use `stripe trigger` CLI or the webhook test endpoint in the Test Dashboard to fire each event type at staging; tail logs + `stripe_events` table.

### C. Catalog sync (pre-v1.3, lightly touched)

**What happens.** Merchant connects their Shopify/Wix store. Adapter at `services/catalog_sync_service.py:ingest_standard_products` (Shopify path A) writes `catalog_products` + `catalog_offers`.

**Current verification.**
- Existing Shopify + Wix integration tests pass (per `project_wix_platform_ready` memory)
- No v1.3 changes to catalog code; presumed stable

**Pass criteria.** Out of scope for this audit — covered by pre-v1.3 quality gates.

### D. Agent attribution metadata flow

**What happens.** Agent presents Pivota product → user clicks → `pvt_click_id`, `pvt_product_id`, `pvt_variant_id`, `pvt_surface` ride in the URL → merchant storefront receives them → at order create time the storefront calls Pivota's order endpoint with attribution metadata → `services/commerce_attribution_service.upsert_order_attribution_edge` writes a row to `commerce_attribution_edges`.

**Current verification.**
- `has_attribution_signal()` gate: covered by 16 attribution tests
- Silent-reject observability: PR #594 adds a Prometheus counter + WARN log
- E2E: 7 edges in prod from a prior session (Mar 30-31, `surface='ucp'`) — those work
- Direct Shopify checkout cohort: 90/98 alpha orders have `agent_id` on orders.row but no edge (the gap codex finding #8 surfaced)

**Pass criteria.**
- Order POST with full attribution metadata → edge written with all dimensions
- Order POST with empty metadata → `commerce_attribution_silent_reject_total` counter increments + WARN log emitted, no edge
- Multi-edge fan-out: one order with N surface_click_events → N edges with same gross stamp post-T9

**Verification plan (staging).** Drive synthetic POST /order requests at staging with rich metadata. Verify edges + counter ticks. Drive with empty metadata, verify the silent reject path.

### E. Order paid → F. T9 stamping (`services/psp_payment_finalizer.py`)

**What happens.** PSP (Stripe) signals payment success → `psp_payment_finalizer:227-238` calls `stamp_gross_attributed_gmv(order_id, subtotal=, discount_total=)`. Stamps `gross_attributed_gmv_cents = (subtotal - discount) * 100` on every edge for that order_id (multi-edge fan-out per T9 acceptance).

**Current verification.**
- 7 attribution_stamping tests pass
- Reaper (PR #597 + hotfix #598) live on prod, idle (0 candidates)
- Real-money path: never exercised in Stage 1 yet

**Pass criteria.**
- Order finalized → matching edges' `gross_attributed_gmv_cents` populated within 5s
- If synchronous stamp fails: reaper catches it within 5min, row gets stamped on next tick
- §A.6 dup detection (same order_id + same attribution dimensions): always 0

**Verification plan (staging).** Mark an order paid via the merchant API; observe edges; observe reaper logs.

### G. T6 daily rollup (`services/gmv_aggregation_service.py`)

**What happens.** Cron `gmv_aggregation_daily` fires at 02:00 UTC, calls `aggregate_daily(yesterday)`, reads all stamped edges, groups by `(date, merchant, agent, channel_partner)`, computes take rate per merchant, upserts `gmv_attribution_daily`.

**Current verification.**
- 11 tests in `test_gmv_aggregation_service.py` (5 base + 2 UTC bucketing + 4 apply_refund)
- §A.3 reconciliation: 0 drift on prod post-cleanup
- Manual `aggregate_daily(yesterday)`: returns 0 (no agent orders in window)

**Pass criteria.**
- Edges from yesterday → rolled up into `gmv_attribution_daily` rows
- Net = gross - refund (clamped ≥ 0)
- Take amount = net × take_rate_bp / 10000
- Promo merchant: 5%; standard: 10%
- Future-date dry-run: returns 0

**Verification plan (staging).** Create synthetic edges with known gross + refund values, call `aggregate_daily`, assert rollup row matches expected math.

### H. T7 monthly invoice (PAUSED until Stage 4)

**What happens.** Cron `invoice_generation_monthly` (currently `next_run_time=None`) would call `run_billing_cycle(period_start, period_end)`. Per merchant: pulls their `gmv_attribution_daily` rows in period, creates a draft Stripe Invoice, attaches one InvoiceItem per rollup row, auto_advance=True so Stripe auto-finalizes after ~1h.

**Current verification.**
- 11 tests in `test_invoice_generation_service.py` (5 base + 5 partial-recovery + 1 dispute)
- Stripe Test mode harness coverage via clock_harness (now prod-blocked)
- Live mode: never exercised
- Idempotency: per-merchant Stripe keys from PR #600, partial-failed resume from PR #601

**Pass criteria.**
- `run_billing_cycle(period)` for one merchant → exactly one draft invoice, one item per rollup row
- Failure mid-loop → run marked `partial_failed`, retry resumes the missing merchant only
- Replay → idempotent (Stripe cache hit on `invoice:{billing_run_id}:{merchant_id}` key)
- Real Live Stripe Invoice gets `auto_advance=true`, auto-finalizes after ~1h

**Verification plan (staging).** Manually invoke `run_billing_cycle` against a recent month with the synthetic merchant's rollups. Inspect Stripe Test Dashboard for the resulting draft invoice. Note: Stripe Test keys are needed here — production Live key would fail because no Live invoice has been created.

### I. T8 monthly partner settlement (PAUSED until Stage 4)

**What happens.** Cron `partner_settlement_monthly` would call `run_settlement(billing_run_id)`. Per channel partner: compute per-merchant compensation, write `settlement_snapshots` + `partner_balance_ledger` rows, create Stripe Transfers to Connect accounts when balance > 0.

**Current verification.**
- Tests exist in clock_harness (now prod-blocked) and `test_partner_settlement_service.py` (not enumerated here yet)
- Live Connect transfers: never executed
- Idempotency: `payout:{payout_id}` key from PR #600

**Pass criteria.**
- For one settlement → settlement_snapshots row written, balance ledger debited correctly
- Connect account missing → payout marked `failed` with `PayoutMissingConnectAccountError`, not silently skipped
- Transfer replay → idempotent (Stripe `payout:{payout_id}` cache hit)

**Verification plan (staging).** Needs a Connect Test account on staging — provision one if absent. Run `run_settlement` against the test billing_run.

### J. Refund attribution

**What happens.** Refund webhook → `services/refund_service.create_refund` → on PSP success calls `commerce_attribution_service.attach_refund_to_attribution_edge`. PR #602 atomic SQL UPDATE writes `refund_amount_cents` + `refunded_amount` on every matching edge, JSONB `?` containment dedupes on `refund_id`.

**Current verification.**
- 10 tests in `test_attach_refund_attribution_cents.py` (single + multi-edge + drift + replay)
- Real refund against a real edge: zero (no production refunds since PR #602)

**Pass criteria.**
- Refund $X against an order → every matching edge sees `refund_amount_cents += X*100` AND `refunded_amount += X`
- Replay same refund_id → no change (JSONB containment idempotency)
- Multi-edge: all N edges accumulate independently
- T6 next rollup: `net_attributed_gmv_cents` reduces by refund * N

**Verification plan (staging).** Refund a paid synthetic order; observe edges + next T6 rollup.

## 2. Readiness scorecard

| System | Tested | E2E vs Live | Verification gap | Risk if untested |
|---|:---:|:---:|---|---|
| A. Subscription signup | ✗ | ✗ | No tests; never run against Live | Merchant can't sign up at all; Stripe idempotency key shape unverified |
| B. Webhook mirror | ✓ (unit) | ✗ | Per-event handler dispatch never run against real Stripe events | Subscription state out of sync with Stripe; invoices unprocessed |
| C. Catalog sync | ✓ (pre-v1.3) | ✓ | Out of audit scope | n/a |
| D. Agent attribution flow | ✓ (unit) | partial | Silent-reject counter never observed; direct-checkout cohort size unknown | Stage 1 promotion gate unmeetable until Section A → B → D works |
| E/F. T9 stamping | ✓ (unit) | ✗ | Reaper idle (no failures to catch yet) | Per-order GMV exits T6 forever if synchronous stamp + reaper both miss |
| G. T6 rollup | ✓ (unit) | ✓ (manual aggregate_daily) | Math against synthetic edges not validated end-to-end | Wrong take rate, wrong invoice math |
| H. T7 invoice | ✓ (unit) | ✗ | Live Stripe invoice path never exercised | First Stage 4 unpause produces broken invoices |
| I. T8 settlement | ✓ (unit) | ✗ | Connect transfer never executed; no Connect account on Test mode? | First settlement run errors per merchant |
| J. Refund attribution | ✓ (unit) | ✗ | Atomic SQL UPDATE never run against real refund | Refund accounting wrong; merchant overcharged |

## 3. Priority shakeout order (recommended)

The right sequence mirrors the merchant journey but starts at the riskiest point (where unit tests are weakest + Live integration unverified):

1. **A. Subscription signup** — highest risk (no tests, no E2E), and it's literally the first thing a merchant touches. Drive against staging with a synthetic merchant. Confirm Stripe Test customer + checkout session created, idempotency replay works.
2. **B. Webhook mirror** — fire all 6 event types via Stripe Test CLI (or webhook test endpoint), confirm `stripe_events` ledger transitions + per-event handler ran.
3. **D + E/F. Attribution end-to-end** — POST a synthetic order with rich attribution metadata, mark paid, confirm edge written + stamped. Repeat with empty metadata, confirm silent-reject counter ticks.
4. **G. T6 rollup** — manually run `aggregate_daily` against the synthetic edges' date; confirm gmv_attribution_daily row matches expected math.
5. **H. T7 invoice (dry-run)** — manually invoke `run_billing_cycle` against the synthetic merchant + period. Confirm a draft invoice + items in Stripe Test Dashboard.
6. **J. Refund** — refund a synthetic paid order; confirm edges + next T6 rollup update.
7. **I. T8 settlement** — needs a Connect Test account; provisioning is its own task. Defer if scope is tight.

## 4. Open prerequisites

- [ ] Connect Test account on staging for §I — TBD whether one exists
- [ ] Synthetic Test merchant on staging — TBD whether one exists with valid API key
- [ ] `STRIPE_CLI` or similar for §B event triggering — install if needed
- [ ] Stripe Test Dashboard access to inspect the side effects of §A, §H, §J

## 5. Pass / fail rubric for "merchant-ready"

The system is ready to onboard merchants when:

- [ ] §1 through §7 in the shakeout each pass once on staging
- [ ] Repeat §1 → §7 with a SECOND synthetic merchant — confirms multi-tenancy
- [ ] §A's checkout session → real merchant browser flow tested at least once (or video-recorded by ops with a Test card)
- [ ] §B fires all 6 event types correctly
- [ ] §J refund updates BOTH `refund_amount_cents` and `refunded_amount` per edge

If any of these fails on staging, fix it. Then promote a code-only deploy to prod (no migrations expected at this point), repeat the smoke of §A only in prod with a single test merchant + small subscription tier, confirm it works against Live Stripe, then green-light external merchant onboarding.
