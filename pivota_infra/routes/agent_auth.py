"""
Agent 认证中间件
处理 API Key 验证、速率限制和使用追踪
"""

from fastapi import HTTPException, Security, Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.security import APIKeyHeader
from typing import Optional, Dict, Any
import time
from datetime import datetime

from db.agents import (
    get_agent_by_key,
    check_rate_limit,
    check_daily_quota,
    log_agent_usage,
    update_agent_stats
)
from utils.logger import logger


# API Key Header
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
bearer_scheme = HTTPBearer(auto_error=False)


class AgentContext:
    """Agent 请求上下文"""
    def __init__(self, agent: Dict[str, Any], request: Request):
        self.agent = agent
        self.agent_id = agent["agent_id"]
        self.agent_name = agent["agent_name"]
        self.allowed_merchants = agent.get("allowed_merchants")
        self.request = request
        self.start_time = time.time()
        self.request_id = None
        
    @property
    def response_time_ms(self) -> int:
        """计算响应时间（毫秒）"""
        return int((time.time() - self.start_time) * 1000)
    
    def can_access_merchant(self, merchant_id: str) -> bool:
        """检查是否可以访问指定商户"""
        if self.allowed_merchants is None:
            return True  # null = 允许访问所有商户
        return merchant_id in self.allowed_merchants


async def get_agent_context(
    request: Request,
    api_key: Optional[str] = Security(api_key_header),
    bearer: Optional[HTTPAuthorizationCredentials] = Security(bearer_scheme)
) -> AgentContext:
    """
    验证 Agent API Key 并返回上下文
    
    这是主要的认证函数，所有 Agent API 都应该使用这个依赖
    """
    
    # 1. 从 X-API-Key 或 Authorization: Bearer 中提取 API Key
    if not api_key and bearer and bearer.scheme.lower() == "bearer":
        api_key = bearer.credentials

    if not api_key:
        raise HTTPException(
            status_code=401,
            detail="Missing API Key. Please provide X-API-Key header"
        )
    
    # 2. 验证 API Key 格式（支持 ak_live_ 前缀）
    # 严格校验：ak_<64hex> 或 ak_live_<64hex>
    import re
    if not re.match(r"^ak_(live_)?[0-9a-f]{64}$", api_key):
        raise HTTPException(
            status_code=401,
            detail="Invalid API Key format"
        )
    
    # 3. 查找 Agent
    agent = await get_agent_by_key(api_key)
    if not agent:
        logger.warning(f"Invalid API key attempted: {api_key[:10]}...")
        raise HTTPException(
            status_code=401,
            detail="Invalid API Key"
        )
    
    # 4. 检查是否激活
    if not agent.get("is_active", True):
        raise HTTPException(
            status_code=403,
            detail="Agent is deactivated"
        )
    
    # 5. 检查速率限制
    rate_ok, current, limit = await check_rate_limit(agent["agent_id"])
    if not rate_ok:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded: {current}/{limit} requests per minute",
            headers={
                "X-RateLimit-Limit": str(limit),
                "X-RateLimit-Remaining": str(max(0, limit - current)),
                "X-RateLimit-Reset": str(int(time.time()) + 60)
            }
        )
    
    # 6. 检查每日配额
    quota_ok, used, quota = await check_daily_quota(agent["agent_id"])
    if not quota_ok:
        raise HTTPException(
            status_code=429,
            detail=f"Daily quota exceeded: {used}/{quota} requests",
            headers={
                "X-Quota-Limit": str(quota),
                "X-Quota-Remaining": str(max(0, quota - used)),
                "X-Quota-Reset": "00:00 UTC"
            }
        )
    
    # 7. 创建上下文
    context = AgentContext(agent, request)
    
    # 8. 更新使用统计（异步，不阻塞）
    await update_agent_stats(agent["agent_id"], increment_requests=1)
    
    logger.info(f"Agent {agent['agent_name']} authenticated for {request.url.path}")
    
    return context


async def require_merchant_access(
    merchant_id: str,
    context: AgentContext = Depends(get_agent_context)
) -> AgentContext:
    """
    验证 Agent 是否可以访问指定商户
    
    使用方式：
    ```python
    @router.get("/merchant/{merchant_id}/products")
    async def get_products(
        merchant_id: str,
        context: AgentContext = Depends(lambda m_id=merchant_id, ctx=Depends(get_agent_context): require_merchant_access(m_id, ctx))
    ):
        ...
    ```
    """
    if not context.can_access_merchant(merchant_id):
        raise HTTPException(
            status_code=403,
            detail=f"Agent not authorized to access merchant {merchant_id}"
        )
    return context


async def log_agent_request(
    context: AgentContext,
    status_code: int,
    merchant_id: Optional[str] = None,
    order_id: Optional[str] = None,
    order_amount: Optional[float] = None,
    error_message: Optional[str] = None
):
    """记录 Agent 请求（通常在响应后调用）"""
    
    # 获取请求信息
    request = context.request
    
    # 记录使用日志
    request_id = await log_agent_usage(
        agent_id=context.agent_id,
        endpoint=str(request.url.path),
        method=request.method,
        status_code=status_code,
        response_time_ms=context.response_time_ms,
        merchant_id=merchant_id,
        order_id=order_id,
        order_amount=order_amount,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        error_message=error_message
    )
    
    context.request_id = request_id
    
    # 如果创建了订单，更新统计
    if order_id and status_code < 400:
        await update_agent_stats(
            context.agent_id,
            increment_orders=1,
            add_gmv=order_amount or 0
        )
    
    return request_id


# ============================================================================
# 速率限制装饰器（可选使用）
# ============================================================================

from functools import wraps
from typing import Callable


def rate_limited(max_calls: int = 10, window_seconds: int = 60):
    """
    装饰器：对特定端点添加额外的速率限制
    
    使用方式：
    ```python
    @router.post("/expensive-operation")
    @rate_limited(max_calls=5, window_seconds=60)
    async def expensive_operation(context: AgentContext = Depends(get_agent_context)):
        ...
    ```
    """
    def decorator(func: Callable) -> Callable:
        # 存储每个 agent 的调用记录
        call_records: Dict[str, list] = {}
        
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # 获取 context（假设它在 kwargs 中）
            context = kwargs.get("context")
            if not context or not isinstance(context, AgentContext):
                return await func(*args, **kwargs)
            
            agent_id = context.agent_id
            now = time.time()
            
            # 清理过期记录
            if agent_id in call_records:
                call_records[agent_id] = [
                    t for t in call_records[agent_id] 
                    if now - t < window_seconds
                ]
            else:
                call_records[agent_id] = []
            
            # 检查是否超限
            if len(call_records[agent_id]) >= max_calls:
                raise HTTPException(
                    status_code=429,
                    detail=f"Rate limit for this operation: {max_calls} calls per {window_seconds} seconds"
                )
            
            # 记录本次调用
            call_records[agent_id].append(now)
            
            # 执行原函数
            return await func(*args, **kwargs)
        
        return wrapper
    return decorator
