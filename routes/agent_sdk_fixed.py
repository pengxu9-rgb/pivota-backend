"""
SDK-Ready Agent API Endpoints - COMPREHENSIVE FIX
Properly handles all database schema issues and edge cases
"""
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import datetime
from db.database import database
from routes.agent_auth import AgentContext, get_agent_context
from utils.logger import logger
import secrets
import json

from services.agent_ranking_service import (
    AgentRankingFeatures,
    get_agent_ranking_config,
    hydrate_quality_and_enrichment,
    passes_agent_gating,
    compute_agent_ranking_score,
    serialize_features_for_log,
)
from services.outbound_links_service import (
    DEFAULT_DISCLOSURE_TEXT,
    DEFAULT_UTM_TEMPLATE,
    _is_domain_allowed,
    apply_utm,
    make_redirect_token,
)
from db.agent_ranking_log import log_ranking_batch
from db.agent_product_events import log_product_events

router = APIRouter(prefix="/agent/v1", tags=["agent-sdk"])

EXTERNAL_SEED_MERCHANT_ID = "external_seed"
DEFAULT_EXTERNAL_SEED_MARKET = "US"


def _stable_external_product_id(url: str) -> str:
    import hashlib

    u = str(url or "").strip()
    if not u:
        return ""
    return "ext_" + hashlib.sha256(u.encode("utf-8")).hexdigest()[:24]


def _ensure_json_obj(val: Any) -> Dict[str, Any]:
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


def _seed_variants(seed_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    variants = seed_data.get("variants")
    if isinstance(variants, list):
        return [v for v in variants if isinstance(v, dict)]
    return []


def _seed_primary_price(seed_row: Dict[str, Any], seed_data: Dict[str, Any]) -> Dict[str, Any]:
    variants = _seed_variants(seed_data)
    for v in variants:
        amt = v.get("price_amount")
        cur = v.get("price_currency") or v.get("currency")
        if amt is not None:
            try:
                return {"amount": float(amt), "currency": str(cur or "") or None}
            except Exception:
                return {"amount": amt, "currency": str(cur or "") or None}
    return {"amount": seed_row.get("price_amount"), "currency": seed_row.get("price_currency")}


def _availability_to_in_stock(availability: Any) -> bool:
    if availability is None:
        return True
    if isinstance(availability, bool):
        return availability
    raw = str(availability).strip().lower()
    if not raw:
        return True
    return raw not in {"out_of_stock", "outofstock", "sold_out", "soldout", "unavailable"}


def _request_base_url(req: Request) -> str:
    return str(req.base_url).rstrip("/")


async def _build_external_seed_product(
    *,
    req: Request,
    seed_row: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    seed_id = str(seed_row.get("id") or "").strip()
    if not seed_id:
        return None

    market = str(seed_row.get("market") or DEFAULT_EXTERNAL_SEED_MARKET).strip().upper() or DEFAULT_EXTERNAL_SEED_MARKET
    tool = str(seed_row.get("tool") or "*").strip() or "*"
    destination_url = str(seed_row.get("destination_url") or "").strip()
    if not destination_url.startswith("http://") and not destination_url.startswith("https://"):
        return None

    seed_data = _ensure_json_obj(seed_row.get("seed_data"))
    canonical_url = str(seed_row.get("canonical_url") or "").strip() or None
    title = seed_data.get("title") or seed_row.get("title") or None
    brand_raw = seed_data.get("brand") or None
    if isinstance(brand_raw, dict):
        brand_raw = brand_raw.get("name") or brand_raw.get("brand") or None
    brand = str(brand_raw).strip() if isinstance(brand_raw, str) and brand_raw.strip() else None
    image_url = seed_data.get("image_url") or seed_row.get("image_url") or None

    external_product_id = (
        str(seed_row.get("external_product_id") or "").strip()
        or str(seed_data.get("external_product_id") or "").strip()
        or _stable_external_product_id(canonical_url or destination_url)
    )
    if not external_product_id:
        return None

    disclosure_text = seed_row.get("disclosure_text") or seed_data.get("disclosure_text") or DEFAULT_DISCLOSURE_TEXT
    utm_template = seed_row.get("utm_template") or seed_data.get("utm_template") or DEFAULT_UTM_TEMPLATE

    dest_with_utm = apply_utm(destination_url, utm_template, {"market": market, "tool": tool})
    if not await _is_domain_allowed(market=market, destination_url=dest_with_utm):
        return None

    external_domain = str(seed_row.get("domain") or "").strip() or None
    if not external_domain:
        try:
            from urllib.parse import urlparse

            external_domain = urlparse(canonical_url or destination_url).hostname or None
        except Exception:
            external_domain = None

    token = make_redirect_token(
        {
            "market": market,
            "tool": tool,
            "dest": dest_with_utm,
            "ctx": {
                "source": "external_seed",
                "external_seed_id": seed_id,
                "external_product_id": external_product_id,
            },
        }
    )
    external_redirect_url = f"{_request_base_url(req)}/r?token={token}"

    primary_price = _seed_primary_price(seed_row, seed_data)
    price_amount = primary_price.get("amount")
    price_currency = primary_price.get("currency") or "USD"
    try:
        price = float(price_amount) if price_amount is not None else 0.0
    except Exception:
        price = 0.0

    seed_variants = _seed_variants(seed_data)
    variants: List[Dict[str, Any]] = []
    seen_variant_ids: set[str] = set()
    for idx, v in enumerate(seed_variants):
        raw_variant_id = v.get("variant_id") or v.get("id") or v.get("sku")
        variant_id = str(raw_variant_id or "").strip() or f"{external_product_id}_{idx + 1}"
        if variant_id in seen_variant_ids:
            continue
        seen_variant_ids.add(variant_id)

        raw_amount = v.get("price_amount")
        if raw_amount is None:
            raw_amount = v.get("price") or v.get("amount") or v.get("value")
        raw_currency = v.get("price_currency") or v.get("currency") or price_currency

        try:
            variant_price = float(raw_amount) if raw_amount is not None else price
        except Exception:
            variant_price = price

        availability = v.get("availability")
        in_stock = _availability_to_in_stock(availability)

        variants.append(
            {
                "id": variant_id,
                "variant_id": variant_id,
                "title": v.get("title") or v.get("name") or f"Variant {idx + 1}",
                "price": variant_price,
                "currency": str(raw_currency or "USD").strip() or "USD",
                "inventory_quantity": 999 if in_stock else 0,
                "in_stock": in_stock,
                **({"availability": availability} if availability is not None else {}),
            }
        )
        if len(variants) >= 30:
            break

    if not variants:
        variants = [
            {
                "id": external_product_id,
                "variant_id": external_product_id,
                "title": "Default",
                "price": price,
                "currency": price_currency,
                "inventory_quantity": 999,
                "in_stock": True,
            }
        ]

    return {
        "id": external_product_id,
        "product_id": external_product_id,
        "merchant_id": EXTERNAL_SEED_MERCHANT_ID,
        "merchant_name": "External",
        "platform": "external",
        "platform_product_id": external_product_id,
        "title": title or destination_url,
        "name": title or destination_url,
        "description": str(seed_data.get("description") or "") or "",
        "price": price,
        "currency": price_currency,
        "image_url": image_url,
        "in_stock": True,
        "inventory_quantity": 999,
        "product_type": "external",
        "source": "external_seed",
        "external_seed_id": seed_id,
        "external_redirect_url": external_redirect_url,
        "external_domain": external_domain,
        "external_url": canonical_url or destination_url,
        "disclosure_text": str(disclosure_text or DEFAULT_DISCLOSURE_TEXT),
        "brand": brand,
        "variants": variants,
    }


async def _load_external_seed_products_for_search(
    *,
    req: Request,
    query: Optional[str],
    limit: int,
    offset: int,
) -> List[Dict[str, Any]]:
    q = str(query or "").strip()
    where = ["status = :status", "attached_product_key IS NULL", "market = :market"]
    values: Dict[str, Any] = {
        "status": "active",
        "market": DEFAULT_EXTERNAL_SEED_MARKET,
        "limit": limit,
        "offset": offset,
    }
    if q:
        values["q_like"] = f"%{q}%"
        clauses = [
            "(destination_url ILIKE :q_like OR canonical_url ILIKE :q_like OR domain ILIKE :q_like OR title ILIKE :q_like)"
        ]
        q_compact = "".join(q.split())
        if q_compact and q_compact != q:
            values["q_compact_like"] = f"%{q_compact}%"
            clauses.append(
                "(destination_url ILIKE :q_compact_like OR canonical_url ILIKE :q_compact_like OR domain ILIKE :q_compact_like OR title ILIKE :q_compact_like)"
            )
        where.append("(" + " OR ".join(clauses) + ")")

    rows = []
    try:
        rows = await database.fetch_all(
            f"""
            SELECT
              id, external_product_id, market, tool, utm_template, partner_type, disclosure_text,
              destination_url, canonical_url, domain, title, image_url,
              price_amount, price_currency, availability,
              seed_data,
              status, notes, created_by_employee_id,
              attached_product_key, attached_variant_id,
              created_at, updated_at
            FROM external_product_seeds
            WHERE {" AND ".join(where)}
            ORDER BY updated_at DESC, created_at DESC
            LIMIT :limit OFFSET :offset
            """,
            values,
        )
    except Exception as exc:
        msg = str(exc)
        if "external_product_seeds" in msg and ("does not exist" in msg or "UndefinedTable" in msg or "relation" in msg):
            return []
        raise

    products: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows or []:
        seed_row = dict(row)
        seed_data = _ensure_json_obj(seed_row.get("seed_data"))
        external_product_id = (
            str(seed_row.get("external_product_id") or "").strip()
            or str(seed_data.get("external_product_id") or "").strip()
            or _stable_external_product_id(seed_row.get("canonical_url") or seed_row.get("destination_url") or "")
        )
        if not external_product_id or external_product_id in seen:
            continue
        seen.add(external_product_id)
        try:
            prod = await _build_external_seed_product(req=req, seed_row=seed_row)
            if prod:
                products.append(prod)
        except Exception:
            continue
    return products

# ============================================================================
# HEALTH CHECK
# ============================================================================

@router.get("/health")
async def health_check():
    """Health check endpoint"""
    try:
        await database.fetch_one("SELECT 1")
        db_status = "healthy"
    except:
        db_status = "unhealthy"
    
    return {
        "status": "ok",
        "timestamp": datetime.now().isoformat(),
        "version": "1.0.0",
        "services": {
            "database": db_status,
            "api": "operational"
        }
    }

# ============================================================================
# AUTHENTICATION
# ============================================================================

class AgentAuthRequest(BaseModel):
    agent_name: str
    agent_email: str
    company: Optional[str] = "Independent"
    description: Optional[str] = None

@router.post("/auth")
async def generate_api_key(request: AgentAuthRequest):
    """Generate or retrieve agent API key"""
    try:
        # Check if agent exists
        check_query = "SELECT agent_id, api_key FROM agents WHERE email = :email"
        existing = await database.fetch_one(check_query, {"email": request.agent_email})
        
        if existing:
            return {
                "status": "success",
                "message": "Agent already exists",
                "agent_id": existing["agent_id"],
                "api_key": existing["api_key"],
                "rate_limit": {
                    "requests_per_minute": 1000,
                    "tier": "standard"
                }
            }
        
        # Generate new agent
        agent_id = f"agent_{secrets.token_hex(8)}"
        api_key = f"ak_live_{secrets.token_hex(32)}"
        
        # Insert with full schema
        insert_query = """
            INSERT INTO agents (
                agent_id, name, email, company, use_case, api_key, 
                status, created_at, request_count, success_rate, rate_limit
            )
            VALUES (
                :agent_id, :name, :email, :company, :use_case, :api_key,
                :status, :created_at, :request_count, :success_rate, :rate_limit
            )
        """
        
        await database.execute(insert_query, {
            "agent_id": agent_id,
            "name": request.agent_name,
            "email": request.agent_email,
            "company": request.company,
            "use_case": request.description or "General API access",
            "api_key": api_key,
            "status": "active",
            "created_at": datetime.now(),
            "request_count": 0,
            "success_rate": 0,
            "rate_limit": 1000
        })
        
        return {
            "status": "success",
            "message": "API key generated successfully",
            "agent_id": agent_id,
            "api_key": api_key,
            "rate_limit": {
                "requests_per_minute": 1000,
                "tier": "standard"
            }
        }
    
    except Exception as e:
        logger.error(f"Failed to generate API key: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to generate API key: {str(e)}")

# ============================================================================
# MERCHANTS
# ============================================================================

@router.get("/merchants")
async def list_merchants(
    status: str = "active",
    limit: int = Query(default=50, le=100),
    offset: int = Query(default=0, ge=0),
    context: AgentContext = Depends(get_agent_context)
):
    """List connected merchants - FIXED to only query existing columns"""
    try:
        # Map status values
        if status == "active":
            db_status = "approved"
        else:
            db_status = status
        
        # Query merchants this agent has interacted with (via orders)
        # Only show merchants where agent has created orders
        query = """
            SELECT DISTINCT
                mo.merchant_id, 
                mo.business_name, 
                mo.status,
                mo.store_url,
                mo.website,
                mo.region,
                mo.contact_email,
                COALESCE(mo.psp_connected, CASE WHEN EXISTS (
                    SELECT 1 FROM merchant_psps mp 
                    WHERE mp.merchant_id = mo.merchant_id AND mp.status = 'active'
                ) THEN true ELSE false END) AS psp_connected,
                COALESCE(mo.psp_type, (
                    SELECT mp.provider FROM merchant_psps mp 
                    WHERE mp.merchant_id = mo.merchant_id AND mp.status = 'active'
                    ORDER BY mp.connected_at DESC LIMIT 1
                )) AS psp_type,
                mo.created_at,
                COUNT(o.order_id) as total_orders,
                SUM(CASE WHEN o.payment_status = 'paid' THEN o.total ELSE 0 END) as total_gmv
            FROM merchant_onboarding mo
            INNER JOIN orders o ON o.merchant_id = mo.merchant_id
            WHERE mo.status = :status
            AND mo.status != 'deleted'
            AND o.agent_id = :agent_id
            AND (o.is_deleted IS NULL OR o.is_deleted = FALSE)
            GROUP BY mo.merchant_id, mo.business_name, mo.status, mo.store_url, 
                     mo.website, mo.region, mo.contact_email, mo.psp_connected, 
                     mo.psp_type, mo.created_at
            ORDER BY total_orders DESC, mo.business_name
            LIMIT :limit OFFSET :offset
        """
        
        merchants = await database.fetch_all(query, {
            "status": db_status,
            "agent_id": context.agent_id,
            "limit": limit,
            "offset": offset
        })
        
        # Get total count (only merchants this agent has ordered from)
        count_query = """
            SELECT COUNT(DISTINCT mo.merchant_id) as total
            FROM merchant_onboarding mo
            INNER JOIN orders o ON o.merchant_id = mo.merchant_id
            WHERE mo.status = :status
            AND mo.status != 'deleted'
            AND o.agent_id = :agent_id
            AND (o.is_deleted IS NULL OR o.is_deleted = FALSE)
        """
        total_result = await database.fetch_one(count_query, {
            "status": db_status,
            "agent_id": context.agent_id
        })
        
        return {
            "status": "success",
            "merchants": [
                {
                    "merchant_id": m["merchant_id"],
                    "business_name": m["business_name"],
                    "status": "active" if m["status"] == "approved" else m["status"],
                    "store_url": m["store_url"],
                    "website": m["website"],
                    "region": m["region"],
                    "contact_email": m["contact_email"],
                    "psp_connected": m["psp_connected"],
                    "psp_type": m["psp_type"],
                    "created_at": m["created_at"].isoformat() if m["created_at"] else None,
                    "total_orders": m["total_orders"],
                    "total_gmv": float(m["total_gmv"]) if m["total_gmv"] else 0
                }
                for m in merchants
            ],
            "pagination": {
                "total": total_result["total"] if total_result else 0,
                "limit": limit,
                "offset": offset,
                "has_more": (total_result["total"] if total_result else 0) > offset + limit
            }
        }
    
    except Exception as e:
        logger.error(f"Failed to list merchants: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to list merchants: {str(e)}")

# ============================================================================
# PRODUCTS SEARCH
# ============================================================================

@router.get("/products/search")
async def search_products(
    req: Request,
    merchant_id: Optional[str] = None,
    query: Optional[str] = None,
    category: Optional[str] = None,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    in_stock: Optional[bool] = None,
    limit: int = Query(default=20, le=100),
    offset: int = Query(default=0, ge=0),
    context: AgentContext = Depends(get_agent_context)
):
    """Search products - supports cross-merchant search with quality-aware ranking"""
    try:
        logger.info("agent_sdk_fixed_search_entry")

        if merchant_id == EXTERNAL_SEED_MERCHANT_ID:
            external_products = await _load_external_seed_products_for_search(
                req=req,
                query=query,
                limit=limit,
                offset=offset,
            )
            return {
                "status": "success",
                "products": external_products,
                "pagination": {
                    "total": offset + len(external_products),
                    "limit": limit,
                    "offset": offset,
                    "has_more": len(external_products) == limit,
                },
            }

        # Build WHERE clauses
        where_clauses = []
        params = {"limit": limit, "offset": offset}
        
        if merchant_id:
            # Verify access to merchant
            merchant_check = await database.fetch_one(
                "SELECT merchant_id FROM merchant_onboarding WHERE merchant_id = :mid AND status != 'deleted'",
                {"mid": merchant_id}
            )
            if not merchant_check:
                raise HTTPException(status_code=404, detail="Merchant not found")
            
            # Qualify to avoid ambiguity between products_cache p and merchant_onboarding m
            where_clauses.append("p.merchant_id = :merchant_id")
            params["merchant_id"] = merchant_id
        
        if query:
            # Search in JSON fields using PostgreSQL JSON operators
            where_clauses.append("(LOWER(p.product_data->>'name') LIKE :query OR LOWER(p.product_data->>'description') LIKE :query)")
            params["query"] = f"%{query.lower()}%"
        
        if category:
            where_clauses.append("LOWER(p.product_data->>'category') = :category")
            params["category"] = category.lower()
        
        if min_price is not None:
            where_clauses.append("(p.product_data->>'price')::numeric >= :min_price")
            params["min_price"] = min_price
        
        if max_price is not None:
            where_clauses.append("(p.product_data->>'price')::numeric <= :max_price")
            params["max_price"] = max_price
        
        if in_stock is not None:
            where_clauses.append("(p.product_data->>'in_stock')::boolean = :in_stock")
            params["in_stock"] = in_stock
        
        # Build query
        where_clause = " AND ".join(where_clauses) if where_clauses else "1=1"
        
        # Get products from cache
        query_str = f"""
            SELECT 
                p.id,
                p.merchant_id,
                p.platform,
                p.platform_product_id,
                p.product_data,
                p.cached_at,
                m.business_name as merchant_name
            FROM products_cache p
            JOIN merchant_onboarding m ON p.merchant_id = m.merchant_id
            WHERE {where_clause}
            AND m.status != 'deleted'
            AND p.cache_status != 'expired'
            ORDER BY p.cached_at DESC
            LIMIT :limit OFFSET :offset
        """
        
        products = await database.fetch_all(query_str, params)
        
        # Get total count
        count_query = f"""
            SELECT COUNT(*) as total
            FROM products_cache p
            JOIN merchant_onboarding m ON p.merchant_id = m.merchant_id
            WHERE {where_clause}
            AND m.status != 'deleted'
        """
        
        # Remove limit and offset from params for count query
        count_params = {k: v for k, v in params.items() if k not in ['limit', 'offset']}
        total_result = await database.fetch_one(count_query, count_params)
        
        # Extract product data from JSON and calculate relevance + ranking features
        ranking_config = get_agent_ranking_config()
        ranked_candidates: List[Dict[str, Any]] = []

        for p in products:
            try:
                # Convert Row to dict safely
                p_dict = dict(p)
                # Extract product data from JSON column (might be string or dict)
                product_data_raw = p_dict.get("product_data")
                if isinstance(product_data_raw, str):
                    product_info = json.loads(product_data_raw)
                elif isinstance(product_data_raw, dict):
                    product_info = product_data_raw
                else:
                    product_info = {}
                
                # Build response object - merge product_info with metadata
                product_dict = {
                    **product_info,  # Spread all product fields
                    "platform_product_id": p_dict["platform_product_id"],
                    "merchant_id": p_dict["merchant_id"],
                    "merchant_name": p_dict.get("merchant_name"),
                    "platform": p_dict["platform"],
                    "cached_at": p_dict["cached_at"].isoformat() if p_dict.get("cached_at") else None
                }
                
                # Add relevance score
                if query:
                    score = 0
                    name_lower = str(product_dict.get("name", "")).lower()
                    desc_lower = str(product_dict.get("description", "")).lower()
                    query_lower = query.lower()
                    
                    if query_lower in name_lower:
                        score += 10
                    if name_lower.startswith(query_lower):
                        score += 5
                    if query_lower in desc_lower:
                        score += 3
                    
                    product_dict["relevance_score"] = score

                # Build ranking features and enrich with quality/enrichment data
                platform_product_id = str(
                    product_dict.get("platform_product_id")
                    or product_dict.get("id")
                    or product_dict.get("product_id")
                    or ""
                )
                if not platform_product_id:
                    continue

                features = AgentRankingFeatures(
                    merchant_id=product_dict.get("merchant_id"),
                    platform=product_dict.get("platform") or "unknown",
                    platform_product_id=platform_product_id,
                    rel_semantic=float(product_dict.get("relevance_score") or 1.0),
                    rel_keyword=float(product_dict.get("relevance_score") or 1.0),
                    rel_category_match=1.0
                    if category
                    and isinstance(product_dict.get("category"), str)
                    and category.lower()
                    in product_dict.get("category", "").lower()
                    else 0.0,
                )

                await hydrate_quality_and_enrichment(features)

                if not passes_agent_gating(features, ranking_config):
                    continue

                score = compute_agent_ranking_score(features, ranking_config)
                product_dict["ranking_score"] = score
                product_dict["ranking_features"] = serialize_features_for_log(
                    features, score
                )

                ranked_candidates.append(product_dict)
            except Exception as e:
                logger.error(f"Error processing product: {e}")
                continue

        # Sort by ranking score (fallback to relevance if needed)
        ranked_candidates.sort(
            key=lambda x: (
                x.get("ranking_score") is not None,
                x.get("ranking_score", x.get("relevance_score", 0)),
            ),
            reverse=True,
        )
        product_list = ranked_candidates

        if offset == 0:
            try:
                external_seed_limit = min(200, max(20, int(limit or 20) * 5))
                external_seed_products = await _load_external_seed_products_for_search(
                    req=req,
                    query=query,
                    limit=external_seed_limit,
                    offset=0,
                )
                if external_seed_products:
                    query_lower = str(query or "").strip().lower()
                    existing_ids = {
                        str(p.get("product_id") or p.get("id") or "") for p in ranked_candidates
                    }
                    for p in external_seed_products:
                        pid = str(p.get("product_id") or p.get("id") or "")
                        if not pid or pid in existing_ids:
                            continue
                        rel = 1.0
                        if query_lower:
                            title = str(p.get("title") or "").lower()
                            desc = str(p.get("description") or "").lower()
                            domain = str(p.get("external_domain") or "").lower()
                            ext_url = str(p.get("external_url") or "").lower()
                            brand = str(p.get("brand") or "").lower()
                            haystack = " ".join([title, desc, brand, domain, ext_url]).strip()
                            if query_lower in title:
                                rel = 1.0 if title == query_lower else 0.9
                            elif query_lower in desc:
                                rel = 0.7
                            elif query_lower in haystack:
                                rel = 0.65
                            else:
                                words = [w for w in query_lower.split() if w]
                                if words:
                                    matches = sum(1 for w in words if w in haystack)
                                    if matches > 0:
                                        rel = 0.5 + (matches / len(words)) * 0.3
                                    else:
                                        continue
                                else:
                                    continue

                        p["relevance_score"] = rel
                        p["ranking_score"] = float(rel) * 0.8
                        p["ranking_features"] = {"source": "external_seed"}
                        ranked_candidates.append(p)
                        existing_ids.add(pid)

                    ranked_candidates.sort(
                        key=lambda x: (
                            x.get("ranking_score") is not None,
                            x.get("ranking_score", x.get("relevance_score", 0)),
                        ),
                        reverse=True,
                    )
                    product_list = ranked_candidates[:limit]
            except Exception as e:
                logger.debug(f"Failed to load external seed products: {e}", exc_info=True)
        
        response = {
            "status": "success",
            "products": product_list,
            "pagination": {
                "total": total_result["total"] if total_result else 0,
                "limit": limit,
                "offset": offset,
                "has_more": (total_result["total"] if total_result else 0) > offset + limit
            }
        }
        # Persist ranking features for LTR / reranker training (best-effort).
        try:
            await log_ranking_batch(
                agent_id=getattr(context, "agent_id", None),
                endpoint="/agent/v1/products/search",
                query=query,
                products=product_list,
                max_rows=50,
            )
        except Exception as e:
            logger.debug(f"Failed to log agent ranking batch: {e}", exc_info=True)

        # Log impression events for the returned products (top N).
        try:
            events = []
            for idx, p in enumerate(product_list[:50]):
                feat = (p.get("ranking_features") or {}) if isinstance(
                    p.get("ranking_features"), dict
                ) else {}
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
                            p.get("platform_product_id")
                            or p.get("product_id")
                            or p.get("id")
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
            logger.debug(f"Failed to log agent product events: {e}", exc_info=True)

        return response
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to search products: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to search products: {str(e)}")

# ============================================================================
# ORDERS
# ============================================================================

@router.get("/sdk/orders", deprecated=True)
async def list_orders(
    merchant_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = Query(default=50, le=100),
    offset: int = Query(default=0, ge=0),
    context: AgentContext = Depends(get_agent_context)
):
    """
    List orders (SDK legacy).

    Deprecated: use `/agent/v1/orders` from `routes/agent_api.py` instead.
    """
    try:
        # Build WHERE clauses
        # Always scope to current agent
        where_clauses = ["o.agent_id = :agent_id"]
        params = {"limit": limit, "offset": offset, "agent_id": context.agent_id}
        
        if merchant_id:
            where_clauses.append("o.merchant_id = :merchant_id")
            params["merchant_id"] = merchant_id
        
        if status:
            where_clauses.append("o.status = :status")
            params["status"] = status
        
        where_clause = " AND ".join(where_clauses) if where_clauses else "1=1"
        
        # Get orders
        query = f"""
            SELECT 
                o.*,
                m.business_name as merchant_name
            FROM orders o
            JOIN merchant_onboarding m ON o.merchant_id = m.merchant_id
            WHERE {where_clause}
            AND m.status != 'deleted'
            ORDER BY o.created_at DESC
            LIMIT :limit OFFSET :offset
        """
        
        orders = await database.fetch_all(query, params)
        
        return {
            "status": "success",
            # Avoid returning sensitive fields (e.g. client_secret, raw metadata, full addresses)
            # from this legacy listing endpoint. Use /agent/v1/orders/{order_id} for details.
            "orders": [
                {
                    "order_id": o.get("order_id"),
                    "merchant_id": o.get("merchant_id"),
                    "merchant_name": o.get("merchant_name"),
                    "status": o.get("status"),
                    "payment_status": o.get("payment_status"),
                    "fulfillment_status": o.get("fulfillment_status"),
                    "total": str(o.get("total")) if o.get("total") is not None else None,
                    "currency": o.get("currency"),
                    "created_at": o.get("created_at"),
                    "updated_at": o.get("updated_at"),
                }
                for o in orders
            ],
            "pagination": {
                "limit": limit,
                "offset": offset,
                "has_more": len(orders) == limit
            }
        }
    
    except Exception as e:
        logger.error(f"Failed to list orders: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to list orders: {str(e)}")

# ============================================================================
# OpenAPI SPEC
# ============================================================================

@router.get("/openapi.json")
async def get_openapi_spec():
    """Return OpenAPI specification for SDK generation"""
    return {
        "openapi": "3.0.0",
        "info": {
            "title": "Pivota Agent API",
            "version": "1.0.0",
            "description": "Production-ready API for agent integrations"
        },
        "servers": [
            {"url": "https://api.pivota.com/agent/v1"}
        ],
        "security": [
            {"ApiKeyAuth": []}
        ],
        "components": {
            "securitySchemes": {
                "ApiKeyAuth": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "X-API-Key"
                }
            }
        },
        "paths": {
            "/health": {
                "get": {
                    "summary": "Health Check",
                    "responses": {
                        "200": {"description": "API is healthy"}
                    }
                }
            },
            "/auth": {
                "post": {
                    "summary": "Generate API Key",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["agent_name", "agent_email"],
                                    "properties": {
                                        "agent_name": {"type": "string"},
                                        "agent_email": {"type": "string"},
                                        "company": {"type": "string"},
                                        "description": {"type": "string"}
                                    }
                                }
                            }
                        }
                    },
                    "responses": {
                        "200": {"description": "API key generated"}
                    }
                }
            },
            "/merchants": {
                "get": {
                    "summary": "List Merchants",
                    "parameters": [
                        {"name": "status", "in": "query", "schema": {"type": "string"}},
                        {"name": "limit", "in": "query", "schema": {"type": "integer"}},
                        {"name": "offset", "in": "query", "schema": {"type": "integer"}}
                    ],
                    "responses": {
                        "200": {"description": "List of merchants"}
                    }
                }
            },
            "/products/search": {
                "get": {
                    "summary": "Search Products",
                    "parameters": [
                        {"name": "merchant_id", "in": "query", "schema": {"type": "string"}},
                        {"name": "query", "in": "query", "schema": {"type": "string"}},
                        {"name": "category", "in": "query", "schema": {"type": "string"}},
                        {"name": "min_price", "in": "query", "schema": {"type": "number"}},
                        {"name": "max_price", "in": "query", "schema": {"type": "number"}},
                        {"name": "limit", "in": "query", "schema": {"type": "integer"}},
                        {"name": "offset", "in": "query", "schema": {"type": "integer"}}
                    ],
                    "responses": {
                        "200": {"description": "Search results"}
                    }
                }
            },
            "/payments": {
                "post": {
                    "summary": "Create Payment",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["order_id", "payment_method"],
                                    "properties": {
                                        "order_id": {"type": "string"},
                                        "payment_method": {"type": "object"},
                                        "return_url": {"type": "string"},
                                        "idempotency_key": {"type": "string"}
                                    }
                                }
                            }
                        }
                    },
                    "responses": {
                        "200": {"description": "Payment created"}
                    }
                }
            }
        }
    }
