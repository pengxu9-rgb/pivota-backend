"""External citation read API (ADR-007 P0).

`GET /agent/v1/citation/{id}` — the offer-free **CitationItem** projection a
frontier agent reads to *cite* Pivota, distinct from the commerce-bearing
`get_pdp` render. It reuses the agent_pdp_v1 index_eligible read gate + id
resolver + `substantiated_claims`, and NEVER emits offers / price /
merchant-private fields. Public-read, rate-limited per `X-Pivota-Agent` (else
client IP), cacheable.

Contract: pivota-merchants-portal/docs/external-citation-api-contract.md
"""

from __future__ import annotations

import time
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from db.database import database
from middleware.rate_limiter import AdvancedRateLimiter
from routes.agent_pdp_v1 import (
    EXT_RESOLVE_SQL,
    _index_eligible_read_enabled,
    _is_external_product_id,
    _query_for_id,
    _row_to_dict,
)
from services.claim_safety import substantiated_claims

router = APIRouter(prefix="/agent/v1/citation", tags=["agent-citation"])

PDP_URL_PREFIX = "https://agent.pivota.cc/products/"
CITE_AS = "Pivota — agent.pivota.cc"
SUMMARY_MAX = 200

# Per-source token bucket (in-memory; reuse of the existing limiter). Keyed on
# X-Pivota-Agent when supplied, else client IP. Named-partner tiers (P3) layer
# on later; P0 serves everyone the standard tier.
_limiter = AdvancedRateLimiter()


async def _citation_rate_limit(request: Request) -> None:
    agent = (request.headers.get("X-Pivota-Agent") or "").strip()
    key = agent or (request.client.host if request.client else "anonymous")
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

    Invariants: buyable=False, offers=None, catalog_track='citation', attribution
    always present. Never includes merchant id/email, internal scores, take-rate,
    or raw competitor offers.
    """
    content_key = row.get("content_key")
    description = str(row.get("description") or "")
    claims = substantiated_claims(row.get("evidence_profile"))
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
        "attribution": {
            "source": "Pivota",
            "canonical_url": f"{PDP_URL_PREFIX}{content_key}",
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


@router.get("/{citation_id}")
async def get_citation(
    citation_id: str,
    request: Request,
    response: Response,
    _rl: None = Depends(_citation_rate_limit),
) -> Dict[str, Any]:
    raw = str(citation_id or "").strip()
    if not raw:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    # Same fail-closed gate as get_pdp: index_eligible rows are readable only
    # when INDEX_ELIGIBLE_READ is on; flag OFF ⇒ serving-only resolution.
    index_read = _index_eligible_read_enabled()

    # ext_* IDs resolve to a content_key first (reuses agent_pdp_v1's resolver).
    if _is_external_product_id(raw):
        resolved = await database.fetch_one(EXT_RESOLVE_SQL, {"ext_id": raw})
        if not resolved:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
        raw = str(dict(resolved).get("content_key") or "")

    sql = _query_for_id(raw, index_eligible_read=index_read)
    if sql is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    row = await database.fetch_one(sql, {"id": raw})
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    item = project_citation_item(_row_to_dict(row))
    # Citation data is not real-time; cacheable + CDN-frontable.
    response.headers["Cache-Control"] = "public, max-age=300, stale-while-revalidate=600"
    response.headers["X-Pivota-Citation-Source"] = "Pivota"
    return item
