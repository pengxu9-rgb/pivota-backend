# ADR-018: Connection Layer — every crawled merchant is real; the layer says HOW a transaction executes, not WHETHER

**Status:** Proposed
**Date:** 2026-07-27
**Deciders:** Founder (peng) — policy; Commerce / Serving owners
**Supersedes (a position, not an ADR):** "the ACP feed is empty by design until a real merchant connects." That position defined *real merchant* as *connected via product sync*. The founder rejects that definition.
**Relationship:** applies **ADR-001** (Pivota owns the canonical record; merchants supply offers) to the *transactability* axis; consumes **ADR-016** (non-custodial) unchanged — nothing here puts Pivota in the fund flow; extends the settlement-rails vocabulary introduced in `services/platform_capabilities.get_platform_settlement_rails`. Companion to **ADR-007** (citable index) — this ADR is about the *commerce* projection of the same rows.
**Scope:** how the three connection layers are represented, how they are expressed on ACP / UCP / MCP, and what the ACP product feed selects. **Out of scope:** opening the ACP checkout doors (`AGENT_CHECKOUT_ACP_REST_ENABLED` + `AGENT_CHECKOUT_STRICT`, and the `ACP_SIGNING_SECRET` the adapter build requires) — this ADR is about feed CONTENT.

---

## Context

### The policy

Founder, 2026-07-27:

> All of our crawled merchants and products should be considered as real merchants. We just have different layers of connections: 1) crawled, and transactable. 2) product synced, and transactable. 3) product synced, and PSP integrated, and transactable.

| Layer | Connection | Transactable |
|---|---|---|
| **1** | crawled | yes |
| **2** | product synced | yes |
| **3** | product synced + PSP integrated | yes |

All three are transactable; the layer describes **how**, not **whether**.

### What was actually wrong

The public ACP feed (`GET /acp/feed` — mounted by `AGENT_CHECKOUT_STRICT` + `AGENT_CHECKOUT_ACP_FEED_ENABLED`, served unsigned by `ACP_PUBLIC_FEED`, checkout doors dark) returns `{"version":"2026-04-17","count":0,"products":[]}` — verified live 2026-07-27. Its source is
`invokeCommerceKernelRawUpstream('find_products', {})` → backend `_handle_find_products_multi` → the **connected_catalog** lane (live catalogs of connected Shopify stores), gated only by `_is_product_sellable`. Every connected store is a test rig, so `testMerchantPolicy` correctly empties it.

Two separate errors were folded into one story:

1. **A category error.** We concluded "the feed is empty until a real merchant connects." Wrong on the founder's model: 12,542 crawled products (`catalog_track='external_referral'`) from 53 sellers are real. The feed is empty because it is pointed at the *smallest, emptiest* lane.
2. **A different, real constraint that was never the one we cited.** Shopping ingesters (ChatGPT / Google) reject price-less items. Until PIVOTA-Agent **#1824** (2026-07-24) every serving-eligible lane emitted `price: null` — `get_product_entity_index_feed` hardcoded `null` price/currency/availability and `canonicalCatalogSearch`'s `includeSkuOffers:false` branch selected `NULL::` placeholders. **Price, not merchant realness, is why the serving catalog was never pointed at the feed.** #1824 fixed both lanes; the constraint is gone and nothing re-measured after it.

### Measured ground truth (prod, 2026-07-27)

Read via the Railway public proxy against `catalog_products` / `catalog_offers` / `index_pipeline_state` / `merchant_stores` / `merchant_onboarding`.

**Corpus by lane**

| Lane column | Value | Products | Distinct merchant_ids |
|---|---|---:|---:|
| `catalog_track` | `external_referral` | 12,542 | 53 |
| `catalog_track` | `internal_merchant` | 1,562 | 6 |
| `platform` | `external_seed` | 12,514 | — |
| `platform` | `shopify` | 1,541 | — |
| `platform` | `wix` / `url_audit` / `brand_authored` | 20 / 28 / 1 | — |
| `source_system` | `external_product_seeds_mirror_v1` | 10,339 | — |
| `source_system` | `catalog_enrichment_agent_v1` | 2,175 | — |
| `source_system` | `shopify_products_sync` | 1,537 | — |
| `source_system` | `universal_product_sync` | 20 | — |
| `source_system` | `ownist_test_fixture_v1` | 4 | — |

**The whole `internal_merchant` track, by merchant**

| merchant_id | platform | source_system | rows | real? |
|---|---|---|---:|---|
| `merch_bbd34645bc1950cc` | shopify | shopify_products_sync | 760 | rig (pivota-review-demo) |
| `merch_efbc46b4619cfbdf` | shopify | shopify_products_sync | 743 | rig (founder test store) |
| `merch_efbc46b4619cfbdf` | wix | universal_product_sync | 20 | rig |
| `merch_shopify_00d4a720d67d96c5dcba` | shopify | shopify_products_sync | 17 | rig |
| `merch_shopify_0584b37f7a8be00a5223` | shopify | shopify_products_sync | 17 | rig |
| `merch_test_ownist_001` | shopify | ownist_test_fixture_v1 | 4 | **rig, NOT in `testMerchantPolicy`** |
| `merch_924da2be8503e5f7` | brand_authored | *(null)* | 1 | real, but not a sync |

**Connected stores** — 5 rows are `status='active'`: `pivota-review-demo.myshopify.com` (under **two** merchant_ids, confirming the domain-prefix leg is load-bearing), `pivota-review-demo-2`, `92sfrj-bi.myshopify.com` (founder rig, 743 products), and `r4ee11-ku.myshopify.com` with **0 products**.

**PSP** — `merchant_onboarding.psp_connected IS TRUE` for exactly **3** of 54 rows: `chydanlab` (status `deleted`), `testlab`, and `merch_efbc46b4619cfbdf` (the founder rig).

**Serving eligibility and price** — `index_pipeline_state`: 11,285 rows, `serving_eligible` **4,782**, `index_eligible` 4,881. Of the serving-eligible rows, `has_price IS TRUE` for **4,782 — 100%; zero price-less**. `has_price` is already a *component* of serving eligibility, not an independent filter.

**Offers** — 21,728 rows, 18,809 unsuppressed, 21,278 priced, and **0 with a NULL currency**. Suppression reasons include `source_currency_or_channel_defect` (**466**) — the existing currency containment — and `demo_retired_2026_07` (2,431).

**The census, mapped onto the founder's layers**

| Layer | Definition | Products | Serving-eligible + priced content_keys | Real (non-rig) |
|---|---|---:|---:|---|
| **1 — crawled** | `catalog_track='external_referral'` | 12,542 | **4,467** | 53 sellers |
| **2 — product synced** | `catalog_track='internal_merchant'` ∧ active store | 1,557 | **0** | **0** |

> **Bucketing note — this table is CUMULATIVE, the census below is EXCLUSIVE.** Layer 2 here (1,557) counts every synced row, *including* the 763 that also satisfy layer 3. The exclusive layer-2 figure is `1,557 − 763 = 794`, which is what the resolved-ruling census reports. Both are correct; they answer different questions, and a reader going top-to-bottom would otherwise see a contradiction.
| **3 — synced + PSP** | layer 2 ∧ `psp_connected` | 763 | **0** | **0** |

(Layer 2's 1,557 = the 1,562 `internal_merchant` rows minus the 1 brand-authored row that was never synced and the 4 `ownist_test_fixture_v1` rows. Layer 3's 763 = every row of the one `psp_connected` merchant that has any, `merch_efbc46b4619cfbdf`: 743 Shopify + 20 Wix. Both counts are rigs end to end, which is why the *real* column is 0.)

Executing the index-feed lane's exact predicate against prod (serving_eligible ∧ `sig_` signature ∧ `activeCatalogProductSourceWhere` ∧ a priced, currency-bearing, unsuppressed offer) yields **4,467 content_keys** — up from the 0 the feed serves today, and **100% Layer 1**.

---

## The three findings the layer model does not absorb

These are stated first because they change what the design can honestly claim.

### F1 — Layers 2 and 3 have zero real population. Not "few". Zero.

Every non-rig product in the corpus is Layer 1. The entire `internal_merchant` track is five test rigs plus one brand-authored row that was never synced. Every `status='active'` store is a rig or empty. All three `psp_connected` merchants are rigs or deleted.

So expressing the layer on outward surfaces is, **today, emitting a constant**. That is still worth doing — it fixes the contract before there is a second value, so the first real merchant sync does not change the feed's *shape* — but nobody should read a `connection_layer` field as informative until a real Layer 2 exists. This ADR ships the vocabulary, not a distribution.

### F2 — The layer number does not predict execution quality. The layers are a *supply* axis; execution is a separate, orthogonal axis.

The founder's model reads as a ladder: higher layer, better transaction. The code does not implement a ladder.

- **Layer 1 today** executes via the attributed redirect (`/r?token=…`), and for six allowlisted brands (`cosrx.com, beautyofjoseon.com, skin1004.com, anua.us, medicube.us, mixsoon.us`) it upgrades at click time to a **warm handoff** — a pre-built cart on the brand's own Shopify checkout (`services/outbound_warm_handoff.py`).
- **Layer 2** — synced but no PSP — executes via… the same attributed redirect. `_attach_connected_product_redirects` stamps a cart-permalink `/r` link on connected cards. Layer 2 is **not a distinct execution path in the code today**; it is a data-freshness and inventory-accuracy tier.
- **Layer 3** would execute via the Pivota-orchestrated ACP checkout — which is **dark** (`AGENT_CHECKOUT_ACP_REST_ENABLED` off, so `POST /acp/checkout_sessions` returns 404, verified live). Layer 3 is presently the *least* transactable of the three through Pivota.

And warm-handoff eligibility keys on **brand domain**, not on layer. A crawled Layer-1 COSRX product gets a strictly better execution path than a hypothetical Layer-2 product from a brand that is not allowlisted.

**Therefore this ADR does not advertise the layer as an execution promise.** It carries two orthogonal fields — a `connection_layer` (supply provenance) and an `execution_path` (what the agent actually gets) — and the second is the one an agent is allowed to act on. Collapsing them into one number would be exactly the execution-layer fallback the standing rule forbids: it would let a redirect-only item be read as one-click because its supply tier happened to be high.

### ✅ RESOLVED 2026-07-28 (founder) — "PSP integrated" means `psp_connected`, and only that

This ADR originally defined layer 3 as `psp_connected` **OR** a verified `pcs_merchant_capabilities.has_shopify_payments` fact, on the reasoning that both mean "a real settlement rail exists on this merchant". That was flagged when it shipped as *a choice, not a reading of the data*. The founder has now made the reading: **"PSP integrated" is `psp_connected` — the flag in the Pivota merchant portal.** The `has_shopify_payments` leg is dropped from the layer definition entirely.

The distinction is worth stating plainly, because conflating the two is exactly what made the OR tempting — they are facts about **different parties**:

| fact | means |
|---|---|
| `merchant_onboarding.psp_connected` | **Pivota** can orchestrate the charge |
| `pcs_merchant_capabilities.has_shopify_payments` | the **merchant's own** checkout can settle |

The three-layer model is about **Pivota's connection depth to the merchant**, so the portal flag is the right axis and the merchant's own checkout capability is not a layer input at all. It remains load-bearing in `get_platform_settlement_rails`, which answers the different and wider question of *which rails a transaction can pass through* — untouched by this.

**Census delta, measured rather than assumed: zero rows move.** Computing both definitions side by side over all 14,104 `catalog_products` rows, `CHANGED_BY_RULING = 0` for every layer. That is because `has_shopify_payments IS TRUE` for **0** merchants in prod. The reason is stronger than "nobody has been verified": **exactly one** merchant has been through the Shopify verify — `merch_efbc46b4619cfbdf`, the founder's test rig, checked 2026-01-19 — and it came back **FALSE**. That merchant is *already* layer 3 through `psp_connected = true`. So the OR arm had never once fired **and could not have**: the only row that could ever have supplied it is false, and its merchant reaches layer 3 by the other arm anyway. The layer census is byte-identical: layer 1 = 12,547 (12,543 non-rig), layer 2 = 794 (0 non-rig), layer 3 = 763 (0 non-rig).

### F3 — "Merchant" is two different entities in the schema, and the founder's sentence spans both.

`merchant_onboarding` holds parties who signed up (54 rows). Crawled sellers are not in it. Layer-1 products carry either the synthetic bucket id `merchant_id='external_seed'` (11,149 rows) or an ADR-009 **observed seller** `merch_obs_*` (45 distinct ids, 1,365 rows), plus 7 other ids over 28 rows.

So "we have 53 crawled merchants" and "we have 54 merchants" count different things, and any per-merchant layer join through `merchant_onboarding` **misses every crawled seller** — it returns NULL, which a naive `COALESCE(psp_connected,false)` reads as "not PSP", which is accidentally right for the wrong reason. The classifier below must therefore treat *absence from `merchant_onboarding`* as **Layer 1 by construction**, never as an unknown to be defaulted.

---

## Decision

**1. The connection layer is DERIVED, never stored.** No new column, no new table, and the parked `transaction_capable` ledger stays parked.

The layer is already fully determined by columns that exist:

```
layer 1  ⟸  catalog_track = 'external_referral'            (or merchant absent from merchant_onboarding)
layer 2  ⟸  catalog_track = 'internal_merchant'  ∧  an active merchant_stores row
layer 3  ⟸  layer 2  ∧  merchant_onboarding.psp_connected IS TRUE
```

Storing it would create a third thing to keep in sync with `catalog_track` and `merchant_stores.status`, both of which already move — and this repo has repeatedly been bitten by a stored derivative drifting from its source (`catalog_row_trust`, the minted/mirror trust split). Derivation costs one `CASE` and two `EXISTS` legs the serving queries already run for the source gate. **Prefer derivation; revisit only if a profile shows the `EXISTS` legs on the hot feed path.**

The parked `transaction_capable` ledger (Fix Plan A Phase 2) is explicitly **not** adopted here: it answers "may this merchant be served at all", which is `AGENT_CHECKOUT_CAPABILITY_GATE` / `transactionCapableMerchantWhere`'s question, and it is a *gate*. The connection layer is a *label*. Fusing a gate and a label is how the "psp_connected = may be served" mistake happened in the first place.

**2. The layer is expressed as TWO fields, never one.**

```
connection_layer  : 1 | 2 | 3            — supply provenance (how the row got here)
execution_path    : "attributed_redirect" | "warm_handoff" | "delegated_checkout" | "pivota_psp_checkout"
```

`execution_path` is resolved from live facts (warm-handoff allowlist membership, ACP door state, settlement rails), **not** from the layer. An agent that wants to know what it gets reads `execution_path`. `connection_layer` is provenance for ranking and disclosure.

Both are advertised honestly per [[feedback_no_execution_layer_fallbacks]]: a Layer-1 crawled item that transacts by redirect must not be advertised identically to a Layer-3 PSP item. Concretely, no surface may imply one-click for anything whose `execution_path` is `attributed_redirect` or `warm_handoff`.

**3. The ACP feed serves the priced serving lane, not the connected lane.**

- **Selects:** one row per `content_key`, `row_rank = 1`.
- **Gated on:** `index_pipeline_state.serving_eligible = TRUE` **and** `activeCatalogProductSourceWhere` (which carries `testMerchantPolicy`, both the merchant-id leg and the `pivota-review-demo%` `source_domain` leg) **and** `pivota_signature_id LIKE 'sig\_%'`.
- **Priced from:** ONE representative `catalog_offers` row via the existing no-fanout `LEFT JOIN LATERAL` — amount, currency and availability from the **same** offer row, cheapest in-market first, `currency IS NOT NULL`, `suppressed_at IS NULL`, `COALESCE(merchant_effective_price, list_price) > 0`. **Currency is never defaulted.**
- **Yields:** 4,467 content_keys today.

This is not new code. `src/services/productEntityIndexFeed.js::getProductEntityIndexFeed` already is exactly this lane, post-#1824. The change is to point `getProducts` at it. See the handoff.

**4. An item with no price is not emitted.** Price is the hard requirement. A row that survives every gate but has no currency-bearing priced offer is dropped from the feed rather than emitted with `price: null`. (Measured cost of this rule today: 0 rows — every serving-eligible row has `has_price`.)

**5. Nothing about the money path changes.** `AGENT_CHECKOUT_ACP_REST_ENABLED` and `ACP_SIGNING_SECRET` stay unset, and `POST /acp/checkout_sessions` continues to 404.

Getting these flag names right matters, because an operator acting on the wrong one verifies nothing. As implemented in `src/acpFeedFlags.js`:

| Flag | What it actually does |
|---|---|
| `AGENT_CHECKOUT_STRICT` | required for **any** `/acp` route to mount; without it everything 404s |
| `AGENT_CHECKOUT_ACP_REST_ENABLED` | mounts the **5 checkout endpoints**; also implies the feed |
| `AGENT_CHECKOUT_ACP_FEED_ENABLED` | mounts **`GET /acp/feed` only** — the decoupling that lets discovery ship without the money path |
| `ACP_PUBLIC_FEED` | serves the feed **without an HMAC signature**. It is not a mount gate. |
| `ACP_SIGNING_SECRET` | not part of the 404 gate at all — it is checked in the lazy adapter build, and its absence surfaces as **503 `MERCHANT_UNAVAILABLE`**, not 404 |

So the feed's mount gate is `AGENT_CHECKOUT_STRICT` + `AGENT_CHECKOUT_ACP_FEED_ENABLED`; `ACP_PUBLIC_FEED` only removes the signature requirement. Pivota remains a mid-man: the feed's `link` is a Pivota canonical PDP, `external_redirect_url` is the signed `/r` attribution link to the **merchant's own** destination, and settlement is the merchant's. **No lane in this design puts Pivota in the fund flow or makes it merchant-of-record** — `buildUcpProfile` already publishes `provider.merchant_of_record: false`, and that stays.

---

## What must NOT change

| Invariant | Why it is fragile |
|---|---|
| `testMerchantPolicy` exclusion, all **3 lockstep files** — `PIVOTA-Agent/src/services/testMerchantPolicy.js`, `pivota-backend/services/test_merchant_policy.py`, `pivota-backend/scripts/step5_working_set.py` | One demo domain appears under **two** merchant_ids (`merch_bbd34645bc1950cc` and `merch_shopify_00d4a720d67d96c5dcba` both on `pivota-review-demo.myshopify.com`), so id-enumeration alone is insufficient — the `source_domain` prefix leg is load-bearing. Confirmed still true in prod today. |
| The exclusion wraps the whole `OR` in `activeCatalogProductSourceWhere`, never as another branch of it | The `external_seed` branch admits on `merchant_id` alone; folding the exclusion into the `OR` would let a rig keep serving through it. |
| ACP checkout doors dark | This item is feed content. Opening the money path is a separate decision with its own review. |
| Pivota is never merchant-of-record | ADR-016. Nothing here holds funds. |
| Currency is never defaulted | See below. |

### 🚨 New gap found while measuring: `merch_test_ownist_001`

`merch_test_ownist_001` ("Ownist Test Merchant", `source_system='ownist_test_fixture_v1'`) has **4 `serving_eligible` products** and is **not** in `testMerchantPolicy` in any of the three files. It does not reach the feed today for one accidental reason: none of its 4 rows has a priced, currency-bearing offer, so the price gate drops them. That is luck, not policy — mint one offer, or relax the price gate, and 4 rig SKUs go to a public shopping ingester.

**Action (separate, small PR): add `merch_test_ownist_001` to all three lockstep files.** Deliberately not folded into this change so the exclusion lands on its own reviewable diff.

---

## Currency honesty

A wider feed widens the blast radius of the mispricing class. The design's position:

1. **The suppression gate is the containment, and the lane already honours it.** 466 offers carry `suppression_reason='source_currency_or_channel_defect'`; the lane's `o.suppressed_at IS NULL` predicate excludes them. Any row a currency audit condemns is removed from the feed by suppressing its offer — **no new mechanism, and no duplication of the currency work in flight.** This ADR *depends on* that work; it does not reimplement it.
2. **Currency is never defaulted.** `#1824`'s rule holds: amount and currency come from the same offer row, and an offer with no currency is not price-quotable and is skipped. Measured: 0 offers have a NULL currency, so this rule currently costs nothing and is pure insurance.
3. **The one bad price that was live** ($1.69 "Winona Soothing Repair Serum", description literally "Test fixture for PDP") belongs to `merch_efbc46b4619cfbdf` and is excluded by `testMerchantPolicy`, not by any currency rule.
4. **Residual risk, stated plainly.** Of the 5,774 representative best-offers behind the feed keys, **160 are non-USD** (EUR 57, GBP 44, JPY 31, AUD 18, HKD 7, KRW 3, CAD 1) — correctly labelled, but a US shopping ingester may still reject or mis-rank them. Worse, **2,497 USD-labelled representative offers carry no `source_domain`**, so their currency provenance cannot be audited at all (the known blind spot: 4,718 of 18,809 unsuppressed offers lack `source_domain`). The INR/ZAR class is exactly "correct-looking currency, wrong-provenance amount", and those 2,497 rows are where it would hide.

**Gate before any external ingester is pointed at the feed:** the feed must be restricted to `upper(market) = 'US'` **and** the currency-provenance audit must have run over the `source_domain`-less mirror offers. Until then the feed is publishable but should not be *submitted* to Google/ChatGPT merchant intake. The `market` restriction is a one-line predicate the gateway already binds (`bestOfferMarketParam`); the audit is the currency owner's item.

---

## Options Considered

### Option A: Derive the layer; point the feed at the existing priced serving lane (this ADR)
**Pros:** no schema change; the lane already exists and is already gated and priced (#1824); `testMerchantPolicy` is inherited through the shared source gate rather than re-implemented; two-field expression keeps the execution promise honest; 0 → 4,467 items.
**Cons:** the derivation runs two `EXISTS` legs per row on a hot path (mitigated: the source gate already runs them); the layer field is a constant today (F1), so it looks like ceremony until a real Layer 2 exists.

### Option B: Store a `connection_layer` column / revive the `transaction_capable` ledger
**Pros:** one read, no joins; a natural place to hang manual overrides.
**Cons:** a third derivative to keep in sync with `catalog_track` and `merchant_stores.status`, both of which move; this repo's recurring failure mode is exactly a stored derivative drifting from its source; and a ledger conflates a *gate* with a *label*. **Rejected** — revisit only under measured latency pressure.

### Option C: Keep the feed on the connected lane and wait for a real merchant to sync
**Pros:** zero work.
**Cons:** this is the superseded position. It defines away 12,542 real products, keeps a public protocol surface permanently empty, and mis-attributes the emptiness to merchant realness when the real historical cause was `price: null`. **Rejected by the founder's policy.**

### Option D: Single `transactability_tier` field collapsing layer and execution path
**Pros:** one number for an agent to sort on.
**Cons:** F2 — the two axes are not co-monotonic today (an allowlisted Layer-1 brand out-executes a non-allowlisted Layer-2 one), so a single tier would advertise a promise the execution path does not keep. **Rejected** under [[feedback_no_execution_layer_fallbacks]].

## Trade-off Analysis

The real trade is **contract honesty vs. field utility**. Option D is what an agent would most like to consume; it is also the one that lies. Option B is what a DBA would most like to query; it is the one that goes stale. Option A costs two joins and ships a field that is currently a constant — and is the only one whose every emitted value is true on the day it is emitted.

Decisive factor: **this is a public, externally-ingested surface. A field that is boring and true beats a field that is useful and occasionally wrong.**

---

## Consequences

**Good**
- The ACP feed goes 0 → 4,467 priced, non-rig, serving-eligible items without opening any money path.
- Layer 1 stops being second-class in our own vocabulary — it is where 100% of the real catalog lives.
- The feed's shape stops depending on how many merchants have synced, so the first real Layer-2 sync is a data change, not a contract change.

**Bad / accepted**
- `connection_layer` emits a constant until a real merchant connects (F1).
- Two fields where consumers will want one; some agents will ignore `execution_path` and assume one-click. Mitigation is documentation on the surface itself, not a collapsed field.
- The feed remains ungated on `market`, so 160 non-USD items would be emitted; **the market gate is a precondition of external submission, not of publication.**

**Risks**
- `merch_test_ownist_001` leaks if the price gate is ever relaxed before the lockstep files are updated.
- 2,497 representative offers have unauditable currency provenance.
- Pointing the feed at a 4,467-row lane changes its cost profile; the lane paginates (default 100, max 500) and the public feed already carries a 60 rpm token bucket and a 32 KiB body cap.

---

## Implementation

### Shipped with this ADR (pivota-backend)

`services/connection_layer.py` — the single named authority for the taxonomy:

- `classify_connection_layer(...)` — pure function over `(catalog_track, has_active_store, psp_connected, merchant_known)`; returns layer + slug. (`has_native_payments` remains in the signature for stability — existing callers pass it and the settlement-rails path still needs the fact — but since the 2026-07-28 ruling it is **not a layer input**.) Absence from `merchant_onboarding` is Layer 1 **by construction** (F3), never an unknown to be defaulted.
- `resolve_execution_paths(...)` — the orthogonal axis: warm-handoff allowlist membership and door state, resolved from live facts, not from the layer.
- `connection_layer_sql(...)` — the SQL twin of the classifier, for serving queries, kept in the same file as its Python twin so the two cannot drift silently.
- A caller alias equal to one of the expression's own internal subquery aliases (`ms_cl`, `mo_cl`, `mo_psp`, `pmc_cl`) would **shadow** the inner scope — `WHERE ms_cl.merchant_id = ms_cl.merchant_id` decorrelates the subquery into "does ANY live store exist anywhere" and silently returns a wrong layer, with no error and no injection. Those names are rejected by the alias validator rather than merely documented.
- `CONNECTION_LAYER_FIELD_ENABLED` — a default-**off** flag reserved for the gateway emission in rollout step 3. **Nothing consults it yet**, and it is stated plainly here rather than described as protecting a surface it does not: the only thing this change emits is the merchant-scoped `connection_layer_ceiling` below, which is unconditional. That is safe because those keys reach no HTTP body — `resolve_merchant_capability`'s single consumer (`services/tier2_acp_lane`) stores the dict on `AcpLaneDecision.capability`, and `routes/agent_checkout_intents` projects only `platform` / `reason` / `settlement_rail` out of it.

Wired additively into `services/merchant_capability_resolver.resolve_merchant_capability`, alongside `settlement_rails`, as **`connection_layer_ceiling`** (+ `_slug`). The `_ceiling` suffix is load-bearing: that resolver is merchant-scoped and has no `catalog_track`, so the only honest answer it can give is *the highest layer this merchant's own synced rows could reach* — a crawled row under the same merchant is still Layer 1. Present on **every** return path including the early ones, so no consumer learns to write `?? 3`.

Tests: `tests/test_connection_layer.py` (the classification matrix, incl. every NULL-bearing and rig-shaped input) and `tests/test_connection_layer_postgres.py` (executes the SQL twin inside a serving-shaped statement on **real Postgres**, per the `postgres-dialect-gate` convention — SQLite green is not evidence in this repo).

Two defects were found by writing the tests rather than by review, which is the point of the pairing:

1. **The twins disagreed on `catalog_track` normalisation, twice.** An `if track and track != TRACK_INTERNAL_MERCHANT` guard let an empty/NULL track fall through to Layer 2/3 while the SQL correctly returned 1; and the Python normalised with `.strip()` while the SQL had no `btrim`, so `' internal_merchant '` was Layer 2 in Python and Layer 1 in Postgres. Both fixed — and the first `btrim` fix was itself wrong in a way worth recording: **single-argument `btrim(x)` strips SPACES ONLY**, not tab/newline/CR, so tab-padded tracks still diverged. Worse, the fixture row added to prove tabs were covered **passed by coincidence** — the untrimmed value missed CASE arm 1 and fell through to arm 3, which returns the same value. A test that passes for the wrong reason is exactly what this gate exists to prevent, reproduced inside the gate. The trim character set is now spelled out (`btrim(x, E' \t\n\r\f\v')`) and a fixture row whose expected value is only reachable after trimming was added. `test_sql_twin_agrees_with_the_python_twin_row_for_row` is the standing guard and it is **not** vacuous: reverting either `btrim` change makes it fail, executed rather than assumed.
2. **The resolver's store check.** A first cut passed `has_active_store=bool(store)`. `get_merchant_active_stores`' legacy-MCP leg synthesises a store row whenever `mcp_platform` is set and stamps it `status = 'active' if mcp_connected else 'disconnected'` — appending it **either way** — so a merchant whose only connection is a *disconnected* legacy MCP link was labelled Layer 2 while the SQL twin (which sees no `merchant_stores` row at all) said Layer 1. The status is now checked against `LIVE_STORE_STATUSES`, which is `('active','connected')` — byte-identical to `merchant_store_service`'s canonical predicate, because a narrower set here would make this module disagree with every other read path about what "connected" means.
3. **`postgres-dialect-gate` has a latent inter-file order dependence.** Every `tests/test_*_postgres.py` runs against ONE database in ONE pytest process, and the gate files declare overlapping lightweight tables with `CREATE TABLE IF NOT EXISTS` and *different* column sets — so whichever module runs first fixes the shape for the whole job. Adding a third gate file with a plain `CREATE` turned `test_pdp_content_depth_postgres` red with `column "has_price" does not exist`. This file therefore builds shared tables **additively** (`ADD COLUMN IF NOT EXISTS`) and declares the union; all three orderings were executed and are green. **Any future gate file sharing a lightweight table must do the same.**

### Handoff — PIVOTA-Agent (gateway). NOT implemented here.

`src/server.js` is owned by another workstream this session. Precise call sites:

1. **`src/server.js`, `getCommerceAcpRestAdapter()`** — the `getProducts` closure (origin/main ~line 30902). Replace
   ```js
   const raw = await invokeCommerceKernelRawUpstream('find_products', query || {});
   ```
   with a call to `getProductEntityIndexFeed({ limit, cursor, market: 'US' })` from `./services/productEntityIndexFeed`. **Keep the `isTestMerchantId` runtime filter** that follows — it is defence-in-depth, and `getProductEntityIndexFeed` gates in SQL via `activeCatalogProductSourceWhere`, which is a different leg. Belt and braces both stay.
   Put the swap behind a new env flag (e.g. `ACP_FEED_SOURCE=index_feed`, default unset ⇒ today's behaviour) so the change deploys dark and is flipped by env, not by merge.

2. **`src/acpFeedItem.js`** — `buildAcpFeedItem` reads `o.price` / `o.currency`, which the index-feed item already provides (`price`, `currency`, `price_amount`, `price_currency`). Add the two new fields to the projection:
   ```js
   connection_layer: o.connection_layer,      // 1 | 2 | 3
   execution_path:   o.execution_path,        // see ADR-018 §Decision 2
   ```
   and **drop any item whose resolved price is null** rather than emitting `price: null` (Decision 4).
   Note on where the keys would otherwise be lost: **not** the sanitizer. `sanitizeResult` is a *denylist* and `connection_layer` matches nothing in it, so it passes straight through (`ATTRIBUTED_LINK_KEYS` only exempts values from URL redaction — non-membership is not a drop). The keys are dropped by `buildAcpFeedItem`'s fixed object literal, which is why they must be added to it explicitly. An implementer who patches the sanitizer instead will change nothing.

3. **`src/services/productEntityIndexFeed.js`** — emit the two fields. `connection_layer` derives from the row it already selects: `cp.catalog_track` is not currently in the `canonical_rows` projection and must be added. `execution_path` needs the warm-handoff brand allowlist, which the gateway already holds.

4. **UCP** (`safety-kernel/src/protocol/ucpProfile.js`) — `buildUcpProfile` advertises capabilities but says nothing about execution paths. Add a `provider.execution_paths` array enumerating the paths this endpoint can actually deliver, so a UCP platform is not left inferring one-click from the presence of a `checkout` capability while `AGENT_CHECKOUT_ACP_REST_ENABLED` is off. `provider.merchant_of_record: false` stays.

5. **MCP `get_product`** — verified live 2026-07-27: returns `price: {amount, currency}` but **no execution information at all** (no buy URL, no merchant, no path). An agent reading it cannot tell how to transact. Add `execution_path` and the attributed `/r` link to the structured content.

### Rollout

1. Merge this ADR + the backend classifier (flag default off). No behaviour change.
2. Add `merch_test_ownist_001` to the three lockstep files (separate PR).
3. Gateway PR 1–3 behind `ACP_FEED_SOURCE`, deployed dark.
4. Flip on staging; verify `GET /acp/feed` returns priced items, `count > 0`, **zero** items whose `merchant_id` is in `getTestMerchantIds()`, and zero `price: null`.
5. Flip in prod. Watch the 60 rpm limiter and feed latency.
6. Only then: the `market='US'` restriction + the currency-provenance audit, before any external ingester submission.

## Open Questions

- Should `execution_path` be a single value or an ordered list (an item can be both `warm_handoff`-eligible and `attributed_redirect`-capable)? Shipped as a list from the backend; the feed projection takes the best one. Revisit if consumers find the list unhelpful.
- Does a Layer-1 item whose brand is warm-handoff-allowlisted deserve a ranking boost in the feed? Deliberately not decided here — ranking is a separate concern from disclosure.
- What is the correct answer for a *crawled* row whose seller later syncs? Its `catalog_track` flips, and the layer flips with it — but the crawled canonical record and the synced offer coexist per ADR-001. The layer is therefore a property of the **row**, not of the **content_key**, and a content_key with members in two layers reports its best member's layer. Stated here; not yet exercised by any data.
