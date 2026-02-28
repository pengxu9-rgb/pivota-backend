# Upstream Rule Prune Manifest

This manifest maps required upstream orchestrator changes that are not present
in the local `pivota-backend` repository.

## Scope

- Target runtime path: `search_route_adapter` / `external_invoke_route`
- Strategy version observed: `search_orchestrator_unified_v1`
- Objective: remove hard external-seed blockers while preserving outbound URL
  safety baseline.

## Required Upstream Changes

### 1) Cache supplement gate hard-block removal

- Current symptom: `route_debug.cross_merchant_cache.supplement.reason=external_fill_gate_blocked`
- Required change:
  - Remove hard block on `min_internal_required` and `overall_confidence` for
    external supplement.
  - Keep gate as soft scoring signal only.
- Expected result:
  - Supplement path still runs when internal pool exists.
  - `external_fill_gate_reason` becomes an informative reason, not a blocker.

### 2) Domain hard filter downgrade (brand/fragrance)

- Current symptom: `domain_filter_dropped_external` spikes on brand+category
  queries (e.g. `kylie cosmetics`).
- Required change:
  - For `brand_query_detected=true` or `query_semantic_class=fragrance`,
    downgrade external domain mismatch from hard drop to soft penalty.
  - Retain allowlist safety checks for outbound redirect generation.
- Expected result:
  - External candidates remain rankable under brand/fragrance intent.
  - `domain_filter_dropped_external` materially decreases.

### 3) Diversity hard clear removal

- Current symptom: `supplement.reason=no_external_candidates_for_diversity`
  causes zero external despite available raw candidates.
- Required change:
  - Remove diversity hard-clear path for external candidates.
  - For `query_semantic_class=fragrance`, bypass diversity hard gate entirely.
- Expected result:
  - Low-diversity external pool is still returned with lower rank instead of
    being fully dropped.

### 4) Fallback chain normalization

- Current symptom: `fallback_not_better` direct empty return without semantic
  retry in fragrance queries.
- Required change:
  - Enforce chain: `primary -> semantic_retry(1) -> clarify`.
  - Prohibit direct empty return from `fallback_not_better` before retry.
- Expected result:
  - Empty final output must include `semantic_retry_applied=true` (or explicit
    clarify response metadata).

## Metadata Contract Requirements

Ensure both `/agent/v1/products/search` and `/api/gateway` responses carry and
synchronize these values in `metadata.route_health` and top-level metadata when
applicable:

- `orchestrator_path`
- `decision_node`
- `domain_filter_dropped_external`
- `external_fill_gate_reason`
- `semantic_retry_applied`
- `semantic_retry_query`
- `semantic_retry_hits`
- `external_seed_brand_strict_rows`
- `external_seed_brand_relevant_rows`
- `external_seed_broad_fallback_used`
- `external_seed_broad_scope_rows`
- `fallback_reason` (must match between top-level and `route_health`)

## Validation Checklist

- `kylie cosmetics`: avg `external_seed_returned_count >= 8`
- `perfume`: no direct empty without semantic retry/clarify
- `fenty/tom ford/sigma`: external coverage no worse than baseline
- `lingerie`: non-empty stable, no permanent `cache not_needed` suppression

