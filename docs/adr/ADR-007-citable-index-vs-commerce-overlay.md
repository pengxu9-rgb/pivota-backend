# ADR-007: Citable Knowledge Index vs Commerce/Offers Overlay

**Status:** Proposed · **Date:** 2026-06-23 · **Scope:** `pivota-backend` index/serving pipeline + agent read surfaces · **Supersedes:** the offer-minting approach in the verify-to-serve spec (`pivota-merchants-portal#117`)
**Deciders:** founder (positioning) + backend (index pipeline)

---

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

An indexed product has **0..N offers**; zero offers = **citable but not buyable**. The external read API is **un-gated by `has_price`** (gated on `index_eligible`). Pivota Agent (first-party) MAY keep its offer/seed gate for a curated shopping UX — that requirement must not leak into the index.

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

1. [ ] Recast `serving_eligible` → `transact_eligible`; add `index_eligible` (trust + quality + identity, offer-free) in `index_pipeline_state_service.py`. Flag-gated; `transact_eligible` semantics unchanged.
2. [ ] Make the canonical-recall offer-join conditional (intent/eligibility-aware) + add an offer-free citation read lane.
3. [ ] Spec + secure the **external read/citation API contract** (fields, attribution, neutrality, limits) over `get_pdp` / `agent_pdp_view` via the MCP/UCP/ACP doors.
4. [ ] Intent classification (shopping vs inform) → eligibility/ranking selection.
5. [ ] Re-scope verify-to-serve (`#117`): verify → `index_eligible` (drop the minted referral offer).
6. [ ] Keep Pivota Agent's shopping path on `transact_eligible` (regression-test unchanged).

*Design-only. No code in this ADR.*
