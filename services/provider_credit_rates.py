"""Provider credit metering for SKU-audit probes.

Rates are loaded from config/provider_credit_rates.json so model and
provider-price drift can be handled by config review rather than code edits.
"""

from __future__ import annotations

import json
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Mapping, Optional


_CONFIG_PATH = (
    Path(__file__).resolve().parents[1]
    / "config"
    / "provider_credit_rates.json"
)


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


@lru_cache(maxsize=1)
def load_provider_credit_config() -> Dict[str, Any]:
    with _CONFIG_PATH.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("provider credit config must be a JSON object")
    return data


def _provider_entries() -> Mapping[str, Mapping[str, Any]]:
    data = load_provider_credit_config().get("provider_credit_rates") or {}
    if not isinstance(data, dict):
        raise ValueError("provider_credit_rates must be a JSON object")
    return data


def provider_rate_entry(provider: str) -> Mapping[str, Any]:
    key = str(provider or "").strip().lower()
    entries = _provider_entries()
    if key in entries:
        return entries[key]
    for entry in entries.values():
        aliases = entry.get("aliases") or []
        if key in {str(alias).strip().lower() for alias in aliases}:
            return entry
    raise ValueError(f"unsupported provider credit rate: {provider}")


def provider_prompt_fraction(provider: str) -> Decimal:
    entry = provider_rate_entry(provider)
    return _decimal(entry.get("prompt_fraction", 1))


def provider_default_grounded(provider: str) -> bool:
    entry = provider_rate_entry(provider)
    return bool(entry.get("default_grounded", False))


def provider_probe_cost_usd(
    provider: str,
    *,
    grounded: bool,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
) -> Decimal:
    """Return provider COGS for one representative probe.

    When token counts are not passed explicitly they resolve PER PROVIDER from
    the rate entry's ``representative_input_tokens`` / ``representative_output_tokens``,
    falling back to the shared ``representative_probe`` block, then to 2000/500.
    Grounded providers differ enormously: ChatGPT's ``web_search_preview`` stuffs
    the retrieved page content into the input context (~15k tokens/probe measured),
    while Gemini grounds server-side (~350). Pricing every provider as a flat
    2000-token probe under-recovered COGS on the ChatGPT/Claude lanes; resolving
    per provider keeps the flat_multiple markup actually clearing cost.
    """
    config = load_provider_credit_config()
    rep = config.get("representative_probe") or {}
    entry = provider_rate_entry(provider)
    if input_tokens is None:
        input_tokens = entry.get("representative_input_tokens", rep.get("input_tokens", 2000))
    if output_tokens is None:
        output_tokens = entry.get("representative_output_tokens", rep.get("output_tokens", 500))
    if int(input_tokens) < 0 or int(output_tokens) < 0:
        raise ValueError("token counts must be >= 0")
    input_cost = (
        _decimal(input_tokens)
        / Decimal("1000000")
        * _decimal(entry.get("input_cost_usd_per_1m_tokens", 0))
    )
    output_cost = (
        _decimal(output_tokens)
        / Decimal("1000000")
        * _decimal(entry.get("output_cost_usd_per_1m_tokens", 0))
    )
    grounding_calls = (
        _decimal(rep.get("grounded_search_calls", 1))
        if grounded else Decimal("0")
    )
    grounding_cost = (
        grounding_calls
        * _decimal(entry.get("grounding_cost_usd_per_call", 0))
    )
    return input_cost + output_cost + grounding_cost


def credits_for_probe(
    provider: str,
    *,
    grounded: bool,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
) -> float:
    """Convert a provider probe's COGS into abstract Pivota credits.

    Pricing model: customer price = COGS_usd * flat_multiple, billed in credits
    at credit_to_usd. So credits = COGS_usd * flat_multiple / credit_to_usd.
    flat_multiple is the markup over provider cost (1.2 => 20% over COGS).

    Back-compat: if `flat_multiple` is absent, fall back to the legacy
    `provider_cost_fraction` form (multiple = 1 / provider_cost_fraction).
    """
    config = load_provider_credit_config()
    credit_to_usd = _decimal(config.get("credit_to_usd"))
    if credit_to_usd <= 0:
        raise ValueError("credit_to_usd must be > 0")

    if config.get("flat_multiple") is not None:
        multiple = _decimal(config.get("flat_multiple"))
    else:
        fraction = _decimal(config.get("provider_cost_fraction"))
        if fraction <= 0:
            raise ValueError("provider_cost_fraction must be > 0")
        multiple = Decimal("1") / fraction
    if multiple <= 0:
        raise ValueError("flat_multiple must be > 0")

    credits = provider_probe_cost_usd(
        provider,
        grounded=grounded,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    ) * multiple / credit_to_usd
    places = int(config.get("rounding_decimal_places", 1))
    quant = Decimal("1") if places <= 0 else Decimal("1").scaleb(-places)
    rounded = credits.quantize(quant, rounding=ROUND_HALF_UP)
    return float(rounded)


def token_cost_usd(
    provider: str,
    *,
    input_tokens: int,
    output_tokens: int,
) -> Decimal:
    """Actual-usage COGS for one ungrounded LLM call: measured token counts
    priced at the provider's per-1M rates. No grounding surcharge — callers
    metering grounded probes use provider_probe_cost_usd instead."""
    entry = provider_rate_entry(provider)
    return (
        _decimal(max(int(input_tokens), 0))
        / Decimal("1000000")
        * _decimal(entry.get("input_cost_usd_per_1m_tokens", 0))
    ) + (
        _decimal(max(int(output_tokens), 0))
        / Decimal("1000000")
        * _decimal(entry.get("output_cost_usd_per_1m_tokens", 0))
    )


def credits_for_tokens(
    provider: str,
    *,
    input_tokens: int,
    output_tokens: int,
    multiple: Decimal,
) -> tuple:
    """Credits + USD COGS for ACTUAL token usage at a caller-chosen markup.

    Unlike credits_for_probe (estimated representative probe x the global
    flat_multiple), this prices what the provider actually billed us for:
    customer price = measured token COGS x `multiple`, converted to credits at
    credit_to_usd and ceiled to whole credits (any non-zero usage bills at
    least 1 credit). Returns (credits:int, usd_cogs:Decimal).
    """
    if multiple <= 0:
        raise ValueError("multiple must be > 0")
    config = load_provider_credit_config()
    credit_to_usd = _decimal(config.get("credit_to_usd"))
    if credit_to_usd <= 0:
        raise ValueError("credit_to_usd must be > 0")
    usd_cogs = token_cost_usd(
        provider, input_tokens=input_tokens, output_tokens=output_tokens
    )
    if usd_cogs <= 0:
        return 0, usd_cogs
    # Ceil in pure Decimal: a float round-trip can nudge an exact integer
    # boundary up a hair (2.0 -> 2.0000000000000004 -> ceil 3) and overcharge.
    credits = int(
        (usd_cogs * _decimal(multiple) / credit_to_usd).to_integral_value(
            rounding=ROUND_CEILING
        )
    )
    return max(credits, 1), usd_cogs
