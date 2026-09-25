from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import httpx

from models.standard_product import ProductStatus, StandardProduct, StandardProductVariant, validate_orderable


logger = logging.getLogger(__name__)
DEFAULT_SHOPLINE_API_VERSION = "v20260601"
_HANDLE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def normalize_shopline_handle(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if "://" in raw:
        raw = urlparse(raw).hostname or ""
    if raw.endswith(".myshopline.com"):
        raw = raw[: -len(".myshopline.com")]
    return raw.split(".", 1)[0]


def build_shopline_domain(handle: Any) -> str:
    normalized = normalize_shopline_handle(handle)
    return f"{normalized}.myshopline.com" if normalized else ""


def build_shopline_api_base(handle: Any, api_version: str = DEFAULT_SHOPLINE_API_VERSION) -> str:
    domain = build_shopline_domain(handle)
    version = str(api_version or DEFAULT_SHOPLINE_API_VERSION).strip()
    return f"https://{domain}/admin/openapi/{version}" if domain else ""


def build_shopline_headers(access_token: Any) -> Dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json; charset=utf-8",
        "Authorization": f"Bearer {str(access_token or '').strip()}",
    }


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value)) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _datetime(value: Any) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _next_page_info(link_header: Any) -> Optional[str]:
    for part in str(link_header or "").split(","):
        if 'rel="next"' not in part and "rel=next" not in part:
            continue
        match = re.search(r"<([^>]+)>", part)
        if not match:
            continue
        value = parse_qs(urlparse(match.group(1)).query).get("page_info", [None])[0]
        if value:
            return str(value)
    return None


def _tags(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _image_urls(raw: Dict[str, Any]) -> List[str]:
    images: List[str] = []
    for item in raw.get("media") or []:
        if not isinstance(item, dict) or str(item.get("content_type") or "IMAGE").upper() != "IMAGE":
            continue
        src = str(item.get("src") or item.get("preview_image") or "").strip()
        if src and src not in images:
            images.append(src)
    for item in raw.get("images") or []:
        if isinstance(item, dict):
            src = str(item.get("src") or "").strip()
            if src and src not in images:
                images.append(src)
    for key in ("featured_media", "image"):
        item = raw.get(key)
        if isinstance(item, dict):
            src = str(item.get("src") or item.get("preview_image") or "").strip()
            if src and src not in images:
                images.insert(0, src)
    return images


class ShoplineAdapter:
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        config = config or {}
        self.handle = normalize_shopline_handle(config.get("handle") or config.get("store_domain"))
        self.access_token = str(config.get("access_token") or "").strip()
        self.api_version = str(config.get("api_version") or DEFAULT_SHOPLINE_API_VERSION).strip()

    def validate_config(self) -> Tuple[bool, Optional[str]]:
        if not self.handle or not _HANDLE_RE.fullmatch(self.handle):
            return False, "SHOPLINE store handle is invalid"
        if not self.access_token:
            return False, "SHOPLINE access token is required"
        return True, None

    async def test_connection(self) -> Dict[str, Any]:
        valid, error = self.validate_config()
        if not valid:
            return {"success": False, "error": error}
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(
                    f"{build_shopline_api_base(self.handle, self.api_version)}/products/products.json",
                    headers=build_shopline_headers(self.access_token),
                    params={"limit": 1},
                )
            if response.status_code != 200:
                return {"success": False, "error": f"HTTP {response.status_code}: {response.text[:200]}"}
            payload = response.json() or {}
            products = payload.get("products") if isinstance(payload, dict) else None
            return {
                "success": True,
                "store_name": build_shopline_domain(self.handle),
                "sample_count": len(products) if isinstance(products, list) else 0,
            }
        except Exception as exc:
            logger.error("SHOPLINE connection test failed handle=%s: %s", self.handle, exc)
            return {"success": False, "error": str(exc)}


class ShoplineProductAdapter:
    @staticmethod
    def convert_product(
        raw: Dict[str, Any],
        *,
        merchant_id: str,
        handle: str,
        currency: str = "USD",
    ) -> StandardProduct:
        product_id = str(raw.get("id") or "").strip()
        if not product_id:
            raise ValueError("SHOPLINE product is missing id")
        variants: List[StandardProductVariant] = []
        for item in raw.get("variants") or []:
            if not isinstance(item, dict):
                continue
            variant_id = str(item.get("id") or item.get("sku") or "").strip()
            if not variant_id:
                continue
            options = {
                f"Option {index}": str(item.get(f"option{index}")).strip()
                for index in range(1, 6)
                if str(item.get(f"option{index}") or "").strip()
            }
            image = item.get("image") if isinstance(item.get("image"), dict) else {}
            variants.append(
                StandardProductVariant(
                    id=variant_id,
                    title=str(item.get("title") or " / ".join(options.values()) or "Default"),
                    sku=str(item.get("sku") or "").strip() or None,
                    barcode=str(item.get("barcode") or "").strip() or None,
                    price=_float(item.get("price")),
                    compare_at_price=_float(item.get("compare_at_price")) or None,
                    inventory_quantity=max(0, _int(item.get("inventory_quantity"))),
                    weight=_float(item.get("weight")) or None,
                    weight_unit=str(item.get("weight_unit") or "").strip() or None,
                    options=options or None,
                    image_url=str(image.get("src") or "").strip() or None,
                    platform_metadata={
                        "inventory_policy": item.get("inventory_policy"),
                        "inventory_tracker": item.get("inventory_tracker"),
                    },
                )
            )
        if not variants:
            raise ValueError("SHOPLINE product has no usable variants")
        status_raw = str(raw.get("status") or "draft").lower()
        status = ProductStatus.ACTIVE if status_raw == "active" else ProductStatus.ARCHIVED if status_raw == "archived" else ProductStatus.DRAFT
        price_values = [variant.price for variant in variants if variant.price > 0]
        inventory = sum(max(0, variant.inventory_quantity) for variant in variants)
        sellable_variant = any(
            variant.price > 0
            and (
                variant.inventory_quantity > 0
                or str((variant.platform_metadata or {}).get("inventory_policy") or "").lower() == "continue"
                or (variant.platform_metadata or {}).get("inventory_tracker") is False
            )
            for variant in variants
        )
        images = _image_urls(raw)
        product_handle = str(raw.get("handle") or "").strip() or None
        path = str(raw.get("path") or "").strip()
        online_url = f"https://{build_shopline_domain(handle)}{path}" if path.startswith("/") else None
        product = StandardProduct(
            id=product_id,
            platform="shopline",
            merchant_id=merchant_id,
            title=str(raw.get("title") or product_id),
            description=str(raw.get("body_html") or raw.get("subtitle") or ""),
            vendor=str(raw.get("vendor") or "").strip() or None,
            product_type=str(raw.get("product_category") or raw.get("product_type") or "").strip() or None,
            tags=_tags(raw.get("tags")),
            price=min(price_values) if price_values else 0.0,
            currency=str(currency or "USD").upper(),
            inventory_quantity=inventory,
            sku=variants[0].sku,
            barcode=variants[0].barcode,
            image_url=images[0] if images else None,
            images=images,
            variants=variants,
            status=status,
            published_at=_datetime(raw.get("published_at")),
            created_at=_datetime(raw.get("created_at")),
            updated_at=_datetime(raw.get("updated_at")),
            handle=product_handle,
            online_store_url=online_url,
            orderable=bool(status == ProductStatus.ACTIVE and sellable_variant),
            platform_metadata={"spu": raw.get("spu"), "path": path or None},
        )
        orderable, validation = validate_orderable(product)
        product.orderable = bool(product.orderable and orderable)
        product.orderable_validation = validation
        product.in_stock = bool(product.orderable and (inventory > 0 or sellable_variant))
        return product

    @staticmethod
    async def fetch_products(
        *,
        handle: str,
        access_token: str,
        merchant_id: str,
        limit: int = 50,
        page_token: Optional[str] = None,
        api_version: str = DEFAULT_SHOPLINE_API_VERSION,
        currency: str = "USD",
    ) -> Tuple[List[StandardProduct], Optional[str], Optional[str]]:
        normalized = normalize_shopline_handle(handle)
        if not normalized or not str(access_token or "").strip():
            return [], None, "SHOPLINE credentials incomplete"
        params: Dict[str, Any] = {"limit": max(1, min(_int(limit, 50), 50))}
        if page_token:
            params["page_info"] = page_token
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(
                    f"{build_shopline_api_base(normalized, api_version)}/products/products.json",
                    headers=build_shopline_headers(access_token),
                    params=params,
                )
            if response.status_code != 200:
                return [], None, f"SHOPLINE API error: {response.status_code} - {response.text[:200]}"
            payload = response.json() or {}
            raw_products = payload.get("products") if isinstance(payload, dict) else None
            if not isinstance(raw_products, list):
                return [], None, "Invalid SHOPLINE response: missing products list"
            products = []
            for raw in raw_products:
                if not isinstance(raw, dict):
                    continue
                try:
                    products.append(ShoplineProductAdapter.convert_product(raw, merchant_id=merchant_id, handle=normalized, currency=currency))
                except Exception as exc:
                    logger.warning("Skipping invalid SHOPLINE product id=%s: %s", raw.get("id"), exc)
            if raw_products and not products:
                return [], None, "Invalid SHOPLINE response: no products could be mapped"
            return products, _next_page_info(response.headers.get("link")), None
        except Exception as exc:
            return [], None, f"Failed to fetch SHOPLINE products: {exc}"
