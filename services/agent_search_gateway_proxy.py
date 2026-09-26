"""Serve ``GET /agent/v1/beauty/products/search`` from the gateway -- one search implementation.

WHY. ADR-021 (founder decision 2026-08-01) makes PIVOTA-Agent -- the gateway -- the protocol door
for every external caller, and calls this backend's ``/agent/v1`` API "Pivota's internal test lane,
not the durable external contract". The backend's public search was never closed, and partners
integrated against it: Meitu calls this exact endpoint. So two search implementations serve
partners and drift apart. Measured 2026-09-18, this endpoint returns ZERO products for every query
(24 seed rows fetched, 0 served, no drop reason recorded), while the gateway serves Meitu's exact
request -- ``market=SG`` included -- with the reported product at #2. Fixes had gone into the
gateway; the partner was on the other door.

This makes the endpoint a thin forward to the gateway's product search
(``/agent/v1/products/search`` with ``catalog_surface=beauty``), and maps the answer back into this
endpoint's response contract so callers see no change of shape. Partners keep their URLs; they
migrate to the gateway later, once the result is proven.

SCOPE, deliberately narrow:
* The beauty alias only. The hop header prevents a request from forwarding twice.
* Search only. ``/agent/v1/products/resolve`` is NOT proxied: the gateway's resolve cannot resolve
  Meitu's variant id either (``no_candidates``, probed 2026-09-18), so forwarding it would trade
  one failure for another. That fix is backend-side (pivota-backend #2189).

ROLLOUT is two env vars, read per call:
* ``AGENT_BEAUTY_SEARCH_VIA_GATEWAY`` -- ``on`` enables it; anything else (the default) leaves the
  endpoint exactly as it was.
* ``AGENT_BEAUTY_SEARCH_VIA_GATEWAY_AGENT_IDS`` -- optional comma list of agent ids. When set, only
  those callers are forwarded, so the switch can be proven on one caller before it reaches a
  partner. Empty means every caller.

LOOPS AND FAILURES. The forwarded request carries ``X-Pivota-Search-Proxy-Hop``; a request that
arrives carrying it is never forwarded again. A failed gateway request returns an explicit error.
It must not silently change the external recall lane.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Mapping, Optional, Tuple

import httpx

FLAG = "AGENT_BEAUTY_SEARCH_VIA_GATEWAY"
AGENT_IDS_FLAG = "AGENT_BEAUTY_SEARCH_VIA_GATEWAY_AGENT_IDS"
HOP_HEADER = "X-Pivota-Search-Proxy-Hop"
PROXY_SOURCE = "api_rest_proxy"
GATEWAY_PATH = "/agent/v1/products/search"
TIMEOUT_SECONDS = 8.0

# Query parameters the gateway must not receive from the caller: they are set here.
_OWNED_PARAMS = {"catalog_surface", "source", "allow_external_seed", "external_seed_strategy"}

_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    """One shared client (keep-alive to the gateway). Tests replace this function."""
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT_SECONDS))
    return _client


def enabled_for(agent_id: Optional[str], headers: Mapping[str, str], env: Mapping[str, str] = os.environ) -> Tuple[bool, str]:
    """Whether THIS request is forwarded, and why not when it is not."""
    if str(env.get(FLAG, "")).strip().lower() not in {"1", "true", "on", "yes"}:
        return False, "flag_off"
    allowed = {item.strip() for item in str(env.get(AGENT_IDS_FLAG, "")).split(",") if item.strip()}
    if allowed and str(agent_id or "") not in allowed:
        return False, "caller_not_enabled"
    if any(key.lower() == HOP_HEADER.lower() for key in headers.keys()):
        return False, "already_proxied"
    return True, "enabled"


def gateway_params(
    query_items: List[Tuple[str, str]], *, catalog_surface: Optional[str] = "beauty"
) -> List[Tuple[str, str]]:
    """Preserve request filters while enforcing one source-neutral recall contract."""
    kept = [(key, value) for key, value in query_items if key not in _OWNED_PARAMS]
    if catalog_surface:
        kept.append(("catalog_surface", catalog_surface))
    return kept + [
        ("allow_external_seed", "true"),
        ("external_seed_strategy", "unified_relevance"),
        ("source", PROXY_SOURCE),
    ]


def gateway_headers(headers: Mapping[str, str]) -> Dict[str, str]:
    """The caller's OWN credential, so the gateway authenticates the same agent (and its limits,
    telemetry and key fingerprint stay per-caller). Never a service credential."""
    lowered = {key.lower(): value for key, value in headers.items()}
    out: Dict[str, str] = {HOP_HEADER: "1", "accept": "application/json"}
    api_key = lowered.get("x-api-key") or lowered.get("x-agent-api-key")
    if api_key:
        out["x-agent-api-key"] = api_key
        out["x-api-key"] = api_key
    authorization = lowered.get("authorization")
    if authorization:
        out["authorization"] = authorization
    return out


def to_backend_envelope(
    gateway_body: Dict[str, Any],
    *,
    limit: int,
    offset: int,
    query: Optional[str],
    category: Optional[str],
    min_price: Optional[float],
    max_price: Optional[float],
    in_stock_only: bool,
    merchant_id: Optional[str],
    merchant_ids: Optional[List[str]],
    catalog_surface: Optional[str] = "beauty",
) -> Dict[str, Any]:
    """The gateway's answer in THIS endpoint's contract: the same top-level keys, the same
    pagination fields, the products as the gateway serves them."""
    products = gateway_body.get("products")
    products = products if isinstance(products, list) else []
    try:
        total = int(gateway_body.get("total"))
    except (TypeError, ValueError):
        total = len(products)
    total = max(total, len(products))
    limit = max(1, int(limit or 1))
    offset = max(0, int(offset or 0))
    gateway_metadata = gateway_body.get("metadata") if isinstance(gateway_body.get("metadata"), dict) else {}
    return {
        "status": "success",
        "products": products,
        "pagination": {
            "total_count": total,
            "limit": limit,
            "offset": offset,
            "page": (offset // limit) + 1,
            "total_pages": (total + limit - 1) // limit,
            "has_more": offset + limit < total,
        },
        "search_context": {
            "merchant_id": merchant_id,
            "merchant_ids": merchant_ids,
            "merchants_searched": None,
            "cross_merchant_search": merchant_id is None and not merchant_ids,
            "catalog_surface": catalog_surface,
        },
        "filters_applied": {
            "query": query,
            "category": category,
            "catalog_surface": catalog_surface,
            "min_price": min_price,
            "max_price": max_price,
            "in_stock_only": in_stock_only,
        },
        "metadata": {
            **gateway_metadata,
            "source": "agent_search_products",
            "catalog_surface": catalog_surface,
            "reason_code": "ok" if products else "no_candidates",
            "served_by": "gateway",
            "gateway_query_source": gateway_metadata.get("query_source"),
        },
    }


# The gateway rejects a search query over its length limit with 400 QUERY_TOO_LONG and says what the
# limit is. That is the one gateway error a caller can act on by itself (shorten the query), so its code
# and these fields pass through; every other gateway failure stays an opaque gateway_search_failed.
_QUERY_TOO_LONG_FIELDS = ("message", "field", "max_chars", "length")


def _query_too_long_detail(response: httpx.Response) -> Optional[Dict[str, Any]]:
    try:
        body = response.json()
    except Exception:
        return None
    if not isinstance(body, dict) or body.get("error") != "QUERY_TOO_LONG":
        return None
    detail: Dict[str, Any] = {"code": "QUERY_TOO_LONG"}
    for key in _QUERY_TOO_LONG_FIELDS:
        value = body.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int) or (isinstance(value, str) and len(value) <= 200):
            detail[key] = value
    return detail


def error_content(reason: str, gateway_error: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The caller-facing error body for a failed proxied search."""
    if gateway_error:
        return {"status": "error", "error": dict(gateway_error)}
    return {"status": "error", "error": {"code": "gateway_search_failed", "reason": reason}}


async def search(
    *,
    base_url: str,
    query_items: List[Tuple[str, str]],
    headers: Mapping[str, str],
    catalog_surface: Optional[str] = "beauty",
) -> Tuple[Optional[Dict[str, Any]], str, int, Optional[Dict[str, Any]]]:
    """Forward once. Return body, safe reason, caller-facing HTTP status and the gateway error a caller
    may see (only QUERY_TOO_LONG; else None); never raise."""
    url = f"{str(base_url).rstrip('/')}{GATEWAY_PATH}"
    try:
        response = await _get_client().get(
            url,
            params=gateway_params(query_items, catalog_surface=catalog_surface),
            headers=gateway_headers(headers),
        )
    except httpx.TimeoutException:
        return None, "gateway_timeout", 504, None
    except httpx.RequestError:
        return None, "gateway_unavailable", 503, None
    if response.status_code != 200:
        status = response.status_code
        if status == 400:
            detail = _query_too_long_detail(response)
            if detail is not None:
                return None, "query_too_long", 400, detail
        # Preserve actionable caller and rate-limit errors, but do not expose upstream internals.
        return None, f"gateway_http_{status}", status if status in {400, 401, 403, 404, 422, 429, 503, 504} else 502, None
    try:
        body = response.json()
    except Exception:
        return None, "gateway_invalid_json", 502, None
    if not isinstance(body, dict) or not isinstance(body.get("products"), list):
        return None, "gateway_unexpected_shape", 502, None
    gateway_status = str(body.get("status", "success")).lower()
    # The gateway uses ``status=failed`` with HTTP 200 for a resolved, empty
    # search decision (for example, no candidates in the selected market). It
    # is a valid terminal search response when no error payload exists. Keep
    # explicit application errors visible while preserving empty-result HTTP
    # semantics for callers of the backend compatibility door.
    if gateway_status in {"error", "failed"} and body.get("error") is not None:
        return None, "gateway_failed_response", 502, None
    return body, "ok", 200, None
