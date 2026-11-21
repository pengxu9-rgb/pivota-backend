from __future__ import annotations

from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from services.agent_webhook_service import (
    ensure_agent_webhook_tables,
    get_webhook_config,
    list_deliveries,
    list_webhook_events_catalog,
    receive_managed_inbox_delivery,
    retry_delivery,
    RotationSuperseded,
    rotate_signing_secret,
    send_test_webhook,
    update_webhook_config,
)
from services import webhook_delivery_gate
from db.database import database
from utils.auth import EMPLOYEE_STAFF_ROLES, get_current_user
from utils.tenant_scope import assert_agent_scope_or_owner


router = APIRouter(prefix="/agents", tags=["agent-webhooks"])


class UpdateWebhookConfigRequest(BaseModel):
    enabled: bool = False
    destination_url: Optional[str] = None
    subscribed_events: List[str] = Field(default_factory=list)


@router.post("/{agent_id}/webhooks/managed-inbox")
async def post_agent_managed_webhook_inbox(
    agent_id: str,
    request: Request,
):
    try:
        body = (await request.body()).decode("utf-8")
        result = await receive_managed_inbox_delivery(
            agent_id,
            raw_body=body,
            headers=request.headers,
            source_ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
        return {
            "status": "success",
            "delivery": result,
        }
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to receive managed webhook event: {exc}")


@router.get("/{agent_id}/webhooks/config")
async def get_agent_webhook_config(
    agent_id: str,
    current_user: dict = Depends(get_current_user),
):
    await assert_agent_scope_or_owner(database, current_user, agent_id, staff_roles=EMPLOYEE_STAFF_ROLES)
    try:
        await ensure_agent_webhook_tables()
        return {
            "status": "success",
            "config": await get_webhook_config(agent_id),
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to load webhook config: {exc}")


@router.put("/{agent_id}/webhooks/config")
async def put_agent_webhook_config(
    agent_id: str,
    request: UpdateWebhookConfigRequest,
    current_user: dict = Depends(get_current_user),
):
    await assert_agent_scope_or_owner(database, current_user, agent_id, staff_roles=EMPLOYEE_STAFF_ROLES)
    try:
        config = await update_webhook_config(
            agent_id,
            enabled=request.enabled,
            destination_url=request.destination_url,
            subscribed_events=request.subscribed_events,
        )
        return {
            "status": "success",
            "config": config,
        }
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to save webhook config: {exc}")


@router.get("/{agent_id}/webhooks/events/catalog")
async def get_agent_webhook_events_catalog(
    agent_id: str,
    current_user: dict = Depends(get_current_user),
):
    await assert_agent_scope_or_owner(database, current_user, agent_id, staff_roles=EMPLOYEE_STAFF_ROLES)
    return list_webhook_events_catalog()


@router.post("/{agent_id}/webhooks/test")
async def post_agent_webhook_test(
    agent_id: str,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    await assert_agent_scope_or_owner(database, current_user, agent_id, staff_roles=EMPLOYEE_STAFF_ROLES)
    try:
        result = await send_test_webhook(
            agent_id,
            request_id=request.headers.get("x-request-id"),
        )
        if webhook_delivery_gate.skipped_by_environment(result):
            raise HTTPException(
                status_code=webhook_delivery_gate.SKIP_HTTP_STATUS,
                detail=webhook_delivery_gate.SKIP_DETAIL,
            )
        return {
            "status": "success",
            "delivery": result,
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to send test webhook: {exc}")


@router.get("/{agent_id}/webhooks/deliveries")
async def get_agent_webhook_deliveries(
    agent_id: str,
    limit: int = Query(default=25, ge=1, le=100),
    status: Optional[str] = Query(default=None),
    current_user: dict = Depends(get_current_user),
):
    await assert_agent_scope_or_owner(database, current_user, agent_id, staff_roles=EMPLOYEE_STAFF_ROLES)
    try:
        return await list_deliveries(agent_id, limit=limit, status=status)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to load webhook deliveries: {exc}")


@router.post("/{agent_id}/webhooks/deliveries/{delivery_id}/retry")
async def post_agent_webhook_retry(
    agent_id: str,
    delivery_id: str,
    current_user: dict = Depends(get_current_user),
):
    await assert_agent_scope_or_owner(database, current_user, agent_id, staff_roles=EMPLOYEE_STAFF_ROLES)
    try:
        delivery = await retry_delivery(agent_id, delivery_id)
        if webhook_delivery_gate.skipped_by_environment(delivery):
            raise HTTPException(
                status_code=webhook_delivery_gate.SKIP_HTTP_STATUS,
                detail=webhook_delivery_gate.SKIP_DETAIL,
            )
        return {
            "status": "success",
            "delivery": delivery,
        }
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to retry webhook delivery: {exc}")


_ROTATE_ACTIVATIONS = ("auto", "immediate")


@router.post("/{agent_id}/webhooks/signing-secret/rotate")
async def post_agent_webhook_rotate_secret(
    agent_id: str,
    activation: str = Query(
        default="auto",
        description=(
            "auto: immediate when the current secret was never shown, else a 24 h overlap. "
            "immediate: the new secret signs now and the old (and any pending) one ends now."
        ),
    ),
    current_user: dict = Depends(get_current_user),
):
    await assert_agent_scope_or_owner(database, current_user, agent_id, staff_roles=EMPLOYEE_STAFF_ROLES)
    # Called as a plain function (not through FastAPI), an omitted param is the Query object.
    mode = activation if isinstance(activation, str) else "auto"
    if mode not in _ROTATE_ACTIVATIONS:
        raise HTTPException(status_code=400, detail="activation must be 'auto' or 'immediate'")
    if mode == "immediate":
        # Ends a possibly live secret now: the owning agent's own choice only, never staff's.
        await assert_agent_scope_or_owner(database, current_user, agent_id, staff_roles=())
    try:
        result = await rotate_signing_secret(agent_id, force_immediate=mode == "immediate")
        return result
    except RotationSuperseded:
        raise HTTPException(
            status_code=409,
            detail="A concurrent rotation replaced this one. Rotate again.",
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to rotate signing secret: {exc}")
