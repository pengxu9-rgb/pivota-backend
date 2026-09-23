"""Reverse a partner-reported order's attribution when the partner reports a refund.

ADR-025 D6 / P3. A payment partner (Reap today) completes the purchase, and Pivota closes a
`partner_reported` edge from the partner's checkout. Anything that happens to the order after
that — a refund, a merchant cancellation, a lost chargeback — must reduce the edge's net GMV, so
that neither the merchant's take nor the originating agent's share is computed on money the
buyer got back.

This module is the PARTNER-AGNOSTIC half: one call, `record_partner_order_adjustment`, keyed by
our own purchase id. Whatever reports the adjustment feeds it: a partner webhook once one exists,
a poll of a post-completion state, or an operator recording one a partner told us about
(`scripts/record_partner_order_adjustment.py`). Reap's agentic API has none of these today, so
the partner-shaped parsers are deliberately NOT here; they are written against a real payload
when one exists, not a guessed one.

What one adjustment does, in ONE transaction holding the edge's row lock:
  1. Finds the edge through `metadata.partner_provenance.purchase_id`, the key the closure
     stamps (`reap_agentic_purchase._close_attribution`).
  2. Refuses anything that would make the ledger lie: a currency that isn't the edge's, a
     currency whose minor unit isn't 1/100 (the shared refund SQL counts cents as major × 100),
     and an amount above what is left un-refunded.
  3. Applies it through the SAME refund UPDATE every other refund path uses
     (`apply_attribution_refund_rows`), idempotent on `refund_id = "<partner>:<event id>"`.
  4. Appends the adjustment (kind, amounts, occurred_at) to `metadata.partner_adjustments`, so
     the edge says WHY its net moved, not only that it did.
After commit, it emits the `refund.succeeded` event and recomputes the edge's day in
`gmv_attribution_daily`. The daily job only rolls up yesterday, so without this recompute a refund
arriving later would never reach billing.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from db.database import database

logger = logging.getLogger(__name__)

#: What a partner can tell us happened to a completed order. Each one reduces net GMV.
#: `cancellation` defaults to "everything still un-refunded"; the others need an amount.
#: A chargeback is recorded only once LOST: an open dispute can still be won, and the shared
#: refund ledger can only add, never give back.
ADJUSTMENT_KINDS = ("refund", "cancellation", "chargeback_lost")

#: `commerce_attribution_edges.latest_refund_id` is String(64).
_REFUND_ID_MAX = 64
_PARTNER_RE = re.compile(r"^[a-z][a-z0-9_]{1,15}$")
_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,64}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")

_EDGE_FOR_PURCHASE_SQL = """
SELECT edge_id, order_id, merchant_id, agent_id, currency, created_at,
       gross_attributed_gmv_cents, COALESCE(refund_amount_cents, 0) AS refund_amount_cents,
       COALESCE(refund_ids, '[]'::jsonb) AS refund_ids
FROM commerce_attribution_edges
WHERE metadata -> 'partner_provenance' ->> 'purchase_id' = :purchase_id
  AND (metadata -> 'partner_provenance' ->> 'partner_reported') = 'true'
ORDER BY edge_id
FOR UPDATE
"""

_APPEND_ADJUSTMENT_SQL = """
UPDATE commerce_attribution_edges
SET metadata = jsonb_set(
      COALESCE(metadata, '{}'::jsonb),
      '{partner_adjustments}',
      COALESCE(metadata -> 'partner_adjustments', '[]'::jsonb) || jsonb_build_array(CAST(:adjustment AS jsonb))
    ),
    updated_at = :now
WHERE edge_id = :edge_id
"""


class AdjustmentRefused(ValueError):
    """The input can never be applied as given. `code` names why."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


@dataclass
class AdjustmentResult:
    #: applied | would_apply | replayed | no_edge | nothing_remaining
    status: str
    purchase_id: str
    refund_id: str
    edge_id: Optional[str] = None
    agent_id: Optional[str] = None
    currency: Optional[str] = None
    gross_minor: Optional[int] = None
    refunded_before_minor: Optional[int] = None
    applied_minor: int = 0
    rollup_recomputed: Optional[bool] = None
    detail: Dict[str, Any] = field(default_factory=dict)


def _minor_exponent(currency: str) -> int:
    # The repo keeps currency exponents in exactly one place; reap_agentic_purchase reads it
    # from there too.
    from services.reap_agentic_purchase import THREE_DECIMAL_CURRENCIES, _exponent

    return 3 if currency in THREE_DECIMAL_CURRENCIES else _exponent(currency)


def _validated(
    *, partner: Any, purchase_id: Any, event_id: Any, kind: Any, currency: Any,
    amount_minor: Any, occurred_at: Any,
) -> Dict[str, Any]:
    p = str(partner or "").strip()
    if not _PARTNER_RE.match(p):
        raise AdjustmentRefused("invalid_partner", "lowercase slug, 2-16 chars")
    pid = str(purchase_id or "").strip()
    if not pid or len(pid) > 64:
        raise AdjustmentRefused("invalid_purchase_id")
    eid = str(event_id or "").strip()
    if not _EVENT_ID_RE.match(eid):
        raise AdjustmentRefused("invalid_event_id", "1-64 chars of [A-Za-z0-9._:-]")
    refund_id = f"{p}:{eid}"
    if len(refund_id) > _REFUND_ID_MAX:
        raise AdjustmentRefused("event_id_too_long", f"'{p}:<event id>' must fit {_REFUND_ID_MAX} chars")
    k = str(kind or "").strip()
    if k not in ADJUSTMENT_KINDS:
        raise AdjustmentRefused("invalid_kind", f"one of {', '.join(ADJUSTMENT_KINDS)}")
    cur = str(currency or "").strip().upper()
    if not _CURRENCY_RE.match(cur):
        raise AdjustmentRefused("invalid_currency")
    if amount_minor is not None:
        # bool is an int subclass; True must not become a one-cent refund.
        if isinstance(amount_minor, bool) or not isinstance(amount_minor, int) or amount_minor <= 0:
            raise AdjustmentRefused("invalid_amount", "a positive integer in minor units")
    elif k != "cancellation":
        raise AdjustmentRefused("amount_required", f"{k} needs amount_minor")
    if occurred_at is not None and (not isinstance(occurred_at, datetime) or occurred_at.tzinfo is None):
        raise AdjustmentRefused("invalid_occurred_at", "a timezone-aware datetime")
    return {"partner": p, "purchase_id": pid, "event_id": eid, "refund_id": refund_id,
            "kind": k, "currency": cur, "amount_minor": amount_minor, "occurred_at": occurred_at}


async def record_partner_order_adjustment(
    *,
    partner: str,
    purchase_id: str,
    event_id: str,
    kind: str,
    currency: str,
    amount_minor: Optional[int] = None,
    occurred_at: Optional[datetime] = None,
    apply: bool = True,
) -> AdjustmentResult:
    """Apply one partner-reported adjustment to the purchase's attribution edge.

    Raises `AdjustmentRefused` for input that can never apply (bad shape, wrong currency,
    more than is left). Returns a result for everything else, including the benign outcomes
    a redelivered or early event produces (`replayed`, `no_edge`, `nothing_remaining`).
    `apply=False` runs every check under the same lock and writes nothing.
    """
    v = _validated(partner=partner, purchase_id=purchase_id, event_id=event_id, kind=kind,
                   currency=currency, amount_minor=amount_minor, occurred_at=occurred_at)
    result = AdjustmentResult(status="no_edge", purchase_id=v["purchase_id"], refund_id=v["refund_id"])
    applied_rows: List[Dict[str, Any]] = []
    refund_major: Optional[Decimal] = None
    edge: Optional[Dict[str, Any]] = None

    async with database.transaction():
        rows = [dict(r) for r in await database.fetch_all(
            _EDGE_FOR_PURCHASE_SQL, {"purchase_id": v["purchase_id"]}
        )]
        if not rows:
            # The purchase never closed an edge (not completed, or its close failed and is waiting
            # on reconciliation). Nothing to reverse yet. The caller keeps the event and retries.
            return result
        if len(rows) > 1:
            raise AdjustmentRefused("ambiguous_edge", f"{len(rows)} partner edges for one purchase")
        edge = rows[0]
        refund_ids = edge["refund_ids"]
        if isinstance(refund_ids, str):
            refund_ids = json.loads(refund_ids)
        gross = edge["gross_attributed_gmv_cents"]
        before = int(edge["refund_amount_cents"] or 0)
        result.edge_id = edge["edge_id"]
        result.agent_id = edge["agent_id"]
        result.currency = (edge["currency"] or "").upper() or None
        result.gross_minor = gross
        result.refunded_before_minor = before

        if v["refund_id"] in (refund_ids or []):
            result.status = "replayed"
            return result
        if result.currency != v["currency"]:
            raise AdjustmentRefused(
                "currency_mismatch", f"edge is {result.currency}, adjustment is {v['currency']}"
            )
        if _minor_exponent(v["currency"]) != 2:
            raise AdjustmentRefused(
                "unsupported_currency",
                f"{v['currency']} is not a 2-decimal currency; the shared refund ledger stores cents",
            )
        if not isinstance(gross, int) or gross <= 0:
            raise AdjustmentRefused("edge_has_no_gross", "a partner edge always carries a positive gross")
        remaining = gross - before
        if remaining <= 0:
            result.status = "nothing_remaining"
            return result
        amount = v["amount_minor"] if v["amount_minor"] is not None else remaining
        if amount > remaining:
            raise AdjustmentRefused(
                "exceeds_remaining", f"{amount} > {remaining} left of {gross} ({v['currency']} minor units)"
            )
        result.applied_minor = amount
        if not apply:
            result.status = "would_apply"
            return result

        refund_major = Decimal(amount) / Decimal(100)
        applied_rows = await _apply_refund(edge["order_id"], v["refund_id"], refund_major)
        if len(applied_rows) != 1:
            # The lock is on the edge found by purchase id. Its order_id matching 0 or 2+ rows means
            # the refund would land somewhere other than where we checked it.
            raise RuntimeError(f"refund on order_id matched {len(applied_rows)} edges, expected 1")
        adjustment = {
            "partner": v["partner"], "event_id": v["event_id"], "kind": v["kind"],
            "amount_minor": amount, "currency": v["currency"],
            "occurred_at": v["occurred_at"].isoformat() if v["occurred_at"] else None,
            "recorded_at": _now().isoformat(),
        }
        await database.execute(
            _APPEND_ADJUSTMENT_SQL,
            {"edge_id": edge["edge_id"], "adjustment": json.dumps(adjustment), "now": _now()},
        )
        result.status = "applied"

    # After commit. Neither step may undo the refund, so each failure is reported, not raised.
    await _emit_refund_event(applied_rows, edge["order_id"], v["refund_id"], refund_major)
    result.rollup_recomputed = await _recompute_rollup(edge)
    return result


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _apply_refund(order_id: str, refund_id: str, amount: Decimal) -> List[Dict[str, Any]]:
    from services.commerce_attribution_service import apply_attribution_refund_rows

    return await apply_attribution_refund_rows(order_id=order_id, refund_id=refund_id, amount=amount)


async def _emit_refund_event(rows: List[Dict[str, Any]], order_id: str, refund_id: str, amount: Any) -> None:
    from services.commerce_attribution_service import emit_attribution_refund_event

    try:
        await emit_attribution_refund_event(rows, order_id=order_id, refund_id=refund_id, amount=amount)
    except Exception as exc:  # noqa: BLE001 -- the refund is committed; the event is best-effort
        logger.warning("partner_adjustment: refund event failed refund_id=%s error_type=%s",
                       refund_id, type(exc).__name__)


async def _recompute_rollup(edge: Dict[str, Any]) -> bool:
    """Re-roll the edge's creation day, which is the day its gross was billed on."""
    from services.gmv_aggregation_service import _coerce_date, recompute_for_date

    try:
        await recompute_for_date(_coerce_date(edge["created_at"]), str(edge["merchant_id"]))
        return True
    except Exception as exc:  # noqa: BLE001 -- the refund stands; the day can be recomputed later
        logger.warning(
            "partner_adjustment: rollup recompute failed edge=%s day=%s error_type=%s",
            edge.get("edge_id"), edge.get("created_at"), type(exc).__name__,
        )
        return False
