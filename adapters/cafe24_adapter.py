from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import httpx

from models.standard_product import (
    ProductStatus,
    StandardProduct,
    StandardProductVariant,
    validate_orderable,
)


logger = logging.getLogger(__name__)

_MALL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,49}$")


def normalize_cafe24_mall_id(value: Any) -> str:
    raw = str(value or "").strip().lower()
    for suffix in (".cafe24api.com", ".cafe24.com"):
        if raw.endswith(suffix):
            raw = raw[: -len(suffix)]
            break
    return raw


def build_cafe24_api_base(mall_id: Any) -> str:
    normalized = normalize_cafe24_mall_id(mall_id)
    return f"https://{normalized}.cafe24api.com/api/v2" if normalized else ""


def build_cafe24_headers(access_token: str, api_version: Optional[str] = None) -> Dict[str, str]:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {str(access_token or '').strip()}",
    }
    version = str(api_version or "").strip()
    if version:
        headers["X-Cafe24-Api-Version"] = version
    return headers


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value)) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _parse_datetime(value: Any) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _variant_title(raw: Dict[str, Any]) -> str:
    options = raw.get("options")
    if isinstance(options, list):
        labels = []
        for option in options:
            if isinstance(option, dict):
                label = str(
                    option.get("value")
                    or option.get("option_value")
                    or option.get("name")
                    or ""
                ).strip()
                if label:
                    labels.append(label)
        if labels:
            return " / ".join(labels)
    return str(raw.get("variant_name") or raw.get("variant_code") or "Default").strip()


class Cafe24Adapter:
    """Cafe24 Admin API connection validation."""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        config = config or {}
        self.mall_id = normalize_cafe24_mall_id(config.get("mall_id"))
        self.access_token = str(config.get("access_token") or "").strip()
        self.api_version = str(config.get("api_version") or "2025-12-01").strip()

    def validate_config(self) -> Tuple[bool, Optional[str]]:
        if not self.mall_id or not _MALL_ID_RE.fullmatch(self.mall_id):
            return False, "Cafe24 mall_id format is invalid"
        if not self.access_token:
            return False, "Cafe24 access_token is required"
        return True, None

    async def test_connection(self) -> Dict[str, Any]:
        valid, error = self.validate_config()
        if not valid:
            return {"success": False, "error": error}
        url = f"{build_cafe24_api_base(self.mall_id)}/admin/products/count"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(
                    url,
                    headers=build_cafe24_headers(self.access_token, self.api_version),
                )
            if response.status_code != 200:
                return {
                    "success": False,
                    "error": f"HTTP {response.status_code}: {response.text[:200]}",
                }
            payload = response.json() if response.content else {}
            return {
                "success": True,
                "store_name": self.mall_id,
                "product_count": _as_int((payload or {}).get("count")),
            }
        except Exception as exc:
            logger.error("Cafe24 connection test failed mall_id=%s: %s", self.mall_id, exc)
            return {"success": False, "error": str(exc)}


class Cafe24ProductAdapter:
    """Cafe24 Admin Products API -> StandardProduct."""

    @staticmethod
    def convert_product(
        raw: Dict[str, Any],
        *,
        merchant_id: str,
        mall_id: str,
        currency: str = "KRW",
    ) -> StandardProduct:
        product_no = str(raw.get("product_no") or raw.get("product_code") or "").strip()
        if not product_no:
            raise ValueError("Cafe24 product is missing product_no")

        base_price = _as_float(raw.get("price"))
        variants_raw = raw.get("variants") if isinstance(raw.get("variants"), list) else []
        variants: List[StandardProductVariant] = []
        for variant_raw in variants_raw:
            if not isinstance(variant_raw, dict):
                continue
            variant_code = str(
                variant_raw.get("variant_code")
                or variant_raw.get("custom_variant_code")
                or f"{product_no}:default"
            ).strip()
            additional_amount = _as_float(variant_raw.get("additional_amount"))
            variants.append(
                StandardProductVariant(
                    id=variant_code,
                    title=_variant_title(variant_raw),
                    sku=str(variant_raw.get("custom_variant_code") or variant_code).strip(),
                    barcode=str(variant_raw.get("barcode") or "").strip() or None,
                    price=base_price + additional_amount,
                    inventory_quantity=_as_int(variant_raw.get("quantity")),
                    image_url=str(variant_raw.get("image") or "").strip() or None,
                    platform_metadata={
                        "display": variant_raw.get("display"),
                        "selling": variant_raw.get("selling"),
                        "use_inventory": variant_raw.get("use_inventory"),
                    },
                )
            )
        if not variants:
            variants.append(
                StandardProductVariant(
                    id=str(raw.get("product_code") or f"{product_no}:default"),
                    title="Default",
                    sku=str(raw.get("custom_product_code") or raw.get("product_code") or "").strip() or None,
                    price=base_price,
                    inventory_quantity=_as_int(raw.get("quantity")),
                )
            )

        display = str(raw.get("display") or "T").upper() == "T"
        selling = str(raw.get("selling") or "T").upper() == "T"
        inventory = sum(max(0, variant.inventory_quantity) for variant in variants)
        images = []
        for key in ("detail_image", "list_image", "main_image", "tiny_image"):
            image = str(raw.get(key) or "").strip()
            if image and image not in images:
                images.append(image)

        product = StandardProduct(
            id=product_no,
            platform="cafe24",
            merchant_id=merchant_id,
            title=str(raw.get("product_name") or raw.get("eng_product_name") or product_no),
            description=str(
                raw.get("description")
                or raw.get("simple_description")
                or raw.get("summary_description")
                or ""
            ),
            vendor=str(
                raw.get("brand_name")
                or raw.get("manufacturer_name")
                or raw.get("brand_code")
                or ""
            ).strip() or None,
            product_type=str(raw.get("category_name") or raw.get("product_type") or "").strip() or None,
            price=base_price,
            compare_at_price=_as_float(raw.get("retail_price")) or None,
            currency=str(raw.get("currency") or currency or "KRW").upper(),
            inventory_quantity=inventory,
            sku=variants[0].sku,
            barcode=variants[0].barcode,
            image_url=images[0] if images else None,
            images=images,
            variants=variants,
            status=ProductStatus.ACTIVE if display else ProductStatus.DRAFT,
            orderable=display and selling,
            created_at=_parse_datetime(raw.get("created_date")),
            updated_at=_parse_datetime(raw.get("updated_date")),
            online_store_url=str(raw.get("product_detail_url") or "").strip() or None,
            platform_metadata={
                "mall_id": mall_id,
                "shop_no": raw.get("shop_no"),
                "product_code": raw.get("product_code"),
                "custom_product_code": raw.get("custom_product_code"),
                "display": raw.get("display"),
                "selling": raw.get("selling"),
            },
        )
        orderable, validation = validate_orderable(product)
        product.orderable = bool(product.orderable and orderable)
        product.orderable_validation = validation
        return product

    @staticmethod
    async def fetch_products(
        mall_id: str,
        access_token: str,
        merchant_id: str,
        limit: int = 50,
        page_token: Optional[str] = None,
        shop_no: int = 1,
        currency: str = "KRW",
        api_version: str = "2025-12-01",
        refresh_token: Optional[str] = None,
        expires_at: Optional[str] = None,
        store_id: Optional[str] = None,
    ) -> Tuple[List[StandardProduct], Optional[str], Optional[str]]:
        normalized_mall_id = normalize_cafe24_mall_id(mall_id)
        if refresh_token or expires_at:
            from services.cafe24_integration_service import resolve_cafe24_access_token

            access_token = await resolve_cafe24_access_token(
                {
                    "mall_id": normalized_mall_id,
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                    "expires_at": expires_at,
                    "api_version": api_version,
                    "shop_no": shop_no,
                    "currency": currency,
                    "store_id": store_id,
                }
            )
        adapter = Cafe24Adapter(
            {
                "mall_id": normalized_mall_id,
                "access_token": access_token,
                "api_version": api_version,
            }
        )
        valid, error = adapter.validate_config()
        if not valid:
            return [], None, error

        safe_limit = max(1, min(_as_int(limit, 50), 100))
        since_product_no = max(0, _as_int(page_token, 0))
        params = {
            "shop_no": max(1, _as_int(shop_no, 1)),
            "limit": safe_limit,
            "since_product_no": since_product_no,
            "embed": "variants",
        }
        url = f"{build_cafe24_api_base(normalized_mall_id)}/admin/products"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(
                    url,
                    headers=build_cafe24_headers(access_token, api_version),
                    params=params,
                )
            if response.status_code != 200:
                return [], None, f"Cafe24 products HTTP {response.status_code}: {response.text[:200]}"
            payload = response.json() or {}
            raw_products = payload.get("products") if isinstance(payload, dict) else []
            if not isinstance(raw_products, list):
                return [], None, "Cafe24 products response is missing products[]"

            products: List[StandardProduct] = []
            for raw_product in raw_products:
                if not isinstance(raw_product, dict):
                    continue
                products.append(
                    Cafe24ProductAdapter.convert_product(
                        raw_product,
                        merchant_id=merchant_id,
                        mall_id=normalized_mall_id,
                        currency=currency,
                    )
                )
            product_numbers = [_as_int(item.get("product_no")) for item in raw_products if isinstance(item, dict)]
            next_page = str(max(product_numbers)) if len(raw_products) == safe_limit and product_numbers else None
            return products, next_page, None
        except Exception as exc:
            logger.error("Cafe24 product fetch failed mall_id=%s: %s", normalized_mall_id, exc)
            return [], None, str(exc)
