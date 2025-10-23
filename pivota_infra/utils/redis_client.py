"""
Async Redis client helper
"""
from typing import Optional

try:
    import redis.asyncio as redis  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    redis = None  # type: ignore

from config.settings import settings

_client: Optional["redis.Redis"] = None


def get_redis_client() -> Optional["redis.Redis"]:
    """Return a singleton async Redis client if REDIS_URL and redis lib exist."""
    global _client
    if _client is not None:
        return _client
    if not settings.redis_url or redis is None:
        return None
    _client = redis.from_url(settings.redis_url, decode_responses=True)
    return _client


