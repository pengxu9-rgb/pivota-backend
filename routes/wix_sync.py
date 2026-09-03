"""Wix Product Sync and Integration"""
import asyncio

from fastapi import APIRouter, Depends, HTTPException
from typing import Optional
from utils.auth import MERCHANT_OR_EMPLOYEE_STAFF_ROLES, get_current_user
from db.database import database
from datetime import datetime
import uuid
from services.wix_connection import (
    WixConnectionValidationError,
    validate_wix_catalog_access,
)

router = APIRouter()

_PLATFORM_LABELS = {
    "wix": "Wix",
    "woocommerce": "WooCommerce",
    "bigcommerce": "BigCommerce",
}


# Keep strong references to in-flight background syncs: asyncio holds tasks
# weakly, and a garbage-collected task dies mid-sync with no log at all.
_BACKGROUND_SYNC_TASKS: set = set()


async def _sync_connected_platform_products(
    *,
    platform: str,
    store_id: Optional[str],
    current_user: dict,
    wait: bool = False,
):
    platform_label = _PLATFORM_LABELS.get(platform, platform.title())

    if current_user["role"] not in MERCHANT_OR_EMPLOYEE_STAFF_ROLES:
        raise HTTPException(status_code=403, detail="Not authorized")

    merchant_id = current_user.get("merchant_id")
    if not merchant_id and current_user["role"] == "merchant":
        raise HTTPException(status_code=400, detail="Merchant ID not found")

    if store_id:
        store_query = """
            SELECT * FROM merchant_stores
            WHERE store_id = :store_id
              AND platform = :platform
              AND status IN ('active', 'connected')
        """
        store = await database.fetch_one(
            store_query,
            {"store_id": store_id, "platform": platform},
        )
    elif merchant_id:
        store_query = """
            SELECT * FROM merchant_stores
            WHERE merchant_id = :merchant_id
              AND platform = :platform
              AND status IN ('active', 'connected')
            LIMIT 1
        """
        store = await database.fetch_one(
            store_query,
            {"merchant_id": merchant_id, "platform": platform},
        )
    else:
        raise HTTPException(status_code=400, detail="store_id or merchant context is required")

    if not store:
        raise HTTPException(status_code=404, detail=f"{platform_label} store not found")

    store_dict = dict(store)
    merchant_id = store_dict.get("merchant_id")
    if not merchant_id:
        raise HTTPException(status_code=400, detail="Merchant ID not found in store record")

    from routes.product_sync import sync_products, SyncRequest
    from fastapi import BackgroundTasks

    sync_request = SyncRequest(
        merchant_id=merchant_id,
        force_refresh=True,
        limit=250,
        platform=platform,
    )

    # WHY THE SYNC NO LONGER RUNS INLINE. This handler used to await the full
    # catalog sync inside the HTTP request. A sync's duration is dominated by
    # per-product writes against a database ~140ms of RTT away, so any store
    # big enough to matter exceeded the edge's idle timeout (~16s observed):
    # the edge closed the connection with NO response, the portal showed
    # "API Error: undefined … ERR_CONNECTION_CLOSED", and the sync — which the
    # server happily finished after the client vanished — looked failed while
    # succeeding. Both syncs of the 2026-07-29 Wix pilot hit exactly this
    # (20/20 rows landed behind a scary error, verified in the DB each time).
    # A merchant's very first interaction with the product was a fake failure.
    #
    # `wait=true` (below) preserves the inline behavior for scripts that rely
    # on the old contract; the portal path returns `status: "started"`
    # immediately and polls GET /merchant/integrations/sync-status until
    # merchant_stores.last_sync advances past started_at — the durable
    # completion signal universal_product_sync already writes, chosen over a
    # new job table precisely so this fix needs no migration.
    if wait:
        sync_result = await sync_products(
            request=sync_request,
            background_tasks=BackgroundTasks(),
            current_user=current_user,
        )

        if sync_result.status != "success":
            raise HTTPException(
                status_code=400,
                detail=f"{platform_label} sync failed: {sync_result.message}",
            )

        return {
            "status": "success",
            "message": sync_result.message,
            "store_id": store_dict["store_id"],
            "store_name": store_dict["name"],
            "product_count": sync_result.products_synced,
            "platform": sync_result.platform,
            "synced_at": sync_result.sync_time,
        }

    started_at = datetime.utcnow()

    async def _run_sync_in_background():
        try:
            result = await sync_products(
                request=sync_request,
                background_tasks=BackgroundTasks(),
                current_user=current_user,
            )
            print(
                f"[portal-sync] background {platform} sync finished "
                f"merchant={merchant_id} status={result.status} "
                f"synced={result.products_synced}"
            )
        except Exception as exc:  # noqa: BLE001 — log; the poll surface shows staleness
            # A background failure has no response to land in. The portal's
            # poll times out against a last_sync that never advances and shows
            # "still running / retry", which is honest; the details live here.
            print(
                f"[portal-sync] background {platform} sync FAILED "
                f"merchant={merchant_id}: {exc!r}"
            )

    task = asyncio.create_task(_run_sync_in_background())
    _BACKGROUND_SYNC_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_SYNC_TASKS.discard)

    return {
        "status": "started",
        "message": (
            f"{platform_label} sync started — poll "
            f"/merchant/integrations/sync-status until last_sync passes started_at"
        ),
        "store_id": store_dict["store_id"],
        "store_name": store_dict["name"],
        "platform": platform,
        "started_at": started_at.isoformat() + "Z",
    }


@router.post("/merchant/integrations/wix/sync")
async def sync_wix_products(
    store_id: Optional[str] = None,
    wait: bool = False,
    current_user: dict = Depends(get_current_user)
):
    """Sync products from Wix store"""
    try:
        return await _sync_connected_platform_products(
            platform="wix",
            store_id=store_id,
            current_user=current_user,
            wait=wait,
        )
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error syncing Wix products: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to sync products: {str(e)}")


@router.post("/merchant/integrations/woocommerce/sync")
async def sync_woocommerce_products(
    store_id: Optional[str] = None,
    wait: bool = False,
    current_user: dict = Depends(get_current_user)
):
    """Sync products from WooCommerce store"""
    try:
        return await _sync_connected_platform_products(
            platform="woocommerce",
            store_id=store_id,
            current_user=current_user,
            wait=wait,
        )
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error syncing WooCommerce products: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to sync products: {str(e)}")


@router.post("/merchant/integrations/bigcommerce/sync")
async def sync_bigcommerce_products(
    store_id: Optional[str] = None,
    wait: bool = False,
    current_user: dict = Depends(get_current_user)
):
    """Sync products from BigCommerce store"""
    try:
        return await _sync_connected_platform_products(
            platform="bigcommerce",
            store_id=store_id,
            current_user=current_user,
            wait=wait,
        )
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error syncing BigCommerce products: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to sync products: {str(e)}")


@router.get("/merchant/integrations/sync-status")
async def merchant_sync_status(
    platform: str,
    store_id: Optional[str] = None,
    current_user: dict = Depends(get_current_user),
):
    """Completion poll for the background sync started by the sibling POSTs.

    Reads the durable signal `universal_product_sync.update_sync_status`
    already writes on completion (`merchant_stores.last_sync` +
    `product_count`) — deliberately no new job table, no migration. The portal
    compares `last_sync` against the `started_at` the POST returned: advanced
    past it = the sync that produced this response has finished. A poll that
    never advances means the background sync died; the server log carries the
    exception ([portal-sync] lines), and the honest client message is
    "still running or failed — retry", not a fabricated success.
    """
    if current_user["role"] not in MERCHANT_OR_EMPLOYEE_STAFF_ROLES:
        raise HTTPException(status_code=403, detail="Not authorized")

    merchant_id = current_user.get("merchant_id")
    if store_id:
        row = await database.fetch_one(
            """SELECT store_id, name, platform, status, last_sync, product_count
               FROM merchant_stores WHERE store_id = :store_id AND platform = :platform""",
            {"store_id": store_id, "platform": platform},
        )
    elif merchant_id:
        row = await database.fetch_one(
            """SELECT store_id, name, platform, status, last_sync, product_count
               FROM merchant_stores
               WHERE merchant_id = :merchant_id AND platform = :platform
                 AND status IN ('active', 'connected')
               LIMIT 1""",
            {"merchant_id": merchant_id, "platform": platform},
        )
    else:
        raise HTTPException(status_code=400, detail="store_id or merchant context is required")

    if not row:
        raise HTTPException(status_code=404, detail="store not found")

    d = dict(row)
    last_sync = d.get("last_sync")
    return {
        "store_id": d.get("store_id"),
        "store_name": d.get("name"),
        "platform": d.get("platform"),
        "store_status": d.get("status"),
        "last_sync": last_sync.isoformat() + "Z" if last_sync else None,
        "product_count": d.get("product_count"),
    }


@router.post("/integrations/wix/connect-sync")
async def connect_wix_store_sync(
    merchant_id: str,
    api_key: str,
    site_id: str,
    store_name: Optional[str] = None,
    current_user: dict = Depends(get_current_user)
):
    """Connect a Wix store"""
    if current_user["role"] not in MERCHANT_OR_EMPLOYEE_STAFF_ROLES:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Verify merchant exists
        merchant_query = "SELECT merchant_id FROM merchant_onboarding WHERE merchant_id = :merchant_id"
        merchant = await database.fetch_one(merchant_query, {"merchant_id": merchant_id})
        
        if not merchant:
            raise HTTPException(status_code=404, detail="Merchant not found")
        
        try:
            validation = await validate_wix_catalog_access(site_id, api_key)
        except WixConnectionValidationError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": exc.message},
            )

        normalized_site_id = validation["site_id"]
        normalized_api_key = validation["api_key"]

        existing = await database.fetch_one(
            """
            SELECT store_id
            FROM merchant_stores
            WHERE merchant_id = :merchant_id
              AND platform = 'wix'
              AND domain = :domain
            """,
            {"merchant_id": merchant_id, "domain": normalized_site_id},
        )

        if existing:
            store_id = existing["store_id"]
            await database.execute(
                """
                UPDATE merchant_stores
                SET name = :name,
                    api_key = :api_key,
                    status = 'active',
                    connected_at = :connected_at
                WHERE store_id = :store_id
                """,
                {
                    "store_id": store_id,
                    "name": store_name or normalized_site_id,
                    "api_key": normalized_api_key,
                    "connected_at": datetime.now(),
                },
            )
        else:
            store_id = f"store_wix_{uuid.uuid4().hex[:8]}"
            insert_query = """
                INSERT INTO merchant_stores (
                    store_id, merchant_id, platform, name, domain,
                    api_key, status, connected_at, product_count
                ) VALUES (
                    :store_id, :merchant_id, :platform, :name, :domain,
                    :api_key, :status, :connected_at, :product_count
                )
            """

            await database.execute(insert_query, {
                "store_id": store_id,
                "merchant_id": merchant_id,
                "platform": "wix",
                "name": store_name or normalized_site_id,
                "domain": normalized_site_id,
                "api_key": normalized_api_key,
                "status": "active",
                "connected_at": datetime.now(),
                "product_count": 0
            })
        
        return {
            "status": "success",
            "message": "Wix store connected successfully",
            "store_id": store_id,
            "store_name": store_name or normalized_site_id
        }
    
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error connecting Wix store: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to connect Wix store: {str(e)}")

@router.post("/integrations/wix/test")
async def test_wix_connection(
    merchant_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Test Wix store connection"""
    if current_user["role"] not in MERCHANT_OR_EMPLOYEE_STAFF_ROLES:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Get Wix store
        store_query = """
            SELECT * FROM merchant_stores
            WHERE merchant_id = :merchant_id AND platform = 'wix'
              AND status IN ('active', 'connected')
            LIMIT 1
        """
        store = await database.fetch_one(store_query, {"merchant_id": merchant_id})
        
        if not store:
            raise HTTPException(status_code=404, detail="Wix store not found")
        
        try:
            validation = await validate_wix_catalog_access(store["domain"], store["api_key"])
        except WixConnectionValidationError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": exc.message},
            )
        
        return {
            "status": "success",
            "message": "Wix connection successful",
            "store_name": store["name"],
            "site_id": validation["site_id"],
            "api_status": "active",
        }
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Connection test failed: {str(e)}")
