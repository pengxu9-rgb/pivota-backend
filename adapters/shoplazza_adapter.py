from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx

from models.standard_product import ProductStatus, StandardProduct, StandardProductVariant, validate_orderable


logger = logging.getLogger(__name__)
DEFAULT_SHOPLAZZA_API_VERSION = "2026-01"


def normalize_shoplazza_store_url(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if not raw.startswith(("http://", "https://")):
        raw = f"https://{raw}"
    parsed = urlparse(raw)
    if not parsed.hostname:
        return ""
    # Admin tokens must never be sent over plaintext, even when a merchant
    # pastes an http:// storefront URL into the connection form.
    host = parsed.hostname.lower()
    return f"https://{host}"


def build_shoplazza_api_base(store_url: Any, api_version: str = DEFAULT_SHOPLAZZA_API_VERSION) -> str:
    normalized = normalize_shoplazza_store_url(store_url)
    version = str(api_version or DEFAULT_SHOPLAZZA_API_VERSION).strip()
    return f"{normalized}/openapi/{version}" if normalized else ""


def build_shoplazza_headers(access_token: Any) -> Dict[str, str]:
    return {"Accept": "application/json", "Access-Token": str(access_token or "").strip()}


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


class ShoplazzaAdapter:
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        config = config or {}
        self.store_url = normalize_shoplazza_store_url(config.get("store_url"))
        self.access_token = str(config.get("access_token") or "").strip()
        self.api_version = str(config.get("api_version") or DEFAULT_SHOPLAZZA_API_VERSION).strip()

    def validate_config(self) -> Tuple[bool, Optional[str]]:
        if not self.store_url:
            return False, "Shoplazza store URL is invalid"
        if not self.access_token:
            return False, "Shoplazza access token is required"
        return True, None

    async def test_connection(self) -> Dict[str, Any]:
        valid, error = self.validate_config()
        if not valid:
            return {"success": False, "error": error}
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(
                    f"{build_shoplazza_api_base(self.store_url, self.api_version)}/products",
                    headers=build_shoplazza_headers(self.access_token),
                    params={"per_page": 1},
                )
            if response.status_code != 200:
                return {"success": False, "error": f"HTTP {response.status_code}: {response.text[:200]}"}
            return {"success": True, "store_name": urlparse(self.store_url).netloc}
        except Exception as exc:
            logger.error("Shoplazza connection test failed store=%s: %s", self.store_url, exc)
            return {"success": False, "error": str(exc)}


class ShoplazzaProductAdapter:
    @staticmethod
    def convert_product(raw: Dict[str, Any], *, merchant_id: str, store_url: str, currency: str = "USD") -> StandardProduct:
        product_id = str(raw.get("id") or "").strip()
        if not product_id:
            raise ValueError("Shoplazza product is missing id")
        variants: List[StandardProductVariant] = []
        for item in raw.get("variants") or []:
            if not isinstance(item, dict):
                continue
            variant_id = str(item.get("id") or item.get("sku") or "").strip()
            if not variant_id:
                continue
            options = {
                f"Option {index}": str(item.get(f"option{index}")).strip()
                for index in range(1, 4)
                if str(item.get(f"option{index}") or "").strip()
            }
            image = item.get("image") if isinstance(item.get("image"), dict) else {}
            variants.append(StandardProductVariant(
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
            ))
        if not variants:
            raise ValueError("Shoplazza product has no usable variants")
        published = bool(raw.get("published"))
        available = bool(raw.get("available", True))
        inventory_tracking = bool(raw.get("inventory_tracking"))
        inventory_policy = str(raw.get("inventory_policy") or "deny").lower()
        inventory = sum(max(0, variant.inventory_quantity) for variant in variants)
        prices = [variant.price for variant in variants if variant.price > 0]
        sellable = bool(prices and available and (inventory > 0 or not inventory_tracking or inventory_policy == "continue"))
        images = [str(item.get("src") or "").strip() for item in raw.get("images") or [] if isinstance(item, dict) and str(item.get("src") or "").strip()]
        primary = raw.get("primary_image") if isinstance(raw.get("primary_image"), dict) else {}
        primary_url = str(primary.get("src") or "").strip()
        if primary_url and primary_url not in images:
            images.insert(0, primary_url)
        online = str(raw.get("url") or "").strip() or None
        if online and online.startswith("/"):
            online = f"{normalize_shoplazza_store_url(store_url)}{online}"
        product = StandardProduct(
            id=product_id,
            platform="shoplazza",
            merchant_id=merchant_id,
            title=str(raw.get("title") or product_id),
            description=str(raw.get("description") or raw.get("brief") or ""),
            vendor=str(raw.get("vendor") or raw.get("brand") or "").strip() or None,
            product_type=str(raw.get("product_type") or "").strip() or None,
            tags=[str(tag).strip() for tag in raw.get("tags") or [] if str(tag).strip()],
            price=min(prices) if prices else 0.0,
            currency=str(currency or "USD").upper(),
            inventory_quantity=inventory,
            sku=variants[0].sku,
            barcode=variants[0].barcode,
            image_url=images[0] if images else None,
            images=images,
            variants=variants,
            status=ProductStatus.ACTIVE if published else ProductStatus.DRAFT,
            published_at=_datetime(raw.get("published_at")),
            created_at=_datetime(raw.get("created_at")),
            updated_at=_datetime(raw.get("updated_at")),
            handle=str(raw.get("handle") or "").strip() or None,
            online_store_url=online,
            orderable=bool(published and sellable),
            platform_metadata={"spu": raw.get("spu"), "inventory_policy": inventory_policy},
        )
        orderable, validation = validate_orderable(product)
        product.orderable = bool(product.orderable and orderable)
        product.orderable_validation = validation
        product.in_stock = bool(product.orderable and (inventory > 0 or sellable))
        return product

    @staticmethod
    async def fetch_products(
        *,
        store_url: str,
        access_token: str,
        merchant_id: str,
        limit: int = 50,
        page_token: Optional[str] = None,
        api_version: str = DEFAULT_SHOPLAZZA_API_VERSION,
        currency: str = "USD",
    ) -> Tuple[List[StandardProduct], Optional[str], Optional[str]]:
        normalized = normalize_shoplazza_store_url(store_url)
        if not normalized or not str(access_token or "").strip():
            return [], None, "Shoplazza credentials incomplete"
        params: Dict[str, Any] = {"per_page": max(1, min(_int(limit, 50), 250))}
        if page_token:
            params["cursor"] = page_token
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(
                    f"{build_shoplazza_api_base(normalized, api_version)}/products",
                    headers=build_shoplazza_headers(access_token),
                    params=params,
                )
            if response.status_code != 200:
                return [], None, f"Shoplazza API error: {response.status_code} - {response.text[:200]}"
            payload = response.json() or {}
            data = payload.get("data") if isinstance(payload, dict) else None
            raw_products = data.get("products") if isinstance(data, dict) else None
            if not isinstance(raw_products, list):
                return [], None, "Invalid Shoplazza response: missing products list"
            products = []
            for raw in raw_products:
                if not isinstance(raw, dict):
                    continue
                try:
                    products.append(ShoplazzaProductAdapter.convert_product(raw, merchant_id=merchant_id, store_url=normalized, currency=currency))
                except Exception as exc:
                    logger.warning("Skipping invalid Shoplazza product id=%s: %s", raw.get("id"), exc)
            if raw_products and not products:
                return [], None, "Invalid Shoplazza response: no products could be mapped"
            cursor = str(data.get("cursor") or "").strip() or None
            return products, cursor, None
        except Exception as exc:
            return [], None, f"Failed to fetch Shoplazza products: {exc}"
