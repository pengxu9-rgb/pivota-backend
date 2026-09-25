"""The ONE path from a fetched Webflow order to ledger rows.

Both Webflow ingresses — the webhook receiver (routes/webflow_webhooks.py) and
the reconciliation sweep (services/webflow_order_sweep.py) — see the same
resource: a whole order fetched from the Data API. They therefore map through
one function, so the two cannot drift.

The write path differs between them (it is what the ledger stamps as the
ingress) and the SOURCE string on the row differs with it. Nothing else does. In
particular the event ids do not: they are derived from the order id, so a
webhook observation and a later sweep observation of the same order collapse
onto one row instead of double-counting it.

NO MONEY LOCK, AND THAT IS A PROPERTY OF WEBFLOW RATHER THAN AN OMISSION.
Shoplazza and Squarespace hold `order_money_read_modify_write_lock` because
their refund figure is a CUMULATIVE total and the amount to record is a delta
against what Pivota already stored — a read-modify-write, and a raced pair of
those inflates refunded GMV. Webflow refunds are full-order only: there is one
refund per order, its amount is the whole `customerPaid`, and it is written
under one deterministic key. Two concurrent observations therefore emit the
IDENTICAL row, which the ledger's first-write-wins dedupe collapses. There is no
baseline to read, so there is nothing to serialise.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from services.merchant_event_ingest_service import ingest_merchant_event_batch
from services.webflow_event_adapter import (
    UnsupportedWebflowEvent,
    map_webflow_order,
)


logger = logging.getLogger("webflow_ledger")


WEBFLOW_WEBHOOK_WRITE_PATH = "webflow_webhook"
WEBFLOW_RECONCILIATION_WRITE_PATH = "webflow_reconciliation"


@dataclass(frozen=True)
class WebflowIngestResult:
    """What one observation of one order did to the ledger."""

    status: str  # "recorded" | "ignored"
    accepted: int = 0
    duplicates: int = 0
    reason: Optional[str] = None
    events: List[Dict[str, Any]] = field(default_factory=list)
    # Named parts of THIS order the mapper deliberately did not record while
    # recording the rest — today only an unreadable refund amount. Distinct from
    # `reason`, which describes an observation that produced nothing at all: a
    # partially-mapped order is still `recorded`, and a caller that treated
    # these as "nothing happened" would under-count real orders.
    ignored_reasons: Tuple[str, ...] = ()

    def as_summary(self, **extra: Any) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "status": self.status,
            "platform": "webflow",
            "accepted": self.accepted,
            "duplicates": self.duplicates,
        }
        if self.reason:
            body["reason"] = self.reason
        if self.ignored_reasons:
            body["ignored_reasons"] = list(self.ignored_reasons)
        if self.events:
            body["events"] = self.events
        body.update(extra)
        return body


async def record_webflow_order(
    *,
    merchant_id: str,
    store_id: str,
    order: Dict[str, Any],
    from_webhook: bool,
    trigger_type: Optional[str] = None,
    trace_id: Optional[str] = None,
) -> WebflowIngestResult:
    """Map one fetched order and append it to the canonical ledger.

    ``from_webhook`` selects the ingress: True for the receiver, False for the
    sweep. It is a BOOLEAN rather than a write-path string on purpose — the
    ledger's write path must be a server-fixed literal
    (tests/test_commerce_ledger_write_path_authority.py), so no caller can hand
    one in, and this integration's two paths are the only two that exist.

    Raises ``ValueError`` (including ``WebflowMoneyFormatError``) when the order
    is malformed. An order with nothing to record is an ``ignored`` result
    rather than an error: nothing went wrong.
    """
    try:
        mapping = map_webflow_order(
            order,
            store_id=store_id,
            source=(
                WEBFLOW_WEBHOOK_WRITE_PATH
                if from_webhook
                else WEBFLOW_RECONCILIATION_WRITE_PATH
            ),
            trigger_type=trigger_type,
            trace_id=trace_id,
        )
    except UnsupportedWebflowEvent as exc:
        return WebflowIngestResult(status="ignored", reason=str(exc))
    for reason in mapping.ignored:
        # WARNING, not silence: an order recorded WITHOUT its refund row
        # under-reports money out, and the only tell is this line plus the
        # sweep's counter.
        logger.warning(
            "webflow order partially mapped merchant_id=%s store_id=%s reason=%s",
            merchant_id,
            store_id,
            reason,
        )
    result = await ingest_merchant_event_batch(
        merchant_id=merchant_id,
        batch=mapping.batch,
        agent_identity_confidence="platform_asserted",
        # Spelled as literals here, not as the module constants above: every
        # production ingest must name its write path as a string constant so a
        # value can never flow in from a caller
        # (tests/test_commerce_ledger_write_path_authority.py). The two arms are
        # pinned equal to the module constants by tests/test_webflow_ledger.py.
        write_path="webflow_webhook" if from_webhook else "webflow_reconciliation",
    )
    return WebflowIngestResult(
        status="recorded",
        accepted=int(result.get("accepted") or 0),
        duplicates=int(result.get("duplicates") or 0),
        events=list(result.get("events") or []),
        ignored_reasons=mapping.ignored,
    )
