"""
Employee Products (MVP)

Purpose:
- Provide an employee-facing products list/search and product detail view backed by products_cache.
- Designed to degrade gracefully while metrics/supply signals are sparse in early stages.

NOTE (v0):
- This is not the final 10M-scale read model. It is a bridge that enables the
  employee portal UX while the `employee_products_index` rollups are built.
"""

from typing import Any, Dict, List, Optional
import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
import uuid

from db.database import database
from db.products import products_cache
from models.standard_product import StandardProduct
from utils.auth import get_current_employee

from services.external_offers_service import resolve_external_offer
from services.outbound_links_service import (
    DEFAULT_DISCLOSURE_TEXT,
    DEFAULT_UTM_TEMPLATE,
    _is_domain_allowed,
    apply_utm,
    make_redirect_token,
)

router = APIRouter(prefix="/employee/products", tags=["employee-products"])

def _to_iso(val: Any) -> Optional[str]:
    if val is None:
        return None
    try:
        iso = getattr(val, "isoformat", None)
        if callable(iso):
            return iso()
    except Exception:
        pass
    try:
        return str(val)
    except Exception:
        return None


def _ensure_dict(val: Any) -> Dict[str, Any]:
    """
    products_cache.product_data is expected to be JSON, but some environments may store it as a JSON string.
    Normalize to a dict so downstream parsing doesn't 500.
    """
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


def _as_product_card(row: Dict[str, Any]) -> Dict[str, Any]:
    merchant_id = row.get("merchant_id")
    platform = row.get("platform")
    platform_product_id = row.get("platform_product_id")
    product_data = _ensure_dict(row.get("product_data"))

    try:
        sp = StandardProduct.parse_obj(product_data)
        title = sp.title
        image_url = sp.image_url or (sp.images[0] if sp.images else None)
        product_id = sp.product_id or sp.id or platform_product_id
        variants = sp.variants or []
        currency = getattr(sp, "currency", None) or product_data.get("currency")
        price = getattr(sp, "price", None) if hasattr(sp, "price") else product_data.get("price")
    except Exception:
        title = product_data.get("title") or product_data.get("name") or platform_product_id
        image_url = product_data.get("image_url") or None
        product_id = product_data.get("product_id") or product_data.get("id") or platform_product_id
        variants = product_data.get("variants") or []
        currency = product_data.get("currency")
        price = product_data.get("price")

    return {
        "product_key": f"{merchant_id}|{platform}|{platform_product_id}",
        "merchant_id": merchant_id,
        "platform": platform,
        "platform_product_id": platform_product_id,
        "product_id": product_id,
        "title": title,
        "image_url": image_url,
        "variants_count": len(variants) if isinstance(variants, list) else 0,
        "price": {"value": price, "currency": currency},
        "cached_at": _to_iso(row.get("cached_at")),
        "expires_at": _to_iso(row.get("expires_at")),
    }

async def _ensure_external_seeds_table() -> None:
    """
    Minimal storage for employee-managed external seeds.
    We intentionally keep this as runtime DDL for MVP to avoid blocking on migration runners.
    """
    await database.execute(
        """
        CREATE TABLE IF NOT EXISTS external_product_seeds (
          id TEXT PRIMARY KEY,
          market TEXT NOT NULL,
          tool TEXT NOT NULL DEFAULT '*',
          destination_url TEXT NOT NULL,
          canonical_url TEXT NULL,
          domain TEXT NULL,
          title TEXT NULL,
          image_url TEXT NULL,
          price_amount DOUBLE PRECISION NULL,
          price_currency TEXT NULL,
          availability TEXT NULL,
          status TEXT NOT NULL DEFAULT 'active',
          notes TEXT NULL,
          created_by_employee_id TEXT NULL,
          attached_product_key TEXT NULL,
          attached_variant_id TEXT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )
    await database.execute(
        "CREATE INDEX IF NOT EXISTS idx_external_product_seeds_status ON external_product_seeds(status);"
    )
    await database.execute(
        "CREATE INDEX IF NOT EXISTS idx_external_product_seeds_attached ON external_product_seeds(attached_product_key, attached_variant_id);"
    )
    await database.execute(
        "CREATE INDEX IF NOT EXISTS idx_external_product_seeds_domain ON external_product_seeds(domain);"
    )


def _normalize_market(market: Optional[str]) -> str:
    m = str(market or "").strip().upper()
    return m or "US"


def _normalize_tool(tool: Optional[str]) -> str:
    t = str(tool or "").strip()
    return t or "*"


def _require_http_url(url: str) -> str:
    u = str(url or "").strip()
    if not (u.startswith("http://") or u.startswith("https://")):
        raise HTTPException(status_code=400, detail="INVALID_URL")
    return u


def _request_base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _seed_id() -> str:
    return f"eps_{uuid.uuid4().hex[:24]}"


async def _make_redirect_url(
    *,
    request: Request,
    market: str,
    tool: str,
    destination_url: str,
    utm_template: Optional[str],
    ctx: Dict[str, Any],
) -> str:
    dest_with_utm = apply_utm(destination_url, utm_template or DEFAULT_UTM_TEMPLATE, {"market": market, "tool": tool})
    if not await _is_domain_allowed(market=market, destination_url=dest_with_utm):
        raise HTTPException(status_code=400, detail="DOMAIN_NOT_ALLOWED")
    token = make_redirect_token(
        {
            "market": market,
            "tool": tool,
            "dest": dest_with_utm,
            "ctx": ctx,
        }
    )
    return f"{_request_base_url(request)}/r?token={token}"


class CreateExternalSeedRequest(BaseModel):
    destination_url: str = Field(..., min_length=1)
    market: Optional[str] = None
    tool: Optional[str] = None
    notes: Optional[str] = None
    attach_product_key: Optional[str] = None
    attach_variant_id: Optional[str] = None
    utm_template: Optional[str] = None


@router.get("/external-seeds")
async def list_external_seeds(
    attached: Optional[bool] = Query(default=None),
    status: str = Query(default="active"),
    limit: int = Query(default=50, ge=1, le=200),
    current_user: dict = Depends(get_current_employee),
):
    await _ensure_external_seeds_table()
    where = ["status = :status"]
    values: Dict[str, Any] = {"status": status, "limit": limit}
    if attached is True:
        where.append("attached_product_key IS NOT NULL")
    elif attached is False:
        where.append("attached_product_key IS NULL")

    rows = await database.fetch_all(
        f"""
        SELECT
          id, market, tool, destination_url, canonical_url, domain, title, image_url,
          price_amount, price_currency, availability,
          status, notes, created_by_employee_id,
          attached_product_key, attached_variant_id,
          created_at, updated_at
        FROM external_product_seeds
        WHERE {" AND ".join(where)}
        ORDER BY created_at DESC
        LIMIT :limit
        """,
        values,
    )
    items = []
    for r in rows:
        r = dict(r)
        items.append(
            {
                "id": r.get("id"),
                "market": r.get("market"),
                "tool": r.get("tool"),
                "destination_url": r.get("destination_url"),
                "canonical_url": r.get("canonical_url"),
                "domain": r.get("domain"),
                "title": r.get("title"),
                "image_url": r.get("image_url"),
                "price": {"amount": r.get("price_amount"), "currency": r.get("price_currency")},
                "availability": r.get("availability"),
                "status": r.get("status"),
                "notes": r.get("notes"),
                "created_by_employee_id": r.get("created_by_employee_id"),
                "attached_product_key": r.get("attached_product_key"),
                "attached_variant_id": r.get("attached_variant_id"),
                "created_at": r.get("created_at").isoformat() if r.get("created_at") else None,
                "updated_at": r.get("updated_at").isoformat() if r.get("updated_at") else None,
            }
        )
    return {"status": "success", "items": items}


@router.post("/external-seeds")
async def create_external_seed(
    body: CreateExternalSeedRequest,
    request: Request,
    current_user: dict = Depends(get_current_employee),
):
    await _ensure_external_seeds_table()
    market = _normalize_market(body.market)
    tool = _normalize_tool(body.tool)
    dest = _require_http_url(body.destination_url)
    seed_id = _seed_id()

    employee_id = current_user.get("employee_id") or current_user.get("employeeId")
    attached_product_key = (body.attach_product_key or "").strip() or None
    attached_variant_id = (body.attach_variant_id or "").strip() or None
    if attached_variant_id == "∅":
        attached_variant_id = "∅"

    snapshot = None
    try:
        snapshot = await resolve_external_offer(market=market, url=dest, force_refresh=False)
    except Exception:
        snapshot = None

    canonical_url = getattr(snapshot, "canonical_url", None) if snapshot else None
    domain = getattr(snapshot, "domain", None) if snapshot else None
    title = getattr(snapshot, "title", None) if snapshot else None
    image_url = getattr(snapshot, "image_url", None) if snapshot else None
    price_amount = getattr(snapshot, "price_amount", None) if snapshot else None
    price_currency = getattr(snapshot, "price_currency", None) if snapshot else None
    availability = getattr(snapshot, "availability", None) if snapshot else None

    await database.execute(
        """
        INSERT INTO external_product_seeds (
          id, market, tool, destination_url, canonical_url, domain, title, image_url,
          price_amount, price_currency, availability,
          status, notes, created_by_employee_id, attached_product_key, attached_variant_id
        ) VALUES (
          :id, :market, :tool, :destination_url, :canonical_url, :domain, :title, :image_url,
          :price_amount, :price_currency, :availability,
          'active', :notes, :created_by_employee_id, :attached_product_key, :attached_variant_id
        )
        """,
        {
            "id": seed_id,
            "market": market,
            "tool": tool,
            "destination_url": dest,
            "canonical_url": canonical_url,
            "domain": domain,
            "title": title,
            "image_url": image_url,
            "price_amount": price_amount,
            "price_currency": price_currency,
            "availability": availability,
            "notes": body.notes,
            "created_by_employee_id": str(employee_id) if employee_id else None,
            "attached_product_key": attached_product_key,
            "attached_variant_id": attached_variant_id,
        },
    )

    redirect_url = await _make_redirect_url(
        request=request,
        market=market,
        tool=tool,
        destination_url=canonical_url or dest,
        utm_template=body.utm_template,
        ctx={
            "seedId": seed_id,
            **({"productKey": attached_product_key} if attached_product_key else {}),
            **({"variantId": attached_variant_id} if attached_variant_id else {}),
        },
    )

    return {
        "status": "success",
        "seed": {
            "id": seed_id,
            "market": market,
            "tool": tool,
            "destination_url": dest,
            "canonical_url": canonical_url,
            "domain": domain,
            "title": title,
            "image_url": image_url,
            "price": {"amount": price_amount, "currency": price_currency},
            "availability": availability,
            "notes": body.notes,
            "attached_product_key": attached_product_key,
            "attached_variant_id": attached_variant_id,
        },
        "action": {"type": "redirect", "redirect_url": redirect_url, "disclosure_text": DEFAULT_DISCLOSURE_TEXT},
    }


@router.get("/external-seeds/{seed_id}")
async def get_external_seed(
    seed_id: str,
    request: Request,
    current_user: dict = Depends(get_current_employee),
):
    await _ensure_external_seeds_table()
    row = await database.fetch_one(
        "SELECT * FROM external_product_seeds WHERE id = :id",
        {"id": seed_id},
    )
    if not row:
        raise HTTPException(status_code=404, detail="SEED_NOT_FOUND")
    row = dict(row)
    redirect_url = await _make_redirect_url(
        request=request,
        market=row.get("market"),
        tool=row.get("tool"),
        destination_url=row.get("canonical_url") or row.get("destination_url"),
        utm_template=None,
        ctx={
            "seedId": row.get("id"),
            **({"productKey": row.get("attached_product_key")} if row.get("attached_product_key") else {}),
            **({"variantId": row.get("attached_variant_id")} if row.get("attached_variant_id") else {}),
        },
    )
    return {
        "status": "success",
        "seed": {
            "id": row.get("id"),
            "market": row.get("market"),
            "tool": row.get("tool"),
            "destination_url": row.get("destination_url"),
            "canonical_url": row.get("canonical_url"),
            "domain": row.get("domain"),
            "title": row.get("title"),
            "image_url": row.get("image_url"),
            "price": {"amount": row.get("price_amount"), "currency": row.get("price_currency")},
            "availability": row.get("availability"),
            "status": row.get("status"),
            "notes": row.get("notes"),
            "attached_product_key": row.get("attached_product_key"),
            "attached_variant_id": row.get("attached_variant_id"),
            "created_at": row.get("created_at").isoformat() if row.get("created_at") else None,
            "updated_at": row.get("updated_at").isoformat() if row.get("updated_at") else None,
        },
        "action": {"type": "redirect", "redirect_url": redirect_url, "disclosure_text": DEFAULT_DISCLOSURE_TEXT},
    }


class AttachSeedRequest(BaseModel):
    product_key: str = Field(..., min_length=3)
    variant_id: Optional[str] = None


@router.post("/external-seeds/{seed_id}/attach")
async def attach_external_seed(
    seed_id: str,
    body: AttachSeedRequest,
    current_user: dict = Depends(get_current_employee),
):
    await _ensure_external_seeds_table()
    pk = (body.product_key or "").strip()
    if pk.count("|") != 2:
        raise HTTPException(status_code=400, detail="INVALID_PRODUCT_KEY")
    vid = (body.variant_id or "").strip() or "∅"
    await database.execute(
        """
        UPDATE external_product_seeds
        SET attached_product_key = :pk,
            attached_variant_id = :vid,
            updated_at = NOW()
        WHERE id = :id
        """,
        {"pk": pk, "vid": vid, "id": seed_id},
    )
    return {"status": "success"}


@router.get("/{product_key}/external-links")
async def list_attached_external_links(
    product_key: str,
    request: Request,
    variant_id: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    current_user: dict = Depends(get_current_employee),
):
    await _ensure_external_seeds_table()
    parts = [p.strip() for p in (product_key or "").split("|")]
    if len(parts) != 3:
        raise HTTPException(status_code=400, detail="INVALID_PRODUCT_KEY")

    values: Dict[str, Any] = {"pk": product_key, "limit": limit}
    where = "attached_product_key = :pk AND status = 'active'"
    if variant_id:
        values["vid"] = str(variant_id).strip()
        where += " AND attached_variant_id = :vid"

    rows = await database.fetch_all(
        f"""
        SELECT id, market, tool, destination_url, canonical_url, domain, title, image_url,
               price_amount, price_currency, availability,
               notes, attached_variant_id, created_at
        FROM external_product_seeds
        WHERE {where}
        ORDER BY created_at DESC
        LIMIT :limit
        """,
        values,
    )

    items = []
    for r in rows:
        r = dict(r)
        redirect_url = await _make_redirect_url(
            request=request,
            market=r.get("market"),
            tool=r.get("tool"),
            destination_url=r.get("canonical_url") or r.get("destination_url"),
            utm_template=None,
            ctx={
                "seedId": r.get("id"),
                "productKey": product_key,
                "variantId": r.get("attached_variant_id") or "∅",
            },
        )
        items.append(
            {
                "id": r.get("id"),
                "market": r.get("market"),
                "tool": r.get("tool"),
                "destination_url": r.get("destination_url"),
                "canonical_url": r.get("canonical_url"),
                "domain": r.get("domain"),
                "title": r.get("title"),
                "image_url": r.get("image_url"),
                "price": {"amount": r.get("price_amount"), "currency": r.get("price_currency")},
                "availability": r.get("availability"),
                "notes": r.get("notes"),
                "attached_variant_id": r.get("attached_variant_id") or "∅",
                "created_at": r.get("created_at").isoformat() if r.get("created_at") else None,
                "action": {"type": "redirect", "redirect_url": redirect_url, "disclosure_text": DEFAULT_DISCLOSURE_TEXT},
            }
        )
    return {"status": "success", "items": items}


class CreateAttachedExternalLinkRequest(BaseModel):
    destination_url: str = Field(..., min_length=1)
    market: Optional[str] = None
    tool: Optional[str] = None
    variant_id: Optional[str] = None
    notes: Optional[str] = None


@router.post("/{product_key}/external-links")
async def create_attached_external_link(
    product_key: str,
    body: CreateAttachedExternalLinkRequest,
    request: Request,
    current_user: dict = Depends(get_current_employee),
):
    pk = (product_key or "").strip()
    if pk.count("|") != 2:
        raise HTTPException(status_code=400, detail="INVALID_PRODUCT_KEY")
    return await create_external_seed(
        CreateExternalSeedRequest(
            destination_url=body.destination_url,
            market=body.market,
            tool=body.tool,
            notes=body.notes,
            attach_product_key=pk,
            attach_variant_id=(body.variant_id or "∅"),
        ),
        request=request,
        current_user=current_user,
    )


@router.get("/search")
async def search_products(
    q: Optional[str] = Query(default=None, description="Search by product_id/platform_product_id/title (best-effort)"),
    merchant_id: Optional[str] = Query(default=None),
    platform: Optional[str] = Query(default=None),
    limit: int = Query(default=30, ge=1, le=100),
    after_id: Optional[int] = Query(default=None, description="Cursor: return rows with id < after_id"),
    current_user: dict = Depends(get_current_employee),
):
    """
    Employee-facing product search over products_cache.

    v0 behavior:
    - Sort: most recently inserted cache rows (id desc).
    - Pagination: keyset on products_cache.id (after_id).
    - Search: best-effort exact id match + title ILIKE when supported.
    """
    where = []
    values: Dict[str, Any] = {"limit": limit}

    if merchant_id:
        where.append("merchant_id = :merchant_id")
        values["merchant_id"] = merchant_id
    if platform:
        where.append("platform = :platform")
        values["platform"] = platform
    if after_id is not None:
        where.append("id < :after_id")
        values["after_id"] = after_id

    base = "SELECT id, merchant_id, platform, platform_product_id, product_data, cached_at, expires_at FROM products_cache"
    clause = f" WHERE {' AND '.join(where)}" if where else ""
    order_limit = " ORDER BY id DESC LIMIT :limit"

    rows: List[Dict[str, Any]] = []
    if q:
        q = q.strip()

    if q:
        # Best-effort: attempt title ILIKE + JSON product_id match (Postgres).
        try:
            q_clause = (
                " (platform_product_id = :q"
                " OR product_data->>'product_id' = :q"
                " OR product_data->>'id' = :q"
                " OR product_data->>'title' ILIKE :q_like"
                " OR product_data->>'name' ILIKE :q_like)"
            )
            values["q"] = q
            values["q_like"] = f"%{q}%"
            rows = await database.fetch_all(
                f"{base}{clause}{' AND ' if clause else ' WHERE '}{q_clause}{order_limit}",
                values,
            )
        except Exception:
            # Fallback: exact matches only.
            q_clause = " (platform_product_id = :q OR product_data->>'product_id' = :q OR product_data->>'id' = :q)"
            values["q"] = q
            rows = await database.fetch_all(
                f"{base}{clause}{' AND ' if clause else ' WHERE '}{q_clause}{order_limit}",
                values,
            )
    else:
        rows = await database.fetch_all(f"{base}{clause}{order_limit}", values)

    cards: List[Dict[str, Any]] = []
    debug_errors: List[str] = []
    for r in rows:
        try:
            cards.append(_as_product_card(dict(r)))
        except Exception as exc:
            debug_errors.append(f"card_parse_failed: {str(exc)}")
    next_after_id = int(rows[-1]["id"]) if rows else None

    return {
        "status": "degraded" if debug_errors else "success",
        "items": cards,
        "next": {"after_id": next_after_id},
        **({"debug_errors": debug_errors[:10]} if debug_errors else {}),
    }


@router.get("/{product_key}")
async def get_product_by_key(
    product_key: str,
    current_user: dict = Depends(get_current_employee),
):
    """
    Product detail by product_key, where product_key = "{merchant_id}|{platform}|{platform_product_id}".
    """
    parts = [p.strip() for p in (product_key or "").split("|")]
    if len(parts) != 3:
        raise HTTPException(status_code=400, detail="INVALID_PRODUCT_KEY")

    merchant_id, platform, platform_product_id = parts
    row = await database.fetch_one(
        products_cache.select().where(
            (products_cache.c.merchant_id == merchant_id)
            & (products_cache.c.platform == platform)
            & (products_cache.c.platform_product_id == platform_product_id)
        )
    )
    if not row:
        raise HTTPException(status_code=404, detail="PRODUCT_NOT_FOUND")

    row = dict(row)
    product_data = _ensure_dict(row.get("product_data"))

    # Parse best-effort StandardProduct for normalized fields, but return the raw JSON as well.
    try:
        sp = StandardProduct.parse_obj(product_data)
        normalized = sp.dict()
    except Exception:
        normalized = None

    return {
        "status": "success",
        "product_key": product_key,
        "merchant_id": merchant_id,
        "platform": platform,
        "platform_product_id": platform_product_id,
        "cached_at": _to_iso(row.get("cached_at")),
        "expires_at": _to_iso(row.get("expires_at")),
        "product": normalized,
        "raw": product_data,
        # v0 placeholders for the employee page; these will be replaced by rollups/index later.
        "metrics": {
            "sales_7d": 0,
            "sales_30d": 0,
            "gmv_7d": {"currency": product_data.get("currency") or "USD", "amount": 0},
            "gmv_30d": {"currency": product_data.get("currency") or "USD", "amount": 0},
            "merchants_selling": 1,
        },
    }


@router.get("/{product_key}/reviews")
async def list_product_reviews(
    product_key: str,
    variant_id: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    current_user: dict = Depends(get_current_employee),
):
    """
    List reviews for a product_key (merchant/platform/platform_product_id).

    Notes:
    - Uses Reviews Center table `product_reviews` when present.
    - If the table is missing in an environment, returns an empty list (degraded) instead of 500.
    """
    parts = [p.strip() for p in (product_key or "").split("|")]
    if len(parts) != 3:
        raise HTTPException(status_code=400, detail="INVALID_PRODUCT_KEY")

    merchant_id, platform, platform_product_id = parts
    values: Dict[str, Any] = {
        "merchant_id": merchant_id,
        "platform": platform,
        "platform_product_id": platform_product_id,
        "limit": limit,
    }
    variant_clause = ""
    if variant_id:
        values["variant_id"] = str(variant_id).strip()
        variant_clause = " AND (variant_id = :variant_id)"

    try:
        rows = await database.fetch_all(
            f"""
            SELECT
              id,
              merchant_id,
              platform,
              platform_product_id,
              variant_id,
              rating,
              title,
              body,
              body_redacted,
              status,
              risk_flags,
              media_count,
              created_at,
              updated_at
            FROM product_reviews
            WHERE merchant_id = :merchant_id
              AND platform = :platform
              AND platform_product_id = :platform_product_id
              {variant_clause}
            ORDER BY created_at DESC
            LIMIT :limit
            """,
            values,
        )
    except Exception as exc:
        # Degrade gracefully in deployments where Reviews Center tables are not present.
        msg = str(exc)
        if "product_reviews" in msg and ("does not exist" in msg or "UndefinedTable" in msg):
            return {
                "status": "degraded",
                "items": [],
                "debug_errors": ["product_reviews table missing"],
            }
        return {
            "status": "degraded",
            "items": [],
            "debug_errors": [f"reviews query failed: {msg[:200]}"],
        }

    items = []
    status_counts: Dict[str, int] = {}
    for r in rows:
        r = dict(r)
        st = (r.get("status") or "unknown").strip().lower()
        status_counts[st] = status_counts.get(st, 0) + 1
        items.append(
            {
                "id": int(r["id"]),
                "merchant_id": r.get("merchant_id"),
                "platform": r.get("platform"),
                "platform_product_id": r.get("platform_product_id"),
                "variant_id": r.get("variant_id"),
                "rating": r.get("rating"),
                "title": r.get("title"),
                "body": r.get("body_redacted") or r.get("body"),
                "status": r.get("status"),
                "media_count": r.get("media_count") or 0,
                "risk_flags": r.get("risk_flags"),
                "created_at": r.get("created_at").isoformat() if r.get("created_at") else None,
                "updated_at": r.get("updated_at").isoformat() if r.get("updated_at") else None,
            }
        )

    return {
        "status": "success",
        "items": items,
        "counts": {
            "total": len(items),
            "by_status": status_counts,
        },
    }
