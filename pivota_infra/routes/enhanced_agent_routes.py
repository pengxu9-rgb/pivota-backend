"""
Enhanced Agent Routes with Full Agent → Merchant → Pivota → PSP Flow
Integrates MCP API for complete order processing
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_router.merchant_store import get_merchant_inventory, create_order, update_order_status, OrderStatus
from pivota_infra.routes.agent_routes import get_best_psp, update_psp_metrics, simulate_payment_processing
from pivota_infra.utils.logger import logger
import time
import asyncio

router = APIRouter(prefix="/agent", tags=["enhanced-agent"])

# Request/Response Models
class AgentOrderItem(BaseModel):
    sku: str
    quantity: int

class AgentOrderRequest(BaseModel):
    agent_id: str
    merchant_id: str
    items: List[AgentOrderItem]
    customer_email: Optional[str] = None
    shipping_address: Optional[Dict[str, Any]] = None

class AgentOrderResponse(BaseModel):
    order_id: str
    agent_id: str
    merchant_id: str
    status: str
    total_amount: float
    currency: str
    items: List[Dict[str, Any]]
    payment_intent_id: Optional[str] = None
    psp_used: Optional[str] = None
    payment_status: Optional[str] = None
    processing_latency_ms: Optional[int] = None
    ai_selected_psp: Optional[str] = None

class AgentBrowseRequest(BaseModel):
    agent_id: str
    merchant_id: Optional[str] = None

class AgentBrowseResponse(BaseModel):
    agent_id: str
    merchants: Dict[str, Any]
    selected_merchant: Optional[str] = None

@router.post("/browse", response_model=AgentBrowseResponse)
async def agent_browse_merchants(request: AgentBrowseRequest):
    """Agent browses available merchants and their inventory"""
    try:
        from ai_router.merchant_store import get_all_merchants
        
        merchants = get_all_merchants()
        
        # If specific merchant requested, filter to that one
        if request.merchant_id:
            if request.merchant_id in merchants:
                merchants = {request.merchant_id: merchants[request.merchant_id]}
            else:
                raise HTTPException(status_code=404, detail="Merchant not found")
        
        logger.info(f"Agent {request.agent_id} browsing merchants")
        
        return AgentBrowseResponse(
            agent_id=request.agent_id,
            merchants=merchants,
            selected_merchant=request.merchant_id
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error in agent browse: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to browse merchants")

@router.post("/order", response_model=AgentOrderResponse)
async def agent_create_order(request: AgentOrderRequest):
    """Agent creates an order with full payment processing"""
    start_time = time.time()
    
    try:
        # 1. Get merchant inventory to validate items and get prices
        merchant = get_merchant_inventory(request.merchant_id)
        if not merchant:
            raise HTTPException(status_code=404, detail="Merchant not found")
        
        # 2. Validate items and get prices
        validated_items = []
        total_amount = 0.0
        
        for item in request.items:
            if item.sku not in merchant["stock"]:
                raise HTTPException(status_code=400, detail=f"SKU {item.sku} not found in merchant inventory")
            
            stock_item = merchant["stock"][item.sku]
            if stock_item["quantity"] < item.quantity:
                raise HTTPException(status_code=400, detail=f"Insufficient stock for {item.sku}")
            
            unit_price = stock_item["price"]
            total_price = unit_price * item.quantity
            total_amount += total_price
            
            validated_items.append({
                "sku": item.sku,
                "quantity": item.quantity,
                "unit_price": unit_price,
                "total_price": total_price
            })
        
        # 3. Create order
        order = create_order(
            agent_id=request.agent_id,
            merchant_id=request.merchant_id,
            items=validated_items,
            customer_email=request.customer_email,
            shipping_address=request.shipping_address
        )
        
        if not order:
            raise HTTPException(status_code=400, detail="Failed to create order")
        
        # 4. AI-powered PSP selection
        try:
            ai_selected_psp = get_best_psp()
            logger.info(f"AI selected PSP: {ai_selected_psp} for order {order.order_id}")
        except Exception as e:
            logger.warning(f"AI PSP selection failed, using stripe: {e}")
            ai_selected_psp = "stripe"
        
        # 5. Simulate payment processing
        payment_result = await simulate_payment_processing(ai_selected_psp, total_amount, merchant["currency"])
        processing_latency = int((time.time() - start_time) * 1000)
        
        # 6. Update AI metrics
        update_psp_metrics(ai_selected_psp, payment_result["success"], payment_result["latency_ms"])
        
        # 7. Update order status based on payment result
        if payment_result["success"]:
            order_status = OrderStatus.PAID
            payment_status = "succeeded"
        else:
            order_status = OrderStatus.FAILED
            payment_status = "failed"
        
        update_order_status(
            order_id=order.order_id,
            status=order_status,
            payment_intent_id=f"pi_demo_{int(total_amount)}_{merchant['currency']}",
            psp_used=ai_selected_psp,
            payment_status=payment_status
        )
        
        logger.info(f"Order {order.order_id} processed: {payment_status} via {ai_selected_psp}")
        
        return AgentOrderResponse(
            order_id=order.order_id,
            agent_id=order.agent_id,
            merchant_id=order.merchant_id,
            status=order_status.value,
            total_amount=order.total_amount,
            currency=order.currency,
            items=validated_items,
            payment_intent_id=f"pi_demo_{int(total_amount)}_{merchant['currency']}",
            psp_used=ai_selected_psp,
            payment_status=payment_status,
            processing_latency_ms=processing_latency,
            ai_selected_psp=ai_selected_psp
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error in agent order creation: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Order processing failed: {str(e)}")

@router.get("/orders/{agent_id}")
async def get_agent_orders(agent_id: str):
    """Get all orders for an agent"""
    try:
        from ai_router.merchant_store import get_orders_by_agent
        
        orders = get_orders_by_agent(agent_id)
        
        return {
            "agent_id": agent_id,
            "total_orders": len(orders),
            "orders": [order.to_dict() for order in orders]
        }
        
    except Exception as e:
        logger.exception(f"Error getting agent orders: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to get agent orders")

@router.get("/inventory/{merchant_id}")
async def get_merchant_inventory_for_agent(merchant_id: str):
    """Get merchant inventory for agent browsing"""
    try:
        inventory = get_merchant_inventory(merchant_id)
        if not inventory:
            raise HTTPException(status_code=404, detail="Merchant not found")
        
        return {
            "merchant_id": merchant_id,
            "name": inventory["name"],
            "location": inventory["location"],
            "currency": inventory["currency"],
            "inventory": inventory["stock"]
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error getting merchant inventory: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to get merchant inventory")

@router.get("/merchants")
async def get_available_merchants():
    """Get all available merchants for agent browsing"""
    try:
        from ai_router.merchant_store import get_all_merchants
        
        merchants = get_all_merchants()
        
        # Return simplified merchant info for browsing
        merchant_list = []
        for merchant_id, merchant_data in merchants.items():
            merchant_list.append({
                "merchant_id": merchant_id,
                "name": merchant_data["name"],
                "location": merchant_data["location"],
                "currency": merchant_data["currency"],
                "total_items": len(merchant_data["stock"]),
                "total_quantity": sum(item["quantity"] for item in merchant_data["stock"].values())
            })
        
        return {
            "merchants": merchant_list,
            "total_merchants": len(merchant_list)
        }
        
    except Exception as e:
        logger.exception(f"Error getting merchants: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to get merchants")

@router.get("/health")
async def agent_health_check():
    """Agent API health check"""
    return {
        "status": "healthy",
        "service": "Enhanced Agent API",
        "features": [
            "merchant_browsing",
            "order_creation", 
            "ai_psp_selection",
            "payment_processing",
            "order_tracking"
        ]
    }

