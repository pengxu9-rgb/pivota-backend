"""
Admin Debug Endpoint for Shopify Token Issues
"""
import logging
import json
import hashlib
from fastapi import APIRouter, HTTPException
from fastapi import Depends
from db.database import database
import httpx

from services.shopify_access_token_service import resolve_shopify_admin_access_token
from utils.auth import require_admin

router = APIRouter(prefix="/admin/debug", tags=["Admin Debug"])
logger = logging.getLogger(__name__)

@router.get("/shopify-token/{merchant_id}")
async def debug_shopify_token(merchant_id: str, current_user: dict = Depends(require_admin)):
    """
    Debug Shopify token storage and parsing
    Shows how token is stored and how it's being parsed
    """
    try:
        # Get Shopify store from database
        store_query = """
            SELECT 
                store_id,
                platform,
                domain,
                name,
                status,
                connected_at,
                LENGTH(api_key) as token_length
            FROM merchant_stores
            WHERE merchant_id = :merchant_id 
                AND platform = 'shopify'
            ORDER BY connected_at DESC
            LIMIT 1
        """
        
        store = await database.fetch_one(store_query, {"merchant_id": merchant_id})
        
        if not store:
            return {
                "status": "not_found",
                "message": "No Shopify store found for this merchant"
            }
        
        # Read raw token separately to avoid accidental leakage in debug SQL.
        api_key = await database.fetch_val(
            """
            SELECT api_key
            FROM merchant_stores
            WHERE store_id = :store_id
            """,
            {"store_id": store["store_id"]},
        )
        
        # Analyze token format
        api_key_str = api_key if isinstance(api_key, str) else ""
        token_analysis = {
            "raw_type": type(api_key).__name__,
            "raw_length": len(api_key_str) if api_key_str else 0,
            "starts_with_brace": api_key_str.strip().startswith("{") if api_key_str else False,
            "raw_sha256_fp": hashlib.sha256(api_key_str.encode("utf-8")).hexdigest()[:12] if api_key_str else None,
        }
        
        # Try to parse as JSON
        parsed_token = None
        parse_method = None
        
        if api_key:
            # Method 1: Direct use (plain token)
            if isinstance(api_key, str) and not api_key.strip().startswith("{"):
                parsed_token = api_key.strip()
                parse_method = "direct"
            else:
                # Method 2: JSON parse
                try:
                    parsed = json.loads(api_key_str)
                    parsed_token = parsed.get("access_token") or parsed.get("token")
                    parse_method = "json"
                    token_analysis["json_parsed"] = True
                    token_analysis["json_keys"] = list(parsed.keys())
                except Exception as e:
                    parsed_token = None
                    parse_method = "fallback"
                    token_analysis["json_parse_error"] = str(e)
        
        refresh_meta = {"has_client_credentials": False, "refreshed": False}
        resolved_token, refresh_meta = await resolve_shopify_admin_access_token(
            shop_domain=store["domain"],
            api_key_raw=api_key,
            store_id=str(store["store_id"]),
        )
        if resolved_token:
            parsed_token = resolved_token

        # Test the token with Shopify API
        test_result = None
        if parsed_token and store["domain"]:
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(
                        f"https://{store['domain']}/admin/api/2025-10/shop.json",
                        headers={"X-Shopify-Access-Token": parsed_token}
                    )
                    
                    test_result = {
                        "status_code": resp.status_code,
                        "success": resp.status_code == 200,
                        "response_preview": str(resp.text)[:200] if resp.status_code != 200 else "OK"
                    }
                    
                    if resp.status_code == 200:
                        shop_data = resp.json().get("shop", {})
                        test_result["shop_name"] = shop_data.get("name")
                        test_result["shop_id"] = shop_data.get("id")
            except Exception as e:
                test_result = {
                    "error": str(e),
                    "success": False
                }
        
        return {
            "status": "success",
            "merchant_id": merchant_id,
            "requested_by": current_user.get("sub"),
            "store_info": {
                "store_id": store["store_id"],
                "domain": store["domain"],
                "name": store["name"],
                "status": store["status"],
                "connected_at": store["connected_at"].isoformat() if store["connected_at"] else None
            },
            "token_analysis": token_analysis,
            "parsing": {
                "method_used": parse_method,
                "parsed_token_length": len(parsed_token) if parsed_token else 0,
                "parsed_token_sha256_fp": hashlib.sha256(parsed_token.encode("utf-8")).hexdigest()[:12]
                if parsed_token
                else None,
            },
            "token_refresh": {
                "has_client_credentials": bool((refresh_meta or {}).get("has_client_credentials")),
                "refreshed": bool((refresh_meta or {}).get("refreshed")),
                "refresh_error": (refresh_meta or {}).get("refresh_error"),
            },
            "api_test": test_result,
            "diagnosis": _diagnose_shopify_issue(test_result, token_analysis) if test_result else None
        }
        
    except Exception as e:
        logger.error(f"Debug failed: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Debug failed: {str(e)}")


def _diagnose_shopify_issue(test_result: dict, token_analysis: dict) -> dict:
    """Diagnose Shopify connection issues"""
    if not test_result:
        return {"issue": "No test performed", "fix": "Token is missing"}
    
    if test_result.get("success"):
        return {"issue": "None", "status": "healthy"}
    
    status_code = test_result.get("status_code")
    
    if status_code == 401:
        return {
            "issue": "Invalid or expired access token",
            "possible_causes": [
                "Token was revoked in Shopify admin",
                "App was uninstalled and reinstalled",
                "Token format is incorrect",
                "Token is for wrong shop"
            ],
            "fix": "Reconnect Shopify in Merchant Portal Integrations page",
            "steps": [
                "1. Login to Merchant Portal",
                "2. Go to Integrations",
                "3. Find Shopify section",
                "4. Click 'Reconnect' or 'Disconnect' then 'Connect'",
                "5. Enter valid Shopify credentials"
            ]
        }
    elif status_code == 403:
        return {
            "issue": "Insufficient permissions",
            "fix": "Token needs read_products, read_inventory, read_orders scopes"
        }
    elif status_code == 404:
        return {
            "issue": "Shop domain incorrect",
            "fix": f"Verify shop domain is correct"
        }
    elif status_code and status_code >= 500:
        return {
            "issue": "Shopify API is down",
            "fix": "Wait for Shopify to recover, no action needed"
        }
    else:
        return {
            "issue": test_result.get("error", "Unknown error"),
            "fix": "Check network connectivity and Shopify status"
        }
