"""
Merchant Inventory & Orders Store
Simulates merchant inventory and order management for the Agent → Merchant → Pivota → PSP loop
"""

from datetime import datetime
from typing import Dict, List, Optional
from enum import Enum
import uuid

class OrderStatus(Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    PROCESSING = "processing"
    PAID = "paid"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    FAILED = "failed"

class OrderItem:
    def __init__(self, sku: str, quantity: int, unit_price: float):
        self.sku = sku
        self.quantity = quantity
        self.unit_price = unit_price
        self.total_price = quantity * unit_price

class Order:
    def __init__(self, order_id: str, agent_id: str, merchant_id: str, items: List[OrderItem], 
                 customer_email: Optional[str] = None, shipping_address: Optional[Dict] = None):
        self.order_id = order_id
        self.agent_id = agent_id
        self.merchant_id = merchant_id
        self.items = items
        self.customer_email = customer_email
        self.shipping_address = shipping_address
        self.status = OrderStatus.PENDING
        self.created_at = datetime.utcnow()
        self.updated_at = datetime.utcnow()
        self.total_amount = sum(item.total_price for item in items)
        self.currency = "EUR"  # Default currency
        self.payment_intent_id = None
        self.psp_used = None
        self.payment_status = None

    def to_dict(self):
        return {
            "order_id": self.order_id,
            "agent_id": self.agent_id,
            "merchant_id": self.merchant_id,
            "items": [
                {
                    "sku": item.sku,
                    "quantity": item.quantity,
                    "unit_price": item.unit_price,
                    "total_price": item.total_price
                } for item in self.items
            ],
            "customer_email": self.customer_email,
            "shipping_address": self.shipping_address,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "total_amount": self.total_amount,
            "currency": self.currency,
            "payment_intent_id": self.payment_intent_id,
            "psp_used": self.psp_used,
            "payment_status": self.payment_status
        }

# Merchant Inventory Store
merchant_inventory = {
    "MERCH_001": {
        "name": "Cool Shoes EU",
        "location": "Amsterdam, Netherlands",
        "currency": "EUR",
        "stock": {
            "SHOE_RED_42": {"price": 120.0, "quantity": 5, "name": "Red Sneakers Size 42"},
            "SHOE_BLUE_40": {"price": 110.0, "quantity": 10, "name": "Blue Sneakers Size 40"},
            "SHOE_BLACK_41": {"price": 125.0, "quantity": 8, "name": "Black Sneakers Size 41"},
            "SHOE_WHITE_39": {"price": 115.0, "quantity": 12, "name": "White Sneakers Size 39"},
        }
    },
    "MERCH_002": {
        "name": "Gadget Hub",
        "location": "Berlin, Germany", 
        "currency": "EUR",
        "stock": {
            "SMARTWATCH_X": {"price": 250.0, "quantity": 3, "name": "SmartWatch X Pro"},
            "EARBUDS_Y": {"price": 60.0, "quantity": 20, "name": "Wireless Earbuds Y"},
            "PHONE_CASE_Z": {"price": 25.0, "quantity": 50, "name": "Phone Case Z"},
            "CHARGER_A": {"price": 35.0, "quantity": 30, "name": "Fast Charger A"},
        }
    },
    "MERCH_003": {
        "name": "Fashion Forward",
        "location": "Paris, France",
        "currency": "EUR", 
        "stock": {
            "JACKET_LEATHER": {"price": 180.0, "quantity": 4, "name": "Leather Jacket"},
            "JEANS_DENIM": {"price": 80.0, "quantity": 15, "name": "Denim Jeans"},
            "SHIRT_COTTON": {"price": 45.0, "quantity": 25, "name": "Cotton Shirt"},
        }
    }
}

# Orders Store
orders_store: List[Order] = []

def get_merchant_inventory(merchant_id: str) -> Optional[Dict]:
    """Get merchant inventory by ID"""
    return merchant_inventory.get(merchant_id)

def get_all_merchants() -> Dict:
    """Get all merchants with their inventory"""
    return merchant_inventory

def check_stock_availability(merchant_id: str, sku: str, requested_quantity: int) -> bool:
    """Check if requested quantity is available in stock"""
    merchant = merchant_inventory.get(merchant_id)
    if not merchant:
        return False
    
    item = merchant["stock"].get(sku)
    if not item:
        return False
    
    return item["quantity"] >= requested_quantity

def reserve_stock(merchant_id: str, sku: str, quantity: int) -> bool:
    """Reserve stock for an order"""
    merchant = merchant_inventory.get(merchant_id)
    if not merchant:
        return False
    
    item = merchant["stock"].get(sku)
    if not item or item["quantity"] < quantity:
        return False
    
    item["quantity"] -= quantity
    return True

def release_stock(merchant_id: str, sku: str, quantity: int):
    """Release reserved stock (for cancelled orders)"""
    merchant = merchant_inventory.get(merchant_id)
    if merchant and sku in merchant["stock"]:
        merchant["stock"][sku]["quantity"] += quantity

def create_order(agent_id: str, merchant_id: str, items: List[Dict], 
                customer_email: Optional[str] = None, 
                shipping_address: Optional[Dict] = None) -> Optional[Order]:
    """Create a new order"""
    # Validate merchant exists
    if merchant_id not in merchant_inventory:
        return None
    
    # Validate stock availability
    for item in items:
        if not check_stock_availability(merchant_id, item["sku"], item["quantity"]):
            return None
    
    # Create order items
    order_items = []
    for item in items:
        order_items.append(OrderItem(
            sku=item["sku"],
            quantity=item["quantity"],
            unit_price=item["unit_price"]
        ))
    
    # Generate order ID
    order_id = f"ORD_{uuid.uuid4().hex[:8].upper()}"
    
    # Create order
    order = Order(
        order_id=order_id,
        agent_id=agent_id,
        merchant_id=merchant_id,
        items=order_items,
        customer_email=customer_email,
        shipping_address=shipping_address
    )
    
    # Reserve stock
    for item in items:
        reserve_stock(merchant_id, item["sku"], item["quantity"])
    
    # Store order
    orders_store.append(order)
    
    return order

def get_order(order_id: str) -> Optional[Order]:
    """Get order by ID"""
    for order in orders_store:
        if order.order_id == order_id:
            return order
    return None

def update_order_status(order_id: str, status: OrderStatus, 
                       payment_intent_id: Optional[str] = None,
                       psp_used: Optional[str] = None,
                       payment_status: Optional[str] = None) -> bool:
    """Update order status"""
    order = get_order(order_id)
    if not order:
        return False
    
    order.status = status
    order.updated_at = datetime.utcnow()
    
    if payment_intent_id:
        order.payment_intent_id = payment_intent_id
    if psp_used:
        order.psp_used = psp_used
    if payment_status:
        order.payment_status = payment_status
    
    return True

def get_orders_by_agent(agent_id: str) -> List[Order]:
    """Get all orders for an agent"""
    return [order for order in orders_store if order.agent_id == agent_id]

def get_orders_by_merchant(merchant_id: str) -> List[Order]:
    """Get all orders for a merchant"""
    return [order for order in orders_store if order.merchant_id == merchant_id]

def get_orders_by_status(status: OrderStatus) -> List[Order]:
    """Get all orders with specific status"""
    return [order for order in orders_store if order.status == status]

def cancel_order(order_id: str) -> bool:
    """Cancel an order and release stock"""
    order = get_order(order_id)
    if not order or order.status in [OrderStatus.SHIPPED, OrderStatus.DELIVERED]:
        return False
    
    # Release stock
    for item in order.items:
        release_stock(order.merchant_id, item.sku, item.quantity)
    
    # Update status
    order.status = OrderStatus.CANCELLED
    order.updated_at = datetime.utcnow()
    
    return True

def get_inventory_summary() -> Dict:
    """Get summary of all merchant inventories"""
    summary = {}
    for merchant_id, merchant in merchant_inventory.items():
        total_items = len(merchant["stock"])
        total_quantity = sum(item["quantity"] for item in merchant["stock"].values())
        total_value = sum(item["price"] * item["quantity"] for item in merchant["stock"].values())
        
        summary[merchant_id] = {
            "name": merchant["name"],
            "location": merchant["location"],
            "currency": merchant["currency"],
            "total_items": total_items,
            "total_quantity": total_quantity,
            "total_value": total_value
        }
    
    return summary

def get_orders_summary() -> Dict:
    """Get summary of all orders"""
    total_orders = len(orders_store)
    total_value = sum(order.total_amount for order in orders_store)
    
    status_counts = {}
    for status in OrderStatus:
        status_counts[status.value] = len(get_orders_by_status(status))
    
    return {
        "total_orders": total_orders,
        "total_value": total_value,
        "status_breakdown": status_counts,
        "recent_orders": [order.to_dict() for order in orders_store[-5:]]  # Last 5 orders
    }


