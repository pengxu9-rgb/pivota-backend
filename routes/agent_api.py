"""
Agent 专用 API 路由
为 AI Agent 提供优化的电商接口
"""

from services.merchant_store_service import get_merchant_active_stores, get_primary_store
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Query, Header, Response, Request
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from decimal import Decimal
from datetime import datetime
import asyncio
import json
import os
import re
import time
from urllib.parse import urlparse

from models.order import CreateOrderRequest, OrderResponse
from models.standard_product import StandardProduct
from db.database import database
from db.merchant_onboarding import get_merchant_onboarding
from db.orders import get_order, get_orders_by_merchant, update_payment_info
from routes.refund_api import process_refund
from routes.order_routes import cancel_order as admin_cancel_order
from routes.fulfillment_api import track_order_fulfillment
import routes.order_routes as order_routes_module
from routes.agent_auth import AgentContext, get_agent_context, log_agent_request
from routes.agent_user_auth import AgentUserContext, get_agent_user_context
from utils.logger import logger
from utils.agent_search_intent import infer_query_overrides
from services.product_query_service import get_products_hybrid
from services.quote_service import QuoteError
from services.outbound_links_service import (
    DEFAULT_DISCLOSURE_TEXT,
    DEFAULT_UTM_TEMPLATE,
    _is_domain_allowed,
    apply_utm,
    make_redirect_token,
)
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
from utils.transient_errors import db_busy_http_exception, is_asyncpg_busy_error


router = APIRouter(prefix="/agent/v1", tags=["agent-api"])

_ORDER_CREATE_LOCKS: Dict[str, asyncio.Lock] = {}

EXTERNAL_SEED_MERCHANT_ID = "external_seed"
DEFAULT_EXTERNAL_SEED_MARKET = "US"


def _get_order_create_lock(key: str) -> asyncio.Lock:
    lock = _ORDER_CREATE_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _ORDER_CREATE_LOCKS[key] = lock
    return lock


# ============================================================================
# Helper Functions
# ============================================================================

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


def _row_as_dict(row: Any) -> Dict[str, Any]:
    if row is None:
        return {}
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


def _safe_price_number(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        if isinstance(value, (int, float, Decimal)):
            return float(value)
        text = str(value).strip()
        if not text:
            return default
        try:
            return float(text)
        except Exception:
            pass
        text = text.replace(",", ".")
        match = re.search(r"-?\d+(?:\.\d+)?", text)
        if not match:
            return default
        return float(match.group(0))
    except Exception:
        return default


def _expand_product_ref_aliases(raw_ref: Optional[str]) -> List[str]:
    raw = str(raw_ref or "").strip()
    if not raw:
        return []

    aliases: List[str] = []

    def _add(v: Optional[str]) -> None:
        s = str(v or "").strip()
        if s and s not in aliases:
            aliases.append(s)

    _add(raw)
    if raw.startswith(("http://", "https://")):
        try:
            parsed = urlparse(raw)
            parts = [p for p in (parsed.path or "").split("/") if p]
            for idx, part in enumerate(parts):
                if part == "products" and idx + 1 < len(parts):
                    _add(parts[idx + 1])
        except Exception:
            pass

    if raw.isdigit():
        _add(f"gid://shopify/Product/{raw}")
        _add(f"gid://shopify/ProductVariant/{raw}")

    if "gid://shopify/" in raw:
        maybe_numeric = raw.rstrip("/").split("/")[-1]
        if maybe_numeric.isdigit():
            _add(maybe_numeric)

    if ":" in raw:
        _add(raw.split(":")[-1])
    if "/" in raw:
        _add(raw.rstrip("/").split("/")[-1])

    return aliases[:20]


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
    image_urls = _seed_image_urls(seed_data)
    image_url = seed_data.get("image_url") or seed_row.get("image_url") or (image_urls[0] if image_urls else None)

    external_product_id = (
        str(seed_row.get("external_product_id") or "").strip()
        or str(seed_data.get("external_product_id") or "").strip()
        or _stable_external_product_id(canonical_url or destination_url)
    )
    if not external_product_id:
        return None

    disclosure_text = (
        seed_row.get("disclosure_text")
        or seed_data.get("disclosure_text")
        or DEFAULT_DISCLOSURE_TEXT
    )
    utm_template = seed_row.get("utm_template") or seed_data.get("utm_template") or DEFAULT_UTM_TEMPLATE

    dest_with_utm = apply_utm(destination_url, utm_template, {"market": market, "tool": tool})
    if not await _is_domain_allowed(market=market, destination_url=dest_with_utm):
        return None

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
        image_url = v.get("image_url") or v.get("image")
        if isinstance(image_url, str):
            image_url = image_url.strip() or None
        else:
            image_url = None

        variants.append(
            {
                "id": f"{external_product_id}:{variant_id}",
                "variant_id": variant_id,
                "title": v.get("title") or v.get("name") or f"Variant {idx + 1}",
                "price": variant_price,
                "currency": str(raw_currency or "USD").strip() or "USD",
                "inventory_quantity": 999 if in_stock else 0,
                "in_stock": in_stock,
                **({"availability": availability} if availability is not None else {}),
                **({"image_url": image_url} if image_url else {}),
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
        "disclosure_text": str(disclosure_text or DEFAULT_DISCLOSURE_TEXT),
        "variants": variants,
    }


async def _load_external_seed_products_for_search(*, req: Request, query: Optional[str], limit: int) -> List[Dict[str, Any]]:
    """
    Load employee-managed external products (unattached external seeds) and surface as first-class products.
    """
    q = str(query or "").strip()
    where = ["status = :status", "attached_product_key IS NULL", "market = :market"]
    values: Dict[str, Any] = {"status": "active", "market": DEFAULT_EXTERNAL_SEED_MARKET, "limit": limit}
    if q:
        values["q_like"] = f"%{q}%"
        where.append(
            "(destination_url ILIKE :q_like OR canonical_url ILIKE :q_like OR domain ILIKE :q_like OR title ILIKE :q_like)"
        )

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
            LIMIT :limit
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
    for row in rows:
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


async def _load_external_seed_product_by_product_id(*, req: Request, product_id: str) -> Optional[Dict[str, Any]]:
    pid = str(product_id or "").strip()
    if not pid:
        return None

    row = None
    try:
        row = await database.fetch_one(
            """
            SELECT
              id, external_product_id, market, tool, utm_template, partner_type, disclosure_text,
              destination_url, canonical_url, domain, title, image_url,
              price_amount, price_currency, availability,
              seed_data,
              status, notes, created_by_employee_id,
              attached_product_key, attached_variant_id,
              created_at, updated_at
            FROM external_product_seeds
            WHERE status = 'active'
              AND attached_product_key IS NULL
              AND (
                external_product_id = :pid
                OR id = :pid
                OR seed_data->>'external_product_id' = :pid
              )
            ORDER BY updated_at DESC, created_at DESC
            LIMIT 1
            """,
            {"pid": pid},
        )
    except Exception as exc:
        msg = str(exc)
        if "external_product_seeds" in msg and ("does not exist" in msg or "UndefinedTable" in msg or "relation" in msg):
            return None
        # Backward compat: some deployments may have seed_data as TEXT.
        if "->>" in msg and ("operator does not exist" in msg or "UndefinedFunction" in msg):
            try:
                row = await database.fetch_one(
                    """
                    SELECT
                      id, external_product_id, market, tool, utm_template, partner_type, disclosure_text,
                      destination_url, canonical_url, domain, title, image_url,
                      price_amount, price_currency, availability,
                      seed_data,
                      status, notes, created_by_employee_id,
                      attached_product_key, attached_variant_id,
                      created_at, updated_at
                    FROM external_product_seeds
                    WHERE status = 'active'
                      AND attached_product_key IS NULL
                      AND (external_product_id = :pid OR id = :pid)
                    ORDER BY updated_at DESC, created_at DESC
                    LIMIT 1
                    """,
                    {"pid": pid},
                )
            except Exception as exc2:
                msg2 = str(exc2)
                if "external_product_seeds" in msg2 and ("does not exist" in msg2 or "UndefinedTable" in msg2 or "relation" in msg2):
                    return None
                raise
        else:
            raise

    if not row:
        return None
    try:
        return await _build_external_seed_product(req=req, seed_row=dict(row))
    except Exception:
        return None

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


def _context_can_access_merchant(context: Any, merchant_id: str) -> bool:
    """
    Defensive merchant-access check for AgentContext.

    In production we observed rare cases where an instance attribute shadows the
    `can_access_merchant` method (becoming None), causing `TypeError: 'NoneType' object is not callable`.
    This helper preserves the intended semantics while failing closed when the
    context shape is unexpected.
    """
    mid = str(merchant_id or "").strip()
    if not mid:
        return False

    # Prefer instance method when present/callable.
    fn = getattr(context, "can_access_merchant", None)
    if callable(fn):
        try:
            return bool(fn(mid))
        except Exception:
            return False

    # If an instance attribute shadowed the method, fall back to the class method.
    cls_fn = getattr(type(context), "can_access_merchant", None)
    if callable(cls_fn):
        try:
            return bool(cls_fn(context, mid))
        except Exception:
            return False

    # Final fallback: use allowed_merchants if available.
    if not hasattr(context, "allowed_merchants"):
        return False
    allowed = getattr(context, "allowed_merchants", None)
    if allowed is None:
        return True
    try:
        return mid in allowed
    except Exception:
        return False

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
        row_data = _row_as_dict(row)
        if row_data.get("target_ref"):
            target_ref = str(row_data["target_ref"])
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
    req: Request,
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
    allow_external_seed: bool = Query(default=True),
    allow_stale_cache: bool = Query(default=True),
    external_seed_strategy: str = Query(default="legacy"),
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
    started = time.perf_counter()
    try:
        normalized_seed_strategy = str(external_seed_strategy or "legacy").strip().lower()
        if normalized_seed_strategy not in {"legacy", "supplement_internal_first"}:
            normalized_seed_strategy = "legacy"

        def _is_external_seed_product(product: Dict[str, Any]) -> bool:
            return (
                str(product.get("merchant_id") or "").strip() == EXTERNAL_SEED_MERCHANT_ID
                or str(product.get("source") or "").strip() == "external_seed"
            )

        overrides = infer_query_overrides(query=query, category=category)
        query = overrides["query"]
        category = overrides["category"]
        query_terms: List[str] = overrides["terms"]

        normalized_query = query.strip() if isinstance(query, str) else ""
        normalized_category = category.strip() if isinstance(category, str) else ""
        is_browse_mode = (
            not normalized_query
            and not normalized_category
            and min_price is None
            and max_price is None
        )

        # Fast path: cross-merchant browse (empty query/filters).
        #
        # The shopping gateway sometimes calls this endpoint with an empty query just to
        # fetch a first page. Iterating every merchant and loading per-merchant pools
        # is slow; instead, read a recent slice directly from products_cache.
        if (
            is_browse_mode
            and not merchant_id
            and not merchant_ids
            and (search_all_merchants is True)
        ):
            try:
                allowed = (
                    [m for m in (getattr(context, "allowed_merchants", None) or []) if m]
                    if isinstance(getattr(context, "allowed_merchants", None), list)
                    else []
                )

                page_limit = max(1, min(int(limit or 20), 100))
                page_offset = max(0, int(offset or 0))

                # Fetch one extra row to compute has_more without an expensive COUNT(*).
                fetch_limit = page_limit + 1

                # Oversample candidates so we can apply merchant filters without
                # forcing the database to sort/join the entire products_cache table.
                candidate_limit = min(500, fetch_limit * 25)

                subquery_where_allowed = ""
                params: Dict[str, Any] = {"candidate_limit": candidate_limit, "offset": page_offset}
                if allowed:
                    subquery_where_allowed = "AND merchant_id = ANY(:allowed_merchants)"
                    params["allowed_merchants"] = allowed

                rows = await database.fetch_all(
                    """
	                    SELECT pc.merchant_id,
	                           mo.business_name AS merchant_name,
	                           pc.product_data
	                    FROM (
	                      SELECT id, expires_at, merchant_id, product_data
	                      FROM products_cache
	                      WHERE expires_at > NOW()
	                      {subquery_where_allowed}
	                      ORDER BY expires_at DESC, id DESC
	                      LIMIT :candidate_limit
	                      OFFSET :offset
	                    ) pc
	                    JOIN merchant_onboarding mo
	                      ON mo.merchant_id = pc.merchant_id
	                    WHERE mo.status NOT IN ('deleted', 'rejected')
	                      AND mo.psp_connected = true
	                    ORDER BY pc.expires_at DESC, pc.id DESC
	                    """.format(subquery_where_allowed=subquery_where_allowed),
	                    params,
	                )

                products: List[Dict[str, Any]] = []
                for row in rows:
                    row_data = _row_as_dict(row)
                    if not row_data:
                        continue
                    pdata = row_data.get("product_data")
                    if isinstance(pdata, str):
                        try:
                            pdata = json.loads(pdata)
                        except Exception:
                            continue
                    if not isinstance(pdata, dict):
                        continue

                    if in_stock_only and not pdata.get("in_stock", True):
                        continue

                    pdata = dict(pdata)
                    pdata["merchant_id"] = str(row_data.get("merchant_id") or pdata.get("merchant_id") or "")
                    pdata["merchant_name"] = row_data.get("merchant_name") or pdata.get("merchant_name") or "Unknown"
                    pdata.setdefault("relevance_score", 1.0)
                    pdata.setdefault("ranking_score", 1.0)
                    pdata.setdefault("ranking_features", {"mode": "browse_fastpath"})
                    products.append(pdata)

                    if len(products) >= fetch_limit:
                        break

                has_more = len(products) > page_limit
                page_items = products[:page_limit]

                background_tasks.add_task(
                    log_agent_request,
                    context=context,
                    status_code=200,
                    merchant_id="cross_merchant_search",
                )
                latency_ms = int((time.perf_counter() - started) * 1000)
                logger.info(
                    "agent_search_products.summary",
                    extra={
                        "event": "agent_search_products.summary",
                        "query": query,
                        "merchant_scope": None,
                        "merchant_ids": allowed if allowed else None,
                        "reason_code": "ok" if page_items else "no_candidates",
                        "latency_ms": latency_ms,
                        "result_count": len(page_items),
                    },
                )

                return {
                    "status": "success",
                    "products": page_items,
                    "pagination": {
                        # Avoid COUNT(*) in hot path; provide a lower bound.
                        "total_count": page_offset + len(page_items) + (1 if has_more else 0),
                        "limit": page_limit,
                        "offset": page_offset,
                        "page": (page_offset // page_limit) + 1 if page_limit > 0 else 1,
                        "total_pages": (page_offset // page_limit) + 1 + (1 if has_more else 0),
                        "has_more": has_more,
                    },
                    "search_context": {
                        "merchant_id": None,
                        "merchant_ids": allowed if allowed else None,
                        "merchants_searched": None,
                        "cross_merchant_search": True,
                    },
                    "filters_applied": {
                        "query": query,
                        "category": category,
                        "min_price": min_price,
                        "max_price": max_price,
                        "in_stock_only": in_stock_only,
                    },
                    "metadata": {
                        "source": "agent_search_products",
                        "reason_code": "ok" if page_items else "no_candidates",
                        "latency_ms": latency_ms,
                        "source_breakdown": {
                            "internal_count": len(page_items),
                            "external_seed_count": 0,
                            "stale_cache_used": False,
                            "strategy_applied": normalized_seed_strategy if allow_external_seed else "external_seed_disabled",
                        },
                    },
                }
            except Exception:
                # If the fast path fails for any reason, fall back to the
                # standard per-merchant code path below.
                logger.debug("agent_search_products browse fast path failed", exc_info=True)
        # Determine which merchants to search
        merchants_to_search = []
        merchant_name_by_id: Dict[str, str] = {}
        
        if merchant_id:
            if merchant_id == EXTERNAL_SEED_MERCHANT_ID:
                merchants_to_search = []
            else:
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
                merchant_rows_data = [_row_as_dict(row) for row in merchant_rows]
                merchants_to_search = [
                    str(row_data.get("merchant_id") or "").strip()
                    for row_data in merchant_rows_data
                    if str(row_data.get("merchant_id") or "").strip()
                ]
                merchant_name_by_id = {
                    mid: (str(row_data.get("business_name") or "").strip() or "Unknown")
                    for row_data in merchant_rows_data
                    for mid in [str(row_data.get("merchant_id") or "").strip()]
                    if mid
                }

        # Collect products from all target merchants
        all_products: List[Dict[str, Any]] = []

        # Multi-merchant fetch can be slow when done sequentially (N+1 DB queries,
        # plus optional realtime calls). For cross-merchant search we force cache-only
        # and fetch concurrently to keep tail latency bounded.
        is_multi_merchant_scope = len(merchants_to_search) > 1
        force_cache_only = is_multi_merchant_scope

        # When callers provide multiple merchant_ids (or the agent has a multi-merchant
        # allowed list), avoid per-merchant verification queries; instead, fetch a
        # compact merchant_id->name map in one query and filter out inactive merchants.
        if force_cache_only and merchants_to_search and not merchant_name_by_id:
            try:
                rows = await database.fetch_all(
                    """
	                    SELECT merchant_id, business_name
	                    FROM merchant_onboarding
	                    WHERE merchant_id = ANY(:merchant_ids)
	                      AND status NOT IN ('deleted', 'rejected')
	                      AND psp_connected = true
	                    """,
                    {"merchant_ids": merchants_to_search},
                )
                rows_data = [_row_as_dict(row) for row in rows]
                merchant_name_by_id = {
                    mid: (str(row_data.get("business_name") or "").strip() or "Unknown")
                    for row_data in rows_data
                    for mid in [str(row_data.get("merchant_id") or "").strip()]
                    if mid
                }
                merchants_to_search = list(merchant_name_by_id.keys())
            except Exception:
                logger.debug("agent_search_products merchant name batch lookup failed", exc_info=True)

        try:
            fetch_concurrency = int(os.getenv("AGENT_SEARCH_FETCH_CONCURRENCY", "12"))
        except Exception:
            fetch_concurrency = 12
        fetch_concurrency = max(1, min(32, fetch_concurrency))
        fetch_sem = asyncio.Semaphore(fetch_concurrency)

        per_merchant_limit = min(50, max(10, int(limit or 20)))

        async def _fetch_products_for_merchant(mid: str) -> List[Dict[str, Any]]:
            async with fetch_sem:
                try:
                    merchant_name = merchant_name_by_id.get(mid)
                    if not merchant_name and not force_cache_only:
                        merchant = await verify_merchant_active(mid)
                        merchant_name = merchant.get("business_name", "Unknown")

                    products, query_source, _ = await get_products_hybrid(
                        merchant_id=mid,
                        limit=per_merchant_limit,
                        agent_id=context.agent_id,
                        background_tasks=background_tasks,
                        force_cache_only=force_cache_only,
                        allow_stale_cache=allow_stale_cache,
                    )

                    out: List[Dict[str, Any]] = []
                    for sp in products:
                        prod_dict = sp.model_dump()
                        prod_dict["merchant_id"] = mid
                        prod_dict["merchant_name"] = merchant_name or prod_dict.get("merchant_name") or "Unknown"
                        prod_dict["query_source"] = query_source
                        out.append(prod_dict)
                    return out
                except Exception as e:
                    logger.warning(f"Failed to get products from {mid}: {e}")
                    return []

        if merchants_to_search:
            try:
                results = await asyncio.gather(*[_fetch_products_for_merchant(mid) for mid in merchants_to_search])
                for chunk in results:
                    if chunk:
                        all_products.extend(chunk)
            except Exception:
                # Non-fatal: fall back to sequential fetch.
                logger.debug("agent_search_products concurrent fetch failed; falling back to sequential", exc_info=True)
                for mid in merchants_to_search:
                    try:
                        all_products.extend(await _fetch_products_for_merchant(mid))
                    except Exception:
                        continue

        # Add employee-managed external products only for explicit external/cross-merchant flows.
        include_external_seed = allow_external_seed and (
            (merchant_id is None and not merchant_ids)
            or merchant_id == EXTERNAL_SEED_MERCHANT_ID
        )
        if include_external_seed:
            try:
                external_seed_limit = min(200, max(20, int(limit or 20) * 5))
                external_seed_products = await _load_external_seed_products_for_search(
                    req=req,
                    query=query,
                    limit=external_seed_limit,
                )
                if external_seed_products:
                    all_products.extend(external_seed_products)
            except Exception as e:
                logger.warning(f"Failed to load external seed products: {e}")

        ranking_config = get_agent_ranking_config()

        # Browse mode (no query/filters): keep this endpoint fast for Agents.
        # The Shopping Agent often calls `/products/search` with an empty query
        # just to fetch a first page of products; doing N+1 enrichment queries
        # can exceed upstream timeouts.
        if is_browse_mode:
            browse_internal: List[Dict[str, Any]] = []
            browse_external: List[Dict[str, Any]] = []
            for product in all_products:
                if in_stock_only and not product.get("in_stock", True):
                    continue

                price = _safe_price_number(product.get("price", 0), 0.0)
                if min_price and price < min_price:
                    continue
                if max_price and price > max_price:
                    continue

                product.setdefault("relevance_score", 1.0)
                product.setdefault(
                    "ranking_score", float(product.get("relevance_score", 1.0) or 1.0)
                )
                product.setdefault("ranking_features", {"mode": "browse"})
                if (
                    normalized_seed_strategy == "supplement_internal_first"
                    and merchant_id != EXTERNAL_SEED_MERCHANT_ID
                    and _is_external_seed_product(product)
                ):
                    browse_external.append(product)
                else:
                    browse_internal.append(product)

            if normalized_seed_strategy == "supplement_internal_first" and merchant_id != EXTERNAL_SEED_MERCHANT_ID:
                browse_candidates = browse_internal + browse_external
            else:
                browse_candidates = browse_internal + browse_external

            total = len(browse_candidates)
            paginated_products = browse_candidates[offset : offset + limit]
            external_count = sum(1 for p in paginated_products if _is_external_seed_product(p))
            source_breakdown = {
                "internal_count": len(paginated_products) - external_count,
                "external_seed_count": external_count,
                "stale_cache_used": any(
                    "stale" in str((p.get("query_source") or "")).lower()
                    for p in browse_candidates
                ),
                "strategy_applied": normalized_seed_strategy if allow_external_seed else "external_seed_disabled",
            }

            background_tasks.add_task(
                log_agent_request,
                context=context,
                status_code=200,
                merchant_id=merchant_id or "cross_merchant_search",
            )
            latency_ms = int((time.perf_counter() - started) * 1000)
            logger.info(
                "agent_search_products.summary",
                extra={
                    "event": "agent_search_products.summary",
                    "query": query,
                    "merchant_scope": merchant_id,
                    "merchant_ids": merchant_ids,
                    "reason_code": "ok" if paginated_products else "no_candidates",
                    "latency_ms": latency_ms,
                    "result_count": len(paginated_products),
                    "merchants_searched": len(merchants_to_search),
                },
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
                    "has_more": offset + limit < total,
                },
                "search_context": {
                    "merchant_id": merchant_id,
                    "merchant_ids": merchant_ids,
                    "merchants_searched": len(merchants_to_search),
                    "cross_merchant_search": merchant_id is None and not merchant_ids,
                },
                "filters_applied": {
                    "query": query,
                    "category": category,
                    "min_price": min_price,
                    "max_price": max_price,
                    "in_stock_only": in_stock_only,
                },
                "metadata": {
                    "source": "agent_search_products",
                    "reason_code": "ok" if paginated_products else "no_candidates",
                    "latency_ms": latency_ms,
                    "source_breakdown": source_breakdown,
                },
            }

        # Search mode: Apply filters, hydrate features, then compute ranking.
        ranked_candidates: List[Dict[str, Any]] = []
        external_ranked_candidates: List[Dict[str, Any]] = []
        candidates: List[Dict[str, Any]] = []
        candidate_features: List[AgentRankingFeatures] = []

        for product in all_products:
            if in_stock_only and not product.get("in_stock", True):
                continue

            price = _safe_price_number(product.get("price", 0), 0.0)
            if min_price and price < min_price:
                continue
            if max_price and price > max_price:
                continue

            if normalized_category:
                product_category = (
                    " ".join(
                        [
                            str(product.get("category") or ""),
                            str(product.get("product_type") or ""),
                            " ".join(product.get("tags") or []),
                        ]
                    )
                ).lower()
                if normalized_category.lower() not in product_category:
                    continue

            relevance_score = 1.0
            if normalized_query:
                query_lower = normalized_query.lower()
                title = product.get("title", "").lower()
                description = product.get("description", "").lower()
                tags = " ".join(product.get("tags") or []).lower()
                product_type = (product.get("product_type") or "").lower()
                haystack = " ".join([title, description, tags, product_type]).strip()

                if query_lower in title:
                    relevance_score = 1.0 if query_lower == title else 0.9
                elif query_lower in description:
                    relevance_score = 0.7
                elif query_lower in tags or query_lower in product_type:
                    relevance_score = 0.75
                else:
                    query_words = query_terms or query_lower.split()
                    matches = sum(
                        1 for word in query_words if word and word in haystack
                    )
                    if matches > 0:
                        relevance_score = 0.5 + (matches / len(query_words)) * 0.3
                    else:
                        continue

                product["relevance_score"] = relevance_score
            else:
                product["relevance_score"] = 1.0

            is_external_seed = (
                product.get("source") == "external_seed"
                or product.get("merchant_id") == EXTERNAL_SEED_MERCHANT_ID
            )
            if is_external_seed:
                platform_product_id = str(
                    product.get("product_id") or product.get("id") or ""
                )
                if not platform_product_id:
                    continue
                try:
                    product["ranking_score"] = (
                        float(product.get("relevance_score", 0.0)) * 0.8
                    )
                except Exception:
                    product["ranking_score"] = product.get("relevance_score", 0.0)
                product["ranking_features"] = {"source": "external_seed"}
                if (
                    normalized_seed_strategy == "supplement_internal_first"
                    and merchant_id != EXTERNAL_SEED_MERCHANT_ID
                ):
                    external_ranked_candidates.append(product)
                else:
                    ranked_candidates.append(product)
                continue

            platform = product.get("platform") or "unknown"
            platform_product_id = str(
                product.get("product_id") or product.get("id") or ""
            )
            if not platform_product_id:
                continue

            features = AgentRankingFeatures(
                merchant_id=product.get("merchant_id"),
                platform=platform,
                platform_product_id=platform_product_id,
                rel_semantic=relevance_score,
                rel_keyword=relevance_score,
                rel_category_match=1.0
                if normalized_category
                and normalized_category.lower()
                in (product.get("product_type") or "").lower()
                else 0.0,
            )

            candidates.append(product)
            candidate_features.append(features)

        # Hydrate quality/enrichment features concurrently (bounded).
        try:
            hydrate_concurrency = int(os.getenv("AGENT_RANKING_HYDRATE_CONCURRENCY", "8"))
        except Exception:
            hydrate_concurrency = 8
        hydrate_concurrency = max(1, min(32, hydrate_concurrency))
        sem = asyncio.Semaphore(hydrate_concurrency)

        async def _hydrate_one(feats: AgentRankingFeatures) -> None:
            async with sem:
                await hydrate_quality_and_enrichment(feats)

        try:
            hydrate_timeout_s = _env_float(
                "AGENT_RANKING_HYDRATE_TIMEOUT_S",
                1.0,
                min_value=0.2,
                max_value=10.0,
            )
        except Exception:
            hydrate_timeout_s = 1.0
        if candidate_features:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*[_hydrate_one(f) for f in candidate_features]),
                    timeout=max(0.2, hydrate_timeout_s),
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "agent_search_products hydration timeout",
                    extra={
                        "event": "agent_search_products_hydration_timeout",
                        "count": len(candidate_features),
                        "timeout_s": hydrate_timeout_s,
                    },
                )
            except Exception:
                logger.debug(
                    "agent_search_products hydration failed (non-fatal)",
                    exc_info=True,
                )

        for product, features in zip(candidates, candidate_features):
            if not passes_agent_gating(features, ranking_config):
                continue
            score = compute_agent_ranking_score(features, ranking_config)
            product["ranking_score"] = score
            product["ranking_features"] = serialize_features_for_log(features, score)
            ranked_candidates.append(product)

        # Sort by ranking score (fallback to relevance when missing)
        ranked_candidates.sort(
            key=lambda p: (p.get("ranking_score") is not None, p.get("ranking_score", p.get("relevance_score", 0))),
            reverse=True,
        )
        if external_ranked_candidates:
            external_ranked_candidates.sort(
                key=lambda p: (
                    p.get("ranking_score") is not None,
                    p.get("ranking_score", p.get("relevance_score", 0)),
                ),
                reverse=True,
            )
            ranked_candidates = ranked_candidates + external_ranked_candidates

        # Pagination
        total = len(ranked_candidates)
        paginated_products = ranked_candidates[offset : offset + limit]
        external_count = sum(1 for p in paginated_products if _is_external_seed_product(p))
        source_breakdown = {
            "internal_count": len(paginated_products) - external_count,
            "external_seed_count": external_count,
            "stale_cache_used": any(
                "stale" in str((p.get("query_source") or "")).lower()
                for p in ranked_candidates
            ),
            "strategy_applied": normalized_seed_strategy if allow_external_seed else "external_seed_disabled",
        }

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
        latency_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "agent_search_products.summary",
            extra={
                "event": "agent_search_products.summary",
                "query": query,
                "merchant_scope": merchant_id,
                "merchant_ids": merchant_ids,
                "reason_code": "ok" if paginated_products else "no_candidates",
                "latency_ms": latency_ms,
                "result_count": len(paginated_products),
                "merchants_searched": len(merchants_to_search),
            },
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
            },
            "metadata": {
                "source": "agent_search_products",
                "reason_code": "ok" if paginated_products else "no_candidates",
                "latency_ms": latency_ms,
                "source_breakdown": source_breakdown,
            },
        }
        
    except HTTPException:
        raise
    except Exception as e:
        reason_code = _classify_db_reason_code(e)
        latency_ms = int((time.perf_counter() - started) * 1000)
        error_text = str(e or "").strip()
        logger.exception(
            "Agent product search error: %s",
            (error_text[:300] if error_text else type(e).__name__),
            extra={
                "event": "agent_search_products.failed",
                "query": query,
                "merchant_scope": merchant_id,
                "merchant_ids": merchant_ids,
                "latency_ms": latency_ms,
                "reason_code": reason_code,
                "error_type": type(e).__name__,
            },
        )
        await log_agent_request(
            context=context,
            status_code=500,
            merchant_id=merchant_id,
            error_message=str(e)
        )
        raise HTTPException(status_code=500, detail="Search failed")


@router.get("/products/resolve")
async def agent_resolve_products(
    req: Request,
    merchant_id: Optional[str] = Query(None, description="Optional merchant scope"),
    product_id: Optional[str] = Query(None, description="Product id / canonical ref / PDP URL"),
    sku_id: Optional[str] = Query(None, description="SKU / variant id"),
    query: Optional[str] = Query(None, description="Fallback free-text query"),
    limit: int = Query(default=10, le=50),
    context: AgentContext = Depends(get_agent_context),
):
    """
    Resolve a stable product reference for PDP flows.

    Resolution order:
    1) products_cache exact aliases (product_id / sku_id)
    2) scoped / global agent search fallback
    3) canonical product_group mapping (when available)
    """
    started = time.perf_counter()

    if merchant_id and not context.can_access_merchant(merchant_id):
        raise HTTPException(status_code=403, detail="Not authorized for this merchant")

    product_aliases = _expand_product_ref_aliases(product_id)
    sku_aliases = _expand_product_ref_aliases(sku_id)
    query_text = str(query or "").strip()
    if not product_aliases and not sku_aliases and not query_text:
        raise HTTPException(
            status_code=400,
            detail="At least one of product_id, sku_id, or query is required",
        )

    sources: List[Dict[str, Any]] = []
    has_identifier_input = bool(product_aliases or sku_aliases)
    resolve_exact_pid_timeout_s = _env_float(
        "AGENT_RESOLVE_EXACT_PID_TIMEOUT_S",
        0.9,
        min_value=0.1,
        max_value=6.0,
    )
    resolve_exact_sku_timeout_s = _env_float(
        "AGENT_RESOLVE_EXACT_SKU_TIMEOUT_S",
        1.1,
        min_value=0.1,
        max_value=6.0,
    )
    resolve_alias_pid_timeout_s = _env_float(
        "AGENT_RESOLVE_ALIAS_PID_TIMEOUT_S",
        1.0,
        min_value=0.1,
        max_value=6.0,
    )
    resolve_alias_sku_scan_timeout_s = _env_float(
        "AGENT_RESOLVE_ALIAS_SKU_SCAN_TIMEOUT_S",
        0.7,
        min_value=0.1,
        max_value=6.0,
    )
    resolve_group_timeout_s = _env_float(
        "AGENT_RESOLVE_GROUP_TIMEOUT_S",
        1.0,
        min_value=0.1,
        max_value=6.0,
    )
    resolve_enable_sku_text_scan = _env_bool(
        "AGENT_RESOLVE_ENABLE_SKU_TEXT_SCAN",
        False,
    )

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
        row: Dict[str, Any] = {
            "source": source,
            "status": status,
            "reason_code": reason_code,
            "reason": _public_reason_code(reason_code),
            "ok": status == "ok",
            "latency_ms": int((time.perf_counter() - source_started) * 1000),
        }
        if row_count is not None:
            row["row_count"] = int(row_count)
        if error:
            row["error"] = str(error)[:500]
        if query:
            row["query"] = query
        sources.append(row)

    candidates_by_key: Dict[str, Dict[str, Any]] = {}
    exact_path_resolved = False

    def _add_candidate(
        *,
        merchant: Optional[str],
        platform: Optional[str],
        platform_product_id: Optional[str],
        title: Optional[str],
        source: str,
        score: float,
    ) -> None:
        mid = str(merchant or "").strip()
        pid = str(platform_product_id or "").strip()
        plat = str(platform or "").strip().lower() or "unknown"
        if not mid or not pid:
            return
        key = f"{mid}:{plat}:{pid}"
        current = candidates_by_key.get(key)
        payload = {
            "merchant_id": mid,
            "platform": plat,
            "product_id": pid,
            "title": str(title or "").strip() or None,
            "source": source,
            "score": float(score),
        }
        if not current or float(payload["score"]) > float(current.get("score", 0)):
            candidates_by_key[key] = payload

    def _ingest_cache_rows(rows: List[Any], source_name: str, score: float) -> None:
        for row in rows:
            row_data = _row_as_dict(row)
            if not row_data:
                continue
            pdata = row_data.get("product_data")
            if isinstance(pdata, str):
                try:
                    pdata = json.loads(pdata)
                except Exception:
                    pdata = None
            if not isinstance(pdata, dict):
                continue
            _add_candidate(
                merchant=row_data.get("merchant_id"),
                platform=row_data.get("platform"),
                platform_product_id=(
                    pdata.get("id")
                    or pdata.get("product_id")
                    or row_data.get("platform_product_id")
                ),
                title=pdata.get("title") or pdata.get("name"),
                source=source_name,
                score=score,
            )

    # Source 1: exact lookup first (primary/unique-key oriented).
    cache_exact_started = time.perf_counter()
    exact_cache_rows: List[Dict[str, Any]] = []
    try:
        if product_aliases:
            rows = await asyncio.wait_for(
                database.fetch_all(
                    """
                    SELECT merchant_id, platform, platform_product_id, product_data
                    FROM products_cache
                    WHERE (expires_at IS NULL OR expires_at > NOW())
                      AND (CAST(:merchant_id AS TEXT) IS NULL OR merchant_id = CAST(:merchant_id AS TEXT))
                      AND platform_product_id = ANY(:pid_aliases)
                    ORDER BY cached_at DESC
                    LIMIT 80
                    """,
                    {
                        "merchant_id": merchant_id,
                        "pid_aliases": product_aliases,
                    },
                ),
                timeout=resolve_exact_pid_timeout_s,
            )
            exact_cache_rows.extend([dict(r) for r in (rows or [])])

        if sku_aliases and not exact_cache_rows:
            rows = await asyncio.wait_for(
                database.fetch_all(
                    """
                    SELECT merchant_id, platform, platform_product_id, product_data
                    FROM products_cache
                    WHERE (expires_at IS NULL OR expires_at > NOW())
                      AND (CAST(:merchant_id AS TEXT) IS NULL OR merchant_id = CAST(:merchant_id AS TEXT))
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
                    LIMIT 80
                    """,
                    {
                        "merchant_id": merchant_id,
                        "sku_aliases": sku_aliases,
                    },
                ),
                timeout=resolve_exact_sku_timeout_s,
            )
            exact_cache_rows.extend([dict(r) for r in (rows or [])])

        _ingest_cache_rows(exact_cache_rows, "products_cache_exact", 1.0)
        _record_source(
            source="products_cache_exact",
            status="ok" if exact_cache_rows else "empty",
            reason_code="ok" if exact_cache_rows else "no_candidates",
            source_started=cache_exact_started,
            row_count=len(exact_cache_rows),
            query="products_cache_exact",
        )
        exact_path_resolved = bool(candidates_by_key)
    except Exception as e:
        _record_source(
            source="products_cache_exact",
            status="error",
            reason_code=_classify_db_reason_code(e),
            source_started=cache_exact_started,
            error=type(e).__name__,
            query="products_cache_exact",
        )

    # Source 2: alias/JSON fallback when exact lookup misses.
    cache_alias_started = time.perf_counter()
    try:
        cache_rows: List[Dict[str, Any]] = []
        cache_alias_scan_skipped = False
        if has_identifier_input and not candidates_by_key and product_aliases:
            rows = await asyncio.wait_for(
                database.fetch_all(
                    """
                    SELECT merchant_id, platform, platform_product_id, product_data
                    FROM products_cache
                    WHERE (expires_at IS NULL OR expires_at > NOW())
                      AND (CAST(:merchant_id AS TEXT) IS NULL OR merchant_id = CAST(:merchant_id AS TEXT))
                      AND (
                        platform_product_id = ANY(:pid_aliases)
                        OR product_data->>'id' = ANY(:pid_aliases)
                        OR product_data->>'product_id' = ANY(:pid_aliases)
                      )
                    ORDER BY cached_at DESC
                    LIMIT 120
                    """,
                    {
                        "merchant_id": merchant_id,
                        "pid_aliases": product_aliases,
                    },
                ),
                timeout=resolve_alias_pid_timeout_s,
            )
            cache_rows.extend([dict(r) for r in (rows or [])])

        if has_identifier_input and not candidates_by_key and sku_aliases and not cache_rows:
            if resolve_enable_sku_text_scan:
                where_parts: List[str] = []
                params: Dict[str, Any] = {"merchant_id": merchant_id}
                for idx, sku_alias in enumerate(sku_aliases[:8]):
                    key = f"sku_like_{idx}"
                    params[key] = f"%{str(sku_alias).lower()}%"
                    where_parts.append(f"LOWER(CAST(product_data AS TEXT)) LIKE :{key}")
                rows = await asyncio.wait_for(
                    database.fetch_all(
                        f"""
                        SELECT merchant_id, platform, platform_product_id, product_data
                        FROM products_cache
                        WHERE (expires_at IS NULL OR expires_at > NOW())
                          AND (CAST(:merchant_id AS TEXT) IS NULL OR merchant_id = CAST(:merchant_id AS TEXT))
                          AND ({' OR '.join(where_parts)})
                        ORDER BY cached_at DESC
                        LIMIT 120
                        """,
                        params,
                    ),
                    timeout=resolve_alias_sku_scan_timeout_s,
                )
                cache_rows.extend([dict(r) for r in (rows or [])])
            else:
                cache_alias_scan_skipped = True

        _ingest_cache_rows(cache_rows, "products_cache_alias", 0.95)
        cache_status = "ok" if cache_rows else "skipped" if cache_alias_scan_skipped else "empty"
        cache_reason_code = "ok" if cache_rows else "skipped_sku_text_scan_disabled" if cache_alias_scan_skipped else "no_candidates"
        _record_source(
            source="products_cache",
            status=cache_status,
            reason_code=cache_reason_code,
            source_started=cache_alias_started,
            row_count=len(cache_rows),
            query="products_cache_by_alias",
        )
    except Exception as e:
        _record_source(
            source="products_cache",
            status="error",
            reason_code=_classify_db_reason_code(e),
            source_started=cache_alias_started,
            error=type(e).__name__,
            query="products_cache_by_alias",
        )

    # Source 2/3: scoped/global search fallback.
    search_query = query_text or (product_aliases[0] if product_aliases else sku_aliases[0] if sku_aliases else "")
    search_timeout_s = 4.0
    try:
        search_timeout_s = _env_float(
            "AGENT_RESOLVE_SEARCH_TIMEOUT_S",
            1.2,
            min_value=0.4,
            max_value=8.0,
        )
    except Exception:
        search_timeout_s = 1.2

    should_try_global = (
        not merchant_id
        or (not product_aliases and not sku_aliases and bool(query_text))
    )

    if search_query and not candidates_by_key:
        async def _resolve_with_search(*, scoped_merchant: Optional[str], source_name: str) -> bool:
            search_started = time.perf_counter()
            try:
                result = await asyncio.wait_for(
                    agent_search_products(
                        req=req,
                        background_tasks=BackgroundTasks(),
                        merchant_id=scoped_merchant,
                        merchant_ids=None,
                        search_all_merchants=(scoped_merchant is None),
                        query=search_query,
                        category=None,
                        min_price=None,
                        max_price=None,
                        in_stock_only=True,
                        limit=max(20, min(80, limit * 3)),
                        offset=0,
                        context=context,
                    ),
                    timeout=search_timeout_s,
                )
                products = (result or {}).get("products") if isinstance(result, dict) else []
                for p in products or []:
                    if not isinstance(p, dict):
                        continue
                    _add_candidate(
                        merchant=p.get("merchant_id") or scoped_merchant,
                        platform=p.get("platform"),
                        platform_product_id=(p.get("product_id") or p.get("id")),
                        title=p.get("title") or p.get("name"),
                        source=source_name,
                        score=0.75,
                    )
                _record_source(
                    source=source_name,
                    status="ok" if products else "empty",
                    reason_code="ok" if products else "no_candidates",
                    source_started=search_started,
                    row_count=len(products or []),
                    query="agent_search_products",
                )
                return bool(products)
            except asyncio.TimeoutError:
                _record_source(
                    source=source_name,
                    status="error",
                    reason_code="upstream_timeout",
                    source_started=search_started,
                    error="TimeoutError",
                    query="agent_search_products",
                )
                return False
            except Exception as e:
                reason_code = "upstream_timeout" if "timeout" in str(e).lower() else "upstream_error"
                _record_source(
                    source=source_name,
                    status="error",
                    reason_code=reason_code,
                    source_started=search_started,
                    error=type(e).__name__,
                    query="agent_search_products",
                )
                return False

        if merchant_id:
            _ = await _resolve_with_search(scoped_merchant=merchant_id, source_name="agent_search_scoped")
        if should_try_global and not candidates_by_key:
            _ = await _resolve_with_search(scoped_merchant=None, source_name="agent_search_global")

    candidates = sorted(
        candidates_by_key.values(),
        key=lambda it: float(it.get("score") or 0),
        reverse=True,
    )

    # Canonical mapping via product groups.
    canonical_group_id: Optional[str] = None
    canonical_ref: Optional[str] = None
    canonical_product: Optional[Dict[str, Any]] = None
    group_started = time.perf_counter()
    if exact_path_resolved and has_identifier_input:
        _record_source(
            source="product_group_members",
            status="skipped",
            reason_code="skipped_fast_path",
            source_started=group_started,
            row_count=0,
            query="product_group_members_by_pid",
        )
    else:
        try:
            candidate_product_ids = [str(c.get("product_id") or "").strip() for c in candidates if c.get("product_id")]
            candidate_product_ids = list(dict.fromkeys([c for c in candidate_product_ids if c]))[:200]
            if candidate_product_ids:
                rows = await asyncio.wait_for(
                    database.fetch_all(
                        """
                        SELECT product_group_id, merchant_id, platform, platform_product_id, is_primary
                        FROM product_group_members
                        WHERE platform_product_id = ANY(:pids)
                        ORDER BY is_primary DESC, merchant_id ASC
                        LIMIT 200
                        """,
                        {"pids": candidate_product_ids},
                    ),
                    timeout=resolve_group_timeout_s,
                )
                if rows:
                    first = dict(rows[0])
                    canonical_group_id = str(first.get("product_group_id") or "").strip() or None
                    if canonical_group_id:
                        primary = None
                        for r in rows:
                            rd = dict(r)
                            if str(rd.get("product_group_id") or "") != canonical_group_id:
                                continue
                            if bool(rd.get("is_primary")):
                                primary = rd
                                break
                            if primary is None:
                                primary = rd
                        if primary:
                            canonical_product = {
                                "merchant_id": primary.get("merchant_id"),
                                "platform": primary.get("platform"),
                                "product_id": primary.get("platform_product_id"),
                                "product_group_id": canonical_group_id,
                            }
                            canonical_ref = f"pg:{canonical_group_id}"
                _record_source(
                    source="product_group_members",
                    status="ok" if rows else "empty",
                    reason_code="ok" if rows else "no_candidates",
                    source_started=group_started,
                    row_count=len(rows or []),
                    query="product_group_members_by_pid",
                )
            else:
                _record_source(
                    source="product_group_members",
                    status="empty",
                    reason_code="no_candidates",
                    source_started=group_started,
                    row_count=0,
                    query="product_group_members_by_pid",
                )
        except Exception as e:
            _record_source(
                source="product_group_members",
                status="error",
                reason_code=_classify_db_reason_code(e),
                source_started=group_started,
                error=type(e).__name__,
                query="product_group_members_by_pid",
            )

    if not canonical_ref and candidates:
        top = candidates[0]
        canonical_ref = f"pc:{top.get('merchant_id')}:{top.get('platform')}:{top.get('product_id')}"
        canonical_product = {
            "merchant_id": top.get("merchant_id"),
            "platform": top.get("platform"),
            "product_id": top.get("product_id"),
            "product_group_id": canonical_group_id,
        }

    candidates = candidates[:limit]
    failure_breakdown = {
        str(s.get("source")): str(s.get("reason_code"))
        for s in sources
        if str(s.get("status")) == "error"
    }
    resolved = bool(candidates)
    cache_failed = any(
        str(s.get("status")) == "error" and str(s.get("source")).startswith("products_cache")
        for s in sources
    )
    search_timed_out = any(str(s.get("reason_code")) == "upstream_timeout" for s in sources)
    first_error_detail = next(
        (str(s.get("reason_code")) for s in sources if str(s.get("status")) == "error"),
        None,
    )
    if resolved:
        reason_code = "OK"
        reason = "resolved"
    elif cache_failed:
        reason_code = "DB_ERROR"
        reason = "products_cache_failed"
    elif search_timed_out:
        reason_code = "UPSTREAM_TIMEOUT"
        reason = "search_timeout"
    elif first_error_detail:
        reason_code = _public_reason_code(first_error_detail)
        reason = "resolution_failed"
    else:
        reason_code = "NO_CANDIDATES"
        reason = "no_candidates"
    latency_ms = int((time.perf_counter() - started) * 1000)

    logger.info(
        "agent_products_resolve",
        extra={
            "event": "agent_products_resolve",
            "query": search_query,
            "merchant_scope": merchant_id,
            "latency_ms": latency_ms,
            "reason_code": reason_code,
            "candidate_count": len(candidates),
            "canonical_ref": canonical_ref,
            "resolved": resolved,
        },
    )

    return {
        "status": "success",
        "resolved": resolved,
        "reason": reason,
        "reason_code": reason_code,
        "input": {
            "merchant_id": merchant_id,
            "product_id": product_id,
            "sku_id": sku_id,
            "query": query_text or None,
        },
        "canonical_ref": canonical_ref,
        "canonical_product_group_id": canonical_group_id,
        "canonical_product": canonical_product,
        "candidates": candidates,
        "candidate_count": len(candidates),
        "metadata": {
            "source": "agent_products_resolve",
            "reason_code": reason_code,
            "reason": reason,
            "latency_ms": latency_ms,
            "sources": sources,
            "failure_breakdown": failure_breakdown,
        },
    }


# ============================================================================
# 产品归一 / Product Groups (multi-seller offers)
# ============================================================================

@router.get("/product-groups/resolve")
async def agent_resolve_product_group(
    merchant_id: str = Query(..., description="Anchor merchant ID"),
    product_id: str = Query(..., description="Merchant-scoped platform product_id (platform_product_id)"),
    platform: Optional[str] = Query(None, description="Optional platform filter (shopify/wix/...)"),
    limit: int = Query(default=50, le=200),
    context: AgentContext = Depends(get_agent_context),
) -> Dict[str, Any]:
    """
    Resolve a merchant product into a curated product_group_id, and return its members.

    This powers the gateway's "one product + many seller offers" flow.
    """
    if not _context_can_access_merchant(context, merchant_id):
        raise HTTPException(status_code=403, detail="Not authorized for this merchant")

    normalized_platform = str(platform or "").strip().lower() or None
    normalized_product_id = str(product_id or "").strip()
    if not normalized_product_id:
        raise HTTPException(status_code=400, detail="product_id is required")

    try:
        where_platform = "AND platform = :platform" if normalized_platform else ""
        values = {"merchant_id": merchant_id, "platform_product_id": normalized_product_id}
        if normalized_platform:
            values["platform"] = normalized_platform
        row = await database.fetch_one(
            f"""
            SELECT product_group_id
            FROM product_group_members
            WHERE merchant_id = :merchant_id
              AND platform_product_id = :platform_product_id
              {where_platform}
            LIMIT 1
            """,
            values,
        )
        product_group_id = (row["product_group_id"] if row else None) or None

        if not product_group_id:
            return {"status": "success", "product_group_id": None, "members": []}

        member_rows = await database.fetch_all(
            """
            SELECT pgm.merchant_id,
                   mo.business_name AS merchant_name,
                   pgm.platform,
                   pgm.platform_product_id AS product_id,
                   pgm.is_primary
            FROM product_group_members pgm
            LEFT JOIN merchant_onboarding mo ON mo.merchant_id = pgm.merchant_id
            WHERE pgm.product_group_id = :product_group_id
            ORDER BY pgm.is_primary DESC, pgm.merchant_id ASC
            LIMIT :limit
            """,
            {"product_group_id": product_group_id, "limit": limit},
        )

        members = []
        for r in member_rows or []:
            rd = dict(r) if r is not None else {}
            mid = str(rd.get("merchant_id") or "").strip()
            if not mid or not _context_can_access_merchant(context, mid):
                continue
            members.append(
                {
                    "merchant_id": mid,
                    "merchant_name": rd.get("merchant_name"),
                    "product_id": str(rd.get("product_id") or "").strip(),
                    "platform": str(rd.get("platform") or "").strip().lower() or None,
                    "is_primary": bool(rd.get("is_primary") or False),
                }
            )

        return {"status": "success", "product_group_id": product_group_id, "members": members}
    except Exception as e:
        message = str(e)
        if "product_group_members" in message and ("does not exist" in message or "relation" in message):
            return {
                "status": "success",
                "product_group_id": None,
                "members": [],
                "warning": "product_group_members table not found (migration not applied)",
            }
        raise


@router.get("/product-groups/resolve-by-product-id")
async def agent_resolve_product_group_by_product_id(
    product_id: str = Query(..., description="Any member product_id (platform_product_id)"),
    platform: Optional[str] = Query(None, description="Optional platform filter (shopify/wix/...)"),
    limit: int = Query(default=50, le=200),
    context: AgentContext = Depends(get_agent_context),
) -> Dict[str, Any]:
    """
    Resolve a product group when the caller only has a product_id (platform_product_id).

    This enables PDP links without a merchant_id by mapping product_id -> product_group_id
    via product_group_members, then returning the group members.
    """
    normalized_platform = str(platform or "").strip().lower() or None
    normalized_product_id = str(product_id or "").strip()
    if not normalized_product_id:
        raise HTTPException(status_code=400, detail="product_id is required")

    try:
        where_platform = "AND platform = :platform" if normalized_platform else ""
        params = {"platform_product_id": normalized_product_id, "limit": limit}
        if normalized_platform:
            params["platform"] = normalized_platform
        rows = await database.fetch_all(
            f"""
            SELECT product_group_id, merchant_id
            FROM product_group_members
            WHERE platform_product_id = :platform_product_id
              {where_platform}
            ORDER BY is_primary DESC, merchant_id ASC
            LIMIT :limit
            """,
            params,
        )

        resolved_group_id: Optional[str] = None
        for r in rows or []:
            rd = dict(r) if r is not None else {}
            gid = str(rd.get("product_group_id") or "").strip()
            mid = str(rd.get("merchant_id") or "").strip()
            if not gid or not mid:
                continue
            if _context_can_access_merchant(context, mid):
                resolved_group_id = gid
                break

        if not resolved_group_id:
            return {"status": "success", "product_group_id": None, "members": []}

        member_rows = await database.fetch_all(
            """
            SELECT pgm.merchant_id,
                   mo.business_name AS merchant_name,
                   pgm.platform,
                   pgm.platform_product_id AS product_id,
                   pgm.is_primary
            FROM product_group_members pgm
            LEFT JOIN merchant_onboarding mo ON mo.merchant_id = pgm.merchant_id
            WHERE pgm.product_group_id = :product_group_id
            ORDER BY pgm.is_primary DESC, pgm.merchant_id ASC
            LIMIT :limit
            """,
            {"product_group_id": resolved_group_id, "limit": limit},
        )

        members = []
        for r in member_rows or []:
            rd = dict(r) if r is not None else {}
            mid = str(rd.get("merchant_id") or "").strip()
            if not mid or not _context_can_access_merchant(context, mid):
                continue
            members.append(
                {
                    "merchant_id": mid,
                    "merchant_name": rd.get("merchant_name"),
                    "product_id": str(rd.get("product_id") or "").strip(),
                    "platform": str(rd.get("platform") or "").strip().lower() or None,
                    "is_primary": bool(rd.get("is_primary") or False),
                }
            )

        return {"status": "success", "product_group_id": resolved_group_id, "members": members}
    except Exception as e:
        message = str(e)
        if "product_group_members" in message and ("does not exist" in message or "relation" in message):
            return {
                "status": "success",
                "product_group_id": None,
                "members": [],
                "warning": "product_group_members table not found (migration not applied)",
            }
        raise


@router.get("/product-groups/{product_group_id}")
async def agent_get_product_group(
    product_group_id: str,
    limit: int = Query(default=50, le=200),
    context: AgentContext = Depends(get_agent_context),
) -> Dict[str, Any]:
    """
    Fetch product-group members by product_group_id.
    """
    normalized_group_id = str(product_group_id or "").strip()
    if not normalized_group_id:
        raise HTTPException(status_code=400, detail="product_group_id is required")

    try:
        member_rows = await database.fetch_all(
            """
            SELECT pgm.merchant_id,
                   mo.business_name AS merchant_name,
                   pgm.platform,
                   pgm.platform_product_id AS product_id,
                   pgm.is_primary
            FROM product_group_members pgm
            LEFT JOIN merchant_onboarding mo ON mo.merchant_id = pgm.merchant_id
            WHERE pgm.product_group_id = :product_group_id
            ORDER BY pgm.is_primary DESC, pgm.merchant_id ASC
            LIMIT :limit
            """,
            {"product_group_id": normalized_group_id, "limit": limit},
        )

        members = []
        for r in member_rows or []:
            rd = dict(r) if r is not None else {}
            mid = str(rd.get("merchant_id") or "").strip()
            if not mid or not _context_can_access_merchant(context, mid):
                continue
            members.append(
                {
                    "merchant_id": mid,
                    "merchant_name": rd.get("merchant_name"),
                    "product_id": str(rd.get("product_id") or "").strip(),
                    "platform": str(rd.get("platform") or "").strip().lower() or None,
                    "is_primary": bool(rd.get("is_primary") or False),
                }
            )

        return {"status": "success", "product_group_id": normalized_group_id, "members": members}
    except Exception as e:
        message = str(e)
        if "product_group_members" in message and ("does not exist" in message or "relation" in message):
            return {
                "status": "success",
                "product_group_id": normalized_group_id,
                "members": [],
                "warning": "product_group_members table not found (migration not applied)",
            }
        raise


@router.get("/products/{merchant_id}/{product_id}")
async def agent_get_product(
    merchant_id: str,
    product_id: str,
    req: Request,
    background_tasks: BackgroundTasks,
    context: AgentContext = Depends(get_agent_context),
):
    """获取单个产品详情"""
    try:
        if merchant_id == EXTERNAL_SEED_MERCHANT_ID:
            prod = await _load_external_seed_product_by_product_id(req=req, product_id=product_id)
            if not prod:
                raise HTTPException(status_code=404, detail="Product not found")
            background_tasks.add_task(
                log_agent_request,
                context=context,
                status_code=200,
                merchant_id=merchant_id,
            )
            return {"status": "success", "product": prod}

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
        if merchant_id == EXTERNAL_SEED_MERCHANT_ID:
            raise HTTPException(status_code=400, detail="EXTERNAL_PRODUCT_CHECKOUT_DISABLED")

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
    if order_request.merchant_id == EXTERNAL_SEED_MERCHANT_ID:
        raise HTTPException(status_code=400, detail="EXTERNAL_PRODUCT_CHECKOUT_DISABLED")

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

        brief_id = getattr(order_request, "brief_id", None) or None
        brief_schema_version = getattr(order_request, "brief_schema_version", None) or None
        try:
            meta = getattr(order_request, "metadata", None)
            if isinstance(meta, dict):
                if not brief_id:
                    brief_id = meta.get("brief_id") or meta.get("briefId") or None
                if not brief_schema_version:
                    brief_schema_version = meta.get("brief_schema_version") or meta.get("briefSchemaVersion") or None
        except Exception:
            pass

        emit_best_effort(
            event_type=EVENT_CHECKOUT_ATTEMPTED,
            payload={
                "stage": "order_create",
                "merchant_id": getattr(order_request, "merchant_id", None),
                "quote_id": getattr(order_request, "quote_id", None),
                "items_count": len(getattr(order_request, "items", None) or []),
                "agent_id": getattr(context, "agent_id", None),
                **({"brief_id": brief_id} if brief_id else {}),
                **({"brief_schema_version": brief_schema_version} if brief_schema_version else {}),
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

                # Decision/Brief join key (best-effort): carry forward from quote snapshot.
                try:
                    if not order_request.metadata:
                        order_request.metadata = {}
                    if isinstance(order_request.metadata, dict) and isinstance(snap, dict):
                        smeta = snap.get("metadata") if isinstance(snap.get("metadata"), dict) else {}
                        brief_meta = smeta.get("brief") if isinstance(smeta.get("brief"), dict) else {}
                        if brief_meta and not order_request.metadata.get("brief_id") and brief_meta.get("brief_id"):
                            order_request.metadata["brief_id"] = brief_meta.get("brief_id")
                            if brief_meta.get("brief_schema_version"):
                                order_request.metadata["brief_schema_version"] = brief_meta.get("brief_schema_version")
                except Exception:
                    pass

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
                        else order_request.shipping_address
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
            # Decision/Brief join key (preferred): allow explicit fields to flow into metadata.
            if getattr(order_request, "brief_id", None) and not order_request.metadata.get("brief_id"):
                order_request.metadata["brief_id"] = getattr(order_request, "brief_id")
            if getattr(order_request, "brief_schema_version", None) and not order_request.metadata.get("brief_schema_version"):
                order_request.metadata["brief_schema_version"] = getattr(order_request, "brief_schema_version")

            token_payload = getattr(context, "checkout_token_payload", None)
            if isinstance(token_payload, dict):
                for key in ("intent_id", "buyer_ref", "job_id", "market", "locale", "brief_id", "brief_schema_version"):
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

        # Safety: prevent silent default-SKU checkout for Shopify.
        try:
            store_info = await get_primary_store(order_request.merchant_id)
        except Exception:
            store_info = None
        if str((store_info or {}).get("platform") or "").lower() == "shopify":
            missing_variant = False
            for item in (order_request.items or []):
                if not getattr(item, "variant_id", None) or not str(getattr(item, "variant_id", "")).strip():
                    missing_variant = True
                    break
            if missing_variant:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "error": "VARIANT_REQUIRED",
                        "message": "variant_id is required for Shopify checkout; select a SKU/variant before adding to cart.",
                    },
                )

        # 调用标准订单创建 (serialize per-merchant to avoid concurrent order creation hazards)
        async with _get_order_create_lock(order_request.merchant_id):
            try:
                order_response = await order_routes_module.create_new_order(order_request, background_tasks)
            except Exception as e:
                # Emergency self-heal: if a pooled asyncpg connection is in a poisoned/busy state,
                # reset the pool and retry once. Idempotency keys (when present) prevent duplicates.
                if is_asyncpg_busy_error(e):
                    try:
                        logger.warning(
                            "[agent_orders_create] asyncpg connection busy; resetting DB pool and retrying once"
                        )
                        await database.disconnect()
                        await database.connect()
                    except Exception:
                        pass
                    try:
                        order_response = await order_routes_module.create_new_order(order_request, background_tasks)
                    except Exception as e2:
                        if is_asyncpg_busy_error(e2):
                            raise db_busy_http_exception()
                        raise
                else:
                    raise

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

            brief_id = None
            brief_schema_version = None
            try:
                meta = getattr(order_request, "metadata", None)
                if isinstance(meta, dict):
                    brief_id = meta.get("brief_id") or meta.get("briefId") or None
                    brief_schema_version = meta.get("brief_schema_version") or meta.get("briefSchemaVersion") or None
            except Exception:
                pass

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
                    **({"brief_id": brief_id} if brief_id else {}),
                    **({"brief_schema_version": brief_schema_version} if brief_schema_version else {}),
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
                payment_action = payment_action_obj.model_dump()
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
        # If the handler wrapped an asyncpg busy error into a 500, surface a retryable 503 instead.
        try:
            if is_asyncpg_busy_error(e):
                raise db_busy_http_exception()
        except Exception:
            pass
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

            brief_id = None
            brief_schema_version = None
            try:
                meta = getattr(order_request, "metadata", None)
                if isinstance(meta, dict):
                    brief_id = meta.get("brief_id") or meta.get("briefId") or None
                    brief_schema_version = meta.get("brief_schema_version") or meta.get("briefSchemaVersion") or None
            except Exception:
                pass

            emit_best_effort(
                event_type=EVENT_CHECKOUT_FAILED,
                payload={
                    "stage": "order_create",
                    "merchant_id": getattr(order_request, "merchant_id", None),
                    "quote_id": getattr(order_request, "quote_id", None),
                    "error_status": getattr(e, "status_code", None),
                    "error": str(e.detail)[:500],
                    **({"brief_id": brief_id} if brief_id else {}),
                    **({"brief_schema_version": brief_schema_version} if brief_schema_version else {}),
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
        if is_asyncpg_busy_error(e):
            raise db_busy_http_exception()
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

            brief_id = None
            brief_schema_version = None
            try:
                meta = getattr(order_request, "metadata", None)
                if isinstance(meta, dict):
                    brief_id = meta.get("brief_id") or meta.get("briefId") or None
                    brief_schema_version = meta.get("brief_schema_version") or meta.get("briefSchemaVersion") or None
            except Exception:
                pass

            emit_best_effort(
                event_type=EVENT_CHECKOUT_FAILED,
                payload={
                    "stage": "order_create",
                    "merchant_id": getattr(order_request, "merchant_id", None),
                    "quote_id": getattr(order_request, "quote_id", None),
                    "error_status": 500,
                    "error": str(e)[:500],
                    **({"brief_id": brief_id} if brief_id else {}),
                    **({"brief_schema_version": brief_schema_version} if brief_schema_version else {}),
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
        from routes.order_routes import create_shopify_order, get_order, log_order_event, mark_order_paid
        from services.merchant_store_service import get_primary_store
        from services.pcs_evidence_pack_service import create_order_snapshot_evidence_pack
        
        order = await get_order(order_id)
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        if not context.can_access_merchant(order["merchant_id"]):
            raise HTTPException(status_code=403, detail="Not authorized for this order")

        store_info = await get_primary_store(order["merchant_id"])
        can_shopify_sync = bool(store_info) and str((store_info or {}).get("platform") or "").strip().lower() == "shopify" and bool(
            str((store_info or {}).get("api_key") or "").strip()
        )

        already_paid = order.get("payment_status") == "paid"
        if already_paid:
            if not order.get("shopify_order_id") and can_shopify_sync:
                try:
                    await log_order_event(
                        event_type="shopify_sync_retry_requested",
                        order_id=order_id,
                        merchant_id=order["merchant_id"],
                        metadata={"requested_by": "agent_confirm_payment"},
                    )
                except Exception:
                    pass

                background_tasks.add_task(create_shopify_order, order_id)

                background_tasks.add_task(
                    log_agent_request,
                    context=context,
                    status_code=200,
                    merchant_id=order["merchant_id"],
                    order_id=order_id,
                )

                return {
                    "status": "success",
                    "message": "Order already paid; Shopify sync initiated",
                    "order_id": order_id,
                    "payment_intent_id": order.get("payment_intent_id"),
                    "shopify_sync": "initiated",
                }

            background_tasks.add_task(
                log_agent_request,
                context=context,
                status_code=200,
                merchant_id=order["merchant_id"],
                order_id=order_id,
            )

            return {
                "status": "success",
                "message": "Order already paid",
                "order_id": order_id,
                "payment_intent_id": order.get("payment_intent_id"),
                "shopify_sync": "already_linked" if order.get("shopify_order_id") else ("not_configured" if not can_shopify_sync else "missing_shopify_order_id"),
                "shopify_order_id": order.get("shopify_order_id"),
            }

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

        # 记录支付成功事件（best-effort; do not fail confirm if event logging hits DB busy）
        try:
            await log_order_event(
                event_type="payment_succeeded",
                order_id=order_id,
                merchant_id=order["merchant_id"],
                metadata={
                    "payment_intent_id": order.get("payment_intent_id"),
                    "amount": float(order["total"]),
                    "currency": order["currency"],
                    "confirmed_by": "agent",
                },
            )
        except Exception:
            pass

        if can_shopify_sync:
            background_tasks.add_task(create_shopify_order, order_id)
        
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
            "message": "Payment confirmed, Shopify order creation initiated" if can_shopify_sync else "Payment confirmed",
            "order_id": order_id,
            "payment_intent_id": order.get("payment_intent_id"),
            "shopify_sync": "initiated" if can_shopify_sync else "not_configured",
        }
        
    except HTTPException:
        raise
    except Exception as e:
        if is_asyncpg_busy_error(e):
            raise db_busy_http_exception()
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
