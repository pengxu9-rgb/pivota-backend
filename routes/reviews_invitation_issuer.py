"""
Internal Reviews Invitation Issuer (server-side)

Goal:
- Allow the order/checkout backend to mint a browser-safe `invitation_token` for a paid order,
  without exposing the proof issuer internal key to browsers.

Flow:
1) Upstream server calls:
   POST /internal/reviews/v1/invitation/issue-from-order
2) This service validates the order is paid and extracts subjects (merchant_id + product/variant ids).
3) It calls the proof issuer internal endpoint to mint an `invitation_token`.

Security:
- Protected by X-Internal-Key (server-side only).
- Never returns or logs PII.
"""

from __future__ import annotations

import hmac
import os
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Header, HTTPException, Response
from pydantic import BaseModel, Field

from db.orders import get_order
from services.merchant_store_service import get_primary_store

router = APIRouter(prefix="/internal/reviews/v1", tags=["internal-reviews-invitation"])


def _internal_key() -> str:
    return (
        (os.getenv("REVIEWS_INVITATION_ISSUER_INTERNAL_KEY") or "").strip()
        or (os.getenv("REVIEWS_PROOF_ISSUER_INTERNAL_KEY") or "").strip()
        or (os.getenv("REVIEWS_BUYER_PROOF_ISSUER_INTERNAL_KEY") or "").strip()
    )


def _require_internal_key(x_internal_key: Optional[str]) -> None:
    expected = _internal_key()
    if not expected:
        raise HTTPException(status_code=503, detail="INVITATION_ISSUER_DISABLED")
    got = (x_internal_key or "").strip()
    if not got or not hmac.compare_digest(got, expected):
        raise HTTPException(status_code=401, detail="UNAUTHORIZED")


def _proof_issuer_base_url() -> str:
    return (os.getenv("REVIEWS_PROOF_ISSUER_BASE_URL") or "").strip()


def _proof_issuer_internal_key() -> str:
    return (
        (os.getenv("REVIEWS_PROOF_ISSUER_INTERNAL_KEY") or "").strip()
        or (os.getenv("REVIEWS_BUYER_PROOF_ISSUER_INTERNAL_KEY") or "").strip()
    )


async def _mint_invitation_via_proof_issuer(
    *,
    merchant_id: str,
    subjects: List[Dict[str, Any]],
    ttl_seconds: int,
    verification: str,
) -> Dict[str, Any]:
    base = _proof_issuer_base_url().rstrip("/")
    key = _proof_issuer_internal_key()
    if not base or not key:
        raise HTTPException(status_code=503, detail="PROOF_ISSUER_DISABLED")

    url = f"{base}/internal/reviews/v1/invitation/issue"
    headers = {"X-Internal-Key": key, "Content-Type": "application/json"}
    payload = {
        "merchant_id": merchant_id,
        "subjects": subjects,
        "verification": verification,
        "ttl_seconds": int(ttl_seconds),
    }

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(url, headers=headers, json=payload)
    except Exception:
        raise HTTPException(status_code=503, detail="PROOF_ISSUER_UNAVAILABLE")

    if resp.status_code != 200:
        if resp.status_code in {401, 403}:
            raise HTTPException(status_code=503, detail="PROOF_ISSUER_UNAVAILABLE")
        raise HTTPException(status_code=503, detail="PROOF_ISSUER_UNAVAILABLE")

    try:
        data = resp.json()
    except Exception:
        raise HTTPException(status_code=503, detail="PROOF_ISSUER_UNAVAILABLE")

    token = str(data.get("invitation_token") or "").strip()
    exp = int(data.get("expires_at") or 0)
    if not token or not exp:
        raise HTTPException(status_code=503, detail="PROOF_ISSUER_UNAVAILABLE")

    return {"invitation_token": token, "expires_at": exp}


class IssueInvitationFromOrderRequest(BaseModel):
    merchant_id: str = Field(..., min_length=1)
    order_id: str = Field(..., min_length=1)
    ttl_seconds: int = Field(7 * 24 * 3600, ge=300, le=7 * 24 * 3600)
    platform_product_id: Optional[str] = None
    variant_id: Optional[str] = None


@router.post("/invitation/issue-from-order")
async def issue_invitation_from_order(
    body: IssueInvitationFromOrderRequest,
    response: Response,
    x_internal_key: Optional[str] = Header(None, alias="X-Internal-Key"),
) -> Dict[str, Any]:
    _require_internal_key(x_internal_key)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"

    merchant_id = (body.merchant_id or "").strip()
    order_id = (body.order_id or "").strip()
    if not merchant_id or not order_id:
        raise HTTPException(status_code=400, detail="INVALID_REQUEST")

    order = await get_order(order_id)
    if not order or str(order.get("merchant_id") or "").strip() != merchant_id:
        raise HTTPException(status_code=404, detail="NOT_FOUND")

    payment_status = str(order.get("payment_status") or "").strip().lower()
    status = str(order.get("status") or "").strip().lower()
    is_paid = payment_status == "paid" or status in {"paid", "shipped", "delivered"}
    if not is_paid:
        raise HTTPException(status_code=403, detail="ORDER_NOT_PAID")

    store = await get_primary_store(merchant_id)
    platform = str((store or {}).get("platform") or "shopify").strip().lower()

    items = order.get("items") or []
    if not isinstance(items, list) or not items:
        raise HTTPException(status_code=400, detail="ORDER_ITEMS_MISSING")

    want_pp = str(body.platform_product_id or "").strip()
    want_vid = str(body.variant_id or "").strip()

    subjects: List[Dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for it in items:
        if not isinstance(it, dict):
            continue
        pp = str(it.get("product_id") or "").strip()
        vid = str(it.get("variant_id") or "").strip()
        if not pp:
            continue
        if want_pp and pp != want_pp:
            continue
        if want_vid and vid != want_vid:
            continue

        key = (pp, vid)
        if key in seen:
            continue
        seen.add(key)

        subject: Dict[str, Any] = {"merchant_id": merchant_id, "platform": platform, "platform_product_id": pp}
        if vid:
            subject["variant_id"] = vid
        subjects.append(subject)

    if not subjects:
        raise HTTPException(status_code=404, detail="NOT_FOUND")

    # IMPORTANT: submission_token is single-use in the Reviews backend, so we must mint
    # one invitation token per subject (product/variant). This enables "one review per line item"
    # without changing the replay/ownership tables.
    out_items: List[Dict[str, Any]] = []
    for s in subjects:
        minted = await _mint_invitation_via_proof_issuer(
            merchant_id=merchant_id,
            subjects=[s],
            ttl_seconds=int(body.ttl_seconds),
            verification="verified_buyer",
        )
        out_items.append({"subject": s, **minted})

    # Convenience compatibility: if exactly one subject, surface the token at top-level too.
    if len(out_items) == 1:
        return {"status": "success", **out_items[0]}

    return {"status": "success", "items": out_items}
