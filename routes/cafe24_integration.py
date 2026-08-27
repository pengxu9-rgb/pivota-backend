from __future__ import annotations

import logging
import os
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from adapters.cafe24_adapter import Cafe24Adapter, normalize_cafe24_mall_id
from config.settings import settings
from services.cafe24_integration_service import (
    build_cafe24_authorization_url,
    create_cafe24_oauth_state,
    enable_cafe24_webhook_reception,
    exchange_cafe24_token,
    find_cafe24_store_by_id,
    get_cafe24_webhook_reception_status,
    get_cafe24_oauth_config,
    upsert_cafe24_store,
    verify_cafe24_oauth_state,
)
from services.cafe24_reconciliation_service import reconcile_cafe24_store
from utils.auth import get_current_user


router = APIRouter(prefix="/integrations/cafe24", tags=["Cafe24 Integration"])
logger = logging.getLogger(__name__)


class Cafe24ConnectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    merchant_id: str
    mall_id: str
    access_token: str
    refresh_token: Optional[str] = None
    expires_at: Optional[str] = None
    refresh_token_expires_at: Optional[str] = None
    webhook_api_key: str = Field(min_length=8)
    shop_no: int = Field(default=1, ge=1)
    currency: str = Field(default="KRW", min_length=3, max_length=3)
    api_version: str = "2026-03-01"


def _authorize_merchant(current_user: dict, merchant_id: str) -> None:
    if current_user.get("role") not in {"merchant", "employee", "admin", "super_admin"}:
        raise HTTPException(status_code=403, detail="Not authorized")
    if current_user.get("role") == "merchant" and current_user.get("merchant_id") != merchant_id:
        raise HTTPException(status_code=403, detail="Can only connect your own store")


def _portal_redirect(*, ok: bool, mall_id: Optional[str] = None, reason: Optional[str] = None) -> str:
    base = (
        os.getenv("CAFE24_POST_INSTALL_REDIRECT_URL", "").strip()
        or f"{settings.merchant_portal_base_url.rstrip('/')}/settings/integrations"
    )
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}{urlencode({'installed': 'cafe24' if ok else 'error', 'mall_id': mall_id or '', 'reason': reason or ''})}"


async def _enable_reception_best_effort(credentials: dict) -> dict:
    try:
        return await enable_cafe24_webhook_reception(credentials)
    except Exception as exc:
        logger.warning(
            "Cafe24 webhook reception activation requires follow-up store_id=%s error=%s",
            credentials.get("store_id"),
            exc,
        )
        return {
            "reception_status": "unknown",
            "event_subscription_configuration": "developer_center_required",
            "activation_error": str(exc)[:200],
        }


@router.post("/connect")
async def connect_cafe24(
    body: Cafe24ConnectRequest,
    current_user: dict = Depends(get_current_user),
):
    """Connect using already-issued Cafe24 OAuth tokens (operator/BYO path)."""
    _authorize_merchant(current_user, body.merchant_id)
    adapter = Cafe24Adapter(
        {
            "mall_id": body.mall_id,
            "access_token": body.access_token,
            "api_version": body.api_version,
        }
    )
    valid, error = adapter.validate_config()
    if not valid:
        raise HTTPException(status_code=400, detail=error)
    check = await adapter.test_connection()
    if not check.get("success"):
        raise HTTPException(status_code=400, detail=f"Cafe24 connection failed: {check.get('error')}")
    store_id = await upsert_cafe24_store(
        merchant_id=body.merchant_id,
        mall_id=adapter.mall_id,
        access_token=body.access_token,
        refresh_token=body.refresh_token,
        expires_at=body.expires_at,
        refresh_token_expires_at=body.refresh_token_expires_at,
        webhook_api_key=body.webhook_api_key,
        api_version=body.api_version,
        shop_no=body.shop_no,
        currency=body.currency,
    )
    reception = await _enable_reception_best_effort(
        {
            "store_id": store_id,
            "mall_id": adapter.mall_id,
            "access_token": body.access_token,
            "refresh_token": body.refresh_token,
            "expires_at": body.expires_at,
            "api_version": body.api_version,
        }
    )
    return {
        "status": "success",
        "platform": "cafe24",
        "store_id": store_id,
        "mall_id": adapter.mall_id,
        "product_count": check.get("product_count"),
        "webhook_path": "/webhooks/cafe24",
        "webhook_reception": reception,
    }


@router.get("/{store_id}/status")
async def cafe24_integration_status(
    store_id: str,
    current_user: dict = Depends(get_current_user),
):
    store = await find_cafe24_store_by_id(store_id)
    if not store:
        raise HTTPException(status_code=404, detail="Cafe24 store was not found")
    _authorize_merchant(current_user, str(store["merchant_id"]))
    try:
        webhook_setting = await get_cafe24_webhook_reception_status(store["credentials"])
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "status": "success",
        "platform": "cafe24",
        "store_id": store_id,
        "mall_id": normalize_cafe24_mall_id(store["credentials"].get("mall_id")),
        "webhook_setting": webhook_setting,
        "event_subscription_configuration": "developer_center_required",
        "required_event_numbers": [90023, 90025, 90026, 90027, 90028, 90029, 90072, 90074],
        "required_data_bridge_events": ["VIEW_CONTENT", "INITIATE_ORDERFORM", "CREATE_ORDER"],
        "reconciliation": store["credentials"].get("reconciliation") or {},
        "webhook_path": "/webhooks/cafe24",
    }


@router.post("/{store_id}/webhook-reception/enable")
async def activate_cafe24_webhook_reception(
    store_id: str,
    current_user: dict = Depends(get_current_user),
):
    store = await find_cafe24_store_by_id(store_id)
    if not store:
        raise HTTPException(status_code=404, detail="Cafe24 store was not found")
    _authorize_merchant(current_user, str(store["merchant_id"]))
    try:
        result = await enable_cafe24_webhook_reception(store["credentials"])
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"status": "success", "platform": "cafe24", "store_id": store_id, **result}


@router.post("/{store_id}/reconcile")
async def run_cafe24_reconciliation(
    store_id: str,
    lookback_days: int = Query(default=7, ge=1, le=90),
    limit_per_stream: int = Query(default=500, ge=1, le=10_000),
    current_user: dict = Depends(get_current_user),
):
    store = await find_cafe24_store_by_id(store_id)
    if not store:
        raise HTTPException(status_code=404, detail="Cafe24 store was not found")
    _authorize_merchant(current_user, str(store["merchant_id"]))
    try:
        return await reconcile_cafe24_store(
            store_id=store_id,
            lookback_days=lookback_days,
            limit_per_stream=limit_per_stream,
        )
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/oauth/start")
async def cafe24_oauth_start(
    merchant_id: str = Query(...),
    mall_id: str = Query(...),
    redirect: bool = Query(default=True),
    current_user: dict = Depends(get_current_user),
):
    _authorize_merchant(current_user, merchant_id)
    normalized = normalize_cafe24_mall_id(mall_id)
    config = get_cafe24_oauth_config()
    if not all(
        (
            config.client_id,
            config.client_secret,
            config.redirect_uri,
            config.state_secret,
            config.webhook_api_key,
        )
    ):
        raise HTTPException(status_code=500, detail="Cafe24 OAuth is not fully configured")
    try:
        state_token = create_cafe24_oauth_state(
            merchant_id=merchant_id,
            mall_id=normalized,
            secret=config.state_secret,
        )
        authorization_url = build_cafe24_authorization_url(
            mall_id=normalized,
            state=state_token,
            config=config,
        )
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if redirect:
        return RedirectResponse(authorization_url, status_code=302)
    return {
        "status": "success",
        "platform": "cafe24",
        "mall_id": normalized,
        "authorization_url": authorization_url,
        "expires_in_seconds": 600,
    }


@router.get("/oauth/callback")
async def cafe24_oauth_callback(code: Optional[str] = None, state: Optional[str] = None):
    config = get_cafe24_oauth_config()
    try:
        if not all(
            (
                config.client_id,
                config.client_secret,
                config.redirect_uri,
                config.state_secret,
                config.webhook_api_key,
            )
        ):
            raise ValueError("Cafe24 OAuth is not fully configured")
        if not code or not state:
            raise ValueError("Cafe24 OAuth callback is missing code or state")
        state_payload = verify_cafe24_oauth_state(state, secret=config.state_secret)
        merchant_id = str(state_payload["merchant_id"])
        mall_id = normalize_cafe24_mall_id(state_payload["mall_id"])
        token = await exchange_cafe24_token(
            mall_id=mall_id,
            config=config,
            code=code,
        )
        store_id = await upsert_cafe24_store(
            merchant_id=merchant_id,
            mall_id=mall_id,
            access_token=str(token["access_token"]),
            refresh_token=token.get("refresh_token"),
            expires_at=token.get("expires_at"),
            refresh_token_expires_at=token.get("refresh_token_expires_at"),
            webhook_api_key=config.webhook_api_key,
            api_version=config.api_version,
        )
        await _enable_reception_best_effort(
            {
                "store_id": store_id,
                "mall_id": mall_id,
                "access_token": token["access_token"],
                "refresh_token": token.get("refresh_token"),
                "expires_at": token.get("expires_at"),
                "api_version": config.api_version,
            }
        )
        return RedirectResponse(
            _portal_redirect(ok=True, mall_id=mall_id) + f"&store_id={store_id}",
            status_code=302,
        )
    except Exception:
        logger.exception("Cafe24 OAuth callback failed")
        return RedirectResponse(
            _portal_redirect(ok=False, reason="oauth_failed"),
            status_code=302,
        )
