"""Read back the order a Wix Order Transactions delivery only points at.

Order-domain deliveries carry the whole Order entity, so they need nothing from
here. The two Order Transactions events do: their body is
``{orderId, refund, sideEffects, orderTransactions}`` and a Wix ``Price`` is
``{amount, formattedAmount}`` — there is **no currency anywhere in it**, and
``services/merchant_commerce_event_funnel_service.py`` drops a money row whose
currency is empty. The order also supplies ``buyerInfo`` and the
``channelInfo.externalOrderId`` the canonical ``order_ref`` is built from.

A failure here is retryable and must surface as a non-2xx: Wix retries a
delivery that does not answer 200 up to 12 more times over ~48 hours
(https://dev.wix.com/docs/build-apps/develop-your-app/api-integrations/events-and-webhooks/about-webhooks.md),
so a 200 after a failed fetch would drop the refund for good.

Endpoint and headers are the ones the catalog/writeback adapters already use:
``GET https://www.wixapis.com/ecom/v1/orders/{orderId}`` with
``adapters``' ``build_wix_api_key_headers`` (a Wix API key is the raw
``Authorization`` value, NOT a bearer token) —
https://dev.wix.com/docs/api-reference/business-solutions/e-commerce/orders/orders/get-order.md
"""

from __future__ import annotations

from typing import Any, Dict

import httpx

from services.wix_connection import build_wix_api_key_headers


WIX_ECOM_ORDER_URL_TEMPLATE = "https://www.wixapis.com/ecom/v1/orders/{order_id}"
WIX_FETCH_TIMEOUT_SECONDS = 15.0


class WixOrderFetchError(RuntimeError):
    """The order could not be read back. Always retryable."""


def _order_body(payload: Any) -> Dict[str, Any]:
    """The Order object out of a ``{"order": {...}}`` envelope, or a bare body."""
    if not isinstance(payload, dict):
        return {}
    order = payload.get("order")
    if isinstance(order, dict):
        return dict(order)
    return dict(payload)


async def fetch_wix_order(
    *,
    api_key: str,
    site_id: str,
    order_id: str,
    timeout: float = WIX_FETCH_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    key = str(api_key or "").strip()
    site = str(site_id or "").strip()
    order_key = str(order_id or "").strip()
    if not key or not site or not order_key:
        raise WixOrderFetchError("Wix order fetch credentials are incomplete")

    headers = build_wix_api_key_headers(key, site)
    url = WIX_ECOM_ORDER_URL_TEMPLATE.format(order_id=order_key)
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise WixOrderFetchError(f"Wix order fetch failed: {exc}") from exc

    if response.status_code != 200:
        raise WixOrderFetchError(f"Wix order fetch failed with HTTP {response.status_code}")
    try:
        payload = response.json()
    except Exception as exc:  # pragma: no cover - httpx raises subclasses
        raise WixOrderFetchError("Invalid Wix order response") from exc
    order = _order_body(payload)
    if not order:
        raise WixOrderFetchError("Invalid Wix order response")
    return order
