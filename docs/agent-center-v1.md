# Agent Center V1 — design doc

**Status:** V1 foundation in place (this PR). LLM contract + UI + agents 2–5 land in follow-up PRs.

## Why this lives in `pivota-backend`

The first attempt at Agent Center (a.k.a. Demand Test Agent V1) shipped as
`pivota-merchants-portal#7`. That PR was rejected and closed because it bundled
an entire Python service + a SQLite DB binary + a developer-machine deploy
script into a Next.js frontend repo. Vercel doesn't run Python, the schema-fluid
JSONB tables don't belong in SQLite, and design specs co-mingled with code make
it harder to evolve either independently.

This rebuild puts each piece in the place that actually deploys it:

```
pivota-merchants-portal           pivota-backend                PIVOTA-Agent (Node)
─────────────────────────         ──────────────────────────    ───────────────────────
agent-center pages, run UI  ──→   /api/agent-center/* routes ──→ /internal/llm-probe
issues lists, dashboards          state machine + DB (this PR)   geminiGlobalGate
                                  workers + cron                 (existing rate limit /
                                                                  circuit breaker)
                                                                       │
                                                                       ▼
                                                                 Gemini API
```

`pivota-backend` owns the agent state machine and the database. It exposes a REST
surface to the UI and calls into PIVOTA-Agent for LLM work via PIVOTA-Agent's
existing `geminiGlobalGate` (which already deploys, rate-limits, and circuit-breaks
every Gemini call we make).

## Why one schema for all 5 agents

The five agents (Demand Test → SKU Match → Offer Execution → Checkout
Verification → GMV Attribution) share more than they differ:

- All take a merchant + store as input
- All run against a "scan target" (one job)
- All produce **issues** that need to be remediated
- All emit **usage events** for audit / billing
- Most issues end up needing a **resolution plan** with an owner and approval
  state

Building five copies of those five concepts produces five drift surfaces. So V1
puts the shared bones in one set of `agent_center_*` tables; each agent only
adds its own diagnoses payload (and, eventually, its own per-agent diagnoses
table) on top.

## Tables (migration `db/migrations/067_agent_center_v1.sql`)

| Table | Purpose | Owner agents |
|---|---|---|
| `agent_center_merchant_stores` | merchant ↔ store binding | all |
| `agent_center_scan_targets` | one row per agent run; `scan_mode` says which agent | all |
| `agent_center_issues` | shared blocker queue | all |
| `agent_center_issue_resolution_plans` | one plan per issue; deterministic per blocker_type | resolution workflow |
| `agent_center_usage_events` | shared audit + idempotency meter | all |
| `agent_center_production_validation_runs` | smoke / pilot validation envelope | internal ops |
| `agent_center_demo_fixtures` | internal-only seed data (gated by `ENABLE_INTERNAL_DEMO_FIXTURES`) | internal ops |

Each table:
- Uses `TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()`
- Has a `payload JSONB NOT NULL DEFAULT '{}'` for agent-specific data
- Soft-deletes via `deleted_at IS NULL` (where applicable)
- Has an `updated_at` BEFORE-UPDATE trigger
- Indexed on `merchant_id`, `store_id`, `scan_target_id`, `status`, `created_at DESC`

`scan_mode` and the `*_status` columns use CHECK constraints so DB rejects
unknown values; corresponding tuples in `services.agent_center_service`
(`ALL_SCAN_MODES`, `SCAN_TARGET_STATUSES`, `ISSUE_STATUSES`, etc.) are the single
source of truth for the application layer.

## Issue resolution workflow

State machine (matches the spec in the closed merchants-portal#7 branch):

```
   open
    │ (assignee picks up)
    ▼
   assigned ────────────► rejected
    │                       (owner decides not to act)
    │ (work needs merchant action)
    ▼
   waiting_merchant_approval
    │ (merchant approves)
    ▼
   in_progress
    │
    ▼
   ready_for_retest
    │ (re-run agent against same context)
    ▼
   retesting
    │
    ├─► resolved
    └─► open  (re-run found a new gap; new plan created)

   ignored                  (manual override — recorded but not actioned)
```

Owner-type routing (set on the resolution plan, default per blocker_type):

| Blocker type (examples) | Owner |
|---|---|
| `merchant_store_attribution_gap`, `missing_attribute` | `shared` (merchant + Pivota collaborate) |
| `pivota_pdp_attribution_gap`, `pivota_pdp_readiness_gap`, `unverified_pivota_attribution` | `pivota_ops` |
| `checkout_url_unreachable`, `coupon_param_missing` | `pivota_eng` |
| Unknown / ambiguous | `human_review` |

## Per-agent contract

What each agent writes:

| Agent | scan_mode values | Issue types it creates | usage_event.agent_type | usage_event.workflow_type |
|---|---|---|---|---|
| 1. Demand Test | `open_product_visibility_test`, `merchant_store_attribution_test`, `pivota_pdp_attribution_test`, `search_grounded_product_discovery_test` | `ai_visibility_loss`, `competitor_substitution`, `merchant_store_attribution_gap`, `pivota_pdp_attribution_gap`, `unverified_pivota_attribution`, `missing_attribute`, `pivota_pdp_readiness_gap` | `demand_test` | `open_product_visibility` / `merchant_store_attribution` / `pivota_pdp_attribution` / `search_grounded_product_discovery` |
| 2. SKU Match | `sku_match` | `sku_mismatch`, `price_drift`, `inventory_mismatch`, `variant_mapping_issue`, `missing_offer` | `sku_match` | `sku_match_readiness` |
| 3. Offer Execution (PSP-blocked) | `offer_execution` | `offer_not_attached_to_pivota_pdp`, `expired_coupon`, `promo_mismatch`, `price_mismatch` | `offer_execution_agent` | `offer_readiness` |
| 4. Checkout Verification (PSP-blocked) | `checkout_verification` | `checkout_url_unreachable`, `stale_checkout_session`, `variant_param_missing`, `coupon_param_missing`, `checkout_domain_mismatch`, `checkout_not_attached_to_pivota_offer` | `checkout_verification_agent` | `checkout_readiness` |
| 5. GMV Attribution (PSP-blocked) | `gmv_attribution` | `attribution_link_missing`, `multi_merchant_split_unresolved` | `gmv_attribution_agent` | `gmv_readiness` |

V1 (this PR) implements only the schema + the Demand Test stub runner. Agents
2–5 add their own logic (and, when payloads grow large, their own
`agent_center_<n>_diagnoses` tables) in follow-up PRs.

## LLM contract (placeholder for the next PR)

The Demand Test stub runner currently records mock usage events instead of
calling Gemini. The actual contract — how `pivota-backend` asks PIVOTA-Agent to
run a Gemini probe, the prompt template, the structured-output parser, the
preflight URL verification rules — is decided in a separate PR (`decision-point #2`
from the architecture menu). Until then, `PIVOTA_AGENT_CENTER_MOCK_GEMINI=true`
is the V1 default.

## Auth model

- **V1** (this PR): `Depends(get_current_employee)` on every `/api/agent-center/*`
  route. Internal pilot only; no merchant has direct access yet.
- **V1.5**: still employee-gated, but the demand-test stub flips over to real
  Gemini calls via PIVOTA-Agent.
- **V2**: merchant-gated routes added; `billing_mode` on usage events flips from
  `preview_only` to `metered` for paid tiers. Real merchant pilot launches here.

## Rollout plan

1. Apply migration via the new admin endpoint:
   `POST /admin/migrations/apply-agent-center-v1` (admin-only).
2. Verify with `GET /admin/migrations/verify-agent-center-v1`.
3. Smoke-test the demand-test endpoints against a known merchant + store. The
   stub runner closes the loop end-to-end so deploy validation doesn't depend
   on Gemini.
4. Land the LLM contract PR (decision #2). Flip
   `PIVOTA_AGENT_CENTER_MOCK_GEMINI=false` once that PR is in production.
5. Land the merchants-portal UI PR (consumes these routes via REST).
6. Repeat per follow-up agents 2–5.

## What's intentionally NOT in V1

- **Real Gemini calls.** Stub runner only.
- **The merchants-portal UI.** Separate PR.
- **Per-agent diagnoses tables** (`agent_center_offer_execution_diagnoses` etc.).
  Each agent's PR adds its own when needed.
- **Merchant-facing auth + billing.** V1 is internal pilot via employee auth,
  `billing_mode='preview_only'` on every usage event.
- **Workers / cron / state-machine background processing.** V1 uses
  FastAPI `BackgroundTasks` for the stub run; long-running orchestration moves
  to dedicated workers when real Gemini lands.
- **The other 4 agents' actual logic.** Their `scan_mode` values are reserved
  in the CHECK constraint so future migrations don't have to ALTER it, but no
  agent code has been written yet.

## Verification

This PR ships with `tests/test_agent_center_service.py` covering:
- Validation of bad inputs (unknown scan_mode / status / severity / missing
  required ids) → `ValueError`
- `record_usage_event` first-write-wins idempotency
- Scan target + issue + resolution-plan happy-path round trips
- Demand-test stub runner walking `queued → running → stub_complete` with
  exactly one usage event and one synthetic issue per run
- Stub runner is replay-safe (re-running against `stub_complete` does not
  double-count usage events)
- Stub runner refuses to start against a `running` row

End-to-end smoke (manual, after migration applied):

```bash
# 1. Apply migration
curl -X POST $BACKEND/admin/migrations/apply-agent-center-v1 -H "Authorization: Bearer $ADMIN_JWT"

# 2. Verify
curl $BACKEND/admin/migrations/verify-agent-center-v1 -H "Authorization: Bearer $ADMIN_JWT"

# 3. Create a demand-test scan target
curl -X POST $BACKEND/api/agent-center/demand-tests \
  -H "Authorization: Bearer $EMPLOYEE_JWT" \
  -H "Content-Type: application/json" \
  -d '{"merchant_id":"m1","store_id":"s1","scan_mode":"pivota_pdp_attribution_test"}'
# → returns scan_target_id

# 4. Kick the stub runner
curl -X POST $BACKEND/api/agent-center/demand-tests/$SCAN_TARGET_ID/run \
  -H "Authorization: Bearer $EMPLOYEE_JWT"

# 5. Read it back; status should be `stub_complete`, with 1 issue attached
curl $BACKEND/api/agent-center/demand-tests/$SCAN_TARGET_ID \
  -H "Authorization: Bearer $EMPLOYEE_JWT"
```
