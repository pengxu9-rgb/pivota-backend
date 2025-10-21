"""
Unified Payment Endpoint for Agent SDK
Provides production-ready payment processing with PSP integration
"""
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any
from decimal import Decimal
from datetime import datetime

from routes.agent_auth import AgentContext, get_agent_context, log_agent_request
from db.merchant_onboarding import get_merchant_onboarding
from db.orders import get_order, update_payment_info
from adapters.psp_adapter import get_psp_adapter
from adapters.multi_psp_orchestrator import MultiPSPOrchestrator
from utils.logger import logger

router = APIRouter(prefix="/agent/v1", tags=["agent-payments"])

# ============================================================================
# Request/Response Models
# ============================================================================

class PaymentMethod(BaseModel):
    """Payment method details"""
    type: str = Field(..., description="card, bank_transfer, wallet")
    token: Optional[str] = Field(None, description="Payment token from PSP (e.g., Stripe token)")
    card_last4: Optional[str] = Field(None, description="Last 4 digits of card")
    brand: Optional[str] = Field(None, description="Card brand (visa, mastercard)")

class PaymentRequest(BaseModel):
    """Unified payment request"""
    order_id: str = Field(..., description="Order ID to create payment for")
    payment_method: PaymentMethod = Field(..., description="Payment method details")
    return_url: Optional[str] = Field(None, description="URL for 3DS redirect callback")
    idempotency_key: Optional[str] = Field(None, description="Prevent duplicate payments")
    save_payment_method: bool = Field(False, description="Save for future use")

class NextAction(BaseModel):
    """Next action for 3DS or additional verification"""
    type: str  # redirect_to_url, display_qr_code, etc.
    redirect_url: Optional[str] = None
    qr_code_data: Optional[str] = None

class PaymentResponse(BaseModel):
    """Unified payment response"""
    status: str  # requires_action, processing, succeeded, failed
    payment_id: str
    payment_intent_id: str
    client_secret: Optional[str] = None
    amount: float
    currency: str
    psp_used: str
    next_action: Optional[NextAction] = None
    error: Optional[str] = None
    created_at: str

# ============================================================================
# Payment Endpoint
# ============================================================================

@router.post("/payments", response_model=PaymentResponse)
async def create_payment(
    request: PaymentRequest,
    context: AgentContext = Depends(get_agent_context),
    background_tasks: BackgroundTasks = BackgroundTasks()
):
    """
    Create payment for an order
    
    Flow:
    1. Validate order exists and not already paid
    2. Get merchant PSP configuration
    3. Create payment intent with PSP
    4. Handle 3DS if required
    5. Return payment status
    
    Features:
    - Automatic PSP failover
    - 3DS authentication support
    - Idempotency protection
    - Payment retry logic
    """
    try:
        # 1. Get order and validate
        order = await get_order(request.order_id)
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")
        
        if order.get("payment_status") == "paid":
            raise HTTPException(status_code=400, detail="Order already paid")
        
        merchant_id = order.get("merchant_id")
        
        # 2. Verify agent has access to merchant
        if not context.can_access_merchant(merchant_id):
            raise HTTPException(status_code=403, detail="Not authorized for this merchant")
        
        # 3. Check idempotency
        if request.idempotency_key:
            # Check if payment with this key already exists
            from db.database import database
            existing = await database.fetch_one(
                """SELECT payment_id, payment_intent_id, status 
                   FROM payments 
                   WHERE idempotency_key = :key AND order_id = :order_id""",
                {"key": request.idempotency_key, "order_id": request.order_id}
            )
            if existing:
                logger.info(f"Returning existing payment for idempotency key: {request.idempotency_key}")
                return PaymentResponse(
                    status=existing["status"],
                    payment_id=existing["payment_id"],
                    payment_intent_id=existing["payment_intent_id"],
                    amount=order["total_amount"],
                    currency=order["currency"],
                    psp_used="cached",
                    created_at=datetime.now().isoformat()
                )
        
        # 4. Get merchant and PSP config
        merchant = await get_merchant_onboarding(merchant_id)
        if not merchant:
            raise HTTPException(status_code=404, detail="Merchant not found")
        
        # 5. Create payment intent with PSP orchestrator (automatic failover)
        orchestrator = MultiPSPOrchestrator(merchant_id)
        
        amount = Decimal(str(order["total_amount"]))
        currency = order.get("currency", "USD")
        
        success, payment_intent, error, psp_used = await orchestrator.create_payment_intent(
            amount=amount,
            currency=currency,
            metadata={
                "order_id": request.order_id,
                "agent_id": context.agent_id,
                "payment_method_type": request.payment_method.type,
                "idempotency_key": request.idempotency_key
            }
        )
        
        if not success:
            logger.error(f"Payment intent creation failed: {error}")
            raise HTTPException(status_code=500, detail=f"Payment failed: {error}")
        
        # 6. Determine if 3DS or other action required
        next_action = None
        status = "processing"
        
        if payment_intent.status == "requires_action":
            status = "requires_action"
            # Check if 3DS redirect needed
            if hasattr(payment_intent, 'next_action'):
                next_action = NextAction(
                    type="redirect_to_url",
                    redirect_url=payment_intent.next_action.get("redirect_to_url", {}).get("url")
                )
        elif payment_intent.status == "succeeded":
            status = "succeeded"
        
        # 7. Store payment record
        from db.database import database
        payment_id = f"pay_{payment_intent.id}"
        
        await database.execute(
            """INSERT INTO payments 
               (payment_id, order_id, payment_intent_id, amount, currency, 
                psp_type, status, idempotency_key, created_at, agent_id)
               VALUES (:payment_id, :order_id, :intent_id, :amount, :currency,
                       :psp, :status, :idem_key, :created_at, :agent_id)""",
            {
                "payment_id": payment_id,
                "order_id": request.order_id,
                "intent_id": payment_intent.id,
                "amount": float(amount),
                "currency": currency,
                "psp": psp_used,
                "status": status,
                "idem_key": request.idempotency_key,
                "created_at": datetime.now(),
                "agent_id": context.agent_id
            }
        )
        
        # 8. Update order payment status
        await update_payment_info(
            order_id=request.order_id,
            payment_intent_id=payment_intent.id,
            psp_type=psp_used
        )
        
        # 9. Log request
        background_tasks.add_task(
            log_agent_request,
            context=context,
            status_code=200,
            merchant_id=merchant_id
        )
        
        logger.info(f"Payment created: {payment_id} for order {request.order_id} via {psp_used}")
        
        return PaymentResponse(
            status=status,
            payment_id=payment_id,
            payment_intent_id=payment_intent.id,
            client_secret=payment_intent.client_secret if hasattr(payment_intent, 'client_secret') else None,
            amount=float(amount),
            currency=currency,
            psp_used=psp_used,
            next_action=next_action,
            created_at=datetime.now().isoformat()
        )
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Payment creation error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Payment processing failed: {str(e)}")

# ============================================================================
# Payment Status Check
# ============================================================================

@router.get("/payments/{payment_id}")
async def get_payment_status(
    payment_id: str,
    context: AgentContext = Depends(get_agent_context)
):
    """
    Get payment status
    
    Returns current status of payment including:
    - Payment status (requires_action, processing, succeeded, failed)
    - PSP used
    - Amount and currency
    - Next action if required
    """
    try:
        from db.database import database
        
        payment = await database.fetch_one(
            """SELECT p.*, o.merchant_id 
               FROM payments p
               JOIN orders o ON p.order_id = o.order_id
               WHERE p.payment_id = :payment_id""",
            {"payment_id": payment_id}
        )
        
        if not payment:
            raise HTTPException(status_code=404, detail="Payment not found")
        
        # Verify access
        if not context.can_access_merchant(payment["merchant_id"]):
            raise HTTPException(status_code=403, detail="Not authorized")
        
        return {
            "status": "success",
            "payment": {
                "payment_id": payment["payment_id"],
                "order_id": payment["order_id"],
                "status": payment["status"],
                "amount": payment["amount"],
                "currency": payment["currency"],
                "psp_used": payment["psp_type"],
                "created_at": payment["created_at"].isoformat(),
                "updated_at": payment.get("updated_at").isoformat() if payment.get("updated_at") else None
            }
        }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Get payment status error: {e}")
        raise HTTPException(status_code=500, detail="Failed to get payment status")

