"""
Agent 管理数据库模型
处理 AI Agent 的注册、认证和使用追踪
"""

from sqlalchemy import Table, Column, Integer, String, Text, DateTime, JSON, Boolean, Numeric
from sqlalchemy.sql import func
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List, Tuple
from collections import OrderedDict
import asyncio
import contextvars
import logging
import secrets
import hashlib
import json
import os
import time
import unicodedata

from db.database import metadata, database, IS_POSTGRES
from services.synthetic_call import is_synthetic_call
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

# A key's "last used" time (api_keys.last_used / agent_api_keys.last_used_at) was read by the
# developer and employee portals but written by nothing, so every key read "Never". It is now stamped
# when a key-table lookup admits an active agent's key, OFF the request path: get_agent_by_key only
# drops a (table, hash, time) item into a bounded in-process queue (put_nowait, O(1), never awaits),
# and one drainer task, started by the app lifespan in a fresh context (its own DB connection, not
# the lifespan's), writes them under lock_timeout / statement_timeout and an overall deadline. With
# no drainer (scripts, tests, a process that never started it) nothing is queued or written.
# At most once per window per key per process (the memo), and the UPDATE's own WHERE skips a row
# another instance stamped in the window. Precision: "within about this many seconds".
_KEY_LAST_USED_STAMP_SECONDS = max(
    60,
    min(86400, int(os.getenv("AGENT_KEY_LAST_USED_STAMP_SECONDS", "300") or 300)),
)
_KEY_LAST_USED_MEMO_MAX_ENTRIES = 20000
_KEY_LAST_USED_MEMO: "OrderedDict[str, float]" = OrderedDict()
_KEY_LAST_USED_QUEUE_MAX = 2000
# A drop (queue full) or a skipped write is retried at the next lookup after the window.
_KEY_LAST_USED_WRITE_DEADLINE_SECONDS = 5.0
_key_last_used_queue: "Optional[asyncio.Queue]" = None
_key_last_used_dropped = 0
# Drops are logged at most once per this many seconds, with the running total.
_KEY_LAST_USED_DROP_LOG_SECONDS = 60.0
_key_last_used_drop_logged_at: Optional[float] = None
# The drainer is restarted if it ever ends without a shutdown, at most this many times per window;
# past that, stamping stops (queue None) with one ERROR rather than restarting in a tight loop.
_KEY_LAST_USED_MAX_RESTARTS = 5
_KEY_LAST_USED_RESTART_WINDOW_SECONDS = 600.0
_key_last_used_restarts: List[float] = []
_key_last_used_task: "Optional[asyncio.Task]" = None
# WARNING and above: prod's root logger is at WARNING, so INFO from a module logger is dropped.
_key_last_used_logger = logging.getLogger("db.agents.key_last_used")


class AgentAuthLookupTransientError(RuntimeError):
    """Raised when agent key lookup fails due to transient DB/pool state."""


def _agent_auth_cache_key(api_key: str) -> str:
    return hashlib.sha256(str(api_key or "").encode("utf-8")).hexdigest()


def agent_is_active(agent: Optional[Dict[str, Any]]) -> bool:
    """THE activity rule for an agent row: is_active when set, else status == 'active', and an
    agent with neither (NULL is_active, no status) is active. Every door that admits an agent's
    key reads this one function: get_agent_context (403), resolve_agent_id_by_api_key, the
    internal introspection verdict the gateway 403s on, and ADR-025 issuing-agent attribution."""
    if not agent:
        return False
    is_active = agent.get("is_active")
    if is_active is None:
        status = agent.get("status")
        return (str(status).strip().lower() == "active") if status else True
    return bool(is_active)


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
        agent["is_active"] = agent_is_active(agent)

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


def agent_auth_cache_has(api_key: str) -> bool:
    """True when get_agent_by_key would answer this key from the auth cache, without a query.

    Read only: nothing is evicted, refreshed or reordered, and a found and a not-found entry
    both count. For a caller that must decide whether a lookup is worth a query before it
    makes one; the answer itself still comes from get_agent_by_key."""
    entry = _AGENT_AUTH_CACHE.get(_agent_auth_cache_key(api_key))
    if not isinstance(entry, dict):
        return False
    return float(entry.get("expires_at") or 0) > time.monotonic()


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


def evict_agent_auth_cache(agent_id: str) -> int:
    """Drop THIS process's positive auth-cache entries for agent_id; returns how many.

    Called after a key rotation so the revoked key stops authenticating here at once. Other
    instances keep their entry until it expires (AGENT_AUTH_CACHE_POSITIVE_TTL_SECONDS, 60s by
    default): the cache is per-process and there is no cross-instance invalidation.
    """
    stale = [
        cache_key
        for cache_key, entry in _AGENT_AUTH_CACHE.items()
        if isinstance(entry, dict)
        and isinstance(entry.get("agent"), dict)
        and entry["agent"].get("agent_id") == agent_id
    ]
    for cache_key in stale:
        _AGENT_AUTH_CACHE.pop(cache_key, None)
    return len(stale)


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
    if row is None:
        return None
    agent = dict(row)
    # Never surface authentication secrets from a DID lookup — a route that
    # returns this dict must not leak the agent's API key / hash.
    agent.pop("api_key", None)
    agent.pop("api_key_hash", None)
    return agent


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


async def set_agent_public_key(agent_id: str, public_key: str) -> None:
    """
    Persist an agent's AP2 **signing** public key into ``agents.public_key`` (the
    raw-PEM pilot key read by ``consent_service.get_agent_identity``; migration
    021). Overwrites any prior value (key rotation for that agent). This is NOT a
    PSP publishable key. Authorization is the CALLER's responsibility — this helper
    binds nothing; pass only an ``agent_id`` the caller is authenticated as.
    """
    await database.execute(
        "UPDATE agents SET public_key = :public_key, updated_at = NOW() WHERE agent_id = :agent_id",
        {"public_key": public_key, "agent_id": agent_id},
    )


# Redacted value written to agents.api_key for agents whose key lives only as a hash in a key
# table. The column is NOT NULL UNIQUE and several legacy readers still select it, so it must
# hold a per-agent, non-secret, recognisable string rather than NULL or the plaintext key.
#
# THE MARKER IS NEVER A CREDENTIAL. agent_id is public (registration response, metrics, employee
# console), so `redacted:<agent_id>` is guessable; every lookup that compares a PRESENTED key
# against agents.api_key (the legacy fallbacks below) must refuse it explicitly, and nothing may
# ever hash it into a key table (routes/agent_keys.py legacy branch, the redaction script).
AGENT_API_KEY_REDACTED_PREFIX = "redacted:"


def redacted_agent_api_key(agent_id: str) -> str:
    return f"{AGENT_API_KEY_REDACTED_PREFIX}{agent_id}"


def is_redacted_agent_api_key(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(AGENT_API_KEY_REDACTED_PREFIX)


# Legacy plaintext lookups exclude the marker at the SQL level too, so the guard does not depend
# on every caller remembering the Python check.
_LEGACY_API_KEY_LOOKUP_SQL = (
    "SELECT * FROM agents WHERE api_key = :api_key AND api_key NOT LIKE 'redacted:%' LIMIT 1"
)


# Only an ACTIVE key row is stamped (a revoked key that still resolved through a race is not).
_KEY_LAST_USED_SQL = {
    "api_keys": (
        "UPDATE api_keys SET last_used = :now WHERE key_hash = :key_hash AND status = 'active' "
        "AND (last_used IS NULL OR last_used < :cutoff)"
    ),
    "agent_api_keys": (
        "UPDATE agent_api_keys SET last_used_at = :now WHERE key_hash = :key_hash "
        "AND COALESCE(is_active, TRUE) = TRUE AND (last_used_at IS NULL OR last_used_at < :cutoff)"
    ),
}


def _enqueue_key_last_used(*, source: str, api_key: str, key_hash_sha256: str, agent: Dict[str, Any]) -> None:
    """Queue a last-used stamp for a key-table key that just admitted an ACTIVE agent. Synchronous and
    O(1): never awaits, never raises, never touches the DB. Skipped for a synthetic (CI probe) call,
    an inactive agent (its request 403s next; a refused call is not a use), a legacy agents.api_key
    match (no per-key row), and a process with no drainer (scripts such as the redaction script)."""
    try:
        queue = _key_last_used_queue
        if queue is None or not agent_is_active(agent) or is_synthetic_call():
            return
        if source == "api_keys":
            table = "api_keys"
            # api_keys.last_used is TIMESTAMP (no zone) and routes/agent_keys.py compares it with
            # datetime.utcnow(): store naive UTC.
            now = datetime.utcnow()
            key_hash = key_hash_sha256
        elif source in {"agent_api_keys", "agent_api_keys_sha256", "agent_api_keys_md5"}:
            table = "agent_api_keys"
            now = datetime.now(timezone.utc)
            key_hash = (
                hashlib.md5(str(api_key or "").encode("utf-8")).hexdigest()
                if source == "agent_api_keys_md5"
                else key_hash_sha256
            )
        else:
            return

        memo_key = f"{table}:{key_hash}"
        mono = time.monotonic()
        last = _KEY_LAST_USED_MEMO.get(memo_key)
        if last is not None and mono - last < _KEY_LAST_USED_STAMP_SECONDS:
            return
        try:
            queue.put_nowait((table, key_hash, now))
        except asyncio.QueueFull:
            global _key_last_used_dropped, _key_last_used_drop_logged_at
            _key_last_used_dropped += 1
            if _key_last_used_drop_logged_at is None or mono - _key_last_used_drop_logged_at >= _KEY_LAST_USED_DROP_LOG_SECONDS:
                _key_last_used_drop_logged_at = mono
                _key_last_used_logger.warning(
                    "agent key last-used queue full: %d stamps dropped since start (queue max %d)",
                    _key_last_used_dropped,
                    _KEY_LAST_USED_QUEUE_MAX,
                )
            return  # not memoized: the next lookup after a free slot tries again
        # Memo on enqueue, so a failed write is retried once per window, not once per lookup.
        _KEY_LAST_USED_MEMO[memo_key] = mono
        _KEY_LAST_USED_MEMO.move_to_end(memo_key)
        while len(_KEY_LAST_USED_MEMO) > _KEY_LAST_USED_MEMO_MAX_ENTRIES:
            _KEY_LAST_USED_MEMO.popitem(last=False)
    except Exception:  # noqa: BLE001 - a usage stamp must never fail an authenticated call
        return


async def _write_key_last_used(table: str, key_hash: str, now: datetime) -> None:
    params = {"now": now, "key_hash": key_hash, "cutoff": now - timedelta(seconds=_KEY_LAST_USED_STAMP_SECONDS)}
    if IS_POSTGRES:
        # Skip rather than wait: a row locked by a rotation, or a slow DB, is retried next window.
        async with database.transaction():
            await database.execute("SET LOCAL lock_timeout = '1s'")
            await database.execute("SET LOCAL statement_timeout = '2s'")
            await database.execute(_KEY_LAST_USED_SQL[table], params)
    else:
        await database.execute(_KEY_LAST_USED_SQL[table], params)


async def _drain_key_last_used(queue: "asyncio.Queue") -> None:
    while True:
        table, key_hash, now = await queue.get()
        try:
            await asyncio.wait_for(_write_key_last_used(table, key_hash, now), timeout=_KEY_LAST_USED_WRITE_DEADLINE_SECONDS)
        except asyncio.CancelledError:
            # Only a cancel of THIS task (shutdown) stops the drainer. A CancelledError raised from
            # inside the write (a driver or pool cancelling its own work) is one failed stamp: before,
            # it ended the drainer for good, silently.
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            _key_last_used_logger.warning("agent key last-used stamp skipped: cancelled inside the write")
        except Exception as exc:  # noqa: BLE001 - one failed stamp must not stop the drainer
            _key_last_used_logger.warning("agent key last-used stamp skipped: %s", type(exc).__name__)
        finally:
            queue.task_done()


def _spawn_key_last_used_drainer(queue: "asyncio.Queue") -> "asyncio.Task":
    global _key_last_used_task
    task = asyncio.get_running_loop().create_task(
        _drain_key_last_used(queue), name="agent-key-last-used", context=contextvars.Context()
    )
    _key_last_used_task = task
    task.add_done_callback(_on_key_last_used_drainer_done)
    return task


def _on_key_last_used_drainer_done(task: "asyncio.Task") -> None:
    """Restart a drainer that ended without a shutdown, and say so."""
    global _key_last_used_queue
    # A stopped drainer (queue None: shutdown, or the restart cap) and a superseded task stay down.
    if task is not _key_last_used_task or _key_last_used_queue is None:
        return
    reason = "cancelled" if task.cancelled() else repr(task.exception())
    now = time.monotonic()
    _key_last_used_restarts[:] = [t for t in _key_last_used_restarts if now - t < _KEY_LAST_USED_RESTART_WINDOW_SECONDS]
    if len(_key_last_used_restarts) >= _KEY_LAST_USED_MAX_RESTARTS:
        _key_last_used_logger.error(
            "agent key last-used drainer ended again (%s) after %d restarts in %.0fs; stamping stopped",
            reason,
            len(_key_last_used_restarts),
            _KEY_LAST_USED_RESTART_WINDOW_SECONDS,
        )
        _key_last_used_queue = None
        return
    _key_last_used_restarts.append(now)
    _key_last_used_logger.error("agent key last-used drainer ended unexpectedly (%s); restarting", reason)
    try:
        _spawn_key_last_used_drainer(_key_last_used_queue)
    except Exception as exc:  # noqa: BLE001 - e.g. the loop is closing
        _key_last_used_logger.error("agent key last-used drainer not restarted: %s", type(exc).__name__)
        _key_last_used_queue = None


def start_key_last_used_drainer() -> "asyncio.Task":
    """Start the one drainer for this process (call from the app lifespan, never from a request).

    The task runs in a FRESH contextvars.Context: databases==0.7 keeps its Connection in a ContextVar
    and a task inherits its creator's, so a task made from the lifespan (or a request) would share
    that connection. An empty context makes the drainer open its own. If it ever ends without a
    shutdown, a done-callback logs it and starts a new one (bounded, see _KEY_LAST_USED_MAX_RESTARTS)."""
    global _key_last_used_queue
    _key_last_used_restarts.clear()
    queue: asyncio.Queue = asyncio.Queue(maxsize=_KEY_LAST_USED_QUEUE_MAX)
    _key_last_used_queue = queue
    return _spawn_key_last_used_drainer(queue)


def stop_key_last_used_drainer() -> None:
    """Stop queueing and restarting (the caller cancels the task). Items still queued are dropped."""
    global _key_last_used_queue
    _key_last_used_queue = None


async def shutdown_key_last_used_drainer(timeout: float = 3.0) -> None:
    """For the lifespan: stop, cancel the live drainer (a restarted one included), and wait for it
    at most `timeout` seconds, so a stuck write can't hold up shutdown."""
    stop_key_last_used_drainer()
    task = _key_last_used_task
    if task is None or task.done():
        return
    task.cancel()
    # asyncio.wait, not wait_for: on timeout wait_for cancels and then WAITS for the cancel to land,
    # which a task stuck in a write that ignores it would never do. wait returns at the deadline.
    await asyncio.wait({task}, timeout=timeout)


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

    # The redaction marker stored in agents.api_key is never a key (see AGENT_API_KEY_REDACTED_PREFIX).
    if is_redacted_agent_api_key(api_key):
        if metrics is not None:
            metrics["auth_lookup_ms"] = int((time.perf_counter() - started) * 1000)
            metrics["auth_source"] = "redacted_marker_refused"
        return None

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
                _LEGACY_API_KEY_LOOKUP_SQL,
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
        _enqueue_key_last_used(source=source, api_key=api_key, key_hash_sha256=key_hash, agent=agent)
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
                    _LEGACY_API_KEY_LOOKUP_SQL,
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


async def resolve_agent_id_by_api_key(
    api_key: str, metrics_out: Optional[Dict[str, Any]] = None
) -> Optional[str]:
    """agent_id for an X-API-Key value of an ACTIVE agent, or None.

    Routes that only need the caller's agent_id used to run
    `SELECT agent_id FROM agents WHERE api_key = :key`, which matches the plaintext column
    only. Keys are now persisted hash-only (agents.api_key holds a redacted marker), so every
    resolver must go through get_agent_by_key: hash table first, legacy plaintext fallback
    second, and the same auth cache.

    A deactivated agent's key resolves to None here, so every caller refuses it the way it
    refuses an unknown key: these routes use the agent_id as the caller's identity, and
    get_agent_context 403s the same agent. get_agent_by_key itself stays neutral (it returns
    inactive agents) so get_agent_context can answer 403 rather than 401 and introspection
    can report is_active.

    `metrics_out` is passed to get_agent_by_key: its `auth_source` reads "error" when the lookup
    failed (get_agent_by_key answers None for that too), so a caller can tell "no such active
    agent" from "could not look".
    """
    key = str(api_key or "").strip()
    if not key:
        return None
    agent = await get_agent_by_key(key, metrics_out=metrics_out)
    if not agent_is_active(agent):
        return None
    agent_id = agent.get("agent_id")
    return str(agent_id) if agent_id else None


async def resolve_active_agent_id(agent_id: Any) -> Optional[str]:
    """agent_id when it names an existing ACTIVE agent, else None.

    For an agent_id read from a signed claim, e.g. the agent-portal JWT (7-day session). The
    signature proves which agent the caller was at login, not that the agent is still active:
    deactivation does not revoke issued tokens, and portal login checks users.active, not
    agents.is_active. Reads the row (get_agent), not the per-key auth cache, so a deactivation
    is seen on the next request. A missing or unreadable row is None: fail closed.
    """
    candidate = str(agent_id or "").strip()
    if not candidate:
        return None
    agent = await get_agent(candidate)
    if not agent_is_active(agent):
        return None
    resolved = str(agent.get("agent_id") or "").strip()
    return resolved if resolved == candidate else None


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


# ============================================================================
# The agent's buyer-facing name (agents.buyer_display_name by hand, else the portal name with checks)
# ============================================================================

#: The width of agents.buyer_display_name (migration 297).
BUYER_DISPLAY_NAME_MAX_LENGTH = 64

# Not on the `agents` Table on purpose: a model column is named by every model SELECT, and one boot whose heal
# lost the lock race would then break every agent read. Only this query names the column.
_SELECT_BUYER_DISPLAY_NAME_SQL = (
    "SELECT buyer_display_name, agent_name FROM agents WHERE agent_id = :agent_id LIMIT 1"
)

#: THE one list of what a portal-registered name (agents.agent_name) may not contain, matched against every
#: `_name_skeleton` (case, spaces, punctuation and accents dropped; Cyrillic and Greek lookalike letters and
#: digit/symbol stand-ins read as the Latin letter they imitate): a name that looks like a payment brand, a bank
#: or Pivota itself is a phishing lever on a payment page, so it shows nothing. A substring match on purpose:
#: over-blocking costs a missing line; under-blocking puts "Visa Secure" on the page a buyer pays from.
#: Each term is matched by its own skeleton (`_BLOCKED_TERM_SKELETONS`), reduced exactly as a name is.
PORTAL_NAME_BLOCKED_TERMS = (
    "visa", "mastercard", "amex", "americanexpress", "discover", "applepay", "googlepay", "paypal", "stripe",
    "pivota", "agenpaz", "agenpas", "bank", "secure",
)

# What other code paths show for an agent with no name ("Agent", "Unknown"): placeholders, never a name.
_PORTAL_NAME_PLACEHOLDERS = frozenset({"agent", "unknown", "unknownagent"})

# Letters of other scripts that read as a Latin letter, both cases, applied BEFORE case folding (a capital and its
# small form can imitate different Latin letters: Greek capital NU is "N", small nu is "v"), and the digit and
# symbol stand-ins. "1" is
# not here: `_name_skeleton` puts it in the i/l class.
_CONFUSABLE_FOLD = str.maketrans({
    # Cyrillic capitals, then small letters.
    "А": "a", "В": "b", "Е": "e", "К": "k", "М": "m", "Н": "h", "О": "o", "Р": "p", "С": "c", "Т": "t",
    "У": "y", "Х": "x", "І": "i", "Ј": "j", "Ѕ": "s", "Ԛ": "q", "Ԝ": "w", "Һ": "h", "Ӏ": "l", "Ү": "y",
    "а": "a", "в": "b", "е": "e", "к": "k", "м": "m", "н": "h", "о": "o", "р": "p", "с": "c", "т": "t",
    "у": "y", "х": "x", "і": "i", "ј": "j", "ѕ": "s", "ԁ": "d", "ԛ": "q", "ԝ": "w", "һ": "h", "ӏ": "l",
    "ү": "y",
    # Cyrillic letters shaped like a Greek lookalike capital or a Latin letter turned (both cases).
    "Л": "l", "л": "l", "П": "n", "п": "n", "Г": "r", "г": "r", "И": "n", "Ш": "w", "ш": "w", "Ь": "b", "ь": "b",
    # Greek capitals, then small letters.
    "Α": "a", "Β": "b", "Ε": "e", "Ζ": "z", "Η": "h", "Ι": "i", "Κ": "k", "Μ": "m", "Ν": "n", "Ο": "o",
    "Ρ": "p", "Τ": "t", "Υ": "y", "Χ": "x", "Ϲ": "c", "Ϳ": "j", "Λ": "l", "Π": "n", "Σ": "e", "Δ": "a",
    "α": "a", "β": "b", "γ": "y", "ε": "e", "ζ": "z", "η": "n", "ι": "i", "κ": "k", "μ": "m", "ν": "v",
    "ο": "o", "ρ": "p", "τ": "t", "υ": "u", "χ": "x", "ω": "w", "ϲ": "c", "ϳ": "j", "λ": "l", "π": "n",
    # Latin letters that read as another Latin letter.
    "ı": "i", "ɡ": "g", "ɩ": "i",
    # Digit and symbol stand-ins.
    "0": "o", "3": "e", "4": "a", "5": "s", "7": "t", "$": "s", "@": "a", "!": "i", "|": "l",
})

# The scripts whose letters pass for Latin ones: a portal name mixing Latin letters with either is refused.
_LOOKALIKE_SCRIPTS = ("CYRILLIC", "GREEK")

# THE allowlist of scripts a portal name's letters may come from, by the first word of each letter's Unicode name
# after NFKD (`_letter_script`): a letter of any other script refuses the name, so a lookalike from a script the
# fold has never heard of (Cherokee, Armenian, Lisu, Canadian syllabics, ...) never shows. How they resolve:
# Han ideographs are "CJK UNIFIED IDEOGRAPH-xxxx" (compatibility ideographs and Kangxi radicals decompose to them)
# -> "CJK"; the iteration mark 々 -> "IDEOGRAPHIC"; kana -> "HIRAGANA" / "KATAKANA" (halfwidth kana decompose to
# KATAKANA), the long-vowel mark ー -> "KATAKANA-HIRAGANA"; Hangul syllables decompose to jamo "HANGUL CHOSEONG /
# JUNGSEONG / JONGSEONG ..." and compatibility jamo to the same -> "HANGUL". LATIN letters pass only as ASCII a-z
# or A-Z (`_portal_name_letters_allowed`).
_PORTAL_NAME_SCRIPTS = frozenset({
    "LATIN", "CJK", "IDEOGRAPHIC", "HIRAGANA", "KATAKANA", "KATAKANA-HIRAGANA", "HANGUL", "CYRILLIC", "GREEK",
    "ARABIC", "HEBREW", "THAI", "DEVANAGARI",
})

# Control, format (bidi overrides, zero-width), surrogate, private-use and unassigned code points, and line or
# paragraph separators: none belongs in a name a buyer reads, and several can make one read as another.
_REFUSED_NAME_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})


def clean_buyer_display_name(raw: Any) -> Optional[str]:
    """The stored buyer-facing name if it has a name's shape, else None. Never repairs one: a name that fails
    is not shown at all (the page shows no "Requested by" line), so the buyer never reads an altered name.

    Shape: a string, surrounding whitespace dropped, NFC, 1-64 characters, the only space U+0020 and never two
    in a row, no code point of a refused category, at least one letter or digit."""
    if not isinstance(raw, str):
        return None
    name = raw.strip()
    if not name or len(name) > BUYER_DISPLAY_NAME_MAX_LENGTH:
        return None
    if unicodedata.normalize("NFC", name) != name or "  " in name:
        return None
    has_letter_or_digit = False
    for ch in name:
        category = unicodedata.category(ch)
        if category in _REFUSED_NAME_CATEGORIES or (ch.isspace() and ch != " "):
            return None
        if category[0] in ("L", "N"):
            has_letter_or_digit = True
    return name if has_letter_or_digit else None


# Letter shapes that read as one another in common sans-serif fonts. Expanded the SAME way in a name and in each
# term ("m" -> "rn", so "Arnex" and "Amex" share "arnex"; a plain "vv" -> "w" would instead turn "VVisa" into
# "wisa" and miss "visa"), then i, l, capital I and 1 are one letter ("PayPaI" reads as "paypal").
_SKELETON_EXPANSIONS = (("m", "rn"), ("d", "cl"), ("w", "vv"))
_SKELETON_I_CLASS = str.maketrans({"l": "i", "1": "i"})


def _name_skeleton(name: str) -> str:
    """The name as the block list reads it: compatibility forms folded (fullwidth, ligatures), accents dropped,
    lookalike letters and stand-ins read as the Latin letter they imitate (`_CONFUSABLE_FOLD`), case folded, only
    letters and digits kept, then `_SKELETON_EXPANSIONS` and the i/l class (capital I is already i after case
    folding). "Ｐаy-PaI", "V1sa" and "Arnex" read as the skeletons of "paypal", "visa" and "amex"."""
    folded = unicodedata.normalize("NFKD", name).translate(_CONFUSABLE_FOLD).casefold()
    key = "".join(ch for ch in folded if ch.isalnum())
    for shape, expanded in _SKELETON_EXPANSIONS:
        key = key.replace(shape, expanded)
    return key.translate(_SKELETON_I_CLASS)


_BLOCKED_TERM_SKELETONS = tuple(_name_skeleton(term) for term in PORTAL_NAME_BLOCKED_TERMS)
_PLACEHOLDER_SKELETONS = frozenset(_name_skeleton(placeholder) for placeholder in _PORTAL_NAME_PLACEHOLDERS)


def _letter_script(ch: str) -> str:
    """The script a letter belongs to, as its Unicode name starts ("LATIN", "CYRILLIC", "GREEK", "CJK", ...)."""
    return unicodedata.name(ch, "").split(" ", 1)[0]


def portal_name_mixes_lookalike_scripts(name: str) -> bool:
    """True when the name has Latin letters AND Cyrillic or Greek ones: the shape of a lookalike spelling. A name
    wholly in Cyrillic or Greek, or Latin mixed with any other script ("Meitu 美图"), is not refused here."""
    scripts = {_letter_script(ch) for ch in unicodedata.normalize("NFKD", name) if ch.isalpha()}
    return "LATIN" in scripts and any(script in scripts for script in _LOOKALIKE_SCRIPTS)


def _portal_name_letters_allowed(name: str) -> bool:
    """True when every letter of the name, after NFKD with combining marks dropped, is ASCII a-z / A-Z or a letter
    of an allowed script (`_PORTAL_NAME_SCRIPTS`). Any other Latin letter (small capitals, IPA, stroke or hook
    letters, ø, ß, ł) and any letter of another script refuses the name: an allowlist, not a table of lookalikes
    that would keep leaking. Over-blocks a few real names; a name set by hand is the way to show one."""
    for ch in unicodedata.normalize("NFKD", name):
        if not ch.isalpha():  # digits, spaces, punctuation, and combining marks (never letters) are not judged
            continue
        script = _letter_script(ch)
        if script == "LATIN":
            if not ("a" <= ch <= "z" or "A" <= ch <= "Z"):
                return False
        elif script not in _PORTAL_NAME_SCRIPTS:
            return False
    return True


def portal_name_is_blocked(name: str) -> bool:
    """True when a portal-registered name must never show: a letter outside the allowlist, a placeholder, a
    blocked term in any fold, or Latin mixed with a lookalike script. Never applied to a name set by hand."""
    if not _portal_name_letters_allowed(name):
        return True
    skeleton = _name_skeleton(name)
    # Placeholders also against the unfolded key: "Agent!" is "agent", not the "agenti" its "!" folds to.
    plain = "".join(ch for ch in unicodedata.normalize("NFKD", name).casefold() if ch.isalnum())
    if plain in _PORTAL_NAME_PLACEHOLDERS or skeleton in _PLACEHOLDER_SKELETONS:
        return True
    if any(term in skeleton for term in _BLOCKED_TERM_SKELETONS):
        return True
    return portal_name_mixes_lookalike_scripts(name)


async def get_buyer_display_name(agent_id: str) -> Optional[str]:
    """The name the buyer page shows for this agent, or None (no "Requested by" line).

    1. A name set by hand (agents.buyer_display_name, migration 297) decides whenever it is not NULL: shown if it
       has a name's shape, else nothing. It never falls through to the portal name, so storing '' hides a portal
       name without the developer.
    2. Otherwise the name the agent registered with in the developer portal (agents.agent_name, this agent's row
       only), when it has a name's shape and `portal_name_is_blocked` is False. A rename hides it until checked
       again (`portal_name_rename_hide`).
    Nothing else, ever: not the user's full_name, not a legacy `name` column, not "Agent" or "Unknown".
    Raises on a read fault; the caller decides what a fault means."""
    row = await database.fetch_one(_SELECT_BUYER_DISPLAY_NAME_SQL, {"agent_id": agent_id})
    if row is None:
        return None
    data = dict(row)
    reviewed = data.get("buyer_display_name")
    if reviewed is not None:
        return clean_buyer_display_name(reviewed)
    registered = clean_buyer_display_name(data.get("agent_name"))
    if registered is None or portal_name_is_blocked(registered):
        return None
    return registered


# A rename of a name the page shows from the portal: hidden ('') in the same transaction as the rename, until a
# gated one-off checks the new name and sets the column back to NULL (or to a name). Only while the column is NULL:
# a name set by hand (or '') already decides and does not depend on agent_name. Raw SQL: not a model column.
_HIDE_PORTAL_NAME_ON_RENAME_SQL = (
    "UPDATE agents SET buyer_display_name = '' "
    "WHERE agent_id = :agent_id AND buyer_display_name IS NULL "
    "AND (agent_name <> :new_name OR agent_name IS NULL)"
)


def portal_name_rename_hide(agent_id: str, new_name: str) -> Tuple[str, Dict[str, Any]]:
    """The statement (sql, values) that hides the portal name of an agent renamed to new_name. Run it with the
    writer's own `database`, inside the transaction that writes agents.agent_name, BEFORE that write; a fault
    must fail the rename, never let it land unhidden. A no-op when the name is unchanged or a name set by hand
    decides."""
    return _HIDE_PORTAL_NAME_ON_RENAME_SQL, {"agent_id": agent_id, "new_name": new_name}


async def update_agent_stats(
    agent_id: str,
    increment_requests: int = 0,
    increment_orders: int = 0,
    add_gmv: float = 0
):
    """更新 Agent 统计数据"""
    if is_synthetic_call():
        return
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
    error_message: Optional[str] = None,
    request_id: Optional[str] = None,
    timestamp: Optional[datetime] = None,
) -> str:
    """记录 Agent API 使用

    `request_id` / `timestamp` are passed only by middleware/usage_logger.py, when this call's row is
    the request's one row there; otherwise a fresh request_id and the column default apply.
    """
    request_id = request_id or f"req_{secrets.token_hex(16)}"
    if is_synthetic_call():
        # Our own eval traffic writes no usage row (services/synthetic_call.py). Only the gateway's
        # service key can carry it, and that key's quota is not enforced.
        return request_id
    
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
        order_amount=order_amount,
        **({"timestamp": timestamp} if timestamp is not None else {}),
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
