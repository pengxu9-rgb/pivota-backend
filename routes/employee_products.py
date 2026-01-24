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
import asyncio
import csv
import io
import json
import hashlib
import re
from urllib.parse import urlparse
from urllib.parse import parse_qsl, urlencode, urlunparse
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
import uuid

from db.database import database
from db.products import products_cache
from db.product_enrichment import get_enrichment, upsert_enrichment
from models.standard_product import StandardProduct
from utils.auth import get_current_employee, require_employee_permissions

from services.external_offers_service import resolve_external_offer
from services.outbound_links_service import (
    DEFAULT_DISCLOSURE_TEXT,
    DEFAULT_UTM_TEMPLATE,
    _is_domain_allowed,
    apply_utm,
    make_redirect_token,
)
from db.reviews_center import product_reviews
from services.reviews_service import GLOBAL_IMPORT_MERCHANT_ID, build_product_key, build_sku_key

router = APIRouter(prefix="/employee/products", tags=["employee-products"])

_EXTERNAL_SEEDS_TABLE_READY = False
_EXTERNAL_SEEDS_TABLE_LOCK = asyncio.Lock()

_EXTERNAL_SEED_IMPORT_TASKS_TABLE_READY = False
_EXTERNAL_SEED_IMPORT_TASKS_TABLE_LOCK = asyncio.Lock()

EXTERNAL_SEED_MERCHANT_ID = "external_seed"
EXTERNAL_SEED_PLATFORM = "external"

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

async def _fetch_latest_products_cache_row(
    *,
    merchant_id: str,
    platform: str,
    platform_product_id: str,
    include_expired: bool = False,
) -> Optional[Dict[str, Any]]:
    """
    Fetch the newest products_cache row for a (merchant_id, platform, platform_product_id).

    Defense-in-depth:
    - Prefer non-expired rows (expires_at NULL or > now) unless include_expired=True.
    - Always order by cached_at/id desc so in the presence of legacy duplicates we
      pick the freshest row deterministically.
    """
    base = (
        products_cache.select()
        .where(
            (products_cache.c.merchant_id == merchant_id)
            & (products_cache.c.platform == platform)
            & (products_cache.c.platform_product_id == platform_product_id)
        )
        .order_by(products_cache.c.cached_at.desc(), products_cache.c.id.desc())
        .limit(1)
    )

    if not include_expired:
        now = datetime.now()
        active = base.where(
            products_cache.c.expires_at.is_(None) | (products_cache.c.expires_at > now)
        )
        row = await database.fetch_one(active)
        if row:
            return dict(row)

    row = await database.fetch_one(base)
    return dict(row) if row else None


def _is_group_by_product_group(group_by: Optional[str]) -> bool:
    v = (group_by or "").strip().lower()
    return v in {"product_group", "product_group_id", "pg", "group", "true", "1"}


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


def _extract_product_summary(product_data: Dict[str, Any], platform_product_id: str) -> Dict[str, Any]:
    try:
        sp = StandardProduct.parse_obj(product_data)
        title = sp.title
        image_url = sp.image_url or (sp.images[0] if sp.images else None)
        product_id = sp.product_id or sp.id or platform_product_id
        variants = sp.variants or []
        currency = getattr(sp, "currency", None) or product_data.get("currency")
        price = getattr(sp, "price", None)
        availability = getattr(sp, "in_stock", None)
    except Exception:
        title = product_data.get("title") or product_data.get("name") or platform_product_id
        image_url = product_data.get("image_url") or None
        product_id = product_data.get("product_id") or product_data.get("id") or platform_product_id
        variants = product_data.get("variants") or []
        currency = product_data.get("currency")
        price = product_data.get("price")
        availability = product_data.get("in_stock") if "in_stock" in product_data else product_data.get("availability")

    return {
        "title": title,
        "image_url": image_url,
        "product_id": product_id,
        "variants": variants if isinstance(variants, list) else [],
        "currency": currency,
        "price": price,
        "availability": availability,
    }

async def _ensure_external_seeds_table() -> None:
    """
    Minimal storage for employee-managed external seeds.
    We intentionally keep this as runtime DDL for MVP to avoid blocking on migration runners.
    """
    global _EXTERNAL_SEEDS_TABLE_READY
    if _EXTERNAL_SEEDS_TABLE_READY:
        return
    async with _EXTERNAL_SEEDS_TABLE_LOCK:
        if _EXTERNAL_SEEDS_TABLE_READY:
            return
    await database.execute(
        """
        CREATE TABLE IF NOT EXISTS external_product_seeds (
          id TEXT PRIMARY KEY,
          external_product_id TEXT NULL,
          market TEXT NOT NULL,
          tool TEXT NOT NULL DEFAULT '*',
          utm_template TEXT NULL,
          partner_type TEXT NULL,
          disclosure_text TEXT NULL,
          destination_url TEXT NOT NULL,
          canonical_url TEXT NULL,
          domain TEXT NULL,
          title TEXT NULL,
          image_url TEXT NULL,
          price_amount DOUBLE PRECISION NULL,
          price_currency TEXT NULL,
          availability TEXT NULL,
          seed_data JSONB NOT NULL DEFAULT '{}'::jsonb,
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
    # Backfill new columns for older deployments (best-effort).
    try:
        await database.execute(
            "ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS seed_data JSONB NOT NULL DEFAULT '{}'::jsonb;"
        )
    except Exception:
        # In case the DB doesn't support JSONB (unlikely in prod), keep the table usable.
        try:
            await database.execute(
                "ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS seed_data TEXT;"
            )
        except Exception:
            pass
    await database.execute(
        "ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS utm_template TEXT;"
    )
    await database.execute(
        "ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS partner_type TEXT;"
    )
    await database.execute(
        "ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS disclosure_text TEXT;"
    )
    await database.execute(
        "ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS external_product_id TEXT;"
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
    await database.execute(
        "CREATE INDEX IF NOT EXISTS idx_external_product_seeds_created_at ON external_product_seeds(created_at DESC);"
    )
    await database.execute(
        "CREATE INDEX IF NOT EXISTS idx_external_product_seeds_external_product_id ON external_product_seeds(external_product_id);"
    )
    await database.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_external_product_seeds_active_unique ON external_product_seeds(market, tool, external_product_id) WHERE status = 'active' AND external_product_id IS NOT NULL;"
    )
    _EXTERNAL_SEEDS_TABLE_READY = True


async def _ensure_external_seed_import_tasks_table() -> None:
    """
    Track async CSV imports so the UI can poll status without holding an HTTP connection open.
    Runtime DDL keeps MVP deploys unblocked.
    """
    global _EXTERNAL_SEED_IMPORT_TASKS_TABLE_READY
    if _EXTERNAL_SEED_IMPORT_TASKS_TABLE_READY:
        return
    async with _EXTERNAL_SEED_IMPORT_TASKS_TABLE_LOCK:
        if _EXTERNAL_SEED_IMPORT_TASKS_TABLE_READY:
            return
        await database.execute(
            """
            CREATE TABLE IF NOT EXISTS employee_external_seed_import_tasks (
              id TEXT PRIMARY KEY,
              status TEXT NOT NULL DEFAULT 'pending',
              market TEXT NOT NULL,
              tool TEXT NOT NULL,
              mode TEXT NOT NULL,
              created_by_employee_id TEXT NULL,
              created_count INTEGER NOT NULL DEFAULT 0,
              updated_count INTEGER NOT NULL DEFAULT 0,
              errors TEXT NOT NULL DEFAULT '[]',
              seed_ids TEXT NOT NULL DEFAULT '[]',
              stats TEXT NOT NULL DEFAULT '{}',
              error TEXT NULL,
              created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              finished_at TIMESTAMPTZ NULL
            );
            """
        )

        # Backfill columns for older deployments (best-effort).
        try:
            await database.execute(
                "ALTER TABLE employee_external_seed_import_tasks ADD COLUMN IF NOT EXISTS created_by_employee_id TEXT;"
            )
            await database.execute(
                "ALTER TABLE employee_external_seed_import_tasks ADD COLUMN IF NOT EXISTS created_count INTEGER NOT NULL DEFAULT 0;"
            )
            await database.execute(
                "ALTER TABLE employee_external_seed_import_tasks ADD COLUMN IF NOT EXISTS updated_count INTEGER NOT NULL DEFAULT 0;"
            )
            await database.execute(
                "ALTER TABLE employee_external_seed_import_tasks ADD COLUMN IF NOT EXISTS errors TEXT NOT NULL DEFAULT '[]';"
            )
            await database.execute(
                "ALTER TABLE employee_external_seed_import_tasks ADD COLUMN IF NOT EXISTS seed_ids TEXT NOT NULL DEFAULT '[]';"
            )
            await database.execute(
                "ALTER TABLE employee_external_seed_import_tasks ADD COLUMN IF NOT EXISTS stats TEXT NOT NULL DEFAULT '{}';"
            )
            await database.execute(
                "ALTER TABLE employee_external_seed_import_tasks ADD COLUMN IF NOT EXISTS error TEXT;"
            )
            await database.execute(
                "ALTER TABLE employee_external_seed_import_tasks ADD COLUMN IF NOT EXISTS finished_at TIMESTAMPTZ;"
            )
        except Exception:
            pass

        await database.execute(
            "CREATE INDEX IF NOT EXISTS idx_employee_external_seed_import_tasks_status ON employee_external_seed_import_tasks(status);"
        )
        await database.execute(
            "CREATE INDEX IF NOT EXISTS idx_employee_external_seed_import_tasks_updated_at ON employee_external_seed_import_tasks(updated_at DESC);"
        )
        _EXTERNAL_SEED_IMPORT_TASKS_TABLE_READY = True


async def _ensure_primary_offers_table() -> None:
    """
    Primary offer selection per product_key (employee curation).
    Runtime DDL keeps MVP deploys unblocked.
    """
    await database.execute(
        """
        CREATE TABLE IF NOT EXISTS employee_product_primary_offers (
          product_key TEXT PRIMARY KEY,
          offer_id TEXT NOT NULL,
          offer_type TEXT NOT NULL,
          created_by_employee_id TEXT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )
    await database.execute(
        "CREATE INDEX IF NOT EXISTS idx_employee_product_primary_offers_type ON employee_product_primary_offers(offer_type);"
    )
    await database.execute(
        "CREATE INDEX IF NOT EXISTS idx_employee_product_primary_offers_updated ON employee_product_primary_offers(updated_at DESC);"
    )


def _stable_external_product_id(url: str) -> str:
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


def _ensure_json_list(val: Any) -> List[Any]:
    if isinstance(val, list):
        return val
    if isinstance(val, tuple):
        return list(val)
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return []
        try:
            parsed = json.loads(s)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def _external_seed_import_task_id() -> str:
    return f"esit_{uuid.uuid4().hex}"


_SEED_DATA_FORCE_TEXT = False


def _seed_data_payload(seed_data: Dict[str, Any]) -> Any:
    if _SEED_DATA_FORCE_TEXT:
        return json.dumps(seed_data)
    return seed_data


async def _execute_seed_data_stmt(query: str, values: Dict[str, Any]) -> None:
    global _SEED_DATA_FORCE_TEXT
    try:
        await database.execute(query, values)
    except Exception as exc:
        msg = str(exc)
        seed_data = values.get("seed_data")
        if (
            not _SEED_DATA_FORCE_TEXT
            and isinstance(seed_data, dict)
            and ("expected str" in msg or "got dict" in msg or "query argument" in msg)
        ):
            _SEED_DATA_FORCE_TEXT = True
            retry_values = dict(values)
            retry_values["seed_data"] = json.dumps(seed_data or {})
            await database.execute(query, retry_values)
            return
        raise


def _seed_variants(seed_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    variants = seed_data.get("variants")
    if isinstance(variants, list):
        return [v for v in variants if isinstance(v, dict)]
    return []


_SIZE_LIKE_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:ml|mL|l|L|oz|fl\s*oz|g|kg|lb|lbs|cm|mm|in|inch|inches)\b",
    re.IGNORECASE,
)


def _normalize_variant_title(title: Any) -> str:
    if title is None:
        return ""
    s = str(title).strip()
    return re.sub(r"\s+", " ", s)


def _variant_title_score(*, title: Any, product_title: Optional[str]) -> int:
    t = _normalize_variant_title(title)
    if not t:
        return 0
    if product_title and t.lower() == str(product_title).strip().lower():
        return 0
    if "http://" in t or "https://" in t:
        return 0
    if _SIZE_LIKE_RE.search(t):
        return 3
    if len(t) <= 12:
        return 2
    if len(t) <= 32:
        return 1
    return 0


def _best_variant_title_score(variants: List[Dict[str, Any]], product_title: Optional[str]) -> int:
    best = 0
    for v in variants:
        if not isinstance(v, dict):
            continue
        best = max(best, _variant_title_score(title=v.get("title"), product_title=product_title))
    return best


def _distinct_variant_titles(variants: List[Dict[str, Any]]) -> List[str]:
    out: List[str] = []
    for v in variants:
        if not isinstance(v, dict):
            continue
        t = _normalize_variant_title(v.get("title"))
        if t and t not in out:
            out.append(t)
    return out


def _should_overwrite_seed_variants(
    *,
    existing: List[Dict[str, Any]],
    incoming: List[Dict[str, Any]],
    product_title: Optional[str],
) -> bool:
    if not incoming:
        return False
    if not existing:
        return True

    existing_score = _best_variant_title_score(existing, product_title)
    incoming_score = _best_variant_title_score(incoming, product_title)
    if incoming_score <= existing_score:
        return False

    # Overwrite only when existing titles are weak or degenerate.
    if existing_score <= 1 and incoming_score >= 2:
        return True

    existing_titles = _distinct_variant_titles(existing)
    incoming_titles = _distinct_variant_titles(incoming)
    if len(existing_titles) <= 1 and len(incoming_titles) > 1:
        return True

    return False


def _seed_primary_price(seed_row: Dict[str, Any], seed_data: Dict[str, Any]) -> Dict[str, Any]:
    variants = _seed_variants(seed_data)
    for v in variants:
        amt = v.get("price_amount")
        cur = v.get("price_currency") or v.get("currency")
        if amt is not None:
            return {"amount": amt, "currency": cur}
    return {"amount": seed_row.get("price_amount"), "currency": seed_row.get("price_currency")}


def _is_external_seed_product_key(*, merchant_id: str, platform: str) -> bool:
    return merchant_id == EXTERNAL_SEED_MERCHANT_ID and platform == EXTERNAL_SEED_PLATFORM


async def _fetch_external_seed_rows_by_external_product_id(
    *,
    external_product_id: str,
    limit: int = 50,
    allow_attached_product_key: Optional[str] = None,
) -> List[Dict[str, Any]]:
    await _ensure_external_seeds_table()
    values: Dict[str, Any] = {"external_product_id": external_product_id, "limit": limit}
    attached_clause = ""
    if allow_attached_product_key:
        values["allow_attached_product_key"] = allow_attached_product_key
        attached_clause = " AND (attached_product_key IS NULL OR attached_product_key = :allow_attached_product_key)"
    try:
        rows = await database.fetch_all(
            """
            SELECT *
            FROM external_product_seeds
            WHERE status = 'active'
              AND (
                external_product_id = :external_product_id
                OR (seed_data->>'external_product_id') = :external_product_id
              )
            """
            + attached_clause
            + """
            ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST
            LIMIT :limit
            """,
            values,
        )
    except Exception:
        rows = await database.fetch_all(
            """
            SELECT *
            FROM external_product_seeds
            WHERE status = 'active'
              AND external_product_id = :external_product_id
            """
            + attached_clause
            + """
            ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST
            LIMIT :limit
            """,
            values,
        )
    return [dict(r) for r in (rows or [])]


def _normalize_seed_image_urls(*, seed_data: Dict[str, Any], row: Dict[str, Any]) -> List[str]:
    candidates: List[str] = []
    raw_list = seed_data.get("image_urls")
    if isinstance(raw_list, list):
        candidates.extend([str(u) for u in raw_list if isinstance(u, str)])
    for u in [seed_data.get("image_url"), row.get("image_url")]:
        if isinstance(u, str):
            candidates.append(u)

    out: List[str] = []
    seen: set[str] = set()
    for u in candidates:
        uu = str(u).strip()
        if not uu or not uu.startswith(("http://", "https://")):
            continue
        if uu in seen:
            continue
        seen.add(uu)
        out.append(uu)
        if len(out) >= 20:
            break
    return out


def _normalize_seed_brand(seed_data: Dict[str, Any]) -> Optional[str]:
    raw = seed_data.get("brand") or seed_data.get("product", {}).get("brand")
    if isinstance(raw, dict):
        raw = raw.get("name") or raw.get("brand")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def _seed_variant_to_raw_row(
    *,
    v: Dict[str, Any],
    idx: int,
    external_product_id: str,
    default_currency: str,
    fallback_price_amount: Optional[float],
) -> Dict[str, Any]:
    variant_id = str(v.get("variant_id") or v.get("id") or v.get("sku") or f"{external_product_id}:{idx + 1}").strip()
    title = v.get("title") or v.get("name") or f"Variant {idx + 1}"
    raw_amount = v.get("price_amount")
    if raw_amount is None:
        raw_amount = v.get("price") or v.get("amount") or v.get("value")
    price_amount: Optional[float] = None
    if raw_amount is not None and raw_amount != "":
        try:
            price_amount = float(raw_amount)
        except Exception:
            price_amount = None
    if price_amount is None:
        price_amount = fallback_price_amount

    raw_currency = v.get("price_currency") or v.get("currency") or default_currency
    price_currency = str(raw_currency or default_currency).strip().upper() or default_currency

    availability = _normalize_seed_availability(v.get("availability"))
    available: Optional[bool] = None
    if availability in (None, "in_stock"):
        available = True
    elif availability == "out_of_stock":
        available = False

    out: Dict[str, Any] = {
        "variant_id": variant_id,
        "title": title,
        "price_amount": price_amount,
        "price_currency": price_currency,
        **({"availability": availability} if availability is not None else {}),
        **({"available": available} if available is not None else {}),
    }
    return out


def _seed_variant_to_standard_variant(
    *,
    raw_row: Dict[str, Any],
    fallback_price: float,
) -> Dict[str, Any]:
    vid = str(raw_row.get("variant_id") or "").strip() or "∅"
    title = str(raw_row.get("title") or "Variant").strip() or "Variant"
    price_amount = raw_row.get("price_amount")
    try:
        price = float(price_amount) if price_amount is not None else float(fallback_price)
    except Exception:
        price = float(fallback_price)
    return {
        "id": vid,
        "variant_id": vid,
        "title": title,
        "sku": vid,
        "price": price,
        "inventory_quantity": 0,
    }


async def _build_external_seed_product_view(
    *,
    external_product_id: str,
    allow_attached_product_key: Optional[str] = None,
) -> Dict[str, Any]:
    rows = await _fetch_external_seed_rows_by_external_product_id(
        external_product_id=external_product_id,
        allow_attached_product_key=allow_attached_product_key,
    )
    if not rows:
        raise HTTPException(status_code=404, detail="PRODUCT_NOT_FOUND")

    primary_row = rows[0]
    seed_data = _ensure_json_obj(primary_row.get("seed_data"))

    title = (
        seed_data.get("title")
        or primary_row.get("title")
        or primary_row.get("canonical_url")
        or primary_row.get("destination_url")
        or external_product_id
    )
    description = seed_data.get("description") or seed_data.get("snapshot", {}).get("description") or ""

    primary_price = _seed_primary_price(primary_row, seed_data)
    raw_amount = primary_price.get("amount")
    try:
        price = float(raw_amount) if raw_amount is not None else 0.0
    except Exception:
        price = 0.0
    currency = str(primary_price.get("currency") or "USD").strip().upper() or "USD"

    image_urls = _normalize_seed_image_urls(seed_data=seed_data, row=primary_row)
    image_url = image_urls[0] if image_urls else None

    seed_variants = _seed_variants(seed_data)
    raw_variants: List[Dict[str, Any]] = []
    std_variants: List[Dict[str, Any]] = []
    for idx, v in enumerate(seed_variants):
        if not isinstance(v, dict):
            continue
        raw_row = _seed_variant_to_raw_row(
            v=v,
            idx=idx,
            external_product_id=external_product_id,
            default_currency=currency,
            fallback_price_amount=(float(raw_amount) if raw_amount is not None else None),
        )
        raw_variants.append(raw_row)
        std_variants.append(_seed_variant_to_standard_variant(raw_row=raw_row, fallback_price=price))
        if len(raw_variants) >= 50:
            break

    if not std_variants:
        std_variants = [
            {
                "id": external_product_id,
                "variant_id": external_product_id,
                "title": "Default",
                "sku": external_product_id,
                "price": price,
                "inventory_quantity": 0,
            }
        ]
        raw_variants = [
            {
                "variant_id": "∅",
                "title": "Default (no variants)",
                "price_amount": (price if raw_amount is not None else None),
                "price_currency": currency,
            }
        ]

    vendor = _normalize_seed_brand(seed_data)

    product_data: Dict[str, Any] = {
        "id": external_product_id,
        "product_id": external_product_id,
        "platform": EXTERNAL_SEED_PLATFORM,
        "merchant_id": EXTERNAL_SEED_MERCHANT_ID,
        "title": title,
        "description": description,
        "vendor": vendor,
        "product_type": "external",
        "tags": [],
        "price": price,
        "currency": currency,
        "inventory_quantity": 0,
        "orderable": False,
        "image_url": image_url,
        "images": image_urls,
        "variants": std_variants,
        "updated_at": primary_row.get("updated_at") or primary_row.get("created_at"),
        "platform_metadata": {
            "source": "external_seed",
            "external_product_id": external_product_id,
            "domain": primary_row.get("domain"),
            "canonical_url": primary_row.get("canonical_url"),
            "destination_url": primary_row.get("destination_url"),
            "seed_ids": [str(r.get("id")) for r in rows if r.get("id")],
        },
    }

    raw: Dict[str, Any] = {
        "title": title,
        "description": description,
        "image_url": image_url,
        "images": image_urls,
        "variants": raw_variants,
        "source": "external_seed",
        "external_product_id": external_product_id,
        "seed_primary": {
            "id": primary_row.get("id"),
            "market": primary_row.get("market"),
            "tool": primary_row.get("tool"),
            "utm_template": primary_row.get("utm_template") or seed_data.get("utm_template"),
            "partner_type": primary_row.get("partner_type") or seed_data.get("partner_type"),
            "disclosure_text": primary_row.get("disclosure_text")
            or seed_data.get("disclosure_text")
            or DEFAULT_DISCLOSURE_TEXT,
            "destination_url": primary_row.get("destination_url"),
            "canonical_url": primary_row.get("canonical_url"),
            "domain": primary_row.get("domain"),
            "price_amount": primary_row.get("price_amount"),
            "price_currency": primary_row.get("price_currency"),
            "availability": primary_row.get("availability"),
            "updated_at": _to_iso(primary_row.get("updated_at") or primary_row.get("created_at")),
        },
        "seed_data": seed_data,
    }

    cached_at = primary_row.get("updated_at") or primary_row.get("created_at")
    return {"product_data": product_data, "raw": raw, "cached_at": cached_at}


def _seed_merchant_display_name(seed_data: Dict[str, Any], domain: Optional[str]) -> Optional[str]:
    val = seed_data.get("merchant_display_name") or seed_data.get("brand") or domain
    if isinstance(val, str):
        val = val.strip()
        return val or None
    return None


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
    partner_type: Optional[str] = None
    disclosure_text: Optional[str] = None
    # Optional manual overrides / richer product seed fields (for employee curation).
    merchant_display_name: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    image_url: Optional[str] = None
    product_id: Optional[str] = None
    brand: Optional[str] = None
    category: Optional[str] = None
    price_amount: Optional[float] = None
    price_currency: Optional[str] = None
    availability: Optional[str] = None
    variants: Optional[List[Dict[str, Any]]] = None


class UpdateExternalSeedRequest(BaseModel):
    status: Optional[str] = None
    notes: Optional[str] = None
    market: Optional[str] = None
    tool: Optional[str] = None
    merchant_display_name: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    image_url: Optional[str] = None
    product_id: Optional[str] = None
    brand: Optional[str] = None
    category: Optional[str] = None
    price_amount: Optional[float] = None
    price_currency: Optional[str] = None
    availability: Optional[str] = None
    variants: Optional[List[Dict[str, Any]]] = None
    utm_template: Optional[str] = None
    partner_type: Optional[str] = None
    disclosure_text: Optional[str] = None


class PreviewExternalSeedRequest(BaseModel):
    destination_url: str = Field(..., min_length=1)
    market: Optional[str] = None
    force_refresh: bool = False


class ExternalSeedsCsvImportResponse(BaseModel):
    created: int
    updated: int = 0
    errors: List[str] = Field(default_factory=list)
    seedIds: List[str] = Field(default_factory=list)
    taskId: Optional[str] = None


def _parse_float(raw: Any) -> Optional[float]:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        return float(s)
    except Exception:
        return None


_CSV_KEY_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")


def _normalize_csv_key(raw: Any) -> str:
    """
    Normalize CSV header keys so we can accept multiple upload formats.
    Examples:
      - "Product URL" -> "product_url"
      - "priceAmount" -> "priceamount"
    """
    s = str(raw or "").strip().lower()
    if not s:
        return ""
    s = _CSV_KEY_NORMALIZE_RE.sub("_", s)
    return s.strip("_")


def _normalize_seed_url_for_id(url: str) -> str:
    """
    Make external_product_id stable across common tracking params.
    Mirrors `services.external_offers_service._normalize_url` semantics (subset).
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise HTTPException(status_code=400, detail="INVALID_URL")
    qs = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if not k.lower().startswith("utm_")]
    qs = [(k, v) for k, v in qs if k.lower() not in {"fbclid", "gclid", "yclid", "msclkid"}]
    query = urlencode(qs, doseq=True)
    return urlunparse(parsed._replace(fragment="", query=query))


def _normalize_seed_availability(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    v = s.strip().lower()
    if v in {"in stock", "in_stock", "instock"}:
        return "in_stock"
    if v in {"out of stock", "out_of_stock", "outofstock", "sold out", "sold_out", "unavailable"}:
        return "out_of_stock"
    return v.replace(" ", "_")


async def _run_external_seed_import_task(
    *,
    task_id: str,
    csv_text: str,
    current_user: dict,
    market: str,
    tool: str,
    mode: str,
) -> None:
    started_at = datetime.now(timezone.utc)
    try:
        await _ensure_external_seed_import_tasks_table()
        await database.execute(
            """
            UPDATE employee_external_seed_import_tasks
            SET status = 'running',
                updated_at = NOW()
            WHERE id = :id
            """,
            {"id": task_id},
        )

        result = await _import_external_seeds_csv_text(
            text=csv_text,
            current_user=current_user,
            market=market,
            tool=tool,
            mode=mode,
        )

        duration_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
        stats = {
            "duration_ms": duration_ms,
            "created": result.created,
            "updated": result.updated,
            "errors_count": len(result.errors or []),
            "seed_ids_count": len(result.seedIds or []),
        }

        await database.execute(
            """
            UPDATE employee_external_seed_import_tasks
            SET status = 'success',
                created_count = :created_count,
                updated_count = :updated_count,
                errors = :errors,
                seed_ids = :seed_ids,
                stats = :stats,
                finished_at = NOW(),
                updated_at = NOW()
            WHERE id = :id
            """,
            {
                "id": task_id,
                "created_count": int(result.created or 0),
                "updated_count": int(result.updated or 0),
                "errors": json.dumps(list(result.errors or [])[:500]),
                "seed_ids": json.dumps(list(result.seedIds or [])[:5000]),
                "stats": json.dumps(stats),
            },
        )
    except Exception as exc:
        try:
            duration_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
            await database.execute(
                """
                UPDATE employee_external_seed_import_tasks
                SET status = 'error',
                    error = :error,
                    stats = :stats,
                    finished_at = NOW(),
                    updated_at = NOW()
                WHERE id = :id
                """,
                {
                    "id": task_id,
                    "error": str(exc)[:800],
                    "stats": json.dumps({"duration_ms": duration_ms}),
                },
            )
        except Exception:
            pass


@router.get("/external-seeds/import-tasks/{task_id}")
async def get_external_seed_import_task(
    task_id: str,
    current_user: dict = Depends(get_current_employee),
):
    await _ensure_external_seed_import_tasks_table()
    row = await database.fetch_one(
        "SELECT * FROM employee_external_seed_import_tasks WHERE id = :id",
        {"id": task_id},
    )
    if not row:
        raise HTTPException(status_code=404, detail="TASK_NOT_FOUND")
    data = dict(row)
    return {
        "status": "success",
        "task": {
            "id": data.get("id"),
            "status": data.get("status"),
            "market": data.get("market"),
            "tool": data.get("tool"),
            "mode": data.get("mode"),
            "created": int(data.get("created_count") or 0),
            "updated": int(data.get("updated_count") or 0),
            "errors": [str(x) for x in _ensure_json_list(data.get("errors")) if str(x)],
            "seedIds": [str(x) for x in _ensure_json_list(data.get("seed_ids")) if str(x)],
            "error": data.get("error"),
            "stats": _ensure_json_obj(data.get("stats")),
            "created_at": _to_iso(data.get("created_at")),
            "updated_at": _to_iso(data.get("updated_at")),
            "finished_at": _to_iso(data.get("finished_at")),
        },
    }


@router.post("/external-seeds/import-csv", response_model=ExternalSeedsCsvImportResponse)
async def import_external_seeds_csv(
    req: Request,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_current_employee),
    market: str = Query("US"),
    tool: str = Query("*"),
    mode: str = Query("upsert"),
    async_import: bool = Query(False, alias="async"),
) -> ExternalSeedsCsvImportResponse:
    raw = await req.body()
    text = raw.decode("utf-8", errors="replace")
    if not text.strip():
        raise HTTPException(status_code=400, detail="EMPTY_CSV")

    employee_id = current_user.get("employee_id") or current_user.get("employeeId")
    created_by = str(current_user.get("email") or employee_id or "")

    if async_import:
        await _ensure_external_seed_import_tasks_table()
        task_id = _external_seed_import_task_id()
        await database.execute(
            """
            INSERT INTO employee_external_seed_import_tasks (
              id, status, market, tool, mode, created_by_employee_id,
              created_count, updated_count, errors, seed_ids, stats,
              created_at, updated_at
            ) VALUES (
              :id, 'pending', :market, :tool, :mode, :created_by_employee_id,
              0, 0, :errors, :seed_ids, :stats,
              NOW(), NOW()
            )
            """,
            {
                "id": task_id,
                "market": _normalize_market(market),
                "tool": _normalize_tool(tool),
                "mode": str(mode or "upsert").strip().lower(),
                "created_by_employee_id": created_by,
                "errors": "[]",
                "seed_ids": "[]",
                "stats": json.dumps({"input_bytes": len(raw)}),
            },
        )
        background_tasks.add_task(
            _run_external_seed_import_task,
            task_id=task_id,
            csv_text=text,
            current_user=current_user,
            market=market,
            tool=tool,
            mode=mode,
        )
        return ExternalSeedsCsvImportResponse(created=0, updated=0, errors=[], seedIds=[], taskId=task_id)

    return await _import_external_seeds_csv_text(
        text=text,
        current_user=current_user,
        market=market,
        tool=tool,
        mode=mode,
    )


async def _import_external_seeds_csv_text(
    *,
    text: str,
    current_user: dict,
    market: str,
    tool: str,
    mode: str,
) -> ExternalSeedsCsvImportResponse:
    """
    CSV import for external seeds / external products.

    Content-Type: text/csv (body is raw CSV text).

    Required columns:
      - destination_url (or url)
    Optional columns:
      - market, tool, title, image_url, price_amount, price_currency, availability
      - utm_template, partner_type, disclosure_text, notes
      - attached_product_key, attached_variant_id
      - status (active|disabled)
    """
    await _ensure_external_seeds_table()

    if not text.strip():
        raise HTTPException(status_code=400, detail="EMPTY_CSV")

    default_market = _normalize_market(market)
    default_tool = _normalize_tool(tool)
    mode_norm = str(mode or "upsert").strip().lower()
    if mode_norm not in ("create", "upsert"):
        raise HTTPException(status_code=400, detail="INVALID_MODE")

    reader = csv.DictReader(io.StringIO(text))
    fieldnames_norm = {_normalize_csv_key(x) for x in (reader.fieldnames or [])}
    is_catalog_export = (
        "product_url" in fieldnames_norm
        and "destination_url" not in fieldnames_norm
        and "url" not in fieldnames_norm
    )

    created = 0
    updated = 0
    errors: List[str] = []
    seed_ids: List[str] = []

    employee_id = current_user.get("employee_id") or current_user.get("employeeId")
    created_by = str(current_user.get("email") or employee_id or "")

    if is_catalog_export:
        # Catalog-style import (one row per variant, grouped into a single seed per Product URL).
        # Example: Tom Ford export CSV.
        groups: Dict[str, Dict[str, Any]] = {}
        group_meta: Dict[str, Dict[str, Any]] = {}

        for idx, row in enumerate(reader, start=2):
            try:
                nrow = { _normalize_csv_key(k): v for k, v in (row or {}).items() if k is not None }

                product_url_raw = str(nrow.get("product_url") or "").strip()
                if not product_url_raw:
                    raise ValueError("MISSING_PRODUCT_URL")
                product_url = _normalize_seed_url_for_id(_require_http_url(product_url_raw))

                row_market = _normalize_market(nrow.get("market") or default_market)
                row_tool = _normalize_tool(nrow.get("tool") or default_tool)

                product_title = str(nrow.get("product_title") or nrow.get("title") or "").strip() or None
                brand = str(nrow.get("brand") or "").strip() or None
                description = str(nrow.get("ai_merged_description") or nrow.get("description") or "").strip() or None

                variant_sku = str(nrow.get("sku") or nrow.get("option_value") or "").strip() or None
                variant_id_raw = str(nrow.get("variant_id") or "").strip() or None
                variant_id = variant_sku or variant_id_raw or f"v_{idx}"

                option_name = str(nrow.get("option_name") or "").strip() or None
                option_value = str(nrow.get("option_value") or "").strip() or None

                deep_link = str(nrow.get("deep_link") or nrow.get("variant_url") or "").strip() or None
                if deep_link and not deep_link.startswith(("http://", "https://")):
                    deep_link = None

                price_amount = _parse_float(nrow.get("price") or nrow.get("price_amount") or nrow.get("priceamount"))
                price_currency = str(nrow.get("currency") or nrow.get("price_currency") or nrow.get("pricecurrency") or "").strip().upper() or None
                availability = _normalize_seed_availability(nrow.get("availability"))

                variant_image_url = str(nrow.get("variant_image_url") or "").strip() or None
                if variant_image_url and not variant_image_url.startswith(("http://", "https://")):
                    variant_image_url = None

                external_product_id = (
                    str(nrow.get("external_product_id") or "").strip()
                    or _stable_external_product_id(product_url)
                )
                if not external_product_id:
                    raise ValueError("MISSING_EXTERNAL_PRODUCT_ID")

                group_key = f"{row_market}|{row_tool}|{external_product_id}"
                groups.setdefault(group_key, {"variants": [], "image_urls": []})
                gm = group_meta.setdefault(
                    group_key,
                    {
                        "market": row_market,
                        "tool": row_tool,
                        "external_product_id": external_product_id,
                        "destination_url": product_url,
                        "canonical_url": product_url,
                        "title": product_title,
                        "brand": brand,
                        "description": description,
                    },
                )

                # Merge group-level fields (first non-empty wins).
                if not gm.get("title") and product_title:
                    gm["title"] = product_title
                if not gm.get("brand") and brand:
                    gm["brand"] = brand
                if not gm.get("description") and description:
                    gm["description"] = description

                options: Dict[str, str] = {}
                if option_name and option_value:
                    options[option_name] = option_value

                variant_title = option_value or variant_sku or variant_id
                variant_payload: Dict[str, Any] = {
                    "variant_id": variant_id,
                    "title": _normalize_variant_title(variant_title),
                    **({"sku": variant_sku} if variant_sku else {}),
                    **({"price_amount": price_amount} if price_amount is not None else {}),
                    **({"price_currency": price_currency} if price_currency else {}),
                    **({"currency": price_currency} if price_currency else {}),
                    **({"availability": availability} if availability else {}),
                    **({"image_url": variant_image_url} if variant_image_url else {}),
                    **({"options": options} if options else {}),
                    **({"destination_url": deep_link} if deep_link else {}),
                }

                groups[group_key]["variants"].append(variant_payload)
                if variant_image_url:
                    groups[group_key]["image_urls"].append(variant_image_url)
            except HTTPException as exc:
                errors.append(f"Row {idx}: {exc.detail}")
            except Exception as exc:
                errors.append(f"Row {idx}: {str(exc)}")

        # Upsert each grouped product seed
        for group_key, g in groups.items():
            try:
                gm = group_meta.get(group_key) or {}
                row_market = gm.get("market") or default_market
                row_tool = gm.get("tool") or default_tool
                external_product_id = gm.get("external_product_id") or ""
                dest = gm.get("destination_url") or ""
                canonical_url = gm.get("canonical_url") or None

                if not external_product_id or not dest:
                    raise ValueError("INVALID_GROUP")

                # Deduplicate + sort variants so primary price is stable (min price first).
                seen_variant_ids: set[str] = set()
                variants: List[Dict[str, Any]] = []
                for v in g.get("variants") or []:
                    if not isinstance(v, dict):
                        continue
                    vid = str(v.get("variant_id") or "").strip()
                    if not vid or vid in seen_variant_ids:
                        continue
                    seen_variant_ids.add(vid)
                    variants.append(v)

                def _variant_price_key(v: Dict[str, Any]) -> float:
                    raw = v.get("price_amount")
                    if raw is None:
                        raw = v.get("price") or v.get("amount") or v.get("value")
                    try:
                        return float(raw)
                    except Exception:
                        return float("inf")

                variants.sort(key=_variant_price_key)

                image_urls: List[str] = []
                seen_images: set[str] = set()
                for u in g.get("image_urls") or []:
                    if not isinstance(u, str):
                        continue
                    uu = u.strip()
                    if not uu or not uu.startswith(("http://", "https://")):
                        continue
                    if uu in seen_images:
                        continue
                    seen_images.add(uu)
                    image_urls.append(uu)
                    if len(image_urls) >= 20:
                        break

                title = gm.get("title") or None
                brand = gm.get("brand") or None
                description = gm.get("description") or None
                image_url = image_urls[0] if image_urls else None

                # Summary price: min variant price when present.
                price_amount = None
                price_currency = None
                for v in variants:
                    if v.get("price_amount") is None:
                        continue
                    price_amount = v.get("price_amount")
                    price_currency = v.get("price_currency") or v.get("currency")
                    break
                if price_currency is not None:
                    price_currency = str(price_currency).strip().upper() or None

                # Summary availability: in_stock if any variant is in_stock.
                availability = None
                if variants:
                    any_in_stock = False
                    for v in variants:
                        a = _normalize_seed_availability(v.get("availability"))
                        if a == "in_stock" or a is None:
                            any_in_stock = True
                            break
                    availability = "in_stock" if any_in_stock else "out_of_stock"

                domain = None
                try:
                    domain = (urlparse(canonical_url or dest).hostname or "").lower() or None
                except Exception:
                    domain = None

                existing_row = None
                if mode_norm == "upsert":
                    existing_row = await database.fetch_one(
                        """
                        SELECT *
                        FROM external_product_seeds
                        WHERE market = :market
                          AND tool = :tool
                          AND external_product_id = :external_product_id
                        ORDER BY updated_at DESC, created_at DESC
                        LIMIT 1
                        """,
                        {"market": row_market, "tool": row_tool, "external_product_id": external_product_id},
                    )
                    if not existing_row:
                        match_url = canonical_url or dest
                        existing_row = await database.fetch_one(
                            """
                            SELECT *
                            FROM external_product_seeds
                            WHERE market = :market
                              AND tool = :tool
                              AND (canonical_url = :match_url OR destination_url = :match_url)
                            ORDER BY updated_at DESC, created_at DESC
                            LIMIT 1
                            """,
                            {"market": row_market, "tool": row_tool, "match_url": match_url},
                        )

                if existing_row:
                    existing = dict(existing_row)
                    seed_id = str(existing.get("id"))
                    seed_data = _ensure_json_obj(existing.get("seed_data"))
                    seed_data["external_product_id"] = seed_data.get("external_product_id") or external_product_id
                    seed_data["source"] = seed_data.get("source") or "employee_seed_csv_catalog"
                    if title is not None:
                        seed_data["title"] = title
                    if brand is not None:
                        seed_data["brand"] = brand
                    if description is not None:
                        seed_data["description"] = description
                    if image_url is not None:
                        seed_data["image_url"] = image_url
                    if image_urls:
                        seed_data["image_urls"] = image_urls
                    if availability is not None:
                        seed_data["availability"] = availability
                    if variants:
                        seed_data["variants"] = variants

                    canonical_url_update = canonical_url if canonical_url is not None else existing.get("canonical_url")
                    domain_update = domain if domain is not None else existing.get("domain")

                    update_values: Dict[str, Any] = {
                        "id": seed_id,
                        "external_product_id": external_product_id,
                        "destination_url": dest,
                        "canonical_url": canonical_url_update,
                        "domain": domain_update,
                        "seed_data": _seed_data_payload(seed_data),
                        "created_by_employee_id": str(employee_id) if employee_id else None,
                    }
                    set_clauses = [
                        "external_product_id = :external_product_id",
                        "destination_url = :destination_url",
                        "canonical_url = :canonical_url",
                        "domain = :domain",
                        "seed_data = :seed_data",
                        "updated_at = NOW()",
                        "created_by_employee_id = :created_by_employee_id",
                    ]

                    if title is not None:
                        update_values["title"] = title
                        set_clauses.append("title = :title")
                    if image_url is not None:
                        update_values["image_url"] = image_url
                        set_clauses.append("image_url = :image_url")
                    if price_amount is not None:
                        update_values["price_amount"] = price_amount
                        set_clauses.append("price_amount = :price_amount")
                    if price_currency is not None:
                        update_values["price_currency"] = price_currency
                        set_clauses.append("price_currency = :price_currency")
                    if availability is not None:
                        update_values["availability"] = availability
                        set_clauses.append("availability = :availability")

                    await _execute_seed_data_stmt(
                        f"UPDATE external_product_seeds SET {', '.join(set_clauses)} WHERE id = :id",
                        update_values,
                    )
                    updated += 1
                    seed_ids.append(seed_id)
                    continue

                seed_id = _seed_id()
                seed_data: Dict[str, Any] = {
                    "external_product_id": external_product_id,
                    "title": title,
                    "brand": brand,
                    "description": description,
                    "image_url": image_url,
                    "image_urls": image_urls,
                    "availability": availability,
                    "variants": variants,
                    "disclosure_text": DEFAULT_DISCLOSURE_TEXT,
                    "source": "employee_seed_csv_catalog",
                }

                await _execute_seed_data_stmt(
                    """
                    INSERT INTO external_product_seeds (
                      id, external_product_id, market, tool,
                      utm_template, partner_type, disclosure_text,
                      destination_url, canonical_url, domain,
                      title, image_url, price_amount, price_currency, availability,
                      seed_data, status, notes, created_by_employee_id, attached_product_key, attached_variant_id,
                      created_at, updated_at
                    ) VALUES (
                      :id, :external_product_id, :market, :tool,
                      :utm_template, :partner_type, :disclosure_text,
                      :destination_url, :canonical_url, :domain,
                      :title, :image_url, :price_amount, :price_currency, :availability,
                      :seed_data, :status, :notes, :created_by_employee_id, :attached_product_key, :attached_variant_id,
                      NOW(), NOW()
                    )
                    """,
                    {
                        "id": seed_id,
                        "external_product_id": external_product_id,
                        "market": row_market,
                        "tool": row_tool,
                        "utm_template": None,
                        "partner_type": None,
                        "disclosure_text": DEFAULT_DISCLOSURE_TEXT,
                        "destination_url": dest,
                        "canonical_url": canonical_url,
                        "domain": domain,
                        "title": title,
                        "image_url": image_url,
                        "price_amount": price_amount,
                        "price_currency": price_currency,
                        "availability": availability,
                        "seed_data": _seed_data_payload(seed_data),
                        "status": "active",
                        "notes": None,
                        "created_by_employee_id": created_by,
                        "attached_product_key": None,
                        "attached_variant_id": None,
                    },
                )
                created += 1
                seed_ids.append(seed_id)
            except HTTPException as exc:
                errors.append(f"Group {group_key}: {exc.detail}")
            except Exception as exc:
                errors.append(f"Group {group_key}: {str(exc)}")
    else:
        for idx, row in enumerate(reader, start=2):
            try:
                destination_url = str(row.get("destination_url") or row.get("url") or "").strip()
                if not destination_url:
                    raise ValueError("MISSING_DESTINATION_URL")
                dest = _normalize_seed_url_for_id(_require_http_url(destination_url))

                row_market = _normalize_market(row.get("market") or default_market)
                row_tool = _normalize_tool(row.get("tool") or default_tool)

                title = str(row.get("title") or "").strip() or None
                description = str(row.get("description") or row.get("product_details") or "").strip() or None
                image_url = str(row.get("image_url") or row.get("imageUrl") or "").strip() or None
                availability = _normalize_seed_availability(row.get("availability"))
                utm_template = str(row.get("utm_template") or row.get("utmTemplate") or "").strip() or None
                partner_type = str(row.get("partner_type") or row.get("partnerType") or "").strip() or None
                disclosure_text = str(row.get("disclosure_text") or row.get("disclosureText") or "").strip() or None
                notes = str(row.get("notes") or "").strip() or None
                status = str(row.get("status") or "").strip().lower() or None

                attached_product_key = str(row.get("attached_product_key") or row.get("product_key") or "").strip() or None
                attached_variant_id = str(row.get("attached_variant_id") or row.get("variant_id") or "").strip() or None
                if attached_variant_id == "":
                    attached_variant_id = None
                if attached_variant_id is not None and attached_variant_id != "∅":
                    attached_variant_id = attached_variant_id
                if attached_variant_id is None and attached_product_key:
                    attached_variant_id = "∅"

                price_amount = _parse_float(row.get("price_amount") or row.get("price") or row.get("priceAmount"))
                price_currency = str(row.get("price_currency") or row.get("currency") or row.get("priceCurrency") or "").strip() or None

                canonical_url = str(row.get("canonical_url") or row.get("canonicalUrl") or "").strip() or None
                if canonical_url:
                    canonical_url = _normalize_seed_url_for_id(_require_http_url(canonical_url))
                domain = None
                try:
                    domain = (urlparse(canonical_url or dest).hostname or "").lower() or None
                except Exception:
                    domain = None

                external_product_id = str(row.get("external_product_id") or "").strip() or _stable_external_product_id(canonical_url or dest)
                if not external_product_id:
                    raise ValueError("MISSING_EXTERNAL_PRODUCT_ID")

                existing_row = None
                if mode_norm == "upsert":
                    existing_row = await database.fetch_one(
                        """
                        SELECT *
                        FROM external_product_seeds
                        WHERE market = :market
                          AND tool = :tool
                          AND external_product_id = :external_product_id
                        ORDER BY updated_at DESC, created_at DESC
                        LIMIT 1
                        """,
                        {"market": row_market, "tool": row_tool, "external_product_id": external_product_id},
                    )
                    if not existing_row:
                        match_url = canonical_url or dest
                        existing_row = await database.fetch_one(
                            """
                            SELECT *
                            FROM external_product_seeds
                            WHERE market = :market
                              AND tool = :tool
                              AND (canonical_url = :match_url OR destination_url = :match_url)
                            ORDER BY updated_at DESC, created_at DESC
                            LIMIT 1
                            """,
                            {"market": row_market, "tool": row_tool, "match_url": match_url},
                        )

                if existing_row:
                    existing = dict(existing_row)
                    seed_id = str(existing.get("id"))
                    seed_data = _ensure_json_obj(existing.get("seed_data"))
                    seed_data["external_product_id"] = seed_data.get("external_product_id") or external_product_id
                    seed_data["source"] = seed_data.get("source") or "employee_seed_csv"

                    if title is not None:
                        seed_data["title"] = title
                    if description is not None:
                        seed_data["description"] = description
                    if image_url is not None:
                        seed_data["image_url"] = image_url
                    if availability is not None:
                        seed_data["availability"] = availability
                    if utm_template is not None:
                        seed_data["utm_template"] = utm_template
                    if partner_type is not None:
                        seed_data["partner_type"] = partner_type
                    if disclosure_text is not None:
                        seed_data["disclosure_text"] = disclosure_text

                    canonical_url_update = canonical_url if canonical_url is not None else existing.get("canonical_url")
                    domain_update = domain if domain is not None else existing.get("domain")

                    update_values: Dict[str, Any] = {
                        "id": seed_id,
                        "external_product_id": external_product_id,
                        "destination_url": dest,
                        "canonical_url": canonical_url_update,
                        "domain": domain_update,
                        "seed_data": _seed_data_payload(seed_data),
                        "created_by_employee_id": str(employee_id) if employee_id else None,
                    }
                    set_clauses = [
                        "external_product_id = :external_product_id",
                        "destination_url = :destination_url",
                        "canonical_url = :canonical_url",
                        "domain = :domain",
                        "seed_data = :seed_data",
                        "updated_at = NOW()",
                        "created_by_employee_id = :created_by_employee_id",
                    ]

                    if title is not None:
                        update_values["title"] = title
                        set_clauses.append("title = :title")
                    if image_url is not None:
                        update_values["image_url"] = image_url
                        set_clauses.append("image_url = :image_url")
                    if price_amount is not None:
                        update_values["price_amount"] = price_amount
                        set_clauses.append("price_amount = :price_amount")
                    if price_currency is not None:
                        update_values["price_currency"] = price_currency
                        set_clauses.append("price_currency = :price_currency")
                    if availability is not None:
                        update_values["availability"] = availability
                        set_clauses.append("availability = :availability")
                    if utm_template is not None:
                        update_values["utm_template"] = utm_template
                        set_clauses.append("utm_template = :utm_template")
                    if partner_type is not None:
                        update_values["partner_type"] = partner_type
                        set_clauses.append("partner_type = :partner_type")
                    if disclosure_text is not None:
                        update_values["disclosure_text"] = disclosure_text
                        set_clauses.append("disclosure_text = :disclosure_text")
                    if notes is not None:
                        update_values["notes"] = notes
                        set_clauses.append("notes = :notes")
                    if status is not None:
                        if status not in ("active", "disabled"):
                            raise ValueError("INVALID_STATUS")
                        update_values["status"] = status
                        set_clauses.append("status = :status")
                    if attached_product_key is not None:
                        update_values["attached_product_key"] = attached_product_key
                        set_clauses.append("attached_product_key = :attached_product_key")
                        update_values["attached_variant_id"] = attached_variant_id or "∅"
                        set_clauses.append("attached_variant_id = :attached_variant_id")

                    await _execute_seed_data_stmt(
                        f"UPDATE external_product_seeds SET {', '.join(set_clauses)} WHERE id = :id",
                        update_values,
                    )
                    updated += 1
                    seed_ids.append(seed_id)
                    continue

                # Create
                seed_id = _seed_id()
                seed_data: Dict[str, Any] = {
                    "external_product_id": external_product_id,
                    "title": title,
                    "description": description,
                    "image_url": image_url,
                    "availability": availability,
                    "utm_template": utm_template,
                    "partner_type": partner_type,
                    "disclosure_text": disclosure_text or DEFAULT_DISCLOSURE_TEXT,
                    "source": "employee_seed_csv",
                }

                await _execute_seed_data_stmt(
                    """
                    INSERT INTO external_product_seeds (
                      id, external_product_id, market, tool,
                      utm_template, partner_type, disclosure_text,
                      destination_url, canonical_url, domain,
                      title, image_url, price_amount, price_currency, availability,
                      seed_data, status, notes, created_by_employee_id, attached_product_key, attached_variant_id,
                      created_at, updated_at
                    ) VALUES (
                      :id, :external_product_id, :market, :tool,
                      :utm_template, :partner_type, :disclosure_text,
                      :destination_url, :canonical_url, :domain,
                      :title, :image_url, :price_amount, :price_currency, :availability,
                      :seed_data, :status, :notes, :created_by_employee_id, :attached_product_key, :attached_variant_id,
                      NOW(), NOW()
                    )
                    """,
                    {
                        "id": seed_id,
                        "external_product_id": external_product_id,
                        "market": row_market,
                        "tool": row_tool,
                        "utm_template": utm_template,
                        "partner_type": partner_type,
                        "disclosure_text": disclosure_text or DEFAULT_DISCLOSURE_TEXT,
                        "destination_url": dest,
                        "canonical_url": canonical_url,
                        "domain": domain,
                        "title": title,
                        "image_url": image_url,
                        "price_amount": price_amount,
                        "price_currency": price_currency,
                        "availability": availability,
                        "seed_data": _seed_data_payload(seed_data),
                        "status": status if status in ("active", "disabled") else "active",
                        "notes": notes,
                        "created_by_employee_id": created_by,
                        "attached_product_key": attached_product_key,
                        "attached_variant_id": attached_variant_id or ("∅" if attached_product_key else None),
                    },
                )
                created += 1
                seed_ids.append(seed_id)
            except HTTPException as exc:
                errors.append(f"Row {idx}: {exc.detail}")
            except Exception as exc:
                errors.append(f"Row {idx}: {str(exc)}")

    return ExternalSeedsCsvImportResponse(created=created, updated=updated, errors=errors, seedIds=seed_ids)


@router.post("/external-seeds/preview")
async def preview_external_seed(
    body: PreviewExternalSeedRequest,
    current_user: dict = Depends(get_current_employee),
):
    market = _normalize_market(body.market)
    dest = _require_http_url(body.destination_url)

    snapshot = None
    try:
        snapshot = await resolve_external_offer(market=market, url=dest, force_refresh=bool(body.force_refresh))
    except Exception as exc:
        parsed = urlparse(dest)
        domain = parsed.hostname or None
        path_tail = (parsed.path or "").rstrip("/").split("/")[-1]
        guess = path_tail.replace("-", " ").replace("_", " ").strip()
        guess_title = guess.title() if guess else None
        return {
            "status": "degraded",
            "error": f"snapshot_failed: {str(exc)[:200]}",
            "preview": {
                "url": dest,
                "canonical_url": dest,
                "domain": domain,
                "external_product_id": _stable_external_product_id(dest),
                "title": guess_title,
                "description": None,
                "image_url": None,
                "image_urls": [],
                "price_amount": None,
                "price_currency": None,
                "availability": None,
                "variants": [],
                "domain_allowed": True,
            },
        }

    canonical_url = getattr(snapshot, "canonical_url", None) or dest
    domain = getattr(snapshot, "domain", None)

    # Preview is best-effort and does not persist anything.
    domain_allowed = True
    try:
        domain_allowed = await _is_domain_allowed(market=market, destination_url=canonical_url)
    except Exception:
        domain_allowed = True

    variants = []
    image_urls: list[str] = []
    description = None
    evidence = getattr(snapshot, "evidence", None)
    if isinstance(evidence, dict):
        raw_variants = evidence.get("variants")
        if isinstance(raw_variants, list):
            variants = raw_variants
        raw_images = evidence.get("image_urls") or evidence.get("images")
        if isinstance(raw_images, list):
            image_urls = [str(u).strip() for u in raw_images if isinstance(u, str) and str(u).strip()]
        raw_desc = evidence.get("description")
        if isinstance(raw_desc, str) and raw_desc.strip():
            description = raw_desc.strip()

    return {
        "status": "success",
        "preview": {
            "url": dest,
            "canonical_url": canonical_url,
            "domain": domain,
            "external_product_id": _stable_external_product_id(canonical_url),
            "title": getattr(snapshot, "title", None),
            "description": description,
            "image_url": getattr(snapshot, "image_url", None),
            "image_urls": image_urls,
            "price_amount": getattr(snapshot, "price_amount", None),
            "price_currency": getattr(snapshot, "price_currency", None),
            "availability": getattr(snapshot, "availability", None),
            "variants": variants,
            "domain_allowed": domain_allowed,
        },
    }


@router.get("/external-seeds")
async def list_external_seeds(
    q: Optional[str] = Query(default=None),
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
    if q:
        q = q.strip()
        if q:
            values["q"] = q
            values["q_like"] = f"%{q}%"
            where.append(
                "("
                "destination_url ILIKE :q_like"
                " OR canonical_url ILIKE :q_like"
                " OR domain ILIKE :q_like"
                " OR title ILIKE :q_like"
                " OR id = :q"
                " OR external_product_id = :q"
                " OR seed_data->>'external_product_id' = :q"
                " OR seed_data->>'product_id' = :q"
                " OR seed_data->'product'->>'product_id' = :q"
                " OR EXISTS ("
                "   SELECT 1"
                "   FROM jsonb_array_elements("
                "     CASE"
                "       WHEN jsonb_typeof(seed_data->'variants') = 'array' THEN seed_data->'variants'"
                "       ELSE '[]'::jsonb"
                "     END"
                "   ) AS v"
                "   WHERE (v->>'variant_id' = :q OR v->>'id' = :q OR v->>'sku' = :q)"
                " )"
                ")"
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
            ORDER BY created_at DESC
            LIMIT :limit
            """,
            values,
        )
    except Exception:
        # Fallback for non-Postgres environments: drop JSONB variant/id filters.
        where_fallback = ["status = :status"]
        values_fallback: Dict[str, Any] = {"status": status, "limit": limit}
        if attached is True:
            where_fallback.append("attached_product_key IS NOT NULL")
        elif attached is False:
            where_fallback.append("attached_product_key IS NULL")
        if q:
            q2 = q.strip()
            if q2:
                where_fallback.append(
                    "(destination_url ILIKE :q_like OR canonical_url ILIKE :q_like OR domain ILIKE :q_like OR title ILIKE :q_like)"
                )
                values_fallback["q_like"] = f"%{q2}%"

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
            WHERE {" AND ".join(where_fallback)}
            ORDER BY created_at DESC
            LIMIT :limit
            """,
            values_fallback,
        )
    items = []
    for r in rows:
        r = dict(r)
        seed_data = _ensure_json_obj(r.get("seed_data"))
        merchant_display_name = _seed_merchant_display_name(seed_data, r.get("domain"))
        external_product_id = (
            r.get("external_product_id")
            or seed_data.get("external_product_id")
            or _stable_external_product_id(r.get("canonical_url") or r.get("destination_url") or "")
        )
        items.append(
            {
                "id": r.get("id"),
                "external_product_id": external_product_id,
                "market": r.get("market"),
                "tool": r.get("tool"),
                "utm_template": r.get("utm_template") or seed_data.get("utm_template"),
                "partner_type": r.get("partner_type") or seed_data.get("partner_type"),
                "disclosure_text": r.get("disclosure_text") or seed_data.get("disclosure_text") or DEFAULT_DISCLOSURE_TEXT,
                "destination_url": r.get("destination_url"),
                "canonical_url": r.get("canonical_url"),
                "domain": r.get("domain"),
                "title": seed_data.get("title") or r.get("title"),
                "image_url": seed_data.get("image_url") or r.get("image_url"),
                "merchant_display_name": merchant_display_name,
                "price": _seed_primary_price(r, seed_data),
                "availability": seed_data.get("availability") or r.get("availability"),
                "product": {
                    "product_id": seed_data.get("product_id") or seed_data.get("product", {}).get("product_id"),
                    "brand": seed_data.get("brand") or seed_data.get("product", {}).get("brand"),
                    "category": seed_data.get("category") or seed_data.get("product", {}).get("category"),
                    "external_product_id": external_product_id,
                },
                "variants_count": len(_seed_variants(seed_data)),
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
    snap_title = getattr(snapshot, "title", None) if snapshot else None
    snap_image_url = getattr(snapshot, "image_url", None) if snapshot else None
    snap_price_amount = getattr(snapshot, "price_amount", None) if snapshot else None
    snap_price_currency = getattr(snapshot, "price_currency", None) if snapshot else None
    snap_availability = getattr(snapshot, "availability", None) if snapshot else None
    snap_description: Optional[str] = None
    snap_variants: Optional[List[Dict[str, Any]]] = None
    evidence = getattr(snapshot, "evidence", None) if snapshot else None
    if isinstance(evidence, dict):
        raw_desc = evidence.get("description")
        if isinstance(raw_desc, str) and raw_desc.strip():
            snap_description = raw_desc.strip()
        raw_variants = evidence.get("variants")
        if isinstance(raw_variants, list) and raw_variants:
            snap_variants = [v for v in raw_variants if isinstance(v, dict)]

    match_url = canonical_url or dest
    existing_row = await database.fetch_one(
        """
        SELECT *
        FROM external_product_seeds
        WHERE status = 'active'
          AND market = :market
          AND tool = :tool
          AND (canonical_url = :match_url OR destination_url = :match_url)
        ORDER BY updated_at DESC, created_at DESC
        LIMIT 1
        """,
        {"market": market, "tool": tool, "match_url": match_url},
    )

    # Merge: employee-provided fields override snapshot-derived values.
    title = (body.title or "").strip() or snap_title
    description = (body.description or "").strip() or snap_description
    image_url = (body.image_url or "").strip() or snap_image_url
    price_amount = body.price_amount if body.price_amount is not None else snap_price_amount
    price_currency = (body.price_currency or "").strip() or snap_price_currency
    availability = (body.availability or "").strip() or snap_availability
    utm_template = (body.utm_template or "").strip() or None
    partner_type = (body.partner_type or "").strip() or None
    disclosure_text = (body.disclosure_text or "").strip() or DEFAULT_DISCLOSURE_TEXT

    if existing_row:
        row = dict(existing_row)
        seed_id = row.get("id")
        existing_seed_data = _ensure_json_obj(row.get("seed_data"))
        canonical_url = canonical_url or row.get("canonical_url")
        domain = domain or row.get("domain")

        title = title or existing_seed_data.get("title") or row.get("title")
        description = description or existing_seed_data.get("description")
        image_url = image_url or existing_seed_data.get("image_url") or row.get("image_url")
        if price_amount is None:
            price_amount = row.get("price_amount")
        if not price_currency:
            price_currency = row.get("price_currency")
        availability = availability or existing_seed_data.get("availability") or row.get("availability")
        utm_template = utm_template or row.get("utm_template") or existing_seed_data.get("utm_template")
        partner_type = partner_type or row.get("partner_type") or existing_seed_data.get("partner_type")
        disclosure_text = (
            disclosure_text
            or row.get("disclosure_text")
            or existing_seed_data.get("disclosure_text")
            or DEFAULT_DISCLOSURE_TEXT
        )

        attached_product_key = attached_product_key or row.get("attached_product_key")
        attached_variant_id = attached_variant_id or row.get("attached_variant_id")
        notes = body.notes if body.notes is not None else row.get("notes")
        market = market or row.get("market")
        tool = tool or row.get("tool")

        seed_data = dict(existing_seed_data)
        external_product_id = (
            row.get("external_product_id")
            or seed_data.get("external_product_id")
            or _stable_external_product_id(canonical_url or dest)
        )
        seed_data["external_product_id"] = external_product_id
        if body.merchant_display_name is not None:
            seed_data["merchant_display_name"] = (body.merchant_display_name or "").strip() or None
        if body.product_id is not None:
            seed_data["product_id"] = (body.product_id or "").strip() or None
        if body.brand is not None:
            seed_data["brand"] = (body.brand or "").strip() or None
        if body.category is not None:
            seed_data["category"] = (body.category or "").strip() or None
        seed_data["title"] = title
        seed_data["description"] = description
        seed_data["image_url"] = image_url
        seed_data["availability"] = availability
        seed_data["utm_template"] = utm_template
        seed_data["partner_type"] = partner_type
        seed_data["disclosure_text"] = disclosure_text
        seed_data["source"] = seed_data.get("source") or "employee_seed"
        seed_data.setdefault("snapshot", {})
        seed_data["snapshot"].update(
            {
                "canonical_url": canonical_url,
                "domain": domain,
                "title": snap_title,
                "image_url": snap_image_url,
                "price_amount": snap_price_amount,
                "price_currency": snap_price_currency,
                "availability": snap_availability,
            }
        )
        if snap_description:
            seed_data["snapshot"]["description"] = snap_description
        if body.variants is not None and body.variants:
            seed_data["variants"] = body.variants
        elif snap_variants:
            existing_variants = _seed_variants(seed_data)
            if _should_overwrite_seed_variants(existing=existing_variants, incoming=snap_variants, product_title=title):
                seed_data["variants"] = snap_variants
            elif not existing_variants:
                seed_data["variants"] = snap_variants

        match_url = canonical_url or dest

        await _execute_seed_data_stmt(
            """
            UPDATE external_product_seeds
            SET external_product_id = :external_product_id,
                market = :market,
                tool = :tool,
                utm_template = :utm_template,
                partner_type = :partner_type,
                disclosure_text = :disclosure_text,
                destination_url = :destination_url,
                canonical_url = :canonical_url,
                domain = :domain,
                title = :title,
                image_url = :image_url,
                price_amount = :price_amount,
                price_currency = :price_currency,
                availability = :availability,
                seed_data = :seed_data,
                notes = :notes,
                created_by_employee_id = :created_by_employee_id,
                attached_product_key = :attached_product_key,
                attached_variant_id = :attached_variant_id,
                updated_at = NOW()
            WHERE id = :id
            """,
            {
                "id": seed_id,
                "external_product_id": external_product_id,
                "market": market,
                "tool": tool,
                "utm_template": utm_template,
                "partner_type": partner_type,
                "disclosure_text": disclosure_text,
                "destination_url": dest,
                "canonical_url": canonical_url,
                "domain": domain,
                "title": title,
                "image_url": image_url,
                "price_amount": price_amount,
                "price_currency": price_currency,
                "availability": availability,
                "seed_data": _seed_data_payload(seed_data),
                "notes": notes,
                "created_by_employee_id": str(employee_id) if employee_id else None,
                "attached_product_key": attached_product_key,
                "attached_variant_id": attached_variant_id,
            },
        )
        await database.execute(
            """
            UPDATE external_product_seeds
            SET status = 'disabled',
                notes = COALESCE(notes, '') || :note,
                updated_at = NOW()
            WHERE id <> :id
              AND status = 'active'
              AND market = :market
              AND tool = :tool
              AND (canonical_url = :match_url OR destination_url = :match_url)
            """,
            {
                "id": seed_id,
                "market": market,
                "tool": tool,
                "match_url": match_url,
                "note": f" superseded_by:{seed_id}",
            },
        )
    else:
        seed_id = _seed_id()
        external_product_id = _stable_external_product_id(match_url)
        seed_data: Dict[str, Any] = {
            "external_product_id": external_product_id,
            "merchant_display_name": (body.merchant_display_name or "").strip() or None,
            "product_id": (body.product_id or "").strip() or None,
            "brand": (body.brand or "").strip() or None,
            "category": (body.category or "").strip() or None,
            "title": title,
            "description": description,
            "image_url": image_url,
            "availability": availability,
            "variants": body.variants or (snap_variants or []),
            "utm_template": utm_template,
            "partner_type": partner_type,
            "disclosure_text": disclosure_text,
            "source": "employee_seed",
            "snapshot": {
                "canonical_url": canonical_url,
                "domain": domain,
                "title": snap_title,
                "description": snap_description,
                "image_url": snap_image_url,
                "price_amount": snap_price_amount,
                "price_currency": snap_price_currency,
                "availability": snap_availability,
            },
        }

        await _execute_seed_data_stmt(
            """
            INSERT INTO external_product_seeds (
              id, external_product_id, market, tool, utm_template, partner_type, disclosure_text,
              destination_url, canonical_url, domain, title, image_url,
              price_amount, price_currency, availability,
              seed_data,
              status, notes, created_by_employee_id, attached_product_key, attached_variant_id
            ) VALUES (
              :id, :external_product_id, :market, :tool, :utm_template, :partner_type, :disclosure_text,
              :destination_url, :canonical_url, :domain, :title, :image_url,
              :price_amount, :price_currency, :availability,
              :seed_data,
              'active', :notes, :created_by_employee_id, :attached_product_key, :attached_variant_id
            )
            """,
            {
                "id": seed_id,
                "external_product_id": external_product_id,
                "market": market,
                "tool": tool,
                "utm_template": utm_template,
                "partner_type": partner_type,
                "disclosure_text": disclosure_text,
                "destination_url": dest,
                "canonical_url": canonical_url,
                "domain": domain,
                "title": title,
                "image_url": image_url,
                "price_amount": price_amount,
                "price_currency": price_currency,
                "availability": availability,
                "seed_data": _seed_data_payload(seed_data),
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
        utm_template=utm_template,
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
            "external_product_id": seed_data.get("external_product_id"),
            "market": market,
            "tool": tool,
            "utm_template": utm_template,
            "partner_type": partner_type,
            "disclosure_text": disclosure_text,
            "destination_url": dest,
            "canonical_url": canonical_url or dest,
            "domain": domain,
            "title": title,
            "image_url": image_url,
            "merchant_display_name": _seed_merchant_display_name(seed_data, domain),
            "price": {"amount": price_amount, "currency": price_currency},
            "availability": availability,
            "notes": notes if existing_row else body.notes,
            "attached_product_key": attached_product_key,
            "attached_variant_id": attached_variant_id,
            "product": {
                "external_product_id": seed_data.get("external_product_id"),
                "product_id": seed_data.get("product_id"),
                "brand": seed_data.get("brand"),
                "category": seed_data.get("category"),
            },
            "variants": _seed_variants(seed_data),
        },
        "action": {"type": "redirect", "redirect_url": redirect_url, "disclosure_text": disclosure_text},
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
    seed_data = _ensure_json_obj(row.get("seed_data"))
    external_product_id = (
        row.get("external_product_id")
        or seed_data.get("external_product_id")
        or _stable_external_product_id(row.get("canonical_url") or row.get("destination_url") or "")
    )
    redirect_url = await _make_redirect_url(
        request=request,
        market=row.get("market"),
        tool=row.get("tool"),
        destination_url=row.get("canonical_url") or row.get("destination_url"),
        utm_template=row.get("utm_template") or seed_data.get("utm_template"),
        ctx={
            "seedId": row.get("id"),
            **({"productKey": row.get("attached_product_key")} if row.get("attached_product_key") else {}),
            **({"variantId": row.get("attached_variant_id")} if row.get("attached_variant_id") else {}),
        },
    )
    disclosure_text = row.get("disclosure_text") or seed_data.get("disclosure_text") or DEFAULT_DISCLOSURE_TEXT
    return {
        "status": "success",
        "seed": {
            "id": row.get("id"),
            "external_product_id": external_product_id,
            "market": row.get("market"),
            "tool": row.get("tool"),
            "utm_template": row.get("utm_template") or seed_data.get("utm_template"),
            "partner_type": row.get("partner_type") or seed_data.get("partner_type"),
            "disclosure_text": disclosure_text,
            "destination_url": row.get("destination_url"),
            "canonical_url": row.get("canonical_url"),
            "domain": row.get("domain"),
            "title": seed_data.get("title") or row.get("title"),
            "image_url": seed_data.get("image_url") or row.get("image_url"),
            "merchant_display_name": _seed_merchant_display_name(seed_data, row.get("domain")),
            "price": _seed_primary_price(row, seed_data),
            "availability": seed_data.get("availability") or row.get("availability"),
            "status": row.get("status"),
            "notes": row.get("notes"),
            "attached_product_key": row.get("attached_product_key"),
            "attached_variant_id": row.get("attached_variant_id"),
            "created_at": row.get("created_at").isoformat() if row.get("created_at") else None,
            "updated_at": row.get("updated_at").isoformat() if row.get("updated_at") else None,
            "seed_data": seed_data,
            "variants": _seed_variants(seed_data),
            "variants_count": len(_seed_variants(seed_data)),
            "product": {
                "external_product_id": external_product_id,
                "product_id": seed_data.get("product_id") or seed_data.get("product", {}).get("product_id"),
                "brand": seed_data.get("brand") or seed_data.get("product", {}).get("brand"),
                "category": seed_data.get("category") or seed_data.get("product", {}).get("category"),
            },
        },
        "action": {"type": "redirect", "redirect_url": redirect_url, "disclosure_text": disclosure_text},
    }


@router.patch("/external-seeds/{seed_id}")
async def update_external_seed(
    seed_id: str,
    body: UpdateExternalSeedRequest,
    current_user: dict = Depends(get_current_employee),
):
    await _ensure_external_seeds_table()
    row = await database.fetch_one("SELECT * FROM external_product_seeds WHERE id = :id", {"id": seed_id})
    if not row:
        raise HTTPException(status_code=404, detail="SEED_NOT_FOUND")
    row = dict(row)
    seed_data = _ensure_json_obj(row.get("seed_data"))
    external_product_id = (
        row.get("external_product_id")
        or seed_data.get("external_product_id")
        or _stable_external_product_id(row.get("canonical_url") or row.get("destination_url") or "")
    )
    seed_data["external_product_id"] = seed_data.get("external_product_id") or external_product_id

    if body.title is not None:
        seed_data["title"] = body.title
    if body.description is not None:
        seed_data["description"] = body.description
    if body.image_url is not None:
        seed_data["image_url"] = body.image_url
    if body.availability is not None:
        seed_data["availability"] = body.availability
    if body.product_id is not None:
        seed_data["product_id"] = body.product_id
    if body.brand is not None:
        seed_data["brand"] = body.brand
    if body.category is not None:
        seed_data["category"] = body.category
    if body.variants is not None:
        seed_data["variants"] = body.variants
    if body.utm_template is not None:
        seed_data["utm_template"] = (body.utm_template or "").strip() or None
    if body.partner_type is not None:
        seed_data["partner_type"] = (body.partner_type or "").strip() or None
    if body.disclosure_text is not None:
        seed_data["disclosure_text"] = (body.disclosure_text or "").strip() or DEFAULT_DISCLOSURE_TEXT

    updates: Dict[str, Any] = {"id": seed_id}
    set_clauses: List[str] = []

    if external_product_id:
        updates["external_product_id"] = external_product_id
        set_clauses.append("external_product_id = :external_product_id")

    if body.market is not None:
        updates["market"] = _normalize_market(body.market)
        set_clauses.append("market = :market")
    if body.tool is not None:
        updates["tool"] = _normalize_tool(body.tool)
        set_clauses.append("tool = :tool")
    if body.notes is not None:
        updates["notes"] = body.notes
        set_clauses.append("notes = :notes")
    if body.status is not None:
        updates["status"] = str(body.status).strip() or "active"
        set_clauses.append("status = :status")
    if body.merchant_display_name is not None:
        seed_data["merchant_display_name"] = (body.merchant_display_name or "").strip() or None
    if body.utm_template is not None:
        updates["utm_template"] = (body.utm_template or "").strip() or None
        set_clauses.append("utm_template = :utm_template")
    if body.partner_type is not None:
        updates["partner_type"] = (body.partner_type or "").strip() or None
        set_clauses.append("partner_type = :partner_type")
    if body.disclosure_text is not None:
        updates["disclosure_text"] = (body.disclosure_text or "").strip() or DEFAULT_DISCLOSURE_TEXT
        set_clauses.append("disclosure_text = :disclosure_text")

    # Compatibility: keep summary columns in sync for existing list views.
    if body.title is not None:
        updates["title"] = body.title
        set_clauses.append("title = :title")
    if body.image_url is not None:
        updates["image_url"] = body.image_url
        set_clauses.append("image_url = :image_url")
    if body.price_amount is not None:
        updates["price_amount"] = body.price_amount
        set_clauses.append("price_amount = :price_amount")
    if body.price_currency is not None:
        updates["price_currency"] = body.price_currency
        set_clauses.append("price_currency = :price_currency")
    if body.availability is not None:
        updates["availability"] = body.availability
        set_clauses.append("availability = :availability")

    updates["seed_data"] = _seed_data_payload(seed_data)
    set_clauses.append("seed_data = :seed_data")
    set_clauses.append("updated_at = NOW()")

    await _execute_seed_data_stmt(
        f"UPDATE external_product_seeds SET {', '.join(set_clauses)} WHERE id = :id",
        updates,
    )
    return {"status": "success"}


@router.post("/external-seeds/{seed_id}/refresh")
async def refresh_external_seed(
    seed_id: str,
    request: Request,
    current_user: dict = Depends(get_current_employee),
):
    await _ensure_external_seeds_table()
    row = await database.fetch_one("SELECT * FROM external_product_seeds WHERE id = :id", {"id": seed_id})
    if not row:
        raise HTTPException(status_code=404, detail="SEED_NOT_FOUND")
    row = dict(row)

    market = row.get("market")
    tool = row.get("tool")
    dest = row.get("destination_url")
    if not dest:
        raise HTTPException(status_code=400, detail="INVALID_URL")

    snapshot = None
    try:
        snapshot = await resolve_external_offer(market=market, url=dest, force_refresh=True)
    except Exception as exc:
        return {"status": "degraded", "error": f"snapshot_failed: {str(exc)[:200]}"}

    canonical_url = getattr(snapshot, "canonical_url", None) if snapshot else None
    domain = getattr(snapshot, "domain", None) if snapshot else None
    snap_title = getattr(snapshot, "title", None) if snapshot else None
    snap_image_url = getattr(snapshot, "image_url", None) if snapshot else None
    snap_price_amount = getattr(snapshot, "price_amount", None) if snapshot else None
    snap_price_currency = getattr(snapshot, "price_currency", None) if snapshot else None
    snap_availability = getattr(snapshot, "availability", None) if snapshot else None

    snap_image_urls: list[str] = []
    snap_description: Optional[str] = None
    snap_variants: Optional[List[Dict[str, Any]]] = None
    evidence = getattr(snapshot, "evidence", None)
    if isinstance(evidence, dict):
        raw_images = evidence.get("image_urls") or evidence.get("images")
        if isinstance(raw_images, list):
            snap_image_urls = [str(u).strip() for u in raw_images if isinstance(u, str) and str(u).strip()]
        raw_desc = evidence.get("description")
        if isinstance(raw_desc, str) and raw_desc.strip():
            snap_description = raw_desc.strip()
        raw_variants = evidence.get("variants")
        if isinstance(raw_variants, list) and raw_variants:
            snap_variants = [v for v in raw_variants if isinstance(v, dict)]

    seed_data = _ensure_json_obj(row.get("seed_data"))
    seed_data.setdefault("snapshot", {})
    seed_data["snapshot"].update(
        {
            "canonical_url": canonical_url,
            "domain": domain,
            "title": snap_title,
            "description": snap_description,
            "image_url": snap_image_url,
            "image_urls": snap_image_urls,
            "price_amount": snap_price_amount,
            "price_currency": snap_price_currency,
            "availability": snap_availability,
            "refreshed_at": _to_iso(getattr(snapshot, "fetched_at", None)) or None,
        }
    )
    # Only overwrite curated fields if they are missing.
    if not seed_data.get("title"):
        seed_data["title"] = snap_title
    if not seed_data.get("description"):
        if snap_description:
            seed_data["description"] = snap_description
    if not seed_data.get("image_url"):
        seed_data["image_url"] = snap_image_url
    if snap_image_urls:
        seed_data["image_urls"] = snap_image_urls
    if not seed_data.get("availability"):
        seed_data["availability"] = snap_availability
    if snap_variants:
        existing_variants = _seed_variants(seed_data)
        product_title = seed_data.get("title") or snap_title
        if _should_overwrite_seed_variants(existing=existing_variants, incoming=snap_variants, product_title=product_title):
            seed_data["variants"] = snap_variants
        elif not existing_variants:
            seed_data["variants"] = snap_variants

    await _execute_seed_data_stmt(
        """
        UPDATE external_product_seeds
        SET canonical_url = :canonical_url,
            domain = :domain,
            title = COALESCE(title, :title),
            image_url = COALESCE(image_url, :image_url),
            price_amount = COALESCE(price_amount, :price_amount),
            price_currency = COALESCE(price_currency, :price_currency),
            availability = COALESCE(availability, :availability),
            seed_data = :seed_data,
            updated_at = NOW()
        WHERE id = :id
        """,
        {
            "id": seed_id,
            "canonical_url": canonical_url,
            "domain": domain,
            "title": snap_title,
            "image_url": snap_image_url,
            "price_amount": snap_price_amount,
            "price_currency": snap_price_currency,
            "availability": snap_availability,
            "seed_data": _seed_data_payload(seed_data),
        },
    )
    redirect_url = await _make_redirect_url(
        request=request,
        market=market,
        tool=tool,
        destination_url=canonical_url or dest,
        utm_template=row.get("utm_template") or seed_data.get("utm_template"),
        ctx={
            "seedId": seed_id,
            **({"productKey": row.get("attached_product_key")} if row.get("attached_product_key") else {}),
            **({"variantId": row.get("attached_variant_id")} if row.get("attached_variant_id") else {}),
        },
    )
    disclosure_text = row.get("disclosure_text") or seed_data.get("disclosure_text") or DEFAULT_DISCLOSURE_TEXT
    return {"status": "success", "action": {"type": "redirect", "redirect_url": redirect_url, "disclosure_text": disclosure_text}}


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


@router.post("/external-seeds/{seed_id}/detach")
async def detach_external_seed(
    seed_id: str,
    current_user: dict = Depends(get_current_employee),
):
    await _ensure_external_seeds_table()
    row = await database.fetch_one("SELECT id FROM external_product_seeds WHERE id = :id", {"id": seed_id})
    if not row:
        raise HTTPException(status_code=404, detail="SEED_NOT_FOUND")
    await database.execute(
        """
        UPDATE external_product_seeds
        SET attached_product_key = NULL,
            attached_variant_id = NULL,
            updated_at = NOW()
        WHERE id = :id
        """,
        {"id": seed_id},
    )
    return {"status": "success"}


@router.get("/external-seeds/{seed_id}/resolve-subject")
async def resolve_external_seed_subject(
    seed_id: str,
    current_user: dict = Depends(get_current_employee),
):
    """
    Convenience helper for the Employee Portal to resolve the canonical PDP subject for an external seed.

    Returns:
    - product_key (when attached)
    - product_group_id (best-effort; may be null when grouping table is unavailable)
    """
    await _ensure_external_seeds_table()
    row = await database.fetch_one(
        "SELECT id, attached_product_key, attached_variant_id FROM external_product_seeds WHERE id = :id",
        {"id": seed_id},
    )
    if not row:
        raise HTTPException(status_code=404, detail="SEED_NOT_FOUND")
    r = dict(row)
    product_key = (r.get("attached_product_key") or "").strip() or None
    attached_variant_id = (r.get("attached_variant_id") or "").strip() or None
    if product_key:
        attached_variant_id = attached_variant_id or "∅"

    product_group_id: Optional[str] = None
    debug_errors: List[str] = []
    if product_key and product_key.count("|") == 2:
        try:
            parts = [p.strip() for p in product_key.split("|")]
            merchant_id, platform, platform_product_id = parts
            pg_row = await database.fetch_one(
                """
                SELECT product_group_id
                FROM product_group_members
                WHERE merchant_id = :merchant_id
                  AND platform = :platform
                  AND platform_product_id = :platform_product_id
                LIMIT 1
                """,
                {
                    "merchant_id": merchant_id,
                    "platform": platform,
                    "platform_product_id": platform_product_id,
                },
            )
            if pg_row and pg_row.get("product_group_id"):
                product_group_id = str(pg_row["product_group_id"])
        except Exception as exc:
            debug_errors.append(f"resolve_product_group_failed: {str(exc)[:200]}")

    return {
        "status": "degraded" if debug_errors else "success",
        "seed_id": seed_id,
        "product_key": product_key,
        "attached_variant_id": attached_variant_id,
        "product_group_id": product_group_id,
        **({"debug_errors": debug_errors} if debug_errors else {}),
    }


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

    merchant_id, platform, platform_product_id = parts

    values: Dict[str, Any]
    where: str

    if _is_external_seed_product_key(merchant_id=merchant_id, platform=platform):
        values = {"external_product_id": platform_product_id, "pk": product_key, "limit": limit}
        where = (
            "status = 'active' AND (external_product_id = :external_product_id OR (seed_data->>'external_product_id') = :external_product_id)"
        )
        where += " AND (attached_product_key IS NULL OR attached_product_key = :pk)"
        if variant_id:
            values["vid"] = str(variant_id).strip()
            where += " AND COALESCE(attached_variant_id, '∅') = :vid"
        try:
            rows = await database.fetch_all(
                f"""
                SELECT id, market, tool, destination_url, canonical_url, domain, title, image_url,
                       price_amount, price_currency, availability,
                       utm_template, partner_type, disclosure_text,
                       seed_data,
                       notes, attached_variant_id, created_at
                FROM external_product_seeds
                WHERE {where}
                ORDER BY created_at DESC
                LIMIT :limit
                """,
                values,
            )
        except Exception:
            where = "status = 'active' AND external_product_id = :external_product_id"
            where += " AND (attached_product_key IS NULL OR attached_product_key = :pk)"
            if variant_id:
                where += " AND COALESCE(attached_variant_id, '∅') = :vid"
            rows = await database.fetch_all(
                f"""
                SELECT id, market, tool, destination_url, canonical_url, domain, title, image_url,
                       price_amount, price_currency, availability,
                       utm_template, partner_type, disclosure_text,
                       seed_data,
                       notes, attached_variant_id, created_at
                FROM external_product_seeds
                WHERE {where}
                ORDER BY created_at DESC
                LIMIT :limit
                """,
                values,
            )
    else:
        values = {"pk": product_key, "limit": limit}
        where = "attached_product_key = :pk AND status = 'active'"
        if variant_id:
            values["vid"] = str(variant_id).strip()
            where += " AND attached_variant_id = :vid"

        rows = await database.fetch_all(
            f"""
            SELECT id, market, tool, destination_url, canonical_url, domain, title, image_url,
                   price_amount, price_currency, availability,
                   utm_template, partner_type, disclosure_text,
                   seed_data,
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
        seed_data = _ensure_json_obj(r.get("seed_data"))
        merchant_display_name = _seed_merchant_display_name(seed_data, r.get("domain"))
        redirect_url = await _make_redirect_url(
            request=request,
            market=r.get("market"),
            tool=r.get("tool"),
            destination_url=r.get("canonical_url") or r.get("destination_url"),
            utm_template=r.get("utm_template"),
            ctx={
                "seedId": r.get("id"),
                "productKey": product_key,
                "variantId": r.get("attached_variant_id") or "∅",
            },
        )
        disclosure_text = r.get("disclosure_text") or DEFAULT_DISCLOSURE_TEXT
        items.append(
            {
                "id": r.get("id"),
                "market": r.get("market"),
                "tool": r.get("tool"),
                "utm_template": r.get("utm_template"),
                "partner_type": r.get("partner_type"),
                "disclosure_text": disclosure_text,
                "destination_url": r.get("destination_url"),
                "canonical_url": r.get("canonical_url"),
                "domain": r.get("domain"),
                "title": r.get("title"),
                "image_url": r.get("image_url"),
                "merchant_display_name": merchant_display_name,
                "price": _seed_primary_price(r, seed_data),
                "availability": r.get("availability"),
                "notes": r.get("notes"),
                "attached_variant_id": r.get("attached_variant_id") or "∅",
                "created_at": r.get("created_at").isoformat() if r.get("created_at") else None,
                "action": {"type": "redirect", "redirect_url": redirect_url, "disclosure_text": disclosure_text},
            }
        )
    return {"status": "success", "items": items}


class CreateAttachedExternalLinkRequest(BaseModel):
    destination_url: str = Field(..., min_length=1)
    market: Optional[str] = None
    tool: Optional[str] = None
    variant_id: Optional[str] = None
    notes: Optional[str] = None
    utm_template: Optional[str] = None
    partner_type: Optional[str] = None
    disclosure_text: Optional[str] = None


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
            utm_template=body.utm_template,
            partner_type=body.partner_type,
            disclosure_text=body.disclosure_text,
            attach_product_key=pk,
            attach_variant_id=(body.variant_id or "∅"),
        ),
        request=request,
        current_user=current_user,
    )


class SetPrimaryOfferRequest(BaseModel):
    offer_id: str = Field(..., min_length=1)
    offer_type: str = Field(..., min_length=1)


def _build_merchant_offer(
    *,
    product_key: str,
    merchant_id: str,
    platform: str,
    platform_product_id: str,
    product_data: Dict[str, Any],
) -> Dict[str, Any]:
    summary = _extract_product_summary(product_data, platform_product_id)
    variants = summary.get("variants") or []
    return {
        "id": product_key,
        "source": "merchant_product",
        "product_key": product_key,
        "merchant_id": merchant_id,
        "platform": platform,
        "platform_product_id": platform_product_id,
        "product_id": summary.get("product_id") or platform_product_id,
        "title": summary.get("title"),
        "image_url": summary.get("image_url"),
        "price": {"amount": summary.get("price"), "currency": summary.get("currency")},
        "availability": summary.get("availability"),
        "variants_count": len(variants),
    }


async def _build_external_seed_offers(
    *,
    product_key: str,
    request: Request,
    variant_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    await _ensure_external_seeds_table()
    values: Dict[str, Any] = {"pk": product_key}
    where = "attached_product_key = :pk AND status = 'active'"
    if variant_id:
        values["vid"] = str(variant_id).strip()
        where += " AND attached_variant_id = :vid"

    rows = await database.fetch_all(
        f"""
        SELECT
          id, market, tool, destination_url, canonical_url, domain, title, image_url,
          price_amount, price_currency, availability,
          utm_template, partner_type, disclosure_text,
          seed_data,
          attached_variant_id, created_at, updated_at
        FROM external_product_seeds
        WHERE {where}
        ORDER BY created_at DESC
        """,
        values,
    )

    offers: List[Dict[str, Any]] = []
    for r in rows:
        r = dict(r)
        seed_data = _ensure_json_obj(r.get("seed_data"))
        redirect_url = await _make_redirect_url(
            request=request,
            market=r.get("market"),
            tool=r.get("tool"),
            destination_url=r.get("canonical_url") or r.get("destination_url"),
            utm_template=r.get("utm_template") or seed_data.get("utm_template"),
            ctx={
                "seedId": r.get("id"),
                "productKey": product_key,
                "variantId": r.get("attached_variant_id") or "∅",
            },
        )
        disclosure_text = r.get("disclosure_text") or seed_data.get("disclosure_text") or DEFAULT_DISCLOSURE_TEXT
        offers.append(
            {
                "id": r.get("id"),
                "source": "external_seed",
                "seed_id": r.get("id"),
                "market": r.get("market"),
                "tool": r.get("tool"),
                "utm_template": r.get("utm_template") or seed_data.get("utm_template"),
                "partner_type": r.get("partner_type") or seed_data.get("partner_type"),
                "disclosure_text": disclosure_text,
                "destination_url": r.get("destination_url"),
                "canonical_url": r.get("canonical_url"),
                "domain": r.get("domain"),
                "title": seed_data.get("title") or r.get("title"),
                "image_url": seed_data.get("image_url") or r.get("image_url"),
                "merchant_display_name": _seed_merchant_display_name(seed_data, r.get("domain")),
                "price": _seed_primary_price(r, seed_data),
                "availability": seed_data.get("availability") or r.get("availability"),
                "variants_count": len(_seed_variants(seed_data)),
                "attached_variant_id": r.get("attached_variant_id") or "∅",
                "action": {"type": "redirect", "redirect_url": redirect_url, "disclosure_text": disclosure_text},
            }
        )
    return offers


async def _build_external_seed_offers_for_external_product_id(
    *,
    external_product_id: str,
    product_key_for_ctx: str,
    request: Request,
    variant_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    rows = await _fetch_external_seed_rows_by_external_product_id(
        external_product_id=external_product_id,
        limit=200,
        allow_attached_product_key=product_key_for_ctx,
    )
    offers: List[Dict[str, Any]] = []
    for r in rows:
        r = dict(r)
        vid = str(r.get("attached_variant_id") or "∅").strip() or "∅"
        if variant_id is not None and str(variant_id).strip() != vid:
            continue

        seed_data = _ensure_json_obj(r.get("seed_data"))
        redirect_url = await _make_redirect_url(
            request=request,
            market=r.get("market"),
            tool=r.get("tool"),
            destination_url=r.get("canonical_url") or r.get("destination_url"),
            utm_template=r.get("utm_template") or seed_data.get("utm_template"),
            ctx={
                "seedId": r.get("id"),
                "productKey": product_key_for_ctx,
                "variantId": vid,
            },
        )
        disclosure_text = r.get("disclosure_text") or seed_data.get("disclosure_text") or DEFAULT_DISCLOSURE_TEXT
        offers.append(
            {
                "id": r.get("id"),
                "source": "external_seed",
                "seed_id": r.get("id"),
                "market": r.get("market"),
                "tool": r.get("tool"),
                "utm_template": r.get("utm_template") or seed_data.get("utm_template"),
                "partner_type": r.get("partner_type") or seed_data.get("partner_type"),
                "disclosure_text": disclosure_text,
                "destination_url": r.get("destination_url"),
                "canonical_url": r.get("canonical_url"),
                "domain": r.get("domain"),
                "title": seed_data.get("title") or r.get("title"),
                "image_url": seed_data.get("image_url") or r.get("image_url"),
                "merchant_display_name": _seed_merchant_display_name(seed_data, r.get("domain")),
                "price": _seed_primary_price(r, seed_data),
                "availability": seed_data.get("availability") or r.get("availability"),
                "variants_count": len(_seed_variants(seed_data)),
                "attached_variant_id": vid,
                "notes": r.get("notes"),
                "created_at": _to_iso(r.get("created_at")),
                "updated_at": _to_iso(r.get("updated_at")),
                "action": {"type": "redirect", "redirect_url": redirect_url, "disclosure_text": disclosure_text},
            }
        )
    return offers


async def _get_primary_offer(product_key: str) -> Optional[Dict[str, Any]]:
    await _ensure_primary_offers_table()
    row = await database.fetch_one(
        "SELECT product_key, offer_id, offer_type, created_by_employee_id, updated_at FROM employee_product_primary_offers WHERE product_key = :pk",
        {"pk": product_key},
    )
    return dict(row) if row else None


async def _set_primary_offer(
    *,
    product_key: str,
    offer_id: str,
    offer_type: str,
    employee_id: Optional[str],
) -> None:
    await _ensure_primary_offers_table()
    await database.execute(
        """
        INSERT INTO employee_product_primary_offers (product_key, offer_id, offer_type, created_by_employee_id)
        VALUES (:pk, :offer_id, :offer_type, :employee_id)
        ON CONFLICT (product_key)
        DO UPDATE SET
          offer_id = EXCLUDED.offer_id,
          offer_type = EXCLUDED.offer_type,
          created_by_employee_id = EXCLUDED.created_by_employee_id,
          updated_at = NOW()
        """,
        {
            "pk": product_key,
            "offer_id": offer_id,
            "offer_type": offer_type,
            "employee_id": employee_id,
        },
    )


async def _compute_product_metrics(
    *,
    merchant_id: str,
    platform: str,
    platform_product_id: str,
    product_data: Dict[str, Any],
) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    last_7d = now - timedelta(days=7)
    last_30d = now - timedelta(days=30)
    currency = product_data.get("currency") or "USD"
    product_id = product_data.get("product_id") or product_data.get("id") or platform_product_id
    debug_errors: List[str] = []

    sales_7d = 0
    sales_30d = 0
    gmv_7d = 0.0
    gmv_30d = 0.0

    try:
        currency_clause = " AND o.currency = :currency" if currency else ""
        row = await database.fetch_one(
            f"""
            SELECT
              COUNT(DISTINCT o.order_id) FILTER (
                WHERE o.created_at >= :last_7d AND o.payment_status = 'paid'
              )::bigint AS sales_7d,
              COUNT(DISTINCT o.order_id) FILTER (
                WHERE o.created_at >= :last_30d AND o.payment_status = 'paid'
              )::bigint AS sales_30d,
              COALESCE(SUM(CASE WHEN o.created_at >= :last_7d AND o.payment_status = 'paid' THEN o.total ELSE 0 END), 0) AS gmv_7d,
              COALESCE(SUM(CASE WHEN o.created_at >= :last_30d AND o.payment_status = 'paid' THEN o.total ELSE 0 END), 0) AS gmv_30d
            FROM orders o
            WHERE (o.is_deleted IS NULL OR o.is_deleted = FALSE)
              AND o.merchant_id = :merchant_id
              {currency_clause}
              AND EXISTS (
                SELECT 1
                FROM jsonb_array_elements(COALESCE(o.items::jsonb, '[]'::jsonb)) item
                WHERE (item->>'product_id' = :product_id OR item->>'product_id' = :platform_product_id)
              )
            """,
            {
                "last_7d": last_7d,
                "last_30d": last_30d,
                "merchant_id": merchant_id,
                "product_id": product_id,
                "platform_product_id": platform_product_id,
                "currency": currency,
            },
        )
        if row:
            sales_7d = int(row.get("sales_7d") or 0)
            sales_30d = int(row.get("sales_30d") or 0)
            gmv_7d = float(row.get("gmv_7d") or 0)
            gmv_30d = float(row.get("gmv_30d") or 0)
    except Exception as exc:
        debug_errors.append(f"orders metrics failed: {str(exc)[:200]}")
        # Fallback: fetch recent orders and filter in Python (MVP-safe, slower).
        try:
            currency_clause = " AND currency = :currency" if currency else ""
            rows = await database.fetch_all(
                f"""
                SELECT order_id, created_at, payment_status, total, currency, items
                FROM orders
                WHERE (is_deleted IS NULL OR is_deleted = FALSE)
                  AND merchant_id = :merchant_id
                  AND created_at >= :last_30d
                  {currency_clause}
                """,
                {
                    "merchant_id": merchant_id,
                    "last_30d": last_30d,
                    "currency": currency,
                },
            )
            sales_7d = 0
            sales_30d = 0
            gmv_7d = 0.0
            gmv_30d = 0.0
            for row in rows or []:
                r = dict(row)
                if (r.get("payment_status") or "").lower() != "paid":
                    continue
                items = r.get("items")
                if isinstance(items, str):
                    try:
                        items = json.loads(items)
                    except Exception:
                        items = None
                if isinstance(items, dict):
                    items = [items]
                if not isinstance(items, list):
                    continue
                match = False
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    pid = item.get("product_id")
                    if pid == product_id or pid == platform_product_id:
                        match = True
                        break
                if not match:
                    continue
                created_at = r.get("created_at")
                total = r.get("total") or 0
                try:
                    total_f = float(total)
                except Exception:
                    total_f = 0.0
                if created_at and created_at >= last_7d:
                    sales_7d += 1
                    gmv_7d += total_f
                sales_30d += 1
                gmv_30d += total_f
            debug_errors = []
        except Exception as fallback_exc:
            debug_errors.append(f"orders metrics fallback failed: {str(fallback_exc)[:200]}")

    merchants_selling = 1
    try:
        pg_row = await database.fetch_one(
            """
            SELECT product_group_id
            FROM product_group_members
            WHERE merchant_id = :merchant_id
              AND platform = :platform
              AND platform_product_id = :platform_product_id
            LIMIT 1
            """,
            {
                "merchant_id": merchant_id,
                "platform": platform,
                "platform_product_id": platform_product_id,
            },
        )
        product_group_id = str(pg_row["product_group_id"]) if pg_row and pg_row["product_group_id"] else None
        if product_group_id:
            count_row = await database.fetch_one(
                """
                SELECT COUNT(DISTINCT merchant_id)::int AS sellers_count
                FROM product_group_members
                WHERE product_group_id = :product_group_id
                """,
                {"product_group_id": product_group_id},
            )
            if count_row:
                merchants_selling = max(1, int(count_row["sellers_count"] or 0))
    except Exception:
        merchants_selling = 1

    metrics = {
        "sales_7d": sales_7d,
        "sales_30d": sales_30d,
        "gmv_7d": {"currency": currency, "amount": gmv_7d},
        "gmv_30d": {"currency": currency, "amount": gmv_30d},
        "merchants_selling": merchants_selling,
    }
    if debug_errors:
        metrics["debug_errors"] = debug_errors
    return metrics


@router.get("/{product_key}/offers")
async def list_product_offers(
    product_key: str,
    request: Request,
    variant_id: Optional[str] = Query(default=None),
    current_user: dict = Depends(get_current_employee),
):
    parts = [p.strip() for p in (product_key or "").split("|")]
    if len(parts) != 3:
        raise HTTPException(status_code=400, detail="INVALID_PRODUCT_KEY")

    merchant_id, platform, platform_product_id = parts

    if _is_external_seed_product_key(merchant_id=merchant_id, platform=platform):
        offers = await _build_external_seed_offers_for_external_product_id(
            external_product_id=platform_product_id,
            product_key_for_ctx=product_key,
            request=request,
            variant_id=variant_id,
        )

        primary = await _get_primary_offer(product_key)
        if primary:
            primary_offer = {
                "offer_id": primary.get("offer_id"),
                "offer_type": primary.get("offer_type"),
                "updated_at": _to_iso(primary.get("updated_at")),
            }
        else:
            primary_offer = {
                "offer_id": offers[0].get("id") if offers else None,
                "offer_type": "external_seed" if offers else None,
            }

        return {
            "status": "success",
            "product_group_id": None,
            "items": offers,
            "primary": primary_offer,
        }
    row = await _fetch_latest_products_cache_row(
        merchant_id=merchant_id,
        platform=platform,
        platform_product_id=platform_product_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="PRODUCT_NOT_FOUND")

    product_data = _ensure_dict(row.get("product_data"))

    product_group_id: Optional[str] = None
    try:
        pg_row = await database.fetch_one(
            """
            SELECT product_group_id
            FROM product_group_members
            WHERE merchant_id = :merchant_id
              AND platform = :platform
              AND platform_product_id = :platform_product_id
            LIMIT 1
            """,
            {
                "merchant_id": merchant_id,
                "platform": platform,
                "platform_product_id": platform_product_id,
            },
        )
        product_group_id = str(pg_row["product_group_id"]) if pg_row and pg_row["product_group_id"] else None
    except Exception:
        product_group_id = None

    merchant_offers: List[Dict[str, Any]] = []
    if product_group_id:
        try:
            members = await database.fetch_all(
                """
                SELECT
                  pgm.merchant_id,
                  pgm.platform,
                  pgm.platform_product_id,
                  pgm.is_primary,
                  pc.product_data
                FROM product_group_members pgm
                JOIN products_cache pc
                  ON pc.merchant_id = pgm.merchant_id
                 AND pc.platform = pgm.platform
                 AND pc.platform_product_id = pgm.platform_product_id
                WHERE pgm.product_group_id = :product_group_id
                ORDER BY pgm.is_primary DESC, pgm.updated_at DESC
                """,
                {"product_group_id": product_group_id},
            )
            for m in members or []:
                m = dict(m)
                mk = f"{m.get('merchant_id')}|{m.get('platform')}|{m.get('platform_product_id')}"
                md = _ensure_dict(m.get("product_data"))
                merchant_offers.append(
                    _build_merchant_offer(
                        product_key=mk,
                        merchant_id=str(m.get("merchant_id")),
                        platform=str(m.get("platform")),
                        platform_product_id=str(m.get("platform_product_id")),
                        product_data=md,
                    )
                )
        except Exception:
            merchant_offers = []

    if not merchant_offers:
        merchant_offers = [
            _build_merchant_offer(
                product_key=product_key,
                merchant_id=merchant_id,
                platform=platform,
                platform_product_id=platform_product_id,
                product_data=product_data,
            )
        ]

    offers = merchant_offers
    external_offers = await _build_external_seed_offers(
        product_key=product_key, request=request, variant_id=variant_id
    )
    offers.extend(external_offers)

    primary = await _get_primary_offer(product_key)
    if primary:
        primary_offer = {
            "offer_id": primary.get("offer_id"),
            "offer_type": primary.get("offer_type"),
            "updated_at": _to_iso(primary.get("updated_at")),
        }
    else:
        primary_offer = {
            "offer_id": product_key if offers else None,
            "offer_type": "merchant_product" if offers else None,
        }

    return {
        "status": "success",
        "product_group_id": product_group_id,
        "items": offers,
        "primary": primary_offer,
    }


@router.post("/{product_key}/offers/primary")
async def set_primary_offer(
    product_key: str,
    body: SetPrimaryOfferRequest,
    current_user: dict = Depends(get_current_employee),
):
    offer_type_raw = (body.offer_type or "").strip()
    offer_id = (body.offer_id or "").strip()
    if offer_type_raw in {"merchant", "merchant_product"}:
        offer_type = "merchant_product"
    elif offer_type_raw in {"external", "external_seed"}:
        offer_type = "external_seed"
    else:
        raise HTTPException(status_code=400, detail="INVALID_OFFER_TYPE")

    if offer_type == "merchant_product":
        offer_id = product_key
    else:
        await _ensure_external_seeds_table()
        row = await database.fetch_one(
            "SELECT id FROM external_product_seeds WHERE id = :id AND status = 'active'",
            {"id": offer_id},
        )
        if not row:
            raise HTTPException(status_code=404, detail="OFFER_NOT_FOUND")

    employee_id = current_user.get("employee_id") or current_user.get("employeeId")
    await _set_primary_offer(
        product_key=product_key,
        offer_id=offer_id,
        offer_type=offer_type,
        employee_id=str(employee_id) if employee_id else None,
    )
    return {"status": "success"}


@router.get("/{product_key}/metrics")
async def get_product_metrics(
    product_key: str,
    current_user: dict = Depends(get_current_employee),
):
    parts = [p.strip() for p in (product_key or "").split("|")]
    if len(parts) != 3:
        raise HTTPException(status_code=400, detail="INVALID_PRODUCT_KEY")

    merchant_id, platform, platform_product_id = parts
    row = await _fetch_latest_products_cache_row(
        merchant_id=merchant_id,
        platform=platform,
        platform_product_id=platform_product_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="PRODUCT_NOT_FOUND")

    product_data = _ensure_dict(row.get("product_data"))
    metrics = await _compute_product_metrics(
        merchant_id=merchant_id,
        platform=platform,
        platform_product_id=platform_product_id,
        product_data=product_data,
    )
    status = "degraded" if metrics.get("debug_errors") else "success"
    return {"status": status, "metrics": metrics}


@router.get("/search")
async def search_products(
    q: Optional[str] = Query(default=None, description="Search by product_id/platform_product_id/title (best-effort)"),
    merchant_id: Optional[str] = Query(default=None),
    platform: Optional[str] = Query(default=None),
    limit: int = Query(default=30, ge=1, le=100),
    after_id: Optional[int] = Query(default=None, description="Cursor: return rows with id < after_id"),
    group_by: Optional[str] = Query(
        default=None,
        description="Optional grouping mode. Use `product_group` to group by product_group_members.product_group_id when available.",
    ),
    include_expired: bool = Query(default=False, description="Include expired cache rows"),
    current_user: dict = Depends(get_current_employee),
):
    """
    Employee-facing product search over products_cache.

    v0 behavior:
    - Sort: most recently inserted cache rows (id desc).
    - Pagination: keyset on products_cache.id (after_id).
    - Search: best-effort exact id match + title ILIKE when supported.
    """
    use_product_groups = _is_group_by_product_group(group_by)

    where = []
    where_group = []
    values: Dict[str, Any] = {"limit": limit}

    if merchant_id:
        where.append("merchant_id = :merchant_id")
        where_group.append("pc.merchant_id = :merchant_id")
        values["merchant_id"] = merchant_id
    if platform:
        where.append("platform = :platform")
        where_group.append("pc.platform = :platform")
        values["platform"] = platform
    if after_id is not None:
        where.append("id < :after_id")
        where_group.append("pc.id < :after_id")
        values["after_id"] = after_id
    if not include_expired:
        where.append("(expires_at IS NULL OR expires_at > NOW())")
        where_group.append("(pc.expires_at IS NULL OR pc.expires_at > NOW())")

    debug_errors: List[str] = []

    if use_product_groups:
        try:
            # Select one representative row per product_group_id (fallback to per-product key when missing).
            group_key = "COALESCE(pgm.product_group_id, pc.merchant_id || '|' || pc.platform || '|' || pc.platform_product_id)"
            base = f"""
            SELECT *
            FROM (
              SELECT DISTINCT ON ({group_key})
                pc.id,
                pc.merchant_id,
                pc.platform,
                pc.platform_product_id,
                pc.product_data,
                pc.cached_at,
                pc.expires_at,
                pgm.product_group_id AS product_group_id
              FROM products_cache pc
              LEFT JOIN product_group_members pgm
                ON pgm.merchant_id = pc.merchant_id
               AND pgm.platform = pc.platform
               AND pgm.platform_product_id = pc.platform_product_id
            """
            clause = f" WHERE {' AND '.join(where_group)}" if where_group else ""
            # Order within group: prefer primary member, then newest cache row.
            order_limit = f" ORDER BY {group_key}, pgm.is_primary DESC NULLS LAST, pc.id DESC"

            rows: List[Dict[str, Any]] = []
            if q:
                q = q.strip()
            if q:
                # Best-effort: attempt title ILIKE + JSON product_id match (Postgres).
                try:
                    q_clause = (
                        " (pc.platform_product_id = :q"
                        " OR pc.product_data->>'product_id' = :q"
                        " OR pc.product_data->>'id' = :q"
                        " OR pc.product_data->>'title' ILIKE :q_like"
                        " OR pc.product_data->>'name' ILIKE :q_like"
                        " OR EXISTS ("
                        "   SELECT 1"
                        "   FROM jsonb_array_elements("
                        "     CASE"
                        "       WHEN jsonb_typeof(pc.product_data::jsonb->'variants') = 'array' THEN pc.product_data::jsonb->'variants'"
                        "       ELSE '[]'::jsonb"
                        "     END"
                        "   ) AS v"
                        "   WHERE (v->>'variant_id' = :q OR v->>'id' = :q OR v->>'sku' = :q)"
                        " ))"
                    )
                    values["q"] = q
                    values["q_like"] = f"%{q}%"
                    rows = await database.fetch_all(
                        f"{base}{clause}{' AND ' if clause else ' WHERE '}{q_clause}{order_limit}"
                        f"\n            ) t\n            ORDER BY t.id DESC LIMIT :limit",
                        values,
                    )
                except Exception:
                    q_clause = (
                        " (pc.platform_product_id = :q"
                        " OR pc.product_data->>'product_id' = :q"
                        " OR pc.product_data->>'id' = :q)"
                    )
                    values["q"] = q
                    rows = await database.fetch_all(
                        f"{base}{clause}{' AND ' if clause else ' WHERE '}{q_clause}{order_limit}"
                        f"\n            ) t\n            ORDER BY t.id DESC LIMIT :limit",
                        values,
                    )
            else:
                rows = await database.fetch_all(
                    f"{base}{clause}{order_limit}\n            ) t\n            ORDER BY t.id DESC LIMIT :limit",
                    values,
                )

            seller_counts: Dict[str, int] = {}
            group_ids = sorted({str(dict(r).get("product_group_id")) for r in rows if dict(r).get("product_group_id")})
            if group_ids:
                try:
                    counts = await database.fetch_all(
                        """
                        SELECT product_group_id, COUNT(DISTINCT merchant_id)::int AS sellers_count
                        FROM product_group_members
                        WHERE product_group_id = ANY(:group_ids)
                        GROUP BY product_group_id
                        """,
                        {"group_ids": group_ids},
                    )
                    for cr in counts or []:
                        seller_counts[str(cr["product_group_id"])] = int(cr["sellers_count"] or 0)
                except Exception as exc:
                    debug_errors.append(f"group_sellers_count_failed: {str(exc)[:200]}")

            cards: List[Dict[str, Any]] = []
            for r in rows:
                try:
                    rr = dict(r)
                    card = _as_product_card(rr)
                    pgid = rr.get("product_group_id")
                    card["product_group_id"] = pgid
                    if pgid:
                        card["sellers_count"] = max(1, int(seller_counts.get(str(pgid)) or 0))
                    else:
                        card["sellers_count"] = 1
                    cards.append(card)
                except Exception as exc:
                    debug_errors.append(f"card_parse_failed: {str(exc)}")

            next_after_id = int(rows[-1]["id"]) if rows else None
            return {
                "status": "degraded" if debug_errors else "success",
                "items": cards,
                "next": {"after_id": next_after_id},
                **({"debug_errors": debug_errors[:10]} if debug_errors else {}),
            }
        except Exception as exc:
            # Degrade gracefully when the grouping table is unavailable or query fails.
            debug_errors.append(f"group_by_failed: {str(exc)[:200]}")
            use_product_groups = False

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
                " OR product_data->>'name' ILIKE :q_like"
                " OR EXISTS ("
                "   SELECT 1"
                "   FROM jsonb_array_elements("
                "     CASE"
                "       WHEN jsonb_typeof(product_data::jsonb->'variants') = 'array' THEN product_data::jsonb->'variants'"
                "       ELSE '[]'::jsonb"
                "     END"
                "   ) AS v"
                "   WHERE (v->>'variant_id' = :q OR v->>'id' = :q OR v->>'sku' = :q)"
                " ))"
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
    seen_keys: set[str] = set()
    for r in rows:
        try:
            card = _as_product_card(dict(r))
            key = card.get("product_key")
            if key and key in seen_keys:
                continue
            if key:
                seen_keys.add(key)
            cards.append(card)
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

    # External-only canonical product, synthesized from external_product_seeds.
    if _is_external_seed_product_key(merchant_id=merchant_id, platform=platform):
        view = await _build_external_seed_product_view(
            external_product_id=platform_product_id,
            allow_attached_product_key=product_key,
        )
        product_data = _ensure_dict(view.get("product_data"))
        cached_at = view.get("cached_at")
        raw_payload = _ensure_dict(view.get("raw"))

        try:
            sp = StandardProduct.parse_obj(product_data)
            normalized = sp.dict()
        except Exception:
            normalized = None

        metrics = {
            "sales_7d": None,
            "sales_30d": None,
            "gmv_7d": None,
            "gmv_30d": None,
            "merchants_selling": 0,
            "debug": {"product_type": "external_seed"},
        }

        enrichment_row: Optional[Dict[str, Any]] = None
        enrichment: Dict[str, Any] = {}
        enrichment_meta: Dict[str, Any] = {"status": "missing"}
        try:
            enrichment_row = await get_enrichment(
                merchant_id=merchant_id,
                platform=platform,
                platform_product_id=platform_product_id,
                geo_code="default",
            )
            if enrichment_row:
                enrichment = {
                    "title_override": enrichment_row.get("title_override"),
                    "summary_short": enrichment_row.get("summary_short"),
                    "description_markdown": enrichment_row.get("description_markdown"),
                    "bullet_points": enrichment_row.get("bullet_points"),
                    "usage_scenarios": enrichment_row.get("usage_scenarios"),
                    "audience_tags": enrichment_row.get("audience_tags"),
                    "topic_tags": enrichment_row.get("topic_tags"),
                    "regulatory_disclaimer_local": enrichment_row.get("regulatory_disclaimer_local"),
                    "extra_images": enrichment_row.get("extra_images"),
                    "llm_readability_score": enrichment_row.get("llm_readability_score"),
                    "llm_safety_flags": enrichment_row.get("llm_safety_flags"),
                }
                enrichment_meta = {
                    "status": "ready",
                    "geo_code": enrichment_row.get("geo_code") or "default",
                    "updated_at": enrichment_row.get("updated_at").isoformat() if enrichment_row.get("updated_at") else None,
                    "updated_by_employee_id": enrichment_row.get("updated_by_employee_id"),
                    "updated_by_email": enrichment_row.get("updated_by_email"),
                }
        except Exception as exc:
            enrichment = {}
            enrichment_meta = {"status": "degraded", "error": str(exc)[:200]}

        return {
            "status": "success",
            "product_key": product_key,
            "merchant_id": merchant_id,
            "platform": platform,
            "platform_product_id": platform_product_id,
            "cached_at": _to_iso(cached_at),
            "expires_at": None,
            "product": normalized,
            "raw": raw_payload,
            "metrics": metrics,
            "enrichment": enrichment,
            "enrichment_meta": enrichment_meta,
        }

    row = await _fetch_latest_products_cache_row(
        merchant_id=merchant_id,
        platform=platform,
        platform_product_id=platform_product_id,
        include_expired=True,
    )
    if not row:
        raise HTTPException(status_code=404, detail="PRODUCT_NOT_FOUND")

    product_data = _ensure_dict(row.get("product_data"))

    # Parse best-effort StandardProduct for normalized fields, but return the raw JSON as well.
    try:
        sp = StandardProduct.parse_obj(product_data)
        normalized = sp.dict()
    except Exception:
        normalized = None

    metrics = await _compute_product_metrics(
        merchant_id=merchant_id,
        platform=platform,
        platform_product_id=platform_product_id,
        product_data=product_data,
    )

    enrichment_row: Optional[Dict[str, Any]] = None
    enrichment: Dict[str, Any] = {}
    enrichment_meta: Dict[str, Any] = {"status": "missing"}
    try:
        enrichment_row = await get_enrichment(
            merchant_id=merchant_id,
            platform=platform,
            platform_product_id=platform_product_id,
            geo_code="default",
        )
        if enrichment_row:
            enrichment = {
                "title_override": enrichment_row.get("title_override"),
                "summary_short": enrichment_row.get("summary_short"),
                "description_markdown": enrichment_row.get("description_markdown"),
                "bullet_points": enrichment_row.get("bullet_points"),
                "usage_scenarios": enrichment_row.get("usage_scenarios"),
                "audience_tags": enrichment_row.get("audience_tags"),
                "topic_tags": enrichment_row.get("topic_tags"),
                "regulatory_disclaimer_local": enrichment_row.get("regulatory_disclaimer_local"),
                "extra_images": enrichment_row.get("extra_images"),
                "llm_readability_score": enrichment_row.get("llm_readability_score"),
                "llm_safety_flags": enrichment_row.get("llm_safety_flags"),
            }
            enrichment_meta = {
                "status": "ready",
                "geo_code": enrichment_row.get("geo_code") or "default",
                "updated_at": enrichment_row.get("updated_at").isoformat() if enrichment_row.get("updated_at") else None,
                "updated_by_employee_id": enrichment_row.get("updated_by_employee_id"),
                "updated_by_email": enrichment_row.get("updated_by_email"),
            }
    except Exception as exc:
        enrichment = {}
        enrichment_meta = {"status": "degraded", "error": str(exc)[:200]}

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
        "metrics": metrics,
        "enrichment": enrichment,
        "enrichment_meta": enrichment_meta,
    }

@router.get("/{merchant_id}/{platform}/{platform_product_id}")
async def get_product_by_triplet(
    merchant_id: str,
    platform: str,
    platform_product_id: str,
    current_user: dict = Depends(get_current_employee),
):
    """
    Product detail by explicit triplet path params.

    This is a convenience wrapper for the employee portal to avoid embedding the composite
    `product_key` (merchant|platform|platform_product_id) into a single URL segment.
    """
    product_key = f"{merchant_id}|{platform}|{platform_product_id}"
    return await get_product_by_key(product_key=product_key, current_user=current_user)


class UpdateProductEnrichmentRequest(BaseModel):
    summary_short: Optional[str] = None
    description_markdown: Optional[str] = None


@router.patch("/{product_key}/enrichment")
async def update_product_enrichment(
    product_key: str,
    body: UpdateProductEnrichmentRequest,
    current_user: dict = Depends(get_current_employee),
):
    parts = [p.strip() for p in (product_key or "").split("|")]
    if len(parts) != 3:
        raise HTTPException(status_code=400, detail="INVALID_PRODUCT_KEY")

    merchant_id, platform, platform_product_id = parts

    patch: Dict[str, Any] = {}
    if body.summary_short is not None:
        patch["summary_short"] = (body.summary_short or "").strip() or None
    if body.description_markdown is not None:
        patch["description_markdown"] = (body.description_markdown or "").strip() or None

    if not patch:
        raise HTTPException(status_code=400, detail="NO_FIELDS_TO_UPDATE")

    employee_id = (
        current_user.get("employee_id")
        or current_user.get("employeeId")
        or current_user.get("user_id")
        or current_user.get("sub")
    )
    email = current_user.get("email")
    patch["updated_by_employee_id"] = str(employee_id) if employee_id else None
    patch["updated_by_email"] = str(email) if email else None

    await upsert_enrichment(
        merchant_id=merchant_id,
        platform=platform,
        platform_product_id=platform_product_id,
        geo_code="default",
        data=patch,
    )

    row = await get_enrichment(
        merchant_id=merchant_id,
        platform=platform,
        platform_product_id=platform_product_id,
        geo_code="default",
    )
    if not row:
        return {"status": "degraded", "enrichment": None}

    return {
        "status": "success",
        "enrichment": {
            "title_override": row.get("title_override"),
            "summary_short": row.get("summary_short"),
            "description_markdown": row.get("description_markdown"),
            "bullet_points": row.get("bullet_points"),
            "usage_scenarios": row.get("usage_scenarios"),
            "audience_tags": row.get("audience_tags"),
            "topic_tags": row.get("topic_tags"),
            "regulatory_disclaimer_local": row.get("regulatory_disclaimer_local"),
            "extra_images": row.get("extra_images"),
            "llm_readability_score": row.get("llm_readability_score"),
            "llm_safety_flags": row.get("llm_safety_flags"),
        },
        "enrichment_meta": {
            "status": "ready",
            "geo_code": row.get("geo_code") or "default",
            "updated_at": row.get("updated_at").isoformat() if row.get("updated_at") else None,
            "updated_by_employee_id": row.get("updated_by_employee_id"),
            "updated_by_email": row.get("updated_by_email"),
        },
    }


class CreateManualReviewRequest(BaseModel):
    rating: int = Field(..., ge=1, le=5)
    title: Optional[str] = Field(default=None, max_length=200)
    body: Optional[str] = Field(default=None, max_length=5000)
    variant_id: Optional[str] = None
    status: Optional[str] = None


@router.post("/{product_key}/reviews")
async def create_manual_review(
    product_key: str,
    body: CreateManualReviewRequest,
    actor: Dict[str, Any] = Depends(require_employee_permissions(["reviews.create.manual"])),
):
    parts = [p.strip() for p in (product_key or "").split("|")]
    if len(parts) != 3:
        raise HTTPException(status_code=400, detail="INVALID_PRODUCT_KEY")

    merchant_id, platform, platform_product_id = parts
    status = (body.status or "under_review").strip().lower()
    if status not in {"active", "folded", "removed", "under_review"}:
        raise HTTPException(status_code=400, detail="INVALID_STATUS")

    variant_id = (body.variant_id or "").strip() or None
    pk = build_product_key(
        merchant_id=merchant_id,
        platform=platform,
        platform_product_id=platform_product_id,
    )
    sk = build_sku_key(
        merchant_id=merchant_id,
        platform=platform,
        platform_product_id=platform_product_id,
        variant_id=variant_id,
    )

    now = datetime.now(timezone.utc)
    ext_review_id = f"manual:{uuid.uuid4().hex}"

    review_id = await database.execute(
        product_reviews.insert().values(
            product_key=pk,
            sku_key=sk,
            merchant_id=merchant_id,
            platform=platform,
            platform_product_id=platform_product_id,
            variant_id=variant_id,
            group_id=None,
            author_user_id=None,
            source_type="manual",
            source_system="employee",
            external_review_id=ext_review_id,
            dedupe_key=ext_review_id,
            verification="unverified",
            rating=int(body.rating),
            title=(body.title or "").strip() or None,
            body=(body.body or "").strip() or None,
            media_count=0,
            risk_flags=None,
            status=status,
            created_at=now,
            updated_at=now,
        )
    )

    return {
        "status": "success",
        "review_id": int(review_id) if review_id is not None else None,
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
        "global_merchant_id": GLOBAL_IMPORT_MERCHANT_ID,
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
            WHERE merchant_id IN (:merchant_id, :global_merchant_id)
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
