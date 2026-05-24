"""INTERNAL ONLY — never serialize to merchant-facing endpoints. See build brief §3 credit display convention."""

from __future__ import annotations

# All values from build brief §4 + §8. Customer-facing surfaces NEVER reference these directly.

CREDIT_TO_USD_CENTS = 1
OVERAGE_PREMIUM = 1.30
OVERAGE_RATE_PER_CREDIT_USD_CENTS = 1.3
# Note: per-credit values are sub-cent. Always multiply by credit count THEN round to cent.

SAAS_GROSS_MARGIN_PCT = 0.85
DEFAULT_CREDIT_COST_RATIO = 0.65
PERSONAL_AGENT_NET_MARGIN = 0.97
THIRD_PARTY_AGENT_NET_MARGIN = 0.35

CREDIT_COST_RATIO_OVERRIDES: dict[str, float] = {
    # Illustrative per-model overrides per brief §8 (assumes ~500 tokens/credit, 1:3 in:out).
    # Replace with negotiated rates once they land. For now, helpers default to
    # DEFAULT_CREDIT_COST_RATIO if model_id is not in this dict.
    "claude-sonnet-4.6": 0.60,
    "claude-haiku-4.5": 0.20,
    "claude-opus-4.6": 1.00,
    "gpt-5.5": 1.19,
    "gpt-5.4": 0.59,
    "gpt-5.4-mini": 0.18,
    "gpt-5.4-nano": 0.05,
    "gpt-5.3-codex": 0.55,
}


def overage_revenue_cents(overage_credits: int) -> int:
    """Cents charged for overage credits. Build brief §6.1 formula:
    overage_revenue_usd = overage_credits × overage_rate_per_credit_usd
    Returns rounded cents (banker's rounding via round())."""

    return round(overage_credits * OVERAGE_RATE_PER_CREDIT_USD_CENTS)
