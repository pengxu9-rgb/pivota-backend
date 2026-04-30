"""
订单数据库表和 CRUD 操作
防御性架构：订单是核心业务数据，只能追加和更新状态，不能删除
"""

import asyncio

from sqlalchemy import Table, Column, Integer, String, Text, DateTime, JSON, Numeric, Boolean
from sqlalchemy.sql import func
from datetime import datetime
from typing import Optional, List, Dict, Any
import secrets
import os
from sqlalchemy import text

from db.database import metadata, database


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
    Column("discount_total", Numeric(10, 2), default=0),
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
    Column("client_secret", Text, nullable=True),  # PSP client secret / Adyen sessionData
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

    # Buyer Vault linkage (internal-only; never expose global buyer_id to agents)
    Column("buyer_id", String(50), nullable=True, index=True),
    Column("intent_id", String(80), nullable=True, index=True),
    Column("agent_user_ref", String(255), nullable=True, index=True),
    Column("agent_scoped_buyer_ref", String(128), nullable=True, index=True),
    
    # 软删除（防御性设计：订单不能真删除）
    Column("is_deleted", Boolean, default=False, index=True),
)


# ============================================================================
# CRUD 操作
# ============================================================================

_client_secret_storage_ready = False
_client_secret_storage_lock = asyncio.Lock()


async def _ensure_client_secret_storage_allows_long_values() -> None:
    """
    Make `orders.client_secret` safe for long PSP surfaces such as Adyen sessionData.

    Older environments still carry `VARCHAR(500)`, which truncates Adyen sessions
    and makes client-owned confirmation surfaces invalid. We self-heal to `TEXT`
    once and keep the call best-effort.
    """
    global _client_secret_storage_ready

    if _client_secret_storage_ready:
        return

    async with _client_secret_storage_lock:
        if _client_secret_storage_ready:
            return
        try:
            await database.execute(
                text(
                    """
                    ALTER TABLE orders
                    ADD COLUMN IF NOT EXISTS client_secret TEXT
                    """
                )
            )
        except Exception:
            pass
        try:
            await database.execute(
                text(
                    """
                    ALTER TABLE orders
                    ALTER COLUMN client_secret TYPE TEXT
                    """
                )
            )
        except Exception:
            pass
        _client_secret_storage_ready = True

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
                    ADD COLUMN IF NOT EXISTS client_secret TEXT;
                """))
            if "column \"subtotal\" of relation \"orders\" does not exist" in err or "subtotal" in err:
                await database.execute(text("""
                    ALTER TABLE orders 
                    ADD COLUMN IF NOT EXISTS subtotal NUMERIC(10,2);
                """))
            if "column \"discount_total\" of relation \"orders\" does not exist" in err or "discount_total" in err:
                await database.execute(text("""
                    ALTER TABLE orders
                    ADD COLUMN IF NOT EXISTS discount_total NUMERIC(10,2) DEFAULT 0;
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
            if "column \"buyer_id\" of relation \"orders\" does not exist" in err or "buyer_id" in err:
                await database.execute(text("""
                    ALTER TABLE orders
                    ADD COLUMN IF NOT EXISTS buyer_id VARCHAR(50);
                """))
            if "column \"intent_id\" of relation \"orders\" does not exist" in err or "intent_id" in err:
                await database.execute(text("""
                    ALTER TABLE orders
                    ADD COLUMN IF NOT EXISTS intent_id VARCHAR(80);
                """))
            if "column \"agent_user_ref\" of relation \"orders\" does not exist" in err or "agent_user_ref" in err:
                await database.execute(text("""
                    ALTER TABLE orders
                    ADD COLUMN IF NOT EXISTS agent_user_ref VARCHAR(255);
                """))
            if "column \"agent_scoped_buyer_ref\" of relation \"orders\" does not exist" in err or "agent_scoped_buyer_ref" in err:
                await database.execute(text("""
                    ALTER TABLE orders
                    ADD COLUMN IF NOT EXISTS agent_scoped_buyer_ref VARCHAR(128);
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


async def find_replayable_order_for_create(
    *,
    merchant_id: str,
    idempotency_key: Optional[str] = None,
    quote_id: Optional[str] = None,
    agent_session_id: Optional[str] = None,
    preferred_psp: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Find the most recent order that is safe to replay for order-create recovery.

    Priority:
    1. metadata.idempotency_key
    2. metadata.pricing_quote.quote_id
    3. agent_session_id
    """
    merchant_id = str(merchant_id or "").strip()
    if not merchant_id:
        return None

    def _normalize_preferred_psp(value: Optional[str]) -> Optional[str]:
        normalized = str(value or "").strip().lower()
        if not normalized:
            return None
        if normalized == "stripe_checkout":
            return "stripe"
        return normalized

    def _replay_matches_preferred_psp(row: Dict[str, Any], preferred: Optional[str]) -> bool:
        if not preferred:
            return True
        actual = _normalize_preferred_psp(
            (row or {}).get("psp_used")
            or ((row or {}).get("metadata") or {}).get("preferred_psp")
            or ((row or {}).get("metadata") or {}).get("selected_psp")
        )
        return actual == preferred

    normalized_preferred_psp = _normalize_preferred_psp(preferred_psp)
    candidate_queries = []
    if idempotency_key:
        candidate_queries.append(
            (
                """
                SELECT *
                FROM orders
                WHERE merchant_id = :merchant_id
                  AND COALESCE(is_deleted, FALSE) = FALSE
                  AND metadata ->> 'idempotency_key' = :match_value
                ORDER BY created_at DESC
                LIMIT 1
                """,
                str(idempotency_key).strip(),
                "idempotency_key",
            )
        )
    if quote_id:
        candidate_queries.append(
            (
                """
                SELECT *
                FROM orders
                WHERE merchant_id = :merchant_id
                  AND COALESCE(is_deleted, FALSE) = FALSE
                  AND metadata -> 'pricing_quote' ->> 'quote_id' = :match_value
                ORDER BY created_at DESC
                LIMIT 1
                """,
                str(quote_id).strip(),
                "quote_id",
            )
        )
    if agent_session_id:
        candidate_queries.append(
            (
                """
                SELECT *
                FROM orders
                WHERE merchant_id = :merchant_id
                  AND COALESCE(is_deleted, FALSE) = FALSE
                  AND agent_session_id = :match_value
                ORDER BY created_at DESC
                LIMIT 1
                """,
                str(agent_session_id).strip(),
                "agent_session_id",
            )
        )

    for query, match_value, replay_scope in candidate_queries:
        if not match_value:
            continue
        row = await database.fetch_one(
            query,
            {"merchant_id": merchant_id, "match_value": match_value},
        )
        if row:
            row_dict = dict(row)
            if replay_scope != "idempotency_key" and not _replay_matches_preferred_psp(
                row_dict,
                normalized_preferred_psp,
            ):
                continue
            return row_dict
    return None


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
            SELECT merchant_id, status, fulfillment_status, payment_status, metadata
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
    if isinstance(update_data.get("metadata"), dict):
        existing_metadata = {}
        try:
            existing_raw = before["metadata"] if before else None
            if isinstance(existing_raw, dict):
                existing_metadata = dict(existing_raw)
        except Exception:
            existing_metadata = {}
        # Metadata updates are additive by default. This prevents webhook/aftercare
        # handlers using an older order snapshot from erasing recovery/audit keys
        # such as merchant_order or payment_recovery.
        update_data["metadata"] = {**existing_metadata, **update_data["metadata"]}

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
    after = None

    async def _fetch_after_update():
        return await database.fetch_one(
            """
            SELECT merchant_id, status, fulfillment_status, payment_status, shopify_order_id
            FROM orders
            WHERE order_id = :order_id
            LIMIT 1
            """,
            {"order_id": order_id},
        )

    if not ok:
        # Some DB drivers/environments return no reliable rowcount for UPDATE
        # even when the write committed. Verify the intended state before
        # treating the update as failed.
        try:
            after = await _fetch_after_update()
        except Exception:
            after = None
        if after:
            try:
                status_matches = str(after.get("status") or "").strip() == str(status).strip()
                payment_status_matches = (
                    "payment_status" not in update_data
                    or str(after.get("payment_status") or "").strip()
                    == str(update_data.get("payment_status") or "").strip()
                )
                fulfillment_status_matches = (
                    "fulfillment_status" not in update_data
                    or str(after.get("fulfillment_status") or "").strip()
                    == str(update_data.get("fulfillment_status") or "").strip()
                )
                shopify_order_matches = (
                    "shopify_order_id" not in update_data
                    or str(after.get("shopify_order_id") or "").strip()
                    == str(update_data.get("shopify_order_id") or "").strip()
                )
                ok = bool(
                    status_matches
                    and payment_status_matches
                    and fulfillment_status_matches
                    and shopify_order_matches
                )
            except Exception:
                ok = False

    # Best-effort invitation enqueue on completion/shipping transitions.
    if ok:
        if after is None:
            try:
                after = await _fetch_after_update()
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
                    from services.reviews_invitation_send_jobs_service import (
                        enqueue_invitation_send_job_from_order,
                    )

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
    safe_secret = client_secret
    try:
        if client_secret and len(str(client_secret)) > 480:
            await _ensure_client_secret_storage_allows_long_values()
            safe_secret = str(client_secret)
    except Exception:
        # Preserve the original secret if self-heal fails; callers still need the
        # complete PSP surface for client confirmation flows.
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
    now = datetime.now()
    query = (
        orders.update()
        .where(orders.c.order_id == order_id)
        .values(status="paid", payment_status="paid", paid_at=now, updated_at=now)
    )

    result = await database.execute(query)
    ok = result is not None and result > 0

    # Best-effort: converge `payments.status` for this order so customer-facing APIs
    # don't show stale "processing" after the PSP webhook confirms payment.
    if ok:
        try:
            row = await database.fetch_one(
                """
                SELECT payment_intent_id
                FROM orders
                WHERE order_id = :order_id
                LIMIT 1
                """,
                {"order_id": order_id},
            )
            payment_intent_id = None
            if row:
                try:
                    payment_intent_id = row["payment_intent_id"]
                except Exception:
                    payment_intent_id = None

            if payment_intent_id:
                await database.execute(
                    """
                    UPDATE payments
                    SET status = 'succeeded'
                    WHERE order_id = :order_id
                      AND payment_intent_id = :payment_intent_id
                      AND status <> 'succeeded'
                    """,
                    {"order_id": order_id, "payment_intent_id": payment_intent_id},
                )
            else:
                await database.execute(
                    """
                    UPDATE payments
                    SET status = 'succeeded'
                    WHERE order_id = :order_id
                      AND status IN ('processing', 'requires_action')
                    """,
                    {"order_id": order_id},
                )
        except Exception:
            pass

    return ok


async def update_fulfillment_info(
    order_id: str,
    shopify_order_id: Optional[str] = None,
    tracking_number: Optional[str] = None,
    carrier: Optional[str] = None,
    fulfillment_status: Optional[str] = None
) -> bool:
    """更新履约信息"""
    update_data: Dict[str, Any] = {"updated_at": datetime.now()}

    if shopify_order_id:
        update_data["shopify_order_id"] = shopify_order_id
    if tracking_number:
        update_data["tracking_number"] = tracking_number
    if carrier:
        update_data["carrier"] = carrier
    if fulfillment_status:
        update_data["fulfillment_status"] = fulfillment_status

    result = await database.execute(
        orders.update().where(orders.c.order_id == order_id).values(**update_data)
    )
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
                paid = (
                    str((row["payment_status"] if row else "") or "").strip().lower()
                    == "paid"
                )
                ful = str((row["fulfillment_status"] if row else "") or "").strip().lower()
                merchant_id = str((row["merchant_id"] if row else "") or "").strip()
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
    # NOTE: `database.fetch_one()` on SQLAlchemy UPDATE + RETURNING can behave differently
    # across driver/adapter versions. Use `execute()` for the mutation and fetch merchant_id
    # with a follow-up SELECT for consistency.
    updated_at = datetime.now()
    result = await database.execute(
        orders.update()
        .where(orders.c.order_id == order_id)
        .values(
            status="completed",
            fulfillment_status="shipped",
            tracking_number=tracking_number,
            carrier=carrier,
            shipped_at=updated_at,
            updated_at=updated_at,
        )
    )
    ok = result is not None and result > 0
    if not ok:
        return False
    row = await database.fetch_one(
        "SELECT merchant_id FROM orders WHERE order_id = :order_id LIMIT 1",
        {"order_id": order_id},
    )

    # Best-effort invitation scheduling: enqueue a job for a worker service to send the email.
    try:
        merchant_id = str((row["merchant_id"] if row else "") or "").strip()
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

    update_values: Dict[str, Any] = {}
    for key, value in update_data.items():
        if key in {"order_id", "is_deleted", "created_at"}:
            continue
        if hasattr(orders.c, key):
            update_values[key] = value

    if not update_values:
        return False

    update_values["updated_at"] = datetime.now()
    query = (
        orders.update()
        .where((orders.c.order_id == order_id) & (orders.c.is_deleted.is_(False)))
        .values(**update_values)
        .returning(orders.c.order_id)
    )

    result = await database.fetch_one(query)
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
