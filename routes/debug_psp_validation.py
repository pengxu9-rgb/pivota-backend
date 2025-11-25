#!/usr/bin/env python3
"""
Debug script to validate PSP API keys and find configuration issues
"""

from fastapi import APIRouter, Depends, HTTPException
from db.database import database
from routes.auth import get_current_user
from routes.auth_routes import require_admin
import httpx
import stripe
import logging
from decimal import Decimal
import json

router = APIRouter(prefix="/debug/psp", tags=["Debug PSP"])
logger = logging.getLogger(__name__)

@router.get("/validate/{merchant_id}")
async def validate_merchant_psps(
    merchant_id: str,
    current_user: dict = Depends(require_admin)
):
    """Validate all PSP configurations for a merchant"""
    
    # Fetch all PSPs for this merchant
    psps = await database.fetch_all(
        """
        SELECT 
            psp_id,
            provider,
            api_key,
            account_id,
            secret_key,
            status,
            connected_at
        FROM merchant_psps
        WHERE merchant_id = :merchant_id
        ORDER BY connected_at DESC
        """,
        {"merchant_id": merchant_id}
    )
    
    results = []
    
    for psp in psps:
        result = {
            "psp_id": psp["psp_id"],
            "provider": psp["provider"],
            "status": psp["status"],
            "connected_at": str(psp["connected_at"]),
            "validation": {}
        }
        
        # Validate based on provider
        if psp["provider"] == "stripe":
            result["validation"] = await validate_stripe(psp["api_key"])
        elif psp["provider"] == "adyen":
            result["validation"] = await validate_adyen(psp["api_key"], psp["account_id"])
        elif psp["provider"] == "checkout":
            result["validation"] = await validate_checkout(psp["api_key"], psp["account_id"])
        elif psp["provider"] == "paypal":
            result["validation"] = await validate_paypal(psp["api_key"], psp["secret_key"])
        
        results.append(result)
    
    return {
        "merchant_id": merchant_id,
        "psp_count": len(psps),
        "validations": results
    }

async def validate_stripe(api_key: str) -> dict:
    """Validate Stripe API key"""
    if not api_key:
        return {"valid": False, "error": "No API key"}
    
    # Check format
    if not (api_key.startswith("sk_test_") or api_key.startswith("sk_live_")):
        return {
            "valid": False, 
            "error": "Invalid format. Should start with sk_test_ or sk_live_",
            "key_prefix": api_key[:10] if len(api_key) > 10 else api_key
        }
    
    # Test the key
    try:
        stripe.api_key = api_key
        # Try to retrieve account info
        account = stripe.Account.retrieve()
        return {
            "valid": True,
            "account_id": account.id,
            "account_name": account.get("business_profile", {}).get("name", "N/A")
        }
    except Exception as e:
        return {"valid": False, "error": str(e)}

async def validate_adyen(api_key: str, merchant_account: str) -> dict:
    """Validate Adyen API key"""
    if not api_key:
        return {"valid": False, "error": "No API key"}
    
    # Check format - Adyen keys typically start with AQE
    if not api_key.startswith("AQE"):
        return {
            "valid": False,
            "error": "Invalid format. Adyen API keys should start with 'AQE'",
            "key_prefix": api_key[:10] if len(api_key) > 10 else api_key,
            "hint": "Make sure you're using the API key from Adyen's Customer Area > Developers > API credentials"
        }
    
    # Test the key with a simple API call
    try:
        headers = {
            "X-API-Key": api_key,
            "Content-Type": "application/json"
        }
        
        # Use payment methods endpoint as a simple test (align with v71 used in manual curl)
        url = "https://checkout-test.adyen.com/v71/paymentMethods"
        payload = {
            "merchantAccount": merchant_account or "PivotaTestMerchant"
        }
        
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, headers=headers, timeout=10.0)
            
            if response.status_code == 200:
                return {
                    "valid": True,
                    "merchant_account": merchant_account or "PivotaTestMerchant",
                    "test_passed": True
                }
            elif response.status_code == 401:
                return {
                    "valid": False,
                    "error": "Authentication failed (401)",
                    "hint": "Check if the API key is correct and has the right permissions",
                    "response": response.text[:200]
                }
            else:
                return {
                    "valid": False,
                    "error": f"API returned {response.status_code}",
                    "response": response.text[:200]
                }
    except Exception as e:
        return {"valid": False, "error": str(e)}

async def validate_checkout(api_key: str, processing_channel_id: str) -> dict:
    """Validate Checkout.com API key"""
    if not api_key:
        return {"valid": False, "error": "No API key"}
    
    if not processing_channel_id:
        return {"valid": False, "error": "No processing channel ID"}
    
    # Check format
    if not (api_key.startswith("sk_test_") or api_key.startswith("sk_sbox_") or api_key.startswith("sk_")):
        return {
            "valid": False,
            "error": "Invalid format. Should start with sk_test_, sk_sbox_, or sk_",
            "key_prefix": api_key[:10] if len(api_key) > 10 else api_key
        }
    
    # Test the key
    try:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        
        url = "https://api.sandbox.checkout.com/payment-sessions"
        payload = {
            "amount": 100,
            "currency": "USD",
            "processing_channel_id": processing_channel_id,
            "success_url": "https://example.com/success",
            "cancel_url": "https://example.com/cancel"
        }
        
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, headers=headers, timeout=10.0)
            
            if response.status_code in [200, 201, 202]:
                return {
                    "valid": True,
                    "processing_channel_id": processing_channel_id,
                    "test_passed": True
                }
            else:
                return {
                    "valid": False,
                    "error": f"API returned {response.status_code}",
                    "response": response.text[:200]
                }
    except Exception as e:
        return {"valid": False, "error": str(e)}

async def validate_paypal(client_id: str, client_secret: str) -> dict:
    """Validate PayPal API credentials"""
    if not client_id or not client_secret:
        return {"valid": False, "error": "Missing client_id or client_secret"}
    
    # Test OAuth token generation
    try:
        auth = (client_id, client_secret)
        headers = {
            "Accept": "application/json",
            "Accept-Language": "en_US"
        }
        data = {"grant_type": "client_credentials"}
        
        url = "https://api-m.sandbox.paypal.com/v1/oauth2/token"
        
        async with httpx.AsyncClient() as client:
            response = await client.post(
                url,
                auth=auth,
                headers=headers,
                data=data,
                timeout=10.0
            )
            
            if response.status_code == 200:
                token_data = response.json()
                return {
                    "valid": True,
                    "token_type": token_data.get("token_type"),
                    "expires_in": token_data.get("expires_in"),
                    "test_passed": True
                }
            else:
                return {
                    "valid": False,
                    "error": f"OAuth failed with {response.status_code}",
                    "response": response.text[:200]
                }
    except Exception as e:
        return {"valid": False, "error": str(e)}

@router.get("/check-duplicates")
async def check_duplicate_psps(current_user: dict = Depends(require_admin)):
    """Check for duplicate PSP configurations"""
    
    query = """
        SELECT 
            merchant_id,
            provider,
            COUNT(*) as count,
            array_agg(psp_id) as psp_ids,
            array_agg(status) as statuses,
            array_agg(connected_at) as connected_times
        FROM merchant_psps
        GROUP BY merchant_id, provider
        HAVING COUNT(*) > 1
        ORDER BY COUNT(*) DESC
    """
    
    duplicates = await database.fetch_all(query)
    
    return {
        "duplicate_count": len(duplicates),
        "duplicates": [
            {
                "merchant_id": d["merchant_id"],
                "provider": d["provider"],
                "count": d["count"],
                "psp_ids": d["psp_ids"],
                "statuses": d["statuses"],
                "connected_times": [str(t) for t in d["connected_times"]]
            }
            for d in duplicates
        ]
    }

@router.post("/fix-adyen-key/{psp_id}")
async def fix_adyen_key_format(
    psp_id: str,
    new_api_key: str,
    current_user: dict = Depends(require_admin)
):
    """Fix Adyen API key format issues"""
    
    # Validate the new key format
    if not new_api_key.startswith("AQE"):
        raise HTTPException(
            status_code=400,
            detail="New API key must start with 'AQE'. Get the correct key from Adyen Customer Area > Developers > API credentials"
        )
    
    # Update the key
    result = await database.execute(
        """
        UPDATE merchant_psps
        SET api_key = :api_key
        WHERE psp_id = :psp_id
        RETURNING merchant_id, provider
        """,
        {"api_key": new_api_key, "psp_id": psp_id}
    )
    
    if result:
        return {
            "status": "success",
            "message": "Adyen API key updated",
            "psp_id": psp_id
        }
    else:
        raise HTTPException(status_code=404, detail="PSP not found")
