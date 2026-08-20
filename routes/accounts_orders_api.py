"""
Accounts & Orders API

Customer-facing authentication + "My Orders" + public order lookup/track.
This sits alongside the existing employee/merchant auth system and does not
change any of the legacy /api/auth or /auth/* endpoints.

Paths implemented here follow the contract in:
  pivota-agent-frontend/docs/accounts-orders-api.md
"""

from __future__ import annotations

import os
import uuid
import json
import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import asyncio
import httpx
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
    shop_user_passwords,
    shop_login_otps,
    public_order_lookup_logs,
    shop_browse_history_events,
    normalize_email,
    create_or_get_shop_user,
    sync_customer_auth_membership,
    record_public_lookup,
    count_recent_public_lookup_by_ip,
    count_recent_public_lookup_by_key,
)
from db.orders import orders as orders_table
from db.products import products_cache
from utils.auth import create_access_token, decode_token, hash_password, verify_password
from utils.order_track_token import verify_order_track_token
from utils.database_readiness import connect_database_with_timeout
from utils.transient_errors import db_busy_http_exception, is_asyncpg_busy_error
from services.ugc_capabilities_service import (
    UgcSubject,
    get_review_slot_summary,
    get_user_review_for_subject,
    is_question_rate_limited,
)
from services.merchant_payment_initiation_service import build_payment_action
from services.merchant_psp_config_service import (
    build_runtime_adapter_kwargs,
    fetch_active_runtime_merchant_psp,
)
from services.external_seed_search import (
    build_seed_quarantine_anti_join as _seed_quarantine_clause,
)
from services.refund_observability import build_order_refund_tracking_payload


router = APIRouter(prefix="/accounts", tags=["accounts-orders"])
logger = logging.getLogger("accounts_orders")


# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

ACCESS_COOKIE_NAME = "acc_access_token"
REFRESH_COOKIE_NAME = "acc_refresh_token"
DEFAULT_AURORA_BFF_BASE = "https://gateway.pivota.cc"
AURORA_BFF_BASE = str(os.getenv("AURORA_BFF_BASE", DEFAULT_AURORA_BFF_BASE) or DEFAULT_AURORA_BFF_BASE).strip().rstrip("/")
try:
    _aurora_timeout_ms = int(float(str(os.getenv("AURORA_BFF_TIMEOUT_MS", "5000"))))
except Exception:
    _aurora_timeout_ms = 5000
AURORA_BFF_TIMEOUT_MS = max(1000, min(15000, _aurora_timeout_ms))
AURORA_BFF_AUTH_ME_PATH = "/v1/auth/me"

# Access token lifetime for accounts UI sessions.
# Previously this was 30 minutes, which caused shoppers to be logged out
# during longer checkout flows. For the Shopping Agent / developer portal
# we extend this to a full 7 days to match the refresh window.
ACCESS_EXPIRE_MINUTES = 7 * 24 * 60  # 7 days
# Refresh token lifetime: rolling 7-day window
REFRESH_EXPIRE_DAYS = 7

PUBLIC_LOOKUP_IP_LIMIT_PER_MINUTE = 10
PUBLIC_LOOKUP_PAIR_LIMIT_PER_MINUTE = 3
_browse_history_schema_ready = False
_browse_history_schema_lock = asyncio.Lock()


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
        await connect_database_with_timeout(3, db=database)
    except Exception as exc:
        logger.warning(f"Database not available for request (connect failed): {exc}")
        raise _error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "TEMPORARY_UNAVAILABLE",
            "Temporary database unavailable. Please retry shortly.",
        )


async def _ensure_browse_history_schema() -> None:
    global _browse_history_schema_ready

    if _browse_history_schema_ready:
        return

    async with _browse_history_schema_lock:
        if _browse_history_schema_ready:
            return
        statements = [
            "ALTER TABLE shop_browse_history_events ADD COLUMN IF NOT EXISTS brand TEXT;",
            "ALTER TABLE shop_browse_history_events ADD COLUMN IF NOT EXISTS category TEXT;",
            "ALTER TABLE shop_browse_history_events ADD COLUMN IF NOT EXISTS product_type TEXT;",
        ]
        for statement in statements:
            await database.execute(statement)
        _browse_history_schema_ready = True


async def _mark_email_verified_best_effort(user_id: str) -> None:
    """
    Best-effort email verification marker for Buyer Vault.

    We keep this tolerant of schema drift: environments that haven't yet applied
    the migration will auto-add the column and proceed.
    """
    uid = str(user_id or "").strip()
    if not uid:
        return
    try:
        await database.execute(
            "ALTER TABLE shop_users ADD COLUMN IF NOT EXISTS email_verified_at TIMESTAMPTZ"
        )
        await database.execute(
            "UPDATE shop_users SET email_verified_at = NOW() WHERE id = :id AND email_verified_at IS NULL",
            {"id": uid},
        )
    except Exception:
        return


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


def _pricing_quote_pricing(order_data: Dict[str, Any]) -> Dict[str, Any]:
    metadata = _coerce_json_object(order_data.get("metadata"))
    pricing_quote = _coerce_json_object(metadata.get("pricing_quote"))
    return _coerce_json_object(pricing_quote.get("pricing"))


def _extract_order_pricing_minor(order_data: Dict[str, Any], items: List[Dict[str, Any]]) -> Dict[str, int]:
    pricing = _pricing_quote_pricing(order_data)

    subtotal_minor = (
        _amount_to_minor(order_data.get("subtotal"))
        or _amount_to_minor(pricing.get("subtotal"))
    )
    discount_total_minor = (
        _amount_to_minor(order_data.get("discount_total"))
        or _amount_to_minor(pricing.get("discount_total"))
    )
    shipping_fee_minor = (
        _amount_to_minor(order_data.get("shipping_fee"))
        or _amount_to_minor(pricing.get("shipping_fee"))
    )
    tax_minor = _amount_to_minor(order_data.get("tax")) or _amount_to_minor(pricing.get("tax"))
    total_amount_minor = (
        _amount_to_minor(order_data.get("total"))
        or _amount_to_minor(pricing.get("total"))
        or max(0, subtotal_minor - discount_total_minor) + shipping_fee_minor + tax_minor
    )

    return {
        "subtotal_minor": subtotal_minor,
        "discount_total_minor": discount_total_minor,
        "shipping_fee_minor": shipping_fee_minor,
        "tax_minor": tax_minor,
        "total_amount_minor": total_amount_minor,
    }


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
    amr: Optional[str] = None
    iat: Optional[int] = None
    auth_time: Optional[int] = None


async def get_accounts_principal(request: Request) -> AccountsPrincipal:
    """Dependency that reads the accounts access token cookie and returns principal."""
    token = request.cookies.get(ACCESS_COOKIE_NAME)
    if not token:
        raise _error(status.HTTP_401_UNAUTHORIZED, "UNAUTHENTICATED", "Not logged in")

    try:
        payload = decode_token(token)
    except HTTPException:
        raise _error(status.HTTP_401_UNAUTHORIZED, "UNAUTHENTICATED", "Invalid or expired session")

    sub = (
        payload.get("customer_user_id")
        or payload.get("user_id")
        or payload.get("sub")
    )
    email = payload.get("email")
    role = payload.get("role", "customer")
    membership_type = str(payload.get("membership_type") or "").strip().lower()
    amr = payload.get("amr")
    raw_iat = payload.get("iat")
    raw_auth_time = payload.get("auth_time")
    iat: Optional[int] = None
    auth_time: Optional[int] = None
    try:
        if isinstance(raw_iat, (int, float)):
            iat = int(raw_iat)
        elif isinstance(raw_iat, str) and raw_iat.strip().isdigit():
            iat = int(raw_iat.strip())
    except Exception:
        iat = None
    try:
        if isinstance(raw_auth_time, (int, float)):
            auth_time = int(raw_auth_time)
        elif isinstance(raw_auth_time, str) and raw_auth_time.strip().isdigit():
            auth_time = int(raw_auth_time.strip())
    except Exception:
        auth_time = None
    if membership_type and membership_type != "customer":
        raise _error(status.HTTP_401_UNAUTHORIZED, "UNAUTHENTICATED", "Invalid account session")
    if not sub or not email:
        raise _error(status.HTTP_401_UNAUTHORIZED, "UNAUTHENTICATED", "Invalid token payload")

    norm = normalize_email(email)
    return AccountsPrincipal(
        user_id=sub,
        email=email,
        email_normalized=norm,
        primary_role=role,
        amr=amr,
        iat=iat,
        auth_time=auth_time,
    )


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


def _ugc_guest_principal(request: Request) -> AccountsPrincipal:
    raw_guest_id = (request.headers.get("x-pivota-ugc-guest-id") or "").strip()
    if raw_guest_id:
        seed = f"client:{raw_guest_id[:128]}"
    else:
        client_host = getattr(request.client, "host", "") if request.client else ""
        user_agent = (request.headers.get("user-agent") or "").strip()
        seed = f"request:{client_host}|{user_agent}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]
    return AccountsPrincipal(
        user_id=f"guest:{digest}",
        email="",
        email_normalized="",
        primary_role="guest",
        amr="guest",
    )


async def get_accounts_or_guest_principal_ugc(request: Request) -> AccountsPrincipal:
    """
    UGC write endpoints are open to non-purchasers and logged-out visitors.
    Logged-in users keep their accounts identity; otherwise we use a stable
    guest actor for rate limiting and review/media ownership.
    """
    try:
        return await get_accounts_principal(request)
    except HTTPException:
        return _ugc_guest_principal(request)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


def _send_login_otp_email(email: str, otp_code: str):
    """
    Email sender for login OTP codes.

    Provider is selected by utils.email_sender:
    - EMAIL_PROVIDER override (`ses` | `sendgrid` | `smtp2go`)
    - Otherwise SMTP2GO when `SMTP2GO_EMAIL_API_KEY` or `SMTP2GO_API_KEY` exists
    - Otherwise SendGrid when `SENDGRID_API_KEY` exists, else SES.
    Returns an EmailSendResult.
    """
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

    from utils.email_sender import send_email

    return send_email(
        to_email=email,
        subject=subject,
        text_body=text_content,
        html_body=html_content,
        from_email=from_email,
        from_name="Pivota",
        tags={"type": "accounts_login_otp"},
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

class PasswordLoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=1, max_length=1024)


class PasswordSetRequest(BaseModel):
    new_password: str = Field(..., min_length=1, max_length=1024)
    current_password: Optional[str] = Field(None, min_length=1, max_length=1024)

class AuroraExchangeRequest(BaseModel):
    aurora_token: Optional[str] = Field(None, max_length=4096)
    aurora_uid: Optional[str] = Field(None, max_length=256)

    @validator("aurora_token", "aurora_uid")
    def trim_optional_str(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        trimmed = v.strip()
        return trimmed or None


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
    refund_status: Optional[str] = None
    total_refunded_minor: int = 0
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


class OrderPricingResponse(BaseModel):
    subtotal_minor: int
    discount_total_minor: int
    shipping_fee_minor: int
    tax_minor: int
    total_amount_minor: int


class PublicOrderLookupResponse(BaseModel):
    order_id: str
    status: str
    currency: str
    total_amount_minor: int
    pricing: OrderPricingResponse
    created_at: str
    items_summary: str
    shipping: Dict[str, Optional[str]]
    customer: Dict[str, str]


class PublicOrderResumeResponse(BaseModel):
    order: Dict[str, Any]
    pricing_quote: Dict[str, Any]
    items: List[Dict[str, Any]]
    payment: Dict[str, Any]
    refund: Dict[str, Any]
    customer: Dict[str, Any]
    permissions: Dict[str, bool]


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


class RefundOrderItemRequest(BaseModel):
    item_id: Optional[str] = Field(default=None, max_length=255)
    quantity: Optional[int] = Field(default=None, ge=1)
    amount_minor: Optional[int] = Field(default=None, ge=1)


class RefundOrderRequest(BaseModel):
    amount_minor: Optional[int] = Field(default=None, ge=1)
    amount: Optional[float] = Field(default=None, gt=0)
    currency: Optional[str] = Field(default=None, min_length=3, max_length=8)
    reason: Optional[str] = Field(default=None, max_length=500)
    items: Optional[List[RefundOrderItemRequest]] = None

    @validator("currency")
    def normalize_currency(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        trimmed = v.strip().upper()
        return trimmed or None

    @validator("reason")
    def normalize_reason(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        trimmed = v.strip()
        return trimmed or None


class RefundOrderResponse(BaseModel):
    order_id: str
    refund_status: str
    case_id: Optional[str] = None
    updated_at: str
    total_refunded_minor: int
    currency: str


class BrowseHistoryEventRequest(BaseModel):
    product_id: str = Field(..., min_length=1, max_length=255)
    merchant_id: Optional[str] = Field(default=None, max_length=255)
    title: Optional[str] = Field(default=None, max_length=1000)
    price: Optional[float] = Field(default=None, ge=0)
    currency: Optional[str] = Field(default=None, min_length=1, max_length=16)
    image_url: Optional[str] = Field(default=None, max_length=4096)
    description: Optional[str] = Field(default=None, max_length=4000)
    brand: Optional[str] = Field(default=None, max_length=255)
    category: Optional[str] = Field(default=None, max_length=255)
    product_type: Optional[str] = Field(default=None, max_length=255)
    viewed_at: Optional[str] = Field(default=None, max_length=64)

    @validator("product_id")
    def normalize_product_id(cls, v: str) -> str:
        return v.strip()

    @validator(
        "merchant_id",
        "title",
        "currency",
        "image_url",
        "description",
        "brand",
        "category",
        "product_type",
        "viewed_at",
    )
    def normalize_optional_fields(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        trimmed = v.strip()
        return trimmed or None


class BrowseHistoryItem(BaseModel):
    product_id: str
    merchant_id: Optional[str] = None
    title: str
    price: float
    currency: str
    image_url: str
    description: Optional[str] = None
    brand: Optional[str] = None
    category: Optional[str] = None
    product_type: Optional[str] = None
    price_source: Optional[str] = None
    price_resolution_status: str = "stored"
    timestamp: int
    viewed_at: str


class BrowseHistoryListResponse(BaseModel):
    items: List[BrowseHistoryItem]
    total: int
    unresolved_total: int = 0
    price_source_counts: Dict[str, int] = Field(default_factory=dict)


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

    # Password status
    has_password = False
    try:
        pw_row = await database.fetch_one(
            shop_user_passwords.select().where(shop_user_passwords.c.user_id == user_id)
        )
        has_password = bool(pw_row)
    except Exception:
        has_password = False

    user_payload = {
        "id": user_id,
        "email": email,
        "phone": user_row.get("phone"),
        "primary_role": primary_role,
        "is_guest": bool(user_row.get("is_guest")),
        "has_password": has_password,
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
    amr: Optional[str] = None,
    auth_time: Optional[int] = None,
    identity_id: Optional[str] = None,
) -> None:
    """Set access + refresh cookies for accounts API."""
    subject = identity_id or user_id
    base_payload = {
        "sub": subject,
        "user_id": user_id,
        "customer_user_id": user_id,
        "identity_id": identity_id,
        "email": email,
        "role": primary_role,
        "scope": "accounts",
        "membership_type": "customer",
        "aud": "accounts",
    }
    if amr:
        base_payload["amr"] = amr
    if auth_time is not None:
        base_payload["auth_time"] = auth_time
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


def _identity_id_from_membership(membership: Optional[dict]) -> Optional[str]:
    if not membership:
        return None
    identity_id = membership.get("identity_id")
    if identity_id:
        return str(identity_id)
    identity = membership.get("identity")
    if isinstance(identity, dict) and identity.get("identity_id"):
        return str(identity["identity_id"])
    return None


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


def _resolve_aurora_email_from_auth_me_payload(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None

    cards = payload.get("cards")
    if isinstance(cards, list):
        for card in cards:
            if not isinstance(card, dict):
                continue
            if str(card.get("type") or "").strip() != "auth_me":
                continue
            card_payload = card.get("payload")
            if not isinstance(card_payload, dict):
                continue
            user = card_payload.get("user")
            if not isinstance(user, dict):
                continue
            email = str(user.get("email") or "").strip().lower()
            if email:
                return email

    fallback_user = payload.get("user")
    if isinstance(fallback_user, dict):
        email = str(fallback_user.get("email") or "").strip().lower()
        if email:
            return email
    email = str(payload.get("email") or "").strip().lower()
    if email:
        return email
    return None


async def _fetch_aurora_auth_me(*, aurora_token: str, aurora_uid: str) -> Dict[str, Any]:
    trace_id = f"accounts_aurora_exchange_{uuid.uuid4().hex[:16]}"
    headers = {
        "Authorization": f"Bearer {aurora_token}",
        "X-Aurora-UID": aurora_uid,
        "X-Lang": "EN",
        "X-Trace-ID": trace_id,
        "Accept": "application/json",
    }
    url = f"{AURORA_BFF_BASE}{AURORA_BFF_AUTH_ME_PATH}"
    timeout_s = max(1.0, AURORA_BFF_TIMEOUT_MS / 1000.0)

    try:
        async with httpx.AsyncClient(timeout=timeout_s, follow_redirects=False) as client:
            res = await client.get(url, headers=headers)
    except Exception as exc:
        logger.warning("[AccountsAuroraExchange] upstream request failed err=%s", type(exc).__name__)
        raise _error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "TEMPORARY_UNAVAILABLE",
            "Aurora auth upstream is temporarily unavailable",
        ) from exc

    payload = None
    try:
        payload = res.json()
    except Exception:
        payload = None

    if res.status_code == status.HTTP_401_UNAUTHORIZED:
        raise _error(
            status.HTTP_401_UNAUTHORIZED,
            "INVALID_AURORA_SESSION",
            "Aurora session is invalid or expired",
        )

    if res.status_code < 200 or res.status_code >= 300:
        logger.warning("[AccountsAuroraExchange] upstream non-2xx status=%s", res.status_code)
        raise _error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "TEMPORARY_UNAVAILABLE",
            "Aurora auth upstream is temporarily unavailable",
        )

    if not isinstance(payload, dict):
        raise _error(
            status.HTTP_502_BAD_GATEWAY,
            "BAD_UPSTREAM_PAYLOAD",
            "Aurora auth upstream returned an invalid payload",
        )

    return payload


def _map_payment_status(raw_status: Optional[str]) -> str:
    raw = (raw_status or "").lower()
    if raw in {"paid", "succeeded", "completed", "success", "settled"}:
        return "paid"
    if raw in {"refunded"}:
        return "refunded"
    if raw in {"partially_refunded", "partial_refund"}:
        return "partially_refunded"
    if raw in {"payment_failed", "failed"}:
        return "payment_failed"
    if raw in {"cancelled", "canceled"}:
        return "cancelled"
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
    payment_status: str,
    fulfillment_status: str,
    cancelled: bool,
    refunded: bool,
    partially_refunded: bool = False,
) -> str:
    if refunded:
        return "refunded"
    if partially_refunded:
        return "partially_refunded"
    if cancelled:
        return "cancelled"
    if payment_status == "cancelled":
        return "cancelled"
    if payment_status == "payment_failed":
        return "payment_failed"
    if payment_status == "paid" and fulfillment_status == "fulfilled":
        return "completed"
    if payment_status == "paid" and fulfillment_status != "fulfilled":
        return "paid"
    return "pending"


def _derive_refund_status(order_row: Dict[str, Any], metadata: Dict[str, Any], status_summary: str) -> str:
    refund_status = str(metadata.get("refund_status") or "").strip().lower()
    if refund_status:
        return refund_status

    raw_status = str(order_row.get("status") or "").strip().lower()
    raw_payment_status = str(order_row.get("payment_status") or "").strip().lower()
    total_refunded_minor = _amount_to_minor(order_row.get("total_refunded"))
    total_minor = _amount_to_minor(order_row.get("total"))

    if raw_status == "refunded" or raw_payment_status == "refunded" or status_summary == "refunded":
        return "refunded"
    if (
        raw_status == "partially_refunded"
        or raw_payment_status == "partially_refunded"
        or status_summary == "partially_refunded"
        or (total_refunded_minor > 0 and (total_minor <= 0 or total_refunded_minor < total_minor))
    ):
        return "partially_refunded"
    if total_refunded_minor > 0 and total_refunded_minor >= total_minor > 0:
        return "refunded"
    return "none"


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


def _ensure_customer_order_access(order_data: Dict[str, Any], principal: AccountsPrincipal) -> None:
    if principal.primary_role == "customer":
        if normalize_email(order_data.get("customer_email", "")) != principal.email_normalized:
            raise _error(
                status.HTTP_404_NOT_FOUND,
                "NOT_FOUND",
                "Order not found",
            )
        return

    raise _error(
        status.HTTP_403_FORBIDDEN,
        "FORBIDDEN",
        "Only customer accounts can access orders via this API for now",
    )


async def _load_public_order_for_customer(
    request: Request,
    *,
    order_id: str,
    email: EmailStr,
) -> Dict[str, Any]:
    ip = await _enforce_public_lookup_ip_limit(request)
    norm_email = normalize_email(str(email))

    pair_count = await count_recent_public_lookup_by_key(norm_email, order_id)
    if pair_count > PUBLIC_LOOKUP_PAIR_LIMIT_PER_MINUTE:
        raise _error(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "RATE_LIMITED",
            "Too many requests for this order. Please try again later.",
        )

    order_data = await _load_public_order_by_id(order_id)
    if normalize_email(order_data.get("customer_email", "")) != norm_email:
        raise _public_order_not_found()

    await record_public_lookup(ip, norm_email, order_id)
    return order_data


def _public_order_not_found() -> HTTPException:
    return _error(
        status.HTTP_404_NOT_FOUND,
        "NOT_FOUND",
        "Order not found or email mismatch",
    )


async def _enforce_public_lookup_ip_limit(request: Request) -> str:
    ip = _get_client_ip(request)
    ip_count = await count_recent_public_lookup_by_ip(ip)
    if ip_count > PUBLIC_LOOKUP_IP_LIMIT_PER_MINUTE:
        raise _error(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "RATE_LIMITED",
            "Too many requests from this IP. Please try again later.",
        )
    return ip


async def _load_public_order_by_id(order_id: str) -> Dict[str, Any]:
    order = await database.fetch_one(
        orders_table.select().where(orders_table.c.order_id == order_id)
    )
    if not order:
        raise _public_order_not_found()

    return dict(order)


async def _load_public_order_for_track_token(
    request: Request,
    *,
    token: str,
) -> Dict[str, Any]:
    ip = await _enforce_public_lookup_ip_limit(request)
    order_id = verify_order_track_token(token)
    if not order_id:
        raise _public_order_not_found()

    order_data = await _load_public_order_by_id(order_id)
    await record_public_lookup(
        ip,
        normalize_email(order_data.get("customer_email", "")),
        order_id,
    )
    return order_data


def _coerce_json_object(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {}
    return {}


def _to_iso_string(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        trimmed = value.strip()
        if not trimmed:
            return None
        parsed = _parse_iso_datetime(trimmed)
        return parsed.isoformat() if parsed else trimmed
    return str(value)


def _extract_tracking_events_from_metadata(metadata: Dict[str, Any]) -> List[Dict[str, Any]]:
    for key in ("tracking_events", "shipping_events", "events", "tracking_timeline", "timeline"):
        raw_events = metadata.get(key)
        if not isinstance(raw_events, list):
            continue
        events: List[Dict[str, Any]] = []
        for raw_event in raw_events:
            event = _coerce_json_object(raw_event)
            if not event:
                continue
            events.append(
                {
                    "status": (
                        str(event.get("status") or event.get("state") or "update")
                    ).strip(),
                    "description": (
                        str(event.get("description") or event.get("message") or event.get("detail") or "")
                    ).strip()
                    or None,
                    "location": (
                        str(event.get("location") or event.get("city") or "")
                    ).strip()
                    or None,
                    "timestamp": _to_iso_string(
                        event.get("timestamp")
                        or event.get("occurred_at")
                        or event.get("created_at")
                    ),
                }
            )
        if events:
            return events
    return []


def _build_tracking_events(order_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    metadata = _coerce_json_object(order_data.get("metadata"))
    metadata_events = _extract_tracking_events_from_metadata(metadata)
    if metadata_events:
        return metadata_events

    created_at = order_data.get("created_at") or datetime.now(timezone.utc)
    payment_status = _map_payment_status(order_data.get("payment_status"))
    fulfillment_status = _map_fulfillment_status(order_data.get("fulfillment_status"))
    delivery_status = _derive_delivery_status(
        order_data.get("fulfillment_status"),
        order_data.get("tracking_number"),
    )
    order_status = str(order_data.get("status") or "").strip().lower()

    timeline: List[Dict[str, Any]] = [
        {
            "status": "ordered",
            "description": "Order received",
            "location": None,
            "timestamp": _to_iso_string(created_at),
        }
    ]

    if payment_status == "paid":
        timeline.append(
            {
                "status": "paid",
                "description": "Payment confirmed",
                "location": None,
                "timestamp": _to_iso_string(order_data.get("paid_at") or created_at),
            }
        )
    elif payment_status == "payment_failed":
        timeline.append(
            {
                "status": "payment_failed",
                "description": "Payment failed",
                "location": None,
                "timestamp": _to_iso_string(order_data.get("updated_at") or created_at),
            }
        )

    if fulfillment_status in {"partially_fulfilled", "fulfilled"} or order_data.get("tracking_number"):
        timeline.append(
            {
                "status": "shipped",
                "description": "Shipment in transit",
                "location": None,
                "timestamp": _to_iso_string(order_data.get("shipped_at") or created_at),
            }
        )

    if delivery_status == "delivered":
        timeline.append(
            {
                "status": "delivered",
                "description": "Delivered",
                "location": None,
                "timestamp": _to_iso_string(
                    order_data.get("delivered_at") or order_data.get("shipped_at") or created_at
                ),
            }
        )

    if order_status in {"cancelled", "canceled"}:
        timeline.append(
            {
                "status": "cancelled",
                "description": "Order cancelled",
                "location": None,
                "timestamp": _to_iso_string(order_data.get("cancelled_at") or order_data.get("updated_at") or created_at),
            }
        )
    elif order_status == "refunded":
        timeline.append(
            {
                "status": "refunded",
                "description": "Refund completed",
                "location": None,
                "timestamp": _to_iso_string(order_data.get("updated_at") or created_at),
            }
        )

    return timeline


async def _build_resumable_payment_payload(
    order_data: Dict[str, Any],
    *,
    payment_status: str,
) -> Optional[Dict[str, Any]]:
    merchant_id = str(order_data.get("merchant_id") or "").strip()
    psp_used = str(order_data.get("psp_used") or "").strip().lower()
    payment_intent_id = str(order_data.get("payment_intent_id") or "").strip()
    client_secret = str(order_data.get("client_secret") or "").strip()

    if payment_status not in {"pending", "processing", "requires_action"}:
        return None
    if not merchant_id or not psp_used or not client_secret:
        return None

    try:
        runtime_row = await fetch_active_runtime_merchant_psp(
            merchant_id=merchant_id,
            provider=psp_used,
        )
    except Exception:
        runtime_row = None
    raw_response: Dict[str, Any] = {}
    if runtime_row:
        adapter_kwargs = build_runtime_adapter_kwargs(
            psp_used,
            api_key=runtime_row.get("api_key"),
            account_id=runtime_row.get("account_id"),
            provider_config=runtime_row.get("provider_config"),
            environment=runtime_row.get("environment"),
            secret_key=runtime_row.get("secret_key"),
        )
        if psp_used == "stripe":
            if adapter_kwargs.get("public_key"):
                raw_response["public_key"] = adapter_kwargs["public_key"]
            if adapter_kwargs.get("account_id"):
                raw_response["stripe_account"] = adapter_kwargs["account_id"]
        elif psp_used == "adyen":
            if adapter_kwargs.get("client_key"):
                raw_response["clientKey"] = adapter_kwargs["client_key"]
        elif psp_used == "checkout":
            if adapter_kwargs.get("public_key"):
                raw_response["public_key"] = adapter_kwargs["public_key"]
            if adapter_kwargs.get("processing_channel_id"):
                raw_response["processing_channel_id"] = adapter_kwargs["processing_channel_id"]

    payment_intent = SimpleNamespace(
        id=payment_intent_id or None,
        client_secret=client_secret,
        raw_response=raw_response,
        psp_type=psp_used,
    )
    payment_action = build_payment_action(payment_intent, psp_used=psp_used)
    if not payment_action or not payment_action.get("type"):
        return None

    return {
        "psp": psp_used,
        "payment_intent_id": payment_intent_id or None,
        "payment_action": payment_action,
        "status": payment_status,
    }


def _extract_tracking_url(order_data: Dict[str, Any]) -> Optional[str]:
    shipping = _coerce_json_object(order_data.get("shipping_address"))
    metadata = _coerce_json_object(order_data.get("metadata"))
    metadata_tracking = _coerce_json_object(metadata.get("tracking"))
    metadata_shipment = _coerce_json_object(metadata.get("shipment"))
    candidates = [
        order_data.get("tracking_url"),
        shipping.get("tracking_url"),
        shipping.get("trackingUrl"),
        metadata.get("tracking_url"),
        metadata.get("trackingUrl"),
        metadata_tracking.get("tracking_url"),
        metadata_tracking.get("trackingUrl"),
        metadata_tracking.get("url"),
        metadata_shipment.get("tracking_url"),
        metadata_shipment.get("trackingUrl"),
        metadata_shipment.get("url"),
    ]
    for raw in candidates:
        if not isinstance(raw, str):
            continue
        url = raw.strip()
        if not url:
            continue
        return url
    return None


def _extract_pricing_quote_line_items(order_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    metadata = _coerce_json_object(order_data.get("metadata"))
    pricing_quote = _coerce_json_object(metadata.get("pricing_quote"))
    raw = pricing_quote.get("line_items")
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    return []


def _extract_pricing_quote_payload(order_data: Dict[str, Any]) -> Dict[str, Any]:
    metadata = _coerce_json_object(order_data.get("metadata"))
    pricing_quote = _coerce_json_object(metadata.get("pricing_quote"))
    pricing = _coerce_json_object(pricing_quote.get("pricing"))
    return {
        "quote_id": pricing_quote.get("quote_id"),
        "currency": pricing_quote.get("currency") or order_data.get("currency", "USD"),
        "pricing": {
            "subtotal": pricing.get("subtotal"),
            "discount_total": pricing.get("discount_total"),
            "shipping_fee": pricing.get("shipping_fee"),
            "tax": pricing.get("tax"),
            "total": pricing.get("total"),
        },
    }


def _match_pricing_quote_line_item(
    *,
    item: Dict[str, Any],
    pricing_line_items: List[Dict[str, Any]],
) -> Dict[str, Any]:
    variant_id = str(item.get("variant_id") or "").strip()
    product_id = str(item.get("product_id") or "").strip()
    if variant_id:
        for candidate in pricing_line_items:
            if str(candidate.get("variant_id") or "").strip() == variant_id:
                return candidate
    if product_id:
        for candidate in pricing_line_items:
            if str(candidate.get("product_id") or "").strip() == product_id:
                return candidate
    return {}


async def _load_order_item_display_context(
    *,
    merchant_id: Optional[str],
    product_id: Optional[str],
) -> Dict[str, Optional[str]]:
    merchant_text = str(merchant_id or "").strip()
    product_text = str(product_id or "").strip()
    if not merchant_text or not product_text:
        return {"title": None, "image_url": None}

    row = await database.fetch_one(
        select(products_cache.c.product_data)
        .where(
            and_(
                products_cache.c.merchant_id == merchant_text,
                products_cache.c.platform_product_id == product_text,
            )
        )
        .order_by(products_cache.c.cached_at.desc())
        .limit(1)
    )
    if not row:
        return {"title": None, "image_url": None}

    row_get = getattr(row, "get", None)
    product_data = row_get("product_data") if callable(row_get) else dict(row).get("product_data")
    product_json = _coerce_json_object(product_data)
    title = (
        str(
            product_json.get("title")
            or product_json.get("name")
            or product_json.get("product_title")
            or ""
        ).strip()
        or None
    )
    image_url = (
        str(
            product_json.get("image_url")
            or product_json.get("main_image_url")
            or (
                product_json.get("images")[0]
                if isinstance(product_json.get("images"), list) and product_json.get("images")
                else ""
            )
            or ""
        ).strip()
        or None
    )
    return {"title": title, "image_url": image_url}


async def _build_order_items_payload(order_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    merchant_id = order_data.get("merchant_id")
    items = order_data.get("items") or []
    pricing_line_items = _extract_pricing_quote_line_items(order_data)
    product_context_cache: Dict[str, Dict[str, Optional[str]]] = {}
    payload: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        product_id = item.get("product_id")
        product_key = str(product_id or "").strip()
        pricing_line = _match_pricing_quote_line_item(
            item=item,
            pricing_line_items=pricing_line_items,
        )
        unit_price = item.get("unit_price")
        if unit_price in {None, "", 0, 0.0, "0", "0.0", "0.00"}:
            unit_price = (
                pricing_line.get("unit_price_effective")
                or pricing_line.get("unit_price_original")
                or pricing_line.get("price")
            )
        subtotal = item.get("subtotal")
        if subtotal in {None, "", 0, 0.0, "0", "0.0", "0.00"} and unit_price not in {None, ""}:
            try:
                subtotal = float(unit_price) * max(int(item.get("quantity") or 1), 1)
            except Exception:
                subtotal = unit_price
        display_context = {"title": None, "image_url": None}
        if product_key:
            display_context = product_context_cache.get(product_key) or {"title": None, "image_url": None}
            if display_context == {"title": None, "image_url": None} and product_key not in product_context_cache:
                display_context = await _load_order_item_display_context(
                    merchant_id=str(merchant_id or "").strip() or None,
                    product_id=product_key,
                )
                product_context_cache[product_key] = display_context
        payload.append(
            {
                "product_id": product_id,
                "variant_id": item.get("variant_id"),
                "offer_id": item.get("offer_id"),
                "title": (
                    item.get("product_title")
                    or item.get("title")
                    or pricing_line.get("product_title")
                    or pricing_line.get("title")
                    or display_context.get("title")
                ),
                "quantity": item.get("quantity", 1),
                "unit_price_minor": _amount_to_minor(unit_price),
                "subtotal_minor": _amount_to_minor(subtotal),
                "sku": item.get("sku"),
                "merchant_id": merchant_id,
                "image_url": item.get("image_url") or item.get("image") or display_context.get("image_url"),
            }
        )
    return payload


def _normalize_history_merchant_id(value: Optional[str]) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def _history_item_key(product_id: str, merchant_id: Optional[str]) -> str:
    return f"{product_id}::{merchant_id or ''}"


@dataclass(frozen=True)
class HistoryPriceResolution:
    price: float
    currency: Optional[str] = None
    source: str = "unknown"


def _history_row_to_item(row: Dict[str, Any]) -> BrowseHistoryItem:
    raw_viewed_at = row.get("viewed_at") or row.get("created_at") or datetime.now(timezone.utc)
    parsed = None
    if isinstance(raw_viewed_at, datetime):
        parsed = raw_viewed_at
    elif isinstance(raw_viewed_at, str):
        parsed = _parse_iso_datetime(raw_viewed_at)
    if not parsed:
        parsed = datetime.now(timezone.utc)

    price_value = row.get("price")
    try:
        normalized_price = float(price_value) if price_value is not None else 0.0
    except Exception:
        normalized_price = 0.0

    return BrowseHistoryItem(
        product_id=str(row.get("product_id") or "").strip(),
        merchant_id=_normalize_history_merchant_id(row.get("merchant_id")),
        title=str(row.get("title") or "Untitled product").strip() or "Untitled product",
        price=max(0.0, normalized_price),
        currency=str(row.get("currency") or "USD").strip() or "USD",
        image_url=str(row.get("image_url") or "/placeholder.svg").strip() or "/placeholder.svg",
        description=(str(row.get("description") or "").strip() or None),
        brand=(str(row.get("brand") or "").strip() or None),
        category=(str(row.get("category") or "").strip() or None),
        product_type=(str(row.get("product_type") or "").strip() or None),
        price_source=(str(row.get("price_source") or "").strip() or None),
        price_resolution_status=str(row.get("price_resolution_status") or "stored").strip() or "stored",
        timestamp=int(parsed.timestamp() * 1000),
        viewed_at=parsed.isoformat(),
    )


def _coerce_positive_history_price(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, dict):
        candidates = [
            value.get("amount"),
            value.get("value"),
            value.get("price_amount"),
            value.get("price"),
            value.get("current_price"),
            value.get("sale_price"),
            value.get("min_price"),
            _coerce_json_object(value.get("current")).get("amount"),
            _coerce_json_object(value.get("current")).get("value"),
            _coerce_json_object(value.get("sale")).get("amount"),
            _coerce_json_object(value.get("sale")).get("value"),
            _coerce_json_object(value.get("min")).get("amount"),
            _coerce_json_object(value.get("min")).get("value"),
        ]
        for candidate in candidates:
            price = _coerce_positive_history_price(candidate)
            if price is not None:
                return price
        return None
    if isinstance(value, str):
        value = value.strip().replace(",", "")
        if not value:
            return None
    try:
        price = float(value)
    except Exception:
        return None
    if price > 0:
        return price
    return None


def _history_aliases_from_object(value: Any, keys: Tuple[str, ...]) -> List[str]:
    data = _coerce_json_object(value)
    aliases: List[str] = []

    def add(raw: Any) -> None:
        if isinstance(raw, dict):
            for nested_key in ("id", "product_id", "variant_id", "sku", "sku_id"):
                add(raw.get(nested_key))
            return
        text = str(raw or "").strip()
        if text and text not in aliases:
            aliases.append(text)

    for key in keys:
        add(data.get(key))
    return aliases


def _history_payload_parts(payload: Any) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]:
    root = _coerce_json_object(payload)
    product = _coerce_json_object(root.get("product")) or root
    variants: List[Dict[str, Any]] = []
    offers: List[Dict[str, Any]] = []

    for source in (product.get("variants"), root.get("variants")):
        if isinstance(source, list):
            variants.extend(_coerce_json_object(item) for item in source if _coerce_json_object(item))

    for source in (product.get("offers"), root.get("offers")):
        if isinstance(source, list):
            offers.extend(_coerce_json_object(item) for item in source if _coerce_json_object(item))

    modules = root.get("modules")
    if isinstance(modules, list):
        for module in modules:
            module_data = _coerce_json_object(module)
            data = _coerce_json_object(module_data.get("data"))
            pdp_payload = _coerce_json_object(data.get("pdp_payload"))
            if not pdp_payload:
                continue
            nested_product, nested_variants, nested_offers = _history_payload_parts(pdp_payload)
            if nested_product and product is root:
                product = nested_product
            variants.extend(nested_variants)
            offers.extend(nested_offers)

    return product, variants, offers


def _extract_history_price_from_product_payload(payload: Any, match_id: Optional[str] = None) -> Optional[float]:
    product, variants, offers = _history_payload_parts(payload)
    if not product:
        return None

    match = str(match_id or "").strip()
    if match:
        for variant_data in variants:
            variant_aliases = _history_aliases_from_object(
                variant_data,
                (
                    "variant_id",
                    "id",
                    "sku",
                    "sku_id",
                    "platform_variant_id",
                    "external_variant_id",
                    "product_id",
                    "platform_product_id",
                    "external_product_id",
                ),
            )
            if match not in variant_aliases:
                continue
            for key in ("price", "pricing", "price_amount", "current_price", "sale_price", "min_price"):
                price = _coerce_positive_history_price(variant_data.get(key))
                if price is not None:
                    return price

        for offer_data in offers:
            offer_aliases = _history_aliases_from_object(
                offer_data,
                (
                    "offer_id",
                    "id",
                    "product_id",
                    "platform_product_id",
                    "external_product_id",
                    "variant_id",
                    "platform_variant_id",
                    "sku",
                    "sku_id",
                ),
            )
            if match not in offer_aliases:
                continue
            for key in ("price", "pricing", "price_amount", "current_price", "sale_price", "min_price"):
                price = _coerce_positive_history_price(offer_data.get(key))
                if price is not None:
                    return price

    for key in ("price", "pricing", "price_amount", "current_price", "sale_price", "min_price"):
        price = _coerce_positive_history_price(product.get(key))
        if price is not None:
            return price

    if isinstance(variants, list):
        for variant in variants:
            for key in ("price", "pricing", "price_amount"):
                price = _coerce_positive_history_price(variant.get(key))
                if price is not None:
                    return price

    if isinstance(offers, list):
        for offer in offers:
            for key in ("price", "pricing", "price_amount"):
                price = _coerce_positive_history_price(offer.get(key))
                if price is not None:
                    return price

    return None


def _extract_history_currency_from_product_payload(payload: Any) -> Optional[str]:
    product, variants, offers = _history_payload_parts(payload)
    if not product:
        return None

    for value in (
        product.get("currency"),
        product.get("price_currency"),
        product.get("currency_code"),
        _coerce_json_object(product.get("price")).get("currency"),
        _coerce_json_object(product.get("price")).get("currency_code"),
        _coerce_json_object(_coerce_json_object(product.get("price")).get("current")).get("currency"),
        _coerce_json_object(_coerce_json_object(product.get("price")).get("current")).get("currency_code"),
    ):
        text = str(value or "").strip().upper()
        if text:
            return text
    for collection in (variants, offers):
        for item in collection:
            for value in (
                item.get("currency"),
                item.get("price_currency"),
                item.get("currency_code"),
                _coerce_json_object(item.get("price")).get("currency"),
                _coerce_json_object(item.get("price")).get("currency_code"),
                _coerce_json_object(_coerce_json_object(item.get("price")).get("current")).get("currency"),
                _coerce_json_object(_coerce_json_object(item.get("price")).get("current")).get("currency_code"),
            ):
                text = str(value or "").strip().upper()
                if text:
                    return text
    return None


def _product_payload_ids(payload: Any) -> List[str]:
    product, variants, offers = _history_payload_parts(payload)
    ids: List[str] = []

    def add(value: Any) -> None:
        if isinstance(value, dict):
            for nested_key in ("id", "product_id", "variant_id", "sku", "sku_id"):
                add(value.get(nested_key))
            return
        text = str(value or "").strip()
        if text and text not in ids:
            ids.append(text)

    for key in (
        "id",
        "product_id",
        "platform_product_id",
        "external_product_id",
        "canonical_product_id",
        "product_group_id",
        "sellable_item_group_id",
        "product_line_id",
        "sku",
        "sku_id",
    ):
        add(product.get(key))
    add(product.get("canonical_product_ref"))
    add(product.get("canonical_payload_product_ref"))

    for variant_data in variants:
        for key in (
            "variant_id",
            "id",
            "sku",
            "sku_id",
            "platform_variant_id",
            "external_variant_id",
            "product_id",
            "platform_product_id",
            "external_product_id",
        ):
            add(variant_data.get(key))

    for offer_data in offers:
        for key in (
            "offer_id",
            "id",
            "product_id",
            "platform_product_id",
            "external_product_id",
            "variant_id",
            "platform_variant_id",
            "sku",
            "sku_id",
        ):
            add(offer_data.get(key))

    return ids


def _is_external_history_merchant(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in {"external_seed", "external_product_seeds", "external"}


async def _resolve_history_price_lookup(rows: List[Dict[str, Any]]) -> Dict[str, HistoryPriceResolution]:
    requests: List[Dict[str, Optional[str]]] = []
    seen_request_keys: set = set()
    for row in rows:
        product_id = str(row.get("product_id") or "").strip()
        if not product_id:
            continue
        merchant_id = _normalize_history_merchant_id(row.get("merchant_id"))
        key = _history_item_key(product_id, merchant_id)
        if key in seen_request_keys:
            continue
        seen_request_keys.add(key)
        requests.append({"product_id": product_id, "merchant_id": merchant_id, "key": key})

    product_ids = sorted({str(req.get("product_id") or "").strip() for req in requests})
    if not product_ids:
        return {}

    lookup: Dict[str, HistoryPriceResolution] = {}

    def put(product_id: Any, price: Optional[float], currency: Optional[str], source: str, merchant_id: Optional[str] = None) -> None:
        alias = str(product_id or "").strip()
        candidate_merchant = _normalize_history_merchant_id(merchant_id)
        if not alias or price is None or price <= 0:
            return
        for req in requests:
            if req["product_id"] != alias:
                continue
            req_key = str(req["key"] or "")
            if req_key in lookup:
                continue
            requested_merchant = _normalize_history_merchant_id(req.get("merchant_id"))
            merchant_matches = (
                not requested_merchant
                or not candidate_merchant
                or requested_merchant == candidate_merchant
                or _is_external_history_merchant(requested_merchant)
            )
            if not merchant_matches:
                continue
            lookup[req_key] = HistoryPriceResolution(
                price=float(price),
                currency=str(currency or "").strip().upper() or None,
                source=source,
            )

    try:
        seed_rows = await asyncio.wait_for(
            database.fetch_all(
                f"""
                SELECT id, external_product_id, attached_product_key, attached_variant_id,
                       price_amount, price_currency, seed_data
                FROM external_product_seeds
                WHERE status = 'active'
                  AND (
                    id = ANY(:product_ids)
                    OR external_product_id = ANY(:product_ids)
                    OR attached_product_key = ANY(:product_ids)
                    OR attached_variant_id = ANY(:product_ids)
                  )
                  {_seed_quarantine_clause()}
                ORDER BY updated_at DESC, created_at DESC
                LIMIT :limit
                """,
                {"product_ids": product_ids, "limit": min(max(len(product_ids) * 2, 1), 250)},
            ),
            timeout=1.0,
        )
        for row in seed_rows or []:
            data = dict(row)
            seed_data = _coerce_json_object(data.get("seed_data"))
            price = (
                _coerce_positive_history_price(data.get("price_amount"))
                or _coerce_positive_history_price(seed_data.get("price_amount"))
                or _coerce_positive_history_price(seed_data.get("price"))
                or _extract_history_price_from_product_payload(seed_data)
            )
            currency = (
                str(data.get("price_currency") or "").strip().upper()
                or str(seed_data.get("price_currency") or seed_data.get("currency") or "").strip().upper()
                or _extract_history_currency_from_product_payload(seed_data)
            )
            for alias in (
                data.get("id"),
                data.get("external_product_id"),
                data.get("attached_product_key"),
                data.get("attached_variant_id"),
                *_product_payload_ids(seed_data),
            ):
                alias_price = _extract_history_price_from_product_payload(seed_data, str(alias or "").strip()) or price
                put(alias, alias_price, currency, "external_product_seeds")
    except Exception as exc:
        logger.warning("Browse history external seed price lookup skipped: %s", exc)

    unresolved_ids = sorted({
        str(req["product_id"] or "").strip()
        for req in requests
        if str(req["key"] or "") not in lookup
    })
    if unresolved_ids:
        try:
            cache_rows = await asyncio.wait_for(
                database.fetch_all(
                    """
                    SELECT merchant_id, platform_product_id, product_data
                    FROM products_cache
                    WHERE (expires_at IS NULL OR expires_at > NOW())
                      AND (
                        platform_product_id = ANY(:product_ids)
                        OR product_data::jsonb->>'id' = ANY(:product_ids)
                        OR product_data::jsonb->>'product_id' = ANY(:product_ids)
                        OR product_data::jsonb->>'platform_product_id' = ANY(:product_ids)
                        OR product_data::jsonb->>'external_product_id' = ANY(:product_ids)
                        OR product_data::jsonb->>'product_group_id' = ANY(:product_ids)
                        OR product_data::jsonb->>'sellable_item_group_id' = ANY(:product_ids)
                        OR product_data::jsonb->>'product_line_id' = ANY(:product_ids)
                        OR product_data::jsonb#>>'{canonical_product_ref,product_id}' = ANY(:product_ids)
                        OR product_data::jsonb#>>'{canonical_payload_product_ref,product_id}' = ANY(:product_ids)
                        OR EXISTS (
                          SELECT 1
                          FROM jsonb_array_elements(
                            CASE
                              WHEN jsonb_typeof(product_data::jsonb->'variants') = 'array'
                              THEN product_data::jsonb->'variants'
                              ELSE '[]'::jsonb
                            END
                          ) AS variant
                          WHERE variant->>'variant_id' = ANY(:product_ids)
                             OR variant->>'id' = ANY(:product_ids)
                             OR variant->>'sku' = ANY(:product_ids)
                             OR variant->>'sku_id' = ANY(:product_ids)
                             OR variant->>'platform_variant_id' = ANY(:product_ids)
                             OR variant->>'external_variant_id' = ANY(:product_ids)
                             OR variant->>'product_id' = ANY(:product_ids)
                             OR variant->>'platform_product_id' = ANY(:product_ids)
                             OR variant->>'external_product_id' = ANY(:product_ids)
                        )
                        OR EXISTS (
                          SELECT 1
                          FROM jsonb_array_elements(
                            CASE
                              WHEN jsonb_typeof(product_data::jsonb->'offers') = 'array'
                              THEN product_data::jsonb->'offers'
                              ELSE '[]'::jsonb
                            END
                          ) AS offer
                          WHERE offer->>'offer_id' = ANY(:product_ids)
                             OR offer->>'id' = ANY(:product_ids)
                             OR offer->>'product_id' = ANY(:product_ids)
                             OR offer->>'platform_product_id' = ANY(:product_ids)
                             OR offer->>'external_product_id' = ANY(:product_ids)
                             OR offer->>'variant_id' = ANY(:product_ids)
                             OR offer->>'platform_variant_id' = ANY(:product_ids)
                             OR offer->>'sku' = ANY(:product_ids)
                             OR offer->>'sku_id' = ANY(:product_ids)
                        )
                      )
                    ORDER BY cached_at DESC, id DESC
                    LIMIT :limit
                    """,
                    {"product_ids": unresolved_ids, "limit": min(max(len(unresolved_ids) * 2, 1), 250)},
                ),
                timeout=1.0,
            )
            for row in cache_rows or []:
                data = dict(row)
                product_data = _coerce_json_object(data.get("product_data"))
                currency = _extract_history_currency_from_product_payload(product_data)
                for alias in (data.get("platform_product_id"), *_product_payload_ids(product_data)):
                    alias_text = str(alias or "").strip()
                    price = _extract_history_price_from_product_payload(product_data, alias_text)
                    put(alias_text, price, currency, "products_cache", data.get("merchant_id"))
        except Exception as exc:
            logger.warning("Browse history products_cache price lookup skipped: %s", exc)

    return lookup


async def _persist_history_price_best_effort(row_id: Any, price: float, currency: Optional[str]) -> None:
    if row_id is None or price <= 0:
        return
    try:
        await database.execute(
            shop_browse_history_events.update()
            .where(shop_browse_history_events.c.id == row_id)
            .values(
                price=float(price),
                **({"currency": currency} if currency else {}),
            )
        )
    except Exception:
        return


def _history_price_source_counts(items: List[BrowseHistoryItem]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for item in items:
        source = str(item.price_source or item.price_resolution_status or "unknown").strip() or "unknown"
        counts[source] = counts.get(source, 0) + 1
    return counts


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

    await _ensure_database_connected()

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

    otp_insert_values = shop_login_otps.insert().values(
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
    try:
        await database.execute(otp_insert_values)
    except Exception as exc:
        if is_asyncpg_busy_error(exc):
            logger.warning(
                "[AccountsAuth] transient DB state during OTP insert; retrying once for %s",
                _mask_email(email),
            )
            await _ensure_database_connected()
            try:
                await asyncio.sleep(0.05)
            except Exception:
                pass
            try:
                await database.execute(otp_insert_values)
            except Exception as exc2:
                if is_asyncpg_busy_error(exc2):
                    raise db_busy_http_exception()
                raise
        else:
            raise

    # Log OTP for observability (avoid PII in logs)
    logger.info("[AccountsAuth] OTP generated for %s", _mask_email(email))

    # Email delivery is required in production; fail-closed so the UI can surface the issue.
    try:
        delivery = await asyncio.to_thread(_send_login_otp_email, email, otp_code)
    except Exception as exc:
        delivery = None
        logger.warning(
            "[AccountsAuth] OTP email send raised error=%s to=%s",
            type(exc).__name__,
            _mask_email(email),
        )
    if not getattr(delivery, "ok", False) and not settings.dev_mode:
        logger.warning(
            "[AccountsAuth] OTP email delivery failed provider=%s error=%s to=%s",
            getattr(delivery, "provider", None),
            getattr(delivery, "error", None),
            _mask_email(email),
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": {
                    "code": "EMAIL_DELIVERY_FAILED",
                    "message": "Unable to deliver login code email. Please retry shortly.",
                    "provider": getattr(delivery, "provider", None),
                    "delivery_error": getattr(delivery, "error", None),
                    "delivery_details": getattr(delivery, "details", None),
                }
            },
        )

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
    await _mark_email_verified_best_effort(user_row.get("id"))
    customer_membership = await sync_customer_auth_membership(user_row)

    session_payload = await _build_user_session(user_row)
    response = JSONResponse(session_payload.dict())
    _set_auth_cookies(
        response,
        user_id=user_row["id"],
        email=user_row["email"],
        primary_role=user_row.get("primary_role", "customer"),
        amr="otp",
        auth_time=int(datetime.now(timezone.utc).timestamp()),
        identity_id=_identity_id_from_membership(customer_membership),
    )
    return response

def _validate_password_bytes(password: str) -> None:
    pw_bytes = password.encode("utf-8")
    if len(pw_bytes) < 8:
        raise _error(
            status.HTTP_400_BAD_REQUEST,
            "INVALID_INPUT",
            "Password must be at least 8 characters.",
        )
    # bcrypt truncates at 72 bytes; enforce to avoid surprising behavior.
    if len(pw_bytes) > 72:
        raise _error(
            status.HTTP_400_BAD_REQUEST,
            "INVALID_INPUT",
            "Password must be at most 72 bytes.",
        )


@router.post("/auth/password/login")
async def password_login(body: PasswordLoginRequest, request: Request):
    """
    Password-based login for accounts users.
    """
    await _ensure_database_connected()
    email = str(body.email)
    norm = normalize_email(email)
    _validate_password_bytes(body.password)

    user_row = await database.fetch_one(
        shop_users.select().where(shop_users.c.email_normalized == norm)
    )
    if not user_row:
        raise _error(
            status.HTTP_401_UNAUTHORIZED,
            "INVALID_CREDENTIALS",
            "Email or password is incorrect",
        )

    pw_row = await database.fetch_one(
        shop_user_passwords.select().where(shop_user_passwords.c.user_id == user_row["id"])
    )
    if not pw_row:
        raise _error(
            status.HTTP_400_BAD_REQUEST,
            "NO_PASSWORD",
            "No password is set for this account. Sign in with an email code once, then set a password.",
        )

    if not verify_password(body.password, pw_row["password_hash"]):
        raise _error(
            status.HTTP_401_UNAUTHORIZED,
            "INVALID_CREDENTIALS",
            "Email or password is incorrect",
        )

    user_dict = dict(user_row)
    await _mark_email_verified_best_effort(user_dict.get("id"))
    customer_membership = await sync_customer_auth_membership(
        user_dict,
        password_hash=pw_row["password_hash"],
    )
    session_payload = await _build_user_session(user_dict)
    response = JSONResponse(session_payload.dict())
    _set_auth_cookies(
        response,
        user_id=user_row["id"],
        email=user_row["email"],
        primary_role=str(user_dict.get("primary_role") or "customer"),
        amr="password",
        auth_time=int(datetime.now(timezone.utc).timestamp()),
        identity_id=_identity_id_from_membership(customer_membership),
    )
    return response


@router.post("/auth/aurora/exchange")
async def aurora_exchange_login(body: AuroraExchangeRequest):
    """
    Exchange an Aurora auth session for Accounts cookies (orders/account surfaces).
    """
    await _ensure_database_connected()
    aurora_token = str(body.aurora_token or "").strip()
    aurora_uid = str(body.aurora_uid or "").strip()
    if not aurora_token or not aurora_uid:
        raise _error(
            status.HTTP_400_BAD_REQUEST,
            "INVALID_INPUT",
            "aurora_token and aurora_uid are required",
        )

    aurora_payload = await _fetch_aurora_auth_me(
        aurora_token=aurora_token,
        aurora_uid=aurora_uid,
    )
    aurora_email = _resolve_aurora_email_from_auth_me_payload(aurora_payload)
    if not aurora_email:
        raise _error(
            status.HTTP_502_BAD_GATEWAY,
            "BAD_UPSTREAM_PAYLOAD",
            "Aurora auth upstream payload is missing user email",
        )

    user_row = await create_or_get_shop_user(email=aurora_email, phone=None)
    user_dict = dict(user_row)
    await _mark_email_verified_best_effort(user_dict.get("id"))
    customer_membership = await sync_customer_auth_membership(user_dict)

    session_payload = await _build_user_session(user_dict)
    response_payload = session_payload.dict()
    response_payload["linked_via"] = "aurora_embed"
    response = JSONResponse(response_payload)
    _set_auth_cookies(
        response,
        user_id=user_row["id"],
        email=user_row["email"],
        primary_role=str(user_dict.get("primary_role") or "customer"),
        amr="aurora_embed",
        auth_time=int(datetime.now(timezone.utc).timestamp()),
        identity_id=_identity_id_from_membership(customer_membership),
    )
    return response


@router.post("/auth/password/set")
async def set_password(
    body: PasswordSetRequest,
    principal: AccountsPrincipal = Depends(get_accounts_principal),
):
    """
    Set or change the current user's password.

    - If a password is already set, `current_password` is required.
    - Exception: if the current session was created via OTP in the last 15 minutes,
      allow setting a new password without providing the current one (password reset UX).
    """
    await _ensure_database_connected()
    _validate_password_bytes(body.new_password)

    # Ensure user exists
    user_row = await database.fetch_one(
        shop_users.select().where(shop_users.c.id == principal.user_id)
    )
    if not user_row:
        raise _error(
            status.HTTP_401_UNAUTHORIZED,
            "UNAUTHENTICATED",
            "User not found",
        )

    existing = await database.fetch_one(
        shop_user_passwords.select().where(
            shop_user_passwords.c.user_id == principal.user_id
        )
    )

    if existing:
        if body.current_password:
            if not verify_password(body.current_password, existing["password_hash"]):
                raise _error(
                    status.HTTP_401_UNAUTHORIZED,
                    "INVALID_CREDENTIALS",
                    "Current password is incorrect",
                )
        else:
            allow_without_current = False
            if principal.amr == "otp" and principal.auth_time:
                try:
                    token_auth_time = datetime.fromtimestamp(
                        int(principal.auth_time), tz=timezone.utc
                    )
                    if datetime.now(timezone.utc) - token_auth_time <= timedelta(minutes=15):
                        allow_without_current = True
                except Exception:
                    allow_without_current = False

            if not allow_without_current:
                raise _error(
                    status.HTTP_400_BAD_REQUEST,
                    "CURRENT_PASSWORD_REQUIRED",
                    "Current password is required to change password",
                )

    password_hash = hash_password(body.new_password)
    if existing:
        await database.execute(
            shop_user_passwords.update()
            .where(shop_user_passwords.c.user_id == principal.user_id)
            .values(password_hash=password_hash, updated_at=func.now())
        )
    else:
        await database.execute(
            shop_user_passwords.insert().values(
                user_id=principal.user_id,
                password_hash=password_hash,
                created_at=func.now(),
                updated_at=func.now(),
            )
        )
    await sync_customer_auth_membership(dict(user_row), password_hash=password_hash)

    return {"status": "ok"}


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

    slot_summary = await get_review_slot_summary(
        email_normalized=principal.email_normalized,
        user_id=principal.user_id,
        subject=subject,
    )
    total_paid_orders = int(slot_summary.get("total_paid_orders") or 0)
    available_slots = int(slot_summary.get("available_slots") or 0)
    is_purchaser = total_paid_orders > 0
    review_info = await get_user_review_for_subject(
        user_id=principal.user_id,
        subject_type=subject.subject_type,
        subject_id=subject.subject_id,
    )
    review_verification = str((review_info or {}).get("verification") or "unverified").strip().lower()
    review_is_verified = review_verification not in {"", "unverified"}
    review_has_rating = bool((review_info or {}).get("has_rating"))

    question_rate_limited = await is_question_rate_limited(
        user_id=principal.user_id,
        subject_type=subject.subject_type,
        subject_id=subject.subject_id,
        window_seconds=60,
    )

    can_upload = bool(is_purchaser)
    # Reviews policy:
    # - Any logged-in user can submit a text review/comment (once).
    # - Only verified purchasers can submit a rating.
    # - If a user already left an unverified review, allow a one-time upgrade after purchase.
    can_upgrade_review = bool(is_purchaser) and bool(review_info) and not bool(review_is_verified)
    can_add_rating = bool(is_purchaser) and bool(review_info) and bool(review_is_verified) and not bool(review_has_rating)
    can_create_new_for_unreviewed_order = available_slots > 0
    can_write_review = can_create_new_for_unreviewed_order or can_upgrade_review or can_add_rating
    can_rate_review = bool(is_purchaser) and (
        can_create_new_for_unreviewed_order or can_upgrade_review or can_add_rating
    )
    can_ask = not bool(question_rate_limited)

    reasons: Dict[str, str] = {}
    if not can_upload:
        reasons["upload"] = "NOT_PURCHASER"
    if not can_write_review:
        reasons["review"] = "NOT_PURCHASER" if not is_purchaser else "ALREADY_REVIEWED"
    if not can_rate_review:
        reasons["rating"] = "NOT_VERIFIED_FOR_RATING" if not is_purchaser else "ALREADY_REVIEWED"
    if not can_ask:
        reasons["question"] = "RATE_LIMITED"

    return {
        "ugcCapabilities": {
            "canUploadMedia": can_upload,
            "canWriteReview": can_write_review,
            "canRateReview": can_rate_review,
            "canAskQuestion": can_ask,
            "reasons": reasons,
            "review": review_info,
            "reviewSlots": {
                "totalPaidOrders": total_paid_orders,
                "usedOrders": int(slot_summary.get("used_slots") or 0),
                "availableOrders": available_slots,
                "legacyBindings": int(slot_summary.get("legacy_binding_count") or 0),
            },
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

    slot_summary = await get_review_slot_summary(
        email_normalized=principal.email_normalized,
        user_id=principal.user_id,
        subject=subject,
    )
    total_paid_orders = int(slot_summary.get("total_paid_orders") or 0)
    available_slots = int(slot_summary.get("available_slots") or 0)
    is_purchaser = total_paid_orders > 0
    review_info = await get_user_review_for_subject(
        user_id=principal.user_id,
        subject_type=subject.subject_type,
        subject_id=subject.subject_id,
    )
    review_verification = str((review_info or {}).get("verification") or "unverified").strip().lower()
    review_is_verified = review_verification not in {"", "unverified"}
    review_has_rating = bool((review_info or {}).get("has_rating"))

    can_upgrade_review = bool(is_purchaser) and bool(review_info) and not bool(review_is_verified)
    can_add_rating = bool(is_purchaser) and bool(review_info) and bool(review_is_verified) and not bool(review_has_rating)
    can_create_new_for_unreviewed_order = available_slots > 0
    eligible = can_create_new_for_unreviewed_order or can_upgrade_review or can_add_rating
    can_rate = bool(is_purchaser) and (can_create_new_for_unreviewed_order or can_upgrade_review or can_add_rating)

    if not eligible:
        reason = "NOT_PURCHASER" if not is_purchaser else "ALREADY_REVIEWED"
        return {
            "eligible": False,
            "reason": reason,
            "canRate": False,
            "reviewSlots": {
                "totalPaidOrders": total_paid_orders,
                "usedOrders": int(slot_summary.get("used_slots") or 0),
                "availableOrders": available_slots,
                "legacyBindings": int(slot_summary.get("legacy_binding_count") or 0),
            },
        }

    action = (
        "CREATE"
        if can_create_new_for_unreviewed_order
        else ("UPGRADE" if can_upgrade_review else "ADD_RATING" if can_add_rating else "CREATE")
    )
    out: Dict[str, Any] = {
        "eligible": True,
        "canRate": bool(can_rate),
        "action": action,
        "reviewSlots": {
            "totalPaidOrders": total_paid_orders,
            "usedOrders": int(slot_summary.get("used_slots") or 0),
            "availableOrders": available_slots,
            "legacyBindings": int(slot_summary.get("legacy_binding_count") or 0),
        },
    }
    if not can_rate:
        out["ratingReason"] = "NOT_VERIFIED_FOR_RATING"
    return out


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

    user_id = (
        payload.get("customer_user_id")
        or payload.get("user_id")
        or payload.get("sub")
    )
    identity_id = payload.get("identity_id")
    if not identity_id and str(payload.get("sub") or "").startswith("identity_"):
        identity_id = payload.get("sub")
    email = payload.get("email")
    primary_role = payload.get("role", "customer")
    amr = payload.get("amr")
    raw_auth_time = payload.get("auth_time")
    auth_time: Optional[int] = None
    try:
        if isinstance(raw_auth_time, (int, float)):
            auth_time = int(raw_auth_time)
        elif isinstance(raw_auth_time, str) and raw_auth_time.strip().isdigit():
            auth_time = int(raw_auth_time.strip())
    except Exception:
        auth_time = None
    if not user_id or not email:
        raise _error(
            status.HTTP_401_UNAUTHORIZED,
            "UNAUTHENTICATED",
            "Invalid refresh token payload",
        )

    response = JSONResponse({"status": "ok"})
    _set_auth_cookies(
        response,
        user_id=user_id,
        email=email,
        primary_role=primary_role,
        amr=amr,
        auth_time=auth_time,
        identity_id=identity_id,
    )
    return response


@router.post("/auth/logout")
async def logout():
    response = JSONResponse({"status": "ok"})
    _clear_auth_cookies(response)
    return response


# ---------------------------------------------------------------------------
# Protected Browse History endpoints
# ---------------------------------------------------------------------------

@router.post("/browse-history/events")
async def create_browse_history_event(
    body: BrowseHistoryEventRequest,
    principal: AccountsPrincipal = Depends(get_accounts_principal),
):
    await _ensure_database_connected()
    await _ensure_browse_history_schema()

    product_id = str(body.product_id or "").strip()
    if not product_id:
        raise _error(
            status.HTTP_400_BAD_REQUEST,
            "INVALID_INPUT",
            "product_id is required",
        )

    merchant_id = _normalize_history_merchant_id(body.merchant_id)
    viewed_at = _parse_iso_datetime(body.viewed_at) or datetime.now(timezone.utc)

    base_filters = [
        shop_browse_history_events.c.user_id == principal.user_id,
        shop_browse_history_events.c.product_id == product_id,
    ]
    if merchant_id is None:
        base_filters.append(shop_browse_history_events.c.merchant_id.is_(None))
    else:
        base_filters.append(shop_browse_history_events.c.merchant_id == merchant_id)

    existing = await database.fetch_one(
        shop_browse_history_events.select()
        .where(and_(*base_filters))
        .order_by(
            shop_browse_history_events.c.viewed_at.desc(),
            shop_browse_history_events.c.id.desc(),
        )
        .limit(1)
    )
    existing_price = _coerce_positive_history_price(dict(existing).get("price") if existing else None)
    incoming_price = _coerce_positive_history_price(body.price)
    resolved_price: Optional[HistoryPriceResolution] = None

    if incoming_price is None:
        if body.price is not None:
            logger.warning(
                "Browse history ignored non-positive price write user_id=%s product_id=%s merchant_id=%s price=%s",
                principal.user_id,
                product_id,
                merchant_id,
                body.price,
            )
        if existing_price is None:
            price_lookup = await _resolve_history_price_lookup(
                [
                    {
                        "product_id": product_id,
                        "merchant_id": merchant_id,
                        "price": body.price,
                    }
                ]
            )
            resolved_price = price_lookup.get(_history_item_key(product_id, merchant_id))

    final_price = incoming_price or existing_price or (resolved_price.price if resolved_price else None)
    final_currency = (
        body.currency
        or (dict(existing).get("currency") if existing else None)
        or (resolved_price.currency if resolved_price else None)
        or "USD"
    )

    write_values: Dict[str, Any] = {
        "user_id": principal.user_id,
        "product_id": product_id,
        "merchant_id": merchant_id,
        "title": body.title,
        "price": float(final_price) if final_price is not None else None,
        "currency": final_currency,
        "image_url": body.image_url or "/placeholder.svg",
        "description": body.description,
        "brand": body.brand,
        "category": body.category,
        "product_type": body.product_type,
        "viewed_at": viewed_at,
    }

    if existing:
        await database.execute(
            shop_browse_history_events.update()
            .where(shop_browse_history_events.c.id == existing["id"])
            .values(**write_values)
        )
        stored = await database.fetch_one(
            shop_browse_history_events.select().where(
                shop_browse_history_events.c.id == existing["id"]
            )
        )
    else:
        inserted_id = await database.execute(
            shop_browse_history_events.insert().values(**write_values)
        )
        stored = None
        try:
            if inserted_id is not None:
                stored = await database.fetch_one(
                    shop_browse_history_events.select().where(
                        shop_browse_history_events.c.id == inserted_id
                    )
                )
        except Exception:
            stored = None
        if not stored:
            stored = await database.fetch_one(
                shop_browse_history_events.select()
                .where(and_(*base_filters))
                .order_by(
                    shop_browse_history_events.c.viewed_at.desc(),
                    shop_browse_history_events.c.id.desc(),
                )
                .limit(1)
            )

    if not stored:
        raise _error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "SERVER_ERROR",
            "Failed to store browse history event",
        )

    return {
        "status": "ok",
        "item": _history_row_to_item(dict(stored)).dict(),
    }


@router.get("/browse-history", response_model=BrowseHistoryListResponse)
async def list_browse_history(
    principal: AccountsPrincipal = Depends(get_accounts_principal),
    limit: int = Query(30, ge=1, le=100),
):
    await _ensure_database_connected()
    await _ensure_browse_history_schema()

    scan_limit = min(max(limit * 5, limit), 500)
    rows = await database.fetch_all(
        shop_browse_history_events.select()
        .where(shop_browse_history_events.c.user_id == principal.user_id)
        .order_by(
            shop_browse_history_events.c.viewed_at.desc(),
            shop_browse_history_events.c.id.desc(),
        )
        .limit(scan_limit)
    )

    selected_rows: List[Dict[str, Any]] = []
    seen: set = set()
    for row in rows:
        data = dict(row)
        key = _history_item_key(
            str(data.get("product_id") or "").strip(),
            _normalize_history_merchant_id(data.get("merchant_id")),
        )
        if not key or key in seen:
            continue
        seen.add(key)
        selected_rows.append(data)
        if len(selected_rows) >= limit:
            break

    rows_missing_price = [
        row
        for row in selected_rows
        if _coerce_positive_history_price(row.get("price")) is None
    ]
    price_lookup = await _resolve_history_price_lookup(rows_missing_price)

    items: List[BrowseHistoryItem] = []
    persist_tasks = []
    unresolved_rows: List[Dict[str, Any]] = []
    for data in selected_rows:
        product_id = str(data.get("product_id") or "").strip()
        merchant_id = _normalize_history_merchant_id(data.get("merchant_id"))
        stored_price = _coerce_positive_history_price(data.get("price"))
        if stored_price is not None:
            data["price"] = stored_price
            data["price_source"] = "history_event"
            data["price_resolution_status"] = "stored"
            items.append(_history_row_to_item(data))
            continue

        resolved = price_lookup.get(_history_item_key(product_id, merchant_id))
        if resolved:
            data["price"] = resolved.price
            if resolved.currency:
                data["currency"] = resolved.currency
            data["price_source"] = resolved.source
            data["price_resolution_status"] = "resolved"
            persist_tasks.append(
                _persist_history_price_best_effort(data.get("id"), resolved.price, resolved.currency)
            )
            items.append(_history_row_to_item(data))
            continue

        data["price_resolution_status"] = "unresolved"
        unresolved_rows.append(data)

    if persist_tasks:
        await asyncio.gather(*persist_tasks, return_exceptions=True)

    if unresolved_rows:
        logger.warning(
            "Browse history unresolved prices user_id=%s count=%s sample=%s",
            principal.user_id,
            len(unresolved_rows),
            [
                {
                    "product_id": str(row.get("product_id") or "").strip(),
                    "merchant_id": _normalize_history_merchant_id(row.get("merchant_id")),
                }
                for row in unresolved_rows[:10]
            ],
        )

    return BrowseHistoryListResponse(
        items=items,
        total=len(items),
        unresolved_total=len(unresolved_rows),
        price_source_counts=_history_price_source_counts(items),
    )


@router.delete("/browse-history")
async def clear_browse_history(
    principal: AccountsPrincipal = Depends(get_accounts_principal),
):
    await _ensure_database_connected()
    deleted = (
        await database.fetch_val(
            select(func.count())
            .select_from(shop_browse_history_events)
            .where(shop_browse_history_events.c.user_id == principal.user_id)
        )
        or 0
    )
    await database.execute(
        shop_browse_history_events.delete().where(
            shop_browse_history_events.c.user_id == principal.user_id
        )
    )
    return {"status": "ok", "deleted": int(deleted)}


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
            metadata = _coerce_json_object(data.get("metadata"))
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
                partially_refunded=(str(data.get("status") or "").strip().lower() == "partially_refunded"),
            )
            refund_status = _derive_refund_status(data, metadata, status_summary)
            total_refunded_minor = _amount_to_minor(data.get("total_refunded"))

            shipping = data.get("shipping_address") or {}
            items = data.get("items") or []

            orders_list.append(
                OrdersListItem(
                    order_id=data["order_id"],
                    currency=data.get("currency", "USD"),
                    total_amount_minor=_amount_to_minor(data.get("total")),
                    status=status_summary,
                    payment_status=payment_status_mapped,
                    refund_status=refund_status,
                    total_refunded_minor=total_refunded_minor,
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
    _ensure_customer_order_access(order_data, principal)

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
        partially_refunded=(
            str(order_data.get("status") or "").strip().lower() == "partially_refunded"
        ),
    )

    shipping = _coerce_json_object(order_data.get("shipping_address"))
    items = order_data.get("items") or []
    metadata = _coerce_json_object(order_data.get("metadata"))
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
            SELECT payment_id, payment_intent_id, amount, currency, psp_type, status, metadata
            FROM payments
            WHERE order_id = :order_id
            ORDER BY created_at ASC
            """,
            {"order_id": order_id},
        )
        for pr in payment_rows:
            pr_status = pr["status"]
            pr_metadata = _coerce_json_object(pr.get("metadata"))
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
                    "method": (
                        pr_metadata.get("method")
                        or pr_metadata.get("payment_method")
                        or pr_metadata.get("type")
                        or order_data.get("payment_method")
                    ),
                    "brand": (
                        pr_metadata.get("brand")
                        or pr_metadata.get("card_brand")
                        or pr_metadata.get("network")
                    ),
                    "last4": (
                        pr_metadata.get("last4")
                        or pr_metadata.get("card_last4")
                        or pr_metadata.get("card_last_4")
                    ),
                }
            )
    except Exception:
        payment_records = []

    resumable_payment = await _build_resumable_payment_payload(
        order_data,
        payment_status=payment_status_mapped,
    )

    # Fulfillment / shipments (best-effort, derived from tracking fields)
    tracking_events = _build_tracking_events(order_data)
    tracking_url = _extract_tracking_url(order_data)
    shipments: List[Dict[str, Any]] = []
    if order_data.get("tracking_number"):
        shipments.append(
            {
                "tracking_number": order_data.get("tracking_number"),
                "carrier": order_data.get("carrier"),
                "status": delivery_status,
                "estimated_delivery": None,
                "tracking_url": tracking_url,
                "events": tracking_events,
            }
        )

    customer_info = {
        "email": order_data.get("customer_email"),
        "phone": (shipping.get("phone") or ""),
        "name": shipping.get("name"),
    }

    total_refunded_minor = _amount_to_minor(order_data.get("total_refunded"))
    refund_status = _derive_refund_status(order_data, metadata, status_summary)
    refund_payload = {
        "status": refund_status,
        "case_id": metadata.get("refund_case_id"),
        "updated_at": metadata.get("refund_updated_at"),
        "total_refunded_minor": total_refunded_minor,
        "currency": order_data.get("currency", "USD"),
        "requests": metadata.get("refund_requests") if isinstance(metadata.get("refund_requests"), list) else [],
    }
    refund_tracking = build_order_refund_tracking_payload(
        order_data,
        psp_used=order_data.get("psp_used"),
    )
    if refund_tracking:
        refund_payload["psp"] = refund_tracking

    shipping_name = shipping.get("name") or shipping.get("full_name") or shipping.get("recipient_name")
    shipping_phone = shipping.get("phone") or shipping.get("phone_number")
    shipping_address_line1 = (
        shipping.get("address_line1")
        or shipping.get("address1")
        or shipping.get("line1")
        or shipping.get("street")
    )
    shipping_address_line2 = (
        shipping.get("address_line2")
        or shipping.get("address2")
        or shipping.get("line2")
        or shipping.get("unit")
    )
    shipping_city = shipping.get("city")
    shipping_province = (
        shipping.get("province")
        or shipping.get("state")
        or shipping.get("region")
    )
    shipping_country = shipping.get("country")
    shipping_postal_code = (
        shipping.get("postal_code")
        or shipping.get("zip")
        or shipping.get("zip_code")
    )
    pricing_minor = _extract_order_pricing_minor(order_data, items)

    response_payload = {
        "order": {
            "order_id": order_data["order_id"],
            "merchant_id": order_data["merchant_id"],
            "currency": order_data.get("currency", "USD"),
            "total_amount_minor": pricing_minor["total_amount_minor"],
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
                "name": shipping_name,
                "address_line1": shipping_address_line1,
                "address_line2": shipping_address_line2,
                "city": shipping_city,
                "province": shipping_province,
                "country": shipping_country,
                "postal_code": shipping_postal_code,
                "phone": shipping_phone,
            },
            "pricing": pricing_minor,
        },
        "pricing_quote": _extract_pricing_quote_payload(order_data),
        "items": await _build_order_items_payload(order_data),
        "payment": {
            "records": payment_records,
            "current": resumable_payment,
        },
        "fulfillment": {"shipments": shipments},
        "tracking": {
            "status": delivery_status,
            "carrier": order_data.get("carrier"),
            "tracking_number": order_data.get("tracking_number"),
            "tracking_url": tracking_url,
            "events": tracking_events,
        },
        "pricing": pricing_minor,
        "refund": refund_payload,
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


@router.get("/orders/{order_id}/tracking")
async def get_order_tracking(
    order_id: str,
    response: Response,
    principal: AccountsPrincipal = Depends(get_accounts_principal),
):
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
    _ensure_customer_order_access(order_data, principal)

    delivery_status = _derive_delivery_status(
        order_data.get("fulfillment_status"),
        order_data.get("tracking_number"),
    )

    return {
        "order_id": order_id,
        "status": delivery_status,
        "carrier": order_data.get("carrier"),
        "tracking_number": order_data.get("tracking_number"),
        "tracking_url": _extract_tracking_url(order_data),
        "events": _build_tracking_events(order_data),
    }


@router.post("/orders/{order_id}/refund", response_model=RefundOrderResponse)
async def request_order_refund(
    order_id: str,
    payload: RefundOrderRequest = Body(default=None),
    principal: AccountsPrincipal = Depends(get_accounts_principal),
):
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
    _ensure_customer_order_access(order_data, principal)

    order_status = str(order_data.get("status") or "").strip().lower()
    if order_status in {"cancelled", "canceled"}:
        raise _error(
            status.HTTP_400_BAD_REQUEST,
            "INVALID_STATE",
            "Cancelled orders cannot request refunds",
        )

    payment_status_mapped = _map_payment_status(order_data.get("payment_status"))
    if payment_status_mapped not in {"paid", "partial"}:
        raise _error(
            status.HTTP_400_BAD_REQUEST,
            "INVALID_STATE",
            "Order is not eligible for refund in its current payment state",
        )

    currency = str(order_data.get("currency") or "USD").strip().upper() or "USD"
    if payload and payload.currency and payload.currency != currency:
        raise _error(
            status.HTTP_400_BAD_REQUEST,
            "INVALID_INPUT",
            "Refund currency must match order currency",
        )

    total_minor = _amount_to_minor(order_data.get("total"))
    already_refunded_minor = _amount_to_minor(order_data.get("total_refunded"))
    remaining_minor = max(0, total_minor - already_refunded_minor)
    if remaining_minor <= 0:
        raise _error(
            status.HTTP_400_BAD_REQUEST,
            "INVALID_STATE",
            "Order has already been fully refunded",
        )

    request_amount_minor: int
    if payload and payload.amount_minor is not None:
        request_amount_minor = int(round(payload.amount_minor))
    elif payload and payload.amount is not None:
        request_amount_minor = _amount_to_minor(payload.amount)
    else:
        request_amount_minor = remaining_minor

    if request_amount_minor <= 0:
        raise _error(
            status.HTTP_400_BAD_REQUEST,
            "INVALID_INPUT",
            "Refund amount must be greater than zero",
        )
    if request_amount_minor > remaining_minor:
        raise _error(
            status.HTTP_400_BAD_REQUEST,
            "INVALID_INPUT",
            "Refund amount exceeds remaining refundable balance",
        )

    now = datetime.now(timezone.utc)
    case_id = f"rfd_{uuid.uuid4().hex[:12]}"
    metadata = _coerce_json_object(order_data.get("metadata"))
    refund_requests = metadata.get("refund_requests")
    if not isinstance(refund_requests, list):
        refund_requests = []

    refund_item_payload: List[Dict[str, Any]] = []
    if payload and payload.items:
        for item in payload.items:
            try:
                refund_item_payload.append(item.dict(exclude_none=True))
            except Exception:
                continue

    refund_requests.append(
        {
            "case_id": case_id,
            "status": "requested",
            "amount_minor": request_amount_minor,
            "currency": currency,
            "reason": payload.reason if payload else None,
            "items": refund_item_payload,
            "created_at": now.isoformat(),
        }
    )

    new_total_refunded_minor = already_refunded_minor + request_amount_minor
    metadata["refund_status"] = "requested"
    metadata["refund_case_id"] = case_id
    metadata["refund_updated_at"] = now.isoformat()
    metadata["refund_requests"] = refund_requests

    update_values: Dict[str, Any] = {
        "total_refunded": new_total_refunded_minor / 100.0,
        "updated_at": now,
        "metadata": metadata,
    }
    if new_total_refunded_minor >= total_minor:
        update_values["status"] = "refunded"
        update_values["payment_status"] = "refunded"

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

    return RefundOrderResponse(
        order_id=order_id,
        refund_status="requested",
        case_id=case_id,
        updated_at=now.isoformat(),
        total_refunded_minor=new_total_refunded_minor,
        currency=currency,
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
    order_data = await _load_public_order_for_customer(
        request,
        order_id=order_id,
        email=email,
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
        partially_refunded=(
            str(order_data.get("status") or "").strip().lower() == "partially_refunded"
        ),
    )

    shipping = order_data.get("shipping_address") or {}
    items = order_data.get("items") or []
    pricing_minor = _extract_order_pricing_minor(order_data, items)

    return PublicOrderLookupResponse(
        order_id=order_id,
        status=status_summary,
        currency=order_data.get("currency", "USD"),
        total_amount_minor=pricing_minor["total_amount_minor"],
        pricing=OrderPricingResponse(**pricing_minor),
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


@router.get("/public/order-resume", response_model=PublicOrderResumeResponse)
async def public_order_resume(
    request: Request,
    order_id: str = Query(...),
    email: EmailStr = Query(...),
):
    order_data = await _load_public_order_for_customer(
        request,
        order_id=order_id,
        email=email,
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

    shipping = _coerce_json_object(order_data.get("shipping_address"))
    resumable_payment = await _build_resumable_payment_payload(
        order_data,
        payment_status=payment_status_mapped,
    )

    shipping_name = shipping.get("name") or shipping.get("full_name") or shipping.get("recipient_name")
    shipping_phone = shipping.get("phone") or shipping.get("phone_number")
    shipping_address_line1 = (
        shipping.get("address_line1")
        or shipping.get("address1")
        or shipping.get("line1")
        or shipping.get("street")
    )
    shipping_address_line2 = (
        shipping.get("address_line2")
        or shipping.get("address2")
        or shipping.get("line2")
        or shipping.get("unit")
    )
    shipping_city = shipping.get("city")
    shipping_province = (
        shipping.get("province")
        or shipping.get("state")
        or shipping.get("region")
    )
    shipping_country = shipping.get("country")
    shipping_postal_code = (
        shipping.get("postal_code")
        or shipping.get("zip")
        or shipping.get("zip_code")
    )
    metadata = _coerce_json_object(order_data.get("metadata"))
    total_refunded_minor = _amount_to_minor(order_data.get("total_refunded"))
    refund_status = (
        str(metadata.get("refund_status") or "").strip().lower()
        or ("refunded" if status_summary == "refunded" else "none")
    )
    refund_payload = {
        "status": refund_status,
        "case_id": metadata.get("refund_case_id"),
        "updated_at": metadata.get("refund_updated_at"),
        "total_refunded_minor": total_refunded_minor,
        "currency": order_data.get("currency", "USD"),
        "requests": metadata.get("refund_requests") if isinstance(metadata.get("refund_requests"), list) else [],
    }
    refund_tracking = build_order_refund_tracking_payload(
        order_data,
        psp_used=order_data.get("psp_used"),
    )
    if refund_tracking:
        refund_payload["psp"] = refund_tracking

    return PublicOrderResumeResponse(
        order={
            "order_id": order_data["order_id"],
            "merchant_id": order_data["merchant_id"],
            "currency": order_data.get("currency", "USD"),
            "total_amount_minor": _amount_to_minor(order_data.get("total")),
            "status": status_summary,
            "payment_status": payment_status_mapped,
            "fulfillment_status": fulfillment_status_mapped,
            "delivery_status": delivery_status,
            "created_at": _to_iso_string(order_data.get("created_at") or datetime.now(timezone.utc)),
            "updated_at": _to_iso_string(order_data.get("updated_at") or datetime.now(timezone.utc)),
            "shipping_address": {
                "name": shipping_name,
                "address_line1": shipping_address_line1,
                "address_line2": shipping_address_line2,
                "city": shipping_city,
                "province": shipping_province,
                "country": shipping_country,
                "postal_code": shipping_postal_code,
                "phone": shipping_phone,
            },
        },
        pricing_quote=_extract_pricing_quote_payload(order_data),
        items=await _build_order_items_payload(order_data),
        payment={"current": resumable_payment},
        refund=refund_payload,
        customer={
            "email": order_data.get("customer_email"),
            "name": shipping_name,
            "masked_email": _mask_email(order_data.get("customer_email", "")),
        },
        permissions={
            "can_pay": payment_status_mapped == "pending",
            "can_cancel": payment_status_mapped == "pending" and fulfillment_status_mapped == "not_fulfilled",
            "can_reorder": payment_status_mapped == "paid",
        },
    )


@router.get("/public/track", response_model=PublicTrackResponse)
async def public_track(
    request: Request,
    order_id: str = Query(...),
    email: EmailStr = Query(...),
):
    order_data = await _load_public_order_for_customer(
        request,
        order_id=order_id,
        email=email,
    )
    return _build_public_track_response(order_data)


@router.get("/public/track-by-token", response_model=PublicTrackResponse)
async def public_track_by_token(
    request: Request,
    token: str = Query(...),
):
    order_data = await _load_public_order_for_track_token(
        request,
        token=token,
    )
    return _build_public_track_response(order_data)


def _build_public_track_response(order_data: Dict[str, Any]) -> PublicTrackResponse:
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
    elif payment_status_mapped == "payment_failed":
        failed_at = order_data.get("updated_at") or created_at
        timeline.append(
            PublicTrackEvent(
                status="payment_failed",
                timestamp=failed_at.isoformat(),
                completed=True,
                description="Payment failed",
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
        order_id=str(order_data.get("order_id") or ""),
        delivery_status=delivery_status,
        timeline=timeline,
    )
