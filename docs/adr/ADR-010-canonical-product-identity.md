# ADR-010: Pivota Canonical Product Identity (resolver-owned, multi-signal)

**Status:** Proposed (rev 2 — substantially revised after adversarial review, 2026-07-09)
**Date:** 2026-07-09
**Deciders:** Commerce-index / Trust & Identity owners (peng)
**Builds on:** ADR-001 (canonical record vs supplier), ADR-007 (citable index vs commerce overlay), ADR-008 (brand-identity reconciliation), ADR-009 (seller-of-record identity). Companion reference: `docs/IDENTITY_REFERENCE.md`.
**Supersedes (partially):** ADR-008's deferral of the identity merge primitive — see "Relationship to ADR-008" below.

> **Rev-2 note.** The first draft of this ADR was reviewed adversarially (two independent passes: codebase fact-check + architecture attack). Several of its factual claims were wrong and its sign-off scope was too large. This revision corrects the facts, adds the missing Option D, resolves the identity-grain question, and narrows the committed decision to a measured, staged increment. The review findings are folded in throughout rather than appended.

## Context

The commerce index's core asset is a **canonical product entity** that offers and sellers attach to — the thing that lets one physical product carry many merchants' offers on one card, accrue trust, and be cited by agents.

**Web-2.0 precedent (the founder's framing, and it is decisive):** the platforms that won GTIN-less categories did not wait for barcodes — they built proprietary catalog-owned identity plus a matching system. Amazon's **ASIN** (catalog-controlled canonical id; listings resolve onto it via matching + contribution + moderation) and Dewu/Poizon's **SPU spec** (in sneakers/luxury, identity is a curated structured-attribute spec that listings attach to). GTIN-less is not a deficiency to patch; it is the condition that makes proprietary identity the moat. Pivota's GTIN coverage is ~0%.

### Current state — corrected by fact-check (file:line verified)

- **Identity keys today.** `content_key = make_content_key(brand, title[, gtin])` (`services/catalog_identity.py:133`) — with no GTIN this is brand+title, documented as *deliberately non-unique*. `product_group_id` (pg) is minted from content_key on the autogrouper/singleton paths (`product_group_autogrouper.py:103,151`) — **but pg is already NOT uniformly f(content_key)**: three other live namespaces exist (`pg_catalog_*` and `pg_ext_*` from `pdp_identity_recovery.py:89,94`; curated `pg_manual_*` from `pdp_governance_service.py:2622`). pg is stored in `product_group_members` and **read from membership, never re-derived at read time** (`_resolve_product_group`, `pdp_governance_service.py:705`). The rev-1 fear that "code recomputes pg from content_key at read time" is a phantom; the content_key→pg writers are ~4 call sites, all batch writers.
- **The real identity spine is `content_key`, not pg.** `agent_pdp_view` (the serving/citation read model) has **PRIMARY KEY content_key** (`085_agent_pdp_view.sql:39`); the decision layer FKs to it (`agent_decision_candidates.content_key REFERENCES agent_pdp_view(content_key)`, `140:59`). Any identity redesign must keep content_key stable as a row-level key.
- **Money does NOT key on pg.** Attribution edges key on merchant-scoped `cp_` canonical ids (`060:35`; `canonical_commerce_service.py:59`); `aggregated_outcomes.subject_key` = merchant | `cp_` id (`149:14`); GSC submissions key on `(merchant_id, url)` (`074:57`). A pg merge therefore does **not** directly fuse attribution/GMV rows — the mis-merge exposure is at the **serving/buy-box layer** (wrong offers on one card; neutrality harm) and the content_key spine, not direct money-row fusion. (Rev 1 claimed the opposite; that claim was refuted with evidence.)
- **Citation handles are stable by construction.** Public cited URLs are per-merchant, **write-once** `pivota_signature_id`s (Trap T5, `IDENTITY_REFERENCE.md`); pg is internal and never a public URL. A merge cannot 404 a citation. The open question a merge *does* raise is **which sig/page is the canonical multi-merchant card** — a serving-selection policy, not an id-stability problem.
- **Scale + measured problem size (D-1 measurement RUN, prod, 2026-07-09).** Catalog = **10,999 products / 8 merchants / 8,284 distinct content_keys** (grown past the 4,895-row Stage-1 backfill count). Measured identity surface:
  - **374 content_keys are shared across >1 merchant, involving 1,513 product rows (~13.8% of the catalog)** — 8× the mig-139 anecdote (47 groups). Cross-merchant identity overlap is *material today*, not hypothetical. Caveat: with brand+title keys, an unknown fraction of these are *collisions* (different products, same key) rather than true duplicates — distinguishing them is precisely the resolver's job, and is why auto-merge stays gated (D2).
  - **1,076 content_keys duplicate within a single merchant** (fragmentation and/or variant families sharing brand+title — the two-grain issue in the flesh).
  - `product_group_members`: 8,500 groups / 11,019 memberships, **3,609 memberships in multi-member groups** (mostly variant-collapse + curation) — the family-grain machinery is already load-bearing.
  - **249 active unattached seeds have an exact canonical_url twin in the catalog** — the Tier-0 exact-attach population is sitting ready (P1.3's dark mechanism covers these the day the co-gate flips).
  Verdict: the numbers justify **Option D now** (the overlap is big enough that resolution is worth building) while still *not* justifying Tier 2/3 (374 cross-merchant keys is trivially coverable by deterministic + attribute matching + review; embeddings/LLM adjudication remain deferred until scale demands them). Re-run monthly as the escalation gauge.
- **Tier-1 inputs are nearly empty today.** `llm_attributes` (mig 174) has a single writer, is **off by default**, allowlisted to the one-merchant electronics pilot, and **beauty never populates it** (beauty attributes come from the lexicon graph / `visible_attributes`, which is the broadly-populated input rev 1 ignored). `VerticalProfile` contains **no identity-defining attribute spec** — that registry entry is greenfield. `pdp_review_tasks` + the employee review API exist (backend plumbing confirmed) but live drain is unverified, and the table is created in service code, not migrations.
- **Membership schema has no room for resolution.** `product_group_members` = `(pg, merchant, platform, platform_product_id, is_primary)` with PK `(merchant, platform, platform_product_id)` (`045:7-16`) — no confidence/evidence/tier columns, and the PK structurally forbids competing proposals. Provenance, proposals, unmerge, and an audit trail are **new schema**, not "mostly a provenance-columns change" (rev-1 understatement).
- **Grain.** pg is explicitly a **family/variant collapse** (`catalog_variant_promoter.py`; IDENTITY_REFERENCE). Offers attach at sku grain. Two merchants' offers for *different shades* already collapse under one pg — so pg alone can never power a like-for-like buy-box.

### The actual structural flaw

Not "pg is derived from a hash" (partially false) but: **there is no resolution process.** Membership is assigned by derivation, curation, or recovery-scripts — with no confidence, no evidence, no proposals, no reversibility, no learning loop — and the entity grain (family) doesn't match the buy-box grain (variant). An ASIN/SPU is *assigned by resolution and improved by adjudication*; Pivota's isn't yet.

## Decision

Adopt **Option B as the target architecture** — the canonical entity becomes a **resolver-owned cluster** — but **commit only Option D as the signed-off increment**: a measurement gate plus the minimal resolver spine (schema + Tier 0/1 propose-mode), with everything aggressive deferred behind explicit gates.

**Target architecture (B):**
1. `content_key` and GTIN become candidate-**blocking** signals; **`content_key` itself is never re-minted or dropped** (it stays the stable row key of the serving spine — hard invariant).
2. Cluster membership (`product_group_members` + new provenance) is assigned by a **tiered resolver**: Tier 0 exact (GTIN / canonical_url / source_product_id — auto), Tier 1 structured-attribute per-vertical spec (propose → review; auto only after the eval gate), Tier 2 embedding/visual candidate-gen (greenfield; deferred), Tier 3 LLM adjudication (deferred), HITL always the floor.
3. **Two-grain identity:** pg remains the *family* entity; a **canonical-variant layer** (cross-merchant variant identity, keyed off the family + identity-defining variant attributes) is where the multi-offer buy-box compares like-for-like. Offers join the buy-box at variant grain, never family grain.
4. Every membership decision carries `{match_tier, confidence, evidence, resolver_version, resolved_at}` and is **reversible** (unmerge) and **append-only stable** (hysteresis: a new resolver version may *propose* moving an attached member, never silently move it).
5. **Adjudication capture from day one, tuning later:** review decisions persist as gold labels; thresholds are hand-set until the eval set is large enough (the flywheel is a year-2 property at current scale, not a year-1 promise).

**Safety invariants (hard, non-negotiable):**
- **Money:** attribution/outcomes stay keyed on merchant-scoped `cp_` ids and are never re-keyed to pg — mis-merge cannot fuse GMV rows by construction; keep it that way.
- **Citations:** sigs are write-once (T5); a merge selects a canonical *card* but never re-mints or retires a cited sig; member pages cross-reference the canonical.
- **Serving:** cross-merchant cards require resolution confidence ≥ auto-threshold **and** `pdp_scope='multi_merchant_canonical'` (mis-merge is worse than fragmentation — the invariant stands; we buy recall with reversibility + gating, not by relaxing it).
- **Neutrality (ADR-007):** a buy-box mis-merge advantages one seller; mis-merge rate is therefore a neutrality metric, monitored, with unmerge SLO.

### Relationship to ADR-008

ADR-008 deliberately **deferred** the general merge/alias primitive ("highest-risk, lowest-demand") and chose prevent-at-intake. This ADR partially supersedes that deferral **on the grounds ADR-008 itself named**: the missing pieces were scoring, evidence, and reversibility — which this ADR makes prerequisites before any merge runs. Prevent-at-intake (the P1.4 guard) remains in force; the resolver adds a governed merge path, it does not replace prevention.

## Options Considered

### Option A: Status quo (derived/curated pg, no resolver)
| Dimension | Assessment |
|-----------|------------|
| Complexity | None |
| Cost | Low now; unbounded later (mis-merge/fragmentation permanent, curation doesn't scale) |
| Moat | None — identity is a recomputable hash + manual effort |

**Pros:** zero work; at ~5k rows curated grouping demonstrably functions.
**Cons:** no path to cross-merchant multi-offer at crawl-scale intake; no learning asset; the two error modes compound as the catalog grows.

### Option B: Resolver-owned cluster on the existing pg substrate (TARGET)
| Dimension | Assessment |
|-----------|------------|
| Complexity | Medium (schema + resolver service; migration surface is small — ~4 writer call sites, no read-time recompute) |
| Cost | Multi-quarter to full depth; real cost is per-vertical spec curation + review staffing |
| Moat | High — proprietary ASIN/SPU-equivalent that improves with adjudication |

**Pros:** builds on real bones (membership table consumed by serving today; matcher skeleton; review-queue plumbing; ADR-009 already made pg total via singletons — this *merges singletons on an existing substrate*). Cons list is now honest: provenance/proposal/unmerge schema is **new** (PK forbids proposals today); Tier-1 attribute inputs must be built (`visible_attributes` first, `llm_attributes` as the extractor ramps); the identity-spec registry is greenfield; HITL staffing at current scale is thin.

### Option C: New `canonical_product_id` entity alongside pg
Rejected — a second identity spine is the ADR-008 dual-store trap, and since ADR-009 already made pg total, the transitional argument for C is even weaker than rev 1 stated. (Fact-check note: rev 1's claim that "attribution, outcomes, GSC all key on pg" — used to argue C's migration cost — was **wrong**; they key on `cp_` ids/URLs. C is still rejected, but on one-spine discipline, not on that claim.)

### Option D: Measurement gate + minimal resolver spine (COMMITTED INCREMENT)
| Dimension | Assessment |
|-----------|------------|
| Complexity | Low-Medium |
| Cost | Weeks, not quarters |
| Risk | Near-zero live risk (propose-mode only; no serving change) |

Ship: (1) the **duplicate/fragmentation measurement** (standing script + prod numbers); (2) provenance/proposal/unmerge **schema**; (3) identity-spec registry for **one pilot vertical** (electronics — where `llm_attributes` actually flows) using `visible_attributes` + `llm_attributes`; (4) Tier 0 auto (exists as P1.3, dark) + **Tier 1 propose-only** into the review queue; (5) gold-label capture. **No auto-merge, no serving change, no Tier 2/3.** Auto-merge and the Phase-2 cross-merchant card flip together later, gated on the eval set and the convergence plan's pivot cutover.

## Trade-off Analysis

The live decision is **how much of B to commit now**. The fact-check reframed the risk surface: the migration is *smaller* than feared (no read-time pg recompute; money doesn't key on pg) but the build is *larger* than claimed (provenance schema is new; Tier-1 inputs and the vertical spec are greenfield; the flywheel needs volume that a ~5k-row catalog with ~47 known duplicate groups cannot generate soon). At that measured scale, a full 4-tier resolver is over-built **today** — but the schema, spec registry, and propose-mode resolver are cheap, become correct-by-construction foundations, and are exactly what crawl-scale intake (the strategic direction) will need. Hence: B as target, D as commitment, with the measurement deciding cadence — if the duplicate rate at 6-12 months of crawl intake stays trivial, Tier 2/3 stay deferred indefinitely at zero waste; if it climbs, the spine is already in place.

## Consequences

**Easier:** cross-merchant buy-box becomes possible at the *correct (variant) grain*; identity decisions become evidenced, reversible, and auditable; GTIN-less verticals get a real identity path; the adjudication dataset starts accruing immediately.

**Harder / to revisit:** per-vertical identity-spec curation is a standing human cost; the resolver, thresholds, review queue, and mis-merge/unmerge metrics become operational surfaces; `pdp_review_tasks` must move into migrations and get a confirmed drain; resolver ownership must be settled (this repo owns membership writes in Python; the Node-side identity-graph readers per ADR-008 A.3 consume, never write); sequencing must respect ADR-009's in-flight seller backfill (no concurrent re-subjecting + merging).

**Relationship to in-flight work:** subsumes P1.3 (= Tier 0, stays dark until the pivot cutover per the convergence co-gate); reframes the Phase-2 regroup as "group by resolved membership at variant grain, confidence-gated" — not a raw brand+title merge.

## Action Items

1. [ ] **Sign off:** Option B as target, **Option D as the committed increment**, the safety invariants, and the two-grain model.
2. [x] **Measurement first (D-1): FIRST RUN COMPLETE** (2026-07-09, prod — numbers in Context above: 374 cross-merchant keys / 1,513 rows / 13.8%; 1,076 intra-merchant dup keys; 249 seed↔catalog URL twins). Remaining: land the script as a standing repo script + re-run monthly as the Tier-2/3 escalation gauge.
3. [ ] Schema (D-2): membership provenance `{match_tier, confidence, evidence, resolver_version, resolved_at}`, a **proposals** table (current PK forbids competing proposals), unmerge path, `identity_resolution_events` audit trail; move `pdp_review_tasks` into migrations.
4. [ ] Identity-spec registry (D-3): identity-defining attributes for the **electronics pilot** vertical in `VerticalProfile`; inputs = `visible_attributes` (broad) + `llm_attributes` (pilot). Beauty follows via its lexicon graph, not `llm_attributes`.
5. [ ] Tier-1 **propose-only** resolver (D-4) feeding the review queue; gold-label capture on every adjudication; hand-set thresholds; per-vertical eval (precision / mis-merge) reported from the labeled set.
6. [ ] Only after 2–5 and the convergence pivot co-gate: auto-merge above threshold + Phase-2 cross-merchant cards (variant grain), with mis-merge/neutrality monitoring + unmerge SLO. Tier 2/3 remain deferred until the measurement justifies them.

**Open decisions for sign-off:** (D1) B-as-target / D-as-increment framing; (D2) Tier-1 auto-merge later requires a *complete* per-vertical identity signature, or confidence threshold alone [rec: complete signature — spec-completeness is the Dewu lesson]; (D3) unmerge granularity [rec: per-member detach + projection re-derive; money rows unaffected by construction]; (D4) merchant-facing identity contributions — review-only in v1.

## Addendum — the publish-surface identity contract (2026-07-09, indexing-recovery session)

The Google-indexing investigation surfaced an identity layer this ADR's resolver scope does not yet
cover: **the identity the outside world sees** (sitemap URLs, PDP `rel=canonical`, JSON-LD `@id`,
agent-facing PDP resolution). Findings are prod-measured (same D-1 run vintage).

**Sig fragmentation is a live publish defect, not just internal debt.** `catalog_products` carries
`pivota_signature_id` in two hex lengths (24-hex: 1,429 rows; 32-hex: 9,356 rows; **no content_key
mixes lengths** — an ingestion-path artifact, not per-product ambiguity). **1,218 content_keys carry
more than one sig**, so the public product sitemap emitted 7,407 URLs for only **6,133 distinct
products** — ~17% duplicate URLs splitting index signal across pages Google must reconcile.
Interim fixes shipped in `pivota-agent-ui` while this ADR's resolver work proceeds: #257 (static
sitemap files — ended GSC "Couldn't fetch"), #260 (stop hard-`noindex` on transient SSR failure,
which ISR cached for up to 1h), #261 (sitemap dedup: **one URL per content_key**, deterministic
winner among duplicate sigs — longer sig class, then lexicographic — max-lastmod merge). #261
landed while product URLs are still unindexed, so the URL consolidation cost zero re-indexing.

**Gap 1 — no single canonical-URL selection policy.** T5 correctly keeps sigs write-once, but
*which* sig is THE public card is decided independently per surface today: the sitemap generator
(the #261 rule) and the PDP's self-declared `rel=canonical`
(`readServerCanonicalRouteId`, `pivota-agent-ui/src/app/products/[id]/page.tsx` — its own
group-id-vs-sig heuristics). Where they disagree, Google receives sitemap URL X whose page declares
canonical Y — a standing index-signal leak. The "canonical multi-merchant card" selection this ADR
already names as a serving-selection policy must be **one deterministic function, exported once and
consumed by sitemap, PDP `rel=canonical`, and JSON-LD `@id`**.

**Gap 2 — publish-side resolution is a parallel implementation.** The Node shop gateway
(`PIVOTA-Agent/src/server.js`, `get_pdp_v2`) implements its own tiered sig→product resolution
(requires a row with `merchant_id` + `source_product_id`; else `400 MISSING_MERCHANT_CONTEXT`),
independent of this repo's identity code. That respects A.3 (Node consumes, never writes) but the
resolution *predicate* is duplicated rather than derived from resolver output — divergence here is
what turned transient backend failures into de-indexed pages during the investigation.

**Additional action items:**

7. [ ] **Canonical-URL policy (D-5):** one deterministic `content_key → public URL` selection,
   single implementation consumed by the sitemap generator, PDP `rel=canonical`, and JSON-LD `@id`
   (#261's rule is the interim reference; align `readServerCanonicalRouteId` with it). Non-winning
   sigs keep resolving (T5) and self-declare the winner as canonical.
8. [ ] **Sig-minting discipline (D-6):** one minting path + truncation length for *new* sigs
   (existing sigs stay write-once per T5); the multi-sig backlog (1,218 content_keys) is consumed
   by the D-2 provenance/alias schema so every historical sig resolves to its canonical card.
