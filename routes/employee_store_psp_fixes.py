"""
Employee Portal Store and PSP Connection Fixes
Handles Shopify, Wix, Stripe, Adyen connections for merchants
"""
from services.merchant_store_service import get_merchant_active_stores, get_primary_store
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from typing import Optional, Dict, Any
from datetime import datetime
from utils.auth import get_current_user
from db.database import database
from services.merchant_psp_config_service import (
    SUPPORTED_CANONICAL_PSPS,
    persist_canonical_merchant_psp,
)
from services.wix_connection import WixConnectionValidationError, validate_wix_catalog_access
import uuid
import json

router = APIRouter()

# Providers `POST /merchant/onboarding/setup-psp` may write to
# merchant_psps.provider.
#
# WHY THIS EXISTS. This route took `psp_type: str` with no allowlist at all and
# persisted it verbatim with status='active'. Every other door into that column
# has always had one — /admin/psp/connect allows stripe/adyen/checkout/paypal,
# /merchant/integrations/psp/connect allows SUPPORTED_CANONICAL_PSPS — and this
# one, which allows role `merchant` for self-service onboarding, had none. The
# value comes straight back out at order creation
# (services.merchant_psp_config_service.fetch_active_runtime_merchant_psp filters
# on status='active' only, then routes/order_routes._resolve_active_order_psp
# returns it) and is written to orders.psp_used, where a CHECK constraint refuses
# anything it has not been taught. Reproduced on Postgres 15, 2026-09-01: 'square'
# and 'antom' both saved, both validated, both showed "connected", and then every
# order creation 500'd on check_psp_used_valid_provider. Same failure mode as the
# psp_id-format defect fixed in 20f4542c — a value the writer accepts and the
# reader refuses, silent between onboarding and the merchant's first sale.
#
# THE SET. SUPPORTED_CANONICAL_PSPS (stripe/adyen/checkout/antom) plus paypal,
# which /admin/psp/connect accepts and migration 006 has always allowed. Note
# what is NOT here: `square`, which this route's own capabilities map and the
# ConnectPSPRequest comment both advertised. Square has no PSP adapter anywhere in
# this repo — only catalog/storefront sync (services/commerce_source_registry.py,
# routes/universal_product_sync.py) — so a merchant who "connected" it had a row
# that could never charge and an account that could never take an order.
#
# Keep this in sync with migration 208's CHECK list on orders.psp_used: a provider
# accepted here that the constraint refuses is exactly the bug above.
SETUP_PSP_ALLOWED_PROVIDERS = frozenset(SUPPORTED_CANONICAL_PSPS | {"paypal"})

# ============== Models ==============

class ConnectShopifyRequest(BaseModel):
    merchant_id: str
    shop_domain: str
    access_token: Optional[str] = None
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    store_name: Optional[str] = None

class ConnectWixRequest(BaseModel):
    merchant_id: str
    api_key: str
    site_id: str
    store_name: Optional[str] = None

class ConnectPSPRequest(BaseModel):
    merchant_id: str
    # See SETUP_PSP_ALLOWED_PROVIDERS below for the values this actually accepts.
    # This comment used to read "stripe, adyen, paypal, square"; `square` has no
    # PSP adapter in this repo and is refused.
    psp_type: str
    api_key: Optional[str] = None
    test_mode: bool = True
    account_id: Optional[str] = None
    secret_key: Optional[str] = None  # For PayPal Client Secret
    public_key: Optional[str] = None
    # Custom PSP onboarding: allow saving provider selection without credentials.
    setup_later: bool = False
    custom_psp: bool = False

class SyncProductsRequest(BaseModel):
    merchant_id: str
    platform: Optional[str] = "shopify"

# ============== Store Connections ==============

@router.post("/integrations/shopify/connect-employee")
async def connect_shopify_store_employee(
    request: ConnectShopifyRequest,
    current_user: dict = Depends(get_current_user)
):
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")

    # Reuse the canonical merchant Shopify connect logic so employee and merchant
    # flows have identical token validation/storage behavior.
    from routes.merchant_store_connections import ConnectShopifyRequest as MerchantConnectShopifyRequest
    from routes.merchant_store_connections import merchant_connect_shopify

    delegated_request = MerchantConnectShopifyRequest(
        merchant_id=request.merchant_id,
        shop_domain=request.shop_domain,
        access_token=request.access_token,
        client_id=request.client_id,
        client_secret=request.client_secret,
    )
    return await merchant_connect_shopify(request=delegated_request, current_user=current_user)

@router.post("/integrations/wix/connect-employee")
async def connect_wix_store_employee(
    request: ConnectWixRequest,
    current_user: dict = Depends(get_current_user)
):
    """Employee-only version. Merchants should use /integrations/wix/connect from merchant_store_connections.py"""
    """Connect Wix store for a merchant (Employee action)"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Check if merchant exists and is in valid status
        merchant_check = await database.fetch_one(
            "SELECT merchant_id, status FROM merchant_onboarding WHERE merchant_id = :merchant_id",
            {"merchant_id": request.merchant_id}
        )
        
        if not merchant_check:
            raise HTTPException(status_code=404, detail="Merchant not found")
        
        # Don't allow connecting stores for rejected/deleted merchants
        if merchant_check["status"] in ["rejected", "deleted"]:
            raise HTTPException(
                status_code=400, 
                detail=f"Cannot connect store for {merchant_check['status']} merchant. Please approve merchant first."
            )
        
        try:
            validation = await validate_wix_catalog_access(request.site_id, request.api_key)
        except WixConnectionValidationError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": exc.message},
            )

        site_id = validation["site_id"]
        api_key = validation["api_key"]
        
        # Check if store already exists
        existing = await database.fetch_one(
            """SELECT store_id FROM merchant_stores 
               WHERE merchant_id = :merchant_id AND platform = 'wix' 
               AND domain = :domain""",
            {"merchant_id": request.merchant_id, "domain": site_id}
        )
        
        if existing:
            # Update existing store
            await database.execute(
                """UPDATE merchant_stores 
                   SET api_key = :api_key, status = 'active', connected_at = :connected_at
                   WHERE store_id = :store_id""",
                {
                    "api_key": api_key,
                    "connected_at": datetime.now(),
                    "store_id": existing["store_id"]
                }
            )
            store_id = existing["store_id"]
        else:
            # Create new store connection
            store_id = f"store_wix_{uuid.uuid4().hex[:8]}"
            
            await database.execute(
                """INSERT INTO merchant_stores 
                   (store_id, merchant_id, platform, name, domain, status, product_count, api_key, connected_at)
                   VALUES (:store_id, :merchant_id, :platform, :name, :domain, :status, :product_count, :api_key, :connected_at)""",
                {
                    "store_id": store_id,
                    "merchant_id": request.merchant_id,
                    "platform": "wix",
                    "name": request.store_name or f"Wix Store {site_id[:8]}",
                    "domain": site_id,
                    "status": "active",
                    "product_count": 0,
                    "api_key": api_key,
                    "connected_at": datetime.now()
                }
            )
        
        # Legacy MCP fields have been migrated to merchant_stores table
        # No need to update merchant_onboarding anymore
        
        return {
            "status": "success",
            "message": "Wix store connected successfully",
            "store_id": store_id
        }
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to connect Wix: {str(e)}")

# ============== PSP Connections ==============

@router.post("/merchant/onboarding/setup-psp")
async def setup_merchant_psp(
    request: ConnectPSPRequest,
    current_user: dict = Depends(get_current_user)
):
    """Setup PSP for a merchant (Employee action or Merchant self-service during onboarding).

    AUTHENTICATION IS REQUIRED, and the guard below is now reachable.

    This route previously declared its dependency as a no-op callable returning
    None, with a comment saying no auth was required for onboarding. That made
    `current_user` None on every request, so the role check was dead code - while
    the handler writes `merchant_psps` rows (api_key, secret_key, account_id,
    provider) for a **caller-supplied** merchant_id. It was reachable
    unauthenticated from the public internet.

    Every other route in this file already depended on get_current_user; this one
    was the outlier, and it had no callers anywhere in the repo. Merchant
    self-service is still supported - `merchant` remains in the allowed roles -
    it just has to be a merchant who is signed in.
    """
    if current_user.get("role") not in ["employee", "admin", "merchant"]:
        raise HTTPException(status_code=403, detail="Not authorized")

    # An unsupported provider must be refused HERE, not discovered at the
    # merchant's first sale. `persist_canonical_merchant_psp` lowercases the
    # provider before writing it, so the allowlist is checked on the same
    # normalised form the row will carry. See SETUP_PSP_ALLOWED_PROVIDERS.
    psp_type_norm = str(request.psp_type or "").strip().lower()
    if psp_type_norm not in SETUP_PSP_ALLOWED_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unsupported PSP provider: {request.psp_type or 'unknown'}. "
                f"Supported: {', '.join(sorted(SETUP_PSP_ALLOWED_PROVIDERS))}"
            ),
        )

    try:
        api_key = (request.api_key or "").strip()
        setup_later = bool(request.setup_later) or not api_key

        # Check if merchant exists
        merchant_check = await database.fetch_one(
            "SELECT merchant_id FROM merchant_onboarding WHERE merchant_id = :merchant_id",
            {"merchant_id": request.merchant_id}
        )
        
        if not merchant_check:
            raise HTTPException(status_code=404, detail="Merchant not found")
        
        # Check if PSP already exists
        existing = await database.fetch_one(
            """SELECT psp_id, merchant_id, provider, name, api_key, account_id, secret_key,
                      capabilities, status, connected_at, environment, provider_config,
                      validation_status, validation_error, last_validated_at
               FROM merchant_psps
               WHERE merchant_id = :merchant_id AND provider = :provider""",
            {"merchant_id": request.merchant_id, "provider": request.psp_type}
        )
        
        # Determine capabilities based on PSP type
        capabilities = {
            "stripe": ["payments", "refunds", "subscriptions", "payouts"],
            "adyen": ["payments", "refunds", "payouts", "risk_management"],
            "paypal": ["payments", "refunds", "payouts"],
            "square": ["payments", "refunds", "inventory"],
        }.get(request.psp_type, ["payments"])

        # Normalise / validate account_id by PSP type.
        # Important: do NOT auto-generate acct_* for Adyen, since Adyen requires
        # the real merchantAccount string.
        raw_account_id = (request.account_id or "").strip()
        if request.psp_type == "adyen":
            if not raw_account_id:
                raise HTTPException(
                    status_code=400,
                    detail="Adyen requires merchantAccount (account_id)",
                )
            account_id = raw_account_id
        elif request.psp_type == "checkout":
            if not raw_account_id:
                raise HTTPException(
                    status_code=400,
                    detail="Checkout.com requires processing_channel_id (account_id)",
                )
            account_id = raw_account_id
        else:
            # Stripe/others: keep provided account_id if present; otherwise NULL.
            account_id = raw_account_id or None

        provider_config = None
        if request.psp_type == "stripe":
            public_key = (request.public_key or "").strip()
            if public_key:
                provider_config = {"public_key": public_key}

        api_key_value = api_key if not setup_later else "pending_setup"
        persisted = await persist_canonical_merchant_psp(
            merchant_id=request.merchant_id,
            provider=request.psp_type,
            api_key=api_key_value,
            account_id=account_id,
            secret_key=request.secret_key,
            environment="test" if request.test_mode else "live",
            provider_config=provider_config,
            name=f"{request.psp_type.capitalize()} Account",
            capabilities=capabilities,
            status="pending" if setup_later else "active",
            connected_at=datetime.now(),
            psp_id=(dict(existing)["psp_id"] if existing else f"psp_{request.psp_type}_{uuid.uuid4().hex[:8]}"),
            existing_row=dict(existing) if existing else None,
            stripe_mode="payment_intent",
        )
        psp_id = persisted["psp_id"]
        
        return {
            "status": "success",
            "message": (
                f"{request.psp_type.capitalize()} saved for later setup"
                if setup_later
                else f"{request.psp_type.capitalize()} connected successfully"
            ),
            "psp_id": psp_id
        }
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to setup PSP: {str(e)}")

# ============== Product Sync ==============

@router.post("/integrations/{platform}/sync-products")
async def sync_merchant_products(
    platform: str,
    request: SyncProductsRequest,
    current_user: dict = Depends(get_current_user)
):
    """Sync products for a merchant store (Employee action)"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    if platform not in ["shopify", "wix", "woocommerce", "bigcommerce"]:
        raise HTTPException(status_code=400, detail="Invalid platform")
    
    try:
        # Try to get store from merchant_stores table first
        store = await database.fetch_one(
            """SELECT store_id, name as store_name, api_key 
               FROM merchant_stores 
               WHERE merchant_id = :merchant_id AND platform = :platform 
               AND status IN ('connected', 'active')
               ORDER BY connected_at DESC LIMIT 1""",
            {"merchant_id": request.merchant_id, "platform": platform}
        )
        
        # Fallback: check merchant_onboarding mcp_* fields
        if not store:
            from db.merchant_onboarding import get_merchant_onboarding
            merchant = await get_merchant_onboarding(request.merchant_id)
            
            if not merchant:
                raise HTTPException(status_code=404, detail="Merchant not found")
            
            # Check if merchant has MCP connected
            if merchant.get("mcp_platform") == platform:
                # Use merchant_onboarding data as a virtual "store"
                store = {
                    "store_id": f"mcp_{request.merchant_id}",
                    "store_name": merchant.get("business_name"),
                    "api_key": None  # Not needed for sync
                }
            else:
                raise HTTPException(
                    status_code=404, 
                    detail=f"No connected {platform} store found for merchant"
                )
        
        # Call the real product sync endpoint
        from routes.product_sync import sync_products, SyncRequest
        from fastapi import BackgroundTasks
        
        sync_request = SyncRequest(
            merchant_id=request.merchant_id,
            force_refresh=False,
            limit=250,
            platform=platform,
        )
        
        # Call real sync
        sync_result = await sync_products(
            request=sync_request,
            background_tasks=BackgroundTasks(),
            current_user=current_user
        )
        
        # Update store table if it exists
        if store.get("store_id") and not store["store_id"].startswith("mcp_"):
            await database.execute(
                """UPDATE merchant_stores 
                   SET product_count = :product_count, 
                       last_sync = :last_sync
                   WHERE store_id = :store_id""",
                {
                    "product_count": sync_result.products_synced,
                    "last_sync": datetime.now(),
                    "store_id": store["store_id"]
                }
            )
        
        return {
            "status": "success",
            "message": sync_result.message,
            "product_count": sync_result.products_synced,
            "platform": sync_result.platform,
            "store_id": store.get("store_id"),
            "store_name": store.get("store_name")
        }
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to sync products: {str(e)}")

# ============== Test Connections ==============

@router.post("/integrations/{platform}/test")
async def test_store_connection(
    platform: str,
    merchant_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Test store connection (Employee action)"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        store = await database.fetch_one(
            """SELECT store_id, api_key, domain, name 
               FROM merchant_stores 
               WHERE merchant_id = :merchant_id AND platform = :platform
               ORDER BY connected_at DESC LIMIT 1""",
            {"merchant_id": merchant_id, "platform": platform}
        )
        
        if not store:
            raise HTTPException(status_code=404, detail=f"No {platform} store found")
        
        # Simulate connection test
        # In real implementation, would make actual API call
        return {
            "status": "success",
            "message": f"{platform.capitalize()} connection successful",
            "connected": True
        }
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Connection test failed: {str(e)}")
