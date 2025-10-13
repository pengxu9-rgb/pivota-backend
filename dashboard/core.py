"""
Dashboard Core Module
"""
import logging
from typing import Dict, Any, Optional, List
from enum import Enum
from datetime import datetime

logger = logging.getLogger("dashboard_core")

class OrderStatus(str, Enum):
    """Order status enumeration"""
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

class PSPType(str, Enum):
    """Payment Service Provider type enumeration"""
    STRIPE = "stripe"
    ADYEN = "adyen"
    PAYPAL = "paypal"

class UserRole(str, Enum):
    """User role enumeration"""
    ADMIN = "admin"
    OPERATOR = "operator"
    VIEWER = "viewer"
    AGENT = "agent"
    MERCHANT = "merchant"

class User:
    """User model"""
    def __init__(self, id: str, name: str, role: UserRole, entity_id: Optional[str] = None):
        self.id = id
        self.name = name
        self.role = role
        self.entity_id = entity_id
        self.created_at = datetime.utcnow()

class Order:
    """Order model"""
    def __init__(self, id: str, user_id: str, amount: float, currency: str = "USD"):
        self.id = id
        self.user_id = user_id
        self.amount = amount
        self.currency = currency
        self.status = OrderStatus.PENDING
        self.created_at = datetime.utcnow()
        self.updated_at = datetime.utcnow()

class Payment:
    """Payment model"""
    def __init__(self, id: str, order_id: str, amount: float, psp: PSPType):
        self.id = id
        self.order_id = order_id
        self.amount = amount
        self.psp = psp
        self.status = OrderStatus.PENDING
        self.created_at = datetime.utcnow()
        self.updated_at = datetime.utcnow()

class DashboardCore:
    """Dashboard core functionality"""
    
    def __init__(self):
        self.users: Dict[str, User] = {}
        self.orders: Dict[str, Order] = {}
        self.payments: Dict[str, Payment] = {}
        self.metrics: Dict[str, Any] = {}
        self.initialized = False
    
    async def initialize(self):
        """Initialize the dashboard core"""
        if not self.initialized:
            logger.info("Dashboard core initialized")
            self.initialized = True
    
    def create_user(self, user_id: str, name: str, role: UserRole, entity_id: Optional[str] = None) -> User:
        """Create a new user"""
        user = User(user_id, name, role, entity_id)
        self.users[user_id] = user
        logger.info(f"Created user: {user_id} with role: {role}")
        return user
    
    def get_user(self, user_id: str) -> Optional[User]:
        """Get user by ID"""
        return self.users.get(user_id)
    
    def create_order(self, order_id: str, user_id: str, amount: float, currency: str = "USD") -> Order:
        """Create a new order"""
        order = Order(order_id, user_id, amount, currency)
        self.orders[order_id] = order
        logger.info(f"Created order: {order_id} for user: {user_id}")
        return order
    
    def get_order(self, order_id: str) -> Optional[Order]:
        """Get order by ID"""
        return self.orders.get(order_id)
    
    def update_order_status(self, order_id: str, status: OrderStatus):
        """Update order status"""
        if order_id in self.orders:
            self.orders[order_id].status = status
            self.orders[order_id].updated_at = datetime.utcnow()
            logger.info(f"Updated order {order_id} status to: {status}")
    
    def create_payment(self, payment_id: str, order_id: str, amount: float, psp: PSPType) -> Payment:
        """Create a new payment"""
        payment = Payment(payment_id, order_id, amount, psp)
        self.payments[payment_id] = payment
        logger.info(f"Created payment: {payment_id} for order: {order_id}")
        return payment
    
    def get_payment(self, payment_id: str) -> Optional[Payment]:
        """Get payment by ID"""
        return self.payments.get(payment_id)
    
    def update_payment_status(self, payment_id: str, status: OrderStatus):
        """Update payment status"""
        if payment_id in self.payments:
            self.payments[payment_id].status = status
            self.payments[payment_id].updated_at = datetime.utcnow()
            logger.info(f"Updated payment {payment_id} status to: {status}")
    
    def get_metrics(self) -> Dict[str, Any]:
        """Get dashboard metrics"""
        total_orders = len(self.orders)
        total_payments = len(self.payments)
        total_users = len(self.users)
        
        # Calculate success rate
        successful_orders = sum(1 for order in self.orders.values() if order.status == OrderStatus.COMPLETED)
        success_rate = (successful_orders / total_orders * 100) if total_orders > 0 else 0
        
        # Calculate PSP distribution
        psp_distribution = {}
        for payment in self.payments.values():
            psp_distribution[payment.psp] = psp_distribution.get(payment.psp, 0) + 1
        
        return {
            "total_orders": total_orders,
            "total_payments": total_payments,
            "total_users": total_users,
            "success_rate": round(success_rate, 2),
            "psp_distribution": psp_distribution,
            "last_updated": datetime.utcnow().isoformat()
        }
    
    def get_user_orders(self, user_id: str) -> List[Order]:
        """Get orders for a specific user"""
        return [order for order in self.orders.values() if order.user_id == user_id]
    
    def get_user_payments(self, user_id: str) -> List[Payment]:
        """Get payments for a specific user's orders"""
        user_orders = self.get_user_orders(user_id)
        user_order_ids = [order.id for order in user_orders]
        return [payment for payment in self.payments.values() if payment.order_id in user_order_ids]

# Global dashboard core instance
dashboard_core = DashboardCore()
