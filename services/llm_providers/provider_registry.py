"""LLM provider registry (PR-7-orchestrator).

Single source of truth for which LLM providers Pivota supports,
their capabilities per scan_mode, cost metadata, and runtime health.
The orchestrator (`orchestrator.py`) reads from this registry to
make routing decisions; new providers (PR-3b ChatGPT/Claude, future
post-Q3 providers) plug in via `register_provider()` without
touching call sites.

**Registry schema:**

```python
LLMProvider(
    id="deepseek",
    display_name="Deepseek V4",
    backend="deepseek_local",        # how to dispatch — local Python
                                       # call vs upstream PIVOTA-Agent
                                       # HTTP call
    capabilities={
        "grounded_search": "supported",   # supported | limited | none
        "structured_json_output": "native",
        "max_context_tokens": 64_000,
        "supports_tool_use": True,
    },
    cost_per_1k_input_tokens_usd=0.00014,
    cost_per_1k_output_tokens_usd=0.00028,
    avg_latency_ms_per_probe=1800,
    rate_limit_per_minute=120,
    health_status="healthy",         # healthy | degraded | unavailable
                                       # (updated by health-check loop)
    suitable_for_scan_modes={
        "open_product_visibility_test": "good",
        "merchant_store_attribution_test": "good",
        "category_visibility_test": "excellent",
        "corporate_intel_lookup": "good",
    },
)
```

**Suitability ratings** ("excellent" > "good" > "limited" > "none"):
The orchestrator consumes these to rank providers per scan_mode.
Ratings reflect both empirical performance (which engine has best
grounded retrieval for this query type) and cost-efficiency
(cheaper providers rated higher when capability is comparable).

**Backwards compat:** Pre-existing callers passing
`provider="gemini"` or `provider="deepseek"` continue to route to
that specific provider directly — the orchestrator only engages
when caller passes `provider="auto"`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# Suitability rating constants for clarity at call sites.
SUITABILITY_EXCELLENT = "excellent"
SUITABILITY_GOOD = "good"
SUITABILITY_LIMITED = "limited"
SUITABILITY_NONE = "none"

# Numerical weights for ranking (higher = better)
_SUITABILITY_WEIGHTS: Dict[str, int] = {
    SUITABILITY_EXCELLENT: 3,
    SUITABILITY_GOOD: 2,
    SUITABILITY_LIMITED: 1,
    SUITABILITY_NONE: 0,
}


# Health status constants
HEALTH_HEALTHY = "healthy"
HEALTH_DEGRADED = "degraded"
HEALTH_UNAVAILABLE = "unavailable"


# Backend constants — determines how the dispatcher actually invokes
# the provider's probe.
BACKEND_DEEPSEEK_LOCAL = "deepseek_local"
BACKEND_PIVOTA_AGENT_UPSTREAM = "pivota_agent_upstream"
BACKEND_LOCAL_MOCK = "local_mock"


@dataclass
class LLMProvider:
    id: str
    display_name: str
    backend: str
    cost_per_1k_input_tokens_usd: float
    cost_per_1k_output_tokens_usd: float
    avg_latency_ms_per_probe: int
    rate_limit_per_minute: int
    health_status: str = HEALTH_HEALTHY
    capabilities: Dict[str, Any] = field(default_factory=dict)
    suitable_for_scan_modes: Dict[str, str] = field(default_factory=dict)

    def suitability_for(self, scan_mode: str) -> str:
        """Return suitability rating for a scan_mode. Defaults to
        SUITABILITY_NONE if the provider has no rating for this mode
        (caller will exclude it from ranking)."""
        return self.suitable_for_scan_modes.get(scan_mode, SUITABILITY_NONE)

    def suitability_weight(self, scan_mode: str) -> int:
        """Numerical weight for ranking. 0 = unsuitable; higher = better."""
        return _SUITABILITY_WEIGHTS.get(self.suitability_for(scan_mode), 0)

    def estimated_cost_per_probe_usd(
        self, *, expected_input_tokens: int = 500,
        expected_output_tokens: int = 300,
    ) -> float:
        """Rough cost estimate for one probe call. Uses the
        provider's per-1k-token rates against expected token counts.
        Default expected tokens are sized for an audit probe (system
        prompt + query + structured JSON response).
        """
        return (
            (expected_input_tokens / 1000.0) * self.cost_per_1k_input_tokens_usd
            + (expected_output_tokens / 1000.0) * self.cost_per_1k_output_tokens_usd
        )


# Module-level mutable registry. Tests can clear via
# `_reset_registry_for_test()`. Production callers use
# `list_providers()` / `get_provider()` / `register_provider()`.
_REGISTRY: Dict[str, LLMProvider] = {}


def register_provider(provider: LLMProvider) -> None:
    """Add a provider to the registry. Replaces existing entry with
    same id (allows test fixtures + dynamic capability updates from
    health-check loop)."""
    if not isinstance(provider, LLMProvider):
        raise TypeError(
            f"register_provider requires an LLMProvider; got {type(provider)}"
        )
    _REGISTRY[provider.id] = provider


def get_provider(provider_id: str) -> Optional[LLMProvider]:
    """Look up a provider by id. None when not registered."""
    return _REGISTRY.get(provider_id)


def list_providers(
    *,
    healthy_only: bool = True,
) -> List[LLMProvider]:
    """List registered providers. Default filters to healthy
    providers only — orchestrator never picks a degraded /
    unavailable provider unless explicitly disabled."""
    providers = list(_REGISTRY.values())
    if healthy_only:
        providers = [p for p in providers if p.health_status == HEALTH_HEALTHY]
    return providers


def _reset_registry_for_test() -> None:
    """Clear the registry. Tests should call this in setup so they
    don't leak state across modules."""
    _REGISTRY.clear()


def update_provider_health(provider_id: str, health_status: str) -> None:
    """Mark a provider as healthy/degraded/unavailable. Called by the
    health-check loop or by error handlers when a provider returns
    repeated 5xx / timeouts. Orchestrator filters degraded providers
    out of selection automatically."""
    provider = _REGISTRY.get(provider_id)
    if provider is None:
        return
    if health_status not in (HEALTH_HEALTHY, HEALTH_DEGRADED, HEALTH_UNAVAILABLE):
        return
    provider.health_status = health_status


# ---------------------------------------------------------------------------
# Default provider seed — registered at module import time.
# ---------------------------------------------------------------------------

def _seed_default_providers() -> None:
    """Pre-register the providers Pivota supports today. New
    providers (ChatGPT, Claude, etc.) get appended here as they
    ship; the orchestrator picks them up automatically."""
    # Gemini (via PIVOTA-Agent codex stack)
    register_provider(LLMProvider(
        id="gemini",
        display_name="Google Gemini Grounded",
        backend=BACKEND_PIVOTA_AGENT_UPSTREAM,
        cost_per_1k_input_tokens_usd=0.00035,    # Gemini 1.5 Flash with grounding
        cost_per_1k_output_tokens_usd=0.00105,
        avg_latency_ms_per_probe=2400,
        rate_limit_per_minute=60,
        health_status=HEALTH_HEALTHY,
        capabilities={
            "grounded_search": "supported",
            "structured_json_output": "native",
            "max_context_tokens": 1_000_000,
            "supports_tool_use": True,
        },
        suitable_for_scan_modes={
            "open_product_visibility_test": SUITABILITY_EXCELLENT,
            "merchant_store_attribution_test": SUITABILITY_EXCELLENT,
            "category_visibility_test": SUITABILITY_EXCELLENT,
            "corporate_intel_lookup": SUITABILITY_EXCELLENT,
            "form_factor_classification": SUITABILITY_GOOD,
        },
    ))

    # Deepseek V4 (backend-direct, post-PR-3a)
    register_provider(LLMProvider(
        id="deepseek",
        display_name="Deepseek V4",
        backend=BACKEND_DEEPSEEK_LOCAL,
        cost_per_1k_input_tokens_usd=0.00014,    # Deepseek-Chat current rate
        cost_per_1k_output_tokens_usd=0.00028,
        avg_latency_ms_per_probe=1800,
        rate_limit_per_minute=120,
        health_status=HEALTH_HEALTHY,
        capabilities={
            "grounded_search": "limited",       # web search support varies
            "structured_json_output": "native",
            "max_context_tokens": 64_000,
            "supports_tool_use": True,
        },
        suitable_for_scan_modes={
            "open_product_visibility_test": SUITABILITY_GOOD,
            "merchant_store_attribution_test": SUITABILITY_GOOD,
            # Deepseek's web search is less mature than Gemini for
            # editorial-citation-style queries; downrate accordingly.
            "category_visibility_test": SUITABILITY_GOOD,
            # Cheap classification: Deepseek wins on cost + native
            # structured output for non-grounded classification.
            "form_factor_classification": SUITABILITY_EXCELLENT,
            "corporate_intel_lookup": SUITABILITY_GOOD,
        },
    ))

    # Mock — for tests / dev environments without API keys
    register_provider(LLMProvider(
        id="mock",
        display_name="Local Mock",
        backend=BACKEND_LOCAL_MOCK,
        cost_per_1k_input_tokens_usd=0.0,
        cost_per_1k_output_tokens_usd=0.0,
        avg_latency_ms_per_probe=10,
        rate_limit_per_minute=10_000,
        health_status=HEALTH_HEALTHY,
        capabilities={
            "grounded_search": "none",
            "structured_json_output": "native",
            "max_context_tokens": 8_000,
            "supports_tool_use": False,
        },
        suitable_for_scan_modes={},  # never suitable for real probes
    ))


# Seed at import time
_seed_default_providers()
