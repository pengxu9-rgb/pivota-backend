"""
订单数据模型
Pivota 核心业务对象
"""

from pydantic import BaseModel, Field, computed_field, model_validator
from typing import Optional, List, Dict, Any
from datetime import datetime
from decimal import Decimal


class OrderItem(BaseModel):
    """订单项（单个产品）"""
    product_id: str
    # Quote-first callers may omit these; pricing is locked by quote_id and stored separately.
    product_title: Optional[str] = None
    variant_id: Optional[str] = None
    variant_title: Optional[str] = None
    sku: Optional[str] = None  # SKU (用于库存追踪)
    quantity: int
    unit_price: Optional[Decimal] = None
    subtotal: Optional[Decimal] = None  # quantity * unit_price
    
    class Config:
        json_encoders = {
            Decimal: lambda v: str(v)
        }

    @model_validator(mode="after")
    def _best_effort_subtotal(self) -> "OrderItem":
        if self.subtotal is None and self.unit_price is not None:
            try:
                self.subtotal = self.unit_price * Decimal(self.quantity)
            except Exception:
                pass
        return self


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
    customer_name: Optional[str] = None  # Customer name (optional)
    quote_id: Optional[str] = None  # Quote-first: lock pricing snapshot
    brief_id: Optional[str] = None  # Decision/Brief join key (optional, additive)
    brief_schema_version: Optional[str] = None
    discount_codes: Optional[List[str]] = None  # Optional; used for quote fingerprint validation
    selected_delivery_option: Optional[Dict[str, Any]] = None  # Optional; used for quote fingerprint validation
    items: List[OrderItem]
    shipping_address: ShippingAddress
    currency: str = "USD"
    agent_session_id: Optional[str] = None  # Agent 会话 ID（用于追踪）
    metadata: Optional[Dict[str, Any]] = None  # 额外元数据
    preferred_psp: Optional[str] = None  # 指定首选 PSP (stripe/adyen/checkout)
    selected_payment_offer_id: Optional[str] = None  # Display-only v1; never changes PSP amount
    payment_method_evidence: Optional[Dict[str, Any]] = None  # PSP/client evidence for future offer verification
    idempotency_key: Optional[str] = None  # Best-effort retry safety (agent/gateway)

    @model_validator(mode="after")
    def _enforce_legacy_item_fields_when_not_quote_first(self) -> "CreateOrderRequest":
        # Legacy (non-quote) order create requires item title + unit_price for pricing calculation.
        # Quote-first order create can omit these fields because pricing is computed from quote snapshot.
        if not self.quote_id:
            for item in self.items or []:
                if not item.product_title:
                    raise ValueError("items[].product_title is required when quote_id is not provided")
                if item.unit_price is None:
                    raise ValueError("items[].unit_price is required when quote_id is not provided")
                if item.subtotal is None:
                    try:
                        item.subtotal = item.unit_price * Decimal(item.quantity)
                    except Exception:
                        raise ValueError("items[].subtotal is required when quote_id is not provided")
        return self


class RecordPaymentOfferEvidenceRequest(BaseModel):
    """Display-only payment-offer evidence captured after checkout UI state changes."""
    order_id: Optional[str] = None
    quote_id: Optional[str] = None
    merchant_id: Optional[str] = None
    selected_payment_offer_id: Optional[str] = None
    payment_method_evidence: Dict[str, Any] = Field(default_factory=dict)
    payment_offer_evidence: Optional[Dict[str, Any]] = None
    surface: str = "checkout"
    event_type: Optional[str] = None
    idempotency_key: Optional[str] = None


class PaymentAction(BaseModel):
    """统一支付动作抽象，供前端根据 type 分发"""
    type: Optional[str] = None  # stripe_client_secret | adyen_session | redirect_url | hosted_page
    client_secret: Optional[str] = None  # 当 type 需要 client_secret 时使用
    url: Optional[str] = None  # 当 type 需要重定向/托管页面时使用
    public_key: Optional[str] = None  # Stripe / Checkout.com public key when client-owned confirmation needs it
    raw: Optional[Dict[str, Any]] = None  # 适配器原始 payload（可选，用于调试/扩展）


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
    
    @computed_field
    @property
    def total_amount(self) -> Decimal:
        """标准字段名（推荐使用）- 与 total 保持一致"""
        return self.total
    
    # 状态
    status: str
    payment_status: str
    fulfillment_status: Optional[str] = None
    
    # 支付相关
    payment_intent_id: Optional[str] = None  # Stripe Payment Intent ID
    client_secret: Optional[str] = None  # Stripe 前端支付用
    psp: Optional[str] = None  # 实际使用的 PSP 提供方（stripe/adyen/checkout/paypal）
    payment_action: Optional[PaymentAction] = None  # 统一支付动作抽象
    
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
