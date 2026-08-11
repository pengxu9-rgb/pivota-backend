from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Tuple

import asyncio
import hashlib
import json
import os
import secrets
import time

from db.quotes import compute_expires_at, expire_quote_if_needed, get_quote, insert_quote, mark_quote_consumed
from services.pcs_hash import sha256_json
from services.payment_offer_evidence_service import (
    PaymentOfferTarget,
    empty_payment_offer_evidence,
    emit_payment_offer_analytics_event,
    payment_pricing_summary,
    resolve_payment_offer_evidence,
)
from services.savings_presentation_service import build_savings_presentation
from services.shopify_pricing_service import ShopifyPricingError
from services.shopify_storefront_pricing_service import ShopifyStorefrontPricingService
from utils.logger import logger


def normalize_discount_codes(codes: Optional[List[str]]) -> List[str]:
    if not codes:
        return []
    out: List[str] = []
    for c in codes:
        if c is None:
            continue
        v = str(c).strip().upper()
        if not v:
            continue
        out.append(v)
    # stable + de-dup
    return sorted(list(dict.fromkeys(out)))


def normalize_items_for_fingerprint(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return _normalize_items_for_fingerprint(items)


def normalize_shipping_for_fingerprint(addr: Optional[Dict[str, Any]]) -> Optional[Dict[str, str]]:
    return _normalize_shipping_for_fingerprint(addr)


def _normalize_shipping_for_fingerprint(addr: Optional[Dict[str, Any]]) -> Optional[Dict[str, str]]:
    if not addr or not isinstance(addr, dict):
        return None
    country = (addr.get("country") or "").strip().upper()
    postal = (addr.get("postal_code") or addr.get("zip") or "").strip().upper()
    city = (addr.get("city") or "").strip()
    state = (addr.get("state") or addr.get("province") or "").strip().upper()
    normalized = {"country": country, "postal_code": postal, "city": city, "state": state}
    # If all empty, return None
    if not any(normalized.values()):
        return None
    return normalized


def _normalize_items_for_fingerprint(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    # Combine duplicates and sort for stable fingerprinting.
    combined: Dict[Tuple[str, str], int] = {}
    for it in items or []:
        product_id = str(it.get("product_id") or "").strip()
        variant_id = str(it.get("variant_id") or "").strip()
        qty = int(it.get("quantity") or 0)
        if not product_id or not variant_id or qty <= 0:
            continue
        key = (product_id, variant_id)
        combined[key] = combined.get(key, 0) + qty
    normalized = [
        {"product_id": k[0], "variant_id": k[1], "quantity": combined[k]}
        for k in combined.keys()
    ]
    normalized.sort(key=lambda x: (x["product_id"], x["variant_id"]))
    return normalized


def compute_request_fingerprint(
    *,
    merchant_id: str,
    items: List[Dict[str, Any]],
    discount_codes: List[str],
    shipping_address: Optional[Dict[str, Any]],
    selected_delivery_option: Optional[Dict[str, Any]],
) -> str:
    payload = {
        "merchant_id": str(merchant_id),
        "items": _normalize_items_for_fingerprint(items),
        "discount_codes": normalize_discount_codes(discount_codes),
        "shipping_address": _normalize_shipping_for_fingerprint(shipping_address),
        "selected_delivery_option": selected_delivery_option or None,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class QuoteSnapshot:
    quote_id: str
    merchant_id: str
    agent_id: Optional[str]
    expires_at: datetime
    status: str
    engine: str
    engine_ref: str
    request_fingerprint: str
    request_json: Dict[str, Any]
    snapshot_json: Dict[str, Any]
    quote_hash_sha256: Optional[str]
    debug_id: Optional[str]
    created_at: Optional[datetime] = None


class QuoteError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        debug_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.debug_id = debug_id
        self.details = details or {}


class QuoteService:
    def __init__(self):
        self.ttl_seconds = int(os.getenv("QUOTE_TTL_SECONDS", "600"))
        self.pricing_storefront = ShopifyStorefrontPricingService()

    async def preview_quote(
        self,
        *,
        merchant_id: str,
        agent_id: Optional[str],
        items: List[Dict[str, Any]],
        discount_codes: Optional[List[str]],
        customer_email: Optional[str],
        shipping_address: Optional[Dict[str, Any]],
        selected_delivery_option: Optional[Dict[str, Any]],
        payment_context: Optional[Any] = None,
        brief_id: Optional[str] = None,
        brief_schema_version: Optional[str] = None,
        persist: bool = True,
        emit_analytics: bool = True,
        require_persistence: bool = False,
    ) -> Dict[str, Any]:
        codes = normalize_discount_codes(discount_codes)
        fingerprint = compute_request_fingerprint(
            merchant_id=merchant_id,
            items=items,
            discount_codes=codes,
            shipping_address=shipping_address,
            selected_delivery_option=selected_delivery_option,
        )

        expires_at = compute_expires_at(self.ttl_seconds)
        quote_id = f"q_{secrets.token_hex(12)}"

        # Engine selection:
        # Prefer Storefront Cart engine when configured (Admin Checkout API requires write_checkouts
        # which is unavailable for many modern apps). Fallback to Admin Checkout for compatibility.
        normalized_items = _normalize_items_for_fingerprint(items)
        normalized_shipping = _normalize_shipping_for_fingerprint(shipping_address)

        result = None
        attempts: List[Dict[str, Any]] = []

        # Storefront Cart is the ONE pricing mainline. The former Admin-Checkout
        # fallback (ShopifyPricingService.preview_checkout_quote → Admin
        # POST /checkouts.json) required write_checkouts — a scope NO install type
        # holds by deliberate decision — so it could only ever 403, AND it shipped
        # customer_email + shipping_address to Shopify. Per the founder no-fallback
        # directive it is removed: a failed Storefront quote fails honestly here.
        # The inventory hard-fail (OUT_OF_STOCK/INSUFFICIENT_INVENTORY) is kept
        # exactly as-is (audit fix #7).
        try:
            result = await self.pricing_storefront.preview_cart_quote(
                merchant_id=merchant_id,
                items=normalized_items,
                discount_codes=codes,
                customer_email=customer_email,
                shipping_address=normalized_shipping,
                selected_delivery_option=selected_delivery_option,
            )
        except ShopifyPricingError as e:
            # Do not mask inventory rejections. Our checkout creates orders via the
            # Shopify Admin API (not Shopify checkout), so these are hard failures
            # to avoid charging buyers for unavailable items.
            if e.code in {"OUT_OF_STOCK", "INSUFFICIENT_INVENTORY"}:
                raise QuoteError(
                    e.code,
                    e.message,
                    debug_id=e.debug_id,
                    details=getattr(e, "details", {}) or {},
                )
            attempts.append(
                {
                    "engine": "shopify_storefront_cart",
                    "code": e.code,
                    "message": e.message,
                    "debug_id": e.debug_id,
                    "details": getattr(e, "details", {}) or {},
                }
            )
            raise QuoteError(
                e.code,
                e.message,
                debug_id=e.debug_id,
                details={"attempts": attempts},
            )

        if result is None:
            raise QuoteError("SHOPIFY_PRICING_UNAVAILABLE", "Pricing engine unavailable")

        self._raise_if_shipping_unverified(
            shipping_address=normalized_shipping,
            result=result,
        )

        discount_evidence = self._coerce_discount_evidence(
            getattr(result, "discount_evidence", None) or (result.debug or {}).get("discount_evidence"),
            submitted_codes=codes,
            source=result.engine,
        )
        presentment_currency = result.currency
        charge_currency = result.currency
        settlement_currency: Optional[str] = None
        payment_offer_evidence: Dict[str, Any]
        try:
            quote_targets = [
                PaymentOfferTarget(
                    target_id=str(item.get("variant_id") or item.get("product_id") or idx),
                    merchant_id=merchant_id,
                    product_id=str(item.get("product_id") or "").strip() or None,
                    variant_id=str(item.get("variant_id") or "").strip() or None,
                    amount=None,
                    currency=presentment_currency,
                )
                for idx, item in enumerate(normalized_items)
            ]
            payment_offer_evidence = await resolve_payment_offer_evidence(
                merchant_id=merchant_id,
                targets=quote_targets,
                payment_context=payment_context,
                total_amount=result.pricing.get("total"),
                currency=presentment_currency,
            )
        except Exception as e:
            payment_offer_evidence = empty_payment_offer_evidence("resolver_error")
            payment_offer_evidence["decisions"] = [
                {
                    "type": "payment_offer_resolution",
                    "reason": "resolver_error",
                    "message": str(e)[:240],
                }
            ]
        payment_pricing = payment_pricing_summary(
            evidence=payment_offer_evidence,
            checkout_total=result.pricing.get("total"),
            currency=presentment_currency,
        )
        savings_presentation = build_savings_presentation(
            pricing=result.pricing,
            currency=presentment_currency,
            promotion_lines=result.promotion_lines,
            discount_evidence=discount_evidence,
            payment_offer_evidence=payment_offer_evidence,
            payment_pricing=payment_pricing,
        )
        if emit_analytics:
            emit_payment_offer_analytics_event(
                event_type="payment_offer.displayed",
                merchant_id=merchant_id,
                surface="quote_preview",
                evidence=payment_offer_evidence,
                payment_context=payment_context,
                quote_id=quote_id,
                idempotency_key=f"payment_offer.displayed:{quote_id}",
            )

        source_updated_at = datetime.now(timezone.utc)
        snapshot_json: Dict[str, Any] = {
            "engine": result.engine,
            "engine_ref": result.engine_ref,
            "currency": presentment_currency,
            "presentment_currency": presentment_currency,
            "charge_currency": charge_currency,
            "settlement_currency": settlement_currency,
            "availability_status": "available_confirmed",
            "available_quantity": None,
            "is_final": True,
            "source_updated_at": source_updated_at.isoformat(),
            "warnings": [],
            "checkout_url": (result.debug or {}).get("checkout_url"),
            "pricing": {
                "subtotal": str(result.pricing["subtotal"]),
                "discount_total": str(result.pricing["discount_total"]),
                "shipping_fee": str(result.pricing["shipping_fee"]),
                "tax": str(result.pricing["tax"]),
                "total": str(result.pricing["total"]),
            },
            "promotion_lines": self._serialize_promotion_lines(result.promotion_lines),
            "discount_evidence": self._serialize_discount_evidence(discount_evidence),
            "payment_offer_evidence": self._json_safe(payment_offer_evidence),
            "payment_pricing": self._json_safe(payment_pricing),
            "savings_presentation": self._json_safe(savings_presentation),
            "line_items": self._serialize_line_items(result.line_items),
            "delivery_options": self._json_safe(result.delivery_options),
            "metadata": {
                **self._json_safe(result.debug or {}),
                "discount_codes": codes,
            },
        }

        request_json = {
            "merchant_id": merchant_id,
            "items": _normalize_items_for_fingerprint(items),
            "discount_codes": codes,
            # PII minimization: only store minimal address fields for fingerprint/audit.
            "shipping_address": _normalize_shipping_for_fingerprint(shipping_address),
            "selected_delivery_option": selected_delivery_option or None,
            "payment_context": (
                payment_context.model_dump(exclude_none=True)
                if hasattr(payment_context, "model_dump")
                else payment_context
            )
            or None,
        }

        now = source_updated_at
        quote_hash_sha256 = sha256_json(
            {
                "request": request_json,
                "snapshot": snapshot_json,
                "request_fingerprint": fingerprint,
            }
        )

        # Decision/Brief join key: store as metadata but do NOT include it in the quote hash.
        # (We intentionally compute quote_hash_sha256 before mutating snapshot_json here.)
        if brief_id:
            try:
                meta = snapshot_json.get("metadata") if isinstance(snapshot_json.get("metadata"), dict) else {}
                meta = dict(meta or {})
                meta["brief"] = {
                    "brief_id": str(brief_id),
                    "brief_schema_version": str(brief_schema_version) if brief_schema_version else None,
                }
                snapshot_json["metadata"] = meta
            except Exception:
                pass
        if persist:
            try:
                await insert_quote(
                    {
                        "quote_id": quote_id,
                        "merchant_id": merchant_id,
                        "agent_id": agent_id,
                        "engine": result.engine,
                        "engine_ref": str(result.engine_ref),
                        "request_fingerprint": fingerprint,
                        "request_json": request_json,
                        "snapshot_json": snapshot_json,
                        "quote_hash_sha256": quote_hash_sha256,
                        "status": "active",
                        "expires_at": expires_at,
                        "consumed_at": None,
                        "created_at": now,
                        "updated_at": now,
                        "debug_id": (result.debug or {}).get("debug_id"),
                        "notes": None,
                    }
                )
            except Exception as e:
                # Fail-open: quote persistence should not take down checkout/preview flows.
                # Order create may still be gated by quote-first enforcement; ops should
                # apply migration 033_quote_first.sql if persistence is required.
                logger.warning(
                    "Failed to persist quote (best-effort)",
                    extra={"merchant_id": merchant_id, "quote_id": quote_id, "error": str(e)},
                )
                if require_persistence:
                    raise QuoteError(
                        "QUOTE_PERSISTENCE_FAILED",
                        "Unable to persist the replacement quote. Please refresh quote again.",
                        debug_id=(result.debug or {}).get("debug_id"),
                        details={"quote_id": quote_id, "error": str(e)[:500]},
                    )

        return {
            "quote_id": quote_id,
            "expires_at": expires_at,
            "engine": result.engine,
            "engine_ref": str(result.engine_ref),
            "currency": presentment_currency,
            "presentment_currency": presentment_currency,
            "charge_currency": charge_currency,
            "settlement_currency": settlement_currency,
            "availability_status": "available_confirmed",
            "available_quantity": None,
            "is_final": True,
            "source_updated_at": source_updated_at,
            "warnings": [],
            "pricing": result.pricing,
            "promotion_lines": self._serialize_promotion_lines(result.promotion_lines),
            "discount_evidence": self._serialize_discount_evidence(discount_evidence),
            "payment_offer_evidence": self._json_safe(payment_offer_evidence),
            "payment_pricing": self._json_safe(payment_pricing),
            "savings_presentation": self._json_safe(savings_presentation),
            "line_items": self._serialize_line_items(result.line_items),
            "delivery_options": self._json_safe(result.delivery_options),
            "checkout_url": (result.debug or {}).get("checkout_url"),
            "debug_id": (result.debug or {}).get("debug_id"),
            "attempts": self._json_safe(attempts or []),
        }

    async def validate_quote_snapshot_live(
        self,
        quote: QuoteSnapshot,
        *,
        customer_email: Optional[str] = None,
        create_replacement_quote_on_mismatch: bool = False,
    ) -> Dict[str, Any]:
        """
        Re-price/re-check an active quote against the source-of-truth platform just before order creation.

        This intentionally does not persist or emit analytics for the transient quote. If the live
        platform state differs from the stored quote snapshot, callers must ask the agent/buyer to
        refresh the quote instead of charging against stale commercial terms.
        """
        request_json = quote.request_json if isinstance(quote.request_json, dict) else {}
        snapshot_json = quote.snapshot_json if isinstance(quote.snapshot_json, dict) else {}

        quote_kwargs = {
            "merchant_id": quote.merchant_id,
            "agent_id": quote.agent_id,
            "items": request_json.get("items") or [],
            "discount_codes": request_json.get("discount_codes") or [],
            "customer_email": customer_email,
            "shipping_address": request_json.get("shipping_address"),
            "selected_delivery_option": request_json.get("selected_delivery_option"),
            "payment_context": request_json.get("payment_context"),
        }

        live = await self.preview_quote(
            **quote_kwargs,
            persist=False,
            emit_analytics=False,
        )

        mismatches = self._quote_snapshot_mismatches(snapshot_json=snapshot_json, live_quote=live)
        if mismatches:
            details: Dict[str, Any] = {
                "quote_id": quote.quote_id,
                "quote_expires_at": quote.expires_at.isoformat() if quote.expires_at else None,
                "live_engine": live.get("engine"),
                "live_engine_ref": live.get("engine_ref"),
                "mismatches": mismatches,
                "action": "review_repriced_quote",
                "requires_user_confirmation": True,
            }
            if create_replacement_quote_on_mismatch:
                try:
                    replacement_quote = await self.preview_quote(
                        **quote_kwargs,
                        persist=True,
                        emit_analytics=False,
                        require_persistence=True,
                    )
                    details["replacement_quote"] = self._quote_for_confirmation_response(replacement_quote)
                except QuoteError as replacement_err:
                    details["replacement_quote_error"] = {
                        "error": replacement_err.code,
                        "message": replacement_err.message,
                        "debug_id": replacement_err.debug_id,
                        "details": replacement_err.details,
                    }
                except Exception as replacement_err:
                    details["replacement_quote_error"] = {
                        "error": "REPLACEMENT_QUOTE_FAILED",
                        "message": str(replacement_err)[:500],
                    }
            raise QuoteError(
                "QUOTE_STALE_REPRICE_REQUIRED",
                "Quote changed. Review the updated quote before checkout.",
                debug_id=quote.debug_id or live.get("debug_id"),
                details=details,
            )

        return {
            "status": "validated",
            "validated_at": datetime.now(timezone.utc).isoformat(),
            "engine": live.get("engine"),
            "engine_ref": live.get("engine_ref"),
            "debug_id": live.get("debug_id"),
        }

    def _quote_for_confirmation_response(self, quote: Dict[str, Any]) -> Dict[str, Any]:
        fields = [
            "quote_id",
            "expires_at",
            "engine",
            "engine_ref",
            "currency",
            "presentment_currency",
            "charge_currency",
            "settlement_currency",
            "availability_status",
            "available_quantity",
            "is_final",
            "source_updated_at",
            "warnings",
            "pricing",
            "line_items",
            "delivery_options",
            "checkout_url",
            "debug_id",
        ]
        out = {field: quote.get(field) for field in fields if field in quote}
        out.update(
            {
                "requires_user_confirmation": True,
                "quote_required_before_purchase": True,
                "validation_authority": "pivota_live_quote",
            }
        )
        return self._json_safe(out)

    def _quote_snapshot_mismatches(
        self,
        *,
        snapshot_json: Dict[str, Any],
        live_quote: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        mismatches: List[Dict[str, Any]] = []

        snapshot_currency = str(
            snapshot_json.get("charge_currency")
            or snapshot_json.get("presentment_currency")
            or snapshot_json.get("currency")
            or ""
        ).strip().upper()
        live_currency = str(
            live_quote.get("charge_currency")
            or live_quote.get("presentment_currency")
            or live_quote.get("currency")
            or ""
        ).strip().upper()
        if snapshot_currency and live_currency and snapshot_currency != live_currency:
            mismatches.append(
                {
                    "field": "currency",
                    "quote": snapshot_currency,
                    "live": live_currency,
                }
            )

        snapshot_pricing = snapshot_json.get("pricing") if isinstance(snapshot_json.get("pricing"), dict) else {}
        live_pricing = live_quote.get("pricing") if isinstance(live_quote.get("pricing"), dict) else {}
        for field in ("subtotal", "discount_total", "shipping_fee", "tax", "total"):
            quoted = self._money_for_validation(snapshot_pricing.get(field))
            live = self._money_for_validation(live_pricing.get(field))
            if quoted != live:
                mismatches.append(
                    {
                        "field": f"pricing.{field}",
                        "quote": str(quoted),
                        "live": str(live),
                    }
                )

        snapshot_lines = self._quote_lines_for_validation(snapshot_json.get("line_items") or [])
        live_lines = self._quote_lines_for_validation(live_quote.get("line_items") or [])
        if snapshot_lines != live_lines:
            mismatches.append(
                {
                    "field": "line_items",
                    "quote": snapshot_lines,
                    "live": live_lines,
                }
            )

        return mismatches

    def _money_for_validation(self, value: Any) -> Decimal:
        try:
            return Decimal(str(value or "0")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        except Exception:
            return Decimal("0.00")

    def _quote_lines_for_validation(self, raw_lines: Any) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        if not isinstance(raw_lines, list):
            return rows
        for line in raw_lines:
            if not isinstance(line, dict):
                continue
            try:
                quantity = int(line.get("quantity") or 0)
            except Exception:
                quantity = 0
            rows.append(
                {
                    "product_id": str(line.get("product_id") or "").strip(),
                    "variant_id": str(line.get("variant_id") or "").strip(),
                    "quantity": quantity,
                    "unit_price_original": str(self._money_for_validation(line.get("unit_price_original"))),
                    "unit_price_effective": str(self._money_for_validation(line.get("unit_price_effective"))),
                    "line_discount_total": str(self._money_for_validation(line.get("line_discount_total"))),
                }
            )
        rows.sort(key=lambda row: (row["product_id"], row["variant_id"], row["quantity"]))
        return rows

    def _raise_if_shipping_unverified(
        self,
        *,
        shipping_address: Optional[Dict[str, Any]],
        result: Any,
    ) -> None:
        if not shipping_address or not isinstance(shipping_address, dict):
            return
        country = str(shipping_address.get("country") or "").strip().upper()
        postal_code = str(shipping_address.get("postal_code") or "").strip().upper()
        if not country or not postal_code:
            return
        if getattr(result, "engine", None) != "shopify_storefront_cart":
            return

        debug = getattr(result, "debug", {}) or {}
        discount_evidence = getattr(result, "discount_evidence", None)
        if not isinstance(discount_evidence, dict) and isinstance(debug, dict):
            discount_evidence = debug.get("discount_evidence")

        shipping_evidence = (
            discount_evidence.get("shipping_evidence")
            if isinstance(discount_evidence, dict)
            else None
        )
        if not isinstance(shipping_evidence, dict) and isinstance(debug, dict):
            shipping_evidence = debug.get("shipping_evidence")
        if not isinstance(shipping_evidence, dict):
            shipping_evidence = {
                "status": "authoritative" if (getattr(result, "delivery_options", None) or []) else "unverified",
                "reason": None if (getattr(result, "delivery_options", None) or []) else "shipping_rates_unavailable_for_shippable_lines",
            }

        if str(shipping_evidence.get("status") or "").strip().lower() != "unverified":
            return

        raise QuoteError(
            "QUOTE_SHIPPING_UNVERIFIED",
            "Unable to verify shipping for this address. Please update the address or try again.",
            debug_id=str(debug.get("debug_id") or "").strip() or None,
            details={"shipping_evidence": shipping_evidence},
        )

    def _coerce_discount_evidence(
        self,
        evidence: Optional[Dict[str, Any]],
        *,
        submitted_codes: List[str],
        source: str,
    ) -> Dict[str, Any]:
        out = dict(evidence or {})
        out.setdefault("source", source)
        out.setdefault("applications", [])
        out.setdefault("decisions", [])
        codes = out.get("codes")
        if not isinstance(codes, list):
            codes = []
        existing = {str((row or {}).get("code") or "").strip().upper() for row in codes if isinstance(row, dict)}
        for code in submitted_codes or []:
            normalized = str(code or "").strip().upper()
            if normalized and normalized not in existing:
                codes.append({"code": normalized, "applicable": None, "source": source})
        out["codes"] = codes
        if "pricing_confidence" not in out:
            out["pricing_confidence"] = "unverified" if submitted_codes else "authoritative"
        return out

    def _has_shopify_applied_discount(self, discount_evidence: Optional[Dict[str, Any]]) -> bool:
        if not isinstance(discount_evidence, dict):
            return False
        for app in discount_evidence.get("applications") or []:
            if not isinstance(app, dict):
                continue
            if str(app.get("source") or "shopify") != "pivota_infra":
                try:
                    if Decimal(str(app.get("amount") or "0")).copy_abs() > 0:
                        return True
                except Exception:
                    return True
        for code in discount_evidence.get("codes") or []:
            if isinstance(code, dict) and code.get("applicable") is True:
                return True
        return False

    def _has_rejected_shopify_discount_code(self, discount_evidence: Optional[Dict[str, Any]]) -> bool:
        if not isinstance(discount_evidence, dict):
            return False
        for code in discount_evidence.get("codes") or []:
            if not isinstance(code, dict):
                continue
            normalized = str(code.get("code") or "").strip()
            if normalized and code.get("applicable") is False:
                return True
        return False

    def _manual_promo_can_apply_after_shopify_code_rejected(self, cfg: Dict[str, Any]) -> bool:
        return cfg.get("canApplyWhenShopifyCodeRejected") is True or cfg.get("can_apply_when_shopify_code_rejected") is True

    def _manual_promo_can_stack_with_shopify(
        self,
        cfg: Dict[str, Any],
        discount_evidence: Optional[Dict[str, Any]] = None,
    ) -> bool:
        if cfg.get("canStackWithShopify") is not True:
            return False
        combines = cfg.get("combinesWith") or cfg.get("combines_with") or {}
        if not isinstance(combines, dict):
            return False
        discount_classes = set()
        if isinstance(discount_evidence, dict):
            for app in discount_evidence.get("applications") or []:
                if not isinstance(app, dict):
                    continue
                discount_class = str(app.get("discount_class") or "").strip().lower()
                if discount_class:
                    discount_classes.add(discount_class)
        if not discount_classes:
            return bool(
                combines.get("orderDiscounts")
                or combines.get("order_discounts")
                or combines.get("productDiscounts")
                or combines.get("product_discounts")
                or combines.get("shippingDiscounts")
                or combines.get("shipping_discounts")
            )
        for discount_class in discount_classes:
            if discount_class == "shipping":
                if not (combines.get("shippingDiscounts") or combines.get("shipping_discounts")):
                    return False
            elif discount_class == "product":
                if not (combines.get("productDiscounts") or combines.get("product_discounts")):
                    return False
            else:
                if not (combines.get("orderDiscounts") or combines.get("order_discounts")):
                    return False
        return True

    def _manual_promo_requires_shopify_new_customer_evidence(self, cfg: Dict[str, Any]) -> bool:
        if not isinstance(cfg, dict):
            return False
        for key in ("newCustomerOnly", "new_customer_only", "firstOrderOnly", "first_order_only"):
            if cfg.get(key) is True:
                return True
        eligibility = cfg.get("customerEligibility") or cfg.get("customer_eligibility") or cfg.get("eligibility")
        if isinstance(eligibility, dict):
            value = eligibility.get("type") or eligibility.get("kind") or eligibility.get("segment")
        else:
            value = eligibility
        token = str(value or "").strip().lower()
        return token in {"new_customer", "first_time_customer", "first_order", "shopify_first_order"}

    def _is_unscoped_legacy_manual_promo(self, scope: Dict[str, Any], cfg: Dict[str, Any]) -> bool:
        if not isinstance(scope, dict) or not isinstance(cfg, dict):
            return False
        if cfg.get("source"):
            return False
        if not scope.get("global"):
            return False
        explicit_scope_keys = (
            "productIds",
            "product_ids",
            "variantIds",
            "variant_ids",
            "collectionIds",
            "collection_ids",
            "brandIds",
            "brand_ids",
            "categoryIds",
            "category_ids",
            "shopifyItems",
            "shopify_items",
        )
        for key in explicit_scope_keys:
            value = scope.get(key)
            if isinstance(value, (list, tuple, set)) and len(value) > 0:
                return False
            if isinstance(value, dict) and len(value) > 0:
                return False
            if isinstance(value, str) and value.strip():
                return False
        return True

    def _has_verified_shopify_new_customer_evidence(self, discount_evidence: Optional[Dict[str, Any]]) -> bool:
        if not isinstance(discount_evidence, dict):
            return False
        evidence = discount_evidence.get("customer_eligibility")
        if not isinstance(evidence, dict):
            return False
        return evidence.get("status") == "verified" and evidence.get("new_customer") is True

    async def load_active_quote_or_raise(self, *, quote_id: str) -> QuoteSnapshot:
        row = await get_quote(quote_id)
        if not row:
            raise QuoteError("QUOTE_NOT_FOUND", "Quote not found")

        await expire_quote_if_needed(quote_id)
        row = await get_quote(quote_id) or row

        expires_at = row.get("expires_at")
        if isinstance(expires_at, str):
            try:
                expires_at = datetime.fromisoformat(expires_at)
            except Exception:
                expires_at = None
        created_at = row.get("created_at")
        if isinstance(created_at, str):
            try:
                created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            except Exception:
                created_at = None
        if isinstance(created_at, datetime) and created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        status = row.get("status")

        if status == "consumed":
            raise QuoteError(
                "QUOTE_CONSUMED",
                "Quote already consumed",
                debug_id=row.get("debug_id"),
                details={"order_id": row.get("consumed_order_id")},
            )
        if status == "expired":
            raise QuoteError("QUOTE_EXPIRED", "Quote expired", debug_id=row.get("debug_id"))
        if expires_at and isinstance(expires_at, datetime):
            if expires_at < datetime.now(timezone.utc):
                raise QuoteError("QUOTE_EXPIRED", "Quote expired", debug_id=row.get("debug_id"))

        return QuoteSnapshot(
            quote_id=row["quote_id"],
            merchant_id=row["merchant_id"],
            agent_id=row.get("agent_id"),
            expires_at=expires_at,
            status=status,
            engine=row.get("engine") or "shopify_rest_checkout",
            engine_ref=row.get("engine_ref") or "",
            request_fingerprint=row.get("request_fingerprint") or "",
            request_json=row.get("request_json") or {},
            snapshot_json=row.get("snapshot_json") or {},
            quote_hash_sha256=row.get("quote_hash_sha256"),
            debug_id=row.get("debug_id"),
            created_at=created_at,
        )

    async def consume_quote_best_effort(self, quote_id: str, *, order_id: Optional[str] = None) -> None:
        try:
            await mark_quote_consumed(quote_id, consumed_order_id=order_id)
        except Exception as e:
            logger.warning(
                "Failed to consume quote (best-effort)",
                extra={"quote_id": quote_id, "error": str(e)},
            )

    def _serialize_promotion_lines(self, lines: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out = []
        for pl in lines or []:
            out.append(
                {
                    **pl,
                    "amount": str(pl.get("amount")),
                    "allocations": [
                        {**a, "amount": str(a.get("amount"))} for a in (pl.get("allocations") or [])
                    ],
                }
            )
        return out

    def _serialize_discount_evidence(self, evidence: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if not isinstance(evidence, dict):
            return {}
        return self._json_safe(evidence)

    def _dedupe_json_objects(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            key = json.dumps(self._json_safe(row), sort_keys=True, separators=(",", ":"))
            if key in seen:
                continue
            seen.add(key)
            out.append(row)
        return out

    def _json_safe(self, value: Any) -> Any:
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, dict):
            return {k: self._json_safe(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._json_safe(v) for v in value]
        if isinstance(value, tuple):
            return [self._json_safe(v) for v in value]
        return value

    def _serialize_line_items(self, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out = []
        for li in items or []:
            out.append(
                {
                    **li,
                    "unit_price_original": str(li.get("unit_price_original")),
                    "unit_price_effective": str(li.get("unit_price_effective")),
                    "line_discount_total": str(li.get("line_discount_total")),
                    "compare_at_savings": str(li.get("compare_at_savings")),
                }
            )
        return out


def parse_decimal_money(v: Any) -> Decimal:
    try:
        return Decimal(str(v or "0"))
    except Exception:
        return Decimal("0")
