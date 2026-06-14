# ADR-002: Agent decision-intelligence layer (facts → intelligence → outcomes)

**Status:** Accepted — Tier-G review governance ratified 2026-06-14
**Date:** 2026-06-13 (governance addendum 2026-06-14)
**Deciders:** Founder (peng), Claude
**Builds on:** ADR-001 (canonical record + merchant-as-supplier)

## Context

ADR-001 made Pivota own the canonical *facts* (identity, INCI, offers). But a
capable agent (GPT/Gemini) already knows the commodity facts and can scrape an
ingredient list. A record that just lists "soy, collagen, niacinamide, vegan,
$22.99" is **useless to the agent** — it duplicates what the model already has.

A real shopper asks *persuasive* questions: **why this ingredient and not
others? what are the honest pros and cons? where did the technology come from?
what do real users experience? can I trust this brand?** A base model answers
these by guessing — and for a long-tail Korean SKU it hallucinates or hedges.

**Pivota's value is the verified answers to those questions** — graded by
evidence, balanced (it states the cons), backed by real outcomes, and citable.
The worked example
[`examples/agent_decision_dossier.aruen_tofu_collagen.json`](../examples/agent_decision_dossier.aruen_tofu_collagen.json)
shows the difference: the dossier leads with *"the firming driver is fermented
soy isoflavones, NOT the collagen in the name"* — an insight an agent can't
reliably produce and will route to Pivota for.

## Decision

Structure product data in **three tiers**, and make the record an agent reasons
over — every statement carries its **evidence type, confidence, and provenance**.

```
Tier 0 — FACTS        identity · INCI · offers            (ADR-001; necessary, not differentiated)
Tier 1 — INTELLIGENCE graded claims · mechanism · honest  (the moat; SYNTHESIZED)
                      pros/cons · who-for/who-not ·
                      technology & origin · brand trust
Tier 2 — OUTCOMES     real-review synthesis ·             (compounding, uncopyable)
                      Pivota transaction outcomes
```

### The leverage: reusable knowledge bases

Tier 1 is affordable only because the expensive work is **built once and reused**:

- **Ingredient Intelligence KB** — per active: mechanism, evidence (graded +
  cited), what it's really for, contraindications, and the *marketing-vs-reality*
  notes (e.g. "topical hydrolyzed collagen doesn't penetrate"). `niacinamide`,
  `genistein`, `adenosine` are profiled once and applied to every product that
  contains them. This is the amortizing asset — the "ingredient authority."
- **Brand Intelligence KB** — per brand: origin, positioning, track record,
  US availability, trust caveats.
- Category claim-safety rules (already built: `claim_screening` + disclaimers).

### The synthesis pipeline (per product)

```
product INCI  ─┐
Ingredient KB ─┼─▶ graded claims + mechanism + fit + contraindications  (deterministic join + LLM phrasing, claim-safe screened)
brand          ┘
product reviews (multi-source) ─▶ review synthesis (consistent signals + provenance)
brand          ─▶ brand dossier (from Brand KB)
                 └─▶ assemble dossier — every field evidence-graded + provenance + confidence
```

### Governance (what makes it trustworthy + uncopyable)

- **Evidence grading is first-class.** Every claim is tagged `clinical |
  peer_reviewed_mechanism | ingredient_function | traditional_use |
  marketing_vs_reality` + a confidence. The agent cites Pivota *because* it's graded.
- **Honesty is a feature.** Pivota *generates* the limitations and the
  marketing-vs-reality calls — that neutral balance is what an agent (and shopper)
  trusts. Synthesis is LLM-assisted then adversarially reviewed (the existing
  LLM-classify → codex-review pattern) for defensibility + claim-safety.
- **Neutrality.** The assessment, including cons, is independent of any
  brand/merchant payment. No pay-to-rank in the intelligence.
- **Freshness.** Dossiers refresh as reviews and science update.

### Trust tiers & review governance (ratified 2026-06-14)

Human review does not scale to the long-tail wedge — it concentrates on pilot
brands and leaves the actual cohort (Aruen, Ownist, BB Lab, …) with *zero*
served intel. The gate is therefore **tiered, not binary**:

```
Tier H — human_reviewed   human rewrite/sign-off            serves (today's gate)
Tier G — grounded         trustworthy BY CONSTRUCTION:      serves (NEW — the cohort path)
                          verified INCI + cited mechanism +
                          per-claim grading + claim-safe +
                          agent adversarial-review pass
Tier L — ungrounded LLM   drafted, no verified grounding    BLOCKED (no fabrication to agents)
```

**Per-SKU gate = agent review, not human.** A grounded bundle is assembled
deterministically (verified INCI × reviewed Ingredient KB × per-claim grading),
then an **agent adversarially reviews it** (the existing LLM-classify →
codex-review pattern) — that pass *is* the Tier-G gate. No human in the per-SKU
loop. Founder, 2026-06-14: *"dispatching an agent to review is good enough
comparing to humans."*

**Human = sampled QA on the KB, not per-SKU sign-off.** Humans spot-check
**randomly-selected Ingredient-KB entries** to keep the reusable asset honest —
statistical quality assurance on the compounding layer, where the leverage is.
Per-SKU bundles inherit that quality through the deterministic join, so they
don't need their own human pass. Founder: *"we can always let human check on KB
randomly selected to verify the quality."*

**Why this is safe:** Tier-G acceptance is *additive* — it never loosens what
already serves, and ungrounded LLM output (Tier L) stays blocked. A bundle only
earns `tier: grounded` if `inci_verified ∧ citations_present ∧ per-claim graded ∧
claim-safe-screened ∧ agent-review = pass`; failing any one drops it to Tier L
(blocked), not through. Prototype:
[`examples/product_intel_grounded.aruen_tofu_collagen.json`](../examples/product_intel_grounded.aruen_tofu_collagen.json).

**Gate change:** extend `isHumanReviewedProductIntelBundle`
(`PIVOTA-Agent/src/pdpProductIntel.js`) from a boolean human-marker check to a
tier resolver returning `human | grounded | reject`, accepting
`provenance.tier === 'grounded'` when the construction predicates hold.

## Options considered

- **A — three-tier dossier with reusable KBs (chosen).** Differentiated, citable,
  amortizes the science across the catalog. Cost: building the KBs + a reviewed
  synthesis pipeline.
- **B — richer facts only (status quo+).** Cheap, but it's what the agent already
  has — no reason to route to Pivota. Rejected.
- **C — per-product LLM free-write.** Fast to fake, but ungraded, unverifiable,
  unsafe, and not reusable — exactly the hallucination Pivota is supposed to
  replace. Rejected.

## Consequences

**Easier / unlocked**
- A genuine reason for agents to route to Pivota over their own knowledge.
- The ingredient KB compounds: every new product is cheaper to make decision-grade.
- Claim-safety and neutrality become product features, not constraints.

**Harder / new work**
- New stores: `ingredient_intelligence`, `brand_intelligence`, `product_dossier`,
  `review_synthesis` (alongside the Tier-0 fact tables).
- A reviewed synthesis pipeline (LLM + adversarial verification + claim-safety) —
  must never let ungraded marketing copy reach the agent as fact.
- Multi-source review ingestion + dedup + provenance.

**To revisit**
- Confidence calibration + an audit trail for every graded claim.
- How outcomes (Tier 2) feed back to re-grade claims as real data accrues.

## Reconciliation: this EXTENDS Pivota Insights, it does not replace it

This was first drafted in a vacuum. The structure largely **already exists** as
**Pivota Insights** (`pivota.product_intel.v1`) — the served contract lives in
`pivota-agent-ui/src/features/pdp/types.ts`, the producer in
`PIVOTA-Agent/src/pdpProductIntel.js` (+ `auroraBff/`), persisted in
`aurora_product_intel_kb` (per product) — and the per-ingredient KB exists too
(`aurora_ingredient_research_kb`). The right move is to **extend the missing
dossier dimensions and wire up what's already built**, in those surfaces.

Mapping the dossier → existing Pivota Insights fields:

| Dossier dimension | Pivota Insights today | Action |
|---|---|---|
| headline / differentiator | `product_intel_core.what_it_is`, `why_it_stands_out[].{headline,body,evidence_strength}` | **extend** — add *why this ingredient not others* + mechanism |
| graded claims | per-FIELD `confidence` + `evidence_profile` (`seller_only…community_supported`) | **add** — per-CLAIM grading incl. a `marketing_vs_reality` type |
| honest pros / cons | `community_signals.{top_loves,top_complaints,mixed_feedback}`, `watchouts[]` | **extend** — explicit `not_for` / contraindications |
| who-for / who-skip | `best_for[]`, `community_signals.best_fit_users[]` | **add** — `not_fit_users[]` / skip-if |
| real-world outcomes | `community_signals` (reviews/creator/editorial synthesis) ✅ strong | reuse; add Pivota transaction outcomes |
| technology / origin | `why_it_stands_out` (partial) | **extend** via the ingredient KB |
| brand trust | not in product_intel (`catalog_row_trust` exists separately) | **connect** |
| ingredient intelligence | `aurora_ingredient_research_kb` EXISTS (46 rows, usage/safety; Gemini; TTL) but **not integrated/served** | **integrate + enrich** (mechanism + graded evidence + marketing-vs-reality) |
| provenance + confidence | `provenance.field_sources`, `confidence{overall,fields}`, evidence `_meta` ✅ | reuse |

## Action items (corrected — extend/integrate, don't rebuild)

1. [ ] Treat **`pivota.product_intel.v1` as the home**; the dossier example is a
   straw man for the *extensions*, not a new contract.
2. [ ] **Integrate the existing `aurora_ingredient_research_kb`** into
   `product_intel_core` (it's built but unserved) — and **enrich** it from
   usage/safety into curated, graded *mechanism + evidence + marketing-vs-reality*
   for the cohort's hero actives (soy isoflavones, niacinamide, adenosine, …).
3. [ ] **Add per-CLAIM grading**: an `evidence_claims[]` block on
   `product_intel_core` with `{claim, drivers, evidence_type (incl.
   marketing_vs_reality), confidence, source_refs[]}`.
4. [ ] **Extend honest-fit fields**: `not_for[]` / contraindications +
   `not_fit_users[]` (inverse of `best_for`/`best_fit_users`).
5. [ ] **Connect brand trust** (`catalog_row_trust` → a `brand_trust` block).
6. [ ] These changes land in **PIVOTA-Agent** (`pdpProductIntel.js` / `auroraBff`)
   + **pivota-agent-ui** types/rendering + the aurora KBs — NOT the pivota-backend
   canonical-sourcing engine, which is the Tier-0 facts feed. Coordinate with
   whoever owns the product_intel surface before extending it.

### Tier-G build sequence (governance ratified 2026-06-14)

7. [ ] **Enrich `aurora_ingredient_research_kb` for the cohort's hero actives**
   (soy isoflavones/genistein, adenosine, niacinamide, centella, …) to ADR-002
   quality — mechanism + graded evidence (cited) + `marketing_vs_reality` +
   contraindications. Pipeline: LLM/codex backfill → **agent adversarial review**
   → store with review provenance. This is the **load-bearing, reusable** step
   (today's KB is 46 thin/fallback rows; Aruen's differentiating actives are
   absent). Human **random-sample QA** runs against this table.
8. [ ] **Grounded generator** — deterministically assemble a
   `product_intel.v1` bundle from verified INCI × reviewed Ingredient KB ×
   per-claim grading × claim-safety, stamping `provenance.tier='grounded'`
   (+ `reviewer_kind='automated_grounded'`, `grounding.{inci_verified,
   citations_present,claim_safety}`). Each assembled bundle gets the agent
   adversarial-review pass before it's stamped.
9. [ ] **Tiered gate** — extend `isHumanReviewedProductIntelBundle` →
   `human | grounded | reject` (see governance section). Additive; Tier-L stays
   blocked. Coordinate with the product_intel surface owner (parallel session).
