"""
Agent 管理数据库模型
处理 AI Agent 的注册、认证和使用追踪
"""

from sqlalchemy import Table, Column, Integer, String, Text, DateTime, JSON, Boolean, Numeric
from sqlalchemy.sql import func
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
import secrets
import hashlib

from db.database import metadata, database


# ============================================================================
# Agent 表
# ============================================================================

agents = Table(
    "agents",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("agent_id", String(50), unique=True, index=True, nullable=False),
    Column("agent_name", String(255), nullable=False),
    Column("agent_type", String(50), nullable=False),  # chatbot, voice_assistant, custom
    Column("description", Text),
    
    # 认证
    Column("api_key", String(100), unique=True, index=True, nullable=False),
    Column("api_key_hash", String(255), nullable=False),  # SHA256 hash for verification
    Column("is_active", Boolean, default=True),
    
    # 权限和限制
    Column("allowed_merchants", JSON),  # 允许访问的商户列表，null = 全部
    Column("rate_limit", Integer, default=100),  # 每分钟请求数
    Column("daily_quota", Integer, default=10000),  # 每日配额
    
    # 统计
    Column("total_requests", Integer, default=0),
    Column("total_orders", Integer, default=0),
    Column("total_gmv", Numeric(12, 2), default=0),  # Gross Merchandise Value
    Column("success_rate", Numeric(5, 2), default=0),  # 成功率百分比
    
    # 元数据
    Column("owner_email", String(255)),
    Column("webhook_url", String(500)),  # 事件通知 URL
    Column("metadata", JSON),  # 额外配置
    
    # 时间戳
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), onupdate=func.now()),
    Column("last_used_at", DateTime(timezone=True))
)


# ============================================================================
# Agent 使用日志
# ============================================================================

agent_usage_logs = Table(
    "agent_usage_logs",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("agent_id", String(50), index=True, nullable=False),
    Column("endpoint", String(255), nullable=False),
    Column("method", String(10), nullable=False),
    
    # 请求详情
    Column("merchant_id", String(50), index=True),
    Column("request_id", String(100), unique=True),
    Column("ip_address", String(50)),
    Column("user_agent", Text),
    
    # 响应
    Column("status_code", Integer),
    Column("response_time_ms", Integer),  # 响应时间（毫秒）
    Column("error_message", Text),
    
    # 业务数据
    Column("order_id", String(50)),
    Column("order_amount", Numeric(10, 2)),
    
    Column("timestamp", DateTime(timezone=True), server_default=func.now(), index=True)
)


# ============================================================================
# CRUD 操作
# ============================================================================

async def create_agent(
    agent_name: str,
    agent_type: str,
    owner_email: Optional[str] = None,
    description: Optional[str] = None,
    rate_limit: int = 100,
    daily_quota: int = 10000,
    allowed_merchants: Optional[List[str]] = None,
    webhook_url: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """创建新的 Agent"""
    
    # 生成唯一 ID 和 API Key
    agent_id = f"agent_{secrets.token_hex(8)}"
    api_key = f"ak_{secrets.token_hex(32)}"  # ak_开头的64字符密钥
    api_key_hash = hashlib.sha256(api_key.encode()).hexdigest()
    
    query = agents.insert().values(
        agent_id=agent_id,
        agent_name=agent_name,
        agent_type=agent_type,
        description=description,
        api_key=api_key,
        api_key_hash=api_key_hash,
        is_active=True,  # Explicitly set to active
        owner_email=owner_email,
        rate_limit=rate_limit,
        daily_quota=daily_quota,
        allowed_merchants=allowed_merchants,
        webhook_url=webhook_url,
        metadata=metadata or {}
    )
    
    await database.execute(query)
    
    return {
        "agent_id": agent_id,
        "agent_name": agent_name,
        "api_key": api_key,  # 仅在创建时返回一次
        "message": "Please save the API key securely. It won't be shown again."
    }


async def get_agent_by_key(api_key: str) -> Optional[Dict[str, Any]]:
    """通过 API Key 获取 Agent（用于认证）"""
    # Use raw SQL to be resilient to schema differences between historical and current definitions
    result = await database.fetch_one("SELECT * FROM agents WHERE api_key = :api_key LIMIT 1", {"api_key": api_key})
    if not result:
        return None
    agent = dict(result)
    # Normalize field names across schemas
    # Map name -> agent_name if needed
    if "agent_name" not in agent and "name" in agent:
        agent["agent_name"] = agent["name"]
    # Derive is_active from status if not present
    if "is_active" not in agent:
        status = agent.get("status")
        agent["is_active"] = (str(status).lower() == "active") if status is not None else True
    # Ensure allowed_merchants exists (can be None)
    agent.setdefault("allowed_merchants", agent.get("allowed_merchants"))
    return agent


async def get_agent(agent_id: str) -> Optional[Dict[str, Any]]:
    """获取 Agent 信息（不含 API Key）"""
    query = agents.select().where(agents.c.agent_id == agent_id)
    result = await database.fetch_one(query)
    
    if result:
        agent = dict(result)
        # 移除敏感信息
        agent.pop("api_key", None)
        agent.pop("api_key_hash", None)
        return agent
    return None


async def update_agent_stats(
    agent_id: str,
    increment_requests: int = 0,
    increment_orders: int = 0,
    add_gmv: float = 0
):
    """更新 Agent 统计数据"""
    query = f"""
        UPDATE agents 
        SET total_requests = total_requests + :inc_req,
            total_orders = total_orders + :inc_orders,
            total_gmv = total_gmv + :add_gmv,
            last_used_at = :now
        WHERE agent_id = :agent_id
    """
    
    await database.execute(
        query,
        {
            "inc_req": increment_requests,
            "inc_orders": increment_orders,
            "add_gmv": add_gmv,
            "now": datetime.utcnow(),
            "agent_id": agent_id
        }
    )


async def log_agent_usage(
    agent_id: str,
    endpoint: str,
    method: str,
    status_code: int,
    response_time_ms: int,
    merchant_id: Optional[str] = None,
    order_id: Optional[str] = None,
    order_amount: Optional[float] = None,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    error_message: Optional[str] = None
) -> str:
    """记录 Agent API 使用"""
    request_id = f"req_{secrets.token_hex(16)}"
    
    query = agent_usage_logs.insert().values(
        agent_id=agent_id,
        endpoint=endpoint,
        method=method,
        merchant_id=merchant_id,
        request_id=request_id,
        ip_address=ip_address,
        user_agent=user_agent,
        status_code=status_code,
        response_time_ms=response_time_ms,
        error_message=error_message,
        order_id=order_id,
        order_amount=order_amount
    )
    
    await database.execute(query)
    return request_id


async def check_rate_limit(agent_id: str) -> tuple[bool, int, int]:
    """
    检查 Agent 是否超过速率限制
    返回: (是否允许, 当前分钟内请求数, 限制数)
    """
    # 获取 Agent 配置
    agent = await get_agent(agent_id)
    if not agent:
        return False, 0, 0
    
    rate_limit = agent.get("rate_limit", 100)
    
    # 统计最近一分钟的请求数
    one_minute_ago = datetime.utcnow() - timedelta(minutes=1)
    query = f"""
        SELECT COUNT(*) as count 
        FROM agent_usage_logs 
        WHERE agent_id = :agent_id 
        AND timestamp > :since
    """
    
    result = await database.fetch_one(
        query,
        {"agent_id": agent_id, "since": one_minute_ago}
    )
    
    current_count = result["count"] if result else 0
    
    return current_count < rate_limit, current_count, rate_limit


async def check_daily_quota(agent_id: str) -> tuple[bool, int, int]:
    """
    检查 Agent 是否超过每日配额
    返回: (是否允许, 今日使用量, 配额)
    """
    agent = await get_agent(agent_id)
    if not agent:
        return False, 0, 0
    
    daily_quota = agent.get("daily_quota", 10000)
    
    # 统计今天的请求数
    today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    query = f"""
        SELECT COUNT(*) as count 
        FROM agent_usage_logs 
        WHERE agent_id = :agent_id 
        AND timestamp >= :today
    """
    
    result = await database.fetch_one(
        query,
        {"agent_id": agent_id, "today": today}
    )
    
    today_count = result["count"] if result else 0
    
    return today_count < daily_quota, today_count, daily_quota


async def get_agent_analytics(
    agent_id: str,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None
) -> Dict[str, Any]:
    """获取 Agent 分析数据"""
    
    if not start_date:
        start_date = datetime.utcnow() - timedelta(days=30)
    if not end_date:
        end_date = datetime.utcnow()
    
    # 基础统计
    stats_query = f"""
        SELECT 
            COUNT(DISTINCT request_id) as total_requests,
            COUNT(DISTINCT order_id) as total_orders,
            SUM(order_amount) as total_gmv,
            AVG(response_time_ms) as avg_response_time,
            COUNT(CASE WHEN status_code >= 400 THEN 1 END) as error_count
        FROM agent_usage_logs
        WHERE agent_id = :agent_id
        AND timestamp BETWEEN :start_date AND :end_date
    """
    
    stats = await database.fetch_one(
        stats_query,
        {
            "agent_id": agent_id,
            "start_date": start_date,
            "end_date": end_date
        }
    )
    
    # 按天统计
    daily_query = f"""
        SELECT 
            DATE(timestamp) as date,
            COUNT(*) as requests,
            COUNT(DISTINCT order_id) as orders,
            SUM(order_amount) as gmv
        FROM agent_usage_logs
        WHERE agent_id = :agent_id
        AND timestamp BETWEEN :start_date AND :end_date
        GROUP BY DATE(timestamp)
        ORDER BY date DESC
    """
    
    daily_stats = await database.fetch_all(
        daily_query,
        {
            "agent_id": agent_id,
            "start_date": start_date,
            "end_date": end_date
        }
    )
    
    # 热门端点
    endpoint_query = f"""
        SELECT 
            endpoint,
            COUNT(*) as count,
            AVG(response_time_ms) as avg_time
        FROM agent_usage_logs
        WHERE agent_id = :agent_id
        AND timestamp BETWEEN :start_date AND :end_date
        GROUP BY endpoint
        ORDER BY count DESC
        LIMIT 10
    """
    
    top_endpoints = await database.fetch_all(
        endpoint_query,
        {
            "agent_id": agent_id,
            "start_date": start_date,
            "end_date": end_date
        }
    )
    
    return {
        "agent_id": agent_id,
        "period": {
            "start": start_date.isoformat(),
            "end": end_date.isoformat()
        },
        "summary": dict(stats) if stats else {},
        "daily_stats": [dict(d) for d in daily_stats],
        "top_endpoints": [dict(e) for e in top_endpoints],
        "success_rate": (
            100 * (1 - (stats["error_count"] / stats["total_requests"]))
            if stats and stats["total_requests"] > 0 else 0
        )
    }
