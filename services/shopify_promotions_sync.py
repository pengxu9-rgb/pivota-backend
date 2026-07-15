"""
Shopify promotions → Pivota promotions sync.

MVP scope:
- Fetch Shopify discount nodes for a given merchant (using existing connector credentials).
- Normalize them into metadata-only PromotionCreate objects for quote/display policy.
- Upsert into the DB-backed promotions table via promotions_service.

This keeps Shopify as the source of truth for discount execution while letting
Pivota cache read-only promotion metadata for quote/display policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
import hashlib
import logging
import os

import httpx

from config.settings import settings
from db.connector_credentials import (
    get_latest_connector_credential_for_merchant,
    mark_credential_used,
)
from services.crypto_service import crypto_service
from services.merchant_store_service import get_primary_store
from services.promotions_service import (
    PromotionCreate,
    PromotionUpdate,
    create_promotion,
    get_promotion,
    update_promotion,
)
from services.shopify_access_token_service import resolve_shopify_admin_access_token
from services.shopify_graphql_client import ShopifyGraphQLError, shopify_admin_graphql

logger = logging.getLogger(__name__)

SHOPIFY_API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2025-10")
SHOPIFY_PRICE_RULE_PAGE_LIMIT = 250
SHOPIFY_DISCOUNT_NODE_PAGE_LIMIT = 50


class ShopifyPromotionsError(Exception):
    """Base exception for Shopify promotions sync errors."""


class ShopifyPromotionsConfigError(ShopifyPromotionsError):
    """Raised when Shopify credentials/config are missing."""


class ShopifyPromotionsAuthError(ShopifyPromotionsError):
    """Raised when Shopify rejects our credentials (401/403)."""


class ShopifyPromotionsRateLimitError(ShopifyPromotionsError):
    """Raised when Shopify rate limits our requests (429)."""


@dataclass
class ShopifyStoreConfig:
    shop_domain: str
    access_token: str

    @property
    def is_configured(self) -> bool:
        return bool(self.shop_domain and self.access_token)


async def _get_shopify_config_from_env() -> ShopifyStoreConfig:
    """
    Global Shopify configuration fallback from settings/env.

    This is primarily used for single-store setups or when per-merchant
    connector credentials are not yet configured.
    """
    shop_domain = (
        getattr(settings, "shopify_store_url", None)
        or os.getenv("SHOPIFY_STORE_URL")
        or os.getenv("SHOPIFY_SHOP_DOMAIN")
        or ""
    ).strip()
    access_token = (
        getattr(settings, "shopify_access_token", None)
        or os.getenv("SHOPIFY_ACCESS_TOKEN")
        or ""
    ).strip()
    return ShopifyStoreConfig(shop_domain=shop_domain, access_token=access_token)


async def get_shopify_config_for_merchant(merchant_id: str) -> ShopifyStoreConfig:
    """
    Resolve Shopify configuration for a given merchant.

    Order of precedence:
    1) Per-merchant encrypted connector_credentials (connector='shopify').
    2) Active merchant_stores Shopify custom app credentials.
    3) Global settings/env fallback.
    """
    # Try per-merchant encrypted credentials first
    try:
        credential = await get_latest_connector_credential_for_merchant(merchant_id, "shopify")
    except Exception as exc:  # pragma: no cover - defensive logging
        logger.error(
            "Failed to load Shopify connector credentials for merchant",
            extra={
                "merchant_id": merchant_id,
                "connector": "shopify",
                "error": str(exc),
            },
        )
        credential = None

    if credential:
        try:
            decrypted = crypto_service.decrypt_json_secret(credential["credentials_encrypted"])
            shop_domain = (decrypted.get("shop_domain") or "").strip()
            access_token = (decrypted.get("access_token") or "").strip()
            if shop_domain and access_token:
                await mark_credential_used(credential["id"])
                return ShopifyStoreConfig(shop_domain=shop_domain, access_token=access_token)
            logger.warning(
                "Shopify connector credentials missing required fields; falling back to env",
                extra={"merchant_id": merchant_id, "credential_id": credential["id"]},
            )
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.error(
                "Failed to decrypt Shopify connector credentials; falling back to env",
                extra={
                    "merchant_id": merchant_id,
                    "credential_id": credential.get("id"),
                    "error": str(exc),
                },
            )

    # Keep discount-node sync aligned with quote/order paths. Production
    # merchants commonly connect Shopify via merchant_stores custom app
    # credentials rather than connector_credentials.
    try:
        store = await get_primary_store(merchant_id)
    except Exception as exc:  # pragma: no cover - defensive logging
        logger.error(
            "Failed to load Shopify merchant store for discount sync",
            extra={
                "merchant_id": merchant_id,
                "connector": "shopify",
                "error": str(exc),
            },
        )
        store = None

    if store and str(store.get("platform") or "").strip().lower() == "shopify":
        shop_domain = (store.get("domain") or "").strip()
        access_token, token_meta = await resolve_shopify_admin_access_token(
            shop_domain=shop_domain,
            api_key_raw=store.get("api_key_raw") or store.get("api_key"),
            store_id=str(store.get("store_id") or "").strip() or None,
        )
        access_token = (access_token or "").strip()
        if shop_domain and access_token:
            logger.info(
                "Resolved Shopify discount sync config from merchant_stores",
                extra={
                    "merchant_id": merchant_id,
                    "store_id": store.get("store_id"),
                    "shop_domain": shop_domain,
                    "token_refreshed": bool((token_meta or {}).get("refreshed")),
                },
            )
            return ShopifyStoreConfig(shop_domain=shop_domain, access_token=access_token)
        logger.warning(
            "Shopify merchant store missing domain or Admin token; falling back to env",
            extra={
                "merchant_id": merchant_id,
                "store_id": store.get("store_id"),
                "has_shop_domain": bool(shop_domain),
                "has_access_token": bool(access_token),
            },
        )

    # Fallback to global configuration
    return await _get_shopify_config_from_env()


def _parse_shopify_next_page_info(link_header: Optional[str]) -> Optional[str]:
    """
    Parse Shopify Link header to extract `page_info` cursor for pagination.
    Example:
      <https://shop.myshopify.com/admin/api/2025-10/price_rules.json?limit=250&page_info=XYZ>; rel=\"next\"
    """
    if not link_header:
        return None

    parts = link_header.split(",")
    for part in parts:
        if 'rel=\"next\"' not in part and "rel='next'" not in part:
            continue
        start = part.find("<")
        end = part.find(">")
        if start == -1 or end == -1 or end <= start + 1:
            continue
        url = part[start + 1 : end]
        # Avoid importing urlparse to keep this helper small; a lightweight parse works.
        query_start = url.find("?")
        if query_start == -1:
            continue
        query_str = url[query_start + 1 :]
        for kv in query_str.split("&"):
            if not kv:
                continue
            key, _, value = kv.partition("=")
            if key == "page_info" and value:
                return value
    return None


async def _fetch_price_rules_page(
    cfg: ShopifyStoreConfig,
    limit: int,
    page_info: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """
    Fetch a single page of price rules from Shopify Admin API.
    Returns (price_rules, next_page_info_cursor).
    """
    if not cfg.is_configured:
        raise ShopifyPromotionsConfigError("Shopify store config is missing shop_domain or access_token")

    url = f"https://{cfg.shop_domain}/admin/api/{SHOPIFY_API_VERSION}/price_rules.json"
    params: Dict[str, Any] = {"limit": limit}
    if page_info:
        params["page_info"] = page_info

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                url,
                params=params,
                headers={"X-Shopify-Access-Token": cfg.access_token},
            )
    except httpx.RequestError as exc:
        raise ShopifyPromotionsError(f"Shopify price_rules request error: {exc}") from exc

    if resp.status_code == 429:
        raise ShopifyPromotionsRateLimitError("Shopify price_rules rate limit exceeded (status=429)")
    if resp.status_code in (401, 403):
        raise ShopifyPromotionsAuthError(f"Shopify price_rules auth failed (status={resp.status_code})")
    if resp.status_code != 200:
        raise ShopifyPromotionsError(f"Shopify price_rules fetch failed (status={resp.status_code})")

    data = resp.json()
    rules = data.get("price_rules", []) or []
    next_cursor = _parse_shopify_next_page_info(resp.headers.get("Link"))
    return rules, next_cursor


async def fetch_all_price_rules(cfg: ShopifyStoreConfig) -> List[Dict[str, Any]]:
    """Fetch all price rules for a store with basic pagination."""
    all_rules: List[Dict[str, Any]] = []
    page_info: Optional[str] = None

    while True:
        rules, next_cursor = await _fetch_price_rules_page(
            cfg,
            limit=SHOPIFY_PRICE_RULE_PAGE_LIMIT,
            page_info=page_info,
        )
        all_rules.extend(rules)
        if not next_cursor:
            break
        page_info = next_cursor

    return all_rules


def _safe_datetime(value: Optional[str], fallback: Optional[datetime] = None) -> Optional[datetime]:
    if not value:
        return fallback
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    except Exception:  # pragma: no cover - defensive
        return fallback


def _build_promotion_id(prefix: str, price_rule_id: Any, merchant_id: str) -> str:
    return f"{prefix}_{merchant_id}_{price_rule_id}"


def _build_discount_node_promotion_id(discount_node_id: str, merchant_id: str) -> str:
    digest = hashlib.sha256(f"{merchant_id}:{discount_node_id}".encode("utf-8")).hexdigest()[:24]
    return f"shopify_discount_{merchant_id}_{digest}"


def _env_enabled(name: str, default: str = "0") -> bool:
    return str(os.getenv(name, default) or "").strip().lower() in {"1", "true", "yes", "on"}


def _connection_nodes(connection: Any) -> List[Dict[str, Any]]:
    if not isinstance(connection, dict):
        return []
    nodes = connection.get("nodes")
    if isinstance(nodes, list):
        return [n for n in nodes if isinstance(n, dict)]
    edges = connection.get("edges")
    out: List[Dict[str, Any]] = []
    if isinstance(edges, list):
        for edge in edges:
            node = (edge or {}).get("node") if isinstance(edge, dict) else None
            if isinstance(node, dict):
                out.append(node)
    return out


def _extract_connection_ids(connection: Any) -> List[str]:
    ids: List[str] = []
    for node in _connection_nodes(connection):
        node_id = str(node.get("id") or "").strip()
        if node_id:
            ids.append(node_id)
    return list(dict.fromkeys(ids))


def _normalize_discount_items(items: Any) -> Dict[str, Any]:
    if not isinstance(items, dict):
        return {}
    normalized = dict(items)
    typename = str(normalized.get("__typename") or "").strip()
    if typename == "DiscountProducts":
        product_ids = _extract_connection_ids(normalized.get("products"))
        variant_ids = _extract_connection_ids(normalized.get("productVariants"))
        if product_ids:
            normalized["productIds"] = product_ids
        if variant_ids:
            normalized["variantIds"] = variant_ids
    elif typename == "DiscountCollections":
        collection_ids = _extract_connection_ids(normalized.get("collections"))
        if collection_ids:
            normalized["collectionIds"] = collection_ids
    return normalized


def _normalize_discount_customer_gets(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    normalized = dict(value)
    normalized["items"] = _normalize_discount_items(value.get("items"))
    return normalized


def _normalize_discount_customer_buys(value: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(value, dict):
        return None
    normalized = dict(value)
    normalized["items"] = _normalize_discount_items(value.get("items"))
    return normalized


def _extract_discount_codes(discount: Dict[str, Any]) -> List[str]:
    codes: List[str] = []
    for node in _connection_nodes(discount.get("codes")):
        code = str(node.get("code") or "").strip()
        if code:
            codes.append(code)
    code = str(discount.get("code") or "").strip()
    if code:
        codes.append(code)
    return list(dict.fromkeys(codes))


def _discount_type_from_typename(typename: str) -> str:
    value = typename.lower()
    if "bxgy" in value:
        return "bxgy"
    if "freeshipping" in value or "free_shipping" in value:
        return "free_shipping"
    return "basic"


def _discount_method_from_typename(typename: str) -> str:
    return "code" if typename.startswith("DiscountCode") else "automatic"


def _map_discount_node_to_promotion(
    node: Dict[str, Any],
    merchant_id: str,
    channel: str = "creator_agents",
) -> Optional[PromotionCreate]:
    discount = node.get("discount") if isinstance(node, dict) else None
    if not isinstance(discount, dict):
        return None
    node_id = str(node.get("id") or "").strip()
    typename = str(discount.get("__typename") or "").strip()
    if not node_id or not typename:
        return None

    discount_type = _discount_type_from_typename(typename)
    discount_method = _discount_method_from_typename(typename)
    promo_type = "FREE_SHIPPING" if discount_type == "free_shipping" else "MULTI_BUY_DISCOUNT"

    title = (
        str(discount.get("title") or "").strip()
        or str(discount.get("summary") or "").strip()
        or typename
    )
    start_at = _safe_datetime(discount.get("startsAt"), fallback=datetime.utcnow())
    end_at = _safe_datetime(discount.get("endsAt"))

    customer_gets = _normalize_discount_customer_gets(discount.get("customerGets"))
    customer_buys = _normalize_discount_customer_buys(discount.get("customerBuys"))
    minimum_requirement = (
        discount.get("minimumRequirement") if isinstance(discount.get("minimumRequirement"), dict) else None
    )
    combines_with = discount.get("combinesWith") if isinstance(discount.get("combinesWith"), dict) else {}
    context = (
        discount.get("customerSelection")
        or discount.get("context")
        or discount.get("appliesOnSubscription")
        or {}
    )

    scope: Dict[str, Any] = {"global": True}
    items = customer_gets.get("items") if isinstance(customer_gets, dict) else None
    if not isinstance(items, dict):
        items = customer_buys.get("items") if isinstance(customer_buys, dict) else None
    if isinstance(items, dict) and items.get("__typename"):
        scope = {"shopifyItems": items}

    cfg: Dict[str, Any] = {
        "source": "shopify_discount_node",
        "kind": promo_type,
        "shopifyDiscountNodeId": node_id,
        "discountTypename": typename,
        "discountMethod": discount_method,
        "discountType": discount_type,
        "status": discount.get("status"),
        "summary": discount.get("summary"),
        "discountClasses": discount.get("discountClasses") or [],
        "combinesWith": combines_with,
        "context": context,
        "customerGets": customer_gets,
        "customerBuys": customer_buys,
        "minimumRequirement": minimum_requirement,
        "usageLimit": discount.get("usageLimit"),
        "appliesOncePerCustomer": discount.get("appliesOncePerCustomer"),
        "asyncUsageCount": discount.get("asyncUsageCount"),
        "codes": _extract_discount_codes(discount),
    }
    if promo_type == "FREE_SHIPPING":
        cfg["freeShipping"] = True

    return PromotionCreate(
        id=_build_discount_node_promotion_id(node_id, merchant_id),
        merchantId=merchant_id,
        name=title,
        type=promo_type,
        description=str(discount.get("summary") or "").strip(),
        startAt=start_at,
        endAt=end_at,
        channels=[channel],
        scope=scope,
        config=cfg,
        exposeToCreators=True,
        allowedCreatorIds=None,
    )


async def fetch_all_discount_nodes(cfg: ShopifyStoreConfig) -> List[Dict[str, Any]]:
    if not cfg.is_configured:
        raise ShopifyPromotionsConfigError("Shopify store config is missing shop_domain or access_token")

    query = """
query($first: Int!, $after: String) {
  discountNodes(first: $first, after: $after) {
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        id
        discount {
          __typename
          ... on DiscountCodeBasic {
            title
            status
            summary
            startsAt
            endsAt
            discountClasses
            combinesWith { orderDiscounts productDiscounts shippingDiscounts }
            customerSelection { __typename }
            customerGets {
              __typename
              items {
                __typename
                ... on AllDiscountItems { allItems }
                ... on DiscountCollections { collections(first: 50) { nodes { id } } }
                ... on DiscountProducts {
                  products(first: 50) { nodes { id } }
                  productVariants(first: 50) { nodes { id } }
                }
              }
              value {
                __typename
                ... on DiscountAmount {
                  amount { amount currencyCode }
                  appliesOnEachItem
                }
                ... on DiscountPercentage { percentage }
                ... on DiscountOnQuantity {
                  quantity { quantity }
                  effect {
                    __typename
                    ... on DiscountAmount {
                      amount { amount currencyCode }
                      appliesOnEachItem
                    }
                    ... on DiscountPercentage { percentage }
                  }
                }
              }
            }
            minimumRequirement {
              __typename
              ... on DiscountMinimumSubtotal { greaterThanOrEqualToSubtotal { amount currencyCode } }
              ... on DiscountMinimumQuantity { greaterThanOrEqualToQuantity }
            }
            usageLimit
            appliesOncePerCustomer
            asyncUsageCount
            codes(first: 20) { nodes { code } }
          }
          ... on DiscountAutomaticBasic {
            title
            status
            summary
            startsAt
            endsAt
            discountClasses
            combinesWith { orderDiscounts productDiscounts shippingDiscounts }
            customerGets {
              __typename
              items {
                __typename
                ... on AllDiscountItems { allItems }
                ... on DiscountCollections { collections(first: 50) { nodes { id } } }
                ... on DiscountProducts {
                  products(first: 50) { nodes { id } }
                  productVariants(first: 50) { nodes { id } }
                }
              }
              value {
                __typename
                ... on DiscountAmount {
                  amount { amount currencyCode }
                  appliesOnEachItem
                }
                ... on DiscountPercentage { percentage }
                ... on DiscountOnQuantity {
                  quantity { quantity }
                  effect {
                    __typename
                    ... on DiscountAmount {
                      amount { amount currencyCode }
                      appliesOnEachItem
                    }
                    ... on DiscountPercentage { percentage }
                  }
                }
              }
            }
            minimumRequirement {
              __typename
              ... on DiscountMinimumSubtotal { greaterThanOrEqualToSubtotal { amount currencyCode } }
              ... on DiscountMinimumQuantity { greaterThanOrEqualToQuantity }
            }
            asyncUsageCount
          }
          ... on DiscountCodeBxgy {
            title
            status
            summary
            startsAt
            endsAt
            discountClasses
            combinesWith { orderDiscounts productDiscounts shippingDiscounts }
            customerSelection { __typename }
            customerBuys {
              __typename
              items {
                __typename
                ... on AllDiscountItems { allItems }
                ... on DiscountCollections { collections(first: 50) { nodes { id } } }
                ... on DiscountProducts {
                  products(first: 50) { nodes { id } }
                  productVariants(first: 50) { nodes { id } }
                }
              }
              value {
                __typename
                ... on DiscountPurchaseAmount { amount }
                ... on DiscountQuantity { quantity }
              }
            }
            customerGets {
              __typename
              items {
                __typename
                ... on AllDiscountItems { allItems }
                ... on DiscountCollections { collections(first: 50) { nodes { id } } }
                ... on DiscountProducts {
                  products(first: 50) { nodes { id } }
                  productVariants(first: 50) { nodes { id } }
                }
              }
              value {
                __typename
                ... on DiscountAmount {
                  amount { amount currencyCode }
                  appliesOnEachItem
                }
                ... on DiscountPercentage { percentage }
                ... on DiscountOnQuantity {
                  quantity { quantity }
                  effect {
                    __typename
                    ... on DiscountAmount {
                      amount { amount currencyCode }
                      appliesOnEachItem
                    }
                    ... on DiscountPercentage { percentage }
                  }
                }
              }
            }
            usageLimit
            appliesOncePerCustomer
            asyncUsageCount
            codes(first: 20) { nodes { code } }
          }
          ... on DiscountAutomaticBxgy {
            title
            status
            summary
            startsAt
            endsAt
            discountClasses
            combinesWith { orderDiscounts productDiscounts shippingDiscounts }
            customerBuys {
              __typename
              items {
                __typename
                ... on AllDiscountItems { allItems }
                ... on DiscountCollections { collections(first: 50) { nodes { id } } }
                ... on DiscountProducts {
                  products(first: 50) { nodes { id } }
                  productVariants(first: 50) { nodes { id } }
                }
              }
              value {
                __typename
                ... on DiscountPurchaseAmount { amount }
                ... on DiscountQuantity { quantity }
              }
            }
            customerGets {
              __typename
              items {
                __typename
                ... on AllDiscountItems { allItems }
                ... on DiscountCollections { collections(first: 50) { nodes { id } } }
                ... on DiscountProducts {
                  products(first: 50) { nodes { id } }
                  productVariants(first: 50) { nodes { id } }
                }
              }
              value {
                __typename
                ... on DiscountAmount {
                  amount { amount currencyCode }
                  appliesOnEachItem
                }
                ... on DiscountPercentage { percentage }
                ... on DiscountOnQuantity {
                  quantity { quantity }
                  effect {
                    __typename
                    ... on DiscountAmount {
                      amount { amount currencyCode }
                      appliesOnEachItem
                    }
                    ... on DiscountPercentage { percentage }
                  }
                }
              }
            }
            asyncUsageCount
          }
          ... on DiscountCodeFreeShipping {
            title
            status
            summary
            startsAt
            endsAt
            discountClasses
            combinesWith { orderDiscounts productDiscounts shippingDiscounts }
            customerSelection { __typename }
            minimumRequirement {
              __typename
              ... on DiscountMinimumSubtotal { greaterThanOrEqualToSubtotal { amount currencyCode } }
              ... on DiscountMinimumQuantity { greaterThanOrEqualToQuantity }
            }
            usageLimit
            appliesOncePerCustomer
            asyncUsageCount
            codes(first: 20) { nodes { code } }
          }
          ... on DiscountAutomaticFreeShipping {
            title
            status
            summary
            startsAt
            endsAt
            discountClasses
            combinesWith { orderDiscounts productDiscounts shippingDiscounts }
            minimumRequirement {
              __typename
              ... on DiscountMinimumSubtotal { greaterThanOrEqualToSubtotal { amount currencyCode } }
              ... on DiscountMinimumQuantity { greaterThanOrEqualToQuantity }
            }
            asyncUsageCount
          }
        }
      }
    }
  }
}
"""
    nodes: List[Dict[str, Any]] = []
    after: Optional[str] = None
    while True:
        data = await shopify_admin_graphql(
            shop_domain=cfg.shop_domain,
            access_token=cfg.access_token,
            query=query,
            variables={"first": SHOPIFY_DISCOUNT_NODE_PAGE_LIMIT, "after": after},
            api_version=SHOPIFY_API_VERSION,
            timeout_s=20.0,
        )
        root = data.get("discountNodes") if isinstance(data, dict) else None
        if not isinstance(root, dict):
            break
        for edge in root.get("edges") or []:
            node = (edge or {}).get("node") if isinstance(edge, dict) else None
            if isinstance(node, dict):
                nodes.append(node)
        page_info = root.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        after = page_info.get("endCursor")
        if not after:
            break
    return nodes


async def _fetch_access_scopes_for_config(
    cfg: ShopifyStoreConfig,
    *,
    timeout_s: float = 12.0,
) -> List[str]:
    if not cfg.is_configured:
        raise ShopifyPromotionsConfigError("Shopify store config is missing shop_domain or access_token")

    url = f"https://{cfg.shop_domain}/admin/oauth/access_scopes.json"
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        resp = await client.get(url, headers={"X-Shopify-Access-Token": cfg.access_token})
        if resp.status_code >= 400:
            raise ShopifyPromotionsAuthError(f"Shopify access_scopes fetch failed (status={resp.status_code})")
        data = resp.json() or {}

    scopes: List[str] = []
    for item in data.get("access_scopes") or []:
        if isinstance(item, dict) and item.get("handle"):
            scopes.append(str(item["handle"]))
    return sorted(set(scopes))


def _first_graphql_error_summary(exc: ShopifyGraphQLError) -> Dict[str, Any]:
    first = exc.errors[0] if exc.errors else {}
    if not isinstance(first, dict):
        first = {}
    extensions = first.get("extensions") if isinstance(first.get("extensions"), dict) else {}
    return {
        "message": str(first.get("message") or exc.message or "").strip(),
        "code": str(extensions.get("code") or "").strip() or None,
        "requestId": exc.request_id,
    }


async def probe_shopify_discount_nodes_access_for_merchant(
    merchant_id: str,
    *,
    api_version: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Read-only probe used by Ops/preflight validation.

    It checks the installed merchant token's scopes and runs a one-node
    `discountNodes` query. It never creates or updates Shopify discounts and
    does not upsert Pivota promotions.
    """
    cfg = await get_shopify_config_for_merchant(merchant_id)
    if not cfg.is_configured:
        raise ShopifyPromotionsConfigError(
            f"Shopify configuration not found for merchant_id={merchant_id}"
        )

    version = api_version or SHOPIFY_API_VERSION
    report: Dict[str, Any] = {
        "merchantId": merchant_id,
        "shopDomain": cfg.shop_domain,
        "apiVersion": version,
        "hasReadDiscountsScope": False,
        "discountNodesAccess": "unknown",
        "sampleNodeCount": 0,
        "errors": [],
    }

    try:
        scopes = await _fetch_access_scopes_for_config(cfg)
        report["installedScopes"] = scopes
        report["hasReadDiscountsScope"] = "read_discounts" in scopes
    except ShopifyPromotionsError as exc:
        report["discountNodesAccess"] = "blocked"
        report["errors"].append({"source": "access_scopes", "message": str(exc)})
        return report

    query = """
query DiscountNodeAccessProbe {
  discountNodes(first: 1) {
    nodes {
      id
      discount {
        __typename
        ... on DiscountCodeBasic { title status }
        ... on DiscountAutomaticBasic { title status }
        ... on DiscountCodeBxgy { title status }
        ... on DiscountAutomaticBxgy { title status }
        ... on DiscountCodeFreeShipping { title status }
        ... on DiscountAutomaticFreeShipping { title status }
      }
    }
  }
}
"""
    try:
        data = await shopify_admin_graphql(
            shop_domain=cfg.shop_domain,
            access_token=cfg.access_token,
            query=query,
            api_version=version,
            timeout_s=20.0,
        )
        root = data.get("discountNodes") if isinstance(data, dict) else None
        nodes = root.get("nodes") if isinstance(root, dict) and isinstance(root.get("nodes"), list) else []
        report["discountNodesAccess"] = "ok"
        report["sampleNodeCount"] = len(nodes)
        report["sampleTypenames"] = [
            str(((node.get("discount") or {}).get("__typename") or "")).strip()
            for node in nodes
            if isinstance(node, dict)
        ]
        return report
    except ShopifyGraphQLError as exc:
        summary = _first_graphql_error_summary(exc)
        report["discountNodesAccess"] = "blocked"
        report["errors"].append({"source": "discountNodes", **summary})
        return report
    except RuntimeError as exc:
        report["discountNodesAccess"] = "blocked"
        report["errors"].append({"source": "discountNodes", "message": str(exc)})
        return report


def _map_price_rule_to_promotion(
    rule: Dict[str, Any],
    merchant_id: str,
    channel: str = "creator_agents",
) -> Optional[PromotionCreate]:
    """
    Map a Shopify price rule into a PromotionCreate.
    Focuses on the common cases:
      - Percentage / fixed amount discounts on line items or orders.
      - Free shipping discounts on shipping lines.
    """
    rule_id = rule.get("id")
    if not rule_id:
        return None

    title = (rule.get("title") or "").strip() or "Shopify promotion"
    target_type = rule.get("target_type") or "line_item"
    value_type = (rule.get("value_type") or "").lower()  # 'percentage' | 'fixed_amount'
    value_raw = rule.get("value")
    try:
        value_num = float(value_raw) if value_raw is not None else None
    except (TypeError, ValueError):
        value_num = None

    prerequisite_qty = None
    qty_range = rule.get("prerequisite_quantity_range") or {}
    if isinstance(qty_range, dict):
        prerequisite_qty = qty_range.get("greater_than_or_equal_to") or qty_range.get("greater_than")

    prerequisite_subtotal = None
    subtotal_range = rule.get("prerequisite_subtotal_range") or {}
    if isinstance(subtotal_range, dict):
        prerequisite_subtotal = subtotal_range.get("greater_than_or_equal_to") or subtotal_range.get("greater_than")

    scope: Dict[str, Any] = {}
    target_selection = (rule.get("target_selection") or "all").lower()
    if target_selection == "all":
        scope["global"] = True
    else:
        product_ids = rule.get("entitled_product_ids") or []
        collection_ids = rule.get("entitled_collection_ids") or []
        variant_ids = rule.get("entitled_variant_ids") or []
        if product_ids:
            scope["productIds"] = [str(pid) for pid in product_ids]
        if collection_ids:
            scope["collectionIds"] = [str(cid) for cid in collection_ids]
        if variant_ids:
            scope["variantIds"] = [str(vid) for vid in variant_ids]

    start_at = _safe_datetime(rule.get("starts_at"), fallback=datetime.utcnow())
    end_at = _safe_datetime(rule.get("ends_at"))

    # Determine promotion type and config
    cfg: Dict[str, Any] = {
        "source": "shopify_price_rule",
        "priceRuleId": str(rule_id),
    }

    if target_type == "shipping_line":
        promo_type = "FREE_SHIPPING"
        cfg["kind"] = "FREE_SHIPPING"
        cfg["freeShipping"] = True
        if prerequisite_subtotal is not None:
            cfg["minSubtotal"] = prerequisite_subtotal
    else:
        # Treat all non-shipping rules as multi-buy / percent-off style.
        promo_type = "MULTI_BUY_DISCOUNT"
        cfg["kind"] = "MULTI_BUY_DISCOUNT"
        if prerequisite_qty is not None:
            try:
                cfg["thresholdQuantity"] = int(prerequisite_qty)
            except (TypeError, ValueError):
                pass

        if value_type == "percentage" and value_num is not None:
            # Shopify stores percentage discounts as negative numbers (e.g. -20.0).
            cfg["discountPercent"] = abs(int(round(value_num)))
        elif value_type == "fixed_amount" and value_num is not None:
            # Fixed amount discounts don't map cleanly to a percentage without basket context.
            # We still surface a label, but leave discountPercent unset.
            cfg["discountAmount"] = abs(value_num)

    promo_id = _build_promotion_id("shopify_rule", rule_id, merchant_id)

    return PromotionCreate(
        id=promo_id,
        merchantId=merchant_id,
        name=title,
        type=promo_type,
        description=(rule.get("description") or "").strip() if rule.get("description") else "",
        startAt=start_at,
        endAt=end_at,
        channels=[channel],
        scope=scope,
        config=cfg,
        exposeToCreators=True,
        allowedCreatorIds=None,
    )


async def sync_shopify_promotions_for_merchant(
    merchant_id: str,
    channel: str = "creator_agents",
) -> Dict[str, Any]:
    """
    Fetch Shopify price rules for a merchant and upsert them into the promotions table.

    Returns a summary dict: { "merchantId", "rulesFetched", "created", "updated", "skipped" }.
    """
    cfg = await get_shopify_config_for_merchant(merchant_id)
    if not cfg.is_configured:
        raise ShopifyPromotionsConfigError(
            f"Shopify configuration not found for merchant_id={merchant_id}"
        )

    use_graphql = _env_enabled("SHOPIFY_DISCOUNT_GRAPHQL_SYNC", "1")
    use_legacy_price_rules = _env_enabled("SHOPIFY_DISCOUNT_LEGACY_PRICE_RULE_SYNC", "0")
    created = 0
    updated = 0
    skipped = 0
    fetched = 0
    sync_source = "shopify_discount_nodes" if use_graphql else "shopify_price_rules"

    if not use_graphql and not use_legacy_price_rules:
        return {
            "merchantId": merchant_id,
            "syncSource": "disabled",
            "rulesFetched": 0,
            "discountNodesFetched": 0,
            "created": 0,
            "updated": 0,
            "skipped": 0,
        }

    async def _upsert(promo: PromotionCreate) -> None:
        nonlocal created, updated
        existing = await get_promotion(promo.id)
        if existing:
            update_payload = PromotionUpdate(
                name=promo.name,
                description=promo.description,
                startAt=promo.startAt,
                endAt=promo.endAt,
                channels=promo.channels,
                scope=promo.scope,
                config=promo.config,
                exposeToCreators=promo.exposeToCreators,
                allowedCreatorIds=promo.allowedCreatorIds,
            )
            await update_promotion(promo.id, update_payload)
            updated += 1
        else:
            await create_promotion(promo)
            created += 1

    if use_graphql:
        try:
            nodes = await fetch_all_discount_nodes(cfg)
            fetched = len(nodes)
            logger.info(
                "Fetched Shopify discount nodes",
                extra={"merchant_id": merchant_id, "count": fetched},
            )
            for node in nodes:
                promo = _map_discount_node_to_promotion(node, merchant_id=merchant_id, channel=channel)
                if not promo:
                    skipped += 1
                    continue
                await _upsert(promo)
            return {
                "merchantId": merchant_id,
                "syncSource": sync_source,
                "rulesFetched": fetched,
                "discountNodesFetched": fetched,
                "created": created,
                "updated": updated,
                "skipped": skipped,
            }
        except (ShopifyGraphQLError, RuntimeError) as exc:
            if not use_legacy_price_rules:
                raise ShopifyPromotionsError(f"Shopify discountNodes sync failed: {exc}") from exc
            logger.warning(
                "Shopify discountNodes sync failed; falling back to legacy price rules",
                extra={"merchant_id": merchant_id, "error": str(exc)},
            )

    if not use_legacy_price_rules and use_graphql:
        return {
            "merchantId": merchant_id,
            "syncSource": sync_source,
            "rulesFetched": fetched,
            "discountNodesFetched": fetched,
            "created": created,
            "updated": updated,
            "skipped": skipped,
        }

    rules = await fetch_all_price_rules(cfg)
    fetched = len(rules)
    sync_source = "shopify_price_rules"
    logger.info(
        "Fetched Shopify price rules",
        extra={"merchant_id": merchant_id, "count": len(rules)},
    )

    for rule in rules:
        promo = _map_price_rule_to_promotion(rule, merchant_id=merchant_id, channel=channel)
        if not promo:
            skipped += 1
            continue

        await _upsert(promo)

    return {
        "merchantId": merchant_id,
        "syncSource": sync_source,
        "rulesFetched": fetched,
        "created": created,
        "updated": updated,
        "skipped": skipped,
    }
