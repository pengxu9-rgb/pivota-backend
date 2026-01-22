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
import csv
import io
import json
import hashlib
from urllib.parse import urlparse
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
import uuid

from db.database import database
from db.products import products_cache
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


def _seed_primary_price(seed_row: Dict[str, Any], seed_data: Dict[str, Any]) -> Dict[str, Any]:
    variants = _seed_variants(seed_data)
    for v in variants:
        amt = v.get("price_amount")
        cur = v.get("price_currency") or v.get("currency")
        if amt is not None:
            return {"amount": amt, "currency": cur}
    return {"amount": seed_row.get("price_amount"), "currency": seed_row.get("price_currency")}


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


@router.post("/external-seeds/import-csv", response_model=ExternalSeedsCsvImportResponse)
async def import_external_seeds_csv(
    req: Request,
    current_user: dict = Depends(get_current_employee),
    market: str = Query("US"),
    tool: str = Query("*"),
    mode: str = Query("upsert"),
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

    text = (await req.body()).decode("utf-8", errors="replace")
    if not text.strip():
        raise HTTPException(status_code=400, detail="EMPTY_CSV")

    default_market = _normalize_market(market)
    default_tool = _normalize_tool(tool)
    mode_norm = str(mode or "upsert").strip().lower()
    if mode_norm not in ("create", "upsert"):
        raise HTTPException(status_code=400, detail="INVALID_MODE")

    reader = csv.DictReader(io.StringIO(text))
    created = 0
    updated = 0
    errors: List[str] = []
    seed_ids: List[str] = []

    employee_id = current_user.get("employee_id") or current_user.get("employeeId")
    created_by = str(current_user.get("email") or employee_id or "")

    for idx, row in enumerate(reader, start=2):
        try:
            destination_url = str(row.get("destination_url") or row.get("url") or "").strip()
            if not destination_url:
                raise ValueError("MISSING_DESTINATION_URL")
            dest = _require_http_url(destination_url)

            row_market = _normalize_market(row.get("market") or default_market)
            row_tool = _normalize_tool(row.get("tool") or default_tool)

            title = str(row.get("title") or "").strip() or None
            image_url = str(row.get("image_url") or row.get("imageUrl") or "").strip() or None
            availability = str(row.get("availability") or "").strip() or None
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
                "image_url": None,
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
    evidence = getattr(snapshot, "evidence", None)
    if isinstance(evidence, dict):
        raw_variants = evidence.get("variants")
        if isinstance(raw_variants, list):
            variants = raw_variants

    return {
        "status": "success",
        "preview": {
            "url": dest,
            "canonical_url": canonical_url,
            "domain": domain,
            "external_product_id": _stable_external_product_id(canonical_url),
            "title": getattr(snapshot, "title", None),
            "image_url": getattr(snapshot, "image_url", None),
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
        seed_data["external_product_id"] = _stable_external_product_id(canonical_url or dest)
        if body.merchant_display_name is not None:
            seed_data["merchant_display_name"] = (body.merchant_display_name or "").strip() or None
        if body.product_id is not None:
            seed_data["product_id"] = (body.product_id or "").strip() or None
        if body.brand is not None:
            seed_data["brand"] = (body.brand or "").strip() or None
        if body.category is not None:
            seed_data["category"] = (body.category or "").strip() or None
        seed_data["title"] = title
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
        if body.variants is not None and body.variants:
            seed_data["variants"] = body.variants
        elif not seed_data.get("variants"):
            evidence = getattr(snapshot, "evidence", None)
            if isinstance(evidence, dict):
                snap_variants = evidence.get("variants")
                if isinstance(snap_variants, list) and snap_variants:
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
                "external_product_id": seed_data.get("external_product_id") or _stable_external_product_id(match_url),
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
        seed_data: Dict[str, Any] = {
            "external_product_id": _stable_external_product_id(match_url),
            "merchant_display_name": (body.merchant_display_name or "").strip() or None,
            "product_id": (body.product_id or "").strip() or None,
            "brand": (body.brand or "").strip() or None,
            "category": (body.category or "").strip() or None,
            "title": title,
            "image_url": image_url,
            "availability": availability,
            "variants": body.variants or [],
            "utm_template": utm_template,
            "partner_type": partner_type,
            "disclosure_text": disclosure_text,
            "source": "employee_seed",
            "snapshot": {
                "canonical_url": canonical_url,
                "domain": domain,
                "title": snap_title,
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
                "external_product_id": seed_data.get("external_product_id") or _stable_external_product_id(match_url),
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

    seed_data = _ensure_json_obj(row.get("seed_data"))
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
            "refreshed_at": _to_iso(getattr(snapshot, "fetched_at", None)) or None,
        }
    )
    # Only overwrite curated fields if they are missing.
    if not seed_data.get("title"):
        seed_data["title"] = snap_title
    if not seed_data.get("image_url"):
        seed_data["image_url"] = snap_image_url
    if not seed_data.get("availability"):
        seed_data["availability"] = snap_availability
    if not seed_data.get("variants"):
        evidence = getattr(snapshot, "evidence", None)
        if isinstance(evidence, dict):
            snap_variants = evidence.get("variants")
            if isinstance(snap_variants, list) and snap_variants:
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

    metrics = {
        "sales_7d": sales_7d,
        "sales_30d": sales_30d,
        "gmv_7d": {"currency": currency, "amount": gmv_7d},
        "gmv_30d": {"currency": currency, "amount": gmv_30d},
        "merchants_selling": 1,
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
    offers = [_build_merchant_offer(
        product_key=product_key,
        merchant_id=merchant_id,
        platform=platform,
        platform_product_id=platform_product_id,
        product_data=product_data,
    )]
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

    return {"status": "success", "items": offers, "primary": primary_offer}


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
    row = await database.fetch_one(
        products_cache.select().where(
            (products_cache.c.merchant_id == merchant_id)
            & (products_cache.c.platform == platform)
            & (products_cache.c.platform_product_id == platform_product_id)
        )
    )
    if not row:
        raise HTTPException(status_code=404, detail="PRODUCT_NOT_FOUND")

    product_data = _ensure_dict(dict(row).get("product_data"))
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

    metrics = await _compute_product_metrics(
        merchant_id=merchant_id,
        platform=platform,
        platform_product_id=platform_product_id,
        product_data=product_data,
    )

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
