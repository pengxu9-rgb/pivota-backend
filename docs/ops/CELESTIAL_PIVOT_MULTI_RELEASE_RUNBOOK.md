# Celestial Pivot Multi Release Runbook

## Scope
- Production target: `find_products_multi`
- Initial serve canary: `shopping_agent`, `page=1`
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
- Release evidence builder: [build_pivot_release_evidence.py](/Users/pengchydan/dev/_worktrees/pivota-backend-hyaluronic-aliases-20260325/scripts/build_pivot_release_evidence.py)
- Bundle orchestrator: [run_pivot_release_bundle.py](/Users/pengchydan/dev/_worktrees/pivota-backend-hyaluronic-aliases-20260325/scripts/run_pivot_release_bundle.py)
- Grafana dashboard: [celestial_pivot_multi_release_dashboard.json](/Users/pengchydan/dev/_worktrees/pivota-backend-hyaluronic-aliases-20260325/observability/grafana/celestial_pivot_multi_release_dashboard.json)
- Alert rules: [celestial_pivot_multi_release_alerts.yml](/Users/pengchydan/dev/_worktrees/pivota-backend-hyaluronic-aliases-20260325/observability/prometheus/celestial_pivot_multi_release_alerts.yml)

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
8. Build one evidence bundle with `build_pivot_release_evidence.py`.
9. Review Grafana dashboard and Prometheus alerts.
10. If shadow metrics pass threshold, enable `serve=true` for `shopping_agent` only.

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
  --catalog-sync-job-smoke
```

## Production Shadow Bundle Command
```bash
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
  --catalog-sync-wait-seconds 60 \
  --search-chain-probe
```

Note:
- `release gate` + direct `catalog/pivot smoke` are the blocking production shadow checks for Celestial Pivot.
- `search_chain_inventory_probe` is legacy parity evidence by default. Older `agent_v1` / `api_gateway` ingress issues should not block pivot shadow signoff unless that parity lane is part of the active release scope.

## Shadow Pass Criteria
- `top1_same >= 0.85`
- no sustained `catalog_pivot_shadow_no_result_mismatch_total`
- no sustained `catalog_pivot_shadow_bad_price_anomaly_total`
- `catalog_search_latency_seconds{path="pivot_semantic_core_multi"}` p95 within SLO
- `pivot_rollout_mode=shadow` and `pivot_rollout_guard_passed=true` visible in `route_health`

## Serve Canary Abort Conditions
- `top1_same` drops below threshold
- no-result mismatch rises materially
- internal/external mix drift is sustained
- price anomaly alerts fire
- latency or error budget regresses

## Evidence Bundle
- migration apply logs
- backfill verify JSON + Markdown
- staging release-gate JSON + Markdown
- prod shadow release-gate JSON + Markdown
- staging/prod catalog-pivot smoke JSON + Markdown
- staging/prod search-chain probe JSON + Markdown
- consolidated evidence JSON + Markdown
- Grafana screenshot/export
- deployed commit from `X-Service-Commit`
