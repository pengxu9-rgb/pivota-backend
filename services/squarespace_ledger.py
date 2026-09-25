"""The ONE path from a fetched Squarespace order to ledger rows.

Both Squarespace ingresses — the signed webhook receiver
(routes/squarespace_webhooks.py) and the reconciliation sweep
(services/squarespace_order_sweep.py) — see the same resource: a whole order,
carrying a CUMULATIVE ``refundedTotal``. They must therefore do the same
read-modify-write, hold the same lock, and read the same baseline; two copies
of that arithmetic would be two chances to get it wrong, and a hand-copied
critical section makes any race proof vacuous.

The write path differs between them (it is what the ledger stamps as the
ingress), the SOURCE string on the row differs with it, and nothing else does.
In particular the event ids do not: they are derived from the order id, so a
webhook observation and a later sweep observation of the same order collapse
onto one row instead of double-counting it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from services.commerce_interaction_service import (
    order_money_read_modify_write_lock,
    recorded_refund_amount_cents,
)
from services.merchant_event_ingest_service import ingest_merchant_event_batch
from services.squarespace_event_adapter import (
    UnsupportedSquarespaceEvent,
    map_squarespace_order,
    squarespace_order_currency,
    squarespace_order_ref,
    squarespace_refunded_total_cents,
)


SQUARESPACE_WEBHOOK_WRITE_PATH = "squarespace_webhook"
SQUARESPACE_RECONCILIATION_WRITE_PATH = "squarespace_reconciliation"

# The pair of ingresses that report the SAME cumulative `refundedTotal` for the
# same orders. The baseline read spans BOTH: reading only the caller's own path
# would let a sweep observation re-record money a webhook already counted, under
# a second `<order>:<cumulative>` key that the funnel then SUMS — the same
# inflation the lock exists to prevent, arriving by a different route.
SQUARESPACE_REFUND_WRITE_PATHS = (
    SQUARESPACE_WEBHOOK_WRITE_PATH,
    SQUARESPACE_RECONCILIATION_WRITE_PATH,
)

_REFUND_LOCK_SCOPE = "squarespace_refund"


@dataclass(frozen=True)
class SquarespaceIngestResult:
    """What one observation of one order did to the ledger."""

    status: str  # "recorded" | "ignored"
    accepted: int = 0
    duplicates: int = 0
    reason: Optional[str] = None
    events: List[Dict[str, Any]] = field(default_factory=list)

    def as_summary(self, **extra: Any) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "status": self.status,
            "platform": "squarespace",
            "accepted": self.accepted,
            "duplicates": self.duplicates,
        }
        if self.reason:
            body["reason"] = self.reason
        if self.events:
            body["events"] = self.events
        body.update(extra)
        return body


async def record_squarespace_order(
    *,
    merchant_id: str,
    store_id: str,
    order: Dict[str, Any],
    from_webhook: bool,
    topic: Optional[str] = None,
    trace_id: Optional[str] = None,
) -> SquarespaceIngestResult:
    """Map one fetched order and append it, holding the money lock when needed.

    ``from_webhook`` selects the ingress: True for the signed receiver, False
    for the reconciliation sweep. It is a BOOLEAN rather than a write-path
    string on purpose — the ledger's write path must be a server-fixed literal
    (tests/test_commerce_ledger_write_path_authority.py), so no caller can hand
    one in, and this integration's two paths are the only two that exist.

    The lock and the baseline read are taken only when the order actually
    reports refunded money: with ``refundedTotal`` at zero there is no delta to
    compute whatever the baseline says, and taking an advisory lock for every
    ``order.create`` would be pure cost on the hot path.

    Raises ``ValueError`` when the order is malformed. A ``testmode`` order, or
    a cumulative total that is not new, is an ``ignored`` result rather than an
    error: nothing went wrong, there is simply nothing to record.
    """
    order_ref = squarespace_order_ref(order)
    refunded_cents = squarespace_refunded_total_cents(order)
    if not order_ref or not refunded_cents:
        return await _map_and_ingest(
            merchant_id=merchant_id,
            store_id=store_id,
            order=order,
            from_webhook=from_webhook,
            topic=topic,
            trace_id=trace_id,
        )
    async with order_money_read_modify_write_lock(
        merchant_id=merchant_id,
        store_id=store_id,
        order_ref=order_ref,
        scope=_REFUND_LOCK_SCOPE,
    ):
        previously = await recorded_refund_amount_cents(
            merchant_id=merchant_id,
            store_id=store_id,
            order_ref=order_ref,
            write_path=SQUARESPACE_REFUND_WRITE_PATHS,
            # Subtraction is only meaningful within one unit; a row in another
            # currency is a different quantity, not a smaller one.
            currency=squarespace_order_currency(order),
        )
        return await _map_and_ingest(
            merchant_id=merchant_id,
            store_id=store_id,
            order=order,
            from_webhook=from_webhook,
            topic=topic,
            trace_id=trace_id,
            previously_recorded_refund_cents=previously,
        )


async def _map_and_ingest(
    *,
    merchant_id: str,
    store_id: str,
    order: Dict[str, Any],
    from_webhook: bool,
    topic: Optional[str],
    trace_id: Optional[str],
    previously_recorded_refund_cents: Optional[int] = None,
) -> SquarespaceIngestResult:
    write_path = (
        SQUARESPACE_WEBHOOK_WRITE_PATH
        if from_webhook
        else SQUARESPACE_RECONCILIATION_WRITE_PATH
    )
    try:
        batch = map_squarespace_order(
            order,
            store_id=store_id,
            source=write_path,
            topic=topic,
            trace_id=trace_id,
            previously_recorded_refund_cents=previously_recorded_refund_cents,
        )
    except UnsupportedSquarespaceEvent as exc:
        return SquarespaceIngestResult(status="ignored", reason=str(exc))
    result = await ingest_merchant_event_batch(
        merchant_id=merchant_id,
        batch=batch,
        agent_identity_confidence="platform_asserted",
        # Spelled as literals here, not as the constants above: every
        # production ingest must name its write path as a string constant so a
        # value can never flow in from a caller
        # (tests/test_commerce_ledger_write_path_authority.py). The two arms
        # are pinned equal to the module constants by
        # tests/test_squarespace_ledger.py.
        write_path=(
            "squarespace_webhook" if from_webhook else "squarespace_reconciliation"
        ),
    )
    return SquarespaceIngestResult(
        status="recorded",
        accepted=int(result.get("accepted") or 0),
        duplicates=int(result.get("duplicates") or 0),
        events=list(result.get("events") or []),
    )
