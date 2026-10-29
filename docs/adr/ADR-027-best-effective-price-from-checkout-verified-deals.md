# ADR-027: Best effective price comes from checkout-verified deals, never from scraped coupon sites

**Status:** Proposed, 2026-10-29.
- peng resolved the four open questions on 2026-10-29 (see "Resolved questions").
- The Phase 0 entry gate below has not been run.
**Decision owner:** peng
**Builds on:**
- ADR-022 (promotions lane deleted). This ADR is the rebuild ADR-022 anticipated, and it keeps ADR-022's invariant word for word.
- ADR-009 (seller of record): deals are keyed by seller.
- ADR-024 / ADR-026 (a market is data): deals are keyed by market and carry the market currency.
- ADR-025 (the attribution loop): the click id and the code travel together, and order evidence closes both.

**Numbering note:** ADR-025 and ADR-026 are on `main`. Check that PIVOTA-Agent has not claimed 027 before this merges.

## Context

### The proposal

The proposal is that agents crawl the web for coupons on the products and merchants we recommend, so the buyer gets a good price. The literal version of that idea is:
- keep a list of third-party coupon aggregator sites;
- search them in real time for each recommendation;
- show what comes back.

The goal is right. A recommendation that is $6 more expensive than the buyer could have paid is a worse recommendation. The literal mechanism conflicts with what this system has already learned, for the reasons below.

### What we already know about remembered prices

`services/live_offer_verification.py` records the audit result that motivated live verification. On a 90-SKU live sample:
- **31.1% of index records** would produce a wrong or unexecutable spec;
- 43% of live PDPs carry an active markdown.

A remembered price is wrong often enough that we stopped trusting it at the moment it matters. A coupon code copied from a third party is a remembered price with weaker evidence: we did not see it, the merchant may never have published it, and nobody has checked it against a checkout.

### What ADR-022 already decided

ADR-022 deleted a promotions lane that showed deals the checkout never applied. It left one invariant for any rebuild:

> **A promotion may be surfaced to an agent only if the system that computes that order's final charge enforces it.**

It also ruled out a second pricing engine. The index stores *observed* offer economics with provenance, predicts at quote time, and reconciles quoted against charged after checkout. A code we scraped satisfies that invariant only after the merchant's checkout has accepted it.

### What exists today and can be reused

- **Code validation against Shopify's checkout authority.** `services/shopify_storefront_pricing_service.py` already submits `discountCodes` on a Storefront cart and reads back `discountCodes { code applicable }` together with allocations. It uses `buyerIdentity.countryCode` / `@inContext(country:)` so pricing is in the market's presentment currency. Today this needs a storefront token from a **connected** store.
- **Showing deals honestly.** `docs/AGENT_SAVINGS_PRESENTATION_CONTRACT.md` (`savings.v1`) already has the groups we need:
  - `appliedStoreDiscounts`: proven by the quote;
  - `availableStoreOffers`: "metadata says it may apply", never shown as applied and never lowers the displayed price;
  - `cartUnlocks`.

  ADR-022 kept `savings_presentation_service` for exactly this.
- **Carrying a code to the merchant.** `routes/order_routes.py` `_build_shopify_cart_permalink_best_effort` already builds `https://{shop}/cart/{variant}:{qty}?discount=CODE`. That URL needs no app scope and no token. `services/outbound_links_service.py` already stamps `attributes[pivota_click_id]` on cart links.
- **Polite crawling.** `services/crawl_politeness.py` handles per-host pacing, robots.txt and 429 backoff on the dedicated crawl egress.
- **Order evidence.** The Shopify `orders/paid` webhook and `external_conversion_poller` receive real order payloads for connected merchants. Those payloads carry the order's discount codes, but nothing records them today (D7 adds that).

### What does not exist

- **No affiliate-network import.** ADR-025 measured this on 2026-09-23, so we hold no network promotion feeds.
- **Few connected stores.** There are 17 connected Shopify stores and 4 Wix stores, against **328 checkout hosts** in the serving catalog (ADR-025). Any design that depends on a merchant connection covers a small minority of what we recommend.
- **No measurement of deal coverage.** We do not know what share of our recommendations have a deal the buyer could actually use today. Phase 0 exists to find out.

## Decision

### D1. No request-time search of third-party coupon sites. Aggregators are out of scope for v1.

Pivota does not query coupon aggregator sites while serving a request, and v1 does not crawl them at all.

1. **Quality.** Aggregator codes are frequently expired, single-use, customer-specific or leaked. A failed code at checkout costs more trust than showing no deal. We have not measured our own failure rate, and we will not bet the feature on someone else's.
2. **ADR-022.** An unverified third-party code cannot be surfaced under the invariant above. Verifying every scraped code means testing guesses against merchant checkouts, which is D3's problem multiplied by the size of the aggregator's database.
3. **Content ownership and merchant trust.** An aggregator's code database is the aggregator's content, not ours to collect. Merchants are Pivota's customers, and merchants distrust any tool that does either of these:
   - surfacing private codes they never meant to publish;
   - taking affiliate credit at checkout.

   We cannot be seen doing either.
4. **Latency and egress.** Live verification already runs with a hard 1.5s cap for the top 3 offers. Aggregators sit behind bot protection, and all crawling leaves from one IP per environment, so a block takes the whole crawl lane down. Bypassing CAPTCHAs or bot detection is not an option.
5. **Attribution.** Some affiliate programs credit the owner of the coupon code over the click. A third-party code can move the commission ADR-025 is trying to secure away from us.

**Reopening this:** aggregators may later be added only through a **licensed API**, only as *leads* that feed D3 verification, and never shown directly. That requires an amendment to this ADR.

### D2. Deal sources are an allowlist, and every deal names its source

| Source | Tier | Example | Shown how (D4) |
|---|---|---|---|
| `checkout_automatic` | A | A Shopify automatic discount that appears as an allocation on a reference cart | Verified |
| `merchant_published` | B | "Use WELCOME15" on the merchant's own homepage, PDP or promo bar, found by our existing crawls | Verified after D3, else "available" |
| `authorized_feed` | B | An affiliate network's promotion feed for a program we belong to | Verified after D3, else "available" |
| `channel_exclusive` | A+ | A code the merchant creates for AI-channel buyers, created in their Shopify by us (D8) | Verified |

These are **never** ingested:
- codes from aggregator sites (D1);
- codes visibly scoped to a person or audience: employee, influencer, student, customer-service or single-use codes;
- codes guessed, enumerated or brute-forced. We only test codes a merchant or an authorized feed published.

### D3. Verification asks the checkout authority, within a strict testing policy

A code becomes `checkout_verified` only when the merchant's checkout platform applies it to a reference cart:
- the cart is in the deal's market (`buyerIdentity.countryCode`);
- the platform reports `applicable = true` with a positive allocation;
- the allocation is in that market's currency;
- the cart is built from a product inside the deal's scope.

**Platforms:**
- **Shopify, connected store:** the existing Storefront cart path in `shopify_storefront_pricing_service`.
- **Shopify, not connected:** Phase 0 checks whether Shopify's tokenless Storefront access supports the cart mutations we need, and at what rate limit. These deals stay `merchant_published` until both of these are true:
  - Phase 0 has confirmed that tokenless carts work;
  - testing codes against non-connected stores has been separately and explicitly approved (Q2). **Testing is off until then.** The verifier refuses non-connected hosts unless `DEAL_VERIFY_NONCONNECTED_ENABLED` is set, and the flag stays unset until that approval.
- **Other platforms:** a UCP `create_checkout` where the merchant exposes one. Otherwise the deal stays `merchant_published`.

**Testing policy (a hard limit, enforced in code):**
- Only D2 sources are tested.
- At most `DEAL_VERIFY_MAX_CARTS_PER_HOST_PER_DAY` carts per host (default small, e.g. 20), through `crawl_politeness`, with Pivota's identifying user agent.
- Carts are abandoned, never checked out.
- No order is ever placed to test a code.

**Freshness:** a verification expires at the earlier of the deal's `ends_at` and `DEAL_VERIFY_TTL_HOURS` (default 24). An expired verification falls back to `merchant_published`.

### D4. Display follows evidence grade, inside `savings.v1`

No new presentation contract. Deals map into the existing groups:

| State | `savings.v1` group | Allowed to |
|---|---|---|
| `checkout_verified`, within TTL | `availableStoreOffers` with `evidence: checkout_verified`, plus an `estimated_effective_price` on the offer | Change offer **ranking**, and show "≈ $38.40 with GLOW10, checked 2h ago" |
| `merchant_published` | **Not emitted in v1** (Q3). If re-enabled later: `availableStoreOffers` with `evidence: merchant_published` | If re-enabled later: shown as "Code available, applied at checkout if eligible". Must **never** change ranking or displayed price |
| Cart-shape dependent (minimum spend, BXGY, free-shipping threshold) | `cartUnlocks` | Show progress only |
| Applied by an actual quote | `appliedStoreDiscounts` | Reduce `pricing.total`, exactly as today |
| `failed` / `expired` | not shown | — |

`estimated_effective_price` always carries:
- `basis` (code, source, `verified_at`, reference-cart market and currency);
- `confirmed_at_checkout: true`.

It never replaces the quoted price, and `pricing.total` stays the only amount that is charged. This is the ADR-022 trapdoor closed by construction: nothing that reached display without checkout evidence can change a price.

**Resolved (Q3): off in v1.**
- `merchant_published` rows are still stored, and verified where D3 allows, but no serving response emits them, to buyers or to agents.
- Re-enabling them needs an amendment to this ADR, backed by Phase 2's measured false-savings rate.

### D5. Serving is a lookup, never a fetch

Deals reach `get_offers`, PDP and recommendation responses through a read of the store in D6, keyed by `(seller_id, market, product_key | collection | sitewide)`. **No request path makes an outbound call for deals.**

**Optional, Phase 2 only:** reuse the live-verification budget of 1.5s for the top 3 offers to re-check the **rank-1** offer's verified code when its verification is older than the TTL. It uses the same `asyncio.wait` partial-results rule, and if the re-check misses the budget the deal stays at its last grade.

### D6. Storage: observed store offers, not a promotions engine

A new table, `observed_store_offers`. It deliberately does not reuse the name `promotions` or any module name pinned by `TestPromotionsLaneStaysDeleted`. Those pins stay. One row per (seller, market, code-or-automatic-rule):

- **Identity:**
  - `seller_id` (ADR-009);
  - `market` (ADR-026 registry);
  - `code` (nullable for automatic discounts);
  - `platform_rule_ref` (when observable).
- **Economics, as published:**
  - `kind` (`percent | amount | free_shipping | gift | bxgy`);
  - `value`;
  - `currency`, which is the **market** currency taken from the evidence. Never the shop default and never FX-converted (ADR-024);
  - `min_subtotal`;
  - `new_customer_only`;
  - scope (`sitewide | collection_refs[] | product_keys[]`);
  - `starts_at`;
  - `ends_at`.
- **Provenance:**
  - `source` (D2);
  - `source_url`;
  - `observed_at`;
  - `terms_text` (raw, for audit).
- **Verification:**
  - `state` (`unverified | checkout_verified | merchant_published | failed | expired`);
  - `verified_at`;
  - `verified_via` (`shopify_storefront_connected | shopify_storefront_tokenless | ucp_checkout`);
  - reference cart (`product_key`, quantity, observed discount amount and currency);
  - `failure_reason`.
- **Outcome (D7):**
  - `orders_seen`;
  - `orders_applied`;
  - `last_order_at`.

Writers are background jobs only. Serving reads only.

### D7. Application and reconciliation follow the existing rails

**Applying the code:**
- **Pivota-run checkout:** the code goes through the existing quote `discount_codes` path. The quote, not this table, decides.
- **Shopify click-out:** the cart permalink carries `?discount=CODE` next to `attributes[pivota_click_id]`, using the existing builder. The code is applied by Shopify and is visible in the merchant's own admin.
- **Other click-outs:** the code is shown to the buyer to enter. We never inject it into a URL whose behaviour we have not verified.

**Reconciling:** the click row records which code (if any) was issued with it. When order evidence returns (ADR-025 link 4), the order's discount codes are compared with the issued code:
- applied: increments `orders_applied`;
- issued but absent from a paid order: counts as a failure signal;
- two failure signals within the TTL demote the deal to `failed` pending re-verification.

Reconciliation coverage is limited to merchants whose orders reach us (about 21 connected stores today). This ADR does not claim more.

### D8. Channel-exclusive codes are the merchant product, and they come last

The differentiated product is not finding other people's codes. It is letting a merchant offer a deal to AI-channel buyers that Shopify enforces and the merchant sees in their own admin. This is the build ADR-022 described:
- the merchant opts in;
- Pivota creates the code in their store through the Admin API (needs the `write_discounts` scope);
- the row is `channel_exclusive`, and is verified the same way as every other deal.

It needs a merchant commitment first (ADR-022's rebuild trigger). It ships in Phase 3, not before.

## Phases and gates

**Phase 0: measure (about 1 week, read-only).**
- Sample the top recommended sellers per served market (US, SG).
- For each, record:
  - automatic discounts visible on a reference cart (connected stores);
  - codes published on the merchant's own surfaces, from pages our crawls already fetch (no new fetches from laptops);
  - whether tokenless Storefront carts can test them.
- Output: the share of recommendations with a `checkout_verified` deal today, the share with `merchant_published` only, and median savings. Measure US and SG; Phase 1 ships US only (Q4).

**Gate:** if fewer than about 10% of recommendations would carry a verified deal, stop here. Put the effort into cross-seller effective-price comparison instead, which needs no coupons.

**Phase 1: US, Shopify, verified only.**
- `observed_store_offers` table.
- `merchant_published` extraction from existing crawls.
- The verifier job (connected stores, plus tokenless if Phase 0 confirmed it).
- `estimated_effective_price` and `availableStoreOffers` on `get_offers`.
- Ranking uses verified deals only.
- `?discount=` on Shopify click-outs.

**Phase 2: reconcile and widen.**
- Code-on-click recording and order reconciliation (D7).
- UCP verification for non-Shopify merchants.
- The optional rank-1 re-check (D5).
- `authorized_feed` ingestion, once affiliate network access exists (Q1).
- SG, if Phase 0 shows enough verified coverage there (Q4).

**Phase 3: channel-exclusive codes (D8)**, on a merchant commitment.

## Metrics

- **Verified-deal coverage:** the share of served recommendations with a `checkout_verified` deal.
- **False-savings rate:** issued codes that did not apply on a paid order, among reconciled orders. Target under 2%. **This is the metric that can stop the lane.**
- **Median and p90 savings** per verified deal, in market currency.
- **Click-out to order conversion** with and without a verified deal (A/B, Phase 2).
- **Verifier load:** carts per host per day. Alert at 80% of the cap.

## Alternatives considered

- **Real-time aggregator search (the original proposal).** Rejected: D1.
- **Testing codes on every checkout, browser-extension style.** Rejected for the same reasons as D1, and because merchants experience it as hostile.
- **Pivota applies the discount itself (infra-side pricing).** Rejected by ADR-022: it creates a second pricing engine, and the discount disappears on delegated checkout.
- **No coupons, cross-seller price comparison only.** This is the fallback if the Phase 0 gate fails. It is also worth doing regardless, since multi-seller offers already exist.

## Consequences

- Buyers see fewer deals than an aggregator would show, and the ones they see work. This is the trade-off.
- Most non-connected merchants contribute only `merchant_published` deals until tokenless verification or UCP covers them. Coverage grows with connections, which lines up with ADR-025's incentive to connect merchants.
- `TestPromotionsLaneStaysDeleted` stays as is. Add one new pin: the serving modules that read `observed_store_offers` make no outbound HTTP calls (D5).

## Resolved questions (peng, 2026-10-29)

1. **Affiliate networks: `authorized_feed` stays in scope.**
   - Ingestion lands in Phase 2, once affiliate network access exists.
   - Network order reports would also give ADR-025's link 4 (order evidence) a source for non-connected merchants.
2. **Testing codes on non-connected stores: off until separately approved.**
   - Phase 0 checks only whether tokenless carts work.
   - Until approval, the verifier refuses non-connected hosts (D3), and their deals stay `merchant_published`.
3. **Showing unverified deals: off in v1.**
   - Only `checkout_verified` deals are emitted (D4).
   - Revisit with Phase 2's measured false-savings rate, by amending this ADR.
4. **Markets: US only in Phase 1.**
   - Phase 0 measures US and SG.
   - SG is a Phase 2 candidate if its verified coverage clears the gate.
