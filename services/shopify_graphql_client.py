import logging
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)


async def shopify_admin_graphql(
    *,
    shop_domain: str,
    access_token: str,
    query: str,
    variables: Optional[Dict[str, Any]] = None,
    api_version: str = "2024-07",
    timeout_s: float = 15.0,
) -> Dict[str, Any]:
    """
    Minimal Shopify Admin GraphQL client.

    Endpoint: https://{shop_domain}/admin/api/{api_version}/graphql.json
    Header: X-Shopify-Access-Token
    """
    url = f"https://{shop_domain}/admin/api/{api_version}/graphql.json"
    headers = {
        "X-Shopify-Access-Token": access_token,
        "Content-Type": "application/json",
    }
    payload: Dict[str, Any] = {"query": query}
    if variables is not None:
        payload["variables"] = variables

    async with httpx.AsyncClient(timeout=timeout_s) as client:
        resp = await client.post(url, headers=headers, json=payload)
        text = resp.text
        if resp.status_code >= 400:
            logger.warning("Shopify GraphQL error: %s %s", resp.status_code, text[:800])
            raise RuntimeError(f"Shopify GraphQL HTTP {resp.status_code}")

        data = resp.json()
        if "errors" in data and data["errors"]:
            logger.warning("Shopify GraphQL top-level errors: %s", str(data["errors"])[:800])
            raise RuntimeError("Shopify GraphQL errors")

        return data.get("data") or {}

