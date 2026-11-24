"""Read-only order list and order timeline for the developer portal, authorized by the portal session.

The developer portal's Orders page and the Orders tab of Logs read GET /agent/v1/orders and
GET /agent/v1/orders/events, which take the agent's API key. A key's secret is shown once and the
portal sign-in no longer hands one to the browser, so both views failed for every developer, and the
only remedy the page could offer was "reset your primary key", which breaks the developer's live
agent.

These routes take the portal session instead (get_current_user plus the shared agent-scope guard,
the same pair the portal's keys and webhook-delivery routes use) and return the same response shapes
as the key routes, so the portal can switch with no reshaping:
- they read every order of the agent, with no buyer_ref / X-Agent-User-JWT narrowing (those exist
  for an agent's own end-user UIs, not for the developer reading their agent's book);
- they write no agent_usage_logs row: a developer reading the portal is not an agent API call, and
  counting it would show up in Logs → Requests and the agent's request totals;
- deleted orders are left out of both, as the event feed already does;
- they are reads: refunds and cancels stay on the key routes.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from db.database import database
from utils.auth import EMPLOYEE_STAFF_ROLES, get_current_user
from utils.tenant_scope import assert_agent_scope_or_owner
from utils.transient_errors import db_busy_http_exception, is_asyncpg_busy_error

router = APIRouter(prefix="/agents", tags=["agent-portal-orders"])

_NOT_DELETED = "(is_deleted IS NULL OR is_deleted = FALSE)"


@router.get("/{agent_id}/orders")
async def get_agent_portal_orders(
    agent_id: str,
    merchant_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_user: dict = Depends(get_current_user),
):
    """The agent's orders, newest first. Same shape as GET /agent/v1/orders."""
    await assert_agent_scope_or_owner(database, current_user, agent_id, staff_roles=EMPLOYEE_STAFF_ROLES)

    query = f"SELECT * FROM orders WHERE agent_id = :agent_id AND {_NOT_DELETED}"
    params: Dict[str, Any] = {"agent_id": agent_id}
    if merchant_id:
        query += " AND merchant_id = :merchant_id"
        params["merchant_id"] = merchant_id
    if status:
        query += " AND status = :status"
        params["status"] = status
    query += " ORDER BY created_at DESC LIMIT :limit OFFSET :offset"
    params["limit"] = limit
    params["offset"] = offset

    try:
        rows = await database.fetch_all(query, params)
    except Exception as exc:
        if is_asyncpg_busy_error(exc):
            raise db_busy_http_exception()
        raise HTTPException(status_code=500, detail="Failed to list orders")

    return {
        "status": "success",
        "total": len(rows),
        "orders": [
            {
                "order_id": row["order_id"],
                "merchant_id": row["merchant_id"],
                "status": row["status"],
                "payment_status": row["payment_status"],
                "total": str(row["total"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ],
    }


@router.get("/{agent_id}/orders/events")
async def get_agent_portal_order_events(
    agent_id: str,
    after_id: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    merchant_id: Optional[str] = None,
    order_id: Optional[str] = None,
    current_user: dict = Depends(get_current_user),
):
    """The agent's order timeline, oldest first after `after_id`. Same shape as
    GET /agent/v1/orders/events, without its long-poll (`wait_ms`)."""
    await assert_agent_scope_or_owner(database, current_user, agent_id, staff_roles=EMPLOYEE_STAFF_ROLES)
    # The row normalizer lives with the key route; imported here so this module stays light at import.
    from routes.agent_api import _normalize_order_event_feed_row

    query = f"""
        SELECT
            e.id,
            e.event_type,
            e.merchant_id,
            e.order_id,
            e.status,
            e.total_amount,
            e.currency,
            e.payment_method,
            e.error_message,
            e.created_at,
            e.metadata AS event_metadata,
            o.currency AS order_currency,
            o.total AS order_total
        FROM order_events e
        JOIN orders o ON o.order_id = e.order_id
        WHERE o.agent_id = :agent_id
          AND (o.is_deleted IS NULL OR o.is_deleted = FALSE)
          AND e.id > :after_id
    """
    params: Dict[str, Any] = {"agent_id": agent_id, "after_id": int(after_id), "limit": int(limit)}
    if merchant_id:
        query += " AND e.merchant_id = :merchant_id"
        params["merchant_id"] = merchant_id
    if order_id:
        query += " AND e.order_id = :order_id"
        params["order_id"] = order_id
    query += " ORDER BY e.id ASC LIMIT :limit"

    try:
        rows = await database.fetch_all(query, params)
    except Exception as exc:
        if is_asyncpg_busy_error(exc):
            raise db_busy_http_exception()
        raise HTTPException(status_code=500, detail="Failed to list order events")

    events = [_normalize_order_event_feed_row(dict(row)) for row in rows]
    last_id = int(events[-1]["id"]) if events else int(after_id)
    return {"status": "success", "events": events, "last_id": last_id}
