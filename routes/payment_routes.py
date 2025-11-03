"""
Payment Routes
API endpoints for payment processing and orchestration
"""

import logging
from datetime import datetime
from typing import Dict, List, Any, Optional
from fastapi import APIRouter, HTTPException, Depends, status
from pydantic import BaseModel

from orchestrator.payment_orchestrator import payment_orchestrator, OrchestrationResult
from dashboard.core import dashboard_core, User
from routes.dashboard_api import get_current_user

logger = logging.getLogger("payment_routes")
router = APIRouter(prefix="/api/payments", tags=["payments"])

# Request/Response Models
class PaymentRequest(BaseModel):
    merchant_id: str
    agent_id: Optional[str] = None
    customer_email: str
    total_amount: float
    currency: str = "USD"
    payment_method: str = "card"
    items: List[Dict[str, Any]] = []
    metadata: Dict[str, Any] = {}
    preferred_psp: Optional[str] = None

class PaymentResponse(BaseModel):
    success: bool
    order_id: str
    payment_id: Optional[str]
    psp_used: Optional[str]
    transaction_id: Optional[str]
    amount: float
    currency: str
    fees: float
    error_message: Optional[str] = None
    metadata: Dict[str, Any] = {}

class RetryRequest(BaseModel):
    order_id: str
    retry_count: int = 1

# Payment processing endpoints
@router.post("/process", response_model=PaymentResponse)
async def process_payment(
    request: PaymentRequest,
    current_user: User = Depends(get_current_user)
):
    """Process a payment for an order"""
    try:
        # Validate user permissions
        if current_user.role.value not in ["pivota_admin", "pivota_operator", "merchant", "agent"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, 
                detail="Insufficient permissions for payment processing"
            )
        
        # Convert request to order data
        order_data = {
            "id": f"order_{datetime.now().timestamp()}",
            "merchant_id": request.merchant_id,
            "agent_id": request.agent_id,
            "customer_email": request.customer_email,
            "total_amount": request.total_amount,
            "currency": request.currency,
            "payment_method": request.payment_method,
            "items": request.items,
            "metadata": request.metadata
        }
        
        logger.info(f"Processing payment for order {order_data['id']} by user {current_user.username}")
        
        # Process payment through orchestrator
        result = await payment_orchestrator.process_order_payment(
            order_data, 
            request.preferred_psp
        )
        
        return PaymentResponse(
            success=result.success,
            order_id=result.order_id,
            payment_id=result.payment_id,
            psp_used=result.psp_used,
            transaction_id=result.transaction_id,
            amount=result.amount,
            currency=result.currency,
            fees=result.fees,
            error_message=result.error_message,
            metadata=result.metadata or {}
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Payment processing failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Payment processing failed: {str(e)}"
        )

@router.post("/retry", response_model=PaymentResponse)
async def retry_payment(
    request: RetryRequest,
    current_user: User = Depends(get_current_user)
):
    """Retry a failed payment"""
    try:
        # Validate user permissions
        if current_user.role.value not in ["pivota_admin", "pivota_operator"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only admin/operator can retry payments"
            )
        
        logger.info(f"Retrying payment for order {request.order_id} by user {current_user.username}")
        
        # Retry payment
        result = await payment_orchestrator.retry_failed_payment(
            request.order_id,
            request.retry_count
        )
        
        return PaymentResponse(
            success=result.success,
            order_id=result.order_id,
            payment_id=result.payment_id,
            psp_used=result.psp_used,
            transaction_id=result.transaction_id,
            amount=result.amount,
            currency=result.currency,
            fees=result.fees,
            error_message=result.error_message,
            metadata=result.metadata or {}
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Payment retry failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Payment retry failed: {str(e)}"
        )

@router.get("/status")
async def get_payment_status(
    current_user: User = Depends(get_current_user)
):
    """Get payment system status"""
    try:
        status = await payment_orchestrator.get_orchestration_status()
        return status
    except Exception as e:
        logger.error(f"Failed to get payment status: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get payment status: {str(e)}"
        )

@router.get("/psps")
async def get_available_psps(
    current_user: User = Depends(get_current_user)
):
    """Get available payment service providers"""
    try:
        from psp.connectors import psp_manager
        psp_status = await psp_manager.get_psp_status()
        
        return {
            "available_psps": list(psp_status.keys()),
            "psp_status": psp_status,
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        logger.error(f"Failed to get PSP status: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get PSP status: {str(e)}"
        )

@router.get("/orders/{order_id}")
async def get_order_payment_status(
    order_id: str,
    current_user: User = Depends(get_current_user)
):
    """Get payment status for a specific order"""
    try:
        # Check if order exists
        order = dashboard_core.orders.get(order_id)
        if not order:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Order not found"
            )
        
        # Check user permissions
        if current_user.role.value == "merchant" and order.merchant_id != current_user.entity_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied to this order"
            )
        
        if current_user.role.value == "agent" and order.agent_id != current_user.entity_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied to this order"
            )
        
        # Get payment information
        payments = [p for p in dashboard_core.payments.values() if p.order_id == order_id]
        
        return {
            "order_id": order_id,
            "order_status": order.status.value,
            "total_amount": order.total_amount,
            "currency": order.currency,
            "payments": [
                {
                    "payment_id": p.id,
                    "amount": p.amount,
                    "psp": p.psp.value,
                    "status": p.status,
                    "transaction_id": p.transaction_id,
                    "fees": p.fees,
                    "created_at": p.created_at.isoformat()
                }
                for p in payments
            ],
            "created_at": order.created_at.isoformat(),
            "updated_at": order.updated_at.isoformat()
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get order payment status: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get order payment status: {str(e)}"
        )