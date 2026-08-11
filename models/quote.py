from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from models.catalog import PivotPaymentContext


class QuoteItemInput(BaseModel):
    product_id: str
    variant_id: str
    quantity: int = Field(ge=1)


class QuotePreviewRequest(BaseModel):
    merchant_id: str
    items: List[QuoteItemInput]
    discount_codes: Optional[List[str]] = None
    customer_email: Optional[str] = None
    shipping_address: Optional[Dict[str, Any]] = None
    selected_delivery_option: Optional[Dict[str, Any]] = None
    payment_context: Optional[PivotPaymentContext] = None
    brief_id: Optional[str] = None
    brief_schema_version: Optional[str] = None


class QuotePricing(BaseModel):
    subtotal: Decimal
    discount_total: Decimal
    shipping_fee: Decimal
    tax: Decimal
    total: Decimal


PromotionDiscountClass = Literal["product", "order", "shipping"]
PromotionMethod = Literal["automatic", "code", "app", "manual_adjustment"]
PromotionAllocationTargetType = Literal["line_item", "shipping", "order"]


class PromotionAllocation(BaseModel):
    target_type: PromotionAllocationTargetType
    target_id: str
    amount: Decimal


class PromotionLine(BaseModel):
    id: str
    source: str = "store"
    source_ref: Optional[str] = None
    discount_class: PromotionDiscountClass
    method: PromotionMethod
    label: str
    code: Optional[str] = None
    amount: Decimal  # negative
    allocations: Optional[List[PromotionAllocation]] = None
    metadata: Optional[Dict[str, Any]] = None


class QuoteLineItem(BaseModel):
    product_id: Optional[str] = None
    variant_id: str
    quantity: int
    unit_price_original: Decimal
    unit_price_effective: Decimal
    line_discount_total: Decimal
    compare_at_savings: Decimal = Decimal("0")  # informational only; not included in discount_total


AvailabilityStatus = Literal[
    "available_confirmed",
    "available_estimated",
    "low_stock_confirmed",
    "unavailable_confirmed",
    "unknown_requires_validation",
    "stale_requires_refresh",
]


class QuotePreviewResponse(BaseModel):
    quote_id: str
    expires_at: datetime
    engine: Literal["shopify_rest_checkout", "shopify_storefront_cart"]
    engine_ref: str
    checkout_url: Optional[str] = None
    # Backward compatible currency field (historically used across clients).
    # Prefer the explicit fields below for new clients.
    currency: str
    # Phase 0: explicit currency terminology (non-MoR path).
    # - presentment_currency: platform-authoritative display currency for the quote
    # - charge_currency: currency the buyer will be charged in (currently same as presentment in non-MoR)
    # - settlement_currency: currency the merchant settles in (may be unknown at quote time)
    presentment_currency: str
    charge_currency: str
    settlement_currency: Optional[str] = None
    pricing: QuotePricing
    availability_status: AvailabilityStatus = "available_confirmed"
    available_quantity: Optional[int] = None
    is_final: bool = True
    source_updated_at: Optional[datetime] = None
    warnings: List[str] = Field(default_factory=list)
    promotion_lines: List[PromotionLine]
    discount_evidence: Optional[Dict[str, Any]] = None
    payment_offer_evidence: Optional[Dict[str, Any]] = None
    payment_pricing: Optional[Dict[str, Any]] = None
    savings_presentation: Optional[Dict[str, Any]] = None
    line_items: List[QuoteLineItem]
    delivery_options: Optional[List[Dict[str, Any]]] = None
    # Debug helpers (safe): allow clients to understand why an engine was chosen.
    debug_id: Optional[str] = None
    attempts: Optional[List[Dict[str, Any]]] = None
    # Decision/Brief join key (optional, additive)
    brief_id: Optional[str] = None
    brief_schema_version: Optional[str] = None
