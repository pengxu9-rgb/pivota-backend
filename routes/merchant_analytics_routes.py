"""
Merchant Analytics Routes
Time-series trends for merchant dashboard (GMV, orders, AOV, success rate, refunds)
Supports gross/net mode, base currency normalization, and 10-minute in-memory cache
"""

import logging
import os
import traceback
from datetime import datetime, timedelta, timezone, date
from typing import Dict, Any, List, Optional, Tuple
from functools import lru_cache
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import httpx

from db.database import database
from db.orders import orders
from readiness.summary import schedule_readiness_optimization_warmup
from services.commerce_interaction_service import trace_interaction
from services.merchant_commerce_diagnostics_service import build_merchant_commerce_funnel_issues
from services.merchant_commerce_funnel_service import (
    SUPPORTED_COMMERCE_FUNNEL_GROUP_BYS,
    get_merchant_commerce_funnel,
)
from services.merchant_commerce_readiness_service import upsert_merchant_commerce_readiness_state
from utils.auth import verify_jwt_token
from dashboard.core import UserRole

logger = logging.getLogger("merchant_analytics_routes")

router = APIRouter(prefix="/merchant", tags=["merchant-analytics"]) 
security = HTTPBearer()

# Version marker for deployment verification
ANALYTICS_VERSION = "v1.6.1-ROW-TO-DICT-FIX"

# Simple in-memory cache with TTL
TRENDS_CACHE: Dict[str, tuple[Dict[str, Any], float]] = {}
CACHE_TTL_SECONDS = 600  # 10 minutes

# Cache statistics
CACHE_HITS = 0
CACHE_MISSES = 0

# FX rates cache: {(from_currency, to_currency, date_str): (rate, timestamp)}
FX_CACHE: Dict[Tuple[str, str, str], Tuple[float, float]] = {}
FX_CACHE_TTL = 259200  # 3 days in seconds

# Global FX rates cache (latest rates as fallback): {(from_currency, to_currency): (rate, timestamp)}
_GLOBAL_FX_RATES: Dict[Tuple[str, str], Tuple[float, float]] = {}
GLOBAL_FX_TTL = 1209600  # 14 days in seconds

# Default base currency
DEFAULT_BASE_CURRENCY = os.getenv("DEFAULT_BASE_CURRENCY", "USD")


@router.get("/analytics/version")
async def get_version():
    """Get analytics API version for deployment verification"""
    return {"version": ANALYTICS_VERSION, "timestamp": datetime.now(timezone.utc).isoformat()}


async def _get_principal(
    credentials: HTTPAuthorizationCredentials = Depends(security)
) -> Dict[str, Any]:
    """Extract and validate JWT token, return principal data"""
    token = credentials.credentials
    try:
        principal = verify_jwt_token(token)
        return principal
    except Exception as e:
        logger.error(f"JWT verification failed: {e}")
        raise HTTPException(status_code=401, detail="Invalid token")


def _parse_window_bound(name: str, raw: Optional[str]) -> Optional[datetime]:
    """ISO-8601 -> aware UTC, or 422.

    A naive value is read as UTC. It is never read as process-local: asyncpg
    binds a naive datetime with the client process timezone, which would move
    the window by the deploy's offset without saying so.
    """
    text = str(raw or "").strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f"{name} must be an ISO-8601 datetime (e.g. 2026-06-01T00:00:00Z)",
        )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@router.get("/analytics/commerce-funnel")
async def get_commerce_funnel(
    surface: Optional[str] = Query(None),
    group_by: str = Query("product"),
    source_channel: Optional[str] = Query(None),
    source_family: Optional[str] = Query(None),
    protocol_name: Optional[str] = Query(None),
    agent_id: Optional[str] = Query(None),
    query_source: Optional[str] = Query(None),
    llm_provider: Optional[str] = Query(None),
    llm_model: Optional[str] = Query(None),
    commerce_surface: Optional[str] = Query(None),
    platform: Optional[str] = Query(None),
    store_id: Optional[str] = Query(None),
    since: Optional[str] = Query(
        None,
        description=(
            "ISO-8601 start of the canonical-event window (inclusive). "
            "Defaults to COMMERCE_FUNNEL_DEFAULT_WINDOW_DAYS before `until`; "
            "a span wider than COMMERCE_FUNNEL_MAX_WINDOW_DAYS is clamped and "
            "the response reports event_funnel.window.clamped=true."
        ),
    ),
    until: Optional[str] = Query(
        None, description="ISO-8601 end of the canonical-event window (inclusive). Defaults to now."
    ),
    principal: Dict[str, Any] = Depends(_get_principal),
):
    merchant_id = principal.get("merchant_id")
    if not merchant_id:
        raise HTTPException(status_code=400, detail="Missing merchant_id")
    parsed_since = _parse_window_bound("since", since)
    parsed_until = _parse_window_bound("until", until)
    if parsed_since is not None and parsed_until is not None and parsed_since > parsed_until:
        raise HTTPException(status_code=422, detail="since must not be after until")
    if group_by not in SUPPORTED_COMMERCE_FUNNEL_GROUP_BYS:
        raise HTTPException(
            status_code=400,
            detail=(
                "group_by must be one of "
                + ", ".join(sorted(SUPPORTED_COMMERCE_FUNNEL_GROUP_BYS))
            ),
        )
    return await get_merchant_commerce_funnel(
        merchant_id=str(merchant_id),
        surface=str(surface).strip().lower() if surface else None,
        group_by=group_by,
        source_channel=str(source_channel).strip().lower() if source_channel else None,
        source_family=str(source_family).strip().lower() if source_family else None,
        protocol_name=str(protocol_name).strip().lower() if protocol_name else None,
        agent_id=str(agent_id).strip() if agent_id else None,
        query_source=str(query_source).strip().lower() if query_source else None,
        llm_provider=str(llm_provider).strip().lower() if llm_provider else None,
        llm_model=str(llm_model).strip().lower() if llm_model else None,
        commerce_surface=str(commerce_surface).strip().lower() if commerce_surface else None,
        platform=(str(platform).strip().lower() or None) if platform else None,
        store_id=(str(store_id).strip() or None) if store_id else None,
        since=parsed_since,
        until=parsed_until,
    )


@router.get("/analytics/commerce-funnel/issues")
async def get_commerce_funnel_issues(
    surface: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    principal: Dict[str, Any] = Depends(_get_principal),
):
    merchant_id = principal.get("merchant_id")
    if not merchant_id:
        raise HTTPException(status_code=400, detail="Missing merchant_id")
    return await build_merchant_commerce_funnel_issues(
        merchant_id=str(merchant_id),
        surface=str(surface).strip().lower() if surface else None,
        limit=limit,
    )


@router.get("/analytics/commerce-interactions/{interaction_id}")
async def get_commerce_interaction_trace(
    interaction_id: str,
    principal: Dict[str, Any] = Depends(_get_principal),
):
    merchant_id = principal.get("merchant_id")
    if not merchant_id:
        raise HTTPException(status_code=400, detail="Missing merchant_id")
    trace = await trace_interaction(interaction_id)
    interaction = trace.get("interaction") or {}
    if str(interaction.get("merchant_id") or "").strip() != str(merchant_id):
        raise HTTPException(status_code=404, detail="Commerce interaction not found")
    return trace


@router.get("/analytics/readiness-state")
async def get_commerce_readiness_state(
    principal: Dict[str, Any] = Depends(_get_principal),
):
    merchant_id = principal.get("merchant_id")
    if not merchant_id:
        raise HTTPException(status_code=400, detail="Missing merchant_id")
    payload = await upsert_merchant_commerce_readiness_state(str(merchant_id))
    schedule_readiness_optimization_warmup(str(merchant_id))
    return payload


@router.get("/analytics/debug/query")
async def debug_analytics_query(
    range: str = Query("7d"),
    principal: Dict[str, Any] = Depends(_get_principal)
):
    """Debug endpoint to check what the analytics query returns"""
    try:
        merchant_id = principal.get("merchant_id")
        now = datetime.now(timezone.utc)
        delta = _parse_range(range)
        start = now - delta
        
        # Same query as trends endpoint
        base_query = """
            SELECT 
                order_id,
                total as amount,
                currency,
                payment_status as status,
                psp_used as psp,
                created_at
            FROM orders
            WHERE merchant_id = :merchant_id
              AND created_at >= :start_time
              AND created_at < :end_time
            LIMIT 5
        """
        params = {
            "merchant_id": merchant_id,
            "start_time": start,
            "end_time": now
        }
        
        rows = await database.fetch_all(base_query, params)
        
        return {
            "status": "success",
            "debug_info": {
                "merchant_id": merchant_id,
                "start_time": start.isoformat(),
                "end_time": now.isoformat(),
                "range": range,
                "query": base_query.strip(),
                "params": {k: str(v) for k, v in params.items()}
            },
            "rows_count": len(rows),
            "sample_rows": [dict(r) for r in rows]
        }
    except Exception as e:
        logger.exception(f"Debug query failed: {e}")
        return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}


@router.get("/analytics/debug/fx-rate")
async def debug_fx_rate(
    from_curr: str = Query("USD"),
    to_curr: str = Query("USD"),
    principal: Dict[str, Any] = Depends(_get_principal)
):
    """Test FX rate fetching"""
    try:
        test_date = datetime.now(timezone.utc).date()
        rate, source = await _get_rate(from_curr, to_curr, test_date)
        return {
            "status": "success",
            "from_currency": from_curr,
            "to_currency": to_curr,
            "date": test_date.isoformat(),
            "rate": rate,
            "source": source
        }
    except Exception as e:
        logger.exception(f"FX rate test failed: {e}")
        return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}


async def _get_latest_fx_rate(from_currency: str, to_currency: str) -> Optional[float]:
    """
    Get latest FX rate from exchangerate.host API with 14-day caching.
    Used as fallback when date-specific rate is unavailable.
    """
    if from_currency == to_currency:
        return 1.0
    
    cache_key = (from_currency.upper(), to_currency.upper())
    
    # Check global cache
    if cache_key in _GLOBAL_FX_RATES:
        rate, cached_time = _GLOBAL_FX_RATES[cache_key]
        if time.time() - cached_time < GLOBAL_FX_TTL:
            return rate
    
    # Fetch latest rate from API
    try:
        url = "https://api.exchangerate.host/latest"
        params = {
            "base": from_currency.upper(),
            "symbols": to_currency.upper()
        }
        
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json()
            
            if data.get("success") and "rates" in data:
                rate = data["rates"].get(to_currency.upper())
                if rate:
                    # Cache the rate globally
                    _GLOBAL_FX_RATES[cache_key] = (float(rate), time.time())
                    return float(rate)
    
    except Exception as e:
        logger.warning(f"Failed to fetch latest FX rate {from_currency}/{to_currency}: {e}")
    
    return None


async def _get_rate(from_currency: str, to_currency: str, date_obj: date) -> Tuple[Optional[float], str]:
    """
    Get FX rate with 3-tier fallback strategy.
    
    Returns: (rate, source) where source is one of:
    - "direct": same currency (1.0)
    - "date": date-specific rate from cache/API
    - "global": latest known rate from global cache
    - "latest": fetched latest rate as fallback
    - "assumed": 1:1 assumption (last resort)
    """
    from_curr = from_currency.upper()
    to_curr = to_currency.upper()
    
    # Tier 1: Same currency
    if from_curr == to_curr:
        return (1.0, "direct")
    
    date_str = date_obj.isoformat()
    cache_key = (from_curr, to_curr, date_str)
    
    # Tier 2: Date-specific rate (cache or API)
    if cache_key in FX_CACHE:
        rate, cached_time = FX_CACHE[cache_key]
        if time.time() - cached_time < FX_CACHE_TTL:
            return (rate, "date")
    
    # Try fetching date-specific rate
    try:
        url = f"https://api.exchangerate.host/{date_str}"
        params = {"base": from_curr, "symbols": to_curr}
        
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json()
            
            if data.get("success") and "rates" in data:
                rate = data["rates"].get(to_curr)
                if rate:
                    FX_CACHE[cache_key] = (float(rate), time.time())
                    return (float(rate), "date")
    except Exception as e:
        logger.debug(f"Date-specific FX fetch failed for {from_curr}/{to_curr} on {date_str}: {e}")
    
    # Tier 3: Global cache (last known rate)
    global_key = (from_curr, to_curr)
    if global_key in _GLOBAL_FX_RATES:
        rate, cached_time = _GLOBAL_FX_RATES[global_key]
        if time.time() - cached_time < GLOBAL_FX_TTL:
            return (rate, "global")
    
    # Tier 4: Fetch latest rate as fallback
    latest_rate = await _get_latest_fx_rate(from_curr, to_curr)
    if latest_rate:
        return (latest_rate, "latest")
    
    # Tier 5: Assume 1:1 (last resort)
    return (1.0, "assumed")


def _parse_range(range_str: str) -> timedelta:
    if range_str == "1d":
        return timedelta(days=1)
    if range_str == "7d":
        return timedelta(days=7)
    if range_str == "30d":
        return timedelta(days=30)
    if range_str == "90d":
        return timedelta(days=90)
    # default
    return timedelta(days=30)


def _floor_to_period(dt: datetime, interval: str) -> datetime:
    # Ensure dt is timezone-aware
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    
    if interval == "week":
        # Monday as start of week
        monday = dt - timedelta(days=dt.weekday())
        return datetime(monday.year, monday.month, monday.day, tzinfo=timezone.utc)
    # day
    return datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc)


def _is_success_status(status: Optional[str]) -> bool:
    """Check if payment_status indicates a successful payment"""
    if not status:
        return False
    s = status.lower()
    # orders table uses payment_status: 'paid', 'succeeded', 'completed'
    return s in {"paid", "succeeded", "completed", "success"}


def _is_refund_status(status: Optional[str]) -> bool:
    """Check if payment_status indicates a refund"""
    if not status:
        return False
    s = status.lower()
    return "refund" in s or s in {"refunded", "partial_refund", "partially_refunded"}


@router.get("/analytics/trends")
@router.get("/dashboard/trends")
async def get_trends(
    metric: str = Query("gmv", pattern="^(gmv|orders|aov|success_rate|refunds)$"),
    range: str = Query("30d", pattern="^(1d|7d|30d|90d)$"),
    interval: str = Query("day", pattern="^(day|week)$"),
    mode: str = Query("gross", pattern="^(gross|net)$"),
    psp: Optional[str] = Query(None),
    currency: Optional[str] = Query(None, description="Filter by currency (deprecated - use base_currency for normalization)"),
    compare: bool = Query(True),
    base_currency: Optional[str] = Query(None, description="Convert all amounts to this currency (e.g. USD, EUR)"),
    merchant_id: Optional[str] = Query(None, description="Admin/operator: override merchant context"),
    principal: Dict[str, Any] = Depends(_get_principal),
):
    """
    Return time-series trends for a merchant with currency normalization.

    - metric: gmv | orders | aov | success_rate | refunds
    - range: 1d | 7d | 30d | 90d
    - interval: day | week
    - mode: gross | net (net subtracts refunds from GMV/AOV)
    - base_currency: convert all amounts to this currency (default: USD)
    - psp: optional PSP filter (e.g. stripe, adyen)
    - currency: optional currency filter (for backwards compatibility)
    - compare: include previous period series
    """
    try:
        # Authorization: only merchants or admins/operators
        role = (principal.get("role") or "").lower()
        if role not in [UserRole.MERCHANT.value, UserRole.ADMIN.value, UserRole.OPERATOR.value]:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")

        # Merchant context
        if role == UserRole.MERCHANT.value:
            merchant_id = principal.get("entity_id") or principal.get("merchant_id") or merchant_id
        # Admin/operator must provide explicit merchant_id if not present in token
        if not merchant_id:
            raise HTTPException(status_code=400, detail="merchant_id required")

        # Determine base currency
        if not base_currency:
            base_currency = principal.get("base_currency") or DEFAULT_BASE_CURRENCY
        base_currency = base_currency.upper()

        # Check cache (include base_currency in key)
        cache_key = f"{merchant_id}|{metric}|{range}|{interval}|{psp or ''}|{currency or ''}|{compare}|{mode}|{base_currency}"
        if cache_key in TRENDS_CACHE:
            cached_data, cached_time = TRENDS_CACHE[cache_key]
            if time.time() - cached_time < CACHE_TTL_SECONDS:
                global CACHE_HITS
                CACHE_HITS += 1
                logger.info(f"Cache hit for {cache_key}")
                return cached_data
        
        # Cache miss
        global CACHE_MISSES
        CACHE_MISSES += 1

        # Time bounds
        now = datetime.now(timezone.utc)
        delta = _parse_range(range)
        start = now - delta
        prev_start = start - delta
        prev_end = start

        # Build base SQL with parameterized query (matching orders API pattern that works)
        base_query = """
            SELECT 
                order_id,
                total as amount,
                currency,
                payment_status as status,
                psp_used as psp,
                created_at
            FROM orders
            WHERE merchant_id = :merchant_id
              AND created_at >= :start_time
              AND created_at < :end_time
        """
        params = {
            "merchant_id": merchant_id,
            "start_time": start,
            "end_time": now
        }
        if psp:
            base_query += " AND psp_used = :psp"
            params["psp"] = psp
        if currency:
            base_query += " AND currency = :currency"
            params["currency"] = currency
        
        rows = await database.fetch_all(base_query, params)
        
        # Debug logging
        logger.info(f"Analytics query - merchant_id: {merchant_id}, rows fetched: {len(rows)}, start: {start}, end: {now}")
        if len(rows) > 0:
            logger.info(f"Sample row: {dict(rows[0])}")

        # If compare, fetch previous period too
        prev_rows = []
        if compare:
            prev_query = """
                SELECT 
                    order_id,
                    total as amount,
                    currency,
                    payment_status as status,
                    psp_used as psp,
                    created_at
                FROM orders
                WHERE merchant_id = :merchant_id
                  AND created_at >= :start_time
                  AND created_at < :end_time
            """
            prev_params = {
                "merchant_id": merchant_id,
                "start_time": prev_start,
                "end_time": prev_end
            }
            if psp:
                prev_query += " AND psp_used = :psp"
                prev_params["psp"] = psp
            if currency:
                prev_query += " AND currency = :currency"
                prev_params["currency"] = currency
            prev_rows = await database.fetch_all(prev_query, prev_params)

        def bucketize(data_rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
            """
            Bucket transactions by date, accumulating amounts by currency.
            No FX conversion here - just collect raw amounts per currency.
            """
            buckets: Dict[str, Dict[str, Any]] = {}
            for r in data_rows:
                created = r["created_at"]
                try:
                    if isinstance(created, str):
                        created_dt = datetime.fromisoformat(created)
                    else:
                        created_dt = created
                    # Ensure timezone-aware datetime
                    if created_dt.tzinfo is None:
                        created_dt = created_dt.replace(tzinfo=timezone.utc)
                    key_dt = _floor_to_period(created_dt, interval)
                    key = key_dt.date().isoformat()
                    
                    if key not in buckets:
                        buckets[key] = {
                            "count": 0.0,
                            "success": 0.0,
                            "sales_by_currency": {},  # {currency: amount}
                            "refunds_by_currency": {}  # {currency: amount}
                        }
                    
                    b = buckets[key]
                    amt = float(r["amount"] or 0.0)
                    order_currency = (r.get("currency") or base_currency).upper()
                    
                    b["count"] += 1.0
                    
                    # Detect refunds: negative amount OR refund status
                    if amt < 0 or _is_refund_status(r["status"]):
                        refund_amt = abs(amt)
                        b["refunds_by_currency"][order_currency] = b["refunds_by_currency"].get(order_currency, 0.0) + refund_amt
                    elif _is_success_status(r["status"]):
                        b["success"] += 1.0
                        b["sales_by_currency"][order_currency] = b["sales_by_currency"].get(order_currency, 0.0) + amt
                        
                except Exception as e:
                    logger.warning(f"Skipping row due to processing error: {e}")
                    continue
            return buckets

        # Convert Row objects to dicts for bucketize
        rows_as_dicts = [dict(r) for r in rows]
        prev_rows_as_dicts = [dict(r) for r in prev_rows] if compare else []
        
        cur_buckets = bucketize(rows_as_dicts)
        prev_buckets = bucketize(prev_rows_as_dicts) if compare else {}
        
        # Debug logging
        logger.info(f"Bucketize result - keys: {list(cur_buckets.keys())}, bucket count: {len(cur_buckets)}")
        if cur_buckets:
            sample_key = list(cur_buckets.keys())[0]
            logger.info(f"Sample bucket ({sample_key}): {cur_buckets[sample_key]}")

        async def compute_series_builder(buckets: Dict[str, Dict[str, Any]], period_start: datetime, period_end: datetime) -> List[Dict[str, Any]]:
            """
            Convert currency-bucketed data to base_currency and compute metrics with coverage tracking.
            """
            series: List[Dict[str, Any]] = []
            try:
                # Ensure timezone-aware boundaries
                if period_start.tzinfo is None:
                    period_start = period_start.replace(tzinfo=timezone.utc)
                if period_end.tzinfo is None:
                    period_end = period_end.replace(tzinfo=timezone.utc)
                
                step = timedelta(days=7) if interval == "week" else timedelta(days=1)
                t = _floor_to_period(period_start, interval)
                end_boundary = _floor_to_period(period_end, interval)
                
                while t <= end_boundary:
                    key = t.date().isoformat()
                    bucket = buckets.get(key, {"count": 0.0, "success": 0.0, "sales_by_currency": {}, "refunds_by_currency": {}})
                    
                    # Convert sales to base currency
                    total_sales = 0.0
                    sales_real_rate_count = 0
                    sales_total_count = 0
                    assumed_pairs_sales = []
                    
                    for curr, amt in bucket["sales_by_currency"].items():
                        rate, source = await _get_rate(curr, base_currency, t.date())
                        total_sales += amt * rate
                        sales_total_count += 1
                        if source in ["direct", "date", "global", "latest"]:
                            sales_real_rate_count += 1
                        if source == "assumed":
                            assumed_pairs_sales.append(f"{curr}->{base_currency}")
                
                    # Convert refunds to base currency
                    total_refunds = 0.0
                    refunds_real_rate_count = 0
                    refunds_total_count = 0
                    assumed_pairs_refunds = []
                    
                    for curr, amt in bucket["refunds_by_currency"].items():
                        rate, source = await _get_rate(curr, base_currency, t.date())
                        total_refunds += amt * rate
                        refunds_total_count += 1
                        if source in ["direct", "date", "global", "latest"]:
                            refunds_real_rate_count += 1
                        if source == "assumed":
                            assumed_pairs_refunds.append(f"{curr}->{base_currency}")
                    
                    # Calculate FX coverage
                    total_transactions = sales_total_count + refunds_total_count
                    total_real_rates = sales_real_rate_count + refunds_real_rate_count
                    fx_coverage = (total_real_rates / total_transactions) if total_transactions > 0 else 1.0
                    
                    # Combine assumed pairs
                    fx_assumed_pairs = list(set(assumed_pairs_sales + assumed_pairs_refunds))
                
                    # Calculate metric value
                    if metric == "gmv":
                        if mode == "net":
                            value = total_sales - total_refunds
                        else:
                            value = total_sales
                    elif metric == "orders":
                        value = bucket["success"]
                    elif metric == "aov":
                        orders = bucket["success"]
                        if orders > 0:
                            if mode == "net":
                                value = (total_sales - total_refunds) / orders
                            else:
                                value = total_sales / orders
                        else:
                            value = 0.0
                    elif metric == "success_rate":
                        total = bucket["count"]
                        value = (bucket["success"] / total) * 100.0 if total > 0 else 0.0
                    elif metric == "refunds":
                        value = total_refunds
                    else:
                        value = 0.0
                    
                    point = {
                        "date": key,
                        "value": round(float(value), 2),
                        "fx_coverage": round(fx_coverage, 3)
                    }
                    
                    if fx_assumed_pairs:
                        point["fx_assumed_pairs"] = fx_assumed_pairs
                    
                    series.append(point)
                    t = t + step
                
                return series
            except Exception as e:
                logger.error(f"compute_series_builder failed: {e}", exc_info=True)
                # Return empty series on error so the API doesn't crash
                return []

        current_series = await compute_series_builder(cur_buckets, start, now)
        comparison_series = await compute_series_builder(prev_buckets, prev_start, prev_end) if compare else []
        
        # Debug logging
        logger.info(f"Series result - current length: {len(current_series)}, sample: {current_series[:2] if current_series else 'EMPTY'}")

        # Calculate overall FX coverage
        all_coverages = [p.get("fx_coverage", 1.0) for p in current_series]
        avg_coverage = sum(all_coverages) / len(all_coverages) if all_coverages else 1.0
        
        # Collect all assumed pairs
        all_assumed = []
        for p in current_series:
            all_assumed.extend(p.get("fx_assumed_pairs", []))
        unique_assumed = list(set(all_assumed))
        
        # Build FX note
        fx_note = None
        if unique_assumed:
            fx_note = f"Some rates used fallback (coverage: {avg_coverage*100:.1f}%): {', '.join(unique_assumed[:5])}"
            if len(unique_assumed) > 5:
                fx_note += f" (and {len(unique_assumed) - 5} more)"
        elif avg_coverage < 1.0:
            fx_note = f"FX coverage: {avg_coverage*100:.1f}% (some rates from global cache)"

        result = {
            "status": "success",
            "data": {
                "metric": metric,
                "interval": interval,
                "range": range,
                "mode": mode,
                "base_currency": base_currency,
                "from": start.isoformat(),
                "to": now.isoformat(),
                "series": current_series,
                "comparison_series": comparison_series,
                "filters": {"psp": psp, "currency": currency},
            },
        }
        
        if fx_note:
            result["data"]["fx_note"] = fx_note
        
        # Cache the result
        TRENDS_CACHE[cache_key] = (result, time.time())
        
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to compute trends: {e}")
        return {
            "status": "success",
            "data": {
                "metric": metric,
                "interval": interval,
                "range": range,
                "series": [],
                "comparison_series": [],
                "error": str(e),
            },
        }


@router.get("/analytics/trends/metrics")
async def trends_cache_metrics(
    principal: Dict[str, Any] = Depends(_get_principal)
):
    """
    Get cache performance metrics.
    
    Returns cache hit rate, miss rate, and current entries.
    Requires valid JWT token.
    """
    total_requests = CACHE_HITS + CACHE_MISSES
    hit_rate = (CACHE_HITS / total_requests * 100) if total_requests > 0 else 0.0
    
    return {
        "status": "success",
        "data": {
            "hits": CACHE_HITS,
            "misses": CACHE_MISSES,
            "total_requests": total_requests,
            "hit_rate": round(hit_rate, 2),
            "entries": len(TRENDS_CACHE),
            "ttl_seconds": CACHE_TTL_SECONDS
        }
    }


@router.get("/analytics/trends.csv")
async def export_trends_csv(
    metric: str = Query("gmv", pattern="^(gmv|orders|aov|success_rate|refunds)$"),
    range: str = Query("30d", pattern="^(1d|7d|30d|90d)$"),
    interval: str = Query("day", pattern="^(day|week)$"),
    mode: str = Query("gross", pattern="^(gross|net)$"),
    psp: Optional[str] = Query(None),
    currency: Optional[str] = Query(None),
    compare: bool = Query(True),
    base_currency: Optional[str] = Query(None, description="Convert all amounts to this currency"),
    merchant_id: Optional[str] = Query(None, description="Admin/operator: override merchant context"),
    principal: Dict[str, Any] = Depends(_get_principal),
):
    """
    Export trends data as CSV with currency normalization.
    
    Same parameters as JSON endpoint.
    Output columns: date, current_value, previous_value, metric, range, interval, mode, base_currency
    """
    from fastapi.responses import StreamingResponse
    import io
    
    # Get data using the same logic as JSON endpoint
    try:
        # Authorization
        role = (principal.get("role") or "").lower()
        if role not in [UserRole.MERCHANT.value, UserRole.ADMIN.value, UserRole.OPERATOR.value]:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
        
        # Merchant context
        if role == UserRole.MERCHANT.value:
            merchant_id = principal.get("entity_id") or principal.get("merchant_id") or merchant_id
        if not merchant_id:
            raise HTTPException(status_code=400, detail="merchant_id required")
        
        # Get trends data (will use cache if available)
        json_response = await get_trends(
            metric=metric,
            range=range,
            interval=interval,
            mode=mode,
            psp=psp,
            currency=currency,
            compare=compare,
            base_currency=base_currency,
            merchant_id=merchant_id,
            principal=principal
        )
        
        data = json_response.get("data", {})
        series = data.get("series", [])
        comparison_series = data.get("comparison_series", [])
        resolved_base_currency = data.get("base_currency", "USD")
        
        # Build CSV
        output = io.StringIO()
        output.write("date,current_value,previous_value,metric,range,interval,mode,base_currency\n")
        
        # Create lookup for comparison data
        comp_lookup = {item["date"]: item["value"] for item in comparison_series}
        
        for item in series:
            date = item["date"]
            current_value = item["value"]
            previous_value = comp_lookup.get(date, "")
            
            output.write(f"{date},{current_value},{previous_value},{metric},{range},{interval},{mode},{resolved_base_currency}\n")
        
        output.seek(0)
        
        # Return as CSV file
        filename = f"analytics_trends_{metric}_{range}_{mode}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv"
        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to export CSV: {e}")
        raise HTTPException(status_code=500, detail=f"CSV export failed: {str(e)}")
