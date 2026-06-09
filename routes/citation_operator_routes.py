"""
HTTP surface for the provider-aware citation operator (merchant-facing).

Endpoints (all gated on a merchant JWT via `get_current_merchant`):

  POST /api/citation-operator/scan                     -> run a provider-aware scan; discover citation gaps
  GET  /api/citation-operator/targets                  -> list discovered gaps for this merchant
  POST /api/citation-operator/targets/{id}/draft       -> generate a METERED draft to close a gap
  GET  /api/citation-operator/actions                  -> the tracking dashboard (drafts + revisit status)
  POST /api/citation-operator/actions/{id}/posted      -> merchant posted -> start the revisit clock
  POST /api/citation-operator/actions/{id}/recheck     -> re-probe now and record whether AI cites them

Ownership is enforced on every per-resource route: a target/action is only
visible/actionable when its `merchant_id` matches the JWT's merchant.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from utils.auth import get_current_merchant, require_admin_or_key
from services import citation_operator_service as cos
from services import citation_draft_service as cds
from services import citation_dashboard_service as dash

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/citation-operator", tags=["citation-operator"])

# scan + recheck fan out LLM probes (real COGS) — bound count AND per-query size
# so a single authenticated call can't amplify cost.
MAX_SCAN_QUERIES = 8
MAX_QUERY_CHARS = 2000
_ALLOWED_PROVIDERS = set(cos.DEFAULT_PROVIDERS)  # gemini, chatgpt


def _clean_queries(raw: Optional[List[str]], *, max_count: int = MAX_SCAN_QUERIES) -> List[str]:
    """Validated, bounded query list for any probe-fanning endpoint."""
    queries = [q.strip() for q in (raw or []) if q and q.strip()]
    if not queries:
        raise HTTPException(status_code=400, detail="queries must be non-empty")
    if len(queries) > max_count:
        raise HTTPException(
            status_code=400, detail=f"at most {max_count} queries (got {len(queries)})")
    for q in queries:
        if len(q) > MAX_QUERY_CHARS:
            raise HTTPException(
                status_code=400, detail=f"each query must be <= {MAX_QUERY_CHARS} chars")
    return queries


class ScanRequest(BaseModel):
    queries: List[str] = Field(..., min_length=1)
    sku_key: Optional[str] = None
    merchant_brand: Optional[str] = None
    merchant_host: Optional[str] = None
    merchant_pdp_url: Optional[str] = None
    product: Optional[Dict[str, Any]] = None
    providers: Optional[List[str]] = None
    idempotency_key: Optional[str] = None  # retry-safe metering (one charge per key+provider)


class DraftRequest(BaseModel):
    action_type: str
    brand: Optional[str] = None
    question: Optional[str] = None
    product: Optional[Dict[str, Any]] = None
    evidence: Optional[List[str]] = None
    idempotency_key: Optional[str] = None


class PostedRequest(BaseModel):
    posted_url: str = Field(..., min_length=1)


class RecheckRequest(BaseModel):
    queries: Optional[List[str]] = None
    merchant_brand: Optional[str] = None
    merchant_host: Optional[str] = None
    product: Optional[Dict[str, Any]] = None


def _queries_from_target(target: Dict[str, Any]) -> List[str]:
    """Recover the probe queries a target was discovered under (stored joined)."""
    q = (target.get("question_text") or "").strip()
    return [s.strip() for s in q.split(";") if s.strip()] or ([q] if q else [])


@router.post("/scan")
async def run_scan(
    body: ScanRequest,
    merchant_id: str = Depends(get_current_merchant),
):
    queries = _clean_queries(body.queries)
    providers = body.providers or list(cos.DEFAULT_PROVIDERS)
    bad = [p for p in providers if p not in _ALLOWED_PROVIDERS]
    if bad:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported provider(s): {bad}. Allowed: {sorted(_ALLOWED_PROVIDERS)}",
        )
    result = await cos.run_citation_scan(
        merchant_id=merchant_id,
        queries=queries,
        merchant_brand=body.merchant_brand,
        merchant_host=body.merchant_host,
        sku_key=body.sku_key,
        product=body.product,
        merchant_pdp_url=body.merchant_pdp_url,
        providers=providers,
        idempotency_key=body.idempotency_key,
    )
    # result = {"billing_mode", "providers": {...}}; free-tier -> not_paid_tier envelope.
    return {"merchant_id": merchant_id, "requested_providers": providers, **result}


@router.get("/dashboard")
async def get_dashboard(
    merchant_id: str = Depends(get_current_merchant),
):
    """Provider-aware visibility overview + the action/citation progress funnel.
    Pure reads over persisted snapshots — never probes (free to view)."""
    overview = await dash.overview(merchant_id=merchant_id)
    prog = await dash.progress(merchant_id=merchant_id)
    return {"overview": overview, "progress": prog}


@router.get("/dashboard/trend")
async def get_dashboard_trend(
    provider: str = Query(...),
    limit: int = Query(30, ge=1, le=180),
    merchant_id: str = Depends(get_current_merchant),
):
    if provider not in _ALLOWED_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported provider: {provider}. Allowed: {sorted(_ALLOWED_PROVIDERS)}",
        )
    return await dash.trend(merchant_id=merchant_id, provider=provider, limit=limit)


@router.get("/targets")
async def list_targets(
    provider: Optional[str] = Query(None),
    channel: Optional[str] = Query(None),
    target_status: Optional[str] = Query(None, alias="status"),
    limit: int = Query(100, ge=1, le=500),
    merchant_id: str = Depends(get_current_merchant),
):
    targets = await cos.list_targets(
        merchant_id=merchant_id, provider=provider, channel=channel,
        status=target_status, limit=limit,
    )
    return {"targets": targets, "count": len(targets)}


@router.post("/targets/{target_id}/draft")
async def draft_for_target(
    target_id: str,
    body: DraftRequest,
    merchant_id: str = Depends(get_current_merchant),
):
    if not cds.supported_action_type(body.action_type):
        raise HTTPException(
            status_code=400,
            detail=f"unsupported action_type: {body.action_type}. "
                   f"Allowed: {list(cds.SUPPORTED_ACTION_TYPES)}",
        )
    target = await cos.get_target(target_id=target_id, merchant_id=merchant_id)
    if not target:
        raise HTTPException(status_code=404, detail="target not found")

    context = {
        "brand": body.brand,
        "question": body.question or target.get("question_text"),
        "subreddit": target.get("subreddit"),
        "channel": target.get("channel"),
        "product": body.product,
        "evidence": body.evidence,
    }
    result = await cds.generate_draft(
        merchant_id=merchant_id,
        target_id=target_id,
        action_type=body.action_type,
        context=context,
        sku_key=target.get("sku_key"),
        idempotency_key=body.idempotency_key,
    )
    return result


@router.get("/actions")
async def list_actions(
    action_status: Optional[str] = Query(None, alias="status"),
    limit: int = Query(100, ge=1, le=500),
    merchant_id: str = Depends(get_current_merchant),
):
    actions = await cos.list_actions(
        merchant_id=merchant_id, status=action_status, limit=limit)
    return {"actions": actions, "count": len(actions)}


@router.post("/actions/{action_id}/posted")
async def mark_posted(
    action_id: str,
    body: PostedRequest,
    merchant_id: str = Depends(get_current_merchant),
):
    action = await cos.get_action(action_id=action_id, merchant_id=merchant_id)
    if not action:
        raise HTTPException(status_code=404, detail="action not found")
    await cos.mark_action_posted(
        action_id=action_id, merchant_id=merchant_id, posted_url=body.posted_url)
    updated = await cos.get_action(action_id=action_id, merchant_id=merchant_id)
    return updated


@router.post("/actions/{action_id}/recheck")
async def recheck(
    action_id: str,
    body: RecheckRequest,
    merchant_id: str = Depends(get_current_merchant),
):
    action = await cos.get_action(action_id=action_id, merchant_id=merchant_id)
    if not action:
        raise HTTPException(status_code=404, detail="action not found")
    target = await cos.get_target(target_id=action["target_id"], merchant_id=merchant_id)
    raw_queries = body.queries or (_queries_from_target(target) if target else [])
    if not raw_queries:
        raise HTTPException(
            status_code=400,
            detail="no queries to re-probe; pass `queries` in the body",
        )
    queries = _clean_queries(raw_queries)  # same count/size cap as /scan (real COGS)
    # Brand/host drive citation detection; fall back to what the scan persisted.
    result = await cos.recheck_action(
        action_id=action_id,
        merchant_id=merchant_id,
        queries=queries,
        merchant_brand=body.merchant_brand or (target or {}).get("merchant_brand"),
        merchant_host=body.merchant_host or (target or {}).get("merchant_host"),
        product=body.product,
    )
    return result


@router.post("/admin/run-due-rechecks")
async def run_due_rechecks_endpoint(
    limit: int = Query(50, ge=1, le=500),
    _admin: Dict[str, Any] = Depends(require_admin_or_key),
):
    """Cron entrypoint (external scheduler hits this with X-ADMIN-KEY): re-probe
    every action whose revisit window is due. Not merchant-scoped — each action is
    rechecked against its own merchant."""
    return await cos.run_due_rechecks(limit=limit)
