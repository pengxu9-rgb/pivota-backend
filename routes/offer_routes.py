from __future__ import annotations

import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from mvp.governance import PolicyInput, governance
from mvp.offer import build_offers_from_quote, preflight_offers
from routes.agent_auth import AgentContext, get_agent_context
from services.quote_service import QuoteError, QuoteService
from services.shopify_policy_service import get_latest_policy_hashes


router = APIRouter(prefix="/agent/v1/offers", tags=["agent-offers"])


class OfferPreflightRequest(BaseModel):
    quote_id: str
    merchant_id: Optional[str] = None
    shipping_address: Optional[Dict[str, Any]] = None
    brief_id: Optional[str] = None
    brief_schema_version: Optional[str] = None


@router.post("/preflight")
async def preflight_quote_offer(
    req: OfferPreflightRequest,
    context: AgentContext = Depends(get_agent_context),
):
    """
    Build canonical OfferObject(s) from a quote snapshot and return structured preflight results.

    Notes:
    - This endpoint is additive and does not replace existing quote/order endpoints.
    - Enforcement is gated by `MVP_PREFLIGHT_ENFORCE=true`.
    """
    try:
        qs = await QuoteService().load_active_quote_or_raise(quote_id=req.quote_id)
    except QuoteError as e:
        raise HTTPException(status_code=400, detail={"error": e.code, "message": e.message})

    merchant_id = str(qs.merchant_id)
    if req.merchant_id and str(req.merchant_id) != merchant_id:
        raise HTTPException(
            status_code=400,
            detail={"error": "QUOTE_MERCHANT_MISMATCH", "message": "quote_id does not belong to merchant_id"},
        )

    if not context.can_access_merchant(merchant_id):
        raise HTTPException(status_code=403, detail="Not authorized for this merchant")

    snap = qs.snapshot_json or {}
    snap_pricing = snap.get("pricing") or {}

    policies = await get_latest_policy_hashes(merchant_id)
    policy_hashes_available = bool(policies)

    try:
        amount_total = float(snap_pricing.get("total") or 0.0)
    except Exception:
        amount_total = None

    geo = None
    if isinstance(req.shipping_address, dict):
        geo = {
            "country": req.shipping_address.get("country"),
            "postal_code": req.shipping_address.get("postal_code") or req.shipping_address.get("zip"),
            "city": req.shipping_address.get("city"),
            "state": req.shipping_address.get("state") or req.shipping_address.get("province"),
        }

    decision = governance.evaluate(
        PolicyInput(
            merchant_id=merchant_id,
            actor_type="agent",
            actor_ref=str(getattr(context, "agent_id", "")) or None,
            action="submit_payment",
            amount=amount_total,
            currency=str(snap.get("currency") or "USD"),
            geo=geo,
            consent_scopes=[],
            approval_id=None,
        )
    )
    hil_required = decision.decision == "require_hil"

    offers = build_offers_from_quote(
        merchant_id=merchant_id,
        quote_id=qs.quote_id,
        expires_at=qs.expires_at,
        engine=str(snap.get("engine") or qs.engine or "unknown"),
        engine_ref=str(snap.get("engine_ref") or qs.engine_ref or ""),
        currency=str(snap.get("currency") or "USD"),
        pricing=snap_pricing,
        line_items=snap.get("line_items") or [],
        delivery_options=snap.get("delivery_options"),
        shipping_address=req.shipping_address,
    )

    preflight = preflight_offers(
        offers=offers,
        policy_hashes_available=policy_hashes_available,
        hil_required=hil_required,
        hil_reason=",".join(decision.reason_codes) if hil_required else None,
    )

    enforce_preflight = os.getenv("MVP_PREFLIGHT_ENFORCE", "false").lower() == "true"
    if enforce_preflight and any(r.status == "fail" for r in preflight):
        raise HTTPException(
            status_code=409,
            detail={
                "error": "PREFLIGHT_FAILED",
                "message": "Offer preflight failed; checkout blocked.",
                "preflight": [p.model_dump(mode="json") for p in preflight],
            },
        )

    brief_id = (req.brief_id or None)
    brief_schema_version = (req.brief_schema_version or None)
    try:
        meta = (snap.get("metadata") or {}) if isinstance(snap, dict) else {}
        brief_meta = (meta.get("brief") or {}) if isinstance(meta, dict) else {}
        if not brief_id:
            brief_id = brief_meta.get("brief_id") or None
        if not brief_schema_version:
            brief_schema_version = brief_meta.get("brief_schema_version") or None
    except Exception:
        pass

    return {
        "status": "success",
        "quote_id": qs.quote_id,
        "merchant_id": merchant_id,
        "offers": [o.model_dump(mode="json") for o in offers],
        "payment_offer_evidence": snap.get("payment_offer_evidence") or {},
        "payment_pricing": snap.get("payment_pricing") or {},
        "savings_presentation": snap.get("savings_presentation") or {},
        "preflight": [p.model_dump(mode="json") for p in preflight],
        "policy_hashes_available": policy_hashes_available,
        "policy_hashes": [
            {"policy_type": p.get("policy_type"), "hash_sha256": p.get("hash_sha256")} for p in (policies or [])
        ],
        "risk_tier": decision.risk_tier,
        "brief_id": brief_id,
        "brief_schema_version": brief_schema_version,
    }
