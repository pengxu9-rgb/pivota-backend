"""T9 stamping reaper — retries gross_attributed_gmv_cents stamping for paid
orders whose synchronous stamp silently failed.

`services/psp_payment_finalizer.py:226-238` marks the order paid first, then
attempts T9 stamping inside a try/except that logs and swallows errors. A paid
order whose stamping throws (transient DB hiccup, lock contention, brief network
blip) is left with `gross_attributed_gmv_cents=NULL` forever. T6's rollup
filters `WHERE gross_attributed_gmv_cents IS NOT NULL` so the order is then
silently excluded from billing — invisible until manual reconciliation.

The reaper finds those orders inside a bounded window and retries stamping.
Registered ACTIVE in `services/audit_scheduler.py` to run every 5 minutes
during Stage 1 onward.

Why a reaper instead of a transactional stamp:
- T9 stamping runs outside the order-mark-paid transaction so a stamping
  exception doesn't roll back the payment success. That choice is correct
  for the customer experience (payment is committed even if observability
  hiccups), but it needs a catch-up path.
- Reaper is cheap: the query joins on a small recent-paid-orders set, and
  only edges with NULL gross are candidates.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping

from db.database import database
from services.psp_payment_finalizer import stamp_gross_attributed_gmv

logger = logging.getLogger("stamp_attribution_reaper")

# Buffer of 2 minutes lets the synchronous T9 path fire normally before the
# reaper picks up the same order — avoids contention on fresh paid orders
# and double-attempt log noise on the happy path.
#
# 24h look-back is the recovery window. Anything older that's still unstamped
# should surface in alerting (the gross-null count) rather than be silently
# patched up days later — that would mask a systemic stamping failure.
_UNSTAMPED_PAID_ORDERS_QUERY = """
SELECT DISTINCT
    o.order_id,
    o.subtotal,
    o.discount_total
FROM commerce_attribution_edges e
JOIN orders o ON o.order_id = e.order_id
WHERE e.gross_attributed_gmv_cents IS NULL
  AND LOWER(COALESCE(o.payment_status, '')) IN
      ('paid', 'completed', 'succeeded', 'success', 'settled', 'partially_refunded')
  AND o.paid_at IS NOT NULL
  AND o.paid_at > NOW() - INTERVAL '24 hours'
  AND o.paid_at < NOW() - INTERVAL '2 minutes'
ORDER BY o.paid_at
LIMIT :batch_limit
"""

DEFAULT_BATCH_LIMIT = 100


def _get(row: Any, key: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(key)
    try:
        return row[key]
    except Exception:
        return getattr(row, key, None)


async def run_stamp_attribution_reaper_tick(
    batch_limit: int = DEFAULT_BATCH_LIMIT,
) -> Dict[str, int]:
    """One tick of the reaper. Returns counts for observability.

    Resilient: scan failures and per-order stamping failures are logged but
    never raise — the scheduler keeps ticking. Returning a counts dict makes
    the function trivially testable.
    """
    try:
        rows: List[Any] = list(
            await database.fetch_all(
                _UNSTAMPED_PAID_ORDERS_QUERY,
                {"batch_limit": batch_limit},
            )
        )
    except Exception as exc:
        logger.warning("stamp_attribution_reaper: scan failed: %s", exc)
        return {"scanned": 0, "stamped": 0, "failed": 0}

    stamped = 0
    failed = 0
    for row in rows:
        order_id = _get(row, "order_id")
        if not order_id:
            continue
        try:
            updated = await stamp_gross_attributed_gmv(
                str(order_id),
                subtotal=_get(row, "subtotal"),
                discount_total=_get(row, "discount_total"),
            )
            if updated and updated > 0:
                stamped += 1
                logger.info(
                    "stamp_attribution_reaper: stamped order=%s edges_updated=%d",
                    order_id,
                    updated,
                )
            # updated == 0 is fine — edges were stamped by a parallel path
            # between SELECT and UPDATE; treat as success, not failure.
        except Exception as exc:
            failed += 1
            logger.warning(
                "stamp_attribution_reaper: failed to stamp order=%s: %s",
                order_id,
                exc,
            )

    if rows:
        logger.info(
            "stamp_attribution_reaper: scanned=%d stamped=%d failed=%d",
            len(rows),
            stamped,
            failed,
        )
    return {"scanned": len(rows), "stamped": stamped, "failed": failed}
