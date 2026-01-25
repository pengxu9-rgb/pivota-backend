"""
Accounts & Orders API

Customer-facing authentication + "My Orders" + public order lookup/track.
This sits alongside the existing employee/merchant auth system and does not
change any of the legacy /api/auth or /auth/* endpoints.

Paths implemented here follow the contract in:
  pivota-agent-frontend/docs/accounts-orders-api.md
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import asyncio
import logging
from textwrap import dedent

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    status,
    Body,
)
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr, Field, validator
from sqlalchemy import and_, func, select

from config.settings import settings
from db.database import database
from db.accounts import (
    shop_users,
    shop_user_memberships,
    shop_login_otps,
    public_order_lookup_logs,
    normalize_email,
    create_or_get_shop_user,
    record_public_lookup,
    count_recent_public_lookup_by_ip,
    count_recent_public_lookup_by_key,
)
from db.orders import orders as orders_table
from utils.auth import create_access_token, decode_token
from services.ugc_capabilities_service import (
    UgcSubject,
    has_user_reviewed_subject,
    is_question_rate_limited,
    user_has_purchased_subject,
)


router = APIRouter(prefix="/accounts", tags=["accounts-orders"])
logger = logging.getLogger("accounts_orders")


# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

ACCESS_COOKIE_NAME = "acc_access_token"
REFRESH_COOKIE_NAME = "acc_refresh_token"

# Access token lifetime for accounts UI sessions.
# Previously this was 30 minutes, which caused shoppers to be logged out
# during longer checkout flows. For the Shopping Agent / developer portal
# we extend this to a full 7 days to match the refresh window.
ACCESS_EXPIRE_MINUTES = 7 * 24 * 60  # 7 days
# Refresh token lifetime: rolling 7-day window
REFRESH_EXPIRE_DAYS = 7

PUBLIC_LOOKUP_IP_LIMIT_PER_MINUTE = 10
PUBLIC_LOOKUP_PAIR_LIMIT_PER_MINUTE = 3


def _error(status_code: int, code: str, message: str) -> HTTPException:
    """Create an HTTPException with a structured error payload."""
    return HTTPException(
        status_code=status_code,
        detail={"error": {"code": code, "message": message}},
    )


async def _ensure_database_connected() -> None:
    """
    Ensure `database` is connected before running queries.

    In production we usually connect during startup, but transient DB/network issues can
    leave the app in a degraded state where `databases.Database` raises AssertionError.
    """
    if getattr(database, "is_connected", False):
        return
    try:
        await asyncio.wait_for(database.connect(), timeout=3)
    except Exception as exc:
        logger.warning(f"Database not available for request (connect failed): {exc}")
        raise _error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "TEMPORARY_UNAVAILABLE",
            "Temporary database unavailable. Please retry shortly.",
        )


def _get_client_ip(request: Request) -> str:
    """Best-effort client IP extraction (considering proxies)."""
    x_forwarded_for = request.headers.get("x-forwarded-for")
    if x_forwarded_for:
        # X-Forwarded-For can be a comma-separated list; use first
        return x_forwarded_for.split(",")[0].strip()
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    client = request.client
    return client.host if client else "unknown"


def _mask_email(email: str) -> str:
    """Simple email masking for public responses."""
    try:
        local, domain = email.split("@", 1)
    except ValueError:
        return "***"
    if len(local) <= 1:
        masked_local = "*"
    elif len(local) == 2:
        masked_local = local[0] + "*"
    else:
        masked_local = local[0] + "***" + local[-1]
    return f"{masked_local}@{domain}"


def _amount_to_minor(amount: Optional[float]) -> int:
    if amount is None:
        return 0
    # Avoid floating precision issues by rounding
    return int(round(float(amount) * 100))


def _parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        # fromisoformat supports "YYYY-MM-DD" and full ISO strings
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt
    except Exception:
        return None


class AccountsPrincipal(BaseModel):
    user_id: str
    email: str
    email_normalized: str
    primary_role: str = "customer"


async def get_accounts_principal(request: Request) -> AccountsPrincipal:
    """Dependency that reads the accounts access token cookie and returns principal."""
    token = request.cookies.get(ACCESS_COOKIE_NAME)
    if not token:
        raise _error(status.HTTP_401_UNAUTHORIZED, "UNAUTHENTICATED", "Not logged in")

    try:
        payload = decode_token(token)
    except HTTPException:
        raise _error(status.HTTP_401_UNAUTHORIZED, "UNAUTHENTICATED", "Invalid or expired session")

    sub = payload.get("sub")
    email = payload.get("email")
    role = payload.get("role", "customer")
    if not sub or not email:
        raise _error(status.HTTP_401_UNAUTHORIZED, "UNAUTHENTICATED", "Invalid token payload")

    norm = normalize_email(email)
    return AccountsPrincipal(user_id=sub, email=email, email_normalized=norm, primary_role=role)


async def get_accounts_principal_ugc(request: Request) -> AccountsPrincipal:
    """
    UGC endpoints require a simple error contract:
      - 401 NOT_AUTHENTICATED

    Wrap the shared accounts session checker but normalize the code.
    """
    try:
        return await get_accounts_principal(request)
    except HTTPException:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="NOT_AUTHENTICATED")


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


def _send_login_otp_email(email: str, otp_code: str) -> None:
    """
    Best-effort email sender for login OTP codes.

    Uses SendGrid when SENDGRID_API_KEY / settings.sendgrid_api_key is configured.
    Failures are logged but never propagated to the caller, so login flow
    still returns 200 even if email delivery fails.
    """
    api_key = getattr(settings, "sendgrid_api_key", None)
    if not api_key:
        logger.info(
            "[AccountsAuth] SENDGRID_API_KEY not configured; "
            "skipping OTP email send"
        )
        return

    from_email = getattr(settings, "from_email", "noreply@pivota.ai")

    subject = "Your login code for Pivota"
    text_content = (
        f"Your login code is {otp_code}. It will expire in 10 minutes.\n"
        "If you did not request this code, you can ignore this email."
    )
    html_content = dedent(
        f"""
        <p>Your login code is <strong>{otp_code}</strong>.</p>
        <p>It will expire in 10 minutes.</p>
        <p>If you did not request this code, you can ignore this email.</p>
        """
    ).strip()

    try:
        import requests

        response = requests.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "personalizations": [{"to": [{"email": email}]}],
                "from": {"email": from_email, "name": "Pivota"},
                "subject": subject,
                "content": [
                    {"type": "text/plain", "value": text_content},
                    {"type": "text/html", "value": html_content},
                ],
            },
            timeout=10,
        )
        if response.status_code >= 400:
            logger.error(
                "[AccountsAuth] Failed to send OTP email via SendGrid: "
                "status=%s body=%s",
                response.status_code,
                response.text,
            )
        else:
            logger.info(
                "[AccountsAuth] OTP email sent via SendGrid to %s", email
            )
    except Exception as exc:
        logger.error(
            "[AccountsAuth] Exception while sending OTP email: %s", exc
        )

class LoginStartRequest(BaseModel):
    channel: str = Field(..., description="email | sms")
    email: Optional[EmailStr] = None
    phone: Optional[str] = None

    @validator("channel")
    def validate_channel(cls, v: str) -> str:
        v = v.lower()
        if v not in {"email", "sms"}:
            raise ValueError("channel must be 'email' or 'sms'")
        return v

    @validator("phone")
    def trim_phone(cls, v: Optional[str]) -> Optional[str]:
        return v.strip() if isinstance(v, str) else v

    @validator("email", always=True)
    def validate_one_of_email_phone(cls, v: Optional[str], values: Dict[str, Any]) -> Optional[str]:
        channel = values.get("channel")
        phone = values.get("phone")
        if channel == "email" and not v:
            raise ValueError("email is required when channel=email")
        if channel == "sms" and not phone:
            raise ValueError("phone is required when channel=sms")
        return v


class VerifyRequest(BaseModel):
    channel: str
    email: Optional[EmailStr] = None
    phone: Optional[str] = None
    otp: str = Field(..., min_length=4, max_length=12)

    @validator("channel")
    def validate_channel(cls, v: str) -> str:
        v = v.lower()
        if v not in {"email", "sms"}:
            raise ValueError("channel must be 'email' or 'sms'")
        return v

    @validator("otp")
    def trim_otp(cls, v: str) -> str:
        return v.strip()


class MembershipInfo(BaseModel):
    merchant_id: str
    role: str


class UserSessionPayload(BaseModel):
    user: Dict[str, Any]
    memberships: List[MembershipInfo]
    active_merchant_id: Optional[str]
    is_new_user: bool
    has_claimable_orders: bool


class OrdersListItem(BaseModel):
    order_id: str
    currency: str
    total_amount_minor: int
    status: str
    payment_status: str
    fulfillment_status: Optional[str]
    delivery_status: str
    created_at: str
    # Optional creator metadata, flattened from orders.metadata for Creator Agent views
    creator_id: Optional[str] = None
    creator_name: Optional[str] = None
    creator_slug: Optional[str] = None
    shipping_city: Optional[str]
    shipping_country: Optional[str]
    items_summary: str
    permissions: Dict[str, bool]
    # Optional preview image for the first item in the order, used by
    # frontends to render a thumbnail on the orders list.
    first_item_image_url: Optional[str] = None


class OrdersListResponse(BaseModel):
    orders: List[OrdersListItem]
    next_cursor: Optional[str]
    has_more: bool


class PublicOrderLookupResponse(BaseModel):
    order_id: str
    status: str
    currency: str
    total_amount_minor: int
    created_at: str
    items_summary: str
    shipping: Dict[str, Optional[str]]
    customer: Dict[str, str]


class PublicTrackEvent(BaseModel):
    status: str
    timestamp: str
    completed: bool
    description: Optional[str] = None


class PublicTrackResponse(BaseModel):
    order_id: str
    delivery_status: str
    timeline: List[PublicTrackEvent]


class CancelOrderRequest(BaseModel):
    """Optional cancel payload from customer."""
    reason: Optional[str] = Field(
        default=None,
        description="Optional free-form reason provided by the customer.",
    )


class CancelOrderResponse(BaseModel):
    """Minimal order summary returned after cancellation."""
    order_id: str
    status: str
    payment_status: str
    fulfillment_status: str
    delivery_status: str
    updated_at: str


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _build_user_session(user_row: dict) -> UserSessionPayload:
    """Assemble the session payload (user + memberships + derived flags)."""
    user_id = user_row["id"]
    email = user_row["email"]
    email_norm = user_row["email_normalized"]
    primary_role = user_row.get("primary_role", "customer")

    # Memberships
    mem_rows = await database.fetch_all(
        shop_user_memberships.select().where(shop_user_memberships.c.user_id == user_id)
    )
    memberships: List[MembershipInfo] = [
        MembershipInfo(merchant_id=row["merchant_id"], role=row["role"])
        for row in mem_rows
    ]
    active_merchant_id = memberships[0].merchant_id if memberships else None

    # Claimable orders (by email)
    has_claimable_orders = False
    try:
        count = await database.fetch_val(
            """
            SELECT COUNT(*) 
            FROM orders 
            WHERE LOWER(customer_email) = :email
              AND is_deleted = FALSE
            """,
            {"email": email_norm},
        )
        has_claimable_orders = bool(count and count > 0)
    except Exception:
        # Fail-open: just mark as False if query fails
        has_claimable_orders = False

    user_payload = {
        "id": user_id,
        "email": email,
        "phone": user_row.get("phone"),
        "primary_role": primary_role,
        "is_guest": bool(user_row.get("is_guest")),
    }

    return UserSessionPayload(
        user=user_payload,
        memberships=memberships,
        active_merchant_id=active_merchant_id,
        is_new_user=bool(user_row.get("is_new_user", False)),
        has_claimable_orders=has_claimable_orders,
    )


def _derive_first_item_image_url(items: List[Dict[str, Any]]) -> Optional[str]:
    """
    Best-effort extraction of a preview image URL from the first order item.

    The shape of items is flexible (different flows may store slightly different
    keys), so we try a small set of common fields and fall back gracefully.
    """
    if not items:
        return None

    first = items[0] or {}
    image_url = (
        first.get("image_url")
        or first.get("main_image_url")
        or (first.get("images") or [None])[0]
    )

    if isinstance(image_url, str):
        image_url = image_url.strip()
        if image_url:
            return image_url
    return None


def _set_auth_cookies(
    response: JSONResponse,
    user_id: str,
    email: str,
    primary_role: str = "customer",
) -> None:
    """Set access + refresh cookies for accounts API."""
    base_payload = {
        "sub": user_id,
        "email": email,
        "role": primary_role,
        "scope": "accounts",
    }
    access_token = create_access_token(
        base_payload,
        expires_delta=timedelta(minutes=ACCESS_EXPIRE_MINUTES),
    )
    refresh_payload = {
        **base_payload,
        "scope": "accounts_refresh",
    }
    refresh_token = create_access_token(
        refresh_payload,
        expires_delta=timedelta(days=REFRESH_EXPIRE_DAYS),
    )

    # For production (dev_mode=False), issue cross-site compatible cookies
    # so that the Accounts API can be called from separate frontends
    secure = not settings.dev_mode
    samesite = "lax" if settings.dev_mode else "none"

    response.set_cookie(
        ACCESS_COOKIE_NAME,
        access_token,
        max_age=ACCESS_EXPIRE_MINUTES * 60,
        httponly=True,
        secure=secure,
        samesite=samesite,
        path="/",
    )
    response.set_cookie(
        REFRESH_COOKIE_NAME,
        refresh_token,
        max_age=REFRESH_EXPIRE_DAYS * 24 * 60 * 60,
        httponly=True,
        secure=secure,
        samesite=samesite,
        path="/",
    )


def _clear_auth_cookies(response: JSONResponse) -> None:
    secure = not settings.dev_mode
    samesite = "lax" if settings.dev_mode else "none"
    for name in (ACCESS_COOKIE_NAME, REFRESH_COOKIE_NAME):
        response.set_cookie(
            name,
            "",
            max_age=0,
            expires=0,
            httponly=True,
            secure=secure,
            samesite=samesite,
            path="/",
        )


def _map_payment_status(raw_status: Optional[str]) -> str:
    raw = (raw_status or "").lower()
    if raw in {"paid", "succeeded"}:
        return "paid"
    if raw in {"refunded"}:
        return "refunded"
    if raw in {"failed"}:
        return "failed"
    if raw in {"partial", "partially_paid"}:
        return "partial"
    return "pending"


def _map_fulfillment_status(raw_status: Optional[str]) -> str:
    raw = (raw_status or "").lower()
    # Backend uses `shipped` as the main fulfillment transition; map it to `fulfilled`
    # for the customer-facing API so that "paid + shipped" is treated as completed.
    if raw in {"fulfilled", "delivered", "shipped"}:
        return "fulfilled"
    if raw in {"partially_fulfilled", "partial"}:
        return "partially_fulfilled"
    if raw in {"returned"}:
        return "returned"
    return "not_fulfilled"


def _derive_delivery_status(
    fulfillment_status: Optional[str], tracking_number: Optional[str]
) -> str:
    f = (fulfillment_status or "").lower()
    if f in {"fulfilled", "delivered"}:
        return "delivered"
    if f in {"partially_fulfilled"}:
        return "in_transit"
    if tracking_number:
        # Has tracking but not fulfilled yet
        return "in_transit"
    return "not_shipped"


def _derive_order_status(
    payment_status: str, fulfillment_status: str, cancelled: bool, refunded: bool
) -> str:
    if refunded:
        return "refunded"
    if cancelled:
        return "cancelled"
    if payment_status == "paid" and fulfillment_status == "fulfilled":
        return "completed"
    if payment_status == "paid" and fulfillment_status != "fulfilled":
        return "paid"
    if payment_status in {"failed"}:
        return "pending"
    return "pending"


def _compute_permissions(order_row: dict, principal: AccountsPrincipal) -> Dict[str, bool]:
    payment_status = _map_payment_status(order_row.get("payment_status"))
    fulfillment_status = _map_fulfillment_status(order_row.get("fulfillment_status"))

    can_pay = payment_status == "pending"
    can_cancel = payment_status == "pending" and fulfillment_status == "not_fulfilled"
    can_reorder = payment_status == "paid"

    return {
        "can_pay": can_pay,
        "can_cancel": can_cancel,
        "can_reorder": can_reorder,
    }


def _build_items_summary(items: List[Dict[str, Any]]) -> str:
    if not items:
        return ""
    first = items[0]
    title = first.get("product_title") or first.get("title") or "Item"
    qty = first.get("quantity", 1)
    if len(items) == 1:
        return f"{title} x{qty}"
    return f"{title} x{qty} (+{len(items) - 1} more)"


# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------

@router.post("/auth/login")
async def start_login(request: Request, body: LoginStartRequest):
    """
    Start login flow (OTP via email or SMS).

    For now, only channel=email is fully supported; sms will return INVALID_INPUT.
    """
    channel = body.channel.lower()
    if channel == "sms":
        raise _error(
            status.HTTP_400_BAD_REQUEST,
            "INVALID_INPUT",
            "SMS login channel is not supported yet",
        )

    email = str(body.email)
    norm = normalize_email(email)
    ip = _get_client_ip(request)

    # Basic rate limiting using OTP table (per IP and per email)
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(minutes=1)

    try:
        ip_count = await database.fetch_val(
            """
            SELECT COUNT(*) FROM shop_login_otps
            WHERE ip_address = :ip AND created_at >= :since
            """,
            {"ip": ip, "since": window_start},
        )
        email_count = await database.fetch_val(
            """
            SELECT COUNT(*) FROM shop_login_otps
            WHERE email_normalized = :email AND created_at >= :since
            """,
            {"email": norm, "since": window_start},
        )
    except Exception:
        ip_count = 0
        email_count = 0

    if ip_count and ip_count > 20:
        raise _error(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "RATE_LIMITED",
            "Too many login attempts from this IP. Please try again later.",
        )
    if email_count and email_count > 5:
        raise _error(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "RATE_LIMITED",
            "Too many login attempts for this email. Please try again later.",
        )

    # Generate OTP
    import secrets

    if not settings.dev_mode:
        otp_code = f"{secrets.randbelow(999999):06d}"
    else:
        # For staging/dev, use deterministic code to simplify testing
        otp_code = "123456"

    expires_at = now + timedelta(minutes=10)

    await database.execute(
        shop_login_otps.insert().values(
            channel="email",
            email_normalized=norm,
            phone=None,
            otp_code=otp_code,
            ip_address=ip,
            created_at=now,
            expires_at=expires_at,
            attempt_count=0,
            max_attempts=5,
        )
    )

    # Log OTP for observability (and dev environments)
    logger.info("[AccountsAuth] OTP generated for %s", email)

    # Best-effort email delivery; failures are logged but do not break the flow
    _send_login_otp_email(email, otp_code)

    # In non-production, optionally echo the OTP for easier local testing
    payload: Dict[str, Any] = {"status": "sent"}
    if settings.dev_mode:
        payload["debug_otp"] = otp_code

    return payload


@router.post("/auth/verify")
async def verify_login(body: VerifyRequest, request: Request):
    """
    Verify OTP and issue session cookies.
    """
    channel = body.channel.lower()
    if channel == "sms":
        raise _error(
            status.HTTP_400_BAD_REQUEST,
            "INVALID_INPUT",
            "SMS login channel is not supported yet",
        )

    email = str(body.email)
    norm = normalize_email(email)

    # Look up latest OTP
    otp_row = await database.fetch_one(
        """
        SELECT * FROM shop_login_otps
        WHERE channel = :channel AND email_normalized = :email
        ORDER BY created_at DESC
        LIMIT 1
        """,
        {"channel": "email", "email": norm},
    )

    if not otp_row:
        raise _error(
            status.HTTP_400_BAD_REQUEST,
            "INVALID_OTP",
            "Invalid or expired verification code",
        )

    now = datetime.now(timezone.utc)
    if otp_row["consumed_at"] is not None or otp_row["expires_at"] < now:
        raise _error(
            status.HTTP_400_BAD_REQUEST,
            "INVALID_OTP",
            "Invalid or expired verification code",
        )

    if otp_row["otp_code"] != body.otp.strip():
        # Increment attempt counter (best-effort)
        try:
            await database.execute(
                shop_login_otps.update()
                .where(shop_login_otps.c.id == otp_row["id"])
                .values(
                    attempt_count=(otp_row["attempt_count"] or 0) + 1,
                    consumed_at=None,
                )
            )
        except Exception:
            pass
        raise _error(
            status.HTTP_400_BAD_REQUEST,
            "INVALID_OTP",
            "Invalid or expired verification code",
        )

    # Mark consumed
    try:
        await database.execute(
            shop_login_otps.update()
            .where(shop_login_otps.c.id == otp_row["id"])
            .values(consumed_at=now)
        )
    except Exception:
        pass

    # Create or get shop user
    user_row = await create_or_get_shop_user(email=email, phone=body.phone)
    user_row["is_new_user"] = bool(user_row.get("created_at") and user_row["created_at"] >= now - timedelta(minutes=1))

    session_payload = await _build_user_session(user_row)
    response = JSONResponse(session_payload.dict())
    _set_auth_cookies(
        response,
        user_id=user_row["id"],
        email=user_row["email"],
        primary_role=user_row.get("primary_role", "customer"),
    )
    return response


@router.get("/auth/me")
async def get_me(principal: AccountsPrincipal = Depends(get_accounts_principal)):
    await _ensure_database_connected()
    # Reload user from DB to get memberships and flags
    try:
        user_row = await database.fetch_one(
            shop_users.select().where(shop_users.c.id == principal.user_id)
        )
    except AssertionError as exc:
        logger.warning(f"Database assertion in /auth/me (degraded): {exc}")
        raise _error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "TEMPORARY_UNAVAILABLE",
            "Temporary database unavailable. Please retry shortly.",
        )
    if not user_row:
        raise _error(
            status.HTTP_401_UNAUTHORIZED,
            "UNAUTHENTICATED",
            "User not found",
        )
    session_payload = await _build_user_session(dict(user_row))
    return session_payload


@router.get("/pdp/v2/personalization")
async def get_pdp_v2_personalization(
    response: Response,
    principal: AccountsPrincipal = Depends(get_accounts_principal_ugc),
    product_id: str = Query(..., min_length=1, alias="productId"),
    product_group_id: Optional[str] = Query(None, alias="productGroupId"),
):
    """
    User-specific PDP v2 personalization (UGC capabilities).

    NOTE: This endpoint must never be cached by CDN/browser.
    """
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"

    pid = str(product_id or "").strip()
    pgid = str(product_group_id or "").strip() or None
    subject_id = pgid or pid

    subject = UgcSubject(
        subject_type="product_group" if pgid else "product",
        subject_id=subject_id,
        product_id=pid,
        product_group_id=pgid,
    )

    is_purchaser = await user_has_purchased_subject(
        email_normalized=principal.email_normalized,
        subject=subject,
    )
    already_reviewed = await has_user_reviewed_subject(
        user_id=principal.user_id,
        subject_type=subject.subject_type,
        subject_id=subject.subject_id,
    )

    question_rate_limited = await is_question_rate_limited(
        user_id=principal.user_id,
        subject_type=subject.subject_type,
        subject_id=subject.subject_id,
        window_seconds=60,
    )

    can_upload = bool(is_purchaser)
    can_review = bool(is_purchaser) and not bool(already_reviewed)
    can_ask = not bool(question_rate_limited)

    reasons: Dict[str, str] = {}
    if not can_upload:
        reasons["upload"] = "NOT_PURCHASER"
    if not can_review:
        reasons["review"] = "ALREADY_REVIEWED" if already_reviewed else "NOT_PURCHASER"
    if not can_ask:
        reasons["question"] = "RATE_LIMITED"

    return {
        "ugcCapabilities": {
            "canUploadMedia": can_upload,
            "canWriteReview": can_review,
            "canAskQuestion": can_ask,
            "reasons": reasons,
        }
    }


@router.get("/reviews/eligibility")
async def get_review_eligibility(
    response: Response,
    principal: AccountsPrincipal = Depends(get_accounts_principal_ugc),
    product_id: str = Query(..., min_length=1, alias="productId"),
    product_group_id: Optional[str] = Query(None, alias="productGroupId"),
):
    """
    Best-effort "can I write a review?" endpoint for UX gating.

    Returns:
      { eligible: boolean, reason?: "NOT_PURCHASER"|"ALREADY_REVIEWED" }
    """
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"

    pid = str(product_id or "").strip()
    pgid = str(product_group_id or "").strip() or None
    subject_id = pgid or pid

    subject = UgcSubject(
        subject_type="product_group" if pgid else "product",
        subject_id=subject_id,
        product_id=pid,
        product_group_id=pgid,
    )

    is_purchaser = await user_has_purchased_subject(
        email_normalized=principal.email_normalized,
        subject=subject,
    )

    if not is_purchaser:
        return {"eligible": False, "reason": "NOT_PURCHASER"}

    already_reviewed = await has_user_reviewed_subject(
        user_id=principal.user_id,
        subject_type=subject.subject_type,
        subject_id=subject.subject_id,
    )
    if already_reviewed:
        return {"eligible": False, "reason": "ALREADY_REVIEWED"}

    return {"eligible": True}


@router.post("/auth/refresh")
async def refresh_token(request: Request):
    token = request.cookies.get(REFRESH_COOKIE_NAME)
    if not token:
        raise _error(
            status.HTTP_401_UNAUTHORIZED,
            "UNAUTHENTICATED",
            "No refresh token",
        )

    try:
        payload = decode_token(token)
    except HTTPException:
        raise _error(
            status.HTTP_401_UNAUTHORIZED,
            "UNAUTHENTICATED",
            "Invalid or expired refresh token",
        )

    if payload.get("scope") != "accounts_refresh":
        raise _error(
            status.HTTP_401_UNAUTHORIZED,
            "UNAUTHENTICATED",
            "Invalid refresh token",
        )

    user_id = payload.get("sub")
    email = payload.get("email")
    primary_role = payload.get("role", "customer")
    if not user_id or not email:
        raise _error(
            status.HTTP_401_UNAUTHORIZED,
            "UNAUTHENTICATED",
            "Invalid refresh token payload",
        )

    response = JSONResponse({"status": "ok"})
    _set_auth_cookies(response, user_id=user_id, email=email, primary_role=primary_role)
    return response


@router.post("/auth/logout")
async def logout():
    response = JSONResponse({"status": "ok"})
    _clear_auth_cookies(response)
    return response


# ---------------------------------------------------------------------------
# Protected Orders endpoints
# ---------------------------------------------------------------------------

@router.get("/orders/list", response_model=OrdersListResponse)
async def list_orders(
    response: Response,
    principal: AccountsPrincipal = Depends(get_accounts_principal),
    cursor: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    status_filter: Optional[str] = Query(None, alias="status"),
    payment_status: Optional[str] = None,
    fulfillment_status: Optional[str] = None,
    from_time: Optional[str] = Query(None, alias="from"),
    to_time: Optional[str] = Query(None, alias="to"),
    q: Optional[str] = Query(None, max_length=100),
    merchant_id: Optional[str] = None,
):
    """
    List orders for the current accounts user.

    - Customers: only their own orders (by email).
    - Merchant staff: requires memberships (not yet populated in production).
    """
    try:
        # This endpoint is user-specific; never cache at the edge/browser.
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        response.headers["X-Content-Type-Options"] = "nosniff"

        offset = int(cursor or 0)

        where_clauses = []

        # Access control
        if principal.primary_role == "customer":
            where_clauses.append(
                func.lower(orders_table.c.customer_email) == principal.email_normalized
            )
        else:
            # For now, only customers are fully supported
            raise _error(
                status.HTTP_403_FORBIDDEN,
                "FORBIDDEN",
                "Only customer accounts can access orders via this API for now",
            )

        # Filters
        if from_time:
            dt_from = _parse_iso_datetime(from_time)
            if not dt_from:
                raise _error(
                    status.HTTP_400_BAD_REQUEST,
                    "INVALID_INPUT",
                    "Invalid from datetime",
                )
            where_clauses.append(orders_table.c.created_at >= dt_from)
        if to_time:
            dt_to = _parse_iso_datetime(to_time)
            if not dt_to:
                raise _error(
                    status.HTTP_400_BAD_REQUEST,
                    "INVALID_INPUT",
                    "Invalid to datetime",
                )
            where_clauses.append(orders_table.c.created_at <= dt_to)

        if status_filter:
            # Map summary status onto underlying fields; for now, best-effort simple mapping
            if status_filter == "paid":
                where_clauses.append(orders_table.c.payment_status == "paid")
            elif status_filter == "cancelled":
                where_clauses.append(orders_table.c.status == "cancelled")
            elif status_filter == "refunded":
                where_clauses.append(orders_table.c.status == "refunded")
            # Other statuses are derived and filtered client-side for now

        if payment_status:
            where_clauses.append(
                func.lower(orders_table.c.payment_status) == payment_status.lower()
            )

        if fulfillment_status:
            where_clauses.append(
                func.lower(orders_table.c.fulfillment_status)
                == fulfillment_status.lower()
            )

        if q:
            q_trimmed = q.strip()
            pattern = f"%{q_trimmed}%"
            where_clauses.append(
                (
                    orders_table.c.order_id.ilike(pattern)
                    | orders_table.c.customer_email.ilike(pattern)
                )
            )

        query = (
            orders_table.select()
            .where(and_(*where_clauses))
            .order_by(orders_table.c.created_at.desc())
            .limit(limit)
            .offset(offset)
        )

        rows = await database.fetch_all(query)
        orders_list: List[OrdersListItem] = []

        for row in rows:
            data = dict(row)
            metadata = data.get("metadata") or {}
            creator_id = None
            creator_name = None
            creator_slug = None
            try:
                if isinstance(metadata, dict):
                    creator_id = metadata.get("creator_id") or metadata.get("creatorId")
                    creator_name = metadata.get("creator_name") or metadata.get("creatorName")
                    creator_slug = metadata.get("creator_slug") or metadata.get("creatorSlug")
            except Exception:
                # Metadata is best-effort; never break listing on parse errors.
                creator_id = None
                creator_name = None
                creator_slug = None

            payment_status_mapped = _map_payment_status(data.get("payment_status"))
            fulfillment_status_mapped = _map_fulfillment_status(
                data.get("fulfillment_status")
            )
            delivery_status = _derive_delivery_status(
                data.get("fulfillment_status"), data.get("tracking_number")
            )
            status_summary = _derive_order_status(
                payment_status_mapped,
                fulfillment_status_mapped,
                cancelled=(data.get("status") == "cancelled"),
                refunded=(data.get("status") == "refunded"),
            )

            shipping = data.get("shipping_address") or {}
            items = data.get("items") or []

            orders_list.append(
                OrdersListItem(
                    order_id=data["order_id"],
                    currency=data.get("currency", "USD"),
                    total_amount_minor=_amount_to_minor(data.get("total")),
                    status=status_summary,
                    payment_status=payment_status_mapped,
                    fulfillment_status=fulfillment_status_mapped,
                    delivery_status=delivery_status,
                    created_at=(data.get("created_at") or datetime.now(timezone.utc)).isoformat(),
                    creator_id=creator_id,
                    creator_name=creator_name,
                    creator_slug=creator_slug,
                    shipping_city=shipping.get("city"),
                    shipping_country=shipping.get("country"),
                    items_summary=_build_items_summary(items),
                    permissions=_compute_permissions(data, principal),
                    first_item_image_url=_derive_first_item_image_url(items),
                )
            )

        has_more = len(rows) == limit
        next_cursor = str(offset + limit) if has_more else None
        return OrdersListResponse(orders=orders_list, next_cursor=next_cursor, has_more=has_more)
    except HTTPException:
        raise
    except Exception as e:
        from utils.logger import logger as app_logger

        app_logger.error(f"[AccountsOrders] list_orders failed: {type(e).__name__}: {e}")
        raise _error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "SERVER_ERROR",
            "list_orders failed",
        )


@router.get("/orders/{order_id}")
async def get_order_detail(
    order_id: str,
    response: Response,
    principal: AccountsPrincipal = Depends(get_accounts_principal),
):
    # This endpoint is user-specific; never cache at the edge/browser.
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"

    order = await database.fetch_one(
        orders_table.select().where(orders_table.c.order_id == order_id)
    )
    if not order:
        raise _error(
            status.HTTP_404_NOT_FOUND,
            "NOT_FOUND",
            "Order not found",
        )

    order_data = dict(order)

    # Access control: customers can only see their own orders
    if principal.primary_role == "customer":
        if normalize_email(order_data.get("customer_email", "")) != principal.email_normalized:
            # Hide existence
            raise _error(
                status.HTTP_404_NOT_FOUND,
                "NOT_FOUND",
                "Order not found",
            )
    else:
        raise _error(
            status.HTTP_403_FORBIDDEN,
            "FORBIDDEN",
            "Only customer accounts can access orders via this API for now",
        )

    payment_status_mapped = _map_payment_status(order_data.get("payment_status"))
    fulfillment_status_mapped = _map_fulfillment_status(
        order_data.get("fulfillment_status")
    )
    delivery_status = _derive_delivery_status(
        order_data.get("fulfillment_status"), order_data.get("tracking_number")
    )
    status_summary = _derive_order_status(
        payment_status_mapped,
        fulfillment_status_mapped,
        cancelled=(order_data.get("status") == "cancelled"),
        refunded=(order_data.get("status") == "refunded"),
    )

    shipping = order_data.get("shipping_address") or {}
    items = order_data.get("items") or []
    metadata = order_data.get("metadata") or {}
    creator_id = None
    creator_name = None
    creator_slug = None
    try:
        if isinstance(metadata, dict):
            creator_id = metadata.get("creator_id") or metadata.get("creatorId")
            creator_name = metadata.get("creator_name") or metadata.get("creatorName")
            creator_slug = metadata.get("creator_slug") or metadata.get("creatorSlug")
    except Exception:
        creator_id = None
        creator_name = None
        creator_slug = None

    # Payments (from payments table, if present)
    payment_records: List[Dict[str, Any]] = []
    try:
        payment_rows = await database.fetch_all(
            """
            SELECT payment_id, payment_intent_id, amount, currency, psp_type, status
            FROM payments
            WHERE order_id = :order_id
            ORDER BY created_at ASC
            """,
            {"order_id": order_id},
        )
        for pr in payment_rows:
            pr_status = pr["status"]
            # Best-effort: if the order is already marked paid but the raw payment
            # record is still "processing", surface it as succeeded in the
            # customer-facing API to avoid confusing UI states.
            try:
                if (
                    str(pr_status or "").lower() in {"processing", "requires_action"}
                    and payment_status_mapped == "paid"
                ):
                    pr_status = "succeeded"
            except Exception:
                pass
            payment_records.append(
                {
                    "payment_id": pr["payment_id"],
                    "provider": pr["psp_type"],
                    "amount_minor": _amount_to_minor(pr["amount"]),
                    "currency": pr["currency"],
                    "status": pr_status,
                    "payment_intent_id": pr["payment_intent_id"],
                }
            )
    except Exception:
        payment_records = []

    # Fulfillment / shipments (best-effort, derived from tracking fields)
    shipments: List[Dict[str, Any]] = []
    if order_data.get("tracking_number"):
        shipments.append(
            {
                "tracking_number": order_data.get("tracking_number"),
                "carrier": order_data.get("carrier"),
                "status": delivery_status,
                "estimated_delivery": None,
                "events": [],
            }
        )

    customer_info = {
        "email": order_data.get("customer_email"),
        "phone": (shipping.get("phone") or ""),
        "name": shipping.get("name"),
    }

    response_payload = {
        "order": {
            "order_id": order_data["order_id"],
            "merchant_id": order_data["merchant_id"],
            "currency": order_data.get("currency", "USD"),
            "total_amount_minor": _amount_to_minor(order_data.get("total")),
            "status": status_summary,
            "payment_status": payment_status_mapped,
            "fulfillment_status": fulfillment_status_mapped,
            "delivery_status": delivery_status,
            "created_at": (order_data.get("created_at") or datetime.now(timezone.utc)).isoformat(),
            "updated_at": (order_data.get("updated_at") or datetime.now(timezone.utc)).isoformat(),
            "creator_id": creator_id,
            "creator_name": creator_name,
            "creator_slug": creator_slug,
            "shipping_address": {
                "name": shipping.get("name"),
                "city": shipping.get("city"),
                "country": shipping.get("country"),
                "postal_code": shipping.get("postal_code"),
            },
        },
        "items": [
            {
                "product_id": it.get("product_id"),
                "title": it.get("product_title") or it.get("title"),
                "quantity": it.get("quantity", 1),
                "unit_price_minor": _amount_to_minor(it.get("unit_price")),
                "subtotal_minor": _amount_to_minor(it.get("subtotal")),
                "sku": it.get("sku"),
                "merchant_id": order_data["merchant_id"],
            }
            for it in items
        ],
        "payment": {"records": payment_records},
        "fulfillment": {"shipments": shipments},
        "customer": customer_info,
        "permissions": _compute_permissions(order_data, principal),
    }

    return response_payload


@router.post("/orders/{order_id}/cancel", response_model=CancelOrderResponse)
async def cancel_order(
    order_id: str,
    payload: CancelOrderRequest = Body(default=None),
    principal: AccountsPrincipal = Depends(get_accounts_principal),
):
    """
    Cancel an order for the logged-in customer.

    Rules:
    - Only the customer who placed the order can cancel via this API.
    - Only pending & not-fulfilled orders can be cancelled.
    - Orders already cancelled/refunded/fulfilled return INVALID_STATE.
    """
    try:
        # Look up order
        order = await database.fetch_one(
            orders_table.select().where(orders_table.c.order_id == order_id)
        )
        if not order:
            # Hide existence by default
            raise _error(
                status.HTTP_404_NOT_FOUND,
                "NOT_FOUND",
                "Order not found",
            )

        order_data = dict(order)

        # Access control: only the customer who placed the order
        if principal.primary_role == "customer":
            if normalize_email(order_data.get("customer_email", "")) != principal.email_normalized:
                # Hide existence from other customers
                raise _error(
                    status.HTTP_404_NOT_FOUND,
                    "NOT_FOUND",
                    "Order not found",
                )
        else:
            # Accounts API currently only supports customer cancellations
            raise _error(
                status.HTTP_403_FORBIDDEN,
                "FORBIDDEN",
                "Only customer accounts can cancel orders via this API for now",
            )

        # Derive current statuses
        payment_status_mapped = _map_payment_status(order_data.get("payment_status"))
        fulfillment_status_mapped = _map_fulfillment_status(
            order_data.get("fulfillment_status")
        )

        # Check current state
        is_already_cancelled = (order_data.get("status") == "cancelled")
        is_refunded = (order_data.get("status") == "refunded")

        if is_already_cancelled or is_refunded:
            raise _error(
                status.HTTP_400_BAD_REQUEST,
                "INVALID_STATE",
                "Order is already cancelled or refunded",
            )

        # Only allow cancellation when payment is still pending and nothing fulfilled
        if not (
            payment_status_mapped == "pending"
            and fulfillment_status_mapped == "not_fulfilled"
        ):
            raise _error(
                status.HTTP_400_BAD_REQUEST,
                "INVALID_STATE",
                "Order cannot be cancelled in its current state",
            )

        # Build update payload
        update_values: Dict[str, Any] = {
            "status": "cancelled",
            "cancelled_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }

        # Attach cancel reason into metadata if provided
        if payload and payload.reason:
            try:
                metadata = order_data.get("metadata") or {}
                # Avoid overwriting existing reasons; keep last one
                metadata["cancel_reason"] = payload.reason
                update_values["metadata"] = metadata
            except Exception:
                # Metadata failure should not block cancellation
                pass

        await database.execute(
            orders_table.update()
            .where(
                and_(
                    orders_table.c.order_id == order_id,
                    orders_table.c.is_deleted == False,  # noqa: E712
                )
            )
            .values(**update_values)
        )

        # Recompute derived fields for response
        delivery_status = _derive_delivery_status(
            order_data.get("fulfillment_status"), order_data.get("tracking_number")
        )
        updated_at = update_values["updated_at"].isoformat()

        return CancelOrderResponse(
            order_id=order_id,
            status="cancelled",
            payment_status=payment_status_mapped,
            fulfillment_status=fulfillment_status_mapped,
            delivery_status=delivery_status,
            updated_at=updated_at,
        )
    except HTTPException:
        raise
    except Exception as e:
        from utils.logger import logger as app_logger

        app_logger.error(f"[AccountsOrders] cancel_order failed: {type(e).__name__}: {e}")
        raise _error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "SERVER_ERROR",
            "cancel_order failed",
        )


# ---------------------------------------------------------------------------
# Public order lookup / tracking
# ---------------------------------------------------------------------------

@router.get("/public/order-lookup", response_model=PublicOrderLookupResponse)
async def public_order_lookup(
    request: Request,
    order_id: str = Query(...),
    email: EmailStr = Query(...),
):
    ip = _get_client_ip(request)
    norm_email = normalize_email(str(email))

    # Rate limits
    ip_count = await count_recent_public_lookup_by_ip(ip)
    if ip_count > PUBLIC_LOOKUP_IP_LIMIT_PER_MINUTE:
        raise _error(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "RATE_LIMITED",
            "Too many requests from this IP. Please try again later.",
        )
    pair_count = await count_recent_public_lookup_by_key(norm_email, order_id)
    if pair_count > PUBLIC_LOOKUP_PAIR_LIMIT_PER_MINUTE:
        raise _error(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "RATE_LIMITED",
            "Too many requests for this order. Please try again later.",
        )

    # Lookup order
    order = await database.fetch_one(
        orders_table.select().where(orders_table.c.order_id == order_id)
    )
    if not order:
        raise _error(
            status.HTTP_404_NOT_FOUND,
            "NOT_FOUND",
            "Order not found or email mismatch",
        )

    order_data = dict(order)
    if normalize_email(order_data.get("customer_email", "")) != norm_email:
        raise _error(
            status.HTTP_404_NOT_FOUND,
            "NOT_FOUND",
            "Order not found or email mismatch",
        )

    await record_public_lookup(ip, norm_email, order_id)

    payment_status_mapped = _map_payment_status(order_data.get("payment_status"))
    fulfillment_status_mapped = _map_fulfillment_status(
        order_data.get("fulfillment_status")
    )
    delivery_status = _derive_delivery_status(
        order_data.get("fulfillment_status"), order_data.get("tracking_number")
    )
    status_summary = _derive_order_status(
        payment_status_mapped,
        fulfillment_status_mapped,
        cancelled=(order_data.get("status") == "cancelled"),
        refunded=(order_data.get("status") == "refunded"),
    )

    shipping = order_data.get("shipping_address") or {}
    items = order_data.get("items") or []

    return PublicOrderLookupResponse(
        order_id=order_id,
        status=status_summary,
        currency=order_data.get("currency", "USD"),
        total_amount_minor=_amount_to_minor(order_data.get("total")),
        created_at=(order_data.get("created_at") or datetime.now(timezone.utc)).isoformat(),
        items_summary=_build_items_summary(items),
        shipping={
            "city": shipping.get("city"),
            "country": shipping.get("country"),
        },
        customer={
            "name": (shipping.get("name") or ""),
            "masked_email": _mask_email(order_data.get("customer_email", "")),
        },
    )


@router.get("/public/track", response_model=PublicTrackResponse)
async def public_track(
    request: Request,
    order_id: str = Query(...),
    email: EmailStr = Query(...),
):
    ip = _get_client_ip(request)
    norm_email = normalize_email(str(email))

    # Rate limits
    ip_count = await count_recent_public_lookup_by_ip(ip)
    if ip_count > PUBLIC_LOOKUP_IP_LIMIT_PER_MINUTE:
        raise _error(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "RATE_LIMITED",
            "Too many requests from this IP. Please try again later.",
        )
    pair_count = await count_recent_public_lookup_by_key(norm_email, order_id)
    if pair_count > PUBLIC_LOOKUP_PAIR_LIMIT_PER_MINUTE:
        raise _error(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "RATE_LIMITED",
            "Too many requests for this order. Please try again later.",
        )

    order = await database.fetch_one(
        orders_table.select().where(orders_table.c.order_id == order_id)
    )
    if not order:
        raise _error(
            status.HTTP_404_NOT_FOUND,
            "NOT_FOUND",
            "Order not found or email mismatch",
        )

    order_data = dict(order)
    if normalize_email(order_data.get("customer_email", "")) != norm_email:
        raise _error(
            status.HTTP_404_NOT_FOUND,
            "NOT_FOUND",
            "Order not found or email mismatch",
        )

    await record_public_lookup(ip, norm_email, order_id)

    payment_status_mapped = _map_payment_status(order_data.get("payment_status"))
    fulfillment_status_mapped = _map_fulfillment_status(
        order_data.get("fulfillment_status")
    )
    delivery_status = _derive_delivery_status(
        order_data.get("fulfillment_status"), order_data.get("tracking_number")
    )

    timeline: List[PublicTrackEvent] = []
    created_at = order_data.get("created_at") or datetime.now(timezone.utc)
    timeline.append(
        PublicTrackEvent(
            status="ordered", timestamp=created_at.isoformat(), completed=True
        )
    )
    if payment_status_mapped == "paid":
        paid_at = order_data.get("paid_at") or created_at
        timeline.append(
            PublicTrackEvent(
                status="paid", timestamp=paid_at.isoformat(), completed=True
            )
        )
    if fulfillment_status_mapped in {"partially_fulfilled", "fulfilled"}:
        shipped_at = order_data.get("shipped_at") or created_at
        timeline.append(
            PublicTrackEvent(
                status="shipped", timestamp=shipped_at.isoformat(), completed=True
            )
        )
    if delivery_status == "delivered":
        delivered_at = order_data.get("delivered_at") or order_data.get("shipped_at") or created_at
        timeline.append(
            PublicTrackEvent(
                status="delivered",
                timestamp=delivered_at.isoformat(),
                completed=True,
            )
        )

    return PublicTrackResponse(
        order_id=order_id,
        delivery_status=delivery_status,
        timeline=timeline,
    )
