# ADR-031: Supply is read through lanes; a price is read from its source when a buyer acts, not crawled in advance

**Status:** Proposed (draft 7), 2026-11-23. Nothing here is built or switched on by this ADR. Acceptance waits on the owner's questions at the end. Drafts 2 to 4 answered three passes of an independent review and recorded the owner's answer on list prices. Draft 5 answers a further review: a kept price lives only in the gateway's price cache and never reaches public or bulk surfaces; the ADR no longer amends the `has_price` gate; request-time reads get an egress rule; one storage rule serves this ADR and ADR-032. Draft 6 records the owner's answer to question 1. Draft 7 names the amendment to ADR-026 on checkout preflight, separates the catalog's caching pages from the store rule the price cache runs under, and says who can see a cached price.

**Decision owner:** Founder. **Runtime owners:** the gateway owner (request-time lanes, the price cache), the backend owner (identity, seller facts, records, scheduled lanes).

**Companion:** ADR-032 decides when two listings are the same sellable item. This ADR uses its result.

**Direction already decided by the owner:**
- **2026-10-14.** Pivota is a cross-platform merchant decision and recommendation layer, not a product index. The flow is: discover the product, compare merchants' offers across platforms, fill in the order, send an agent to complete it at the merchant, confirm, and leave service to the merchant.
- **2026-11-22.** Supply comes from store platforms connected directly and from affiliate networks, not from a wider crawl. Four conditions hold on every source: same-product matching, a live comparison of a short list, a path to a paid order for every offer, and Pivota's credit on the order on every path.
- **2026-11-23.** Asked how lists keep a recent price once the page crawl stops for a store with a door, the owner chose to keep a dated copy of the door's price and availability, with a paced re-read. D9 says what that is today and what it waits on. Asked the same day what applies meanwhile on stores whose platform limits a kept price to minutes, the owner chose to keep the page refresh there (question 1, answered).

**Decisions this ADR follows and does not reopen:**
- **The list-price rule** (gateway `src/services/listPriceTiers.js`). Lists use a recent price and never block on live reads. The live check runs where the buyer acts on one product. The merchant's own cart price is final. A list row says where its price came from with one of three tokens: `live`, `cache`, `stored`.
- **The fresh-price contract** (gateway `src/agentSignals/offerToSignal.js`, backend offers resolution). A best price is picked only from offers that are `price_comparable`, fresh after the request's live reads, not known to be out of stock, priced, and in one currency. An empty set is no best price.
- **Ranking.**
  - A completable offer ranks above a click-out offer for the same product (ADR-028 D1).
  - A domestic offer ranks above a cross-border one, before price (ADR-026).
  - Then the served price orders sellers within one product (ADR-027, resolved question 6).
  - What Pivota earns never moves an offer, including as a tie-break (ADR-025, owner decision 4).
  - Official source is a display and trust signal, not a rank input (ADR-019).
- **ADR-030.** The merchant's totals are authoritative and shown verbatim (D1). Only the gateway calls a merchant's protocol door; the backend asks the gateway (D2). Who completes a payment (D3). Eligibility is a stored fact, never a request-time fetch (D8). A merchant checkout is created on buyer intent only.
- **ADR-029 D6.** The purchase router chooses the path. Quoting several paths to compare them is not allowed.
- **ADR-028 D7.** The buyer's contact and delivery details reach a merchant only with the buyer's consent for that order.
- **ADR-027.** Deals are a lookup, never a fetch (D5). D1 item 4: "Request-path traffic leaves by the payment-allowlisted address, which must not carry crawl-like load." That binds the gateway as much as the backend (D10).
- **ADR-026.** The buyer market is resolved once. Price, availability and currency come from one session for that market. A currency is never defaulted.
- **ADR-025 D1.** A click is minted when a link, cart or checkout is issued. The storefront hand-off record meets it.
- **ADR-007, ADR-018 and ADR-012 (catalog convergence), Phase 0b.** The `has_price` gate, the recall join on a stored offer, the trust gates and the public-surface invariant are untouched. A listing still needs a stored offer row with a price.
- **ADR-010, ADR-011, ADR-016.** Resolver-owned identity; attach before mint; Pivota is non-custodial.
- **A standing rule.** No lane, no seller discovery and no stored record comes from search-grounded model results.

**Amends** (each restated where it bites):
- **The live-read rule** (gateway `src/services/ucpLiveOfferRead.js`, citing the owner's 2026-10-14 direction): "nothing is written back". D9 keeps a door's price and availability in the gateway's shared price cache. Inside the source's window that is what the cache was built for. Beyond it, it is new and waits on the source (owner, 2026-11-23).
- **ADR-026, section 4.6** (the scheduled refresh as the recurring price read). It changes only for a store where the page refresh is stopped, which D9 allows only after open dependency 5 and owner question 7. Until then the page refresh keeps running (owner, 2026-11-23, question 1).
- **ADR-026, section 6, and its phases P0 and P3 where they repeat it** (checkout preflight: "preflight recording is switched on"; "Reap enrichment and checkout preflight must pick the offer for the buyer's market through the one selection module"). D9 retires the backend checkout preflight, so those lines change:
  - **stored-versus-landed drift at checkout** is recorded by the gateway at `quote` instead (D5): the cart total against the stored price and against the price read, per offer and market. That covers sellers with a door;
  - **for a seller without a door,** no check runs at checkout time after this. The preflight there ran in shadow only and never verified a price. The purchasability sweep, with its expected price and currency (the first half of the same section, unchanged), stays the drift record;
  - **the selection rule stands.** The offer handed to `quote`, and the one Reap enrichment picks, is the buyer market's sibling, chosen through the one selection module;
  - the preflight is deleted only after the `quote` drift record is on (D13).
- **ADR-025 D2**, the carrier table. D7 adds one row, and names what that row cannot do.
- **ADR-012 (catalog convergence), rule 1** ("full coverage over time"). Listing pointers and cached prices are refreshed only when a buyer asks (D11). Their staleness is made visible, not reconciled.

## Scope

**Terms.**
- A *lane* is one sanctioned way to read supply, for an answer to a partner agent, from outside our database: a platform's cross-merchant catalog, a store's own protocol door, a platform app a merchant installs, an affiliate or marketplace interface, a bulk feed, or our own crawl.
- A *listing pointer* is the set of bare ids a lane uses for one seller's sellable variant. It holds no price, no content and no tracking parameter.
- The *decision layer* is everything between a partner's request and the hand-off to order execution: resolve the product, read, rank under the existing rules, and name the offer the buyer picked.

**In scope:** how products, prices and stock reach an answer on the agent tools: `search_catalog`, `recommend_products`, `get_alternatives`, `get_product`, the agent product page read, `get_offers` and `create_checkout_session`; what may be kept from each lane; what each reader becomes.

**Out of scope:**
- who completes the payment (ADR-028, ADR-029, ADR-030);
- the matching rules (ADR-032);
- **public and bulk surfaces:** public product pages, sitemaps, feeds, and the stored view behind them. They read stored prices under their own rules, and nothing here feeds them (D8);
- **connected merchants:** the backend's product sync, webhooks and order poller for merchants that installed our app. They are not lanes under this ADR and stay where they are. The difference from a platform-app lane is direction: a connected merchant's catalog is pushed into our database and is stored supply; a platform-app lane would be read when a partner asks, under the merchant's grant on that platform. No platform-app lane exists yet (D13).

## Context

### What exists today (both repos' main; production settings read 2026-11-22)

Reading supply live is not new. Most request-time readers are already on. The "production" column is what the deployed services were set to on that date; code defaults are off.

| Reader | Where | What it asks | Production |
|---|---|---|---|
| Store-door live read: offers, product, product page | gateway `src/services/ucpLiveOfferRead.js`, wired in `src/server.js` | the seller's own UCP `lookup_catalog`, 10 variant ids per call; at most 3 offers, or 30 variants of one product, per request | **enforce** (`UCP_LIVE_OFFER_READ`, `_PRODUCT_READ`, `_PDP_READ`) |
| Store-door live read: search | the same reader; top-K and budget in `src/services/liveMerchantSearchPrice.js` | the same, for the top cards (up to 10, 600 ms) | shadow (`UCP_LIVE_SEARCH_READ`; top-K 0) |
| Storefront page read for list prices | gateway `src/services/liveMerchantSearchPrice.js` | the store's public product JSON | on (`SERVE_LIVE_MERCHANT_PRICE`) |
| Store-priced cart | gateway `mcp-server/src/ucpMerchantDoorPricing.js` | the seller's `create_cart`, with the line items and the market's country; no buyer data | on |
| Live cart on offers | gateway `src/services/offerLiveVerification.js`, backend `services/offer_live_verification.py` | a signed token from a verified live read unlocks the cart for that answer | on (`GET_OFFERS_LIVE_CART`) |
| Store-door variant sourcing | gateway `src/services/merchantVariantSource.js` | the seller's `search_catalog`, to find real variant ids | on, for a brand list |
| Platform catalog client | gateway `mcp-server/src/shopifyCatalogAttribution.js` | the cross-merchant catalog's `lookup_catalog`, 1 to 10 ids | shadow, one market, **attribution only** |
| Backend page read for stock | backend `services/live_offer_verification.py` | the store's public product JSON | off |
| Backend checkout preflight | backend `services/checkout_preflight.py` | the same page read, at checkout time | shadow |
| Price refresh | backend `jobs/external_referral_refresh.py` | one product page per listing, stalest first, up to about 2,000 per run | ten runs a day in the production scheduler as read; the repo's scheduler script shows one nightly run |
| Destination sweep | backend `jobs/external_seed_destination_sweep.py` | is the product page still there | daily |
| Purchasability sweep; cart-link eligibility | backend jobs | can this store sell to this market by cart link | hourly; daily |
| Retailer ingest | backend `services/retailer_ingest/`, `jobs/retailer_ingest_drain.py` | a store's product list; an affiliate feed reader sits beside it | every ten minutes |

Three more things that are built:

- **A stale listing is already served on `get_offers` when its door verifies it.** Offers resolution returns a freshness-gated listing flagged `requires_live_verification` when the caller opts in, and the gateway serves it only when the door read verifies it.
- **A shared verified-price cache** (gateway `src/services/priceCache.js`; off in production).
  - It records each verified merchant answer once for the whole fleet: amount, compare-at amount, availability, the product handle, the source, the writer and the read time.
  - Its key is host, declared market, variant id and currency. With no declared market it neither writes nor reads.
  - Its window is a constant of 15 minutes, which no setting can raise, citing the platform's limit for keeping a price or stock answer.
  - Its one consumer today is search. `recommend_products` and `get_alternatives` rows carry no declared market and no host and variant key, so it gives them nothing.
- **Seller facts:** `execution_routes` (a seller's protocol endpoint), the purchasability and cart-link tables, destination liveness. The per-domain profile facts of ADR-030 D8 and the capability table of ADR-026 are decided and not built.

**Egress.** Request-path traffic from the gateway and from the backend leaves by the payment-allowlisted address. Crawl jobs leave by the crawl egress.

### What is wrong with the shape

- **The page crawl still does three jobs for a listing its door can read.** It keeps the list price recent, it keeps the public and bulk price recent, and it checks the page is alive. The offer a buyer acts on no longer needs it.
- **The scheduled read is the expensive kind.** It fetches one public page per listing from a crawl address. The door answers ten variants in one call, to a caller that has identified itself.
- **No lane adds a second seller.** Every reader starts from a variant id we already hold. The platform catalog, the one source that knows other sellers of a product, is used for attribution alone.
- **There is no common result.** The readers return four status vocabularies and two money units (major units in the live read, minor units in the cart and the backend).
- **A source's rules are scattered constants.** Cache windows, per-host budgets and "not stored" promises live in separate files and comments.
- **Two request-time readers fetch public pages.** One is in the backend.
- **Request-time reads share the payment address,** and nothing states how much of it they may use.
- **The buyer client pins one spec version** while stores publish a later one. ADR-030 D2 decides negotiation; it is not built.

### What sources publish about use (as read 2026-11-22)

These were read through a summarising reader. Each is re-read directly before a lane's terms record is written (D3). Nothing here is a legal conclusion.

- **The Shopify cross-merchant catalog.**
  - One page gives cache limits: product data and images up to 24 hours, price and availability up to 10 minutes, commission values never. Another of its pages says not to cache results. Both pages govern the catalog lane. This ADR keeps nothing from that lane (D1), so the strict reading, no caching, costs nothing there.
  - As read, it does not allow a standing product database or commerce index built from it, bulk or systematic collection, or syndication.
  - Tracked ids and links are to be used exactly as returned. Our client's notes add: a tracked id identifies one buyer's click and is never reused.
  - Five requests a second per client.
  - Results are grouped by the platform's own product id across sellers on that platform. The grouping is product-level, not variant-level.
  - Availability is yes or no. No barcode, brand field or SKU is documented in the response.
  - Not stated either way: whether bare ids may be kept as join keys, and whether its offers may be shown beside offers from other sources.
- **A store's own UCP door.** The spec says catalog responses are not transactional commitments, checkout is authoritative, and results should not be reused across sessions without re-validation. The window the price cache runs under is a separate, store-level rule: the 15 minutes the gateway's cache cites as the platform's limit for keeping a store's price or stock answer. It is not taken from the catalog's pages. Like every rule here it is re-read directly before the door lane's terms record is written (open dependency 2), and the cache is not turned on before that record exists (D3, D9).
- **Other store platforms.** None was found to offer a cross-merchant catalog. Each offers an app a merchant installs, with catalog, inventory and order scopes. Several have announced UCP support; a shipped endpoint was confirmed for few.
- **Marketplaces.** Affiliate interfaces: offers and tracked links, no order creation.
- **Affiliate network feeds.** Bulk files, refreshed up to a few times a day, often carrying GTIN, brand and part number, storable under each network's terms.

### What comparable systems do

Every public system found keeps an index for discovery and re-checks at checkout. The index comes from merchant feeds, from a platform's own data, or from pages read by a model. The one per-request reader found takes a product URL as input and does not discover. The failure these systems report is staleness. None publishes a latency budget for reading several sources per request.

## Decision

### D1. What may be kept from each lane

One rule, for this ADR and for ADR-032.

| From | May be kept |
|---|---|
| A store's door | the pointer (bare ids); price and availability, as D9 lists them, in the gateway's price cache, for the lane's window; Pivota's own decision records: a verdict, which fields agreed, a fingerprint of the inputs |
| A platform catalog | nothing until open dependency 1 is answered. Then the same as a door |
| A platform app the merchant installed | what the merchant's grant allows |
| A feed, our own crawl of a permitted page, a connected merchant, brand-supplied data | content and prices, as today |

- **Not kept from a door or a catalog:** titles, images, descriptions, or field values derived from them, until the source allows it in writing (ADR-032 question 5 covers both lanes).
- **A tracked id, a tracked link, a cart or checkout id and a `continue_url`** are never written to a pointer and never cached across buyers.
- **Pivota also stores,** as its own records: seller facts (ADR-030 D8, ADR-026, ADR-025, ADR-019), lane terms (D3), the ticket and outcomes (D7), and demand: what was asked and what could not be answered.
- **A stored price is never served as current.** In a list it carries its date. Where the buyer acts on a product, the price is read.

### D2. One lane interface

- **Request-time lane calls are made by the gateway:** to a merchant's door (ADR-030 D2), and by extension to a platform catalog. The backend's request path makes no lane call.
- **Feeds and our own crawl are background jobs in the backend.** They read files and public pages, not doors. They leave by the crawl egress, are paced with every other crawl job, stay under a per-host daily cap, and never follow a tracking link.
- A lane implements whichever of four calls its source supports:

| Call | Meaning | Used by |
|---|---|---|
| `search(query, market)` | find listings | finding real variant ids at a seller we already serve (today); finding other sellers of a known product (ADR-032) |
| `lookup(pointers, market)` | current price and availability for known listings | product, product page, offers; the top search cards |
| `quote(lines, market)` | the seller's cart total for the selected offer | checkout escalation |
| `open_checkout(selection)` | the seller's checkout and its `continue_url` | `create_checkout_session` (ADR-030 D3 path 1) |

- **One result for `lookup` and `quote`:** lane, seller, the pointer asked for, market, amount in minor units with its currency, availability (yes, no, unknown), the time observed, and a status.
- **One status vocabulary,** the store-door reader's, unchanged: `verified`, `currency_mismatch`, `variant_gone`, `not_ucp`, `timeout`, `error`, `skipped` with a reason. A lane that is not a UCP door adds `not_supported` for a call it does not offer.
- **A lane answers for the pointer it was asked about.** An answer for another product is an error (`other_product`), as today. Nothing is substituted.

| Lane kind | search | lookup | quote | open_checkout |
|---|---|---|---|---|
| Platform catalog | yes | yes | no | no |
| Store door | yes | yes | yes | yes |
| Platform app the merchant installed | yes | yes | where granted | where granted |
| Affiliate or marketplace interface | yes | where offered | no | no (tracked link) |
| Feed (scheduled) | offline | no | no | no |
| Own crawl (scheduled) | offline | no | no | no |

### D3. A lane's terms are data, enforced in one place

- Each lane has one terms record:
  - what may be kept (D1) and for how long;
  - its request budget;
  - its attribution rule: which carrier, whether a redirect hop is allowed, what must be used verbatim;
  - the markets where its prices were measured correct (ADR-026);
  - the identity it is called with (D10);
  - any display duty, such as a disclosure.
- One module applies the record to every call: cache lifetime, budget, the persistence filter, link composition. No lane code sets its own retention. The price cache's window becomes the record's, capped by it, instead of a constant.
- **A lane without a terms record is not called.**
- The record names the page and the date its rules were read from. Where a rule is unstated, the record takes the strict reading until the source answers in writing.

### D4. The price contracts stay as they are

This ADR adds no price vocabulary and changes no served field.

- **List rows** (`search_catalog`, `recommend_products`, `get_alternatives`) carry `price_source` and `price_as_of` as `listPriceTiers.js` defines them:

| Token | Meaning, unchanged |
|---|---|
| `live` | the merchant answered during this request |
| `cache` | an earlier merchant answer, from a cache |
| `stored` | our stored copy, dated by its last read |

- **Product and offers** keep their fields: `price_source: merchant_live`, the `live_read` block with its status, `merchant_price_verified`, `stock_verified`.
- **The seller's cart total is final** and is shown verbatim (ADR-030 D1). As built, it is the total before the shipping and tax the storefront adds at checkout, unless the seller lists them.
- **List staleness is the existing rule,** for list rows only: with `PRICE_STALENESS_DEMOTE` on, a price older than seven days, or undated, is labelled and demoted. ADR-026's rules for when a stored offer stops serving are not changed by it.
- **The comparable pool is the existing fresh-price contract.** Identity decides `price_comparable` (ADR-032).
- **A read in another currency is `currency_mismatch`** and gives no price (ADR-026).
- **The list-price flags are turned on in phase 1** (`LIST_ROW_PRICE_AS_OF`, `PRICE_STALENESS_DEMOTE`), so a stale price is never served unlabelled.

### D5. What runs on each tool

| Tool | What runs | Limits today |
|---|---|---|
| `search_catalog` | stored identity and the list-price rule. The top cards may be read live inside the budget; the list never waits beyond it | 600 ms; up to 10 cards |
| `recommend_products`, `get_alternatives` | stored identity and the list-price rule; no live read, as today | none |
| `get_product` | `lookup` for the product's variants | 3 s; 10 ids per call; 30 variants |
| The agent product page read | the same; it may serve the overlay's ten-minute cache, as today | 3 s |
| `get_offers` | `lookup` for the offers the reader takes | 3 s; 3 offers |
| `create_checkout_session` | `quote` for the selected offer, then ADR-030's paths | 5 s; 3.5 s per call |

- **Which lane answers a `lookup`:**
  - the seller's own door where the seller has one. It is the seller's answer, and its request limit is counted per store, not shared across sellers;
  - the platform catalog where a door is absent or has failed, and only once open dependency 1 is answered. No phase before phase 6 turns this on, and it adds reads, so D10's egress rule applies to it.
- **A cart is created for the selected offer only.** Carts at several sellers to compare totals are not adopted (ADR-029 D6; owner question 3).
- **No buyer contact or delivery detail goes to a seller before the buyer's consent for that order** (ADR-028 D7). A `quote` carries the line items and the market's country, and, where their flags are on, the seller's discount code and our cart attribution. It carries no buyer data.
- **A request that names no market** gets no cart and no quote, and is answered as today, labelled as assumed (ADR-030 D3). The reader makes no live read without a declared market, as today.
- **When the cart total differs from the price read earlier,** the cart total is shown and the difference is recorded as drift for that lane and seller. The same record compares the cart total with the stored price, per offer and market. That is the stored-versus-landed record ADR-026 section 6 asked of the checkout preflight.
- **A seller with no door and no app** is served from its stored price, with a link hand-off, as today.
- **Failure never invents a value.** A failed read leaves the stored fields untouched, as today. A failing lane opens its breaker.
- Partner isolation is applied before any lane call (ADR-030 D6).

### D6. Ranking and routing do not change

- **This ADR adds no rank input and removes none.** The order is the one already decided: completable before click-out for the same product (ADR-028 D1); domestic before cross-border (ADR-026); then the served price within a product (ADR-027).
- **The lane that answered, the commission a lane pays, and a paid placement a source may offer are never rank inputs** (ADR-025, owner decision 4; ADR-007).
- **The execution path** comes from stored facts and, for a partner-completed purchase, from the purchase router (ADR-029 D6). It never comes from which lane answered.
- **Every decision records its reasons:** the offers considered, how each price was known and when, and why any was left out.

### D7. The decision ticket: the contract with order execution

- A ticket is the record of the offer a buyer picked. It extends the storefront hand-off record and the `esc_` id, which already hold the quoted total and its time.
- **It carries:**
  - the partner agent and the buyer market;
  - the resolved variant (ADR-032) and the chosen seller's pointer;
  - the evidence: how the price was known, the lane, the time observed;
  - the quoted cart total in minor units, with its currency and expiry, where a cart was priced;
  - the attribution carrier the lane's terms require.
- **The seller's cart or checkout id and the `continue_url`** are kept server-side in the hand-off record. The `continue_url` is returned to the partner agent in the answer. Neither is put inside an id, and neither is logged (ADR-030 D10).
- **Execution does not re-rank.** It applies the rules that exist: ADR-029 D5's check before the buyer's authorization, ADR-030 D3's approval and payment rules, ADR-028 D7's re-ask on any change.
- **One ticket names one seller.** A different seller is a new decision.
- **Attribution, by carrier:**
  - ADR-025 stands on every other carrier: our click id rides in the carrier that seller's evidence source reads back;
  - **new row in ADR-025 D2's table:** *platform catalog commission; carrier = the catalog's tracked variant id, fetched for this buyer at hand-off, used verbatim, never stored; read back from the platform's earnings report, per saved catalog*.
- **What that row cannot do, as built** (`services/storefront_handoff_record.py`):
  - ADR-025 D1 is met: the hand-off is one click row, written at issue time, and it names the partner agent;
  - the departure is in D2 and after it. The cart carries the catalog's tracking and none of ours: no referral click id, no tracking member. The platform reports the sale per catalog, not per order;
  - so no order joins that click row, and no agent share can accrue from it under ADR-025 D5.

  This exists in code, in shadow. This ADR records it and does not resolve it (owner question 2).
- **The verbatim rule applies to the platform catalog carrier only.** No redirect hop and no attribute of ours is added to a cart that carries its tracked ids.

### D8. What still needs a stored price, and what does not

- **Nothing in this ADR changes a stored-price gate.** `has_price`, the recall join on a stored offer, the trust gates, the public-surface invariant and the feed rule (ADR-018) stay as they are. A listing needs a stored offer row with a price to be recalled at all.
- **So the crawl, or a feed, or a connected merchant, still creates a listing and its first price.** A lane does not.
- **What a lane removes is the need to keep that price fresh for the offer a buyer acts on.** A stale listing is served on `get_offers` when its door verifies it in the request. That path is built. This ADR makes it the standing rule for a listing a door can read, and asks for nothing new in code.
- **Who may appear does not change.** The directory's exclusions (quarantine, test and rejected merchants) apply first; a link hand-off needs no purchasability fact; every agent-paid path needs ADR-029 D6's facts.
- **A seller or listing first seen through a lane's `search` is not served in that answer.** A new seller needs a seller record so the exclusions can be applied. A new listing is queued (ADR-032 M8). See owner question 4.
- **Nothing is written from the platform catalog until open dependency 1 is answered.** In shadow it is only counted.
- **Public and bulk surfaces are fed by stored prices only.** The price cache is a gateway store. No backend reader, view, feed, sitemap or public page reads it.

### D9. Keeping a door's price, and what each scheduled reader becomes

**The dated copy of a door's answer** (owner, 2026-11-23) **is the gateway's shared price cache.** It is not written to the offer table or to the listing's stored snapshot, because those feed public pages, feeds and the stored-price gates.

- **What it holds,** as built: amount, compare-at amount, availability, the product handle (so a reader can refuse another product's answer), the source, the writer and the read time. No title, image or description.
- **Its market rule,** as built: it writes and reads only for a declared market, in that market's currency. A list served under no declared market gets nothing from it.
- **Inside the source's window (minutes today):** once the door lane's terms record is written from a direct re-read of the store rule (D3; open dependency 2), the cache is turned on, first writing only, then read. A list row served from it is `cache`, with the original read time.
- **Who can see it,** as built: the search overlay is the cache's only reader, and it answers every caller of the gateway's search operation. The first-party shopping UI calls that operation, so a cached price can show in that UI's search lists too: for that request, with its read time, never stored. That is still a list. No public page, sitemap or feed reads the cache, and the backend has no client for it.
- **Beyond the window (days):** this is what could replace the page refresh for list prices. It needs the source's written allowance (open dependency 5). Then the lane's terms record sets the window (D3), and the seven-day list rule still labels an old price.
- **The paced re-read** keeps a days-long copy recent for what lists serve. The owner chose it; it is not built or run, in any mode, until open dependency 5 is settled. When it is built:
  - it reads only listings that a list served, under a declared market, in the last 14 days, and whose copy is older than five days. It reads them for that market. Both numbers are starting values;
  - ten variants per call, under one per-host daily counter shared with the backend's crawl jobs for that host. That counter is new infrastructure;
  - a challenge, a block or a rate-limit answer stops it for that host until the next day;
  - it never walks a seller's catalogue, and it never runs on the platform catalog lane;
  - it does not leave by the payment-allowlisted address. Which runtime runs it is part of open dependency 5: a backend job on the crawl egress would amend ADR-030 D2 for this one read; a gateway job needs an egress of its own.
- **Shadow for the days-long copy** is computed from request-time reads only. It makes no read of its own.

**What each reader becomes:**

| Reader | After this ADR |
|---|---|
| Price refresh (one page per listing) | Unchanged. It is the only writer of the stored price that public surfaces, the stored-price gates and stale list rows read. The owner kept it for stores whose platform limits a kept price to minutes (2026-11-23, question 1). It stops for a store only after open dependency 5 and owner question 7 |
| Destination sweep | Unchanged. A `variant_gone` door answer is one observation for its rule, at variant grain; one gone variant never marks a product gone |
| Purchasability sweep; cart-link eligibility | Unchanged. ADR-029 D6 needs their facts |
| Retailer ingest; affiliate feed reader | Unchanged. The feed reader gets a terms record per network (D3) |
| Backend page read for stock; backend checkout preflight | Retired. The backend's request path makes no outbound read. Drift at checkout is recorded by the gateway at `quote` (D5); this amends ADR-026 section 6, as the Amends list says |
| Gateway storefront page read for list prices | Retired for stores with a door once the top-card read is enforced. For stores without a door: owner question 5 |

### D10. Identity, conduct and egress on every lane

- One platform profile and signing key (ADR-030 D6); one registered client per platform catalog. Requests are signed where the lane supports it.
- The client negotiates the spec version (ADR-030 D2) and records the version used.
- A lane call stops on a challenge or block. No address rotation, no disguised agent, no solver.
- **Every request-time lane read is caused by one buyer request,** inside that tool's limits (D5). A lane is never used to collect a catalogue.
- **Egress** (ADR-027 D1 item 4). Request-time reads leave by the payment-allowlisted address today, and they already do in production. To keep them from becoming crawl-like load:
  - the fleet per-host budget (`PRICE_HOST_BUDGET`) is on before any phase adds reads;
  - a fleet-wide ceiling on lane reads per minute is set, and stated in the change that turns each phase on;
  - phases 2 and 3 add reads (the top search cards; catalog and door search). They ship only after lane reads have an egress of their own, or after the owner accepts their measured volume on the shared address (open dependency 6; owner question 8).

### D11. Reconcilers and drift metrics (ADR-012, catalog convergence)

| New stored thing | Upstream truth | How it is kept right | Drift metric |
|---|---|---|---|
| Listing pointers | the lane's answers | request-time reads; intake adds. **Coverage follows demand, not the whole set:** a source's rules do not allow a sweep of it. A pointer not answered for 30 days is labelled `unverified`; the label does not change serving | pointers by time since last answer; `unverified` count; `variant_gone` rate per lane and seller |
| The price cache | the door's answers | it is a cache: an entry expires at the end of its window and is rewritten by the next read. With a days-long window, coverage again follows demand | share of door answers that disagree with the cached value they replace |
| Lane terms | the source's published rules | not derived: a reviewed file, re-read on a schedule | days since each record's rules were last read |

Both demand-bound rows depart from ADR-012 (catalog convergence) rule 1, and are listed under Amends. Tickets, hand-off records and outcomes are records of events, not derived stores.

### D12. Switches

Every new behaviour defaults off. With its flag off, every answer is byte-identical to today's.

| Flag | Repo | Effect |
|---|---|---|
| `SUPPLY_LANE_TERMS` (off, report, enforce) | gateway | the D3 module; in report mode it only logs what it would refuse |
| `PRICE_HOST_BUDGET` (existing) | gateway | D10: the fleet per-host budget |
| `PRICE_CACHE_STORE`, `PRICE_CACHE_MODE` (existing; mode off, write, read) | gateway | D9: the copy inside the source's window |
| `LIST_ROW_PRICE_AS_OF`, `PRICE_STALENESS_DEMOTE` (existing) | gateway | D4 |
| `UCP_LIVE_SEARCH_READ` with a top-K above 0 (existing) | gateway | the top search cards are read live |
| `SHOPIFY_CATALOG_SUPPLY_LANE` (off, shadow) | gateway | catalog `search` and `lookup` are called and counted. Nothing is written or served; no further value exists until open dependency 1 is answered |
| `DECISION_TICKET` (off, shadow, on) | gateway and backend | D7 |
| `LANE_PACED_REREAD` (off, shadow, on) | to be decided with open dependency 5 | D9: the paced re-read |
| `REFERRAL_REFRESH_SKIP_STORES` (a list of stores, empty by default) | backend job | D9: the page refresh skips a listed store |

A days-long cache window is not a flag. It is a value in a lane's terms record. Each reader D9 retires is turned off by its own existing flag. The purchasability gate and the agent-checkout kill switch apply to all of them.

### D13. Upgrade path, piece by piece

| Existing piece | Production | Becomes | Work |
|---|---|---|---|
| Store-door live read: offers, product, product page | enforce | the store-door lane's `lookup` | wrap in the D2 result and the D3 record; no behaviour change |
| Stale listing served when its door verifies it | built | the standing rule for door-readable listings (D8) | none |
| Store-door live read: search | shadow | the same, for the top search cards | measure, then enforce with a top-K, under D10 |
| Store-priced cart | on | the store-door lane's `quote` | result shape only |
| Live cart on offers, signed token | on | the evidence a ticket carries | keep |
| Store-door variant sourcing | on, brand list | the store-door lane's `search` | from a brand list to demand; results are queued (ADR-032 M8) |
| Platform catalog client | shadow, attribution only | the platform catalog lane | add `search` and supply `lookup`, counted only until open dependency 1; attribution unchanged |
| Shared price cache | off | the dated copy of a door's answer (D9) | on, write then read; its window moves into the terms record |
| List-price contract (`listPriceTiers.js`) | flags not read for this ADR | the D4 contract | on in phase 1 |
| `priceHostBudget`, scattered windows | off | D3 and D10 | consolidate; set each window from the source's published one |
| Backend page read; backend checkout preflight | off; shadow | retired | delete after the door read covers them and, for the preflight, after the `quote` drift record is on |
| Gateway storefront page read | on | retired where a door exists | D9 |
| Backend `services/merchant_ucp_checkout.py` | one caller | none | goes with its caller (ADR-030 D2) |
| Price refresh; destination, purchasability and cart-link sweeps; ingest | on | unchanged | the refresh stops per store only under D9's conditions |
| A per-host daily counter shared by crawl jobs and the paced re-read | does not exist | D9 | new; built with the paced re-read |
| Stored-price gates (`has_price`, recall join, trust, public invariant) | on | unchanged | none |
| Adapters for installed apps | built | out of scope | a platform-app lane is designed when a first platform agreement exists |

## Phases

Each phase ships alone, behind its switch. None depends on the payment work, and none changes the partner contract.

1. **Contract.** The D2 result and the D3 terms module in report mode, around the readers that are already on. The list-price flags on. The per-host budget on. The price cache on, after the door lane's terms record is written: write, then read. The buyer client negotiates the spec version. The two backend readers retired; the preflight only once the `quote` drift record is on. Exit: every live read passes through the terms module; report mode shows no refusal that is not understood; the cache's hit rate and disagreement rate are known.
2. **Top search cards read live.** The search read is enforced with a top-K, under D10's egress rule. The storefront page reader is retired for stores with a door. Exit: the share of top cards answered `live`; list latency inside the budget; reads per minute under the ceiling.
3. **Finding other sellers.** Store-door `search` at sellers we already serve queues listings for ADR-032. Catalog `search` and `lookup` run in shadow and are counted only. Under D10's egress rule. Exit: for products partners asked about, how many more sellers each source would add.
4. **The ticket.** D7. Exit: the share of tickets whose quoted cart total equals the merchant's total at checkout.
5. **A days-long copy, and the crawl stops for a store.** Only after open dependency 5 and owner question 7. Exit: scheduled page reads per day fall by a number predicted beforehand, and no list row loses a price it had.
6. **More lanes.** The platform catalog as a `lookup` fallback, once open dependency 1 is answered and under D10's egress rule. A second store platform, a marketplace's affiliate interface, affiliate feeds for retailers outside the platforms. Each needs its own terms record and its own market proof. Own crawl shrinks to sellers that partners ask for and no lane reaches.

## What we count

- The share of list prices served `live`, `cache` and `stored`.
- Resolved, eligible sellers per product that partners asked about.
- Drift: cart total against the price read; door answer against the cached and the stored price.
- Lane health: timeouts, `variant_gone`, `currency_mismatch`, refusals by the terms module.
- Lane reads per minute, per host and fleet-wide.
- Scheduled page reads per day.
- The share of partner requests that end in a checkout an agent can complete.

## Risks

- **The dated copy cannot replace the crawl on the largest lane yet.** Stores on a platform whose rule is a window of minutes keep the page refresh until that platform allows a days-long copy (owner, 2026-11-23, question 1). The crawl's exposure on those stores is unchanged until then; the question was put to the owner with that stated.
- **A source changes its rules.** A seller reachable by two lanes survives one change, and the terms record makes a rule change one edit. Many sellers will have one lane.
- **Request-time reads share the payment address.** A block of that address would affect payments. D10 bounds the volume; only a separate egress removes the coupling.
- **Latency.** A live read adds one to three seconds on product and offers. Lists do not wait.
- **Kept ids may be read as an index.** As read, the catalog's rules do not allow a standing product database and do not say whether bare ids may be kept. Pointers on that lane wait for the answer (open dependency 1).
- **The catalog lane's commission joins no order to a click** (D7). Revenue on it is real but cannot be split by partner as built.
- **A kept copy against the spec.** The UCP spec asks that results not be reused across sessions without re-validation. The copy is shown only in lists, with its time, and every buyer action reads again. A source may still read that more strictly.
- **A paced re-read can look like collection.** Shaping it by demand, capping it per host and identifying the caller reduce that risk; only the source's answer removes it.
- **A cold listing.** A listing no list has served is not re-read. Its price goes stale and it is demoted in lists. That is accepted: the alternative is reading what nobody asked for.

## Open dependencies

1. A written answer from the catalog's operator: may Pivota keep bare product and variant ids as join keys beside its own match decisions, and show its offers beside offers from other sources.
2. A direct re-read of each source's rules, and of the two pages that disagree on caching.
3. For each further store platform: which calls of D2 it offers a buyer-side platform, under what identity and terms.
4. The ADR-030 D8 profile facts, which spec negotiation and the terms record read.
5. For a days-long copy and the paced re-read (D9): the platform's written allowance to keep a dated price and availability for days, for list display to agents, re-validated whenever a buyer acts, and to re-read on a schedule the listings buyers were shown. Also the runtime and the egress for that job.
6. An egress for request-time lane reads apart from the payment-allowlisted address (D10).

## Questions for the owner

1. **Answered 2026-11-23: keep the page refresh.** The question was: on stores whose platform limits a kept price to minutes (D9), until that platform allows more, keep the page refresh, or stop it there and accept list rows with no recent price beyond the cache window. The owner chose to keep it. The answer covers the time before the platform's allowance only. What public pages and feeds show once a store's refresh does stop is still question 7.
2. **Catalog-attributed hand-offs (D7).** No order joins their click row, so no agent share accrues under ADR-025 D5. Accept that for this lane, or hold the lane's attribution at shadow until the source offers a per-partner unit?
3. **Comparing totals by cart (D5).** This draft prices a cart for the selected offer only, as ADR-029 D6 implies. The 2026-11-22 "live comparison of a short list" is met by live price reads, without shipping or tax. Is that enough, or should carts at a short list of sellers be designed as an amendment to ADR-029?
4. **First sight (D8).** A listing first seen through a lane, at a seller we already serve: show it in the same answer as its own product, or only after it is queued and stored, as this draft does?
5. **Stores without a door (D9).** Keep the gateway's public-page read for their list prices, or show stored prices there and retire the reader entirely?
6. **Which lane after the catalog (phase 6):** a second store platform, or affiliate feeds for retailers outside the platforms?
7. **Public pages and feeds, once the page refresh stops for a store.** They read stored prices only. Show no price there (the page stays citable under ADR-007), or keep a slow page refresh for them?
8. **Egress (D10).** Accept request-time lane reads on the shared payment address up to a stated ceiling, or require their own egress before phases 2 and 3?

**Answered (owner, 2026-11-23): how lists keep a recent price.** Three options were put: (a) keep the page refresh for list prices; (b) keep a dated copy of the door's price and availability, with a paced re-read; (c) keep no stored price for these listings. The owner chose (b). This draft implements (b) as the gateway's price cache (D9). What applies on a platform whose rule is minutes was not part of that answer; it was asked separately and answered the same day (question 1).

## Consequences

- The offer a buyer acts on is priced by its seller at the time, wherever a door exists. That is built; this ADR makes it the rule and gives it one contract.
- The page crawl keeps running for list, public and bulk prices until a source allows a days-long copy. It is no longer needed for the offer itself.
- A partner sees more than one seller for a product only when identity is resolved (ADR-032), the seller is eligible, and a price for it can be read.
- Order execution receives one ticket and does not re-decide.
- What each source permits is written down per lane instead of spread through the code.

## Alternatives considered

- **Keep widening the crawl.** Rejected by the owner on 2026-11-22: it duplicates what platforms serve live, its cost grows with products, markets and refresh rate, and it is the access most easily blocked.
- **Write the dated copy through the page refresh's write path.** Draft 4 proposed it. Rejected: that path ends in the offer table, which feeds public pages, feeds and the stored-price gates, so a copy allowed for list display would be republished.
- **Let a lane pointer stand in for a stored price** (`has_price`). Drafts 1 to 4 proposed it. Rejected: recall, trust and the public invariant all read the stored offer, the public surface is out of scope, and the built live-verification path already serves a stale listing.
- **Keep the page refresh for list prices where a door exists** (option (a)), and **no stored price where a door exists** (option (c)). Not chosen by the owner on 2026-11-23 as the standing design. For the interim on a platform whose rule is minutes, the owner chose (a) the same day (question 1); (c) is not adopted anywhere.
- **No stored identity; search every source on every request.** Rejected: no public system works this way, no source outside one platform offers cross-merchant search, and a seller cannot be served before its facts are stored (ADR-030 D8).
- **Copy the platform catalog into our tables.** Rejected: as read, its rules do not allow a standing index or bulk collection.
- **A new price vocabulary and a new rank order.** Draft 1 proposed both. Rejected: the three-token contract and the ranking decisions already exist.
- **One third-party product interface as the only source.** Not adopted as the base: it puts another party between Pivota and the merchant on identity, attribution and terms. One may be added later as a lane under D2 and D3.
