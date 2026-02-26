"""
Product Platform Adapters
将各平台的产品数据转换为 StandardProduct 格式
Pivota 的核心价值层
"""

from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime
import httpx
import logging
import time
import re
from urllib.parse import urlparse, parse_qs

from models.standard_product import StandardProduct, StandardProductVariant, ProductStatus

logger = logging.getLogger(__name__)

_SHOP_CURRENCY_CACHE: Dict[str, tuple[float, str]] = {}
_SHOP_CURRENCY_TTL_SECONDS = 6 * 60 * 60
SHOPIFY_NEXT_PAGE_TOKEN_UNPARSEABLE = "__SHOPIFY_NEXT_PAGE_TOKEN_UNPARSEABLE__"


def extract_shopify_next_page_token(link_header: str) -> Tuple[Optional[str], bool, Optional[str]]:
    """
    Parse Shopify Link header and extract `page_info` from rel=next URL.

    Returns:
      (next_page_token, has_next, parse_error)
    """
    header = str(link_header or "").strip()
    if not header:
        return None, False, None

    parts = [part.strip() for part in header.split(",") if part.strip()]
    has_next = False
    parse_error: Optional[str] = None
    for part in parts:
        if re.search(r'rel\s*=\s*"next"|rel\s*=\s*next', part, flags=re.IGNORECASE):
            has_next = True
            match = re.search(r"<([^>]+)>", part)
            if not match:
                parse_error = "next_link_missing_url_brackets"
                continue
            url_part = (match.group(1) or "").strip()
            if not url_part:
                parse_error = "next_link_empty_url"
                continue
            parsed = urlparse(url_part)
            query = parse_qs(parsed.query)
            token = (query.get("page_info", [None])[0] or "").strip()
            if token:
                return token, True, None
            parse_error = "next_link_missing_page_info"

    return None, has_next, parse_error


def _get_cached_shop_currency(shop_domain: str) -> Optional[str]:
    key = (shop_domain or "").strip().lower()
    if not key:
        return None
    hit = _SHOP_CURRENCY_CACHE.get(key)
    if not hit:
        return None
    expires_at, currency = hit
    if expires_at < time.time():
        _SHOP_CURRENCY_CACHE.pop(key, None)
        return None
    return currency


def _set_cached_shop_currency(shop_domain: str, currency: str) -> None:
    key = (shop_domain or "").strip().lower()
    cur = (currency or "").strip().upper()
    if not key or not cur:
        return
    _SHOP_CURRENCY_CACHE[key] = (time.time() + _SHOP_CURRENCY_TTL_SECONDS, cur)


async def _fetch_shop_currency(
    *,
    client: httpx.AsyncClient,
    shop_domain: str,
    headers: Dict[str, str],
    api_version: str = "2024-07",
) -> Optional[str]:
    """
    Fetch the shop's base currency from Shopify.

    Shopify product/variant prices returned by Admin REST are in the shop currency, but the
    products endpoint does not include a currency field. We must fetch it from /shop.json.
    """
    url = f"https://{shop_domain}/admin/api/{api_version}/shop.json"
    resp = await client.get(url, headers=headers)
    if resp.status_code != 200:
        return None
    data = resp.json() or {}
    shop = data.get("shop") if isinstance(data, dict) else None
    currency = (shop.get("currency") if isinstance(shop, dict) else None) or ""
    cur = str(currency).strip().upper()
    return cur or None


class ShopifyProductAdapter:
    """Shopify 产品适配器：Shopify API → StandardProduct"""

    @staticmethod
    async def fetch_shop_currency(
        *,
        shop_domain: str,
        access_token: str,
        api_version: str = "2024-07",
    ) -> Optional[str]:
        """
        Fetch and cache Shopify shop currency (EUR/USD/etc.) for a shop domain.
        Returns None when the currency cannot be fetched.
        """
        shop_domain = (shop_domain or "").strip()
        access_token = (access_token or "").strip()
        if not shop_domain or not access_token:
            return None

        cached = _get_cached_shop_currency(shop_domain)
        if cached:
            return cached

        headers = {"X-Shopify-Access-Token": access_token}
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                cur = await _fetch_shop_currency(
                    client=client,
                    shop_domain=shop_domain,
                    headers=headers,
                    api_version=api_version,
                )
            if cur:
                _set_cached_shop_currency(shop_domain, cur)
            return cur
        except Exception:
            return None

    @staticmethod
    async def fetch_product_by_id(
        *,
        shop_domain: str,
        access_token: str,
        merchant_id: str,
        product_id: str,
        api_version: str = "2024-07",
    ) -> Tuple[Optional[StandardProduct], Optional[str]]:
        """
        Fetch a single product from Shopify Admin REST by numeric product id.

        Returns:
            (product, error_message)
        """
        shop_domain = (shop_domain or "").strip()
        access_token = (access_token or "").strip()
        pid = (product_id or "").strip()
        if not shop_domain or not access_token or not pid:
            return None, "Missing shop_domain/access_token/product_id"

        url = f"https://{shop_domain}/admin/api/{api_version}/products/{pid}.json"
        headers = {"X-Shopify-Access-Token": access_token}

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                shop_currency = _get_cached_shop_currency(shop_domain)
                if not shop_currency:
                    try:
                        shop_currency = await _fetch_shop_currency(
                            client=client,
                            shop_domain=shop_domain,
                            headers=headers,
                            api_version=api_version,
                        )
                        if shop_currency:
                            _set_cached_shop_currency(shop_domain, shop_currency)
                    except Exception:
                        shop_currency = None

                resp = await client.get(url, headers=headers)

            if resp.status_code == 404:
                return None, "NOT_FOUND"
            if resp.status_code != 200:
                return (
                    None,
                    f"Shopify API error: {resp.status_code} - {resp.text[:200]}",
                )

            data = resp.json() or {}
            product = data.get("product") if isinstance(data, dict) else None
            if not isinstance(product, dict):
                return None, "Invalid Shopify response: missing product"

            standard = ShopifyProductAdapter.convert_to_standard(
                product, merchant_id, currency=(shop_currency or "USD")
            )
            return standard, None
        except Exception as e:
            return None, f"Failed to fetch Shopify product: {str(e)}"
    
    @staticmethod
    async def fetch_products(
        shop_domain: str,
        access_token: str,
        merchant_id: str,
        limit: int = 50,
        page_info: Optional[str] = None
    ) -> Tuple[List[StandardProduct], Optional[str], Optional[str]]:
        """
        实时从 Shopify 拉取产品并转换为标准格式
        
        Returns:
            (products, next_page_token, error_message)
        """
        url = f"https://{shop_domain}/admin/api/2024-07/products.json"
        # Use published_status=any only on the first page. Shopify does not
        # allow published_status together with page_info pagination.
        params = {"limit": min(limit, 250)}
        if page_info:
            params["page_info"] = page_info
        else:
            params["published_status"] = "any"
            # Keep status unset. Some shops now return an empty list when `status=any`
            # is passed, even with a valid token and non-empty catalog.
        
        headers = {"X-Shopify-Access-Token": access_token}
        
        try:
            logger.info(f"🌐 ShopifyAdapter Fetch start merchant_id={merchant_id} shop_domain={shop_domain} limit={limit}")
            async with httpx.AsyncClient(timeout=30.0) as client:
                shop_currency = _get_cached_shop_currency(shop_domain)
                if not shop_currency:
                    try:
                        shop_currency = await _fetch_shop_currency(
                            client=client, shop_domain=shop_domain, headers=headers
                        )
                        if shop_currency:
                            _set_cached_shop_currency(shop_domain, shop_currency)
                    except Exception:
                        shop_currency = None

                response = await client.get(url, headers=headers, params=params)
            
            if response.status_code != 200:
                error_msg = f"Shopify API error: {response.status_code} - {response.text[:200]}"
                logger.error(error_msg)
                return [], None, error_msg
            
            data = response.json()
            shopify_products = data.get("products", [])
            logger.info(
                f"📊 ShopifyAdapter response status={response.status_code} "
                f"products_len={len(shopify_products)} keys={list(data.keys())}"
            )
            
            # 转换为标准格式
            standard_products = [
                ShopifyProductAdapter.convert_to_standard(sp, merchant_id, currency=(shop_currency or "USD"))
                for sp in shopify_products
            ]
            
            next_page_token = None
            next_token, has_next, parse_error = extract_shopify_next_page_token(
                response.headers.get("Link", ""),
            )
            if next_token:
                next_page_token = next_token
            elif has_next:
                next_page_token = SHOPIFY_NEXT_PAGE_TOKEN_UNPARSEABLE
                logger.warning(
                    "Shopify Link header contains rel=next but page_info is unavailable: merchant_id=%s parse_error=%s link=%s",
                    merchant_id,
                    parse_error,
                    response.headers.get("Link", ""),
                )
            
            logger.info(f"✅ Fetched {len(standard_products)} products from Shopify for merchant {merchant_id}")
            return standard_products, next_page_token, None
            
        except Exception as e:
            error_msg = f"Failed to fetch Shopify products: {str(e)}"
            logger.error(error_msg)
            return [], None, error_msg
    
    @staticmethod
    def convert_to_standard(shopify_product: Dict[str, Any], merchant_id: str, currency: str = "USD") -> StandardProduct:
        """
        核心转换逻辑：Shopify Product → StandardProduct
        """
        sp = shopify_product
        
        # 解析图片
        image_url = None
        images = []
        if sp.get("images"):
            image_url = sp["images"][0]["src"]
            images = [img["src"] for img in sp["images"]]
        elif sp.get("image"):
            image_url = sp["image"]["src"]
            images = [image_url]
        
        # 解析变体
        variants = []
        variant_prices: List[float] = []
        default_price = 0.0
        default_inventory = 0
        default_sku = None
        default_barcode = None
        
        if sp.get("variants"):
            for idx, sv in enumerate(sp["variants"]):
                # 构建变体选项字典
                options_dict = {}
                if sp.get("options"):
                    for i, opt in enumerate(sp["options"]):
                        opt_name = opt.get("name", f"Option{i+1}")
                        opt_value = sv.get(f"option{i+1}")
                        if opt_value:
                            options_dict[opt_name] = opt_value
                
                variant = StandardProductVariant(
                    id=str(sv["id"]),
                    title=sv.get("title", "Default"),
                    sku=sv.get("sku"),
                    barcode=sv.get("barcode"),
                    price=float(sv.get("price", 0)),
                    compare_at_price=float(sv["compare_at_price"]) if sv.get("compare_at_price") else None,
                    inventory_quantity=int(sv.get("inventory_quantity") or 0),
                    weight=sv.get("weight"),
                    weight_unit=sv.get("weight_unit"),
                    options=options_dict if options_dict else None,
                    image_url=None  # Shopify 变体图片需要匹配 images 数组
                )
                variants.append(variant)

                default_inventory += variant.inventory_quantity
                if variant.price and variant.price > 0:
                    variant_prices.append(variant.price)
                if default_sku is None and variant.sku:
                    default_sku = variant.sku
                if default_barcode is None and variant.barcode:
                    default_barcode = variant.barcode

            if variant_prices:
                default_price = min(variant_prices)
        
        # 解析时间
        published_at = None
        if sp.get("published_at"):
            try:
                published_at = datetime.fromisoformat(sp["published_at"].replace('Z', '+00:00'))
            except:
                pass
        
        created_at = None
        if sp.get("created_at"):
            try:
                created_at = datetime.fromisoformat(sp["created_at"].replace('Z', '+00:00'))
            except:
                pass
        
        updated_at = None
        if sp.get("updated_at"):
            try:
                updated_at = datetime.fromisoformat(sp["updated_at"].replace('Z', '+00:00'))
            except:
                pass
        
        # 解析标签
        tags = []
        if sp.get("tags"):
            tags = [t.strip() for t in sp["tags"].split(",") if t.strip()]
        
        # 解析状态
        status = ProductStatus.ACTIVE
        if sp.get("status") == "draft":
            status = ProductStatus.DRAFT
        elif sp.get("status") == "archived":
            status = ProductStatus.ARCHIVED
        
        def _is_variant_sellable(raw_variant: Dict[str, Any]) -> bool:
            """
            Shopify inventory semantics:
            - If inventory is NOT tracked (inventory_management is null), the variant can be sold.
            - If inventory is tracked, the variant is sellable when inventory_quantity > 0,
              OR when inventory_policy == "continue" (backorders allowed).
            """
            try:
                price_value = float(raw_variant.get("price") or 0)
            except Exception:
                price_value = 0.0
            if price_value <= 0:
                return False

            inventory_management = raw_variant.get("inventory_management")
            inventory_policy = (raw_variant.get("inventory_policy") or "").lower()
            try:
                inventory_quantity = int(raw_variant.get("inventory_quantity") or 0)
            except Exception:
                inventory_quantity = 0

            if inventory_management in (None, "", False):
                return True
            if inventory_quantity > 0:
                return True
            if inventory_policy == "continue":
                return True
            return False

        raw_variants = sp.get("variants") or []
        sellable_variant_count = 0
        min_sellable_price: Optional[float] = None
        if isinstance(raw_variants, list):
            for v in raw_variants:
                if not isinstance(v, dict):
                    continue
                if not _is_variant_sellable(v):
                    continue
                sellable_variant_count += 1
                try:
                    v_price = float(v.get("price") or 0)
                except Exception:
                    v_price = 0.0
                if v_price > 0 and (min_sellable_price is None or v_price < min_sellable_price):
                    min_sellable_price = v_price

        # Determine if product is orderable/sellable.
        # We intentionally do NOT require published_at to be present because many Shopify
        # stores sell products without an Online Store "published_at" timestamp (e.g. other channels).
        is_orderable = bool(status == ProductStatus.ACTIVE and sellable_variant_count > 0)
        is_in_stock = is_orderable
        if min_sellable_price is not None and min_sellable_price > 0:
            default_price = float(min_sellable_price)

        product = StandardProduct(
            id=str(sp["id"]),
            platform="shopify",
            merchant_id=merchant_id,
            title=sp.get("title", "Untitled"),
            description=sp.get("body_html", ""),
            vendor=sp.get("vendor"),
            product_type=sp.get("product_type"),
            tags=tags,
            price=default_price,
            compare_at_price=None,  # 在变体中
            currency=str(currency or "USD").upper(),
            inventory_quantity=default_inventory,
            sku=default_sku,
            barcode=default_barcode,
            image_url=image_url,
            images=images,
            variants=variants,
            status=status,
            published_at=published_at,
            created_at=created_at,
            updated_at=updated_at,
            in_stock=is_in_stock,
            orderable=is_orderable,
            platform_metadata={
                "shopify_id": sp["id"],
                "handle": sp.get("handle"),
                "product_type": sp.get("product_type"),
                "template_suffix": sp.get("template_suffix"),
            }
        )
        
        # Run orderable validation
        from models.standard_product import validate_orderable
        orderable, validation = validate_orderable(product)
        product.orderable = orderable
        product.orderable_validation = validation
        
        return product


class WixProductAdapter:
    """Wix 产品适配器：Wix API → StandardProduct"""

    @staticmethod
    def _stock_to_inventory(stock_data: Optional[Dict[str, Any]]) -> int:
        if not stock_data or not isinstance(stock_data, dict):
            return 0

        # Wix 库存语义（综合兼容）：
        # - 老字段：trackInventory
        # - 新字段：trackQuantity
        # - inStock: 是否展示为“有货”
        # - quantity: 仅在跟踪库存时有意义
        raw_quantity = stock_data.get("quantity", 0) or 0
        in_stock_flag = stock_data.get("inStock")
        track_quantity = stock_data.get("trackQuantity")
        track_inventory = stock_data.get("trackInventory")

        # 不跟踪库存但标记为 inStock：视为充足库存（Wix 前台会显示有货）
        if (track_quantity is False or track_inventory is False) and in_stock_flag is True and raw_quantity <= 0:
            return 9999

        try:
            return int(raw_quantity)
        except Exception:
            return 0

    @staticmethod
    def _first_image_url(media: Any) -> Optional[str]:
        if not media or not isinstance(media, dict):
            return None
        items = media.get("items") or []
        if not isinstance(items, list) or not items:
            return None
        first = items[0] if isinstance(items[0], dict) else None
        img = first.get("image") if first else None
        url = img.get("url") if isinstance(img, dict) else None
        return str(url).strip() if url else None

    @staticmethod
    def _normalize_variant_choices(raw: Any) -> Dict[str, str]:
        if not raw:
            return {}
        if isinstance(raw, dict):
            out: Dict[str, str] = {}
            for k, v in raw.items():
                kk = str(k).strip()
                vv = str(v).strip()
                if kk and vv:
                    out[kk] = vv
            return out
        if isinstance(raw, list):
            out: Dict[str, str] = {}
            for it in raw:
                if not isinstance(it, dict):
                    continue
                k = it.get("name") or it.get("option") or it.get("key")
                v = it.get("value")
                kk = str(k).strip() if k else ""
                vv = str(v).strip() if v else ""
                if kk and vv:
                    out[kk] = vv
            return out
        return {}

    @staticmethod
    def _extract_wix_variants(wp: Dict[str, Any]) -> List[Dict[str, Any]]:
        raw = wp.get("variants")
        if isinstance(raw, list):
            return [v for v in raw if isinstance(v, dict)]
        if isinstance(raw, dict):
            inner = raw.get("variants") or raw.get("items") or raw.get("results") or []
            if isinstance(inner, list):
                return [v for v in inner if isinstance(v, dict)]
        return []

    @staticmethod
    def _convert_product(wp: Dict[str, Any], merchant_id: str) -> Optional[StandardProduct]:
        if not wp or not isinstance(wp, dict):
            return None

        price_data = wp.get("priceData") if wp else {}
        if not price_data:
            price_data = {}

        image_url = WixProductAdapter._first_image_url(wp.get("media"))
        inventory = WixProductAdapter._stock_to_inventory(wp.get("stock"))

        name = str(wp.get("name", "Unnamed Product"))
        product_id = str(wp.get("id", "")).strip()

        # Base product price; if missing/0, fall back to min variant price.
        try:
            base_price = float(price_data.get("price", 0) or 0)
        except Exception:
            base_price = 0.0

        currency = str(price_data.get("currency", "USD"))
        sku = str(wp.get("sku", "")) or None

        variants_out: List[StandardProductVariant] = []
        for v in WixProductAdapter._extract_wix_variants(wp):
            # Wix sometimes nests variant-specific attributes under `variant`.
            v_body = v.get("variant") if isinstance(v.get("variant"), dict) else v
            variant_id = str(v.get("id") or v_body.get("id") or "").strip()
            if not variant_id:
                continue

            v_price_data = v_body.get("priceData") if isinstance(v_body.get("priceData"), dict) else {}
            if not v_price_data and isinstance(v.get("priceData"), dict):
                v_price_data = v.get("priceData")

            try:
                v_price = float(v_price_data.get("price", base_price) or base_price)
            except Exception:
                v_price = base_price

            v_stock = v_body.get("stock") if isinstance(v_body.get("stock"), dict) else v.get("stock")
            v_inventory = WixProductAdapter._stock_to_inventory(v_stock)

            v_sku = str(v_body.get("sku") or v.get("sku") or "").strip() or None

            v_media = v_body.get("media") if isinstance(v_body.get("media"), dict) else v.get("media")
            v_image_url = WixProductAdapter._first_image_url(v_media) or image_url

            choices = WixProductAdapter._normalize_variant_choices(v.get("choices") or v_body.get("choices"))
            title = " / ".join([str(val) for val in choices.values()]) if choices else name

            variants_out.append(
                StandardProductVariant(
                    id=variant_id,
                    title=title,
                    sku=v_sku,
                    price=v_price,
                    inventory_quantity=int(v_inventory),
                    options=choices or None,
                    image_url=v_image_url,
                )
            )

        # If no real variants, keep legacy behavior with a synthetic default variant.
        if not variants_out:
            variants_out = [
                StandardProductVariant(
                    id=str(product_id or sku or "default"),
                    title=name,
                    sku=sku,
                    price=base_price,
                    inventory_quantity=int(inventory),
                    image_url=image_url,
                )
            ]
        else:
            # Ensure stable product inventory and price when variants exist.
            if base_price <= 0:
                prices = [v.price for v in variants_out if isinstance(v.price, (int, float)) and v.price > 0]
                if prices:
                    base_price = float(min(prices))
            invs = [int(v.inventory_quantity or 0) for v in variants_out]
            inventory = 9999 if any(i >= 9999 for i in invs) else int(sum(invs))

        # Check if product is orderable from a Wix perspective:
        # visible + has a positive price.
        is_orderable = bool(wp.get("visible", True)) and base_price > 0

        product = StandardProduct(
            id=product_id,
            title=name,
            description=str(wp.get("description", "")),
            price=base_price,
            currency=currency,
            inventory_quantity=int(inventory),
            sku=sku,
            image_url=image_url,
            platform="wix",
            merchant_id=merchant_id,
            status=ProductStatus.ACTIVE,
            variants=variants_out,
            orderable=is_orderable,
            created_at=wp.get("dateCreated"),
            updated_at=wp.get("lastUpdated"),
        )

        # Run orderable validation
        from models.standard_product import validate_orderable

        orderable, validation = validate_orderable(product)
        product.orderable = orderable
        product.orderable_validation = validation
        return product
    
    @staticmethod
    async def fetch_products(
        site_id: str,
        api_key: str,
        merchant_id: str,
        limit: int = 50,
        page_token: Optional[str] = None
    ) -> Tuple[List[StandardProduct], Optional[str], Optional[str]]:
        """实时从 Wix 拉取产品"""
        import httpx
        
        try:
            logger.info(f"🔄 Fetching Wix products: site_id={site_id}")
            
            url = "https://www.wixapis.com/stores/v1/products/query"
            headers = {
                "Authorization": api_key,
                "wix-site-id": site_id,
                "Content-Type": "application/json"
            }
            
            # Wix products query does NOT include `variants` by default even when a product has
            # productOptions/manageVariants=true. We must opt in.
            payload = {"query": {"paging": {"limit": min(limit, 100)}}, "includeVariants": True}
            
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(url, json=payload, headers=headers)
                
                logger.info(f"🔍 Wix API response: status={response.status_code}, content_length={len(response.text)}")
                
                if response.status_code != 200:
                    error_msg = f"Wix API error: {response.status_code} - {response.text[:200]}"
                    logger.error(error_msg)
                    return [], None, error_msg
                
                data = response.json()
                wix_products = data.get("products", [])
                logger.info(f"✅ Wix API returned {len(wix_products)} products (total_results={data.get('totalResults', 'unknown')})")
                
                standard_products = []
                for wp in wix_products:
                    try:
                        product = WixProductAdapter._convert_product(wp, merchant_id=merchant_id)
                        if product:
                            standard_products.append(product)
                    except Exception as product_error:
                        logger.error(f"Error converting Wix product: {product_error}")
                        continue
                
                return standard_products, None, None
                
        except Exception as e:
            error_msg = f"Error fetching Wix products: {str(e)}"
            logger.error(error_msg)
            return [], None, error_msg


class WooCommerceProductAdapter:
    """WooCommerce 产品适配器：WooCommerce API → StandardProduct（待实现）"""
    
    @staticmethod
    async def fetch_products(
        store_url: str,
        consumer_key: str,
        consumer_secret: str,
        merchant_id: str,
        limit: int = 50,
        page_token: Optional[str] = None
    ) -> Tuple[List[StandardProduct], Optional[str], Optional[str]]:
        """实时从 WooCommerce 拉取产品"""
        # TODO: 实现 WooCommerce API 调用
        logger.warning("WooCommerce adapter not yet implemented")
        return [], None, "WooCommerce adapter not yet implemented"


# 适配器工厂
PLATFORM_ADAPTERS = {
    "shopify": ShopifyProductAdapter,
    "wix": WixProductAdapter,
    "woocommerce": WooCommerceProductAdapter,
}


async def fetch_merchant_products(
    merchant_id: str,
    platform: str,
    credentials: Dict[str, str],
    limit: int = 50,
    page_token: Optional[str] = None
) -> Tuple[List[StandardProduct], Optional[str], Optional[str]]:
    """
    通用产品获取函数（根据平台自动选择适配器）
    
    Args:
        merchant_id: 商户 ID
        platform: shopify, wix, woocommerce
        credentials: 平台凭证（不同平台字段不同）
        limit: 返回产品数量
    
    Returns:
        (products, next_page_token, error_message)
    """
    adapter_class = PLATFORM_ADAPTERS.get(platform)
    
    if not adapter_class:
        error_msg = f"Unsupported platform: {platform}"
        logger.error(error_msg)
        return [], None, error_msg
    
    # 根据平台调用对应适配器
    if platform == "shopify":
        return await adapter_class.fetch_products(
            shop_domain=credentials.get("shop_domain"),
            access_token=credentials.get("access_token"),
            merchant_id=merchant_id,
            limit=limit,
            page_info=page_token
        )
    elif platform == "wix":
        return await adapter_class.fetch_products(
            site_id=credentials.get("site_id"),
            api_key=credentials.get("api_key"),
            merchant_id=merchant_id,
            limit=limit,
            page_token=page_token
        )
    elif platform == "woocommerce":
        return await adapter_class.fetch_products(
            store_url=credentials.get("store_url"),
            consumer_key=credentials.get("consumer_key"),
            consumer_secret=credentials.get("consumer_secret"),
            merchant_id=merchant_id,
            limit=limit,
            page_token=page_token
        )
    else:
        return [], None, f"Platform {platform} not implemented"
