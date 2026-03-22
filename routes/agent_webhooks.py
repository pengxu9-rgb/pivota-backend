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
    rotate_signing_secret,
    send_test_webhook,
    update_webhook_config,
)
from utils.auth import get_current_user


router = APIRouter(prefix="/agents", tags=["agent-webhooks"])


class UpdateWebhookConfigRequest(BaseModel):
    enabled: bool = False
    destination_url: Optional[str] = None
    subscribed_events: List[str] = Field(default_factory=list)


def _authorize_agent_scope(agent_id: str, current_user: dict) -> None:
    role = current_user.get("role")
    if role in {"admin", "employee", "super_admin"}:
        return
    user_agent_id = current_user.get("agent_id") or current_user.get("user_id")
    if str(user_agent_id or "") != str(agent_id):
        raise HTTPException(status_code=403, detail="Not authorized")


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
    _authorize_agent_scope(agent_id, current_user)
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
    _authorize_agent_scope(agent_id, current_user)
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
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to save webhook config: {exc}")


@router.get("/{agent_id}/webhooks/events/catalog")
async def get_agent_webhook_events_catalog(
    agent_id: str,
    current_user: dict = Depends(get_current_user),
):
    _authorize_agent_scope(agent_id, current_user)
    return list_webhook_events_catalog()


@router.post("/{agent_id}/webhooks/test")
async def post_agent_webhook_test(
    agent_id: str,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    _authorize_agent_scope(agent_id, current_user)
    try:
        result = await send_test_webhook(
            agent_id,
            request_id=request.headers.get("x-request-id"),
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
    _authorize_agent_scope(agent_id, current_user)
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
    _authorize_agent_scope(agent_id, current_user)
    try:
        delivery = await retry_delivery(agent_id, delivery_id)
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


@router.post("/{agent_id}/webhooks/signing-secret/rotate")
async def post_agent_webhook_rotate_secret(
    agent_id: str,
    current_user: dict = Depends(get_current_user),
):
    _authorize_agent_scope(agent_id, current_user)
    try:
        result = await rotate_signing_secret(agent_id)
        return result
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to rotate signing secret: {exc}")
