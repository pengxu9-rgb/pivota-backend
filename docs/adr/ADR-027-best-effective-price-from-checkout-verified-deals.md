# ADR-027: Best effective price comes from checkout-verified deals, never from scraped coupon sites

**Status:** Proposed, 2026-10-29. Revised 2026-10-30 after review. Amended 2026-10-30 (Q5 ranking, Q11 coverage before checkout, Q12 code and attribution on one link) and 2026-10-31 (Q13 program permission to test, Q14 the network lane).
- peng resolved four questions on 2026-10-29, eight more on 2026-10-30 and two on 2026-10-31 (see "Resolved questions").
- Phase 0a ran on 2026-10-29. Its base was unusable, so the gate has not been computed (see "Phases and gates").

**Decision owner:** peng

**Builds on:**
- ADR-022 (promotions lane deleted). This ADR keeps ADR-022's invariant word for word, and **amends ADR-022's rebuild trigger** (Q5): showing observed, checkout-verified deals is not the gated rebuild, so it does not wait for a merchant commitment. Merchant-created codes (D8) still do.
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

ADR-022's concrete bullet says a deal renders only if **the quote layer** marks it predictable for the lane the order will use. On the click-out lanes there is no Pivota quote. On those lanes this ADR meets that bullet with the D3 reference cart for that variant and market, together with invalidation (D3) and the stop rule (D7). The residual risk is accepted: the buyer's real cart can still differ from the reference cart, which `subject_to_checkout: true` (D4) states and the stop rule watches. On the orchestrated lane the quote still decides (D7).

### What exists today and can be reused

- **Code validation against Shopify's checkout authority.** `services/shopify_storefront_pricing_service.py` already submits `discountCodes` on a Storefront cart and reads back `discountCodes { code applicable }` together with allocations. It uses `buyerIdentity.countryCode` / `@inContext(country:)` so pricing is in the market's presentment currency. Today this needs a storefront token from a **connected** store.
- **Showing deals honestly.** `docs/AGENT_SAVINGS_PRESENTATION_CONTRACT.md` (`savings.v1`) already has the groups we need:
  - `appliedStoreDiscounts`: proven by the quote;
  - `availableStoreOffers`: "metadata says it may apply", never shown as applied and never lowers the displayed price;
  - `cartUnlocks`.

  ADR-022 kept `savings_presentation_service` for exactly this. Showing an effective price for a verified deal is still a change to that contract (D4).
- **Carrying a code to the merchant.** Three paths compose Shopify cart links today, and none builds the link this ADR needs:
  - `routes/agent_shop_gateway.py::compose_attributed_destinations` is the main human click-out and `offers.resolve` composer. It adds the click attribute and pins `country=` to the buyer's **declared** market only (`_cart_country_pin`; never the listing market, and nothing when the market was defaulted). It feeds the `/r` redirect token and the published `cart_url`. It adds no discount;
  - `services/outbound_links_service.py::build_shopify_cart_permalink` adds `attributes[pivota_click_id]` and an optional `country=`, but no discount. Its callers are the A2A purchase route, the Reap cart-link lane and the cart-link preflight;
  - `routes/order_routes.py::_build_shopify_cart_permalink_best_effort` is the Pivota-orchestrated checkout's fallback when payment-intent creation fails. It takes multi-line carts, quantities above 1, `payment=shop_pay` and the buyer's own quote `discount_codes`, with no click id and no `country=`.

  D7 says which of these may carry a deal-lane code, and how.
- **Redirect links.** `/r` tokens are signed with a 7-day life, and an expired token still redirects to its signed destination without logging the click, so cached agent feeds keep working.
- **Polite crawling.** `services/crawl_politeness.py` (per-host pacing, robots.txt, 429 backoff), `services/crawl_global_pacer.py` and `services/shopify_edge_pacer.py`. All three keep their state inside one process or one run. None of them paces two jobs against each other, and none counts a daily quota.
- **Order evidence.** The Shopify `orders/paid` webhook and `external_conversion_poller` receive real order payloads for connected merchants. Those payloads carry the order's discount codes, but nothing records them today (D7 adds that).
- **Click minting.** `services/commerce_attribution_service.py::issue_click` is the one helper that mints click rows (ADR-025 D1). `IssuedClick` has no field for a discount code today.

### What does not exist

- **No affiliate-network import.** ADR-025 measured this on 2026-09-23, so we hold no network promotion feeds.
- **Few connected stores.** There are 17 connected Shopify stores and 4 Wix stores, against **328 checkout hosts** in the serving catalog (ADR-025). Any design that depends on a merchant connection covers a small minority of what we recommend.
- **No checkout webhooks.** We subscribe to Shopify `orders/*` topics only; no `checkouts/*` or `carts/*` topic.
- **No measurement of deal coverage, and no base to measure it on yet.** The recommendation ledger records only part of our traffic today. The gateway answers most searches itself and writes no ledger rows. The backend half of the fix is live with its ingest route dark, and the gateway's writer has not been built. Until both run, "share of recommendations" has no trustworthy denominator (Phase 0).

## Decision

### D1. No request-time search of third-party coupon sites. Aggregators are out of scope for v1.

Pivota does not query coupon aggregator sites while serving a request, and v1 does not crawl them at all.

1. **Quality.** Aggregator codes are frequently expired, single-use, customer-specific or leaked. A failed code at checkout costs more trust than showing no deal. We have not measured our own failure rate, and we will not bet the feature on someone else's.
2. **ADR-022.** An unverified third-party code cannot be surfaced under the invariant above. Verifying every scraped code means testing guesses against merchant checkouts, which is D3's problem multiplied by the size of the aggregator's database.
3. **Product and trust choice.** We do not use other sites' code databases; our deals come from merchants and from programs we belong to. Merchants are Pivota's customers, and merchants distrust any tool that does either of these:
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
| `authorized_feed` | B | An affiliate network's promotion feed for a program we belong to, limited to codes Q14 allows | Verified per variant after D3 (Q13); otherwise stored, **not emitted (v1)** |
| `channel_exclusive` | A+ | A code the merchant creates for AI-channel buyers, created in their Shopify by us (D8) | Verified, per variant (D3) |

These are **never** ingested:
- codes from aggregator sites (D1);
- codes visibly scoped to a person or audience: employee, influencer, student, customer-service or single-use codes. A code a network lists as assigned to Pivota is not one of these (Q14);
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

**Buyer eligibility.** An anonymous reference cart cannot test once-per-customer, first-order-only, segment-limited or total-usage-limited codes; Shopify checks those against the buyer, or the remaining uses, at checkout.
- In v1, any deal that is `new_customer_only`, `once_per_customer`, `segment_limited` or `usage_limited` **is not emitted at all**. It never ranks, never shows an effective price, and is not shown as information either.
- On a connected store, eligibility is read from the store's Admin discount object, using the `read_discounts` scope. A store that has not granted that scope gets no emitted deals.
- Where eligibility is **unknown** (no Admin read is possible, and the published terms say nothing), the deal is not emitted.
- A failure reason that shows a limit (used up, customer not eligible) sets the flag.

**Platforms:**
- **Shopify, connected store:** the existing Storefront cart path in `shopify_storefront_pricing_service`.
- **Shopify, not connected, but in an affiliate program we have joined (Q13):** that program's feed codes may be tested, under every rule below, once all of these hold: the tokenless-cart precondition below; the program's permission is recorded; and `DEAL_VERIFY_PROGRAM_ENABLED` (default off) is set. Q2's approval never applies to such a host.
- **Shopify, not connected, with no such relationship:** these deals stay stored and unverified until both of these are true:
  - tokenless Storefront carts are confirmed to support the cart mutations we need. Phase 0a answered this from Shopify's documentation only; it has not been tried against any store;
  - testing codes against non-connected stores with no program relationship (Q13) has been separately and explicitly approved by peng (Q2, Q10). **Testing is off until then.** The verifier refuses such hosts unless `DEAL_VERIFY_NONCONNECTED_ENABLED` (default off) is set, and the flag stays unset until that approval.
- **Other platforms:** a UCP `create_checkout` where the merchant exposes one (Phase 2). Otherwise the deal stays stored and unverified.

**Testing policy (a hard limit, enforced in code):**
- Only D2 sources are tested, and never for a seller on the opt-out denylist.
- Carts are abandoned, never checked out. No order is ever placed to test a code.
- At most `DEAL_VERIFY_MAX_CARTS_PER_HOST_PER_DAY` carts per host per UTC day (default small, e.g. 20). The counter lives **in the database**, because the verifier may run as several processes and several runs a day. The verifier reserves a slot before building a cart and stops for that host when the counter is spent.

**Egress (a hard limit):**
- The verifier is a **background job only**. It never runs in a request path, from a laptop, or from a developer worktree.
- It leaves by the **`pivota-crawl` subnet**, never by the default or payment-allowlisted egress.
- Its requests are **signed** with the bot-authentication mechanism Shopify recognises (Web Bot Auth or Shopify's own), not just an identifying user agent. Unsigned bots get Shopify's strictest limits.
- It is paced **jointly** with the other crawl jobs on that address. Either it runs inside the same run's `crawl_global_pacer` / `shopify_edge_pacer`, or it holds a **shared crawl lock** (in the database) that every crawl job on that address takes before it starts and honours while it runs. The lock works both ways: the verifier does not start while a crawl job holds it, and no crawl job starts while the verifier holds it. Pacing within one process is not enough, because other crawl jobs share the same exit address.

**Freshness:** a verification expires at the earlier of the deal's `ends_at` and `DEAL_VERIFY_TTL_HOURS` (default 24). **An expired deal stops being shown.** It goes back to unverified, which v1 does not emit. Nothing serves a deal on the strength of a past verification.

**Invalidation inside the TTL.** A verification holds only while the variant is unchanged. Serving shows an effective price only while both of these are true:
- the variant's current observed unit price, from the latest live verification or refresh in the same currency, equals the reference cart's pre-discount unit price;
- no change to the automatic discounts on the variant has been observed since the reference cart was built.

Any change to the variant's price, its compare-at (sale) state or the automatic discounts observed on it, and any report that the code is used up or disabled, sets that (deal, variant) back to `unverified` at once and queues a re-verify. The 24h TTL is a ceiling, not a promise.

### D4. Display follows evidence grade, and needs a `savings.v1` amendment

Showing an effective price, and later letting it rank, changes `savings.v1`:
- the contract says `availableStoreOffers` must not reduce the displayed product price;
- applied discounts may affect value only when the quote total already includes them.

This ADR therefore needs a `savings.v1` amendment (or a `savings.v2`), in two parts:
- **Display (Phase 1, needed whatever ranking does):** defines `evidence`, `estimated_effective_price` and its `basis`.
- **Ranking (Phase 2, only within Q6's limits):** states when a verified deal may change seller order within a product.

| State | `savings.v1` group | Allowed to |
|---|---|---|
| `checkout_verified` for this variant and market, within TTL and unchanged since (D3), where the request **declared** a buyer market equal to the verification market, on an offer whose lane carries the code (D7) | `availableStoreOffers` with `evidence: checkout_verified`, plus an `estimated_effective_price` on that offer's code-carrying link | Show "≈ $38.40 with GLOW10, checked 2h ago". **Phase 1 is display-only: no ranking effect.** Ranking is limited by Q6 |
| Verified for another variant only; the buyer's market was defaulted or differs from the verification market; or the offer's lane cannot carry the code | not emitted | — |
| Unverified (`merchant_published`, `authorized_feed`) | **Not emitted in v1** (Q3) | — |
| Eligibility-limited (new customer, once per customer, segment, usage-limited) or eligibility unknown | **Not emitted in v1** (D3) | — |
| Cart-shape dependent (minimum spend, above quantity 1, BXGY, free-shipping threshold) | `cartUnlocks` | Show progress only |
| Applied by an actual quote | `appliedStoreDiscounts` | Reduce `pricing.total`, exactly as today |
| `failed` / `expired` | not shown | — |

**The base of `estimated_effective_price`** is the reference cart's own pre-discount unit price for that variant, in the market currency. It is never the index price, which the Context shows is wrong too often. If the reference cart is older than the TTL, or the variant changed since (D3), there is no effective price.

**The arithmetic.**
- Effective price = the pre-discount unit price, minus every discount allocation on the quantity-1 reference cart, before tax and shipping.
- Allocations are read line by line and order-level allocations are read separately, and both are subtracted. Whether Shopify's cart subtotal already includes order-level allocations is checked when the verifier is built; the arithmetic does not rely on either answer.
- The saving credited to the code is only the code's own allocation. An automatic discount that applies anyway is shown as part of the price ("≈ $38.40: $2.00 automatic, $4.00 with GLOW10"), never as the code's saving.
- It is a price for **one unit**. A fixed-amount code verified at quantity 1 is never read as a per-unit saving at higher quantities, and serving never multiplies it.

`estimated_effective_price` always carries:
- `basis`: code, source, `verified_at`, `valid_until` (the verification's expiry), the variant verified, the reference-cart market, currency, pre-discount unit price and each allocation;
- `subject_to_checkout: true`, meaning the merchant's checkout decides the final amount.

An agent that caches a response must stop relaying the claim after `valid_until`.

It never replaces the quoted price, and `pricing.total` stays the only amount that is charged. This is the ADR-022 trapdoor closed by construction: nothing that reached display without checkout evidence, for that variant and that lane, can change a price.

**Resolved (Q3): unverified deals off in v1.**
- Unverified rows are still stored, and verified where D3 allows, but no serving response emits them, to buyers or to agents.
- Re-enabling them needs an amendment to this ADR, backed by the measured false-savings rate (Metrics).

### D5. Serving is a lookup, never a fetch

Deals reach `get_offers`, PDP and recommendation responses through a read of the store in D6, keyed by `(seller_id, market, variant)`. **No request path makes an outbound call for deals.** There is no request-time re-check, in any phase: a re-check would run in `web`, which leaves by the payment-allowlisted address.

Deals must be found before the buyer reaches the merchant's checkout, because a buyer who leaves checkout to look for a code is a conversion we may lose (Q11). The background verifier therefore spends the D3 cap in this order, and re-verifies before the TTL runs out:
1. the merchants and variants we send the most click-outs to, from the click rows;
2. then the variants most often shown.

### D6. Storage: observed store offers, not a promotions engine

A new table, `observed_store_offers`. It deliberately does not reuse the name `promotions` or any module name pinned by `TestPromotionsLaneStaysDeleted`. Those pins stay. One row per (seller, market, code-or-automatic-rule), plus `program_id` for `authorized_feed` rows:

- **Identity:**
  - `seller_id` (ADR-009);
  - `market` (ADR-026 registry; see "Market" below);
  - `code` (nullable for automatic discounts);
  - `platform_rule_ref` (when observable);
  - `program_id`, for `authorized_feed` rows: the affiliate program whose feed listed the code. An open code listed by two programs is two rows (Q13, Q14).
- **Economics, as published (metadata, never evidence):**
  - `kind` (`percent | amount | free_shipping | gift | bxgy`);
  - `value`;
  - `currency`, which is the **market** currency taken from the evidence. Never the shop default and never FX-converted (ADR-024);
  - `min_subtotal`;
  - eligibility: `new_customer_only`, `once_per_customer`, `segment_limited`, `usage_limited`, and whether eligibility is known;
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
  - `failure_reason`;
  - `authorized_by`: which permission allowed the test (`connected`, `q2_approval` or `program:<program id>`), for the audit trail.
- **Program permissions (Q13):** one row per affiliate program, holding the program, the network, our membership status, the source of the permission (program terms or the merchant in writing), its date, and the hosts it covers. A permission counts only while our membership is active. It is re-confirmed whenever the network reports a change to the program's terms, and in any case by a recorded re-confirmation date at most 90 days after the last one. That covers written merchant permissions and term changes the network does not report. Until it is re-confirmed, the permission is suspended.
- **Network links (Q14):** one row per (deal, variant, market), holding its id, the deal reference, the tracking-only network link and the coded network link, with when each was generated and when it expires. A regenerated link replaces the old one in the same row, so tokens already handed out reach the current **coded** link. The tracking-only link is frozen in each token as its signed `dest`, so an old token, including an expired one, keeps redirecting to the tracking-only link it was issued with. If the network later retires that link, such a click may go untracked or fail to land. That cost is accepted: it is the same as for every `/r` destination today, and that link never carries a code.
- **On the click row** (a schema change to ADR-025's click row; D7):
  - a new nullable `discount_code` field on `IssuedClick` and its click table, written by `issue_click` at issue time: the deal-lane code the link may carry;
  - a new `discount_carried` flag, filled by `/r` on that same row when the link is followed: whether `/r` actually appended the code.
- **Outcome (D7):**
  - `orders_seen`;
  - `orders_applied`;
  - `last_order_at`.

Writers are background jobs only, except program-permission rows, which a person records after reading the program's terms or the merchant's written agreement. Serving reads only.

**Market.** A market is never defaulted.
- **`merchant_published` rows:** crawls leave from one region, so a stored page is whatever version the store served there. The row's market comes from page evidence: the store's market context, a locale path, or the currency on the page. With no evidence, the row has no market. It is never verified and never served. It never falls back to US.
- **Connected-store rows:** the market comes from the ADR-026 registry or from the presentment currency. The `market` column on connected-store offers may hold a placeholder `'US'` for every row. That must be checked before D5 joins on it, and if it holds, D5 must not use it.
- **The buyer's market:** some request paths default the market to `'US'` when the request names none. A defaulted market is not a buyer's market. A deal is emitted only when the request **declared** a market and it equals the verification market. This is the same rule `_cart_country_pin` applies to the link (D7).

### D7. Lanes, application and reconciliation

#### Every discount is merchant-funded

Every discount this ADR shows is funded by the merchant and enforced by the merchant's checkout. Pivota never funds, subsidises, prefunds or rebates a discount (ADR-016).

#### The lane matrix

A verified deal may show an effective price only on a lane that both carries the code and has the evidence that lane needs. Everywhere else it is not emitted.

| Lane | Carries the code? | Evidence needed | v1 |
|---|---|---|---|
| **Pivota-orchestrated checkout** (the merchant's PSP charges; Pivota orchestrates, ADR-016) | Yes: the quote's `discount_codes` | The quote itself. The quote decides, and an applied code lands in `appliedStoreDiscounts` | Yes, for **order-level** codes only. Product-scoped codes on this lane are hard to write into the Shopify order (the order is written as a draft, or its receipt is suppressed), so a product-scoped deal-lane code is not offered here in v1 |
| **Orchestrated checkout's permalink fallback** (used when payment-intent creation fails) | Today: the buyer's quote codes, with no click id and no `country=` | The quote's own `checkout_url` where one exists | For an order carrying a **deal-lane** code: use the quote's `checkout_url`; with none, no fallback permalink is built for that order. The permalink has no click id, so it cannot meet the rule below. Buyer-entered codes keep today's behaviour |
| **Shopify cart permalink** (human click-out, through `/r`) | Yes: `?discount=CODE`, appended **only by `/r`** when the link is followed | Variant verified in the buyer's declared market (D3). The link pins that market with `country=` | Yes. The caller decides, the composer signs a deal reference, and `/r` appends the code (below) |
| **Direct `cart_url`** (no `/r` hop: `offers.resolve` hands the cart link to an agent; connected-store product cards publish it through `_seed_attribution_from_redirect`) | **No** in v1 | — | Carries no `discount=` and no effective price, because a link handed out directly cannot be recalled |
| **Coded network link handed out directly** (no `/r` hop) | **No** | — | Never handed out. A coded network link reaches a buyer only through `/r`'s click-time switch (Q14), because a link handed out directly cannot be recalled |
| **Referral link** (the card's `/r` destination is a product page, not a cart, for example when the purchasability gate declines the cart) | **No** | — | Not emitted. `/r` never appends a code to a referral destination |
| **Reap cart-link** (ADR-025 D6b) | **No.** `validate_cart_link` deliberately refuses `discount=`, and Reap's callers never pass a code | — | **Excluded.** Carrying a code there needs a change to Reap's allowlist, which is a separate decision with its own review |
| **UCP delegated checkout / Reap variant lane** | Not established | Per-merchant proof that a code can be passed and is applied | Excluded in v1. Revisit in Phase 2 with UCP verification |
| **Affiliate-network seller** (`authorized_feed`) | Only if the network's deep link can carry it | The carrier follows ADR-025's evidence route (network link with our click id as sub-id), plus written program terms allowing code and click together, plus that program's carrier check (Q14) | Excluded in v1. When added, the carrier is the network link (Q9, Q14) |
| **Handed-out `pdp_url`** (an agent passes the product page on) | No | — | No effective price. The effective price is attached to the code-carrying link only, never to the bare `pdp_url` |
| **Other click-outs** (non-Shopify web checkout; the buyer types the code) | Only by the buyer | A verification on that same checkout (UCP, Phase 2) | Excluded in v1 |

#### One decision at issue time, one re-check at click time

Two places touch a deal-lane code, each with one job:
- **Issue time.** The caller resolves the deal decision once per offer and passes it to `compose_attributed_destinations`. The decision needs database reads (the verification row, the stop row, the flags, the opt-out denylist). The composer stays pure and synchronous, as its contract already requires: `recovery_key` is passed in pre-resolved for the same reason. The composer uses the decision both to say whether the deal is shown on the offer (D4) and to sign a **deal reference** (deal, variant, verification market) into the `/r` token, so display and link cannot disagree. It adds:
  - `attributes[pivota_click_id]` from `issue_click`;
  - `country=<MARKET>` from `_cart_country_pin`, which pins the **buyer's declared** market and nothing else. Without it, the checkout's market follows the opener's IP address.

  It **never** writes `discount=` into the signed `dest` or the published `cart_url`. Both stay exactly as today, so `_seed_attribution_from_redirect`'s check that the recomposed link equals the signed `dest` still holds.
- **Click time.** **Only `/r` appends `discount=CODE`**, and only after it re-checks the deal (below).

The rule applies to **deal-lane codes** only: no deal-lane `discount=` on a click-out link without `country=` and the click attribute. On the network lane, our sub-id in the network's tracking link takes the place of the click attribute, and `country=` sits in the coded link's destination (Q14). Codes a buyer typed into a Pivota quote are not deal-lane codes, and their behaviour does not change.

**Links outlive decisions, so `/r` checks again.** In v1, a deal-lane code travels only on the `/r` redirect, never on a direct `cart_url`. When a `/r` link is followed, the redirect reads the token's deal reference and appends `discount=` only if all of these hold at that moment:
- the token has not expired;
- the signed `dest` is the Shopify cart permalink of the verified variant, pinned with `country=` to the verification market. `/r` never appends to a referral destination or to a warm-handoff `continue_url`;
- the verification is still valid (D3);
- no stop row exists and the flags allow it.

Otherwise it redirects without the code. So the kill switches and the stop row take effect on links already handed out, not only on new ones. An expired token still redirects to its signed `dest` for cached feeds, as today; since that `dest` never contains `discount=`, it never carries a deal-lane code.

**Migration (Phase 1).** The author of the Phase 1 deal-lane PRs owns this, reviewed through the PR controller:
- the callers of `compose_attributed_destinations` resolve the deal decision and pass it in; the composer signs the deal reference; `/r` re-checks and appends `discount=`;
- `_attach_connected_product_redirects`, which mints `/r` links on connected-store product cards, must pass the request's **declared** buyer market to the mint. Today its three callers (`find_products`, `find_products_multi`, `get_product_detail`) pass the request market to the purchasability gate only, and the mint gets no market and no `cart_country`. So the pin is unset, and under D4 no deal would ever be emitted on the one population Phase 1 can verify. This fails closed, but it must be fixed for Phase 1 to show anything;
- `build_shopify_cart_permalink` callers (A2A purchase, Reap cart-link, cart-link preflight) are unchanged; none passes a deal-lane code;
- the orchestrated fallback gets the matrix rule above. `_get_platform_checkout_fallback_url_best_effort` receives a bare list of codes today, so the quote records which of its codes are deal-lane codes. A code counts as one when it matches a verified deal emitted for that variant and market. The order keeps that marker, and the fallback's callers pass it in.

**Purchasability enforcement.** Under `MERCHANT_PURCHASABILITY_ENFORCE` (on in prod `web` when last measured, on 2026-09-27; not re-checked for this ADR), a connected Shopify card gets a cart permalink only with a fresh positive purchasability fact for its host and the buyer's market. The purchasability sweep has covered connected stores since 2026-09-28. Without such a fact the card is a referral link, which carries no code, so no deal is emitted on it. Phase 1's reach is therefore connected Shopify stores that have a fresh positive fact in the buyer's declared market. That is a subset of the connected-store share Phase 0 reports.

#### Recording the code, and reconciling (Phase 1)

- **Recording.**
  - At issue time, ADR-025's `issue_click` writes the deal-lane code the link may carry into the new `discount_code` field on the click row (D6). This includes network-lane tokens (Q14).
  - When the link is followed, `/r` fills that same row, as it already fills the issued click today, with `discount_carried`: whether it actually appended the code.
  - This is `/r` completing the issued row, not a second writer. An expired token is not logged, so its click has no `discount_carried`.
- **Reconciling.** When order evidence returns (ADR-025 link 4), the order's discount codes are compared with the issued code:
  - applied: increments `orders_applied`;
  - a **failure signal** is an order that meets all of these: it is paid; it came from a click where `/r` recorded `discount_carried = true`; it contains the verified variant; and the code is absent from it. Clicks where `/r` dropped the code, because the verification had lapsed (it lasts at most 24h, a token 7 days) or a stop row existed, are never failure signals. So a cleared stop row is not re-tripped by its own stripped clicks. Eligibility-limited codes are never emitted in v1 (D3), so a returning customer's ineligibility cannot cause one;
  - not counted: orders without the verified variant; orders whose cart changed shape so that another discount won instead (an extra item unlocking a better automatic discount); and, where order evidence shows the landing parameters, orders where the buyer removed the code;
  - two failure signals within the TTL demote that variant's verification to `failed`, pending re-verification.
- **Coverage.** Reconciliation covers only merchants whose orders reach us (about 21 connected stores today), plus network programs whose conversion reports show the code an order used (Q14). Whether a given network's report carries the code is checked per program before any code is emitted on it (Q14, step 4).
- **Abandonment.** Paid orders count only the buyers who finished. A buyer whose code failed and who then left produces no order, so the stop rule below is **survivor-only** and biased towards not firing. A signal that counts those buyers would need new `checkouts/*` webhook subscriptions on every connected store, and probably a customer-data review, since checkout payloads carry contact details. That is not in Phase 1. The stop rule compensates with an absolute trigger and a weekly human review instead.

#### Kill switches

Four independent flags, all default off, each able to stop its part alone. The two testing-scope flags in D3, `DEAL_VERIFY_NONCONNECTED_ENABLED` and `DEAL_VERIFY_PROGRAM_ENABLED`, are also default off.
- `DEAL_LANE_EMIT_ENABLED`: serving emits deals at all;
- `DEAL_LANE_RANKING_ENABLED`: deals may change seller order within a product (stays off through Phase 1; Q6);
- `DEAL_LANE_DISCOUNT_PARAM_ENABLED`: `/r` appends `discount=`, or on the network lane switches to the coded link (Q14);
- `DEAL_VERIFY_JOB_ENABLED`: the verifier job runs.

**Stop rule (automatic).**
- **Cadence:** a scheduled check runs **hourly**, over the last **14 days**, **per market**. A stop row stops that market only.
- **Rate trigger:** once the window has at least **50 reconciled orders** carrying an issued code, a rate of **2% or higher** writes a stop row. At 50 orders that is a single failure. **This hair trigger is intended**: a stop fails safe, because it only hides deals until a person looks.
- **Absolute trigger:** **3 or more failure signals in 14 days** writes a stop row at any volume, armed or not.
- **Before the rate trigger arms:** while the window has fewer than 50 reconciled orders, a person reviews every failure signal **weekly**. Expected volume from about 21 connected stores may keep the rate trigger unarmed for a long time; the absolute trigger and the weekly review are the guards until it arms.
- **Effect:** serving, the deal decision its callers pass to `compose_attributed_destinations`, and the `/r` re-check read the stop row. While it exists they behave as if `DEAL_LANE_EMIT_ENABLED`, `DEAL_LANE_RANKING_ENABLED` and `DEAL_LANE_DISCOUNT_PARAM_ENABLED` were off, including for links already handed out.
- **Alerting:** writing a stop row alerts the deal-lane owner and the on-call channel. Only a person clears it, after review.
- **Time to detect:** the order-evidence lag (webhook: minutes; poller: up to its interval), plus up to one hour for the check. A failure can therefore stop the lane within a few hours of the order, once it meets a trigger.

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

**Phase 0 base (dependency, Q7).** The gate is computed only on the recommendation ledger, once the gateway's ledger writer is built and switched on and has recorded **7 full days**. No provisional base is used.

**Gate (exact form).** Per market, the share of **distinct recommended offers** in that base that have at least one variant with a `checkout_verified` deal on a lane that carries it. The threshold is **10.0%**. Each market passes or fails on its own: if US passes and SG fails, US goes ahead and SG waits (Q4). If US fails, stop here and put the effort into cross-seller effective-price comparison instead, which needs no coupons. peng confirmed the threshold and denominator (Q7).

**Report two numbers beside the gate.** In Phase 1 only connected stores can be verified (Q2; Q13 adds joined programs' feed codes from Phase 2), and connected stores are a small minority of checkout hosts, so the gate can fail just because few recommended offers come from connected stores. Phase 0 therefore reports, per market:
- the **verified share**: the gate's measure (connected stores only, in Phase 1);
- the **published-code share**: distinct recommended offers whose seller has a published code in stored text (all sellers);
- the share of distinct recommended offers that come from connected stores.

That shows peng whether a failed gate means few deals or few connections.

**Phase 0b: reference carts on connected stores.** Verifying automatic discounts and published codes on connected stores means creating carts in merchant stores. Those carts are writes, however harmless, so each run is a one-off prod job that needs a controller slot and stays under the D3 cap (Q8). Phase 0b does not touch non-connected stores (Q2).

**Phase 0: prepare Q2's approval request now (Q11).** Most of the stores we send buyers to are not connected, so Phase 1 alone leaves most guest checkouts without a code. The request for Q2's separate approval, which now covers only stores with no program relationship (Q13), is prepared during Phase 0, not after Phase 1. Phase 0 also reports, per market, the merchants with the most click-outs and whether each is connected, so the first stores verified after an approval are the ones that matter most.

**Phase 1: US, Shopify, verified, display-only.**
- `observed_store_offers` and its per-variant verification table, the opt-out denylist and the daily cap counter.
- `merchant_published` extraction from text existing crawls already store.
- The verifier job, under D3's egress rules. Connected stores only, unless Q2's separate approval exists for stores with no program relationship. Joined programs' feed codes (Q13) come in Phase 2.
- `estimated_effective_price` and `availableStoreOffers` on `get_offers`, with the display part of the `savings.v1` amendment. **No ranking effect.**
- Invalidation on price, sale-state, automatic-discount or code-status change (D3), and `read_discounts` eligibility reads on connected stores.
- The deal decision resolved by the callers of `compose_attributed_destinations`, the signed deal reference, the `/r` re-check that appends `discount=`, the declared buyer market on connected-store product cards, and the orchestrated-fallback rule (D7 migration).
- The `discount_code` and `discount_carried` fields on the click row (written by `issue_click`, filled by `/r`), reconciliation, and the stop rule with its absolute trigger and weekly review (D7).
- The four kill flags and the shared crawl lock.

**Phase 2: measure, then widen.**
- Ranking on verified deals, within Q6's limits: seller order within a product only, and only once the false-savings rate has been measured.
- UCP verification for non-Shopify merchants.
- `authorized_feed` ingestion, once affiliate network access exists, carried on the network link (Q9), under Q13's testing permission and Q14's per-program rules.
- SG, if it passes the gate (Q4).

**Phase 3: channel-exclusive codes (D8)**, on a merchant commitment.

## Metrics

- **Verified-deal coverage:** per market, the gate's measure over the Phase 0 base.
- **Click-out coverage:** per market, the share of click-outs whose link carried a verified code (`discount_carried = true`). Click-outs on both the Shopify cart-permalink lane and the network lane count; on the network lane, a click-out carried a code when `/r` served the coded link (Q14). It is reported overall and for the merchants with the most click-outs (Q11).
- **Both-carried rate:** among reconciled orders from clicks where `/r` appended the code, the share whose order shows both the code and our click attribute. The first such orders are also checked by hand (Q12).
- **False-savings rate:** codes `/r` appended (`discount_carried = true`) that did not apply on a paid order, among reconciled orders. Target under 2%. **This is the metric that can stop the lane** (D7 stop rule). It counts finished orders only, so it is survivor-only (D7).
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
- Per-variant verification costs more carts than per-scope verification. The daily cap therefore limits coverage, and the verifier spends it on the merchants and variants we send the most click-outs to (D5).
- Only connected stores can have a verified deal until Q2 or UCP widens it, and in Phase 2 only sellers in our affiliate programs join them. Those are also the sellers Pivota can attribute and earn on. This is why Phase 1 is display-only, and why Q6 limits ranking to seller order within a product, never product order in a slate.
- `TestPromotionsLaneStaysDeleted` stays as is. Add new pins:
  - the deal-read module (the code that reads `observed_store_offers` for serving and for `/r`) makes no outbound HTTP calls (D5). `/r`'s warm handoff, which does, is outside it;
  - `compose_attributed_destinations` never writes `discount=` into the signed `dest` or the published `cart_url` (D7);
  - `/r` never appends a deal-lane `discount=` without `country=` and the click attribute (on the network lane, the sub-id), to a referral or warm-handoff destination, on an expired token, or when the declared market differs from the verification market or the market was defaulted (D7);
  - a direct `cart_url` never carries a deal-lane `discount=` (D7);
  - a click where `/r` dropped the code is never counted as a failure signal (D7);
  - a price or sale-state change on a verified variant drops its effective price (D3);
  - the verifier refuses hosts on the opt-out denylist, and non-connected hosts with no program relationship while `DEAL_VERIFY_NONCONNECTED_ENABLED` is unset (D2, D3);
  - the verifier refuses a host in a joined program unless its program permission is recorded, `DEAL_VERIFY_PROGRAM_ENABLED` is set and the code came from that program's feed. `DEAL_VERIFY_NONCONNECTED_ENABLED` never authorizes such a host (Q13);
  - on the network lane, `/r` redirects to the coded network link only after its re-check passes, and otherwise to the tracking-only link. The token never contains the coded link or the code. `/r` loads the coded link by the network-links row id signed in the token, and uses it only when the row's deal reference matches the token's (Q14);
  - a network-links row is rejected at write time unless the coded link's host is that program's network tracking host, and its embedded destination is exactly the verified variant's cart permalink with the deal's code and `country=` for the verification market (Q14);
  - `/r` uses a coded link only when its host equals the signed `dest`'s host, and its embedded destination, parsed the same way as at write time, is exactly the cart permalink of the variant in the token's deal reference, with that deal's code and `country=`. Otherwise, and when the row is missing, its deal reference does not match or the coded link is past its expiry, `/r` goes to the tracking-only link (Q14);
  - a coded network link is never handed out except through `/r` (Q14).

## Resolved questions (peng, 2026-10-29)

1. **Affiliate networks: `authorized_feed` stays in scope.**
   - Ingestion lands in Phase 2, once affiliate network access exists.
   - Network order reports would also give ADR-025's link 4 (order evidence) a source for non-connected merchants. Whether they carry the coupon code is not established (D7).
2. **Testing codes on non-connected stores: off until separately approved.**
   - Phase 0a answered only whether tokenless carts work, from documentation.
   - Until approval, the verifier refuses non-connected hosts (D3), and their deals stay unverified and unemitted.
   - Narrowed by Q13 (2026-10-31): Q2 now covers only stores with no relationship to Pivota.
3. **Showing unverified deals: off in v1.**
   - Only `checkout_verified` deals are emitted (D4).
   - Revisit with the measured false-savings rate, by amending this ADR.
4. **Markets: US only in Phase 1.**
   - Phase 0 measures US and SG.
   - SG is a Phase 2 candidate if it passes the gate on its own.

## Resolved questions (peng, 2026-10-30)

5. **ADR-022's rebuild trigger: amended.**
   - Showing observed, checkout-verified deals is not the rebuild ADR-022 gates. It adds no pricing engine and keeps ADR-022's invariant: only a deal the merchant's checkout applied, for that item, market and lane, is shown.
   - Phase 1 therefore does not wait for a merchant commitment.
   - The trigger still applies to merchant-created codes (D8, Phase 3).
   - Phase 2 ranking, within Q6's limits, needs no merchant commitment either: it stays within one product, adds no pricing engine, and what Pivota earns never enters it. The Phase 2 ranking part of the `savings.v1` amendment (D4) still applies.
6. **Ranking: off until measured, then within a product only.**
   - Phase 1 is display-only.
   - Ranking may start only after the false-savings rate has been measured, meaning at least 50 reconciled orders in the window, and is under the D7 stop threshold.
   - When on, a verified deal may change only the **order of sellers within one product**, by the buyer's effective price. It never changes the order of products in a recommendation slate.
   - What Pivota earns on a seller never enters the ranking (ADR-025 Q4). Effective price is a fact about the buyer's cost, not about our relationship with the seller.
7. **Phase 0 base: wait for the ledger.**
   - The gate is computed on the gateway's recommendation ledger after 7 full days of data. No provisional base.
   - The threshold stays 10.0% of distinct recommended offers, per market.
8. **Phase 0b: allowed, capped.**
   - Reference carts on connected stores may be created.
   - Each run is a one-off prod job with a controller slot, under the D3 per-host daily cap. Carts are abandoned, never checked out.
9. **Affiliate sellers: the network link carries the code.**
   - An `authorized_feed` deal follows ADR-025's carrier: the network's tracking link, with our click id as sub-id. The code rides that link where the network supports it; otherwise the deal is not emitted on that lane.
   - Before a code and a click are combined for a program, that program's terms must allow it in writing.
10. **Q2's separate approval.** Its form is recorded by peng outside this ADR. The public wording stays: testing codes on non-connected stores needs a separate, explicit approval, and `DEAL_VERIFY_NONCONNECTED_ENABLED` stays unset until then. Narrowed by Q13: it covers only stores with no relationship to Pivota.
11. **Coverage before guest checkout.**
   - A verified code must already be on the link when the buyer reaches the merchant's checkout. A buyer who leaves to search for a code may not come back.
   - The verifier puts the merchants and variants we send the most click-outs to first (D5).
   - Testing codes on non-connected stores with no program relationship still needs Q2's separate approval, and `DEAL_VERIFY_NONCONNECTED_ENABLED` stays unset until then. Joined programs are covered by Q13. The approval request is prepared now, during Phase 0, so coverage can widen as soon as it is given.
12. **The code and our attribution travel on one link.**
   - A deal-lane code is emitted only on a link that also carries Pivota's attribution: the click attribute on a Shopify cart permalink (D7), or the network's tracking link with our click id as sub-id (Q9).
   - Where a lane can carry our attribution but not the code, the link carries the attribution only, and the deal is not emitted on that lane. We never trade attribution for the code.
   - Those lanes are listed as work to do, each with its own review: the Reap cart-link, whose allowlist refuses `discount=` (D7), and any affiliate network whose tracking link drops it (Q9).
   - That both arrive together is checked on the first reconciled orders (Metrics: both-carried rate).

## Resolved questions (peng, 2026-10-31)

13. **An affiliate program we have joined counts as permission to test its codes.**
   - We treat being an approved publisher in a merchant's program as that merchant's permission to test that program's feed codes, where the program's terms or the merchant in writing allow automated checks. The permission is recorded per program, in the database, with its source and date (D6).
   - Testing then follows every D3 rule, including: the tokenless-cart precondition; the per-host daily cap; the crawl lock and joint pacing; the crawl egress; signed requests; background jobs only; carts never checked out; the opt-out denylist; the eligibility rules (an eligibility-limited or unknown code is not emitted); and the TTL with its invalidation.
   - **What the check is keyed on.** The verifier tests a code on a non-connected host under this permission only when all of these hold:
     - the code came from that program's feed (Q14), as recorded on its deal row (`program_id`, D6);
     - that program's permission is recorded and current (D6);
     - `DEAL_VERIFY_PROGRAM_ENABLED` (default off) is set.

     A `merchant_published` code, or another program's code, on the same host does not pass.
   - **Q2 never applies to a host in a joined program.** Such a host is tested only under its program's recorded permission. `DEAL_VERIFY_NONCONNECTED_ENABLED` never authorizes it, so a program whose terms forbid automated checks is never tested, whatever Q2's approval says.
   - Q2's separate approval now covers only stores with no relationship to Pivota.
   - Each verification records which permission authorized it, Q2 or Q13 (D6).
   - This testing lands with `authorized_feed` ingestion, in Phase 2.
14. **The network lane: which codes, and how the link is checked.**
   - **Which codes.** Only codes that the network itself lists for our account, for a program we have joined. In this ADR, "the program's feed" means that listing, whether the network provides it as a feed or as a promo-code listing. The codes must be inside their start and end dates, and either assigned to Pivota or open to every publisher in the program. A code assigned to Pivota counts only once the network lists it for our account; a code sent to us any other way is not used until it is listed. Never:
     - a code assigned to another publisher. Networks may credit an assigned code to its owner, even without a click, or reverse the sale;
     - a code from outside the network, such as one from the merchant's own site. Q13 tests only feed codes, so such a code could never be verified for this lane;
     - a code whose assignment the network does not state. Where a network's feed has no such field, we ask the program for a code assigned to Pivota, or emit none.
   - **Which code when two apply.** We ask each program for a code assigned to Pivota, because such a code cannot be credited to another publisher. When our code and an open code both apply to an offer, ours is used only when the buyer's verified price is the same or better. The buyer's price comes first (Q6).
   - **How we apply.** We describe Pivota as what it is: an AI shopping assistant that compares sellers and shows offers the merchant provides. We do not declare a coupon or deal model only to obtain codes, and we do not hide code use behind another category. A program that excludes coupon or deal publishers gets the tracked link only, never a code (Q12).
   - **The network lane goes through `/r` too.**
     - For each (deal, variant, market), a background job generates two network links through the network's link interface, and stores them in one network-links row (D6):
       - a tracking-only link;
       - a coded link whose destination is the verified variant's cart permalink, carrying the code and `country=` for the verification market.

       The job regenerates both before the network's links expire, in the same row. No deal is emitted on this lane until both links exist.
     - **Our sub-id is added per click.** The links are generated before any click exists. So at issue time, our click id from `issue_click` is appended to the tracking-only link as the network's sub-id parameter. At click time, `/r` appends the same sub-id to the coded link, taken from the signed token, never from the request's query string. Carrier steps 1 and 2 confirm that the network accepts the sub-id appended this way, without regenerating the link, and that the code stays intact.
     - **The token never holds the coded link or the code.** `/r` tokens are signed but not encrypted, so anything in them can be read by whoever holds the URL.
       - The signed `dest` is the tracking-only link with our sub-id. It carries no code.
       - The token also carries the deal reference and the network-links row id.
       - **Checked when a row is written.** The background job writes a row only from the network's own link interface, after carrier steps 1 and 2 pass for that program. Each row is also checked on its own: it is rejected unless the coded link's host is that program's network tracking host, and its embedded destination is exactly the verified variant's cart permalink with the deal's code and `country=` for the verification market.
       - **Checked when a link is followed.** `/r` loads the coded link from that row. It uses it only when all of these hold: the row's deal reference matches the token's; the coded link's host equals the signed `dest`'s host; its embedded destination, parsed the same way as at write time, is exactly the cart permalink of the variant in the token's deal reference, with that deal's code and `country=`; and the coded link is not past its expiry.
       - **Fallback.** A missing row, a mismatched deal reference, a different host, a different embedded destination or an expired coded link sends the buyer to the tracking-only link. So a bad row cannot send a buyer through `/r` anywhere but the verified variant's cart, by way of that program's network tracking host.
     - When the link is followed, `/r` runs the same re-check as D7: an unexpired token, a valid verification, a declared buyer market equal to the verification market, no stop row, and the flags allow it (`DEAL_LANE_EMIT_ENABLED` and `DEAL_LANE_DISCOUNT_PARAM_ENABLED`). Only then does it redirect to the coded link.
     - Otherwise it redirects to the tracking-only link. An expired token goes to the tracking-only link.
     - `issue_click` writes `discount_code` and `/r` fills `discount_carried` as on the Shopify lane, so the kill switches, the stop row and the reconciliation rules (D7) cover this lane as well.
     - A coded network link is never handed out directly (D7 lane matrix).
   - **Before any code is emitted on a program** (once per program):
     1. The deep link is generated through the network's own link interface, without a click, and it confirms that deep links to the exact destination host are allowed. Some networks silently send a disallowed deep link to the home page, so the code would never load.
     2. The generated coded link still carries the destination's code intact.
     3. One test click confirms that the network's click identifier reaches the merchant's landing page.
        - It runs as a one-off job on the crawl egress with a controller slot. It uses a coded link built exactly as `/r` builds one: from a row that passes the write-time check, with a test sub-id appended the same way. Opening the coded cart permalink creates a cart on the store, so the test click:
          - reserves a slot under the per-host daily cart cap (D3);
          - requires that program's recorded and current permission and `DEAL_VERIFY_PROGRAM_ENABLED` (Q13);
          - is refused for a seller on the opt-out denylist (D2). It does not go through `/r`, writes no click row, stays out of the click-out coverage metric, and is never followed by a purchase.
        - **If the identifier does not arrive, the program gets tracking-only links.** Without the click identifier the network cannot tie an order to our click, so neither the hand check nor the D7 failure signal could be computed for its orders.
     4. For each order, the network's conversion report must show our sub-id, the code the order used, and enough line-item detail to tell whether it contains the verified variant. These are what the D7 failure signal needs. If the report cannot show all three, the program's orders cannot be reconciled, and its codes are not emitted.
   - **After the first codes are emitted on a program:**
     - The first conversions from clicks where `/r` sent the coded link are checked by hand. Each must show both our sub-id and the code (Metrics: both-carried rate). If one shows either missing, that program goes back to tracking-only links until a person reviews it.
     - From then on, the network's conversion reports are this lane's order evidence. Their failure signals, defined as in D7, are counted per program and feed the D7 stop rule for the market, in the same way as connected stores' orders.
   - Agreements made directly with merchants are a separate, later decision.
