# ADR-002: Agent decision-intelligence layer (facts → intelligence → outcomes)

**Status:** Proposed
**Date:** 2026-06-13
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

## Action items

1. [ ] Ratify the `agent_decision_dossier/v1` shape (the example is the straw man).
2. [ ] Build the **Ingredient Intelligence KB** schema + seed the cohort's hero
   actives (soy isoflavones, niacinamide, adenosine, snail mucin, centella, …)
   with graded, cited mechanism + contraindications + marketing-vs-reality notes.
3. [ ] Build the per-product **synthesis pipeline** (INCI × KB → graded claims),
   LLM-drafted + adversarially reviewed + claim-safety screened.
4. [ ] Build **review synthesis** (multi-source ingest → consistent-signal extraction
   with provenance).
5. [ ] Build the **Brand Intelligence KB**; assemble the dossier; serve on the PDP.
