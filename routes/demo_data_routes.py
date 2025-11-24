"""
Demo Data Routes
API endpoints for populating demo data
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, Any
from fastapi import APIRouter, HTTPException, Depends, status
from pydantic import BaseModel

from dashboard.core import dashboard_core, User, UserRole, Order, Payment, OrderStatus, PSPType
from utils.auth import get_current_user

logger = logging.getLogger("demo_data_routes")
router = APIRouter(prefix="/api/demo", tags=["demo_data"])

class DemoDataResponse(BaseModel):
    success: bool
    message: str
    data: Dict[str, Any]

@router.post("/populate", response_model=DemoDataResponse)
async def populate_demo_data(current_user: User = Depends(get_current_user)):
    """Populate dashboard with demo data"""
    try:
        # Check permissions (admin/operator only)
        if current_user.role not in [UserRole.PIVOTA_ADMIN, UserRole.PIVOTA_OPERATOR]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only admin/operator can populate demo data"
            )
        
        # Create demo orders
        demo_orders = [
            Order(
                id="order_demo_001",
                merchant_id="MERCH_001",
                agent_id="AGENT_001",
                customer_email="customer1@example.com",
                total_amount=29.99,
                currency="USD",
                status=OrderStatus.PAID,
                items=[{"name": "T-Shirt", "quantity": 1, "price": 29.99}],
                payment_method="card",
                psp_used="stripe",
                created_at=datetime.now() - timedelta(hours=2),
                updated_at=datetime.now() - timedelta(hours=2),
                metadata={"source": "demo"}
            ),
            Order(
                id="order_demo_002",
                merchant_id="MERCH_001",
                agent_id="AGENT_002",
                customer_email="customer2@example.com",
                total_amount=79.99,
                currency="EUR",
                status=OrderStatus.PAID,
                items=[{"name": "Hoodie", "quantity": 1, "price": 79.99}],
                payment_method="card",
                psp_used="adyen",
                created_at=datetime.now() - timedelta(hours=1),
                updated_at=datetime.now() - timedelta(hours=1),
                metadata={"source": "demo"}
            ),
            Order(
                id="order_demo_003",
                merchant_id="MERCH_002",
                agent_id="AGENT_001",
                customer_email="customer3@example.com",
                total_amount=149.99,
                currency="USD",
                status=OrderStatus.FAILED,
                items=[{"name": "Premium Package", "quantity": 1, "price": 149.99}],
                payment_method="card",
                psp_used="paypal",
                created_at=datetime.now() - timedelta(minutes=30),
                updated_at=datetime.now() - timedelta(minutes=30),
                metadata={"source": "demo"}
            )
        ]
        
        # Create demo payments
        demo_payments = [
            Payment(
                id="payment_demo_001",
                order_id="order_demo_001",
                amount=29.99,
                currency="USD",
                psp=PSPType.STRIPE,
                status="succeeded",
                transaction_id="pi_stripe_demo_001",
                fees=1.17,
                created_at=datetime.now() - timedelta(hours=2),
                metadata={"source": "demo"}
            ),
            Payment(
                id="payment_demo_002",
                order_id="order_demo_002",
                amount=79.99,
                currency="EUR",
                psp=PSPType.ADYEN,
                status="succeeded",
                transaction_id="pi_adyen_demo_002",
                fees=1.37,
                created_at=datetime.now() - timedelta(hours=1),
                metadata={"source": "demo"}
            ),
            Payment(
                id="payment_demo_003",
                order_id="order_demo_003",
                amount=149.99,
                currency="USD",
                psp=PSPType.PAYPAL,
                status="failed",
                transaction_id=None,
                fees=0.0,
                created_at=datetime.now() - timedelta(minutes=30),
                metadata={"source": "demo", "error": "Insufficient funds"}
            )
        ]
        
        # Add to dashboard core
        for order in demo_orders:
            dashboard_core.orders[order.id] = order
        
        for payment in demo_payments:
            dashboard_core.payments[payment.id] = payment
        
        logger.info(f"Demo data populated: {len(demo_orders)} orders, {len(demo_payments)} payments")
        
        return DemoDataResponse(
            success=True,
            message="Demo data populated successfully",
            data={
                "orders_created": len(demo_orders),
                "payments_created": len(demo_payments),
                "total_orders": len(dashboard_core.orders),
                "total_payments": len(dashboard_core.payments)
            }
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to populate demo data: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to populate demo data: {str(e)}"
        )

@router.delete("/clear", response_model=DemoDataResponse)
async def clear_demo_data(current_user: User = Depends(get_current_user)):
    """Clear all demo data"""
    try:
        # Check permissions (admin/operator only)
        if current_user.role not in [UserRole.PIVOTA_ADMIN, UserRole.PIVOTA_OPERATOR]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only admin/operator can clear demo data"
            )
        
        # Clear demo data (keep users and PSP configs)
        demo_orders = [oid for oid, order in dashboard_core.orders.items() 
                      if order.metadata.get("source") == "demo"]
        demo_payments = [pid for pid, payment in dashboard_core.payments.items() 
                        if payment.metadata.get("source") == "demo"]
        
        for order_id in demo_orders:
            del dashboard_core.orders[order_id]
        
        for payment_id in demo_payments:
            del dashboard_core.payments[payment_id]
        
        logger.info(f"Demo data cleared: {len(demo_orders)} orders, {len(demo_payments)} payments")
        
        return DemoDataResponse(
            success=True,
            message="Demo data cleared successfully",
            data={
                "orders_cleared": len(demo_orders),
                "payments_cleared": len(demo_payments),
                "total_orders": len(dashboard_core.orders),
                "total_payments": len(dashboard_core.payments)
            }
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to clear demo data: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to clear demo data: {str(e)}"
        )
