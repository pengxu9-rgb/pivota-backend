# Path to Merchant-Ready — Audit Reporting Layer

**Status:** in progress · **Date:** 2026-06-15 · **Owner:** (assign)

The analytical core of the merchant AI-readiness audit is now solid: real cited
hosts (Fix 1, #888), honest findability-vs-endorsement (Fix 2, #894), and a
decision-grade merchant narrative (Fix 3, #895, reviewed + hardened). The
*diagnosis* a merchant reads is correct and non-inflated.

This punch list is what remains before **outside merchants** can use it end-to-end.

---

## A — Portal rendering  *(hard blocker: merchants can't see any of this yet)*

**Goal:** render `report_jsonb.merchant_narrative` + `authority_map` in the
AI-readiness page. Frontend (separate repo); the backend contract is frozen.

- Render the 7 narrative sections: `headline_story`, `whats_working`
  (+`evidence_excerpt`), `where_youre_losing` (+`who_ai_cites_instead`),
  `per_sku_scorecard`, `verify_summary_plain`, `prioritized_actions`,
  `honest_limits`.
- Render `authority_map.host_attribution_summary` as a **findability vs
  endorsement** split (two distinct buckets, never merged); show per-host
  `citation_role` chips.
- Degrade gracefully: hide `0/0` branded/category counts on backfilled runs;
  show `honest_limits` prominently; print "not available" notes verbatim.

**Acceptance:** a live run renders all 7 sections; findability ≠ endorsement
visually; "who AI cites instead" shows real competitors + hosts.
**Effort:** M–L (frontend) · **Risk:** low · **Longest pole — start now, in parallel.**

## B — Action copy-quality fix  *(✅ done this session)*

**Defect:** when the real query generator fell short of the requested prompt
count it padded with placeholders `"<title> shopper question <n>"`
(`agent_center_bd_report_service._build_per_sku_base_query_specs`). Those leaked
into action headlines (*"Own the answer to '… shopper question 15'"*).

**Fix (landed):** `is_synthetic_probe_query()` in `services/sku_lane_priority.py`;
`_top_open_lanes` (sku_opportunity) now excludes placeholder queries from open
lanes; `_sku_query_phrase` (next_best_action) degrades any placeholder to a
generic phrase as a safety net. Tests in
`tests/services/test_synthetic_query_suppression.py`.

**Reviewed, not changed:** the per-gap CTA labels ("Publish the enriched product
page", etc.) are short button labels backed by now-rich action content
(headline / why / first_move / self-serve steps / pivota_path) — acceptable;
rewriting would be lateral.

**Follow-up (deeper, needs a billing decision):** stop *generating/probing* the
placeholder queries at all (they also waste LLM calls). Options: generate real
category/intent variations to fill the quota, or bill only for real queries
rather than padding to `prompts_per_sku`.

## C — Multi-provider coverage  *(unblocked by OpenAI top-up)*

**State:** default profile was `pilot_gemini` (Gemini only). `us_shopper`
(Gemini + ChatGPT + DeepSeek verify) already exists; `OPENAI_API_KEY` is live in
prod.

1. ✅ Validation run enqueued on `us_shopper` for the Aruen scope (run
   `cc6d1f16-44f9-4e73-91a0-e8bf3f3d6900`) — confirm ChatGPT citations appear,
   `citation_by_provider` splits by engine, `cost_summary` meters both,
   `honest_limits` no longer says "Gemini-only".
2. **Confirm ChatGPT cost metering end-to-end** (`provider_credit_rates.json` +
   #884/#885) — un-metered ChatGPT COGS was the original reason it was pulled;
   verify before going wide.
3. Decide the premium gate (`premium_providers` is currently empty) — which
   merchant tier gets ChatGPT.
4. Only then flip the active/default profile `pilot_gemini → us_shopper`
   (`config/coverage_profiles.json`).
5. *(Next)* add Claude via the `full` profile once Agent-engine support +
   metering are confirmed.

**Acceptance:** a run shows per-engine citations (gemini + chatgpt), both metered.
**Effort:** S · **Risk:** med — **gate on confirming metering before the default flip.**

## D — Real-merchant validation sweep

**Goal:** prove correctness beyond the Aruen/Ownist pair before going wide.

**Cohort (~5–8):** a multi-SKU brand; a brand with genuine *editorial*
endorsement; an *invisible* brand (zero grounding); a *non-beauty* category; one
with a populated domain and one without.

**Per merchant, check:** identity resolves (`own_domain` tagged, or honest
degrade); findability/endorsement split correct (spot-check hosts);
`who_ai_cites_instead` competitors are real (no fabrication); narrative clean (no
scaffolding/contradictions); actions specific. Run on `us_shopper`. Use the
prod-extract-and-render protocol from the Fix 2/3 validation.

**Acceptance:** a sign-off table across the cohort; defects filed.
**Effort:** M · **Risk:** low · **Depends on B + C.**

**Cross-cutting — identity population:** confirm onboarding reliably captures
brand + domain (Fix 2's `own_domain` depends on it; the test merchant
`external_seed` had `merchant_domain=None`). Fold into D or do standalone.

---

## Sequence
1. **A** — start now (frontend, longest pole), against the frozen contract.
2. **C** — quickest win; verify metering, then flip the default.
3. **B** — ✅ done; deeper probe-generation follow-up optional.
4. **D** — after B + C; gates go-wide.
