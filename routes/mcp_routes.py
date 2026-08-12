"""
Legacy "MCP" (Merchant Communication Protocol) simulation routes — FAIL CLOSED.

This module never talked to a merchant. It is an in-memory SIMULATION over the
hardcoded fixture in `ai_router/merchant_store.py` ("Cool Shoes EU", "Red
Sneakers Size 42", ...). A prior audit fail-closed the WRITES (they raise 501);
the READS were left serving that fixture, and were still doing so in production
(GET /mcp/inventory/merchants returned the fake catalog) until 2026-08-11.

Every endpoint now refuses with 501. Nothing here is a real inventory, order, or
health signal. The whole module is a deletion candidate — it is kept only so the
refusal is discoverable by anything still pointed at these paths.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_router.merchant_store import (
    get_merchant_inventory, get_all_merchants, create_order, get_order,
    update_order_status, get_orders_by_agent, get_orders_by_merchant,
    get_orders_by_status, cancel_order, get_inventory_summary, get_orders_summary,
    OrderStatus
)
from utils.logger import logger

# No prefix here: main.py mounts this router at the real prefix AND at the
# legacy /mcp alias (see PLATFORM_CONNECTORS_PREFIX / LEGACY_MCP_PREFIX).
router = APIRouter(tags=["platform-connectors-legacy-simulation"])

# Request/Response Models
class InventoryItem(BaseModel):
    sku: str
    name: str
    price: float
    quantity: int

class MerchantInfo(BaseModel):
    merchant_id: str
    name: str
    location: str
    currency: str
    inventory: List[InventoryItem]

class OrderItemRequest(BaseModel):
    sku: str
    quantity: int
    unit_price: float

class CreateOrderRequest(BaseModel):
    agent_id: str
    merchant_id: str
    items: List[OrderItemRequest]
    customer_email: Optional[str] = None
    shipping_address: Optional[Dict[str, Any]] = None

class OrderResponse(BaseModel):
    order_id: str
    agent_id: str
    merchant_id: str
    items: List[Dict[str, Any]]
    status: str
    total_amount: float
    currency: str
    created_at: str
    updated_at: str
    payment_intent_id: Optional[str] = None
    psp_used: Optional[str] = None
    payment_status: Optional[str] = None

class OrderStatusUpdate(BaseModel):
    order_id: str
    status: str
    payment_intent_id: Optional[str] = None
    psp_used: Optional[str] = None
    payment_status: Optional[str] = None

# MCP API Endpoints

@router.get("/inventory/merchants", response_model=Dict[str, MerchantInfo])
async def get_inventory_merchants():
    """Get the legacy inventory snapshot across all merchants"""
    # FAIL CLOSED: this endpoint read from an in-memory SIMULATION fixture
    # (ai_router/merchant_store.py), not from any merchant. It served that
    # fabricated inventory/order data in production until 2026-08-11.
    raise HTTPException(
        status_code=501,
        detail="This endpoint is a simulation surface and is disabled. It never reflected real merchant inventory or orders; use the production catalog / order APIs.",
    )
    try:
        merchants = get_all_merchants()
        result = {}
        
        for merchant_id, merchant_data in merchants.items():
            inventory_items = []
            for sku, item_data in merchant_data["stock"].items():
                inventory_items.append(InventoryItem(
                    sku=sku,
                    name=item_data["name"],
                    price=item_data["price"],
                    quantity=item_data["quantity"]
                ))
            
            result[merchant_id] = MerchantInfo(
                merchant_id=merchant_id,
                name=merchant_data["name"],
                location=merchant_data["location"],
                currency=merchant_data["currency"],
                inventory=inventory_items
            )
        
        return result
    except Exception as e:
        logger.exception(f"Error getting merchants: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to get merchants")

@router.get("/merchants/{merchant_id}/inventory")
async def get_merchant_inventory_endpoint(merchant_id: str):
    """Get specific merchant inventory"""
    # FAIL CLOSED: this endpoint read from an in-memory SIMULATION fixture
    # (ai_router/merchant_store.py), not from any merchant. It served that
    # fabricated inventory/order data in production until 2026-08-11.
    raise HTTPException(
        status_code=501,
        detail="This endpoint is a simulation surface and is disabled. It never reflected real merchant inventory or orders; use the production catalog / order APIs.",
    )
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

@router.post("/orders", response_model=OrderResponse)
async def create_order_endpoint(request: CreateOrderRequest):
    """Create a new order"""
    # FAIL CLOSED: /mcp/orders is an in-memory SIMULATION store (module docstring:
    # "Simulates agent-merchant communication for the full order flow") — orders
    # here are lost on restart, charge nothing, and must never be mistaken for a
    # real order now that partners are live. Use the production order/checkout flow.
    raise HTTPException(
        status_code=501,
        detail="This MCP order endpoint is a simulation surface and is disabled. "
               "Use the production order / checkout flow.",
    )
    try:
        # Convert request items to dict format
        items_dict = [
            {
                "sku": item.sku,
                "quantity": item.quantity,
                "unit_price": item.unit_price
            } for item in request.items
        ]
        
        order = create_order(
            agent_id=request.agent_id,
            merchant_id=request.merchant_id,
            items=items_dict,
            customer_email=request.customer_email,
            shipping_address=request.shipping_address
        )
        
        if not order:
            raise HTTPException(status_code=400, detail="Failed to create order - check stock availability")
        
        logger.info(f"Created order {order.order_id} for agent {request.agent_id} with merchant {request.merchant_id}")
        
        return OrderResponse(**order.to_dict())
        
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error creating order: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to create order")

@router.get("/orders/{order_id}", response_model=OrderResponse)
async def get_order_endpoint(order_id: str):
    """Get order by ID"""
    # FAIL CLOSED: this endpoint read from an in-memory SIMULATION fixture
    # (ai_router/merchant_store.py), not from any merchant. It served that
    # fabricated inventory/order data in production until 2026-08-11.
    raise HTTPException(
        status_code=501,
        detail="This endpoint is a simulation surface and is disabled. It never reflected real merchant inventory or orders; use the production catalog / order APIs.",
    )
    try:
        order = get_order(order_id)
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")
        
        return OrderResponse(**order.to_dict())
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error getting order: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to get order")

@router.get("/orders/agent/{agent_id}")
async def get_agent_orders(agent_id: str):
    """Get all orders for an agent"""
    # FAIL CLOSED: this endpoint read from an in-memory SIMULATION fixture
    # (ai_router/merchant_store.py), not from any merchant. It served that
    # fabricated inventory/order data in production until 2026-08-11.
    raise HTTPException(
        status_code=501,
        detail="This endpoint is a simulation surface and is disabled. It never reflected real merchant inventory or orders; use the production catalog / order APIs.",
    )
    try:
        orders = get_orders_by_agent(agent_id)
        return {
            "agent_id": agent_id,
            "total_orders": len(orders),
            "orders": [order.to_dict() for order in orders]
        }
    except Exception as e:
        logger.exception(f"Error getting agent orders: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to get agent orders")

@router.get("/orders/merchant/{merchant_id}")
async def get_merchant_orders(merchant_id: str):
    """Get all orders for a merchant"""
    # FAIL CLOSED: this endpoint read from an in-memory SIMULATION fixture
    # (ai_router/merchant_store.py), not from any merchant. It served that
    # fabricated inventory/order data in production until 2026-08-11.
    raise HTTPException(
        status_code=501,
        detail="This endpoint is a simulation surface and is disabled. It never reflected real merchant inventory or orders; use the production catalog / order APIs.",
    )
    try:
        orders = get_orders_by_merchant(merchant_id)
        return {
            "merchant_id": merchant_id,
            "total_orders": len(orders),
            "orders": [order.to_dict() for order in orders]
        }
    except Exception as e:
        logger.exception(f"Error getting merchant orders: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to get merchant orders")

@router.put("/orders/{order_id}/status")
async def update_order_status_endpoint(order_id: str, update: OrderStatusUpdate):
    """Update order status"""
    # FAIL CLOSED: in-memory simulation store — see create_order_endpoint.
    raise HTTPException(
        status_code=501,
        detail="This MCP order endpoint is a simulation surface and is disabled.",
    )
    try:
        # Validate status
        try:
            status = OrderStatus(update.status)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid status: {update.status}")
        
        success = update_order_status(
            order_id=order_id,
            status=status,
            payment_intent_id=update.payment_intent_id,
            psp_used=update.psp_used,
            payment_status=update.payment_status
        )
        
        if not success:
            raise HTTPException(status_code=404, detail="Order not found")
        
        logger.info(f"Updated order {order_id} status to {update.status}")
        
        # Return updated order
        order = get_order(order_id)
        return OrderResponse(**order.to_dict())
        
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error updating order status: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to update order status")

@router.delete("/orders/{order_id}")
async def cancel_order_endpoint(order_id: str):
    """Cancel an order"""
    # FAIL CLOSED: in-memory simulation store — see create_order_endpoint.
    raise HTTPException(
        status_code=501,
        detail="This MCP order endpoint is a simulation surface and is disabled.",
    )
    try:
        success = cancel_order(order_id)
        if not success:
            raise HTTPException(status_code=404, detail="Order not found or cannot be cancelled")
        
        logger.info(f"Cancelled order {order_id}")
        
        order = get_order(order_id)
        return OrderResponse(**order.to_dict())
        
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error cancelling order: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to cancel order")

@router.get("/inventory/summary")
async def get_inventory_summary_endpoint():
    """Get inventory summary across all merchants"""
    # FAIL CLOSED: this endpoint read from an in-memory SIMULATION fixture
    # (ai_router/merchant_store.py), not from any merchant. It served that
    # fabricated inventory/order data in production until 2026-08-11.
    raise HTTPException(
        status_code=501,
        detail="This endpoint is a simulation surface and is disabled. It never reflected real merchant inventory or orders; use the production catalog / order APIs.",
    )
    try:
        return get_inventory_summary()
    except Exception as e:
        logger.exception(f"Error getting inventory summary: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to get inventory summary")

@router.get("/orders/summary")
async def get_orders_summary_endpoint():
    """Get orders summary"""
    # FAIL CLOSED: this endpoint read from an in-memory SIMULATION fixture
    # (ai_router/merchant_store.py), not from any merchant. It served that
    # fabricated inventory/order data in production until 2026-08-11.
    raise HTTPException(
        status_code=501,
        detail="This endpoint is a simulation surface and is disabled. It never reflected real merchant inventory or orders; use the production catalog / order APIs.",
    )
    try:
        return get_orders_summary()
    except Exception as e:
        logger.exception(f"Error getting orders summary: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to get orders summary")

@router.get("/health")
async def health_check():
    """Disabled-surface marker.

    This used to answer "healthy" and count rows in the simulation fixture,
    which read as a live integration health signal. It is not one.
    """
    raise HTTPException(
        status_code=501,
        detail="This endpoint is a simulation surface and is disabled. It never "
               "reflected real merchant inventory or orders; use the production catalog / order APIs.",
    )
