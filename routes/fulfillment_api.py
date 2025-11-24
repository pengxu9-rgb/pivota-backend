"""
Fulfillment Tracking API for Agents
Allows agents to monitor order delivery status
"""

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from typing import Optional, Dict, Any, List
from datetime import datetime

from db.orders import get_order
from db.merchant_onboarding import get_merchant_onboarding
from routes.agent_auth import AgentContext, get_agent_context, log_agent_request
from utils.logger import logger


router = APIRouter(prefix="/agent/v1/fulfillment", tags=["agent-fulfillment"])


@router.get("/track/{order_id}")
async def track_order_fulfillment(
    order_id: str,
    context: AgentContext = Depends(get_agent_context),
    background_tasks: BackgroundTasks = BackgroundTasks()
):
    """
    Get fulfillment and tracking information for an order
    
    Returns:
    - Fulfillment status
    - Tracking number (if available)
    - Carrier information
    - Estimated delivery (if available)
    - Shipment timeline
    """
    try:
        # Get order
        order = await get_order(order_id)
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")
        
        # Verify agent has access to this merchant
        if not context.can_access_merchant(order["merchant_id"]):
            raise HTTPException(status_code=403, detail="Not authorized for this order")
        
        # Log request
        background_tasks.add_task(
            log_agent_request,
            context=context,
            status_code=200,
            merchant_id=order["merchant_id"],
            order_id=order_id
        )
        
        # Build fulfillment response
        fulfillment_status = order.get("fulfillment_status", "pending")
        
        # Determine delivery status
        delivery_status = "not_shipped"
        if fulfillment_status == "shipped":
            delivery_status = "in_transit"
        elif fulfillment_status == "delivered":
            delivery_status = "delivered"
        elif fulfillment_status == "failed":
            delivery_status = "failed"
        
        tracking_info = {
            "order_id": order_id,
            "fulfillment_status": fulfillment_status,
            "delivery_status": delivery_status,
            "tracking_number": order.get("tracking_number"),
            "carrier": order.get("carrier"),
            "shipped_at": order.get("shipped_at").isoformat() if order.get("shipped_at") else None,
            "estimated_delivery": None,  # TODO: Calculate based on carrier and location
            "timeline": [
                {
                    "status": "ordered",
                    "timestamp": order["created_at"].isoformat(),
                    "completed": True
                },
                {
                    "status": "paid",
                    "timestamp": order.get("paid_at").isoformat() if order.get("paid_at") else None,
                    "completed": order.get("payment_status") == "paid"
                },
                {
                    "status": "shipped",
                    "timestamp": order.get("shipped_at").isoformat() if order.get("shipped_at") else None,
                    "completed": fulfillment_status in ["shipped", "delivered"]
                },
                {
                    "status": "delivered",
                    "timestamp": None,  # TODO: Track delivery confirmation
                    "completed": fulfillment_status == "delivered"
                }
            ]
        }
        
        # Add tracking URL if available
        if tracking_info["tracking_number"] and tracking_info["carrier"]:
            carrier = tracking_info["carrier"].lower()
            tracking_number = tracking_info["tracking_number"]
            
            # Generate carrier tracking URLs
            tracking_urls = {
                "ups": f"https://www.ups.com/track?tracknum={tracking_number}",
                "fedex": f"https://www.fedex.com/fedextrack/?trknbr={tracking_number}",
                "usps": f"https://tools.usps.com/go/TrackConfirmAction?tLabels={tracking_number}",
                "dhl": f"https://www.dhl.com/en/express/tracking.html?AWB={tracking_number}"
            }
            
            tracking_info["tracking_url"] = tracking_urls.get(carrier)
        
        return {
            "status": "success",
            "tracking": tracking_info
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Fulfillment tracking error: {e}")
        await log_agent_request(
            context=context,
            status_code=500,
            order_id=order_id,
            error_message=str(e)
        )
        raise HTTPException(status_code=500, detail="Failed to get tracking information")


@router.get("/orders/in-transit")
async def get_in_transit_orders(
    merchant_id: Optional[str] = None,
    context: AgentContext = Depends(get_agent_context)
):
    """
    Get all orders that are currently in transit
    
    Useful for agents to track active deliveries
    """
    try:
        # Build query for shipped orders
        from db.database import database
        from db.orders import orders
        
        query = orders.select().where(
            (orders.c.fulfillment_status == "shipped") &
            orders.c.is_deleted.is_(False)
        )
        
        if merchant_id:
            if not context.can_access_merchant(merchant_id):
                raise HTTPException(status_code=403, detail="Not authorized for this merchant")
            query = query.where(orders.c.merchant_id == merchant_id)
        
        # Filter by agent's allowed merchants if restricted
        if context.allowed_merchants is not None:
            query = query.where(orders.c.merchant_id.in_(context.allowed_merchants))
        
        query = query.order_by(orders.c.shipped_at.desc()).limit(50)
        
        results = await database.fetch_all(query)
        
        in_transit_orders = []
        for order in results:
            in_transit_orders.append({
                "order_id": order["order_id"],
                "customer_email": order["customer_email"],
                "tracking_number": order.get("tracking_number"),
                "carrier": order.get("carrier"),
                "shipped_at": order.get("shipped_at").isoformat() if order.get("shipped_at") else None,
                "total": str(order["total"]),
                "currency": order["currency"]
            })
        
        return {
            "status": "success",
            "total": len(in_transit_orders),
            "orders": in_transit_orders
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting in-transit orders: {e}")
        raise HTTPException(status_code=500, detail="Failed to get in-transit orders")

