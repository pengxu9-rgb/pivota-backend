"""
Agent 管理数据库模型
处理 AI Agent 的注册、认证和使用追踪
"""

from sqlalchemy import Table, Column, Integer, String, Text, DateTime, JSON, Boolean, Numeric
from sqlalchemy.sql import func
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
from collections import OrderedDict
import secrets
import hashlib
import json
import os
import time

from db.database import metadata, database, IS_POSTGRES
from utils.transient_errors import is_asyncpg_busy_error

_AGENT_AUTH_CACHE_MAX_ENTRIES = max(
    100,
    min(100000, int(os.getenv("AGENT_AUTH_CACHE_MAX_ENTRIES", "20000") or 20000)),
)
_AGENT_AUTH_POSITIVE_TTL_SECONDS = max(
    1,
    min(3600, int(os.getenv("AGENT_AUTH_CACHE_POSITIVE_TTL_SECONDS", "60") or 60)),
)
_AGENT_AUTH_NEGATIVE_TTL_SECONDS = max(
    1,
    min(600, int(os.getenv("AGENT_AUTH_CACHE_NEGATIVE_TTL_SECONDS", "15") or 15)),
)
_AGENT_AUTH_ENABLE_LEGACY_API_KEY_FALLBACK = (
    str(os.getenv("AGENT_AUTH_ENABLE_LEGACY_API_KEY_FALLBACK", "false")).strip().lower()
    in {"1", "true", "yes", "on"}
)
_AGENT_AUTH_KEY_TABLE_MODE = str(
    os.getenv("AGENT_AUTH_KEY_TABLE", "auto")
).strip().lower() or "auto"
if _AGENT_AUTH_KEY_TABLE_MODE not in {"auto", "api_keys", "agent_api_keys", "legacy_only"}:
    _AGENT_AUTH_KEY_TABLE_MODE = "auto"
_AGENT_AUTH_KEY_TABLE_CACHE_TTL_SECONDS = max(
    5,
    min(3600, int(os.getenv("AGENT_AUTH_KEY_TABLE_CACHE_TTL_SECONDS", "300") or 300)),
)
_AGENT_AUTH_CACHE: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
_AGENT_AUTH_KEY_TABLE_CACHE: Dict[str, Any] = {"table": None, "expires_at": 0.0}


class AgentAuthLookupTransientError(RuntimeError):
    """Raised when agent key lookup fails due to transient DB/pool state."""


def _agent_auth_cache_key(api_key: str) -> str:
    return hashlib.sha256(str(api_key or "").encode("utf-8")).hexdigest()


def _normalize_agent_row(result: Any) -> Dict[str, Any]:
    agent = dict(result)
    raw_allowed = agent.get("allowed_merchants")
    if isinstance(raw_allowed, str):
        try:
            raw_allowed = json.loads(raw_allowed)
        except Exception:
            raw_allowed = []
    if raw_allowed is None:
        agent["allowed_merchants"] = None
    elif isinstance(raw_allowed, list):
        agent["allowed_merchants"] = [str(m).strip() for m in raw_allowed if str(m or "").strip()] or []
    else:
        agent["allowed_merchants"] = []

    if "agent_name" not in agent and "name" in agent:
        agent["agent_name"] = agent["name"]

    if "is_active" not in agent:
        status = agent.get("status")
        agent["is_active"] = (str(status).lower() == "active") if status is not None else True

    if "allowed_merchants" not in agent:
        agent["allowed_merchants"] = None
    return agent


def _get_cached_agent_auth(cache_key: str) -> tuple[bool, Optional[Dict[str, Any]]]:
    now = time.monotonic()
    entry = _AGENT_AUTH_CACHE.get(cache_key)
    if not isinstance(entry, dict):
        return False, None
    expires_at = float(entry.get("expires_at") or 0)
    if expires_at <= now:
        _AGENT_AUTH_CACHE.pop(cache_key, None)
        return False, None
    _AGENT_AUTH_CACHE.move_to_end(cache_key)
    if entry.get("found") is False:
        return True, None
    cached_agent = entry.get("agent")
    if not isinstance(cached_agent, dict):
        return True, None
    return True, dict(cached_agent)


def _put_cached_agent_auth(cache_key: str, agent: Optional[Dict[str, Any]]) -> None:
    ttl = _AGENT_AUTH_POSITIVE_TTL_SECONDS if agent else _AGENT_AUTH_NEGATIVE_TTL_SECONDS
    _AGENT_AUTH_CACHE[cache_key] = {
        "expires_at": time.monotonic() + max(1, int(ttl or 1)),
        "found": bool(agent),
        "agent": dict(agent) if isinstance(agent, dict) else None,
    }
    _AGENT_AUTH_CACHE.move_to_end(cache_key)
    while len(_AGENT_AUTH_CACHE) > _AGENT_AUTH_CACHE_MAX_ENTRIES:
        _AGENT_AUTH_CACHE.popitem(last=False)


def _clear_auth_key_table_cache() -> None:
    _AGENT_AUTH_KEY_TABLE_CACHE["table"] = None
    _AGENT_AUTH_KEY_TABLE_CACHE["expires_at"] = 0.0


def _is_missing_table_error(exc: Exception, table_name: str) -> bool:
    msg = str(exc or "").lower()
    table = str(table_name or "").strip().lower()
    if not table:
        return False
    return table in msg and (
        "does not exist" in msg
        or "undefined table" in msg
        or "relation" in msg
        or "no such table" in msg
    )


async def _resolve_auth_key_table() -> Optional[str]:
    mode = _AGENT_AUTH_KEY_TABLE_MODE
    if mode in {"api_keys", "agent_api_keys"}:
        return mode
    if mode == "legacy_only":
        return None
    if not IS_POSTGRES:
        return None

    now = time.monotonic()
    expires_at = float(_AGENT_AUTH_KEY_TABLE_CACHE.get("expires_at") or 0)
    if expires_at > now:
        cached = _AGENT_AUTH_KEY_TABLE_CACHE.get("table")
        return str(cached) if isinstance(cached, str) and cached else None

    table_name: Optional[str] = None
    try:
        row = await database.fetch_one(
            """
            SELECT
              to_regclass('public.api_keys') AS api_keys_table,
              to_regclass('public.agent_api_keys') AS agent_api_keys_table
            """
        )
        row_dict = dict(row or {})
        has_api_keys = bool(row_dict.get("api_keys_table"))
        has_agent_api_keys = bool(row_dict.get("agent_api_keys_table"))
        if has_api_keys:
            table_name = "api_keys"
        elif has_agent_api_keys:
            table_name = "agent_api_keys"
    except Exception:
        table_name = None

    _AGENT_AUTH_KEY_TABLE_CACHE["table"] = table_name
    _AGENT_AUTH_KEY_TABLE_CACHE["expires_at"] = now + float(_AGENT_AUTH_KEY_TABLE_CACHE_TTL_SECONDS)
    return table_name


async def _lookup_agent_from_key_table(
    *,
    api_key: str,
    key_hash_sha256: str,
) -> tuple[Optional[Dict[str, Any]], str, Optional[str]]:
    key_table = await _resolve_auth_key_table()
    if key_table == "api_keys":
        row = await database.fetch_one(
            """
            SELECT a.*
            FROM agents a
            JOIN api_keys k ON k.agent_id = a.agent_id
            WHERE k.key_hash = :key_hash AND k.status = 'active'
            LIMIT 1
            """,
            {"key_hash": key_hash_sha256},
        )
        return (dict(row) if row else None, "api_keys", key_table)

    if key_table == "agent_api_keys":
        key_hash_md5 = hashlib.md5(str(api_key or "").encode("utf-8")).hexdigest()
        row = await database.fetch_one(
            """
            SELECT
              a.*,
              CASE
                WHEN k.key_hash = :key_hash_sha256 THEN 'sha256'
                WHEN k.key_hash = :key_hash_md5 THEN 'md5'
                ELSE 'unknown'
              END AS _auth_hash_alg
            FROM agents a
            JOIN agent_api_keys k ON k.agent_id = a.agent_id
            WHERE (k.key_hash = :key_hash_sha256 OR k.key_hash = :key_hash_md5)
              AND COALESCE(k.is_active, TRUE) = TRUE
            ORDER BY
              CASE WHEN k.key_hash = :key_hash_sha256 THEN 0 ELSE 1 END,
              k.id DESC
            LIMIT 1
            """,
            {
                "key_hash_sha256": key_hash_sha256,
                "key_hash_md5": key_hash_md5,
            },
        )
        if not row:
            return (None, "agent_api_keys", key_table)
        row_dict = dict(row)
        hash_alg = str(row_dict.pop("_auth_hash_alg", "")).strip().lower()
        if hash_alg in {"sha256", "md5"}:
            return (row_dict, f"agent_api_keys_{hash_alg}", key_table)
        return (row_dict, "agent_api_keys", key_table)

    return (None, "none", key_table)


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
    Column("caller_id", String(128), index=True),
    Column("source_channel", String(128), index=True),
    Column("source_family", String(64), index=True),
    Column("query_source", String(128), index=True),
    Column("protocol_name", String(64), index=True),
    Column("commerce_surface", String(64), index=True),
    Column("llm_provider", String(64), index=True),
    Column("llm_model", String(128), index=True),
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


async def get_agent_by_did(did: str) -> Optional[Dict[str, Any]]:
    """
    Look up an agent by its AP2 DID (agents.did, migration 183 / ADR-012).

    The DID is the agent's self-sovereign external identity on the signed rail;
    this maps it back to the internal agent row (for x402_enabled, wallet
    binding, and rate/quota limits). Returns the row as a dict, or None if no
    agent claims this DID. ``agent_id`` remains the internal identifier.
    """
    if not did:
        return None
    row = await database.fetch_one(
        "SELECT * FROM agents WHERE did = :did",
        {"did": did},
    )
    return dict(row) if row else None


async def set_agent_did(agent_id: str, did: str) -> None:
    """
    Associate a DID with an existing agent (agents.did). Overwrites any prior
    value (DID rotation for that agent). The DID is unique across agents
    (partial unique index, migration 183), so a DID already claimed by another
    agent raises a uniqueness error at the DB layer — callers must handle it.
    """
    await database.execute(
        "UPDATE agents SET did = :did, updated_at = NOW() WHERE agent_id = :agent_id",
        {"did": did, "agent_id": agent_id},
    )


async def get_agent_by_key(api_key: str, metrics_out: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """通过 API Key 获取 Agent（用于认证）

    支持两种来源：
    1) api_keys.key_hash（默认主路径）
    2) agents.api_key（legacy 兜底，可通过 AGENT_AUTH_ENABLE_LEGACY_API_KEY_FALLBACK 开关）
    """
    started = time.perf_counter()
    metrics = metrics_out if isinstance(metrics_out, dict) else None
    if metrics is not None:
        metrics.setdefault("auth_lookup_ms", 0)
        metrics.setdefault("auth_cache_hit", False)
        metrics.setdefault("auth_source", "none")

    key_hash = _agent_auth_cache_key(api_key)
    cache_hit, cached_agent = _get_cached_agent_auth(key_hash)
    if cache_hit:
        if metrics is not None:
            metrics["auth_lookup_ms"] = int((time.perf_counter() - started) * 1000)
            metrics["auth_cache_hit"] = True
            metrics["auth_source"] = "cache_hit" if cached_agent else "cache_miss_cached"
        return cached_agent

    try:
        result, source, key_table = await _lookup_agent_from_key_table(
            api_key=api_key,
            key_hash_sha256=key_hash,
        )

        # Optional/auto legacy fallback.
        should_try_legacy = bool(_AGENT_AUTH_ENABLE_LEGACY_API_KEY_FALLBACK)
        if key_table is None:
            # Compatibility: when no hash-key table is available, fall back to agents.api_key
            # to avoid per-request exceptions and auth regressions during schema drift.
            should_try_legacy = True
        if not result and should_try_legacy:
            legacy = await database.fetch_one(
                "SELECT * FROM agents WHERE api_key = :api_key LIMIT 1",
                {"api_key": api_key},
            )
            if legacy:
                result = dict(legacy)
                source = "legacy_fallback" if _AGENT_AUTH_ENABLE_LEGACY_API_KEY_FALLBACK else "legacy_auto"

        if not result:
            _put_cached_agent_auth(key_hash, None)
            if metrics is not None:
                metrics["auth_lookup_ms"] = int((time.perf_counter() - started) * 1000)
                metrics["auth_cache_hit"] = False
                metrics["auth_source"] = "not_found"
            return None

        agent = _normalize_agent_row(result)
        _put_cached_agent_auth(key_hash, agent)
        if metrics is not None:
            metrics["auth_lookup_ms"] = int((time.perf_counter() - started) * 1000)
            metrics["auth_cache_hit"] = False
            metrics["auth_source"] = source
        return agent
    except Exception as e:
        if is_asyncpg_busy_error(e):
            if metrics is not None:
                metrics["auth_lookup_ms"] = int((time.perf_counter() - started) * 1000)
                metrics["auth_cache_hit"] = False
                metrics["auth_source"] = "error_transient"
            raise AgentAuthLookupTransientError(str(e)) from e

        if _is_missing_table_error(e, "api_keys") or _is_missing_table_error(e, "agent_api_keys"):
            _clear_auth_key_table_cache()
        should_try_legacy = _AGENT_AUTH_ENABLE_LEGACY_API_KEY_FALLBACK or _is_missing_table_error(e, "api_keys") or _is_missing_table_error(e, "agent_api_keys")
        # Compatibility rollback: if hash key table is missing or fallback explicitly enabled,
        # use legacy agents.api_key path.
        if should_try_legacy:
            try:
                legacy = await database.fetch_one(
                    "SELECT * FROM agents WHERE api_key = :api_key LIMIT 1",
                    {"api_key": api_key},
                )
                if legacy:
                    normalized = _normalize_agent_row(legacy)
                    _put_cached_agent_auth(key_hash, normalized)
                    if metrics is not None:
                        metrics["auth_lookup_ms"] = int((time.perf_counter() - started) * 1000)
                        metrics["auth_cache_hit"] = False
                        metrics["auth_source"] = (
                            "legacy_fallback"
                            if _AGENT_AUTH_ENABLE_LEGACY_API_KEY_FALLBACK
                            else "legacy_auto"
                        )
                    return normalized
            except Exception:
                pass
        print(f"Error in get_agent_by_key: {e}")
        if metrics is not None:
            metrics["auth_lookup_ms"] = int((time.perf_counter() - started) * 1000)
            metrics["auth_cache_hit"] = False
            metrics["auth_source"] = "error"
        return None


async def get_agent(agent_id: str) -> Optional[Dict[str, Any]]:
    """获取 Agent 信息（不含 API Key）"""
    try:
        # Use raw SQL to avoid schema conflicts (table doesn't have 'id' column)
        result = await database.fetch_one(
            "SELECT * FROM agents WHERE agent_id = :agent_id LIMIT 1",
            {"agent_id": agent_id}
        )
        
        if result:
            agent = dict(result)
            raw_allowed = agent.get("allowed_merchants")
            if isinstance(raw_allowed, str):
                try:
                    raw_allowed = json.loads(raw_allowed)
                except Exception:
                    raw_allowed = []
            if raw_allowed is None:
                agent["allowed_merchants"] = None
            elif isinstance(raw_allowed, list):
                agent["allowed_merchants"] = [str(m).strip() for m in raw_allowed if str(m or "").strip()] or []
            else:
                agent["allowed_merchants"] = []
            # 移除敏感信息
            agent.pop("api_key", None)
            agent.pop("api_key_hash", None)
            return agent
        return None
    except Exception as e:
        print(f"Error in get_agent: {e}")
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
    
    try:
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
    except Exception:
        # If table/columns not present yet, skip updating stats (non-fatal)
        return


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
    
    try:
        await database.execute(query)
    except Exception:
        # Ignore logging failures to avoid breaking API flow
        pass
    return request_id


async def check_rate_limit(agent_id: str, rate_limit: Optional[int] = None) -> tuple[bool, int, int]:
    """
    检查 Agent 是否超过速率限制
    返回: (是否允许, 当前分钟内请求数, 限制数)
    """
    # 获取 Agent 配置（允许调用方传入 limit 以避免重复 DB roundtrip）
    if rate_limit is None:
        agent = await get_agent(agent_id)
        if not agent:
            return False, 0, 0
        rate_limit = agent.get("rate_limit", 100)

    try:
        limit_value = int(rate_limit or 100)
    except Exception:
        limit_value = 100
    
    # 统计最近一分钟的请求数
    one_minute_ago = datetime.utcnow() - timedelta(minutes=1)
    # Avoid full-table COUNT(*) scans on hot paths by limiting to limit+1 rows.
    limit_plus_one = max(1, limit_value + 1)
    query = """
        SELECT COUNT(*) as count
        FROM (
            SELECT 1
            FROM agent_usage_logs
            WHERE agent_id = :agent_id
              AND timestamp > :since
            LIMIT :limit
        ) t
    """
    
    try:
        result = await database.fetch_one(
            query,
            {"agent_id": agent_id, "since": one_minute_ago, "limit": limit_plus_one}
        )
        current_count = result["count"] if result else 0
        return current_count < limit_value, current_count, limit_value
    except Exception:
        # If logs table missing, allow request with default limits
        return True, 0, limit_value


async def check_daily_quota(agent_id: str, daily_quota: Optional[int] = None) -> tuple[bool, int, int]:
    """
    检查 Agent 是否超过每日配额
    返回: (是否允许, 今日使用量, 配额)
    """
    if daily_quota is None:
        agent = await get_agent(agent_id)
        if not agent:
            return False, 0, 0
        daily_quota = agent.get("daily_quota", 10000)

    try:
        quota_value = int(daily_quota or 10000)
    except Exception:
        quota_value = 10000
    
    # 统计今天的请求数
    today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    limit_plus_one = max(1, quota_value + 1)
    query = """
        SELECT COUNT(*) as count
        FROM (
            SELECT 1
            FROM agent_usage_logs
            WHERE agent_id = :agent_id
              AND timestamp >= :today
            LIMIT :limit
        ) t
    """
    
    try:
        result = await database.fetch_one(
            query,
            {"agent_id": agent_id, "today": today, "limit": limit_plus_one}
        )
        today_count = result["count"] if result else 0
        return today_count < quota_value, today_count, quota_value
    except Exception:
        # If logs table missing, allow within quota by default
        return True, 0, quota_value


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
