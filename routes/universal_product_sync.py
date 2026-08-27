"""Universal Product Sync - Works for all platforms"""
from services.merchant_store_service import get_merchant_active_stores, get_primary_store
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Response, status
from typing import Optional, Dict, Any
from pydantic import BaseModel
from utils.auth import ADMIN_ROLES, get_current_user
from db.database import database
from db.merchant_onboarding import get_merchant_onboarding
from datetime import datetime
import httpx
import json
import logging
from adapters.bigcommerce_adapter import normalize_bigcommerce_store_hash
from adapters.magento_adapter import normalize_magento_store_url
from adapters.shopline_adapter import normalize_shopline_handle
from adapters.shoplazza_adapter import normalize_shoplazza_store_url
from adapters.woocommerce_adapter import normalize_woocommerce_store_url
from adapters.product_adapters import fetch_merchant_products
from routes.product_routes import upsert_product_cache
from db.products import delete_missing_products_from_cache
from services.catalog_sync_service import ingest_standard_products
from services.commerce_source_registry import catalog_sync_blocker
from services.shopify_access_token_service import resolve_shopify_admin_access_token
from services.wix_connection import (
    WixConnectionValidationError,
    extract_wix_site_id,
    normalize_wix_api_key,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/products", tags=["product-sync-v2"])

class UniversalSyncRequest(BaseModel):
    merchant_id: str
    force_refresh: bool = False
    limit: int = 50
    # Optional platform hint: shopify, wix, woocommerce, etc.
    # When provided, we will try to sync that specific platform first.
    platform: Optional[str] = None

class UniversalSyncResponse(BaseModel):
    status: str
    message: str
    merchant_id: str
    platform: str
    products_synced: int
    catalog_synced: int = 0
    error: Optional[str] = None
    sync_time: str
    demo_mode: bool = False

@router.post("/sync-universal/", response_model=UniversalSyncResponse)
async def universal_product_sync(
    request: UniversalSyncRequest,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_current_user),
    response: Response = None,
):
    """
    Universal product sync endpoint that intelligently handles all platforms.
    
    Features:
    - Auto-detects platform from connected stores
    - Graceful fallbacks for missing credentials
    - Demo mode for testing
    - No 500 errors - always returns meaningful responses
    """
    start_time = datetime.now()

    if current_user.get("merchant_id") != request.merchant_id and current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="cannot sync products for another merchant")

    # A PSP connection is not evidence of permission to read a merchant catalogue.
    # Reject it before store lookup so payment-only sources cannot return a silent
    # zero-product "success" from this catalogue endpoint.
    requested_platform = str(request.platform or "").strip().lower() or None
    requested_platform_blocker = catalog_sync_blocker(requested_platform) if requested_platform else None
    if requested_platform_blocker:
        return UniversalSyncResponse(
            status="unsupported",
            message=requested_platform_blocker,
            merchant_id=request.merchant_id,
            platform=requested_platform or "unknown",
            products_synced=0,
            sync_time=datetime.now().isoformat(),
        )
    
    try:
        logger.info(f"🔄 Universal sync request: merchant_id={request.merchant_id}")
        
        # 1. Get merchant info (best effort)
        #    For legacy MCP merchants, we may have connected stores but no
        #    merchant_onboarding row yet, so missing merchant should not
        #    block sync as long as we can find a store.
        merchant = await get_merchant_onboarding(request.merchant_id)
        
        # 2. Find connected store (check all possible sources)
        store_info = await find_connected_store(
            merchant_id=request.merchant_id,
            merchant=merchant,
            platform_hint=request.platform,
        )
        
        # If both merchant record and store are missing, treat as hard error
        if not merchant and not store_info:
            logger.warning(
                f"Universal sync: merchant {request.merchant_id} not found and no store connected"
            )
            return UniversalSyncResponse(
                status="error",
                message="Merchant account not found",
                merchant_id=request.merchant_id,
                platform="unknown",
                products_synced=0,
                sync_time=datetime.now().isoformat(),
            )
        
        if not store_info:
            if request.platform:
                msg = (
                    f"No {request.platform} store connected. "
                    f"Please connect your {request.platform.title()} store in Integrations."
                )
                logger.info(
                    f"{msg} (merchant_id={request.merchant_id})"
                )
            else:
                msg = "No store connected. Please connect your store in Integrations."
                logger.info(
                    f"No store connected for merchant {request.merchant_id}"
                )
            return UniversalSyncResponse(
                status="success",
                message=msg,
                merchant_id=request.merchant_id,
                platform=request.platform or "none",
                products_synced=0,
                sync_time=datetime.now().isoformat()
            )
        
        platform = store_info["platform"]
        logger.info(f"Found {platform} store for merchant {request.merchant_id}")

        platform_blocker = catalog_sync_blocker(platform)
        if platform_blocker:
            return UniversalSyncResponse(
                status="unsupported",
                message=platform_blocker,
                merchant_id=request.merchant_id,
                platform=platform,
                products_synced=0,
                sync_time=datetime.now().isoformat(),
            )

        if platform == "shopify":
            refreshed_token, _ = await resolve_shopify_admin_access_token(
                shop_domain=store_info.get("domain"),
                api_key_raw=store_info.get("api_key_raw") or store_info.get("api_key"),
                store_id=str(store_info.get("store_id") or "").strip() or None,
            )
            if refreshed_token:
                store_info["api_key"] = refreshed_token
        
        # 3. Check credentials and prepare for sync
        credentials = prepare_platform_credentials(platform, store_info)
        
        if not credentials:
            logger.warning(f"Incomplete credentials for {platform}")
            # Return clear status without creating fake products
            return UniversalSyncResponse(
                status="warning",
                message=f"{platform.title()} API credentials are missing or incomplete. Please reconnect your {platform.title()} store in the Integrations page.",
                merchant_id=request.merchant_id,
                platform=platform,
                products_synced=0,
                sync_time=datetime.now().isoformat(),
                demo_mode=False
            )
        
        # 4. Fetch products using the universal adapter
        # 4.1 Paginate through all pages (Shopify limit=250/page). Guard with max_pages.
        synced_count = 0
        synced_platform_ids = set()
        catalog_payloads = []
        page_token: Optional[str] = None
        max_pages = 20  # safety guard: 20 * 250 = 5000 items
        pages = 0

        while pages < max_pages:
            products_obj, next_page_token, error = await fetch_merchant_products(
                merchant_id=request.merchant_id,
                platform=platform,
                credentials=credentials,
                limit=request.limit,
                page_token=page_token,
            )

            if error:
                logger.error(f"Platform API error: {error}")
                # Still return success with helpful message
                return UniversalSyncResponse(
                    status="warning",
                    message=f"Could not fetch from {platform}: {error}. Please verify your store connection.",
                    merchant_id=request.merchant_id,
                    platform=platform,
                    products_synced=synced_count,
                    sync_time=datetime.now().isoformat()
                )

            if products_obj:
                for product in products_obj:
                    try:
                        product_data = json.loads(product.json())
                        await upsert_product_cache(
                            merchant_id=request.merchant_id,
                            platform=platform,
                            platform_product_id=product.id,
                            product_data=product_data,
                            ttl_seconds=604800  # 7 days
                        )
                        synced_platform_ids.add(str(product.id))
                        catalog_payloads.append(product_data)
                        synced_count += 1
                    except Exception as e:
                        logger.error(f"Failed to cache product {product.id}: {e}")

            pages += 1
            if next_page_token:
                page_token = next_page_token
            else:
                break

        # 5. Cleanup products that no longer exist upstream for this merchant/platform
        try:
            deleted_stale = await delete_missing_products_from_cache(
                merchant_id=request.merchant_id,
                platform=platform,
                valid_platform_product_ids=list(synced_platform_ids),
            )
            logger.info(
                f"Universal sync cleanup: removed {deleted_stale} stale "
                f"products for merchant={request.merchant_id}, platform={platform}"
            )
        except Exception as cleanup_error:
            # 清理失败不应该影响整体同步结果，只记录日志以便后续排查
            logger.error(
                f"Failed to cleanup stale products after sync for "
                f"merchant={request.merchant_id}, platform={platform}: {cleanup_error}"
            )

        catalog_synced = 0
        catalog_error_message = None
        if catalog_payloads:
            try:
                catalog_result = await ingest_standard_products(
                    merchant_id=request.merchant_id,
                    platform=platform,
                    product_payloads=catalog_payloads,
                    source_system="universal_product_sync",
                    source_ref=f"universal_product_sync:{request.merchant_id}:{platform}",
                    source_domain=store_info.get("domain"),
                )
                if isinstance(catalog_result, dict):
                    catalog_synced = int(catalog_result.get("products_ingested") or 0)
                # Onboarding→audit readiness: same hook run_catalog_sync_job has
                # carried since it existed, and which THIS path — the one every
                # portal sync button actually goes through (wix/shopify/
                # woocommerce/bigcommerce routes → sync_products → here) — never
                # got. Without it a portal-synced merchant's catalog is born with
                # ZERO quality snapshots: the classifier stamps every content_key
                # `low_quality` ("no quality snapshot found"), serving_eligible
                # stays false, and nothing ever schedules the scoring that would
                # change it. Measured on the 2026-07-29 Wix pilot
                # (merch_e68c20b0189746d0): 20/20 rows ingested, identity minted,
                # IPS classified — and product_quality_backfill_jobs contained
                # NOTHING for the merchant. The 30s quality-drain tick can only
                # drain what someone enqueues. Best-effort by the same contract
                # as the sibling hook: a scoring-enqueue failure must never fail
                # the sync that just succeeded.
                try:
                    from db.product_quality_backfill_jobs import (
                        create_quality_backfill_job,
                    )
                    await create_quality_backfill_job(
                        merchant_id=request.merchant_id,
                        platform=platform,
                        requested_by="universal_product_sync",
                        # HONOUR the caller's force_refresh. This was hardcoded
                        # False while `UniversalSyncRequest.force_refresh` existed
                        # and was silently ignored, which made a whole CLASS of
                        # fix undeliverable: the backfill skips any row that
                        # already has a snapshot (product_quality_backfill_service
                        # :102 — `missing_only and not force_refresh and
                        # quality_row_has_scores(...)`), so re-syncing a merchant
                        # whose products NEWLY GAINED a field could never move
                        # their stored score.
                        #
                        # Concretely, and not hypothetically: a Wix merchant
                        # scored 66.7 before category mapping existed. With this
                        # hardcoded False, syncing after the mapping lands writes
                        # the category correctly, enqueues the job, skips every
                        # row, leaves the snapshot at 66.7, and returns success —
                        # the products stay unservable while every signal says the
                        # sync worked. Both `routes/wix_sync.py` and
                        # `routes/platform_products_sync_api.py` pass
                        # force_refresh=True and were silently downgraded here.
                        force_refresh=bool(request.force_refresh),
                        missing_only=True,
                    )
                except Exception as enqueue_error:  # noqa: BLE001 — best-effort
                    logger.warning(
                        "universal_sync: quality-backfill enqueue failed merchant=%s: %s",
                        request.merchant_id,
                        enqueue_error,
                    )
            except Exception as catalog_error:
                catalog_error_message = str(catalog_error)
                logger.error(
                    "Failed canonical catalog ingest after universal sync merchant=%s platform=%s err=%s",
                    request.merchant_id,
                    platform,
                    catalog_error,
                )

        # 6. Update store sync status
        await update_sync_status(store_info.get("store_id"), synced_count)

        if catalog_error_message:
            if response is not None:
                response.status_code = status.HTTP_207_MULTI_STATUS
            return UniversalSyncResponse(
                status="partial_failure",
                message=(
                    f"Synced {synced_count} products from {platform.title()}, "
                    "but canonical catalog ingest failed"
                ),
                merchant_id=request.merchant_id,
                platform=platform,
                products_synced=synced_count,
                catalog_synced=0,
                error=catalog_error_message,
                sync_time=datetime.now().isoformat()
            )
        
        return UniversalSyncResponse(
            status="success",
            message=f"Successfully synced {synced_count} products from {platform.title()}",
            merchant_id=request.merchant_id,
            platform=platform,
            products_synced=synced_count,
            catalog_synced=catalog_synced,
            sync_time=datetime.now().isoformat()
        )
        
    except Exception as e:
        logger.error(f"Unexpected error in universal sync: {e}")
        # Never return 500 - always give meaningful response
        return UniversalSyncResponse(
            status="error",
            message=f"Sync service temporarily unavailable: {str(e)}",
            merchant_id=request.merchant_id,
            platform="unknown",
            products_synced=0,
            sync_time=datetime.now().isoformat()
        )


async def find_connected_store(
    merchant_id: str,
    merchant: Dict,
    platform_hint: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Find connected store from any source.

    If platform_hint is provided, prefer a store for that platform. This
    is important when a merchant has multiple stores (e.g. both Wix and Shopify).
    """

    # Normalize hint
    platform_hint_normalized = (
        platform_hint.lower() if isinstance(platform_hint, str) else None
    )

    # 1) Prefer an exact platform match in merchant_stores when hint is provided
    if platform_hint_normalized:
        store_query_hint = """
            SELECT store_id, platform, name, domain, api_key, status
            FROM merchant_stores
            WHERE merchant_id = :merchant_id
              AND platform = :platform
              AND status IN ('active', 'connected')
            ORDER BY is_primary DESC, connected_at DESC
            LIMIT 1
        """
        try:
            store = await database.fetch_one(
                store_query_hint,
                {"merchant_id": merchant_id, "platform": platform_hint_normalized},
            )
        except Exception:
            store_query_hint = """
                SELECT store_id, platform, name, domain, api_key, status
                FROM merchant_stores
                WHERE merchant_id = :merchant_id
                  AND platform = :platform
                  AND status IN ('active', 'connected')
                ORDER BY connected_at DESC
                LIMIT 1
            """
            store = await database.fetch_one(
                store_query_hint,
                {"merchant_id": merchant_id, "platform": platform_hint_normalized},
            )
        if store:
            return dict(store)

    # 2) Fallback: any active/connected store for this merchant
    store_query = """
        SELECT store_id, platform, name, domain, api_key, status
        FROM merchant_stores 
        WHERE merchant_id = :merchant_id 
        AND status IN ('active', 'connected')
        ORDER BY is_primary DESC, connected_at DESC
        LIMIT 1
    """
    try:
        store = await database.fetch_one(store_query, {"merchant_id": merchant_id})
    except Exception:
        store_query = """
            SELECT store_id, platform, name, domain, api_key, status
            FROM merchant_stores
            WHERE merchant_id = :merchant_id
            AND status IN ('active', 'connected')
            ORDER BY connected_at DESC
            LIMIT 1
        """
        store = await database.fetch_one(store_query, {"merchant_id": merchant_id})

    if store:
        store_dict = dict(store)
        # If api_key is empty but legacy token exists, migrate it in-place for stability
        try:
            if not store_dict.get("api_key") and merchant and merchant.get("mcp_access_token"):
                legacy_token = merchant.get("mcp_access_token")
                await database.execute(
                    """
                    UPDATE merchant_stores
                    SET api_key = :api_key
                    WHERE store_id = :store_id
                    """,
                    {"api_key": legacy_token, "store_id": store_dict.get("store_id")}
                )
                store_dict["api_key"] = legacy_token
        except Exception as e:
            logger.warning(f"Could not migrate legacy token to merchant_stores: {e}")
        return store_dict

    # 3) Fallback to merchant_onboarding for legacy MCP (if platform matches or no hint)
    if merchant and merchant.get("mcp_platform"):
        mcp_platform = str(merchant.get("mcp_platform")).lower()
        if not platform_hint_normalized or platform_hint_normalized == mcp_platform:
            return {
                "platform": merchant.get("mcp_platform"),
                "domain": merchant.get("mcp_shop_domain"),
                "api_key": merchant.get("mcp_access_token"),
                "store_id": f"legacy_{merchant_id}",
                "name": merchant.get("business_name"),
            }

    return None


def prepare_platform_credentials(platform: str, store_info: Dict) -> Optional[Dict[str, str]]:
    """Prepare credentials based on platform requirements"""
    
    if platform == "shopify":
        # For Shopify, domain is the shop domain and api_key is the access token (raw or JSON)
        domain = store_info.get("domain")
        token_raw = store_info.get("api_key")
        token_val = None
        if token_raw:
            try:
                if isinstance(token_raw, str) and token_raw.strip().startswith("{"):
                    parsed = json.loads(token_raw)
                    token_val = parsed.get("access_token") or parsed.get("token")
                else:
                    token_val = token_raw
            except Exception:
                token_val = token_raw
        if domain and token_val:
            return {
                "shop_domain": domain,
                "access_token": token_val
            }
    
    elif platform == "wix":
        api_key = normalize_wix_api_key(store_info.get("api_key"))
        try:
            site_id = extract_wix_site_id(store_info.get("domain"), store_info.get("api_key"))
        except WixConnectionValidationError:
            site_id = ""

        if site_id and api_key:
            return {
                "site_id": site_id,
                "api_key": api_key
            }
    
    elif platform == "woocommerce":
        # WooCommerce needs both consumer key and secret
        store_url = normalize_woocommerce_store_url(store_info.get("domain"))
        api_key_data = store_info.get("api_key")
        
        if store_url and api_key_data:
            try:
                if isinstance(api_key_data, str) and api_key_data.strip().startswith("{"):
                    credentials = json.loads(api_key_data)
                    return {
                        "store_url": store_url,
                        "consumer_key": credentials.get("consumer_key", ""),
                        "consumer_secret": credentials.get("consumer_secret", "")
                    }
                if isinstance(api_key_data, str) and ":" in api_key_data:
                    consumer_key, consumer_secret = api_key_data.split(":", 1)
                    return {
                        "store_url": store_url,
                        "consumer_key": consumer_key,
                        "consumer_secret": consumer_secret
                    }
                return {
                    "store_url": store_url,
                    "consumer_key": api_key_data,
                    "consumer_secret": ""
                }

            except json.JSONDecodeError:
                return {
                    "store_url": store_url,
                    "consumer_key": api_key_data,
                    "consumer_secret": ""
                }

    elif platform == "magento":
        store_url = normalize_magento_store_url(store_info.get("domain"))
        raw_credentials = store_info.get("api_key")
        if not store_url or not raw_credentials:
            return None
        try:
            parsed_credentials = (
                json.loads(raw_credentials)
                if isinstance(raw_credentials, str) and raw_credentials.strip().startswith("{")
                else raw_credentials
            )
        except json.JSONDecodeError:
            parsed_credentials = raw_credentials
        credentials = (
            dict(parsed_credentials)
            if isinstance(parsed_credentials, dict)
            else {"access_token": parsed_credentials}
        )
        access_token = str(credentials.get("access_token") or "").strip()
        if not access_token:
            return None
        return {
            "store_url": store_url,
            "access_token": access_token,
            "store_view_code": str(credentials.get("store_view_code") or "default"),
            "currency": str(credentials.get("currency") or "USD").upper(),
            "product_url_suffix": credentials.get("product_url_suffix"),
        }

    elif platform in {"shopline", "shoplazza"}:
        raw_credentials = store_info.get("api_key")
        if not raw_credentials:
            return None
        try:
            parsed = json.loads(raw_credentials) if isinstance(raw_credentials, str) else raw_credentials
        except json.JSONDecodeError:
            parsed = {"access_token": raw_credentials}
        credentials = dict(parsed) if isinstance(parsed, dict) else {"access_token": parsed}
        access_token = str(credentials.get("access_token") or "").strip()
        if not access_token:
            return None
        common = {
            "access_token": access_token,
            "api_version": str(credentials.get("api_version") or ("v20260601" if platform == "shopline" else "2026-01")),
            "currency": str(credentials.get("currency") or "USD").upper(),
        }
        if platform == "shopline":
            handle = normalize_shopline_handle(credentials.get("handle") or store_info.get("domain"))
            return {**common, "handle": handle} if handle else None
        store_url = normalize_shoplazza_store_url(store_info.get("domain"))
        return {**common, "store_url": store_url} if store_url else None
    
    elif platform == "square":
        # Square uses location ID and access token
        location_id = store_info.get("domain")
        api_key_data = store_info.get("api_key")
        
        if location_id and api_key_data:
            try:
                if api_key_data.startswith("{"):
                    credentials = json.loads(api_key_data)
                    return {
                        "location_id": location_id,
                        "access_token": credentials.get("access_token", ""),
                        "application_id": credentials.get("application_id", "")
                    }
                else:
                    return {
                        "location_id": location_id,
                        "access_token": api_key_data,
                        "application_id": ""
                    }
            except json.JSONDecodeError:
                return {
                    "location_id": location_id,
                    "access_token": api_key_data,
                    "application_id": ""
                }
    
    elif platform == "bigcommerce":
        # BigCommerce uses store hash and access token
        domain = store_info.get("domain")
        api_key_data = store_info.get("api_key")
        
        if domain and api_key_data:
            try:
                if isinstance(api_key_data, str) and api_key_data.strip().startswith("{"):
                    credentials = json.loads(api_key_data)
                    store_hash = normalize_bigcommerce_store_hash(
                        credentials.get("store_hash") or domain
                    )
                    return {
                        "store_hash": store_hash,
                        "access_token": credentials.get("access_token", ""),
                        "client_id": credentials.get("client_id", "")
                    }
                store_hash = normalize_bigcommerce_store_hash(domain)
                return {
                    "store_hash": store_hash,
                    "access_token": api_key_data,
                    "client_id": ""
                }
            except json.JSONDecodeError:
                store_hash = normalize_bigcommerce_store_hash(domain)
                return {
                    "store_hash": store_hash,
                    "access_token": api_key_data,
                    "client_id": ""
                }

    elif platform == "cafe24":
        domain = store_info.get("domain")
        api_key_data = (
            store_info.get("api_credentials")
            or store_info.get("api_key_raw")
            or store_info.get("api_key")
        )
        if domain and api_key_data:
            try:
                credentials = (
                    json.loads(api_key_data)
                    if isinstance(api_key_data, str)
                    else dict(api_key_data)
                )
            except (json.JSONDecodeError, TypeError, ValueError):
                return None
            credentials["mall_id"] = credentials.get("mall_id") or str(domain).split(".", 1)[0]
            credentials["store_id"] = store_info.get("store_id")
            if credentials.get("access_token"):
                return credentials
    
    # Add more platforms as needed
    logger.warning(f"Platform {platform} not yet implemented in credential preparation")
    return None




async def update_sync_status(store_id: Optional[str], product_count: int):
    """Update store sync status"""
    if store_id and not store_id.startswith("legacy_"):
        try:
            await database.execute(
                """UPDATE merchant_stores 
                   SET last_sync = :last_sync, product_count = :count
                   WHERE store_id = :store_id""",
                {
                    "last_sync": datetime.now(),
                    "count": product_count,
                    "store_id": store_id
                }
            )
        except Exception as e:
            logger.error(f"Failed to update sync status: {e}")
