# Frontend Handoff — APM + Performance Marketing

**Audience:** Claude Design (and downstream Claude Code that implements the specs).

**Source:** Backend implementation completed in PRs #414–#427 (Pivota APM build, May 2026).

**Scope:** This doc captures the new API surfaces, data shapes, and UX intents so Claude Design can produce visual specs without re-reading the backend code.

---

## Where each surface lives

| Surface | Portal | Audience |
|---|---|---|
| Audit trend dashboard | merchant portal | merchant operator |
| Cohort comparison view | employee portal | BD operator |
| Executor agent activity feed | merchant portal + employee portal | both |
| Funnel chart | merchant portal | merchant operator |
| Task queue | merchant portal + employee portal | both (BD assigns to merchant) |

---

## 1. Audit trend dashboard (PR-1a + PR-1b)

### What it shows
Time-series of merchant's audit scores so they see "AI visibility +12 over 14 days" instead of just today's snapshot.

### API
```
GET /api/merchant-center/audit/history?limit=N
```

Response shape:
```json
{
  "merchant_id": "...",
  "runs": [
    {
      "run_id": "uuid",
      "requested_at": "2026-05-09T...",
      "completed_at": "2026-05-09T...",
      "status": "succeeded",
      "verdict_labels": ["PARTIAL"],
      "visibility_score_avg": 33,
      "attribution_score_avg": 45,
      "category_visibility_score_avg": 22,
      "audited_via_pivota_canonical_count": 0
    }
  ],
  "rate_limit": {"max": 2, "window_seconds": 86400, "used_in_window": 1}
}
```

Per-audit-detail view also has trend deltas embedded inline at `merchant_view.tracking.history`:
```json
{
  "audits_in_history": 5,
  "most_recent_audit": {"run_id": "...", "requested_at": "...", "visibility": 21, "attribution": 50},
  "delta_from_most_recent": {
    "visibility": 12,            // current minus most_recent
    "attribution": -5,
    "category_visibility": null, // null when current didn't measure it
    "days_since_last_audit": 14
  },
  "series": [                    // sparkline data, oldest → newest
    {"requested_at": "...", "visibility": 21, "attribution": 50, "category_visibility": 18},
    ...
  ]
}
```

### Render intent
- Sparkline mini-chart per metric (visibility / attribution / category)
- Delta badge ("+12 over 14 days") next to each score, color-coded green/red
- "Schedule re-audit" toggle that flips `catalog_merchants.audit_schedule` between 'none' / 'weekly' / 'monthly' (no API endpoint for this yet — admin sets via SQL today; UI control would need a backend PUT route added)

### Empty state
First audit ever for this merchant: `delta_from_most_recent: null`. Render scores without deltas + a "Run another audit in 7 days to see trend" hint.

---

## 2. Cohort comparison (PR-2 + PR-2b + PR-2c)

### What it shows
When BD operator audits a brand with `audit_competitors=true`, they get auto-audits of the top-N competitor brands plus a cross-brand mention matrix showing who Gemini cites in this category.

### APIs

Trigger (existing endpoint, opt-in):
```
POST /api/agent-center/bd/cold-start-audit
body: {
  url: "...",
  audit_competitors: true,        // PR-2 opt-in
  cohort_size: 3                  // 1-5
}
```

Response includes:
```json
{
  "cohort": {
    "queued": true,
    "competitor_brands": ["MegaFood", "Nordic Naturals"],
    "category_override": "daily gummy vitamins",  // PR-2c
    "parent_audit_run_id": "uuid",
    "poll_url": "/api/agent-center/bd/cohort/{uuid}"
  }
}
```

Poll for results + comparison:
```
GET /api/agent-center/bd/cohort/{parent_audit_run_id}?include_comparison=true
```

Response:
```json
{
  "parent_audit_run_id": "...",
  "cohort_size": 2,
  "completed_count": 2,
  "still_running": 0,
  "runs": [
    {
      "competitor_brand": "Nordic Naturals",
      "competitor_domain": "nordic.com",
      "status": "succeeded",
      "visibility_score_avg": 0,
      "attribution_score_avg": 0,
      "category_visibility_score_avg": 0
    },
    ...
  ],
  "comparison": {
    "summary": {
      "parent_brand": "Grüns",
      "competitors_audited": 2,
      "brands_named_across_audits": 25,
      "queries_total": 9,
      "category_override_applied": true,
      "category_used": "daily gummy vitamins"
    },
    "brand_mention_matrix": {
      "audits": ["Grüns", "Nordic Naturals", "MegaFood"],
      "matrix": [
        {
          "brand": "MegaFood",
          "brand_lower": "megafood",
          "total_mentions": 12,
          "by_audit": {"Grüns": 6, "Nordic Naturals": 6, "MegaFood": 0},
          "audit_count": 2     // mentioned in 2 of 3 audits
        },
        ...sorted by total_mentions descending
      ]
    },
    "per_query_breakdown": [
      {
        "brand": "Grüns",
        "product_title": "Daily Gummies",
        "query": "best daily gummy vitamins 2026",
        "self_report_yes": false,
        "cited_urls_count": 1,
        "matched_in_grounding": false,
        "top_cited_url": "https://healthline.com/...",
        "top_cited_url_was_redirector": false
      },
      ...
    ],
    "caveat": "All N cohort competitors were audited under the parent's category ('daily gummy vitamins') — the brand_mention_matrix is a true apples-to-apples comparison..."
  }
}
```

### Render intent
- **Polling pattern**: hit cohort endpoint without `include_comparison` while `still_running > 0` (small payload), switch to `include_comparison=true` when `still_running == 0`
- **Brand mention matrix as table**: brand name | total mentions | per-audit columns | small bar chart of audit_count
- **Caveat** rendered prominently when `category_override_applied=true` — frames the comparison as apples-to-apples
- **Honest empty/failure states**:
  - `cohort.queued=false` with reason "no competitor brands extracted from audit"
  - per-cohort-run `status="failed"` with `error_message="domain_resolution_failed"` etc.

---

## 3. Executor agent activity feed (PR-4a/b/c)

### What it shows
"What Pivota did for you this week" — the executor agents that fired automatically after audits.

### API
```
GET /api/merchant-center/executor-runs?agent_name=...&limit=N
```

Query params:
- `agent_name` — filter to one agent (`gsc_url_submission_loop`, `sitemap_freshness_monitor`, `content_brief_generator`). Omit for all.
- `limit` — 1-100, default 20.

Response:
```json
{
  "merchant_id": "...",
  "agent_name": "gsc_url_submission_loop|null",
  "count": 5,
  "runs": [...see shape below]
}
```

Per-run shape (from `db/executor_runs.recent_runs_for_merchant`):
```json
[
  {
    "run_id": "uuid",
    "agent_name": "gsc_url_submission_loop",
    "merchant_id": "...",
    "parent_audit_run_id": "uuid|null",
    "requested_at": "...",
    "completed_at": "...",
    "status": "succeeded",
    "evidence": {
      "candidates_total": 12,
      "submits_attempted": 12,
      "succeeded_count": 8,
      "failed_count": 4,
      "results": [...]
    },
    "error_message": null
  }
]
```

### Three agent evidence shapes

**`gsc_url_submission_loop`** — Pivota submitted URLs to Google Indexing API:
```json
{
  "candidates_total": 12,
  "submits_attempted": 12,
  "succeeded_count": 8,
  "failed_count": 4,
  "skipped_for_throttle": 0,
  "results": [{"url": "...", "status": "submitted|error", "message": "..."}]
}
```

**`sitemap_freshness_monitor`** — diff vs published sitemap:
```json
{
  "merchant_host": "acme.co",
  "sitemap_url": "https://acme.co/sitemap.xml",
  "catalog_url_count": 742,
  "sitemap_url_count": 510,
  "missing_from_sitemap_count": 232,
  "orphan_in_sitemap_count": 0,
  "missing_from_sitemap_sample": ["https://acme.co/products/...", ...],   // top 20
  "orphan_in_sitemap_sample": [...],
  "freshness_score": 0.78,                                                // 0-1
  "severity": "high"                                                       // low | medium | high
}
```

**`content_brief_generator`** — Markdown briefs for failed category queries:
```json
{
  "candidate_queries_total": 3,
  "briefs_generated": 3,
  "briefs_failed": 0,
  "briefs": [
    {
      "target_query": "best daily gummy vitamins 2026",
      "suggested_title": "The 7 Best Gummy Vitamins of 2026",
      "suggested_word_count": 1500,
      "outline_h2_sections": ["What to Look For", "Top Picks", "How We Tested"],
      "key_talking_points": ["Most gummies contain 5-10g of added sugar", ...],
      "competitor_articles": [{"title": "...", "publication": "Healthline", "topics_covered": "..."}],
      "differentiation_angle": "..."
    }
  ],
  "failures": [{"query": "...", "reason": "gemini_call_failed_or_unparseable"}]
}
```

### Render intent
- Activity log in reverse chronological order
- Per-agent-type custom renderer (sitemap diff is a counter, content brief is a card with outline preview)
- "Pivota did 8 things for you this week" headline count
- Honest failure surfaces ("Sitemap unreachable: HTTP 404 — fix at sitemap.xml")

---

## 4. Funnel chart (PR-5)

### What it shows
Stage-level conversion per channel: impression → click → conversion drop-off.

### API
```
GET /api/merchant-center/funnel?channel=ai_agent&window_days=30
```

Response:
```json
{
  "merchant_id": "...",
  "source_channel": "ai_agent",         // or null = all channels
  "window_days": 30,
  "total_events": 1234,
  "stages": [
    {"stage": "impression",   "count": 1000, "conversion_to_next": 0.30, "drop_off_pct": 0.70},
    {"stage": "profile_visit","count": 0,    "conversion_to_next": null, "drop_off_pct": null},
    {"stage": "click",        "count": 300,  "conversion_to_next": 0.83, "drop_off_pct": 0.17},
    {"stage": "pdp_view",     "count": 250,  "conversion_to_next": 0.20, "drop_off_pct": 0.80},
    {"stage": "add_to_cart",  "count": 50,   "conversion_to_next": 0.50, "drop_off_pct": 0.50},
    {"stage": "conversion",   "count": 25,   "conversion_to_next": null, "drop_off_pct": null}
  ],
  "channel_breakdown": [
    {"source_channel": "ai_agent", "total_events": 1000},
    {"source_channel": "direct",   "total_events": 234},
    ...
  ]
}
```

### Channel enum
`ai_grounded_search | ai_agent | social_own | social_kol | editorial | seo_organic | retail | direct | unknown`

### Stage enum
`impression | profile_visit | click | pdp_view | add_to_cart | conversion` (canonical order)

### Render intent
- Sankey or vertical-bar funnel chart with one bar per stage
- `conversion_to_next` as label between stages (e.g. "30%" between impression and click)
- `count: 0` stages still appear (canonical order) — hide visually or render as collapsed bar
- Channel picker dropdown driven by `channel_breakdown`
- "No events tracked yet" empty state when `total_events: 0`

### Honesty caveat to surface in UI
Channel inference is heuristic. Source: backend's `services/funnel_recorder.py.infer_source_channel`. UTM params + endpoint paths drive the inference; real cross-channel attribution would need richer tracking we don't have.

---

## 5. Task queue (PR-6)

### What it shows
Merchant's open work — tasks materialized from audit action_items + executor agent emissions. Status lifecycle: pending → in_progress → done | dismissed | failed.

### APIs

List tasks:
```
GET /api/merchant-center/tasks?status_filter=pending,in_progress&limit=50
```
Default filter is open work. Pass `status_filter=all` for everything; pass `done,dismissed` for archive view.

Response:
```json
{
  "merchant_id": "...",
  "count": 7,
  "tasks": [
    {
      "task_id": "uuid",
      "merchant_id": "...",
      "parent_audit_run_id": "uuid|null",
      "source_executor_run_id": "uuid|null",
      "lever": "gsc_integration",
      "severity": "high",                  // critical | high | medium | low
      "title": "Connect Google Search Console",
      "body": "Grant Pivota Search Console access so we can submit URLs to Google's indexing API on your behalf.",
      "status": "pending",
      "assigned_to_agent": "gsc_url_submission_loop|null",
      "assigned_to_human": null,
      "evidence": {"priority_order": 1, "cta_url": "/onboarding/gsc", "cta_label": "Grant access"},
      "created_at": "...",
      "updated_at": "...",
      "completed_at": null,
      "dismissed_reason": null
    }
  ]
}
```

Update status:
```
PATCH /api/merchant-center/tasks/{task_id}
body: {
  "status": "in_progress",                 // or "done" | "failed"
  "assigned_to_human": "alice@merchant.com",
  "evidence": {...optional context}
}
```

Dismiss:
```
POST /api/merchant-center/tasks/{task_id}/dismiss
body: {"reason": "Already done outside Pivota; we manually submitted via GSC dashboard"}
```

### Render intent
- Kanban board (pending / in_progress / done) with severity-colored cards
- Filter chips: by severity, by lever, by status
- Inline action: "I did this" → PATCH status='done'; "Not relevant" → POST dismiss with reason prompt
- Card body supports markdown (content brief tasks have multi-paragraph body with bullet outlines)
- Audit drill-down: clicking parent_audit_run_id navigates to the audit detail
- Executor evidence drill-down: clicking source_executor_run_id shows the agent's full evidence_jsonb

### Cold-start framing
Tasks created from a cold-start prospect audit (synthetic merchant_id `prospect_<hash>`) skip Phase 0 `pivota_integration` lever — those are pitch material in the report's `pivota_value_prop` section, not work for the prospect.

---

## Cross-cutting design constraints

### Honest empty states
Every surface should handle "no data yet" gracefully:
- Trend: "Run another audit to see trend" (not just blank chart)
- Cohort: "Cohort still running — N of M complete"
- Funnel: "No tracked events yet — visit the [docs link] to instrument your traffic"
- Tasks: "Nothing on your plate — your audit didn't surface action items"

### Severity color map
Used by tasks + sitemap freshness + executor agent results:
- `critical` → red
- `high` → orange
- `medium` → amber
- `low` → slate/gray

### Score bands
For visibility / attribution / category_visibility scores (0-100):
- 0-30: red ("invisible")
- 31-60: amber ("partial")
- 61-100: green ("strong")

### Don't editorialize numbers
The audit pipeline + executor agents are deliberately honest about confidence. The UI should render `null` (or "n/a") when a field is genuinely unknown — never default to 0 or fabricate. Backend's responses preserve null vs zero distinction precisely.

---

## What's NOT covered (deferred / out of scope)

- **Multi-LLM (PR-3)**: the `scores_by_provider` field and `providers` request param aren't wired yet — needs upstream PIVOTA-Agent (Node) work. Don't design for ChatGPT / Claude scores until the backend supports them.
- **Cross-channel attribution model**: the funnel surfaces stage counts; "channel X drove conversion Y" multi-touch attribution is a future milestone (post-PR-5 once 90+ days of data accumulate).
- **Scheduled re-audit toggle UI**: backend has the column (`catalog_merchants.audit_schedule`), no PUT endpoint yet. Either Claude Design specs the UI control + I add the endpoint, or we defer.

## Funnel data quality — known gaps (May 2026 backfill)

After backfilling 90 days of order_events into funnel_events (2,505 rows), two gaps were diagnosed and need follow-up before the funnel chart can render meaningful BD value:

### Gap 1: AI agent impression events are not captured
- `api_call_events` table is **empty across all 90 days** in production. `log_api_call` is wired into ONE route (`/products/v2/{merchant_id}`) which has zero hits.
- Real high-traffic agent-facing routes (`agent_search_products` in `routes/agent_api.py:4492` — handles `/agent/v1/.../products/search`) do NOT call `log_api_call`. This means agent search impressions never reach the funnel.
- Net effect today: funnel shows conversions (orders) but no upstream impressions to compute conversion-from-search rates.
- **Recommended PR**: instrument `agent_search_products` to fire `background_tasks.add_task(log_api_call, ...)` at each of its 8 return points (or via a `/agent/*` middleware). Risk: critical-path function; changes need staging validation. Not done in PR #429 — defer to focused PR with proper review.

### Gap 2: All backfilled events are `source_channel: unknown`
- Production `order_events.metadata` does not contain `utm_source` / `source` / `referrer` fields. The funnel_recorder correctly falls back to `unknown` rather than guessing.
- To get richer channel attribution, the order create / payment intent paths need to start passing utm context through to `log_order_event(metadata=...)`.
- **Recommended PR**: thread utm context from frontend → checkout payload → order metadata. Touches `routes/agent_api.py` order-create paths. Coordinate with merchant portal team for the frontend half.

Both gaps are honest data-pipeline issues — the funnel infrastructure is sound, the instrumentation surface is incomplete. The funnel chart UI can ship against current data (showing the conversion-only side honestly) and improve as instrumentation lands.

---

## Backend reference paths

For Claude Code implementing these specs:
- API contract: `routes/merchant_audit_routes.py` + `routes/agent_center_bd_routes.py`
- Type shapes: `db/funnel_events.py` (SOURCE_CHANNELS, STAGES), `db/merchant_tasks.py` (VALID_STATUSES, VALID_SEVERITIES), `db/merchant_audit_runs.py`
- Pure renderers / projections: `services/cohort_comparison.py`, `services/funnel_analytics.py.STAGE_ORDER`
- Executor evidence shapes: `services/executor_agents/{gsc_url_submission,sitemap_freshness,content_brief}.py`
