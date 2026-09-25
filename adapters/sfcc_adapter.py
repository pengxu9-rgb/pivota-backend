from __future__ import annotations

import logging
import re
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import httpx

from models.standard_product import (
    ProductStatus,
    StandardProduct,
    StandardProductVariant,
    validate_orderable,
)


logger = logging.getLogger(__name__)
SFCC_PLATFORM = "salesforce_commerce_cloud"
SFCC_SEARCH_PAGE_LIMIT = 200
SFCC_DETAIL_BATCH_LIMIT = 24
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{1,127}$")
_SLAS_TOKEN_CACHE: Dict[Tuple[str, str, str, str], Tuple[float, str]] = {}


def normalize_sfcc_short_code(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if "://" in raw:
        raw = urlparse(raw).hostname or ""
    suffix = ".api.commercecloud.salesforce.com"
    if raw.endswith(suffix):
        raw = raw[: -len(suffix)]
    return raw.split(".", 1)[0]


def normalize_sfcc_storefront_url(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if not raw.startswith(("http://", "https://")):
        raw = f"https://{raw}"
    parsed = urlparse(raw)
    return f"https://{parsed.hostname.lower()}" if parsed.hostname else ""


def build_sfcc_api_origin(short_code: Any) -> str:
    normalized = normalize_sfcc_short_code(short_code)
    return (
        f"https://{normalized}.api.commercecloud.salesforce.com"
        if normalized
        else ""
    )


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


def _text(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("value") or value.get("default") or ""
    return str(value or "").strip()


def _tags(value: Any) -> List[str]:
    values = value if isinstance(value, list) else str(value or "").split(",")
    return [str(item).strip() for item in values if str(item).strip()]


def _inventory(raw: Dict[str, Any]) -> Tuple[int, bool, bool]:
    inventory = raw.get("inventory") if isinstance(raw.get("inventory"), dict) else {}
    quantity = max(
        0,
        _int(
            inventory.get("stockLevel")
            or inventory.get("ats")
            or raw.get("inventoryQuantity")
        ),
    )
    orderable_value = raw.get("orderable")
    if orderable_value is None:
        orderable_value = inventory.get("orderable")
    orderable = bool(orderable_value)
    # Shopper APIs can expose only the authoritative orderable boolean while
    # withholding an exact ATS count. Preserve sellability without inventing a
    # large stock number; 1 is an availability sentinel, documented in metadata.
    sentinel = bool(orderable and quantity == 0)
    if sentinel:
        quantity = 1
    return quantity, orderable, sentinel


def _image_urls(raw: Dict[str, Any]) -> List[str]:
    urls: List[str] = []
    direct = raw.get("image") if isinstance(raw.get("image"), dict) else {}
    groups = raw.get("imageGroups") or raw.get("image_groups") or []
    for item in [direct, *groups]:
        if not isinstance(item, dict):
            continue
        candidates = item.get("images") if isinstance(item.get("images"), list) else [item]
        for image in candidates:
            if not isinstance(image, dict):
                continue
            url = _text(image.get("link") or image.get("url") or image.get("src"))
            if url and url not in urls:
                urls.append(url)
    return urls


def _online_url(raw: Dict[str, Any], storefront_url: str) -> Optional[str]:
    value = _text(raw.get("link") or raw.get("url") or raw.get("c_url"))
    if not value:
        return None
    if value.startswith(("http://", "https://")):
        return value
    base = normalize_sfcc_storefront_url(storefront_url)
    return urljoin(f"{base}/", value.lstrip("/")) if base else None


async def _get_slas_token(
    client: httpx.AsyncClient,
    *,
    short_code: str,
    organization_id: str,
    site_id: str,
    client_id: str,
    client_secret: str,
) -> Tuple[Optional[str], Optional[str]]:
    key = (short_code, organization_id, site_id, client_id)
    cached = _SLAS_TOKEN_CACHE.get(key)
    if cached and cached[0] > time.monotonic():
        return cached[1], None
    url = (
        f"{build_sfcc_api_origin(short_code)}/shopper/auth/v1/organizations/"
        f"{organization_id}/oauth2/token"
    )
    try:
        response = await client.post(
            url,
            auth=httpx.BasicAuth(client_id, client_secret),
            headers={"Accept": "application/json"},
            data={"grant_type": "client_credentials", "channel_id": site_id},
        )
    except Exception as exc:
        return None, f"SFCC SLAS token request failed: {exc}"
    if response.status_code != 200:
        return None, f"SFCC SLAS token error: {response.status_code} - {response.text[:200]}"
    try:
        payload = response.json() or {}
    except Exception:
        return None, "Invalid SFCC SLAS response"
    token = _text(payload.get("access_token")) if isinstance(payload, dict) else ""
    if not token:
        return None, "Invalid SFCC SLAS response: missing access_token"
    expires_in = max(120, _int(payload.get("expires_in"), 1800))
    _SLAS_TOKEN_CACHE[key] = (time.monotonic() + expires_in - 60, token)
    return token, None


def _headers(access_token: str) -> Dict[str, str]:
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {access_token}",
        "correlation-id": str(uuid.uuid4()),
    }


class SalesforceCommerceCloudAdapter:
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        config = config or {}
        self.short_code = normalize_sfcc_short_code(config.get("short_code"))
        self.organization_id = _text(config.get("organization_id"))
        self.site_id = _text(config.get("site_id"))
        self.client_id = _text(config.get("client_id"))
        self.client_secret = _text(config.get("client_secret"))

    def validate_config(self) -> Tuple[bool, Optional[str]]:
        if not self.short_code or not _IDENTIFIER_RE.fullmatch(self.short_code):
            return False, "Salesforce Commerce Cloud short code is invalid"
        for label, value in (
            ("organization ID", self.organization_id),
            ("site ID", self.site_id),
            ("SLAS client ID", self.client_id),
        ):
            if not value or not _IDENTIFIER_RE.fullmatch(value):
                return False, f"Salesforce Commerce Cloud {label} is invalid"
        if not self.client_secret:
            return False, "Salesforce Commerce Cloud SLAS client secret is required"
        return True, None

    async def test_connection(self) -> Dict[str, Any]:
        valid, error = self.validate_config()
        if not valid:
            return {"success": False, "error": error}
        async with httpx.AsyncClient(timeout=15.0) as client:
            token, token_error = await _get_slas_token(
                client,
                short_code=self.short_code,
                organization_id=self.organization_id,
                site_id=self.site_id,
                client_id=self.client_id,
                client_secret=self.client_secret,
            )
            if token_error or not token:
                return {"success": False, "error": token_error}
            url = (
                f"{build_sfcc_api_origin(self.short_code)}/search/shopper-search/v1/"
                f"organizations/{self.organization_id}/product-search"
            )
            try:
                response = await client.get(
                    url,
                    headers=_headers(token),
                    params={"siteId": self.site_id, "limit": 1, "offset": 0},
                )
            except Exception as exc:
                return {"success": False, "error": f"SFCC product search failed: {exc}"}
        if response.status_code != 200:
            return {
                "success": False,
                "error": f"SFCC product search error: {response.status_code} - {response.text[:200]}",
            }
        try:
            payload = response.json() or {}
        except Exception:
            return {"success": False, "error": "Invalid SFCC product search response"}
        return {
            "success": True,
            "store_name": self.site_id,
            "sample_count": len(payload.get("hits") or []) if isinstance(payload, dict) else 0,
        }


class SalesforceCommerceCloudProductAdapter:
    @staticmethod
    def convert_product(
        raw: Dict[str, Any],
        *,
        merchant_id: str,
        storefront_url: str = "",
        fallback_currency: str = "USD",
    ) -> StandardProduct:
        product_id = _text(raw.get("id") or raw.get("productId"))
        if not product_id:
            raise ValueError("SFCC product is missing id")
        product_price = _float(raw.get("price"))
        currency = _text(raw.get("currency") or fallback_currency).upper()
        variants: List[StandardProductVariant] = []
        for item in raw.get("variants") or []:
            if not isinstance(item, dict):
                continue
            variant_id = _text(item.get("productId") or item.get("id"))
            if not variant_id:
                continue
            quantity, orderable, sentinel = _inventory(item)
            values = item.get("variationValues") if isinstance(item.get("variationValues"), dict) else {}
            variants.append(
                StandardProductVariant(
                    id=variant_id,
                    title=" / ".join(str(value) for value in values.values() if value) or variant_id,
                    sku=_text(item.get("c_sku") or variant_id),
                    price=_float(item.get("price"), product_price),
                    inventory_quantity=quantity,
                    options={str(key): str(value) for key, value in values.items()} or None,
                    platform_metadata={
                        "orderable": orderable,
                        "inventory_quantity_is_sentinel": sentinel,
                    },
                )
            )
        has_native_variants = bool(variants)
        product_quantity, product_orderable, product_sentinel = _inventory(raw)
        if not variants:
            variants.append(
                StandardProductVariant(
                    id=product_id,
                    title="Default",
                    sku=_text(raw.get("c_sku") or product_id),
                    price=product_price,
                    inventory_quantity=product_quantity,
                    platform_metadata={
                        "orderable": product_orderable,
                        "inventory_quantity_is_sentinel": product_sentinel,
                    },
                )
            )
        orderable_variants = [
            variant
            for variant in variants
            if bool((variant.platform_metadata or {}).get("orderable")) and variant.price > 0
        ]
        effective_orderable = (
            bool(orderable_variants) if has_native_variants else product_orderable
        )
        quantity = sum(variant.inventory_quantity for variant in variants)
        images = _image_urls(raw)
        product = StandardProduct(
            id=product_id,
            platform=SFCC_PLATFORM,
            merchant_id=merchant_id,
            title=_text(raw.get("name") or raw.get("productName") or product_id),
            description=_text(raw.get("longDescription") or raw.get("shortDescription")),
            vendor=_text(raw.get("brand")) or None,
            product_type=_text(raw.get("primaryCategoryId") or raw.get("c_productType")) or None,
            tags=_tags(raw.get("c_tags")),
            price=min([variant.price for variant in variants if variant.price > 0] or [product_price]),
            currency=currency,
            inventory_quantity=quantity,
            sku=variants[0].sku,
            image_url=images[0] if images else None,
            images=images,
            variants=variants,
            status=ProductStatus.ACTIVE,
            handle=product_id,
            online_store_url=_online_url(raw, storefront_url),
            orderable=bool(effective_orderable and orderable_variants),
            platform_metadata={
                "product_type": raw.get("type") or raw.get("productType"),
                "hit_type": raw.get("hitType"),
                "inventory_quantity_may_be_availability_sentinel": True,
            },
        )
        orderable, validation = validate_orderable(product)
        product.orderable = bool(product.orderable and orderable)
        product.orderable_validation = validation
        product.in_stock = bool(product.orderable)
        return product

    @staticmethod
    async def fetch_products(
        *,
        short_code: str,
        organization_id: str,
        site_id: str,
        client_id: str,
        client_secret: str,
        merchant_id: str,
        limit: int = 50,
        page_token: Optional[str] = None,
        currency: str = "USD",
        locale: Optional[str] = None,
        storefront_url: str = "",
    ) -> Tuple[List[StandardProduct], Optional[str], Optional[str]]:
        adapter = SalesforceCommerceCloudAdapter(
            {
                "short_code": short_code,
                "organization_id": organization_id,
                "site_id": site_id,
                "client_id": client_id,
                "client_secret": client_secret,
            }
        )
        valid, error = adapter.validate_config()
        if not valid:
            return [], None, error
        offset = max(0, _int(page_token))
        page_limit = max(1, min(_int(limit, 50), SFCC_SEARCH_PAGE_LIMIT))
        origin = build_sfcc_api_origin(adapter.short_code)
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                token, token_error = await _get_slas_token(
                    client,
                    short_code=adapter.short_code,
                    organization_id=adapter.organization_id,
                    site_id=adapter.site_id,
                    client_id=adapter.client_id,
                    client_secret=adapter.client_secret,
                )
                if token_error or not token:
                    return [], None, token_error or "SFCC SLAS token unavailable"
                headers = _headers(token)
                search_params: Dict[str, Any] = {
                    "siteId": adapter.site_id,
                    "limit": page_limit,
                    "offset": offset,
                }
                if currency:
                    search_params["currency"] = str(currency).upper()
                if locale:
                    search_params["locale"] = locale
                search = await client.get(
                    f"{origin}/search/shopper-search/v1/organizations/"
                    f"{adapter.organization_id}/product-search",
                    headers=headers,
                    params=search_params,
                )
                if search.status_code != 200:
                    return [], None, f"SFCC search error: {search.status_code} - {search.text[:200]}"
                search_payload = search.json() or {}
                hits = search_payload.get("hits") if isinstance(search_payload, dict) else None
                if not isinstance(hits, list):
                    return [], None, "Invalid SFCC search response: missing hits list"
                ids = [_text(hit.get("productId")) for hit in hits if isinstance(hit, dict)]
                ids = [product_id for product_id in ids if product_id]
                if not ids:
                    return [], None, None
                raw_products: List[Dict[str, Any]] = []
                for start in range(0, len(ids), SFCC_DETAIL_BATCH_LIMIT):
                    detail_params: Dict[str, Any] = {
                        "ids": ",".join(ids[start : start + SFCC_DETAIL_BATCH_LIMIT]),
                        "siteId": adapter.site_id,
                        "expand": "availability,images,prices,variations",
                        "allImages": "true",
                        "currency": str(currency or "USD").upper(),
                    }
                    if locale:
                        detail_params["locale"] = locale
                    details = await client.get(
                        f"{origin}/product/shopper-products/v1/organizations/"
                        f"{adapter.organization_id}/products",
                        headers=headers,
                        params=detail_params,
                    )
                    if details.status_code != 200:
                        return [], None, (
                            f"SFCC product detail error: {details.status_code} - "
                            f"{details.text[:200]}"
                        )
                    detail_payload = details.json() or {}
                    batch = detail_payload.get("data") if isinstance(detail_payload, dict) else None
                    if not isinstance(batch, list):
                        return [], None, "Invalid SFCC product response: missing data list"
                    raw_products.extend(item for item in batch if isinstance(item, dict))
            if ids and not raw_products:
                return [], None, "Invalid SFCC product response: no product details returned"
            products: List[StandardProduct] = []
            for raw in raw_products:
                if not isinstance(raw, dict):
                    continue
                try:
                    products.append(
                        SalesforceCommerceCloudProductAdapter.convert_product(
                            raw,
                            merchant_id=merchant_id,
                            storefront_url=storefront_url,
                            fallback_currency=currency,
                        )
                    )
                except Exception as exc:
                    logger.warning("Skipping invalid SFCC product id=%s: %s", raw.get("id"), exc)
            if raw_products and not products:
                return [], None, "Invalid SFCC product response: no products could be mapped"
            total = max(0, _int(search_payload.get("total")))
            next_offset = offset + len(hits)
            next_token = str(next_offset) if next_offset < total and hits else None
            return products, next_token, None
        except Exception as exc:
            return [], None, f"Failed to fetch SFCC products: {exc}"
