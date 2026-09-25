from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlparse, urlunparse

import httpx

from models.standard_product import (
    ProductStatus,
    StandardProduct,
    StandardProductVariant,
    validate_orderable,
)


logger = logging.getLogger(__name__)


def normalize_magento_store_url(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = f"https://{raw}"
    try:
        parsed = urlparse(raw)
    except ValueError:
        return ""
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        return ""
    host = parsed.hostname.lower().rstrip(".")
    if host == "localhost" or host.endswith((".localhost", ".local")):
        return ""
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and not literal.is_global:
        return ""
    return f"https://{parsed.netloc.lower()}{parsed.path.rstrip('/')}"


async def _validate_public_https_target(store_url: str) -> Tuple[str, str]:
    parsed = urlparse(store_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Magento Store URL must use HTTPS")
    loop = asyncio.get_running_loop()
    try:
        addresses = await loop.getaddrinfo(
            parsed.hostname,
            parsed.port or 443,
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise ValueError("Magento Store hostname could not be resolved") from exc
    resolved = {entry[4][0] for entry in addresses if entry and entry[4]}
    if not resolved:
        raise ValueError("Magento Store hostname could not be resolved")
    for address in resolved:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise ValueError("Magento Store resolved to an invalid address") from exc
        if not ip.is_global:
            raise ValueError("Magento Store must resolve only to public IP addresses")
    return parsed.hostname, sorted(resolved)[0]


def _pinned_https_url(url: str, address: str) -> str:
    """Connect to a validated IP while the caller supplies the original TLS SNI/Host."""
    parsed = urlparse(url)
    address_host = f"[{address}]" if ipaddress.ip_address(address).version == 6 else address
    netloc = address_host if parsed.port in (None, 443) else f"{address_host}:{parsed.port}"
    return urlunparse((parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))


def normalize_magento_store_view(value: Any) -> str:
    return str(value or "default").strip() or "default"


def build_magento_rest_base(store_url: Any, store_view_code: Any = "default") -> str:
    normalized_url = normalize_magento_store_url(store_url)
    if not normalized_url:
        return ""
    store_view = quote(normalize_magento_store_view(store_view_code), safe="")
    return f"{normalized_url}/rest/{store_view}/V1"


def build_magento_headers(access_token: Any) -> Dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {str(access_token or '').strip()}",
    }


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


def _custom_attributes(raw: Dict[str, Any]) -> Dict[str, Any]:
    attributes: Dict[str, Any] = {}
    for item in raw.get("custom_attributes") or []:
        if not isinstance(item, dict):
            continue
        code = str(item.get("attribute_code") or "").strip()
        if code:
            attributes[code] = item.get("value")
    return attributes


def _extension_attributes(raw: Dict[str, Any]) -> Dict[str, Any]:
    value = raw.get("extension_attributes")
    return dict(value) if isinstance(value, dict) else {}


def _stock_state(raw: Dict[str, Any]) -> Tuple[int, bool, bool]:
    stock = _extension_attributes(raw).get("stock_item")
    if not isinstance(stock, dict):
        return 0, False, False
    quantity = max(0, _as_int(stock.get("qty")))
    in_stock = bool(stock.get("is_in_stock"))
    manage_stock = bool(stock.get("manage_stock", True))
    orderable = in_stock and (quantity > 0 or not manage_stock)
    return quantity if manage_stock else (1 if in_stock else 0), in_stock, orderable


def _media_url(store_url: str, file_path: Any) -> Optional[str]:
    path = str(file_path or "").strip()
    if not path or path == "no_selection":
        return None
    if path.startswith(("http://", "https://")):
        return path
    return f"{store_url}/media/catalog/product/{path.lstrip('/')}"


def _product_images(raw: Dict[str, Any], store_url: str) -> List[str]:
    images: List[str] = []
    for entry in raw.get("media_gallery_entries") or []:
        if not isinstance(entry, dict) or bool(entry.get("disabled")):
            continue
        image = _media_url(store_url, entry.get("file"))
        if image and image not in images:
            images.append(image)
    attributes = _custom_attributes(raw)
    for code in ("image", "small_image", "thumbnail"):
        image = _media_url(store_url, attributes.get(code))
        if image and image not in images:
            images.append(image)
    return images


def _option_labels(parent: Dict[str, Any]) -> Dict[Tuple[str, str], str]:
    labels: Dict[Tuple[str, str], str] = {}
    options = _extension_attributes(parent).get("configurable_product_options")
    for option in options or []:
        if not isinstance(option, dict):
            continue
        code = str(option.get("attribute_code") or option.get("label") or "").strip()
        for value in option.get("values") or []:
            if not isinstance(value, dict):
                continue
            index = str(value.get("value_index") or "").strip()
            label = str(value.get("label") or index).strip()
            if code and index:
                labels[(code, index)] = label
    return labels


class MagentoAdapter:
    """Adobe Commerce PaaS/on-prem and Magento Open Source REST validation."""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        config = config or {}
        self.store_url = normalize_magento_store_url(config.get("store_url"))
        self.access_token = str(config.get("access_token") or "").strip()
        self.store_view_code = normalize_magento_store_view(config.get("store_view_code"))

    def validate_config(self) -> Tuple[bool, Optional[str]]:
        parsed = urlparse(self.store_url)
        if parsed.scheme != "https" or not parsed.netloc:
            return False, "Magento Store URL must be a public HTTPS URL without credentials"
        if not self.access_token:
            return False, "Magento Integration Access Token is required"
        return True, None

    async def test_connection(self) -> Dict[str, Any]:
        valid, error = self.validate_config()
        if not valid:
            return {"success": False, "error": error}
        try:
            tls_hostname, pinned_address = await _validate_public_https_target(self.store_url)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        base = build_magento_rest_base(self.store_url, self.store_view_code)
        pinned_base = _pinned_https_url(base, pinned_address)
        headers = {
            **build_magento_headers(self.access_token),
            "Host": urlparse(self.store_url).netloc,
        }
        params = {
            "searchCriteria[pageSize]": 1,
            "searchCriteria[currentPage]": 1,
        }
        try:
            async with httpx.AsyncClient(
                timeout=15.0,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = await client.get(
                    f"{pinned_base}/products",
                    headers=headers,
                    params=params,
                    extensions={"sni_hostname": tls_hostname},
                )
                config_response = await client.get(
                    f"{pinned_base}/store/storeConfigs",
                    headers=headers,
                    extensions={"sni_hostname": tls_hostname},
                )
            if response.status_code != 200:
                return {
                    "success": False,
                    "error": f"Magento API returned HTTP {response.status_code}",
                }
            payload = response.json() or {}
            store_config: Dict[str, Any] = {}
            if config_response.status_code == 200:
                config_payload = config_response.json() or []
                if isinstance(config_payload, list):
                    candidates = [item for item in config_payload if isinstance(item, dict)]
                    store_config = next(
                        (
                            item
                            for item in candidates
                            if str(item.get("code") or "") == self.store_view_code
                        ),
                        candidates[0] if candidates else {},
                    )
            return {
                "success": True,
                "store_name": str(store_config.get("name") or "").strip()
                or urlparse(self.store_url).netloc,
                "product_count": _as_int(payload.get("total_count")),
                "currency": str(
                    store_config.get("base_currency_code")
                    or store_config.get("default_display_currency_code")
                    or ""
                ).upper()
                or None,
                "product_url_suffix": store_config.get("product_url_suffix"),
            }
        except Exception:
            logger.exception(
                "Magento connection test failed host=%s",
                urlparse(self.store_url).hostname,
            )
            return {"success": False, "error": "Magento connection request failed"}


class MagentoProductAdapter:
    """Magento/Adobe Commerce REST catalog -> StandardProduct."""

    @staticmethod
    def _variant(
        raw: Dict[str, Any],
        *,
        store_url: str,
        label_map: Dict[Tuple[str, str], str],
    ) -> StandardProductVariant:
        attributes = _custom_attributes(raw)
        options: Dict[str, str] = {}
        for code in ("color", "size"):
            value = str(attributes.get(code) or "").strip()
            if value:
                options[code.title()] = label_map.get((code, value), value)
        quantity, _, _ = _stock_state(raw)
        images = _product_images(raw, store_url)
        sku = str(raw.get("sku") or raw.get("id") or "").strip()
        return StandardProductVariant(
            id=str(raw.get("id") or sku),
            title=" / ".join(options.values()) or sku or "Default",
            sku=sku or None,
            price=_as_float(raw.get("price")),
            inventory_quantity=quantity,
            weight=_as_float(raw.get("weight")) or None,
            options=options or None,
            image_url=images[0] if images else None,
            platform_metadata={"type_id": raw.get("type_id")},
        )

    @staticmethod
    def convert_product(
        raw: Dict[str, Any],
        *,
        merchant_id: str,
        store_url: str,
        currency: str = "USD",
        children: Optional[List[Dict[str, Any]]] = None,
        product_url_suffix: Optional[str] = None,
    ) -> StandardProduct:
        product_id = str(raw.get("id") or raw.get("sku") or "").strip()
        if not product_id:
            raise ValueError("Magento product is missing id/sku")
        attributes = _custom_attributes(raw)
        label_map = _option_labels(raw)
        variants = [
            MagentoProductAdapter._variant(
                child,
                store_url=store_url,
                label_map=label_map,
            )
            for child in (children or [])
            if isinstance(child, dict)
        ]
        if not variants:
            variants = [
                MagentoProductAdapter._variant(
                    raw,
                    store_url=store_url,
                    label_map=label_map,
                )
            ]
        own_quantity, own_in_stock, own_orderable = _stock_state(raw)
        inventory_quantity = (
            sum(max(0, variant.inventory_quantity) for variant in variants)
            if children
            else own_quantity
        )
        price_candidates = [variant.price for variant in variants if variant.price > 0]
        price = min(price_candidates) if price_candidates else _as_float(raw.get("price"))
        enabled = _as_int(raw.get("status"), 1) == 1
        visibility = _as_int(raw.get("visibility"))
        orderable = bool(
            enabled
            and price > 0
            and visibility != 1
            and (
                any(variant.inventory_quantity > 0 for variant in variants)
                if children
                else own_orderable
            )
        )
        images = _product_images(raw, store_url)
        url_key = str(attributes.get("url_key") or "").strip() or None
        raw_url_suffix = attributes.get("url_suffix")
        if raw_url_suffix is None:
            raw_url_suffix = product_url_suffix
        url_suffix = str(raw_url_suffix or "").strip()
        online_store_url = (
            f"{store_url}/{url_key}{url_suffix}"
            if url_key and raw_url_suffix is not None
            else None
        )
        product = StandardProduct(
            id=product_id,
            platform="magento",
            merchant_id=merchant_id,
            title=str(raw.get("name") or raw.get("sku") or product_id).strip(),
            description=str(attributes.get("description") or attributes.get("short_description") or ""),
            vendor=str(attributes.get("manufacturer") or "").strip() or None,
            product_type=str(raw.get("type_id") or "").strip() or None,
            price=price,
            currency=str(currency or "USD").upper(),
            inventory_quantity=inventory_quantity,
            sku=str(raw.get("sku") or "").strip() or None,
            image_url=images[0] if images else None,
            images=images,
            variants=variants,
            status=ProductStatus.ACTIVE if enabled else ProductStatus.DRAFT,
            created_at=_parse_datetime(raw.get("created_at")),
            updated_at=_parse_datetime(raw.get("updated_at")),
            handle=url_key,
            online_store_url=online_store_url,
            in_stock=bool(own_in_stock or inventory_quantity > 0),
            orderable=orderable,
            platform_metadata={
                "attribute_set_id": raw.get("attribute_set_id"),
                "type_id": raw.get("type_id"),
                "visibility": visibility,
                "store_url": store_url,
            },
        )
        validated, validation = validate_orderable(product)
        product.orderable = bool(product.orderable and validated)
        product.orderable_validation = validation
        product.in_stock = bool(product.inventory_quantity and product.orderable)
        return product

    @staticmethod
    async def fetch_products(
        *,
        store_url: str,
        access_token: str,
        merchant_id: str,
        limit: int = 50,
        page_token: Optional[str] = None,
        store_view_code: str = "default",
        currency: str = "USD",
        product_url_suffix: Optional[str] = None,
    ) -> Tuple[List[StandardProduct], Optional[str], Optional[str]]:
        normalized_url = normalize_magento_store_url(store_url)
        if not normalized_url or not str(access_token or "").strip():
            return [], None, "Magento credentials incomplete"
        page = max(1, _as_int(page_token, 1))
        page_size = max(1, min(_as_int(limit, 50), 100))
        base = build_magento_rest_base(normalized_url, store_view_code)
        try:
            tls_hostname, pinned_address = await _validate_public_https_target(normalized_url)
            pinned_base = _pinned_https_url(base, pinned_address)
            headers = {
                **build_magento_headers(access_token),
                "Host": urlparse(normalized_url).netloc,
            }
            async with httpx.AsyncClient(
                timeout=30.0,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = await client.get(
                    f"{pinned_base}/products",
                    headers=headers,
                    params={
                        "searchCriteria[pageSize]": page_size,
                        "searchCriteria[currentPage]": page,
                    },
                    extensions={"sni_hostname": tls_hostname},
                )
                if response.status_code != 200:
                    return [], None, f"Magento API returned HTTP {response.status_code}"
                payload = response.json() or {}
                raw_products = payload.get("items") if isinstance(payload, dict) else None
                if not isinstance(raw_products, list):
                    return [], None, "Invalid Magento response: missing products list"

                products: List[StandardProduct] = []
                for raw in raw_products:
                    if not isinstance(raw, dict):
                        continue
                    children: List[Dict[str, Any]] = []
                    if str(raw.get("type_id") or "").lower() == "configurable":
                        sku = quote(str(raw.get("sku") or ""), safe="")
                        child_response = await client.get(
                            f"{pinned_base}/configurable-products/{sku}/children",
                            headers=headers,
                            extensions={"sni_hostname": tls_hostname},
                        )
                        if child_response.status_code == 200:
                            child_payload = child_response.json() or []
                            if isinstance(child_payload, list):
                                children = [item for item in child_payload if isinstance(item, dict)]
                        else:
                            logger.warning(
                                "Magento configurable children fetch failed sku=%s status=%s",
                                raw.get("sku"),
                                child_response.status_code,
                            )
                    try:
                        products.append(
                            MagentoProductAdapter.convert_product(
                                raw,
                                merchant_id=merchant_id,
                                store_url=normalized_url,
                                currency=currency,
                                children=children,
                                product_url_suffix=product_url_suffix,
                            )
                        )
                    except Exception as exc:
                        logger.warning("Skipping invalid Magento product id=%s: %s", raw.get("id"), exc)

            total_count = _as_int(payload.get("total_count"))
            next_page = str(page + 1) if page * page_size < total_count else None
            return products, next_page, None
        except Exception:
            logger.exception(
                "Magento product fetch failed host=%s",
                urlparse(normalized_url).hostname,
            )
            return [], None, "Failed to fetch Magento products"
