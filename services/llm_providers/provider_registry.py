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

# Single source of truth for provider token prices is the billing config
# (config/provider_credit_rates.json), loaded via this leaf module (stdlib-only
# deps, so no import cycle). The registry DERIVES its per-1k telemetry rates from
# it (#1508) so llm_probe_runs.cost_usd can't drift off what we actually bill.
from services.provider_credit_rates import provider_rate_entry


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
    # Provider-level fallback rates. Used when the active model has no
    # entry in `model_rates`, or for orchestrator ranking that doesn't
    # know the runtime model. Should be the headline SKU's rate so
    # ranking decisions stay representative.
    cost_per_1k_input_tokens_usd: float
    cost_per_1k_output_tokens_usd: float
    avg_latency_ms_per_probe: int
    rate_limit_per_minute: int
    health_status: str = HEALTH_HEALTHY
    capabilities: Dict[str, Any] = field(default_factory=dict)
    suitable_for_scan_modes: Dict[str, str] = field(default_factory=dict)
    # Per-model overrides. Keys are concrete model ids (e.g.
    # "deepseek-chat", "deepseek-reasoner"); values are
    # {"input_per_1k": float, "output_per_1k": float}. Telemetry
    # callers that know the runtime model should pass it to
    # `rate_for_model` so cost isn't silently undercounted when an
    # operator overrides DEEPSEEK_MODEL / GEMINI_MODEL / etc to a
    # different SKU than the provider's headline rate.
    model_rates: Dict[str, Dict[str, float]] = field(default_factory=dict)

    def suitability_for(self, scan_mode: str) -> str:
        """Return suitability rating for a scan_mode. Defaults to
        SUITABILITY_NONE if the provider has no rating for this mode
        (caller will exclude it from ranking)."""
        return self.suitable_for_scan_modes.get(scan_mode, SUITABILITY_NONE)

    def suitability_weight(self, scan_mode: str) -> int:
        """Numerical weight for ranking. 0 = unsuitable; higher = better."""
        return _SUITABILITY_WEIGHTS.get(self.suitability_for(scan_mode), 0)

    def rate_for_model(
        self, model_id: Optional[str],
    ) -> Dict[str, float]:
        """Return per-1k-token rates for a specific model id, falling
        back to provider-level rates when the model has no entry. Use
        this in telemetry/cost computation so DEEPSEEK_MODEL (etc)
        overrides don't silently undercount."""
        if model_id:
            rates = self.model_rates.get(model_id)
            if rates:
                return {
                    "input_per_1k": float(
                        rates.get("input_per_1k",
                                  self.cost_per_1k_input_tokens_usd),
                    ),
                    "output_per_1k": float(
                        rates.get("output_per_1k",
                                  self.cost_per_1k_output_tokens_usd),
                    ),
                }
        return {
            "input_per_1k": self.cost_per_1k_input_tokens_usd,
            "output_per_1k": self.cost_per_1k_output_tokens_usd,
        }

    def estimated_cost_per_probe_usd(
        self, *, expected_input_tokens: int = 500,
        expected_output_tokens: int = 300,
        model_id: Optional[str] = None,
    ) -> float:
        """Rough cost estimate for one probe call. Uses per-model
        rates when `model_id` is known (and that model has a rate
        entry), otherwise falls back to the provider-level rates.
        Default expected tokens are sized for an audit probe (system
        prompt + query + structured JSON response).
        """
        rates = self.rate_for_model(model_id)
        return (
            (expected_input_tokens / 1000.0) * rates["input_per_1k"]
            + (expected_output_tokens / 1000.0) * rates["output_per_1k"]
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

def _config_rate_per_1k(provider_id: str) -> Optional[Dict[str, float]]:
    """Per-1k input/output token USD rates for `provider_id`, derived from the
    billing config (config/provider_credit_rates.json).

    The config stores prices per 1M tokens (input_cost_usd_per_1m_tokens /
    output_cost_usd_per_1m_tokens); the registry meters per 1k, so divide by
    1000. Returns None when the provider (or its price fields) is absent from the
    config — the caller then falls back to a hardcoded constant. Deriving here is
    what keeps registry cost_per_1k_* from silently disagreeing with the rates we
    actually bill against (#1508)."""
    try:
        entry = provider_rate_entry(provider_id)
    except ValueError:
        # Provider not in the billing config (e.g. "mock", or a future provider
        # priced only in code) — let the caller use its constant fallback.
        return None
    in_1m = entry.get("input_cost_usd_per_1m_tokens")
    out_1m = entry.get("output_cost_usd_per_1m_tokens")
    if in_1m is None or out_1m is None:
        return None
    return {
        "input_per_1k": float(in_1m) / 1000.0,
        "output_per_1k": float(out_1m) / 1000.0,
    }


def _rates_or(provider_id: str, *, fallback_in: float, fallback_out: float) -> Dict[str, float]:
    """Config-derived per-1k rates for `provider_id`, or the given constants when
    the provider isn't in the billing config."""
    derived = _config_rate_per_1k(provider_id)
    if derived is not None:
        return derived
    return {"input_per_1k": fallback_in, "output_per_1k": fallback_out}


def _seed_default_providers() -> None:
    """Pre-register the providers Pivota supports today. New
    providers (ChatGPT, Claude, etc.) get appended here as they
    ship; the orchestrator picks them up automatically."""
    # Gemini (via PIVOTA-Agent codex stack)
    # Rates derived from config (gemini-2.5-flash: $0.30/$2.50 per 1M).
    _gemini_rates = _rates_or("gemini", fallback_in=0.00035, fallback_out=0.00105)
    register_provider(LLMProvider(
        id="gemini",
        display_name="Google Gemini Grounded",
        backend=BACKEND_PIVOTA_AGENT_UPSTREAM,
        cost_per_1k_input_tokens_usd=_gemini_rates["input_per_1k"],   # gemini-2.5-flash (config)
        cost_per_1k_output_tokens_usd=_gemini_rates["output_per_1k"],
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
    # Provider-level + headline SKU (deepseek-chat, V4 Flash) rate = config
    # (deepseek-v4-flash: $0.14/$0.28 per 1M). deepseek-reasoner is a distinct
    # SKU NOT in the billing config, so it keeps its code-owned constant.
    _deepseek_rates = _rates_or("deepseek", fallback_in=0.00014, fallback_out=0.00028)
    register_provider(LLMProvider(
        id="deepseek",
        display_name="Deepseek V4",
        backend=BACKEND_DEEPSEEK_LOCAL,
        # Provider-level rate = headline SKU (deepseek-chat, V4 Flash).
        # Per-model rates below cover the published lineup; if an
        # operator points DEEPSEEK_MODEL at one of them, telemetry
        # uses the right rate. Unknown model ids fall back to these
        # provider-level numbers.
        cost_per_1k_input_tokens_usd=_deepseek_rates["input_per_1k"],    # deepseek-chat (config)
        cost_per_1k_output_tokens_usd=_deepseek_rates["output_per_1k"],
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
        # Per-model rates (cache-miss list price; update as Deepseek's
        # pricing page changes). Cache-hit pricing is a separate axis
        # not modeled here.
        model_rates={
            "deepseek-chat": _deepseek_rates,   # V4 Flash — config-derived
            "deepseek-reasoner": {   # V4 Pro / reasoner SKU (not in billing config)
                "input_per_1k": 0.00055,
                "output_per_1k": 0.00219,
            },
        },
    ))

    # ChatGPT (OpenAI Responses + web_search_preview, via PIVOTA-Agent upstream).
    # Premium/paid-tier provider. Registered so probe telemetry computes a real
    # cost_usd instead of NULL/$0 — before this, get_provider("chatgpt") was None
    # and every ChatGPT probe recorded $0 even with real tokens. Token rates
    # mirror config/provider_credit_rates.json; the per-call web_search fee is
    # priced there (grounding_cost_usd_per_call) and in the gateway estimate.
    # Rates derived from config (gpt-5.5: $5.00/$30.00 per 1M).
    _chatgpt_rates = _rates_or("chatgpt", fallback_in=0.005, fallback_out=0.03)
    register_provider(LLMProvider(
        id="chatgpt",
        display_name="OpenAI ChatGPT Grounded",
        backend=BACKEND_PIVOTA_AGENT_UPSTREAM,
        cost_per_1k_input_tokens_usd=_chatgpt_rates["input_per_1k"],   # $5.00 / 1M input (config)
        cost_per_1k_output_tokens_usd=_chatgpt_rates["output_per_1k"], # $30.00 / 1M output (config)
        avg_latency_ms_per_probe=14000,
        rate_limit_per_minute=60,
        health_status=HEALTH_HEALTHY,
        capabilities={
            "grounded_search": "supported",      # web_search_preview tool
            "structured_json_output": "native",
            "max_context_tokens": 400_000,
            "supports_tool_use": True,
        },
        # Intentionally empty: ChatGPT is a premium/paid-tier provider, so it
        # must NOT be auto-selected by the orchestrator (provider="auto"), which
        # would silently route free/default audits onto an expensive provider.
        # Empty suitability => suitability_weight==0 => excluded from auto
        # ranking. It is still fully usable via EXPLICIT selection (coverage
        # profiles list "chatgpt" directly, bypassing the orchestrator), and
        # get_provider("chatgpt") still resolves rates for cost telemetry.
        # Promote these once entitlement-aware auto-selection ships.
        suitable_for_scan_modes={},
        model_rates={
            # Headline model (gpt-5.5) — config-derived.
            "chat-latest": _chatgpt_rates,
        },
    ))

    # Claude (Anthropic Messages + web_search, via PIVOTA-Agent upstream).
    # Premium/paid-tier provider. Same rationale as ChatGPT above.
    # Rates derived from config (claude-sonnet-4: $3.00/$15.00 per 1M).
    _claude_rates = _rates_or("claude", fallback_in=0.003, fallback_out=0.015)
    register_provider(LLMProvider(
        id="claude",
        display_name="Anthropic Claude Grounded",
        backend=BACKEND_PIVOTA_AGENT_UPSTREAM,
        cost_per_1k_input_tokens_usd=_claude_rates["input_per_1k"],    # $3.00 / 1M input (config)
        cost_per_1k_output_tokens_usd=_claude_rates["output_per_1k"],  # $15.00 / 1M output (config)
        avg_latency_ms_per_probe=12000,
        rate_limit_per_minute=60,
        health_status=HEALTH_HEALTHY,
        capabilities={
            "grounded_search": "supported",      # web_search tool
            "structured_json_output": "native",
            "max_context_tokens": 200_000,
            "supports_tool_use": True,
        },
        # Empty for the same reason as ChatGPT above: premium/paid-tier, so
        # excluded from orchestrator auto-selection while remaining explicitly
        # selectable and cost-visible.
        suitable_for_scan_modes={},
        model_rates={
            # Headline model (claude-sonnet-4) — config-derived.
            "claude-sonnet-4": _claude_rates,
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
