"""
Pivota canonical PDP resolver — public read-only routes that turn a
sig_* signature into the product data needed to render the PDP page
at agent.pivota.cc/products/{sig_id}, and that enumerate sigs for
sitemap generation.

These routes back the dynamic Pivota canonical PDP surface (Phase C-2
of the canonical-PDP build). Phase C-1 (PR #327) added the schema +
sig generator + audit fallback so every onboarded merchant product
gets a sig_*. This PR makes those URLs actually serve content +
appear in the sitemap so Google can index them.

Surface:
  - GET /api/canonical/products/{sig_id}
        Returns { product: {title, brand, description, image_url,
                            canonical_url, vendor, product_type, ...} }
        404 if sig_id doesn't exist.
        Public — no auth (it's a discovery surface).
  - GET /api/canonical/products?limit=N&offset=M
        Returns { items: [{sig_id, canonical_url, last_modified}, ...],
                  total, limit, offset }
        For sitemap generation (pivota-agent-ui sitemap-products.xml).
        Bounded list (max 1000 per page) to keep response size sane.
        Public — no auth.

Why not gate on auth? These endpoints serve data we WANT public
indexing for — anyone who can see the agent.pivota.cc/products/ URL
can already see the PDP. Gating the resolver would just block our
own gateway/sitemap from working.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime
from typing import Any, Awaitable, Dict, TypeVar

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import Boolean, DateTime, Float, String, Text, and_, column, func, or_, select, table

from db.catalog import catalog_merchants, catalog_products
from db.database import database
from services.claim_safety import substantiated_claims
from utils.logger import logger

router = APIRouter(
    prefix="/api/canonical",
    tags=["canonical-pdp"],
)

# Public PDP URL base. Used to synthesize a canonical_url for offer-free
# brand-authored rows that have no minted sig (their pivota_canonical_url may be
# null) — the served PDP resolves by content_key, so the sitemap points there.
_PDP_URL_PREFIX = "https://agent.pivota.cc/products/"

T = TypeVar("T")


def _env_float(name: str, default: float, *, min_value: float, max_value: float) -> float:
    raw = (os.getenv(name) or "").strip()
    try:
        value = float(raw) if raw else default
    except Exception:
        value = default
    return max(min_value, min(max_value, value))


CANONICAL_PRODUCTS_DB_TIMEOUT_SECONDS = _env_float(
    "CANONICAL_PRODUCTS_DB_TIMEOUT_SECONDS",
    4.0,
    min_value=0.2,
    max_value=15.0,
)


async def _bounded_db(awaitable: Awaitable[T], operation: str) -> T:
    try:
        return await asyncio.wait_for(
            awaitable,
            timeout=CANONICAL_PRODUCTS_DB_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        logger.warning(
            "canonical_products route timed out",
            extra={
                "operation": operation,
                "timeout_seconds": CANONICAL_PRODUCTS_DB_TIMEOUT_SECONDS,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail={
                "message": "Canonical products lookup timed out",
                "operation": operation,
                "timeout_seconds": CANONICAL_PRODUCTS_DB_TIMEOUT_SECONDS,
            },
        ) from exc


index_pipeline_state = table(
    "index_pipeline_state",
    column("content_key", String),
    column("serving_eligible", Boolean),
    # ADR-007 SLICE 1: offer-free citation floor (migration 165). Referenced by
    # the flag-gated eligibility filters below.
    column("index_eligible", Boolean),
    column("blocker_code", Text),
    column("blocker_detail", Text),
    column("content_quality_score", Float),
    column("quality_scored_at", DateTime),
)

# Lightweight handle to the evidence columns (migration 152). Mirrors the local
# index_pipeline_state pattern above rather than importing the full db.catalog
# Table (whose Core def predates the evidence columns).
agent_pdp_view = table(
    "agent_pdp_view",
    column("content_key", String),
    column("evidence_profile"),
    column("required_disclaimers"),
)


def _flag_on(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _eligibility_predicate(*, widen_with_index_eligible: bool):
    """SQLAlchemy boolean for the index-pipeline serving gate.

    Default: serving_eligible = TRUE (byte-identical to the pre-ADR-007 gate).
    When ``widen_with_index_eligible`` is True (the relevant flag is ON), the
    gate widens to (serving_eligible OR index_eligible) — the OFFER-FREE
    citation floor from ADR-007 SLICE 1.

    The by-signature PDP READ is widened by INDEX_ELIGIBLE_READ; the public
    /products SITEMAP listing is a separate content/SEO decision gated only by
    INDEX_ELIGIBLE_SITEMAP. The two callers pass their own flag so neither is
    widened by the other."""
    if widen_with_index_eligible:
        return or_(
            index_pipeline_state.c.serving_eligible.is_(True),
            index_pipeline_state.c.index_eligible.is_(True),
        )
    return index_pipeline_state.c.serving_eligible.is_(True)


def _shape_product_for_pdp(row: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a catalog_products row into the flat product object the
    pivota-agent-ui PDP page expects (see
    pivota-agent-ui/src/app/products/[id]/productJsonLd.ts +
    page.tsx:readCanonicalPdpProduct for the consumer shape).

    Falls back to product_payload fields where the top-level catalog
    columns are sparse — that's why the catalog stores the full raw
    payload alongside the normalized columns."""
    payload = row.get("product_payload") or {}
    if not isinstance(payload, dict):
        payload = {}

    # Title: catalog.title is required at sync time, so this always populates.
    title = (row.get("title") or "").strip() or payload.get("title") or ""

    # Brand: catalog.brand may be null for older syncs; payload often has it.
    brand_str = (
        (row.get("brand") or "").strip()
        or (payload.get("brand") or "")
        or (payload.get("vendor") or "")
        or ""
    )

    description = (
        (row.get("description") or "").strip()
        or (payload.get("description") or "")
        or (payload.get("description_text") or "")
        or ""
    )

    image = (row.get("image_url") or "").strip() or payload.get("image_url") or ""

    return {
        "id": row.get("pivota_signature_id"),
        "product_id": row.get("pivota_signature_id"),
        "title": title,
        "name": title,
        "brand": brand_str or None,
        "vendor": brand_str or None,
        "product_type": row.get("product_type"),
        "description": description or None,
        "image_url": image or None,
        "main_image_url": image or None,
        "canonical_url": row.get("pivota_canonical_url"),
        # Echo the merchant's own URL too (when set) so consumers can
        # link out to the storefront from the canonical PDP.
        "merchant_canonical_url": row.get("canonical_url"),
        "platform": row.get("platform"),
        "source_product_id": row.get("source_product_id"),
        # Substantiated, attributable claims for the agent-crawled PDP + JSON-LD.
        # Serve gate: only `substantiated` claims are emitted (never raw/unverified).
        "evidence_claims": substantiated_claims(row.get("evidence_profile")),
        "disclaimers": row.get("required_disclaimers") or [],
        # Carry the full upstream payload for consumers that need
        # variants / price / inventory beyond what we normalized.
        "payload": payload or None,
    }


@router.get("/products/{sig_id}")
async def get_canonical_pdp_by_signature(sig_id: str) -> Dict[str, Any]:
    """Resolve a sig_* to product fields. Backs the SSR + client-side
    data fetch for agent.pivota.cc/products/{sig_id}."""
    sig = (sig_id or "").strip()
    if not sig.startswith("sig_") or len(sig) < 5:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="sig_id must look like sig_<hex>",
        )
    query = (
        select(
            catalog_products.c.product_key,
            catalog_products.c.merchant_id,
            catalog_products.c.platform,
            catalog_products.c.source_product_id,
            catalog_products.c.title,
            catalog_products.c.description,
            catalog_products.c.brand,
            catalog_products.c.product_type,
            catalog_products.c.canonical_url,
            catalog_products.c.image_url,
            catalog_products.c.product_payload,
            catalog_products.c.pivota_signature_id,
            catalog_products.c.pivota_canonical_url,
            catalog_products.c.updated_at,
            # Evidence layer (migration 152) — JOINed below so the public PDP can
            # surface substantiated, attributable claims for agents to cite.
            agent_pdp_view.c.evidence_profile,
            agent_pdp_view.c.required_disclaimers,
        )
        .select_from(
            catalog_products.join(
                index_pipeline_state,
                catalog_products.c.content_key == index_pipeline_state.c.content_key,
            ).join(
                catalog_merchants,
                catalog_products.c.merchant_id == catalog_merchants.c.merchant_id,
            ).outerjoin(
                agent_pdp_view,
                catalog_products.c.content_key == agent_pdp_view.c.content_key,
            )
        )
        .where(
            and_(
                catalog_products.c.pivota_signature_id == sig,
                catalog_products.c.content_key.isnot(None),
                # ADR-007 SLICE 1: by-signature PDP READ widens under
                # INDEX_ELIGIBLE_READ (the citation read surface), NOT under
                # the separate sitemap flag.
                _eligibility_predicate(
                    widen_with_index_eligible=_flag_on("INDEX_ELIGIBLE_READ")
                ),
                catalog_merchants.c.indexable.is_(True),
                catalog_merchants.c.status == "active",
            )
        )
        .limit(1)
    )
    row = await _bounded_db(database.fetch_one(query), "product_by_signature")
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "message": "No canonical PDP for this signature",
                "sig_id": sig,
            },
        )
    row_dict = dict(row)
    return {
        "product": _shape_product_for_pdp(row_dict),
        "updated_at": (
            row_dict["updated_at"].isoformat()
            if isinstance(row_dict.get("updated_at"), datetime)
            else None
        ),
    }


@router.get("/products")
async def list_canonical_pdp_signatures(
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> Dict[str, Any]:
    """Paginated list of public-serving canonical PDP signatures.

    This route backs the pivota-agent-ui product sitemap. It must use the
    same fail-closed serving gate as the PDP read path: a sig is public only
    when its content_key is present in index_pipeline_state with
    serving_eligible=TRUE.
    """
    # ADR-007 SLICE 1: the public /products SITEMAP listing is a content/SEO
    # decision distinct from the citation read surface. It is widened ONLY by
    # INDEX_ELIGIBLE_SITEMAP — never by INDEX_ELIGIBLE_READ. Both default OFF.
    widen_sitemap = _flag_on("INDEX_ELIGIBLE_SITEMAP")
    # content_key is the always-required identity (it keys the served PDP). A
    # canonical sig qualifies a row as before; when widened to the offer-free
    # citation index, store-less brand-authored rows (null pivota_signature_id,
    # index_eligible) ALSO qualify — keyed on content_key, URL falling back to
    # /products/{content_key}. We deliberately do NOT drop the sig requirement
    # wholesale: a serving row without a sig still doesn't qualify; only the
    # index_eligible citation rows are added.
    sig_present = and_(
        catalog_products.c.pivota_signature_id.isnot(None),
        catalog_products.c.pivota_signature_id.like("sig_%"),
    )
    identity_term = (
        or_(sig_present, index_pipeline_state.c.index_eligible.is_(True))
        if widen_sitemap
        else sig_present
    )
    eligibility_filter = and_(
        catalog_products.c.content_key.isnot(None),
        identity_term,
        _eligibility_predicate(widen_with_index_eligible=widen_sitemap),
        catalog_merchants.c.indexable.is_(True),
        catalog_merchants.c.status == "active",
    )
    serving_join = catalog_products.join(
        index_pipeline_state,
        catalog_products.c.content_key == index_pipeline_state.c.content_key,
    ).join(
        catalog_merchants,
        catalog_products.c.merchant_id == catalog_merchants.c.merchant_id,
    )

    total_q = (
        select(func.count())
        .select_from(serving_join)
        .where(eligibility_filter)
    )
    total = int(
        await _bounded_db(database.fetch_val(total_q), "product_signature_count") or 0
    )

    # index_eligible is selected ONLY when the sitemap is widened — keeps the
    # strict (flag-OFF) query byte-identical to the pre-Tier-2 behavior.
    select_cols = [
        catalog_products.c.product_key,
        catalog_products.c.pivota_signature_id,
        catalog_products.c.content_key,
        catalog_products.c.pivota_canonical_url,
        catalog_products.c.content_changed_at,
        index_pipeline_state.c.serving_eligible,
        index_pipeline_state.c.blocker_code,
        index_pipeline_state.c.blocker_detail,
        index_pipeline_state.c.content_quality_score,
        index_pipeline_state.c.quality_scored_at,
    ]
    if widen_sitemap:
        select_cols.append(index_pipeline_state.c.index_eligible)
    rows_q = (
        select(*select_cols)
        .select_from(serving_join)
        .where(eligibility_filter)
        .order_by(
            catalog_products.c.content_changed_at.desc(),
            catalog_products.c.pivota_signature_id.asc(),
            catalog_products.c.content_key.asc(),
            catalog_products.c.product_key.asc(),
        )
        .limit(limit)
        .offset(offset)
    )
    rows = await _bounded_db(database.fetch_all(rows_q), "product_signature_list")
    has_more = offset + len(rows) < total
    items = [
        {
            "sig_id": r["pivota_signature_id"],
            "content_key": r["content_key"],
            "canonical_url": (
                r["pivota_canonical_url"]
                or (f"{_PDP_URL_PREFIX}{r['content_key']}" if r["content_key"] else None)
            ),
            "serving_eligible": bool(r["serving_eligible"]),
            "index_eligible": (bool(r["index_eligible"]) if widen_sitemap else False),
            "blocker_code": r["blocker_code"],
            "blocker_detail": r["blocker_detail"],
            "content_quality_score": r["content_quality_score"],
            "quality_scored_at": (
                r["quality_scored_at"].isoformat()
                if isinstance(r["quality_scored_at"], datetime)
                else None
            ),
            "last_modified": (
                r["content_changed_at"].isoformat()
                if isinstance(r["content_changed_at"], datetime)
                else None
            ),
        }
        for r in rows
    ]
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": has_more,
    }
