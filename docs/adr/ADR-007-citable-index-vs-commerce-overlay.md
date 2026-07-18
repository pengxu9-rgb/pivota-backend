# ADR-007: Citable Knowledge Index vs Commerce/Offers Overlay

**Status:** **Accepted** (2026-06-24 — both founder questions resolved, see "Founder decisions" below; slices 1–3 merged to `main`) · adversarial review 2026-06-23 → GO-WITH-CHANGES, corrections folded into Action Items · **Date:** 2026-06-23 · **Scope:** `pivota-backend` index/serving pipeline + agent read surfaces · **Supersedes:** the offer-minting approach in the verify-to-serve spec (`pivota-merchants-portal#117`)
**Deciders:** founder (positioning) + backend (index pipeline)
**Part of:** the agent-protocol interoperability model — **ADR-014** (meta). ADR-007 is its **discovery / read-layer** instance: the single unified read surface across MCP / ACP / UCP / API-key agents (no protocol adds its own discovery endpoints).

---

## Founder decisions (2026-06-24) — these move the ADR to Accepted

Two questions gated acceptance. Both now resolved by the founder:

1. **Is offer-free citation the external end-state?** → **YES.** Frontier models and external agents *should* be able to call the Pivota commerce index for **citation**, with no offer required. Offer-free `index_eligible` is the durable external read surface — not a transitional state. This confirms the Decision below and greenlights the [external read/citation API contract](https://github.com/pengxu9-rgb/pivota-merchants-portal/blob/main/docs/external-citation-api-contract.md).
2. **Does Pivota Agent stay offer-gated while external read opens?** → **YES.** Pivota Agent (first-party shopping/checkout UX) **keeps** its offer/seed gate; the external citation read opens independently. The two are different callers on different lanes — the first-party curation requirement must never leak into the index gate (it doesn't: slice 3 threads `strict_serving_mode` only for explicit commerce surfaces).

Consequence: the "MAY keep its offer/seed gate" hedge in the Decision is now a firm **DOES**. The asymmetry is intentional and permanent — open index for citation, curated gate for first-party transact.

## Context

Pivota's thesis (founder-confirmed): the **neutral commerce INDEX + DECISION LAYER** that frontier agents read to **DECIDE & ACT**, where the moat is **answer-completeness + trust** — cite *across* everything. "Equality runs through all brands; segment index *density* for answer-completeness."

But the code today **gates agent visibility on commerce**:

- A product is agent-visible only if `index_pipeline_state.serving_eligible = TRUE`, and that gate **requires `has_price`** — a `catalog_offers` row with `list_price > 0` (`services/index_pipeline_state_service.py:279` `not has_price → blocker "no_price"`; the `serving_eligible` formula `:307–314` includes `and has_price`; the `has_price` subquery `:454–459`).
- The canonical recall path INNER-JOINs both SKUs and offers (`services/pivot_query_service.py:844` `JOIN catalog_skus`, `:924` `JOIN catalog_offers o … AND o.suppressed_at IS NULL`).
- The **only** offer-free agent surface is `external_product_seeds` (carries a `destination_url`, and only appears as a *thin-results fallback*).

**Consequence:** the citation/read surface is coupled to commerce — a product is only citable if it is *buyable*. This shrinks the index to transactable products, the opposite of answer-completeness.

Two different consumption modes are conflated onto one set of gates:
1. **Pivota Agent (first-party shopping app)** — discover *and* transact. Requiring an offer/seed per result is reasonable here.
2. **External / frontier agents** (ChatGPT, Claude, Gemini, Perplexity, custom) calling Pivota to **get product truth and cite it** — the **DECIDE** half. This needs content + substantiated claims + brand identity (+ an optional destination), **not** a buyable offer.

## The distinction that drives the decision (citation ≠ commerce)

- A **commerce offer** (`catalog_offers` / PSP-buyable) is *transaction* machinery.
- A **link-out destination** (a URL to the brand) is *content*, not commerce (cf. `external_product_seeds.destination_url`).
- An agent **citing** a product needs content (+ optional destination), not a buyable offer. Citation is the distribution we want **maximal and free**; commerce is a monetized overlay on a subset.

---

## Decision

Split eligibility into two **orthogonal** concepts:

- **`index_eligible` / `citation_eligible`** (NEW) — gated on **trust + quality + identity-resolved**, **offer-independent**. Drives the external/frontier **read + cite** surface (`get_pdp` / `agent_pdp_view` via MCP/UCP/ACP). A product with **zero offers is citable**.
- **`transact_eligible`** (RECAST of today's `serving_eligible`) — has a **buyable offer** (`has_price` + PSP-executable). Drives Pivota Agent **shopping + checkout**.

An indexed product has **0..N offers**; zero offers = **citable but not buyable**. The external read API is **un-gated by `has_price`** (gated on `index_eligible`). Pivota Agent (first-party) **keeps** its offer/seed gate for a curated shopping UX (founder-confirmed 2026-06-24) — that requirement must not leak into the index.

**Ranking is intent-aware**, not gate-based: shopping intent → prefer `transact_eligible`; recommend/inform intent → `index_eligible`, with offer presence a *ranking signal*, not a hard filter.

**verify-to-serve becomes a special case**: domain-verify → set `index_eligible` (no minted offer). This **supersedes** the `brand_direct` referral-offer approach in `#117`.

---

## Options Considered

### Option A: Decouple index from commerce (this ADR)
| Dimension | Assessment |
|---|---|
| Complexity | High — touches `index_pipeline_state` + every agent read gate |
| Strategic fit | Highest — directly serves the answer-completeness moat |
| Reversibility | Medium — flag-gated; recast must preserve `transact_eligible` exactly |

**Pros:** matches the thesis; index grows to maximal coverage; store-less / brand-authored / audit-seeded products become citable without commerce; no fake offers polluting `catalog_offers`; external agents read product truth directly (distribution = moat).
**Cons:** two eligibility concepts to maintain; ranking must become intent-aware; the external read contract becomes a first-class, security-relevant API.

### Option B: Mint a referral offer per product (the `#117` approach)
| Dimension | Assessment |
|---|---|
| Complexity | Low — reuses existing gates |
| Strategic fit | Low — leaves the structural coupling |
| Reversibility | Hard — `catalog_offers` gets polluted with non-commerce rows |

**Pros:** surgical; products are first-class in canonical recall immediately.
**Cons:** **binds citation to a commerce artifact** (the exact coupling we're removing); conflates "indexed" with "has an offer"; doesn't fix the answer-completeness ceiling.

### Option C: Reuse `external_product_seeds` for offer-free content
| Dimension | Assessment |
|---|---|
| Complexity | Low/Med — existing offer-free path |
| Strategic fit | Partial — offer-free, but second-class |
| Reversibility | Medium |

**Pros:** already offer-free with a destination; no serving-gate change.
**Cons:** seeds only surface as a *thin-results fallback* (not first-class discovery); splits representation (`external_product_seeds` vs `catalog_products`); the destination behaves like a quasi-offer.

## Trade-off analysis

Only **A** decouples the index from commerce at the architecture level; **B** and **C** are workarounds that leave the coupling in place. A's cost is concentrated in the `index_pipeline_state` gate and the read paths' `WHERE`/`JOIN` clauses; scoping the change behind a flag and preserving `transact_eligible` byte-for-byte keeps Pivota Agent shopping + checkout unchanged. The mis-merge and neutrality invariants are orthogonal and carry over unchanged. We accept the maintenance of a second eligibility concept and intent-aware ranking as the price of the moat.

## Consequences

- **Easier:** the index moves toward answer-completeness; store-less/brand-authored/audit-seeded products become citable with no commerce; frontier agents can read product truth directly (the distribution flywheel).
- **Harder:** two eligibility concepts; ranking must classify intent; the external read/citation API becomes a first-class surface to spec + secure (fields exposed, citation attribution, neutrality, rate limits).
- **Revisit:** the meaning of `serving_eligible` across the codebase (recast carefully — many readers); the canonical recall query (the offer INNER JOIN must become conditional on intent/eligibility); the first-party-vs-external read split.

## Invariants preserved (non-negotiable)

- **Checkout fail-closed:** no PSP-executable offer → not buyable. Decoupling visibility must never make a no-offer product transactable.
- **No-GTIN mis-merge gate:** GTIN-or-resolved before cross-merchant capture; brand-authored stays isolated (`pdp_scope` not `multi_merchant_canonical`).
- **Neutrality firewall:** merit ranking only; offer presence is a ranking *signal*, never take-rate (the P0.3 firewall + invariance test still apply).

## Action Items

> **Revised after adversarial review (2026-06-23) — GO-WITH-CHANGES.** Strictly **additive** (no rename); sliced so shopping/checkout are never touched. See "Review corrections" below.

**Slice 1 (safe, additive):** — **MERGED ([#1010](https://github.com/pengxu9-rgb/pivota-backend/pull/1010))**
1. [x] ADD `index_pipeline_state.index_eligible BOOLEAN NOT NULL DEFAULT FALSE` (+ partial index, migration `165`). Computed in `_classify_product` as the full `serving_eligible` predicate **minus `has_price`** (with `has_image` REQUIRED, from raw predicates not the short-circuited `blocker_code`). Persisted in the upsert, `recompute_serving_eligibility`, the nightly job, and **cleared in the stale-invalidation / regression demotion UPDATEs** (M6). `serving_eligible` untouched.
2. [x] Behind flag `INDEX_ELIGIBLE_READ`, widened the **three read gates** to `serving_eligible OR index_eligible`: `routes/agent_pdp_v1.py` (get_pdp), `services/catalog_trust_policy.py`, `routes/pivota_canonical_routes.py` by-signature PDP. Public **sitemap** held behind its own `INDEX_ELIGIBLE_SITEMAP` flag.
3. [x] verify-to-serve lands brand-authored products as `index_eligible` with **no minted offer** — shipped as slice 2 below (`graduate_brand_authored_products`), superseding the `#117` referral offer.

**Slice 2 — verify-to-serve → `index_eligible`:** — **MERGED ([#1013](https://github.com/pengxu9-rgb/pivota-backend/pull/1013))**
   `graduate_brand_authored_products(merchant_id)` on domain-verify sets `pdp_scope='merchant_owned'` (neutral, never `multi_merchant_canonical`), refreshes `agent_pdp_view`, recomputes eligibility. No `catalog_skus`/`catalog_offers` minted → `transact_eligible` stays false (checkout fail-closed by construction).

**Slice 3 — citable RECALL:** — **MERGED ([#1016](https://github.com/pengxu9-rgb/pivota-backend/pull/1016))**
4. [x] Citable RECALL as a **separate no-OfferNode lane** (`_fetch_citable_canonical_rows` / `_build_citable_items`) — never LEFT-JOINs `catalog_offers` (`pivot_query_service.py:924` INNER JOIN untouched). Citable rows carry `buyable=false`, gated on `INDEX_ELIGIBLE_RECALL` AND `not strict_serving_mode`.

**Remaining:**
5. [~] Intent classification (shopping vs inform) — **partial**: slice 3 threads `strict_serving_mode` (suppresses citable rows for explicit commerce surfaces) and the neutrality invariance test was extended to the **citable-vs-buyable** axis. A *sharper* shopping/inform classifier is future work.
6. [x] External read/citation API contract — **spec'd**: [external-citation-api-contract.md](https://github.com/pengxu9-rgb/pivota-merchants-portal/blob/main/docs/external-citation-api-contract.md) (REST citation routes + future MCP read door; public-read + rate-limit/abuse; attribution; neutrality). Build is P0–P3 in that doc; the MCP/UCP/ACP product-read door remains unbuilt (the "via MCP/UCP/ACP" framing stays aspirational until P2).

**Gates that MUST stay offer-required for checkout (untouched):** `pivot_query_service.py:924` recall offer INNER JOIN; the downstream Agent API order/payment offer/quote resolution; `serving_eligible`'s `and has_price`.

## Review corrections (2026-06-23 — adversarial architecture review, GO-WITH-CHANGES)
- **M1 Additive, not a rename.** `serving_eligible` spans 24 non-test files (raw SQL, SQLAlchemy core, upsert, nightly UPDATE, `catalog_trust_policy`, public sitemap). Keep it (= transact semantics); add `index_eligible`.
- **M2 Recall ≠ a `serving_eligible` read** — it gates on the `catalog_offers` INNER JOIN. LEFT-joining it injects null-priced rows = un-buyable appears buyable → separate no-OfferNode lane, later.
- **M3** The three public-read gates move together (get_pdp / trust-policy / canonical-PDP); sitemap behind its own flag.
- **M4** `index_eligible` = `serving_eligible` − `has_price`, from the **full** predicate set (today `no_price` short-circuits before description/identity). Decide `has_image` requirement explicitly.
- **M5** Don't collapse "un-gate" into the global `AGENT_PDP_V1_BYPASS_*` (no quality floor).
- **M6** Lifecycle: the nightly stale-invalidation + regression demotion must also clear `index_eligible` (else delisted products stay citable).
- **Foundation (why it's cheap):** `agent_pdp_view` rows are written for offer-less products already (read-time gate) — no assembly change.
- **Invariants confirmed preserved:** checkout fail-closed (downstream Agent API); no-GTIN mis-merge (`index_eligible` requires identity-resolved); neutrality (extend invariance test to citable-vs-buyable).

*Design-only. No code in this ADR.*
