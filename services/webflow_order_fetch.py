"""Read Webflow orders back: one by id, or one offset page of the list.

A Webflow webhook delivery carries the order object inline, but this bridge
never maps it: the delivery is treated as a TRIGGER and the order is fetched.
Two reasons, and the second is the one that matters.

* For a site-token installation Webflow does not sign the delivery at all
  (routes/webflow_webhooks.py), so the body is only as trustworthy as the URL
  secret that admitted it — which proves the SENDER knows a secret, not that the
  money numbers in the body are Webflow's.
* Even for a signed delivery the body is a snapshot at send time; the fetch is
  the current state, and it is the same object the sweep lists, so both
  ingresses map identical input.

A failure here is retryable and must surface as a non-2xx from the receiver:
Webflow retries a failed delivery, and answering 200 after a failed fetch would
drop the event until the sweep's next run.

THE LIST HAS NO MODIFIED-SINCE FILTER. `GET /v2/sites/{id}/orders` takes
`status`, `offset` and `limit` (<= 100) and nothing else — no `modifiedAfter`,
no cursor. That single absence is what forces the sweep's design
(services/webflow_order_sweep.py) and it is why this module exposes an
offset-paged reader rather than a windowed one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx

from services.webflow_connection import WEBFLOW_API_ROOT, build_webflow_headers


# The order id is INTERPOLATED INTO A URL PATH, and everything that reaches here
# is attacker-influenced: it comes out of a webhook body, and a signature (when
# there is one at all) proves the sender, not the shape of a field. An id of
# `../../token/introspect` walks the path out of the orders collection and makes
# the fetch read a different endpoint entirely. Webflow order ids are short
# opaque tokens (`0000-0001`-shaped hyphenated groups, per the docs), so an
# allowlist costs nothing; the percent-encoding is the second belt for anything
# the pattern would let through.
WEBFLOW_ORDER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
WEBFLOW_FETCH_TIMEOUT_SECONDS = 15.0
# The receiver reads ONE order; a page reads at most 100. Neither is megabytes.
#
# MEASURED AFTER THE FACT, and deliberately not claimed otherwise. `httpx` has
# already buffered the whole response into `response.content` by the time this
# constant is consulted, so the check is a PARSE guard — it stops a wrong
# endpoint or a hostile redirect target from being handed to `json.loads` and
# expanded into objects — and NOT a memory bound. Bounding the read itself needs
# `client.stream(...)` with a running byte count, which is what
# `routes/webflow_webhooks.py::_read_limited_body` does for the INBOUND
# direction, where the sender is untrusted. Here the peer is api.webflow.com
# over TLS with `follow_redirects=False`, so the residual is a compromised or
# impersonated Webflow, and the timeout is the bound that applies to it.
MAX_WEBFLOW_RESPONSE_BYTES = 4_000_000
# Webflow's documented maximum page size.
WEBFLOW_MAX_PAGE_LIMIT = 100


class WebflowOrderFetchError(RuntimeError):
    """The order (or a page of orders) could not be read. Always retryable."""


class WebflowOrderUnauthorizedError(WebflowOrderFetchError):
    """Webflow refused THIS credential for the read (401/403).

    Its own type so a caller can tell "the token is wrong or its scope is
    missing" from "Webflow is having a bad day" — the first needs a human, the
    second needs a retry.
    """


class WebflowOrderNotFoundError(WebflowOrderFetchError):
    """Webflow has no such order for this site (404).

    Its own type because it is the one fetch failure that retrying cannot fix,
    and because it is the tell for a delivery that named an order belonging to
    a DIFFERENT site: the fetch is scoped to this store's `site_id`, so an order
    id from elsewhere simply is not there.
    """


@dataclass(frozen=True)
class WebflowOrderPage:
    orders: List[Dict[str, Any]]
    offset: int
    limit: int
    total: Optional[int]

    @property
    def next_offset(self) -> int:
        return self.offset + len(self.orders)


def _json_object(response: httpx.Response, what: str) -> Dict[str, Any]:
    if len(response.content or b"") > MAX_WEBFLOW_RESPONSE_BYTES:
        raise WebflowOrderFetchError(f"Webflow {what} response is oversized")
    try:
        payload = response.json()
    except Exception as exc:  # pragma: no cover - httpx raises subclasses
        raise WebflowOrderFetchError(f"Invalid Webflow {what} response") from exc
    if not isinstance(payload, dict):
        raise WebflowOrderFetchError(f"Invalid Webflow {what} response")
    return payload


def _raise_for_status(response: httpx.Response, what: str) -> None:
    if response.status_code == 200:
        return
    if response.status_code in (401, 403):
        raise WebflowOrderUnauthorizedError(
            f"Webflow refused the credential for the {what} read "
            f"(HTTP {response.status_code})"
        )
    if response.status_code == 404:
        raise WebflowOrderNotFoundError(f"Webflow {what} read found nothing (HTTP 404)")
    if response.status_code == 429:
        # ~60 requests/min per token, with `Retry-After`. Surfaced by name so a
        # sweep that trips the limit does not read as a broken credential.
        retry_after = str(response.headers.get("retry-after") or "").strip()
        raise WebflowOrderFetchError(
            f"Webflow rate-limited the {what} read"
            + (f" (retry after {retry_after}s)" if retry_after else "")
        )
    raise WebflowOrderFetchError(
        f"Webflow {what} read failed with HTTP {response.status_code}"
    )


def _validated_path_id(value: str, *, what: str) -> str:
    key = str(value or "").strip()
    if not WEBFLOW_ORDER_ID_PATTERN.match(key):
        # Refused rather than encoded-and-sent: a value outside this shape is
        # not a Webflow id, so the only thing a request built from it can do is
        # reach somewhere it should not.
        raise WebflowOrderFetchError(f"Webflow {what} is not a valid identifier")
    return quote(key, safe="")


async def fetch_webflow_order(
    *,
    api_token: str,
    site_id: str,
    order_id: str,
    timeout: float = WEBFLOW_FETCH_TIMEOUT_SECONDS,
    client: Optional[httpx.AsyncClient] = None,
) -> Dict[str, Any]:
    """`GET /v2/sites/{site_id}/orders/{order_id}` as a dict.

    The site id is in the path, so this read is SCOPED: an order id belonging to
    another site cannot be read through this store's credential even if a
    delivery names one. That is the structural half of the site binding; the
    receiver's payload check is the diagnostic half.
    """
    token = str(api_token or "").strip()
    if not token:
        raise WebflowOrderFetchError("Webflow order fetch credentials are incomplete")
    site = _validated_path_id(site_id, what="site id")
    order = _validated_path_id(order_id, what="order id")

    async def _call(http: httpx.AsyncClient) -> Dict[str, Any]:
        response = await http.get(
            f"{WEBFLOW_API_ROOT}/sites/{site}/orders/{order}",
            headers=build_webflow_headers(token),
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
    except WebflowOrderFetchError:
        raise
    except httpx.HTTPError as exc:
        raise WebflowOrderFetchError(f"Webflow order fetch failed: {exc}") from exc


async def fetch_webflow_order_page(
    *,
    api_token: str,
    site_id: str,
    offset: int = 0,
    limit: int = WEBFLOW_MAX_PAGE_LIMIT,
    status: Optional[str] = None,
    client: httpx.AsyncClient,
) -> WebflowOrderPage:
    """One offset page of `GET /v2/sites/{site_id}/orders`.

    `status` narrows the list to one lifecycle state; the sweep uses it to run a
    separate, cheap lane for `refunded` and `dispute-lost` rather than paging the
    entire order history looking for money that left.
    """
    token = str(api_token or "").strip()
    if not token:
        raise WebflowOrderFetchError("Webflow order list credentials are incomplete")
    site = _validated_path_id(site_id, what="site id")
    params: Dict[str, Any] = {
        "offset": max(0, int(offset)),
        "limit": max(1, min(int(limit), WEBFLOW_MAX_PAGE_LIMIT)),
    }
    if status:
        params["status"] = str(status).strip()
    try:
        response = await client.get(
            f"{WEBFLOW_API_ROOT}/sites/{site}/orders",
            headers=build_webflow_headers(token),
            params=params,
        )
    except httpx.HTTPError as exc:
        raise WebflowOrderFetchError(f"Webflow order list failed: {exc}") from exc
    _raise_for_status(response, "order list")
    payload = _json_object(response, "order list")
    # `orders` is the documented collection key; `items` is accepted as a second
    # spelling so a future/legacy envelope reads as an empty page loudly (the
    # sweep stops) rather than as a silently empty success.
    raw = payload.get("orders")
    if not isinstance(raw, list):
        raw = payload.get("items")
    orders = [dict(row) for row in raw if isinstance(row, dict)] if isinstance(raw, list) else []
    pagination = payload.get("pagination")
    pagination = pagination if isinstance(pagination, dict) else {}
    total = pagination.get("total")
    return WebflowOrderPage(
        orders=orders,
        offset=params["offset"],
        limit=params["limit"],
        total=int(total) if isinstance(total, int) and not isinstance(total, bool) else None,
    )
