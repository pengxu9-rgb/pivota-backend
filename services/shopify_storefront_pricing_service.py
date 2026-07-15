from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

import asyncio
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


def _log_shopify_warning(event: str, fields: Dict[str, Any]) -> None:
    logger.warning("%s %s", event, json.dumps(fields, sort_keys=True, default=str))


def _log_shopify_info(event: str, fields: Dict[str, Any]) -> None:
    logger.info("%s %s", event, json.dumps(fields, sort_keys=True, default=str))


def _gid(kind: str, numeric_id: str) -> str:
    return f"gid://shopify/{kind}/{numeric_id}"


def _storefront_id(kind: str, numeric_id: str) -> str:
    return _gid(kind, numeric_id)


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
    line_pricing_by_variant_id: Dict[str, Dict[str, Decimal]]
    promotion_lines: List[Dict[str, Any]]
    discount_codes: List[Dict[str, Any]]
    discount_total: Decimal
    shipping_discount_total: Decimal
    discount_evidence: Dict[str, Any]
    delivery_diagnostics: Optional[Dict[str, Any]] = None


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


def _normalize_code(value: Any) -> str:
    return str(value or "").strip().upper()


def _discount_class_from_storefront_target(target_type: Any) -> str:
    value = str(target_type or "").strip().lower()
    if "shipping" in value:
        return "shipping"
    if "line" in value or "product" in value:
        return "product"
    return "order"


def _discount_class_from_storefront_cart_allocation(allocation: Dict[str, Any]) -> str:
    target_type = str((allocation or {}).get("targetType") or "").strip().lower()
    if "shipping" in target_type:
        return "shipping"
    return "order"


def _discount_method_from_storefront_allocation(allocation: Dict[str, Any]) -> str:
    typename = str(allocation.get("__typename") or "")
    if typename == "CartCodeDiscountAllocation" or allocation.get("code"):
        return "code"
    if typename == "CartAutomaticDiscountAllocation":
        return "automatic"
    return "app"


def _empty_discount_evidence(
    *,
    source: str,
    submitted_codes: Optional[List[str]] = None,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    codes = [
        {"code": code, "applicable": None, "source": source}
        for code in [_normalize_code(c) for c in (submitted_codes or [])]
        if code
    ]
    return {
        "source": source,
        "codes": codes,
        "applications": [],
        "decisions": [],
        "pricing_confidence": "unverified" if codes else "authoritative",
        **({"reason": reason} if reason else {}),
    }


def _original_subtotal_from_line_items(line_items: List[Dict[str, Any]]) -> Decimal:
    subtotal = Decimal("0.00")
    for li in line_items or []:
        if not isinstance(li, dict):
            continue
        try:
            qty = int(li.get("quantity") or 0)
        except Exception:
            qty = 0
        if qty <= 0:
            continue
        subtotal += _d(li.get("unit_price_original")) * Decimal(qty)
    return _d(subtotal)


def _infer_shipping_fee_from_totals(
    *,
    subtotal: Decimal,
    total: Decimal,
    tax: Decimal,
    discount_total: Decimal,
) -> Optional[Decimal]:
    delta = _d(total) - _d(subtotal) - _d(tax) + _d(discount_total)
    if delta <= 0:
        return None
    return _d(delta)


def _mark_shipping_evidence(
    evidence: Dict[str, Any],
    *,
    status: str,
    reason: Optional[str] = None,
    amount: Optional[Decimal] = None,
    source: str = "shopify_storefront_cart",
) -> None:
    existing = evidence.get("shipping_evidence") if isinstance(evidence, dict) else None
    row: Dict[str, Any] = dict(existing or {}) if isinstance(existing, dict) else {}
    row.update({"status": status, "source": source})
    if reason:
        row["reason"] = reason
    if amount is not None:
        row["amount"] = str(_d(amount))
    evidence["shipping_evidence"] = row
    if status != "authoritative" and evidence.get("pricing_confidence") == "authoritative":
        evidence["pricing_confidence"] = "partial"


def _shipping_unverified_reason(evidence: Dict[str, Any]) -> str:
    shipping_evidence = evidence.get("shipping_evidence") if isinstance(evidence, dict) else None
    line_requirements = (
        shipping_evidence.get("line_shipping_requirements")
        if isinstance(shipping_evidence, dict)
        else None
    )
    if not isinstance(line_requirements, list) or not line_requirements:
        return "delivery_options_unavailable"
    known = [row.get("requires_shipping") for row in line_requirements if isinstance(row, dict)]
    if known and all(value is False for value in known):
        return "cart_lines_do_not_require_shipping"
    if known and any(value is True for value in known):
        return "shipping_rates_unavailable_for_shippable_lines"
    return "delivery_options_unavailable"


def _shopify_cart_selectable_address_input(
    *,
    country: str,
    postal: str,
    city: Optional[str],
    province: Optional[str],
    address1: Optional[str],
    address2: Optional[str],
) -> Dict[str, Any]:
    delivery_address = {
        "countryCode": country,
        "zip": postal,
        **({"city": city} if city else {}),
        **({"provinceCode": province} if province else {}),
        **({"address1": address1} if address1 else {}),
        **({"address2": address2} if address2 else {}),
    }
    return {
        "selected": True,
        "oneTimeUse": True,
        "address": {"deliveryAddress": delivery_address},
    }


def _shopify_buyer_delivery_address_preference_input(
    *,
    country: str,
    postal: str,
    city: Optional[str],
    province: Optional[str],
    address1: Optional[str],
    address2: Optional[str],
) -> Dict[str, Any]:
    delivery_address = {
        "country": country,
        "zip": postal,
        **({"city": city} if city else {}),
        **({"province": province} if province else {}),
        **({"address1": address1} if address1 else {}),
        **({"address2": address2} if address2 else {}),
    }
    return {
        "oneTimeUse": True,
        "deliveryAddress": delivery_address,
    }


def _shopify_cart_buyer_identity_input(
    *,
    customer_email: Optional[str],
    country: Optional[str],
    postal: Optional[str],
    city: Optional[str],
    province: Optional[str],
    address1: Optional[str],
    address2: Optional[str],
    use_buyer_country_for_pricing: bool,
) -> Dict[str, Any]:
    buyer_identity: Dict[str, Any] = {}
    email = str(customer_email or "").strip()
    if email:
        buyer_identity["email"] = email
    if use_buyer_country_for_pricing and country:
        buyer_identity["countryCode"] = country
    if country and postal:
        buyer_identity["preferences"] = {"delivery": {"deliveryMethod": ["SHIPPING"]}}
        buyer_identity["deliveryAddressPreferences"] = [
            _shopify_buyer_delivery_address_preference_input(
                country=country,
                postal=postal,
                city=city,
                province=province,
                address1=address1,
                address2=address2,
            )
        ]
    return buyer_identity


def _is_storefront_discount_query_error(err: ShopifyPricingError) -> bool:
    details = getattr(err, "details", {}) or {}
    errors = details.get("errors") or []
    haystack = " ".join(
        str((e or {}).get("message") or "") + " " + str((e or {}).get("path") or "")
        for e in errors
        if isinstance(e, dict)
    ).lower()
    return any(
        token in haystack
        for token in (
            "discountallocations",
            "discountcodes",
            "discountedamount",
            "targettype",
            "cartcodediscountallocation",
            "cartautomaticdiscountallocation",
            "cartcustomdiscountallocation",
        )
    )


def _parse_storefront_cart_discounts(
    *,
    cart: Dict[str, Any],
    submitted_codes: Optional[List[str]],
    source: str = "shopify_storefront_cart",
) -> Dict[str, Any]:
    submitted = [_normalize_code(c) for c in (submitted_codes or []) if _normalize_code(c)]

    code_rows: List[Dict[str, Any]] = []
    cart_codes = cart.get("discountCodes")
    if isinstance(cart_codes, list):
        seen_codes = set()
        for row in cart_codes:
            if not isinstance(row, dict):
                continue
            code = _normalize_code(row.get("code"))
            if not code or code in seen_codes:
                continue
            seen_codes.add(code)
            applicable = row.get("applicable")
            code_rows.append(
                {
                    "code": code,
                    "applicable": bool(applicable) if applicable is not None else None,
                    "source": source,
                }
            )
        for code in submitted:
            if code not in seen_codes:
                code_rows.append({"code": code, "applicable": None, "source": source})
    else:
        code_rows = [{"code": code, "applicable": None, "source": source} for code in submitted]

    unit_price_by_variant_id: Dict[str, Decimal] = {}
    line_pricing_by_variant_id: Dict[str, Dict[str, Decimal]] = {}
    grouped: Dict[str, Dict[str, Any]] = {}
    line_shipping_requirements: List[Dict[str, Any]] = []
    discount_total = Decimal("0.00")
    shipping_discount_total = Decimal("0.00")

    lines_root = cart.get("lines") or {}
    edges = lines_root.get("edges") or []
    for edge in edges or []:
        node = (edge.get("node") or {}) if isinstance(edge, dict) else {}
        if not isinstance(node, dict):
            continue
        attrs = node.get("attributes") or []
        variant_id = None
        if isinstance(attrs, list):
            for attr in attrs:
                if isinstance(attr, dict) and attr.get("key") == "pivota_variant_id":
                    variant_id = attr.get("value")
                    break
        if not isinstance(variant_id, str) or not variant_id.strip():
            continue
        variant_id = variant_id.strip()
        merchandise = node.get("merchandise") if isinstance(node, dict) else None
        if isinstance(merchandise, dict):
            line_shipping_requirements.append(
                {
                    "variant_id": variant_id,
                    "storefront_variant_id": merchandise.get("id"),
                    "requires_shipping": (
                        bool(merchandise.get("requiresShipping"))
                        if merchandise.get("requiresShipping") is not None
                        else None
                    ),
                    "available_for_sale": (
                        bool(merchandise.get("availableForSale"))
                        if merchandise.get("availableForSale") is not None
                        else None
                    ),
                    "weight": merchandise.get("weight"),
                    "weight_unit": merchandise.get("weightUnit"),
                }
            )
        try:
            qty = int(node.get("quantity") or 0)
        except Exception:
            qty = 0
        qty_decimal = Decimal(str(qty if qty > 0 else 1))

        cost = node.get("cost") or {}
        apq = (cost.get("amountPerQuantity") or {}) if isinstance(cost, dict) else {}
        total_amount = (cost.get("totalAmount") or {}) if isinstance(cost, dict) else {}
        amount_per_quantity = _d(apq.get("amount"))
        line_total_after_discount = _d(total_amount.get("amount"))

        line_discount_total = Decimal("0.00")
        allocations = node.get("discountAllocations") or []
        if isinstance(allocations, list):
            for alloc in allocations:
                if not isinstance(alloc, dict):
                    continue
                amount = _d((alloc.get("discountedAmount") or {}).get("amount"))
                if amount <= 0:
                    continue
                line_discount_total += amount
                method = _discount_method_from_storefront_allocation(alloc)
                code = _normalize_code(alloc.get("code")) or None
                label = code or alloc.get("title") or "Shopify discount"
                discount_class = _discount_class_from_storefront_target(alloc.get("targetType") or "LINE_ITEM")
                group_key = "|".join(
                    [
                        str(method),
                        str(code or ""),
                        str(label or ""),
                        str(discount_class),
                        str(alloc.get("__typename") or ""),
                    ]
                )
                group = grouped.setdefault(
                    group_key,
                    {
                        "source": "shopify",
                        "source_ref": group_key,
                        "discount_class": discount_class,
                        "method": method,
                        "label": label,
                        "code": code,
                        "amount": Decimal("0.00"),
                        "allocations": [],
                        "metadata": {
                            "source": source,
                            "typename": alloc.get("__typename"),
                        },
                    },
                )
                group["amount"] += amount
                group["allocations"].append(
                    {
                        "target_type": "line_item",
                        "target_id": variant_id,
                        "amount": (Decimal("0.00") - amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
                    }
                )

        discount_total += line_discount_total
        if qty > 0:
            unit_effective = (line_total_after_discount / qty_decimal).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            unit_original = ((line_total_after_discount + line_discount_total) / qty_decimal).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        else:
            unit_effective = amount_per_quantity
            unit_original = amount_per_quantity
        if unit_original <= 0 and amount_per_quantity > 0:
            unit_original = amount_per_quantity
        if unit_effective <= 0 and amount_per_quantity > 0 and line_total_after_discount <= 0:
            unit_effective = amount_per_quantity

        unit_price_by_variant_id[variant_id] = unit_original
        line_pricing_by_variant_id[variant_id] = {
            "unit_price_original": unit_original,
            "unit_price_effective": unit_effective,
            "line_discount_total": line_discount_total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        }

    cart_allocations = cart.get("discountAllocations") or []
    if isinstance(cart_allocations, list):
        for alloc in cart_allocations:
            if not isinstance(alloc, dict):
                continue
            amount = _d((alloc.get("discountedAmount") or {}).get("amount"))
            if amount <= 0:
                continue
            method = _discount_method_from_storefront_allocation(alloc)
            code = _normalize_code(alloc.get("code")) or None
            label = code or alloc.get("title") or "Shopify discount"
            discount_class = _discount_class_from_storefront_cart_allocation(alloc)
            group_key = "|".join(
                [
                    "cart",
                    str(method),
                    str(code or ""),
                    str(label or ""),
                    str(discount_class),
                    str(alloc.get("__typename") or ""),
                ]
            )
            group = grouped.setdefault(
                group_key,
                {
                    "source": "shopify",
                    "source_ref": group_key,
                    "discount_class": discount_class,
                    "method": method,
                    "label": label,
                    "code": code,
                    "amount": Decimal("0.00"),
                    "allocations": [],
                    "metadata": {
                        "source": source,
                        "typename": alloc.get("__typename"),
                        "allocation_scope": "cart",
                    },
                },
            )
            group["amount"] += amount
            group["allocations"].append(
                {
                    "target_type": "shipping" if discount_class == "shipping" else "order",
                    "target_id": cart.get("id") or "cart",
                    "amount": (Decimal("0.00") - amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
                }
            )
            if discount_class == "shipping":
                shipping_discount_total += amount
            else:
                discount_total += amount

    promotion_lines: List[Dict[str, Any]] = []
    for idx, group in enumerate(grouped.values()):
        amount = _d(group.get("amount"))
        if amount <= 0:
            continue
        promotion_lines.append(
            {
                "id": f"sf_pl_{idx}",
                "source": "shopify",
                "source_ref": group.get("source_ref"),
                "discount_class": group.get("discount_class") or "product",
                "method": group.get("method") or "automatic",
                "label": group.get("label") or "Shopify discount",
                "code": group.get("code"),
                "amount": (Decimal("0.00") - amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
                "allocations": group.get("allocations") or [],
                "metadata": group.get("metadata") or {},
            }
        )

    applications = [
        {
            "id": pl.get("id"),
            "source": pl.get("source") or "shopify",
            "source_ref": pl.get("source_ref"),
            "discount_class": pl.get("discount_class"),
            "method": pl.get("method"),
            "label": pl.get("label"),
            "code": pl.get("code"),
            "amount": str(pl.get("amount")),
        }
        for pl in promotion_lines
    ]
    if promotion_lines:
        pricing_confidence = "authoritative"
    elif any(row.get("applicable") is not None for row in code_rows):
        pricing_confidence = "partial"
    elif submitted:
        pricing_confidence = "unverified"
    else:
        pricing_confidence = "authoritative"

    discount_evidence = {
        "source": source,
        "codes": code_rows,
        "applications": applications,
        "decisions": [],
        "pricing_confidence": pricing_confidence,
    }
    if line_shipping_requirements:
        discount_evidence["shipping_evidence"] = {
            "line_shipping_requirements": line_shipping_requirements,
        }
    if shipping_discount_total > 0:
        shipping_evidence = dict(discount_evidence.get("shipping_evidence") or {})
        shipping_evidence["discount_total"] = str(_d(shipping_discount_total))
        discount_evidence["shipping_evidence"] = shipping_evidence

    return {
        "unit_price_by_variant_id": unit_price_by_variant_id,
        "line_pricing_by_variant_id": line_pricing_by_variant_id,
        "promotion_lines": promotion_lines,
        "discount_codes": code_rows,
        "discount_total": discount_total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        "shipping_discount_total": shipping_discount_total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        "discount_evidence": discount_evidence,
    }


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
        url = f"https://{shop_domain}/admin/api/2025-10/storefront_access_tokens.json"
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

    def __init__(self, api_version: Optional[str] = None, timeout_seconds: float = 20.0):
        self.api_version = (api_version or os.getenv("SHOPIFY_STOREFRONT_API_VERSION") or "2026-04").strip()
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
            _log_shopify_warning("Shopify Admin GraphQL request failed", {"debug_id": debug_id, "error": str(e)})
            return {}

        if resp.status_code >= 400:
            _log_shopify_warning(
                "Shopify Admin GraphQL HTTP error",
                {
                    "debug_id": debug_id,
                    "status_code": resp.status_code,
                    "x_request_id": resp.headers.get("x-request-id"),
                },
            )
            return {}

        data = resp.json() or {}
        if data.get("errors"):
            _log_shopify_warning("Shopify Admin GraphQL errors", {"debug_id": debug_id, "errors": data.get("errors")[:3]})
            return {}
        return data.get("data") or {}

    async def _fetch_new_customer_evidence(
        self,
        *,
        shop_domain: str,
        admin_access_token: Optional[str],
        customer_email: Optional[str],
        debug_id: str,
    ) -> Dict[str, Any]:
        email = str(customer_email or "").strip()
        if not email:
            return {"source": "shopify_admin_graphql", "status": "unverified", "reason": "missing_customer_email"}
        if not admin_access_token:
            return {"source": "shopify_admin_graphql", "status": "unverified", "reason": "missing_admin_access_token"}

        query = """
query($query: String!) {
  customers(first: 1, query: $query) {
    edges {
      node {
        id
        numberOfOrders
      }
    }
  }
}
"""
        safe_email = email.replace("\\", "\\\\").replace('"', '\\"')
        data = await self._admin_graphql(
            shop_domain=shop_domain,
            admin_access_token=admin_access_token,
            query=query,
            variables={"query": f'email:"{safe_email}"'},
            debug_id=debug_id,
        )
        customers = data.get("customers") if isinstance(data, dict) else None
        edges = customers.get("edges") if isinstance(customers, dict) else None
        if not isinstance(edges, list):
            return {"source": "shopify_admin_graphql", "status": "unverified", "reason": "customer_lookup_unavailable"}
        if not edges:
            return {
                "source": "shopify_admin_graphql",
                "status": "verified",
                "new_customer": True,
                "basis": "no_shopify_customer_found",
            }
        node = (edges[0] or {}).get("node") or {}
        raw_count = node.get("numberOfOrders")
        try:
            order_count = int(raw_count or 0)
        except Exception:
            return {
                "source": "shopify_admin_graphql",
                "status": "unverified",
                "reason": "number_of_orders_unavailable",
                "customer_id": node.get("id"),
            }
        return {
            "source": "shopify_admin_graphql",
            "status": "verified",
            "new_customer": order_count == 0,
            "shopify_order_count": order_count,
            "customer_id": node.get("id"),
        }

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
                customer_email=customer_email,
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
                                customer_email=customer_email,
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

        if cart is None:
            vid = _first_item_variant_id(items or [])
            raise ShopifyPricingError(
                "SHOPIFY_PRICING_UNAVAILABLE",
                "Storefront cartCreate returned no cart",
                debug_id,
                details={
                    "shop_domain": shop_domain,
                    "variant_id": vid,
                    "item_count": len(items or []),
                },
            )

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
            line_pricing = cart.line_pricing_by_variant_id.get(vid) or {}
            unit_original = line_pricing.get("unit_price_original") or cart.unit_price_by_variant_id.get(vid) or Decimal("0.00")
            unit_effective = line_pricing.get("unit_price_effective") or unit_original
            line_discount_total = line_pricing.get("line_discount_total") or Decimal("0.00")
            line_items.append(
                {
                    "product_id": it.get("product_id"),
                    "variant_id": vid,
                    "quantity": qty,
                    "unit_price_original": unit_original,
                    "unit_price_effective": unit_effective,
                    "line_discount_total": line_discount_total,
                    "compare_at_savings": Decimal("0.00"),
                }
            )

        discount_evidence = dict(cart.discount_evidence or _empty_discount_evidence(source="shopify_storefront_cart"))
        if cart.delivery_diagnostics:
            shipping_evidence = dict(discount_evidence.get("shipping_evidence") or {})
            shipping_evidence["delivery_diagnostics"] = cart.delivery_diagnostics
            discount_evidence["shipping_evidence"] = shipping_evidence
        if customer_email:
            discount_evidence["customer_eligibility"] = await self._fetch_new_customer_evidence(
                shop_domain=shop_domain,
                admin_access_token=admin_access_token,
                customer_email=customer_email,
                debug_id=debug_id,
            )

        # Shopify's CartCost subtotal can already be net of line-level allocations.
        # Keep Pivota's quote subtotal as the pre-discount item subtotal so discount
        # totals and shipping inference do not double count product discounts.
        line_original_subtotal = _original_subtotal_from_line_items(line_items)
        pricing_subtotal = _d(line_original_subtotal) if line_original_subtotal > 0 else cart.subtotal

        pricing = {
            "subtotal": pricing_subtotal,
            "discount_total": cart.discount_total,
            "shipping_fee": Decimal("0.00"),
            "tax": cart.tax,
            "total": cart.total,
        }

        # Shipping is authoritative only when Shopify returns a selected delivery option.
        # Delta inference is default-off because it is not safe enough for the charge path.
        shipping_fee = self._derive_shipping_fee(cart)
        if shipping_fee is not None:
            pricing["shipping_fee"] = shipping_fee
            _mark_shipping_evidence(discount_evidence, status="authoritative", amount=shipping_fee)
            shipping_evidence = dict(discount_evidence.get("shipping_evidence") or {})
            gross_shipping_fee = None
            opts = cart.delivery_options or []
            chosen = cart.selected_delivery_option or (opts[0] if opts else None)
            est = chosen.get("estimatedCost") if isinstance(chosen, dict) else None
            if isinstance(est, dict) and est.get("amount") is not None:
                gross_shipping_fee = _d(est.get("amount"))
            if gross_shipping_fee is not None:
                shipping_evidence["gross_amount"] = str(gross_shipping_fee)
            if cart.shipping_discount_total > 0:
                shipping_evidence["discount_total"] = str(_d(cart.shipping_discount_total))
            discount_evidence["shipping_evidence"] = shipping_evidence
        else:
            fallback_enabled = (
                os.getenv("SHOPIFY_STOREFRONT_SHIPPING_FALLBACK_INFER", "").strip().lower()
                in {"1", "true", "yes", "on"}
            )
            inferred_shipping_fee = (
                _infer_shipping_fee_from_totals(
                    subtotal=pricing["subtotal"],
                    total=pricing["total"],
                    tax=pricing["tax"],
                    discount_total=pricing["discount_total"],
                )
                if fallback_enabled
                else None
            )
            if inferred_shipping_fee is not None:
                pricing["shipping_fee"] = inferred_shipping_fee
                _mark_shipping_evidence(
                    discount_evidence,
                    status="inferred",
                    reason="subtotal_total_delta",
                    amount=inferred_shipping_fee,
                )
            elif shipping_address:
                _mark_shipping_evidence(
                    discount_evidence,
                    status="unverified",
                    reason=_shipping_unverified_reason(discount_evidence),
                )

        debug = {
            "debug_id": debug_id,
            "engine": "shopify_storefront_cart",
            "shop_domain": shop_domain,
            "cart_id": cart.cart_id,
            "checkout_url": cart.checkout_url,
            "selected_delivery_option": cart.selected_delivery_option,
            "storefront_delivery_options_count": len(cart.delivery_options or []),
            "discount_evidence": discount_evidence,
        }

        return ShopifyPricingResult(
            engine="shopify_storefront_cart",
            engine_ref=str(cart.cart_id),
            currency=cart.currency,
            pricing=pricing,
            promotion_lines=cart.promotion_lines,
            line_items=line_items,
            delivery_options=cart.delivery_options,
            debug=debug,
            discount_evidence=discount_evidence,
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
            _log_shopify_warning("Shopify Storefront request failed", {"debug_id": debug_id, "error": str(e)})
            raise ShopifyPricingError("SHOPIFY_PRICING_UNAVAILABLE", "Storefront request failed", debug_id)

        if resp.status_code >= 400:
            _log_shopify_warning(
                "Shopify Storefront HTTP error",
                {
                    "debug_id": debug_id,
                    "status_code": resp.status_code,
                    "x_request_id": resp.headers.get("x-request-id"),
                },
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
            _log_shopify_warning("Shopify Storefront GraphQL errors", {"debug_id": debug_id, "errors": safe_errors[:5]})
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
        customer_email: Optional[str],
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
	      discountCodes { code applicable }
          discountAllocations {
            __typename
            targetType
            discountedAmount { amount currencyCode }
            ... on CartCodeDiscountAllocation { code }
            ... on CartAutomaticDiscountAllocation { title }
            ... on CartCustomDiscountAllocation { title }
          }
	      lines(first: 100) {
	        edges {
	          node {
	            id
	            quantity
	            attributes { key value }
	            merchandise {
	              ... on ProductVariant {
	                id
	                availableForSale
	                requiresShipping
	                weight
	                weightUnit
	              }
	            }
	            discountAllocations {
	              __typename
	              targetType
	              discountedAmount { amount currencyCode }
	              ... on CartCodeDiscountAllocation { code }
	              ... on CartAutomaticDiscountAllocation { title }
	              ... on CartCustomDiscountAllocation { title }
	            }
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
        legacy_cart_create = """
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
	            merchandise {
	              ... on ProductVariant {
	                id
	                availableForSale
	                requiresShipping
	                weight
	                weightUnit
	              }
	            }
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
        buyer_identity = _shopify_cart_buyer_identity_input(
            customer_email=customer_email,
            country=country,
            postal=postal,
            city=city,
            province=province,
            address1=address1,
            address2=address2,
            use_buyer_country_for_pricing=use_buyer_country_for_pricing,
        )
        if buyer_identity:
            variables["input"]["buyerIdentity"] = buyer_identity
        if country and postal:
            variables["input"]["delivery"] = {
                "addresses": [
                    _shopify_cart_selectable_address_input(
                        country=country,
                        postal=postal,
                        city=city,
                        province=province,
                        address1=address1,
                        address2=address2,
                    )
                ]
            }

        discount_schema_fallback = False
        try:
            data = await self._storefront_graphql(
                shop_domain=shop_domain,
                storefront_token=storefront_token,
                query=cart_create,
                variables=variables,
                debug_id=debug_id,
            )
        except ShopifyPricingError as e:
            if not _is_storefront_discount_query_error(e):
                raise
            discount_schema_fallback = True
            data = await self._storefront_graphql(
                shop_domain=shop_domain,
                storefront_token=storefront_token,
                query=legacy_cart_create,
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
            _log_shopify_warning(
                "Shopify Storefront cartCreate userErrors",
                {
                    "debug_id": debug_id,
                    "item_count": len(lines),
                    "variant_ids": [str(it.get("variant_id") or "").strip() for it in items or [] if str(it.get("variant_id") or "").strip()][:5],
                    "user_errors": safe_user_errors[:5],
                },
            )
            msg = (safe_user_errors[0].get("message") if safe_user_errors else None) or "cartCreate failed"
            raise ShopifyPricingError(
                "SHOPIFY_PRICING_UNAVAILABLE",
                msg,
                debug_id,
                details={"user_errors": safe_user_errors[:5]},
            )
        cart = root.get("cart") or {}
        if discount_schema_fallback and isinstance(cart, dict):
            cart["_discount_schema_fallback"] = True

        cart_id = cart.get("id") or ""
        checkout_url = cart.get("checkoutUrl") or None
        if not cart_id:
            _log_shopify_warning(
                "Shopify Storefront cartCreate returned no cart",
                {
                    "debug_id": debug_id,
                    "item_count": len(lines),
                    "variant_ids": [str(it.get("variant_id") or "").strip() for it in items or [] if str(it.get("variant_id") or "").strip()][:5],
                    "cart_keys": sorted(cart.keys())[:20] if isinstance(cart, dict) else [],
                },
            )

        parsed_discount_state = _parse_storefront_cart_discounts(
            cart=cart,
            submitted_codes=discount_codes,
            source="shopify_storefront_cart",
        )
        if discount_schema_fallback:
            evidence = dict(parsed_discount_state["discount_evidence"])
            evidence["pricing_confidence"] = "unverified"
            evidence["reason"] = "storefront_discount_schema_fallback"
            parsed_discount_state["discount_evidence"] = evidence

        unit_price_by_variant_id: Dict[str, Decimal] = dict(parsed_discount_state["unit_price_by_variant_id"])
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
            unit_price_by_variant_id = dict(parsed_discount_state["unit_price_by_variant_id"])
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
        delivery_diagnostics = None

        # Best-effort: attach a delivery address and fetch delivery options.
        if cart_id and country and postal:
            delivery_timeout_s = float(os.getenv("SHOPIFY_STOREFRONT_DELIVERY_TIMEOUT_SECONDS", "8") or "8")
            delivery_timeout_s = max(0.5, delivery_timeout_s)
            try:
                async def _delivery_work() -> tuple[
                    Optional[List[Dict[str, Any]]],
                    Optional[Dict[str, Any]],
                    Optional[StorefrontCartResult],
                    Optional[Dict[str, Any]],
                ]:
                    opts, sel, diagnostics = await self._attach_address_and_select_delivery_best_effort(
                        shop_domain=shop_domain,
                        storefront_token=storefront_token,
                        cart_id=cart_id,
                        customer_email=customer_email,
                        country=country,
                        postal=postal,
                        city=city,
                        province=province,
                        address1=address1,
                        address2=address2,
                        selected_delivery_option=selected_delivery_option,
                        use_buyer_country_for_pricing=use_buyer_country_for_pricing,
                        debug_id=debug_id,
                    )

                    # Refresh totals after delivery selection.
                    refreshed = await self._get_cart_cost(
                        shop_domain=shop_domain,
                        storefront_token=storefront_token,
                        cart_id=cart_id,
                        submitted_codes=discount_codes,
                        debug_id=debug_id,
                    )
                    return opts, sel, refreshed, diagnostics

                delivery_options, selected, refreshed, delivery_diagnostics = await asyncio.wait_for(
                    _delivery_work(), timeout=delivery_timeout_s
                )

                if refreshed:
                    subtotal = refreshed.subtotal
                    total = refreshed.total
                    tax = refreshed.tax
                    currency = refreshed.currency
                    unit_price_by_variant_id = refreshed.unit_price_by_variant_id or unit_price_by_variant_id
                    parsed_discount_state["line_pricing_by_variant_id"] = refreshed.line_pricing_by_variant_id
                    parsed_discount_state["promotion_lines"] = refreshed.promotion_lines
                    parsed_discount_state["discount_codes"] = refreshed.discount_codes
                    parsed_discount_state["discount_total"] = refreshed.discount_total
                    parsed_discount_state["discount_evidence"] = refreshed.discount_evidence
            except asyncio.TimeoutError:
                delivery_diagnostics = {"delivery_timeout": True, "timeout_seconds": delivery_timeout_s}
                _log_shopify_info(
                    "Storefront delivery options timed out; continuing without delivery selection",
                    {"debug_id": debug_id, "timeout_seconds": delivery_timeout_s},
                )
            except ShopifyPricingError as e:
                delivery_diagnostics = {
                    "delivery_error": {
                        "code": e.code,
                        "message": e.message,
                        "details": getattr(e, "details", {}) or {},
                    }
                }
                # Delivery address/options are best-effort; keep the quote usable even if
                # the Storefront schema differs across shops/versions.
                _log_shopify_info(
                    "Storefront delivery options unavailable; continuing without delivery selection",
                    {"debug_id": debug_id, "code": e.code, "message": e.message, "details": getattr(e, "details", {})},
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
            line_pricing_by_variant_id=parsed_discount_state["line_pricing_by_variant_id"],
            promotion_lines=parsed_discount_state["promotion_lines"],
            discount_codes=parsed_discount_state["discount_codes"],
            discount_total=parsed_discount_state["discount_total"],
            shipping_discount_total=parsed_discount_state["shipping_discount_total"],
            discount_evidence=parsed_discount_state["discount_evidence"],
            delivery_diagnostics=delivery_diagnostics,
        )

    async def _get_cart_cost(
        self,
        *,
        shop_domain: str,
        storefront_token: str,
        cart_id: str,
        submitted_codes: Optional[List[str]] = None,
        debug_id: str,
    ) -> Optional[StorefrontCartResult]:
        query = """
query($id: ID!) {
  cart(id: $id) {
    id
    checkoutUrl
    discountCodes { code applicable }
    discountAllocations {
      __typename
      targetType
      discountedAmount { amount currencyCode }
      ... on CartCodeDiscountAllocation { code }
      ... on CartAutomaticDiscountAllocation { title }
      ... on CartCustomDiscountAllocation { title }
    }
    lines(first: 100) {
      edges {
	        node {
	          id
	          quantity
	          attributes { key value }
	          merchandise {
	            ... on ProductVariant {
	              id
	              availableForSale
	              requiresShipping
	              weight
	              weightUnit
	            }
	          }
	          discountAllocations {
            __typename
            targetType
            discountedAmount { amount currencyCode }
            ... on CartCodeDiscountAllocation { code }
            ... on CartAutomaticDiscountAllocation { title }
            ... on CartCustomDiscountAllocation { title }
          }
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
        parsed_discount_state = _parse_storefront_cart_discounts(
            cart=cart,
            submitted_codes=submitted_codes or [],
            source="shopify_storefront_cart",
        )
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
            unit_price_by_variant_id=parsed_discount_state["unit_price_by_variant_id"],
            line_pricing_by_variant_id=parsed_discount_state["line_pricing_by_variant_id"],
            promotion_lines=parsed_discount_state["promotion_lines"],
            discount_codes=parsed_discount_state["discount_codes"],
            discount_total=parsed_discount_state["discount_total"],
            shipping_discount_total=parsed_discount_state["shipping_discount_total"],
            discount_evidence=parsed_discount_state["discount_evidence"],
        )

    async def _attach_address_and_select_delivery_best_effort(
        self,
        *,
        shop_domain: str,
        storefront_token: str,
        cart_id: str,
        customer_email: Optional[str],
        country: str,
        postal: str,
        city: Optional[str],
        province: Optional[str],
        address1: Optional[str],
        address2: Optional[str],
        selected_delivery_option: Optional[Dict[str, Any]],
        use_buyer_country_for_pricing: bool,
        debug_id: str,
    ) -> tuple[Optional[List[Dict[str, Any]]], Optional[Dict[str, Any]], Dict[str, Any]]:
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

        selected_address = _shopify_cart_selectable_address_input(
            country=country,
            postal=postal,
            city=city,
            province=province,
            address1=address1,
            address2=address2,
        )
        base_addr = selected_address["address"]["deliveryAddress"]

        add_shapes = [
            [selected_address],
            [{"address": {"deliveryAddress": base_addr}}],
            [{"deliveryAddress": base_addr}],
            [{"address": base_addr}],
        ]

        added = False
        last_error_details: Dict[str, Any] = {}
        diagnostics: Dict[str, Any] = {
            "storefront_api_version": self.api_version,
            "address_add_attempted": True,
            "address_add_succeeded": False,
            "address_add_shapes_attempted": 0,
            "delivery_groups_count": None,
            "delivery_options_count": None,
            "selected_delivery_addresses_count": None,
            "buyer_identity_update_attempted": False,
            "buyer_identity_update_succeeded": False,
        }
        for addresses in add_shapes:
            diagnostics["address_add_shapes_attempted"] = int(diagnostics["address_add_shapes_attempted"] or 0) + 1
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
                    safe_errors = [
                        {
                            "field": err.get("field"),
                            "message": err.get("message"),
                            "code": err.get("code"),
                        }
                        for err in user_errors
                        if isinstance(err, dict)
                    ]
                    last_error_details = {"user_errors": safe_errors[:5]}
                    diagnostics["last_address_add_user_errors"] = safe_errors[:5]
                    continue
                added = True
                diagnostics["address_add_succeeded"] = True
                break
            except ShopifyPricingError as e:
                last_error_details = getattr(e, "details", {}) or {}
                diagnostics["last_address_add_error"] = {
                    "code": e.code,
                    "message": e.message,
                    "details": last_error_details,
                }
                continue

        # Even if we fail to attach the address, still try to query deliveryGroups.
        # Some shops return delivery options based on buyerIdentity/country only.

        buyer_identity = _shopify_cart_buyer_identity_input(
            customer_email=customer_email,
            country=country,
            postal=postal,
            city=city,
            province=province,
            address1=address1,
            address2=address2,
            use_buyer_country_for_pricing=use_buyer_country_for_pricing,
        )
        if buyer_identity:
            update_buyer_identity = """
mutation($cartId: ID!, $buyerIdentity: CartBuyerIdentityInput!) {
  cartBuyerIdentityUpdate(cartId: $cartId, buyerIdentity: $buyerIdentity) {
    cart { id }
    userErrors { field message code }
  }
}
"""
            diagnostics["buyer_identity_update_attempted"] = True
            try:
                data = await self._storefront_graphql(
                    shop_domain=shop_domain,
                    storefront_token=storefront_token,
                    query=update_buyer_identity,
                    variables={"cartId": cart_id, "buyerIdentity": buyer_identity},
                    debug_id=debug_id,
                )
                root = (data.get("cartBuyerIdentityUpdate") or {}) if isinstance(data, dict) else {}
                user_errors = root.get("userErrors") or []
                if user_errors:
                    diagnostics["last_buyer_identity_update_user_errors"] = [
                        {
                            "field": err.get("field"),
                            "message": err.get("message"),
                            "code": err.get("code"),
                        }
                        for err in user_errors
                        if isinstance(err, dict)
                    ][:5]
                else:
                    diagnostics["buyer_identity_update_succeeded"] = True
            except ShopifyPricingError as e:
                diagnostics["last_buyer_identity_update_error"] = {
                    "code": e.code,
                    "message": e.message,
                    "details": getattr(e, "details", {}) or {},
                }

        # Query delivery options (schema varies; keep it tolerant).
        delivery_query = """
query($id: ID!) {
  cart(id: $id) {
    delivery {
      addresses(selected: true) {
        id
        selected
        oneTimeUse
      }
    }
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
        attempts = int(os.getenv("SHOPIFY_STOREFRONT_DELIVERY_OPTIONS_ATTEMPTS", "4") or "4")
        attempts = max(1, min(attempts, 8))
        retry_delay_s = float(os.getenv("SHOPIFY_STOREFRONT_DELIVERY_OPTIONS_RETRY_DELAY_SECONDS", "0.75") or "0.75")
        retry_delay_s = max(0.0, min(retry_delay_s, 3.0))

        options: List[Dict[str, Any]] = []
        for attempt in range(attempts):
            data2 = await self._storefront_graphql(
                shop_domain=shop_domain,
                storefront_token=storefront_token,
                query=delivery_query,
                variables={"id": cart_id},
                debug_id=debug_id,
            )
            cart = (data2.get("cart") or {}) if isinstance(data2, dict) else {}
            delivery = cart.get("delivery") if isinstance(cart, dict) else None
            if isinstance(delivery, dict):
                selected_addresses = delivery.get("addresses")
                if isinstance(selected_addresses, list):
                    diagnostics["selected_delivery_addresses_count"] = len(selected_addresses)
            groups = (((cart.get("deliveryGroups") or {}).get("edges")) or []) if isinstance(cart, dict) else []
            diagnostics["delivery_groups_count"] = len(groups)
            options = []
            for edge in groups:
                node = (edge or {}).get("node") or {}
                group_id = node.get("id")
                for opt in node.get("deliveryOptions") or []:
                    if not isinstance(opt, dict):
                        continue
                    options.append({**opt, "delivery_group_id": group_id})
            diagnostics["delivery_options_count"] = len(options)
            if options:
                break
            if attempt < attempts - 1 and retry_delay_s > 0:
                await asyncio.sleep(retry_delay_s)

        if not options:
            if not added and last_error_details:
                diagnostics["address_add_failure_details"] = last_error_details
            return None, None, diagnostics

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

        diagnostics["selected_delivery_option_handle"] = chosen.get("handle")
        diagnostics["selected_delivery_option_amount"] = (chosen.get("estimatedCost") or {}).get("amount")
        return options, chosen, diagnostics

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
        shipping_discount_total = _d(getattr(cart, "shipping_discount_total", Decimal("0.00")))
        return max(_d(fee - shipping_discount_total), Decimal("0.00"))
