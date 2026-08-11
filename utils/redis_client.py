"""
Async Redis client helper
"""
from typing import Optional

try:
    import redis.asyncio as redis  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    redis = None  # type: ignore

import os

from config.settings import settings


def _timeout_seconds(env_name: str, default: float) -> float:
    """Positive float from env, else the default. A 0 or negative value would
    mean "no timeout" to redis-py, which is the failure mode being closed."""
    try:
        value = float(str(os.getenv(env_name, "")).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default

_client: Optional["redis.Redis"] = None


def get_redis_client() -> Optional["redis.Redis"]:
    """Return a singleton async Redis client if REDIS_URL and redis lib exist."""
    global _client
    if _client is not None:
        return _client
    if not settings.redis_url or redis is None:
        return None
    # SOCKET TIMEOUTS ARE LOAD-BEARING, not tuning. RateLimitMiddleware calls
    # this client on the request path for every /agent/* request, and its error
    # handler fails OPEN — but failing open on the VERDICT is worthless if the
    # call itself hangs. Without these, a blackholed Redis (accepting TCP but
    # never answering) turns a rate-limit check into an unbounded stall on the
    # public agent surface, which had made zero Redis calls before that change.
    # Adversarial review measured 1.02s of added latency across 10 requests
    # against a 50ms-stalling Redis with no timeout set.
    _client = redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=_timeout_seconds("REDIS_CONNECT_TIMEOUT_SECONDS", 2.0),
        socket_timeout=_timeout_seconds("REDIS_SOCKET_TIMEOUT_SECONDS", 2.0),
    )
    return _client









