from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from services.shopify_graphql_client import ShopifyGraphQLError, shopify_admin_graphql
from services.shopify_promotions_sync import (
    SHOPIFY_API_VERSION,
    ShopifyPromotionsAuthError,
    ShopifyPromotionsConfigError,
    ShopifyPromotionsError,
    _first_graphql_error_summary,
    get_shopify_config_for_merchant,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _normalize_code_prefix(value: Optional[str]) -> str:
    raw = str(value or "").strip().upper()
    cleaned = "".join(ch if ("A" <= ch <= "Z") or ("0" <= ch <= "9") else "_" for ch in raw)
    cleaned = "_".join(part for part in cleaned.split("_") if part)
    return cleaned[:48] if cleaned else ""


def _basic_discount_mutation() -> str:
    return """
mutation CreateDiscountCode($basicCodeDiscount: DiscountCodeBasicInput!) {
  discountCodeBasicCreate(basicCodeDiscount: $basicCodeDiscount) {
    codeDiscountNode {
      id
      codeDiscount {
        ... on DiscountCodeBasic {
          title
          startsAt
          endsAt
          usageLimit
          appliesOncePerCustomer
          codes(first: 10) {
            nodes { code }
          }
          context {
            ... on DiscountCustomerSegments {
              segments { id }
            }
          }
          customerSelection {
            ... on DiscountCustomers {
              customers { id }
            }
          }
        }
      }
    }
    userErrors {
      field
      message
      code
    }
  }
}
""".strip()


def _segment_create_mutation() -> str:
    return """
mutation CreateSegment($name: String!, $query: String!) {
  segmentCreate(name: $name, query: $query) {
    segment {
      id
      name
      query
    }
    userErrors {
      field
      message
    }
  }
}
""".strip()


def _customer_lookup_query() -> str:
    return """
query CustomerLookup($query: String!) {
  customers(first: 1, query: $query) {
    nodes {
      id
      email
      numberOfOrders
    }
  }
}
""".strip()


async def _run_graphql(
    *,
    shop_domain: str,
    access_token: str,
    query: str,
    variables: Dict[str, Any],
    api_version: str,
) -> Dict[str, Any]:
    try:
        return await shopify_admin_graphql(
            shop_domain=shop_domain,
            access_token=access_token,
            query=query,
            variables=variables,
            api_version=api_version,
        )
    except ShopifyGraphQLError as exc:
        summary = _first_graphql_error_summary(exc)
        raise ShopifyPromotionsError(summary["message"] or "Shopify GraphQL error") from exc
    except RuntimeError as exc:
        msg = str(exc)
        if "HTTP 401" in msg or "HTTP 403" in msg:
            raise ShopifyPromotionsAuthError(msg) from exc
        raise ShopifyPromotionsError(msg) from exc


def _raise_if_user_errors(root: Dict[str, Any], key: str) -> None:
    payload = root.get(key) or {}
    user_errors = payload.get("userErrors") or []
    if not user_errors:
        return
    first = user_errors[0] if isinstance(user_errors, list) and user_errors else {}
    if not isinstance(first, dict):
        first = {}
    message = str(first.get("message") or "Shopify mutation failed").strip()
    raise ShopifyPromotionsError(message)


async def _create_segment(
    *,
    shop_domain: str,
    access_token: str,
    api_version: str,
    name: str,
    query: str,
) -> Dict[str, Any]:
    data = await _run_graphql(
        shop_domain=shop_domain,
        access_token=access_token,
        query=_segment_create_mutation(),
        variables={"name": name, "query": query},
        api_version=api_version,
    )
    _raise_if_user_errors(data, "segmentCreate")
    segment = ((data.get("segmentCreate") or {}).get("segment")) or {}
    if not isinstance(segment, dict) or not str(segment.get("id") or "").strip():
        raise ShopifyPromotionsError("Shopify segmentCreate returned no segment id")
    return segment


async def _create_basic_discount(
    *,
    shop_domain: str,
    access_token: str,
    api_version: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    data = await _run_graphql(
        shop_domain=shop_domain,
        access_token=access_token,
        query=_basic_discount_mutation(),
        variables={"basicCodeDiscount": payload},
        api_version=api_version,
    )
    _raise_if_user_errors(data, "discountCodeBasicCreate")
    node = ((data.get("discountCodeBasicCreate") or {}).get("codeDiscountNode")) or {}
    if not isinstance(node, dict) or not str(node.get("id") or "").strip():
        raise ShopifyPromotionsError("Shopify discountCodeBasicCreate returned no discount node id")
    discount = node.get("codeDiscount") if isinstance(node.get("codeDiscount"), dict) else {}
    codes = (((discount or {}).get("codes") or {}).get("nodes")) or []
    return {
        "discountNodeId": str(node.get("id") or "").strip(),
        "title": str((discount or {}).get("title") or payload.get("title") or "").strip(),
        "startsAt": (discount or {}).get("startsAt") or payload.get("startsAt"),
        "endsAt": (discount or {}).get("endsAt"),
        "usageLimit": (discount or {}).get("usageLimit"),
        "appliesOncePerCustomer": (discount or {}).get("appliesOncePerCustomer"),
        "codes": [str((row or {}).get("code") or "").strip() for row in codes if isinstance(row, dict)],
    }


async def _lookup_customer(
    *,
    shop_domain: str,
    access_token: str,
    api_version: str,
    customer_email: str,
) -> Optional[Dict[str, Any]]:
    email = str(customer_email or "").strip()
    if not email:
        return None
    safe_email = email.replace("\\", "\\\\").replace('"', '\\"')
    data = await _run_graphql(
        shop_domain=shop_domain,
        access_token=access_token,
        query=_customer_lookup_query(),
        variables={"query": f'email:"{safe_email}"'},
        api_version=api_version,
    )
    nodes = (((data.get("customers") or {}).get("nodes")) or [])
    if not isinstance(nodes, list) or not nodes:
        return None
    first = nodes[0] if isinstance(nodes[0], dict) else None
    if not isinstance(first, dict):
        return None
    return {
        "id": str(first.get("id") or "").strip() or None,
        "email": str(first.get("email") or "").strip() or None,
        "numberOfOrders": int(first.get("numberOfOrders") or 0),
    }


async def create_shopify_discount_validation_fixtures(
    *,
    merchant_id: str,
    customer_email: str,
    code_prefix: Optional[str] = None,
    upcoming_starts_in_minutes: int = 2,
    upcoming_duration_minutes: int = 20,
    api_version: Optional[str] = None,
) -> Dict[str, Any]:
    cfg = await get_shopify_config_for_merchant(merchant_id)
    if not cfg.is_configured:
        raise ShopifyPromotionsConfigError(f"Shopify configuration not found for merchant_id={merchant_id}")

    email = str(customer_email or "").strip().lower()
    if "@" not in email:
        raise ShopifyPromotionsConfigError("customer_email is required for segment/new-customer fixtures")
    email_domain = email.split("@", 1)[1].strip().lower()
    if not email_domain:
        raise ShopifyPromotionsConfigError("customer_email must include a valid domain")

    version = str(api_version or "2026-04").strip() or "2026-04"
    now = _utc_now()
    starts_at = now + timedelta(minutes=max(1, int(upcoming_starts_in_minutes)))
    ends_at = starts_at + timedelta(minutes=max(5, int(upcoming_duration_minutes)))
    run_key = _normalize_code_prefix(code_prefix) or now.strftime("PIVOTA_AUDIT_%Y%m%d_%H%M%S")

    customer = await _lookup_customer(
        shop_domain=cfg.shop_domain,
        access_token=cfg.access_token,
        api_version=version,
        customer_email=email,
    )

    segment_domain = await _create_segment(
        shop_domain=cfg.shop_domain,
        access_token=cfg.access_token,
        api_version=version,
        name=f"{run_key} CHYDAN Domain",
        query=f"customer_email_domain = '{email_domain}'",
    )
    segment_new_customer = await _create_segment(
        shop_domain=cfg.shop_domain,
        access_token=cfg.access_token,
        api_version=version,
        name=f"{run_key} New Customers",
        query="number_of_orders = 0",
    )

    fixed_amount = await _create_basic_discount(
        shop_domain=cfg.shop_domain,
        access_token=cfg.access_token,
        api_version=version,
        payload={
            "title": f"{run_key} Fixed Item Amount",
            "code": f"{run_key}_FIXITEM60",
            "startsAt": _iso(now),
            "endsAt": None,
            "customerSelection": {"all": True},
            "customerGets": {
                "value": {"discountAmount": {"amount": "0.60", "appliesOnEachItem": True}},
                "items": {"all": True},
            },
        },
    )
    usage_limit = await _create_basic_discount(
        shop_domain=cfg.shop_domain,
        access_token=cfg.access_token,
        api_version=version,
        payload={
            "title": f"{run_key} Usage Limit One",
            "code": f"{run_key}_LIMIT1",
            "startsAt": _iso(now),
            "endsAt": None,
            "customerSelection": {"all": True},
            "customerGets": {
                "value": {"percentage": 0.1},
                "items": {"all": True},
            },
            "usageLimit": 1,
            "appliesOncePerCustomer": True,
        },
    )
    upcoming = await _create_basic_discount(
        shop_domain=cfg.shop_domain,
        access_token=cfg.access_token,
        api_version=version,
        payload={
            "title": f"{run_key} Upcoming Window",
            "code": f"{run_key}_UPCOMING",
            "startsAt": _iso(starts_at),
            "endsAt": _iso(ends_at),
            "customerSelection": {"all": True},
            "customerGets": {
                "value": {"percentage": 0.12},
                "items": {"all": True},
            },
        },
    )
    segment_discount = await _create_basic_discount(
        shop_domain=cfg.shop_domain,
        access_token=cfg.access_token,
        api_version=version,
        payload={
            "title": f"{run_key} Segment Domain",
            "code": f"{run_key}_SEGMENT",
            "startsAt": _iso(now),
            "endsAt": None,
            "context": {"customerSegments": {"add": [segment_domain["id"]]}},
            "customerGets": {
                "value": {"discountAmount": {"amount": "0.50", "appliesOnEachItem": True}},
                "items": {"all": True},
            },
        },
    )
    new_customer_discount = await _create_basic_discount(
        shop_domain=cfg.shop_domain,
        access_token=cfg.access_token,
        api_version=version,
        payload={
            "title": f"{run_key} New Customer",
            "code": f"{run_key}_NEWCUST",
            "startsAt": _iso(now),
            "endsAt": None,
            "context": {"customerSegments": {"add": [segment_new_customer["id"]]}},
            "customerGets": {
                "value": {"discountAmount": {"amount": "0.75", "appliesOnEachItem": True}},
                "items": {"all": True},
            },
        },
    )

    return {
        "merchant_id": merchant_id,
        "shop_domain": cfg.shop_domain,
        "api_version": version,
        "run_key": run_key,
        "customer": customer,
        "segments": {
            "email_domain": segment_domain,
            "new_customer": segment_new_customer,
        },
        "discounts": {
            "fixed_amount_product": fixed_amount,
            "usage_limit": usage_limit,
            "upcoming": upcoming,
            "segment_customer": segment_discount,
            "new_customer": new_customer_discount,
        },
    }
