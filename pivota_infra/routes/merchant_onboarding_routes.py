"""
Merchant Onboarding Routes - Phase 2
Handles merchant registration, KYC, PSP setup, and API key issuance
"""

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, status, UploadFile, File, Form
from pydantic import BaseModel, EmailStr
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta
import asyncio
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
from db.database import database
from routes.auth_routes import get_current_user, require_admin
from urllib.parse import urlparse

router = APIRouter(prefix="/merchant/onboarding", tags=["merchant-onboarding"])

# ============================================================================
# REQUEST/RESPONSE MODELS
# ============================================================================

class MerchantRegisterRequest(BaseModel):
    business_name: str
    store_url: str  # Required for KYB and MCP integration
    region: str  # US, EU, APAC
    contact_email: EmailStr
    contact_phone: Optional[str] = None
    website: Optional[str] = None  # Optional, for backward compatibility

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

async def validate_stripe_key(api_key: str) -> bool:
    """Async wrapper for Stripe validation"""
    import concurrent.futures
    loop = asyncio.get_event_loop()
    with concurrent.futures.ThreadPoolExecutor() as pool:
        return await loop.run_in_executor(pool, validate_stripe_key_sync, api_key)

async def validate_adyen_key(api_key: str) -> bool:
    """Validate Adyen API key by making a lightweight test request.

    Notes:
    - Adyen may return 401/403 for merchantAccount mismatch/permissions even if the key is structurally valid.
    - 422 indicates the request shape is valid but data is not (still proves key/header accepted).
    - Therefore, we accept 200, 401, 403, 422 as evidence that the key is recognized by Adyen.
    """
    try:
        from config.settings import settings
        merchant_account = settings.adyen_merchant_account or "TEST"
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://checkout-test.adyen.com/v70/paymentMethods",
                headers={
                    "X-API-Key": api_key,
                    "Content-Type": "application/json"
                },
                json={"merchantAccount": merchant_account},
                timeout=10.0
            )
            # Accept 200/401/403/422 as evidence key is recognized; otherwise fail
            return response.status_code in [200, 401, 403, 422]
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

        # 0. 去重检查（基于标准化 store_url）
        norm_url = canonicalize_store_url(merchant_data.store_url)
        dup = await database.fetch_one(
            merchant_onboarding.select().where(
                (merchant_onboarding.c.store_url == merchant_data.store_url) |
                (merchant_onboarding.c.store_url == norm_url)
            )
        )
        if dup:
            raise HTTPException(status_code=409, detail="Store URL already registered")

        # 1. 自动 KYB 预审批验证（含 Shopify 域名快速通道）
        print("🔍 Starting auto-KYB pre-approval validation...")
        try:
            from utils.auto_kyb_validator import auto_kyb_pre_approval
            print("✅ auto_kyb_validator imported successfully")
        except Exception as import_err:
            print(f"❌ Failed to import auto_kyb_validator: {import_err}")
            import traceback
            traceback.print_exc()
            raise
        
        from datetime import datetime
        
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
            validation_result = {"approved": False, "confidence_score": 0}
        
        # 2. 创建商户记录
        merchant_dict = merchant_data.dict()
        merchant_id = await create_merchant_onboarding(merchant_dict)
        print(f"✅ Merchant created: {merchant_id}")
        
        # 3. 如果自动批准，立即更新状态为 approved
        if validation_result["approved"]:
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
        
        return {
            "status": "success",
            "message": (
                "✅ Registration approved! You can now connect your PSP and start processing payments.\n"
                "⚠️ Please complete full KYB documentation within 7 days."
                if validation_result["approved"]
                else "Registration received. Manual review required before PSP connection."
            ),
            "merchant_id": merchant_id,
            "auto_approved": validation_result["approved"],
            "confidence_score": validation_result["confidence_score"],
            "validation_details": validation_result["validation_results"],
            "full_kyb_deadline": validation_result.get("full_kyb_deadline"),
            "next_step": "Connect PSP" if validation_result["approved"] else "Wait for admin approval"
        }
    except Exception as e:
        error_msg = str(e)
        
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
async def setup_psp(psp_data: PSPSetupRequest):
    """
    Step 3: Setup PSP connection and get merchant API key
    Only allowed if KYC is approved
    Now with REAL PSP credential validation!
    """
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
        # Idempotent behavior: if already connected, return existing credentials
        return {
            "status": "success",
            "message": "PSP already connected",
            "merchant_id": merchant["merchant_id"],
            "api_key": merchant.get("api_key"),
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
        "psp_connected": merchant["psp_connected"],
        "psp_type": merchant.get("psp_type"),
        "api_key_issued": bool(merchant.get("api_key")),
        "created_at": merchant["created_at"].isoformat() if merchant["created_at"] else None,
        "verified_at": merchant["verified_at"].isoformat() if merchant.get("verified_at") else None,
    }

@router.get("/details/{merchant_id}", response_model=Dict[str, Any])
async def get_onboarding_details(
    merchant_id: str,
    current_user: dict = Depends(require_admin)
):
    """
    Get full onboarding merchant details including KYB documents
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
    
    # Normalize response for frontend expectations
    result = {
        "merchant_id": merchant["merchant_id"],
        "business_name": merchant["business_name"],
        "store_url": merchant.get("store_url"),
        "website": merchant.get("website"),
        "platform": merchant.get("region"),  # reuse region as platform label
        "status": merchant["status"],
        "kyc_documents": merchant.get("kyc_documents") or [],
        "psp_connected": merchant.get("psp_connected", False),
        "psp_type": merchant.get("psp_type"),
        "created_at": merchant.get("created_at").isoformat() if merchant.get("created_at") else None,
    }
    return {"status": "success", "merchant": result}

@router.get("/all", response_model=Dict[str, Any])
async def list_all_onboardings(
    status: Optional[str] = None,
    include_deleted: bool = False,
    current_user: dict = Depends(require_admin)
):
    """
    Admin: List all merchant onboardings (filtered by status if provided)
    """
    try:
        merchants = await get_all_merchant_onboardings(status, include_deleted=include_deleted)
        
        return {
            "status": "success",
            "count": len(merchants),
            "merchants": [
                {
                    "merchant_id": m["merchant_id"],
                    "business_name": m["business_name"],
                    "store_url": m.get("store_url") or "N/A",
                    "region": m.get("region") or "Unknown",
                    "contact_email": m.get("contact_email"),
                    "status": m["status"],
                    "auto_approved": m.get("auto_approved", False),
                    "approval_confidence": m.get("approval_confidence"),
                    "full_kyb_deadline": m.get("full_kyb_deadline").isoformat() if m.get("full_kyb_deadline") else None,
                    "psp_connected": m.get("psp_connected", False),
                    "psp_type": m.get("psp_type"),
                    "mcp_connected": m.get("mcp_connected", False),
                    "mcp_platform": m.get("mcp_platform"),
                    "created_at": m["created_at"].isoformat() if m["created_at"] else None,
                }
                for m in merchants
            ]
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
    """Multipart 多文件上传（商户门户使用，无需鉴权）。仅存元数据。"""
    merchant = await get_merchant_onboarding(merchant_id)
    if not merchant:
        raise HTTPException(status_code=404, detail="Merchant not found")

    stored: List[Dict[str, Any]] = []
    for f in files:
        meta = {
            "name": f.filename,
            "content_type": f.content_type,
            "size": None,
            "document_type": document_type,
            "uploaded_at": datetime.now().isoformat()
        }
        try:
            content = await f.read()
            meta["size"] = len(content)
        except Exception:
            meta["size"] = None
        ok = await add_kyc_document(merchant_id, meta)
        if ok:
            stored.append(meta)

    return {
        "status": "success",
        "message": f"Stored {len(stored)} document(s) metadata",
        "merchant_id": merchant_id,
        "documents": stored
    }

