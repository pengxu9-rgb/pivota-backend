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
import copy
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
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field, ConfigDict

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
    apply_utm,
    get_allowed_domains_for_market,
    is_destination_domain_allowed,
    make_redirect_token,
)
from services.external_referral_readiness import should_block_external_referral_runtime
from models.standard_product import StandardProduct, ProductStatus
from services.agent_task_manager import AgentTaskManager
from observability.reliability_metrics import (
    record_catalog_search,
    record_catalog_upstream_fallback,
    record_catalog_upstream_timeout,
    set_catalog_upstream_circuit,
)

def _resolve_default_agent_api_base() -> str:
    configured = str(os.getenv("AGENT_API_BASE", "") or "").strip().rstrip("/")
    if configured:
        return configured
    # Service-local loopback avoids public TLS handshakes for internal proxy calls.
    port = str(os.getenv("PORT", "8080") or "8080").strip() or "8080"
    return f"http://127.0.0.1:{port}"


AGENT_API_BASE = _resolve_default_agent_api_base()
AGENT_API_KEY = os.getenv("SHOP_GATEWAY_AGENT_API_KEY") or os.getenv("PIVOTA_API_KEY") or os.getenv("AGENT_API_KEY")

logger = logging.getLogger(__name__)


def _bootstrap_env_int(name: str, default: int, *, min_value: int, max_value: int) -> int:
    raw = (os.getenv(name) or "").strip()
    try:
        value = int(raw) if raw else default
    except Exception:
        value = default
    return max(min_value, min(max_value, value))


def _bootstrap_env_float(name: str, default: float, *, min_value: float, max_value: float) -> float:
    raw = (os.getenv(name) or "").strip()
    try:
        value = float(raw) if raw else default
    except Exception:
        value = default
    return max(min_value, min(max_value, value))


def _bootstrap_env_bool(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


_UPSTREAM_HTTP_MAX_CONNECTIONS = _bootstrap_env_int(
    "AGENT_SHOP_UPSTREAM_CLIENT_MAX_CONNECTIONS",
    256,
    min_value=8,
    max_value=2048,
)
_UPSTREAM_HTTP_MAX_KEEPALIVE_CONNECTIONS = _bootstrap_env_int(
    "AGENT_SHOP_UPSTREAM_CLIENT_MAX_KEEPALIVE_CONNECTIONS",
    128,
    min_value=8,
    max_value=2048,
)
_UPSTREAM_HTTP_KEEPALIVE_EXPIRY_SECONDS = _bootstrap_env_float(
    "AGENT_SHOP_UPSTREAM_CLIENT_KEEPALIVE_EXPIRY_SECONDS",
    300.0,
    min_value=5.0,
    max_value=3600.0,
)
_UPSTREAM_HTTP_ENABLE_HTTP2 = _bootstrap_env_bool(
    "AGENT_SHOP_UPSTREAM_CLIENT_HTTP2",
    True,
)
_UPSTREAM_HTTP_WARMUP_ENABLED = _bootstrap_env_bool(
    "AGENT_SHOP_UPSTREAM_CLIENT_WARMUP_ENABLED",
    True,
)
_UPSTREAM_HTTP_WARMUP_TIMEOUT_SECONDS = _bootstrap_env_float(
    "AGENT_SHOP_UPSTREAM_CLIENT_WARMUP_TIMEOUT_SECONDS",
    1.2,
    min_value=0.2,
    max_value=10.0,
)

_SHARED_UPSTREAM_HTTP_CLIENT: Optional[httpx.AsyncClient] = None
_SHARED_UPSTREAM_HTTP_CLIENT_LOCK = asyncio.Lock()
_SHARED_UPSTREAM_HTTP_LIMITS = httpx.Limits(
    max_connections=_UPSTREAM_HTTP_MAX_CONNECTIONS,
    max_keepalive_connections=_UPSTREAM_HTTP_MAX_KEEPALIVE_CONNECTIONS,
    keepalive_expiry=_UPSTREAM_HTTP_KEEPALIVE_EXPIRY_SECONDS,
)


def _build_request_timeout(timeout_seconds: float) -> httpx.Timeout:
    total = max(0.1, float(timeout_seconds or 0.1))
    connect_timeout = min(5.0, total)
    pool_timeout = min(2.0, total)
    return httpx.Timeout(connect=connect_timeout, read=total, write=total, pool=pool_timeout)


async def _get_shared_upstream_http_client() -> httpx.AsyncClient:
    global _SHARED_UPSTREAM_HTTP_CLIENT
    client = _SHARED_UPSTREAM_HTTP_CLIENT
    if client is not None:
        return client
    async with _SHARED_UPSTREAM_HTTP_CLIENT_LOCK:
        client = _SHARED_UPSTREAM_HTTP_CLIENT
        if client is None:
            client = httpx.AsyncClient(
                http2=_UPSTREAM_HTTP_ENABLE_HTTP2,
                limits=_SHARED_UPSTREAM_HTTP_LIMITS,
                timeout=_build_request_timeout(15.0),
            )
            _SHARED_UPSTREAM_HTTP_CLIENT = client
    return client


def _collect_upstream_warmup_urls() -> List[str]:
    urls: List[str] = []
    for base in (
        AGENT_API_BASE,
        MULTI_SEARCH_UPSTREAM_FALLBACK_BASE_URL,
    ):
        base_url = str(base or "").strip().rstrip("/")
        if not base_url:
            continue
        candidate = f"{base_url}/health"
        if candidate not in urls:
            urls.append(candidate)
    return urls


async def _warm_shared_upstream_http_client() -> None:
    if not _UPSTREAM_HTTP_WARMUP_ENABLED:
        return
    warmup_urls = _collect_upstream_warmup_urls()
    if not warmup_urls:
        return
    client = await _get_shared_upstream_http_client()
    timeout = _build_request_timeout(_UPSTREAM_HTTP_WARMUP_TIMEOUT_SECONDS)

    async def _probe(url: str) -> None:
        try:
            await client.get(
                url,
                timeout=timeout,
                headers={"Cache-Control": "no-cache"},
            )
        except Exception as exc:
            logger.debug(
                "upstream client warmup probe failed",
                extra={"url": url, "error": str(exc)},
            )

    await asyncio.gather(*[_probe(url) for url in warmup_urls], return_exceptions=True)

_MERCHANT_SHOPIFY_CURRENCY_CACHE: Dict[str, tuple[float, str]] = {}
_MERCHANT_SHOPIFY_CURRENCY_TTL_SECONDS = 6 * 60 * 60
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


@router.on_event("shutdown")
async def _close_shared_upstream_http_client() -> None:
    global _SHARED_UPSTREAM_HTTP_CLIENT
    client = _SHARED_UPSTREAM_HTTP_CLIENT
    if client is None:
        return
    _SHARED_UPSTREAM_HTTP_CLIENT = None
    try:
        await client.aclose()
    except Exception:
        logger.debug("failed to close shared upstream http client", exc_info=True)


@router.on_event("startup")
async def _warm_shared_upstream_http_client_on_startup() -> None:
    if not _UPSTREAM_HTTP_WARMUP_ENABLED:
        return
    # Fire-and-forget warmup so deploy healthchecks are not blocked.
    asyncio.create_task(_warm_shared_upstream_http_client())

# Bounded queue + worker pool for heavy agent work.
agent_task_manager = AgentTaskManager.from_env()


def _env_int(name: str, default: int, *, min_value: int, max_value: int) -> int:
    raw = (os.getenv(name) or "").strip()
    try:
        value = int(raw) if raw else default
    except Exception:
        value = default
    return max(min_value, min(max_value, value))


def _env_float(name: str, default: float, *, min_value: float, max_value: float) -> float:
    raw = (os.getenv(name) or "").strip()
    try:
        value = float(raw) if raw else default
    except Exception:
        value = default
    return max(min_value, min(max_value, value))


def _env_bool(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


SEARCH_ORCHESTRATOR_UNIFIED = _env_bool(
    "SEARCH_ORCHESTRATOR_UNIFIED",
    True,
)
SEARCH_FRAGRANCE_SEMANTIC_RETRY = _env_bool(
    "SEARCH_FRAGRANCE_SEMANTIC_RETRY",
    True,
)
SEARCH_EXTERNAL_HARD_RULE_PRUNE = _env_bool(
    "SEARCH_EXTERNAL_HARD_RULE_PRUNE",
    True,
)
SEARCH_LIMIT_MAX = _env_int(
    "AGENT_SEARCH_LIMIT_MAX",
    200,
    min_value=1,
    max_value=200,
)


def _clamp_search_limit(raw_limit: Any, *, fallback: int = 20) -> int:
    try:
        limit = int(raw_limit) if raw_limit is not None else int(fallback)
    except Exception:
        limit = int(fallback)
    return max(1, min(limit, SEARCH_LIMIT_MAX))


def _classify_query_semantic_class(query: Optional[str]) -> str:
    q = str(query or "").strip().lower()
    if not q:
        return "default"
    q_compact = re.sub(r"[^a-z0-9]+", "", q)
    if re.search(
        r"\b(perfume|perfumes|fragrance|fragrances|parfum|parfums|cologne|eau de parfum|eau de toilette|body mist)\b",
        q,
    ):
        return "fragrance"
    if any(
        token in q_compact
        for token in (
            "perfume",
            "perfumes",
            "fragrance",
            "fragrances",
            "parfum",
            "parfums",
            "cologne",
            "bodymist",
            "eaudeparfum",
            "eaudetoilette",
            "edp",
            "edt",
        )
    ):
        return "fragrance"
    if re.search(
        r"\b(lingerie|underwear|bra|panties|panty|briefs|thong|lencer[ií]a|ropa interior)\b",
        q,
    ):
        return "lingerie"
    if re.search(
        r"\b(beauty|skincare|skin care|cosmetic|cosmetics|makeup|serum|toner|moisturizer|cleanser)\b",
        q,
    ):
        return "beauty"
    return "default"


def _build_fragrance_semantic_retry_query(query: Optional[str]) -> Optional[str]:
    q = str(query or "").strip().lower()
    if not q:
        return None
    tokens = re.findall(r"[a-z0-9]+", q)
    if not tokens:
        return None
    drop_tokens = {
        "a",
        "an",
        "and",
        "for",
        "with",
        "the",
        "to",
        "beauty",
        "cosmetics",
        "makeup",
        "tool",
        "tools",
        "brush",
        "brushes",
        "kit",
    }
    keep: List[str] = []
    for token in tokens:
        if token in drop_tokens:
            continue
        if token in keep:
            continue
        keep.append(token)
        if len(keep) >= 8:
            break
    if not keep:
        return None
    has_core_fragrance_term = any(
        token in {"perfume", "perfumes", "fragrance", "fragrances", "parfum", "parfums", "cologne", "mist"}
        for token in keep
    )
    if not has_core_fragrance_term:
        keep.append("fragrance")
    retry_query = " ".join(keep)
    if retry_query != q:
        return retry_query

    # Avoid no-op retries like query="perfume".
    # We intentionally append compact semantic expansions to broaden recall.
    expansions = [
        "fragrance",
        "parfum",
        "cologne",
        "body mist",
        "eau de parfum",
        "eau de toilette",
    ]
    expanded_parts: List[str] = [q]
    for term in expansions:
        if term in q:
            continue
        expanded_parts.append(term)
    expanded_query = " ".join(expanded_parts).strip()
    return expanded_query if expanded_query and expanded_query != q else None


def _normalize_gateway_route_health(
    metadata: Optional[Dict[str, Any]],
    *,
    default_decision_node: str,
) -> Dict[str, Any]:
    md = metadata if isinstance(metadata, dict) else {}
    current = md.get("route_health")
    route_health: Dict[str, Any] = dict(current) if isinstance(current, dict) else {}
    search_decision = md.get("search_decision")
    if not isinstance(search_decision, dict):
        search_decision = None
    source_breakdown = md.get("source_breakdown")
    source_breakdown = source_breakdown if isinstance(source_breakdown, dict) else {}

    def _int_non_negative(value: Any) -> int:
        try:
            return max(0, int(value))
        except Exception:
            return 0

    route_health["orchestrator_path"] = str(
        route_health.get("orchestrator_path")
        or md.get("orchestrator_path")
        or "shop_gateway.find_products_multi"
    )
    route_health["decision_node"] = str(
        route_health.get("decision_node")
        or md.get("decision_node")
        or default_decision_node
    )
    route_health["query_semantic_class"] = (
        str(
            route_health.get("query_semantic_class")
            or md.get("query_semantic_class")
            or (search_decision or {}).get("query_semantic_class")
            or "default"
        )
        .strip()
        .lower()
        or "default"
    )
    route_health["domain_filter_dropped_external"] = _int_non_negative(
        route_health.get("domain_filter_dropped_external")
        if route_health.get("domain_filter_dropped_external") is not None
        else (
            md.get("domain_filter_dropped_external")
            if md.get("domain_filter_dropped_external") is not None
            else (search_decision or {}).get("domain_filter_dropped_external")
        )
    )
    route_health["external_fill_gate_reason"] = (
        str(
            route_health.get("external_fill_gate_reason")
            or md.get("external_fill_gate_reason")
            or md.get("query_source")
            or ""
        ).strip()
        or None
    )
    fallback_reason = route_health.get("fallback_reason")
    if fallback_reason is None:
        fallback_reason = md.get("fallback_reason")
    route_health["fallback_reason"] = fallback_reason
    semantic_retry_applied = bool(
        route_health.get("semantic_retry_applied")
        if route_health.get("semantic_retry_applied") is not None
        else md.get("semantic_retry_applied")
    )
    route_health["semantic_retry_applied"] = semantic_retry_applied
    route_health["semantic_retry_query"] = (
        str(
            route_health.get("semantic_retry_query")
            or md.get("semantic_retry_query")
            or ""
        ).strip()
        or None
    )
    route_health["semantic_retry_hits"] = _int_non_negative(
        route_health.get("semantic_retry_hits")
        if route_health.get("semantic_retry_hits") is not None
        else md.get("semantic_retry_hits")
    )
    route_health["semantic_retry_actual_attempted"] = bool(
        route_health.get("semantic_retry_actual_attempted")
        if route_health.get("semantic_retry_actual_attempted") is not None
        else md.get("semantic_retry_actual_attempted")
    )
    route_health["external_seed_executed"] = bool(
        route_health.get("external_seed_executed")
        if route_health.get("external_seed_executed") is not None
        else md.get("external_seed_executed")
    )
    route_health["external_seed_skip_reason"] = (
        str(
            route_health.get("external_seed_skip_reason")
            if route_health.get("external_seed_skip_reason") is not None
            else md.get("external_seed_skip_reason")
            or ""
        ).strip()
        or None
    )
    route_health["external_seed_cache_hit"] = bool(
        route_health.get("external_seed_cache_hit")
        if route_health.get("external_seed_cache_hit") is not None
        else md.get("external_seed_cache_hit")
    ) or route_health["external_seed_skip_reason"] == "cache_hit"
    route_health["external_seed_query_timeout"] = bool(
        route_health.get("external_seed_query_timeout")
        if route_health.get("external_seed_query_timeout") is not None
        else md.get("external_seed_query_timeout")
    )
    route_health["external_seed_rows_fetched"] = _int_non_negative(
        route_health.get("external_seed_rows_fetched")
        if route_health.get("external_seed_rows_fetched") is not None
        else md.get("external_seed_rows_fetched")
    )
    route_health["external_seed_rows_built"] = _int_non_negative(
        route_health.get("external_seed_rows_built")
        if route_health.get("external_seed_rows_built") is not None
        else md.get("external_seed_rows_built")
    )
    route_health["external_seed_brand_strict_rows"] = _int_non_negative(
        route_health.get("external_seed_brand_strict_rows")
        if route_health.get("external_seed_brand_strict_rows") is not None
        else md.get("external_seed_brand_strict_rows")
    )
    route_health["external_seed_brand_relevant_rows"] = _int_non_negative(
        route_health.get("external_seed_brand_relevant_rows")
        if route_health.get("external_seed_brand_relevant_rows") is not None
        else md.get("external_seed_brand_relevant_rows")
    )
    route_health["external_seed_broad_fallback_used"] = bool(
        route_health.get("external_seed_broad_fallback_used")
        if route_health.get("external_seed_broad_fallback_used") is not None
        else md.get("external_seed_broad_fallback_used")
    )
    route_health["external_seed_broad_scope_rows"] = _int_non_negative(
        route_health.get("external_seed_broad_scope_rows")
        if route_health.get("external_seed_broad_scope_rows") is not None
        else md.get("external_seed_broad_scope_rows")
    )
    route_health["internal_raw_count"] = _int_non_negative(
        route_health.get("internal_raw_count")
        if route_health.get("internal_raw_count") is not None
        else md.get("internal_raw_count")
    )
    route_health["external_raw_count"] = _int_non_negative(
        route_health.get("external_raw_count")
        if route_health.get("external_raw_count") is not None
        else (md.get("external_raw_count") if md.get("external_raw_count") is not None else source_breakdown.get("external_seed_count"))
    )
    route_health["external_seed_returned_count"] = route_health["external_raw_count"]
    route_health["merged_pre_limit_count"] = _int_non_negative(
        route_health.get("merged_pre_limit_count")
        if route_health.get("merged_pre_limit_count") is not None
        else md.get("merged_pre_limit_count")
    )
    route_health["primary_quality_gate_passed"] = bool(
        route_health.get("primary_quality_gate_passed")
        if route_health.get("primary_quality_gate_passed") is not None
        else md.get("primary_quality_gate_passed")
    )
    primary_quality_score_raw = (
        route_health.get("primary_quality_score")
        if route_health.get("primary_quality_score") is not None
        else md.get("primary_quality_score")
    )
    try:
        route_health["primary_quality_score"] = (
            max(0.0, min(1.0, float(primary_quality_score_raw)))
            if primary_quality_score_raw is not None
            else None
        )
    except Exception:
        route_health["primary_quality_score"] = None
    route_health["low_quality_nonempty_detected"] = bool(
        route_health.get("low_quality_nonempty_detected")
        if route_health.get("low_quality_nonempty_detected") is not None
        else md.get("low_quality_nonempty_detected")
    )
    route_health["supplement_attempted"] = bool(
        route_health.get("supplement_attempted")
        if route_health.get("supplement_attempted") is not None
        else md.get("supplement_attempted")
    )
    route_health["supplement_skip_reason"] = (
        str(route_health.get("supplement_skip_reason") or md.get("supplement_skip_reason") or "").strip()
        or None
    )
    route_health["retry_attempt_count"] = _int_non_negative(
        route_health.get("retry_attempt_count")
        if route_health.get("retry_attempt_count") is not None
        else md.get("retry_attempt_count")
    )
    route_health["fallback_attempt_count"] = _int_non_negative(
        route_health.get("fallback_attempt_count")
        if route_health.get("fallback_attempt_count") is not None
        else md.get("fallback_attempt_count")
    )
    route_health["selected_fallback_attempt"] = _int_non_negative(
        route_health.get("selected_fallback_attempt")
        if route_health.get("selected_fallback_attempt") is not None
        else md.get("selected_fallback_attempt")
    )
    route_health["final_returned_count"] = _int_non_negative(
        route_health.get("final_returned_count")
        if route_health.get("final_returned_count") is not None
        else md.get("final_returned_count")
    )

    md["orchestrator_path"] = route_health["orchestrator_path"]
    md["decision_node"] = route_health["decision_node"]
    md["query_semantic_class"] = route_health["query_semantic_class"]
    md["domain_filter_dropped_external"] = route_health["domain_filter_dropped_external"]
    md["external_fill_gate_reason"] = route_health["external_fill_gate_reason"]
    md["fallback_reason"] = route_health["fallback_reason"]
    md["semantic_retry_applied"] = route_health["semantic_retry_applied"]
    md["semantic_retry_query"] = route_health["semantic_retry_query"]
    md["semantic_retry_hits"] = route_health["semantic_retry_hits"]
    md["semantic_retry_actual_attempted"] = route_health["semantic_retry_actual_attempted"]
    md["external_seed_executed"] = route_health["external_seed_executed"]
    md["external_seed_skip_reason"] = route_health["external_seed_skip_reason"]
    md["external_seed_cache_hit"] = route_health["external_seed_cache_hit"]
    md["external_seed_query_timeout"] = route_health["external_seed_query_timeout"]
    md["external_seed_rows_fetched"] = route_health["external_seed_rows_fetched"]
    md["external_seed_rows_built"] = route_health["external_seed_rows_built"]
    md["external_seed_brand_strict_rows"] = route_health["external_seed_brand_strict_rows"]
    md["external_seed_brand_relevant_rows"] = route_health["external_seed_brand_relevant_rows"]
    md["external_seed_broad_fallback_used"] = route_health["external_seed_broad_fallback_used"]
    md["external_seed_broad_scope_rows"] = route_health["external_seed_broad_scope_rows"]
    md["external_seed_returned_count"] = route_health["external_seed_returned_count"]
    md["internal_raw_count"] = route_health["internal_raw_count"]
    md["external_raw_count"] = route_health["external_raw_count"]
    if source_breakdown and source_breakdown.get("external_seed_count") is None:
        source_breakdown["external_seed_count"] = route_health["external_seed_returned_count"]
        md["source_breakdown"] = source_breakdown
    md["merged_pre_limit_count"] = route_health["merged_pre_limit_count"]
    md["primary_quality_gate_passed"] = route_health["primary_quality_gate_passed"]
    md["primary_quality_score"] = route_health["primary_quality_score"]
    md["low_quality_nonempty_detected"] = route_health["low_quality_nonempty_detected"]
    md["supplement_attempted"] = route_health["supplement_attempted"]
    md["supplement_skip_reason"] = route_health["supplement_skip_reason"]
    md["retry_attempt_count"] = route_health["retry_attempt_count"]
    md["fallback_attempt_count"] = route_health["fallback_attempt_count"]
    md["selected_fallback_attempt"] = route_health["selected_fallback_attempt"]
    md["final_returned_count"] = route_health["final_returned_count"]
    if search_decision is not None:
        search_decision["query_semantic_class"] = route_health["query_semantic_class"]
        search_decision["domain_filter_dropped_external"] = route_health[
            "domain_filter_dropped_external"
        ]
        md["search_decision"] = search_decision
    md["route_health"] = route_health
    return md


MULTI_SEARCH_MERCHANT_SCAN_LIMIT = _env_int(
    "AGENT_SHOP_MULTI_MERCHANT_SCAN_LIMIT",
    18,
    min_value=1,
    max_value=200,
)
MULTI_SEARCH_MERCHANT_SCAN_LIMIT_CREATOR = _env_int(
    "AGENT_SHOP_MULTI_MERCHANT_SCAN_LIMIT_CREATOR",
    32,
    min_value=1,
    max_value=300,
)
MULTI_SEARCH_MERCHANT_CONCURRENCY = _env_int(
    "AGENT_SHOP_MULTI_MERCHANT_CONCURRENCY",
    6,
    min_value=1,
    max_value=64,
)
MULTI_SEARCH_MERCHANT_FETCH_TIMEOUT_SECONDS = _env_float(
    "AGENT_SHOP_MULTI_MERCHANT_FETCH_TIMEOUT_SECONDS",
    0.8,
    min_value=0.2,
    max_value=10.0,
)
MULTI_SEARCH_SEED_QUERY_TIMEOUT_SECONDS = _env_float(
    "AGENT_SHOP_MULTI_SEED_QUERY_TIMEOUT_SECONDS",
    1.6,
    min_value=0.1,
    max_value=5.0,
)
MULTI_SEARCH_SEED_BUILD_BUDGET_SECONDS = _env_float(
    "AGENT_SHOP_MULTI_SEED_BUILD_BUDGET_SECONDS",
    1.0,
    min_value=0.05,
    max_value=3.0,
)
MULTI_SEARCH_RECALL_QUERY_TIMEOUT_SECONDS = _env_float(
    "AGENT_SHOP_MULTI_RECALL_QUERY_TIMEOUT_SECONDS",
    1.0,
    min_value=0.1,
    max_value=5.0,
)
MULTI_SEARCH_SHOPPING_FAST_MERCHANT_SEED_LIMIT = _env_int(
    "AGENT_SHOP_MULTI_SHOPPING_FAST_MERCHANT_SEED_LIMIT",
    60,
    min_value=1,
    max_value=200,
)
MULTI_SEARCH_SHOPPING_ENABLE_RECALL_BOOST = _env_bool(
    "AGENT_SHOP_MULTI_SHOPPING_ENABLE_RECALL_BOOST",
    False,
)
MULTI_SEARCH_SHOPPING_ENABLE_SKU_JSON_SCAN = _env_bool(
    "AGENT_SHOP_MULTI_SHOPPING_ENABLE_SKU_JSON_SCAN",
    False,
)
MULTI_SEARCH_SHOPPING_ENABLE_SEED_TEXT_SCAN = _env_bool(
    "AGENT_SHOP_MULTI_SHOPPING_ENABLE_SEED_TEXT_SCAN",
    False,
)
MULTI_SEARCH_SHOPPING_RECALL_QUERY_TIMEOUT_SECONDS = _env_float(
    "AGENT_SHOP_MULTI_SHOPPING_RECALL_QUERY_TIMEOUT_SECONDS",
    1.0,
    min_value=0.1,
    max_value=5.0,
)
MULTI_SEARCH_FORCE_CACHE_ONLY_LEGACY = _env_bool(
    "AGENT_SHOP_MULTI_FORCE_CACHE_ONLY",
    True,
)
MULTI_SEARCH_FORCE_CACHE_ONLY_CREATOR = _env_bool(
    "AGENT_SHOP_MULTI_FORCE_CACHE_ONLY_CREATOR",
    MULTI_SEARCH_FORCE_CACHE_ONLY_LEGACY,
)
MULTI_SEARCH_FORCE_CACHE_ONLY_SHOPPING = _env_bool(
    "AGENT_SHOP_MULTI_FORCE_CACHE_ONLY_SHOPPING",
    False,
)
MULTI_SEARCH_FORCE_CACHE_ONLY_DEFAULT = _env_bool(
    "AGENT_SHOP_MULTI_FORCE_CACHE_ONLY_DEFAULT",
    MULTI_SEARCH_FORCE_CACHE_ONLY_LEGACY,
)
MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT = _env_bool(
    "AGENT_SHOP_MULTI_ENABLE_BASE_MERCHANT_FANOUT",
    True,
)
MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT_CREATOR = _env_bool(
    "AGENT_SHOP_MULTI_ENABLE_BASE_MERCHANT_FANOUT_CREATOR",
    MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT,
)
MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT_SHOPPING = _env_bool(
    "AGENT_SHOP_MULTI_ENABLE_BASE_MERCHANT_FANOUT_SHOPPING",
    False,
)
MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT_DEFAULT = _env_bool(
    "AGENT_SHOP_MULTI_ENABLE_BASE_MERCHANT_FANOUT_DEFAULT",
    MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT,
)
MULTI_SEARCH_SKIP_HISTORY_SHOPPING = _env_bool(
    "AGENT_SHOP_MULTI_SKIP_HISTORY_SHOPPING",
    True,
)
MULTI_SEARCH_SEED_QUERY_LIMIT_SHOPPING = _env_int(
    "AGENT_SHOP_MULTI_SEED_QUERY_LIMIT_SHOPPING",
    200,
    min_value=0,
    max_value=300,
)
MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM = _env_bool(
    "AGENT_SHOP_MULTI_DELEGATE_SHOPPING_TO_UPSTREAM",
    False,
)
MULTI_SEARCH_UPSTREAM_FALLBACK_BASE_URL = (
    os.getenv("AGENT_SHOP_MULTI_UPSTREAM_FALLBACK_BASE_URL", "").strip().rstrip("/")
)
MULTI_SEARCH_UPSTREAM_FALLBACK_TIMEOUT_SECONDS = _env_float(
    "AGENT_SHOP_MULTI_UPSTREAM_FALLBACK_TIMEOUT_SECONDS",
    4.5,
    min_value=0.5,
    max_value=20.0,
)
MULTI_SEARCH_UPSTREAM_RESPONSE_CACHE_TTL_SECONDS = _env_float(
    "AGENT_SHOP_MULTI_UPSTREAM_RESPONSE_CACHE_TTL_SECONDS",
    45.0,
    min_value=0.0,
    max_value=600.0,
)
MULTI_SEARCH_UPSTREAM_EMPTY_CACHE_TTL_SECONDS = _env_float(
    "AGENT_SHOP_MULTI_UPSTREAM_EMPTY_CACHE_TTL_SECONDS",
    30.0,
    min_value=0.0,
    max_value=600.0,
)
MULTI_SEARCH_UPSTREAM_ERROR_CACHE_TTL_SECONDS = _env_float(
    "AGENT_SHOP_MULTI_UPSTREAM_ERROR_CACHE_TTL_SECONDS",
    20.0,
    min_value=0.0,
    max_value=300.0,
)
MULTI_SEARCH_UPSTREAM_CACHE_MAX_ENTRIES = _env_int(
    "AGENT_SHOP_MULTI_UPSTREAM_CACHE_MAX_ENTRIES",
    512,
    min_value=1,
    max_value=10000,
)
MULTI_SEARCH_UPSTREAM_SHOPPING_TIMEOUT_CAP_SECONDS = _env_float(
    "AGENT_SHOP_MULTI_UPSTREAM_SHOPPING_TIMEOUT_CAP_SECONDS",
    0.9,
    min_value=0.3,
    max_value=20.0,
)
MULTI_SEARCH_UPSTREAM_CIRCUIT_FAILURE_THRESHOLD = _env_int(
    "AGENT_SHOP_MULTI_UPSTREAM_CIRCUIT_FAILURE_THRESHOLD",
    1,
    min_value=1,
    max_value=100,
)
MULTI_SEARCH_UPSTREAM_CIRCUIT_WINDOW_SECONDS = _env_float(
    "AGENT_SHOP_MULTI_UPSTREAM_CIRCUIT_WINDOW_SECONDS",
    45.0,
    min_value=1.0,
    max_value=600.0,
)
MULTI_SEARCH_UPSTREAM_CIRCUIT_OPEN_SECONDS = _env_float(
    "AGENT_SHOP_MULTI_UPSTREAM_CIRCUIT_OPEN_SECONDS",
    90.0,
    min_value=1.0,
    max_value=1200.0,
)
MULTI_SEARCH_UPSTREAM_CIRCUIT_OPEN_ON_TIMEOUT = _env_bool(
    "AGENT_SHOP_MULTI_UPSTREAM_CIRCUIT_OPEN_ON_TIMEOUT",
    True,
)
CATALOG_RELIABILITY_V2_ENABLED = _env_bool(
    "CATALOG_RELIABILITY_V2_ENABLED",
    False,
)
CATALOG_RELIABILITY_V2_LOCAL_FALLBACK_ON_DELEGATE_FAIL = _env_bool(
    "CATALOG_RELIABILITY_V2_LOCAL_FALLBACK_ON_DELEGATE_FAIL",
    False,
)
CATALOG_UPSTREAM_V2_SHOPPING_TIMEOUT_CAP_SECONDS = _env_float(
    "CATALOG_UPSTREAM_V2_SHOPPING_TIMEOUT_CAP_SECONDS",
    1.2,
    min_value=0.3,
    max_value=20.0,
)
CATALOG_UPSTREAM_V2_CIRCUIT_FAILURE_THRESHOLD = _env_int(
    "CATALOG_UPSTREAM_V2_CIRCUIT_FAILURE_THRESHOLD",
    3,
    min_value=1,
    max_value=100,
)
CATALOG_UPSTREAM_V2_CIRCUIT_WINDOW_SECONDS = _env_float(
    "CATALOG_UPSTREAM_V2_CIRCUIT_WINDOW_SECONDS",
    45.0,
    min_value=1.0,
    max_value=600.0,
)
CATALOG_UPSTREAM_V2_CIRCUIT_OPEN_SECONDS = _env_float(
    "CATALOG_UPSTREAM_V2_CIRCUIT_OPEN_SECONDS",
    60.0,
    min_value=1.0,
    max_value=1200.0,
)
CATALOG_UPSTREAM_V2_CIRCUIT_OPEN_ON_TIMEOUT = _env_bool(
    "CATALOG_UPSTREAM_V2_CIRCUIT_OPEN_ON_TIMEOUT",
    True,
)
CATALOG_UPSTREAM_V2_LOCAL_FALLBACK_MIN_BUDGET_SECONDS = _env_float(
    "CATALOG_UPSTREAM_V2_LOCAL_FALLBACK_MIN_BUDGET_SECONDS",
    0.4,
    min_value=0.05,
    max_value=10.0,
)
MULTI_SEARCH_AURORA_FORCE_LOCAL_FALLBACK_ON_DELEGATE_FAIL = _env_bool(
    "AGENT_SHOP_MULTI_AURORA_FORCE_LOCAL_FALLBACK_ON_DELEGATE_FAIL",
    True,
)
MULTI_SEARCH_PAGE_REQUEST_DEDUP_ENABLED = _env_bool(
    "AGENT_SHOP_MULTI_PAGE_REQUEST_DEDUP_ENABLED",
    True,
)
MULTI_SEARCH_PAGE_REQUEST_DEDUP_TTL_SECONDS = _env_float(
    "AGENT_SHOP_MULTI_PAGE_REQUEST_DEDUP_TTL_SECONDS",
    4.0,
    min_value=0.0,
    max_value=60.0,
)
OFFERS_RESOLVE_SEED_QUERY_TIMEOUT_SECONDS = _env_float(
    "AGENT_SHOP_OFFERS_RESOLVE_SEED_QUERY_TIMEOUT_SECONDS",
    1.2,
    min_value=0.2,
    max_value=10.0,
)
OFFERS_RESOLVE_INTERNAL_TOTAL_BUDGET_SECONDS = _env_float(
    "AGENT_SHOP_OFFERS_RESOLVE_INTERNAL_TOTAL_BUDGET_SECONDS",
    2.2,
    min_value=0.4,
    max_value=20.0,
)
OFFERS_RESOLVE_INTERNAL_PID_QUERY_TIMEOUT_SECONDS = _env_float(
    "AGENT_SHOP_OFFERS_RESOLVE_INTERNAL_PID_QUERY_TIMEOUT_SECONDS",
    0.8,
    min_value=0.1,
    max_value=10.0,
)
OFFERS_RESOLVE_INTERNAL_SKU_EXACT_QUERY_TIMEOUT_SECONDS = _env_float(
    "AGENT_SHOP_OFFERS_RESOLVE_INTERNAL_SKU_EXACT_QUERY_TIMEOUT_SECONDS",
    0.9,
    min_value=0.1,
    max_value=10.0,
)
OFFERS_RESOLVE_INTERNAL_SKU_TEXT_SCAN_TIMEOUT_SECONDS = _env_float(
    "AGENT_SHOP_OFFERS_RESOLVE_INTERNAL_SKU_TEXT_SCAN_TIMEOUT_SECONDS",
    0.6,
    min_value=0.1,
    max_value=10.0,
)
OFFERS_RESOLVE_INTERNAL_GROUP_QUERY_TIMEOUT_SECONDS = _env_float(
    "AGENT_SHOP_OFFERS_RESOLVE_INTERNAL_GROUP_QUERY_TIMEOUT_SECONDS",
    0.8,
    min_value=0.1,
    max_value=10.0,
)
OFFERS_RESOLVE_INTERNAL_GROUP_CACHE_TIMEOUT_SECONDS = _env_float(
    "AGENT_SHOP_OFFERS_RESOLVE_INTERNAL_GROUP_CACHE_TIMEOUT_SECONDS",
    0.8,
    min_value=0.1,
    max_value=10.0,
)
OFFERS_RESOLVE_ENABLE_SKU_TEXT_SCAN = _env_bool(
    "AGENT_SHOP_OFFERS_RESOLVE_ENABLE_SKU_TEXT_SCAN",
    False,
)

SHOPPING_MULTI_SOURCES = {
    "shopping-agent",
    "shopping-agent-ui",
    "shopping-agent-web",
    "aurora",
    "aurora-chatbox",
}


def _normalize_surface_source(source: Optional[str]) -> str:
    return str(source or "").strip().lower().replace("_", "-")


def _is_shopping_multi_source(source: Optional[str]) -> bool:
    normalized = _normalize_surface_source(source)
    if not normalized:
        return False
    if normalized in SHOPPING_MULTI_SOURCES:
        return True
    if normalized.startswith("shopping-") or normalized.startswith("aurora-"):
        return True
    if "shopping" in normalized:
        return True
    if "aurora" in normalized:
        return True
    return False


def _catalog_rel_v2_enabled() -> bool:
    return bool(CATALOG_RELIABILITY_V2_ENABLED)


def _multi_upstream_circuit_failure_threshold() -> int:
    if _catalog_rel_v2_enabled():
        return max(1, int(CATALOG_UPSTREAM_V2_CIRCUIT_FAILURE_THRESHOLD))
    return max(1, int(MULTI_SEARCH_UPSTREAM_CIRCUIT_FAILURE_THRESHOLD))


def _multi_upstream_circuit_window_seconds() -> float:
    if _catalog_rel_v2_enabled():
        return max(1.0, float(CATALOG_UPSTREAM_V2_CIRCUIT_WINDOW_SECONDS))
    return max(1.0, float(MULTI_SEARCH_UPSTREAM_CIRCUIT_WINDOW_SECONDS))


def _multi_upstream_circuit_open_seconds() -> float:
    if _catalog_rel_v2_enabled():
        return max(1.0, float(CATALOG_UPSTREAM_V2_CIRCUIT_OPEN_SECONDS))
    return max(1.0, float(MULTI_SEARCH_UPSTREAM_CIRCUIT_OPEN_SECONDS))


def _multi_upstream_circuit_open_on_timeout() -> bool:
    if _catalog_rel_v2_enabled():
        return bool(CATALOG_UPSTREAM_V2_CIRCUIT_OPEN_ON_TIMEOUT)
    return bool(MULTI_SEARCH_UPSTREAM_CIRCUIT_OPEN_ON_TIMEOUT)


def _multi_upstream_shopping_timeout_cap_seconds() -> float:
    if _catalog_rel_v2_enabled():
        return float(CATALOG_UPSTREAM_V2_SHOPPING_TIMEOUT_CAP_SECONDS)
    return float(MULTI_SEARCH_UPSTREAM_SHOPPING_TIMEOUT_CAP_SECONDS)

_MULTI_SEARCH_UPSTREAM_CACHE: Dict[str, Dict[str, Any]] = {}
_MULTI_SEARCH_UPSTREAM_FAILURE_EVENTS: List[float] = []
_MULTI_SEARCH_UPSTREAM_CIRCUIT_OPEN_UNTIL: float = 0.0
_MULTI_SEARCH_PAGE_REQUEST_CACHE: Dict[str, Dict[str, Any]] = {}
_MULTI_SEARCH_PAGE_REQUEST_INFLIGHT: Dict[str, asyncio.Future] = {}


def _normalize_upstream_cache_text(value: Optional[str]) -> str:
    text = str(value or "").strip().lower()
    return re.sub(r"\s+", " ", text)


def _multi_page_request_cache_prune(now_mono: float) -> None:
    if not _MULTI_SEARCH_PAGE_REQUEST_CACHE:
        return
    expired = []
    for key, entry in _MULTI_SEARCH_PAGE_REQUEST_CACHE.items():
        try:
            expires_at = float(entry.get("expires_at") or 0.0)
        except Exception:
            expires_at = 0.0
        if expires_at <= now_mono:
            expired.append(key)
    for key in expired:
        _MULTI_SEARCH_PAGE_REQUEST_CACHE.pop(key, None)


def _multi_page_request_cache_get(cache_key: str) -> Optional[Dict[str, Any]]:
    if not cache_key:
        return None
    now_mono = time.monotonic()
    _multi_page_request_cache_prune(now_mono)
    entry = _MULTI_SEARCH_PAGE_REQUEST_CACHE.get(cache_key)
    if not isinstance(entry, dict):
        return None
    value = entry.get("value")
    return copy.deepcopy(value) if isinstance(value, dict) else None


def _multi_page_request_cache_put(cache_key: str, result: Dict[str, Any]) -> None:
    if not cache_key:
        return
    ttl_seconds = max(0.0, float(MULTI_SEARCH_PAGE_REQUEST_DEDUP_TTL_SECONDS))
    if ttl_seconds <= 0:
        return
    now_mono = time.monotonic()
    _MULTI_SEARCH_PAGE_REQUEST_CACHE[cache_key] = {
        "value": copy.deepcopy(result),
        "expires_at": now_mono + ttl_seconds,
    }
    _multi_page_request_cache_prune(now_mono)


def _build_multi_page_request_dedup_key(
    payload: "FindProductsMultiPayload",
    source_normalized: str,
    page_request_id: Optional[str],
) -> Optional[str]:
    rid = str(page_request_id or "").strip()
    if not rid:
        return None
    filters = payload.search
    normalized_query = _normalize_upstream_cache_text(getattr(filters, "query", "") or "")
    return "|".join(
        [
            source_normalized or "unknown",
            rid,
            normalized_query,
            str(int(getattr(filters, "limit", 10) or 10)),
            str(int(getattr(filters, "page", 1) or 1)),
            "1" if bool(getattr(filters, "in_stock_only", False)) else "0",
        ]
    )


def _build_multi_upstream_cache_key(
    payload: "FindProductsMultiPayload",
    request_metadata: Optional[Dict[str, Any]],
    source_normalized: str,
) -> str:
    md = request_metadata or {}
    scope = md.get("scope") if isinstance(md.get("scope"), dict) else {}
    filters = payload.search
    raw_query = _normalize_upstream_cache_text(getattr(filters, "query", "") or "")
    return "|".join(
        [
            source_normalized or "unknown",
            str(scope.get("catalog") or ""),
            str(scope.get("region") or ""),
            str(scope.get("language") or ""),
            raw_query,
            str(int(getattr(filters, "limit", 10) or 10)),
            str(int(getattr(filters, "page", 1) or 1)),
            "1" if bool(getattr(filters, "in_stock_only", False)) else "0",
        ]
    )


def _multi_upstream_cache_prune(now_mono: float) -> None:
    if not _MULTI_SEARCH_UPSTREAM_CACHE:
        return

    expired_keys: List[str] = []
    for key, entry in _MULTI_SEARCH_UPSTREAM_CACHE.items():
        try:
            expires_at = float(entry.get("expires_at") or 0.0)
        except Exception:
            expires_at = 0.0
        if expires_at <= now_mono:
            expired_keys.append(key)
    for key in expired_keys:
        _MULTI_SEARCH_UPSTREAM_CACHE.pop(key, None)

    max_entries = max(1, int(MULTI_SEARCH_UPSTREAM_CACHE_MAX_ENTRIES))
    overflow = len(_MULTI_SEARCH_UPSTREAM_CACHE) - max_entries
    if overflow <= 0:
        return
    oldest_keys = sorted(
        _MULTI_SEARCH_UPSTREAM_CACHE.keys(),
        key=lambda cache_key: float(_MULTI_SEARCH_UPSTREAM_CACHE[cache_key].get("stored_at") or 0.0),
    )[:overflow]
    for key in oldest_keys:
        _MULTI_SEARCH_UPSTREAM_CACHE.pop(key, None)


def _multi_upstream_cache_get(cache_key: str) -> Optional[Dict[str, Any]]:
    now_mono = time.monotonic()
    _multi_upstream_cache_prune(now_mono)
    entry = _MULTI_SEARCH_UPSTREAM_CACHE.get(cache_key)
    if not isinstance(entry, dict):
        return None

    try:
        expires_at = float(entry.get("expires_at") or 0.0)
    except Exception:
        expires_at = 0.0
    if expires_at <= now_mono:
        _MULTI_SEARCH_UPSTREAM_CACHE.pop(cache_key, None)
        return None

    result = entry.get("result")
    if not isinstance(result, dict):
        _MULTI_SEARCH_UPSTREAM_CACHE.pop(cache_key, None)
        return None

    remaining_ttl = max(0.0, expires_at - now_mono)
    return {
        "kind": str(entry.get("kind") or "result"),
        "remaining_ttl_seconds": remaining_ttl,
        "result": copy.deepcopy(result),
    }


def _multi_upstream_cache_put(cache_key: str, result: Dict[str, Any], kind: str) -> None:
    if not cache_key or not isinstance(result, dict):
        return

    if kind == "error":
        ttl_seconds = float(MULTI_SEARCH_UPSTREAM_ERROR_CACHE_TTL_SECONDS)
    elif kind == "empty":
        ttl_seconds = float(MULTI_SEARCH_UPSTREAM_EMPTY_CACHE_TTL_SECONDS)
    else:
        ttl_seconds = float(MULTI_SEARCH_UPSTREAM_RESPONSE_CACHE_TTL_SECONDS)

    if ttl_seconds <= 0:
        return

    now_mono = time.monotonic()
    _MULTI_SEARCH_UPSTREAM_CACHE[cache_key] = {
        "kind": kind,
        "stored_at": now_mono,
        "expires_at": now_mono + ttl_seconds,
        "result": copy.deepcopy(result),
    }
    _multi_upstream_cache_prune(now_mono)


def _multi_upstream_circuit_prune(now_mono: float) -> None:
    if not _MULTI_SEARCH_UPSTREAM_FAILURE_EVENTS:
        return
    window_seconds = _multi_upstream_circuit_window_seconds()
    threshold_ts = now_mono - window_seconds
    _MULTI_SEARCH_UPSTREAM_FAILURE_EVENTS[:] = [
        ts for ts in _MULTI_SEARCH_UPSTREAM_FAILURE_EVENTS if ts >= threshold_ts
    ]


def _multi_upstream_circuit_is_open(now_mono: Optional[float] = None) -> bool:
    now = float(now_mono) if now_mono is not None else time.monotonic()
    return float(_MULTI_SEARCH_UPSTREAM_CIRCUIT_OPEN_UNTIL or 0.0) > now


def _multi_upstream_record_outcome(success: bool, *, timeout: bool = False) -> None:
    global _MULTI_SEARCH_UPSTREAM_CIRCUIT_OPEN_UNTIL

    now_mono = time.monotonic()
    _multi_upstream_circuit_prune(now_mono)

    if success:
        _MULTI_SEARCH_UPSTREAM_FAILURE_EVENTS.clear()
        _MULTI_SEARCH_UPSTREAM_CIRCUIT_OPEN_UNTIL = 0.0
        set_catalog_upstream_circuit(surface="shopping", state="closed")
        return

    if timeout and _multi_upstream_circuit_open_on_timeout():
        open_seconds = _multi_upstream_circuit_open_seconds()
        _MULTI_SEARCH_UPSTREAM_CIRCUIT_OPEN_UNTIL = max(
            float(_MULTI_SEARCH_UPSTREAM_CIRCUIT_OPEN_UNTIL or 0.0),
            now_mono + open_seconds,
        )
        _MULTI_SEARCH_UPSTREAM_FAILURE_EVENTS[:] = [now_mono]
        set_catalog_upstream_circuit(surface="shopping", state="open")
        return

    _MULTI_SEARCH_UPSTREAM_FAILURE_EVENTS.append(now_mono)
    _multi_upstream_circuit_prune(now_mono)
    failure_threshold = _multi_upstream_circuit_failure_threshold()
    if len(_MULTI_SEARCH_UPSTREAM_FAILURE_EVENTS) < failure_threshold:
        set_catalog_upstream_circuit(surface="shopping", state="closed")
        return

    open_seconds = _multi_upstream_circuit_open_seconds()
    _MULTI_SEARCH_UPSTREAM_CIRCUIT_OPEN_UNTIL = max(
        float(_MULTI_SEARCH_UPSTREAM_CIRCUIT_OPEN_UNTIL or 0.0),
        now_mono + open_seconds,
    )
    set_catalog_upstream_circuit(surface="shopping", state="open")


def _resolve_multi_upstream_timeout_seconds(is_shopping_surface: bool) -> float:
    timeout_seconds = float(MULTI_SEARCH_UPSTREAM_FALLBACK_TIMEOUT_SECONDS)
    if is_shopping_surface:
        timeout_seconds = min(
            timeout_seconds,
            _multi_upstream_shopping_timeout_cap_seconds(),
        )
    return max(0.3, timeout_seconds)


def _build_multi_delegate_empty_result(
    *,
    page: int,
    force_cache_only: bool,
    base_merchant_fanout_enabled: bool,
    creator_id: Optional[str],
    creator_name: Optional[str],
    upstream_timeout_seconds: float,
    upstream_attempted: bool,
    upstream_circuit_open: bool,
) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {
        "query_source": "agent_products_resolver_fallback_empty",
        "fetched_at": datetime.utcnow().isoformat(),
        "merchants_searched": 0,
        "merchants_scanned": 0,
        "merchant_scan_limited": False,
        "force_cache_only": force_cache_only,
        "base_merchant_fanout_enabled": base_merchant_fanout_enabled,
        "creator_id": creator_id,
        "creator_name": creator_name,
        "history_boost_applied": False,
        "upstream_fallback_configured": bool(MULTI_SEARCH_UPSTREAM_FALLBACK_BASE_URL),
        "upstream_fallback_attempted": bool(upstream_attempted),
        "upstream_timeout_seconds": float(upstream_timeout_seconds),
    }
    if upstream_circuit_open:
        metadata["upstream_circuit_open"] = True
    return {
        "products": [],
        "total": 0,
        "page": page,
        "page_size": 0,
        "reply": None,
        "metadata": metadata,
    }


def _allow_local_fallback_after_delegate_fail(request_metadata: Optional[Dict[str, Any]]) -> bool:
    md = request_metadata or {}
    source_normalized = _normalize_surface_source(md.get("source"))

    remaining_seconds: Optional[float] = None
    try:
        if md.get("remaining_budget_seconds") is not None:
            remaining_seconds = float(md.get("remaining_budget_seconds"))
        elif md.get("remaining_budget_ms") is not None:
            remaining_seconds = float(md.get("remaining_budget_ms")) / 1000.0
    except Exception:
        remaining_seconds = None

    if (
        MULTI_SEARCH_AURORA_FORCE_LOCAL_FALLBACK_ON_DELEGATE_FAIL
        and "aurora" in source_normalized
    ):
        if remaining_seconds is None:
            return True
        return remaining_seconds >= 0.2

    if not _catalog_rel_v2_enabled():
        return False
    if not CATALOG_RELIABILITY_V2_LOCAL_FALLBACK_ON_DELEGATE_FAIL:
        return False

    if remaining_seconds is None:
        return True
    return remaining_seconds >= float(CATALOG_UPSTREAM_V2_LOCAL_FALLBACK_MIN_BUDGET_SECONDS)


def _resolve_multi_force_cache_only(source: Optional[str], is_creator_surface: bool) -> bool:
    if is_creator_surface:
        return MULTI_SEARCH_FORCE_CACHE_ONLY_CREATOR
    if _is_shopping_multi_source(source):
        return MULTI_SEARCH_FORCE_CACHE_ONLY_SHOPPING
    return MULTI_SEARCH_FORCE_CACHE_ONLY_DEFAULT


def _resolve_multi_base_merchant_fanout(source: Optional[str], is_creator_surface: bool) -> bool:
    if is_creator_surface:
        return MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT_CREATOR
    if _is_shopping_multi_source(source):
        return MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT_SHOPPING
    return MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT_DEFAULT


class SearchFilters(BaseModel):
    merchant_id: str = Field(..., description="Merchant ID")
    query: str = Field("", description="Search query, empty string means 'all products'")
    category: Optional[str] = Field(None, description="Optional category filter")
    price_min: Optional[float] = Field(None, description="Minimum price filter")
    price_max: Optional[float] = Field(None, description="Maximum price filter")
    page: int = Field(1, ge=1, description="Page number (1-based)")
    # Allow larger requested limits; internal logic clamps to the public contract max (200).
    limit: int = Field(20, ge=1, description="Page size (internally clamped to max 200)")


class FindProductsPayload(BaseModel):
    search: SearchFilters

class MultiSearchFilters(BaseModel):
    query: str = Field("", description="Search query, empty string means 'all products'")
    category: Optional[str] = Field(None, description="Optional category filter")
    price_min: Optional[float] = Field(None, description="Minimum price filter")
    price_max: Optional[float] = Field(None, description="Maximum price filter")
    page: int = Field(1, ge=1, description="Page number (1-based)")
    # Front-ends may request above 200; we clamp internally to 200.
    limit: int = Field(20, ge=1, description="Page size (internally clamped to max 200)")
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

    model_config = ConfigDict(populate_by_name=True)


class FindProductsMultiPayload(BaseModel):
    search: MultiSearchFilters
    user: Optional[UserIntent] = None
    metadata: Optional[RequestMetadata] = None
    creator_id: Optional[str] = Field(None, alias="creatorId", description="Optional creator context to scope results")
    intent_safety: Optional[Dict[str, Any]] = Field(
        None,
        description="Optional structured intent safety hints from upstream (e.g. high_level_intent, forbid/filter adult).",
    )

    model_config = ConfigDict(populate_by_name=True)


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


class OffersResolveProductRef(BaseModel):
    """
    Standardized product reference for offer resolution.

    - sku_id: preferred when available (variant-level resolution)
    - product_id: fallback when sku_id is missing
    """

    product_id: Optional[str] = Field(None, description="Product id or external_product_id")
    sku_id: Optional[str] = Field(None, description="SKU / variant id")
    merchant_id: Optional[str] = Field(None, description="Optional merchant scope for deterministic resolution")


class OffersResolvePayload(BaseModel):
    product: OffersResolveProductRef
    limit: int = Field(10, ge=1, le=30, description="Max offers to return")
    market: Optional[str] = Field(None, description="Market for outbound allowlist and UTM")
    tool: Optional[str] = Field(None, description="Tool identifier for outbound allowlist and UTM")


def _normalize_find_products_payload(raw_payload: Dict[str, Any]) -> Dict[str, Any]:
    payload = raw_payload if isinstance(raw_payload, dict) else {}
    search_raw = payload.get("search")
    if isinstance(search_raw, dict):
        search = dict(search_raw)
    else:
        search = dict(payload)
    return {"search": search}


def _normalize_find_products_multi_payload(raw_payload: Dict[str, Any]) -> Dict[str, Any]:
    payload = raw_payload if isinstance(raw_payload, dict) else {}
    search_raw = payload.get("search")
    if isinstance(search_raw, dict):
        search = dict(search_raw)
    else:
        search = {}
        for key in (
            "query",
            "category",
            "price_min",
            "price_max",
            "page",
            "limit",
            "in_stock_only",
        ):
            if key in payload:
                search[key] = payload.get(key)
    normalized: Dict[str, Any] = {"search": search}
    for key in ("user", "metadata", "creator_id", "creatorId", "intent_safety"):
        if key in payload:
            normalized[key] = payload.get(key)
    return normalized


def _normalize_offers_resolve_payload(raw_payload: Dict[str, Any]) -> Dict[str, Any]:
    payload = raw_payload if isinstance(raw_payload, dict) else {}
    product_raw = payload.get("product")
    if isinstance(product_raw, dict):
        product = dict(product_raw)
    else:
        product = {}

    for key in ("product_id", "productId", "sku_id", "skuId", "merchant_id", "merchantId"):
        if key in payload and key not in product:
            product[key] = payload.get(key)

    normalized: Dict[str, Any] = {"product": product}
    for key in ("limit", "market", "tool"):
        if key in payload:
            normalized[key] = payload.get(key)
    return normalized


def _safe_lower(s: Any) -> str:
    return str(s or "").strip().lower()


def _coerce_int(v: Any) -> Optional[int]:
    try:
        if v is None:
            return None
        return int(v)
    except Exception:
        return None


def _coerce_float(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        return float(v)
    except Exception:
        return None


def _row_to_dict(row: Any) -> Dict[str, Any]:
    if isinstance(row, dict):
        return row
    mapping = getattr(row, "_mapping", None)
    if mapping is not None:
        try:
            return dict(mapping)
        except Exception:
            pass
    try:
        return dict(row)
    except Exception:
        return {}


def _extract_price_currency_from_variant(v: Dict[str, Any], fallback_currency: str) -> tuple[Optional[float], str]:
    price = (
        v.get("price_amount")
        if v.get("price_amount") is not None
        else v.get("price")
        if v.get("price") is not None
        else v.get("amount")
        if v.get("amount") is not None
        else None
    )
    currency = v.get("price_currency") or v.get("currency") or v.get("currency_code") or fallback_currency
    return (_coerce_float(price), str(currency or fallback_currency).upper())


def _seed_variants(seed_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = seed_data.get("variants")
    if isinstance(raw, list):
        return [it for it in raw if isinstance(it, dict)]
    return []


def _seed_image_url(row: Dict[str, Any], seed_data: Dict[str, Any]) -> Optional[str]:
    for key in ("image_url",):
        v = row.get(key) or seed_data.get(key)
        if isinstance(v, str) and v.startswith(("http://", "https://")):
            return v
    snap = seed_data.get("snapshot")
    if isinstance(snap, dict):
        v = snap.get("image_url")
        if isinstance(v, str) and v.startswith(("http://", "https://")):
            return v
    return None


def _seed_display_name(row: Dict[str, Any], seed_data: Dict[str, Any]) -> str:
    for key in ("merchant_display_name", "merchant_name", "brand", "vendor", "store_name"):
        v = row.get(key) or seed_data.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    domain = row.get("domain") or seed_data.get("domain")
    if isinstance(domain, str) and domain.strip():
        return domain.strip()
    return "External"


def _seed_offer_variant_id(v: Dict[str, Any]) -> str:
    raw = (
        v.get("variant_id")
        or v.get("variantId")
        or v.get("sku")
        or v.get("sku_id")
        or v.get("id")
        or ""
    )
    return str(raw).strip()


def _classify_db_reason_code(exc: Exception) -> str:
    msg = str(exc or "").lower()
    exc_type = type(exc).__name__.lower()
    if (
        "ambiguousparametererror" in exc_type
        or "ambiguous parameter" in msg
        or "could not determine data type of parameter" in msg
    ):
        return "db_ambiguous_param"
    if isinstance(exc, asyncio.TimeoutError) or "timeout" in msg:
        return "db_query_timeout"
    if "does not exist" in msg or "undefined table" in msg or "undefined column" in msg or "relation" in msg:
        return "db_schema"
    if "password authentication failed" in msg or "authentication failed" in msg or "permission denied" in msg:
        return "db_auth"
    if "too many connections" in msg or "connection refused" in msg or "connection reset" in msg:
        return "db_connection"
    return "db_error"


def _expand_ref_aliases(raw_ref: Optional[str]) -> List[str]:
    """
    Expand a product/sku reference into stable aliases:
    - raw string
    - URL-derived id (e.g. /products/9886499864904?merchant_id=...)
    - Shopify GID <-> numeric id variants
    """
    raw = str(raw_ref or "").strip()
    if not raw:
        return []

    aliases: List[str] = []

    def _add(v: Optional[str]) -> None:
        s = str(v or "").strip()
        if s and s not in aliases:
            aliases.append(s)

    _add(raw)

    # URL form: https://.../products/<id>?...
    if raw.startswith(("http://", "https://")):
        try:
            parsed = urlparse(raw)
            path_parts = [p for p in (parsed.path or "").split("/") if p]
            for idx, part in enumerate(path_parts):
                if part == "products" and idx + 1 < len(path_parts):
                    _add(path_parts[idx + 1])
            q = parsed.query or ""
            if q:
                # Lightweight query parser to avoid importing parse_qs in hot path.
                for item in q.split("&"):
                    if "=" not in item:
                        continue
                    k, v = item.split("=", 1)
                    if k in {"product_id", "productId", "id", "variant_id", "variantId", "sku_id", "skuId"}:
                        _add(v)
        except Exception:
            pass

    # GID form: gid://shopify/Product/123456 or ProductVariant.
    gid_match = re.search(r"gid://shopify/(?:Product|ProductVariant)/(\d+)", raw)
    if gid_match:
        _add(gid_match.group(1))

    # Numeric id can map to common Shopify GID aliases.
    if raw.isdigit():
        _add(f"gid://shopify/Product/{raw}")
        _add(f"gid://shopify/ProductVariant/{raw}")

    # Generic suffix extraction, useful when IDs are wrapped (e.g. "shopify:988...").
    if ":" in raw:
        _add(raw.split(":")[-1])
    if "/" in raw:
        _add(raw.rstrip("/").split("/")[-1])

    return aliases[:20]


def _resolve_offers_merchant_scope(
    *,
    payload: OffersResolvePayload,
    request_metadata: Optional[Dict[str, Any]],
) -> Optional[str]:
    # Explicit scope in payload wins.
    scoped = str(payload.product.merchant_id or "").strip() or None
    if scoped:
        return scoped
    meta = request_metadata if isinstance(request_metadata, dict) else {}
    for key in ("merchant_id", "merchantId"):
        val = str(meta.get(key) or "").strip()
        if val:
            return val
    merchant_scope = meta.get("merchant_scope")
    if isinstance(merchant_scope, list):
        for val in merchant_scope:
            s = str(val or "").strip()
            if s:
                return s
    if isinstance(merchant_scope, str):
        s = merchant_scope.strip()
        if s:
            return s
    return None


async def _handle_offers_resolve(
    payload: OffersResolvePayload,
    request_metadata: Optional[Dict[str, Any]],
    background_tasks: BackgroundTasks,
) -> Dict[str, Any]:
    """
    Resolve purchasable offers for a given sku_id/product_id.

    Contract goal: internal checkout offers are primary; external outbound links are fallback.
    """
    from db.database import database

    started = time.perf_counter()

    product_id = str(payload.product.product_id or "").strip() or None
    sku_id = str(payload.product.sku_id or "").strip() or None
    market_hint = str(payload.market or "").strip() or None
    tool_hint = str(payload.tool or "").strip() or None
    limit = int(payload.limit or 10)
    attached_seed_limit = min(max(limit * 6, 40), 240)
    merchant_scope = _resolve_offers_merchant_scope(
        payload=payload,
        request_metadata=request_metadata,
    )
    product_id_aliases = _expand_ref_aliases(product_id)
    sku_id_aliases = _expand_ref_aliases(sku_id)

    def _conf(kind: str, confidence: float, reason: str, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return {
            "kind": kind,
            "confidence": max(0.0, min(1.0, float(confidence))),
            "reason": reason,
            **(extra or {}),
        }

    mapping_candidates: List[Dict[str, Any]] = []
    offers: List[Dict[str, Any]] = []
    source_status: List[Dict[str, Any]] = []
    seen_external_offer_ids: set[str] = set()

    def _public_reason_code(raw_code: Optional[str]) -> str:
        code = str(raw_code or "").strip().lower()
        if code == "ok":
            return "OK"
        if code == "no_candidates":
            return "NO_CANDIDATES"
        if code.startswith("db_"):
            return "DB_ERROR"
        if code == "upstream_timeout":
            return "UPSTREAM_TIMEOUT"
        if code.startswith("upstream_"):
            return "UPSTREAM_ERROR"
        if code.startswith("skipped"):
            return "SKIPPED"
        return code.upper() if code else "UNKNOWN"

    def _record_source(
        *,
        source: str,
        status: str,
        reason_code: str,
        source_started: float,
        row_count: Optional[int] = None,
        error: Optional[str] = None,
        query: Optional[str] = None,
    ) -> None:
        entry: Dict[str, Any] = {
            "source": source,
            "status": status,
            "reason_code": reason_code,
            "reason": _public_reason_code(reason_code),
            "ok": status == "ok",
            "latency_ms": int((time.perf_counter() - source_started) * 1000),
        }
        if row_count is not None:
            entry["row_count"] = int(row_count)
        if error:
            entry["error"] = str(error)[:500]
        if query:
            entry["query"] = query
        source_status.append(entry)

    async def _fetch_attached_seed_rows(
        *,
        merchant_id: Optional[str],
        platform: Optional[str] = None,
        product_aliases: Optional[List[str]] = None,
        variant_aliases: Optional[List[str]] = None,
    ) -> List[Any]:
        attached_merchant_id = str(merchant_id or "").strip() or None
        if not attached_merchant_id:
            return []

        pid_aliases = [str(alias or "").strip() for alias in (product_aliases or []) if str(alias or "").strip()]
        sku_aliases = [str(alias or "").strip() for alias in (variant_aliases or []) if str(alias or "").strip()]
        if not pid_aliases and not sku_aliases:
            return []

        attached_platform = str(platform or "").strip() or None
        params: Dict[str, Any] = {
            "limit": attached_seed_limit,
            "attached_prefix": f"{attached_merchant_id}|%",
        }
        match_clauses: List[str] = []

        for idx, pid_alias in enumerate(pid_aliases[:8]):
            pid_key = f"attached_pid_{idx}"
            if attached_platform:
                params[pid_key] = f"{attached_merchant_id}|{attached_platform}|{pid_alias}"
                match_clauses.append(f"attached_product_key = :{pid_key}")
            else:
                params[pid_key] = f"{attached_merchant_id}|%|{pid_alias}"
                match_clauses.append(f"attached_product_key LIKE :{pid_key}")

        for idx, sku_alias in enumerate(sku_aliases[:8]):
            sku_key = f"attached_sku_{idx}"
            params[sku_key] = sku_alias
            match_clauses.append(f"attached_variant_id = :{sku_key}")

        if not match_clauses:
            return []

        rows = await asyncio.wait_for(
            database.fetch_all(
                f"""
                SELECT *
                FROM external_product_seeds
                WHERE status = 'active'
                  AND attached_product_key IS NOT NULL
                  AND attached_product_key LIKE :attached_prefix
                  AND ({' OR '.join(match_clauses)})
                ORDER BY updated_at DESC, created_at DESC
                LIMIT :limit
                """,
                params,
            ),
            timeout=min(OFFERS_RESOLVE_SEED_QUERY_TIMEOUT_SECONDS, 1.0),
        )
        return list(rows or [])

    async def _prefetch_canonical_internal_context() -> Optional[Dict[str, Any]]:
        if merchant_scope or (not product_id_aliases and not sku_id_aliases):
            return None

        rows: List[Any] = []
        if product_id_aliases:
            rows = await asyncio.wait_for(
                database.fetch_all(
                    """
                    SELECT merchant_id, platform, platform_product_id, product_data
                    FROM products_cache
                    WHERE (expires_at IS NULL OR expires_at > NOW())
                      AND (
                        platform_product_id = ANY(:pid_aliases)
                        OR product_data->>'id' = ANY(:pid_aliases)
                        OR product_data->>'product_id' = ANY(:pid_aliases)
                      )
                    ORDER BY cached_at DESC
                    LIMIT 20
                    """,
                    {
                        "pid_aliases": product_id_aliases,
                    },
                ),
                timeout=min(OFFERS_RESOLVE_INTERNAL_PID_QUERY_TIMEOUT_SECONDS, 0.5),
            )

        if not rows and sku_id_aliases:
            rows = await asyncio.wait_for(
                database.fetch_all(
                    """
                    SELECT merchant_id, platform, platform_product_id, product_data
                    FROM products_cache
                    WHERE (expires_at IS NULL OR expires_at > NOW())
                      AND EXISTS (
                        SELECT 1
                        FROM jsonb_array_elements(
                          CASE
                            WHEN jsonb_typeof(product_data::jsonb->'variants') = 'array'
                            THEN product_data::jsonb->'variants'
                            ELSE '[]'::jsonb
                          END
                        ) AS variant
                        WHERE COALESCE(
                          variant->>'variant_id',
                          variant->>'id',
                          variant->>'sku',
                          variant->>'sku_id'
                        ) = ANY(:sku_aliases)
                      )
                    ORDER BY cached_at DESC
                    LIMIT 20
                    """,
                    {
                        "sku_aliases": sku_id_aliases,
                    },
                ),
                timeout=min(OFFERS_RESOLVE_INTERNAL_SKU_EXACT_QUERY_TIMEOUT_SECONDS, 0.5),
            )

        first_row = _row_to_dict(rows[0]) if rows else {}
        merchant_id = str(first_row.get("merchant_id") or "").strip() or None
        platform = str(first_row.get("platform") or "").strip() or None
        product_data = first_row.get("product_data")
        if isinstance(product_data, str):
            try:
                product_data = json.loads(product_data)
            except Exception:
                product_data = None
        if not isinstance(product_data, dict):
            product_data = {}
        canonical_product_id = (
            str(
                product_data.get("id")
                or product_data.get("product_id")
                or first_row.get("platform_product_id")
                or ""
            ).strip()
            or None
        )
        if not merchant_id or not canonical_product_id:
            return None

        variant_aliases: List[str] = []
        variants = product_data.get("variants") if isinstance(product_data.get("variants"), list) else []
        for variant in variants[:12]:
            if not isinstance(variant, dict):
                continue
            variant_id = str(
                variant.get("variant_id")
                or variant.get("id")
                or variant.get("sku")
                or variant.get("sku_id")
                or ""
            ).strip()
            if variant_id and variant_id not in variant_aliases:
                variant_aliases.append(variant_id)

        return {
            "merchant_id": merchant_id,
            "platform": platform,
            "product_id": canonical_product_id,
            "variant_aliases": variant_aliases,
        }

    async def _append_external_offers_from_seed_rows(seed_rows: List[Any]) -> None:
        for row in seed_rows:
            row_dict = _row_to_dict(row)
            blocked, gate_status = await should_block_external_referral_runtime(
                row_dict,
                matched_via="agent_shop_gateway",
            )
            if blocked:
                mapping_candidates.append(
                    _conf(
                        "external_seed",
                        0.0,
                        "filtered_external_seed",
                        {
                            "seed_id": row_dict.get("id"),
                            "blockers": list(gate_status.blocker_anomaly_types),
                        },
                    )
                )
                continue
            seed_data = _ensure_seed_data_obj(row_dict.get("seed_data"))
            variants = _seed_variants(seed_data)

            matched_variants: List[Dict[str, Any]] = []
            if sku_id:
                for v in variants:
                    vid = _seed_offer_variant_id(v)
                    if vid and vid == sku_id:
                        matched_variants.append(v)
            else:
                matched_variants = variants[: min(12, max(1, limit))]

            if not matched_variants:
                matched_variants = [{}]

            destination_url = row_dict.get("destination_url") or seed_data.get("destination_url") or ""
            canonical_url = row_dict.get("canonical_url") or seed_data.get("canonical_url") or destination_url
            if not isinstance(destination_url, str) or not destination_url.startswith(("http://", "https://")):
                continue

            seed_id = str(row_dict.get("id") or "").strip() or None
            if not seed_id:
                continue

            used_market = str(row_dict.get("market") or market_hint or "US")
            used_tool = str(row_dict.get("tool") or tool_hint or "*")

            for v in matched_variants:
                vid = _seed_offer_variant_id(v) or (sku_id or "∅")
                offer_id = f"of:external_seed:{seed_id}:{vid}"
                if offer_id in seen_external_offer_ids:
                    continue
                seen_external_offer_ids.add(offer_id)

                price_amount, currency = _extract_price_currency_from_variant(
                    v,
                    fallback_currency=str(row_dict.get("price_currency") or seed_data.get("price_currency") or "USD"),
                )
                if price_amount is None:
                    price_amount = _coerce_float(row_dict.get("price_amount") or seed_data.get("price_amount") or 0) or 0.0

                availability = (
                    v.get("availability")
                    or seed_data.get("availability")
                    or row_dict.get("availability")
                    or "unknown"
                )
                in_stock = True
                if isinstance(availability, str):
                    in_stock = availability.lower() not in {"out_of_stock", "outofstock", "sold_out"}

                redirect_url = await _make_external_redirect_url(
                    market=used_market,
                    tool=used_tool,
                    destination_url=str(canonical_url or destination_url),
                    utm_template=row_dict.get("utm_template") or seed_data.get("utm_template"),
                    ctx={
                        "seedId": seed_id,
                        "variantId": vid,
                        "eventType": "outbound_opened",
                        "source": "offers.resolve",
                        **({"skuId": sku_id} if sku_id else {}),
                        **({"productId": product_id} if product_id else {}),
                    },
                )
                if not redirect_url:
                    continue

                title = (
                    v.get("title")
                    or seed_data.get("title")
                    or row_dict.get("title")
                    or canonical_url
                    or destination_url
                )
                seller = _seed_display_name(row_dict, seed_data)
                shipping_days = _coerce_int(v.get("shipping_days") or v.get("shippingDays"))
                original_price = _coerce_float(v.get("original_price") or v.get("compare_at_price") or v.get("originalPrice"))

                confidence = 1.0 if (sku_id and vid in sku_id_aliases) else 0.8 if product_id else 0.6
                mapping_candidates.append(
                    _conf(
                        "external_seed",
                        confidence,
                        "matched_external_seed",
                        {
                            "seed_id": seed_id,
                            "variant_id": vid,
                            "external_product_id": row_dict.get("external_product_id") or seed_data.get("external_product_id"),
                            "domain": row_dict.get("domain") or seed_data.get("domain"),
                        },
                    )
                )

                external_offers.append(
                    {
                        "offer_id": offer_id,
                        "seller": seller,
                        "price": price_amount,
                        "currency": currency,
                        **({"original_price": original_price} if original_price is not None else {}),
                        **({"shipping_days": shipping_days} if shipping_days is not None else {}),
                        "in_stock": bool(in_stock),
                        "purchase_route": "affiliate_outbound",
                        "affiliate_url": redirect_url,
                        "internal_checkout_items": None,
                        "confidence": confidence,
                        "source": {
                            "type": "external_seed",
                            "seed_id": seed_id,
                            "external_product_id": row_dict.get("external_product_id") or seed_data.get("external_product_id"),
                            "variant_id": vid,
                            "title": title,
                            "image_url": _seed_image_url(row_dict, seed_data),
                            "canonical_url": canonical_url,
                            "destination_url": destination_url,
                            "domain": row_dict.get("domain") or seed_data.get("domain"),
                        },
                    }
                )
                if len(external_offers) >= limit:
                    return

    # 1) External offers from external seeds (affiliate outbound)
    external_offers: List[Dict[str, Any]] = []
    external_started = time.perf_counter()
    try:
        query_label = "external_seed_by_ref"
        where_clauses = ["status = 'active'"]
        params: Dict[str, Any] = {"limit": attached_seed_limit}
        seed_rows: List[Any] = []

        if merchant_scope and (product_id_aliases or sku_id_aliases):
            seed_rows = await _fetch_attached_seed_rows(
                merchant_id=merchant_scope,
                product_aliases=product_id_aliases,
                variant_aliases=sku_id_aliases,
            )
            if seed_rows:
                query_label = "external_seed_by_attached_ref"

        if not seed_rows:
            prefetched_internal = await _prefetch_canonical_internal_context()
            if prefetched_internal:
                retry_variant_aliases = [
                    str(alias or "").strip()
                    for alias in (
                        [sku_id]
                        + list(prefetched_internal.get("variant_aliases") or [])
                        + sku_id_aliases
                    )
                    if str(alias or "").strip()
                ]
                seed_rows = await _fetch_attached_seed_rows(
                    merchant_id=str(prefetched_internal.get("merchant_id") or "").strip() or None,
                    platform=str(prefetched_internal.get("platform") or "").strip() or None,
                    product_aliases=[
                        str(prefetched_internal.get("product_id") or "").strip() or None
                    ] + product_id_aliases,
                    variant_aliases=retry_variant_aliases,
                )
                if seed_rows:
                    query_label = "external_seed_by_canonical_attached_prefetch"

        if not seed_rows and product_id_aliases:
            pid_clause: List[str] = []
            for idx, pid_alias in enumerate(product_id_aliases[:8]):
                pid_key = f"pid_{idx}"
                like_key = f"pid_like_{idx}"
                params[pid_key] = pid_alias
                params[like_key] = f"%{_safe_lower(pid_alias)}%"
                pid_clause.append(
                    "("
                    f"external_product_id = :{pid_key}"
                    f" OR LOWER(COALESCE(title,'')) LIKE :{like_key}"
                    f" OR LOWER(COALESCE(canonical_url,'')) LIKE :{like_key}"
                    f" OR LOWER(COALESCE(destination_url,'')) LIKE :{like_key}"
                    f" OR LOWER(CAST(seed_data AS TEXT)) LIKE :{like_key}"
                    ")"
                )
            where_clauses.append("(" + " OR ".join(pid_clause) + ")")

        if not seed_rows and sku_id_aliases:
            sku_clause: List[str] = []
            for idx, sku_alias in enumerate(sku_id_aliases[:8]):
                key = f"sku_like_{idx}"
                params[key] = f"%{_safe_lower(sku_alias)}%"
                sku_clause.append(f"LOWER(CAST(seed_data AS TEXT)) LIKE :{key}")
            where_clauses.append("(" + " OR ".join(sku_clause) + ")")

        if not seed_rows and len(where_clauses) == 1:
            # No input: return empty (caller must provide sku_id or product_id).
            return {
                "status": "error",
                "error": {
                    "code": "MISSING_PRODUCT_REF",
                    "message": "offers.resolve requires product.sku_id or product.product_id",
                },
            }

        if not seed_rows:
            seed_rows = await asyncio.wait_for(
                database.fetch_all(
                    f"""
                    SELECT *
                    FROM external_product_seeds
                    WHERE {" AND ".join(where_clauses)}
                    ORDER BY updated_at DESC, created_at DESC
                    LIMIT :limit
                    """,
                    params,
                ),
                timeout=OFFERS_RESOLVE_SEED_QUERY_TIMEOUT_SECONDS,
            )

        await _append_external_offers_from_seed_rows(list(seed_rows or []))
        _record_source(
            source="external_product_seeds",
            status="ok",
            reason_code="ok",
            source_started=external_started,
            row_count=len(seed_rows or []),
            query=query_label,
        )
    except Exception as e:
        logger.info("offers.resolve.external.failed", extra={"error": str(e)})
        _record_source(
            source="external_product_seeds",
            status="error",
            reason_code=_classify_db_reason_code(e),
            source_started=external_started,
                error=type(e).__name__,
            query="external_seed_by_ref",
        )

    # 2) Internal checkout offers (primary)
    internal_offers: List[Dict[str, Any]] = []
    canonical_group_id: Optional[str] = None
    canonical_member: Optional[Dict[str, Any]] = None
    internal_first_checkout = True
    if internal_first_checkout:
        internal_started = time.perf_counter()
        try:
            internal_deadline = time.perf_counter() + OFFERS_RESOLVE_INTERNAL_TOTAL_BUDGET_SECONDS
            internal_budget_exhausted = False
            internal_sku_scan_skipped = False

            def _next_internal_timeout(default_timeout: float) -> Optional[float]:
                nonlocal internal_budget_exhausted
                remaining = internal_deadline - time.perf_counter()
                if remaining <= 0:
                    internal_budget_exhausted = True
                    return None
                return max(0.05, min(default_timeout, remaining))

            # First try direct product id match
            rows: List[Any] = []
            if product_id_aliases:
                pid_timeout_s = _next_internal_timeout(OFFERS_RESOLVE_INTERNAL_PID_QUERY_TIMEOUT_SECONDS)
                if pid_timeout_s is not None:
                    rows = await asyncio.wait_for(
                        database.fetch_all(
                            """
                            SELECT merchant_id, platform, platform_product_id, product_data
                            FROM products_cache
                            WHERE (expires_at IS NULL OR expires_at > NOW())
                              AND (CAST(:merchant_scope AS TEXT) IS NULL OR merchant_id = CAST(:merchant_scope AS TEXT))
                              AND (
                                platform_product_id = ANY(:pid_aliases)
                                OR product_data->>'id' = ANY(:pid_aliases)
                                OR product_data->>'product_id' = ANY(:pid_aliases)
                              )
                            ORDER BY cached_at DESC
                            LIMIT 80
                            """,
                            {
                                "merchant_scope": merchant_scope,
                                "pid_aliases": product_id_aliases,
                            },
                        ),
                        timeout=pid_timeout_s,
                    )

            # Exact SKU/variant lookup before LIKE fallback.
            if not rows and sku_id_aliases:
                sku_exact_timeout_s = _next_internal_timeout(OFFERS_RESOLVE_INTERNAL_SKU_EXACT_QUERY_TIMEOUT_SECONDS)
                if sku_exact_timeout_s is not None:
                    rows = await asyncio.wait_for(
                        database.fetch_all(
                            """
                            SELECT merchant_id, platform, platform_product_id, product_data
                            FROM products_cache
                            WHERE (expires_at IS NULL OR expires_at > NOW())
                              AND (CAST(:merchant_scope AS TEXT) IS NULL OR merchant_id = CAST(:merchant_scope AS TEXT))
                              AND EXISTS (
                                SELECT 1
                                FROM jsonb_array_elements(
                                  CASE
                                    WHEN jsonb_typeof(product_data::jsonb->'variants') = 'array'
                                    THEN product_data::jsonb->'variants'
                                    ELSE '[]'::jsonb
                                  END
                                ) AS variant
                                WHERE COALESCE(
                                  variant->>'variant_id',
                                  variant->>'id',
                                  variant->>'sku',
                                  variant->>'sku_id'
                                ) = ANY(:sku_aliases)
                              )
                            ORDER BY cached_at DESC
                            LIMIT 120
                            """,
                            {
                                "merchant_scope": merchant_scope,
                                "sku_aliases": sku_id_aliases,
                            },
                        ),
                        timeout=sku_exact_timeout_s,
                    )

            # Next try SKU-like match in cached JSON (bounded).
            if not rows and sku_id_aliases:
                if OFFERS_RESOLVE_ENABLE_SKU_TEXT_SCAN:
                    sku_scan_timeout_s = _next_internal_timeout(OFFERS_RESOLVE_INTERNAL_SKU_TEXT_SCAN_TIMEOUT_SECONDS)
                    if sku_scan_timeout_s is not None:
                        sku_clauses: List[str] = []
                        sku_params: Dict[str, Any] = {"merchant_scope": merchant_scope}
                        for idx, sku_alias in enumerate(sku_id_aliases[:8]):
                            key = f"sku_like_{idx}"
                            sku_params[key] = f"%{_safe_lower(sku_alias)}%"
                            sku_clauses.append(f"LOWER(CAST(product_data AS TEXT)) LIKE :{key}")
                        rows = await asyncio.wait_for(
                            database.fetch_all(
                                f"""
                                SELECT merchant_id, platform, platform_product_id, product_data
                                FROM products_cache
                                WHERE (expires_at IS NULL OR expires_at > NOW())
                                  AND (CAST(:merchant_scope AS TEXT) IS NULL OR merchant_id = CAST(:merchant_scope AS TEXT))
                                  AND ({' OR '.join(sku_clauses)})
                                ORDER BY cached_at DESC
                                LIMIT 120
                                """,
                                sku_params,
                            ),
                            timeout=sku_scan_timeout_s,
                        )
                else:
                    internal_sku_scan_skipped = True

            # Canonical product-group lookup when available.
            if rows:
                member_candidates = {
                    str(_row_to_dict(r).get("platform_product_id") or "").strip()
                    for r in rows
                }
                member_candidates.update(product_id_aliases)
                member_candidates = {v for v in member_candidates if v}

                if member_candidates:
                    group_rows: List[Any] = []
                    group_timeout_s = _next_internal_timeout(OFFERS_RESOLVE_INTERNAL_GROUP_QUERY_TIMEOUT_SECONDS)
                    if group_timeout_s is not None:
                        group_rows = await asyncio.wait_for(
                            database.fetch_all(
                                """
                                SELECT product_group_id, merchant_id, platform, platform_product_id, is_primary
                                FROM product_group_members
                                WHERE platform_product_id = ANY(:platform_product_ids)
                                ORDER BY is_primary DESC, merchant_id ASC
                                LIMIT 200
                                """,
                                {"platform_product_ids": list(member_candidates)},
                            ),
                            timeout=group_timeout_s,
                        )
                    if group_rows:
                        first = _row_to_dict(group_rows[0])
                        canonical_group_id = str(first.get("product_group_id") or "").strip() or None
                    if group_rows and canonical_group_id:
                        group_members = [
                            _row_to_dict(r)
                            for r in group_rows
                            if str(_row_to_dict(r).get("product_group_id") or "") == canonical_group_id
                        ]
                        for gm in group_members:
                            if bool(gm.get("is_primary")):
                                canonical_member = gm
                                break
                        if canonical_member is None and group_members:
                            canonical_member = group_members[0]
                        mapping_candidates.append(
                            _conf(
                                "canonical_group",
                                1.0,
                                "resolved_product_group",
                                {
                                    "product_group_id": canonical_group_id,
                                    "member_count": len(group_members),
                                },
                            )
                        )

                        member_mids = sorted(
                            {
                                str((gm.get("merchant_id") or "")).strip()
                                for gm in group_members
                                if str(gm.get("merchant_id") or "").strip()
                            }
                        )
                        member_pids = sorted(
                            {
                                str((gm.get("platform_product_id") or "")).strip()
                                for gm in group_members
                                if str(gm.get("platform_product_id") or "").strip()
                            }
                        )
                        if member_mids and member_pids:
                            group_cache_rows: List[Any] = []
                            group_cache_timeout_s = _next_internal_timeout(
                                OFFERS_RESOLVE_INTERNAL_GROUP_CACHE_TIMEOUT_SECONDS
                            )
                            if group_cache_timeout_s is not None:
                                group_cache_rows = await asyncio.wait_for(
                                    database.fetch_all(
                                        """
                                        SELECT merchant_id, platform, platform_product_id, product_data
                                        FROM products_cache
                                        WHERE merchant_id = ANY(:merchant_ids)
                                          AND platform_product_id = ANY(:platform_product_ids)
                                          AND (expires_at IS NULL OR expires_at > NOW())
                                        ORDER BY cached_at DESC
                                        LIMIT 200
                                        """,
                                        {
                                            "merchant_ids": member_mids,
                                            "platform_product_ids": member_pids,
                                        },
                                    ),
                                    timeout=group_cache_timeout_s,
                                )
                            # Merge + dedupe by (merchant_id, platform_product_id).
                            merged_rows: List[Dict[str, Any]] = []
                            seen_keys: set[str] = set()
                            for it in list(rows) + list(group_cache_rows or []):
                                rd = _row_to_dict(it)
                                k = f"{rd.get('merchant_id')}::{rd.get('platform_product_id')}"
                                if not rd or k in seen_keys:
                                    continue
                                seen_keys.add(k)
                                merged_rows.append(rd)
                            rows = merged_rows

            seen_internal_offer_ids: set[str] = set()
            for row in rows or []:
                row_dict = _row_to_dict(row)
                merchant_id = row_dict.get("merchant_id") or None
                platform = row_dict.get("platform") or "unknown"
                product_data = row_dict.get("product_data")
                if isinstance(product_data, str):
                    try:
                        product_data = json.loads(product_data)
                    except Exception:
                        continue
                if not isinstance(product_data, dict):
                    continue

                pid = str(
                    product_data.get("id")
                    or product_data.get("product_id")
                    or row_dict.get("platform_product_id")
                    or product_id
                    or ""
                ).strip() or None
                if not pid or not merchant_id:
                    continue

                variants = product_data.get("variants") if isinstance(product_data.get("variants"), list) else []
                chosen_variant: Dict[str, Any] = {}
                if sku_id_aliases and isinstance(variants, list):
                    for v in variants:
                        if not isinstance(v, dict):
                            continue
                        vid = str(v.get("variant_id") or v.get("id") or v.get("sku") or v.get("sku_id") or "").strip()
                        if vid and vid in sku_id_aliases:
                            chosen_variant = v
                            break
                if not chosen_variant and isinstance(variants, list) and variants:
                    first = variants[0] if isinstance(variants[0], dict) else None
                    chosen_variant = first or {}

                variant_id = str(
                    chosen_variant.get("variant_id")
                    or chosen_variant.get("id")
                    or chosen_variant.get("sku")
                    or chosen_variant.get("sku_id")
                    or ""
                ).strip() or (sku_id or None)
                offer_id = f"of:internal_checkout:{merchant_id}:{pid}:{variant_id or '∅'}"
                if offer_id in seen_internal_offer_ids:
                    continue
                seen_internal_offer_ids.add(offer_id)

                price = (
                    chosen_variant.get("price")
                    if chosen_variant.get("price") is not None
                    else product_data.get("price")
                )
                currency = product_data.get("currency") or product_data.get("currency_code") or "USD"
                price_amount = _coerce_float(price) or 0.0

                original_price = _coerce_float(
                    chosen_variant.get("compare_at_price")
                    or product_data.get("compare_at_price")
                )

                in_stock = True
                inv = _coerce_int(chosen_variant.get("inventory_quantity") or product_data.get("inventory_quantity"))
                if inv is not None:
                    in_stock = inv > 0
                if isinstance(product_data.get("in_stock"), bool):
                    in_stock = product_data.get("in_stock")

                seller = (
                    str(product_data.get("merchant_name") or product_data.get("store_name") or "").strip()
                    or str(merchant_id)
                )
                exact_pid_match = bool(product_id_aliases and pid in product_id_aliases)
                exact_sku_match = bool(sku_id_aliases and variant_id and variant_id in sku_id_aliases)
                confidence = 0.95 if (exact_pid_match or exact_sku_match) else 0.8 if sku_id else 0.7
                canonical_ref = (
                    f"pg:{canonical_group_id}"
                    if canonical_group_id
                    else f"pc:{merchant_id}:{platform}:{pid}"
                )
                mapping_candidates.append(
                    _conf(
                        "internal_product",
                        confidence,
                        "matched_products_cache",
                        {
                            "merchant_id": merchant_id,
                            "platform": platform,
                            "product_id": pid,
                            "variant_id": variant_id,
                            "canonical_ref": canonical_ref,
                            "product_group_id": canonical_group_id,
                        },
                    )
                )

                internal_offers.append(
                    {
                        "offer_id": offer_id,
                        "seller": seller,
                        "price": price_amount,
                        "currency": str(currency).upper(),
                        **({"original_price": original_price} if original_price is not None else {}),
                        "in_stock": bool(in_stock),
                        "purchase_route": "internal_checkout",
                        "affiliate_url": None,
                        "internal_checkout_items": [
                            {
                                "merchant_id": merchant_id,
                                "product_id": pid,
                                **({"variant_id": variant_id} if variant_id else {}),
                                "quantity": 1,
                            }
                        ],
                        "confidence": confidence,
                        "source": {
                            "type": "internal_product",
                            "merchant_id": merchant_id,
                            "platform": platform,
                            "product_id": pid,
                            "variant_id": variant_id,
                            "canonical_ref": canonical_ref,
                            "product_group_id": canonical_group_id,
                        },
                    }
                )
                if len(internal_offers) >= min(3, limit):
                    break
            internal_status = "ok" if rows else "empty"
            internal_reason_code = "ok" if rows else "no_candidates"
            if not rows and internal_budget_exhausted:
                internal_status = "skipped"
                internal_reason_code = "skipped_budget_exhausted"
            elif not rows and internal_sku_scan_skipped:
                internal_status = "skipped"
                internal_reason_code = "skipped_sku_text_scan_disabled"
            _record_source(
                source="products_cache",
                status=internal_status,
                reason_code=internal_reason_code,
                source_started=internal_started,
                row_count=len(rows or []),
                query="products_cache_by_alias",
            )
        except Exception as e:
            logger.info("offers.resolve.internal.failed", extra={"error": str(e)})
            _record_source(
                source="products_cache",
                status="error",
                reason_code=_classify_db_reason_code(e),
                source_started=internal_started,
                error=type(e).__name__,
                query="products_cache_by_alias",
            )

    canonical_product: Optional[Dict[str, Any]] = None
    if canonical_member:
        canonical_product = {
            "merchant_id": str(canonical_member.get("merchant_id") or "").strip() or None,
            "platform": str(canonical_member.get("platform") or "").strip() or None,
            "product_id": str(canonical_member.get("platform_product_id") or "").strip() or None,
            "product_group_id": canonical_group_id,
        }
    elif internal_offers:
        src = (internal_offers[0].get("source") or {}) if isinstance(internal_offers[0], dict) else {}
        if isinstance(src, dict):
            canonical_product = {
                "merchant_id": src.get("merchant_id"),
                "platform": src.get("platform"),
                "product_id": src.get("product_id"),
                "product_group_id": canonical_group_id,
            }

    if not external_offers and canonical_product:
        attached_retry_started = time.perf_counter()
        try:
            retry_variant_aliases = [
                str(alias or "").strip()
                for alias in (
                    [sku_id]
                    + [
                        ((offer.get("source") or {}).get("variant_id"))
                        for offer in internal_offers
                        if isinstance(offer, dict)
                    ]
                )
                if str(alias or "").strip()
            ]
            retry_rows = await _fetch_attached_seed_rows(
                merchant_id=str(canonical_product.get("merchant_id") or "").strip() or None,
                platform=str(canonical_product.get("platform") or "").strip() or None,
                product_aliases=[str(canonical_product.get("product_id") or "").strip() or None] + product_id_aliases,
                variant_aliases=retry_variant_aliases + sku_id_aliases,
            )
            await _append_external_offers_from_seed_rows(retry_rows)
            _record_source(
                source="external_product_seeds_attached_retry",
                status="ok" if retry_rows else "empty",
                reason_code="ok" if retry_rows else "no_candidates",
                source_started=attached_retry_started,
                row_count=len(retry_rows or []),
                query="external_seed_by_canonical_attached_ref",
            )
        except Exception as e:
            logger.info("offers.resolve.external.attached_retry.failed", extra={"error": str(e)})
            _record_source(
                source="external_product_seeds_attached_retry",
                status="error",
                reason_code=_classify_db_reason_code(e),
                source_started=attached_retry_started,
                error=type(e).__name__,
                query="external_seed_by_canonical_attached_ref",
            )

    # Internal offers always come first for checkout.
    offers = internal_offers + external_offers
    offers = offers[:limit]

    canonical_ref: Optional[str] = None
    if canonical_group_id:
        canonical_ref = f"pg:{canonical_group_id}"
    elif internal_offers:
        src = (internal_offers[0].get("source") or {}) if isinstance(internal_offers[0], dict) else {}
        if isinstance(src, dict):
            if src.get("canonical_ref"):
                canonical_ref = str(src.get("canonical_ref"))
            else:
                mid = str(src.get("merchant_id") or "").strip()
                pid = str(src.get("product_id") or "").strip()
                plat = str(src.get("platform") or "").strip() or "unknown"
                if mid and pid:
                    canonical_ref = f"pc:{mid}:{plat}:{pid}"

    if canonical_product is None and canonical_member:
        canonical_product = {
            "merchant_id": str(canonical_member.get("merchant_id") or "").strip() or None,
            "platform": str(canonical_member.get("platform") or "").strip() or None,
            "product_id": str(canonical_member.get("platform_product_id") or "").strip() or None,
            "product_group_id": canonical_group_id,
        }
    elif canonical_product is None and internal_offers:
        src = (internal_offers[0].get("source") or {}) if isinstance(internal_offers[0], dict) else {}
        if isinstance(src, dict):
            canonical_product = {
                "merchant_id": src.get("merchant_id"),
                "platform": src.get("platform"),
                "product_id": src.get("product_id"),
                "product_group_id": canonical_group_id,
            }

    failure_breakdown = {
        str(s.get("source")): str(s.get("reason_code"))
        for s in source_status
        if str(s.get("status")) == "error"
    }
    cache_failed = any(
        str(s.get("status")) == "error" and str(s.get("source")).startswith("products_cache")
        for s in source_status
    )
    if offers:
        reason_code = "OK"
        reason = "resolved"
    elif cache_failed:
        reason_code = "DB_ERROR"
        reason = "products_cache_failed"
    elif any(str(s.get("reason_code")) == "upstream_timeout" for s in source_status):
        reason_code = "UPSTREAM_TIMEOUT"
        reason = "search_timeout"
    elif failure_breakdown:
        first_detail = next(iter(failure_breakdown.values()))
        reason_code = _public_reason_code(first_detail)
        reason = "offers_unavailable"
    else:
        reason_code = "NO_CANDIDATES"
        reason = "no_candidates"
    latency_ms = int((time.perf_counter() - started) * 1000)

    logger.info(
        "offers.resolve.summary",
        extra={
            "event": "offers.resolve.summary",
            "product_id": product_id,
            "sku_id": sku_id,
            "merchant_scope": merchant_scope,
            "offers_count": len(offers),
            "reason_code": reason_code,
            "reason": reason,
            "latency_ms": latency_ms,
            "sources": source_status,
            "canonical_ref": canonical_ref,
        },
    )

    return {
        "status": "success",
        "input": {"product_id": product_id, "sku_id": sku_id},
        "offers": offers,
        "offers_count": len(offers),
        **({"canonical_product_ref": canonical_ref} if canonical_ref else {}),
        "mapping": {
            "canonical_ref": canonical_ref,
            "canonical_product_group_id": canonical_group_id,
            "canonical_product": canonical_product,
            "candidates": mapping_candidates[:50],
        },
        "metadata": {
            "source": "offers.resolve",
            "has_external": bool(external_offers),
            "has_internal": bool(internal_offers),
            "merchant_scope": merchant_scope,
            "reason_code": reason_code,
            "reason": reason,
            "latency_ms": latency_ms,
            "sources": source_status,
            "failure_breakdown": failure_breakdown,
        },
    }

class ProductRef(BaseModel):
    merchant_id: str
    product_id: str


class GetProductDetailPayload(BaseModel):
    product: ProductRef


class GetReviewSummaryPayload(BaseModel):
    sku: ReviewsSkuRef


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
    variant_id: Optional[str] = None
    sku: Optional[str] = None
    selected_options: Optional[Dict[str, Any]] = None
    quantity: int
    unit_price: float
    subtotal: float


class ShippingAddress(BaseModel):
    name: str
    address_line1: str
    address_line2: Optional[str] = ""
    city: str
    state: Optional[str] = None
    country: str
    postal_code: str
    phone: Optional[str] = None


class OrderPayloadBody(BaseModel):
    merchant_id: str
    customer_email: str
    currency: Optional[str] = None
    offer_id: Optional[str] = None
    preferred_psp: Optional[str] = None
    quote_id: Optional[str] = None
    discount_codes: Optional[List[str]] = None
    selected_delivery_option: Optional[Dict[str, Any]] = None
    metadata: Optional[Dict[str, Any]] = None
    idempotency_key: Optional[str] = None
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


def _seed_domain_from_url(url: Optional[str]) -> str:
    if not url:
        return ""
    try:
        parsed = urlparse(url)
    except Exception:
        return ""
    host = (parsed.netloc or "").strip()
    if not host:
        return ""
    if "@" in host:
        host = host.split("@")[-1]
    if ":" in host:
        host = host.split(":")[0]
    return host.lower()


def _format_domain_display_name(domain: str) -> str:
    host = (domain or "").strip().lower()
    if host.startswith("www."):
        host = host[4:]
    for prefix in ("m.", "shop.", "store."):
        if host.startswith(prefix):
            host = host[len(prefix):]
            break
    if not host:
        return "Official Site"
    label = host.split(".")[0]
    label = re.sub(r"[^a-z0-9]+", " ", label).strip()
    if not label:
        return "Official Site"
    name = " ".join(part.capitalize() for part in label.split())
    return f"{name} Official Site"


def _external_seed_display_name(row: Dict[str, Any], seed_data: Dict[str, Any]) -> str:
    for key in ("merchant_name", "brand", "vendor", "store_name"):
        raw = row.get(key) or seed_data.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    domain = (
        row.get("domain")
        or seed_data.get("domain")
        or _seed_domain_from_url(seed_data.get("canonical_url") or seed_data.get("destination_url"))
        or _seed_domain_from_url(row.get("canonical_url") or row.get("destination_url"))
    )
    return _format_domain_display_name(str(domain))


def _strip_accents(text: str) -> str:
    if not text:
        return ""
    return "".join(
        c
        for c in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(c)
    )


def _normalize_offer_title(text: str) -> str:
    if not text:
        return ""
    ascii_text = _strip_accents(text.lower())
    ascii_text = re.sub(r"[^a-z0-9]+", " ", ascii_text).strip()
    return ascii_text


def _offer_price_key(price: Any) -> Optional[int]:
    if price is None:
        return None
    try:
        value = float(price)
    except Exception:
        return None
    if value <= 0:
        return None
    return int(round(value * 100))


def _build_offer_keys(title: str, price: Any, currency: str, vendor: Optional[str]) -> set[str]:
    keys: set[str] = set()
    title_key = _normalize_offer_title(title)
    if not title_key:
        return keys
    currency_key = (currency or "USD").upper()
    price_key = _offer_price_key(price)
    base = f"{title_key}|{currency_key}|{price_key}" if price_key is not None else f"{title_key}|{currency_key}"
    keys.add(base)
    if vendor:
        vendor_key = _normalize_offer_title(vendor)
        if vendor_key:
            keys.add(
                f"{title_key}|{vendor_key}|{currency_key}|{price_key}"
                if price_key is not None
                else f"{title_key}|{vendor_key}|{currency_key}"
            )
    return keys


def _collect_internal_offer_keys(items: List[Any]) -> tuple[set[str], set[str]]:
    offer_keys: set[str] = set()
    ids: set[str] = set()
    for item in items:
        if isinstance(item, StandardProduct):
            title = item.title or ""
            price = item.price
            currency = item.currency or "USD"
            vendor = item.vendor
            if item.product_id:
                ids.add(str(item.product_id))
            if item.id:
                ids.add(str(item.id))
            for v in item.variants or []:
                vid = getattr(v, "variant_id", None) or getattr(v, "id", None)
                if vid:
                    ids.add(str(vid))
            offer_keys.update(_build_offer_keys(title, price, currency, vendor))
        elif isinstance(item, dict):
            title = item.get("title") or item.get("name") or ""
            price = item.get("price")
            if isinstance(price, dict):
                price = price.get("amount") or price.get("value")
            currency = item.get("currency") or item.get("price", {}).get("currency") or "USD"
            vendor = item.get("vendor") or item.get("brand")
            pid = item.get("product_id") or item.get("id")
            if pid:
                ids.add(str(pid))
            for v in item.get("variants") or []:
                if isinstance(v, dict):
                    vid = v.get("variant_id") or v.get("id") or v.get("sku")
                else:
                    vid = getattr(v, "variant_id", None) or getattr(v, "id", None)
                if vid:
                    ids.add(str(vid))
            offer_keys.update(_build_offer_keys(title, price, currency, vendor))
    return offer_keys, ids


def _filter_external_seed_wrappers(
    wrappers: list[dict[str, Any]],
    offer_keys: set[str],
    internal_ids: set[str],
) -> list[dict[str, Any]]:
    if not wrappers:
        return []
    filtered: list[dict[str, Any]] = []
    for wrapper in wrappers:
        product = wrapper.get("product") or {}
        if not isinstance(product, dict):
            filtered.append(wrapper)
            continue
        if product.get("attached_product_key") or product.get("attached_variant_id"):
            continue
        external_id = product.get("product_id") or product.get("external_product_id")
        if external_id and str(external_id) in internal_ids:
            continue
        filtered.append(wrapper)
    return filtered


async def _make_external_redirect_url(
    *,
    market: str,
    tool: str,
    destination_url: str,
    utm_template: Optional[str],
    ctx: Dict[str, Any],
    allowed_domains: Optional[List[str]] = None,
) -> Optional[str]:
    if not destination_url.startswith(("http://", "https://")):
        return None
    dest_with_utm = apply_utm(
        destination_url,
        utm_template or DEFAULT_UTM_TEMPLATE,
        {"market": market, "tool": tool},
    )
    runtime_allowed_domains = allowed_domains
    if runtime_allowed_domains is None:
        runtime_allowed_domains = await get_allowed_domains_for_market(market=market)
    if not is_destination_domain_allowed(
        destination_url=dest_with_utm,
        allowed_domains=runtime_allowed_domains,
    ):
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
    merchant_name = _external_seed_display_name(row, seed_data)
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
        "merchant_name": merchant_name,
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
        "attached_product_key": row.get("attached_product_key") or seed_data.get("attached_product_key"),
        "attached_variant_id": row.get("attached_variant_id") or seed_data.get("attached_variant_id"),
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
    explicit_fields = getattr(product, "model_fields_set", None)
    if explicit_fields is None:
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
    WHERE (CAST(:mid AS TEXT) IS NULL OR merchant_id = CAST(:mid AS TEXT))
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
                return StandardProduct.model_validate(row["product_data"])
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
                sp = StandardProduct.model_validate(row["product_data"])
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
    limit = _clamp_search_limit(filters.limit, fallback=20)

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


async def _invoke_multi_upstream_fallback(
    payload: FindProductsMultiPayload,
    request_metadata: Optional[Dict[str, Any]],
    *,
    timeout_seconds: float,
    hop: int,
) -> Optional[Dict[str, Any]]:
    if not MULTI_SEARCH_UPSTREAM_FALLBACK_BASE_URL:
        return None

    metadata_payload: Dict[str, Any] = dict(request_metadata or {})
    metadata_payload["upstream_fallback_hop"] = hop + 1
    body: Dict[str, Any] = {
        "operation": "find_products_multi",
        "payload": payload.model_dump(by_alias=True, exclude_none=True),
        "metadata": metadata_payload,
    }

    url = f"{MULTI_SEARCH_UPSTREAM_FALLBACK_BASE_URL}/agent/shop/v1/invoke"
    try:
        client = await _get_shared_upstream_http_client()
        # Enforce wall-clock budget, not per-phase socket timeout only.
        resp = await asyncio.wait_for(
            client.post(
                url,
                json=body,
                headers={"Content-Type": "application/json"},
                timeout=_build_request_timeout(timeout_seconds),
            ),
            timeout=timeout_seconds,
        )
        if resp.status_code >= 400:
            _multi_upstream_record_outcome(False)
            record_catalog_upstream_fallback(reason=f"http_{resp.status_code}")
            logger.info(
                "multi.upstream_fallback.http_error",
                extra={"status_code": resp.status_code, "url": url},
            )
            return None

        data = resp.json()
        if not isinstance(data, dict):
            _multi_upstream_record_outcome(False)
            record_catalog_upstream_fallback(reason="invalid_payload")
            return None

        products_raw = data.get("products")
        if not isinstance(products_raw, list):
            _multi_upstream_record_outcome(False)
            record_catalog_upstream_fallback(reason="missing_products")
            return None

        total_val = data.get("total")
        if not isinstance(total_val, int):
            total_val = len(products_raw)

        page_val = data.get("page")
        if not isinstance(page_val, int):
            page_val = int(getattr(payload.search, "page", 1) or 1)

        page_size_val = data.get("page_size")
        if not isinstance(page_size_val, int):
            page_size_val = len(products_raw)

        reply_val = data.get("reply")
        metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}

        merged_metadata = dict(metadata)
        merged_metadata["query_source"] = (
            str(metadata.get("query_source") or "agent_products_resolver_fallback")
        )
        merged_metadata["upstream_fallback"] = {
            "applied": True,
            "base_url": MULTI_SEARCH_UPSTREAM_FALLBACK_BASE_URL,
            "timeout_seconds": timeout_seconds,
        }
        _multi_upstream_record_outcome(True)
        record_catalog_upstream_fallback(reason="delegate_success")

        return {
            "products": products_raw,
            "total": total_val,
            "page": page_val,
            "page_size": page_size_val,
            "reply": reply_val,
            "metadata": merged_metadata,
        }
    except Exception as exc:
        timeout_failure = isinstance(exc, asyncio.TimeoutError)
        if not timeout_failure:
            timeout_failure = isinstance(exc, httpx.TimeoutException)
        if not timeout_failure:
            timeout_failure = "timeout" in str(exc or "").lower()
        logger.info(
            "multi.upstream_fallback.failed",
            extra={"error": str(exc), "url": url},
        )
        _multi_upstream_record_outcome(False, timeout=timeout_failure)
        if timeout_failure:
            record_catalog_upstream_timeout(surface="shopping")
            record_catalog_upstream_fallback(reason="timeout")
        else:
            record_catalog_upstream_fallback(reason="exception")
        return None


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
    source_normalized = ""
    upstream_fallback_hop = 0
    semantic_retry_attempted = False
    if creator_meta:
        creator_id = creator_meta.creator_id
        creator_name = creator_meta.creator_name
        source = creator_meta.source
        source_normalized = _normalize_surface_source(source)
    if isinstance(request_metadata, dict):
        try:
            upstream_fallback_hop = max(
                0,
                int(request_metadata.get("upstream_fallback_hop") or 0),
            )
        except Exception:
            upstream_fallback_hop = 0
        semantic_retry_attempted = bool(request_metadata.get("semantic_retry_attempted") or False)

    # Creator surfaces (creator-agent UI + creator category service) are
    # allowed to use a broader cross-merchant pool and slightly more
    # permissive visibility rules (do not drop products solely because
    # orderable is false).
    is_creator_surface = source_normalized in {"creator-agent-ui", "creator-category-service"}
    is_shopping_surface = _is_shopping_multi_source(source_normalized)
    force_cache_only = _resolve_multi_force_cache_only(source_normalized, is_creator_surface)
    base_merchant_fanout_enabled = _resolve_multi_base_merchant_fanout(
        source_normalized,
        is_creator_surface,
    )
    page = filters.page or 1
    limit = _clamp_search_limit(filters.limit, fallback=20)

    should_try_upstream = (
        is_shopping_surface
        and bool(MULTI_SEARCH_UPSTREAM_FALLBACK_BASE_URL)
        and upstream_fallback_hop < 1
    )
    force_local_fallback_on_delegate_fail = bool(is_shopping_surface)
    upstream_timeout_seconds = _resolve_multi_upstream_timeout_seconds(is_shopping_surface)
    upstream_cache_key: Optional[str] = None
    skip_delegate_due_circuit_local_fallback = False
    if should_try_upstream and MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM:
        upstream_cache_key = _build_multi_upstream_cache_key(
            payload,
            request_metadata,
            source_normalized,
        )
        cached = _multi_upstream_cache_get(upstream_cache_key)
        if isinstance(cached, dict):
            cached_result = cached.get("result")
            if isinstance(cached_result, dict):
                cached_meta = (
                    cached_result.get("metadata")
                    if isinstance(cached_result.get("metadata"), dict)
                    else {}
                )
                cached_meta["upstream_response_cache"] = {
                    "hit": True,
                    "kind": str(cached.get("kind") or "result"),
                    "remaining_ttl_seconds": round(
                        float(cached.get("remaining_ttl_seconds") or 0.0),
                        3,
                    ),
                }
                cached_result["metadata"] = cached_meta
                return cached_result

        upstream_circuit_open = _multi_upstream_circuit_is_open()
        if upstream_circuit_open:
            if _allow_local_fallback_after_delegate_fail(request_metadata) or force_local_fallback_on_delegate_fail:
                skip_delegate_due_circuit_local_fallback = True
                logger.info(
                    "multi.upstream_fallback.local_fallback_on_circuit_open",
                    extra={
                        "event": "multi.upstream_fallback.local_fallback_on_circuit_open",
                        "source": source_normalized,
                    },
                )
            else:
                empty_result = _build_multi_delegate_empty_result(
                    page=page,
                    force_cache_only=force_cache_only,
                    base_merchant_fanout_enabled=base_merchant_fanout_enabled,
                    creator_id=creator_id,
                    creator_name=creator_name,
                    upstream_timeout_seconds=upstream_timeout_seconds,
                    upstream_attempted=False,
                    upstream_circuit_open=True,
                )
                if upstream_cache_key:
                    _multi_upstream_cache_put(upstream_cache_key, empty_result, "error")
                return empty_result

    if (
        should_try_upstream
        and MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM
        and not skip_delegate_due_circuit_local_fallback
    ):
        delegated = await _invoke_multi_upstream_fallback(
            payload,
            request_metadata,
            timeout_seconds=upstream_timeout_seconds,
            hop=upstream_fallback_hop,
        )
        if isinstance(delegated, dict):
            result_kind = "result"
            try:
                total_val = int(delegated.get("total") or 0)
            except Exception:
                total_val = 0
            products_val = delegated.get("products")
            if total_val <= 0 and isinstance(products_val, list) and len(products_val) == 0:
                result_kind = "empty"
            if upstream_cache_key:
                _multi_upstream_cache_put(upstream_cache_key, delegated, result_kind)
            if result_kind == "empty" and force_local_fallback_on_delegate_fail:
                logger.info(
                    "multi.upstream_fallback.empty_result_local_fallback",
                    extra={
                        "event": "multi.upstream_fallback.empty_result_local_fallback",
                        "source": source_normalized,
                    },
                )
                record_catalog_upstream_fallback(reason="delegate_empty_local_fallback")
            else:
                return delegated
        if _allow_local_fallback_after_delegate_fail(request_metadata) or force_local_fallback_on_delegate_fail:
            logger.info(
                "multi.upstream_fallback.local_fallback_enabled",
                extra={
                    "event": "multi.upstream_fallback.local_fallback_enabled",
                    "reason": "delegate_failed",
                    "source": source_normalized,
                },
            )
            record_catalog_upstream_fallback(reason="delegate_failed_local_fallback")
        else:
            # In delegate mode we intentionally avoid running the local
            # multi-merchant path after an upstream timeout/error, because
            # combining both paths can exceed the invoke queue wait budget
            # and surface 504 UPSTREAM_TIMEOUT to the frontend.
            empty_result = _build_multi_delegate_empty_result(
                page=page,
                force_cache_only=force_cache_only,
                base_merchant_fanout_enabled=base_merchant_fanout_enabled,
                creator_id=creator_id,
                creator_name=creator_name,
                upstream_timeout_seconds=upstream_timeout_seconds,
                upstream_attempted=True,
                upstream_circuit_open=False,
            )
            if upstream_cache_key:
                _multi_upstream_cache_put(upstream_cache_key, empty_result, "error")
            return empty_result

    eval_enabled = bool(
        isinstance(request_metadata, dict) and request_metadata.get("eval") is not None
    )
    try:
        _recent_queries = (
            list(user_ctx.recent_queries or []) if user_ctx else []
        )  # type: ignore[union-attr]
    except Exception:
        _recent_queries = []
    used_recent_queries_count = len([q for q in _recent_queries if str(q or "").strip()])

    def _tokenize(text: str) -> List[str]:
        if not text:
            return []
        return [t for t in re.split(r"[^a-z0-9]+", text.lower()) if len(t) > 2]

    query_stopwords = {
        "a",
        "an",
        "and",
        "at",
        "for",
        "from",
        "in",
        "my",
        "of",
        "on",
        "or",
        "the",
        "to",
        "with",
    }

    def _filter_relevance_terms(terms: List[str]) -> List[str]:
        if not terms:
            return []
        return [t for t in terms if t and t not in query_stopwords]

    def _strip_accents(text: str) -> str:
        if not text:
            return ""
        return "".join(
            c
            for c in unicodedata.normalize("NFKD", text)
            if not unicodedata.combining(c)
        )

    def _maybe_attach_eval_debug(
        result: Dict[str, Any],
        *,
        rewritten_query: Optional[str] = None,
        fallback_reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not eval_enabled:
            return result

        if rewritten_query is None:
            raw_q = (filters.query or "").strip()
            rewritten_query = _strip_accents(raw_q.lower())

        if fallback_reason is None:
            meta = result.get("metadata")
            if isinstance(meta, dict):
                query_source = meta.get("query_source")
                if isinstance(query_source, str) and "fallback" in query_source.lower():
                    fallback_reason = query_source

        debug_payload = {
            "fallback_reason": fallback_reason,
            "history_used": used_recent_queries_count > 0,
            "used_recent_queries_count": used_recent_queries_count,
            "rewritten_query": rewritten_query,
        }

        existing_debug = result.get("debug")
        if isinstance(existing_debug, dict):
            merged_debug = dict(existing_debug)
            merged_debug.update(debug_payload)
            result["debug"] = merged_debug
        else:
            result["debug"] = debug_payload

        return result

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

    history_product_ids: set[str] = set()
    history_titles: List[str] = []
    if not (is_shopping_surface and MULTI_SEARCH_SKIP_HISTORY_SHOPPING):
        history_product_ids, history_titles = await _load_user_history_signals()
    history_terms = set()
    if user_ctx and user_ctx.recent_queries:
        for q_term in user_ctx.recent_queries:
            history_terms.update(_filter_relevance_terms(_tokenize(q_term)))
    for title in history_titles:
        history_terms.update(_filter_relevance_terms(_tokenize(title)))

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
            ORDER BY
              CASE WHEN psp_connected = true THEN 0 ELSE 1 END,
              updated_at DESC NULLS LAST,
              created_at DESC NULLS LAST
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
    query_semantic_class = _classify_query_semantic_class(q_ascii or q_lower)
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
            return _maybe_attach_eval_debug(
                {
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
                },
                rewritten_query=q_ascii,
            )

    external_seed_wrappers: list[dict[str, Any]] = []
    try:
        external_seed_stopwords = {
            "a",
            "an",
            "and",
            "buy",
            "cart",
            "checkout",
            "find",
            "for",
            "item",
            "items",
            "me",
            "of",
            "or",
            "please",
            "product",
            "products",
            "recommend",
            "recommendation",
            "show",
            "the",
            "to",
        }

        def _external_seed_query_terms(raw_query: str) -> List[str]:
            q_norm = str(raw_query or "").strip().lower()
            if not q_norm:
                return []
            terms = re.findall(r"[a-z0-9]+", q_norm)
            if not terms:
                terms = [t for t in q_norm.split() if t]

            filtered_terms: List[str] = []
            for term in terms:
                if term in external_seed_stopwords:
                    continue
                if len(term) <= 1:
                    continue
                if term not in filtered_terms:
                    filtered_terms.append(term)
                if len(filtered_terms) >= 8:
                    break
            return filtered_terms or terms[:4]

        seed_query_terms = _external_seed_query_terms(q_ascii or q_lower)
        seed_query_compacts = [
            re.sub(r"[^a-z0-9]+", "", t.lower()) for t in seed_query_terms if t
        ]
        seed_query_compacts = [t for t in seed_query_compacts if t]

        seed_limit = min(max(limit * max(page, 1) * 2, 30), 200)
        if is_shopping_surface:
            shopping_seed_cap = max(200, int(MULTI_SEARCH_SEED_QUERY_LIMIT_SHOPPING or 0))
            seed_limit = min(seed_limit, shopping_seed_cap)
        seed_params: Dict[str, Any] = {"limit": seed_limit}
        seed_where = "status = 'active'"
        if q_lower:
            terms = seed_query_terms or [q_lower]
            where_clauses: List[str] = []
            for idx, term in enumerate(terms[:8]):
                key = f"like_{idx}"
                seed_params[key] = f"%{term.lower()}%"
                clause = (
                    "("
                    "LOWER(COALESCE(title,'')) LIKE :" + key
                    + " OR LOWER(COALESCE(domain,'')) LIKE :" + key
                    + " OR LOWER(COALESCE(canonical_url,'')) LIKE :" + key
                    + " OR LOWER(COALESCE(destination_url,'')) LIKE :" + key
                )
                seed_text_scan_enabled = True
                if seed_text_scan_enabled:
                    clause += " OR LOWER(CAST(seed_data AS TEXT)) LIKE :" + key
                clause += ")"
                where_clauses.append(clause)
            if where_clauses:
                seed_where += " AND (" + " OR ".join(where_clauses) + ")"

        seed_rows: List[Any] = []
        if seed_limit > 0:
            seed_rows = await asyncio.wait_for(
                database.fetch_all(
                    f"""
                    SELECT *
                    FROM external_product_seeds
                    WHERE {seed_where}
                    ORDER BY updated_at DESC, created_at DESC
                    LIMIT :limit
                    """,
                    seed_params,
                ),
                timeout=MULTI_SEARCH_SEED_QUERY_TIMEOUT_SECONDS,
            )

        seen_external_ids: set[str] = set()
        external_redirect_cache: Dict[str, Optional[str]] = {}
        seed_budget_ms = int(FIND_PRODUCTS_MULTI_SEED_BUDGET_MS or 0)
        seed_build_deadline = (
            time.perf_counter() + (seed_budget_ms / 1000.0)
            if seed_budget_ms > 0
            else None
        )
        shopping_seed_target = max(1, limit * max(page, 1))
        for row in seed_rows:
            if seed_build_deadline is not None and time.perf_counter() >= seed_build_deadline:
                break
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
            brand = seed_data.get("brand") or seed_data.get("merchant_display_name") or ""
            blob = " ".join([title, str(brand or ""), domain, canonical_url or "", dest]).lower().strip()
            blob_ascii = _strip_accents(blob)
            blob_compact = re.sub(r"[^a-z0-9]+", "", blob_ascii)

            if q_lower:
                if q_lower in blob:
                    score = 0.85
                elif seed_query_terms and any(t in blob for t in seed_query_terms):
                    score = 0.75
                elif q_compact and q_compact in blob_compact:
                    score = 0.7
                elif seed_query_compacts and any(t in blob_compact for t in seed_query_compacts):
                    score = 0.65
                elif _fuzzy_token_match(seed_query_terms or q_tokens, _tokenize(blob_ascii), max_dist=1):
                    score = 0.6
                else:
                    # Recall-first: keep low-confidence external seeds for downstream rerank.
                    score = 0.12
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

            market = str(row_dict.get("market") or "US")
            tool = str(row_dict.get("tool") or "*")
            utm_template = row_dict.get("utm_template") or seed_data.get("utm_template")
            redirect_cache_key = "||".join(
                [
                    market,
                    tool,
                    str(dest),
                    str(utm_template or ""),
                ]
            )
            if redirect_cache_key in external_redirect_cache:
                redirect_url = external_redirect_cache[redirect_cache_key]
            else:
                redirect_url = await _make_external_redirect_url(
                    market=market,
                    tool=tool,
                    destination_url=dest,
                    utm_template=utm_template,
                    ctx={"seedId": row_dict.get("id")},
                    allowed_domains=None,
                )
                external_redirect_cache[redirect_cache_key] = redirect_url
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
                    "merchant_name": product.get("merchant_name"),
                    "relevance_score": score,
                }
            )
            seen_external_ids.add(external_id)
            if is_shopping_surface and len(external_seed_wrappers) >= shopping_seed_target:
                break
    except Exception as e:
        logger.info("multi.external_seeds.failed", extra={"error": str(e)})

    if not has_merchants and not external_seed_wrappers:
        return _maybe_attach_eval_debug(
            {
                "products": [],
                "total": 0,
                "page": page,
                "page_size": 0,
                "metadata": {
                    "query_source": "cache_multi",
                    "fetched_at": datetime.utcnow().isoformat(),
                    "merchants_searched": 0,
                },
            },
            rewritten_query=q_ascii,
            fallback_reason="no_eligible_merchants",
        )

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
            offer_keys, internal_ids = _collect_internal_offer_keys(mapped)
            external_seed_wrappers = _filter_external_seed_wrappers(
                external_seed_wrappers,
                offer_keys,
                internal_ids,
            )
            if external_seed_wrappers:
                external_products = [w["product"] for w in external_seed_wrappers]
                mapped = external_products + mapped

        start_idx = (page - 1) * limit
        page_items = mapped[start_idx : start_idx + limit]
        return _maybe_attach_eval_debug(
            {
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
            },
            rewritten_query=q_ascii,
        )

    # How many products to fetch per merchant (before global filtering/pagination)
    # Keep per-merchant payload small to bound latency; the cross-merchant cache
    # recall query below provides additional coverage for query-specific matches.
    per_merchant_limit = min(max(limit + 2, 12), 60)
    target_candidate_count = min(max(limit * max(page, 1) * 2, 80), 500)

    # Merchant fan-out guardrail:
    # - `find_products_multi` is primarily cache-driven, so scanning every merchant
    #   on each request creates long-tail latency spikes with little recall benefit.
    # - Keep a bounded merchant slice and fetch in parallel.
    merchant_items = list(merchant_map.items()) if base_merchant_fanout_enabled else []
    max_merchants_to_scan = (
        MULTI_SEARCH_MERCHANT_SCAN_LIMIT_CREATOR
        if is_creator_surface
        else MULTI_SEARCH_MERCHANT_SCAN_LIMIT
    )
    if max_merchants_to_scan > 0 and len(merchant_items) > max_merchants_to_scan:
        merchant_items = merchant_items[:max_merchants_to_scan]
    merchants_scanned = len(merchant_items)

    # Collect products as (StandardProduct, merchant_name) tuples.
    merchant_products: list[tuple[StandardProduct, str]] = []
    seen_merchant_product_keys: set[tuple[str, str]] = set()

    def _append_merchant_candidate(
        product: StandardProduct,
        merchant_name: str,
        merchant_id_hint: Optional[str] = None,
    ) -> None:
        pid = str(product.product_id or product.id or "").strip()
        mid = str(product.merchant_id or merchant_id_hint or "").strip()
        if pid and mid:
            key = (mid, pid)
            if key in seen_merchant_product_keys:
                return
            seen_merchant_product_keys.add(key)
        merchant_products.append((product, merchant_name))

    merchant_ids_for_search = [mid for mid, _ in merchant_items]
    if (
        not merchant_ids_for_search
        and is_shopping_surface
        and q
        and merchant_map
    ):
        merchant_ids_for_search = list(merchant_map.keys())[
            : max(1, int(MULTI_SEARCH_SHOPPING_FAST_MERCHANT_SEED_LIMIT))
        ]

    # Fast path: perform one cache-wide SQL search across eligible merchants first.
    # This avoids N-per-merchant round trips in the common non-empty query path.
    if merchant_ids_for_search and q:
        try:
            cache_terms: List[str] = []
            if q_tokens:
                cache_terms.extend(q_tokens[:8])
            if not cache_terms and q_ascii and len(q_ascii) >= 2:
                cache_terms.append(q_ascii)
            cache_terms = [t for t in cache_terms if t]

            if cache_terms:
                cache_limit = min(max(target_candidate_count * 3, 120), 1200)
                where_clauses: List[str] = []
                params: Dict[str, Any] = {
                    "merchant_ids": merchant_ids_for_search,
                    "cache_limit": cache_limit,
                }
                for idx, term in enumerate(cache_terms):
                    key = f"like_{idx}"
                    params[key] = f"%{term.lower()}%"
                    where_clauses.append(
                        "("
                        "LOWER(COALESCE(product_data->>'title','')) LIKE :" + key
                        + " OR LOWER(COALESCE(product_data->>'description','')) LIKE :" + key
                        + " OR LOWER(COALESCE(product_data->>'product_type','')) LIKE :" + key
                        + " OR LOWER(COALESCE(product_data->>'vendor','')) LIKE :" + key
                        + " OR LOWER(COALESCE(product_data->>'sku','')) LIKE :" + key
                        + ")"
                    )
                    if sku_like_query and (
                        (not is_shopping_surface) or MULTI_SEARCH_SHOPPING_ENABLE_SKU_JSON_SCAN
                    ):
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
                    mid = str(row.get("merchant_id") or "").strip()
                    if not mid:
                        continue
                    product_data = row.get("product_data")
                    if isinstance(product_data, str):
                        try:
                            product_data = json.loads(product_data)
                        except Exception:
                            continue
                    if not isinstance(product_data, dict):
                        continue
                    try:
                        prod = StandardProduct(**product_data)
                        prod.merchant_id = prod.merchant_id or mid
                        _append_merchant_candidate(prod, merchant_map.get(mid, ""), mid)
                    except Exception:
                        continue
        except Exception as e:
            logger.info(
                "multi.cross_merchant_cache_prefetch.failed",
                extra={"query": q, "error": str(e)},
            )

    # Slow path fallback: if cache prefetch is not enough, pull per-merchant pools.
    if merchant_items and len(merchant_products) < target_candidate_count:
        semaphore = asyncio.Semaphore(max(1, MULTI_SEARCH_MERCHANT_CONCURRENCY))

        async def _fetch_multi_candidates_for_merchant(
            mid: str,
            name: str,
        ) -> list[tuple[StandardProduct, str, str]]:
            async with semaphore:
                try:
                    products, _source, _error = await asyncio.wait_for(
                        get_products_hybrid(
                            merchant_id=mid,
                            limit=per_merchant_limit,
                            agent_id="shopping_ai_multi",
                            background_tasks=background_tasks,
                            force_cache_only=force_cache_only,
                        ),
                        timeout=MULTI_SEARCH_MERCHANT_FETCH_TIMEOUT_SECONDS,
                    )
                except Exception:
                    return []

                shop_currency = _get_cached_merchant_shopify_currency(mid)
                out: list[tuple[StandardProduct, str, str]] = []
                for p in products:
                    if shop_currency and (p.platform or "").lower() == "shopify":
                        p.currency = shop_currency
                    out.append((p, name, mid))
                return out

        gathered = await asyncio.gather(
            *[_fetch_multi_candidates_for_merchant(mid, name) for mid, name in merchant_items],
            return_exceptions=True,
        )
        for chunk in gathered:
            if not isinstance(chunk, list):
                continue
            for prod, name, mid in chunk:
                _append_merchant_candidate(prod, name, mid)

    # Recall boost: when the user asks a specific query (e.g. a character name),
    # searching only a small "top-N" slice per merchant can miss relevant items.
    # Pull additional candidates directly from products_cache using cheap text matching.
    if merchant_map and (not is_shopping_surface or MULTI_SEARCH_SHOPPING_ENABLE_RECALL_BOOST):
        try:
            anchor_terms: List[str] = []
            if "labubu" in q_ascii:
                anchor_terms = ["labubu"]
            elif q_tokens:
                informative_tokens = [
                    t
                    for t in q_tokens
                    if len(t) >= 3 and t not in query_stopwords
                ]
                anchor_terms = informative_tokens[:3] if informative_tokens else q_tokens[:1]
            elif len(q_compact) >= 4:
                anchor_terms = [q_compact]

            if anchor_terms:
                # Pull a bounded recent slice using indexed columns and do
                # token matching in-memory. This avoids expensive JSON LIKE
                # scans that can trigger long-tail DB latency.
                if is_shopping_surface:
                    cache_scan_limit = min(max(limit * max(page, 1) * 30, 240), 1500)
                    recall_timeout_seconds = float(MULTI_SEARCH_SHOPPING_RECALL_QUERY_TIMEOUT_SECONDS)
                else:
                    cache_scan_limit = min(max(limit * max(page, 1) * 80, 600), 5000)
                    recall_timeout_seconds = float(MULTI_SEARCH_RECALL_QUERY_TIMEOUT_SECONDS)
                rows = await asyncio.wait_for(
                    database.fetch_all(
                        """
                        SELECT merchant_id, product_data
                        FROM products_cache
                        WHERE (expires_at IS NULL OR expires_at > NOW())
                          AND merchant_id = ANY(:merchant_ids)
                        ORDER BY cached_at DESC
                        LIMIT :cache_limit
                        """,
                        {
                            "merchant_ids": list(merchant_map.keys()),
                            "cache_limit": cache_scan_limit,
                        },
                    ),
                    timeout=recall_timeout_seconds,
                )

                anchor_compacts = [
                    re.sub(r"[^a-z0-9]+", "", str(t or "").lower())
                    for t in anchor_terms
                    if str(t or "").strip()
                ]
                anchor_compacts = [t for t in anchor_compacts if t]

                def _recall_row_matches(data: Dict[str, Any]) -> bool:
                    title = str(data.get("title") or "").lower()
                    desc = str(data.get("description") or "").lower()
                    ptype = str(data.get("product_type") or "").lower()
                    sku = str(data.get("sku") or "").lower()
                    blob = " ".join([title, desc, ptype, sku])
                    blob_compact = re.sub(r"[^a-z0-9]+", "", blob)

                    for term in anchor_terms:
                        tt = str(term or "").lower().strip()
                        if tt and tt in blob:
                            return True
                    for compact in anchor_compacts:
                        if compact and compact in blob_compact:
                            return True

                    if sku_like_query:
                        json_blob = str(data).lower()
                        for term in anchor_terms:
                            tt = str(term or "").lower().strip()
                            if tt and tt in json_blob:
                                return True

                    return False

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
                    if not _recall_row_matches(product_data):
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
        if beauty_exclude_tags and query_semantic_class != "fragrance":
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
                query_terms = _filter_relevance_terms(_tokenize(q_ascii))

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
        internal_products = []
        for wrapper in filtered_products:
            product_item = wrapper.get("product") if isinstance(wrapper, dict) else None
            if isinstance(product_item, (StandardProduct, dict)):
                internal_products.append(product_item)
        offer_keys, internal_ids = _collect_internal_offer_keys(internal_products)
        external_seed_wrappers = _filter_external_seed_wrappers(
            external_seed_wrappers,
            offer_keys,
            internal_ids,
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

    semantic_retry_applied = False
    semantic_retry_query: Optional[str] = None
    semantic_retry_hits = 0

    # Fallback: if primary query returned nothing, surface top-sellers instead
    # - For general queries: only when creator_id is present (as before)
    # - For tee intent queries: also allow a global tee-only fallback so we don't
    #   respond with an empty list for strong tee intent (e.g. Spanish camisetas).
    if not out_products:
        if should_try_upstream:
            upstream_result = await _invoke_multi_upstream_fallback(
                payload,
                request_metadata,
                timeout_seconds=upstream_timeout_seconds,
                hop=upstream_fallback_hop,
            )
            if isinstance(upstream_result, dict):
                return _maybe_attach_eval_debug(
                    upstream_result,
                    rewritten_query=q_ascii,
                    fallback_reason="upstream_resolver_fallback",
                )

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
                return _maybe_attach_eval_debug(
                    {
                        "products": fallback_items,
                        "total": len(mapped),
                        "page": page,
                        "page_size": len(fallback_items),
                        "reply": reply_text,
                        "metadata": {
                            "query_source": source,
                            "fetched_at": datetime.utcnow().isoformat(),
                            "merchants_searched": len(merchant_map),
                            "merchants_scanned": merchants_scanned,
                            "merchant_scan_limited": merchants_scanned < len(merchant_map),
                            "base_merchant_fanout_enabled": base_merchant_fanout_enabled,
                            "creator_id": creator_id,
                            "creator_name": creator_name,
                        },
                    },
                    rewritten_query=q_ascii,
                )

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
                return _maybe_attach_eval_debug(
                    {
                        "products": fallback_items,
                        "total": len(mapped),
                        "page": page,
                        "page_size": len(fallback_items),
                        "reply": reply_text,
                        "metadata": {
                            "query_source": source,
                            "fetched_at": datetime.utcnow().isoformat(),
                            "merchants_searched": len(merchant_map),
                            "merchants_scanned": merchants_scanned,
                            "merchant_scan_limited": merchants_scanned < len(merchant_map),
                            "base_merchant_fanout_enabled": base_merchant_fanout_enabled,
                            "creator_id": creator_id,
                            "creator_name": creator_name,
                        },
                    },
                    rewritten_query=q_ascii,
                )

    if (
        not out_products
        and SEARCH_FRAGRANCE_SEMANTIC_RETRY
        and query_semantic_class == "fragrance"
        and bool(q)
        and not semantic_retry_attempted
    ):
        retry_query = _build_fragrance_semantic_retry_query(q_ascii or q_lower)
        if retry_query:
            retry_payload = FindProductsMultiPayload(
                search=MultiSearchFilters(
                    query=retry_query,
                    category=filters.category,
                    price_min=filters.price_min,
                    price_max=filters.price_max,
                    page=filters.page,
                    limit=limit,
                    in_stock_only=filters.in_stock_only,
                ),
                user=payload.user,
                metadata=payload.metadata,
                creator_id=payload.creator_id,
                intent_safety=payload.intent_safety,
            )
            retry_metadata = dict(request_metadata or {})
            retry_metadata["semantic_retry_attempted"] = True
            retry_result = await _handle_find_products_multi(
                retry_payload,
                retry_metadata,
                background_tasks,
            )
            if isinstance(retry_result, dict):
                retry_products = retry_result.get("products")
                retry_count = len(retry_products) if isinstance(retry_products, list) else 0
                semantic_retry_applied = True
                semantic_retry_query = retry_query
                semantic_retry_hits = retry_count
                retry_meta = retry_result.get("metadata")
                if not isinstance(retry_meta, dict):
                    retry_meta = {}
                retry_meta["query_semantic_class"] = query_semantic_class
                retry_meta["semantic_retry_applied"] = True
                retry_meta["semantic_retry_query"] = retry_query
                retry_meta["semantic_retry_hits"] = retry_count
                retry_meta.setdefault("external_fill_gate_reason", "semantic_retry")
                retry_result["metadata"] = retry_meta
                if retry_count > 0:
                    return _maybe_attach_eval_debug(
                        retry_result,
                        rewritten_query=_strip_accents(retry_query.lower()),
                    )
        semantic_retry_applied = True
        semantic_retry_query = semantic_retry_query or retry_query
        semantic_retry_hits = 0
        reply_text = reply_text or (
            "I couldn’t find a strong fragrance match yet. "
            "Try adding a brand, scent note, or budget."
        )

    if not out_products and toys_intent_query:
        reply_text = reply_text or (
            "I couldn’t find toy items in the current shop catalog for that query. "
            "If you share a brand or character name (for example: Labubu), I can narrow it down."
        )

    history_used = bool(history_product_ids or history_terms)

    return _maybe_attach_eval_debug(
        {
            "products": out_products,
            "total": total,
            "page": page,
            "page_size": len(out_products),
            "reply": reply_text,
            "metadata": {
                "query_source": "cache_multi_intent",
                "query_semantic_class": query_semantic_class,
                "semantic_retry_applied": semantic_retry_applied,
                "semantic_retry_query": semantic_retry_query,
                "semantic_retry_hits": semantic_retry_hits,
                "fetched_at": datetime.utcnow().isoformat(),
                "merchants_searched": len(merchant_map),
                "merchants_scanned": merchants_scanned,
                "merchant_scan_limited": merchants_scanned < len(merchant_map),
                "force_cache_only": force_cache_only,
                "base_merchant_fanout_enabled": base_merchant_fanout_enabled,
                "creator_id": creator_id,
                "creator_name": creator_name,
                "history_boost_applied": history_used,
                "upstream_fallback_configured": bool(MULTI_SEARCH_UPSTREAM_FALLBACK_BASE_URL),
                "upstream_fallback_attempted": bool(should_try_upstream),
                "shopping_fast_prefetch_used": bool(is_shopping_surface and q and merchant_ids_for_search),
                "shopping_recall_boost_enabled": bool(
                    is_shopping_surface and MULTI_SEARCH_SHOPPING_ENABLE_RECALL_BOOST
                ),
                "shopping_sku_json_scan_enabled": bool(
                    is_shopping_surface and MULTI_SEARCH_SHOPPING_ENABLE_SKU_JSON_SCAN
                ),
            },
        },
        rewritten_query=q_ascii,
    )


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
    if payload.merchant_id:
        base_product = await _load_product_by_id(payload.product_id, merchant_id=payload.merchant_id)
    else:
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
    payload_metadata = request.payload.get("metadata") if isinstance(request.payload, dict) else None
    if not isinstance(payload_metadata, dict):
        payload_metadata = {}
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
    if not normalized_metadata.get("source"):
        for k in ("source",):
            if payload_metadata.get(k):
                normalized_metadata["source"] = payload_metadata.get(k)
                break
    if not normalized_metadata.get("trace_id") and not normalized_metadata.get("traceId"):
        for k in ("trace_id", "traceId"):
            if payload_metadata.get(k):
                normalized_metadata[k] = payload_metadata.get(k)
                break
    if not normalized_metadata.get("page_request_id") and not normalized_metadata.get("pageRequestId"):
        for k in ("page_request_id", "pageRequestId"):
            if payload_metadata.get(k):
                normalized_metadata[k] = payload_metadata.get(k)
                break
    if not normalized_metadata.get("page_request_id") and not normalized_metadata.get("pageRequestId"):
        for k in ("page_request_id", "pageRequestId"):
            if k in request.payload and request.payload.get(k):
                normalized_metadata[k] = request.payload.get(k)
                break

    # For now we support async tasks for the heavy operations only.
    if operation == "find_products_multi":
        payload = FindProductsMultiPayload(
            **_normalize_find_products_multi_payload(request.payload)
        )
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
                            fetched.model_dump(),
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


async def _proxy_agent_api(
    method: str,
    path: str,
    json_body: Dict[str, Any],
    *,
    checkout_token: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Forward a request to the Agent API.

    Auth precedence:
    - If `checkout_token` is present (Checkout UI flow), forward it so agent_id is derived from the token.
    - Otherwise fall back to a server-side API key (legacy / demo mode).
    """
    url = f"{AGENT_API_BASE}{path}"
    headers: Dict[str, str] = {"Content-Type": "application/json"}

    token = (checkout_token or "").strip()
    if token:
        headers["X-Checkout-Token"] = token
    else:
        if not AGENT_API_KEY:
            raise HTTPException(
                status_code=500,
                detail="SHOP_GATEWAY_AGENT_API_KEY / PIVOTA_API_KEY is not configured for agent payments",
            )
        headers["X-API-Key"] = AGENT_API_KEY

    try:
        client = await _get_shared_upstream_http_client()
        resp = await client.request(
            method,
            url,
            json=json_body,
            headers=headers,
            timeout=_build_request_timeout(15.0),
        )
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


async def _handle_create_order(order: OrderPayloadBody, *, checkout_token: Optional[str]) -> Dict[str, Any]:
    """Proxy create_order to Agent API (/agent/v1/orders/create)."""
    body = {
        "merchant_id": order.merchant_id,
        "customer_email": order.customer_email,
        **({"currency": order.currency} if order.currency else {}),
        **({"offer_id": order.offer_id} if order.offer_id else {}),
        **({"preferred_psp": order.preferred_psp} if order.preferred_psp else {}),
        **({"quote_id": order.quote_id} if order.quote_id else {}),
        **({"discount_codes": order.discount_codes} if isinstance(order.discount_codes, list) else {}),
        **({"selected_delivery_option": order.selected_delivery_option} if isinstance(order.selected_delivery_option, dict) else {}),
        **({"metadata": order.metadata} if isinstance(order.metadata, dict) else {}),
        **({"idempotency_key": order.idempotency_key} if order.idempotency_key else {}),
        "items": [
            {
                "merchant_id": item.merchant_id,
                "product_id": item.product_id,
                "product_title": item.product_title,
                **({"variant_id": item.variant_id} if item.variant_id else {}),
                **({"sku": item.sku} if item.sku else {}),
                **({"selected_options": item.selected_options} if isinstance(item.selected_options, dict) else {}),
                "quantity": item.quantity,
                "unit_price": item.unit_price,
                "subtotal": item.subtotal,
            }
            for item in order.items
        ],
        "shipping_address": {
            "name": order.shipping_address.name,
            "address_line1": order.shipping_address.address_line1,
            "address_line2": order.shipping_address.address_line2 or "",
            "city": order.shipping_address.city,
            **({"state": order.shipping_address.state} if order.shipping_address.state else {}),
            "country": order.shipping_address.country,
            "postal_code": order.shipping_address.postal_code,
            "phone": order.shipping_address.phone or "",
        },
        "customer_notes": order.customer_notes or "",
    }

    return await _proxy_agent_api("POST", "/agent/v1/orders/create", body, checkout_token=checkout_token)


async def _handle_submit_payment(payment: PaymentPayloadBody, *, checkout_token: Optional[str]) -> Dict[str, Any]:
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

    return await _proxy_agent_api("POST", "/agent/v1/payments", body, checkout_token=checkout_token)


INVOKE_SHORT_WAIT_SECONDS_RAW = os.getenv("AGENT_SHOP_INVOKE_MAX_WAIT_SECONDS")
try:
    INVOKE_SHORT_WAIT_SECONDS = float(INVOKE_SHORT_WAIT_SECONDS_RAW) if INVOKE_SHORT_WAIT_SECONDS_RAW else 0.0
    if INVOKE_SHORT_WAIT_SECONDS < 0:
        INVOKE_SHORT_WAIT_SECONDS = 0.0
except ValueError:
    INVOKE_SHORT_WAIT_SECONDS = 0.0

INVOKE_MULTI_BYPASS_QUEUE_SHOPPING = _env_bool(
    "AGENT_SHOP_MULTI_BYPASS_QUEUE_SHOPPING",
    True,
)


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
    - get_review_summary
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
    checkout_token = (http_request.headers.get("x-checkout-token") or "").strip() or None

    # Normalize metadata: allow creatorId/creatorName to be passed at payload top-level
    normalized_metadata: Dict[str, Any] = dict(request.metadata or {})
    payload_metadata = request.payload.get("metadata") if isinstance(request.payload, dict) else None
    if not isinstance(payload_metadata, dict):
        payload_metadata = {}
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
    if not normalized_metadata.get("source"):
        if payload_metadata.get("source"):
            normalized_metadata["source"] = payload_metadata.get("source")
    if not normalized_metadata.get("trace_id") and not normalized_metadata.get("traceId"):
        for k in ("trace_id", "traceId"):
            if payload_metadata.get(k):
                normalized_metadata[k] = payload_metadata.get(k)
                break
    if not normalized_metadata.get("page_request_id") and not normalized_metadata.get("pageRequestId"):
        for k in ("page_request_id", "pageRequestId"):
            if payload_metadata.get(k):
                normalized_metadata[k] = payload_metadata.get(k)
                break
    if not normalized_metadata.get("page_request_id") and not normalized_metadata.get("pageRequestId"):
        for k in ("page_request_id", "pageRequestId"):
            if k in request.payload and request.payload.get(k):
                normalized_metadata[k] = request.payload.get(k)
                break

    if operation == "find_products":
        normalized_find_products = _normalize_find_products_payload(request.payload)
        search_payload = (
            normalized_find_products.get("search")
            if isinstance(normalized_find_products, dict)
            else {}
        )
        if not isinstance(search_payload, dict):
            search_payload = {}

        merchant_id = str(
            search_payload.get("merchant_id") or search_payload.get("merchantId") or ""
        ).strip()
        if merchant_id:
            search_payload["merchant_id"] = merchant_id
            payload = FindProductsPayload(search=SearchFilters(**search_payload))
            return await _handle_find_products(payload.search, background_tasks)

        # Backward-compatible fallback: treat non-merchant-scoped find_products as multi-search.
        multi_payload = FindProductsMultiPayload(
            **_normalize_find_products_multi_payload(request.payload)
        )
        return await _handle_find_products_multi(
            multi_payload,
            normalized_metadata,
            background_tasks,
        )

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

    if operation == "get_review_summary":
        if not _reviews_enabled():
            raise HTTPException(status_code=404, detail="REVIEWS_DISABLED")
        payload = GetReviewSummaryPayload(**request.payload)
        http_request.state.operation = operation
        http_request.state.merchant_id = payload.sku.merchant_id
        started = time.time()
        status_code = 200
        error_detail = None
        try:
            from services.reviews_service import get_review_summary_for_sku

            variant_id = payload.sku.variant_id
            if variant_id is not None and str(variant_id).strip() == "":
                variant_id = None

            review_summary = await get_review_summary_for_sku(
                merchant_id=payload.sku.merchant_id,
                platform=payload.sku.platform,
                platform_product_id=payload.sku.platform_product_id,
                variant_id=variant_id,
            )

            dv = _reviews_default_view_override()
            if dv and review_summary:
                review_summary["default_view"] = dv
            try:
                from observability.reviews_metrics import record_pdp_default_view

                if review_summary and isinstance(review_summary, dict):
                    record_pdp_default_view(str(review_summary.get("default_view") or "merchant"))
            except Exception:
                pass

            return {"review_summary": review_summary}
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
        return await _handle_create_order(payload.order, checkout_token=checkout_token)

    if operation == "find_products_multi":
        started = time.time()
        status_code = 200
        error_detail = None
        request_id = getattr(http_request.state, "request_id", None)
        payload = FindProductsMultiPayload(
            **_normalize_find_products_multi_payload(request.payload)
        )
        payload.search.limit = _clamp_search_limit(payload.search.limit, fallback=20)
        query_semantic_class = _classify_query_semantic_class(payload.search.query)
        source_normalized = _normalize_surface_source(normalized_metadata.get("source"))
        is_shopping_surface = _is_shopping_multi_source(source_normalized)
        page_request_id = (
            normalized_metadata.get("page_request_id")
            or normalized_metadata.get("pageRequestId")
        )
        dedup_cache_hit = False
        dedup_inflight_joined = False
        dedup_key: Optional[str] = None
        try:
            if INVOKE_MULTI_BYPASS_QUEUE_SHOPPING and is_shopping_surface:
                if MULTI_SEARCH_PAGE_REQUEST_DEDUP_ENABLED:
                    dedup_key = _build_multi_page_request_dedup_key(
                        payload=payload,
                        source_normalized=source_normalized,
                        page_request_id=page_request_id,
                    )
                if dedup_key:
                    cached_result = _multi_page_request_cache_get(dedup_key)
                    if isinstance(cached_result, dict):
                        dedup_cache_hit = True
                        result = cached_result
                    else:
                        inflight = _MULTI_SEARCH_PAGE_REQUEST_INFLIGHT.get(dedup_key)
                        if inflight is not None:
                            dedup_inflight_joined = True
                            result = copy.deepcopy(await inflight)
                        else:
                            task = asyncio.create_task(
                                _handle_find_products_multi(
                                    payload,
                                    normalized_metadata,
                                    background_tasks,
                                )
                            )
                            _MULTI_SEARCH_PAGE_REQUEST_INFLIGHT[dedup_key] = task
                            try:
                                result = await task
                                if isinstance(result, dict):
                                    _multi_page_request_cache_put(dedup_key, result)
                            finally:
                                _MULTI_SEARCH_PAGE_REQUEST_INFLIGHT.pop(dedup_key, None)
                else:
                    result = await _handle_find_products_multi(
                        payload,
                        normalized_metadata,
                        background_tasks,
                    )
            else:
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
                if INVOKE_SHORT_WAIT_SECONDS > 0:
                    try:
                        result = await asyncio.wait_for(
                            future, timeout=INVOKE_SHORT_WAIT_SECONDS
                        )
                    except asyncio.TimeoutError:
                        # Short-wait budget exceeded; keep task running in the background.
                        result = {
                            "status": "pending",
                            "task_id": task_id,
                        }
                else:
                    result = await future

            elapsed_ms = round(max(0.0, (time.time() - started) * 1000.0), 1)
            if isinstance(result, dict):
                response_metadata = result.get("metadata")
                if not isinstance(response_metadata, dict):
                    response_metadata = {}
                response_metadata["gateway_latency_ms"] = elapsed_ms
                response_metadata["source_normalized"] = source_normalized
                response_metadata["shopping_surface_detected"] = is_shopping_surface
                if request_id:
                    response_metadata["request_id"] = request_id
                if page_request_id:
                    response_metadata["page_request_id"] = page_request_id
                if dedup_key:
                    response_metadata["page_request_dedup_enabled"] = True
                    response_metadata["page_request_dedup_cache_hit"] = dedup_cache_hit
                    response_metadata["page_request_dedup_inflight_joined"] = dedup_inflight_joined
                response_metadata.setdefault("query_semantic_class", query_semantic_class)
                response_metadata = _normalize_gateway_route_health(
                    response_metadata,
                    default_decision_node=str(response_metadata.get("query_source") or "cache_multi_intent"),
                )
                route_health = response_metadata.get("route_health")
                if isinstance(route_health, dict):
                    fallback_reason = route_health.get("fallback_reason")
                    route_health["fallback_reason"] = fallback_reason
                    response_metadata["fallback_reason"] = fallback_reason
                    response_metadata["route_health"] = route_health
                result["metadata"] = response_metadata
                try:
                    products = result.get("products")
                    has_products = isinstance(products, list) and len(products) > 0
                    query_source = str(response_metadata.get("query_source") or "unknown")
                    mode = "shopping_surface" if is_shopping_surface else "multi_search"
                    record_catalog_search(
                        mode=mode,
                        path=query_source,
                        result="ok" if has_products else "no_candidates",
                        duration_seconds=max(0.0, time.time() - started),
                    )
                except Exception:
                    pass
            return result
        except asyncio.CancelledError:
            # Client disconnected; best-effort cancellation.
            status_code = 499
            error_detail = "client_disconnect"
            try:
                if "task_id" in locals():
                    await agent_task_manager.cancel(task_id, reason="client_disconnect")
            except Exception:
                pass
            raise
        except HTTPException as e:
            status_code = int(e.status_code)
            error_detail = str(e.detail)
            raise
        except Exception as e:
            status_code = 500
            error_detail = type(e).__name__
            raise
        finally:
            duration_seconds = max(0.0, time.time() - started)
            if duration_seconds >= 2.0:
                logger.info(
                    "multi.invoke.slow",
                    extra={
                        "source": source_normalized,
                        "is_shopping_surface": is_shopping_surface,
                        "query": payload.search.query,
                        "page_request_id": (
                            normalized_metadata.get("page_request_id")
                            or normalized_metadata.get("pageRequestId")
                        ),
                        "duration_ms": round(duration_seconds * 1000.0, 1),
                    },
                )
            try:
                from observability.reviews_metrics import record_invoke

                record_invoke(
                    operation=operation,
                    status_code=status_code,
                    duration_seconds=duration_seconds,
                    error_detail=error_detail,
                )
            except Exception:
                pass

    if operation in ("offers.resolve", "offers_resolve", "offersResolve"):
        payload = OffersResolvePayload(
            **_normalize_offers_resolve_payload(request.payload)
        )
        return await _handle_offers_resolve(payload, normalized_metadata, background_tasks)

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
        return await _handle_submit_payment(payload.payment, checkout_token=checkout_token)

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

        from services.reviews_service import (
            verify_review_media_signature_with_reason,
            _allow_legacy_review_media_id,
            _reviews_media_s3_client,
        )

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

            client = _reviews_media_s3_client()
            if client is None:
                status_code = 500
                raise HTTPException(status_code=500, detail="MEDIA_STORAGE_UNAVAILABLE")

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
