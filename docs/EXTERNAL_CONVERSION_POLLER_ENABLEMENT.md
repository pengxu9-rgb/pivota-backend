# External Conversion Poller Enablement Runbook

The external-conversion pollers close the one genuinely **non-custodial**
attribution path (ADR-016): a merchant settles a sale **on their own store**, and
Pivota records the attribution edge + stamps the referred GMV **without ever being
in the buyer→merchant transaction fund flow**. For a non-custodial Pivota this is
the crown-jewel loop (ADR-017 workstream 2).

The pollers are **fully built, hardened, and scheduler-wired — but gated off by
default.** They no-op every tick unless `EXTERNAL_CONVERSION_POLLER_ENABLED` is set
in the environment. With the flag unset (the default), deploying changes nothing:
no autonomous polling starts.

This runbook is the **go/no-go** for flipping that flag. Enabling is an
**operational** action (an env var per environment) — there is no code change that
flips it, and this document ships no such change.

## Status at a glance

**Engineering is complete** — every layer below shipped through adversarial review
and is on `main`. **What remains before a flip is operational** (a one-off SQL smoke
check + the env flip) plus sign-off.

| | Item | State |
|---|---|---|
| ✅ | Idempotent closure primitive + `(merchant_id, external_order_id)` guard | DONE — merged & reviewed |
| ✅ | Watermark hold on fetch failure (data-loss fix) — #1484 | DONE |
| ✅ | MAX_PAGES tail-hold (F1) + stuck-merchant escalation & batch metric (F2) — #1488 | DONE |
| ✅ | 429 Retry-After backoff + throttle + per-tick cap (fair rotation) — #1490 | DONE |
| ✅ | `referral_only` `utm_content` join key — #1226 | DONE |
| ✅ | Conversion-report / receipt-ingest API (bare-link closure) — #1486 | DONE |
| ⬜ | **Pre-enable SQL smoke check** (candidate queries vs. prod Postgres) | ops — section 1 |
| ⬜ | **Flip `EXTERNAL_CONVERSION_POLLER_ENABLED=true`** per environment | ops — section 2 |
| ⬜ | **Watch the first ticks** (observability) | ops — section 5 |

## The non-custodial closure map

Every path below records an attribution edge via the SAME idempotent primitive —
`close_external_order_conversion` (`services/commerce_attribution_service.py`) —
and none touches transaction funds. A merchant's integration determines which path
closes their conversions:

| Merchant integration | Closure path | Gated by |
|---|---|---|
| Shopify store with a registered `orders/paid` **webhook** | Webhook (`routes/webhook_routes.py`) recovers `pivota_click_id` from `note_attributes` (cart-permalink) | **live** — no flag |
| Shopify store with `read_orders` but **no** webhook (App-Store installs; the connected TEST merchant) | **Shopify read-orders poller** (this runbook) recovers `pivota_click_id` from `note_attributes` | `EXTERNAL_CONVERSION_POLLER_ENABLED` |
| Connected **WooCommerce** store (WC 8.5+ Order Attribution) | **Woo poller** (this runbook) recovers the click id from `_wc_order_attribution_utm_content` order meta | `EXTERNAL_CONVERSION_POLLER_ENABLED` |
| `referral_only` **bare link** — no store connection at all | **Conversion-report API** — the merchant POSTs the settled order (#1486); see below | live — no flag (endpoint mounted) |

`referral_only` redirects already carry the click id as a `utm_content` landing
param (`append_referral_click_param`, `services/outbound_links_service.py`; #1226),
which is what the Woo lane reads for connected stores. For a **truly unintegrated**
merchant (a bare link with no store connection Pivota can poll), the defined
closure is the signed **conversion-report API**:

- `POST /attribution/conversions/report` — the merchant (or their platform)
  reports a settled order referencing the Pivota `click_id`.
- Auth: `X-Pivota-Merchant-Id` names the merchant; `X-Pivota-Signature` is an
  HMAC-SHA256 of the **raw request body** keyed by the merchant's API key.
- Idempotent on `(merchant_id, external_order_id)`; a replay reporting a different
  gross surfaces a `gmv_discrepancy` reconciliation signal rather than
  double-counting. No funds are touched — Pivota sees the *order report*, not the
  money.

So **acceptance criterion 3 is satisfied**: `referral_only` conversions have a
defined closure (Woo `utm_content` join for connected stores; the report API for
bare links) — not merely "documented as unsupported."

## What's built and verified (no action needed)

- **Idempotent, non-custodial closure.** All lanes funnel through
  `close_external_order_conversion`, which owns the edge upsert, the
  `(merchant_id, external_order_id)` ON-CONFLICT-DO-NOTHING guard, the
  `click_matched` gate, and the ADR-009 D3 seller-of-record subject/mismatch guard.
  A poll running alongside (or replaying after) the webhook **never double-counts
  GMV**.
- **Watermark data-safety (#1484, #1488).** Each merchant has a per-merchant
  watermark (`external_conversion_poll_state.last_polled_at`) so each tick only
  fetches new/updated orders. The watermark advances **only when the window was
  fully scanned**. On an **incomplete** scan — a page-FETCH failure (429 / timeout
  / 5xx) *or* a MAX_PAGES cap with a page still pending — the watermark is **held**
  and the window is re-polled next tick, so no unscanned order is ever skipped.
  Idempotency dedups the re-poll.
- **Stuck-merchant escalation (#1488).** A merchant whose window can't fully scan
  (persistent page-cap or a broken credential) is held every tick and, once its
  watermark ages past the threshold (or on a first run already over the page cap),
  escalates to an **ERROR** log with `watermark_stuck` — telling an operator to
  raise `MAX_PAGES` or fix the credential rather than failing silently.
- **Rate-limit backoff + fair scheduling (#1490).** A 429 is honored with a
  bounded, capped `Retry-After` backoff (retried in-tick); if exhausted it degrades
  to the same safe watermark hold. Optional inter-page / inter-merchant throttle
  and a per-tick merchant cap (fair rotation by attempt recency) bound the API
  budget — **all off by default** (section 4).

## Section 1 — Pre-enable SQL smoke check (do this first)

#1490 changed both candidate queries to order by fair-share recency. They are only
exercised against a stubbed `fetch_all` in tests, never real Postgres. Before the
flip, run each once against **prod Postgres (or a staging replica)** to confirm it
parses and returns as expected — the `GROUP BY` + `ORDER BY MIN(ps.last_run_at) ASC
NULLS FIRST` + the `'woo::' || s.merchant_id` namespaced join are the new bits:

- `_CANDIDATE_MERCHANTS_SQL` — `services/external_conversion_poller.py`
- `_WOO_CANDIDATE_MERCHANTS_SQL` — `services/woocommerce_conversion_poller.py`

Substitute `:cutoff` with `now() - interval '30 days'`. A clean result set (one row
per merchant, most-stale-first) is the go signal for section 2.

Also confirm the `external_conversion_poll_state` table exists (migration 168 /
`db/schema_guard.py` self-heal — Railway deploys skip `db/migrations/`).

## Section 2 — Flip the flag

Set, per environment you intend to enable:

```
EXTERNAL_CONVERSION_POLLER_ENABLED=true
```

Accepted truthy values: `1`, `true`, `yes`, `on` (case-insensitive;
`services/external_conversion_poller.py`). The pollers are already registered on
the scheduler (`services/audit_scheduler.py`, id `external_conversion_poll`), so no
restart-for-registration is needed — the next 15-minute tick will run instead of
no-op. The one batch entry (`poll_external_conversions_batch`) drives **both** the
Shopify read-orders lane and the WooCommerce lane.

## Section 3 — Cadence & scope (what "enabled" does)

- **Every 15 minutes** (`audit_scheduler.py`, `misfire_grace_time=600`,
  `coalesce=True`).
- **Scoped** to merchants with (a) a connected store for the lane AND (b) a
  Pivota-referred click within the last **30 days** (`CLICK_RECENCY_WINDOW_DAYS`) —
  a merchant with no recent attributed clicks is never polled (no wasted API
  quota).
- **Per merchant per tick:** up to `MAX_PAGES=20` pages of
  `ORDERS_PAGE_LIMIT` orders (Shopify 250, Woo 100) newer than the watermark. A
  window larger than 20×limit holds (section: stuck escalation) rather than
  dropping the tail.

## Section 4 — Optional throttle / cap knobs (default off)

Leave all three unset for the default behavior (poll everyone, no inter-request
sleep). Set them to spread load once enabled for a large or rate-limited fleet:

| Env var | Effect | Default |
|---|---|---|
| `EXTERNAL_CONVERSION_POLLER_INTER_PAGE_SLEEP_S` | Sleep between pages within a merchant | `0` (off) |
| `EXTERNAL_CONVERSION_POLLER_INTER_MERCHANT_SLEEP_S` | Sleep between merchants | `0` (off) |
| `EXTERNAL_CONVERSION_POLLER_MAX_MERCHANTS_PER_TICK` | Cap merchants polled **per lane** per tick; remainder carried to the next tick (fair rotation by attempt recency, so no merchant starves) | `0` (unlimited) |

The 429 `Retry-After` backoff is **always on** regardless of these.

## Section 5 — Observability (what to watch after the flip)

- **Batch metric (per tick, INFO):**
  `external_conversion_poller batch: merchants=N closed=N held=N stuck=N errors=N shopify_deferred=N`.
  `closed` climbing and `held`/`stuck`/`errors` near zero is healthy.
- **`held` rising** — merchants whose window couldn't fully scan this tick (fetch
  failure or page cap); transient is fine, sustained is a signal.
- **`stuck` > 0 / ERROR `holding watermark … STUCK …`** — a merchant needs
  operator action: raise `MAX_PAGES` (page-cap) or fix the credential
  (fetch-failure). This is the loud alarm the hardening added.
- **`external_conversion_poll_state` table** — `last_polled_at` (watermark) and
  `last_run_at` (last attempt) per merchant confirm progress; `last_closed_count`
  is the last run's closes.
- **Attribution edges** — a Woo/read-orders conversion should appear as a
  `converted` edge with `source = external_redirect` and no Pivota `orders` row.

## Section 6 — Rollback

Unset `EXTERNAL_CONVERSION_POLLER_ENABLED` (or set it falsy). The next tick
`poll_external_conversions_batch` returns `{skipped: "disabled"}` and no-ops. There
is **no state to unwind** — closures already recorded are correct and idempotent;
watermarks simply stop advancing. Rollback is safe at any time.

## Acceptance criteria (#1480) mapping

- **"The pollers run (enabled) on a schedule in prod (observable)."** → section 2
  (flip) + section 5 (the batch metric + `poll_state` table make it observable).
  The scheduler wiring (15-min) is already live.
- **"A conversion on a connected Woo store binds to its edge without Pivota
  touching transaction funds."** → the Woo lane's `utm_content` join → the
  idempotent, non-custodial primitive (closure map + "what's built").
- **"`referral_only` conversions have a defined closure, or are explicitly
  documented as report-API-only."** → the closure map: connected stores via the
  Woo `utm_content` join; bare links via the conversion-report API (#1486).

## References

- ADR-016 (Pivota non-custodial), ADR-017 (attribution integrity).
- Poller hardening: #1484, #1488, #1490. Bare-link closure: #1486. Join key: #1226.
- Code: `services/external_conversion_poller.py`,
  `services/woocommerce_conversion_poller.py`,
  `services/commerce_attribution_service.py`, `services/audit_scheduler.py`,
  `routes/attribution_conversions.py`.
