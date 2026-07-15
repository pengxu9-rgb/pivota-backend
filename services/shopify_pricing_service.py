from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Tuple

import asyncio
import hashlib
import httpx
import json
import os

from services.merchant_store_service import get_primary_store
from services.shopify_access_token_service import resolve_shopify_admin_access_token
from utils.logger import logger


def _log_shopify_warning(event: str, fields: Dict[str, Any]) -> None:
    logger.warning("%s %s", event, json.dumps(fields, sort_keys=True, default=str))


@dataclass(frozen=True)
class ShopifyPricingResult:
    engine: str
    engine_ref: str
    currency: str
    pricing: Dict[str, Decimal]
    promotion_lines: List[Dict[str, Any]]
    line_items: List[Dict[str, Any]]
    delivery_options: Optional[List[Dict[str, Any]]]
    debug: Dict[str, Any]
    discount_evidence: Optional[Dict[str, Any]] = None


class ShopifyPricingError(Exception):
    def __init__(self, code: str, message: str, debug_id: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.debug_id = debug_id
        self.message = message
        self.details = details or {}


class _VariantCompareAtCache:
    def __init__(self, ttl_seconds: int = 6 * 60 * 60, max_entries: int = 2000):
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._cache: Dict[str, Tuple[float, Optional[str]]] = {}

    def get(self, variant_id: str) -> Optional[str]:
        now = datetime.now(timezone.utc).timestamp()
        hit = self._cache.get(str(variant_id))
        if not hit:
            return None
        expires_at, value = hit
        if expires_at < now:
            self._cache.pop(str(variant_id), None)
            return None
        return value

    def set(self, variant_id: str, compare_at_price: Optional[str]) -> None:
        if len(self._cache) >= self._max_entries:
            # Simple eviction: drop an arbitrary item.
            self._cache.pop(next(iter(self._cache.keys())), None)
        now = datetime.now(timezone.utc).timestamp()
        self._cache[str(variant_id)] = (now + self._ttl_seconds, compare_at_price)


_compare_at_cache = _VariantCompareAtCache()


class ShopifyPricingService:
    """
    Pricing oracle backed by Shopify Admin REST Checkout API (deprecated on Shopify side,
    but used as P0 engine for quote-first).
    """

    def __init__(self, api_version: str = "2025-10", timeout_seconds: float = 20.0):
        self.api_version = api_version
        self.timeout_seconds = timeout_seconds

    async def preview_checkout_quote(
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
            f"{merchant_id}:{datetime.now(timezone.utc).isoformat()}".encode("utf-8")
        ).hexdigest()[:16]

        store = await get_primary_store(merchant_id)
        if not store or store.get("platform") != "shopify":
            raise ShopifyPricingError(
                "SHOPIFY_PRICING_UNAVAILABLE",
                f"Merchant {merchant_id} has no Shopify primary store",
                debug_id,
            )

        shop_domain = store.get("domain")
        access_token, _ = await resolve_shopify_admin_access_token(
            shop_domain=shop_domain,
            api_key_raw=store.get("api_key_raw") or store.get("api_key"),
            store_id=str(store.get("store_id") or "").strip() or None,
        )
        if not shop_domain or not access_token:
            raise ShopifyPricingError(
                "SHOPIFY_PRICING_UNAVAILABLE",
                f"Shopify credentials missing for merchant {merchant_id}",
                debug_id,
            )

        checkout_payload = self._build_checkout_payload(
            items=items,
            discount_codes=discount_codes,
            customer_email=customer_email,
            shipping_address=shipping_address,
        )

        url = f"https://{shop_domain}/admin/api/{self.api_version}/checkouts.json"
        headers = {
            "X-Shopify-Access-Token": access_token,
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                resp = await client.post(url, headers=headers, json=checkout_payload)
        except Exception as e:
            _log_shopify_warning(
                "Shopify checkout pricing request failed",
                {"debug_id": debug_id, "merchant_id": merchant_id, "error": str(e)},
            )
            raise ShopifyPricingError(
                "SHOPIFY_PRICING_UNAVAILABLE",
                "Failed to call Shopify pricing engine",
                debug_id,
            )

        if resp.status_code not in (200, 201):
            # Do not log response bodies here: they may include customer email/address.
            _log_shopify_warning(
                "Shopify checkout pricing error response",
                {
                    "debug_id": debug_id,
                    "merchant_id": merchant_id,
                    "status_code": resp.status_code,
                    "x_request_id": resp.headers.get("x-request-id"),
                },
            )
            hint = None
            if resp.status_code == 403:
                hint = (
                    "Admin REST Checkout API is deprecated and often blocked for custom apps "
                    "(requires write_checkouts). Prefer Storefront Cart pricing with a Storefront token."
                )
            raise ShopifyPricingError(
                "SHOPIFY_PRICING_UNAVAILABLE",
                f"Shopify pricing error (HTTP {resp.status_code})",
                debug_id,
                details={"status_code": resp.status_code, "hint": hint} if hint else {"status_code": resp.status_code},
            )

        payload = resp.json() or {}
        checkout = payload.get("checkout") or {}
        token = checkout.get("token") or ""
        # Checkout API currency can be missing or inconsistent across shops/versions.
        # When absent, derive it from the `*_price_set` money objects so we don't label
        # shop currency amounts (e.g. EUR) as USD.
        currency = (
            checkout.get("currency")
            or checkout.get("presentment_currency")
            or (checkout.get("total_price_set") or {}).get("presentment_money", {}).get("currency_code")
            or (checkout.get("subtotal_price_set") or {}).get("presentment_money", {}).get("currency_code")
            or (checkout.get("total_price_set") or {}).get("shop_money", {}).get("currency_code")
            or (checkout.get("subtotal_price_set") or {}).get("shop_money", {}).get("currency_code")
            or "USD"
        )

        pricing = self._extract_pricing(checkout)
        promotion_lines, rounding_meta = self._extract_promotion_lines(
            checkout=checkout, discount_total=pricing["discount_total"]
        )
        discount_evidence = self._build_discount_evidence(
            submitted_codes=discount_codes,
            promotion_lines=promotion_lines,
            discount_total=pricing["discount_total"],
            source="shopify_rest_checkout",
        )
        line_items = await self._extract_line_items(
            merchant_id=merchant_id,
            store=store,
            checkout=checkout,
            access_token=access_token,
        )

        delivery_options = None
        shipping_rates = checkout.get("shipping_rates")
        if isinstance(shipping_rates, list):
            delivery_options = shipping_rates

        debug = {
            "debug_id": debug_id,
            "engine": "shopify_rest_checkout",
            "shop_domain": shop_domain,
            "request_hash": hashlib.sha256(
                str(checkout_payload).encode("utf-8")
            ).hexdigest()[:16],
            "shopify_status_code": resp.status_code,
            "checkout_url": checkout.get("web_url"),
            "rounding": rounding_meta,
            "selected_delivery_option": selected_delivery_option,
        }

        return ShopifyPricingResult(
            engine="shopify_rest_checkout",
            engine_ref=str(token),
            currency=currency,
            pricing=pricing,
            promotion_lines=promotion_lines,
            line_items=line_items,
            delivery_options=delivery_options,
            debug=debug,
            discount_evidence=discount_evidence,
        )

    def _build_discount_evidence(
        self,
        *,
        submitted_codes: List[str],
        promotion_lines: List[Dict[str, Any]],
        discount_total: Decimal,
        source: str,
    ) -> Dict[str, Any]:
        applied_codes = {
            str(pl.get("code") or "").strip().upper()
            for pl in promotion_lines or []
            if str(pl.get("method") or "").lower() == "code" and str(pl.get("code") or "").strip()
        }
        codes: List[Dict[str, Any]] = []
        for code in submitted_codes or []:
            normalized = str(code or "").strip().upper()
            if not normalized:
                continue
            codes.append(
                {
                    "code": normalized,
                    "applicable": True if normalized in applied_codes else None,
                    "source": source,
                }
            )

        applications: List[Dict[str, Any]] = []
        for pl in promotion_lines or []:
            applications.append(
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
            )

        return {
            "source": source,
            "codes": codes,
            "applications": applications,
            "decisions": [],
            "pricing_confidence": "authoritative" if Decimal(str(discount_total or "0")) > 0 or not submitted_codes else "partial",
        }

    def _build_checkout_payload(
        self,
        *,
        items: List[Dict[str, Any]],
        discount_codes: List[str],
        customer_email: Optional[str],
        shipping_address: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        line_items = []
        for item in items or []:
            variant_id = item.get("variant_id")
            qty = int(item.get("quantity") or 0)
            if not variant_id or qty <= 0:
                continue
            line_items.append({"variant_id": int(variant_id), "quantity": qty})

        checkout: Dict[str, Any] = {"line_items": line_items}
        if customer_email:
            checkout["email"] = customer_email

        if shipping_address:
            # Best-effort: accept either Shopify-shaped or Pivota minimal shape.
            country = (shipping_address.get("country") or "US").strip()
            checkout["shipping_address"] = {
                "country": country,
                "zip": shipping_address.get("postal_code") or shipping_address.get("zip"),
                "city": shipping_address.get("city"),
                "province": shipping_address.get("state") or shipping_address.get("province"),
            }

        # Shopify Checkout API tends to accept only one code at a time; apply the first code.
        if discount_codes:
            checkout["discount_code"] = discount_codes[0]

        return {"checkout": checkout}

    def _extract_pricing(self, checkout: Dict[str, Any]) -> Dict[str, Decimal]:
        def d(v: Any) -> Decimal:
            try:
                return Decimal(str(v or "0")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            except Exception:
                return Decimal("0.00")

        subtotal = d(checkout.get("subtotal_price") or checkout.get("subtotal_price_set", {}).get("shop_money", {}).get("amount"))
        discount_total = d(checkout.get("total_discounts") or checkout.get("total_discounts_set", {}).get("shop_money", {}).get("amount"))
        tax = d(checkout.get("total_tax") or checkout.get("total_tax_set", {}).get("shop_money", {}).get("amount"))
        total = d(checkout.get("total_price") or checkout.get("total_price_set", {}).get("shop_money", {}).get("amount"))

        shipping_fee = d(
            checkout.get("shipping_line", {}).get("price")
            if isinstance(checkout.get("shipping_line"), dict)
            else None
        )

        # Normalize: shipping_fee should not be negative.
        if shipping_fee < 0:
            shipping_fee = Decimal("0.00")

        return {
            "subtotal": subtotal,
            "discount_total": discount_total,
            "shipping_fee": shipping_fee,
            "tax": tax,
            "total": total,
        }

    def _method_from_application(self, app: Dict[str, Any]) -> str:
        # Checkout API varies; prefer application_type when present.
        application_type = (app.get("application_type") or app.get("type") or "").lower()
        if application_type == "automatic":
            return "automatic"
        if application_type in ("discount_code", "code"):
            return "code"
        if application_type == "manual":
            return "manual_adjustment"
        if application_type == "script":
            return "app"
        # Fallback heuristic
        if app.get("code"):
            return "code"
        return "automatic"

    def _class_from_application(self, app: Dict[str, Any]) -> str:
        target_type = (app.get("target_type") or "").lower()
        if target_type in ("shipping_line", "shipping"):
            return "shipping"
        if target_type in ("line_item", "product"):
            return "product"
        return "order"

    def _extract_promotion_lines(
        self, *, checkout: Dict[str, Any], discount_total: Decimal
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        applications = checkout.get("discount_applications") or []
        # line_items[].discount_allocations: superset for allocations
        line_items = checkout.get("line_items") or []

        allocations_by_app_index: Dict[int, List[Dict[str, Any]]] = {}
        for li in line_items:
            li_variant_id = str(li.get("variant_id") or "")
            for alloc in li.get("discount_allocations") or []:
                idx = alloc.get("discount_application_index")
                if idx is None:
                    continue
                allocations_by_app_index.setdefault(int(idx), []).append(
                    {
                        "target_type": "line_item",
                        "target_id": li_variant_id,
                        "amount": Decimal(str(alloc.get("amount") or "0")),
                    }
                )

        promo_lines: List[Dict[str, Any]] = []
        for idx, app in enumerate(applications):
            allocs = allocations_by_app_index.get(idx, [])
            amount = sum((a["amount"] for a in allocs), Decimal("0"))
            label = app.get("title") or app.get("description") or "Shopify discount"
            method = self._method_from_application(app)
            discount_class = self._class_from_application(app)
            code = app.get("code") if method == "code" else None

            # If allocations missing, best-effort fallback by looking at applied_discount amounts.
            if amount == 0 and Decimal(str(discount_total or "0")) > 0:
                # don't guess per-application split; represent as order-level if this is the only app.
                if len(applications) == 1:
                    amount = Decimal(str(discount_total or "0"))

            if amount <= 0:
                continue

            promo_lines.append(
                {
                    "id": f"pl_{idx}",
                    "source": "shopify",
                    "source_ref": str(idx),
                    "discount_class": discount_class,
                    "method": method,
                    "label": label,
                    "code": code,
                    "amount": (amount * Decimal("-1")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
                    "allocations": [
                        {
                            **a,
                            "amount": (a["amount"] * Decimal("-1")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
                        }
                        for a in allocs
                    ]
                    if allocs
                    else [],
                    "metadata": {"raw": app},
                }
            )

        # Shipping discounts: best-effort from shipping_line.applied_discounts
        shipping_line = checkout.get("shipping_line") if isinstance(checkout.get("shipping_line"), dict) else None
        if shipping_line:
            applied_discounts = shipping_line.get("applied_discounts") or []
            for i, disc in enumerate(applied_discounts):
                amt = Decimal(str(disc.get("amount") or "0"))
                if amt <= 0:
                    continue
                promo_lines.append(
                    {
                        "id": f"pl_shipping_{i}",
                        "source": "shopify",
                        "source_ref": "shipping_line",
                        "discount_class": "shipping",
                        "method": self._method_from_application(disc),
                        "label": disc.get("title") or disc.get("description") or "Shipping discount",
                        "code": disc.get("code"),
                        "amount": (amt * Decimal("-1")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
                        "allocations": [
                            {
                                "target_type": "shipping",
                                "target_id": "shipping",
                                "amount": (amt * Decimal("-1")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
                            }
                        ],
                        "metadata": {"raw": disc},
                    }
                )

        # Rounding adjustment: ensure sum(promotion_lines.amount) == -discount_total (within 1 cent)
        sum_promos = sum((Decimal(str(pl.get("amount") or "0")) for pl in promo_lines), Decimal("0"))
        expected = (Decimal(str(discount_total or "0")) * Decimal("-1")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        diff = (expected - sum_promos).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        rounding_meta = {"expected": str(expected), "sum_promos": str(sum_promos), "diff": str(diff)}
        if diff.copy_abs() >= Decimal("0.01"):
            promo_lines.append(
                {
                    "id": "pl_rounding",
                    "source": "shopify",
                    "source_ref": "rounding",
                    "discount_class": "order",
                    "method": "manual_adjustment",
                    "label": "Rounding adjustment",
                    "code": None,
                    "amount": diff,
                    "allocations": [],
                    "metadata": {"reason": "sum(promotion_lines) != -discount_total"},
                }
            )
            rounding_meta["applied"] = True
        else:
            rounding_meta["applied"] = False

        return promo_lines, rounding_meta

    async def _extract_line_items(
        self,
        *,
        merchant_id: str,
        store: Dict[str, Any],
        checkout: Dict[str, Any],
        access_token: str,
    ) -> List[Dict[str, Any]]:
        shop_domain = store.get("domain")
        if not shop_domain or not access_token:
            return []

        items = checkout.get("line_items") or []
        results: List[Dict[str, Any]] = []

        # Fallback compare-at lookup only when compare_at_price missing.
        missing_compare_at: List[str] = []
        for li in items:
            if li.get("compare_at_price") in (None, "", 0, "0"):
                if li.get("variant_id"):
                    missing_compare_at.append(str(li.get("variant_id")))

        # Cap the number of fallback calls to avoid N+1 explosions.
        max_compare_at_variants = int(os.getenv("SHOPIFY_COMPARE_AT_LOOKUP_MAX_VARIANTS", "5") or "5")
        max_compare_at_variants = max(0, max_compare_at_variants)
        missing_compare_at = list(dict.fromkeys(missing_compare_at))[:max_compare_at_variants]
        compare_at_by_variant: Dict[str, Optional[str]] = {}
        if missing_compare_at:
            budget_s = float(os.getenv("SHOPIFY_COMPARE_AT_LOOKUP_BUDGET_SECONDS", "2.5") or "2.5")
            budget_s = max(0.1, budget_s)
            try:
                compare_at_by_variant = await asyncio.wait_for(
                    self._fetch_compare_at_prices(
                        shop_domain=shop_domain,
                        access_token=access_token,
                        variant_ids=missing_compare_at,
                    ),
                    timeout=budget_s,
                )
            except asyncio.TimeoutError:
                compare_at_by_variant = {}
            except Exception:
                compare_at_by_variant = {}

        def d(v: Any) -> Decimal:
            try:
                return Decimal(str(v or "0")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            except Exception:
                return Decimal("0.00")

        for li in items:
            variant_id = str(li.get("variant_id") or "")
            qty = int(li.get("quantity") or 0)
            unit_price_original = d(li.get("price"))
            alloc_total = sum((d(a.get("amount")) for a in (li.get("discount_allocations") or [])), Decimal("0.00"))
            line_discount_total = alloc_total
            if qty <= 0:
                qty = 1
            unit_price_effective = (
                (unit_price_original * Decimal(str(qty)) - line_discount_total) / Decimal(str(qty))
            ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

            compare_at_price_raw = li.get("compare_at_price")
            if compare_at_price_raw in (None, "", 0, "0"):
                compare_at_price_raw = compare_at_by_variant.get(variant_id) or _compare_at_cache.get(variant_id)

            compare_at_price = d(compare_at_price_raw)
            compare_at_savings = (compare_at_price - unit_price_original).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            if compare_at_savings < 0:
                compare_at_savings = Decimal("0.00")

            results.append(
                {
                    "product_id": str(li.get("product_id") or ""),
                    "variant_id": variant_id,
                    "quantity": qty,
                    "unit_price_original": unit_price_original,
                    "unit_price_effective": unit_price_effective,
                    "line_discount_total": line_discount_total,
                    "compare_at_savings": compare_at_savings,
                }
            )

        return results

    async def _fetch_compare_at_prices(
        self, *, shop_domain: str, access_token: str, variant_ids: List[str]
    ) -> Dict[str, Optional[str]]:
        headers = {
            "X-Shopify-Access-Token": access_token,
            "Content-Type": "application/json",
        }
        out: Dict[str, Optional[str]] = {}

        async def fetch_one(client: httpx.AsyncClient, vid: str) -> None:
            cached = _compare_at_cache.get(vid)
            if cached is not None:
                out[vid] = cached
                return
            url = f"https://{shop_domain}/admin/api/{self.api_version}/variants/{vid}.json"
            try:
                resp = await client.get(url, headers=headers)
                if resp.status_code != 200:
                    out[vid] = None
                    _compare_at_cache.set(vid, None)
                    return
                variant = (resp.json() or {}).get("variant") or {}
                cap = variant.get("compare_at_price")
                out[vid] = str(cap) if cap is not None else None
                _compare_at_cache.set(vid, out[vid])
            except Exception:
                out[vid] = None
                _compare_at_cache.set(vid, None)

        request_timeout_s = float(os.getenv("SHOPIFY_COMPARE_AT_LOOKUP_REQUEST_TIMEOUT_SECONDS", "2") or "2")
        request_timeout_s = max(0.5, request_timeout_s)
        async with httpx.AsyncClient(timeout=request_timeout_s) as client:
            for vid in variant_ids:
                await fetch_one(client, vid)

        return out
