"""
Agent 专用 API 路由
为 AI Agent 提供优化的电商接口
"""

from services.merchant_store_service import get_merchant_active_stores, get_primary_store
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Query, Header, Response
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from decimal import Decimal
from datetime import datetime
import json
import os
import time

from models.order import CreateOrderRequest, OrderResponse
from models.standard_product import StandardProduct
from db.database import database
from db.merchant_onboarding import get_merchant_onboarding
from db.orders import get_order, get_orders_by_merchant, update_payment_info
from routes.refund_api import process_refund
from routes.order_routes import cancel_order as admin_cancel_order
from routes.fulfillment_api import track_order_fulfillment
from routes.order_routes import create_new_order
from routes.agent_auth import AgentContext, get_agent_context, log_agent_request
from routes.agent_user_auth import AgentUserContext, get_agent_user_context
from utils.logger import logger
from utils.agent_search_intent import infer_query_overrides
from services.product_query_service import get_products_hybrid
from services.quote_service import QuoteError
from services.agent_ranking_service import (
    AgentRankingFeatures,
    get_agent_ranking_config,
    hydrate_quality_and_enrichment,
    passes_agent_gating,
    compute_agent_ranking_score,
    serialize_features_for_log,
)
from db.agent_product_events import log_product_events
from config.feature_flags import ENABLE_QUOTE_FIRST_ORDER_CREATE
from db.products import get_cached_products
import httpx
import uuid

from routes.reviews_invitation_issuer import mint_invitations_from_paid_order


router = APIRouter(prefix="/agent/v1", tags=["agent-api"])


# ============================================================================
# Helper Functions
# ============================================================================

def _normalize_buyer_ref(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    v = str(value).strip()
    return v or None


def _order_agent_user_ref(order: Dict[str, Any]) -> Optional[str]:
    meta = (order or {}).get("metadata")
    if isinstance(meta, dict):
        raw = meta.get("agent_user_ref") or meta.get("agentUserRef")
        if raw is None:
            return None
        s = str(raw).strip()
        return s or None
    return None


def _agent_user_matches_order_ref(*, stored_ref: str, agent_user: AgentUserContext) -> bool:
    """
    Backward-compatible matching:
    - preferred: match stored `agent_user_ref` exactly
    - legacy: older systems may have stored the JWT `sub` directly (without issuer prefix)
    """
    if not stored_ref:
        return False
    if stored_ref == agent_user.agent_user_ref:
        return True
    # Only allow subject match when the stored ref does not appear issuer-prefixed.
    if ":" not in stored_ref and agent_user.subject and stored_ref == agent_user.subject:
        return True
    return False


def _enforce_agent_user_order_access(*, order: Dict[str, Any], context: AgentContext, agent_user: AgentUserContext) -> None:
    if str(order.get("agent_id") or "") != str(context.agent_id):
        raise HTTPException(status_code=403, detail="Not authorized for this order")
    stored = _order_agent_user_ref(order)
    if not stored or not _agent_user_matches_order_ref(stored_ref=stored, agent_user=agent_user):
        raise HTTPException(status_code=403, detail="Not authorized for this order")


async def resolve_buyer_ref_sources(agent_id: str, canonical_buyer_ref: str) -> List[str]:
    """
    Return buyer_refs that should be visible when requesting orders for `canonical_buyer_ref`.
    Direction is one-way: sources -> target.

    Example: guest:xxx merged into user:yyy
    - resolve_buyer_ref_sources(agent_id, "user:yyy") => ["user:yyy", "guest:xxx", ...]
    - resolve_buyer_ref_sources(agent_id, "guest:xxx") => ["guest:xxx"] (no inverse expansion)
    """
    canonical = _normalize_buyer_ref(canonical_buyer_ref)
    if not canonical:
        return []

    try:
        rows = await database.fetch_all(
            """
            SELECT source_ref
            FROM buyer_ref_aliases
            WHERE agent_id = :agent_id AND target_ref = :target_ref
            """,
            {"agent_id": agent_id, "target_ref": canonical},
        )
        sources = [str(r["source_ref"]) for r in (rows or []) if r and r.get("source_ref")]
        # stable de-dup
        out: List[str] = [canonical]
        for s in sources:
            if s and s not in out:
                out.append(s)
        return out
    except Exception:
        # Best-effort: if table isn't available yet, fall back to canonical only.
        return [canonical]


def build_in_params(prefix: str, values: List[str]) -> (str, Dict[str, Any]):
    placeholders: List[str] = []
    params: Dict[str, Any] = {}
    for idx, v in enumerate(values):
        key = f"{prefix}_{idx}"
        placeholders.append(f":{key}")
        params[key] = v
    return ", ".join(placeholders), params

async def verify_merchant_active(merchant_id: str) -> Dict[str, Any]:
    """Verify merchant exists and is not deleted"""
    merchant = await get_merchant_onboarding(merchant_id)
    if not merchant:
        raise HTTPException(status_code=404, detail="Merchant not found")
    
    if merchant.get("status") == "deleted":
        raise HTTPException(
            status_code=403, 
            detail="Merchant account has been deactivated"
        )
    
    return merchant

async def load_cached_product_data_for_merchant(merchant_id: str) -> List[Dict[str, Any]]:
    """
    Load cached StandardProduct dicts for a merchant across all active stores.

    Agent endpoints should be read-only and avoid realtime pulls; use cache rows from `products_cache`.
    """
    stores = await get_merchant_active_stores(merchant_id)
    if not stores:
        return []

    rows: List[Dict[str, Any]] = []
    for store in stores:
        platform = (store or {}).get("platform")
        if not platform:
            continue
        try:
            cached_rows = await get_cached_products(merchant_id, platform, include_expired=False)
            rows.extend(cached_rows or [])
        except Exception as e:
            logger.error(f"Failed to load cached products for merchant={merchant_id} platform={platform}: {e}")
            continue

    products: List[Dict[str, Any]] = []
    for row in rows:
        data = (row or {}).get("product_data")
        if isinstance(data, dict):
            products.append(data)
    return products

def extract_variant_id(product: Dict[str, Any]) -> Optional[str]:
    variants = product.get("variants")
    if isinstance(variants, list) and variants:
        first = variants[0]
        if isinstance(first, dict):
            vid = first.get("variant_id") or first.get("id")
            if vid:
                return str(vid)
    meta = product.get("platform_metadata")
    if isinstance(meta, dict):
        vid = meta.get("variant_id") or meta.get("variantId")
        if vid:
            return str(vid)
    return None

def extract_sku(product: Dict[str, Any]) -> Optional[str]:
    variants = product.get("variants")
    if isinstance(variants, list) and variants and isinstance(variants[0], dict):
        sku = variants[0].get("sku")
        if sku:
            return str(sku)
    sku = product.get("sku")
    return str(sku) if sku else None

def index_variants(products: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """
    Build a lookup from variant_id -> { product, variant } using cached StandardProduct payloads.
    """
    out: Dict[str, Dict[str, Any]] = {}
    for product in products:
        variants = product.get("variants")
        if not isinstance(variants, list):
            continue
        for v in variants:
            if not isinstance(v, dict):
                continue
            vid = v.get("variant_id") or v.get("id")
            if not vid:
                continue
            out[str(vid)] = {"product": product, "variant": v}
    return out

def extract_variant_price(variant: Dict[str, Any]) -> Optional[Decimal]:
    try:
        p = variant.get("price")
        if p is None:
            return None
        return Decimal(str(p))
    except Exception:
        return None


class BuyerRefMergeRequest(BaseModel):
    source_buyer_ref: str
    target_buyer_ref: str


@router.post("/buyers/merge")
async def agent_merge_buyer_refs(
    req: BuyerRefMergeRequest,
    context: AgentContext = Depends(get_agent_context),
):
    """
    Merge (alias) a source buyer_ref into a canonical target buyer_ref (agent-scoped).

    Intended usage:
    - user logs in: merge guest:{uuid} -> user:{public_id}
    - later order lookups with buyer_ref=user:{public_id} include both
    """
    source_ref = _normalize_buyer_ref(getattr(req, "source_buyer_ref", None))
    target_ref = _normalize_buyer_ref(getattr(req, "target_buyer_ref", None))
    if not source_ref or not target_ref:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "INVALID_REQUEST",
                "message": "source_buyer_ref and target_buyer_ref are required",
            },
        )
    if source_ref == target_ref:
        return {
            "status": "success",
            "source_buyer_ref": source_ref,
            "target_buyer_ref": target_ref,
        }

    # If the provided target is itself a source, collapse to its canonical target (best-effort).
    try:
        row = await database.fetch_one(
            """
            SELECT target_ref
            FROM buyer_ref_aliases
            WHERE agent_id = :agent_id AND source_ref = :source_ref
            """,
            {"agent_id": context.agent_id, "source_ref": target_ref},
        )
        if row and row.get("target_ref"):
            target_ref = str(row["target_ref"])
    except Exception:
        pass

    try:
        await database.execute(
            """
            INSERT INTO buyer_ref_aliases (agent_id, source_ref, target_ref, created_at, updated_at)
            VALUES (:agent_id, :source_ref, :target_ref, NOW(), NOW())
            ON CONFLICT (agent_id, source_ref)
            DO UPDATE SET target_ref = EXCLUDED.target_ref, updated_at = NOW()
            """,
            {
                "agent_id": context.agent_id,
                "source_ref": source_ref,
                "target_ref": target_ref,
            },
        )
    except Exception as e:
        logger.error(f"buyer_ref merge failed: {e}")
        raise HTTPException(
            status_code=503,
            detail={
                "error": "UPSTREAM_ERROR",
                "message": "Failed to store buyer_ref merge",
            },
        )

    return {"status": "success", "source_buyer_ref": source_ref, "target_buyer_ref": target_ref}

def variant_in_stock(variant: Dict[str, Any]) -> Optional[bool]:
    try:
        qty = variant.get("inventory_quantity")
        if qty is None:
            return None
        return int(qty) > 0
    except Exception:
        return None


@router.post("/checkout/acp-session")
async def agent_create_acp_checkout_session(
    payload: Dict[str, Any],
    context: AgentContext = Depends(get_agent_context),
    agent_user: Optional[AgentUserContext] = Depends(get_agent_user_context),
    x_buyer_ref: Optional[str] = Header(None, alias="X-Buyer-Ref"),
):
    """
    Create an ACP-hosted checkout session for a merchant.

    This keeps users on a Pivota-controlled checkout surface (ACP) instead of redirecting to merchant storefront URLs.
    """
    merchant_id = str(payload.get("merchant_id") or "").strip()
    if not merchant_id:
        raise HTTPException(status_code=400, detail="merchant_id is required")
    if not context.can_access_merchant(merchant_id):
        raise HTTPException(status_code=403, detail="Not authorized for this merchant")

    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise HTTPException(status_code=400, detail="items[] is required")

    # Determine platform for ACP routing.
    stores = await get_merchant_active_stores(merchant_id)
    platform = None
    if stores and isinstance(stores, list):
        platform = (stores[0] or {}).get("platform")
    platform = str(platform or "shopify").strip().lower()
    if platform not in {"shopify", "wix"}:
        # ACP currently supports shopify/wix; use shopify as proxy fallback.
        platform = "shopify"

    acp_url = str(os.getenv("ACP_URL") or "https://pivota-acp-production.up.railway.app").rstrip("/")
    api_version = str(os.getenv("ACP_API_VERSION") or "2025-09-29").strip()
    service_token = str(os.getenv("ACP_SERVICE_TOKEN") or os.getenv("ACP_API_KEY") or "").strip()
    if not service_token:
        raise HTTPException(status_code=500, detail="Missing ACP_SERVICE_TOKEN")

    # Normalize items into ACP schema: {id, quantity}
    acp_items = []
    for it in items:
        if not isinstance(it, dict):
            continue
        pid = str(it.get("id") or "").strip()
        if not pid:
            continue
        try:
            qty = int(it.get("quantity", 1) or 1)
        except Exception:
            qty = 1
        acp_items.append({"id": pid, "quantity": qty})

    if not acp_items:
        raise HTTPException(status_code=400, detail="items[] must include id")

    request_id = str(uuid.uuid4())
    return_url = payload.get("return_url") or payload.get("returnUrl") or None
    buyer_ref = _normalize_buyer_ref(x_buyer_ref or payload.get("buyer_ref") or payload.get("buyerRef"))
    body = {
        "items": acp_items,
        "buyer": None,
        "fulfillment_address": None,
        "metadata": {
            "request_id": request_id,
            "source": "look_replicator",
            "agent_id": getattr(context, "agent_id", None),
            **({"agent_user_ref": agent_user.agent_user_ref} if agent_user else {}),
            **({"buyer_ref": buyer_ref} if buyer_ref else {}),
            **({"return_url": return_url} if return_url else {}),
        },
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{acp_url}/checkout_sessions",
                headers={
                    "Authorization": f"Bearer {service_token}",
                    "API-Version": api_version,
                    "X-Merchant-Id": merchant_id,
                    "X-Platform": platform,
                    "Content-Type": "application/json",
                },
                json=body,
            )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail={"error": "ACP_UNAVAILABLE", "message": str(exc)})

    if resp.status_code < 200 or resp.status_code >= 300:
        try:
            data = resp.json()
        except Exception:
            data = resp.text
        raise HTTPException(status_code=resp.status_code, detail=data)

    data = resp.json()
    session_id = data.get("id") or data.get("session_id")
    checkout_url = f"{acp_url}/checkout/{session_id}" if session_id else None
    if not checkout_url:
        raise HTTPException(status_code=502, detail={"error": "ACP_INVALID_RESPONSE", "message": "Missing session id"})

    return {"checkout_url": checkout_url, "session_id": session_id}

# ============================================================================
# PCS / Shopify Webhook Debug (metadata-only)
# ============================================================================

@router.get("/debug/shopify/webhooks/events")
async def agent_get_shopify_webhook_events(
    merchant_id: str,
    limit: int = Query(default=20, ge=1, le=200),
    context: AgentContext = Depends(get_agent_context),
):
    """
    Metadata-only view of ingested Shopify webhook events for a merchant.

    Why this exists:
    - Production debugging often has no easy way to obtain a Bearer login token.
    - Agent API keys already have merchant scoping via AgentContext.

    Security:
    - Requires X-API-Key (AgentContext).
    - Enforces merchant access via context.can_access_merchant().
    - Never returns raw payload_json / PII.
    """
    if not context.can_access_merchant(merchant_id):
        raise HTTPException(status_code=403, detail="Not authorized for this merchant")

    try:
        query = """
            SELECT
                id,
                merchant_id,
                shop_domain,
                topic,
                signature_verified,
                occurred_at,
                received_at,
                payload_sha256,
                prev_chain_hash,
                chain_hash,
                idempotency_key
            FROM pcs_shopify_webhook_events
            WHERE merchant_id = :merchant_id
            ORDER BY received_at DESC, id DESC
            LIMIT :limit
        """
        rows = await database.fetch_all(query=query, values={"merchant_id": merchant_id, "limit": limit})

        events = []
        for row in rows:
            events.append(
                {
                    "id": row["id"],
                    "merchant_id": row["merchant_id"],
                    "shop_domain": row["shop_domain"],
                    "topic": row["topic"],
                    "signature_verified": row["signature_verified"],
                    "occurred_at": row["occurred_at"].isoformat() if row["occurred_at"] else None,
                    "received_at": row["received_at"].isoformat() if row["received_at"] else None,
                    "payload_sha256": row["payload_sha256"],
                    "prev_chain_hash": row["prev_chain_hash"],
                    "chain_hash": row["chain_hash"],
                    "idempotency_key": row["idempotency_key"],
                }
            )

        return {"status": "success", "merchant_id": merchant_id, "events": events}
    except Exception as e:
        message = str(e)
        if "pcs_shopify_webhook_events" in message and ("does not exist" in message or "relation" in message):
            return {
                "status": "success",
                "merchant_id": merchant_id,
                "events": [],
                "warning": "pcs_shopify_webhook_events table not found (migration not applied)",
            }
        raise


# ============================================================================
# 产品搜索和浏览
# ============================================================================

@router.get("/products/search")
async def agent_search_products(
    background_tasks: BackgroundTasks,
    merchant_id: Optional[str] = None,  # Now optional for cross-merchant search
    merchant_ids: Optional[List[str]] = Query(None, description="List of merchant IDs to search"),
    search_all_merchants: bool = Query(
        default=False,
        description="Opt-in cross-merchant search (requires explicit intent to avoid irrelevant results)",
    ),
    query: Optional[str] = None,
    category: Optional[str] = None,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    in_stock_only: bool = True,
    limit: int = Query(default=20, le=100),
    offset: int = Query(default=0, ge=0),
    context: AgentContext = Depends(get_agent_context),
):
    """
    智能产品搜索 - Cross-Merchant Support
    
    特点：
    - ✨ NEW: Cross-merchant search (omit merchant_id to search all)
    - 支持自然语言查询
    - 自动过滤库存
    - 价格区间筛选
    - 分页支持
    - 相关度评分
    """
    try:
        overrides = infer_query_overrides(query=query, category=category)
        query = overrides["query"]
        category = overrides["category"]
        query_terms: List[str] = overrides["terms"]

        # Determine which merchants to search
        merchants_to_search = []
        
        if merchant_id:
            # Single merchant search (backward compatible)
            if not context.can_access_merchant(merchant_id):
                raise HTTPException(status_code=403, detail="Not authorized for this merchant")
            merchants_to_search = [merchant_id]
        elif merchant_ids:
            # Multiple specific merchants
            for mid in merchant_ids:
                if not context.can_access_merchant(mid):
                    raise HTTPException(status_code=403, detail=f"Not authorized for merchant {mid}")
            merchants_to_search = merchant_ids
        else:
            # No explicit merchant scope.
            #
            # Prefer searching within the agent's allowed merchants when set,
            # otherwise fall back to cross-merchant search (legacy behavior).
            if isinstance(getattr(context, "allowed_merchants", None), list):
                allowed = [m for m in context.allowed_merchants if m]
                if len(allowed) == 1:
                    merchants_to_search = allowed
                    merchant_id = allowed[0]
                elif allowed:
                    merchants_to_search = allowed

            if not merchants_to_search:
                # Cross-merchant search (legacy behavior). `search_all_merchants`
                # is kept for client-side explicitness but is not required.
                query_merchants = """
                    SELECT merchant_id, business_name FROM merchant_onboarding
                    WHERE status NOT IN ('deleted', 'rejected')
                    AND psp_connected = true
                    LIMIT 100
                """
                merchant_rows = await database.fetch_all(query_merchants)
                merchants_to_search = [row["merchant_id"] for row in merchant_rows]

        # Collect products from all target merchants
        all_products: List[Dict[str, Any]] = []
        
        for mid in merchants_to_search:
            try:
                # Verify merchant is active
                merchant = await verify_merchant_active(mid)

                # Use hybrid query service (cache + realtime) to fetch products
                products, query_source, _ = await get_products_hybrid(
                    merchant_id=mid,
                    limit=limit,
                    agent_id=context.agent_id,
                    background_tasks=background_tasks,
                )

                for sp in products:
                    prod_dict = sp.dict()
                    prod_dict["merchant_id"] = mid
                    prod_dict["merchant_name"] = merchant.get("business_name", "Unknown")
                    prod_dict["query_source"] = query_source
                    all_products.append(prod_dict)
            except Exception as e:
                # Log but continue with other merchants
                logger.warning(f"Failed to get products from {mid}: {e}")
                continue

        ranking_config = get_agent_ranking_config()

        # Apply filters, build features and calculate scores
        ranked_candidates: List[Dict[str, Any]] = []

        for product in all_products:
            # 库存过滤
            if in_stock_only and not product.get("in_stock", True):
                continue
            
            # 价格过滤
            price = float(product.get("price", 0))
            if min_price and price < min_price:
                continue
            if max_price and price > max_price:
                continue
            
            # 类别过滤
            if category:
                product_category = (
                    " ".join(
                        [
                            str(product.get("category") or ""),
                            str(product.get("product_type") or ""),
                            " ".join(product.get("tags") or []),
                        ]
                    )
                ).lower()
                if category.lower() not in product_category:
                    continue
            
            # 搜索查询 + 相关度评分（简单 keyword 匹配，可作为 rel_keyword）
            relevance_score = 1.0
            if query:
                query_lower = query.lower()
                title = product.get("title", "").lower()
                description = product.get("description", "").lower()
                tags = " ".join(product.get("tags") or []).lower()
                product_type = (product.get("product_type") or "").lower()
                haystack = " ".join([title, description, tags, product_type]).strip()
                
                # Calculate relevance
                if query_lower in title:
                    # Exact match in title = high score
                    relevance_score = 1.0 if query_lower == title else 0.9
                elif query_lower in description:
                    relevance_score = 0.7
                elif query_lower in tags or query_lower in product_type:
                    relevance_score = 0.75
                else:
                    # Check for partial word matches
                    query_words = query_terms or query_lower.split()
                    matches = sum(1 for word in query_words if word and word in haystack)
                    if matches > 0:
                        relevance_score = 0.5 + (matches / len(query_words)) * 0.3
                    else:
                        continue  # No match, skip product
                
                product["relevance_score"] = relevance_score
            else:
                product["relevance_score"] = 1.0

            # Build feature vector for ranking
            platform = product.get("platform") or "unknown"
            platform_product_id = str(
                product.get("product_id") or product.get("id") or ""
            )
            if not platform_product_id:
                # Skip products without a stable identifier
                continue

            features = AgentRankingFeatures(
                merchant_id=product.get("merchant_id"),
                platform=platform,
                platform_product_id=platform_product_id,
                rel_semantic=relevance_score,  # Placeholder until true semantic score
                rel_keyword=relevance_score,
                rel_category_match=1.0
                if category
                and category.lower()
                in (product.get("product_type") or "").lower()
                else 0.0,
            )

            # Enrich features with quality / enrichment data
            await hydrate_quality_and_enrichment(features)

            # Apply hard gating (quality / compliance)
            if not passes_agent_gating(features, ranking_config):
                continue

            # Compute final ranking score
            score = compute_agent_ranking_score(features, ranking_config)
            product["ranking_score"] = score
            product["ranking_features"] = serialize_features_for_log(
                features, score
            )

            ranked_candidates.append(product)

        # Sort by ranking score (fallback to relevance when missing)
        ranked_candidates.sort(
            key=lambda p: (p.get("ranking_score") is not None, p.get("ranking_score", p.get("relevance_score", 0))),
            reverse=True,
        )

        # Pagination
        total = len(ranked_candidates)
        paginated_products = ranked_candidates[offset : offset + limit]

        # Log a compact view of ranking features for top N
        try:
            top_sample = [
                {
                    "merchant_id": p.get("merchant_id"),
                    "product_id": str(p.get("product_id") or p.get("id")),
                    "score": p.get("ranking_score"),
                    "rel": p.get("relevance_score"),
                    "cq": (p.get("ranking_features") or {}).get(
                        "quality_content_score"
                    ),
                    "mr": (p.get("ranking_features") or {}).get(
                        "quality_model_readiness"
                    ),
                }
                for p in paginated_products[:10]
            ]
            logger.info(
                "agent_search_ranking",
                extra={
                    "event": "agent_search_ranking",
                    "query": query,
                    "merchant_ids": merchants_to_search,
                    "sample": top_sample,
                },
            )
        except Exception:
            # Logging must never break the handler
            logger.debug("Failed to log agent_search_ranking sample", exc_info=True)

        # Log impression events for cross-merchant search (best-effort).
        try:
            events = []
            for idx, p in enumerate(paginated_products[:50]):
                feats = p.get("ranking_features") or {}
                if not isinstance(feats, dict):
                    feats = {}
                events.append(
                    {
                        "agent_id": getattr(context, "agent_id", None),
                        "session_id": getattr(context, "session_id", None),
                        "event_type": "impression",
                        "endpoint": "/agent/v1/products/search",
                        "query": query,
                        "merchant_id": p.get("merchant_id"),
                        "platform": p.get("platform"),
                        "platform_product_id": str(
                            p.get("product_id") or p.get("id") or ""
                        )
                            or None,
                        "ranking_score": p.get("ranking_score"),
                        "position": idx,
                        "quality_content_score": feats.get(
                            "quality_content_score"
                        ),
                        "quality_model_readiness": feats.get(
                            "quality_model_readiness"
                        ),
                    }
                )
            if events:
                await log_product_events(events)
        except Exception:
            logger.debug(
                "Failed to log agent product events from agent_search_products",
                exc_info=True,
            )
        
        # Record request
        background_tasks.add_task(
            log_agent_request,
            context=context,
            status_code=200,
            merchant_id=merchant_id or "cross_merchant_search"
        )
        
        return {
            "status": "success",
            "products": paginated_products,
            "pagination": {
                "total_count": total,
                "limit": limit,
                "offset": offset,
                "page": (offset // limit) + 1 if limit > 0 else 1,
                "total_pages": (total + limit - 1) // limit if limit > 0 else 1,
                "has_more": offset + limit < total
            },
            "search_context": {
                "merchant_id": merchant_id,
                "merchant_ids": merchant_ids,
                "merchants_searched": len(merchants_to_search),
                "cross_merchant_search": merchant_id is None and not merchant_ids
            },
            "filters_applied": {
                "query": query,
                "category": category,
                "min_price": min_price,
                "max_price": max_price,
                "in_stock_only": in_stock_only
            }
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Agent product search error: {e}")
        await log_agent_request(
            context=context,
            status_code=500,
            merchant_id=merchant_id,
            error_message=str(e)
        )
        raise HTTPException(status_code=500, detail="Search failed")


@router.get("/products/{merchant_id}/{product_id}")
async def agent_get_product(
    merchant_id: str,
    product_id: str,
    background_tasks: BackgroundTasks,
    context: AgentContext = Depends(get_agent_context),
):
    """获取单个产品详情"""
    try:
        # 验证商户访问权限
        if not context.can_access_merchant(merchant_id):
            raise HTTPException(status_code=403, detail="Not authorized for this merchant")
        
        # Special handling for Platform Orders products (SKU-xxx format)
        # These are from Amazon/Temu and don't exist in Shopify/Wix cache
        if product_id.startswith('SKU-'):
            logger.info(f"Returning mock product for Platform Order SKU: {product_id}")
            background_tasks.add_task(
                log_agent_request,
                context=context,
                status_code=200,
                merchant_id=merchant_id
            )
            return {
                "status": "success",
                "product": {
                    "id": product_id,
                    "title": f"Platform Order Product {product_id}",
                    "price": 10.00,  # Default price, actual price is in order data
                    "currency": "USD",
                    "platform": "shopify",  # Mock as shopify for ACP compatibility
                    "stock": 999,
                    "available": True,
                    "variants": [{
                        "id": product_id,
                        "title": "Default",
                        "price": 10.00,
                        "sku": product_id,
                        "available": True
                    }]
                }
            }
        
        # 从缓存获取产品
        products = await load_cached_product_data_for_merchant(merchant_id)
        for product in products:
            pid = product.get("product_id") or product.get("id")
            if str(pid) == str(product_id):
                background_tasks.add_task(
                    log_agent_request,
                    context=context,
                    status_code=200,
                    merchant_id=merchant_id
                )
                return {
                    "status": "success",
                    "product": product
                }
        
        raise HTTPException(status_code=404, detail="Product not found")
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Agent get product error: {e}")
        await log_agent_request(
            context=context,
            status_code=500,
            merchant_id=merchant_id,
            error_message=str(e)
        )
        raise HTTPException(status_code=500, detail="Failed to get product")


# ============================================================================
# 购物车验证和价格计算
# ============================================================================

@router.post("/cart/validate")
async def agent_validate_cart(
    merchant_id: str,
    items: List[Dict[str, Any]],
    background_tasks: BackgroundTasks,
    shipping_country: str = "US",
    context: AgentContext = Depends(get_agent_context),
):
    """
    验证购物车并计算价格
    
    功能：
    - 库存验证
    - 价格更新
    - 运费计算
    - 税费估算
    """
    try:
        # 验证商户访问权限
        if not context.can_access_merchant(merchant_id):
            raise HTTPException(status_code=403, detail="Not authorized for this merchant")
        
        # 获取商户信息并验证状态（检查是否被软删除）
        merchant = await verify_merchant_active(merchant_id)
        
        # 获取产品信息
        product_map = {}
        products = await load_cached_product_data_for_merchant(merchant_id)
        variant_map = index_variants(products)
        for product in products:
            pid = product.get("product_id") or product.get("id")
            if pid is None:
                continue
            product_map[str(pid)] = product
        
        # 验证每个商品
        validated_items = []
        validation_errors = []
        subtotal = Decimal("0")
        
        for item in items:
            input_id = str(item.get("product_id"))
            try:
                quantity = int(item.get("quantity", 1) or 1)
            except Exception:
                quantity = 1

            product = product_map.get(input_id)
            variant = None
            if product is None:
                hit = variant_map.get(input_id)
                if hit:
                    product = hit.get("product")
                    variant = hit.get("variant")

            if product is None:
                validation_errors.append({
                    "product_id": input_id,
                    "error": "Product not found"
                })
                continue

            canonical_product_id = str(product.get("product_id") or product.get("id") or input_id)
            
            # 检查库存
            v_stock = variant_in_stock(variant) if isinstance(variant, dict) else None
            in_stock = bool(product.get("in_stock", True)) if v_stock is None else bool(v_stock)
            if not in_stock:
                validation_errors.append({
                    "product_id": input_id,
                    "error": "Out of stock"
                })
                continue
            
            # 计算价格
            unit_price = None
            if isinstance(variant, dict):
                unit_price = extract_variant_price(variant)
            if unit_price is None:
                try:
                    unit_price = Decimal(str(product.get("price", 0) or 0))
                except Exception:
                    unit_price = Decimal("0")
            item_subtotal = unit_price * quantity
            subtotal += item_subtotal

            variant_id = None
            if isinstance(variant, dict):
                variant_id = variant.get("variant_id") or variant.get("id")
            if not variant_id:
                variant_id = extract_variant_id(product)
            if not variant_id:
                validation_errors.append({
                    "product_id": input_id,
                    "error": "Missing variant_id"
                })
                continue
            
            validated_items.append({
                # Always return canonical IDs suitable for quote/order endpoints.
                "product_id": canonical_product_id,
                "product_title": product.get("title"),
                "variant_id": str(variant_id),
                "sku": extract_sku(product) if not isinstance(variant, dict) else (variant.get("sku") or extract_sku(product)),
                "quantity": quantity,
                "unit_price": str(unit_price),
                "subtotal": str(item_subtotal),
                "in_stock": True
            })
        
        # 计算运费（简单示例）
        shipping_fee = Decimal("10.00") if shipping_country == "US" else Decimal("25.00")
        if subtotal > 100:
            shipping_fee = Decimal("0")  # 免运费
        
        # 计算税费（简单示例）
        tax_rate = Decimal("0.08") if shipping_country == "US" else Decimal("0.15")
        tax = subtotal * tax_rate
        
        # 总计
        total = subtotal + shipping_fee + tax
        
        # 记录请求
        background_tasks.add_task(
            log_agent_request,
            context=context,
            status_code=200,
            merchant_id=merchant_id
        )
        
        return {
            "status": "success",
            "valid": len(validation_errors) == 0,
            "items": validated_items,
            "errors": validation_errors,
            "pricing": {
                "subtotal": str(subtotal),
                "shipping_fee": str(shipping_fee),
                "tax": str(tax),
                "total": str(total),
                "currency": "USD"
            },
            "shipping": {
                "country": shipping_country,
                "free_shipping_threshold": 100,
                "estimated_delivery": "3-5 business days" if shipping_country == "US" else "7-14 business days"
            }
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Cart validation error: {e}")
        await log_agent_request(
            context=context,
            status_code=500,
            merchant_id=merchant_id,
            error_message=str(e)
        )
        raise HTTPException(status_code=500, detail="Cart validation failed")


# ============================================================================
# 订单管理
# ============================================================================

@router.post("/orders/create")
async def agent_create_order(
    order_request: CreateOrderRequest,
    background_tasks: BackgroundTasks,
    context: AgentContext = Depends(get_agent_context),
    agent_user: Optional[AgentUserContext] = Depends(get_agent_user_context),
    x_buyer_ref: Optional[str] = Header(None, alias="X-Buyer-Ref"),
):
    """
    创建订单（代理标准订单创建流程）
    
    自动添加 Agent 追踪信息
    集成 Agent Governance 治理检查
    """
    # STEP 1: Governance validation (before main logic)
    from services.agent_governance import agent_governance
    await agent_governance.validate_request(context.agent_id)

    # MVP measurement scaffolding: record checkout attempt (order creation stage).
    try:
        from mvp.constants import EVENT_CHECKOUT_ATTEMPTED, SURFACE_BACKEND
        from mvp.events import emit_best_effort

        addr = getattr(order_request, "shipping_address", None)
        geo = None
        if addr is not None:
            geo = {
                "country": getattr(addr, "country", None),
                "postal_code": getattr(addr, "postal_code", None),
                "city": getattr(addr, "city", None),
                "state": getattr(addr, "state", None),
            }

        emit_best_effort(
            event_type=EVENT_CHECKOUT_ATTEMPTED,
            payload={
                "stage": "order_create",
                "merchant_id": getattr(order_request, "merchant_id", None),
                "quote_id": getattr(order_request, "quote_id", None),
                "items_count": len(getattr(order_request, "items", None) or []),
                "agent_id": getattr(context, "agent_id", None),
            },
            merchant_id=getattr(order_request, "merchant_id", None),
            geo=geo,
            surface=SURFACE_BACKEND,
            adapter="agent_orders_create",
            risk_tier="unknown",
            idempotency_key=getattr(order_request, "quote_id", None) or getattr(order_request, "agent_session_id", None),
        )
    except Exception:
        pass
    
    start_time = time.time()
    success = False
    
    try:
        # 验证商户访问权限
        if not context.can_access_merchant(order_request.merchant_id):
            raise HTTPException(status_code=403, detail="Not authorized for this merchant")

        # Quote-first enforcement (PCS v0.2-a):
        # - Keep existing global flag behavior (FF_ENABLE_QUOTE_FIRST_ORDER_CREATE).
        # - Add tiered enforcement for L1C/L2+ (FF_ENABLE_QUOTE_FIRST_TIERED_ENFORCEMENT).
        from services.quote_first_enforcement import should_require_quote_for_order_create

        require_quote, require_ctx = await should_require_quote_for_order_create(merchant_id=order_request.merchant_id)
        if require_quote and not order_request.quote_id:
            # Quote-first enforcement: explicit telemetry signal for rollout / debugging.
            try:
                from mvp.constants import EVENT_QUOTE_REQUIRED_BLOCKED, SURFACE_BACKEND
                from mvp.events import emit_best_effort

                addr = getattr(order_request, "shipping_address", None)
                geo = None
                if addr is not None:
                    geo = {
                        "country": getattr(addr, "country", None),
                        "postal_code": getattr(addr, "postal_code", None),
                        "city": getattr(addr, "city", None),
                        "state": getattr(addr, "state", None),
                    }

                emit_best_effort(
                    event_type=EVENT_QUOTE_REQUIRED_BLOCKED,
                    payload={
                        "stage": "order_create",
                        "merchant_id": getattr(order_request, "merchant_id", None),
                        "agent_id": getattr(context, "agent_id", None),
                        "context": require_ctx,
                    },
                    merchant_id=getattr(order_request, "merchant_id", None),
                    geo=geo,
                    surface=SURFACE_BACKEND,
                    adapter="agent_orders_create",
                    risk_tier="unknown",
                    idempotency_key=getattr(order_request, "idempotency_key", None)
                    or getattr(order_request, "agent_session_id", None),
                )
            except Exception:
                pass
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "QUOTE_REQUIRED",
                    "message": "quote_id is required",
                    "context": require_ctx,
                },
            )

        # Quote-first idempotency: default idempotency_key to merchant_id:quote_id when quote_id is present.
        if order_request.quote_id and not order_request.idempotency_key:
            order_request.idempotency_key = f"{order_request.merchant_id}:{order_request.quote_id}"

        # Idempotency (best-effort): if provided and already processed, replay the cached response.
        if order_request.idempotency_key:
            try:
                from mvp.idempotency import PostgresIdempotencyStore

                idem = PostgresIdempotencyStore()
                existing = await idem.get(scope="order_create", key=order_request.idempotency_key)
                if existing and isinstance(existing.value, dict):
                    if (
                        existing.value.get("status") == "success"
                        and existing.value.get("order_id")
                        and (existing.value.get("merchant_id") in (None, order_request.merchant_id))
                    ):
                        return existing.value
            except Exception:
                pass

        # OfferObject + PreFlight (best-effort, additive): compute canonical offer(s) from quote snapshot and
        # attach to order metadata. Enforcement is gated by `MVP_PREFLIGHT_ENFORCE=true`.
        offers = None
        preflight = None
        try:
            if order_request.quote_id:
                from mvp.governance import PolicyInput, governance
                from mvp.offer import build_offers_from_quote, preflight_offers
                from services.quote_service import QuoteService
                from services.shopify_policy_service import get_latest_policy_hashes

                qs = await QuoteService().load_active_quote_or_raise(quote_id=order_request.quote_id)
                if qs.merchant_id != order_request.merchant_id:
                    raise HTTPException(
                        status_code=400,
                        detail={
                            "error": "QUOTE_MERCHANT_MISMATCH",
                            "message": "quote_id does not belong to merchant_id",
                        },
                    )

                snap = qs.snapshot_json or {}
                snap_pricing = snap.get("pricing") or {}

                policies = await get_latest_policy_hashes(order_request.merchant_id)
                policy_hashes_available = bool(policies)

                try:
                    amount_total = float(snap_pricing.get("total") or 0.0)
                except Exception:
                    amount_total = None

                addr = getattr(order_request, "shipping_address", None)
                geo = None
                if addr is not None:
                    geo = {
                        "country": getattr(addr, "country", None),
                        "postal_code": getattr(addr, "postal_code", None),
                        "city": getattr(addr, "city", None),
                        "state": getattr(addr, "state", None),
                    }

                decision = governance.evaluate(
                    PolicyInput(
                        merchant_id=str(order_request.merchant_id),
                        actor_type="agent",
                        actor_ref=str(getattr(context, "agent_id", "")) or None,
                        action="submit_payment",
                        amount=amount_total,
                        currency=str(snap.get("currency") or order_request.currency or "USD"),
                        geo=geo,
                        consent_scopes=[],
                        approval_id=None,
                    )
                )
                hil_required = decision.decision == "require_hil"

                offers = build_offers_from_quote(
                    merchant_id=str(order_request.merchant_id),
                    quote_id=qs.quote_id,
                    expires_at=qs.expires_at,
                    engine=str(snap.get("engine") or qs.engine or "unknown"),
                    engine_ref=str(snap.get("engine_ref") or qs.engine_ref or ""),
                    currency=str(snap.get("currency") or order_request.currency or "USD"),
                    pricing=snap_pricing,
                    line_items=snap.get("line_items") or [],
                    delivery_options=snap.get("delivery_options"),
                    shipping_address=(
                        order_request.shipping_address.model_dump()
                        if hasattr(order_request.shipping_address, "model_dump")
                        else order_request.shipping_address.dict()
                    ),
                )

                preflight = preflight_offers(
                    offers=offers,
                    policy_hashes_available=policy_hashes_available,
                    hil_required=hil_required,
                    hil_reason=",".join(decision.reason_codes) if hil_required else None,
                )

                # Attach to metadata for downstream audit/evidence.
                if not order_request.metadata:
                    order_request.metadata = {}
                mvp_meta = order_request.metadata.get("mvp") if isinstance(order_request.metadata, dict) else None
                if not isinstance(mvp_meta, dict):
                    mvp_meta = {}
                mvp_meta.update(
                    {
                        "schema_version": "0.1",
                        "quote_id": qs.quote_id,
                        "offers": [o.model_dump(mode="json") for o in offers],
                        "preflight": [p.model_dump(mode="json") for p in preflight],
                        "policy_hashes_available": policy_hashes_available,
                        "policy_hashes": [
                            {
                                "policy_type": p.get("policy_type"),
                                "hash_sha256": p.get("hash_sha256"),
                                "fetched_at": str(p.get("fetched_at") or ""),
                            }
                            for p in (policies or [])
                        ],
                        "risk_tier": decision.risk_tier,
                    }
                )
                order_request.metadata["mvp"] = mvp_meta

                enforce_preflight = os.getenv("MVP_PREFLIGHT_ENFORCE", "false").lower() == "true"
                if enforce_preflight and any(r.status == "fail" for r in preflight):
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "error": "PREFLIGHT_FAILED",
                            "message": "Offer preflight failed; checkout blocked.",
                            "preflight": [p.model_dump(mode="json") for p in preflight],
                        },
                    )
        except QuoteError as e:
            # Keep behavior backward-compatible unless quote-first is explicitly required.
            if require_quote or ENABLE_QUOTE_FIRST_ORDER_CREATE:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "error": e.code,
                        "message": e.message,
                        **({"details": e.details} if getattr(e, "details", None) else {}),
                    },
                )
        except HTTPException:
            raise
        except Exception:
            pass
        
        # 添加 Agent 元数据
        if not order_request.metadata:
            order_request.metadata = {}

        # Checkout token: if present, hydrate identity/context fields into order metadata.
        # This prevents footguns where callers forget to pass buyer_ref/job_id while still
        # keeping server-side identity anchored to the minted token.
        if isinstance(order_request.metadata, dict):
            token_payload = getattr(context, "checkout_token_payload", None)
            if isinstance(token_payload, dict):
                for key in ("buyer_ref", "job_id", "market", "locale"):
                    v = token_payload.get(key)
                    if v and not order_request.metadata.get(key):
                        order_request.metadata[key] = v

            # Agent tools end-user attribution (verified via JWKS).
            if agent_user and not order_request.metadata.get("agent_user_ref"):
                order_request.metadata["agent_user_ref"] = agent_user.agent_user_ref
            buyer_ref = _normalize_buyer_ref(x_buyer_ref)
            if buyer_ref and not order_request.metadata.get("buyer_ref"):
                order_request.metadata["buyer_ref"] = buyer_ref
        
        order_request.metadata.update({
            "agent_id": context.agent_id,
            "agent_name": context.agent_name,
            "created_via": "agent_api"
        })
        
        # 设置 agent session ID
        if not order_request.agent_session_id:
            order_request.agent_session_id = f"{context.agent_id}_{int(datetime.utcnow().timestamp())}"
        
        # 调用标准订单创建
        from routes.order_routes import create_new_order
        order_response = await create_new_order(order_request, background_tasks)

        # PCS v0.2-b (best-effort): emit internal fact for reducer replay (no PII).
        try:
            from services.pcs_fact_ingest import append_internal_fact_best_effort

            quote_meta = None
            try:
                if isinstance(order_request.metadata, dict):
                    pricing_quote = (order_request.metadata or {}).get("pricing_quote") or {}
                    if isinstance(pricing_quote, dict):
                        quote_meta = {
                            "quote_id": pricing_quote.get("quote_id"),
                            "quote_hash_sha256": pricing_quote.get("quote_hash_sha256"),
                        }
            except Exception:
                quote_meta = None

            await append_internal_fact_best_effort(
                merchant_id=str(order_request.merchant_id),
                order_id=str(order_response.order_id),
                fact_type="internal.order_created",
                payload={
                    "order_id": str(order_response.order_id),
                    "merchant_id": str(order_request.merchant_id),
                    "quote_id": getattr(order_request, "quote_id", None)
                    or (quote_meta or {}).get("quote_id"),
                    "quote_hash_sha256": (quote_meta or {}).get("quote_hash_sha256"),
                    "currency": str(order_response.currency or order_request.currency or "USD"),
                    "total": float(order_response.total),
                    "psp": getattr(order_response, "psp", None),
                    "idempotency_key": getattr(order_request, "idempotency_key", None),
                },
                idempotency_key=getattr(order_request, "idempotency_key", None) or str(order_response.order_id),
            )
        except Exception:
            pass

        # Quote-first telemetry: quote successfully consumed by an order create.
        try:
            if getattr(order_request, "quote_id", None):
                from mvp.constants import EVENT_QUOTE_CONSUMED, SURFACE_BACKEND
                from mvp.events import emit_best_effort

                addr = getattr(order_request, "shipping_address", None)
                geo = None
                if addr is not None:
                    geo = {
                        "country": getattr(addr, "country", None),
                        "postal_code": getattr(addr, "postal_code", None),
                        "city": getattr(addr, "city", None),
                        "state": getattr(addr, "state", None),
                    }

                emit_best_effort(
                    event_type=EVENT_QUOTE_CONSUMED,
                    payload={
                        "stage": "order_create",
                        "merchant_id": getattr(order_request, "merchant_id", None),
                        "order_id": getattr(order_response, "order_id", None),
                        "quote_id": getattr(order_request, "quote_id", None),
                        "agent_id": getattr(context, "agent_id", None),
                    },
                    merchant_id=getattr(order_request, "merchant_id", None),
                    geo=geo,
                    surface=SURFACE_BACKEND,
                    adapter="agent_orders_create",
                    risk_tier="unknown",
                    idempotency_key=getattr(order_request, "idempotency_key", None)
                    or getattr(order_request, "quote_id", None),
                )
        except Exception:
            pass

        # MVP ledger event (best-effort): canonical order creation timeline entry.
        try:
            from mvp.ledger_events import emit_ledger_event_best_effort

            emit_ledger_event_best_effort(
                merchant_id=str(order_request.merchant_id),
                event_type="order_created",
                order_id=str(order_response.order_id),
                source={"type": "backend"},
                amount={"value": float(order_response.total), "currency": str(order_response.currency)},
                refs={
                    "payment_intent_id": getattr(order_response, "payment_intent_id", None),
                    "shopify_order_id": getattr(order_response, "shopify_order_id", None),
                },
                geo={
                    "country": getattr(order_request.shipping_address, "country", None),
                    "postal_code": getattr(order_request.shipping_address, "postal_code", None),
                    "city": getattr(order_request.shipping_address, "city", None),
                    "state": getattr(order_request.shipping_address, "state", None),
                }
                if getattr(order_request, "shipping_address", None) is not None
                else None,
                surface="backend",
                adapter="agent_orders_create",
                risk_tier=(order_request.metadata.get("mvp", {}).get("risk_tier") if isinstance(order_request.metadata, dict) else "unknown")
                or "unknown",
                idempotency_key=getattr(order_request, "idempotency_key", None) or str(order_response.order_id),
            )
        except Exception:
            pass
        
        # 计算订单总额
        order_amount = float(order_response.total)
        
        # 记录成功请求
        await log_agent_request(
            context=context,
            status_code=200,
            merchant_id=order_request.merchant_id,
            order_id=order_response.order_id,
            order_amount=order_amount
        )
        # 记录购买事件（best-effort，与业务逻辑解耦）
        try:
            # Try to infer basic product identifiers from order items metadata
            from db.agent_product_events import log_product_events

            events = []
            for item in order_request.items or []:
                meta = item.metadata or {}
                platform = meta.get("platform")
                platform_product_id = meta.get("platform_product_id") or meta.get(
                    "product_id"
                )
                if not platform_product_id:
                    continue
                events.append(
                    {
                        "agent_id": getattr(context, "agent_id", None),
                        "session_id": getattr(context, "session_id", None),
                        "event_type": "purchase",
                        "endpoint": "/agent/v1/orders/create",
                        "query": None,
                        "merchant_id": order_request.merchant_id,
                        "platform": platform,
                        "platform_product_id": str(platform_product_id),
                        "ranking_score": None,
                        "position": None,
                        "quality_content_score": None,
                        "quality_model_readiness": None,
                    }
                )
            if events:
                await log_product_events(events)
        except Exception as e:
            logger.debug(f"Failed to log purchase events: {e}", exc_info=True)
        
        # STEP 3: Record governance metrics (success)
        success = True

        # MVP measurement scaffolding: record checkout success for order creation stage.
        try:
            from mvp.constants import EVENT_CHECKOUT_SUCCEEDED, SURFACE_BACKEND
            from mvp.events import emit_best_effort

            addr = getattr(order_request, "shipping_address", None)
            geo = None
            if addr is not None:
                geo = {
                    "country": getattr(addr, "country", None),
                    "postal_code": getattr(addr, "postal_code", None),
                    "city": getattr(addr, "city", None),
                    "state": getattr(addr, "state", None),
                }

            emit_best_effort(
                event_type=EVENT_CHECKOUT_SUCCEEDED,
                payload={
                    "stage": "order_create",
                    "merchant_id": getattr(order_request, "merchant_id", None),
                    "order_id": order_response.order_id,
                    "quote_id": getattr(order_request, "quote_id", None),
                    "currency": order_response.currency,
                    "total": float(order_response.total),
                    "psp": getattr(order_response, "psp", None),
                },
                merchant_id=getattr(order_request, "merchant_id", None),
                geo=geo,
                surface=SURFACE_BACKEND,
                adapter="agent_orders_create",
                risk_tier="unknown",
                idempotency_key=order_response.order_id,
            )
        except Exception:
            pass
        
        # 返回简化的响应给 Agent（统一支付协议）
        # 从标准 OrderResponse 中提取 PSP 信息和统一的 payment_action
        psp_type = order_response.psp or "stripe"
        payment_action_obj = order_response.payment_action
        payment_action: Optional[dict] = None
        if payment_action_obj is not None:
            try:
                # Pydantic model -> dict for JSON response
                payment_action = payment_action_obj.dict()
            except Exception:
                # 防御性：即使序列化失败也不要影响下游
                payment_action = None
        
        # 根据 PSP 类型生成说明文案，兼容旧的 Stripe 提示
        if psp_type == "adyen":
            payment_instructions = (
                "Use payment_action.type='adyen_session' with payment_action.client_secret "
                "(sessionData) to initialize Adyen Drop-in."
            )
        elif payment_action and payment_action.get("type") == "redirect_url":
            payment_instructions = (
                "Redirect the shopper to payment_action.url to complete the payment, then wait "
                "for webhook/order status to update."
            )
        else:
            # 默认保持 Stripe 风格，兼容已有客户端
            payment_instructions = "Use client_secret for Stripe payment confirmation"
        
        # 返回简化的响应给 Agent
        response = {
            "status": "success",
            "order_id": order_response.order_id,
            "merchant_id": order_request.merchant_id,
            "total": str(order_response.total),  # 保留兼容 (deprecated)
            "total_amount": float(order_response.total),  # 新增：标准字段
            "currency": order_response.currency,
            # Phase 0: explicit currency terminology (non-MoR path).
            # Presentment currency is the platform-authoritative quote currency (when quote-first),
            # charge currency is the currency used for PSP charge (currently same as order_response.currency),
            # settlement currency may be configured via employee settlement rules (not returned here yet).
            "presentment_currency": order_response.currency,
            "charge_currency": order_response.currency,
            "settlement_currency": None,
            "payment": {
                "psp": psp_type,
                "client_secret": order_response.client_secret,
                "payment_intent_id": order_response.payment_intent_id,
                "payment_action": payment_action,
                "instructions": payment_instructions,
            },
            "tracking": {
                "agent_session_id": order_request.agent_session_id,
                "created_at": order_response.created_at.isoformat()
            }
        }

        # Attach computed offers + preflight to response when available (additive; safe for existing clients).
        try:
            if offers is not None:
                response["offers"] = [o.model_dump(mode="json") for o in offers]
            if preflight is not None:
                response["preflight"] = [p.model_dump(mode="json") for p in preflight]
        except Exception:
            pass

        # If quote-first snapshot is present in order metadata, return it to client for UI rendering.
        try:
            from db.orders import get_order

            raw = await get_order(order_response.order_id)
            meta = (raw or {}).get("metadata") or {}
            pricing_quote = meta.get("pricing_quote")
            if pricing_quote:
                response["pricing"] = pricing_quote.get("pricing")
                response["promotion_lines"] = pricing_quote.get("promotion_lines") or []
                response["line_items"] = pricing_quote.get("line_items") or []
                response["quote"] = {
                    "quote_id": pricing_quote.get("quote_id"),
                    "expires_at": pricing_quote.get("expires_at"),
                    "engine": pricing_quote.get("engine"),
                    "engine_ref": pricing_quote.get("engine_ref"),
                }
                # Override presentment/charge currency when quote snapshot is available.
                q_currency = (
                    pricing_quote.get("charge_currency")
                    or pricing_quote.get("presentment_currency")
                    or pricing_quote.get("currency")
                )
                if q_currency:
                    response["presentment_currency"] = q_currency
                    response["charge_currency"] = q_currency
                q_settlement = pricing_quote.get("settlement_currency")
                if q_settlement:
                    response["settlement_currency"] = q_settlement
        except Exception:
            # Best-effort: do not break order creation if quote metadata read fails.
            pass

        # Store idempotency record (best-effort).
        if order_request.idempotency_key:
            try:
                from mvp.idempotency import PostgresIdempotencyStore

                idem = PostgresIdempotencyStore()
                await idem.put(scope="order_create", key=order_request.idempotency_key, value=response)
            except Exception:
                pass
        
        return response
        
    except HTTPException as e:
        success = False
        # Quote-first telemetry: capture quote drift diagnostics distribution (no PII).
        try:
            detail = getattr(e, "detail", None)
            if isinstance(detail, dict) and detail.get("error") == "QUOTE_MISMATCH":
                from mvp.constants import EVENT_QUOTE_DRIFT_DETECTED, SURFACE_BACKEND
                from mvp.events import emit_best_effort

                addr = getattr(order_request, "shipping_address", None)
                geo = None
                if addr is not None:
                    geo = {
                        "country": getattr(addr, "country", None),
                        "postal_code": getattr(addr, "postal_code", None),
                        "city": getattr(addr, "city", None),
                        "state": getattr(addr, "state", None),
                    }

                emit_best_effort(
                    event_type=EVENT_QUOTE_DRIFT_DETECTED,
                    payload={
                        "stage": "order_create",
                        "merchant_id": getattr(order_request, "merchant_id", None),
                        "quote_id": getattr(order_request, "quote_id", None),
                        "agent_id": getattr(context, "agent_id", None),
                        "debug_id": detail.get("debug_id"),
                        "drift": detail.get("details") if isinstance(detail.get("details"), dict) else None,
                    },
                    merchant_id=getattr(order_request, "merchant_id", None),
                    geo=geo,
                    surface=SURFACE_BACKEND,
                    adapter="agent_orders_create",
                    risk_tier="unknown",
                    idempotency_key=getattr(order_request, "idempotency_key", None)
                    or getattr(order_request, "quote_id", None)
                    or getattr(order_request, "agent_session_id", None),
                )
        except Exception:
            pass
        try:
            from mvp.constants import EVENT_CHECKOUT_FAILED, SURFACE_BACKEND
            from mvp.events import emit_best_effort

            addr = getattr(order_request, "shipping_address", None)
            geo = None
            if addr is not None:
                geo = {
                    "country": getattr(addr, "country", None),
                    "postal_code": getattr(addr, "postal_code", None),
                    "city": getattr(addr, "city", None),
                    "state": getattr(addr, "state", None),
                }

            emit_best_effort(
                event_type=EVENT_CHECKOUT_FAILED,
                payload={
                    "stage": "order_create",
                    "merchant_id": getattr(order_request, "merchant_id", None),
                    "quote_id": getattr(order_request, "quote_id", None),
                    "error_status": getattr(e, "status_code", None),
                    "error": str(e.detail)[:500],
                },
                merchant_id=getattr(order_request, "merchant_id", None),
                geo=geo,
                surface=SURFACE_BACKEND,
                adapter="agent_orders_create",
                risk_tier="unknown",
                idempotency_key=getattr(order_request, "quote_id", None) or getattr(order_request, "agent_session_id", None),
            )
        except Exception:
            pass
        await log_agent_request(
            context=context,
            status_code=e.status_code,
            merchant_id=order_request.merchant_id,
            error_message=e.detail
        )
        raise
    except Exception as e:
        success = False
        logger.error(f"Agent order creation error: {e}")
        try:
            from mvp.constants import EVENT_CHECKOUT_FAILED, SURFACE_BACKEND
            from mvp.events import emit_best_effort

            addr = getattr(order_request, "shipping_address", None)
            geo = None
            if addr is not None:
                geo = {
                    "country": getattr(addr, "country", None),
                    "postal_code": getattr(addr, "postal_code", None),
                    "city": getattr(addr, "city", None),
                    "state": getattr(addr, "state", None),
                }

            emit_best_effort(
                event_type=EVENT_CHECKOUT_FAILED,
                payload={
                    "stage": "order_create",
                    "merchant_id": getattr(order_request, "merchant_id", None),
                    "quote_id": getattr(order_request, "quote_id", None),
                    "error_status": 500,
                    "error": str(e)[:500],
                },
                merchant_id=getattr(order_request, "merchant_id", None),
                geo=geo,
                surface=SURFACE_BACKEND,
                adapter="agent_orders_create",
                risk_tier="unknown",
                idempotency_key=getattr(order_request, "quote_id", None) or getattr(order_request, "agent_session_id", None),
            )
        except Exception:
            pass
        await log_agent_request(
            context=context,
            status_code=500,
            merchant_id=order_request.merchant_id,
            error_message=str(e)
        )
        raise HTTPException(status_code=500, detail=f"Order creation internal error: {str(e)}")
    finally:
        # STEP 3: Record governance metrics (always executed)
        latency_ms = int((time.time() - start_time) * 1000)
        await agent_governance.record_response(
            agent_id=context.agent_id,
            latency_ms=latency_ms,
            success=success
        )


@router.post("/orders/{order_id}/confirm-payment")
async def agent_confirm_payment(
    order_id: str,
    background_tasks: BackgroundTasks,
    context: AgentContext = Depends(get_agent_context),
):
    """确认支付并触发 Shopify 订单创建（Agent 调用）"""
    try:
        from routes.order_routes import mark_order_paid, create_shopify_order, log_order_event, get_order
        from routes.merchant_onboarding_routes import get_merchant_onboarding
        from services.pcs_evidence_pack_service import create_order_snapshot_evidence_pack
        
        # 获取订单
        order = await get_order(order_id)
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")
        
        # 验证访问权限
        if not context.can_access_merchant(order["merchant_id"]):
            raise HTTPException(status_code=403, detail="Not authorized for this order")
        
        # 检查是否已支付
        if order.get("payment_status") == "paid":
            return {"status": "success", "message": "Order already paid"}
        
        # 标记订单已支付
        await mark_order_paid(order_id)

        # PCS: freeze order snapshot evidence (best-effort; does not block confirm)
        try:
            await create_order_snapshot_evidence_pack(order_id, triggered_by="agent_confirm_payment")
        except Exception as e:
            logger.warning(f"PCS evidence snapshot failed for {order_id}: {e}")
        
        # [Phase 6.2] 自动触发 commission 计算
        if order.get("agent_id"):
            async def trigger_commission():
                try:
                    from services.order_commission_service import OrderCommissionService
                    from db.database import database
                    service = OrderCommissionService(database)
                    await service.calculate_commission_for_order(order_id)
                    logger.info(f"✅ Commission auto-calculated for order {order_id}")
                except Exception as e:
                    logger.error(f"Commission auto-calculation failed for {order_id}: {e}")
            
            background_tasks.add_task(trigger_commission)
        
        # 记录支付成功事件
        await log_order_event(
            event_type="payment_succeeded",
            order_id=order_id,
            merchant_id=order["merchant_id"],
            metadata={
                "payment_intent_id": order.get("payment_intent_id"),
                "amount": float(order["total"]),
                "currency": order["currency"],
                "confirmed_by": "agent"
            }
        )
        
        # 获取商户信息用于 Shopify 同步
        merchant = await get_merchant_onboarding(order["merchant_id"])
        
        # 后台任务：创建 Shopify 订单（直接调用，避免嵌套异步）
        from services.merchant_store_service import get_primary_store
        from routes.order_routes import create_shopify_order
        
        async def create_shopify_order_task():
            """创建 Shopify 订单通知商户发货"""
            try:
                logger.info(f"[Background] Starting Shopify order creation for {order_id}")
                
                # 获取主店铺信息以决定是否同步到 Shopify
                store_info = await get_primary_store(order["merchant_id"])
                logger.info(f"[Background] Store info: platform={store_info.get('platform') if store_info else 'None'}, has_token={bool(store_info.get('api_key')) if store_info else False}")
                
                if not store_info:
                    logger.warning(f"[Background] No store info found for merchant {order['merchant_id']}")
                    return
                    
                if store_info.get("platform") != "shopify":
                    logger.info(f"[Background] Merchant not connected to Shopify, skipping order sync")
                    return
                
                logger.info(f"[Background] Calling create_shopify_order for {order_id}")
                success = await create_shopify_order(order_id)
                
                if success:
                    logger.info(f"[Background] ✅ Shopify order created successfully for {order_id}")
                else:
                    logger.error(f"[Background] ❌ Failed to create Shopify order for {order_id}")
                    
            except Exception as e:
                logger.error(f"[Background] Error in Shopify order creation task: {type(e).__name__}: {e}", exc_info=True)
        
        background_tasks.add_task(create_shopify_order_task)
        
        # 记录请求
        background_tasks.add_task(
            log_agent_request,
            context=context,
            status_code=200,
            merchant_id=order["merchant_id"],
            order_id=order_id
        )
        
        return {
            "status": "success",
            "message": "Payment confirmed, Shopify order creation initiated",
            "order_id": order_id,
            "payment_intent_id": order.get("payment_intent_id"),
            "shopify_sync": "initiated" if merchant and True else "not_configured"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Agent payment confirmation error: {e}")
        raise HTTPException(status_code=500, detail=f"Payment confirmation failed: {str(e)}")


@router.get("/orders/{order_id}")
async def agent_get_order(
    order_id: str,
    background_tasks: BackgroundTasks,
    buyer_ref: Optional[str] = None,
    context: AgentContext = Depends(get_agent_context),
    agent_user: Optional[AgentUserContext] = Depends(get_agent_user_context),
):
    """获取订单状态"""
    try:
        # 获取订单
        order = await get_order(order_id)
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        # Always enforce agent ownership for Agent API calls.
        if str(order.get("agent_id") or "") != str(context.agent_id):
            raise HTTPException(status_code=403, detail="Not authorized for this order")

        # If the order is attributed to a verified agent-user, require that identity.
        stored_agent_user_ref = _order_agent_user_ref(order)
        if stored_agent_user_ref:
            if not agent_user or not _agent_user_matches_order_ref(stored_ref=stored_agent_user_ref, agent_user=agent_user):
                raise HTTPException(status_code=403, detail="Not authorized for this order")
        # Legacy compatibility: allow access by buyer_ref even when X-Agent-User-JWT is present,
        # as long as the order itself is not agent-user-attributed.
        elif buyer_ref:
            stored = (order.get("metadata") or {}).get("buyer_ref")
            allowed_refs = await resolve_buyer_ref_sources(context.agent_id, str(buyer_ref))
            if str(stored or "") not in allowed_refs:
                raise HTTPException(status_code=403, detail="Not authorized for this order")
        else:
            # Legacy access: validate merchant access.
            if not context.can_access_merchant(order["merchant_id"]):
                raise HTTPException(status_code=403, detail="Not authorized for this order")
        
        # 记录请求
        background_tasks.add_task(
            log_agent_request,
            context=context,
            status_code=200,
            merchant_id=order["merchant_id"],
            order_id=order_id
        )
        
        # 返回订单信息（包含必要字段用于 Shopify 同步）
        return {
            "status": "success",
            "order": {
                "order_id": order["order_id"],
                "merchant_id": order["merchant_id"],
                "customer_email": order["customer_email"],
                "items": order.get("items", []),
                "shipping_address": order.get("shipping_address"),
                "status": order["status"],
                "payment_status": order["payment_status"],
                "fulfillment_status": order.get("fulfillment_status"),
                "total": str(order["total"]),
                "total_refunded": str(order.get("total_refunded") or 0),
                "currency": order["currency"],
                "shopify_order_id": order.get("shopify_order_id"),
                "tracking_number": order.get("tracking_number"),
                "created_at": order["created_at"],
                "updated_at": order.get("updated_at"),
                "confirmed_at": order.get("confirmed_at")
            }
        }
        
    except HTTPException as e:
        await log_agent_request(
            context=context,
            status_code=e.status_code,
            error_message=e.detail
        )
        raise
    except Exception as e:
        logger.error(f"Agent get order error: {e}")
        await log_agent_request(
            context=context,
            status_code=500,
            error_message=str(e)
        )
        raise HTTPException(status_code=500, detail="Failed to get order")


class IssueOrderReviewInvitationsRequest(BaseModel):
    platform_product_id: Optional[str] = None
    variant_id: Optional[str] = None
    ttl_seconds: int = Field(24 * 3600, ge=300, le=7 * 24 * 3600)


@router.post("/orders/{order_id}/reviews/invitations")
async def agent_issue_review_invitations_from_order(
    order_id: str,
    body: IssueOrderReviewInvitationsRequest,
    response: Response,
    buyer_ref: Optional[str] = None,
    context: AgentContext = Depends(get_agent_context),
    agent_user: Optional[AgentUserContext] = Depends(get_agent_user_context),
):
    """
    Mint browser-safe invitation_token(s) for a paid order.

    This endpoint is intended for checkout/order detail UIs to offer "Write a review"
    without exposing internal issuer keys to browsers. Tokens are single-use via the
    exchange endpoint and can be minted per line-item (product/variant).
    """
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"

    order = await get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if str(order.get("agent_id") or "") != str(context.agent_id):
        raise HTTPException(status_code=403, detail="Not authorized for this order")

    stored_agent_user_ref = _order_agent_user_ref(order)
    if stored_agent_user_ref:
        if not agent_user or not _agent_user_matches_order_ref(stored_ref=stored_agent_user_ref, agent_user=agent_user):
            raise HTTPException(status_code=403, detail="Not authorized for this order")
    elif buyer_ref:
        stored = (order.get("metadata") or {}).get("buyer_ref")
        allowed_refs = await resolve_buyer_ref_sources(context.agent_id, str(buyer_ref))
        if str(stored or "") not in allowed_refs:
            raise HTTPException(status_code=403, detail="Not authorized for this order")

    return await mint_invitations_from_paid_order(
        merchant_id=str(order.get("merchant_id") or "").strip(),
        order=order,
        ttl_seconds=int(body.ttl_seconds),
        platform_product_id=body.platform_product_id,
        variant_id=body.variant_id,
        verification="verified_buyer",
    )


@router.get("/orders")
async def agent_list_orders(
    background_tasks: BackgroundTasks,
    merchant_id: Optional[str] = None,
    buyer_ref: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = Query(default=20, le=100),
    offset: int = Query(default=0, ge=0),
    context: AgentContext = Depends(get_agent_context),
    agent_user: Optional[AgentUserContext] = Depends(get_agent_user_context),
):
    """
    列出 Agent 创建的订单
    
    可以按商户或状态过滤
    """
    try:
        # 如果指定了商户，验证访问权限
        if merchant_id and not context.can_access_merchant(merchant_id):
            raise HTTPException(status_code=403, detail="Not authorized for this merchant")
        
        # 构建查询 - use agent_id column directly
        query = f"""
            SELECT * FROM orders 
            WHERE agent_id = :agent_id
        """
        params = {"agent_id": context.agent_id}
        
        if merchant_id:
            query += " AND merchant_id = :merchant_id"
            params["merchant_id"] = merchant_id
        
        if status:
            query += " AND status = :status"
            params["status"] = status

        buyer_filter_sql = None
        buyer_filter_params: Dict[str, Any] = {}
        if buyer_ref:
            allowed_refs = await resolve_buyer_ref_sources(context.agent_id, str(buyer_ref))
            placeholders, extra = build_in_params("buyer_ref", allowed_refs)
            if placeholders:
                buyer_filter_sql = f"(metadata ->> 'buyer_ref') IN ({placeholders})"
                buyer_filter_params.update(extra)

        agent_user_ref_expr = "COALESCE(metadata ->> 'agent_user_ref', metadata ->> 'agentUserRef')"

        # Compatibility: when both are present, union agent_user_ref + buyer_ref legacy orders.
        agent_user_filter_sql = None
        if agent_user:
            agent_user_filter_sql = f"({agent_user_ref_expr}) = :agent_user_ref"
            params["agent_user_ref"] = agent_user.agent_user_ref
            if agent_user.subject and ":" not in (agent_user.subject or ""):
                agent_user_filter_sql = f"({agent_user_filter_sql} OR ({agent_user_ref_expr}) = :agent_user_subject)"
                params["agent_user_subject"] = agent_user.subject

        if agent_user_filter_sql and buyer_filter_sql:
            # Keep list/detail consistent: when an order has agent_user_ref, it must match the
            # verified agent-user identity; buyer_ref fallback should only include legacy orders
            # that have no agent_user_ref attributed.
            query += (
                " AND ("
                f"{agent_user_filter_sql}"
                " OR ("
                f"{buyer_filter_sql}"
                f" AND ({agent_user_ref_expr}) IS NULL"
                ")"
                ")"
            )
            params.update(buyer_filter_params)
        elif agent_user_filter_sql:
            query += f" AND {agent_user_filter_sql}"
        elif buyer_filter_sql:
            # buyer_ref is a legacy/compat identifier; do not expose agent-user-attributed orders
            # unless a verified X-Agent-User-JWT is present (handled above).
            query += f" AND ({buyer_filter_sql} AND ({agent_user_ref_expr}) IS NULL)"
            params.update(buyer_filter_params)
        else:
            pass
        
        query += " ORDER BY created_at DESC LIMIT :limit OFFSET :offset"
        params["limit"] = limit
        params["offset"] = offset
        
        # 执行查询
        from db.database import database
        orders = await database.fetch_all(query, params)
        
        # 记录请求
        background_tasks.add_task(
            log_agent_request,
            context=context,
            status_code=200,
            merchant_id=merchant_id
        )
        
        return {
            "status": "success",
            "total": len(orders),
            "orders": [
                {
                    "order_id": order["order_id"],
                    "merchant_id": order["merchant_id"],
                    "status": order["status"],
                    "payment_status": order["payment_status"],
                    "total": str(order["total"]),
                    "created_at": order["created_at"]
                }
                for order in orders
            ]
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Agent list orders error: {e}")
        await log_agent_request(
            context=context,
            status_code=500,
            error_message=str(e)
        )
        raise HTTPException(status_code=500, detail="Failed to list orders")

@router.get("/orders/events")
async def agent_list_order_events(
    background_tasks: BackgroundTasks,
    after_id: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    wait_ms: int = Query(default=0, ge=0, le=25_000),
    merchant_id: Optional[str] = None,
    buyer_ref: Optional[str] = None,
    context: AgentContext = Depends(get_agent_context),
    agent_user: Optional[AgentUserContext] = Depends(get_agent_user_context),
):
    """
    Incremental order event feed for Agent tools UIs.

    Filters:
    - Prefer verified agent-user scoping when X-Agent-User-JWT is present.
    - Fall back to buyer_ref for legacy anonymous sessions.
    - Otherwise returns all events for the agent (agent-scoped).
    """
    import asyncio

    try:
        # If specified, enforce merchant access (agent-level control).
        if merchant_id and not context.can_access_merchant(merchant_id):
            raise HTTPException(status_code=403, detail="Not authorized for this merchant")

        params: Dict[str, Any] = {
            "agent_id": context.agent_id,
            "after_id": int(after_id or 0),
            "limit": int(limit),
        }

        query = """
            SELECT
                e.id,
                e.event_type,
                e.merchant_id,
                e.order_id,
                e.status,
                e.total_amount,
                e.currency,
                e.payment_method,
                e.error_message,
                e.created_at
            FROM order_events e
            JOIN orders o ON o.order_id = e.order_id
            WHERE o.agent_id = :agent_id
              AND o.is_deleted = FALSE
              AND e.id > :after_id
        """

        if merchant_id:
            query += " AND e.merchant_id = :merchant_id"
            params["merchant_id"] = merchant_id

        buyer_filter_sql = None
        buyer_filter_params: Dict[str, Any] = {}
        if buyer_ref:
            allowed_refs = await resolve_buyer_ref_sources(context.agent_id, str(buyer_ref))
            placeholders, extra = build_in_params("buyer_ref", allowed_refs)
            if placeholders:
                buyer_filter_sql = f"(o.metadata ->> 'buyer_ref') IN ({placeholders})"
                buyer_filter_params.update(extra)

        agent_user_ref_expr = "COALESCE(o.metadata ->> 'agent_user_ref', o.metadata ->> 'agentUserRef')"

        # Compatibility: when both are present, union agent_user_ref + buyer_ref legacy events.
        agent_user_filter_sql = None
        if agent_user:
            agent_user_filter_sql = f"({agent_user_ref_expr}) = :agent_user_ref"
            params["agent_user_ref"] = agent_user.agent_user_ref
            if agent_user.subject and ":" not in (agent_user.subject or ""):
                agent_user_filter_sql = f"({agent_user_filter_sql} OR ({agent_user_ref_expr}) = :agent_user_subject)"
                params["agent_user_subject"] = agent_user.subject

        if agent_user_filter_sql and buyer_filter_sql:
            query += (
                " AND ("
                f"{agent_user_filter_sql}"
                " OR ("
                f"{buyer_filter_sql}"
                f" AND ({agent_user_ref_expr}) IS NULL"
                ")"
                ")"
            )
            params.update(buyer_filter_params)
        elif agent_user_filter_sql:
            query += f" AND {agent_user_filter_sql}"
        elif buyer_filter_sql:
            query += f" AND ({buyer_filter_sql} AND ({agent_user_ref_expr}) IS NULL)"
            params.update(buyer_filter_params)
        else:
            pass

        query += " ORDER BY e.id ASC LIMIT :limit"

        deadline = None
        if wait_ms and wait_ms > 0:
            deadline = asyncio.get_event_loop().time() + (wait_ms / 1000.0)

        rows = []
        while True:
            rows = await database.fetch_all(query, params)
            if rows:
                break
            if deadline is None:
                break
            if asyncio.get_event_loop().time() >= deadline:
                break
            await asyncio.sleep(0.25)

        events = [dict(r) for r in (rows or [])]
        last_id = int(events[-1]["id"]) if events else int(after_id or 0)

        background_tasks.add_task(
            log_agent_request,
            context=context,
            status_code=200,
            merchant_id=merchant_id,
        )

        return {"status": "success", "events": events, "last_id": last_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Agent list order events error: {e}")
        await log_agent_request(context=context, status_code=500, error_message=str(e))
        raise HTTPException(status_code=500, detail="Failed to list order events")


# ----------------------------------------------------------------------------
# Order actions for Agents (refund, cancel, track)
# ----------------------------------------------------------------------------

@router.post("/orders/{order_id}/refund")
async def agent_refund_order(
    order_id: str,
    background_tasks: BackgroundTasks,
    buyer_ref: Optional[str] = None,
    context: AgentContext = Depends(get_agent_context),
    agent_user: Optional[AgentUserContext] = Depends(get_agent_user_context),
):
    """Proxy refund to admin refund API, but enforce agent ownership."""
    try:
        order = await get_order(order_id)
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        # Always enforce agent ownership for Agent API calls.
        if str(order.get("agent_id") or "") != str(context.agent_id):
            raise HTTPException(status_code=403, detail="Not authorized for this order")

        stored_agent_user_ref = _order_agent_user_ref(order)
        if stored_agent_user_ref:
            if not agent_user or not _agent_user_matches_order_ref(stored_ref=stored_agent_user_ref, agent_user=agent_user):
                raise HTTPException(status_code=403, detail="Not authorized for this order")
        elif buyer_ref:
            stored = (order.get("metadata") or {}).get("buyer_ref")
            allowed_refs = await resolve_buyer_ref_sources(context.agent_id, str(buyer_ref))
            if str(stored or "") not in allowed_refs:
                raise HTTPException(status_code=403, detail="Not authorized for this order")

        # Build refund request (full refund)
        class _Req(BaseModel):
            order_id: str
            amount: Optional[float] = None
            reason: Optional[str] = None
            restore_inventory: bool = True

        req = _Req(order_id=order_id, amount=None, reason="Agent requested refund", restore_inventory=True)
        result = await process_refund(order_id, req, background_tasks, current_user={"role": "admin"})
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Agent refund error: {e}")
        raise HTTPException(status_code=500, detail="Failed to refund order")


@router.post("/orders/{order_id}/cancel")
async def agent_cancel_order(
    order_id: str,
    buyer_ref: Optional[str] = None,
    context: AgentContext = Depends(get_agent_context),
    agent_user: Optional[AgentUserContext] = Depends(get_agent_user_context),
):
    """Cancel an order owned by the agent (defensive - no optional columns)."""
    try:
        order = await get_order(order_id)
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        # Always enforce agent ownership for Agent API calls.
        if str(order.get("agent_id") or "") != str(context.agent_id):
            raise HTTPException(status_code=403, detail="Not authorized for this order")

        stored_agent_user_ref = _order_agent_user_ref(order)
        if stored_agent_user_ref:
            if not agent_user or not _agent_user_matches_order_ref(stored_ref=stored_agent_user_ref, agent_user=agent_user):
                raise HTTPException(status_code=403, detail="Not authorized for this order")
        elif buyer_ref:
            stored = (order.get("metadata") or {}).get("buyer_ref")
            allowed_refs = await resolve_buyer_ref_sources(context.agent_id, str(buyer_ref))
            if str(stored or "") not in allowed_refs:
                raise HTTPException(status_code=403, detail="Not authorized for this order")

        # Block cancel if clearly paid/succeeded
        paid_status = str(order.get("payment_status") or "").lower()
        if paid_status in ("paid", "succeeded", "completed"):
            raise HTTPException(status_code=400, detail="Cannot cancel a paid/completed order. Please refund instead.")

        # If already cancelled, treat as idempotent success
        current_status = str(order.get("status") or "")
        if current_status.lower() == "cancelled":
            return {"status": "success", "order_id": order_id, "message": "Order already cancelled"}

        # Defensive update: only set status to avoid missing columns like cancelled_at
        from db.database import database
        try:
            await database.execute(
                """
                UPDATE orders
                SET status = 'cancelled'
                WHERE order_id = :order_id
                """,
                {"order_id": order_id}
            )
        except Exception as e:
            logger.error(f"Cancel update error: {e}")
            raise HTTPException(status_code=500, detail="Cancel update failed")

        # Some DB drivers return rowcount via different means; fetch again to verify
        after = await get_order(order_id)
        if not after:
            raise HTTPException(status_code=500, detail="Cancel verification failed")
        if str(after.get("status") or "").lower() != "cancelled":
            raise HTTPException(status_code=500, detail="Failed to cancel order")

        return {"status": "success", "order_id": order_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Agent cancel error: {e}")
        raise HTTPException(status_code=500, detail="Failed to cancel order")


@router.get("/orders/{order_id}/track")
async def agent_track_order(
    order_id: str,
    context: AgentContext = Depends(get_agent_context)
):
    """Return fulfillment tracking info for the order if owned by agent."""
    try:
        order = await get_order(order_id)
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")
        if order.get("agent_id") != context.agent_id:
            raise HTTPException(status_code=403, detail="Not authorized for this order")
        # `track_order_fulfillment` is also an HTTP handler that expects FastAPI-injected
        # `BackgroundTasks` + `context`. When called from here, pass arguments explicitly.
        tracking = await track_order_fulfillment(
            order_id=order_id,
            background_tasks=BackgroundTasks(),
            context=context,
        )
        return tracking
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Agent track error: {e}")
        raise HTTPException(status_code=500, detail="Failed to get tracking info")


# ============================================================================
# Agent 分析
# ============================================================================

@router.get("/analytics/summary")
async def agent_get_analytics(
    days: int = Query(default=30, le=365),
    context: AgentContext = Depends(get_agent_context)
):
    """
    获取 Agent 自己的分析数据
    
    包括：
    - 请求统计
    - 订单转化率
    - GMV
    - 热门商户
    """
    try:
        from datetime import timedelta
        from db.agents import get_agent_analytics
        
        start_date = datetime.utcnow() - timedelta(days=days)
        analytics = await get_agent_analytics(
            context.agent_id,
            start_date=start_date
        )
        
        return {
            "status": "success",
            "agent_id": context.agent_id,
            "agent_name": context.agent_name,
            "analytics": analytics
        }
        
    except Exception as e:
        logger.error(f"Agent analytics error: {e}")
        raise HTTPException(status_code=500, detail="Failed to get analytics")


# ============================================================================
# 佣金查询
# ============================================================================

@router.get("/commissions")
async def get_agent_commissions(
    limit: int = Query(50, ge=1, le=200, description="返回数量"),
    offset: int = Query(0, ge=0, description="偏移量"),
    status: Optional[str] = Query(None, description="状态过滤: pending, paid"),
    context: AgentContext = Depends(get_agent_context)
):
    """
    获取 Agent 的佣金列表
    
    返回所有与此 Agent 相关的订单佣金
    """
    try:
        # 构建查询条件
        conditions = ["agent_id = :agent_id"]
        params = {
            "agent_id": context.agent_id,
            "limit": limit,
            "offset": offset
        }
        
        if status:
            conditions.append("status = :status")
            params["status"] = status
        
        where_clause = " AND ".join(conditions)
        
        # 获取佣金总数
        count_query = f"""
            SELECT COUNT(*) as total
            FROM commissions
            WHERE {where_clause}
        """
        count_result = await database.fetch_one(count_query, params)
        total = count_result["total"] if count_result else 0
        
        # 获取佣金列表
        commissions_query = f"""
            SELECT 
                commission_id,
                order_id,
                merchant_id,
                amount,
                rate,
                status,
                matched,
                created_at,
                updated_at
            FROM commissions
            WHERE {where_clause}
            ORDER BY created_at DESC
            LIMIT :limit OFFSET :offset
        """
        
        rows = await database.fetch_all(commissions_query, params)
        commissions = [dict(row) for row in rows]
        
        # 格式化日期
        for comm in commissions:
            if comm.get('created_at'):
                comm['created_at'] = comm['created_at'].isoformat() if hasattr(comm['created_at'], 'isoformat') else str(comm['created_at'])
            if comm.get('updated_at'):
                comm['updated_at'] = comm['updated_at'].isoformat() if hasattr(comm['updated_at'], 'isoformat') else str(comm['updated_at'])
        
        # 计算摘要
        summary_query = """
            SELECT 
                COUNT(*) as total_count,
                COALESCE(SUM(CASE WHEN status = 'pending' THEN amount ELSE 0 END), 0) as pending_amount,
                COALESCE(SUM(CASE WHEN status = 'paid' THEN amount ELSE 0 END), 0) as paid_amount,
                COALESCE(SUM(amount), 0) as total_amount
            FROM commissions
            WHERE agent_id = :agent_id
        """
        summary_result = await database.fetch_one(summary_query, {"agent_id": context.agent_id})
        
        return {
            "status": "success",
            "commissions": commissions,
            "summary": {
                "total_count": summary_result["total_count"] if summary_result else 0,
                "pending_amount": float(summary_result["pending_amount"]) if summary_result else 0.0,
                "paid_amount": float(summary_result["paid_amount"]) if summary_result else 0.0,
                "total_amount": float(summary_result["total_amount"]) if summary_result else 0.0
            },
            "pagination": {
                "total": total,
                "limit": limit,
                "offset": offset,
                "has_more": (offset + limit) < total
            }
        }
        
    except Exception as e:
        logger.error(f"Get commissions error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get commissions: {str(e)}")
