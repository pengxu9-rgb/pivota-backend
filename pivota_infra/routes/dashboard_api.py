"""
Dashboard API Routes
REST API endpoints for the Payment Infrastructure Dashboard
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
from fastapi import APIRouter, HTTPException, Depends, Query, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel

from dashboard.core import dashboard_core, User, UserRole
from utils.auth import verify_jwt_token, get_current_user

logger = logging.getLogger("dashboard_api")
router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])
security = HTTPBearer()

# Request/Response Models
class LoginRequest(BaseModel):
    username: str
    password: str

class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: Dict[str, Any]

class OrderResponse(BaseModel):
    id: str
    merchant_id: str
    agent_id: Optional[str]
    customer_email: str
    total_amount: float
    currency: str
    status: str
    created_at: str
    updated_at: str

class PaymentResponse(BaseModel):
    id: str
    order_id: str
    amount: float
    currency: str
    psp: str
    status: str
    transaction_id: Optional[str]
    fees: float
    created_at: str

class AnalyticsResponse(BaseModel):
    total_orders: int
    total_payments: int
    total_volume: float
    success_rate: float
    average_order_value: float
    psp_breakdown: Dict[str, Any]
    currency_breakdown: Dict[str, int]
    time_series: List[Dict[str, Any]]

# Authentication dependency
async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> User:
    """Get current authenticated user"""
    try:
        token = credentials.credentials
        user_info = verify_jwt_token(token)
        
        # Find user in dashboard core
        user = None
        for u in dashboard_core.users.values():
            if u.id == user_info.get("sub") or u.username == user_info.get("sub"):
                user = u
                break
        
        if not user:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
        
        return user
    except Exception as e:
        logger.error(f"Authentication error: {e}")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

# Authentication endpoints
@router.post("/auth/login", response_model=LoginResponse)
async def login(request: LoginRequest):
    """Authenticate user and return JWT token"""
    try:
        user = await dashboard_core.authenticate_user(request.username, request.password)
        if not user:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
        
        # Create JWT token (using existing auth system)
        from utils.auth import create_jwt_token
        from datetime import timedelta
        
        access_token = create_jwt_token(
            user_id=user.id,
            role=user.role.value,
            entity_id=user.entity_id,
            expires_delta=timedelta(hours=24)
        )
        
        logger.info(f"User {user.username} logged in successfully")
        return LoginResponse(
            access_token=access_token,
            user={
                "id": user.id,
                "username": user.username,
                "email": user.email,
                "role": user.role.value,
                "entity_id": user.entity_id,
                "permissions": user.permissions
            }
        )
    except Exception as e:
        logger.error(f"Login error: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Login failed")

@router.get("/auth/me")
async def get_current_user_info(current_user: User = Depends(get_current_user)):
    """Get current user information"""
    return {
        "id": current_user.id,
        "username": current_user.username,
        "email": current_user.email,
        "role": current_user.role.value,
        "entity_id": current_user.entity_id,
        "permissions": current_user.permissions
    }

# Orders endpoints
@router.get("/orders", response_model=List[OrderResponse])
async def get_orders(
    current_user: User = Depends(get_current_user),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0)
):
    """Get orders for the current user"""
    try:
        orders = await dashboard_core.get_user_orders(current_user, limit + offset)
        orders = orders[offset:offset + limit]
        
        return [
            OrderResponse(
                id=order.id,
                merchant_id=order.merchant_id,
                agent_id=order.agent_id,
                customer_email=order.customer_email,
                total_amount=order.total_amount,
                currency=order.currency,
                status=order.status.value,
                created_at=order.created_at.isoformat(),
                updated_at=order.updated_at.isoformat()
            )
            for order in orders
        ]
    except Exception as e:
        logger.error(f"Error getting orders: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to get orders")

@router.get("/orders/{order_id}", response_model=OrderResponse)
async def get_order(
    order_id: str,
    current_user: User = Depends(get_current_user)
):
    """Get specific order details"""
    try:
        # Check if order exists and user has access
        orders = await dashboard_core.get_user_orders(current_user)
        order = next((o for o in orders if o.id == order_id), None)
        
        if not order:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
        
        return OrderResponse(
            id=order.id,
            merchant_id=order.merchant_id,
            agent_id=order.agent_id,
            customer_email=order.customer_email,
            total_amount=order.total_amount,
            currency=order.currency,
            status=order.status.value,
            created_at=order.created_at.isoformat(),
            updated_at=order.updated_at.isoformat()
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting order {order_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to get order")

# Payments endpoints
@router.get("/payments", response_model=List[PaymentResponse])
async def get_payments(
    current_user: User = Depends(get_current_user),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0)
):
    """Get payments for the current user"""
    try:
        payments = await dashboard_core.get_user_payments(current_user, limit + offset)
        payments = payments[offset:offset + limit]
        
        return [
            PaymentResponse(
                id=payment.id,
                order_id=payment.order_id,
                amount=payment.amount,
                currency=payment.currency,
                psp=payment.psp.value if payment.psp else "unknown",
                status=payment.status,
                transaction_id=payment.transaction_id,
                fees=payment.fees,
                created_at=payment.created_at.isoformat()
            )
            for payment in payments
        ]
    except Exception as e:
        logger.error(f"Error getting payments: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to get payments")

# Analytics endpoints
@router.get("/analytics", response_model=AnalyticsResponse)
async def get_analytics(
    current_user: User = Depends(get_current_user),
    time_range: str = Query("7d", regex="^(1d|7d|30d|90d)$")
):
    """Get analytics data for the current user"""
    try:
        analytics = await dashboard_core.get_analytics(current_user, time_range)
        return AnalyticsResponse(**analytics)
    except Exception as e:
        logger.error(f"Error getting analytics: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to get analytics")

# PSP Management endpoints
@router.get("/psps")
async def get_psps(current_user: User = Depends(get_current_user)):
    """Get PSP configurations (admin/operator only)"""
    if current_user.role not in [UserRole.PIVOTA_ADMIN, UserRole.PIVOTA_OPERATOR]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")
    
    try:
        psp_list = []
        for psp in dashboard_core.psp_configs.values():
            psp_list.append({
                "id": psp.id,
                "name": psp.name,
                "type": psp.type.value,
                "is_active": psp.is_active,
                "supported_currencies": psp.supported_currencies,
                "supported_countries": psp.supported_countries,
                "fees": psp.fees
            })
        return psp_list
    except Exception as e:
        logger.error(f"Error getting PSPs: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to get PSPs")

# System status endpoint
@router.get("/status")
async def get_system_status(current_user: User = Depends(get_current_user)):
    """Get system status and health"""
    try:
        # Get basic system metrics
        total_orders = len(dashboard_core.orders)
        total_payments = len(dashboard_core.payments)
        active_psps = len([psp for psp in dashboard_core.psp_configs.values() if psp.is_active])
        
        return {
            "status": "healthy",
            "timestamp": datetime.now().isoformat(),
            "metrics": {
                "total_orders": total_orders,
                "total_payments": total_payments,
                "active_psps": active_psps,
                "total_users": len(dashboard_core.users)
            },
            "user_role": current_user.role.value,
            "permissions": current_user.permissions
        }
    except Exception as e:
        logger.error(f"Error getting system status: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to get system status")
