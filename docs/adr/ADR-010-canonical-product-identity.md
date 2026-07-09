# ADR-010: Pivota Canonical Product Identity (resolver-owned, multi-signal)

**Status:** Proposed
**Date:** 2026-07-09
**Deciders:** Commerce-index / Trust & Identity owners (peng)
**Builds on:** ADR-001 (canonical record vs supplier), ADR-007 (citable index vs commerce overlay), ADR-008 (brand-identity reconciliation), ADR-009 (seller-of-record identity). Companion reference: `docs/IDENTITY_REFERENCE.md`.

## Context

The commerce index's core asset is a **canonical product entity** that offers and sellers attach to — the thing that lets one physical product carry many merchants' offers in one card, accrue trust, and be cited by agents. Today that entity is `product_group_id` (pg). But:

- **pg is derived 1:1 from the title hash.** `pg = "pg_" + content_key` where `content_key = make_content_key(brand, title[, gtin])` (`services/catalog_identity.py`, `services/product_group_autogrouper.py`). GTIN coverage is ~0% (see the GTIN-enrichment scoping), so in practice `content_key` is **brand + title**, which the code itself documents as *deliberately non-unique* ("two different physical products can share one key", `catalog_identity.py`).
- **Identity therefore inherits both error modes of a weak key:**
  - *Fragmentation* — the same product listed with two different titles → two `content_key`s → two pgs → never merged (two cards instead of one).
  - *Collision* — two different products sharing brand+title → one pg → wrongly merged (offers/prices/attribution cross-contaminate, and it's effectively write-once).
- **An identity that is a function of a string cannot survive title drift.** A real canonical id is *assigned by a resolution process*, not derived from the listing text.

**Web-2.0 precedent is decisive here.** The platforms that won GTIN-less categories did not wait for barcodes — they built proprietary catalog-owned identity plus a matching system:
- **Amazon — ASIN:** a catalog-controlled canonical product id; many seller listings resolve onto one ASIN via a matching + contribution + moderation pipeline. The ASIN catalog is a durable moat.
- **Dewu / Poizon — SPU spec:** in sneakers/luxury (grey-market, no usable barcodes), identity is a curated **structured-attribute spec** (model + colorway + ... ) — a SPU — that listings and offers attach to. The spec system *is* the moat.

The lesson: **GTIN-less is not a deficiency to patch; it is the condition that makes proprietary identity the defensible asset.** For a neutral commerce index, the canonical identity + its resolver is the core moat.

**Pivota already has ~60% of the bones**, which is why this is an evolution, not a greenfield build:
- `product_group_id` (the ASIN slot) + `product_group_members` (the membership table: many listings → one entity).
- `services/pdp_matcher/` — a matching skeleton (deterministic matchers + optional Gemini LLM tail), today a batch CLI.
- `VerticalProfile` registry + `catalog_products.llm_attributes` (mig 174) — per-vertical **structured attributes**: the raw material for spec-based (Dewu-style) identity.
- `claim_state` / `brand_claims` / `pdp_review_tasks` — a provenance + human-adjudication layer.
- ADR-009 seller-of-record + ADR-008 brand-fragmentation guard — the seller/offer layer that hangs off the product entity.

**The gap:** identity is a hash of the title, not a resolved entity. There is no multi-signal resolver, no confidence/evidence on a merge, no reversibility (unmerge), and no learning loop from human corrections. Convergence P1.3 (shipped dark) added *deterministic-exact* seed→canonical attachment — the safe floor — but exact signals alone can't do cross-merchant identity (different merchants share neither URL nor SKU), which is exactly what multi-offer serving needs.

## Decision

Make the canonical product entity a **resolver-owned cluster**, decoupled from the title hash:

1. **`content_key` and GTIN become candidate-*blocking* signals, not the identity.** They generate match candidates; they do not *define* the cluster.
2. **The canonical entity is a resolved cluster** (`product_group_id` + `product_group_members`) whose membership is **assigned by a tiered resolver**, carrying **confidence, evidence, and reversibility** on every attach.
3. **Tiered resolver** (the Amazon/Dewu pattern), highest-precision first:
   - **Tier 0 — exact/deterministic (auto):** GTIN, exact `canonical_url`, exact `source_product_id`. Near-zero false-positive. (P1.3 already does this for seed→canonical.)
   - **Tier 1 — structured-attribute / vertical-spec (Dewu SPU):** match on the attributes that *define* identity in that vertical, from `llm_attributes` against the `VerticalProfile` spec (e.g. beauty: brand + product line + size/shade; electronics: brand + model + variant). Confidence-scored; auto-merge above a high per-vertical threshold, else propose-for-review.
   - **Tier 2 — embedding / visual candidate generation:** text + image embeddings to *surface* candidates for Tier-1 attribute verification (never to merge on their own).
   - **Tier 3 — LLM adjudication:** for the ambiguous tail, an LLM decides same/different with the vertical spec as context. Proposal, not silent merge.
   - **HITL review:** low-confidence and cross-merchant-canonical decisions route to `pdp_review_tasks`.
4. **Contribution / review loop (the flywheel):** human + merchant corrections become **gold labels** that tune per-vertical thresholds and evaluate the resolver. Every merge is **reversible** (an unmerge path) and **evidenced** (why these were merged). This is what makes aggressive matching *safe* — the reason ADR-009/P1.3 kept auto-merge to exact-only was the absence of reversibility and scoring; this ADR supplies both.

## Options Considered

### Option A: Keep `pg = f(content_key)` (status quo)
| Dimension | Assessment |
|-----------|------------|
| Complexity | Low (nothing to build) |
| Cost | Low upfront / High ongoing (mis-merges + fragmentation are permanent) |
| Scalability | Poor — quality degrades as catalog grows and title variants multiply |
| Moat | None — identity == a public hash anyone can recompute |

**Pros:** deterministic, simple, already shipped.
**Cons:** inherits both error modes forever; cannot do cross-merchant identity; no moat; blocks Phase-2 multi-offer serving.

### Option B: Evolve `pg` into a resolver-owned cluster (RECOMMENDED)
| Dimension | Assessment |
|-----------|------------|
| Complexity | Medium — reuses pg + `product_group_members` + matcher + vertical specs |
| Cost | Medium/High, multi-quarter (per-vertical spec curation is the real cost) |
| Scalability | Strong — quality *improves* with data via the gold-label loop |
| Moat | High — a proprietary, curated ASIN/SPU-equivalent |

**Pros:** builds on existing bones (least churn); `content_key` degrades gracefully to a blocking signal; membership table already exists; subsumes P1.3 and the Phase-2 grouping cleanly; the flywheel compounds.
**Cons:** pg stops being a pure function of content_key (a semantic change downstream code assumes — see Consequences); needs confidence/evidence/reversibility columns + a resolver service; ongoing curation investment.

### Option C: New `canonical_product_id` entity alongside pg
| Dimension | Assessment |
|-----------|------------|
| Complexity | High — a parallel identity spine to keep in sync with pg |
| Cost | High — dual-write, dual-read, migration of every consumer |
| Scalability | Same end-state as B, more transitional risk |
| Moat | High (same asset, later) |

**Pros:** clean-slate schema; leaves pg semantics untouched during transition.
**Cons:** two identity systems to reconcile (the exact dual-store trap ADR-008 warns about); every consumer (offers, attribution, trust, GSC, serving) must migrate; higher blast radius for no better end-state than B.

## Trade-off Analysis

The decision is **B vs C** — A is a non-starter once identity is treated as the moat. B and C reach the same end-state (a resolved canonical entity); the difference is transition risk. C spins up a *second* identity spine, which is precisely the fragmentation ADR-008 exists to prevent and doubles the consumer-migration surface (attribution edges, `aggregated_outcomes`, seller_trust, GSC, pivot serving all key on pg today). B keeps *one* entity and changes how its membership is *assigned* — from "derived from a hash" to "resolved by a scored process." The membership table (`product_group_members`) already supports many→one, so B is mostly a resolver + provenance-columns change, not a schema rebuild.

The load-bearing risk in B is that **pg stops being a pure function of `content_key`**: code that recomputes pg from content_key (rather than reading membership) would break. That is a bounded audit (grep `derive_product_group_id` / `make_singleton_product_group_id` callers) and is the primary migration task.

The precision/recall posture is set per tier and is *reversible*: start conservative (exact + high-threshold spec), widen as the gold set grows. Reversibility (unmerge) is the safety net that lets recall climb without the collision cost being permanent.

## Consequences

**Easier:**
- Cross-merchant multi-offer cards (Phase-2) become correct-by-construction — offers attach to a resolved entity, not a title hash.
- Identity quality *improves over time* (gold-label loop) instead of degrading with scale.
- GTIN-less verticals (beauty, electronics pilot) get real identity; GTIN, when present, is just the strongest Tier-0 signal.
- A defensible catalog asset (ASIN/SPU-equivalent) — the neutral-index moat.

**Harder / to revisit:**
- pg semantics change — every "recompute pg from content_key" site must switch to reading membership. **Migration task, must precede resolver rollout.**
- Per-vertical spec curation is an ongoing human cost (this is the Amazon/Dewu reality — years, not a sprint).
- The resolver, its thresholds, and the review queue become operational surfaces to monitor (mis-merge rate, unmerge rate, review backlog).
- Confidence/evidence/reversibility columns + an `identity_resolution` audit trail need schema.

**Relationship to in-flight work:**
- Subsumes **P1.3** (deterministic-exact attach becomes Tier 0 of the resolver).
- Reframes **Phase-2 multi-offer serving**: the `sku_key → content_key` regroup becomes "group by resolved pg," gated on resolution confidence + `pdp_scope='multi_merchant_canonical'` — no longer a raw brand+title merge.
- Complements **ADR-009** (seller-of-record): seller lives on the offer; this ADR hardens the product the offer attaches to.

## Action Items

1. [ ] **Sign-off on Option B** (resolver-owned pg) and the tiered model.
2. [ ] Audit + document every site that derives pg from content_key vs reads `product_group_members` (the migration surface).
3. [ ] Schema: add resolution provenance to `product_group_members` (or a companion table) — `match_tier`, `confidence`, `evidence`, `resolved_at`, `resolver_version` — plus an **unmerge** path and an `identity_resolution_events` audit trail.
4. [ ] **First implementation slice — Tier 1 (structured-attribute / vertical-spec matching):** resolve identity from `llm_attributes` against the `VerticalProfile` spec, feed `product_group_members` with confidence + evidence, auto-merge above a high per-vertical threshold else `pdp_review_tasks`. Reuses P1.3's `_candidates_for_seed` + `pdp_scope` scope gate; starts on the electronics pilot + beauty (GTIN-less).
5. [ ] Stand up the **gold-label loop**: HITL review decisions persist as labeled pairs; a per-vertical eval (precision/recall/mis-merge) gates threshold changes.
6. [ ] Only after 3–5: enable cross-merchant auto-merge + the Phase-2 pivot regroup (flip together, behind the shadow flag, per the convergence plan co-gate).

**Open decisions for sign-off:** (D1) evolve pg in place [rec] vs new entity; (D2) does Tier-1 auto-merge require a *unique* attribute signature per vertical (spec completeness) before it may auto-attach, or is a confidence threshold sufficient; (D3) unmerge granularity — per-pair vs re-cluster; (D4) whether merchant-facing identity contributions (Amazon-style) are in scope for v1 or review-only.
