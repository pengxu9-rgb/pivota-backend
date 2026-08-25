# Celestial Pivot Multi Release Runbook

> ⚠️ **Production is GCP Cloud Run (`pivota-prod`, `us-west1`) since 2026-08-22. Railway is the
> ROLLBACK.** The `railway ...` commands below have NOT been rewritten — they were left as-is
> rather than translated by guesswork, because the procedures here were never re-verified against
> GCP. Running one changes the platform nobody is served from: the incident continues while the
> dial reads as turned. Translate with
> [operating_on_gcp_production.md](../runbooks/operating_on_gcp_production.md) before acting, or treat this
> document as a historical record of how the Railway rollout was done.


## Scope
- Production target: `find_products_multi`
- Serve canary model: `stage by source`
- Stage 1 serve: `shopping_agent`, `page=1`
- Stage 2 serve: `shopping-agent-ui`, `page=1`
- Stage 3 serve: `shopping-agent-web`, `page=1`
- Result pool: `internal + external + estimated incentives`
- `Aurora`: shadow only

## Required Assets
- SQL migration: [058_catalog_core.sql](/Users/pengchydan/dev/_worktrees/pivota-backend-hyaluronic-aliases-20260325/db/migrations/058_catalog_core.sql)
- Search index migration: [059_catalog_pivot_search_indexes.sql](/Users/pengchydan/dev/_worktrees/pivota-backend-hyaluronic-aliases-20260325/db/migrations/059_catalog_pivot_search_indexes.sql)
- Migration apply/verify CLI: [catalog_migration_058.py](/Users/pengchydan/dev/_worktrees/pivota-backend-hyaluronic-aliases-20260325/scripts/catalog_migration_058.py)
- Search index apply/verify CLI: [catalog_migration_059.py](/Users/pengchydan/dev/_worktrees/pivota-backend-hyaluronic-aliases-20260325/scripts/catalog_migration_059.py)
- Backfill / verify CLI: [catalog_backfill_verify.py](/Users/pengchydan/dev/_worktrees/pivota-backend-hyaluronic-aliases-20260325/scripts/catalog_backfill_verify.py)
- Release gate CLI: [pivot_multi_release_gate.py](/Users/pengchydan/dev/_worktrees/pivota-backend-hyaluronic-aliases-20260325/scripts/pivot_multi_release_gate.py)
- Catalog/Pivot live smoke: [smoke_catalog_pivot_v1.py](/Users/pengchydan/dev/_worktrees/pivota-backend-hyaluronic-aliases-20260325/scripts/smoke_catalog_pivot_v1.py)
- Commerce channels signoff smoke: [smoke_commerce_channels_signoff.py](/Users/pengchydan/dev/_worktrees/pivota-backend-hyaluronic-aliases-20260325/scripts/smoke_commerce_channels_signoff.py)
- Employee/admin JWT mint helper: [mint_employee_jwt.py](/Users/pengchydan/dev/_worktrees/pivota-backend-hyaluronic-aliases-20260325/scripts/mint_employee_jwt.py)
- Generic commerce shadow audit: [commerce_shadow_audit.py](/Users/pengchydan/dev/_worktrees/pivota-backend-hyaluronic-aliases-20260325/scripts/commerce_shadow_audit.py)
- Generic commerce shadow compare: [compare_commerce_shadow_audit.py](/Users/pengchydan/dev/_worktrees/pivota-backend-hyaluronic-aliases-20260325/scripts/compare_commerce_shadow_audit.py)
- Release evidence builder: [build_pivot_release_evidence.py](/Users/pengchydan/dev/_worktrees/pivota-backend-hyaluronic-aliases-20260325/scripts/build_pivot_release_evidence.py)
- Bundle orchestrator: [run_pivot_release_bundle.py](/Users/pengchydan/dev/_worktrees/pivota-backend-hyaluronic-aliases-20260325/scripts/run_pivot_release_bundle.py)
- Grafana dashboard: [celestial_pivot_multi_release_dashboard.json](/Users/pengchydan/dev/_worktrees/pivota-backend-hyaluronic-aliases-20260325/observability/grafana/celestial_pivot_multi_release_dashboard.json)
- Merchant readiness dashboard query pack: [merchant_commerce_readiness_queries.sql](/Users/pengchydan/dev/_worktrees/pivota-backend-celestial-four-domains-default-on-20260330/observability/grafana/merchant_commerce_readiness_queries.sql)
- Alert rules: [celestial_pivot_multi_release_alerts.yml](/Users/pengchydan/dev/_worktrees/pivota-backend-hyaluronic-aliases-20260325/observability/prometheus/celestial_pivot_multi_release_alerts.yml)
- Follow-on phase plan: [CELESTIAL_PIVOT_FOLLOW_ON_PHASES.md](/Users/pengchydan/dev/_worktrees/pivota-backend-hyaluronic-aliases-20260325/docs/ops/CELESTIAL_PIVOT_FOLLOW_ON_PHASES.md)

## Corpus Assets
- Fast beauty smoke corpus: [beauty_ranking_golden_corpus.json](/Users/pengchydan/dev/_worktrees/pivota-backend-hyaluronic-aliases-20260325/scripts/fixtures/beauty_ranking_golden_corpus.json)
- Expanded beauty rollout corpus: [beauty_ranking_golden_corpus_v2.json](/Users/pengchydan/dev/_worktrees/pivota-backend-hyaluronic-aliases-20260325/scripts/fixtures/beauty_ranking_golden_corpus_v2.json)
- Generic commerce parity corpus: [generic_commerce_shadow_corpus.json](/Users/pengchydan/dev/_worktrees/pivota-backend-hyaluronic-aliases-20260325/scripts/fixtures/generic_commerce_shadow_corpus.json)
- Source-stage serve canary corpus: [serve_canary_corpus.json](/Users/pengchydan/dev/_worktrees/pivota-backend-hyaluronic-aliases-20260325/scripts/fixtures/serve_canary_corpus.json)
- Staging/prod release gate corpora: [staging_corpus.json](/Users/pengchydan/dev/_worktrees/pivota-backend-hyaluronic-aliases-20260325/scripts/fixtures/pivot_release_gate/staging_corpus.json), [prod_corpus.json](/Users/pengchydan/dev/_worktrees/pivota-backend-hyaluronic-aliases-20260325/scripts/fixtures/pivot_release_gate/prod_corpus.json)

## Feature Flags
- `AGENT_SHOP_PIVOT_MULTI_SHADOW_ENABLED=true`
- `AGENT_SHOP_PIVOT_MULTI_SERVE_ENABLED=false`
- `AGENT_SHOP_PIVOT_MULTI_SHADOW_SOURCE_ALLOWLIST=shopping_agent,shopping-agent-ui,shopping-agent-web,aurora,aurora-chatbox`
- `AGENT_SHOP_PIVOT_MULTI_SERVE_SOURCE_ALLOWLIST=shopping_agent`
- `AGENT_SHOP_PIVOT_MULTI_SERVE_MAX_PAGE=1`
- `AGENT_SHOP_PIVOT_MULTI_SERVE_INCLUDE_EXTERNAL=true`
- `AGENT_SHOP_PIVOT_MULTI_SERVE_INCLUDE_INCENTIVES=true`

## Rollout Order
1. Apply `058_catalog_core.sql`, then `059_catalog_pivot_search_indexes.sql`, in staging, then production.
2. Run `catalog_backfill_verify.py --mode apply` for sample merchants.
3. Run `catalog_backfill_verify.py --mode verify` and archive JSON/Markdown evidence.
4. Deploy with `shadow=true`, `serve=false`.
5. Run `pivot_multi_release_gate.py` against staging corpus, then prod corpus.
6. Run `smoke_catalog_pivot_v1.py` against staging, then prod shadow.
   Include `--catalog-migration-verify-smoke` so the deployed admin migration endpoint is verified live.
   For production shadow from a laptop, prefer service-side data-plane validation so the bundle does not depend on Railway private Postgres reachability.
7. Run `search_chain_inventory_probe.py` for route-health / dual-entry parity evidence when doing full staging or prod shadow signoff.
   Treat this as legacy parity evidence, not as a blocking pivot rollout gate, unless you are explicitly validating those older public entrypoints and have the correct agent/gateway credentials.
8. Run `beauty_ranking_audit.py` for cross-merchant beauty ranking parity evidence before deploy, then compare the before/after reports after deploy.
   Use the fast corpus for smoke and the `v2` corpus for ranking signoff / serve expansion.
9. Run `commerce_shadow_audit.py` for `default + fragrance` served-vs-pivot parity evidence before deploy, then compare the before/after reports after deploy.
10. Build one evidence bundle with `build_pivot_release_evidence.py`.
11. Review Grafana dashboard and Prometheus alerts grouped by `source`, `query_semantic_class`, `served_path`, `shadow_path`.
12. If shadow metrics pass threshold, enable serve in stages:
    - Stage 1: `shopping_agent`
    - Stage 2: `shopping-agent-ui`
    - Stage 3: `shopping-agent-web`
13. Keep `Aurora` shadow-only throughout this phase.
14. After stable all-sources observation passes, run `smoke_commerce_channels_signoff.py` against one approved primary merchant to directly sign off:
    - catalog read-side query
    - catalog write-side webhook + sync job + backfill apply/verify
    - order-backed payment initiation canary

## Standard Bundle Command
```bash
python3 scripts/run_pivot_release_bundle.py \
  --base-url "$BASE_URL" \
  --corpus scripts/fixtures/pivot_release_gate/staging_corpus.json \
  --merchant-id "$MERCHANT_ID" \
  --output-dir output/pivot-release/staging \
  --backfill-limit 10 \
  --migration-mode apply-verify \
  --catalog-migration-verify-smoke \
  --search-chain-probe \
  --beauty-ranking-audit \
  --beauty-ranking-audit-corpus scripts/fixtures/beauty_ranking_golden_corpus.json \
  --beauty-ranking-audit-db-mode sync \
  --beauty-ranking-audit-database-url "$DATABASE_PUBLIC_URL" \
  --commerce-shadow-audit \
  --commerce-shadow-audit-corpus scripts/fixtures/generic_commerce_shadow_corpus.json \
  --catalog-sync-job-smoke
```

## Production Shadow Bundle Command
```bash
ADMIN_JWT="$(python3 scripts/mint_employee_jwt.py \
  --railway-service web \
  --role admin \
  --email ops+pivot@pivota.invalid \
  --employee-id emp_pivot)"

python3 scripts/run_pivot_release_bundle.py \
  --base-url "$BASE_URL" \
  --release-gate-base-url https://api.pivota.cc \
  --smoke-base-url https://api.pivota.cc \
  --corpus scripts/fixtures/pivot_release_gate/prod_corpus.json \
  --merchant-id "$MERCHANT_ID" \
  --output-dir output/pivot-release/prod-shadow \
  --migration-mode apply-verify \
  --service-side-data-plane-verify \
  --smoke-header "Authorization: Bearer $ADMIN_JWT" \
  --beauty-ranking-audit \
  --beauty-ranking-audit-corpus scripts/fixtures/beauty_ranking_golden_corpus_v2.json \
  --beauty-ranking-audit-pivot-header "Authorization: Bearer $ADMIN_JWT" \
  --beauty-ranking-audit-db-mode sync \
  --beauty-ranking-audit-database-url "$DATABASE_PUBLIC_URL" \
  --beauty-ranking-audit-compare-before-json output/pivot-release/beauty-ranking-before.json \
  --commerce-shadow-audit \
  --commerce-shadow-audit-corpus scripts/fixtures/generic_commerce_shadow_corpus.json \
  --commerce-shadow-audit-pivot-header "Authorization: Bearer $ADMIN_JWT" \
  --commerce-shadow-audit-compare-before-json output/pivot-release/commerce-shadow-before.json \
  --catalog-sync-wait-seconds 60 \
  --search-chain-probe
```

Note:
- `release gate` + direct `catalog/pivot smoke` are the blocking production shadow checks for Celestial Pivot.
- `search_chain_inventory_probe` is legacy parity evidence by default. Older `agent_v1` / `api_gateway` ingress issues should not block pivot shadow signoff unless that parity lane is part of the active release scope.

## Shadow Pass Criteria
- by-source `no_result_mismatch = 0`
- by-source `bad_price_anomaly = 0`
- target source `top1_same` does not regress below its pre-serve shadow baseline
- `catalog_search_latency_seconds{path="pivot_semantic_core_multi"}` p95 within SLO
- `pivot_rollout_mode=shadow` and `pivot_rollout_guard_passed=true` visible in `route_health`
- use the release bundle evidence summary fields:
  - `release_gate_source_summary`
  - `semantic_class_summary`
  - `serve_readiness_by_source`

## Serve Canary Abort Conditions
- target source `top1_same` regresses below its baseline
- target source gets any `no_result_mismatch`
- target source gets any `bad_price_anomaly`
- latency or error budget regresses for the active source / semantic class
- rollback command:
  - remove the active source from `AGENT_SHOP_PIVOT_MULTI_SERVE_SOURCE_ALLOWLIST`
  - if the failing source is `shopping_agent`, set `AGENT_SHOP_PIVOT_MULTI_SERVE_ENABLED=false`

## Source-Stage Canary Procedure
1. Pre-serve baseline:
   - keep `AGENT_SHOP_PIVOT_MULTI_SERVE_ENABLED=false`
   - run `pivot_multi_release_gate.py` and `run_pivot_release_bundle.py` with `serve_canary_corpus.json` in shadow mode
2. Stage 1:
   - `AGENT_SHOP_PIVOT_MULTI_SERVE_ENABLED=true`
   - `AGENT_SHOP_PIVOT_MULTI_SERVE_SOURCE_ALLOWLIST=shopping_agent`
   - `AGENT_SHOP_PIVOT_MULTI_SERVE_MAX_PAGE=1`
   - run the gate/bundle with `--release-gate-source-filter shopping_agent` and `--release-gate-default-rollout-mode serve`
3. Stage 2:
   - `AGENT_SHOP_PIVOT_MULTI_SERVE_SOURCE_ALLOWLIST=shopping_agent,shopping-agent-ui`
   - rerun with `--release-gate-source-filter shopping-agent-ui`
4. Stage 3:
   - `AGENT_SHOP_PIVOT_MULTI_SERVE_SOURCE_ALLOWLIST=shopping_agent,shopping-agent-ui,shopping-agent-web`
   - rerun with `--release-gate-source-filter shopping-agent-web`
5. Never add `aurora` or `aurora-chatbox` to the serve allowlist in this phase.

## Post-Enable Observation Window
After the active source is enabled and the new deployment is visible in `/health`, run one stable observation pass before closing the stage:

1. Confirm the deployment/commit changed and the serve allowlist is live.
2. Wait for the deployment to settle before judging parity.
   A short settle window is enough; avoid using the first requests after a config flip as final evidence.
3. Rerun the source-stage gate and source-stage commerce audit.
4. For final stage close-out, rerun the full all-sources gate and full generic commerce audit.
5. Build a fresh evidence bundle from the stable rerun artifacts and archive that bundle as the canonical stage-close record.

Notes:
- `pivot_multi_release_gate.py` now retries transient transport failures per case and records request-level failures in the report instead of aborting the entire run.
- If a report shows a one-off `shadow` or `no_result_mismatch` immediately after a deployment flip, verify with a stable rerun before treating it as a rollback signal.
- `smoke_commerce_channels_signoff.py` is a close-out signoff for one approved merchant after the stage is already green.
  It is not a replacement for the corpus-driven release gate.

Example final all-sources observation:

```bash
python3 scripts/pivot_multi_release_gate.py \
  --base-url https://api.pivota.cc \
  --corpus scripts/fixtures/serve_canary_corpus.json \
  --default-rollout-mode serve \
  --output-json output/pivot-release/serve-observation/pivot-release-gate.json \
  --output-md output/pivot-release/serve-observation/pivot-release-gate.md

python3 scripts/commerce_shadow_audit.py \
  --gateway-base-url https://api.pivota.cc \
  --pivot-base-url https://api.pivota.cc \
  --pivot-header "Authorization: Bearer $ADMIN_JWT" \
  --corpus scripts/fixtures/generic_commerce_shadow_corpus.json \
  --timeout-seconds 20 \
  --output-json output/pivot-release/serve-observation/commerce-shadow-after.json \
  --output-md output/pivot-release/serve-observation/commerce-shadow-after.md
```

Example source-stage gate:

```bash
python3 scripts/pivot_multi_release_gate.py \
  --base-url https://api.pivota.cc \
  --corpus scripts/fixtures/serve_canary_corpus.json \
  --source-filter shopping_agent \
  --default-rollout-mode serve \
  --output-json output/pivot-release/serve-stage1-gate.json \
  --output-md output/pivot-release/serve-stage1-gate.md
```

## Evidence Bundle
- migration apply logs
- backfill verify JSON + Markdown
- staging release-gate JSON + Markdown
- prod shadow release-gate JSON + Markdown
- staging/prod catalog-pivot smoke JSON + Markdown
- staging/prod search-chain probe JSON + Markdown
- beauty ranking audit JSON + Markdown
- beauty ranking audit compare JSON + Markdown
- generic commerce shadow audit JSON + Markdown
- generic commerce shadow audit compare JSON + Markdown
- commerce channels signoff JSON + Markdown
- consolidated evidence JSON + Markdown
- Grafana screenshot/export
- deployed commit from `X-Service-Commit`

## Beauty Ranking Audit
Before deploy, capture a production baseline with the fast sync path:

```bash
ADMIN_JWT="$(python3 scripts/mint_employee_jwt.py \
  --railway-service web \
  --role admin \
  --email ops+pivot@pivota.invalid \
  --employee-id emp_pivot)"

python3 scripts/beauty_ranking_audit.py \
  --gateway-base-url https://api.pivota.cc \
  --pivot-base-url https://api.pivota.cc \
  --gateway-header "Authorization: Bearer $SHOP_GATEWAY_AGENT_API_KEY" \
  --pivot-header "Authorization: Bearer $ADMIN_JWT" \
  --database-url "$DATABASE_PUBLIC_URL" \
  --db-mode sync \
  --seed-fetch-mode fast \
  --timeout-seconds 6 \
  --output-json output/pivot-release/beauty-ranking-before.json \
  --output-md output/pivot-release/beauty-ranking-before.md
```

After deploy, rerun the same command and compare the two reports:

```bash
python3 scripts/compare_beauty_ranking_audit.py \
  --before-json output/pivot-release/beauty-ranking-before.json \
  --after-json output/pivot-release/beauty-ranking-after.json \
  --output-json output/pivot-release/beauty-ranking-compare.json \
  --output-md output/pivot-release/beauty-ranking-compare.md \
  --before-label before-deploy \
  --after-label after-deploy
```

Review:
- `top1_match_delta`
- `improved_query_count`
- `regressed_query_count`
- per-query `before/after` top1 and top5 overlap

Bundle integration:
- `run_pivot_release_bundle.py --beauty-ranking-audit` writes `beauty-ranking-audit.json/md` into the bundle output directory.
- Add `--beauty-ranking-audit-compare-before-json ...` to have the bundle also emit `beauty-ranking-audit-compare.json/md`.
- Keep beauty audit non-blocking by default, or add `--beauty-ranking-audit-blocking` when ranking parity is part of the active release gate.

## Generic Commerce Shadow Audit
Before deploy, capture a generic-commerce baseline:

```bash
python3 scripts/commerce_shadow_audit.py \
  --gateway-base-url https://api.pivota.cc \
  --pivot-base-url https://api.pivota.cc \
  --pivot-header "Authorization: Bearer $ADMIN_JWT" \
  --corpus scripts/fixtures/generic_commerce_shadow_corpus.json \
  --timeout-seconds 6 \
  --output-json output/pivot-release/commerce-shadow-before.json \
  --output-md output/pivot-release/commerce-shadow-before.md
```

After deploy, rerun and compare:

```bash
python3 scripts/compare_commerce_shadow_audit.py \
  --before-json output/pivot-release/commerce-shadow-before.json \
  --after-json output/pivot-release/commerce-shadow-after.json \
  --output-json output/pivot-release/commerce-shadow-compare.json \
  --output-md output/pivot-release/commerce-shadow-compare.md \
  --before-label before-deploy \
  --after-label after-deploy
```

Review:
- `top1_match_delta`
- `regressed_query_count`
- per-source `source_summary`
- `no_result_mismatch_cases`

Bundle integration:
- `run_pivot_release_bundle.py --commerce-shadow-audit` writes `commerce-shadow-audit.json/md`.
- Add `--commerce-shadow-audit-compare-before-json ...` to also emit `commerce-shadow-audit-compare.json/md`.
- Keep generic commerce audit non-blocking by default, or add `--commerce-shadow-audit-blocking` for active serve canary signoff.

## Commerce Channels Signoff
Run this after the all-sources observation window is green and production is stable.
This signoff is intentionally direct and merchant-specific; it supplements the corpus-based release bundle instead of replacing it.

Required auth:
- admin/super_admin JWT for every `/v1/catalog/*` route (`ADMIN_JWT` below). These
  routes are tenant-scoped: a non-admin token may only name the `merchant_id` in
  its own `merchant_id` claim, so ADR-009 `merch_obs_*` observed-seller ids —
  which no tenant token carries — are reachable by admins only.
  This applies to `scripts/run_commerce_channels_signoff_batch.py` too, which fans
  one `--header` set across every merchant case — so the batch run needs an admin
  token for ALL cases, not just the single-merchant block below.

```bash
ADMIN_JWT="$(python3 scripts/mint_employee_jwt.py \
  --railway-service web \
  --role admin \
  --email ops+commerce-signoff@pivota.invalid \
  --employee-id emp_commerce_signoff)"

READINESS_INTERNAL_API_KEY="$(railway variables --service web --json | \
  python3 -c 'import json,sys; print(json.load(sys.stdin)["READINESS_INTERNAL_API_KEY"])')"

python3 scripts/smoke_commerce_channels_signoff.py \
  --base-url https://api.pivota.cc \
  --merchant-id "$MERCHANT_ID" \
  --database-url "$DATABASE_PUBLIC_URL" \
  --query "winona soothing repair serum" \
  --header "Authorization: Bearer $ADMIN_JWT" \
  --internal-key "$READINESS_INTERNAL_API_KEY" \
  --backfill-timeout-seconds 60 \
  --output-json output/pivot-release/commerce-signoff/commerce-channels-signoff.json \
  --output-md output/pivot-release/commerce-signoff/commerce-channels-signoff.md
```

Review:
- `overall_ok`
- `summary.catalog_read_ok`
- `summary.catalog_write_ok`
- `summary.payment_order_ok`
- step `catalog_webhook_ingest`
- step `catalog_sync_job_final`
- step `catalog_backfill_apply`
- step `catalog_backfill_verify`
- step `payment_order_backed_canary`

Acceptance boundaries:
- This directly signs off:
  - product/catalog read-side lookup
  - catalog write-side webhook ingest
  - catalog sync job create/poll
  - products-cache backfill apply + verify
  - production-safe order-backed payment initiation
- This does not sign off a real paid terminal state.
  The order-backed canary intentionally stops at payment initiation and should remain safe to run during routine release close-out.
- The script redacts `client_secret` and similar secrets before writing JSON/Markdown artifacts.

When to rerun:
- rerun after material changes to catalog write plumbing, PSP routing, or merchant-readiness order-backed canary behavior
- rerun when closing a new production stage if the direct merchant signoff is part of that phase's acceptance

For the next step beyond this merchant-specific signoff, use the separate follow-on phase plan:
- [CELESTIAL_PIVOT_FOLLOW_ON_PHASES.md](/Users/pengchydan/dev/_worktrees/pivota-backend-hyaluronic-aliases-20260325/docs/ops/CELESTIAL_PIVOT_FOLLOW_ON_PHASES.md)

## Merchant Portal Readiness And Signals
This release train now exposes merchant-facing readiness and commerce diagnostics in addition to the existing product-level optimization views.
Treat these surfaces as complementary:

- product diagnosis remains the single-product/source-data remediation lane
- merchant readiness answers whether the whole merchant is `Foundation / Discover / Signals / Execute` ready
- commerce issues + interaction trace answer where the runtime path broke across listing, click, order, refund, and return

Merchant-facing endpoints:
- `GET /merchant/analytics/readiness-state`
  - source: [merchant_analytics_routes.py](/Users/pengchydan/dev/_worktrees/pivota-backend-celestial-four-domains-default-on-20260330/routes/merchant_analytics_routes.py)
  - computation: [merchant_commerce_readiness_service.py](/Users/pengchydan/dev/_worktrees/pivota-backend-celestial-four-domains-default-on-20260330/services/merchant_commerce_readiness_service.py)
- `GET /merchant/analytics/commerce-funnel`
  - grouped summary/read model for indexed, surfaced, clicked, ordered, refunded, and listing status breakdown
  - computation: [merchant_commerce_funnel_service.py](/Users/pengchydan/dev/_worktrees/pivota-backend-celestial-four-domains-default-on-20260330/services/merchant_commerce_funnel_service.py)
- `GET /merchant/analytics/commerce-funnel/issues`
  - merchant-facing diagnostic buckets such as `LISTING_ERROR`, `MISSING_INFO`, `VARIANT_MISMATCH`, `TRACE_BROKEN`, `UNATTRIBUTED_ORDER`
  - computation: [merchant_commerce_diagnostics_service.py](/Users/pengchydan/dev/_worktrees/pivota-backend-celestial-four-domains-default-on-20260330/services/merchant_commerce_diagnostics_service.py)
- `GET /merchant/analytics/commerce-interactions/{interaction_id}`
  - drilldown for a single interaction ledger trace
  - lookup: [commerce_interaction_service.py](/Users/pengchydan/dev/_worktrees/pivota-backend-celestial-four-domains-default-on-20260330/services/commerce_interaction_service.py)

Portal expectation:
- `/dashboard/analytics` now has four complementary blocks:
  - funnel summary
  - grouped funnel table
  - issue drilldown
  - readiness state + interaction trace
- these are merchant-level operating views and do not replace `/dashboard/products` or product optimization blockers

Operational guardrails:
- migrations `062_commerce_interaction_ledger.sql`, `063_merchant_commerce_readiness_state.sql`, and `064_commerce_interaction_backrefs.sql` should be applied before treating the portal readiness/signals views as canonical
- dual-write into the interaction ledger is best-effort during rollout
  - legacy write paths such as listing export and click attribution must continue even if the new ledger tables are not present yet
  - after migrations are live, investigate any degraded ledger writes immediately instead of treating them as acceptable steady-state behavior
- `surfaced_exposure_supported` must be `true` for `shopify` and `wix`; legacy referral-only surfaces can remain excluded from readiness denominator

Suggested operator checks after deploy:
1. Confirm `/merchant/analytics/readiness-state` returns domain statuses and blocker arrays for a known merchant.
2. Confirm `/merchant/analytics/commerce-funnel` returns non-null `indexed_exposure`, `surfaced_exposure`, `clicked_rate`, `ordered_rate`.
3. Confirm `/merchant/analytics/commerce-funnel/issues` returns stable diagnostic codes and at least one sample interaction id when issues exist.
4. From one sample interaction id, call `/merchant/analytics/commerce-interactions/{interaction_id}` and verify the event timeline is ordered and merchant-scoped.

Rollback/cutover interpretation:
- `Stage A`
  - schema + dual-write + hidden portal drilldown can ship without switching merchant eligibility
- `Stage B`
  - 5-merchant cohort validation should include at least `3 Shopify + 2 Wix`
- `Stage C`
  - readiness policy replaces manual merchant allowlists for Discover/Signals/Execute
- `Stage D`
  - remove fake metrics, single-merchant alpha loaders, and any legacy funnel logic that is write-only / not read anymore

If the readiness/signals UI regresses but primary product diagnosis still works:
- keep the merchant product optimization lane live
- disable or hide the merchant analytics drilldown before reverting core catalog or execute paths
- prefer rollback to the old analytics read path while preserving new writes for later replay

## Public Execute API
The commerce execute surface now has a public agent contract in addition to the existing internal readiness routes.

Endpoints:
- `POST /agent/v2/commerce/checkouts`
- `POST /agent/v2/commerce/checkouts/{checkout_id}/payment-intent`
- `GET /agent/v2/commerce/checkouts/{checkout_id}/status`
- `POST /agent/v2/commerce/checkouts/{checkout_id}/refunds`
- `POST /agent/v2/commerce/checkouts/{checkout_id}/returns`

Implementation:
- route surface: [agent_commerce.py](/Users/pengchydan/dev/_worktrees/pivota-backend-celestial-four-domains-default-on-20260330/routes/agent_commerce.py)
- the public contract requires `interaction_id` plus normalized item references
- Shopify uses the readiness/native PSP orchestration already present
- Wix shares the same public contract; return sync can still report `pending_external_platform` until full adapter automation lands

Operator checks:
1. Validate create-checkout returns `checkout_id`, `platform`, `status`, and either `payment_url` or `client_secret`.
2. Validate payment-intent fetch does not mint a second order and only reflects the existing checkout action.
3. Validate refund and return actions append ledger events and do not break merchant authorization boundaries.

## Real Click Order Funnel Seed
Use this when the goal is to move the merchant commerce funnel off `clicked_exposure=0` and `ordered_conversion=0` with one real attributed sample, without doing real paid/refund yet.

Why this is separate from readiness payment signoff:
- readiness-owned checkout/session scripts prove payment and merchant write-back convergence
- they do not automatically create a real public click attribution edge for the merchant funnel
- this run must start from `POST /api/links/resolve`, then carry the same `pvt_*` values into `POST /agent/v2/commerce/checkouts`

Operator script:
- [smoke_real_click_order_funnel_signoff.py](/Users/pengchydan/dev/_worktrees/pivota-backend-celestial-four-domains-default-on-20260330/scripts/smoke_real_click_order_funnel_signoff.py)

Default target:
- API base: `https://api.pivota.cc`
- merchant: `merch_efbc46b4619cfbdf`
- click surface: `ucp`
- analytics read: merchant total by default

Required auth:
- internal readiness key for `GET /internal/readiness/merchants/{merchant_id}/report` and `/exports/{surface}`
- agent API key for `POST /agent/v2/commerce/checkouts` and `GET /agent/v2/commerce/checkouts/{checkout_id}/status`
- merchant JWT for `GET /merchant/analytics/commerce-funnel`, `/issues`, `/commerce-interactions/{interaction_id}`

Execution:
```bash
python3 scripts/smoke_real_click_order_funnel_signoff.py \
  --base-url https://api.pivota.cc \
  --merchant-id merch_efbc46b4619cfbdf \
  --surface ucp \
  --internal-key "$READINESS_INTERNAL_API_KEY" \
  --agent-api-key "$SHOP_GATEWAY_AGENT_API_KEY" \
  --merchant-jwt "$MERCHANT_JWT" \
  --output-json output/click-order-funnel-signoff/click-order-funnel-signoff.json \
  --output-md output/click-order-funnel-signoff/click-order-funnel-signoff.md
```

Current prod status as of `2026-03-30T13:26:59Z`:
- the default command above is now valid on prod for this merchant
- the script now defaults the `/api/links/resolve` `skuId` candidate to the selected readiness `variant_id`, so operators do not need to pass `--sku-id` for normal `sku`-scoped rules
- prod now has a published `market=US`, `tool=ucp`, `scope=sku` outbound rule set covering all current `ucp/exported` variants for this merchant
- rule-set rollout artifacts:
  - [summary.json](/Users/pengchydan/dev/_worktrees/pivota-backend-celestial-four-domains-default-on-20260330/output/outbound-link-rules/prod-merchant-ucp-sku-20260330T132009Z/summary.json)
  - [ucp-sku-rules.csv](/Users/pengchydan/dev/_worktrees/pivota-backend-celestial-four-domains-default-on-20260330/output/outbound-link-rules/prod-merchant-ucp-sku-20260330T132009Z/ucp-sku-rules.csv)
  - [import-response.json](/Users/pengchydan/dev/_worktrees/pivota-backend-celestial-four-domains-default-on-20260330/output/outbound-link-rules/prod-merchant-ucp-sku-20260330T132009Z/import-response.json)
  - [publish-response.json](/Users/pengchydan/dev/_worktrees/pivota-backend-celestial-four-domains-default-on-20260330/output/outbound-link-rules/prod-merchant-ucp-sku-20260330T132009Z/publish-response.json)
- rollout summary:
  - `created = 2098`
  - `updated = 0`
  - `published = 2098`
  - storefront domain = `jwx893-fz.myshopify.com`

Observed default prod rerun on `2026-03-30T13:25:40Z`:
```bash
python3 scripts/smoke_real_click_order_funnel_signoff.py \
  --base-url https://api.pivota.cc \
  --merchant-id merch_efbc46b4619cfbdf \
  --surface ucp \
  --internal-key "$READINESS_INTERNAL_API_KEY" \
  --agent-api-key "$SHOP_GATEWAY_AGENT_API_KEY" \
  --merchant-jwt "$MERCHANT_JWT" \
  --run-id prod-default-ucp-20260330T132540Z \
  --output-json output/click-order-funnel-signoff/prod-default-ucp-20260330T132540Z/click-order-funnel-signoff.json \
  --output-md output/click-order-funnel-signoff/prod-default-ucp-20260330T132540Z/click-order-funnel-signoff.md
```

Observed default prod outcome on `2026-03-30T13:25:40Z`:
- evidence:
  - [click-order-funnel-signoff.md](/Users/pengchydan/dev/_worktrees/pivota-backend-celestial-four-domains-default-on-20260330/output/click-order-funnel-signoff/prod-default-ucp-20260330T132540Z/click-order-funnel-signoff.md)
  - [click-order-funnel-signoff.json](/Users/pengchydan/dev/_worktrees/pivota-backend-celestial-four-domains-default-on-20260330/output/click-order-funnel-signoff/prod-default-ucp-20260330T132540Z/click-order-funnel-signoff.json)
- verdict:
  - `overall_ok = true`
  - `resolve_ok = true`
  - `click_ok = true`
  - `order_ok = true`
  - `issues_ok = true`
  - `trace_ok = true`
- emitted ids:
  - `click_id = clk_6b3619f8d5f44a3a905f0760`
  - `interaction_id = int_3a3062b605155a5ce39c2951`
  - `checkout_id = ORD_34142F2F5B423A8D`
  - `order_id = ORD_34142F2F5B423A8D`
- funnel delta:
  - `clicked_exposure += 1`
  - `ordered_conversion += 1`
  - `refunded_orders += 0`
- runtime note:
  - `readiness_export_full` still timed out after `30s`
  - the script recovered via `summary_only=true` export sample + full readiness report fallback
  - `commerce_checkout_create_attempt_1` returned `503`
  - the script retry logic succeeded on `commerce_checkout_create_attempt_2`

Historical pre-rule-set override rerun on `2026-03-30T13:04:16Z`:
```bash
python3 scripts/smoke_real_click_order_funnel_signoff.py \
  --base-url https://api.pivota.cc \
  --merchant-id merch_efbc46b4619cfbdf \
  --surface ucp \
  --tool truthfulness_canary_20260330t104628z-c5ebe982 \
  --sku-id 52438737846600 \
  --product-id 9886500749640 \
  --variant-id 52438737846600 \
  --brand Winona \
  --category Serum \
  --title "Winona Soothing Repair Serum" \
  --unit-price 29 \
  --currency EUR \
  --internal-key "$READINESS_INTERNAL_API_KEY" \
  --agent-api-key "$SHOP_GATEWAY_AGENT_API_KEY" \
  --merchant-jwt "$MERCHANT_JWT" \
  --run-id prod-sku-override-20260330T130416Z \
  --output-json output/click-order-funnel-signoff/prod-sku-override-20260330T130416Z/click-order-funnel-signoff.json \
  --output-md output/click-order-funnel-signoff/prod-sku-override-20260330T130416Z/click-order-funnel-signoff.md
```

Historical pre-rule-set outcome on `2026-03-30T13:04:16Z`:
- evidence:
  - [click-order-funnel-signoff.md](/Users/pengchydan/dev/_worktrees/pivota-backend-celestial-four-domains-default-on-20260330/output/click-order-funnel-signoff/prod-sku-override-20260330T130416Z/click-order-funnel-signoff.md)
  - [click-order-funnel-signoff.json](/Users/pengchydan/dev/_worktrees/pivota-backend-celestial-four-domains-default-on-20260330/output/click-order-funnel-signoff/prod-sku-override-20260330T130416Z/click-order-funnel-signoff.json)
- verdict:
  - `overall_ok = true`
  - `resolve_ok = true`
  - `click_ok = true`
  - `order_ok = true`
  - `issues_ok = true`
  - `trace_ok = true`
- emitted ids:
  - `click_id = clk_45ba77dd2db34aee90a19104`
  - `interaction_id = int_a1d97479aafbdca6cd0fbe6c`
  - `checkout_id = ORD_4C458E850DFA9EA4`
  - `order_id = ORD_4C458E850DFA9EA4`
- funnel delta:
  - `clicked_exposure += 1`
  - `ordered_conversion += 1`
  - `refunded_orders += 0`
- runtime note:
  - `commerce_checkout_create_attempt_1` returned `503`
  - the script retry logic succeeded on `commerce_checkout_create_attempt_2`

What the script does:
1. Calls `/health` and saves the active build/deployment evidence from the live API runtime.
2. Calls merchant `/analytics/readiness-state` plus internal readiness report/export to confirm the merchant still has at least one ready offer.
   If the full readiness export is unavailable or too slow, the script falls back to `summary_only=true` export samples plus the full readiness report to recover one ready product/variant automatically.
3. Resolves a real outbound redirect via `/api/links/resolve`, carrying `merchantId`, `surface`, and stable canonical `pvt_product_id` / `pvt_variant_id`.
4. Optionally records `/api/links/impression`, then performs one real `GET /r?token=...` click with redirects disabled so the click row is written but the script stays local.
5. Derives the `interaction_id` from that new `click_id` and reuses it in `POST /agent/v2/commerce/checkouts`.
   This keeps `surface.click`, `checkout.created`, and `order.created` on the same interaction trace instead of splitting them across multiple ids.
6. Polls merchant funnel + trace until `clicked_exposure` and `ordered_conversion` both increase by at least `1`, while `refunded_orders` stays `0`.
7. Verifies the new interaction is not sampled under `TRACE_BROKEN` or `UNATTRIBUTED_ORDER`.

Expected round-1 output:
- one real `click_id`
- one real `interaction_id`
- one `checkout_id` / `order_id`
- one captured `payment_action`
- funnel delta:
  - `clicked_exposure += 1`
  - `ordered_conversion += 1`
  - `refunded_orders += 0`

Round-2 handoff:
- preserve the emitted `order_id`, `checkout_id`, `click_id`, `interaction_id`, and `payment_action`
- if the next run switches to real paid/refund, continue from this sample or rerun the same script path and then finish:
  - paid-state convergence
  - `POST /agent/v2/commerce/checkouts/{checkout_id}/refunds`
  - post-refund funnel verification

## Follow-On Phase Status
Current follow-on status as of `2026-03-30 UTC`:

- `Phase A` current-environment gate is green.
  - Evidence:
    - [commerce-signoff-batch.md](/Users/pengchydan/dev/_worktrees/pivota-backend-hyaluronic-aliases-20260325/output/commerce-signoff/prod-batch-20260329-current-gate/commerce-signoff-batch.md)
    - [commerce-signoff-batch.json](/Users/pengchydan/dev/_worktrees/pivota-backend-hyaluronic-aliases-20260325/output/commerce-signoff/prod-batch-20260329-current-gate/commerce-signoff-batch.json)
  - Current gate outcome:
    - `overall_ok = true`
    - `enabled_cases = 1`
    - `passed_cases = 1`
    - `min_enabled_cases = 1`
    - required semantic coverage for the current environment: `beauty`
  - The long-term cohort target remains open by design:
    - `target_enabled_cases = 5`
    - target semantic coverage: `beauty`, `generic_default`, `fragrance`
    - target platform mix: `shopify >= 3`, `wix >= 2`
    - target readiness domains per enabled merchant: `foundation`, `discover`, `signals`, `execute`

- `Phase B` supervised paid terminal-state signoff is also green.
  - This was executed with a fresh readiness-owned Stripe Checkout session, not a reused historical PSP reference.
  - Execution shape:
    - create readiness-owned checkout + local order
    - mint fresh Stripe Checkout session
    - complete the real payment externally
    - run read-only `payment-status-sync` verification
    - run `payment-bridge`
    - run `refund`
    - confirm post-refund audit convergence
  - Evidence:
    - [bridge-paid-reference-signoff.md](/Users/pengchydan/dev/_worktrees/pivota-backend-hyaluronic-aliases-20260325/output/phase-b-signoff/prod-bridge-paid-reference-complete-20260330T003949Z/bridge-paid-reference-signoff.md)
    - [bridge-paid-reference-signoff.json](/Users/pengchydan/dev/_worktrees/pivota-backend-hyaluronic-aliases-20260325/output/phase-b-signoff/prod-bridge-paid-reference-complete-20260330T003949Z/bridge-paid-reference-signoff.json)
  - Outcome:
    - `normalized_payment_status = paid`
    - `bridge payment_status = paid`
    - `refund_eligible_after_bridge = true`
    - `refund_status = success`
    - `post_refund_payment_status = refunded`
    - `overall_ok = true`

Operational interpretation:
- the standard Celestial pivot release close-out remains separate from these follow-on phases
- there is no remaining baseline backend-runtime blocker in the follow-on path
- what remains open after `2026-03-30 UTC` is cohort expansion and repeatability, not whether the supervised paid path can converge end to end
