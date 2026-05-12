-- P2.5: per-LLM-probe cost + performance telemetry.
--
-- Each row records ONE invocation of a probe call against ONE LLM
-- provider. Audit-level cost rollup (cost_summary_jsonb on
-- merchant_audit_runs) is computed by aggregating these rows by
-- audit_run_id at completion.
--
-- Powers:
--   - Real numbers for merchant_audit_runs.cost_summary_jsonb
--     (P2.2 worker shipped a placeholder; P2.5 wires the truth)
--   - Per-merchant + per-day cost cap engagement (orchestrator
--     reads SUM(cost_usd) WHERE merchant_id=X + date_trunc('day',
--     completed_at)=today)
--   - Empirical suitability calibration (a follow-up reads which
--     provider produced the most-cited results per scan_mode and
--     adjusts provider_registry.suitable_for_scan_modes ratings)
--
-- Recording is best-effort: if persistence fails the probe still
-- returns its result. Telemetry gaps are acceptable; broken probes
-- are not.
--
-- Idempotent — safe to re-run.

CREATE TABLE IF NOT EXISTS llm_probe_runs (
    probe_run_id        UUID PRIMARY KEY,
    -- The provider id from services/llm_providers/provider_registry.py
    -- (gemini | deepseek | chatgpt | claude | mock).
    provider            TEXT NOT NULL,
    -- The scan_mode the probe was run in (open_product_visibility_test,
    -- merchant_store_attribution_test, category_visibility_test,
    -- corporate_intel_lookup, form_factor_classification, etc.).
    scan_mode           TEXT NOT NULL,
    -- Tenant + parent audit. NULL audit_run_id allowed for one-off
    -- standalone probes (BD interactive lookup, debugging, etc.).
    merchant_id         TEXT,
    audit_run_id        UUID,
    -- Performance.
    latency_ms          INTEGER,
    -- Cost. Token counts populated when the provider returns usage
    -- data; cost_usd computed as
    -- (input_tokens * cost_per_1k_input + output_tokens * cost_per_1k_output) / 1000
    -- using rates from provider_registry.
    input_tokens        INTEGER,
    output_tokens       INTEGER,
    cost_usd            NUMERIC(10, 6),
    -- Outcome. status in (succeeded | failed | rate_limited | timeout |
    -- cost_capped | mock_fallback). The orchestrator uses status to
    -- decide whether to fall back to a sibling provider.
    status              TEXT NOT NULL,
    error_message       TEXT,
    completed_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE llm_probe_runs IS
  'P2.5: per-LLM-probe cost + performance telemetry. One row per probe invocation. Aggregated by audit_run_id into merchant_audit_runs.cost_summary_jsonb.';

-- Index for the cost-rollup query the worker runs at audit completion:
-- SELECT provider, SUM(cost_usd), SUM(input_tokens), ... FROM llm_probe_runs
-- WHERE audit_run_id = $1 GROUP BY provider
CREATE INDEX IF NOT EXISTS idx_llm_probe_runs_audit_run
  ON llm_probe_runs (audit_run_id)
  WHERE audit_run_id IS NOT NULL;

-- Index for the per-merchant-per-day cost cap check the orchestrator
-- runs before each call:
-- SELECT SUM(cost_usd) FROM llm_probe_runs
-- WHERE merchant_id = $1 AND completed_at >= date_trunc('day', NOW())
CREATE INDEX IF NOT EXISTS idx_llm_probe_runs_merchant_day
  ON llm_probe_runs (merchant_id, completed_at)
  WHERE merchant_id IS NOT NULL;

-- Index for the global cost cap check (every call):
-- SELECT SUM(cost_usd) FROM llm_probe_runs
-- WHERE completed_at >= date_trunc('day', NOW())
CREATE INDEX IF NOT EXISTS idx_llm_probe_runs_completed_at
  ON llm_probe_runs (completed_at);
