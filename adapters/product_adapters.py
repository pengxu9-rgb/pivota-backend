"""
Product Platform Adapters
将各平台的产品数据转换为 StandardProduct 格式
Pivota 的核心价值层
"""

from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime
import httpx
import logging
from urllib.parse import urlparse, parse_qs

from models.standard_product import StandardProduct, StandardProductVariant, ProductStatus

logger = logging.getLogger(__name__)


class ShopifyProductAdapter:
    """Shopify 产品适配器：Shopify API → StandardProduct"""
    
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
        # Use status=any so that draft/unlisted/archived products are also
        # synced into products_cache for the merchant portal. Agent-facing
        # surfaces still gate on StandardProduct.status/orderable.
        params = {"limit": min(limit, 250), "status": "any"}
        if page_info:
            params["page_info"] = page_info
        
        headers = {"X-Shopify-Access-Token": access_token}
        
        try:
            logger.info(f"🌐 ShopifyAdapter Fetch start merchant_id={merchant_id} shop_domain={shop_domain} limit={limit}")
            async with httpx.AsyncClient(timeout=30.0) as client:
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
                ShopifyProductAdapter.convert_to_standard(sp, merchant_id)
                for sp in shopify_products
            ]
            
            # 提取分页信息（Shopify 使用 Link header）
            next_page_token = None
            link_header = response.headers.get("Link", "")
            if "rel=\"next\"" in link_header:
                try:
                    # Link header example:
                    # <https://shop.myshopify.com/admin/api/2024-07/products.json?page_info=abc&limit=250>; rel="next"
                    parts = [p.strip() for p in link_header.split(",")]
                    for part in parts:
                        if 'rel="next"' in part:
                            start = part.find("<")
                            end = part.find(">")
                            if start != -1 and end != -1:
                                url_part = part[start + 1 : end]
                                parsed = urlparse(url_part)
                                qs = parse_qs(parsed.query)
                                token = qs.get("page_info", [None])[0]
                                if token:
                                    next_page_token = token
                                    break
                    # Fallback: if parsing fails but rel=next exists, keep a simple flag
                    if not next_page_token:
                        next_page_token = "has_next"
                except Exception:
                    next_page_token = "has_next"
            
            logger.info(f"✅ Fetched {len(standard_products)} products from Shopify for merchant {merchant_id}")
            return standard_products, next_page_token, None
            
        except Exception as e:
            error_msg = f"Failed to fetch Shopify products: {str(e)}"
            logger.error(error_msg)
            return [], None, error_msg
    
    @staticmethod
    def convert_to_standard(shopify_product: Dict[str, Any], merchant_id: str) -> StandardProduct:
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
                    inventory_quantity=sv.get("inventory_quantity", 0),
                    weight=sv.get("weight"),
                    weight_unit=sv.get("weight_unit"),
                    options=options_dict if options_dict else None,
                    image_url=None  # Shopify 变体图片需要匹配 images 数组
                )
                variants.append(variant)
                
                # 第一个变体作为默认值
                if idx == 0:
                    default_price = variant.price
                    default_inventory = variant.inventory_quantity
                    default_sku = variant.sku
                    default_barcode = variant.barcode
        
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
        
        # Determine if product is orderable
        # Product is orderable if it's active/published and has at least one variant with price > 0
        is_orderable = (
            status == ProductStatus.ACTIVE and 
            published_at is not None and
            len(variants) > 0 and
            default_price > 0
        )
        
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
            currency="USD",  # 可从 shop.json 获取
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
            
            payload = {"query": {"paging": {"limit": min(limit, 100)}}}
            
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
                        price_data = wp.get("priceData") if wp else {}
                        if not price_data:
                            price_data = {}
                        
                        media_items = []
                        if wp and wp.get("media"):
                            media_items = wp.get("media", {}).get("items", [])
                        
                        image_url = None
                        if media_items and len(media_items) > 0:
                            image_obj = media_items[0].get("image") if media_items[0] else None
                            if image_obj:
                                image_url = image_obj.get("url")
                        
                        stock_data = wp.get("stock") if wp else {}
                        inventory = 0
                        if stock_data:
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
                                inventory = 9999
                            else:
                                # 正常按照数量来
                                inventory = raw_quantity
                        
                        # Check if product is orderable
                        is_orderable = (
                            wp.get("visible", True) and  # Product is visible
                            float(price_data.get("price", 0)) > 0  # Has valid price
                        )
                        
                        product = StandardProduct(
                            id=str(wp.get("id", "")),
                            title=str(wp.get("name", "Unnamed Product")),
                            description=str(wp.get("description", "")),
                            price=float(price_data.get("price", 0)),
                            currency=str(price_data.get("currency", "USD")),
                            inventory_quantity=int(inventory),
                            sku=str(wp.get("sku", "")),
                            image_url=image_url,
                            platform="wix",
                            merchant_id=merchant_id,
                            status=ProductStatus.ACTIVE,
                            orderable=is_orderable,
                            created_at=wp.get("dateCreated"),
                            updated_at=wp.get("lastUpdated")
                        )
                        
                        # Run orderable validation
                        from models.standard_product import validate_orderable
                        orderable, validation = validate_orderable(product)
                        product.orderable = orderable
                        product.orderable_validation = validation
                        
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
