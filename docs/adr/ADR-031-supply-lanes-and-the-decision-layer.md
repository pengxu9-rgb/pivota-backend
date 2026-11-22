# ADR-031: Supply is read through lanes; a price is read from its source when a buyer acts, not crawled in advance

**Status:** Proposed (draft 3), 2026-11-22. Nothing here is built or switched on by this ADR. Acceptance waits on the owner's questions at the end. Drafts 2 and 3 answer two passes of an independent review: the ADR keeps the existing price contracts and ranking rules instead of adding new ones, limits a cart quote to the selected offer, keeps the gateway as the one caller of merchant doors, leaves appearance rules as they are, and turns the question of how lists keep a recent price into an owner question instead of deciding it.

**Decision owner:** Founder. **Runtime owners:** the gateway owner (request-time lanes), the backend owner (identity, routing facts, records, scheduled lanes).

**Companion:** ADR-032 decides when two listings are the same sellable item. This ADR uses its result.

**Direction already decided by the owner:**
- **2026-10-14.** Pivota is a cross-platform merchant decision and recommendation layer, not a product index. The flow is: discover the product, compare merchants' offers across platforms, fill in the order, send an agent to complete it at the merchant, confirm, and leave service to the merchant.
- **2026-11-22.** Supply comes from store platforms connected directly and from affiliate networks, not from a wider crawl. Four conditions hold on every source: same-product matching, a live comparison of a short list, a path to a paid order for every offer, and Pivota's credit on the order on every path.

**Decisions this ADR follows and does not reopen:**
- **The list-price rule** (gateway `src/services/listPriceTiers.js`). Lists use a recent price and never block on live reads. The live check runs where the buyer acts on one product. The merchant's own cart price is final. A list row says where its price came from with one of three tokens: `live`, `cache`, `stored`.
- **The fresh-price contract** (gateway `src/agentSignals/offerToSignal.js`, backend offers resolution). A best price is picked only from offers that are `price_comparable`, fresh after the request's live reads, not known to be out of stock, priced, and in one currency. An empty set is no best price.
- **The live-read rule** (gateway `src/services/ucpLiveOfferRead.js`, citing the owner's 2026-10-14 direction). A live read's result lives in the response and a seconds-long memo. Nothing is written back.
- **Ranking.**
  - A completable offer ranks above a click-out offer for the same product (ADR-028 D1).
  - Among them, the served price orders sellers within one product (ADR-027, resolved question 6).
  - A domestic offer ranks above a cross-border one, before price (ADR-026).
  - What Pivota earns never moves an offer, including as a tie-break (ADR-025, owner decision 4).
  - Official source is a display and trust signal, not a rank input (ADR-019).
- **ADR-030.** The merchant's totals are authoritative and shown verbatim (D1). Only the gateway calls a merchant's protocol door; the backend asks the gateway (D2). Who completes a payment (D3). Eligibility is a stored fact, never a request-time fetch (D8). A merchant checkout is created on buyer intent only.
- **ADR-029 D6.** The purchase router chooses the path. Quoting several paths to compare them is not allowed.
- **ADR-028 D7.** The buyer's contact and delivery details reach a merchant only with the buyer's consent for that order.
- **ADR-027.** Deals are a lookup, never a fetch (D5). Request-path traffic from the backend must not carry crawl-like load.
- **ADR-026.** The buyer market is resolved once. Price, availability and currency come from one session for that market. A currency is never defaulted.
- **ADR-025 D1.** A click is minted when a link, cart or checkout is issued. D7 records one existing departure and does not widen it.
- **ADR-010, ADR-011, ADR-016.** Resolver-owned identity; attach before mint; Pivota is non-custodial.
- **ADR-018.** The connection layer is derived, never stored. The execution path is resolved from facts, not from the layer.

**Amends** (each restated where it bites):
- **ADR-007** ("gates that must stay offer-required": `serving_eligible`'s `and has_price`) and **ADR-012** (Phase 0b: "public ⇒ has a live offer with price > 0"). D8 adds a second way to meet both.
- **ADR-026**, section 4.2 ("a sibling not re-proven within its cadence stops serving"). D4 says what proves a price for a listing a lane reads. Section 4.6 (the scheduled refresh as the recurring price read) changes only if owner question 1 chooses option (b) or (c).
- **ADR-025 D2**, the carrier table. D7 adds one row, and names what that row cannot do.
- **ADR-012**, rule 1 ("full coverage over time"). A listing pointer is re-read only when a buyer asks (D11). Its staleness is made visible, not reconciled.

## Scope

**Terms.**
- A *lane* is one sanctioned way to read supply from outside our database: a platform's cross-merchant catalog, a store's own protocol door, a platform app a merchant installs, an affiliate or marketplace interface, a bulk feed, or our own crawl.
- A *listing pointer* is the set of bare ids a lane uses for one seller's sellable variant. It holds no price, no content and no tracking parameter.
- The *decision layer* is everything between a partner's request and the hand-off to order execution: resolve the product, read, rank under the existing rules, and name the offer the buyer picked.

**In scope:** how products, prices and stock reach an answer on `search_catalog`, `recommend_products`, `get_alternatives`, `get_product`, the product page, `get_offers` and `create_checkout_session`; what is stored; what each scheduled reader becomes.

**Out of scope:** who completes the payment (ADR-028, ADR-029, ADR-030); the matching rules (ADR-032); public product pages and their indexing (ADR-006, ADR-007), except one rule in D8.

## Context

### What exists today (both repos' main; production settings read 2026-11-22)

Reading supply live is not new. Nine request-time readers and five scheduled readers exist. Most request-time readers are already on. The "production" column is what the deployed services were set to on that date; code defaults are off.

| Reader | Where | What it asks | Production |
|---|---|---|---|
| Store-door live read: offers, product, product page | gateway `src/services/ucpLiveOfferRead.js` | the seller's own UCP `lookup_catalog`, 10 variant ids per call | **enforce** (`UCP_LIVE_OFFER_READ`, `_PRODUCT_READ`, `_PDP_READ`) |
| Store-door live read: search | same file | the same, for the top cards (up to 10, 600 ms) | shadow (`UCP_LIVE_SEARCH_READ`; top-K 0) |
| Storefront page read for list prices | gateway `src/services/liveMerchantSearchPrice.js` | the store's public product JSON | on (`SERVE_LIVE_MERCHANT_PRICE`) |
| Store-priced cart | gateway `mcp-server/src/ucpMerchantDoorPricing.js` | the seller's `create_cart`, with the line items and the market's country; no buyer data | on |
| Live cart on offers | gateway `src/services/offerLiveVerification.js`, backend `services/offer_live_verification.py` | a signed token from a verified live read unlocks the cart for that answer | on (`GET_OFFERS_LIVE_CART`) |
| Store-door variant sourcing | gateway `src/services/merchantVariantSource.js` | the seller's `search_catalog`, to find real variant ids | on, for a brand list |
| Platform catalog client | gateway `mcp-server/src/shopifyCatalogAttribution.js` | the cross-merchant catalog's `lookup_catalog` | shadow, one market, **attribution only** |
| Backend page read for stock | backend `services/live_offer_verification.py` | the store's public product JSON | off |
| Backend checkout preflight | backend `services/checkout_preflight.py` | the same page read, at checkout time | shadow |
| Price refresh | backend `jobs/external_referral_refresh.py` | one product page per listing, stalest first, up to about 2,000 per run | scheduled ten times a day (the script's own default is nightly) |
| Destination sweep | backend `jobs/external_seed_destination_sweep.py` | is the product page still there | daily |
| Purchasability sweep; cart-link eligibility | backend jobs | can this store sell to this market by cart link | hourly; daily |
| Retailer ingest | backend `services/retailer_ingest/`, `jobs/retailer_ingest_drain.py` | a store's product list; an affiliate feed reader sits beside it | every ten minutes |

Also built: adapters for merchants that installed our app (`adapters/`), with product sync and webhooks. Their production use was not measured for this ADR.

Stored routing facts today: `execution_routes` (a seller's protocol endpoint), the purchasability and cart-link tables, destination liveness. The per-domain profile facts of ADR-030 D8 and the storefront-by-market capability table of ADR-026 are decided and not built.

### What is wrong with the shape

- **Serving depends on the crawl even where a live read answers.**
  - `services/external_referral_readiness.py` treats a listing as stale after seven days. Offers resolution drops a stale listing unless the caller opted in to live verification.
  - `has_price` in `services/index_pipeline_state_service.py` needs a stored offer price.
  - So the refresh keeps reading a page per listing for listings whose price is re-read through the door whenever a buyer looks.
- **The scheduled read is the expensive kind.** It fetches one public page per listing from a crawl address. The door answers ten variants in one call, to a caller that has identified itself.
- **No lane adds a second seller.** Every reader starts from a variant id we already hold. The platform catalog, the one source that knows other sellers of a product, is used for attribution alone.
- **There is no common result.** The readers return four status vocabularies and two money units (major units in the live read, minor units in the cart and the backend).
- **A source's rules are scattered constants.** Cache windows, per-host budgets and "not stored" promises live in separate files and comments (`priceCache.js`, `priceHostBudget.js`, both off in production).
- **Two request-time readers fetch public pages.** One is in the backend, whose request-path address must not carry crawl-like load (ADR-027).
- **The buyer client pins one spec version** while stores publish a later one. ADR-030 D2 decides negotiation; it is not built.

### What sources publish about use (as read 2026-11-22)

These were read through a summarising reader. Each is re-read directly before a lane's terms record is written (D3). Nothing here is a legal conclusion.

- **The Shopify cross-merchant catalog.**
  - One page gives cache limits: product data and images up to 24 hours, price and availability up to 10 minutes, commission values never. Another of its pages says not to cache results. The stricter reading applies until confirmed.
  - As read, it does not allow a standing product database or commerce index built from it, bulk or systematic collection, or syndication.
  - Tracked ids and links are to be used exactly as returned. Our own client's notes add: a tracked id identifies one buyer's click and is never reused.
  - Five requests a second per client. One page gives lookup as 1 to 50 ids; our client sends 10. To be confirmed.
  - Results are grouped by the platform's own product id across sellers on that platform. The grouping is product-level, not variant-level.
  - Availability is yes or no. No barcode, brand field or SKU is documented in the response.
  - Not stated either way: whether bare ids may be kept as join keys, and whether its offers may be shown beside offers from other sources.
- **A store's own UCP door.** The spec says catalog responses are not transactional commitments, checkout is authoritative, and results should not be reused across sessions without re-validation.
- **Other store platforms.** None was found to offer a cross-merchant catalog. Each offers an app a merchant installs, with catalog, inventory and order scopes. Several have announced UCP support; a shipped endpoint was confirmed for few.
- **Marketplaces.** Affiliate interfaces: offers and tracked links, no order creation.
- **Affiliate network feeds.** Bulk files, refreshed up to a few times a day, often carrying GTIN, brand and part number, storable under each network's terms.

### What comparable systems do

Every public system found keeps an index for discovery and re-checks at checkout. The index comes from merchant feeds, from a platform's own data, or from pages read by a model. The one per-request reader found takes a product URL as input and does not discover. The failure these systems report is staleness. None publishes a latency budget for reading several sources per request.

## Decision

### D1. What Pivota stores and what it reads

- **Stored:**
  - identities and listing pointers (ADR-032);
  - facts per seller and market: protocol profile facts (ADR-030 D8), storefront-by-market capability (ADR-026), evidence route (ADR-025), seller identity (ADR-019);
  - lane terms (D3);
  - decisions and outcomes: the ticket (D7), the hand-off record, the purchase record, order snapshots, commission as reported;
  - demand: what was asked and what could not be answered.
- **Read when a buyer acts on a product:** price and availability, through a lane. The seller's cart total when the buyer selects an offer.
- **A stored price is never served as current.** In a list it is served as `stored`, with its date. Where the buyer acts on a product, the price is read.
- **A live read is not written back,** as today. Whether a dated copy of a door's answer may be kept for lists is owner question 1.
- **A pointer holds bare ids only.** A tracked id, a tracked link, a cart or checkout id and a `continue_url` are never written to a pointer and never cached across buyers.
- **Content** (title, images, description):
  - from a source whose terms allow storage, it is stored as today: our own crawl of permitted pages, affiliate feeds, connected merchants, brand-supplied data;
  - from a lane that allows only a cache window, it is held inside that window and never written to a catalog table.

### D2. One lane interface

- **Every call to a merchant's door, a platform catalog or a platform app is made by the gateway** (ADR-030 D2, extended to catalogs and apps). The backend's request path makes no lane call.
- **Feeds and our own crawl are background jobs in the backend.** They read files and public pages, not doors. They leave by the crawl egress, are paced with every other crawl job, stay under a per-host daily cap, and never follow a tracking link.
- Both kinds obey the same terms record (D3) and return the same result.
- A lane implements whichever of four calls its source supports:

| Call | Meaning | Used by |
|---|---|---|
| `search(query, market)` | find listings | finding real variant ids at a seller we already serve (today); finding other sellers of a known product (D8) |
| `lookup(pointers, market)` | current price and availability for known listings | product, product page, offers; the top list cards |
| `quote(lines, market)` | the seller's cart total for the selected offer | checkout escalation |
| `open_checkout(selection)` | the seller's checkout and its `continue_url` | `create_checkout_session` (ADR-030 D3 path 1) |

- **One result for `lookup` and `quote`:** lane, seller, the pointer asked for, market, amount in minor units with its currency, availability (yes, no, unknown), the time observed, and a status.
- **One status vocabulary,** the store-door reader's, unchanged: `verified`, `currency_mismatch`, `variant_gone`, `not_ucp`, `timeout`, `error`, `skipped` with a reason. A lane that is not a UCP door adds `not_supported` for a call it does not offer.
- **A lane answers for the pointer it was asked about.** If the source returns another variant, the status is `variant_gone` and nothing is substituted.

| Lane kind | search | lookup | quote | open_checkout | Content may be stored |
|---|---|---|---|---|---|
| Platform catalog | yes | yes | no | no | cache window only |
| Store door | yes | yes | yes | yes | no (owner question 1 for a dated price) |
| Platform app the merchant installed | yes | yes | where granted | where granted | per the merchant's grant |
| Affiliate or marketplace interface | yes | where offered | no | no (tracked link) | per programme terms |
| Feed (scheduled) | offline | no | no | no | yes, per network terms |
| Own crawl (scheduled) | offline | no | no | no | yes, where the page permits |

### D3. A lane's terms are data, enforced in one place

- Each lane has one terms record:
  - what may be persisted (bare ids, content, a dated price) and for how long;
  - its request budget;
  - its attribution rule: which carrier, whether a redirect hop is allowed, what must be used verbatim;
  - the markets where its prices were measured correct (ADR-026);
  - the identity it is called with (D10);
  - any display duty, such as a disclosure.
- One module applies the record to every call: cache lifetime, budget, the persistence filter, link composition. No lane code sets its own retention.
- **A lane without a terms record is not called.**
- The record names the page and the date its rules were read from. Where a rule is unstated, the record takes the strict reading until the source answers in writing.

### D4. The price contracts stay as they are

This ADR adds no price vocabulary and changes no served field.

- **List rows** (`search_catalog`, `recommend_products`, `get_alternatives`) carry `price_source` and `price_as_of` as `listPriceTiers.js` defines them:

| Token | Meaning, unchanged |
|---|---|
| `live` | the merchant answered during this request |
| `cache` | an earlier merchant answer still inside its cache window |
| `stored` | our dated copy, from the crawl or a feed |

- **Product and offers** keep their fields: `price_source: merchant_live`, the `live_read` block with its status, `merchant_price_verified`, `stock_verified`.
- **The seller's cart total is final** and is shown verbatim (ADR-030 D1). As built, it is the total before the shipping and tax the storefront adds at checkout, unless the seller lists them.
- **Staleness is the existing rule.** With `PRICE_STALENESS_DEMOTE` on, a `stored` price older than seven days, or undated, is labelled and demoted, and is never the answer to a cheapest or price-capped request while a fresh row exists.
- **The comparable pool is the existing fresh-price contract.** Identity decides `price_comparable` (ADR-032). The best price is picked only from that pool, fresh after the request's live reads, not known to be out of stock, in one currency.
- **The one rule this ADR adds:** a price a lane verified in this request is fresh for that contract, even when the listing has no fresh stored price. Where the code today needs a fresh stored price before the read, that changes behind D8's switch.
- **A read in another currency is `currency_mismatch`** and gives no price (ADR-026).
- **Each cache window comes from the lane's terms record** (D3), not from a constant in the reader.
- **This amends ADR-026 section 4.2.** For a listing a lane reads, a price is proven by a lane answer in the market's currency at request time. The capture cadence applies to stored prices.

### D5. What runs on each tool

| Tool | What runs | Budget today |
|---|---|---|
| `search_catalog` | stored identity and the list-price rule. The top cards may be read live inside the budget; the list never waits beyond it | 600 ms, up to 10 cards |
| `recommend_products`, `get_alternatives` | stored identity and the list-price rule; no live read, as today | none |
| `get_product` | `lookup` for the product's variants | 3 s; 10 ids per call |
| The product page | the same; it may serve the overlay's ten-minute cache, as today | 3 s |
| `get_offers` | `lookup` for every comparable offer | 3 s |
| `create_checkout_session` | `quote` for the selected offer, then ADR-030's paths | 5 s; 3.5 s per call |

- **Which lane answers a `lookup`:**
  - the seller's own door where the seller has one. It is the seller's answer, and its request limit is counted per store, not shared across sellers;
  - a platform app where the merchant installed one;
  - the platform catalog where a door is absent or has failed, and only once open dependency 1 is answered.
- **A cart is created for the selected offer only.** Carts at several sellers to compare totals are not adopted (ADR-029 D6; owner question 3).
- **No buyer contact or delivery detail goes to a seller before the buyer's consent for that order** (ADR-028 D7). A `quote` carries the line items and the market's country, as today.
- **A request that names no market** gets no cart and no quote, and is answered as today, labelled as assumed (ADR-030 D3).
- **When the cart total differs from the price read earlier,** the cart total is shown and the difference is recorded as drift for that lane and seller.
- **A seller with no door, no catalog entry and no app** is served from its `stored` price under the staleness rule, with a link hand-off, as today.
- **Failure never invents a value.** A failed read leaves the stored fields untouched, as today. A failing lane opens its breaker.
- The fleet-wide per-host budget (`priceHostBudget.js`) is turned on before any lane widens. Partner isolation is applied before any lane call (ADR-030 D6).

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
- **Server-side only, never in an id handed out, never logged:** the seller's cart or checkout id and the `continue_url` (ADR-030 D10).
- **Execution does not re-rank.** It applies the rules that exist: ADR-029 D5's check before the buyer's authorization, ADR-030 D3's approval and payment rules, ADR-028 D7's re-ask on any change.
- **One ticket names one seller.** A different seller is a new decision.
- **Attribution, by carrier:**
  - ADR-025 stands on every other carrier: the click is minted at issue time and our click id rides in the carrier that seller's evidence source reads back;
  - **new row in ADR-025 D2's table:** *platform catalog commission; carrier = the catalog's tracked variant id, fetched for this buyer at hand-off, used verbatim, never stored; read back from the platform's earnings report, per saved catalog*.
- **What that row cannot do, as built** (`services/storefront_handoff_record.py`): a catalog-attributed hand-off carries no Pivota click id, and the platform reports the sale per catalog, not per order. Its only issue-time record is the hand-off record, which names the partner agent. So the order joins no click, and no agent share can accrue from it under ADR-025 D5. This is a departure from ADR-025 D1 that already exists in code, in shadow. This ADR records it and does not resolve it (owner question 2).
- **The verbatim rule applies to the platform catalog carrier only.** No redirect hop and no attribute of ours is added to a cart that carries its tracked ids.

### D8. Priceable without a stored price

- **Priceable.** A listing is priceable in a market when either holds:
  - it has a stored offer price, as today; or
  - it is a catalog listing with a pointer on a lane that has a terms record, its seller's door or app endpoint is a stored fact (`execution_routes` today; the ADR-030 D8 facts when built), and recent reads for that seller on that lane succeeded.
- **This amends ADR-007's `has_price` gate and ADR-012's Phase 0b invariant.** "Has a live offer with price > 0" becomes "has one, or is priceable by the second rule".
- **The checkout rule does not move.** An offer that is priceable only by the second rule is buyable in an answer only when its price was read in that request. An offer with a stored price behaves as today.
- Priceable is a stored fact. It is never decided by a request-time fetch (ADR-030 D8).
- **Who may appear does not change.** A seller appears under today's rules: the directory's exclusions (quarantine, test and rejected merchants) apply first; a link hand-off needs no purchasability fact; every agent-paid path needs ADR-029 D6's facts. This ADR adds no fact a seller must have, and removes none.
- **A seller or listing first seen through a lane's `search` is not served in that answer.** A new seller needs a seller record so the exclusions can be applied to it. A new listing goes through intake (ADR-032). See owner question 4.
- **Nothing is written from the platform catalog until open dependency 1 is answered:** no pointer, no derived record. In shadow it is only counted.
- **Bulk outputs carry only what Pivota may store and republish.** Feeds, sitemaps and public pages never contain data held only inside a lane's cache window. ADR-018's rule that an item with no price is not emitted to a feed stands.
- Citation eligibility (ADR-007) is unchanged.

### D9. What each scheduled reader becomes

| Reader | After this ADR |
|---|---|
| Price refresh (one page per listing) | No longer a condition of serving for a listing with a door pointer (D8). It keeps running until owner question 1 says what keeps list prices recent for those listings. It stays, as today, for listings no lane reads |
| Destination sweep | For door-covered listings, a `variant_gone` answer marks the pointer. The page sweep stays for the rest, and for door-covered listings until question 1 is answered |
| Purchasability sweep; cart-link eligibility | Unchanged. ADR-029 D6 needs their facts |
| Retailer ingest | Unchanged for stores without a door. For a store with a door, new listings are found through the door's `search`, by the gateway |
| Affiliate feed reader | Becomes the feed lane, one terms record per network |
| Backend page read for stock; backend checkout preflight | Retired. The backend's request path makes no outbound read |
| Gateway storefront page read for list prices | Retired for stores with a door once the top-card read is enforced. For stores without a door: owner question 5 |

- Each change is its own switch, flipped after a measured comparison: how often the door answer and the stored value disagree, and how many listings would leave serving.
- **How lists keep a recent price once the page refresh stops is not decided here.** The three ways, for owner question 1:
  - **(a) Keep the page refresh** for list prices. It is what runs today, and it is the access a store blocks first.
  - **(b) Keep a dated copy of the door's answer:** what request-time reads return, plus a paced re-read, by the gateway, of listings that lists served recently. One identified call covers ten variants. It reverses today's "nothing is written back" rule, the copy lands in the existing offer row for that listing and market (ADR-011 R6, ADR-026), and it needs the source's answer on keeping a dated price and on paced re-reads.
  - **(c) No stored price for these listings.** A list row has a price only when it was read in the request or sits in a cache window. Cold rows show none, and cheapest or price-capped requests see fewer rows.
- **This ADR's recommendation is (b) for a seller's own door, once the source confirms it; (c) where it does not; (a) only where no lane exists.**

### D10. Identity and conduct on every lane

- One platform profile and signing key (ADR-030 D6); one registered client per platform catalog. Requests are signed where the lane supports it.
- The client negotiates the spec version (ADR-030 D2) and records the version used.
- A lane call stops on a challenge or block. No address rotation, no disguised agent, no solver.
- Request volume follows buyer requests. A lane is never used to collect a catalogue in bulk.

### D11. Reconcilers and drift metrics (ADR-012)

| New stored thing | Upstream truth | Reconciler | Drift metric |
|---|---|---|---|
| Listing pointers | the lane's answers | request-time reads mark `variant_gone`; intake adds. **Coverage follows demand, not the whole set** (amends ADR-012 rule 1): a source's rules do not allow a sweep of it. A pointer not answered for 30 days is marked `unverified` | pointers by time since last answer; `unverified` count; `variant_gone` rate per lane and seller |
| Priceable fact (D8) | pointers, seller facts, recent read outcomes | a periodic recompute, stalest first, counting rows written | listings where priceable disagrees with the last read outcome |
| Lane terms | the source's published rules | not derived: a reviewed file, re-read on a schedule | days since each record's rules were last read |

Tickets, hand-off records and outcomes are records of events, not derived stores.

### D12. Switches

Every new behaviour defaults off. With its flag off, every answer is byte-identical to today's.

| Flag | Repo | Effect |
|---|---|---|
| `SUPPLY_LANE_TERMS` (off, report, enforce) | gateway | the D3 module; in report mode it only logs what it would refuse |
| `UCP_LIVE_SEARCH_READ` with a top-K above 0 (existing) | gateway | the top list cards are read live |
| `SERVING_PRICEABLE_BY_LANE` (off, shadow, on) | backend | D8 |
| `DECISION_TICKET` (off, shadow, on) | gateway and backend | D7 |
| `SHOPIFY_CATALOG_SUPPLY_LANE` (off, shadow) | gateway | catalog `search` and `lookup` are called and counted. Nothing is written or served; no further value exists until open dependency 1 is answered |

Existing flags keep their meaning, and each reader D9 retires is turned off by its own existing flag. Whatever owner question 1 decides gets its own switches then. The purchasability gate and the agent-checkout kill switch apply to all of them.

### D13. Upgrade path, piece by piece

| Existing piece | Production | Becomes | Work |
|---|---|---|---|
| Store-door live read: offers, product, product page | enforce | the store-door lane's `lookup` | wrap in the D2 result and the D3 record; no behaviour change |
| Store-door live read: search | shadow | the same, for the top list cards | measure, then enforce with a top-K |
| Store-priced cart | on | the store-door lane's `quote` | result shape only |
| Live cart on offers, signed token | on | the evidence a ticket carries | keep |
| Store-door variant sourcing | on, brand list | the store-door lane's `search` | from a brand list to demand; results go through intake (ADR-032) |
| Platform catalog client | shadow, attribution only | the platform catalog lane | add `search` and supply `lookup`, counted only until open dependency 1; attribution unchanged |
| List-price contract (`listPriceTiers.js`) | flags not read for this ADR | the D4 contract | turned on before D8 |
| Backend page read; backend checkout preflight | off; shadow | retired | delete after the door read covers them |
| Gateway storefront page read | on | retired where a door exists | D9 |
| Backend `services/merchant_ucp_checkout.py` | one caller | none | goes with its caller (ADR-030 D2) |
| Price refresh, sweeps, ingest | on | D9 | the refresh waits on owner question 1 |
| `execution_routes`, purchasability, liveness | stored | seller facts | add ADR-030 D8 profile facts |
| Seven-day staleness, `has_price` | on | D8 | one new stored fact |
| `priceCache`, `priceHostBudget`, scattered windows | off | the D3 module | consolidate; set each window from the source's published one |
| Adapters for installed apps | built | the platform-app lane | wrapped when a first platform agreement exists |

## Phases

Each phase ships alone, behind its switch. None depends on the payment work, and none changes the partner contract.

1. **Contract.** The D2 result and the D3 terms module in report mode, around the readers that are already on. The list-price contract on. The buyer client negotiates the spec version. Exit: every live read passes through the terms module, and report mode shows no refusal that is not understood.
2. **Top list cards read live.** The search read is enforced with a top-K. The storefront page reader is retired for stores with a door. Exit: the share of top cards answered `live`; list latency inside the budget.
3. **The crawl stops being a condition of serving.** D8 in shadow, then on. Then whatever owner question 1 decides for list prices, and only then does the page refresh stop for door-covered listings. Exit: no listing a lane can read leaves serving; scheduled page reads per day fall by a number predicted beforehand.
4. **Finding other sellers.** Store-door `search` at sellers we already serve feeds intake (ADR-032). Catalog `search` and `lookup` run in shadow and are counted only. Exit: for products partners asked about, how many more sellers each source would add.
5. **The ticket.** D7. Exit: the share of tickets whose quoted cart total equals the merchant's total at checkout.
6. **More lanes.** A second store platform, a marketplace's affiliate interface, affiliate feeds for retailers outside the platforms. Each needs its own terms record and its own market proof. Own crawl shrinks to sellers that partners ask for and no lane reaches.

## What we count

- The share of prices served `live`, `cache` and `stored`, by tool.
- Resolved, eligible sellers per product that partners asked about.
- Drift: cart total against the price read; door answer against the stored price.
- Lane health: timeouts, `variant_gone`, `currency_mismatch`, refusals by the terms module.
- Scheduled page reads per day.
- The share of partner requests that end in a checkout an agent can complete.

## Risks

- **A source changes its rules.** A seller reachable by two lanes survives one change, and the terms record makes a rule change one edit. Many sellers will have one lane.
- **Latency.** A live read adds one to three seconds on product and offers. Lists do not wait.
- **Kept ids may be read as an index.** As read, the catalog's rules do not allow a standing product database and do not say whether bare ids may be kept. Pointers on that lane wait for the answer (open dependency 1).
- **The catalog lane's commission joins no click** (D7). Revenue on it is real but cannot be split by partner as built.
- **A cold listing.** Without a recent price a listing is demoted in lists and so is looked at less. Owner question 1 decides how much of that is accepted.
- **A paced re-read can look like collection.** Option (b) of D9 reads on a schedule. Shaping it by demand and running it through the identified door reduces that risk; only the source's answer removes it.

## Open dependencies

1. A written answer from the catalog's operator: may Pivota keep bare product and variant ids as join keys beside its own match decisions, and show its offers beside offers from other sources.
2. A direct re-read of each source's rules, the two pages that disagree on caching, and the lookup batch limit.
3. For each further store platform: which calls of D2 it offers a buyer-side platform, under what identity and terms.
4. The ADR-030 D8 profile facts and the ADR-026 capability table, which D8 reads.

## Questions for the owner

1. **How lists keep a recent price (D9).** Options (a), (b), (c). This draft recommends (b) for a seller's own door once the source confirms, (c) where it does not, (a) only where no lane exists. Option (b) reverses the 2026-10-14 rule that a live read is never written back, for price and availability only.
2. **Catalog-attributed hand-offs (D7).** They carry no Pivota click, so no agent share accrues under ADR-025 D5. Accept that for this lane, or hold the lane's attribution at shadow until the source offers a per-partner unit?
3. **Comparing totals by cart (D5).** This draft prices a cart for the selected offer only, as ADR-029 D6 implies. The 2026-11-22 "live comparison of a short list" is met by live price reads, without shipping or tax. Is that enough, or should carts at a short list of sellers be designed as an amendment to ADR-029?
4. **First sight (D8).** A listing first seen through a lane, at a seller we already serve: show it in the same answer as its own product, or only after intake as this draft does?
5. **Stores without a door (D9).** Keep the gateway's public-page read for their list prices, or show `stored` prices there and retire the reader entirely?
6. **Which lane after the catalog (phase 6):** a second store platform, or affiliate feeds for retailers outside the platforms?

## Consequences

- For most of the index, a page crawl stops being a condition of serving. It remains for sellers no lane reaches.
- A partner sees more than one seller for a product only when identity is resolved (ADR-032), the seller is eligible, and a price for it can be read.
- Every price a partner receives says how it is known, in the tokens already defined.
- Order execution receives one ticket and does not re-decide.
- What each source permits is written down per lane instead of spread through the code.

## Alternatives considered

- **Keep widening the crawl.** Rejected by the owner on 2026-11-22: it duplicates what platforms serve live, its cost grows with products, markets and refresh rate, and it is the access most easily blocked.
- **No stored identity; search every source on every request.** Rejected: no public system works this way, no source outside one platform offers cross-merchant search, and a seller cannot be served before its facts are stored (ADR-030 D8).
- **Copy the platform catalog into our tables.** Rejected: as read, its rules do not allow a standing index or bulk collection.
- **A new price vocabulary and a new rank order.** Draft 1 proposed both. Rejected: the three-token contract and the ranking decisions already exist.
- **One third-party product interface as the only source.** Not adopted as the base: it puts another party between Pivota and the merchant on identity, attribution and terms. One may be added later as a lane under D2 and D3.
