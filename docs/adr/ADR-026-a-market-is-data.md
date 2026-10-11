# ADR-026: A market is data — one registry, four separate axes, per-market prices and eligibility

**Status:** **Accepted, 2026-10-11.**
- Peng decided the four open questions (see "Resolved questions").
- The coverage/drain lane, which owns the drain, reviewed every ingestion and drain line against drain `a9b6396d3`; its review is merged.
- Appendix B holds its price-mix measurement (160 stores, 2026-10-11).

**Extends:** ADR-024. Every ADR-024 commitment stands unchanged:
- sibling offers, never rewrites;
- serving gates on currency;
- no FX conversion reaches a buyer;
- a market with no local supply gets an honest empty answer.

This ADR finishes ADR-024's Phase 2b. It also adds what ADR-024 left out: how the system grows from 2 served markets to N.

## Context

Two markets are served today: US and SG. Each was added by editing code and environment variables in many places, and the third will cost more than the second.

### Where market is hard-coded (code inventory, both repos on main, 2026-10-11)

**Lists and maps that must agree:**
- About 7 market → currency maps:
  - `region_pricing.py:53`
  - gateway `buyerRegion.js:38`, a hand-kept mirror of `region_pricing`
  - `agent_commerce_reap.py:616`
  - `employee_products.py:1410`
  - gateway `commerceFacts.js:3`
  - `travelLocalProductAuthority.js:84`
  - a census script
- 3 code lists: `INGEST_MARKETS`, `CAPTURE_MARKETS`, `ACQUISITION_MARKETS`.
- 4 list env vars:
  - `PIVOTA_SERVING_PRICING_REGIONS`, read at import by every backend writer and by the drain;
  - `CREATOR_CATEGORIES_EXTERNAL_SEED_MARKET`;
  - `BUYER_MARKET_OFFER_SCOPE_MARKETS`;
  - `DISCOVERY_BUYER_MARKET_WARM_MARKETS`.
- About 6 scalar single-market env vars: `AGENT_PDP_SERVING_MARKET`, `RELGRAPH_MARKET`, `EXTERNAL_SEED_MARKET`, …

**Code that assumes exactly one or two markets:**
- `pipeline.py:300` tests `!= "SG"`.
- `offer_classification.py:87` `select_best_us_offer`.
- `canonicalPdpOwnMoney.js:75` tests `market==='US' && currency==='USD'`.
- `discoveryFeed.js:9728` has a US/USD ternary.
- `server.js:19215` and `:47734` collapse the market list to its first element.
- `buyerMarket.js:73` returns null when given two markets.

**Silent fallbacks that are live today:**
- Gateway `commerceFacts.normalizeMarketId('GB')` returns `'US'`, so a GB buyer is validated against USD.
- `market.js` is `z.enum(['US','JP'])`, so `normalizeMarket('SG')` returns `'US'`.
- The FX default tables (`agent_shop_gateway.py:827`, `policy.js:266`) have no SGD or AUD.

**Storage:**
- `catalog_offers.market` defaults to `'US'`, and only one lane writes it.
- `external_product_seeds.market` is a read partition, and every row in it is `'US'`. SG seeds sit in that partition and are found by currency, which amounts to inferring market from currency.
- `index_pipeline_state.serving_eligible` is a single boolean: an OR over the served regions, built once at import. A product priced only in USD therefore reads "eligible" for SG, and every reader has to drop it again.
- `catalog_offers` has no tax, shipping or duties columns.

### Supply: sellers that sell across borders are the normal case

- **SG nail-polish discovery (2026-10-11).** Of the 36 Shopify stores whose cart quotes SGD to SG, 16 (44%) are cross-border:
  - by base currency: 7 USD, 3 GBP, 3 AUD, 2 CAD, 1 EUR;
  - One of them is an `.sg` domain whose store is AUD/AU.
- **Coverage wave A (2026-10-10).** 327 of 1,780 claimed (brand, store) pairs were lost to `currency_mismatch`.
- **Pending acquisition.** In the drain lane's SG checkout-currency batch, 65 pairs are held because their base currency (CAD/GBP/KRW/HKD/EUR/SEK/NZD) has no ingest market. A `shopify_markets` capture needs a prior base crawl in an ingest market.

### What is already wrong in prod (drain lane, measured 2026-10-11)

- **Checkouts landing in the wrong currency.** 5 of 157 US checkouts that landed a price landed in GBP (4) or ZAR (1), on stores we list to US buyers in USD. `CHECKOUT_MARKET_MISMATCH` fired 3 times.
- **Mis-stamped offers:**
  - 10 US-market offers are priced in JPY;
  - 5 GB-market offers are priced in USD;
  - 1,084 SGD seeds sit in the US seed partition.
- **Drift is effectively unmonitored.** Only 2 of 197 purchasability rows carry `expected_price_minor`, and `checkout_preflight_observations` has had 0 rows in 30 days.
- **Effectively one currency per product.** Only 24 of 24,746 products carry more than one currency.
- **The per-market capture has never produced an offer.** There are 0 live `shopify_markets_*` sibling offers, and no capture job has ever completed.
  - `shopify_markets._Session.json` treats a content type without "json" as a bot wall (~:569, `_is_json` ~:292).
  - Shopify serves `/products/<handle>.js` as `text/javascript` from the crawl egress (88 of 90 stores).
  - The unit tests' MockTransport answered `application/json`, so the tests passed against a response the real stores never send.
  - So every US capture since 09-26 and every SG capture under #2553 would have failed with `capture_unverifiable`.
  - The drain lane has paused captures and is fixing it.
- **Price-mix measurement (Appendix B, 160 stores).** `?country=<m>` with the same-response `cart_currency` cookie finds market pricing more completely than the `/localization` + `/cart.js` session (24 vs 17 SGD stores) and agrees with it to the cent. 19% of stores disallow the cart paths in robots.txt. One store's un-pinned read is presented in QAR while `/meta.json` says USD.
  - This revises the 2026-08 finding, quoted in `shopify_markets.py`, that "`?country=` is never evidence". That finding read the payload alone, which has no currency field. The same-response cookie supplies the currency.

Two further facts:
- `products.json` and `/products/<handle>.js` carry no currency field. A geo or IP redirect to a localized path can therefore change the numbers we read without telling us.
- `shopify_markets` proves currency with the cart in the same session (`/cart.js`). The base crawl does not.

## Decision

### 1. Four separate axes, plus a capability record per (storefront, market)

Code currently blurs four separate things. From now on each is its own field, and none is ever inferred from another:

| Axis | Question | Example |
|---|---|---|
| **Seller home** | Where is the store? | an `.sg` domain whose store is in AU |
| **Buyer market** | Where is the shopper? (set on the request) | SG |
| **Offer currency** | What does checkout charge, in this buyer market's session? | SGD |
| **Fulfilment reach** | Does the store ship to the buyer market, and how? (domestic, cross-border, duties) | ships to SG, cross-border |

Rules:
- **Never infer market from currency, or currency from market or language.** The registry (decision 2) says which currency a buyer market expects. A store proves which currency it charges.
- **"Priced for the market" is not "buyable from the market."** ADR-024 deliberately gated only on the first. This ADR makes the second into data (decision 3) and adds it as a further gate, without redefining the first.

### 2. The market registry: one source, read by both repos

There is one checked-in registry; `services/region_pricing.py` grows into it. Its entries:

```
market: SG
currency: SGD
status: served            # off | acquisition | shadow | served   (rollout states, below)
locales: [en-SG]
price_display: tax_inclusive
checkout_rails: [reap]    # rails, never custody (ADR-016)
capture_methods: [shopify_markets, storefront]
partner_label: Singapore
```

- **Everything market-shaped derives from it:**
  - `INGEST_MARKETS`, `CAPTURE_MARKETS`, `ACQUISITION_MARKETS`, the served regions, the Reap currency map, the CSV-import map;
  - the gateway's `buyerRegion` / `servedMarkets` / `commerceFacts` / `market.js`.
- **The gateway gets the registry as a generated JSON artifact** that CI checks byte-for-byte against the backend's. It never keeps a hand-written mirror.
- **Unknown markets raise.** An unknown market code raises, as `region_pricing` already does today. It never falls back to US.
- **No per-market flags.** Rollout state is a registry field, not `ENABLE_MARKET_JP` or `*_SG_*` env vars. Feature flags stay for features and are never per market.

**Rollout states:**

| State | Meaning |
|---|---|
| `off` | Unknown to ingestion and serving. |
| `acquisition` | Ingest and store truthfully; never serve. This is what AU and JP are today. |
| `shadow` | Serve to internal and partner test traffic only, with full tripwires. |
| `served` | Live. |

Moving a market between states is a reviewed one-line registry change plus the launch checklist (decision 7).

### 3. The data model: product, listing, market offer, capability, eligibility

| Entity | Key | Market-specific? | Today |
|---|---|---|---|
| Product | content_key | no | as is |
| Listing | (storefront, product) | no | `ext:retailer:` keys, as is |
| **Market offer** | (listing, buyer market) | **yes** | `catalog_offers`, with `market` as the declared destination (ADR-024) |
| **Storefront × market capability** | (storefront domain, market) | yes | **new**: ADR-024's planned `merchant_region_support` |
| **Product × market eligibility** | (content_key, market) | yes | **replaces** the single `serving_eligible` boolean |

- **Each market offer gains** `tax_included`, `fulfilment` (domestic / cross_border), `ships_from`, `captured_at`, `capture_method`, and a `proof_ref` pointing at the session evidence (the cart currency observed, the localization used).
  - Price, availability and currency must all come from the same session for that market.
  - Shipping cost and duties stay unmodelled until a rail needs them, but are declared as columns from the start.
- **Each capability row records:** `ships_to` (from `/meta.json` and `/localization`), `markets_enabled`, `checkout_currency` (proved by `/cart.js` in a session for that market), `tax_included`, `domestic` (seller home = market), `checkout_rails` available (UCP door, Reap), `verified_at`, and `method`.
  - Ingestion schedules from it, serving joins it, and the purchasability sweep refreshes it.
- **Eligibility becomes one row per (content_key, market)**, with that market's blocker code: ADR-024 Option D, deferred while there was one market, now justified.
  - The stored boolean stays as the US projection until every consumer has moved. This is ADR-024 commitment 3, carried through.
  - Changing the served markets no longer needs a restart of every writer.
- **Seeds stop being partitioned by market.** A seed has a currency. The served list (or each seed's market offers) decides who sees it. The 1,084 SGD seeds in the "US" partition are the reason.

### 4. Ingestion: an identity crawl, then a price capture per market

1. **Identity crawl** (per storefront, any base currency) writes products, listings and base-currency offers.
   - A base offer **serves its home market only when the capability census proves all three of these:** the store ships there, the cart in that market reports the base currency, and a sampled price taken in that market's session matches the stored price.
   - Otherwise the base offer is stored for identity only and claims no market.
   - This keeps the ~64k US offers that come straight from base crawls serving, but by measurement rather than assumption.
   - **No base row serves another market by default.**
   - **Re-verified on a schedule, not once.** The purchasability sweep, given `expected_price_minor` and `expected_currency`, is the recurring check. A store that later switches on Markets pricing fails it and drops back to identity-only for that market automatically.
2. **Market price capture** (per storefront × market, scheduled from the capability table) writes sibling offers under an evidence standard defined per capture method:
   - **`shopify_markets` (US+SG; reader fixed in #2558, merged as 08c420e84, drain rolled to it; no live sibling yet):** the primary proof becomes the pinned read in 4.4. The `/localization` + `/cart.js` session is kept as a fallback where robots allow it.
   - **`affiliate_feed`:** the network's country feed. The feed's declared currency is the proof, by definition of the method.
   - **Regional storefront:** `store.com.sg` is its own storefront, linked to one retailer (see below).
   - **Further platforms (Salesforce CC locale sites, Magento store views):** added as further methods, never as special cases.
   - This removes the "base crawl must sit in an ingest market" limit, which today blocks every GBP/CAD/EUR/KRW-base store.
   - **Capture lifecycle.** Today a sibling is written once and never refreshed, and never gets `price_checked_at`.
     - Each capture method declares a refresh cadence, scheduled from the capability table.
     - Every sibling carries `price_checked_at`.
     - A sibling not re-proven within its cadence stops serving, just as a stale base offer does.
3. **The ships-to check becomes the default** for storefront crawls, after a census of existing cohorts (#2553 already added the opt-in and the `base_market_not_shipped` tripwire).
4. **Every price read must have same-session currency proof**, or it is refused.
   - **The primary proof (measured, Appendix B):** `services/shopify_presentment.py`'s market-pinned read, `?country=<m>` plus the `cart_currency` cookie on the same response. It is stateless and needs no cart.
     - It found SG pricing at 24 of 24 SGD-capable stores; the `/localization` + `/cart.js` session found 17.
     - Where both worked, they agreed to the cent (15 of 15).
     - It avoids the cart paths that robots.txt disallows at 19% of stores. robots.txt is still asked about the pinned URL itself.
     - Today only the proof jobs and the variant backfill use it; the crawl, the refresh, the preflight and the PDP do not.
   - **Payload vs cookie: measured, not assumed.** The concern was a page carrying an SGD cookie over base-currency numbers. That trap occurred at 0 of 36 stores, on both `.js` and products.json. The per-session cart-line check is therefore not required on every read. Instead:
     - a periodic sampled cross-check runs against the session method where robots allow the cart path;
     - the anomaly band (below) flags drift.
   - **No cookie means the currency is unprovable.** A response with no `cart_currency` cookie (3 of 36 stores) gives a currency that cannot be proven. The capability table records `currency_unprovable` for that (storefront, market). The read is `unverified`, never defaulted to `/meta.json` or to the market's currency.
   - **The base crawl proves its own currency too.** The crawl reads the cookie on its own `/products.json` response and holds the read when the cookie differs from `/meta.json`. Measured: 1 of 127 stores is presented in QAR from our egress while `/meta.json` says USD, and a re-crawl would write 116.00 labelled USD over a stored USD 31.20.
   - **Availability is market-specific too.** Stock follows egress geography, so price, availability and currency must all come from the one market session. Known violation today: the SG storefront jobs record stock seen from the US crawl egress.
   - An anomaly band compares the market price with the base price at a reference FX rate.
   - It only flags rows for review: it is never shown to buyers and never decides anything (ADR-024 commitment 5).
5. **One retailer entity** links regional storefronts (`x.com`, `x.com.sg`, `sg.x.com`). For each market, the domestic storefront is preferred, and one retailer counts as one seller.
6. **The nightly refresh is a market price read like any other.** `jobs/external_referral_refresh.py` with `services/external_offers_service.py` (~1455-1526, 1852-1870) is the biggest recurring price writer, larger than capture.
   - **What it does today:**
     - It re-reads about 13k served prices every night, from PDP HTML, from a US egress, with no market pinned.
     - When the currency it reads differs from the stored one, the stored price is frozen for good. `skipped_currency_mismatch` is only a counter.
     - Its projection onto offers checks currency only, not market or source_system. So once siblings exist it can overwrite one (inferred from code).
   - **Decision:**
     - The refresh re-reads per (offer, market) under the same proof rule as capture (4.4).
     - Its writer matches on (market, source_system, currency).
     - **A mismatch drops the offer from serving, with a reason, only when the read is proven to be in the offer's market** (pinned `?country` + cookie + cart-line check, or a market session).
     - **An unpinned read that mismatches is recorded as `unverified_read` and changes nothing:** no freeze-as-truth, no drop. Otherwise a US-egress read of a correct SGD offer at a store that localizes by IP would drop real SG supply every night.
     - **Ordering:** until P3's market-pinned refresh ships, the existing freeze stays. P0 makes the mismatch counter visible per market, and the drop rule ships with the market-pinned refresh in P3.
7. **Request budget per market.** Every market read multiplies Shopify-edge requests from the one crawl IP. The shared pacer allows 2 requests per second across every crawl job, and the refresh alone uses most of a night.
   - The capability table drives cadence: capture and refresh only the (storefront, market) pairs that are served and in demand.
   - Each market gets a request budget, so the drain, the captures and the nightly refresh don't starve each other.

### 5. Serving: the buyer market is resolved once and threaded everywhere

- The request's buyer market (partner-supplied first, per ADR-024) is resolved once at the edge into a **MarketContext** `{market, currency, locale, price_display, rails}`, taken from the registry.
- Every recall, gate, cache key, warmer and prompt takes the MarketContext as an input. None reads a default or a deployment-wide market. This removes:
  - the `[0]` collapses;
  - the literal-`'US'` binds (`server.js:3246`, `strictFindProductsMulti.js:806`);
  - the US/USD ternaries;
  - the `PRODUCT_DETAIL_CACHE` key that has no market in it.
- **Offer selection lives in one module** (ADR-024 Phase 2b item 12). For a buyer market it uses only offers in that market's currency, then prefers a domestic storefront over a cross-border one, then ranks as today.
- **Cross-border offers carry a label** ("ships from AU; duties may apply").
- **Thin markets get an honest empty answer** (ADR-024).
- Cross-border offers are served, ranked below domestic offers, and labelled (Resolved question 3). The registry keeps a per-market override to make a market domestic-only.

### 6. Checkout, payment and monitoring

- Rails come from the registry per market (Reap is SG-domestic only today; ADR-016 non-custodial is unchanged).
- Purchasability is measured per (offer, market). The sweep is given `expected_price_minor` and `expected_currency` so stored-vs-landed drift is visible, and preflight recording is switched on.
- **Buy paths select the buyer market's sibling.** Today the Reap enrichment cart query filters `source_system='catalog_enrichment_agent_v1'`, so a correct SGD sibling is unbuyable on Reap (`row_currency_mismatch`). Reap enrichment and checkout preflight must pick the offer for the buyer's market through the one selection module (decision 5).
- The 5 of 157 US checkouts that landed in GBP/ZAR become a per-market tripwire, not an anecdote.

### 7. Launching a market is a checklist, not a code change

To move market X from `acquisition` to `served`:
1. Add X's registry entry: currency, locale, display, rails, capture methods. **List only capture methods that a live canary has proven** (sibling offers landed for X), never methods proven only by unit tests.
2. **Supply census:** the capability table shows ≥ N domestic plus localized storefronts; the eligibility table shows ≥ M served products in the top categories.
3. **Tripwires green for X:** 0 offers whose currency differs from X's currency; drift under a threshold; no `unknown`-classified offers served.
4. **Taxonomy and query understanding for X's locales:** synonyms, the budget-currency parse, and no SGD-first regex.
5. **Shadow traffic for X:** partner test keys, with per-market dashboards.
6. Flip the state. **No code edits.** Any edit this step needs is a registry bug to fix first.

## What we keep from ADR-024

- No FX conversion reaches a buyer, ever. FX exists only for review-only anomaly detection.
- Market is never inferred from currency.
- Offers are siblings, never rewrites.
- Thin markets get honest empty answers.
- Serving gates on the buyer market's currency; reachability is added as a separate gate.

## Migration (in order; each phase ships alone)

| Phase | Work | Behaviour change | Owner |
|---|---|---|---|
| **P0 stop the bleeding** | **Prove each capture method against real storefront responses before any market depends on it.** Its tests use the real content types and payloads (recorded fixtures from live stores). First case: `shopify_markets` fix #2558 (the `.js` content type), done: merged as 08c420e84, drain rolled. Then run a canary that lands live siblings for one US brand store and one SG retailer before any registry entry lists `shopify_markets`. **Base-crawl cookie check:** the crawl reads `cart_currency` on its own `/products.json` response and holds the read when it differs from `/meta.json`. This corrects 1 measured store today (QAR presented, labelled USD) and closes the mislabelled-read failure. **Refresh writer condition:** the nightly refresh's offer projection matches on market + source_system + currency, so it can never overwrite a sibling. The refresh's currency-mismatch counter is made visible per market (the existing freeze stays until P3). Fix GB→US in gateway `normalizeMarketId`. Add SGD/AUD to the FX defaults, or delete the defaults. Clean the mis-stamped offers (10 JPY-as-US, 5 USD-as-GB). Feed `expected_price_minor` into the sweep. Turn on preflight recording. | small, corrective | gateway + drain lane |
| **P1 registry** | `region_pricing` becomes the registry; derive every list and map from it; generate the gateway JSON, with a CI check of equality. | none (same values) | backend + gateway |
| **P2 capability census** | Build the storefront × market table, including the `currency_unprovable` and `robots_disallows_cart` states. Census the ~417 US-serving domains and the SG stores (ships_to, cart currency, sampled price match). Turn the ships-to check on by default. Apply the home-market rule from measurement, re-verified by the sweep. Capacity: cadence driven by served demand. | stores that fail stop serving base offers in that market | drain lane |
| **P3 identity crawl + general capture** | Allow base crawls in any currency (identity only). Make capture generic over the registry's capture markets, with a declared refresh cadence and `price_checked_at`. Re-read the nightly refresh per (offer, market) under the proof rule, and ship with it the mismatch rule: drop only on proven-market reads; an unpinned mismatch is recorded as `unverified_read` and changes nothing. Buy paths (Reap enrichment, preflight) select the buyer market's sibling. Per-market request budget across drain, capture and refresh. Add the retailer entity. | +supply (unblocks the GBP/CAD/EUR/KRW bases); siblings stay fresh and buyable | drain lane (rewrites shopify_markets.py and the refresh lane; both run in jobs it owns: drain, external-referral-refresh) |
| **P4 per-market eligibility + MarketContext** | Eligibility rows per (content_key, market). Remove the seed partition. Thread MarketContext through the gateway (the 10 chokepoints in Appendix A). Key caches by market. | consumer by consumer, each named | backend + gateway |
| **P5 selection + labels + rails** | One offer-selection module, domestic preference, cross-border labels, rails from the registry. | ranking and labels | gateway + backend |

After P5, a third served market (GB, JP, AU…) is a registry entry, supply, and the launch checklist.

## Resolved questions (Peng, 2026-10-11: "go with your recommendations")

1. **Where the registry lives: a checked-in file.** It is reviewed and deployed like code. The gateway reads a generated JSON copy, which CI checks byte-for-byte against the backend's. There is no runtime-toggled DB table: a market change is reviewed like a code change.
2. **Seed partition: dropped.** A seed's own offers (their currency, and the market they declare) decide which buyer markets see it. The `external_product_seeds.market` read partition goes away in P4.
3. **Cross-border offers: served, ranked below domestic offers, and labelled.** Within a buyer market's currency, a domestic storefront ranks first. A cross-border offer carries "ships from X; duties may apply". The registry keeps a per-market override for a future market that must be domestic-only.
4. **Price display: per the registry.** Prices are tax-inclusive where that is the local norm (SG, GB, AU, JP) and tax-exclusive in the US. Each market offer stores `tax_included`, so what is displayed matches what was captured.
5. **Ownership:** the backend and gateway own the registry jointly. The capability table and price capture belong to the drain lane.

## Appendix A — chokepoints (from the code inventory)

1. BE `services/region_pricing.py` → the registry. Derive `INGEST/CAPTURE/ACQUISITION_MARKETS` and the served regions from it.
2. GW `buyerRegion.js` + `servedMarkets.js` + `buyerMarket.js` → one registry client, generated from item 1. This also replaces `commerceFacts.js` and `market.js`.
3. BE `index_pipeline_state_service.py:670-720` → per-region eligibility (instead of a boolean built at import).
4. BE `ingestion.py:989` + `external_seed_search.py:447-530` → separate the seed partition from market.
5. BE `retailer_ingest/pipeline.py` + `shopify_markets.py` → derived lists, no SG literal, a source_system for any market.
6. GW `discoveryFeed.js:9727` + `buyerMarketOfferScope.js` → always scope by the buyer's currency.
7. GW `canonicalPdpOwnMoney.js` + `canonicalOwnOfferSellerSql.js` → take market and currency as parameters.
8. BE `agent_commerce_reap.py:616` + `shopify_presentment.py` → read the registry.
9. BE `offer_classification.py:87` + `offer_buyability.py:37` + `agent_pdp_v1.py:44` → take the buyer market as input.
10. GW `commerceFacts.js` + `market.js` / `getMarketPack.js` → stop the silent fallback to US. Fix FX defaults (`agent_shop_gateway.py:827`, `policy.js:266`).

## Appendix B — price-mix measurement (drain lane, 2026-10-11)

**Method:**
- **Sample:** the 160 served US/SG hosts with the most live offers that have a `/products/` URL; one offer per host.
- **Run:** a read-only prod one-off on the crawl subnet. Every read went through `crawl_politeness` (robots + pacing) and the Shopify-edge pacer, with no retries.
- **Reader:** #2558's (text/javascript accepted).
- **Privacy:** store names are kept out of this repo; the raw logs are local to the drain lane.

| # | Finding | Consequence |
|---|---|---|
| 1 | Robots refused 30 of 160 (19%): 29 disallow `/cart.js`, 1 disallows `/meta.json`. 2 were unverifiable (the `.js` body was not a JSON object). 128 were measured. | The cart-session capture can never run at about 1 in 5 stores, so it cannot be the primary proof. |
| 2 | Ingest read vs no-session `.js`: 127 of 127 identical. | The crawl endpoint is fine; what matters is what each market is shown. |
| 3 | US, stored price vs US-session price, same currency: 121 of 123 match. The 2 differences are price drift since the crawl. | US home-market rule (4.1) is mostly sound; drift needs the sweep's `expected_price_minor`. |
| 4 | 1 of 127: the un-pinned read is presented in QAR while `/meta.json` says USD, and the response cookie says QAR. Stored USD 31.20; a re-crawl would write 116.00 labelled "USD". | `/meta.json` is not proof. The same-response cookie is (P0 base-crawl cookie check). |
| 5 | SG session (`/localization` → `/cart.js`): 17 of 123 non-SGD stores localize to SGD, 106 stay USD, 4 are domestic SGD. | The session method finds some cross-border SG supply. |
| 6 | Cookieless `?country=SG` (n=36): an SGD cookie with prices equal to base occurred **0 of 36** times on `.js` and **0 of 36** on products.json. | The feared payload-vs-cookie trap did not occur. |
| 7 | Session-localizers: 15 of 15 priced products match the session SG price to the cent; 2 are unavailable in SG. | The pinned read agrees with the session where both work. Availability is per market. |
| 8 | Of the 19 stores whose session stayed USD, 7 DO present SGD under `?country=SG` (6 priced, 1 unavailable). | The session method missed 29% of SGD-capable stores (7 of 24). The pinned read found all 24, so it becomes primary (4.4). |
| 9 | 3 of 36 send no `cart_currency` cookie. | Their currency is unprovable: `currency_unprovable` state, never a default. |
| 10 | SG/base price ratio across 21 priced SGD stores: min 1.18, median 1.32, max 1.93; 17 of 21 are whole-dollar rounded. | FX conversion cannot reproduce merchant market prices: data behind ADR-024's no-FX rule. |
| 11 | 8 of the 24 SGD-presenting stores do not list SG in `/meta.json` ships_to. | Offer currency is not fulfilment reach: the four axes (decision 1) are separate in the data. |
