import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

from db.database import database
from services.pcs_hash import sha256_hex
from services.shopify_graphql_client import ShopifyGraphQLError, shopify_admin_graphql

logger = logging.getLogger(__name__)


SHOP_POLICIES_QUERY = """
query ShopPolicies {
  shop {
    primaryDomain { host url }
    refundPolicy { title url body updatedAt }
    shippingPolicy { title url body updatedAt }
    privacyPolicy { title url body updatedAt }
    termsOfService { title url body updatedAt }
  }
}
"""

POLICIES_REST_PATH = "/admin/api/{api_version}/policies.json"


_POLICY_URL_PATH_BY_TYPE = {
    "refund": "/policies/refund-policy",
    "shipping": "/policies/shipping-policy",
    "privacy": "/policies/privacy-policy",
    "terms": "/policies/terms-of-service",
}


def _derived_policy_url(shop_domain: str, policy_type: str) -> Optional[str]:
    path = _POLICY_URL_PATH_BY_TYPE.get(policy_type)
    if not shop_domain or not path:
        return None
    return f"https://{shop_domain}{path}"


def _normalize_policy_type(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    v = str(raw).strip().lower()
    if "refund" in v or "return" in v:
        return "refund"
    if "shipping" in v or "delivery" in v or "fulfillment" in v:
        return "shipping"
    if "privacy" in v:
        return "privacy"
    if "terms" in v or "tos" in v or "service" in v:
        return "terms"
    return None


def _extract_policies_from_rest(rest: Any) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Normalize Shopify REST policies response into a list of dicts.

    Shopify returns different shapes across versions/app types:
    - {"policies": [ {...}, ... ]}
    - {"policies": { "refund_policy": {...}, ... }}
    - [ {...}, ... ]
    """
    debug: Dict[str, Any] = {"rest_type": type(rest).__name__}

    if isinstance(rest, list):
        items = [x for x in rest if isinstance(x, dict)]
        debug.update({"shape": "list_root", "items_count": len(items)})
        return items, debug

    if not isinstance(rest, dict):
        debug.update({"shape": "unknown", "items_count": 0})
        return [], debug

    policies = rest.get("policies")
    debug["top_keys"] = sorted(list(rest.keys()))[:25]

    if isinstance(policies, list):
        items = [x for x in policies if isinstance(x, dict)]
        debug.update({"shape": "dict.policies_list", "items_count": len(items)})
        return items, debug

    if isinstance(policies, dict):
        items: List[Dict[str, Any]] = []
        for key, value in policies.items():
            if isinstance(value, dict):
                merged = dict(value)
                # Preserve the dict key as a hint for type inference.
                merged.setdefault("_policy_key", key)
                items.append(merged)
            else:
                items.append({"_policy_key": key, "value": value})
        debug.update({"shape": "dict.policies_dict", "items_count": len(items)})
        return items, debug

    debug.update({"shape": "dict.policies_missing_or_unknown", "items_count": 0})
    return [], debug


def summarize_policies_rest_response(rest: Any, *, max_items: int = 6) -> Dict[str, Any]:
    """
    Return a safe diagnostics summary (no body content) for onboarding/debugging.
    """
    items, debug = _extract_policies_from_rest(rest)
    preview: List[Dict[str, Any]] = []
    for item in items[: max(0, max_items)]:
        if not isinstance(item, dict):
            continue
        body = item.get("body") or item.get("body_html") or item.get("bodyHtml") or ""
        preview.append(
            {
                "policy_key": item.get("_policy_key"),
                "type": item.get("type"),
                "handle": item.get("handle"),
                "title": item.get("title"),
                "url": item.get("url"),
                "updated_at": item.get("updatedAt") or item.get("updated_at"),
                "body_len": len(str(body)) if body else 0,
                "keys": sorted(list(item.keys()))[:20],
            }
        )
    debug["preview"] = preview
    return debug


async def _fetch_policies_rest(
    *, shop_domain: str, access_token: str, api_version: str, timeout_s: float = 12.0
) -> Dict[str, Any]:
    """
    Best-effort REST fallback. Some Shopify versions/stores do not expose policy fields on Admin GraphQL Shop.
    """
    url = f"https://{shop_domain}{POLICIES_REST_PATH.format(api_version=api_version)}"
    headers = {"X-Shopify-Access-Token": access_token}
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code >= 400:
            raise RuntimeError(f"Shopify REST policies HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json() or {}


def _hash_policy_body(body_html: Optional[str]) -> str:
    body = (body_html or "").encode("utf-8")
    return sha256_hex(body)


def _parse_shopify_datetime(value: Any) -> Optional[datetime]:
    """
    Shopify timestamps are typically ISO-8601 strings (e.g. 2025-12-22T16:35:29+01:00).
    asyncpg expects a Python datetime for TIMESTAMPTZ parameters.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        # Interpret as unix seconds (best-effort).
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except Exception:
            return None
    if not isinstance(value, str):
        return None

    v = value.strip()
    if not v:
        return None
    # Normalize Zulu suffix.
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(v)
    except Exception:
        return None


def _normalize_policy(policy: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not policy:
        return {"title": None, "url": None, "body_html": None, "updated_at": None, "hash_sha256": _hash_policy_body(None)}
    body = policy.get("body")
    return {
        "title": policy.get("title"),
        "url": policy.get("url"),
        "body_html": body,
        "updated_at": _parse_shopify_datetime(policy.get("updatedAt")),
        "hash_sha256": _hash_policy_body(body),
    }


async def fetch_and_store_shop_policies(
    *,
    merchant_id: str,
    shop_domain: str,
    access_token: str,
    api_version: str = "2025-10",
) -> Dict[str, Dict[str, Any]]:
    """
    Fetch shop policies and upsert a snapshot into pcs_shop_policies (append-only by hash).

    Primary: Admin GraphQL (legacy query).
    Fallback: Admin REST `GET /policies.json` when the GraphQL schema doesn't expose policy fields.

    Returns normalized policies mapping {policy_type -> policy_dict}.
    """
    shop: Dict[str, Any] = {}
    policies: Dict[str, Dict[str, Any]] = {}

    try:
        data = await shopify_admin_graphql(
            shop_domain=shop_domain,
            access_token=access_token,
            query=SHOP_POLICIES_QUERY,
            api_version=api_version,
        )
        shop = (data or {}).get("shop") or {}

        policies = {
            "refund": _normalize_policy(shop.get("refundPolicy")),
            "shipping": _normalize_policy(shop.get("shippingPolicy")),
            "privacy": _normalize_policy(shop.get("privacyPolicy")),
            "terms": _normalize_policy(shop.get("termsOfService")),
        }
    except ShopifyGraphQLError as e:
        # When policy fields are removed from Shop, Shopify returns undefinedField errors.
        # In that case, fall back to REST policies endpoint.
        codes = [((err.get("extensions") or {}).get("code")) for err in (e.errors or [])]
        if any(c == "undefinedField" for c in codes):
            rest = await _fetch_policies_rest(
                shop_domain=shop_domain, access_token=access_token, api_version=api_version
            )
            raw_list, _debug = _extract_policies_from_rest(rest)

            for item in raw_list:
                if not isinstance(item, dict):
                    continue
                # Typical shapes seen in practice: {type/handle, title, body, url, updated_at/updatedAt}
                policy_type = (
                    _normalize_policy_type(item.get("type"))
                    or _normalize_policy_type(item.get("handle"))
                    or _normalize_policy_type(item.get("_policy_key"))
                    or _normalize_policy_type(item.get("name"))
                    or _normalize_policy_type(item.get("title"))
                    or _normalize_policy_type(item.get("url"))
                )
                if policy_type not in ("refund", "shipping", "privacy", "terms"):
                    continue

                body = item.get("body") or item.get("body_html") or item.get("bodyHtml")
                url = item.get("url") or _derived_policy_url(shop_domain, policy_type)
                policies[policy_type] = {
                    "title": item.get("title"),
                    "url": url,
                    "body_html": body,
                    "updated_at": _parse_shopify_datetime(item.get("updatedAt") or item.get("updated_at")),
                    "hash_sha256": _hash_policy_body(body),
                }
        else:
            raise

    if not policies:
        return {}

    # Ensure url is always present to satisfy pcs_shop_policies.url NOT NULL (best-effort derived url).
    for policy_type, p in list(policies.items()):
        if not p.get("url"):
            p["url"] = _derived_policy_url(shop_domain, policy_type)
        policies[policy_type] = p

    # Append-only inserts; duplicates are ignored by unique constraint.
    for policy_type, p in policies.items():
        if not p.get("url"):
            continue
        try:
            await database.execute(
                """
                INSERT INTO pcs_shop_policies
                  (merchant_id, policy_type, url, title, body_html, updated_at, hash_sha256, fetched_at)
                VALUES
                  (:merchant_id, :policy_type, :url, :title, :body_html, :updated_at, :hash_sha256, NOW())
                ON CONFLICT (merchant_id, policy_type, hash_sha256) DO NOTHING
                """,
                {
                    "merchant_id": merchant_id,
                    "policy_type": policy_type,
                    "url": p.get("url"),
                    "title": p.get("title"),
                    "body_html": p.get("body_html"),
                    "updated_at": p.get("updated_at"),
                    "hash_sha256": p.get("hash_sha256"),
                },
            )
        except Exception as e:
            logger.warning("Failed to store policy snapshot merchant=%s type=%s: %s", merchant_id, policy_type, e)

    return policies


async def get_latest_policy_hashes(merchant_id: str) -> List[Dict[str, Any]]:
    """
    Return latest policy hashes by type for a merchant. If none exist, returns [].
    """
    rows = await database.fetch_all(
        """
        SELECT DISTINCT ON (policy_type)
          policy_type, url, updated_at, hash_sha256, fetched_at
        FROM pcs_shop_policies
        WHERE merchant_id = :merchant_id
        ORDER BY policy_type, fetched_at DESC
        """,
        {"merchant_id": merchant_id},
    )
    return [dict(r) for r in rows]
