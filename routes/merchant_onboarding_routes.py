"""
Merchant Onboarding Routes - Phase 2
Handles merchant registration, KYC, PSP setup, and API key issuance
"""

from services.merchant_store_service import get_merchant_active_stores, get_primary_store
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, status, UploadFile, File, Form
from pydantic import BaseModel, EmailStr, validator
from typing import Optional, Dict, Any, List, Tuple
from datetime import datetime, timedelta
import asyncio
import re
import stripe
import httpx

from db.merchant_onboarding import (
    create_merchant_onboarding,
    get_merchant_onboarding,
    update_kyc_status,
    upload_kyc_documents,
    setup_psp_connection,
    get_all_merchant_onboardings,
    soft_delete_merchant_onboarding,
    add_kyc_document,
    merchant_onboarding  # Table object for direct queries
)
from db.payment_router import register_merchant_psp_route
from db.auth_identity import upsert_membership
from db.database import database
from readiness.summary import build_readiness_summary
from utils.auth import ADMIN_ROLES, get_current_user, get_current_employee, require_admin
from routes.manage_integrations import sync_legacy_primary_store_fields
from urllib.parse import urlparse
# from utils.r2_storage import upload_file_to_r2, get_presigned_url  # R2 存储功能推迟实现
from fastapi.responses import StreamingResponse
import io

router = APIRouter(prefix="/merchant/onboarding", tags=["merchant-onboarding"])

INTERNAL_ACCOUNT_ROLES = {"super_admin", "admin", "employee", "outsourced"}
PUBLIC_MERGEABLE_ACCOUNT_ROLES = {"agent"}

# ============================================================================
# REQUEST/RESPONSE MODELS
# ============================================================================

# Declared merchant mode: 'storefront' keeps today's behavior (store_url required,
# URL-based auto-KYB); 'store_less' onboards a brand with no storefront URL.
VALID_OPERATING_MODES = {"storefront", "store_less"}
DEFAULT_OPERATING_MODE = "storefront"
# Store-less signups are lenient auto-approved at LOW confidence, WITHOUT the
# URL-based auto-KYB. Safe because downstream serving keeps their products
# un-served until graduated/claimed.
STORE_LESS_APPROVAL_CONFIDENCE = 0.2
# Registration-first policy (founder decision 2026-07-22): EVERY signup whose
# auto-KYB fails is approved at the same low-confidence floor store_less has
# always used — no signup dead-ends in manual review / mandatory document
# upload. Adds no exposure beyond store_less (anyone could already register
# auto-approved with NO URL at all); document verification moves to the
# dashboard lifecycle, and PSP connection still live-validates real
# credentials before any transacting.
APPROVAL_FLOOR_CONFIDENCE = STORE_LESS_APPROVAL_CONFIDENCE


class MerchantRegisterRequest(BaseModel):
    business_name: str
    store_url: Optional[str] = None  # Required for storefront mode (app-enforced); omitted for store_less
    region: str  # US, EU, APAC
    contact_email: EmailStr
    contact_phone: Optional[str] = None
    website: Optional[str] = None  # Optional, for backward compatibility
    password: Optional[str] = None  # Password for merchant login (auto-generated if not provided)
    operating_mode: str = DEFAULT_OPERATING_MODE  # 'storefront' (default) or 'store_less'
    signup_source: Optional[str] = None  # Acquisition attribution (e.g. 'ai-readiness-audit')

    @validator("operating_mode", pre=True, always=True)
    def _normalize_operating_mode(cls, v):
        """Only accept storefront/store_less; anything else defaults to storefront."""
        if not v or not isinstance(v, str):
            return DEFAULT_OPERATING_MODE
        normalized = v.strip().lower()
        return normalized if normalized in VALID_OPERATING_MODES else DEFAULT_OPERATING_MODE

    @validator("signup_source", pre=True, always=True)
    def _normalize_signup_source(cls, v):
        """Attribution slug only: lowercase [a-z0-9_-], capped at 64 chars.
        Anything else becomes None rather than 422 — attribution must never
        block a registration."""
        if not v or not isinstance(v, str):
            return None
        normalized = v.strip().lower()[:64]
        if not normalized or not re.fullmatch(r"[a-z0-9_-]+", normalized):
            return None
        return normalized

class KYCUploadRequest(BaseModel):
    merchant_id: str
    documents: Dict[str, Any]  # JSON blob of documents

class PSPSetupRequest(BaseModel):
    merchant_id: str
    psp_type: str  # stripe, adyen, shoppay
    psp_key: str  # API key from PSP

class KYBUpdateRequest(BaseModel):
    """Request model for updating KYB status via JSON body"""
    status: str  # approved or rejected
    reason: Optional[str] = None  # rejection reason (optional)

# ============================================================================
# PSP VALIDATION
# ============================================================================

def validate_stripe_key_sync(api_key: str) -> bool:
    """Validate Stripe API key by calling Stripe HTTP API (sync)."""
    # 1) 格式快速校验
    if not api_key or not (api_key.startswith("sk_test_") or api_key.startswith("sk_live_")):
        print(f"🔒 Invalid Stripe key format: {api_key[:15] if api_key else 'empty'} (len={len(api_key) if api_key else 0})")
        return False

    # 2) 真实请求 Stripe /v1/account 以验证密钥有效性
    try:
        print(f"🔍 Validating Stripe key via HTTP: {api_key[:20]}...")
        resp = httpx.get(
            "https://api.stripe.com/v1/account",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10.0,
        )
        if resp.status_code == 200:
            print("✅ Stripe key valid (200)")
            return True
        if resp.status_code == 401:
            print(f"🔒 Stripe key invalid (status=401)")
            return False
        if resp.status_code == 403:
            # 403 代表密钥被识别但权限不足（受限密钥/权限配置），视为“有效但权限不足”
            print("✅ Stripe key recognized but insufficient permissions (403) — treating as valid")
            return True
        print(f"⚠️ Stripe validation unexpected status={resp.status_code}, body={resp.text[:200]}")
        return False
    except Exception as e:
        print(f"❌ Stripe HTTP validation error: {type(e).__name__}: {str(e)[:200]}")
        return False


def canonicalize_store_url(raw_url: str) -> str:
    """Normalize store url for duplicate detection: lowercase, strip scheme and trailing slash."""
    if not raw_url:
        return ""
    url = raw_url.strip()
    # Ensure scheme to parse
    if not url.startswith("http://") and not url.startswith("https://"):
        url = "https://" + url
    parsed = urlparse(url)
    host_path = (parsed.netloc + parsed.path).rstrip("/")
    return host_path.lower()


def normalize_contact_email(raw_email: str) -> str:
    """Normalize contact email so onboarding and auth use the same lookup key."""
    return (raw_email or "").strip().lower()


def normalize_account_role(raw_role: Optional[str]) -> Optional[str]:
    role = (raw_role or "").strip().lower()
    return role or None


def build_identity_conflict_detail(
    *,
    code: str,
    message: str,
    current_role: Optional[str],
    resolution: str,
    merchant_id: Optional[str] = None,
) -> Dict[str, Any]:
    detail: Dict[str, Any] = {
        "code": code,
        "message": message,
        "current_role": current_role,
        "resolution": resolution,
    }
    if merchant_id:
        detail["merchant_id"] = merchant_id
    return detail
async def get_active_onboarding_by_email(contact_email: str) -> Optional[Dict[str, Any]]:
    """Return the latest non-deleted merchant onboarding record for an email."""
    record = await database.fetch_one(
        """
        SELECT merchant_id, business_name, store_url, website, region,
               contact_email, contact_phone, status, auto_approved,
               approval_confidence, full_kyb_deadline
        FROM merchant_onboarding
        WHERE LOWER(contact_email) = LOWER(:email)
          AND COALESCE(status, 'pending_verification') != 'deleted'
        ORDER BY created_at DESC
        LIMIT 1
        """,
        {"email": contact_email},
    )
    return dict(record) if record else None


async def get_user_auth_binding(contact_email: str) -> Optional[Dict[str, Any]]:
    """Return the existing auth user row for an email if it exists."""
    record = await database.fetch_one(
        """
        SELECT id, email, role, merchant_id, active, password_hash, full_name
        FROM users
        WHERE LOWER(email) = LOWER(:email)
        LIMIT 1
        """,
        {"email": contact_email},
    )
    return dict(record) if record else None


async def sync_merchant_auth_membership(
    *,
    contact_email: str,
    business_name: str,
    merchant_id: str,
    password_hash: Optional[str] = None,
    credential_source: Optional[str] = None,
) -> None:
    try:
        await upsert_membership(
            email=contact_email,
            membership_type="merchant",
            role="merchant",
            entity_id=merchant_id,
            status="active",
            full_name=business_name,
            password_hash=password_hash,
            credential_source=credential_source,
            source="merchant_onboarding_sync",
        )
    except Exception as exc:
        print(f"⚠️ Merchant canonical auth membership sync skipped: {type(exc).__name__}: {exc}")


async def sync_merchant_auth_user(
    *,
    contact_email: str,
    business_name: str,
    merchant_id: str,
    password: Optional[str],
    existing_user: Optional[Dict[str, Any]] = None,
) -> None:
    """Create or update the merchant auth account tied to this onboarding record."""
    from utils.auth import hash_password

    password_hash = hash_password(password) if password else None

    if existing_user:
        await database.execute(
            """
            UPDATE users
            SET password_hash = COALESCE(:password_hash, password_hash),
                full_name = :full_name,
                role = :role,
                active = :active,
                merchant_id = :merchant_id
            WHERE id = :user_id
            """,
            {
                "password_hash": password_hash,
                "full_name": business_name,
                "role": "merchant",
                "active": True,
                "merchant_id": merchant_id,
                "user_id": existing_user["id"],
            },
        )
        await sync_merchant_auth_membership(
            contact_email=contact_email,
            business_name=business_name,
            merchant_id=merchant_id,
            password_hash=password_hash,
            credential_source="merchant_sync" if password_hash else None,
        )
        return

    if not password:
        raise ValueError("password is required when creating a merchant auth user")
    await database.execute(
        """
        INSERT INTO users (email, password_hash, full_name, role, active, merchant_id)
        VALUES (:email, :password_hash, :full_name, :role, :active, :merchant_id)
        """,
        {
            "email": contact_email,
            "password_hash": password_hash,
            "full_name": business_name,
            "role": "merchant",
            "active": True,
            "merchant_id": merchant_id,
        },
    )
    await sync_merchant_auth_membership(
        contact_email=contact_email,
        business_name=business_name,
        merchant_id=merchant_id,
        password_hash=password_hash,
        credential_source="merchant_sync",
    )

async def resolve_public_merchant_identity_merge(
    *,
    existing_user: Optional[Dict[str, Any]],
    existing_onboarding: Optional[Dict[str, Any]],
    entered_password: Optional[str],
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """
    Decide whether a public merchant signup can reuse/convert an existing identity.

    Rules:
    - merchant role: reuse directly
    - agent role: allow conversion only when the current password is verified
    - internal roles: require an explicit admin merge path
    """
    if not existing_user:
        return None, None

    normalized_role = normalize_account_role(existing_user.get("role"))
    if normalized_role in {None, "merchant"}:
        return existing_user, None

    merchant_id = (
        (existing_onboarding or {}).get("merchant_id")
        or existing_user.get("merchant_id")
    )

    if normalized_role in INTERNAL_ACCOUNT_ROLES:
        raise HTTPException(
            status_code=409,
            detail=build_identity_conflict_detail(
                code="MERCHANT_IDENTITY_MERGE_REQUIRED",
                message=(
                    "This email is already attached to an internal account. "
                    "Pivota support must merge it into a merchant identity before signup can continue."
                ),
                current_role=normalized_role,
                resolution="admin_merge_required",
                merchant_id=merchant_id,
            ),
        )

    if normalized_role in PUBLIC_MERGEABLE_ACCOUNT_ROLES:
        from utils.auth import verify_password

        if not entered_password or not verify_password(
            entered_password,
            str(existing_user.get("password_hash") or ""),
        ):
            raise HTTPException(
                status_code=409,
                detail=build_identity_conflict_detail(
                    code="MERCHANT_IDENTITY_PASSWORD_VERIFICATION_REQUIRED",
                    message=(
                        "This email already has an existing account. "
                        "Enter the current account password to convert it into a merchant identity, "
                        "or contact support to merge it."
                    ),
                    current_role=normalized_role,
                    resolution="verify_current_password_or_admin_merge",
                    merchant_id=merchant_id,
                ),
            )

        return existing_user, {
            "converted_from_role": normalized_role,
            "message": "Existing account verified and converted into the merchant identity.",
        }

    raise HTTPException(
        status_code=409,
        detail=build_identity_conflict_detail(
            code="MERCHANT_IDENTITY_MERGE_REQUIRED",
            message=(
                "This email is already attached to another account type. "
                "Pivota support must merge it into a merchant identity before signup can continue."
            ),
            current_role=normalized_role,
            resolution="admin_merge_required",
            merchant_id=merchant_id,
        ),
    )

async def validate_stripe_key(api_key: str) -> bool:
    """Async wrapper for Stripe validation"""
    import concurrent.futures
    loop = asyncio.get_event_loop()
    with concurrent.futures.ThreadPoolExecutor() as pool:
        return await loop.run_in_executor(pool, validate_stripe_key_sync, api_key)

async def validate_adyen_key(api_key: str) -> bool:
    """Validate Adyen API key by making a lightweight test request.

    Updated behavior:
    - 200  => Treat as valid (key + merchantAccount OK).
    - 422  => Treat as valid (request shape accepted, data needs refinement).
    - 401/403 => Treat as invalid and surface clear error to the merchant
                instead of silently accepting.
    Any other status or network error => invalid.
    """
    try:
        from config.settings import settings

        merchant_account = settings.adyen_merchant_account or "TEST"
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://checkout-test.adyen.com/v71/paymentMethods",
                headers={
                    "X-API-Key": api_key,
                    "Content-Type": "application/json",
                },
                json={"merchantAccount": merchant_account},
                timeout=10.0,
            )

        status = response.status_code
        body_snippet = response.text[:200]

        if status == 200:
            print("✅ Adyen key and merchantAccount validated successfully (200)")
            return True

        if status == 422:
            # Request is well-formed and key/header accepted; treat as valid but log details.
            print(
                f"✅ Adyen key accepted (422 validation error) – treating as valid. Body={body_snippet}"
            )
            return True

        if status in (401, 403):
            # Explicitly reject bad credentials / merchantAccount mismatch
            print(
                f"❌ Adyen credentials rejected (status={status}). Response={body_snippet}"
            )
            return False

        print(
            f"⚠️ Adyen validation unexpected status={status}, body={body_snippet}"
        )
        return False
    except Exception as e:
        # Network or environment errors => treat as invalid to avoid false completion
        print(f"⚠️ Adyen validation error: {e}")
        return False

async def validate_psp_credentials(psp_type: str, api_key: str) -> tuple[bool, str]:
    """
    Validate PSP credentials
    Returns: (is_valid, error_message)
    """
    if psp_type == "stripe":
        is_valid = await validate_stripe_key(api_key)
        return (is_valid, "" if is_valid else "Invalid Stripe API key. Please check your key and try again.")
    
    elif psp_type == "adyen":
        is_valid = await validate_adyen_key(api_key)
        return (is_valid, "" if is_valid else "Invalid Adyen API key. Please check your key and try again.")
    
    elif psp_type == "shoppay":
        # ShopPay validation would go here
        # For now, accept any non-empty key
        return (bool(api_key), "" if api_key else "ShopPay API key is required")
    
    else:
        return (False, f"Unsupported PSP type: {psp_type}")

# ============================================================================
# BACKGROUND TASKS
# ============================================================================

async def auto_approve_kyc(merchant_id: str):
    """Simulate KYC verification - auto-approve after 5 seconds"""
    await asyncio.sleep(5)  # Simulate review time
    await update_kyc_status(merchant_id, "approved")
    print(f"✅ Auto-approved KYC for merchant: {merchant_id}")

# ============================================================================
# ENDPOINTS
# ============================================================================

@router.post("/register", response_model=Dict[str, Any])
async def register_merchant(
    merchant_data: MerchantRegisterRequest,
    background_tasks: BackgroundTasks
):
    """
    Step 1: Register a new merchant with intelligent auto-approval
    快速自动审批：验证 Store URL 和名称匹配后自动批准，允许立即连接PSP
    """
    try:
        # Pre-flight connection check to avoid aborted transaction state
        try:
            await database.execute("SELECT 1")
        except Exception as pre_err:
            if "transaction is aborted" in str(pre_err).lower():
                # Attempt rollback then reconnect
                try:
                    await database.execute("ROLLBACK")
                except Exception:
                    pass
                await database.disconnect()
                await asyncio.sleep(0.5)
                await database.connect()

        normalized_email = normalize_contact_email(merchant_data.contact_email)
        # operating_mode is normalized by the request validator to storefront/store_less.
        operating_mode = merchant_data.operating_mode or DEFAULT_OPERATING_MODE
        is_store_less = operating_mode == "store_less"

        # Storefront still requires a store URL — enforce in the app even though
        # the column is now nullable (store_less is the only URL-free path).
        if not is_store_less and not (merchant_data.store_url or "").strip():
            raise HTTPException(
                status_code=422,
                detail="store_url is required for storefront onboarding.",
            )

        normalized_store_url = canonicalize_store_url(merchant_data.store_url)

        # 0. 收紧 email 绑定，避免创建无法登录的孤立 merchant 记录。
        existing_onboarding = await get_active_onboarding_by_email(normalized_email)
        reuse_existing_onboarding = False
        if existing_onboarding:
            existing_norm_url = canonicalize_store_url(existing_onboarding.get("store_url") or "")
            if existing_norm_url and existing_norm_url != normalized_store_url:
                raise HTTPException(
                    status_code=409,
                    detail="Email already registered to another merchant. Sign in or use a different email.",
                )
            reuse_existing_onboarding = True

        existing_user = await get_user_auth_binding(normalized_email)
        existing_user, identity_merge = await resolve_public_merchant_identity_merge(
            existing_user=existing_user,
            existing_onboarding=existing_onboarding,
            entered_password=merchant_data.password,
        )

        if existing_user and existing_user.get("merchant_id") and not reuse_existing_onboarding:
            linked_onboarding = await get_merchant_onboarding(existing_user["merchant_id"])
            if linked_onboarding and linked_onboarding.get("status") != "deleted":
                raise HTTPException(
                    status_code=409,
                    detail="Email already registered. Sign in or reset password to continue.",
                )

        # 1. 去重检查（基于标准化 store_url）
        from datetime import datetime

        if reuse_existing_onboarding:
            merchant_id = existing_onboarding["merchant_id"]
            await database.execute(
                merchant_onboarding.update()
                .where(merchant_onboarding.c.merchant_id == merchant_id)
                .values(
                    business_name=merchant_data.business_name,
                    store_url=merchant_data.store_url,
                    website=merchant_data.website,
                    region=merchant_data.region,
                    contact_email=normalized_email,
                    contact_phone=merchant_data.contact_phone,
                    updated_at=datetime.now(),
                )
            )
            print(f"✅ Recovered existing merchant onboarding: {merchant_id}")

            full_kyb_deadline = existing_onboarding.get("full_kyb_deadline")
            validation_result = {
                "approved": bool(
                    existing_onboarding.get("auto_approved")
                    or existing_onboarding.get("status") == "approved"
                ),
                "confidence_score": existing_onboarding.get("approval_confidence") or 0,
                "validation_results": {
                    "recovery": {
                        "match": True,
                        "message": "Recovered existing onboarding for the same email and storefront.",
                    }
                },
                "full_kyb_deadline": (
                    full_kyb_deadline.isoformat()
                    if full_kyb_deadline and hasattr(full_kyb_deadline, "isoformat")
                    else full_kyb_deadline
                ),
            }
        elif is_store_less:
            # Declared store-less brand: no storefront URL to dedupe or KYB against.
            # Lenient auto-approve at LOW confidence WITHOUT URL-based auto-KYB.
            # Safe because downstream serving keeps store_less products un-served
            # until graduated/claimed.
            print("🏷️ Store-less onboarding: skipping store_url dup check + auto-KYB")
            validation_result = {
                "approved": True,
                "confidence_score": STORE_LESS_APPROVAL_CONFIDENCE,
                "validation_results": {
                    "store_less": {
                        "match": True,
                        "message": (
                            "Store-less brand auto-approved at low confidence; "
                            "gated downstream until graduated/claimed."
                        ),
                    }
                },
                "requires_full_kyb": True,
                "full_kyb_deadline": (datetime.utcnow() + timedelta(days=7)).isoformat(),
            }

            # 3. 创建商户记录（store_url=None, operating_mode='store_less'）
            merchant_dict = merchant_data.dict()
            merchant_dict["contact_email"] = normalized_email
            merchant_dict["store_url"] = None
            merchant_dict["operating_mode"] = "store_less"
            # Remove password field as it's not in the merchant_onboarding table
            merchant_dict.pop('password', None)
            merchant_id = await create_merchant_onboarding(merchant_dict)
            print(f"✅ Store-less merchant created: {merchant_id}")
        else:
            dup = await database.fetch_one(
                merchant_onboarding.select().where(
                    (merchant_onboarding.c.store_url == merchant_data.store_url) |
                    (merchant_onboarding.c.store_url == normalized_store_url)
                )
            )
            if dup:
                raise HTTPException(status_code=409, detail="Store URL already registered")

            # 2. 自动 KYB 预审批验证（含 Shopify 域名快速通道）
            print("🔍 Starting auto-KYB pre-approval validation...")
            try:
                from utils.auto_kyb_validator import auto_kyb_pre_approval
                print("✅ auto_kyb_validator imported successfully")
            except Exception as import_err:
                print(f"❌ Failed to import auto_kyb_validator: {import_err}")
                import traceback
                traceback.print_exc()
                raise

            try:
                # Shopify 域名快速通道：*.myshopify.com 直接视为可用平台域
                parsed = urlparse(merchant_data.store_url if merchant_data.store_url.startswith(('http://','https://')) else ('https://' + merchant_data.store_url))
                if parsed.netloc.endswith('.myshopify.com'):
                    confidence = 0.96
                    validation_result = {
                        "approved": True,
                        "confidence_score": confidence,
                        "validation_results": {
                            "url_validation": {"valid": True, "message": f"Known e-commerce platform detected: {parsed.netloc}"},
                            "name_match": {"match": True, "message": "Trusted platform auto-approval", "score": 0.9},
                        },
                        "requires_full_kyb": True,
                        "full_kyb_deadline": (datetime.utcnow() + timedelta(days=7)).isoformat(),
                    }
                else:
                    validation_result = await auto_kyb_pre_approval(
                        business_name=merchant_data.business_name,
                        store_url=merchant_data.store_url,
                        region=merchant_data.region
                    )
                print(f"✅ Auto-KYB validation completed: {validation_result}")
            except Exception as val_err:
                print(f"❌ Auto-KYB validation failed: {val_err}")
                import traceback
                traceback.print_exc()
                # Continue without auto-approval
                validation_result = {"approved": False, "confidence_score": 0, "validation_results": {}}

            # Approval floor: no signup dead-ends in manual review just
            # because auto-KYB could not verify the URL. Approve at the
            # store_less low-confidence floor, keeping the original KYB
            # findings on record and the 7-day full-KYB deadline.
            if not validation_result.get("approved"):
                print("🪜 Applying low-confidence approval floor (auto-KYB did not approve)")
                validation_result = {
                    "approved": True,
                    "confidence_score": APPROVAL_FLOOR_CONFIDENCE,
                    "validation_results": {
                        **(validation_result.get("validation_results") or {}),
                        "approval_floor": {
                            "match": True,
                            "message": (
                                "Auto-approved at low confidence despite "
                                "failed auto-KYB (registration-first policy); "
                                "PSP connection still requires live "
                                "credential validation."
                            ),
                        },
                    },
                    "requires_full_kyb": True,
                    "full_kyb_deadline": (datetime.utcnow() + timedelta(days=7)).isoformat(),
                }

            # 3. 创建商户记录
            merchant_dict = merchant_data.dict()
            merchant_dict["contact_email"] = normalized_email
            # Remove password field as it's not in the merchant_onboarding table
            merchant_dict.pop('password', None)
            merchant_id = await create_merchant_onboarding(merchant_dict)
            print(f"✅ Merchant created: {merchant_id}")
        
        # 3.5 创建/更新用户登录账户
        try:
            import secrets
            
            # Generate password if not provided
            password = merchant_data.password if merchant_data.password else secrets.token_urlsafe(12)
            
            print(f"🔐 Syncing merchant auth user for {normalized_email}")
            await sync_merchant_auth_user(
                contact_email=normalized_email,
                business_name=merchant_data.business_name,
                merchant_id=merchant_id,
                password=password,
                existing_user=existing_user,
            )
            print(f"✅ Merchant auth user synced for {normalized_email} with merchant_id {merchant_id}")
        except Exception as user_err:
            import traceback
            print(f"❌ Failed to create user account: {user_err}")
            print(f"Full traceback: {traceback.format_exc()}")
            # IMPORTANT: Fail registration if user creation fails
            raise HTTPException(
                status_code=500,
                detail=f"Failed to create user account: {str(user_err)}"
            )
        
        # 3. 如果自动批准，立即更新状态为 approved
        if not reuse_existing_onboarding and validation_result["approved"]:
            print(f"🎉 Auto-approving merchant {merchant_id}...")
            await update_kyc_status(merchant_id, "approved")
            # Update additional fields
            query = merchant_onboarding.update().where(
                merchant_onboarding.c.merchant_id == merchant_id
            ).values(
                auto_approved=True,
                approval_confidence=validation_result["confidence_score"],
                full_kyb_deadline=datetime.fromisoformat(validation_result["full_kyb_deadline"]) if validation_result.get("full_kyb_deadline") else None
            )
            await database.execute(query)
            print(f"✅ Merchant {merchant_id} auto-approved successfully")
        
        # Build response
        response_message = (
            "Merchant account recovered. Continue onboarding in the portal."
            if reuse_existing_onboarding
            else (
                "✅ Registration approved! You can now connect your PSP and start processing payments.\n"
                "⚠️ Please complete full KYB documentation within 7 days."
                if validation_result["approved"]
                else "Registration received. Manual review required before PSP connection."
            )
        )

        response_data = {
            "status": "success",
            "message": response_message,
            "merchant_id": merchant_id,
            "auto_approved": validation_result["approved"],
            "confidence_score": validation_result["confidence_score"],
            "validation_details": validation_result["validation_results"],
            "full_kyb_deadline": validation_result.get("full_kyb_deadline"),
            "next_step": "Connect PSP" if validation_result["approved"] else "Wait for admin approval",
            "login_email": normalized_email
        }

        if identity_merge:
            response_data["identity_merge"] = identity_merge

        # Include password if auto-generated
        if not merchant_data.password and 'password' in locals():
            response_data["temporary_password"] = password
            response_data["message"] += f"\n\n🔑 Your login credentials:\nEmail: {merchant_data.contact_email}\nPassword: {password}\n(Please change this password after first login)"
        
        return response_data
    except HTTPException:
        # Propagate FastAPI HTTP errors (e.g. 409 duplicate store URL)
        raise
    except Exception as e:
        import traceback
        error_msg = str(e)
        print(f"❌ Registration error: {error_msg}")
        print(f"Full traceback: {traceback.format_exc()}")
        
        # Handle database transaction errors
        if "transaction is aborted" in error_msg.lower():
            print("🔄 Attempting to recover from transaction error...")
            try:
                await database.disconnect()
                await asyncio.sleep(1)
                await database.connect()
                print("✅ Database reconnected, retrying once...")
                # Retry once after reconnection
                try:
                    merchant_dict = merchant_data.dict()
                    # Remove password field as it's not in the merchant_onboarding table
                    merchant_dict.pop('password', None)
                    merchant_id = await create_merchant_onboarding(merchant_dict)
                    background_tasks.add_task(auto_approve_kyc, merchant_id)
                    return {
                        "status": "success",
                        "message": "Merchant registered successfully. KYC verification in progress.",
                        "merchant_id": merchant_id,
                        "next_step": "Upload KYC documents or wait for auto-verification"
                    }
                except Exception as retry_err:
                    print(f"⚠️ Retry after reconnect failed: {retry_err}")
            except Exception as reconnect_error:
                print(f"⚠️ Database reconnect attempt failed: {reconnect_error}")
            # Always return 503 to prompt a retry on the client
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Database connection reset. Please try your request again."
            )
        
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to register merchant: {str(e)}"
        )

@router.post("/kyc/upload", response_model=Dict[str, Any])
async def upload_kyc(kyc_data: KYCUploadRequest):
    """
    Step 2: Upload KYC documents (simulated as JSON blob)
    """
    merchant = await get_merchant_onboarding(kyc_data.merchant_id)
    if not merchant:
        raise HTTPException(status_code=404, detail="Merchant not found")
    
    await upload_kyc_documents(kyc_data.merchant_id, kyc_data.documents)
    
    return {
        "status": "success",
        "message": "KYC documents uploaded successfully",
        "merchant_id": kyc_data.merchant_id
    }

@router.post("/kyb/{merchant_id}", response_model=Dict[str, Any])
async def update_kyb_status_alias(
    merchant_id: str,
    kyb_update: KYBUpdateRequest,
    current_user: dict = Depends(require_admin)
):
    """
    Admin: Update KYB status via JSON body
    Accepts: {"status": "approved" | "rejected", "reason": "optional rejection reason"}
    """
    merchant = await get_merchant_onboarding(merchant_id)
    if not merchant:
        raise HTTPException(status_code=404, detail="Merchant not found")
    await update_kyc_status(merchant_id, kyb_update.status, kyb_update.reason)
    return {"status": "success", "merchant_id": merchant_id, "new_status": kyb_update.status}

@router.post("/psp/setup", response_model=Dict[str, Any])
async def setup_psp(
    psp_data: PSPSetupRequest,
    current_user: dict = Depends(get_current_user)
):
    """
    Step 3: Setup PSP connection and get merchant API key
    Only allowed if KYC is approved
    Now with REAL PSP credential validation!
    """
    if current_user.get("merchant_id") != psp_data.merchant_id and current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="merchant_id does not match authenticated user")

    merchant = await get_merchant_onboarding(psp_data.merchant_id)
    
    if not merchant:
        raise HTTPException(status_code=404, detail="Merchant not found")
    
    # Allow PSP setup if merchant is approved OR auto-approved
    is_auto_approved = merchant.get("auto_approved", False)
    if merchant["status"] != "approved" and not is_auto_approved:
        raise HTTPException(
            status_code=400,
            detail=f"KYC must be approved first. Current status: {merchant['status']}, auto_approved: {is_auto_approved}"
        )
    
    if merchant["psp_connected"]:
        return {
            "status": "success",
            "message": "PSP already connected",
            "merchant_id": merchant["merchant_id"],
            "psp_type": merchant.get("psp_type"),
            "validated": True
        }
    
    # ✨ NEW: Validate PSP credentials
    print(f"🔍 Validating {psp_data.psp_type} credentials...")
    try:
        is_valid, error_message = await validate_psp_credentials(
            psp_data.psp_type,
            psp_data.psp_key
        )
    except Exception as e:
        import traceback
        print(f"❌ PSP validation raised exception: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"PSP validation error: {str(e)}")
    
    if not is_valid:
        print(f"❌ PSP validation failed: {error_message}")
        raise HTTPException(
            status_code=400,
            detail=error_message
        )
    
    print(f"✅ {psp_data.psp_type} credentials validated successfully")
    
    # Setup PSP and generate API key
    try:
        result = await setup_psp_connection(
            psp_data.merchant_id,
            psp_data.psp_type,
            psp_data.psp_key
        )
    except Exception as e:
        import traceback
        print(f"❌ setup_psp_connection failed: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Failed to persist PSP connection: {str(e)}")
    
    # Register merchant in payment router for unified /payment/execute
    try:
        await register_merchant_psp_route(
            merchant_id=psp_data.merchant_id,
            psp_type=psp_data.psp_type,
            # 存为JSON对象，避免JSON列存储纯字符串时的类型问题
            psp_credentials={"api_key": psp_data.psp_key}
        )
    except Exception as e:
        import traceback
        print(f"❌ register_merchant_psp_route failed: {e}")
        traceback.print_exc()
        # 不阻断主流程，但提示客户端路由注册失败
        return {
            "status": "success",
            "message": f"{psp_data.psp_type.title()} connected. Routing registration failed: {str(e)}",
            "merchant_id": result["merchant_id"],
            "api_key": result["api_key"],
            "psp_type": result["psp_type"],
            "validated": True,
            "next_step": "Use this API key to call /payment/execute with header 'X-Merchant-API-Key'"
        }
    
    return {
        "status": "success",
        "message": f"{psp_data.psp_type.title()} connected successfully. Credentials validated and registered in payment router.",
        "merchant_id": result["merchant_id"],
        "api_key": result["api_key"],  # Return API key ONCE - save this!
        "psp_type": result["psp_type"],
        "validated": True,  # NEW: Indicate credentials were validated
        "next_step": "Use this API key to call /payment/execute with header 'X-Merchant-API-Key'"
    }

@router.get("/status/{merchant_id}", response_model=Dict[str, Any])
async def get_onboarding_status(merchant_id: str):
    """
    Get merchant onboarding status (KYC + PSP setup)
    """
    merchant = await get_merchant_onboarding(merchant_id)
    
    if not merchant:
        raise HTTPException(status_code=404, detail="Merchant not found")
    
    return {
        "status": "success",
        "merchant_id": merchant["merchant_id"],
        "business_name": merchant["business_name"],
        "kyc_status": merchant["status"],
        "operating_mode": merchant.get("operating_mode") or "storefront",
        "psp_connected": merchant["psp_connected"],
        "psp_type": merchant.get("psp_type"),
        "api_key_issued": bool(merchant.get("api_key")),
        "created_at": merchant["created_at"].isoformat() if merchant["created_at"] else None,
        "verified_at": merchant["verified_at"].isoformat() if merchant.get("verified_at") else None,
    }

@router.get("/details/{merchant_id}", response_model=Dict[str, Any])
async def get_onboarding_details(
    merchant_id: str,
    current_user: dict = Depends(get_current_user)  # Allow all authenticated users
):
    """
    Get full onboarding merchant details including KYB documents, stores, and PSPs
    """
    merchant = await get_merchant_onboarding(merchant_id)
    if not merchant:
        raise HTTPException(status_code=404, detail="Merchant not found")
    
    # Debug logging
    kyc_docs = merchant.get("kyc_documents")
    print(f"🔍 DEBUG /details/{merchant_id}:")
    print(f"   kyc_documents type: {type(kyc_docs)}")
    print(f"   kyc_documents value: {kyc_docs}")
    print(f"   kyc_documents length: {len(kyc_docs) if kyc_docs else 0}")
    
    # Fetch connected stores
    stores = []
    try:
        stores_query = """SELECT store_id, platform, name, domain, status, connected_at, product_count
                          FROM merchant_stores
                          WHERE merchant_id = :merchant_id
                            AND lower(COALESCE(status, 'active')) NOT IN ('disconnected', 'deleted', 'inactive')
                          ORDER BY connected_at DESC"""
        stores_rows = await database.fetch_all(stores_query, {"merchant_id": merchant_id})
        stores = [dict(row) for row in stores_rows]
    except Exception as e:
        print(f"Error fetching stores: {e}")
    
    # Fetch connected PSPs
    psps = []
    try:
        psps_query = """SELECT psp_id, provider, name, account_id, status, connected_at, capabilities
                        FROM merchant_psps 
                        WHERE merchant_id = :merchant_id
                        ORDER BY connected_at DESC"""
        psps_rows = await database.fetch_all(psps_query, {"merchant_id": merchant_id})
        psps = [dict(row) for row in psps_rows]
    except Exception as e:
        print(f"Error fetching PSPs: {e}")
    
    # Fetch transaction stats
    stats = {"total_transactions": 0, "total_revenue": 0, "unique_customers": 0}
    try:
        stats_query = """SELECT COUNT(*) as total_transactions, 
                         COALESCE(SUM(amount), 0) as total_revenue,
                         COUNT(DISTINCT customer_email) as unique_customers
                         FROM orders WHERE merchant_id = :merchant_id"""
        stats_row = await database.fetch_one(stats_query, {"merchant_id": merchant_id})
        if stats_row:
            stats = dict(stats_row)
    except Exception as e:
        print(f"Error fetching stats: {e}")
    
    # Normalize response for frontend expectations
    result = {
        "merchant_id": merchant["merchant_id"],
        "business_name": merchant["business_name"],
        "email": merchant.get("contact_email"),
        "phone": merchant.get("contact_phone"),
        "store_url": merchant.get("store_url"),
        "website": merchant.get("website"),
        "platform": merchant.get("region"),  # reuse region as platform label
        "status": merchant["status"],
        "kyc_documents": merchant.get("kyc_documents") or [],
        "psp_connected": merchant.get("psp_connected", False),
        "psp_type": merchant.get("psp_type"),
        "created_at": merchant.get("created_at").isoformat() if merchant.get("created_at") else None,
        "readiness_summary": (await build_readiness_summary(merchant_id)).model_dump(),
        "stores": stores,
        "psps": psps,
        "stats": stats
    }
    return {"status": "success", "merchant": result}


@router.delete("/{merchant_id}/stores/{store_id}", response_model=Dict[str, Any])
async def admin_delete_merchant_store(
    merchant_id: str,
    store_id: str,
    current_user: dict = Depends(get_current_employee),
):
    """Employee/admin: soft-delete a specific store from a merchant account.

    Reversible by design: sets status='deleted' rather than removing the row, so a
    mis-click can be undone. Soft-deleted stores are excluded from all active-store
    reads (get_merchant_active_stores) and from the employee portal views.
    """
    del current_user

    store = await database.fetch_one(
        "SELECT store_id, is_primary FROM merchant_stores WHERE store_id = :store_id AND merchant_id = :merchant_id",
        {"store_id": store_id, "merchant_id": merchant_id},
    )
    if not store:
        raise HTTPException(status_code=404, detail="Store not found for this merchant")

    store_data = dict(store)

    async with database.transaction():
        # Clear is_primary as we soft-delete so the partial unique index
        # (uniq_merchant_stores_primary_per_merchant WHERE is_primary=TRUE) stays
        # satisfiable when the next store is promoted below.
        await database.execute(
            "UPDATE merchant_stores SET status = 'deleted', is_primary = FALSE WHERE store_id = :store_id AND merchant_id = :merchant_id",
            {"store_id": store_id, "merchant_id": merchant_id},
        )
        if store_data.get("is_primary"):
            await database.execute(
                """
                WITH next_store AS (
                    SELECT store_id FROM merchant_stores
                    WHERE merchant_id = :merchant_id
                      AND lower(COALESCE(status, '')) IN ('active', 'connected')
                    ORDER BY connected_at DESC NULLS LAST, store_id DESC
                    LIMIT 1
                )
                UPDATE merchant_stores SET is_primary = TRUE
                WHERE store_id = (SELECT store_id FROM next_store)
                """,
                {"merchant_id": merchant_id},
            )

    # After the transaction: sync legacy mcp_* fields to whichever store is now primary.
    next_primary_full = await database.fetch_one(
        """SELECT store_id, platform, domain, api_key
           FROM merchant_stores
           WHERE merchant_id = :merchant_id AND is_primary = TRUE
           LIMIT 1""",
        {"merchant_id": merchant_id},
    )
    if next_primary_full:
        await sync_legacy_primary_store_fields(merchant_id, dict(next_primary_full))
    else:
        try:
            await database.execute(
                """UPDATE merchant_onboarding
                   SET mcp_connected = FALSE, mcp_platform = NULL,
                       mcp_shop_domain = NULL, mcp_access_token = NULL
                   WHERE merchant_id = :merchant_id""",
                {"merchant_id": merchant_id},
            )
        except Exception:
            pass

    # Soft-deleting the last serving store must also close the public-search
    # door: recall gates on catalog_merchants.status, which no lifecycle path
    # used to write (#1648). Re-derived from the remaining stores; never raises.
    from services.store_lifecycle_service import sync_catalog_merchant_status

    await sync_catalog_merchant_status(merchant_id, reason="store_soft_deleted")

    return {"status": "success", "store_id": store_id, "merchant_id": merchant_id}


@router.post("/{merchant_id}/stores/repair-primary", response_model=Dict[str, Any])
async def admin_repair_primary_store(
    merchant_id: str,
    current_user: dict = Depends(get_current_employee),
):
    """Re-sync mcp_* fields on merchant_onboarding to whichever store is currently primary.
    Use after a store deletion left the legacy fields pointing at the deleted store.
    """
    del current_user
    primary = await database.fetch_one(
        """SELECT store_id, platform, domain, api_key
           FROM merchant_stores
           WHERE merchant_id = :merchant_id AND is_primary = TRUE
           LIMIT 1""",
        {"merchant_id": merchant_id},
    )
    if not primary:
        # No primary — pick the most recently connected active store and promote it.
        primary = await database.fetch_one(
            """SELECT store_id, platform, domain, api_key
               FROM merchant_stores
               WHERE merchant_id = :merchant_id
                 AND lower(COALESCE(status, '')) IN ('active', 'connected')
               ORDER BY connected_at DESC NULLS LAST
               LIMIT 1""",
            {"merchant_id": merchant_id},
        )
        if primary:
            await database.execute(
                "UPDATE merchant_stores SET is_primary = TRUE WHERE store_id = :store_id",
                {"store_id": primary["store_id"]},
            )
    if primary:
        await sync_legacy_primary_store_fields(merchant_id, dict(primary))
        return {"status": "success", "synced_to": primary["store_id"], "platform": primary["platform"], "domain": primary["domain"]}
    # No active stores at all — clear legacy fields.
    await database.execute(
        """UPDATE merchant_onboarding
           SET mcp_connected = FALSE, mcp_platform = NULL,
               mcp_shop_domain = NULL, mcp_access_token = NULL
           WHERE merchant_id = :merchant_id""",
        {"merchant_id": merchant_id},
    )
    return {"status": "success", "synced_to": None, "note": "no active stores remain"}


@router.get("/all", response_model=Dict[str, Any])
async def list_all_onboardings(
    status: Optional[str] = None,
    include_deleted: bool = False,
    current_user: dict = Depends(get_current_user)  # Allow authenticated users (employee/admin/agent)
):
    """
    Admin: List all merchant onboardings (filtered by status if provided)
    """
    try:
        merchants = await get_all_merchant_onboardings(status, include_deleted=include_deleted)

        # Previously: for each merchant, three awaits in series (PSP lookup,
        # products_cache aggregate, build_readiness_summary) — and merchants
        # were processed one-at-a-time. With N merchants that is 3*N
        # sequential round-trips and is the load-bearing slow path the
        # employee portal dashboard times out on. Run the three per-merchant
        # queries concurrently for each merchant, and run merchants
        # concurrently with a bounded semaphore so we don't blow out the
        # connection pool on installs with many merchants.
        sem = asyncio.Semaphore(8)

        async def _fetch_psp_row(merchant_id: str):
            try:
                return await database.fetch_one(
                    """
                    SELECT provider
                    FROM merchant_psps
                    WHERE merchant_id = :merchant_id AND status = 'active'
                    ORDER BY connected_at DESC
                    LIMIT 1
                    """,
                    {"merchant_id": merchant_id},
                )
            except Exception:
                return None

        async def _fetch_product_info(merchant_id: str):
            try:
                return await database.fetch_one(
                    """SELECT
                           COUNT(*) as count,
                           MAX(cached_at) as last_synced,
                           COUNT(CASE WHEN expires_at < NOW() THEN 1 END) as expired_count
                       FROM products_cache
                       WHERE merchant_id = :merchant_id""",
                    {"merchant_id": merchant_id},
                )
            except Exception:
                return None

        async def _enrich(m):
            async with sem:
                psp_row, product_info, readiness = await asyncio.gather(
                    _fetch_psp_row(m["merchant_id"]),
                    _fetch_product_info(m["merchant_id"]),
                    build_readiness_summary(m["merchant_id"]),
                )

            psp_connected = m.get("psp_connected", False) or bool(psp_row)
            psp_type = m.get("psp_type") or (psp_row["provider"] if psp_row else None)
            product_count = product_info["count"] if product_info else 0
            last_synced = product_info["last_synced"] if product_info else None
            expired_count = product_info["expired_count"] if product_info else 0

            return {
                "merchant_id": m["merchant_id"],
                "business_name": m["business_name"],
                "store_url": m.get("store_url") or "N/A",
                "region": m.get("region") or "Unknown",
                "contact_email": m.get("contact_email"),
                "status": m["status"],
                "auto_approved": m.get("auto_approved", False),
                "approval_confidence": m.get("approval_confidence"),
                "full_kyb_deadline": m.get("full_kyb_deadline").isoformat() if m.get("full_kyb_deadline") else None,
                "psp_connected": psp_connected,
                "psp_type": psp_type,
                "mcp_connected": m.get("mcp_connected", False),
                "mcp_platform": m.get("mcp_platform"),
                "product_count": product_count,
                "last_synced": last_synced.isoformat() if last_synced else None,
                "products_expired": expired_count > 0,
                "expired_count": expired_count,
                "created_at": m["created_at"].isoformat() if m["created_at"] else None,
                "readiness_summary": readiness.model_dump(),
            }

        merchant_list = await asyncio.gather(*(_enrich(m) for m in merchants))

        return {
            "status": "success",
            "count": len(merchant_list),
            "merchants": merchant_list
        }
    except Exception as e:
        print(f"❌ Error listing merchant onboardings: {e}")
        import traceback
        traceback.print_exc()
        # Return empty list instead of error to prevent frontend crash
        return {
            "status": "success",
            "count": 0,
            "merchants": [],
            "note": "Database may be initializing. Try registering a merchant first."
        }

@router.post("/approve/{merchant_id}", response_model=Dict[str, Any])
async def manual_approve_kyc(
    merchant_id: str,
    current_user: dict = Depends(require_admin)
):
    """
    Admin: Manually approve merchant KYC
    After rejection, merchant can upload new documents and be re-approved
    """
    merchant = await get_merchant_onboarding(merchant_id)
    if not merchant:
        raise HTTPException(status_code=404, detail="Merchant not found")
    
    # Clear rejection reason when approving
    await update_kyc_status(merchant_id, "approved", rejection_reason=None)
    
    return {
        "status": "success",
        "message": f"Merchant {merchant['business_name']} approved",
        "merchant_id": merchant_id
    }

@router.post("/reject/{merchant_id}", response_model=Dict[str, Any])
async def reject_kyc(
    merchant_id: str,
    reason: str,
    current_user: dict = Depends(require_admin)
):
    """
    Admin: Reject merchant KYC with reason
    """
    merchant = await get_merchant_onboarding(merchant_id)
    if not merchant:
        raise HTTPException(status_code=404, detail="Merchant not found")
    
    await update_kyc_status(merchant_id, "rejected", reason)
    
    return {
        "status": "success",
        "message": f"Merchant {merchant['business_name']} rejected",
        "merchant_id": merchant_id,
        "reason": reason
    }

@router.post("/db/reset", response_model=Dict[str, Any])
async def reset_database_connection(current_user: dict = Depends(require_admin)):
    """
    Admin: Reset database connection (for transaction errors)
    """
    from db.database import database
    try:
        await database.disconnect()
        await asyncio.sleep(1)
        await database.connect()
        return {
            "status": "success",
            "message": "Database connection reset successfully"
        }
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to reset database: {str(e)}"
        )

@router.delete("/delete/{merchant_id}", response_model=Dict[str, Any])
async def delete_onboarding_merchant(
    merchant_id: str,
    reason: Optional[str] = None,
    current_user: dict = Depends(require_admin)
):
    """
    Admin: Soft delete Phase 2 onboarding merchant by merchant_id (string)
    This is separate from legacy merchants table deletion.
    """
    merchant = await get_merchant_onboarding(merchant_id)
    if not merchant:
        raise HTTPException(status_code=404, detail=f"Merchant {merchant_id} not found")
    
    success = await soft_delete_merchant_onboarding(merchant_id)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to delete merchant")
    
    return {
        "status": "success",
        "message": f"Merchant {merchant['business_name']} soft-deleted successfully",
        "merchant_id": merchant_id
    }

@router.delete("/{merchant_id}/delete", response_model=Dict[str, Any])
async def delete_onboarding_merchant_alias(
    merchant_id: str,
    reason: Optional[str] = None,
    current_user: dict = Depends(require_admin)
):
    """
    Alias: Admin soft delete by merchant_id (string). Mirrors /delete/{merchant_id}
    """
    merchant = await get_merchant_onboarding(merchant_id)
    if not merchant:
        raise HTTPException(status_code=404, detail=f"Merchant {merchant_id} not found")
    success = await soft_delete_merchant_onboarding(merchant_id)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to delete merchant")
    return {
        "status": "success",
        "message": f"Merchant {merchant['business_name']} soft-deleted successfully",
        "merchant_id": merchant_id
    }

@router.post("/kyc/upload/file/{merchant_id}", response_model=Dict[str, Any])
async def upload_kyc_file(
    merchant_id: str,
    document_type: str = Form(...),
    file: UploadFile = File(...)
):
    """
    Upload a KYB document for onboarding merchant (Phase 2) via multipart form.
    We only store metadata for now (no real file storage).
    """
    merchant = await get_merchant_onboarding(merchant_id)
    if not merchant:
        raise HTTPException(status_code=404, detail="Merchant not found")
    # Build simple metadata
    meta = {
        "name": file.filename,
        "content_type": file.content_type,
        "size": None,
        "document_type": document_type,
        "uploaded_at": datetime.now().isoformat()
    }
    try:
        # Try to read length if available
        content = await file.read()
        meta["size"] = len(content)
    except Exception:
        meta["size"] = None
    # Append to onboarding record
    ok = await add_kyc_document(merchant_id, meta)
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to save document metadata")
    return {
        "status": "success",
        "message": "Document uploaded",
        "merchant_id": merchant_id,
        "document": meta
    }

@router.post("/upload/{merchant_id}", response_model=Dict[str, Any])
async def upload_kyc_files(
    merchant_id: str,
    files: list[UploadFile] = File(...),
    document_type: str = Form("other")
):
    """Multipart 多文件上传（商户门户使用，无需鉴权）。上传到 R2 并存元数据。"""
    merchant = await get_merchant_onboarding(merchant_id)
    if not merchant:
        raise HTTPException(status_code=404, detail="Merchant not found")

    stored: List[Dict[str, Any]] = []
    errors: List[str] = []
    
    for f in files:
        try:
            # 读取文件内容
            content = await f.read()
            file_size = len(content)
            
            # R2 存储功能推迟实现 - 暂时只保存元数据
            # success, file_key, error = await upload_file_to_r2(
            #     file_content=content,
            #     filename=f.filename,
            #     merchant_id=merchant_id,
            #     content_type=f.content_type or "application/octet-stream"
            # )
            # 
            # if not success:
            #     errors.append(f"{f.filename}: {error}")
            #     continue
            
            # 存储元数据（R2 功能推迟）
            meta = {
                "name": f.filename,
                "content_type": f.content_type,
                "size": file_size,
                "document_type": document_type,
                "uploaded_at": datetime.now().isoformat(),
                "r2_key": None  # R2 功能推迟实现
            }
            
            ok = await add_kyc_document(merchant_id, meta)
            if ok:
                stored.append(meta)
            else:
                errors.append(f"{f.filename}: Failed to save metadata")
                
        except Exception as e:
            errors.append(f"{f.filename}: {str(e)}")

    return {
        "status": "success" if stored else "error",
        "message": f"Uploaded {len(stored)} file(s) to R2",
        "merchant_id": merchant_id,
        "documents": stored,
        "errors": errors if errors else None
    }

@router.get("/document/download/{merchant_id}/{document_index}")
async def download_kyc_document(
    merchant_id: str,
    document_index: int,
    current_user: dict = Depends(require_admin)
):
    """
    Admin: 下载/预览 KYC 文档
    通过预签名 URL 重定向，有效期 1 小时
    """
    merchant = await get_merchant_onboarding(merchant_id)
    if not merchant:
        raise HTTPException(status_code=404, detail="Merchant not found")
    
    docs = merchant.get("kyc_documents") or []
    if document_index >= len(docs):
        raise HTTPException(status_code=404, detail="Document not found")
    
    doc = docs[document_index]
    r2_key = doc.get("r2_key")
    
    if not r2_key:
        raise HTTPException(
            status_code=404,
            detail="File not stored in R2 (legacy metadata-only document)"
        )
    
    # R2 存储功能推迟实现
    raise HTTPException(
        status_code=501,
        detail="File download functionality not yet implemented (R2 storage pending)"
    )

@router.get("/kyb/{merchant_id}/documents", response_model=Dict[str, Any])
async def get_merchant_kyb_documents(
    merchant_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Get KYB documents for a merchant"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Get merchant
        merchant = await get_merchant_onboarding(merchant_id)
        if not merchant:
            raise HTTPException(status_code=404, detail="Merchant not found")
        
        # Get documents from kyc_documents JSON field  
        documents = merchant.get("kyc_documents", [])
        if isinstance(documents, str):
            import json
            try:
                documents = json.loads(documents)
            except:
                documents = []
        elif documents is None:
            documents = []
        
        return {
            "status": "success",
            "kyb_status": merchant.get("status", "pending"),
            "review_notes": merchant.get("rejection_reason"),
            "documents": documents
        }
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get documents: {str(e)}")

@router.post("/restore/{merchant_id}", response_model=Dict[str, Any])
async def restore_merchant(
    merchant_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Restore a soft-deleted merchant"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Get merchant
        merchant = await get_merchant_onboarding(merchant_id)
        if not merchant:
            raise HTTPException(status_code=404, detail="Merchant not found")
        
        if merchant.get("status") != "deleted":
            raise HTTPException(status_code=400, detail="Merchant is not deleted")
        
        # Restore merchant by setting status back to previous state (or 'pending')
        # We'll set it to 'pending' and let employee re-approve if needed
        from db.merchant_onboarding import merchant_onboarding
        from db.database import database
        
        update_query = merchant_onboarding.update().where(
            merchant_onboarding.c.merchant_id == merchant_id
        ).values(
            status="pending",
            updated_at=datetime.now()
        )
        
        await database.execute(update_query)
        
        return {
            "status": "success",
            "message": "Merchant restored successfully. Status set to 'pending' for review.",
            "merchant_id": merchant_id
        }
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to restore merchant: {str(e)}")
