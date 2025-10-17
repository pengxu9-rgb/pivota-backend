"""
订单数据模型
Pivota 核心业务对象
"""

from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from datetime import datetime
from decimal import Decimal


class OrderItem(BaseModel):
    """订单项（单个产品）"""
    product_id: str
    product_title: str
    variant_id: Optional[str] = None
    variant_title: Optional[str] = None
    quantity: int
    unit_price: Decimal
    subtotal: Decimal  # quantity * unit_price
    
    class Config:
        json_encoders = {
            Decimal: lambda v: str(v)
        }


class ShippingAddress(BaseModel):
    """收货地址"""
    name: str
    address_line1: str
    address_line2: Optional[str] = None
    city: str
    state: Optional[str] = None
    postal_code: str
    country: str
    phone: Optional[str] = None


class OrderStatus:
    """订单状态枚举"""
    # 初始状态
    PENDING = "pending"  # 待支付
    
    # 支付相关
    PAYMENT_PROCESSING = "payment_processing"  # 支付处理中
    PAYMENT_FAILED = "payment_failed"  # 支付失败
    PAID = "paid"  # 已支付
    
    # 履约相关
    PROCESSING = "processing"  # 商户处理中
    SHIPPED = "shipped"  # 已发货
    DELIVERED = "delivered"  # 已送达
    
    # 异常状态
    CANCELLED = "cancelled"  # 已取消
    REFUNDED = "refunded"  # 已退款


class CreateOrderRequest(BaseModel):
    """创建订单请求"""
    merchant_id: str
    customer_email: str
    items: List[OrderItem]
    shipping_address: ShippingAddress
    currency: str = "USD"
    agent_session_id: Optional[str] = None  # Agent 会话 ID（用于追踪）
    metadata: Optional[Dict[str, Any]] = None  # 额外元数据


class OrderResponse(BaseModel):
    """订单响应"""
    order_id: str
    merchant_id: str
    customer_email: str
    items: List[OrderItem]
    shipping_address: ShippingAddress
    
    # 金额
    subtotal: Decimal
    shipping_fee: Decimal
    tax: Decimal
    total: Decimal
    currency: str
    
    # 状态
    status: str
    payment_status: str
    fulfillment_status: Optional[str] = None
    
    # 支付相关
    payment_intent_id: Optional[str] = None  # Stripe Payment Intent ID
    client_secret: Optional[str] = None  # Stripe 前端支付用
    
    # 履约相关
    shopify_order_id: Optional[str] = None  # Shopify 订单 ID
    tracking_number: Optional[str] = None  # 物流单号
    
    # 时间戳
    created_at: datetime
    updated_at: datetime
    paid_at: Optional[datetime] = None
    shipped_at: Optional[datetime] = None
    
    # 元数据
    agent_session_id: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    
    class Config:
        json_encoders = {
            Decimal: lambda v: str(v),
            datetime: lambda v: v.isoformat() if v else None
        }


class PaymentConfirmRequest(BaseModel):
    """支付确认请求"""
    order_id: str
    payment_method_id: str  # Stripe Payment Method ID
    billing_address: Optional[ShippingAddress] = None


class OrderListResponse(BaseModel):
    """订单列表响应"""
    status: str = "success"
    total: int
    orders: List[OrderResponse]
    
    class Config:
        json_encoders = {
            Decimal: lambda v: str(v),
            datetime: lambda v: v.isoformat() if v else None
        }

