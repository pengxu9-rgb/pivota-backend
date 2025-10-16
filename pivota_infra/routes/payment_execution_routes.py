"""
Phase 3: Unified Payment Execution Router
Merchants use their API keys to execute payments through their connected PSP
"""

from fastapi import APIRouter, HTTPException, Header, status
from pydantic import BaseModel
from typing import Optional
import logging
from datetime import datetime
import secrets

from db.merchant_onboarding import get_merchant_onboarding, get_merchant_by_api_key
from db.payment_router import get_merchant_psp_route
from config.settings import settings
import stripe
import httpx

logger = logging.getLogger("payment_execution")
router = APIRouter(prefix="/payment", tags=["payment-execution"])


class PaymentExecuteRequest(BaseModel):
    amount: float  # Amount in cents (e.g., 1000 = $10.00)
    currency: str  # ISO currency code (e.g., USD, EUR)
    order_id: str  # Merchant's order ID
    customer_email: Optional[str] = None
    description: Optional[str] = None
    metadata: Optional[dict] = None


class PaymentExecuteResponse(BaseModel):
    success: bool
    payment_id: str
    order_id: str
    amount: float
    currency: str
    psp_used: str
    status: str  # completed, failed, pending
    transaction_id: Optional[str] = None
    error_message: Optional[str] = None
    timestamp: str


async def verify_merchant_api_key(api_key: str) -> dict:
    """Verify merchant API key and return merchant data"""
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key. Include 'X-Merchant-API-Key' header"
        )
    
    merchant = await get_merchant_by_api_key(api_key)
    if not merchant:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key"
        )
    
    if merchant["status"] != "approved":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Merchant account is {merchant['status']}. Only approved merchants can process payments."
        )
    
    if not merchant["psp_connected"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No PSP connected. Please connect a PSP first."
        )
    
    return merchant


async def execute_stripe_payment(merchant: dict, payment_data: PaymentExecuteRequest) -> dict:
    """Execute payment through Stripe"""
    try:
        # Get merchant's Stripe credentials from payment router
        psp_route = await get_merchant_psp_route(merchant["merchant_id"])
        if not psp_route or psp_route["psp_type"] != "stripe":
            raise HTTPException(
                status_code=400,
                detail="Stripe not configured for this merchant"
            )
        
        # Use merchant's Stripe key
        stripe_key = psp_route["psp_credentials"].get("api_key")
        if not stripe_key:
            raise HTTPException(
                status_code=400,
                detail="Stripe API key not found"
            )
        
        # Create Stripe payment intent
        stripe.api_key = stripe_key
        
        intent = stripe.PaymentIntent.create(
            amount=int(payment_data.amount),  # Stripe expects amount in cents
            currency=payment_data.currency.lower(),
            description=payment_data.description or f"Payment for order {payment_data.order_id}",
            metadata={
                "order_id": payment_data.order_id,
                "merchant_id": merchant["merchant_id"],
                **(payment_data.metadata or {})
            },
            receipt_email=payment_data.customer_email,
            confirm=True,  # Auto-confirm for testing
            automatic_payment_methods={"enabled": True, "allow_redirects": "never"}
        )
        
        return {
            "success": intent.status in ["succeeded", "processing"],
            "payment_id": intent.id,
            "status": "completed" if intent.status == "succeeded" else "pending",
            "transaction_id": intent.id,
            "error_message": None
        }
    
    except stripe.error.StripeError as e:
        logger.error(f"Stripe payment failed: {e}")
        return {
            "success": False,
            "payment_id": f"failed_{secrets.token_hex(8)}",
            "status": "failed",
            "transaction_id": None,
            "error_message": str(e)
        }
    except Exception as e:
        logger.error(f"Unexpected error in Stripe payment: {e}")
        return {
            "success": False,
            "payment_id": f"error_{secrets.token_hex(8)}",
            "status": "failed",
            "transaction_id": None,
            "error_message": str(e)
        }


async def execute_adyen_payment(merchant: dict, payment_data: PaymentExecuteRequest) -> dict:
    """Execute payment through Adyen"""
    try:
        # Get merchant's Adyen credentials from payment router
        psp_route = await get_merchant_psp_route(merchant["merchant_id"])
        if not psp_route or psp_route["psp_type"] != "adyen":
            raise HTTPException(
                status_code=400,
                detail="Adyen not configured for this merchant"
            )
        
        # Use merchant's Adyen API key
        adyen_key = psp_route["psp_credentials"].get("api_key")
        if not adyen_key:
            raise HTTPException(
                status_code=400,
                detail="Adyen API key not found"
            )
        
        # Adyen payment API call
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://checkout-test.adyen.com/v70/payments",
                headers={
                    "X-API-Key": adyen_key,
                    "Content-Type": "application/json"
                },
                json={
                    "amount": {
                        "value": int(payment_data.amount),
                        "currency": payment_data.currency.upper()
                    },
                    "reference": payment_data.order_id,
                    "merchantAccount": settings.adyen_merchant_account or "WoopayECOM",
                    "paymentMethod": {
                        "type": "scheme",
                        "number": "4111111111111111",  # Test card
                        "expiryMonth": "03",
                        "expiryYear": "2030",
                        "holderName": "Test User",
                        "cvc": "737"
                    },
                    "shopperEmail": payment_data.customer_email,
                    "metadata": payment_data.metadata or {}
                },
                timeout=30.0
            )
            
            result = response.json()
            
            if response.status_code == 200:
                return {
                    "success": result.get("resultCode") == "Authorised",
                    "payment_id": result.get("pspReference", f"adyen_{secrets.token_hex(8)}"),
                    "status": "completed" if result.get("resultCode") == "Authorised" else "failed",
                    "transaction_id": result.get("pspReference"),
                    "error_message": result.get("refusalReason") if result.get("resultCode") != "Authorised" else None
                }
            else:
                return {
                    "success": False,
                    "payment_id": f"failed_{secrets.token_hex(8)}",
                    "status": "failed",
                    "transaction_id": None,
                    "error_message": result.get("message", "Adyen payment failed")
                }
    
    except Exception as e:
        logger.error(f"Adyen payment failed: {e}")
        return {
            "success": False,
            "payment_id": f"error_{secrets.token_hex(8)}",
            "status": "failed",
            "transaction_id": None,
            "error_message": str(e)
        }


@router.post("/execute", response_model=PaymentExecuteResponse)
async def execute_payment(
    payment_request: PaymentExecuteRequest,
    x_merchant_api_key: str = Header(None, alias="X-Merchant-API-Key")
):
    """
    Unified payment execution endpoint for merchants.
    
    Merchants use their API key to execute payments through their connected PSP.
    
    Headers:
        X-Merchant-API-Key: Merchant's API key (issued after KYC approval)
    
    Body:
        amount: Payment amount in cents (e.g., 1000 = $10.00)
        currency: ISO currency code (e.g., USD, EUR)
        order_id: Merchant's order reference
        customer_email: (optional) Customer email for receipt
        description: (optional) Payment description
        metadata: (optional) Additional metadata
    
    Returns:
        PaymentExecuteResponse with payment status and details
    """
    try:
        # 1. Verify merchant API key
        merchant = await verify_merchant_api_key(x_merchant_api_key)
        logger.info(f"Payment request from merchant: {merchant['merchant_id']} ({merchant['business_name']})")
        
        # 2. Get merchant's PSP configuration
        psp_route = await get_merchant_psp_route(merchant["merchant_id"])
        if not psp_route:
            raise HTTPException(
                status_code=400,
                detail="Payment routing not configured"
            )
        
        psp_type = psp_route["psp_type"]
        logger.info(f"Routing payment to PSP: {psp_type}")
        
        # 3. Execute payment through the configured PSP
        if psp_type == "stripe":
            result = await execute_stripe_payment(merchant, payment_request)
        elif psp_type == "adyen":
            result = await execute_adyen_payment(merchant, payment_request)
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported PSP: {psp_type}"
            )
        
        # 4. Return response
        return PaymentExecuteResponse(
            success=result["success"],
            payment_id=result["payment_id"],
            order_id=payment_request.order_id,
            amount=payment_request.amount,
            currency=payment_request.currency,
            psp_used=psp_type,
            status=result["status"],
            transaction_id=result.get("transaction_id"),
            error_message=result.get("error_message"),
            timestamp=datetime.now().isoformat()
        )
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Payment execution failed: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Payment execution failed: {str(e)}"
        )


@router.get("/health")
async def payment_router_health():
    """Health check for payment router"""
    return {
        "status": "healthy",
        "service": "payment-execution-router",
        "timestamp": datetime.now().isoformat()
    }

