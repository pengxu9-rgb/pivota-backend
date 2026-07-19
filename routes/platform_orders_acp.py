"""
Platform Orders → ACP Integration Routes

Connects platform orders (Amazon/Temu) with ACP payment and fulfillment.
"""

import hashlib
import hmac
import httpx
import json
import logging
import os
from datetime import datetime
from typing import Dict, Any, Optional
from fastapi import APIRouter, HTTPException, Depends, Header, Request, status, BackgroundTasks
from utils.auth import get_current_user, require_admin, can_access_merchant
from db.platform_orders import (
    get_platform_order_by_id,
    update_platform_order_data,
    update_platform_order_by_platform_id,
)
from services.commerce_attribution_service import (
    PVT_CLICK_ID,
    PVT_PRODUCT_ID,
    PVT_PROMPT_CLUSTER,
    PVT_SURFACE,
    PVT_VARIANT_ID,
    has_attribution_signal,
    materialize_attribution_context,
    upsert_order_attribution_edge,
)
from config.settings import settings

# P-T2.0: the pvt_* attribution keys we thread through the ACP checkout metadata
# so a Tier-2 (in-chat protocol) order deposits an attribution edge with the same
# pvt_click_id as the redirect floor. Kept as a tuple so the carry-in (checkout
# create) and recovery (order-completed webhook) stay in lockstep.
_ACP_ATTRIBUTION_KEYS = (
    PVT_SURFACE,
    PVT_CLICK_ID,
    PVT_PRODUCT_ID,
    PVT_VARIANT_ID,
    PVT_PROMPT_CLUSTER,
)

router = APIRouter(
    prefix="/platform-orders",
    tags=["Platform Orders ACP Integration"]
)

# Feature flag check
def ensure_acp_feature_enabled():
    """Ensure Platform Orders ACP integration is enabled"""
    if not getattr(settings, 'enable_platform_orders_acp', False):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Platform Orders ACP integration not enabled"
        )

ACP_URL = settings.platform_orders_acp_url


def _resolve_acp_bearer_token() -> str:
    """Resolve the bearer token for service-to-service ACP calls.

    In production we require PLATFORM_ORDERS_ACP_TOKEN to be set; the
    previous default of "Bearer test" would either be rejected by the
    ACP service or, worse, accepted by a misconfigured peer. In
    dev/staging we fall back to "test" so local end-to-end flows keep
    working.
    """
    token = settings.platform_orders_acp_token
    if token:
        # Fail loud on a placeholder/weak token (#1296) — e.g. an unexpanded `$TOK`
        # — instead of sending it and letting a misconfigured peer accept it.
        from utils.service_token import validate_service_token
        try:
            validate_service_token(token, label="PLATFORM_ORDERS_ACP_TOKEN")
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"platform_orders_acp_token_misconfigured: {e}",
            )
        return token
    is_prod = (
        os.getenv("ENVIRONMENT", "").lower() == "production"
        or os.getenv("RAILWAY_ENVIRONMENT", "").lower() == "production"
    )
    if is_prod:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="platform_orders_acp_token_not_configured",
        )
    return "test"


@router.post("/{order_id}/create-acp-checkout", dependencies=[Depends(ensure_acp_feature_enabled)])
async def create_acp_checkout_for_platform_order(
    order_id: int,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_current_user)
) -> Dict[str, Any]:
    """
    Create ACP checkout session from a platform order.
    
    Version: 2.0 - Supports amazon/temu via shopify proxy
    
    Flow:
    1. Get order from platform_orders
    2. Verify access
    3. Convert to ACP format (map amazon/temu → shopify)
    4. Call ACP API
    5. Update order status
    """
    # Get order
    order = await get_platform_order_by_id(order_id)
    if not order:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Order not found"
        )
    
    # Verify access
    if not can_access_merchant(current_user, order['merchant_id']):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to access this order"
        )
    
    # Check if already created
    if order['data'].get('acp_session_id'):
        return {
            "status": "already_created",
            "session_id": order['data']['acp_session_id'],
            "checkout_url": f"{ACP_URL}/checkout/{order['data']['acp_session_id']}",
            "message": "ACP session already exists for this order"
        }
    
    # Calculate total and prepare items
    items = order['data'].get('items', [])
    if not items:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Order has no items"
        )
    
    # Convert to ACP format
    acp_items = []
    for item in items:
        acp_items.append({
            "id": item.get('sku') or item.get('product_id') or 'unknown',
            "quantity": item.get('quantity', 1)
        })
    
    currency = items[0].get('currency', 'USD').lower()
    
    # Prepare fulfillment address
    fulfillment_address = {
        "name": order['data'].get('buyer_name', 'Customer'),
        "line_one": order['data'].get('ship_address', 'N/A'),
        "city": "N/A",
        "state": "N/A",
        "country": "US",
        "postal_code": "00000"
    }
    
    # Map platform to ACP-supported platform
    # ACP currently only supports shopify/wix
    # For amazon/temu, we use shopify as a proxy
    acp_platform = "shopify" if order['platform'] in ['amazon', 'temu'] else order['platform']

    # P-T2.0 attribution parity: thread the pvt_click_id/surface into the ACP
    # checkout metadata so the completed-order webhook can deposit a
    # commerce_attribution_edge, exactly like the redirect floor. The click id
    # rides the platform order's own `data` (stamped upstream at order intake,
    # the Tier-2 analogue of the redirect cart-attribute round-trip). We only
    # add keys we actually resolved — an order with no attribution signal keeps
    # today's behavior (no synthetic click id injected into the provider call).
    acp_metadata = {
        "platform_order_id": order['order_id'],
        "platform_order_db_id": str(order_id),
        "platform": order['platform'],  # Keep original platform in metadata
        "acp_platform": acp_platform,
    }
    attribution_updates: Dict[str, Any] = {}
    if has_attribution_signal(order.get('data') or {}):
        attribution_context = materialize_attribution_context(
            order['data'],
            default_surface=str((order['data'] or {}).get(PVT_SURFACE) or "acp"),
            merchant_id=order['merchant_id'],
        )
        for key in _ACP_ATTRIBUTION_KEYS:
            value = attribution_context.get(key)
            if value:
                acp_metadata[key] = str(value)
                attribution_updates[key] = str(value)

    # Call ACP API
    acp_bearer = _resolve_acp_bearer_token()
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{ACP_URL}/checkout_sessions",
                headers={
                    "Authorization": f"Bearer {acp_bearer}",
                    "API-Version": "2025-09-29",
                    "X-Merchant-Id": order['merchant_id'],
                    "X-Platform": acp_platform,  # Use shopify as proxy for amazon/temu
                    "Content-Type": "application/json"
                },
                json={
                    "items": acp_items,
                    "fulfillment_address": fulfillment_address,
                    "metadata": acp_metadata
                }
            )
        
        if resp.status_code not in (200, 201):
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"ACP API error: {resp.status_code} - {resp.text[:200]}"
            )
        
        acp_session = resp.json()
        session_id = acp_session.get('id')
        
        if not session_id:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="ACP API did not return session_id"
            )
        
        # Calculate total from ACP response
        totals = acp_session.get('totals', [])
        total_obj = next((t for t in totals if t.get('type') == 'total'), None)
        total_cents = total_obj.get('amount') if total_obj else 0
        
        # Update order status in background to avoid blocking response
        async def update_order_status():
            try:
                await update_platform_order_data(
                    order_id,
                    {
                        'acp_session_id': session_id,
                        'payment_status': 'pending',
                        'acp_created_at': datetime.utcnow().isoformat(),
                        'acp_total_cents': total_cents,
                        'acp_currency': currency,
                        # P-T2.0: persist the resolved pvt_* keys onto the order so
                        # the completed-order webhook can recover the click id even
                        # if the ACP provider does not echo our metadata back.
                        **attribution_updates,
                    }
                )
            except Exception as db_exc:
                import logging
                logging.getLogger(__name__).error(f"Failed to update order status: {db_exc}")
        
        background_tasks.add_task(update_order_status)
        
        # Return immediately with simplified response
        return {
            "status": "success",
            "session_id": session_id,
            "checkout_url": f"{ACP_URL}/checkout/{session_id}",
            "order_id": order_id
        }
    
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to connect to ACP: {str(exc)}"
        )


@router.post("/webhooks/acp/order-completed", dependencies=[Depends(ensure_acp_feature_enabled)])
async def handle_acp_order_completed_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    Merchant_Signature: Optional[str] = Header(default=None, alias="Merchant-Signature")
) -> Dict[str, Any]:
    """
    Handle ACP order completed webhook.

    Updates platform_orders payment status when ACP payment completes.
    Logs payment transaction for audit trail.

    The body is parsed manually (not via FastAPI's `payload: dict` auto-binding)
    so we have the raw bytes for HMAC-SHA256 signature verification against
    the `Merchant-Signature` header.
    """
    logger = logging.getLogger(__name__)

    body = await request.body()

    # Verify HMAC-SHA256 signature against PLATFORM_ORDERS_ACP_WEBHOOK_SECRET.
    # In production we require both the secret and a header; in dev we
    # accept unsigned payloads with a warning.
    secret = settings.platform_orders_acp_webhook_secret
    is_prod = (
        os.getenv("ENVIRONMENT", "").lower() == "production"
        or os.getenv("RAILWAY_ENVIRONMENT", "").lower() == "production"
    )
    if secret:
        if not Merchant_Signature:
            logger.warning("ACP webhook rejected: Merchant-Signature header missing")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing Merchant-Signature header",
            )
        expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(Merchant_Signature, expected):
            logger.warning("ACP webhook rejected: signature mismatch")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid signature",
            )
    elif is_prod:
        logger.error(
            "PLATFORM_ORDERS_ACP_WEBHOOK_SECRET not configured in production "
            "— refusing to accept unsigned ACP webhook"
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="webhook_secret_not_configured",
        )
    else:
        logger.warning(
            "ACP webhook signature verification skipped "
            "(PLATFORM_ORDERS_ACP_WEBHOOK_SECRET unset; permitted only in non-production)"
        )

    try:
        payload = json.loads(body.decode("utf-8") or "{}")
    except json.JSONDecodeError as exc:
        logger.warning(f"ACP webhook rejected: invalid JSON ({exc})")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON payload",
        )

    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Webhook payload must be a JSON object",
        )

    # Log webhook received
    logger.info(f"ACP webhook received: {payload.get('id', 'unknown')}")

    # Extract information
    metadata = payload.get('metadata', {})
    platform_order_id = metadata.get('platform_order_id')
    platform_order_db_id = metadata.get('platform_order_db_id')
    
    acp_order_id = payload.get('order_id')
    session_id = payload.get('checkout_session_id')
    payment_status = payload.get('status', 'completed')
    
    if not platform_order_id and not platform_order_db_id:
        logger.error(f"Webhook missing platform order ID: {payload}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing platform_order_id or platform_order_db_id in metadata"
        )
    
    # Determine payment status
    status_mapping = {
        'completed': 'paid',
        'cancelled': 'cancelled',
        'failed': 'failed'
    }
    mapped_status = status_mapping.get(payment_status, 'paid')
    
    # Update order status
    updates = {
        'payment_status': mapped_status,
        'acp_order_id': acp_order_id,
        'acp_session_id': session_id,
        'paid_at': datetime.utcnow().isoformat() if mapped_status == 'paid' else None,
        'payment_updated_at': datetime.utcnow().isoformat()
    }
    
    try:
        if platform_order_db_id:
            await update_platform_order_data(int(platform_order_db_id), updates)
            logger.info(f"Updated order #{platform_order_db_id}: {mapped_status}")
        elif platform_order_id:
            await update_platform_order_by_platform_id(platform_order_id, updates)
            logger.info(f"Updated order {platform_order_id}: {mapped_status}")

        # P-T2.0 attribution parity: on a paid Tier-2 (in-chat ACP) order, deposit a
        # commerce_attribution_edge with the same pvt_click_id the redirect floor
        # deposits. The click id round-trips two ways: the ACP provider echoes the
        # metadata we stamped at checkout create, and we persisted the pvt_* keys onto
        # the platform order's `data` as a fallback. upsert_order_attribution_edge is
        # idempotent on order_id and self-gates on has_attribution_signal, so an order
        # with no signal is a warning-logged skip (Stage-1 gap sizing), never a fake
        # edge. Best-effort: an attribution write must never fail the webhook ack.
        if mapped_status == 'paid':
            try:
                edge_merchant_id: Optional[str] = None
                order_data: Dict[str, Any] = {}
                if platform_order_db_id:
                    platform_order = await get_platform_order_by_id(int(platform_order_db_id))
                    if platform_order:
                        edge_merchant_id = platform_order.get('merchant_id')
                        order_data = platform_order.get('data') or {}
                # Merge the persisted order-side pvt_* (from checkout create) with the
                # provider-echoed webhook metadata; the webhook wins on conflict since
                # it reflects the session that actually converted.
                edge_metadata = {
                    **{k: order_data[k] for k in _ACP_ATTRIBUTION_KEYS if order_data.get(k)},
                    **{k: v for k, v in (metadata or {}).items() if v is not None},
                    "protocol_name": "acp",
                    "commerce_surface": str(
                        (metadata or {}).get(PVT_SURFACE) or order_data.get(PVT_SURFACE) or "acp"
                    ),
                }
                edge_order_id = str(acp_order_id or platform_order_id or platform_order_db_id or "").strip()
                if edge_merchant_id and edge_order_id and has_attribution_signal(edge_metadata):
                    await upsert_order_attribution_edge(
                        order_id=edge_order_id,
                        merchant_id=str(edge_merchant_id),
                        metadata=edge_metadata,
                    )
                elif not edge_merchant_id:
                    logger.info(
                        "ACP attribution edge skipped: merchant unresolved "
                        "(platform_order_db_id=%s platform_order_id=%s)",
                        platform_order_db_id, platform_order_id,
                    )
            except Exception as attribution_exc:  # noqa: BLE001 — best-effort
                logger.warning(
                    "Failed to persist ACP commerce attribution edge for order "
                    "%s: %s",
                    platform_order_id or platform_order_db_id,
                    attribution_exc,
                )

        # Log payment transaction in background
        async def log_payment():
            try:
                # TODO: Create payment_transactions table to log all payments
                logger.info(f"Payment logged: order={platform_order_id or platform_order_db_id}, acp_order={acp_order_id}, status={mapped_status}")
            except Exception as e:
                logger.error(f"Failed to log payment: {e}")
        
        background_tasks.add_task(log_payment)
        
        return {
            "status": "ok",
            "message": "Order payment status updated",
            "order_id": platform_order_id or platform_order_db_id,
            "payment_status": mapped_status
        }
    
    except Exception as exc:
        logger.error(f"Failed to update order status: {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update order: {str(exc)}"
        )


@router.post("/{order_id}/mark-shipped", dependencies=[Depends(ensure_acp_feature_enabled)])
async def mark_platform_order_shipped(
    order_id: int,
    tracking_number: str,
    carrier: Optional[str] = None,
    current_user: dict = Depends(require_admin)
) -> Dict[str, Any]:
    """
    Mark platform order as shipped (placeholder).
    
    Future: Integrate with real logistics API and sync back to platform.
    """
    order = await get_platform_order_by_id(order_id)
    if not order:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Order not found"
        )
    
    # Update fulfillment status
    await update_platform_order_data(
        order_id,
        {
            'fulfillment_status': 'shipped',
            'tracking_number': tracking_number,
            'carrier': carrier or 'unknown',
            'shipped_at': datetime.utcnow().isoformat()
        }
    )
    
    # TODO: Sync to platform (Amazon/Temu API)
    # await sync_fulfillment_to_platform(order)
    
    return {
        "status": "success",
        "order_id": order_id,
        "fulfillment_status": "shipped",
        "tracking_number": tracking_number,
        "message": "Order marked as shipped (placeholder)"
    }


@router.post("/{order_id}/mark-delivered", dependencies=[Depends(ensure_acp_feature_enabled)])
async def mark_platform_order_delivered(
    order_id: int,
    current_user: dict = Depends(require_admin)
) -> Dict[str, Any]:
    """
    Mark platform order as delivered (placeholder).
    """
    order = await get_platform_order_by_id(order_id)
    if not order:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Order not found"
        )
    
    # Update fulfillment status
    await update_platform_order_data(
        order_id,
        {
            'fulfillment_status': 'delivered',
            'delivered_at': datetime.utcnow().isoformat()
        }
    )
    
    return {
        "status": "success",
        "order_id": order_id,
        "fulfillment_status": "delivered",
        "message": "Order marked as delivered"
    }


@router.get("/{order_id}/status", dependencies=[Depends(ensure_acp_feature_enabled)])
async def get_platform_order_status(
    order_id: int,
    current_user: dict = Depends(get_current_user)
) -> Dict[str, Any]:
    """
    Get platform order payment and fulfillment status.
    """
    order = await get_platform_order_by_id(order_id)
    if not order:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Order not found"
        )
    
    # Verify access
    if not can_access_merchant(current_user, order['merchant_id']):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized"
        )
    
    return {
        "order_id": order_id,
        "platform_order_id": order['order_id'],
        "platform": order['platform'],
        "payment_status": order['data'].get('payment_status'),
        "acp_session_id": order['data'].get('acp_session_id'),
        "acp_order_id": order['data'].get('acp_order_id'),
        "paid_at": order['data'].get('paid_at'),
        "fulfillment_status": order['data'].get('fulfillment_status'),
        "tracking_number": order['data'].get('tracking_number'),
        "shipped_at": order['data'].get('shipped_at'),
        "delivered_at": order['data'].get('delivered_at')
    }

