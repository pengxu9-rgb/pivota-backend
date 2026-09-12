"""Versioned pricing for measured text generation, separate from audit quotes.

Missing usage is an error, never a representative-token estimate. The caller
must persist the returned receipt alongside the result and debit atomically.
"""
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Any, Mapping


PRICE_VERSION = "deepseek-flash-2026-09-12"
PRICE_SOURCE = "https://api-docs.deepseek.com/quick_start/pricing/"
CREDIT_USD = Decimal("0.01")
MARKUP = Decimal("1.6")
CREDIT_QUANTUM = Decimal("0.00000001")


def credits_from_actual_cost(cost_usd: Decimal) -> Decimal:
    if not cost_usd.is_finite() or cost_usd < 0:
        raise ValueError("Actual model cost must be finite and nonnegative")
    return (cost_usd * MARKUP / CREDIT_USD).quantize(
        CREDIT_QUANTUM, rounding=ROUND_HALF_EVEN
    )


def _count(usage: Mapping[str, Any], name: str) -> int:
    value = usage.get(name)
    if type(value) is not int or value < 0:
        raise ValueError(f"Missing or invalid measured usage: {name}")
    return value


def price_deepseek_response(
    response: Mapping[str, Any], *, requested_at: datetime
) -> dict[str, Any]:
    """Price only the explicitly supported model and provider-reported usage.

    UTC request time selects the published peak/off-peak price. Keep this
    timestamp in the receipt so historical debits never use today's rates.
    """
    if requested_at.tzinfo is None:
        raise ValueError("A timezone-aware request timestamp is required")
    model = response.get("model")
    if model not in {"deepseek-flash", "deepseek-v4-flash", "deepseek-v4-flash-vision-exp"}:
        raise ValueError(f"No verified measured price for model: {model}")
    usage = response.get("usage")
    if not isinstance(usage, Mapping):
        raise ValueError("Provider response is missing measured usage")
    prompt = _count(usage, "prompt_tokens")
    completion = _count(usage, "completion_tokens")
    hit = _count(usage, "prompt_cache_hit_tokens")
    miss = _count(usage, "prompt_cache_miss_tokens")
    if hit + miss != prompt:
        raise ValueError("Cache usage does not reconcile with prompt tokens")
    if "total_tokens" in usage and _count(usage, "total_tokens") != prompt + completion:
        raise ValueError("Total usage does not reconcile")
    instant = requested_at.astimezone(timezone.utc)
    peak = instant.weekday() < 5 and (1 <= instant.hour < 4 or 6 <= instant.hour < 10)
    hit_rate, miss_rate, output_rate = (
        (Decimal("0.006"), Decimal("0.3"), Decimal("1.2")) if peak
        else (Decimal("0.003"), Decimal("0.15"), Decimal("0.6"))
    )
    cost = (hit * hit_rate + miss * miss_rate + completion * output_rate) / Decimal("1000000")
    return {
        "provider": "deepseek", "model": model,
        "price_version": PRICE_VERSION, "price_source": PRICE_SOURCE,
        "requested_at": instant.isoformat(), "price_window": "peak" if peak else "off_peak",
        "input_tokens": prompt, "output_tokens": completion,
        "cache_hit_tokens": hit, "cache_miss_tokens": miss,
        "usd_cogs": str(cost), "markup": str(MARKUP),
        "credit_to_usd": str(CREDIT_USD), "credits": str(credits_from_actual_cost(cost)),
    }
