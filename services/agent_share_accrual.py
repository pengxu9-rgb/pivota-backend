"""Accrue each agent's share of Pivota's commission on the orders it originated (ADR-025 D5).

The rule, decided 2026-09-23:
    share = floor( floor(net_gmv x take_rate_bp / 10000) x share_bp / 10000 )
- net_gmv is the edge's gross minus its refunds, so a refund reduces the share.
- take_rate_bp is the rate BILLING charged on the edge's day: the `gmv_attribution_daily` row the
  invoice run reads for (day, merchant, agent, channel partner).
- The commission is zero when the merchant cannot be invoiced (no Stripe customer through the
  monetization bridge). Pivota earns nothing there, so neither does the agent. Pivota never owes
  an agent more than it billed.
- share_bp is the agent's rate in `agent_share_rates` at the moment the order converted. No rate
  means 0. The table starts empty, so nothing accrues until someone sets a rate.
Both floors round toward Pivota; the ledger stores every input, so any row can be re-derived.

The ledger is APPEND-ONLY. `accrue_for_edge` computes the edge's target now, compares it with
what the ledger already holds for the edge, and writes the signed difference. Re-running changes
nothing, and a refund appears as its own negative adjustment. It runs under the edge's row lock,
so two runs cannot both write the same difference.

This is bookkeeping only. Nothing here moves money. Paying agents is an open decision (the
2026-09-06 no-money rule).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from db.database import database

logger = logging.getLogger(__name__)

FLAG = "AGENT_SHARE_ACCRUAL_ENABLED"
DEFAULT_LOOKBACK_DAYS = 60
#: services.traffic_taxonomy_service.UNKNOWN_TOKEN: written when traffic has no agent.
UNKNOWN_AGENT = "unknown"

_EDGE_FOR_UPDATE_SQL = """
SELECT edge_id, merchant_id, agent_id, channel_partner_id, currency, state, created_at,
       converted_at, gross_attributed_gmv_cents, COALESCE(refund_amount_cents, 0) AS refund_amount_cents,
       (metadata ->> 'inferred') AS inferred
FROM commerce_attribution_edges
WHERE edge_id = :edge_id
FOR UPDATE
"""

_LEDGER_SUMS_SQL = """
SELECT agent_id, currency, COALESCE(SUM(amount_minor), 0) AS total, COUNT(*) AS n
FROM agent_share_ledger
WHERE edge_id = :edge_id
GROUP BY agent_id, currency
"""

# The row the invoice run bills for this edge: same grouping as gmv_aggregation_service._ROLLUP_QUERY.
_BILLED_RATE_SQL = """
SELECT take_rate_bp
FROM gmv_attribution_daily
WHERE date = (CAST(:created_at AS timestamptz) AT TIME ZONE 'UTC')::date
  AND merchant_id = :merchant_id
  AND COALESCE(agent_id, '') = COALESCE(CAST(:agent_id AS TEXT), '')
  AND COALESCE(channel_partner_id, -1) = COALESCE(CAST(:channel_partner_id AS BIGINT), -1)
LIMIT 1
"""

_RATE_AT_SQL = """
SELECT id, share_bp
FROM agent_share_rates
WHERE agent_id = :agent_id
  AND effective_from <= :at
  AND (effective_to IS NULL OR effective_to > :at)
ORDER BY effective_from DESC
LIMIT 1
"""

_INSERT_ENTRY_SQL = """
INSERT INTO agent_share_ledger (
  edge_id, agent_id, merchant_id, currency, entry_kind, amount_minor, net_gmv_minor,
  take_rate_bp, commission_minor, share_bp, rate_id, target_minor, commission_source, created_at
) VALUES (
  :edge_id, :agent_id, :merchant_id, :currency, :entry_kind, :amount_minor, :net_gmv_minor,
  :take_rate_bp, :commission_minor, :share_bp, :rate_id, :target_minor, :commission_source, :now
)
"""

_RECENT_EDGES_SQL = """
SELECT edge_id FROM commerce_attribution_edges
WHERE (updated_at >= :since OR created_at >= :since)
  AND (agent_id IS NOT NULL OR edge_id IN (SELECT DISTINCT edge_id FROM agent_share_ledger))
ORDER BY edge_id
"""


def is_enabled() -> bool:
    return str(os.getenv(FLAG) or "").strip().lower() in ("1", "true", "on", "yes")


@dataclass
class EdgeAccrual:
    #: written | would_write | unchanged | rollup_pending | no_edge
    status: str
    edge_id: str
    agent_id: Optional[str] = None
    target_minor: int = 0
    written_minor: int = 0
    commission_source: Optional[str] = None
    detail: Dict[str, Any] = field(default_factory=dict)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _real_agent(value: Any) -> Optional[str]:
    agent = str(value or "").strip()
    return agent if agent and agent != UNKNOWN_AGENT else None


def compute_share(net_gmv_minor: int, take_rate_bp: int, share_bp: int) -> Dict[str, int]:
    """The rule, pure: floor at each step, never negative."""
    net = max(int(net_gmv_minor), 0)
    commission = net * int(take_rate_bp) // 10000
    target = commission * int(share_bp) // 10000
    return {"net_gmv_minor": net, "commission_minor": commission, "target_minor": target}


async def _merchant_is_billable(merchant_id: str) -> bool:
    # One owner: the same bridge the invoice run uses to find the merchant's Stripe customer.
    from services.invoice_generation_service import _MERCHANT_STRIPE_CUSTOMER_QUERY

    row = await database.fetch_one(_MERCHANT_STRIPE_CUSTOMER_QUERY, {"merchant_id": merchant_id})
    return bool(row and dict(row).get("stripe_customer_id"))


async def accrue_for_edge(edge_id: str, *, apply: bool = True) -> EdgeAccrual:
    """Bring one edge's ledger to its current target. Idempotent. `apply=False` computes the
    same entries under the same lock and writes none of them."""
    async with database.transaction():
        row = await database.fetch_one(_EDGE_FOR_UPDATE_SQL, {"edge_id": edge_id})
        if row is None:
            return EdgeAccrual(status="no_edge", edge_id=edge_id)
        edge = dict(row)
        agent = _real_agent(edge["agent_id"])
        currency = str(edge["currency"] or "").upper()
        gross = edge["gross_attributed_gmv_cents"]
        eligible = (
            agent is not None
            and edge["state"] == "converted"
            and isinstance(gross, int) and gross > 0
            and str(edge["inferred"] or "").lower() != "true"  # the rollup never bills inferred edges
            and bool(currency)
        )

        take_bp = share_bp = 0
        rate_id = None
        source = "ineligible"
        net = 0
        if eligible:
            billed = await database.fetch_one(_BILLED_RATE_SQL, {
                "created_at": edge["created_at"], "merchant_id": edge["merchant_id"],
                "agent_id": edge["agent_id"], "channel_partner_id": edge["channel_partner_id"],
            })
            if billed is None:
                # The day has not been rolled up yet, so there is no billed rate to share. Come back.
                return EdgeAccrual(status="rollup_pending", edge_id=edge_id, agent_id=agent)
            if await _merchant_is_billable(str(edge["merchant_id"])):
                take_bp = int(dict(billed)["take_rate_bp"] or 0)
                source = "billed_take"
            else:
                source = "merchant_not_billable"
            rate = await database.fetch_one(
                _RATE_AT_SQL, {"agent_id": agent, "at": edge["converted_at"] or edge["created_at"]}
            )
            if rate is not None:
                rate_id, share_bp = int(dict(rate)["id"]), int(dict(rate)["share_bp"])
            net = int(gross) - int(edge["refund_amount_cents"] or 0)

        numbers = compute_share(net, take_bp, share_bp)
        target = numbers["target_minor"] if eligible else 0

        # What the ledger holds per (agent, currency). Anything not for the edge's current agent and
        # currency is reversed to zero: an edge credits one agent, in one currency.
        held = {(r["agent_id"], r["currency"]): (int(r["total"]), int(r["n"]))
                for r in (dict(x) for x in await database.fetch_all(_LEDGER_SUMS_SQL, {"edge_id": edge_id}))}
        entries_before = sum(n for _, n in held.values())
        writes: List[Dict[str, Any]] = []
        for (held_agent, held_cur), (total, _) in held.items():
            if (held_agent, held_cur) != (agent, currency) and total != 0:
                writes.append({"agent_id": held_agent, "currency": held_cur, "amount": -total, "target": 0})
        if agent is not None:
            current = held.get((agent, currency), (0, 0))[0]
            if target != current:
                writes.append({"agent_id": agent, "currency": currency, "amount": target - current,
                               "target": target})

        now = _now()
        for i, w in enumerate(writes if apply else []):
            await database.execute(_INSERT_ENTRY_SQL, {
                "edge_id": edge_id, "agent_id": w["agent_id"], "merchant_id": edge["merchant_id"],
                "currency": w["currency"],
                "entry_kind": "accrual" if entries_before == 0 and i == 0 else "adjustment",
                "amount_minor": w["amount"], "net_gmv_minor": numbers["net_gmv_minor"],
                "take_rate_bp": take_bp, "commission_minor": numbers["commission_minor"],
                "share_bp": share_bp, "rate_id": rate_id, "target_minor": w["target"],
                "commission_source": source, "now": now,
            })

    return EdgeAccrual(
        status=("written" if apply else "would_write") if writes else "unchanged",
        edge_id=edge_id, agent_id=agent,
        target_minor=target, written_minor=sum(w["amount"] for w in writes), commission_source=source,
        detail={"take_rate_bp": take_bp, "share_bp": share_bp, "net_gmv_minor": numbers["net_gmv_minor"]},
    )


async def accrue_recent(lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> Dict[str, int]:
    """Accrue every edge touched in the lookback window, including edges the ledger already
    credited, so an agent change or a refund reverses them. One edge failing never stops the rest."""
    since = _now() - timedelta(days=lookback_days)
    ids = [dict(r)["edge_id"] for r in await database.fetch_all(_RECENT_EDGES_SQL, {"since": since})]
    summary: Dict[str, int] = {"edges": len(ids), "failed": 0}
    for edge_id in ids:
        try:
            result = await accrue_for_edge(edge_id)
            summary[result.status] = summary.get(result.status, 0) + 1
        except Exception as exc:  # noqa: BLE001 -- the next run retries; report, do not stop
            summary["failed"] += 1
            logger.warning("agent_share: accrual failed edge=%s error_type=%s", edge_id, type(exc).__name__)
    return summary


# ── rates ──────────────────────────────────────────────────────────────────────────────────────

class RateRefused(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


_LOCK_AGENT_SQL = "SELECT pg_advisory_xact_lock(hashtext(:key))"
_RATES_FOR_AGENT_SQL = """
SELECT id, share_bp, effective_from, effective_to FROM agent_share_rates
WHERE agent_id = :agent_id ORDER BY effective_from
"""
_CLOSE_RATE_SQL = "UPDATE agent_share_rates SET effective_to = :to WHERE id = :id"
_INSERT_RATE_SQL = """
INSERT INTO agent_share_rates (agent_id, share_bp, effective_from, effective_to, created_by, note, created_at)
VALUES (:agent_id, :share_bp, :effective_from, NULL, :created_by, :note, :now)
RETURNING id
"""


async def set_agent_share_rate(
    *, agent_id: str, share_bp: int, effective_from: datetime, created_by: str, note: Optional[str] = None,
) -> int:
    """Start a new rate for an agent from `effective_from`, closing the open one at that instant.

    Forward-only: a rate cannot start at or before the start of the agent's latest window. Changing
    history would silently re-price orders already accrued, and the next run would rewrite their
    shares. To correct a past rate, add a new window and re-accrue deliberately.
    """
    agent = _real_agent(agent_id)
    if agent is None or len(agent) > 64:
        raise RateRefused("invalid_agent_id")
    if isinstance(share_bp, bool) or not isinstance(share_bp, int) or not 0 <= share_bp <= 10000:
        raise RateRefused("invalid_share_bp", "an integer from 0 to 10000")
    if not isinstance(effective_from, datetime) or effective_from.tzinfo is None:
        raise RateRefused("invalid_effective_from", "a timezone-aware datetime")
    who = str(created_by or "").strip()
    if not who:
        raise RateRefused("created_by_required")
    async with database.transaction():
        await database.execute(_LOCK_AGENT_SQL, {"key": f"agent_share_rates:{agent}"})
        rates = [dict(r) for r in await database.fetch_all(_RATES_FOR_AGENT_SQL, {"agent_id": agent})]
        if rates and effective_from <= rates[-1]["effective_from"]:
            raise RateRefused("not_forward", f"latest window starts {rates[-1]['effective_from'].isoformat()}")
        open_rates = [r for r in rates if r["effective_to"] is None or r["effective_to"] > effective_from]
        for r in open_rates:
            await database.execute(_CLOSE_RATE_SQL, {"to": effective_from, "id": r["id"]})
        row = await database.fetch_one(_INSERT_RATE_SQL, {
            "agent_id": agent, "share_bp": share_bp, "effective_from": effective_from,
            "created_by": who, "note": note, "now": _now(),
        })
        return int(dict(row)["id"])
