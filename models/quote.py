from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


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
    source: Literal["shopify"] = "shopify"
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


class QuotePreviewResponse(BaseModel):
    quote_id: str
    expires_at: datetime
    engine: Literal["shopify_rest_checkout"]
    engine_ref: str
    currency: str
    pricing: QuotePricing
    promotion_lines: List[PromotionLine]
    line_items: List[QuoteLineItem]
    delivery_options: Optional[List[Dict[str, Any]]] = None
