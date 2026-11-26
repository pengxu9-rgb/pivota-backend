# ADR-034: One written order for a product's offers, and the reasons returned with it

**Status:** Proposed, 2026-11-24, revised twice the same day after review. It records three owner decisions of 2026-11-24 (see "Owner decisions") and the owner's answers of 2026-11-24 to its five questions (see "Resolved questions"). Nothing here is built or switched on by this ADR.

**Decision owner:** Founder. **Runtime owners:** the backend's `offers.resolve` code owner and the agent gateway's `get_offers` code owner, to be named at acceptance. Both are needed: the backend cuts the offer list before the gateway sees it, and the gateway reads merchants live after that cut.

## Amendment 1 (owner, 2026-11-26): offers found live are labelled, and only confirmed ones compete

*Decided by the owner on 2026-11-26; this ADR stays Proposed. Where it conflicts with the text below, this amendment governs. Companions: ADR-029 Amendment 2, ADR-031 Amendment 1, ADR-032 Amendment 1, ADR-033 Amendment 1.*

1. **The best-offer filter gains a condition.** An offer is never the best offer unless it is `comparable`: it is for the listing asked about, or it is matched to that listing at variant grain, either live (`confirmed`, ADR-032 Amendment 1) or by a stored match.
2. **Rule 0 keeps its one meaning, the fit tier.** A live `confirmed` match takes the value `exact`. A live `likely` match takes the value `loose`. Nothing new is added to the order, so this ships as part of version 1.
3. **Each offer carries its match.** The fields are:
   - `match.status`: `confirmed` or `likely`;
   - `match.basis`: which fields agreed, and the source of each;
   - `match.grain`: variant or family;
   - `found_via`: the lane and the time it answered;
   - `comparable`: as item 1 defines it.

   `rank_facts` carries the match as rule `match`.
4. **The answer says what was asked.** It names the kinds of source that were asked (our catalog, a platform catalog, store doors) and whether each answered inside the budget. It never names a seller, and never one that was not admitted. It flags a product that no source returned.
5. **Recorded for D3, the later product ranking.** For `get_alternatives`, the owner defines a dupe as a product of another brand that does the same job, at any price (owner, 2026-11-26). A same-brand product that does the same job is an alternative, not a dupe. The relationship graph's rule that a dupe costs no more than the product is removed when D3 is built.
6. **ADR-033 is accepted** (owner, 2026-11-26), with its own Amendment 1. The "Related" entry and the admitted-sellers filter below are updated to say so.
7. **Switch.** The match fields and the new filter condition ride the gateway switch this ADR already names for its reason fields. Serving live offers at all is gated by ADR-031's `LIVE_LANE_SERVE`.
8. **Text below that this amendment overrides:**
   - the best-offer filter, which gains item 1's condition;
   - rule 0's row and the `match` value types: a live `confirmed` match takes the value `exact`, and a live `likely` match takes `loose` (item 2).

## Related

- **ADR-002 (accepted):** every statement to an agent carries its evidence type and provenance. Today `get_offers` emits `evidence.grade: null`, `evidence.method: 'merchant_reported'` and `evidence.sources: []` on every offer.
- **ADR-025 (proposed), Q4:** an owner decision of 2026-09-24. Whether Pivota can earn on a seller never moves it up or down, including as a tie-break.
- **ADR-026 (accepted):**
  - for a buyer market, only offers in that market's currency, then a domestic storefront before a cross-border one, "then ranks as today" (Decision 5);
  - cross-border offers are served, ranked below domestic ones and labelled (ADR-026's Resolved question 3);
  - one retailer counts as one seller (Decision 4, item 5);
  - the registry keeps a per-market override for a domestic-only market.
- **ADR-027 (proposed), Q6 and Q16:**
  - a verified deal may order sellers within a product only once the false-savings rate has been measured for that lane, with the `savings.v1` ranking amendment and `DEAL_LANE_RANKING_ENABLED` on;
  - between lanes, a completable offer ranks first.
- **ADR-028 (proposed), D1:**
  - when a product has a completable offer, click-out offers for that product are not recommended;
  - a completable offer ranks above every click-out, even at a higher price;
  - among completable offers, ADR-027 Q6 orders them;
  - Phase C switches this on per market after the completion success rate is measured.
- **ADR-033 (accepted 2026-11-26, with Amendment 1):**
  - only admitted sellers are offered;
  - the admission tier stays internal;
  - an agent is never told which seller was left out or why;
  - offers of the `unverified` tier are counted only, never served.
- **ADR-007 amendment 1 (proposed, same pull request):** the citation index is a mode of the same catalog.

## Context

Under the owner's direction of 2026-11-24, the frontend agent talks to the shopper and recommends products, and Pivota supplies the offers for a product the agent has chosen. The scope document behind that direction is still a draft. The partners integrating now call `get_offers`, so which offer comes first, and why, is the recommendation Pivota makes most often.

### What is built (origin/main, 2026-11-24)

**There are two orders today, not one.**

- **The list order comes from the backend.** `offers.resolve` (`routes/agent_shop_gateway.py`, `_rank_offers_merit_first`, sort key at about `:4850-4856`) orders by three keys:
  - the fit tier, a bucket of match confidence: at least 0.95 is exact, at least 0.8 is product, anything lower is loose. It does not prove the size: two sizes can share one content key;
  - known unavailable last;
  - match confidence.

  The stable sort keeps the input order as the last tie-break: the catalog query's own order, then the order in which lanes were joined. Further passes follow:
  - offers needing live verification go last;
  - the buyer-currency filter;
  - in group mode only, fresh before stale;
  - one offer per host first, then the cut to the limit (about `:7562`). Without it, one seller's variants can fill the whole limit;
  - live-verified offers first;
  - in group mode only, stale last.
- **The gateway then reads merchants live, after the cut.** That read can mark an offer out of stock or stale. Flagged offers the read did not verify are withheld (`src/agentSignals/intelligenceReads.js`, about `:774-794`). The gateway does not re-sort the list.
- **The best offer is a separate pick in the gateway,** over the offers after that read. `offersToSignals` (`src/agentSignals/offerToSignal.js`, comparator at about `:417-424`) orders by:
  1. not known to be unavailable (unknown `in_stock` ranks with in stock);
  2. the product's primary seller (`is_primary`);
  3. stock confidence: `stock_verified` true, then unknown, then false;
  4. lowest price.

  When the fresh-price contract is on, only served offers with a comparable, fresh price, in the first eligible offer's currency, enter the pick. The contract is on when the backend sends `best_price_offer_id` or a price-comparability field, and the group-sibling mode that fills those is off by default. The tool description states this order (`mcp-server/src/commerceToolSurface.js:1459`).
- **The market enters `get_offers` only as a currency filter,** with the US assumed when no market is named. `get_offers` does not use domestic versus cross-border.
- **`get_pdp` does use it,** which makes a third best-offer rule. `services/offer_buyability.py` tags each offer domestic or cross-border from the offer's `market` field, which is almost always a US default. It then sets `is_buy_pick` to the cheapest in-stock domestic offer, falling back to cross-border.
- **A completable checkout is not used.** The backend says so (`agent_shop_gateway.py:4808`, "Checkout transport is not a ranking signal"). No stored completability fact exists yet.
- **ADR-026's retailer entity and domestic-only override are not built.**
- **No offer says why it is where it is.**
  - The closest fields are `metadata.best_price.source`, `best_price.reason` (`no_fresh_comparable_price`) and `rank_one_unverified`.
  - `metadata.reason` explains an empty answer only.
  - `seller_trust` is always `null`.
- **Product search** orders by a match score. It is guarded by `tests/services/test_ranking_neutrality.py`, which forbids price, margin, commission and take rate as ranking features. This ADR does not change search ranking.
- **`recommend_products`** is declared in the gateway's canonical contract (`safety-kernel/src/protocol/canonicalContract.js:216-218`).
  - It is exposed on MCP, and the executor dispatches it (`canonicalExecutor.js:605`).
  - The UCP dialect neither lists nor accepts it: `catalog.recommend` there is an internal label.
  - Its MCP listing is not behind a switch. A call returns `disabled` unless `AURORA_BFF_RECOMMEND_PRODUCTS_AGENT_ENABLED` is on, and that switch's production state was not checked.
  - It answers for the US whatever the buyer's market.

## Owner decisions (2026-11-24)

*Owner decisions of 2026-11-24, to be confirmed by the owner when this ADR is merged.*

1. **A fact-based order for offers matters more** than a product recommendation.
2. **Reasons are fixed fields, and Pivota sets the list.**
3. **The model-driven recommendation lane gets no new work.** It comes off the partner tool list until a fact-based product ranking replaces it.

## Decision

### D1. One written order for a product's offers

One order governs both the list and the best offer, for `get_offers`. From P2, `get_pdp`'s buy pick follows it too, so one product has one best offer on every tool (Resolved question 5). The order is applied twice, with the same rules:

- **In the backend, before the cut to the limit,** so an offer the order puts first is not cut.
- **In the gateway, after its live read and withholding,** over the offers it serves. The gateway's result is the answer. It assigns `rank`, and its facts are the ones D2 reports.

**Filters, before ordering:**

- Only admitted sellers for the brand and the buyer's market, once ADR-033 (accepted 2026-11-26) is built.
- Only offers in the buyer market's currency (ADR-026, built).
  - In a market the registry marks domestic-only, only domestic offers (not built).
- One retailer counts as one seller (ADR-026, not built), and its domestic storefront is preferred.
- **One offer per seller host before the cut** (built), the first by this order. One seller's variants must not fill the limit.
- In the gateway, flagged offers the live read did not verify are withheld (built).

**The best-offer filter:**

- Never an offer known to be unavailable.
- Where the fresh-price contract is on, a fresh, comparable price as the contract defines it.
- Otherwise, a price.

**The order.** A lower rule decides only between offers that tie on every rule above it. Every rule gives each value a fixed place, including the value `unknown`.

| # | Rule (code) | Places, first to last |
|---|---|---|
| 0 | Match (`match`, built): the backend's fit tier | `exact`, `product`, `loose` |
| 1 | Not known to be unavailable (`available`, built) | `true` and `unknown` together, then `false` |
| 2 | Completable (`completable`, ADR-028 D1). An offer without a stored, current fact that a completion route works for that seller and market is `false` | `true`, then `false` |
| 3 | Domestic before cross-border (`domestic`, ADR-026) | `true`, then `false` and `unknown` together |
| 4 | Stock verified (`stock_verified`, built) | `true`, then `unknown`, then `false` |
| 5 | Lowest price in the buyer's currency (`price`) | lowest first, then offers with no price |
| 6 | Final tie-break (`tie_break`) | a stable hash of the offer id |

Notes on the rules:

- **Rule 0** groups by match confidence. It does not prove the size. Size comparability is the best-offer filter's job where a resolved group exists.
- **Rule 2** is on only in a market where ADR-028 Phase C has opened; until then it is skipped. When a product has a completable offer that passes the best-offer filter, a click-out offer is never its best offer. Click-outs stay in the list, after the completable offers, so the agent can see them (Resolved question 2).
- **Rule 3** is skipped while offers carry no ships-from fact. A cross-border offer carries "ships from X; duties may apply".
- **Rule 5** uses the served price. The verified effective price replaces it only on a lane where ADR-027 Q6's gate has opened, with the `savings.v1` ranking amendment in force and `DEAL_LANE_RANKING_ENABLED` on.
- **Rule 6** uses a hash, not the id itself, because ids carry a lane prefix and sorting them would prefer one lane.

The primary seller is not in the order (Resolved question 1).

**What never enters the order:**

- what Pivota earns, or could earn, on a seller, including as a tie-break (ADR-025 Q4);
- whether an offer carries our attribution, or which completion route would carry it (ADR-028 D1, D2);
- the seller's admission tier (ADR-033);
- any payment for placement. No decision allows a promoted offer, and one would need a new decision.

### D2. Every offer returns its reasons as fixed fields

Each offer in a `get_offers` answer gains the fields below. They are added beside today's fields, and none is removed. The response's existing top-level `best_offer` object is unchanged.

- **`rank`:** its place in the gateway's order, 1 being first in the list, with no gaps.
- **`is_best_offer`:** `true` on the one offer picked as best, otherwise `false`.
- **`rank_facts`:** one entry for each rule of D1 from 0 to 5, in D1's order. These are the facts the gateway's order was computed on. Each entry is `{ rule, status, value, source, observed_at }`:
  - `rule` is one of the codes in D1;
  - `status` is one of:
    - `applied`: Pivota holds the fact and the rule is on;
    - `unknown`: the rule is on but Pivota does not hold the fact. Only rules 1, 3, 4 and 5 can be unknown;
    - `skipped`: the rule is not on in this market;
  - `value` is the fact, with the type below. It is `null` unless `status` is `applied`;
  - `source` is where the fact came from, from a closed list. It is `null` unless `status` is `applied`. The list:
    - `live_read`: the store's endpoint, at request time;
    - `cart`: a cart read at the store;
    - `stored`: a stored offer;
    - `probe`: a completion probe or a completed order;
    - `registry`: the market registry;
    - `match`: the backend's match;
  - `observed_at` is when the fact was read. It is `null` unless `status` is `applied`.
- **`decided_by`:** the code of the first rule on which this offer and the offer ranked just above it fall in different places, by D1's table. For rank 1, the comparison is with rank 2. A skipped rule puts every offer in the same place. When no rule separates the two, the value is `tie_break`.

**Value types (version 1):**

| Code | `value` when applied |
|---|---|
| `match` | `exact`, `product` or `loose` |
| `available` | `true` or `false` |
| `completable` | `true` or `false`. Never the route |
| `domestic` | `true`, or `false` with `ships_from` beside it |
| `stock_verified` | `true` or `false` |
| `price` | `{ amount, currency, basis }`, where `basis` is `served` or `effective_verified` |
| `tie_break` | used only as a value of `decided_by` |

**Versions.**

- The response names the list's version in `rank_rules_version`. Version 1 is the order of D1.
- The order built today is not version 1. P1 below emits it as version `0`, with its own codes.

**The evidence block.**

- The same facts fill the offer's ADR-002 evidence block: `evidence.sources` lists the sources used, and is never empty when an order was applied.
- `evidence.grade` and `evidence.method` are not changed by this ADR.
- `seller_trust` is never filled from the admission tier.

**Pivota sets the list.** The codes, their places and their value types are this ADR's. A code is added, removed or reordered only by an amendment the owner accepts. Partners and agents read the reasons; they cannot send their own order or weights.

**What a reason never says:**

- why a seller is not offered, or that one was left out (ADR-033);
- a seller's admission tier, directly or through a stand-in for it;
- which completion route an offer would use;
- anything about what Pivota earns. A `price` with basis `effective_verified` states the buyer's cost, and it ranks only within ADR-027 Q6's limits.

**Counts.** The existing counts keep their meaning:

- `withheld_other_currency_count`;
- `withheld_unverified_count`, which counts stale offers the live read did not verify. It has nothing to do with ADR-033's `unverified` tier.

ADR-033's admission counts are never emitted to agents.

### D3. Product rankings use the same shape, later

When a fact-based product ranking is designed, it returns its reasons in the same shape (`rank`, `rank_facts`, `decided_by`), with its own codes set in its own ADR. Until then, product search returns no reasons, and the neutrality test stays as it is.

### D4. The model-driven recommendation lane

- `recommend_products` gets no new work.
- **It comes off every partner door** until a fact-based product ranking replaces it:
  - it is removed from the MCP tool list;
  - the executor refuses it as an unknown tool on every door that uses the canonical contract.
- **Why:**
  - it answers for the US whatever the market, which ADR-026 forbids;
  - its reasons are model text, which D3's shape replaces once a product ranking exists.
- **The switch.** If `AURORA_BFF_RECOMMEND_PRODUCTS_AGENT_ENABLED` is on in production, turning it off is also a production change.
- **How it ships.** Each change goes through the release controller, with the owner's go for that change.
- **The code stays on main** until the replacement ships, so the first-party advisor that shares the engine is untouched.

## Phases

- **P1, reasons for today's order (version `0`).**
  - **The backend** emits each offer's sort keys with `offers.resolve`, behind a backend switch:
    - `fit_tier`;
    - `known_unavailable`;
    - `match_confidence`;
    - `needs_live_verification`;
    - `host_first`;
    - `live_verified`;
    - `stale`, in group mode only;
    - `input_order`, the stable-sort fallback.
  - **The gateway** adds `rank`, `is_best_offer`, `rank_facts`, `decided_by` and `rank_rules_version` behind a gateway switch, off by default. For the best-offer pick it uses the version-0 codes `available`, `primary_seller`, `stock_verified` and `price`.
  - `primary_seller` appears in version `0` only, because it is already emitted as `is_primary` today.
  - The two switches turn on backend first.
- **P2, the written order (version 1).**
  - A backend switch moves `offers.resolve` to D1, and a gateway switch moves the gateway's order and pick to D1.
  - The backend switch goes on first. Both repositories deploy separately, and the gateway never deploys on merge.
  - Rules 2 and 3 start as `skipped`. Rule 3 turns on once offers carry a ships-from fact, and rule 2 turns on per market at ADR-028 Phase C.
  - `get_pdp`'s buy pick (`services/offer_buyability.py`) moves to D1 behind the same backend switch (Resolved question 5).
  - Version 0's facts make each change visible before and after.
- **P3, the recommendation lane off every door** (D4). It is independent of P1 and P2.

## Consequences

- An agent can tell a shopper why an offer is first, from facts with their source and time. It can override the order knowing what it overrides.
- The order becomes a contract. Changing it is an amendment, not a code change, and partners can see the version.
- Two rules, completable and domestic, are written before their data exists. Until it does, they report `skipped` rather than pretend.
- The gateway's best-offer comparator is replaced by D1, applied over the offers it serves.

## Alternatives considered

- **Accept ADR-027 and ADR-028 in full.** Not taken. They also decide deal sources, code verification, program permissions, completion routes and card handling, and much of that has changed since through ADR-030 (proposed). Only their ordering rules are needed here (Resolved question 4).
- **Free-text reasons from a model.** Rejected: they cannot be checked, compared or versioned, and ADR-002 requires evidence and source.
- **Let the agent send weights.** Rejected: the order is Pivota's recommendation. An agent that wants another order already has every fact it needs to apply its own.

## Resolved questions (the owner, 2026-11-24)

*Owner answers of 2026-11-24, to be confirmed by the owner when this ADR is merged. Each one took the recommendation the question gave.*

1. **The primary seller.**
   - Today it is the best-offer pick's second rule, above stock confidence and price. No ADR decides it, and it is not a fact about the buyer's outcome.
   - The product's primary merchant is often the brand's own store, so a `primary_seller` reason would stand in for ADR-033's `brand_own` tier.
   - It would also change when an exclusion removes the primary row, which would reveal the exclusion.
   - **Answer:** it is dropped from the order and from the reasons. `is_primary` stays where it is emitted today; removing it is a separate question.
2. **Click-outs when a completable offer exists.** ADR-028 D1 says click-out offers for such a product "are not recommended". **Answer:**
   - they are never the best offer while a completable offer passes the best-offer filter;
   - they stay in the list after the completable offers, with their reasons, so the agent sees every admitted seller.
3. **Where availability and domestic sit.**
   - ADR-026 (accepted) puts domestic right after currency, "then ranks as today". Read literally, that is above availability.
   - ADR-028 D1 puts a completable offer above every click-out, and orders completable offers by price only.
   - **Answer:** match, then available, then completable, then domestic, then stock, then price, as D1 writes it.
     - An out-of-stock offer is never shown above an in-stock one, whether it is completable or domestic.
     - Domestic and stock still apply among completable offers.
     - Accepting this ADR amends ADR-026 Decision 5 and ADR-028 D1 on these points. Each gets a note pointing here when this ADR is accepted.
4. **What acceptance binds.** **Answer:** accepting this ADR makes ADR-028 D1's serving rules and ADR-027 Q6 and Q16 binding for this order only, as D1 adapts them. The rest of ADR-027 and ADR-028 stays proposed.
5. **`get_pdp`'s buy pick.** It is a third best-offer rule: the cheapest in-stock domestic offer. **Answer:** in P2 it follows D1, so one product has one best offer on every tool.
