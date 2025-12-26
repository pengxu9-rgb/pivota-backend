from __future__ import annotations

from typing import Any, Dict, List, Optional

from services.shopify_graphql_client import shopify_admin_graphql
from services.return_records_service import upsert_shopify_return_record_best_effort
from utils.logger import logger


RETURNS_LIST_QUERY = """
query ListReturns($first: Int!) {
  returns(first: $first) {
    nodes {
      id
      status
      createdAt
      updatedAt
      order {
        id
        legacyResourceId
      }
    }
  }
}
"""


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
        query=RETURNS_LIST_QUERY,
        variables={"first": max(1, min(int(first), 100))},
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
            order = r.get("order") or {}
            payload: Dict[str, Any] = {
                "id": r.get("id"),
                "status": r.get("status"),
                "created_at": r.get("createdAt"),
                "updated_at": r.get("updatedAt"),
                "order_id": order.get("legacyResourceId") or order.get("id"),
            }
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

