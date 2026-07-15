# Report Summary Contract v1 (draft) — 2026-07-15

One condensed, versioned summary layer on top of the AI-readiness / merchant audit report, consumed by **three renderers**: the portal's new 3-page condensed report view, the paid PPT export, and the portal homepage hero module. Built once per audit run; renderers never re-derive conclusions.

> **Provenance caveat:** field names below were audited against a local `pivota-backend` checkout that is **51 commits behind origin/main**. Core shapes (`next_best_action.py`, `sku_opportunity.py`, `audit_projection_builder.py`) are long-standing, but implementation must re-verify each mapping against `origin/main` (known lesson: `outreach_outcomes` exists on origin/main but not on the stale local checkout).

---

## 1. Why a contract

Partner feedback items 1 (evidence under actions), 2 (PPT export), 3 (readable score), and 4 (3-page first view) all consume the *same* condensed shape: score + verdict + top findings + top actions with supporting evidence. Today that shape does not exist — the portal stitches it from `per_sku_reports`, `merchant_narrative.prioritized_actions`, `report.opportunity.per_prompt[]`, etc., and a PPT exporter would have to re-stitch it. Define it once, render it three ways.

## 2. Sources (existing, verified)

| Source | Where | What we take |
|---|---|---|
| Surface A: URL/per-SKU wedge report | `routes/merchant_audit_routes.py` → `_shape_url_audit_response` (`report_jsonb`) | `per_sku_reports`, `brand_rollup`, `merchant_narrative`, `suggested_prompts`, `where_youre_losing`, `outreach_outcomes` (origin/main) |
| Surface B: v3 projection | `services/audit_projection_builder.py` → `build_merchant_projection` | `headline_score`, `verdict`, `findings_summary`, `action_queue`, `evidence_quotes`; FK lattice `action_id → parent_finding_id → finding → evidence.probe_run_id` |
| Scores | audit run row: `visibility_score_avg`, `attribution_score_avg`, `category_visibility_score_avg`; per-SKU: `compute_agent_readiness_score` (`services/agent_readiness_score.py`) → `overall` + axis subscores; per-SKU `scores.*.breakdown.*` as `{points, max}` | raw 0–100 numbers |
| Actions | `services/next_best_action.py` (`build_next_best_action` / `build_sku_next_best_action`); item keys incl. `primary_gap`, `headline`, `why_this_first`, `first_move`, `evidence_used`, `cta`, `secondary_moves` | top actions + embedded justification |
| Prompt results | `services/sku_opportunity.py` → `_score_prompt_group`; item keys incl. `query`, `axis`, `provider_verdicts` (`win|partial|loss|absent`), `who_owns`, `competitors`, `source_summary`, `cited_evidence` | supporting evidence per action |
| Report layout precedent | `services/audit_html_renderer.py` → `render_brand_html_v2` section order | de-facto summary layout to mirror |

**Join keys for action → supporting prompts (honesty-critical):**
1. **Hard link (preferred, Surface B):** `action.parent_finding_id → finding → evidence.probe_run_id → prompt run`.
2. **Embedded (Surface A):** `action.evidence_used.failing_prompts` — the prompts that *actually produced* the action. Use verbatim.
3. **Soft link (fallback):** `axis` match between action's `primary_gap` axes and prompt results.
Never LLM-inferred linkage. If none of the three applies, the action ships with **no** supporting prompts.

## 3. Contract schema

```jsonc
{
  "contract_version": "1.0",
  "audit_run_id": "…",
  "generated_at": "…",                     // ISO; PPT prints this as "data as of"
  "subject": {
    "type": "brand | sku",
    "merchant_id": "…",
    "product_key": null,                    // sku only
    "sku_key": null
  },

  "score": {
    "raw": 42,                              // 0–100, unchanged; delta tracking uses this
    "display": 4.2,                         // raw/10, ONE decimal, never rounded to int
    "scale_max": 10,
    "band": "needs_work | pass | good | excellent",
    "band_thresholds": [6.0, 7.5, 9.0],     // pass/good/excellent cutoffs — pending calibration decision (§7)
    "subscores": [
      { "key": "visibility",   "raw": 42, "display": 4.2 },
      { "key": "attribution",  "raw": 30, "display": 3.0 },
      { "key": "category_visibility", "raw": 55, "display": 5.5 }
    ],
    "delta": { "raw": 5, "previous_audit_run_id": "…" }   // null on first run
  },

  "verdict": {
    "headline": "…",                        // one sentence: can/can't agents recommend you + main blocker
    "primary_gap": "PRIMARY_RETRIEVAL_FOUNDATION",   // enum from next_best_action.py
    "source": "merchant_narrative.headline | projection.verdict"
  },

  "top_findings": [                          // max 3
    {
      "finding_id": null,                    // Surface B id when available
      "title": "…",
      "severity": "…",
      "evidence_summary": "…",               // one short paragraph, pre-written, no renderer re-summarization
      "supporting_prompts": [ /* PromptEvidence, see below */ ]
    }
  ],

  "top_actions": [                           // max 3, ordered; from prioritized_actions / action_queue
    {
      "action_id": null,                     // Surface B when available
      "headline": "…",
      "why_this_first": "…",
      "first_move": "…",
      "expected_outcome": "…",
      "cta": { "action": "…", "target_sku_key": null },
      "supporting_prompts": [ /* PromptEvidence */ ],
      "supporting_prompts_basis": "finding_link | evidence_used | axis_match | none"
    }
  ],

  // PromptEvidence (collapsed-by-default in UI; slide-appendix in PPT):
  // {
  //   "query": "…", "axis": "…",
  //   "provider_verdicts": { "chatgpt": "loss", "claude": "absent" },
  //   "who_owns": "…", "competitors": ["…"],
  //   "conclusion": "…"        // one pre-written sentence: what this probe proved, cited fields only
  // }

  "competitive_snapshot": {
    "top_cited_hosts": ["…"],
    "competitors_named": ["…"],
    "source": "source_summary / cited_evidence aggregates"
  },

  "meta": {
    "providers_probed": ["…"],
    "prompts_probed": 24,
    "methodology_note": "…",                 // measured coverage, never static claims
    "locale": "en | zh | ko"
  }
}
```

### Population rules
- **Pre-written prose, not raw data.** `headline`, `evidence_summary`, `conclusion` are authored at build time (existing narrative builders), so renderers (web/PPT) do zero summarization → identical story on every surface.
- **Caps are hard:** 3 findings, 3 actions, ≤3 supporting prompts per action. Overflow stays in the full report; the summary links down.
- **`supporting_prompts_basis` is mandatory** and rendered nowhere for merchants — it's an internal honesty marker for QA and future audits of the summary layer itself.
- **Truncation is disclosed:** if findings/actions were dropped by the cap, `meta` carries counts (`findings_total`, `actions_total`).

## 4. Score display rules (feedback item 3)

- Internal storage and deltas stay **0–100 raw**. Display is `raw/10` at one decimal — never integer-rounded, or the "do action → score moves" loop dies (42→47 must show as 4.2→4.7).
- Band label is the primary read; the number is secondary. Portal already has `bandFromScore()` in `components/audit/PerSkuReportCard.tsx` — lift into the contract so web and PPT can't diverge.
- **Open calibration question (needs founder/team decision, §7):** "6 = pass" implies a decent merchant scores ~6. Current `visibility_score_avg` is percentile-calibrated (`calibrate_thresholds_from_baseline`, bottom 25 / top 75 in `agent_center_bd_report_service.py`) — good news: anchor tuning is a threshold change, not a rubric rewrite. Red line: no score inflation; adjust anchors + banding, not the measurement.
- Phase 2 (not in v1): peer percentile ("better than 62% of beauty brands") once per-vertical sample sizes justify it.

## 5. Renderer mappings

### 5a. Portal 3-page condensed view (feedback items 1 + 4)
- **Page 1** = `score` + `verdict` (+ `delta` chip).
- **Page 2** = `top_findings` (+ `competitive_snapshot`).
- **Page 3** = `top_actions`; each action's `supporting_prompts` behind a collapsed disclosure (item 1). "View full report" links to the existing panels.
- Portal has **no shared Disclosure/Accordion primitive** (each panel rolls its own `useState`) — build one in `components/ui/` first, use it for both supporting-prompts and the page-2/3 sections.
- Existing components to refit, not rebuild: `PerSkuReportCard`, `PrioritizedActionsPanel`, `PromptEvidencePanel`, `AgenticVisibilityPanels` (current panel order becomes the "full report" tail).

### 5b. PPT export (feedback item 2)
- Slide map: ① cover — brand, score band + display score, "data as of {generated_at}", Pivota branding; ② verdict + competitive snapshot; ③–⑤ one action per slide (`headline` / `why_this_first` / `expected_outcome`); ⑥ methodology + how-to-read (honesty slide).
- Net-new machinery: **python-pptx** (nothing exists; only HTML→PDF via lazy weasyprint in `audit_html_renderer.py`). Endpoint modeled on `cold_start_audit_export` (`routes/agent_center_bd_routes.py`) but gated with `require_approved_merchant` (`routes/billing_routes.py`) + `merchant_is_paid_tier` (`services/credit_consumption_service.py`) — the established `preview_only` pattern from `citation_draft_service.py`.
- Gating shape: **free = single watermarked summary slide** (distribution loop stays alive), **paid = full deck**.

### 5c. Homepage hero (feedback item 5)
- Nav order is single-sourced in `pivota-merchants-portal/lib/merchant-navigation.ts`; promote the AI-readiness group above Primary, feature-flag the Workflows group hidden (don't delete).
- Hero module on `app/dashboard/page.tsx` renders contract `score` + `verdict` + first action.
- **Empty state is the critical path:** no audit yet → hero funnels into URL audit (the wedge). Known dependency: supply-proof button is invisible without `content_key` — re-verify that chain before shipping the reorder.

## 6. Build order

1. **PR-1 (backend):** `services/report_summary_builder.py` — build contract from Surface A + B, stamp `contract_version`, attach to audit response (additive key, e.g. `report_summary`). No UI change; dark.
2. **PR-2 (portal):** shared Disclosure primitive + 3-page condensed view consuming `report_summary`; score display switch (0–10 + band) behind a flag until calibration decision lands.
3. **PR-3 (portal):** nav reorder + homepage hero + empty-state funnel; Workflows behind flag.
4. **PR-4 (backend + portal):** PPT exporter (python-pptx) + paid gate + free watermarked slide.

## 7. Open decisions (blocking items in *bold*)

- **Band thresholds & anchor calibration** — display-only rescale vs. re-anchoring so "6 = pass" is truthful. Owner: founder + audit team. Blocks the un-flagging of the 0–10 display (PR-2 flag flip), not the contract itself.
- Locale strategy for pre-written prose (`meta.locale`): build-time single locale vs. multi-locale authoring.
- Whether `outreach_outcomes` earns a summary slot (e.g. a "what happened since last audit" line) in v1 or waits for v1.1.
