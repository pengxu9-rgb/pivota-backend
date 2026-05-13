from __future__ import annotations

"""Wix Stores order writeback adapter.

This adapter intentionally only consumes already-stored merchant tokens. Wix
App OAuth is not wired in this pass because a real Wix developer app is not
available in this environment. Future onboarding should exchange the Wix app
instance for a Bearer access token using:

  * WIX_APP_CLIENT_ID
  * WIX_APP_CLIENT_SECRET

and persist the merchant store credential blob as:

  {"access_token": "<wix bearer token>", "site_id": "<wix site id>"}

Until then, missing merchant tokens return ``wix_credentials_not_configured``
instead of raising into the paid-order pipeline.
"""

from decimal import Decimal, ROUND_HALF_UP
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Union

import httpx

logger = logging.getLogger(__name__)

WIX_STORES_CREATE_ORDER_URL = "https://www.wixapis.com/stores/v2/orders"
DEFAULT_WIX_PAYMENT_METHOD = "Pivota External Payment"


def _clean_str(value: Any) -> str:
    return str(value or "").strip()


def _coerce_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        raw = value.strip()
        if raw.startswith("{"):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    return dict(parsed)
            except Exception:
                return {}
    return {}


def _coerce_money(value: Any, default: str = "0.00") -> Decimal:
    try:
        if value is None or value == "":
            return Decimal(default)
        return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except Exception:
        return Decimal(default)


def _money_str(value: Any, default: str = "0.00") -> str:
    return str(_coerce_money(value, default=default))


def _name_parts(order: Dict[str, Any]) -> tuple[str, str]:
    shipping_address = _coerce_dict(order.get("shipping_address"))
    raw = _clean_str(shipping_address.get("name") or order.get("customer_name"))
    email = _clean_str(order.get("customer_email"))
    if not raw and "@" in email:
        raw = email.split("@", 1)[0]
    parts = [part for part in raw.split() if part.strip()]
    if not parts:
        return "Customer", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _normalize_subdivision(country: str, state: str) -> str:
    country = _clean_str(country).upper() or "US"
    state = _clean_str(state).upper()
    if country == "US" and len(state) == 2:
        return f"US-{state}"
    return state


def _build_wix_address(order: Dict[str, Any]) -> Dict[str, Any]:
    shipping_address = _coerce_dict(order.get("shipping_address"))
    first_name, last_name = _name_parts(order)
    country = _clean_str(shipping_address.get("country")).upper() or "US"
    return {
        "fullName": {
            "firstName": first_name,
            "lastName": last_name,
        },
        "country": country,
        "subdivision": _normalize_subdivision(country, shipping_address.get("state")),
        "city": _clean_str(shipping_address.get("city")),
        "zipCode": _clean_str(shipping_address.get("postal_code")),
        "addressLine": _clean_str(shipping_address.get("address_line1")),
        "addressLine2": _clean_str(shipping_address.get("address_line2")),
        "phone": _clean_str(shipping_address.get("phone")),
        "email": _clean_str(order.get("customer_email")),
    }


def _as_order_items(raw_items: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw_items, list):
        return []
    return [dict(item) for item in raw_items if isinstance(item, dict)]


def _line_item_options(item: Dict[str, Any]) -> Any:
    options = item.get("options") or item.get("selected_options") or item.get("variant_options")
    if isinstance(options, dict):
        return [
            {"option": _clean_str(key), "selection": _clean_str(value)}
            for key, value in options.items()
            if _clean_str(key) and _clean_str(value)
        ]
    if isinstance(options, list):
        return options
    return []


def _build_wix_line_item(item: Dict[str, Any]) -> Dict[str, Any]:
    quantity = int(item.get("quantity") or 1)
    if quantity <= 0:
        quantity = 1

    title = _clean_str(
        item.get("product_title")
        or item.get("title")
        or item.get("name")
        or item.get("sku")
        or "Product"
    )
    unit_price = _money_str(item.get("unit_price") or item.get("price"))
    line_item: Dict[str, Any] = {
        "name": title,
        "quantity": quantity,
        "price": unit_price,
        "lineItemType": "CUSTOM_AMOUNT_ITEM",
    }

    sku = _clean_str(item.get("sku"))
    if sku:
        line_item["sku"] = sku

    product_id = _clean_str(
        item.get("wix_product_id")
        or item.get("platform_product_id")
        or item.get("external_product_id")
        or item.get("product_id")
    )
    if product_id:
        line_item["productId"] = product_id
        line_item["lineItemType"] = "PHYSICAL"

    variant_id = _clean_str(
        item.get("wix_variant_id")
        or item.get("platform_variant_id")
        or item.get("external_variant_id")
        or item.get("variant_id")
    )
    if variant_id:
        line_item["variantId"] = variant_id
        line_item["options"] = _line_item_options(item)

    return line_item


def build_wix_order_payload(order_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Build the Wix Stores v2 order payload from a Pivota order row."""
    order = dict(order_dict or {})
    order_id = _clean_str(order.get("order_id"))
    email = _clean_str(order.get("customer_email"))
    first_name, last_name = _name_parts(order)
    address = _build_wix_address(order)
    payment_reference = _clean_str(order.get("payment_intent_id") or order_id)
    payment_status = _clean_str(order.get("payment_status")).lower()
    currency = _clean_str(order.get("currency")).upper() or "USD"
    line_items = [_build_wix_line_item(item) for item in _as_order_items(order.get("items"))]

    billing_info: Dict[str, Any] = {
        "address": address,
        "firstName": first_name,
        "lastName": last_name,
        "email": email,
        "phone": address.get("phone") or "",
        "paymentMethod": DEFAULT_WIX_PAYMENT_METHOD,
    }
    if payment_reference:
        billing_info["paymentProviderTransactionId"] = payment_reference

    return {
        "lineItems": line_items,
        "shippingInfo": {
            "shipmentDetails": {
                "address": address,
                "shippingCarrier": "Pivota",
                "shippingMethod": "Pivota External Shipping",
            }
        },
        "billingInfo": billing_info,
        "buyerInfo": {
            "email": email,
            "firstName": first_name,
            "lastName": last_name,
        },
        "paymentMethod": DEFAULT_WIX_PAYMENT_METHOD,
        "paymentStatus": "PAID" if payment_status == "paid" else "NOT_PAID",
        "fulfillmentStatus": "NOT_FULFILLED",
        "currency": currency,
        "totals": {
            "subtotal": _money_str(order.get("subtotal")),
            "shipping": _money_str(order.get("shipping_fee")),
            "tax": _money_str(order.get("tax")),
            "total": _money_str(order.get("total")),
        },
        "channelInfo": {
            "type": "OTHER_PLATFORM",
            "channelName": "Pivota",
            "externalOrderId": order_id,
        },
        "buyerNote": f"Pivota Order ID: {order_id}" if order_id else "Pivota order",
    }


def extract_wix_order_credentials(order_dict: Dict[str, Any]) -> Dict[str, str]:
    """Extract stored Wix credentials from a dispatcher order payload."""
    order = dict(order_dict or {})
    store = _coerce_dict(order.get("store") or order.get("store_info"))
    credential_sources = [
        _coerce_dict(order.get("wix_credentials")),
        _coerce_dict(order.get("api_credentials")),
        _coerce_dict(store.get("api_credentials")),
        _coerce_dict(order.get("api_key_raw")),
        _coerce_dict(order.get("api_key")),
        _coerce_dict(store.get("api_key_raw")),
        _coerce_dict(store.get("api_key")),
    ]

    access_token = _clean_str(
        order.get("wix_access_token")
        or order.get("access_token")
        or store.get("access_token")
        or store.get("api_key")
    )
    if access_token.startswith("{"):
        access_token = ""
    site_id = _clean_str(order.get("wix_site_id") or order.get("site_id") or store.get("site_id"))
    instance_id = _clean_str(order.get("wix_instance_id") or order.get("instance_id") or store.get("instance_id"))

    for source in credential_sources:
        if not access_token:
            access_token = _clean_str(
                source.get("access_token")
                or source.get("wix_access_token")
                or source.get("token")
                or source.get("api_key")
            )
        if not site_id:
            site_id = _clean_str(source.get("site_id") or source.get("wix_site_id"))
        if not instance_id:
            instance_id = _clean_str(source.get("instance_id") or source.get("wix_instance_id"))

    if not site_id:
        site_id = _clean_str(store.get("domain") or order.get("domain"))

    return {
        "access_token": access_token,
        "site_id": site_id,
        "instance_id": instance_id,
    }


def _authorization_header(access_token: str) -> str:
    token = _clean_str(access_token)
    if token.lower().startswith("bearer "):
        return token
    return f"Bearer {token}"


def _error_result(
    error: str,
    *,
    raw_response: Optional[Dict[str, Any]] = None,
    status_code: Optional[int] = None,
    retryable: bool = True,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "order_id": None,
        "status": "error",
        "error": error,
        "raw_response": raw_response or {},
        "retryable": retryable,
    }
    if status_code is not None:
        result["status_code"] = status_code
    return result


def _safe_response_json(response: httpx.Response) -> Dict[str, Any]:
    try:
        payload = response.json()
        if isinstance(payload, dict):
            return payload
        return {"response": payload}
    except Exception:
        return {"response_text": (response.text or "")[:1000]}


def _extract_wix_order_id(payload: Dict[str, Any]) -> str:
    for key in ("id", "_id", "orderId"):
        value = _clean_str(payload.get(key))
        if value:
            return value
    order = payload.get("order")
    if isinstance(order, dict):
        for key in ("id", "_id", "orderId"):
            value = _clean_str(order.get(key))
            if value:
                return value
    return ""


async def create_wix_order(
    merchant_id: str,
    order_dict: Dict[str, Any],
    *,
    timeout_s: float = 20.0,
) -> Dict[str, Any]:
    """Create an order in Wix Stores and return the platform result shape."""
    order = dict(order_dict or {})
    credentials = extract_wix_order_credentials(order)
    access_token = credentials.get("access_token") or ""
    site_id = credentials.get("site_id") or ""
    # Require BOTH access_token AND site_id. Wix's API needs the
    # site_id in the request URL/header — sending a probe with just
    # an access_token would 4xx upstream and waste a paid attempt.
    # Also: per the codex code review of PR #491, accepting any
    # non-empty access_token (without checking site_id) lets legacy
    # raw api_key blobs sneak through to upstream as bogus OAuth
    # bearer tokens. Strict-pair validation closes that gap.
    if not access_token or not site_id:
        missing = []
        if not access_token:
            missing.append("access_token")
        if not site_id:
            missing.append("site_id")
        logger.warning(
            "[Wix] Order writeback skipped; credentials not configured "
            "(missing=%s): merchant_id=%s order_id=%s",
            ",".join(missing),
            merchant_id,
            order.get("order_id"),
        )
        return _error_result(
            "wix_credentials_not_configured",
            raw_response={
                "message": (
                    f"Wix credentials incomplete; missing: "
                    f"{', '.join(missing)}. Both access_token AND "
                    f"site_id must be present to call the Wix API."
                ),
                "missing_fields": missing,
                "expected_store_credentials": {
                    "access_token": "stored Wix OAuth bearer token",
                    "site_id": "Wix site id",
                },
                "oauth_env": {
                    "WIX_APP_CLIENT_ID": bool(os.getenv("WIX_APP_CLIENT_ID")),
                    "WIX_APP_CLIENT_SECRET": bool(os.getenv("WIX_APP_CLIENT_SECRET")),
                },
            },
            retryable=False,
        )

    payload = build_wix_order_payload(order)
    if not payload.get("lineItems"):
        return _error_result(
            "wix_order_payload_invalid",
            raw_response={"message": "Wix order payload requires at least one line item"},
            retryable=False,
        )

    headers = {
        "Authorization": _authorization_header(access_token),
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    site_id = credentials.get("site_id") or ""
    if site_id:
        headers["wix-site-id"] = site_id

    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            response = await client.post(
                WIX_STORES_CREATE_ORDER_URL,
                headers=headers,
                json=payload,
            )
    except httpx.RequestError as exc:
        logger.warning(
            "[Wix] Order writeback network error: merchant_id=%s order_id=%s error=%s",
            merchant_id,
            order.get("order_id"),
            exc,
        )
        return _error_result(
            "wix_network_error",
            raw_response={"message": str(exc)},
            retryable=True,
        )
    except Exception as exc:
        logger.exception("[Wix] Unexpected order writeback error")
        return _error_result(
            "wix_order_writeback_failed",
            raw_response={"message": str(exc)},
            retryable=True,
        )

    response_payload = _safe_response_json(response)
    if response.status_code in (401, 403):
        return _error_result(
            "wix_auth_failed",
            raw_response=response_payload,
            status_code=response.status_code,
            retryable=False,
        )
    if response.status_code not in (200, 201):
        return _error_result(
            "wix_api_error",
            raw_response=response_payload,
            status_code=response.status_code,
            retryable=True,
        )

    wix_order_id = _extract_wix_order_id(response_payload)
    if not wix_order_id:
        return _error_result(
            "wix_response_missing_order_id",
            raw_response=response_payload,
            status_code=response.status_code,
            retryable=True,
        )

    logger.info(
        "[Wix] Order created: merchant_id=%s order_id=%s wix_order_id=%s",
        merchant_id,
        order.get("order_id"),
        wix_order_id,
    )
    return {
        "order_id": wix_order_id,
        "status": "created",
        "raw_response": response_payload,
    }


class WixAdapter:
    """Small compatibility wrapper around ``create_wix_order``."""

    def __init__(self, store_url: str = "", api_key: str = ""):
        self.store_url = _clean_str(store_url)
        self.api_key = _clean_str(api_key)

    async def create_order(self, order_data: Dict[str, Any]) -> Dict[str, Any]:
        payload = dict(order_data or {})
        payload["store"] = {
            "domain": self.store_url,
            "api_key": self.api_key,
        }
        return await create_wix_order(
            _clean_str(payload.get("merchant_id")) or "unknown",
            payload,
        )


class WixMockAdapter:
    """Legacy in-memory test adapter retained for older callers."""

    def __init__(self, store_url: str = "mock-wix-store.wix.com"):
        self.store_url = _clean_str(store_url)
        self.orders: Dict[str, Dict[str, Any]] = {}
        self.products = [
            {
                "id": "wix_prod_001",
                "title": "Wix Premium T-Shirt",
                "price": 29.99,
                "currency": "USD",
                "inventory": 50,
            },
            {
                "id": "wix_prod_002",
                "title": "Wix Designer Hoodie",
                "price": 79.99,
                "currency": "USD",
                "inventory": 25,
            },
            {
                "id": "wix_prod_003",
                "title": "Wix Business Card Holder",
                "price": 19.99,
                "currency": "USD",
                "inventory": 100,
            },
        ]

    def get_products(self) -> List[Dict[str, Any]]:
        return list(self.products)

    def create_order(
        self,
        customer_info: Dict[str, Any],
        line_items: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        try:
            order_id = f"wix_order_{int(time.time())}"
            total_amount = sum(
                _coerce_money(item.get("price")) * Decimal(int(item.get("quantity") or 1))
                for item in line_items
            )
            order = {
                "id": order_id,
                "customer": dict(customer_info or {}),
                "lineItems": list(line_items or []),
                "totalAmount": float(total_amount),
                "currency": "USD",
                "status": "pending",
                "store": self.store_url,
            }
            self.orders[order_id] = order
            return order
        except Exception:
            return None

    def update_order_payment(self, order_id: str, payment_result: Dict[str, Any]) -> bool:
        order = self.orders.get(order_id)
        if not order:
            return False
        success = bool((payment_result or {}).get("success"))
        order["status"] = "paid" if success else "cancelled"
        order["paymentInfo"] = dict(payment_result or {})
        return True

    def get_order(self, order_id: str) -> Optional[Dict[str, Any]]:
        return self.orders.get(order_id)


def get_wix_adapter(
    store_url: str,
    api_key: Optional[str] = None,
    use_mock: bool = True,
) -> Union[WixAdapter, WixMockAdapter]:
    if use_mock or not api_key:
        return WixMockAdapter(store_url)
    return WixAdapter(store_url=store_url, api_key=api_key or "")
