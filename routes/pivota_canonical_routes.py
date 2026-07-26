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
from sqlalchemy import String, and_, column, func, or_, select, table

from db.catalog import catalog_merchants, catalog_products
from db.database import database
from services.claim_safety import substantiated_claims
# The sitemap-eligibility predicate and its index_pipeline_state handle live in
# a shared module because the canonical ELECTION asks the same question of the
# same rows (services/content_canonical_election). A second hand-kept copy of
# this filter would let the elector crown a sig this feed never emits — see
# that module's header for why that failure is worse than the duplicate it
# fixes.
from services.canonical_sitemap_candidates import (
    electable_sig_exists,
    eligibility_predicate as _eligibility_predicate,
    flag_on as _flag_on,
    index_pipeline_state,
    sitemap_candidate_filter,
    sitemap_candidate_join,
    sitemap_widen_enabled,
)
from services.pdp_renderability import (
    EXTERNAL_SEED_MERCHANT_ID as _EXTERNAL_SEED_MERCHANT_ID,
    external_product_seeds,
    pdp_will_render_expression,
)
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


# Migration 181. ONE elected canonical sig per content_key — the shared answer
# that keeps the sitemap's advertised URL and the gateway's <link
# rel="canonical"> naming the same sig. Local Core handle, same pattern as the
# other lightweight tables in this module.
content_canonical_election = table(
    "content_canonical_election",
    column("content_key", String),
    column("canonical_sig_id", String),
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

# The merchant id the gateway treats as the external-seed lane. Re-exported
# from the shared predicate module so callers of this route module keep working.
EXTERNAL_SEED_MERCHANT_ID = _EXTERNAL_SEED_MERCHANT_ID


def _renderable_column():
    """EXISTS boolean: will agent.pivota.cc/products/{sig} actually render?

    Exposed on the /products list so sitemap generation can stop advertising
    rows whose PDP cannot be built. The predicate itself lives in
    :mod:`services.pdp_renderability` — shared verbatim with the
    ``public_not_renderable`` invariant, which is the only way the two stop
    drifting (they were 52% apart before #1575, and BOTH wrong after it).

    HISTORY — why this stopped requiring an identity listing. #1575 encoded
    "no approved + live_read_enabled ``pdp_identity_listing`` row ⇒ generic
    shell". 29 live PDP fetches on 2026-07-25 disproved it in both directions:
    rows with NO identity listing at all served full 200s with product JSON-LD
    (3/3), rows with ``live_read_enabled=false`` served full 200s (12/12), and
    rows WITH nothing wrong at the identity layer served hard 500s (12/12)
    because no seed answered their content route. ``get_pdp_v2``'s serving gate
    never reads ``pdp_identity_listing`` at all; ``live_read_enabled`` gates the
    identity promotion lane, not the renderer. Net effect of the correction on
    the live feed: 943 rows that render fine stop being withheld from the
    sitemap, and 2,297 non-renderable rows stay out. (1,376 of those 2,297 were
    the cohort that is ALSO trust-``public`` — the number
    ``public_not_renderable`` counts; the remainder are non-renderable rows the
    trust layer was already withholding for other reasons.)

    P3, 2026-07-25 — the second correction, in the same direction. The minted
    lane's seeds attach by ``attached_product_key``, and until PIVOTA-Agent's
    ``get_pdp_v2`` learned that key nothing could render them. It now does
    (12/12 sampled minted PDPs went 404 → 200 with real title/brand/image/price
    against prod data), so this column admits them: corpus-wide 2,051 rows flip
    to renderable and ZERO flip away, and ``public_not_renderable`` falls
    1,376 → 1. The sitemap grows on its next 6h cron; a shrink guard exists,
    growth is unguarded, which is the safe direction for this change.

    "Non-renderable" here means the URL does not answer with a real PDP: it is
    either a hard HTTP 500 or a generic noindex shell carrying no product
    JSON-LD (6/6 sampled non-renderable feed rows were the latter). Both are
    worthless to a crawler; only the 500 is loud.

    2026-07-26 — THE THIRD CORRECTION, and the first one in the RESTRICTING
    direction. The two above both widened the column, and both were about the
    same half of the question: can the gateway resolve a content ROUTE. They
    left the OTHER gate unmodelled. ``get_pdp_v2`` checks serving eligibility
    FIRST and 404s ``PRODUCT_NOT_SERVABLE`` before it ever looks for content, so
    a row can pass the route question and still never render.

    Measured against the live sitemap on 2026-07-26: 77 of 4,528 advertised URLs
    returned a hard HTTP 500 (77/77 on serial retry). All 77 had
    ``renderable=true`` from this column, ``serving_eligible=false``, and
    ``blocker_code='no_price'`` — in the sitemap only because
    ``INDEX_ELIGIBLE_SITEMAP`` widens the eligibility filter to the ADR-007
    SLICE 1 offer-free citation floor, which ``get_pdp_v2`` was never taught.
    This column now asks BOTH gates via
    :func:`pdp_will_render_expression`, so it stops advertising them.

    Direction of the change: the 100 widen-only rows in the feed (78 of them
    previously ``renderable=true``) flip to ``renderable=false``; nothing that
    is ``serving_eligible`` moves, because for those rows the added conjunct is
    true by construction.

    Operationally the sitemap goes 4,528 → 4,451 on its next 6h cron, and NO
    agent-ui guard blocks it: ``sitemapCountGuard`` refuses below 50% of the
    committed count (and an absolute floor of 1,000), and 1.7% is nowhere near
    it; ``sitemapCoverageVerdict`` measures ROWS CONSUMED against the feed's
    ``total``, which does not move at all — all 5,887 rows are still emitted,
    77 of them now flagged unrenderable. Expect one
    ``NOTE: 77 previously advertised URL(s) are not in this build`` line in the
    cron log. That note is the intended outcome, not a warning to chase.

    See :func:`pdp_serving_gate_passes` for why the fix is to stop advertising
    these rather than to start serving them, and for the order of work that
    would let them back in.
    """
    return pdp_will_render_expression(catalog_products).label("renderable")


def _elected_canonical_sig_note():
    """WHY the feed's `canonical_sig_id` is validated, and where.

    The validation itself is in the election JOIN's ON clause (see
    ``list_canonical_pdp_signatures``); this note is the reasoning, kept next to
    the other column helpers so it is findable.

    A stored election is a durable fact; electability is a live one. When the
    elected sig stops rendering (P3 moved 2,051 rows on that field in a day, and
    nothing stops the reverse), an unvalidated read produces the failure this
    whole feature exists to prevent:

      the sitemap correctly drops the dead sig and advertises its sibling, while
      the sibling's PDP emits <link rel="canonical"> pointing AT the dead sig

    We would then submit a URL that disavows itself in favour of a page that
    500s, and the content_key loses all index presence — worse than the
    duplicate, and worse than a moved URL. The sitemap is structurally immune
    (its renderable filter runs before the dedup, so a dead sig is never a
    candidate); every reader of this table has to earn the same immunity here.

    Degrading to NULL is exactly right: NULL means "no election", which every
    consumer already handles by falling back to self — so both surfaces fall
    back TOGETHER, which is the invariant.
    """


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
    #
    # The join and the filter are built by services/canonical_sitemap_candidates
    # rather than inline, because the canonical ELECTION has to pick its winner
    # from EXACTLY these rows. A second copy that drifted even slightly would
    # let it crown a sig this feed never emits, and then the sitemap advertises
    # one URL while that URL's own page canonicalises at another.
    widen_sitemap = sitemap_widen_enabled()
    serving_join = sitemap_candidate_join(widen=widen_sitemap)
    eligibility_filter = sitemap_candidate_filter(widen=widen_sitemap)

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
        # Already validated by the JOIN's ON clause below — see the note there.
        content_canonical_election.c.canonical_sig_id,
    ]
    if widen_sitemap:
        select_cols.append(index_pipeline_state.c.index_eligible)
    rows_q = (
        select(*select_cols)
        # LEFT JOIN, never INNER: a content_key that has not been elected yet
        # (freshly minted, or the sweep has not run) must still appear in the
        # feed. It gets a null canonical_sig_id and the consumer falls back to
        # its own ordering — the same answer it computed before this existed.
        .select_from(
            serving_join.outerjoin(
                content_canonical_election,
                and_(
                    catalog_products.c.content_key
                    == content_canonical_election.c.content_key,
                    # The validation lives in the ON clause, not the SELECT
                    # list. Semantically identical — the EXISTS depends only on
                    # cce.canonical_sig_id — but it is evaluated per MATCHED
                    # ELECTION row instead of per product row, so the correlated
                    # 3-table join plus pdp_renderable_expression's nested
                    # EXISTS over external_product_seeds runs only where an
                    # election actually exists. The feed pages up to 1000 rows
                    # and already pays that predicate once per row for its own
                    # `renderable` column; paying it twice per row would have
                    # doubled the most expensive part of the query.
                    #
                    # A row whose election fails validation simply does not
                    # match, so the LEFT JOIN yields NULL — which is exactly the
                    # "no election" the consumer already falls back on.
                    electable_sig_exists(
                        content_canonical_election.c.canonical_sig_id,
                        widen=widen_sitemap,
                    ),
                ),
            )
        )
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
            # SELF-referential, deliberately unchanged. It is this ROW's own
            # URL and several consumers read it that way; the group-level
            # answer is the separate field below.
            "canonical_url": (
                r["pivota_canonical_url"]
                or (f"{_PDP_URL_PREFIX}{r['content_key']}" if r["content_key"] else None)
            ),
            # The ONE URL id for this row's content_key (migration 181), or
            # null when the content_key has not been elected yet.
            #
            # 474 content_keys carry more than one eligible+renderable sig (551
            # redundant URLs; groups of 2, 3, 4, 5 and 7), every sibling serving
            # identical content under a self-referential canonical tag. This
            # field is the shared answer that resolves them: the sitemap
            # advertises exactly this id, and get_pdp_v2's `canonical` module
            # hands the same id to every sibling PDP to emit as its <link
            # rel="canonical">. The two MUST agree — advertising URL A while A's
            # page canonicalises at B tells the crawler to drop the URL we just
            # submitted — which is why it is one stored value read twice rather
            # than a rule computed twice.
            #
            # Null is a safe degradation, not an error: the consumer falls back
            # to the ordering it used before this field existed.
            "canonical_sig_id": r["canonical_sig_id"],
            "serving_eligible": bool(r["serving_eligible"]),
            # Renderability of the public PDP: can the gateway resolve a
            # CONTENT ROUTE for this row (an acceptable external_product_seeds
            # row on external_product_id = source_product_id, or a
            # merchant-synced upstream)? It is explicitly NOT an identity
            # question — see _renderable_column's HISTORY note; the
            # "approved + live_read_enabled identity listing" this comment used
            # to describe is the exact belief #1584 disproved.
            # serving_eligible says "we want this public"; renderable says "the
            # PDP will actually render" — sitemap generation must require both.
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
