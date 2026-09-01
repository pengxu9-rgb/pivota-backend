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
    # PAID is referenced by orchestrator/payment_orchestrator.py and
    # routes/demo_data_routes.py and was simply absent here, so both raised
    # AttributeError("PAID") before reaching anything else. It is a distinct
    # state from COMPLETED: money captured vs order fulfilled.
    PAID = "paid"
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
    """Order model.

    Two call shapes exist and both are supported deliberately:

      Order(order_id, user_id, amount, currency)      # positional, DashboardCore
      Order(id=..., merchant_id=..., agent_id=...,    # keyword, the commerce path
            customer_email=..., total_amount=..., items=[...], ...)

    Only the first was implemented. The second is what
    orchestrator/payment_orchestrator.py and routes/demo_data_routes.py have
    always passed, and what routes/dashboard_api.py and routes/payment_routes.py
    have always READ back (`order.total_amount`, `order.merchant_id`,
    `order.agent_id`, `order.customer_email`) — so construction raised TypeError
    and every reader would have raised AttributeError. The keyword fields below
    are what those four modules require; none is new invention.

    `amount` and `total_amount` are the same number under two names, because
    both are already read in the codebase. Setting either sets both.
    """

    def __init__(
        self,
        id: str,
        user_id: Optional[str] = None,
        amount: Optional[float] = None,
        currency: str = "USD",
        *,
        merchant_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        customer_email: Optional[str] = None,
        total_amount: Optional[float] = None,
        status: OrderStatus = OrderStatus.PENDING,
        items: Optional[List[Dict[str, Any]]] = None,
        payment_method: Optional[str] = None,
        psp_used: Optional[str] = None,
        created_at: Optional[datetime] = None,
        updated_at: Optional[datetime] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        self.id = id
        self.user_id = user_id
        resolved_amount = amount if amount is not None else total_amount
        self.amount = resolved_amount if resolved_amount is not None else 0.0
        self.total_amount = self.amount
        self.currency = currency
        self.merchant_id = merchant_id
        self.agent_id = agent_id
        self.customer_email = customer_email
        self.status = status
        self.items = items if items is not None else []
        self.payment_method = payment_method
        self.psp_used = psp_used
        self.created_at = created_at or datetime.utcnow()
        self.updated_at = updated_at or datetime.utcnow()
        self.metadata = metadata if metadata is not None else {}

class Payment:
    """Payment model.

    Same story as Order: the positional shape below is DashboardCore's, the
    keyword fields are what payment_orchestrator.py and demo_data_routes.py pass
    and what dashboard_api.py reads back (`payment.currency`, `.transaction_id`,
    `.fees`).

    `status` is intentionally untyped: DashboardCore sets an OrderStatus, while
    the payment path sets the PSP vocabulary ("succeeded"/"failed") that
    realtime/metrics_store.py counts on. Narrowing it would break one of them.
    """

    def __init__(
        self,
        id: str,
        order_id: str,
        amount: float,
        psp: PSPType,
        *,
        currency: str = "USD",
        status: Any = OrderStatus.PENDING,
        transaction_id: Optional[str] = None,
        fees: float = 0.0,
        created_at: Optional[datetime] = None,
        updated_at: Optional[datetime] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        self.id = id
        self.order_id = order_id
        self.amount = amount
        self.psp = psp
        self.currency = currency
        self.status = status
        self.transaction_id = transaction_id
        self.fees = fees
        self.created_at = created_at or datetime.utcnow()
        self.updated_at = updated_at or datetime.utcnow()
        self.metadata = metadata if metadata is not None else {}

class PSPConfig:
    """PSP Configuration model"""
    def __init__(self, psp_type: PSPType, api_key: str, webhook_secret: str, 
                 merchant_account: Optional[str] = None, enabled: bool = True):
        self.psp_type = psp_type
        self.api_key = api_key
        self.webhook_secret = webhook_secret
        self.merchant_account = merchant_account
        self.enabled = enabled
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
