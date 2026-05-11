"""LLM probe orchestrator (PR-7-orchestrator).

Routes probe calls across registered LLM providers based on
strategy + cost + capability + health. Caller asks for
`provider="auto"` and the orchestrator decides which provider(s)
actually run; existing callers passing a specific provider id
continue to bypass the orchestrator and route directly.

**Strategies:**

  - `single_best` (default for most scan modes) — pick the highest-
    rated single provider for this scan_mode. Ties broken by
    cost (cheaper wins) then by latency (faster wins).
  - `cost_optimized` — pick the cheapest provider rated at least
    "good" for this scan_mode. Useful for high-volume cheap
    classification work (form factor lookup, etc.).
  - `cross_validate` — pick the top 2 providers in parallel; both
    results returned to caller for comparison. Highest-cost
    strategy; reserved for high-stakes audits where false-negative
    risk is high.
  - `fallback_chain` — primary provider plus secondary cascade
    candidates. Caller invokes primary first; on failure, cascades
    to secondary. Mid-cost; defends against single-provider outage.

**Cost guard:**

Per-merchant + per-day cost cap (env vars
`AGENT_CENTER_LLM_DAILY_COST_USD_PER_MERCHANT` default $5,
`AGENT_CENTER_LLM_DAILY_COST_USD_GLOBAL` default $200). When cap
reached, orchestrator falls back to the cheapest healthy provider
regardless of suitability rating, and surfaces
`upstream_status.cost_capped: true` so the caller can decide
whether to honor the result. The cap-tracking storage lives in
`db.llm_probe_runs` (recorded by the dispatcher after each call).

**Backwards compat:**

  - `provider="gemini"` / `provider="deepseek"` / etc. → bypass
    orchestrator entirely, dispatch to that specific provider
    (existing behavior preserved)
  - `provider="auto"` → engage orchestrator with default strategy
    `single_best`
  - `provider="auto:cost_optimized"` → engage with explicit
    strategy override

**Telemetry:**

Each orchestration decision is logged via the dispatcher's
`record_probe_run` hook (see `db/llm_probe_runs.py`) for cost +
performance tracking over time. Used by the registry's empirical
suitability calibration loop (a follow-up PR can update
`suitable_for_scan_modes` ratings based on which provider produced
the most-cited results in completed audits).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from services.llm_providers.provider_registry import (
    LLMProvider,
    SUITABILITY_GOOD,
    list_providers,
    get_provider,
)

logger = logging.getLogger(__name__)


# Strategy constants
STRATEGY_SINGLE_BEST = "single_best"
STRATEGY_COST_OPTIMIZED = "cost_optimized"
STRATEGY_CROSS_VALIDATE = "cross_validate"
STRATEGY_FALLBACK_CHAIN = "fallback_chain"

VALID_STRATEGIES = {
    STRATEGY_SINGLE_BEST,
    STRATEGY_COST_OPTIMIZED,
    STRATEGY_CROSS_VALIDATE,
    STRATEGY_FALLBACK_CHAIN,
}


def parse_provider_spec(provider_spec: str) -> Tuple[str, Optional[str]]:
    """Parse a provider= spec from caller. Returns (provider_id_or_auto,
    strategy_override).

    Examples:
      "gemini"                   -> ("gemini", None)        # direct route
      "deepseek"                 -> ("deepseek", None)      # direct route
      "auto"                     -> ("auto", None)          # default strategy
      "auto:cost_optimized"      -> ("auto", "cost_optimized")
      "auto:cross_validate"      -> ("auto", "cross_validate")
    """
    spec = (provider_spec or "").strip()
    if ":" not in spec:
        return spec, None
    provider_id, _, strategy = spec.partition(":")
    return provider_id.strip(), strategy.strip() or None


class OrchestratorSelectionError(RuntimeError):
    """Raised when the orchestrator can't pick any provider for the
    requested scan_mode (no healthy providers, or all rated 'none')."""


def _rank_providers_for_scan_mode(
    providers: List[LLMProvider],
    scan_mode: str,
) -> List[LLMProvider]:
    """Return providers ranked by suitability (higher first), then by
    cost (cheaper wins ties), then by latency. Excludes providers
    rated 'none' for this scan mode."""
    candidates = [
        p for p in providers
        if p.suitability_weight(scan_mode) > 0
    ]

    def sort_key(p: LLMProvider) -> Tuple[int, float, int]:
        # Negative suitability so "higher" weight sorts first
        return (
            -p.suitability_weight(scan_mode),
            p.estimated_cost_per_probe_usd(),
            p.avg_latency_ms_per_probe,
        )
    return sorted(candidates, key=sort_key)


def _rank_by_cost(
    providers: List[LLMProvider],
    scan_mode: str,
    *,
    min_suitability_weight: int = 2,  # SUITABILITY_GOOD = 2
) -> List[LLMProvider]:
    """Return providers rated at least `good` for this scan_mode,
    sorted by cost ascending (cheapest first)."""
    candidates = [
        p for p in providers
        if p.suitability_weight(scan_mode) >= min_suitability_weight
    ]
    return sorted(candidates, key=lambda p: p.estimated_cost_per_probe_usd())


def select_provider(
    *,
    scan_mode: str,
    strategy: str = STRATEGY_SINGLE_BEST,
    merchant_id: Optional[str] = None,
    cost_capped: bool = False,
) -> str:
    """Pick a single provider id for this scan_mode + strategy.

    `cost_capped=True` (set by the dispatcher when daily cap is hit)
    forces the cheapest healthy provider regardless of suitability —
    we'd rather emit a degraded result than refuse the audit.

    Raises OrchestratorSelectionError when no provider is available.
    """
    if strategy not in VALID_STRATEGIES:
        raise ValueError(
            f"Unknown orchestration strategy: {strategy!r}. "
            f"Valid: {sorted(VALID_STRATEGIES)}"
        )
    providers = list_providers(healthy_only=True)
    if not providers:
        raise OrchestratorSelectionError(
            "No healthy LLM providers registered."
        )

    if cost_capped:
        # Cap engaged: pick the absolute cheapest healthy provider,
        # even if it's "limited" or untrained for this scan mode.
        # Caller's response will carry cost_capped=true so consumer
        # knows the provider was downgraded.
        cheapest = min(
            providers,
            key=lambda p: p.estimated_cost_per_probe_usd(),
        )
        logger.warning(
            "orchestrator: cost cap engaged for merchant=%s scan_mode=%s; "
            "falling back to cheapest provider=%s",
            merchant_id, scan_mode, cheapest.id,
        )
        return cheapest.id

    if strategy == STRATEGY_COST_OPTIMIZED:
        ranked = _rank_by_cost(providers, scan_mode)
        if not ranked:
            raise OrchestratorSelectionError(
                f"No provider rated 'good' or better for "
                f"scan_mode={scan_mode!r}; consider expanding the "
                f"registry or relaxing the strategy."
            )
        return ranked[0].id

    # STRATEGY_SINGLE_BEST + STRATEGY_FALLBACK_CHAIN both pick
    # the top-ranked provider as primary
    ranked = _rank_providers_for_scan_mode(providers, scan_mode)
    if not ranked:
        raise OrchestratorSelectionError(
            f"No suitable provider for scan_mode={scan_mode!r}; "
            f"all providers rated 'none' or unavailable."
        )
    return ranked[0].id


def select_providers(
    *,
    scan_mode: str,
    strategy: str = STRATEGY_CROSS_VALIDATE,
    merchant_id: Optional[str] = None,
    max_providers: int = 2,
) -> List[str]:
    """Pick multiple providers for cross-validation or fallback
    cascade strategies.

    For STRATEGY_CROSS_VALIDATE: returns top N providers by
    suitability rank (caller invokes all in parallel).
    For STRATEGY_FALLBACK_CHAIN: returns ordered list (primary,
    secondary, tertiary) for the dispatcher's cascade logic.
    Other strategies fall through to single-provider list.
    """
    providers = list_providers(healthy_only=True)
    if not providers:
        raise OrchestratorSelectionError("No healthy LLM providers registered.")

    if strategy in (STRATEGY_CROSS_VALIDATE, STRATEGY_FALLBACK_CHAIN):
        ranked = _rank_providers_for_scan_mode(providers, scan_mode)
        if not ranked:
            raise OrchestratorSelectionError(
                f"No suitable provider for scan_mode={scan_mode!r}."
            )
        return [p.id for p in ranked[:max_providers]]

    # Single-provider strategies fall back to one-element list
    return [select_provider(
        scan_mode=scan_mode, strategy=strategy, merchant_id=merchant_id,
    )]


def estimate_strategy_cost_usd(
    *,
    scan_mode: str,
    strategy: str,
    expected_input_tokens: int = 500,
    expected_output_tokens: int = 300,
) -> float:
    """Estimate the cost of running this strategy for one probe.
    Used by the cost-cap layer to project per-merchant cost before
    actually executing the probe. Single-provider strategies cost
    one probe; cross_validate costs N probes."""
    try:
        provider_ids = select_providers(
            scan_mode=scan_mode, strategy=strategy,
        )
    except OrchestratorSelectionError:
        return 0.0
    total = 0.0
    for pid in provider_ids:
        provider = get_provider(pid)
        if provider:
            total += provider.estimated_cost_per_probe_usd(
                expected_input_tokens=expected_input_tokens,
                expected_output_tokens=expected_output_tokens,
            )
    return total
