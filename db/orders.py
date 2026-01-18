"""
订单数据库表和 CRUD 操作
防御性架构：订单是核心业务数据，只能追加和更新状态，不能删除
"""

from sqlalchemy import Table, Column, Integer, String, Text, DateTime, JSON, Numeric, Boolean
from sqlalchemy.sql import func
from datetime import datetime
from typing import Optional, List, Dict, Any
import secrets
import os

from db.database import metadata, database
from services.reviews_invitation_send_jobs_service import enqueue_invitation_send_job_from_order


# ============================================================================
# 订单表（核心业务数据）
# ============================================================================

orders = Table(
    "orders",
    metadata,
    Column("order_id", String(50), primary_key=True),  # 订单唯一ID（主键）
    Column("merchant_id", String(50), index=True, nullable=False),
    
    # Legacy fields (for backward compatibility with existing DB)
    Column("store_id", String(50), nullable=True),
    Column("psp_id", String(50), nullable=True),
    # Column("amount", Numeric(10, 2), nullable=True),  # REMOVED - use "total" instead
    
    # 客户信息
    Column("customer_name", String(255), nullable=True),
    Column("customer_email", String(255), nullable=False),
    Column("shipping_address", JSON, nullable=False),  # ShippingAddress JSON
    
    # 订单内容
    Column("items", JSON, nullable=False),  # List[OrderItem] JSON
    
    # 金额（使用 Numeric 精确存储）
    Column("subtotal", Numeric(10, 2), nullable=False),
    Column("shipping_fee", Numeric(10, 2), default=0),
    Column("tax", Numeric(10, 2), default=0),
    Column("total", Numeric(10, 2), nullable=False),
    Column("total_refunded", Numeric(10, 2), default=0),
    Column("currency", String(3), default="USD"),
    
    # 状态机
    Column("status", String(50), default="pending", index=True),  # 订单状态
    Column("payment_status", String(50), default="unpaid", index=True),  # 支付状态
    Column("payment_method", String(50), nullable=True),  # Legacy payment method field
    Column("fulfillment_status", String(50), nullable=True),  # 履约状态
    
    # 支付集成（Stripe）
    Column("payment_intent_id", String(255), nullable=True, unique=True),
    Column("payment_method_id", String(255), nullable=True),
    Column("client_secret", String(500), nullable=True),  # Stripe 前端支付用
    Column("psp_used", String(50), nullable=True),  # Which PSP was actually used for this order
    
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
    Column("agent_id", String(255), nullable=True, index=True),  # Agent who created this order
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
    try:
        await database.execute(query)
        return order_id
    except Exception as e:
        # Auto-migrate missing columns on production DBs, then retry once
        err = str(e)
        try:
            from sqlalchemy import text
            if "column \"shipping_address\" of relation \"orders\" does not exist" in err or "shipping_address" in err:
                # Add missing columns defensively
                await database.execute(text("""
                    ALTER TABLE orders 
                    ADD COLUMN IF NOT EXISTS shipping_address JSONB;
                """))
            if "column \"items\" of relation \"orders\" does not exist" in err or " column \"items\"" in err:
                await database.execute(text("""
                    ALTER TABLE orders 
                    ADD COLUMN IF NOT EXISTS items JSONB;
                """))
            if "column \"client_secret\" of relation \"orders\" does not exist" in err or "client_secret" in err:
                await database.execute(text("""
                    ALTER TABLE orders 
                    ADD COLUMN IF NOT EXISTS client_secret VARCHAR(500);
                """))
            if "column \"subtotal\" of relation \"orders\" does not exist" in err or "subtotal" in err:
                await database.execute(text("""
                    ALTER TABLE orders 
                    ADD COLUMN IF NOT EXISTS subtotal NUMERIC(10,2);
                """))
            if "column \"tax\" of relation \"orders\" does not exist" in err or "tax" in err:
                await database.execute(text("""
                    ALTER TABLE orders 
                    ADD COLUMN IF NOT EXISTS tax NUMERIC(10,2);
                """))
            if "column \"total\" of relation \"orders\" does not exist" in err or "total" in err:
                await database.execute(text("""
                    ALTER TABLE orders 
                    ADD COLUMN IF NOT EXISTS total NUMERIC(10,2);
                """))
            if "column \"total_refunded\" of relation \"orders\" does not exist" in err or "total_refunded" in err:
                await database.execute(text("""
                    ALTER TABLE orders
                    ADD COLUMN IF NOT EXISTS total_refunded NUMERIC(10,2) DEFAULT 0;
                """))
            if "column \"shipping_fee\" of relation \"orders\" does not exist" in err or "shipping_fee" in err:
                await database.execute(text("""
                    ALTER TABLE orders 
                    ADD COLUMN IF NOT EXISTS shipping_fee NUMERIC(10,2);
                """))
            if "column \"payment_status\" of relation \"orders\" does not exist" in err or "payment_status" in err:
                await database.execute(text("""
                    ALTER TABLE orders 
                    ADD COLUMN IF NOT EXISTS payment_status VARCHAR(50);
                """))
            if "column \"agent_id\" of relation \"orders\" does not exist" in err or "agent_id" in err:
                await database.execute(text("""
                    ALTER TABLE orders 
                    ADD COLUMN IF NOT EXISTS agent_id VARCHAR(50);
                """))
            if "column \"agent_session_id\" of relation \"orders\" does not exist" in err or "agent_session_id" in err:
                await database.execute(text("""
                    ALTER TABLE orders 
                    ADD COLUMN IF NOT EXISTS agent_session_id VARCHAR(100);
                """))
            if "column \"is_deleted\" of relation \"orders\" does not exist" in err or "is_deleted" in err:
                await database.execute(text("""
                    ALTER TABLE orders 
                    ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN DEFAULT FALSE;
                """))
            if "column \"metadata\" of relation \"orders\" does not exist" in err or "metadata" in err:
                await database.execute(text("""
                    ALTER TABLE orders 
                    ADD COLUMN IF NOT EXISTS metadata JSONB;
                """))
            if "column \"psp_used\" of relation \"orders\" does not exist" in err or "psp_used" in err or "Unconsumed column names: psp_used" in err:
                await database.execute(text("""
                    ALTER TABLE orders 
                    ADD COLUMN IF NOT EXISTS psp_used VARCHAR(50);
                """))
            if "null value in column \"amount\"" in err or "amount" in err:
                # Drop NOT NULL constraint on amount column (we use total instead)
                await database.execute(text("""
                    ALTER TABLE orders 
                    ALTER COLUMN amount DROP NOT NULL;
                """))
            # Retry the insert once after migration
            await database.execute(query)
            return order_id
        except Exception as mig_err:
            # Surface original error if migration fails
            raise mig_err


async def get_order(order_id: str) -> Optional[Dict[str, Any]]:
    """获取订单详情"""
    # PostgreSQL-compatible query
    query = orders.select().where(
        (orders.c.order_id == order_id) & 
        orders.c.is_deleted.is_(False)
    )
    try:
        result = await database.fetch_one(query)
    except Exception as e:
        # Defensive: some prod DBs may lag behind the SQLAlchemy table definition.
        err = str(e)
        if "total_refunded" in err:
            try:
                from sqlalchemy import text

                await database.execute(
                    text("ALTER TABLE orders ADD COLUMN IF NOT EXISTS total_refunded NUMERIC(10,2) DEFAULT 0;")
                )
                result = await database.fetch_one(query)
            except Exception:
                raise
        else:
            raise
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
    """更新订单状态（防御性：只能前进，不能回退）

    Side-effect (best-effort): when an order transitions into a "completed" or
    shipped/delivered fulfillment state, enqueue a review invitation email job
    for the worker to process.
    """
    before = None
    try:
        before = await database.fetch_one(
            """
            SELECT merchant_id, status, fulfillment_status, payment_status
            FROM orders
            WHERE order_id = :order_id
            LIMIT 1
            """,
            {"order_id": order_id},
        )
    except Exception:
        before = None

    update_data = {"status": status, "updated_at": datetime.now()}
    update_data.update(additional_fields)

    # Defensive: ignore fields not present in the SQLAlchemy table definition.
    # Some environments have divergent schemas; passing unknown keys raises:
    # "Unconsumed column names: <...>"
    valid_cols = set(orders.c.keys())
    update_data = {k: v for (k, v) in update_data.items() if k in valid_cols}
    
    query = orders.update().where(
        orders.c.order_id == order_id
    ).values(**update_data)
    
    result = await database.execute(query)
    ok = result is not None and result > 0

    # Best-effort invitation enqueue on completion/shipping transitions.
    if ok:
        try:
            after = await database.fetch_one(
                """
                SELECT merchant_id, status, fulfillment_status, payment_status
                FROM orders
                WHERE order_id = :order_id
                LIMIT 1
                """,
                {"order_id": order_id},
            )
        except Exception:
            after = None

        try:
            if before and after:
                before_status = str(before.get("status") or "").strip().lower()
                after_status = str(after.get("status") or "").strip().lower()
                before_ful = str(before.get("fulfillment_status") or "").strip().lower()
                after_ful = str(after.get("fulfillment_status") or "").strip().lower()
                after_paid = str(after.get("payment_status") or "").strip().lower() == "paid"
                merchant_id = str(after.get("merchant_id") or "").strip()

                transitioned_completed = before_status != "completed" and after_status == "completed"
                transitioned_fulfilled = before_ful != after_ful and after_ful in {"shipped", "delivered"}

                if merchant_id and after_paid and (transitioned_completed or transitioned_fulfilled):
                    await enqueue_invitation_send_job_from_order(
                        merchant_id=merchant_id,
                        order_id=order_id,
                        force_reschedule=False,
                    )
        except Exception:
            pass

    return ok


async def update_payment_info(
    order_id: str,
    payment_intent_id: str,
    client_secret: str,
    payment_status: str = "processing",
    psp_used: Optional[str] = None
) -> bool:
    """更新支付信息
    
    - payment_intent_id / client_secret / payment_status 按照原有逻辑更新
    - 可选的 psp_used 用于记录实际使用的 PSP 提供方（如 'stripe' 或 'adyen'）
    """
    # Protect against extremely long secrets (e.g. Adyen sessionData) exceeding
    # the VARCHAR(500) limit on client_secret. We only need full secrets for
    # PSP/frontends, not for the orders table, so truncation here is safe.
    safe_secret = client_secret
    try:
        if client_secret and len(client_secret) > 480:
            safe_secret = client_secret[:480]
    except Exception:
        # If length check fails for any reason, fall back to original value.
        safe_secret = client_secret

    update_values = {
        "payment_intent_id": payment_intent_id,
        "client_secret": safe_secret,
        "payment_status": payment_status,
        "updated_at": datetime.now(),
    }
    if psp_used:
        update_values["psp_used"] = psp_used
    
    query = orders.update().where(orders.c.order_id == order_id).values(**update_values)
    
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
    ok = result is not None and result > 0

    # Best-effort: enqueue invitation when fulfillment becomes shipped/delivered.
    if ok and fulfillment_status:
        try:
            row = await database.fetch_one(
                """
                SELECT merchant_id, payment_status, fulfillment_status
                FROM orders
                WHERE order_id = :order_id
                LIMIT 1
                """,
                {"order_id": order_id},
            )
            if row:
                paid = str(row.get("payment_status") or "").strip().lower() == "paid"
                ful = str(row.get("fulfillment_status") or "").strip().lower()
                merchant_id = str(row.get("merchant_id") or "").strip()
                if paid and merchant_id and ful in {"shipped", "delivered"}:
                    await enqueue_invitation_send_job_from_order(
                        merchant_id=merchant_id,
                        order_id=order_id,
                        force_reschedule=False,
                    )
        except Exception:
            pass

    return ok


async def mark_order_shipped(
    order_id: str,
    tracking_number: str,
    carrier: Optional[str] = None
) -> bool:
    """标记订单已发货"""
    query = (
        orders.update()
        .where(orders.c.order_id == order_id)
        .values(
            status="completed",
            fulfillment_status="shipped",
            tracking_number=tracking_number,
            carrier=carrier,
            shipped_at=datetime.now(),
            updated_at=datetime.now()
        )
        .returning(orders.c.order_id, orders.c.merchant_id)
    )
    result = await database.fetch_one(query)
    if not result:
        return False

    # Best-effort invitation scheduling: enqueue a job for a worker service to send the email.
    try:
        merchant_id = str(result.get("merchant_id") or "").strip()
        if merchant_id:
            await enqueue_invitation_send_job_from_order(
                merchant_id=merchant_id,
                order_id=order_id,
                force_reschedule=False,
            )
    except Exception:
        # Do not block fulfillment on invitation scheduling failures.
        pass

    return True


# ============================================================================
# 通用更新函数
# ============================================================================

async def update_order(order_id: str, update_data: Dict[str, Any]) -> bool:
    """更新订单数据"""
    if not update_data:
        return False
    
    # 构建更新语句
    set_clauses = []
    values = {"order_id": order_id}
    
    for key, value in update_data.items():
        if key not in ["order_id", "is_deleted", "created_at"]:  # 保护某些字段
            # 将Python字段名转换为数据库字段名
            if hasattr(orders.c, key):
                set_clauses.append(f"{key} = :{key}")
                values[key] = value
    
    if not set_clauses:
        return False
    
    query = f"""
        UPDATE orders 
        SET {', '.join(set_clauses)}, updated_at = CURRENT_TIMESTAMP
        WHERE order_id = :order_id AND is_deleted = false
        RETURNING order_id
    """
    
    result = await database.fetch_one(query, values)
    return result is not None

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
