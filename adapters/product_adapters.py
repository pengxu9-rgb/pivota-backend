"""
Product Platform Adapters
将各平台的产品数据转换为 StandardProduct 格式
Pivota 的核心价值层
"""

from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime
import httpx
import json
import logging
import os
import time
import re
from urllib.parse import urlparse, parse_qs

from adapters.bigcommerce_adapter import (
    build_bigcommerce_headers,
    normalize_bigcommerce_store_hash,
)
from adapters.cafe24_adapter import Cafe24ProductAdapter
from adapters.woocommerce_adapter import normalize_woocommerce_store_url
from models.standard_product import StandardProduct, StandardProductVariant, ProductStatus
from services.wix_connection import (
    WIX_ALL_PRODUCTS_COLLECTION_ID,
    WIX_COLLECTIONS_QUERY_URL,
    WIX_PRODUCTS_QUERY_URL,
    build_wix_catalog_headers,
)

logger = logging.getLogger(__name__)

_SHOP_CURRENCY_CACHE: Dict[str, tuple[float, str]] = {}
_SHOP_CURRENCY_TTL_SECONDS = 6 * 60 * 60
SHOPIFY_NEXT_PAGE_TOKEN_UNPARSEABLE = "__SHOPIFY_NEXT_PAGE_TOKEN_UNPARSEABLE__"
WIX_MAX_PRODUCTS = 5000


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
    api_version: str = "2025-10",
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


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except Exception:
        return default


# Collections are a per-STORE resource, not per-product: a store has tens of
# them, not thousands. These bounds exist so a pathological or hostile response
# cannot spin the sync, not because real stores approach them.
_WIX_COLLECTIONS_PAGE_LIMIT = 100
_WIX_COLLECTIONS_MAX_PAGES = 10

# Merchandising collections describe WHERE a product sits in the storefront, not
# WHAT it is. They are legitimate collections — we just never want one as the
# category when a semantic sibling exists, because everything downstream of
# `product_type` (vertical resolution, beauty taxonomy, category probes) reads it
# as the product's kind. Anchored at the start so "Sale" and "Sale Items" match
# while "Salt Scrubs" does not.
_WIX_MERCHANDISING_COLLECTION_RE = re.compile(
    r"^\s*(all\b|shop\s+all\b|new\b|new\s+arrivals?\b|best\b|best\s*sell|top\s+sell"
    r"|featured\b|sale\b|on\s+sale\b|clearance\b|trending\b|popular\b|gifts?\b"
    # NOT "accessories": that is a real product kind, not a merchandising
    # shelf, and it resolves to a correct vertical on its own.
    r"|gift\s+(guide|ideas?)\b|bundles?\b|deals?\b)",
    re.IGNORECASE,
)


def _wix_max_pages_override() -> Optional[int]:
    raw = os.getenv("WIX_MAX_PAGES")
    if raw is None or str(raw).strip() == "":
        return None
    pages = _as_int(raw, 0)
    return pages if pages > 0 else None


def _parse_platform_datetime(value: Any) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None


def _split_tags(raw: Any) -> List[str]:
    if isinstance(raw, list):
        values = raw
    else:
        values = str(raw or "").split(",")
    tags: List[str] = []
    for item in values:
        if isinstance(item, dict):
            candidate = str(item.get("name") or item.get("label") or "").strip()
        else:
            candidate = str(item or "").strip()
        if candidate and candidate not in tags:
            tags.append(candidate)
    return tags


def _shopify_metafield_ingest_enabled() -> bool:
    """Phase O-5b #4: feature flag for per-product metafield fetch.
    Default OFF — flip SHOPIFY_METAFIELD_INGEST_ENABLED=true on the
    environment to enable. Adds N HTTP calls per sync page.
    """
    return os.getenv("SHOPIFY_METAFIELD_INGEST_ENABLED", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


class ShopifyProductAdapter:
    """Shopify 产品适配器：Shopify API → StandardProduct"""

    @staticmethod
    async def _fetch_metafields_for_products(
        *,
        client: httpx.AsyncClient,
        shop_domain: str,
        headers: Dict[str, str],
        products: List[Dict[str, Any]],
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Bulk-fetch metafields for a product list via a single Shopify
        GraphQL Admin API call. Returns {str(product_id): [metafield_dict,
        ...]}. Each metafield dict is normalized to match the REST shape
        (namespace, key, value, type) so the downstream consumer in
        services/fashion_field_payload_extractor.py works unchanged.

        Why GraphQL instead of parallel REST: Shopify Admin REST throttles
        at 2 req/sec per client; per-product /metafields.json calls hit
        429 in bursts even for modest catalogs. GraphQL has a separate
        cost-based throttle (1000 cost points/sec) and this query is ~10
        points per product, so 250 products in one call ≈ 2500 points —
        well within the burst budget. Verified live 2026-05-18 on an
        active Shopify catalog after the original REST adapter (PR #546)
        blew up with 429 storms.

        Failure mode: on any GraphQL transport/parse failure, returns an
        empty dict and logs the cause. One bad sync page should not
        break the whole catalog sync.
        """
        product_ids = [str(p.get("id")) for p in products if p.get("id")]
        if not product_ids:
            return {}

        gids = [f"gid://shopify/Product/{pid}" for pid in product_ids]
        query = """
        query getProductMetafields($ids: [ID!]!) {
          nodes(ids: $ids) {
            ... on Product {
              id
              metafields(first: 50) {
                edges {
                  node {
                    namespace
                    key
                    value
                    type
                  }
                }
              }
            }
          }
        }
        """
        url = f"https://{shop_domain}/admin/api/2025-10/graphql.json"
        try:
            resp = await client.post(
                url,
                headers={**headers, "Content-Type": "application/json"},
                json={"query": query, "variables": {"ids": gids}},
            )
        except Exception as exc:
            logger.warning("shopify_metafields_graphql.transport_fail err=%s", exc)
            return {pid: [] for pid in product_ids}
        if resp.status_code != 200:
            logger.warning(
                "shopify_metafields_graphql.http_%d body=%s",
                resp.status_code, resp.text[:300],
            )
            return {pid: [] for pid in product_ids}
        try:
            payload = resp.json() or {}
            if "errors" in payload:
                logger.warning(
                    "shopify_metafields_graphql.api_errors %s",
                    json.dumps(payload["errors"])[:300],
                )
                return {pid: [] for pid in product_ids}
            nodes = ((payload.get("data") or {}).get("nodes") or [])
        except Exception as exc:
            logger.warning("shopify_metafields_graphql.parse_fail err=%s", exc)
            return {pid: [] for pid in product_ids}

        out: Dict[str, List[Dict[str, Any]]] = {pid: [] for pid in product_ids}
        for node in nodes:
            if not isinstance(node, dict):
                continue
            gid = node.get("id") or ""
            # gid shape: "gid://shopify/Product/10064562225449" → tail = pid
            pid = gid.rsplit("/", 1)[-1] if gid else None
            if not pid or pid not in out:
                continue
            mfs_block = node.get("metafields") or {}
            edges = mfs_block.get("edges") or []
            mfs: List[Dict[str, Any]] = []
            for edge in edges:
                inner = (edge or {}).get("node")
                if not isinstance(inner, dict):
                    continue
                # Normalize to REST shape so the consumer code doesn't
                # have to know which adapter produced this.
                mfs.append({
                    "namespace": inner.get("namespace"),
                    "key": inner.get("key"),
                    "value": inner.get("value"),
                    "type": inner.get("type"),
                })
            out[pid] = mfs
        return out

    @staticmethod
    async def fetch_shop_currency(
        *,
        shop_domain: str,
        access_token: str,
        api_version: str = "2025-10",
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
        api_version: str = "2025-10",
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
        url = f"https://{shop_domain}/admin/api/2025-10/products.json"
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

            # Optional metafield ingest (Phase O-5b #4). Default OFF; flip
            # SHOPIFY_METAFIELD_INGEST_ENABLED=true to fetch per-product
            # metafields in parallel after the product list. Adds N HTTP
            # calls per sync page (1 per product); Shopify rate limits at
            # 40 req/s on REST so a 250-product page = ~6s worst case.
            # See _fetch_metafields_for_products for the bulk fetch impl.
            metafields_by_product_id: Dict[str, List[Dict[str, Any]]] = {}
            if _shopify_metafield_ingest_enabled() and shopify_products:
                async with httpx.AsyncClient(timeout=30.0) as mf_client:
                    metafields_by_product_id = await ShopifyProductAdapter._fetch_metafields_for_products(
                        client=mf_client,
                        shop_domain=shop_domain,
                        headers=headers,
                        products=shopify_products,
                    )

            # 转换为标准格式
            standard_products = [
                ShopifyProductAdapter.convert_to_standard(
                    sp,
                    merchant_id,
                    currency=(shop_currency or "USD"),
                    metafields=metafields_by_product_id.get(str(sp.get("id")), []),
                )
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
    def convert_to_standard(
        shopify_product: Dict[str, Any],
        merchant_id: str,
        currency: str = "USD",
        metafields: Optional[List[Dict[str, Any]]] = None,
    ) -> StandardProduct:
        """
        核心转换逻辑：Shopify Product → StandardProduct

        metafields (optional): list of Shopify metafield dicts in the
        {namespace, key, value, type} shape returned by
        /products/{id}/metafields.json. Attached to platform_metadata
        under the "metafields" key so the consumer in
        services/fashion_field_payload_extractor.py can read them.
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
            # TOP-LEVEL handle, not just platform_metadata: the agent gateway
            # serves StandardProduct(**products_cache.product_data) directly
            # (services/product_query_service), so only top-level fields reach
            # the card builder — the P2b redirect post-pass
            # (_attach_connected_product_redirects) derives the merchant-store
            # destination from connected shop domain + handle. No
            # online_store_url here: the adapter only knows the admin-API
            # domain; the gateway builds the destination from the connected
            # store's domain.
            handle=(str(sp.get("handle") or "").strip() or None),
            platform_metadata={
                "shopify_id": sp["id"],
                "handle": sp.get("handle"),
                "product_type": sp.get("product_type"),
                "template_suffix": sp.get("template_suffix"),
                # Phase O-5b #4: Shopify metafields surface here so
                # services/fashion_field_payload_extractor.py can read
                # material/care/size_guide from authoritative merchant
                # input. Empty list when the metafield ingest flag is
                # off OR the product has no metafields.
                "metafields": list(metafields or []),
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
    def _pick_wix_identifier(*sources: Any) -> Optional[str]:
        for source in sources:
            if not isinstance(source, dict):
                continue
            for key in (
                "gtin",
                "gtin8",
                "gtin12",
                "gtin13",
                "gtin14",
                "gtin_8",
                "gtin_12",
                "gtin_13",
                "gtin_14",
                "upc",
                "upcA",
                "upc_a",
                "upc12",
                "upc_12",
                "ean",
                "ean8",
                "ean13",
                "ean_8",
                "ean_13",
                "barcode",
                "barCode",
                "bar_code",
                "mpn",
                "manufacturerPartNumber",
                "manufacturer_part_number",
            ):
                value = str(source.get(key) or "").strip()
                if value:
                    return value
        return None

    @staticmethod
    def _storefront_page_url(wp: Dict[str, Any]) -> Optional[str]:
        """Absolute public product-page URL from Wix ``productPageUrl``.

        Wix Stores returns it either as base+path parts
        (``{"base": "https://www.site.com/", "path": "/product-page/mug"}``)
        or, on some payloads, a plain string. Returns None when the field is
        absent or not an absolute URL — the attributed-redirect lane must
        never get a fabricated destination."""
        raw = wp.get("productPageUrl") or wp.get("product_page_url")
        if isinstance(raw, str):
            url = raw.strip()
            return url if url.startswith(("http://", "https://")) else None
        if not isinstance(raw, dict):
            return None
        base = str(raw.get("base") or "").strip()
        path = str(raw.get("path") or "").strip()
        if not base.startswith(("http://", "https://")) or not path:
            return None
        return base.rstrip("/") + "/" + path.lstrip("/")

    @staticmethod
    def _storefront_slug(wp: Dict[str, Any], page_url: Optional[str]) -> Optional[str]:
        slug = str(wp.get("slug") or "").strip()
        if slug:
            return slug
        if not page_url:
            return None
        tail = page_url.split("?", 1)[0].split("#", 1)[0].rstrip("/").rsplit("/", 1)[-1].strip()
        return tail or None

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
    def _extract_wix_collection_ids(wp: Dict[str, Any]) -> List[str]:
        """The collection ids on a Wix product, minus the implicit "All Products".

        Wix has moved this field's spelling across catalog versions, so accept
        the known aliases rather than pinning one and silently mapping nothing:
        a wrong guess here does not raise, it just leaves every product
        category-less exactly as before, which is indistinguishable from the
        bug we are fixing.
        """
        raw: Any = None
        for key in ("collectionIds", "collection_ids", "collectionIDs"):
            if isinstance(wp.get(key), list):
                raw = wp.get(key)
                break
        if raw is None:
            return []
        out: List[str] = []
        for cid in raw:
            text = str(cid or "").strip()
            if text and text != WIX_ALL_PRODUCTS_COLLECTION_ID:
                out.append(text)
        return out

    @staticmethod
    def _pick_wix_product_type(
        wp: Dict[str, Any],
        collection_names: Optional[Dict[str, str]],
    ) -> Optional[str]:
        """Resolve a Wix product's category name from its collections.

        A product can belong to many collections, so the choice must be
        DETERMINISTIC: an unstable `product_type` churns the quality score (and
        anything keyed off it) on every re-sync, turning a fixed field into a
        source of drift.

        But determinism is a TIE-BREAK rule, not a selection rule, and plain
        alphabetical order is not neutral — merchandising collections cluster on
        A/B/N/S ("Accessories", "Best Sellers", "New Arrivals", "Sale"), so
        alphabetical-first is systematically biased toward them and away from the
        semantic category. Measured on realistic pairs: {Best Sellers, Dog
        Harnesses} -> "Best Sellers"; {New Arrivals, Vitamin C Serum} -> "New
        Arrivals", which `resolve_vertical` then reads as `other` instead of
        `beauty`, so beauty taxonomy rows are never written and category probes
        generate queries for "New Arrivals".

        That is the same valid-but-wrong failure the All-Products exclusion
        exists to prevent, just softer: the score component fills either way,
        which is exactly what makes it easy to miss. So deprioritise the
        merchandising tier first, then sort alphabetically WITHIN the surviving
        tier, and fall back to the full set only if the stoplist empties it —
        a merchandising category still beats none for the score.
        """
        if not collection_names:
            return None
        names = sorted(
            {
                name.strip()
                for cid in WixProductAdapter._extract_wix_collection_ids(wp)
                if (name := str(collection_names.get(cid) or "").strip())
            }
        )
        if not names:
            return None
        semantic = [n for n in names if not _WIX_MERCHANDISING_COLLECTION_RE.match(n)]
        return (semantic or names)[0]

    @staticmethod
    async def _fetch_wix_collection_names(
        client: "httpx.AsyncClient",
        headers: Dict[str, str],
        merchant_id: str,
    ) -> Dict[str, str]:
        """`{collection_id: name}` for the store, or `{}` if unavailable.

        NEVER raises and never fails the sync. Categories are an enrichment:
        losing them costs one of six score components, while letting this call
        abort the run would cost the entire catalog. Degrading here reproduces
        exactly the pre-2026-07-29 behaviour, which is a known, survivable state.

        Degrades ALL-OR-NOTHING. A partial map is worse than no map: with 101
        collections, a product in {"Zebra Print" (page 1), "Apparel" (page 2)}
        resolves to "Apparel" on a complete fetch and "Zebra Print" when page 2
        500s — a different, valid-looking category, with nothing to distinguish
        it, that silently flips back on the next successful sync. That breaks the
        determinism `_pick_wix_product_type` depends on. Returning {} instead
        costs one component for one sync and trips the canary, which is a state
        we can see.
        """
        names: Dict[str, str] = {}
        try:
            offset = 0
            for _ in range(_WIX_COLLECTIONS_MAX_PAGES):
                response = await client.post(
                    WIX_COLLECTIONS_QUERY_URL,
                    json={"query": {"paging": {"limit": _WIX_COLLECTIONS_PAGE_LIMIT, "offset": offset}}},
                    headers=headers,
                )
                if response.status_code != 200:
                    logger.warning(
                        "Wix collections unavailable merchant_id=%s status=%s partial_pages=%s "
                        "body=%s — DISCARDING %s partial results; products will sync WITHOUT "
                        "a category",
                        merchant_id,
                        response.status_code,
                        offset // _WIX_COLLECTIONS_PAGE_LIMIT,
                        response.text[:200],
                        len(names),
                    )
                    return {}
                payload = response.json() or {}
                page = payload.get("collections")
                if not isinstance(page, list) or not page:
                    break
                for collection in page:
                    if not isinstance(collection, dict):
                        continue
                    cid = str(collection.get("id") or "").strip()
                    name = str(collection.get("name") or "").strip()
                    if cid and name and cid != WIX_ALL_PRODUCTS_COLLECTION_ID:
                        names[cid] = name
                if len(page) < _WIX_COLLECTIONS_PAGE_LIMIT:
                    break
                offset += len(page)
        except Exception as exc:  # noqa: BLE001 — see docstring: never fail the sync
            logger.warning(
                "Wix collections fetch failed merchant_id=%s error=%s — DISCARDING %s "
                "partial results; products will sync WITHOUT a category",
                merchant_id,
                exc,
                len(names),
            )
            return {}
        return names

    @staticmethod
    def _convert_product(
        wp: Dict[str, Any],
        merchant_id: str,
        collection_names: Optional[Dict[str, str]] = None,
    ) -> Optional[StandardProduct]:
        if not wp or not isinstance(wp, dict):
            return None

        price_data = wp.get("priceData") if wp else {}
        if not price_data:
            price_data = {}

        image_url = WixProductAdapter._first_image_url(wp.get("media"))
        inventory = WixProductAdapter._stock_to_inventory(wp.get("stock"))

        # Storefront identity (attributed-redirect lane): the agent gateway
        # serves StandardProduct(**products_cache.product_data) directly, so
        # these must land as TOP-LEVEL fields at sync time; the portal's
        # products-v2 mapper additionally reads platform_metadata
        # permalink/slug (see _apply_storefront_fields).
        page_url = WixProductAdapter._storefront_page_url(wp)
        slug = WixProductAdapter._storefront_slug(wp, page_url)

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
            v_barcode = WixProductAdapter._pick_wix_identifier(v_body, v)

            v_media = v_body.get("media") if isinstance(v_body.get("media"), dict) else v.get("media")
            v_image_url = WixProductAdapter._first_image_url(v_media) or image_url

            choices = WixProductAdapter._normalize_variant_choices(v.get("choices") or v_body.get("choices"))
            title = " / ".join([str(val) for val in choices.values()]) if choices else name

            variants_out.append(
                StandardProductVariant(
                    id=variant_id,
                    title=title,
                    sku=v_sku,
                    barcode=v_barcode,
                    price=v_price,
                    inventory_quantity=int(v_inventory),
                    options=choices or None,
                    image_url=v_image_url,
                    platform_metadata={"raw_wix_variant": v},
                )
            )

        # If no real variants, keep legacy behavior with a synthetic default variant.
        if not variants_out:
            product_identifier = WixProductAdapter._pick_wix_identifier(wp)
            variants_out = [
                StandardProductVariant(
                    id=str(product_id or sku or "default"),
                    title=name,
                    sku=sku,
                    barcode=product_identifier,
                    price=base_price,
                    inventory_quantity=int(inventory),
                    image_url=image_url,
                    platform_metadata={"raw_wix_product": wp},
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

        # Phase O-1 followup: explicitly pass tags=[] so downstream sees
        # "ingest saw the feed and it was empty" rather than "field
        # missing entirely". Wix v3 API does not expose a clean tags
        # concept (unlike Shopify product.tags). Future Phase O-3 will
        # have a LabelAgent fill tags from product description /
        # additionalInfoSections content. For now: empty list, not None.
        product = StandardProduct(
            id=product_id,
            title=name,
            description=str(wp.get("description", "")),
            price=base_price,
            currency=currency,
            inventory_quantity=int(inventory),
            sku=sku,
            barcode=variants_out[0].barcode if len(variants_out) == 1 else WixProductAdapter._pick_wix_identifier(wp),
            image_url=image_url,
            platform="wix",
            # Wix stores-reader v1 carries the product's brand as a top-level
            # string; until 2026-07-29 this adapter dropped it entirely — the
            # ONLY StandardProduct field the identity layer cannot live
            # without. catalog_sync derives brand from `product.vendor or
            # metadata.brand`, and `make_content_key(brand, title, barcode)`
            # yields NO content_key for a brandless row, which cascades:
            # content_key NULL -> no index_pipeline_state row possible (PK
            # content_key) -> no eligibility -> gateway gate 1 fails closed ->
            # the row can never serve, score, or be elected, silently.
            # Measured on the 2026-07-29 Wix pilot: 20/20 rows arrived
            # brandless with content_key NULL. Shopify has always mapped
            # `vendor`; this brings Wix to parity. Empty/whitespace collapses
            # to None so a blank Wix field behaves like an absent one.
            vendor=str(wp.get("brand") or "").strip() or None,
            # Wix keeps categories in COLLECTIONS, a resource this adapter never
            # called until 2026-07-29, so `product_type` was always None. That is
            # not cosmetic: `product_quality_service` scores
            # `brand_category = 1.0 if brand_present and category_present else 0.0`
            # as one of six equal-weight components, so a category-less Wix row
            # forfeits 16.7 points outright. Measured on the Wix pilot: scoring
            # capped at 4-of-6 = 66.7 against a 71.4 floor — every product
            # unservable, for a field the store already had. Writing
            # product_type by hand onto those 20 rows moved them to 83.3 and
            # 14/14 serving_eligible, which is the experiment this makes
            # permanent. Shopify has always mapped `product_type`; this brings
            # Wix to parity.
            product_type=WixProductAdapter._pick_wix_product_type(wp, collection_names),
            merchant_id=merchant_id,
            status=ProductStatus.ACTIVE,
            variants=variants_out,
            tags=[],
            orderable=is_orderable,
            created_at=wp.get("dateCreated"),
            updated_at=wp.get("lastUpdated"),
            handle=slug,
            online_store_url=page_url,
            platform_metadata={
                "permalink": page_url,
                "slug": slug,
            },
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
        try:
            logger.info(f"🔄 Fetching Wix products: site_id={site_id}")
            
            url = WIX_PRODUCTS_QUERY_URL
            headers = build_wix_catalog_headers(api_key, site_id)
            page_limit = max(1, min(_as_int(limit, 50), 100))
            max_total_products = max(1, _as_int(WIX_MAX_PRODUCTS, 5000))
            offset = max(_as_int(page_token, 0), 0)
            max_pages_override = _wix_max_pages_override()
            max_pages = max_pages_override or max(
                1,
                (max_total_products + page_limit - 1) // page_limit,
            )
            single_page_rollback = max_pages_override == 1

            standard_products: List[StandardProduct] = []
            total_raw_products = 0
            pages_fetched = 0
            next_page_token = None
            truncated = False

            products_with_collection_ids = 0
            products_with_product_type = 0

            async with httpx.AsyncClient(timeout=30.0) as client:
                # Once per sync, not per product. Must precede the product loop
                # because every converted row reads it.
                collection_names = await WixProductAdapter._fetch_wix_collection_names(
                    client, headers, merchant_id
                )
                logger.info(
                    "Wix collections merchant_id=%s resolved=%s",
                    merchant_id,
                    len(collection_names),
                )

                while total_raw_products < max_total_products and pages_fetched < max_pages:
                    current_limit = min(page_limit, max_total_products - total_raw_products)
                    payload = {
                        "query": {"paging": {"limit": current_limit, "offset": offset}},
                        "includeVariants": True,
                    }

                    try:
                        response = await client.post(url, json=payload, headers=headers)
                    except Exception as exc:
                        error_msg = f"Error fetching Wix products: {str(exc)}"
                        logger.error(error_msg)
                        return standard_products, None, error_msg

                    logger.info(
                        "Wix API response merchant_id=%s offset=%s limit=%s status=%s content_length=%s",
                        merchant_id,
                        offset,
                        current_limit,
                        response.status_code,
                        len(response.text),
                    )

                    if response.status_code != 200:
                        error_msg = f"Wix API error: {response.status_code} - {response.text[:200]}"
                        logger.error(error_msg)
                        return standard_products, None, error_msg

                    try:
                        data = response.json() or {}
                    except Exception as exc:
                        error_msg = f"Invalid Wix API response: {str(exc)}"
                        logger.error(error_msg)
                        return standard_products, None, error_msg

                    wix_products = data.get("products", [])
                    if not isinstance(wix_products, list):
                        error_msg = "Invalid Wix API response: missing products list"
                        logger.error(error_msg)
                        return standard_products, None, error_msg

                    page_count = len(wix_products)
                    total_results = _as_int(data.get("totalResults"), 0) or None
                    logger.info(
                        "Wix API page merchant_id=%s offset=%s page_count=%s total_fetched=%s total_results=%s",
                        merchant_id,
                        offset,
                        page_count,
                        total_raw_products + page_count,
                        total_results if total_results is not None else "unknown",
                    )

                    for wp in wix_products:
                        try:
                            product = WixProductAdapter._convert_product(
                                wp,
                                merchant_id=merchant_id,
                                collection_names=collection_names,
                            )
                            if product:
                                standard_products.append(product)
                                if WixProductAdapter._extract_wix_collection_ids(wp):
                                    products_with_collection_ids += 1
                                if product.product_type:
                                    products_with_product_type += 1
                        except Exception as product_error:
                            logger.error(f"Error converting Wix product: {product_error}")
                            continue

                    pages_fetched += 1
                    total_raw_products += page_count
                    next_offset = offset + page_count
                    has_more = page_count >= current_limit and (
                        total_results is None or next_offset < total_results
                    )

                    if not has_more:
                        break

                    if total_raw_products >= max_total_products or pages_fetched >= max_pages:
                        truncated = True
                        if not single_page_rollback:
                            next_page_token = str(next_offset)
                        break

                    offset = next_offset

            logger.info(
                "Wix products fetch complete merchant_id=%s total_fetched=%s pages=%s truncated=%s next_page_token=%s",
                merchant_id,
                len(standard_products),
                pages_fetched,
                truncated,
                next_page_token,
            )

            # CANARY. Category mapping fails SILENTLY by construction: every
            # failure mode — wrong endpoint, renamed field, revoked permission,
            # a store with no collections — produces `product_type=None`, which
            # is byte-identical to the bug this replaced. Nothing downstream
            # raises; the products merely score 16.7 lower and never serve, the
            # way they did for months without anyone noticing.
            #
            # So emit a reading that distinguishes the cases, and make the one
            # that means "the wiring is broken" a WARNING rather than a silence:
            # products carrying collection ids while none resolved to a name is
            # not a store without categories, it is a lookup that did not work.
            if standard_products:
                logger.info(
                    "Wix category mapping merchant_id=%s products=%s with_collection_ids=%s "
                    "with_product_type=%s collections_resolved=%s",
                    merchant_id,
                    len(standard_products),
                    products_with_collection_ids,
                    products_with_product_type,
                    len(collection_names),
                )
                if products_with_collection_ids and not products_with_product_type:
                    logger.warning(
                        "Wix category mapping RESOLVED NOTHING merchant_id=%s: %s products carry "
                        "collection ids but none matched a collection name (collections_resolved=%s). "
                        "brand_category will score 0 for every row — check the collections "
                        "endpoint, credentials scope, and the collectionIds field name.",
                        merchant_id,
                        products_with_collection_ids,
                        len(collection_names),
                    )
                elif collection_names and not products_with_collection_ids:
                    # The OTHER silent failure, and the likelier one: the store
                    # has real collections but no product appears to belong to
                    # any of them. A store genuinely organised that way is
                    # possible; a `collectionIds` field RENAMED to a spelling
                    # outside our accepted aliases looks exactly the same from
                    # here — and the branch above structurally cannot see it,
                    # because it requires products_with_collection_ids to be
                    # non-zero, which is precisely what a rename drives to zero.
                    logger.warning(
                        "Wix category mapping SAW NO COLLECTION IDS merchant_id=%s: %s collections "
                        "resolved but 0 of %s products carry collection ids. Either this store "
                        "assigns no products to collections, or the product field changed name "
                        "(accepted: collectionIds / collection_ids / collectionIDs). "
                        "brand_category will score 0 for every row.",
                        merchant_id,
                        len(collection_names),
                        len(standard_products),
                    )

            return standard_products, next_page_token, None
                
        except Exception as e:
            error_msg = f"Error fetching Wix products: {str(e)}"
            logger.error(error_msg)
            return [], None, error_msg


class WooCommerceProductAdapter:
    """WooCommerce 产品适配器：WooCommerce API → StandardProduct"""

    @staticmethod
    def _variation_inventory(raw_variation: Dict[str, Any]) -> int:
        quantity = _as_int(raw_variation.get("stock_quantity"))
        manage_stock = bool(raw_variation.get("manage_stock"))
        stock_status = str(raw_variation.get("stock_status") or "").strip().lower()
        if quantity > 0:
            return quantity
        if stock_status == "instock" and not manage_stock:
            return 1
        return 0

    @staticmethod
    def _variation_title(raw_variation: Dict[str, Any], options: Dict[str, str]) -> str:
        if options:
            return " / ".join(value for value in options.values() if value)
        explicit = str(raw_variation.get("name") or "").strip()
        if explicit:
            return explicit
        variation_id = str(raw_variation.get("id") or "").strip()
        return variation_id or "Variation"

    @staticmethod
    def _convert_variation(
        raw_variation: Dict[str, Any],
        default_image_url: Optional[str] = None,
    ) -> StandardProductVariant:
        option_values: Dict[str, str] = {}
        for attribute in raw_variation.get("attributes") or []:
            if not isinstance(attribute, dict):
                continue
            option_name = str(
                attribute.get("name")
                or attribute.get("slug")
                or attribute.get("id")
                or ""
            ).strip()
            option_value = str(attribute.get("option") or "").strip()
            if option_name and option_value:
                option_values[option_name] = option_value

        price = _as_float(
            raw_variation.get("price")
            or raw_variation.get("sale_price")
            or raw_variation.get("regular_price")
        )
        regular_price = _as_float(raw_variation.get("regular_price"))
        compare_at = regular_price if regular_price > price > 0 else None
        image_payload = raw_variation.get("image") or {}
        image_url = (
            image_payload.get("src")
            if isinstance(image_payload, dict)
            else None
        ) or default_image_url

        return StandardProductVariant(
            id=str(raw_variation.get("id")),
            title=WooCommerceProductAdapter._variation_title(raw_variation, option_values),
            sku=str(raw_variation.get("sku") or "").strip() or None,
            price=price,
            compare_at_price=compare_at,
            inventory_quantity=WooCommerceProductAdapter._variation_inventory(raw_variation),
            options=option_values or None,
            image_url=image_url,
            platform_metadata={
                "status": raw_variation.get("status"),
                "stock_status": raw_variation.get("stock_status"),
            },
        )

    @staticmethod
    async def _fetch_variations(
        *,
        client: httpx.AsyncClient,
        store_url: str,
        consumer_key: str,
        consumer_secret: str,
        product_id: str,
    ) -> List[Dict[str, Any]]:
        variations: List[Dict[str, Any]] = []
        page = 1
        while True:
            response = await client.get(
                f"{store_url}/wp-json/wc/v3/products/{product_id}/variations",
                params={
                    "consumer_key": consumer_key,
                    "consumer_secret": consumer_secret,
                    "per_page": 100,
                    "page": page,
                },
            )
            if response.status_code != 200:
                logger.warning(
                    "WooCommerce variations fetch failed for product %s: %s",
                    product_id,
                    response.status_code,
                )
                break

            batch = response.json() or []
            if not isinstance(batch, list) or not batch:
                break

            variations.extend([item for item in batch if isinstance(item, dict)])
            total_pages = _as_int(response.headers.get("X-WP-TotalPages"))
            if total_pages and page >= total_pages:
                break
            if len(batch) < 100:
                break
            page += 1

        return variations

    @staticmethod
    def _convert_product(
        raw_product: Dict[str, Any],
        merchant_id: str,
        variations: Optional[List[Dict[str, Any]]] = None,
    ) -> StandardProduct:
        image_urls = [
            str(item.get("src") or "").strip()
            for item in raw_product.get("images") or []
            if isinstance(item, dict) and str(item.get("src") or "").strip()
        ]
        default_image_url = image_urls[0] if image_urls else None

        variation_models: List[StandardProductVariant] = []
        for raw_variation in variations or []:
            try:
                variation_models.append(
                    WooCommerceProductAdapter._convert_variation(
                        raw_variation,
                        default_image_url=default_image_url,
                    )
                )
            except Exception as exc:
                logger.warning("Skipping WooCommerce variation: %s", exc)

        default_price = _as_float(
            raw_product.get("price")
            or raw_product.get("sale_price")
            or raw_product.get("regular_price")
        )
        if variation_models:
            sellable_prices = [variant.price for variant in variation_models if variant.price > 0]
            if sellable_prices:
                default_price = min(sellable_prices)

        regular_price = _as_float(raw_product.get("regular_price"))
        compare_at = regular_price if regular_price > default_price > 0 else None
        inventory_quantity = _as_int(raw_product.get("stock_quantity"))
        manage_stock = bool(raw_product.get("manage_stock"))
        stock_status = str(raw_product.get("stock_status") or "").strip().lower()
        if variation_models:
            inventory_quantity = sum(max(variant.inventory_quantity, 0) for variant in variation_models)
        elif inventory_quantity <= 0 and stock_status == "instock" and not manage_stock:
            inventory_quantity = 1

        status_raw = str(raw_product.get("status") or "").strip().lower()
        if status_raw == "publish":
            status = ProductStatus.ACTIVE
        elif status_raw in {"trash", "private"}:
            status = ProductStatus.ARCHIVED
        else:
            status = ProductStatus.DRAFT

        is_catalog_visible = str(raw_product.get("catalog_visibility") or "").strip().lower() != "hidden"
        has_sellable_variant = any(
            variant.price > 0 and variant.inventory_quantity > 0
            for variant in variation_models
        )
        is_orderable = bool(
            status == ProductStatus.ACTIVE
            and default_price > 0
            and is_catalog_visible
            and (
                has_sellable_variant
                or inventory_quantity > 0
                or (stock_status == "instock" and not manage_stock)
            )
        )

        product = StandardProduct(
            id=str(raw_product.get("id")),
            platform="woocommerce",
            merchant_id=merchant_id,
            title=str(raw_product.get("name") or "Untitled").strip() or "Untitled",
            description=str(
                raw_product.get("description")
                or raw_product.get("short_description")
                or ""
            ),
            product_type=str(raw_product.get("type") or "").strip() or None,
            tags=_split_tags(raw_product.get("tags")),
            price=default_price,
            compare_at_price=compare_at,
            currency="USD",
            inventory_quantity=inventory_quantity,
            sku=(
                str(raw_product.get("sku") or "").strip()
                or next((variant.sku for variant in variation_models if variant.sku), None)
            ),
            image_url=default_image_url,
            images=image_urls,
            variants=variation_models,
            status=status,
            created_at=_parse_platform_datetime(
                raw_product.get("date_created_gmt") or raw_product.get("date_created")
            ),
            updated_at=_parse_platform_datetime(
                raw_product.get("date_modified_gmt") or raw_product.get("date_modified")
            ),
            in_stock=is_orderable,
            orderable=is_orderable,
            platform_metadata={
                "catalog_visibility": raw_product.get("catalog_visibility"),
                "permalink": raw_product.get("permalink"),
                "slug": raw_product.get("slug"),
                "stock_status": raw_product.get("stock_status"),
                "type": raw_product.get("type"),
            },
        )

        from models.standard_product import validate_orderable

        orderable, validation = validate_orderable(product)
        product.orderable = orderable
        product.orderable_validation = validation
        return product

    @staticmethod
    async def fetch_product_by_id(
        *,
        store_url: str,
        consumer_key: str,
        consumer_secret: str,
        merchant_id: str,
        product_id: str,
    ) -> Tuple[Optional[StandardProduct], Optional[str]]:
        store_url = normalize_woocommerce_store_url(store_url)
        product_id_str = str(product_id or "").strip()
        if not store_url or not consumer_key or not consumer_secret or not product_id_str:
            return None, "Missing WooCommerce store/product credentials"

        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                response = await client.get(
                    f"{store_url}/wp-json/wc/v3/products/{product_id_str}",
                    params={
                        "consumer_key": consumer_key,
                        "consumer_secret": consumer_secret,
                    },
                )
                if response.status_code == 404:
                    return None, "NOT_FOUND"
                if response.status_code != 200:
                    return None, f"WooCommerce API error: {response.status_code} - {response.text[:200]}"

                raw_product = response.json() or {}
                variations: List[Dict[str, Any]] = []
                if (
                    isinstance(raw_product, dict)
                    and str(raw_product.get("type") or "").strip().lower() == "variable"
                    and raw_product.get("variations")
                ):
                    variations = await WooCommerceProductAdapter._fetch_variations(
                        client=client,
                        store_url=store_url,
                        consumer_key=consumer_key,
                        consumer_secret=consumer_secret,
                        product_id=product_id_str,
                    )

            if not isinstance(raw_product, dict):
                return None, "Invalid WooCommerce response: missing product"

            return (
                WooCommerceProductAdapter._convert_product(
                    raw_product,
                    merchant_id=merchant_id,
                    variations=variations,
                ),
                None,
            )
        except Exception as exc:
            return None, f"Failed to fetch WooCommerce product: {str(exc)}"
    
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
        normalized_store_url = normalize_woocommerce_store_url(store_url)
        if not normalized_store_url or not consumer_key or not consumer_secret:
            return [], None, "WooCommerce credentials incomplete"

        page = max(_as_int(page_token, 1), 1)
        per_page = max(1, min(limit, 100))

        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                response = await client.get(
                    f"{normalized_store_url}/wp-json/wc/v3/products",
                    params={
                        "consumer_key": consumer_key,
                        "consumer_secret": consumer_secret,
                        "per_page": per_page,
                        "page": page,
                    },
                )

                if response.status_code != 200:
                    error_msg = f"WooCommerce API error: {response.status_code} - {response.text[:200]}"
                    logger.error(error_msg)
                    return [], None, error_msg

                raw_products = response.json() or []
                if not isinstance(raw_products, list):
                    return [], None, "Invalid WooCommerce response: missing products list"

                standard_products: List[StandardProduct] = []
                for raw_product in raw_products:
                    if not isinstance(raw_product, dict):
                        continue
                    variations: List[Dict[str, Any]] = []
                    if (
                        str(raw_product.get("type") or "").strip().lower() == "variable"
                        and raw_product.get("variations")
                    ):
                        variations = await WooCommerceProductAdapter._fetch_variations(
                            client=client,
                            store_url=normalized_store_url,
                            consumer_key=consumer_key,
                            consumer_secret=consumer_secret,
                            product_id=str(raw_product.get("id")),
                        )
                    try:
                        standard_products.append(
                            WooCommerceProductAdapter._convert_product(
                                raw_product,
                                merchant_id=merchant_id,
                                variations=variations,
                            )
                        )
                    except Exception as product_error:
                        logger.error("Error converting WooCommerce product: %s", product_error)

                total_pages = _as_int(response.headers.get("X-WP-TotalPages"))
                next_page_token = None
                if total_pages and page < total_pages:
                    next_page_token = str(page + 1)
                elif len(raw_products) == per_page and raw_products:
                    next_page_token = str(page + 1)

                return standard_products, next_page_token, None
        except Exception as exc:
            error_msg = f"Error fetching WooCommerce products: {str(exc)}"
            logger.error(error_msg)
            return [], None, error_msg


class BigCommerceProductAdapter:
    """BigCommerce 产品适配器：BigCommerce API → StandardProduct"""

    @staticmethod
    def _image_urls(raw_product: Dict[str, Any]) -> List[str]:
        urls: List[str] = []
        for image in raw_product.get("images") or []:
            if not isinstance(image, dict):
                continue
            url = (
                image.get("url_standard")
                or image.get("url_zoom")
                or image.get("url_thumbnail")
            )
            candidate = str(url or "").strip()
            if candidate and candidate not in urls:
                urls.append(candidate)
        return urls

    @staticmethod
    def _variant_inventory(raw_variant: Dict[str, Any]) -> int:
        quantity = _as_int(raw_variant.get("inventory_level"))
        inventory_tracking = str(raw_variant.get("inventory_tracking") or "").strip().lower()
        if quantity > 0:
            return quantity
        if inventory_tracking in {"", "none"}:
            return 1
        return 0

    @staticmethod
    def _convert_variant(
        raw_variant: Dict[str, Any],
        default_image_url: Optional[str] = None,
    ) -> StandardProductVariant:
        option_values: Dict[str, str] = {}
        for option in raw_variant.get("option_values") or []:
            if not isinstance(option, dict):
                continue
            option_name = str(
                option.get("option_display_name")
                or option.get("display_name")
                or option.get("option_name")
                or ""
            ).strip()
            option_value = str(
                option.get("label")
                or option.get("option_value")
                or option.get("value")
                or ""
            ).strip()
            if option_name and option_value:
                option_values[option_name] = option_value

        price = _as_float(raw_variant.get("calculated_price") or raw_variant.get("price"))
        retail_price = _as_float(raw_variant.get("retail_price"))
        compare_at = retail_price if retail_price > price > 0 else None
        title = " / ".join(value for value in option_values.values() if value)
        if not title:
            title = str(raw_variant.get("sku") or raw_variant.get("id") or "Variant").strip()

        return StandardProductVariant(
            id=str(raw_variant.get("id")),
            title=title,
            sku=str(raw_variant.get("sku") or "").strip() or None,
            price=price,
            compare_at_price=compare_at,
            inventory_quantity=BigCommerceProductAdapter._variant_inventory(raw_variant),
            options=option_values or None,
            image_url=str(raw_variant.get("image_url") or "").strip() or default_image_url,
            platform_metadata={
                "inventory_tracking": raw_variant.get("inventory_tracking"),
                "is_visible": raw_variant.get("is_visible"),
            },
        )

    @staticmethod
    def _convert_product(raw_product: Dict[str, Any], merchant_id: str) -> StandardProduct:
        image_urls = BigCommerceProductAdapter._image_urls(raw_product)
        default_image_url = image_urls[0] if image_urls else None

        variants: List[StandardProductVariant] = []
        for raw_variant in raw_product.get("variants") or []:
            if not isinstance(raw_variant, dict):
                continue
            try:
                variants.append(
                    BigCommerceProductAdapter._convert_variant(
                        raw_variant,
                        default_image_url=default_image_url,
                    )
                )
            except Exception as exc:
                logger.warning("Skipping BigCommerce variant: %s", exc)

        default_price = _as_float(
            raw_product.get("price")
            or raw_product.get("sale_price")
            or raw_product.get("retail_price")
        )
        if variants:
            sellable_prices = [variant.price for variant in variants if variant.price > 0]
            if sellable_prices:
                default_price = min(sellable_prices)

        retail_price = _as_float(raw_product.get("retail_price"))
        compare_at = retail_price if retail_price > default_price > 0 else None
        inventory_quantity = _as_int(raw_product.get("inventory_level"))
        inventory_tracking = str(raw_product.get("inventory_tracking") or "").strip().lower()
        if variants:
            inventory_quantity = sum(max(variant.inventory_quantity, 0) for variant in variants)
        elif inventory_quantity <= 0 and inventory_tracking in {"", "none"}:
            inventory_quantity = 1

        is_visible = bool(raw_product.get("is_visible", True))
        availability = str(raw_product.get("availability") or "").strip().lower()
        has_sellable_variant = any(
            variant.price > 0 and variant.inventory_quantity > 0
            for variant in variants
        )
        is_orderable = bool(
            is_visible
            and availability != "disabled"
            and default_price > 0
            and (
                has_sellable_variant
                or inventory_quantity > 0
                or inventory_tracking in {"", "none"}
            )
        )

        tags = _split_tags(raw_product.get("keywords"))
        status = ProductStatus.ACTIVE if is_visible else ProductStatus.DRAFT

        product = StandardProduct(
            id=str(raw_product.get("id")),
            platform="bigcommerce",
            merchant_id=merchant_id,
            title=str(raw_product.get("name") or "Untitled").strip() or "Untitled",
            description=str(raw_product.get("description") or ""),
            vendor=str(raw_product.get("brand_name") or "").strip() or None,
            product_type=str(raw_product.get("type") or "").strip() or None,
            tags=tags,
            price=default_price,
            compare_at_price=compare_at,
            currency="USD",
            inventory_quantity=inventory_quantity,
            sku=(
                str(raw_product.get("sku") or "").strip()
                or next((variant.sku for variant in variants if variant.sku), None)
            ),
            image_url=default_image_url,
            images=image_urls,
            variants=variants,
            status=status,
            created_at=_parse_platform_datetime(raw_product.get("date_created")),
            updated_at=_parse_platform_datetime(raw_product.get("date_modified")),
            in_stock=is_orderable,
            orderable=is_orderable,
            platform_metadata={
                "availability": raw_product.get("availability"),
                "custom_url": raw_product.get("custom_url"),
                "inventory_tracking": raw_product.get("inventory_tracking"),
                "is_visible": raw_product.get("is_visible"),
            },
        )

        from models.standard_product import validate_orderable

        orderable, validation = validate_orderable(product)
        product.orderable = orderable
        product.orderable_validation = validation
        return product

    @staticmethod
    async def fetch_product_by_id(
        *,
        store_hash: str,
        access_token: str,
        client_id: Optional[str],
        merchant_id: str,
        product_id: str,
    ) -> Tuple[Optional[StandardProduct], Optional[str]]:
        normalized_store_hash = normalize_bigcommerce_store_hash(store_hash)
        product_id_str = str(product_id or "").strip()
        if not normalized_store_hash or not access_token or not product_id_str:
            return None, "Missing BigCommerce store/product credentials"

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(
                    f"https://api.bigcommerce.com/stores/{normalized_store_hash}/v3/catalog/products/{product_id_str}",
                    params={"include": "images,variants"},
                    headers=build_bigcommerce_headers(access_token, client_id),
                )

            if response.status_code == 404:
                return None, "NOT_FOUND"
            if response.status_code != 200:
                return None, f"BigCommerce API error: {response.status_code} - {response.text[:200]}"

            payload = response.json() or {}
            raw_product = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(raw_product, dict):
                return None, "Invalid BigCommerce response: missing product"

            return BigCommerceProductAdapter._convert_product(raw_product, merchant_id), None
        except Exception as exc:
            return None, f"Failed to fetch BigCommerce product: {str(exc)}"

    @staticmethod
    async def fetch_products(
        store_hash: str,
        access_token: str,
        client_id: Optional[str],
        merchant_id: str,
        limit: int = 50,
        page_token: Optional[str] = None,
    ) -> Tuple[List[StandardProduct], Optional[str], Optional[str]]:
        normalized_store_hash = normalize_bigcommerce_store_hash(store_hash)
        if not normalized_store_hash or not access_token:
            return [], None, "BigCommerce credentials incomplete"

        page = max(_as_int(page_token, 1), 1)
        per_page = max(1, min(limit, 250))

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(
                    f"https://api.bigcommerce.com/stores/{normalized_store_hash}/v3/catalog/products",
                    params={
                        "page": page,
                        "limit": per_page,
                        "include": "images,variants",
                    },
                    headers=build_bigcommerce_headers(access_token, client_id),
                )

            if response.status_code != 200:
                error_msg = f"BigCommerce API error: {response.status_code} - {response.text[:200]}"
                logger.error(error_msg)
                return [], None, error_msg

            payload = response.json() or {}
            raw_products = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(raw_products, list):
                return [], None, "Invalid BigCommerce response: missing products list"

            standard_products: List[StandardProduct] = []
            for raw_product in raw_products:
                if not isinstance(raw_product, dict):
                    continue
                try:
                    standard_products.append(
                        BigCommerceProductAdapter._convert_product(raw_product, merchant_id)
                    )
                except Exception as product_error:
                    logger.error("Error converting BigCommerce product: %s", product_error)

            meta = payload.get("meta") if isinstance(payload, dict) else {}
            pagination = meta.get("pagination") if isinstance(meta, dict) else {}
            current_page = _as_int(pagination.get("current_page"), page)
            total_pages = _as_int(pagination.get("total_pages"))
            next_page_token = None
            if total_pages and current_page < total_pages:
                next_page_token = str(current_page + 1)

            return standard_products, next_page_token, None
        except Exception as exc:
            error_msg = f"Error fetching BigCommerce products: {str(exc)}"
            logger.error(error_msg)
            return [], None, error_msg


# 适配器工厂
PLATFORM_ADAPTERS = {
    "shopify": ShopifyProductAdapter,
    "wix": WixProductAdapter,
    "woocommerce": WooCommerceProductAdapter,
    "bigcommerce": BigCommerceProductAdapter,
    "cafe24": Cafe24ProductAdapter,
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
        platform: shopify, wix, woocommerce, bigcommerce
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
    elif platform == "bigcommerce":
        return await adapter_class.fetch_products(
            store_hash=credentials.get("store_hash"),
            access_token=credentials.get("access_token"),
            client_id=credentials.get("client_id"),
            merchant_id=merchant_id,
            limit=limit,
            page_token=page_token,
        )
    elif platform == "cafe24":
        return await adapter_class.fetch_products(
            mall_id=credentials.get("mall_id"),
            access_token=credentials.get("access_token"),
            merchant_id=merchant_id,
            limit=limit,
            page_token=page_token,
            shop_no=credentials.get("shop_no", 1),
            currency=credentials.get("currency", "KRW"),
            api_version=credentials.get("api_version", "2026-03-01"),
            refresh_token=credentials.get("refresh_token"),
            expires_at=credentials.get("expires_at"),
            store_id=credentials.get("store_id"),
        )
    else:
        return [], None, f"Platform {platform} not implemented"
