"""External citation read API (ADR-007 P0).

`GET /agent/v1/citation/{id}` — the offer-free **CitationItem** projection a
frontier agent reads to *cite* Pivota, distinct from the commerce-bearing
`get_pdp` render. It reuses the agent_pdp_v1 index_eligible read gate + id
resolver + `substantiated_claims`, and NEVER emits offers / price /
merchant-private fields. Public-read, rate-limited per `X-Pivota-Agent` (else
client IP), cacheable.

B④-P1 (attribution telemetry): every inbound read is logged best-effort to
`citation_read_log` off the response hot path (`X-Pivota-Agent` else IP + what
was asked for + outcome) — the external half of "who cites us". Flag-gated
`CITATION_READ_TELEMETRY`, default OFF.

Contract: pivota-merchants-portal/docs/external-citation-api-contract.md
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

from db.citation_read_log import (
    STATUS_DISABLED,
    STATUS_EMPTY,
    STATUS_HIT,
    STATUS_MISS,
    STATUS_SUPPRESSED,
    log_citation_read,
)
from db.database import database
from middleware.rate_limiter import AdvancedRateLimiter
from routes.agent_pdp_v1 import (
    EXT_RESOLVE_SQL,
    _index_eligible_read_enabled,
    _is_external_product_id,
    _query_for_id,
    _row_to_dict,
)
from services.catalog_sync_service import pivota_canonical_pdp_url
from services.claim_safety import substantiated_claims

router = APIRouter(prefix="/agent/v1/citation", tags=["agent-citation"])

CITE_AS = "Pivota — agent.pivota.cc"
SUMMARY_MAX = 200

# Per-source token bucket (in-memory; reuse of the existing limiter). Keyed on
# X-Pivota-Agent when supplied, else client IP. Named-partner tiers (P3) layer
# on later; P0 serves everyone the standard tier.
_limiter = AdvancedRateLimiter()


def _caller_identity(request: Request) -> Tuple[Optional[str], Optional[str]]:
    """The caller's self-declared `X-Pivota-Agent` id (free-form UA-style, e.g.
    `openai-chatgpt/1.0`) and client IP. Single source of truth for both the
    rate-limit key and the B④-P1 attribution telemetry."""
    agent = (request.headers.get("X-Pivota-Agent") or "").strip() or None
    client_ip = request.client.host if request.client else None
    return agent, client_ip


def _telemetry_enabled() -> bool:
    """B④-P1 attribution telemetry flag. Default OFF ⇒ no logging task is ever
    spawned, so the off-path is byte-identical to today. Canary on per repo
    rollout convention."""
    return (os.getenv("CITATION_READ_TELEMETRY") or "").strip().lower() in {
        "1", "true", "yes", "on",
    }


# Strong refs to in-flight fire-and-forget log tasks so the event loop doesn't
# GC them mid-write (asyncio only holds weak refs to bare tasks).
_log_tasks: "set[asyncio.Task[Any]]" = set()


def _spawn_log(**fields: Any) -> None:
    """Fire-and-forget the best-effort telemetry write OFF the response hot
    path. Never raises into the handler; a flag-OFF or loop-less context is a
    silent no-op."""
    if not _telemetry_enabled():
        return
    try:
        task = asyncio.ensure_future(log_citation_read(**fields))
    except RuntimeError:
        # No running loop (e.g. a sync test path) — drop quietly.
        return
    _log_tasks.add(task)
    task.add_done_callback(_log_tasks.discard)


async def _citation_rate_limit(request: Request) -> None:
    agent, client_ip = _caller_identity(request)
    key = agent or client_ip or "anonymous"
    allowed, meta = await _limiter.check_limit(key, tier="standard")
    if not allowed:
        retry = max(1, int(meta.get("reset", 0) - time.time()))
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="citation read rate limit exceeded",
            headers={
                "Retry-After": str(retry),
                "X-RateLimit-Limit": str(meta.get("limit", "")),
                "X-RateLimit-Remaining": str(meta.get("remaining", 0)),
            },
        )


def _first_sentence(text: str, *, cap: int = SUMMARY_MAX) -> str:
    """A one-line summary an agent can quote verbatim — first sentence, capped."""
    collapsed = " ".join(str(text or "").split())
    if not collapsed:
        return ""
    for sep in (". ", "! ", "? "):
        idx = collapsed.find(sep)
        if 0 < idx <= cap:
            return collapsed[: idx + 1].strip()
    return collapsed[:cap].strip()


def project_citation_item(row: Dict[str, Any]) -> Dict[str, Any]:
    """Project an agent_pdp_view row into the offer-free CitationItem.

    Invariants: buyable=False, offers=None, catalog_track='citation'. Never
    includes merchant id/email, internal scores, take-rate, or raw competitor
    offers.

    The ``attribution`` BLOCK is always present, but its ``canonical_url`` is
    NULLABLE — null for a row with no minted sig, because there is no followable
    PDP for one. ``source``/``cite_as``/``attribution_required`` are
    unconditional, so attribution to the source survives a null URL. The contract
    doc's "``attribution.canonical_url`` is always present" rule
    (pivota-merchants-portal/docs/external-citation-api-contract.md) predates the
    measurement that the URL form it specified was a guaranteed 500, and needs
    that follow-up edit; the doc's own EXAMPLE already shows the sig form this
    now emits.
    """
    content_key = row.get("content_key")
    description = str(row.get("description") or "")
    claims = substantiated_claims(row.get("evidence_profile"))

    # THE CITED URL MUST BE THE **SIG** FORM. It used to be
    # ``PDP_URL_PREFIX + content_key``, which is a dead URL for EVERY row this
    # endpoint serves — not just the unrenderable ones.
    #
    # MEASURED 2026-07-26 against prod: ``agent.pivota.cc/products/{ck_*}``
    # returned a hard **HTTP 500** (2,007 bytes, no product JSON-LD) on every
    # probe — 135 serial requests over **133 distinct content_keys**, 34 of them
    # rows the sitemap feed calls ``renderable=true`` and whose OWN sig-form URL
    # serves a real 54-67 KB PDP. Reproduced INDEPENDENTLY by an adversarial
    # re-measurement the same day that set out to refute it: 103 further distinct
    # ck ids, 103/103 dead, byte-identical 2,007-byte bodies, **zero 3xx** (so the
    # old behaviour was broken, not merely a redirect), ``x-vercel-cache: MISS``
    # on every one (500s are never cached, so no ck URL can warm into a 200), and
    # a fabricated ``sig_0000…`` returning the SAME 500/2,007 — i.e. a ck id is
    # indistinguishable from a nonexistent signature, exactly as the mechanism
    # below predicts. The gateway's sig-exact resolve keys on
    # ``cp.pivota_signature_id = $1`` (PIVOTA-Agent ``src/server.js``,
    # ``resolveCatalogProductRefFromPivotaSignatureInner``); nothing there matches
    # a ``ck_*`` id, so the ISR page route turns the miss into a 500. agent-ui
    # already learned this and fixed its half — ``scripts/sitemap_lib.mjs`` drops
    # any row whose ``sig_id`` fails ``/^sig_.+/`` and notes that "get_pdp_v2
    # rejects a bare content_key with MISSING_MERCHANT_CONTEXT". This endpoint was
    # the half that never got the memo.
    #
    # Why this is the worst place for that bug: the response also carries
    # ``attribution_required: true``, so we tell a frontier agent it MUST
    # attribute, and hand it a URL that 500s. ADR-007's premise is that a
    # citation is followable; this made every citation unfollowable.
    #
    # Built via ``pivota_canonical_pdp_url`` rather than a local prefix so the
    # URL is byte-identical to the one ``routes/agent_pdp_v1._row_as_product``
    # emits as ``pivota_canonical_url``/``url`` for the same row. The two
    # surfaces serve the same rows through the same gate and resolver; a citation
    # that disagreed with the PDP read about its own URL would be a second
    # drift of exactly the kind ``services/pdp_renderability`` exists to prevent.
    #
    # ONE COUPLING THAT COMES WITH THAT HELPER: it derives its host from
    # ``CHECKOUT_UI_BASE_URL`` (services/catalog_sync_service.py), whereas
    # ``CITE_AS`` above hardcodes "agent.pivota.cc". VERIFIED 2026-07-26 that prod
    # resolves the env var to exactly ``https://agent.pivota.cc`` — read back off
    # the live ``/api/agent/pdp/{sig}`` response, which builds its URL with this
    # same helper — so the two agree today. If ``CHECKOUT_UI_BASE_URL`` is ever
    # repointed at a checkout or staging host, ONE response would advertise two
    # different hosts; move ``CITE_AS`` onto the same base at that point.
    #
    # NULL WHEN NO SIG IS MINTED, deliberately, matching
    # ``services/agent_pdp_view_assembler`` (``pdp_url = ... if sig else None``).
    # A row with no sig has no followable PDP at all — the ck fallback was never
    # a weaker URL, it was a broken one — so emitting null is the honest answer
    # and strictly better than a guaranteed 500. ``attribution_required`` STAYS
    # TRUE and ``cite_as`` is unchanged: attribution is to the SOURCE ("Pivota —
    # agent.pivota.cc"), which an agent can still honour by name without a deep
    # link. Dropping the requirement instead would hand away the moat on exactly
    # the rows we cannot yet link to.
    #
    # SCOPE — THIS FIXES THE URL **FORM**, NOT ITS RENDERABILITY. A minted sig
    # whose PDP fails either of ``get_pdp_v2``'s gates still gets its own (dead)
    # sig URL here. Measured on the live feed: 879 of 5,887 rows are
    # non-renderable (779 that are ``serving_eligible`` but whose content route
    # does not resolve, plus 100 offer-free ``no_price`` rows admitted by
    # ``INDEX_ELIGIBLE_READ``), and this endpoint answers 200 for them. Teaching
    # this projection ``services.pdp_renderability.pdp_will_render_expression``
    # — and substituting the content_key's renderable sibling, which exists for
    # 229 of the 245 affected content_keys — is the follow-up; it needs a
    # ``catalog_products`` join this content_key-grain projection does not have.
    signature_id = str(row.get("pivota_signature_id") or "").strip()
    canonical_url = (
        pivota_canonical_pdp_url(signature_id)
        if signature_id.startswith("sig_") and len(signature_id) > len("sig_")
        else None
    )

    return {
        "content_key": content_key,
        "title": str(row.get("title") or ""),
        "brand": row.get("brand"),
        "summary": _first_sentence(description),
        "description": description,
        "bullet_points": row.get("bullet_points") or [],
        "usage_scenarios": row.get("usage_scenarios") or [],
        "taxonomy_tags": row.get("taxonomy_tags") or [],
        "image_url": row.get("image_url"),
        # ── trust / substantiation (the differentiator) ──
        "substantiation": {
            "claims": claims,
            "trust_grade": "substantiated" if claims else "listed",
            # Not carried on the served row — disclosed as unknown rather than
            # implying full coverage (honesty seam). Populated when the
            # per-claim verify coverage reaches agent_pdp_view.
            "verify_coverage": None,
        },
        # ── attribution (REQUIRED for the moat) ──
        # canonical_url is the sig-form PDP, or null when no sig is minted — see
        # the measurement above the return for why the content_key form was a
        # guaranteed 500 and why attribution_required stays true regardless.
        "attribution": {
            "source": "Pivota",
            "canonical_url": canonical_url,
            "cite_as": CITE_AS,
            "attribution_required": True,
        },
        # ── routing (content, NOT a commerce offer) ──
        "destination_url": None,  # P0: external brand URL not projected yet
        "buyable": False,
        "catalog_track": "citation",
        "offers": None,
        "usage_terms": {"attribution_required": True, "commercial_use": "cite-and-link"},
    }


def _search_row_to_citation(row: Dict[str, Any]) -> Dict[str, Any]:
    """Adapt a citable-recall row to the CitationItem projection.

    Recall rows are lighter than a full PDP read (no graded claims / structured
    fields), so the projection's substantiation comes back empty here — the agent
    fetches the single-item endpoint for full substantiation. Reuses
    project_citation_item so search + single-item emit the SAME shape.
    """
    return project_citation_item(
        {
            "content_key": row.get("content_key"),
            # Threaded so search emits the same sig-form attribution URL as the
            # single-item read; selected as
            # COALESCE(apv.pivota_signature_id, p.pivota_signature_id) by
            # services/pivot_query_service._fetch_citable_canonical_rows. Absent
            # ⇒ null canonical_url, never the ck form (a guaranteed 500).
            "pivota_signature_id": row.get("pivota_signature_id"),
            "title": row.get("product_title"),
            "description": row.get("product_description"),
            "brand": row.get("brand"),
            "image_url": row.get("product_image_url"),
            "evidence_profile": None,
            "bullet_points": None,
            "usage_scenarios": None,
            "taxonomy_tags": None,
        }
    )


# NOTE: /search MUST be registered before /{citation_id} or FastAPI matches the
# literal "search" as a citation_id.
@router.get("/search")
async def search_citations(
    request: Request,
    response: Response,
    q: str = Query("", description="free-text query"),
    intent: str = Query("inform", description="inform (cite) | shop (suppressed)"),
    limit: int = Query(20, ge=1, le=50),
    _rl: None = Depends(_citation_rate_limit),
) -> Dict[str, Any]:
    agent, client_ip = _caller_identity(request)
    query = str(q or "").strip()
    norm_intent = str(intent or "inform").strip().lower()
    if not query:
        _spawn_log(endpoint="search", status=STATUS_EMPTY, query=query,
                   intent=norm_intent or "inform", result_count=0,
                   agent=agent, client_ip=client_ip)
        return {"items": [], "count": 0, "query": q, "intent": norm_intent or "inform"}

    # Intent gate (parity with the recall lane): shop / strict_serving_mode
    # SUPPRESSES citation rows — an agent driving a checkout never gets a
    # non-buyable row. Only inform-intent surfaces citations.
    if norm_intent in ("shop", "strict", "strict_serving_mode", "transact"):
        response.headers["Cache-Control"] = "public, max-age=120"
        _spawn_log(endpoint="search", status=STATUS_SUPPRESSED, query=query,
                   intent="shop", result_count=0, agent=agent, client_ip=client_ip)
        return {
            "items": [],
            "count": 0,
            "query": query,
            "intent": "shop",
            "suppressed": "citation rows are inform-intent only",
        }

    # Same gate as the slice-3 recall lane (INDEX_ELIGIBLE_RECALL, default OFF).
    from services.pivot_query_service import (
        _fetch_citable_canonical_rows,
        _index_eligible_recall_enabled,
    )

    if not _index_eligible_recall_enabled():
        _spawn_log(endpoint="search", status=STATUS_DISABLED, query=query,
                   intent="inform", result_count=0, agent=agent, client_ip=client_ip)
        return {"items": [], "count": 0, "query": query, "intent": "inform"}

    rows = await _fetch_citable_canonical_rows(query=query, merchant_id=None, limit=limit)
    items = [_search_row_to_citation(r) for r in rows]
    response.headers["Cache-Control"] = "public, max-age=120"
    response.headers["X-Pivota-Citation-Source"] = "Pivota"
    _spawn_log(endpoint="search", status=(STATUS_HIT if items else STATUS_EMPTY),
               query=query, intent="inform", result_count=len(items),
               agent=agent, client_ip=client_ip)
    return {"items": items, "count": len(items), "query": query, "intent": "inform"}


@router.get("/{citation_id}")
async def get_citation(
    citation_id: str,
    request: Request,
    response: Response,
    _rl: None = Depends(_citation_rate_limit),
) -> Dict[str, Any]:
    requested_id = str(citation_id or "").strip()
    agent, client_ip = _caller_identity(request)
    item = await _resolve_citation_item(requested_id)

    # B④-P1: log every call (hit or miss) off the response hot path. A miss is
    # itself signal — an agent asking for a product we don't (yet) cite.
    _spawn_log(
        endpoint="item",
        status=(STATUS_HIT if item else STATUS_MISS),
        requested_id=requested_id or None,
        content_key=(item.get("content_key") if item else None),
        agent=agent,
        client_ip=client_ip,
    )

    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    # Citation data is not real-time; cacheable + CDN-frontable.
    response.headers["Cache-Control"] = "public, max-age=300, stale-while-revalidate=600"
    response.headers["X-Pivota-Citation-Source"] = "Pivota"
    return item


async def _resolve_citation_item(raw: str) -> Optional[Dict[str, Any]]:
    """Resolve a raw path id to a CitationItem, or None when nothing matches.
    No raises — so the single get_citation telemetry point covers hit + miss
    uniformly. Same fail-closed gate / resolver chain as get_pdp."""
    if not raw:
        return None

    # index_eligible rows are readable only when INDEX_ELIGIBLE_READ is on;
    # flag OFF ⇒ serving-only resolution.
    index_read = _index_eligible_read_enabled()

    # ext_* IDs resolve to a content_key first (reuses agent_pdp_v1's resolver).
    if _is_external_product_id(raw):
        resolved = await database.fetch_one(EXT_RESOLVE_SQL, {"ext_id": raw})
        if not resolved:
            return None
        raw = str(dict(resolved).get("content_key") or "")

    sql = _query_for_id(raw, index_eligible_read=index_read)
    if sql is None:
        return None
    row = await database.fetch_one(sql, {"id": raw})
    if not row:
        return None
    return project_citation_item(_row_to_dict(row))
