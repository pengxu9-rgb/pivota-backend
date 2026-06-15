# ADR-003: Unify the merchant AI-Commerce-Readiness audit on the async per-SKU lifecycle to deliver the Fix 2/3 narrative

**Status:** Proposed · **Date:** 2026-06-15 · **Owners:** Agent Center / Merchant Audit
**Repos:** `pivota-backend`, `pivota-merchants-portal-audit-render`

> Authored from a code-verified cross-repo analysis. All claims cite `file:line`.
> Builds on Fix 1 (#888), Fix 2 (#894), Fix 3 (#895) and the portal contract
> (`docs/PORTAL_RENDERING_CONTRACT.md`).

## Context

The audit tells a merchant whether AI shopping agents find and recommend their
products. Fix 1/2/3 made it decision-grade: real cited hosts, a findability-vs-
endorsement split, and a 7-section `merchant_narrative`. Those outputs
(`merchant_narrative` + `authority_map`) are produced **only** by the per-SKU
branch of `run_brand_report` and returned in its report dict
(`services/agent_center_bd_report_service.py:8505-8544`). The portal rendering
layer is built (`components/audit/MerchantNarrativePanel.tsx`; types in
`lib/types/ai-readiness.ts:438-576`) but **never mounted** in the page.

**Blocker:** there are two divergent audit paths and the portal is on the wrong one.

### Two audit paths (current state)

**Path A — legacy synchronous endpoint (what the portal calls today)**
- `POST /api/merchant-center/audit/ai-commerce-readiness`
  (`routes/merchant_audit_routes.py:503`), `Depends(get_current_merchant)`.
- Calls `run_brand_report(...)` with **no `audit_mode` → defaults to `"legacy"`**
  (`agent_center_bd_report_service.py:8315`); returns
  `{ brand_report: { per_product, aggregate, cross_product_competitors, failed }, … }`
  (`routes/merchant_audit_routes.py:962-976`). **No `merchant_narrative`/`authority_map`.**
- **Deprecated:** emits `Deprecation: true` + `Sunset: Sat, 01 Nov 2026` +
  `Link: </api/audits>; rel="successor-version"` (lines 531-537).
- `?via=async_pipeline` shim polls 5s (`_COMPAT_POLL_BUDGET_SECONDS`, line 326 —
  docstring stale at "30s") but **still launches legacy mode**, so it does not
  produce the narrative.
- Portal client: `lib/api-client.ts:1238-1243` (180s timeout); renders only
  `brand_report` (`page.tsx:207,384`); the narrative panel is **not imported**.

**Path B — async per-SKU pipeline (what produces the narrative)**
- `POST /api/audits` (`routes/audit_runs_routes.py:855`), **also
  `Depends(get_current_merchant)` — merchant-accessible, same auth as Path A.**
  Accepts `platform:source_product_id` composites; enqueues with
  `launch.audit_mode="per_sku"` (line 1094). Full pre-flight: premium-provider
  gate, verified-payment-method, credit gaps, free-tier rate limit (lines 957-1045).
- Worker (`services/audit_run_worker.py`) runs `run_brand_report(audit_mode="per_sku")`
  (lines 487-505) and persists the full `report_jsonb` incl. `merchant_narrative`
  + `authority_map` (lines 1352-1395). Lifecycle:
  `queued→discovering→probing→scoring→materializing→verifying→completed|failed`.
  Probing is LLM-heavy (can run >15 min; lease heartbeat every 5 min, line 394-405).
- Read back via `GET /api/audits/{run_id}` (line 1251, merchant-accessible),
  returning `report_jsonb`; `sanitize_report_for_merchant` strips only score
  `breakdown` blocks, so the narrative survives (`agent_center_bd_report_service.py:4712-4725`).

**Employee/cron (do NOT disturb):** `routes/agent_center_bd_routes.py`
(`get_current_employee`, legacy mode, line 322-330) and
`jobs/scheduled_audit_job.py` (legacy mode, line 373-381) are the real remaining
consumers of `run_brand_report(audit_mode="legacy")`. The Sunset applies to the
merchant **sync endpoint**, not the legacy engine branch.

### Why the two models can't be bridged

| | Legacy | Per-SKU |
|---|---|---|
| Top-level | `per_product[]`, `aggregate`, `cross_product_competitors`, `failed` | `per_sku_reports[]`, `brand_rollup`, `authority_map`, `merchant_narrative`, `verify_summary`, `cost_summary` |
| Score axes | `visibility`, `attribution`, `category_visibility` (`:8938-8946`) | `identity`, `content_richness`, `routability`, `citation` (`:4787-4804`) |
| Per-item unit | product (`pdp_url`) | SKU (`sku_key`) |

The axes don't map and the per-item key changes (product→SKU). A per-SKU→legacy
adapter would have to fabricate the legacy axes — violating the narrative
builder's own no-fabrication/no-inflation guardrail. Running per-SKU
synchronously times out (the 5s compat poll + 15-min worker lease exist for this
reason) and bypasses the credit/premium/cost-cap governance on the async path.

## Forces / constraints

1. Per-SKU is heavy and inherently async (~40 prompts/SKU × up to 50 SKUs).
2. Cost + premium gating live **only** on Path B: free=Gemini; ChatGPT/Claude
   premium (`_maybe_premium_block` `routes/audit_runs_routes.py:411-444`;
   `coverage_profiles.py:70-77`); per-merchant **$5/day** + global $200 cost cap
   (`services/llm_providers/orchestrator.py:28-32`). The sync endpoint has only a
   2/24h quota and **no** credit/premium gating.
3. The legacy report shape still backs the employee BD portal + cron — keep the
   legacy engine branch.
4. Mock-data guard must survive (sync `_detect_mock_per_product`
   `merchant_audit_routes.py:220-253`; worker `_detect_mock_audit_output`
   `audit_run_worker.py:527-561`).
5. The legacy `per_product.merchant_view` carries merchant-valued surfaces (GSC
   submission tracking, indexing-arc) — parity must be checked (Open Q1).
6. The rendering layer + types are already built; remaining portal work is the
   run→poll→render lifecycle, not the panel.

## Options

**Option 1 (recommended) — migrate the portal to the async per-SKU lifecycle;
retire only the merchant sync endpoint.** Point the page at `POST /api/audits` →
poll `GET /api/audits/{run_id}` → render `merchant_narrative`/`authority_map` via
the built panel. Keep `audit_mode="legacy"` for employee/cron. *Effort:* medium,
portal-concentrated. *Risk:* low-moderate (Path B already merchant-accessible,
credit-gated, idempotent, crash-resumable). *Trade:* audits become async/minutes;
cost rises (governed); two report shapes coexist during transition.

**Option 2 — unify everything on per-SKU; migrate employee portal + cron, delete
`audit_mode="legacy"`.** Correct long-term end-state but high effort/risk (touches
employee tooling, cron, an unbuilt employee-auth per-SKU projection
`routes/audit_runs_routes.py:1298-1318`). **Defer as a follow-on; do not couple to
the merchant unblock.**

**Option 3 — hybrid: keep legacy "quick look" + add a per-SKU "deep audit".**
Two score models in one UI is confusing and worst-cost; the free async URL-wedge
(`/api/merchant-center/audit/url-readiness`) arguably already fills the
quick-free-look niche. Defensible only with a strong product reason.

## Decision

**Adopt Option 1.** Migrate the merchant portal's AI-Readiness page to the async
per-SKU lifecycle; render the narrative via the existing `MerchantNarrativePanel`.
Keep `run_brand_report(audit_mode="legacy")` for employee BD + cron. Retire the
merchant **sync** endpoint on/after Sunset (2026-11-01), gated on telemetry
showing the portal no longer calls it. Treat full unification (Option 2) as a
separate follow-on.

**Rationale:** Path B is already merchant-accessible under identical auth, already
produces the narrative, and already carries the cost/premium governance. The
remaining work is overwhelmingly portal lifecycle plumbing + mounting the
already-built panel — the shortest honest path from "merchants can't see Fix 2/3"
to "they can," without the lossy-adapter or sync-timeout traps, and without
destabilizing employee tooling.

## Consequences

**Positive:** merchants see the narrative + findability/endorsement split;
merchant audits inherit credit metering, premium gating, the $5/day cap,
idempotency, cancellation, resume-on-crash; one canonical merchant path; the
deprecated sync endpoint can finally be retired.

**Negative:** merchant audits become async/slower (portal must show real progress
from `partial_result_jsonb`); per-audit cost rises (governed, but free-tier UX must
explain "Gemini-only free"); two report shapes coexist during transition;
merchant-valued legacy surfaces must be confirmed present in the per-SKU report or
explicitly deferred (Open Q1).

## Migration plan (phased)

- **Phase 0 — decide/de-risk:** confirm per-SKU `report_jsonb` carries the
  merchant-valued legacy surfaces merchants act on (GSC tracking, indexing-arc) —
  Open Q1; if missing, file follow-ups, don't block. Fix the stale
  `_run_async_pipeline_compat` docstring; decide its fate (launches legacy → retire
  with the sync endpoint).
- **Phase 1 — backend (additive):** verify `GET /api/audits/{run_id}` (no
  `?audience`) returns `merchant_narrative`+`authority_map` post-sanitize;
  optionally surface them in the `?audience=merchant` projection.
- **Phase 2 — portal behind flag `ai_readiness_async`:** add client methods
  (`createAudit({merchant_id:'self', sku_keys, coverage_profile, prompts_per_sku})`,
  `previewAudit`, `getAuditRun`, `listAuditRuns`, `cancelAuditRun`); implement
  run→poll→render in `page.tsx` (stage from `partial_result_jsonb`; poll every
  3–5s w/ backoff; mount `<MerchantNarrativePanel>`); surface credit/premium UX
  from `POST /api/audits/preview` + the structured 402 paywall; keep legacy
  history read-only.
- **Phase 3 — rollout:** enable for pilot merchants; validate cost ($5/day cap +
  `cost_summary_jsonb`), latency, narrative correctness on real prod data; default on.
- **Phase 4 — retire the sync endpoint:** once portal traffic is gone and Sunset
  passed, return `410 Gone`. **Do not** remove `audit_mode="legacy"` (employee +
  cron still need it — that's Option 2, tracked separately).

## Open questions

1. **Merchant-view parity:** does the per-SKU `report_jsonb` carry the GSC-tracking
   / indexing-arc surfaces the legacy `merchant_view` had? (Not verified from code —
   check before defaulting the flag on.)
2. **Free-tier per-SKU cost:** is 2/24h + $5/day enough, or does free tier need a
   reduced `prompts_per_sku` / SKU cap?
3. **Polling UX:** audits run minutes; confirm poll/backoff + "safe to leave" UX +
   stage display.
4. **`?via=async_pipeline` shim:** launches legacy (no narrative), stale docstring —
   retire with the sync endpoint?
5. **Historical legacy runs:** render read-only or hide post-migration (two score
   models in one UI is a confusion risk)?
6. **Employee BD + cron (Option 2):** when/if to migrate and delete
   `audit_mode="legacy"` — out of scope here.
