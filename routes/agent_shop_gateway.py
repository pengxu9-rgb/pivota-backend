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
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional, Tuple, get_args
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field, ConfigDict

from config.settings import resolve_public_api_base_url, settings
from services.outbound_warm_handoff import could_upgrade_at_click_time
from db.database import database
from models.catalog import PivotPaymentContext, PivotQueryRequest, PivotResultItem
from models.reviews_refs import SkuRef as ReviewsSkuRef
from services.beauty_external_ranking import (
    BEAUTY_EXTERNAL_RANKING_AUDIT_VERSION,
    build_external_seed_filter_product as _shared_build_external_seed_filter_product,
    normalize_external_seed_product_type as _shared_normalize_external_seed_product_type,
    normalize_external_seed_structured_ingredient_ids as _shared_normalize_external_seed_structured_ingredient_ids,
    rank_external_seed_rows,
)
from services.product_query_service import get_products_hybrid
from services.test_merchant_policy import (
    get_excluded_merchant_ids,
    filter_out_test_merchants,
)
from services.external_seed_search import (
    build_seed_quarantine_anti_join as _seed_quarantine_clause,
    fetch_external_seed_rows,
)
from services.pivot_query_service import search_pivot_catalog
from services.query_semantic_class import classify_query_semantic_class
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
    append_referral_click_param,
    append_shopify_cart_click_attribute,
    extract_shopify_numeric_variant_id,
    get_allowed_domains_for_market,
    is_destination_domain_allowed,
    REFERRAL_CLICK_PARAM,
    SHOPIFY_CART_CLICK_ATTRIBUTE,
    make_redirect_token,
    normalize_shop_host,
    parse_redirect_token_verified,
    shopify_cart_base_url,
)
from services import live_offer_verification
from services.shopify_variant_identity import (
    sole_stamped_variant_id,
    storefront_is_shopify,
)
from services.commerce_attribution_service import (
    PVT_CLICK_ID,
    PVT_PRODUCT_ID,
    PVT_SURFACE,
    PVT_VARIANT_ID,
    new_click_id,
    normalize_surface,
)
from models.standard_product import StandardProduct, StandardProductVariant, ProductStatus
from services.agent_task_manager import AgentTaskManager
from services.commerce_surface_service import (
    COMMERCE_SURFACE_AGENT_API,
    normalize_commerce_surface,
    payment_capabilities_support_surface,
)
from services.product_exposure_service import (
    build_agent_push_projection_from_standard_variant,
    pick_first_eligible_variant_from_standard_product,
)
from observability.reliability_metrics import (
    record_catalog_pivot_shadow_compare,
    record_catalog_search,
    record_catalog_upstream_fallback,
    record_traffic_taxonomy,
    record_catalog_upstream_timeout,
    set_catalog_upstream_circuit,
)
from services.traffic_taxonomy_service import attach_traffic_taxonomy, build_traffic_taxonomy
from services.payment_offer_evidence_service import (
    enrich_product_cards_with_payment_offers,
    enrich_product_detail_with_payment_offers,
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

try:
    from services.external_referral_readiness import should_block_external_referral_runtime
except ModuleNotFoundError:
    class _FallbackExternalReferralStatus:
        def __init__(self, *, seed_id: Optional[str] = None, matched_via: str = "runtime") -> None:
            self.seed_id = seed_id
            self.status = "healthy"
            self.gating_policy_version = "external_referral_fallback"
            self.matched_via = matched_via
            self.blocker_anomaly_types: List[str] = []
            self.review_anomaly_types: List[str] = []

    async def should_block_external_referral_runtime(
        row: Dict[str, Any],
        *,
        matched_via: str = "runtime",
        allowed_domains: Optional[List[str]] = None,
    ) -> tuple[bool, _FallbackExternalReferralStatus]:
        return False, _FallbackExternalReferralStatus(
            seed_id=str((row or {}).get("id") or "").strip() or None,
            matched_via=matched_via,
        )


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


def _bootstrap_normalize_surface_source(source: Optional[str]) -> str:
    return str(source or "").strip().lower().replace("_", "-")


def _bootstrap_env_csv_set(name: str, default: set[str]) -> set[str]:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return {item for item in default if str(item or "").strip()}
    values: set[str] = set()
    for part in raw.split(","):
        token = _bootstrap_normalize_surface_source(part)
        if token:
            values.add(token)
    return values


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
PIVOT_MULTI_SHADOW_ENABLED = _bootstrap_env_bool(
    "AGENT_SHOP_PIVOT_MULTI_SHADOW_ENABLED",
    False,
)
PIVOT_MULTI_SERVE_ENABLED = _bootstrap_env_bool(
    "AGENT_SHOP_PIVOT_MULTI_SERVE_ENABLED",
    False,
)
PIVOT_MULTI_LIMIT_MULTIPLIER = _bootstrap_env_int(
    "AGENT_SHOP_PIVOT_MULTI_LIMIT_MULTIPLIER",
    4,
    min_value=1,
    max_value=8,
)
PIVOT_MULTI_SERVE_MAX_PAGE = _bootstrap_env_int(
    "AGENT_SHOP_PIVOT_MULTI_SERVE_MAX_PAGE",
    1,
    min_value=1,
    max_value=10,
)
PIVOT_MULTI_SHADOW_SOURCE_ALLOWLIST = _bootstrap_env_csv_set(
    "AGENT_SHOP_PIVOT_MULTI_SHADOW_SOURCE_ALLOWLIST",
    {"shopping_agent", "shopping-agent-ui", "shopping-agent-web", "aurora", "aurora-chatbox"},
)
PIVOT_MULTI_SERVE_SOURCE_ALLOWLIST = _bootstrap_env_csv_set(
    "AGENT_SHOP_PIVOT_MULTI_SERVE_SOURCE_ALLOWLIST",
    {"shopping_agent"},
)
PIVOT_MULTI_SERVE_INCLUDE_EXTERNAL = _bootstrap_env_bool(
    "AGENT_SHOP_PIVOT_MULTI_SERVE_INCLUDE_EXTERNAL",
    True,
)
PIVOT_MULTI_SERVE_INCLUDE_INCENTIVES = _bootstrap_env_bool(
    "AGENT_SHOP_PIVOT_MULTI_SERVE_INCLUDE_INCENTIVES",
    True,
)
PIVOT_MULTI_BAD_PRICE_DELTA_RATIO_THRESHOLD = 0.20

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


# ── Anonymous /invoke throttle ────────────────────────────────────────────────
# POST /agent/shop/v1/invoke is deliberately public (it is the first-party BFF
# and the ai-plugin manifest declares auth:none), but it was also UNMETERED for
# anonymous callers: RateLimitMiddleware only counts requests that carry an API
# key, and the agent-ui proxy authenticates with a trusted key — so the one
# caller class with no identity had no throttle at all (2026-08-08 audit).
#
# UPDATE 2026-08-11: the second sentence is no longer true. RateLimitMiddleware
# now applies an identity-independent ceiling to ALL non-exempt /agent/*
# requests, keyless included, so this route has two independent counters. This
# per-IP one is the finer-grained of the two and stays authoritative for
# /invoke; the middleware's per-IP layer ships DISABLED partly because it would
# duplicate this one at the same default (60/min). If you consolidate them,
# consolidate onto ONE 429 shape — this raises {"detail": ...} while the
# middleware returns {"error": ..., "message": ..., "retry_after": ...}.
#
# NOTE also that _review_media_client_ip above falls back to
# `request.client.host`, which behind Railway is the platform's CGNAT proxy pool
# (measured: every peer in 100.64.0.0/10). For callers that send no
# X-Forwarded-For that collapses distinct clients into one bucket, so one abuser
# can throttle others. Pre-existing, deliberately not changed here to keep this
# a rate-limit-middleware change; it wants its own PR.
# Scope precisely: requests carrying ANY credential (X-API-Key / Bearer /
# X-Checkout-Token) are untouched — the first-party proxy and keyed agents keep
# today's behavior — and only credential-less direct hits are limited per IP.
# SHOP_INVOKE_ANON_RPM tunes it; 0 disables. In-memory per-instance store,
# same precedent as the review-media limiter above.
_INVOKE_ANON_IP_LIMIT_STORE: Dict[str, Tuple[int, int]] = {}
_INVOKE_ANON_IP_LIMIT_MAX_KEYS = 50_000


def _invoke_anon_rpm() -> int:
    try:
        return max(0, int(os.getenv("SHOP_INVOKE_ANON_RPM") or "60"))
    except ValueError:
        return 60


def _request_carries_credential(request: Request) -> bool:
    return bool(
        (request.headers.get("x-api-key") or "").strip()
        or (request.headers.get("authorization") or "").strip()
        or (request.headers.get("x-checkout-token") or "").strip()
    )


def _check_invoke_anon_rate_limit(ip: str) -> bool:
    rpm = _invoke_anon_rpm()
    if rpm == 0:
        return True
    window = int(time.time() // 60)
    if len(_INVOKE_ANON_IP_LIMIT_STORE) > _INVOKE_ANON_IP_LIMIT_MAX_KEYS:
        # Bound memory against IP-cycling: drop entries from past windows.
        stale = [k for k, v in _INVOKE_ANON_IP_LIMIT_STORE.items() if v[0] != window]
        for k in stale:
            _INVOKE_ANON_IP_LIMIT_STORE.pop(k, None)
    prev = _INVOKE_ANON_IP_LIMIT_STORE.get(ip)
    if prev and prev[0] == window:
        if prev[1] >= rpm:
            return False
        _INVOKE_ANON_IP_LIMIT_STORE[ip] = (window, prev[1] + 1)
        return True
    _INVOKE_ANON_IP_LIMIT_STORE[ip] = (window, 1)
    return True


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


@asynccontextmanager
async def _shopping_gateway_router_lifespan(_: Any):
    if _UPSTREAM_HTTP_WARMUP_ENABLED:
        # Fire-and-forget warmup so deploy healthchecks are not blocked.
        asyncio.create_task(_warm_shared_upstream_http_client())
    try:
        yield
    finally:
        await _close_shared_upstream_http_client()


router = APIRouter(
    prefix="/agent/shop/v1",
    tags=["Shopping Gateway"],
    lifespan=_shopping_gateway_router_lifespan,
)
DEV_MODE = os.getenv("APP_ENV", "dev") != "production"

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


async def _enrich_product_cards_with_savings_evidence(
    product_payloads: List[Dict[str, Any]],
    *,
    merchant_id: str,
    payment_context: Optional[PivotPaymentContext] = None,
    market: Optional[str] = None,
) -> List[Dict[str, Any]]:
    if not product_payloads:
        return product_payloads
    try:
        product_payloads = await enrich_product_cards_with_payment_offers(
            product_payloads,
            merchant_id=merchant_id,
            payment_context=payment_context,
            market=market,
        )
        return product_payloads
    except Exception as exc:
        logger.warning(
            "savings.card_enrichment.batch_failed",
            extra={
                "merchant_id": merchant_id,
                "product_count": len(product_payloads),
                "error": type(exc).__name__,
            },
        )

    isolated: List[Dict[str, Any]] = []
    for product in product_payloads:
        if not isinstance(product, dict):
            continue
        try:
            single = await enrich_product_cards_with_payment_offers(
                [product],
                merchant_id=merchant_id,
                payment_context=payment_context,
                market=market,
            )
            isolated.append(single[0] if single else product)
        except Exception as exc:
            product["savings_evidence_status"] = "unavailable"
            logger.warning(
                "savings.card_enrichment.item_failed",
                extra={
                    "merchant_id": merchant_id,
                    "product_id": product.get("product_id") or product.get("id"),
                    "error": type(exc).__name__,
                },
            )
            isolated.append(product)
    return isolated


def _classify_query_semantic_class(query: Optional[str]) -> str:
    return classify_query_semantic_class(query)


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


def _normalize_budget_currency(raw: Optional[str]) -> Optional[str]:
    token = str(raw or "").strip().lower()
    if not token:
        return None
    if token in {"€", "eur", "euro", "euros"}:
        return "EUR"
    if token in {"$", "usd", "dollar", "dollars"}:
        return "USD"
    if token in {"£", "gbp", "pound", "pounds"}:
        return "GBP"
    return None


_BUDGET_FX_CACHE_TTL_SECONDS = 900.0
_BUDGET_FX_RATE_CACHE: Dict[Tuple[str, str], Tuple[Optional[float], Optional[str], float]] = {}
_DEFAULT_BUDGET_FX_USD_RATES: Dict[str, float] = {
    "USD": 1.0,
    "EUR": 1.09,
    "GBP": 1.27,
    "CNY": 0.14,
    "JPY": 0.0067,
}
_BUDGET_FX_LATEST_FALLBACK_ENABLED = _env_bool(
    "AGENT_SHOP_BUDGET_FX_LATEST_FALLBACK_ENABLED",
    True,
)
_BUDGET_FX_STATIC_FALLBACK_ENABLED = _env_bool(
    "AGENT_SHOP_BUDGET_FX_STATIC_FALLBACK_ENABLED",
    True,
)
_BUDGET_FX_LATEST_BASE_URL = str(
    os.getenv("AGENT_SHOP_BUDGET_FX_LATEST_BASE_URL", "https://api.exchangerate.host") or ""
).strip().rstrip("/")
_BUDGET_FX_LATEST_TIMEOUT_SECONDS = _env_float(
    "AGENT_SHOP_BUDGET_FX_LATEST_TIMEOUT_SECONDS",
    1.5,
    min_value=0.2,
    max_value=10.0,
)


def _parse_budget_fx_rates_payload(raw_rates: Any) -> Dict[str, Any]:
    if isinstance(raw_rates, dict):
        return raw_rates
    if isinstance(raw_rates, str):
        try:
            parsed = json.loads(raw_rates)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _load_budget_fx_usd_rates() -> Dict[str, float]:
    raw = str(os.getenv("AGENT_SHOP_BUDGET_FX_USD_RATES", "") or "").strip()
    if not raw:
        return dict(_DEFAULT_BUDGET_FX_USD_RATES)
    try:
        parsed = json.loads(raw)
    except Exception:
        return dict(_DEFAULT_BUDGET_FX_USD_RATES)
    if not isinstance(parsed, dict):
        return dict(_DEFAULT_BUDGET_FX_USD_RATES)
    normalized = dict(_DEFAULT_BUDGET_FX_USD_RATES)
    for key, value in parsed.items():
        currency = str(key or "").strip().upper()
        try:
            rate = float(value)
        except Exception:
            continue
        if currency and rate > 0:
            normalized[currency] = rate
    return normalized


_BUDGET_FX_USD_RATES = _load_budget_fx_usd_rates()
_BUDGET_FX_STATIC_SOURCE = (
    "env_usd_base_rates"
    if str(os.getenv("AGENT_SHOP_BUDGET_FX_USD_RATES", "") or "").strip()
    else "static_default"
)


def _coerce_budget_fx_snapshot(snapshot: Any) -> Dict[str, Any]:
    if isinstance(snapshot, dict):
        return snapshot
    try:
        return dict(snapshot or {})
    except Exception:
        return {}


async def _lookup_budget_fx_latest_rate(
    from_currency: Optional[str],
    to_currency: Optional[str],
) -> Tuple[Optional[float], Optional[str]]:
    if not _BUDGET_FX_LATEST_FALLBACK_ENABLED:
        return None, None

    source_currency = str(from_currency or "").strip().upper()
    target_currency = str(to_currency or "").strip().upper()
    if not source_currency or not target_currency:
        return None, None

    base_url = _BUDGET_FX_LATEST_BASE_URL
    if not base_url:
        return None, None

    try:
        async with httpx.AsyncClient(
            timeout=_build_request_timeout(_BUDGET_FX_LATEST_TIMEOUT_SECONDS)
        ) as client:
            response = await client.get(
                f"{base_url}/latest",
                params={
                    "base": source_currency,
                    "symbols": target_currency,
                },
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:
        logger.info(
            "multi.budget_fx.latest_lookup.failed",
            extra={
                "event": "multi.budget_fx.latest_lookup.failed",
                "from_currency": source_currency,
                "to_currency": target_currency,
                "error": str(exc),
            },
        )
        return None, None

    rates = payload.get("rates") if isinstance(payload, dict) else None
    if not isinstance(rates, dict):
        return None, None

    raw_rate = rates.get(target_currency)
    try:
        if raw_rate is not None and float(raw_rate) > 0:
            return float(raw_rate), "latest_rate_api"
    except Exception:
        return None, None
    return None, None


def _lookup_budget_fx_static_rate(
    from_currency: Optional[str],
    to_currency: Optional[str],
) -> Tuple[Optional[float], Optional[str]]:
    if not _BUDGET_FX_STATIC_FALLBACK_ENABLED:
        return None, None
    source_currency = str(from_currency or "").strip().upper()
    target_currency = str(to_currency or "").strip().upper()
    if not source_currency or not target_currency:
        return None, None
    if source_currency == target_currency:
        return 1.0, "same_currency"
    source_rate = _BUDGET_FX_USD_RATES.get(source_currency)
    target_rate = _BUDGET_FX_USD_RATES.get(target_currency)
    try:
        if source_rate is not None and target_rate is not None:
            source_rate = float(source_rate)
            target_rate = float(target_rate)
            if source_rate > 0 and target_rate > 0:
                return source_rate / target_rate, _BUDGET_FX_STATIC_SOURCE
    except Exception:
        return None, None
    return None, None


async def _lookup_budget_fx_rate(
    from_currency: Optional[str],
    to_currency: Optional[str],
) -> Tuple[Optional[float], Optional[str]]:
    source_currency = str(from_currency or "").strip().upper()
    target_currency = str(to_currency or "").strip().upper()
    if not source_currency or not target_currency:
        return None, None
    if source_currency == target_currency:
        return 1.0, "same_currency"

    cache_key = (source_currency, target_currency)
    cached = _BUDGET_FX_RATE_CACHE.get(cache_key)
    now = time.time()
    if cached and (now - cached[2]) < _BUDGET_FX_CACHE_TTL_SECONDS:
        return cached[0], cached[1]

    async def _fetch_snapshot(base_currency: str) -> Optional[Dict[str, Any]]:
        return await database.fetch_one(
            """
            SELECT rates, base_currency
            FROM x402_exchange_rates
            WHERE base_currency = :base_currency
              AND (expires_at IS NULL OR expires_at > NOW())
            ORDER BY created_at DESC
            LIMIT 1
            """,
            {"base_currency": base_currency},
        )

    direct_snapshot = _coerce_budget_fx_snapshot(await _fetch_snapshot(source_currency))
    direct_rates = _parse_budget_fx_rates_payload(direct_snapshot.get("rates"))
    direct_value = direct_rates.get(target_currency)
    try:
        if direct_value is not None and float(direct_value) > 0:
            rate = float(direct_value)
            _BUDGET_FX_RATE_CACHE[cache_key] = (rate, "x402_snapshot_direct", now)
            return rate, "x402_snapshot_direct"
    except Exception:
        pass

    reverse_snapshot = _coerce_budget_fx_snapshot(await _fetch_snapshot(target_currency))
    reverse_rates = _parse_budget_fx_rates_payload(reverse_snapshot.get("rates"))
    reverse_value = reverse_rates.get(source_currency)
    try:
        if reverse_value is not None and float(reverse_value) > 0:
            rate = 1.0 / float(reverse_value)
            _BUDGET_FX_RATE_CACHE[cache_key] = (rate, "x402_snapshot_reverse", now)
            return rate, "x402_snapshot_reverse"
    except Exception:
        pass

    latest_rate, latest_source = await _lookup_budget_fx_latest_rate(
        source_currency,
        target_currency,
    )
    try:
        if latest_rate is not None and float(latest_rate) > 0:
            rate = float(latest_rate)
            _BUDGET_FX_RATE_CACHE[cache_key] = (rate, latest_source, now)
            return rate, latest_source
    except Exception:
        pass

    static_rate, static_source = _lookup_budget_fx_static_rate(
        source_currency,
        target_currency,
    )
    try:
        if static_rate is not None and float(static_rate) > 0:
            rate = float(static_rate)
            _BUDGET_FX_RATE_CACHE[cache_key] = (rate, static_source, now)
            return rate, static_source
    except Exception:
        pass

    _BUDGET_FX_RATE_CACHE[cache_key] = (None, None, now)
    return None, None


_ISO_CURRENCY_SHAPE = re.compile(r"[A-Za-z]{3}")


def _observed_currency(*candidates: Any) -> Optional[str]:
    """The first currency any source actually asserted, or None.

    NO `or "USD"` tail. A chain ending in a constant emits a value no source
    asserted AND destroys the question — with the field always populated,
    nothing downstream can ask "what currency is this actually?" (#1634).

    Whitespace-only is not an assertion either, so it collapses to None rather
    than surviving as a truthy string that a `typeof x === 'string'` consumer
    would accept.

    ONE resolver for every external-seed projection. Two sites open-coding this
    is how the tail came back the first time: the fix landed on the projection
    and missed the ranked-candidate path, and a mutation run showed the second
    site had no test at all.

    Measured on prod 2026-07-30 before the tail was removed: 159 of 11,381
    active seeds carry no currency, and ZERO of those carry a price — so this is
    latent, not a live wrong-price bug, and a zero delta after deploy is the
    expected result.
    """
    for candidate in candidates:
        # ⚠️ isinstance BEFORE str(). `seed_data` is untyped JSON, so a currency
        # can arrive as a dict, list or int. `str()` turned those into truthy
        # strings — `{"code": "INR"}` became `"{'CODE': 'INR'}"` — which then
        # PASSED `isQuotableFeedItem`'s `typeof === 'string'` check and got
        # published as the currency code. On the old code they were non-strings
        # and the gate dropped them safely, so stringifying was a REGRESSION
        # that created exactly the case this function's docstring warns about.
        if not isinstance(candidate, str):
            continue
        text = candidate.strip().upper()
        # ISO-4217 shape. Without it a sentinel like "unknown" projects as the
        # currency code "UNKNOWN" and clears the quotable gate — a plausible,
        # publishable, wrong value. Anything that is not three letters is not a
        # currency anyone asserted.
        if _ISO_CURRENCY_SHAPE.fullmatch(text):
            return text
    return None


async def _budget_allows_price(
    *,
    price_amount: Any,
    price_currency: Optional[str],
    budget_currency: Optional[str],
    price_min: Optional[float],
    price_max: Optional[float],
) -> Tuple[bool, Dict[str, Any]]:
    diagnostics: Dict[str, Any] = {}
    amount = _coerce_float(price_amount)
    currency = str(price_currency or "").strip().upper() or None

    comparable_amount = amount
    comparable_currency = currency
    if budget_currency and comparable_currency and comparable_currency != budget_currency:
        fx_rate, fx_source = await _lookup_budget_fx_rate(comparable_currency, budget_currency)
        if fx_rate is None or comparable_amount is None:
            diagnostics["budget_fx_unresolved"] = True
            diagnostics["budget_candidate_currency"] = comparable_currency
            diagnostics["budget_currency"] = budget_currency
            return False, diagnostics
        comparable_amount = comparable_amount * fx_rate
        comparable_currency = budget_currency
        diagnostics["budget_fx_applied"] = True
        diagnostics["budget_fx_rate"] = fx_rate
        diagnostics["budget_fx_source"] = fx_source
        diagnostics["budget_candidate_currency"] = currency
        diagnostics["budget_comparison_currency"] = budget_currency

    # An amount with NO currency cannot be compared to a budget. Falling through
    # would compare the bare number against price_min/price_max as though it were
    # already in the budget's currency — the same fabrication the `or "USD"`
    # removal above exists to end, one layer down. Refuse, and say why.
    #
    # Only when a budget constraint actually exists: with no min, no max and no
    # budget currency the currency is irrelevant and excluding would be a
    # regression.
    _budget_constrained = (
        price_min is not None or price_max is not None or budget_currency is not None
    )
    # `price_currency is None`, NOT `comparable_currency is None`. The two differ
    # and the difference is a whole lane: the internal/connected caller passes
    # `str(product.currency or "").strip().upper()`, which is `""` — not None —
    # for a cached row with a blank currency, and `""` collapses to None inside
    # this function. Keying on the collapsed value silently dropped those rows
    # too, a recall change on a lane this fix never measured and does not claim
    # to touch (the 159/11,381 measurement covers external_product_seeds only;
    # StandardProduct.currency is a required str that pydantic accepts as "").
    #
    # Keying on the ARGUMENT means only a caller that genuinely has no
    # observation — the seed lane, which now passes None from
    # `_observed_currency` — triggers the refusal. A blank string keeps its
    # pre-existing behaviour until someone measures that cohort.
    if _budget_constrained and comparable_amount is not None and price_currency is None:
        diagnostics["budget_currency_unknown"] = True
        return False, diagnostics

    if price_min is not None and comparable_amount is not None and comparable_amount < price_min:
        return False, diagnostics
    if price_max is not None and comparable_amount is not None and comparable_amount > price_max:
        return False, diagnostics
    return True, diagnostics


def _normalized_intent_term_match(text: Optional[str], term: Optional[str]) -> bool:
    source = re.sub(r"[^a-z0-9]+", " ", _strip_accents(str(text or "").lower())).strip()
    target = re.sub(r"[^a-z0-9]+", " ", _strip_accents(str(term or "").lower())).strip()
    if not source or not target:
        return False
    parts = [re.escape(part) for part in re.split(r"\s+", target) if part]
    if not parts:
        return False
    pattern = r"\s+".join(parts)
    return re.search(rf"(?<![a-z0-9]){pattern}(?![a-z0-9])", source) is not None


def _normalized_intent_terms_match(text: Optional[str], terms: list[str]) -> bool:
    return any(_normalized_intent_term_match(text, term) for term in terms if term)


def _normalize_product_visible_attributes(product: StandardProduct) -> Dict[str, List[str]]:
    raw = getattr(product, "visible_attributes", None) or {}
    if not isinstance(raw, dict):
        return {}
    normalized: Dict[str, List[str]] = {}
    for bucket, values in raw.items():
        bucket_name = str(bucket or "").strip()
        if not bucket_name:
            continue
        if isinstance(values, str):
            items = [values]
        elif isinstance(values, list):
            items = values
        else:
            continue
        deduped: List[str] = []
        for value in items:
            label = str(value or "").strip()
            if label and label not in deduped:
                deduped.append(label)
        if deduped:
            normalized[bucket_name] = deduped
    return normalized


def _product_visible_attribute_label_matches(
    product_visible_attributes: Dict[str, List[str]],
    *,
    bucket: Optional[str],
    label: Optional[str],
) -> bool:
    bucket_name = str(bucket or "").strip()
    target_label = str(label or "").strip()
    if not bucket_name or not target_label:
        return False
    return target_label in (product_visible_attributes.get(bucket_name) or [])


def _record_matched_visible_attribute(
    matched_visible_attributes: Dict[str, List[str]],
    *,
    bucket: Optional[str],
    label: Optional[str],
) -> None:
    bucket_name = str(bucket or "").strip()
    target_label = str(label or "").strip()
    if not bucket_name or not target_label:
        return
    bucket_values = matched_visible_attributes.setdefault(bucket_name, [])
    if target_label not in bucket_values:
        bucket_values.append(target_label)


_SKINCARE_INGREDIENT_PROFILES: Dict[str, Dict[str, Any]] = {
    "ascorbic_acid": {
        "display_name": "Vitamin C",
        "aliases": ["ascorbic acid", "vitamin c", "l ascorbic acid"],
        "expected_step_families": ["serum", "treatment"],
    },
    "azelaic_acid": {
        "display_name": "Azelaic Acid",
        "aliases": ["azelaic acid", "azelaic"],
        "expected_step_families": ["serum", "treatment", "cream"],
    },
    "benzoyl_peroxide": {
        "display_name": "Benzoyl Peroxide",
        "aliases": ["benzoyl peroxide", "benzoyl", "bpo"],
        "expected_step_families": ["treatment", "cleanser", "gel"],
    },
    "ceramide_np": {
        "display_name": "Ceramide NP",
        "aliases": ["ceramide", "ceramides", "ceramide np"],
        "expected_step_families": ["serum", "moisturizer"],
    },
    "glycerin": {
        "display_name": "Glycerin",
        "aliases": ["glycerin", "glycerine"],
        "expected_step_families": ["serum", "moisturizer"],
    },
    "hyaluronic_acid": {
        "display_name": "Hyaluronic Acid",
        "aliases": ["hyaluronic acid", "hyaluronic", "hyaluron", "sodium hyaluronate"],
        "expected_step_families": ["serum", "moisturizer"],
    },
    "niacinamide": {
        "display_name": "Niacinamide",
        "aliases": ["niacinamide", "nicotinamide", "vitamin b3"],
        "expected_step_families": ["serum", "treatment"],
    },
    "panthenol": {
        "display_name": "Panthenol",
        "aliases": ["panthenol", "vitamin b5", "provitamin b5", "b5"],
        "expected_step_families": ["serum", "moisturizer"],
    },
    "peptides": {
        "display_name": "Peptides",
        "aliases": [
            "peptide",
            "peptides",
            "multi peptide",
            "multi-peptide",
            "copper peptide",
            "copper peptides",
            "tripeptide",
            "tetrapeptide",
            "hexapeptide",
        ],
        "expected_step_families": ["serum", "treatment"],
    },
    "retinol": {
        "display_name": "Retinol",
        "aliases": ["retinol", "retinoid", "vitamin a"],
        "expected_step_families": ["serum", "treatment", "cream"],
    },
    "salicylic_acid": {
        "display_name": "Salicylic Acid",
        "aliases": ["salicylic acid", "salicylic", "bha"],
        "expected_step_families": ["serum", "treatment", "cleanser"],
    },
    "zinc_pca": {
        "display_name": "Zinc PCA",
        "aliases": ["zinc pca", "zinc"],
        "expected_step_families": ["serum", "treatment"],
    },
}


def _normalize_skincare_ingredient_alias_term(value: Optional[str]) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or "").lower())
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", " ", ascii_text).strip()


def _build_skincare_ingredient_alias_map() -> Dict[str, str]:
    aliases: Dict[str, str] = {}
    for canonical_id, profile in _SKINCARE_INGREDIENT_PROFILES.items():
        terms = [canonical_id.replace("_", " "), *(profile.get("aliases") or [])]
        for term in terms:
            normalized = _normalize_skincare_ingredient_alias_term(term)
            if normalized:
                aliases[normalized] = canonical_id
    return aliases


_SKINCARE_INGREDIENT_CANONICAL_ALIASES: Dict[str, str] = _build_skincare_ingredient_alias_map()
_SKINCARE_INGREDIENT_DISPLAY_NAMES: Dict[str, str] = {
    canonical_id: str(profile.get("display_name") or canonical_id.replace("_", " ").title())
    for canonical_id, profile in _SKINCARE_INGREDIENT_PROFILES.items()
}

_SKINCARE_INGREDIENT_CATEGORY_LABELS = {"serum", "moisturizer", "cleanser", "toner"}
_EXTERNAL_SEED_SKINCARE_CATEGORY_PATTERNS: List[Tuple[str, Any]] = [
    ("serum", re.compile(r"\b(serum|essence|ampoule|concentrate)\b", re.IGNORECASE)),
    (
        "moisturizer",
        re.compile(r"\b(moisturizer|moisturiser|cream|lotion|gel cream|gel-cream|barrier cream)\b", re.IGNORECASE),
    ),
    (
        "cleanser",
        re.compile(r"\b(cleanser|cleansing|face wash|facial wash|cleansing milk|cleansing foam|cleansing gel|wash)\b", re.IGNORECASE),
    ),
    ("toner", re.compile(r"\b(toner|mist|pad)\b", re.IGNORECASE)),
]
_COSMETIC_SHADE_CATEGORY_LABELS = {"foundation", "lipstick", "blush", "gloss"}


def _skincare_ingredient_alias_terms(ingredient_id: Optional[str]) -> List[str]:
    canonical_id = str(ingredient_id or "").strip()
    profile = _SKINCARE_INGREDIENT_PROFILES.get(canonical_id) or {}
    deduped: List[str] = []
    for term in [canonical_id.replace("_", " "), *(profile.get("aliases") or [])]:
        normalized = _normalize_skincare_ingredient_alias_term(term)
        if normalized and normalized not in deduped:
            deduped.append(normalized)
    return deduped


def _build_strict_ingredient_surface_text(product: StandardProduct) -> str:
    platform_metadata = getattr(product, "platform_metadata", None) or {}
    if not isinstance(platform_metadata, dict):
        platform_metadata = {}
    values = [
        getattr(product, "title", None),
        getattr(product, "product_type", None),
        platform_metadata.get("canonical_url"),
        platform_metadata.get("destination_url"),
        platform_metadata.get("source_ref"),
    ]
    return " ".join(str(value or "").strip() for value in values if str(value or "").strip())


def _count_strict_ingredient_term_hits(text: Optional[str], terms: List[str]) -> int:
    return sum(1 for term in terms if _normalized_intent_term_match(text, term))


def _infer_strict_ingredient_step_families(
    product: StandardProduct,
    product_visible_attributes: Dict[str, List[str]],
) -> List[str]:
    deduped: List[str] = []
    for label in product_visible_attributes.get("product_category", []) or []:
        normalized = str(label or "").strip().lower()
        if normalized in _SKINCARE_INGREDIENT_CATEGORY_LABELS and normalized not in deduped:
            deduped.append(normalized)
    fallback_blob = " ".join(
        [
            getattr(product, "product_type", None) or "",
            getattr(product, "title", None) or "",
        ]
    )
    for label in _SKINCARE_INGREDIENT_CATEGORY_LABELS:
        if label in deduped:
            continue
        if _normalized_intent_term_match(fallback_blob, label):
            deduped.append(label)
    return deduped


def _evaluate_strict_ingredient_candidate_precision(
    product: StandardProduct,
    *,
    product_visible_attributes: Dict[str, List[str]],
    active_ingredient_intents: List[Dict[str, Any]],
    candidate_source: str = "internal",
) -> Dict[str, Any]:
    step_families = _infer_strict_ingredient_step_families(product, product_visible_attributes)
    surface_text = _build_strict_ingredient_surface_text(product)
    summary = {
        "target_surface_anchor_hits": 0,
        "competing_surface_anchor_hits": 0,
        "step_family_mismatch": False,
        "rejected_reason": None,
        "resolved_step_families": list(step_families),
    }
    if not active_ingredient_intents:
        return {"passed": True, "summary": summary}

    for group in active_ingredient_intents:
        ingredient_id = str(group.get("ingredient_id") or "").strip()
        if not ingredient_id:
            continue
        profile = _SKINCARE_INGREDIENT_PROFILES.get(ingredient_id) or {}
        target_hits = _count_strict_ingredient_term_hits(
            surface_text,
            _skincare_ingredient_alias_terms(ingredient_id),
        )
        competing_hits = 0
        for other_ingredient_id in _SKINCARE_INGREDIENT_PROFILES.keys():
            if other_ingredient_id == ingredient_id:
                continue
            if _count_strict_ingredient_term_hits(
                surface_text,
                _skincare_ingredient_alias_terms(other_ingredient_id),
            ) > 0:
                competing_hits += 1
        expected_step_families = {
            str(label or "").strip().lower()
            for label in (profile.get("expected_step_families") or [])
            if str(label or "").strip()
        }
        step_family_mismatch = bool(expected_step_families) and not bool(
            expected_step_families.intersection(step_families)
        )

        summary["target_surface_anchor_hits"] += target_hits
        summary["competing_surface_anchor_hits"] += competing_hits
        summary["step_family_mismatch"] = summary["step_family_mismatch"] or step_family_mismatch

        if step_family_mismatch:
            summary["rejected_reason"] = "step_family_mismatch"
            return {"passed": False, "summary": summary}
        if target_hits <= 0:
            if candidate_source != "external_seed" and competing_hits <= 0:
                continue
            summary["rejected_reason"] = (
                "competing_surface_anchor"
                if competing_hits > 0
                else "missing_target_surface_anchor"
            )
            return {"passed": False, "summary": summary}

    return {"passed": True, "summary": summary}


def _normalize_serving_token(value: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]+", "_", _strip_accents(str(value or "").lower())).strip("_")


def _normalize_product_ingredient_ids(product: StandardProduct) -> List[str]:
    ingredient_ids = getattr(product, "ingredient_ids", None) or []
    if isinstance(ingredient_ids, str):
        ingredient_ids = [ingredient_ids]
    deduped: List[str] = []
    for value in ingredient_ids if isinstance(ingredient_ids, list) else []:
        normalized = _normalize_serving_token(str(value or "").replace("_", " "))
        if not normalized:
            continue
        canonical = _SKINCARE_INGREDIENT_CANONICAL_ALIASES.get(normalized.replace("_", " "), normalized)
        if canonical not in deduped:
            deduped.append(canonical)
    return deduped


def _collect_product_visible_option_labels(product: StandardProduct) -> List[str]:
    deduped: List[str] = []
    for variant in getattr(product, "variants", None) or []:
        for label in getattr(variant, "visible_option_labels", None) or []:
            normalized = _normalize_serving_token(label)
            if normalized and normalized not in deduped:
                deduped.append(normalized)
    return deduped


def _extract_skin_care_ingredient_intents(
    query: Optional[str],
    *,
    query_semantic_class: str,
) -> List[Dict[str, Any]]:
    q = _strip_accents(str(query or "").lower())
    if not q:
        return []
    if query_semantic_class not in {"beauty", "unknown"}:
        return []

    active: List[Dict[str, Any]] = []
    for term, canonical_id in _SKINCARE_INGREDIENT_CANONICAL_ALIASES.items():
        if not _normalized_intent_term_match(q, term):
            continue
        if any(item.get("ingredient_id") == canonical_id for item in active):
            continue
        active.append(
            {
                "label": canonical_id,
                "ingredient_id": canonical_id,
                "display_name": _SKINCARE_INGREDIENT_DISPLAY_NAMES.get(canonical_id, canonical_id.replace("_", " ").title()),
                "query_terms": [term],
            }
        )
    return active


def _extract_visible_shade_option_intents(
    query: Optional[str],
    *,
    active_category_labels: list[str],
) -> List[Dict[str, Any]]:
    q = _strip_accents(str(query or "").lower())
    if not q:
        return []
    if not any(label in _COSMETIC_SHADE_CATEGORY_LABELS for label in active_category_labels):
        return []

    active: List[Dict[str, Any]] = []
    for match in re.finditer(r"\bshade\s+(?P<value>[a-z0-9][a-z0-9 -]{0,30})", q):
        raw_value = str(match.group("value") or "").strip(" -")
        normalized = _normalize_serving_token(raw_value)
        if not normalized:
            continue
        label = f"shade_{normalized}"
        if any(item.get("label") == label for item in active):
            continue
        active.append(
            {
                "label": label,
                "product_terms": [raw_value],
                "structured_only": True,
            }
        )
    return active


def _extract_visible_size_option_intents(query: Optional[str]) -> List[Dict[str, Any]]:
    q = _strip_accents(str(query or "").lower())
    if not q:
        return []

    groups = [
        {
            "label": "size_xs",
            "patterns": [r"\bsize\s*(?:xs|x-small|extra small)\b"],
            "product_terms": ["xs", "x-small", "extra small"],
        },
        {
            "label": "size_s",
            "patterns": [r"\bsize\s*(?:s|small)\b"],
            "product_terms": ["s", "small"],
        },
        {
            "label": "size_m",
            "patterns": [r"\bsize\s*(?:m|medium)\b"],
            "product_terms": ["m", "medium"],
        },
        {
            "label": "size_l",
            "patterns": [r"\bsize\s*(?:l|large)\b"],
            "product_terms": ["l", "large"],
        },
        {
            "label": "size_xl",
            "patterns": [r"\bsize\s*(?:xl|x-large|extra large)\b"],
            "product_terms": ["xl", "x-large", "extra large"],
        },
        {
            "label": "size_xxl",
            "patterns": [r"\bsize\s*(?:xxl|2xl|xx-large|extra extra large)\b"],
            "product_terms": ["xxl", "2xl", "xx-large", "extra extra large"],
        },
    ]
    active: List[Dict[str, Any]] = []
    for group in groups:
        if any(re.search(pattern, q) for pattern in group["patterns"]):
            active.append(group)
    for match in re.finditer(r"\bsize\s*(?P<num>\d{2,3})\b", q):
        raw_value = str(match.group("num") or "").strip()
        if not raw_value:
            continue
        label = f"size_{raw_value}"
        if any(existing.get("label") == label for existing in active):
            continue
        active.append(
            {
                "label": label,
                "patterns": [],
                "product_terms": [raw_value],
            }
        )
    return active


def _resolve_strict_constraint_reason(
    *,
    strict_serving_mode: bool,
    ingredient_labels: list[str],
    visible_option_labels: list[str],
    visible_attribute_labels: list[str],
    price_min: Optional[float],
    price_max: Optional[float],
) -> Optional[str]:
    if not strict_serving_mode:
        return None
    has_ingredient_constraint = bool(ingredient_labels)
    has_shade_constraint = any(str(label).startswith("shade_") for label in visible_option_labels)
    if not has_ingredient_constraint and not has_shade_constraint:
        return None
    has_additional_constraint = (
        bool(visible_attribute_labels)
        or price_min is not None
        or price_max is not None
        or (has_ingredient_constraint and has_shade_constraint)
    )
    if has_additional_constraint:
        return "multi_constraint"
    if has_ingredient_constraint:
        return "ingredient"
    return "shade"


def _extract_visible_color_option_intents(
    query: Optional[str],
    *,
    active_category_labels: list[str],
) -> List[Dict[str, Any]]:
    q = _strip_accents(str(query or "").lower())
    if not q:
        return []
    apparel_category_labels = {"hoodie", "sweater", "vest", "skirt", "dress"}
    if not any(label in apparel_category_labels for label in active_category_labels):
        return []

    groups = [
        {
            "label": "color_red",
            "query_terms": ["red"],
            "product_terms": ["red"],
        },
        {
            "label": "color_black",
            "query_terms": ["black"],
            "product_terms": ["black"],
        },
        {
            "label": "color_blue",
            "query_terms": ["blue"],
            "product_terms": ["blue"],
        },
        {
            "label": "color_pink",
            "query_terms": ["pink"],
            "product_terms": ["pink"],
        },
        {
            "label": "color_white",
            "query_terms": ["white"],
            "product_terms": ["white"],
        },
        {
            "label": "color_gray",
            "query_terms": ["gray", "grey"],
            "product_terms": ["gray", "grey"],
        },
    ]
    return [
        group
        for group in groups
        if _normalized_intent_terms_match(q, list(group["query_terms"]))
    ]


def _extract_query_budget_constraints(query: Optional[str]) -> Dict[str, Any]:
    text = str(query or "").strip()
    if not text:
        return {
            "clean_query": "",
            "price_min": None,
            "price_max": None,
            "currency": None,
        }

    patterns = [
        (
            "price_max",
            re.compile(
                r"(?P<full>\b(?:under|below|less than|up to|max(?:imum)?)\s*"
                r"(?:(?P<currency1>[$€£]|usd|eur|gbp|dollars?|euros?|pounds?)\s*)?"
                r"(?P<amount>\d+(?:\.\d{1,2})?)"
                r"(?:\s*(?P<currency2>[$€£]|usd|eur|gbp|dollars?|euros?|pounds?))?)",
                re.IGNORECASE,
            ),
        ),
        (
            "price_min",
            re.compile(
                r"(?P<full>\b(?:over|above|more than|at least|min(?:imum)?)\s*"
                r"(?:(?P<currency1>[$€£]|usd|eur|gbp|dollars?|euros?|pounds?)\s*)?"
                r"(?P<amount>\d+(?:\.\d{1,2})?)"
                r"(?:\s*(?P<currency2>[$€£]|usd|eur|gbp|dollars?|euros?|pounds?))?)",
                re.IGNORECASE,
            ),
        ),
    ]

    clean_text = text
    price_min: Optional[float] = None
    price_max: Optional[float] = None
    currency: Optional[str] = None

    for field_name, pattern in patterns:
        match = pattern.search(clean_text)
        if not match:
            continue
        try:
            amount = float(match.group("amount"))
        except Exception:
            continue
        normalized_currency = _normalize_budget_currency(
            match.group("currency1") or match.group("currency2")
        )
        if field_name == "price_max":
            price_max = amount if price_max is None else min(price_max, amount)
        else:
            price_min = amount if price_min is None else max(price_min, amount)
        if normalized_currency and not currency:
            currency = normalized_currency
        full_text = str(match.group("full") or "").strip()
        if full_text:
            clean_text = re.sub(re.escape(full_text), " ", clean_text, count=1, flags=re.IGNORECASE)

    clean_text = re.sub(r"\s+", " ", clean_text).strip()
    return {
        "clean_query": clean_text,
        "price_min": price_min,
        "price_max": price_max,
        "currency": currency,
    }


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
    route_health["pivot_shadow_scheduled"] = bool(
        route_health.get("pivot_shadow_scheduled")
        if route_health.get("pivot_shadow_scheduled") is not None
        else md.get("pivot_shadow_scheduled")
    )
    route_health["pivot_shadow_mode"] = (
        str(route_health.get("pivot_shadow_mode") or md.get("pivot_shadow_mode") or "").strip()
        or None
    )
    derived_rollout_mode = (
        str(route_health.get("pivot_rollout_mode") or md.get("pivot_rollout_mode") or "").strip().lower()
    )
    if not derived_rollout_mode:
        derived_rollout_mode = "shadow" if route_health["pivot_shadow_scheduled"] else "legacy"
    route_health["pivot_rollout_mode"] = derived_rollout_mode
    route_health["pivot_rollout_guard_passed"] = bool(
        route_health.get("pivot_rollout_guard_passed")
        if route_health.get("pivot_rollout_guard_passed") is not None
        else md.get("pivot_rollout_guard_passed")
        if md.get("pivot_rollout_guard_passed") is not None
        else route_health["pivot_rollout_mode"] in {"shadow", "serve"}
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
    md["pivot_shadow_scheduled"] = route_health["pivot_shadow_scheduled"]
    md["pivot_shadow_mode"] = route_health["pivot_shadow_mode"]
    md["pivot_rollout_mode"] = route_health["pivot_rollout_mode"]
    md["pivot_rollout_guard_passed"] = route_health["pivot_rollout_guard_passed"]
    if search_decision is not None:
        search_decision["query_semantic_class"] = route_health["query_semantic_class"]
        search_decision["domain_filter_dropped_external"] = route_health[
            "domain_filter_dropped_external"
        ]
        md["search_decision"] = search_decision
    md["route_health"] = route_health
    return md


def _apply_pivot_rollout_metadata(
    metadata: Optional[Dict[str, Any]],
    *,
    pivot_shadow_scheduled: bool,
) -> Dict[str, Any]:
    md = dict(metadata) if isinstance(metadata, dict) else {}
    query_source = str(md.get("query_source") or "").strip()
    rollout_mode = str(md.get("pivot_rollout_mode") or "").strip().lower()

    if query_source == "pivot_semantic_core_multi" or rollout_mode == "serve":
        md["pivot_shadow_scheduled"] = False
        md.pop("pivot_shadow_mode", None)
        md["pivot_rollout_mode"] = "serve"
        md["pivot_rollout_guard_passed"] = True
        return md

    md["pivot_shadow_scheduled"] = pivot_shadow_scheduled
    if pivot_shadow_scheduled:
        md["pivot_shadow_mode"] = "background_compare"
    md["pivot_rollout_mode"] = "shadow" if pivot_shadow_scheduled else "legacy"
    md["pivot_rollout_guard_passed"] = bool(pivot_shadow_scheduled)
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


def _seed_query_fast_multiterm_enabled() -> bool:
    """Flag for the fast multi-term external-seed query. Default OFF ⇒
    byte-identical SQL. When ON, the seed text query drops the redundant
    whole-phrase/compact OR arms (a strict subset of the per-token OR) and ranks
    on cheap indexed columns instead of detoasting seed_data JSON — flipping a
    broad multi-term query off the parallel seq scan (~4s) onto the trgm indexes
    (~0.4s). Recall is unchanged; ranking is a soft signal the caller re-ranks."""
    return (os.getenv("SEED_QUERY_FAST_MULTITERM") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _seed_query_lean_where_min_tokens() -> int:
    """Token-count threshold at/above which the fast stage-A seed query drops the
    seed_data->recall JSON arms from the WHERE and matches only the cheap inline
    columns (title/url/domain). A many-token OR over the long trgm-indexed
    retrieval_summary detoasts thousands of rows on recheck and blows the stage-A
    timeout → 0 results for long natural-language queries. Restricting to inline
    columns keeps such queries at ~0.06s with higher-precision title matches.
    Only applies when SEED_QUERY_FAST_MULTITERM is on; 0 disables. Default 4 keeps
    short (≤3-token) ingredient queries on the recall-rich full path."""
    return _env_int(
        "SEED_QUERY_LEAN_WHERE_MIN_TOKENS",
        4,
        min_value=0,
        max_value=8,
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
    normalized = _bootstrap_normalize_surface_source(source)
    if normalized in {"creator", "creator-agent-ui", "creator-category-service"}:
        return "creator-agent"
    return normalized


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


def _normalize_gateway_request_metadata(
    *,
    metadata: Optional[Dict[str, Any]],
    payload: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    normalized_metadata: Dict[str, Any] = dict(metadata or {})
    payload_dict = payload if isinstance(payload, dict) else {}
    payload_metadata = payload_dict.get("metadata") if isinstance(payload_dict.get("metadata"), dict) else {}

    if not normalized_metadata.get("creator_id"):
        for key in ("creatorId", "creator_id"):
            if key in payload_dict and payload_dict.get(key):
                normalized_metadata["creator_id"] = payload_dict.get(key)
                break
    if not normalized_metadata.get("creator_name"):
        for key in ("creatorName", "creator_name"):
            if key in payload_dict and payload_dict.get(key):
                normalized_metadata["creator_name"] = payload_dict.get(key)
                break
    if not normalized_metadata.get("source") and payload_metadata.get("source"):
        normalized_metadata["source"] = payload_metadata.get("source")
    if not normalized_metadata.get("trace_id") and not normalized_metadata.get("traceId"):
        for key in ("trace_id", "traceId"):
            if payload_metadata.get(key):
                normalized_metadata[key] = payload_metadata.get(key)
                break
    if not normalized_metadata.get("page_request_id") and not normalized_metadata.get("pageRequestId"):
        for key in ("page_request_id", "pageRequestId"):
            if payload_metadata.get(key):
                normalized_metadata[key] = payload_metadata.get(key)
                break
    if not normalized_metadata.get("page_request_id") and not normalized_metadata.get("pageRequestId"):
        for key in ("page_request_id", "pageRequestId"):
            if payload_dict.get(key):
                normalized_metadata[key] = payload_dict.get(key)
                break

    query_source = str(
        normalized_metadata.get("query_source")
        or payload_metadata.get("query_source")
        or payload_dict.get("query_source")
        or ""
    ).strip() or None
    commerce_surface = str(
        normalized_metadata.get("commerce_surface")
        or payload_metadata.get("commerce_surface")
        or payload_dict.get("commerce_surface")
        or payload_metadata.get("surface")
        or payload_dict.get("surface")
        or ""
    ).strip() or None
    taxonomy = build_traffic_taxonomy(
        normalized_metadata,
        metadata=payload_metadata,
        default_source_channel=str(
            normalized_metadata.get("source")
            or payload_metadata.get("source")
            or ""
        ).strip()
        or None,
        default_query_source=query_source,
        default_protocol_name=str(
            normalized_metadata.get("protocol_name")
            or payload_metadata.get("protocol_name")
            or "rest"
        ).strip()
        or "rest",
        default_commerce_surface=commerce_surface,
    )
    return attach_traffic_taxonomy(normalized_metadata, taxonomy)


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


_GENERIC_DEFAULT_PRECISION_GATE_SOURCES = {
    "shopping-agent-ui",
    "shopping-agent-web",
}
_GENERIC_DEFAULT_PRECISION_IGNORED_QUERY_TOKENS = {
    "black",
    "white",
    "beige",
    "brown",
    "camel",
    "gray",
    "grey",
    "green",
    "navy",
    "neutral",
    "pink",
    "red",
    "silver",
    "tan",
    "vintage",
    "warm",
}
_GENERIC_DEFAULT_PRECISION_MIN_COVERAGE = 0.6
_GENERIC_DEFAULT_PRECISION_MIN_MATCHES = 2


def _normalize_generic_precision_token(value: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]+", "", _strip_accents(str(value or "").lower())).strip()


def _generic_precision_token_variants(token: str) -> set[str]:
    normalized = _normalize_generic_precision_token(token)
    if not normalized:
        return set()
    variants = {normalized}
    if len(normalized) > 4 and normalized.endswith("ies"):
        variants.add(normalized[:-3] + "y")
    if len(normalized) > 4 and normalized.endswith("es"):
        variants.add(normalized[:-2])
    if len(normalized) > 3 and normalized.endswith("s"):
        variants.add(normalized[:-1])
    return {item for item in variants if item}


def _default_query_precision_terms(query: Optional[str]) -> list[str]:
    deduped: list[str] = []
    for raw_token in re.split(r"[^a-z0-9]+", _strip_accents(str(query or "").lower())):
        token = _normalize_generic_precision_token(raw_token)
        if (
            not token
            or len(token) <= 2
            or token.isdigit()
            or token in _GENERIC_DEFAULT_PRECISION_IGNORED_QUERY_TOKENS
        ):
            continue
        if token not in deduped:
            deduped.append(token)
    return deduped


def _candidate_generic_precision_terms(product: StandardProduct) -> set[str]:
    candidate_terms: set[str] = set()
    raw_terms: list[str] = []
    raw_terms.extend(re.split(r"[^a-z0-9]+", _strip_accents(str(product.title or "").lower())))
    raw_terms.extend(re.split(r"[^a-z0-9]+", _strip_accents(str(product.product_type or "").lower())))
    raw_terms.extend(re.split(r"[^a-z0-9]+", _strip_accents(str(getattr(product, "vendor", "") or "").lower())))
    raw_terms.extend(re.split(r"[^a-z0-9]+", _strip_accents(str(getattr(product, "sku", "") or "").lower())))
    for tag in getattr(product, "tags", None) or []:
        raw_terms.extend(re.split(r"[^a-z0-9]+", _strip_accents(str(tag or "").lower())))
    for raw_term in raw_terms:
        for variant in _generic_precision_token_variants(raw_term):
            if len(variant) > 2:
                candidate_terms.add(variant)
    return candidate_terms


def _evaluate_generic_default_precision_gate(
    *,
    query: Optional[str],
    product: StandardProduct,
) -> Dict[str, Any]:
    query_terms = _default_query_precision_terms(query)
    if len(query_terms) < 2:
        return {
            "applied": False,
            "passed": True,
            "matched_terms": [],
            "coverage_ratio": 1.0,
            "required_matches": 0,
        }

    candidate_terms = _candidate_generic_precision_terms(product)
    matched_terms = [
        term
        for term in query_terms
        if _generic_precision_token_variants(term) & candidate_terms
    ]
    coverage_ratio = len(matched_terms) / float(len(query_terms)) if query_terms else 1.0
    query_compact = _normalize_generic_precision_token(query)
    candidate_compact = _normalize_generic_precision_token(
        " ".join(
            [
                str(product.title or ""),
                str(product.product_type or ""),
                " ".join(str(tag or "") for tag in (getattr(product, "tags", None) or [])),
            ]
        )
    )
    exact_phrase_match = bool(query_compact and len(query_compact) >= 8 and query_compact in candidate_compact)
    passed = exact_phrase_match or (
        len(matched_terms) >= min(_GENERIC_DEFAULT_PRECISION_MIN_MATCHES, len(query_terms))
        and coverage_ratio >= _GENERIC_DEFAULT_PRECISION_MIN_COVERAGE
    )
    return {
        "applied": True,
        "passed": passed,
        "matched_terms": matched_terms,
        "coverage_ratio": round(coverage_ratio, 4),
        "required_matches": min(_GENERIC_DEFAULT_PRECISION_MIN_MATCHES, len(query_terms)),
        "exact_phrase_match": exact_phrase_match,
    }


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
    commerce_surface: Optional[str] = Field(
        None,
        alias="commerceSurface",
        description="Serving surface eligibility policy (agent_api | ucp | acp)",
    )

    model_config = ConfigDict(populate_by_name=True)


class UserIntent(BaseModel):
    id: Optional[str] = Field(None, description="Accounts user id or email if available")
    email: Optional[str] = Field(None, description="Optional explicit email")
    recent_queries: List[str] = Field(default_factory=list, description="Recent free-text queries from the user")


class RequestMetadata(BaseModel):
    creator_id: Optional[str] = Field(None, alias="creatorId", description="Creator id for contextual recommendations")
    creator_name: Optional[str] = Field(None, alias="creatorName", description="Human friendly creator name")
    source: Optional[str] = Field(None, description="Calling surface (e.g. creator_agent)")
    trace_id: Optional[str] = Field(None, alias="traceId", description="Optional trace id for observability")
    commerce_surface: Optional[str] = Field(
        None,
        alias="commerceSurface",
        description="Serving surface eligibility policy (agent_api | ucp | acp)",
    )

    model_config = ConfigDict(populate_by_name=True)


class FindProductsMultiPayload(BaseModel):
    search: MultiSearchFilters
    user: Optional[UserIntent] = None
    metadata: Optional[RequestMetadata] = None
    payment_context: Optional[PivotPaymentContext] = None
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
    commerce_surface: Optional[str] = Field(
        None,
        alias="commerceSurface",
        description="Serving surface eligibility policy (agent_api | ucp | acp)",
    )

    model_config = ConfigDict(populate_by_name=True)


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
            "commerce_surface",
            "commerceSurface",
        ):
            if key in payload:
                search[key] = payload.get(key)
    normalized: Dict[str, Any] = {"search": search}
    for key in ("user", "metadata", "payment_context", "paymentContext", "creator_id", "creatorId", "intent_safety"):
        if key in payload:
            normalized["payment_context" if key == "paymentContext" else key] = payload.get(key)
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
    for key in ("limit", "market", "tool", "commerce_surface", "commerceSurface"):
        if key in payload:
            normalized[key] = payload.get(key)
    return normalized


def _safe_lower(s: Any) -> str:
    return str(s or "").strip().lower()


def _escape_like(value: str) -> str:
    """Escape SQL LIKE wildcards so an interpolated literal matches itself.

    Needed for the storage-format attached-seed lookup (see IDENTITY_REFERENCE
    Trap T1): ``merchant_id`` values contain underscores (``merch_...``), and an
    unescaped ``_`` in a LIKE pattern matches any single char, which would
    over-match across merchants. Pair every LIKE using an escaped value with
    ``ESCAPE '\\'``.
    """
    return str(value or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _parse_catalog_product_key(key: Any) -> Optional[Tuple[str, str, str]]:
    """Split the STORAGE-format product key `prod::{merchant}::{platform}::{pid}`.

    Returns (merchant, platform, source_product_id) or None if `key` is not the
    `prod::` storage form (e.g. NULL, or a bare/pipe form — see IDENTITY_REFERENCE
    Trap T1). Source product ids never contain `::`, so the pid is the 4th segment.
    """
    if not isinstance(key, str) or not key.startswith("prod::"):
        return None
    parts = key.split("::")
    if len(parts) < 4 or not parts[1] or not parts[3]:
        return None
    return parts[1], parts[2], "::".join(parts[3:])


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


def _products_cache_row_candidate(row: Any) -> tuple[str, Optional[Dict[str, Any]]]:
    """Parse a products_cache row into (merchant_id, product_data dict).

    `databases` returns Record objects (not dicts) whose `.get` attribute
    resolves to a column lookup, so row.get(...) must never be called on the
    raw row. Returns ("", None) / (mid, None) when the row is unusable.
    """
    row_map = _row_to_dict(row)
    mid = str(row_map.get("merchant_id") or "").strip()
    product_data = row_map.get("product_data")
    if isinstance(product_data, str):
        try:
            product_data = json.loads(product_data)
        except Exception:
            return mid, None
    if not isinstance(product_data, dict):
        return mid, None
    return mid, product_data


def _order_row_merchant_items(row: Any) -> tuple[Any, Optional[List[Any]]]:
    """Parse an orders row into (merchant_id, items list).

    Same Record-vs-dict trap as _products_cache_row_candidate: rows from
    database.fetch_all are Record objects, so row.get(...) must never be
    called on the raw row. `items` may arrive as a JSON string or an
    already-decoded list. Returns (merchant_id, None) when items are unusable.
    """
    row_map = _row_to_dict(row)
    merchant_id = row_map.get("merchant_id")
    raw_items = row_map.get("items")
    if isinstance(raw_items, str):
        try:
            raw_items = json.loads(raw_items)
        except Exception:
            return merchant_id, None
    if not isinstance(raw_items, list):
        return merchant_id, None
    return merchant_id, raw_items


def _extract_price_currency_from_variant(
    v: Dict[str, Any], fallback_currency: Optional[str]
) -> tuple[Optional[float], Optional[str]]:
    """Variant price and currency, with UNKNOWN preserved as None.

    `fallback_currency` is a caller-supplied observation, not a default: when the
    caller has nothing, it passes None and this returns None rather than
    inventing one. The signature used to force `str`, which is why the
    offers.resolve caller had its own `or "USD"` tail — the type demanded a
    fabrication. #1634.
    """
    price = (
        v.get("price_amount")
        if v.get("price_amount") is not None
        else v.get("price")
        if v.get("price") is not None
        else v.get("amount")
        if v.get("amount") is not None
        else None
    )
    currency = _observed_currency(
        v.get("price_currency"), v.get("currency"), v.get("currency_code"), fallback_currency
    )
    return (_coerce_float(price), currency)


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


def _extract_raw_commerce_surface(
    *,
    payload_surface: Optional[str],
    request_metadata: Optional[Dict[str, Any]],
) -> Optional[str]:
    raw = str(payload_surface or "").strip()
    if raw and raw.lower() != "unknown":
        return raw
    meta = request_metadata if isinstance(request_metadata, dict) else {}
    for key in ("commerce_surface", "commerceSurface"):
        value = str(meta.get(key) or "").strip()
        if value and value.lower() != "unknown":
            return value
    return None


def _resolve_commerce_surface(
    *,
    payload_surface: Optional[str],
    request_metadata: Optional[Dict[str, Any]],
) -> tuple[str, bool]:
    raw = _extract_raw_commerce_surface(
        payload_surface=payload_surface,
        request_metadata=request_metadata,
    )
    return normalize_commerce_surface(raw), bool(raw)


def _coerce_product_payload_dict(product: Any) -> Dict[str, Any]:
    if isinstance(product, StandardProduct):
        return product.model_dump()
    if hasattr(product, "model_dump"):
        try:
            dumped = product.model_dump()
            if isinstance(dumped, dict):
                return dumped
        except Exception:
            pass
    if isinstance(product, dict):
        return dict(product)
    try:
        return dict(product)
    except Exception:
        return {}


def _coerce_variant_payload_dict(variant: Any) -> Dict[str, Any]:
    if isinstance(variant, dict):
        return dict(variant)
    if hasattr(variant, "model_dump"):
        try:
            dumped = variant.model_dump()
            if isinstance(dumped, dict):
                return dumped
        except Exception:
            pass
    try:
        return dict(variant)
    except Exception:
        return {}


def _variant_ref_from_payload(variant: Dict[str, Any]) -> Optional[str]:
    return (
        str(
            variant.get("variant_id")
            or variant.get("id")
            or variant.get("sku")
            or variant.get("sku_id")
            or ""
        ).strip()
        or None
    )


def _variant_sku_from_payload(variant: Dict[str, Any]) -> Optional[str]:
    return str(variant.get("sku") or variant.get("sku_id") or "").strip() or None


def _product_payment_capabilities(product_payload: Dict[str, Any]) -> Dict[str, Any]:
    direct = product_payload.get("payment_capabilities")
    if isinstance(direct, dict):
        return dict(direct)
    platform_metadata = product_payload.get("platform_metadata")
    if isinstance(platform_metadata, dict):
        nested = platform_metadata.get("payment_capabilities")
        if isinstance(nested, dict):
            return dict(nested)
    return {}


def _product_supports_commerce_surface(
    product_payload: Dict[str, Any],
    commerce_surface: str,
) -> bool:
    if not _is_dict_sellable(product_payload):
        return False
    if commerce_surface == COMMERCE_SURFACE_AGENT_API:
        return True

    payment_capabilities = _product_payment_capabilities(product_payload)
    if payment_capabilities:
        return payment_capabilities_support_surface(payment_capabilities, commerce_surface)

    return product_payload.get("orderable") is not False


def _build_internal_offer_summary(
    *,
    merchant_id: str,
    platform: str,
    product_payload: Dict[str, Any],
    variant_payload: Dict[str, Any],
    confidence: float,
    canonical_ref: Optional[str],
    canonical_group_id: Optional[str],
) -> Dict[str, Any]:
    product_id = str(
        product_payload.get("id")
        or product_payload.get("product_id")
        or ""
    ).strip()
    variant_id = _variant_ref_from_payload(variant_payload)
    price_amount = _coerce_float(
        variant_payload.get("price")
        if variant_payload.get("price") is not None
        else product_payload.get("price")
    ) or 0.0
    currency = str(
        variant_payload.get("currency")
        or product_payload.get("currency")
        or product_payload.get("currency_code")
        or "USD"
    ).upper()
    original_price = _coerce_float(
        variant_payload.get("compare_at_price")
        or product_payload.get("compare_at_price")
    )
    inventory_quantity = _coerce_int(
        variant_payload.get("inventory_quantity")
        if variant_payload.get("inventory_quantity") is not None
        else product_payload.get("inventory_quantity")
    )
    in_stock = True
    if inventory_quantity is not None:
        in_stock = inventory_quantity > 0
    elif isinstance(product_payload.get("in_stock"), bool):
        in_stock = bool(product_payload.get("in_stock"))

    seller = (
        str(
            product_payload.get("merchant_name")
            or product_payload.get("store_name")
            or merchant_id
        ).strip()
        or merchant_id
    )
    offer_id = f"of:internal_checkout:{merchant_id}:{product_id}:{variant_id or '∅'}"
    return {
        "offer_id": offer_id,
        "seller": seller,
        "price": price_amount,
        "currency": currency,
        **({"original_price": original_price} if original_price is not None else {}),
        "in_stock": bool(in_stock),
        "purchase_route": "internal_checkout",
        "affiliate_url": None,
        "internal_checkout_items": [
            {
                "merchant_id": merchant_id,
                "product_id": product_id,
                **({"variant_id": variant_id} if variant_id else {}),
                "quantity": 1,
            }
        ],
        "confidence": confidence,
        "source": {
            "type": "internal_product",
            "merchant_id": merchant_id,
            "platform": platform,
            "product_id": product_id,
            "variant_id": variant_id,
            "canonical_ref": canonical_ref,
            "product_group_id": canonical_group_id,
            "title": str(product_payload.get("title") or "").strip() or None,
            "brand": str(
                product_payload.get("brand")
                or product_payload.get("vendor")
                or product_payload.get("merchant_name")
                or product_payload.get("store_name")
                or ""
            ).strip()
            or None,
        },
    }


def _build_shop_top_offer_summary(
    *,
    merchant_id: str,
    product_payload: Dict[str, Any],
    variant_payload: Dict[str, Any],
    commerce_surface: str,
) -> Dict[str, Any]:
    product_id = str(product_payload.get("product_id") or product_payload.get("id") or "").strip()
    variant_id = _variant_ref_from_payload(variant_payload)
    sku = _variant_sku_from_payload(variant_payload)
    price_amount = _coerce_float(
        variant_payload.get("price")
        if variant_payload.get("price") is not None
        else product_payload.get("price")
    ) or 0.0
    currency = str(
        variant_payload.get("currency")
        or product_payload.get("currency")
        or "USD"
    ).upper()
    return {
        "purchase_route": "internal_checkout",
        "merchant_id": merchant_id,
        "product_id": product_id,
        **({"variant_id": variant_id} if variant_id else {}),
        **({"sku_id": sku} if sku else {}),
        "price": price_amount,
        "currency": currency,
        "commerce_surface": commerce_surface,
    }


def _attach_eligible_serving_fields(
    item: Dict[str, Any],
    product: Any,
    *,
    commerce_surface: str,
) -> Optional[Dict[str, Any]]:
    product_payload = _coerce_product_payload_dict(product)
    merchant_id = str(item.get("merchant_id") or product_payload.get("merchant_id") or "").strip()
    if not merchant_id:
        return None
    if not _product_supports_commerce_surface(product_payload, commerce_surface):
        return None
    first_eligible = pick_first_eligible_variant_from_standard_product(product_payload)
    if not first_eligible:
        return None
    variant_payload = _coerce_variant_payload_dict(first_eligible.get("variant") or {})
    variant_id = _variant_ref_from_payload(variant_payload)
    sku_id = _variant_sku_from_payload(variant_payload)
    attached = dict(item)
    attached["commerce_surface"] = commerce_surface
    attached["top_offer_summary"] = _build_shop_top_offer_summary(
        merchant_id=merchant_id,
        product_payload=product_payload,
        variant_payload=variant_payload,
        commerce_surface=commerce_surface,
    )
    attached["exact_resolution_identifiers"] = {
        "merchant_id": merchant_id,
        "product_id": str(item.get("product_id") or product_payload.get("product_id") or product_payload.get("id") or "").strip(),
        **({"variant_id": variant_id} if variant_id else {}),
        **({"sku_id": sku_id} if sku_id else {}),
    }
    return attached


def _attach_eligible_serving_fields_to_items(
    items: List[Dict[str, Any]],
    *,
    commerce_surface: str,
) -> List[Dict[str, Any]]:
    attached_items: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        attached = _attach_eligible_serving_fields(
            item,
            item,
            commerce_surface=commerce_surface,
        )
        if attached is not None:
            attached_items.append(attached)
    return attached_items


def _rank_offers_merit_first(offers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Rank offers MERIT-FIRST, never by in-agent integration status (T2-4, decision #3).

    Index neutrality is a core differentiator: an external (referred, ``orderable:false``)
    offer must not be demoted purely for being un-integrated. Internal (buy-here) and
    external (referral) offers compete on the same relevance/quality signal — the per-offer
    match ``confidence`` — and in-agent transactability is applied ONLY as a tiebreaker
    between otherwise-equal-FIT offers (an orderable/buy-here offer wins the tie).

    The two tiers score match ``confidence`` on non-comparable scales (internal exact = 0.95
    at ``_build_internal_offer_summary``; external exact = 1.0 in ``_append_external_...``), so
    a RAW-confidence sort would let a 0.05 scale artifact permanently outrank an exact
    buy-here offer with an exact referral — inverting the demotion instead of neutralising it.
    We therefore bucket confidence into shared FIT TIERS (exact / product / loose) so both
    scales' exact matches land in the same tier; only then does transactability break the tie.
    Raw confidence is a final deterministic tiebreak within a tier. We do NOT mutate the source
    confidence values (other code reads them) — the bucketing is local to the sort key.

    Sort key (all ascending): (fit_tier_rank, transactability_rank, -confidence). The sort is
    stable, so equal-key offers keep prior order — a pure-internal set (same-product offers
    share a confidence) is ordered byte-identically to the old ``internal + external`` list.
    """

    def _merit(offer: Dict[str, Any]) -> float:
        try:
            return float(offer.get("confidence") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _fit_tier_rank(confidence: float) -> int:
        # Shared tiers across both scales: internal 0.95/0.8/0.7 and external 1.0/0.8/0.6.
        # exact (internal 0.95 AND external 1.0) collapse to one tier so the tiebreaker fires.
        if confidence >= 0.95:
            return 0  # exact match
        if confidence >= 0.8:
            return 1  # product-level match
        return 2  # loose match

    def _transactability_rank(offer: Dict[str, Any]) -> int:
        # Tiebreaker only: 0 = transactable in-agent (buy-here) wins ties, 1 = referral.
        return 0 if str(offer.get("purchase_route") or "") == "internal_checkout" else 1

    def _key(offer: Dict[str, Any]) -> Tuple[int, int, float]:
        confidence = _merit(offer)
        return (_fit_tier_rank(confidence), _transactability_rank(offer), -confidence)

    return sorted(offers, key=_key)


# THE `resolution_mode` VOCABULARY -- offers.resolve's public answer to "what did you
# actually give me?". Emitted in three places on the success envelope (top level, `mapping`,
# and `metadata`) and, until this type existed, constrained by nothing at all: no enum, no
# Literal, no response_model, no OpenAPI schema, no doc. Third-party agents may already parse
# it and we cannot grep them, so the four values below are a CONTRACT -- extend it additively,
# never repurpose an existing value.
#
#   exact_match              An internal offer ships and no shipped offer had to swap away
#                            from a requested variant it carried. NOTE the honest limit: when
#                            the requested sku_id is not present on the matched product at all,
#                            no variant was identified to compare against, so a product-level
#                            match reports this value too. That predates this vocabulary; it is
#                            written down rather than quietly implied.
#   same_product_substitution An internal offer ships for the right PRODUCT but a different
#                            variant than requested; `substitution_reason_codes` says why.
#   external_only            Offers ship, but all of them are referred (affiliate_outbound);
#                            nothing internal survived, so `resolved_target` stays None.
#   not_servable             Nothing ships. Also the pre-resolution initializer -- the handler
#                            must never claim a match before one is established.
#
# The response envelope returns a raw dict rather than a response_model, so this annotates
# the variable instead. Be honest about what that buys: this repo runs NO static type checker
# in CI (no mypy/pyright anywhere in .github/workflows, requirements*.txt, or any config), so
# the annotation documents the vocabulary and helps a local language server -- it does not
# gate anything. The gate is a test:
# tests/test_offers_resolve.py::test_every_resolution_mode_assignment_is_in_the_vocabulary
# AST-walks this handler and fails on any value not listed here.
ResolutionMode = Literal[
    "exact_match",
    "same_product_substitution",
    "external_only",
    "not_servable",
]

RESOLUTION_MODES: frozenset[str] = frozenset(get_args(ResolutionMode))


async def _handle_offers_resolve(
    payload: OffersResolvePayload,
    request_metadata: Optional[Dict[str, Any]],
    background_tasks: BackgroundTasks,
) -> Dict[str, Any]:
    """
    Resolve purchasable offers for a given sku_id/product_id.

    Contract goal: internal checkout offers and external outbound offers are BOTH first-class
    sources on every commerce surface, ranked merit-first together (T2-4 index neutrality).
    External offers are never a "fallback" a caller's surface choice can switch off — an
    explicit commerce_surface tightens internal-offer servability (strict mode), not sourcing.
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
    commerce_surface, commerce_surface_explicit = _resolve_commerce_surface(
        payload_surface=payload.commerce_surface,
        request_metadata=request_metadata,
    )
    strict_serving_mode = bool(commerce_surface_explicit)
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
    # HONEST INITIALIZER. This is the value a response carries until a resolution is
    # actually ESTABLISHED, so it must not claim one. The previous initializer
    # ("not_servable" strict / "exact_match" relaxed) claimed one on the relaxed
    # surface: a relaxed request that matched nothing at all still reported
    # "we matched your product exactly" next to offers_count=0. #1907 named this
    # initializer "equally untrue" and then fixed only the external-only half.
    #
    # "not_servable" is the honest pre-resolution state on BOTH surfaces: nothing has
    # been shown to serve yet. The assignments below (internal lane) and the
    # shipped-offer reconciliation at the end of this handler upgrade it to the
    # truthful value; neither can be skipped on a path that emits the field.
    resolution_mode: ResolutionMode = "not_servable"
    requested_target: Dict[str, Any] = {
        **({"product_id": product_id} if product_id else {}),
        **({"sku_id": sku_id} if sku_id else {}),
        **({"merchant_id": merchant_scope} if merchant_scope else {}),
    }
    resolved_target: Optional[Dict[str, Any]] = None
    substitution_reason_codes: List[str] = []
    exact_target_matched = False
    surface_not_servable_reason_codes: List[str] = []
    internal_identity_payloads: List[Dict[str, Any]] = []
    # offer_id -> what that internal offer did about the caller's requested variant. Read by
    # the shipped-offer reconciliation, which is the only place the verdict is decided.
    internal_offer_context: Dict[str, Dict[str, Any]] = {}

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
        extra: Optional[Dict[str, Any]] = None,
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
        if extra:
            entry.update(extra)
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
        # T1 fix (docs/IDENTITY_REFERENCE.md §4 "Trap T1"; ADR-009 §Prerequisite fix):
        # external_product_seeds.attached_product_key stores the STORAGE format
        # `prod::{merchant}::{platform}::{pid}` (make_catalog_product_key), NOT the
        # pipe transport form. The prior pipe-format match keys matched zero prod rows
        # (8,004 prod:: / 0 pipe as of 2026-07-05) — a confirmed dead path. Build match
        # keys with make_catalog_product_key + a `prod::{merchant}::%` prefix so the
        # attached-ref mainline actually matches. LIKE values are wildcard-escaped
        # because merchant ids contain `_` (see _escape_like). The 720 bare-format rows
        # are a separate ADR-009 backfill decision and are deliberately NOT matched here
        # (adding a third format would be a new crutch).
        from services.catalog_sync_service import make_catalog_product_key

        params: Dict[str, Any] = {
            "limit": attached_seed_limit,
            "attached_prefix": f"prod::{_escape_like(attached_merchant_id)}::%",
        }
        match_clauses: List[str] = []

        for idx, pid_alias in enumerate(pid_aliases[:8]):
            pid_key = f"attached_pid_{idx}"
            if attached_platform:
                params[pid_key] = make_catalog_product_key(attached_merchant_id, attached_platform, pid_alias)
                match_clauses.append(f"attached_product_key = :{pid_key}")
            else:
                # platform-unknown: prod::{merchant}::<any platform>::{pid}
                params[pid_key] = f"prod::{_escape_like(attached_merchant_id)}::%::{_escape_like(pid_alias)}"
                match_clauses.append(f"attached_product_key LIKE :{pid_key} ESCAPE '\\'")

        for idx, sku_alias in enumerate(sku_aliases[:8]):
            # attached_variant_id stores the RAW variant id (not prod::-prefixed) — the
            # T2-1/T2-2 order-side join key — so exact equality is correct here.
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
                  AND attached_product_key LIKE :attached_prefix ESCAPE '\\'
                  AND ({' OR '.join(match_clauses)})
                  {_seed_quarantine_clause()}
                ORDER BY updated_at DESC, created_at DESC
                LIMIT :limit
                """,
                params,
            ),
            timeout=min(OFFERS_RESOLVE_SEED_QUERY_TIMEOUT_SECONDS, 1.0),
        )
        return list(rows or [])

    def _external_identity_terms_from_product_payloads(
        product_payloads: List[Dict[str, Any]],
    ) -> tuple[List[str], List[str]]:
        title_terms: List[str] = []
        brand_terms: List[str] = []

        def _add_unique(target: List[str], raw: Any, *, min_len: int = 3) -> None:
            value = _normalize_offer_title(str(raw or ""))
            if len(value) >= min_len and value not in target:
                target.append(value)

        for product_payload in product_payloads[:6]:
            if not isinstance(product_payload, dict):
                continue
            title = str(product_payload.get("title") or "").strip()
            normalized_title = _normalize_offer_title(title)
            _add_unique(title_terms, title, min_len=5)

            raw_brand_values = [
                product_payload.get("brand"),
                product_payload.get("vendor"),
                product_payload.get("merchant_name"),
                product_payload.get("store_name"),
            ]
            brands_before = list(brand_terms)
            for raw_brand in raw_brand_values:
                _add_unique(brand_terms, raw_brand, min_len=3)

            # Shopify merchant products often duplicate the brand in title
            # (e.g. "KraveBeauty Great Barrier Relief") while external seeds
            # may keep the canonical product title ("Great Barrier Relief").
            for brand in brands_before + brand_terms:
                if normalized_title.startswith(f"{brand} "):
                    _add_unique(title_terms, normalized_title[len(brand) + 1 :], min_len=5)

        return title_terms[:8], brand_terms[:8]

    async def _fetch_external_seed_rows_by_internal_identity(
        product_payloads: List[Dict[str, Any]],
    ) -> List[Any]:
        title_terms, brand_terms = _external_identity_terms_from_product_payloads(product_payloads)
        if not title_terms:
            return []

        params: Dict[str, Any] = {
            "limit": attached_seed_limit,
            "market": market_hint,
            "tool": tool_hint,
        }
        title_clauses: List[str] = []
        for idx, term in enumerate(title_terms):
            key = f"identity_title_{idx}"
            params[key] = f"%{term}%"
            title_clauses.append(
                "("
                f"LOWER(COALESCE(title,'')) LIKE :{key}"
                f" OR LOWER(COALESCE(seed_data->>'title','')) LIKE :{key}"
                ")"
            )

        brand_clause = "TRUE"
        if brand_terms:
            brand_clauses: List[str] = []
            for idx, brand in enumerate(brand_terms):
                key = f"identity_brand_{idx}"
                params[key] = f"%{brand}%"
                brand_clauses.append(
                    "("
                    f"LOWER(COALESCE(seed_data->>'brand','')) LIKE :{key}"
                    f" OR LOWER(COALESCE(seed_data->>'vendor','')) LIKE :{key}"
                    f" OR LOWER(COALESCE(domain,'')) LIKE :{key}"
                    f" OR LOWER(COALESCE(canonical_url,'')) LIKE :{key}"
                    f" OR LOWER(COALESCE(destination_url,'')) LIKE :{key}"
                    f" OR LOWER(COALESCE(title,'')) LIKE :{key}"
                    ")"
                )
            brand_clause = "(" + " OR ".join(brand_clauses) + ")"

        rows = await asyncio.wait_for(
            database.fetch_all(
                f"""
                SELECT *
                FROM external_product_seeds
                WHERE status = 'active'
                  AND (CAST(:market AS TEXT) IS NULL OR market = CAST(:market AS TEXT) OR market = '*')
                  AND (CAST(:tool AS TEXT) IS NULL OR tool = CAST(:tool AS TEXT) OR tool = '*')
                  AND ({' OR '.join(title_clauses)})
                  {_seed_quarantine_clause()}
                  AND {brand_clause}
                ORDER BY
                  CASE WHEN attached_product_key IS NULL THEN 1 ELSE 0 END ASC,
                  updated_at DESC,
                  created_at DESC
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

                # The THIRD seed-currency chain, and the one the first cut of
                # #1634 missed: same two-source observation, on the
                # offers.resolve serving path. It fabricated "USD" and published
                # it as the external offer's currency.
                price_amount, currency = _extract_price_currency_from_variant(
                    v,
                    fallback_currency=_observed_currency(
                        row_dict.get("price_currency"), seed_data.get("price_currency")
                    ),
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

                redirect_identity = _external_seed_redirect_identity(
                    row=row_dict,
                    seed_data=seed_data,
                    offer_variant_id=_seed_offer_variant_id(v) or None,
                )
                # T2-12: mint the join key HERE, not inside the builder, and hand the same one
                # to both. The id has to be identical on the surface_click_events row, on the
                # merchant's order, and on the `cart_url` / `pdp_url` we publish below — if the
                # builder minted its own, the agent's lane and the /r hop would be two different
                # clicks and the order-side join would silently split.
                stable_click_id = new_click_id()
                redirect_url = await _make_external_redirect_url(
                    market=used_market,
                    tool=used_tool,
                    destination_url=str(canonical_url or destination_url),
                    utm_template=row_dict.get("utm_template") or seed_data.get("utm_template"),
                    click_id=stable_click_id,
                    ctx={
                        "seedId": seed_id,
                        "variantId": vid,
                        "eventType": "outbound_opened",
                        "source": "offers.resolve",
                        **({"skuId": sku_id} if sku_id else {}),
                        **({"productId": product_id} if product_id else {}),
                    },
                    merchant_id=redirect_identity["merchant_id"],
                    product_id=redirect_identity["product_id"],
                    variant_id=redirect_identity["variant_id"],
                    cart_variant_id=redirect_identity.get("cart_variant_id"),
                    shop_domain=redirect_identity["shop_domain"],
                    platform=redirect_identity["platform"],
                    seller_ref=redirect_identity["seller_ref"],
                    seed_kind=redirect_identity["seed_kind"],
                )
                if not redirect_url:
                    continue

                # EXECUTION SPEC v0. Composed by the SAME function the redirect itself used, with
                # the SAME click id, so `cart_url` cannot describe a different destination from
                # the one `affiliate_url` resolves to.
                composed_spec = compose_attributed_destinations(
                    # The SAME value the builder above was called with (`canonical_url or
                    # destination_url`), not the raw column — _seed_domain_from_url reads the
                    # host off it, so a different input here could disagree with the link.
                    destination_url=str(canonical_url or destination_url),
                    utm_template=row_dict.get("utm_template") or seed_data.get("utm_template"),
                    market=used_market,
                    tool=used_tool,
                    shop_domain=redirect_identity.get("shop_domain"),
                    platform=redirect_identity.get("platform"),
                    cart_variant_id=redirect_identity.get("cart_variant_id"),
                    click_id=stable_click_id,
                )
                # ONE decision, read twice. `cart_prefilled` and `execution_spec.rail` answer the
                # same question — what does following our link land the buyer in — so computing
                # them separately would let them disagree about a single offer in a single
                # payload. Deriving both from this call makes that impossible by construction.
                prefilled_claim = _cart_prefilled_claim(
                    cart_url=composed_spec["cart_url"],
                    # The SAME value compose_attributed_destinations was given
                    # (`canonical_url or destination_url`), not the raw column —
                    # _seed_domain_from_url reads the host off it, so a different input here
                    # could disagree with the link it describes.
                    destination_url=str(canonical_url or destination_url),
                    # The link we JUST minted, so the resolve-time rollout bucket is computed
                    # from the very token the click will carry.
                    redirect_url=redirect_url,
                )

                offer_spec = {
                    "merchant_domain": normalize_shop_host(
                        redirect_identity.get("shop_domain")
                        or row_dict.get("domain")
                        or seed_data.get("domain")
                        or str(canonical_url or destination_url)
                    )
                    or None,
                    # F1: the allowlist ran on `primary_unkeyed` — which is the CART when one
                    # exists, built on shop_domain. `pdp_url` comes from `canonical_url or
                    # destination_url`, which can be a DIFFERENT host that nothing vetted. Before
                    # this spec that URL was never emitted, so publishing it unchecked — with our
                    # click id on it — would be net-new egress to an unapproved destination, and
                    # would also contradict the `merchant_domain` printed beside it. Publish it
                    # only when it is the same host the allowlist already approved.
                    "pdp_url": (
                        composed_spec["pdp_url"]
                        if _seed_domain_from_url(composed_spec["pdp_url"])
                        == _seed_domain_from_url(composed_spec["primary_unkeyed"])
                        else None
                    ),
                    "cart_url": composed_spec["cart_url"],
                    # The NUMERIC storefront variant id a cart permalink can actually be built
                    # from — not the catalog SKU. None when we could not justify one, which is
                    # exactly when cart_url is None too.
                    "variant_id": redirect_identity.get("cart_variant_id"),
                    # What the buyer is handed off TO. `shopify_cart` and `referral` are the two
                    # this lane can produce today. UCP is NOT claimed here: this route does not
                    # call a merchant's UCP endpoint, and naming a rail we do not execute would
                    # be the fabrication the rest of this spec exists to avoid.
                    #
                    # NULL is the third state, for the same reason `cart_prefilled` has one and
                    # keyed off the SAME decision so the two can never disagree. `"referral"` is
                    # a positive claim about where the buyer ends up, and it is emitted on
                    # exactly the cold population the warm-handoff click lane targets: an agent
                    # that hands the buyer `affiliate_url` — the attributed link we want them to
                    # use — can be told "referral" and have the buyer land in a prefilled cart.
                    # Same defect as a falsifiable `cart_prefilled: false`, same fix.
                    #
                    # NOT nulled merely because a cart exists: a `shopify_cart` cannot be
                    # falsified (the lane only ever BUILDS carts, and since #1848 it refuses a
                    # dest that is already one), so the `true` side needs no guard here either.
                    #
                    # Safe to send: the gateway passes `rail` through as an opaque label rather
                    # than checking it against a known set (PIVOTA-Agent
                    # `src/agentSignals/offerToSignal.js::toExecutionSpec`), so a null reads as
                    # "no rail named" rather than breaking a consumer.
                    "rail": (
                        None
                        if prefilled_claim is None
                        else "shopify_cart" if composed_spec["cart_url"] else "referral"
                    ),
                    "expires_at": _redirect_token_expiry(redirect_url),
                    # T2-12: attribution on the lane the agent actually uses. Until now the join
                    # key existed only inside the signed /r token, so an agent that used
                    # `pdp_url` / `cart_url` directly generated revenue we could not see.
                    "tracking": {
                        "click_id": stable_click_id,
                        # F2: the carrier differs by join mode and the agent needs the one that is
                        # actually IN the URL it was handed. A cart carries the id as a cart
                        # ATTRIBUTE; a referral carries it as a plain query param. Naming
                        # REFERRAL_CLICK_PARAM unconditionally pointed an agent following
                        # `cart_url` at a string that appears nowhere in it.
                        "param": (
                            SHOPIFY_CART_CLICK_ATTRIBUTE
                            if composed_spec["cart_url"]
                            else REFERRAL_CLICK_PARAM
                        ),
                        "join_mode": composed_spec["join_mode"],
                    },
                }

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
                        # EXECUTION SPEC v0, one field: what does following `affiliate_url`
                        # actually DO? It resolves either to a PRE-FILLED CART on the
                        # merchant's own storefront or to a bare product page, and until now
                        # an agent could not tell which — the decision was computed inside
                        # _make_external_redirect_url, stamped into the signed token as
                        # `join_mode`, and never returned. An agent planning a card-rail
                        # handoff needs it: a prefilled cart is a materially different
                        # completion path from "land on a PDP and find the variant yourself".
                        #
                        # Computed by the SAME resolve_cart_permalink the redirect itself
                        # uses, so the answer cannot drift from what the link does.
                        #
                        # SCOPE — this is RESOLVE-TIME truth, and one lane can change it at
                        # CLICK time. The public redirect (`GET /r`, routes/outbound_links.py)
                        # has a warm-handoff lane that, for an eligible destination, calls the
                        # gateway's internal resolve and 302s the shopper to a PRE-BUILT cart
                        # on the brand's own storefront instead of to `dest`
                        # (services/outbound_warm_handoff.py :: evaluate_warm_eligibility).
                        # Nothing in that lane looks at what we said here.
                        #
                        # The exposure is ONE-SIDED, and it is the `false` side:
                        #   - `true`  -> dest is already a cart permalink; a warm handoff only
                        #                ever BUILDS a cart, so the claim cannot be falsified.
                        #   - `false` -> we told the agent "this lands on a product page, the
                        #                buyer picks the variant themselves". If the lane
                        #                fires, the buyer lands in a prefilled cart and the
                        #                answer we already sent is wrong.
                        #
                        # NOT hypothetical: OUTBOUND_WARM_HANDOFF_ENABLED is `true` on the
                        # serving prod revision with a live internal key and
                        # OUTBOUND_WARM_HANDOFF_BRANDS set to six brand domains (verified
                        # 2026-08-22 against Cloud Run `web`, us-west1). The code DEFAULT in
                        # config/settings.py is false — read the deployed env, not the default.
                        #
                        # Fed the cart_url `compose_attributed_destinations` ALREADY decided,
                        # so the claim cannot drift from the `execution_spec.cart_url` printed
                        # beside it or from the link `affiliate_url` resolves to.
                        # See docs/runbooks/outbound_warm_handoff_rollout.md.
                        # Computed above as `prefilled_claim`, and shared with
                        # `execution_spec.rail` so the two cannot contradict each other.
                        "cart_prefilled": prefilled_claim,
                        "execution_spec": offer_spec,
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
        # Fuzzy (external_product_id/title/url LIKE) is the default label; the
        # attached-ref mainline overwrites it on success so telemetry can alarm on
        # the fuzzy:attached ratio (founder directive: fuzzy must not be a silent
        # fallback for attached seeds). See IDENTITY_REFERENCE Trap T1 / ADR-009.
        query_label = "external_seed_by_fuzzy_ref"
        where_clauses = ["status = 'active'"]
        params: Dict[str, Any] = {"limit": attached_seed_limit}
        seed_rows: List[Any] = []
        # (merchant, pid) pairs the attached-ref mainline actually searched — used to
        # tell a genuine mainline miss (fuzzy surfaced a seed the mainline SHOULD have
        # matched) from a legitimate cross-merchant / rebound-store fuzzy match.
        searched_attached_merchants: set[str] = set()
        searched_attached_pids: set[str] = {a for a in product_id_aliases if a}

        if merchant_scope and (product_id_aliases or sku_id_aliases):
            searched_attached_merchants.add(merchant_scope)
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
                prefetched_merchant = str(prefetched_internal.get("merchant_id") or "").strip() or None
                prefetched_pid = str(prefetched_internal.get("product_id") or "").strip() or None
                if prefetched_merchant:
                    searched_attached_merchants.add(prefetched_merchant)
                if prefetched_pid:
                    searched_attached_pids.add(prefetched_pid)
                seed_rows = await _fetch_attached_seed_rows(
                    merchant_id=prefetched_merchant,
                    platform=str(prefetched_internal.get("platform") or "").strip() or None,
                    product_aliases=[prefetched_pid] + product_id_aliases,
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
                    {_seed_quarantine_clause()}
                    ORDER BY updated_at DESC, created_at DESC
                    LIMIT :limit
                    """,
                    params,
                ),
                timeout=OFFERS_RESOLVE_SEED_QUERY_TIMEOUT_SECONDS,
            )

        # T1 mainline-miss telemetry (founder directive: no silent fuzzy fallback for
        # attached seeds). The fuzzy query legitimately surfaces STANDALONE seeds
        # (attached_product_key NULL) and rebound-store / cross-merchant matches. But
        # if it surfaces a seed whose STORAGE-format attached key names a (merchant, pid)
        # the attached-ref mainline actually searched, the mainline SHOULD have matched
        # it and didn't — that is OBSERVABLE breakage, not a pass. Deliver the offer
        # anyway (honest delivery); just make the miss alarmable. See IDENTITY_REFERENCE
        # Trap T1 / ADR-009 §Prerequisite fix.
        mainline_miss_seed_ids: List[str] = []
        if query_label == "external_seed_by_fuzzy_ref":
            for row in seed_rows or []:
                row_dict = _row_to_dict(row)
                parsed = _parse_catalog_product_key(row_dict.get("attached_product_key"))
                if not parsed:
                    continue
                seed_merchant, _seed_platform, seed_pid = parsed
                if seed_merchant in searched_attached_merchants and seed_pid in searched_attached_pids:
                    mainline_miss_seed_ids.append(str(row_dict.get("id") or ""))
        if mainline_miss_seed_ids:
            logger.warning(
                "attached_seed_mainline_miss",
                extra={
                    "event": "attached_seed_mainline_miss",
                    "seed_ids": mainline_miss_seed_ids,
                    "product_aliases": product_id_aliases,
                    "sku_aliases": sku_id_aliases,
                    "searched_merchants": sorted(searched_attached_merchants),
                },
            )

        await _append_external_offers_from_seed_rows(list(seed_rows or []))
        _record_source(
            source="external_product_seeds",
            status="ok",
            reason_code="ok",
            source_started=external_started,
            row_count=len(seed_rows or []),
            query=query_label,
            extra=(
                {"mainline_miss_seed_ids": mainline_miss_seed_ids}
                if mainline_miss_seed_ids
                else None
            ),
        )
    except Exception as e:
        logger.info("offers.resolve.external.failed", extra={"error": str(e)})
        _record_source(
            source="external_product_seeds",
            status="error",
            reason_code=_classify_db_reason_code(e),
            source_started=external_started,
            error=type(e).__name__,
            query=query_label,
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

                product_payload = dict(product_data)
                exact_pid_match = bool(product_id_aliases and pid in product_id_aliases)
                variants = product_payload.get("variants") if isinstance(product_payload.get("variants"), list) else []
                exact_variant: Optional[Dict[str, Any]] = None
                exact_variant_projection: Optional[Dict[str, Any]] = None
                if sku_id_aliases and isinstance(variants, list):
                    for raw_variant in variants:
                        variant_payload = _coerce_variant_payload_dict(raw_variant)
                        vid = _variant_ref_from_payload(variant_payload)
                        if vid and vid in sku_id_aliases:
                            exact_variant = variant_payload
                            exact_variant_projection = build_agent_push_projection_from_standard_variant(
                                variant_payload,
                                product_currency=product_payload.get("currency") or product_payload.get("currency_code"),
                                checked_at=product_payload.get("updated_at")
                                or product_payload.get("published_at")
                                or product_payload.get("created_at"),
                            )
                            break

                surface_product_eligible = _product_supports_commerce_surface(
                    product_payload,
                    commerce_surface,
                )
                if exact_variant is not None or exact_pid_match or not sku_id_aliases:
                    exact_target_matched = True
                    product_identity_key = f"{merchant_id}:{platform}:{pid}"
                    if not any(
                        str(item.get("_identity_key") or "") == product_identity_key
                        for item in internal_identity_payloads
                    ):
                        identity_payload = dict(product_payload)
                        identity_payload["_identity_key"] = product_identity_key
                        internal_identity_payloads.append(identity_payload)

                chosen_bundle = (
                    pick_first_eligible_variant_from_standard_product(product_payload)
                    if surface_product_eligible
                    else None
                )
                chosen_variant = _coerce_variant_payload_dict(chosen_bundle.get("variant") or {}) if chosen_bundle else {}
                chosen_projection = dict(chosen_bundle.get("projection") or {}) if chosen_bundle else {}
                chosen_variant_id = _variant_ref_from_payload(chosen_variant)

                variant_id = chosen_variant_id or (sku_id or None)
                offer_id = f"of:internal_checkout:{merchant_id}:{pid}:{variant_id or '∅'}"
                if offer_id in seen_internal_offer_ids:
                    continue

                if strict_serving_mode:
                    if not surface_product_eligible:
                        if not surface_not_servable_reason_codes:
                            surface_not_servable_reason_codes = ["surface_not_enabled"]
                        mapping_candidates.append(
                            _conf(
                                "internal_product",
                                0.0,
                                "surface_not_enabled",
                                {
                                    "merchant_id": merchant_id,
                                    "platform": platform,
                                    "product_id": pid,
                                    "commerce_surface": commerce_surface,
                                },
                            )
                        )
                        continue
                    if not chosen_variant:
                        if exact_variant_projection and not surface_not_servable_reason_codes:
                            surface_not_servable_reason_codes = list(
                                exact_variant_projection.get("agent_push_reason_codes") or []
                            )
                        elif not surface_not_servable_reason_codes:
                            surface_not_servable_reason_codes = ["not_servable"]
                        mapping_candidates.append(
                            _conf(
                                "internal_product",
                                0.0,
                                "matched_but_not_servable",
                                {
                                    "merchant_id": merchant_id,
                                    "platform": platform,
                                    "product_id": pid,
                                    "requested_variant_id": _variant_ref_from_payload(exact_variant or {}),
                                    "commerce_surface": commerce_surface,
                                    "reason_codes": list(surface_not_servable_reason_codes),
                                },
                            )
                        )
                        continue
                else:
                    if not chosen_variant and isinstance(variants, list) and variants:
                        first = variants[0]
                        chosen_variant = _coerce_variant_payload_dict(first if isinstance(first, dict) else {})
                        # REFRESH. This relaxed-only fallback swaps in a different variant AFTER
                        # chosen_variant_id was bound above, and everything downstream that asks
                        # "which variant did we ship?" -- resolved_target, and the requested-vs-
                        # shipped comparison -- reads chosen_variant_id. Leaving it stale made a
                        # relaxed response that shipped EXACTLY the requested variant report
                        # same_product_substitution, because a stale None can never equal the
                        # requested id. It also gave resolved_target a sku_id (read from the
                        # fresh variant) with no variant_id (read from the stale id).
                        # `offer_id` above is deliberately left alone: it is the dedupe key for
                        # rows already seen this pass, and re-keying it would change which rows
                        # collapse.
                        chosen_variant_id = _variant_ref_from_payload(chosen_variant)

                exact_sku_match = bool(sku_id_aliases and exact_variant and _variant_ref_from_payload(exact_variant) in sku_id_aliases)
                confidence = 0.95 if (exact_pid_match or exact_sku_match) else 0.8 if sku_id else 0.7
                # ADR-009 ratified decision 1 (no-fallback): the offer keys on
                # product_group_id UNCONDITIONALLY. A product with no pg has no
                # canonical identity yet (store-less / thin row) — it gets NO
                # canonical_ref (honest-absent), never a merchant-scoped `pc:`
                # substitute. Singleton pg minting (services.product_group_
                # autogrouper.ensure_singleton_group_membership + the backfill)
                # makes pg total for every content_key'd product, so the former
                # `else pc:…` branch is dead; removing it kills the crutch.
                canonical_ref = (
                    f"pg:{canonical_group_id}" if canonical_group_id else None
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

                seen_internal_offer_ids.add(offer_id)
                internal_summary = _build_internal_offer_summary(
                    merchant_id=str(merchant_id),
                    platform=str(platform),
                    product_payload=product_payload,
                    variant_payload=chosen_variant,
                    confidence=confidence,
                    canonical_ref=canonical_ref,
                    canonical_group_id=canonical_group_id,
                )
                internal_offers.append(internal_summary)
                resolved_target = {
                    "merchant_id": str(merchant_id),
                    "platform": str(platform),
                    "product_id": pid,
                    **({"variant_id": chosen_variant_id} if chosen_variant_id else {}),
                    **({"sku_id": _variant_sku_from_payload(chosen_variant)} if _variant_sku_from_payload(chosen_variant) else {}),
                }
                # BOOKKEEPING ONLY -- this row records what IT did; it does not decide the
                # response. `resolution_mode` documents what the RESPONSE delivered, and a
                # per-row assignment cannot answer that: rows are a loop, so the LAST row
                # silently overwrote every earlier one. A request that matched its variant
                # exactly in row 1 and substituted in row 2 reported "substitution" while
                # shipping both offers, and the reason codes from a substituting row leaked
                # onto a later row's "exact_match". The verdict is taken once, below, from
                # the offers that actually ship.
                #
                # Keyed on the BUILT summary's offer_id (not the dedupe `offer_id` above,
                # which may key on a stale variant); apply_verdicts copies every key, so it
                # survives live verification.
                requested_variant_id = _variant_ref_from_payload(exact_variant or {})
                row_substituted = bool(requested_variant_id and chosen_variant_id != requested_variant_id)
                row_codes: List[str] = []
                if row_substituted:
                    row_codes = list(
                        (exact_variant_projection or {}).get("agent_push_reason_codes") or []
                    )
                    if "requested_variant_not_servable" not in row_codes:
                        row_codes.append("requested_variant_not_servable")
                internal_offer_context[str(internal_summary.get("offer_id") or "")] = {
                    "substituted": row_substituted,
                    "substitution_reason_codes": row_codes,
                    "resolved_target": dict(resolved_target),
                }
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

    if not external_offers and internal_identity_payloads:
        identity_retry_started = time.perf_counter()
        try:
            identity_rows = await _fetch_external_seed_rows_by_internal_identity(internal_identity_payloads)
            await _append_external_offers_from_seed_rows(identity_rows)
            _record_source(
                source="external_product_seeds_identity_retry",
                status="ok" if identity_rows else "empty",
                reason_code="ok" if identity_rows else "no_candidates",
                source_started=identity_retry_started,
                row_count=len(identity_rows or []),
                query="external_seed_by_internal_identity",
            )
        except Exception as e:
            logger.info("offers.resolve.external.identity_retry.failed", extra={"error": str(e)})
            _record_source(
                source="external_product_seeds_identity_retry",
                status="error",
                reason_code=_classify_db_reason_code(e),
                source_started=identity_retry_started,
                error=type(e).__name__,
                query="external_seed_by_internal_identity",
            )

    # T2-4 (decision #3 — index neutrality): rank internal (buy-here) and external (referred)
    # offers MERIT-FIRST in one list, never by integration status (was: internal-first block).
    offers = _rank_offers_merit_first(internal_offers + external_offers)
    offers = offers[:limit]

    # LIVE VERIFICATION (audit item 6). Ranking decides WHICH offers we would hand over; this
    # decides whether the top few are still true. It runs AFTER the truncation on purpose — the
    # 1.5s budget is per turn, so verifying anything the caller will not see spends it for nothing.
    #
    # Default OFF. Arming it adds request-path egress to third parties on the shared crawl NAT IP,
    # which is exactly the traffic the dedicated crawl subnet exists to isolate.
    #
    # Failure here must never cost the turn: a verifier that raised would turn the 31.1%
    # wrong-spec problem into a 100% no-answer problem, which is strictly worse.
    if live_offer_verification.is_enabled() and offers:
        try:
            verdicts = await live_offer_verification.verify_offers(offers)
            offers = live_offer_verification.apply_verdicts(offers, verdicts)
        except Exception as exc:  # noqa: BLE001
            logger.warning("live offer verification failed; serving unverified: %s", exc)

    # RECONCILE resolution_mode AGAINST WHAT ACTUALLY SHIPS. Everything above describes
    # what we RESOLVED; `offers` is what the caller RECEIVES, and the two diverge twice:
    #
    #   1. ranking truncates to `limit`;
    #   2. live_offer_verification.apply_verdicts DROPS every offer it proved GONE, and
    #      it can drop all of them.
    #
    # #1907 stamped "external_only" ABOVE this block, so a response whose external offers
    # were all verified-away announced "external_only" over offers_count=0. That is latent
    # only because LIVE_OFFER_VERIFICATION_ENABLED defaults OFF; arming the flag would have
    # made it live. Deriving from `offers` here is what makes the flag safe to arm.
    #
    # Partitioned on purchase_route, the same discriminator _rank_offers_merit_first uses.
    shipped_internal = [
        offer
        for offer in offers
        if isinstance(offer, dict) and str(offer.get("purchase_route") or "") == "internal_checkout"
    ]
    shipped_internal_count = len(shipped_internal)
    shipped_external_count = len(offers) - shipped_internal_count

    if not offers:
        # Nothing serves. True whatever we matched upstream -- a match whose only offer was
        # dropped is not a match the caller can act on.
        resolution_mode = "not_servable"
        resolved_target = None
        substitution_reason_codes = []
    elif not shipped_internal:
        # Only referred offers survived. Also catches "internal resolved but every internal
        # offer was truncated away or verified away", which the old pre-verification stamp
        # could not see. resolved_target is cleared to match: nothing internal reached the
        # caller, so a target left over from a candidate that did not ship would describe a
        # resolution this response does not contain -- which is what the vocabulary above
        # promises never happens ("external_only ... resolved_target stays None").
        resolution_mode = "external_only"
        resolved_target = None
        substitution_reason_codes = []
    else:
        # BEST WINS, NOT LAST WINS. If ANY shipped internal offer carries the variant the
        # caller asked for, this response did match exactly -- regardless of what some other
        # row did. Only when no shipped offer satisfied the request is it a substitution, and
        # then the reason codes come from the offer we actually name in resolved_target rather
        # than from whichever row happened to run last.
        exact_offer = next(
            (
                offer
                for offer in shipped_internal
                if not (
                    internal_offer_context.get(str(offer.get("offer_id") or "")) or {}
                ).get("substituted")
            ),
            None,
        )
        chosen_offer = exact_offer if exact_offer is not None else shipped_internal[0]
        chosen_context = internal_offer_context.get(str(chosen_offer.get("offer_id") or "")) or {}
        if chosen_context.get("resolved_target"):
            resolved_target = dict(chosen_context["resolved_target"])
        if exact_offer is not None:
            resolution_mode = "exact_match"
            substitution_reason_codes = []
        else:
            resolution_mode = "same_product_substitution"
            substitution_reason_codes = list(chosen_context.get("substitution_reason_codes") or [])

    # ADR-009 ratified decision 1 (no-fallback): canonical_ref is pg-keyed or
    # ABSENT — never a merchant-scoped `pc:{merchant}:{platform}:{pid}`
    # substitute. The internal build above only ever stamps a `pg:…`
    # source.canonical_ref (or None), so propagating it here cannot resurrect
    # the removed `pc:` crutch.
    canonical_ref: Optional[str] = None
    if canonical_group_id:
        canonical_ref = f"pg:{canonical_group_id}"
    elif internal_offers:
        src = (internal_offers[0].get("source") or {}) if isinstance(internal_offers[0], dict) else {}
        if isinstance(src, dict) and src.get("canonical_ref"):
            canonical_ref = str(src.get("canonical_ref"))

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
    elif strict_serving_mode and exact_target_matched:
        reason_code = "NOT_SERVABLE"
        reason = "not_servable"
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
        "commerce_surface": commerce_surface,
        "resolution_mode": resolution_mode,
        "requested_target": requested_target,
        "resolved_target": resolved_target,
        "substitution_reason_codes": substitution_reason_codes,
        "offers": offers,
        "offers_count": len(offers),
        **({"canonical_product_ref": canonical_ref} if canonical_ref else {}),
        "mapping": {
            "canonical_ref": canonical_ref,
            "canonical_product_group_id": canonical_group_id,
            # ADR-009 decision 1: observable honest-absent state. `resolved`
            # when the offer keyed on a product_group_id; `no_canonical_identity`
            # when the product has no pg yet (store-less / thin, pg-NULL) — an
            # explicit reason, NOT a silent content_key/`pc:` substitution.
            "canonical_identity_status": (
                "resolved" if canonical_ref else "no_canonical_identity"
            ),
            "canonical_product": canonical_product,
            "requested_target": requested_target,
            "resolved_target": resolved_target,
            "resolution_mode": resolution_mode,
            "substitution_reason_codes": substitution_reason_codes,
            "candidates": mapping_candidates[:50],
        },
        "metadata": {
            "source": "offers.resolve",
            "commerce_surface": commerce_surface,
            # Counted from what SHIPPED, not from the pre-ranking/pre-verification
            # candidate lists. Sourced from `external_offers`/`internal_offers` these
            # could read true over an empty `offers` -- truncation or a GONE verdict
            # removes the offer but not the candidate it came from.
            "has_external": bool(shipped_external_count),
            "has_internal": bool(shipped_internal_count),
            "merchant_scope": merchant_scope,
            "reason_code": reason_code,
            "reason": reason,
            "latency_ms": latency_ms,
            "sources": source_status,
            "failure_breakdown": failure_breakdown,
            "requested_target": requested_target,
            "resolved_target": resolved_target,
            "resolution_mode": resolution_mode,
            "substitution_reason_codes": substitution_reason_codes,
            "servable_reason_codes": surface_not_servable_reason_codes,
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
    selected_payment_offer_id: Optional[str] = None
    payment_method_evidence: Optional[Dict[str, Any]] = None
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


class RecordPaymentOfferEvidencePayload(BaseModel):
    order_id: Optional[str] = None
    quote_id: Optional[str] = None
    merchant_id: Optional[str] = None
    selected_payment_offer_id: Optional[str] = None
    payment_method_evidence: Dict[str, Any] = Field(default_factory=dict)
    payment_offer_evidence: Optional[Dict[str, Any]] = None
    surface: str = "checkout"
    event_type: Optional[str] = None
    idempotency_key: Optional[str] = None


class CreatePaymentLinkPayload(BaseModel):
    """Payload for the create_payment_link operation (guest hosted checkout).

    Sent FLAT by the safety-kernel executor (canonicalExecutor.createPaymentLink):
      { "order_id": "...", "customer_email": "...", "shipping_address": {...},
        "return_url": "...", "user_ref": "..." }

    Proxied to POST /agent/v2/payments/checkout-sessions, which turns an EXISTING order
    into a HOSTED Stripe Checkout page the buyer pays on. This path NEVER charges — there
    is no submit_payment here; the buyer authorizes by paying on the returned page.
    """

    order_id: str
    customer_email: Optional[str] = None
    shipping_address: Optional[Dict[str, Any]] = None
    return_url: Optional[str] = None
    user_ref: Optional[str] = None


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


# Only strip an EXPLICIT storefront suffix ("… Official Site/Store/Shop",
# "… Flagship Store") — never a bare trailing "Store"/"Shop", which would mangle a
# legitimate brand name (e.g. "The Body Shop" → "The Body").
_BRAND_MERCHANT_NAME_SUFFIX_RE = re.compile(
    r"\s+(official\s+(site|store|shop)|flagship\s+store)\s*$",
    re.IGNORECASE,
)
# Degenerate display names that carry no real brand (the external-seed display-name
# builder falls back to a bare "Official Site" when no brand/domain is known) — map
# these to no brand rather than emit garbage.
_STOREFRONT_ONLY_BRAND_NAMES = frozenset(
    {
        "", "official", "official site", "official store", "official shop",
        "flagship store", "site", "store", "shop",
    }
)


def _clean_brand_from_merchant_name(merchant_name: Optional[str]) -> Optional[str]:
    """Derive a brand from a merchant display name: strip an explicit storefront
    suffix and drop degenerate storefront-only names (e.g. "Official Site")."""
    name = str(merchant_name or "").strip()
    if not name:
        return None
    cleaned = _BRAND_MERCHANT_NAME_SUFFIX_RE.sub("", name).strip()
    if cleaned.lower() in _STOREFRONT_ONLY_BRAND_NAMES:
        return None
    return cleaned or None


def _derive_product_brand(p: StandardProduct) -> Optional[str]:
    """Populate a structured brand for the product contract so agents can cite
    "<brand>'s <product>" without parsing the title. StandardProduct.vendor is the
    catalog brand (hydrated from products_cache product_data->>'vendor'); fall back
    to the merchant display name with a trailing " Official Site/Store" suffix
    stripped. Previously the projection emitted no brand at all (always null)."""
    vendor = str(getattr(p, "vendor", None) or "").strip()
    if vendor:
        return vendor
    brand = str(getattr(p, "brand", None) or "").strip()
    if brand:
        return brand
    return _clean_brand_from_merchant_name(getattr(p, "merchant_name", None))


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
        "brand": _derive_product_brand(p),
        "description": p.description or "",
        "price": p.price,
        "currency": p.currency,
        "image_url": image_url,
        "product_type": p.product_type,
        "inventory_quantity": p.inventory_quantity,
        "sku": p.sku,
        "platform": p.platform,
    }

    # Storefront identity for the attributed-redirect lane (P2b): lets the
    # post-pass derive the merchant-store PDP destination. Additive; absent for
    # platforms whose sync captures neither.
    if getattr(p, "handle", None):
        base["handle"] = p.handle
    if getattr(p, "online_store_url", None):
        base["online_store_url"] = p.online_store_url

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


async def _attach_connected_product_redirects(
    products: Any,
    *,
    market: Optional[str] = None,
    tool: Optional[str] = None,
) -> None:
    """P2b (attributed-redirect lane): stamp signed /r attribution links onto
    CONNECTED-merchant product cards, in place. External-seed cards are already
    stamped at build time; connected cards previously carried no outbound link
    at all, so agent click-outs to connected stores were unattributed.

    Additive only — sets ``external_redirect_url`` when a merchant-store
    destination is derivable: ``online_store_url``, else (Shopify) the
    connected shop domain + ``handle``. A connected Shopify product with a
    numeric variant id yields join_mode=cart_permalink inside the mint — an
    ORDER-side join closable by T2-2 with zero merchant setup. Wix cards join
    via ``online_store_url`` captured by the Wix sync (referral_only join).
    Connected WooCommerce cards join via ``online_store_url`` (real permalink),
    else the WooCommerce default product base + ``handle`` (referral_only).
    Cards with no derivable destination are skipped (honest degradation).

    Trust basis: the destination is the merchant's OWN connected store, so the
    mint's domain allowlist is exactly that destination's host (the store
    connection IS the policy) — market allowlists govern external seeds, not
    connected merchants. Fail-soft: any error leaves cards unchanged; one
    domain lookup per merchant and one mint per (dest, variant) per request.
    """
    try:
        if not isinstance(products, list):
            return
        candidates = [
            p
            for p in products
            if isinstance(p, dict)
            and str(p.get("merchant_id") or "").strip()
            and not p.get("external_redirect_url")
            and str(p.get("source") or "") != "external_seed"
        ]
        if not candidates:
            return
        used_market = str(market or "US").strip().upper() or "US"
        used_tool = str(tool or "*").strip() or "*"

        from services.merchant_store_service import get_merchant_active_stores

        # Per-merchant connected-store domain keyed by PLATFORM, so a card's
        # destination can be derived for its own platform (a merchant may have
        # e.g. both a Shopify and a Woo store). Was Shopify-only.
        store_domains: Dict[str, Dict[str, str]] = {}
        for merchant_id in {str(p["merchant_id"]).strip() for p in candidates}:
            try:
                stores = await get_merchant_active_stores(merchant_id)
            except Exception:
                stores = []
            by_platform: Dict[str, str] = {}
            for s in stores or []:
                plat = str(s.get("platform") or "").strip().lower()
                dom = str(s.get("domain") or "").strip()
                if plat and dom and plat not in by_platform:
                    by_platform[plat] = dom
            if by_platform:
                store_domains[merchant_id] = by_platform

        mint_cache: Dict[str, Optional[str]] = {}
        for p in candidates:
            merchant_id = str(p.get("merchant_id") or "").strip()
            platform = str(p.get("platform") or "").strip().lower()
            merchant_store_domain = (store_domains.get(merchant_id) or {}).get(platform)
            # shop_domain feeds the Shopify cart-permalink mint; only meaningful
            # for Shopify (its absence → referral_only for other platforms).
            shop_domain = merchant_store_domain if platform == "shopify" else None
            dest = str(p.get("online_store_url") or "").strip()
            handle = str(p.get("handle") or "").strip()
            if not dest and platform == "shopify" and merchant_store_domain and handle:
                dest = f"https://{normalize_shop_host(merchant_store_domain)}/products/{handle}"
            elif not dest and platform == "woocommerce" and merchant_store_domain and handle:
                # A-F1.3: connected-Woo fallback. The real permalink
                # (online_store_url captured at sync) wins; this only fires when
                # it's absent, using WooCommerce's DEFAULT product base
                # (/product/<slug>). Not fabricated beyond that convention —
                # non-default-permalink stores carry online_store_url and take
                # the branch above. No handle → skip (never guess).
                #
                # DEPENDENCY (dead today: woo=0 connected, no Woo product sync
                # exists — adapters/woocommerce_adapter.py is a connection stub):
                # when a Woo product sync is built it MUST populate
                # online_store_url for CUSTOM-permalink stores, else this default
                # /product/<slug> fabricates a 404 for them (a dead link is worse
                # than no link). Keep in lockstep with that sync.
                dest = f"https://{normalize_shop_host(merchant_store_domain)}/product/{handle}"
            if not dest.startswith(("http://", "https://")):
                continue  # no derivable merchant destination — never fabricate one

            variant_id: Optional[str] = None
            for v in p.get("variants") or []:
                if isinstance(v, dict) and str(v.get("variant_id") or v.get("id") or "").strip():
                    variant_id = str(v.get("variant_id") or v.get("id")).strip()
                    break

            cache_key = "||".join([used_market, used_tool, dest, variant_id or ""])
            if cache_key in mint_cache:
                redirect_url = mint_cache[cache_key]
            else:
                redirect_url = await _make_external_redirect_url(
                    market=used_market,
                    tool=used_tool,
                    destination_url=dest,
                    utm_template=None,
                    ctx={"source": "connected_catalog"},
                    # The merchant's own connected store is the trust basis. Allow both the
                    # destination host and the connected shop domain — online_store_url may
                    # live on a custom domain while the connection stores the .myshopify one.
                    allowed_domains=[h for h in {normalize_shop_host(dest), normalize_shop_host(merchant_store_domain or ""), normalize_shop_host(shop_domain or "")} if h],
                    merchant_id=merchant_id,
                    product_id=str(p.get("product_id") or p.get("id") or "").strip() or None,
                    variant_id=variant_id,
                    # Provenance, not shape: this id comes from the merchant's OWN connected
                    # catalog sync (p["variants"][].variant_id), so for a shopify-platform
                    # store it is the Shopify-issued variant id. Passed explicitly because the
                    # builder has no fallback — a caller that cannot justify its id passes None.
                    cart_variant_id=variant_id,
                    shop_domain=shop_domain,
                    platform=platform or None,
                )
                mint_cache[cache_key] = redirect_url
            if redirect_url:
                p["external_redirect_url"] = redirect_url
    except Exception as e:  # never let attribution stamping break search
        logger.warning("connected-product redirect stamping failed: %s", str(e)[:160])


def _record_gateway_decision_events(
    result: Any,
    *,
    surface: str,
    query: Optional[str] = None,
    merchant_id: Optional[str] = None,
    source: Optional[str] = None,
    extra_context: Optional[Dict[str, Any]] = None,
) -> None:
    """Phase 0 (convergence plan): deposit the serving slate into the
    decision-layer event store from the mainline gateway — previously the
    highest-traffic lane emitted NO decision events, so the ledger had no
    behavioral baseline for this surface.

    Mirrors the agent_v2/agent_sdk_fixed writer pattern: stamp a decision_id
    into result metadata (so order creation can link the funnel), then
    fire-and-forget the event-store enqueue. Fail-soft everywhere — recording
    can never break search. The ``protocol`` dimension is derived from the
    request source label (mcp/acp/ucp) instead of hardcoding pdp_direct."""
    try:
        import uuid as _uuid

        if not isinstance(result, dict):
            return
        # Idempotence: retry/fallback branches re-enter the wrapper with the
        # SAME result object — one returned slate must yield exactly one
        # decision event, so a result already stamped is never re-recorded.
        existing_meta = result.get("metadata")
        if isinstance(existing_meta, dict) and isinstance(existing_meta.get("decision_layer"), dict):
            return
        products = [p for p in (result.get("products") or []) if isinstance(p, dict)]

        decision_id = str(_uuid.uuid4())
        metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        metadata = dict(metadata or {})
        metadata["decision_id"] = decision_id
        metadata["decision_layer"] = {
            "decision_id": decision_id,
            "correlation_source": surface,
        }
        result["metadata"] = metadata

        from services.agent_decision_event_store import (
            record_decision_candidates,
            record_decision_event,
            record_exposure_events,
        )
        from services.protocols import derive_protocol_for_surface

        # NOTE (honest scope): on the gateway SHOPPING lane the source label is
        # shopping-*/aurora-*/creator-* (not a protocol session), so this
        # resolves DEFAULT_PROTOCOL for ~all mainline traffic — which is
        # correct (direct-PDP serving). The dimension only becomes non-default
        # when a genuine mcp/acp/ucp surface drives the call (e.g. agent_v2's
        # request channel). It replaces the previous unconditional hardcode.
        protocol = derive_protocol_for_surface(source)
        rows = []
        for idx, product in enumerate(products):
            rows.append(
                {
                    # external-seed cards carry catalog identity under
                    # attached_product_key; connected/pivot mainline cards
                    # don't emit a top-level content_key yet (Phase-2 card
                    # mapping) — captured here best-effort, NULL otherwise.
                    "content_key": (
                        product.get("content_key")
                        or product.get("product_key")
                        or product.get("attached_product_key")
                    ),
                    "catalog_offer_id": product.get("catalog_offer_id") or product.get("offer_id"),
                    "position": idx,
                    "eligibility_flags": {
                        "merchant_id": product.get("merchant_id"),
                        "platform": product.get("platform"),
                        "in_stock": product.get("in_stock"),
                        "source": product.get("source"),
                        "has_external_redirect": bool(product.get("external_redirect_url")),
                    },
                    "slot": "search_result",
                }
            )

        async def _record() -> None:
            try:
                await record_decision_event(
                    decision_id=decision_id,
                    merchant_id=merchant_id,
                    surface=surface,
                    channel=source,
                    protocol=protocol,
                    agent_context={
                        "query": query,
                        "source": source,
                        "result_count": len(products),
                        **(extra_context or {}),
                    },
                )
                await record_decision_candidates(decision_id, rows)
                await record_exposure_events(decision_id, rows)
            except Exception:
                logger.debug("gateway decision event enqueue failed", exc_info=True)

        asyncio.create_task(_record())
    except Exception:  # never let ledger recording break search
        logger.debug("gateway decision event scheduling failed", exc_info=True)


def _pivot_primary_offer(item: PivotResultItem) -> Optional[Any]:
    if not item.offers:
        return None
    for offer in item.offers:
        if offer.catalog_track == "internal_merchant":
            return offer
    return item.offers[0]


def _pivot_price_value(offer: Any) -> Optional[Any]:
    if offer is None:
        return None
    pricing = getattr(offer, "pricing", None)
    if pricing is None:
        return None
    return (
        pricing.estimated_best_price
        or pricing.merchant_effective_price
        or pricing.list_price
    )


def _pivot_market_from_payload(
    payload: FindProductsMultiPayload,
    request_metadata: Optional[Dict[str, Any]],
) -> str:
    raw_locale = ""
    if isinstance(request_metadata, dict):
        raw_locale = str(
            request_metadata.get("locale")
            or request_metadata.get("market")
            or ""
        ).strip()
    if not raw_locale:
        try:
            raw_locale = str(getattr(payload, "locale", "") or "").strip()
        except Exception:
            raw_locale = ""
    if not raw_locale:
        return "US"
    token = raw_locale.replace("_", "-").split("-")[-1].strip().upper()
    return token if len(token) == 2 else "US"


def _pivot_multi_source_allowed(source_normalized: str, allowed_sources: set[str]) -> bool:
    if not allowed_sources:
        return False
    source_normalized = _normalize_surface_source(source_normalized)
    if not source_normalized:
        return False
    normalized_allowed_sources = {
        _normalize_surface_source(item)
        for item in allowed_sources
        if _normalize_surface_source(item)
    }
    if source_normalized in normalized_allowed_sources:
        return True
    if _is_shopping_multi_source(source_normalized):
        for allowed in normalized_allowed_sources:
            if allowed.endswith("*") and source_normalized.startswith(allowed[:-1]):
                return True
    return False


def _pivot_multi_rollout_allowed(
    *,
    source_normalized: str,
    page: int,
    mode: str,
) -> bool:
    normalized_mode = str(mode or "").strip().lower()
    if normalized_mode == "shadow":
        return _pivot_multi_source_allowed(
            source_normalized,
            PIVOT_MULTI_SHADOW_SOURCE_ALLOWLIST,
        )
    if normalized_mode == "serve":
        if page > PIVOT_MULTI_SERVE_MAX_PAGE:
            return False
        return _pivot_multi_source_allowed(
            source_normalized,
            PIVOT_MULTI_SERVE_SOURCE_ALLOWLIST,
        )
    return False


def _pivot_items_to_multi_products(items: List[PivotResultItem]) -> List[Dict[str, Any]]:
    grouped: Dict[str, Dict[str, Any]] = {}

    for item in items:
        primary_offer = _pivot_primary_offer(item)
        product_id = (
            item.product.source_product_id
            or item.product.product_key
            or (primary_offer.offer_id if primary_offer else None)
            or item.sku.sku_key
        )
        if not product_id:
            continue

        group = grouped.get(product_id)
        if group is None:
            image_url = item.product.image_url
            if not image_url and primary_offer:
                image_url = None
            best_deal_payload: Optional[Dict[str, Any]] = None
            if primary_offer:
                merchant_price = getattr(primary_offer.pricing, "merchant_effective_price", None)
                estimated_price = getattr(primary_offer.pricing, "estimated_best_price", None)
                payment_offer_evidence = getattr(primary_offer, "payment_offer_evidence", {}) or {}
                payment_offers = (
                    payment_offer_evidence.get("offers")
                    if isinstance(payment_offer_evidence, dict)
                    else []
                ) or []
                if payment_offers:
                    best_deal_payload = {
                        "estimated_best_price": (
                            payment_offers[0].get("estimated_total_after_payment_offer")
                            if isinstance(payment_offers[0], dict)
                            else estimated_price
                        ),
                        "payment_offer_evidence": payment_offer_evidence,
                        "source": "payment_offer_evidence",
                    }
                elif primary_offer.incentives or (
                    estimated_price is not None
                    and merchant_price is not None
                    and estimated_price < merchant_price
                ):
                    best_deal_payload = {
                        "estimated_best_price": estimated_price,
                        "incentives": [incentive.model_dump() for incentive in primary_offer.incentives],
                        "source": "pivot_incentive_graph",
                    }
            group = {
                "id": product_id,
                "product_id": product_id,
                "merchant_id": item.merchant.merchant_id,
                "merchant_name": item.merchant.merchant_name,
                "title": item.product.title,
                "description": item.product.description or "",
                "price": _pivot_price_value(primary_offer),
                "currency": getattr(primary_offer.pricing, "currency", None) if primary_offer else None,
                "image_url": image_url,
                "product_type": item.product.product_type,
                "inventory_quantity": 0,
                "sku": item.sku.sku,
                "platform": item.merchant.primary_platform,
                "catalog_track": item.catalog_track,
                "truth_tier": item.truth_tier,
                "readiness_tier": item.readiness_tier,
                "canonical_url": item.product.canonical_url,
                "visible_attributes": item.sku.visible_attributes or {},
                "visible_option_labels": list(item.sku.visible_option_labels or []),
                "ingredient_ids": list(item.sku.ingredient_ids or []),
                "variants": [],
            }
            if best_deal_payload:
                group["best_deal"] = best_deal_payload
            if primary_offer:
                group["payment_offer_evidence"] = getattr(primary_offer, "payment_offer_evidence", {}) or {}
                group["savings_presentation"] = getattr(primary_offer, "savings_presentation", {}) or {}
            if item.verticals.get("beauty"):
                group["beauty"] = item.verticals.get("beauty")
            grouped[product_id] = group

        availability = getattr(primary_offer, "availability", None) if primary_offer else None
        inventory_quantity = getattr(primary_offer, "inventory_quantity", None) if primary_offer else None
        if isinstance(inventory_quantity, int) and inventory_quantity > 0:
            group["inventory_quantity"] = int(group.get("inventory_quantity") or 0) + inventory_quantity

        variant_id = item.sku.source_variant_id or item.sku.sku_key or item.sku.sku or product_id
        variant = {
            "variant_id": variant_id,
            "id": variant_id,
            "title": item.sku.title or item.product.title,
            "price": _pivot_price_value(primary_offer),
            "compare_at_price": getattr(primary_offer.pricing, "list_price", None) if primary_offer else None,
            "sku": item.sku.sku,
            "inventory_quantity": inventory_quantity,
            "options": {
                "visible_attributes": item.sku.visible_attributes or {},
                "visible_option_labels": item.sku.visible_option_labels or [],
            },
            "image_url": item.product.image_url,
            "availability": availability,
            "payment_offer_evidence": getattr(primary_offer, "payment_offer_evidence", {}) if primary_offer else {},
            "savings_presentation": getattr(primary_offer, "savings_presentation", {}) if primary_offer else {},
        }

        existing_variant_ids = {
            str(v.get("variant_id") or v.get("id") or "")
            for v in group["variants"]
            if isinstance(v, dict)
        }
        if str(variant_id) not in existing_variant_ids:
            group["variants"].append(variant)

    return list(grouped.values())


def _normalize_pivot_multi_visible_attributes(product: Any) -> Dict[str, List[str]]:
    if not isinstance(product, dict):
        return {}
    raw = product.get("visible_attributes") or {}
    if not isinstance(raw, dict):
        return {}
    normalized: Dict[str, List[str]] = {}
    for bucket, values in raw.items():
        bucket_name = str(bucket or "").strip()
        if not bucket_name:
            continue
        items = values if isinstance(values, list) else [values] if isinstance(values, str) else []
        deduped: List[str] = []
        for value in items:
            label = str(value or "").strip()
            if label and label not in deduped:
                deduped.append(label)
        if deduped:
            normalized[bucket_name] = deduped
    return normalized


def _normalize_pivot_multi_ingredient_ids(product: Any) -> List[str]:
    if not isinstance(product, dict):
        return []
    ingredient_ids = product.get("ingredient_ids") or []
    if isinstance(ingredient_ids, str):
        ingredient_ids = [ingredient_ids]
    deduped: List[str] = []
    for value in ingredient_ids if isinstance(ingredient_ids, list) else []:
        normalized = _normalize_serving_token(str(value or "").replace("_", " "))
        if not normalized:
            continue
        canonical = _SKINCARE_INGREDIENT_CANONICAL_ALIASES.get(
            normalized.replace("_", " "),
            normalized,
        )
        if canonical not in deduped:
            deduped.append(canonical)
    return deduped


def _collect_pivot_multi_visible_option_labels(product: Any) -> List[str]:
    if not isinstance(product, dict):
        return []
    deduped: List[str] = []
    for label in product.get("visible_option_labels") or []:
        normalized = _normalize_serving_token(label)
        if normalized and normalized not in deduped:
            deduped.append(normalized)
    for variant in product.get("variants") or []:
        if not isinstance(variant, dict):
            continue
        for label in (variant.get("options") or {}).get("visible_option_labels", []) or []:
            normalized = _normalize_serving_token(label)
            if normalized and normalized not in deduped:
                deduped.append(normalized)
    return deduped


def _pivot_multi_search_text_blob(product: Any) -> str:
    if not isinstance(product, dict):
        return ""
    parts: List[str] = [
        str(product.get("title") or ""),
        str(product.get("description") or ""),
        str(product.get("product_type") or ""),
        str(product.get("sku") or ""),
    ]
    visible_attributes = product.get("visible_attributes")
    if isinstance(visible_attributes, dict):
        for values in visible_attributes.values():
            if isinstance(values, list):
                parts.extend(str(value or "") for value in values)
            elif isinstance(values, str):
                parts.append(values)
    ingredient_ids = product.get("ingredient_ids")
    if isinstance(ingredient_ids, list):
        parts.extend(str(value or "") for value in ingredient_ids)
    elif isinstance(ingredient_ids, str):
        parts.append(ingredient_ids)
    for variant in product.get("variants") or []:
        if not isinstance(variant, dict):
            continue
        parts.append(str(variant.get("title") or ""))
        parts.append(str(variant.get("sku") or ""))
        options = variant.get("options") or {}
        if isinstance(options, dict):
            for option_name, option_value in options.items():
                parts.append(str(option_name or ""))
                if isinstance(option_value, list):
                    parts.extend(str(value or "") for value in option_value)
                else:
                    parts.append(str(option_value or ""))
    return " ".join(part.lower() for part in parts if str(part or "").strip()).strip()


def _ingredient_alias_matches_text(text: Optional[str], ingredient_id: Optional[str]) -> bool:
    return any(
        _normalized_intent_term_match(text, alias)
        for alias in _skincare_ingredient_alias_terms(ingredient_id)
    )


def _pivot_multi_product_signatures(product: Any) -> set[str]:
    if not isinstance(product, dict):
        return set()
    merchant_id = str(product.get("merchant_id") or "").strip().lower()
    product_id = str(product.get("product_id") or product.get("id") or "").strip().lower()
    canonical_url = str(product.get("canonical_url") or "").strip().lower()
    title = _normalize_offer_title(str(product.get("title") or ""))
    signatures: set[str] = set()
    if merchant_id and product_id:
        signatures.add(f"{merchant_id}::{product_id}")
    if canonical_url:
        signatures.add(f"url::{canonical_url}")
    if title:
        signatures.add(f"title::{title}")
        if merchant_id:
            signatures.add(f"{merchant_id}::title::{title}")
    return signatures


def _pivot_multi_group_counts(result: Optional[Dict[str, Any]]) -> tuple[int, int]:
    products = result.get("products") if isinstance(result, dict) else None
    if not isinstance(products, list):
        return 0, 0
    internal_count = 0
    external_count = 0
    for product in products:
        if not isinstance(product, dict):
            continue
        track = str(product.get("catalog_track") or "").strip().lower()
        if track == "internal_merchant":
            internal_count += 1
        elif track == "external_referral":
            external_count += 1
    return internal_count, external_count


def _pivot_multi_share(count: int, total: int) -> float:
    if total <= 0:
        return 0.0
    try:
        return max(0.0, min(1.0, float(count) / float(total)))
    except Exception:
        return 0.0


def _pivot_multi_page_bucket(page: int) -> str:
    try:
        normalized_page = max(1, int(page))
    except Exception:
        normalized_page = 1
    if normalized_page <= 1:
        return "page_1"
    if normalized_page <= 3:
        return "page_2_3"
    return "page_4_plus"


def _pivot_multi_product_best_price(product: Any) -> Optional[float]:
    if not isinstance(product, dict):
        return None

    candidates: List[Any] = []
    best_deal = product.get("best_deal")
    if isinstance(best_deal, dict):
        candidates.append(best_deal.get("estimated_best_price"))
        candidates.append(best_deal.get("merchant_effective_price"))
    candidates.append(product.get("price"))

    for variant in product.get("variants") or []:
        if not isinstance(variant, dict):
            continue
        candidates.append(variant.get("price"))
        candidates.append(variant.get("compare_at_price"))

    for candidate in candidates:
        try:
            if candidate is None or candidate == "":
                continue
            return float(candidate)
        except Exception:
            continue
    return None


def _build_pivot_multi_shadow_diff_summary(
    served_result: Optional[Dict[str, Any]],
    pivot_result: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    served_products = served_result.get("products") if isinstance(served_result, dict) else None
    pivot_products = pivot_result.get("products") if isinstance(pivot_result, dict) else None
    served_products = served_products if isinstance(served_products, list) else []
    pivot_products = pivot_products if isinstance(pivot_products, list) else []

    served_signatures = [
        signatures
        for signatures in (_pivot_multi_product_signatures(product) for product in served_products)
        if signatures
    ]
    pivot_signatures = [
        signatures
        for signatures in (_pivot_multi_product_signatures(product) for product in pivot_products)
        if signatures
    ]
    matched_pivot_indexes: set[int] = set()
    overlap_count = 0
    for served_signature_set in served_signatures:
        for pivot_idx, pivot_signature_set in enumerate(pivot_signatures):
            if pivot_idx in matched_pivot_indexes:
                continue
            if served_signature_set.intersection(pivot_signature_set):
                matched_pivot_indexes.add(pivot_idx)
                overlap_count += 1
                break
    if not served_signatures and not pivot_signatures:
        overlap_ratio = 1.0
        top1_same = True
    else:
        overlap_ratio = (
            round(overlap_count / max(1, len(served_signatures)), 4)
            if served_signatures
            else 0.0
        )
        top1_same = bool(
            served_signatures
            and pivot_signatures
            and served_signatures[0].intersection(pivot_signatures[0])
        )
    served_internal_count, served_external_count = _pivot_multi_group_counts(served_result)
    pivot_internal_count, pivot_external_count = _pivot_multi_group_counts(pivot_result)

    served_metadata = served_result.get("metadata") if isinstance(served_result, dict) else None
    served_metadata = served_metadata if isinstance(served_metadata, dict) else {}
    pivot_metadata = pivot_result.get("metadata") if isinstance(pivot_result, dict) else None
    pivot_metadata = pivot_metadata if isinstance(pivot_metadata, dict) else {}

    served_top_price = _pivot_multi_product_best_price(served_products[0]) if served_products else None
    pivot_top_price = _pivot_multi_product_best_price(pivot_products[0]) if pivot_products else None
    estimated_price_delta_ratio: Optional[float] = None
    if served_top_price and served_top_price > 0 and pivot_top_price is not None:
        estimated_price_delta_ratio = round((pivot_top_price - served_top_price) / served_top_price, 4)

    served_returned_count = len(served_products)
    pivot_returned_count = len(pivot_products)
    no_result_mismatch = bool((served_returned_count == 0) != (pivot_returned_count == 0))
    internal_share_delta = round(
        _pivot_multi_share(pivot_internal_count, pivot_returned_count)
        - _pivot_multi_share(served_internal_count, served_returned_count),
        4,
    )
    external_share_delta = round(
        _pivot_multi_share(pivot_external_count, pivot_returned_count)
        - _pivot_multi_share(served_external_count, served_returned_count),
        4,
    )
    returned_count_delta = pivot_returned_count - served_returned_count
    bad_price_anomaly = bool(
        estimated_price_delta_ratio is not None
        and abs(estimated_price_delta_ratio) >= PIVOT_MULTI_BAD_PRICE_DELTA_RATIO_THRESHOLD
    )

    return {
        "pivot_shadow_attempted": True,
        "served_query_source": str(served_metadata.get("query_source") or "unknown"),
        "served_returned_count": served_returned_count,
        "pivot_shadow_returned_count": pivot_returned_count,
        "pivot_shadow_overlap_count": overlap_count,
        "pivot_shadow_overlap_ratio": overlap_ratio,
        "pivot_shadow_top1_same": top1_same,
        "served_internal_count": served_internal_count,
        "served_external_count": served_external_count,
        "pivot_shadow_internal_count": pivot_internal_count,
        "pivot_shadow_external_count": pivot_external_count,
        "pivot_shadow_returned_count_delta": returned_count_delta,
        "pivot_shadow_internal_share_delta": internal_share_delta,
        "pivot_shadow_external_share_delta": external_share_delta,
        "pivot_shadow_no_result_mismatch": no_result_mismatch,
        "pivot_shadow_served_top_price": served_top_price,
        "pivot_shadow_top_price": pivot_top_price,
        "pivot_shadow_estimated_price_delta_ratio": estimated_price_delta_ratio,
        "pivot_shadow_bad_price_anomaly": bad_price_anomaly,
        "pivot_shadow_query_source": str(
            pivot_metadata.get("query_source") or "pivot_semantic_core_multi"
        ),
    }


async def _handle_find_products_multi_via_pivot(
    payload: FindProductsMultiPayload,
    request_metadata: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    filters = payload.search
    query = str(filters.query or "").strip()
    if not query:
        return None

    page = filters.page or 1
    limit = _clamp_search_limit(filters.limit, fallback=20)
    raw_limit = min(max(limit * page * PIVOT_MULTI_LIMIT_MULTIPLIER, limit), 100)

    # ADR-007 SLICE 3: resolve the commerce-intent signal here (same derivation as
    # _handle_find_products_multi / line ~6870) so the OFFER-FREE citable lane in
    # search_pivot_catalog can be SUPPRESSED for strict/commerce-explicit shopping.
    _commerce_surface, commerce_surface_explicit = _resolve_commerce_surface(
        payload_surface=filters.commerce_surface,
        request_metadata=request_metadata,
    )
    strict_serving_mode = bool(commerce_surface_explicit)

    q_ascii = _strip_accents(query.lower())
    query_semantic_class = _classify_query_semantic_class(q_ascii or query.lower())
    active_visible_category_intents = []
    for group in [
        {
            "label": "serum",
            "query_terms": ["serum", "serums"],
            "product_terms": ["serum", "serums"],
            "semantic_classes": ["beauty"],
            "visible_attribute_bucket": "product_category",
        },
        {
            "label": "moisturizer",
            "query_terms": ["moisturizer", "moisturizers", "moisturiser", "moisturisers"],
            "product_terms": ["moisturizer", "moisturizers", "moisturiser", "moisturisers"],
            "semantic_classes": ["beauty"],
            "visible_attribute_bucket": "product_category",
        },
        {
            "label": "cleanser",
            "query_terms": ["cleanser", "cleansers"],
            "product_terms": ["cleanser", "cleansers"],
            "semantic_classes": ["beauty"],
            "visible_attribute_bucket": "product_category",
        },
        {
            "label": "toner",
            "query_terms": ["toner", "toners"],
            "product_terms": ["toner", "toners"],
            "semantic_classes": ["beauty"],
            "visible_attribute_bucket": "product_category",
        },
        {
            "label": "foundation",
            "query_terms": ["foundation", "foundations"],
            "product_terms": ["foundation", "foundations"],
            "semantic_classes": ["beauty"],
        },
        {
            "label": "lipstick",
            "query_terms": ["lipstick", "lipsticks"],
            "product_terms": ["lipstick", "lipsticks"],
            "semantic_classes": ["beauty"],
        },
        {
            "label": "blush",
            "query_terms": ["blush", "blushes"],
            "product_terms": ["blush", "blushes"],
            "semantic_classes": ["beauty"],
        },
        {
            "label": "gloss",
            "query_terms": ["gloss", "glosses", "lip gloss", "lip glosses"],
            "product_terms": ["gloss", "glosses", "lip gloss", "lip glosses"],
            "semantic_classes": ["beauty"],
        },
    ]:
        if not _normalized_intent_terms_match(q_ascii, list(group["query_terms"])):
            continue
        allowed_semantic_classes = {
            str(item)
            for item in (group.get("semantic_classes") or [])
            if item
        }
        if allowed_semantic_classes and query_semantic_class not in allowed_semantic_classes:
            continue
        active_visible_category_intents.append(group)
    active_visible_category_labels = [
        str(group["label"]) for group in active_visible_category_intents
    ]
    active_visible_attribute_intents = []
    for group in [
        {
            "label": "fragrance_free",
            "query_terms": [
                "fragrance free",
                "fragrance-free",
                "free fragrance",
                "sin fragancia",
            ],
            "product_terms": [
                "fragrance free",
                "fragrance-free",
                "free fragrance",
                "without fragrance",
                "no fragrance",
                "sin fragancia",
            ],
            "semantic_classes": ["beauty"],
            "category_labels": ["serum"],
            "visible_attribute_bucket": "formula_constraint",
        },
        {
            "label": "sensitive_skin",
            "query_terms": ["sensitive skin", "sensitive-skin"],
            "product_terms": ["sensitive skin", "sensitive-skin"],
            "semantic_classes": ["beauty"],
            "category_labels": ["serum"],
            "visible_attribute_bucket": "skin_concern",
        },
        {
            "label": "hydrating",
            "query_terms": ["hydrating", "hydrate", "hydration"],
            "product_terms": ["hydrating", "hydrate", "hydration"],
            "semantic_classes": ["beauty"],
            "category_labels": ["serum"],
            "visible_attribute_bucket": "skin_concern",
        },
        {
            "label": "brightening",
            "query_terms": ["brightening", "brighten"],
            "product_terms": ["brightening", "brighten"],
            "semantic_classes": ["beauty"],
            "category_labels": ["serum"],
            "visible_attribute_bucket": "skin_concern",
        },
    ]:
        if not _normalized_intent_terms_match(q_ascii, list(group["query_terms"])):
            continue
        allowed_semantic_classes = {
            str(item)
            for item in (group.get("semantic_classes") or [])
            if item
        }
        if allowed_semantic_classes and query_semantic_class not in allowed_semantic_classes:
            continue
        allowed_category_labels = {
            str(item)
            for item in (group.get("category_labels") or [])
            if item
        }
        if allowed_category_labels and not any(
            label in allowed_category_labels for label in active_visible_category_labels
        ):
            continue
        active_visible_attribute_intents.append(group)
    active_visible_option_intents = [
        *_extract_visible_size_option_intents(q_ascii),
        *_extract_visible_color_option_intents(
            q_ascii,
            active_category_labels=active_visible_category_labels,
        ),
        *_extract_visible_shade_option_intents(
            q_ascii,
            active_category_labels=active_visible_category_labels,
        ),
    ]
    active_visible_option_labels = [
        str(group["label"]) for group in active_visible_option_intents
    ]
    cosmetic_shade_category_intents = [
        label
        for label in active_visible_category_labels
        if label in _COSMETIC_SHADE_CATEGORY_LABELS
    ]
    requires_explicit_shade_query = bool(cosmetic_shade_category_intents)
    has_active_shade_option_intent = any(
        label.startswith("shade_") for label in active_visible_option_labels
    )
    active_unsupported_beauty_category_labels = [
        str(group["label"])
        for group in [
            {"label": "skincare", "query_terms": ["skincare", "skin care", "skin-care"]},
            {"label": "cosmetics", "query_terms": ["cosmetics", "makeup", "make-up"]},
        ]
        if _normalized_intent_terms_match(q_ascii, list(group["query_terms"]))
    ]
    active_ingredient_intents = _extract_skin_care_ingredient_intents(
        q_ascii,
        query_semantic_class=query_semantic_class,
    )

    pivot_result = await search_pivot_catalog(
        PivotQueryRequest(
            query=query,
            merchant_id=None,
            market=_pivot_market_from_payload(payload, request_metadata),
            limit=raw_limit,
            include_external=PIVOT_MULTI_SERVE_INCLUDE_EXTERNAL,
            include_incentives=PIVOT_MULTI_SERVE_INCLUDE_INCENTIVES,
            payment_context=payload.payment_context,
            # ADR-007 SLICE 3: suppress the citable lane for shopping intent.
            strict_serving_mode=strict_serving_mode,
        )
    )

    products = _pivot_items_to_multi_products(pivot_result.items)

    if products and (
        active_visible_category_intents
        or active_visible_attribute_intents
        or active_visible_option_intents
        or active_ingredient_intents
    ):
        filtered_products = []
        for product in products:
            visible_attributes = _normalize_pivot_multi_visible_attributes(product)
            visible_option_labels = _collect_pivot_multi_visible_option_labels(product)
            ingredient_ids = _normalize_pivot_multi_ingredient_ids(product)
            visible_text_blob = _pivot_multi_search_text_blob(product)
            visible_category_blob = visible_text_blob
            visible_attribute_blob = visible_text_blob

            matched_visible_category_labels = []
            for group in active_visible_category_intents:
                label = str(group["label"])
                visible_attribute_bucket = str(
                    group.get("visible_attribute_bucket") or ""
                ).strip()
                matched = False
                if visible_attribute_bucket:
                    matched = _product_visible_attribute_label_matches(
                        visible_attributes,
                        bucket=visible_attribute_bucket,
                        label=label,
                    )
                if not matched:
                    matched = _normalized_intent_terms_match(
                        visible_category_blob,
                        list(group["product_terms"]),
                    )
                if matched:
                    matched_visible_category_labels.append(label)
            if active_visible_category_intents and not matched_visible_category_labels:
                continue

            matched_visible_attribute_labels = []
            for group in active_visible_attribute_intents:
                label = str(group["label"])
                visible_attribute_bucket = str(
                    group.get("visible_attribute_bucket") or ""
                ).strip()
                matched = False
                if visible_attribute_bucket:
                    matched = _product_visible_attribute_label_matches(
                        visible_attributes,
                        bucket=visible_attribute_bucket,
                        label=label,
                    )
                if not matched:
                    matched = _normalized_intent_terms_match(
                        visible_attribute_blob,
                        list(group["product_terms"]),
                    )
                if matched:
                    matched_visible_attribute_labels.append(label)
            if active_visible_attribute_intents and (
                len(matched_visible_attribute_labels)
                < len(active_visible_attribute_intents)
            ):
                continue

            matched_visible_option_labels = []
            for group in active_visible_option_intents:
                label = str(group["label"])
                if label in visible_option_labels:
                    matched_visible_option_labels.append(label)
            if active_visible_option_intents and (
                len(matched_visible_option_labels) < len(active_visible_option_intents)
            ):
                continue

            if active_ingredient_intents:
                product_skin_care_categories = {
                    label
                    for label in visible_attributes.get("product_category", [])
                    if label in _SKINCARE_INGREDIENT_CATEGORY_LABELS
                }
                for label in _SKINCARE_INGREDIENT_CATEGORY_LABELS:
                    if _normalized_intent_term_match(visible_category_blob, label):
                        product_skin_care_categories.add(label)
                if not product_skin_care_categories:
                    continue
                matched_ingredient_ids = []
                for group in active_ingredient_intents:
                    ingredient_id = str(group.get("ingredient_id") or "").strip()
                    if ingredient_id and (
                        ingredient_id in ingredient_ids
                        or _ingredient_alias_matches_text(visible_text_blob, ingredient_id)
                    ):
                        matched_ingredient_ids.append(ingredient_id)
                if len(matched_ingredient_ids) < len(active_ingredient_intents):
                    continue

            filtered_products.append(product)
        products = filtered_products

    category = str(filters.category or "").strip().lower()
    if category:
        products = [
            product
            for product in products
            if category in str(product.get("product_type") or "").lower()
        ]

    if filters.price_min is not None:
        products = [
            product
            for product in products
            if product.get("price") is not None and float(product["price"]) >= float(filters.price_min)
        ]
    if filters.price_max is not None:
        products = [
            product
            for product in products
            if product.get("price") is not None and float(product["price"]) <= float(filters.price_max)
        ]
    if bool(filters.in_stock_only):
        products = [
            product
            for product in products
            if int(product.get("inventory_quantity") or 0) > 0
        ]

    total = len(products)
    start_idx = (page - 1) * limit
    end_idx = start_idx + limit
    page_items = products[start_idx:end_idx]
    if not page_items:
        return None

    internal_count = sum(1 for item in pivot_result.items if item.catalog_track == "internal_merchant")
    external_count = sum(1 for item in pivot_result.items if item.catalog_track == "external_referral")
    return {
        "products": page_items,
        "total": total,
        "page": page,
        "page_size": len(page_items),
        "reply": None,
        "metadata": {
            "query_source": "pivot_semantic_core_multi",
            "query_semantic_class": query_semantic_class,
            "fetched_at": datetime.utcnow().isoformat(),
            "pivot_rollout_mode": "serve",
            "pivot_rollout_guard_passed": True,
            "pivot_include_external": PIVOT_MULTI_SERVE_INCLUDE_EXTERNAL,
            "pivot_include_incentives": PIVOT_MULTI_SERVE_INCLUDE_INCENTIVES,
            "pivot_internal_item_count": internal_count,
            "pivot_external_item_count": external_count,
            "pivot_total_items": int(pivot_result.total or len(pivot_result.items)),
            "primary_path_used": "pivot_semantic_core_multi",
            "fallback_triggered": False,
            "fallback_reason": None,
            "external_seed_executed": external_count > 0,
            "external_seed_rows_built": external_count,
            "internal_raw_count": internal_count,
            "external_raw_count": external_count,
            "merged_pre_limit_count": total,
            "final_returned_count": len(page_items),
        },
    }


async def _shadow_find_products_multi_via_pivot(
    payload: FindProductsMultiPayload,
    request_metadata: Optional[Dict[str, Any]],
    served_result: Optional[Dict[str, Any]] = None,
) -> None:
    try:
        result = await _handle_find_products_multi_via_pivot(payload, request_metadata)
        diff_summary = (
            _build_pivot_multi_shadow_diff_summary(served_result, result)
            if isinstance(served_result, dict)
            else None
        )
        if diff_summary:
            source_normalized = _normalize_surface_source((request_metadata or {}).get("source"))
            query_semantic_class = str(
                ((served_result or {}).get("metadata") or {}).get("query_semantic_class")
                or _classify_query_semantic_class(payload.search.query)
                or "default"
            ).strip().lower() or "default"
            record_catalog_pivot_shadow_compare(
                source=source_normalized or "unknown",
                page_bucket=_pivot_multi_page_bucket(payload.search.page or 1),
                query_semantic_class=query_semantic_class,
                served_path=str(diff_summary.get("served_query_source") or "unknown"),
                shadow_path=str(diff_summary.get("pivot_shadow_query_source") or "pivot_semantic_core_multi"),
                top1_same=bool(diff_summary.get("pivot_shadow_top1_same")),
                overlap_ratio=float(diff_summary.get("pivot_shadow_overlap_ratio") or 0.0),
                returned_count_delta=int(diff_summary.get("pivot_shadow_returned_count_delta") or 0),
                internal_share_delta=float(diff_summary.get("pivot_shadow_internal_share_delta") or 0.0),
                external_share_delta=float(diff_summary.get("pivot_shadow_external_share_delta") or 0.0),
                no_result_mismatch=bool(diff_summary.get("pivot_shadow_no_result_mismatch")),
                estimated_price_delta_ratio=diff_summary.get("pivot_shadow_estimated_price_delta_ratio"),
                bad_price_anomaly=bool(diff_summary.get("pivot_shadow_bad_price_anomaly")),
            )
        logger.info(
            "pivot.multi.shadow.diff" if diff_summary else "pivot.multi.shadow.result",
            extra={
                "query": str(payload.search.query or "").strip(),
                "served": bool(result and result.get("products")),
                "returned_count": len((result or {}).get("products") or []),
                "metadata": (result or {}).get("metadata") or {},
                "diff_summary": diff_summary or {},
            },
        )
    except Exception as exc:  # pragma: no cover - observational path
        logger.info(
            "pivot.multi.shadow.failed",
            extra={
                "query": str(payload.search.query or "").strip(),
                "error": str(exc),
            },
        )


def _maybe_schedule_pivot_multi_shadow_compare(
    *,
    payload: FindProductsMultiPayload,
    request_metadata: Optional[Dict[str, Any]],
    background_tasks: BackgroundTasks,
    served_result: Optional[Dict[str, Any]],
    source_normalized: str,
    page: int,
    dedup_cache_hit: bool = False,
    dedup_inflight_joined: bool = False,
) -> bool:
    if not PIVOT_MULTI_SHADOW_ENABLED:
        return False
    if dedup_cache_hit or dedup_inflight_joined:
        return False
    if not str(payload.search.query or "").strip():
        return False
    if not _pivot_multi_rollout_allowed(
        source_normalized=source_normalized,
        page=page,
        mode="shadow",
    ):
        return False
    if not isinstance(served_result, dict):
        return False
    served_products = served_result.get("products")
    if not isinstance(served_products, list):
        return False
    served_metadata = served_result.get("metadata")
    served_metadata = served_metadata if isinstance(served_metadata, dict) else {}
    if str(served_metadata.get("query_source") or "").strip() == "pivot_semantic_core_multi":
        return False

    background_tasks.add_task(
        _shadow_find_products_multi_via_pivot,
        payload.model_copy(deep=True),
        dict(request_metadata or {}),
        copy.deepcopy(served_result),
    )
    return True


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
        # An attached seed (resolved to a canonical pg/sig) IS the merchant offer for that product. Drop it
        # only when the same product is ALSO being served internally in THIS result — i.e. real dedup against
        # an internal id or a colliding internal offer (same title/price/vendor). Otherwise the seed is the
        # only representation of that offer and must surface. Previously attached seeds were dropped
        # unconditionally, which collapsed legitimate beauty offers to near-zero on the keyless/agent path
        # where no internal offer is served. (#1659 recall)
        external_id = product.get("product_id") or product.get("external_product_id")
        if external_id and str(external_id) in internal_ids:
            continue
        # Soft dedup: only drop when this is a FULL offer-key collision with a served internal offer (all of
        # the seed's offer keys present), i.e. a true internal twin. A partial overlap (e.g. same title/price
        # but no vendor match) keeps the seed, preserving the existing soft-prune contract.
        seed_offer_keys = _build_offer_keys(
            product.get("title") or "",
            product.get("price"),
            product.get("currency") or "USD",
            product.get("vendor") or product.get("brand"),
        )
        if seed_offer_keys and seed_offer_keys.issubset(offer_keys):
            continue
        filtered.append(wrapper)
    return filtered


def _normalize_external_seed_structured_ingredient_ids(
    row: Dict[str, Any],
    seed_data: Dict[str, Any],
) -> List[str]:
    return _shared_normalize_external_seed_structured_ingredient_ids(row, seed_data)


def _normalize_external_seed_product_type(
    row: Dict[str, Any],
    seed_data: Dict[str, Any],
) -> str:
    return _shared_normalize_external_seed_product_type(row, seed_data)


def _build_external_seed_filter_product(
    *,
    row: Dict[str, Any],
    seed_data: Dict[str, Any],
    external_product: Dict[str, Any],
) -> StandardProduct:
    return _shared_build_external_seed_filter_product(
        row=row,
        seed_data=seed_data,
        external_product=external_product,
    )


def _external_seed_redirect_identity(
    *,
    row: Dict[str, Any],
    seed_data: Dict[str, Any],
    offer_variant_id: Optional[str] = None,
) -> Dict[str, Optional[str]]:
    """Derive the attribution identity for an external-seed redirect.

    ``attached_product_key`` is stored double-colon as
    ``prod::{merchant_id}::{platform}::{source_product_id}`` (make_catalog_product_key,
    IDENTITY_REFERENCE §2; the pipe form is a never-persisted transport, Trap T1), so
    an *attached* seed yields the pivota merchant id, the platform, and the canonical
    product id. A standalone seed
    (no attached key) yields Nones for those — an honest Tier-2a referral with no merchant
    join. The shop host + variant id gate whether a Shopify cart permalink can be built.
    """
    row = row or {}
    seed_data = seed_data or {}
    attached_key = str(
        row.get("attached_product_key") or seed_data.get("attached_product_key") or ""
    ).strip()
    merchant_id: Optional[str] = None
    platform: Optional[str] = None
    canonical_product_id: Optional[str] = None
    # attached_product_key STORAGE is the double-colon form
    # prod::{merchant}::{platform}::{source_product_id} (IDENTITY_REFERENCE §2);
    # the pipe form is a never-persisted transport (Trap T1). The old parse only
    # handled pipe, so on every real (double-colon) seed merchant/platform/
    # product stayed None → surface_click_events.merchant_id NULL. Handle both.
    if attached_key.startswith("prod::"):
        parts = attached_key.split("::")
        if len(parts) >= 4:
            merchant_id = parts[1].strip() or None
            platform = parts[2].strip() or None
            canonical_product_id = attached_key
    elif attached_key.count("|") >= 2:
        merchant_part, platform_part, _rest = attached_key.split("|", 2)
        merchant_id = merchant_part.strip() or None
        platform = (platform_part.strip() or None)
        canonical_product_id = attached_key

    attached_variant_id = str(
        row.get("attached_variant_id") or seed_data.get("attached_variant_id") or ""
    ).strip() or None
    variant_id = attached_variant_id or (str(offer_variant_id).strip() if offer_variant_id else None) or None

    shop_domain = str(row.get("domain") or seed_data.get("domain") or "").strip() or None
    if not shop_domain:
        shop_domain = _seed_domain_from_url(
            row.get("destination_url") or seed_data.get("destination_url") or row.get("canonical_url")
        ) or None

    # ADR-009 D3 (docs/adr/ADR-009-seller-of-record-identity.md; IDENTITY_REFERENCE
    # §4): carry the seed's stored seller-of-record (seller_ref) + seed_kind so the
    # T2-1 redirect stamps them into the signed token ctx alongside the ANCHOR
    # merchant_id above. T2-2 closure keys the conversion SUBJECT by seller_ref;
    # the anchor stays a separate surface dimension. Read-only here — the value is
    # DERIVED AT WRITE TIME (services/seller_identity.derive_seed_seller) and stored
    # on the row, so the hot redirect path does no minting. NULL (legacy/pre-A9-4
    # rows) threads through as None and closure stamps seller_ref_missing.
    seller_ref = str(row.get("seller_ref") or seed_data.get("seller_ref") or "").strip() or None
    seed_kind = str(row.get("seed_kind") or seed_data.get("seed_kind") or "").strip() or None

    # STOREFRONT EVIDENCE (services/shopify_variant_identity). The `platform` parsed above
    # is the INTAKE LANE — "external_seed" for the whole crawl cohort — but the consumer of
    # this field, _make_external_redirect_url's is_shopify gate, needs the STOREFRONT's
    # platform, and for that cohort the two diverge on essentially every row (measured:
    # 81/81 reachable PDPs are Shopify on custom domains). A successful `/products/x.js`
    # parse is definitive proof of Shopify-ness, and the backfill stamps that proof into
    # seed_data.snapshot. Two hard rules keep this from becoming the round-3 regression:
    #
    #   * OVERRIDE ONLY THE LANE LABEL. A real attached platform ("wix", "woocommerce")
    #     is writer-verified identity; snapshot evidence never outranks it. Only None or
    #     the lane token itself is eligible.
    #   * NEVER GUESS A VARIANT. The stamped id is used only when the product has exactly
    #     one variant (sole_stamped_variant_id); otherwise the permalink is declined.
    #
    # (Two claims that stood here have been retracted by later rounds and are stated here so
    # nobody re-derives them from a stale comment: "a numeric attached_variant_id always
    # wins" — false, being all-digits is not evidence of being a SHOPIFY id, see :7007; and
    # "the cart and the attribution ctx then name the SAME variant" — false, they are
    # deliberately separate channels, see :6988.)
    #
    # Price, currency, availability and every other serving read are untouched on purpose:
    # this function feeds the REDIRECT, and widening it past identity is exactly what made
    # the previous attempt serve a frozen snapshot price as live.
    # ROUND-4 CORRECTION: `variant_id` is deliberately NOT overridden any more. The first
    # version replaced a SKU-shaped id with the stamped numeric one so cart and attribution
    # would "name the same variant" — but the attribution layer cross-fills BOTH ways
    # (commerce_attribution_service: `product_id = fallback or variant_id` and vice versa),
    # so the numeric id leaked UP A GRAIN into surface_click_events.canonical_product_id,
    # and an attached SKU that joins catalog aliases (canonical_variant_id ~ sku_key) was
    # silently replaced by a value that joins nothing. Attribution therefore keeps its
    # pre-change values byte for byte, and the recovered id travels on `cart_variant_id`,
    # a channel only the cart-permalink construction reads — never the token ctx.
    # THE CART VARIANT ID IS CHOSEN BY PROVENANCE, NEVER BY SHAPE, AND NEVER BY FALLBACK.
    #
    # `variant_id` above is an ATTRIBUTION value: `_seed_offer_variant_id` resolves it from
    # variant_id | variantId | sku | sku_id | id, so it is routinely a SKU string or a
    # synthetic "{epid}-default". Being all-digits does not make such a value a Shopify
    # variant id — that was the round-5 P0, where a numeric SKU built a cart for a product
    # Shopify never had under that id, on multi-variant products where the stamped-id guard
    # had deliberately declined.
    #
    # Round 5 fixed only the evidence-derived branch and left the `or` fallback standing for
    # the rest, which left the identical bug reachable through an ATTACHED shopify product
    # (platform writer-verified, so no evidence flip, and variant_id still from the SKU
    # chain). A conditional fallback is still a fallback. So there is none: this value is set
    # from a source whose provenance justifies it, or it stays None and the redirect degrades
    # to referral_only — which is the correct, honest outcome when we cannot name the variant.
    cart_variant_id: Optional[str] = None
    if platform in (None, "external_seed") and storefront_is_shopify(seed_data):
        platform = "shopify"
        # ROUND-5 CORRECTION. This used to run only `if not extract_shopify_numeric_variant_id(
        # variant_id)`, on the reasoning that a numeric id already in hand needs no second
        # channel. That reasoning was wrong: `variant_id` here comes from
        # `_seed_offer_variant_id` (variant_id | variantId | sku | sku_id | id), and a plain
        # NUMERIC SKU satisfies extract_shopify_numeric_variant_id by design. So a seed whose
        # sku was e.g. "80072940" skipped this branch, and the builder's `cart_variant_id or
        # variant_id` fallback then prefilled a cart from a number Shopify never issued as a
        # variant id — INCLUDING on multi-variant products where sole_stamped_variant_id had
        # deliberately declined. Demonstrated end to end before this fix.
        #
        # When the platform label is EVIDENCE-DERIVED, the stamped id is the only value we
        # have any evidence for, so it is used unconditionally or the permalink is declined.
        # Stamped from the storefront's own /products/x.js — the only Shopify-issued id we
        # have for a crawl seed. None when the product has more than one variant.
        cart_variant_id = sole_stamped_variant_id(seed_data)
    elif platform == "shopify":
        # Writer-verified Shopify attachment. `attached_variant_id` is INTENDED to be catalog
        # identity (catalog_skus.source_variant_id), which for platform='shopify' is the
        # Shopify variant id — but that is a convention, not an enforced one: the attach
        # endpoints (routes/employee_products attach_external_seed, the CSV column) store
        # whatever string an operator posts, with no catalog lookup, and
        # attached_seed_runtime_evidence matches the same column against the SEED's own
        # variant_id/sku. So this is the one branch here whose input is operator-typed.
        # extract_shopify_numeric_variant_id bounds the damage to all-digit values, and the
        # branch is strictly narrower than the pre-round-5 behaviour it replaced — but if a
        # wrong-cart report ever traces back here, this is why. The offer/SKU chain is
        # deliberately NOT consulted.
        cart_variant_id = extract_shopify_numeric_variant_id(attached_variant_id)

    return {
        "merchant_id": merchant_id,
        "platform": platform,
        "product_id": canonical_product_id,
        "variant_id": variant_id,
        "cart_variant_id": cart_variant_id,
        "shop_domain": shop_domain,
        "seller_ref": seller_ref,
        "seed_kind": seed_kind,
    }


def resolve_cart_permalink(
    *,
    destination_url: str,
    shop_domain: Optional[str],
    platform: Optional[str],
    cart_variant_id: Optional[str],
    quantity: int = 1,
) -> Optional[str]:
    """The single decision: can this handoff land in a PRE-FILLED cart, or only on the PDP?

    Extracted so there is exactly ONE implementation. `_make_external_redirect_url` uses it to
    choose the redirect's destination, and `offers.resolve` uses it to tell the agent which of
    the two its `affiliate_url` will do — a fact that was previously computed here, stamped
    into the signed token as `join_mode`, and never returned, so an agent holding the link had
    no way to know whether following it would produce a cart. Two call sites re-deriving that
    from parts is exactly the twin-implementation drift this codebase keeps paying for.

    Returns the cart base URL, or None for an honest referral. Never fabricates: the variant id
    must already be one a caller could justify (see the provenance note in
    _external_seed_redirect_identity), and shopify_cart_base_url refuses anything non-numeric.
    """
    candidate_host = normalize_shop_host(shop_domain) or _seed_domain_from_url(destination_url)
    is_shopify = (
        str(platform or "").strip().lower() == "shopify"
        or candidate_host.endswith(".myshopify.com")
    )
    if not is_shopify:
        return None
    return shopify_cart_base_url(
        shop_domain=shop_domain or destination_url,
        variant_id=cart_variant_id,
        quantity=quantity,
    )


def _redirect_token_from_url(redirect_url: str) -> str:
    """Read the signed token back out of a link _make_external_redirect_url just built.

    The click lane's rollout bucket is a stable hash of this exact string, so recovering it
    (rather than re-minting or guessing) is what makes the resolve-time eligibility check
    agree with the click. The token is base64url with the padding stripped and a `.`
    separator, so it survives the query string byte-identically — no percent-decoding gap
    between what is written here and what `GET /r` receives.
    """
    try:
        query = urlparse(str(redirect_url or "")).query or ""
    except Exception:
        return ""
    for item in query.split("&"):
        if item.startswith("token="):
            return unquote(item[len("token="):])
    return ""


def _cart_prefilled_claim(
    *,
    cart_url: Optional[str],
    destination_url: str,
    redirect_url: str,
) -> Optional[bool]:
    """TRI-STATE: True = prefilled cart, False = bare PDP, None = we cannot promise either.

    `cart_url` is the decision already made by `compose_attributed_destinations` — NOT
    recomputed here. A third derivation of "is there a cart" could disagree with both the
    published `execution_spec.cart_url` and the link `affiliate_url` resolves to.

    `None` exists because a `False` is not merely the absence of a cart — it is a POSITIVE
    claim to a buyer, relayed as "this link lands on a product page, you pick the variant
    yourself", and the warm-handoff click lane (routes/outbound_links.py,
    services/outbound_warm_handoff.py) can land that same buyer in a prefilled cart
    afterwards. The answer has already been sent by then, so it cannot be corrected.
    PIVOTA-Agent #2082 made the gateway field a tri-state for exactly this reason: only an
    explicit backend `False` licenses saying it.

    The exposure is ONE-SIDED, which is why only the `False` leg is guarded: the warm lane can
    only ever BUILD a cart, so it can turn a `False` into a lie but never a `True`. That
    guarantee IS now enforced in this repo (it previously was not, and this paragraph used to
    say so): `evaluate_warm_eligibility` knocks out a dest that is already a cart — on the
    signed `join_mode` OR the dest path shape — and `_validate_continue_url` requires the 302
    target to be a cart/checkout, not merely the right host. See Constraint 5 in the runbook.
    The gateway also derives continue_url from a UCP create_cart and returns nothing else, but
    a change on that side can no longer falsify a `True` here silently.

    Why not predict `True` instead? Because the upgrade depends on a live gateway call at
    click time that can miss (timeout, non-200, off-brand continue_url) and on a user-agent we
    do not have. "Prefilled cart" would be just as unprovable a claim as "bare PDP". The only
    honest answer for a warm-eligible cold offer is "unknown".

    `False` still survives wherever it is provable — flag off, internal key unset, unparseable
    destination host, affiliate destination (never warm-handed), host not allowlisted, or
    token outside the rollout bucket — so this does not blanket the field with nulls; it
    removes it exactly where it would be wrong.

    READ ONCE, USED TWICE. The caller stores this as `prefilled_claim` and derives BOTH
    `cart_prefilled` and `execution_spec.rail` from it — `rail` is `"referral"` on exactly this
    cold population and carries the same falsifiability, so it is null whenever this is. Do not
    recompute either of them separately: one offer must not carry two different answers to the
    same question in one payload.

    (An earlier version of this note said `rail` was deliberately left alone because its
    two-value vocabulary was one the gateway CONSUMED, making a third state a contract change.
    That was wrong on the facts — `PIVOTA-Agent src/agentSignals/offerToSignal.js` passes `rail`
    through as an opaque label, never checks it against a known set, and has a standing test
    that an unknown rail is relayed rather than nulled. Verify a consumer before deciding a
    field cannot change.)
    """
    if cart_url:
        return True

    # The token's `dest` for this (cold) branch is the same destination_url with UTM and the
    # referral click param appended — query-only edits — so its HOST, the only part warm
    # eligibility reads, is the host of the value passed here.
    redirect_token = _redirect_token_from_url(redirect_url)
    if settings.outbound_warm_handoff_enabled and not redirect_token:
        # Unreachable while _make_external_redirect_url returns `{base}/r?token={token}`, and
        # deliberately not an exception: the pct-rollout branch of eligibility is keyed on the
        # token, so without it `False` is no longer PROVABLE. Say unknown rather than guess —
        # a wrong `False` is the defect this whole function exists to prevent.
        return None
    if could_upgrade_at_click_time(
        dest=destination_url,
        token=redirect_token,
        # EXACT, not conservative: this line is unreachable unless `if cart_url` above fell
        # through, and the mint stamps `join_mode` from that same `cart_url` decision — so the
        # token for this branch provably carries `referral_only`, and the already-a-cart
        # knockout provably cannot fire on it.
        ctx={"join_mode": "referral_only"},
        settings=settings,
    ):
        return None
    return False


def _redirect_token_expiry(redirect_url: Optional[str]) -> Optional[str]:
    """The `/r` token's own expiry, as UTC ISO-8601, or None.

    Read back OFF THE TOKEN rather than recomputed from the TTL constant: an agent caching a
    spec needs to know when `affiliate_url` stops resolving, and a second copy of the TTL would
    be free to drift from the one that actually signed it.

    Returns None for anything unparseable — including a stubbed redirect URL in a test, which
    must degrade to "no expiry claimed" rather than inventing one.
    """
    try:
        token = _redirect_token_from_url(str(redirect_url or ""))
        if not token:
            return None
        payload, _is_expired = parse_redirect_token_verified(token)
        exp = int(payload.get("exp") or 0)
        if exp <= 0:
            return None
        return datetime.fromtimestamp(exp, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except Exception:
        return None


def compose_attributed_destinations(
    *,
    destination_url: str,
    utm_template: Optional[str],
    market: str,
    tool: str,
    shop_domain: Optional[str],
    platform: Optional[str],
    cart_variant_id: Optional[str],
    click_id: str,
    quantity: int = 1,
) -> Dict[str, Any]:
    """Compose every attributed URL for one external offer, ONCE.

    EXECUTION SPEC v0 / T2-12. Two consumers need the same answer and must never disagree:
    `_make_external_redirect_url` picks the destination it signs into the `/r` token, and
    `offers.resolve` publishes `cart_url` / `pdp_url` so the agent's OWN lane carries
    attribution instead of only the `/r` hop. Deriving those separately is the twin-
    implementation drift this codebase keeps paying for — see the note on resolve_cart_permalink.

    `click_id` is REQUIRED and caller-minted on purpose. The join key has to be identical on
    the surface_click_events row, the merchant's order, and the URLs we hand the agent; a
    default here would mint a second id and silently split the join.

    Returns `primary` (what the redirect signs), `cart_url` (None for an honest referral),
    `pdp_url` (always the product page, attributed), and `join_mode`.
    """
    cart_base = resolve_cart_permalink(
        destination_url=destination_url,
        shop_domain=shop_domain,
        platform=platform,
        cart_variant_id=cart_variant_id,
        quantity=quantity,
    )
    join_mode = "cart_permalink" if cart_base else "referral_only"
    utm_ctx = {"market": market, "tool": tool}
    template = utm_template or DEFAULT_UTM_TEMPLATE

    # The PDP is always composed, including when a cart exists: an agent may legitimately show
    # the product page while still handing off to the cart, and it must not have to strip cart
    # syntax to get there. It carries the plain query param, never the cart-attribute form —
    # `attributes[...]` is only meaningful on /cart/.
    pdp_utm = apply_utm(destination_url, template, utm_ctx)
    cart_utm = apply_utm(cart_base, template, utm_ctx) if cart_base else None

    pdp_url = append_referral_click_param(pdp_utm, click_id)
    cart_url = (
        append_shopify_cart_click_attribute(cart_utm, click_id) if cart_utm else None
    )
    return {
        "primary": cart_url or pdp_url,
        "cart_url": cart_url,
        "pdp_url": pdp_url,
        # The same destination BEFORE the join key is appended. The allowlist reader is checked
        # against this: the cart-attribute form carries literal `[` / `]`, and the click param
        # is ours, so neither belongs in a domain decision. Keeping both forms here is what lets
        # the allowlist keep seeing exactly what it saw before this function existed.
        "primary_unkeyed": cart_utm or pdp_utm,
        "join_mode": join_mode,
    }


async def _make_external_redirect_url(
    *,
    market: str,
    tool: str,
    destination_url: str,
    utm_template: Optional[str],
    ctx: Dict[str, Any],
    allowed_domains: Optional[List[str]] = None,
    merchant_id: Optional[str] = None,
    product_id: Optional[str] = None,
    variant_id: Optional[str] = None,
    # CART-ONLY channel for a backfill-recovered numeric Shopify variant id. Read by the
    # permalink construction below and by NOTHING else — in particular it is never stamped
    # into the token ctx, because commerce_attribution_service cross-fills product<->variant
    # ids both ways and a numeric variant id must not leak up a grain into
    # canonical_product_id (round-4 review of #1813).
    # THE ONLY INPUT TO THE CART PERMALINK, and REQUIRED — no default, deliberately.
    #
    # Must be a value the CALLER can justify as a Shopify-issued variant id. There is no
    # fallback to `variant_id`, which is an attribution value and routinely a SKU or a
    # synthetic "{epid}-default"; treating one as a variant id built carts for products
    # Shopify never had under that id.
    #
    # No default, because a defaulted one makes OMISSION silent: a new caller (or a deleted
    # line) would simply stop producing permalinks with nothing failing. Requiring it turns
    # that into a TypeError the suite catches, and forces every call site to say either "here
    # is an id I can justify" or an explicit None — which correctly degrades to referral_only.
    cart_variant_id: Optional[str],
    shop_domain: Optional[str] = None,
    platform: Optional[str] = None,
    quantity: int = 1,
    click_id: Optional[str] = None,
    seller_ref: Optional[str] = None,
    seed_kind: Optional[str] = None,
) -> Optional[str]:
    if not destination_url.startswith(("http://", "https://")):
        return None

    # T2-1: mint a stable click id at redirect-build time so the *same* id lands on both the
    # surface_click_events row (via the signed token ctx) and the merchant order (via the
    # destination). Reusing a caller-supplied id keeps redirect caches idempotent.
    stable_click_id = str(click_id or "").strip() or new_click_id()

    # Join strategy:
    #   cart_permalink -> Shopify store + numeric variant id known: the id rides in
    #     attributes[pivota_click_id], which Shopify persists into the order's note_attributes
    #     (order-side join, closable by T2-2) with zero merchant setup.
    #   referral_only  -> otherwise: keep the product URL and append the id as a plain query
    #     param for click-side attribution only. Honest degradation (no order-side join).
    # ONE composition, shared with offers.resolve (see compose_attributed_destinations), so the
    # URL we sign and the URLs we publish to the agent cannot describe different destinations.
    composed = compose_attributed_destinations(
        destination_url=destination_url,
        utm_template=utm_template,
        market=market,
        tool=tool,
        shop_domain=shop_domain,
        platform=platform,
        cart_variant_id=cart_variant_id,
        click_id=stable_click_id,
        quantity=quantity,
    )
    join_mode = composed["join_mode"]
    dest = composed["primary"]

    runtime_allowed_domains = allowed_domains
    if runtime_allowed_domains is None:
        runtime_allowed_domains = await get_allowed_domains_for_market(market=market)
    # Checked on the destination WITHOUT the join key appended — byte-identical to the
    # pre-refactor `dest_with_utm`. The cart-attribute form carries literal brackets, and the
    # click param is ours; neither belongs in a domain decision.
    if not is_destination_domain_allowed(
        destination_url=composed["primary_unkeyed"],
        allowed_domains=runtime_allowed_domains,
    ):
        return None

    # Enrich the token ctx with the exact keys materialize_attribution_context reads so
    # record_surface_event stamps the stable click id + merchant + product onto
    # surface_click_events instead of minting a fresh throwaway id with NULL merchant/product.
    enriched_ctx: Dict[str, Any] = dict(ctx or {})
    enriched_ctx[PVT_CLICK_ID] = stable_click_id
    enriched_ctx.setdefault(PVT_SURFACE, normalize_surface(tool))
    enriched_ctx.setdefault("tool", tool)
    enriched_ctx["join_mode"] = join_mode
    if merchant_id:
        enriched_ctx["merchant_id"] = merchant_id
    if product_id:
        enriched_ctx[PVT_PRODUCT_ID] = product_id
    if variant_id:
        enriched_ctx[PVT_VARIANT_ID] = variant_id
    # ADR-009 D3: thread the seed's seller-of-record into the signed token ctx.
    # record_surface_event persists the whole ctx into surface_click_events.context
    # (JSONB), so T2-2 closure reads seller_ref/seed_kind from there — see the
    # "column vs context" note in services/commerce_attribution_service.
    # close_external_order_conversion. Only stamp when present: a NULL (legacy)
    # seed threads through as absent, and closure stamps seller_ref_missing.
    if seller_ref:
        enriched_ctx["seller_ref"] = seller_ref
    if seed_kind:
        enriched_ctx["seed_kind"] = seed_kind

    token = make_redirect_token(
        {
            "market": market,
            "tool": tool,
            "dest": dest,
            "ctx": enriched_ctx,
        }
    )
    base = resolve_public_api_base_url()
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
    # NO `or "USD"`. A seed with no recorded currency now carries None, not a
    # fabricated one. The chain ending in a constant emitted a value no source
    # asserted AND made it impossible for anything downstream to ask "what
    # currency is this actually?" — the field was always populated. #1634.
    #
    # Measured on prod 2026-07-30 before removing it: 159 of 11,381 active seeds
    # have no currency, and ZERO of those carry a price. So this is latent, not
    # a live wrong-price bug — which is also why it was safe to remove. A
    # currency-less row must not become quotable; see _budget_allows_price and
    # the quotable gate in the ACP feed.
    price_currency = _observed_currency(
        row.get("price_currency"), seed_data.get("price_currency")
    )
    availability = row.get("availability") or seed_data.get("availability") or "unknown"
    variants = _normalize_seed_variants(seed_data)
    merchant_name = _external_seed_display_name(row, seed_data)
    external_product_id = seed_data.get("external_product_id") or _stable_external_product_id(
        row.get("canonical_url") or row.get("destination_url")
    )
    ingredient_ids = _normalize_external_seed_structured_ingredient_ids(row, seed_data)
    product_type = _normalize_external_seed_product_type(row, seed_data)

    in_stock = True
    if isinstance(availability, str):
        in_stock = availability.lower() not in {"out_of_stock", "outofstock", "sold_out"}

    # Structured brand so agents can cite "<brand>'s <product>" without parsing the
    # title. External seeds carry brand/vendor in seed_data; fall back to the
    # merchant display name with a trailing " Official Site/Store" suffix stripped.
    # (The connected-merchant projection _standard_to_shop_product does the same;
    # this covers the external-seed lane, which is the bulk of catalog results.)
    seed_brand = str(seed_data.get("brand") or seed_data.get("vendor") or "").strip()
    if not seed_brand:
        seed_brand = _clean_brand_from_merchant_name(merchant_name) or ""

    filter_product = _build_external_seed_filter_product(
        row=row,
        seed_data=seed_data,
        external_product={
            "id": external_product_id,
            "product_id": external_product_id,
            "title": title,
            "description": seed_data.get("description") or "",
            "price": price_amount or 0,
            "currency": price_currency,
            "image_url": image_url,
            "in_stock": in_stock,
            "external_seed_id": row.get("id"),
        },
    )

    product: Dict[str, Any] = {
        "id": external_product_id,
        "product_id": external_product_id,
        "merchant_id": None,
        "merchant_name": merchant_name,
        "title": title or "External product",
        "brand": seed_brand or None,
        "description": seed_data.get("description") or "",
        "price": price_amount or 0,
        "currency": price_currency,
        "image_url": image_url,
        "product_type": product_type or None,
        "inventory_quantity": 999 if in_stock else 0,
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
        "visible_attributes": dict(filter_product.visible_attributes or {}),
    }
    if ingredient_ids:
        product["ingredient_ids"] = list(ingredient_ids)
    return product


def _normalize_prefetched_external_seed_candidates(
    request_metadata: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if not isinstance(request_metadata, dict):
        return []
    raw_candidates = request_metadata.get("external_seed_candidates")
    if not isinstance(raw_candidates, list):
        return []
    normalized: List[Dict[str, Any]] = []
    seen_product_ids: set[str] = set()
    for candidate in raw_candidates:
        candidate_dict = _coerce_product_payload_dict(candidate)
        if not candidate_dict:
            continue
        product_id = str(
            candidate_dict.get("product_id")
            or candidate_dict.get("id")
            or candidate_dict.get("external_product_id")
            or ""
        ).strip()
        if not product_id or product_id in seen_product_ids:
            continue
        seen_product_ids.add(product_id)
        normalized.append(candidate_dict)
    return normalized


async def _build_prefetched_external_seed_wrappers(
    request_metadata: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    candidates = _normalize_prefetched_external_seed_candidates(request_metadata)
    if not candidates:
        return []

    wrappers: List[Dict[str, Any]] = []
    redirect_cache: Dict[str, Optional[str]] = {}
    for candidate in candidates:
        destination_url = str(
            candidate.get("destination_url")
            or candidate.get("url")
            or candidate.get("canonical_url")
            or ""
        ).strip()
        if not destination_url.startswith(("http://", "https://")):
            continue

        canonical_url = str(candidate.get("canonical_url") or destination_url).strip() or destination_url
        market = str(candidate.get("market") or "US").strip() or "US"
        tool = str(candidate.get("tool") or "*").strip() or "*"
        utm_template = candidate.get("utm_template")
        redirect_url = str(candidate.get("external_redirect_url") or "").strip() or None
        if not redirect_url:
            redirect_identity = _external_seed_redirect_identity(
                row=candidate,
                seed_data=_ensure_seed_data_obj(candidate.get("seed_data")) or {},
                offer_variant_id=candidate.get("variant_id"),
            )
            # ADR-009 D3: seller_ref/seed_kind ride in the token ctx, so a cache
            # hit must not reuse a redirect built for a different seller. Include
            # them in the key (cheap — read from the already-loaded row).
            redirect_cache_key = "||".join([
                market, tool, destination_url, str(utm_template or ""),
                str(redirect_identity.get("seller_ref") or ""),
                str(redirect_identity.get("seed_kind") or ""),
            ])
            if redirect_cache_key in redirect_cache:
                redirect_url = redirect_cache[redirect_cache_key]
            else:
                redirect_url = await _make_external_redirect_url(
                    market=market,
                    tool=tool,
                    destination_url=destination_url,
                    utm_template=utm_template,
                    ctx={"seedId": candidate.get("external_seed_id")},
                    allowed_domains=None,
                    merchant_id=redirect_identity["merchant_id"],
                    product_id=redirect_identity["product_id"],
                    variant_id=redirect_identity["variant_id"],
                    cart_variant_id=redirect_identity.get("cart_variant_id"),
                    shop_domain=redirect_identity["shop_domain"],
                    platform=redirect_identity["platform"],
                    seller_ref=redirect_identity["seller_ref"],
                    seed_kind=redirect_identity["seed_kind"],
                )
                redirect_cache[redirect_cache_key] = redirect_url
        if not redirect_url:
            continue

        seed_data = _ensure_seed_data_obj(candidate.get("seed_data"))
        seed_data = dict(seed_data) if isinstance(seed_data, dict) else {}
        category = None
        for category_candidate in (
            candidate.get("category"),
            candidate.get("product_type"),
            seed_data.get("category"),
            seed_data.get("product_type"),
        ):
            text = str(category_candidate or "").strip()
            if not text or text.lower() == "external":
                continue
            category = text
            break
        ingredient_ids = candidate.get("ingredient_ids") or seed_data.get("reviewed_ingredient_ids")
        if candidate.get("title") and not seed_data.get("title"):
            seed_data["title"] = candidate.get("title")
        if candidate.get("description") and not seed_data.get("description"):
            seed_data["description"] = candidate.get("description")
        if category and not seed_data.get("category"):
            seed_data["category"] = category
        if candidate.get("brand") and not seed_data.get("brand"):
            seed_data["brand"] = candidate.get("brand")
        if candidate.get("merchant_name") and not seed_data.get("merchant_display_name"):
            seed_data["merchant_display_name"] = candidate.get("merchant_name")
        if candidate.get("variants") and not seed_data.get("variants"):
            seed_data["variants"] = candidate.get("variants")
        if ingredient_ids and not seed_data.get("reviewed_ingredient_ids"):
            seed_data["reviewed_ingredient_ids"] = ingredient_ids
        if canonical_url and not seed_data.get("canonical_url"):
            seed_data["canonical_url"] = canonical_url
        if destination_url and not seed_data.get("destination_url"):
            seed_data["destination_url"] = destination_url

        parsed_dest = urlparse(destination_url)
        row = {
            "id": candidate.get("external_seed_id"),
            "external_product_id": candidate.get("external_product_id") or candidate.get("product_id") or candidate.get("id"),
            "market": market,
            "tool": tool,
            "destination_url": destination_url,
            "canonical_url": canonical_url,
            "domain": candidate.get("domain") or parsed_dest.netloc,
            "title": candidate.get("title"),
            "image_url": candidate.get("image_url"),
            "price_amount": candidate.get("price"),
            "price_currency": candidate.get("currency"),
            "availability": candidate.get("availability") or ("in_stock" if candidate.get("in_stock") is not False else "out_of_stock"),
            "attached_product_key": candidate.get("attached_product_key"),
            "attached_variant_id": candidate.get("attached_variant_id"),
        }
        if category:
            row["category"] = category

        product = _external_seed_to_shop_product(
            row=row,
            seed_data=seed_data,
            redirect_url=redirect_url,
        )
        filter_product = _build_external_seed_filter_product(
            row=row,
            seed_data=seed_data,
            external_product=product,
        )
        try:
            relevance_score = float(candidate.get("relevance_score") or 0.8)
        except Exception:
            relevance_score = 0.8
        wrappers.append(
            {
                "product": product,
                "filter_product": filter_product,
                "merchant_name": product.get("merchant_name"),
                "relevance_score": relevance_score,
            }
        )
    return wrappers


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

    # Test/demo rigs never surface publicly, even on an explicit merchant-scoped
    # query. Mirrors the multi lane's exclusion; see services/test_merchant_policy.
    try:
        from db.database import database as _db
        _excluded_ids = await get_excluded_merchant_ids(_db)
        if _excluded_ids:
            _before = len(visible)
            visible = filter_out_test_merchants(visible, _excluded_ids)
            if len(visible) != _before:
                logger.info(
                    "find_products.excluded_test_merchants",
                    extra={"dropped": _before - len(visible), "merchant_id": merchant_id},
                )
    except Exception:
        pass  # never let the rig filter break search

    # Quarantined-source exclusion — the merchant-scoped twin of the gate in
    # _handle_find_products_multi. Both are needed: `POST /agent/shop/v1/invoke`
    # has NO auth dependency, and _normalize_find_products_payload routes a
    # payload carrying a top-level merchant_id to THIS handler, so an unsigned
    # `GET /acp/feed` body of {"query":{"merchant_id":"...","query":"serum"}}
    # reaches here rather than the multi lane.
    #
    # ⚠️ SCOPE, stated honestly because the obvious reading is wrong. This lane
    # sources rows from products_cache ONLY, as StandardProduct — which is
    # extra='ignore', so seven of the eight URL fields the policy checks are
    # structurally unreachable here. `online_store_url` is the only real one, and
    # only the Wix adapter populates it (Shopify explicitly does not; WooCommerce
    # keeps its permalink in platform_metadata). So on THIS lane a `domain`
    # quarantine can fire for Wix merchants, and everything else is covered only
    # by `merchant_platform` quarantines.
    #
    # It is still worth having — merchant_platform is a real, used match type, and
    # the alternative is an unauthenticated door with no gate at all — but do not
    # read this as closing the external-seed hole. No external-seed row can reach
    # this handler; that is the multi lane's job. Giving Shopify rows a resolvable
    # host (handle + connected shop domain) is the follow-up that would make this
    # gate general.
    try:
        # Imported here rather than reusing the sibling block's `_db`: that name
        # is only bound if the rig block's own import succeeded, so borrowing it
        # would make this gate's availability depend on an unrelated try.
        from db.database import database as _quarantine_db
        from services.offer_currency_policy import (
            filter_out_quarantined_rows,
            get_quarantined_sources,
        )

        _quarantines = await get_quarantined_sources(_quarantine_db)
        _before_q = len(visible)
        visible = filter_out_quarantined_rows(visible, _quarantines)
        if len(visible) != _before_q:
            logger.info(
                "find_products.excluded_quarantined",
                extra={"dropped": _before_q - len(visible), "surface": "find_products"},
            )
    except Exception:
        # Fail open so a gate hiccup cannot empty a merchant's own catalog —
        # but never silently: failing open republishes mislabelled prices.
        logger.warning(
            "find_products.quarantine_filter_failed — slate served WITHOUT the gate",
            exc_info=True,
        )

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
    product_payloads = [_standard_to_shop_product(p) for p in page_items]
    product_payloads = await _enrich_product_cards_with_savings_evidence(
        product_payloads,
        merchant_id=merchant_id,
        payment_context=None,
        market=None,
    )
    # P2b: attributed /r links on connected cards (fail-soft, additive).
    await _attach_connected_product_redirects(product_payloads, tool="find_products")

    result = {
        "products": product_payloads,
        "total": total,
        "page": page,
        "page_size": len(page_items),
        "metadata": {
            "query_source": query_source,
            "fetched_at": datetime.utcnow().isoformat(),
        },
    }
    # Phase 0 (convergence): merchant-scoped search also deposits its slate.
    _record_gateway_decision_events(
        result,
        surface="agent_shop_gateway.find_products",
        query=filters.query,
        merchant_id=merchant_id,
    )
    return result


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
    *,
    emit_decision_event: bool = True,
) -> Dict[str, Any]:
    """Thin wrapper over the real multi-search implementation that stamps
    attributed /r links onto connected-merchant cards on EVERY return branch
    (the implementation has many exits — cached, pivot, fallback, retry). P2b;
    fail-soft and idempotent (already-stamped cards, incl. external_seed, are
    skipped), so re-entrant retry paths that call this wrapper again are safe.

    ``emit_decision_event`` (Phase 0): this wrapper is ALSO used as an internal
    building block — find_similar_products calls it (primary + broad fallback)
    and the fragrance semantic-retry re-invokes it — where the produced slate is
    an intermediate, not what gets served. Those callers pass False so the
    decision-layer ledger records exactly one event per SERVED slate, not the
    intermediate queries (which would pollute the behavioral baseline)."""
    result = await _handle_find_products_multi_inner(payload, request_metadata, background_tasks)
    # Exclude test/demo rigs BEFORE redirect stamping + decision recording, so a
    # rig is neither /r-attributed nor deposited in the behavioral ledger. This
    # is the wrapper over EVERY inner return branch (cached, pivot, fallback,
    # retry), so it is the single choke point for the connected_catalog /
    # products_cache lane — which is gated only by _is_product_sellable and was
    # serving Shopify's stock dev-store sample catalog on the UNAUTH public
    # find_products_multi endpoint (2026-07-23). See services/test_merchant_policy.
    try:
        if isinstance(result, dict) and isinstance(result.get("products"), list):
            from db.database import database as _db
            excluded_ids = await get_excluded_merchant_ids(_db)
            before = len(result["products"])
            result["products"] = filter_out_test_merchants(result["products"], excluded_ids)
            dropped = before - len(result["products"])
            if dropped:
                # Keep the counters honest so a shrunken slate is not read as a
                # ranking regression.
                if isinstance(result.get("total"), int):
                    result["total"] = max(0, result["total"] - dropped)
                result["page_size"] = len(result["products"])
                logger.info(
                    "find_products_multi.excluded_test_merchants",
                    extra={"dropped": dropped, "surface": "find_products_multi"},
                )
    except Exception:
        pass  # never let the rig filter break search
    # Quarantined-source exclusion, at the SAME choke point and for the same
    # reason. On 2026-07-28 this lane published seven Mintree rows priced
    # 847-3927.70 and labelled "USD" on the public UNAUTHENTICATED ACP feed —
    # rupee prices served as US dollars. Every other door blocks those stores;
    # this lane read none of the gates. It cannot be folded into the rig filter
    # above: merchant_id is explicitly None on external-seed rows, so that filter
    # structurally cannot see them.
    #
    # See services/offer_currency_policy — the gate READS catalog_source_quarantine
    # (the mechanism that has a writer, a revoke path and expiry, and that
    # catalog_row_trust_upserter already reads) rather than restating the rule,
    # because a fourth copy of a predicate is how the first three drifted.
    #
    # POSITION IS LOAD-BEARING: this must stay ABOVE _attach_connected_product_
    # redirects and _record_gateway_decision_events, so a quarantined row is
    # neither /r-attributed nor deposited in the behavioural ledger. Moving this
    # block below them leaves the served slate correct and silently poisons both
    # — a test asserts the ordering.
    try:
        if isinstance(result, dict) and isinstance(result.get("products"), list):
            from db.database import database as _db
            from services.offer_currency_policy import (
                filter_out_quarantined_rows,
                get_quarantined_sources,
            )

            quarantines = await get_quarantined_sources(_db)
            before = len(result["products"])
            result["products"] = filter_out_quarantined_rows(
                result["products"], quarantines
            )
            dropped = before - len(result["products"])
            if dropped:
                # Keep the counters honest so a shrunken slate is not read as a
                # ranking regression — same contract as the rig filter.
                if isinstance(result.get("total"), int):
                    result["total"] = max(0, result["total"] - dropped)
                result["page_size"] = len(result["products"])
                logger.info(
                    "find_products_multi.excluded_quarantined",
                    extra={"dropped": dropped, "surface": "find_products_multi"},
                )
    except Exception:
        # Never break search — but never fail SILENTLY either. Failing open here
        # means mislabelled prices are publishable again, which is precisely the
        # state this gate exists to end; a bare `pass` would recreate the bug and
        # leave nothing to notice it by.
        logger.warning(
            "find_products_multi.quarantine_filter_failed — "
            "slate served WITHOUT the quarantine gate",
            exc_info=True,
        )
    try:
        if isinstance(result, dict):
            await _attach_connected_product_redirects(result.get("products"), tool="find_products_multi")
    except Exception:
        pass  # never let attribution stamping break search
    # Phase 0 (convergence): deposit the served slate into the decision-layer
    # ledger AFTER redirect stamping so eligibility_flags see the final cards.
    if emit_decision_event:
        _record_gateway_decision_events(
            result,
            surface="agent_shop_gateway.find_products_multi",
            query=payload.search.query if payload and payload.search else None,
            source=(request_metadata or {}).get("source") if isinstance(request_metadata, dict) else None,
            extra_context={
                "page": payload.search.page if payload and payload.search else None,
                "limit": payload.search.limit if payload and payload.search else None,
            },
        )
    return result


async def _handle_find_products_multi_inner(
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
    pivot_shadow_schedule_suppressed = bool(
        isinstance(request_metadata, dict)
        and request_metadata.get("_pivot_shadow_schedule_suppressed")
    )

    # Creator surfaces are
    # allowed to use a broader cross-merchant pool and slightly more
    # permissive visibility rules (do not drop products solely because
    # orderable is false).
    is_creator_surface = source_normalized == "creator-agent"
    is_shopping_surface = _is_shopping_multi_source(source_normalized)
    force_cache_only = _resolve_multi_force_cache_only(source_normalized, is_creator_surface)
    base_merchant_fanout_enabled = _resolve_multi_base_merchant_fanout(
        source_normalized,
        is_creator_surface,
    )
    commerce_surface, commerce_surface_explicit = _resolve_commerce_surface(
        payload_surface=filters.commerce_surface,
        request_metadata=request_metadata,
    )
    strict_serving_mode = bool(commerce_surface_explicit)
    page = filters.page or 1
    limit = _clamp_search_limit(filters.limit, fallback=20)

    if (
        not pivot_shadow_schedule_suppressed
        and
        PIVOT_MULTI_SHADOW_ENABLED
        and str(filters.query or "").strip()
        and _pivot_multi_rollout_allowed(
            source_normalized=source_normalized,
            page=page,
            mode="shadow",
        )
    ):
        background_tasks.add_task(
            _shadow_find_products_multi_via_pivot,
            payload.model_copy(deep=True),
            dict(request_metadata or {}),
        )

    if (
        PIVOT_MULTI_SERVE_ENABLED
        and str(filters.query or "").strip()
        and _pivot_multi_rollout_allowed(
            source_normalized=source_normalized,
            page=page,
            mode="serve",
        )
    ):
        pivot_result = await _handle_find_products_multi_via_pivot(
            payload,
            request_metadata,
        )
        if pivot_result:
            return pivot_result

    should_try_upstream = (
        is_shopping_surface
        and bool(MULTI_SEARCH_UPSTREAM_FALLBACK_BASE_URL)
        and upstream_fallback_hop < 1
    )
    upstream_fallback_attempted = False
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
        upstream_fallback_attempted = True
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
            _, raw_items = _order_row_merchant_items(row)
            if raw_items is None:
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
            merchant_id, product_data = _products_cache_row_candidate(row)
            if product_data is None:
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
            merchant_id, raw_items = _order_row_merchant_items(row)
            if not merchant_id or raw_items is None:
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
            _, product_data = _products_cache_row_candidate(row)
            if product_data is None:
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
            merchant_id, raw_items = _order_row_merchant_items(row)
            if not merchant_id or raw_items is None:
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
            _, product_data = _products_cache_row_candidate(row)
            if product_data is None:
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
            _, product_data = _products_cache_row_candidate(row)
            if product_data is None:
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
    parsed_budget = _extract_query_budget_constraints(q_raw)
    q = str(parsed_budget.get("clean_query") or q_raw or "").strip()
    explicit_price_min = filters.price_min
    explicit_price_max = filters.price_max
    parsed_price_min = parsed_budget.get("price_min")
    parsed_price_max = parsed_budget.get("price_max")
    effective_price_min = explicit_price_min
    effective_price_max = explicit_price_max
    if parsed_price_min is not None:
        effective_price_min = (
            parsed_price_min
            if effective_price_min is None
            else max(float(effective_price_min), float(parsed_price_min))
        )
    if parsed_price_max is not None:
        effective_price_max = (
            parsed_price_max
            if effective_price_max is None
            else min(float(effective_price_max), float(parsed_price_max))
        )
    budget_currency = str(parsed_budget.get("currency") or "").strip().upper() or None
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

    pet_accessory_query_terms = (
        "harness",
        "harnesses",
        "leash",
        "leashes",
        "collar",
        "collars",
        "lead",
        "leads",
    )
    pet_subject_query_terms = (
        "dog",
        "dogs",
        "cat",
        "cats",
        "pet",
        "pets",
        "puppy",
        "puppies",
        "kitten",
        "kittens",
    )
    pet_accessory_intent_query = any(term in q_ascii for term in pet_accessory_query_terms)
    pet_subject_intent_query = any(term in q_ascii for term in pet_subject_query_terms)
    visible_category_intent_groups = [
        {
            "label": "serum",
            "query_terms": ["serum", "serums"],
            "product_terms": ["serum", "serums"],
            "semantic_classes": ["beauty"],
            "visible_attribute_bucket": "product_category",
        },
        {
            "label": "moisturizer",
            "query_terms": ["moisturizer", "moisturizers", "moisturiser", "moisturisers"],
            "product_terms": ["moisturizer", "moisturizers", "moisturiser", "moisturisers"],
            "semantic_classes": ["beauty"],
            "visible_attribute_bucket": "product_category",
        },
        {
            "label": "cleanser",
            "query_terms": ["cleanser", "cleansers"],
            "product_terms": ["cleanser", "cleansers"],
            "semantic_classes": ["beauty"],
            "visible_attribute_bucket": "product_category",
        },
        {
            "label": "toner",
            "query_terms": ["toner", "toners"],
            "product_terms": ["toner", "toners"],
            "semantic_classes": ["beauty"],
            "visible_attribute_bucket": "product_category",
        },
        {
            "label": "foundation",
            "query_terms": ["foundation", "foundations"],
            "product_terms": ["foundation", "foundations"],
            "semantic_classes": ["beauty"],
        },
        {
            "label": "lipstick",
            "query_terms": ["lipstick", "lipsticks"],
            "product_terms": ["lipstick", "lipsticks"],
            "semantic_classes": ["beauty"],
        },
        {
            "label": "blush",
            "query_terms": ["blush", "blushes"],
            "product_terms": ["blush", "blushes"],
            "semantic_classes": ["beauty"],
        },
        {
            "label": "gloss",
            "query_terms": ["gloss", "glosses", "lip gloss", "lip glosses"],
            "product_terms": ["gloss", "glosses", "lip gloss", "lip glosses"],
            "semantic_classes": ["beauty"],
        },
        {
            "label": "hoodie",
            "query_terms": ["hoodie", "hoodies", "sudadera", "sudaderas"],
            "product_terms": ["hoodie", "hoodies", "sudadera", "sudaderas"],
        },
        {
            "label": "sweater",
            "query_terms": ["sweater", "sweaters", "jumper", "jumpers"],
            "product_terms": ["sweater", "sweaters", "jumper", "jumpers", "knit sweater", "knitted sweater"],
        },
        {
            "label": "vest",
            "query_terms": ["vest", "vests"],
            "product_terms": ["vest", "vests"],
        },
        {
            "label": "skirt",
            "query_terms": ["skirt", "skirts", "falda", "faldas"],
            "product_terms": ["skirt", "skirts", "falda", "faldas"],
        },
        {
            "label": "dress",
            "query_terms": ["dress", "dresses", "vestido", "vestidos"],
            "product_terms": ["dress", "dresses", "vestido", "vestidos"],
        },
    ]
    active_visible_category_intents = []
    for group in visible_category_intent_groups:
        if not _normalized_intent_terms_match(q_ascii, list(group["query_terms"])):
            continue
        allowed_semantic_classes = {str(item) for item in (group.get("semantic_classes") or []) if item}
        if allowed_semantic_classes and query_semantic_class not in allowed_semantic_classes:
            continue
        active_visible_category_intents.append(group)
    active_visible_category_labels = [str(group["label"]) for group in active_visible_category_intents]
    unsupported_beauty_category_intent_groups = [
        {
            "label": "skincare",
            "query_terms": ["skincare", "skin care", "skin-care"],
        },
        {
            "label": "cosmetics",
            "query_terms": ["cosmetics", "makeup", "make-up"],
        },
    ]
    active_unsupported_beauty_category_labels = [
        str(group["label"])
        for group in unsupported_beauty_category_intent_groups
        if _normalized_intent_terms_match(q_ascii, list(group["query_terms"]))
    ]
    apparel_visible_category_labels = {"hoodie", "sweater", "vest", "skirt", "dress"}
    visible_attribute_intent_groups = [
        {
            "label": "fragrance_free",
            "query_terms": [
                "fragrance free",
                "fragrance-free",
                "free fragrance",
                "sin fragancia",
            ],
            "product_terms": [
                "fragrance free",
                "fragrance-free",
                "free fragrance",
                "without fragrance",
                "no fragrance",
                "sin fragancia",
            ],
            "semantic_classes": ["beauty"],
            "category_labels": ["serum"],
            "visible_attribute_bucket": "formula_constraint",
        },
        {
            "label": "sensitive_skin",
            "query_terms": ["sensitive skin", "sensitive-skin"],
            "product_terms": ["sensitive skin", "sensitive-skin"],
            "semantic_classes": ["beauty"],
            "category_labels": ["serum"],
            "visible_attribute_bucket": "skin_concern",
        },
        {
            "label": "hydrating",
            "query_terms": ["hydrating", "hydrate", "hydration"],
            "product_terms": ["hydrating", "hydrate", "hydration"],
            "semantic_classes": ["beauty"],
            "category_labels": ["serum"],
            "visible_attribute_bucket": "skin_concern",
        },
        {
            "label": "brightening",
            "query_terms": ["brightening", "brighten"],
            "product_terms": ["brightening", "brighten"],
            "semantic_classes": ["beauty"],
            "category_labels": ["serum"],
            "visible_attribute_bucket": "skin_concern",
        },
        {
            "label": "waterproof",
            "query_terms": ["waterproof", "water-resistant", "water resistant"],
            "product_terms": ["waterproof", "water-resistant", "water resistant"],
            "category_labels": sorted(apparel_visible_category_labels),
        },
        {
            "label": "cotton",
            "query_terms": ["cotton"],
            "product_terms": ["cotton"],
            "category_labels": sorted(apparel_visible_category_labels),
        },
        {
            "label": "wool",
            "query_terms": ["wool", "woolen", "woollen"],
            "product_terms": ["wool", "woolen", "woollen"],
            "category_labels": sorted(apparel_visible_category_labels),
        },
        {
            "label": "fleece",
            "query_terms": ["fleece", "polar fleece"],
            "product_terms": ["fleece", "polar fleece"],
            "category_labels": sorted(apparel_visible_category_labels),
        },
        {
            "label": "sleeveless",
            "query_terms": ["sleeveless"],
            "product_terms": ["sleeveless"],
            "category_labels": sorted(apparel_visible_category_labels),
        },
        {
            "label": "striped",
            "query_terms": ["striped", "stripe"],
            "product_terms": ["striped", "stripe"],
            "category_labels": sorted(apparel_visible_category_labels),
        },
        {
            "label": "color_block",
            "query_terms": ["color block", "color-block", "colour block", "colour-block"],
            "product_terms": ["color block", "color-block", "colour block", "colour-block"],
            "category_labels": sorted(apparel_visible_category_labels),
        },
        {
            "label": "knitted",
            "query_terms": ["knitted", "knit"],
            "product_terms": ["knitted", "knit"],
            "category_labels": sorted(apparel_visible_category_labels),
        },
    ]
    active_visible_attribute_intents = []
    for group in visible_attribute_intent_groups:
        if not _normalized_intent_terms_match(q_ascii, list(group["query_terms"])):
            continue
        allowed_semantic_classes = {str(item) for item in (group.get("semantic_classes") or []) if item}
        if allowed_semantic_classes and query_semantic_class not in allowed_semantic_classes:
            continue
        allowed_category_labels = {str(item) for item in (group.get("category_labels") or []) if item}
        if allowed_category_labels and not any(label in allowed_category_labels for label in active_visible_category_labels):
            continue
        active_visible_attribute_intents.append(group)
    active_visible_attribute_labels = [str(group["label"]) for group in active_visible_attribute_intents]
    active_visible_option_intents = [
        *_extract_visible_size_option_intents(q_ascii),
        *_extract_visible_color_option_intents(
            q_ascii,
            active_category_labels=active_visible_category_labels,
        ),
        *_extract_visible_shade_option_intents(
            q_ascii,
            active_category_labels=active_visible_category_labels,
        ),
    ]
    active_visible_option_labels = [str(group["label"]) for group in active_visible_option_intents]
    cosmetic_shade_category_intents = [
        label for label in active_visible_category_labels if label in _COSMETIC_SHADE_CATEGORY_LABELS
    ]
    requires_explicit_shade_query = bool(cosmetic_shade_category_intents)
    has_active_shade_option_intent = any(label.startswith("shade_") for label in active_visible_option_labels)
    active_ingredient_intents = _extract_skin_care_ingredient_intents(
        q_ascii,
        query_semantic_class=query_semantic_class,
    )
    active_ingredient_labels = [str(group["ingredient_id"]) for group in active_ingredient_intents]
    non_strict_beauty_text_recall_enabled = query_semantic_class == "beauty" and not strict_serving_mode
    # Ingredient text-recall is allowed for ALL beauty queries, including the strict agent/MCP surface. A
    # generic discovery query like "vitamin c serum" should match serums that name the ingredient in their
    # text and rank structured-evidence products higher — not hard-zero when structured ingredient_ids are
    # absent. Scoped to the ingredient gate only (does not widen the broader non_strict recall). (#1659)
    beauty_ingredient_text_recall_enabled = query_semantic_class == "beauty"
    # Category/attribute text-recall on the STRICT agent surface. Mirrors the
    # ingredient text-recall #1659 made always-on for beauty: a category query like
    # "green tea toner" should match toners that NAME the category in their text
    # instead of hard-zeroing when structured visible_attributes are absent. On the
    # strict agent surface (agent_api) essentially no products carry structured
    # visible_attributes, so the structured-only category/attribute gate blocks
    # EVERY beauty category query. Flag-gated (STRICT_BEAUTY_CATEGORY_TEXT_RECALL,
    # default OFF ⇒ byte-identical). Precision preserved: the category/attribute
    # word must appear in the product TITLE/TYPE/TAGS (never description), and the
    # merchant-status gate + downstream precision still apply.
    beauty_category_text_recall_enabled = query_semantic_class == "beauty" and (
        not strict_serving_mode
        or (os.getenv("STRICT_BEAUTY_CATEGORY_TEXT_RECALL") or "").strip().lower()
        in {"1", "true", "yes", "on"}
    )
    expanded_shopping_beauty_prefetch = False
    # Apply the generic-default precision gate to ANY default-class generic query with no structured
    # intents, regardless of source or serving mode. The OR-over-terms lexical recall otherwise leaks
    # off-domain products (e.g. "paula choice" → dog harnesses whose description merely contains "choice"),
    # which is wrong on every surface — including the strict agent/MCP commerce surface (find_products →
    # find_products_multi fallback), which previously skipped the gate via `not strict_serving_mode` and the
    # UI-only source allowlist. The gate requires ≥0.6 term coverage over title/type/vendor/sku/tags (not
    # description), so single-term and genuinely-matching queries are unaffected. #1659.
    generic_default_precision_gate_enabled = bool(
        query_semantic_class == "default"
        and not active_visible_category_intents
        and not active_visible_attribute_intents
        and not active_visible_option_intents
        and not active_ingredient_intents
    )
    # Bare brand-name queries (e.g. "acropass") frequently classify as the
    # "default" semantic class, which blocks the external-seed leg — where
    # observed / crawl-seed brands live — so the query returns zero instead of
    # that brand's own products. When the query names a brand that ACTUALLY
    # EXISTS in the catalog (dynamic dictionary, GATEWAY_DYNAMIC_BRAND_DETECT) or
    # a curated static brand, allow the external-seed fallback: the seed fetch is
    # scoped by the query terms so it returns brand-matching rows (not the
    # off-domain lexical junk this gate guards against), and the
    # generic_default_precision_gate still filters multi-term default queries.
    # Mirrors the Node gateway brand-detection guard (#1769).
    brand_query_detected = False
    brand_query_terms: List[str] = []
    try:
        from routes.agent_api import (
            _detect_brand_query as _agent_detect_brand_query,
            _ensure_brand_dictionary_loaded as _agent_ensure_brand_dictionary_loaded,
        )

        await _agent_ensure_brand_dictionary_loaded()
        _brand_detect = _agent_detect_brand_query(q_ascii or q_lower) or {}
        if _brand_detect.get("brand_like") and str(_brand_detect.get("mode") or "") in {
            "catalog",
            "static",
        }:
            brand_query_detected = True
            brand_query_terms = [str(t) for t in (_brand_detect.get("brand_terms") or []) if t]
    except Exception:
        # Never let brand detection break recall — fall back to the prior gate.
        brand_query_detected = False
        brand_query_terms = []
    semantic_external_seed_fallback_allowed = bool(
        strict_serving_mode
        or query_semantic_class in {"beauty", "fragrance"}
        or brand_query_detected
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
            mid, product_data = _products_cache_row_candidate(row)
            merchant_id = mid or None
            if product_data is None:
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
    strict_external_output_by_product_id: Dict[str, Dict[str, Any]] = {}
    external_seed_query_timeout = False
    external_seed_rows_fetched = 0
    external_seed_brand_strict_rows = 0
    external_seed_brand_relevant_rows = 0
    external_seed_broad_fallback_used = False
    external_seed_broad_scope_rows = 0
    external_seed_ranked_count = 0
    external_seed_skip_reason: Optional[str] = None
    budget_fx_applied = False
    budget_fx_rate: Optional[float] = None
    budget_fx_source: Optional[str] = None
    budget_fx_unresolved = False
    budget_fx_candidate_currency: Optional[str] = None
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
            shopping_seed_cap = int(MULTI_SEARCH_SEED_QUERY_LIMIT_SHOPPING or 0)
            if shopping_seed_cap <= 0:
                shopping_seed_cap = 200
            seed_limit = min(seed_limit, shopping_seed_cap)
        seed_rows: List[Any] = []
        ranked_seed_candidates = []
        if not semantic_external_seed_fallback_allowed:
            external_seed_skip_reason = "semantic_class_blocked"
        elif seed_limit > 0:
            stage_a_timeout_seconds = max(
                0.05,
                min(
                    float(MULTI_SEARCH_SEED_QUERY_TIMEOUT_SECONDS or 1.6),
                    0.9,
                ),
            )
            _seed_fast_multiterm = _seed_query_fast_multiterm_enabled()
            stage_a_result = await fetch_external_seed_rows(
                database=database,
                market=None,
                query=q_ascii or q_lower,
                limit=seed_limit,
                offset=0,
                include_seed_data_text_match=False,
                only_unattached=False,
                query_timeout_seconds=stage_a_timeout_seconds,
                required_terms=None,
                prefer_terms=seed_query_terms or None,
                scope="default",
                use_required_terms_filter=False,
                include_total_count=False,
                fast_multiterm=_seed_fast_multiterm,
                # Lean WHERE (inline columns only) for long many-token queries, so a
                # broad OR over the trgm-indexed retrieval_summary can't detoast
                # thousands of rows and time stage-A out to 0. Gated behind the fast
                # flag ⇒ byte-identical when the flag is off. Stage-B (the empty-
                # result fallback below) deliberately keeps the full recall path.
                lean_where_min_tokens=(
                    _seed_query_lean_where_min_tokens() if _seed_fast_multiterm else None
                ),
            )
            seed_rows = stage_a_result.get("rows") or []
            external_seed_query_timeout = bool(stage_a_result.get("query_timeout") or False)
            external_seed_rows_fetched = len(seed_rows)
            external_seed_brand_relevant_rows = len(seed_rows)

            if (
                not seed_rows
                and bool(q_lower)
                and bool(MULTI_SEARCH_SHOPPING_ENABLE_SEED_TEXT_SCAN)
            ):
                external_seed_broad_fallback_used = True
                stage_b_result = await fetch_external_seed_rows(
                    database=database,
                    market=None,
                    query=q_ascii or q_lower,
                    limit=seed_limit,
                    offset=0,
                    include_seed_data_text_match=True,
                    only_unattached=False,
                    query_timeout_seconds=float(MULTI_SEARCH_SEED_QUERY_TIMEOUT_SECONDS or 1.6),
                    required_terms=None,
                    prefer_terms=seed_query_terms or None,
                    scope="default",
                    use_required_terms_filter=False,
                    include_total_count=False,
                    fast_multiterm=_seed_query_fast_multiterm_enabled(),
                )
                stage_b_rows = stage_b_result.get("rows") or []
                external_seed_query_timeout = bool(
                    external_seed_query_timeout or stage_b_result.get("query_timeout") or False
                )
                external_seed_broad_scope_rows = len(stage_b_rows)
                if stage_b_rows:
                    seed_rows = stage_b_rows
                    external_seed_rows_fetched = len(seed_rows)
                    external_seed_brand_relevant_rows = len(seed_rows)

            ranked_seed_candidates = rank_external_seed_rows(
                seed_rows,
                query=q_ascii or q_lower,
                limit=seed_limit,
            )
            external_seed_ranked_count = len(ranked_seed_candidates)

        seen_external_ids: set[str] = set()
        external_redirect_cache: Dict[str, Optional[str]] = {}
        seed_budget_ms = int(FIND_PRODUCTS_MULTI_SEED_BUDGET_MS or 0)
        seed_build_deadline = (
            time.perf_counter() + (seed_budget_ms / 1000.0)
            if seed_budget_ms > 0
            else None
        )
        shopping_seed_target = max(1, limit * max(page, 1))
        for candidate in ranked_seed_candidates:
            if seed_build_deadline is not None and time.perf_counter() >= seed_build_deadline:
                break
            row_dict = dict(candidate.row or {})
            seed_data = dict(candidate.seed_data or {})
            dest = candidate.destination_url or row_dict.get("destination_url") or seed_data.get("destination_url")
            if not isinstance(dest, str) or not dest.startswith(("http://", "https://")):
                continue

            canonical_url = candidate.canonical_url or row_dict.get("canonical_url") or seed_data.get("canonical_url") or dest
            external_id = candidate.external_product_id or seed_data.get("external_product_id") or _stable_external_product_id(canonical_url or dest)
            if not external_id or external_id in seen_external_ids:
                continue

            price_amount = candidate.price_amount if candidate.price_amount is not None else row_dict.get("price_amount") or seed_data.get("price_amount")
            price_currency = _observed_currency(
                candidate.price_currency,
                row_dict.get("price_currency"),
                seed_data.get("price_currency"),
            )
            budget_allowed, budget_diagnostics = await _budget_allows_price(
                price_amount=price_amount,
                price_currency=price_currency,
                budget_currency=budget_currency,
                price_min=effective_price_min,
                price_max=effective_price_max,
            )
            if not budget_allowed:
                if budget_diagnostics.get("budget_fx_unresolved"):
                    budget_fx_unresolved = True
                    budget_fx_candidate_currency = str(
                        budget_diagnostics.get("budget_candidate_currency") or budget_fx_candidate_currency or ""
                    ).upper() or budget_fx_candidate_currency
                continue
            if budget_diagnostics.get("budget_fx_applied"):
                budget_fx_applied = True
                budget_fx_rate = _coerce_float(budget_diagnostics.get("budget_fx_rate"))
                budget_fx_source = str(budget_diagnostics.get("budget_fx_source") or "").strip() or budget_fx_source
                budget_fx_candidate_currency = str(
                    budget_diagnostics.get("budget_candidate_currency") or budget_fx_candidate_currency or ""
                ).upper() or budget_fx_candidate_currency

            availability = candidate.availability or row_dict.get("availability") or seed_data.get("availability") or "unknown"
            if filters.in_stock_only and isinstance(availability, str):
                if availability.lower() in {"out_of_stock", "outofstock", "sold_out"}:
                    continue

            market = str(row_dict.get("market") or "US")
            tool = str(row_dict.get("tool") or "*")
            utm_template = row_dict.get("utm_template") or seed_data.get("utm_template")
            redirect_identity = _external_seed_redirect_identity(
                row=row_dict,
                seed_data=seed_data,
                offer_variant_id=getattr(candidate, "variant_id", None),
            )
            # ADR-009 D3: include seller_ref/seed_kind in the cache key so a cache
            # hit never reuses a redirect built for a different seller.
            redirect_cache_key = "||".join(
                [
                    market,
                    tool,
                    str(dest),
                    str(utm_template or ""),
                    str(redirect_identity.get("seller_ref") or ""),
                    str(redirect_identity.get("seed_kind") or ""),
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
                    merchant_id=redirect_identity["merchant_id"],
                    product_id=redirect_identity["product_id"],
                    variant_id=redirect_identity["variant_id"],
                    cart_variant_id=redirect_identity.get("cart_variant_id"),
                    shop_domain=redirect_identity["shop_domain"],
                    platform=redirect_identity["platform"],
                    seller_ref=redirect_identity["seller_ref"],
                    seed_kind=redirect_identity["seed_kind"],
                )
                external_redirect_cache[redirect_cache_key] = redirect_url
            if not redirect_url:
                continue

            product = _external_seed_to_shop_product(
                row=row_dict,
                seed_data=seed_data,
                redirect_url=redirect_url,
            )
            product["visible_attributes"] = dict(candidate.filter_product.visible_attributes or {})
            product["ingredient_ids"] = list(candidate.filter_product.ingredient_ids or [])
            product["candidate_source"] = "external_seed"
            product["ranking_audit_version"] = BEAUTY_EXTERNAL_RANKING_AUDIT_VERSION
            product["ranking_features"] = dict(candidate.ranking_features or {})
            product["ranking_score_breakdown"] = dict(candidate.ranking_score_breakdown or {})
            filter_product = candidate.filter_product
            external_seed_wrappers.append(
                {
                    "product": product,
                    "filter_product": filter_product,
                    "merchant_name": product.get("merchant_name"),
                    "relevance_score": candidate.candidate_score,
                    "candidate_score": candidate.candidate_score,
                    "source_boost": candidate.source_boost,
                    "quality_penalties": dict(
                        (candidate.ranking_score_breakdown or {}).get("quality_penalties") or {}
                    ),
                    "quality_penalties_total": candidate.quality_penalties_total,
                    "price_tie_break": candidate.price_amount,
                    "source_order": candidate.source_order,
                    "candidate_source": "external_seed",
                    "ranking_audit_version": BEAUTY_EXTERNAL_RANKING_AUDIT_VERSION,
                    "ranking_features": dict(candidate.ranking_features or {}),
                    "ranking_score_breakdown": dict(candidate.ranking_score_breakdown or {}),
                }
            )
            seen_external_ids.add(external_id)
            if is_shopping_surface and len(external_seed_wrappers) >= shopping_seed_target:
                break
    except Exception as e:
        logger.info("multi.external_seeds.failed", extra={"error": str(e)})

    try:
        prefetched_external_seed_wrappers = []
        if semantic_external_seed_fallback_allowed:
            prefetched_external_seed_wrappers = await _build_prefetched_external_seed_wrappers(
                request_metadata
            )
        elif external_seed_skip_reason is None:
            external_seed_skip_reason = "semantic_class_blocked"
        if prefetched_external_seed_wrappers:
            existing_external_product_ids = {
                str(
                    (
                        wrapper.get("product") or {}
                    ).get("product_id")
                    or (
                        wrapper.get("product") or {}
                    ).get("id")
                    or ""
                ).strip()
                for wrapper in external_seed_wrappers
                if isinstance(wrapper, dict)
            }
            for wrapper in prefetched_external_seed_wrappers:
                wrapper_payload = dict(wrapper) if isinstance(wrapper, dict) else {}
                wrapper_payload.setdefault("candidate_source", "external_seed")
                product_payload = wrapper_payload.get("product") if isinstance(wrapper_payload, dict) else {}
                product_id = str(
                    (product_payload or {}).get("product_id")
                    or (product_payload or {}).get("id")
                    or ""
                ).strip()
                if product_id and product_id in existing_external_product_ids:
                    continue
                if product_id:
                    existing_external_product_ids.add(product_id)
                external_seed_wrappers.append(wrapper_payload)
    except Exception as e:
        logger.info("multi.external_seeds.prefetch.failed", extra={"error": str(e)})

    if not has_merchants and not external_seed_wrappers:
        early_strict_constraint_reason = _resolve_strict_constraint_reason(
            strict_serving_mode=strict_serving_mode,
            ingredient_labels=active_ingredient_labels,
            visible_option_labels=active_visible_option_labels,
            visible_attribute_labels=active_visible_attribute_labels,
            price_min=effective_price_min,
            price_max=effective_price_max,
        )
        return _maybe_attach_eval_debug(
            {
                "products": [],
                "total": 0,
                "page": page,
                "page_size": 0,
                "metadata": {
                    "query_source": "cache_multi",
                    "query_semantic_class": query_semantic_class,
                    "visible_category_intents": active_visible_category_labels,
                    "visible_attribute_intents": active_visible_attribute_labels,
                    "visible_option_intents": active_visible_option_labels,
                    "ingredient_intents": active_ingredient_labels,
                    "matched_visible_categories": [],
                    "matched_visible_attribute_labels": [],
                    "matched_visible_option_labels": [],
                    "matched_ingredient_ids": [],
                    "matched_ingredient_labels": [],
                    "matched_visible_attributes": {},
                    "budget_price_min": effective_price_min,
                    "budget_price_max": effective_price_max,
                    "budget_currency": budget_currency,
                    "budget_fx_applied": budget_fx_applied,
                    "budget_fx_rate": budget_fx_rate,
                    "budget_fx_source": budget_fx_source,
                    "budget_fx_candidate_currency": budget_fx_candidate_currency,
                    "budget_fx_unresolved": budget_fx_unresolved,
                    "external_seed_executed": False,
                    "external_seed_skip_reason": external_seed_skip_reason,
                    "brand_query_detected": brand_query_detected,
                    "brand_query_terms": brand_query_terms,
                    "fetched_at": datetime.utcnow().isoformat(),
                    "merchants_searched": 0,
                    **(
                        {
                            "ingredient_precision_mode": "precision_first_v1",
                            "ingredient_precision_stage": "surface_anchor_gate",
                            "ingredient_candidate_breakdown": {
                                "eligible_total": 0,
                                "eligible_internal": 0,
                                "eligible_external_seed": 0,
                                "precision_passed_total": 0,
                                "precision_passed_internal": 0,
                                "precision_passed_external_seed": 0,
                            },
                            "ingredient_rejected_reason_summary": {},
                        }
                        if active_ingredient_intents
                        else {}
                    ),
                    **(
                        {
                            "source_breakdown": {
                                "internal_count": 0,
                                "external_seed_count": 0,
                                "strategy_applied": (
                                    "strict_ingredient_mixed_parity"
                                    if active_ingredient_intents and commerce_surface == "agent_api"
                                    else "strict_serving_mode"
                                ),
                            }
                        }
                        if strict_serving_mode
                        else {}
                    ),
                    **(
                        {
                            "commerce_surface": commerce_surface,
                            "serving_mode": "eligible_only",
                            "strict_constraint_query": bool(early_strict_constraint_reason),
                            "strict_constraint_reason": early_strict_constraint_reason,
                        }
                        if strict_serving_mode
                        else {}
                    ),
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
                mid, product_data = _products_cache_row_candidate(row)
                merchant_id = mid or None
                if product_data is None:
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

        if strict_serving_mode:
            mapped = _attach_eligible_serving_fields_to_items(
                mapped,
                commerce_surface=commerce_surface,
            )

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
                    **(
                        {
                            "commerce_surface": commerce_surface,
                            "serving_mode": "eligible_only",
                        }
                        if strict_serving_mode
                        else {}
                    ),
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
        if (
            strict_serving_mode
            or active_visible_category_intents
            or active_visible_attribute_intents
            or active_visible_option_intents
            or active_ingredient_intents
            or active_unsupported_beauty_category_labels
            or query_semantic_class == "beauty"
        ):
            merchant_ids_for_search = list(merchant_map.keys())
            expanded_shopping_beauty_prefetch = not strict_serving_mode
        else:
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
                    mid, product_data = _products_cache_row_candidate(row)
                    if not mid or product_data is None:
                        continue
                    try:
                        prod = StandardProduct(**product_data)
                        prod.merchant_id = prod.merchant_id or mid
                        _append_merchant_candidate(prod, merchant_map.get(mid, ""), mid)
                    except Exception:
                        continue
        except Exception as e:
            logger.warning(
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
    if merchant_map and (
        not is_shopping_surface
        or MULTI_SEARCH_SHOPPING_ENABLE_RECALL_BOOST
        or (non_strict_beauty_text_recall_enabled and q and not merchant_products)
    ):
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
                    mid, product_data = _products_cache_row_candidate(row)
                    if not mid or product_data is None:
                        continue
                    if not _recall_row_matches(product_data):
                        continue
                    try:
                        prod = StandardProduct(**product_data)
                        prod.merchant_id = prod.merchant_id or mid
                        _append_merchant_candidate(prod, merchant_map.get(mid, ""), mid)
                    except Exception:
                        continue
        except Exception as e:
            logger.warning(
                "multi.cache_query_boost.failed",
                extra={"query": q, "error": str(e)},
            )

    strict_live_query_fallback_used = False
    beauty_live_query_fallback_used = False
    if (
        strict_serving_mode
        and is_shopping_surface
        and q
        and merchant_map
        and not merchant_products
    ):
        strict_live_query_fallback_used = True
        fallback_merchant_items = list(merchant_map.items())
        merchants_scanned = max(merchants_scanned, len(fallback_merchant_items))
        semaphore = asyncio.Semaphore(max(1, MULTI_SEARCH_MERCHANT_CONCURRENCY))

        async def _fetch_strict_query_fallback_candidates(
            mid: str,
            name: str,
        ) -> list[tuple[StandardProduct, str, str]]:
            async with semaphore:
                try:
                    products, _source, _error = await asyncio.wait_for(
                        get_products_hybrid(
                            merchant_id=mid,
                            limit=per_merchant_limit,
                            agent_id="shopping_ai_multi_strict_query_fallback",
                            background_tasks=background_tasks,
                            force_cache_only=True,
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
            *[
                _fetch_strict_query_fallback_candidates(mid, name)
                for mid, name in fallback_merchant_items
            ],
            return_exceptions=True,
        )
        for chunk in gathered:
            if not isinstance(chunk, list):
                continue
            for prod, name, mid in chunk:
                _append_merchant_candidate(prod, name, mid)

    if (
        not strict_serving_mode
        and non_strict_beauty_text_recall_enabled
        and is_shopping_surface
        and q
        and merchant_map
        and not merchant_products
    ):
        beauty_live_query_fallback_used = True
        fallback_merchant_items = list(merchant_map.items())
        if max_merchants_to_scan > 0 and len(fallback_merchant_items) > max_merchants_to_scan:
            fallback_merchant_items = fallback_merchant_items[:max_merchants_to_scan]
        merchants_scanned = max(merchants_scanned, len(fallback_merchant_items))
        semaphore = asyncio.Semaphore(max(1, MULTI_SEARCH_MERCHANT_CONCURRENCY))

        async def _fetch_beauty_query_fallback_candidates(
            mid: str,
            name: str,
        ) -> list[tuple[StandardProduct, str, str]]:
            async with semaphore:
                try:
                    products, _source, _error = await asyncio.wait_for(
                        get_products_hybrid(
                            merchant_id=mid,
                            limit=per_merchant_limit,
                            agent_id="shopping_ai_multi_beauty_query_fallback",
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
            *[
                _fetch_beauty_query_fallback_candidates(mid, name)
                for mid, name in fallback_merchant_items
            ],
            return_exceptions=True,
        )
        for chunk in gathered:
            if not isinstance(chunk, list):
                continue
            for prod, name, mid in chunk:
                _append_merchant_candidate(prod, name, mid)

    if (
        strict_serving_mode
        and commerce_surface == "agent_api"
        and active_ingredient_intents
        and external_seed_wrappers
    ):
        for wrapper in external_seed_wrappers:
            filter_product = wrapper.get("filter_product")
            if not isinstance(filter_product, StandardProduct):
                continue
            filter_product_id = str(filter_product.product_id or filter_product.id or "").strip()
            if not filter_product_id:
                continue
            strict_external_output_by_product_id[filter_product_id] = dict(wrapper.get("product") or {})
            merchant_products.append(
                (
                    filter_product,
                    str(wrapper.get("merchant_name") or ""),
                )
            )

    # In-memory filtering and simple relevance scoring (reuse Agent API logic)
    filtered_products: list[dict[str, Any]] = []
    ingredient_candidate_breakdown = {
        "eligible_total": 0,
        "eligible_internal": 0,
        "eligible_external_seed": 0,
        "precision_passed_total": 0,
        "precision_passed_internal": 0,
        "precision_passed_external_seed": 0,
    }
    ingredient_rejected_reason_summary: Dict[str, int] = {}
    non_strict_beauty_text_recall_used = False
    generic_default_precision_filtered_count = 0
    beauty_pet_noise_filtered_count = 0
    beauty_apparel_noise_filtered_count = 0

    for product, merchant_name in merchant_products:
        if (
            strict_serving_mode
            and active_unsupported_beauty_category_labels
            and not (
                active_visible_category_intents or active_ingredient_intents
            )
        ):
            continue
        if (
            strict_serving_mode
            and requires_explicit_shade_query
            and not has_active_shade_option_intent
        ):
            continue
        # Visibility: only surface sellable products to the agent front-end.
        if not _is_product_sellable(product):
            continue

        # Price filter
        product_currency = str(product.currency or "").strip().upper()
        budget_allowed, budget_diagnostics = await _budget_allows_price(
            price_amount=product.price,
            price_currency=product_currency,
            budget_currency=budget_currency,
            price_min=effective_price_min,
            price_max=effective_price_max,
        )
        if not budget_allowed:
            if budget_diagnostics.get("budget_fx_unresolved"):
                budget_fx_unresolved = True
                budget_fx_candidate_currency = str(
                    budget_diagnostics.get("budget_candidate_currency") or budget_fx_candidate_currency or ""
                ).upper() or budget_fx_candidate_currency
            continue
        if budget_diagnostics.get("budget_fx_applied"):
            budget_fx_applied = True
            budget_fx_rate = _coerce_float(budget_diagnostics.get("budget_fx_rate"))
            budget_fx_source = str(budget_diagnostics.get("budget_fx_source") or "").strip() or budget_fx_source
            budget_fx_candidate_currency = str(
                budget_diagnostics.get("budget_candidate_currency") or budget_fx_candidate_currency or ""
            ).upper() or budget_fx_candidate_currency

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
        pet_accessory_markers = [
            "harness",
            "harnesses",
            "leash",
            "leashes",
            "collar",
            "collars",
            "lead",
            "leads",
        ]
        pet_subject_markers = [
            "dog",
            "dogs",
            "cat",
            "cats",
            "pet",
            "pets",
            "puppy",
            "puppies",
            "kitten",
            "kittens",
        ]
        beauty_apparel_noise_markers = [
            "sleepwear",
            "nightdress",
            "nightgown",
            "lingerie",
            "underwear",
            "bralette",
            "bra",
            "panties",
            "panty",
            "briefs",
            "thong",
            "deep v",
            "deep-v",
            "robe",
            "slip dress",
            "slipdress",
        ]
        pet_accessory_blob = " ".join(
            [
                (product.title or "").lower(),
                (product.product_type or "").lower(),
            ]
        ).strip()
        # title/type/tags ONLY (no description) — the precise blob for strict-surface
        # category/attribute text recall.
        title_type_tag_blob = " ".join(
            [
                (product.title or "").lower(),
                (product.product_type or "").lower(),
                " ".join(str(tag).lower() for tag in (getattr(product, "tags", None) or []) if tag),
            ]
        ).strip()
        if non_strict_beauty_text_recall_enabled:
            beauty_text_blob = blob_for_filters
        elif beauty_category_text_recall_enabled:
            beauty_text_blob = title_type_tag_blob
        else:
            beauty_text_blob = pet_accessory_blob
        visible_attribute_blob = title_type_tag_blob
        if non_strict_beauty_text_recall_enabled:
            visible_attribute_blob = beauty_text_blob
        product_visible_attributes = _normalize_product_visible_attributes(product)
        product_ingredient_ids = _normalize_product_ingredient_ids(product)
        structured_visible_option_labels = _collect_product_visible_option_labels(product)
        matched_visible_attributes: Dict[str, List[str]] = {}
        visible_option_blob = " ".join(
            [
                str(getattr(variant, "title", "") or "").lower()
                + " "
                + " ".join(
                    [
                        str(name).lower(),
                        str(value).lower(),
                    ]
                )
                for variant in (getattr(product, "variants", None) or [])
                for name, value in ((getattr(variant, "options", None) or {}) or {}).items()
            ]
        ).strip()
        visible_category_blob = pet_accessory_blob
        has_pet_accessory_marker = any(tok in pet_accessory_blob for tok in pet_accessory_markers)
        has_pet_subject_marker = any(tok in pet_accessory_blob for tok in pet_subject_markers)

        if pet_accessory_intent_query and not has_pet_accessory_marker:
            continue
        if query_semantic_class == "beauty" and not pet_accessory_intent_query and has_pet_subject_marker:
            beauty_pet_noise_filtered_count += 1
            continue
        if (
            query_semantic_class == "beauty"
            and any(tok in blob_for_filters_ascii for tok in beauty_apparel_noise_markers)
        ):
            beauty_apparel_noise_filtered_count += 1
            continue

        matched_visible_category_labels = []
        for group in active_visible_category_intents:
            label = str(group["label"])
            visible_attribute_bucket = str(group.get("visible_attribute_bucket") or "").strip()
            structured_match = False
            matched = False
            if visible_attribute_bucket:
                structured_match = _product_visible_attribute_label_matches(
                    product_visible_attributes,
                    bucket=visible_attribute_bucket,
                    label=label,
                )
                matched = structured_match
                if structured_match:
                    _record_matched_visible_attribute(
                        matched_visible_attributes,
                        bucket=visible_attribute_bucket,
                        label=label,
                    )
                if (
                    not matched
                    and (non_strict_beauty_text_recall_enabled or beauty_category_text_recall_enabled)
                ):
                    matched = _normalized_intent_terms_match(beauty_text_blob, list(group["product_terms"]))
            elif not matched:
                matched = _normalized_intent_terms_match(pet_accessory_blob, list(group["product_terms"]))
            if matched:
                if matched and not structured_match and (non_strict_beauty_text_recall_enabled or beauty_category_text_recall_enabled):
                    non_strict_beauty_text_recall_used = True
                matched_visible_category_labels.append(label)
        if active_visible_category_intents and not matched_visible_category_labels:
            continue
        matched_visible_attribute_labels = []
        for group in active_visible_attribute_intents:
            label = str(group["label"])
            visible_attribute_bucket = str(group.get("visible_attribute_bucket") or "").strip()
            structured_match = False
            matched = False
            if visible_attribute_bucket:
                structured_match = _product_visible_attribute_label_matches(
                    product_visible_attributes,
                    bucket=visible_attribute_bucket,
                    label=label,
                )
                matched = structured_match
                if structured_match:
                    _record_matched_visible_attribute(
                        matched_visible_attributes,
                        bucket=visible_attribute_bucket,
                        label=label,
                    )
                if (
                    not matched
                    and (non_strict_beauty_text_recall_enabled or beauty_category_text_recall_enabled)
                ):
                    matched = _normalized_intent_terms_match(visible_attribute_blob, list(group["product_terms"]))
            elif not matched:
                matched = _normalized_intent_terms_match(visible_attribute_blob, list(group["product_terms"]))
            if matched:
                if matched and not structured_match and (non_strict_beauty_text_recall_enabled or beauty_category_text_recall_enabled):
                    non_strict_beauty_text_recall_used = True
                matched_visible_attribute_labels.append(label)
        if active_visible_attribute_intents and (
            len(matched_visible_attribute_labels) < len(active_visible_attribute_intents)
        ):
            continue
        matched_visible_option_labels = []
        for group in active_visible_option_intents:
            label = str(group["label"])
            structured_match = label in structured_visible_option_labels
            fallback_match = False
            if not structured_match and not bool(group.get("structured_only")):
                fallback_match = _normalized_intent_terms_match(visible_option_blob, list(group["product_terms"]))
            if structured_match or fallback_match:
                matched_visible_option_labels.append(label)
        if active_visible_option_intents and len(matched_visible_option_labels) < len(active_visible_option_intents):
            continue
        matched_ingredient_ids = []
        matched_ingredient_labels = []
        candidate_source = (
            "external_seed"
            if str(getattr(product, "product_id", None) or getattr(product, "id", None) or "").strip()
            in strict_external_output_by_product_id
            else "internal"
        )
        if active_ingredient_intents:
            product_skin_care_categories = {
                label
                for label in product_visible_attributes.get("product_category", [])
                if label in _SKINCARE_INGREDIENT_CATEGORY_LABELS
            }
            category_anchor_blob = (
                blob_for_filters
                if (non_strict_beauty_text_recall_enabled or beauty_ingredient_text_recall_enabled)
                else pet_accessory_blob
            )
            if not product_skin_care_categories:
                for label in _SKINCARE_INGREDIENT_CATEGORY_LABELS:
                    if _normalized_intent_term_match(category_anchor_blob, label):
                        product_skin_care_categories.add(label)
            if not product_skin_care_categories:
                continue
            for group in active_ingredient_intents:
                ingredient_id = str(group.get("ingredient_id") or "").strip()
                if not ingredient_id:
                    continue
                matched = ingredient_id in product_ingredient_ids
                if (
                    not matched
                    and beauty_ingredient_text_recall_enabled
                    and _ingredient_alias_matches_text(blob_for_filters, ingredient_id)
                ):
                    matched = True
                    non_strict_beauty_text_recall_used = True
                if matched:
                    matched_ingredient_ids.append(ingredient_id)
                    matched_ingredient_labels.append(str(group.get("display_name") or ingredient_id))
            if len(matched_ingredient_ids) < len(active_ingredient_intents):
                continue
            ingredient_candidate_breakdown["eligible_total"] += 1
            ingredient_candidate_breakdown[
                "eligible_external_seed" if candidate_source == "external_seed" else "eligible_internal"
            ] += 1
            if strict_serving_mode:
                precision_eval = _evaluate_strict_ingredient_candidate_precision(
                    product,
                    product_visible_attributes=product_visible_attributes,
                    active_ingredient_intents=active_ingredient_intents,
                    candidate_source=candidate_source,
                )
                if not precision_eval.get("passed"):
                    rejected_reason = str(
                        (precision_eval.get("summary") or {}).get("rejected_reason") or "precision_rejected"
                    ).strip() or "precision_rejected"
                    ingredient_rejected_reason_summary[rejected_reason] = (
                        ingredient_rejected_reason_summary.get(rejected_reason, 0) + 1
                    )
                    continue
            ingredient_candidate_breakdown["precision_passed_total"] += 1
            ingredient_candidate_breakdown[
                "precision_passed_external_seed"
                if candidate_source == "external_seed"
                else "precision_passed_internal"
            ] += 1

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

        if generic_default_precision_gate_enabled:
            precision_eval = _evaluate_generic_default_precision_gate(
                query=q_ascii,
                product=product,
            )
            if precision_eval.get("applied") and not precision_eval.get("passed"):
                generic_default_precision_filtered_count += 1
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

        if pet_accessory_intent_query and has_pet_accessory_marker:
            relevance_score += 0.45
            if pet_subject_intent_query and has_pet_subject_marker:
                relevance_score += 0.15

        if matched_visible_category_labels:
            relevance_score += 0.35
        if matched_visible_attribute_labels:
            relevance_score += min(0.3, 0.12 * len(matched_visible_attribute_labels))
        if matched_visible_option_labels:
            relevance_score += min(0.2, 0.1 * len(matched_visible_option_labels))
        if matched_ingredient_ids:
            relevance_score += min(0.25, 0.12 * len(matched_ingredient_ids))

        filtered_products.append(
            {
                "product": product,
                "merchant_name": merchant_name,
                "relevance_score": relevance_score,
                "candidate_score": relevance_score,
                "candidate_source": candidate_source,
                "source_boost": 0.08 if candidate_source != "external_seed" else 0.0,
                "quality_penalties": {},
                "quality_penalties_total": 0.0,
                "price_tie_break": getattr(product, "price", None),
                "is_toy_like": is_toy_like if toys_intent_query else False,
                "matched_visible_category_labels": list(matched_visible_category_labels),
                "matched_visible_attribute_labels": list(matched_visible_attribute_labels),
                "matched_visible_option_labels": list(matched_visible_option_labels),
                "matched_ingredient_ids": list(matched_ingredient_ids),
                "matched_ingredient_labels": list(matched_ingredient_labels),
                "matched_visible_attributes": matched_visible_attributes,
            }
        )

    if external_seed_wrappers and not strict_serving_mode:
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

    def _candidate_sort_key(item: Dict[str, Any]) -> tuple[float, int, float]:
        try:
            candidate_score = float(
                item.get("candidate_score")
                if item.get("candidate_score") is not None
                else item.get("relevance_score") or 0.0
            )
        except Exception:
            candidate_score = 0.0
        try:
            source_boost = float(item.get("source_boost") or 0.0)
        except Exception:
            source_boost = 0.0
        raw_source_order = item.get("source_order")
        try:
            source_order = int(raw_source_order) if raw_source_order is not None else 999999
        except Exception:
            source_order = 999999
        price_tie_break = item.get("price_tie_break")
        try:
            normalized_price = float(price_tie_break) if price_tie_break is not None else 999999.0
        except Exception:
            normalized_price = 999999.0
        return (-(candidate_score + source_boost), source_order, normalized_price)

    # Sort by canonical ranking contract.
    filtered_products.sort(
        key=_candidate_sort_key
    )

    external_filtered_count = sum(
        1 for item in filtered_products if item.get("candidate_source") == "external_seed"
    )
    internal_filtered_count = max(0, len(filtered_products) - external_filtered_count)
    total = len(filtered_products)
    start_idx = (page - 1) * limit
    end_idx = start_idx + limit
    page_items = filtered_products[start_idx:end_idx]

    # Map to Shopping contract; inject merchant_id into result
    out_products = []
    matched_visible_category_summary: List[str] = []
    matched_visible_attribute_summary: List[str] = []
    matched_visible_option_summary: List[str] = []
    matched_ingredient_id_summary: List[str] = []
    matched_ingredient_label_summary: List[str] = []
    matched_visible_attributes_summary: Dict[str, List[str]] = {}
    strict_source_breakdown = {
        "internal_count": sum(1 for item in filtered_products if item.get("candidate_source") != "external_seed"),
        "external_seed_count": sum(1 for item in filtered_products if item.get("candidate_source") == "external_seed"),
    }
    for item_wrapper in page_items:
        product_item = item_wrapper.get("product")
        merchant_name = item_wrapper.get("merchant_name")
        strict_external_output = None

        if isinstance(product_item, StandardProduct):
            strict_external_output = strict_external_output_by_product_id.get(
                str(product_item.product_id or product_item.id or "").strip()
            )
            if strict_external_output is not None:
                item = dict(strict_external_output)
            else:
                item = _standard_to_shop_product(product_item)
        elif isinstance(product_item, dict):
            item = dict(product_item)
        else:
            continue

        if strict_serving_mode:
            if item.get("source") == "external_seed":
                item = dict(item)
                item["commerce_surface"] = commerce_surface
            else:
                attached = _attach_eligible_serving_fields(
                    item,
                    product_item,
                    commerce_surface=commerce_surface,
                )
                if attached is None:
                    continue
                item = attached

        if merchant_name and not item.get("merchant_name"):
            item["merchant_name"] = merchant_name
        for label in item_wrapper.get("matched_visible_category_labels") or []:
            label_text = str(label or "").strip()
            if label_text and label_text not in matched_visible_category_summary:
                matched_visible_category_summary.append(label_text)
        for label in item_wrapper.get("matched_visible_attribute_labels") or []:
            label_text = str(label or "").strip()
            if label_text and label_text not in matched_visible_attribute_summary:
                matched_visible_attribute_summary.append(label_text)
        for label in item_wrapper.get("matched_visible_option_labels") or []:
            label_text = str(label or "").strip()
            if label_text and label_text not in matched_visible_option_summary:
                matched_visible_option_summary.append(label_text)
        for label in item_wrapper.get("matched_ingredient_ids") or []:
            label_text = str(label or "").strip()
            if label_text and label_text not in matched_ingredient_id_summary:
                matched_ingredient_id_summary.append(label_text)
        for label in item_wrapper.get("matched_ingredient_labels") or []:
            label_text = str(label or "").strip()
            if label_text and label_text not in matched_ingredient_label_summary:
                matched_ingredient_label_summary.append(label_text)
        wrapper_matched_visible_attributes = item_wrapper.get("matched_visible_attributes")
        if isinstance(wrapper_matched_visible_attributes, dict):
            for bucket, labels in wrapper_matched_visible_attributes.items():
                bucket_name = str(bucket or "").strip()
                if not bucket_name:
                    continue
                summary_labels = matched_visible_attributes_summary.setdefault(bucket_name, [])
                for label in labels if isinstance(labels, list) else [labels]:
                    label_text = str(label or "").strip()
                    if label_text and label_text not in summary_labels:
                        summary_labels.append(label_text)
        out_products.append(item)

    if strict_serving_mode:
        total = len(out_products)

    semantic_retry_applied = False
    semantic_retry_query: Optional[str] = None
    semantic_retry_hits = 0

    # Fallback: if primary query returned nothing, surface top-sellers instead
    # - For general queries: only when creator_id is present (as before)
    # - For tee intent queries: also allow a global tee-only fallback so we don't
    #   respond with an empty list for strong tee intent (e.g. Spanish camisetas).
    if not out_products:
        if should_try_upstream and not upstream_fallback_attempted:
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
                if strict_serving_mode:
                    mapped = _attach_eligible_serving_fields_to_items(
                        mapped,
                        commerce_surface=commerce_surface,
                    )
                    fallback_items = mapped[start_idx:end_idx]
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
                            **(
                                {
                                    "commerce_surface": commerce_surface,
                                    "serving_mode": "eligible_only",
                                }
                                if strict_serving_mode
                                else {}
                            ),
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
                if strict_serving_mode:
                    mapped = _attach_eligible_serving_fields_to_items(
                        mapped,
                        commerce_surface=commerce_surface,
                    )
                    fallback_items = mapped[start_idx:end_idx]
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
                            **(
                                {
                                    "commerce_surface": commerce_surface,
                                    "serving_mode": "eligible_only",
                                }
                                if strict_serving_mode
                                else {}
                            ),
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
                    price_min=effective_price_min,
                    price_max=effective_price_max,
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
                # internal semantic-retry slate — not the served result; the
                # outer wrapper records the one event for what's actually served.
                emit_decision_event=False,
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
    if not out_products and pet_accessory_intent_query:
        reply_text = reply_text or (
            "I couldn’t find an eligible pet accessory match for that query right now. "
            "I’m only showing products that are currently purchasable."
        )
    if (
        not out_products
        and strict_serving_mode
        and active_unsupported_beauty_category_labels
        and not (active_visible_category_intents or active_ingredient_intents)
    ):
        reply_text = reply_text or (
            f"I couldn’t find an eligible {active_unsupported_beauty_category_labels[0]} match for that query right now. "
            "I’m only showing products whose visible catalog labels support the requested beauty category."
        )
    if (
        not out_products
        and strict_serving_mode
        and requires_explicit_shade_query
        and not has_active_shade_option_intent
    ):
        descriptor = cosmetic_shade_category_intents[0] if cosmetic_shade_category_intents else "cosmetic product"
        reply_text = reply_text or (
            f"I couldn’t find an eligible {descriptor} shade match for that query right now. "
            "I’m only showing cosmetic products when an explicit purchasable shade is available."
        )
    if not out_products and active_ingredient_intents:
        descriptor = active_visible_category_labels[0] if active_visible_category_labels else "skin-care product"
        reply_text = reply_text or (
            f"I couldn’t find an eligible {descriptor} match with those reviewed ingredients right now. "
            "I’m only showing products whose structured ingredient evidence supports the requested constraints."
        )
    if not out_products and active_visible_attribute_labels and active_visible_option_labels:
        descriptor = active_visible_category_labels[0] if active_visible_category_labels else "product"
        reply_text = reply_text or (
            f"I couldn’t find an eligible {descriptor} match with those visible attributes and options right now. "
            "I’m only showing products whose visible catalog labels and variant options support the requested constraints."
        )
    if not out_products and active_visible_attribute_labels:
        attribute_descriptor = active_visible_category_labels[0] if active_visible_category_labels else "product"
        reply_text = reply_text or (
            f"I couldn’t find an eligible {attribute_descriptor} match with those visible attributes right now. "
            "I’m only showing products whose visible catalog labels support the requested attributes."
        )
    if not out_products and active_visible_option_labels:
        option_descriptor = active_visible_category_labels[0] if active_visible_category_labels else "product"
        reply_text = reply_text or (
            f"I couldn’t find an eligible {option_descriptor} match with those visible options right now. "
            "I’m only showing products whose variant options support the requested constraints."
        )
    if not out_products and active_visible_category_labels:
        reply_text = reply_text or (
            f"I couldn’t find an eligible {active_visible_category_labels[0]} match for that query right now. "
            "I’m only showing products that are currently purchasable."
        )

    history_used = bool(history_product_ids or history_terms)
    strict_constraint_reason = _resolve_strict_constraint_reason(
        strict_serving_mode=strict_serving_mode,
        ingredient_labels=active_ingredient_labels,
        visible_option_labels=active_visible_option_labels,
        visible_attribute_labels=active_visible_attribute_labels,
        price_min=effective_price_min,
        price_max=effective_price_max,
    )
    strict_constraint_query = bool(strict_constraint_reason)

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
                "pet_accessory_intent_query": pet_accessory_intent_query,
                "visible_category_intents": active_visible_category_labels,
                "visible_attribute_intents": active_visible_attribute_labels,
                "visible_option_intents": active_visible_option_labels,
                "ingredient_intents": active_ingredient_labels,
                "unsupported_beauty_category_intents": active_unsupported_beauty_category_labels,
                "matched_visible_categories": matched_visible_category_summary,
                "matched_visible_attribute_labels": matched_visible_attribute_summary,
                "matched_visible_option_labels": matched_visible_option_summary,
                "matched_ingredient_ids": matched_ingredient_id_summary,
                "matched_ingredient_labels": matched_ingredient_label_summary,
                "matched_visible_attributes": matched_visible_attributes_summary,
                "budget_price_min": effective_price_min,
                "budget_price_max": effective_price_max,
                "budget_currency": budget_currency,
                "budget_fx_applied": budget_fx_applied,
                "budget_fx_rate": budget_fx_rate,
                "budget_fx_source": budget_fx_source,
                "budget_fx_candidate_currency": budget_fx_candidate_currency,
                "budget_fx_unresolved": budget_fx_unresolved,
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
                "upstream_fallback_attempted": bool(upstream_fallback_attempted),
                "shopping_fast_prefetch_used": bool(is_shopping_surface and q and merchant_ids_for_search),
                "shopping_recall_boost_enabled": bool(
                    is_shopping_surface and MULTI_SEARCH_SHOPPING_ENABLE_RECALL_BOOST
                ),
                "non_strict_beauty_text_recall_enabled": non_strict_beauty_text_recall_enabled,
                "non_strict_beauty_text_recall_used": non_strict_beauty_text_recall_used,
                "expanded_shopping_beauty_prefetch": expanded_shopping_beauty_prefetch,
                "beauty_live_query_fallback_used": beauty_live_query_fallback_used,
                "strict_live_query_fallback_used": strict_live_query_fallback_used,
                "generic_default_precision_gate_enabled": generic_default_precision_gate_enabled,
                "generic_default_precision_filtered_count": generic_default_precision_filtered_count,
                "beauty_pet_noise_filtered_count": beauty_pet_noise_filtered_count,
                "beauty_apparel_noise_filtered_count": beauty_apparel_noise_filtered_count,
                "external_seed_query_timeout": external_seed_query_timeout,
                "external_seed_rows_fetched": external_seed_rows_fetched,
                "external_seed_ranked_count": external_seed_ranked_count,
                "external_seed_brand_strict_rows": external_seed_brand_strict_rows,
                "external_seed_brand_relevant_rows": external_seed_brand_relevant_rows,
                "external_seed_broad_fallback_used": external_seed_broad_fallback_used,
                "external_seed_broad_scope_rows": external_seed_broad_scope_rows,
                "external_seed_executed": bool(external_seed_rows_fetched or external_filtered_count),
                "external_seed_skip_reason": external_seed_skip_reason,
                "brand_query_detected": brand_query_detected,
                "brand_query_terms": brand_query_terms,
                "external_seed_cache_hit": False,
                "external_seed_rows_built": len(external_seed_wrappers),
                "external_seed_returned_count": external_filtered_count,
                "ranking_audit_version": (
                    BEAUTY_EXTERNAL_RANKING_AUDIT_VERSION
                    if external_seed_wrappers or external_seed_rows_fetched
                    else None
                ),
                "internal_raw_count": internal_filtered_count,
                "external_raw_count": external_filtered_count,
                "merged_pre_limit_count": total,
                "primary_quality_gate_passed": bool(total > 0),
                "primary_quality_score": None,
                "low_quality_nonempty_detected": False,
                "supplement_attempted": False,
                "supplement_skip_reason": None,
                "retry_attempt_count": 0,
                "fallback_attempt_count": 0,
                "selected_fallback_attempt": 0,
                "final_returned_count": len(out_products),
                "route_health": {
                    "external_seed_executed": bool(external_seed_rows_fetched or external_filtered_count),
                    "external_seed_skip_reason": external_seed_skip_reason,
                    "brand_query_detected": brand_query_detected,
                    "brand_query_terms": brand_query_terms,
                    "external_seed_cache_hit": False,
                    "external_seed_query_timeout": external_seed_query_timeout,
                    "external_seed_rows_fetched": external_seed_rows_fetched,
                    "external_seed_ranked_count": external_seed_ranked_count,
                    "external_seed_rows_built": len(external_seed_wrappers),
                    "external_seed_brand_strict_rows": external_seed_brand_strict_rows,
                    "external_seed_brand_relevant_rows": external_seed_brand_relevant_rows,
                    "external_seed_broad_fallback_used": external_seed_broad_fallback_used,
                    "external_seed_broad_scope_rows": external_seed_broad_scope_rows,
                    "ranking_audit_version": (
                        BEAUTY_EXTERNAL_RANKING_AUDIT_VERSION
                        if external_seed_wrappers or external_seed_rows_fetched
                        else None
                    ),
                    "internal_raw_count": internal_filtered_count,
                    "external_raw_count": external_filtered_count,
                    "merged_pre_limit_count": total,
                    "primary_quality_gate_passed": bool(total > 0),
                    "primary_quality_score": None,
                    "low_quality_nonempty_detected": False,
                    "supplement_attempted": False,
                    "supplement_skip_reason": None,
                    "retry_attempt_count": 0,
                    "fallback_attempt_count": 0,
                    "selected_fallback_attempt": 0,
                    "final_returned_count": len(out_products),
                },
                "source_breakdown": {
                    "internal_count": internal_filtered_count,
                    "external_seed_count": external_filtered_count,
                    "strategy_applied": (
                        "strict_ingredient_mixed_parity"
                        if active_ingredient_intents and commerce_surface == "agent_api"
                        else "strict_serving_mode"
                        if strict_serving_mode
                        else "cache_multi_intent"
                    ),
                },
                **(
                    {
                        "ingredient_precision_mode": "precision_first_v1",
                        "ingredient_precision_stage": "surface_anchor_gate",
                        "ingredient_candidate_breakdown": ingredient_candidate_breakdown,
                        "ingredient_rejected_reason_summary": ingredient_rejected_reason_summary,
                    }
                    if active_ingredient_intents
                    else {}
                ),
                "shopping_sku_json_scan_enabled": bool(
                    is_shopping_surface and MULTI_SEARCH_SHOPPING_ENABLE_SKU_JSON_SCAN
                ),
                **(
                    {
                        "commerce_surface": commerce_surface,
                        "serving_mode": "eligible_only",
                        "strict_constraint_query": strict_constraint_query,
                        "strict_constraint_reason": strict_constraint_reason,
                    }
                    if strict_serving_mode
                    else {}
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
        "metadata": { "creator_id": "creator_456", "source": "creator_agent" }
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
        _normalize_surface_source(source) == "creator-agent"
        and base_is_toy_context
        and not base_is_underwear_like
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
                # find_similar building block — the served slate is the similar
                # result, not this internal multi-search; don't record it.
                emit_decision_event=False,
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
                    emit_decision_event=False,  # internal fallback slate
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
    normalized_metadata = _normalize_gateway_request_metadata(
        metadata=request.metadata,
        payload=request.payload,
    )
    try:
        http_request.state.traffic_taxonomy = dict(normalized_metadata.get("traffic") or {})
    except Exception:
        pass
    if isinstance(normalized_metadata.get("traffic"), dict):
        record_traffic_taxonomy(stage="gateway_request", taxonomy=normalized_metadata["traffic"])

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
    try:
        await enrich_product_detail_with_payment_offers(
            base,
            merchant_id=merchant_id,
            payment_context=None,
            market=None,
        )
    except Exception:
        pass

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

    # A-F1.2 (funnel plan): attribute PDP click-outs. The detail card previously
    # emitted no /r link, so a buyer landing on a product-detail card had no
    # attributed path to checkout. Reuse the exact post-pass find_products uses
    # (in place on the single-card list; fail-soft; external/pre-stamped cards
    # skipped, connected cards with a derivable destination get cart_permalink
    # or referral_only).
    await _attach_connected_product_redirects([base], tool="get_product_detail")

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


async def _handle_create_order(
    order: OrderPayloadBody,
    *,
    checkout_token: Optional[str],
    request_metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Proxy create_order to Agent API (/agent/v1/orders/create)."""
    order_metadata = dict(order.metadata or {})
    request_taxonomy = build_traffic_taxonomy(
        request_metadata or {},
        metadata=order_metadata,
        default_source_channel=str(
            order_metadata.get("source")
            or (request_metadata or {}).get("source")
            or ""
        ).strip()
        or None,
        default_query_source=str(
            order_metadata.get("query_source")
            or (request_metadata or {}).get("query_source")
            or ""
        ).strip()
        or None,
        default_protocol_name=str(
            order_metadata.get("protocol_name")
            or (request_metadata or {}).get("protocol_name")
            or "rest"
        ).strip()
        or "rest",
        default_commerce_surface=str(
            order_metadata.get("commerce_surface")
            or (request_metadata or {}).get("commerce_surface")
            or "agent_api"
        ).strip()
        or "agent_api",
    )
    order_metadata = attach_traffic_taxonomy(order_metadata, request_taxonomy)
    body = {
        "merchant_id": order.merchant_id,
        "customer_email": order.customer_email,
        **({"currency": order.currency} if order.currency else {}),
        **({"offer_id": order.offer_id} if order.offer_id else {}),
        **({"preferred_psp": order.preferred_psp} if order.preferred_psp else {}),
        **({"quote_id": order.quote_id} if order.quote_id else {}),
        **({"discount_codes": order.discount_codes} if isinstance(order.discount_codes, list) else {}),
        **({"selected_delivery_option": order.selected_delivery_option} if isinstance(order.selected_delivery_option, dict) else {}),
        **({"metadata": order_metadata} if order_metadata else {}),
        **({"selected_payment_offer_id": order.selected_payment_offer_id} if order.selected_payment_offer_id else {}),
        **({"payment_method_evidence": order.payment_method_evidence} if isinstance(order.payment_method_evidence, dict) else {}),
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


async def _handle_create_payment_link(
    payload: "CreatePaymentLinkPayload",
    *,
    checkout_token: Optional[str],
) -> Dict[str, Any]:
    """Proxy create_payment_link to the Agent v2 hosted-checkout endpoint.

    POST /agent/v2/payments/checkout-sessions turns an EXISTING order (created moments
    earlier by the kernel's create_order) into a HOSTED Stripe Checkout page. The buyer
    pays on that page and the PSP webhook finalizes the order — so this NEVER charges and
    needs no payment authorization. The response is the backend's
    `{ "checkout_session": { "hosted_url", ... } }` shape, which the kernel executor reads
    verbatim (hosted_url -> checkout_url). order ownership + state are enforced upstream.
    """
    body: Dict[str, Any] = {"order_id": payload.order_id}
    if payload.customer_email:
        body["customer_email"] = payload.customer_email
    if isinstance(payload.shipping_address, dict) and payload.shipping_address:
        body["shipping_address"] = payload.shipping_address
    if payload.return_url:
        body["return_url"] = payload.return_url
    if payload.user_ref:
        # The hosted-checkout endpoint records the guest buyer reference for the session.
        body["buyer_ref"] = payload.user_ref

    return await _proxy_agent_api(
        "POST",
        "/agent/v2/payments/checkout-sessions",
        body,
        checkout_token=checkout_token,
    )


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


async def _handle_record_payment_offer_evidence(
    payload: RecordPaymentOfferEvidencePayload,
    *,
    checkout_token: Optional[str],
) -> Dict[str, Any]:
    body = {
        **({"order_id": payload.order_id} if payload.order_id else {}),
        **({"quote_id": payload.quote_id} if payload.quote_id else {}),
        **({"merchant_id": payload.merchant_id} if payload.merchant_id else {}),
        **({"selected_payment_offer_id": payload.selected_payment_offer_id} if payload.selected_payment_offer_id else {}),
        "payment_method_evidence": payload.payment_method_evidence or {},
        **({"payment_offer_evidence": payload.payment_offer_evidence} if isinstance(payload.payment_offer_evidence, dict) else {}),
        "surface": payload.surface or "checkout",
        **({"event_type": payload.event_type} if payload.event_type else {}),
        **({"idempotency_key": payload.idempotency_key} if payload.idempotency_key else {}),
    }
    return await _proxy_agent_api("POST", "/agent/v1/orders/payment-offer-evidence", body, checkout_token=checkout_token)


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
    - create_payment_link (guest hosted checkout → /agent/v2/payments/checkout-sessions; no charge)
    - submit_payment     (demo-only)
    - find_similar_products
    """
    operation = (request.operation or "").strip()
    checkout_token = (http_request.headers.get("x-checkout-token") or "").strip() or None

    # Credential-less direct callers only — see the limiter's comment block.
    if not _request_carries_credential(http_request):
        if not _check_invoke_anon_rate_limit(_review_media_client_ip(http_request)):
            raise HTTPException(
                status_code=429,
                detail="Too many anonymous requests; slow down or authenticate with an API key.",
                headers={"Retry-After": "60"},
            )

    # Normalize metadata: allow creatorId/creatorName to be passed at payload top-level
    normalized_metadata = _normalize_gateway_request_metadata(
        metadata=request.metadata,
        payload=request.payload,
    )

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
        multi_request_metadata = dict(normalized_metadata)
        multi_request_metadata["_pivot_shadow_schedule_suppressed"] = True
        result = await _handle_find_products_multi(
            multi_payload,
            multi_request_metadata,
            background_tasks,
        )
        if isinstance(result, dict):
            response_metadata = result.get("metadata")
            if not isinstance(response_metadata, dict):
                response_metadata = {}
            pivot_shadow_scheduled = _maybe_schedule_pivot_multi_shadow_compare(
                payload=multi_payload,
                request_metadata=normalized_metadata,
                background_tasks=background_tasks,
                served_result=result,
                source_normalized=_normalize_surface_source(normalized_metadata.get("source")),
                page=multi_payload.search.page or 1,
            )
            response_metadata = _apply_pivot_rollout_metadata(
                response_metadata,
                pivot_shadow_scheduled=pivot_shadow_scheduled,
            )
            response_metadata = _normalize_gateway_route_health(
                response_metadata,
                default_decision_node=str(response_metadata.get("query_source") or "cache_multi_intent"),
            )
            result["metadata"] = response_metadata
        return result

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
        return await _handle_create_order(
            payload.order,
            checkout_token=checkout_token,
            request_metadata=normalized_metadata,
        )

    if operation == "create_payment_link":
        payment_link_payload = CreatePaymentLinkPayload(**request.payload)
        return await _handle_create_payment_link(
            payment_link_payload,
            checkout_token=checkout_token,
        )

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
            multi_request_metadata = dict(normalized_metadata)
            multi_request_metadata["_pivot_shadow_schedule_suppressed"] = True
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
                                    multi_request_metadata,
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
                        multi_request_metadata,
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
                        payload, multi_request_metadata, background_tasks
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
                pivot_shadow_scheduled = _maybe_schedule_pivot_multi_shadow_compare(
                    payload=payload,
                    request_metadata=normalized_metadata,
                    background_tasks=background_tasks,
                    served_result=result,
                    source_normalized=source_normalized,
                    page=payload.search.page or 1,
                    dedup_cache_hit=dedup_cache_hit,
                    dedup_inflight_joined=dedup_inflight_joined,
                )
                response_metadata = _apply_pivot_rollout_metadata(
                    response_metadata,
                    pivot_shadow_scheduled=pivot_shadow_scheduled,
                )
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

    if operation == "record_payment_offer_evidence":
        payload = RecordPaymentOfferEvidencePayload(**request.payload)
        return await _handle_record_payment_offer_evidence(payload, checkout_token=checkout_token)

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
