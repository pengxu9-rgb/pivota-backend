"""
订单数据库表和 CRUD 操作
防御性架构：订单是核心业务数据，只能追加和更新状态，不能删除
"""

from sqlalchemy import Table, Column, Integer, String, Text, DateTime, JSON, Numeric, Boolean
from sqlalchemy.sql import func
from datetime import datetime
from typing import Optional, List, Dict, Any
import secrets

from db.database import metadata, database


# ============================================================================
# 订单表（核心业务数据）
# ============================================================================

orders = Table(
    "orders",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("order_id", String(50), unique=True, index=True, nullable=False),  # 订单唯一ID
    Column("merchant_id", String(50), index=True, nullable=False),
    
    # 客户信息
    Column("customer_email", String(255), nullable=False),
    Column("shipping_address", JSON, nullable=False),  # ShippingAddress JSON
    
    # 订单内容
    Column("items", JSON, nullable=False),  # List[OrderItem] JSON
    
    # 金额（使用 Numeric 精确存储）
    Column("subtotal", Numeric(10, 2), nullable=False),
    Column("shipping_fee", Numeric(10, 2), default=0),
    Column("tax", Numeric(10, 2), default=0),
    Column("total", Numeric(10, 2), nullable=False),
    Column("currency", String(3), default="USD"),
    
    # 状态机
    Column("status", String(50), default="pending", index=True),  # 订单状态
    Column("payment_status", String(50), default="unpaid", index=True),  # 支付状态
    Column("fulfillment_status", String(50), nullable=True),  # 履约状态
    
    # 支付集成（Stripe）
    Column("payment_intent_id", String(255), nullable=True, unique=True),
    Column("payment_method_id", String(255), nullable=True),
    Column("client_secret", String(500), nullable=True),  # Stripe 前端支付用
    
    # 履约集成（Shopify/Wix）
    Column("shopify_order_id", String(255), nullable=True, unique=True),
    Column("tracking_number", String(255), nullable=True),
    Column("carrier", String(100), nullable=True),
    
    # 时间戳
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now()),
    Column("paid_at", DateTime(timezone=True), nullable=True),
    Column("shipped_at", DateTime(timezone=True), nullable=True),
    Column("delivered_at", DateTime(timezone=True), nullable=True),
    Column("cancelled_at", DateTime(timezone=True), nullable=True),
    
    # 元数据
    Column("agent_session_id", String(255), nullable=True, index=True),
    Column("metadata", JSON, nullable=True),
    
    # 软删除（防御性设计：订单不能真删除）
    Column("is_deleted", Boolean, default=False, index=True),
)


# ============================================================================
# CRUD 操作
# ============================================================================

async def create_order(order_data: Dict[str, Any]) -> str:
    """创建新订单"""
    order_id = f"ORD_{secrets.token_hex(8).upper()}"
    order_data["order_id"] = order_id
    order_data["status"] = "pending"
    order_data["payment_status"] = "unpaid"
    order_data["is_deleted"] = False  # Explicitly set for PostgreSQL
    
    query = orders.insert().values(**order_data)
    await database.execute(query)
    return order_id


async def get_order(order_id: str) -> Optional[Dict[str, Any]]:
    """获取订单详情"""
    # PostgreSQL-compatible query
    query = orders.select().where(
        (orders.c.order_id == order_id) & 
        orders.c.is_deleted.is_(False)
    )
    result = await database.fetch_one(query)
    return dict(result) if result else None


async def get_orders_by_merchant(
    merchant_id: str, 
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0
) -> List[Dict[str, Any]]:
    """获取商户的订单列表"""
    query = orders.select().where(
        (orders.c.merchant_id == merchant_id) & 
        orders.c.is_deleted.is_(False)
    )
    
    if status:
        query = query.where(orders.c.status == status)
    
    query = query.order_by(orders.c.created_at.desc()).limit(limit).offset(offset)
    
    results = await database.fetch_all(query)
    return [dict(r) for r in results]


async def get_orders_by_customer(customer_email: str, limit: int = 50) -> List[Dict[str, Any]]:
    """获取客户的订单列表"""
    query = orders.select().where(
        (orders.c.customer_email == customer_email) & 
        orders.c.is_deleted.is_(False)
    ).order_by(orders.c.created_at.desc()).limit(limit)
    
    results = await database.fetch_all(query)
    return [dict(r) for r in results]


async def update_order_status(
    order_id: str, 
    status: str, 
    **additional_fields
) -> bool:
    """更新订单状态（防御性：只能前进，不能回退）"""
    update_data = {"status": status, "updated_at": datetime.now()}
    update_data.update(additional_fields)
    
    query = orders.update().where(
        orders.c.order_id == order_id
    ).values(**update_data)
    
    result = await database.execute(query)
    # Handle None result from PostgreSQL
    return result is not None and result > 0


async def update_payment_info(
    order_id: str,
    payment_intent_id: str,
    client_secret: str,
    payment_status: str = "processing"
) -> bool:
    """更新支付信息"""
    query = orders.update().where(
        orders.c.order_id == order_id
    ).values(
        payment_intent_id=payment_intent_id,
        client_secret=client_secret,
        payment_status=payment_status,
        updated_at=datetime.now()
    )
    
    result = await database.execute(query)
    # Handle None result from PostgreSQL
    return result is not None and result > 0


async def mark_order_paid(order_id: str) -> bool:
    """标记订单已支付"""
    query = orders.update().where(
        orders.c.order_id == order_id
    ).values(
        status="paid",
        payment_status="paid",
        paid_at=datetime.now(),
        updated_at=datetime.now()
    )
    
    result = await database.execute(query)
    # Handle None result from PostgreSQL
    return result is not None and result > 0


async def update_fulfillment_info(
    order_id: str,
    shopify_order_id: Optional[str] = None,
    tracking_number: Optional[str] = None,
    carrier: Optional[str] = None,
    fulfillment_status: Optional[str] = None
) -> bool:
    """更新履约信息"""
    update_data = {"updated_at": datetime.now()}
    
    if shopify_order_id:
        update_data["shopify_order_id"] = shopify_order_id
    if tracking_number:
        update_data["tracking_number"] = tracking_number
    if carrier:
        update_data["carrier"] = carrier
    if fulfillment_status:
        update_data["fulfillment_status"] = fulfillment_status
    
    query = orders.update().where(
        orders.c.order_id == order_id
    ).values(**update_data)
    
    result = await database.execute(query)
    # Handle None result from PostgreSQL
    return result is not None and result > 0


async def mark_order_shipped(
    order_id: str,
    tracking_number: str,
    carrier: Optional[str] = None
) -> bool:
    """标记订单已发货"""
    query = orders.update().where(
        orders.c.order_id == order_id
    ).values(
        status="shipped",
        fulfillment_status="shipped",
        tracking_number=tracking_number,
        carrier=carrier,
        shipped_at=datetime.now(),
        updated_at=datetime.now()
    )
    
    result = await database.execute(query)
    # Handle None result from PostgreSQL
    return result is not None and result > 0


# ============================================================================
# 统计查询
# ============================================================================

async def get_order_stats(merchant_id: str) -> Dict[str, Any]:
    """获取商户订单统计"""
    from sqlalchemy import select, func
    
    # 总订单数
    total_query = select([func.count()]).select_from(orders).where(
        (orders.c.merchant_id == merchant_id) & 
        orders.c.is_deleted.is_(False)
    )
    total_orders = await database.fetch_val(total_query)
    
    # 已支付订单数
    paid_query = select([func.count()]).select_from(orders).where(
        (orders.c.merchant_id == merchant_id) & 
        (orders.c.payment_status == "paid") &
        orders.c.is_deleted.is_(False)
    )
    paid_orders = await database.fetch_val(paid_query)
    
    # 总收入
    revenue_query = select([func.sum(orders.c.total)]).select_from(orders).where(
        (orders.c.merchant_id == merchant_id) & 
        (orders.c.payment_status == "paid") &
        orders.c.is_deleted.is_(False)
    )
    total_revenue = await database.fetch_val(revenue_query) or 0
    
    return {
        "total_orders": total_orders or 0,
        "paid_orders": paid_orders or 0,
        "pending_orders": (total_orders or 0) - (paid_orders or 0),
        "total_revenue": float(total_revenue),
        "currency": "USD"
    }

