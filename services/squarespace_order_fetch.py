"""Read Squarespace orders back: one by id, or a modified-window page.

A Squarespace webhook notification names `data.orderId` and carries no order
fields, so the receiver has to fetch the order before anything can be mapped —
the same shape as BigCommerce (services/bigcommerce_order_fetch.py).

A failure here is retryable and must surface as a non-2xx from the receiver:
Squarespace retries a failed notification delivery, and answering 200 after a
failed fetch would silently drop the event.

The list call is the reconciliation sweep's only read. Two constraints on it,
both recorded in docs/SQUARESPACE_TELEMETRY.md:

* `modifiedAfter` and `modifiedBefore` are a PAIR — one without the other is
  rejected;
* `cursor` is mutually exclusive with that pair, so the first page carries the
  bounds and every later page carries only the cursor.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx

from services.squarespace_connection import (
    SQUARESPACE_API_ROOT,
    build_squarespace_headers,
)


SQUARESPACE_ORDERS_PATH = "/commerce/orders"
SQUARESPACE_FETCH_TIMEOUT_SECONDS = 15.0
# The receiver reads ONE order; a body larger than this is not an order.
MAX_SQUARESPACE_ORDER_BYTES = 2_000_000


class SquarespaceOrderFetchError(RuntimeError):
    """The order (or a page of orders) could not be read. Always retryable."""


@dataclass(frozen=True)
class SquarespaceOrderPage:
    orders: List[Dict[str, Any]]
    next_cursor: Optional[str]


def _json_object(response: httpx.Response, what: str) -> Dict[str, Any]:
    if len(response.content or b"") > MAX_SQUARESPACE_ORDER_BYTES:
        raise SquarespaceOrderFetchError(f"Squarespace {what} response is oversized")
    try:
        payload = response.json()
    except Exception as exc:  # pragma: no cover - httpx raises subclasses
        raise SquarespaceOrderFetchError(f"Invalid Squarespace {what} response") from exc
    if not isinstance(payload, dict):
        raise SquarespaceOrderFetchError(f"Invalid Squarespace {what} response")
    return payload


def _raise_for_status(response: httpx.Response, what: str) -> None:
    if response.status_code == 200:
        return
    if response.status_code == 429:
        raise SquarespaceOrderFetchError(f"Squarespace rate-limited the {what} read")
    raise SquarespaceOrderFetchError(
        f"Squarespace {what} read failed with HTTP {response.status_code}"
    )


async def fetch_squarespace_order(
    *,
    access_token: str,
    order_id: str,
    timeout: float = SQUARESPACE_FETCH_TIMEOUT_SECONDS,
    client: Optional[httpx.AsyncClient] = None,
) -> Dict[str, Any]:
    """`GET /1.0/commerce/orders/{id}` as a dict."""
    token = str(access_token or "").strip()
    key = str(order_id or "").strip()
    if not token or not key:
        raise SquarespaceOrderFetchError("Squarespace order fetch credentials are incomplete")

    async def _call(http: httpx.AsyncClient) -> Dict[str, Any]:
        response = await http.get(
            f"{SQUARESPACE_API_ROOT}{SQUARESPACE_ORDERS_PATH}/{key}",
            headers=build_squarespace_headers(token),
        )
        _raise_for_status(response, "order")
        return _json_object(response, "order")

    if client is not None:
        return await _call(client)
    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=False, trust_env=False
        ) as http:
            return await _call(http)
    except SquarespaceOrderFetchError:
        raise
    except httpx.HTTPError as exc:
        raise SquarespaceOrderFetchError(f"Squarespace order fetch failed: {exc}") from exc


async def fetch_squarespace_order_page(
    *,
    access_token: str,
    modified_after: Optional[str] = None,
    modified_before: Optional[str] = None,
    cursor: Optional[str] = None,
    client: httpx.AsyncClient,
) -> SquarespaceOrderPage:
    """One page of `GET /1.0/commerce/orders`.

    Pass EITHER the `modified_after`/`modified_before` pair (first page) OR a
    `cursor` (every later page). Squarespace rejects the two together.
    """
    token = str(access_token or "").strip()
    if not token:
        raise SquarespaceOrderFetchError("Squarespace order list credentials are incomplete")
    params: Dict[str, Any] = {}
    if cursor:
        params["cursor"] = cursor
    else:
        if not modified_after or not modified_before:
            raise SquarespaceOrderFetchError(
                "Squarespace order list needs both modifiedAfter and modifiedBefore"
            )
        params["modifiedAfter"] = modified_after
        params["modifiedBefore"] = modified_before
    try:
        response = await client.get(
            f"{SQUARESPACE_API_ROOT}{SQUARESPACE_ORDERS_PATH}",
            headers=build_squarespace_headers(token),
            params=params,
        )
    except httpx.HTTPError as exc:
        raise SquarespaceOrderFetchError(f"Squarespace order list failed: {exc}") from exc
    _raise_for_status(response, "order list")
    payload = _json_object(response, "order list")
    raw_orders = payload.get("result")
    orders = (
        [dict(row) for row in raw_orders if isinstance(row, dict)]
        if isinstance(raw_orders, list)
        else []
    )
    pagination = payload.get("pagination")
    pagination = pagination if isinstance(pagination, dict) else {}
    # The presence of `nextPageCursor` is the continuation signal, not
    # `hasNextPage`: the cursor is what the next request actually needs, and
    # gating on the boolean alone would truncate a sweep if the envelope ever
    # stopped sending that flag. Squarespace omits the cursor on the last page.
    next_cursor = str(pagination.get("nextPageCursor") or "").strip() or None
    return SquarespaceOrderPage(orders=orders, next_cursor=next_cursor)
