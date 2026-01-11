from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from routes.agent_auth import AgentContext, get_agent_context
from services.promotions_service import PromotionStatus, list_promotions


router = APIRouter(prefix="/agent/v1/promotions", tags=["agent-promotions"])


def _matches_scope(promo: Any, *, product_id: Optional[str], variant_id: Optional[str]) -> bool:
    if not product_id and not variant_id:
        return True
    scope = getattr(promo, "scope", None) or {}
    if scope.get("global"):
        return True
    product_ids = scope.get("productIds") or scope.get("product_ids") or []
    if isinstance(product_ids, list) and str(product_id) in [str(x) for x in product_ids]:
        return True
    variant_ids = scope.get("variantIds") or scope.get("variant_ids") or []
    if isinstance(variant_ids, list) and str(variant_id) in [str(x) for x in variant_ids]:
        return True
    # Best-effort: we cannot resolve collection membership without extra Shopify calls,
    # but we still surface the promo so UIs can display "may apply at checkout".
    collection_ids = scope.get("collectionIds") or scope.get("collection_ids") or []
    if isinstance(collection_ids, list) and collection_ids:
        return True
    return False


@router.get("/active")
async def list_active_promotions(
    merchant_id: str = Query(..., min_length=1),
    product_id: Optional[str] = Query(None),
    variant_id: Optional[str] = Query(None),
    channel: str = Query("creator_agents", min_length=1),
    context: AgentContext = Depends(get_agent_context),
) -> Dict[str, Any]:
    """
    List active promotions for a merchant (PII-safe).

    This endpoint is intended for agent/creator frontends to surface marketing
    context (e.g. buy-X-get-Y) without requiring admin credentials.
    """
    if not context.can_access_merchant(merchant_id):
        raise HTTPException(status_code=403, detail="Not authorized for this merchant")

    promos, _ = await list_promotions(
        merchant_id=merchant_id,
        status=PromotionStatus.ACTIVE,
        channel=channel,
        creator_id=getattr(context, "agent_id", None),
        limit=50,
        offset=0,
    )
    # Fallback: channel naming drift across stacks; avoid silently dropping promos.
    if not promos:
        promos, _ = await list_promotions(
            merchant_id=merchant_id,
            status=PromotionStatus.ACTIVE,
            channel=None,
            creator_id=getattr(context, "agent_id", None),
            limit=50,
            offset=0,
        )

    out: List[Dict[str, Any]] = []
    for p in promos or []:
        if not _matches_scope(p, product_id=product_id, variant_id=variant_id):
            continue
        out.append(
            {
                "id": p.id,
                "merchant_id": merchant_id,
                "name": p.name,
                "type": p.type,
                "description": getattr(p, "description", "") or "",
                "human_readable_rule": getattr(p, "humanReadableRule", "") or "",
                "start_at": getattr(p, "startAt", None),
                "end_at": getattr(p, "endAt", None),
                "channels": getattr(p, "channels", None) or [],
                "scope": getattr(p, "scope", None) or {},
                "config": getattr(p, "config", None) or {},
            }
        )

    return {"status": "success", "promotions": out}
