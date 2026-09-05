"""
SDK-Ready Agent API Endpoints - COMPREHENSIVE FIX
Properly handles all database schema issues and edge cases
"""
from fastapi import APIRouter, Depends, HTTPException, Query, Request, BackgroundTasks
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import datetime
from db.database import database
from routes.agent_auth import AgentContext, get_agent_context
from routes.agent_docs import build_agent_openapi_schema
from utils.logger import logger
import secrets
import json
import re
import os
import time
import asyncio
import uuid

from services.seed_variant_options import normalize_seed_variant_options
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
    apply_utm,
    get_allowed_domains_for_market,
    is_destination_domain_allowed,
    make_redirect_token,
)
from services.external_seed_search import (
    dedupe_external_seed_rows,
    fetch_external_seed_rows,
)
from services.external_referral_readiness import should_block_external_referral_runtime
from db.agent_ranking_log import log_ranking_batch
from db.agent_product_events import log_product_events

router = APIRouter(prefix="/agent/v1", tags=["agent-sdk"])

EXTERNAL_SEED_MERCHANT_ID = "external_seed"
DEFAULT_EXTERNAL_SEED_MARKET = "US"


def _env_float(name: str, default: float, *, min_value: float, max_value: float) -> float:
    raw = (os.getenv(name) or "").strip()
    try:
        value = float(raw) if raw else default
    except Exception:
        value = default
    return max(min_value, min(max_value, value))


AGENT_SDK_FIXED_DELEGATE_TIMEOUT_SECONDS = _env_float(
    "AGENT_SDK_FIXED_DELEGATE_TIMEOUT_SECONDS",
    7.0,
    min_value=0.5,
    max_value=30.0,
)
AGENT_SDK_FIXED_SCOPED_DELEGATE_TIMEOUT_SECONDS = _env_float(
    "AGENT_SDK_FIXED_SCOPED_DELEGATE_TIMEOUT_SECONDS",
    4.0,
    min_value=0.5,
    max_value=30.0,
)
FIND_PRODUCTS_MULTI_SEED_BUDGET_MS = max(
    0,
    min(
        5000,
        int(os.getenv("FIND_PRODUCTS_MULTI_SEED_BUDGET_MS", "400") or 400),
    ),
)
FIND_PRODUCTS_MULTI_SEED_BUILD_CONCURRENCY = max(
    1,
    min(
        32,
        int(os.getenv("FIND_PRODUCTS_MULTI_SEED_BUILD_CONCURRENCY", "8") or 8),
    ),
)
AGENT_SDK_FIXED_EXTERNAL_SEED_BUDGET_MS = int(
    _env_float(
        "AGENT_SDK_FIXED_EXTERNAL_SEED_BUDGET_MS",
        float(FIND_PRODUCTS_MULTI_SEED_BUDGET_MS or 400),
        min_value=100.0,
        max_value=5000.0,
    )
)
AGENT_SDK_FIXED_EXTERNAL_SEED_BUILD_CONCURRENCY = int(
    _env_float(
        "AGENT_SDK_FIXED_EXTERNAL_SEED_BUILD_CONCURRENCY",
        float(FIND_PRODUCTS_MULTI_SEED_BUILD_CONCURRENCY or 8),
        min_value=1.0,
        max_value=64.0,
    )
)
AGENT_SDK_FIXED_EXTERNAL_SEED_QUERY_TIMEOUT_SECONDS = _env_float(
    "AGENT_SDK_FIXED_EXTERNAL_SEED_QUERY_TIMEOUT_SECONDS",
    0.35,
    min_value=0.05,
    max_value=5.0,
)
AGENT_SDK_FIXED_SEARCH_LIMIT_MAX = int(
    _env_float(
        "AGENT_SEARCH_LIMIT_MAX",
        200.0,
        min_value=1.0,
        max_value=200.0,
    )
)

def _resolve_delegate_timeout_seconds(merchant_id: Optional[str]) -> float:
    return (
        float(AGENT_SDK_FIXED_SCOPED_DELEGATE_TIMEOUT_SECONDS)
        if merchant_id
        else float(AGENT_SDK_FIXED_DELEGATE_TIMEOUT_SECONDS)
    )


async def _await_with_hard_timeout(coro: Any, timeout_seconds: float) -> Any:
    timeout_s = max(0.1, float(timeout_seconds or 0.1))
    task = asyncio.create_task(coro)
    done, _pending = await asyncio.wait({task}, timeout=timeout_s)
    if task in done:
        return await task
    task.cancel()
    raise asyncio.TimeoutError()


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


def _seed_image_urls(seed_data: Dict[str, Any]) -> List[str]:
    raw = seed_data.get("image_urls")
    if not isinstance(raw, list) or not raw:
        raw = seed_data.get("images")
    if not isinstance(raw, list) or not raw:
        snapshot = seed_data.get("snapshot")
        if isinstance(snapshot, dict):
            raw = snapshot.get("image_urls") or snapshot.get("images")

    if not isinstance(raw, list):
        return []

    urls: List[str] = []
    seen: set[str] = set()
    for item in raw:
        url = None
        if isinstance(item, str):
            url = item.strip()
        elif isinstance(item, dict):
            raw_url = item.get("url") or item.get("image_url")
            url = str(raw_url).strip() if isinstance(raw_url, str) else None
        if not url or not url.startswith(("http://", "https://")):
            continue
        if url in seen:
            continue
        seen.add(url)
        urls.append(url)
        if len(urls) >= 20:
            break
    return urls


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
    allowed_domains: Optional[List[str]] = None,
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
    image_urls = _seed_image_urls(seed_data)
    image_url = seed_data.get("image_url") or seed_row.get("image_url") or (image_urls[0] if image_urls else None)

    external_product_id = (
        str(seed_row.get("external_product_id") or "").strip()
        or str(seed_data.get("external_product_id") or "").strip()
        or _stable_external_product_id(canonical_url or destination_url)
    )
    if not external_product_id:
        return None

    blocked, _gate_status = await should_block_external_referral_runtime(
        seed_row,
        matched_via="agent_sdk_fixed",
        allowed_domains=allowed_domains,
    )
    if blocked:
        return None

    disclosure_text = seed_row.get("disclosure_text") or seed_data.get("disclosure_text") or DEFAULT_DISCLOSURE_TEXT
    utm_template = seed_row.get("utm_template") or seed_data.get("utm_template") or DEFAULT_UTM_TEMPLATE

    dest_with_utm = apply_utm(destination_url, utm_template, {"market": market, "tool": tool})
    if allowed_domains is None:
        allowed_domains = await get_allowed_domains_for_market(market=market)
    if not is_destination_domain_allowed(
        destination_url=dest_with_utm,
        allowed_domains=allowed_domains,
    ):
        return None

    external_domain = str(seed_row.get("domain") or "").strip() or None
    if not external_domain:
        try:
            from urllib.parse import urlparse

            external_domain = urlparse(canonical_url or destination_url).hostname or None
        except Exception:
            external_domain = None

    # A-F1.1 (funnel plan): stable click identity + referral carriers — same
    # fix as the agent_api builder; see the comment there. Without this, /r
    # clicks from this surface mint throwaway ids with NULL merchant and the
    # dest lacks utm_content (the WC order-side join key).
    from services.commerce_attribution_service import (
        PVT_CLICK_ID,
        PVT_SURFACE,
        new_click_id,
        normalize_surface,
    )
    from services.outbound_links_service import append_referral_click_param
    from services.seller_identity import anchor_merchant_from_product_key

    stable_click_id = new_click_id()
    dest_with_utm = append_referral_click_param(dest_with_utm, stable_click_id)

    # Canonical double-colon extractor (see the agent_api builder for why an
    # inline pipe parse silently dropped the anchor for every real seed).
    _anchor_merchant_id = anchor_merchant_from_product_key(
        seed_row.get("attached_product_key")
    )
    _seller_ref = str(seed_row.get("seller_ref") or seed_data.get("seller_ref") or "").strip() or None
    _seed_kind = str(seed_row.get("seed_kind") or seed_data.get("seed_kind") or "").strip() or None

    _redirect_ctx: Dict[str, Any] = {
        "source": "external_seed",
        "external_seed_id": seed_id,
        "external_product_id": external_product_id,
        PVT_CLICK_ID: stable_click_id,
        PVT_SURFACE: normalize_surface(tool),
        "tool": tool,
        "join_mode": "referral_only",
    }
    if _anchor_merchant_id:
        _redirect_ctx["merchant_id"] = _anchor_merchant_id
    if _seller_ref:
        _redirect_ctx["seller_ref"] = _seller_ref
    if _seed_kind:
        _redirect_ctx["seed_kind"] = _seed_kind

    token = make_redirect_token(
        {
            "market": market,
            "tool": tool,
            "dest": dest_with_utm,
            "ctx": _redirect_ctx,
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
        image_url = v.get("image_url") or v.get("image")
        if isinstance(image_url, str):
            image_url = image_url.strip() or None
        else:
            image_url = None

        # The axis, carried through. This builder whitelists variant fields, so
        # an axis written into the seed reached here and stopped. It is read
        # through the shared normalizer because the column holds TWO shapes —
        # a list of pairs from the enrichment lane, a {name: value} mapping from
        # the employee CSV lane — and a list-only reader silently discards the
        # axis the CSV lane already had.
        options = normalize_seed_variant_options(v.get("options"))

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
                **({"image_url": image_url} if image_url else {}),
                **({"options": options} if options else {}),
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
        "image_urls": image_urls,
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
    build_budget_ms: Optional[int] = None,
    build_concurrency: Optional[int] = None,
    include_seed_data_text_match: bool = False,
    metrics_out: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    metrics = metrics_out if isinstance(metrics_out, dict) else None
    if metrics is not None:
        metrics.setdefault("executed", True)
        metrics.setdefault("skip_reason", None)
        metrics.setdefault("query_ms", 0)
        metrics.setdefault("build_ms", 0)
        metrics.setdefault("query_timeout", False)
        metrics.setdefault("rows_fetched", 0)
        metrics.setdefault("rows_built", 0)
        metrics.setdefault("budget_exhausted", False)

    fetch_result = await fetch_external_seed_rows(
        database=database,
        market=DEFAULT_EXTERNAL_SEED_MARKET,
        query=query,
        limit=limit,
        offset=offset,
        include_seed_data_text_match=include_seed_data_text_match,
        query_timeout_seconds=float(AGENT_SDK_FIXED_EXTERNAL_SEED_QUERY_TIMEOUT_SECONDS or 0.35),
    )
    rows = fetch_result.get("rows") or []
    if metrics is not None:
        metrics["query_timeout"] = bool(fetch_result.get("query_timeout") or False)
        metrics["query_ms"] = max(0, int(fetch_result.get("query_ms") or 0))
        metrics["rows_fetched"] = len(rows)
        if metrics["query_timeout"]:
            metrics["skip_reason"] = "query_timeout"
        if fetch_result.get("table_missing"):
            metrics["rows_built"] = 0
            metrics["build_ms"] = 0
            metrics["budget_exhausted"] = False
            metrics["skip_reason"] = "seed_table_missing"
    if fetch_result.get("table_missing"):
        return []

    candidate_rows = dedupe_external_seed_rows(rows, limit=limit)
    if not candidate_rows:
        if metrics is not None:
            metrics["rows_built"] = 0
            metrics["build_ms"] = 0
            metrics["budget_exhausted"] = False
            if not str(metrics.get("skip_reason") or "").strip():
                metrics["skip_reason"] = "no_seed_candidates"
        return []

    allowlist_by_market: Dict[str, List[str]] = {}
    build_started = time.perf_counter()
    for seed_row in candidate_rows:
        seed_market = str(seed_row.get("market") or DEFAULT_EXTERNAL_SEED_MARKET).strip().upper() or DEFAULT_EXTERNAL_SEED_MARKET
        if seed_market in allowlist_by_market:
            continue
        allowlist_by_market[seed_market] = await get_allowed_domains_for_market(market=seed_market)

    concurrency = max(
        1,
        int(build_concurrency or AGENT_SDK_FIXED_EXTERNAL_SEED_BUILD_CONCURRENCY),
    )
    deadline = None
    budget_ms = int(build_budget_ms) if build_budget_ms is not None else AGENT_SDK_FIXED_EXTERNAL_SEED_BUDGET_MS
    if budget_ms > 0:
        deadline = time.perf_counter() + (budget_ms / 1000.0)
    budget_exhausted = False

    semaphore = asyncio.Semaphore(concurrency)

    async def _build_guarded(seed_row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        async with semaphore:
            if deadline is not None and time.perf_counter() >= deadline:
                return None
            try:
                seed_market = str(seed_row.get("market") or DEFAULT_EXTERNAL_SEED_MARKET).strip().upper() or DEFAULT_EXTERNAL_SEED_MARKET
                allowed_domains = allowlist_by_market.get(seed_market, [])
                return await _build_external_seed_product(
                    req=req,
                    seed_row=seed_row,
                    allowed_domains=allowed_domains,
                )
            except Exception:
                return None

    tasks = [asyncio.create_task(_build_guarded(seed_row)) for seed_row in candidate_rows]
    products: List[Dict[str, Any]] = []
    try:
        for task in asyncio.as_completed(tasks):
            if deadline is not None:
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    budget_exhausted = True
                    break
                try:
                    prod = await asyncio.wait_for(task, timeout=remaining)
                except asyncio.TimeoutError:
                    budget_exhausted = True
                    break
            else:
                prod = await task
            if prod:
                products.append(prod)
                if len(products) >= limit:
                    break
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    if metrics is not None:
        metrics["rows_built"] = len(products[:limit])
        metrics["build_ms"] = int((time.perf_counter() - build_started) * 1000)
        metrics["budget_exhausted"] = budget_exhausted
        if budget_exhausted and not products:
            metrics["skip_reason"] = "build_budget_exhausted"
    return products[:limit]

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
    background_tasks: BackgroundTasks,
    merchant_id: Optional[str] = None,
    merchant_ids: Optional[List[str]] = Query(None, description="List of merchant IDs to search"),
    search_all_merchants: bool = Query(
        default=False,
        description="Opt-in cross-merchant search",
    ),
    query: Optional[str] = None,
    category: Optional[str] = None,
    catalog_surface: Optional[str] = Query(
        default=None,
        description="Optional search surface. Set to 'beauty' for beauty-only candidate filtering.",
    ),
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    in_stock: Optional[bool] = None,
    in_stock_only: Optional[bool] = Query(None),
    limit: int = Query(default=20, ge=1),
    offset: int = Query(default=0, ge=0),
    allow_external_seed: bool = Query(default=True),
    allow_stale_cache: bool = Query(default=True),
    external_seed_strategy: str = Query(default="legacy"),
    fast_mode: bool = Query(default=False),
    context: AgentContext = Depends(get_agent_context)
):
    """
    Delegate to optimized `/agent/v1/products/search` implementation in `agent_api`.

    Reason:
    this router is registered earlier than `agent_api` in `main.py`, so this path
    is the effective handler in production. Delegation keeps route compatibility
    while using the lower-latency implementation.
    """
    from routes.agent_api import (
        _classify_db_reason_code,
        _expand_product_ref_aliases,
        _matches_catalog_surface,
        _normalize_catalog_surface,
        agent_search_products,
    )

    normalized_catalog_surface = _normalize_catalog_surface(catalog_surface)

    async def _legacy_scoped_search_fallback() -> Dict[str, Any]:
        # Merchant-scoped fallback to preserve historical SDK semantics and tests.
        fallback_started = time.perf_counter()
        try:
            params: Dict[str, Any] = {
                "merchant_id": merchant_id,
                "fetch_limit": int(limit) + 1,
                "offset": offset,
            }
            where_clauses: List[str] = [
                "p.merchant_id = :merchant_id",
                "(p.expires_at IS NULL OR p.expires_at > NOW())",
            ]

            merchant_check = await database.fetch_one(
                "SELECT merchant_id FROM merchant_onboarding WHERE merchant_id = :mid AND status != 'deleted'",
                {"mid": merchant_id},
            )
            if not merchant_check:
                latency_ms = int((time.perf_counter() - fallback_started) * 1000)
                return {
                    "status": "success",
                    "products": [],
                    "pagination": {"total": 0, "limit": limit, "offset": offset, "has_more": False},
                    "metadata": {
                        "reason_code": "no_candidates",
                        "source": "agent_sdk_fixed_legacy_fallback",
                        "latency_ms": latency_ms,
                    },
                }

            query_raw = str(query or "").strip()
            query_aliases = _expand_product_ref_aliases(query_raw)[:20] if query_raw else []
            is_id_like_query = bool(
                query_raw
                and (
                    query_raw.startswith(("http://", "https://", "gid://shopify/"))
                    or query_raw.isdigit()
                    or (
                        len(query_raw) >= 8
                        and bool(re.search(r"\d", query_raw))
                        and bool(re.fullmatch(r"[A-Za-z0-9:_/\-.]+", query_raw))
                    )
                )
            )
            if query_aliases and is_id_like_query:
                params["query_aliases"] = query_aliases
                where_clauses.append(
                    """
                    (
                        p.platform_product_id = ANY(:query_aliases)
                        OR p.product_data->>'id' = ANY(:query_aliases)
                        OR p.product_data->>'product_id' = ANY(:query_aliases)
                    )
                    """
                )
            elif query_raw:
                where_clauses.append(
                    """
                    (
                        LOWER(COALESCE(p.product_data->>'name', '')) LIKE :query
                        OR LOWER(COALESCE(p.product_data->>'title', '')) LIKE :query
                        OR LOWER(COALESCE(p.product_data->>'description', '')) LIKE :query
                        OR LOWER(COALESCE(p.product_data->>'vendor', '')) LIKE :query
                        OR LOWER(COALESCE(p.product_data->>'product_type', '')) LIKE :query
                        OR LOWER(COALESCE(p.product_data->>'sku', '')) LIKE :query
                    )
                    """
                )
                params["query"] = f"%{query_raw.lower()}%"

            if category:
                where_clauses.append(
                    """
                    (
                        LOWER(COALESCE(p.product_data->>'category', '')) = :category
                        OR LOWER(COALESCE(p.product_data->>'product_type', '')) = :category
                    )
                    """
                )
                params["category"] = category.lower()

            if min_price is not None:
                where_clauses.append(
                    "(NULLIF(p.product_data->>'price', '')::numeric >= :min_price)"
                )
                params["min_price"] = min_price

            if max_price is not None:
                where_clauses.append(
                    "(NULLIF(p.product_data->>'price', '')::numeric <= :max_price)"
                )
                params["max_price"] = max_price

            if effective_in_stock_only:
                where_clauses.append(
                    "(COALESCE(NULLIF(p.product_data->>'in_stock','')::boolean, true) = true)"
                )
            elif in_stock is not None:
                where_clauses.append(
                    "(COALESCE(NULLIF(p.product_data->>'in_stock','')::boolean, true) = :in_stock)"
                )
                params["in_stock"] = in_stock

            where_clause = " AND ".join(where_clauses)

            rows = await database.fetch_all(
                f"""
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
                  AND (p.cache_status IS NULL OR p.cache_status != 'expired')
                ORDER BY p.cached_at DESC
                LIMIT :fetch_limit OFFSET :offset
                """,
                params,
            )

            products: List[Dict[str, Any]] = []
            for row in rows or []:
                row_dict = dict(row) if isinstance(row, dict) else {}
                pdata = row_dict.get("product_data")
                if isinstance(pdata, str):
                    try:
                        pdata = json.loads(pdata)
                    except Exception:
                        pdata = {}
                if not isinstance(pdata, dict):
                    pdata = {}
                merged = {
                    **pdata,
                    "platform_product_id": row_dict.get("platform_product_id"),
                    "merchant_id": row_dict.get("merchant_id"),
                    "merchant_name": row_dict.get("merchant_name"),
                    "platform": row_dict.get("platform"),
                    "cached_at": row_dict.get("cached_at").isoformat()
                    if row_dict.get("cached_at")
                    else None,
                }
                if not merged.get("title") and merged.get("name"):
                    merged["title"] = merged.get("name")
                if not merged.get("name") and merged.get("title"):
                    merged["name"] = merged.get("title")
                if not _matches_catalog_surface(merged, normalized_catalog_surface):
                    continue
                products.append(merged)

            has_more = len(products) > int(limit)
            page_items = products[: int(limit)]
            total = int(offset) + len(page_items) + (1 if has_more else 0)
            reason_code = "ok" if page_items else "no_candidates"
            latency_ms = int((time.perf_counter() - fallback_started) * 1000)
            logger.info(
                "agent_sdk_fixed.legacy_search_fallback.summary",
                extra={
                    "event": "agent_sdk_fixed.legacy_search_fallback.summary",
                    "query": query,
                    "merchant_scope": merchant_id,
                    "latency_ms": latency_ms,
                    "reason_code": reason_code,
                    "result_count": len(page_items),
                },
            )
            return {
                "status": "success",
                "products": page_items,
                "pagination": {
                    "total": total,
                    "limit": limit,
                    "offset": offset,
                    "has_more": has_more,
                },
                "metadata": {
                    "reason_code": reason_code,
                    "source": "agent_sdk_fixed_legacy_fallback",
                    "catalog_surface": normalized_catalog_surface,
                    "latency_ms": latency_ms,
                },
            }
        except Exception as e:
            reason_code = _classify_db_reason_code(e)
            latency_ms = int((time.perf_counter() - fallback_started) * 1000)
            logger.warning(
                "agent_sdk_fixed legacy fallback failed",
                extra={
                    "event": "agent_sdk_fixed.legacy_search_fallback.failed",
                    "query": query,
                    "merchant_scope": merchant_id,
                    "latency_ms": latency_ms,
                    "reason_code": reason_code,
                    "error_type": type(e).__name__,
                },
            )
            return {
                "status": "success",
                "products": [],
                "pagination": {"total": 0, "limit": limit, "offset": offset, "has_more": False},
                "metadata": {
                    "reason_code": reason_code,
                    "source": "agent_sdk_fixed_legacy_fallback",
                    "catalog_surface": normalized_catalog_surface,
                    "latency_ms": latency_ms,
                    "error": type(e).__name__,
                },
            }

    started = time.perf_counter()
    # Contract: allow callers to request above 200, but clamp internally.
    limit = max(1, min(int(limit or 20), AGENT_SDK_FIXED_SEARCH_LIMIT_MAX))
    offset = max(0, int(offset or 0))
    effective_in_stock_only = (
        in_stock_only
        if in_stock_only is not None
        else in_stock
        if in_stock is not None
        else True
    )

    try:
        delegate_timeout_s = _resolve_delegate_timeout_seconds(merchant_id)
        result = await _await_with_hard_timeout(
            agent_search_products(
                req=req,
                background_tasks=background_tasks,
                merchant_id=merchant_id,
                merchant_ids=merchant_ids,
                search_all_merchants=search_all_merchants,
                query=query,
                category=category,
                catalog_surface=catalog_surface,
                min_price=min_price,
                max_price=max_price,
                in_stock_only=bool(effective_in_stock_only),
                limit=limit,
                offset=offset,
                allow_external_seed=allow_external_seed,
                allow_stale_cache=allow_stale_cache,
                external_seed_strategy=external_seed_strategy,
                fast_mode=fast_mode,
                context=context,
            ),
            delegate_timeout_s,
        )
    except asyncio.TimeoutError:
        if merchant_id:
            result = await _legacy_scoped_search_fallback()
        else:
            raise HTTPException(status_code=504, detail="Search timeout")
    except HTTPException as e:
        # Preserve backward compatibility for merchant-scoped SDK calls.
        if merchant_id and int(e.status_code) in (404, 500):
            result = await _legacy_scoped_search_fallback()
        else:
            raise
    except Exception:
        if merchant_id:
            result = await _legacy_scoped_search_fallback()
        else:
            raise

    if merchant_id and isinstance(result, dict):
        current_products = result.get("products")
        if isinstance(current_products, list) and not current_products:
            result = await _legacy_scoped_search_fallback()

    if isinstance(result, dict):
        metadata = result.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        metadata.setdefault("reason_code", "ok")
        metadata.setdefault("catalog_surface", normalized_catalog_surface)
        metadata["source"] = "agent_sdk_fixed_delegate"
        metadata["latency_ms"] = int((time.perf_counter() - started) * 1000)
        decision_id = str(uuid.uuid4())
        metadata["decision_id"] = decision_id
        metadata["decision_layer"] = {
            "decision_id": decision_id,
            "correlation_source": "agent_sdk_fixed.products.search",
        }
        result["metadata"] = metadata
        try:
            from services.agent_decision_event_store import (
                record_decision_candidates,
                record_decision_event,
                record_exposure_events,
            )
            from services.protocols import DEFAULT_PROTOCOL

            raw_products = [p for p in (result.get("products") or []) if isinstance(p, dict)]
            rows = []
            for idx, product in enumerate(raw_products):
                rows.append(
                    {
                        "content_key": product.get("content_key") or product.get("product_key"),
                        "catalog_offer_id": product.get("catalog_offer_id") or product.get("offer_id"),
                        "position": idx,
                        "eligibility_flags": {
                            "merchant_id": product.get("merchant_id"),
                            "in_stock": product.get("in_stock"),
                            "source": product.get("source"),
                            "ranking_score": product.get("ranking_score") or product.get("score"),
                        },
                        "slot": "search_result",
                    }
                )
            async def _record_search_decision() -> None:
                try:
                    await record_decision_event(
                        decision_id=decision_id,
                        merchant_id=merchant_id,
                        surface="agent_sdk_fixed.products.search",
                        channel=None,
                        protocol=DEFAULT_PROTOCOL,
                        agent_context={
                            "agent_id": getattr(context, "agent_id", None),
                            "session_id": getattr(context, "session_id", None),
                            "query": query,
                            "category": category,
                            "merchant_ids": merchant_ids,
                            "search_all_merchants": search_all_merchants,
                            "limit": limit,
                            "offset": offset,
                        },
                    )
                    await record_decision_candidates(decision_id, rows)
                    await record_exposure_events(decision_id, rows)
                except Exception:
                    logger.debug("agent_sdk_fixed decision event enqueue failed", exc_info=True)

            asyncio.create_task(_record_search_decision())
        except Exception:
            logger.debug("agent_sdk_fixed decision event scheduling failed", exc_info=True)
    return result


async def search_products_beauty(
    req: Request,
    background_tasks: BackgroundTasks,
    merchant_id: Optional[str] = None,
    merchant_ids: Optional[List[str]] = Query(None, description="List of merchant IDs to search"),
    search_all_merchants: bool = Query(
        default=False,
        description="Opt-in cross-merchant search",
    ),
    query: Optional[str] = None,
    category: Optional[str] = None,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    in_stock: Optional[bool] = None,
    in_stock_only: Optional[bool] = Query(None),
    limit: int = Query(default=20, ge=1),
    offset: int = Query(default=0, ge=0),
    allow_external_seed: bool = Query(default=True),
    allow_stale_cache: bool = Query(default=True),
    external_seed_strategy: str = Query(default="legacy"),
    fast_mode: bool = Query(default=False),
    context: AgentContext = Depends(get_agent_context),
):
    return await search_products(
        req=req,
        background_tasks=background_tasks,
        merchant_id=merchant_id,
        merchant_ids=merchant_ids,
        search_all_merchants=search_all_merchants,
        query=query,
        category=category,
        catalog_surface="beauty",
        min_price=min_price,
        max_price=max_price,
        in_stock=in_stock,
        in_stock_only=in_stock_only,
        limit=limit,
        offset=offset,
        allow_external_seed=allow_external_seed,
        allow_stale_cache=allow_stale_cache,
        external_seed_strategy=external_seed_strategy,
        fast_mode=fast_mode,
        context=context,
    )

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
async def get_openapi_spec(request: Request):
    """Return runtime-derived agent OpenAPI specification for SDK generation"""
    return build_agent_openapi_schema(request.app, base_url=str(request.base_url))
