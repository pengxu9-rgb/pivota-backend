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

import asyncio
import hashlib
import json
import logging
import os
import re
import time
import unicodedata
import mimetypes
from collections import Counter
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from db.database import database
from models.reviews_refs import SkuRef as ReviewsSkuRef
from services.product_query_service import get_products_hybrid
from services.similarity_service import (
    SimilarityService,
    SimilarityStrategy,
    SimilarCandidate,
    similarity_service,
)
from services.similarity_config import get_similarity_scoring_weights
from services.outbound_links_service import (
    DEFAULT_UTM_TEMPLATE,
    _is_domain_allowed,
    apply_utm,
    make_redirect_token,
)
from models.standard_product import StandardProduct, ProductStatus
from services.agent_task_manager import AgentTaskManager

AGENT_API_BASE = os.getenv("AGENT_API_BASE", "https://web-production-fedb.up.railway.app").rstrip("/")
AGENT_API_KEY = os.getenv("SHOP_GATEWAY_AGENT_API_KEY") or os.getenv("PIVOTA_API_KEY") or os.getenv("AGENT_API_KEY")

logger = logging.getLogger(__name__)

_MERCHANT_SHOPIFY_CURRENCY_CACHE: Dict[str, tuple[float, str]] = {}
_MERCHANT_SHOPIFY_CURRENCY_TTL_SECONDS = 6 * 60 * 60

_REVIEW_MEDIA_IP_LIMIT_STORE: Dict[str, tuple[int, int]] = {}


def _reviews_enabled() -> bool:
    return os.getenv("REVIEWS_ENABLED", "true").lower() == "true"


def _reviews_featured_enabled() -> bool:
    return os.getenv("REVIEWS_FEATURED_ENABLED", "true").lower() == "true"


def _reviews_default_view_override() -> Optional[str]:
    v = (os.getenv("REVIEWS_DEFAULT_VIEW") or "").strip().lower()
    if v in {"merchant", "group"}:
        return v
    return None


def _reviews_media_rpm() -> int:
    raw = (os.getenv("REVIEWS_MEDIA_RPM") or "").strip()
    try:
        v = int(raw) if raw else 120
    except Exception:
        v = 120
    return max(1, min(v, 10_000))


def _reviews_media_import_dir() -> str:
    base = os.getenv("REVIEWS_IMPORT_DIR", os.path.join(os.getcwd(), "tmp", "reviews-imports"))
    return os.path.realpath(base)


def _review_media_client_ip(request: Request) -> str:
    xff = (request.headers.get("x-forwarded-for") or "").strip()
    if xff:
        return xff.split(",")[0].strip() or "unknown"
    return (request.client.host if request.client else None) or "unknown"


def _check_review_media_rate_limit(ip: str) -> bool:
    rpm = _reviews_media_rpm()
    window = int(time.time() // 60)
    prev = _REVIEW_MEDIA_IP_LIMIT_STORE.get(ip)
    if prev and prev[0] == window:
        if prev[1] >= rpm:
            return False
        _REVIEW_MEDIA_IP_LIMIT_STORE[ip] = (window, prev[1] + 1)
        return True
    _REVIEW_MEDIA_IP_LIMIT_STORE[ip] = (window, 1)
    return True


def _set_media_cache_headers(resp: Response, etag: Optional[str]) -> None:
    resp.headers["Cache-Control"] = "private, max-age=300"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    if etag:
        resp.headers["ETag"] = f"\"{etag}\""


def _get_cached_merchant_shopify_currency(merchant_id: str) -> Optional[str]:
    mid = (merchant_id or "").strip()
    if not mid:
        return None
    hit = _MERCHANT_SHOPIFY_CURRENCY_CACHE.get(mid)
    if not hit:
        return None
    expires_at, currency = hit
    if expires_at < time.time():
        _MERCHANT_SHOPIFY_CURRENCY_CACHE.pop(mid, None)
        return None
    return currency


def _set_cached_merchant_shopify_currency(merchant_id: str, currency: str) -> None:
    mid = (merchant_id or "").strip()
    cur = (currency or "").strip().upper()
    if not mid or not cur:
        return
    _MERCHANT_SHOPIFY_CURRENCY_CACHE[mid] = (
        time.time() + _MERCHANT_SHOPIFY_CURRENCY_TTL_SECONDS,
        cur,
    )


async def _resolve_shopify_currency_for_merchant(merchant_id: str) -> Optional[str]:
    cached = _get_cached_merchant_shopify_currency(merchant_id)
    if cached:
        return cached

    try:
        from services.merchant_store_service import get_merchant_active_stores
        from adapters.product_adapters import ShopifyProductAdapter

        stores = await get_merchant_active_stores(merchant_id)
        shopify_store = next(
            (
                s
                for s in stores
                if (s.get("platform") or "").lower() == "shopify"
                and (s.get("domain") or "").strip()
                and (s.get("api_key") or "").strip()
            ),
            None,
        )
        if not shopify_store:
            return None

        shop_domain = str(shopify_store["domain"]).strip()
        access_token = str(shopify_store["api_key"]).strip()
        cur = await ShopifyProductAdapter.fetch_shop_currency(
            shop_domain=shop_domain,
            access_token=access_token,
        )
        if cur:
            _set_cached_merchant_shopify_currency(merchant_id, cur)
        return cur
    except Exception:
        return None

router = APIRouter(prefix="/agent/shop/v1", tags=["Shopping Gateway"])
DEV_MODE = os.getenv("APP_ENV", "dev") != "production"

# Bounded queue + worker pool for heavy agent work.
agent_task_manager = AgentTaskManager.from_env()


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
    intent_safety: Optional[Dict[str, Any]] = Field(
        None,
        description="Optional structured intent safety hints from upstream (e.g. high_level_intent, forbid/filter adult).",
    )

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


class ListSkuReviewsFilters(BaseModel):
    featured_only: bool = False
    has_media: bool = False
    rating: Optional[int] = None  # 1..5
    limit: int = 20
    cursor: Optional[str] = None


class ListSkuReviewsPayload(BaseModel):
    sku: ReviewsSkuRef
    filters: Optional[ListSkuReviewsFilters] = None


class ListGroupReviewsFilters(BaseModel):
    merchant_ids: Optional[List[str]] = None
    featured_only: bool = False
    has_media: bool = False
    limit: int = 20
    cursor: Optional[str] = None


class ListGroupReviewsPayload(BaseModel):
    group_id: int
    filters: Optional[ListGroupReviewsFilters] = None


class ListGroupMerchantsPayload(BaseModel):
    group_id: int


class ListSellerFeedbackPayload(BaseModel):
    merchant_id: str
    limit: int = 20
    cursor: Optional[str] = None


class ReviewSubjectRef(BaseModel):
    merchant_id: str
    platform: str
    platform_product_id: str
    variant_id: Optional[str] = None


class ListReviewEntrypointsPayload(BaseModel):
    agent_id: Optional[str] = None
    surface: Optional[str] = None
    locale: Optional[str] = None
    capabilities: Optional[Dict[str, Any]] = None
    subject: Optional[ReviewSubjectRef] = None


class ResolveReviewIntentPayload(BaseModel):
    agent_id: Optional[str] = None
    surface: Optional[str] = None
    locale: Optional[str] = None
    entrypoint_id: str
    intent: str = Field(..., description="read | write")
    subject: ReviewSubjectRef


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


class CreatorTaskCreateRequest(ShopGatewayRequest):
    """
    Request body for async creator tasks.

    - operation / payload / metadata mirror ShopGatewayRequest
    - request_id: optional idempotency key supplied by caller
    - session_id: optional explicit session identifier; when omitted,
      the gateway derives a session id from metadata and payload.
    """

    request_id: Optional[str] = None
    session_id: Optional[str] = None


class CreatorTaskStatus(BaseModel):
    task_id: str
    status: str
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


def _derive_session_id_for_multi(
    payload: "FindProductsMultiPayload",
    metadata: Dict[str, Any],
) -> Optional[str]:
    """
    Build a stable per-conversation/session identifier for cross-merchant search.

    Priority:
    - Explicit trace_id when provided by upstream callers.
    - Creator + user tuple (creator_id + user.id) for Creator Agent UI.
    """
    trace_id = metadata.get("trace_id") or metadata.get("traceId")
    if trace_id:
        return f"multi:{trace_id}"

    creator_id: Optional[str] = None
    if payload.creator_id:
        creator_id = payload.creator_id
    elif payload.metadata and payload.metadata.creator_id:
        creator_id = payload.metadata.creator_id
    elif metadata.get("creator_id"):
        creator_id = str(metadata["creator_id"])

    user_id = payload.user.id if payload.user and payload.user.id else None

    parts: List[str] = ["multi"]
    if creator_id:
        parts.append(f"creator={creator_id}")
    if user_id:
        parts.append(f"user={user_id}")

    if len(parts) == 1:
        return None
    return "|".join(parts)


def _derive_session_id_for_similar(
    payload: "FindSimilarProductsPayload",
    metadata: Dict[str, Any],
) -> Optional[str]:
    """
    Build a stable session identifier for similar-products operations.
    """
    trace_id = metadata.get("trace_id") or metadata.get("traceId")
    if trace_id:
        return f"similar:{trace_id}"

    creator_id = (
        payload.creator_id
        or (payload.metadata.creator_id if payload.metadata else None)
        or metadata.get("creator_id")
    )

    parts: List[str] = ["similar", f"product={payload.product_id}"]
    if creator_id:
        parts.append(f"creator={creator_id}")
    return "|".join(parts)


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
        "product_id": p.product_id or p.id,
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
        # Surface creator pick metadata when available so that UIs can
        # implement a "Creator picks" filter without separate calls.
        creator_pick = p.platform_metadata.get("creator_pick")
        creator_pick_rank = p.platform_metadata.get("creator_pick_rank")
        if creator_pick is not None:
            base["creator_pick"] = bool(creator_pick)
        if creator_pick_rank is not None:
            try:
                base["creator_pick_rank"] = int(creator_pick_rank)
            except Exception:
                # If rank is not an int, skip rather than raising.
                pass

    if best_deal is not None:
        base["best_deal"] = best_deal
    if all_deals:
        base["all_deals"] = all_deals

    return base


def _stable_external_product_id(url: str) -> str:
    u = str(url or "").strip()
    if not u:
        return ""
    return "ext_" + hashlib.sha256(u.encode("utf-8")).hexdigest()[:24]


def _ensure_seed_data_obj(val: Any) -> Dict[str, Any]:
    if isinstance(val, dict):
        return val
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return {}
        try:
            parsed = json.loads(s)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _normalize_seed_variants(seed_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = seed_data.get("variants")
    if not isinstance(raw, list):
        return []

    variants: List[Dict[str, Any]] = []
    for idx, v in enumerate(raw):
        if not isinstance(v, dict):
            continue
        variant_id = v.get("variant_id") or v.get("id") or v.get("sku") or f"ext_variant_{idx + 1}"
        price_amount = v.get("price_amount") or v.get("price") or v.get("amount") or v.get("value")
        price_currency = v.get("price_currency") or v.get("currency")
        variants.append(
            {
                "variant_id": str(variant_id),
                "title": v.get("title") or v.get("name"),
                "price": {
                    "price_amount": price_amount,
                    "currency": price_currency,
                },
                "availability": v.get("availability"),
                "image_url": v.get("image_url") or v.get("image"),
                "options": v.get("options") or {},
            }
        )
        if len(variants) >= 30:
            break
    return variants


async def _make_external_redirect_url(
    *,
    market: str,
    tool: str,
    destination_url: str,
    utm_template: Optional[str],
    ctx: Dict[str, Any],
) -> Optional[str]:
    dest_with_utm = apply_utm(
        destination_url,
        utm_template or DEFAULT_UTM_TEMPLATE,
        {"market": market, "tool": tool},
    )
    if not await _is_domain_allowed(market=market, destination_url=dest_with_utm):
        return None
    token = make_redirect_token(
        {
            "market": market,
            "tool": tool,
            "dest": dest_with_utm,
            "ctx": ctx,
        }
    )
    base = (os.getenv("PUBLIC_BASE_URL") or os.getenv("BASE_URL") or AGENT_API_BASE).rstrip("/")
    return f"{base}/r?token={token}"


def _external_seed_to_shop_product(
    *,
    row: Dict[str, Any],
    seed_data: Dict[str, Any],
    redirect_url: Optional[str],
) -> Dict[str, Any]:
    title = row.get("title") or seed_data.get("title") or row.get("canonical_url") or row.get("destination_url")
    image_url = row.get("image_url") or seed_data.get("image_url")
    price_amount = row.get("price_amount") if row.get("price_amount") is not None else seed_data.get("price_amount")
    price_currency = row.get("price_currency") or seed_data.get("price_currency") or "USD"
    availability = row.get("availability") or seed_data.get("availability") or "unknown"
    variants = _normalize_seed_variants(seed_data)
    external_product_id = seed_data.get("external_product_id") or _stable_external_product_id(
        row.get("canonical_url") or row.get("destination_url")
    )

    in_stock = True
    if isinstance(availability, str):
        in_stock = availability.lower() not in {"out_of_stock", "outofstock", "sold_out"}

    return {
        "id": external_product_id,
        "product_id": external_product_id,
        "merchant_id": None,
        "title": title or "External product",
        "description": seed_data.get("description") or "",
        "price": price_amount or 0,
        "currency": price_currency,
        "image_url": image_url,
        "platform": "external",
        "availability": availability,
        "in_stock": in_stock,
        "variants": variants,
        "external_seed_id": row.get("id"),
        "external_redirect_url": redirect_url,
        "external_destination_url": row.get("destination_url"),
        "disclosure_text": row.get("disclosure_text") or seed_data.get("disclosure_text"),
        "partner_type": row.get("partner_type") or seed_data.get("partner_type"),
        "source": "external_seed",
        "orderable": False,
    }


def _is_status_active(status: Any) -> bool:
    """
    Normalize and check product status.

    We accept both the ProductStatus enum and raw string values.
    """
    if isinstance(status, ProductStatus):
        return status == ProductStatus.ACTIVE
    if isinstance(status, str):
        return status.lower() == ProductStatus.ACTIVE.value
    return False


def _is_product_sellable(product: Any) -> bool:
    """
    Shared visibility rule used across all search paths:
    - status must be ACTIVE
    - orderable must not be explicitly False

    Important nuance: for StandardProduct objects we only treat
    orderable=False as a hard block when the field was explicitly
    set by the ingestion pipeline. If the field is missing and the
    Pydantic default False is used, we consider it "unspecified" and
    allow the product to surface (status gating still applies).
    """
    status = getattr(product, "status", ProductStatus.ACTIVE)
    if not _is_status_active(status):
        return False
    orderable = getattr(product, "orderable", None)

    # Detect whether orderable was explicitly provided on the model.
    explicit_fields = getattr(product, "__fields_set__", None)
    if isinstance(explicit_fields, set) and "orderable" in explicit_fields:
        if orderable is False:
            return False

    # When orderable is unspecified (field not set), we follow the
    # "orderable != false" contract and allow the product through.
    return True


def _is_dict_sellable(data: Dict[str, Any]) -> bool:
    """
    Sellable check for raw dict rows (e.g. cache fallbacks).
    """
    status = data.get("status") or ProductStatus.ACTIVE.value
    orderable = data.get("orderable")
    return _is_status_active(status) and orderable is not False


def _is_product_visible_for_creator_featured(product: Any) -> bool:
    """
    Relaxed visibility for the Creator Featured surface.

    For the home "Featured for you" grid we still require ACTIVE
    status but do not treat orderable=False as a hard block so that
    the pool can include a broader set of inspirational products.
    """
    status = getattr(product, "status", ProductStatus.ACTIVE)
    return _is_status_active(status)


def _is_dict_visible_for_creator_featured(data: Dict[str, Any]) -> bool:
    """
    Relaxed visibility for raw dict rows in Creator Featured surfaces.
    """
    status = data.get("status") or ProductStatus.ACTIVE.value
    return _is_status_active(status)


async def _load_product_by_id(
    product_id: str,
    *,
    merchant_id: Optional[str] = None,
) -> Optional[StandardProduct]:
    """
    Load a single product from cache by product_id/platform_product_id.

    Important: product_id can collide across merchants/platforms, so we always
    prefer filtering by merchant_id when available and ordering by cached_at.
    """
    from db.database import database

    pid = (product_id or "").strip()
    mid = (merchant_id or "").strip() or None
    if not pid:
        return None

    query = """
    SELECT product_data
    FROM products_cache
    WHERE (:mid IS NULL OR merchant_id = :mid)
      AND (expires_at IS NULL OR expires_at > NOW())
      AND (
        product_data->>'product_id' = :pid
        OR platform_product_id = :pid
        OR product_data->>'id' = :pid
      )
    ORDER BY cached_at DESC
    LIMIT 1
    """
    try:
        row = await database.fetch_one(query, {"pid": pid, "mid": mid})
        if row and "product_data" in row:
            try:
                return StandardProduct.parse_obj(row["product_data"])
            except Exception:
                return None
    except Exception:
        return None
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

    # Shopify currency correction (避免缓存里币种默认 USD 导致前端单位错误).
    shop_currency = await _resolve_shopify_currency_for_merchant(merchant_id)
    if shop_currency:
        for p in products:
            if (p.platform or "").lower() == "shopify":
                p.currency = shop_currency

    # Visibility: only surface sellable products to the agent front-end.
    visible: List[StandardProduct] = []
    for p in products:
        if not _is_product_sellable(p):
            continue
        visible.append(p)

    # In-memory filtering based on query/category/price
    filtered: List[StandardProduct] = visible

    q = (filters.query or "").strip().lower()
    if q:
        def matches_query(prod: StandardProduct) -> bool:
            title = (prod.title or "").lower()
            desc = (prod.description or "").lower()
            ptype = (prod.product_type or "").lower()
            sku = (getattr(prod, "sku", None) or "").lower()
            variant_skus = []
            try:
                for v in getattr(prod, "variants", None) or []:
                    vs = getattr(v, "sku", None)
                    if vs:
                        variant_skus.append(str(vs).lower())
            except Exception:
                variant_skus = []
            return q in title or q in desc or q in ptype or (sku and q in sku) or any(q in s for s in variant_skus)

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
    source: Optional[str] = None
    if creator_meta:
        creator_id = creator_meta.creator_id
        creator_name = creator_meta.creator_name
        source = creator_meta.source

    # Creator surfaces (creator-agent UI + creator category service) are
    # allowed to use a broader cross-merchant pool and slightly more
    # permissive visibility rules (do not drop products solely because
    # orderable is false).
    is_creator_surface = source in ("creator-agent-ui", "creator-category-service")

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
        """Fetch creator-curated picks plus top-selling products for a creator."""
        if not creator_id:
            return []

        # Helper: build a stable dedupe key for a product.
        def _product_key(prod: StandardProduct) -> tuple[str, str]:
            return (
                str(getattr(prod, "merchant_id", "") or ""),
                str(getattr(prod, "product_id", None) or getattr(prod, "id", "") or ""),
            )

        # First, try to load explicit creator_picks (manual curation).
        pick_products: List[StandardProduct] = []
        seen_keys: set[tuple[str, str]] = set()

        async def _fetch_product_any_merchant(product_id: str) -> Optional[StandardProduct]:
            row = await database.fetch_one(
                """
                SELECT merchant_id, product_data
                FROM products_cache
                WHERE (
                  platform_product_id = :pid
                  OR product_data->>'id' = :pid
                  OR product_data->>'product_id' = :pid
                )
                ORDER BY cached_at DESC
                LIMIT 1
                """,
                {"pid": product_id},
            )
            if not row:
                return None
            merchant_id = row.get("merchant_id") if isinstance(row, dict) else None
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
                if merchant_id:
                    product.merchant_id = merchant_id
                return product
            except Exception:
                return None

        pick_rows = await database.fetch_all(
            """
            SELECT product_id, rank
            FROM creator_picks
            WHERE creator_id = :creator_id
            ORDER BY rank ASC
            LIMIT :limit
            """,
            {"creator_id": creator_id, "limit": max_candidates},
        )

        for row in pick_rows:
            if isinstance(row, dict):
                pid = str(row.get("product_id") or "").strip()  # type: ignore[union-attr]
                rank = row.get("rank")
            else:
                pid = str(getattr(row, "product_id", "") or "").strip()
                rank = getattr(row, "rank", None)
            if not pid:
                continue
            prod = await _fetch_product_any_merchant(pid)
            if not prod or not _is_product_sellable(prod):
                continue
            key = _product_key(prod)
            if key in seen_keys:
                continue
            # Mark as explicit creator pick so downstream UIs can filter.
            meta = getattr(prod, "platform_metadata", None) or {}
            try:
                meta = dict(meta)
            except Exception:
                meta = {}
            meta["creator_pick"] = True
            if rank is not None:
                meta["creator_pick_rank"] = int(rank)
            prod.platform_metadata = meta  # type: ignore[attr-defined]
            seen_keys.add(key)
            pick_products.append(prod)
            if len(pick_products) >= max_candidates:
                break

        # Then, mine historical orders for additional popular products.
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

        order_products: List[StandardProduct] = []
        if popularity:
            for (m_id, pid), _count in popularity.most_common(max_candidates * 2):
                prod = await _fetch_product(m_id, pid)
                if not prod or not _is_product_sellable(prod):
                    continue
                key = _product_key(prod)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                order_products.append(prod)
                if len(pick_products) + len(order_products) >= max_candidates:
                    break

        # Creator picks first, then historical top sellers.
        return pick_products + order_products

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
                if not prod:
                    continue
                if not _is_product_sellable(prod):
                    continue
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
                prod = StandardProduct(**product_data)
                if not _is_product_sellable(prod):
                    continue
                products.append(prod)
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

    # Optional: normalized intent safety hints from upstream (LLM or gateway).
    # Shape is intentionally flexible, but we expect fields like:
    # - high_level_intent: "toys_kids_collectibles" | "adult_toys_lingerie" | ...
    # - forbid_adult_and_lingerie: bool
    # - filter_out_adult_and_lingerie: bool
    intent_safety: Dict[str, Any] = {}
    if payload.intent_safety:
        try:
            intent_safety = dict(payload.intent_safety)
        except Exception:
            intent_safety = {}
    elif request_metadata:
        raw = request_metadata.get("intent_safety")
        if isinstance(raw, dict):
            intent_safety = raw

    # Fetch candidate merchants. For generic agent surfaces we restrict to
    # PSP-connected merchants; for creator surfaces we allow all active
    # merchants so that creator experiences can span multiple brands.
    if is_creator_surface:
        merchant_rows = await database.fetch_all(
            """
            SELECT merchant_id, business_name
            FROM merchant_onboarding
            WHERE status NOT IN ('deleted', 'rejected')
            LIMIT 100
            """
        )
    else:
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

    has_merchants = bool(merchant_map)

    # Cold start & intent detection.
    q_raw = filters.query or ""
    q = q_raw.strip()
    q_lower = q.lower()
    q_ascii = _strip_accents(q_lower)
    q_tokens = _tokenize(q_ascii)
    q_compact = re.sub(r"[^a-z0-9]+", "", q_lower)

    # SKU-like queries: often numeric or short alphanumerics; we treat them as strong anchors for recall.
    sku_like_query = bool(re.fullmatch(r"[a-z0-9\\-]{6,}", q_ascii) and any(ch.isdigit() for ch in q_ascii))

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

    # Query-level toy outfit/accessory intent (e.g. "clothes for my Labubu", "doll outfit").
    toy_outfit_intent_query = bool(
        toys_intent_query
        and (
            re.search(r"\bclothes\b", q_ascii)
            or re.search(r"\bclothing\b", q_ascii)
            or re.search(r"\boutfit\b", q_ascii)
            or re.search(r"\bdoll\s+outfit\b", q_ascii)
            or re.search(r"\bdoll\s+clothes\b", q_ascii)
            or re.search(r"\baccessor(?:y|ies)\b", q_ascii)
            or re.search(r"\bhat\b", q_ascii)
            or "衣服" in q_lower
            or "穿" in q_lower
        )
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

    # Intent safety overrides from upstream (when provided).
    # This allows LLM/frontends to explicitly declare that adult/lingerie
    # products must be excluded even if the raw query is ambiguous.
    try:
        high_level_intent = str(intent_safety.get("high_level_intent") or "").lower()
        forbid_adult = bool(intent_safety.get("forbid_adult_and_lingerie"))
        filter_adult = bool(intent_safety.get("filter_out_adult_and_lingerie"))
        outerwear_prefs = intent_safety.get("outerwear_preferences") or {}
        beauty_prefs = intent_safety.get("beauty_preferences") or {}
    except Exception:
        high_level_intent = ""
        forbid_adult = False
        filter_adult = False
        outerwear_prefs = {}
        beauty_prefs = {}

    # If upstream explicitly says "kids/collectibles toys" and does not allow
    # adult/lingerie, we respect that by:
    # - Disabling lingerie intent
    # - Enabling exclusion flags
    if high_level_intent == "toys_kids_collectibles" and (forbid_adult or filter_adult):
        lingerie_intent_query = False
        # Strong negative constraint: do not surface lingerie/underwear at all.
        exclude_lingerie = True
        exclude_underwear = True

    # Outerwear preferences (minimal wiring for hoodies):
    # - if exclude_types includes "hoodie", force exclude_hoodies.
    # - if only_types is exactly ["hoodie"], we will later filter to hoodie-like items.
    only_hoodies = False
    try:
        exclude_types = [str(t).lower() for t in (outerwear_prefs.get("exclude_types") or [])]
        only_types = [str(t).lower() for t in (outerwear_prefs.get("only_types") or [])]
    except Exception:
        exclude_types = []
        only_types = []
    if "hoodie" in exclude_types:
        exclude_hoodies = True
    if only_types and all(t == "hoodie" for t in only_types):
        only_hoodies = True

    # Beauty preferences are consumed later in per-product filtering; we just
    # normalize them here for reuse.
    beauty_primary_category = ""
    beauty_exclude_tags: list[str] = []
    try:
        beauty_primary_category = str(beauty_prefs.get("primary_category") or "").lower()
        beauty_exclude_tags = [str(t).lower() for t in (beauty_prefs.get("exclude_tags") or [])]
    except Exception:
        beauty_primary_category = ""
        beauty_exclude_tags = []

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

    # Generic cold-weather clothing intent: for creator surfaces, when the user
    # asks for "clothes" in a cold context (e.g. temperature dropping), we
    # default to excluding lingerie/underwear unless explicitly requested.
    generic_clothes = False
    cold_hint = False
    if q_lower:
        if any(
            token in q_lower
            for token in [
                "衣服",
                "穿点",
                "穿些",
                "外套",
                "大衣",
                "羽绒服",
                "clothes",
                "coat",
                "jacket",
                "sweater",
            ]
        ):
            generic_clothes = True
        if any(
            token in q_lower
            for token in [
                "冷",
                "降温",
                "变冷",
                "很冷",
                "temperature",
                "degrees",
                "度",
            ]
        ):
            cold_hint = True

    if is_creator_surface and generic_clothes and cold_hint and not lingerie_intent_query:
        exclude_lingerie = True
        exclude_underwear = True

    # Whether we are serving the Creator Featured grid: creator surface + empty query.
    creator_featured_mode = is_creator_surface and not q

    # Construct reply for look-intent queries: similar items + disclaimer/prompt.
    reply_text: Optional[str] = None
    if look_intent:
        reply_text = (
            "I can’t guarantee an exact match for that outfit, "
            "but here are similar items inspired by Nina Studio’s looks. "
            "If you share a link or photo of the outfit, I can refine these suggestions."
        )

    # Special-case: Creator Featured cold-start grid.
    # For this surface we want a broad, cache-first view of ACTIVE + in-stock
    # products across merchants so that the initial grid is rich enough, even
    # when there is little or no creator-specific history.
    if creator_featured_mode:
        cache_limit = max(limit * max(page, 1) * 2, 20)
        rows = await database.fetch_all(
            """
            SELECT merchant_id, product_data
            FROM products_cache
            WHERE (product_data->>'status') = 'active'
              AND COALESCE((product_data->>'inventory_quantity')::int, 0) > 0
            ORDER BY cached_at DESC
            LIMIT :limit
            """,
            {"limit": cache_limit},
        )
        mapped: list[dict[str, Any]] = []
        seen_keys: set[tuple[str, str]] = set()

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
                if not _is_product_visible_for_creator_featured(prod):
                    continue
                item = _standard_to_shop_product(prod)
                item["merchant_name"] = merchant_map.get(prod.merchant_id)
                key = (str(prod.merchant_id), str(item["id"]))
                if key in seen_keys:
                    continue
                mapped.append(item)
                seen_keys.add(key)
            except Exception:
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
                if not _is_dict_visible_for_creator_featured(product_data):
                    continue
                if pid and title and price is not None:
                    key = (str(merchant_id), str(pid))
                    if key in seen_keys:
                        continue
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

        if mapped:
            start_idx = (page - 1) * limit
            page_items = mapped[start_idx : start_idx + limit]
            return {
                "products": page_items,
                "total": len(mapped),
                "page": page,
                "page_size": len(page_items),
                "reply": reply_text,
                "metadata": {
                    "query_source": "creator_featured_cache",
                    "fetched_at": datetime.utcnow().isoformat(),
                    "merchants_searched": len(merchant_map),
                    "creator_id": creator_id,
                    "creator_name": creator_name,
                },
            }

    external_seed_wrappers: list[dict[str, Any]] = []
    try:
        seed_limit = min(max(limit * max(page, 1) * 2, 30), 200)
        seed_params: Dict[str, Any] = {"limit": seed_limit}
        seed_where = "status = 'active'"
        if q_lower:
            seed_where += (
                " AND (LOWER(COALESCE(title,'')) LIKE :like"
                " OR LOWER(COALESCE(domain,'')) LIKE :like"
                " OR LOWER(COALESCE(canonical_url,'')) LIKE :like"
                " OR LOWER(COALESCE(destination_url,'')) LIKE :like)"
            )
            seed_params["like"] = f"%{q_lower}%"

        seed_rows = await database.fetch_all(
            f"""
            SELECT *
            FROM external_product_seeds
            WHERE {seed_where}
            ORDER BY updated_at DESC, created_at DESC
            LIMIT :limit
            """,
            seed_params,
        )

        seen_external_ids: set[str] = set()
        for row in seed_rows:
            row_dict = dict(row) if isinstance(row, dict) else {}
            seed_data = _ensure_seed_data_obj(row_dict.get("seed_data"))
            dest = row_dict.get("destination_url") or seed_data.get("destination_url")
            if not isinstance(dest, str) or not dest.startswith(("http://", "https://")):
                continue

            canonical_url = row_dict.get("canonical_url") or seed_data.get("canonical_url") or dest
            external_id = seed_data.get("external_product_id") or _stable_external_product_id(canonical_url or dest)
            if not external_id or external_id in seen_external_ids:
                continue

            title = row_dict.get("title") or seed_data.get("title") or ""
            domain = row_dict.get("domain") or seed_data.get("domain") or ""
            blob = " ".join([title, domain, canonical_url or "", dest]).lower().strip()
            blob_ascii = _strip_accents(blob)
            blob_compact = re.sub(r"[^a-z0-9]+", "", blob_ascii)

            if q_lower:
                if q_lower in blob:
                    score = 0.85
                elif q_compact and q_compact in blob_compact:
                    score = 0.7
                elif _fuzzy_token_match(q_tokens, _tokenize(blob_ascii), max_dist=1):
                    score = 0.6
                else:
                    continue
            else:
                score = 0.15

            price_amount = row_dict.get("price_amount") or seed_data.get("price_amount")
            if filters.price_min is not None and price_amount is not None and price_amount < filters.price_min:
                continue
            if filters.price_max is not None and price_amount is not None and price_amount > filters.price_max:
                continue

            availability = row_dict.get("availability") or seed_data.get("availability") or "unknown"
            if filters.in_stock_only and isinstance(availability, str):
                if availability.lower() in {"out_of_stock", "outofstock", "sold_out"}:
                    continue

            redirect_url = await _make_external_redirect_url(
                market=str(row_dict.get("market") or "US"),
                tool=str(row_dict.get("tool") or "*"),
                destination_url=dest,
                utm_template=row_dict.get("utm_template") or seed_data.get("utm_template"),
                ctx={"seedId": row_dict.get("id")},
            )
            if not redirect_url:
                continue

            product = _external_seed_to_shop_product(
                row=row_dict,
                seed_data=seed_data,
                redirect_url=redirect_url,
            )
            external_seed_wrappers.append(
                {
                    "product": product,
                    "merchant_name": None,
                    "relevance_score": score,
                }
            )
            seen_external_ids.add(external_id)
    except Exception as e:
        logger.info("multi.external_seeds.failed", extra={"error": str(e)})

    if not has_merchants and not external_seed_wrappers:
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

    # Cold start: empty query falls back to creator/global top sellers and cache/live fallbacks.
    if not q:
        source = "creator_top_sellers"
        top_sellers = await _load_creator_top_sellers(max_candidates=limit * 2)
        mapped: list[dict[str, Any]] = []
        seen_keys: set[tuple[str, str]] = set()

        def _creator_featured_visible_product(prod: StandardProduct) -> bool:
            if creator_featured_mode:
                return _is_product_visible_for_creator_featured(prod)
            return _is_product_sellable(prod)

        def _creator_featured_visible_dict(data: Dict[str, Any]) -> bool:
            if creator_featured_mode:
                return _is_dict_visible_for_creator_featured(data)
            return _is_dict_sellable(data)

        for prod in top_sellers[: limit * page]:
            if not _creator_featured_visible_product(prod):
                continue
            item = _standard_to_shop_product(prod)
            item["merchant_name"] = merchant_map.get(prod.merchant_id)
            mapped.append(item)
            key = (str(prod.merchant_id), str(item["id"]))
            seen_keys.add(key)

        # If creator-specific history is thin, blend in global top sellers
        # so that the "Featured for you" surface can still show a richer
        # cross-merchant pool rather than stopping at a small handful of
        # previously ordered items.
        if len(mapped) < limit:
            global_top = await _load_global_top_sellers(max_candidates=limit * 2)
            if global_top:
                source = "creator_plus_global_top_sellers"
                for prod in global_top:
                    if not _creator_featured_visible_product(prod):
                        continue
                    key = (str(prod.merchant_id), str(prod.product_id or prod.id))
                    if key in seen_keys:
                        continue
                    item = _standard_to_shop_product(prod)
                    item["merchant_name"] = merchant_map.get(prod.merchant_id)
                    mapped.append(item)
                    seen_keys.add(key)
                    if len(mapped) >= limit * page:
                        break

        # If still below the desired pool size, fall back to recent cached products.
        # For Creator Featured surfaces we explicitly bias toward ACTIVE + in-stock
        # products across all merchants so that the initial grid has a richer pool.
        if len(mapped) < limit:
            cache_limit = max(limit * max(page, 1) * 2, 20)
            if creator_featured_mode:
                rows = await database.fetch_all(
                    """
                    SELECT merchant_id, product_data
                    FROM products_cache
                    WHERE (product_data->>'status') = 'active'
                      AND COALESCE((product_data->>'inventory_quantity')::int, 0) > 0
                    ORDER BY cached_at DESC
                    LIMIT :limit
                    """,
                    {"limit": cache_limit},
                )
            else:
                rows = await database.fetch_all(
                    """
                    SELECT merchant_id, product_data
                    FROM products_cache
                    ORDER BY cached_at DESC
                    LIMIT :limit
                    """,
                    {"limit": cache_limit},
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
                    if (prod.platform or "").lower() == "shopify":
                        shop_currency = await _resolve_shopify_currency_for_merchant(
                            prod.merchant_id
                        )
                        if shop_currency:
                            prod.currency = shop_currency
                    if not _creator_featured_visible_product(prod):
                        continue
                    item = _standard_to_shop_product(prod)
                    item["merchant_name"] = merchant_map.get(prod.merchant_id)
                    key = (str(prod.merchant_id), str(item["id"]))
                    if key in seen_keys:
                        continue
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
                    if isinstance(merchant_id, str) and merchant_id:
                        try:
                            shop_currency = await _resolve_shopify_currency_for_merchant(merchant_id)
                            if shop_currency:
                                currency = shop_currency
                        except Exception:
                            pass
                    image_url = (
                        product_data.get("image_url")
                        or (product_data.get("images") or [{}])[0].get("src")
                        if isinstance(product_data.get("images"), list)
                        else None
                    )
                    if not _creator_featured_visible_dict(product_data):
                        continue
                    if pid and title and price is not None:
                        key = (str(merchant_id), str(pid))
                        if key in seen_keys:
                            continue
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

        # Last resort: fetch from merchants if cache is still empty. For
        # creator-agent surfaces we prefer a cache-only view so that the
        # Featured grid can reflect the broader catalog instead of being
        # constrained by realtime API limits.
        if not mapped and merchant_map:
            source = "live_merchant_fallback"
            per_merchant = min(max(limit * 2, 10), 200)
            for mid, name in merchant_map.items():
                try:
                    products, _src, _err = await get_products_hybrid(
                        merchant_id=mid,
                        limit=per_merchant,
                        agent_id="shopping_ai_multi_live",
                        background_tasks=background_tasks,
                        force_cache_only=True,
                    )
                    shop_currency = await _resolve_shopify_currency_for_merchant(mid)
                    for p in products:
                        if not _creator_featured_visible_product(p):
                            continue
                        if shop_currency and (p.platform or "").lower() == "shopify":
                            p.currency = shop_currency
                        item = _standard_to_shop_product(p)
                        item["merchant_name"] = name
                        mapped.append(item)
                except Exception:
                    continue

        if external_seed_wrappers:
            mapped.extend([w["product"] for w in external_seed_wrappers])

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
            shop_currency = await _resolve_shopify_currency_for_merchant(mid)
            for p in products:
                if shop_currency and (p.platform or "").lower() == "shopify":
                    p.currency = shop_currency
                merchant_products.append((p, name))
        except Exception:
            # Ignore individual merchant failures to keep cross-merchant search robust
            continue

    # Recall boost: when the user asks a specific query (e.g. a character name),
    # searching only a small "top-N" slice per merchant can miss relevant items.
    # Pull additional candidates directly from products_cache using cheap text matching.
    if merchant_map:
        try:
            anchor_terms: List[str] = []
            if "labubu" in q_ascii:
                anchor_terms = ["labubu"]
            elif q_tokens:
                anchor_terms = [q_tokens[0]]
                if len(q_tokens) > 1 and len(q_tokens[1]) >= 4:
                    anchor_terms.append(q_tokens[1])
            elif len(q_compact) >= 4:
                anchor_terms = [q_compact]

            if anchor_terms:
                likes = [f"%{t.lower()}%" for t in anchor_terms if t]
                # Clamp so we don't overfetch too much from cache.
                cache_limit = min(max(limit * max(page, 1) * 6, 120), 900)

                where_clauses: List[str] = []
                params: Dict[str, Any] = {
                    "merchant_ids": list(merchant_map.keys()),
                    "cache_limit": cache_limit,
                }
                for idx, like in enumerate(likes):
                    key = f"like_{idx}"
                    params[key] = like
                    where_clauses.append(
                        "("
                        "LOWER(COALESCE(product_data->>'title','')) LIKE :" + key
                        + " OR LOWER(COALESCE(product_data->>'description','')) LIKE :" + key
                        + " OR LOWER(COALESCE(product_data->>'product_type','')) LIKE :" + key
                        + " OR LOWER(COALESCE(product_data->>'sku','')) LIKE :" + key
                        + ")"
                    )
                    # Variant SKUs are nested; for SKU-like queries we allow a bounded JSON text scan.
                    if sku_like_query:
                        where_clauses.append("LOWER(CAST(product_data AS TEXT)) LIKE :" + key)

                rows = await database.fetch_all(
                    """
                    SELECT merchant_id, product_data
                    FROM products_cache
                    WHERE (expires_at IS NULL OR expires_at > NOW())
                      AND merchant_id = ANY(:merchant_ids)
                      AND ("""
                    + " OR ".join(where_clauses)
                    + """)
                    ORDER BY cached_at DESC
                    LIMIT :cache_limit
                    """,
                    params,
                )

                for row in rows:
                    mid = row.get("merchant_id") if isinstance(row, dict) else None
                    if not mid:
                        continue
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
                        prod.merchant_id = prod.merchant_id or str(mid)
                        merchant_products.append((prod, merchant_map.get(str(mid), "")))
                    except Exception:
                        continue
        except Exception as e:
            logger.info(
                "multi.cache_query_boost.failed",
                extra={"query": q, "error": str(e)},
            )

    # In-memory filtering and simple relevance scoring (reuse Agent API logic)
    filtered_products: list[dict[str, Any]] = []

    for product, merchant_name in merchant_products:
        # Visibility: only surface sellable products to the agent front-end.
        if not _is_product_sellable(product):
            continue

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
                (getattr(product, "sku", None) or "").lower(),
                " ".join(
                    [
                        str(getattr(v, "sku", "")).lower()
                        for v in (getattr(product, "variants", None) or [])
                        if getattr(v, "sku", None)
                    ]
                ),
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

        # "Only hoodie" constraint from outerwear preferences: if set,
        # keep only hoodie-like products in the pool.
        if only_hoodies:
            hoodie_markers = [
                "hoodie",
                "hoodies",
                "sweatshirt",
                "sudadera",
            ]
            if not any(tok in blob_for_filters for tok in hoodie_markers):
                continue

        # Beauty preference exclusions: when exclude_tags are present, drop
        # products whose text contains those markers (best-effort).
        if beauty_exclude_tags:
            if any(tag in blob_for_filters_ascii for tag in beauty_exclude_tags):
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
            sku = (getattr(product, "sku", None) or "").lower()
            variant_skus = []
            try:
                for v in getattr(product, "variants", None) or []:
                    vs = getattr(v, "sku", None)
                    if vs:
                        variant_skus.append(str(vs).lower())
            except Exception:
                variant_skus = []
            blob = " ".join([title, description, product_type, sku, " ".join(variant_skus)]).strip()
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
            # Keep underwear/lingerie out of toy queries unless the user explicitly asked for lingerie.
            underwear_tokens_for_flags = [
                "lingerie",
                "underwear",
                "bra",
                "panties",
                "panty",
                "briefs",
                "thong",
                "sleepwear",
                "night dress",
                "nightdress",
                "nightgown",
                "sexy",
                "lace",
                "push-up",
                "push up",
                "backless",
                "ropa interior",
                "sujetador",
                "bragas",
                "calzoncillos",
            ]
            is_underwear_like = any(tok in blob_for_filters_ascii for tok in underwear_tokens_for_flags)

            # Character anchors: for queries like "clothes for my Labubu",
            # allow character-matched items as toy-like (but still apply underwear exclusion above).
            character_anchors: List[str] = []
            if "labubu" in q_ascii:
                character_anchors.append("labubu")
            is_character_match = any(a in blob_for_filters_ascii for a in character_anchors) if character_anchors else False

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
            ]
            is_toy_like = any(tok in blob_for_filters_ascii for tok in toys_tokens)
            if not is_toy_like and is_character_match and toy_outfit_intent_query and not lingerie_intent_query:
                is_toy_like = True
            if is_toy_like and is_underwear_like and not lingerie_intent_query:
                is_toy_like = False
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

    if external_seed_wrappers:
        filtered_products.extend(external_seed_wrappers)

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
        product_item = item_wrapper.get("product")
        merchant_name = item_wrapper.get("merchant_name")

        if isinstance(product_item, StandardProduct):
            item = _standard_to_shop_product(product_item)
        elif isinstance(product_item, dict):
            item = dict(product_item)
        else:
            continue

        if merchant_name and not item.get("merchant_name"):
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
    background_tasks: Optional[BackgroundTasks] = None,
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
    bt = background_tasks or BackgroundTasks()
    limit = min(payload.limit or 6, 30)
    background_tasks = background_tasks or BackgroundTasks()

    # Try loading base product from cache first
    base_product = await _load_product_by_id(payload.product_id, merchant_id=payload.merchant_id)

    # If not found in cache but we know the merchant, fall back to the
    # hybrid path so products that only exist in the realtime slice can
    # still be used as a similarity anchor.
    if not base_product and payload.merchant_id:
        try:
            products, _, _ = await get_products_hybrid(
                merchant_id=payload.merchant_id,
                limit=500,
                agent_id="shopping_ai_similar",
                background_tasks=bt,
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

    def _strip_accents_text(text: str) -> str:
        if not text:
            return ""
        return "".join(
            c
            for c in unicodedata.normalize("NFKD", text)
            if not unicodedata.combining(c)
        )

    def _product_blob_ascii(prod: Optional[StandardProduct]) -> str:
        if not prod:
            return ""
        tags = getattr(prod, "tags", None) or []
        if not isinstance(tags, list):
            tags = []
        raw = " ".join(
            [
                (prod.title or ""),
                (prod.description or ""),
                (prod.product_type or ""),
                " ".join([str(t) for t in tags if t]),
            ]
        ).lower()
        return _strip_accents_text(raw)

    def _is_underwear_like_blob(blob_ascii: str) -> bool:
        if not blob_ascii:
            return False
        tokens = [
            "lingerie",
            "underwear",
            "bra",
            "panties",
            "panty",
            "briefs",
            "thong",
            "sleepwear",
            "night dress",
            "nightdress",
            "nightgown",
            "sexy",
            "lace",
            "push-up",
            "push up",
            "backless",
            "ropa interior",
            "sujetador",
            "bragas",
            "calzoncillos",
        ]
        return any(tok in blob_ascii for tok in tokens)

    def _is_toy_context_blob(blob_ascii: str) -> bool:
        if not blob_ascii:
            return False
        tokens = [
            "toy",
            "toys",
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
        return any(tok in blob_ascii for tok in tokens)

    base_blob_ascii = _product_blob_ascii(base_product)
    base_is_toy_context = _is_toy_context_blob(base_blob_ascii)
    base_is_underwear_like = _is_underwear_like_blob(base_blob_ascii)
    exclude_underwear_candidates = bool(
        source == "creator-agent-ui" and base_is_toy_context and not base_is_underwear_like
    )

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

            if not _is_product_sellable(sp):
                continue
            if exclude_underwear_candidates:
                cand_blob = _product_blob_ascii(sp)
                if _is_underwear_like_blob(cand_blob):
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


if DEV_MODE:
    @router.get("/dev/queue-status")
    async def debug_queue_status() -> Dict[str, Any]:
        """
        Dev-only endpoint to inspect the agent task queue state.
        """
        return await agent_task_manager.snapshot()


@router.post("/creator/tasks", response_model=CreatorTaskStatus)
async def create_creator_task(
    request: CreatorTaskCreateRequest,
    background_tasks: BackgroundTasks,
) -> CreatorTaskStatus:
    """
    Async task creation endpoint for Creator Agent workloads.

    This provides an explicit queue-based API:
      - POST /agent/shop/v1/creator/tasks -> {task_id, status="queued"}
      - GET  /agent/shop/v1/creator/tasks/{task_id} -> status + result/error
    """
    operation = (request.operation or "").strip()
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

    # For now we support async tasks for the heavy operations only.
    if operation == "find_products_multi":
        payload = FindProductsMultiPayload(**request.payload)
        session_id = request.session_id or _derive_session_id_for_multi(payload, normalized_metadata)
        creator_id_for_hash = (
            payload.creator_id
            or (payload.metadata.creator_id if payload.metadata else None)
            or normalized_metadata.get("creator_id")
        )
        payload_key = {
            "query": payload.search.query,
            "category": payload.search.category,
            "price_min": payload.search.price_min,
            "price_max": payload.search.price_max,
            "in_stock_only": payload.search.in_stock_only,
            "creator_id": creator_id_for_hash,
        }
        payload_hash = AgentTaskManager.compute_payload_hash("find_products_multi", payload_key)
        task_id, _ = await agent_task_manager.enqueue(
            operation="find_products_multi",
            session_id=session_id,
            payload_hash=payload_hash,
            coro_factory=lambda: _handle_find_products_multi(
                payload, normalized_metadata, background_tasks
            ),
            request_id=request.request_id,
        )
        return CreatorTaskStatus(task_id=task_id, status="queued")

    if operation == "find_similar_products":
        payload = FindSimilarProductsPayload(**request.payload)
        session_id = request.session_id or _derive_session_id_for_similar(payload, normalized_metadata)
        creator_id_for_hash = (
            payload.creator_id
            or (payload.metadata.creator_id if payload.metadata else None)
            or normalized_metadata.get("creator_id")
        )
        payload_key = {
            "product_id": payload.product_id,
            "creator_id": creator_id_for_hash,
            "strategy": payload.strategy,
            "limit": payload.limit,
        }
        payload_hash = AgentTaskManager.compute_payload_hash("find_similar_products", payload_key)
        task_id, _ = await agent_task_manager.enqueue(
            operation="find_similar_products",
            session_id=session_id,
            payload_hash=payload_hash,
            coro_factory=lambda: _handle_find_similar_products(
                payload, normalized_metadata, background_tasks
            ),
            request_id=request.request_id,
        )
        return CreatorTaskStatus(task_id=task_id, status="queued")

    raise HTTPException(
        status_code=400,
        detail=f"Unsupported async operation for creator task: {operation}",
    )


@router.get("/creator/tasks/{task_id}", response_model=CreatorTaskStatus)
async def get_creator_task_status(task_id: str) -> CreatorTaskStatus:
    """
    Poll task status and (when ready) result for creator tasks.
    """
    # We intentionally keep this light-weight: it only introspects the in-memory
    # TaskRecord and does not perform any heavy operations.
    async with agent_task_manager._lock:  # type: ignore[attr-defined]
        # Internal use only; safe within this process.
        record = agent_task_manager._tasks.get(task_id)  # type: ignore[attr-defined]
        if not record:
            raise HTTPException(status_code=404, detail="TASK_NOT_FOUND")

        status = record.state.value
        result = record.result if record.state == record.state.SUCCEEDED else None
        error = record.error

    return CreatorTaskStatus(
        task_id=task_id,
        status=status,
        result=result,
        error=error,
    )


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
    match: Optional[StandardProduct] = await _load_product_by_id(product_id, merchant_id=merchant_id)
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
            match = await _load_product_by_id(product_id, merchant_id=merchant_id)
            if match:
                query_source = "product_cache_fallback"

    if not match:
        # Strong contract: this should not happen if product comes from find_products,
        # so treat it as PRODUCT_NOT_FOUND.
        # Final fallback for Shopify: the product may exist in the merchant's
        # Shopify store but not be present in our cache/hybrid slice yet.
        try:
            from services.merchant_store_service import get_merchant_active_stores
            from adapters.product_adapters import ShopifyProductAdapter
            from db.products import upsert_product_cache

            stores = await get_merchant_active_stores(merchant_id)
            shopify_store = next(
                (
                    s
                    for s in stores
                    if (s.get("platform") or "").lower() == "shopify"
                    and (s.get("domain") or "").strip()
                    and (s.get("api_key") or "").strip()
                ),
                None,
            )

            if shopify_store:
                shop_domain = str(shopify_store["domain"]).strip()
                access_token = str(shopify_store["api_key"]).strip()
                fetched, fetch_error = await ShopifyProductAdapter.fetch_product_by_id(
                    shop_domain=shop_domain,
                    access_token=access_token,
                    merchant_id=merchant_id,
                    product_id=product_id,
                )
                if fetched:
                    match = fetched
                    query_source = "shopify_admin_by_id"
                    # Best-effort: cache the fetched product so future calls are fast.
                    try:
                        background_tasks.add_task(
                            upsert_product_cache,
                            merchant_id,
                            "shopify",
                            str(product_id),
                            fetched.dict(),
                            6 * 60 * 60,
                        )
                    except Exception:
                        pass
                elif fetch_error and fetch_error != "NOT_FOUND":
                    raise HTTPException(
                        status_code=502,
                        detail={
                            "error": "SHOPIFY_PRODUCT_FETCH_FAILED",
                            "message": fetch_error,
                        },
                    )
        except HTTPException:
            raise
        except Exception:
            # If anything goes wrong in the fallback, keep the contract
            # and return PRODUCT_NOT_FOUND rather than leaking internals.
            pass

        if not match:
            raise HTTPException(status_code=404, detail="PRODUCT_NOT_FOUND")

    # Shopify currency correction (avoid stale USD labels from cache rows).
    if (match.platform or "").lower() == "shopify":
        shop_currency = await _resolve_shopify_currency_for_merchant(merchant_id)
        if shop_currency:
            match.currency = shop_currency

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

    review_summary = None
    seller_feedback_summary = None
    try:
        if not _reviews_enabled():
            raise RuntimeError("reviews_disabled")
        from services.reviews_service import get_review_summary_for_sku, get_seller_feedback_summary
        from observability.reviews_metrics import record_pdp_default_view

        review_summary = await get_review_summary_for_sku(
            merchant_id=merchant_id,
            platform=str(match.platform),
            platform_product_id=str(match.product_id or match.id),
            variant_id=None,
        )
        seller_feedback_summary = await get_seller_feedback_summary(merchant_id)

        dv = _reviews_default_view_override()
        if dv and review_summary:
            review_summary["default_view"] = dv
        record_pdp_default_view(str(review_summary.get("default_view") or "merchant"))
    except Exception:
        # PDP should stay available even when reviews are degraded.
        review_summary = None
        seller_feedback_summary = None

    return {
        "product": {
            **base,
            "attributes": attributes or None,
            "options": options or None,
            "product_options": legacy_product_options,
            "review_summary": review_summary,
            "seller_feedback_summary": seller_feedback_summary,
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


INVOKE_SHORT_WAIT_SECONDS_RAW = os.getenv("AGENT_SHOP_INVOKE_MAX_WAIT_SECONDS")
try:
    INVOKE_SHORT_WAIT_SECONDS = float(INVOKE_SHORT_WAIT_SECONDS_RAW) if INVOKE_SHORT_WAIT_SECONDS_RAW else 0.0
    if INVOKE_SHORT_WAIT_SECONDS < 0:
        INVOKE_SHORT_WAIT_SECONDS = 0.0
except ValueError:
    INVOKE_SHORT_WAIT_SECONDS = 0.0


@router.post("/invoke")
async def invoke_shop_operation(
    request: ShopGatewayRequest,
    background_tasks: BackgroundTasks,
    http_request: Request,
) -> Dict[str, Any]:
    """
    Unified entrypoint for Shopping AI frontend & LLM agents.

    Supported operations:
    - find_products
    - get_product_detail
    - list_sku_reviews
    - list_group_reviews
    - list_group_merchants
    - list_seller_feedback
    - list_review_entrypoints
    - resolve_review_intent
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
        http_request.state.operation = operation
        http_request.state.merchant_id = payload.product.merchant_id
        started = time.time()
        status_code = 200
        error_detail = None
        try:
            return await _handle_get_product_detail(payload.product, background_tasks)
        except HTTPException as e:
            status_code = int(e.status_code)
            error_detail = str(e.detail)
            raise
        except Exception as e:
            status_code = 500
            error_detail = type(e).__name__
            raise
        finally:
            try:
                from observability.reviews_metrics import record_invoke

                record_invoke(
                    operation=operation,
                    status_code=status_code,
                    duration_seconds=max(0.0, time.time() - started),
                    error_detail=error_detail,
                )
            except Exception:
                pass

    if operation == "list_sku_reviews":
        if not _reviews_enabled():
            raise HTTPException(status_code=404, detail="REVIEWS_DISABLED")
        payload = ListSkuReviewsPayload(**request.payload)
        http_request.state.operation = operation
        http_request.state.merchant_id = payload.sku.merchant_id
        started = time.time()
        status_code = 200
        error_detail = None
        try:
            from services.reviews_service import build_product_key, build_sku_key, list_product_reviews, list_sku_reviews

            f = payload.filters or ListSkuReviewsFilters()
            if bool(f.featured_only) and not _reviews_featured_enabled():
                return {"items": [], "next_cursor": None, "limit": int(f.limit or 20)}
            # If variant_id is omitted, treat as product-level listing across all variants.
            if payload.sku.variant_id is None or str(payload.sku.variant_id).strip() == "":
                product_key = build_product_key(
                    merchant_id=payload.sku.merchant_id,
                    platform=payload.sku.platform,
                    platform_product_id=payload.sku.platform_product_id,
                )
                return await list_product_reviews(
                    product_key=product_key,
                    has_media=bool(f.has_media),
                    rating=f.rating,
                    limit=int(f.limit or 20),
                    cursor=f.cursor,
                )
            sku_key = build_sku_key(
                merchant_id=payload.sku.merchant_id,
                platform=payload.sku.platform,
                platform_product_id=payload.sku.platform_product_id,
                variant_id=payload.sku.variant_id,
            )
            return await list_sku_reviews(
                sku_key=sku_key,
                featured_only=bool(f.featured_only),
                has_media=bool(f.has_media),
                rating=f.rating,
                limit=int(f.limit or 20),
                cursor=f.cursor,
            )
        except HTTPException as e:
            status_code = int(e.status_code)
            error_detail = str(e.detail)
            raise
        except Exception as e:
            status_code = 500
            error_detail = type(e).__name__
            raise
        finally:
            try:
                from observability.reviews_metrics import record_invoke

                record_invoke(
                    operation=operation,
                    status_code=status_code,
                    duration_seconds=max(0.0, time.time() - started),
                    error_detail=error_detail,
                )
            except Exception:
                pass

    if operation == "list_group_reviews":
        if not _reviews_enabled():
            raise HTTPException(status_code=404, detail="REVIEWS_DISABLED")
        payload = ListGroupReviewsPayload(**request.payload)
        http_request.state.operation = operation
        http_request.state.group_id = int(payload.group_id)
        started = time.time()
        status_code = 200
        error_detail = None
        try:
            from services.reviews_service import list_group_reviews

            f = payload.filters or ListGroupReviewsFilters()
            if bool(f.featured_only) and not _reviews_featured_enabled():
                return {"items": [], "next_cursor": None, "limit": int(f.limit or 20)}
            return await list_group_reviews(
                group_id=int(payload.group_id),
                merchant_ids=f.merchant_ids,
                featured_only=bool(f.featured_only),
                has_media=bool(f.has_media),
                limit=int(f.limit or 20),
                cursor=f.cursor,
            )
        except HTTPException as e:
            status_code = int(e.status_code)
            error_detail = str(e.detail)
            raise
        except Exception as e:
            status_code = 500
            error_detail = type(e).__name__
            raise
        finally:
            try:
                from observability.reviews_metrics import record_invoke

                record_invoke(
                    operation=operation,
                    status_code=status_code,
                    duration_seconds=max(0.0, time.time() - started),
                    error_detail=error_detail,
                )
            except Exception:
                pass

    if operation == "list_group_merchants":
        if not _reviews_enabled():
            raise HTTPException(status_code=404, detail="REVIEWS_DISABLED")
        payload = ListGroupMerchantsPayload(**request.payload)
        http_request.state.operation = operation
        http_request.state.group_id = int(payload.group_id)
        started = time.time()
        status_code = 200
        error_detail = None
        try:
            from services.reviews_service import get_group_counts_by_merchant

            counts = await get_group_counts_by_merchant(int(payload.group_id))
            return {"group_id": int(payload.group_id), "counts_by_merchant": counts}
        except HTTPException as e:
            status_code = int(e.status_code)
            error_detail = str(e.detail)
            raise
        except Exception as e:
            status_code = 500
            error_detail = type(e).__name__
            raise
        finally:
            try:
                from observability.reviews_metrics import record_invoke

                record_invoke(
                    operation=operation,
                    status_code=status_code,
                    duration_seconds=max(0.0, time.time() - started),
                    error_detail=error_detail,
                )
            except Exception:
                pass

    if operation == "list_seller_feedback":
        if not _reviews_enabled():
            raise HTTPException(status_code=404, detail="REVIEWS_DISABLED")
        payload = ListSellerFeedbackPayload(**request.payload)
        http_request.state.operation = operation
        http_request.state.merchant_id = payload.merchant_id
        started = time.time()
        status_code = 200
        error_detail = None
        try:
            from services.reviews_service import list_seller_feedback

            return await list_seller_feedback(
                merchant_id=payload.merchant_id,
                limit=int(payload.limit or 20),
                cursor=payload.cursor,
            )
        except HTTPException as e:
            status_code = int(e.status_code)
            error_detail = str(e.detail)
            raise
        except Exception as e:
            status_code = 500
            error_detail = type(e).__name__
            raise
        finally:
            try:
                from observability.reviews_metrics import record_invoke

                record_invoke(
                    operation=operation,
                    status_code=status_code,
                    duration_seconds=max(0.0, time.time() - started),
                    error_detail=error_detail,
                )
            except Exception:
                pass

    if operation == "list_review_entrypoints":
        payload = ListReviewEntrypointsPayload(**request.payload)
        http_request.state.operation = operation
        http_request.state.merchant_id = payload.subject.merchant_id if payload.subject else None
        started = time.time()
        status_code = 200
        error_detail = None
        try:
            read_allowed = bool(_reviews_enabled())
            write_allowed = False
            write_reason = "BUYER_SUBMIT_DISABLED"
            try:
                from services.buyer_reviews_service import buyer_submit_enabled, buyer_submit_merchant_allowed

                mid = ""
                try:
                    mid = (payload.subject.merchant_id if payload.subject else "") or ""
                except Exception:
                    mid = ""
                buyer_enabled = bool(buyer_submit_enabled())
                if not buyer_enabled:
                    write_allowed = False
                    write_reason = "BUYER_SUBMIT_DISABLED"
                elif not mid:
                    write_allowed = False
                    write_reason = "MISSING_SUBJECT"
                elif not bool(buyer_submit_merchant_allowed(mid)):
                    write_allowed = False
                    write_reason = "BUYER_SUBMIT_NOT_ALLOWED"
                else:
                    write_allowed = True
                    write_reason = "OK"
            except Exception:
                write_allowed = False
                write_reason = "BUYER_SUBMIT_DISABLED"

            items: List[Dict[str, Any]] = []

            # Read entrypoints (existing read path via invoke + review-media).
            for eid, prio in (
                ("PDP_SUMMARY", 100),
                ("PDP_TAB", 90),
                ("AGENT_CHAT_CARD", 80),
                ("SEARCH_SNIPPET", 30),
            ):
                items.append(
                    {
                        "entrypoint_id": eid,
                        "allowed": read_allowed,
                        "reason": "OK" if read_allowed else "REVIEWS_DISABLED",
                        "priority": prio,
                        "launch_modes": ["EMBED_CONFIG"],
                        "ui_spec": {"label": "Reviews"},
                        "policy_tags": ["reviews.read"],
                        "analytics_schema_version": 1,
                        "tracking_required_fields": ["entrypoint_id", "surface", "agent_id", "intent"],
                    }
                )

            # Write entrypoints (buyer submission flow, default gated by env flag).
            items.append(
                {
                    "entrypoint_id": "PDP_WRITE_REVIEW",
                    "allowed": write_allowed,
                    "reason": write_reason,
                    "priority": 70,
                    "launch_modes": ["EMBED_CONFIG"],
                    "ui_spec": {"label": "Write a review"},
                    "policy_tags": ["reviews.write"],
                    "analytics_schema_version": 1,
                    "tracking_required_fields": ["entrypoint_id", "surface", "agent_id", "intent"],
                }
            )

            return {"status": "success", "items": items}
        except Exception as e:
            status_code = 500
            error_detail = type(e).__name__
            raise
        finally:
            try:
                from observability.reviews_metrics import record_invoke

                record_invoke(
                    operation=operation,
                    status_code=status_code,
                    duration_seconds=max(0.0, time.time() - started),
                    error_detail=error_detail,
                )
            except Exception:
                pass

    if operation == "resolve_review_intent":
        payload = ResolveReviewIntentPayload(**request.payload)
        http_request.state.operation = operation
        http_request.state.merchant_id = payload.subject.merchant_id
        started = time.time()
        status_code = 200
        error_detail = None
        try:
            intent = (payload.intent or "").strip().lower()
            if intent not in {"read", "write"}:
                raise HTTPException(status_code=400, detail="INVALID_INTENT")

            subject = payload.subject
            merchant_id = (subject.merchant_id or "").strip()
            platform = (subject.platform or "").strip().lower()
            platform_product_id = (subject.platform_product_id or "").strip()
            variant_id = (subject.variant_id or "").strip()

            tracking = {
                "entrypoint_id": payload.entrypoint_id,
                "intent": intent,
                "surface": payload.surface,
                "agent_id": payload.agent_id,
            }

            if intent == "read":
                allowed = bool(_reviews_enabled())
                if not allowed:
                    return {
                        "status": "success",
                        "allowed": False,
                        "reason": "REVIEWS_DISABLED",
                        "launch_mode": None,
                        "target": None,
                        "tracking": tracking,
                    }
                embed_config = {
                    "type": "reviews_read",
                    "invoke": {
                        "operation": "list_sku_reviews",
                        "payload": {
                            "sku": {
                                "merchant_id": merchant_id,
                                "platform": platform,
                                "platform_product_id": platform_product_id,
                                "variant_id": variant_id or None,
                            },
                            "filters": {"limit": 20},
                        },
                    },
                }
                return {
                    "status": "success",
                    "allowed": True,
                    "reason": "OK",
                    "launch_mode": "EMBED_CONFIG",
                    "target": {"embed_config": embed_config},
                    "tracking": tracking,
                }

            # intent == "write"
            write_allowed = False
            try:
                from services.buyer_reviews_service import buyer_submit_enabled, buyer_submit_merchant_allowed

                buyer_enabled = bool(buyer_submit_enabled())
                merchant_allowed = bool(buyer_submit_merchant_allowed(merchant_id))
                write_allowed = buyer_enabled and merchant_allowed
            except Exception:
                write_allowed = False

            if not write_allowed:
                reason = "BUYER_SUBMIT_DISABLED"
                try:
                    from services.buyer_reviews_service import buyer_submit_enabled

                    if bool(buyer_submit_enabled()):
                        reason = "BUYER_SUBMIT_NOT_ALLOWED"
                except Exception:
                    reason = "BUYER_SUBMIT_DISABLED"
                return {
                    "status": "success",
                    "allowed": False,
                    "reason": reason,
                    "launch_mode": None,
                    "target": None,
                    "tracking": tracking,
                }

            embed_config = {
                "type": "buyer_review_submission",
                "requirements": {
                    "auth": "Bearer submission_token",
                    "idempotency_header": "Idempotency-Key",
                    "submission_token_issue": "server_side_only",
                },
                "endpoints": {
                    "proof_exchange_path": "/buyer/reviews/v1/verification/exchange",
                    "create_review_path": "/buyer/reviews/v1/reviews",
                    "get_review_path_template": "/buyer/reviews/v1/reviews/{review_id}",
                    "attach_media_path_template": "/buyer/reviews/v1/reviews/{review_id}/media",
                },
                "subject": {
                    "merchant_id": merchant_id,
                    "platform": platform,
                    "platform_product_id": platform_product_id,
                    "variant_id": variant_id or None,
                },
            }
            return {
                "status": "success",
                "allowed": True,
                "reason": "OK",
                "launch_mode": "EMBED_CONFIG",
                "target": {"embed_config": embed_config},
                "tracking": tracking,
            }
        except HTTPException as e:
            status_code = int(e.status_code)
            error_detail = str(e.detail)
            raise
        except Exception as e:
            status_code = 500
            error_detail = type(e).__name__
            raise
        finally:
            try:
                from observability.reviews_metrics import record_invoke

                record_invoke(
                    operation=operation,
                    status_code=status_code,
                    duration_seconds=max(0.0, time.time() - started),
                    error_detail=error_detail,
                )
            except Exception:
                pass

    if operation == "create_order":
        payload = CreateOrderPayload(**request.payload)
        return await _handle_create_order(payload.order)

    if operation == "find_products_multi":
        payload = FindProductsMultiPayload(**request.payload)
        session_id = _derive_session_id_for_multi(payload, normalized_metadata)
        creator_id_for_hash = (
            payload.creator_id
            or (payload.metadata.creator_id if payload.metadata else None)
            or normalized_metadata.get("creator_id")
        )
        payload_key = {
            "query": payload.search.query,
            "category": payload.search.category,
            "price_min": payload.search.price_min,
            "price_max": payload.search.price_max,
            "in_stock_only": payload.search.in_stock_only,
            "creator_id": creator_id_for_hash,
        }
        payload_hash = AgentTaskManager.compute_payload_hash(
            "find_products_multi", payload_key
        )
        task_id, future = await agent_task_manager.enqueue(
            operation="find_products_multi",
            session_id=session_id,
            payload_hash=payload_hash,
            coro_factory=lambda: _handle_find_products_multi(
                payload, normalized_metadata, background_tasks
            ),
        )
        try:
            if INVOKE_SHORT_WAIT_SECONDS > 0:
                try:
                    return await asyncio.wait_for(future, timeout=INVOKE_SHORT_WAIT_SECONDS)
                except asyncio.TimeoutError:
                    # Short-wait budget exceeded; keep task running in the background.
                    return {
                        "status": "pending",
                        "task_id": task_id,
                    }
            return await future
        except asyncio.CancelledError:
            # Client disconnected; best-effort cancellation.
            await agent_task_manager.cancel(task_id, reason="client_disconnect")
            raise

    if operation == "find_similar_products":
        payload = FindSimilarProductsPayload(**request.payload)
        session_id = _derive_session_id_for_similar(payload, normalized_metadata)
        creator_id_for_hash = (
            payload.creator_id
            or (payload.metadata.creator_id if payload.metadata else None)
            or normalized_metadata.get("creator_id")
        )
        payload_key = {
            "product_id": payload.product_id,
            "creator_id": creator_id_for_hash,
            "strategy": payload.strategy,
            "limit": payload.limit,
        }
        payload_hash = AgentTaskManager.compute_payload_hash(
            "find_similar_products", payload_key
        )
        task_id, future = await agent_task_manager.enqueue(
            operation="find_similar_products",
            session_id=session_id,
            payload_hash=payload_hash,
            coro_factory=lambda: _handle_find_similar_products(
                payload, normalized_metadata, background_tasks
            ),
        )
        try:
            if INVOKE_SHORT_WAIT_SECONDS > 0:
                try:
                    return await asyncio.wait_for(future, timeout=INVOKE_SHORT_WAIT_SECONDS)
                except asyncio.TimeoutError:
                    return {
                        "status": "pending",
                        "task_id": task_id,
                    }
            return await future
        except asyncio.CancelledError:
            await agent_task_manager.cancel(task_id, reason="client_disconnect")
            raise

    if operation == "submit_payment":
        payload = SubmitPaymentPayload(**request.payload)
        return await _handle_submit_payment(payload.payment)

    # For now we only support product operations here.
    raise HTTPException(
        status_code=400,
        detail=f"Unsupported operation: {operation}",
    )


@router.get("/review-media/{public_id}")
async def get_review_media(public_id: str, request: Request) -> Response:
    """
    Serves imported review media by unguessable public_id.
    Requires exp+sig query params (HMAC) and applies IP rate limiting.
    """
    started = time.time()
    status_code = 200
    bytes_served = 0
    sig_fail_reason: Optional[str] = None
    rate_limited = False
    try:
        if not _reviews_enabled():
            status_code = 404
            raise HTTPException(status_code=404, detail="REVIEWS_DISABLED")
        request.state.operation = "review_media"

        ip = _review_media_client_ip(request)
        if not _check_review_media_rate_limit(ip):
            rate_limited = True
            status_code = 429
            raise HTTPException(status_code=429, detail="RATE_LIMITED")

        exp_raw = (request.query_params.get("exp") or "").strip()
        sig = (request.query_params.get("sig") or "").strip()
        if not exp_raw or not sig:
            status_code = 403
            sig_fail_reason = "missing"
            raise HTTPException(status_code=403, detail="SIGNATURE_REQUIRED")

        try:
            exp = int(exp_raw)
        except Exception:
            status_code = 403
            sig_fail_reason = "bad_exp"
            raise HTTPException(status_code=403, detail="BAD_SIGNATURE")

        from services.reviews_service import verify_review_media_signature_with_reason, _allow_legacy_review_media_id

        ok, reason = verify_review_media_signature_with_reason(public_id=public_id, exp=exp, sig=sig)
        if not ok:
            status_code = 403
            sig_fail_reason = reason
            raise HTTPException(status_code=403, detail="BAD_SIGNATURE")

        media_row = await database.fetch_one(
            """
            SELECT m.id, m.public_id, m.type, m.file_path, m.file_hash
            FROM media_assets m
            JOIN product_reviews r ON r.id = m.review_id
            WHERE m.status = 'active'
              AND r.status IN ('active', 'under_review', 'folded')
              AND (
                m.public_id = :pid
                OR (:allow_legacy = true AND m.public_id IS NULL AND m.id = :legacy_id)
              )
            LIMIT 1
            """,
            {
                "pid": str(public_id),
                "allow_legacy": bool(_allow_legacy_review_media_id()),
                "legacy_id": int(public_id) if public_id.isdigit() else -1,
            },
        )
        if not media_row:
            status_code = 404
            raise HTTPException(status_code=404, detail="MEDIA_NOT_FOUND")

        etag = str(media_row["file_hash"] or "").strip() or None
        if etag and (request.headers.get("if-none-match") or "").strip() == f"\"{etag}\"":
            resp = Response(status_code=304)
            _set_media_cache_headers(resp, etag)
            return resp

        file_path_raw = str(media_row["file_path"] or "").strip()

        # S3-backed media: file_path is stored as `s3://bucket/key`.
        if file_path_raw.startswith("s3://"):
            try:
                import asyncio
                from starlette.concurrency import iterate_in_threadpool
                from starlette.responses import StreamingResponse

                import boto3
            except Exception:
                status_code = 500
                raise HTTPException(status_code=500, detail="MEDIA_STORAGE_UNAVAILABLE")

            # Parse `s3://bucket/key`
            try:
                rest = file_path_raw[len("s3://") :]
                bucket, key = rest.split("/", 1)
            except Exception:
                status_code = 404
                raise HTTPException(status_code=404, detail="MEDIA_NOT_FOUND")

            endpoint_url = (os.getenv("AWS_ENDPOINT_URL") or os.getenv("S3_ENDPOINT_URL") or "").strip() or None
            region = (os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "").strip() or None
            client = boto3.client("s3", region_name=region, endpoint_url=endpoint_url)

            try:
                obj = await asyncio.to_thread(client.get_object, Bucket=bucket, Key=key)
                body = obj["Body"]
                content_type = (obj.get("ContentType") or "").strip() or "application/octet-stream"
                content_length = obj.get("ContentLength")
            except Exception:
                status_code = 404
                raise HTTPException(status_code=404, detail="MEDIA_FILE_MISSING")

            # Streaming body is blocking; run iteration in a threadpool.
            resp = StreamingResponse(
                iterate_in_threadpool(body.iter_chunks(chunk_size=1024 * 1024)),
                media_type=content_type,
            )
            if content_length is not None:
                try:
                    resp.headers["Content-Length"] = str(int(content_length))
                except Exception:
                    pass
            _set_media_cache_headers(resp, etag)
            return resp

        # Default: local disk media under REVIEWS_IMPORT_DIR.
        file_path = os.path.realpath(file_path_raw)
        if not file_path or not os.path.exists(file_path):
            status_code = 404
            raise HTTPException(status_code=404, detail="MEDIA_FILE_MISSING")

        # Safety: only serve from REVIEWS_IMPORT_DIR.
        base = _reviews_media_import_dir()
        if not file_path.startswith(base + os.sep) and file_path != base:
            status_code = 404
            raise HTTPException(status_code=404, detail="MEDIA_NOT_FOUND")

        media_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
        bytes_served = int(os.path.getsize(file_path)) if os.path.exists(file_path) else 0
        resp = FileResponse(file_path, media_type=media_type)
        _set_media_cache_headers(resp, etag)
        return resp
    except HTTPException as e:
        status_code = int(e.status_code)
        raise
    finally:
        try:
            from observability.reviews_metrics import record_media_request

            record_media_request(
                status_code=status_code,
                duration_seconds=max(0.0, time.time() - started),
                bytes_served=bytes_served,
                sig_fail_reason=sig_fail_reason,
                rate_limited=rate_limited,
            )
        except Exception:
            pass
