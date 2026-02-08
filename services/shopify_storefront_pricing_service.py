from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

import asyncio
import base64
import hashlib
import httpx
import json
import os
import re
import time

from db.database import database
from services.merchant_store_service import get_primary_store
from services.shopify_access_token_service import resolve_shopify_admin_access_token
from services.shopify_pricing_service import ShopifyPricingError, ShopifyPricingResult
from utils.logger import logger


def _d(v: Any) -> Decimal:
    try:
        return Decimal(str(v or "0")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except Exception:
        return Decimal("0.00")


def _gid(kind: str, numeric_id: str) -> str:
    return f"gid://shopify/{kind}/{numeric_id}"


def _storefront_id(kind: str, numeric_id: str) -> str:
    # Storefront GraphQL IDs are opaque (base64) strings, not raw gid://... URIs.
    return base64.b64encode(_gid(kind, numeric_id).encode("utf-8")).decode("utf-8")


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
    unit_price_by_variant_id: Dict[str, Decimal]


_STOREFRONT_ROTATE_ATTEMPTED_AT: Dict[str, float] = {}


def _rotate_allowed(*, merchant_id: str, cooldown_s: int) -> bool:
    now = time.time()
    last = _STOREFRONT_ROTATE_ATTEMPTED_AT.get(merchant_id, 0.0)
    if (now - last) < float(max(cooldown_s, 0)):
        return False
    _STOREFRONT_ROTATE_ATTEMPTED_AT[merchant_id] = now
    return True


def _is_invalid_merchandise_id(err: ShopifyPricingError) -> bool:
    try:
        details = getattr(err, "details", {}) or {}
        user_errors = details.get("user_errors") or []
        if not isinstance(user_errors, list):
            return False
        for ue in user_errors:
            if not isinstance(ue, dict):
                continue
            msg = str(ue.get("message") or "")
            field = ue.get("field") or []
            code = str(ue.get("code") or "").upper()
            if code == "INVALID" and "merchandise" in msg.lower() and "does not exist" in msg.lower():
                return True
            if isinstance(field, list) and any(str(p).lower() == "merchandiseid" for p in field):
                if "does not exist" in msg.lower():
                    return True
        return False
    except Exception:
        return False


def _first_item_variant_id(items: List[Dict[str, Any]]) -> Optional[str]:
    for it in items or []:
        vid = str(it.get("variant_id") or "").strip()
        if vid:
            return vid
    return None


async def _rotate_storefront_token_best_effort(
    *,
    merchant_id: str,
    store_id: Optional[str],
    shop_domain: str,
    admin_access_token: str,
    existing_credentials: Optional[Dict[str, Any]],
) -> Optional[str]:
    """
    Best-effort: create a new Storefront token and persist it for the store row.
    This helps when Storefront scopes were enabled AFTER the original token was issued.
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

        # Persist: merge into api_key JSON for this store row (when it's a real merchant_stores row).
        if store_id and not str(store_id).startswith("legacy_"):
            merged = dict(existing_credentials or {})
            merged.setdefault("access_token", admin_access_token)
            merged["storefront_access_token"] = new_token
            await database.execute(
                """
                UPDATE merchant_stores
                SET api_key = :api_key, connected_at = CURRENT_TIMESTAMP
                WHERE store_id = :store_id AND merchant_id = :merchant_id
                """,
                {"api_key": json.dumps(merged), "store_id": store_id, "merchant_id": merchant_id},
            )
        return new_token
    except Exception:
        return None


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
        # Avoid repeatedly using Admin API tokens to auto-create Storefront tokens at runtime.
        # Storefront token should be created once at connect time (or manually), then reused.
        # Enable runtime rotate only when explicitly opted-in.
        self._runtime_rotate_enabled = (os.getenv("SHOPIFY_STOREFRONT_RUNTIME_ROTATE") or "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

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

    async def _admin_graphql(
        self,
        *,
        shop_domain: str,
        admin_access_token: str,
        query: str,
        variables: Optional[Dict[str, Any]],
        debug_id: str,
    ) -> Dict[str, Any]:
        url = f"https://{shop_domain}/admin/api/{self.api_version}/graphql.json"
        headers = {"X-Shopify-Access-Token": admin_access_token, "Content-Type": "application/json"}
        payload: Dict[str, Any] = {"query": query}
        if variables is not None:
            payload["variables"] = variables

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                resp = await client.post(url, headers=headers, json=payload)
        except Exception as e:
            logger.warning({"debug_id": debug_id, "error": str(e)}, "Shopify Admin GraphQL request failed")
            return {}

        if resp.status_code >= 400:
            logger.warning(
                {
                    "debug_id": debug_id,
                    "status_code": resp.status_code,
                    "x_request_id": resp.headers.get("x-request-id"),
                },
                "Shopify Admin GraphQL HTTP error",
            )
            return {}

        data = resp.json() or {}
        if data.get("errors"):
            logger.warning({"debug_id": debug_id, "errors": data.get("errors")[:3]}, "Shopify Admin GraphQL errors")
            return {}
        return data.get("data") or {}

    async def _admin_variant_exists(
        self,
        *,
        shop_domain: str,
        admin_access_token: str,
        variant_id: str,
        debug_id: str,
    ) -> Optional[bool]:
        if not shop_domain or not admin_access_token or not variant_id:
            return None
        query = """
query($id: ID!) {
  productVariant(id: $id) { id }
}
"""
        data = await self._admin_graphql(
            shop_domain=shop_domain,
            admin_access_token=admin_access_token,
            query=query,
            variables={"id": _gid("ProductVariant", variant_id)},
            debug_id=debug_id,
        )
        pv = (data.get("productVariant") if isinstance(data, dict) else None) or None
        return bool(pv)

    async def _admin_fetch_variant_inventory(
        self,
        *,
        shop_domain: str,
        admin_access_token: str,
        variant_ids: List[str],
        debug_id: str,
    ) -> Dict[str, Dict[str, Any]]:
        """
        Best-effort inventory signal for enforcing Shopify inventoryPolicy=DENY.

        Notes:
        - Admin API is authoritative for inventory policy. We only enforce when:
          - inventory is tracked, AND
          - inventoryPolicy == DENY, AND
          - inventoryQuantity is available.
        - If the Admin API call fails (401/403/etc), return {} and fail-open.
        """
        ids = []
        for vid in variant_ids or []:
            s = str(vid or "").strip()
            if not s:
                continue
            ids.append(_gid("ProductVariant", s))
        # de-dup (stable)
        ids = list(dict.fromkeys(ids))
        if not ids:
            return {}

        query = """
query($ids: [ID!]!) {
  nodes(ids: $ids) {
    ... on ProductVariant {
      legacyResourceId
      inventoryQuantity
      inventoryPolicy
      inventoryItem { tracked }
    }
  }
}
"""
        data = await self._admin_graphql(
            shop_domain=shop_domain,
            admin_access_token=admin_access_token,
            query=query,
            variables={"ids": ids},
            debug_id=debug_id,
        )
        nodes = (data.get("nodes") if isinstance(data, dict) else None) or []
        if not isinstance(nodes, list):
            return {}

        out: Dict[str, Dict[str, Any]] = {}
        for n in nodes:
            if not isinstance(n, dict):
                continue
            legacy = n.get("legacyResourceId")
            if legacy is None:
                continue
            vid = str(legacy).strip()
            if not vid:
                continue
            inv_qty = n.get("inventoryQuantity")
            policy = n.get("inventoryPolicy")
            tracked = None
            try:
                inv_item = n.get("inventoryItem") or {}
                tracked = inv_item.get("tracked") if isinstance(inv_item, dict) else None
            except Exception:
                tracked = None
            out[vid] = {
                "inventory_quantity": inv_qty,
                "inventory_policy": policy,
                "inventory_tracked": tracked,
            }
        return out

    async def _enforce_inventory_policy_best_effort(
        self,
        *,
        shop_domain: str,
        admin_access_token: str,
        items: List[Dict[str, Any]],
        debug_id: str,
    ) -> None:
        variant_ids: List[str] = []
        for it in items or []:
            vid = str((it or {}).get("variant_id") or "").strip()
            if vid:
                variant_ids.append(vid)
        variant_ids = list(dict.fromkeys(variant_ids))
        if not variant_ids:
            return

        inv = await self._admin_fetch_variant_inventory(
            shop_domain=shop_domain,
            admin_access_token=admin_access_token,
            variant_ids=variant_ids,
            debug_id=debug_id,
        )
        if not inv:
            return

        for it in items or []:
            vid = str((it or {}).get("variant_id") or "").strip()
            qty = int((it or {}).get("quantity") or 0)
            if not vid or qty <= 0:
                continue
            meta = inv.get(vid) or {}
            policy = str(meta.get("inventory_policy") or "").upper()
            tracked = meta.get("inventory_tracked")
            if tracked is False:
                continue
            if policy != "DENY":
                continue
            inv_qty = meta.get("inventory_quantity")
            if inv_qty is None:
                continue
            try:
                available = int(inv_qty)
            except Exception:
                continue

            if available <= 0:
                raise ShopifyPricingError(
                    "OUT_OF_STOCK",
                    "Item is out of stock",
                    debug_id,
                    details={
                        "shop_domain": shop_domain,
                        "variant_id": vid,
                        "requested_quantity": qty,
                        "available_quantity": available,
                        "inventory_policy": policy,
                    },
                )
            if available < qty:
                raise ShopifyPricingError(
                    "INSUFFICIENT_INVENTORY",
                    "Not enough inventory available",
                    debug_id,
                    details={
                        "shop_domain": shop_domain,
                        "variant_id": vid,
                        "requested_quantity": qty,
                        "available_quantity": available,
                        "inventory_policy": policy,
                    },
                )

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
        if not shop_domain:
            raise ShopifyPricingError(
                "SHOPIFY_PRICING_UNAVAILABLE",
                "Missing Shopify shop domain",
                debug_id,
            )

        admin_access_token, _ = await resolve_shopify_admin_access_token(
            shop_domain=shop_domain,
            api_key_raw=store.get("api_key_raw") or store.get("api_key"),
            store_id=str(store.get("store_id") or "").strip() or None,
        )
        admin_access_token = (admin_access_token or "").strip() or None

        if (
            not storefront_token
            and self._runtime_rotate_enabled
            and _rotate_allowed(merchant_id=merchant_id, cooldown_s=self._rotate_cooldown_s)
        ):
            if admin_access_token:
                storefront_token = (
                    await _rotate_storefront_token_best_effort(
                        merchant_id=merchant_id,
                        store_id=store.get("store_id") if isinstance(store.get("store_id"), str) else None,
                        shop_domain=shop_domain,
                        admin_access_token=admin_access_token,
                        existing_credentials=store.get("api_credentials")
                        if isinstance(store.get("api_credentials"), dict)
                        else None,
                    )
                    or ""
                )

        if not storefront_token:
            raise ShopifyPricingError(
                "SHOPIFY_PRICING_UNAVAILABLE",
                "Missing Shopify Storefront token (X-Shopify-Storefront-Access-Token)",
                debug_id,
            )

        use_buyer_country_for_pricing = self._use_buyer_country_for_pricing()
        cart: Optional[StorefrontCartResult] = None
        err: Optional[ShopifyPricingError] = None
        try:
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
        except ShopifyPricingError as e:
            err = e

        if err is not None:
            if _is_invalid_merchandise_id(err):
                rotated = False

                # Self-heal common misconfig: Storefront scopes enabled after token issuance.
                if (
                    admin_access_token
                    and self._runtime_rotate_enabled
                    and _rotate_allowed(merchant_id=merchant_id, cooldown_s=self._rotate_cooldown_s)
                ):
                    new_token = await _rotate_storefront_token_best_effort(
                        merchant_id=merchant_id,
                        store_id=store.get("store_id") if isinstance(store.get("store_id"), str) else None,
                        shop_domain=shop_domain,
                        admin_access_token=admin_access_token,
                        existing_credentials=store.get("api_credentials")
                        if isinstance(store.get("api_credentials"), dict)
                        else None,
                    )
                    if new_token:
                        rotated = True
                        storefront_token = new_token
                        try:
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
                            err = None
                        except ShopifyPricingError as e2:
                            err = e2

                if err is not None:
                    vid = _first_item_variant_id(items or [])
                    exists = (
                        await self._admin_variant_exists(
                            shop_domain=shop_domain,
                            admin_access_token=admin_access_token,
                            variant_id=vid or "",
                            debug_id=debug_id,
                        )
                        if admin_access_token and vid
                        else None
                    )
                    hint = None
                    if exists is True:
                        hint = (
                            "Variant exists in Shopify Admin but is not available to Storefront API. "
                            "Ensure the product is published to the Online Store sales channel and that "
                            "Storefront API access is enabled (incl. `unauthenticated_read_product_listings`)."
                        )
                    elif exists is False:
                        hint = (
                            "Variant not found in this Shopify store. Your product cache may be stale; "
                            "resync products after reconnecting the store."
                        )
                    raise ShopifyPricingError(
                        "SHOPIFY_PRICING_UNAVAILABLE",
                        (hint or str(getattr(err, "message", None) or "Storefront cartCreate failed")),
                        debug_id,
                        details={
                            "shop_domain": shop_domain,
                            "variant_id": vid,
                            "admin_variant_exists": exists,
                            "storefront_token_rotated": rotated,
                            "storefront_runtime_rotate_enabled": self._runtime_rotate_enabled,
                            "cart_create_error": getattr(err, "details", {}) or {},
                        },
                    )

                raise err

        # Inventory enforcement (best-effort): Shopify Admin inventoryPolicy=DENY must not be oversold.
        # This is critical because we create Shopify orders via Admin API (not Shopify checkout),
        # which can otherwise bypass storefront "sold out" restrictions.
        try:
            if admin_access_token:
                await self._enforce_inventory_policy_best_effort(
                    shop_domain=shop_domain,
                    admin_access_token=admin_access_token,
                    items=items,
                    debug_id=debug_id,
                )
        except ShopifyPricingError:
            raise
        except Exception:
            pass

        country_for_prices = None
        if use_buyer_country_for_pricing and shipping_address and isinstance(shipping_address, dict):
            raw_country = (shipping_address.get("country") or "").strip().upper()
            if re.fullmatch(r"[A-Z]{2}", raw_country or ""):
                country_for_prices = raw_country

        # Build line items from cart line costs (avoids extra ProductVariant nodes() query,
        # which can be denied when `unauthenticated_read_product_listings` isn't enabled).
        line_items: List[Dict[str, Any]] = []
        for it in items or []:
            vid = str(it.get("variant_id") or "").strip()
            qty = int(it.get("quantity") or 0)
            if not vid or qty <= 0:
                continue
            unit = cart.unit_price_by_variant_id.get(vid) or Decimal("0.00")
            line_items.append(
                {
                    "product_id": it.get("product_id"),
                    "variant_id": vid,
                    "quantity": qty,
                    "unit_price_original": unit,
                    "unit_price_effective": unit,
                    "line_discount_total": Decimal("0.00"),
                    "compare_at_savings": Decimal("0.00"),
                }
            )

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
            required_access: List[str] = []
            for e in safe_errors:
                msg = str(e.get("message") or "")
                m = re.search(r"Required access: `([^`]+)`", msg)
                if m:
                    required_access.append(m.group(1))

            details: Dict[str, Any] = {"errors": safe_errors[:5]}
            if required_access:
                details["required_access"] = sorted(set(required_access))
            raise ShopifyPricingError(
                "SHOPIFY_PRICING_UNAVAILABLE",
                "Storefront GraphQL error"
                if not required_access
                else f"Storefront access denied (missing scope: {details['required_access'][0]})",
                debug_id,
                details=details,
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
            # Attach variant_id as a cart line attribute so we can map line costs back
            # without needing ProductVariant read scopes.
            lines.append(
                {
                    "merchandiseId": _storefront_id("ProductVariant", variant_id),
                    "quantity": qty,
                    "attributes": [{"key": "pivota_variant_id", "value": variant_id}],
                }
            )

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
      lines(first: 100) {
        edges {
          node {
            quantity
            attributes { key value }
            cost {
              amountPerQuantity { amount currencyCode }
              totalAmount { amount currencyCode }
            }
          }
        }
      }
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

        unit_price_by_variant_id: Dict[str, Decimal] = {}
        try:
            lines_root = cart.get("lines") or {}
            edges = lines_root.get("edges") or []
            for e in edges or []:
                node = (e.get("node") or {}) if isinstance(e, dict) else {}
                if not isinstance(node, dict):
                    continue
                attrs = node.get("attributes") or []
                variant_id = None
                if isinstance(attrs, list):
                    for a in attrs:
                        if not isinstance(a, dict):
                            continue
                        if a.get("key") == "pivota_variant_id":
                            variant_id = a.get("value")
                            break
                if not isinstance(variant_id, str) or not variant_id.strip():
                    continue
                cost = node.get("cost") or {}
                apq = (cost.get("amountPerQuantity") or {}) if isinstance(cost, dict) else {}
                amt = apq.get("amount") if isinstance(apq, dict) else None
                if amt is None:
                    # Fallback: total / qty
                    total_amt = ((cost.get("totalAmount") or {}) if isinstance(cost, dict) else {}).get("amount")
                    qty = int(node.get("quantity") or 0)
                    if total_amt is not None and qty > 0:
                        unit_price_by_variant_id[variant_id.strip()] = _d(Decimal(str(total_amt)) / Decimal(qty))
                    continue
                unit_price_by_variant_id[variant_id.strip()] = _d(amt)
        except Exception:
            unit_price_by_variant_id = {}
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
            delivery_timeout_s = float(os.getenv("SHOPIFY_STOREFRONT_DELIVERY_TIMEOUT_SECONDS", "8") or "8")
            delivery_timeout_s = max(0.5, delivery_timeout_s)
            try:
                async def _delivery_work() -> tuple[Optional[List[Dict[str, Any]]], Optional[Dict[str, Any]], Optional[StorefrontCartResult]]:
                    opts, sel = await self._attach_address_and_select_delivery_best_effort(
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
                        shop_domain=shop_domain,
                        storefront_token=storefront_token,
                        cart_id=cart_id,
                        debug_id=debug_id,
                    )
                    return opts, sel, refreshed

                delivery_options, selected, refreshed = await asyncio.wait_for(
                    _delivery_work(), timeout=delivery_timeout_s
                )

                if refreshed:
                    subtotal = refreshed.subtotal
                    total = refreshed.total
                    tax = refreshed.tax
                    currency = refreshed.currency
            except asyncio.TimeoutError:
                logger.info(
                    {"debug_id": debug_id, "timeout_seconds": delivery_timeout_s},
                    "Storefront delivery options timed out; continuing without delivery selection",
                )
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
            unit_price_by_variant_id=unit_price_by_variant_id,
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
            unit_price_by_variant_id={},
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

        ids = [_storefront_id("ProductVariant", vid) for vid in unique]
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
            variables = {"ids": ids, "country": country}
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
            variables = {"ids": ids}

        data = await self._storefront_graphql(
            shop_domain=shop_domain,
            storefront_token=storefront_token,
            query=query,
            variables=variables,
            debug_id=debug_id,
        )
        nodes = data.get("nodes") or []
        price_by_id: Dict[str, Dict[str, Any]] = {}
        for n in nodes:
            if not isinstance(n, dict):
                continue
            node_id = n.get("id")
            if node_id:
                price_by_id[str(node_id)] = n

        out: List[Dict[str, Any]] = []
        for it in items or []:
            vid = str(it.get("variant_id") or "").strip()
            qty = int(it.get("quantity") or 0)
            if not vid or qty <= 0:
                continue
            node_id = _storefront_id("ProductVariant", vid)
            node = price_by_id.get(node_id) or {}
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
