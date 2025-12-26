from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx

from services.shopify_graphql_client import shopify_admin_graphql
from services.shopify_graphql_client import ShopifyGraphQLError
from services.return_records_service import upsert_shopify_return_record_best_effort
from utils.logger import logger


FALLBACK_ADMIN_API_VERSIONS = ["2025-01", "2024-10", "2024-07"]

_INTROSPECT_TYPE_FIELDS_QUERY = """
query TypeFields($name: String!) {
  __type(name: $name) {
    name
    fields { name }
  }
}
"""


def _returns_list_query(*, first: int) -> str:
    safe_first = max(1, min(int(first), 100))
    # Note: use a literal `first` value (no variables) so that when Shopify doesn't expose
    # the Returns API, the error surface is a single `undefinedField` (without `variableNotUsed` noise).
    return f"""
    query ListReturns {{
      returns(first: {safe_first}) {{
        nodes {{
          id
          status
          createdAt
          updatedAt
          order {{
            id
            legacyResourceId
          }}
        }}
      }}
    }}
    """


def _orders_with_returns_query(*, orders_first: int, returns_first: int) -> str:
    safe_orders_first = max(1, min(int(orders_first), 50))
    safe_returns_first = max(1, min(int(returns_first), 50))
    return f"""
    query ListOrdersWithReturns {{
      orders(first: {safe_orders_first}, sortKey: UPDATED_AT, reverse: true) {{
        nodes {{
          id
          legacyResourceId
          returns(first: {safe_returns_first}) {{
            nodes {{
              id
              status
              createdAt
              updatedAt
            }}
          }}
        }}
      }}
    }}
    """


async def _shopify_admin_rest_get(
    *,
    shop_domain: str,
    access_token: str,
    api_version: str,
    path: str,
    timeout_s: float = 15.0,
) -> Dict[str, Any]:
    url = f"https://{shop_domain}/admin/api/{api_version}/{path.lstrip('/')}"
    headers = {"X-Shopify-Access-Token": access_token, "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code >= 400:
            logger.warning("Shopify REST error: %s %s", resp.status_code, resp.text[:800])
            raise RuntimeError(f"Shopify REST HTTP {resp.status_code} path={path}")
        return resp.json() or {}


async def fetch_shopify_returns(
    *,
    shop_domain: str,
    access_token: str,
    api_version: str,
    first: int = 20,
) -> List[Dict[str, Any]]:
    data = await shopify_admin_graphql(
        shop_domain=shop_domain,
        access_token=access_token,
        query=_returns_list_query(first=first),
        api_version=api_version,
    )
    nodes = (((data or {}).get("returns") or {}).get("nodes")) or []
    return nodes if isinstance(nodes, list) else []


async def _introspect_type_fields(
    *,
    shop_domain: str,
    access_token: str,
    api_version: str,
    type_name: str,
) -> List[str]:
    data = await shopify_admin_graphql(
        shop_domain=shop_domain,
        access_token=access_token,
        query=_INTROSPECT_TYPE_FIELDS_QUERY,
        variables={"name": type_name},
        api_version=api_version,
    )
    fields = (((data or {}).get("__type") or {}).get("fields")) or []
    names: List[str] = []
    if isinstance(fields, list):
        for f in fields:
            if isinstance(f, dict) and f.get("name"):
                names.append(str(f["name"]))
    return names


def _filter_returnish_fields(fields: List[str]) -> List[str]:
    out: List[str] = []
    for f in fields or []:
        s = str(f or "")
        if "return" in s.lower():
            out.append(s)
    return sorted(set(out))


def _is_returns_undefined_field(err: ShopifyGraphQLError) -> bool:
    try:
        for e in (err.errors or [])[:10]:
            if not isinstance(e, dict):
                continue
            ext = e.get("extensions") or {}
            if not isinstance(ext, dict):
                continue
            if str(ext.get("code") or "") == "undefinedField" and str(ext.get("fieldName") or "") == "returns":
                return True
    except Exception:
        return False
    return False


async def _fetch_shopify_returns_via_orders_best_effort(
    *,
    shop_domain: str,
    access_token: str,
    api_version: str,
    orders_first: int,
    returns_first: int,
) -> List[Dict[str, Any]]:
    data = await shopify_admin_graphql(
        shop_domain=shop_domain,
        access_token=access_token,
        query=_orders_with_returns_query(orders_first=orders_first, returns_first=returns_first),
        api_version=api_version,
    )
    orders = (((data or {}).get("orders") or {}).get("nodes")) or []
    if not isinstance(orders, list):
        return []
    flattened: List[Dict[str, Any]] = []
    for o in orders:
        if not isinstance(o, dict):
            continue
        shopify_order_id = o.get("legacyResourceId") or o.get("id")
        returns = (((o.get("returns") or {}).get("nodes")) or []) if isinstance(o.get("returns"), dict) else []
        if not isinstance(returns, list):
            continue
        for r in returns:
            if not isinstance(r, dict):
                continue
            flattened.append({**r, "order_id": shopify_order_id})
    return flattened


async def sync_shopify_returns_best_effort(
    *,
    merchant_id: str,
    shop_domain: str,
    access_token: str,
    api_version: str,
    limit: int = 20,
    db=None,
) -> Dict[str, Any]:
    """
    Best-effort pull of latest returns via Admin GraphQL and upsert into return_records.
    Useful when webhooks aren't available/enabled yet.
    """
    graphql_attempts: List[Dict[str, Any]] = []
    graphql_orders_fallback_attempts: List[Dict[str, Any]] = []
    versions_to_try: List[str] = []
    try:
        versions_to_try = [str(api_version).strip()] if api_version else []
    except Exception:
        versions_to_try = []
    for v in FALLBACK_ADMIN_API_VERSIONS:
        if v not in versions_to_try:
            versions_to_try.append(v)

    try:
        nodes: List[Dict[str, Any]] = []
        api_version_used: Optional[str] = None
        last_graphql_error: Optional[ShopifyGraphQLError] = None

        for v in versions_to_try:
            try:
                nodes = await fetch_shopify_returns(
                    shop_domain=shop_domain,
                    access_token=access_token,
                    api_version=v,
                    first=limit,
                )
                api_version_used = v
                graphql_attempts.append({"api_version": v, "ok": True, "fetched": len(nodes)})
                break
            except ShopifyGraphQLError as e:
                last_graphql_error = e
                graphql_attempts.append(
                    {
                        "api_version": v,
                        "ok": False,
                        "error": str(e),
                        "request_id": getattr(e, "request_id", None),
                        "errors": (e.errors or [])[:2],
                    }
                )
                # If this isn't the "returns field missing" case, don't keep retrying versions.
                if not _is_returns_undefined_field(e):
                    raise

        if api_version_used is None and last_graphql_error is not None:
            raise last_graphql_error
    except ShopifyGraphQLError as e:
        is_returns_undefined = _is_returns_undefined_field(e)

        schema_diag = None
        if is_returns_undefined:
            # Gather safe schema hints to help decide the correct query path.
            try:
                diag_version = versions_to_try[0] if versions_to_try else api_version
                queryroot_fields = await _introspect_type_fields(
                    shop_domain=shop_domain,
                    access_token=access_token,
                    api_version=diag_version,
                    type_name="QueryRoot",
                )
                order_fields = await _introspect_type_fields(
                    shop_domain=shop_domain,
                    access_token=access_token,
                    api_version=diag_version,
                    type_name="Order",
                )
                shop_fields = await _introspect_type_fields(
                    shop_domain=shop_domain,
                    access_token=access_token,
                    api_version=diag_version,
                    type_name="Shop",
                )
                schema_diag = {
                    "api_version": diag_version,
                    "queryroot_returnish_fields": _filter_returnish_fields(queryroot_fields),
                    "order_returnish_fields": _filter_returnish_fields(order_fields),
                    "shop_returnish_fields": _filter_returnish_fields(shop_fields),
                }
            except Exception:
                schema_diag = None

        # If QueryRoot.returns is not available, it may still be available under Order.returns.
        if is_returns_undefined:
            for v in versions_to_try:
                try:
                    order_fields = await _introspect_type_fields(
                        shop_domain=shop_domain,
                        access_token=access_token,
                        api_version=v,
                        type_name="Order",
                    )
                    if "returns" not in set(order_fields):
                        graphql_orders_fallback_attempts.append(
                            {
                                "api_version": v,
                                "ok": False,
                                "error": "Order.returns not available in schema",
                            }
                        )
                        continue

                    nodes = await _fetch_shopify_returns_via_orders_best_effort(
                        shop_domain=shop_domain,
                        access_token=access_token,
                        api_version=v,
                        orders_first=max(5, min(int(limit), 20)),
                        returns_first=max(5, min(int(limit), 20)),
                    )
                    graphql_orders_fallback_attempts.append(
                        {"api_version": v, "ok": True, "fetched": len(nodes), "strategy": "Order.returns"}
                    )
                    break
                except ShopifyGraphQLError as e2:
                    graphql_orders_fallback_attempts.append(
                        {
                            "api_version": v,
                            "ok": False,
                            "error": str(e2),
                            "request_id": getattr(e2, "request_id", None),
                            "errors": (e2.errors or [])[:2],
                        }
                    )
                except Exception as e2:
                    graphql_orders_fallback_attempts.append(
                        {"api_version": v, "ok": False, "error": str(e2)}
                    )

            if nodes:
                # Continue to upsert below using the same upsert path.
                pass

        # Best-effort fallback: attempt REST returns endpoint (availability varies by shop/app).
        rest_error = None
        try:
            rest_data = await _shopify_admin_rest_get(
                shop_domain=shop_domain,
                access_token=access_token,
                api_version=api_version,
                path=f"returns.json?limit={max(1, min(int(limit), 250))}",
            )
            # Shopify REST shapes vary by version; try common keys.
            rest_nodes = (
                rest_data.get("returns")
                or rest_data.get("return_requests")
                or rest_data.get("returnRequests")
                or []
            )
            if isinstance(rest_nodes, list):
                nodes = rest_nodes
            else:
                nodes = []
        except Exception as e2:
            rest_error = str(e2)
            nodes = []

        if not nodes:
            return {
                "ok": False,
                "code": "RETURNS_API_UNAVAILABLE" if is_returns_undefined else "SHOPIFY_GRAPHQL_ERROR",
                "error": str(e),
                "errors": (e.errors or [])[:3],
                "request_id": getattr(e, "request_id", None),
                "graphql_attempts": graphql_attempts or None,
                "graphql_orders_fallback_attempts": graphql_orders_fallback_attempts or None,
                "attempted_api_versions": versions_to_try,
                "schema_diag": schema_diag,
                "rest_error": rest_error,
                "fetched": 0,
                "upserted": 0,
                "hint": (
                    "Shopify Returns API is not available for this shop/api_version. "
                    "If you expect returns, verify Shopify plan/features and try a newer Admin API version (e.g. 2024-10+)."
                    if is_returns_undefined
                    else None
                ),
            }
    except Exception as e:
        logger.warning(
            {"merchant_id": merchant_id, "shop_domain": shop_domain, "error": str(e)},
            "Failed to fetch Shopify returns",
        )
        return {"ok": False, "error": str(e), "fetched": 0, "upserted": 0}

    upserted = 0
    for r in nodes:
        try:
            # Normalize into the same payload-ish shape our webhook upsert understands.
            order = (r.get("order") or {}) if isinstance(r, dict) else {}
            payload: Dict[str, Any]
            if isinstance(r, dict) and ("createdAt" in r or "updatedAt" in r):
                payload = {
                    "id": r.get("id"),
                    "status": r.get("status"),
                    "created_at": r.get("createdAt"),
                    "updated_at": r.get("updatedAt"),
                    "order_id": order.get("legacyResourceId") or order.get("id"),
                }
            else:
                # REST-ish fallback shape
                payload = dict(r or {})
            await upsert_shopify_return_record_best_effort(
                merchant_id=merchant_id,
                payload=payload,
                topic="returns/sync",
                db=db,
            )
            upserted += 1
        except Exception:
            continue

    return {
        "ok": True,
        "fetched": len(nodes),
        "upserted": upserted,
        "graphql_attempts": graphql_attempts or None,
        "attempted_api_versions": versions_to_try,
    }
