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
    GET /api/canonical/products?limit=N&cursor=<next_cursor>
        Returns { items: [{sig_id, canonical_url, last_modified}, ...],
                  total, limit, offset, has_more, next_cursor }
        For sitemap generation (pivota-agent-ui sitemap-products.xml).
        Bounded list (max 1000 per page) to keep response size sane.
        `total` is computed only on the first page (offset=0, no cursor)
        and is null otherwise — the eligibility-filtered COUNT(*) is the
        most expensive part of the query and consumers only need it once.
        Prefer cursor (keyset) pagination: OFFSET cost grows linearly
        with page depth and deep pages can hit the DB timeout.
        Public — no auth.

Why not gate on auth? These endpoints serve data we WANT public
indexing for — anyone who can see the agent.pivota.cc/products/ URL
can already see the PDP. Gating the resolver would just block our
own gateway/sitemap from working.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from datetime import datetime
from typing import Any, Awaitable, Dict, Optional, TypeVar

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

# PIVOTA-Agent-owned identity layer. The public PDP at
# agent.pivota.cc/products/{sig} only renders when the sig's OWN source row has
# an approved, live-read-enabled identity listing (row grain, not content_key
# grain: a sibling row's listing does not make this sig render — verified
# empirically 2026-07-23). The join key is (merchant_id, product_id) =
# (cp.merchant_id, cp.source_product_id), the same pairing
# catalog_row_trust_upserter uses.
pdp_identity_listing = table(
    "pdp_identity_listing",
    column("merchant_id", String),
    column("product_id", String),
    column("live_read_enabled", Boolean),
    column("identity_status", String),
)

# Bootstrap-content lane. get_pdp_v2 runs an external-seed status PRECHECK
# before any identity resolution and hard-404s
# (PRODUCT_NOT_FOUND / reason=external_seed_not_active) whenever the seed row
# exists with a status other than 'active'. The predicate mirrors
# PIVOTA-Agent server.js exactly: a MISSING seed row is fine (merchant-owned
# rows have none), an empty/NULL status is fine (the gateway falls through on
# a falsy status), any other non-'active' value is fatal.
external_product_seeds = table(
    "external_product_seeds",
    column("external_product_id", String),
    column("status", String),
)


def _seed_blocks_render():
    """EXISTS boolean: does an inactive external seed hard-404 this row's PDP?"""
    return (
        select(external_product_seeds.c.external_product_id)
        .where(
            and_(
                external_product_seeds.c.external_product_id
                == catalog_products.c.source_product_id,
                func.coalesce(
                    func.lower(func.trim(external_product_seeds.c.status)), ""
                )
                .notin_(["", "active"]),
            )
        )
        .exists()
    )


def _renderable_column():
    """EXISTS boolean: will agent.pivota.cc/products/{sig} actually render?

    Exposed on the /products list so sitemap generation can stop advertising
    serving_eligible rows whose PDP still serves the generic shell (the two
    gates — serving_eligible here, live_read_enabled in PIVOTA-Agent — are
    owned by different repos and had drifted 52% apart).

    TWO independent ways a PDP fails to render, and the sitemap must exclude
    both:

    1. No approved + live-read-enabled identity listing → generic shell.
    2. An external_product_seeds row whose status is not 'active' → the
       gateway's seed precheck hard-404s before identity is even consulted.

    (2) was the miss that made ~127 of 1,901 sitemap URLs serve HTTP 500 after
    pivota-agent-ui#269 flipped canonical PDPs to static/ISR: those rows pass
    every gate here (serving_eligible, priced offers, agent_pdp_view,
    approved+live_read identity) yet can never render, so the feed kept
    advertising them. Note this cohort is DISJOINT from the
    `public_not_renderable` invariant, which counts rows failing (1)."""
    return and_(
        select(pdp_identity_listing.c.product_id)
        .where(
            and_(
                pdp_identity_listing.c.merchant_id == catalog_products.c.merchant_id,
                pdp_identity_listing.c.product_id
                == catalog_products.c.source_product_id,
                pdp_identity_listing.c.live_read_enabled.is_(True),
                pdp_identity_listing.c.identity_status == "approved",
            )
        )
        .exists(),
        ~_seed_blocks_render(),
    ).label("renderable")


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


_LIST_CURSOR_VERSION = 1


def _encode_list_cursor(row: Dict[str, Any]) -> Optional[str]:
    """Opaque keyset cursor: the full ORDER BY key of the last row on a
    page, so the next page seeks past it instead of OFFSET-scanning.

    Returns None when the boundary row can't anchor a seek — e.g. a
    widened-sitemap citation row with a NULL pivota_signature_id (ADR-007).
    Consumers then fall back to offset paging (next_cursor is null while
    has_more stays accurate)."""
    ts = row.get("content_changed_at")
    if not isinstance(ts, datetime):
        return None
    if not all(
        isinstance(row.get(k), str)
        for k in ("pivota_signature_id", "content_key", "product_key")
    ):
        return None
    payload = json.dumps(
        {
            "v": _LIST_CURSOR_VERSION,
            "ts": ts.isoformat(),
            "sig": row["pivota_signature_id"],
            "ck": row["content_key"],
            "pk": row["product_key"],
        },
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_list_cursor(cursor: str) -> Dict[str, Any]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        if payload.get("v") != _LIST_CURSOR_VERSION:
            raise ValueError("unsupported cursor version")
        decoded = {
            "ts": datetime.fromisoformat(payload["ts"]),
            "sig": payload["sig"],
            "ck": payload["ck"],
            "pk": payload["pk"],
        }
        if not all(isinstance(decoded[k], str) for k in ("sig", "ck", "pk")):
            raise ValueError("cursor key fields must be strings")
        return decoded
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="cursor must be a next_cursor value from a previous response",
        ) from exc


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
                # Suppressed rows are withdrawn from serving (see the list
                # endpoint's matching filter); fail closed with a 404.
                catalog_products.c.suppressed_at.is_(None),
                # ADR-007 SLICE 1: by-signature PDP READ widens under
                # INDEX_ELIGIBLE_READ (the citation read surface), NOT under
                # the separate sitemap flag.
                _eligibility_predicate(
                    widen_with_index_eligible=_flag_on("INDEX_ELIGIBLE_READ")
                ),
                catalog_merchants.c.indexable.is_(True),
                # ADR-009 amendment (A9-2 review): merchant status is an IDENTITY-
                # LIFECYCLE field (observed → claimed/active), not a serving switch.
                # Observed sellers' pages served under the shared bucket yesterday and
                # keep serving; product-level gates (serving_eligible/index_eligible)
                # remain the SOLE serving control. Gate semantics = "not disabled".
                catalog_merchants.c.status.in_(["active", "observed"]),
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
    cursor: Optional[str] = Query(
        None,
        description=(
            "Opaque keyset cursor from a previous response's next_cursor. "
            "Mutually exclusive with offset; preferred for deep pagination."
        ),
    ),
) -> Dict[str, Any]:
    """Paginated list of public-serving canonical PDP signatures.

    This route backs the pivota-agent-ui product sitemap. It must use the
    same fail-closed serving gate as the PDP read path: a sig is public only
    when its content_key is present in index_pipeline_state with
    serving_eligible=TRUE.

    Two pagination modes:
      - offset (legacy): kept for existing consumers; deep offsets scan
        linearly and can hit the DB timeout.
      - cursor (keyset): seeks on the ORDER BY key, constant cost per page.
    `total` is returned only on the first page (offset=0, no cursor) so the
    expensive eligibility-filtered COUNT(*) runs once per crawl, not per page.
    """
    if cursor is not None and offset:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Pass either cursor or offset, not both",
        )
    cursor_key = _decode_list_cursor(cursor) if cursor is not None else None

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
    # Merchant gate. Strict (flag-OFF): retail merchants must be indexable +
    # active (INNER JOIN) — unchanged. Widened: store-less brand-authored rows
    # have NO retail catalog_merchants row (onboarded via brand verification, not
    # catalog_sync), so the INNER JOIN would drop them. LEFT JOIN and allow a
    # missing merchant row through ONLY when index_eligible (the brand passed
    # domain verification → inherently publishable). A retail merchant that IS
    # present but hidden/inactive is still excluded.
    base_join = catalog_products.join(
        index_pipeline_state,
        catalog_products.c.content_key == index_pipeline_state.c.content_key,
    )
    if widen_sitemap:
        serving_join = base_join.outerjoin(
            catalog_merchants,
            catalog_products.c.merchant_id == catalog_merchants.c.merchant_id,
        )
        merchant_gate = or_(
            and_(
                catalog_merchants.c.indexable.is_(True),
                catalog_merchants.c.status.in_(["active", "observed"]),
            ),
            and_(
                catalog_merchants.c.merchant_id.is_(None),
                index_pipeline_state.c.index_eligible.is_(True),
            ),
        )
    else:
        serving_join = base_join.join(
            catalog_merchants,
            catalog_products.c.merchant_id == catalog_merchants.c.merchant_id,
        )
        merchant_gate = and_(
            catalog_merchants.c.indexable.is_(True),
            catalog_merchants.c.status.in_(["active", "observed"]),
        )
    eligibility_filter = and_(
        catalog_products.c.content_key.isnot(None),
        # A suppressed row (catalog_products.suppressed_at set, e.g. the
        # demo_retired_2026_07 sweep) is withdrawn from serving and must not
        # be advertised, even while its content_key stays eligible through
        # live sibling rows. Row-level, matching this endpoint's row grain.
        catalog_products.c.suppressed_at.is_(None),
        identity_term,
        _eligibility_predicate(widen_with_index_eligible=widen_sitemap),
        merchant_gate,
    )

    # The eligibility-filtered COUNT(*) scans the whole join, so it runs on
    # the first page only. Later pages return total=null; has_more (from the
    # limit+1 fetch below) is the paging signal.
    total: Optional[int] = None
    if cursor_key is None and offset == 0:
        total_q = (
            select(func.count())
            .select_from(serving_join)
            .where(eligibility_filter)
        )
        total = int(
            await _bounded_db(database.fetch_val(total_q), "product_signature_count") or 0
        )

    where_clause = eligibility_filter
    if cursor_key is not None:
        # The ORDER BY mixes directions (content_changed_at DESC, rest ASC),
        # so the seek can't be a single row-tuple comparison.
        seek_filter = or_(
            catalog_products.c.content_changed_at < cursor_key["ts"],
            and_(
                catalog_products.c.content_changed_at == cursor_key["ts"],
                catalog_products.c.pivota_signature_id > cursor_key["sig"],
            ),
            and_(
                catalog_products.c.content_changed_at == cursor_key["ts"],
                catalog_products.c.pivota_signature_id == cursor_key["sig"],
                catalog_products.c.content_key > cursor_key["ck"],
            ),
            and_(
                catalog_products.c.content_changed_at == cursor_key["ts"],
                catalog_products.c.pivota_signature_id == cursor_key["sig"],
                catalog_products.c.content_key == cursor_key["ck"],
                catalog_products.c.product_key > cursor_key["pk"],
            ),
            # Widened sitemap (ADR-007): NULL-sig citation rows sort after
            # every non-null sig within the same timestamp (ASC NULLS LAST),
            # so they are strictly past any cursor (cursors are only minted
            # from non-null-sig rows).
            and_(
                catalog_products.c.content_changed_at == cursor_key["ts"],
                catalog_products.c.pivota_signature_id.is_(None),
            ),
        )
        where_clause = and_(eligibility_filter, seek_filter)

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
        _renderable_column(),
    ]
    if widen_sitemap:
        select_cols.append(index_pipeline_state.c.index_eligible)
    rows_q = (
        select(*select_cols)
        .select_from(serving_join)
        .where(where_clause)
        .order_by(
            catalog_products.c.content_changed_at.desc(),
            catalog_products.c.pivota_signature_id.asc(),
            catalog_products.c.content_key.asc(),
            catalog_products.c.product_key.asc(),
        )
        # limit+1 answers has_more without a second COUNT query.
        .limit(limit + 1)
    )
    if cursor_key is None and offset:
        rows_q = rows_q.offset(offset)
    rows = await _bounded_db(database.fetch_all(rows_q), "product_signature_list")
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = _encode_list_cursor(dict(rows[-1])) if has_more and rows else None
    items = [
        {
            "sig_id": r["pivota_signature_id"],
            "content_key": r["content_key"],
            "canonical_url": (
                r["pivota_canonical_url"]
                or (f"{_PDP_URL_PREFIX}{r['content_key']}" if r["content_key"] else None)
            ),
            "serving_eligible": bool(r["serving_eligible"]),
            # Renderability of the public PDP (approved + live_read_enabled
            # identity listing for THIS row). serving_eligible says "we want
            # this public"; renderable says "the PDP will actually render" —
            # sitemap generation must require both.
            "renderable": bool(r["renderable"]),
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
        "next_cursor": next_cursor,
    }
