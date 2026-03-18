from __future__ import annotations

import hmac
import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, EmailStr, Field

from readiness import service as readiness_service
from readiness.flags import readiness_router_enabled


router = APIRouter(prefix="/internal/readiness", tags=["internal-readiness"])


class CheckoutShippingAddress(BaseModel):
    name: str = Field(..., min_length=1)
    address_line1: str = Field(..., min_length=1)
    city: str = Field(..., min_length=1)
    postal_code: str = Field(..., min_length=1)
    country: str = Field(..., min_length=2, max_length=2)
    address_line2: Optional[str] = None
    state: Optional[str] = None
    phone: Optional[str] = None


class CheckoutRequest(BaseModel):
    variant_id: str = Field(..., min_length=1)
    quantity: int = Field(1, ge=1, le=20)
    idempotency_key: Optional[str] = Field(default=None, max_length=128)
    buyer_email: Optional[EmailStr] = None
    customer_name: Optional[str] = Field(default=None, max_length=255)
    shipping_address: Optional[CheckoutShippingAddress] = None


class OrderSyncAdvanceRequest(BaseModel):
    replay: bool = False


def _model_dump(model: Any) -> Dict[str, Any]:
    dump = getattr(model, "model_dump", None)
    if callable(dump):
        return dump()
    return model.dict()


def _feature_enabled() -> bool:
    return readiness_router_enabled()


def _dev_allow_unauthenticated(request: Request) -> bool:
    host = (request.url.hostname or "").lower()
    if os.getenv("READINESS_ALLOW_UNAUTHED_DEV", "").strip().lower() in ("1", "true", "yes", "on"):
        return host in ("localhost", "127.0.0.1", "::1", "testserver")
    return host in ("localhost", "127.0.0.1", "::1") and os.getenv("DEV_MODE", "").strip().lower() in ("1", "true", "yes", "on")


def _extract_internal_key(request: Request, x_pivota_internal_key: Optional[str]) -> str:
    if x_pivota_internal_key:
        return x_pivota_internal_key.strip()
    auth = (request.headers.get("authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def _require_internal_access(request: Request, x_pivota_internal_key: Optional[str]) -> None:
    if not _feature_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    if _dev_allow_unauthenticated(request):
        return

    secret = (os.getenv("READINESS_INTERNAL_API_KEY") or "").strip() or (os.getenv("UCP_INTERNAL_API_KEY") or "").strip()
    if not secret:
        raise HTTPException(status_code=404, detail="Not Found")

    provided = _extract_internal_key(request, x_pivota_internal_key)
    if not provided or not hmac.compare_digest(provided, secret):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")


def _unsupported_merchant(merchant_id: str) -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={
            "code": "READINESS_MERCHANT_UNSUPPORTED",
            "message": f"Readiness currently supports only {', '.join(readiness_service.supported_merchants())}, not '{merchant_id}'.",
            "supported_merchants": readiness_service.supported_merchants(),
        },
        headers={"X-Error-Code": "READINESS_MERCHANT_UNSUPPORTED"},
    )


def _readiness_http_exception(status_code: int, code: str, detail: Dict[str, Any]) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail=detail,
        headers={"X-Error-Code": code},
    )


@router.get("/merchants/{merchant_id}/report")
async def get_readiness_report(
    merchant_id: str,
    request: Request,
    channel: str = Query("ucp"),
    summary_only: bool = Query(False),
    sample_limit: int = Query(25, ge=1, le=100),
    x_pivota_internal_key: Optional[str] = Header(default=None, alias="X-Pivota-Internal-Key"),
) -> Dict[str, Any]:
    _require_internal_access(request, x_pivota_internal_key)
    if channel != "ucp":
        raise _readiness_http_exception(
            400,
            "UNSUPPORTED_CHANNEL",
            {"code": "UNSUPPORTED_CHANNEL", "channel": channel},
        )
    try:
        snapshot = await readiness_service.build_readiness_snapshot(merchant_id, channel=channel)
    except readiness_service.UnsupportedMerchantError:
        raise _unsupported_merchant(merchant_id)
    if summary_only:
        return readiness_service.build_snapshot_summary_response(snapshot, sample_limit=sample_limit)
    return _model_dump(snapshot)


@router.get("/merchants/{merchant_id}/exports/ucp")
async def get_ucp_export(
    merchant_id: str,
    request: Request,
    summary_only: bool = Query(False),
    sample_limit: int = Query(25, ge=1, le=100),
    x_pivota_internal_key: Optional[str] = Header(default=None, alias="X-Pivota-Internal-Key"),
) -> Dict[str, Any]:
    _require_internal_access(request, x_pivota_internal_key)
    try:
        if summary_only:
            snapshot = await readiness_service.build_readiness_snapshot(merchant_id, channel="ucp")
            return readiness_service.build_export_summary_response(snapshot, sample_limit=sample_limit)
        export = await readiness_service.build_channel_export(merchant_id, channel="ucp")
    except readiness_service.UnsupportedMerchantError:
        raise _unsupported_merchant(merchant_id)
    return _model_dump(export)


@router.post("/merchants/{merchant_id}/checkout")
async def create_readiness_checkout(
    merchant_id: str,
    body: CheckoutRequest,
    request: Request,
    x_pivota_internal_key: Optional[str] = Header(default=None, alias="X-Pivota-Internal-Key"),
) -> Dict[str, Any]:
    _require_internal_access(request, x_pivota_internal_key)
    try:
        snapshot, product, variant = await readiness_service.resolve_snapshot_variant(merchant_id, body.variant_id, channel="ucp")
    except readiness_service.UnsupportedMerchantError:
        raise _unsupported_merchant(merchant_id)
    except KeyError:
        raise _readiness_http_exception(
            404,
            "VARIANT_NOT_FOUND",
            {"code": "VARIANT_NOT_FOUND", "variant_id": body.variant_id},
        )
    if variant.channel_coverage.get("ucp") != "ready":
        raise _readiness_http_exception(
            409,
            "VARIANT_NOT_READY_FOR_CHECKOUT",
            {
                "code": "VARIANT_NOT_READY_FOR_CHECKOUT",
                "variant_id": body.variant_id,
                "blockers": variant.checkout.blockers,
                "warnings": variant.checkout.warnings,
            },
        )

    base_url = str(request.base_url).rstrip("/")
    try:
        return await readiness_service.create_checkout_session(
            merchant_id=merchant_id,
            variant_id=variant.variant_id,
            quantity=body.quantity,
            base_url=base_url,
            idempotency_key=body.idempotency_key,
            buyer_email=str(body.buyer_email) if body.buyer_email else None,
            customer_name=body.customer_name,
            shipping_address=_model_dump(body.shipping_address) if body.shipping_address is not None else None,
        )
    except ValueError as exc:
        detail = exc.args[0] if exc.args else {"code": "CHECKOUT_INVALID"}
        raise _readiness_http_exception(
            409,
            str(detail.get("code") or "CHECKOUT_INVALID"),
            detail,
        )


@router.get("/checkout-sessions/{checkout_id}")
async def get_checkout_session(
    checkout_id: str,
    request: Request,
    x_pivota_internal_key: Optional[str] = Header(default=None, alias="X-Pivota-Internal-Key"),
) -> Dict[str, Any]:
    _require_internal_access(request, x_pivota_internal_key)
    try:
        return await readiness_service.get_checkout_session_view(checkout_id)
    except KeyError:
        raise _readiness_http_exception(
            404,
            "CHECKOUT_NOT_FOUND",
            {"code": "CHECKOUT_NOT_FOUND", "checkout_id": checkout_id},
        )


@router.get("/merchants/{merchant_id}/order-sync-audit/{checkout_id}")
async def get_order_sync_audit(
    merchant_id: str,
    checkout_id: str,
    request: Request,
    sample_limit: int = Query(10, ge=1, le=50),
    x_pivota_internal_key: Optional[str] = Header(default=None, alias="X-Pivota-Internal-Key"),
) -> Dict[str, Any]:
    _require_internal_access(request, x_pivota_internal_key)
    try:
        return await readiness_service.build_order_sync_audit(
            merchant_id,
            checkout_id,
            sample_limit=sample_limit,
        )
    except readiness_service.UnsupportedMerchantError:
        raise _unsupported_merchant(merchant_id)
    except KeyError:
        raise _readiness_http_exception(
            404,
            "CHECKOUT_NOT_FOUND",
            {"code": "CHECKOUT_NOT_FOUND", "checkout_id": checkout_id},
        )


@router.post("/merchants/{merchant_id}/order-sync/{checkout_id}")
async def advance_order_sync(
    merchant_id: str,
    checkout_id: str,
    request: Request,
    body: Optional[OrderSyncAdvanceRequest] = None,
    x_pivota_internal_key: Optional[str] = Header(default=None, alias="X-Pivota-Internal-Key"),
) -> Dict[str, Any]:
    _require_internal_access(request, x_pivota_internal_key)
    try:
        result = await readiness_service.advance_order_sync(merchant_id, checkout_id, replay=bool(body.replay) if body else False)
    except readiness_service.UnsupportedMerchantError:
        raise _unsupported_merchant(merchant_id)
    except KeyError:
        raise _readiness_http_exception(
            404,
            "CHECKOUT_NOT_FOUND",
            {"code": "CHECKOUT_NOT_FOUND", "checkout_id": checkout_id},
        )

    updated_checkout = result["checkout"]
    return {
        "merchant_id": merchant_id,
        "merchant_alpha_mode": (updated_checkout.session_payload or {}).get("merchant_alpha_mode"),
        "checkout_id": checkout_id,
        "order_id": updated_checkout.order_id,
        "status": updated_checkout.status,
        "replayed": bool(result.get("replayed")),
        "events": [_model_dump(event) for event in result["events"]],
        "capability_status": (updated_checkout.session_payload or {}).get("capability_status") or {},
        "source_of_truth": (updated_checkout.session_payload or {}).get("source_of_truth") or {},
        "todo": [
            "TODO: add live webhook signature verification before broader non-synthetic rollout",
            "TODO: attach real payment authorization references when merchant-native checkout execution is implemented",
        ],
        "requested_replay": bool(body.replay) if body else False,
    }
