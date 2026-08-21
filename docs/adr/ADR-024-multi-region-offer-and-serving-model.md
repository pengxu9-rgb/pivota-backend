# ADR-024: Region is a request dimension, not a global constant

**Status:** Accepted (2026-08-21; Phase 0 + Phase 1 merged same day — #1798, #1799, #2071, #1803, then #1807 invariant + #2078 tripwire; open questions resolved and the Phase 2 entry gate RUN, below)
**Decision owner:** peng
**Builds on:** ADR-001 (canonical record vs supplier), ADR-012 (catalog convergence),
ADR-018 (connection layer and priced serving lane), ADR-021 (PIVOTA-Agent is the protocol gateway)
**Numbering note:** ADR-023 is claimed by the open UCP hosted-payment-escalation ADR in PIVOTA-Agent (#2005).

## Context

Today the commerce index serves one region, and region enters the system through
**two mechanisms, not one** — a first draft of this ADR claimed one predicate was
the whole region model, and review falsified that:

**Mechanism 1 — a stored, build-time verdict.** The US test is one predicate:

```python
# services/index_pipeline_state_service.py:554
_HAS_US_OFFER_EXISTS = priced_offer_exists_sql(
    "cp.product_key", extra_predicate="upper(trim(coalesce(co.currency, ''))) = 'USD'")
```

But it does not run at request time. It runs inside `_classify_product` during the
index-pipeline recompute, where `evaluate_agent_decision_gates` turns a missing US
offer into `blocker_code = "no_us_offer"`, and `serving_eligible` requires
`blocker_code == "none"`. **The region verdict is baked into the stored
`serving_eligible` boolean at build time, where no request — and therefore no
buyer region — exists.** The core predicates (quality ≥ 71.4, image, description,
identity, price > 0) are region-neutral; region enters only through that
flag-gated blocker conjunct, and the flag is live on prod — the 963 keys blocked
`no_us_offer` prove it. Every consumer of the stored bit inherits the verdict:
sitemap candidates (`canonical_sitemap_candidates.eligibility_predicate`),
`pipeline_stage` public/shadow assignment, IndexNow, catalog health
(`foreign_market` classification), and the merchant-facing copy in
`serving_status_service` ("Add US pricing…").

**Mechanism 2 — request-time cross-border tagging.** `services/offer_buyability.py`
already tags every served offer `domestic`/`cross_border` against a request market
and selects an `is_buy_pick`, driven from `pivot_query_service.py:2283`
(`annotate_offer_nodes(item.offers, request.market)`) and from
`routes/agent_pdp_v1.py` via the env knob `AGENT_PDP_SERVING_MARKET` (default US).
So the decision layer has *already* asked what region a request is for — the
plumbing exists on one lane and is defaulted-US on the rest.

As we integrate Minds and other agentic partners, whose buyers span many regions
behind one endpoint, the question becomes: **"does this product have an offer
buyable in the region this request is for?"** — asked per request, answered
consistently by both mechanisms.

### What `market` actually means (and does not)

`catalog_offers` carries `market` and `currency` per offer, but they are not
symmetric in trustworthiness:

- `market` is `NOT NULL DEFAULT 'US'` (migration 149: "refine to real per-offer
  geo when modeled"). The serving code explicitly refuses to trust it —
  `has_us_offer` is "Derived from CURRENCY, not catalog_offers.market: … it is
  'US' for every row and carries no signal" (index_pipeline_state_service.py:600).
- Where `market` *is* non-US, it was stamped by
  `scripts/backfill_offer_market_currency.py` from the storefront's `/meta.json` —
  **the same source that stamped `currency`**, so any measured market~currency
  correlation is true by construction, and `market` there means "store's home",
  not "buyable by". `services/storefront_currency.py` is explicit: market and
  currency are "different axes (destination served vs store base currency) — a
  KR/HK exporter legitimately prices in USD."
- `currency` IS written truthfully by every writer (0 of 18,279 live priced
  offers null), and is exactly how the INR/ZAR/GBP mispricings were detected.

Therefore: **Phase 1 gates on currency, not on `market`.** The predicate is named
what it is — `has_offer_priced_for_region` — and fulfilment reachability is
explicitly out of scope until modeled. When Phase 2 writers begin stamping
`market` as *destination* (Shopify Markets capture), that semantics change is
declared, and old store-home values are not mixed with new destination values.

### The supply, measured (prod, 2026-08-21)

Of 14,981 servable offers (unsuppressed, priced): **1,843 non-USD (12.3%), held
by 48 merchants** — 29 single-currency regional storefronts plus 19
multi-currency merchants. (The catalog's full merchant population is ~302; most
single-currency merchants are USD-only.)

GBP 780 · EUR 608 · JPY 333 · AUD 26 · SEK 25 · KRW 23 · HKD 22 · SGD 14 · CAD 12
— every code already inside the decision layer's 14-currency allowlist.

Two acquisition populations:
- **Regional storefronts (1,184 offers, 64%)** — `dearbarber.co.uk`, `arencia.jp`,
  `roundlab.co.kr`; nearly all crawled (`external_product_seeds_mirror_v1`).
- **Multi-currency merchants (659 offers, 36%)** — every one exactly two
  currencies, always `X,USD`: the Shopify-Markets pattern.

Separately, 963 content_keys are blocked `no_us_offer`, all `priced_but_not_usd`,
all quality ≥ 71.4, with 573 already carrying genuine merchant-set USD prices via
Shopify Markets (probe proven in `scripts/capture_us_market_offers.py`). Under
this ADR those keys become **displayable** supply for their own regions —
*displayable, not yet buyable*: the cohort is dominated by crawled seeds that
`create_checkout` refuses today (`no_real_variant_identity`), so the transact leg
is a separate, explicitly unproven claim.

### The recurring failure mode this ADR must not feed

Currency has produced the same defect **four** times, in four layers:

1. **Ingestion (2026-07-28):** Mintree INR prices published as `"USD"` on the
   unauthenticated ACP feed (`services/offer_currency_policy.py` docstring).
2. **Read (PR #2065):** `extractCatalogCandidatePrice` discarded a row's declared
   currency for scalar seeds and stamped USD. Against a $40 ceiling, 1,172 offers
   read falsely *conforming* and 671 falsely *over* — including all 333 JPY rows.
3. **Presentation (same PR):** price positions sorted by currency code
   alphabetically, labelling EUR 200 the "lowest" of [EUR 200, GBP 5, USD 50].
4. **Selection (live today, unfixed):** `offer_buyability`'s buy-pick is
   `min(pool, key=… float(price))` over a cross-border fallback pool that mixes
   currencies — 4500 (JPY) loses to 12 (GBP) as floats, on the pivot/UCP lane.

Each is a comparison or label asserted across units we cannot compare. Any design
that introduces conversion introduces a fifth. This history is the strongest
constraint on the decision.

## Decision

**Region becomes an explicit request dimension carried end to end; the index
stores one honest offer per (product, region-pricing); the stored eligibility
verdict is un-baked from any single region on a named, per-consumer schedule —
not silently.**

Five commitments:

1. **Sibling offers, never rewrites.** One `content_key` → N `catalog_offers`,
   each keyed by its own pricing currency (and, once destination semantics are
   declared, market), each honestly attributed. We never rewrite a foreign offer
   into USD. This generalizes the already-decided sibling-US-offer design.

2. **One region predicate, parameterized, gating on currency.**
   `has_offer_priced_for_region(region)` replaces `_HAS_US_OFFER_EXISTS`, built on
   `priced_offer_exists_sql`'s existing `extra_predicate` seam, mapping region →
   expected pricing currency. Fulfilment reachability is a second, weaker gate we
   do not yet model and do not pretend to.

3. **The stored verdict un-bakes consumer by consumer.** `serving_eligible`'s
   region conjunct cannot simply "take a parameter" — it is stored. Each consumer
   of the stored bit gets a named disposition (see Phase 2a) before the US
   conjunct moves out of the stored formula. Until then the stored bit keeps
   today's US semantics and is treated as `serving_eligible_us` in all new code.

4. **The decision layer receives `buyer_region` and derives ceiling currency from
   it.** No component infers region from currency, or currency from language.
   Existing region signals (`request.market` on the pivot lane,
   `profileSummary.region`, `derivePhotoModulesMarket`'s language inference) are
   reconciled to the request's `buyer_region`, not left to fight it.

5. **No FX conversion reaches a buyer — ever.** Cross-currency comparison remains
   refused exactly as `classifyRecoCandidateAgainstPriceCeiling` refuses it. If
   cross-currency *ranking* is ever wanted, it ships separately gated, may
   reorder, and may never produce a displayed number or a stated verdict. This
   decision is explicitly coupled to the thin-region fallback question (see Open
   questions): if the fallback is "show foreign-priced offers with a marker",
   mixed-currency pools become the *normal* thin-region case and ranking-only FX
   moves into scope at that moment — not before.

## Options Considered

### Option A: One deployment (or config) per region

| Dimension | Assessment |
|-----------|------------|
| Complexity | Low to build, High to operate |
| Cost | N× infra, N× index |
| Fit for partners | **Wrong shape** |

One Minds integration serves buyers in many regions through one endpoint; we
cannot ask a partner to pick a base URL per shopper. Splits identity. Rejected.

### Option B: Region as a request dimension, offers as siblings *(recommended)*

| Dimension | Assessment |
|-----------|------------|
| Complexity | Medium — threading + un-baking the stored verdict + one acquisition producer |
| Cost | Row growth in `catalog_offers`; no new store |
| Scalability | Good — new region is data, not schema |
| Team familiarity | High — extends the sibling-offer design |

**Pros:** schema already carries per-offer currency; the predicate seam exists;
adding a region becomes acquisition. **Cons:** region must be threaded through
recall, gating, caching, prompting, and *every cache key on the path* — and the
stored-verdict un-baking is a real, consumer-by-consumer migration, not a
parameter change. The first draft of this ADR underestimated that; review
corrected it.

### Option C: Normalize everything to USD at ingestion with stored FX

**Rejected — for display and verdicts, absolutely.** It is the Mintree incident
as an architecture, and the repo has now shipped that defect four times in four
layers. A converted price is not a price any merchant will honour at checkout.
The narrow carve-out (ranking-only FX, never displayed, never a verdict) is
defined in commitment 5 and is *not* Option C.

### Option D: Per-region precomputed eligibility table

**Deferred, with a sharper justification than the first draft.** The existing
system is already D-shaped *for one region* — a precomputed per-content_key
eligibility row with a blocker code, consumed by merchant copy, health, and
scripts. That is precisely why the un-baking in Phase 2a must be explicit: those
consumers exist. But building the N-region version of that table before the read
paths exist would create a second source of truth against `catalog_offers` — the
split-brain `priced_offer_sql`'s docstring records happening once already.
**Revisit when** the runtime region `EXISTS` shows up in query plans, or when a
consumer genuinely needs per-region blocker copy (merchant portal showing "no
offer for JP" is the likely first).

## Consequences

**Easier:** adding a region becomes acquisition; the 963 blocked keys become
displayable regional supply; merchant status copy can be region-specific; honest
regional pricing becomes a partner-facing feature.

**Harder:** every cache key, prompt, and telemetry dimension acquires a region —
omission is a silent cross-region leak CI will not catch; the stored-verdict
migration touches sitemap/IndexNow/health/merchant-copy; offer selection needs a
real rule (the current one is defect #4 above); merchant region support becomes
state to acquire and keep fresh.

**Revisit when:** region `EXISTS` in slow plans → Option D; a partner needs
cross-currency ranking → ranking-only FX per commitment 5; region count > ~10 →
row growth.

## Action Items

**Phase 0 — live defects and guards (before any region work)**
1. [ ] Fix `offer_buyability`'s cross-currency buy-pick: partition the pool by
       currency, prefer the serving market's expected currency, never `min()`
       across currencies; a pool with no same-currency priced offer picks by
       stock + stable order and is marked, not ranked. (Defect #4; live on the
       pivot/UCP lane today.)
2. [ ] Add the market/currency invariant check: no served offer's `currency` may
       disagree with its `market`'s expected currency without a recorded reason.
       Quarantine or reclassify today's anomaly — **433 EUR offers stamped
       `market='US'`** from one `universal_product_sync` merchant with a bare
       UUID as `source_domain` (23% of all servable non-USD offers) — *before*
       any predicate or Phase 2 acquisition math trusts these rows.
3. [ ] `formatPriceLabel` renders `$` for 10 of the 14 known currencies (already
       filed); the budget gate's unmarked foreign-currency admit (already filed).

**Phase 1 — make region explicit, zero behavior change**
4. [ ] Thread `buyer_region` (ISO-3166-1 alpha-2) through the gateway request
       contract into recall, defaulting to `US` — **with a `region_source:
       explicit|defaulted` telemetry dimension per partner, and a per-partner
       `region_required` flag** so the silent default is a measured transition,
       not a permanent wrong-region path. (The pivot lane's optional `market`
       field that partners never send is evidence the silent default is the
       steady state unless instrumented.)
5. [ ] Introduce `has_offer_priced_for_region(region)` beside (not replacing)
       `_HAS_US_OFFER_EXISTS`, on the `extra_predicate` seam, with the region →
       currency map in one module. The stored bit keeps US semantics; new code
       reads the parameterized form.
6. [ ] **Audit every cache key on the recall/serving path for a region
       dimension** — `reco_recall_pool_cache` (add region to the key hash, bump
       `RECO_RECALL_POOL_CACHE_VERSION`; the price-ceiling dimension was added
       exactly this way), the gateway's in-process Maps in routes.js, and any
       backend caches not already market-keyed (the external-seed search cache
       and outbound-link cache already are).
7. [ ] Derive the price-ceiling currency from `buyer_region`; sweep the USD/US
       assumption inventory, not just two sites: `resolveConcernFrameworkBudgetCeiling`
       ('USD' hardcoded), prose budget parsing ($-symbol), `priceDeltaUsd`
       tradeoff copy in auroraStructuredMapper.js, the `normalizePriceObject` /
       chatCardFactory USD fallback stamps, `derivePhotoModulesMarket`'s
       language→market inference, the ACP feed's `currency or "USD"`, and
       `AGENT_PDP_SERVING_MARKET`'s env default.

**Phase 2 entry gate — falsify cheaply before spending**
8. [x] Three read-only probes — RUN 2026-08-21, results below: (a) *supply* — count content_keys passing every
       region-neutral gate that hold a GBP (then JPY) offer; if ≈0, the unlock is
       illusory; (b) *transact* — run 5 survivors through search → PDP →
       `create_checkout`; the seed-cohort refusal predicts failure, which would
       reframe Phase 2 from "unlock supply" to "acquire buyable siblings";
       (c) *demand* — Minds request share by shopper region from gateway edge
       logs; if non-US < 1%, defer Phase 2 cheaply.

**Entry-gate results (measured on prod, 2026-08-21):**

- **(a) Supply — real.** The blocked cohort is 418 keys (down from 963 on
  2026-08-14; USD-sibling capture has been landing since). By currency:
  GBP 323, JPY 52, EUR 19, SGD 14, HKD 5, KRW 4, AUD 1 — all quality-passed by
  construction.
- **(b) Transact — fails 5/5, as predicted.** The live UCP door requires OAuth
  non-interactively unavailable, so the refusal predicate itself was evaluated
  against prod rows (`is_real_variant` + the mirror-title rule in
  catalog_variant_promoter — deterministic). Five candidates across five
  merchants: four carry a single variant whose title mirrors the product title
  (the mirror-script synthetic), one is a literal `AUTO-…` placeholder. Every
  one would be refused `no_real_variant_identity`. **Phase 2b is therefore
  "acquire buyable siblings" — prices AND variant identity — not "unlock
  existing supply."** Displayable ≠ buyable stands.
- **(c) Demand — zero.** The gateway edge-log window held exactly two
  requests, both from the probe session itself. Per this gate's own rule,
  Phase 2b acquisition DEFERS until partner traffic exists or a partner
  declares non-US regions. Correctness work (Phase 0/1, the invariant, the
  tripwire) is merged, so deferral costs nothing.

**The 433-EUR-as-US cohort is dispositioned: a real merchant, not garbage.**
"Tsingtao Bear" — an active, indexable, internal Wix merchant
(`universal_product_sync`) genuinely pricing in EUR; the UUID-as-source_domain
is its Wix site id and `market='US'` is migration 149's default. 14 products,
433 offer rows, none serving-eligible. Remediation: fix the WRITER
(universal_product_sync stops stamping the US default; derive market from
store settings or leave NULL) and leave the rows until `market` becomes
load-bearing — quarantine would be wrong, the currency is honest. The #1807
invariant counts the cohort visibly in the meantime.

**Phase 2a — un-bake the stored verdict (behavior change, per consumer)**
9. [ ] Name the disposition for each stored-bit consumer before moving the US
       conjunct: sitemap candidates (stay US? go region-neutral?), IndexNow,
       `pipeline_stage`, catalog health's `foreign_market` class, merchant
       status copy (becomes per-region), `capture_us_market_offers.py`.
       Rename the blocker `no_us_offer` → `no_regional_offer` with region in the
       detail, alias preserved until consumers migrate.

**Phase 2b — acquire regional offers**
10. [ ] `merchant_region_support` (which markets a store prices/ships to), from
        Shopify `/localization` availability + declaration + observation.
11. [ ] Generalize the proven Markets capture (multipart `/localization` POST
        with `_method=put` + `country_code`, verify `/cart.js`, read
        `/products/<handle>.js`) from US-only to per-region, writing sibling
        offers with declared destination semantics. Start with the 573 proven
        keys.
12. [ ] Centralize the offer-selection rule (N regional offers → which to show)
        in one module, the way `priced_offer_sql` centralizes "is it priced".

## Resolved questions (decision owner, 2026-08-21)

- **Pricing region vs fulfilment reachability — DECIDED: pricing region.**
  `has_offer_priced_for_region` gates on the region's expected pricing currency,
  exactly as Phase 1 shipped it. Fulfilment reachability stays unmodeled until
  `merchant_region_support` (Phase 2b) makes it data; it may then become a
  second, additive gate — never a retroactive redefinition of this one.

- **Thin-region fallback — DECIDED: none.** A region with no priced offers gets
  an honest empty answer, not foreign-priced filler with a marker. "We have
  nothing buyable in your region" is a true statement; a GBP price shown to a
  JP buyer as fallback is a soft version of the comparison this ADR forbids,
  and it would make mixed-currency pools the normal thin-region case.

- **FX-ranking — DECIDED: not built; its trigger was declined above.** With
  pricing-region gating and no fallback, every served pool is single-currency
  by construction, so ranking never needs a rate. The residual case — a
  foreign-currency row inside a region's pool — is mislabeled supply (see the
  433-EUR-as-US anomaly), an ingestion defect for the Phase 0 invariant, not a
  ranking problem to absorb. TRIPWIRE instead of a ranker: count
  `unknown`-classified rows actually served, per region; materially nonzero
  means clean data, not convert it. If a partner ever asks for cross-region
  price comparison, that is EXPLICIT comparison with the rate and its
  timestamp disclosed — its own ADR, its own trigger, never a latent ranker
  capability. Commitment 5's no-FX-to-buyers rule is thereby unconditional.

- **Where `buyer_region` comes from** (still open, low stakes): prefer the
  explicit partner-supplied field; never infer from currency; reconcile
  `request.market` / `profileSummary.region` / language inference to it as
  those surfaces are touched.
