import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from services.pcs_fact_ingest import backfill_shopify_webhook_events_to_facts
from services.pcs_reducer import reduce_merchant
from utils.auth import get_current_employee

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ops/v1", tags=["ops:pcs-reducer"])


def _parse_iso_ts(value: Optional[str]) -> Optional[datetime]:
    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None


@router.post("/pcs/reducer/replay")
async def ops_pcs_reducer_replay(
    merchant_id: str = Query(..., min_length=2),
    since: Optional[str] = Query(None, description="ISO timestamp. Uses received_at for scanning/backfill."),
    limit: int = Query(5000, ge=1, le=50000),
    current_user: dict = Depends(get_current_employee),
):
    """
    Employee-only: backfill Shopify webhook events into pcs_order_facts and run reducer replay.

    Safety:
    - Returns counts only (no payloads).
    - Best-effort; does not modify production behavior outside reducer tables.
    """
    since_dt = _parse_iso_ts(since)
    if since is not None and since_dt is None:
        raise HTTPException(status_code=400, detail="Invalid 'since' timestamp; expected ISO-8601 string")

    try:
        backfill = await backfill_shopify_webhook_events_to_facts(
            merchant_id=merchant_id,
            since_received_at=since_dt,
            limit=limit,
        )
        reduced = await reduce_merchant(
            merchant_id=merchant_id,
            stream_id="orders",
            since=since_dt,
            limit=limit,
        )
    except Exception as e:
        logger.warning("Ops reducer replay failed merchant=%s: %s", merchant_id, e)
        raise HTTPException(status_code=500, detail={"error": "REDUCER_REPLAY_FAILED", "message": str(e)})

    return {
        "status": "success",
        "requested_by": current_user.get("sub"),
        "merchant_id": merchant_id,
        "since": since_dt.isoformat() if since_dt else None,
        "backfill": {
            "facts_scanned": backfill.facts_scanned,
            "facts_inserted": backfill.facts_inserted,
            "facts_duplicated": backfill.facts_duplicated,
            "orders_touched": backfill.orders_touched,
            "last_received_at": backfill.last_received_at.isoformat() if backfill.last_received_at else None,
        },
        "reducer": {
            "facts_scanned": reduced.facts_scanned,
            "facts_applied": reduced.facts_applied,
            "orders_updated": reduced.orders_updated,
            "duration_ms": reduced.duration_ms,
            "checkpoint": reduced.checkpoint,
        },
    }
