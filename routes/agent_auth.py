"""
Agent 认证中间件
处理 API Key 验证、速率限制和使用追踪
"""

from fastapi import HTTPException, Security, Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.security import APIKeyHeader
from typing import Optional, Dict, Any
import asyncio
import time
from datetime import datetime

from config.platform import pytest_bypass_allowed
from db.agents import (
    get_agent_by_key,
    AgentAuthLookupTransientError,
    get_agent,
    check_rate_limit,
    check_daily_quota,
    log_agent_usage,
    update_agent_stats
)
from utils.logger import logger
from utils.transient_errors import db_busy_http_exception
import os
import base64
import hashlib
import hmac
import json


# API Key Header
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
checkout_token_header = APIKeyHeader(name="X-Checkout-Token", auto_error=False)
bearer_scheme = HTTPBearer(auto_error=False)

_INTERNAL_TRUSTED_KEY_ENV_NAMES = (
    "SHOP_GATEWAY_AGENT_API_KEY",
    "PIVOTA_API_KEY",
    "PIVOTA_BACKEND_AGENT_API_KEY",
    "PIVOTA_AGENT_API_KEY",
)


def _parse_internal_trusted_api_keys() -> tuple[str, ...]:
    keys: list[str] = []
    for env_name in _INTERNAL_TRUSTED_KEY_ENV_NAMES:
        value = str(os.getenv(env_name, "") or "").strip()
        if value:
            keys.append(value)
    extra_keys = str(os.getenv("AGENT_INTERNAL_TRUSTED_API_KEYS", "") or "").strip()
    if extra_keys:
        for token in extra_keys.replace("\n", ",").split(","):
            key = str(token or "").strip()
            if key:
                keys.append(key)
    deduped: list[str] = []
    for key in keys:
        if key not in deduped:
            deduped.append(key)
    return tuple(deduped)


_INTERNAL_TRUSTED_API_KEYS = _parse_internal_trusted_api_keys()


def _is_internal_trusted_api_key(api_key: Optional[str]) -> bool:
    candidate = str(api_key or "").strip()
    if not candidate or not _INTERNAL_TRUSTED_API_KEYS:
        return False
    for trusted_key in _INTERNAL_TRUSTED_API_KEYS:
        if hmac.compare_digest(candidate, trusted_key):
            return True
    return False


def _build_internal_trusted_agent(api_key: str) -> Dict[str, Any]:
    key_hash = hashlib.sha256(str(api_key or "").encode("utf-8")).hexdigest()[:16]
    return {
        "agent_id": f"agent_internal_trusted_{key_hash}",
        "agent_name": "Internal Trusted Agent",
        "allowed_merchants": None,
        "is_active": True,
        "metadata": {
            "auth_source": "internal_trusted_key",
            "api_key_hash": key_hash,
        },
    }


class AgentContext:
    """Agent 请求上下文"""
    def __init__(self, agent: Dict[str, Any], request: Request):
        self.agent = agent
        self.agent_id = agent["agent_id"]
        # Fallback: use 'name' if 'agent_name' is NULL
        self.agent_name = agent.get("agent_name") or agent.get("name") or "Unknown"
        raw_allowed_merchants = agent.get("allowed_merchants")
        allowed_merchants: Optional[list[str]] = None
        if raw_allowed_merchants is None:
            allowed_merchants = None
        elif isinstance(raw_allowed_merchants, list):
            allowed_merchants = [str(m).strip() for m in raw_allowed_merchants if str(m or "").strip()] or []
        elif isinstance(raw_allowed_merchants, str):
            try:
                parsed = json.loads(raw_allowed_merchants)
            except Exception:
                parsed = None
            if parsed is None:
                allowed_merchants = None
            elif isinstance(parsed, list):
                allowed_merchants = [str(m).strip() for m in parsed if str(m or "").strip()] or []
            else:
                # Fail closed if the DB contains an unexpected type.
                allowed_merchants = []
        else:
            allowed_merchants = []

        self.allowed_merchants = allowed_merchants
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
    bearer: Optional[HTTPAuthorizationCredentials] = Security(bearer_scheme),
    checkout_token: Optional[str] = Security(checkout_token_header),
) -> AgentContext:
    """
    验证 Agent API Key 并返回上下文
    
    这是主要的认证函数，所有 Agent API 都应该使用这个依赖
    """
    
    dep_started = time.perf_counter()

    # 0. Checkout token auth (preferred for embedded checkout / shared UI)
    if checkout_token:
        context = await _get_agent_context_from_checkout_token(request, checkout_token)

        # Apply the same rate/quota guards at the agent_id level.
        rate_started = time.perf_counter()
        rate_ok, current, limit = await check_rate_limit(
            context.agent_id,
            rate_limit=(context.agent.get("rate_limit") if isinstance(context.agent, dict) else None),
        )
        try:
            request.state.agent_rate_limit_check_ms = max(0, int((time.perf_counter() - rate_started) * 1000))
        except Exception:
            pass
        if not rate_ok:
            raise HTTPException(
                status_code=429,
                detail=f"Rate limit exceeded: {current}/{limit} requests per minute",
                headers={
                    "X-RateLimit-Limit": str(limit),
                    "X-RateLimit-Remaining": str(max(0, limit - current)),
                    "X-RateLimit-Reset": str(int(time.time()) + 60),
                },
            )

        quota_started = time.perf_counter()
        quota_ok, used, quota = await check_daily_quota(
            context.agent_id,
            daily_quota=(context.agent.get("daily_quota") if isinstance(context.agent, dict) else None),
        )
        try:
            request.state.agent_daily_quota_check_ms = max(0, int((time.perf_counter() - quota_started) * 1000))
        except Exception:
            pass
        if not quota_ok:
            raise HTTPException(
                status_code=429,
                detail=f"Daily quota exceeded: {used}/{quota} requests",
                headers={
                    "X-Quota-Limit": str(quota),
                    "X-Quota-Remaining": str(max(0, quota - used)),
                    "X-Quota-Reset": "00:00 UTC",
                },
            )

        # Best-effort stats update; do not block the request on a hot row lock.
        try:
            asyncio.create_task(update_agent_stats(context.agent_id, increment_requests=1))
        except Exception:
            pass
        try:
            request.state.agent_id = context.agent_id
            # The ENFORCED limit for this agent, for RateLimitMiddleware to
            # publish as X-RateLimit-*. Recorded only on the authenticated path:
            # the middleware runs before auth and must not invent a number for
            # callers who never got here. See middleware/rate_limiter.py.
            request.state.agent_authenticated = True
            request.state.agent_rate_limit_limit = limit
            request.state.agent_rate_limit_used = current
            request.state.agent_dependency_total_ms = max(0, int((time.perf_counter() - dep_started) * 1000))
        except Exception:
            pass
        logger.info(f"Agent {context.agent_name} authenticated via checkout token for {request.url.path}")
        return context

    # 1. 从 X-API-Key 或 Authorization: Bearer 中提取 API Key
    if not api_key and bearer and bearer.scheme.lower() == "bearer":
        api_key = bearer.credentials

    if not api_key:
        raise HTTPException(
            status_code=401,
            detail="Missing API Key. Please provide X-API-Key header"
        )

    # Test-only bypass: allow unit tests to use a placeholder API key without
    # hitting the database or enforcing rate/quota logic. Fails closed on ANY
    # DEPLOYED host — staging included — as well as anything resolving to
    # production; since #1900 the gate is `not (is_deployed() or
    # is_production())`, and since the Cloud Run **Jobs** markers were added it
    # covers job containers too. See config.platform.pytest_bypass_allowed.
    if (
        api_key in {"test-agent-key", "test-api-key"}
        and pytest_bypass_allowed(bypass_name="the agent test-key bypass")
    ):
        agent = {
            "agent_id": "agent_test",
            "agent_name": "Test Agent",
            "allowed_merchants": None,
            "is_active": True,
        }
        context = AgentContext(agent, request)
        try:
            request.state.agent_id = context.agent_id
            request.state.agent_authenticated = True
            request.state.agent_dependency_total_ms = max(0, int((time.perf_counter() - dep_started) * 1000))
        except Exception:
            pass
        return context

    internal_trusted_api_key = _is_internal_trusted_api_key(api_key)
    if internal_trusted_api_key:
        agent = _build_internal_trusted_agent(api_key)
        context = AgentContext(agent, request)
        try:
            request.state.agent_id = context.agent_id
            request.state.agent_auth_lookup_ms = 0
            request.state.agent_auth_cache_hit = False
            request.state.agent_authenticated = True
            request.state.agent_auth_source = "internal_trusted_key"
            request.state.agent_auth_total_ms = 0
            request.state.agent_rate_limit_check_ms = 0
            request.state.agent_daily_quota_check_ms = 0
            request.state.agent_internal_trusted_key = True
            request.state.agent_dependency_total_ms = max(0, int((time.perf_counter() - dep_started) * 1000))
        except Exception:
            pass
        logger.info(f"Internal trusted agent authenticated for {request.url.path}")
        return context

    # 2. 验证 API Key 格式（支持 ak_live_ 前缀）
    # 严格校验：ak_<64hex> 或 ak_live_<64hex>
    import re
    if not re.match(r"^ak_(live_)?[0-9a-f]{64}$", api_key):
        raise HTTPException(
            status_code=401,
            detail="Invalid API Key format"
        )

    # 3. 查找 Agent
    auth_started = time.perf_counter()
    auth_metrics: Dict[str, Any] = {}
    try:
        agent = await get_agent_by_key(api_key, metrics_out=auth_metrics)
    except AgentAuthLookupTransientError:
        logger.warning("[AgentAuth] transient DB state during API key lookup; retrying once")
        try:
            await asyncio.sleep(0.05)
        except Exception:
            pass
        try:
            agent = await get_agent_by_key(api_key, metrics_out=auth_metrics)
        except AgentAuthLookupTransientError as exc2:
            raise db_busy_http_exception() from exc2
    try:
        request.state.agent_auth_lookup_ms = max(0, int(auth_metrics.get("auth_lookup_ms") or 0))
        request.state.agent_auth_cache_hit = bool(auth_metrics.get("auth_cache_hit") or False)
        request.state.agent_auth_source = str(auth_metrics.get("auth_source") or "")
        request.state.agent_auth_total_ms = max(0, int((time.perf_counter() - auth_started) * 1000))
        if not hasattr(request.state, "db_pool_wait_ms"):
            request.state.db_pool_wait_ms = 0
    except Exception:
        pass
    if not agent:
        logger.warning(f"Invalid API key attempted: {api_key[:10]}...")
        raise HTTPException(
            status_code=401,
            detail="Invalid API Key"
        )
    
    # 4. 检查是否激活 (support both is_active and status fields)
    is_active = agent.get("is_active")
    if is_active is None:
        # Fallback to status field
        status = agent.get("status")
        is_active = (str(status).lower() == "active") if status else True
    
    if not is_active:
        raise HTTPException(
            status_code=403,
            detail="Agent is deactivated"
        )

    try:
        request.state.agent_id = agent["agent_id"]
    except Exception:
        pass

    # 5. 检查速率限制
    rate_started = time.perf_counter()
    rate_ok, current, limit = await check_rate_limit(
        agent["agent_id"],
        rate_limit=agent.get("rate_limit"),
    )
    try:
        request.state.agent_rate_limit_check_ms = max(0, int((time.perf_counter() - rate_started) * 1000))
    except Exception:
        pass
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
    quota_started = time.perf_counter()
    quota_ok, used, quota = await check_daily_quota(
        agent["agent_id"],
        daily_quota=agent.get("daily_quota"),
    )
    try:
        request.state.agent_daily_quota_check_ms = max(0, int((time.perf_counter() - quota_started) * 1000))
    except Exception:
        pass
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
    try:
        request.state.agent_id = context.agent_id
        # See the note on the checkout-token path above: this is the per-agent
        # limit actually enforced, which the middleware publishes in place of the
        # global RATE_LIMIT_RPM it used to leak to unauthenticated callers.
        request.state.agent_authenticated = True
        request.state.agent_rate_limit_limit = limit
        request.state.agent_rate_limit_used = current
        request.state.agent_dependency_total_ms = max(0, int((time.perf_counter() - dep_started) * 1000))
    except Exception:
        pass
    
    # 8. 更新使用统计（异步，不阻塞）
    try:
        asyncio.create_task(update_agent_stats(agent["agent_id"], increment_requests=1))
    except Exception:
        pass
    
    logger.info(f"Agent {agent['agent_name']} authenticated for {request.url.path}")
    
    return context


def _base64url_decode(data: str) -> bytes:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded.encode("utf-8"))


def _base64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


def _constant_time_equals(a: str, b: str) -> bool:
    try:
        return hmac.compare_digest(str(a or ""), str(b or ""))
    except Exception:
        return False


def _checkout_token_secret() -> str:
    return (os.getenv("CHECKOUT_TOKEN_SECRET") or os.getenv("AGENT_CHECKOUT_TOKEN_SECRET") or "").strip()


async def _get_agent_context_from_checkout_token(request: Request, token: str) -> AgentContext:
    raw = (token or "").strip()
    if not raw:
        raise HTTPException(status_code=401, detail="Missing checkout token")

    secret = _checkout_token_secret()
    if not secret:
        raise HTTPException(status_code=500, detail="Checkout token secret is not configured")

    # Accept either:
    # - "<payload_b64url>.<sig_b64url>"
    # - "v1.<payload_b64url>.<sig_b64url>"
    parts = raw.split(".")
    if len(parts) == 2:
        payload_b64, sig = parts
    elif len(parts) == 3 and parts[0] == "v1":
        payload_b64, sig = parts[1], parts[2]
    else:
        raise HTTPException(status_code=401, detail="Invalid checkout token format")

    expected = _base64url_encode(hmac.new(secret.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).digest())
    if not _constant_time_equals(sig, expected):
        raise HTTPException(status_code=401, detail="Invalid checkout token signature")

    try:
        payload_raw = _base64url_decode(payload_b64).decode("utf-8")
        payload = json.loads(payload_raw)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid checkout token payload")

    now = int(time.time())
    exp = int(payload.get("exp") or 0)
    if exp and now > exp:
        raise HTTPException(status_code=401, detail="Checkout token expired")

    agent_id = str(payload.get("agent_id") or "").strip()
    if not agent_id:
        raise HTTPException(status_code=401, detail="Checkout token missing agent_id")

    agent = await get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=401, detail="Invalid checkout token agent")

    # Normalize fields expected by AgentContext
    if "agent_name" not in agent and "name" in agent:
        agent["agent_name"] = agent["name"]
    if "allowed_merchants" not in agent:
        agent["allowed_merchants"] = None

    # Active check: support both is_active and status fields
    is_active = agent.get("is_active")
    if is_active is None:
        status = agent.get("status")
        is_active = (str(status).lower() == "active") if status else True
    if not is_active:
        raise HTTPException(status_code=403, detail="Agent is deactivated")

    context = AgentContext(agent, request)

    # Restrict merchant access to the token's merchant_ids (when provided).
    token_merchants = payload.get("merchant_ids")
    if isinstance(token_merchants, list):
        token_merchants_norm = [str(m).strip() for m in token_merchants if str(m or "").strip()]
        if token_merchants_norm:
            if context.allowed_merchants is None:
                context.allowed_merchants = token_merchants_norm
            else:
                allowed = set(str(m).strip() for m in (context.allowed_merchants or []) if str(m or "").strip())
                context.allowed_merchants = [m for m in token_merchants_norm if m in allowed]

    # Attach raw token payload for downstream callers (best-effort; do not rely on it for PII).
    setattr(context, "checkout_token_payload", payload)

    # Optional scope enforcement: when scopes include "checkout", allow only checkout-safe agent endpoints.
    scopes = payload.get("scopes")
    if isinstance(scopes, list) and "checkout" in scopes and "agent_api" not in scopes and "full" not in scopes:
        path = str(getattr(request, "url", None).path if getattr(request, "url", None) else request.url.path)
        allowed_prefixes = (
            "/agent/v1/quotes/",
            "/agent/v1/orders/",
            "/agent/v1/payments",
            "/agent/v1/buyers/merge",
            "/agent/v1/checkout/prefill",
        )
        if not any(path.startswith(p) for p in allowed_prefixes):
            raise HTTPException(status_code=403, detail="Checkout token not authorized for this endpoint")

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
