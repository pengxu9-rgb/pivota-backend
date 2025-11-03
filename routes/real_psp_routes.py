#!/usr/bin/env python3
"""
Real PSP Status Routes
This module provides real PSP connection status for the admin panel
"""

import logging
import time
import requests
from typing import Dict, Any, List, Optional
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from datetime import datetime

from utils.auth import verify_jwt_token, check_permission

logger = logging.getLogger("real_psp_routes")
router = APIRouter(prefix="/admin/real-psp", tags=["real-psp"])

# Real PSP Configuration - Using environment variables
import os

REAL_PSP_CONFIGS = {
    "stripe": {
        "name": "Stripe",
        "type": "stripe",
        "api_key": os.getenv("STRIPE_SECRET_KEY", ""),
        "webhook_secret": os.getenv("STRIPE_WEBHOOK_SECRET", ""),
        "merchant_account": None,
        "enabled": True,
        "sandbox_mode": True,
        "status": "active",
        "last_tested": None,
        "test_results": None
    },
    "adyen": {
        "name": "Adyen",
        "type": "adyen", 
        "api_key": os.getenv("ADYEN_API_KEY", ""),
        "webhook_secret": os.getenv("ADYEN_WEBHOOK_SECRET", ""),
        "merchant_account": os.getenv("ADYEN_MERCHANT_ACCOUNT", ""),
        "enabled": True,
        "sandbox_mode": True,
        "status": "active",
        "last_tested": None,
        "test_results": None
    }
}

class PSPStatusResponse(BaseModel):
    psp_id: str
    name: str
    type: str
    status: str
    enabled: bool
    sandbox_mode: bool
    last_tested: Optional[str]
    test_results: Optional[Dict[str, Any]]
    connection_health: str
    api_response_time: Optional[float]

def test_stripe_connection() -> Dict[str, Any]:
    """Test real Stripe connection"""
    try:
        start_time = time.time()
        
        # Test Stripe API connection
        response = requests.get(
            "https://api.stripe.com/v1/account",
            auth=(REAL_PSP_CONFIGS["stripe"]["api_key"], ""),
            timeout=10
        )
        
        response_time = (time.time() - start_time) * 1000  # Convert to milliseconds
        
        if response.status_code == 200:
            account_data = response.json()
            return {
                "success": True,
                "response_time_ms": response_time,
                "account_id": account_data.get("id"),
                "country": account_data.get("country"),
                "currency": account_data.get("default_currency"),
                "charges_enabled": account_data.get("charges_enabled", False),
                "payouts_enabled": account_data.get("payouts_enabled", False)
            }
        else:
            return {
                "success": False,
                "response_time_ms": response_time,
                "error": f"HTTP {response.status_code}",
                "details": response.text
            }
            
    except Exception as e:
        return {
            "success": False,
            "response_time_ms": None,
            "error": str(e),
            "details": "Connection failed"
        }

def test_adyen_connection() -> Dict[str, Any]:
    """Test real Adyen connection"""
    try:
        start_time = time.time()
        
        # Test Adyen API connection
        response = requests.post(
            "https://checkout-test.adyen.com/v71/payments",
            headers={
                "X-API-Key": REAL_PSP_CONFIGS["adyen"]["api_key"],
                "Content-Type": "application/json"
            },
            json={
                "amount": {
                    "value": 100,  # $1.00 test amount
                    "currency": "USD"
                },
                "reference": f"test_connection_{int(time.time())}",
                "paymentMethod": {
                    "type": "scheme",
                    "encryptedCardNumber": "test_4111111111111111",
                    "encryptedExpiryMonth": "test_03",
                    "encryptedExpiryYear": "test_2030",
                    "encryptedSecurityCode": "test_737"
                },
                "merchantAccount": REAL_PSP_CONFIGS["adyen"]["merchant_account"],
                "returnUrl": "https://pivota-dashboard.onrender.com/return"
            },
            timeout=10
        )
        
        response_time = (time.time() - start_time) * 1000  # Convert to milliseconds
        
        if response.status_code == 200:
            result = response.json()
            return {
                "success": True,
                "response_time_ms": response_time,
                "result_code": result.get("resultCode"),
                "psp_reference": result.get("pspReference"),
                "merchant_account": REAL_PSP_CONFIGS["adyen"]["merchant_account"]
            }
        else:
            return {
                "success": False,
                "response_time_ms": response_time,
                "error": f"HTTP {response.status_code}",
                "details": response.text
            }
            
    except Exception as e:
        return {
            "success": False,
            "response_time_ms": None,
            "error": str(e),
            "details": "Connection failed"
        }

@router.get("/status")
async def get_real_psp_status(
    credentials: dict = Depends(verify_jwt_token)
):
    """Get real PSP connection status - requires admin role"""
    check_permission(credentials, "admin")
    
    try:
        psp_statuses = []
        
        for psp_id, config in REAL_PSP_CONFIGS.items():
            # Test connection
            if psp_id == "stripe":
                test_results = test_stripe_connection()
            elif psp_id == "adyen":
                test_results = test_adyen_connection()
            else:
                test_results = {"success": False, "error": "Unknown PSP"}
            
            # Determine connection health
            if test_results.get("success"):
                connection_health = "healthy"
                status = "active"
            else:
                connection_health = "unhealthy"
                status = "error"
            
            # Update last tested time
            config["last_tested"] = datetime.utcnow().isoformat()
            config["test_results"] = test_results
            config["status"] = status
            
            psp_status = PSPStatusResponse(
                psp_id=psp_id,
                name=config["name"],
                type=config["type"],
                status=status,
                enabled=config["enabled"],
                sandbox_mode=config["sandbox_mode"],
                last_tested=config["last_tested"],
                test_results=test_results,
                connection_health=connection_health,
                api_response_time=test_results.get("response_time_ms")
            )
            
            psp_statuses.append(psp_status)
        
        logger.info(f"Retrieved real PSP status for {len(psp_statuses)} PSPs")
        
        return {
            "status": "success",
            "psps": psp_statuses,
            "total_psps": len(psp_statuses),
            "active_psps": len([p for p in psp_statuses if p.status == "active"]),
            "healthy_psps": len([p for p in psp_statuses if p.connection_health == "healthy"]),
            "timestamp": datetime.utcnow().isoformat()
        }
        
    except Exception as e:
        logger.error(f"Error retrieving real PSP status: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to retrieve PSP status: {str(e)}")

@router.get("/list")
async def get_real_psp_list(
    credentials: dict = Depends(verify_jwt_token)
):
    """Get list of real PSPs - requires admin role"""
    check_permission(credentials, "admin")
    
    try:
        psp_list = []
        
        for psp_id, config in REAL_PSP_CONFIGS.items():
            psp_info = {
                "psp_id": psp_id,
                "name": config["name"],
                "type": config["type"],
                "enabled": config["enabled"],
                "sandbox_mode": config["sandbox_mode"],
                "status": config.get("status", "unknown"),
                "last_tested": config.get("last_tested"),
                "has_api_key": bool(config.get("api_key")),
                "has_webhook_secret": bool(config.get("webhook_secret")),
                "merchant_account": config.get("merchant_account")
            }
            psp_list.append(psp_info)
        
        return {
            "status": "success",
            "psps": psp_list,
            "total_psps": len(psp_list),
            "enabled_psps": len([p for p in psp_list if p["enabled"]]),
            "timestamp": datetime.utcnow().isoformat()
        }
        
    except Exception as e:
        logger.error(f"Error retrieving PSP list: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to retrieve PSP list: {str(e)}")

@router.post("/test/{psp_id}")
async def test_real_psp_connection(
    psp_id: str,
    credentials: dict = Depends(verify_jwt_token)
):
    """Test real PSP connection - requires admin role"""
    check_permission(credentials, "admin")
    
    if psp_id not in REAL_PSP_CONFIGS:
        raise HTTPException(status_code=404, detail="PSP not found")
    
    try:
        # Test the specific PSP
        if psp_id == "stripe":
            test_results = test_stripe_connection()
        elif psp_id == "adyen":
            test_results = test_adyen_connection()
        else:
            raise HTTPException(status_code=400, detail="Unknown PSP type")
        
        # Update the PSP config with test results
        REAL_PSP_CONFIGS[psp_id]["last_tested"] = datetime.utcnow().isoformat()
        REAL_PSP_CONFIGS[psp_id]["test_results"] = test_results
        
        if test_results.get("success"):
            REAL_PSP_CONFIGS[psp_id]["status"] = "active"
        else:
            REAL_PSP_CONFIGS[psp_id]["status"] = "error"
        
        logger.info(f"Tested {psp_id} PSP connection: {test_results.get('success', False)}")
        
        return {
            "status": "success",
            "psp_id": psp_id,
            "test_results": test_results,
            "connection_status": "healthy" if test_results.get("success") else "unhealthy",
            "timestamp": datetime.utcnow().isoformat()
        }
        
    except Exception as e:
        logger.error(f"Error testing {psp_id} PSP: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to test PSP: {str(e)}")

@router.get("/health")
async def get_real_psp_health(
    credentials: dict = Depends(verify_jwt_token)
):
    """Get real PSP health summary - requires admin role"""
    check_permission(credentials, "admin")
    
    try:
        health_summary = {
            "total_psps": len(REAL_PSP_CONFIGS),
            "enabled_psps": len([p for p in REAL_PSP_CONFIGS.values() if p.get("enabled", False)]),
            "active_psps": len([p for p in REAL_PSP_CONFIGS.values() if p.get("status") == "active"]),
            "healthy_psps": 0,
            "unhealthy_psps": 0,
            "psp_details": []
        }
        
        for psp_id, config in REAL_PSP_CONFIGS.items():
            test_results = config.get("test_results", {})
            is_healthy = test_results.get("success", False)
            
            if is_healthy:
                health_summary["healthy_psps"] += 1
            else:
                health_summary["unhealthy_psps"] += 1
            
            psp_detail = {
                "psp_id": psp_id,
                "name": config["name"],
                "status": config.get("status", "unknown"),
                "enabled": config.get("enabled", False),
                "healthy": is_healthy,
                "last_tested": config.get("last_tested"),
                "response_time_ms": test_results.get("response_time_ms")
            }
            health_summary["psp_details"].append(psp_detail)
        
        return {
            "status": "success",
            "health_summary": health_summary,
            "timestamp": datetime.utcnow().isoformat()
        }
        
    except Exception as e:
        logger.error(f"Error retrieving PSP health: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to retrieve PSP health: {str(e)}")
