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
import unicodedata
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
    creator_id: Optional[str] = Field(None, alias="creatorId", description="Creator id for contextual recommendations")
    creator_name: Optional[str] = Field(None, alias="creatorName", description="Human friendly creator name")
    source: Optional[str] = Field(None, description="Calling surface (e.g. creator-agent-ui)")
    trace_id: Optional[str] = Field(None, alias="traceId", description="Optional trace id for observability")

    class Config:
        allow_population_by_field_name = True


class FindProductsMultiPayload(BaseModel):
    search: MultiSearchFilters
    user: Optional[UserIntent] = None
    metadata: Optional[RequestMetadata] = None
    creator_id: Optional[str] = Field(None, alias="creatorId", description="Optional creator context to scope results")

    class Config:
        allow_population_by_field_name = True


class SimilarUserContext(BaseModel):
    id: Optional[str] = Field(None, description="Accounts user id or email if available")
    recent_queries: List[str] = Field(default_factory=list, description="Recent free-text queries from the user")
    segments: List[str] = Field(default_factory=list, description="User segments for personalization")


class FindSimilarProductsPayload(BaseModel):
    product_id: str = Field(..., description="Base product id to find similar products for")
    merchant_id: Optional[str] = Field(None, description="Optional merchant id for the base product")
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


def _build_options_from_variants(p: StandardProduct) -> List[Dict[str, Any]]:
    """
    Derive a simple options structure from StandardProduct.variants.

    We aggregate unique values per option name (e.g. Color / Size) so that
    frontends can render structured selectors without caring about the
    underlying platform (Shopify, Wix, etc.).
    """
    buckets: Dict[str, set] = {}

    for v in p.variants or []:
        if not v.options:
            continue
        for raw_name, raw_value in v.options.items():
            if raw_name is None or raw_value is None:
                continue
            name = str(raw_name).strip()
            value = str(raw_value).strip()
            if not name or not value:
                continue
            if name not in buckets:
                buckets[name] = set()
            buckets[name].add(value)

    options: List[Dict[str, Any]] = []
    for name, values in buckets.items():
        options.append(
            {
                "name": name,
                "values": sorted(values),
            }
        )
    return options


def _standard_to_shop_product(p: StandardProduct) -> Dict[str, Any]:
    """
    Map internal StandardProduct to Shopping AI product contract.
    """
    # Prefer explicit image_url, then first image in list
    image_url = p.image_url or (p.images[0] if p.images else None)

    base: Dict[str, Any] = {
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

    # Expose full image gallery so frontends can render multi-image carousels.
    if p.images:
        base["images"] = p.images

    # Surface variant summary for size/color selection and inventory checks.
    if p.variants:
        base["variants"] = [
            {
                "variant_id": v.variant_id or v.id,
                "id": v.variant_id or v.id,
                "title": v.title,
                "price": v.price,
                "compare_at_price": v.compare_at_price,
                "sku": v.sku,
                "inventory_quantity": v.inventory_quantity,
                "options": v.options or {},
                "image_url": v.image_url,
            }
            for v in p.variants
        ]

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

    def _strip_accents(text: str) -> str:
        if not text:
            return ""
        return "".join(
            c
            for c in unicodedata.normalize("NFKD", text)
            if not unicodedata.combining(c)
        )

    def _edit_distance_leq(a: str, b: str, max_dist: int) -> bool:
        """Return True if Levenshtein(a,b) <= max_dist (with early exit)."""
        if a == b:
            return True
        if max_dist <= 0:
            return False
        if not a or not b:
            return max(len(a), len(b)) <= max_dist
        if abs(len(a) - len(b)) > max_dist:
            return False

        if len(a) > len(b):
            a, b = b, a

        prev = list(range(len(a) + 1))
        for i, ch_b in enumerate(b, start=1):
            cur = [i]
            min_in_row = cur[0]
            for j, ch_a in enumerate(a, start=1):
                cost = 0 if ch_a == ch_b else 1
                cur_val = min(
                    prev[j] + 1,
                    cur[j - 1] + 1,
                    prev[j - 1] + cost,
                )
                cur.append(cur_val)
                if cur_val < min_in_row:
                    min_in_row = cur_val
            if min_in_row > max_dist:
                return False
            prev = cur
        return prev[-1] <= max_dist

    def _fuzzy_token_match(tokens: List[str], targets: List[str], max_dist: int) -> bool:
        if not tokens or not targets:
            return False
        target_set = {t for t in targets if t}
        for tok in tokens:
            if tok in target_set:
                return True
            if len(tok) < 4:
                continue
            for t in target_set:
                if abs(len(tok) - len(t)) > max_dist:
                    continue
                if _edit_distance_leq(tok, t, max_dist):
                    return True
        return False

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

    async def _load_global_top_sellers(max_candidates: int = 50) -> List[StandardProduct]:
        """Global popular products as a fallback when creator context is missing."""
        rows = await database.fetch_all(
            """
            SELECT merchant_id, items
            FROM orders
            WHERE is_deleted IS NOT TRUE
            ORDER BY created_at DESC
            LIMIT 800
            """
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

        products: List[StandardProduct] = []

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

        if popularity:
            for (m_id, pid), _count in popularity.most_common(max_candidates * 2):
                prod = await _fetch_product(m_id, pid)
                if prod:
                    products.append(prod)
                if len(products) >= max_candidates:
                    break
            if products:
                return products

        # Final fallback: recent cached products
        rows = await database.fetch_all(
            """
            SELECT product_data
            FROM products_cache
            ORDER BY cached_at DESC
            LIMIT :limit
            """,
            {"limit": max_candidates},
        )
        for row in rows:
            product_data = row.get("product_data") if isinstance(row, dict) else None
            if isinstance(product_data, str):
                try:
                    product_data = json.loads(product_data)
                except Exception:
                    continue
            if not isinstance(product_data, dict):
                continue
            try:
                products.append(StandardProduct(**product_data))
            except Exception:
                continue

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

    # Cold start & intent detection.
    q_raw = filters.query or ""
    q = q_raw.strip()
    q_lower = q.lower()
    q_ascii = _strip_accents(q_lower)
    q_tokens = _tokenize(q_ascii)
    q_compact = re.sub(r"[^a-z0-9]+", "", q_lower)

    # Query-level tee intent (including Spanish synonyms).
    tee_intent_query = bool(
        q_compact == "tee"
        or "tshirt" in q_compact
        or re.search(r"\btees?\b", q_lower)
        or re.search(r"\bt\s*-?\s*shirts?\b", q_lower)
        or "t恤" in q_lower
        or "t 恤" in q_lower
        or "camiseta" in q_lower
        or "camisetas" in q_lower
        or "playera" in q_lower
        or "playeras" in q_lower
    )

    # Query-level lingerie intent (user explicitly looking for lingerie).
    lingerie_intent_query = bool(
        "lingerie" in q_lower
        or "lenceria" in q_lower
        or "lencer\u00eda" in q_lower
        or "ropa interior" in q_lower
    )

    # Query-level toys intent (kids + designer toys), including common misspellings (e.g. "tolls" ≈ "dolls").
    toys_intent_query = bool(
        "toy" in q_ascii
        or "toys" in q_ascii
        or "juguete" in q_ascii
        or "juguetes" in q_ascii
        or "art toy" in q_ascii
        or "art toys" in q_ascii
        or "designer toy" in q_ascii
        or "designer toys" in q_ascii
        or "labubu" in q_ascii
        or _fuzzy_token_match(q_tokens, ["doll", "dolls", "toys"], max_dist=1)
    )

    # Detect special intents for downstream filtering/UX.
    look_intent = False
    if "nina studio" in q_lower and any(
        token in q_lower for token in ["exact outfit", "shop", "look", "wear", "ropa", "outfit"]
    ):
        look_intent = True

    # Detect negative lingerie constraint (e.g. "no lingerie", "sin lenceria").
    exclude_lingerie = False
    if (
        "no lingerie" in q_lower
        or "without lingerie" in q_lower
        or "sin lenceria" in q_lower
        or "sin lencer\u00eda" in q_lower
        or "sin ropa interior" in q_lower
    ):
        exclude_lingerie = True

    # Detect negative hoodie constraint (e.g. "no hoodies").
    exclude_hoodies = False
    if (
        "no hoodie" in q_lower
        or "no hoodies" in q_lower
        or "without hoodie" in q_lower
        or "without hoodies" in q_lower
        or "sin sudadera" in q_lower
        or "sin sudaderas" in q_lower
        or "no sudadera" in q_lower
        or "no sudaderas" in q_lower
    ):
        exclude_hoodies = True

    # Detect negative joggers constraint.
    exclude_joggers = False
    if (
        "no joggers" in q_lower
        or "no jogger" in q_lower
        or "sin jogger" in q_lower
        or "sin joggers" in q_lower
        or "sin pantalones jogger" in q_lower
        or "no pantalones jogger" in q_lower
    ):
        exclude_joggers = True

    # Detect negative underwear constraint, while still allowing lingerie when requested.
    exclude_underwear = False
    if (
        "no underwear" in q_lower
        or "without underwear" in q_lower
        or "no panties" in q_lower
        or "no panty" in q_lower
        or "no briefs" in q_lower
        or "no thong" in q_lower
        or "sin ropa interior" in q_lower
        or "sin bragas" in q_lower
        or "sin calzoncillos" in q_lower
    ):
        exclude_underwear = True

    # Detect positive-only skirts intent (e.g. "only skirts", "solo faldas").
    only_skirts = False
    if any(
        trigger in q_lower
        for trigger in [
            "only skirt",
            "only skirts",
            "skirts only",
            "solo faldas",
            "solo falda",
        ]
    ):
        only_skirts = True

    # Construct reply for look-intent queries: similar items + disclaimer/prompt.
    reply_text: Optional[str] = None
    if look_intent:
        reply_text = (
            "I can’t guarantee an exact match for that outfit, "
            "but here are similar items inspired by Nina Studio’s looks. "
            "If you share a link or photo of the outfit, I can refine these suggestions."
        )

    # Cold start: empty query falls back to creator/global top sellers and cache/live fallbacks.
    if not q:
        source = "creator_top_sellers"
        top_sellers = await _load_creator_top_sellers(max_candidates=limit * 2)
        if not top_sellers:
            top_sellers = await _load_global_top_sellers(max_candidates=limit * 2)
            source = "global_top_sellers"
        mapped = []
        for prod in top_sellers[: limit * page]:
            item = _standard_to_shop_product(prod)
            item["merchant_name"] = merchant_map.get(prod.merchant_id)
            mapped.append(item)

        # If still empty (no creator/global top sellers), fall back to recent cached products
        if not mapped:
            rows = await database.fetch_all(
                """
                SELECT merchant_id, product_data
                FROM products_cache
                ORDER BY cached_at DESC
                LIMIT :limit
                """,
                {"limit": max(limit * max(page, 1) * 2, 20)},
            )
            source = "cache_global_fallback"
            for row in rows:
                merchant_id = row.get("merchant_id") if isinstance(row, dict) else None
                product_data = row.get("product_data") if isinstance(row, dict) else None
                if isinstance(product_data, str):
                    try:
                        product_data = json.loads(product_data)
                    except Exception:
                        continue
                if not isinstance(product_data, dict):
                    continue
                try:
                    prod = StandardProduct(**product_data)
                    prod.merchant_id = prod.merchant_id or merchant_id
                    item = _standard_to_shop_product(prod)
                    item["merchant_name"] = merchant_map.get(prod.merchant_id)
                    mapped.append(item)
                except Exception:
                    # Fallback: tolerate partially invalid cache rows
                    pid = (
                        product_data.get("id")
                        or product_data.get("product_id")
                        or product_data.get("platform_product_id")
                        or None
                    )
                    title = product_data.get("title") or product_data.get("name") or ""
                    price = product_data.get("price") or product_data.get("compare_at_price") or 0
                    currency = product_data.get("currency") or "USD"
                    image_url = (
                        product_data.get("image_url")
                        or (product_data.get("images") or [{}])[0].get("src")
                        if isinstance(product_data.get("images"), list)
                        else None
                    )
                    if pid and title and price is not None:
                        mapped.append(
                            {
                                "id": pid,
                                "platform": product_data.get("platform") or product_data.get("source") or "",
                                "merchant_id": merchant_id,
                                "product_id": pid,
                                "title": title,
                                "description": product_data.get("description") or "",
                                "vendor": product_data.get("vendor"),
                                "product_type": product_data.get("product_type"),
                                "tags": product_data.get("tags") or [],
                                "price": price,
                                "compare_at_price": product_data.get("compare_at_price"),
                                "currency": currency,
                                "inventory_quantity": product_data.get("inventory_quantity") or 0,
                                "in_stock": bool(product_data.get("inventory_quantity", 0) > 0)
                                if product_data.get("inventory_quantity") is not None
                                else True,
                                "sku": product_data.get("sku") or "",
                                "barcode": product_data.get("barcode"),
                                "image_url": image_url,
                                "images": product_data.get("images") or [],
                                "variants": product_data.get("variants") or [],
                                "status": product_data.get("status") or "active",
                                "published_at": product_data.get("published_at"),
                                "created_at": product_data.get("created_at"),
                                "updated_at": product_data.get("updated_at"),
                                "data_completeness_score": product_data.get("data_completeness_score"),
                                "platform_metadata": product_data.get("platform_metadata"),
                                "orderable": product_data.get("orderable", True),
                                "merchant_name": merchant_map.get(merchant_id),
                            }
                        )

        # Last resort: fetch live from merchants if cache is still empty
        if not mapped and merchant_map:
            source = "live_merchant_fallback"
            per_merchant = min(max(limit * 2, 10), 50)
            for mid, name in merchant_map.items():
                try:
                    products, _src, _err = await get_products_hybrid(
                        merchant_id=mid,
                        limit=per_merchant,
                        agent_id="shopping_ai_multi_live",
                        background_tasks=background_tasks,
                    )
                    for p in products:
                        item = _standard_to_shop_product(p)
                        item["merchant_name"] = name
                        mapped.append(item)
                except Exception:
                    continue

        start_idx = (page - 1) * limit
        page_items = mapped[start_idx : start_idx + limit]
        return {
            "products": page_items,
            "total": len(mapped),
            "page": page,
            "page_size": len(page_items),
            "reply": reply_text,
            "metadata": {
                "query_source": source,
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

        # Explicit exclusion / inclusion based on user constraints.
        blob_for_filters = " ".join(
            [
                (product.title or "").lower(),
                (product.description or "").lower(),
                (product.product_type or "").lower(),
                " ".join(getattr(product, "tags", None) or []),
            ]
        )
        blob_for_filters_ascii = _strip_accents(blob_for_filters)

        if exclude_lingerie or exclude_hoodies or exclude_joggers or exclude_underwear:
            if exclude_lingerie:
                lingerie_tokens = [
                    "lingerie",
                    "lenceria",
                    "lencer\u00eda",
                    "underwear",
                    "bra",
                    "panties",
                    "ropa interior",
                    "sujetador",
                    "bragas",
                ]
                if any(tok in blob_for_filters for tok in lingerie_tokens):
                    continue
            if exclude_hoodies:
                hoodie_tokens = [
                    "hoodie",
                    "hoodies",
                    "sweatshirt",
                    "sudadera",
                ]
                if any(tok in blob_for_filters for tok in hoodie_tokens):
                    continue
            if exclude_joggers:
                jogger_tokens = [
                    "joggers",
                    "jogger",
                    "pantalones jogger",
                ]
                if any(tok in blob_for_filters for tok in jogger_tokens):
                    continue
            if exclude_underwear:
                underwear_tokens = [
                    "underwear",
                    "panties",
                    "panty",
                    "briefs",
                    "thong",
                    "ropa interior",
                    "calzoncillos",
                    "bragas",
                ]
                if any(tok in blob_for_filters for tok in underwear_tokens):
                    continue

        if only_skirts:
            skirt_tokens = [
                "skirt",
                "skirts",
                "falda",
                "faldas",
            ]
            if not any(tok in blob_for_filters for tok in skirt_tokens):
                continue

        # Text relevance
        relevance_score = 1.0
        if q_lower:
            title = (product.title or "").lower()
            description = (product.description or "").lower()
            product_type = (product.product_type or "").lower()
            blob = " ".join([title, description, product_type]).strip()
            blob_compact = re.sub(r"[^a-z0-9]+", "", blob)
            q_compact = re.sub(r"[^a-z0-9]+", "", q_lower)

            # Detect tee intent, including Spanish tee synonyms (camiseta/playera).
            tee_intent = bool(
                q_compact == "tee"
                or "tshirt" in q_compact
                or re.search(r"\btees?\b", q_lower)
                or re.search(r"\bt\s*-?\s*shirts?\b", q_lower)
                or "t恤" in q_lower
                or "t 恤" in q_lower
                or "camiseta" in q_lower
                or "camisetas" in q_lower
                or "playera" in q_lower
                or "playeras" in q_lower
            )

            if tee_intent:
                has_tee_marker = bool(
                    "tshirt" in blob_compact
                    or re.search(r"\btees?\b", blob)
                    or re.search(r"\bt\s*-?\s*shirts?\b", blob)
                    or "t恤" in blob
                    or "t 恤" in blob
                    or "camiseta" in blob
                    or "camisetas" in blob
                    or "playera" in blob
                    or "playeras" in blob
                )
                if not has_tee_marker:
                    continue

            if q_lower in title:
                relevance_score = 1.0 if q_lower == title else 0.9
            elif q_lower in description:
                relevance_score = 0.7
            elif q_compact and len(q_compact) >= 4 and q_compact in blob_compact:
                # Handle queries like "t-shirt" vs "tshirt" or "te e" vs "tee"
                relevance_score = 0.8
            else:
                # Token-based matching with short-token guard (prevents "te e" -> ["te","e"] over-matching).
                query_terms = _tokenize(q_ascii)

                if not query_terms and q_compact and len(q_compact) > 2:
                    query_terms = [q_compact]

                if tee_intent:
                    for t in ("tee", "tshirt", "t-shirt", "camiseta", "camisetas", "playera", "playeras"):
                        if t not in query_terms:
                            query_terms.append(t)

                if toys_intent_query:
                    for t in (
                        "toy",
                        "toys",
                        "juguete",
                        "juguetes",
                        "doll",
                        "dolls",
                        "plush",
                        "plushie",
                        "peluche",
                        "figure",
                        "figures",
                        "vinyl",
                        "blind",
                        "box",
                        "collectible",
                        "collector",
                        "art",
                        "designer",
                        "labubu",
                    ):
                        if t not in query_terms:
                            query_terms.append(t)

                if not query_terms:
                    continue

                matches = sum(
                    1
                    for term in query_terms
                    if term and (term in blob or term in blob_compact)
                )
                if matches == 0:
                    continue
                relevance_score = 0.5 + (matches / len(query_terms)) * 0.3

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

        # Lingerie / toys intent boosts: if the user explicitly asks for these,
        # gently boost matching products so they are more likely to surface.
        if lingerie_intent_query:
            lingerie_tokens = [
                "lingerie",
                "lenceria",
                "lencer\u00eda",
                "underwear",
                "ropa interior",
            ]
            if any(tok in blob_for_filters_ascii for tok in lingerie_tokens):
                relevance_score += 0.4

        if toys_intent_query:
            toys_tokens = [
                "toy",
                "toys",
                "juguete",
                "juguetes",
                "doll",
                "dolls",
                "plush",
                "plushie",
                "peluche",
                "figure",
                "figures",
                "vinyl",
                "blind box",
                "collectible",
                "designer toy",
                "art toy",
                "labubu",
            ]
            is_toy_like = any(tok in blob_for_filters_ascii for tok in toys_tokens)
            if is_toy_like:
                relevance_score += 0.45

        filtered_products.append(
            {
                "product": product,
                "merchant_name": merchant_name,
                "relevance_score": relevance_score,
                "is_toy_like": is_toy_like if toys_intent_query else False,
            }
        )

    if toys_intent_query:
        toy_candidates = [p for p in filtered_products if p.get("is_toy_like")]
        filtered_products = toy_candidates if toy_candidates else []

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

    # Fallback: if primary query returned nothing, surface top-sellers instead
    # - For general queries: only when creator_id is present (as before)
    # - For tee intent queries: also allow a global tee-only fallback so we don't
    #   respond with an empty list for strong tee intent (e.g. Spanish camisetas).
    if not out_products:
        # Creator-scoped fallback (original behavior), now also respecting tee intent.
        if creator_id and not toys_intent_query:
            top_sellers = await _load_creator_top_sellers(max_candidates=limit * 2)
            source = "creator_top_sellers_fallback"

            # For special look-intent queries (e.g. "exact outfit on a date"),
            # fall back to global top sellers when creator history is empty,
            # so we can still propose similar/inspired items.
            if not top_sellers and look_intent:
                top_sellers = await _load_global_top_sellers(max_candidates=limit * 2)
                source = "global_top_sellers_fallback"

            mapped: list[dict[str, Any]] = []
            for prod in top_sellers[: limit * page]:
                # If this is a tee intent query, enforce tee-like products here as well.
                if tee_intent_query:
                    blob = " ".join(
                        [
                            (prod.title or "").lower(),
                            (prod.description or "").lower(),
                            (prod.product_type or "").lower(),
                        ]
                    ).strip()
                    blob_compact = re.sub(r"[^a-z0-9]+", "", blob)
                    has_tee_marker = bool(
                        "tshirt" in blob_compact
                        or re.search(r"\btees?\b", blob)
                        or re.search(r"\bt\s*-?\s*shirts?\b", blob)
                        or "t恤" in blob
                        or "t 恤" in blob
                        or "camiseta" in blob
                        or "camisetas" in blob
                        or "playera" in blob
                        or "playeras" in blob
                    )
                    if not has_tee_marker:
                        continue

                item = _standard_to_shop_product(prod)
                item["merchant_name"] = merchant_map.get(prod.merchant_id)
                mapped.append(item)

            fallback_items = mapped[start_idx:end_idx]
            if mapped:
                return {
                    "products": fallback_items,
                    "total": len(mapped),
                    "page": page,
                    "page_size": len(fallback_items),
                    "reply": reply_text,
                    "metadata": {
                        "query_source": source,
                        "fetched_at": datetime.utcnow().isoformat(),
                        "merchants_searched": len(merchant_map),
                        "creator_id": creator_id,
                        "creator_name": creator_name,
                    },
                }

        # Tee-intent global fallback when there is no creator context.
        if tee_intent_query:
            top_sellers = await _load_global_top_sellers(max_candidates=limit * 2)
            source = "global_top_sellers_tee_fallback"
            mapped: list[dict[str, Any]] = []
            for prod in top_sellers[: limit * page]:
                blob = " ".join(
                    [
                        (prod.title or "").lower(),
                        (prod.description or "").lower(),
                        (prod.product_type or "").lower(),
                    ]
                ).strip()
                blob_compact = re.sub(r"[^a-z0-9]+", "", blob)
                has_tee_marker = bool(
                    "tshirt" in blob_compact
                    or re.search(r"\btees?\b", blob)
                    or re.search(r"\bt\s*-?\s*shirts?\b", blob)
                    or "t恤" in blob
                    or "t 恤" in blob
                    or "camiseta" in blob
                    or "camisetas" in blob
                    or "playera" in blob
                    or "playeras" in blob
                )
                if not has_tee_marker:
                    continue

                item = _standard_to_shop_product(prod)
                item["merchant_name"] = merchant_map.get(prod.merchant_id)
                mapped.append(item)

            fallback_items = mapped[start_idx:end_idx]
            if mapped:
                return {
                    "products": fallback_items,
                    "total": len(mapped),
                    "page": page,
                    "page_size": len(fallback_items),
                    "reply": reply_text,
                    "metadata": {
                        "query_source": source,
                        "fetched_at": datetime.utcnow().isoformat(),
                        "merchants_searched": len(merchant_map),
                        "creator_id": creator_id,
                        "creator_name": creator_name,
                    },
                }

    if not out_products and toys_intent_query:
        reply_text = reply_text or (
            "I couldn’t find toy items in the current shop catalog for that query. "
            "If you share a brand or character name (for example: Labubu), I can narrow it down."
        )

    history_used = bool(history_product_ids or history_terms)

    return {
        "products": out_products,
        "total": total,
        "page": page,
        "page_size": len(out_products),
        "reply": reply_text,
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
    background_tasks: BackgroundTasks,
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

    # Try loading base product from cache first
    base_product = await _load_product_by_id(payload.product_id)

    # If not found in cache but we know the merchant, fall back to the
    # hybrid path so products that only exist in the realtime slice can
    # still be used as a similarity anchor.
    if not base_product and payload.merchant_id:
        try:
            products, _, _ = await get_products_hybrid(
                merchant_id=payload.merchant_id,
                limit=500,
                agent_id="shopping_ai_similar",
                background_tasks=background_tasks,
            )
            for p in products:
                if p.product_id == payload.product_id or p.id == payload.product_id:
                    base_product = p
                    break
        except Exception as e:
            logger.error(
                "similar.base.hybrid_failed",
                extra={
                    "product_id": payload.product_id,
                    "merchant_id": payload.merchant_id,
                    "error": str(e),
                },
            )

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
    if desired_strategy == "auto" and base_product:
        try:
            has_coview = await similarity_service.hasCoViewData(payload.product_id)
        except Exception:
            has_coview = False
        strategy_used = "co_view" if has_coview else "content_embedding"
    elif desired_strategy == "auto":
        # Without a concrete base product we still default to content-based
        # similarity semantics, but will rely entirely on fallback below.
        strategy_used = "content_embedding"

    overfetch = min(limit * 3, 90)
    candidates: List[SimilarCandidate] = []
    if base_product:
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
    raw_products: List[Any] = []
    seen_ids: set[str] = set()
    strict_candidates: List[Dict[str, Any]] = []
    relaxed_candidates: List[Dict[str, Any]] = []

    if base_product and candidates:
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
            return similarity_score, price_score, merchant_score, personalization_score, final_score

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
            similarity_score, price_score, merchant_score, personalization_score, final_score = _score(sp, cand_obj)
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

        # Relaxed pass if needed
        if not strict_candidates:
            seen_ids.clear()
            for pid, sp, cand_obj in raw_products:
                if pid in seen_ids:
                    continue
                similarity_score, price_score, merchant_score, personalization_score, final_score = _score(sp, cand_obj)
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

    chosen_candidates = strict_candidates or relaxed_candidates

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

    # Fallback: if similarity pipeline produced no items, reuse multi search
    if not items:
        try:
            # First attempt: use the base product title as query (when available).
            primary_query = ""
            primary_category = None
            if base_product:
                primary_query = (base_product.title or "") or ""
                primary_category = base_product.product_type

            primary_search = MultiSearchFilters(
                query=primary_query,
                category=primary_category,
                price_min=None,
                price_max=None,
                page=1,
                limit=limit,
                in_stock_only=False,
            )
            primary_payload = FindProductsMultiPayload(
                search=primary_search,
                user=None,
                metadata=None,
                creator_id=creator_id,
            )
            primary_result = await _handle_find_products_multi(
                primary_payload,
                request_metadata or {},
                background_tasks,
            )
            fallback_products = primary_result.get("products", []) or []

            # If a title-based query still yields nothing, fall back to a broad
            # creator/global top-sellers style search by using an empty query.
            if not fallback_products:
                broad_search = MultiSearchFilters(
                    query="",
                    category=None,
                    price_min=None,
                    price_max=None,
                    page=1,
                    limit=limit,
                    in_stock_only=False,
                )
                broad_payload = FindProductsMultiPayload(
                    search=broad_search,
                    user=None,
                    metadata=None,
                    creator_id=creator_id,
                )
                broad_result = await _handle_find_products_multi(
                    broad_payload,
                    request_metadata or {},
                    background_tasks,
                )
                fallback_products = broad_result.get("products", []) or []

            items = [
                {
                    "product": prod,
                    "best_deal": prod.get("best_deal"),
                    "all_deals": prod.get("all_deals", []),
                    "scores": None,
                    "reason": "fallback_from_multi_search",
                }
                for prod in fallback_products[:limit]
            ]
            logger.info(
                "similar.fallback.multi_search",
                extra={
                    "base_product_id": payload.product_id,
                    "strategy_used": strategy_used,
                    "fallback_count": len(items),
                    "creator_id": creator_id,
                },
            )
        except Exception as e:
            logger.error(
                "similar.fallback.failed",
                extra={"base_product_id": payload.product_id, "error": str(e)},
            )

    return {
        "base_product_id": (base_product.product_id if base_product else None) or payload.product_id,
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
        result = await _handle_find_similar_products(
            payload,
            request_metadata={},
            background_tasks=BackgroundTasks(),
        )
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

    # Fast path: try loading directly from products cache by product_id.
    # 对于已经通过产品列表曝光过的商品，这条路径通常是命中缓存的，
    # 能避免为单个商品再去拉整页 catalog，显著降低详情页延迟。
    match: Optional[StandardProduct] = await _load_product_by_id(product_id)
    query_source: str = "product_cache_direct" if match else "unknown"

    products: List[StandardProduct] = []
    error: Optional[str] = None

    if not match:
        # Fallback: fetch a reasonably large slice of the catalog and locate the product.
        # For typical merchants this is sufficient and keeps latency acceptable.
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

        for p in products:
            if p.product_id == product_id or p.id == product_id:
                match = p
                break

        if not match:
            # Second chance: if hybrid layer couldn't find it, try cache again in case
            # the product was recently synced but not yet surfaced in the hybrid slice.
            match = await _load_product_by_id(product_id)
            if match:
                query_source = "product_cache_fallback"

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

    # Structured options for frontend (Color / Size / etc.).
    # Creator Agent UI expects:
    # - product.options: [{ name, values[] }]
    # - and optionally product.product_options for legacy naming.
    options = _build_options_from_variants(match)
    legacy_product_options = (
        [{"label": opt["name"], "values": opt["values"]} for opt in options]
        if options
        else None
    )

    return {
        "product": {
            **base,
            "attributes": attributes or None,
            "options": options or None,
            "product_options": legacy_product_options,
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
    - find_similar_products
    """
    operation = (request.operation or "").strip()

    # Normalize metadata: allow creatorId/creatorName to be passed at payload top-level
    normalized_metadata: Dict[str, Any] = dict(request.metadata or {})
    if not normalized_metadata.get("creator_id"):
        for k in ("creatorId", "creator_id"):
            if k in request.payload:
                normalized_metadata["creator_id"] = request.payload.get(k)
                break
    if not normalized_metadata.get("creator_name"):
        for k in ("creatorName", "creator_name"):
            if k in request.payload:
                normalized_metadata["creator_name"] = request.payload.get(k)
                break

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
        return await _handle_find_products_multi(payload, normalized_metadata, background_tasks)

    if operation == "find_similar_products":
        payload = FindSimilarProductsPayload(**request.payload)
        return await _handle_find_similar_products(payload, normalized_metadata, background_tasks)

    if operation == "submit_payment":
        payload = SubmitPaymentPayload(**request.payload)
        return await _handle_submit_payment(payload.payment)

    # For now we only support product operations here.
    raise HTTPException(
        status_code=400,
        detail=f"Unsupported operation: {operation}",
    )
