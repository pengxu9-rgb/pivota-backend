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

**Backend confirmed ready (2026-06-15):** `sanitize_report_for_merchant` is a
passthrough (strips only score `breakdown` blocks), so `merchant_narrative` +
`authority_map` already reach the frontend via the merchant audit GET endpoint.
**No backend change needed.** The exact field-by-field render contract is now in
[`docs/PORTAL_RENDERING_CONTRACT.md`](PORTAL_RENDERING_CONTRACT.md) — hand it to
the frontend team to unblock the build.

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

**Follow-up (deeper — confirmed to need a product/billing decision):** stop
*generating/probing* the placeholder queries at all (they also waste a paid LLM
call each). Verified the blocker: the audit charge is a function of
`prompts_per_sku` (`_audit_metering(prompts_per_sku=...)` in
`routes/audit_runs_routes.py`), so simply capping at the real query count would
charge the merchant for probes they didn't get. The two honest options:
(a) a **richer query generator** that produces `prompts_per_sku` genuinely
distinct real queries (today `_build_per_sku_base_query_specs` yields only ~8–15
from title + category, then pads); or (b) **bill by actual real queries** (change
`_audit_metering` to charge generated-count, not requested-count). Either is a
design decision, not a code edit — needs sign-off. The #897 suppression keeps the
junk out of merchant copy in the meantime.

## C — Multi-provider coverage  *(unblocked by OpenAI top-up)*

**State:** default profile was `pilot_gemini` (Gemini only). `us_shopper`
(Gemini + ChatGPT + DeepSeek verify) already exists; `OPENAI_API_KEY` is live in
prod.

1. ✅ **Validated** on `us_shopper` (run `cc6d1f16-44f9-4e73-91a0-e8bf3f3d6900`,
   Aruen): `authority_map` providers = `['chatgpt','gemini']`;
   `citation_by_provider` splits by engine; `honest_limits` now reads "grounded
   on gemini, chatgpt"; `aruen.us → own_domain` cited by **both** engines (Fix 2
   holds multi-engine).
2. ✅ **Metering confirmed** — `cost_summary` captured per provider:
   chatgpt **$1.87** (10 calls, 348k input tokens), gemini $0.018, deepseek
   $0.003. The original un-metered-COGS problem is fixed.
3. ✅ **Premium gate is ALREADY ACTIVE** (corrected 2026-06-15). `premium_providers()`
   defaults to `("chatgpt","claude")` even with no config key
   (`services/coverage_profiles.py:70`), and the route enforces it:
   `_maybe_premium_block` returns **402** when a free account
   (`plan_tier == "free"`) requests ChatGPT/Claude. Verified by
   `tests/test_audit_premium_provider_gate.py`. So free → Gemini, paid → may opt
   into ChatGPT — the gate is done.
4. ⚠️ **The real remaining gate is COST, not the tier gate.** ChatGPT grounded is
   ~**100× Gemini's cost** ($1.87 / 10-prompt single-SKU run). Two pieces remain:
   - **Per-merchant cost caps** (the real blocker): cap premium $ per merchant
     per period. Building block exists — `db.llm_probe_runs.cost_today_for_merchant`.
     Enforce at the launch route alongside `_maybe_premium_block` (block/downgrade
     when a paid merchant exceeds their cap). **This is the next concrete C code task.**
   - **Tier-aware default** (NOT a blunt global flip): a global
     `pilot_gemini → us_shopper` flip would 402 every free account. Instead make
     the default profile tier-aware (free → `pilot_gemini`, paid → `us_shopper`)
     so paid merchants get multi-engine automatically — *after* caps are in.
5. *(Next)* add Claude via the `full` profile once Agent-engine support +
   metering are confirmed.

**Observation to confirm:** the validation run executed ~10 prompts/engine, not
the 40 requested — almost certainly the intentional "ChatGPT hero-SKU only" cap
(cost control); confirm it's deliberate, not a silent prompt-count drop.

**Acceptance:** ✅ per-engine citations + metering proven; ✅ premium tier gate
live. Remaining = **per-merchant cost caps**, then the **tier-aware default**.
**Effort:** M (caps) · **Risk:** the economics, not the plumbing — **gate the tier default on cost caps.**

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
1. **A** — frontend build against [`PORTAL_RENDERING_CONTRACT.md`](PORTAL_RENDERING_CONTRACT.md)
   (backend ready; longest pole — start now).
2. **C** — ✅ multi-engine + metering + premium tier gate done; remaining =
   **per-merchant cost caps** (next code task), then the **tier-aware default**.
3. **B** — ✅ suppression done; deeper probe-generation follow-up needs a
   billing/generator decision (see B).
4. **D** — after the C cost caps; gates go-wide.
