# Celestial Pivot Multi Release Runbook

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
- Employee/admin JWT mint helper: [mint_employee_jwt.py](/Users/pengchydan/dev/_worktrees/pivota-backend-hyaluronic-aliases-20260325/scripts/mint_employee_jwt.py)
- Generic commerce shadow audit: [commerce_shadow_audit.py](/Users/pengchydan/dev/_worktrees/pivota-backend-hyaluronic-aliases-20260325/scripts/commerce_shadow_audit.py)
- Generic commerce shadow compare: [compare_commerce_shadow_audit.py](/Users/pengchydan/dev/_worktrees/pivota-backend-hyaluronic-aliases-20260325/scripts/compare_commerce_shadow_audit.py)
- Release evidence builder: [build_pivot_release_evidence.py](/Users/pengchydan/dev/_worktrees/pivota-backend-hyaluronic-aliases-20260325/scripts/build_pivot_release_evidence.py)
- Bundle orchestrator: [run_pivot_release_bundle.py](/Users/pengchydan/dev/_worktrees/pivota-backend-hyaluronic-aliases-20260325/scripts/run_pivot_release_bundle.py)
- Grafana dashboard: [celestial_pivot_multi_release_dashboard.json](/Users/pengchydan/dev/_worktrees/pivota-backend-hyaluronic-aliases-20260325/observability/grafana/celestial_pivot_multi_release_dashboard.json)
- Alert rules: [celestial_pivot_multi_release_alerts.yml](/Users/pengchydan/dev/_worktrees/pivota-backend-hyaluronic-aliases-20260325/observability/prometheus/celestial_pivot_multi_release_alerts.yml)

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
  --smoke-base-url https://web-production-fedb.up.railway.app \
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
  --pivot-base-url https://web-production-fedb.up.railway.app \
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
  --pivot-base-url https://web-production-fedb.up.railway.app \
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
  --pivot-base-url https://web-production-fedb.up.railway.app \
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
