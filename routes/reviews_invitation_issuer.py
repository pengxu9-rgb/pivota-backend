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
import html
import os
import secrets
import time
from hashlib import sha256
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlsplit

import httpx
from fastapi import APIRouter, Header, HTTPException, Response
from pydantic import BaseModel, Field

from observability.reviews_metrics import record_invitation_issue, record_invitation_send
from services.buyer_reviews_service import buyer_submit_enabled, buyer_submit_merchant_allowed
from services.reviews_invitation_send_jobs_service import (
    enqueue_invitation_send_job_from_order as _enqueue_invitation_send_job_from_order,
)
from db.orders import get_order
from services.merchant_store_service import get_primary_store
from db.database import database

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

def _invitation_send_delay_seconds() -> int:
    """
    Delay between order shipped/fulfilled and invitation email send.

    Default 0 keeps backward compatibility (send immediately where enabled).
    """
    raw = (os.getenv("REVIEWS_INVITATION_SEND_DELAY_SECONDS") or "").strip()
    try:
        val = int(raw) if raw else 0
    except Exception:
        val = 0
    return max(0, val)


async def _ensure_invitation_send_jobs_table_best_effort() -> None:
    """
    Best-effort schema ensure for delayed invitation email jobs.
    Keeps prod/staging usable even if migrations are not applied yet.
    """
    try:
        await database.execute(
            """
            CREATE TABLE IF NOT EXISTS reviews_invitation_send_jobs (
              id BIGSERIAL PRIMARY KEY,
              merchant_id VARCHAR(64) NOT NULL,
              order_id VARCHAR(64) NOT NULL,
              send_at TIMESTAMPTZ NOT NULL,
              status VARCHAR(32) NOT NULL DEFAULT 'pending',
              attempts INTEGER NOT NULL DEFAULT 0,
              last_attempt_at TIMESTAMPTZ NULL,
              sent_at TIMESTAMPTZ NULL,
              sendgrid_message_id TEXT NULL,
              last_error TEXT NULL,
              created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
    except Exception:
        return

    try:
        await database.execute(
            "CREATE INDEX IF NOT EXISTS idx_reviews_invitation_send_jobs_due ON reviews_invitation_send_jobs (status, send_at)"
        )
    except Exception:
        pass

    try:
        await database.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_reviews_invitation_send_jobs_order_status ON reviews_invitation_send_jobs (order_id, status)"
        )
    except Exception:
        pass


async def enqueue_invitation_email_send_job_from_order(*, merchant_id: str, order_id: str) -> bool:
    """
    Enqueue a delayed invitation email send job for an order (idempotent).

    Returns True if a pending job exists after the call.
    """
    mid = (merchant_id or "").strip()
    oid = (order_id or "").strip()
    if not mid or not oid:
        return False

    await _ensure_invitation_send_jobs_table_best_effort()

    # Dedupe: if we've already sent (or are currently sending) for this order,
    # do not create a new pending job.
    try:
        existing = await database.fetch_one(
            """
            SELECT status
            FROM reviews_invitation_send_jobs
            WHERE order_id = :order_id AND status IN ('sent', 'processing', 'pending')
            ORDER BY
              CASE status
                WHEN 'sent' THEN 1
                WHEN 'processing' THEN 2
                WHEN 'pending' THEN 3
                ELSE 99
              END ASC
            LIMIT 1
            """,
            {"order_id": oid},
        )
        if existing:
            st = str(existing["status"] or "").strip()
            if st == "sent":
                return False
            if st in {"processing", "pending"}:
                return True
    except Exception:
        # Best-effort: do not fail order flows on dedupe query errors.
        pass

    delay = _invitation_send_delay_seconds()
    # If delay is 0, callers should send immediately (compat); still allow enqueue for tests.
    try:
        await database.execute(
            """
            INSERT INTO reviews_invitation_send_jobs
              (merchant_id, order_id, send_at, status, updated_at)
            VALUES
              (:merchant_id, :order_id, NOW() + (:delay * interval '1 second'), 'pending', NOW())
            ON CONFLICT (order_id, status)
            DO UPDATE SET
              send_at = EXCLUDED.send_at,
              updated_at = NOW()
            """,
            {"merchant_id": mid, "order_id": oid, "delay": int(delay)},
        )
    except Exception:
        # Best-effort: do not fail order flows on enqueue errors.
        return False

    return True

def _invitation_link_base_url() -> str:
    """
    Optional base URL for buyer review submission UI.

    If set, responses from invitation issuance endpoints include `invitation_url` as:
      {base}#invitation_token=<token>

    Using the URL fragment reduces accidental leakage via Referer headers.
    """
    return (os.getenv("REVIEWS_BUYER_INVITATION_LINK_BASE_URL") or "").strip()

def _invitation_shortlink_base_url() -> str:
    """
    Optional base URL for short links (recommended for emails).

    If not set, derives `https://{host}/r` from `REVIEWS_BUYER_INVITATION_LINK_BASE_URL` when possible.
    """
    explicit = (os.getenv("REVIEWS_BUYER_INVITATION_SHORTLINK_BASE_URL") or "").strip()
    if explicit:
        return explicit.rstrip("/")

    base = _invitation_link_base_url().strip()
    if not base:
        return ""
    parts = urlsplit(base)
    if parts.scheme and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}/r"
    return ""


async def _create_shortlink(
    *,
    merchant_id: str,
    order_id: str,
    invitation_token: str,
    expires_at: int,
) -> Optional[str]:
    base = _invitation_shortlink_base_url().strip()
    if not base:
        return None

    tok = (invitation_token or "").strip()
    if not tok:
        return None

    # Best-effort insert; retry a few times on rare code collision.
    for _ in range(5):
        code = "rv_" + secrets.token_urlsafe(9).replace("-", "").replace("_", "")
        code = code[:32]
        try:
            await database.execute(
                """
                INSERT INTO reviews_invitation_shortlinks
                  (code, merchant_id, order_id, invitation_token, expires_at)
                VALUES
                  (:code, :merchant_id, :order_id, :invitation_token, to_timestamp(:exp))
                """,
                {
                    "code": code,
                    "merchant_id": merchant_id,
                    "order_id": order_id,
                    "invitation_token": tok,
                    "exp": int(expires_at or 0),
                },
            )
            return f"{base.rstrip('/')}/{quote(code, safe='')}"
        except Exception:
            continue

    return None


def _invitation_url_for_token(invitation_token: str, *, order_id: Optional[str] = None) -> Optional[str]:
    base = _invitation_link_base_url().strip()
    if not base:
        return None
    b = base.rstrip("#")
    t = (invitation_token or "").strip()
    if not t:
        return None
    oid = (order_id or "").strip()
    if oid:
        joiner = "&" if "?" in b else "?"
        return f"{b}{joiner}order_id={quote(oid, safe='')}#invitation_token={quote(t, safe='')}"
    return f"{b}#invitation_token={quote(t, safe='')}"


def _sendgrid_api_key() -> str:
    return (os.getenv("SENDGRID_API_KEY") or "").strip()


def _sendgrid_from_email() -> str:
    return (os.getenv("FROM_EMAIL") or "noreply@pivota.ai").strip()


def _sendgrid_template_id() -> str:
    # Optional SendGrid dynamic template ID.
    return (os.getenv("REVIEWS_INVITATION_SENDGRID_TEMPLATE_ID") or "").strip()


def _use_sendgrid_template() -> bool:
    """
    Whether to use SendGrid dynamic templates for review invitations.

    Default is off to keep the copy controlled by code, and to avoid long visible
    URLs when templates render raw links.
    """
    raw = (os.getenv("REVIEWS_INVITATION_USE_SENDGRID_TEMPLATE") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _disable_sendgrid_click_tracking() -> bool:
    """
    Whether to disable SendGrid click tracking for invitation emails.

    Default is enabled (i.e. we do NOT disable) because some recipient mail
    gateways/filters are more likely to accept SendGrid-tracked links than
    direct links to new domains.
    """
    raw = (os.getenv("REVIEWS_INVITATION_DISABLE_SENDGRID_CLICK_TRACKING") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _include_store_url_in_email() -> bool:
    raw = (os.getenv("REVIEWS_INVITATION_INCLUDE_STORE_URL") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _support_email_default(*, store: Optional[Dict[str, Any]], from_email: str) -> str:
    if isinstance(store, dict):
        email = str(store.get("support_email") or "").strip()
        if email:
            return email
    # Allow operators to intentionally set an empty support email (omit footer).
    if "REVIEWS_INVITATION_SUPPORT_EMAIL" in os.environ:
        return (os.environ.get("REVIEWS_INVITATION_SUPPORT_EMAIL") or "").strip()

    # Backward-compatible fallback (opt-in).
    raw = (os.getenv("REVIEWS_INVITATION_SUPPORT_EMAIL_FALLBACK_TO_FROM_EMAIL") or "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return (from_email or "").strip()

    return ""


def _sendgrid_from_name() -> str:
    return (os.getenv("REVIEWS_INVITATION_FROM_NAME") or "").strip()


def _reply_to_support_email_enabled() -> bool:
    raw = (os.getenv("REVIEWS_INVITATION_REPLY_TO_SUPPORT_EMAIL") or "").strip().lower()
    # Default on: makes replies go to support when provided.
    return raw not in {"0", "false", "no", "off"}


def _store_name_default(store: Optional[Dict[str, Any]]) -> str:
    if isinstance(store, dict):
        name = str(store.get("name") or "").strip()
        if name:
            return name
    return (os.getenv("REVIEWS_INVITATION_STORE_NAME") or "our store").strip()


def _store_url_default(store: Optional[Dict[str, Any]]) -> str:
    if not _include_store_url_in_email():
        return ""
    if isinstance(store, dict):
        domain = str(store.get("domain") or "").strip()
        if domain:
            if domain.startswith("http://") or domain.startswith("https://"):
                return domain
            return f"https://{domain}"
    return (os.getenv("REVIEWS_INVITATION_STORE_URL") or "").strip()


def _first_name_from_order(order: Dict[str, Any]) -> str:
    raw = str(order.get("customer_name") or "").strip()
    if not raw:
        return ""
    return raw.split()[0].strip()


def _compose_invitation_email(
    *,
    first_name: str,
    store_name: str,
    review_link: str,
    support_email: str,
    store_url: str,
) -> tuple[str, str, str, Dict[str, Any]]:
    safe_first = (first_name or "").strip()
    safe_store = (store_name or "our store").strip()
    safe_link = (review_link or "").strip()
    safe_support = (support_email or "").strip()
    safe_store_url = (store_url or "").strip()

    greeting = f"Hi {safe_first}," if safe_first else "Hi,"

    closing_parts: List[str] = []
    if safe_support:
        closing_parts.append(safe_support)
    if safe_store_url:
        closing_parts.append(safe_store_url)
    closing_line = " | ".join(closing_parts)

    text_lines = [
        greeting,
        "",
        f"Thanks again for your recent purchase from {safe_store}—we hope you’re loving it so far.",
        "",
        "If you have a minute, would you share your feedback by leaving a quick review? Your review helps other shoppers feel confident, and it helps us keep improving.",
        "",
        f"Leave your review here: {safe_link}",
        "",
        "As a thank-you, you’ll also help us decide what to stock and build next.",
        "",
        "Thanks for your time,",
        safe_store,
    ]
    if closing_line:
        text_lines.append(closing_line)

    subject = f"How was your purchase from {safe_store}?"

    # HTML version: show a short, friendly CTA instead of a long visible URL.
    safe_link_html = html.escape(safe_link, quote=True)
    safe_store_html = html.escape(safe_store, quote=True)
    safe_first_html = html.escape(safe_first, quote=True)
    greeting_html = f"Hi {safe_first_html}," if safe_first_html else "Hi,"

    html_lines = [
        f"<p>{greeting_html}</p>",
        f"<p>Thanks again for your recent purchase from {safe_store_html}—we hope you’re loving it so far.</p>",
        "<p>If you have a minute, would you share your feedback by leaving a quick review? "
        "Your review helps other shoppers feel confident, and it helps us keep improving.</p>",
        f'<p><a href="{safe_link_html}">Click here to leave your review!</a></p>',
        "<p>As a thank-you, you’ll also help us decide what to stock and build next.</p>",
        f"<p>Thanks for your time,<br>{safe_store_html}</p>",
    ]
    if safe_support:
        html_lines.append(f"<p>{html.escape(safe_support, quote=True)}</p>")

    html_body = "\n".join(html_lines).strip() + "\n"

    template_data: Dict[str, Any] = {
        "FirstName": safe_first,
        "StoreName": safe_store,
        "ReviewLink": safe_link,
        "SupportEmail": safe_support,
        "StoreURL": safe_store_url,
        "first_name": safe_first,
        "store_name": safe_store,
        "review_link": safe_link,
        "support_email": safe_support,
        "store_url": safe_store_url,
    }

    return subject, "\n".join(text_lines).strip() + "\n", html_body, template_data


async def _send_sendgrid_email(
    *,
    to_email: str,
    subject: str,
    text_body: str,
    html_body: str,
    template_data: Dict[str, Any],
    reply_to_email: Optional[str] = None,
) -> Dict[str, Any]:
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
    use_template = bool(template_id and _use_sendgrid_template())
    from_name = _sendgrid_from_name()
    payload: Dict[str, Any] = {
        "personalizations": [{"to": [{"email": to_email}]}],
        "from": {"email": from_email},
    }
    if from_name:
        payload["from"]["name"] = from_name

    if _reply_to_support_email_enabled():
        reply = (reply_to_email or "").strip()
        if reply:
            payload["reply_to"] = {"email": reply}
    click_tracking_disabled = False
    if _disable_sendgrid_click_tracking():
        payload["tracking_settings"] = {"click_tracking": {"enable": False, "enable_text": False}}
        click_tracking_disabled = True

    if use_template:
        payload["template_id"] = template_id
        payload["personalizations"][0]["dynamic_template_data"] = template_data
    else:
        payload["subject"] = subject
        payload["content"] = [
            {"type": "text/plain", "value": text_body},
            {"type": "text/html", "value": html_body},
        ]

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

    # Best-effort: capture message id for debugging (non-PII).
    msg_id = (
        resp.headers.get("x-message-id")
        or resp.headers.get("X-Message-Id")
        or resp.headers.get("X-Message-ID")
        or ""
    ).strip()
    return {
        "sendgrid_status_code": resp.status_code,
        "sendgrid_message_id": msg_id or None,
        "sendgrid_template_used": bool(use_template),
        "sendgrid_click_tracking_disabled": click_tracking_disabled,
    }


async def _mint_invitation_via_proof_issuer(
    *,
    merchant_id: str,
    subjects: List[Dict[str, Any]],
    ttl_seconds: int,
    verification: str,
    order_id: Optional[str] = None,
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
    url = _invitation_url_for_token(token, order_id=order_id)
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
    order_id: Optional[str] = None,
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

    order_id_value = (order_id or "").strip() or str(order.get("order_id") or "").strip()
    out_items: List[Dict[str, Any]] = []
    for s in subjects:
        minted = await _mint_invitation_via_proof_issuer(
            merchant_id=merchant_id,
            subjects=[s],
            ttl_seconds=int(ttl_seconds),
            verification=verification,
            order_id=order_id_value,
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
    t0 = time.monotonic()
    try:
        _require_internal_key(x_internal_key)
    except HTTPException as e:
        record_invitation_issue(result="error", reason=str(e.detail or "UNAUTHORIZED"), duration_seconds=time.monotonic() - t0)
        raise
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
        record_invitation_issue(result="error", reason="INVALID_REQUEST", duration_seconds=time.monotonic() - t0)
        raise HTTPException(status_code=400, detail="INVALID_REQUEST")

    order = await get_order(order_id)
    if not order or str(order.get("merchant_id") or "").strip() != merchant_id:
        record_invitation_issue(result="error", reason="NOT_FOUND", duration_seconds=time.monotonic() - t0)
        raise HTTPException(status_code=404, detail="NOT_FOUND")

    try:
        out = await mint_invitations_from_paid_order(
            merchant_id=merchant_id,
            order=order,
            ttl_seconds=int(body.ttl_seconds),
            platform_product_id=body.platform_product_id,
            variant_id=body.variant_id,
            verification="verified_buyer",
            order_id=order_id,
        )
    except HTTPException as e:
        record_invitation_issue(result="error", reason=str(e.detail or "ERROR"), duration_seconds=time.monotonic() - t0)
        raise
    except Exception:
        record_invitation_issue(result="error", reason="ERROR", duration_seconds=time.monotonic() - t0)
        raise

    out["invitation_link_base_url_configured"] = bool(link_base)
    if link_base:
        out["invitation_link_base_url"] = link_base.rstrip("#")
    record_invitation_issue(result="success", reason="ok", duration_seconds=time.monotonic() - t0)
    return out


class SendInvitationEmailFromOrderRequest(BaseModel):
    merchant_id: str = Field(..., min_length=1)
    order_id: str = Field(..., min_length=1)
    ttl_seconds: int = Field(7 * 24 * 3600, ge=300, le=7 * 24 * 3600)
    platform_product_id: Optional[str] = None
    variant_id: Optional[str] = None
    max_links: int = Field(3, ge=1, le=10)
    force: bool = False


class EnqueueInvitationSendJobRequest(BaseModel):
    merchant_id: str = Field(..., min_length=1)
    order_id: str = Field(..., min_length=1)
    force_reschedule: bool = False
    send_now: bool = False


@router.post("/invitation/enqueue-send-job")
async def enqueue_invitation_send_job(
    body: EnqueueInvitationSendJobRequest,
    response: Response,
    x_internal_key: Optional[str] = Header(None, alias="X-Internal-Key"),
) -> Dict[str, Any]:
    """
    Internal helper to enqueue a send job (useful for ops/testing without SQL access).
    """
    _require_internal_key(x_internal_key)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"

    out = await _enqueue_invitation_send_job_from_order(
        merchant_id=(body.merchant_id or "").strip(),
        order_id=(body.order_id or "").strip(),
        force_reschedule=bool(body.force_reschedule),
        send_now=bool(body.send_now),
    )
    return {"status": "success", **out}


@router.get("/invitation/jobs/stats")
async def invitation_send_jobs_stats(
    response: Response,
    x_internal_key: Optional[str] = Header(None, alias="X-Internal-Key"),
) -> Dict[str, Any]:
    """
    Lightweight, non-PII stats for alerting/ops dashboards.

    This is intentionally internal-only and does not expose order ids or emails.
    """
    _require_internal_key(x_internal_key)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"

    await _ensure_invitation_send_jobs_table_best_effort()

    counts: Dict[str, int] = {}
    try:
        rows = await database.fetch_all(
            "SELECT status, COUNT(*)::bigint AS n FROM reviews_invitation_send_jobs GROUP BY status"
        )
        for r in rows:
            try:
                counts[str(r.get("status") or "").strip() or "unknown"] = int(r.get("n") or 0)
            except Exception:
                continue
    except Exception:
        counts = {}

    pending_oldest_seconds: Optional[int] = None
    try:
        row = await database.fetch_one(
            """
            SELECT EXTRACT(EPOCH FROM (NOW() - MIN(send_at)))::bigint AS age
            FROM reviews_invitation_send_jobs
            WHERE status='pending'
            """
        )
        if row and row.get("age") is not None:
            pending_oldest_seconds = max(0, int(row.get("age") or 0))
    except Exception:
        pending_oldest_seconds = None

    return {
        "status": "success",
        "counts": counts,
        "pending_oldest_seconds": pending_oldest_seconds,
    }


@router.get("/invitation/jobs/by-order")
async def invitation_send_jobs_by_order(
    order_id: str,
    response: Response,
    x_internal_key: Optional[str] = Header(None, alias="X-Internal-Key"),
) -> Dict[str, Any]:
    """
    Internal debug endpoint to inspect send-job statuses for a specific order.
    No PII; intended for ops verification without SQL access.
    """
    _require_internal_key(x_internal_key)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"

    oid = (order_id or "").strip()
    if not oid:
        raise HTTPException(status_code=400, detail="INVALID_REQUEST")

    await _ensure_invitation_send_jobs_table_best_effort()
    rows = await database.fetch_all(
        """
        SELECT id, status, attempts, send_at, last_attempt_at, sent_at, last_error
        FROM reviews_invitation_send_jobs
        WHERE order_id = :order_id
        ORDER BY id DESC
        LIMIT 10
        """,
        {"order_id": oid},
    )
    items: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        err = str(d.get("last_error") or "")
        if err:
            d["last_error"] = err[:256]
        items.append(d)

    return {"status": "success", "order_id": oid, "items": items}


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
    t0 = time.monotonic()
    try:
        _require_internal_key(x_internal_key)
    except HTTPException as e:
        record_invitation_send(result="error", reason=str(e.detail or "UNAUTHORIZED"), sent=False, duration_seconds=time.monotonic() - t0)
        raise
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
        record_invitation_send(result="error", reason="INVALID_REQUEST", sent=False, duration_seconds=time.monotonic() - t0)
        raise HTTPException(status_code=400, detail="INVALID_REQUEST")

    if not buyer_submit_enabled():
        record_invitation_send(result="success", reason="BUYER_SUBMIT_DISABLED", sent=False, duration_seconds=time.monotonic() - t0)
        return {"status": "success", "sent": False, "reason": "BUYER_SUBMIT_DISABLED", "order_id": order_id}
    if not buyer_submit_merchant_allowed(merchant_id):
        record_invitation_send(result="success", reason="BUYER_SUBMIT_NOT_ALLOWED", sent=False, duration_seconds=time.monotonic() - t0)
        return {"status": "success", "sent": False, "reason": "BUYER_SUBMIT_NOT_ALLOWED", "order_id": order_id}

    # Ensure we have a safe landing URL (token placed in URL fragment).
    if not link_base:
        record_invitation_send(result="error", reason="INVITATION_LINK_DISABLED", sent=False, duration_seconds=time.monotonic() - t0)
        raise HTTPException(status_code=503, detail="INVITATION_LINK_DISABLED")

    order = await get_order(order_id)
    if not order or str(order.get("merchant_id") or "").strip() != merchant_id:
        record_invitation_send(result="error", reason="NOT_FOUND", sent=False, duration_seconds=time.monotonic() - t0)
        raise HTTPException(status_code=404, detail="NOT_FOUND")

    to_email = str(order.get("customer_email") or "").strip()
    if not to_email:
        record_invitation_send(result="error", reason="ORDER_EMAIL_MISSING", sent=False, duration_seconds=time.monotonic() - t0)
        raise HTTPException(status_code=400, detail="ORDER_EMAIL_MISSING")
    to_email_lower = to_email.lower()
    to_email_domain = ""
    if "@" in to_email_lower:
        to_email_domain = to_email_lower.split("@", 1)[1].strip()
    to_email_fp = sha256(to_email_lower.encode("utf-8")).hexdigest()[:12] if to_email_lower else ""

    metadata = order.get("metadata") or {}
    if isinstance(metadata, dict):
        already_sent_at = str(metadata.get("reviews_invitation_email_sent_at") or "").strip()
    else:
        already_sent_at = ""

    if already_sent_at and not body.force:
        record_invitation_send(result="success", reason="ALREADY_SENT", sent=False, duration_seconds=time.monotonic() - t0)
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
    short_urls: List[str] = []
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
            short_url = await _create_shortlink(
                merchant_id=merchant_id,
                order_id=order_id,
                invitation_token=tok,
                expires_at=int(minted.get("expires_at") or 0),
            )
            if short_url:
                short_urls.append(short_url)

    if not invitation_urls:
        record_invitation_send(result="error", reason="INVITATION_LINK_DISABLED", sent=False, duration_seconds=time.monotonic() - t0)
        raise HTTPException(status_code=503, detail="INVITATION_LINK_DISABLED")

    invitation_urls = invitation_urls[: int(body.max_links)]
    short_urls = short_urls[: int(body.max_links)]

    # Compose email.
    store = await get_primary_store(merchant_id)
    from_email = _sendgrid_from_email()
    store_name = _store_name_default(store)
    store_url = _store_url_default(store)
    support_email = _support_email_default(store=store, from_email=from_email)
    first_name = _first_name_from_order(order)

    review_link = short_urls[0] if short_urls else invitation_urls[0]
    email_subject, text_body, html_body, template_data = _compose_invitation_email(
        first_name=first_name,
        store_name=store_name,
        review_link=review_link,
        support_email=support_email,
        store_url=store_url,
    )
    template_data.update(
        {
            "merchant_id": merchant_id,
            "order_id": order_id,
            "invitation_url": review_link,
            "invitation_urls": short_urls or invitation_urls,
            "expires_at": int(minted.get("expires_at") or 0),
        }
    )

    try:
        sendgrid_meta = await _send_sendgrid_email(
            to_email=to_email,
            subject=email_subject,
            text_body=text_body,
            html_body=html_body,
            template_data=template_data,
            reply_to_email=support_email,
        )
    except HTTPException as e:
        record_invitation_send(result="error", reason=str(e.detail or "SEND_FAILED"), sent=False, duration_seconds=time.monotonic() - t0)
        raise
    except Exception:
        record_invitation_send(result="error", reason="SEND_FAILED", sent=False, duration_seconds=time.monotonic() - t0)
        raise

    # Best-effort: mark as sent to prevent duplicates.
    try:
        from datetime import datetime, timezone
        from db.orders import update_order

        new_meta: Dict[str, Any] = {}
        if isinstance(metadata, dict):
            new_meta = dict(metadata)
        new_meta["reviews_invitation_email_sent_at"] = datetime.now(timezone.utc).isoformat()
        if sendgrid_meta.get("sendgrid_message_id"):
            new_meta["reviews_invitation_sendgrid_message_id"] = str(sendgrid_meta["sendgrid_message_id"])
        await update_order(order_id, {"metadata": new_meta})
    except Exception:
        pass

    record_invitation_send(result="success", reason="ok", sent=True, duration_seconds=time.monotonic() - t0)
    return {
        "status": "success",
        "sent": True,
        "order_id": order_id,
        "to_email_domain": to_email_domain or None,
        "to_email_fp": to_email_fp or None,
        "subject_count": len(invitation_urls),
        "expires_at": int(minted.get("expires_at") or 0),
        "invitation_fps": invitation_fps[:3],
        "shortlink_base_url": _invitation_shortlink_base_url().strip() or None,
        "invitation_link_base_url_configured": bool(link_base),
        "invitation_link_base_url": link_base.rstrip("#") if link_base else None,
        **sendgrid_meta,
    }
