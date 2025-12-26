from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx

from services.shopify_graphql_client import shopify_admin_graphql
from services.shopify_graphql_client import ShopifyGraphQLError
from services.return_records_service import upsert_shopify_return_record_best_effort
from utils.logger import logger


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
    try:
        nodes = await fetch_shopify_returns(
            shop_domain=shop_domain,
            access_token=access_token,
            api_version=api_version,
            first=limit,
        )
    except ShopifyGraphQLError as e:
        # Shopify can entirely omit the Returns API from the Admin GraphQL schema (e.g. store/plan/feature gating).
        # When that happens, the query fails with `undefinedField` for QueryRoot. We should treat it as a
        # "not supported" condition (actionable), not a generic 500.
        is_returns_undefined = False
        try:
            for err in (e.errors or [])[:5]:
                if not isinstance(err, dict):
                    continue
                ext = err.get("extensions") or {}
                if not isinstance(ext, dict):
                    continue
                if str(ext.get("code") or "") == "undefinedField" and str(ext.get("fieldName") or "") == "returns":
                    is_returns_undefined = True
                    break
        except Exception:
            is_returns_undefined = False

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

    return {"ok": True, "fetched": len(nodes), "upserted": upserted}
