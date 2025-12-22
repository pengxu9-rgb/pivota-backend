from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import hashlib
import json
import os
import secrets

from db.quotes import compute_expires_at, expire_quote_if_needed, get_quote, insert_quote, mark_quote_consumed
from services.shopify_pricing_service import ShopifyPricingError, ShopifyPricingService
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
    snapshot_json: Dict[str, Any]
    debug_id: Optional[str]


class QuoteError(Exception):
    def __init__(self, code: str, message: str, *, debug_id: Optional[str] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.debug_id = debug_id


class QuoteService:
    def __init__(self):
        self.ttl_seconds = int(os.getenv("QUOTE_TTL_SECONDS", "600"))
        self.pricing = ShopifyPricingService()

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

        try:
            result = await self.pricing.preview_checkout_quote(
                merchant_id=merchant_id,
                items=_normalize_items_for_fingerprint(items),
                discount_codes=codes,
                customer_email=customer_email,
                shipping_address=_normalize_shipping_for_fingerprint(shipping_address),
                selected_delivery_option=selected_delivery_option,
            )
        except ShopifyPricingError as e:
            raise QuoteError(e.code, e.message, debug_id=e.debug_id)

        snapshot_json: Dict[str, Any] = {
            "engine": "shopify_rest_checkout",
            "engine_ref": result.engine_ref,
            "currency": result.currency,
            "pricing": {
                "subtotal": str(result.pricing["subtotal"]),
                "discount_total": str(result.pricing["discount_total"]),
                "shipping_fee": str(result.pricing["shipping_fee"]),
                "tax": str(result.pricing["tax"]),
                "total": str(result.pricing["total"]),
            },
            "promotion_lines": self._serialize_promotion_lines(result.promotion_lines),
            "line_items": self._serialize_line_items(result.line_items),
            "delivery_options": result.delivery_options,
            "metadata": {
                **(result.debug or {}),
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
        }

        now = datetime.now(timezone.utc)
        await insert_quote(
            {
                "quote_id": quote_id,
                "merchant_id": merchant_id,
                "agent_id": agent_id,
                "engine": "shopify_rest_checkout",
                "engine_ref": str(result.engine_ref),
                "request_fingerprint": fingerprint,
                "request_json": request_json,
                "snapshot_json": snapshot_json,
                "status": "active",
                "expires_at": expires_at,
                "consumed_at": None,
                "created_at": now,
                "updated_at": now,
                "debug_id": (result.debug or {}).get("debug_id"),
                "notes": None,
            }
        )

        return {
            "quote_id": quote_id,
            "expires_at": expires_at,
            "engine": "shopify_rest_checkout",
            "engine_ref": str(result.engine_ref),
            "currency": result.currency,
            "pricing": result.pricing,
            "promotion_lines": result.promotion_lines,
            "line_items": result.line_items,
            "delivery_options": result.delivery_options,
        }

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
        status = row.get("status")

        if status == "consumed":
            raise QuoteError("QUOTE_CONSUMED", "Quote already consumed", debug_id=row.get("debug_id"))
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
            snapshot_json=row.get("snapshot_json") or {},
            debug_id=row.get("debug_id"),
        )

    async def consume_quote_best_effort(self, quote_id: str) -> None:
        try:
            await mark_quote_consumed(quote_id)
        except Exception as e:
            logger.warning({"quote_id": quote_id, "error": str(e)}, "Failed to consume quote (best-effort)")

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
