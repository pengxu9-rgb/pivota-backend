"""Canonical credit-consumption route for metered workflows.

One place that (1) prices an operation with the per-LLM-probe model shared with
the audit path, and (2) atomically consumes/refunds credits against the balance
system (merchant_credit_balance via merchant_credit_balance_service), with the
same idempotency + refund-on-failure semantics audit uses.

New metered workflows should consume through here rather than calling debit()
directly, so pricing and category mapping stay consistent. Pricing is the
per-probe model (provider_credit_rates); the audit cost path shares
`estimate_probe_credits` so both bill identically per provider probe.
"""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Any, Dict, Iterable, Optional, Tuple

from services import merchant_credit_balance_service as _mcb
from services.provider_credit_rates import (
    credits_for_probe,
    provider_probe_cost_usd,
)

# (provider, probe_count, grounded)
ProbeSpec = Tuple[str, int, bool]

# operation_type -> balance-system credit category. Categories are the coarse
# ledger buckets the balance system tracks (audit | prompt | execution); the
# fine-grained operation_type is carried separately for reporting.
_OPERATION_CATEGORY: Dict[str, str] = {
    "audit": "audit",
    "prompt": "prompt",
    # Agent-center workflows (V2 paid tiers) bill under 'execution'.
    "agent_sku_match": "execution",
    "agent_sku_match_live": "execution",
    "agent_demand_test": "execution",
    "agent_bd_report": "execution",
}


def category_for(operation_type: str) -> str:
    """Map an operation_type to its balance-system credit category."""
    try:
        return _OPERATION_CATEGORY[operation_type]
    except KeyError as exc:
        raise ValueError(f"unknown credit operation_type: {operation_type}") from exc


def estimate_probe_credits(probes: Iterable[ProbeSpec]) -> Tuple[int, Decimal]:
    """Credits + internal USD COGS for a set of provider probes.

    The shared pricing model: sum per-probe credits across providers, then ceil
    to whole credits ONCE (matching the audit cost path so equivalent probe sets
    bill identically). Returns (credits:int, usd_cogs:Decimal).
    """
    credits_total = Decimal("0")
    usd_cogs_total = Decimal("0")
    for provider, count, grounded in probes:
        n = int(count)
        if n <= 0:
            continue
        per_probe_credits = Decimal(str(credits_for_probe(provider, grounded=grounded)))
        credits_total += per_probe_credits * n
        usd_cogs_total += provider_probe_cost_usd(provider, grounded=grounded) * Decimal(n)
    return int(math.ceil(float(credits_total))), usd_cogs_total


async def consume(
    merchant_id: str,
    operation_type: str,
    idempotency_key: str,
    *,
    probes: Optional[Iterable[ProbeSpec]] = None,
    credits: Optional[int] = None,
    usd_cogs: Any = None,
    conn: Any = None,
) -> Dict[str, Any]:
    """Atomically consume credits for an operation, idempotent on idempotency_key.

    Pass `probes` to price via the per-probe model, or an explicit `credits`
    amount (e.g. when the caller already computed it). A zero/negative cost is a
    no-op. The idempotency_key is passed through to the balance-system debit
    unchanged, so callers that already debit() directly can migrate without
    changing their replay behavior.
    """
    category = category_for(operation_type)
    if credits is None:
        if probes is None:
            raise ValueError("consume() requires probes or credits")
        credits, estimated_cogs = estimate_probe_credits(probes)
        if usd_cogs is None:
            usd_cogs = estimated_cogs
    credits = int(credits)
    if credits <= 0:
        return {"credits": 0, "category": category, "skipped": True}

    result = await _mcb.debit(
        merchant_id,
        category,
        credits,
        idempotency_key=idempotency_key,
        usd_cogs=usd_cogs or 0,
        conn=conn,
    )
    return {
        "credits": credits,
        "category": category,
        "operation_type": operation_type,
        "usd_cogs": usd_cogs or Decimal("0"),
        "debit": result,
        "replay": bool(result.get("replay")) if isinstance(result, dict) else False,
    }


async def refund(
    merchant_id: str,
    operation_type: str,
    credits: int,
    source_event_id: str,
    *,
    usd_cogs: Any = 0,
    conn: Any = None,
) -> Dict[str, Any]:
    """Refund a prior consume (failure path), idempotent on source_event_id."""
    category = category_for(operation_type)
    credits = int(credits)
    if credits <= 0:
        return {"credits": 0, "category": category, "skipped": True}

    result = await _mcb.credit(
        merchant_id,
        category,
        credits,
        source_event_id=source_event_id,
        usd_cogs=usd_cogs or 0,
        conn=conn,
    )
    return {
        "credits": credits,
        "category": category,
        "operation_type": operation_type,
        "credit": result,
    }
