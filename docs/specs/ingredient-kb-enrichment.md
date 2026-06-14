# Ingredient KB enrichment — Tier-G load-bearing step (ADR-002 item 7)

**Status:** spec for execution
**Date:** 2026-06-14
**Owner surface:** PIVOTA-Agent (`aurora_ingredient_research_kb`, producer in
`src/auroraBff/routes.js`, store in `src/auroraBff/ingredientResearchKbStore.js`)
**Why:** the reusable, compounding asset behind Tier-G. The grounded generator
(item 8) joins product INCI × this KB; if the KB is thin, every bundle is thin.
Today the KB is 46 rows, mostly `fallback`, and the cohort's *differentiating*
actives (soy isoflavones/genistein, adenosine) are **absent**.

## Don't fork the shape — extend `v2-lite`

The existing `ingredient_profile_json` already carries most of what ADR-002
Tier-1 needs. The store persists `ingredient_profile_json` as an opaque
pass-through, so **adding fields needs no migration**. Reuse what's there:

| Need (ADR-002) | Already in `v2-lite` | Action |
|---|---|---|
| graded evidence | `evidence.grade` (A–D), `evidence.summary`, `evidence.citations[]{title,url,year,source}` | **reuse** — require grade + ≥1 citation for cohort hero actives |
| per-benefit | `benefits[]{concern, strength(0–3), what_it_means}` | **extend** — add `mechanism` (the *why*, not just *what*) |
| contraindications | `safety.watchouts[]{issue, likelihood, what_to_do}` | reuse |
| routine fit | `usage.{routine_step, pair_well[], consider_separating[]}` | reuse |
| identity | `ingredient.{inci, display_name, aliases[], what_it_is}` | reuse |
| **marketing-vs-reality** | — *(missing)* | **add** `ingredient.marketing_vs_reality[]` |
| **review provenance** | `source_meta` (free-form) | **add** `source_meta.review{...}` |

### Added fields (additive, no migration)

```jsonc
ingredient.marketing_vs_reality: [           // the honest, hard-to-fake calls
  { claim_in_market: "collagen cream builds collagen",
    reality: "topical hydrolyzed collagen is too large to penetrate; it hydrates the surface",
    evidence_type: "marketing_vs_reality" }
]
benefits[].mechanism: "genistein up-regulates type-I procollagen + inhibits MMP-1"  // the WHY
source_meta.review: {
  method: "agent_adversarial",             // ADR-002: agent review IS the gate
  decision: "pass" | "revise" | "reject",
  reviewer: "codex" | "<model>",
  reviewed_at: "<iso>",
  human_qa: { sampled: false, decision: null, reviewer: null, at: null }  // filled by random-sample QA
}
```

`schema_version` stays `v2-lite` (additive); set `confidence_level` honestly
(`high` only with grade A/B + citations). Status `ready` requires the existing
gate (`confidence_level !== 'low' && ingredient.what_it_is && watchouts.length > 0`)
**plus**, for Tier-G hero actives: `evidence.grade ∈ {A,B,C}` and
`evidence.citations.length ≥ 1` and `source_meta.review.decision === 'pass'`.

## Cohort hero actives (priority order)

Drawn from the cohort INCI (Aruen, Ownist, BB Lab, Baie Botanique). Enrich the
**differentiators** first — the ones a base model gets wrong or generic:

1. **Soy isoflavones / genistein** (Aruen) — phytoestrogen firming; the
   "collagen cream isn't collagen" call. *Gold-standard exemplar shipped:*
   [`examples/ingredient_kb.genistein_soy_isoflavones.json`](../examples/ingredient_kb.genistein_soy_isoflavones.json).
2. **Adenosine** — anti-wrinkle signaling; commonly under-explained.
3. **Niacinamide** — tone/barrier; correct the over-claiming (no, it doesn't "shrink pores").
4. **Centella asiatica / madecassoside** — barrier/soothing; traditional-use + mechanism.
5. **Fermented ferments** (Lactobacillus/Bifida lysate, Galactomyces) — bioavailability + postbiotic story.
6. **Hyaluronic acid / sodium hyaluronate** — humectant; correct "plumps from within" overclaim.
7. **Snail secretion filtrate, propolis, panthenol, ceramides** — round out the cohort.

## Production pipeline (ADR-002 governance)

```
draft (LLM/codex, web-grounded)
  → agent adversarial review  ← THE GATE (no human per-active)
      checks: every benefit/claim has mechanism + ≥1 real citation;
              evidence_type honest (no clinical-grade label on mechanism-only data);
              marketing_vs_reality is genuinely corrective, not filler;
              no medical/drug claims (cosmetic-safe); watchouts present
  → on pass: upsert status='ready', source_meta.review.decision='pass'
  → on revise/reject: do NOT serve (stays fallback/queued)
human random-sample QA  ← periodic, on the KB table (NOT per-active)
  sample N rows/week, verify citations resolve + claims hold;
  on fail: flip source_meta.review.human_qa.decision='reject' + re-queue,
           and widen the sample (a miss implies a systematic drafting fault)
```

### Agent-review rubric (the gate must reject on any)
- A benefit/claim with no `mechanism` or no resolvable citation.
- `evidence_type: clinical` without a human-trial citation (downgrade to `peer_reviewed_mechanism`).
- `marketing_vs_reality` that just restates the benefit (must correct an over-claim).
- Any drug/medical claim ("treats", "cures", "heals") — cosmetic vocab only.
- Empty `watchouts` for an active with known contraindications (e.g. soy → allergen/pregnancy).

### Human random-sample QA protocol
- **Cadence:** weekly while enriching; monthly at steady state.
- **Sample:** `max(5, ceil(0.1 × ready_rows_added_since_last_QA))`, uniform random over `status='ready'` Tier-G rows.
- **Pass bar:** citations resolve and support the graded claim; `marketing_vs_reality` is accurate; no drug claims. ≥90% pass → batch trusted. <90% → reject failures, widen next sample, fix the drafting prompt.
- Record outcomes in `source_meta.review.human_qa`.

## Execution
- Curated **seed** (not the live on-demand Gemini path) so hero actives are
  pre-populated `ready` and don't depend on a thin runtime call. Seed loader
  writes via `upsertIngredientResearchKbEntry` (layer `generic`, lang `EN`).
- Drafting = LLM/codex backfill (web-grounded) against this spec + the
  genistein exemplar as the quality bar; **Claude reviews** the output before it
  is trusted (treat codex output as candidate, not truth).
- Lands in PIVOTA-Agent; coordinate with the product_intel surface owner before
  wiring the KB into `product_intel_core` (item 8).
