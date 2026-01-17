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
from hashlib import sha256
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Header, HTTPException, Response
from pydantic import BaseModel, Field

from services.buyer_reviews_service import buyer_submit_enabled, buyer_submit_merchant_allowed
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

def _invitation_link_base_url() -> str:
    """
    Optional base URL for buyer review submission UI.

    If set, responses from invitation issuance endpoints include `invitation_url` as:
      {base}#invitation_token=<token>

    Using the URL fragment reduces accidental leakage via Referer headers.
    """
    return (os.getenv("REVIEWS_BUYER_INVITATION_LINK_BASE_URL") or "").strip()


def _invitation_url_for_token(invitation_token: str) -> Optional[str]:
    base = _invitation_link_base_url().strip()
    if not base:
        return None
    b = base.rstrip("#")
    t = (invitation_token or "").strip()
    if not t:
        return None
    return f"{b}#invitation_token={quote(t, safe='')}"


def _sendgrid_api_key() -> str:
    return (os.getenv("SENDGRID_API_KEY") or "").strip()


def _sendgrid_from_email() -> str:
    return (os.getenv("FROM_EMAIL") or "noreply@pivota.ai").strip()


def _sendgrid_template_id() -> str:
    # Optional SendGrid dynamic template ID.
    return (os.getenv("REVIEWS_INVITATION_SENDGRID_TEMPLATE_ID") or "").strip()


async def _send_sendgrid_email(*, to_email: str, subject: str, text_body: str, template_data: Dict[str, Any]) -> None:
    api_key = _sendgrid_api_key()
    if not api_key:
        raise HTTPException(status_code=503, detail="SENDGRID_DISABLED")

    to_email = (to_email or "").strip()
    if not to_email:
        raise HTTPException(status_code=400, detail="ORDER_EMAIL_MISSING")

    from_email = _sendgrid_from_email()
    if not from_email:
        raise HTTPException(status_code=503, detail="SENDGRID_FROM_EMAIL_MISSING")

    template_id = _sendgrid_template_id()
    payload: Dict[str, Any] = {
        "personalizations": [{"to": [{"email": to_email}]}],
        "from": {"email": from_email},
    }

    if template_id:
        payload["template_id"] = template_id
        payload["personalizations"][0]["dynamic_template_data"] = template_data
    else:
        payload["subject"] = subject
        payload["content"] = [{"type": "text/plain", "value": text_body}]

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://api.sendgrid.com/v3/mail/send",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
    except Exception:
        raise HTTPException(status_code=503, detail="SENDGRID_UNAVAILABLE")

    # SendGrid returns 202 Accepted on success.
    if resp.status_code not in {200, 202}:
        raise HTTPException(status_code=503, detail="SENDGRID_UNAVAILABLE")


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

    out: Dict[str, Any] = {"invitation_token": token, "expires_at": exp}
    url = _invitation_url_for_token(token)
    if url:
        out["invitation_url"] = url
    return out

def _order_is_paid(order: Dict[str, Any]) -> bool:
    payment_status = str(order.get("payment_status") or "").strip().lower()
    status = str(order.get("status") or "").strip().lower()
    return payment_status == "paid" or status in {"paid", "shipped", "delivered"}


async def mint_invitations_from_paid_order(
    *,
    merchant_id: str,
    order: Dict[str, Any],
    ttl_seconds: int,
    platform_product_id: Optional[str] = None,
    variant_id: Optional[str] = None,
    verification: str = "verified_buyer",
) -> Dict[str, Any]:
    """
    Core logic: extract product/variant subjects from an order and mint invitation tokens.

    This function assumes the caller has already authorized the request context (agent/employee/etc).
    It enforces that the order is paid, but does not perform any auth checks itself.
    """
    if not order or str(order.get("merchant_id") or "").strip() != (merchant_id or "").strip():
        raise HTTPException(status_code=404, detail="NOT_FOUND")

    if not _order_is_paid(order):
        raise HTTPException(status_code=403, detail="ORDER_NOT_PAID")

    store = await get_primary_store(merchant_id)
    platform = str((store or {}).get("platform") or "shopify").strip().lower()

    items = order.get("items") or []
    if not isinstance(items, list) or not items:
        raise HTTPException(status_code=400, detail="ORDER_ITEMS_MISSING")

    want_pp = str(platform_product_id or "").strip()
    want_vid = str(variant_id or "").strip()

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

    out_items: List[Dict[str, Any]] = []
    for s in subjects:
        minted = await _mint_invitation_via_proof_issuer(
            merchant_id=merchant_id,
            subjects=[s],
            ttl_seconds=int(ttl_seconds),
            verification=verification,
        )
        out_items.append({"subject": s, **minted})

    if len(out_items) == 1:
        return {"status": "success", **out_items[0]}

    return {"status": "success", "items": out_items}


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

    # Debuggable, non-sensitive config surface for internal callers.
    # Helps confirm env propagation without leaking tokens/PII.
    link_base = _invitation_link_base_url().strip()
    response.headers["X-Reviews-Invitation-Link-Configured"] = "1" if link_base else "0"
    if link_base:
        response.headers["X-Reviews-Invitation-Link-Base"] = link_base.rstrip("#")

    merchant_id = (body.merchant_id or "").strip()
    order_id = (body.order_id or "").strip()
    if not merchant_id or not order_id:
        raise HTTPException(status_code=400, detail="INVALID_REQUEST")

    order = await get_order(order_id)
    if not order or str(order.get("merchant_id") or "").strip() != merchant_id:
        raise HTTPException(status_code=404, detail="NOT_FOUND")

    out = await mint_invitations_from_paid_order(
        merchant_id=merchant_id,
        order=order,
        ttl_seconds=int(body.ttl_seconds),
        platform_product_id=body.platform_product_id,
        variant_id=body.variant_id,
        verification="verified_buyer",
    )
    out["invitation_link_base_url_configured"] = bool(link_base)
    if link_base:
        out["invitation_link_base_url"] = link_base.rstrip("#")
    return out


class SendInvitationEmailFromOrderRequest(BaseModel):
    merchant_id: str = Field(..., min_length=1)
    order_id: str = Field(..., min_length=1)
    ttl_seconds: int = Field(7 * 24 * 3600, ge=300, le=7 * 24 * 3600)
    platform_product_id: Optional[str] = None
    variant_id: Optional[str] = None
    max_links: int = Field(3, ge=1, le=10)
    force: bool = False


@router.post("/invitation/send-email-from-order")
async def send_invitation_email_from_order(
    body: SendInvitationEmailFromOrderRequest,
    response: Response,
    x_internal_key: Optional[str] = Header(None, alias="X-Internal-Key"),
) -> Dict[str, Any]:
    """
    Internal helper: mint invitation_token(s) for a paid order and send buyer email via SendGrid.

    Security:
    - Requires X-Internal-Key (server-side only).
    - Never returns tokens or email addresses.
    """
    _require_internal_key(x_internal_key)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"

    # Debuggable, non-sensitive config surface for internal callers.
    link_base = _invitation_link_base_url().strip()
    response.headers["X-Reviews-Invitation-Link-Configured"] = "1" if link_base else "0"
    if link_base:
        response.headers["X-Reviews-Invitation-Link-Base"] = link_base.rstrip("#")

    merchant_id = (body.merchant_id or "").strip()
    order_id = (body.order_id or "").strip()
    if not merchant_id or not order_id:
        raise HTTPException(status_code=400, detail="INVALID_REQUEST")

    if not buyer_submit_enabled():
        raise HTTPException(status_code=404, detail="BUYER_SUBMIT_DISABLED")
    if not buyer_submit_merchant_allowed(merchant_id):
        raise HTTPException(status_code=403, detail="BUYER_SUBMIT_NOT_ALLOWED")

    # Ensure we have a safe landing URL (token placed in URL fragment).
    if not link_base:
        raise HTTPException(status_code=503, detail="INVITATION_LINK_DISABLED")

    order = await get_order(order_id)
    if not order or str(order.get("merchant_id") or "").strip() != merchant_id:
        raise HTTPException(status_code=404, detail="NOT_FOUND")

    to_email = str(order.get("customer_email") or "").strip()
    if not to_email:
        raise HTTPException(status_code=400, detail="ORDER_EMAIL_MISSING")

    metadata = order.get("metadata") or {}
    if isinstance(metadata, dict):
        already_sent_at = str(metadata.get("reviews_invitation_email_sent_at") or "").strip()
    else:
        already_sent_at = ""

    if already_sent_at and not body.force:
        return {"status": "success", "sent": False, "reason": "ALREADY_SENT", "order_id": order_id}

    minted = await mint_invitations_from_paid_order(
        merchant_id=merchant_id,
        order=order,
        ttl_seconds=int(body.ttl_seconds),
        platform_product_id=body.platform_product_id,
        variant_id=body.variant_id,
        verification="verified_buyer",
    )

    items: List[Dict[str, Any]] = []
    if isinstance(minted.get("items"), list):
        items = [x for x in minted["items"] if isinstance(x, dict)]
    elif isinstance(minted.get("subject"), dict):
        items = [minted]  # single-item shape

    invitation_urls: List[str] = []
    invitation_fps: List[str] = []
    subjects: List[Dict[str, Any]] = []
    for it in items:
        url = str(it.get("invitation_url") or "").strip()
        tok = str(it.get("invitation_token") or "").strip()
        subj = it.get("subject")
        if isinstance(subj, dict):
            subjects.append(subj)
        if url:
            invitation_urls.append(url)
        if tok:
            invitation_fps.append(sha256(tok.encode("utf-8")).hexdigest()[:12])

    if not invitation_urls:
        raise HTTPException(status_code=503, detail="INVITATION_LINK_DISABLED")

    invitation_urls = invitation_urls[: int(body.max_links)]

    # Compose email.
    email_subject = "How was your purchase? Leave a review"
    text_body = "Write a review:\n" + "\n".join(invitation_urls) + "\n"
    template_data = {
        "merchant_id": merchant_id,
        "order_id": order_id,
        "invitation_url": invitation_urls[0],
        "invitation_urls": invitation_urls,
        "expires_at": int(minted.get("expires_at") or 0),
    }

    await _send_sendgrid_email(
        to_email=to_email,
        subject=email_subject,
        text_body=text_body,
        template_data=template_data,
    )

    return {
        "status": "success",
        "sent": True,
        "order_id": order_id,
        "subject_count": len(invitation_urls),
        "expires_at": int(minted.get("expires_at") or 0),
        "invitation_fps": invitation_fps[:3],
        "invitation_link_base_url_configured": bool(link_base),
        "invitation_link_base_url": link_base.rstrip("#") if link_base else None,
    }
