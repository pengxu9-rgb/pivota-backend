from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from mvp.ops import get_default_ops_store
from mvp.schemas import (
    EvidencePointer,
    Geo,
    RecommendationClaim,
    RecommendationContext,
    RecommendationItem,
    RecommendationPack,
)
from routes.agent_auth import AgentContext, get_agent_context


router = APIRouter(prefix="/agent/v1/ops", tags=["agent-ops"])


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class CreateDraftPackRequest(BaseModel):
    merchant_id: str
    pack_id: Optional[str] = None
    session_id: str
    language: str = "en"
    geo: Geo
    surface: str = "unknown"
    recommendations: List[RecommendationItem]


class GenerateDraftPackRequest(BaseModel):
    merchant_id: str
    session_id: str
    language: str = "en"
    geo: Geo
    surface: str = "unknown"
    platform: str = "shopify"
    limit: int = 3
    platform_product_ids: Optional[List[str]] = None


@router.post("/packs/draft")
async def create_draft_pack(
    req: CreateDraftPackRequest,
    context: AgentContext = Depends(get_agent_context),
):
    if not context.can_access_merchant(req.merchant_id):
        raise HTTPException(status_code=403, detail="Not authorized for this merchant")

    pack = RecommendationPack(
        pack_id=req.pack_id or "",
        created_at=_utc_now(),
        context=RecommendationContext(
            session_id=req.session_id,
            creator_id=None,
            language=req.language if req.language in ("en", "zh") else "en",
            geo=req.geo,
            surface=req.surface,
        ),
        recommendations=req.recommendations,
    )

    store = get_default_ops_store()
    rec = await store.create_draft(merchant_id=req.merchant_id, pack=pack)
    return {
        "status": "success",
        "pack_id": rec.pack_id,
        "merchant_id": rec.merchant_id,
        "pack_status": rec.status,
        "validation": rec.validation,
        "pack": rec.pack.model_dump(mode="json"),
    }


@router.post("/packs/generate")
async def generate_draft_pack(
    req: GenerateDraftPackRequest,
    context: AgentContext = Depends(get_agent_context),
):
    """
    Best-effort generator: creates a draft pack from existing product cache rows.

    Constraints:
    - No scraping / automated social discovery. `authorized_signals` are always empty by default.
    - Uses only internal cached product data.
    """
    if not context.can_access_merchant(req.merchant_id):
        raise HTTPException(status_code=403, detail="Not authorized for this merchant")

    try:
        from db.products import get_cached_products
    except Exception as e:
        raise HTTPException(status_code=500, detail={"error": "PRODUCT_CACHE_UNAVAILABLE", "message": str(e)})

    rows = await get_cached_products(req.merchant_id, req.platform, include_expired=False)

    wanted = set(str(x) for x in (req.platform_product_ids or []) if x)
    candidates: List[Dict[str, Any]] = []
    for r in rows or []:
        pid = str(r.get("platform_product_id") or "")
        if wanted and pid not in wanted:
            continue
        candidates.append(r)
        if len(candidates) >= max(1, min(req.limit, 10)):
            break

    rec_items: List[RecommendationItem] = []
    for r in candidates:
        pdata = r.get("product_data") or {}
        platform_product_id = str(r.get("platform_product_id") or "")
        title = str(pdata.get("title") or pdata.get("name") or "Recommended item")
        price = pdata.get("price") or pdata.get("price_min") or pdata.get("amount")
        currency = pdata.get("currency") or "USD"
        tags = pdata.get("tags") or []
        tag_str = ", ".join([str(t) for t in tags[:5] if t]) if isinstance(tags, list) else ""

        narrative = title
        if tag_str:
            narrative += f" — {tag_str}"
        if price is not None:
            narrative += f". Priced at {price} {currency} (from cache)."

        rec_items.append(
            RecommendationItem(
                offer_id=f"offer_unquoted_{req.merchant_id}_{platform_product_id}" if platform_product_id else "offer_unquoted",
                title=title,
                narrative=narrative,
                claims=[
                    RecommendationClaim(
                        claim="Product details available in cache",
                        confidence=0.5,
                        evidence=[
                            EvidencePointer(
                                type="product_cache",
                                ref={
                                    "platform": req.platform,
                                    "platform_product_id": platform_product_id,
                                    "cached_row_id": r.get("id"),
                                },
                            )
                        ],
                    )
                ],
                authorized_signals=[],
            )
        )

    pack = RecommendationPack(
        pack_id="",
        created_at=_utc_now(),
        context=RecommendationContext(
            session_id=req.session_id,
            creator_id=None,
            language=req.language if req.language in ("en", "zh") else "en",
            geo=req.geo,
            surface=req.surface,
        ),
        recommendations=rec_items,
    )

    store = get_default_ops_store()
    rec = await store.create_draft(merchant_id=req.merchant_id, pack=pack)
    return {
        "status": "success",
        "pack_id": rec.pack_id,
        "merchant_id": rec.merchant_id,
        "pack_status": rec.status,
        "validation": rec.validation,
        "pack": rec.pack.model_dump(mode="json"),
    }


@router.get("/packs/{pack_id}")
async def get_pack(
    pack_id: str,
    context: AgentContext = Depends(get_agent_context),
):
    store = get_default_ops_store()
    rec = await store.get(pack_id=pack_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Pack not found")
    if not context.can_access_merchant(rec.merchant_id):
        raise HTTPException(status_code=403, detail="Not authorized for this merchant")
    return {
        "status": "success",
        "pack_id": rec.pack_id,
        "merchant_id": rec.merchant_id,
        "pack_status": rec.status,
        "validation": rec.validation,
        "pack": rec.pack.model_dump(mode="json"),
    }


@router.post("/packs/{pack_id}/validate")
async def validate_pack(
    pack_id: str,
    context: AgentContext = Depends(get_agent_context),
):
    store = get_default_ops_store()
    rec = await store.get(pack_id=pack_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Pack not found")
    if not context.can_access_merchant(rec.merchant_id):
        raise HTTPException(status_code=403, detail="Not authorized for this merchant")
    try:
        nxt = await store.transition(pack_id=pack_id, target_status="validated")
    except ValueError:
        raise HTTPException(status_code=409, detail={"error": "VALIDATION_FAILED", "validation": rec.validation})
    return {
        "status": "success",
        "pack_id": nxt.pack_id,
        "merchant_id": nxt.merchant_id,
        "pack_status": nxt.status,
        "validation": nxt.validation,
    }


@router.post("/packs/{pack_id}/publish")
async def publish_pack(
    pack_id: str,
    context: AgentContext = Depends(get_agent_context),
):
    store = get_default_ops_store()
    rec = await store.get(pack_id=pack_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Pack not found")
    if not context.can_access_merchant(rec.merchant_id):
        raise HTTPException(status_code=403, detail="Not authorized for this merchant")
    if rec.status != "validated":
        raise HTTPException(status_code=409, detail={"error": "INVALID_STATE", "message": "Pack must be validated"})
    nxt = await store.transition(pack_id=pack_id, target_status="published")
    return {"status": "success", "pack_id": nxt.pack_id, "merchant_id": nxt.merchant_id, "pack_status": nxt.status}

