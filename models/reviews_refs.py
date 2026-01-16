from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


VARIANT_ID_SENTINEL = "∅"


class ProductRef(BaseModel):
    merchant_id: str = Field(..., min_length=1)
    platform: str = Field(..., min_length=1)
    platform_product_id: str = Field(..., min_length=1)


class SkuRef(ProductRef):
    variant_id: Optional[str] = None


def make_product_key(ref: ProductRef) -> str:
    return f"{ref.merchant_id}|{ref.platform}|{ref.platform_product_id}"


def make_sku_key(ref: SkuRef) -> str:
    product_key = make_product_key(ref)
    variant = (ref.variant_id or "").strip() or VARIANT_ID_SENTINEL
    return f"{product_key}|{variant}"

