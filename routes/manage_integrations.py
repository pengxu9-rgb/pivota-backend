"""Manage Integrations - Delete and Update"""
from fastapi import APIRouter, Depends, HTTPException, Body
from typing import Dict, Any
from db.database import database
from utils.auth import get_current_user
from services.merchant_psp_config_service import (
    persist_canonical_merchant_psp,
)
import json
import httpx

router = APIRouter()

async def get_merchant_id_from_user(current_user: dict) -> str:
    """Get merchant ID from JWT token"""
    merchant_id = current_user.get("merchant_id")
    if not merchant_id:
        row = await database.fetch_one(
            "SELECT merchant_id FROM merchant_onboarding WHERE contact_email = :email LIMIT 1",
            {"email": current_user.get("email")}
        )
        if row:
            merchant_id = row["merchant_id"]
    
    if not merchant_id:
        raise HTTPException(status_code=404, detail="Merchant ID not found")
    
    return merchant_id

# ============================================================================
# Store Management
# ============================================================================

@router.delete("/merchant/integrations/store/{store_id}")
async def delete_store(
    store_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Delete a connected store"""
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Not authorized")
    
    merchant_id = await get_merchant_id_from_user(current_user)
    
    try:
        # Verify ownership before deleting
        check_query = "SELECT store_id FROM merchant_stores WHERE store_id = :store_id AND merchant_id = :merchant_id"
        store = await database.fetch_one(check_query, {"store_id": store_id, "merchant_id": merchant_id})
        
        if not store:
            raise HTTPException(status_code=404, detail="Store not found or not owned by this merchant")
        
        # Delete the store
        delete_query = "DELETE FROM merchant_stores WHERE store_id = :store_id AND merchant_id = :merchant_id"
        await database.execute(delete_query, {"store_id": store_id, "merchant_id": merchant_id})
        
        return {
            "status": "success",
            "message": "Store disconnected successfully"
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete store: {str(e)}")

@router.put("/merchant/integrations/store/{store_id}")
async def update_store(
    store_id: str,
    store_data: Dict[str, Any] = Body(...),
    current_user: dict = Depends(get_current_user)
):
    """Update store settings (e.g., name, api_key)"""
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Not authorized")
    
    merchant_id = await get_merchant_id_from_user(current_user)
    
    try:
        # Verify ownership
        check_query = """
            SELECT store_id, platform, domain, api_key
            FROM merchant_stores
            WHERE store_id = :store_id AND merchant_id = :merchant_id
        """
        store = await database.fetch_one(check_query, {"store_id": store_id, "merchant_id": merchant_id})
        
        if not store:
            raise HTTPException(status_code=404, detail="Store not found or not owned by this merchant")
        
        # Build update query dynamically based on provided fields
        update_fields = []
        values = {"store_id": store_id, "merchant_id": merchant_id}
        
        if "name" in store_data:
            update_fields.append("name = :name")
            values["name"] = store_data["name"]
        
        if "api_key" in store_data:
            # Merge credentials when api_key is provided as a JSON/dict patch, e.g.
            # {"storefront_access_token":"..."} so we don't wipe the existing Admin token.
            patch = store_data["api_key"]
            current_raw = store.get("api_key") or ""

            current: Dict[str, Any] = {}
            if isinstance(current_raw, str) and current_raw.strip().startswith("{"):
                try:
                    parsed = json.loads(current_raw)
                    if isinstance(parsed, dict):
                        current = parsed
                except Exception:
                    current = {}
            elif isinstance(current_raw, str) and current_raw.strip():
                # Legacy single-token format
                current = {"access_token": current_raw.strip()}

            patch_dict: Dict[str, Any] = {}
            if isinstance(patch, dict):
                patch_dict = patch
            elif isinstance(patch, str) and patch.strip().startswith("{"):
                try:
                    parsed = json.loads(patch)
                    if isinstance(parsed, dict):
                        patch_dict = parsed
                except Exception:
                    patch_dict = {}
            elif isinstance(patch, str) and patch.strip():
                # Treat as full admin token replacement
                patch_dict = {"access_token": patch.strip()}

            # Detect whether the caller is attempting to update Shopify Admin token.
            updates_shopify_admin_token = False
            if store.get("platform") == "shopify":
                if isinstance(patch_dict, dict) and any(k in patch_dict for k in ("access_token", "token")):
                    updates_shopify_admin_token = True

            merged = dict(current)
            for k, v in (patch_dict or {}).items():
                if not isinstance(k, str) or not k:
                    continue
                if isinstance(v, str):
                    if not v.strip():
                        continue
                    merged[k] = v.strip()
                elif v is None:
                    continue
                else:
                    merged[k] = v

            # Verify Shopify Admin token when it is being updated to prevent storing invalid credentials.
            if store.get("platform") == "shopify" and updates_shopify_admin_token:
                domain = str(store.get("domain") or "").strip().lower()
                admin_token = merged.get("access_token") or merged.get("token")
                admin_token = admin_token.strip() if isinstance(admin_token, str) else ""
                if not domain or not admin_token:
                    raise HTTPException(status_code=400, detail="Shopify domain/token missing")
                try:
                    url = f"https://{domain}/admin/api/2024-07/shop.json"
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        resp = await client.get(url, headers={"X-Shopify-Access-Token": admin_token})
                    if resp.status_code != 200:
                        raise HTTPException(
                            status_code=400,
                            detail=f"Invalid Shopify Admin token. API returned: {resp.status_code}",
                        )
                    shop = (resp.json() or {}).get("shop") or {}
                    canonical = str(shop.get("myshopify_domain") or "").strip().lower()
                    if canonical and canonical != domain:
                        raise HTTPException(
                            status_code=400,
                            detail=f"Shopify token does not match store domain (expected {domain}, got {canonical})",
                        )
                except HTTPException:
                    raise
                except Exception:
                    raise HTTPException(status_code=400, detail="Failed to verify Shopify Admin token")

            # Optional verify: if storefront token present for Shopify, ping Storefront API.
            if (
                (store.get("platform") == "shopify")
                and isinstance(store.get("domain"), str)
                and isinstance(merged.get("storefront_access_token"), str)
                and merged.get("storefront_access_token")
            ):
                domain = store.get("domain")
                token = merged.get("storefront_access_token")
                try:
                    url = f"https://{domain}/api/2024-07/graphql.json"
                    headers = {"X-Shopify-Storefront-Access-Token": token, "Content-Type": "application/json"}
                    payload = {"query": "query { shop { name } }"}
                    async with httpx.AsyncClient(timeout=8.0) as client:
                        resp = await client.post(url, headers=headers, json=payload)
                    if resp.status_code != 200:
                        raise HTTPException(
                            status_code=400,
                            detail=f"Invalid Shopify Storefront token. API returned: {resp.status_code}",
                        )
                except HTTPException:
                    raise
                except Exception:
                    # Best-effort: don't block update if verification can't run
                    pass

            update_fields.append("api_key = :api_key")
            values["api_key"] = json.dumps(merged, ensure_ascii=False)
        
        if "status" in store_data:
            update_fields.append("status = :status")
            values["status"] = store_data["status"]
        
        if not update_fields:
            raise HTTPException(status_code=400, detail="No fields to update")
        
        update_query = f"""
            UPDATE merchant_stores 
            SET {", ".join(update_fields)}
            WHERE store_id = :store_id AND merchant_id = :merchant_id
        """
        await database.execute(update_query, values)
        
        return {
            "status": "success",
            "message": "Store updated successfully"
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update store: {str(e)}")

@router.post("/merchant/integrations/store/{store_id}/primary")
async def set_primary_store(
    store_id: str,
    current_user: dict = Depends(get_current_user)
):
    """
    Mark a store as the merchant's primary store.

    Current system derives "primary" from recency (ORDER BY connected_at DESC).
    We implement "set primary" by bumping the target store's connected_at to CURRENT_TIMESTAMP,
    so it becomes the top candidate for get_primary_store and /merchant/{merchant_id}/integrations.
    """
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Not authorized")

    merchant_id = await get_merchant_id_from_user(current_user)

    store = await database.fetch_one(
        """
        SELECT store_id, status, api_key
        FROM merchant_stores
        WHERE store_id = :store_id AND merchant_id = :merchant_id
        """,
        {"store_id": store_id, "merchant_id": merchant_id},
    )
    if not store:
        raise HTTPException(status_code=404, detail="Store not found or not owned by this merchant")

    status = (store.get("status") or "").lower()
    api_key = store.get("api_key") or ""
    if status not in ("active", "connected"):
        raise HTTPException(status_code=400, detail=f"Store status is '{status}'. Please reconnect the store first.")
    if not api_key.strip():
        raise HTTPException(status_code=400, detail="Store credentials missing. Please reconnect the store first.")

    try:
        await database.execute(
            """
            UPDATE merchant_stores
            SET connected_at = CURRENT_TIMESTAMP
            WHERE store_id = :store_id AND merchant_id = :merchant_id
            """,
            {"store_id": store_id, "merchant_id": merchant_id},
        )
        return {"status": "success", "message": "Primary store updated", "store_id": store_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to set primary store: {str(e)}")

# ============================================================================
# PSP Management
# ============================================================================

@router.delete("/merchant/integrations/psp/{psp_id}")
async def delete_psp(
    psp_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Delete a connected PSP"""
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Not authorized")
    
    merchant_id = await get_merchant_id_from_user(current_user)
    
    try:
        # Verify ownership before deleting
        check_query = "SELECT psp_id FROM merchant_psps WHERE psp_id = :psp_id AND merchant_id = :merchant_id"
        psp = await database.fetch_one(check_query, {"psp_id": psp_id, "merchant_id": merchant_id})
        
        if not psp:
            raise HTTPException(status_code=404, detail="PSP not found or not owned by this merchant")
        
        # Delete the PSP
        delete_query = "DELETE FROM merchant_psps WHERE psp_id = :psp_id AND merchant_id = :merchant_id"
        await database.execute(delete_query, {"psp_id": psp_id, "merchant_id": merchant_id})
        
        return {
            "status": "success",
            "message": "PSP disconnected successfully"
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete PSP: {str(e)}")

@router.put("/merchant/integrations/psp/{psp_id}")
async def update_psp(
    psp_id: str,
    psp_data: Dict[str, Any] = Body(...),
    current_user: dict = Depends(get_current_user)
):
    """Update PSP settings (e.g., api_key, status)"""
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Not authorized")
    
    merchant_id = await get_merchant_id_from_user(current_user)
    
    try:
        # Verify ownership
        check_query = """
            SELECT psp_id, provider, name, api_key, account_id, secret_key, environment,
                   provider_config, capabilities, status, connected_at, validation_status,
                   validation_error, last_validated_at
            FROM merchant_psps
            WHERE psp_id = :psp_id AND merchant_id = :merchant_id
        """
        psp = await database.fetch_one(check_query, {"psp_id": psp_id, "merchant_id": merchant_id})
        
        if not psp:
            raise HTTPException(status_code=404, detail="PSP not found or not owned by this merchant")

        psp_row = dict(psp)
        provider = str(psp_row.get("provider") or "").strip().lower()

        if "api_key" not in psp_data and "status" not in psp_data and "name" not in psp_data:
            raise HTTPException(status_code=400, detail="No fields to update")

        api_key = str(psp_data.get("api_key") if "api_key" in psp_data else psp_row.get("api_key") or "").strip()
        if not api_key:
            raise HTTPException(status_code=400, detail="api_key cannot be empty")

        status_value = str(
            psp_data.get("status") if "status" in psp_data else psp_row.get("status") or "active"
        ).strip().lower()
        if status_value not in {"active", "inactive"}:
            raise HTTPException(status_code=400, detail="status must be active or inactive")

        name_value = psp_data.get("name") if "name" in psp_data else psp_row.get("name")

        async with database.transaction():
            await persist_canonical_merchant_psp(
                merchant_id=merchant_id,
                provider=provider,
                api_key=api_key,
                account_id=psp_row.get("account_id"),
                secret_key=psp_row.get("secret_key"),
                environment=psp_row.get("environment"),
                provider_config=psp_row.get("provider_config"),
                name=name_value,
                capabilities=psp_row.get("capabilities"),
                status=status_value,
                connected_at=psp_row.get("connected_at"),
                psp_id=psp_id,
                existing_row=psp_row,
                stripe_mode="payment_intent",
            )
        
        return {
            "status": "success",
            "message": "PSP updated successfully"
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update PSP: {str(e)}")

# ============================================================================
# Batch Operations
# ============================================================================

@router.post("/merchant/integrations/cleanup")
async def cleanup_integrations(current_user: dict = Depends(get_current_user)):
    """Remove all inactive or test integrations"""
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Not authorized")
    
    merchant_id = await get_merchant_id_from_user(current_user)
    
    try:
        # Delete inactive stores
        await database.execute(
            "DELETE FROM merchant_stores WHERE merchant_id = :merchant_id AND status = 'inactive'",
            {"merchant_id": merchant_id}
        )
        
        # Delete inactive PSPs
        await database.execute(
            "DELETE FROM merchant_psps WHERE merchant_id = :merchant_id AND status = 'inactive'",
            {"merchant_id": merchant_id}
        )
        
        return {
            "status": "success",
            "message": "Inactive integrations cleaned up"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to cleanup: {str(e)}")


