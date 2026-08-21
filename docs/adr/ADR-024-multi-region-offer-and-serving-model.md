# ADR-024: Region is a request dimension, not a global constant

**Status:** Proposed (2026-08-21)
**Decision owner:** peng
**Builds on:** ADR-001 (canonical record vs supplier), ADR-012 (catalog convergence),
ADR-018 (connection layer and priced serving lane), ADR-021 (PIVOTA-Agent is the protocol gateway)
**Numbering note:** ADR-023 is claimed by the open UCP hosted-payment-escalation ADR in PIVOTA-Agent.

## Context

Today the commerce index serves exactly one region. `serving_eligible` is a single
boolean per `content_key`, and the region question is asked once, in one place, as
`has_us_offer`:

```python
# services/index_pipeline_state_service.py:554
_HAS_US_OFFER_EXISTS = priced_offer_exists_sql(
    "cp.product_key", extra_predicate="upper(trim(coalesce(co.currency,''))) = 'USD'")
```

That predicate is the whole of our region model. Everything downstream inherits it:
`agent_decision_gates.BLOCKER_NO_US_OFFER`, the merchant-facing copy in
`serving_status_service` ("Add US pricing…"), and by omission the decision layer,
which has never needed to ask what region a buyer is in because there has only
ever been one answer.

As we integrate Minds and other agentic partners, that assumption stops holding.
A single agent integration serves buyers in many regions from one endpoint. The
question the index must answer changes from *"does this product have a US offer?"*
to **"does this product have an offer buyable in the region this request is for?"**

### What we already have (measured on prod, 2026-08-21)

The schema is further along than the gate is. `catalog_offers` already carries
`market` and `currency` per offer row, and identity/offer separation is already
the decided architecture — one `content_key`, N offers, US-buyability arriving as
an *attached sibling offer* rather than a rewrite of the honest foreign one.

Of 14,981 servable offers (unsuppressed, priced):

| | Offers | Merchants |
|---|---:|---:|
| USD | 13,138 | — |
| **Non-USD** | **1,843 (12.3%)** | **48** |

GBP 780 · EUR 608 · JPY 333 · AUD 26 · SEK 25 · KRW 23 · HKD 22 · SGD 14 · CAD 12.
Every one of those codes is already inside the decision layer's 14-currency
allowlist. `market` and `currency` track each other almost perfectly.

Two distinct populations, and the difference matters for acquisition:

- **283 single-currency merchants hold 1,184 non-USD offers (64%)** — genuinely
  regional storefronts (`dearbarber.co.uk`, `arencia.jp`, `roundlab.co.kr`),
  nearly all arriving via `external_product_seeds_mirror_v1`, i.e. crawled.
- **19 multi-currency merchants hold 659 (36%)** — every one is exactly two
  currencies, always `X,USD`. That is the Shopify-Markets pattern: one store
  already presenting a home currency alongside USD.

Separately, 963 `content_key`s are blocked `no_us_offer`, **all of them
`priced_but_not_usd`** and all quality-scored ≥ 71.4. This is high-quality supply
withheld purely by the region gate — the largest single unlock left in the index.
A prod probe found **573/963 recoverable** because the merchant already sets a
genuine US price via Shopify Markets; we simply never captured it.

### The recurring failure mode this ADR must not feed

Currency has produced the same defect three times, in three layers:

1. **Ingestion (2026-07-28):** Mintree INR prices published as `"USD"` — rupee
   amounts served as dollars on the unauthenticated ACP feed.
2. **Read (PR #2065, this week):** `extractCatalogCandidatePrice` discarded a
   row's declared currency for scalar price seeds and stamped USD. Measured
   against a $40 ceiling, 1,172 offers would have read falsely *conforming* and
   671 falsely *over* — including all 333 JPY rows, making the entire
   Japanese-priced catalog invisible to any budget-constrained search.
3. **Presentation (same PR):** price positions sorted by currency code
   alphabetically, labelling EUR 200 the "lowest" of `[EUR 200, GBP 5, USD 50]`.

Each was a *comparison or a label asserted across units we cannot compare*. Any
multi-region design that introduces conversion introduces a fourth instance. That
history is the strongest constraint on this decision.

## Decision

**Region becomes an explicit request dimension carried end to end, and the index
stores one honest offer per (product, region) rather than one privileged offer.**

Four commitments:

1. **The index stores regional offers as siblings.** One `content_key` → N
   `catalog_offers`, each keyed by its own `(market, currency)`, each honestly
   attributed to the source that produced it. We never rewrite a foreign offer
   into USD. This generalizes the already-decided sibling-offer design from
   "US" to "N regions".

2. **The serving gate is parameterized, not duplicated.** `serving_eligible`
   stays region-neutral (quality, image, description, identity, price > 0 — all
   of it already is). The region test stays exactly where it is today, as one
   `extra_predicate` on `priced_offer_exists_sql`, with the region supplied
   rather than hardcoded.

3. **The decision layer receives a buyer region and derives its price ceiling
   currency from it.** No component infers currency from a hardcoded default.

4. **No FX conversion reaches a buyer.** Cross-currency comparison remains
   refused, exactly as `classifyRecoCandidateAgainstPriceCeiling` refuses it
   today. If we later want cross-currency *ranking*, it ships as a separately
   gated capability that may reorder results but may never produce a displayed
   number or a stated verdict.

The single most important structural point: **the core serving predicate is
already region-neutral, and the region question is already isolated into one
`EXISTS` over a table that already carries `market` and `currency`.** This is a
parameterization and an acquisition problem, not a re-architecture.

## Options Considered

### Option A: One deployment (or config) per region

| Dimension | Assessment |
|-----------|------------|
| Complexity | Low to build, High to operate |
| Cost | N× infra, N× index |
| Scalability | Poor — linear in regions |
| Fit for partners | **Wrong shape** |

**Pros:** No code change; the US-only assumption stays true within each deployment.
**Cons:** Fatal for the actual requirement. One Minds integration serves buyers in
many regions through one endpoint; we cannot ask a partner to pick a different
base URL per shopper. Also duplicates the catalog and splits identity.

### Option B: Region as a request dimension, offers as siblings *(recommended)*

| Dimension | Assessment |
|-----------|------------|
| Complexity | Medium — threading + one acquisition producer |
| Cost | Row growth in `catalog_offers`; no new store |
| Scalability | Good — new region is data, not schema |
| Team familiarity | High — extends the existing sibling-offer design |

**Pros:** Schema already supports it (`market`, `currency` per offer). The region
gate is one predicate. Adding a region becomes an acquisition task, not a
migration. Keeps every price honestly attributed to the merchant who set it.
**Cons:** Region must be threaded through recall, gating, caching and prompting —
and *every cache key* becomes a correctness hazard until it includes region.

### Option C: Normalize everything to USD at ingestion with stored FX

| Dimension | Assessment |
|-----------|------------|
| Complexity | Medium |
| Cost | Rate source + staleness policy |
| Scalability | Good |
| Risk | **Unacceptable** |

**Pros:** Every downstream comparison "just works"; one currency everywhere;
no threading.
**Cons:** It is the Mintree incident as an architecture. A converted price is not
a price the merchant will honour, and a buyer shown "$44" for a £35 item has been
quoted a number no checkout will match. It also destroys the information needed to
render the real price, and rate staleness turns every stored price into a slowly
rotting claim. Rejected on the strength of three separate live defects of exactly
this shape.

### Option D: Per-region precomputed eligibility table

`content_key_region_eligibility(content_key, region, eligible, blocker_code)`.

| Dimension | Assessment |
|-----------|------------|
| Complexity | Medium-High |
| Cost | Row count × regions; a new pipeline to keep fresh |
| Scalability | Good for read, costly to maintain |

**Pros:** Per-region blocker reasons for merchant-facing copy ("no offer for JP")
come for free; read path is a single indexed lookup.
**Cons:** Premature. The region test is a cheap `EXISTS` over an already-indexed
table; precomputing it adds a second source of truth that can go stale against
`catalog_offers` — the exact split-brain `priced_offer_sql` exists to prevent.
**Revisit if** the runtime `EXISTS` shows up in query plans, not before.

## Trade-off Analysis

**B vs C is the load-bearing choice**, and it is not primarily a performance
trade-off — it is a question of what we are willing to assert. C buys uniformity
by manufacturing a number no merchant agreed to. This codebase has already shipped
that mistake three times and paid for it three times. B keeps every price
attributable to the merchant that set it, and pays for that with threading work
and cache-key discipline. That is the right price to pay.

**B vs D is a timing choice, not a philosophy choice.** D is where B goes if the
runtime predicate becomes a bottleneck. Building D first would create a derived
eligibility store before we know its access pattern, and the repo's own history
(`priced_offer_sql`'s docstring) is explicit about what happens when two
components answer the same question from different sources.

**The honest cost of B** is that region silently becomes part of the identity of
every cached artifact. `reco_recall_pool_cache` is keyed on
`(cache_key, step_family, lang, catalog_surface, planner_mode)` — **not region.**
Ship B without adding that dimension and a pool recalled for a JP buyer will be
served to a US buyer. This is not hypothetical: a price-ceiling cache-key
dimension had to be added for exactly this reason, and the cache version bumped to
orphan the old rows.

## Consequences

**Easier**
- Adding a region becomes data acquisition, not a migration.
- The 963 `no_us_offer` keys stop being blocked supply and become *supply for
  another region*, immediately serveable to a buyer in that region.
- Merchant-facing status copy can finally be specific: "no offer for JP" rather
  than a US-shaped message shown to a Japanese merchant.
- Honest per-region pricing is a partner-facing feature, not an internal detail.

**Harder**
- Every cache key, every prompt, and every telemetry dimension acquires a region.
  Omitting it anywhere is a cross-region data leak that CI will not catch.
- `resolveConcernFrameworkBudgetCeiling` hardcodes `currency: 'USD'`; prose budget
  parsing ("under $40") assumes the symbol. Both need the request's currency.
- More offers per product means the "which offer do we show" question gets a real
  answer for the first time — a selection rule, not `offers[0]`.
- Merchant region support becomes state we must acquire and keep fresh.

**Revisit when**
- Runtime region `EXISTS` appears in slow-query plans → build Option D.
- A partner genuinely needs cross-currency ranking → design it as ranking-only,
  never display.
- Region count exceeds ~10 → revisit row growth in `catalog_offers`.

## Action Items

**Phase 1 — make region explicit (no behavior change)**
1. [ ] Thread `buyer_region` (ISO-3166-1 alpha-2) through the gateway request
       contract into recall, defaulting to `US` when absent so today's behavior
       is byte-identical.
2. [ ] Parameterize `_HAS_US_OFFER_EXISTS` → `has_offer_for_region(region)`,
       keeping `priced_offer_exists_sql`'s existing `extra_predicate` seam. Rename
       the blocker `no_us_offer` → `no_regional_offer` with the region in the
       detail, preserving the old code as an alias until consumers migrate.
3. [ ] **Add `region` to `reco_recall_pool_cache`'s key and bump the cache
       version** — before any multi-region traffic exists, not after.
4. [ ] Derive the price-ceiling currency from `buyer_region`; remove the
       hardcoded `'USD'` in `resolveConcernFrameworkBudgetCeiling`.

**Phase 2 — acquire regional offers**
5. [ ] Introduce `merchant_region_support` (which markets a store actually prices
       and ships to), sourced from Shopify `/localization` availability, merchant
       declaration, and observation.
6. [ ] Generalize the proven Shopify Markets capture (multipart POST to
       `/localization` with `_method=put` + `country_code`, verify via
       `/cart.js`, read `/products/<handle>.js`) from US-only to per-region,
       writing sibling offers. Start with the measured 573/963 recoverable keys.
7. [ ] Define the offer-selection rule for a product with N regional offers, and
       put it in one module the way `priced_offer_sql` centralizes "is it priced".

**Phase 3 — presentation and truthfulness**
8. [ ] Fix `formatPriceLabel`: it renders `$` for 10 of the 14 known currencies
       (already filed).
9. [ ] Decide the budget-gate behavior for a foreign-currency row — admit with an
       honest marker rather than silently (already filed).
10. [ ] Add an invariant check: no served offer's `currency` may disagree with its
        `market`'s expected currency without an explicit, recorded reason. This is
        the check that would have caught Mintree, and it would flag today's
        anomaly: merchant `merch_e68c20b0189746d0` carries **433 EUR offers
        stamped `market='US'`** via `universal_product_sync`, with a bare UUID
        where `source_domain` should be a hostname — 23% of all servable non-USD
        offers, and it looks like an ingestion defect rather than a real store.

## Open questions

- **Region vs market vs shipping destination.** `market` today conflates "where
  the store is" with "who can buy". A UK store shipping to the US is a US-buyable
  offer with a GBP price. Does `buyer_region` gate on *pricing* region or on
  *fulfilment* reachability? This ADR assumes pricing region; fulfilment
  reachability is a second, weaker gate we do not yet model.
- **Does the buyer's region come from the partner, the shopper, or both?** Minds
  knows its shopper's locale; we should prefer an explicit request field over
  inference, and never infer region from currency (that re-derives what we store).
- **What is the fallback when a region has no offer?** Refusing is honest but
  yields empty answers in thin regions; showing a foreign-priced offer with an
  explicit "priced in GBP" marker is likely better, and is a product decision.
