import logging
from typing import Any, Dict, List, Optional

from db.database import database
from services.pcs_hash import sha256_hex
from services.shopify_graphql_client import shopify_admin_graphql

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


def _hash_policy_body(body_html: Optional[str]) -> str:
    body = (body_html or "").encode("utf-8")
    return sha256_hex(body)


def _normalize_policy(policy: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not policy:
        return {"title": None, "url": None, "body_html": None, "updated_at": None, "hash_sha256": _hash_policy_body(None)}
    body = policy.get("body")
    return {
        "title": policy.get("title"),
        "url": policy.get("url"),
        "body_html": body,
        "updated_at": policy.get("updatedAt"),
        "hash_sha256": _hash_policy_body(body),
    }


async def fetch_and_store_shop_policies(
    *,
    merchant_id: str,
    shop_domain: str,
    access_token: str,
    api_version: str = "2024-07",
) -> Dict[str, Dict[str, Any]]:
    """
    Fetch shop policies via Admin GraphQL and upsert a snapshot into pcs_shop_policies (append-only by hash).
    Returns normalized policies mapping {policy_type -> policy_dict}.
    """
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

