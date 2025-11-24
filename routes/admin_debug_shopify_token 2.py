"""
Admin Debug Endpoint for Shopify Token Issues
"""
import logging
import json
from fastapi import APIRouter, HTTPException
from db.database import database
import httpx

router = APIRouter(prefix="/admin/debug", tags=["Admin Debug"])
logger = logging.getLogger(__name__)

@router.get("/shopify-token/{merchant_id}")
async def debug_shopify_token(merchant_id: str):
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
                api_key,
                status,
                connected_at,
                LENGTH(api_key) as token_length,
                SUBSTRING(api_key, 1, 50) as token_preview
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
        
        api_key = store["api_key"]
        
        # Analyze token format
        token_analysis = {
            "raw_type": type(api_key).__name__,
            "raw_length": len(api_key) if api_key else 0,
            "starts_with_brace": api_key.strip().startswith("{") if api_key else False,
            "preview": api_key[:100] if api_key else None
        }
        
        # Try to parse as JSON
        parsed_token = None
        parse_method = None
        
        if api_key:
            # Method 1: Direct use (plain token)
            if not api_key.strip().startswith("{"):
                parsed_token = api_key
                parse_method = "direct"
            else:
                # Method 2: JSON parse
                try:
                    parsed = json.loads(api_key)
                    parsed_token = parsed.get("access_token") or parsed.get("token") or api_key
                    parse_method = "json"
                    token_analysis["json_parsed"] = True
                    token_analysis["json_keys"] = list(parsed.keys())
                except Exception as e:
                    parsed_token = api_key
                    parse_method = "fallback"
                    token_analysis["json_parse_error"] = str(e)
        
        # Test the token with Shopify API
        test_result = None
        if parsed_token and store["domain"]:
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(
                        f"https://{store['domain']}/admin/api/2024-01/shop.json",
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
                "parsed_token_preview": parsed_token[:20] + "..." if parsed_token and len(parsed_token) > 20 else parsed_token
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


