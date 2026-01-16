"""
Merchant Store Connections
Allow merchants to connect their own stores (Shopify, Wix, etc.)
"""
from services.merchant_store_service import get_merchant_active_stores, get_primary_store
from fastapi import APIRouter, Depends, HTTPException, Body, BackgroundTasks
from pydantic import BaseModel
from typing import Dict, Any, Optional
import logging
import httpx
import json
from datetime import datetime

from db.database import database
from utils.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/integrations", tags=["Merchant Integrations"])


class ConnectShopifyRequest(BaseModel):
    merchant_id: str
    shop_domain: str
    access_token: str
    # Optional: Storefront token for quote/checkout pricing (Storefront Cart API).
    # If omitted, backend will try to auto-create one using the Admin token.
    storefront_access_token: Optional[str] = None
    storefront_token: Optional[str] = None


async def _create_storefront_access_token_best_effort(*, shop_domain: str, access_token: str) -> Optional[str]:
    """
    Best-effort create a Shopify Storefront API token using the Admin token.
    Requires the custom app to have Storefront API enabled on Shopify side.
    """
    try:
        url = f"https://{shop_domain}/admin/api/2024-07/storefront_access_tokens.json"
        headers = {"X-Shopify-Access-Token": access_token, "Content-Type": "application/json"}
        payload = {"storefront_access_token": {"title": "Pivota Pricing"}}
        async with httpx.AsyncClient(timeout=12.0) as client:
            resp = await client.post(url, headers=headers, json=payload)
        if resp.status_code not in (200, 201):
            return None
        data = resp.json() or {}
        storefront = data.get("storefront_access_token") if isinstance(data, dict) else None
        token = storefront.get("access_token") if isinstance(storefront, dict) else None
        return token.strip() if isinstance(token, str) and token.strip() else None
    except Exception:
        return None


class ShopifySyncRequest(BaseModel):
    merchant_id: Optional[str] = None


class VerifyShopifyIntegrationRequest(BaseModel):
    merchant_id: str
    callback_base_url: str
    api_version: Optional[str] = None


class ShopifyWebhookEventOut(BaseModel):
    id: int
    merchant_id: str
    shop_domain: Optional[str] = None
    topic: str
    webhook_id: Optional[str] = None
    idempotency_key: str
    signature_verified: bool
    received_at: Optional[str] = None
    occurred_at: Optional[str] = None
    payload_sha256: str
    prev_chain_hash: Optional[str] = None
    chain_hash: str


class ConnectWixRequest(BaseModel):
    merchant_id: str
    site_id: str
    api_key: str
    store_name: Optional[str] = None


class ConnectWooCommerceRequest(BaseModel):
    merchant_id: str
    store_url: str
    consumer_key: str
    consumer_secret: str


class ConnectBigCommerceRequest(BaseModel):
    merchant_id: str
    store_hash: str
    access_token: str
    client_id: Optional[str] = None


class ConnectPrestaShopRequest(BaseModel):
    merchant_id: str
    store_url: str
    api_key: str


@router.post("/shopify/connect")
async def merchant_connect_shopify(
    request: ConnectShopifyRequest,
    current_user: dict = Depends(get_current_user)
):
    """Allow merchant to connect their Shopify store"""
    # Allow merchant, employee, or admin
    if current_user["role"] not in ["merchant", "employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    # If merchant role, verify they can only connect their own store
    if current_user["role"] == "merchant":
        if current_user.get("merchant_id") != request.merchant_id:
            raise HTTPException(status_code=403, detail="Can only connect your own store")
    
    try:
        # Validate shop domain and access token
        if not request.shop_domain or not request.shop_domain.strip():
            raise HTTPException(status_code=400, detail="Shop domain is required")
        
        if not request.access_token or not request.access_token.strip():
            raise HTTPException(status_code=400, detail="Access token is required")
        
        # Test Shopify API connection
        test_url = f"https://{request.shop_domain}/admin/api/2024-07/shop.json"
        headers = {"X-Shopify-Access-Token": request.access_token}
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            test_response = await client.get(test_url, headers=headers)
        
        if test_response.status_code != 200:
            raise HTTPException(
                status_code=400, 
                detail=f"Invalid Shopify credentials. API returned: {test_response.status_code}"
            )
        
        # Verify shop data
        shop_data = test_response.json()
        if not shop_data.get("shop"):
            raise HTTPException(status_code=400, detail="Invalid Shopify response")
        
        shop_info = shop_data["shop"]
        canonical_myshopify_domain = shop_info.get("myshopify_domain") or request.shop_domain
        logger.info(f"✅ Shopify credentials verified for {canonical_myshopify_domain}")

        # Storefront token strategy:
        # 1) accept from request (optional)
        # 2) preserve prior stored token (so merchants can reconnect without re-entering)
        # 3) best-effort auto-create using Admin token (to simplify merchant UX)
        storefront_token_raw = (request.storefront_access_token or request.storefront_token or "").strip()
        storefront_token_from_request = bool(storefront_token_raw)
        storefront_token = storefront_token_raw or None
        storefront_token_created = False
        storefront_token_verified = None

        existing = await database.fetch_one(
            """SELECT store_id, api_key FROM merchant_stores 
               WHERE merchant_id = :merchant_id AND platform = 'shopify' 
               AND (domain = :domain_input OR domain = :domain_canonical)""",
            {
                "merchant_id": request.merchant_id,
                "domain_input": request.shop_domain,
                "domain_canonical": canonical_myshopify_domain,
            },
        )

        existing_creds: Dict[str, Any] = {}
        if existing and (existing.get("api_key") or ""):
            try:
                parsed = json.loads(existing.get("api_key") or "")
                if isinstance(parsed, dict):
                    existing_creds = parsed
            except Exception:
                existing_creds = {}

        if not storefront_token:
            stored = (
                (
                    existing_creds.get("storefront_access_token")
                    if isinstance(existing_creds.get("storefront_access_token"), str)
                    else None
                )
                or (existing_creds.get("storefront_token") if isinstance(existing_creds.get("storefront_token"), str) else None)
                or (
                    existing_creds.get("storefrontAccessToken")
                    if isinstance(existing_creds.get("storefrontAccessToken"), str)
                    else None
                )
            )
            storefront_token = stored.strip() if isinstance(stored, str) and stored.strip() else None

        if not storefront_token:
            auto = await _create_storefront_access_token_best_effort(
                shop_domain=canonical_myshopify_domain,
                access_token=request.access_token,
            )
            if auto:
                storefront_token = auto
                storefront_token_created = True

        if storefront_token:
            try:
                sf_url = f"https://{canonical_myshopify_domain}/api/2024-07/graphql.json"
                sf_payload = {"query": "query { shop { name } }"}
                async with httpx.AsyncClient(timeout=8.0) as client:
                    sf_resp = await client.post(
                        sf_url,
                        headers={
                            "X-Shopify-Storefront-Access-Token": storefront_token,
                            "Content-Type": "application/json",
                        },
                        json=sf_payload,
                    )
                if sf_resp.status_code != 200:
                    if storefront_token_from_request:
                        raise HTTPException(
                            status_code=400,
                            detail=f"Invalid Shopify Storefront token. API returned: {sf_resp.status_code}",
                        )

                    storefront_token_verified = False
                    storefront_token = None

                    # If stored token is invalid, try to auto-create a fresh one (best-effort).
                    if not storefront_token_created:
                        auto2 = await _create_storefront_access_token_best_effort(
                            shop_domain=canonical_myshopify_domain,
                            access_token=request.access_token,
                        )
                        if auto2:
                            try:
                                async with httpx.AsyncClient(timeout=8.0) as client:
                                    sf_resp2 = await client.post(
                                        sf_url,
                                        headers={
                                            "X-Shopify-Storefront-Access-Token": auto2,
                                            "Content-Type": "application/json",
                                        },
                                        json=sf_payload,
                                    )
                                if sf_resp2.status_code == 200:
                                    storefront_token = auto2
                                    storefront_token_created = True
                                    storefront_token_verified = True
                            except Exception:
                                pass
                else:
                    storefront_token_verified = True
            except HTTPException:
                raise
            except Exception:
                storefront_token_verified = None

        token_blob: Dict[str, Any] = {"access_token": request.access_token}
        if storefront_token:
            token_blob["storefront_access_token"] = storefront_token
        token_json = json.dumps(token_blob, ensure_ascii=False)
        
        if existing:
            # Update existing store - store token as JSON for consistency
            await database.execute(
                """UPDATE merchant_stores 
                   SET domain = :domain,
                       api_key = :token,
                       status = 'active',
                       connected_at = CURRENT_TIMESTAMP,
                       last_sync = CURRENT_TIMESTAMP
                   WHERE store_id = :store_id""",
                {
                    "domain": canonical_myshopify_domain,
                    "token": token_json,
                    "store_id": existing["store_id"],
                }
            )
            store_id = existing["store_id"]
        else:
            # Create new store record
            store_id = f"store_{request.merchant_id[:8]}_{int(datetime.now().timestamp())}"
            await database.execute(
                """INSERT INTO merchant_stores 
                   (store_id, merchant_id, platform, domain, name, api_key, status, connected_at)
                   VALUES (:store_id, :merchant_id, 'shopify', :domain, :name, :token, 'active', CURRENT_TIMESTAMP)""",
                {
                    "store_id": store_id,
                    "merchant_id": request.merchant_id,
                    "domain": canonical_myshopify_domain,
                    "name": shop_info.get("name", canonical_myshopify_domain),
                    "token": token_json,
                }
            )

        # Prevent stale recommendations/checkout attempts after reconnecting a Shopify store:
        # cached product IDs from a previous shop can linger until TTL, which breaks pricing/checkout.
        try:
            await database.execute(
                """
                UPDATE products_cache
                SET expires_at = NOW()
                WHERE merchant_id = :merchant_id
                  AND platform = 'shopify'
                  AND (expires_at IS NULL OR expires_at > NOW())
                """,
                {"merchant_id": request.merchant_id},
            )
        except Exception:
            pass
        
        # Legacy MCP fields have been migrated to merchant_stores table
        # No need to update merchant_onboarding anymore
        
        return {
            "status": "success",
            "message": "Shopify store connected successfully",
            "store_id": store_id,
            "shop_name": shop_info.get("name"),
            "shop_domain": canonical_myshopify_domain,
            "storefront_token_present": bool(storefront_token),
            "storefront_token_verified": storefront_token_verified,
            "storefront_token_created": storefront_token_created,
            "warning": None
            if storefront_token
            else "Storefront token missing: enable Storefront API for the Shopify custom app so Pivota can auto-generate it, or have support add it later.",
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error connecting Shopify: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Failed to connect Shopify: {str(e)}")


@router.post("/shopify/verify")
async def merchant_verify_shopify_integration(
    request: VerifyShopifyIntegrationRequest,
    current_user: dict = Depends(get_current_user),
):
    """
    Verify Shopify integration at onboarding time:
    - token validity + canonical myshopify domain
    - access scopes (REST access_scopes)
    - webhook registration (best-effort)
    - policies snapshot (best-effort)
    - capability probes (Shopify Payments / Returns)
    Persists a snapshot to pcs_merchant_capabilities when available.
    """
    if current_user["role"] not in ["merchant", "employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")

    if current_user["role"] == "merchant" and current_user.get("merchant_id") != request.merchant_id:
        raise HTTPException(status_code=403, detail="Can only verify your own store")

    if not request.callback_base_url or not request.callback_base_url.strip():
        raise HTTPException(status_code=400, detail="callback_base_url is required")

    try:
        from services.shopify_integration_verify import verify_shopify_integration

        report = await verify_shopify_integration(
            merchant_id=request.merchant_id,
            callback_base_url=request.callback_base_url,
            api_version=request.api_version or "2024-07",
        )
        return {"status": "success", "report": report}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Shopify integration verify failed merchant={request.merchant_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to verify Shopify integration")


@router.get("/shopify/webhooks/events")
async def list_shopify_webhook_events(
    merchant_id: Optional[str] = None,
    limit: int = 20,
    current_user: dict = Depends(get_current_user),
):
    """
    Read-only debug: list latest ingested Shopify webhook events for a merchant.
    Does NOT return payload_json (to avoid leaking PII).
    """
    if current_user["role"] not in ["merchant", "employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")

    target_merchant_id = merchant_id or current_user.get("merchant_id")
    if not target_merchant_id:
        raise HTTPException(status_code=400, detail="merchant_id is required")

    if current_user["role"] == "merchant" and current_user.get("merchant_id") != target_merchant_id:
        raise HTTPException(status_code=403, detail="Can only access your own merchant")

    safe_limit = max(1, min(int(limit or 20), 200))

    try:
        rows = await database.fetch_all(
            """
            SELECT
              id,
              merchant_id,
              shop_domain,
              topic,
              webhook_id,
              idempotency_key,
              signature_verified,
              received_at,
              occurred_at,
              payload_sha256,
              prev_chain_hash,
              chain_hash
            FROM pcs_shopify_webhook_events
            WHERE merchant_id = :merchant_id
            ORDER BY received_at DESC
            LIMIT :limit
            """,
            {"merchant_id": target_merchant_id, "limit": safe_limit},
        )
    except Exception as e:
        logger.error(f"Failed to query pcs_shopify_webhook_events merchant={target_merchant_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to load webhook events")

    events = []
    for row in rows:
        d = dict(row)
        # ISO stringify datetimes for JSON stability (FastAPI will also handle, but be explicit)
        for k in ("received_at", "occurred_at"):
            if d.get(k) is not None:
                d[k] = d[k].isoformat()
        events.append(d)

    return {"status": "success", "merchant_id": target_merchant_id, "events": events}


@router.post("/shopify/products/sync")
async def merchant_sync_shopify_products(
    request: ShopifySyncRequest,
    current_user: dict = Depends(get_current_user)
):
    """
    Sync Shopify products for a merchant.
    Mirrors /merchant/integrations/shopify/sync so legacy front-ends keep working.
    """
    if current_user["role"] not in ["merchant", "employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")

    target_merchant_id = request.merchant_id or current_user.get("merchant_id")
    if not target_merchant_id:
        raise HTTPException(status_code=400, detail="merchant_id is required")

    if current_user["role"] == "merchant" and current_user.get("merchant_id") != target_merchant_id:
        raise HTTPException(status_code=403, detail="Can only sync your own store")

    store_row = await database.fetch_one(
        """
        SELECT store_id, platform, domain, status
        FROM merchant_stores
        WHERE merchant_id = :merchant_id AND platform = 'shopify' AND status IN ('active', 'connected')
        ORDER BY connected_at DESC NULLS LAST
        LIMIT 1
        """,
        {"merchant_id": target_merchant_id}
    )

    if not store_row:
        raise HTTPException(
            status_code=400,
            detail="No Shopify store connected. Please connect your store first in Integrations."
        )

    store = dict(store_row)
    if (store.get("status") or "").lower() != "active":
        raise HTTPException(
            status_code=400,
            detail=f"Store is {store.get('status')}. Please reconnect your store."
        )

    # Call Shopify adapter directly
    try:
        import json
        from adapters.product_adapters import ShopifyProductAdapter
        from db.products import upsert_product_cache
        
        # Get credentials from merchant_stores
        cred_row = await database.fetch_one(
            "SELECT api_key FROM merchant_stores WHERE store_id = :store_id",
            {"store_id": store["store_id"]}
        )
        api_key_raw = cred_row["api_key"] if cred_row else None
        
        # Parse token (support JSON format)
        if api_key_raw and api_key_raw.strip().startswith("{"):
            token_data = json.loads(api_key_raw)
            access_token = token_data.get("access_token", api_key_raw)
        else:
            access_token = api_key_raw
        
        if not access_token:
            raise HTTPException(status_code=400, detail="Shopify access token not found")
        
        # Fetch products from Shopify API (paginated).
        synced_count = 0
        page_info = None
        pages_fetched = 0
        max_pages = 40  # Safety cap (40 * 250 = 10,000 products)

        while pages_fetched < max_pages:
            products, next_page, error = await ShopifyProductAdapter.fetch_products(
                shop_domain=store["domain"],
                access_token=access_token,
                merchant_id=target_merchant_id,
                limit=250,
                page_info=page_info,
            )

            if error:
                raise HTTPException(status_code=400, detail=f"Shopify API error: {error}")

            if not products:
                break

            pages_fetched += 1

            # Cache products
            for product in products:
                try:
                    product_data = json.loads(product.json())
                    await upsert_product_cache(
                        merchant_id=target_merchant_id,
                        platform="shopify",
                        platform_product_id=product.id,
                        product_data=product_data,
                        ttl_seconds=604800  # 7 days
                    )
                    synced_count += 1
                except Exception as cache_err:
                    logger.error(f"Failed to cache product {product.id}: {cache_err}")
                    continue

            # Stop if there's no next page token.
            if not next_page:
                break

            # Defensive: adapter sometimes returns a sentinel token when Link parsing fails.
            if next_page == "has_next":
                logger.warning(
                    "Shopify pagination indicated next page but page_info token could not be parsed; stopping early",
                    extra={"merchant_id": target_merchant_id, "domain": store.get("domain")},
                )
                break

            # Continue pagination.
            page_info = next_page

        # Update store product_count
        await database.execute(
            """UPDATE merchant_stores 
               SET product_count = :count, last_sync = CURRENT_TIMESTAMP
               WHERE store_id = :store_id""",
            {"count": synced_count, "store_id": store["store_id"]}
        )
        
        return {
            "status": "success",
            "message": f"Successfully synced {synced_count} products from {store['domain']}",
            "data": {
                "product_count": synced_count,
                "store_domain": store["domain"],
                "pages_fetched": pages_fetched,
                "synced_at": datetime.now().isoformat()
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error syncing Shopify products via legacy endpoint: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Failed to sync products: {str(e)}")


@router.post("/wix/connect")
async def merchant_connect_wix(
    request: ConnectWixRequest,
    current_user: dict = Depends(get_current_user)
):
    """Allow merchant to connect their Wix store"""
    # Allow merchant, employee, or admin
    if current_user["role"] not in ["merchant", "employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    # If merchant role, verify they can only connect their own store
    if current_user["role"] == "merchant":
        if current_user.get("merchant_id") != request.merchant_id:
            raise HTTPException(status_code=403, detail="Can only connect your own store")
    
    try:
        # Validate inputs
        if not request.site_id or not request.site_id.strip():
            raise HTTPException(status_code=400, detail="Wix Site ID is required")
        
        if not request.api_key or not request.api_key.strip():
            raise HTTPException(status_code=400, detail="Wix API Key is required")
        
        # Test Wix API connection (simplified check)
        test_url = "https://www.wixapis.com/stores/v1/products/query"
        headers = {
            "Authorization": request.api_key,
            "wix-site-id": request.site_id
        }
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            test_response = await client.post(
                test_url,
                json={"query": {"limit": 1}},
                headers=headers
            )
        
        if test_response.status_code not in [200, 401, 403]:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid Wix credentials. API returned: {test_response.status_code}"
            )
        
        logger.info(f"✅ Wix credentials verified for site {request.site_id}")
        
        # Check if store already exists
        existing_store = await database.fetch_one(
            """SELECT store_id FROM merchant_stores 
               WHERE merchant_id = :merchant_id AND platform = 'wix' AND domain = :site_id""",
            {"merchant_id": request.merchant_id, "site_id": request.site_id}
        )
        
        if existing_store:
            # Update existing store
            await database.execute(
                """UPDATE merchant_stores 
                   SET api_key = :token, status = 'active', last_sync = CURRENT_TIMESTAMP
                   WHERE store_id = :store_id""",
                {"store_id": existing_store["store_id"], "token": request.api_key}
            )
            store_id = existing_store["store_id"]
        else:
            # Insert new store
            store_id = f"store_{request.merchant_id[:8]}_{int(datetime.now().timestamp())}"
            await database.execute(
                """INSERT INTO merchant_stores 
                   (store_id, merchant_id, platform, domain, name, api_key, status, connected_at)
                   VALUES (:store_id, :merchant_id, 'wix', :site_id, :name, :token, 'active', CURRENT_TIMESTAMP)""",
                {
                    "store_id": store_id,
                    "merchant_id": request.merchant_id,
                    "site_id": request.site_id,
                    "name": request.store_name or f"Wix Store {request.site_id[:8]}",
                    "token": request.api_key
                }
            )
        
        return {
            "status": "success",
            "message": "Wix store connected successfully",
            "store_id": store_id
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error connecting Wix: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Failed to connect Wix: {str(e)}")


@router.post("/woocommerce/connect")
async def merchant_connect_woocommerce(
    request: ConnectWooCommerceRequest,
    current_user: dict = Depends(get_current_user)
):
    """Allow merchant to connect their WooCommerce store"""
    if current_user["role"] not in ["merchant", "employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    if current_user["role"] == "merchant":
        if current_user.get("merchant_id") != request.merchant_id:
            raise HTTPException(status_code=403, detail="Can only connect your own store")
    
    try:
        if not request.store_url or not request.consumer_key or not request.consumer_secret:
            raise HTTPException(status_code=400, detail="Store URL, Consumer Key and Consumer Secret are required")
        
        # Test WooCommerce API connection using adapter
        from adapters.woocommerce_adapter import WooCommerceAdapter
        
        adapter = WooCommerceAdapter({
            'store_url': request.store_url,
            'consumer_key': request.consumer_key,
            'consumer_secret': request.consumer_secret
        })
        
        # Validate config
        is_valid, error_msg = adapter.validate_config()
        if not is_valid:
            raise HTTPException(status_code=400, detail=error_msg)
        
        # Test connection
        test_result = await adapter.test_connection()
        if not test_result.get('success'):
            raise HTTPException(status_code=400, detail=f"WooCommerce connection failed: {test_result.get('error')}")
        
        logger.info(f"✅ WooCommerce credentials verified for {request.store_url}")
        
        # Check if store already exists
        existing_store = await database.fetch_one(
            """SELECT store_id FROM merchant_stores 
               WHERE merchant_id = :merchant_id AND platform = 'woocommerce' AND domain = :domain""",
            {"merchant_id": request.merchant_id, "domain": request.store_url}
        )
        
        if existing_store:
            # Update existing store
            await database.execute(
                """UPDATE merchant_stores 
                   SET api_key = :api_key, status = 'active', last_sync = CURRENT_TIMESTAMP
                   WHERE store_id = :store_id""",
                {"store_id": existing_store["store_id"], "api_key": f"{request.consumer_key}:{request.consumer_secret}"}
            )
            store_id = existing_store["store_id"]
        else:
            # Insert new store
            store_id = f"store_{request.merchant_id[:8]}_{int(datetime.now().timestamp())}"
            await database.execute(
                """INSERT INTO merchant_stores 
                   (store_id, merchant_id, platform, domain, name, api_key, status, connected_at)
                   VALUES (:store_id, :merchant_id, 'woocommerce', :domain, :name, :api_key, 'active', CURRENT_TIMESTAMP)""",
                {
                    "store_id": store_id,
                    "merchant_id": request.merchant_id,
                    "domain": request.store_url,
                    "name": test_result.get('store_name', f"WooCommerce Store"),
                    "api_key": f"{request.consumer_key}:{request.consumer_secret}"  # Store both
                }
            )
        
        return {
            "status": "success",
            "message": "WooCommerce store connected successfully",
            "store_id": store_id
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error connecting WooCommerce: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Failed to connect WooCommerce: {str(e)}")


@router.post("/bigcommerce/connect")
async def merchant_connect_bigcommerce(
    request: ConnectBigCommerceRequest,
    current_user: dict = Depends(get_current_user)
):
    """Allow merchant to connect their BigCommerce store"""
    if current_user["role"] not in ["merchant", "employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    if current_user["role"] == "merchant":
        if current_user.get("merchant_id") != request.merchant_id:
            raise HTTPException(status_code=403, detail="Can only connect your own store")
    
    try:
        if not request.store_hash or not request.access_token:
            raise HTTPException(status_code=400, detail="Store Hash and Access Token are required")
        
        # Test BigCommerce API connection using adapter
        from adapters.bigcommerce_adapter import BigCommerceAdapter
        
        adapter = BigCommerceAdapter({
            'store_hash': request.store_hash,
            'access_token': request.access_token,
            'client_id': request.client_id
        })
        
        # Validate config
        is_valid, error_msg = adapter.validate_config()
        if not is_valid:
            raise HTTPException(status_code=400, detail=error_msg)
        
        # Test connection
        test_result = await adapter.test_connection()
        if not test_result.get('success'):
            raise HTTPException(status_code=400, detail=f"BigCommerce connection failed: {test_result.get('error')}")
        
        logger.info(f"✅ BigCommerce credentials verified for {request.store_hash}")
        
        # Check if store already exists
        store_domain = f"{request.store_hash}.mybigcommerce.com"
        existing_store = await database.fetch_one(
            """SELECT store_id FROM merchant_stores 
               WHERE merchant_id = :merchant_id AND platform = 'bigcommerce' AND domain = :domain""",
            {"merchant_id": request.merchant_id, "domain": store_domain}
        )
        
        if existing_store:
            # Update existing store
            await database.execute(
                """UPDATE merchant_stores 
                   SET api_key = :api_key, status = 'active', last_sync = CURRENT_TIMESTAMP
                   WHERE store_id = :store_id""",
                {"store_id": existing_store["store_id"], "api_key": request.access_token}
            )
            store_id = existing_store["store_id"]
        else:
            # Insert new store
            store_id = f"store_{request.merchant_id[:8]}_{int(datetime.now().timestamp())}"
            await database.execute(
                """INSERT INTO merchant_stores 
                   (store_id, merchant_id, platform, domain, name, api_key, status, connected_at)
                   VALUES (:store_id, :merchant_id, 'bigcommerce', :domain, :name, :api_key, 'active', CURRENT_TIMESTAMP)""",
                {
                    "store_id": store_id,
                    "merchant_id": request.merchant_id,
                    "domain": store_domain,
                    "name": test_result.get('store_name', f"BigCommerce Store"),
                    "api_key": request.access_token
                }
            )
        
        return {
            "status": "success",
            "message": "BigCommerce store connected successfully",
            "store_id": store_id
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error connecting BigCommerce: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Failed to connect BigCommerce: {str(e)}")


@router.post("/prestashop/connect")
async def merchant_connect_prestashop(
    request: ConnectPrestaShopRequest,
    current_user: dict = Depends(get_current_user)
):
    """Allow merchant to connect their PrestaShop store"""
    if current_user["role"] not in ["merchant", "employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    if current_user["role"] == "merchant":
        if current_user.get("merchant_id") != request.merchant_id:
            raise HTTPException(status_code=403, detail="Can only connect your own store")
    
    try:
        if not request.store_url or not request.api_key:
            raise HTTPException(status_code=400, detail="Store URL and API Key are required")
        
        # Test PrestaShop API connection using adapter
        from adapters.prestashop_adapter import PrestaShopAdapter
        
        adapter = PrestaShopAdapter({
            'store_url': request.store_url,
            'api_key': request.api_key
        })
        
        # Validate config
        is_valid, error_msg = adapter.validate_config()
        if not is_valid:
            raise HTTPException(status_code=400, detail=error_msg)
        
        # Test connection
        test_result = await adapter.test_connection()
        if not test_result.get('success'):
            raise HTTPException(status_code=400, detail=f"PrestaShop connection failed: {test_result.get('error')}")
        
        logger.info(f"✅ PrestaShop credentials verified for {request.store_url}")
        
        # Check if store already exists
        existing_store = await database.fetch_one(
            """SELECT store_id FROM merchant_stores 
               WHERE merchant_id = :merchant_id AND platform = 'prestashop' AND domain = :domain""",
            {"merchant_id": request.merchant_id, "domain": request.store_url}
        )
        
        if existing_store:
            # Update existing store
            await database.execute(
                """UPDATE merchant_stores 
                   SET api_key = :api_key, status = 'active', last_sync = CURRENT_TIMESTAMP
                   WHERE store_id = :store_id""",
                {"store_id": existing_store["store_id"], "api_key": request.api_key}
            )
            store_id = existing_store["store_id"]
        else:
            # Insert new store
            store_id = f"store_{request.merchant_id[:8]}_{int(datetime.now().timestamp())}"
            await database.execute(
                """INSERT INTO merchant_stores 
                   (store_id, merchant_id, platform, domain, name, api_key, status, connected_at)
                   VALUES (:store_id, :merchant_id, 'prestashop', :domain, :name, :api_key, 'active', CURRENT_TIMESTAMP)""",
                {
                    "store_id": store_id,
                    "merchant_id": request.merchant_id,
                    "domain": request.store_url,
                    "name": test_result.get('store_name', f"PrestaShop Store"),
                    "api_key": request.api_key
                }
            )
        
        return {
            "status": "success",
            "message": "PrestaShop store connected successfully",
            "store_id": store_id
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error connecting PrestaShop: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Failed to connect PrestaShop: {str(e)}")
