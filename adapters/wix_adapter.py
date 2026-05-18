from __future__ import annotations

"""Wix eCommerce order writeback adapter.

Production Wix catalog sync is API-key based. Order writeback follows the same
credential model and is gated by per-store readiness on ``merchant_stores``.
Legacy OAuth bearer support remains available only when the stored credential
blob explicitly declares ``auth_mode=oauth``.
"""

from decimal import Decimal, ROUND_HALF_UP
import logging
import time
from typing import Any, Dict, List, Optional, Union

import httpx

from services.wix_connection import (
    build_wix_api_key_headers,
    coerce_wix_credential_blob,
    extract_wix_site_id,
    normalize_wix_api_key,
)
from services.platform_order_writeback_readiness import (
    GLOBAL_ORDER_WRITEBACK_DISABLE_FLAG,
    is_store_order_writeback_allowed,
    store_order_writeback_context,
)

logger = logging.getLogger(__name__)

WIX_ECOM_CREATE_ORDER_URL = "https://www.wixapis.com/ecom/v1/orders"
WIX_STORES_APP_ID = "1380b703-ce81-ff05-f115-39571d94dfcd"
DEFAULT_WIX_PAYMENT_METHOD = "Pivota External Payment"


def _clean_str(value: Any) -> str:
    return str(value or "").strip()


def _coerce_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        raw = value.strip()
        if raw.startswith("{"):
            return coerce_wix_credential_blob(raw)
    return {}


def wix_order_writeback_readiness_context(order_dict: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    order = dict(order_dict or {}) if isinstance(order_dict, dict) else {}
    store = _coerce_dict(order.get("store") or order.get("store_info"))
    return store_order_writeback_context(
        store,
        order_id=_clean_str(order.get("order_id")),
        platform="wix",
    )


def is_wix_order_writeback_allowed(order_dict: Optional[Dict[str, Any]] = None) -> bool:
    order = dict(order_dict or {}) if isinstance(order_dict, dict) else {}
    store = _coerce_dict(order.get("store") or order.get("store_info"))
    return is_store_order_writeback_allowed(
        store,
        order_id=_clean_str(order.get("order_id")),
        platform="wix",
    )


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
        "postalCode": _clean_str(shipping_address.get("postal_code")),
        "zipCode": _clean_str(shipping_address.get("postal_code")),
        "addressLine1": _clean_str(shipping_address.get("address_line1")),
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
        "productName": {"original": title},
        "quantity": quantity,
        "price": {"amount": unit_price},
        "itemType": {"preset": "PHYSICAL"},
    }

    sku = _clean_str(item.get("sku"))
    if sku:
        line_item["sku"] = sku

    product_id = _clean_str(
        item.get("wix_product_id")
        or item.get("platform_product_id")
        or item.get("source_product_id")
        or item.get("external_product_id")
        or item.get("product_id")
    )
    variant_id = _clean_str(
        item.get("wix_variant_id")
        or item.get("platform_variant_id")
        or item.get("source_variant_id")
        or item.get("external_variant_id")
        or item.get("variant_id")
    )
    if product_id:
        catalog_options: Dict[str, Any] = {}
        if variant_id:
            catalog_options["variantId"] = variant_id
        line_item["catalogReference"] = {
            "appId": WIX_STORES_APP_ID,
            "catalogItemId": product_id,
            **({"options": catalog_options} if catalog_options else {}),
        }
    if variant_id:
        line_item["selectedOptions"] = _line_item_options(item)

    return line_item


def build_wix_order_payload(order_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Build the Wix eCommerce Create Order payload from a Pivota order row."""
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

    wix_order = {
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
        "priceSummary": {
            "subtotal": {"amount": _money_str(order.get("subtotal"))},
            "shipping": {"amount": _money_str(order.get("shipping_fee"))},
            "tax": {"amount": _money_str(order.get("tax"))},
            "total": {"amount": _money_str(order.get("total"))},
        },
        "channelInfo": {
            "type": "OTHER_PLATFORM",
            "channelName": "Pivota",
            "externalOrderId": order_id,
        },
        "buyerNote": f"Pivota Order ID: {order_id}" if order_id else "Pivota order",
    }
    return {"order": wix_order}


def _explicit_auth_mode(source: Dict[str, Any]) -> str:
    mode = _clean_str(
        source.get("auth_mode")
        or source.get("auth_type")
        or source.get("authentication_mode")
        or source.get("credential_type")
    ).lower()
    if mode in {"oauth", "bearer", "access_token"}:
        return "oauth"
    if mode in {"api_key", "apikey", "wix_api_key"}:
        return "api_key"
    token_type = _clean_str(source.get("token_type")).lower()
    if token_type == "bearer":
        return "oauth"
    return ""


def _safe_extract_site_id(domain: Any, api_key: Any = None) -> str:
    try:
        return extract_wix_site_id(domain, api_key)
    except Exception:
        return _clean_str(domain)


def extract_wix_order_credentials(order_dict: Dict[str, Any]) -> Dict[str, str]:
    """Extract Wix order credentials without treating raw API keys as OAuth."""
    order = dict(order_dict or {})
    store = _coerce_dict(order.get("store") or order.get("store_info"))
    credential_sources = [
        _coerce_dict(order.get("wix_credentials")),
        _coerce_dict(order.get("api_credentials")),
        _coerce_dict(store.get("api_credentials")),
        _coerce_dict(order.get("wix_api_key")),
        _coerce_dict(order.get("api_key_raw")),
        _coerce_dict(order.get("api_key")),
        _coerce_dict(store.get("wix_api_key")),
        _coerce_dict(store.get("api_key_raw")),
        _coerce_dict(store.get("api_key")),
    ]

    auth_mode = ""
    api_key = _clean_str(order.get("wix_api_key") or order.get("api_key") or store.get("api_key"))
    if api_key.startswith("{"):
        api_key = normalize_wix_api_key(api_key)
    access_token = ""
    site_id = _clean_str(order.get("wix_site_id") or order.get("site_id") or store.get("site_id"))
    instance_id = _clean_str(order.get("wix_instance_id") or order.get("instance_id") or store.get("instance_id"))

    for source in credential_sources:
        source_mode = _explicit_auth_mode(source)
        if source_mode and not auth_mode:
            auth_mode = source_mode
        if source_mode == "oauth" and not access_token:
            access_token = _clean_str(source.get("access_token") or source.get("wix_access_token"))
        if source_mode != "oauth" and not api_key:
            api_key = _clean_str(
                source.get("api_key")
                or source.get("wix_api_key")
                or source.get("token")
            )
        if not site_id:
            site_id = _clean_str(source.get("site_id") or source.get("wix_site_id"))
        if not instance_id:
            instance_id = _clean_str(source.get("instance_id") or source.get("wix_instance_id"))

    if not site_id:
        site_id = _safe_extract_site_id(store.get("domain") or order.get("domain"), store.get("api_key"))

    if not auth_mode:
        auth_mode = "api_key" if api_key else ("oauth" if access_token else "")

    return {
        "auth_mode": auth_mode,
        "api_key": api_key,
        "access_token": access_token,
        "site_id": site_id,
        "instance_id": instance_id,
    }


def _authorization_header(access_token: str) -> str:
    token = _clean_str(access_token)
    if token.lower().startswith("bearer "):
        return token
    return f"Bearer {token}"


def _build_order_headers(credentials: Dict[str, str]) -> Dict[str, str]:
    site_id = credentials.get("site_id") or ""
    if credentials.get("auth_mode") == "oauth":
        return {
            "Authorization": _authorization_header(credentials.get("access_token") or ""),
            "wix-site-id": site_id,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
    return build_wix_api_key_headers(credentials.get("api_key") or "", site_id)


def _credential_missing_fields(credentials: Dict[str, str]) -> List[str]:
    auth_mode = credentials.get("auth_mode") or "api_key"
    missing: List[str] = []
    if auth_mode == "oauth":
        if not credentials.get("access_token"):
            missing.append("access_token")
    else:
        if not credentials.get("api_key"):
            missing.append("api_key")
    if not credentials.get("site_id"):
        missing.append("site_id")
    return missing


def _payload_blockers(payload: Dict[str, Any]) -> List[str]:
    order = payload.get("order") if isinstance(payload, dict) else None
    if not isinstance(order, dict):
        return ["order"]
    line_items = order.get("lineItems")
    if not isinstance(line_items, list) or not line_items:
        return ["lineItems"]
    blockers: List[str] = []
    for index, item in enumerate(line_items):
        if not isinstance(item, dict):
            blockers.append(f"lineItems[{index}]")
            continue
        catalog_ref = item.get("catalogReference")
        if not isinstance(catalog_ref, dict) or not _clean_str(catalog_ref.get("catalogItemId")):
            blockers.append(f"lineItems[{index}].catalogReference.catalogItemId")
    return blockers


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
    """Create an order in Wix eCommerce and return the platform result shape."""
    order = dict(order_dict or {})
    readiness = wix_order_writeback_readiness_context(order)
    if not readiness.get("allowed"):
        return _error_result(
            "wix_order_writeback_not_ready",
            raw_response={
                "message": "Wix order writeback is not enabled for this active store.",
                "readiness": readiness,
                "required_store_fields": {
                    "order_writeback_status": "enabled or canary",
                    "order_writeback_canary_order_id": "matching order_id when status is canary",
                },
                "global_kill_switch": GLOBAL_ORDER_WRITEBACK_DISABLE_FLAG,
            },
            retryable=False,
        )

    credentials = extract_wix_order_credentials(order)
    missing = _credential_missing_fields(credentials)
    if missing:
        auth_mode = credentials.get("auth_mode") or "api_key"
        logger.warning(
            "[Wix] Order writeback skipped; credentials not configured "
            "(auth_mode=%s missing=%s): merchant_id=%s order_id=%s",
            auth_mode,
            ",".join(missing),
            merchant_id,
            order.get("order_id"),
        )
        return _error_result(
            "wix_order_writeback_not_ready",
            raw_response={
                "message": (
                    "Wix order writeback credentials are incomplete; "
                    f"missing: {', '.join(missing)}."
                ),
                "missing_fields": missing,
                "auth_mode": auth_mode,
                "expected_store_credentials": {
                    "api_key": "stored Wix API key",
                    "site_id": "Wix site id",
                },
            },
            retryable=False,
        )

    payload = build_wix_order_payload(order)
    blockers = _payload_blockers(payload)
    if blockers:
        return _error_result(
            "wix_order_payload_invalid",
            raw_response={
                "message": "Wix order payload is missing required catalog-backed line item fields",
                "blockers": blockers,
            },
            retryable=False,
        )

    headers = _build_order_headers(credentials)

    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            response = await client.post(
                WIX_ECOM_CREATE_ORDER_URL,
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
