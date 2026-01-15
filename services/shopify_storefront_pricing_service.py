from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

import hashlib
import httpx
import json
import os
import re
import time

from db.database import database
from services.merchant_store_service import get_primary_store
from services.shopify_pricing_service import ShopifyPricingError, ShopifyPricingResult
from utils.logger import logger


def _d(v: Any) -> Decimal:
    try:
        return Decimal(str(v or "0")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except Exception:
        return Decimal("0.00")


def _gid(kind: str, numeric_id: str) -> str:
    return f"gid://shopify/{kind}/{numeric_id}"


def _extract_storefront_token(store: Dict[str, Any]) -> Optional[str]:
    creds = store.get("api_credentials") if isinstance(store.get("api_credentials"), dict) else {}
    candidates = [
        creds.get("storefront_access_token"),
        creds.get("storefront_token"),
        creds.get("storefrontAccessToken"),
        creds.get("storefrontAccessTokenPublic"),
    ]
    for c in candidates:
        if isinstance(c, str) and c.strip():
            return c.strip()

    # Per-store fallback (if upstream populated a dedicated field).
    direct = store.get("storefront_access_token") if isinstance(store.get("storefront_access_token"), str) else None
    if isinstance(direct, str) and direct.strip():
        return direct.strip()

    # Global fallback is dangerous in multi-merchant deployments; keep it explicitly opt-in.
    allow_global = (os.getenv("SHOPIFY_STOREFRONT_ALLOW_GLOBAL_TOKEN", "") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if not allow_global:
        return None

    env_token = (os.getenv("SHOPIFY_STOREFRONT_ACCESS_TOKEN", "") or "").strip()
    if not env_token:
        return None

    env_domain = (os.getenv("SHOPIFY_STOREFRONT_ACCESS_TOKEN_DOMAIN", "") or "").strip().lower()
    store_domain = (store.get("domain") or "").strip().lower()
    if env_domain and store_domain and env_domain != store_domain:
        return None

    return env_token


@dataclass(frozen=True)
class StorefrontCartResult:
    cart_id: str
    checkout_url: Optional[str]
    currency: str
    subtotal: Decimal
    total: Decimal
    tax: Decimal
    delivery_options: Optional[List[Dict[str, Any]]]
    selected_delivery_option: Optional[Dict[str, Any]]


class ShopifyStorefrontPricingService:
    """
    Pricing oracle backed by Shopify Storefront Cart API.

    Goal: provide a quote-first "locked pricing" snapshot without relying on deprecated
    Admin REST Checkout API (which requires write_checkouts).
    """

    def __init__(self, api_version: str = "2024-07", timeout_seconds: float = 20.0):
        self.api_version = api_version
        self.timeout_seconds = timeout_seconds
        self._rotate_cooldown_s = int(os.getenv("SHOPIFY_STOREFRONT_ROTATE_COOLDOWN_SECONDS", "3600") or "3600")


_storefront_rotate_attempted_at: Dict[str, float] = {}


def _is_missing_product_listings_scope(err: ShopifyPricingError) -> bool:
    try:
        details = getattr(err, "details", {}) or {}
        errors = details.get("errors") or []
        if not isinstance(errors, list) or not errors:
            return False
        first = errors[0] if isinstance(errors[0], dict) else {}
        msg = (first.get("message") or "") if isinstance(first, dict) else ""
        code = (first.get("code") or "") if isinstance(first, dict) else ""
        return (
            str(code).upper() == "ACCESS_DENIED"
            and "unauthenticated_read_product_listings" in str(msg)
            and "productvariant" in str(msg).lower()
        )
    except Exception:
        return False


def _rotate_allowed(*, merchant_id: str, cooldown_s: int) -> bool:
    now = time.time()
    last = _storefront_rotate_attempted_at.get(merchant_id, 0.0)
    if (now - last) < float(max(cooldown_s, 0)):
        return False
    _storefront_rotate_attempted_at[merchant_id] = now
    return True


async def _rotate_storefront_token_best_effort(
    *,
    merchant_id: str,
    store_id: Optional[str],
    shop_domain: str,
    admin_access_token: str,
) -> Optional[str]:
    """
    Create a new Storefront token and persist it for the merchant store.
    This is best-effort and will not raise.
    """
    try:
        url = f"https://{shop_domain}/admin/api/2024-07/storefront_access_tokens.json"
        headers = {"X-Shopify-Access-Token": admin_access_token, "Content-Type": "application/json"}
        payload = {"storefront_access_token": {"title": "Pivota Pricing"}}
        async with httpx.AsyncClient(timeout=12.0) as client:
            resp = await client.post(url, headers=headers, json=payload)
        if resp.status_code not in (200, 201):
            return None

        data = resp.json() or {}
        storefront = data.get("storefront_access_token") if isinstance(data, dict) else None
        new_token = storefront.get("access_token") if isinstance(storefront, dict) else None
        new_token = new_token.strip() if isinstance(new_token, str) and new_token.strip() else None
        if not new_token:
            return None

        # Persist: merge into api_key JSON for this store row.
        if store_id:
            token_json = json.dumps({"access_token": admin_access_token, "storefront_access_token": new_token})
            await database.execute(
                """
                UPDATE merchant_stores
                SET api_key = :api_key, connected_at = CURRENT_TIMESTAMP
                WHERE store_id = :store_id AND merchant_id = :merchant_id
                """,
                {"api_key": token_json, "store_id": store_id, "merchant_id": merchant_id},
            )
        return new_token
    except Exception:
        return None

    def _use_buyer_country_for_pricing(self) -> bool:
        # When enabled, we set buyerIdentity.countryCode and @inContext(country: ...)
        # so Shopify returns the buyer's presentment currency for that country.
        # Disable this to always price in shop currency (useful when a merchant does
        # not want multi-currency pricing).
        raw = (
            os.getenv("SHOPIFY_STOREFRONT_USE_BUYER_COUNTRY_FOR_PRICING")
            or os.getenv("SHOPIFY_STOREFRONT_USE_BUYER_COUNTRY_FOR_CURRENCY")
            or "true"
        )
        v = str(raw).strip().lower()
        return v not in {"0", "false", "no", "off"}

    async def preview_cart_quote(
        self,
        *,
        merchant_id: str,
        items: List[Dict[str, Any]],
        discount_codes: List[str],
        customer_email: Optional[str],
        shipping_address: Optional[Dict[str, Any]],
        selected_delivery_option: Optional[Dict[str, Any]],
    ) -> ShopifyPricingResult:
        debug_id = hashlib.sha256(
            f"storefront:{merchant_id}:{datetime.now(timezone.utc).isoformat()}".encode("utf-8")
        ).hexdigest()[:16]

        store = await get_primary_store(merchant_id)
        if not store or store.get("platform") != "shopify":
            raise ShopifyPricingError(
                "SHOPIFY_PRICING_UNAVAILABLE",
                f"Merchant {merchant_id} has no Shopify primary store",
                debug_id,
            )

        shop_domain = store.get("domain") or ""
        storefront_token = _extract_storefront_token(store) or ""
        if not shop_domain or not storefront_token:
            raise ShopifyPricingError(
                "SHOPIFY_PRICING_UNAVAILABLE",
                "Missing Shopify Storefront token (X-Shopify-Storefront-Access-Token)",
                debug_id,
            )

        use_buyer_country_for_pricing = self._use_buyer_country_for_pricing()
        cart = await self._create_cart(
            shop_domain=shop_domain,
            storefront_token=storefront_token,
            items=items,
            discount_codes=discount_codes,
            shipping_address=shipping_address,
            selected_delivery_option=selected_delivery_option,
            use_buyer_country_for_pricing=use_buyer_country_for_pricing,
            debug_id=debug_id,
        )

        country_for_prices = None
        if use_buyer_country_for_pricing and shipping_address and isinstance(shipping_address, dict):
            raw_country = (shipping_address.get("country") or "").strip().upper()
            if re.fullmatch(r"[A-Z]{2}", raw_country or ""):
                country_for_prices = raw_country

        # Build line items: best-effort unit price from variant nodes.
        try:
            line_items = await self._build_line_items(
                shop_domain=shop_domain,
                storefront_token=storefront_token,
                items=items,
                currency=cart.currency,
                country=country_for_prices,
                debug_id=debug_id,
            )
        except ShopifyPricingError as e:
            # Self-heal common misconfig:
            # when Storefront scopes were enabled AFTER token issuance, the existing token can still pass
            # `shop { name }` but fails on ProductVariant fields with ACCESS_DENIED.
            if _is_missing_product_listings_scope(e) and _rotate_allowed(
                merchant_id=merchant_id, cooldown_s=self._rotate_cooldown_s
            ):
                new_token = await _rotate_storefront_token_best_effort(
                    merchant_id=merchant_id,
                    store_id=store.get("store_id") if isinstance(store.get("store_id"), str) else None,
                    shop_domain=shop_domain,
                    admin_access_token=store.get("api_key") or "",
                )
                if new_token:
                    storefront_token = new_token
                    line_items = await self._build_line_items(
                        shop_domain=shop_domain,
                        storefront_token=storefront_token,
                        items=items,
                        currency=cart.currency,
                        country=country_for_prices,
                        debug_id=debug_id,
                    )
                else:
                    raise
            else:
                raise

        pricing = {
            "subtotal": cart.subtotal,
            "discount_total": max(cart.subtotal - cart.total, Decimal("0.00")),
            "shipping_fee": Decimal("0.00"),
            "tax": cart.tax,
            "total": cart.total,
        }

        # Best-effort derive shipping_fee:
        # - Prefer selected delivery option estimatedCost when available.
        # - Else fallback to delta: total - subtotal - tax + discount_total (clamped >= 0).
        shipping_fee = self._derive_shipping_fee(cart)
        if shipping_fee is not None:
            pricing["shipping_fee"] = shipping_fee
        else:
            delta = pricing["total"] - pricing["subtotal"] - pricing["tax"] + pricing["discount_total"]
            if delta > 0:
                pricing["shipping_fee"] = _d(delta)

        debug = {
            "debug_id": debug_id,
            "engine": "shopify_storefront_cart",
            "shop_domain": shop_domain,
            "cart_id": cart.cart_id,
            "checkout_url": cart.checkout_url,
            "selected_delivery_option": cart.selected_delivery_option,
            "storefront_delivery_options_count": len(cart.delivery_options or []),
        }

        return ShopifyPricingResult(
            engine="shopify_storefront_cart",
            engine_ref=str(cart.cart_id),
            currency=cart.currency,
            pricing=pricing,
            promotion_lines=[],
            line_items=line_items,
            delivery_options=cart.delivery_options,
            debug=debug,
        )

    async def _storefront_graphql(
        self,
        *,
        shop_domain: str,
        storefront_token: str,
        query: str,
        variables: Optional[Dict[str, Any]],
        debug_id: str,
    ) -> Dict[str, Any]:
        url = f"https://{shop_domain}/api/{self.api_version}/graphql.json"
        headers = {
            "X-Shopify-Storefront-Access-Token": storefront_token,
            "Content-Type": "application/json",
        }
        payload: Dict[str, Any] = {"query": query}
        if variables is not None:
            payload["variables"] = variables

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                resp = await client.post(url, headers=headers, json=payload)
        except Exception as e:
            logger.warning({"debug_id": debug_id, "error": str(e)}, "Shopify Storefront request failed")
            raise ShopifyPricingError("SHOPIFY_PRICING_UNAVAILABLE", "Storefront request failed", debug_id)

        if resp.status_code >= 400:
            logger.warning(
                {
                    "debug_id": debug_id,
                    "status_code": resp.status_code,
                    "x_request_id": resp.headers.get("x-request-id"),
                },
                "Shopify Storefront HTTP error",
            )
            hint = None
            if resp.status_code == 401:
                hint = (
                    "Storefront token invalid for this shop. Reconnect store and provide a valid "
                    "Storefront access token (X-Shopify-Storefront-Access-Token)."
                )
            raise ShopifyPricingError(
                "SHOPIFY_PRICING_UNAVAILABLE",
                f"Storefront pricing error (HTTP {resp.status_code})",
                debug_id,
                details={"status_code": resp.status_code, "hint": hint} if hint else {"status_code": resp.status_code},
            )

        data = resp.json() or {}
        if data.get("errors"):
            raw_errors = data.get("errors") or []
            safe_errors: List[Dict[str, Any]] = []
            for err in raw_errors:
                if not isinstance(err, dict):
                    continue
                ext = err.get("extensions") or {}
                safe_errors.append(
                    {
                        "message": err.get("message"),
                        "code": (ext.get("code") if isinstance(ext, dict) else None),
                        "path": err.get("path"),
                    }
                )
            logger.warning({"debug_id": debug_id, "errors": safe_errors[:5]}, "Shopify Storefront GraphQL errors")
            raise ShopifyPricingError(
                "SHOPIFY_PRICING_UNAVAILABLE",
                "Storefront GraphQL error",
                debug_id,
                details={"errors": safe_errors[:5]},
            )
        return data.get("data") or {}

    async def _create_cart(
        self,
        *,
        shop_domain: str,
        storefront_token: str,
        items: List[Dict[str, Any]],
        discount_codes: List[str],
        shipping_address: Optional[Dict[str, Any]],
        selected_delivery_option: Optional[Dict[str, Any]],
        use_buyer_country_for_pricing: bool,
        debug_id: str,
    ) -> StorefrontCartResult:
        lines = []
        for it in items or []:
            variant_id = str(it.get("variant_id") or "").strip()
            qty = int(it.get("quantity") or 0)
            if not variant_id or qty <= 0:
                continue
            lines.append({"merchandiseId": _gid("ProductVariant", variant_id), "quantity": qty})

        country = None
        postal = None
        city = None
        province = None
        address1 = None
        address2 = None
        if shipping_address and isinstance(shipping_address, dict):
            country = (shipping_address.get("country") or "").strip().upper() or None
            postal = (shipping_address.get("postal_code") or shipping_address.get("zip") or "").strip() or None
            city = (shipping_address.get("city") or "").strip() or None
            province = (shipping_address.get("state") or shipping_address.get("province") or "").strip() or None
            address1 = (
                (shipping_address.get("address_line1") or shipping_address.get("address1") or shipping_address.get("line1") or "")
                .strip()
                or None
            )
            address2 = (
                (shipping_address.get("address_line2") or shipping_address.get("address2") or shipping_address.get("line2") or "")
                .strip()
                or None
            )

        cart_create = """
mutation($input: CartInput!) {
  cartCreate(input: $input) {
    cart {
      id
      checkoutUrl
      cost {
        subtotalAmount { amount currencyCode }
        totalTaxAmount { amount currencyCode }
        totalAmount { amount currencyCode }
      }
    }
    userErrors { field message code }
  }
}
"""
        variables: Dict[str, Any] = {"input": {"lines": lines}}
        if discount_codes:
            variables["input"]["discountCodes"] = discount_codes
        if use_buyer_country_for_pricing and country:
            variables["input"]["buyerIdentity"] = {"countryCode": country}

        data = await self._storefront_graphql(
            shop_domain=shop_domain,
            storefront_token=storefront_token,
            query=cart_create,
            variables=variables,
            debug_id=debug_id,
        )
        root = (data.get("cartCreate") or {}) if isinstance(data, dict) else {}
        user_errors = root.get("userErrors") or []
        if user_errors:
            safe_user_errors: List[Dict[str, Any]] = []
            for err in user_errors:
                if not isinstance(err, dict):
                    continue
                safe_user_errors.append(
                    {
                        "field": err.get("field"),
                        "message": err.get("message"),
                        "code": err.get("code"),
                    }
                )
            msg = (safe_user_errors[0].get("message") if safe_user_errors else None) or "cartCreate failed"
            raise ShopifyPricingError(
                "SHOPIFY_PRICING_UNAVAILABLE",
                msg,
                debug_id,
                details={"user_errors": safe_user_errors[:5]},
            )
        cart = root.get("cart") or {}

        cart_id = cart.get("id") or ""
        checkout_url = cart.get("checkoutUrl") or None
        cost = cart.get("cost") or {}
        subtotal = _d((cost.get("subtotalAmount") or {}).get("amount"))
        total = _d((cost.get("totalAmount") or {}).get("amount"))
        tax_raw = (cost.get("totalTaxAmount") or {}).get("amount")
        tax = _d(tax_raw) if tax_raw is not None else Decimal("0.00")
        currency = (cost.get("totalAmount") or {}).get("currencyCode") or (cost.get("subtotalAmount") or {}).get(
            "currencyCode"
        ) or "USD"

        delivery_options = None
        selected = None

        # Best-effort: attach a delivery address and fetch delivery options.
        if cart_id and country and postal:
            try:
                delivery_options, selected = await self._attach_address_and_select_delivery_best_effort(
                    shop_domain=shop_domain,
                    storefront_token=storefront_token,
                    cart_id=cart_id,
                    country=country,
                    postal=postal,
                    city=city,
                    province=province,
                    address1=address1,
                    address2=address2,
                    selected_delivery_option=selected_delivery_option,
                    debug_id=debug_id,
                )

                # Refresh totals after delivery selection.
                refreshed = await self._get_cart_cost(
                    shop_domain=shop_domain, storefront_token=storefront_token, cart_id=cart_id, debug_id=debug_id
                )
                if refreshed:
                    subtotal = refreshed.subtotal
                    total = refreshed.total
                    tax = refreshed.tax
                    currency = refreshed.currency
            except ShopifyPricingError as e:
                # Delivery address/options are best-effort; keep the quote usable even if
                # the Storefront schema differs across shops/versions.
                logger.info(
                    {"debug_id": debug_id, "code": e.code, "message": e.message, "details": getattr(e, "details", {})},
                    "Storefront delivery options unavailable; continuing without delivery selection",
                )

        return StorefrontCartResult(
            cart_id=cart_id,
            checkout_url=checkout_url,
            currency=currency,
            subtotal=subtotal,
            total=total,
            tax=tax,
            delivery_options=delivery_options,
            selected_delivery_option=selected,
        )

    async def _get_cart_cost(
        self,
        *,
        shop_domain: str,
        storefront_token: str,
        cart_id: str,
        debug_id: str,
    ) -> Optional[StorefrontCartResult]:
        query = """
query($id: ID!) {
  cart(id: $id) {
    id
    checkoutUrl
    cost {
      subtotalAmount { amount currencyCode }
      totalTaxAmount { amount currencyCode }
      totalAmount { amount currencyCode }
    }
  }
}
"""
        data = await self._storefront_graphql(
            shop_domain=shop_domain,
            storefront_token=storefront_token,
            query=query,
            variables={"id": cart_id},
            debug_id=debug_id,
        )
        cart = (data.get("cart") or {}) if isinstance(data, dict) else {}
        if not cart:
            return None
        cost = cart.get("cost") or {}
        subtotal = _d((cost.get("subtotalAmount") or {}).get("amount"))
        total = _d((cost.get("totalAmount") or {}).get("amount"))
        tax_raw = (cost.get("totalTaxAmount") or {}).get("amount")
        tax = _d(tax_raw) if tax_raw is not None else Decimal("0.00")
        currency = (cost.get("totalAmount") or {}).get("currencyCode") or (cost.get("subtotalAmount") or {}).get(
            "currencyCode"
        ) or "USD"
        return StorefrontCartResult(
            cart_id=cart.get("id") or cart_id,
            checkout_url=cart.get("checkoutUrl") or None,
            currency=currency,
            subtotal=subtotal,
            total=total,
            tax=tax,
            delivery_options=None,
            selected_delivery_option=None,
        )

    async def _attach_address_and_select_delivery_best_effort(
        self,
        *,
        shop_domain: str,
        storefront_token: str,
        cart_id: str,
        country: str,
        postal: str,
        city: Optional[str],
        province: Optional[str],
        address1: Optional[str],
        address2: Optional[str],
        selected_delivery_option: Optional[Dict[str, Any]],
        debug_id: str,
    ) -> tuple[Optional[List[Dict[str, Any]]], Optional[Dict[str, Any]]]:
        # NOTE: Storefront schema varies by shop/api-version. Some expect:
        # - addresses: [CartSelectableAddressInput!]! with field "deliveryAddress"
        # - others expect field "address"
        # Try both shapes before giving up.
        add_address = """
mutation($cartId: ID!, $addresses: [CartSelectableAddressInput!]!) {
  cartDeliveryAddressesAdd(cartId: $cartId, addresses: $addresses) {
    cart { id }
    userErrors { field message code }
  }
}
"""

        base_addr = {
            "countryCode": country,
            "zip": postal,
            **({"city": city} if city else {}),
            **({"provinceCode": province} if province else {}),
            **({"address1": address1} if address1 else {}),
            **({"address2": address2} if address2 else {}),
        }

        add_shapes = [
            [{"deliveryAddress": base_addr}],
            [{"address": base_addr}],
        ]

        added = False
        last_error_details: Dict[str, Any] = {}
        for addresses in add_shapes:
            try:
                data = await self._storefront_graphql(
                    shop_domain=shop_domain,
                    storefront_token=storefront_token,
                    query=add_address,
                    variables={"cartId": cart_id, "addresses": addresses},
                    debug_id=debug_id,
                )
                root = (data.get("cartDeliveryAddressesAdd") or {}) if isinstance(data, dict) else {}
                user_errors = root.get("userErrors") or []
                if user_errors:
                    # Keep trying other shapes.
                    last_error_details = {"user_errors": user_errors}
                    continue
                added = True
                break
            except ShopifyPricingError as e:
                last_error_details = getattr(e, "details", {}) or {}
                continue

        # Even if we fail to attach the address, still try to query deliveryGroups.
        # Some shops return delivery options based on buyerIdentity/country only.

        # Query delivery options (schema varies; keep it tolerant).
        delivery_query = """
query($id: ID!) {
  cart(id: $id) {
    deliveryGroups(first: 10) {
      edges {
        node {
          id
          deliveryOptions {
            handle
            title
            description
            estimatedCost { amount currencyCode }
          }
        }
      }
    }
  }
}
"""
        data2 = await self._storefront_graphql(
            shop_domain=shop_domain,
            storefront_token=storefront_token,
            query=delivery_query,
            variables={"id": cart_id},
            debug_id=debug_id,
        )
        cart = (data2.get("cart") or {}) if isinstance(data2, dict) else {}
        groups = (((cart.get("deliveryGroups") or {}).get("edges")) or []) if isinstance(cart, dict) else []
        options: List[Dict[str, Any]] = []
        for edge in groups:
            node = (edge or {}).get("node") or {}
            group_id = node.get("id")
            for opt in node.get("deliveryOptions") or []:
                if not isinstance(opt, dict):
                    continue
                options.append({**opt, "delivery_group_id": group_id})

        if not options:
            if not added and last_error_details:
                raise ShopifyPricingError(
                    "SHOPIFY_PRICING_UNAVAILABLE",
                    "No delivery options; address attach failed",
                    debug_id,
                    details=last_error_details,
                )
            return None, None

        def _opt_cost_amount(o: Dict[str, Any]) -> Decimal:
            est = o.get("estimatedCost") or {}
            return _d(est.get("amount"))

        # Select: honor provided selection if possible, else pick cheapest.
        chosen = None
        if selected_delivery_option and isinstance(selected_delivery_option, dict):
            want_handle = selected_delivery_option.get("handle") or selected_delivery_option.get("id")
            want_group = selected_delivery_option.get("delivery_group_id") or selected_delivery_option.get("deliveryGroupId")
            if want_handle:
                for o in options:
                    if o.get("handle") == want_handle and (not want_group or o.get("delivery_group_id") == want_group):
                        chosen = o
                        break
        if chosen is None:
            chosen = sorted(options, key=_opt_cost_amount)[0]

        # Apply selection.
        update_sel = """
mutation($cartId: ID!, $selectedDeliveryOptions: [CartSelectedDeliveryOptionInput!]!) {
  cartSelectedDeliveryOptionsUpdate(cartId: $cartId, selectedDeliveryOptions: $selectedDeliveryOptions) {
    cart { id }
    userErrors { field message code }
  }
}
"""
        group_id = chosen.get("delivery_group_id")
        try:
            await self._storefront_graphql(
                shop_domain=shop_domain,
                storefront_token=storefront_token,
                query=update_sel,
                variables={
                    "cartId": cart_id,
                    "selectedDeliveryOptions": [
                        {
                            "deliveryGroupId": group_id,
                            "deliveryOptionHandle": chosen.get("handle"),
                        }
                    ],
                },
                debug_id=debug_id,
            )
        except Exception:
            pass

        return options, chosen

    async def _build_line_items(
        self,
        *,
        shop_domain: str,
        storefront_token: str,
        items: List[Dict[str, Any]],
        currency: str,
        country: Optional[str],
        debug_id: str,
    ) -> List[Dict[str, Any]]:
        # Fetch variant prices in one round-trip using nodes().
        variant_ids: List[str] = []
        for it in items or []:
            vid = str(it.get("variant_id") or "").strip()
            if vid:
                variant_ids.append(vid)
        unique = list(dict.fromkeys(variant_ids))
        if not unique:
            return []

        gids = [_gid("ProductVariant", vid) for vid in unique]
        if country:
            # IMPORTANT: cart pricing can be in the buyer's presentment currency (based on buyerIdentity country).
            # Without `@inContext(country: ...)`, variant prices returned by nodes() default to shop currency,
            # which leads to mismatched currency codes in checkout (e.g. cart totals USD but unit prices EUR).
            query = """
query($ids: [ID!]!, $country: CountryCode!) @inContext(country: $country) {
  nodes(ids: $ids) {
    ... on ProductVariant {
      id
      price { amount currencyCode }
      compareAtPrice { amount currencyCode }
    }
  }
}
"""
            variables = {"ids": gids, "country": country}
        else:
            query = """
query($ids: [ID!]!) {
  nodes(ids: $ids) {
    ... on ProductVariant {
      id
      price { amount currencyCode }
      compareAtPrice { amount currencyCode }
    }
  }
}
"""
            variables = {"ids": gids}

        data = await self._storefront_graphql(
            shop_domain=shop_domain,
            storefront_token=storefront_token,
            query=query,
            variables=variables,
            debug_id=debug_id,
        )
        nodes = data.get("nodes") or []
        price_by_gid: Dict[str, Dict[str, Any]] = {}
        for n in nodes:
            if not isinstance(n, dict):
                continue
            gid = n.get("id")
            if gid:
                price_by_gid[str(gid)] = n

        out: List[Dict[str, Any]] = []
        for it in items or []:
            vid = str(it.get("variant_id") or "").strip()
            qty = int(it.get("quantity") or 0)
            if not vid or qty <= 0:
                continue
            gid = _gid("ProductVariant", vid)
            node = price_by_gid.get(gid) or {}
            price = (node.get("price") or {}) if isinstance(node, dict) else {}
            compare = (node.get("compareAtPrice") or {}) if isinstance(node, dict) else {}
            unit = _d(price.get("amount"))
            compare_unit = _d(compare.get("amount")) if compare.get("amount") is not None else Decimal("0.00")
            compare_savings = max(compare_unit - unit, Decimal("0.00")) if compare_unit else Decimal("0.00")
            out.append(
                {
                    "product_id": it.get("product_id"),
                    "variant_id": vid,
                    "quantity": qty,
                    "unit_price_original": unit,
                    "unit_price_effective": unit,
                    "line_discount_total": Decimal("0.00"),
                    "compare_at_savings": compare_savings,
                }
            )
        return out

    def _derive_shipping_fee(self, cart: StorefrontCartResult) -> Optional[Decimal]:
        opts = cart.delivery_options or []
        if not opts:
            return None
        chosen = cart.selected_delivery_option or opts[0]
        est = chosen.get("estimatedCost") if isinstance(chosen, dict) else None
        if not isinstance(est, dict):
            return None
        amt = est.get("amount")
        if amt is None:
            return None
        fee = _d(amt)
        if fee < 0:
            return Decimal("0.00")
        return fee
