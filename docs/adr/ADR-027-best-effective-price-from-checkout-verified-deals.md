# ADR-027: Best effective price comes from checkout-verified deals, never from scraped coupon sites

**Status:** Proposed, 2026-10-29. Revised 2026-10-30 after review.
- peng resolved four questions on 2026-10-29 (see "Resolved questions").
- Six strategic questions are still open (see "Open questions for peng"). This ADR cannot be Accepted until they are answered.
- Phase 0a ran on 2026-10-29. Its base was unusable, so the gate has not been computed (see "Phases and gates").

**Decision owner:** peng

**Builds on:**
- ADR-022 (promotions lane deleted). This ADR keeps ADR-022's invariant word for word. Whether it is the rebuild ADR-022's trigger describes is open question (a).
- ADR-009 (seller of record): deals are keyed by seller.
- ADR-016 (Pivota is non-custodial): every discount is merchant-funded.
- ADR-024 / ADR-026 (a market is data): deals are keyed by market and carry the market currency.
- ADR-025 (the attribution loop): the click id and the code travel together, and order evidence closes both. **ADR-025 is still Proposed.** This ADR depends on its `issue_click` and link-4 decisions.

**Numbering note:** ADR-025 and ADR-026 are on `main`. PIVOTA-Agent has no `docs/adr` tree, so 027 is not claimed there.

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

The 43% figure matters twice here. Shopify codes often exclude items already on sale, so a code that works on one product in a store says little about the next one.

### What ADR-022 already decided

ADR-022 deleted a promotions lane that showed deals the checkout never applied. It left one invariant for any rebuild:

> **A promotion may be surfaced to an agent only if the system that computes that order's final charge enforces it.**

It also requires that display is evidence-gated for **the lane the order will use**, and it ruled out a second pricing engine. The index stores *observed* offer economics with provenance, predicts at quote time, and reconciles quoted against charged after checkout. A code we scraped satisfies that invariant only after the merchant's checkout has accepted it, for that item, in that market, on that lane.

### What exists today and can be reused

- **Code validation against Shopify's checkout authority.** `services/shopify_storefront_pricing_service.py` already submits `discountCodes` on a Storefront cart and reads back `discountCodes { code applicable }` together with allocations. It uses `buyerIdentity.countryCode` / `@inContext(country:)` so pricing is in the market's presentment currency. Today this needs a storefront token from a **connected** store.
- **Showing deals honestly.** `docs/AGENT_SAVINGS_PRESENTATION_CONTRACT.md` (`savings.v1`) already has the groups we need:
  - `appliedStoreDiscounts`: proven by the quote;
  - `availableStoreOffers`: "metadata says it may apply", never shown as applied and never lowers the displayed price;
  - `cartUnlocks`.

  ADR-022 kept `savings_presentation_service` for exactly this. Showing an effective price for a verified deal is still a change to that contract (D4).
- **Carrying a code to the merchant.** Two Shopify cart-permalink builders exist, and neither builds the link this ADR needs:
  - `routes/order_routes.py::_build_shopify_cart_permalink_best_effort` adds `?discount=` and `payment=shop_pay`, but no click attribute and no `country=`;
  - `services/outbound_links_service.py::build_shopify_cart_permalink` adds `attributes[pivota_click_id]` and an optional `country=` that pins the checkout's market, but no discount.

  D7 makes one builder carry all three.
- **Polite crawling.** `services/crawl_politeness.py` (per-host pacing, robots.txt, 429 backoff), `services/crawl_global_pacer.py` and `services/shopify_edge_pacer.py`. All three keep their state inside one process or one run. None of them paces two jobs against each other, and none counts a daily quota.
- **Order evidence.** The Shopify `orders/paid` webhook and `external_conversion_poller` receive real order payloads for connected merchants. Those payloads carry the order's discount codes, but nothing records them today (D7 adds that).
- **Click minting.** `services/commerce_attribution_service.py::issue_click` is the one helper that mints click rows (ADR-025 D1).

### What does not exist

- **No affiliate-network import.** ADR-025 measured this on 2026-09-23, so we hold no network promotion feeds.
- **Few connected stores.** There are 17 connected Shopify stores and 4 Wix stores, against **328 checkout hosts** in the serving catalog (ADR-025). Any design that depends on a merchant connection covers a small minority of what we recommend.
- **No measurement of deal coverage, and no base to measure it on yet.** The recommendation ledger records only part of our traffic today. The gateway answers most searches itself and writes no ledger rows. The backend half of the fix is live with its ingest route dark, and the gateway's writer has not been built. Until both run, "share of recommendations" has no trustworthy denominator (Phase 0).

## Decision

### D1. No request-time search of third-party coupon sites. Aggregators are out of scope for v1.

Pivota does not query coupon aggregator sites while serving a request, and v1 does not crawl them at all.

1. **Quality.** Aggregator codes are frequently expired, single-use, customer-specific or leaked. A failed code at checkout costs more trust than showing no deal. We have not measured our own failure rate, and we will not bet the feature on someone else's.
2. **ADR-022.** An unverified third-party code cannot be surfaced under the invariant above. Verifying every scraped code means testing guesses against merchant checkouts, which is D3's problem multiplied by the size of the aggregator's database.
3. **Content ownership and merchant trust.** An aggregator's code database is the aggregator's content, not ours to collect. Merchants are Pivota's customers, and merchants distrust any tool that does either of these:
   - surfacing private codes they never meant to publish;
   - taking affiliate credit at checkout.

   We cannot be seen doing either.
4. **Latency and egress.** All crawling leaves from one address per environment, and aggregators sit behind bot protection, so a block takes the whole crawl lane down. Request-path traffic leaves by the payment-allowlisted address, which must not carry crawl-like load. Bypassing CAPTCHAs or bot detection is not an option.
5. **Attribution.** Some affiliate programs credit the owner of the coupon code over the click. A third-party code can move the commission ADR-025 is trying to secure away from us.

**Reopening this:** aggregators may later be added only through a **licensed API**, only as *leads* that feed D3 verification, and never shown directly. That requires an amendment to this ADR.

### D2. Deal sources are an allowlist, and every deal names its source

| Source | Tier | Example | Shown how (D4) |
|---|---|---|---|
| `checkout_automatic` | A | A Shopify automatic discount that appears as an allocation on a reference cart | Verified, per variant (D3) |
| `merchant_published` | B | "Use WELCOME15" on the merchant's own homepage, PDP or promo bar | Verified per variant after D3; otherwise stored, **not emitted (v1)** |
| `authorized_feed` | B | An affiliate network's promotion feed for a program we belong to | Verified per variant after D3; otherwise stored, **not emitted (v1)** |
| `channel_exclusive` | A+ | A code the merchant creates for AI-channel buyers, created in their Shopify by us (D8) | Verified, per variant (D3) |

These are **never** ingested:
- codes from aggregator sites (D1);
- codes visibly scoped to a person or audience: employee, influencer, student, customer-service or single-use codes;
- codes guessed, enumerated or brute-forced. We only test codes a merchant or an authorized feed published.

**Where `merchant_published` text comes from.** Today's crawls store mostly PDP copy, not homepages or promo bars. Phase 1 reads only what existing crawls already store. Fetching new surfaces (homepage, promo bar) is a new fetch. It needs its own per-host budget, runs on the crawl egress under D3's egress rules, and is not part of Phase 1 unless Phase 0 shows PDP text misses most published codes.

**Merchant opt-out.** A per-seller denylist (`deal_lane_optout`, in the DB) stops both testing and display for that seller. The verifier refuses denylisted sellers, and serving drops their rows. A merchant asks once and is never tested again.

### D3. Verification asks the checkout authority, per variant, within a strict testing policy

A deal is `checkout_verified` **for one variant in one market**, and only when the merchant's checkout platform applies it to a reference cart that meets all of these:
- the cart holds **that variant at quantity 1**, and nothing else;
- the cart is in the deal's market (`buyerIdentity.countryCode`);
- the platform reports `applicable = true` with a positive allocation;
- the allocation is in that market's currency.

What one verification proves, and what it does not:
- **Scope** is the set of variants actually verified. A published scope ("sitewide", "all serums") is stored as metadata. It never extends a verification to a variant that was not tested, because codes routinely exclude sale items and some collections. Where the platform exposes the real scope (a connected store's Admin discount objects; to be confirmed), it may decide **which variants to test next**, never which variants count as verified.
- **Quantity.** A deal that applies only above quantity 1, or only above a `min_subtotal` that one unit does not reach, is cart-shape dependent. It goes to `cartUnlocks` (D4), never to an effective price.
- **Combinations.** Whether a code stacks with an automatic discount already on the variant is part of what the reference cart shows, so it holds for that variant only.

**Buyer eligibility.** An anonymous reference cart cannot test once-per-customer, first-order-only or segment-limited codes; Shopify checks those against the buyer at checkout. In v1, any deal that is `new_customer_only`, once-per-customer or segment-limited (known from the published terms, the platform, or a failure reason) **is not emitted at all**. It never ranks, never shows an effective price, and is not shown as information either.

**Platforms:**
- **Shopify, connected store:** the existing Storefront cart path in `shopify_storefront_pricing_service`.
- **Shopify, not connected:** these deals stay stored and unverified until both of these are true:
  - tokenless Storefront carts are confirmed to support the cart mutations we need. Phase 0a answered this from Shopify's documentation only; it has not been tried against any store;
  - testing codes against non-connected stores has been separately and explicitly approved (Q2, and open question (f)). **Testing is off until then.** The verifier refuses non-connected hosts unless `DEAL_VERIFY_NONCONNECTED_ENABLED` is set, and the flag stays unset until that approval.
- **Other platforms:** a UCP `create_checkout` where the merchant exposes one (Phase 2). Otherwise the deal stays stored and unverified.

**Testing policy (a hard limit, enforced in code):**
- Only D2 sources are tested, and never for a seller on the opt-out denylist.
- Carts are abandoned, never checked out. No order is ever placed to test a code.
- At most `DEAL_VERIFY_MAX_CARTS_PER_HOST_PER_DAY` carts per host per UTC day (default small, e.g. 20). The counter lives **in the database**, because the verifier may run as several processes and several runs a day. The verifier reserves a slot before building a cart and stops for that host when the counter is spent.

**Egress (a hard limit):**
- The verifier is a **background job only**. It never runs in a request path, from a laptop, or from a developer worktree.
- It leaves by the **`pivota-crawl` subnet**, never by the default or payment-allowlisted egress.
- Its requests are **signed** with the bot-authentication mechanism Shopify recognises (Web Bot Auth or Shopify's own), not just an identifying user agent. Unsigned bots get Shopify's strictest limits.
- It is paced **jointly** with the other crawl jobs on that address. Either it runs inside the same run's `crawl_global_pacer` / `shopify_edge_pacer`, or a launch gate refuses to start it while any other crawl job is running. Pacing within one process is not enough, because other crawl jobs share the same exit address.

**Freshness:** a verification expires at the earlier of the deal's `ends_at` and `DEAL_VERIFY_TTL_HOURS` (default 24). **An expired deal stops being shown.** It goes back to unverified, which v1 does not emit. Nothing serves a deal on the strength of a past verification.

### D4. Display follows evidence grade, and needs a `savings.v1` amendment

Showing an effective price, and later letting it rank, changes `savings.v1`:
- the contract says `availableStoreOffers` must not reduce the displayed product price;
- applied discounts may affect value only when the quote total already includes them.

This ADR therefore needs a `savings.v1` amendment (or a `savings.v2`) that defines `evidence` and `estimated_effective_price`. The amendment ships with Phase 1 and is part of open question (b).

| State | `savings.v1` group | Allowed to |
|---|---|---|
| `checkout_verified` for this variant and market, within TTL, on an offer whose lane carries the code (D7) | `availableStoreOffers` with `evidence: checkout_verified`, plus an `estimated_effective_price` on that offer's code-carrying link | Show "≈ $38.40 with GLOW10, checked 2h ago". **Phase 1 is display-only: no ranking effect.** Ranking is open question (b) |
| Verified for another variant only, or the offer's lane cannot carry the code | not emitted | — |
| Unverified (`merchant_published`, `authorized_feed`) | **Not emitted in v1** (Q3) | — |
| Eligibility-limited (new customer, once per customer, segment) | **Not emitted in v1** (D3) | — |
| Cart-shape dependent (minimum spend, above quantity 1, BXGY, free-shipping threshold) | `cartUnlocks` | Show progress only |
| Applied by an actual quote | `appliedStoreDiscounts` | Reduce `pricing.total`, exactly as today |
| `failed` / `expired` | not shown | — |

**The base of `estimated_effective_price`** is the reference cart's own subtotal for that variant, in the market currency. It is never the index price, which the Context shows is wrong too often. If the reference cart is older than the TTL, there is no effective price.

`estimated_effective_price` always carries:
- `basis`: code, source, `verified_at`, the variant verified, the reference-cart market, currency and subtotal;
- `subject_to_checkout: true`, meaning the merchant's checkout decides the final amount.

It never replaces the quoted price, and `pricing.total` stays the only amount that is charged. This is the ADR-022 trapdoor closed by construction: nothing that reached display without checkout evidence, for that variant and that lane, can change a price.

**Resolved (Q3): unverified deals off in v1.**
- Unverified rows are still stored, and verified where D3 allows, but no serving response emits them, to buyers or to agents.
- Re-enabling them needs an amendment to this ADR, backed by the measured false-savings rate (Metrics).

### D5. Serving is a lookup, never a fetch

Deals reach `get_offers`, PDP and recommendation responses through a read of the store in D6, keyed by `(seller_id, market, variant)`. **No request path makes an outbound call for deals.** There is no request-time re-check, in any phase: a re-check would run in `web`, which leaves by the payment-allowlisted address.

Hot deals are kept fresh by the background verifier instead: it re-verifies the variants most often shown before their TTL runs out, inside the D3 cap.

### D6. Storage: observed store offers, not a promotions engine

A new table, `observed_store_offers`. It deliberately does not reuse the name `promotions` or any module name pinned by `TestPromotionsLaneStaysDeleted`. Those pins stay. One row per (seller, market, code-or-automatic-rule):

- **Identity:**
  - `seller_id` (ADR-009);
  - `market` (ADR-026 registry; see "Market" below);
  - `code` (nullable for automatic discounts);
  - `platform_rule_ref` (when observable).
- **Economics, as published (metadata, never evidence):**
  - `kind` (`percent | amount | free_shipping | gift | bxgy`);
  - `value`;
  - `currency`, which is the **market** currency taken from the evidence. Never the shop default and never FX-converted (ADR-024);
  - `min_subtotal`;
  - eligibility: `new_customer_only`, `once_per_customer`, `segment_limited`;
  - published scope (`sitewide | collection_refs[] | product_keys[]`), used only to choose what to test;
  - `starts_at`;
  - `ends_at`.
- **Provenance:**
  - `source` (D2);
  - `source_url`;
  - `observed_at`;
  - `terms_text` (raw, for audit).
- **Verification**, in a child table with one row per (deal, variant):
  - `state` (`unverified | checkout_verified | failed | expired`);
  - `verified_at`;
  - `verified_via` (`shopify_storefront_connected | shopify_storefront_tokenless | ucp_checkout`);
  - the reference cart: variant, quantity (always 1), subtotal, discount amount and currency;
  - `failure_reason`.
- **Outcome (D7):**
  - `orders_seen`;
  - `orders_applied`;
  - `last_order_at`.

Writers are background jobs only. Serving reads only.

**Market.** A market is never defaulted.
- **`merchant_published` rows:** crawls leave from one region, so a stored page is whatever version the store served there. The row's market comes from page evidence: the store's market context, a locale path, or the currency on the page. With no evidence, the row has no market. It is never verified and never served. It never falls back to US.
- **Connected-store rows:** the market comes from the ADR-026 registry or from the presentment currency. The `market` column on connected-store offers may hold a placeholder `'US'` for every row. That must be checked before D5 joins on it, and if it holds, D5 must not use it.

### D7. Lanes, application and reconciliation

#### Every discount is merchant-funded

Every discount this ADR shows is funded by the merchant and enforced by the merchant's checkout. Pivota never funds, subsidises, prefunds or rebates a discount (ADR-016).

#### The lane matrix

A verified deal may show an effective price only on a lane that both carries the code and has the evidence that lane needs. Everywhere else it is not emitted.

| Lane | Carries the code? | Evidence needed | v1 |
|---|---|---|---|
| **Pivota-orchestrated checkout** (the merchant's PSP charges; Pivota orchestrates, ADR-016) | Yes: the quote's `discount_codes` | The quote itself. The quote decides, and an applied code lands in `appliedStoreDiscounts` | Yes |
| **Shopify cart permalink** (human click-out) | Yes: `?discount=CODE` | Variant verified in this market (D3). The link pins the market with `country=` | Yes, through the one builder below |
| **Reap cart-link** (ADR-025 D6b) | **No.** `validate_cart_link` deliberately refuses `discount=` | — | **Excluded.** Carrying a code there needs a change to Reap's allowlist, which is a separate decision with its own review |
| **UCP delegated checkout / Reap variant lane** | Not established | Per-merchant proof that a code can be passed and is applied | Excluded in v1. Revisit in Phase 2 with UCP verification |
| **Affiliate-network seller** (`authorized_feed`) | Only if the network's deep link can carry it | The carrier follows ADR-025's evidence route (network link with our click id as sub-id), plus written program terms allowing code and click together | Excluded in v1. Open question (e) |
| **Handed-out `pdp_url`** (an agent passes the product page on) | No | — | No effective price. The effective price is attached to the code-carrying link only, never to the bare `pdp_url` |
| **Other click-outs** (non-Shopify web checkout; the buyer types the code) | Only by the buyer | A verification on that same checkout (UCP, Phase 2) | Excluded in v1 |

#### One builder for the Shopify permalink

One function builds the click-out link, and it composes all three in one URL:
- `attributes[pivota_click_id]` from `issue_click`;
- `country=<MARKET>`, which pins the checkout to the market the code was verified in. Without it, the checkout's market follows the opener's IP address;
- `discount=CODE`.

It refuses to add `discount=` if it cannot also add `country=` and the click attribute. The two existing builders either call it or are retired.

#### Recording the code, and reconciling (Phase 1)

- **Recording.** The code issued with a click is a field on the click row, written by ADR-025's `issue_click` at issue time. There is no second writer.
- **Reconciling.** When order evidence returns (ADR-025 link 4), the order's discount codes are compared with the issued code:
  - applied: increments `orders_applied`;
  - issued, carried in the URL, absent from a paid order: a **failure signal**. Eligibility-limited codes are never emitted in v1 (D3), so a returning customer's ineligibility cannot cause one;
  - two failure signals within the TTL demote that variant's verification to `failed`, pending re-verification.
- **Coverage.** Reconciliation covers only merchants whose orders reach us (about 21 connected stores today). Whether an affiliate network's report carries the coupon code used is not established. Until it is confirmed per network, the false-savings rate is measured on connected stores only.
- **Abandonment.** Paid orders count only the buyers who finished. A buyer whose code failed and who then left produces no order. For connected stores, the checkout-update (abandoned checkout) payload, where it carries the code and its application status, gives a signal that does not depend on survivors (needs the scope confirmed).

#### Kill switches

Four independent flags, all default off, each able to stop its part alone:
- `DEAL_LANE_EMIT_ENABLED`: serving emits deals at all;
- `DEAL_LANE_RANKING_ENABLED`: deals may change ranking (stays off through Phase 1);
- `DEAL_LANE_DISCOUNT_PARAM_ENABLED`: the click-out builder adds `discount=`;
- `DEAL_VERIFY_JOB_ENABLED`: the verifier job runs.

**Stop rule (automatic).**
- A scheduled check computes the false-savings rate per market over the last **14 days**.
- Once that window has at least **50 reconciled orders** carrying an issued code, and the rate is **2% or higher**, the check writes a stop row in the database.
- Serving and the click-out builder read the stop row. While it exists they behave as if `DEAL_LANE_EMIT_ENABLED`, `DEAL_LANE_RANKING_ENABLED` and `DEAL_LANE_DISCOUNT_PARAM_ENABLED` were off.
- Only a person clears it, after review.
- Below 50 reconciled orders the rate is reported but does not trigger the stop.

### D8. Channel-exclusive codes are the merchant product, and they come last

The differentiated product is not finding other people's codes. It is letting a merchant offer a deal to AI-channel buyers that Shopify enforces and the merchant sees in their own admin. This is the build ADR-022 described:
- the merchant opts in;
- Pivota creates the code in their store through the Admin API (needs the `write_discounts` scope). The code is merchant-funded like every other (D7);
- the row is `channel_exclusive`, and is verified the same way as every other deal.

It needs a merchant commitment first (ADR-022's rebuild trigger). It ships in Phase 3, not before.

## Phases and gates

**Phase 0a: read-only (ran 2026-10-29).**
- SQL census over the recommendation ledger and stored crawl text, plus Shopify's documentation for tokenless carts. It wrote nothing and fetched no store.
- Result: the ledger base was unusable. It held exposures from one backend search route only, with no offer ids, and no rows after 2026-10-26. The gateway's own searches write nothing yet. Tokenless carts: answered from documentation only; not tried against any store.
- The gate was not computed.

**Phase 0 base (dependency).** The gate needs a denominator. Either:
- the gateway's ledger writer is built and switched on, and has run for N days; or
- a named provisional base is used and re-checked once the ledger has data.

Which one, and N, is open question (c).

**Gate (exact form).** Per market, the share of **distinct recommended offers** in that base that have at least one variant with a `checkout_verified` deal on a lane that carries it. The threshold is **10.0%**. Each market passes or fails on its own: if US passes and SG fails, US goes ahead and SG waits (Q4). If US fails, stop here and put the effort into cross-seller effective-price comparison instead, which needs no coupons. Whether 10.0% and this denominator stand is part of open question (c).

**Phase 0b: reference carts on connected stores.** Verifying automatic discounts and published codes on connected stores means creating carts in merchant stores. Those carts are writes, however harmless, so they need their own approval (open question (d)), a prod slot, and the D3 cap. Phase 0b does not touch non-connected stores (Q2).

**Phase 1: US, Shopify, verified, display-only.**
- `observed_store_offers` and its per-variant verification table, the opt-out denylist and the daily cap counter.
- `merchant_published` extraction from text existing crawls already store.
- The verifier job (connected stores only, unless Q2's separate approval exists), under D3's egress rules.
- `estimated_effective_price` and `availableStoreOffers` on `get_offers`, with the `savings.v1` amendment. **No ranking effect.**
- The one permalink builder with `country=`, the click attribute and `discount=`.
- Code-on-click recording through `issue_click`, reconciliation, and the stop rule (D7).
- The four kill flags.

**Phase 2: measure, then widen.**
- Ranking on verified deals, only once the false-savings rate has been measured and only if open question (b) allows it.
- UCP verification for non-Shopify merchants.
- `authorized_feed` ingestion, once affiliate network access exists and open question (e) is answered.
- SG, if it passes the gate (Q4).

**Phase 3: channel-exclusive codes (D8)**, on a merchant commitment.

## Metrics

- **Verified-deal coverage:** per market, the gate's measure over the Phase 0 base.
- **False-savings rate:** issued codes, carried in the URL, that did not apply on a paid order, among reconciled orders. Target under 2%. **This is the metric that can stop the lane** (D7 stop rule). It counts finished orders only; the abandoned-checkout signal is reported beside it.
- **Median and p90 savings** per verified deal, in market currency, on the reference-cart base.
- **Click-out to order conversion** with and without a verified deal (A/B, Phase 2).
- **Verifier load:** carts per host per day, from the database counter. Alert at 80% of the cap.

## Alternatives considered

- **Real-time aggregator search (the original proposal).** Rejected: D1.
- **Testing codes on every checkout, browser-extension style.** Rejected for the same reasons as D1, and because merchants experience it as hostile.
- **Pivota applies the discount itself (infra-side pricing).** Rejected by ADR-022: it creates a second pricing engine, and the discount disappears on delegated checkout. It would also mean Pivota funding a discount, which ADR-016 rules out.
- **Verify once per published scope.** Rejected: codes routinely exclude sale items and collections, so one verified product says too little about the rest (D3).
- **No coupons, cross-seller price comparison only.** This is the fallback if the gate fails. It is also worth doing regardless, since multi-seller offers already exist.

## Consequences

- Buyers see fewer deals than an aggregator would show, and the ones they see work. This is the trade-off.
- Per-variant verification costs more carts than per-scope verification. The daily cap therefore limits coverage, and the verifier spends it on the variants we show most.
- Only connected stores can have a verified deal until Q2 or UCP widens it, and in Phase 2 only sellers in our affiliate programs join them. Those are also the sellers Pivota can attribute and earn on. This is why Phase 1 is display-only, and why ranking waits on open question (b) against ADR-025 Q4.
- `TestPromotionsLaneStaysDeleted` stays as is. Add new pins:
  - the serving modules that read `observed_store_offers` make no outbound HTTP calls (D5);
  - the click-out builder never emits `discount=` without `country=` and the click attribute (D7);
  - the verifier refuses hosts on the opt-out denylist and non-connected hosts while `DEAL_VERIFY_NONCONNECTED_ENABLED` is unset (D2, D3).

## Resolved questions (peng, 2026-10-29)

1. **Affiliate networks: `authorized_feed` stays in scope.**
   - Ingestion lands in Phase 2, once affiliate network access exists.
   - Network order reports would also give ADR-025's link 4 (order evidence) a source for non-connected merchants. Whether they carry the coupon code is not established (D7).
2. **Testing codes on non-connected stores: off until separately approved.**
   - Phase 0a answered only whether tokenless carts work, from documentation.
   - Until approval, the verifier refuses non-connected hosts (D3), and their deals stay unverified and unemitted.
3. **Showing unverified deals: off in v1.**
   - Only `checkout_verified` deals are emitted (D4).
   - Revisit with the measured false-savings rate, by amending this ADR.
4. **Markets: US only in Phase 1.**
   - Phase 0 measures US and SG.
   - SG is a Phase 2 candidate if it passes the gate on its own.

## Open questions for peng

These are strategic and are not decided here.

- **(a) ADR-022's rebuild trigger.** ADR-022 (Accepted) says a rebuild needs a real partner or merchant commitment, "not before". This ADR applies that trigger to D8 only. Either amend ADR-022 to say observed-deal display is not the gated rebuild, and why, or hold Phase 1 until a merchant commitment exists.
- **(b) Ranking and neutrality.** Only connected sellers, and later affiliate sellers, can have a verified deal, and those are the sellers Pivota earns on. ADR-025 Q4 says earning never moves a seller up or down. Decide whether verified deals may ever change ranking, and if so what "ranking" means (seller order within a product, or product order in a slate). The `savings.v1` amendment (D4) goes with this answer.
- **(c) The Phase 0 base and threshold.** Wait for the gateway ledger writer plus N days, or approve a named provisional base to be re-checked later. Confirm the 10.0% threshold and the "distinct recommended offers" denominator.
- **(d) Phase 0b.** Whether reference carts on connected stores are allowed. They are writes to merchant stores, so they need a slot and a cap.
- **(e) Affiliate sellers.** For an `authorized_feed` deal, does the code ride the network's deep link (ADR-025's carrier) rather than the permalink, and who checks each program's terms before code and click are combined?
- **(f) Q2's form.** What the "separate approval" for testing codes on non-connected stores consists of.
