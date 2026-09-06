"""Fetch the order a BigCommerce webhook delivery only points at.

A BigCommerce delivery carries `{"data": {"type": "order", "id": 250}}` and no
order fields at all, so the receiver has to read the order back from the store
with the merchant's stored access token before anything can be mapped.

A failure here is retryable and must surface as a non-2xx: BigCommerce retries
a failed delivery at escalating intervals over roughly 48 hours, so answering
200 after a failed fetch would silently drop the event
(https://docs.bigcommerce.com/docs/integrations/webhooks).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

from adapters.bigcommerce_adapter import build_bigcommerce_headers
from services.bigcommerce_event_adapter import refunds_are_relevant


BIGCOMMERCE_API_ROOT = "https://api.bigcommerce.com"
BIGCOMMERCE_FETCH_TIMEOUT_SECONDS = 15.0


class BigCommerceOrderFetchError(RuntimeError):
    """The order (or its refunds) could not be read back. Always retryable."""


@dataclass(frozen=True)
class BigCommerceOrderContext:
    order: Dict[str, Any]
    refunds: List[Dict[str, Any]] = field(default_factory=list)


def _refund_rows(payload: Any) -> List[Dict[str, Any]]:
    """The refund objects out of a v3 `{"data": [...], "meta": {...}}` body.

    A bare list is accepted too so a future/legacy shape does not turn a real
    refund into a silent zero.
    """
    rows = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return []
    return [dict(row) for row in rows if isinstance(row, dict)]


async def fetch_bigcommerce_order_context(
    *,
    store_hash: str,
    access_token: str,
    client_id: Optional[str],
    order_id: str,
    scope: str,
    timeout: float = BIGCOMMERCE_FETCH_TIMEOUT_SECONDS,
) -> BigCommerceOrderContext:
    hash_value = str(store_hash or "").strip()
    token = str(access_token or "").strip()
    order_key = str(order_id or "").strip()
    if not hash_value or not token or not order_key:
        raise BigCommerceOrderFetchError("BigCommerce order fetch credentials are incomplete")

    headers = build_bigcommerce_headers(token, client_id)
    base = f"{BIGCOMMERCE_API_ROOT}/stores/{hash_value}"
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            order_response = await client.get(f"{base}/v2/orders/{order_key}", headers=headers)
            if order_response.status_code != 200:
                raise BigCommerceOrderFetchError(
                    f"BigCommerce order fetch failed with HTTP {order_response.status_code}"
                )
            try:
                order = order_response.json()
            except Exception as exc:  # pragma: no cover - httpx raises subclasses
                raise BigCommerceOrderFetchError("Invalid BigCommerce order response") from exc
            if not isinstance(order, dict):
                raise BigCommerceOrderFetchError("Invalid BigCommerce order response")

            if not refunds_are_relevant(scope, order):
                return BigCommerceOrderContext(order=dict(order), refunds=[])

            refunds_response = await client.get(
                f"{base}/v3/orders/{order_key}/payment_actions/refunds",
                headers=headers,
            )
            if refunds_response.status_code != 200:
                raise BigCommerceOrderFetchError(
                    f"BigCommerce refund fetch failed with HTTP "
                    f"{refunds_response.status_code}"
                )
            try:
                refunds_payload = refunds_response.json()
            except Exception as exc:  # pragma: no cover - httpx raises subclasses
                raise BigCommerceOrderFetchError("Invalid BigCommerce refund response") from exc
            return BigCommerceOrderContext(
                order=dict(order),
                refunds=_refund_rows(refunds_payload),
            )
    except BigCommerceOrderFetchError:
        raise
    except httpx.HTTPError as exc:
        raise BigCommerceOrderFetchError(f"BigCommerce order fetch failed: {exc}") from exc
