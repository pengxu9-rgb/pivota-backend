"""
Shopping AI Gateway

High-level, LLM/Agent-friendly operations on top of the existing product/order APIs.

Currently supports:
- find_products
- get_product_detail
- create_order       (proxied to Agent API)
- submit_payment     (proxied to Agent API)
- find_similar_products

Path: POST /agent/shop/v1/invoke
"""

import json
import logging
import os
import re
from collections import Counter
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field

from services.product_query_service import get_products_hybrid
from services.similarity_service import (
    SimilarityService,
    SimilarityStrategy,
    SimilarCandidate,
    similarity_service,
)
from services.similarity_config import get_similarity_scoring_weights
from models.standard_product import StandardProduct, ProductStatus

AGENT_API_BASE = os.getenv("AGENT_API_BASE", "https://web-production-fedb.up.railway.app").rstrip("/")
AGENT_API_KEY = os.getenv("SHOP_GATEWAY_AGENT_API_KEY") or os.getenv("PIVOTA_API_KEY") or os.getenv("AGENT_API_KEY")

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/agent/shop/v1", tags=["Shopping Gateway"])
DEV_MODE = os.getenv("APP_ENV", "dev") != "production"


class SearchFilters(BaseModel):
    merchant_id: str = Field(..., description="Merchant ID")
    query: str = Field("", description="Search query, empty string means 'all products'")
    category: Optional[str] = Field(None, description="Optional category filter")
    price_min: Optional[float] = Field(None, description="Minimum price filter")
    price_max: Optional[float] = Field(None, description="Maximum price filter")
    page: int = Field(1, ge=1, description="Page number (1-based)")
    # Allow larger requested limits; internal logic will clamp to safe bounds.
    limit: int = Field(20, ge=1, le=500, description="Page size (max 500; internally clamped)")


class FindProductsPayload(BaseModel):
    search: SearchFilters

class MultiSearchFilters(BaseModel):
    query: str = Field("", description="Search query, empty string means 'all products'")
    category: Optional[str] = Field(None, description="Optional category filter")
    price_min: Optional[float] = Field(None, description="Minimum price filter")
    price_max: Optional[float] = Field(None, description="Maximum price filter")
    page: int = Field(1, ge=1, description="Page number (1-based)")
    # Front-ends may request up to 500; we still clamp internally.
    limit: int = Field(20, ge=1, le=500, description="Page size (max 500; internally clamped)")
    in_stock_only: bool = Field(False, description="Return only in-stock products when true")


class UserIntent(BaseModel):
    id: Optional[str] = Field(None, description="Accounts user id or email if available")
    email: Optional[str] = Field(None, description="Optional explicit email")
    recent_queries: List[str] = Field(default_factory=list, description="Recent free-text queries from the user")


class RequestMetadata(BaseModel):
    creator_id: Optional[str] = Field(None, description="Creator id for contextual recommendations")
    creator_name: Optional[str] = Field(None, description="Human friendly creator name")
    source: Optional[str] = Field(None, description="Calling surface (e.g. creator-agent-ui)")
    trace_id: Optional[str] = Field(None, description="Optional trace id for observability")


class FindProductsMultiPayload(BaseModel):
    search: MultiSearchFilters
    user: Optional[UserIntent] = None
    metadata: Optional[RequestMetadata] = None


class SimilarUserContext(BaseModel):
    id: Optional[str] = Field(None, description="Accounts user id or email if available")
    recent_queries: List[str] = Field(default_factory=list, description="Recent free-text queries from the user")
    segments: List[str] = Field(default_factory=list, description="User segments for personalization")


class FindSimilarProductsPayload(BaseModel):
    product_id: str = Field(..., description="Base product id to find similar products for")
    creator_id: Optional[str] = Field(None, description="Optional creator context to scope results")
    limit: int = Field(6, ge=1, le=30, description="Max similar products to return (default 6, max 30)")
    strategy: Optional[SimilarityStrategy] = Field(None, description="Similarity strategy to use; defaults to auto")
    user: Optional[SimilarUserContext] = None
    locale: Optional[str] = None
    currency: Optional[str] = None
    metadata: Optional[RequestMetadata] = None
    debug: Optional[bool] = Field(False, description="Enable debug scores in dev environments")

class ProductRef(BaseModel):
    merchant_id: str
    product_id: str


class GetProductDetailPayload(BaseModel):
    product: ProductRef


class OrderItem(BaseModel):
    merchant_id: str
    product_id: str
    product_title: str
    quantity: int
    unit_price: float
    subtotal: float


class ShippingAddress(BaseModel):
    name: str
    address_line1: str
    address_line2: Optional[str] = ""
    city: str
    country: str
    postal_code: str
    phone: Optional[str] = None


class OrderPayloadBody(BaseModel):
    merchant_id: str
    customer_email: str
    items: List[OrderItem]
    shipping_address: ShippingAddress
    customer_notes: Optional[str] = None


class CreateOrderPayload(BaseModel):
    order: OrderPayloadBody


class PaymentPayloadBody(BaseModel):
    """
    Payload for submit_payment operation.

    约定（供 LLM/前端使用）：
    {
      "payment": {
        "order_id": "ORD_xxx",
        "expected_amount": 59.0,  # 可选，主要用于前端自检
        "currency": "USD",        # 可选
        "payment_method": "stripe_checkout" | "card" | ...
      }
    }

    其中 payment_method 只是一个 hint，Gateway 会将其映射为
    Agent Payment API 需要的 PaymentMethod.type 字段。
    """
    order_id: str
    expected_amount: float
    currency: str
    payment_method: Optional[str] = None  # e.g. "stripe_checkout", "card"


class SubmitPaymentPayload(BaseModel):
    payment: PaymentPayloadBody


class ShopGatewayRequest(BaseModel):
    operation: str
    payload: Dict[str, Any]
    metadata: Dict[str, Any] = Field(default_factory=dict)


def _standard_to_shop_product(p: StandardProduct) -> Dict[str, Any]:
    """
    Map internal StandardProduct to Shopping AI product contract.
    """
    # Prefer explicit image_url, then first image in list
    image_url = p.image_url or (p.images[0] if p.images else None)

    base = {
        "id": p.product_id or p.id,
        "merchant_id": p.merchant_id,
        "title": p.title,
        "description": p.description or "",
        "price": p.price,
        "currency": p.currency,
        "image_url": image_url,
        "product_type": p.product_type,
        "inventory_quantity": p.inventory_quantity,
        "sku": p.sku,
        "platform": p.platform,
    }

    best_deal = getattr(p, "best_deal", None)
    all_deals = getattr(p, "all_deals", None)
    if p.platform_metadata:
        best_deal = best_deal or p.platform_metadata.get("best_deal")
        all_deals = all_deals or p.platform_metadata.get("all_deals")

    if best_deal is not None:
        base["best_deal"] = best_deal
    if all_deals:
        base["all_deals"] = all_deals

    return base


async def _load_product_by_id(product_id: str) -> Optional[StandardProduct]:
    """
    Load a single product from cache by product_id/platform_product_id.
    """
    from db.database import database

    queries = [
        """
        SELECT product_data
        FROM products_cache
        WHERE product_data->>'product_id' = :pid
        LIMIT 1
        """,
        """
        SELECT product_data
        FROM products_cache
        WHERE platform_product_id = :pid
        LIMIT 1
        """,
    ]
    for q in queries:
        try:
            row = await database.fetch_one(q, {"pid": product_id})
            if row and "product_data" in row:
                try:
                    return StandardProduct.parse_obj(row["product_data"])
                except Exception:
                    continue
        except Exception:
            continue
    return None


async def _load_products_by_ids(product_ids: List[str]) -> Dict[str, StandardProduct]:
    """
    Bulk load products by ids to minimize queries.
    """
    if not product_ids:
        return {}
    from db.database import database

    unique_ids = list({pid for pid in product_ids if pid})
    placeholders = ",".join([f":pid{i}" for i in range(len(unique_ids))])
    params = {f"pid{i}": pid for i, pid in enumerate(unique_ids)}

    query = f"""
    SELECT product_data
    FROM products_cache
    WHERE product_data->>'product_id' IN ({placeholders})
       OR platform_product_id IN ({placeholders})
    """
    result: Dict[str, StandardProduct] = {}
    try:
        rows = await database.fetch_all(query, params)
        for row in rows:
            try:
                sp = StandardProduct.parse_obj(row["product_data"])
                pid = sp.product_id or sp.id
                if pid:
                    result[pid] = sp
            except Exception:
                continue
    except Exception:
        pass
    return result


async def _handle_find_products(
    filters: SearchFilters,
    background_tasks: BackgroundTasks,
) -> Dict[str, Any]:
    """
    Implementation of the find_products operation.

    Contract (simplified):
    - Input: { search: { merchant_id, query, category?, price_min?, price_max?, page?, limit? } }
    - Output: { products: [...], total, page, page_size }
    """
    merchant_id = filters.merchant_id
    page = filters.page or 1
    limit = min(filters.limit or 20, 100)

    # To support pagination, fetch up to page * limit items (capped)
    # and slice in-memory. For now we cap to 500 for safety.
    raw_limit = min(page * limit, 500)

    # Use a fixed agent_id for logging/metrics
    agent_id = "shopping_ai_frontend"

    products, query_source, error = await get_products_hybrid(
        merchant_id=merchant_id,
        limit=raw_limit,
        agent_id=agent_id,
        background_tasks=background_tasks,
    )

    if error and not products:
        # Hybrid layer completely failed
        raise HTTPException(
            status_code=502,
            detail=f"Failed to fetch products for merchant {merchant_id}: {error}",
        )

    # In-memory filtering based on query/category/price
    filtered: List[StandardProduct] = products

    q = (filters.query or "").strip().lower()
    if q:
        def matches_query(prod: StandardProduct) -> bool:
            title = (prod.title or "").lower()
            desc = (prod.description or "").lower()
            ptype = (prod.product_type or "").lower()
            return q in title or q in desc or q in ptype

        filtered = [p for p in filtered if matches_query(p)]

    if filters.category:
        cat = filters.category.lower()

        def matches_category(prod: StandardProduct) -> bool:
            ptype = (prod.product_type or "").lower()
            return cat in ptype

        filtered = [p for p in filtered if matches_category(p)]

    if filters.price_min is not None:
        filtered = [p for p in filtered if p.price >= filters.price_min]

    if filters.price_max is not None:
        filtered = [p for p in filtered if p.price <= filters.price_max]

    total = len(filtered)

    # Pagination slice
    start_idx = (page - 1) * limit
    end_idx = start_idx + limit
    page_items = filtered[start_idx:end_idx]

    return {
        "products": [_standard_to_shop_product(p) for p in page_items],
        "total": total,
        "page": page,
        "page_size": len(page_items),
        "metadata": {
            "query_source": query_source,
            "fetched_at": datetime.utcnow().isoformat(),
        },
    }


async def _handle_find_products_multi(
    payload: FindProductsMultiPayload,
    request_metadata: Optional[Dict[str, Any]],
    background_tasks: BackgroundTasks,
) -> Dict[str, Any]:
    """
    Cross-merchant implementation of the find_products operation.

    Input:  { search: { query, category?, price_min?, price_max?, page?, limit? } }
    Output: { products: [...], total, page, page_size }
    """
    from db.database import database

    filters = payload.search
    user_ctx = payload.user
    creator_meta = payload.metadata or None
    # Prefer top-level metadata for creator context when provided by caller.
    if request_metadata:
        creator_meta = RequestMetadata(**request_metadata)

    creator_id = None
    creator_name = None
    if creator_meta:
        creator_id = creator_meta.creator_id
        creator_name = creator_meta.creator_name

    page = filters.page or 1
    limit = min(filters.limit or 20, 100)

    def _tokenize(text: str) -> List[str]:
        if not text:
            return []
        return [t for t in re.split(r"[^a-z0-9]+", text.lower()) if len(t) > 2]

    async def _load_user_history_signals() -> tuple[set[str], List[str]]:
        """Best-effort fetch of the user's historical purchases to bias ranking."""
        if not user_ctx:
            return set(), []

        uid = (user_ctx.id or "").strip()
        explicit_email = (user_ctx.email or "").strip()
        email_from_id = uid if "@" in uid and not explicit_email else ""

        if not uid and not explicit_email and not email_from_id:
            return set(), []

        query = """
            SELECT merchant_id, items
            FROM orders
            WHERE is_deleted IS NOT TRUE
              AND (
                (:uid <> '' AND (metadata->>'accounts_user_id' = :uid OR metadata->>'user_id' = :uid))
                OR (:email <> '' AND customer_email = :email)
                OR (:email_from_id <> '' AND customer_email = :email_from_id)
              )
            ORDER BY created_at DESC
            LIMIT 100
        """
        rows = await database.fetch_all(
            query,
            {
                "uid": uid,
                "email": explicit_email,
                "email_from_id": email_from_id,
            },
        )

        product_ids: set[str] = set()
        titles: List[str] = []
        for row in rows:
            raw_items = row.get("items") if isinstance(row, dict) else None
            if isinstance(raw_items, str):
                try:
                    raw_items = json.loads(raw_items)
                except Exception:
                    raw_items = None
            if not isinstance(raw_items, list):
                continue
            for item in raw_items:
                if not isinstance(item, dict):
                    continue
                pid = str(
                    item.get("product_id")
                    or item.get("id")
                    or item.get("platform_product_id")
                    or item.get("variant_id")
                    or ""
                ).strip()
                if pid:
                    product_ids.add(pid)
                if item.get("product_title"):
                    titles.append(str(item["product_title"]))
        return product_ids, titles

    async def _load_creator_top_sellers(max_candidates: int = 50) -> List[StandardProduct]:
        """Fetch top-selling products for a creator by mining order metadata."""
        if not creator_id:
            return []

        rows = await database.fetch_all(
            """
            SELECT merchant_id, items
            FROM orders
            WHERE is_deleted IS NOT TRUE
              AND (
                metadata->>'creator_id' = :creator_id
                OR metadata->>'creatorId' = :creator_id
              )
            ORDER BY created_at DESC
            LIMIT 400
            """,
            {"creator_id": creator_id},
        )

        popularity = Counter()
        for row in rows:
            merchant_id = row.get("merchant_id") if isinstance(row, dict) else None
            raw_items = row.get("items") if isinstance(row, dict) else None
            if not merchant_id:
                continue
            if isinstance(raw_items, str):
                try:
                    raw_items = json.loads(raw_items)
                except Exception:
                    raw_items = None
            if not isinstance(raw_items, list):
                continue
            for item in raw_items:
                if not isinstance(item, dict):
                    continue
                pid = str(
                    item.get("product_id")
                    or item.get("id")
                    or item.get("platform_product_id")
                    or ""
                ).strip()
                if not pid:
                    continue
                qty = int(item.get("quantity") or 1)
                popularity[(merchant_id, pid)] += max(qty, 1)

        if not popularity:
            return []

        async def _fetch_product(merchant_id: str, product_id: str) -> Optional[StandardProduct]:
            row = await database.fetch_one(
                """
                SELECT product_data
                FROM products_cache
                WHERE merchant_id = :merchant_id
                  AND (
                    platform_product_id = :pid
                    OR product_data->>'id' = :pid
                    OR product_data->>'product_id' = :pid
                  )
                ORDER BY cached_at DESC
                LIMIT 1
                """,
                {"merchant_id": merchant_id, "pid": product_id},
            )
            if not row:
                return None
            product_data = row.get("product_data") if isinstance(row, dict) else None
            if isinstance(product_data, str):
                try:
                    product_data = json.loads(product_data)
                except Exception:
                    return None
            if not isinstance(product_data, dict):
                return None
            try:
                product = StandardProduct(**product_data)
                product.merchant_id = merchant_id
                return product
            except Exception:
                return None

        products: List[StandardProduct] = []
        for (m_id, pid), _count in popularity.most_common(max_candidates * 2):
            prod = await _fetch_product(m_id, pid)
            if prod:
                products.append(prod)
            if len(products) >= max_candidates:
                break
        return products

    history_product_ids, history_titles = await _load_user_history_signals()
    history_terms = set()
    if user_ctx and user_ctx.recent_queries:
        for q_term in user_ctx.recent_queries:
            history_terms.update(_tokenize(q_term))
    for title in history_titles:
        history_terms.update(_tokenize(title))

    # Fetch candidate merchants (active + PSP connected)
    merchant_rows = await database.fetch_all(
        """
        SELECT merchant_id, business_name
        FROM merchant_onboarding
        WHERE status NOT IN ('deleted', 'rejected')
        AND psp_connected = true
        LIMIT 100
        """
    )
    merchant_map = {row["merchant_id"]: row["business_name"] for row in merchant_rows}

    if not merchant_map:
        return {
            "products": [],
            "total": 0,
            "page": page,
            "page_size": 0,
            "metadata": {
                "query_source": "cache_multi",
                "fetched_at": datetime.utcnow().isoformat(),
                "merchants_searched": 0,
            },
        }

    # Cold start: empty query falls back to creator top sellers.
    q = (filters.query or "").strip()
    if not q:
        top_sellers = await _load_creator_top_sellers(max_candidates=limit * 2)
        mapped = []
        for prod in top_sellers[: limit * page]:
            item = _standard_to_shop_product(prod)
            item["merchant_name"] = merchant_map.get(prod.merchant_id)
            mapped.append(item)

        start_idx = (page - 1) * limit
        page_items = mapped[start_idx : start_idx + limit]
        return {
            "products": page_items,
            "total": len(mapped),
            "page": page,
            "page_size": len(page_items),
            "reply": None,
            "metadata": {
                "query_source": "creator_top_sellers",
                "fetched_at": datetime.utcnow().isoformat(),
                "merchants_searched": len(merchant_map),
                "creator_id": creator_id,
                "creator_name": creator_name,
            },
        }

    # How many products to fetch per merchant (before global filtering/pagination)
    # We fetch a bit more than the requested page size to have headroom for filtering.
    per_merchant_limit = min(max(limit * 2, 20), 200)

    # Collect products as (StandardProduct, merchant_name) tuples
    merchant_products: list[tuple[StandardProduct, str]] = []
    for mid, name in merchant_map.items():
        try:
            products, _source, _error = await get_products_hybrid(
                merchant_id=mid,
                limit=per_merchant_limit,
                agent_id="shopping_ai_multi",
                background_tasks=background_tasks,
            )
            for p in products:
                merchant_products.append((p, name))
        except Exception:
            # Ignore individual merchant failures to keep cross-merchant search robust
            continue

    # In-memory filtering and simple relevance scoring (reuse Agent API logic)
    filtered_products: list[dict[str, Any]] = []
    q_lower = q.lower()

    for product, merchant_name in merchant_products:
        # Price filter
        if filters.price_min is not None and product.price < filters.price_min:
            continue
        if filters.price_max is not None and product.price > filters.price_max:
            continue

        # Category filter
        if filters.category:
            cat = filters.category.lower()
            product_category = (product.product_type or "").lower()
            if cat not in product_category:
                continue

        # In-stock filter (best-effort)
        if filters.in_stock_only:
            in_stock_flag = getattr(product, "in_stock", None)
            inventory_qty = product.inventory_quantity or 0
            if in_stock_flag is False or (in_stock_flag is None and inventory_qty <= 0):
                continue

        # Text relevance
        relevance_score = 1.0
        if q_lower:
            title = (product.title or "").lower()
            description = (product.description or "").lower()

            if q_lower in title:
                relevance_score = 1.0 if q_lower == title else 0.9
            elif q_lower in description:
                relevance_score = 0.7
            else:
                words = q_lower.split()
                matches = sum(1 for w in words if w in title or w in description)
                if matches == 0:
                    continue
                relevance_score = 0.5 + (matches / len(words)) * 0.3

        # User intent boost based on history and recency
        pid = str(product.product_id or product.id or "")
        history_boost = 0.0
        if pid and pid in history_product_ids:
            history_boost += 0.6
        if history_terms:
            blob = " ".join(
                [
                    (product.title or "").lower(),
                    (product.description or "").lower(),
                    (product.product_type or "").lower(),
                ]
            )
            matched_terms = sum(1 for term in history_terms if term and term in blob)
            if matched_terms:
                history_boost += min(0.5, matched_terms * 0.1)

        relevance_score += history_boost

        filtered_products.append(
            {
                "product": product,
                "merchant_name": merchant_name,
                "relevance_score": relevance_score,
            }
        )

    # Sort by relevance
    filtered_products.sort(
        key=lambda p: p.get("relevance_score", 0), reverse=True
    )

    total = len(filtered_products)
    start_idx = (page - 1) * limit
    end_idx = start_idx + limit
    page_items = filtered_products[start_idx:end_idx]

    # Map to Shopping contract; inject merchant_id into result
    out_products = []
    for item_wrapper in page_items:
        sp: StandardProduct = item_wrapper["product"]
        merchant_name = item_wrapper.get("merchant_name")

        item = _standard_to_shop_product(sp)
        # add merchant name if we have it
        item["merchant_name"] = merchant_name
        out_products.append(item)

    # Fallback: if primary query returned nothing, surface creator top-sellers instead
    if not out_products and creator_id:
        top_sellers = await _load_creator_top_sellers(max_candidates=limit * 2)
        mapped = []
        for prod in top_sellers[: limit * page]:
            item = _standard_to_shop_product(prod)
            item["merchant_name"] = merchant_map.get(prod.merchant_id)
            mapped.append(item)

        fallback_items = mapped[start_idx:end_idx]
        return {
            "products": fallback_items,
            "total": len(mapped),
            "page": page,
            "page_size": len(fallback_items),
            "reply": None,
            "metadata": {
                "query_source": "creator_top_sellers_fallback",
                "fetched_at": datetime.utcnow().isoformat(),
                "merchants_searched": len(merchant_map),
                "creator_id": creator_id,
                "creator_name": creator_name,
            },
        }

    history_used = bool(history_product_ids or history_terms)

    return {
        "products": out_products,
        "total": total,
        "page": page,
        "page_size": len(out_products),
        "metadata": {
            "query_source": "cache_multi_intent",
            "fetched_at": datetime.utcnow().isoformat(),
            "merchants_searched": len(merchant_map),
            "creator_id": creator_id,
            "creator_name": creator_name,
            "history_boost_applied": history_used,
        },
    }


async def _handle_find_similar_products(
    payload: FindSimilarProductsPayload,
    request_metadata: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Find similar products for a given base product.

    Example curl:
    curl -X POST https://<host>/agent/shop/v1/invoke \\
      -H 'Content-Type: application/json' \\
      -H 'Authorization: Bearer <PIVOTA_AGENT_API_KEY>' \\
      -d '{
        "operation": "find_similar_products",
        "payload": {
          "product_id": "prod_123",
          "limit": 6,
          "strategy": "auto",
          "user": { "id": "user_789" }
        },
        "metadata": { "creator_id": "creator_456", "source": "creator-agent-ui" }
      }'
    """
    limit = min(payload.limit or 6, 30)

    base_product = await _load_product_by_id(payload.product_id)
    if not base_product:
        raise HTTPException(status_code=404, detail="Base product not found")

    # Merge creator context (payload overrides metadata)
    creator_id = payload.creator_id
    creator_name = None
    source = None
    trace_id = None
    meta_from_payload = payload.metadata
    meta_from_request = None
    if request_metadata:
        try:
            meta_from_request = RequestMetadata(**request_metadata)
        except Exception:
            meta_from_request = None

    for meta in [meta_from_request, meta_from_payload]:
        if not meta:
            continue
        creator_id = creator_id or meta.creator_id
        creator_name = creator_name or meta.creator_name
        source = source or meta.source
        trace_id = trace_id or meta.trace_id

    # Decide strategy
    desired_strategy = payload.strategy or "auto"
    strategy_used: SimilarityStrategy = desired_strategy
    if desired_strategy == "auto":
        try:
            has_coview = await similarity_service.hasCoViewData(payload.product_id)
        except Exception:
            has_coview = False
        strategy_used = "co_view" if has_coview else "content_embedding"

    overfetch = min(limit * 3, 90)
    try:
        candidates = await similarity_service.findSimilar(
            {
                "baseProductId": payload.product_id,
                "limit": overfetch,
                "strategy": strategy_used,
                "userId": payload.user.id if payload.user else None,
            }
        )
    except Exception as e:
        logger.error(f"[similar] similarity_service failed: {e}")
        candidates = []

    def _personalization_score(prod: StandardProduct) -> float:
        """Lightweight personalization using recent query tokens."""
        if not payload.user or not payload.user.recent_queries:
            return 0.0
        title_tokens = set(re.split(r"[^a-z0-9]+", (prod.title or "").lower()))
        q_tokens: set[str] = set()
        for q in payload.user.recent_queries:
            q_tokens |= set(re.split(r"[^a-z0-9]+", (q or "").lower()))
        q_tokens = {t for t in q_tokens if len(t) > 2}
        if not q_tokens:
            return 0.0
        overlap = len(title_tokens & q_tokens)
        return min(1.0, overlap / max(len(q_tokens), 1))

    filtered: List[Dict[str, Any]] = []
    raw_products = []
    seen_ids: set[str] = set()
    candidate_ids = [c.productId for c in candidates if c.productId]
    product_map = await _load_products_by_ids(candidate_ids)

    for cand in candidates:
        pid = cand.productId
        if not pid or pid in seen_ids:
            continue

        sp = product_map.get(pid)
        if not sp:
            continue

        if sp.status and sp.status != ProductStatus.ACTIVE:
            continue
        raw_products.append((pid, sp, cand))

    strict_candidates: List[Dict[str, Any]] = []
    relaxed_candidates: List[Dict[str, Any]] = []

    def _score(sp: StandardProduct, cand_obj):
        similarity_score = max(0.0, float(getattr(cand_obj, "score", 0.0) or 0.0))
        price_score = 0.0
        base_price = base_product.price or 0.0
        if base_price > 0:
            price_score = max(0.0, 1.0 - abs(sp.price - base_price) / base_price)
        merchant_score = 1.0 if strategy_used == "same_merchant_first" and sp.merchant_id == base_product.merchant_id else 0.0
        personalization_score = _personalization_score(sp)
        weights = get_similarity_scoring_weights()
        final_score = (
            weights["similarity"] * similarity_score
            + weights["price"] * price_score
            + weights["merchant"] * merchant_score
            + weights["personalization"] * personalization_score
        )
        return similarity_score, personalization_score, final_score

    # First pass: strict
    for pid, sp, cand_obj in raw_products:
        if pid in seen_ids:
            continue
        if sp.in_stock is False or (sp.inventory_quantity is not None and sp.inventory_quantity <= 0):
            continue
        if creator_id:
            cand_creator = None
            if sp.platform_metadata:
                cand_creator = sp.platform_metadata.get("creator_id") or sp.platform_metadata.get("creatorId")
            if cand_creator and cand_creator != creator_id:
                continue
        similarity_score, personalization_score, final_score = _score(sp, cand_obj)
        seen_ids.add(pid)
        strict_candidates.append(
            {
                "product": sp,
                "scores": {
                    "similarity": round(similarity_score, 3),
                    "personalization": round(personalization_score, 3) if personalization_score else None,
                },
                "debug_scores": {
                    "price": round(price_score, 3),
                    "merchant": round(merchant_score, 3),
                    "personalization": round(personalization_score, 3),
                },
                "final_score": final_score,
            }
        )

    chosen_candidates = strict_candidates

    # Relaxed pass if needed
    if not strict_candidates:
        seen_ids.clear()
        for pid, sp, cand_obj in raw_products:
            if pid in seen_ids:
                continue
            similarity_score, personalization_score, final_score = _score(sp, cand_obj)
            price_score = 0.0
            base_price = base_product.price or 0.0
            if base_price > 0:
                price_score = max(0.0, 1.0 - abs(sp.price - base_price) / base_price)
            merchant_score = 1.0 if strategy_used == "same_merchant_first" and sp.merchant_id == base_product.merchant_id else 0.0
            seen_ids.add(pid)
            relaxed_candidates.append(
                {
                    "product": sp,
                    "scores": {
                        "similarity": round(similarity_score, 3),
                        "personalization": round(personalization_score, 3) if personalization_score else None,
                    },
                    "debug_scores": {
                        "price": round(price_score, 3),
                        "merchant": round(merchant_score, 3),
                        "personalization": round(personalization_score, 3),
                    },
                    "final_score": final_score,
                }
            )
        if relaxed_candidates:
            logger.info(
                "similar.filter.relax",
                extra={
                    "base_product_id": base_product.product_id or payload.product_id,
                    "raw_count": len(raw_products),
                },
            )
            chosen_candidates = relaxed_candidates

    # Rank and trim
    chosen_candidates.sort(key=lambda x: x["final_score"], reverse=True)
    top = chosen_candidates[:limit]

    items = []
    include_debug_scores = DEV_MODE and bool(payload.debug)
    for entry in top:
        sp: StandardProduct = entry["product"]
        product_payload = _standard_to_shop_product(sp)
        items.append(
            {
                "product": product_payload,
                "best_deal": product_payload.get("best_deal"),
                "all_deals": product_payload.get("all_deals", []),
                "scores": entry.get("scores"),
                "debug_scores": {
                    "price": entry.get("debug_scores", {}).get("price"),
                    "merchant": entry.get("debug_scores", {}).get("merchant"),
                    "personalization": entry.get("debug_scores", {}).get("personalization"),
                    "final": entry.get("final_score"),
                }
                if include_debug_scores
                else None,
                "reason": "ranked_by_similarity",
            }
        )

    # Summary log
    logger.info(
        "similar.rank.summary",
        extra={
            "base_product_id": payload.product_id,
            "strategy_used": strategy_used,
            "raw_count": len(raw_products),
            "strict_count": len(strict_candidates),
            "relaxed_count": len(relaxed_candidates),
            "final_count": len(items),
            "creator_id": creator_id,
            "trace_id": trace_id,
        },
    )

    # Top candidates log (up to 5)
    debug_top = []
    for entry in chosen_candidates[:5]:
        pid = entry.get("product").product_id or entry.get("product").id
        debug_top.append(
            {
                "product_id": pid,
                "similarity_score": entry.get("scores", {}).get("similarity"),
                "price_score": entry.get("debug_scores", {}).get("price"),
                "merchant_score": entry.get("debug_scores", {}).get("merchant"),
                "personalization_score": entry.get("debug_scores", {}).get("personalization"),
                "final_score": entry.get("final_score"),
                "reason": entry.get("reason"),
            }
        )
    logger.info(
        "similar.rank.top_candidates",
        extra={
            "base_product_id": payload.product_id,
            "strategy_used": strategy_used,
            "top": debug_top,
            "trace_id": trace_id,
        },
    )

    return {
        "base_product_id": base_product.product_id or payload.product_id,
        "strategy_used": strategy_used,
        "items": items,
    }


if DEV_MODE:
    @router.get("/dev/similar")
    async def debug_similar_products(
        product_id: str,
        limit: int = 6,
        strategy: str = "auto",
    ):
        """
        Dev-only endpoint to inspect similar products.
        """
        payload = FindSimilarProductsPayload(
            product_id=product_id,
            limit=limit,
            strategy=strategy,
            debug=True,
        )
        result = await _handle_find_similar_products(payload, request_metadata={})
        return result


async def _handle_get_product_detail(
    ref: ProductRef,
    background_tasks: BackgroundTasks,
) -> Dict[str, Any]:
    """
    Implementation of the get_product_detail operation.

    Contract (simplified):
    - Input: { product: { merchant_id, product_id } }
    - Output: { product: {...same shape as find_products item, with optional attributes} }
    """
    merchant_id = ref.merchant_id
    product_id = ref.product_id

    # Fetch a reasonably large slice of the catalog to locate the product.
    # For typical merchants this is sufficient and keeps latency low.
    agent_id = "shopping_ai_frontend"
    products, query_source, error = await get_products_hybrid(
        merchant_id=merchant_id,
        limit=500,
        agent_id=agent_id,
        background_tasks=background_tasks,
    )

    if error and not products:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to fetch products for merchant {merchant_id}: {error}",
        )

    match: Optional[StandardProduct] = None
    for p in products:
        if p.product_id == product_id or p.id == product_id:
            match = p
            break

    if not match:
        # Strong contract: this should not happen if product comes from find_products,
        # so treat it as PRODUCT_NOT_FOUND.
        raise HTTPException(
            status_code=404,
            detail="PRODUCT_NOT_FOUND",
        )

    base = _standard_to_shop_product(match)

    # Optional attributes bag for LLM/Agent use; keep it simple for now.
    attributes: Dict[str, Any] = {}
    if match.platform_metadata:
        attributes.update(match.platform_metadata)

    # Include variants summary if available
    if match.variants:
        attributes["variants"] = [
            {
                "variant_id": v.variant_id or v.id,
                "title": v.title,
                "price": v.price,
                "sku": v.sku,
                "inventory_quantity": v.inventory_quantity,
                "options": v.options or {},
            }
            for v in match.variants
        ]

    return {
        "product": {
            **base,
            "attributes": attributes or None,
        },
        "metadata": {
            "query_source": query_source,
            "fetched_at": datetime.utcnow().isoformat(),
        },
    }


async def _proxy_agent_api(method: str, path: str, json_body: Dict[str, Any]) -> Dict[str, Any]:
    """Forward a request to the Agent API using a server-side API key."""
    if not AGENT_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="SHOP_GATEWAY_AGENT_API_KEY / PIVOTA_API_KEY is not configured for agent payments",
        )

    url = f"{AGENT_API_BASE}{path}"
    headers = {
        "Content-Type": "application/json",
        "X-API-Key": AGENT_API_KEY,
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.request(method, url, json=json_body, headers=headers)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Upstream agent API error: {exc}") from exc

    if resp.status_code >= 400:
        # Propagate upstream error detail when available
        try:
            err_json = resp.json()
        except Exception:
            err_json = {"detail": resp.text}
        raise HTTPException(status_code=resp.status_code, detail=err_json)

    try:
        return resp.json()
    except Exception:
        raise HTTPException(status_code=502, detail="Invalid JSON from agent API")


async def _handle_create_order(order: OrderPayloadBody) -> Dict[str, Any]:
    """Proxy create_order to Agent API (/agent/v1/orders/create)."""
    body = {
        "merchant_id": order.merchant_id,
        "customer_email": order.customer_email,
        "items": [
            {
                "merchant_id": item.merchant_id,
                "product_id": item.product_id,
                "product_title": item.product_title,
                "quantity": item.quantity,
                "unit_price": item.unit_price,
                "subtotal": item.subtotal,
            }
            for item in order.items
        ],
        "shipping_address": {
            "recipient_name": order.shipping_address.name,
            "address_line1": order.shipping_address.address_line1,
            "address_line2": order.shipping_address.address_line2 or "",
            "city": order.shipping_address.city,
            "country": order.shipping_address.country,
            "postal_code": order.shipping_address.postal_code,
            "phone": order.shipping_address.phone or "",
        },
        "customer_notes": order.customer_notes or "",
    }

    return await _proxy_agent_api("POST", "/agent/v1/orders/create", body)


async def _handle_submit_payment(payment: PaymentPayloadBody) -> Dict[str, Any]:
    """Proxy submit_payment to Agent API (/agent/v1/payments)."""
    # 将简单的 payment_method 字符串映射为 Agent Payment API 的结构化字段
    method_type = (payment.payment_method or "").strip() or "card"

    body = {
        "order_id": payment.order_id,
        "payment_method": {
            "type": method_type
        },
        # expected_amount / currency 目前仅用于前端自检，Agent Payments 会根据订单记录金额
        # 接收端的 Pydantic 模型不会使用这些字段，但保留在 body 中也无妨。
        "expected_amount": payment.expected_amount,
        "currency": payment.currency,
    }

    return await _proxy_agent_api("POST", "/agent/v1/payments", body)


@router.post("/invoke")
async def invoke_shop_operation(
    request: ShopGatewayRequest,
    background_tasks: BackgroundTasks,
) -> Dict[str, Any]:
    """
    Unified entrypoint for Shopping AI frontend & LLM agents.

    Supported operations:
    - find_products
    - get_product_detail
    - create_order       (demo-only)
    - submit_payment     (demo-only)
    """
    operation = (request.operation or "").strip()

    if operation == "find_products":
        payload = FindProductsPayload(**request.payload)
        return await _handle_find_products(payload.search, background_tasks)

    if operation == "get_product_detail":
        payload = GetProductDetailPayload(**request.payload)
        return await _handle_get_product_detail(payload.product, background_tasks)

    if operation == "create_order":
        payload = CreateOrderPayload(**request.payload)
        return await _handle_create_order(payload.order)

    if operation == "find_products_multi":
        payload = FindProductsMultiPayload(**request.payload)
        return await _handle_find_products_multi(payload, request.metadata, background_tasks)

    if operation == "find_similar_products":
        payload = FindSimilarProductsPayload(**request.payload)
        return await _handle_find_similar_products(payload, request.metadata)

    if operation == "submit_payment":
        payload = SubmitPaymentPayload(**request.payload)
        return await _handle_submit_payment(payload.payment)

    # For now we only support product operations here.
    raise HTTPException(
        status_code=400,
        detail=f"Unsupported operation: {operation}",
    )
