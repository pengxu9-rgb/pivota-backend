# Product Optimization Backend Architecture

## Verdict

As of March 19, 2026, the backend is ready for a `readiness-first workspace v1`, but it is not yet a complete optimization operating system.

The most accurate description of the current system is:

- `diagnosis` is production-real
- `planning / prioritization` is real but still compact
- `merchant-safe projection` is real
- `execution` is still fragmented across older product and integration surfaces
- `verification` exists, but its action model is not yet formalized
- `LLM` should be introduced as one bounded execution subsystem, not as the system brain

In one sentence:

`The current system is a diagnosis-led orchestration layer over fragmented execution surfaces.`

That is good enough for a merchant-facing workspace v1. It is not yet sufficient as the long-term product and engineering control plane.

This document is primarily about the merchant-facing optimization workspace for `internal commerce`. As of March 19, 2026, production also supports `external referral` surfaces through employee-managed external seeds and tracked outbound redirects, but those surfaces are not yet first-class in the optimization plan contract. See `UNIFIED_READINESS_SCORECARD.md` for the dual-track audit that combines internal commerce and external referral into one readiness framing.

## Initial Landing State

The first implementation step is now landed in backend code:

- `GET /merchant/readiness/optimization` now carries plan-shaped metadata through:
  - `plan.plan_id`
  - `plan.snapshot_id`
  - `plan.workspace_version`
  - `plan.priority_policy_version`
  - `plan.refresh_state`
  - `plan.expires_at`
  - `plan.last_successful_rescore_at`
- the optimization payload now exposes a `score_bundle`
- issue buckets, merchant actions, and product queue items now carry:
  - `fixability`
  - `priority_score`
  - `priority_reason`
- product queue items now carry:
  - `queue_item_scope`
  - `queue_item_id`
- product queue items now also carry:
  - `platform_product_id`
  - `recommended_action_id`
  - `recommended_action_type`
- the first action APIs are now landed:
  - `POST /merchant/readiness/actions/refresh`
  - `POST /merchant/readiness/actions/preview`
  - `POST /merchant/readiness/actions/run`
  - `GET /merchant/readiness/jobs/{job_id}`

- a first backend remediation boundary is now landed in:
  - `readiness/remediation.py`

The current remediation boundary supports:

- plan consistency checks
- deterministic preview generation for product-content actions
- deterministic execution via the existing enrichment pipeline
- in-process execution job tracking
- basic before/after verification data

This is still not the full remediation orchestrator, but it moves the current system from a loose response payload toward a real plan-oriented contract.

## Product Positioning

The merchant-facing surface should be called:

`Agent Commerce Readiness & Optimization Workspace`

It is not:

- just an enrichment editor
- just a readiness report viewer
- just a wrapper over old product APIs

It should be a merchant-facing system for:

- diagnosing readiness and lift opportunities
- turning them into executable optimization work
- running bounded deterministic or LLM-assisted actions
- verifying whether those actions improved merchant outcomes

## Product Decision

The dashboard remains intentionally minimal.

It should answer only:

- what tier the merchant is in
- how many variants are ready or blocked
- why the merchant should optimize now
- whether the merchant should click `Optimize now`

The dashboard is a launcher.

The working surface is `/dashboard/product-optimization`, which should function as the actual optimization cockpit.

That means the backend must support two separate contracts:

1. `dashboard summary`
2. `optimization plan`

The second is not just a payload. It is a work plan bound to a snapshot and a prioritization policy.

## System Goal

The system should not optimize a single vague concept called `readiness`.

It should optimize a three-stage funnel:

1. `Eligibility / Readiness`
   Can this merchant or listing safely participate in agent commerce at all?

2. `Exposure / Retrieval Presence`
   Can LLMs and agents understand, retrieve, rank, and present this merchant and its listings well?

3. `Conversion / Actionability`
   Once surfaced, can the merchant actually support clicks, handoffs, checkout, and after-sales actions?

This leads to three score families:

- `readiness_score`
- `exposure_score`
- `conversion_score`

And three issue classes:

- `eligibility blockers`
- `exposure degraders`
- `conversion degraders`

This distinction matters because most current readiness logic is strongest in stage 1, partially covers stage 2, and only selectively covers stage 3.

## Dual-Track Scope Note

The funnel model above is still most mature for `internal commerce`, where Pivota owns the product, checkout, and operational loop.

Production also has a second agent-compatible surface:

- `External Referral`
  - employee-managed external seeds
  - tracked redirects
  - no-checkout handoff to merchant or partner destinations

That external referral track is already real in runtime code, but it is not yet represented as a first-class merchant-safe readiness contract. The current `OptimizationPlan` and `MerchantReadinessOptimizationPayload` should therefore be treated as `internal-commerce-first` until referral-specific status and issue buckets are added.

## Goals

- Keep the dashboard compact and decision-light.
- Project internal readiness findings into a merchant-safe optimization plan.
- Separate eligibility fixing from exposure and conversion lifting.
- Avoid conflating `referral-ready` with `checkout-ready`.
- Make optimization work explainable, auditable, and attributable.
- Reuse the current product enrichment and quality machinery while introducing a cleaner orchestration boundary.
- Create a controlled insertion point for future LLM-powered optimization.

## Non-Goals

- Replacing the internal readiness snapshot with a merchant payload.
- Exposing raw internal readiness reports directly to merchants.
- Letting LLM mutate canonical price, inventory, checkout, policy, or order state.
- Treating the product optimization workspace as the public contract for order execution.
- Fully specifying the external referral readiness contract in this document.

## Architecture Principles

- `dashboard = launcher`
- `workspace = operating surface`
- `readiness = source of diagnosis`
- `optimization plan = merchant-safe action projection`
- `deterministic fixes and LLM fixes are separate lanes`
- `every action must be attributable to a snapshot and a plan`
- `every execution action should support verification and impact reporting`
- `merchant-facing language should hide raw internal blocker codes by default`

## Current Backend Capability Map

### Diagnosis Layer

Status: `production-stable`

Primary modules:

- `readiness/service.py`
- `readiness/models.py`
- `readiness/scoring.py`
- `routes/readiness_internal.py`

Merchant projection:

- `readiness/summary.py`
- `routes/merchant_api_extensions.py`

Current merchant-facing routes:

- `GET /merchant/dashboard/readiness`
- `GET /merchant/readiness/optimization`

Current strength:

- canonical readiness diagnosis exists
- merchant-safe projection exists
- readiness tiering and issue bucketing exist

### Optimization Projection Layer

Status: `usable-but-fragmented`

Current projection already includes:

- `readiness_summary`
- `issue_buckets`
- `merchant_actions`
- `product_queue`
- `last_generated_at`

Current weakness:

- projection is still response-shaped, not yet plan-shaped
- snapshot and plan versioning are not first-class
- queue ordering logic is not yet formalized as a stable ranking policy

### Execution Layer: Product Content

Status: `usable-but-fragmented`

Current routes:

- `routes/merchant_products.py`
  - `GET /merchant/products`
  - `GET /merchant/products/quality/summary`
  - `GET /merchant/products/{platform}/{platform_product_id}`
  - `POST /merchant/products/enrichment/backfill`
  - `PUT /merchant/products/{platform}/{platform_product_id}/enrichment`
  - `POST /merchant/products/{platform}/{platform_product_id}/enrichment/run`

Current services:

- `services/product_enrichment_pipeline.py`
- `services/product_enrichment_ai.py`
- `services/product_quality_service.py`

Current truth:

- the execution surface exists
- enrichment generation is still largely deterministic / heuristic
- quality scoring is lightweight and rule-based
- the readiness workspace currently rides on top of these older APIs

### Execution Layer: Merchant Setup / Policy / Integrations

Status: `routed, not unified`

Problem classes handled here:

- checkout / PSP connection
- shipping and returns policy
- order sync configuration
- refund / return readiness surfaces

Current truth:

- these issues are now routed correctly out of the product editor
- they are not yet owned by a unified remediation orchestrator

### Verification Layer

Status: `partial`

Current truth:

- the portal can refresh readiness after product edits or enrichment runs
- the backend can regenerate summary and queue outputs
- the system does not yet expose a first-class verification object or before/after delta contract for every optimization action

## What Is Ready Now

The backend is ready for:

- dashboard-safe readiness summary
- merchant-safe issue bucketing
- merchant action routing by fix surface
- readiness-prioritized product queue
- issue-aware workspace rendering
- post-edit readiness refresh

Short version:

`ready for workspace orchestration v1`

## What Is Not Fully Ready Yet

The backend is not yet fully ready for:

- one unified remediation service for all issue classes
- one canonical action and job model
- server-owned bulk execution
- stable snapshot / plan freezing
- explicit verification and attribution contracts
- full LLM governance and preview-first actioning

Short version:

`not yet a complete optimization operating system`

## Core Domain Model

The next step is not more frontend complexity. It is to formalize the missing middle layer with explicit objects.

### 1. ReadinessSnapshot

One diagnosis snapshot bound to system truth.

Recommended fields:

- `snapshot_id`
- `merchant_id`
- `generated_at`
- `expires_at`
- `source_versions`
- `score_bundle`
- `raw_issue_refs`
- `freshness`
- `staleness_reason`

### 2. OptimizationPlan

Merchant-safe work plan derived from a snapshot.

Recommended fields:

- `plan_id`
- `snapshot_id`
- `workspace_version`
- `generated_at`
- `expires_at`
- `summary`
- `issue_buckets`
- `recommended_actions`
- `queue`
- `priority_policy_version`
- `refresh_state`
- `plan_superseded_by`

This should become the new conceptual name for `GET /merchant/readiness/optimization`.

### 3. RemediationAction

The unified action object for any fixable optimization step.

Recommended fields:

- `action_id`
- `plan_id`
- `action_type`
- `surface`
- `scope`
- `targets`
- `fixability`
- `priority_score`
- `priority_reason`
- `reason`
- `preconditions`
- `idempotency_key`
- `status`

### 4. ExecutionJob

The runtime object for action execution.

Recommended fields:

- `job_id`
- `action_id`
- `executor_type`
- `started_at`
- `completed_at`
- `result`
- `error_code`
- `retry_count`

### 5. PatchCandidate

The output of a deterministic suggestion engine or LLM engine before persistence.

Recommended fields:

- `candidate_id`
- `action_id`
- `target_field`
- `before`
- `after`
- `confidence`
- `rationale`
- `evidence_used`
- `risk_flags`
- `requires_approval`

### 6. VerificationResult

The post-action result that proves whether optimization actually helped.

Recommended fields:

- `verification_id`
- `action_id`
- `before_snapshot_id`
- `after_snapshot_id`
- `delta_scores`
- `resolved_issues`
- `remaining_issues`
- `expected_impact`
- `observed_impact`
- `merchant_visible_impact`

## Optimization Unit Model

The system currently mixes:

- merchant
- product
- variant

That needs to be made explicit.

### Supported scopes

- `merchant`
- `product`
- `variant`

### Recommended ownership

- checkout / PSP / policy / order sync issues -> `merchant`
- title / bullets / summary / FAQ / media / tags -> `product`
- price / inventory / option completeness -> `variant`

### Queue rule

Every queue item should expose:

- `queue_item_scope`
- `queue_item_id`
- `affected_variant_count`
- `priority_score`
- `priority_reason`

Without this, the UI and execution layers will keep mixing product- and variant-level work.

## Snapshot And Plan Consistency

This is a required system concern, not a future nice-to-have.

The workspace should not operate on a floating response that mutates invisibly after every refresh.

### Required fields for v1.5

- `snapshot_id`
- `plan_id`
- `workspace_version`
- `generated_at`
- `expires_at`
- `refresh_state`
- `can_apply_actions`
- `last_successful_rescore_at`

### Required behavior

- workspace loads against one `plan_id`
- action execution is bound to that `plan_id`
- refresh creates a new `snapshot_id` and `plan_id`
- older plans become `superseded`, not immediately erased
- UI can prompt the merchant to adopt the new plan

This prevents page logic from drifting away from backend truth.

## Priority Model

The queue cannot remain a loosely ordered list.

Priority must be server-side and explainable.

### Priority inputs

- `business_impact`
- `merchant_effort`
- `fixability`
- `scope_size`
- `dependency_order`

### Suggested output

- `priority_score`
- `priority_reason`
- `ranking_explanation`

Merchant-safe surfaces should use `priority_reason`.
Internal debugging can expose `ranking_explanation`.

### Examples

- fixing checkout connection may rank above rewriting 100 product summaries
- fixing one missing shipping policy may unlock more variants than ten image edits
- fixing missing price on 40 variants may outrank a low-confidence review rewrite

## LLM Governance

LLM should be integrated, but only under explicit policy.

### LLM should handle

- title rewrite
- summary rewrite
- bullet rewrite
- FAQ drafting
- usage scenario generation
- audience/tag inference
- merchant-facing explanation
- issue-specific content suggestions

### LLM must not own canonical truth for

- price / currency
- inventory
- checkout capability
- PSP setup
- shipping / returns policy truth
- order sync state
- refund / return / transaction state

### Required governance

1. `field allowlist`
2. `source grounding`
3. `approval policy`
4. `policy and brand safety checks`

### Recommended output contract

Any future `services/product_optimization_llm.py` should emit structured candidates:

- `suggested_patch`
- `evidence_used`
- `rationale`
- `confidence`
- `risk_flags`
- `requires_human_review`

This service should support `preview-first`, not blind auto-apply.

## Control Plane Lifecycle

The desired lifecycle is:

1. generate `ReadinessSnapshot`
2. project `OptimizationPlan`
3. merchant previews `RemediationAction`
4. backend runs `ExecutionJob`
5. backend emits `VerificationResult`
6. workspace refreshes to a new plan if needed

This is the missing middle layer between diagnosis and execution.

## API Direction

### Keep

- `GET /merchant/dashboard/readiness`

Use it only for the dashboard launcher summary.

### Reframe

- `GET /merchant/readiness/optimization`

This should conceptually return an `optimization_plan`, not a generic payload.

Recommended additions:

- `snapshot_id`
- `plan_id`
- `workspace_version`
- `refresh_state`
- `generated_at`
- `expires_at`
- `last_successful_rescore_at`

### Add

#### `POST /merchant/readiness/actions/preview`

Purpose:

- deterministic preview
- LLM preview
- dry-run impact preview

Recommended request:

- `plan_id`
- `action_type`
- `targets`
- `dry_run`

Recommended response:

- `candidate_patches`
- `expected_impact`
- `requires_approval`
- `warnings`

#### `POST /merchant/readiness/actions/run`

Recommended request:

- `plan_id`
- `action_id` or `action_spec`
- `idempotency_key`
- `approval_token`
- `execution_mode`

Recommended response:

- `job_id`
- `action_status`
- `verification_state`

#### `GET /merchant/readiness/actions/{action_id}`

Or:

- `GET /merchant/readiness/jobs/{job_id}`

Recommended response:

- `status`
- `executor_type`
- `started_at`
- `completed_at`
- `result_summary`
- `before_after_delta`
- `next_recommended_action`

#### `POST /merchant/readiness/actions/refresh`

Recommended inputs:

- `scope`
- `reason`

## Current To Target Gap

### Current state

- readiness report exists
- dashboard summary exists
- merchant-safe optimization projection exists
- product edit and enrichment routes exist
- heuristic AI enrichment exists
- quality preview exists

### Missing middle layer

The missing layer is a true `remediation orchestrator`.

It should own:

- issue-to-action mapping
- action dependency graph
- idempotency
- execution dispatch
- async job lifecycle
- post-action verification
- audit trail

Right now, some of this still leaks into page logic or older route behavior. That is acceptable for v1, but it should not remain the long-term architecture.

## Verification And Attribution

Refreshing a score is not enough.

The system should show:

### System impact

- score delta
- tier delta
- resolved issue count
- unlocked variants

### Business impact

- eligible products delta
- retrievable products delta
- actionability delta
- conversion proxy delta

Even before business metrics are fully wired, the backend should reserve:

- `expected_impact`
- `observed_impact`

Otherwise the system will optimize for scores instead of merchant outcomes.

## Recommended Backend Framework

### Phase 1: Current V1

- keep readiness diagnosis in readiness modules
- keep merchant-safe projection in `readiness/summary.py`
- keep execution routed through current merchant product and integration surfaces
- keep LLM optional and constrained

### Phase 2: Add Remediation Orchestrator

Introduce a dedicated orchestration boundary, for example:

- `readiness/remediation.py`

This should not be a helper file. It should be the server-side action orchestrator.

Responsibilities:

- issue-to-action mapping
- action dependency management
- idempotent action execution
- execution dispatch
- job tracking
- post-action verification trigger
- action audit trail

### Phase 3: Add LLM Optimization Engine

Introduce:

- `services/product_optimization_llm.py`

Responsibilities:

- generate structured rewrite candidates from readiness context
- support preview-first suggestions
- produce merchant-facing rationale
- stay bounded to approved fields

### Phase 4: Move Bulk Fix Server-Side

Bulk execution should be backend-owned, not a frontend loop over old APIs.

Otherwise the system will keep suffering from:

- partial success ambiguity
- retry inconsistency
- ordering issues
- weak attribution

## Implementation Order

1. formalize `ReadinessSnapshot`, `OptimizationPlan`, `RemediationAction`, `ExecutionJob`, `PatchCandidate`, and `VerificationResult`
2. add `snapshot_id`, `plan_id`, `workspace_version`, and `refresh_state` to `GET /merchant/readiness/optimization`
3. build the remediation orchestrator before adding more LLM behavior
4. add `preview / run / status / refresh` action APIs
5. move priority scoring fully server-side
6. connect LLM only to the preview-first path
7. add explicit verification delta and attribution fields

## Recommendation

Treat the system as:

- `backend-ready for readiness-first workspace v1`
- `not yet backend-complete for a full optimization control plane`

The highest-value next backend step is to build the missing remediation layer, not to add more frontend logic or more free-form AI behavior.
