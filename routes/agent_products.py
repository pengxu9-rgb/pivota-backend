"""
Agent Product Browsing API
Allows agents to view merchant products via hybrid query (cache or realtime)
"""

from services.product_query_service import get_products_hybrid, log_query_source
from services.agent_product_service import get_agent_product_view
import services.merchant_store_service as merchant_store_service
from services.shopify_access_token_service import resolve_shopify_admin_access_token
from services.agent_ranking_service import (
    AgentRankingFeatures,
    get_agent_ranking_config,
    hydrate_quality_and_enrichment,
    passes_agent_gating,
    compute_agent_ranking_score,
    serialize_features_for_log,
)
from services.product_exposure_service import (
    AGENT_PUSH_STATUS_EXCLUDED,
    build_agent_push_projection_from_standard_product,
)
from services.payment_offer_evidence_service import (
    enrich_product_cards_with_payment_offers,
    enrich_product_detail_with_payment_offers,
)
from services.store_discount_evidence_service import (
    enrich_product_cards_with_store_discounts,
    enrich_product_detail_with_store_discounts,
)
from db.agent_ranking_log import log_ranking_batch
from db.agent_product_events import log_product_events
from fastapi import APIRouter, Depends, HTTPException, Query
from typing import List, Optional, Dict, Any
import httpx
import logging
import time
import json
import os
from datetime import datetime

from routes.agent_api import get_agent_context, AgentContext, log_agent_request
from routes.merchant_onboarding_routes import get_merchant_onboarding
from fastapi import BackgroundTasks
from db.database import database
from db.products import get_product_cache_row
from models.catalog import PivotPaymentContext
from models.standard_product import StandardProduct
from db.product_quality import product_quality_snapshot
from config.settings import settings
from utils.redis_client import get_redis_client

router = APIRouter(prefix="/agent/v1/products", tags=["Agent Products"])
logger = logging.getLogger(__name__)

PRODUCT_DETAILS_CACHE_TTL_SECONDS = int(os.getenv("PRODUCT_DETAILS_CACHE_TTL_SECONDS", "3600"))


def _product_details_cache_key(*, merchant_id: str, platform: str, platform_product_id: str) -> str:
    return f"agent_products:details:{platform}:{merchant_id}:{platform_product_id}"


def _variant_to_product_cache_key(*, merchant_id: str, platform: str, variant_id: str) -> str:
    return f"agent_products:variant_to_product:{platform}:{merchant_id}:{variant_id}"


async def _redis_get_json(key: str) -> Optional[Dict[str, Any]]:
    client = get_redis_client()
    if client is None:
        return None
    try:
        raw = await client.get(key)
        if not raw:
            return None
        return json.loads(raw)
    except Exception:
        return None


async def _redis_set_json(key: str, value: Dict[str, Any], ttl_seconds: int) -> None:
    client = get_redis_client()
    if client is None:
        return None
    try:
        await client.set(key, json.dumps(value, separators=(",", ":"), ensure_ascii=False), ex=max(1, int(ttl_seconds)))
    except Exception:
        return None


def _get_quality_thresholds() -> Dict[str, float]:
    """
    Read CQ/MR thresholds from settings; fall back to sensible defaults.
    """
    try:
        cq_min = float(getattr(settings, "cq_min_for_agent", 0.0) or 0.0)
    except Exception:
        cq_min = 0.0
    try:
        mr_min = float(getattr(settings, "mr_min_for_agent", 0.0) or 0.0)
    except Exception:
        mr_min = 0.0
    return {"cq_min": cq_min, "mr_min": mr_min}


def _payment_context_from_query(
    *,
    psp: Optional[str] = None,
    payment_method_type: Optional[str] = None,
    card_network: Optional[str] = None,
    issuer_name: Optional[str] = None,
    wallet_type: Optional[str] = None,
    installment_provider: Optional[str] = None,
) -> Optional[PivotPaymentContext]:
    payload = {
        "psp": psp,
        "payment_method_type": payment_method_type,
        "card_network": card_network,
        "issuer_name": issuer_name,
        "wallet_type": wallet_type,
        "installment_provider": installment_provider,
    }
    payload = {k: str(v).strip() for k, v in payload.items() if str(v or "").strip()}
    return PivotPaymentContext(**payload) if payload else None


def _normalize_recommendation_meta(product_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalize recommendation_meta into a stable structure.

    Older rows may not have this field; in that case, return an empty meta
    with version=1 so downstream logic can rely on the shape.
    """
    meta = (product_data or {}).get("recommendation_meta") or {}
    return {
        "version": meta.get("version", 1),
        "group_id": meta.get("group_id"),
        "tags_raw": meta.get("tags_raw") or [],
        "tags": meta.get("tags") or [],
        "facets": meta.get("facets") or {},
        "parse_error": bool(meta.get("parse_error")),
    }


def _build_secondary_tag_tokens(meta: Dict[str, Any], max_tokens: int = 6) -> List[str]:
    """
    Build a small set of tag tokens for secondary candidate prefiltering.

    Tokens map back to recommendation_meta.tags entries, e.g. "use:blush".
    Priority: use > area > cat > material > hair > feature > series > color.
    """
    facets = meta.get("facets") or {}
    tokens: List[str] = []

    for val in facets.get("use", []):
        tokens.append(f"use:{val}")
    for val in facets.get("area", []):
        tokens.append(f"area:{val}")

    cat = facets.get("cat")
    if cat:
        tokens.append(f"cat:{cat}")

    for val in facets.get("material", []):
        tokens.append(f"material:{val}")
    for val in facets.get("hair", []):
        tokens.append(f"hair:{val}")
    for val in facets.get("feature", []):
        tokens.append(f"feature:{val}")
    for val in facets.get("series", []):
        tokens.append(f"series:{val}")
    for val in facets.get("color", []):
        tokens.append(f"color:{val}")

    # Preserve order but clamp to a safe upper bound.
    seen = set()
    result: List[str] = []
    for t in tokens:
        if t in seen:
            continue
        seen.add(t)
        result.append(t)
        if len(result) >= max_tokens:
            break

    # Fallback: if facets are empty, use normalized tags directly so we can still
    # prefilter candidates by generic Shopify tags (e.g. "concealer").
    if not result:
        for t in (meta.get("tags") or []):
            if not isinstance(t, str):
                continue
            tok = t.strip()
            if not tok:
                continue
            if tok.startswith("group-") or tok.startswith("group:"):
                continue
            if tok in seen:
                continue
            seen.add(tok)
            result.append(tok)
            if len(result) >= max_tokens:
                break
    return result


def _extract_product_card(
    merchant_id: str,
    platform: str,
    platform_product_id: str,
    product_data: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Build a minimal product card from products_cache.product_data.

    For Shopify, this uses the DTO fields plus raw payload when available.
    """
    raw = (product_data or {}).get("raw") or {}

    title = (
        product_data.get("title")
        or raw.get("title")
        or ""
    )
    platform_metadata = product_data.get("platform_metadata") or {}
    handle = (
        product_data.get("handle")
        or raw.get("handle")
        or (platform_metadata.get("handle") if isinstance(platform_metadata, dict) else None)
    )

    # Main image: prefer raw.image.src, then first images[x].src, then DTO hint.
    image_url = None
    try:
        image_url = (raw.get("image") or {}).get("src") or None
    except Exception:
        image_url = None

    if not image_url:
        try:
            images = raw.get("images") or []
            if isinstance(images, list):
                for img in images:
                    if isinstance(img, dict) and img.get("src"):
                        image_url = img.get("src")
                        break
        except Exception:
            image_url = None

    if not image_url:
        image_url = product_data.get("image_url")

    # Derive simple price / inventory signals from raw variants when possible.
    price: Optional[float] = None
    currency: str = "USD"
    in_stock = False
    inventory_quantity = 0
    variants = raw.get("variants") or []
    if isinstance(variants, list) and variants:
        # Aggregate inventory and pick a reasonable price.
        for v in variants:
            try:
                qty = v.get("inventory_quantity")
                if qty is not None:
                    qty_int = int(qty)
                    inventory_quantity += max(qty_int, 0)
                    if qty_int > 0:
                        in_stock = True
            except Exception:
                continue
        try:
            first = variants[0]
            p_raw = first.get("price")
            if p_raw is not None and p_raw != "":
                price = float(p_raw)
            currency = (
                first.get("currency")
                or raw.get("presentment_currency")
                or currency
            )
        except Exception:
            # Keep defaults
            pass

    status = (product_data.get("status") or raw.get("status") or "").lower()

    # Prefer explicit orderable flag when present on product_data; otherwise
    # fall back to a simple heuristic based on status + inventory.
    explicit_orderable = product_data.get("orderable")
    if explicit_orderable is not None:
        orderable = bool(explicit_orderable)
    else:
        orderable = bool(status == "active" and (in_stock or inventory_quantity > 0))

    return {
        "merchant_id": merchant_id,
        "platform": platform,
        "platform_product_id": platform_product_id,
        "title": title,
        "handle": handle,
        "image_url": image_url,
        "price": price,
        "currency": currency,
        "in_stock": in_stock,
        "orderable": orderable,
        "status": status or None,
    }


def _compute_similarity_score(
    target_meta: Dict[str, Any],
    candidate_meta: Dict[str, Any],
    is_primary: bool,
    is_sellable: bool,
    candidate_price: Optional[float],
    target_price: Optional[float],
) -> float:
    """
    Compute a simple similarity score between target and candidate.

    Heuristics:
    - Primary (same group_id) gets a large base boost.
    - use/area overlap is strongest, then cat/material, then other facets.
    - Sellable (in stock + orderable) gets a small boost.
    - Price distance is penalized when both prices are known.
    """
    score = 0.0

    if is_primary:
        score += 1000.0

    t_facets = target_meta.get("facets") or {}
    c_facets = candidate_meta.get("facets") or {}

    def _set(name: str) -> set:
        vals = (t_facets.get(name) or []) if isinstance(t_facets.get(name), list) else t_facets.get(name)
        cand_vals = (c_facets.get(name) or []) if isinstance(c_facets.get(name), list) else c_facets.get(name)
        if isinstance(vals, list):
            s1 = set(vals)
        elif vals:
            s1 = {vals}
        else:
            s1 = set()
        if isinstance(cand_vals, list):
            s2 = set(cand_vals)
        elif cand_vals:
            s2 = {cand_vals}
        else:
            s2 = set()
        return s1, s2

    # use: strongest signal
    t_use, c_use = _set("use")
    use_overlap = len(t_use & c_use)
    score += use_overlap * 50.0

    # area: face/eyes/etc.
    t_area, c_area = _set("area")
    area_overlap = len(t_area & c_area)
    score += area_overlap * 20.0

    # cat: single-valued category
    t_cat = t_facets.get("cat")
    c_cat = c_facets.get("cat")
    if t_cat and c_cat and t_cat == c_cat:
        score += 15.0

    # material
    t_mat, c_mat = _set("material")
    score += len(t_mat & c_mat) * 10.0

    # hair/feature/series/color: weaker but still useful
    for name in ["hair", "feature", "series", "color"]:
        t_set, c_set = _set(name)
        score += len(t_set & c_set) * 3.0

    # ships: very weak tie-breaker
    t_ships, c_ships = _set("ships")
    score += len(t_ships & c_ships) * 2.0

    # Generic tag overlap (helps when facets are sparse).
    try:
        t_tags = {t for t in (target_meta.get("tags") or []) if isinstance(t, str)}
        c_tags = {t for t in (candidate_meta.get("tags") or []) if isinstance(t, str)}
        group_filtered = {t for t in (t_tags & c_tags) if not (t.startswith("group-") or t.startswith("group:"))}
        score += len(group_filtered) * 2.0
    except Exception:
        pass

    if is_sellable:
        score += 5.0

    if (
        candidate_price is not None
        and target_price is not None
        and target_price > 0
    ):
        price_diff_ratio = abs(candidate_price - target_price) / float(target_price)
        # Penalize large price deviations; cap penalty to avoid dominating other signals.
        score -= min(price_diff_ratio * 10.0, 30.0)

    return score


async def _fetch_latest_quality(
    merchant_id: str,
    platform: str,
    platform_product_id: str,
) -> Optional[Dict[str, Any]]:
    """
    Fetch latest product_quality_snapshot row for gating/sorting.
    """
    query = """
    SELECT content_quality_score, model_readiness_score
    FROM product_quality_snapshot
    WHERE merchant_id = :merchant_id
      AND platform = :platform
      AND platform_product_id = :platform_product_id
    ORDER BY snapshot_date DESC
    LIMIT 1
    """
    row = await database.fetch_one(
        query,
        {
            "merchant_id": merchant_id,
            "platform": platform,
            "platform_product_id": platform_product_id,
        },
    )
    return dict(row) if row else None


async def _fetch_group_candidates(
    merchant_id: str,
    platform_product_id: str,
    group_id: Optional[str],
    platform: str = "shopify",
    limit: int = 24,
) -> List[Dict[str, Any]]:
    """
    Fetch primary candidates sharing the same recommendation group_id.
    """
    if not group_id:
        return []

    query = """
    SELECT platform_product_id, product_data, cached_at
    FROM products_cache
    WHERE merchant_id = :merchant_id
      AND platform = :platform
      AND platform_product_id != :platform_product_id
      AND expires_at > NOW()
      AND (cache_status IS NULL OR cache_status = 'fresh')
      AND product_data::jsonb -> 'recommendation_meta' ->> 'group_id' = :group_id
    ORDER BY cached_at DESC
    LIMIT :limit
    """
    rows = await database.fetch_all(
        query,
        {
            "merchant_id": merchant_id,
            "platform": platform,
            "platform_product_id": platform_product_id,
            "group_id": group_id,
            "limit": limit,
        },
    )
    return [dict(r) for r in rows]


async def _fetch_secondary_candidates(
    merchant_id: str,
    platform_product_id: str,
    tokens: List[str],
    platform: str = "shopify",
    limit: int = 80,
) -> List[Dict[str, Any]]:
    """
    Fetch secondary candidates for facet-based similarity.

    Uses a light JSON filter on recommendation_meta.tags when tokens are provided,
    otherwise falls back to most recently cached products for the merchant.
    """
    params: Dict[str, Any] = {
        "merchant_id": merchant_id,
        "platform": platform,
        "platform_product_id": platform_product_id,
        "limit": limit,
    }

    base_where = """
    WHERE merchant_id = :merchant_id
      AND platform = :platform
      AND platform_product_id != :platform_product_id
      AND expires_at > NOW()
      AND (cache_status IS NULL OR cache_status = 'fresh')
    """

    tag_clauses: List[str] = []
    for idx, tok in enumerate(tokens):
        key = f"tag_{idx}"
        params[key] = tok
        tag_clauses.append(
            "(product_data::jsonb #> '{recommendation_meta,tags}') ? :" + key
        )

    where_sql = base_where
    if tag_clauses:
        where_sql += "\n      AND (" + " OR ".join(tag_clauses) + ")"

    query = (
        "SELECT platform_product_id, product_data, cached_at\n"
        "FROM products_cache\n"
        f"{where_sql}\n"
        "ORDER BY cached_at DESC\n"
        "LIMIT :limit"
    )

    rows = await database.fetch_all(query, params)
    return [dict(r) for r in rows]


@router.get("/merchants/{merchant_id}")
async def get_merchant_products(
    merchant_id: str,
    background_tasks: BackgroundTasks,
    limit: int = Query(default=50, le=250),
    market: Optional[str] = Query(default=None),
    psp: Optional[str] = Query(default=None),
    payment_method_type: Optional[str] = Query(default=None),
    card_network: Optional[str] = Query(default=None),
    issuer_name: Optional[str] = Query(default=None),
    wallet_type: Optional[str] = Query(default=None),
    installment_provider: Optional[str] = Query(default=None),
    context: AgentContext = Depends(get_agent_context),
):
    """
    Get merchant products via hybrid query (cache or realtime merchant API)
    
    Automatically decides between:
    - Cached products (fast, <100ms)
    - Realtime merchant API (if configured, <1s)
    
    Returns unified format regardless of source
    """
    start_time = time.time()
    
    try:
        # Verify agent has access to this merchant
        if not context.can_access_merchant(merchant_id):
            raise HTTPException(status_code=403, detail="Not authorized for this merchant")
        
        # Use hybrid query service (decides cache vs realtime)
        products, query_source, error = await get_products_hybrid(
            merchant_id=merchant_id,
            limit=limit,
            agent_id=context.agent_id,
            background_tasks=background_tasks
        )
        
        if error and not products:
            # Both realtime and cache failed
            raise HTTPException(status_code=502, detail=f"Failed to fetch products: {error}")
        
        # Calculate response time
        response_time_ms = int((time.time() - start_time) * 1000)
        
        # Log query with source tracking
        log_query_source(
            agent_id=context.agent_id,
            merchant_id=merchant_id,
            endpoint="/agent/v1/products/merchants/{merchant_id}",
            query_source=query_source,
            response_time_ms=response_time_ms,
            product_count=len(products)
        )
        
        thresholds = _get_quality_thresholds()
        cq_min = thresholds["cq_min"]
        mr_min = thresholds["mr_min"]
        ranking_config = get_agent_ranking_config()

        # Transform StandardProduct to agent-friendly format enriched via overlay,
        # then apply quality-based gating + ranking helpers.
        enriched_list: List[Dict[str, Any]] = []
        for product in products:
            exposure_projection = build_agent_push_projection_from_standard_product(
                product,
                checked_at=getattr(product, "updated_at", None),
            )
            if exposure_projection.get("agent_push_status") == AGENT_PUSH_STATUS_EXCLUDED:
                continue

            # Try to build an enriched view; fall back to StandardProduct if needed
            enriched = await get_agent_product_view(
                merchant_id=merchant_id,
                platform=product.platform,
                platform_product_id=product.id,
                geo_code="default",
            )

            display_title = (
                enriched["title"] if enriched and enriched.get("title") else product.title
            )
            image_url = (
                enriched["main_image_url"]
                if enriched and enriched.get("main_image_url")
                else product.image_url
            )

            # Handle variants (if exists) or create default variant
            if product.variants and len(product.variants) > 0:
                for variant in product.variants:
                    enriched_list.append({
                        "product_id": product.id,
                        "platform_product_id": product.id,
                        "platform": product.platform,
                        "merchant_id": merchant_id,
                        "variant_id": variant.id,
                        "title": display_title,
                        "variant_title": variant.title,
                        "price": variant.price,
                        "sku": variant.sku,
                        "inventory_quantity": variant.inventory_quantity,
                        "available": variant.inventory_quantity > 0,
                        "image_url": variant.image_url or image_url,
                        "currency": product.currency,
                        "agent_push_status": exposure_projection.get("agent_push_status"),
                        "eligible_variant_count": exposure_projection.get("eligible_variant_count"),
                        "excluded_variant_count": exposure_projection.get("excluded_variant_count"),
                    })
            else:
                # No variants - single product
                enriched_list.append({
                    "product_id": product.id,
                    "platform_product_id": product.id,
                    "platform": product.platform,
                    "merchant_id": merchant_id,
                    "variant_id": product.id,  # Use product_id as variant_id
                    "title": display_title,
                    "variant_title": "Default",
                    "price": product.price,
                    "sku": product.sku,
                    "inventory_quantity": product.inventory_quantity,
                    "available": product.inventory_quantity > 0,
                    "image_url": image_url,
                    "currency": product.currency,
                    "agent_push_status": exposure_projection.get("agent_push_status"),
                    "eligible_variant_count": exposure_projection.get("eligible_variant_count"),
                    "excluded_variant_count": exposure_projection.get("excluded_variant_count"),
                })
        # Quality-aware ranking using the same helper as /agent/v1/products/search
        ranked_candidates: List[Dict[str, Any]] = []

        for item in enriched_list:
            platform_product_id = str(
                item.get("platform_product_id")
                or item.get("product_id")
                or ""
            )
            if not platform_product_id:
                continue

            features = AgentRankingFeatures(
                merchant_id=item.get("merchant_id"),
                platform=item.get("platform") or "unknown",
                platform_product_id=platform_product_id,
                # 浏览场景没有 query，相关度统一视为 1
                rel_semantic=1.0,
                rel_keyword=1.0,
                rel_category_match=1.0,
            )

            await hydrate_quality_and_enrichment(features)

            # CQ/MR gating 仍然由 ranking_config 控制
            if not passes_agent_gating(features, ranking_config):
                continue

            score = compute_agent_ranking_score(features, ranking_config)
            item["ranking_score"] = score
            item["ranking_features"] = serialize_features_for_log(features, score)
            ranked_candidates.append(item)

        # 如果全部被 gating 掉，就回退到 enriched_list；否则使用排序结果
        if ranked_candidates:
            ranked_candidates.sort(
                key=lambda x: (
                    x.get("ranking_score") is not None,
                    x.get("ranking_score", 0.0),
                ),
                reverse=True,
            )
            agent_products = ranked_candidates
        else:
            agent_products = enriched_list

        payment_context = _payment_context_from_query(
            psp=psp,
            payment_method_type=payment_method_type,
            card_network=card_network,
            issuer_name=issuer_name,
            wallet_type=wallet_type,
            installment_provider=installment_provider,
        )
        agent_products = await enrich_product_cards_with_payment_offers(
            agent_products,
            merchant_id=merchant_id,
            payment_context=payment_context,
            market=market,
        )
        agent_products = await enrich_product_cards_with_store_discounts(
            agent_products,
            merchant_id=merchant_id,
        )

        # Log request for analytics
        background_tasks.add_task(
            log_agent_request,
            context=context,
            status_code=200,
            merchant_id=merchant_id
        )
        
        response = {
            "status": "success",
            "merchant_id": merchant_id,
            "query_source": query_source,  # NEW: indicate data source
            "total_products": len(agent_products),
            "products": agent_products,
            "response_time_ms": response_time_ms  # NEW: performance metric
        }
        # Persist ranking features for browse-by-merchant as well (best-effort).
        try:
            await log_ranking_batch(
                agent_id=getattr(context, "agent_id", None),
                endpoint="/agent/v1/products/merchants/{merchant_id}",
                query=None,
                products=agent_products,
                max_rows=50,
            )
        except Exception as e:
            logger.debug(
                f"Failed to log merchant browse ranking batch: {e}", exc_info=True
            )

        # Log impression events for browsing by merchant.
        try:
            events = []
            for idx, p in enumerate(agent_products[:50]):
                feat = (p.get("ranking_features") or {}) if isinstance(
                    p.get("ranking_features"), dict
                ) else {}
                events.append(
                    {
                        "agent_id": getattr(context, "agent_id", None),
                        "session_id": getattr(context, "session_id", None),
                        "event_type": "impression",
                        "endpoint": "/agent/v1/products/merchants/{merchant_id}",
                        "query": None,
                        "merchant_id": merchant_id,
                        "platform": p.get("platform"),
                        "platform_product_id": str(
                            p.get("platform_product_id")
                            or p.get("product_id")
                            or ""
                        )
                            or None,
                        "ranking_score": p.get("ranking_score"),
                        "position": idx,
                        "quality_content_score": feat.get("quality_content_score"),
                        "quality_model_readiness": feat.get(
                            "quality_model_readiness"
                        ),
                    }
                )
            await log_product_events(events)
        except Exception as e:
            logger.debug(
                f"Failed to log merchant browse product events: {e}", exc_info=True
            )

        return response
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get merchant products: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get products: {str(e)}")


@router.get("/merchants/{merchant_id}/product/{product_id}")
async def get_product_details(
    merchant_id: str,
    product_id: str,
    background_tasks: BackgroundTasks,
    market: Optional[str] = Query(default=None),
    psp: Optional[str] = Query(default=None),
    payment_method_type: Optional[str] = Query(default=None),
    card_network: Optional[str] = Query(default=None),
    issuer_name: Optional[str] = Query(default=None),
    wallet_type: Optional[str] = Query(default=None),
    installment_provider: Optional[str] = Query(default=None),
    context: AgentContext = Depends(get_agent_context),
):
    """Get detailed information about a specific product"""
    try:
        # Verify access
        if not context.can_access_merchant(merchant_id):
            raise HTTPException(status_code=403, detail="Not authorized")

        merchant = await get_merchant_onboarding(merchant_id)
        if not merchant:
            raise HTTPException(status_code=404, detail="Merchant not found")

        # Get primary store for this merchant (may be Shopify, Wix, etc.)
        store = await merchant_store_service.get_primary_store(merchant_id)
        if not store:
            raise HTTPException(
                status_code=404,
                detail="No connected stores found for merchant",
                headers={"X-Error-Code": "STORE_NOT_FOUND"},
            )

        platform = store.get("platform")

        cached = await _redis_get_json(
            _product_details_cache_key(merchant_id=merchant_id, platform=str(platform or "unknown"), platform_product_id=str(product_id))
        )
        if cached and isinstance(cached, dict) and cached.get("status") == "success":
            product_payload = cached.get("product")
            if isinstance(product_payload, dict):
                await enrich_product_detail_with_payment_offers(
                    product_payload,
                    merchant_id=merchant_id,
                    payment_context=_payment_context_from_query(
                        psp=psp,
                        payment_method_type=payment_method_type,
                        card_network=card_network,
                        issuer_name=issuer_name,
                        wallet_type=wallet_type,
                        installment_provider=installment_provider,
                    ),
                    market=market,
                )
                await enrich_product_detail_with_store_discounts(
                    product_payload,
                    merchant_id=merchant_id,
                )
            background_tasks.add_task(
                log_agent_request,
                context=context,
                status_code=200,
                merchant_id=merchant_id,
            )
            return cached

        # For non-Shopify platforms (e.g. Wix), serve details from cached StandardProduct
        if platform != "shopify":
            cache_row = await get_product_cache_row(
                merchant_id=merchant_id,
                platform=platform,
                platform_product_id=product_id,
                include_expired=False,
            )

            if not cache_row:
                raise HTTPException(status_code=404, detail="Product not found")

            product_data = cache_row.get("product_data") or {}
            if isinstance(product_data, str):
                try:
                    product_data = json.loads(product_data)
                except Exception:
                    product_data = {}

            try:
                sp = StandardProduct(**product_data)
                prod = sp.dict()
            except Exception:
                prod = product_data

            exposure_projection = build_agent_push_projection_from_standard_product(
                prod,
                checked_at=cache_row.get("cached_at"),
            )
            if exposure_projection.get("agent_push_status") == AGENT_PUSH_STATUS_EXCLUDED:
                raise HTTPException(
                    status_code=404,
                    detail="Product is currently excluded from agent exposure",
                )

            product_id_out = str(prod.get("product_id") or prod.get("id") or product_id)
            title = prod.get("title") or prod.get("name") or ""
            description = prod.get("description") or ""
            product_type = prod.get("product_type")
            tags = prod.get("tags") or []

            images = prod.get("images") or []
            if isinstance(images, dict):
                if images.get("image_url"):
                    images = [images.get("image_url")]
                else:
                    images = []

            image_url = prod.get("image_url")
            if image_url and image_url not in images:
                images = [image_url] + images

            variants_raw = prod.get("variants") or []
            variants: List[Dict[str, Any]] = []
            for v in variants_raw:
                vid = str(v.get("variant_id") or v.get("id") or product_id_out)
                price = float(v.get("price") or prod.get("price") or 0)
                inv_qty = int(v.get("inventory_quantity") or 0)
                variants.append(
                    {
                        "variant_id": vid,
                        "title": v.get("title") or "Default",
                        "price": price,
                        "sku": v.get("sku"),
                        "inventory_quantity": inv_qty,
                        "available": inv_qty > 0,
                    }
                )

            if not variants:
                inv_qty = int(prod.get("inventory_quantity") or 0)
                variants.append(
                    {
                        "variant_id": product_id_out,
                        "title": "Default",
                        "price": float(prod.get("price") or 0),
                        "sku": prod.get("sku"),
                        "inventory_quantity": inv_qty,
                        "available": inv_qty > 0,
                    }
                )

            background_tasks.add_task(
                log_agent_request,
                context=context,
                status_code=200,
                merchant_id=merchant_id,
            )

            resp = {
                "status": "success",
                "product": {
                    "id": product_id_out,
                    "merchant_id": merchant_id,
                    "currency": prod.get("currency") or "USD",
                    "title": title,
                    "description": description,
                    "vendor": prod.get("vendor"),
                    "product_type": product_type,
                    "variants": variants,
                    "options": prod.get("options") or [],
                    "images": images,
                    "tags": tags,
                },
            }
            await _redis_set_json(
                _product_details_cache_key(merchant_id=merchant_id, platform=str(platform or "unknown"), platform_product_id=str(product_id)),
                resp,
                PRODUCT_DETAILS_CACHE_TTL_SECONDS,
            )
            await enrich_product_detail_with_payment_offers(
                resp["product"],
                merchant_id=merchant_id,
                payment_context=_payment_context_from_query(
                    psp=psp,
                    payment_method_type=payment_method_type,
                    card_network=card_network,
                    issuer_name=issuer_name,
                    wallet_type=wallet_type,
                    installment_provider=installment_provider,
                ),
                market=market,
            )
            await enrich_product_detail_with_store_discounts(
                resp["product"],
                merchant_id=merchant_id,
            )
            return resp

        # Shopify path: fetch fresh details from Shopify Admin API
        shop_domain = store.get("domain") or store.get("shop_domain")
        if not shop_domain:
            raise HTTPException(
                status_code=400,
                detail="Store configuration incomplete - missing domain",
                headers={"X-Error-Code": "INVALID_STORE_CONFIG"},
            )

        access_token, _ = await resolve_shopify_admin_access_token(
            shop_domain=shop_domain,
            api_key_raw=store.get("api_key_raw") or store.get("api_key") or store.get("access_token"),
            store_id=str(store.get("store_id") or "").strip() or None,
        )

        if not access_token:
            raise HTTPException(
                status_code=400,
                detail="Store configuration incomplete - missing credentials",
                headers={"X-Error-Code": "INVALID_STORE_CONFIG"},
            )

        # Fetch product from Shopify
        url = f"https://{shop_domain}/admin/api/2025-10/products/{product_id}.json"
        headers = {
            "X-Shopify-Access-Token": access_token,
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=headers, timeout=10.0)

        if response.status_code != 200:
            raise HTTPException(status_code=404, detail="Product not found")

        product = response.json()["product"]

        # Transform for agent
        options_out: List[Dict[str, Any]] = []
        for opt in product.get("options") or []:
            if not isinstance(opt, dict):
                continue
            options_out.append(
                {
                    "name": opt.get("name"),
                    "position": opt.get("position"),
                    "values": opt.get("values") or [],
                }
            )

        variants = []
        for variant in product.get("variants", []):
            option_map: Dict[str, str] = {}
            for opt in options_out:
                name = str(opt.get("name") or "").strip()
                if not name:
                    continue
                pos = int(opt.get("position") or 0)
                if pos == 1:
                    val = variant.get("option1")
                elif pos == 2:
                    val = variant.get("option2")
                elif pos == 3:
                    val = variant.get("option3")
                else:
                    val = None
                if val:
                    option_map[name] = str(val)
            variants.append(
                {
                    "variant_id": str(variant["id"]),
                    "title": variant.get("title", "Default"),
                    "price": float(variant.get("price", 0)),
                    "sku": variant.get("sku"),
                    "inventory_quantity": variant.get("inventory_quantity", 0),
                    "available": variant.get("inventory_quantity", 0) > 0,
                    "weight": variant.get("weight"),
                    "weight_unit": variant.get("weight_unit"),
                    "options": option_map,
                }
            )

        exposure_projection = build_agent_push_projection_from_standard_product(
            {
                "id": str(product.get("id") or product_id),
                "platform": "shopify",
                "merchant_id": merchant_id,
                "title": product.get("title"),
                "currency": (variants[0].get("currency") if variants else None) or "USD",
                "variants": [
                    {
                        "id": item.get("variant_id"),
                        "price": item.get("price"),
                        "currency": "USD",
                        "inventory_quantity": item.get("inventory_quantity"),
                        "available": item.get("available"),
                    }
                    for item in variants
                ],
            }
        )
        if exposure_projection.get("agent_push_status") == AGENT_PUSH_STATUS_EXCLUDED:
            raise HTTPException(
                status_code=404,
                detail="Product is currently excluded from agent exposure",
            )

        background_tasks.add_task(
            log_agent_request,
            context=context,
            status_code=200,
            merchant_id=merchant_id,
        )
        # Log a click event for this product (best-effort; independent of response)
        try:
            await log_product_events(
                [
                    {
                        "agent_id": getattr(context, "agent_id", None),
                        "session_id": getattr(context, "session_id", None),
                        "event_type": "click",
                        "endpoint": "/agent/v1/products/merchants/{merchant_id}/product/{product_id}",
                        "query": None,
                        "merchant_id": merchant_id,
                        "platform": platform,
                        "platform_product_id": str(product_id),
                        "ranking_score": None,
                        "position": None,
                        "quality_content_score": None,
                        "quality_model_readiness": None,
                    }
                ]
            )
        except Exception as e:
            logger.debug(f"Failed to log product click event: {e}", exc_info=True)

        resp = {
            "status": "success",
            "product": {
                "id": str(product["id"]),
                "merchant_id": merchant_id,
                "currency": "USD",
                "title": product["title"],
                "description": product.get("body_html", ""),
                "description_text": product.get("description_text", ""),
                "vendor": product.get("vendor"),
                "product_type": product.get("product_type"),
                "variants": variants,
                "options": options_out,
                "images": [img.get("src") for img in product.get("images", [])],
                "tags": product.get("tags", "").split(",")
                if product.get("tags")
                else [],
            },
        }
        await _redis_set_json(
            _product_details_cache_key(merchant_id=merchant_id, platform="shopify", platform_product_id=str(product_id)),
            resp,
            PRODUCT_DETAILS_CACHE_TTL_SECONDS,
        )
        await enrich_product_detail_with_payment_offers(
            resp["product"],
            merchant_id=merchant_id,
            payment_context=_payment_context_from_query(
                psp=psp,
                payment_method_type=payment_method_type,
                card_network=card_network,
                issuer_name=issuer_name,
                wallet_type=wallet_type,
                installment_provider=installment_provider,
            ),
            market=market,
        )
        await enrich_product_detail_with_store_discounts(
            resp["product"],
            merchant_id=merchant_id,
        )
        return resp
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get product details: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get product: {str(e)}")


@router.get("/merchants/{merchant_id}/variant/{variant_id}")
async def get_product_details_by_variant(
    merchant_id: str,
    variant_id: str,
    background_tasks: BackgroundTasks,
    context: AgentContext = Depends(get_agent_context),
):
    """Resolve a platform variant_id to its parent product and return product details."""
    if not context.can_access_merchant(merchant_id):
        raise HTTPException(status_code=403, detail="Not authorized")

    merchant = await get_merchant_onboarding(merchant_id)
    if not merchant:
        raise HTTPException(status_code=404, detail="Merchant not found")

    store = await merchant_store_service.get_primary_store(merchant_id)
    if not store:
        raise HTTPException(
            status_code=404,
            detail="No connected stores found for merchant",
            headers={"X-Error-Code": "STORE_NOT_FOUND"},
        )

    platform = store.get("platform")
    if platform != "shopify":
        # Best-effort: treat variant_id as product_id for non-Shopify cached products.
        resp = await get_product_details(
            merchant_id=merchant_id,
            product_id=variant_id,
            background_tasks=background_tasks,
            context=context,
        )
        if isinstance(resp, dict) and resp.get("status") == "success":
            resp["selected_variant_id"] = str(variant_id)
        return resp

    shop_domain = store.get("domain") or store.get("shop_domain")
    access_token, _ = await resolve_shopify_admin_access_token(
        shop_domain=shop_domain,
        api_key_raw=store.get("api_key_raw") or store.get("api_key") or store.get("access_token"),
        store_id=str(store.get("store_id") or "").strip() or None,
    )

    if not shop_domain:
        raise HTTPException(
            status_code=400,
            detail="Store configuration incomplete - missing domain",
            headers={"X-Error-Code": "INVALID_STORE_CONFIG"},
        )

    if not access_token:
        raise HTTPException(
            status_code=400,
            detail="Store configuration incomplete - missing credentials",
            headers={"X-Error-Code": "INVALID_STORE_CONFIG"},
        )

    variant_key = _variant_to_product_cache_key(merchant_id=merchant_id, platform="shopify", variant_id=str(variant_id))
    cached_pid = None
    client = get_redis_client()
    if client is not None:
        try:
            cached_pid = await client.get(variant_key)
        except Exception:
            cached_pid = None

    product_id = str(cached_pid or "").strip() or None
    if not product_id:
        url = f"https://{shop_domain}/admin/api/2025-10/variants/{variant_id}.json"
        headers = {
            "X-Shopify-Access-Token": access_token,
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient() as http:
            response = await http.get(url, headers=headers, timeout=10.0)
        if response.status_code != 200:
            raise HTTPException(status_code=404, detail="Variant not found")
        variant = response.json().get("variant") or {}
        product_id = str(variant.get("product_id") or "").strip()
        if not product_id:
            raise HTTPException(status_code=404, detail="Variant not found")
        if client is not None:
            try:
                await client.set(variant_key, product_id, ex=max(1, int(PRODUCT_DETAILS_CACHE_TTL_SECONDS)))
            except Exception:
                pass

    resp = await get_product_details(
        merchant_id=merchant_id,
        product_id=product_id,
        background_tasks=background_tasks,
        context=context,
    )

    if isinstance(resp, dict) and resp.get("status") == "success":
        selected_variant_id = str(variant_id)
        resp["selected_variant_id"] = selected_variant_id
        try:
            variants = resp.get("product", {}).get("variants") or []
            for v in variants:
                if isinstance(v, dict) and str(v.get("variant_id")) == selected_variant_id:
                    v["selected"] = True
        except Exception:
            pass

        # Best-effort: log a click for variant-details surface (no PII).
        try:
            await log_product_events(
                [
                    {
                        "agent_id": getattr(context, "agent_id", None),
                        "session_id": getattr(context, "session_id", None),
                        "event_type": "click",
                        "endpoint": "/agent/v1/products/merchants/{merchant_id}/variant/{variant_id}",
                        "query": None,
                        "merchant_id": merchant_id,
                        "platform": platform,
                        "platform_product_id": str(product_id),
                        "ranking_score": None,
                        "position": None,
                        "quality_content_score": None,
                        "quality_model_readiness": None,
                    }
                ]
            )
        except Exception as e:
            logger.debug(f"Failed to log product click event: {e}", exc_info=True)

    return resp



@router.get("/merchants/{merchant_id}/product/{product_id}/related")
async def get_related_products(
    merchant_id: str,
    product_id: str,
    background_tasks: BackgroundTasks,
    limit: int = Query(default=10, ge=1, le=50),
    context: AgentContext = Depends(get_agent_context),
):
    """
    Recommend related products for a given product.

    Strategy:
    - Fetch products via hybrid query (cache or realtime)
    - Compute similarity based on vendor, product_type, tags, and title tokens
    - Return top-N related items (excluding the current product)
    """
    start_time = time.time()

    # Verify access
    if not context.can_access_merchant(merchant_id):
        raise HTTPException(status_code=403, detail="Not authorized for this merchant")

    try:
        # Load a reasonably sized candidate pool
        candidates, query_source, _ = await get_products_hybrid(
            merchant_id=merchant_id,
            limit=250,
            agent_id=context.agent_id,
            background_tasks=background_tasks,
        )

        # Find target product
        target = None
        for p in candidates:
            if str(p.id) == str(product_id):
                target = p
                break

        if not target and candidates:
            # If exact not found, pick first as fallback basis
            target = candidates[0]

        if not target:
            raise HTTPException(status_code=404, detail="No products available for recommendations")

        def tokenize(text: str) -> set:
            if not text:
                return set()
            return set([t for t in text.lower().replace("/", " ").replace("-", " ").split() if len(t) > 2])

        target_vendor = (target.vendor or "").strip().lower()
        target_type = (target.product_type or "").strip().lower()
        target_tags = set([t.strip().lower() for t in (target.tags or []) if t])
        target_tokens = tokenize(target.title) | tokenize(target.description or "")

        scored = []
        for p in candidates:
            if str(p.id) == str(product_id):
                continue
            score = 0.0
            reasons = []

            # Vendor boost
            if p.vendor and p.vendor.strip().lower() == target_vendor and target_vendor:
                score += 3.0
                reasons.append("same_vendor")

            # Product type boost
            if p.product_type and p.product_type.strip().lower() == target_type and target_type:
                score += 2.0
                reasons.append("same_type")

            # Tag overlap
            p_tags = set([t.strip().lower() for t in (p.tags or []) if t])
            overlap = len(p_tags & target_tags)
            if overlap:
                score += min(2.0, 0.6 * overlap)
                reasons.append("tag_overlap")

            # Title token overlap
            p_tokens = tokenize(p.title) | tokenize(p.description or "")
            token_overlap = len(p_tokens & target_tokens)
            if token_overlap:
                score += min(1.5, 0.3 * token_overlap)
                reasons.append("title_overlap")

            if score > 0:
                # Map to agent product shape
                image_url = p.image_url
                price = p.price
                if p.variants and len(p.variants) > 0:
                    # pick cheapest variant
                    v = sorted(p.variants, key=lambda v: v.price)[0]
                    image_url = v.image_url or image_url
                    price = v.price
                scored.append({
                    "product_id": p.id,
                    "title": p.title,
                    "price": price,
                    "currency": p.currency,
                    "image_url": image_url,
                    "vendor": p.vendor,
                    "product_type": p.product_type,
                    "relevance_score": round(score, 3),
                    "reasons": reasons,
                })

        scored.sort(key=lambda x: x["relevance_score"], reverse=True)
        top_related = scored[:limit]

        response_time_ms = int((time.time() - start_time) * 1000)
        log_query_source(
            agent_id=context.agent_id,
            merchant_id=merchant_id,
            endpoint="/agent/v1/products/merchants/{merchant_id}/product/{product_id}/related",
            query_source=query_source,
            response_time_ms=response_time_ms,
            product_count=len(top_related),
        )

        background_tasks.add_task(
            log_agent_request,
            context=context,
            status_code=200,
            merchant_id=merchant_id,
        )

        return {
            "status": "success",
            "merchant_id": merchant_id,
            "product_id": product_id,
            "query_source": query_source,
            "response_time_ms": response_time_ms,
            "total": len(top_related),
            "related": top_related,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Related products error: {e}")
        raise HTTPException(status_code=500, detail="Failed to compute related products")


@router.get("/recommendations")
async def get_product_recommendations(
    merchant_id: str,
    platform_product_id: str,
    background_tasks: BackgroundTasks,
    limit: int = Query(default=8, ge=1, le=20),
    context: AgentContext = Depends(get_agent_context),
):
    """
    Recommend related products for a given Shopify product.

    Strategy:
    - Target is resolved from products_cache (platform='shopify').
    - Primary candidates: same recommendation_meta.group_id.
    - Secondary candidates: facet-similar products from products_cache using
      recommendation_meta.tags prefilter + Python reranking.
    """
    start_time = time.time()

    # Verify agent has access to this merchant
    if not context.can_access_merchant(merchant_id):
        raise HTTPException(
            status_code=403, detail="Not authorized for this merchant"
        )

    platform = "shopify"

    try:
        # 1. Load target product from products_cache
        target_row = await get_product_cache_row(
            merchant_id=merchant_id,
            platform=platform,
            platform_product_id=platform_product_id,
            include_expired=False,
        )

        if not target_row:
            raise HTTPException(status_code=404, detail="Target product not found")

        product_data = target_row.get("product_data") or {}
        if isinstance(product_data, str):
            try:
                product_data = json.loads(product_data)
            except Exception:
                product_data = {}

        target_meta = _normalize_recommendation_meta(product_data)
        target_group_id = target_meta.get("group_id")
        target_price: Optional[float] = None

        # Build a unified target card for response and scoring.
        target_card = _extract_product_card(
            merchant_id=merchant_id,
            platform=platform,
            platform_product_id=platform_product_id,
            product_data=product_data,
        )
        target_price = target_card.get("price")

        # 2. Fetch primary (same-group) candidates.
        primary_fetch_limit = min(limit * 3, 48)
        primary_rows = await _fetch_group_candidates(
            merchant_id=merchant_id,
            platform_product_id=platform_product_id,
            group_id=target_group_id,
            platform=platform,
            limit=primary_fetch_limit,
        )

        # 3. Fetch secondary (facet-similar) candidates.
        tokens = _build_secondary_tag_tokens(target_meta, max_tokens=6)
        secondary_fetch_limit = min(max(limit * 10, 40), 200)
        if tokens:
            secondary_rows = await _fetch_secondary_candidates(
                merchant_id=merchant_id,
                platform_product_id=platform_product_id,
                tokens=tokens,
                platform=platform,
                limit=secondary_fetch_limit,
            )
        else:
            secondary_rows = []

        candidates: Dict[str, Dict[str, Any]] = {}

        # Helper to ingest rows into the candidate map.
        def _ingest_rows(
            rows: List[Dict[str, Any]],
            source: str,
            is_primary: bool,
        ) -> None:
            for row in rows:
                pid = str(row.get("platform_product_id") or "")
                if not pid or pid == platform_product_id:
                    continue
                if pid in candidates:
                    # Prefer primary metadata when duplicate appears.
                    if is_primary and not candidates[pid].get("is_primary"):
                        candidates[pid]["is_primary"] = True
                    continue

                pdata = row.get("product_data") or {}
                if isinstance(pdata, str):
                    try:
                        pdata = json.loads(pdata)
                    except Exception:
                        pdata = {}

                meta = _normalize_recommendation_meta(pdata)
                card = _extract_product_card(
                    merchant_id=merchant_id,
                    platform=platform,
                    platform_product_id=pid,
                    product_data=pdata,
                )
                candidates[pid] = {
                    "platform_product_id": pid,
                    "product_data": pdata,
                    "meta": meta,
                    "card": card,
                    "cached_at": row.get("cached_at"),
                    "source": source,
                    "is_primary": is_primary,
                }

        _ingest_rows(primary_rows, source="primary_group", is_primary=True)
        _ingest_rows(secondary_rows, source="secondary_facets", is_primary=False)

        # 4. Compute scores and build final recommendation list.
        ranked: List[Dict[str, Any]] = []

        for pid, item in candidates.items():
            card = item["card"]
            meta = item["meta"]
            is_primary = bool(item.get("is_primary"))
            is_sellable = bool(card.get("orderable")) and bool(
                card.get("in_stock") or card.get("inventory_quantity", 0) > 0
            )
            candidate_price = card.get("price")
            score = _compute_similarity_score(
                target_meta=target_meta,
                candidate_meta=meta,
                is_primary=is_primary,
                is_sellable=is_sellable,
                candidate_price=candidate_price,
                target_price=target_price,
            )
            item["score"] = score
            ranked.append(item)

        # Sort by score, then sellability, then recency.
        def _sort_key(it: Dict[str, Any]):
            card = it.get("card") or {}
            cached_at = it.get("cached_at")
            if isinstance(cached_at, str):
                try:
                    cached_at_dt = datetime.fromisoformat(cached_at)
                except Exception:
                    cached_at_dt = datetime.min
            elif isinstance(cached_at, datetime):
                cached_at_dt = cached_at
            else:
                cached_at_dt = datetime.min

            return (
                it.get("score", 0.0),
                cached_at_dt,
            )

        ranked.sort(key=_sort_key, reverse=True)
        top_ranked = ranked[:limit]

        recommendations = [it["card"] | {"score": it.get("score")} for it in top_ranked]

        response_time_ms = int((time.time() - start_time) * 1000)

        primary_count = len(primary_rows)
        secondary_count = len(secondary_rows)

        # Log query source and performance.
        log_query_source(
            agent_id=context.agent_id,
            merchant_id=merchant_id,
            endpoint="/agent/v1/products/recommendations",
            query_source="cache_only",
            response_time_ms=response_time_ms,
            product_count=len(recommendations),
        )

        logger.info(
            "agent_product_recommendations",
            extra={
                "event": "agent_product_recommendations",
                "merchant_id": merchant_id,
                "platform": platform,
                "platform_product_id": platform_product_id,
                "group_id": target_group_id,
                "tokens_used": tokens,
                "primary_count": primary_count,
                "secondary_count": secondary_count,
                "returned": len(recommendations),
                "response_time_ms": response_time_ms,
            },
        )

        background_tasks.add_task(
            log_agent_request,
            context=context,
            status_code=200,
            merchant_id=merchant_id,
        )

        return {
            "status": "success",
            "merchant_id": merchant_id,
            "platform": platform,
            "platform_product_id": platform_product_id,
            "target": target_card,
            "recommendations": recommendations,
            "debug": {
                "group_id": target_group_id,
                "primary_count": primary_count,
                "secondary_count": secondary_count,
                "parse_error": bool(target_meta.get("parse_error")),
            },
            "response_time_ms": response_time_ms,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to compute product recommendations: {e}")
        raise HTTPException(
            status_code=500, detail="Failed to compute product recommendations"
        )
