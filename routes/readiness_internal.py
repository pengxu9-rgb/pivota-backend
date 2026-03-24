from __future__ import annotations

import hmac
import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, EmailStr, Field

from readiness import service as readiness_service
from readiness.flags import (
    readiness_payment_bridge_enabled,
    readiness_payment_intent_enabled,
    readiness_refund_enabled,
    readiness_payment_status_sync_enabled,
    readiness_return_sync_enabled,
    readiness_router_enabled,
)
from readiness.summary import (
    get_readiness_optimization_cache_metrics,
    invalidate_readiness_optimization_cache,
)


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


class PaymentBridgeRequest(BaseModel):
    payment_reference: str = Field(..., min_length=1, max_length=255)
    psp_used: Optional[str] = Field(default=None, max_length=64)
    client_secret: Optional[str] = Field(default=None, max_length=1024)
    source: str = Field(default="external_payment_execution", min_length=1, max_length=128)
    mark_paid: bool = True
    sync_shopify_transaction: bool = True


class PaymentIntentRequest(BaseModel):
    preferred_psps: Optional[list[str]] = None
    psp_mode: Optional[str] = Field(default=None, max_length=64)


class PaymentStatusSyncRequest(BaseModel):
    mark_paid_on_success: bool = True
    sync_shopify_transaction: bool = True
    source: str = Field(default="readiness_payment_status_sync", min_length=1, max_length=128)


class CheckoutRefundRequest(BaseModel):
    amount: Optional[float] = Field(default=None, gt=0)
    reason: str = Field(default="readiness_alpha_refund", min_length=1, max_length=500)
    source: str = Field(default="readiness_alpha_refund", min_length=1, max_length=128)
    idempotency_key: Optional[str] = Field(default=None, max_length=255)
    sync_shopify_refund_transaction: bool = True


class CheckoutReturnSyncRequest(BaseModel):
    api_version: Optional[str] = Field(default=None, max_length=16)
    limit: int = Field(20, ge=1, le=100)
    sample_limit: int = Field(10, ge=1, le=50)


class OptimizationCacheInvalidateRequest(BaseModel):
    merchant_id: Optional[str] = Field(default=None, min_length=1, max_length=255)
    channel: Optional[str] = Field(default=None, min_length=1, max_length=64)


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


def _require_payment_bridge_enabled() -> None:
    if not readiness_payment_bridge_enabled():
        raise HTTPException(status_code=404, detail="Not Found")


def _require_payment_intent_enabled() -> None:
    if not readiness_payment_intent_enabled():
        raise HTTPException(status_code=404, detail="Not Found")


def _require_payment_status_sync_enabled() -> None:
    if not readiness_payment_status_sync_enabled():
        raise HTTPException(status_code=404, detail="Not Found")


def _require_refund_enabled() -> None:
    if not readiness_refund_enabled():
        raise HTTPException(status_code=404, detail="Not Found")


def _require_return_sync_enabled() -> None:
    if not readiness_return_sync_enabled():
        raise HTTPException(status_code=404, detail="Not Found")


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
    if channel not in {"ucp", "acp"}:
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
            return readiness_service.build_export_summary_response(snapshot, sample_limit=sample_limit, channel="ucp")
        export = await readiness_service.build_channel_export(merchant_id, channel="ucp")
    except readiness_service.UnsupportedMerchantError:
        raise _unsupported_merchant(merchant_id)
    return _model_dump(export)

@router.get("/merchants/{merchant_id}/exports/acp")
async def get_acp_export(
    merchant_id: str,
    request: Request,
    summary_only: bool = Query(False),
    sample_limit: int = Query(25, ge=1, le=100),
    x_pivota_internal_key: Optional[str] = Header(default=None, alias="X-Pivota-Internal-Key"),
) -> Dict[str, Any]:
    _require_internal_access(request, x_pivota_internal_key)
    try:
        if summary_only:
            snapshot = await readiness_service.build_readiness_snapshot(merchant_id, channel="acp")
            return readiness_service.build_export_summary_response(snapshot, sample_limit=sample_limit, channel="acp")
        export = await readiness_service.build_channel_export(merchant_id, channel="acp")
    except readiness_service.UnsupportedMerchantError:
        raise _unsupported_merchant(merchant_id)
    return _model_dump(export)
@router.get("/cache/optimization/metrics")
async def get_optimization_cache_metrics(
    request: Request,
    x_pivota_internal_key: Optional[str] = Header(default=None, alias="X-Pivota-Internal-Key"),
) -> Dict[str, Any]:
    _require_internal_access(request, x_pivota_internal_key)
    return {
        "status": "success",
        "data": get_readiness_optimization_cache_metrics(),
    }


@router.post("/cache/optimization/invalidate")
async def invalidate_optimization_cache(
    body: OptimizationCacheInvalidateRequest,
    request: Request,
    x_pivota_internal_key: Optional[str] = Header(default=None, alias="X-Pivota-Internal-Key"),
) -> Dict[str, Any]:
    _require_internal_access(request, x_pivota_internal_key)
    invalidated_entries = invalidate_readiness_optimization_cache(
        merchant_id=body.merchant_id,
        channel=body.channel,
    )
    return {
        "status": "success",
        "data": {
            "invalidated_entries": invalidated_entries,
            "scope": {
                "merchant_id": body.merchant_id,
                "channel": body.channel,
            },
            "cache_metrics": get_readiness_optimization_cache_metrics(),
        },
    }


@router.get("/merchants/{merchant_id}/exports/acp")
async def get_acp_export(
    merchant_id: str,
    request: Request,
    summary_only: bool = Query(False),
    sample_limit: int = Query(25, ge=1, le=100),
    x_pivota_internal_key: Optional[str] = Header(default=None, alias="X-Pivota-Internal-Key"),
) -> Dict[str, Any]:
    _require_internal_access(request, x_pivota_internal_key)
    try:
        if summary_only:
            snapshot = await readiness_service.build_readiness_snapshot(merchant_id, channel="acp")
            return readiness_service.build_export_summary_response(snapshot, sample_limit=sample_limit, channel="acp")
        export = await readiness_service.build_channel_export(merchant_id, channel="acp")
    except readiness_service.UnsupportedMerchantError:
        raise _unsupported_merchant(merchant_id)
    return _model_dump(export)


@router.get("/cache/optimization/metrics")
async def get_optimization_cache_metrics(
    request: Request,
    x_pivota_internal_key: Optional[str] = Header(default=None, alias="X-Pivota-Internal-Key"),
) -> Dict[str, Any]:
    _require_internal_access(request, x_pivota_internal_key)
    return {
        "status": "success",
        "data": get_readiness_optimization_cache_metrics(),
    }


@router.post("/cache/optimization/invalidate")
async def invalidate_optimization_cache(
    body: OptimizationCacheInvalidateRequest,
    request: Request,
    x_pivota_internal_key: Optional[str] = Header(default=None, alias="X-Pivota-Internal-Key"),
) -> Dict[str, Any]:
    _require_internal_access(request, x_pivota_internal_key)
    invalidated_entries = invalidate_readiness_optimization_cache(
        merchant_id=body.merchant_id,
        channel=body.channel,
    )
    return {
        "status": "success",
        "data": {
            "invalidated_entries": invalidated_entries,
            "scope": {
                "merchant_id": body.merchant_id,
                "channel": body.channel,
            },
            "cache_metrics": get_readiness_optimization_cache_metrics(),
        },
    }


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


@router.post("/merchants/{merchant_id}/checkout-sessions/{checkout_id}/payment-bridge")
async def attach_payment_reference(
    merchant_id: str,
    checkout_id: str,
    body: PaymentBridgeRequest,
    request: Request,
    x_pivota_internal_key: Optional[str] = Header(default=None, alias="X-Pivota-Internal-Key"),
) -> Dict[str, Any]:
    _require_internal_access(request, x_pivota_internal_key)
    _require_payment_bridge_enabled()
    try:
        result = await readiness_service.attach_payment_reference_to_checkout(
            merchant_id,
            checkout_id,
            payment_reference=body.payment_reference,
            psp_used=body.psp_used,
            client_secret=body.client_secret,
            source=body.source,
            mark_paid=body.mark_paid,
            sync_shopify_transaction=body.sync_shopify_transaction,
        )
    except readiness_service.UnsupportedMerchantError:
        raise _unsupported_merchant(merchant_id)
    except KeyError:
        raise _readiness_http_exception(
            404,
            "CHECKOUT_NOT_FOUND",
            {"code": "CHECKOUT_NOT_FOUND", "checkout_id": checkout_id},
        )
    except ValueError as exc:
        detail = exc.args[0] if exc.args else {"code": "CHECKOUT_INVALID"}
        code = str(detail.get("code") or "CHECKOUT_INVALID")
        status_code = 400 if code in {"CHECKOUT_INVALID"} else 409
        raise _readiness_http_exception(status_code, code, detail)

    checkout = result["checkout"]
    order = result["order"] or {}
    return {
        "merchant_id": merchant_id,
        "merchant_alpha_mode": (checkout.session_payload or {}).get("merchant_alpha_mode"),
        "checkout_id": checkout_id,
        "order_id": checkout.order_id,
        "status": checkout.status,
        "payment_status": order.get("payment_status"),
        "payment_reference": result["payment_reference"],
        "psp_used": result["psp_used"],
        "transaction_sync": result["transaction_sync"],
        "replayed": bool(result.get("replayed")),
        "events": [_model_dump(event) for event in result["events"]],
    }


@router.post("/merchants/{merchant_id}/checkout-sessions/{checkout_id}/payment-intent")
async def create_payment_intent(
    merchant_id: str,
    checkout_id: str,
    body: Optional[PaymentIntentRequest],
    request: Request,
    x_pivota_internal_key: Optional[str] = Header(default=None, alias="X-Pivota-Internal-Key"),
) -> Dict[str, Any]:
    _require_internal_access(request, x_pivota_internal_key)
    _require_payment_intent_enabled()
    try:
        result = await readiness_service.create_payment_intent_for_checkout(
            merchant_id,
            checkout_id,
            preferred_psps=body.preferred_psps if body else None,
            psp_mode=body.psp_mode if body else None,
        )
    except readiness_service.UnsupportedMerchantError:
        raise _unsupported_merchant(merchant_id)
    except KeyError:
        raise _readiness_http_exception(
            404,
            "CHECKOUT_NOT_FOUND",
            {"code": "CHECKOUT_NOT_FOUND", "checkout_id": checkout_id},
        )
    except ValueError as exc:
        detail = exc.args[0] if exc.args else {"code": "CHECKOUT_INVALID"}
        code = str(detail.get("code") or "CHECKOUT_INVALID")
        if code == "PAYMENT_FAILED":
            status_code = 402
        elif code in {"CHECKOUT_INVALID"}:
            status_code = 400
        else:
            status_code = 409
        raise _readiness_http_exception(status_code, code, detail)

    checkout = result["checkout"]
    order = result["order"] or {}
    return {
        "merchant_id": merchant_id,
        "merchant_alpha_mode": (checkout.session_payload or {}).get("merchant_alpha_mode"),
        "checkout_id": checkout_id,
        "order_id": checkout.order_id,
        "status": checkout.status,
        "payment_status": order.get("payment_status"),
        "payment_intent_id": result["payment_intent_id"],
        "client_secret": result["client_secret"],
        "psp_used": result["psp_used"],
        "payment_intent_status": result["payment_intent_status"],
        "payment_action": result["payment_action"],
        "bridged_to_paid": bool(result.get("bridged_to_paid")),
        "replayed": bool(result.get("replayed")),
        "events": [_model_dump(event) for event in result["events"]],
    }


@router.post("/merchants/{merchant_id}/checkout-sessions/{checkout_id}/payment-status-sync")
async def sync_payment_status(
    merchant_id: str,
    checkout_id: str,
    body: Optional[PaymentStatusSyncRequest],
    request: Request,
    x_pivota_internal_key: Optional[str] = Header(default=None, alias="X-Pivota-Internal-Key"),
) -> Dict[str, Any]:
    _require_internal_access(request, x_pivota_internal_key)
    _require_payment_status_sync_enabled()
    try:
        result = await readiness_service.sync_payment_status_for_checkout(
            merchant_id,
            checkout_id,
            mark_paid_on_success=body.mark_paid_on_success if body else True,
            sync_shopify_transaction=body.sync_shopify_transaction if body else True,
            source=body.source if body else "readiness_payment_status_sync",
        )
    except readiness_service.UnsupportedMerchantError:
        raise _unsupported_merchant(merchant_id)
    except KeyError:
        raise _readiness_http_exception(
            404,
            "CHECKOUT_NOT_FOUND",
            {"code": "CHECKOUT_NOT_FOUND", "checkout_id": checkout_id},
        )
    except ValueError as exc:
        detail = exc.args[0] if exc.args else {"code": "CHECKOUT_INVALID"}
        code = str(detail.get("code") or "CHECKOUT_INVALID")
        if code == "PAYMENT_STATUS_SYNC_FAILED":
            status_code = 502
        elif code in {"CHECKOUT_INVALID"}:
            status_code = 400
        else:
            status_code = 409
        raise _readiness_http_exception(status_code, code, detail)

    checkout = result["checkout"]
    order = result["order"] or {}
    return {
        "merchant_id": merchant_id,
        "merchant_alpha_mode": (checkout.session_payload or {}).get("merchant_alpha_mode"),
        "checkout_id": checkout_id,
        "order_id": checkout.order_id,
        "status": checkout.status,
        "payment_status": order.get("payment_status"),
        "payment_intent_id": result["payment_intent_id"],
        "payment_reference": result.get("payment_reference"),
        "payment_reference_type": result.get("payment_reference_type"),
        "checkout_session_id": result.get("checkout_session_id"),
        "psp_used": result["psp_used"],
        "payment_intent_status": result["payment_intent_status"],
        "normalized_payment_status": result["normalized_payment_status"],
        "transaction_sync": result["transaction_sync"],
        "bridged_to_paid": bool(result.get("bridged_to_paid")),
        "replayed": bool(result.get("replayed")),
        "events": [_model_dump(event) for event in result["events"]],
    }


@router.post("/merchants/{merchant_id}/checkout-sessions/{checkout_id}/refund")
async def create_checkout_refund(
    merchant_id: str,
    checkout_id: str,
    body: Optional[CheckoutRefundRequest],
    request: Request,
    x_pivota_internal_key: Optional[str] = Header(default=None, alias="X-Pivota-Internal-Key"),
) -> Dict[str, Any]:
    _require_internal_access(request, x_pivota_internal_key)
    _require_refund_enabled()
    try:
        result = await readiness_service.create_refund_for_checkout(
            merchant_id,
            checkout_id,
            amount=body.amount if body else None,
            reason=body.reason if body else "readiness_alpha_refund",
            source=body.source if body else "readiness_alpha_refund",
            idempotency_key=body.idempotency_key if body else None,
            sync_shopify_refund_transaction=body.sync_shopify_refund_transaction if body else True,
        )
    except readiness_service.UnsupportedMerchantError:
        raise _unsupported_merchant(merchant_id)
    except KeyError:
        raise _readiness_http_exception(
            404,
            "CHECKOUT_NOT_FOUND",
            {"code": "CHECKOUT_NOT_FOUND", "checkout_id": checkout_id},
        )
    except ValueError as exc:
        detail = exc.args[0] if exc.args else {"code": "CHECKOUT_INVALID"}
        code = str(detail.get("code") or "CHECKOUT_INVALID")
        status_code = 400 if code in {"CHECKOUT_INVALID"} else 409
        raise _readiness_http_exception(status_code, code, detail)

    checkout = result["checkout"]
    order = result["order"] or {}
    return {
        "merchant_id": merchant_id,
        "merchant_alpha_mode": (checkout.session_payload or {}).get("merchant_alpha_mode"),
        "checkout_id": checkout_id,
        "order_id": checkout.order_id,
        "status": checkout.status,
        "payment_status": order.get("payment_status"),
        "refund_status": result["refund_status"],
        "refund_id": result["refund_id"],
        "psp_refund_id": result["psp_refund_id"],
        "platform_refund_id": result.get("platform_refund_id"),
        "amount": result["amount"],
        "remaining_refundable_before": result["remaining_refundable_before"],
        "transaction_sync": result["transaction_sync"],
        "replayed": bool(result.get("replayed")),
        "events": [_model_dump(event) for event in result["events"]],
    }


@router.post("/merchants/{merchant_id}/checkout-sessions/{checkout_id}/return-sync")
async def sync_checkout_returns(
    merchant_id: str,
    checkout_id: str,
    body: Optional[CheckoutReturnSyncRequest],
    request: Request,
    x_pivota_internal_key: Optional[str] = Header(default=None, alias="X-Pivota-Internal-Key"),
) -> Dict[str, Any]:
    _require_internal_access(request, x_pivota_internal_key)
    _require_return_sync_enabled()
    try:
        result = await readiness_service.sync_returns_for_checkout(
            merchant_id,
            checkout_id,
            api_version=body.api_version if body else None,
            limit=body.limit if body else 20,
            sample_limit=body.sample_limit if body else 10,
        )
    except readiness_service.UnsupportedMerchantError:
        raise _unsupported_merchant(merchant_id)
    except KeyError:
        raise _readiness_http_exception(
            404,
            "CHECKOUT_NOT_FOUND",
            {"code": "CHECKOUT_NOT_FOUND", "checkout_id": checkout_id},
        )
    except ValueError as exc:
        detail = exc.args[0] if exc.args else {"code": "CHECKOUT_INVALID"}
        code = str(detail.get("code") or "CHECKOUT_INVALID")
        status_code = 400 if code in {"CHECKOUT_INVALID"} else 409
        raise _readiness_http_exception(status_code, code, detail)

    checkout = result["checkout"]
    order = result["order"] or {}
    return {
        "merchant_id": merchant_id,
        "merchant_alpha_mode": (checkout.session_payload or {}).get("merchant_alpha_mode"),
        "checkout_id": checkout_id,
        "order_id": checkout.order_id,
        "shopify_order_id": order.get("shopify_order_id"),
        "return_sync_result": result["return_sync_result"],
        "sync_audit": result["audit"],
    }


@router.get("/merchants/{merchant_id}/checkout-sessions/{checkout_id}/return-eligibility")
async def get_checkout_return_eligibility(
    merchant_id: str,
    checkout_id: str,
    request: Request,
    api_version: Optional[str] = Query(default=None),
    sample_limit: int = Query(10, ge=1, le=50),
    x_pivota_internal_key: Optional[str] = Header(default=None, alias="X-Pivota-Internal-Key"),
) -> Dict[str, Any]:
    _require_internal_access(request, x_pivota_internal_key)
    _require_return_sync_enabled()
    try:
        result = await readiness_service.probe_return_eligibility_for_checkout(
            merchant_id,
            checkout_id,
            api_version=api_version,
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
    except ValueError as exc:
        detail = exc.args[0] if exc.args else {"code": "CHECKOUT_INVALID"}
        code = str(detail.get("code") or "CHECKOUT_INVALID")
        status_code = 400 if code in {"CHECKOUT_INVALID"} else 409
        raise _readiness_http_exception(status_code, code, detail)

    checkout = result["checkout"]
    order = result["order"] or {}
    return {
        "merchant_id": merchant_id,
        "merchant_alpha_mode": (checkout.session_payload or {}).get("merchant_alpha_mode"),
        "checkout_id": checkout_id,
        "order_id": checkout.order_id,
        "shopify_order_id": order.get("shopify_order_id"),
        "eligibility": result["eligibility"],
        "platform_probe": result["platform_probe"],
        "sync_audit": result["audit"],
    }


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
