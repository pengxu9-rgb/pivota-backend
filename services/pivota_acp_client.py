"""P-T2.3.1b — backend → pivota-acp checkout-session client.

A thin, reusable client for creating an ACP checkout SESSION on the pivota-acp
service (`settings.platform_orders_acp_url`). This is the in-chat screen/quote
step only — it creates a session and returns its descriptor; it does NOT call
`/complete` and it does NOT charge (real capture is P-T2.3.2+).

It threads the `pvt_*` attribution keys into the session metadata so the
order-completed webhook (P-T2.0) can later deposit an attribution edge with the
same `pvt_click_id` as the redirect floor.

Framework-agnostic: raises `AcpClientError` (not HTTPException) so callers own
the HTTP translation.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

from config.settings import settings
from services.commerce_attribution_service import (
    PVT_CLICK_ID,
    PVT_PRODUCT_ID,
    PVT_PROMPT_CLUSTER,
    PVT_SURFACE,
    PVT_VARIANT_ID,
    has_attribution_signal,
    materialize_attribution_context,
)

logger = logging.getLogger("pivota_acp_client")

PIVOTA_ACP_API_VERSION = "2025-09-29"

# Keep in lockstep with routes/platform_orders_acp._ACP_ATTRIBUTION_KEYS: the
# pvt_* keys we thread into the ACP session metadata for attribution parity.
_ACP_ATTRIBUTION_KEYS = (
    PVT_SURFACE,
    PVT_CLICK_ID,
    PVT_PRODUCT_ID,
    PVT_VARIANT_ID,
    PVT_PROMPT_CLUSTER,
)


class AcpClientError(Exception):
    """A pivota-acp call failed. `status_code` is the upstream status when known."""

    def __init__(self, message: str, *, status_code: Optional[int] = None, code: str = "acp_error"):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


@dataclass(frozen=True)
class AcpSession:
    session_id: str
    checkout_url: str
    status: Optional[str]
    currency: Optional[str]
    total_cents: Optional[int]
    totals: List[Dict[str, Any]] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)


def _acp_base_url() -> str:
    return str(settings.platform_orders_acp_url or "").rstrip("/")


def _resolve_bearer_token() -> str:
    """Service-to-service bearer for pivota-acp. Required in production; falls
    back to the shared dev placeholder otherwise (mirrors
    routes/platform_orders_acp._resolve_acp_bearer_token, but raises
    AcpClientError instead of HTTPException)."""
    token = settings.platform_orders_acp_token
    if token:
        # Fail loud on a placeholder/weak token (#1296) instead of shipping it as a
        # Bearer and letting a misconfigured peer accept it — same guard as the route.
        from utils.service_token import validate_service_token
        try:
            validate_service_token(token, label="PLATFORM_ORDERS_ACP_TOKEN")
        except ValueError as e:
            raise AcpClientError(
                f"platform_orders_acp_token_misconfigured: {e}",
                status_code=503,
                code="acp_token_misconfigured",
            )
        return token
    is_prod = (
        os.getenv("ENVIRONMENT", "").lower() == "production"
        or os.getenv("RAILWAY_ENVIRONMENT", "").lower() == "production"
    )
    if is_prod:
        raise AcpClientError(
            "platform_orders_acp_token_not_configured",
            status_code=503,
            code="acp_token_missing",
        )
    return "test"


def _acp_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Map cart items to the ACP `{id, quantity}` shape. `id` prefers sku, then
    variant, then product id — matching how the platform-order ACP path picks it.

    P-T2.3.4: also carry `product_id` + `variant_id` when present. The pivota-acp
    session stores items as JSONB, so these persist to /complete where the real-
    capture path needs BOTH to build a Pivota quote (a Pivota quote requires a
    product_id AND a variant_id; the single ACP `id` is not enough). Omitted when
    absent so nothing changes for callers that don't supply them."""
    out: List[Dict[str, Any]] = []
    for item in items or []:
        product_id = str(item.get("product_id") or "").strip()
        variant_id = str(item.get("variant_id") or "").strip()
        item_id = (
            str(item.get("sku") or "").strip()
            or variant_id
            or product_id
            or "unknown"
        )
        qty = item.get("quantity")
        try:
            qty = int(qty) if qty is not None else 1
        except (TypeError, ValueError):
            qty = 1
        acp_item: Dict[str, Any] = {"id": item_id, "quantity": max(1, qty)}
        if product_id:
            acp_item["product_id"] = product_id
        if variant_id:
            acp_item["variant_id"] = variant_id
        out.append(acp_item)
    return out


def _acp_address(addr: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Map a Pivota checkout `shipping_address` (address_line1/address_line2) to
    the ACP session `Address` contract (line_one/line_two). pivota-acp's Address
    model *requires* line_one, so passing our shape straight through 422s. Accepts
    either shape (idempotent when already ACP-shaped) and drops empties. Returns
    None when there's no usable street line."""
    if not addr:
        return None
    a = dict(addr)
    line_one = (
        a.get("line_one") or a.get("address_line1") or a.get("line1") or a.get("addressLine1")
    )
    if not line_one:
        return None
    line_two = a.get("line_two") or a.get("address_line2") or a.get("line2") or a.get("addressLine2")
    out: Dict[str, Any] = {
        "line_one": str(line_one),
        "name": a.get("name"),
        "city": a.get("city"),
        "state": a.get("state"),
        "country": a.get("country"),
        "postal_code": a.get("postal_code") or a.get("postalCode") or a.get("zip"),
    }
    if line_two:
        out["line_two"] = str(line_two)
    return {k: v for k, v in out.items() if v is not None}


def _acp_buyer(buyer: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Map a Pivota buyer to the ACP session `Buyer` contract. pivota-acp's Buyer
    REQUIRES first_name + last_name, so our email-only buyer 422s the session. Map
    first/last (splitting a single `name` when needed); when both names aren't
    available, return None so the buyer is omitted (it's optional on the session)
    rather than failing the whole checkout. Email, when present, is preserved into
    metadata by the caller."""
    if not buyer:
        return None
    b = dict(buyer)
    first = b.get("first_name") or b.get("firstName")
    last = b.get("last_name") or b.get("lastName")
    if (not first or not last) and b.get("name"):
        parts = str(b["name"]).strip().split(None, 1)
        first = first or (parts[0] if parts else None)
        last = last or (parts[1] if len(parts) > 1 else None)
    if not (first and last):
        return None
    out: Dict[str, Any] = {"first_name": str(first), "last_name": str(last)}
    email = b.get("email")
    if email:
        out["email"] = email
    return out


async def create_checkout_session(
    *,
    merchant_id: str,
    platform: str,
    items: List[Dict[str, Any]],
    fulfillment_address: Optional[Dict[str, Any]] = None,
    buyer: Optional[Dict[str, Any]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    timeout: float = 30.0,
) -> AcpSession:
    """Create a pivota-acp checkout session. Threads pvt_* attribution into the
    session metadata. Raises AcpClientError on connection/HTTP/parse failure.
    Does NOT complete or charge."""
    base = _acp_base_url()
    if not base:
        raise AcpClientError("platform_orders_acp_url_not_configured", code="acp_url_missing")
    acp_items = _acp_items(items)
    if not acp_items:
        raise AcpClientError("items_required", status_code=400, code="acp_items_required")

    acp_metadata: Dict[str, Any] = {
        "merchant_id": merchant_id,
        "platform": platform,
    }
    # Attribution parity: carry the click id/surface into the session so the
    # order-completed webhook can deposit the edge (P-T2.0).
    src = dict(metadata or {})
    if has_attribution_signal(src):
        attribution = materialize_attribution_context(
            src, default_surface=str(src.get(PVT_SURFACE) or "acp"), merchant_id=merchant_id
        )
        for key in _ACP_ATTRIBUTION_KEYS:
            value = attribution.get(key)
            if value:
                acp_metadata[key] = str(value)
    # Pass through any caller metadata that isn't a reserved key (non-destructive).
    for k, v in src.items():
        acp_metadata.setdefault(k, v)

    # Preserve the buyer email into metadata even when the buyer object is
    # omitted below (ACP Buyer needs first+last names we may not have), so the
    # order-completed path can still recover it.
    if isinstance(buyer, dict) and buyer.get("email"):
        acp_metadata.setdefault("customer_email", buyer["email"])

    body: Dict[str, Any] = {"items": acp_items, "metadata": acp_metadata}
    acp_address = _acp_address(fulfillment_address)
    if acp_address:
        body["fulfillment_address"] = acp_address
    acp_buyer = _acp_buyer(buyer)
    if acp_buyer:
        body["buyer"] = acp_buyer

    headers = {
        "Authorization": f"Bearer {_resolve_bearer_token()}",
        "API-Version": PIVOTA_ACP_API_VERSION,
        "X-Merchant-Id": str(merchant_id),
        "X-Platform": str(platform or "shopify"),
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(f"{base}/checkout_sessions", headers=headers, json=body)
    except httpx.RequestError as exc:
        raise AcpClientError(f"acp_connection_error: {exc}", code="acp_connection_error") from exc

    if resp.status_code not in (200, 201):
        raise AcpClientError(
            f"acp_http_{resp.status_code}: {resp.text[:200]}",
            status_code=resp.status_code,
            code="acp_http_error",
        )

    try:
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise AcpClientError("acp_invalid_json", status_code=resp.status_code, code="acp_bad_json") from exc

    session_id = str((payload or {}).get("id") or "").strip()
    if not session_id:
        raise AcpClientError("acp_missing_session_id", status_code=resp.status_code, code="acp_no_session")

    totals = payload.get("totals") if isinstance(payload.get("totals"), list) else []
    total_obj = next((t for t in totals if isinstance(t, dict) and t.get("type") == "total"), None)
    total_cents = None
    if isinstance(total_obj, dict) and total_obj.get("amount") is not None:
        try:
            total_cents = int(total_obj["amount"])
        except (TypeError, ValueError):
            total_cents = None

    return AcpSession(
        session_id=session_id,
        checkout_url=f"{base}/checkout/{session_id}",
        status=str(payload.get("status") or "") or None,
        currency=str(payload.get("currency") or "") or None,
        total_cents=total_cents,
        totals=totals,
        raw=payload if isinstance(payload, dict) else {},
    )
