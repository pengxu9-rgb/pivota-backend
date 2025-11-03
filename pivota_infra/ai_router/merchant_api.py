"""
MCP API Simulation
Simulates merchant API interactions for the Agent → Merchant → Pivota → PSP loop
"""

from ai_router.merchant_store import merchant_inventory, orders_store, create_order as store_create_order, get_order, update_order_status, OrderStatus
from typing import Dict, Any, Optional
import uuid
from datetime import datetime

def check_inventory(merchant_id: str, sku: str, qty: int) -> Dict[str, Any]:
    """Check if inventory is available for a specific SKU"""
    merchant = merchant_inventory.get(merchant_id)
    if not merchant:
        return {"available": False, "reason": "merchant not found"}

    item = merchant["stock"].get(sku)
    if not item:
        return {"available": False, "reason": "SKU not found"}

    if item["quantity"] >= qty:
        return {
            "available": True, 
            "price": item["price"],
            "name": item["name"],
            "currency": merchant["currency"]
        }
    else:
        return {"available": False, "reason": "not enough stock"}

def create_order(merchant_id: str, sku: str, qty: int, buyer_id: str) -> Dict[str, Any]:
    """Create a simple order for a single SKU"""
    result = check_inventory(merchant_id, sku, qty)
    if not result["available"]:
        return {"success": False, "reason": result["reason"]}

    # Reduce stock
    merchant_inventory[merchant_id]["stock"][sku]["quantity"] -= qty

    order_id = f"ORD_{len(orders_store)+1:04d}"
    order = {
        "order_id": order_id,
        "merchant_id": merchant_id,
        "sku": sku,
        "quantity": qty,
        "buyer_id": buyer_id,
        "price": result["price"],
        "total_amount": result["price"] * qty,
        "currency": result["currency"],
        "status": "pending",
        "created_at": datetime.utcnow().isoformat(),
        "updated_at": datetime.utcnow().isoformat()
    }
    orders_store.append(order)
    return {
        "success": True, 
        "order_id": order_id, 
        "price": result["price"],
        "total_amount": result["price"] * qty,
        "currency": result["currency"]
    }

def get_merchant_catalog(merchant_id: str) -> Dict[str, Any]:
    """Get merchant's product catalog"""
    merchant = merchant_inventory.get(merchant_id)
    if not merchant:
        return {"success": False, "reason": "merchant not found"}
    
    catalog = []
    for sku, item in merchant["stock"].items():
        catalog.append({
            "sku": sku,
            "name": item["name"],
            "price": item["price"],
            "quantity": item["quantity"],
            "available": item["quantity"] > 0
        })
    
    return {
        "success": True,
        "merchant_id": merchant_id,
        "merchant_name": merchant["name"],
        "location": merchant["location"],
        "currency": merchant["currency"],
        "catalog": catalog,
        "total_items": len(catalog)
    }

def search_products(merchant_id: str, query: str = None, min_price: float = None, max_price: float = None) -> Dict[str, Any]:
    """Search products in merchant catalog"""
    catalog_result = get_merchant_catalog(merchant_id)
    if not catalog_result["success"]:
        return catalog_result
    
    catalog = catalog_result["catalog"]
    filtered_catalog = catalog
    
    # Apply filters
    if query:
        filtered_catalog = [item for item in filtered_catalog if query.lower() in item["name"].lower()]
    
    if min_price is not None:
        filtered_catalog = [item for item in filtered_catalog if item["price"] >= min_price]
    
    if max_price is not None:
        filtered_catalog = [item for item in filtered_catalog if item["price"] <= max_price]
    
    return {
        "success": True,
        "merchant_id": merchant_id,
        "query": query,
        "filters": {"min_price": min_price, "max_price": max_price},
        "results": filtered_catalog,
        "total_results": len(filtered_catalog)
    }

def get_order_status(order_id: str) -> Dict[str, Any]:
    """Get order status"""
    order = get_order(order_id)
    if not order:
        return {"success": False, "reason": "order not found"}
    
    return {
        "success": True,
        "order_id": order_id,
        "status": order.status.value,
        "merchant_id": order.merchant_id,
        "agent_id": order.agent_id,
        "total_amount": order.total_amount,
        "currency": order.currency,
        "created_at": order.created_at.isoformat(),
        "updated_at": order.updated_at.isoformat(),
        "payment_status": order.payment_status,
        "psp_used": order.psp_used
    }

def update_order_status_api(order_id: str, status: str, payment_info: Dict[str, Any] = None) -> Dict[str, Any]:
    """Update order status via API"""
    try:
        order_status = OrderStatus(status)
    except ValueError:
        return {"success": False, "reason": f"Invalid status: {status}"}
    
    success = update_order_status(
        order_id=order_id,
        status=order_status,
        payment_intent_id=payment_info.get("payment_intent_id") if payment_info else None,
        psp_used=payment_info.get("psp_used") if payment_info else None,
        payment_status=payment_info.get("payment_status") if payment_info else None
    )
    
    if not success:
        return {"success": False, "reason": "order not found"}
    
    return {"success": True, "order_id": order_id, "status": status}

def get_merchant_orders(merchant_id: str, status: str = None) -> Dict[str, Any]:
    """Get all orders for a merchant"""
    from ai_router.merchant_store import get_orders_by_merchant, get_orders_by_status
    
    if status:
        try:
            order_status = OrderStatus(status)
            orders = get_orders_by_status(order_status)
            # Filter by merchant
            orders = [order for order in orders if order.merchant_id == merchant_id]
        except ValueError:
            return {"success": False, "reason": f"Invalid status: {status}"}
    else:
        orders = get_orders_by_merchant(merchant_id)
    
    return {
        "success": True,
        "merchant_id": merchant_id,
        "status_filter": status,
        "orders": [order.to_dict() for order in orders],
        "total_orders": len(orders)
    }

def get_merchant_analytics(merchant_id: str) -> Dict[str, Any]:
    """Get merchant analytics and performance metrics"""
    from ai_router.merchant_store import get_orders_by_merchant
    
    orders = get_orders_by_merchant(merchant_id)
    merchant = merchant_inventory.get(merchant_id)
    
    if not merchant:
        return {"success": False, "reason": "merchant not found"}
    
    # Calculate analytics
    total_orders = len(orders)
    total_revenue = sum(order.total_amount for order in orders)
    
    status_breakdown = {}
    for status in OrderStatus:
        status_breakdown[status.value] = len([o for o in orders if o.status == status])
    
    # Inventory analytics
    total_inventory_value = sum(
        item["price"] * item["quantity"] 
        for item in merchant["stock"].values()
    )
    
    low_stock_items = [
        {"sku": sku, "name": item["name"], "quantity": item["quantity"]}
        for sku, item in merchant["stock"].items()
        if item["quantity"] < 5  # Low stock threshold
    ]
    
    return {
        "success": True,
        "merchant_id": merchant_id,
        "merchant_name": merchant["name"],
        "analytics": {
            "total_orders": total_orders,
            "total_revenue": total_revenue,
            "average_order_value": total_revenue / total_orders if total_orders > 0 else 0,
            "status_breakdown": status_breakdown,
            "inventory_value": total_inventory_value,
            "low_stock_items": low_stock_items,
            "total_products": len(merchant["stock"])
        }
    }

def simulate_merchant_webhook(merchant_id: str, order_id: str, event_type: str, data: Dict[str, Any] = None) -> Dict[str, Any]:
    """Simulate merchant webhook notifications"""
    order = get_order(order_id)
    if not order:
        return {"success": False, "reason": "order not found"}
    
    if order.merchant_id != merchant_id:
        return {"success": False, "reason": "order does not belong to merchant"}
    
    # Simulate different webhook events
    if event_type == "order.created":
        return {
            "success": True,
            "webhook_type": "order.created",
            "order_id": order_id,
            "merchant_id": merchant_id,
            "message": "Order created successfully"
        }
    elif event_type == "order.updated":
        return {
            "success": True,
            "webhook_type": "order.updated",
            "order_id": order_id,
            "merchant_id": merchant_id,
            "message": "Order updated successfully"
        }
    elif event_type == "payment.completed":
        # Update order status to paid
        update_order_status(order_id, OrderStatus.PAID, 
                          payment_intent_id=data.get("payment_intent_id"),
                          psp_used=data.get("psp_used"),
                          payment_status="succeeded")
        return {
            "success": True,
            "webhook_type": "payment.completed",
            "order_id": order_id,
            "merchant_id": merchant_id,
            "message": "Payment completed successfully"
        }
    elif event_type == "payment.failed":
        # Update order status to failed
        update_order_status(order_id, OrderStatus.FAILED,
                          payment_intent_id=data.get("payment_intent_id"),
                          psp_used=data.get("psp_used"),
                          payment_status="failed")
        return {
            "success": True,
            "webhook_type": "payment.failed",
            "order_id": order_id,
            "merchant_id": merchant_id,
            "message": "Payment failed"
        }
    else:
        return {"success": False, "reason": f"Unknown event type: {event_type}"}

def get_merchant_health(merchant_id: str) -> Dict[str, Any]:
    """Get merchant system health status"""
    merchant = merchant_inventory.get(merchant_id)
    if not merchant:
        return {"success": False, "reason": "merchant not found"}
    
    # Check inventory health
    total_items = len(merchant["stock"])
    out_of_stock = len([item for item in merchant["stock"].values() if item["quantity"] == 0])
    low_stock = len([item for item in merchant["stock"].values() if 0 < item["quantity"] < 5])
    
    health_score = 100
    if out_of_stock > 0:
        health_score -= (out_of_stock / total_items) * 50
    if low_stock > 0:
        health_score -= (low_stock / total_items) * 20
    
    return {
        "success": True,
        "merchant_id": merchant_id,
        "health_score": max(0, health_score),
        "status": "healthy" if health_score > 80 else "warning" if health_score > 50 else "critical",
        "inventory": {
            "total_items": total_items,
            "out_of_stock": out_of_stock,
            "low_stock": low_stock,
            "healthy_items": total_items - out_of_stock - low_stock
        }
    }

