"""
Products Database - Defense-in-Depth Architecture
分层数据库架构：核心层 / 缓存层 / 事件层 / 分析层
防止 Agent 查询弄乱数据
"""

from sqlalchemy import Table, Column, Integer, String, DateTime, Boolean, Text, JSON, Float, BigInteger, Index
from sqlalchemy.sql import func
from db.database import metadata, database
from typing import Dict, List, Any, Optional
from datetime import datetime, timedelta
import logging

logger = logging.getLogger(__name__)

# ============================================================================
# LAYER 2: CACHE TABLES (临时缓存层 - Agent 只读)
# ============================================================================

products_cache = Table(
    "products_cache",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("merchant_id", String(50), nullable=False, index=True),
    Column("platform", String(50), nullable=False, index=True),
    Column("platform_product_id", String(100), nullable=False, index=True),
    
    # 产品数据（JSON，存储 StandardProduct）
    Column("product_data", JSON, nullable=False),  # 完整的 StandardProduct 数据
    
    # 缓存元数据
    Column("cache_status", String(20), default="fresh"),  # fresh, stale, expired
    Column("cached_at", DateTime, server_default=func.now()),
    Column("expires_at", DateTime, nullable=False),  # TTL 过期时间
    Column("ttl_seconds", Integer, default=3600),  # 缓存生命周期（秒）
    
    # 访问统计
    Column("access_count", Integer, default=0),
    Column("last_accessed_at", DateTime, nullable=True),
    
    # 索引优化
    Index("idx_merchant_platform", "merchant_id", "platform"),
    Index("idx_expires_at", "expires_at"),
)


# ============================================================================
# LAYER 3: EVENT TABLES (事件层 - 只追加，用于分析)
# ============================================================================

api_call_events = Table(
    "api_call_events",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("event_type", String(50), nullable=False),  # product_query, product_detail, order_create, payment_execute
    Column("merchant_id", String(50), nullable=False, index=True),
    Column("user_type", String(20), default="agent"),  # agent, admin, customer
    Column("endpoint", String(255), nullable=True),  # API 路径
    
    # 请求详情
    Column("request_params", JSON, nullable=True),  # 查询参数
    Column("response_status", Integer, nullable=True),  # HTTP 状态码
    Column("cache_hit", Boolean, default=False),  # 是否命中缓存
    
    # 性能指标
    Column("response_time_ms", Integer, nullable=True),  # 响应时间（毫秒）
    
    # 关联数据
    Column("product_ids", JSON, nullable=True),  # 涉及的产品 ID 列表
    Column("order_id", String(100), nullable=True),  # 关联订单（如有）
    
    # 时间戳
    Column("created_at", DateTime, server_default=func.now(), index=True),
    
    # 索引优化
    Index("idx_api_call_merchant_created", "merchant_id", "created_at"),
    Index("idx_api_call_event_type_created", "event_type", "created_at"),
)


order_events = Table(
    "order_events",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("event_type", String(50), nullable=False),  # order_created, payment_attempted, payment_succeeded, payment_failed, order_fulfilled
    Column("merchant_id", String(50), nullable=False, index=True),
    Column("order_id", String(100), nullable=False, index=True),
    
    # 订单详情
    Column("product_ids", JSON, nullable=True),  # 产品 ID 列表
    Column("total_amount", Float, nullable=True),
    Column("currency", String(10), default="USD"),
    Column("payment_method", String(50), nullable=True),  # stripe, adyen, etc.
    
    # 状态信息
    Column("status", String(50), nullable=True),  # pending, succeeded, failed, cancelled
    Column("error_message", Text, nullable=True),
    
    # 元数据
    Column("metadata", JSON, nullable=True),  # 额外信息
    
    # 时间戳
    Column("created_at", DateTime, server_default=func.now(), index=True),
    
    # 索引优化
    Index("idx_order_merchant_order", "merchant_id", "order_id"),
    Index("idx_order_event_type_created", "event_type", "created_at"),
)


# ============================================================================
# LAYER 4: ANALYTICS TABLES (分析层 - 定期计算的衍生数据)
# ============================================================================

merchant_analytics = Table(
    "merchant_analytics",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("merchant_id", String(50), nullable=False, unique=True, index=True),
    
    # 产品指标
    Column("total_products", Integer, default=0),
    Column("products_accessed", Integer, default=0),  # 被查询过的产品数
    Column("avg_product_access", Float, default=0.0),  # 平均每个产品被访问次数
    
    # API 调用指标
    Column("total_api_calls", Integer, default=0),
    Column("cache_hit_rate", Float, default=0.0),  # 缓存命中率
    Column("avg_response_time_ms", Float, default=0.0),
    
    # 订单指标
    Column("total_orders", Integer, default=0),
    Column("successful_orders", Integer, default=0),
    Column("failed_orders", Integer, default=0),
    Column("conversion_rate", Float, default=0.0),  # API 调用 → 订单转化率
    
    # 支付指标
    Column("total_payments", Integer, default=0),
    Column("successful_payments", Integer, default=0),
    Column("failed_payments", Integer, default=0),
    Column("payment_success_rate", Float, default=0.0),
    Column("total_revenue", Float, default=0.0),
    
    # 时间窗口
    Column("window_start", DateTime, nullable=True),  # 统计窗口开始
    Column("window_end", DateTime, nullable=True),  # 统计窗口结束
    
    # 更新时间
    Column("updated_at", DateTime, server_default=func.now(), onupdate=func.now()),
    Column("created_at", DateTime, server_default=func.now()),
)


# ============================================================================
# CACHE OPERATIONS (缓存层操作)
# ============================================================================

async def get_cached_products(
    merchant_id: str,
    platform: str,
    include_expired: bool = False
) -> List[Dict[str, Any]]:
    """
    从缓存获取产品（Agent 只读）
    """
    query = products_cache.select().where(
        (products_cache.c.merchant_id == merchant_id) &
        (products_cache.c.platform == platform)
    )
    
    if not include_expired:
        query = query.where(products_cache.c.expires_at > datetime.now())
    
    query = query.order_by(products_cache.c.cached_at.desc())
    
    results = await database.fetch_all(query)
    return [dict(r) for r in results]


async def upsert_product_cache(
    merchant_id: str,
    platform: str,
    platform_product_id: str,
    product_data: Dict[str, Any],
    ttl_seconds: int = 3600
) -> int:
    """
    更新产品缓存（后台任务调用，非 Agent）
    """
    expires_at = datetime.now() + timedelta(seconds=ttl_seconds)
    
    # 检查是否存在
    query = products_cache.select().where(
        (products_cache.c.merchant_id == merchant_id) &
        (products_cache.c.platform == platform) &
        (products_cache.c.platform_product_id == platform_product_id)
    )
    existing = await database.fetch_one(query)
    
    if existing:
        # 更新
        update_query = products_cache.update().where(
            products_cache.c.id == existing["id"]
        ).values(
            product_data=product_data,
            cached_at=datetime.now(),
            expires_at=expires_at,
            ttl_seconds=ttl_seconds,
            cache_status="fresh"
        )
        await database.execute(update_query)
        return existing["id"]
    else:
        # 插入
        insert_query = products_cache.insert().values(
            merchant_id=merchant_id,
            platform=platform,
            platform_product_id=platform_product_id,
            product_data=product_data,
            cached_at=datetime.now(),
            expires_at=expires_at,
            ttl_seconds=ttl_seconds,
            cache_status="fresh"
        )
        return await database.execute(insert_query)


async def mark_cache_accessed(cache_id: int):
    """记录缓存访问（用于统计）"""
    query = products_cache.update().where(
        products_cache.c.id == cache_id
    ).values(
        access_count=products_cache.c.access_count + 1,
        last_accessed_at=datetime.now()
    )
    await database.execute(query)


async def cleanup_expired_cache():
    """清理过期缓存（定时任务）"""
    query = products_cache.delete().where(
        products_cache.c.expires_at < datetime.now()
    )
    deleted = await database.execute(query)
    logger.info(f"🗑️ Cleaned up {deleted} expired cache entries")
    return deleted


# ============================================================================
# EVENT OPERATIONS (事件层操作 - 只追加)
# ============================================================================

async def log_api_call(
    event_type: str,
    merchant_id: str,
    endpoint: str,
    request_params: Optional[Dict] = None,
    response_status: Optional[int] = None,
    cache_hit: bool = False,
    response_time_ms: Optional[int] = None,
    product_ids: Optional[List[str]] = None,
    order_id: Optional[str] = None
):
    """
    记录 API 调用事件（只追加，永不修改）
    """
    query = api_call_events.insert().values(
        event_type=event_type,
        merchant_id=merchant_id,
        endpoint=endpoint,
        request_params=request_params,
        response_status=response_status,
        cache_hit=cache_hit,
        response_time_ms=response_time_ms,
        product_ids=product_ids,
        order_id=order_id
    )
    await database.execute(query)


async def log_order_event(
    event_type: str,
    merchant_id: str,
    order_id: str,
    product_ids: Optional[List[str]] = None,
    total_amount: Optional[float] = None,
    currency: str = "USD",
    payment_method: Optional[str] = None,
    status: Optional[str] = None,
    error_message: Optional[str] = None,
    metadata: Optional[Dict] = None
):
    """
    记录订单事件（只追加，永不修改）
    """
    query = order_events.insert().values(
        event_type=event_type,
        merchant_id=merchant_id,
        order_id=order_id,
        product_ids=product_ids,
        total_amount=total_amount,
        currency=currency,
        payment_method=payment_method,
        status=status,
        error_message=error_message,
        metadata=metadata
    )
    await database.execute(query)


# ============================================================================
# ANALYTICS OPERATIONS (分析层操作 - 定期计算)
# ============================================================================

async def calculate_merchant_analytics(merchant_id: str, days: int = 30):
    """
    计算商户分析指标（定时任务调用）
    从事件表聚合数据，不直接查询核心表
    """
    from sqlalchemy import select, func as sqlfunc
    
    window_start = datetime.now() - timedelta(days=days)
    window_end = datetime.now()
    
    # API 调用统计
    api_stats = await database.fetch_one(
        select(
            sqlfunc.count(api_call_events.c.id).label("total_calls"),
            sqlfunc.sum(sqlfunc.cast(api_call_events.c.cache_hit, Integer)).label("cache_hits"),
            sqlfunc.avg(api_call_events.c.response_time_ms).label("avg_response_time")
        ).where(
            (api_call_events.c.merchant_id == merchant_id) &
            (api_call_events.c.created_at >= window_start)
        )
    )
    
    # 订单统计
    order_stats = await database.fetch_one(
        select(
            sqlfunc.count(order_events.c.id).label("total_orders"),
            sqlfunc.sum(sqlfunc.case((order_events.c.status == "succeeded", 1), else_=0)).label("successful_orders"),
            sqlfunc.sum(sqlfunc.case((order_events.c.status == "failed", 1), else_=0)).label("failed_orders"),
            sqlfunc.sum(order_events.c.total_amount).label("total_revenue")
        ).where(
            (order_events.c.merchant_id == merchant_id) &
            (order_events.c.event_type == "payment_attempted") &
            (order_events.c.created_at >= window_start)
        )
    )
    
    total_calls = api_stats["total_calls"] or 0
    cache_hits = api_stats["cache_hits"] or 0
    total_orders = order_stats["total_orders"] or 0
    successful_orders = order_stats["successful_orders"] or 0
    
    analytics_data = {
        "merchant_id": merchant_id,
        "total_api_calls": total_calls,
        "cache_hit_rate": (cache_hits / total_calls * 100) if total_calls > 0 else 0.0,
        "avg_response_time_ms": float(api_stats["avg_response_time"] or 0),
        "total_orders": total_orders,
        "successful_orders": successful_orders,
        "failed_orders": order_stats["failed_orders"] or 0,
        "conversion_rate": (total_orders / total_calls * 100) if total_calls > 0 else 0.0,
        "payment_success_rate": (successful_orders / total_orders * 100) if total_orders > 0 else 0.0,
        "total_revenue": float(order_stats["total_revenue"] or 0),
        "window_start": window_start,
        "window_end": window_end,
    }
    
    # Upsert
    existing = await database.fetch_one(
        merchant_analytics.select().where(merchant_analytics.c.merchant_id == merchant_id)
    )
    
    if existing:
        await database.execute(
            merchant_analytics.update().where(
                merchant_analytics.c.merchant_id == merchant_id
            ).values(**analytics_data)
        )
    else:
        await database.execute(merchant_analytics.insert().values(**analytics_data))
    
    logger.info(f"📊 Updated analytics for merchant {merchant_id}")
